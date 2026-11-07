#!/usr/bin/env python3
"""local_embedder.py — 纯本地语义向量：TF-IDF + 截断 SVD（LSA）

设计：
  · 中文按 1/2-gram 切分（单字兜底），英文按词，保守停用词；
  · IDF 由本地记忆语料统计，词项与 IDF 落库（local_embed_terms），可增量重算；
  · 截断 SVD 用「幂迭代 + deflation」求前 k 个奇异三元组（纯标准库，无需 numpy）；
  · 新记忆走 folding-in：query/doc 都投影到同一个 k 维主题空间，再 L2 归一化；
  · 语料增长到 +20% 或新增 N 条时，后台重建模型；
  · 向量存 chat_memory_vectors，模型名带版本（local-lsa:vN），不会与 hash/openai 混用。

CLI：
  python -m roleplay_core.local_embedder train [--dim 128] [--min-docs 50]
      [--force] [--source memory|messages|all] [--set-default] [--no-split]
  python -m roleplay_core.local_embedder backfill [--limit 0]
  python -m roleplay_core.local_embedder stats
  python -m roleplay_core.local_embedder compare --source messages --queries "..." "..."
"""
from __future__ import annotations

import argparse
import math
import random
import re
import struct
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

if __package__ in (None, ""):                      # 直接 python roleplay_core/local_embedder.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db                                        # noqa: E402

