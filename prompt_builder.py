"""提示词组装的共享组件。

roleplay_core/prompt.py 是唯一拼装引擎；本模块只提供它复用的
token 估算、分区统计、固定前缀 hash，以及角色卡 CG / 状态面板片段。
整段消息顺序、历史滑窗与预算分配都由它负责。
"""
from __future__ import annotations

import hashlib
import math
from typing import Dict, Iterable, List, Optional

from roleplay_core.emotion_mapper import EmotionMapper
from roleplay_core.sanitizer import clean_description_html

# 零依赖粗估：CJK 1.5 token/字，其他 0.25 token/字，每条消息再算 4 个结构开销。
# 真实用量由 usage.prompt_tokens 反向校准（α 存在 server.py，按 provider+model 分键）。
TOKEN_CJK_RATIO = 1.5
TOKEN_OTHER_RATIO = 0.25
TOKEN_MSG_OVERHEAD = 4
TOKEN_ALPHA_DEFAULT = 1.0
TOKEN_ALPHA_MIN = 0.75
TOKEN_ALPHA_MAX = 1.6

_ALPHA = TOKEN_ALPHA_DEFAULT

# 剧情选项·系统默认模板：promptData.postPrompt 为空（自定义开关关闭）时使用
NEXT_PROMPT_DEFAULT = (
    "作为互动小说剧情引导者，基于当前剧情、核心人物关系与未解决冲突，"
    "生成 3 个差异化下一步剧情选项（每个选项含明确行动 / 转折方向，简洁不冗余），"
    "输出选项，请使用<next_prompt>标签对包裹。示例如下：\n"
    "<next_prompt>\n"
    "A、XXXXXXXX\n"
    "B、XXXXXXXX\n"
    "C、XXXXXXXX\n"
    "</next_prompt>"
)

# 世界书里没有「状态栏规则」条目时的兜底数值规则
DEFAULT_STATUS_RULE = "每次回复数值变化幅度不超过 5，数字变化要自然。"


# 强制输出格式规范：<say> 只包动作/神态/心理（括号内），台词纯文本，
# 结尾带 <book_status> 状态栏。属性名与数值不写死，按角色设定填写。
FORMAT_RULES = (
    "### 强制输出格式规范（必须绝对遵守）\n"
    "1. 你的动作、神态与心理描写必须用 `<say>` 标签包裹，并放在括号内，"
    "例如：`<say>（推开门走进来）</say>`。\n"
    "2. 你的对话台词绝对不能放进 `<say>` 标签里，直接输出纯文本。\n"
    "3. 回复结尾必须附带状态栏，属性名与数值按角色设定填写，格式："
    "`<book_status><item>属性名：<b>当前数值</b></item></book_status>`。"
)

FORMAT_EXAMPLE = (
    "### 格式示例\n"
    "<say>（放下手中的书，抬眼看向你，语气放得很轻）</say>\n"
    "外面的雨还在下，先喝口热茶吧。\n"
    "<book_status><item>属性名：<b>数值</b></item></book_status>"
)

# 贴在本轮 user 消息末尾（离生成最近，注意力最高）；只进请求，不落库
FORMAT_REMINDER = (
    "\n（输出格式提醒：正文结束后必须输出状态栏 "
    "<book_status><item>属性名：<b>数值</b></item></book_status>，"
    "并用 <next_prompt> 包裹 A、/B、/C、 三个下一步选项。"
    "缺少状态栏或选项视为不合格回复。）"
)


def set_token_alpha(alpha) -> None:
    """注入当前 provider+model 的校准系数（夹在 [0.75, 1.6]）。"""
    global _ALPHA
    try:
        val = float(alpha)
    except (TypeError, ValueError):
        val = TOKEN_ALPHA_DEFAULT
    if val <= 0:
        val = TOKEN_ALPHA_DEFAULT
    _ALPHA = min(TOKEN_ALPHA_MAX, max(TOKEN_ALPHA_MIN, val))


def get_token_alpha() -> float:
    return _ALPHA


def _raw_tokens(text) -> float:
    """1.5×CJK + 0.25×其他（CJK 取 0x4E00–0x9FFF）。"""
    cjk = other = 0
    for ch in str(text or ""):
        if "\u4e00" <= ch <= "\u9fff":
            cjk += 1
        else:
            other += 1
    return cjk * TOKEN_CJK_RATIO + other * TOKEN_OTHER_RATIO


def count_tokens(text: str) -> int:
    """与 PromptBuilder 相同的 token 估算口径（供 prompt 日志 / 回归统计复用）。"""
    return int(math.ceil(_raw_tokens(text) * _ALPHA))


def estimate_messages(messages: Iterable[Dict], alpha: float | None = None) -> int:
    """整段 messages 的 token 估算：正文 × α + 4/消息（供 max_tokens 预算）。"""
    msgs = list(messages or [])
    a = _ALPHA if alpha is None else float(alpha)
    raw = sum(_raw_tokens((m or {}).get("content") if isinstance(m, dict) else m)
              for m in msgs)
    return int(math.ceil(raw * a + TOKEN_MSG_OVERHEAD * len(msgs)))


