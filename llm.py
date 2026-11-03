#!/usr/bin/env python3
"""
llm.py — OpenAI 协议对接（纯 Python 标标库，无第三方依赖）

三条请求，全部按 OpenAI 官方协议：
    GET  {base_url}/models            拉模型列表（控制台「模型接入」检测用）
    POST {base_url}/chat/completions  对话（stream=false，一次拿全）
    POST {base_url}/chat/completions  对话（stream=true，逐段拿，chat_stream）

base_url 填官方 https://api.openai.com/v1，或任何兼容这套协议的中转站；
密钥与地址由 server.py 从数据库 llm_providers 取出后传进来。
日后接阿里百炼 / DeepSeek / 火山方舟，只要换 base_url（协议相同）。
"""

import json
import urllib.error
import urllib.request

UA = "missai-local/1.0"

# 控制台「聊天模型」只检测对话模型：模型名里带这些词的（绘图 / 视频 / 向量 / 语音等）一律跳过。
# 注意 vision / omni 这类多模态对话模型不算，仍然保留。
NON_CHAT_HINTS = (
    "image", "img-", "imagen", "video", "sora", "veo", "kling", "seedance",
    "wan2", "wanx", "t2v", "i2v", "s2v", "hailuo", "runway", "luma", "pika",
    "vidu", "dreamina", "jimeng", "embedding", "embed-", "rerank", "audio",
    "tts", "asr", "speech", "ocr", "dall-e", "midjourney", "flux", "sdxl",
)


class LLMError(RuntimeError):
    """对接失败的可读原因（控制台与日志直接显示）"""


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


def _request(url: str, api_key: str = "", payload=None, timeout: float = 60.0,
             on_send=None) -> dict:
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    key = str(api_key or "").strip()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    _notify_sent(on_send, url, payload, False)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:              # 4xx/5xx：把上游的说明带回来
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:                              # noqa: BLE001
            detail = ""
        raise LLMError(f"HTTP {exc.code} {exc.reason}：{detail or url}") from None
    except urllib.error.URLError as exc:
        raise LLMError(f"连不上 {url}：{exc.reason}") from None
    except TimeoutError:
        raise LLMError(f"请求超时（{timeout:.0f}s）：{url}") from None
    except OSError as exc:
        raise LLMError(f"连不上 {url}：{exc}") from None
    try:
        obj = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise LLMError(f"回包不是 JSON：{body[:200]!r}") from None
    if isinstance(obj, dict) and obj.get("error"):
        err = obj["error"]
        msg = err.get("message") if isinstance(err, dict) else err
        raise LLMError(f"上游报错：{msg}")
    return obj if isinstance(obj, dict) else {}


def _with_v1_fallback(base_url: str, tail: str, api_key: str, payload,
                      timeout: float, on_send=None) -> dict:
    """地址没写 /v1 但上游要 /v1 → 404 时自动补一次"""
    try:
        return _request(_endpoint(base_url, tail), api_key, payload, timeout,
                        on_send=on_send)
    except LLMError as exc:
        base = str(base_url or "").strip().rstrip("/")
        if "HTTP 404" not in str(exc) or "/v1/" in f"{base}/":
            raise
        return _request(f"{base}/v1/{tail.lstrip('/')}", api_key, payload, timeout,
                        on_send=on_send)


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
                  extra: dict) -> dict:
    payload: dict = {"model": model, "messages": messages, "stream": stream}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _content_text(content) -> str:
    """message.content / delta.content —— 兼容新式分块 [{type,text},…]"""
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def chat(base_url: str, api_key: str, model: str, messages: list[dict],
         timeout: float = 90.0, temperature: float | None = 0.8,
         max_tokens: int | None = None, on_send=None, **extra) -> str:
    """一次对话（非流式），返回模型文本"""
    if not model:
        raise LLMError("没有指定模型")
    payload = _chat_payload(model, messages, False, temperature, max_tokens, extra)
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
    if not content:
        raise LLMError("模型没有返回文本内容")
    return content


