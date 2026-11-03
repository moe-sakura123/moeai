#!/usr/bin/env python3
"""
captcha.py — 本地「数字加减法」验证码（纯标准库）

前端要求：
  · POST /api/gva/base/captchaV3 返回 captchaMode="digit"、picPath 必须是
    data:image/png;base64, 开头的题图，captchaId 非空
  · 题面是 100 以内的加减法，答案 1~3 位（前端 digit 输入框原为硬编码 6 位，
    已在 js/1068-*.js 中放宽为 1~3 位）
  · POST /api/gva/base/captchaV3/verify 返回 {code:0,data:{success:true}}
  · 登录请求体带 captcha / captchaId，由服务端校验
"""

import base64
import random
import secrets
import struct
import threading
import time
import zlib

TTL_SECONDS = 600

_lock = threading.Lock()
_challenges: dict[str, tuple[str, float]] = {}   # captchaId -> (答案, 过期时间戳)

# --------------------------------------------------------------- 5x7 点阵字
FONT = {
    "0": [".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."],
    "1": ["..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "2": [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"],
    "3": ["#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."],
    "4": ["...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
    "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
    "6": ["..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."],
    "7": ["#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."],
    "8": [".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."],
    "9": [".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."],
    "+": [".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."],
    "-": [".....", ".....", ".....", "#####", ".....", ".....", "....."],
    "=": [".....", ".....", "#####", ".....", "#####", ".....", "....."],
    "?": [".###.", "#...#", "....#", "...#.", "..#..", ".....", "..#.."],
    " ": [".....", ".....", ".....", ".....", ".....", ".....", "....."],
}

CHAR_W, CHAR_H = 5, 7


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _encode(text: str, scale: int = 5, pad: int = 14,
            fg=(26, 32, 64), bg=(238, 241, 250)) -> bytes:
    cols = len(text)
    w = cols * (CHAR_W + 1) * scale + pad * 2
    h = CHAR_H * scale + pad * 2
    buf = [bytearray([bg[0], bg[1], bg[2]] * w) for _ in range(h)]

    for _ in range(max(1, (w * h) // 60)):
        x, y = random.randrange(w), random.randrange(h)
        buf[y][x * 3] = max(0, bg[0] - 45)
        buf[y][x * 3 + 1] = max(0, bg[1] - 45)
        buf[y][x * 3 + 2] = max(0, bg[2] - 45)

    for idx, ch in enumerate(text):
        glyph = FONT.get(ch) or FONT[" "]
        ox = pad + idx * (CHAR_W + 1) * scale
        oy = pad
        for gy, line in enumerate(glyph):
            for gx, cell in enumerate(line):
                if cell != "#":
                    continue
                for sy in range(scale):
                    row = buf[oy + gy * scale + sy]
                    for sx in range(scale):
                        p = (ox + gx * scale + sx) * 3
                        row[p], row[p + 1], row[p + 2] = fg

    raw = b"".join(b"\x00" + bytes(r) for r in buf)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    text_chunk = _chunk(b"tEXt", b"Comment\x00" + text.encode("latin-1"))
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + text_chunk
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


def read_png_text(png: bytes) -> str:
    """读出题面（测试用）"""
    i = png.find(b"tEXt")
    if i < 0:
        return ""
    length = struct.unpack(">I", png[i - 4:i])[0]
    payload = png[i + 4:i + 4 + length]
    return payload.split(b"\x00", 1)[-1].decode("latin-1", "replace")


def _problem() -> tuple[str, str]:
    """出一道 100 以内的加/减法 → (题面, 答案)"""
    if random.random() < 0.5:                       # 加法：和不超过 100
        a = random.randint(1, 99)
        b = random.randint(1, 100 - a)
        return f"{a} + {b} = ?", str(a + b)
    a = random.randint(2, 100)                      # 减法：差不小于 1
    b = random.randint(1, a - 1)
    return f"{a} - {b} = ?", str(a - b)


def _prune() -> None:
    now = time.time()
    for cid in [k for k, (_, exp) in _challenges.items() if exp < now]:
        _challenges.pop(cid, None)


def new_challenge() -> tuple[str, str]:
    """→ (captchaId, data:image/png;base64,... 题图)"""
    question, answer = _problem()
    png = _encode(question)
    cid = secrets.token_urlsafe(18)
    with _lock:
        _prune()
        _challenges[cid] = (answer, time.time() + TTL_SECONDS)
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return cid, uri


def verify(captcha_id: str, value) -> bool:
    """校验答案；题目在 TTL 内可重复校验（登录前可能先 verify 一次）

    比较时忽略前导零，所以 "042" 与 "42" 都算对。
    """
    if not captcha_id:
        return False
    digits = _norm("".join(ch for ch in str(value or "") if ch.isdigit()))
    with _lock:
        entry = _challenges.get(captcha_id)
        if not entry:
            return False
        answer, exp = entry
        if exp < time.time():
            _challenges.pop(captcha_id, None)
            return False
    return secrets.compare_digest(digits, _norm(answer))


def _norm(s: str) -> str:
    """去掉前导零；空串按 0 处理"""
    s = s.lstrip("0")
    return s or "0"


def answer_of(captcha_id: str) -> str | None:
    """调试用：取当前答案"""
    with _lock:
        entry = _challenges.get(captcha_id)
    return entry[0] if entry else None
