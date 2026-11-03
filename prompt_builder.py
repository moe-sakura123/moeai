"""带 Token 预算的 Prompt 组装器（借鉴 RisuAI sendChat 流水线）。

把 server.Handler._llm_messages 里「拼提示词」的部分独立出来：
系统提示词分层拼装 → 历史按 Token 预算滑窗裁剪 → 世界书注入 → 格式规范收尾。

注意：chat_history 里的 assistant 内容应已是落库时的纯文本（content_llm 列），
本模块不再做标签清洗。
"""
from __future__ import annotations

import sys
from typing import Callable, Dict, Iterable, List, Optional

from roleplay_core.emotion_mapper import EmotionMapper
from roleplay_core.sanitizer import clean_description_html

ENCODER_NAME = "cl100k_base"
# 历史滑窗预算（只算对话历史，不含 system / 当前消息 / 世界书）
HISTORY_TOKEN_BUDGET = 2000

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

_ENCODER = None
_ENCODER_READY = False


def _get_encoder():
    """懒加载 tiktoken 编码器；装不上时返回 None（退回字符估算）。"""
    global _ENCODER, _ENCODER_READY
    if not _ENCODER_READY:
        _ENCODER_READY = True
        try:
            import tiktoken
            _ENCODER = tiktoken.get_encoding(ENCODER_NAME)
        except Exception as exc:                       # noqa: BLE001
            print(f"[prompt] tiktoken 不可用，退回字符估算：{exc}", file=sys.stderr)
            _ENCODER = None
    return _ENCODER


def _cg_words(v) -> List[str]:
    """标签 / 关键词统一成字符串列表（兼容库里偶发的字符串写法）。"""
    if isinstance(v, list):
        return [str(x) for x in v if x]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


