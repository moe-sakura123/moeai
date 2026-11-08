#!/usr/bin/env python3
"""
llm.py — OpenAI 协议对接（传输层用 httpx，同步客户端）

三条请求，全部按 OpenAI 官方协议：
    GET  {base_url}/models            拉模型列表（控制台「模型接入」检测用）
    POST {base_url}/chat/completions  对话（stream=false，一次拿全）
    POST {base_url}/chat/completions  对话（stream=true，逐段拿，chat_stream）

base_url 填官方 https://api.openai.com/v1，或任何兼容这套协议的中转站；
密钥与地址由 server.py 从数据库 llm_providers 取出后传进来。
日后接阿里百炼 / DeepSeek / 火山方舟，只要换 base_url（协议相同）。
"""

import json
import os
import random
import sys
import threading
import time
from email.utils import parsedate_to_datetime

import httpx

UA = "missai-local/1.0"

# 超时三段：连接 3s（含连接池等待 pool）/ 首字节 30s（流式 60s）/ 空闲 120s。
# httpx 的 read 是"单次读"超时：请求发出时先用首字节阈值（覆盖握手后等响应头），
# 拿到响应头后把同一次请求的 read 改成空闲阈值，再用 watchdog 兜住"第一帧"上限。
# 连接阶段单独收紧：不可达地址/坏链路快速失败，不把首字节预算耗在拨号上。
CONNECT_TIMEOUT = 3.0
POOL_TIMEOUT = 3.0
WRITE_TIMEOUT = 10.0
FIRST_BYTE_TIMEOUT = 30.0
FIRST_BYTE_TIMEOUT_STREAM = 60.0
IDLE_TIMEOUT = 120.0

# 重试：总尝试 4 次；连接建立阶段失败最多尝试 2 次（WriteTimeout 仍按 4 次）。
# 等待 1/2/4 秒 + 0~25% 抖动；Retry-After 封顶 30s。
MAX_ATTEMPTS = 4
CONNECT_MAX_ATTEMPTS = 2
RETRYABLE_STATUS = (408, 409, 425, 429)
RETRY_AFTER_CAP = 30.0

# SSE 坏帧容忍：帧数 >= 20 且坏帧占比 > 5% 视为上游流损坏
BAD_FRAME_MIN_TOTAL = 20
BAD_FRAME_RATIO = 0.05

# 上下文超限：上游 400/413 或流内 error 里出现这些字样 → 走降级，不重试原 payload
OVERFLOW_MARKERS = (
    "context_length_exceeded", "maximum context length", "context window",
    "too many tokens", "reduce the length", "prompt is too long",
    "input is too long", "exceeds the maximum",
)

# 正在读取的流式响应：用户点停止时由另一个线程直接 close()，
# 让阻塞在网络读取上的循环立刻退出（而不是等下一段数据到达）。
_ACTIVE_RESP: dict[str, object] = {}
_ACTIVE_RESP_LOCK = threading.Lock()


def close_stream(key: str) -> bool:
    """关闭指定 key 的流式响应；返回是否命中。"""
    with _ACTIVE_RESP_LOCK:
        resp = _ACTIVE_RESP.get(str(key or ""))
    if resp is None:
        return False
    try:
        resp.close()
    except Exception:                             # noqa: BLE001
        pass
    return True

# 控制台「聊天模型」只检测对话模型：模型名里带这些词的（绘图 / 视频 / 向量 / 语音等）一律跳过。
# 注意 vision / omni 这类多模态对话模型不算，仍然保留。
NON_CHAT_HINTS = (
    "image", "img-", "imagen", "video", "sora", "veo", "kling", "seedance",
    "wan2", "wanx", "t2v", "i2v", "s2v", "hailuo", "runway", "luma", "pika",
    "vidu", "dreamina", "jimeng", "embedding", "embed-", "rerank", "audio",
    "tts", "asr", "speech", "ocr", "dall-e", "midjourney", "flux", "sdxl",
)


