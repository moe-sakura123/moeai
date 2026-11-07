#!/usr/bin/env python3
"""prompt.py — 唯一提示词组装引擎（流式 / 非流式 / 访客统一走这里）

设计要点：
  · 分区（PromptSection）+ 每区预算 + 优先级裁剪，而不是单一 history budget；
  · 世界书升级为「触发器 + order + position/depth + group + 递归 1 步」；
  · 角色卡 personality / scenario / mesExample 真正进入 prompt；
  · 世界书按 position 插到固定前缀、历史之后或历史内部（depth）；
  · 输出结构可被日志逐段 diff（sectionize 在 prompt_builder 里）。

本模块不碰 DB：世界书条目、记忆、配置都由调用方传入，便于单测与回归。
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, Iterable, List, Optional

from prompt_builder import (
    FORMAT_EXAMPLE, FORMAT_REMINDER, FORMAT_RULES, NEXT_PROMPT_DEFAULT,
    PromptBuilder, clean_description_html, count_tokens, estimate_messages)

# 各分区预算占 contextWindow 的比例；优先级越小越先保。
# 历史拿剩余预算（下限 25%），超出从最旧开始丢。
SECTION_SPECS: List[Dict] = [
    {"id": "persona", "role": "system", "ratio": 0.30, "priority": 1,
     "stable": True},
    {"id": "identity", "role": "system", "ratio": 0.10, "priority": 2,
     "stable": True},
    {"id": "character_extra", "role": "system", "ratio": 0.08, "priority": 3,
     "stable": True},
    {"id": "examples", "role": "system", "ratio": 0.10, "priority": 4,
     "stable": True},
    {"id": "worldbook_before", "role": "system", "ratio": 0.15, "priority": 2},
    {"id": "memory", "role": "system", "ratio": 0.15, "priority": 3},
    {"id": "status", "role": "system", "ratio": 0.08, "priority": 2},
    {"id": "worldbook_after", "role": "system", "ratio": 0.15, "priority": 2},
    {"id": "format", "role": "system", "ratio": 0.06, "priority": 1},
]

DEFAULT_CONTEXT_WINDOW = 8192
# 已确认的取舍：group 取 order 最高一条；probability 默认 100；
# 递归只做 1 步。probability 只作为“是否允许出现”的开关（0 = 永不出现），
# 不做随机抽签，保证回归样本可复现。
WB_POS_BEFORE = ("before_char", "before", "before_an", "at_start", "0")
WB_POS_AFTER = ("after_char", "after", "after_an", "at_end", "1")

# 记忆四槽：优先级从高到低；同组只保留总预算内的高优先级内容。
SLOT_ORDER = ("promise", "user_info", "long_term", "short_term")
SLOT_DEPTH = {"promise": 2, "user_info": 3, "long_term": 3, "short_term": 4}
SLOT_BUDGET = {"promise": 150, "user_info": 150, "long_term": 300,
               "short_term": 300}
SLOT_LABEL = {"promise": "承诺与待办", "user_info": "用户信息",
              "long_term": "长期记忆", "short_term": "近期剧情"}
MEMORY_TOTAL_BUDGET = 800


def _tok(text: str) -> int:
    return count_tokens(text)


def trim_tokens(text: str, budget: int) -> str:
    """按预算截断文本（尽量按行切，避免把一句话切在中间）。"""
    text = str(text or "").strip()
    if not text or budget <= 0:
        return ""
    if _tok(text) <= budget:
        return text
    out: List[str] = []
    used = 0
    for line in text.splitlines():
        n = _tok(line)
        if used + n > budget:
            break
        out.append(line)
        used += n
    return "\n".join(out).strip()


def _keys(raw) -> List[str]:
    """世界书 keys 兼容 dict / list / 逗号字符串。"""
    if isinstance(raw, dict):
        raw = raw.get("keys") or raw.get("key") or []
    if isinstance(raw, str):
        raw = re.split(r"[,，\n]", raw)
    out = []
    for k in raw if isinstance(raw, (list, tuple)) else []:
        k = str(k or "").strip()
        if k:
            out.append(k)
    return out


def normalize_entry(entry: Dict, entry_id: str = "") -> Dict:
    """把库里的 payload 归一成世界书引擎需要的最小结构。"""
    e = dict(entry or {})
    eid = str(e.get("entry_id") or entry_id or e.get("worldbookID")
              or e.get("id") or e.get("ID") or "").strip()
    try:
        order = int(e.get("order") or 0)
    except (TypeError, ValueError):
        order = 0
    try:
        depth = int(e.get("depth") or 0)
    except (TypeError, ValueError):
        depth = 0
    try:
        prob = int(e.get("probability") if e.get("probability") is not None else 100)
    except (TypeError, ValueError):
        prob = 100
    pos_raw = e.get("position", e.get("pos", "before_char"))
    pos = str(pos_raw).strip().lower()
    if pos in WB_POS_BEFORE:
        position = "before"
    elif pos in WB_POS_AFTER:
        position = "after"
    elif pos in ("at_depth", "depth", "2"):
        position = "depth"
    else:
        position = "before"
    # priority 只管预算淘汰，order 只管注入位置；老数据没有 priority → 等价于 order
    try:
        priority = int(e.get("priority"))
    except (TypeError, ValueError):
        priority = order
    if e.get("ignore_on_max_context") or e.get("ignoreOnMaxContext"):
        priority = -1000                 # 显式「上下文吃紧就不注入」
    return {
        "id": eid,
        "content": str(e.get("content") or "").strip(),
        "keys": _keys(e.get("keys")),
        "constant": bool(e.get("constant", False)),
        "enabled": bool(e.get("enabled", True)),
        "order": order,
        "priority": priority,
        "position": position,
        "depth": depth,
        "probability": prob,
        "group": str(e.get("group") or "").strip(),
        "recursive": bool(e.get("recursive", False)),
    }


def match_worldbook(entries: Iterable[Dict], context: str,
                    max_tokens: int = 0) -> Dict[str, List[Dict]]:
    """世界书匹配：constant 恒注入，其余按 keys 子串命中；递归扩 1 步。

    返回 {"before": [...], "after": [...], "depth": [...]}，已按 order 升序。
    """
    text = str(context or "").lower()
    norm = [normalize_entry(e) for e in (entries or [])]
    norm = [e for e in norm if e["enabled"] and e["content"]]
    # probability=0 视为永不出现；其余（含默认 100）都参与匹配，不做抽签
    norm = [e for e in norm if e["probability"] > 0]

    def hit(e: Dict) -> bool:
        if e["constant"]:
            return True
        return any(k.lower() in text for k in e["keys"])

    picked = [e for e in norm if hit(e)]
    selected = {e["id"]: e for e in picked if e["id"]}
    # 递归只扩 1 步：用已命中条目的正文再扫一遍未命中条目的 keys
    seed = "\n".join(e["content"] for e in picked).lower()
    if seed:
        for e in norm:
            if e["id"] in selected or not e["keys"]:
                continue
            if any(k.lower() in seed for k in e["keys"]):
                selected[e["id"]] = e
    rows = list(selected.values()) or picked

    # group：同组只留 order 最高（并列时 id 最大）的一条
    keep_ids = {e["id"] for e in rows}
    groups: Dict[str, List[Dict]] = {}
    for e in rows:
        if e["group"]:
            groups.setdefault(e["group"], []).append(e)
    for grp in groups.values():
        best = max(grp, key=lambda e: (e["order"], str(e["id"])))
        for e in grp:
            if e["id"] != best["id"]:
                keep_ids.discard(e["id"])
    rows = [e for e in rows if e["id"] in keep_ids]

    # 装满阶段按 priority 从高到低（老数据 priority=order，行为不变）；
    # 输出阶段再按 order 升序，谁先注入跟预算淘汰彻底分开。
    rows.sort(key=lambda e: (-e["priority"], -e["order"], str(e["id"])))
    budget = int(max_tokens or 0)
    used = 0
    taken: List[Dict] = []
    for e in rows:
        n = _tok(e["content"])
        if budget and used + n > budget:
            continue                  # 单条超预算跳过，继续尝试更小的条目
        taken.append(e)
        used += n
    taken.sort(key=lambda e: (e["order"], str(e["id"])))
    out = {"before": [], "after": [], "depth": []}
    for e in taken:
        out[e["position"]].append(e)
    return out


def _character_parts(ctx) -> Dict[str, str]:
    """角色卡的扩展字段：personality / scenario / mesExample / firstMes。"""
    ch = (ctx.character_data or {}).get("character") or {}
    prompt = (ctx.character_data or {}).get("promptData") or {}
    return {
        "personality": str(ch.get("personality") or "").strip(),
        "scenario": str(ch.get("scenario") or "").strip(),
        "mes_example": str(ch.get("mesExample") or "").strip(),
        "first_mes": str(ch.get("firstMes") or "").strip(),
        "post_history": str(ch.get("postHistoryInstructions")
                            or prompt.get("postHistoryInstructions") or "").strip(),
    }


def _prefix(ctx) -> str:
    """固定前缀（稳定）：核心设定 → 身份/世界观/风格 → 人格/场景 → CG → 选项 → 破甲。"""
    ch = (ctx.character_data or {}).get("character") or {}
    prompt = (ctx.character_data or {}).get("promptData") or {}
    extra = _character_parts(ctx)
    pb = PromptBuilder(ch, prompt, (ctx.character_data or {}).get("cg"),
                       name=ctx.character_data.get("name") or "角色",
                       db_rules_list=ctx.stat_rules)
    parts: List[str] = []
    core = str(prompt.get("systemPrompt") or "").strip() \
        or clean_description_html(ch.get("description") or "")
    if core:
        parts.append("### 角色核心设定\n" + core)
    ident = str(prompt.get("aiIdentityStylePrompt") or "").strip()
    if ident and ident not in core:
        parts.append("### 身份与任务\n" + ident)
    world = str(prompt.get("worldViewPrompt") or "").strip()
    if world:
        parts.append("### 世界观背景\n" + world)
    style = str(prompt.get("writingStylePrompt") or "").strip()
    if style:
        parts.append("### 写作风格要求\n" + style)
    if extra["personality"]:
        parts.append("### 角色性格\n" + extra["personality"])
    if extra["scenario"]:
        parts.append("### 当前场景\n" + extra["scenario"])
    catalog = pb._build_cg_catalog()
    if catalog:
        parts.append(catalog)
    post = str(prompt.get("postPrompt") or "").strip() or NEXT_PROMPT_DEFAULT
    parts.append("### 剧情选项要求\n" + post)
    bk = str(prompt.get("breakArmorWord") or "").strip()
    if bk:
        parts.append("### 破甲词\n" + bk)
    if not (ctx.character_data or {}).get("hasPayload"):
        parts.insert(0, f"### 角色核心设定\n你是{ctx.character_data.get('name') or '角色'}，"
                        "保持这个角色的设定、语气与故事背景，用自然的中文推进对话。")
    return "\n\n".join(parts)


def _status_text(ctx) -> str:
    ch = (ctx.character_data or {}).get("character") or {}
    prompt = (ctx.character_data or {}).get("promptData") or {}
    pb = PromptBuilder(ch, prompt, None, db_rules_list=ctx.stat_rules)
    return pb._build_status_panel(ctx.current_status)


def _depth_blocks(entries: List[Dict]) -> Dict[int, List[str]]:
    """depth 注入：0 = 历史最后一句之前，1 = 再往前一条…"""
    out: Dict[int, List[str]] = {}
    for e in entries or []:
        d = max(0, int(e.get("depth") or 0))
        out.setdefault(d, []).append(str(e.get("content") or "").strip())
    return out


def _drop_blocks(messages: List[Dict], prefixes: tuple[str, ...]) -> List[Dict]:
    """按块前缀删掉 system 消息里的段落；整条删空则丢弃该消息。"""
    out: List[Dict] = []
    for m in messages or []:
        msg = dict(m)
        if str(msg.get("role") or "") == "system":
            parts = str(msg.get("content") or "").split("\n\n")
            kept = [p for p in parts if not p.startswith(prefixes)]
            if len(kept) != len(parts):
                msg["content"] = "\n\n".join(p for p in kept if p.strip()).strip()
                if not msg["content"]:
                    continue
        out.append(msg)
    return out


def _drop_old_history(messages: List[Dict], keep_recent: int) -> List[Dict]:
    """从最旧开始丢历史，但保留最近 keep_recent 条（摘要窗口不受影响）。"""
    conv = [i for i, m in enumerate(messages)
            if str((m or {}).get("role") or "") in ("user", "assistant")]
    if len(conv) <= max(1, int(keep_recent or 0)):
        return list(messages)
    drop = set(conv[:-max(1, int(keep_recent or 0))])
    out: List[Dict] = []
    for i, m in enumerate(messages):
        if i in drop:
            continue
        out.append(dict(m))
    return out


def shrink_for_overflow(messages: List[Dict], level: int,
                        keep_recent: int = 24) -> List[Dict]:
    """上下文超限降级：level 越大删得越多，返回新列表，不改入参。

    1) 丢世界书（前置 / 后置 / 历史里的深度注入）
    2) 丢记忆块、相关记忆、作者注
    3) 从最旧开始丢历史，保留最近 keep_recent 条（默认与 SUMMARY_WINDOW 对齐）
    """
    out = [dict(m) for m in (messages or [])]
    lv = max(0, min(3, int(level or 0)))
    if lv >= 1:
        out = _drop_blocks(out, ("### 世界书（前置）", "### 世界书（后置）",
                                 "### 世界书（深度注入）"))
    if lv >= 2:
        out = _drop_blocks(out, ("### 记忆 · ", "### 相关记忆", "### 记忆召回",
                                 "### 作者注"))
    if lv >= 3:
        out = _drop_old_history(out, keep_recent)
    return out


def compress_for_budget(messages: List[Dict], target_tokens: int,
                        keep_rounds: int = 6) -> tuple[List[Dict], int]:
    """发送前确定性裁剪（纯本地、无网络、无延迟）。

    返回 (新 messages, 被丢掉的历史条数)。顺序固定：
      前置世界书 → 后置世界书 → 深度世界书 → few-shot → 最旧历史
    人设前缀 / 状态栏 / 格式规则 / 本轮 user 永不删；
    历史保留最近 keep_rounds 轮（默认 6 轮 = 12 条）原文。
    低 priority 世界书在 match_worldbook 装预算时已经被淘汰，这里删的是整段。
    """
    out = [dict(m) for m in (messages or [])]
    target = int(target_tokens or 0)

    def _est(ms: List[Dict]) -> int:
        # 与发送前预判同一口径（含每条消息的固定开销），裁剪后必定小于 target
        return estimate_messages(ms)

    for prefixes in (("### 世界书（前置）",), ("### 世界书（后置）",),
                     ("### 世界书（深度注入）",), ("### 对话示例",)):
        if target and _est(out) <= target:
            break
        out = _drop_blocks(out, prefixes)

    before_conv = sum(1 for m in out
                      if str(m.get("role") or "") in ("user", "assistant"))
    if target and _est(out) <= target:
        return out, 0                   # 世界书 / few-shot 删完已够小，不动历史
    keep = max(2, int(keep_rounds or 0) * 2)
    out = _drop_old_history(out, keep)
    after_conv = sum(1 for m in out
                     if str(m.get("role") or "") in ("user", "assistant"))
    return out, max(0, before_conv - after_conv)


def _slot_blocks(slots: Dict[str, str]) -> List[tuple[int, int, str]]:
    """记忆四槽 → (depth, 排序位, 文本)，总预算 800 token。

    优先级 promises > user_info > long_term > short_term；超预算先砍短期。
    """
    out: List[tuple[int, int, str]] = []
    used = 0
    for rank, slot in enumerate(SLOT_ORDER):
        text = trim_tokens(str((slots or {}).get(slot) or "").strip(),
                           SLOT_BUDGET[slot])
        if not text:
            continue
        if used + _tok(text) > MEMORY_TOTAL_BUDGET:
            text = trim_tokens(text, max(0, MEMORY_TOTAL_BUDGET - used))
            if not text:
                continue
        out.append((SLOT_DEPTH[slot], rank,
                    f"### 记忆 · {SLOT_LABEL[slot]}\n{text}"))
        used += _tok(text)
    return out


def _anchor_block(cfg: Dict, ctx) -> tuple[int, str] | None:
    """作者注 / Persona anchor：按配置的 depth + frequency 注入。"""
    anchor = cfg.get("anchor") if isinstance(cfg.get("anchor"), dict) else {}
    if not anchor.get("enabled"):
        return None
    text = str(anchor.get("text") or "").strip() \
        or str((ctx.prompt_data or {}).get("authorNote") or "").strip()
    if not text:
        return None
    try:
        depth = max(0, int(anchor.get("depth") if anchor.get("depth") is not None
                           else 2))
    except (TypeError, ValueError):
        depth = 2
    try:
        freq = max(1, int(anchor.get("frequency") or 1))
    except (TypeError, ValueError):
        freq = 1
    turn_no = 1 + sum(1 for m in (ctx.history or [])
                      if getattr(m, "role", "") == "assistant")
    if turn_no % freq:
        return None
    return depth, trim_tokens("### 作者注\n" + text, 300)


def _merged_depth(wb_depth: List[Dict], slots: Dict[str, str],
                  anchor: tuple[int, str] | None,
                  retrieved_block: str = "",
                  retrieved_depth: int = 4) -> Dict[int, List[str]]:
    """把世界书 / 作者注 / 算法检索记忆 / 四槽合并成 {depth: [block, ...]}。

    同一 depth 内顺序：世界书 → 作者注 → 检索记忆 → promise → user_info
    → long_term → short_term。
    """
    grouped: Dict[int, List[tuple[int, str]]] = {}
    for d, texts in _depth_blocks(wb_depth).items():
        for t in texts:
            grouped.setdefault(d, []).append((0, "### 世界书（深度注入）\n" + t))
    if anchor:
        d, text = anchor
        grouped.setdefault(d, []).append((1, text))
    if retrieved_block:
        grouped.setdefault(max(0, int(retrieved_depth)), []).append(
            (2, retrieved_block))
    for depth, rank, text in _slot_blocks(slots):
        grouped.setdefault(depth, []).append((3 + rank, text))
    return {d: [t for _, t in sorted(items, key=lambda x: x[0])]
            for d, items in grouped.items()}


def _retrieved_block(retrieved: List[Dict] | None, max_tokens: int = 600) -> str:
    lines = []
    for it in retrieved or []:
        kind = str(it.get("kind") or "fact")
        try:
            imp = float(it.get("importance") or 0.0)
        except (TypeError, ValueError):
            imp = 0.0
        date = str(it.get("created_at") or "")[:10]
        lines.append(f"- [{kind}｜重要度{imp:.2f}｜{date}] "
                     f"{str(it.get('content') or '').strip()}")
    if not lines:
        return ""
    return trim_tokens("### 相关记忆（按综合得分排序）\n" + "\n".join(lines),
                       max(60, int(max_tokens or 600)))


def _injected_memory_block(cfg: Dict, retrieved: List[Dict] | None) -> str:
    """记忆注入块：优先用新模块产出的文本，旧路径仅作为兼容后备。"""
    mem = cfg.get("memory") if isinstance(cfg.get("memory"), dict) else {}
    block = str(mem.get("injectedBlock") or "").strip()
    if block:
        return block
    return _retrieved_block(retrieved, int(mem.get("maxTokens") or 600))


def build_prompt(ctx, *, worldbook_entries: Iterable[Dict] | None = None,
                 memories: List[Dict] | None = None,
                 slots: Dict[str, str] | None = None,
                 retrieved: List[Dict] | None = None,
                 config: Dict | None = None) -> List[Dict]:
    """组装 messages。

    返回顺序：
      [固定前缀] [before 世界书] [few-shot + 历史(含世界书/记忆/作者注 depth 注入)]
      [after 世界书] [状态] [格式] [本轮 user]
    """
    cfg = dict(config or {})
    window = int(cfg.get("contextWindow") or DEFAULT_CONTEXT_WINDOW)
    extra = _character_parts(ctx)
    wb = match_worldbook(worldbook_entries or [], _worldbook_context(ctx),
                         max_tokens=int(window * 0.15))

    # 旧表（chat_memories）只读兼容：没有四槽时整段当 short_term
    slot_text = {k: str(v or "").strip()
                 for k, v in (slots or {}).items() if str(v or "").strip()}
    if not slot_text and memories:
        slot_text = {"short_term": "\n".join(
            f"- {m.get('content')}" for m in memories if m.get("content"))}

    # 各段先切预算，再按顺序拼
    prefix = trim_tokens(_prefix(ctx), int(window * 0.32))
    before = trim_tokens("\n\n".join(e["content"] for e in wb["before"]),
                         int(window * 0.15))
    after = trim_tokens("\n\n".join(e["content"] for e in wb["after"]),
                        int(window * 0.15))
    status = trim_tokens(_status_text(ctx), int(window * 0.10))
    examples = trim_tokens("### 对话示例\n" + extra["mes_example"],
                           int(window * 0.10)) if extra["mes_example"] else ""
    depth_map = _merged_depth(wb["depth"], slot_text, _anchor_block(cfg, ctx),
                              _injected_memory_block(cfg, retrieved),
                              int((cfg.get("memory") or {}).get("injectDepth")
                                  or 4))
    slot_tokens = sum(_tok(t) for texts in depth_map.values() for t in texts)

    # 历史预算 = 剩余（但至少 25%），从最新往前装
    used = sum(_tok(x) for x in (prefix, before, status, examples)) \
        + _tok(after) + _tok(FORMAT_RULES + FORMAT_EXAMPLE) + slot_tokens
    hist_budget = max(int(window * 0.25), window - used - 400)
    history = _history(ctx, hist_budget, depth_map)

    messages: List[Dict] = [{"role": "system", "content": prefix}]
    if before:
        messages.append({"role": "system", "content": "### 世界书（前置）\n" + before})
    if examples:
        messages.append({"role": "system", "content": examples})
    messages.extend(history)
    if after:
        messages.append({"role": "system", "content": "### 世界书（后置）\n" + after})
    if status:
        messages.append({"role": "system", "content": status})
    fmt = FORMAT_RULES + "\n\n" + FORMAT_EXAMPLE
    if extra["post_history"]:
        fmt = "### 本轮补充要求\n" + extra["post_history"] + "\n\n" + fmt
    messages.append({"role": "system", "content": fmt})
    user = str(ctx.user_input or "").strip()
    if user:
        messages.append({"role": "user", "content": user + FORMAT_REMINDER})
    return messages


def _worldbook_context(ctx) -> str:
    """世界书匹配语料：本轮输入 + 最近 4 条历史。"""
    hist = list(ctx.history or [])[-4:]
    return "\n".join([str(ctx.user_input or "")]
                     + [str(getattr(m, "content", "")) for m in hist])


def _history(ctx, budget: int, depth_entries: Dict[int, List[str]]) -> List[Dict]:
    """历史从最新往前装；depth=N 的世界书插在倒数第 N 条消息之前。"""
    hist = []
    for m in ctx.history or []:
        text = str(getattr(m, "content", "")).strip()
        if text:
            hist.append({"role": "user" if getattr(m, "role", "") == "user"
                         else "assistant", "content": text})
    kept: List[Dict] = []
    used = 0
    for msg in reversed(hist):
        n = _tok(msg["content"])
        if kept and used + n > budget:
            break
        kept.insert(0, msg)
        used += n
        if used >= budget:
            break
    # depth 插入：depth 0 贴在本轮 user 之前；1/2… 依次往前
    inserts: List[Dict] = []
    if depth_entries:
        for d in sorted(depth_entries):
            block = "\n\n".join(x for x in depth_entries[d] if x)
            if not block:
                continue
            pos = len(kept) - d
            pos = max(0, min(pos, len(kept)))
            inserts.append({"pos": pos, "content": block,
                            "seq": d})
    if not inserts:
        return kept
    out: List[Dict] = []
    by_pos: Dict[int, List[str]] = {}
    for it in inserts:
        by_pos.setdefault(it["pos"], []).append(it["content"])
    for i, msg in enumerate(kept):
        if i in by_pos:
            out.append({"role": "system",
                        "content": "\n\n".join(by_pos[i])})
        out.append(msg)
    if len(kept) in by_pos:
        out.append({"role": "system",
                    "content": "\n\n".join(by_pos[len(kept)])})
    return out


def prompt_diff(prev: List[Dict], cur: List[Dict]) -> List[Dict]:
    """逐段 diff（按 section 的 hash 对比），供日志定位 OOC 是由哪一段引起的。"""
    from prompt_builder import sectionize
    a = {s["index"]: s for s in sectionize(prev or [])}
    b = {s["index"]: s for s in sectionize(cur or [])}
    out = []
    for i in sorted(set(a) | set(b)):
        x, y = a.get(i), b.get(i)
        if x is None or y is None:
            out.append({"index": i, "change": "added" if x is None else "removed",
                        "id": (y or x)["id"]})
        elif x["hash"] != y["hash"] or x["tokens"] != y["tokens"]:
            out.append({"index": i, "change": "changed", "id": y["id"],
                        "from": x["hash"], "to": y["hash"],
                        "tokensDelta": y["tokens"] - x["tokens"]})
    return out
