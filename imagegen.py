#!/usr/bin/env python3
"""
imagegen.py — 绘图模型对接（Agnes 官方模板，传输层 httpx）

两类请求：
    GET  {base_url}/models               拉模型列表（控制台「绘图模型」检测用）
    POST {base_url}/images/generations   文生图（返回图片 URL 或 b64_json）

契约（Agnes Image 2.5 Flash 官方文档）：
    · size 用档位 1K/2K/3K/4K，配 ratio（1:1、16:9…）；像素写法兼容但会被标准化
    · response_format 必须放在 extra_body 里，顶层不认
    · 文生图要 Base64 用顶层 return_base64=true；图生图用 extra_body.response_format
    · 响应 data[0].url 或 data[0].b64_json，同步返回，无 task 轮询

默认优先要 URL（回包小、省流量），下载失败或上游只给 Base64 时自动回退到
return_base64=true；老式 OpenAI 兼容中转站再回退到顶层 response_format。
地址与密钥由 server.py 从数据库 image_providers 取出后传进来。

另外提供 local 预设：不联网，按提示词生成一张渐变占位 PNG，
用于没配密钥时也能把「绘图广场 → 生成」整条链路跑通。
"""

import base64
import hashlib
import json
import math
import struct
import time
import urllib.parse
import zlib

import httpx

import llm

UA = "missai-local/1.0"

# 传输层复用 llm.py 的连接池：同主机、同样的 IPv4 强制（无代理时）与 3s 连接超时。
# 单次请求再按预算覆盖 read，避免把整张图的等待压在连接阶段。
_HTTP = llm._CLIENT
CONNECT_TIMEOUT = llm.CONNECT_TIMEOUT
POOL_TIMEOUT = 3.0
WRITE_TIMEOUT = 10.0
# 出图总预算：官方建议客户端 60~360s，这里取 120s；最多尝试 2 次（1 次重试）。
TOTAL_BUDGET = 120.0
DOWNLOAD_TIMEOUT = 60.0
MAX_ATTEMPTS = 2

# 判断「这是不是一个绘图模型」用的关键词（/models 会连聊天模型一起返回）
IMAGE_HINTS = (
    "dall-e", "dalle", "gpt-image", "image", "imagen", "wanx", "seedream",
    "kolors", "flux", "stable-diffusion", "sdxl", "sd3", "sd-3", "qwen-image",
    "hunyuan-image", "cogview", "janus", "ideogram", "recraft", "midjourney",
    "niji", "playground", "dreamina", "doubao-seedream",
)


class ImageGenError(RuntimeError):
    """对接失败的可读原因（控制台与绘图页直接显示）"""