DEFAULT_MODEL_NAME = "local-lsa"
DEFAULT_DIM = 128
MIN_DOCS = 50
MAX_TERMS = 3000
RETRAIN_GROWTH = 1.2
RETRAIN_EVERY = 20
SVD_ITERS = 12
SVD_TOL = 1e-5
SPLIT_CHARS = 140

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-z0-9][a-z0-9_\-']*")
_TAG = re.compile(r"<[^>]{1,40}>")
_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")

# 保守停用词：只去掉纯功能词，保留「不 / 我 / 你 / 他」等会改变语义的字。
_STOP = {
    "的", "了", "是", "在", "有", "和", "与", "及", "而", "也", "都", "就",
    "着", "或", "把", "被", "给", "让", "对", "从", "向", "于", "之", "其",
    "这", "那", "个", "些", "很", "更", "最", "又", "还", "并", "且", "则",
    "吗", "呢", "吧", "啊", "呀", "哦", "嗯", "啦", "么",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "and", "or", "in", "on", "at", "for", "with", "as", "by",
    "from", "but", "if", "then", "so", "do", "does", "did", "have", "has",
    "had", "will", "would", "can", "could", "should", "may", "might", "this",
    "that", "these", "those", "it", "its", "there", "here", "我们", "你们",
    "他们", "她们", "它们", "这个", "那个", "一个", "一些", "什么", "怎么",
}

_MODEL_CACHE: Dict[Tuple[int, int, int], Dict[str, Tuple[float, List[float]]]] = {}
_CACHE_LOCK = threading.Lock()
_RETRAIN_LOCK = threading.Lock()
_RETRAIN_RUNNING = {"flag": False}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", str(text or "")).lower()).strip()


def tokens(text: str) -> List[str]:
    """中文双字为主、单字兜底（只有单字成段时才发单字），英文/数字按词。"""
    raw = _clean(text)
    out: List[str] = []
    for run in _CJK.findall(raw):
        if len(run) == 1:
            if run not in _STOP:
                out.append(run)                 # 单字兜底
            continue
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg[0] in _STOP or bg[1] in _STOP:
                continue
            out.append(bg)
    for word in _WORD.findall(raw):
        if len(word) >= 2 and word not in _STOP:
            out.append(word)
    return out


def _model_id(meta: dict) -> str:
    return f"{meta['name']}:v{int(meta['version'])}"


def _split_passages(text: str) -> List[str]:
    """把长消息切成句子级片段，合并过短片段，避免单条消息占满一个文档。"""
    parts = [p.strip() for p in _SPLIT.split(_clean(text)) if p.strip()]
    out, buf = [], ""
    for part in parts:
        if len(buf) + len(part) <= SPLIT_CHARS:
            buf = (buf + part) if buf else part
        else:
            if buf:
                out.append(buf)
            buf = part
    if buf:
        out.append(buf)
    return [x for x in out if len(x) >= 6]


def _load_docs(source: str, split: bool = True) -> List[dict]:
    """训练/对比的语料。memory=活跃记忆；messages=本地真实对话；all=两者。"""
    docs: List[dict] = []
    src = str(source or "memory")
    if src in ("memory", "all"):
        for r in db.query("SELECT id, content, subject, object FROM chat_memory_items"
                          " WHERE status='active' ORDER BY id"):
            text = " ".join(str(r[k] or "") for k in ("content", "subject", "object"))
            if len(_clean(text)) >= 6:
                docs.append({"id": int(r["id"]), "text": text})
    if src in ("messages", "all"):
        for r in db.query("SELECT id, role, content FROM chat_messages ORDER BY id"):
            text = str(r["content"] or "")
            if not _clean(text):
                continue
            if split:
                for piece in _split_passages(text):
                    docs.append({"id": None, "text": piece,
                                 "label": f"msg{r['id']}:{piece[:18]}"})
            elif len(_clean(text)) >= 6:
                docs.append({"id": None, "text": text, "label": f"msg{r['id']}"})
    return docs


def _min_df(n_docs: int) -> int:
    """小语料保留单次出现的词项，语料足够大才过滤低频噪声。"""
    return 2 if n_docs >= 100 else 1


def _build(rows_text: Sequence[str], max_terms: int, n_docs: int):
    """返回 vocab / idf / 归一化后的稀疏 TF-IDF 行（index -> weight）。"""
    counts = [Counter(tokens(t)) for t in rows_text]
    df: Counter = Counter()
    for c in counts:
        df.update(c.keys())
    min_df = _min_df(n_docs)
    terms = [(t, d) for t, d in df.items() if d >= min_df]
    terms.sort(key=lambda kv: (-kv[1], kv[0]))
    terms = terms[:max_terms]
    vocab = {t: i for i, (t, _d) in enumerate(terms)}
    idf = [math.log((1 + n_docs) / (1 + df[t])) + 1.0 for t, _d in terms]
    sparse: List[Dict[int, float]] = []
    for c in counts:
        row: Dict[int, float] = {}
        for term, tf in c.items():
            idx = vocab.get(term)
            if idx is None:
                continue
            row[idx] = (1 + math.log(tf)) * idf[idx]
        norm = math.sqrt(sum(w * w for w in row.values())) or 1.0
        sparse.append({i: w / norm for i, w in row.items()})
    return vocab, idf, sparse, terms


def _svd(sparse: List[Dict[int, float]], n_terms: int, k: int,
         iters: int = SVD_ITERS) -> List[Tuple[float, List[float], List[float]]]:
    """幂迭代求前 k 个奇异三元组（s, u, v），每轮对残差做 deflation。"""
    rnd = random.Random(20261107)
    work = [dict(r) for r in sparse]
    comps: List[Tuple[float, List[float], List[float]]] = []
    v = [rnd.gauss(0.0, 1.0) for _ in range(n_terms)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    v = [x / norm for x in v]

    for _comp in range(k):
        y: List[float] = [0.0] * len(work)
        for it in range(iters):
            for i, row in enumerate(work):
                acc = 0.0
                for t, w in row.items():
                    acc += w * v[t]
                y[i] = acc
            u = [0.0] * n_terms
            for i, row in enumerate(work):
                yi = y[i]
                if yi == 0.0:
                    continue
                for t, w in row.items():
                    u[t] += w * yi
            nu = math.sqrt(sum(x * x for x in u))
            if nu < 1e-12:
                return comps
            new_v = [x / nu for x in u]
            delta = math.sqrt(sum((new_v[i] - v[i]) ** 2 for i in range(n_terms)))
            v = new_v
            if delta < SVD_TOL:
                break

        for i, row in enumerate(work):
            acc = 0.0
            for t, w in row.items():
                acc += w * v[t]
            y[i] = acc
        s = math.sqrt(sum(x * x for x in y))
        if s < 1e-9:
            return comps
        uvec = [x / s for x in y]
        comps.append((s, uvec, list(v)))

        # A := A - s·u·vᵀ
        for i, row in enumerate(work):
            sub = s * uvec[i]
            if sub == 0.0:
                continue
            for t in list(row.keys()):
                row[t] -= sub * v[t]
                if abs(row[t]) < 1e-10:
                    del row[t]
    return comps


def _terms_for(meta: dict) -> Dict[str, Tuple[float, List[float]]]:
    key = (int(meta["id"]), int(meta["version"]), int(meta["dim"]))
    with _CACHE_LOCK:
        hit = _MODEL_CACHE.get(key)
    if hit:
        return hit
    dim = int(meta["dim"])
    table: Dict[str, Tuple[float, List[float]]] = {}
    for row in db.local_embedding_terms(int(meta["id"])):
        blob = row.get("vec") or b""
        try:
            vec = list(struct.unpack(f"{dim}f", blob))
        except struct.error:
            continue
        table[str(row["term"])] = (float(row["idf"]), vec)
    with _CACHE_LOCK:
        _MODEL_CACHE.clear()
        _MODEL_CACHE[key] = table
    return table


def project(text: str, meta: dict | None = None) -> List[float]:
    """folding-in：把任意文本投影到已训练的主题空间（L2 归一化）。"""
    model = meta or db.local_embedding_model(DEFAULT_MODEL_NAME)
    if not model:
        return []
    dim = int(model["dim"] or 0)
    if dim <= 0:
        return []
    table = _terms_for(model)
    out = [0.0] * dim
    for term, tf in Counter(tokens(text)).items():
        hit = table.get(term)
        if not hit:
            continue
        idf, vec = hit
        weight = (1 + math.log(tf)) * idf
        for j in range(dim):
            out[j] += weight * vec[j]
    norm = math.sqrt(sum(x * x for x in out))
    if norm < 1e-9:
        return []                                   # 查询词项全部 OOV：交给调用方回落
    return [round(x / norm, 6) for x in out]


def active_model(name: str = DEFAULT_MODEL_NAME) -> dict | None:
    return db.local_embedding_model(name)


def model_id(meta: dict | None = None) -> str:
    meta = meta or active_model()
    return _model_id(meta) if meta else "hash"


def project_query(text: str, name: str = DEFAULT_MODEL_NAME) -> Tuple[List[float], str, bool]:
    """检索用：未训练 / 语料 < MIN_DOCS 时返回空向量并标记降级。"""
    meta = active_model(name)
    if not meta:
        return [], "hash", True
    if int(meta.get("corpus_count") or 0) < MIN_DOCS:
        return [], "hash", True
    vec = project(text, meta)
    if not vec:
        return [], "hash", True
    return vec, _model_id(meta), False


# ------------------------------------------------------------------ 训练
def train(dim: int = DEFAULT_DIM, min_docs: int = MIN_DOCS, force: bool = False,
          source: str = "memory", name: str = DEFAULT_MODEL_NAME,
          set_default: bool = False, max_terms: int = MAX_TERMS,
          iters: int = SVD_ITERS, split: bool = True) -> dict:
    db.init()
    started = time.time()
    docs = _load_docs(source, split=split)
    n_docs = len(docs)
    if n_docs < min_docs and not force:
        print(f"[local-embed] 语料不足：{n_docs} 条 < 阈值 {min_docs}，不训练"
              f"（可 --force 或等记忆积累；当前检索继续用哈希基线）")
        return {"ok": False, "reason": "insufficient", "corpus": n_docs}

    rows_text = [d["text"] for d in docs]
    vocab, idf, sparse, terms = _build(rows_text, max_terms, n_docs)
    k = min(int(dim), len(vocab), n_docs)
    if k < 2:
        print(f"[local-embed] 词项/语料过少（语料={n_docs} 词项={len(vocab)}），无法训练")
        return {"ok": False, "reason": "too_small", "corpus": n_docs}
    comps = _svd(sparse, len(vocab), k, iters=max(4, int(iters)))
    k_actual = len(comps)
    if k_actual < 2:
        print(f"[local-embed] SVD 秩不足（{k_actual}），无法训练")
        return {"ok": False, "reason": "rank", "corpus": n_docs}

    term_rows = []
    for idx, (term, df_count) in enumerate(terms):
        vals = [comps[j][2][idx] for j in range(k_actual)]
        term_rows.append((term, df_count, idf[idx],
                          struct.pack(f"{k_actual}f", *vals)))

    params = {"dim_request": int(dim), "dim_actual": k_actual,
              "max_terms": int(max_terms), "svd_iters": int(iters),
              "min_df": _min_df(n_docs),
              "ngram": "cjk:2(+1 fallback),en:word", "normalize": "l2"}
    model_id_int = db.create_local_embedding_model(
        name=name, dim=k_actual, corpus_count=n_docs,
        term_count=len(vocab), source=source, params=params,
        elapsed_ms=int((time.time() - started) * 1000))
    db.save_local_embedding_terms(model_id_int, term_rows)
    meta = db.local_embedding_model(name)
    mid = _model_id(meta)

    projected = 0
    if source in ("memory", "all"):
        for i, doc in enumerate(docs):
            if not doc.get("id"):
                continue
            vals = [comps[j][0] * comps[j][1][i] for j in range(k_actual)]
            if not any(vals):
                continue
            db.set_memory_vector(int(doc["id"]), vals, mid)
            projected += 1
    coverage = db.local_vector_coverage(mid)
    db.update_local_embedding_state(model_id_int, projected_count=coverage)
    if set_default:
        _set_default(name, k_actual)

    elapsed = time.time() - started
    print(f"[local-embed] 训练完成 model={mid} 语料={n_docs} 词项={len(vocab)}"
          f" 维度={k_actual} 耗时={elapsed:.2f}s 覆盖={coverage}条 source={source}")
    return {"ok": True, "model": mid, "corpus": n_docs, "terms": len(vocab),
            "dim": k_actual, "elapsed": round(elapsed, 2),
            "coverage": coverage, "model_id": model_id_int}


def _set_default(name: str, dim: int) -> None:
    """把本地模型设为默认检索空间；保留原有 OpenAI 配置字段。"""
    cfg = db.get_config("embeddingConfig:0") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.update({"provider": "local", "local_enabled": True,
                "local_model": name, "local_dim": int(dim)})
    db.set_config("embeddingConfig:0", cfg)
    print(f"[local-embed] 已设为默认：embeddingConfig:0 provider=local model={name}")


def backfill(limit: int = 0, name: str = DEFAULT_MODEL_NAME,
             batch: int = 64) -> dict:
    """给已有活跃记忆补当前本地模型向量（folding-in）。"""
    db.init()
    meta = active_model(name)
    if not meta:
        print(f"[local-embed] 模型 {name} 尚未训练，先执行 train")
        return {"ok": False, "reason": "untrained"}
    mid = _model_id(meta)
    started = time.time()
    sql = ("SELECT id, content, subject, object FROM chat_memory_items"
           " WHERE status='active' ORDER BY id DESC")
    args: tuple = ()
    if int(limit or 0) > 0:
        sql += " LIMIT ?"
        args = (int(limit),)
    rows = db.query(sql, args)
    done = 0
    for start in range(0, len(rows), max(1, int(batch))):
        for r in rows[start:start + batch]:
            text = " ".join(str(r[k] or "") for k in ("content", "subject", "object"))
            vec = project(text, meta)
            if vec:
                db.set_memory_vector(int(r["id"]), vec, mid)
                done += 1
    coverage = db.local_vector_coverage(mid)
    db.update_local_embedding_state(int(meta["id"]), projected_count=coverage)
    elapsed = time.time() - started
    print(f"[local-embed] 投影完成 model={mid} 条数={done} 维度={int(meta['dim'])}"
          f" 耗时={elapsed:.2f}s 覆盖={coverage}条")
    return {"ok": True, "model": mid, "count": done,
            "dim": int(meta["dim"]), "elapsed": round(elapsed, 2),
            "coverage": coverage}


# ------------------------------------------------------------- 自动重建
def maybe_retrain_async(name: str = DEFAULT_MODEL_NAME) -> bool:
    """语料增长 ≥20% 或新增 ≥N 条时，后台重建；不阻塞调用线程。"""
    try:
        corpus = int((db.one("SELECT COUNT(*) AS c FROM chat_memory_items"
                             " WHERE status='active'") or {}).get("c") or 0)
        if corpus < MIN_DOCS:
            return False
        meta = active_model(name)
        if meta:
            base = int(meta.get("corpus_count") or 0)
            grown = corpus >= max(base + 1, int(base * RETRAIN_GROWTH)) or \
                (corpus - base) >= RETRAIN_EVERY
            if not grown:
                return False
        with _RETRAIN_LOCK:
            if _RETRAIN_RUNNING["flag"]:
                return False
            _RETRAIN_RUNNING["flag"] = True

        def _job():
            try:
                train(source="memory", name=name, set_default=False, force=False,
                      iters=SVD_ITERS)
                backfill(name=name)
            except Exception as exc:                     # noqa: BLE001
                print(f"[local-embed] 后台重建失败：{exc}", file=sys.stderr)
            finally:
                _RETRAIN_RUNNING["flag"] = False

        threading.Thread(target=_job, daemon=True,
                         name="local-embed-retrain").start()
        print("[local-embed] 语料增长触发后台重建")
        return True
    except Exception as exc:                             # noqa: BLE001
        print(f"[local-embed] 重建检查失败：{exc}", file=sys.stderr)
        return False


# ------------------------------------------------------------------ 统计
def stats() -> None:
    db.init()
    corpus = int((db.one("SELECT COUNT(*) AS c FROM chat_memory_items"
                         " WHERE status='active'") or {}).get("c") or 0)
    cfg = db.get_config("embeddingConfig:0") or {}
    print(f"[local-embed] 当前语料：{corpus} 条活跃记忆；配置 provider="
          f"{cfg.get('provider') or '（未设）'} local_model="
          f"{cfg.get('local_model') or '（未设）'}")
    models = db.local_embedding_models(10)
    if not models:
        print("[local-embed] 还没有训练过任何本地模型")
        return
    for m in models:
        state = db.local_embedding_state(int(m["id"])) or {}
        coverage = db.local_vector_coverage(_model_id(m))
        flag = "ACTIVE" if int(m["active"]) else "old"
        print(f"[local-embed] {flag} {_model_id(m)} 维度={m['dim']}"
              f" 语料={m['corpus_count']} 词项={m['term_count']}"
              f" 耗时={int(m['elapsed_ms']) / 1000:.2f}s"
              f" 已投影={int(state.get('projected_count') or 0)}"
              f" 覆盖={coverage} 训练于={m['trained_at']} source={m['source']}")


# ------------------------------------------------------------------ 对比
def compare(queries: Sequence[str], source: str = "messages", top: int = 5,
            split: bool = True) -> None:
    from roleplay_core import memory_engine as me

    docs = _load_docs(source, split=split)
    meta = active_model()
    if not docs:
        print(f"[local-embed] 语料为空（source={source}），无法对比")
        return
    if not meta:
        print("[local-embed] 本地模型尚未训练，无法对比")
        return
    hash_vecs = [me.embed(d["text"], me.DEFAULT_DIM) for d in docs]
    lsa_vecs = [project(d["text"], meta) for d in docs]
    print(f"[local-embed] 对比语料={len(docs)} 维度 LSA={meta['dim']}"
          f" 哈希={me.DEFAULT_DIM} 查询数={len(queries)}")
    for q in queries:
        qh = me.embed(q, me.DEFAULT_DIM)
        ql = project(q, meta)
        if not ql:
            print(f"\nQ: {q}\n  LSA 查询无匹配词项（全部 OOV），本次跳过 LSA 排序；"
                  f"生产检索此时回落哈希")
            continue
        hs = [me.cosine(qh, v) for v in hash_vecs]
        ls = [me.cosine(ql, v) for v in lsa_vecs]
        h_rank = sorted(range(len(docs)), key=lambda i: -hs[i])[:top]
        l_rank = sorted(range(len(docs)), key=lambda i: -ls[i])[:top]
        h_set, l_set = set(h_rank), set(l_rank)
        print(f"\nQ: {q}")
        print(f"  top{top} 重合={len(h_set & l_set)}/{top}"
              f"  哈希独有={sorted(h_set - l_set)}  LSA独有={sorted(l_set - h_set)}")
        print("  #  hash(score) | lsa(score) | 文本")
        for pos in range(top):
            hi = h_rank[pos]
            li = l_rank[pos]
            text = _clean(docs[hi]["text"])[:34] if docs[hi]["text"] else ""
            print(f"  {pos + 1}. h#{hi}({hs[hi]:.3f}) | l#{li}({ls[li]:.3f})"
                  f" | {text}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local_embedder",
                                     description="本地 TF-IDF + LSA 语义向量")
    sub = parser.add_subparsers(dest="cmd")
    p_train = sub.add_parser("train", help="训练 / 重建本地模型")
    p_train.add_argument("--dim", type=int, default=DEFAULT_DIM)
    p_train.add_argument("--min-docs", type=int, default=MIN_DOCS)
    p_train.add_argument("--force", action="store_true",
                         help="语料低于阈值也训练（生产不建议）")
    p_train.add_argument("--source", choices=("memory", "messages", "all"),
                         default="memory")
    p_train.add_argument("--name", default=DEFAULT_MODEL_NAME)
    p_train.add_argument("--set-default", action="store_true")
    p_train.add_argument("--max-terms", type=int, default=MAX_TERMS)
    p_train.add_argument("--iters", type=int, default=SVD_ITERS)
    p_train.add_argument("--no-split", action="store_true")

    p_back = sub.add_parser("backfill", help="给已有记忆补本地向量")
    p_back.add_argument("--limit", type=int, default=0)
    p_back.add_argument("--name", default=DEFAULT_MODEL_NAME)

    sub.add_parser("stats", help="查看语料 / 模型 / 覆盖")

    p_cmp = sub.add_parser("compare", help="同一批查询下哈希 vs LSA 的召回差异")
    p_cmp.add_argument("--source", choices=("memory", "messages", "all"),
                       default="messages")
    p_cmp.add_argument("--top", type=int, default=5)
    p_cmp.add_argument("--no-split", action="store_true")
    p_cmp.add_argument("--queries", nargs="*", default=[])

    args = parser.parse_args(argv)
    cmd = args.cmd or "stats"
    if cmd == "train":
        train(dim=args.dim, min_docs=args.min_docs, force=args.force,
              source=args.source, name=args.name, set_default=args.set_default,
              max_terms=args.max_terms, iters=args.iters,
              split=not args.no_split)
    elif cmd == "backfill":
        backfill(limit=args.limit, name=args.name)
    elif cmd == "stats":
        stats()
    elif cmd == "compare":
        compare(args.queries, source=args.source, top=args.top,
                split=not args.no_split)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