class LLMError(RuntimeError):
    """对接失败的可读原因（控制台与日志直接显示）。

    额外带上状态码 / 错误码 / 是否可重试 / Retry-After，供退避与降级判断；
    str(exc) 仍是给人看的中文原因，老的字符串匹配调用方不受影响。
    """

    def __init__(self, message, status: int = 0, code: str = "",
                 retryable: bool | None = None, retry_after: float = 0.0,
                 connect_failed: bool = False):
        super().__init__(message)
        self.status = int(status or 0)
        self.code = str(code or "")
        if retryable is None:                     # 默认按状态码分类
            retryable = self.status >= 500 or self.status in RETRYABLE_STATUS
        self.retryable = bool(retryable)
        self.retry_after = max(0.0, float(retry_after or 0.0))
        # 仅标记"连接建立阶段"的失败，用于单独压低重试次数；不影响原有分类
        self.connect_failed = bool(connect_failed)


def _retry_after_seconds(headers) -> float:
    """Retry-After：支持秒数或 HTTP-date，统一封顶 RETRY_AFTER_CAP。"""
    if headers is None:
        return 0.0
    try:
        raw = str(headers.get("Retry-After") or "").strip()
    except Exception:                             # noqa: BLE001
        return 0.0
    if not raw:
        return 0.0
    try:
        return min(RETRY_AFTER_CAP, max(0.0, float(raw)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        return min(RETRY_AFTER_CAP, max(0.0, when.timestamp() - time.time()))
    except Exception:                             # noqa: BLE001  解析失败就当没给
        return 0.0


def _retry_delay(attempt: int, retry_after: float = 0.0) -> float:
    """第 attempt 次失败后的等待：Retry-After 优先；否则 1/2/4s + 0~25% 抖动。"""
    if retry_after and retry_after > 0:
        return min(RETRY_AFTER_CAP, float(retry_after))
    base = float(2 ** (max(1, int(attempt)) - 1))
    return base + random.uniform(0.0, 0.25 * base)


def _is_context_overflow(text) -> bool:
    """上游报错是否属于"上下文超出模型上限"（这类错误不能原样重试）。"""
    low = str(text or "").lower()
    return any(k in low for k in OVERFLOW_MARKERS)


def _endpoint(base_url: str, tail: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise LLMError("API 地址为空")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    return f"{base}/{tail.lstrip('/')}"


def _notify_sent(on_send, url: str, payload, stream: bool) -> None:
    """真正发包前的观察回调：把这一刻的 URL 与完整 body 原样交给调用方。"""
    if on_send is None or payload is None:
        return
    try:
        on_send({"url": url, "stream": stream, "payload": payload})
    except Exception:                             # noqa: BLE001  观察器出错不影响请求
        pass


# 同步客户端：连接池 / 重定向 / 环境代理 / TLS / gzip 解压都交给 httpx。
# read 默认不设，由每次请求按"首字节 → 空闲"两段自己改。
#
# IPv4 优先：本机网络存在 IPv6 黑洞，系统解析会把上游的 AAAA 记录排在 A 记录之前，
# httpx 逐个地址拨号会白等多个 connect 超时。显式绑定 IPv4 源地址后，AF_INET6
# 候选会立即失败并被跳过，只走 IPv4。
# 注意：httpx 在显式传 transport 时会关闭环境代理（allow_env_proxies=False），
# 所以检测到 HTTP(S)_PROXY 等配置时保留默认链路，并打印一条降级说明。
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                   "http_proxy", "https_proxy", "all_proxy")
_PROXY_CONFIGURED = any(os.environ.get(k) for k in _PROXY_ENV_KEYS)
_TRANSPORT = None
if _PROXY_CONFIGURED:
    print("[llm] 检测到环境代理配置，跳过 IPv4 强制，沿用 httpx 默认代理链路",
          file=sys.stderr, flush=True)
else:
    _TRANSPORT = httpx.HTTPTransport(local_address="0.0.0.0")
_CLIENT = httpx.Client(
    transport=_TRANSPORT,
    follow_redirects=True,
    trust_env=True,
    timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=None,
                          write=WRITE_TIMEOUT, pool=POOL_TIMEOUT),
)


class _FirstByteGuard:
    """发出请求后 n 秒内没看到首个数据块 → 主动关闭响应，中断阻塞读。

    httpx 的 read 超时按单次读计，无法区分"首字节"和"空闲"；拿到响应头后
    把 read 改成空闲阈值，首帧上限就由这个 watchdog 兜住。
    """

    def __init__(self, resp, seconds: float):
        self.resp = resp
        self.seconds = max(0.0, float(seconds))
        self.seen = threading.Event()
        self.fired = False
        self._started = False
        self._timer = threading.Timer(self.seconds, self._fire)
        self._timer.daemon = True

    def _fire(self) -> None:
        if self.seen.is_set():
            return
        self.fired = True
        try:
            self.resp.close()                     # 从另一个线程关闭，唤醒阻塞读
        except Exception:                          # noqa: BLE001
            pass

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._timer.start()

    def mark(self) -> None:
        self.seen.set()

    def cancel(self) -> None:
        self.seen.set()
        self._timer.cancel()

    def timed_out(self) -> bool:
        return self.fired and not self.seen.is_set()


def _build_request(method: str, url: str, api_key: str, data, accept: str,
                   first_timeout: float) -> httpx.Request:
    headers = {"User-Agent": UA, "Accept": accept}
    if data is not None:
        headers["Content-Type"] = "application/json"
    key = str(api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return _CLIENT.build_request(
        method, url, headers=headers, content=data,
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=float(first_timeout),
                              write=WRITE_TIMEOUT, pool=POOL_TIMEOUT))


def _switch_idle(request: httpx.Request) -> None:
    """响应头已到：把同一次请求后续读的超时改成空闲阈值（必须在首读之前改）。"""
    ext = request.extensions.get("timeout")
    if isinstance(ext, dict):
        ext["read"] = IDLE_TIMEOUT


def _remaining_first(started: float, first_timeout: float) -> float:
    return max(0.0, float(first_timeout) - (time.monotonic() - started))


def _send_error(exc: Exception, url: str) -> LLMError:
    if isinstance(exc, httpx.PoolTimeout):
        return LLMError(f"连接池等待超时（{POOL_TIMEOUT:.0f}s）：{url}",
                        code="timeout", retryable=True, connect_failed=True)
    if isinstance(exc, httpx.ConnectTimeout):
        return LLMError(f"连接超时（{CONNECT_TIMEOUT:.0f}s）：{url}",
                        code="timeout", retryable=True, connect_failed=True)
    if isinstance(exc, httpx.WriteTimeout):
        return LLMError(f"发送超时（{WRITE_TIMEOUT:.0f}s）：{url}",
                        code="timeout", retryable=True)
    if isinstance(exc, httpx.TimeoutException):
        return LLMError(f"请求超时：{url}", code="timeout", retryable=True)
    return LLMError(f"连不上 {url}：{exc}", code="network", retryable=True,
                    connect_failed=True)


def _read_body(resp: httpx.Response, request: httpx.Request,
               guard: _FirstByteGuard, url: str,
               first_timeout: float) -> bytes:
    """按空闲阈值整段读完；首字节仍受 guard 约束。"""
    _switch_idle(request)
    guard.start()
    chunks: list[bytes] = []
    try:
        for chunk in resp.iter_bytes():
            guard.mark()
            chunks.append(chunk)
    except httpx.TimeoutException as exc:
        if guard.timed_out():
            raise LLMError(f"请求超时（{first_timeout:.0f}s 内未收到首字节）：{url}",
                           code="timeout", retryable=True) from None
        raise LLMError(f"读取超时（空闲 {IDLE_TIMEOUT:.0f}s）：{url}",
                       code="timeout", retryable=True) from exc
    except (httpx.HTTPError, OSError, ValueError) as exc:
        if guard.timed_out():
            raise LLMError(f"请求超时（{first_timeout:.0f}s 内未收到首字节）：{url}",
                           code="timeout", retryable=True) from None
        raise LLMError(f"连接中断 {url}：{exc}",
                       code="network", retryable=True) from exc
    finally:
        guard.cancel()
    return b"".join(chunks)


def _request(url: str, api_key: str = "", payload=None, timeout: float = 60.0,
             on_send=None) -> dict:
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    _notify_sent(on_send, url, payload, False)
    started = time.monotonic()
    request = _build_request(method, url, api_key, data,
                             "application/json", timeout)
    try:
        resp = _CLIENT.send(request, stream=True)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise _send_error(exc, url) from None
    try:
        if resp.status_code >= 400:                    # 4xx/5xx：把上游的说明带回来
            detail = ""
            try:
                guard = _FirstByteGuard(
                    resp, _remaining_first(started, timeout))
                body = _read_body(resp, request, guard, url, timeout)[:400]
                detail = body.decode("utf-8", "replace")
            except Exception:                          # noqa: BLE001
                detail = ""
            raise LLMError(f"HTTP {resp.status_code}：{detail or url}",
                           status=resp.status_code,
                           code=("context_overflow"
                                 if _is_context_overflow(detail) else ""),
                           retry_after=_retry_after_seconds(resp.headers))
        guard = _FirstByteGuard(resp, _remaining_first(started, timeout))
        body = _read_body(resp, request, guard, url, timeout)
    finally:
        try:
            resp.close()
        except Exception:                              # noqa: BLE001
            pass
    try:
        obj = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise LLMError(f"回包不是 JSON：{body[:200]!r}") from None
    if isinstance(obj, dict) and obj.get("error"):
        err = obj["error"]
        msg = err.get("message") if isinstance(err, dict) else err
        raise LLMError(f"上游报错：{msg}")
    return obj if isinstance(obj, dict) else {}


def _request_with_retry(url: str, api_key: str = "", payload=None,
                        timeout: float = FIRST_BYTE_TIMEOUT, on_send=None,
                        attempts: int = MAX_ATTEMPTS) -> dict:
    """非流式请求：按状态码分类重试，总尝试 attempts 次。

    可重试：429 / 408 / 409 / 425 / 5xx / 网络错误 / 超时；
    不可重试：400 / 401 / 403 / 404 等其余 4xx（密钥或参数错了重试也没用）。
    """
    total = max(1, int(attempts or 1))
    last: LLMError | None = None
    for attempt in range(1, total + 1):
        try:
            return _request(url, api_key, payload, timeout, on_send=on_send)
        except LLMError as exc:
            last = exc
            if not exc.retryable or attempt >= total:
                raise
            time.sleep(_retry_delay(attempt, exc.retry_after))
    raise last                                    # pragma: no cover


def _with_v1_fallback(base_url: str, tail: str, api_key: str, payload,
                      timeout: float, on_send=None,
                      attempts: int = MAX_ATTEMPTS) -> dict:
    """地址没写 /v1 但上游要 /v1 → 404 时自动补一次（404 本身不参与退避）"""
    try:
        return _request_with_retry(_endpoint(base_url, tail), api_key, payload,
                                   timeout, on_send=on_send, attempts=attempts)
    except LLMError as exc:
        base = str(base_url or "").strip().rstrip("/")
        if exc.status != 404 or "/v1/" in f"{base}/":
            raise
        return _request_with_retry(f"{base}/v1/{tail.lstrip('/')}", api_key,
                                   payload, timeout, on_send=on_send,
                                   attempts=attempts)


def list_models(base_url: str, api_key: str, timeout: float = 60.0) -> list[str]:
    """检测连通性 + 拿模型列表（中转站返回几个，这里就有几个）"""
    obj = _with_v1_fallback(base_url, "models", api_key, None, timeout)
    data = obj.get("data")
    if data is None:
        data = obj.get("models")
    if not isinstance(data, list):
        raise LLMError(f"回包里没有模型列表：{str(obj)[:200]}")
    out: list[str] = []
    for item in data:
        if isinstance(item, dict):
            mid = item.get("id") or item.get("model") or item.get("name")
        else:
            mid = item
        mid = str(mid or "").strip()
        if mid and mid not in out:
            out.append(mid)
    if not out:
        raise LLMError("上游返回了空的模型列表")
    # 只留聊天模型：过滤掉绘图 / 视频 / 向量 / 语音等非对话模型
    chat = [m for m in out if not any(h in m.lower() for h in NON_CHAT_HINTS)]
    return chat or out                 # 一个都没剩下时保留全部，避免接入直接不可用


def _chat_payload(model: str, messages: list[dict], stream: bool,
                  temperature: float | None, max_tokens: int | None,
                  extra: dict, profile: dict | None = None) -> dict:
    """按 provider profile 组装 payload（兼容 max_completion_tokens / stop 等差异）。"""
    prof = profile if isinstance(profile, dict) else {}
    payload: dict = {"model": model, "messages": messages, "stream": stream}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        field = str(prof.get("max_tokens_field") or "max_tokens")
        payload[field] = int(max_tokens)
    stop = prof.get("stop")
    if stop:
        payload["stop"] = stop if isinstance(stop, list) else [stop]
    # 覆盖顺序（有意为之）：profile.extra 在 temperature / max_tokens 之后应用，
    # 因此接入级配置优先；随后 extra（调用方参数，如 top_p / 惩罚）再覆盖 profile.extra。
    if isinstance(prof.get("extra"), dict):
        payload.update(prof["extra"])
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _content_text(content) -> str:
    """message.content / delta.content —— 兼容新式分块 [{type,text},…]"""
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def chat_with_meta(base_url: str, api_key: str, model: str, messages: list[dict],
                   timeout: float = 90.0, temperature: float | None = 0.8,
                   max_tokens: int | None = None, on_send=None,
                   profile: dict | None = None,
                   **extra) -> tuple[str, dict]:
    """一次对话（非流式），返回 (模型文本, 元信息)。

    元信息至少带 finish_reason（stop / length / content_filter …），
    调用方可以据此判断是否被截断，而不是把半截回复当成功。
    """
    if not model:
        raise LLMError("没有指定模型")
    payload = _chat_payload(model, messages, False, temperature, max_tokens,
                            extra, profile)
    obj = _with_v1_fallback(base_url, "chat/completions", api_key, payload, timeout,
                            on_send=on_send)
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError(f"回包里没有 choices：{str(obj)[:200]}")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = _content_text(message.get("content"))
    if not content.strip():
        content = first.get("text")
    content = str(content or "").strip()
    meta = {
        "finish_reason": str(first.get("finish_reason") or ""),
        "usage": obj.get("usage") if isinstance(obj.get("usage"), dict) else {},
        # 推理模型把思维链放这里；内容仍以 content 为准，不把思考过程当正文
        "reasoning": _content_text(message.get("reasoning_content")),
    }
    if not content:
        raise LLMError("模型没有返回文本内容")
    if meta["finish_reason"] == "content_filter":
        raise LLMError("模型输出被内容审核拦截（content_filter）",
                       code="content_filter", retryable=False)
    return content, meta


def chat(base_url: str, api_key: str, model: str, messages: list[dict],
         timeout: float = 90.0, temperature: float | None = 0.8,
         max_tokens: int | None = None, on_send=None,
         profile: dict | None = None, **extra) -> str:
    """一次对话（非流式），返回模型文本"""
    return chat_with_meta(base_url, api_key, model, messages, timeout=timeout,
                          temperature=temperature, max_tokens=max_tokens,
                          on_send=on_send, profile=profile, **extra)[0]


def embeddings(base_url: str, api_key: str, model: str, texts,
               dimensions: int = 0, timeout: float = 60.0,
               batch_size: int = 16, retries: int = 2) -> list[list[float]]:
    """OpenAI 兼容 /v1/embeddings：分批请求 + 429/5xx/超时指数退避重试。

    返回与输入同序的原始向量（L2 归一化与落库由调用方做）。
    任何非可重试错误直接抛 LLMError，由上层回落哈希嵌入。
    """
    items = [str(t or "") for t in (texts or [])]
    if not items:
        return []
    size = max(1, int(batch_size or 16))
    out: list[list[float]] = []
    for start in range(0, len(items), size):
        batch = items[start:start + size]
        payload = {"model": model, "input": batch}
        if int(dimensions or 0) > 0:
            payload["dimensions"] = int(dimensions)
        attempt = 0
        while True:
            try:
                obj = _with_v1_fallback(base_url, "embeddings", api_key,
                                        payload, timeout, attempts=1)
                break
            except LLMError as exc:
                if attempt >= max(0, int(retries)) or not exc.retryable:
                    raise
                # 限流退避：Retry-After 优先，其余按 1/2/4s 曲线，单次不超过 8s
                time.sleep(min(8.0, _retry_delay(attempt + 1, exc.retry_after)))
                attempt += 1
        data = obj.get("data")
        if not isinstance(data, list):
            raise LLMError("embedding 回包缺少 data 数组")
        got: list[list[float]] = []
        for i, rec in enumerate(data):
            if not isinstance(rec, dict) or not isinstance(rec.get("embedding"), list):
                continue
            got.append((int(rec.get("index", i)), [float(x) for x in rec["embedding"]]))
        got.sort(key=lambda x: x[0])
        if len(got) != len(batch):
            raise LLMError(f"embedding 条数不匹配：要 {len(batch)} 条，回 {len(got)} 条")
        out.extend(v for _, v in got)
    return out


def _iter_sse_frames(resp, should_stop=None):
    """按 SSE 规范把响应切成一帧一帧的 data 文本。

    · 空行结束一帧（\\n 或 \\r\\n 之后再来一个换行）；
    · 同一帧内多条 data: 用 \\n 拼接；
    · event: / id: / retry: 以及以 : 开头的注释行忽略；
    · 流在 EOF 时若还有未结束的 data，按最后一帧冲出来（很多中转站不补空行）。
    """
    data: list[str] = []
    for raw in resp.iter_lines():
        if should_stop and should_stop():
            return
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if line.endswith("\r\n"):
            line = line[:-2]
        elif line.endswith(("\n", "\r")):
            line = line[:-1]
        if not line:                              # 空行 = 帧结束
            if data:
                yield "\n".join(data)
                data = []
            continue
        if line.startswith(":"):                  # 注释 / 心跳
            continue
        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "data":
            data.append(value)
    if data:
        yield "\n".join(data)


def _stream_once(url: str, api_key: str, payload: dict, timeout: float,
                 on_send=None, should_stop=None, on_finish=None, stream_key=None):
    """连一次上游，逐段 yield 文本增量（生成器）。

    上游若无视 stream 直接回整包 JSON（不少中转站会这样），也照样解析出来。
    should_stop：用户点停止时返回 True → 立即停止读取并关闭上游连接；
    on_finish：收尾回调，带回 finish_reason / stopped / usage / reasoning_chars。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    _notify_sent(on_send, url, payload, True)
    first_timeout = float(timeout or FIRST_BYTE_TIMEOUT_STREAM)
    started = time.monotonic()
    request = _build_request("POST", url, api_key, data,
                             "text/event-stream", first_timeout)
    try:
        resp = _CLIENT.send(request, stream=True)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise _send_error(exc, url) from None
    if resp.status_code >= 400:                        # 4xx/5xx：把上游的说明带回来
        detail = ""
        try:
            body = _read_body(resp, request,
                              _FirstByteGuard(
                                  resp, _remaining_first(started, first_timeout)),
                              url, first_timeout)[:400]
            detail = body.decode("utf-8", "replace")
        except Exception:                              # noqa: BLE001
            detail = ""
        try:
            resp.close()
        except Exception:                              # noqa: BLE001
            pass
        raise LLMError(f"HTTP {resp.status_code}：{detail or url}",
                       status=resp.status_code,
                       code=("context_overflow"
                             if _is_context_overflow(detail) else ""),
                       retry_after=_retry_after_seconds(resp.headers))

    guard = _FirstByteGuard(resp, _remaining_first(started, first_timeout))
    _switch_idle(request)                              # 首帧之后按空闲阈值计
    guard.start()                                      # 首帧上限由 watchdog 兜住
    if stream_key:
        with _ACTIVE_RESP_LOCK:
            _ACTIVE_RESP[str(stream_key)] = resp

    got = False
    finish = ""
    stopped = False
    reasoning_chars = 0
    usage: dict = {}
    frames = 0
    bad_frames = 0
    try:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "event-stream" not in ctype and "json" in ctype:
            raw = _read_body(resp, request, guard, url, first_timeout)  # 整包 JSON
            try:
                obj = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                raise LLMError(f"回包不是 JSON：{raw[:200]!r}") from None
            if isinstance(obj, dict) and obj.get("error"):
                err = obj["error"]
                msg = err.get("message") if isinstance(err, dict) else err
                code = str(err.get("code") or "") if isinstance(err, dict) else ""
                if _is_context_overflow(f"{code} {msg}"):
                    code = "context_overflow"
                raise LLMError(f"上游报错：{msg}", code=code,
                               retryable=False if code == "context_overflow" else None)
            choices = obj.get("choices") if isinstance(obj, dict) else None
            first = choices[0] if isinstance(choices, list) and choices else {}
            first = first if isinstance(first, dict) else {}
            finish = str(first.get("finish_reason") or "")
            if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
            msg = first.get("message") if isinstance(first.get("message"), dict) else {}
            text = _content_text(msg.get("content")) or str(first.get("text") or "")
            if not text:
                raise LLMError("模型没有返回文本内容", code="empty", retryable=True)
            got = True
            yield text
            return
        for frame in _iter_sse_frames(resp, should_stop):
            guard.mark()                               # 已收到首个 SSE 帧
            if should_stop and should_stop():
                stopped = True
                break
            text = frame.strip()
            if not text:
                continue
            if text == "[DONE]":
                break
            frames += 1
            try:
                obj = json.loads(text)
            except ValueError:
                bad_frames += 1                    # 半截 JSON / 心跳乱码：先记数
                continue
            if not isinstance(obj, dict):
                bad_frames += 1
                continue
            if isinstance(obj.get("usage"), dict) and obj["usage"]:
                usage = obj["usage"]               # include_usage 的末帧
            if obj.get("error"):
                err = obj["error"]
                msg = err.get("message") if isinstance(err, dict) else err
                code = str(err.get("code") or "") if isinstance(err, dict) else ""
                if _is_context_overflow(f"{code} {msg}"):
                    code = "context_overflow"
                raise LLMError(f"上游报错：{msg}", code=code,
                               retryable=False if code == "context_overflow" else None)
            choices = obj.get("choices")
            first = choices[0] if isinstance(choices, list) and choices else {}
            first = first if isinstance(first, dict) else {}
            if first.get("finish_reason"):
                finish = str(first.get("finish_reason"))
            delta = first.get("delta")
            if isinstance(delta, dict):
                reasoning_chars += len(_content_text(delta.get("reasoning_content")))
            piece = _content_text(delta.get("content")) if isinstance(delta, dict) else ""
            if not piece and not delta:
                msg = first.get("message")
                piece = _content_text(msg.get("content")) if isinstance(msg, dict) else ""
                if not piece:
                    piece = str(first.get("text") or "")
            if piece:
                got = True
                yield piece
        if frames >= BAD_FRAME_MIN_TOTAL and bad_frames > frames * BAD_FRAME_RATIO:
            raise LLMError(f"上游流损坏：{bad_frames}/{frames} 帧无法解析",
                           code="bad_stream", retryable=True)
        if finish == "content_filter":
            raise LLMError("模型输出被内容审核拦截（content_filter）",
                           code="content_filter", retryable=False)
    except LLMError:
        raise
    except httpx.TimeoutException as exc:
        if should_stop and should_stop():
            stopped = True
            return
        if guard.timed_out() or not guard.seen.is_set():
            raise LLMError(f"流式首帧超时（{first_timeout:.0f}s）：{url}",
                           code="timeout", retryable=True) from exc
        raise LLMError(f"流式空闲超时（{IDLE_TIMEOUT:.0f}s）：{url}",
                       code="idle_timeout", retryable=True) from exc
    except (httpx.HTTPError, OSError, ValueError) as exc:
        if should_stop and should_stop():
            stopped = True
            return
        if guard.timed_out():
            raise LLMError(f"流式首帧超时（{first_timeout:.0f}s）：{url}",
                           code="timeout", retryable=True) from None
        raise LLMError(f"流式读取中断：{exc}",
                       code="stream_broken", retryable=True) from exc
    finally:
        guard.cancel()
        if stream_key:
            with _ACTIVE_RESP_LOCK:
                if _ACTIVE_RESP.get(str(stream_key)) is resp:
                    _ACTIVE_RESP.pop(str(stream_key), None)
        if on_finish is not None:
            try:
                on_finish({"finish_reason": "stopped" if stopped else finish,
                           "stopped": bool(stopped),
                           "reasoning_chars": reasoning_chars,
                           "usage": usage})
            except Exception:                          # noqa: BLE001  观察器出错不影响收尾
                pass
        try:
            resp.close()
        except Exception:                              # noqa: BLE001
            pass
    if stopped:
        return
    if not got:
        raise LLMError("模型没有返回文本内容", code="empty", retryable=True)


# 400 参数不兼容：按顺序摘参数重试（与 imagegen.py 的「被拒即摘」同一思路）。
# 每一步只摘一次；全部摘完仍失败 → 不再重试，按原错误返回。
PARAM_STRIP_STEPS = (
    ("presence_penalty / frequency_penalty",
     ("presence_penalty", "frequency_penalty")),
    ("temperature / top_p", ("temperature", "top_p")),
    ("max_tokens → max_completion_tokens", ("max_tokens",)),
)
_PARAM_NAMES = ("temperature", "top_p", "presence_penalty", "frequency_penalty",
                "max_tokens", "max_completion_tokens")
_PARAM_REJECT_HINTS = ("unsupported parameter", "unknown parameter",
                       "unexpected parameter", "extra fields", "not supported",
                       "does not support", "invalid parameter", "不支持")


def _param_reject(exc: LLMError) -> bool:
    """400 且错误信息指向「参数不支持」（上下文超限走自己的降级，不在这里摘）。"""
    if exc.status != 400 or exc.code == "context_overflow":
        return False
    low = str(exc).lower()
    if any(name in low for name in _PARAM_NAMES):
        return True
    return any(hint in low for hint in _PARAM_REJECT_HINTS)


def _strip_param_step(payload: dict, done: set) -> str:
    """按固定顺序摘掉一组参数；返回说明，None 表示没有可摘的。"""
    for desc, keys in PARAM_STRIP_STEPS:
        if desc in done:
            continue
        if not any(k in payload for k in keys):
            continue
        if desc.startswith("max_tokens"):
            if "max_completion_tokens" in payload:
                done.add(desc)
                continue
            payload["max_completion_tokens"] = payload.pop("max_tokens")
        else:
            for k in keys:
                payload.pop(k, None)
        done.add(desc)
        return desc
    return None


def chat_stream(base_url: str, api_key: str, model: str, messages: list[dict],
                timeout: float = FIRST_BYTE_TIMEOUT_STREAM,
                temperature: float | None = 0.7,
                max_tokens: int | None = None, on_send=None, should_stop=None,
                on_finish=None, stream_key=None, profile: dict | None = None,
                include_usage: bool = False, **extra):
    """流式对话：逐段 yield 文本增量。

    还没吐出任何内容时按状态码重试（非连接建立类失败总尝试 MAX_ATTEMPTS 次，
    连接建立阶段失败只尝试 CONNECT_MAX_ATTEMPTS 次；等待 1/2/4s + 抖动，
    Retry-After 封顶 30s，401/403/400 不重试）；地址没写 /v1 且 404 时先补一次 /v1。
    一旦吐出第一段就不再换地址或重试——客户端已经收到半截内容，
    已下发内容的回滚由上层处理。
    include_usage：给流式请求加 stream_options.include_usage（默认关，
    部分中转站不认这个字段，会让整个请求 400）。
    """
    if not model:
        raise LLMError("没有指定模型")
    payload = _chat_payload(model, messages, True, temperature, max_tokens,
                            extra, profile)
    if include_usage:
        opts = payload.get("stream_options")
        opts = dict(opts) if isinstance(opts, dict) else {}
        opts["include_usage"] = True
        payload["stream_options"] = opts
    base = str(base_url or "").strip().rstrip("/")
    urls = [_endpoint(base_url, "chat/completions")]
    if "/v1/" not in f"{base}/":
        urls.append(f"{base}/v1/chat/completions")
    url_idx = 0
    attempt = 1
    connect_failures = 0
    empty_retried = False
    strip_done: set[str] = set()          # 已摘过的参数组（每组只重试一次）
    while True:
        if should_stop and should_stop():
            on_finish and on_finish({"finish_reason": "stopped", "stopped": True})
            return
        started = False
        try:
            for piece in _stream_once(urls[url_idx], api_key, payload, timeout,
                                      on_send=on_send, should_stop=should_stop,
                                      on_finish=on_finish, stream_key=stream_key):
                started = True
                yield piece
            return
        except LLMError as exc:
            if started:
                raise                     # 已下发内容：不重试、不换地址
            if exc.status == 404 and url_idx + 1 < len(urls):
                url_idx += 1              # 只补一次 /v1，不算退避次数
                continue
            if _param_reject(exc):
                desc = _strip_param_step(payload, strip_done)
                if desc:
                    print(f"[llm] 上游拒绝参数，摘掉 {desc} 后重试：{exc}",
                          file=sys.stderr, flush=True)
                    continue          # 不占网络重试次数，但每组只摘一次
            if exc.connect_failed:
                connect_failures += 1      # 只对连接建立阶段单独计数
                if connect_failures >= CONNECT_MAX_ATTEMPTS:
                    raise
            if attempt >= MAX_ATTEMPTS:
                raise
            retry_ok = exc.retryable
            if exc.code == "empty" and not empty_retried:
                empty_retried = True      # 空响应：stop 且空时只额外重试一次
                retry_ok = True
            if not retry_ok:
                raise
            time.sleep(_retry_delay(attempt, exc.retry_after))
            attempt += 1
