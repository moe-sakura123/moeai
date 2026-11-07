"""算法驱动的角色记忆引擎（纯标准库基线，零联网、零安装）。

分层：
  写入  规则抽取兜底 + LLM JSON 抽取解析
  表示  特征哈希嵌入（feature hashing）→ 384 维单位向量
  检索  词法 BM25（优先 FTS5，失败回落纯 Python）+ 余弦相似度
        混合分 = w_rel·z(相关性) + w_imp·z(重要性) + w_rec·时近性
  整合  同 kind 余弦单链接聚类，组内合并（merged_into 指向归档旧条目）
  遗忘  archive 降权，不物理删除

没有 numpy / 没有外部 embedding / 没有 FTS5 / 没有 LLM 时，各层都有
可预期的降级路径，不会抛异常打断对话。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Tuple

DEFAULT_DIM = 384
KINDS = ("fact", "event", "relation", "promise", "preference")

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-z0-9_]+", re.I)
_SENT = re.compile(r"[。！？!?\n]+")


# ---------------------------------------------------------------- 表示层
def tokens(text: str, dim: int = DEFAULT_DIM) -> List[str]:
    """词 + 单字 + 双字（中文），用于特征哈希；结果确定、可复现。"""
    raw = str(text or "").lower()
    words = _WORD.findall(raw)
    chars, bigrams = [], []
    for run in _CJK.findall(raw):
        chars.extend(run)
        bigrams.extend(run[i:i + 2] for i in range(len(run) - 1))
    return words + chars + bigrams


def embed(text: str, dim: int = DEFAULT_DIM) -> List[float]:
    """特征哈希嵌入：token → 伪随机桶 + 符号，L2 归一化。"""
    dim = max(32, int(dim or DEFAULT_DIM))
    vec = [0.0] * dim
    for tok in tokens(text, dim):
        h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "big") % dim
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 1e-9:
        vec = [x / norm for x in vec]
    return [round(x, 6) for x in vec]


def cosine(a: Iterable[float], b: Iterable[float]) -> float:
    """纯 Python 余弦；任一为空返回 0（降级安全）。"""
    va, vb = list(a or []), list(b or [])
    if not va or not vb:
        return 0.0
    n = min(len(va), len(vb))
    dot = sum(float(va[i]) * float(vb[i]) for i in range(n))
    return round(dot, 6)          # 已归一化，dot 即 cosine


def _unit(vec) -> List[float]:
    vals = [float(x) for x in (vec or [])]
    norm = math.sqrt(sum(x * x for x in vals)) or 1.0
    return [x / norm for x in vals]


# 查询向量进程内缓存（模型 + 内容 hash），降低每轮 embedding 成本。
_Q_CACHE: Dict[Tuple[str, str], List[float]] = {}
_Q_CACHE_MAX = 256


def _emb_provider(cfg: dict) -> dict:
    eb = cfg.get("embedding") or {}
    if not isinstance(eb, dict):
        return {}
    if not eb.get("enabled"):
        return {}
    if not str(eb.get("base_url") or "").strip():
        return {}
    if not str(eb.get("model") or "").strip():
        return {}
    return eb


def embedding_model_id(cfg: dict) -> str:
    """当前检索空间的模型标识；没有可用 provider 时就是 hash 基线。"""
    eb = _emb_provider(cfg)
    if not eb:
        return "hash"
    model = str(eb.get("model") or "").strip()
    dims = int(eb.get("dimensions") or 0)
    return f"openai:{model}" + (f"@{dims}" if dims > 0 else "")


def provider_embeddings(texts, cfg: dict) -> Tuple[str, List[List[float]], bool]:
    """批量走 OpenAI 兼容 /v1/embeddings；失败回落哈希。

    返回 (模型标识, 向量列表, 是否降级)。调用方负责落库。
    """
    texts = [str(t or "") for t in (texts or [])]
    eb = _emb_provider(cfg)
    if eb:
        try:
            import llm
            vecs = llm.embeddings(
                eb["base_url"], str(eb.get("api_key") or ""),
                str(eb["model"]), texts,
                dimensions=int(eb.get("dimensions") or 0),
                timeout=float(eb.get("timeout") or 30.0),
                batch_size=int(eb.get("batch_size") or eb.get("batchSize") or 16),
                retries=int(eb.get("max_retries") or eb.get("maxRetries") or 2))
            if len(vecs) == len(texts) and all(vecs):
                return embedding_model_id(cfg), [_unit(v) for v in vecs], False
        except Exception as exc:                     # noqa: BLE001
            print(f"[memory] embedding 调用失败，回落哈希：{exc}", file=sys.stderr)
    dim = int(cfg.get("embedDim", DEFAULT_DIM) or DEFAULT_DIM)
    return "hash", [embed(t, dim) for t in texts], True


def query_vector(query: str, cfg: dict) -> Tuple[str, List[float], bool]:
    """检索查询向量：短超时；超时/报错立刻回落哈希，不卡主流程。"""
    text = str(query or "")
    limit = min(float(_emb_provider(cfg).get("timeout") or 30.0),
                float(cfg.get("queryTimeout", 3.0) or 3.0))
    model = embedding_model_id(cfg)
    key = (model, content_hash(text))
    hit = _Q_CACHE.get(key)
    if hit:
        return model, hit, model == "hash"
    eb = _emb_provider(cfg)
    if eb:
        try:
            import llm
            vecs = llm.embeddings(
                eb["base_url"], str(eb.get("api_key") or ""),
                str(eb["model"]), [text],
                dimensions=int(eb.get("dimensions") or 0),
                timeout=max(1.0, limit), batch_size=1, retries=0)
            if vecs:
                vec = _unit(vecs[0])
                if len(_Q_CACHE) >= _Q_CACHE_MAX:
                    _Q_CACHE.clear()
                _Q_CACHE[key] = vec
                return model, vec, False
        except Exception as exc:                     # noqa: BLE001
            print(f"[memory] 查询向量回落哈希：{exc}", file=sys.stderr)
    dim = int(cfg.get("embedDim", DEFAULT_DIM) or DEFAULT_DIM)
    vec = embed(text, dim)
    if len(_Q_CACHE) >= _Q_CACHE_MAX:
        _Q_CACHE.clear()
    _Q_CACHE[("hash", content_hash(text))] = vec
    return "hash", vec, True


def mmr_rerank(items: List[dict], lambda_: float = 0.7, k: int = 8) -> List[dict]:
    """MMR 多样性重排：λ·相关性 − (1−λ)·与已选项的最大余弦相似。"""
    if not items:
        return []
    lam = min(1.0, max(0.0, float(lambda_)))
    pool = list(items)
    out: List[dict] = []
    while pool and len(out) < max(1, int(k or 8)):
        best, best_val = None, None
        for it in pool:
            rel = float(it.get("score") or 0.0)
            if out:
                div = max(cosine(it.get("vec") or [], s.get("vec") or [])
                          for s in out)
            else:
                div = 0.0
            val = lam * rel - (1 - lam) * div
            if best_val is None or val > best_val:
                best, best_val = it, val
        pool.remove(best)
        out.append(best)
    return out


def content_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", str(text or "").strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 检索层
def bm25(query: str, docs: List[Tuple[int, str]], k1: float = 1.5,
         b: float = 0.75) -> Dict[int, float]:
    """纯 Python BM25 兜底。docs = [(id, text)]，返回 {id: score}。"""
    q_terms = tokens(query)
    if not q_terms or not docs:
        return {}
    dl = []
    freq: Dict[int, Dict[str, int]] = {}
    for mid, text in docs:
        ts = tokens(text)
        dl.append(len(ts))
        d: Dict[str, int] = {}
        for t in ts:
            d[t] = d.get(t, 0) + 1
        freq[mid] = d
    avg = sum(dl) / max(1, len(dl))
    score: Dict[int, float] = {}
    for mid, _text in docs:
        d = freq[mid]
        for term in q_terms:
            tf = d.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (len(docs) - 1 + 0.5) / (0.5))
            denom = tf + k1 * (1 - b + b * (len(d) or 1) / max(1.0, avg))
            score[mid] = score.get(mid, 0.0) + idf * tf * (k1 + 1) / max(1e-6, denom)
    return score


def _minmax(values: List[float]) -> List[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [1.0 if hi > 0 else 0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _age_days(iso: str, now: datetime | None = None) -> float:
    try:
        ts = datetime.fromisoformat(str(iso or "").replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        try:
            ts = datetime.strptime(str(iso), "%Y-%m-%d %H:%M:%S")
            ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return 0.0
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds() / 86400.0)


def recency(iso: str, half_life_days: float = 30.0,
            now: datetime | None = None) -> float:
    """时近性：exp(-ln2 · age / half_life)，半衰期可配。"""
    hl = max(0.1, float(half_life_days or 30.0))
    return round(math.exp(-math.log(2) * _age_days(iso, now) / hl), 6)


def rank_items(candidates: List[dict], cfg: dict,
               now: datetime | None = None) -> List[dict]:
    """归一化加权排序。candidates 至少含 bm25 / cosine / importance / created_at。"""
    if not candidates:
        return []
    weights = cfg.get("weights") or {}
    rel_w = cfg.get("relevance") or {}
    w_rel = float(weights.get("relevance", 0.6))
    w_imp = float(weights.get("importance", 0.25))
    w_rec = float(weights.get("recency", 0.15))
    w_bm = float(rel_w.get("bm25", 0.6))
    w_cos = float(rel_w.get("cosine", 0.4))
    half_life = float(cfg.get("halfLifeDays", 30.0))

    bm = _minmax([float(c.get("bm25") or 0.0) for c in candidates])
    cos = _minmax([float(c.get("cosine") or 0.0) for c in candidates])
    imp = _minmax([float(c.get("importance") or 0.0) for c in candidates])
    rec = [recency(c.get("created_at"), half_life, now) for c in candidates]

    out = []
    for i, c in enumerate(candidates):
        rel = w_bm * bm[i] + w_cos * cos[i]
        total = w_rel * rel + w_imp * imp[i] + w_rec * rec[i]
        item = dict(c)
        item["scores"] = {
            "relevance": round(rel, 4),
            "bm25": round(float(c.get("bm25") or 0.0), 4),
            "cosine": round(float(c.get("cosine") or 0.0), 4),
            "importance": round(imp[i], 4),
            "recency": round(rec[i], 4),
            "total": round(total, 4),
        }
        item["score"] = round(total, 4)
        out.append(item)
    return out


def retrieve(uid: int | None, cid: str, sid: str, query: str, cfg: dict) -> List[dict]:
    """混合检索入口；FTS5 / 纯 BM25 / 余弦都有降级路径。"""
    import db
    if not uid or not sid:
        return []
    limit = int(cfg.get("capacity", 300) or 300)
    items = db.memory_items(uid, cid, sid, status="active", limit=limit)
    if not items:
        return []
    model_id, qvec, _degraded = query_vector(query, cfg)
    fts = db.search_memory_fts(uid, cid, sid, query, limit=limit)
    if not fts:
        fts = bm25(query, [(int(it["id"]), " ".join(
            [str(it.get("content") or ""), str(it.get("subject") or ""),
             str(it.get("object") or "")])) for it in items])
    cand = []
    for it in items:
        vecs = it.get("vecs") or {}
        vec = vecs.get(model_id) or (it.get("vec") if model_id == "hash" else [])
        cand.append({
            **it,
            "bm25": float(fts.get(int(it["id"]), 0.0)),
            "cosine": cosine(qvec, vec or []),
            "embed_model": model_id,
        })
    scored = rank_items(cand, cfg)
    scored = [x for x in scored if x["score"] >= float(cfg.get("minScore", 0.25))]
    scored.sort(key=lambda x: -x["score"])
    top_k = max(1, int(cfg.get("topK", 8) or 8))
    if cfg.get("mmr", True):
        return mmr_rerank(scored, float(cfg.get("mmrLambda", 0.7) or 0.7), top_k)
    return scored[:top_k]


# ---------------------------------------------------------------- 写入层
_RULES = [
    ("promise", re.compile(r"(答应|约好|说好|承诺|下次|以后一定|保证)")),
    ("preference", re.compile(r"(我(?:很|最|特别)?(?:喜欢|爱|讨厌|不喜欢|害怕|怕))")),
    ("relation", re.compile(r"(你的|我的|他是|她是|叫我|是我的|是你的)")),
    ("event", re.compile(r"(今天|昨天|刚才|上次|这次|明天|周末|以后|曾经|当时|刚刚)")),
    ("fact", re.compile(r"(我是|我在|我有|我叫|我住|我在做|我一直)")),
]
_IMPORTANCE = {"promise": 0.8, "relation": 0.75, "preference": 0.7,
               "event": 0.55, "fact": 0.6}


def extract_rules(user_text: str, ai_text: str = "") -> List[dict]:
    """规则兜底抽取：按句切分 → 5 类命中 → 去重。"""
    out, seen = [], set()
    for source in (str(user_text or ""), str(ai_text or "")):
        for sent in _SENT.split(source):
            text = sent.strip()
            if len(text) < 4 or len(text) > 120:
                continue
            kind = ""
            for k, pat in _RULES:
                if pat.search(text):
                    kind = k
                    break
            if not kind:
                continue
            h = content_hash(text)
            if h in seen:
                continue
            seen.add(h)
            out.append({"kind": kind, "content": text,
                        "importance": _IMPORTANCE[kind], "confidence": 0.7,
                        "subject": "", "object": ""})
    return out[:12]


EXTRACT_PROMPT = (
    "从下面这轮角色扮演对话中抽取值得长期记住的记忆。只输出 JSON 数组，"
    "每项字段：kind(fact|event|relation|promise|preference)、content(一句中文事实)、"
    "subject(主体，可空)、object(客体/值，可空)、importance(0~1)。"
    "只记录稳定信息，不复述寒暄。最多 6 条。\n\n"
)


def parse_extract(text: str) -> List[dict]:
    """解析 LLM 输出的 JSON 数组；失败返回空（调用方回落规则抽取）。"""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    if "[" not in raw or "]" not in raw:
        return []
    try:
        arr = json.loads(raw[raw.find("["):raw.rfind("]") + 1])
    except (ValueError, TypeError):
        return []
    out = []
    for obj in arr if isinstance(arr, list) else []:
        if not isinstance(obj, dict):
            continue
        kind = str(obj.get("kind") or "").strip().lower()
        content = str(obj.get("content") or "").strip()
        if kind not in KINDS or len(content) < 2:
            continue
        try:
            imp = float(obj.get("importance") or _IMPORTANCE.get(kind, 0.6))
        except (ValueError, TypeError):
            imp = _IMPORTANCE.get(kind, 0.6)
        out.append({
            "kind": kind, "content": content[:160],
            "subject": str(obj.get("subject") or "").strip()[:40],
            "object": str(obj.get("object") or "").strip()[:80],
            "importance": min(1.0, max(0.0, imp)),
            "confidence": 0.85,
        })
    return out[:6]


def classify_duplicate(existing: List[dict], item: dict, dim: int,
                       threshold: float = 0.92) -> Tuple[str, int]:
    """语义近似去重 / 冲突替换。

    返回 (action, target_id)：add / merge / replace。
    merge = 近似重复（余弦 ≥ 阈值）；replace = 同 kind+subject 但内容不同。
    """
    nv = item.get("vec") or embed(str(item.get("content") or ""), dim)
    best, best_sim = None, -1.0
    for old in existing:
        if str(old.get("status") or "") != "active":
            continue
        ov = old.get("vec") or []
        if not ov:
            ov = embed(str(old.get("content") or ""), dim)
        sim = cosine(nv, ov)
        if sim > best_sim:
            best, best_sim = old, sim
    if best is not None and best_sim >= threshold:
        return "merge", int(best["id"])
    subject = str(item.get("subject") or "").strip()
    if subject:
        for old in existing:
            if (str(old.get("kind") or "") == str(item.get("kind") or "")
                    and str(old.get("subject") or "").strip() == subject):
                return "replace", int(old["id"])
    return "add", 0


def cluster_items(items: List[dict], threshold: float = 0.85) -> List[List[dict]]:
    """同 kind 余弦单链接聚类（并查集）；用于容量触顶时按主题归并。"""
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    vecs = []
    for it in items:
        v = it.get("vec") or embed(str(it.get("content") or ""))
        vecs.append(v)
    for i in range(n):
        for j in range(i + 1, n):
            if str(items[i].get("kind") or "") != str(items[j].get("kind") or ""):
                continue
            if cosine(vecs[i], vecs[j]) >= threshold:
                union(i, j)
    groups: Dict[int, List[dict]] = {}
    for i, it in enumerate(items):
        groups.setdefault(find(i), []).append(it)
    return [g for g in groups.values() if len(g) > 1]


def union_content(items: List[dict], max_chars: int = 600) -> str:
    """组内归并文本：保留高 importance / 新条目，按句拼接去重。"""
    ranked = sorted(items, key=lambda x: str(x.get("created_at") or ""), reverse=True)
    ranked.sort(key=lambda x: -float(x.get("importance") or 0))
    seen, lines = set(), []
    for it in ranked:
        line = str(it.get("content") or "").strip()
        h = content_hash(line)
        if not line or h in seen:
            continue
        seen.add(h)
        lines.append(line)
    text = "；".join(lines)
    return text[:max_chars]