def _cg_words(v) -> List[str]:
    """标签 / 关键词统一成字符串列表（兼容库里偶发的字符串写法）。"""
    if isinstance(v, list):
        return [str(x) for x in v if x]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def sectionize(messages: Iterable[Dict]) -> List[Dict]:
    """把最终 messages 按 section 打标并统计 token（阶段 0 的对照基线）。

    id 只用于日志定位，不改变发送内容：persona / worldbook / status /
    history.* / current。hash 可用于跨轮比较固定前缀是否漂移。
    """
    msgs = list(messages or [])
    last_user = -1
    for i, m in enumerate(msgs):
        if (m or {}).get("role") == "user":
            last_user = i
    out: List[Dict] = []
    for i, m in enumerate(msgs):
        role = str((m or {}).get("role") or "")
        content = str((m or {}).get("content") or "")
        if role == "system":
            if "### 世界书" in content:
                sid = "worldbook"
            elif "### 当前状态面板" in content:
                sid = "status"
            elif i == 0:
                sid = "persona"
            else:
                sid = f"system.{i}"
        elif i == last_user:
            sid = "current"
        elif role == "assistant":
            sid = "history.assistant"
        else:
            sid = "history.user"
        out.append({
            "id": sid, "role": role, "index": i,
            "tokens": count_tokens(content),
            "hash": hashlib.sha1(content.encode("utf-8")).hexdigest()[:12],
        })
    return out


def prompt_prefix_hash(messages: Iterable[Dict]) -> str:
    """固定前缀 hash：拿第一条 system（角色核心设定/格式规范）做对照。"""
    msgs = list(messages or [])
    if not msgs:
        return ""
    first = msgs[0] if isinstance(msgs[0], dict) else {}
    if first.get("role") != "system":
        return ""
    return hashlib.sha1(str(first.get("content") or "").encode("utf-8")).hexdigest()[:16]


def section_tokens(messages: Iterable[Dict]) -> Dict[str, int]:
    """按 section 汇总 token（同 id 多段合并）。"""
    total: Dict[str, int] = {}
    for sec in sectionize(messages):
        total[sec["id"]] = total.get(sec["id"], 0) + int(sec["tokens"])
    return total


class PromptBuilder:
    """角色卡固定片段（CG 目录 / 状态面板）的构建器，供前缀与状态段复用。"""

    def __init__(self, character: Optional[Dict] = None,
                 prompt_data: Optional[Dict] = None,
                 cg_data: Optional[Dict] = None, *,
                 name: str = "角色",
                 has_payload: bool = True,
                 current_stats: Optional[Dict] = None,
                 db_rules_list=None):
        self.character = character or {}
        self.prompt_data = prompt_data if isinstance(prompt_data, dict) else {}
        self.cg_data = cg_data or {}
        self.name = name
        self.has_payload = has_payload
        # 当前状态数值 + 平台数值规则：构建时注入，档位匹配在这里一次性完成
        self.current_stats = dict(current_stats) if isinstance(current_stats, dict) else {}
        self.emotion_mapper = EmotionMapper(db_rules_list or {})

    # ---------------------------------------------------------- token 计数
    def _count_tokens(self, text: str) -> int:
        return count_tokens(text)

    # ---------------------------------------------------------- system
    def _build_cg_catalog(self) -> str:
        if not self.cg_data:
            return ""
        images = [c for c in (self.cg_data.get("images") or [])
                  if isinstance(c, dict) and c.get("isActive")]
        if not images:
            return ""
        catalog = ["### 视觉 CG 库（当剧情需要展示画面时，请直接输出 "
                   "`<image>ID</image>`，一次只能输出一张）"]
        first_id = ""
        for cg in images:
            pid = str(cg.get("id") or cg.get("imageId") or "").strip()
            if not pid:
                continue
            if not first_id:
                first_id = pid
            tags = (_cg_words(cg.get("sceneTags"))
                    + _cg_words(cg.get("actionTags"))
                    + _cg_words(cg.get("emotionTags")))
            desc = str(cg.get("description") or "")[:50]
            catalog.append(f"- ID: `{pid}` | 标签: {','.join(tags)} | 画面: {desc}...")
        if not first_id:
            return ""
        catalog.append(f"示例：`<image>{first_id}</image>`")
        return "\n".join(catalog)

    def _build_status_panel(self, status: Optional[Dict]) -> str:
        """当前状态面板 + 当前档位表现 + 世界书规则 + 输出格式。

        当前数值优先用调用方传入的 status["values"]，否则退回构建时注入的
        self.current_stats；当前档位表现由构建时注入的 EmotionMapper 按数据库
        规则匹配。放在动态 system 段而不是固定前缀里，避免破坏前缀缓存。
        """
        status = status if isinstance(status, dict) else {}
        values = status.get("values")
        if not isinstance(values, dict) or not values:
            values = self.current_stats
        if not values:
            # 兼容旧结构（items 数组）以及完全没有状态时的默认面板
            values = {str(it.get("name")): it.get("value")
                      for it in (status.get("items") or [])
                      if isinstance(it, dict) and it.get("name")}
        if not values:
            values = {"好感度": 0, "SAN值": 70}
        names: List[str] = [str(n) for n in values.keys()]
        panel = "\n".join(f"- {n}：{v}" for n, v in values.items())
        emotion = str(status.get("emotion") or "").strip() \
            or self.emotion_mapper.get_state_prompt(values)
        rule = str(status.get("rule") or "").strip() or DEFAULT_STATUS_RULE
        sample = "".join(f"<item>{n}：<b>新数字</b></item>" for n in names[:6])
        text = "### 当前状态面板\n" + panel
        if emotion:
            text += "\n\n### 当前档位表现（按数据库规则匹配）\n" + emotion
        text += ("\n\n### 状态变化规则（来自世界观）\n"
                 + rule
                 + "\n\n### 输出格式要求\n"
                 + "请在回复结尾，根据剧情对当前状态进行合理微调（幅度不超过 5），"
                   "并严格按此格式输出所有状态：\n"
                 + f"`<book_status>{sample}</book_status>`")
        return text
