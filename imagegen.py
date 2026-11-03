#!/usr/bin/env python3
"""
imagegen.py — 绘图模型对接（OpenAI 兼容协议，纯 Python 标准库）

两类请求：
    GET  {base_url}/models               拉模型列表（控制台「绘图模型」检测用）
    POST {base_url}/images/generations   文生图（返回 b64_json 或图片 URL）

base_url 填 https://api.openai.com/v1，或任何兼容这套协议的服务
（阿里百炼兼容模式、硅基流动、火山方舟、自建中转站…）。
地址与密钥由 server.py 从数据库 image_providers 取出后传进来。

另外提供 local 预设：不联网，按提示词生成一张渐变占位 PNG，
用于没配密钥时也能把「绘图广场 → 生成」整条链路跑通。
"""

import base64
import hashlib
import json
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

UA = "missai-local/1.0"

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


def _request(url: str, api_key: str = "", payload=None, timeout: float = 60.0) -> dict:
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:                                  # noqa: BLE001
            detail = ""
        raise ImageGenError(f"HTTP {exc.code} {exc.reason}：{detail or url}") from None
    except urllib.error.URLError as exc:
        raise ImageGenError(f"连不上 {url}：{exc.reason}") from None
    except TimeoutError:
        raise ImageGenError(f"请求超时（{timeout:.0f}s）：{url}") from None
    except OSError as exc:
        raise ImageGenError(f"连不上 {url}：{exc}") from None
    try:
        obj = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise ImageGenError(f"回包不是 JSON：{body[:200]!r}") from None
    if isinstance(obj, dict) and obj.get("error"):
        err = obj["error"]
        msg = err.get("message") if isinstance(err, dict) else err
        raise ImageGenError(f"上游报错：{msg}")
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
    """网络抖动类错误（TLS 握手超时 / 连接被重置 / 临时不可达）→ 值得再试一次。
    这台机器到 apihub.agnes-ai.com 的 TLS 握手正常时也要 20 多秒，经常整段挂住，
    不重试的话一次抖动就白跑一张图。"""
    text = str(exc).lower()
    return any(k in text for k in (
        "handshake operation timed out", "timed out", "timeout", "10054",
        "10060", "connection reset", "connection aborted", "connection refused",
        "eof occurred", "temporarily unavailable", "远程主机强迫关闭",
    ))


def _post_with_retry(base_url: str, tail: str, api_key: str, payload,
                     timeout: float, attempts: int = 3) -> dict:
    """POST 带重试：只对网络类错误重试，参数类错误（400/422）直接抛给上层去裁参数。
    第一次用完整超时，后续重试把单次超时压到 60s，避免一个坏网络把任务拖到十几分钟。"""
    last: ImageGenError | None = None
    for i in range(max(1, attempts)):
        try:
            return _with_v1_fallback(base_url, tail, api_key, payload,
                                     timeout if i == 0 else min(timeout, 60.0))
        except ImageGenError as exc:
            last = exc
            if i + 1 >= attempts or _is_param_reject(exc) or not _is_transient(exc):
                raise
            time.sleep(2.0 * (i + 1))
    raise last if last else ImageGenError("请求失败")


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


def _download(url: str, timeout: float = 120.0) -> bytes:
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ImageGenError(f"图片下载失败：{exc}") from None


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


def _decode_item(item) -> bytes | None:
    if isinstance(item, str):
        text = item.strip()
        if text.startswith("data:"):
            text = text.split(",", 1)[-1]
        if text.startswith("http://") or text.startswith("https://"):
            return _download(text)
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
        return _download(str(url))
    return None


def _is_param_reject(exc: Exception) -> bool:
    """上游报错像是“参数不被支持”（400/422，或 200 回包里的 error）"""
    text = str(exc).lower()
    if "http 400" in text or "http 422" in text:
        return True
    return "上游报错" in text and ("not supported" in text or "unsupported" in text)


def generate(base_url: str, api_key: str, model: str, prompt: str,
             negative: str = "", size: str = "1024x1024", n: int = 1,
             timeout: float = 180.0, seed: int = 0) -> list[bytes]:
    """文生图：返回若干张图片的字节流。

    不同中转站对可选参数的支持不一样（有的不认 negative_prompt / seed /
    response_format / n / size，会直接 400）。这里按「上游点名的参数」逐个去掉
    重试，而不是整条生成直接失败。
    """
    if not model:
        raise ImageGenError("没有指定绘图模型")
    if not str(prompt or "").strip():
        raise ImageGenError("提示词不能为空")
    n = max(1, min(4, int(n or 1)))
    if str(base_url or "").strip().startswith("local://"):
        return [placeholder_png(prompt, size, seed) for _ in range(n)]
    payload = {
        "model": model,
        "prompt": str(prompt).strip(),
        "n": n,
        "size": str(size or "1024x1024"),
        "response_format": "b64_json",
    }
    if str(negative or "").strip():
        payload["negative_prompt"] = str(negative).strip()
    if int(seed or 0):
        payload["seed"] = int(seed)

    optional = ("negative_prompt", "seed", "response_format", "n", "size")
    last: ImageGenError | None = None
    for _ in range(len(optional) + 1):
        try:
            obj = _post_with_retry(base_url, "images/generations",
                                   api_key, payload, timeout)
            break
        except ImageGenError as exc:
            last = exc
            if not _is_param_reject(exc):
                raise
            text = str(exc).lower()
            drop = next((k for k in optional if k in payload and k in text), "")
            if not drop:            # 上游没说清是哪个参数 → 先去掉最常被拒的两个
                drop = next((k for k in ("negative_prompt", "seed") if k in payload), "")
            if not drop:
                raise
            payload.pop(drop, None)
    else:                                            # pragma: no cover
        raise last
    items = _pick_items(obj)
    if not items:
        raise ImageGenError(f"回包里没有图片：{str(obj)[:240]}")
    images: list[bytes] = []
    for item in items[:n]:
        data = _decode_item(item)
        if data:
            images.append(data)
    if not images:
        raise ImageGenError("上游没有返回可用的图片数据")
    return images


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


def placeholder_png(prompt: str, size: str = "1024x1024", seed: int = 0) -> bytes:
    """按提示词 + 种子哈希生成一张渐变 + 光斑的占位图（不联网、可复现）"""
    w, h = parse_size(size)
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

