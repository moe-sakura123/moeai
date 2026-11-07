#!/usr/bin/env python3
"""memory_recall.py — 稳定的记忆检索入口（给后续 OpenAI 类模型的记忆注入用）。

链路：
    BM25/FTS5 关键词通道  ┐
                          ├→ RRF(k=60) 融合去重 → bge-reranker 精排 → MMR → top-k
    bge-m3 稠密通道       ┘

两条通道各取 20 条；融合后前 20 条交给 reranker；MMR(λ=0.7) 做最终多样性重排。
返回结构保留 `rrf / rerank / mmr / final` 四个分数与两通道原始排名，便于解释。

降级链：bge-m3 → local-lsa → hash。模型未就绪、加载失败、推理报错、单条超时、
总时限超时，都只打 `[recall]` 日志并回落关键词结果，绝不抛异常。

模型懒加载单例（首次调用触发后台加载，最多等 2s，之后继续用关键词通道）；
路径可用环境变量 BGE_M3_PATH / BGE_RERANK_PATH 或 embeddingConfig:0 里的
bge_model_path / bge_rerank_path 覆盖。

CLI：
    python -m roleplay_core.memory_recall recall --uid 1 --cid 1 --q "窗外的雨" --top-k 5
    python -m roleplay_core.memory_recall bench --q "窗外的雨" --repeat 3
    python -m roleplay_core.memory_recall compare --q "窗外的雨" --q "泛黄的书页"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DENSE_LIMIT = 20
FTS_LIMIT = 20
RRF_K = 60
RERANK_TOP = 20
DEFAULT_BUDGET = 3.0
DEFAULT_SINGLE_TIMEOUT = 2.0
DEFAULT_MMR_LAMBDA = 0.7
MAX_QUERY_CHARS = 4000          # 超长查询截断，避免 tokenizer 空转
RERANK_PAIR_CHARS = 60          # 交叉编码器单条候选截断（CPU 上按 token 线性变慢）
RERANK_QUERY_CHARS = 120        # 精排 query 只留最近一段（否则历史拼接越长、每对越慢）
RERANK_COST = 0.18              # 单对 (query, doc) 的保守耗时估计（秒）
BGE_MODEL_NAME = "bge-m3:v1"
MAX_SEGMENT_CHARS = 300         # 历史分段编码截断：控制单段 token，跨轮缓存更易复用
SEGMENT_WEIGHT_LAST = 2.0       # 当前用户消息在分段查询向量里的权重

_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 模型单例
_LOAD_LOCK = threading.Lock()
_EMB_LOCK = threading.Lock()
_RERANK_LOCK = threading.Lock()
_Q_LOCK = threading.Lock()
_Q_CACHE: Dict[Tuple[str, str], List[float]] = {}
_Q_ORDER: List[Tuple[str, str]] = []
_Q_CACHE_MAX = 256
# 历史分段向量缓存：key = 内容 sha1，跨轮/跨会话复用已编码的消息
_SEG_LOCK = threading.Lock()
_SEG_CACHE: Dict[str, List[float]] = {}
_SEG_ORDER: List[str] = []
_SEG_MAX = 256
_BACKFILL = {"running": False, "done": False, "stored": 0, "total": 0}
_EMB = None
_EMB_ERR = ""
_EMB_LOADING = False
_EMB_READY = threading.Event()
_RERANK = None
_RERANK_ERR = ""
_RERANK_LOADING = False
_RERANK_READY = threading.Event()
_LOAD_STARTED = False
_LOGGED: Dict[str, float] = {}


def _log(msg: str, *, once: str = "") -> None:
    """打日志；once 给定时同 key 60 秒内只打一次，避免每轮刷屏。"""
    if once:
        now = time.time()
        if now - _LOGGED.get(once, 0.0) < 60:
            return
        _LOGGED[once] = now
    print(f"[recall] {msg}", file=sys.stderr, flush=True)


def _config() -> dict:
    try:
        import db
        cfg = db.get_config("embeddingConfig:0") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:        # noqa: BLE001
        return {}


def _paths(overrides: dict | None = None) -> Tuple[Path, Path]:
    overrides = overrides or {}
    cfg = _config()
    embed = (overrides.get("embed_path") or os.getenv("BGE_M3_PATH")
             or cfg.get("bge_model_path") or "models/bge-m3")
    rerank = (overrides.get("rerank_path") or os.getenv("BGE_RERANK_PATH")
              or cfg.get("bge_rerank_path") or "models/bge-reranker-v2-m3")
    return (Path(str(embed)) if Path(str(embed)).is_absolute()
            else _ROOT / str(embed),
            Path(str(rerank)) if Path(str(rerank)).is_absolute()
            else _ROOT / str(rerank))


def _apply_thread_limit(torch) -> int:
    """CPU 推理限核：torch 默认吃满所有逻辑核，召回和后台任务会互相抢核。

    实测（6 物理核 / 12 逻辑核、连跑 5 轮）精排 20 条耗时：
      12 线程 3.8–4.5s（p95 5.03s）；6 线程 4.1–4.2s（p95 4.66s）；
      3 线程 6.3s（p95 6.94s，太少了反而慢）。
    默认取「物理核数」（按 os.cpu_count() 的 SMT 折半估算，至少 2），
    可用 BGE_INFER_THREADS 覆盖。返回实际生效的线程数。
    """
    raw = os.getenv("BGE_INFER_THREADS", "").strip()
    if raw:
        n = max(1, int(raw))
    else:
        logical = os.cpu_count() or 4
        n = max(2, logical // 2)
    torch.set_num_threads(n)
    _log(f"CPU 推理线程数={n}（逻辑核={os.cpu_count()}，避免与后台任务抢核）")
    return n


def _load_models(embed_path: Path, rerank_path: Path) -> None:
    """后台线程：先加载 embedding，再加载 reranker；失败只记状态。"""
    global _EMB, _EMB_ERR, _EMB_LOADING, _RERANK, _RERANK_ERR, _RERANK_LOADING
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    t0 = time.time()
    try:
        import torch
        _apply_thread_limit(torch)
        from sentence_transformers import SentenceTransformer
        if not embed_path.exists():
            raise FileNotFoundError(f"模型目录不存在：{embed_path}")
        # CPU 上 fp16 是软件模拟，比 fp32 慢数倍；强制 fp32
        _EMB = SentenceTransformer(
            str(embed_path), device="cpu",
            model_kwargs={"dtype": torch.float32})
        getter = getattr(_EMB, "get_embedding_dimension",
                         _EMB.get_sentence_embedding_dimension)
        dim = int(getter() or 0)
        _log(f"bge-m3 加载完成 path={embed_path} 维度={dim}"
             f" 耗时={time.time() - t0:.2f}s")
        _EMB_READY.set()
        threading.Thread(target=_backfill_vectors, daemon=True).start()
    except Exception as exc:         # noqa: BLE001
        _EMB_ERR = f"{type(exc).__name__}: {exc}"
        _log(f"bge-m3 加载失败（{_EMB_ERR}），稠密通道降级")
        _EMB_READY.set()
    finally:
        _EMB_LOADING = False

    t1 = time.time()
    try:
        import torch
        from sentence_transformers import CrossEncoder
        if not rerank_path.exists():
            raise FileNotFoundError(f"模型目录不存在：{rerank_path}")
        _RERANK = CrossEncoder(
            str(rerank_path), device="cpu", max_length=160,
            model_kwargs={"dtype": torch.float32})
        _log(f"bge-reranker 加载完成 path={rerank_path}"
             f" 耗时={time.time() - t1:.2f}s")
        _RERANK_READY.set()
    except Exception as exc:         # noqa: BLE001
        _RERANK_ERR = f"{type(exc).__name__}: {exc}"
        _log(f"bge-reranker 加载失败（{_RERANK_ERR}），跳过精排")
        _RERANK_READY.set()
    finally:
        _RERANK_LOADING = False


def _backfill_vectors() -> None:
    """后台把现有记忆补成 bge-m3 向量；不阻塞召回，失败只记日志。"""
    if _BACKFILL["running"] or _EMB is None:
        return
    _BACKFILL.update({"running": True, "done": False, "stored": 0, "total": 0})
    t0 = time.time()
    try:
        import db
        scopes = db.query(
            "SELECT DISTINCT user_id, character_id, session_id"
            " FROM chat_memory_items WHERE status='active'")
        for s in scopes:
            todo = db.items_missing_vector(
                int(s["user_id"] or 0), str(s["character_id"] or ""),
                str(s["session_id"] or ""), BGE_MODEL_NAME, limit=400)
            _BACKFILL["total"] += len(todo)
            for start in range(0, len(todo), 8):
                batch = todo[start:start + 8]
                got = None
                for _ in range(3):      # 与查询编码抢锁时短暂重试，别直接终止回填
                    got = encode([_item_text(it) for it in batch], batch_size=8,
                                 timeout=60.0, wait=1.0)
                    if got or _EMB is None:
                        break
                if not got:
                    if _EMB is None:
                        break
                    continue
                for it, vec in zip(batch, got):
                    try:
                        db.set_memory_vector(int(it["id"]), vec, BGE_MODEL_NAME)
                        _BACKFILL["stored"] += 1
                    except Exception:        # noqa: BLE001
                        pass
    except Exception as exc:         # noqa: BLE001
        _log(f"向量回填失败（{type(exc).__name__}: {exc}）", once="backfill-fail")
    finally:
        _BACKFILL.update({"running": False, "done": True})
        _log(f"向量回填完成 条数={_BACKFILL['stored']}/{_BACKFILL['total']}"
             f" 耗时={time.time() - t0:.2f}s")


def warmup(wait: float = 2.0, overrides: dict | None = None) -> dict:
    """启动/首次调用时预热；最多等 wait 秒，超时继续后台加载。"""
    global _LOAD_STARTED, _EMB_LOADING, _RERANK_LOADING
    with _LOAD_LOCK:
        if not _LOAD_STARTED:
            _LOAD_STARTED = True
            _EMB_LOADING = _RERANK_LOADING = True
            embed_path, rerank_path = _paths(overrides)
            threading.Thread(target=_load_models,
                             args=(embed_path, rerank_path), daemon=True).start()
    if wait > 0:
        end = time.monotonic() + max(0.0, float(wait))
        _EMB_READY.wait(max(0.0, end - time.monotonic()))
        if _RERANK_LOADING:
            _RERANK_READY.wait(max(0.0, end - time.monotonic()))
    return status()


def status() -> dict:
    return {
        "embed_ready": _EMB is not None,
        "embed_error": _EMB_ERR,
        "embed_loading": _EMB_LOADING,
        "rerank_ready": _RERANK is not None,
        "rerank_error": _RERANK_ERR,
        "rerank_loading": _RERANK_LOADING,
        "backfill_running": _BACKFILL["running"],
        "backfill_done": _BACKFILL["done"],
        "backfill_stored": _BACKFILL["stored"],
    }


def _reset() -> None:
    """测试用：回到未加载状态（不会自动触发重载）。"""
    global _EMB, _RERANK, _EMB_ERR, _RERANK_ERR, _LOAD_STARTED
    global _EMB_LOADING, _RERANK_LOADING
    _EMB = _RERANK = None
    _EMB_ERR = _RERANK_ERR = ""
    _EMB_LOADING = _RERANK_LOADING = False
    _LOAD_STARTED = False
    _EMB_READY.clear()
    _RERANK_READY.clear()
    with _Q_LOCK:
        _Q_CACHE.clear()
        _Q_ORDER.clear()
    with _SEG_LOCK:
        _SEG_CACHE.clear()
        _SEG_ORDER.clear()


def _cache_get(key: Tuple[str, str]) -> Optional[List[float]]:
    with _Q_LOCK:
        vec = _Q_CACHE.get(key)
        return list(vec) if vec else None


def _cache_put(key: Tuple[str, str], vec: Sequence[float]) -> None:
    with _Q_LOCK:
        if key not in _Q_CACHE:
            _Q_ORDER.append(key)
        _Q_CACHE[key] = list(vec)
        while len(_Q_ORDER) > _Q_CACHE_MAX:
            old = _Q_ORDER.pop(0)
            _Q_CACHE.pop(old, None)


def _infer(fn, timeout: float, lock: threading.Lock, wait: float = 0.0,
           hard: Optional[float] = None):
    """在后台线程跑一次推理；超时返回 None（线程跑完自行释放锁）。

    wait>0 时先最多等 wait 秒拿锁（用于查询编码插队到回填批次之间），
    等待期间仍受调用方总时限约束。
    hard>timeout 时：超过单块上限后不丢弃这次计算，继续等同一个后台线程
    到总时限（机器被后台任务抢占时避免「超时即重算」的重复浪费）。
    """
    budget = max(0.05, float(timeout))
    if wait > 0:
        got_lock = lock.acquire(timeout=min(0.5, float(wait)))
    else:
        got_lock = lock.acquire(blocking=False)
    if not got_lock:
        _log("上一次推理仍未结束，跳过本通道", once="infer-busy")
        return None
    box: list = []
    done = threading.Event()

    def run():
        try:
            box.append(fn())
        except Exception as exc:     # noqa: BLE001
            box.append(exc)
        finally:
            lock.release()
            done.set()

    threading.Thread(target=run, daemon=True).start()
    if not done.wait(budget):
        # 软超时：计算还在后台跑，若总时限没到就继续等这一份结果，不重算
        if hard is not None and hard - time.monotonic() > 0.02:
            _log(f"推理超过单块上限 {budget:.1f}s，继续等到总时限",
                 once="infer-soft")
            done.wait(max(0.0, hard - time.monotonic()))
        if not done.is_set():
            _log(f"单条推理超时（>{budget:.1f}s），本通道降级", once="infer-timeout")
            return None
    if not box:
        return None
    got = box[0]
    if isinstance(got, Exception):
        _log(f"推理失败（{type(got).__name__}: {got}），本通道降级",
             once="infer-fail")
        return None
    return got


def _truncate(text: str, max_chars: int = MAX_QUERY_CHARS) -> str:
    text = str(text or "").strip()
    return text[:max_chars]


# ---------------------------------------------------------------- 编码/精排
def encode(texts: Sequence[str], batch_size: int = 16, timeout: float = 8.0,
           wait: float = 0.0) -> Optional[List[List[float]]]:
    """批量编码；模型未就绪/失败/超时返回 None（调用方降级）。"""
    if _EMB is None:
        return None
    texts = [str(t or "") for t in texts]
    if not texts:
        return []

    def run():
        arr = _EMB.encode(texts, batch_size=max(1, int(batch_size)),
                          normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, v)) for v in arr]

    return _infer(run, timeout, _EMB_LOCK, wait=wait)


def encode_query(text: str, timeout: float = DEFAULT_SINGLE_TIMEOUT) -> Optional[List[float]]:
    """查询编码（进程内缓存，短文本命中直接返回）。"""
    q = str(text or "")
    key = (BGE_MODEL_NAME, q)
    cached = _cache_get(key)
    if cached:
        return cached
    vecs = encode([q], batch_size=1, timeout=timeout, wait=0.45)
    if vecs:
        _cache_put(key, vecs[0])
        return vecs[0]
    return None


def _seg_key(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()


def _seg_get(text: str) -> Optional[List[float]]:
    with _SEG_LOCK:
        vec = _SEG_CACHE.get(_seg_key(text))
        return list(vec) if vec else None


def _seg_put(text: str, vec: Sequence[float]) -> None:
    key = _seg_key(text)
    with _SEG_LOCK:
        if key not in _SEG_CACHE:
            _SEG_ORDER.append(key)
        _SEG_CACHE[key] = list(vec)
        while len(_SEG_ORDER) > _SEG_MAX:
            old = _SEG_ORDER.pop(0)
            _SEG_CACHE.pop(old, None)


def segment_vector(segments: Sequence[str],
                   weights: Optional[Sequence[float]] = None,
                   timeout: float = DEFAULT_SINGLE_TIMEOUT,
                   deadline: Optional[float] = None) -> Optional[Tuple[str, List[float]]]:
    """把「最近几轮消息 + 本轮输入」各段向量加权平均成查询向量。

    段向量按内容 sha1 缓存，跨轮复用：本轮只编码缓存里没有的新消息，
    不再把整条拼接 query 重新编码。返回 (模型, 归一化向量)；失败返回 None。
    """
    if _EMB is None:
        return None
    segs = [str(s or "").strip()[:MAX_SEGMENT_CHARS] for s in (segments or [])]
    ws = list(weights or [])
    pairs = [(s, float(ws[i]) if i < len(ws) else 1.0)
             for i, s in enumerate(segs) if s]
    if not pairs:
        return None
    miss = [s for s, _ in pairs if _seg_get(s) is None]
    if miss:
        per = max(0.2, float(timeout))
        if deadline is not None:
            per = min(per, deadline - time.monotonic())
            if per <= 0.15:
                return None
        vecs = encode(miss, batch_size=len(miss), timeout=per, wait=0.45)
        if not vecs or len(vecs) != len(miss):
            _log("历史分段编码失败，查询向量回落整串编码", once="seg-encode-fail")
            return None
        for text, vec in zip(miss, vecs):
            _seg_put(text, vec)
    dim = 0
    acc: List[float] = []
    total_w = 0.0
    for text, w in pairs:
        vec = _seg_get(text)
        if not vec or w <= 0:
            continue
        if not acc:
            acc = [0.0] * len(vec)
            dim = len(vec)
        elif len(vec) != dim:
            continue
        total_w += w
        for i, x in enumerate(vec):
            acc[i] += w * x
    if not acc or total_w <= 0:
        return None
    norm = math.sqrt(sum(x * x for x in acc))
    if norm <= 0:
        return None
    return BGE_MODEL_NAME, [x / norm for x in acc]


def rerank(query: str, docs: Sequence[str], batch_size: int = 20,
           timeout: float = 4.0,
           deadline: Optional[float] = None) -> Optional[List[float]]:
    """交叉编码器打分；返回 0~1 的分数列表。

    CPU 上单对约 RERANK_COST 秒，一次喂 20 条会长于单条超时；
    这里按 timeout 推导每块条数，逐块推理（每块仍受 timeout 约束），
    整体再受 deadline 约束。任一块失败即返回 None（调用方跳过精排）。
    """
    if _RERANK is None or not docs:
        return None
    query = str(query or "")
    # 精排只看最近一段（当前用户消息）：历史拼接越长，每对 token 越多、CPU 越慢
    rq = query[-RERANK_QUERY_CHARS:] if len(query) > RERANK_QUERY_CHARS else query
    pairs = [(rq, str(d or "")[:RERANK_PAIR_CHARS]) for d in docs]
    try:
        # 0.7 安全系数：每块预计占单块时限的七成，兼顾批内并行与抖动余量
        chunk = max(1, int(max(0.2, float(timeout)) * 0.7 / RERANK_COST))
    except (TypeError, ValueError):
        chunk = 1
    chunk = min(chunk, max(1, int(batch_size or len(pairs))))
    _log(f"精排分块：{len(pairs)} 条 → 每块 {chunk} 条，块上限 {timeout:.1f}s")

    out: List[float] = []
    for start in range(0, len(pairs), chunk):
        part = pairs[start:start + chunk]
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0.15:
                _log(f"精排到总时限（已打分 {len(out)}/{len(pairs)} 条），跳过剩余")
                return None
            per = min(max(0.2, float(timeout)), left)
        else:
            per = max(0.2, float(timeout))

        def run(batch=part):
            scores = _RERANK.predict(batch, batch_size=len(batch))
            vals = [float(x) for x in scores]
            if any(x < 0.0 or x > 1.0 for x in vals):
                vals = [1.0 / (1.0 + math.exp(-x)) for x in vals]
            return vals

        got = _infer(run, per, _RERANK_LOCK, wait=0.3, hard=deadline)
        if not got or len(got) != len(part):
            # 抢锁失败（另有召回在跑）：等它收尾后再试一次，仍失败才放弃精排
            left = deadline - time.monotonic() if deadline is not None else per
            if left > 0.5:
                got = _infer(run, min(per, left), _RERANK_LOCK, wait=0.45,
                             hard=deadline)
        if not got or len(got) != len(part):
            _log(f"精排第 {start // chunk + 1} 块失败，跳过精排", once="rerank-fail")
            return None
        out.extend(got)
    return out


# ---------------------------------------------------------------- 向量空间
def _dense_space(query: str, timeout: float) -> Tuple[str, List[float]]:
    """选稠密向量空间：bge-m3 → local-lsa → hash。返回 (模型, 查询向量)。"""
    q = _truncate(query)
    vec = encode_query(q, timeout)
    if vec:
        return BGE_MODEL_NAME, vec
    try:
        from roleplay_core import local_embedder
        vec, mid, degraded = local_embedder.project_query(q)
        if vec and not degraded:
            key = (str(mid), q)
            _cache_put(key, vec)
            return str(mid), list(vec)
    except Exception as exc:         # noqa: BLE001
        _log(f"local-lsa 查询向量失败（{exc}），回落哈希", once="local-fail")
    from roleplay_core import memory_engine
    vec = memory_engine.embed(q)
    _cache_put(("hash", q), vec)
    return "hash", vec


def _doc_vector(text: str, model_id: str, item: dict) -> List[float]:
    vecs = item.get("vecs") or {}
    vec = vecs.get(model_id) or []
    if vec:
        return list(vec)
    try:
        if model_id.startswith("local-lsa"):
            from roleplay_core import local_embedder
            meta = local_embedder.active_model()
            return local_embedder.project(text, meta) if meta else []
        if model_id == "hash":
            from roleplay_core import memory_engine
            return memory_engine.embed(text)
    except Exception:        # noqa: BLE001
        return []
    return []


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦；维度不一致（不同向量空间）视为不相似，避免跨空间误算。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(float(a[i]) * float(b[i]) for i in range(len(a)))


# ---------------------------------------------------------------- 通道
def _item_text(item: dict) -> str:
    return " ".join(str(item.get(k) or "").strip()
                    for k in ("content", "subject", "object")).strip()


def _bm25_channel(query: str, uid: int, cid: str, sid: str,
                  limit: int = FTS_LIMIT) -> List[dict]:
    """FTS5 优先，失败/无命中回落纯 Python BM25。"""
    import db
    from roleplay_core import memory_engine
    items = db.memory_items(uid, cid, sid, status="active",
                            limit=int(limit * 8))
    if not items:
        return []
    texts = {int(it["id"]): _item_text(it) for it in items}
    scores = db.search_memory_fts(uid, cid, sid, query, limit=limit * 4)
    if not scores:
        scores = memory_engine.bm25(query, list(texts.items()))
    ranked = sorted(((mid, float(s)) for mid, s in scores.items() if s > 0),
                    key=lambda kv: -kv[1])[:limit]
    by_id = {int(it["id"]): it for it in items}
    out = []
    for rank, (mid, score) in enumerate(ranked, 1):
        it = by_id.get(int(mid))
        if not it:
            continue
        out.append({"id": int(mid), "text": texts[int(mid)],
                    "kind": str(it.get("kind") or ""),
                    "importance": float(it.get("importance") or 0.0),
                    "bm25": round(score, 4), "bm25_rank": rank})
    return out


def _dense_channel(query: str, uid: int, cid: str, sid: str,
                   deadline: float, limit: int = DENSE_LIMIT,
                   qvec: Optional[Sequence[float]] = None,
                   qmodel: str = "") -> List[dict]:
    """稠密余弦通道；向量缺失时由后台回填线程补齐，召回本身不等编码。

    qvec 给定时直接用它做查询向量（历史分段缓存复用），不再整串编码。
    """
    import db
    items = db.memory_items(uid, cid, sid, status="active", limit=limit * 10)
    if not items:
        return []
    remaining = deadline - time.monotonic()
    if remaining <= 0.1:
        return []
    if qvec:
        model_id, qvec_use = (qmodel or BGE_MODEL_NAME), list(qvec)
    else:
        model_id, qvec_use = _dense_space(query, min(DEFAULT_SINGLE_TIMEOUT, remaining))
    if not qvec_use:
        return []
    if model_id == BGE_MODEL_NAME:
        # 回填线程可能刚写入向量，重取一次快照，避免首轮召回漏掉新向量
        fresh = db.memory_items(uid, cid, sid, status="active", limit=limit * 10)
        if fresh:
            items = fresh
    for it in items:
        vec = _doc_vector(_item_text(it), model_id, it)
        if vec:
            it["_dense_vec"] = vec
    scored = []
    for it in items:
        vec = it.get("_dense_vec") or []
        sim = _cosine(qvec_use, vec)
        if sim > 0.0:
            scored.append({"id": int(it["id"]), "text": _item_text(it),
                           "kind": str(it.get("kind") or ""),
                           "importance": float(it.get("importance") or 0.0),
                           "cosine": round(sim, 4), "dense_vec": vec,
                           "dense_model": model_id})
    scored.sort(key=lambda x: -x["cosine"])
    for rank, it in enumerate(scored[:limit], 1):
        it["dense_rank"] = rank
    return scored[:limit]


def _rrf(rows: List[dict], k: int = RRF_K) -> List[dict]:
    """RRF 融合：同一 id 各通道 1/(k+rank) 相加，保留原始排名。"""
    merged: Dict[int, dict] = {}
    for row in rows:
        mid = int(row["id"])
        cur = merged.get(mid)
        if cur is None:
            cur = {"id": mid, "text": row.get("text") or "",
                   "kind": row.get("kind") or "",
                   "importance": float(row.get("importance") or 0.0),
                   "rrf": 0.0, "ranks": {}, "channels": [],
                   "bm25": row.get("bm25"), "cosine": row.get("cosine"),
                   "dense_model": row.get("dense_model"),
                   "vec": row.get("dense_vec") or []}
            merged[mid] = cur
        ch = "bm25" if "bm25_rank" in row else "dense"
        rank = int(row.get("bm25_rank") or row.get("dense_rank") or 999)
        cur["rrf"] += 1.0 / (k + rank)
        cur["ranks"][ch] = rank
        if ch not in cur["channels"]:
            cur["channels"].append(ch)
        if row.get("bm25") is not None:
            cur["bm25"] = row["bm25"]
        if row.get("cosine") is not None:
            cur["cosine"] = row["cosine"]
        if row.get("dense_vec") and not cur.get("vec"):
            cur["vec"] = row["dense_vec"]
    out = list(merged.values())
    for it in out:
        it["rrf"] = round(it["rrf"], 6)
    out.sort(key=lambda x: -x["rrf"])
    return out


def _mmr(items: List[dict], k: int, lam: float) -> List[dict]:
    """MMR 多样性重排，把每步的 mmr 值写回结果。"""
    if not items:
        return []
    lam = min(1.0, max(0.0, float(lam)))
    pool = list(items)
    out: List[dict] = []
    while pool and len(out) < max(1, int(k)):
        best, best_val = None, None
        for it in pool:
            rel = float(it.get("score") or 0.0)
            div = max((_cosine(it.get("vec") or [], s.get("vec") or [])
                       for s in out), default=0.0)
            val = lam * rel - (1 - lam) * div
            if best_val is None or val > best_val:
                best, best_val = it, val
        pool.remove(best)
        best["mmr"] = round(best_val, 4)
        best["final"] = round(best_val, 4)
        out.append(best)
    return out


# ---------------------------------------------------------------- 对外接口
def recall(query: str, uid: int, cid: str, sid: Optional[str] = None,
           top_k: int = 5, **overrides) -> List[dict]:
    """检索记忆；任何异常都降级成关键词结果，绝不抛出。"""
    q = _truncate(query)
    if not q:
        return []
    started = time.monotonic()
    budget = float(overrides.get("budget") or DEFAULT_BUDGET)
    deadline = started + max(0.5, budget)
    single = float(overrides.get("single_timeout") or DEFAULT_SINGLE_TIMEOUT)
    uid_i = int(uid or 0)
    cid_s = str(cid or "")
    sid_s = str(sid or "")
    k = max(1, int(top_k or 5))

    if not _LOAD_STARTED:
        warmup(wait=float(overrides.get("load_wait", 2.0)), overrides=overrides)

    keyword: List[dict] = []
    try:
        keyword = _bm25_channel(q, uid_i, cid_s, sid_s)
    except Exception as exc:         # noqa: BLE001
        _log(f"关键词通道失败（{exc}）", once="bm25-fail")
    if overrides.get("keyword_only"):
        return _finish_keyword(keyword, k) if keyword else []

    dense: List[dict] = []
    try:
        if time.monotonic() < deadline:
            qvec = qmodel = None
            segs = overrides.get("query_segments")
            if segs:
                got = segment_vector(
                    segs, overrides.get("segment_weights"),
                    timeout=min(single, max(0.2, deadline - time.monotonic() - 0.2)),
                    deadline=deadline)
                if got:
                    qmodel, qvec = got
            dense = _dense_channel(q, uid_i, cid_s, sid_s, deadline,
                                   qvec=qvec, qmodel=qmodel or "")
    except Exception as exc:         # noqa: BLE001
        _log(f"稠密通道失败（{exc}），仅用关键词", once="dense-fail")

    fused = _rrf(keyword + dense, int(overrides.get("rrf_k") or RRF_K))
    if not fused:
        return []

    # 总时限已到：直接回关键词通道结果，不返回空
    if time.monotonic() >= deadline:
        _log(f"候选召回用尽总时限（{time.monotonic() - started:.2f}s），"
             "返回关键词通道结果", once="budget-keyword")
        return _finish_keyword(keyword, k) if keyword else _public(fused[:k])

    # 精排：候选数按剩余总预算取满（rerank 内部再按 single_timeout 分块）
    head = fused[:int(overrides.get("rerank_top") or RERANK_TOP)]
    reranked = False
    if _RERANK is not None and not overrides.get("no_rerank"):
        remaining = deadline - time.monotonic()
        afford = max(3, int(max(0.0, remaining) * 0.85 / RERANK_COST))
        n = min(len(head), afford)
        sub = head[:n]
        t_rerank = time.monotonic()
        scores = rerank(q, [it["text"] for it in sub], batch_size=len(sub),
                        timeout=single, deadline=deadline)
        if scores and len(scores) == len(sub):
            _log(f"精排 {len(sub)}/{len(head)} 条"
                 f" 耗时={time.monotonic() - t_rerank:.2f}s")
            for it, s in zip(sub, scores):
                it["rerank"] = round(float(s), 4)
                it["score"] = round(float(s), 4)
            sub.sort(key=lambda x: -x["score"])
            tail = head[n:]
            head = sub + tail
            floor = min(float(it["score"]) for it in sub)
            for i, it in enumerate(tail, 1):
                it["rerank"] = None
                it["score"] = round(max(0.0, floor - 0.01 * i), 4)
            reranked = True
    if not reranked:
        top = max(float(head[0].get("rrf") or 0.0), 1e-9)
        for it in head:
            it["rerank"] = None
            it["score"] = round(float(it.get("rrf") or 0.0) / top, 4)

    # MMR：向量优先用稠密通道的，缺失时用哈希向量充当多样性度量
    for it in head:
        if not it.get("vec"):
            try:
                from roleplay_core import memory_engine
                it["vec"] = memory_engine.embed(it["text"])
            except Exception:        # noqa: BLE001
                it["vec"] = []
    if overrides.get("no_mmr"):
        for it in head[:k]:
            it["mmr"] = None
            it["final"] = it.get("score")
        return _public(head[:k])

    out = _mmr(head, k, float(overrides.get("mmr_lambda") or DEFAULT_MMR_LAMBDA))
    for it in out:
        it["rrf"] = it.get("rrf")
        it["score"] = it.get("final")
    elapsed = time.monotonic() - started
    if elapsed > budget:
        _log(f"总时限超出 {elapsed:.2f}s > {budget:.1f}s，本轮已降级输出",
             once="total-budget")
    return _public(out)


def _finish_keyword(rows: List[dict], k: int) -> List[dict]:
    out = []
    for i, it in enumerate(rows[:k], 1):
        it = dict(it)
        it.update({"rrf": round(1.0 / (RRF_K + i), 6), "rerank": None,
                   "mmr": None, "final": it.get("bm25"),
                   "score": it.get("bm25"), "channels": ["bm25"],
                   "ranks": {"bm25": i}})
        out.append(it)
    return _public(out)


def _public(rows: Iterable[dict]) -> List[dict]:
    """收敛返回结构：去掉内部向量，保留可解释分数。"""
    out = []
    for it in rows:
        out.append({
            "id": int(it.get("id") or 0),
            "text": str(it.get("text") or ""),
            "kind": str(it.get("kind") or ""),
            "importance": float(it.get("importance") or 0.0),
            "score": it.get("score"),
            "rrf": it.get("rrf"),
            "rerank": it.get("rerank"),
            "mmr": it.get("mmr"),
            "final": it.get("final"),
            "scores": {"bm25": it.get("bm25"), "cosine": it.get("cosine"),
                       "rrf": it.get("rrf"), "rerank": it.get("rerank"),
                       "mmr": it.get("mmr"), "final": it.get("final")},
            "channels": list(it.get("channels") or []),
            "ranks": dict(it.get("ranks") or {}),
        })
    return out


def build_injection(results: Sequence[dict], max_tokens: int = 800) -> Optional[str]:
    """把召回结果拼成可注入 prompt 的文本块；空结果返回 None。"""
    if not results:
        return None
    label = {"fact": "事实", "event": "事件", "relation": "关系",
             "promise": "承诺", "preference": "偏好"}
    try:
        from prompt_builder import count_tokens
    except Exception:        # noqa: BLE001
        def count_tokens(text):
            return max(1, len(str(text or "")))      # 粗略兜底

    lines = ["### 记忆召回（按相关度排序）"]
    used = count_tokens(lines[0])
    budget = max(1, int(max_tokens or 800))
    for it in results:
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        kind = label.get(str(it.get("kind") or ""), "记忆")
        line = f"- [{kind}] {text}"
        n = count_tokens(line)
        if used + n > budget:
            break
        lines.append(line)
        used += n
    if len(lines) == 1:
        return None
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI 辅助
def _corpus(source: str = "messages") -> List[dict]:
    """对照用真实语料：优先记忆表，空库时用本地真实对话切段。"""
    try:
        from roleplay_core import local_embedder
        docs = local_embedder._load_docs(source)
    except Exception:        # noqa: BLE001
        docs = []
    if not docs:
        try:
            import db
            for r in db.query("SELECT id, content FROM chat_messages ORDER BY id"):
                text = str(r["content"] or "").strip()
                if text:
                    docs.append({"id": None, "text": text[:600],
                                 "label": f"msg{r['id']}"})
        except Exception:    # noqa: BLE001
            pass
    return docs[:200]


def _fmt(rows: Sequence[Tuple[float, str, str]], k: int = 5,
         ordered: bool = False) -> List[str]:
    seq = rows[:k] if ordered else sorted(rows, key=lambda x: -x[0])[:k]
    out = []
    for rank, (score, key, text) in enumerate(seq, 1):
        out.append(f"{rank}. {key} ({score:.4f}) {text[:46]}")
    return out


def compare(queries: Sequence[str], k: int = 5) -> None:
    """四通道对照：BM25 / 稠密 / 融合+精排 / 融合+精排+MMR。"""
    from roleplay_core import memory_engine
    docs = _corpus("messages")
    if not docs:
        print("[recall] 没有可用语料（chat_messages 为空）")
        return
    texts = [d["text"] for d in docs]
    labels = [str(d.get("label") or d.get("id") or i) for i, d in enumerate(docs)]
    if _EMB is None:
        warmup(wait=15.0)
    vecs = encode(texts, batch_size=16, timeout=60.0)
    if not vecs:
        print(f"[recall] bge-m3 不可用：{_EMB_ERR or '未就绪'}")
        return
    print(f"[recall] 对照语料 {len(docs)} 条，bge-m3 维度 {len(vecs[0])}")
    for q in queries:
        qvec = encode_query(q, timeout=8.0)
        if not qvec:
            print(f"\nQ: {q}\n  bge-m3 查询编码失败")
            continue
        bm = memory_engine.bm25(q, list(enumerate(texts)))
        bm_rows = [(float(s), labels[i], texts[i]) for i, s in bm.items() if s > 0]
        dense_rows = [(_cosine(qvec, vecs[i]), labels[i], texts[i])
                      for i in range(len(docs))]
        # RRF 融合（两通道各取 20）
        bm_top = sorted(bm_rows, key=lambda x: -x[0])[:20]
        dn_top = sorted(dense_rows, key=lambda x: -x[0])[:20]
        fused: Dict[str, dict] = {}
        for rank, row in enumerate(bm_top, 1):
            key = row[1]
            fused.setdefault(key, {"text": row[2], "rrf": 0.0, "rank": {}})
            fused[key]["rrf"] += 1.0 / (RRF_K + rank)
            fused[key]["rank"]["bm25"] = rank
        for rank, row in enumerate(dn_top, 1):
            key = row[1]
            fused.setdefault(key, {"text": row[2], "rrf": 0.0, "rank": {}})
            fused[key]["rrf"] += 1.0 / (RRF_K + rank)
            fused[key]["rank"]["dense"] = rank
        order = sorted(fused.items(), key=lambda kv: -kv[1]["rrf"])[:RERANK_TOP]
        scores = rerank(q, [v["text"] for _k, v in order], batch_size=len(order),
                        timeout=10.0) or []
        rr_rows = []
        for i, (key, val) in enumerate(order):
            s = float(scores[i]) if i < len(scores) else float(val["rrf"])
            rr_rows.append((s, key, val["text"]))
        rr_rows.sort(key=lambda x: -x[0])
        # MMR（多样性用稠密向量）
        pool = []
        for s, key, text in rr_rows:
            idx = labels.index(key) if key in labels else 0
            pool.append({"key": key, "text": text, "score": s,
                         "vec": vecs[idx] if idx < len(vecs) else []})
        mm_rows = []
        picked: List[dict] = []
        while pool and len(picked) < k:
            best, best_val = None, None
            for it in pool:
                div = max((_cosine(it["vec"], p["vec"]) for p in picked), default=0.0)
                val = DEFAULT_MMR_LAMBDA * it["score"] - (1 - DEFAULT_MMR_LAMBDA) * div
                if best_val is None or val > best_val:
                    best, best_val = it, val
            pool.remove(best)
            picked.append(best)
            mm_rows.append((best_val, best["key"], best["text"]))
        print(f"\nQ: {q}")
        print("  [BM25]            " + " | ".join(_fmt(bm_rows, k)))
        print("  [bge-m3 稠密]     " + " | ".join(_fmt(dense_rows, k)))
        print("  [融合+rerank]     " + " | ".join(_fmt(rr_rows, k)))
        print("  [融合+rerank+MMR] " + " | ".join(_fmt(mm_rows, k, ordered=True)))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="记忆检索（BM25 + bge-m3 + rerank + MMR）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_recall = sub.add_parser("recall")
    p_recall.add_argument("--uid", type=int, default=1)
    p_recall.add_argument("--cid", default="1")
    p_recall.add_argument("--sid", default="")
    p_recall.add_argument("--q", required=True)
    p_recall.add_argument("--top-k", type=int, default=5)
    p_recall.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    p_recall.add_argument("--keyword-only", action="store_true")

    p_bench = sub.add_parser("bench")
    p_bench.add_argument("--q", default="窗外的雨")
    p_bench.add_argument("--repeat", type=int, default=3)

    p_cmp = sub.add_parser("compare")
    p_cmp.add_argument("--q", action="append", required=True)
    p_cmp.add_argument("--top-k", type=int, default=5)

    args = ap.parse_args(argv)
    import db
    db.init()

    if args.cmd == "recall":
        st = warmup(wait=10.0)
        t0 = time.monotonic()
        got = recall(args.q, args.uid, args.cid, sid=args.sid or None,
                     top_k=args.top_k, budget=args.budget,
                     keyword_only=args.keyword_only)
        dt = time.monotonic() - t0
        print(f"[recall] 命中 {len(got)} 条，耗时 {dt * 1000:.1f}ms，模型 {st}")
        for i, it in enumerate(got, 1):
            print(f"{i}. id={it['id']} [{it['kind']}] score={it['score']} "
                  f"rrf={it['rrf']} rerank={it['rerank']} mmr={it['mmr']} "
                  f"ch={','.join(it['channels'])}"
                  f"\n   {it['text'][:80]}")
        block = build_injection(got)
        if block:
            print("\n--- 注入块 ---\n" + block)
        return 0

    if args.cmd == "bench":
        warmup(wait=20.0)
        st = status()
        print(f"[recall] 模型状态 {st}")
        texts = [f"第{i}段测试文本，关于雨天、书本和桌上的茶杯。" for i in range(16)]
        docs = [f"候选文档{i}：窗外下雨，书页泛黄，毛巾和大麦茶。" for i in range(20)]
        single = []
        for _ in range(max(1, args.repeat)):
            t0 = time.monotonic()
            encode([args.q], batch_size=1, timeout=10.0)   # 直连模型，绕过查询缓存
            single.append((time.monotonic() - t0) * 1000)
        batch = []
        for _ in range(max(1, args.repeat)):
            t0 = time.monotonic()
            encode(texts, batch_size=16, timeout=30.0)
            batch.append((time.monotonic() - t0) * 1000)
        rer = []
        for _ in range(max(1, args.repeat)):
            t0 = time.monotonic()
            rerank(args.q, docs, batch_size=20, timeout=30.0)
            rer.append((time.monotonic() - t0) * 1000)
        avg = lambda xs: sum(xs) / max(1, len(xs))
        print(f"[recall] 单条 query 编码 {avg(single):.0f}ms"
              f"（{', '.join(f'{x:.0f}' for x in single)}）")
        print(f"[recall] 批量 16 条编码   {avg(batch):.0f}ms"
              f"（{', '.join(f'{x:.0f}' for x in batch)}）")
        print(f"[recall] rerank 20 条     {avg(rer):.0f}ms"
              f"（{', '.join(f'{x:.0f}' for x in rer)}）")
        t0 = time.monotonic()
        got = recall(args.q, 1, "1", top_k=5)
        print(f"[recall] 真实库 recall {len(got)} 条，{(time.monotonic() - t0) * 1000:.0f}ms")
        return 0

    if args.cmd == "compare":
        compare(list(args.q), args.top_k)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