def _base(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise ImageGenError("API 地址为空")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    return base


def _endpoint(base_url: str, tail: str) -> str:
    return f"{_base(base_url)}/{tail.lstrip('/')}"


def _timeout(read: float) -> httpx.Timeout:
    """连接 3s / 单次读 read / 写 10s / 连接池 3s；read 可按总预算动态收缩。"""
    return httpx.Timeout(connect=CONNECT_TIMEOUT, read=max(1.0, float(read)),
                         write=WRITE_TIMEOUT, pool=POOL_TIMEOUT)


def _request(url: str, api_key: str = "", payload=None, timeout: float = 60.0) -> dict:
    headers = {"User-Agent": UA, "Accept": "application/json"}
    key = str(api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        if payload is None:
            resp = _HTTP.get(url, headers=headers, timeout=_timeout(timeout))
        else:
            resp = _HTTP.post(url, headers=headers, json=payload, timeout=_timeout(timeout))
    except httpx.TimeoutException:
        raise ImageGenError(f"请求超时（{timeout:.0f}s）：{url}") from None
    except httpx.HTTPError as exc:
        raise ImageGenError(f"连不上 {url}：{type(exc).__name__}: {exc}") from None
    try:
        body = resp.read()
    except httpx.TimeoutException:
        raise ImageGenError(f"请求超时（{timeout:.0f}s）：{url}") from None
    except httpx.HTTPError as exc:
        raise ImageGenError(f"连不上 {url}：{type(exc).__name__}: {exc}") from None
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = body.decode("utf-8", "replace")[:400]
        except Exception:                                  # noqa: BLE001
            detail = ""
        raise ImageGenError(
            f"HTTP {resp.status_code} {resp.reason_phrase}：{detail or url}")
    try:
        obj = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise ImageGenError(f"回包不是 JSON：{body[:200]!r}") from None
    if isinstance(obj, dict) and obj.get("error"):
        err = obj["error"]
        msg = err.get("message") if isinstance(err, dict) else err
        raise ImageGenError(f"上游报错：{msg}")
    # 常见中转站会把错误放在顶层 code/message（官方文档未定义该形状，做安全兼容）
    if isinstance(obj, dict) and obj.get("success") is False:
        raise ImageGenError(f"上游报错：{obj.get('message') or obj.get('msg') or obj}")
    if isinstance(obj, dict) and not obj.get("data"):
        code = obj.get("code")
        if code not in (None, 0, "0", 200, "200"):
            msg = obj.get("message") or obj.get("msg") or obj
            raise ImageGenError(f"上游报错（code {code}）：{msg}")
    return obj if isinstance(obj, dict) else {}


def _with_v1_fallback(base_url: str, tail: str, api_key: str, payload,
                      timeout: float) -> dict:
    """地址没写 /v1 但上游要 /v1 → 404 时自动补一次"""
    try:
        return _request(_endpoint(base_url, tail), api_key, payload, timeout)
    except ImageGenError as exc:
        base = _base(base_url)
        if "HTTP 404" not in str(exc) or "/v1/" in f"{base}/":
            raise
        return _request(f"{base}/v1/{tail.lstrip('/')}", api_key, payload, timeout)


def _is_transient(exc: Exception) -> bool:
    """网络抖动 / 上游 5xx / 429 才值得重试；参数错误与鉴权错误直接抛。"""
    text = str(exc).lower()
    if any(k in text for k in ("http 401", "http 403", "http 400", "http 422")):
        return False
    return any(k in text for k in (
        "timed out", "timeout", "超时", "connect", "read error", "remoteprotocol",
        "server disconnected", "connection reset", "connection aborted",
        "connection refused", "eof occurred", "temporarily unavailable",
        "远程主机强迫关闭", "http 408", "http 429", "http 500", "http 502",
        "http 503", "http 504",
    ))


def _post_with_retry(base_url: str, tail: str, api_key: str, payload,
                     timeout: float, attempts: int = MAX_ATTEMPTS) -> dict:
    """POST 带重试：只对网络类错误重试，参数类错误（400/422）直接抛给上层去裁参数。
    总预算由 timeout 控制（默认调用方会压到 TOTAL_BUDGET），最多 2 次尝试；
    每次按剩余预算收缩 read 超时，避免单次 180s + 多次重试的长挂。"""
    attempts = max(1, min(MAX_ATTEMPTS, int(attempts or MAX_ATTEMPTS)))
    budget = max(1.0, float(timeout or TOTAL_BUDGET))
    deadline = time.monotonic() + budget
    last: ImageGenError | None = None
    for i in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            return _with_v1_fallback(base_url, tail, api_key, payload, remaining)
        except ImageGenError as exc:
            last = exc
            if i + 1 >= attempts or _is_param_reject(exc) or not _is_transient(exc):
                raise
            time.sleep(min(1.5, max(0.0, deadline - time.monotonic())))
    raise last if last else ImageGenError(f"请求超时（总预算 {budget:.0f}s）")


def list_models(base_url: str, api_key: str, timeout: float = 60.0,
                hints=None) -> list[str]:
    """检测连通性 + 拉模型列表；优先挑出绘图类模型（hints 可换成视频类关键词）"""
    obj = _with_v1_fallback(base_url, "models", api_key, None, timeout)
    data = obj.get("data")
    if data is None:
        data = obj.get("models")
    if not isinstance(data, list):
        raise ImageGenError(f"回包里没有模型列表：{str(obj)[:200]}")
    names: list[str] = []
    for item in data:
        if isinstance(item, dict):
            mid = item.get("id") or item.get("model") or item.get("name")
        else:
            mid = item
        mid = str(mid or "").strip()
        if mid and mid not in names:
            names.append(mid)
    if not names:
        raise ImageGenError("上游返回了空的模型列表")
    words = IMAGE_HINTS if hints is None else tuple(hints)
    picked = [n for n in names if any(h in n.lower() for h in words)]
    return picked or names          # 一个都没匹配上就把全部列出来，让用户自己选


def parse_size(size: str) -> tuple[int, int]:
    """'1024x1536' → (1024, 1536)，异常值回到 1024x1024"""
    try:
        w, h = str(size or "").lower().replace("×", "x").split("x", 1)
        w, h = int(w), int(h)
    except (ValueError, AttributeError):
        return 1024, 1024
    w = max(64, min(2048, w))
    h = max(64, min(2048, h))
    return w, h


def _download(url: str, timeout: float = DOWNLOAD_TIMEOUT) -> bytes:
    try:
        resp = _HTTP.get(url, headers={"User-Agent": UA}, timeout=_timeout(timeout))
    except httpx.TimeoutException:
        raise ImageGenError(f"图片下载超时（{timeout:.0f}s）：{url}") from None
    except httpx.HTTPError as exc:
        raise ImageGenError(f"图片下载失败：{type(exc).__name__}: {exc}") from None
    if resp.status_code >= 400:
        raise ImageGenError(
            f"图片下载失败：HTTP {resp.status_code} {resp.reason_phrase}")
    try:
        return resp.read()
    except httpx.TimeoutException:
        raise ImageGenError(f"图片下载超时（{timeout:.0f}s）：{url}") from None
    except httpx.HTTPError as exc:
        raise ImageGenError(f"图片下载失败：{type(exc).__name__}: {exc}") from None


def _pick_items(obj: dict) -> list:
    """兼容 OpenAI / 部分中转站的回包形状"""
    for key in ("data", "images", "results"):
        items = obj.get(key)
        if isinstance(items, list) and items:
            return items
    out = obj.get("output")
    if isinstance(out, dict):
        for key in ("results", "images", "data"):
            items = out.get(key)
            if isinstance(items, list) and items:
                return items
    return []


def _decode_item(item, timeout: float = DOWNLOAD_TIMEOUT) -> bytes | None:
    if isinstance(item, str):
        text = item.strip()
        if text.startswith("data:"):
            text = text.split(",", 1)[-1]
        if text.startswith("http://") or text.startswith("https://"):
            return _download(text, timeout)
        try:
            return base64.b64decode(text, validate=False)
        except (ValueError, TypeError):
            return None
    if not isinstance(item, dict):
        return None
    b64 = item.get("b64_json") or item.get("b64") or item.get("base64")
    if b64:
        text = str(b64).strip()
        if text.startswith("data:"):
            text = text.split(",", 1)[-1]
        try:
            return base64.b64decode(text, validate=False)
        except (ValueError, TypeError):
            return None
    url = item.get("url") or item.get("image_url")
    if url:
        return _download(str(url), timeout)
    return None


def _is_param_reject(exc: Exception) -> bool:
    """上游报错像是“参数不被支持”（400/422，或 200 回包里的 error）"""
    text = str(exc).lower()
    if "http 400" in text or "http 422" in text:
        return True
    return "上游报错" in text and ("not supported" in text or "unsupported" in text)


def _tier_or_default(tier: str, size: str) -> str:
    value = str(tier or "").strip().upper()
    if value in ("1K", "2K", "3K", "4K"):
        return value
    raw = str(size or "").strip().upper().replace(" ", "")
    if raw in ("1K", "2K", "3K", "4K"):
        return raw
    return "1K"


def _payload_variants(model: str, prompt: str, negative: str, size: str,
                      seed: int, ratio: str = "", width: int = 0,
                      height: int = 0, tier: str = "",
                      official_only: bool = False) -> list[tuple[str, dict]]:
    """官方写法优先；老中转站再用兼容写法回退。

    官方语义：size=档位（1K/2K/3K/4K）+ ratio（1:1/16:9…）；宽高只用于老式兼容写法。
    official_only=True（Agnes 官方接入）时绝不发送文档未定义的字段：
    n / negative_prompt / seed / 顶层 response_format 都不会出现在请求体里。
    """
    tier_name = _tier_or_default(tier, size)
    w, h = int(width or 0), int(height or 0)
    if w <= 0 or h <= 0:
        w, h = parse_size(size)
    base = {"model": model, "prompt": prompt, "size": tier_name}
    legacy = {"model": model, "prompt": prompt, "size": f"{w}x{h}",
              "n": 1, "response_format": "b64_json"}
    if str(negative or "").strip():
        legacy["negative_prompt"] = str(negative).strip()
    if int(seed or 0):
        legacy["seed"] = int(seed)
    ratio_text = str(ratio or "").strip()
    if ratio_text:
        variants = [
            ("official-url", dict(base, ratio=ratio_text,
                                  extra_body={"response_format": "url"})),
            ("official-b64", dict(base, ratio=ratio_text, return_base64=True)),
        ]
    else:
        # 没有明确比例时才允许不带 ratio；有比例时绝不下发「无 ratio」请求
        variants = [
            ("official-url", dict(base, extra_body={"response_format": "url"})),
            ("official-b64", dict(base, return_base64=True)),
        ]
    if not official_only:
        variants.append(("legacy-b64", legacy))
    return variants


def _generate_one(base_url: str, api_key: str, model: str, prompt: str,
                  negative: str, size: str, budget: float, seed: int,
                  ratio: str = "", width: int = 0, height: int = 0,
                  tier: str = "", official_only: bool = False) -> bytes:
    deadline = time.monotonic() + budget
    last: ImageGenError | None = None
    for label, payload in _payload_variants(model, prompt, negative, size, seed,
                                            ratio, width, height, tier,
                                            official_only):
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            break
        try:
            obj = _post_with_retry(base_url, "images/generations",
                                   api_key, payload, remaining)
        except ImageGenError as exc:
            last = exc
            if _is_param_reject(exc):
                continue
            raise
        items = _pick_items(obj)
        if not items:
            last = ImageGenError(f"回包里没有图片（{label}）：{str(obj)[:240]}")
            continue
        for item in items:
            try:
                data = _decode_item(
                    item, min(DOWNLOAD_TIMEOUT, max(1.0, deadline - time.monotonic())))
            except ImageGenError as exc:
                last = exc
                continue
            if data:
                return data
        last = last or ImageGenError(f"上游没有返回可用的图片数据（{label}）")
    raise last if last else ImageGenError(f"请求超时（总预算 {budget:.0f}s）")


def generate(base_url: str, api_key: str, model: str, prompt: str,
             negative: str = "", size: str = "1024x1024", n: int = 1,
             timeout: float = 180.0, seed: int = 0, ratio: str = "",
             width: int = 0, height: int = 0, tier: str = "") -> list[bytes]:
    """文生图：返回若干张图片的字节流。

    Agnes 官方模板：size 用 1K/2K/3K/4K 档位 + ratio（宽高由数据库尺寸表解析后传入），
    URL 优先、Base64 回退；对老式 OpenAI 兼容中转站再回退到顶层 response_format。
    """
    if not model:
        raise ImageGenError("没有指定绘图模型")
    if not str(prompt or "").strip():
        raise ImageGenError("提示词不能为空")
    n = max(1, min(4, int(n or 1)))
    if str(base_url or "").strip().startswith("local://"):
        return [placeholder_png(prompt, size, seed, width, height) for _ in range(n)]
    budget = min(TOTAL_BUDGET, max(1.0, float(timeout or TOTAL_BUDGET)))
    # Agnes 官方接入只发文档定义的字段；其他 OpenAI 兼容中转站保留老写法回退
    official_only = "agnes" in str(base_url or "").lower()
    return [_generate_one(base_url, api_key, model, str(prompt).strip(),
                          negative, size, budget, seed, ratio, width, height,
                          tier, official_only) for _ in range(n)]


# --------------------------------------------------------------- 本地占位图

def _png(width: int, height: int, pixels: bytes) -> bytes:
    raw = b"".join(
        b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def placeholder_png(prompt: str, size: str = "1024x1024", seed: int = 0,
                    width: int = 0, height: int = 0) -> bytes:
    """按提示词 + 种子哈希生成一张渐变 + 光斑的占位图（不联网、可复现）

    width/height 由后端按数据库尺寸表解析后传入；未传时回退到 size 像素写法。
    """
    w, h = (int(width), int(height)) if width > 0 and height > 0 else parse_size(size)
    if w * h > 1024 * 1024:                        # 占位图别太大，纯 Python 逐像素算
        scale = (1024 * 1024 / (w * h)) ** 0.5
        w, h = max(64, int(w * scale)), max(64, int(h * scale))
    digest = hashlib.sha256(f"{prompt or 'missai'}|{int(seed or 0)}".encode("utf-8")).digest()
    # 两组主色（偏深、偏亮），保证和站点深色主题协调
    c1 = (26 + digest[0] % 56, 22 + digest[1] % 52, 52 + digest[2] % 78)
    c2 = (72 + digest[3] % 78, 26 + digest[4] % 58, 78 + digest[5] % 86)
    # 两个光斑
    blobs = []
    for i in range(2):
        bx = int((digest[6 + i * 3] / 255) * w)
        by = int((digest[7 + i * 3] / 255) * h)
        br = int(min(w, h) * (0.22 + digest[8 + i * 3] / 255 * 0.25))
        bc = (150 + digest[9 + i] % 100, 80 + digest[10 + i] % 100, 200)
        blobs.append((bx, by, br, bc))

    buf = bytearray(w * h * 3)
    for y in range(h):
        ty = y / max(1, h - 1)
        base_r = int(c1[0] + (c2[0] - c1[0]) * ty)
        base_g = int(c1[1] + (c2[1] - c1[1]) * ty)
        base_b = int(c1[2] + (c2[2] - c1[2]) * ty)
        row = y * w * 3
        for x in range(w):
            tx = x / max(1, w - 1)
            r = int(base_r * (0.82 + 0.18 * tx))
            g = int(base_g * (0.82 + 0.18 * tx))
            b = int(base_b * (0.86 + 0.14 * (1 - tx)))
            for bx, by, br, bc in blobs:
                dx, dy = x - bx, y - by
                d2 = dx * dx + dy * dy
                if d2 < br * br:
                    k = (1 - (d2 ** 0.5) / br) * 0.55
                    r = int(r * (1 - k) + bc[0] * k)
                    g = int(g * (1 - k) + bc[1] * k)
                    b = int(b * (1 - k) + bc[2] * k)
            if (x // 24 + y // 24) % 2 == 0:                 # 淡淡的格纹
                r, g, b = min(255, r + 6), min(255, g + 4), min(255, b + 10)
            i = row + x * 3
            buf[i] = r & 0xFF
            buf[i + 1] = g & 0xFF
            buf[i + 2] = b & 0xFF
    return _png(w, h, bytes(buf))


def detect_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".png"


# ---------------------------------------------------------------- 视频（OpenAI Videos 兼容）

VIDEO_MODES = ("text", "keyframe", "reference")
VIDEO_ASPECTS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")


def create_video(base_url: str, api_key: str, model: str, prompt: str,
                 mode: str = "text", seconds: str = "5", size: str = "720P",
                 aspect_ratio: str = "16:9", seed: int = 0,
                 timeout: float = 180.0, extra: dict | None = None) -> dict:
    """创建视频任务（POST {base}/videos），返回上游 JSON（含 video_id）。"""
    if not model:
        raise ImageGenError("没有指定视频模型")
    if not str(prompt or "").strip():
        raise ImageGenError("提示词不能为空")
    mode = mode if mode in VIDEO_MODES else "text"
    size = str(size or "720P").upper()
    if "flash" in str(model).lower():
        size = "720P"                                   # Flash 仅支持 720P
    try:
        sec = str(max(4, min(12, int(str(seconds or "5")))))
    except (TypeError, ValueError):
        sec = "5"
    ratio = aspect_ratio if aspect_ratio in VIDEO_ASPECTS else "16:9"
    payload = {
        "model": model,
        "prompt": str(prompt).strip(),
        "mode": mode,
        "seconds": sec,
        "size": size,
        "aspect_ratio": ratio,
        "n": 1,
    }
    if int(seed or 0):
        payload["seed"] = int(seed)
    if isinstance(extra, dict):
        for k in ("first_frame", "last_frame", "images", "audios"):
            val = extra.get(k)
            if val:
                payload[k] = val
    return _post_with_retry(base_url, "videos", api_key, payload, timeout, attempts=2)


def fetch_video(base_url: str, api_key: str, video_id: str, model_name: str = "",
                timeout: float = 60.0) -> dict:
    """查询视频任务（GET {base}/agnesapi?video_id=...&model_name=...）。"""
    if not str(video_id or "").strip():
        raise ImageGenError("缺少 video_id")
    q = {"video_id": str(video_id).strip()}
    if model_name:
        q["model_name"] = str(model_name).strip()
    return _request(_endpoint(base_url, "agnesapi?" + urllib.parse.urlencode(q)),
                    api_key, None, timeout)


def download_video(url: str, timeout: float = 180.0) -> bytes:
    """下载任务完成后的视频文件。"""
    if not str(url or "").strip():
        raise ImageGenError("任务未返回视频地址")
    data = _download(str(url), timeout=timeout)
    if not data:
        raise ImageGenError("视频下载为空")
    return data