class PromptBuilder:
    """按 Token 预算组装最终发给 LLM 的 messages。"""

    def __init__(self, character: Optional[Dict] = None,
                 prompt_data: Optional[Dict] = None,
                 cg_data: Optional[Dict] = None, *,
                 name: str = "角色",
                 has_payload: bool = True,
                 history_budget: int = HISTORY_TOKEN_BUDGET,
                 current_stats: Optional[Dict] = None,
                 db_rules_list=None):
        self.character = character or {}
        self.prompt_data = prompt_data if isinstance(prompt_data, dict) else {}
        self.cg_data = cg_data or {}
        self.name = name
        self.has_payload = has_payload
        self.history_budget = max(0, int(history_budget or 0))
        # 当前状态数值 + 平台数值规则：构建时注入，档位匹配在这里一次性完成
        self.current_stats = dict(current_stats) if isinstance(current_stats, dict) else {}
        self.emotion_mapper = EmotionMapper(db_rules_list or {})

    # ---------------------------------------------------------- token 计数
    def _count_tokens(self, text: str) -> int:
        text = str(text or "")
        enc = _get_encoder()
        if enc is not None:
            return len(enc.encode(text))
        return len(text)          # 退化估算：中文约 1 字 1 token

    def _trim_history(self, chat_history: Iterable[Dict]) -> List[Dict]:
        """从最新往前装，装不下的更早消息直接丢弃（滑动窗口）。"""
        kept: List[Dict] = []
        used = 0
        for msg in reversed(list(chat_history or [])):
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            role = "user" if msg.get("role") == "user" else "assistant"
            tokens = self._count_tokens(content)
            if kept and used + tokens > self.history_budget:
                break
            # 第一条（最新的）即使超预算也保留，否则本轮就只剩 system
            kept.insert(0, {"role": role, "content": content})
            used += tokens
            if used >= self.history_budget:
                break
        return kept

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

    def _build_system_prompt(self) -> str:
        """固定前缀：人设 → 身份 / 世界观 / 风格 → CG 目录 → 剧情选项 → 破甲词 → 格式。

        这里不放世界书等每轮变化的内容，保证同一角色多轮对话时前缀稳定、
        可以直接命中上游的 prefix cache。
        """
        prompt = self.prompt_data
        ch = self.character

        sys_prompt = str(prompt.get("systemPrompt") or "").strip()
        if not sys_prompt:                       # 主提示词为空 → 退回角色详细介绍
            sys_prompt = clean_description_html(ch.get("description") or "")
        ai_identity = str(prompt.get("aiIdentityStylePrompt") or "").strip()
        world_view = str(prompt.get("worldViewPrompt") or "").strip()
        style_prompt = str(prompt.get("writingStylePrompt") or "").strip()

        parts: List[str] = []
        if sys_prompt:
            parts.append("### 角色核心设定\n" + sys_prompt)
        # 去重：身份设定已经写在主提示词里就不再重复拼
        if ai_identity and ai_identity not in sys_prompt:
            parts.append("### 身份与任务\n" + ai_identity)
        if world_view:
            parts.append("### 世界观背景\n" + world_view)
        if style_prompt:
            parts.append("### 写作风格要求\n" + style_prompt)
        catalog = self._build_cg_catalog()
        if catalog:
            parts.append(catalog)
        # 剧情选项：postPrompt 为空（自定义关）→ 用系统默认模板
        post = (str(prompt.get("postPrompt") or "").strip()
                or NEXT_PROMPT_DEFAULT)
        parts.append("### 剧情选项要求\n" + post)
        # 破甲词：仅自定义填了才拼
        bk = str(prompt.get("breakArmorWord") or "").strip()
        if bk:
            parts.append("### 破甲词\n" + bk)

        system = "\n\n".join(parts)
        if not self.has_payload:                 # 拿不到角色整包（如镜像角色）→ 通用兜底
            system = (f"### 角色核心设定\n你是{self.name}，保持这个角色的设定、语气与"
                      "故事背景，用自然的中文推进对话。\n\n" + system)
        # 格式规范放在最后（离模型最近），避免被前面的剧情冲淡
        system += "\n\n" + FORMAT_RULES + "\n\n" + FORMAT_EXAMPLE
        return system

    def _match_lorebook(self, context: str,
                        matcher: Optional[Callable[[str], str]]) -> str:
        """世界书按语境匹配；匹配器由调用方注入（核心层不碰 DB）。"""
        if not matcher:
            return ""
        return str(matcher(context) or "").strip()

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

    def build(self, chat_history: Iterable[Dict], current_message: str,
              worldbook_matcher: Optional[Callable[[str], str]] = None,
              current_status: Optional[Dict] = None) -> List[Dict]:
        """system 前缀 → 预算内历史 → 世界书(system) → 状态面板(system) → 当前用户消息。

        世界书 / 状态面板单独成 system 消息插在历史之后：前面的角色/格式前缀
        每轮完全不变，上游可以稳定命中 prefix cache；只有这两段随语境变化。
        """
        history = self._trim_history(chat_history)

        tail = None
        if history and history[-1]["role"] == "user":
            tail = history.pop()               # 最新一句 user 永远排最后
        elif str(current_message or "").strip():
            tail = {"role": "user", "content": str(current_message).strip()}

        ctx = "\n".join([m["content"] for m in history]
                        + ([tail["content"]] if tail else []))
        lorebook = self._match_lorebook(ctx, worldbook_matcher)

        messages: List[Dict] = [{"role": "system",
                                 "content": self._build_system_prompt()}]
        messages.extend(history)
        if lorebook:
            messages.append({"role": "system",
                             "content": "### 世界书\n" + lorebook})
        # 状态面板每轮都注入：历史里没有状态，模型只能靠这段知道当前数值
        messages.append({"role": "system",
                         "content": self._build_status_panel(current_status)})
        if tail:
            # 格式提醒贴在本轮用户消息里：模型最容易漏状态栏/选项，靠最近位置兜住
            tail["content"] = str(tail["content"]).rstrip() + FORMAT_REMINDER
            messages.append(tail)
        return messages