def _stream_once(url: str, api_key: str, payload: dict, timeout: float,
                 on_send=None):
    """连一次上游，逐段 yield 文本增量（生成器）

    上游若无视 stream 直接回整包 JSON（不少中转站会这样），也照样解析出来。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "text/event-stream")
    req.add_header("Content-Type", "application/json")
    key = str(api_key or "").strip()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    _notify_sent(on_send, url, payload, True)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:              # 4xx/5xx：把上游的说明带回来
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:                              # noqa: BLE001
            detail = ""
        raise LLMError(f"HTTP {exc.code} {exc.reason}：{detail or url}") from None
    except urllib.error.URLError as exc:
        raise LLMError(f"连不上 {url}：{exc.reason}") from None
    except TimeoutError:
        raise LLMError(f"请求超时（{timeout:.0f}s）：{url}") from None
    except OSError as exc:
        raise LLMError(f"连不上 {url}：{exc}") from None

    got = False
    try:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "event-stream" not in ctype and "json" in ctype:
            raw = resp.read()                          # 上游没流式 → 当整包处理
            try:
                obj = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                raise LLMError(f"回包不是 JSON：{raw[:200]!r}") from None
            if isinstance(obj, dict) and obj.get("error"):
                err = obj["error"]
                msg = err.get("message") if isinstance(err, dict) else err
                raise LLMError(f"上游报错：{msg}")
            choices = obj.get("choices") if isinstance(obj, dict) else None
            first = choices[0] if isinstance(choices, list) and choices else {}
            first = first if isinstance(first, dict) else {}
            msg = first.get("message") if isinstance(first.get("message"), dict) else {}
            text = _content_text(msg.get("content")) or str(first.get("text") or "")
            if text:
                got = True
                yield text
            return
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.lower().startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk:
                continue
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("error"):
                err = obj["error"]
                msg = err.get("message") if isinstance(err, dict) else err
                raise LLMError(f"上游报错：{msg}")
            choices = obj.get("choices")
            first = choices[0] if isinstance(choices, list) and choices else {}
            first = first if isinstance(first, dict) else {}
            delta = first.get("delta")
            piece = _content_text(delta.get("content")) if isinstance(delta, dict) else ""
            if not piece and not delta:
                msg = first.get("message")
                piece = _content_text(msg.get("content")) if isinstance(msg, dict) else ""
                if not piece:
                    piece = str(first.get("text") or "")
            if piece:
                got = True
                yield piece
    except (TimeoutError, OSError) as exc:
        raise LLMError(f"流式读取中断：{exc}") from None
    finally:
        try:
            resp.close()
        except Exception:                              # noqa: BLE001
            pass
    if not got:
        raise LLMError("模型没有返回文本内容")


def chat_stream(base_url: str, api_key: str, model: str, messages: list[dict],
                timeout: float = 300.0, temperature: float | None = 0.8,
                max_tokens: int | None = None, on_send=None, **extra):
    """流式对话：逐段 yield 文本增量。

    生成器一旦吐出第一段就不能再“换地址重试”（客户端已经收到半截内容），
    所以只有在**还没吐出任何内容**时，404 才补一次 /v1 重试。
    """
    if not model:
        raise LLMError("没有指定模型")
    payload = _chat_payload(model, messages, True, temperature, max_tokens, extra)
    base = str(base_url or "").strip().rstrip("/")
    urls = [_endpoint(base_url, "chat/completions")]
    if "/v1/" not in f"{base}/":
        urls.append(f"{base}/v1/chat/completions")
    last: LLMError | None = None
    for idx, url in enumerate(urls):
        started = False
        try:
            for piece in _stream_once(url, api_key, payload, timeout,
                                      on_send=on_send):
                started = True
                yield piece
            return
        except LLMError as exc:
            if started or idx + 1 >= len(urls) or "HTTP 404" not in str(exc):
                raise
            last = exc
    if last:                                            # pragma: no cover
        raise last
