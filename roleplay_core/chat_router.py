"""API 控制器：一次对话请求的编排。

它只按顺序调度各层，不写任何正则、提示词拼装或存储细节：
  载入上下文 → PromptBuilder 拼 messages → LLM 流式产出
  → 清洗 / 补齐格式 → post_processor 拆展示态 → 落库 → 推增量帧

数据库、模型客户端、SSE 通道都由 server 以端口（ChatPorts）注入，
本模块不 import server，避免循环依赖。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional

from prompt_builder import PromptBuilder
from roleplay_core.models import ChatContext
from roleplay_core.post_processor import parse_status, status_dict
from roleplay_core.sanitizer import (normalize_say_blocks, stream_hold_start,
                                     wrap_next_prompts)

# 流式分片节奏：上游常 1~5 字推一条，逐条转发会让前端每秒重绘几十次；
# 攒够 30 字或 150ms 再发一帧，帧数降到 1/10，文字仍然是连续冒出来的。
FLUSH_CHARS = 30
FLUSH_SECONDS = 0.15

ABORT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


@dataclass
class ChatRequest:
    """一次生成请求（已由 HTTP 层解析好，不含任何请求对象）。"""

    session_id: str
    character_id: str
    character_name: str
    user_input: str
    user_message_id: int = 0
    enable_aicg: Optional[bool] = None
    src: str = "chat-local"          # 本轮来源备注（chat-local / chat-llm:model）
    fail_on_empty: bool = False      # 模型空回复且不允许本地兜底 → 直接报错


@dataclass
class StreamDecision:
    """模型流出错后的处理决定。fatal=True → 报错收尾，不落库、不扣费。"""

    fatal: bool = False
    error: str = ""
    text: str = ""                                   # 可继续时用的兜底正文
    src: str = "chat-llm-failed"


@dataclass
class ChatTurn:
    """本轮编排结果，交给 HTTP 层拼收尾帧 / 计费。"""

    ok: bool
    src: str
    display_text: str = ""
    tagged_text: str = ""
    status: List[Dict[str, str]] = field(default_factory=list)
    options: List[str] = field(default_factory=list)
    has_image: bool = False
    images: List[str] = field(default_factory=list)   # 正文内嵌图片的真实 URL
    assistant_id: int = 0
    user_id: int = 0
    error: str = ""
    aborted: bool = False
    rewritten: bool = False  # 收尾改写越过了已下发部分 → 前端已收到重画指令


@dataclass
class ChatPorts:
    """server 注入的环境能力（DB / 模型 / SSE / 本地兜底）。"""

    load_context: Callable[[ChatRequest], ChatContext]
    stream_llm: Callable[[List[dict]], Iterator[str]]
    on_stream_error: Callable[[Exception, str, ChatRequest], StreamDecision]
    post_process: Callable[[str, ChatContext], Dict]
    save_assistant: Callable[[ChatContext, str, str], int]
    emit: Callable[[Dict], bool]          # 发一帧；返回 False 表示客户端已断开
    local_reply: Callable[[ChatRequest], str]
    prepare_messages: Optional[Callable[[ChatContext, List[dict]], List[dict]]] = None
    worldbook_match: Optional[Callable[[str, str], str]] = None
    ensure_format: Optional[Callable[[ChatContext, List[dict], str], str]] = None


class ChatRouter:
    """对话控制器：只做编排与顺序调用。"""

    def __init__(self, ports: ChatPorts):
        self.ports = ports
        self._last_status = ()

    # ------------------------------------------------------------ 组装
    @staticmethod
    def build_messages(ctx: ChatContext,
                       worldbook_match: Optional[Callable[[str, str], str]] = None,
                       prepare: Optional[Callable[[List[dict]], List[dict]]] = None,
                       ) -> List[dict]:
        """PromptBuilder 拼 messages；世界书匹配由调用方注入（核心层不碰 DB）。"""
        builder = PromptBuilder(
            character=ctx.character,
            prompt_data=ctx.prompt_data,
            cg_data=ctx.cg_data,
            name=ctx.character_name,
            has_payload=ctx.has_payload,
            current_stats=ctx.current_status.get("values") or {},
            db_rules_list=ctx.stat_rules,
        )
        matcher = None
        if worldbook_match:
            cid = ctx.character_id
            matcher = lambda text: worldbook_match(cid, text)
        messages = builder.build(ctx.llm_history(), ctx.user_input,
                                 worldbook_matcher=matcher,
                                 current_status=ctx.current_status)
        if prepare:
            messages = prepare(messages) or messages
        return messages

    def _messages(self, ctx: ChatContext) -> List[dict]:
        p = self.ports
        prepare = None
        if p.prepare_messages:
            prepare = lambda msgs: p.prepare_messages(ctx, msgs)
        return self.build_messages(ctx, p.worldbook_match, prepare)

    # ------------------------------------------------------------ 主流程
    def chat_stream(self, request: ChatRequest) -> ChatTurn:
        ctx = self.ports.load_context(request)
        messages = self._messages(ctx)

        raw, src, sent, abort, error = self._pump(messages, request, ctx)
        if abort == "fatal":
            return ChatTurn(ok=False, src=src, error=error,
                            user_id=request.user_message_id)
        # 收尾：先把攒着的部分刷掉，别让补格式请求拖慢首屏
        if not abort:
            cut = stream_hold_start(raw)
            if cut > sent:
                if self._emit_content(raw[sent:cut]):
                    sent = cut
                else:
                    abort, src = "client", "chat-stream-aborted"

        final = wrap_next_prompts(normalize_say_blocks(raw))
        if not final.strip():
            if request.fail_on_empty:
                return ChatTurn(ok=False, src=src, error="模型没有返回内容",
                                user_id=request.user_message_id)
            final = self.ports.local_reply(request)
        elif not abort and self.ports.ensure_format:
            # 模型漏状态栏/选项时补齐（用户中断不补，省一次调用）
            final = self.ports.ensure_format(ctx, messages, final) or final
        # 补齐后可能才出现状态栏：补发一次 status_update（有变化才发）
        if not abort:
            self._maybe_emit_status(final, ctx.stats_schema)

        out = self.ports.post_process(final, ctx)
        display = str(out.get("display_text") or "")
        tagged = str(out.get("tagged_text") or display)
        rewritten = False

        # 收尾补发：改写越过了已下发的半截内容时，让前端清屏后按最终文本重画
        if not abort:
            head = raw[:sent]
            if display.startswith(head):
                rest = display[sent:]
                if rest and not self._emit_content(rest):
                    abort, src = "client", "chat-stream-aborted"
            else:
                rewritten = True
                if not self._begin_rewrite(display):
                    abort, src = "client", "chat-stream-aborted"

        assistant_id = int(self.ports.save_assistant(ctx, display, tagged) or 0)
        return ChatTurn(
            ok=True, src=src,
            display_text=display, tagged_text=tagged,
            status=out.get("status") or [], options=out.get("options") or [],
            has_image=bool(out.get("has_image")),
            images=list(out.get("images") or []),
            assistant_id=assistant_id, user_id=request.user_message_id,
            aborted=bool(abort), rewritten=rewritten,
        )

    # ------------------------------------------------------------ 流式
    def _pump(self, messages: List[dict], request: ChatRequest,
              ctx: ChatContext):
        """把 LLM 流按边界攒帧下发。返回 (raw, src, sent, abort, error)。"""
        raw, sent, src = "", 0, request.src
        last_flush = time.time()
        stream = self.ports.stream_llm(messages)
        while True:
            try:
                piece = next(stream)
            except StopIteration:
                return raw, src, sent, None, ""
            except ABORT_ERRORS:
                return raw, "chat-stream-aborted", sent, "client", ""
            except Exception as exc:                     # noqa: BLE001
                decision = self.ports.on_stream_error(exc, raw, request)
                if decision.fatal:
                    return raw, decision.src, sent, "fatal", decision.error
                src = decision.src
                if decision.text:
                    raw = decision.text
                return raw, src, sent, None, ""
            raw += piece
            cut = stream_hold_start(raw)
            if cut > sent and (cut - sent >= FLUSH_CHARS
                               or time.time() - last_flush >= FLUSH_SECONDS):
                if not self._emit_content(raw[sent:cut]):
                    return raw, "chat-stream-aborted", sent, "client", ""
                sent = cut
                last_flush = time.time()
                self._maybe_emit_status(raw, ctx.stats_schema)

    def _emit_content(self, text: str) -> bool:
        return self.ports.emit({"type": "content", "content": text})

    def _maybe_emit_status(self, raw: str, schema: Optional[Dict] = None) -> None:
        """状态栏一闭合就把数值单独推给前端（前端只更新胶囊，不重绘正文）。"""
        items = parse_status(raw, schema)
        if not items:
            return
        key = tuple((str(i.get("name")), str(i.get("value"))) for i in items)
        if key == self._last_status:
            return
        self._last_status = key
        self.ports.emit({"type": "status_update", "items": items,
                         "values": status_dict(items)})

    def _begin_rewrite(self, display: str) -> bool:
        """清空前端已渲染的分片，再用最终文本重画。"""
        if not self.ports.emit({"type": "clear_content"}):
            return False
        return self.ports.emit({"type": "content", "content": display})
