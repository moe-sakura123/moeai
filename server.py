#!/usr/bin/env python3
"""
server.py — 本地运行镜像站点（纯 Python 标准库，无第三方依赖）

静态：html/ 里放 html / js / css / image / assets
动态：
  · 本地账号 / 会话 / 上传 / 收藏 / 对话  → 落到本地 SQLite（db.py）
  · 页面里的每一张图（动图/静态图）        → 登记进图片库 image.db，存本地实际路径
  · 其余页面与接口                        → 反向代理到 missai01.com
  · 每次请求                             → 写入 request_log

用法： python server.py [端口]     默认 4173
访问： http://localhost:4173/home        站点
      http://localhost:4173/local/      本地控制台（日志/账号/图片库/收藏/对话）
"""

import hashlib
import html
import http.client
import json
import math
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import captcha
import db
import subprocess
import imagedb
import imagegen
import llm
import mediagen
import tplimport
from prompt_builder import (count_tokens, estimate_messages, prompt_prefix_hash,
                            sectionize, set_token_alpha)
from roleplay_core.prompt import (build_prompt, compress_for_budget,
                                     prompt_diff, shrink_for_overflow)
from roleplay_core import memory_engine, memory_recall
from roleplay_core.post_processor import parse_status, process_output, status_dict
from roleplay_core import ChatContext, Message
from roleplay_core.emotion_mapper import EmotionMapper
from roleplay_core.chat_router import (ChatPorts, ChatRequest, ChatRouter,
                                       StreamDecision)
from roleplay_core.sanitizer import (
    BOOK_STATUS_RE, clean_for_storage, clean_history_for_llm,
    normalize_say_blocks, wrap_next_prompts)

ORIGIN = "https://missai01.com"
CDN = "https://store.story6688.com"
CDN2 = "https://images.comfyai.site"

ROOT = Path(__file__).resolve().parent
HTML_DIR = ROOT / "html"
APP_DIR = HTML_DIR / "app"        # 唯一前端：自建原生页（/app/*）
JS_DIR = APP_DIR / "js"           # 前端脚本：html/app/js（对外只走 /app/js/*）
IMAGE_DIR = HTML_DIR / "image"    # 图片：html/image（统一落盘 + 进 image.db）
ASSET_DIR = HTML_DIR / "assets"

# 页面名 → 文件由目录扫描 + 名称白名单决定，不再维护逐页路由表。
# 页面名只允许小写字母 / 数字 / 下划线 / 连字符（不含点、斜杠、百分号），
# 从根上排除 ../、绝对路径、反斜杠与 %2e 编码绕过；再叠加 page_target 的目录校验。
PAGE_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
# 根级页面（/<name> → html/<name>.html）不与这些一级路径抢命名空间
ROOT_PAGE_RESERVED = frozenset({
    "app", "api", "local", "js", "css", "img", "assets", "cdn", "cdn2", "html",
})

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 4173

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".map": "application/json; charset=utf-8",
}

# 页面里写死的、需要落到本地的根目录资源
ROOT_FILES = {
    "/res.js": JS_DIR / "res.js",
    "/legacy-polyfills.js": JS_DIR / "legacy-polyfills.js",
    "/logo-goddess1.webp": ASSET_DIR / "logo-goddess1.webp",
    "/favicon.svg": ASSET_DIR / "favicon.svg",
    "/favicon-192.png": ASSET_DIR / "favicon-192.png",
    "/apple-touch-icon-180.png": ASSET_DIR / "apple-touch-icon-180.png",
    "/missai.webp": ASSET_DIR / "missai.webp",
    "/robots.txt": ASSET_DIR / "robots.txt",
}

FORWARD_HEADERS = (
    "cookie", "authorization", "content-type", "accept",
    "accept-language", "referer", "origin",
    "if-none-match", "if-modified-since", "range",
)

REWRITABLE = re.compile(
    r"(text/|application/json|application/javascript|application/ld\+json|x-javascript)"
)

# ---------------------------------------------------------------- SSE 事件
# 正文只走 message；状态、图片、计费各自独立事件，前端按 event 名分流，
# 不再从 JSON 的 type 字段猜用途（旧 type 仍保留在 data 里做兼容）。
SSE_EVENT_NAMES = {
    "content": "message", "message": "message", "delta": "message",
    "clear_content": "reset",
    "status_update": "status_update",
    "cg": "image_trigger", "image_trigger": "image_trigger",
    "usage": "usage", "done": "done", "error": "error",
}

# ------------------------------------------------- 本地接管的接口

SESSION_PATHS = {
    "/api/auth/session/bootstrap",
    "/api/auth/session",
    "/api/auth/session/establish",
}

LOGIN_PATHS = {
    "/api/gva/base/login",
    "/api/gva/base/email/login",
    "/api/gva/base/email/passwordLogin",
    "/api/gva/user/register_and_login",
    "/api/gva/user/register_v2",
    "/api/gva/user/admin_register",
}

LOGOUT_PATHS = {
    "/api/gva/base/logout",
    "/api/gva/base/email/logout",
    "/api/auth/logout",
    "/api/gva/user/logout",
}

# 只在本地回执，避免真的往外发短信 / 邮件
CODE_PATHS = {
    "/api/gva/base/email/sendCode",
    "/api/gva/user/sendResetPasswordCode",
    "/api/gva/user/resetPasswordByEmail",
}

# 数字加减法验证码：本地出题 + 本地判分（替代原站滑块）
CHALLENGE_PATHS = {"/api/gva/base/captchaV3", "/api/gva/base/captcha"}
VERIFY_PATHS = {"/api/gva/base/captchaV3/verify"}

# 交给浏览器缓存的读接口（GET）→ 只下发 Cache-Control，缓存留在浏览器里
BROWSER_CACHE_PREFIXES = (
    "/api/gva/narratium/",
    "/api/gva/narratiumCharacter/",
    "/api/app/download-config",
    "/api/gva/creativePlaza",
)
BROWSER_CACHE_MAX_AGE = 300     # 5 分钟内直接用浏览器缓存，不回源

# 本地读写的接口（创作页 + 聊天界面 + 角色详情）：本地回包永远新鲜，
# 不能让浏览器拿 5 分钟前的线上回包盖住本地数据
LOCAL_DATA_PREFIX = "/api/gva/"
LOCAL_DATA_TAILS = (
    "work", "work/", "works/", "workFilter/", "llmModelRate/", "aiMedia/",
    "fileUploadAndDownload/", "narratiumWorldbookEntry/", "narratiumStatRules/",
    "narratium/chatSession/", "narratium/guestChat/", "narratium/work/",
    "narratium/modelConfig/", "narratium/summaryConfig/", "narratium/promptConfig/",
    "narratium/embeddingConfig/",
    # 对话设置：模型参数 / 记忆 / embedding
    "narratiumCharacter/",
    "checkin/", "user/", "oauth/", "email/",                 # 签到 / 账户设置
)


def local_data_rel(path: str) -> str:
    """是本地读写接口 → 返回 /api/gva/ 后面的部分；否则返回空串"""
    if not path.startswith(LOCAL_DATA_PREFIX):
        return ""
    rel = path[len(LOCAL_DATA_PREFIX):]
    if rel == LOCAL_DATA_TAILS[0] or rel.startswith(LOCAL_DATA_TAILS):
        return rel
    return ""

# 内容接口（卡片文字 + 标签）：回包时拆进内容库 works/work_tags，
# 线上不通时用内容库还原同一个回包（离线兜底）。
# LOCAL_ONLY=1 时这些路径直接只读本地库，不再实时反代。
# /api/gva/aiImage 是绘图广场（图库）数据面，一并纳入内容库。
CONTENT_PREFIXES = (
    "/api/gva/narratium",
    "/api/gva/narratiumCharacter",
    "/api/gva/creativePlaza",
    "/api/gva/aiImage",
)
CONTENT_EXCLUDE = ("stream", "notification", "interaction", "vote", "comment", "statrules")

# 自测离线兜底用：MISSAI_OFFLINE=1 时不连线上，内容接口直接走本地库
OFFLINE = os.environ.get("MISSAI_OFFLINE", "") == "1"

# 完全本地化（用户已拍板）：探索/创意/绘图这些内容接口只从本地库读，
# 不再实时反代。镜像数据靠控制台 mirror 按钮 / 手动镜像拉进 content_api。
LOCAL_ONLY = True

# ---------------------------------------------------------------- 手动镜像
# tools/mirror.py 是独立脚本，server.py 不 import 它（两者完全解耦，删掉它
# 也不影响本服务）：这里用子进程拉起，进度由 mirror.py 写进 config_cache 的
# mirror_state，本进程只读库。
_MIRROR_LOCK = threading.Lock()
_MIRROR_PROC: "subprocess.Popen | None" = None
_MIRROR_DEFAULT = {
    "running": False, "surface": "", "phase": "idle", "message": "",
    "done": 0, "total": 0, "items": 0, "errors": 0,
    "started_at": "", "finished_at": "",
}

# 正在生成的流：用户点「停止」时置位事件，让上游读取循环立即退出。
# key = f"{uid}:{sessionId}"（游客 uid 为 None → 0）。
_ACTIVE_STREAMS: dict[str, threading.Event] = {}
_ACTIVE_STREAMS_LOCK = threading.Lock()
_SUMMARY_LOCK = threading.Lock()
_MEMORY_LOCK = threading.Lock()

# 最近一次召回状态（只读展示用）：key = (uid, sessionId, characterId)
_RECALL_STATS: dict[tuple[int, str, str], dict] = {}

# 最近一轮真实下发的模型参数（只读展示用）：key = (uid, sessionId, modelRateId)
_LAST_MODEL_PARAMS: dict[tuple[int, str, str], dict] = {}
_LAST_MODEL_PARAMS_LOCK = threading.Lock()

# ------------------------------------------------------------- 历史压缩
# 发送前确定性裁剪保底最近 COMPRESS_KEEP_ROUNDS 轮原文；
# 被裁掉的历史进后台压缩队列，由默认模型并进滚动摘要 / 记忆槽（下一轮生效）。
COMPRESS_KEEP_ROUNDS = 6           # 可被 db 配置 llm_compress_keep_rounds 覆盖
COMPRESS_KEEP_MAX_ROUNDS = 24      # 与 SUMMARY_WINDOW=24 条摘要窗口对齐的上限
SUMMARY_BATCH = 6                  # 每次摘要最多吃 6 条淘汰消息

# ------------------------------------------------------- 对话设置的模型参数
# 前端 chat.js 的 MODEL_RANGE 与这里必须完全一致；保存时越界钳制、非数字拒绝。
MODEL_PARAM_RANGES = {
    "temperature": (0.0, 1.0, float),
    "topP": (0.0, 1.0, float),
    "presencePenalty": (0.0, 1.0, float),
    "frequencyPenalty": (0.0, 1.0, float),
    "maxTokens": (256, 8192, int),
    "contextWindow": (1024, 32768, int),
}
MODEL_PARAM_DEFAULTS = {
    "temperature": 0.7, "topP": 0.8,
    "presencePenalty": 0.0, "frequencyPenalty": 0.0, "maxTokens": 8192,
}
MODEL_OUTPUT_MIN, MODEL_OUTPUT_MAX = 256, 8192
# 输出预算最多占窗口 3/4：输入至少留 1/4（且不低于 512 token）。
# 「最大回复长度」调大到窗口上限时，也只会拿到这个上限，不会挤掉输入空间。
MODEL_INPUT_RESERVE_RATIO = 0.25
MODEL_INPUT_RESERVE_MIN = 512

_COMPRESS_Q: "queue.Queue[tuple[int, str, str]]" = queue.Queue()
_COMPRESS_SEEN: set[tuple[int, str, str]] = set()
_COMPRESS_LOCK = threading.Lock()
_COMPRESS_STARTED = False


def _compress_worker() -> None:
    """单线程后台压缩：串行化，避免多会话同时打爆默认模型 / SQLite。"""
    while True:
        uid, sid, cid = _COMPRESS_Q.get()
        try:
            Handler._maybe_summarize(
                uid, sid, cid,
                keep=max(2, Handler._compress_keep_rounds(uid, cid) * 2))
        except Exception as exc:                     # noqa: BLE001
            print(f"[compress] 后台压缩失败：{exc}", file=sys.stderr)
        finally:
            with _COMPRESS_LOCK:
                _COMPRESS_SEEN.discard((uid, sid, cid))
            _COMPRESS_Q.task_done()


def _start_compress_worker() -> None:
    global _COMPRESS_STARTED
    with _COMPRESS_LOCK:
        if _COMPRESS_STARTED:
            return
        _COMPRESS_STARTED = True
    threading.Thread(target=_compress_worker, name="llm-compress",
                     daemon=True).start()


def _stream_key(uid: int | None, sid: str) -> str:
    return f"{int(uid or 0)}:{str(sid or '')}"


def _register_stream(uid: int | None, sid: str) -> threading.Event:
    ev = threading.Event()
    with _ACTIVE_STREAMS_LOCK:
        _ACTIVE_STREAMS[_stream_key(uid, sid)] = ev
    return ev


def _unregister_stream(uid: int | None, sid: str, ev: threading.Event) -> None:
    with _ACTIVE_STREAMS_LOCK:
        key = _stream_key(uid, sid)
        if _ACTIVE_STREAMS.get(key) is ev:
            _ACTIVE_STREAMS.pop(key, None)


def _cancel_stream(uid: int | None, sid: str = "") -> bool:
    """停止指定会话（或该用户全部）的生成；返回是否命中正在跑的流。"""
    hit = False
    with _ACTIVE_STREAMS_LOCK:
        if sid:
            key = _stream_key(uid, sid)
            ev = _ACTIVE_STREAMS.get(key)
            if ev is not None:
                ev.set()
                llm.close_stream(key)
                hit = True
        else:
            prefix = f"{int(uid or 0)}:"
            for key, ev in _ACTIVE_STREAMS.items():
                if key.startswith(prefix):
                    ev.set()
                    llm.close_stream(key)
                    hit = True
    return hit


def _mirror_status() -> dict:
    """读 mirror.py 写进库的进度；子进程已不在则视为没在跑，
    避免它崩掉后库里残留的 running=True 把进度条永久卡住。"""
    st = dict(_MIRROR_DEFAULT)
    st.update(db.get_config("mirror_state") or {})
    with _MIRROR_LOCK:
        proc = _MIRROR_PROC
    if st.get("running") and (proc is None or proc.poll() is not None):
        st["running"] = False
        if not st.get("finished_at"):
            st["phase"] = "error"
            st["message"] = st.get("message") or "镜像进程已退出"
    return st


def _mirror_start(surface: str, limit: int, username: str = "", password: str = "") -> str | None:
    """用子进程拉起 mirror.py 的镜像引擎。成功返回 None，失败返回错误文案。"""
    global _MIRROR_PROC
    with _MIRROR_LOCK:
        if _MIRROR_PROC is not None and _MIRROR_PROC.poll() is None:
            return "已有镜像任务在运行"
        if not (ROOT / "tools" / "mirror.py").exists():
            return "抓取脚本 tools/mirror.py 不存在，手动镜像不可用"
        # 先在库里占位：子进程刚起时还没写进度，不占位前端会看到 running=false 误判完成
        db.set_config("mirror_state", {
            **_MIRROR_DEFAULT, "running": True, "surface": surface,
            "phase": "start", "message": "启动镜像",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        args = [sys.executable, str(ROOT / "tools" / "mirror.py"),
                "mirror", surface, str(limit)]
        if username and password:
            args += [username, password]
        try:
            _MIRROR_PROC = subprocess.Popen(
                args, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:  # noqa: BLE001
            _MIRROR_PROC = None
            db.set_config("mirror_state", {
                **_MIRROR_DEFAULT, "phase": "error", "message": f"启动失败：{exc}",
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            return f"启动失败：{exc}"
    return None


def is_content_path(path: str) -> bool:
    if not path.startswith(CONTENT_PREFIXES):
        return False
    low = path.lower()
    return not any(x in low for x in CONTENT_EXCLUDE)

SESSION_COOKIE = "local_session"

# ---------------------------------------------------------------- CG 平台默认规则
# 创作页「平台默认规则」下拉对应的规则文案，hintRule 接口原样返回给前端展示；
# 同时也是 _match_cg 的实际打法：备注（画面描述）权重最高，标签次之，关键词最低。
CG_PLATFORM_RULE = (
    "平台默认规则：按 AI 回复与图片设定的重合度自动挑选最匹配的一张。"
    "备注（画面描述）参与匹配权重最高——写画面里有什么（人物、动作、环境、镜头），"
    "不要写这张图什么时候用；其次是角色 / 场景 / 情绪 / 动作标签；"
    "触发关键词权重最低，只在多张图都命中时用于择优。"
)
CG_WEIGHT_DESC = 3      # 备注 / 画面描述
CG_WEIGHT_TAG = 2       # 角色 / 场景 / 情绪 / 动作标签
CG_WEIGHT_KEYWORD = 1   # 触发关键词
CG_IMAGE_POINTS = 1     # 回复里实际带图时额外扣的积分
# 每个角色的权重是全局配置（对名下所有 CG 生效），随 CG JSON 的 weights 节点导入导出
DEFAULT_CG_WEIGHTS = {
    "description": CG_WEIGHT_DESC,
    "tag": CG_WEIGHT_TAG,
    "keyword": CG_WEIGHT_KEYWORD,
}


def _norm_cg_weights(raw) -> dict:
    """把 weights 归一成 {description, tag, keyword}，缺失项用默认值，范围 0-20。"""
    out = dict(DEFAULT_CG_WEIGHTS)
    if isinstance(raw, dict):
        for k in out:
            try:
                v = int(raw.get(k))
            except (TypeError, ValueError):
                continue
            out[k] = max(0, min(20, v))
    return out


def _cg_words(v) -> list[str]:
    """标签 / 关键词统一成字符串列表（兼容库里偶发的字符串写法）。"""
    if isinstance(v, list):
        return [str(x) for x in v if x]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def _cg_tag_hit(tag: str, text: str) -> bool:
    """标签命中：完整子串优先；两字标签允许中间夹一个字（「抱书」命中「抱着书」）。
    三字以上标签仍要求完整出现，避免「樱花」误命中「樱花书屋」。"""
    if tag in text:
        return True
    if len(tag) != 2 or tag[0] not in text or tag[1] not in text:
        return False
    i = text.find(tag[0])
    while i != -1:
        j = text.find(tag[1], i + 1)
        if j != -1 and j - i <= 2:
            return True
        i = text.find(tag[0], i + 1)
    return False


def _cg_score(img: dict, text: str, weights: dict | None = None) -> int:
    """单张 CG 按 weights 打分：备注 / 标签 / 关键词命中即累加。"""
    w = weights or DEFAULT_CG_WEIGHTS
    score = 0
    desc = img.get("description") or ""
    if isinstance(desc, str) and desc and desc in text:
        score += w.get("description", CG_WEIGHT_DESC)
    tags = []
    for f in ("characterTags", "sceneTags", "emotionTags", "actionTags"):
        tags += _cg_words(img.get(f))
    for t in tags:
        if _cg_tag_hit(t, text):
            score += w.get("tag", CG_WEIGHT_TAG)
    for kw in _cg_words(img.get("keywords")):
        if kw in text:
            score += w.get("keyword", CG_WEIGHT_KEYWORD)
    return score


def _match_cg(text, cg_list, weights=None):
    """根据文本，按权重匹配最合适的 CG 图；同分按 priority 降序。"""
    if not cg_list:
        return None
    w = weights or DEFAULT_CG_WEIGHTS
    results = []
    for img in cg_list:
        if not img.get("isActive", False):
            continue
        score = _cg_score(img, text, w)
        if score > 0:
            results.append({"image": img, "score": score})
    if not results:
        return None
    results.sort(key=lambda x: (x["score"], x["image"].get("priority", 0)),
                 reverse=True)
    return results[0]["image"]


# 登录后才会调、且只有线上账号才有的接口 → 本地会话直接回桩，不再外发
AUTH_STUB_PREFIXES = (
    "/api/gva/pointsAcc/",
    "/api/gva/pointsTrans/",
    "/api/gva/spiritStone/",
    "/api/gva/invitation/",
    "/api/gva/authorRebate/",
    "/api/gva/rechargeOrder/",
    "/api/gva/email/getVerificationStatus",
    "/api/gva/narratium/content-preferences",
    "/api/gva/narratium/scoreRanking/user/",
)


def stub_payload(path: str, user_id: int | None) -> bytes:
    """按接口给出结构合理的本地桩数据（保证前端字段可用）"""
    p = path.lower()
    if "getuserpointsaccount" in p:
        balance = db.points_balance(user_id) if user_id else db.DEFAULT_POINTS
        growth = db.user_level_info(user_id) if user_id else db.level_info(0)
        data = {
            "success": True, "userLevelIsValid": True, "userLevel": growth["level"],
            "ID": user_id, "userId": user_id, "balance": balance, "points": balance,
            "expPointsBalance": growth["experience"], "combinedBalance": balance,
            "isPaidUser": False, "totalSpent": 0, "totalEarned": 0,
            "userLevelName": growth["levelName"], "userLevelWeight": 1,
            "userExp": growth["experience"], "nextLevelExp": growth["nextLevelExp"],
            "isMaxLevel": growth["isMaxLevel"],
            "expBreakdown": {"fromSpending": 0, "fromVotes": 0, "fromComments": 0},
        }
    elif "findpointsaccount" in p or "checkbalance" in p:
        balance = db.points_balance(user_id) if user_id else db.DEFAULT_POINTS
        data = {"ID": user_id, "userId": user_id, "balance": balance, "points": balance,
                "combinedBalance": balance, "isPaidUser": False}
    elif "content-preferences" in p:
        data = {"likedCategoryKeys": [], "likedTagKeys": [], "blockedTags": [],
                "blockedAuthors": [], "basePolicyVersion": 0}
    elif "getverificationstatus" in p:
        data = {"email": "", "emailVerified": False, "emailProvider": ""}
    elif "spiritstone" in p:
        data = {"amount": 0, "balance": 0, "total": 0, "spiritStone": 0}
    elif "list" in p or "stats" in p or "ranking" in p:
        data = {"list": [], "records": [], "total": 0, "page": 1, "pageSize": 10}
    else:
        data = {}
    return json.dumps({"code": 0, "msg": "本地模式", "data": data},
                      ensure_ascii=False).encode("utf-8")


def safe(base: Path, rel: str) -> Path | None:
    """防目录穿越"""
    try:
        target = (base / rel).resolve()
    except Exception:  # noqa: BLE001
        return None
    return target if str(target).startswith(str(base.resolve())) and target.is_file() else None


def page_target(base: Path, name: str) -> Path | None:
    """页面名 → base/<name>.html。

    只接受单段白名单名称，并且要求解析后的绝对路径**恰好**落在 base 目录内
    （父目录必须等于 base），因此 `<name>.html` 之外的文件、子目录与穿越路径
    全部拒绝；文件不存在也返回 None（未知名称交给调用方回 404）。
    """
    if not PAGE_NAME_RE.match(name):
        return None
    target = safe(base, name + ".html")
    if target is None or target.parent != base.resolve():
        return None
    return target


def rewrite(text: str) -> str:
    """把响应体里的跨域绝对地址改写成本地路径（图片走 /cdn 反代，接口走同源）"""
    return (text
            .replace("https://store.story6688.com", "/cdn")
            .replace("https://images.comfyai.site", "/cdn2")
            .replace("https://missai01.com", ""))


def _rewrite_imgs(obj) -> None:
    """递归把 JSON 里的跨域图片/接口地址改写成本地路径（就地修改）"""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str) and ("http" in v):
                obj[k] = rewrite(v)
            else:
                _rewrite_imgs(v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str) and ("http" in v):
                obj[i] = rewrite(v)
            else:
                _rewrite_imgs(v)


CONSOLE_ENTRY_TAG = '<script src="/app/js/console-entry.js" defer></script>'
HIDE_ADS_TAG = '<script src="/app/js/hide-ads.js" defer></script>'
FAULT_TOLERANCE_TAG = '<script src="/app/js/fault-tolerance.js"></script>'
EMPTY_CLEANER_TAG = '<script src="/app/js/empty-cleaner.js"></script>'
PUBLISH_JUMP_TAG = '<script src="/app/js/publish-jump.js"></script>'
# 探索页首页 feed 缓存屏蔽：不带 defer，保证在站点 chunk（defer/模块）执行前
# 就把 localStorage 的 missai:home-feed:* 读写截断掉，页面直读本地库
NO_FEED_CACHE_TAG = '<script src="/app/js/no-feed-cache.js"></script>'


def inject_local_scripts(html: str, admin: bool) -> str:
    """在页面 </body> 前插本地脚本（磁盘上的 HTML 保持原样，重跑 mirror.py 也不会丢）：

    - `hide-ads.js`：所有人 —— 隐藏登录后搜索栏上方的促销广告轮播条
    - `no-feed-cache.js`：所有人 —— 关掉原站首页 feed 的 localStorage 快照缓存，
      探索页（热力橱窗 最新/24H/周榜/月榜）每次都从本地 SQLite 接口取，
      清空数据库后界面会立刻变空，不再残留浏览器里的旧卡片
    - `fault-tolerance.js`：所有人 —— 内容接口 502/失败时改写为空成功，页面渲染空态而非无限加载
    - `empty-cleaner.js`：所有人 —— 登录态空数据区块（如热力橱窗）骨架占位改为"暂无内容"空态
    - `console-entry.js`：仅管理员 —— 左侧导航栏加「控制台」按钮
    （AI 绘图不再注入 `gallery-link.js`，侧边栏「AI绘图/我的图库」保持原站链接，走原站页面）
    """
    parts = []
    if NO_FEED_CACHE_TAG not in html:
        parts.append(NO_FEED_CACHE_TAG)
    if HIDE_ADS_TAG not in html:
        parts.append(HIDE_ADS_TAG)
    if FAULT_TOLERANCE_TAG not in html:
        parts.append(FAULT_TOLERANCE_TAG)
    if EMPTY_CLEANER_TAG not in html:
        parts.append(EMPTY_CLEANER_TAG)
    if PUBLISH_JUMP_TAG not in html:
        parts.append(PUBLISH_JUMP_TAG)
    if admin and CONSOLE_ENTRY_TAG not in html:
        parts.append(CONSOLE_ENTRY_TAG)
    if not parts:
        return html
    i = html.lower().rfind("</body>")
    if i < 0:
        return html + "".join(parts)
    return html[:i] + "".join(parts) + html[i:]


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0：每条连接一个请求，避免 POST body 残留污染下一请求行（HTTP/1.1 keep-alive 下
    # 某些本地兜底分支没消费完 Content-Length，会把 body 拼进下一请求 → 畸形 method → 501 崩页）
    protocol_version = "HTTP/1.0"
    server_version = "MissAI-Local/1.0"

    # ---------------------------------------------------------- 生命周期
    def log_message(self, fmt, *args):  # 只在 4xx/5xx 时打印，避免刷屏
        if args and len(args) > 1 and str(args[1]).startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_response(self, code, message=None):
        self.response_status = code
        super().send_response(code, message)

    # ---------------------------------------------------------- 基础发送
    def _finish_log(self, note: str = "") -> None:
        duration = (time.time() - getattr(self, "_t0", time.time())) * 1000
        db.log_request(
            method=self.command,
            path=self.path[:500],
            status=getattr(self, "response_status", None),
            duration_ms=duration,
            user_id=getattr(self, "_user_id", None),
            ip=self.client_address[0] if self.client_address else "",
            size=getattr(self, "_bytes", 0),
            note=note or getattr(self, "_note", ""),
        )

    def send_file(self, path: Path, status: int = 200, ctype: str | None = None) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            return self.not_found()
        # 站点页面（控制台页除外）：回包时插本地脚本（隐藏广告轮播 + 管理员的控制台入口）
        if path.suffix == ".html" and path.name != "console.html":
            admin = bool(self.admin_session())
            try:
                text = data.decode("utf-8")
                injected = inject_local_scripts(text, admin)
                if injected != text:
                    data = injected.encode("utf-8")
            except UnicodeDecodeError:
                pass
        ctype = ctype or MIME.get(path.suffix.lower(), "application/octet-stream")
        # 页面里出现的图片（静态图 / 动图）→ 一律登记进图片库，记下本地实际路径
        if path.suffix.lower() in imagedb.IMAGE_EXT:
            try:
                if imagedb.lookup(self.path) is None:
                    imagedb.register(self.path, path, "page")
                else:
                    imagedb.touch(self.path)
            except Exception:  # noqa: BLE001
                pass
        low = path.suffix.lower()
        if low in (".html", ".js", ".mjs", ".css"):
            # 页面与脚本必须每次回源校验（本地改过 js 后要能立刻生效）
            cache = "no-cache"
        elif low in (".woff", ".woff2", ".ttf"):
            cache = "public, max-age=604800"          # 7 天，交给浏览器
        else:
            cache = "public, max-age=86400"
        self._bytes = len(data)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, obj, status: int = 200, headers: dict | None = None) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._bytes = len(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def not_found(self) -> None:
        body = b"404 Not Found"
        self._bytes = len(body)
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _method_not_allowed(self) -> None:
        """非 GET/HEAD 打到了只做 GET 的页面路由 → 本地回 405（不连上游）。

        上游对页面路由的非 GET 方法回 501「Unsupported method」，
        405 语义更准确，也避免把上游整页错误体透传给前端。
        """
        self._note = "method-not-allowed"
        msg = f"405 {self.command} {self.path[:200]} 不支持（该路由仅 GET）".encode("utf-8", "replace")
        self._bytes = len(msg)
        self.send_response(405)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(msg)))
        self.send_header("Allow", "GET, HEAD")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(msg)

    def _server_error(self, exc: BaseException) -> None:
        """接口处理抛异常 → 500 + 可直接显示的原因（不再伪装成 405）"""
        self._note = "server-error"
        msg = json.dumps({
            "code": 1,
            "msg": f"本地接口异常：{type(exc).__name__}: {exc}"[:400],
            "data": None,
        }, ensure_ascii=False).encode("utf-8")
        try:
            self._bytes = len(msg)
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(msg)
        except Exception:  # noqa: BLE001
            pass

    def read_body(self) -> bytes:
        """读请求体 —— **可重复调用**（结果缓存住）。

        本地分派先读一次 body，若最终没接住还要反代，第二次 read 会卡在
        `rfile.read(length)` 上等永远不来的字节 → 该连接挂死（浏览器转圈、
        别的请求排队）。缓存后重读直接拿到第一次的字节。
        """
        if getattr(self, "_body_read", False):
            return self._body_cache
        self._body_read = True
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)
        elif (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            body = self._read_chunked()
        else:
            body = b""
        self._body_cache = body
        return body

    def _read_chunked(self) -> bytes:
        chunks = []
        while True:
            line = self.rfile.readline().strip()
            if b";" in line:
                line = line.split(b";", 1)[0]
            try:
                size = int(line, 16)
            except ValueError:
                break
            if size == 0:
                self.rfile.readline()
                break
            chunks.append(self.rfile.read(size))
            self.rfile.readline()
        return b"".join(chunks)

    def cookies(self) -> dict:
        raw = self.headers.get("Cookie") or ""
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def current_session(self) -> dict | None:
        token = self.cookies().get(SESSION_COOKIE) or self.headers.get("x-token") or ""
        return db.get_session(token)

    def admin_session(self) -> dict | None:
        """只有 role=admin 的本地会话才算控制台管理员"""
        sess = self.current_session()
        if sess and str(sess.get("role") or "") == "admin":
            return sess
        return None

    # ================================================== 控制台登录（仅管理员）
    CONSOLE_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>控制台登录 · MissAI 镜像</title>
  <link rel="stylesheet" href="/app/css/console.css">
</head>
<body>
  <div id="local-console-login" class="login-box">
    <div class="brand"><span class="dot"></span>
      <div class="brand-text"><h1>本地控制台</h1><span class="sub">SQLite · data/missai.db</span></div>
    </div>
    <p class="muted">控制台仅限<b>管理员账号</b>访问，请先登录。</p>
    <form id="cLogin">
      <input id="c-user" placeholder="管理员用户名" autocomplete="username">
      <input id="c-pass" type="password" placeholder="密码" autocomplete="current-password">
      <button type="submit">登录</button>
      <p class="alert err" id="c-msg" hidden></p>
    </form>
    <a class="home" href="/home">← 返回站点</a>
  </div>
  <script>
    document.getElementById('cLogin').addEventListener('submit', async (e) => {
      e.preventDefault();
      const msg = document.getElementById('c-msg');
      const id = document.getElementById('c-user').value.trim();
      const pw = document.getElementById('c-pass').value;
      if (!id || !pw) { msg.hidden = false; msg.textContent = '请输入账号和密码'; return; }
      msg.hidden = false; msg.textContent = '登录中…';
      try {
        const r = await fetch('/local/api/login', {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: id, password: pw }),
        });
        const data = await r.json();
        if (r.ok && data.code === 0) { location.reload(); return; }
        msg.textContent = data.msg || '登录失败';
      } catch (err) { msg.textContent = '请求失败：' + err; }
    });
  </script>
</body>
</html>
"""

    def send_console_login(self) -> None:
        data = self.CONSOLE_LOGIN_HTML.encode("utf-8")
        self._bytes = len(data)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self._note = "console-login-page"

    def handle_console_login(self) -> None:
        """POST /local/api/login —— 校验账号密码，且必须是管理员"""
        body = self.read_body()
        try:
            d = json.loads(body.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            d = {}
        ident = str(d.get("username") or d.get("userName") or d.get("email") or "").strip()
        pw = str(d.get("password") or d.get("passWord") or "")
        user = db.verify_user(ident, pw) if ident and pw else None
        if not user or str(user.get("role") or "") != "admin":
            self._note = "console-login-denied"
            return self.send_json({"code": 401, "msg": "账号或密码错误，或不是管理员",
                                   "data": None}, status=401)
        sess = db.create_session(user["id"], self.headers.get("User-Agent", ""),
                                 self.client_address[0])
        self._user_id = user["id"]
        self._note = "console-login"
        return self.send_json(
            {"code": 0, "msg": "登录成功", "data": {"user": db.public_user(user)}},
            headers={"Set-Cookie": f"{SESSION_COOKIE}={sess['token']}; Path=/; HttpOnly; "
                                   f"SameSite=Lax; Max-Age={30 * 86400}"},
        )

    # ================================================== 本地账号 / 会话
    def handle_session(self, path: str, query: str) -> None:
        sess = self.current_session()
        if self.command in ("GET", "HEAD"):
            if not sess:
                # bootstrap 未登录对齐线上（401）；/api/auth/session 保持 200，
                # 前端 store.js 直接读 authenticated 字段
                if path.endswith("/bootstrap"):
                    return self.send_json({"authenticated": False}, status=401)
                return self.send_json({"authenticated": False}, status=200)
            return self.send_json({
                "authenticated": True,
                "expiresAt": int(time.time() * 1000) + 30 * 86400_000,
                "user": db.public_user(sess),
            })

        # POST /api/auth/session/establish  —— 前端用 x-token 换会话
        token = self.headers.get("x-token") or self.cookies().get(SESSION_COOKIE) or ""
        sess = db.get_session(token)
        if not sess:
            return self.send_json(
                {"authenticated": False, "reason": "missing-or-invalid-token"}, status=400)
        self.send_json({
            "authenticated": True,
            "expiresAt": int(time.time() * 1000) + 30 * 86400_000,
            "user": db.public_user(sess),
        })

    def handle_login(self, path: str) -> None:
        raw = self.read_body()
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            data = {}

        def pick(*keys: str) -> str:
            for k in keys:
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return ""

        identifier = pick("userName", "username", "email", "account", "phone", "identifier", "mobile")
        password = pick("passWord", "password", "pwd")
        nickname = pick("nickName", "nickname", "name") or identifier
        email = pick("email")
        is_register = "register" in path

        if not identifier or not password:
            return self.send_json({"code": 1, "msg": "用户名和密码不能为空", "data": None})

        # 数字加减法验证码：请求里带了就判分
        cid = pick("captchaId")
        if cid:
            if not captcha.verify(cid, data.get("captcha")):
                self._note = "captcha-fail"
                return self.send_json({"code": 1, "msg": "验证码算错了，请重试", "data": None})

        if is_register:
            user = db.create_user(identifier, password, nickname, email)
            if not user:
                return self.send_json({"code": 1, "msg": "用户名或邮箱已被占用", "data": None})
        else:
            user = db.verify_user(identifier, password)
            if not user:
                return self.send_json({"code": 1, "msg": "用户名或密码错误", "data": None})

        sess = db.create_session(user["id"], self.headers.get("User-Agent", ""),
                                 self.client_address[0])
        self._user_id = user["id"]
        self._note = "login" if not is_register else "register"
        self.send_json(
            {"code": 0, "msg": "登录成功", "data": {"token": sess["token"], "user": db.public_user(user)}},
            headers={"Set-Cookie": f"{SESSION_COOKIE}={sess['token']}; Path=/; HttpOnly; "
                                   f"SameSite=Lax; Max-Age={30 * 86400}"},
        )

    def handle_logout(self) -> None:
        """本地退出登录：删掉会话记录，并让浏览器立刻丢弃 HttpOnly 会话 Cookie"""
        self.read_body()
        token = self.cookies().get(SESSION_COOKIE) or self.headers.get("x-token") or ""
        if token:
            db.drop_session(token)
        self._user_id = None
        self._note = "logout"
        self.send_json(
            {"code": 0, "msg": "已退出登录", "data": None},
            headers={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; "
                                   f"SameSite=Lax; Max-Age=0"},
        )

    def handle_code(self, path: str) -> None:
        """邮件/短信类验证码接口：本地直接放行，不真的外发"""
        self.read_body()
        self._note = "local-code"
        self.send_json({"code": 0, "msg": "本地模式：验证码已通过", "data": {}})

    def handle_captcha_challenge(self) -> None:
        """出一道数字加减法题（digit 模式 + PNG 题图）"""
        self.read_body()
        cid, uri = captcha.new_challenge()
        self._note = "captcha-issue"
        self.send_json({
            "code": 0,
            "msg": "",
            "data": {
                "captchaId": cid,
                "picPath": uri,
                "captchaLength": 3,
                "openCaptcha": True,
                "captchaMode": "digit",
            },
        })

    def handle_captcha_verify(self) -> None:
        """校验算式答案"""
        try:
            data = json.loads(self.read_body().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            data = {}
        ok = captcha.verify(str(data.get("captchaId") or ""), data.get("captcha"))
        self._note = "captcha-verify"
        if ok:
            self.send_json({"code": 0, "msg": "验证成功",
                            "data": {"success": True, "retries": 0}})
        else:
            self.send_json({"code": 1, "msg": "算错了，请重试",
                            "data": {"success": False, "reason": "mismatch", "retries": 1}})

    # ================================================== CDN 图片 → 本地图片库
    CTYPE_EXT = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "image/svg+xml": ".svg", "image/avif": ".avif",
        "image/x-icon": ".ico", "image/bmp": ".bmp",
    }

    def handle_cdn_image(self, base: str, rest: str) -> None:
        """CDN 图片：库里有 → 直接读本地；没有 → 抓一次存进 html/image/ 再读"""
        url_key = self.path                      # /cdn/xxx 或 /cdn2/xxx
        query = urlparse(self.path).query
        local = imagedb.resolve(url_key)
        if local:
            self._note = "image-cdn-local"
            return self.send_file(local)

        upstream = base + rest + ("?" + query if query else "")
        parsed = urlparse(upstream)
        conn = http.client.HTTPSConnection(parsed.hostname, 443, timeout=60)
        try:
            conn.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""),
                         headers={"User-Agent": UA, "Accept-Encoding": "identity",
                                  "Connection": "close"})
            resp = conn.getresponse()
            data = resp.read()
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip()
        except Exception as exc:  # noqa: BLE001
            conn.close()
            return self._upstream_error(f"图片抓取失败: {exc}")
        conn.close()

        if resp.status >= 400 or not data:
            self._note = "image-upstream-miss"
            self.send_response(resp.status if resp.status < 500 else 502)
            body = b"image not found"
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._bytes = len(body)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return

        ext = Path(parsed.path).suffix.lower()
        if ext not in imagedb.IMAGE_EXT:
            ext = self.CTYPE_EXT.get(ctype, ".bin")
        name = hashlib.sha1(url_key.encode("utf-8")).hexdigest()[:24] + ext
        target = IMAGE_DIR / name
        try:
            IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            imagedb.register(url_key, target, "cdn")
            self._note = "image-cdn-cache"
            return self.send_file(target, ctype=ctype or None)
        except OSError:
            return self._upstream_error("图片落盘失败")

    # ================================================== 上传图片
    def handle_upload(self, path: str) -> None:
        rel = unquote(path[len("/api/uploads/file/"):]).lstrip("/")
        name = re.sub(r"[^A-Za-z0-9._\-/]", "_", rel) or f"upload-{int(time.time())}"
        name = name.lstrip("/")
        body = self.read_body()
        if not body:
            return self.send_json({"code": 1, "msg": "空文件", "data": None})

        # 上传的图片统一落到 html/image/，并登记进图片库
        target = IMAGE_DIR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        sha = hashlib.sha256(body).hexdigest()
        row = imagedb.register(path, target, "upload")
        db.add_upload("image", str(target.relative_to(ROOT)).replace("\\", "/"),
                      remote_url=path, sha=sha, size=len(body))
        self._note = "upload"
        self.send_json({
            "code": 0,
            "msg": "已保存到本地",
            "data": {
                "url": path,
                "path": (row or {}).get("local_path", f"html/image/{name}"),
                "absPath": (row or {}).get("abs_path", str(target)),
                "imageId": (row or {}).get("id"),
                "kind": (row or {}).get("kind"),
                "animated": bool((row or {}).get("animated")),
                "bytes": len(body),
                "sha256": sha,
                # 设置页头像上传读的是 data.file.url（GVAAuthAPI 上传约定）
                "file": {
                    "url": path,
                    "name": name,
                    "size": len(body),
                    "path": (row or {}).get("local_path", f"html/image/{name}"),
                },
            },
        })

    # ================================================== 探索页（公开）
    def handle_explore(self, query: str) -> None:
        """GET /api/app/explore —— 探索页数据：本地内容库 works 表。

        支持 q（名称/简介/作者模糊搜索）、tag（标签包含）、sort（latest/hot/rate）、
        limit/offset 分页；返回卡片字段 + 标签候选，未登录也能看。
        """
        params = {k: v[0] for k, v in parse_qs(query).items()}
        limit = max(1, min(self._int(params.get("limit") or 48, 48), 100))
        offset = max(0, min(self._int(params.get("offset") or 0, 0), 100000))
        q = str(params.get("q") or "").strip()
        tag = str(params.get("tag") or "").strip()
        sort = str(params.get("sort") or "latest")
        order = {"hot": "votes DESC, updated_at DESC",
                 "rate": "rate DESC, votes DESC, updated_at DESC"}.get(
                     sort, "updated_at DESC")
        where = "name <> ''"
        args: list = []
        if q:
            where += " AND (name LIKE ? OR summary LIKE ? OR author_name LIKE ?)"
            args += ["%" + q + "%"] * 3
        if tag:
            where += " AND tags LIKE ?"
            args.append("%" + tag + "%")
        total = (db.one(f"SELECT COUNT(*) AS c FROM works WHERE {where}",
                        tuple(args)) or {}).get("c", 0)
        rows = db.query(
            "SELECT character_id, name, summary, author_name, cover_url, tags,"
            " rate, votes, updated_at FROM works WHERE " + where +
            " ORDER BY " + order + " LIMIT ? OFFSET ?",
            tuple(args + [limit, offset]))
        items = []
        for r in rows:
            try:
                tags = json.loads(r.get("tags") or "[]")
            except Exception:  # noqa: BLE001
                tags = []
            items.append({
                "characterId": r.get("character_id") or "",
                "name": r.get("name") or "",
                "summary": r.get("summary") or "",
                "author": r.get("author_name") or "",
                "cover": r.get("cover_url") or "",
                "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
                "rate": r.get("rate") or 0,
                "votes": r.get("votes") or 0,
                "updatedAt": r.get("updated_at") or "",
            })
        self.send_json({"code": 0, "msg": "", "data": {
            "list": items, "total": total, "limit": limit, "offset": offset,
            "tags": db.top_tags(24),
        }})

    # ================================================== 本地控制台 API
    def handle_local_api(self, path: str, query: str) -> None:
        params = {k: v[0] for k, v in parse_qs(query).items()}
        if path.startswith("/local/api/db/"):
            return self.handle_db_admin(path, params)
        if path.startswith("/local/api/creation"):
            return self.handle_creation(path, params)
        if path == "/local/api/stats":
            s = db.stats()
            s.update(imagedb.stats())
            s["image_dir"] = str(imagedb.IMAGE_DIR)
            return self.send_json(s)

        if path == "/local/api/logs":
            limit = min(int(params.get("limit", 200)), 1000)
            offset = int(params.get("offset", 0))
            return self.send_json(db.query(
                "SELECT * FROM request_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)))

        if path == "/local/api/prompt-logs":     # 只读：分段统计 + 本轮 diff
            uid = params.get("userId") or None
            return self.send_json(db.recent_chat_prompts(
                int(uid) if uid else None,
                params.get("sessionId") or "",
                int(params.get("limit", 10) or 10)))

        if path == "/local/api/users":
            rows = db.query(
                "SELECT id, user_name, nick_name, email, role, header_img, created_at, last_login_at"
                " FROM users ORDER BY id DESC LIMIT 200")
            return self.send_json(rows)

        if path == "/local/api/sessions":
            return self.send_json(db.query(
                "SELECT token, user_id, created_at, expires_at, ip, user_agent FROM sessions"
                " ORDER BY created_at DESC LIMIT 100"))

        # ---- 用户积分 / 经验管理 ----
        if path == "/local/api/points":
            limit = min(int(params.get("limit", 200) or 200), 500)
            uid = self._int(params.get("userId"), 0)
            return self.send_json({
                "summary": db.points_admin_summary(),
                "users": db.points_admin_rows(params.get("q", ""), limit),
                "ledger": db.points_ledger_rows(40, uid),
            })

        if path == "/local/api/points/adjust" and self.command == "POST":
            body = self._json_body()
            uid = self._int(body.get("userId"), 0)
            if not uid:
                return self.send_json({"code": 1, "msg": "请选择要调整的账号", "data": None},
                                      status=400)
            points_delta = self._int(body.get("pointsDelta"), 0)
            exp_delta = self._int(body.get("expDelta"), 0)
            reason = str(body.get("reason") or "")
            if not points_delta and not exp_delta:
                return self.send_json({"code": 1, "msg": "积分与经验调整都为 0，无需保存",
                                       "data": None}, status=400)
            row = db.admin_adjust_account(uid, points_delta, exp_delta, reason)
            if not row:
                return self.send_json({"code": 1, "msg": "账号不存在", "data": None},
                                      status=404)
            return self._local_ok(row, "points-adjust")

        # ---- 成长 / 计费规则管理 ----
        if path == "/local/api/rules":
            if self.command == "POST":
                body = self._json_body()
                payload = body.get("rules") if isinstance(body.get("rules"), dict) else body
                return self._local_ok(db.save_rules(payload), "rules-saved")
            return self.send_json({"rules": db.get_rules(),
                                   "defaults": db.default_rules()})

        if path == "/local/api/rules/reset" and self.command == "POST":
            return self._local_ok(db.reset_rules(), "rules-reset")

        # ---- 消耗管理：用户 / 作品 的 token 与积分消耗统计 ----
        if path == "/local/api/consumption":
            return self.send_json(db.consumption_dashboard(
                params.get("range") or "daily",
                self._int(params.get("limit"), 200)))

        if path == "/local/api/favorites":
            if self.command == "POST":
                body = self.read_body()
                try:
                    d = json.loads(body.decode("utf-8") or "{}")
                except Exception:  # noqa: BLE001
                    d = {}
                sess = self.current_session()
                uid = sess["id"] if sess else None
                kind = d.get("kind", "character")
                target = str(d.get("target_id", ""))
                created = db.add_favorite(uid, kind, target, d.get("title", ""),
                                          json.dumps(d, ensure_ascii=False))
                if created and uid:
                    db.award_exp_once(uid, "like" if kind == "like" else "favorite", target)
                return self.send_json({"code": 0, "ok": True})
            if self.command == "DELETE":
                sess = self.current_session()
                uid = sess["id"] if sess else None
                db.remove_favorite(uid, params.get("kind", "character"),
                                   params.get("target_id", ""))
                return self.send_json({"code": 0, "ok": True})
            return self.send_json(db.query(
                "SELECT * FROM favorites ORDER BY id DESC LIMIT 300"))

        if path == "/local/api/chat":
            return self.send_json(db.query(
                "SELECT m.id, m.thread_id, m.role, m.content, m.created_at, t.character_id, t.title"
                " FROM chat_messages m LEFT JOIN chat_threads t ON t.id=m.thread_id"
                " ORDER BY m.id DESC LIMIT 300"))

        if path == "/local/api/uploads":
            return self.send_json(db.query(
                "SELECT * FROM uploads ORDER BY id DESC LIMIT 300"))

        if path == "/local/api/images":
            limit = min(int(params.get("limit", 1000)), 1000)
            offset = min(int(params.get("offset", 0)), 100000)
            rows = imagedb.list_images(limit, offset)
            return self.send_json(rows)

        if path == "/local/api/works":
            limit = min(int(params.get("limit", 100)), 1000)
            offset = min(int(params.get("offset", 0)), 100000)
            return self.send_json(db.list_works(limit, offset, params.get("q", "")))

        if path == "/local/api/tags":
            return self.send_json(db.top_tags(min(int(params.get("limit", 200)), 1000)))

        if path == "/local/api/content_apis":
            return self.send_json(db.content_apis())

        # ---- 创意广场（商品 / 源码 / 同步） ----
        if path == "/local/api/creative":
            limit = min(int(params.get("limit", 300)), 1000)
            offset = min(int(params.get("offset", 0)), 100000)
            return self.send_json({
                "stats": db.creative_counts(),
                "items": db.creative_items_page(limit, offset, params.get("q", ""),
                                                params.get("open") in ("1", "true")),
            })

        if path == "/local/api/creative/item":
            row = db.creative_item_row(self._int(params.get("id") or 0, 0))
            if not row:
                return self.send_json({"code": 1, "msg": "商品不存在", "data": None}, status=404)
            return self.send_json(db.creative_item_public(row, include_prompt=True))

        if path == "/local/api/creative/source":
            row = db.creative_item_row(self._int(params.get("id") or 0, 0))
            if not row:
                return self.not_found()
            item = db.creative_item_public(row, include_prompt=True)
            text = item.get("promptContent") or item.get("pairedCssPrompt") or ""
            if not text:
                text = "（本地没有这个商品的源码正文：非开源商品需要线上购买后才会返回）"
            data = text.encode("utf-8")
            self._bytes = len(data)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             f'attachment; filename="creative-{row["id"]}.txt"')
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/local/api/creative/export":
            scope = params.get("scope", "open")
            rows = db.creative_export_rows(open_only=(scope != "all"))
            data = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
            self._bytes = len(data)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             'attachment; filename="creative-plaza-sources.json"')
            self.end_headers()
            self.wfile.write(data)
            return

        # ---- 镜像引擎：子进程跑 mirror.py（不 import），进度读库 ----
        if path == "/local/api/mirror/start" and self.command == "POST":
            body = self._json_body()
            surface = str(body.get("surface") or "works")
            limit = self._int(body.get("limit") or 60, 60)
            username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "").strip()
            err = _mirror_start(surface, limit, username, password)
            if err:
                return self.send_json({"code": 1, "msg": err, "data": _mirror_status()})
            return self.send_json({"code": 0, "msg": "镜像已在后台启动",
                                   "data": _mirror_status()})

        if path == "/local/api/mirror/status" and self.command == "GET":
            return self.send_json({"code": 0, "msg": "ok", "data": _mirror_status()})


        # ---- 模型接入（OpenAI 协议：填 api 地址 + 密钥 → 检测模型 → 聊天用） ----
        if path == "/local/api/llm":
            if self.command == "GET":
                self._note = "llm-list"
                return self.send_json({
                    "presets": [{"key": k, **v} for k, v in db.LLM_PRESETS.items()],
                    "providers": db.list_llm_providers(),
                    "models": db.llm_rate_models(),
                    "timeoutLocal": self._timeout_local_fallback(),
                })
            prov = db.save_llm_provider(self._json_body())
            self._note = "llm-save"
            if not prov:
                return self.send_json({"code": 1, "msg": "请填写 API 地址", "data": None})
            return self.send_json({"code": 0, "msg": "已保存", "data": prov})

        if path == "/local/api/llm/check":       # 保存并连上去拉模型列表
            payload = self._json_body()
            prov = db.save_llm_provider(payload)
            if not prov:
                return self.send_json({"code": 1, "msg": "请填写 API 地址", "data": None})
            cred = db.llm_credentials(prov["id"]) or ("", "")
            try:
                models = llm.list_models(cred[0], cred[1])
            except llm.LLMError as exc:
                prov = db.set_llm_models(prov["id"], [], "error", str(exc))
                kept = len((prov or {}).get("models") or [])
                msg = str(exc) + (f"（已沿用上次检测到的 {kept} 个模型）" if kept else "")
                self._note = "llm-check-failed"
                return self.send_json({"code": 1, "msg": msg, "data": prov})
            prov = db.set_llm_models(prov["id"], models, "ok", "",
                                     str(payload.get("default_model") or ""))
            self._note = "llm-check"
            return self.send_json({"code": 0, "msg": f"检测到 {len(models)} 个聊天模型",
                                   "data": prov})

        if path == "/local/api/llm/select":      # 设为默认接入（聊天用它）
            d = self._json_body()
            prov = db.set_llm_active(d.get("id"), str(d.get("model") or ""))
            self._note = "llm-select"
            if not prov:
                return self.send_json({"code": 1, "msg": "接入不存在", "data": None})
            return self.send_json({"code": 0, "msg": "已设为默认", "data": prov})

        if path == "/local/api/llm/delete":
            d = self._json_body()
            self._note = "llm-delete"
            return self.send_json({"code": 0, "msg": "已删除",
                                   "data": {"deleted": db.delete_llm_provider(d.get("id"))}})

        if path == "/local/api/llm/timeout-local":   # 超时是否自动切本地模式回复
            d = self._json_body()
            enabled = bool(d.get("enabled"))
            db.set_config("llm_timeout_local", {"enabled": enabled})
            self._note = "llm-timeout-local"
            return self.send_json({"code": 0, "msg": "已保存",
                                   "data": {"enabled": enabled}})

        # ---- 绘图模型接入（OpenAI 兼容：/images/generations） ----
        if path == "/local/api/image-models":
            if self.command == "GET":
                self._note = "image-models-list"
                return self.send_json({
                    "presets": [{"key": k, **v} for k, v in db.IMAGE_PRESETS.items()],
                    "providers": db.list_image_providers(),
                    "models": db.image_rate_models(),
                    "works": db.list_image_works("new", 24),
                    "workCount": db.count_image_works(),
                })
            prov = db.save_image_provider(self._json_body())
            self._note = "image-models-save"
            if not prov:
                return self.send_json({"code": 1, "msg": "请填写 API 地址", "data": None})
            return self.send_json({"code": 0, "msg": "已保存", "data": prov})

        if path == "/local/api/image-models/check":     # 保存并连上去拉模型列表
            payload = self._json_body()
            prov = db.save_image_provider(payload)
            if not prov:
                return self.send_json({"code": 1, "msg": "请填写 API 地址", "data": None})
            if prov.get("kind") == "local":
                models = ["local-placeholder"]
            else:
                cred = db.image_credentials(prov["id"]) or ("", "")
                try:
                    models = imagegen.list_models(cred[0], cred[1])
                except imagegen.ImageGenError as exc:
                    prov = db.set_image_models(prov["id"], [], "error", str(exc))
                    kept = len((prov or {}).get("models") or [])
                    msg = str(exc) + (f"（已沿用上次检测到的 {kept} 个绘图模型）" if kept else "")
                    self._note = "image-check-failed"
                    return self.send_json({"code": 1, "msg": msg, "data": prov})
            prov = db.set_image_models(prov["id"], models, "ok", "",
                                       str(payload.get("default_model") or ""))
            self._note = "image-check"
            return self.send_json({"code": 0, "msg": f"检测到 {len(models)} 个绘图模型",
                                   "data": prov})

        if path == "/local/api/image-models/select":    # 设为默认绘图接入
            d = self._json_body()
            prov = db.set_image_active(d.get("id"), str(d.get("model") or ""))
            self._note = "image-select"
            if not prov:
                return self.send_json({"code": 1, "msg": "接入不存在", "data": None})
            return self.send_json({"code": 0, "msg": "已设为默认", "data": prov})

        if path == "/local/api/image-models/delete":
            d = self._json_body()
            self._note = "image-delete"
            return self.send_json({"code": 0, "msg": "已删除",
                                   "data": {"deleted": db.delete_image_provider(d.get("id"))}})

        # ---- 视频模型接入（OpenAI 兼容：GET /models 检测） ----
        if path == "/local/api/video-models":
            if self.command == "GET":
                self._note = "video-models-list"
                return self.send_json({
                    "presets": [{"key": k, **v} for k, v in db.VIDEO_PRESETS.items()],
                    "providers": db.list_video_providers(),
                    "models": db.video_rate_models(),
                })
            prov = db.save_video_provider(self._json_body())
            self._note = "video-models-save"
            if not prov:
                return self.send_json({"code": 1, "msg": "请填写 API 地址", "data": None})
            return self.send_json({"code": 0, "msg": "已保存", "data": prov})

        if path == "/local/api/video-models/check":     # 保存并连上去拉视频模型列表
            payload = self._json_body()
            prov = db.save_video_provider(payload)
            if not prov:
                return self.send_json({"code": 1, "msg": "请填写 API 地址", "data": None})
            if prov.get("kind") == "local":
                models = ["local-video-placeholder"]
            else:
                cred = db.video_credentials(prov["id"]) or ("", "")
                try:
                    models = imagegen.list_models(cred[0], cred[1], hints=db.VIDEO_HINTS)
                except imagegen.ImageGenError as exc:
                    prov = db.set_video_models(prov["id"], [], "error", str(exc))
                    kept = len((prov or {}).get("models") or [])
                    msg = str(exc) + (f"（已沿用上次检测到的 {kept} 个视频模型）" if kept else "")
                    self._note = "video-check-failed"
                    return self.send_json({"code": 1, "msg": msg, "data": prov})
            prov = db.set_video_models(prov["id"], models, "ok", "",
                                       str(payload.get("default_model") or ""))
            self._note = "video-check"
            return self.send_json({"code": 0, "msg": f"检测到 {len(models)} 个视频模型",
                                   "data": prov})

        if path == "/local/api/video-models/select":    # 设为默认视频接入
            d = self._json_body()
            prov = db.set_video_active(d.get("id"), str(d.get("model") or ""))
            self._note = "video-select"
            if not prov:
                return self.send_json({"code": 1, "msg": "接入不存在", "data": None})
            return self.send_json({"code": 0, "msg": "已设为默认", "data": prov})

        if path == "/local/api/video-models/delete":
            d = self._json_body()
            self._note = "video-delete"
            return self.send_json({"code": 0, "msg": "已删除",
                                   "data": {"deleted": db.delete_video_provider(d.get("id"))}})

        if path == "/local/api/image/works":
            limit = min(int(params.get("limit", 60)), 300)
            offset = int(params.get("offset", 0))
            return self.send_json({
                "works": db.list_image_works(params.get("sort", "new"), limit, offset),
                "total": db.count_image_works(),
            })

        if path == "/local/api/image/works/delete":
            d = self._json_body()
            ok = db.delete_image_work(d.get("id"), is_admin=True)
            self._note = "image-work-delete"
            return self.send_json({"code": 0 if ok else 1,
                                   "msg": "已删除" if ok else "作品不存在",
                                   "data": {"deleted": ok}})

        # --- 绘图模板接入（行业通用 JSON：可直接导入别人做的模板）---
        if path == "/local/api/templates":
            if self.command == "GET":
                catalog = mediagen.get_catalog(seed=True)
                categories = catalog.get("categories", [])
                builtin = {r.get("recipeKey")
                           for c in mediagen.load_builtin().get("categories", [])
                           for r in (c.get("recipes") or [])}
                flat = []
                for cat in categories:
                    for recipe in cat.get("recipes") or []:
                        conf = recipe.get("local") if isinstance(recipe.get("local"), dict) else {}
                        flat.append({
                            "recipeKey": recipe.get("recipeKey"),
                            "name": recipe.get("name"),
                            "categoryKey": cat.get("key"),
                            "categoryName": cat.get("name"),
                            "version": recipe.get("version"),
                            "points": (recipe.get("price") or {}).get("points", 0),
                            "fieldCount": len((recipe.get("schema") or {}).get("fields") or []),
                            "mediaType": recipe.get("outputMediaType"),
                            "author": str(conf.get("author") or ""),
                            "origin": "builtin" if recipe.get("recipeKey") in builtin else "imported",
                        })
                return self.send_json({
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "catalog": catalog,
                        "recipes": flat,
                        "totalCategories": len(categories),
                        "totalRecipes": len(flat),
                        "templateDir": str(mediagen.TEMPLATE_DIR.relative_to(ROOT))
                                        .replace("\\", "/"),
                        "templateFiles": mediagen.builtin_sources(),
                    }
                })
            # POST：导入行业通用模板 JSON（mode=merge 按 key 覆盖，replace 整包替换）
            raw = self._json_any()
            if raw is None:
                return self.send_json({"code": 1, "data": None,
                                       "msg": "请求体不是合法 JSON"})
            payload = raw if isinstance(raw, (dict, list)) else {"json": str(raw)}
            mode = str(payload.get("mode") or "merge") if isinstance(payload, dict) else "merge"
            out = tplimport.import_payload(payload, mode)
            report = out["report"]
            if not out.get("saved"):
                return self.send_json({"code": 1, "data": out,
                                       "msg": "没有导入任何模板：" +
                                              "；".join(report["warnings"][:3])})
            self._note = f"templates-imported:{len(report['added'])}"
            detail = f"已导入 {len(report['added'])} 个新模板"
            if report["replaced"]:
                detail += f"，覆盖 {len(report['replaced'])} 个同名模板"
            return self.send_json({
                "code": 0,
                "msg": f"{detail}；当前共 {report['recipes']} 个模板",
                "data": out,
            })

        # 只适配不落库：导入前先看会被裁成什么样
        if path == "/local/api/templates/preview" and self.command == "POST":
            raw = self._json_any()
            if raw is None:
                return self.send_json({"code": 1, "data": None,
                                       "msg": "请求体不是合法 JSON"})
            payload = raw if isinstance(raw, (dict, list)) else {"json": str(raw)}
            return self.send_json({"code": 0, "msg": "ok",
                                   "data": tplimport.preview_payload(payload)})

        # 导出当前目录（给别人拿去改）：直接回一份通用模板清单
        if path == "/local/api/templates/export" and self.command == "GET":
            catalog = mediagen.get_catalog(seed=True)
            return self.send_json({"code": 0, "msg": "ok", "data": {
                "schema": "missai.template/v1",
                "exportedAt": db.now_iso(),
                "categories": catalog.get("categories", []),
            }})

        # 示例模板：粘贴进控制台就能导入看效果
        if path == "/local/api/templates/sample" and self.command == "GET":
            return self.send_json({"code": 0, "msg": "ok", "data": tplimport.sample()})

        # 删除单个配方（分类空了一起删）
        if path == "/local/api/templates/delete" and self.command == "POST":
            key = str((self._json_body() or {}).get("recipeKey") or "").strip()
            if not key:
                return self.send_json({"code": 1, "msg": "缺少 recipeKey", "data": None})
            ok = mediagen.delete_recipe(key)
            self._note = f"templates-delete:{key}"
            return self.send_json({"code": 0 if ok else 1, "data": {"recipeKey": key},
                                   "msg": "已删除" if ok else "没有这个模板"})

        # 清空 / 恢复内置模板（恢复 = 重新扫 json/ 目录里的内置模板文件）
        if path == "/local/api/templates/clear" and self.command == "POST":
            db.set_config("aiMedia/creator/catalog", {"categories": []})
            self._note = "templates-cleared"
            return self.send_json({"code": 0, "msg": "模板已清空", "data": None})

        if path == "/local/api/templates/reset" and self.command == "POST":
            out = mediagen.reset_builtin()
            categories = out["catalog"]["categories"]
            files = out["files"]
            self._note = "templates-reset"
            if not categories:
                return self.send_json({
                    "code": 1,
                    "msg": f"{mediagen.TEMPLATE_DIR.name}/ 目录里没有可用的内置模板文件",
                    "data": out,
                })
            where = "、".join(f["file"] for f in files if not f["error"]) or "—"
            return self.send_json({
                "code": 0,
                "msg": f"已恢复内置模板：{len(categories)} 个分类，共 "
                       f"{sum(len(c['recipes']) for c in categories)} 个模板"
                       f"（读自 {mediagen.TEMPLATE_DIR.name}/ 下 {len(files)} 个文件：{where}）",
                "data": out,
            })

        if path == "/local/api/schema":
            return self.send_json({
                "db_path": str(db.DB_PATH),
                "tables": db.schema(),
            })

        self.send_json({"code": 1, "msg": "unknown local api"}, status=404)

    # ================================================== AI 绘图（公开页面 + 登录后生成）
    def handle_gallery(self, path: str, query: str) -> None:
        """绘图广场：作品列表公开；生成 / 点赞 / 删除按本地会话判定"""
        params = {k: v[0] for k, v in parse_qs(query).items()}
        sess = self.current_session() or {}
        me = {
            "loggedIn": bool(sess),
            "id": sess.get("id"),
            "name": sess.get("nick_name") or sess.get("user_name") or "",
            "isAdmin": str(sess.get("role") or "") == "admin",
        }

        if path == "/api/local/gallery/works" and self.command in ("GET", "HEAD"):
            sort = params.get("sort") or "new"
            limit = max(1, min(200, int(params.get("limit") or 60)))
            offset = max(0, int(params.get("offset") or 0))
            mine = str(params.get("mine") or "") in ("1", "true", "yes") or sort == "mine"
            uid = (sess.get("id") if sess else None) if mine else None
            if mine:
                works = db.list_image_works("new", limit, offset, uid) if uid else []
                total = db.count_image_works(uid) if uid else 0
            else:
                works = db.list_image_works(sort, limit, offset)
                total = db.count_image_works()
            base_charge = db.image_charge(1)
            return self.send_json({
                "code": 0,
                "data": {
                    "works": works,
                    "total": total,
                    "models": db.image_rate_models(),
                    "me": me,
                    "providers": len(db.list_image_providers()),
                    "points": db.points_balance(sess["id"]) if sess else None,
                    "mine": mine,
                    **db.image_size_table(),
                    "charge": {"pointsPerImage": base_charge["perImage"],
                               "freeLocal": base_charge["freeLocal"]},
                },
            })

        if path == "/api/local/gallery/models" and self.command in ("GET", "HEAD"):
            base_charge = db.image_charge(1)
            return self.send_json({"code": 0, "data": {
                "models": db.image_rate_models(),
                "presets": [{"key": k, "label": v.get("label"),
                             "sizes": v.get("sizes")} for k, v in db.IMAGE_PRESETS.items()],
                "me": me,
                **db.image_size_table(),
                "charge": {"pointsPerImage": base_charge["perImage"],
                           "freeLocal": base_charge["freeLocal"]},
            }})

        body = self._json_body()

        # 视频模型：控制台「视频接入」里已检测到的模型，供绘图页展示
        if path == "/api/local/gallery/video-models" and self.command == "GET":
            models = []
            for m in db.video_rate_models():
                models.append({
                    "id": m.get("id"), "modelID": m.get("modelID"),
                    "modelName": m.get("modelName"), "providerName": m.get("providerName"),
                    "providerId": m.get("providerId"),
                    "providerKind": m.get("providerKind"), "description": m.get("description"),
                    "specs": m.get("specs") or [], "isDefault": bool(m.get("isDefault")),
                })
            return self.send_json({"code": 0, "data": {"models": models}})

        # 视频生成：创建上游任务（Agnes Video 2.5 / Flash，OpenAI Videos 兼容）
        if path == "/api/local/gallery/generate-video" and self.command == "POST":
            if not sess:
                return self.send_json({"code": 401, "msg": "请先登录后再生成",
                                       "data": None}, status=401)
            prompt = str(body.get("prompt") or "").strip()
            if not prompt:
                return self.send_json({"code": 1, "msg": "请先写提示词", "data": None})
            model_id = str(body.get("modelID") or "").strip()
            want_pid = self._int(body.get("providerId") or 0, 0)
            items = db.video_rate_models()
            target = next((x for x in items
                           if str(x.get("modelID")) == model_id
                           and (not want_pid or int(x.get("providerId") or 0) == want_pid)), None)
            if target is None and not model_id:
                target = (next((x for x in items if x.get("isDefault")), None)
                          or (items[0] if items else None))
            if target is None:
                return self.send_json({"code": 1, "msg": "还没有可用的视频模型，请先在控制台「视频接入」检测",
                                       "data": None})
            provider_id = int(target.get("providerId") or 0)
            model = str(target.get("modelID") or "")
            cred = db.video_credentials(provider_id) or ("", "")
            base, key = cred
            if not base or not key:
                return self.send_json({"code": 1, "msg": "视频接入缺少 API 地址或密钥", "data": None})

            try:
                seed = max(0, min(2147483647, int(body.get("seed") or 0)))
            except (TypeError, ValueError):
                seed = 0
            seconds = str(body.get("seconds") or "5")
            aspect = str(body.get("aspectRatio") or "16:9")

            bill = db.image_charge(1, local=False)
            balance_before = db.points_balance(sess["id"])
            if bill["total"] > 0 and balance_before < bill["total"]:
                return self.send_json({
                    "code": 1, "msg": f"积分不足：生成视频需要 {bill['total']} 积分，"
                                      f"当前余额 {balance_before}。",
                    "data": {"need": bill["total"], "balance": balance_before},
                })
            task = None
            try:
                for attempt in range(2):
                    try:
                        task = imagegen.create_video(base, key, model, prompt, seconds=seconds,
                                                     aspect_ratio=aspect, seed=seed, timeout=240.0)
                        break
                    except imagegen.ImageGenError as exc:
                        # 上游队列满时按文档建议稍后重试一次
                        if attempt == 0 and "video_queue_full" in str(exc):
                            time.sleep(8)
                            continue
                        raise
            except imagegen.ImageGenError as exc:
                text = str(exc)
                if "video_queue_full" in text:
                    text = "上游视频队列已满，请稍后重试（免费视频模型排队较多）"
                elif "insufficient_user_quota" in text:
                    text = "上游账户额度不足，无法使用该视频模型，请更换模型或联系管理员"
                db.db_log("VIDEO_GENERATE_FAIL", "image_works",
                          json.dumps({"model": model, "error": str(exc)[:300]},
                                     ensure_ascii=False), sess.get("id"))
                return self.send_json({"code": 1, "msg": text, "data": None})

            video_id = str(task.get("video_id") or task.get("id")
                           or task.get("task_id") or "")
            if not video_id:
                return self.send_json({"code": 1, "msg": "上游没有返回视频任务 ID",
                                       "data": None})
            db.set_config(f"video_task:{sess['id']}:{video_id}", {
                "videoId": video_id, "modelID": model, "providerId": provider_id,
                "providerName": str(target.get("providerName") or ""),
                "prompt": prompt, "seconds": seconds, "aspectRatio": aspect,
                "seed": seed, "size": "720P", "createdAt": int(time.time()),
            })
            points_deducted = min(bill["total"], balance_before)
            if points_deducted > 0:
                db.add_points(sess["id"], -points_deducted)
            db.db_log("VIDEO_GENERATE", "image_works",
                      json.dumps({"model": model, "videoId": video_id,
                                  "points": points_deducted}, ensure_ascii=False),
                      sess.get("id"))
            return self.send_json({
                "code": 0, "msg": "视频任务已提交",
                "data": {"videoId": video_id,
                         "status": str(task.get("status") or "queued"),
                         "progress": task.get("progress") or 0,
                         "modelID": model, "providerId": provider_id,
                         "providerName": str(target.get("providerName") or ""),
                         "pointsDeducted": points_deducted,
                         "balance": db.points_balance(sess["id"])},
            })

        # 视频任务轮询：完成时下载落盘、登记作品并缓存结果（重复查询不重复落库）
        if path == "/api/local/gallery/video-status" and self.command in ("GET", "HEAD"):
            if not sess:
                return self.send_json({"code": 401, "msg": "请先登录", "data": None},
                                      status=401)
            video_id = str(params.get("videoId") or params.get("taskId") or "").strip()
            meta = db.get_config(f"video_task:{sess['id']}:{video_id}") or {}
            model = str(meta.get("modelID") or params.get("modelID") or "")
            provider_id = int(meta.get("providerId") or self._int(params.get("providerId") or 0, 0))
            cred = db.video_credentials(provider_id) or ("", "")
            base, key = cred
            if not video_id or not base or not key:
                return self.send_json({"code": 1, "msg": "视频任务不存在或接入配置缺失",
                                       "data": None})
            try:
                obj = imagegen.fetch_video(base, key, video_id, model, timeout=60.0)
            except imagegen.ImageGenError as exc:
                return self.send_json({"code": 1, "msg": str(exc), "data": None})

            status = str(obj.get("status") or obj.get("state") or "processing")
            progress = obj.get("progress") or 0
            if status != "completed":
                return self.send_json({"code": 0, "msg": status, "data": {
                    "videoId": video_id, "status": status,
                    "progress": progress,
                    "failedReason": obj.get("failed_reason") or obj.get("error"),
                }})

            done = db.get_config(f"video_done:{sess['id']}:{video_id}") or {}
            if done.get("urlKey"):
                return self.send_json({"code": 0, "msg": "completed", "data": {
                    "videoId": video_id, "status": "completed", "progress": 100,
                    "url": done.get("urlKey"), "work": done.get("work"),
                }})
            url = str(obj.get("url") or obj.get("video_url") or "")
            if not url:
                return self.send_json({"code": 1, "msg": "任务已完成但没有视频地址",
                                       "data": {"status": status}})
            try:
                data = imagegen.download_video(url)
            except imagegen.ImageGenError as exc:
                return self.send_json({"code": 1, "msg": str(exc),
                                       "data": {"status": "completed"}})
            sha = hashlib.sha256(data).hexdigest()[:8]
            name = f"ai_video_{int(time.time() * 1000)}_{sha}.mp4"
            IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            target_file = IMAGE_DIR / name
            try:
                target_file.write_bytes(data)
            except OSError as exc:
                return self.send_json({"code": 1, "msg": f"视频落盘失败：{exc}",
                                       "data": {"status": "completed"}})
            url_key = f"/image/{name}"
            row = imagedb.register(url_key, target_file, "ai")
            work = db.add_image_work(
                sess.get("id"), (me or {}).get("name") or "本地用户",
                str(meta.get("prompt") or ""), "",
                provider_id, str(meta.get("providerName") or ""), model,
                str(meta.get("aspectRatio") or "16:9"), url_key,
                (row or {}).get("id"), kind="video",
                seed=int(meta.get("seed") or 0))
            out = {"videoId": video_id, "status": "completed", "progress": 100,
                   "url": url_key, "work": work}
            db.set_config(f"video_done:{sess['id']}:{video_id}",
                          {"urlKey": url_key, "work": work})
            db.db_log("VIDEO_DONE", "image_works",
                      json.dumps({"videoId": video_id, "url": url_key},
                                 ensure_ascii=False), sess.get("id"))
            return self.send_json({"code": 0, "msg": "completed", "data": out})


        # 模板表单 → 预览最终提示词（按模板字段拼前缀/选项映射/后缀）
        if path == "/api/local/gallery/template-prompt" and self.command == "POST":
            key = str(body.get("recipeKey") or "").strip()
            recipe = mediagen.find_recipe(key) if key else None
            if not recipe:
                return self.send_json({"code": 1, "msg": "模板不存在，请刷新后重试",
                                       "data": None})
            fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
            err = mediagen.validate_fields(recipe, fields)
            if err:
                return self.send_json({"code": 1, "msg": err, "data": None})
            conf = recipe.get("local") if isinstance(recipe.get("local"), dict) else {}
            return self.send_json({"code": 0, "data": {
                "prompt": mediagen.build_prompt(recipe, fields),
                "negative": str(conf.get("negative") or ""),
                "size": mediagen.pick_size(recipe, fields),
            }})

        if path == "/api/local/gallery/generate" and self.command == "POST":
            if not sess:
                return self.send_json({"code": 401, "msg": "请先登录后再生成",
                                       "data": None}, status=401)
            prompt = str(body.get("prompt") or "").strip()
            negative = str(body.get("negative") or "").strip()
            # 官方语义：size 收档位（1K/2K/3K/4K），ratio 收比例（1:1、16:9…）；
            # 旧客户端传像素时由 db.image_size_lookup 反查表内组合。
            size = str(body.get("size") or "").strip()
            ratio = str(body.get("ratio") or "").strip()
            tier = str(body.get("tier") or "").strip()
            # 自定义宽高：前端传 ratio="custom" + width/height；也兼容不传 ratio 只传宽高
            try:
                custom_w = int(body.get("width") or 0)
                custom_h = int(body.get("height") or 0)
            except (TypeError, ValueError):
                return self.send_json({"code": 1, "msg": "自定义宽高必须是整数",
                                       "data": {"error": "size_invalid"}})
            custom_size = (ratio.lower() == "custom"
                           or (bool(custom_w or custom_h) and not ratio and not size))

            # 选了模板：按模板 schema 校验字段并生成最终提示词（忽略前端传来的 prompt）
            recipe_key = str(body.get("recipeKey") or "").strip()
            recipe = mediagen.find_recipe(recipe_key) if recipe_key else None
            if recipe:
                fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                err = mediagen.validate_fields(recipe, fields)
                if err:
                    return self.send_json({"code": 1, "msg": err, "data": None})
                prompt = mediagen.build_prompt(recipe, fields)
                conf = recipe.get("local") if isinstance(recipe.get("local"), dict) else {}
                if not negative:
                    negative = str(conf.get("negative") or "")
                # 模板自带 aspectRatio → 用旧像素值反查数据库尺寸表，不另设比例下拉
                size = mediagen.pick_size(recipe, fields)
                ratio = ""
                tier = ""
                custom_size = False           # 模板尺寸由模板决定，忽略自定义宽高
                if not prompt:
                    return self.send_json({"code": 1, "msg": "请先填写模板要求的内容",
                                           "data": None})

            if not prompt:
                return self.send_json({"code": 1, "msg": "请先写提示词", "data": None})
            try:
                n = max(1, min(4, int(body.get("n") or 1)))
            except (TypeError, ValueError):
                n = 1
            try:
                seed = max(0, min(2147483647, int(body.get("seed") or 0)))
            except (TypeError, ValueError):
                seed = 0
            dry_run = bool(body.get("dryRun"))

            if custom_size:
                size_row, size_err = db.image_size_custom(custom_w, custom_h)
            else:
                if not size and not ratio:
                    size, ratio = "1K", "1:1"     # 旧客户端未传尺寸时的显式默认
                size_row, size_err = db.image_size_lookup(size, ratio, tier)
            if not size_row:
                return self.send_json({"code": 1, "msg": size_err, "data": {
                    "error": "size_invalid", "size": size, "ratio": ratio,
                    "tier": tier, "width": custom_w, "height": custom_h,
                }})

            # 1) 显式选了本地占位 → 走本地；2) 选了具体模型 → 用它；
            # 3) 否则用默认接入；4) 都没有 → 本地占位图
            try:
                want_rid = int(body.get("rateId") or 0)
            except (TypeError, ValueError):
                want_rid = 0
            base = key = ""
            provider_id = None
            provider_name = "本地占位绘图"
            model = "local-placeholder"
            local = True
            if want_rid == db.LOCAL_IMAGE_RATE_ID:
                provider_id = 0
            else:
                target = db.resolve_image_rate_id(want_rid)
                if target:
                    row, model = target
                    if str(row.get("kind") or "") == "local" or str(row.get("base_url") or "").startswith("local://"):
                        local = True
                        provider_id = int(row["id"])
                        provider_name = str(row["name"] or "")
                    else:
                        cred = db.image_credentials(row["id"]) or ("", "")
                        base, key = cred
                        provider_id = int(row["id"])
                        provider_name = str(row["name"] or "")
                        local = False
                else:
                    endpoint = db.active_image_endpoint()
                    if endpoint:
                        base, key, model, _sizes = endpoint
                        prov = db.one("SELECT id, name, kind, base_url FROM image_providers"
                                      " WHERE enabled=1 ORDER BY is_default DESC, id ASC")
                        provider_id = int(prov["id"]) if prov else None
                        provider_name = str((prov or {}).get("name") or "")
                        local = (str((prov or {}).get("kind") or "") == "local"
                                 or str(base or "").startswith("local://"))

            # 上游没有 negative 字段：把排除语义并入 prompt，保证反向提示词真的生效
            final_prompt = prompt
            if negative and not local:
                final_prompt = prompt + "\n避免出现：" + negative

            bill = db.image_charge(n, local=local, points=size_row.get("points"))
            balance_before = db.points_balance(sess["id"])
            size_echo = {
                "size": size_row["pixels"], "pixels": size_row["pixels"],
                "width": int(size_row["width"]), "height": int(size_row["height"]),
                "ratio": str(size_row["ratio"]), "tier": str(size_row["tier"]),
                "sizeId": int(size_row["id"]),
                # 自定义宽高：请求值 / 实际值 / 是否归一化，全部显式回传
                "requestedPixels": str(size_row.get("requestedPixels") or size_row["pixels"]),
                "actualPixels": str(size_row.get("actualPixels") or size_row["pixels"]),
                "normalized": bool(size_row.get("normalized")),
                "custom": bool(custom_size),
            }
            if custom_size:
                size_echo["aspectDeviation"] = float(size_row.get("aspectDeviation") or 0)
                size_echo["aspectWarning"] = bool(size_row.get("aspectWarning"))
            if dry_run:
                # 只回预览（最终提示词 + 费用/余额），不生成、不落库、不扣分。
                # 绘图页不传 dryRun，行为不变；创作页 AI CG 用它做点击前校验。
                return self.send_json({"code": 0, "msg": "预览", "data": {
                    "prompt": final_prompt, "negative": negative, "n": n,
                    "cost": bill["total"], "pointsPerImage": bill["perImage"],
                    "balance": balance_before,
                    "canAfford": bill["total"] <= 0 or balance_before >= bill["total"],
                    "local": local, "model": model, "providerName": provider_name,
                    **size_echo,
                }})
            if bill["total"] > 0 and balance_before < bill["total"]:
                return self.send_json({
                    "code": 1,
                    "msg": (f"积分不足：生成 {n} 张需要 {bill['total']} 积分，"
                            f"当前余额 {balance_before}。可在账号中心签到或联系管理员充值。"),
                    "data": {"need": bill["total"], "balance": balance_before,
                             "pointsPerImage": bill["perImage"], **size_echo},
                })

            try:
                if local:
                    images = [imagegen.placeholder_png(
                        final_prompt, size_row["tier"], seed,
                        width=int(size_row["width"]),
                        height=int(size_row["height"])) for _ in range(n)]
                else:
                    images = imagegen.generate(
                        base, key, model, final_prompt, negative, n=n, seed=seed,
                        size=size_row["tier"], ratio=size_row["ratio"],
                        width=int(size_row["width"]),
                        height=int(size_row["height"]),
                        tier=size_row["tier"])
            except imagegen.ImageGenError as exc:
                db.db_log("IMAGE_GENERATE_FAIL", "image_works",
                          json.dumps({"model": model, "size": size_row["pixels"],
                                      "ratio": size_row["ratio"],
                                      "tier": size_row["tier"],
                                      "error": str(exc)[:300]},
                                     ensure_ascii=False), sess.get("id"))
                return self.send_json({"code": 1, "msg": str(exc), "data": None})

            author = me["name"] or "本地用户"
            works = []
            for idx, data in enumerate(images):
                sha = hashlib.sha256(data).hexdigest()[:8]
                ext = imagegen.detect_ext(data)
                name = f"ai_{int(time.time() * 1000)}_{idx}_{sha}{ext}"
                IMAGE_DIR.mkdir(parents=True, exist_ok=True)
                target_file = IMAGE_DIR / name
                try:
                    target_file.write_bytes(data)
                except OSError as exc:
                    return self.send_json({"code": 1, "msg": f"图片落盘失败：{exc}",
                                           "data": None})
                url_key = f"/image/{name}"
                row = imagedb.register(url_key, target_file, "ai")
                work = db.add_image_work(
                    sess.get("id"), author, final_prompt, negative,
                    provider_id, provider_name, model, size_row["pixels"], url_key,
                    (row or {}).get("id"), seed=seed)
                if work:
                    works.append(work)
                    if bill["perImage"] > 0:
                        db.log_consumption(sess.get("id"), "image", str(work.get("id")),
                                           final_prompt[:60], "image", 0, bill["perImage"])
            points_deducted = min(bill["total"], balance_before)
            if points_deducted > 0:
                db.add_points(sess["id"], -points_deducted)
            db.db_log("IMAGE_GENERATE", "image_works",
                      json.dumps({"model": model, "size": size_row["pixels"],
                                  "ratio": size_row["ratio"],
                                  "tier": size_row["tier"], "n": len(works),
                                  "points": points_deducted}, ensure_ascii=False),
                      sess.get("id"))
            self._note = "ai-generate"
            return self.send_json({
                "code": 0,
                "msg": f"已生成 {len(works)} 张",
                "data": {"works": works, "local": local, "model": model,
                         "pointsDeducted": points_deducted,
                         "pointsPerImage": bill["perImage"],
                         "balance": db.points_balance(sess["id"]),
                         "prompt": final_prompt,
                         **size_echo},
            })

        if path == "/api/local/gallery/like" and self.command == "POST":
            work = db.like_image_work(body.get("id"))
            if not work:
                return self.send_json({"code": 1, "msg": "作品不存在", "data": None})
            return self.send_json({"code": 0, "msg": "已点赞",
                                   "data": {"id": work["id"], "likes": work["likes"]}})

        if path == "/api/local/gallery/delete" and self.command == "POST":
            if not sess:
                return self.send_json({"code": 401, "msg": "请先登录", "data": None},
                                      status=401)
            ok = db.delete_image_work(body.get("id"), sess.get("id"),
                                      me["isAdmin"])
            return self.send_json({"code": 0 if ok else 1,
                                   "msg": "已删除" if ok else "只能删除自己的作品",
                                   "data": {"deleted": ok}})

        self.send_json({"code": 1, "msg": "unknown gallery api"}, status=404)

    # ================================================== 创作数据（控制台）
    def handle_creation(self, path: str, params: dict) -> None:
        """控制台「创作数据」：作品 + 世界书的列表 / 详情 / 导入 / 导出 / 删除"""
        sess = self.current_session() or {}
        uid = sess.get("id")
        try:
            if path == "/local/api/creation/overview" and self.command in ("GET", "HEAD"):
                out = db.creation_overview()
                out["users"] = db.creation_users()
                out["schema"] = db.CREATION_SCHEMA
                return self.send_json(out)

            if path == "/local/api/creation/works" and self.command in ("GET", "HEAD"):
                return self.send_json(db.creation_works(
                    self._int(params.get("userId") or 0, 0),
                    params.get("q") or "",
                    params.get("status") or "",
                    self._int(params.get("page") or 1, 1),
                    self._int(params.get("pageSize") or 20, 20)))

            if path == "/local/api/creation/work" and self.command in ("GET", "HEAD"):
                cid = params.get("characterId") or params.get("characterID") or params.get("id") or ""
                data = db.creation_work_detail(
                    self._int(params.get("userId") or 0, 0), cid)
                if not data:
                    return self.send_json({"code": 1, "msg": "作品不存在",
                                           "data": None}, status=404)
                return self.send_json({"code": 0, "msg": "ok", "data": data})

            if path == "/local/api/creation/export" and self.command in ("GET", "HEAD"):
                scope = "worldbook" if params.get("scope") == "worldbook" else "full"
                ids = [x for x in str(params.get("characterId") or params.get("characterID")
                                      or "").split(",")
                       if x.strip()]
                data = db.creation_export(
                    self._int(params.get("userId") or 0, 0), ids, scope)
                blob = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                self._bytes = len(blob)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(blob)))
                self.send_header(
                    "Content-Disposition",
                    'attachment; filename="creation-worldbook.json"' if scope == "worldbook"
                    else 'attachment; filename="creation-works.json"')
                self.end_headers()
                self.wfile.write(blob)
                self._note = f"creation-export:{data['counts']['works']}"
                db.db_log("EXPORT", "my_works", json.dumps(
                    {"scope": scope, **data["counts"]}, ensure_ascii=False), uid)
                return

            body = self._json_body()
            raw = body.get("json")
            if raw is None:
                raw = body.get("data")
            if raw is None and (body.get("works") is not None or body.get("schema")):
                raw = body                     # 直接把整包贴进来
            if raw is None:
                return self.send_json({"code": 1, "data": None,
                                       "msg": "缺少要导入的 JSON（json / data 字段）"})
            if isinstance(raw, (dict, list)):
                payload = raw
            else:
                try:
                    payload = json.loads(str(raw))
                except ValueError:
                    return self.send_json({"code": 1, "data": None,
                                           "msg": "JSON 解析失败：检查引号 / 逗号"})

            if path == "/local/api/creation/preview" and self.command == "POST":
                return self.send_json({"code": 0, "msg": "预览完成",
                                       "data": db.creation_preview(payload)})

            if path == "/local/api/creation/import" and self.command == "POST":
                out = db.creation_import(payload, str(body.get("mode") or "merge"),
                                         self._int(body.get("targetUserId") or 0, 0), uid)
                db.db_log("IMPORT", "my_works",
                          json.dumps(out, ensure_ascii=False)[:4000], uid)
                self._note = f"creation-import:{out['works']}"
                detail = (f"导入完成：新增 {out['created']} 个作品、覆盖 {out['replaced']} 个，"
                          f"世界书 {out['entries']} 条")
                if out["skipped"]:
                    detail += f"；跳过 {len(out['skipped'])} 项"
                return self.send_json({"code": 0, "msg": detail, "data": out})

            if path == "/local/api/creation/delete" and self.command == "POST":
                if str(body.get("confirm") or "") != "yes":
                    return self.send_json({"code": 1, "data": None,
                                           "msg": "危险操作需要确认"})
                data = db.creation_delete_work(
                    self._int(body.get("userId") or 0, 0),
                    str(body.get("characterId") or body.get("characterID") or ""))
                self._note = "creation-delete"
                return self.send_json({"code": 0, "msg": "已删除作品与世界书",
                                       "data": data})

            return self.send_json({"code": 1, "msg": "unknown creation api",
                                   "data": None}, status=404)
        except Exception as exc:  # noqa: BLE001
            self._note = "creation-error"
            return self.send_json({"code": 1, "msg": str(exc) or exc.__class__.__name__,
                                   "data": None})

    # ================================================== 数据库控制台 API
    def handle_db_admin(self, path: str, params: dict) -> None:
        """概览 / 表数据 / 行增删改 / 建表删表 / 维护 / 切换数据库 / 扫描 json/"""
        sess = self.current_session() or {}
        uid = sess.get("id")
        try:
            if path == "/local/api/db/overview" and self.command in ("GET", "HEAD"):
                db.db_log("READ_STATE", "",
                          json.dumps({"view": "overview"}, ensure_ascii=False), uid)
                return self.send_json(db.db_overview())

            if path == "/local/api/db/tables" and self.command in ("GET", "HEAD"):
                return self.send_json(db.db_tables())

            if path == "/local/api/db/table" and self.command in ("GET", "HEAD"):
                name = params.get("name") or ""
                limit = self._int(params.get("limit") or 50, 50)
                offset = self._int(params.get("offset") or 0, 0)
                data = db.db_table_data(name, limit, offset)
                db.db_log("QUERY", name, json.dumps(
                    {"limit": limit, "offset": offset, "rows": len(data["rows"])},
                    ensure_ascii=False), uid)
                return self.send_json(data)

            if path == "/local/api/db/logs" and self.command in ("GET", "HEAD"):
                limit = self._int(params.get("limit") or 60, 60)
                offset = self._int(params.get("offset") or 0, 0)
                return self.send_json(db.db_logs(limit, offset))

            body = self._json_body()

            if path == "/local/api/db/row/save" and self.command == "POST":
                data = db.db_save_row(body.get("table"), body.get("values") or {},
                                      body.get("pk") or None, uid)
                return self.send_json({"code": 0, "msg": "记录已保存", "data": data})

            if path == "/local/api/db/row/delete" and self.command == "POST":
                data = db.db_delete_row(body.get("table"), body.get("pk") or None, uid)
                return self.send_json({"code": 0, "msg": "记录已删除", "data": data})

            if path == "/local/api/db/table/create" and self.command == "POST":
                data = db.db_create_table(body.get("name"),
                                          body.get("columns") or [], uid)
                return self.send_json({"code": 0, "msg": "数据表已创建", "data": data})

            if path == "/local/api/db/table/drop" and self.command == "POST":
                if str(body.get("confirm") or "") != "yes":
                    return self.send_json({"code": 1, "msg": "危险操作需要确认",
                                           "data": None})
                data = db.db_drop_table(body.get("name"), uid)
                return self.send_json({"code": 0, "msg": "数据表已删除", "data": data})

            if path == "/local/api/db/table/clear" and self.command == "POST":
                if str(body.get("confirm") or "") != "yes":
                    return self.send_json({"code": 1, "msg": "危险操作需要确认",
                                           "data": None})
                data = db.db_clear_table(body.get("name"), uid)
                return self.send_json({"code": 0, "msg": "数据表数据已清空", "data": data})

            if path == "/local/api/db/maintenance" and self.command == "POST":
                if str(body.get("confirm") or "") != "yes":
                    return self.send_json({"code": 1, "msg": "危险操作需要确认",
                                           "data": None})
                action = str(body.get("action") or "")
                if action == "clear":
                    data = db.db_clear_all(uid)
                    data["image_cleared"] = imagedb.clear_all()
                    msg = "已清空业务数据（含图片库）"
                elif action == "recreate":
                    data, msg = db.db_recreate(uid), "数据表已重新创建"
                elif action == "drop_all":
                    data, msg = db.db_drop_all(uid), "已删除所有业务数据表"
                else:
                    return self.send_json({"code": 1, "msg": "未知的维护操作",
                                           "data": None})
                return self.send_json({"code": 0, "msg": msg, "data": data})

            if path == "/local/api/db/scan-json" and self.command == "POST":
                data = db.db_scan_json(uid)
                return self.send_json({"code": 0, "msg": "扫描完成", "data": data})

            if path == "/local/api/db/connect" and self.command == "POST":
                db_type = str(body.get("type") or "sqlite").strip().lower()
                if db_type != "sqlite":
                    return self.send_json({
                        "code": 1,
                        "msg": "本地后端只内置 SQLite 驱动；PostgreSQL / MySQL "
                               "需要额外安装驱动后再接入。",
                        "data": None,
                    })
                target = (body.get("database") or body.get("path")
                          or body.get("name") or "")
                db.switch_db(target, uid)
                admin = db.one("SELECT * FROM users WHERE role='admin'"
                               " ORDER BY id LIMIT 1")
                headers = {}
                if admin:
                    sess2 = db.create_session(
                        admin["id"], self.headers.get("User-Agent", ""),
                        self.client_address[0])
                    self._user_id = admin["id"]
                    headers["Set-Cookie"] = (
                        f"{SESSION_COOKIE}={sess2['token']}; Path=/; HttpOnly; "
                        f"SameSite=Lax; Max-Age={30 * 86400}")
                return self.send_json({"code": 0, "msg": "已切换数据库",
                                       "data": db.db_overview()}, headers=headers)

            return self.send_json({"code": 1, "msg": "unknown db api", "data": None},
                                  status=404)
        except Exception as exc:  # noqa: BLE001
            self._note = "db-admin-error"
            return self.send_json({"code": 1, "msg": str(exc) or exc.__class__.__name__,
                                   "data": None})

    # ================================================== 收藏嗅探
    def sniff_favorite(self, path: str, body: bytes) -> None:
        low = path.lower()
        if not any(k in low for k in ("favorite", "favourite", "like", "follow", "collect", "bookmark")):
            return
        if self.command == "GET":
            return
        sess = self.current_session()
        uid = sess["id"] if sess else None
        payload = {}
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            pass
        target = str(payload.get("characterID") or payload.get("characterId")
                     or payload.get("workID") or payload.get("id") or path.rstrip("/").split("/")[-1])
        kind = "like" if "like" in low else ("follow" if "follow" in low else "favorite")
        if self.command == "DELETE":
            db.remove_favorite(uid, kind, target)
            self._note = "favorite-del"
        else:
            created = db.add_favorite(uid, kind, target, "",
                                      json.dumps(payload, ensure_ascii=False)[:4000])
            if created and uid:
                db.award_exp_once(uid, "like" if kind == "like" else "favorite", target)
            self._note = "favorite-add"

    # ================================================== 对话记录
    def sniff_chat_request(self, path: str, body: bytes) -> None:
        """代理出去的聊天请求：把用户这一条按 sessionId 归档到本地库"""
        if "chat" not in path.lower() or not body:
            return
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return
        if not isinstance(data, dict):
            return
        text = ""
        for k in ("message", "content", "prompt", "input", "text", "userMessage"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                text = v
                break
        if not text:
            return
        sess = self.current_session()
        uid = sess["id"] if sess else None

        sid = str(data.get("sessionId") or "").strip()
        cid = str(data.get("characterId") or data.get("characterID") or "")
        if sid:
            db.ensure_chat_session(uid, cid, session_id=sid,
                                   initial_stats=Handler._initial_stats(cid))
            db.record_chat_message(uid, sid, "user", text)
            self._chat_sid = sid
            self._note = "chat-user"
            return

        char_id = str(data.get("characterID") or data.get("characterId") or "unknown")
        thread = db.upsert_thread(uid, char_id, str(data.get("title") or ""))
        db.add_message(thread, "user", text)
        self._chat_thread = thread
        self._note = "chat-user"

    def store_chat_reply(self, text: str) -> None:
        if not text.strip():
            return
        sid = getattr(self, "_chat_sid", None)
        if sid:
            sess = self.current_session()
            uid = sess["id"] if sess else None
            db.record_chat_message(uid, sid, "assistant", text.strip(),
                                   clean_for_storage(text))
            return
        thread = getattr(self, "_chat_thread", None)
        if thread:
            db.add_message(thread, "assistant", text.strip(),
                           clean_for_storage(text))

    @staticmethod
    def extract_sse_text(raw: str) -> str:
        """从 SSE / 流式 JSON 里尽量拼出 AI 回复"""
        out = ""
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                line = line[5:].strip()
            else:
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            pieces = obj if isinstance(obj, list) else [obj]
            for item in pieces:
                if not isinstance(item, dict):
                    continue
                for key in ("content", "text", "delta", "message", "answer", "reply"):
                    v = item.get(key)
                    if isinstance(v, str) and v:
                        if out and v.startswith(out):
                            out = v                      # 累积式
                        elif v not in out:
                            out += v                     # 增量式
                        break
        return out

    # ================================================== 反向代理 + 缓存
    def _send_stub(self, path: str, user_id: int | None) -> None:
        payload = stub_payload(path, user_id)
        self._bytes = len(payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Local", "stub")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self._note = "local-stub"

    def proxy(self, target: str) -> None:
        parsed = urlparse(target)
        # 这些读接口交给浏览器缓存：服务端不落库，只下发 Cache-Control
        # 本地读写接口除外 —— 它们的回包必须每次都新鲜（否则旧的线上回包盖住本地数据）
        cacheable = (self.command == "GET"
                     and target.startswith(ORIGIN)
                     and any(parsed.path.startswith(p) for p in BROWSER_CACHE_PREFIXES)
                     and not local_data_rel(parsed.path)
                     and "stream" not in parsed.path
                     and "notification" not in parsed.path)

        # 内容接口（卡片文字 + 标签）：本地优先，只从本地内容库读
        content = self.command == "GET" and is_content_path(parsed.path)
        url_key = parsed.path + ("?" + parsed.query if parsed.query else "")
        if content and (OFFLINE or LOCAL_ONLY):
            return self._content_fallback(url_key, parsed,
                                          "本地化（LOCAL_ONLY）" if LOCAL_ONLY else "离线模式（MISSAI_OFFLINE=1）")

        # 聊天开始的能力检查：登录态接口，本地存 config 秒回，避免代理挂 90s 拖垮对话
        if parsed.path == "/api/gva/narratium/interaction/v2/capabilities":
            cap = db.get_config("interaction/v2/capabilities")
            if cap:
                return self._local_ok(cap.get("data") or cap, "capabilities")

        # 已登录时，账户类接口直接本地回桩（线上不认识本地会话）
        sess = self.current_session()
        if sess and any(parsed.path.startswith(p) for p in AUTH_STUB_PREFIXES):
            return self._send_stub(parsed.path, sess["id"])

        headers = {"User-Agent": UA, "Accept-Encoding": "identity", "Connection": "close"}
        for name in FORWARD_HEADERS:
            value = self.headers.get(name)
            if value:
                headers[name] = value
        if "origin" in headers:
            headers["origin"] = ORIGIN
        if "referer" in headers:
            headers["referer"] = ORIGIN + "/"

        body = self.read_body() if self.command not in ("GET", "HEAD") else None

        # 本地嗅探：收藏、对话
        if body:
            self.sniff_favorite(parsed.path, body)
            if "chat" in parsed.path.lower():
                self.sniff_chat_request(parsed.path, body)

        conn = http.client.HTTPSConnection(parsed.hostname, 443, timeout=90)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:  # noqa: BLE001
            conn.close()
            if content:
                return self._content_fallback(url_key, parsed, f"上游连接失败: {exc}")
            return self._upstream_error(f"上游连接失败: {exc}")

        # 30x 改写成相对跳转，避免跳回线上域名
        if 300 <= resp.status < 400:
            location = resp.getheader("location") or "/"
            if location.startswith("http"):
                location = location.replace(ORIGIN, "") or "/"
            resp.read()
            conn.close()
            self.send_response(resp.status)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        ctype = resp.getheader("Content-Type") or ""

        # 线上不认识本地会话会回 401 → 已登录时降级成本地桩，避免前端连锁登出
        if resp.status in (401, 403) and parsed.path.startswith("/api/"):
            sess = self.current_session()
            if sess:
                resp.read()
                conn.close()
                # 个性化发现流必须给出 items，回 code:0 空壳会让前端
                # 读 items.length 直接崩 → 按前端约定抛 reasonCode，
                # 让它走内置兜底（热门榜/安全列表）
                if "/discovery/" in parsed.path:
                    self._note = "stub-not-enrolled"
                    return self.send_json({
                        "code": 1,
                        "msg": "本地会话未参与线上个性化策略",
                        "reasonCode": "SURFACE_NOT_ENROLLED",
                        "data": None,
                    })
                return self._send_stub(parsed.path, sess["id"])
        out_headers = []
        local_api = bool(local_data_rel(parsed.path))   # 本地读写接口：不给浏览器留缓存
        for key, value in resp.getheaders():
            if key.lower() in ("content-length", "content-encoding", "transfer-encoding",
                               "connection", "date", "server", "set-cookie"):
                continue
            # 可缓存接口由我们统一发缓存头，避免和线上的冲突
            if key.lower() == "cache-control" and (cacheable or local_api):
                continue
            # 回包头必须是 latin-1（HTTP 头编码），线上偶发的非 ASCII 头
            # 会在 end_headers() 里抛 UnicodeEncodeError → 整条请求变 500
            try:
                key.encode("latin-1")
                value.encode("latin-1")
            except (UnicodeEncodeError, AttributeError):
                continue
            out_headers.append((key, value))
        if local_api:
            out_headers.append(("Cache-Control", "no-store"))

        set_cookies = []
        for key, value in resp.getheaders():
            if key.lower() == "set-cookie":
                value = re.sub(r";\s*Domain=[^;]*", "", value, flags=re.I)
                try:
                    value.encode("latin-1")
                except UnicodeEncodeError:
                    continue
                set_cookies.append(value)

        if resp.status in (204, 304) or self.command == "HEAD":
            resp.read()
            conn.close()
            self.send_response(resp.status)
            for k, v in out_headers:
                self.send_header(k, v)
            for c in set_cookies:
                self.send_header("Set-Cookie", c)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if REWRITABLE.search(ctype):
            data = resp.read()
            conn.close()
            if resp.status >= 500:
                if content:
                    return self._content_fallback(url_key, parsed, f"上游 {resp.status}")
                return self._upstream_error(f"上游 {resp.status}")
            text = rewrite(data.decode("utf-8", "replace"))
            if content and resp.status == 200 and "application/json" in ctype:
                # 文字与标签落库：works / work_tags / tag_catalog + 该接口的离线载荷
                try:
                    db.ingest_content(parsed.path, url_key, json.loads(text))
                    self._note = "content-ingest"
                except (ValueError, TypeError):  # noqa: BLE001
                    pass
            if ctype.startswith("text/html"):
                # 站点页面（原站回的 HTML）也插本地脚本：广告隐藏所有人、控制台入口仅管理员
                text = inject_local_scripts(text, bool(self.admin_session()))
            payload = text.encode("utf-8")
            self._bytes = len(payload)
            self.send_response(resp.status)
            for k, v in out_headers:
                self.send_header(k, v)
            for c in set_cookies:
                self.send_header("Set-Cookie", c)
            if cacheable and resp.status == 200:
                # 缓存放浏览器：5 分钟内直接用本地副本，过期后回源校验
                self.send_header(
                    "Cache-Control",
                    f"private, max-age={BROWSER_CACHE_MAX_AGE}, stale-while-revalidate=600")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return

        # SSE 流式（对话）→ 透传并记录 AI 回复
        tee = "chat" in parsed.path.lower() or "stream" in parsed.path.lower()
        chunks: list[bytes] = []
        length = resp.getheader("Content-Length")
        self.send_response(resp.status)
        for k, v in out_headers:
            self.send_header(k, v)
        for c in set_cookies:
            self.send_header("Set-Cookie", c)
        if length:
            self.send_header("Content-Length", length)
        else:
            self.close_connection = True
        self.end_headers()

        if self.command != "HEAD":
            remaining = int(length) if length else None
            while True:
                want = 65536 if remaining is None else min(65536, remaining)
                chunk = resp.read(want) if want else b""
                if not chunk:
                    break
                if remaining is not None:
                    remaining -= len(chunk)
                if tee:
                    chunks.append(chunk)
                self.wfile.write(chunk)
                if remaining is not None and remaining <= 0:
                    break
        conn.close()
        if tee and chunks:
            self.store_chat_reply(self.extract_sse_text(b"".join(chunks).decode("utf-8", "replace")))
            self._note = "chat-reply"

    def _upstream_error(self, reason: str) -> None:
        """线上不通 / 5xx：不再由服务端兜底（缓存在浏览器里），直接明确报错"""
        msg = json.dumps({"code": 502, "msg": f"上游不可用：{reason}",
                          "data": None}, ensure_ascii=False).encode("utf-8")
        self._bytes = len(msg)
        self.send_response(502)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(msg)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(msg)
        self._note = "upstream-failed"

    def _content_fallback(self, url_key: str, parsed, reason: str) -> None:
        """线上不通（或本地化模式）→ 用本地内容库还原文字与标签。
        本地库没有该接口数据时，返回 200 空成功（不再 502），让前端渲染干净空态、
        而不是无限重试加载。"""
        obj = db.content_fallback(url_key, parsed.path, parsed.query)
        if obj is None:
            return self._send_empty_content(parsed.path)
        # 本地化时把回包里的跨域图片地址改成本地 /cdn，否则浏览器跨域会
        # 用 ORB 拦掉（store.story6688.com 直接跨域引图被浏览器屏蔽）
        _rewrite_imgs(obj)
        self._note = "content-fallback"
        self.send_json(obj, 200, {"X-Local": "db-fallback"})

    def _send_empty_content(self, path: str) -> None:
        """本地内容库无该接口数据时的空成功响应（code 0，形状按接口类型）"""
        if re.search(r"findNarratiumCharacter|content-preferences|/preferences|/catalog", path):
            data: dict = {}
        else:
            data = {"list": [], "items": [], "records": [], "rows": [], "works": [],
                    "total": 0, "count": 0, "page": 1, "pageSize": 0, "hasMore": False}
        msg = json.dumps({"code": 0, "msg": "", "data": data},
                         ensure_ascii=False).encode("utf-8")
        self._bytes = len(msg)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(msg)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Local", "empty-db")
        self.end_headers()
        self.wfile.write(msg)
        self._note = "empty-db"

    # ================================================== 路由
    # ================================================== 创作页 / 聊天（本地库读写）
    def _local_ok(self, data, note: str) -> bool:
        self._note = note
        self.send_json({"code": 0, "msg": "ok", "data": data})
        return True

    @staticmethod
    def _int(v, default: int) -> int:
        try:
            return int(str(v).strip() or default)
        except (TypeError, ValueError):  # noqa: PERF203
            return default

    def _json_body(self) -> dict:
        body = self.read_body()
        if not body:
            return {}
        try:
            obj = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):  # noqa
            return {}
        return obj if isinstance(obj, dict) else {}

    def _json_any(self):
        """读 body 并解析成任意 JSON（对象 / 数组 / 字符串都认）"""
        body = self.read_body()
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8-sig") or "null")
        except (ValueError, UnicodeDecodeError):  # noqa
            return None

    @staticmethod
    def _local_reply(name: str, message: str) -> str:
        """本地生成的 AI 回复（没配模型 / 模型不可用时的兜底，保证对话能继续）"""
        msg = " ".join((message or "").split())
        if len(msg) > 160:
            msg = msg[:160] + "…"
        if not msg:
            return f"（本地模式）{name} 在等你开口。"
        return (f"（本地模式）{name}：「{msg}」——我收到了。\n"
                "当前跑的是本地镜像，AI 回复由服务端本地生成；"
                "这段对话已经写进本地数据库，刷新页面依然能看到。")

    @staticmethod
    def _chat_context(uid: int | None, sid: str, cid: str, name: str,
                      message: str, aicg: bool | None = None) -> ChatContext:
        """查库组装一次生成所需的强类型上下文（各层只认 ChatContext）。"""
        payload = db.find_work(cid) if cid else None
        prompt: dict = {}
        ch: dict = {}
        if payload:
            ch = db.work_as_character(payload)
            pd = ch.get("promptData")
            prompt = pd if isinstance(pd, dict) else {}

        rows = db.query(
            "SELECT m.id, m.role, m.content, m.content_llm FROM chat_messages m"
            " JOIN chat_threads t ON t.id=m.thread_id"
            " WHERE t.user_id IS ? AND t.session_id=? ORDER BY m.id ASC",
            (uid, sid))
        history = []
        current_status = {}
        # 历史不再硬截条数：按 contextWindow 预算从最旧消息开始裁剪
        for row in rows:
            # 优先用落库时已清洗好的纯文本；旧数据（空值）再按原文现算
            text = str(row["content_llm"] or "").strip()
            if not text:
                text = clean_history_for_llm(row["content"])
            if text:
                history.append(Message(
                    role="user" if row["role"] == "user" else "assistant",
                    content=text, id=row["id"]))
        # 记忆召回预取：入口处已提前启动；这里同 key 直接复用（兜底其它调用方）
        Handler._recall_prefetch(uid, sid, cid,
                                 Handler._recall_query_from(history, message),
                                 Handler._recall_segments(history, message))
        # 历史里的 content_llm 已去掉状态栏标签：从原始 content 里找最近一次的数值。
        schema = Handler._stats_schema(ch if payload else {})
        # 平台数值规则只从库里读一次：全局 + 当前角色覆盖合并
        platform = db.list_stat_rules(cid)
        meta = db.list_stat_rule_meta(cid)
        mapper = EmotionMapper(platform, meta)
        values = dict(db.thread_stats(uid, sid) if sid else {})
        if not values:
            values = {}
            for n, cfg in schema.items():
                dv = cfg.get("default")
                values[n] = int(dv) if isinstance(dv, (int, float)) else 0
        if not values:
            # 没声明 schema → 用平台规则里的初始值，最后兜底内建的好感度 / SAN 起步值
            values = mapper.get_initial_stats()
        if not values:
            values = {"好感度": 0, "SAN值": 70}
        for row in reversed(rows[-24:]):
            if row["role"] == "user":
                continue
            items = parse_status(row["content"] or "", schema)
            if items:
                parsed = status_dict(items)
                # 有 schema 时只接受声明过的属性，模型编造的新属性直接丢弃
                if schema:
                    parsed = {k: v for k, v in parsed.items() if k in schema}
                values.update(parsed)
                break
        values = mapper.clamp_stats(values)
        current_status = {"values": values}
        # 关键词绑定：平台档位只在本轮上下文提到相关词时才注入（省 token）
        context = " ".join([message] + [str(r["content"] or "")[-120:] for r in rows[-4:]])
        rule = Handler._status_rules(cid, list(values.keys()), context, platform, uid)
        if rule:
            current_status["rule"] = rule

        return ChatContext(
            session_id=sid,
            character_data={
                "id": cid,
                "character": ch if payload else {},
                "promptData": prompt,
                "cg": Handler._cg_payload(uid, cid, aicg),
                "name": name,
                "hasPayload": bool(payload),
            },
            history=history,
            user_input=message,
            enable_aicg=(aicg if isinstance(aicg, bool) else True),
            current_status=current_status,
            stat_rules=platform,
            stat_meta=meta,
            stats_schema=schema,
        )

    @staticmethod
    def _stats_schema(ch: dict) -> dict:
        """角色卡声明的属性字典：{属性名: {min, max, default}}。

        兼容 custom_stats / customStats 放在顶层、extensions 或 characterCard 里。
        """
        src = None
        for key in ("custom_stats", "customStats"):
            v = (ch or {}).get(key)
            if isinstance(v, dict):
                src = v
                break
        if src is None:
            for holder in ((ch or {}).get("extensions"),
                           (ch or {}).get("characterCard")):
                if isinstance(holder, dict):
                    v = holder.get("custom_stats") or holder.get("customStats")
                    if isinstance(v, dict):
                        src = v
                        break
        schema: dict = {}
        for name, cfg in (src or {}).items():
            if not isinstance(cfg, dict):
                continue
            entry = {}
            for k in ("min", "max", "default"):
                val = cfg.get(k)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    entry[k] = int(val)
            if entry:
                schema[str(name)] = entry
        return schema

    @staticmethod
    def _clamp_schema(ctx: ChatContext) -> dict:
        """后处理截断用的边界：角色卡 schema 优先，平台规则补 min/max。"""
        merged = {str(k): dict(v) for k, v in (ctx.stats_schema or {}).items()
                  if isinstance(v, dict)}
        for stat, meta in (ctx.stat_meta or {}).items():
            if not isinstance(meta, dict):
                continue
            cfg = merged.setdefault(str(stat), {})
            if isinstance(meta.get("min_value"), (int, float)):
                cfg.setdefault("min", int(meta["min_value"]))
            if isinstance(meta.get("max_value"), (int, float)):
                cfg.setdefault("max", int(meta["max_value"]))
        return merged

    @staticmethod
    def _initial_stats(cid: str) -> dict:
        """新建会话时写入数据库的初始状态：平台规则初始值 + 角色卡默认值覆盖。"""
        payload = db.find_work(cid) if cid else None
        ch = db.work_as_character(payload) if payload else {}
        schema = Handler._stats_schema(ch if payload else {})
        mapper = EmotionMapper(db.list_stat_rules(cid), db.list_stat_rule_meta(cid))
        if schema:
            # 角色卡声明了属性字典 → 只初始化它声明的属性，不再混入平台默认的好感度 / SAN
            values = {}
            for n, cfg in schema.items():
                dv = cfg.get("default")
                if isinstance(dv, (int, float)):
                    values[n] = int(dv)
                elif n in mapper.initial_stats:
                    values[n] = mapper.initial_stats[n]
                else:
                    values[n] = 0
            return values
        return mapper.get_initial_stats()

    @staticmethod
    def _status_rules(cid: str, stats: list[str], context: str = "",
                      platform: dict | None = None, uid: int | None = None) -> str:
        """状态变化规则：平台分档表 + 该角色世界书里与状态相关的条目。

        - 平台规则按属性名取，且必须命中该属性的触发关键词（存于规则 JSON）
        - 没配关键词的属性按常驻处理，避免作者忘配就直接失效
        - 世界书命中条件：出现「规则」，或同时提到当前属性名与数值变化词
        合并后最多 1800 字，避免 lore 条目把上下文撑爆。
        """
        names = [str(s) for s in (stats or []) if s]
        text = str(context or "").lower()
        markers = ("变化", "数值", "上下限", "范围", "加减", "±",
                   "增加", "减少", "提升", "降低")
        lines: list[str] = []

        platform = platform if platform is not None else db.list_stat_rules(cid)
        keywords = db.list_stat_rule_keywords(cid)
        for n in names:
            rows = platform.get(n)
            if not rows:
                continue
            kws = keywords.get(n) or []
            if kws and not any(str(k).lower() in text for k in kws):
                continue                      # 本轮没提到相关词 → 跳过该属性档位
            lines.append(f"【{n}】")
            for r in rows:
                lines.append(f"{r['range']}｜{r['emotion']}｜{r['behavior']}")

        hits: list[str] = []
        for p in Handler._chat_worldbook(cid, uid):
            if not p.get("enabled", True):
                continue
            title = str(p.get("comment") or p.get("title") or p.get("name") or "")
            content = str(p.get("content") or "").strip()
            blob = title + "\n" + content
            if not blob.strip():
                continue
            if "规则" in blob:
                hits.append(content or title)
            elif any(n in blob for n in names) and any(k in blob for k in markers):
                hits.append(content or title)
        if hits:
            lines.extend(h.strip() for h in hits[:3])
        return "\n".join(lines).strip()[:1800]

    @staticmethod
    def _chat_worldbook(cid: str, uid: int | None = None) -> list[dict]:
        """该角色的世界书条目：系统镜像（user_id=0）+ 当前用户自建。

        其他用户的条目永不可见——旧实现只按 character_id 查，会把别人写的
        设定注入到自己的对话里。
        """
        rows = db.query(
            "SELECT entry_id, payload FROM worldbook_entries"
            " WHERE character_id=? AND (user_id=0 OR user_id=?)"
            " ORDER BY position, entry_id", (cid, int(uid or 0)))
        out = []
        for r in rows:
            try:
                p = json.loads(r["payload"] or "{}")
            except Exception:  # noqa: BLE001
                p = {}
            if isinstance(p, dict):
                p.setdefault("ID", r["entry_id"])
                p.setdefault("worldbookID", r["entry_id"])
                out.append(p)
        return out

    @staticmethod
    def _match_cg(uid: int | None, cid: str, text: str, sid=None) -> list[dict]:
        """按 AI 回复文本关键词命中 → 返回要展示的 CG 列表。
        triggerSource 含 ai / 默认触发；命中后 triggerOnce 的图自动停用并落库。
        scanDepth：每张图各自设定回溯深度（=扫描最近 N 条对话，含当前回复），
        例 depth=1 只看当前回复，depth=3 看当前回复+前 2 条。"""
        # AICG 插图开关：用户关了就不返回 CG 图
        prefs = db.get_config(f"cg:preference:{uid or 0}") or {}
        if prefs.get("showCGInChat") is False:
            return []
        # 下拉选「关闭 CG」→ 不再自动配图
        rule = db.get_config(f"cg:hintRule:{uid or 0}:{cid}") or {}
        if str(rule.get("mode") or "") == "off":
            return []
        key = f"cg:images:{uid or 0}:{cid}"
        images = db.get_config_list(key)
        if not images:
            return []
        weights = _norm_cg_weights(db.get_config(f"cg:weights:{uid or 0}:{cid}"))
        cur_text = text or ""
        # 一次性取所有图里最大的扫描深度对应的历史消息，再按各图深度切片
        max_depth = 1
        for img in images:
            try:
                max_depth = max(max_depth, int(img.get("scanDepth") or 1))
            except (TypeError, ValueError):
                pass
        older = []                      # 时间正序，不含当前回复
        if sid and max_depth > 1:
            try:
                rows = db.query(
                    "SELECT content FROM chat_messages WHERE thread_id=? "
                    "ORDER BY id DESC LIMIT ?",
                    (int(sid), max_depth))
                older = [r["content"] for r in rows[1:]][::-1]  # 丢最新(=当前回复)
            except (TypeError, ValueError):
                older = []
        matched = []
        dirty = False
        for img in images:
            if not img.get("isActive"):
                continue
            try:
                depth = max(1, int(img.get("scanDepth") or 1))
            except (TypeError, ValueError):
                depth = 1
            if depth <= 1:
                ctx_text = cur_text
            else:
                ctx_text = "\n".join(older[:depth - 1] + [cur_text])
            # 严格子串匹配：备注整段 / 标签 / 关键词命中即按 weights 累加
            score = _cg_score(img, ctx_text, weights)
            if score > 0:
                matched.append({
                    "id": img.get("id") or img.get("imageId"),
                    "url": img.get("url") or img.get("imageURL"),
                    "imageName": img.get("imageName"),
                    "displayDuration": img.get("displayDuration", 0),
                    "position": img.get("position", "center"),
                    "_score": score,
                    "_priority": img.get("priority", 0),
                })
                if img.get("triggerOnce"):
                    img["isActive"] = False
                    dirty = True
        if dirty:
            db.set_config(key, images)
        # 得分降序，同分按 priority 降序，只返回最匹配的一张
        if matched:
            matched.sort(key=lambda x: (x.get("_score", 0), x.get("_priority", 0)),
                         reverse=True)
            top = matched[0]
            top.pop("_score", None)
            top.pop("_priority", None)
            return [top]
        return []

    @staticmethod
    def _cg_payload(uid: int | None, cid: str, aicg: bool | None = None) -> dict | None:
        """给流式收尾用的 CG 数据包（含权重）；本次关闭插图 / 关闭 CG 时返回 None。"""
        if not cid or aicg is False:
            return None
        prefs = db.get_config(f"cg:preference:{uid or 0}") or {}
        if not isinstance(aicg, bool) and prefs.get("showCGInChat") is False:
            return None
        rule = db.get_config(f"cg:hintRule:{uid or 0}:{cid}") or {}
        if str(rule.get("mode") or "") == "off":
            return None
        images = db.get_config_list(f"cg:images:{uid or 0}:{cid}")
        if not images:
            return None
        return {
            "images": images,
            "weights": _norm_cg_weights(db.get_config(f"cg:weights:{uid or 0}:{cid}")),
        }

    @staticmethod
    def _rate_id(payload: dict):
        """前端传的模型 id：流式叫 modelRateId，非流式（chatSync）叫 modelId"""
        for key in ("modelRateId", "modelRateID", "modelId", "modelID"):
            val = payload.get(key)
            if val is None:
                continue
            if isinstance(val, str):
                val = val.strip()
                if val in ("", "None", "0"):
                    continue
            elif val in (0, False):
                continue
            return val
        return None

    @staticmethod
    def _llm_endpoint(rate_id=None) -> tuple[str, str, str] | None:
        """前端选的模型（或默认接入）→ (base_url, api_key, model)；没配 → None"""
        if str(rate_id or "").strip() not in ("", "None"):
            picked = db.resolve_rate_id(rate_id)   # 本地兜底/查不到 → None
            if not picked:
                return None
            row, model = picked
            cred = db.llm_credentials(row["id"])
            return (cred[0], cred[1], model) if cred else None
        return db.active_llm_endpoint()            # 没指定 → 默认接入

    @staticmethod
    def _timeout_local_fallback() -> bool:
        """控制台「模型接入 → 超时回退」开关：模型回复超时/失败时是否自动切本地模式。
        本地模式只用于测试、不作保底 → 默认关闭（失败直接报错，不悄悄换本地回复）。"""
        saved = db.get_config("llm_timeout_local") or {}
        return bool(saved.get("enabled", False))

    # ---------------------------------------------------- token 估算校准
    @staticmethod
    def _token_model_key(base: str, model: str) -> str:
        """按 provider（base_url 主机名）+ model 分键，避免不同接入互相污染。"""
        try:
            host = urlparse(str(base or "")).netloc
        except Exception:                            # noqa: BLE001
            host = ""
        return f"{host or str(base or '')}::{model}"

    @staticmethod
    def _token_alpha(base: str, model: str) -> float:
        data = db.get_config("token_alpha") or {}
        val = (data.get(Handler._token_model_key(base, model))
               if isinstance(data, dict) else None)
        try:
            return min(1.6, max(0.75, float(val))) if val else 1.0
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _calibrate_tokens(base: str, model: str, est: int, actual: int) -> None:
        """α ← 0.7α + 0.3×实际/估算；只在 est≥200 且 ratio∈[0.6,1.8] 时更新。"""
        try:
            est = int(est or 0)
            actual = int(actual or 0)
            if est < 200 or actual <= 0:
                return
            ratio = float(actual) / float(est)
            if ratio < 0.6 or ratio > 1.8:
                return
            key = Handler._token_model_key(base, model)
            data = db.get_config("token_alpha") or {}
            if not isinstance(data, dict):
                data = {}
            cur = Handler._token_alpha(base, model)
            data[key] = round(min(1.6, max(0.75, 0.7 * cur + 0.3 * ratio)), 4)
            db.set_config("token_alpha", data)
        except Exception as exc:                     # noqa: BLE001
            print(f"[llm] token 校准失败：{exc}", file=sys.stderr)

    @staticmethod
    def _include_usage() -> bool:
        """流式 include_usage 开关（默认关，部分中转站不认 stream_options）。"""
        cfg = db.get_config("llm_include_usage") or {}
        return bool(cfg.get("value")) if isinstance(cfg, dict) else bool(cfg)

    # ------------------------------------------------------ 历史压缩
    @staticmethod
    def _compress_keep_rounds(uid: int | None, cid: str) -> int:
        """最近保留多少轮原文不压。

        优先级：角色级 promptEngine.memory.compressKeepRounds
                → db 配置 llm_compress_keep_rounds → 常量 6。
        范围统一钳在 1..COMPRESS_KEEP_MAX_ROUNDS。
        """
        saved = db.get_config(f"promptEngine:{int(uid or 0)}:{cid}") or {}
        mem = saved.get("memory") if isinstance(saved.get("memory"), dict) else {}
        try:
            rounds = int(mem.get("compressKeepRounds"))
        except (TypeError, ValueError):
            rounds = 0
        if rounds <= 0:
            cfg = db.get_config("llm_compress_keep_rounds") or {}
            val = cfg.get("value") if isinstance(cfg, dict) else cfg
            try:
                rounds = int(val)
            except (TypeError, ValueError):
                rounds = COMPRESS_KEEP_ROUNDS
        return max(1, min(COMPRESS_KEEP_MAX_ROUNDS, rounds))

    @staticmethod
    def _param_num(saved, key: str, default, lo, hi, as_int: bool = False):
        """读模型参数：非数字 / NaN / 无穷 → 默认值，越界 → 钳制。

        与保存校验同规（越界钳制、非法值不落地），保证库里脏值不会原样下发。
        """
        raw = (saved or {}).get(key) if isinstance(saved, dict) else None
        if isinstance(raw, bool) or raw is None:
            return default
        try:
            num = float(raw)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(num):
            return default
        num = max(float(lo), min(float(hi), num))
        return int(round(num)) if as_int else num

    @staticmethod
    def _model_config_clean(payload, strict: bool = False):
        """模型参数的保存校验（与前端 MODEL_RANGE 完全一致）。

        策略：数字越界 → 钳制到范围内；非数字 / NaN / 无穷 → 不落地。
        strict=True（保存）：遇到非法值整包拒绝，返回 ( {}, 错误文案 )；
        strict=False（读取）：逐项丢弃脏值，返回 ( 干净值, 首个错误文案 )。
        """
        out: dict = {}
        errors: list[str] = []
        src = payload if isinstance(payload, dict) else {}
        for key, (lo, hi, typ) in MODEL_PARAM_RANGES.items():
            if key not in src:
                continue
            raw = src[key]
            bad = ""
            if isinstance(raw, bool) or raw is None:
                bad = f"{key} 必须是数字"
            else:
                try:
                    num = float(raw)
                except (TypeError, ValueError):
                    bad = f"{key} 必须是数字"
                else:
                    if not math.isfinite(num):
                        bad = f"{key} 不能是 NaN 或无穷大"
            if bad:
                errors.append(bad)
                if strict:
                    return {}, bad
                continue
            num = max(float(lo), min(float(hi), num))
            out[key] = int(round(num)) if typ is int else float(num)
        return out, (errors[0] if errors else "")

    @staticmethod
    def _output_budget(uid: int | None, rate_id) -> int:
        """本轮输出 token 预算（发送前预裁剪用）。

        保存值先钳 256–8192，再保证输入至少留窗口的 1/4（不低于 512 token）：
        输出预算最多 window - 输入保底 - 64。这样「最大回复长度」调大也不会
        把输入空间挤成负数、更不会关闭预裁剪。
        """
        who = int(uid or 0)
        mid = str(rate_id or "0").strip() or "0"
        saved = db.get_config(f"modelConfig:{who}:{mid}") or {}
        want = Handler._param_num(saved, "maxTokens", MODEL_PARAM_DEFAULTS["maxTokens"],
                                  MODEL_OUTPUT_MIN, MODEL_OUTPUT_MAX, True)
        window = int(Handler._prompt_config(uid, "", rate_id)
                     .get("contextWindow") or 8192)
        reserve = max(MODEL_INPUT_RESERVE_MIN,
                      int(round(window * MODEL_INPUT_RESERVE_RATIO)))
        cap = max(MODEL_OUTPUT_MIN, window - reserve - 64)
        return max(MODEL_OUTPUT_MIN, min(MODEL_OUTPUT_MAX, want, cap))

    @staticmethod
    def _note_sent_params(uid: int | None, stream_key: str, rate_id,
                          params: dict, est: int) -> None:
        """记录本轮真实下发参数（面板只读展示），被压低时打一行日志。"""
        try:
            mid = str(rate_id or "0").strip() or "0"
            sid = str(stream_key or "").split(":", 1)[-1]
            sent = int(params.get("max_tokens") or 0)
            saved = Handler._param_num(
                db.get_config(f"modelConfig:{int(uid or 0)}:{mid}") or {},
                "maxTokens", MODEL_PARAM_DEFAULTS["maxTokens"],
                MODEL_OUTPUT_MIN, MODEL_OUTPUT_MAX, True)
            window = int(Handler._prompt_config(uid, "", rate_id)
                         .get("contextWindow") or 8192)
            rec = {
                "temperature": round(float(params.get("temperature") or 0.0), 3),
                "topP": round(float(params.get("top_p") or 0.0), 3),
                "presencePenalty": round(float(params.get("presence_penalty") or 0.0), 3),
                "frequencyPenalty": round(float(params.get("frequency_penalty") or 0.0), 3),
                "maxTokens": sent,
                "maxTokensSaved": saved,
                "inputTokens": int(est or 0),
                "contextWindow": window,
                "at": db.now_iso(),
            }
            with _LAST_MODEL_PARAMS_LOCK:
                _LAST_MODEL_PARAMS[(int(uid or 0), sid, mid)] = rec
            if sent < saved:
                print(f"[model] max_tokens 被上下文压低：设定 {saved} → 实际 {sent}"
                      f"（输入约 {est} token，窗口 {window}）",
                      file=sys.stderr, flush=True)
        except Exception:                            # noqa: BLE001  观测失败不影响请求
            pass

    @staticmethod
    def _log_compression(kind: str, uid, sid, cid, before: int, after: int,
                         interval: str, summary_id) -> None:
        """压缩可观测：before/after token、压缩区间、摘要 id 各一行。"""
        print(f"[compress] mode={kind} before={before} after={after} "
              f"range={interval or '-'} summary={summary_id or '-'} "
              f"uid={uid} sid={sid} cid={cid}",
              file=sys.stderr, flush=True)

    @staticmethod
    def _enqueue_compress(uid, sid, cid) -> bool:
        """把被淘汰的历史区间排进后台压缩（同一会话去重，幂等靠摘要游标）。"""
        if not (uid and sid and cid):
            return False
        key = (int(uid), str(sid), str(cid))
        with _COMPRESS_LOCK:
            if key in _COMPRESS_SEEN:
                return False
            _COMPRESS_SEEN.add(key)
        _start_compress_worker()
        _COMPRESS_Q.put(key)
        return True

    @staticmethod
    def _model_params(uid: int | None, rate_id,
                      messages: list[dict] | None = None) -> dict:
        """「对话设置」里保存的模型参数 → llm.chat* 的关键字参数。

        默认值必须真正下发（0.7 / 0.8 / 0 / 0）：面板显示多少，请求体就是多少；
        不再用「没存过就不传」把默认值交给 llm.py 的形参兜底（那里是 0.7，
        但 top_p / 惩罚会落到上游自己的默认值，与面板显示不一致）。
        """
        who = int(uid or 0)
        mid = str(rate_id or "0").strip() or "0"
        saved = db.get_config(f"modelConfig:{who}:{mid}") or {}
        out: dict = {
            "temperature": Handler._param_num(
                saved, "temperature", MODEL_PARAM_DEFAULTS["temperature"], 0, 1),
            "top_p": Handler._param_num(
                saved, "topP", MODEL_PARAM_DEFAULTS["topP"], 0, 1),
            "presence_penalty": Handler._param_num(
                saved, "presencePenalty", MODEL_PARAM_DEFAULTS["presencePenalty"], 0, 1),
            "frequency_penalty": Handler._param_num(
                saved, "frequencyPenalty", MODEL_PARAM_DEFAULTS["frequencyPenalty"], 0, 1),
            "max_tokens": Handler._param_num(
                saved, "maxTokens", MODEL_PARAM_DEFAULTS["maxTokens"],
                MODEL_OUTPUT_MIN, MODEL_OUTPUT_MAX, True),
        }
        if messages:
            # 实际下发值 = min(设定值, 窗口 - 输入估算 - 64)，下限 256。
            # 预裁剪已保证输入留出 _output_budget 的空间，正常情况下这里等于设定值。
            window = int(Handler._prompt_config(uid, "", rate_id)
                         .get("contextWindow") or 8192)
            est = estimate_messages(messages)
            budget = min(MODEL_OUTPUT_MAX, window - est - 64)
            out["max_tokens"] = max(MODEL_OUTPUT_MIN,
                                    min(int(out["max_tokens"]), budget))
        prof = db.llm_profile(rate_id)     # 接入级格式差异（字段名 / stop / 额外参数）
        if prof:
            out["profile"] = prof
        return out

    @staticmethod
    def _prompt_config(uid: int | None, cid: str, rate_id=None) -> dict:
        """提示词组装配置：contextWindow 默认 8192，可按模型覆盖（D1）。"""
        who = int(uid or 0)
        mid = str(rate_id or "0").strip() or "0"
        saved = db.get_config(f"modelConfig:{who}:{mid}") or {}
        window = saved.get("contextWindow")
        if window in (None, ""):
            window = (db.get_config("prompt_context_window") or {}).get("value")
        try:
            window = int(window or 0)
        except (TypeError, ValueError):
            window = 0
        if window <= 0:
            window = 8192
        return {"contextWindow": max(1024, min(window, 32768)),
                "modelRateId": mid, "characterId": cid}

    # 算法记忆默认配置；角色级 promptEngine 配置里的 memory 字段按需覆盖。
    MEMORY_DEFAULTS = {
        "enabled": True,
        "topK": 8, "minScore": 0.25,
        "halfLifeDays": 30.0,
        "weights": {"relevance": 0.6, "importance": 0.25, "recency": 0.15},
        "relevance": {"bm25": 0.6, "cosine": 0.4},
        "embedDim": memory_engine.DEFAULT_DIM,
        "injectDepth": 4, "maxTokens": 600,
        "extractEvery": 2, "llmExtract": True,
        "mmr": True, "mmrLambda": 0.7, "queryTimeout": 3.0,
        "capacity": 300, "clusterThreshold": 0.85, "dedupThreshold": 0.92,
        "archiveDays": 60, "archiveImportance": 0.35,
        # 新召回模块：请求入口后台预取，prompt 组装最多等 waitMs；
        # version=4：8 秒等待窗口 + 20 条精排（实测 6 秒会被降频后的精排顶穿），
        # 正常负载下完整链路 100% 进 prompt；waitMs 是上限，召回完成即返回。
        # 存量配置里被深合并写死的旧默认值由 _recall_cfg 按 version 迁移。
        "recall": {
            "version": 4,
            "enabled": True, "topK": 8, "waitMs": 8000, "rerankTop": 20,
            "mmrLambda": 0.7, "singleTimeoutMs": 1500,
            "totalTimeoutMs": 8000, "modelPath": "", "rerankPath": "",
        },
    }

    # OpenAI 兼容 embedding provider（与 llm_providers 的 profile 风格一致）；
    # provider=local 时改走本地 TF-IDF + LSA，不再需要 base_url/api_key。
    EMBEDDING_DEFAULTS = {
        "provider": "", "local_enabled": False,
        "local_model": "local-lsa", "local_dim": 128,
        "enabled": False, "base_url": "", "api_key": "", "model": "",
        "dimensions": 0, "batch_size": 16, "timeout": 30.0, "max_retries": 2,
    }

    @staticmethod
    def _emb_cfg(uid: int | None) -> dict:
        """embedding 配置：用户级优先，空则回落全局（embeddingConfig:0）。"""
        cfg = dict(Handler.EMBEDDING_DEFAULTS)
        saved = db.get_config(f"embeddingConfig:{int(uid or 0)}") or {}
        if not saved:
            saved = db.get_config("embeddingConfig:0") or {}
        for key in Handler.EMBEDDING_DEFAULTS:
            if key in saved:
                cfg[key] = saved[key]
        return cfg

    # 历史版本 recall 的速度默认值：迁移时据此判断哪些字段是用户真改过的
    RECALL_V2_DEFAULTS = {"waitMs": 5000, "totalTimeoutMs": 5500,
                          "rerankTop": 20, "singleTimeoutMs": 1500}
    RECALL_V3_DEFAULTS = {"waitMs": 6000, "totalTimeoutMs": 6000,
                          "rerankTop": 20, "singleTimeoutMs": 1500}

    @staticmethod
    def _recall_cfg(mem: dict) -> dict:
        """recall 配置：默认值 ← 角色级覆盖；version<4 的存量配置走迁移。

        旧版默认值曾被深合并写进存量配置（v1: 300/2500/8/800，v2: 5000/5500/20/1500，
        v3: 6000/6000/20/1500），直接合并会让老值锁死。迁移规则：
          · 无 version / v1：速度项全部回落到新默认；
          · v2 / v3：等于各自版本默认值的速度项回落到新默认，
            用户真改过的（与对应版本默认不同）保留；
          · 所有版本都保留用户显式设过的 enabled/topK/mmrLambda/模型路径。
        任何一次 promptConfig/saveByCharacter 都会写入当前版本号，之后按普通深合并。
        """
        base = dict(Handler.MEMORY_DEFAULTS["recall"])
        saved = mem.get("recall") if isinstance(mem.get("recall"), dict) else {}
        try:
            ver = int(saved.get("version") or 1)
        except (TypeError, ValueError):
            ver = 1
        if ver < int(base["version"]):
            for key in ("enabled", "topK", "mmrLambda", "modelPath", "rerankPath"):
                if saved.get(key) not in (None, ""):
                    base[key] = saved[key]
            speed_defaults = (Handler.RECALL_V3_DEFAULTS if ver >= 3
                              else Handler.RECALL_V2_DEFAULTS if ver >= 2 else {})
            for key, old in speed_defaults.items():
                if key in saved and saved[key] != old:
                    base[key] = saved[key]
        else:
            base.update(saved)
        base["version"] = int(Handler.MEMORY_DEFAULTS["recall"]["version"])
        return base

    @staticmethod
    def _memory_cfg(uid: int | None, cid: str) -> dict:
        """算法记忆配置：默认值 ← 角色级 promptEngine.memory 深合并。"""
        cfg = dict(Handler.MEMORY_DEFAULTS)
        cfg["weights"] = dict(Handler.MEMORY_DEFAULTS["weights"])
        cfg["relevance"] = dict(Handler.MEMORY_DEFAULTS["relevance"])
        saved = db.get_config(f"promptEngine:{int(uid or 0)}:{cid}") or {}
        mem = saved.get("memory") if isinstance(saved.get("memory"), dict) else {}
        for key, val in (mem or {}).items():
            if key in ("weights", "relevance") and isinstance(val, dict):
                cfg[key] = {**cfg[key], **val}
            elif key in Handler.MEMORY_DEFAULTS:
                cfg[key] = val
        cfg["recall"] = Handler._recall_cfg(mem)
        cfg["embedding"] = Handler._emb_cfg(uid)
        return cfg

    # 召回预取：请求入口起线程跑 recall，prompt 组装时最多等 waitMs；
    # 完成结果按 (用户, 会话, 归一化 query) 短期缓存，重复查询直接命中。
    _RECALL_CACHE: dict = {}
    _RECALL_CACHE_LOCK = threading.Lock()
    _RECALL_CACHE_TTL = 20.0
    _RECALL_CACHE_MAX = 64

    @staticmethod
    def _memory_query(ctx: ChatContext) -> str:
        """检索查询 = 最近 3 条历史 + 本轮输入（短文本，避免整段历史稀释词面命中）。"""
        return Handler._recall_query_from(ctx.history, ctx.user_input)

    @staticmethod
    def _memory_segments(ctx: ChatContext) -> list[str]:
        """检索查询的分段形式（供历史向量 hash 缓存跨轮复用）。"""
        return Handler._recall_segments(ctx.history, ctx.user_input)

    @staticmethod
    def _recall_segments(history, message: str) -> list[str]:
        """召回查询分段 = 最近 2 条历史 + 本轮输入（本轮输入只出现一次）。

        请求入口已把本轮用户消息落库，history 末尾会带上它；这里先摘掉，
        保证入口预取与 _chat_context 算出的查询串完全一致（同一缓存 key）。
        """
        cur = str(message or "").strip()
        recent = [str(getattr(m, "content", "") or "").strip()
                  for m in (history or [])]
        if cur and recent and recent[-1] == cur:
            recent = recent[:-1]
        recent = [x for x in recent if x][-2:]
        if cur:
            recent.append(cur)
        return recent

    @staticmethod
    def _recall_query_from(history, message: str) -> str:
        return " ".join(Handler._recall_segments(history, message))[:2000]

    @staticmethod
    def _recall_query_early(uid: int, sid: str,
                            message: str) -> tuple[str, list[str]]:
        """请求入口（历史尚未构建）时用的轻量查询：只取最近 3 条消息。

        产出与 _recall_query_from(history, message) 完全一致，保证预取缓存 key
        与 prompt 组装阶段一致，不会重复召回；同时让召回与历史加载/上下文准备并行。
        """
        try:
            rows = db.query(
                "SELECT m.content, m.content_llm FROM chat_messages m"
                " JOIN chat_threads t ON t.id=m.thread_id"
                " WHERE t.user_id IS ? AND t.session_id=?"
                " ORDER BY m.id DESC LIMIT 3", (uid, sid))
        except Exception:                            # noqa: BLE001
            rows = []
        cur = str(message or "").strip()
        segs: list[str] = []
        for row in reversed(rows or []):
            text = str(row["content_llm"] or "").strip()
            if not text:
                text = clean_history_for_llm(row["content"]).strip()
            if text:
                segs.append(text)
        # 当前这条用户消息刚落库，会在最近几条里重复出现：摘掉后再补一份
        if cur and segs and segs[-1] == cur:
            segs = segs[:-1]
        segs = [x for x in segs if x][-2:]
        if cur:
            segs.append(cur)
        return " ".join(segs)[:2000], segs

    @staticmethod
    def _recall_overrides(recall_cfg: dict, keyword_only: bool = False) -> dict:
        """记忆配置 → memory_recall 的参数；全部按毫秒换算成秒。"""
        def _sec(key: str, default: float) -> float:
            try:
                return max(1.0, float(recall_cfg.get(key) or default)) / 1000.0
            except (TypeError, ValueError):
                return default / 1000.0

        ov = {
            "budget": _sec("totalTimeoutMs", 6000),
            "single_timeout": _sec("singleTimeoutMs", 1500),
            "rerank_top": int(recall_cfg.get("rerankTop") or 20),
            "mmr_lambda": float(recall_cfg.get("mmrLambda") or 0.7),
            "load_wait": 0.0,          # 预热交给启动线程，请求链路不等模型
        }
        if recall_cfg.get("modelPath"):
            ov["embed_path"] = str(recall_cfg["modelPath"])
        if recall_cfg.get("rerankPath"):
            ov["rerank_path"] = str(recall_cfg["rerankPath"])
        if keyword_only:
            ov["keyword_only"] = True
        return ov

    @staticmethod
    def _recall_gc(now: float) -> None:
        cache = Handler._RECALL_CACHE
        for key in [k for k, v in cache.items()
                    if now - float(v.get("at") or 0.0) > Handler._RECALL_CACHE_TTL]:
            cache.pop(key, None)
        while len(cache) > Handler._RECALL_CACHE_MAX:
            oldest = min(cache.items(), key=lambda kv: kv[1].get("at", 0.0))[0]
            cache.pop(oldest, None)

    @staticmethod
    def _recall_start(uid: int, sid: str, cid: str, query: str,
                      recall_cfg: dict,
                      segments: list[str] | None = None) -> dict:
        """同 key 复用进行中/刚完成的召回，否则起后台线程。"""
        key = (int(uid or 0), str(sid or ""), str(query or ""))
        now = time.monotonic()
        with Handler._RECALL_CACHE_LOCK:
            Handler._recall_gc(now)
            ent = Handler._RECALL_CACHE.get(key)
            if ent is not None:
                return ent
            ent = {"event": threading.Event(), "result": None,
                   "error": "", "at": now, "thread": None}
            Handler._RECALL_CACHE[key] = ent
            print(f"[recall] 预取启动 key="
                  f"{hashlib.sha1(str(key[2]).encode('utf-8')).hexdigest()[:8]}"
                  f" 段数={len(segments or [])}", file=sys.stderr, flush=True)

        top_k = int(recall_cfg.get("topK") or 8)
        ov = Handler._recall_overrides(recall_cfg)
        if segments:
            # 最近几轮消息分段编码（内容 hash 缓存跨轮复用），查询向量在后台组装
            ov["query_segments"] = [str(s or "") for s in segments]
            ov["segment_weights"] = [1.0] * (len(segments) - 1) + [2.0]

        def run():
            try:
                ent["result"] = memory_recall.recall(
                    query, uid, cid, sid=sid, top_k=top_k, **ov)
            except Exception as exc:                 # noqa: BLE001
                ent["error"] = f"{type(exc).__name__}: {exc}"
                print(f"[recall] 后台召回失败：{ent['error']}", file=sys.stderr)
            finally:
                ent["event"].set()

        th = threading.Thread(target=run, name="memory-recall", daemon=True)
        ent["thread"] = th
        th.start()
        return ent

    @staticmethod
    def _recall_mode(rows: list[dict]) -> str:
        """按返回字段判断本轮结果来自完整链路还是降级/兜底。"""
        if not rows:
            return "空结果"
        if all(it.get("rerank") is not None for it in rows):
            return "完整链路"
        if any(it.get("rerank") is not None for it in rows):
            return "完整链路(部分精排)"
        if any(it.get("cosine") is not None for it in rows):
            return "降级(稠密,无精排)"
        return "关键词兜底"

    @staticmethod
    def _recall_tag(rows: list[dict], mode: str) -> list[dict]:
        """给本轮结果打来源标记（完整链路 / 关键词兜底），写进 prompt log。"""
        for it in rows:
            it["recall_mode"] = mode
        return rows

    @staticmethod
    def _recall_results(uid: int, sid: str, cid: str, query: str,
                        recall_cfg: dict,
                        segments: list[str] | None = None) -> list[dict]:
        """最多等 waitMs；只有超时/失败才改用关键词通道兜底。

        兜底结果不写回预取缓存：后台完整链路结果到达后仍以成功结果为准，
        同一 query 再次请求时若已就绪直接复用（_RECALL_CACHE 的 result）。
        """
        if not query.strip():
            return []
        t0 = time.monotonic()
        top_k = int(recall_cfg.get("topK") or 8)
        wait_ms = max(0.0, float(recall_cfg.get("waitMs") or 6000))
        ent = None
        try:
            ent = Handler._recall_start(uid, sid, cid, query, recall_cfg,
                                        segments=segments)
            finished = ent["event"].wait(wait_ms / 1000.0)
            if finished and ent.get("result") is not None:
                rows = list(ent.get("result") or [])
                mode = Handler._recall_mode(rows)
                rows = Handler._recall_tag(rows, mode)
                print(f"[recall] 注入={mode} 条数={len(rows)}"
                      f" 等待={int((time.monotonic() - t0) * 1000)}ms"
                      f" 后台耗时={int((time.monotonic() - float(ent.get('at') or t0)) * 1000)}ms",
                      file=sys.stderr, flush=True)
                return rows
            if finished:
                print(f"[recall] 后台召回失败（{ent.get('error') or 'empty'}），"
                      "改用关键词通道", file=sys.stderr, flush=True)
            else:
                print(f"[recall] 等待 {int(wait_ms)}ms 未完成，改用关键词通道",
                      file=sys.stderr, flush=True)
        except Exception as exc:                     # noqa: BLE001
            print(f"[recall] 预取等待失败：{exc}，改用关键词通道",
                  file=sys.stderr, flush=True)

        try:
            fast = memory_recall.recall(
                query, uid, cid, sid=sid, top_k=top_k,
                **Handler._recall_overrides(recall_cfg, keyword_only=True)) or []
            fast = Handler._recall_tag(fast, "关键词兜底")
            print(f"[recall] 注入=关键词兜底(bm25) 条数={len(fast)}"
                  f" 等待={int((time.monotonic() - t0) * 1000)}ms",
                  file=sys.stderr, flush=True)
            return fast
        except Exception as exc:                     # noqa: BLE001
            print(f"[recall] 关键词通道失败：{exc}", file=sys.stderr, flush=True)
            return []

    @staticmethod
    def _recall_prefetch(uid: int | None, sid: str, cid: str, query: str,
                         segments: list[str] | None = None) -> None:
        """请求入口调用：配置检查通过就在后台启动召回，不阻塞。"""
        if not uid or not sid or not str(query or "").strip():
            return
        try:
            if not Handler._memory_enabled(uid, cid):
                return
            mem_cfg = Handler._memory_cfg(uid, cid)
            recall_cfg = mem_cfg.get("recall") if isinstance(
                mem_cfg.get("recall"), dict) else {}
            if not mem_cfg.get("enabled", True) or not recall_cfg.get("enabled", True):
                return
            Handler._recall_start(uid, sid, cid, query, recall_cfg,
                                  segments=segments)
        except Exception as exc:                     # noqa: BLE001
            print(f"[recall] 预取启动失败：{exc}", file=sys.stderr)

    @staticmethod
    def _first_mes(cid: str) -> str:
        payload = db.find_work(cid) if cid else None
        if not payload:
            return ""
        ch = db.work_as_character(payload)
        return str(ch.get("firstMes") or "").strip()

    @staticmethod
    def _prompt_messages(uid: int | None, sid: str, cid: str, ctx: ChatContext,
                     rate_id=None) -> tuple[list[dict], dict]:
        """生成 messages（prompt.py 是唯一拼装引擎）。"""
        ep = Handler._llm_endpoint(rate_id)
        if ep:
            # 预算估算先用本接入已校准的 α，再按本次 usage 反向更新
            set_token_alpha(Handler._token_alpha(ep[0], ep[2]))
        cfg = Handler._prompt_config(uid, cid, rate_id)
        entries = Handler._chat_worldbook(cid, uid)
        mem_on = Handler._memory_enabled(uid, cid) if uid else False
        slots: dict = {}
        if uid and sid and mem_on:
            got = db.memory_slots(uid, sid)
            slots = {k: str(v.get("content") or "")
                     for k, v in (got or {}).items()
                     if str(v.get("content") or "").strip()}
            if not slots:
                # 旧表 chat_memories 只读兼容：还没有四槽时整段当 short_term
                keep = Handler._compress_keep_rounds(uid, cid)
                old = db.chat_memories(uid, cid, session_id=sid)[-max(1, keep):]
                text = "\n".join(str(m.get("content") or "") for m in old
                                 if m.get("content"))
                if text:
                    slots = {"short_term": text}
        pcfg = db.get_config(f"promptEngine:{int(uid or 0)}:{cid}") or {}
        anchor = pcfg.get("anchor") if isinstance(pcfg.get("anchor"), dict) else {}
        if anchor:
            cfg["anchor"] = anchor
        # 记忆召回：请求入口已在后台预取；这里最多等 waitMs，超时走关键词通道
        mem_cfg = Handler._memory_cfg(uid, cid)
        recall_cfg = mem_cfg.get("recall") if isinstance(
            mem_cfg.get("recall"), dict) else {}
        retrieved: list[dict] = []
        recall_mode = ""
        recall_wait = 0
        t_prompt = time.monotonic()
        if (uid and sid and mem_on and mem_cfg.get("enabled", True)
                and recall_cfg.get("enabled", True)):
            t_recall = time.monotonic()
            try:
                retrieved = Handler._recall_results(
                    uid, sid, cid, Handler._memory_query(ctx), recall_cfg,
                    segments=Handler._memory_segments(ctx))
            except Exception as exc:                 # noqa: BLE001
                print(f"[recall] 检索失败：{exc}", file=sys.stderr)
                retrieved = []
            recall_wait = int((time.monotonic() - t_recall) * 1000)
            recall_mode = (str(retrieved[0].get("recall_mode") or "")
                           if retrieved else "空结果")
            block = memory_recall.build_injection(
                retrieved, max_tokens=int(mem_cfg.get("maxTokens") or 600))
            if block:
                mem_cfg["injectedBlock"] = block
                depth = int(mem_cfg.get("injectDepth", 4) or 4)
                for it in retrieved:
                    it["injected_at"] = depth
        cfg["memory"] = mem_cfg
        if uid and sid and mem_on:
            _RECALL_STATS[(int(uid), str(sid), str(cid))] = {
                "count": len(retrieved), "waitMs": int(recall_wait),
                "at": db.now_iso()}
        messages = build_prompt(ctx, worldbook_entries=entries, slots=slots,
                                   retrieved=retrieved, config=cfg)
        if recall_mode:
            print(f"[recall] prompt 组装={int((time.monotonic() - t_prompt) * 1000)}ms"
                  f" 检索等待={recall_wait}ms 注入={recall_mode}"
                  f" 条数={len(retrieved)}", file=sys.stderr, flush=True)
        compact = [{k: v for k, v in it.items() if k != "vec"}
                   for it in retrieved]
        return messages, {"config": cfg, "worldbook": entries,
                          "memory": slots, "retrieved": compact, "anchor": anchor}

    @staticmethod
    def _maybe_first_mes(uid: int | None, sid: str, cid: str) -> int:
        """新会话时后端只写一次 firstMes。"""
        if not sid or not cid:
            return 0
        return db.record_first_message_once(uid, sid, Handler._first_mes(cid))

    @staticmethod
    def _prompt_diff(uid: int | None, sid: str, msgs: list[dict]) -> list[dict]:
        """与同会话上一条 prompt 快照按段对比，定位变化来源。"""
        try:
            row = db.one(
                "SELECT messages FROM chat_prompt_log WHERE user_id IS ?"
                " AND session_id=? ORDER BY id DESC LIMIT 1", (uid, sid))
            if not row:
                return []
            prev = json.loads(row["messages"] or "[]")
            return prompt_diff(prev if isinstance(prev, list) else [], msgs)
        except Exception:                            # noqa: BLE001
            return []

    @staticmethod
    def _memory_enabled(uid: int | None, cid: str) -> bool:
        """「对话设置 → 记忆设置」总闸：关闭后不检索、不注入、不生成摘要与记忆。"""
        if not uid or not cid:
            return False
        saved = db.get_config(f"summaryConfig:{int(uid)}:{cid}") or {}
        return bool(saved.get("summaryEnabled", True))

    @staticmethod
    def _prev_status(uid: int | None, sid: str) -> str:
        """上一条助手回复里的状态栏：补全时作为基准，避免数值凭空跳变。"""
        if not sid:
            return ""
        rows = db.query(
            "SELECT m.content FROM chat_messages m"
            " JOIN chat_threads t ON t.id=m.thread_id"
            " WHERE t.user_id IS ? AND t.session_id=? AND m.role='assistant'"
            " ORDER BY m.id DESC LIMIT 1", (uid, sid))
        if not rows:
            return ""
        m = BOOK_STATUS_RE.search(str(rows[0]["content"] or ""))
        return m.group(0) if m else ""

    def _ensure_format(self, base: str, key: str, model: str,
                       msgs: list[dict], text: str,
                       uid: int | None = None, sid: str = "",
                       on_send=None) -> str:
        """模型偶尔漏掉 <book_status> / <next_prompt>：用一次小请求补齐，
        保证前端始终能渲染状态栏与剧情选项。已有标签则原样返回。"""
        low = str(text or "").lower()
        need_status = "<book_status" not in low
        need_opt = "<next_prompt" not in low
        if not (need_status or need_opt) or not str(text).strip():
            return text
        spec = ""
        if msgs:
            spec = "\n".join(
                ln for ln in str(msgs[0].get("content") or "").splitlines()
                if "状态栏" in ln)[:500]
        prev = self._prev_status(uid, sid) if need_status else ""
        want = []
        if need_status:
            want.append("状态栏：<book_status><item>属性名：<b>数值</b></item></book_status>"
                        "（属性名与取值范围按上面的设定；数值以【上一轮状态栏】为基准"
                        "小幅变化，没有上一轮时按初始值）")
        if need_opt:
            want.append("<next_prompt>\nA、……\nB、……\nC、……\n</next_prompt>"
                        "（3 个差异化下一步选项，简洁，不替 User 做决定）")
        ask = ("下面是一段互动小说回复。只补全要求的标签块，不要复述正文、"
               "不要解释：\n" + "\n".join("- " + w for w in want))
        req = [
            {"role": "system", "content": "你是互动小说的格式整理器，只输出标签块。"},
            {"role": "user", "content":
                (f"【状态栏设定】\n{spec}\n\n" if spec else "")
                + (f"【上一轮状态栏】\n{prev}\n\n" if prev else "")
                + f"【本轮回复】\n{text}\n\n【补全要求】\n{ask}"},
        ]
        try:
            # 后台调用参数写死，不受「对话设置 → 模型参数」影响（面板只管正文一轮）
            got = llm.chat(base, key, model, req, timeout=45.0,
                           temperature=0.3, max_tokens=300, on_send=on_send)
        except llm.LLMError as exc:
            print(f"[format] 补全请求失败（{model}）：{exc}", file=sys.stderr)
            return text
        blocks = []
        if need_status:
            m = re.search(r"<book_status[^>]*>[\s\S]*?</book_status>", got, re.I)
            if m:
                blocks.append(m.group(0).strip())
        if need_opt:
            m = re.search(r"<next_prompt[^>]*>[\s\S]*?</next_prompt>", got, re.I)
            if m:
                opt = m.group(0).strip()
                # 至少 A、B 两个选项才算补全成功
                if len(re.findall(r"[A-Za-z]\s*[、.．]", opt)) >= 2:
                    blocks.append(opt)
        if not blocks:
            return text
        return str(text).rstrip() + "\n\n" + "\n".join(blocks)

    def _local_upload(self, body: bytes) -> bool:
        """创作页封面上传：FormData(multipart) → 落到 html/image/ → 回 data.file.url"""
        ctype = self.headers.get("Content-Type") or ""
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if not body or not m:
            self.send_json({"code": 1, "msg": "缺少文件", "data": None})
            self._note = "upload-bad"
            return True
        boundary = b"--" + m.group(1).encode("utf-8", "replace")
        blob = b""
        fname = ""
        for part in body.split(boundary):
            part = part.strip(b"\r\n")
            if not part or part.startswith(b"--"):
                continue
            if b"\r\n\r\n" not in part:
                continue
            head, data = part.split(b"\r\n\r\n", 1)
            head_txt = head.decode("utf-8", "replace")
            fm = re.search(r'filename="([^"]*)"', head_txt)
            if not fm:
                continue
            fname = fm.group(1) or ""
            blob = data.rstrip(b"\r\n")
            break
        if not blob:
            self.send_json({"code": 1, "msg": "空文件", "data": None})
            self._note = "upload-empty"
            return True

        ext = os.path.splitext(fname)[1].lower()
        if ext not in MIME:
            ext = ".png"
        name = f"img_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{ext}"
        target = IMAGE_DIR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        url = f"/api/uploads/file/{name}"
        imagedb.register(url, target, "upload")
        db.add_upload("image", str(target.relative_to(ROOT)).replace("\\", "/"),
                      remote_url=url, sha=hashlib.sha256(blob).hexdigest(), size=len(blob))
        return self._local_ok({
            "file": {"url": url, "name": name, "path": f"html/image/{name}",
                     "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()},
        }, "upload")

    # ---------------------------------------------- 创作页
    def _local_creation(self, rel: str, params: dict, payload: dict,
                        uid: int | None, body: bytes = b"") -> bool:
        if rel == "narratiumCharacter/findNarratiumCharacterByCharacterID":
            cid = params.get("characterId") or params.get("characterID") or ""
            # 镜像角色详情（原站整包，profile 页本地渲染任意角色的关键）
            mirrored = db.character_get(cid)
            if mirrored is not None:
                if uid and cid:
                    db.award_exp_once(uid, "view", cid)  # 浏览经验：每人每天每个角色一次
                return self._local_ok(mirrored.get("data") or mirrored, "character-mirrored")
            # 本地新建的作品不在内容库里 → profile 页直接读本地整包（游客也能看）
            local = db.find_work(cid)
            if local is None:
                return False
            if uid and cid:
                db.award_exp_once(uid, "view", cid)
            return self._local_ok({"character": db.work_as_character(local)},
                                  "character-local")

        # 平台数值规则：全局默认 + 角色专属覆盖（同名属性角色优先）
        if rel.startswith("narratiumStatRules/"):
            sub = rel.split("/", 1)[1]
            stat_cid = str(params.get("characterId") or params.get("characterID")
                           or params.get("character_id")
                           or payload.get("characterId")
                           or payload.get("characterID") or "").strip()

            def stat_rule_view(extra: dict | None = None) -> dict:
                data = {
                    "stats": db.list_stat_rules(stat_cid),
                    "keywords": db.list_stat_rule_keywords(stat_cid),
                    "meta": db.list_stat_rule_meta(stat_cid),
                    "scope": db.stat_rule_scope(stat_cid),
                    "overrides": db.list_stat_rule_overrides(stat_cid),
                    "characterId": stat_cid,
                }
                if extra:
                    data.update(extra)
                return data

            if sub == "setStatRuleScope":
                if uid is None:
                    self.send_json({"code": 401, "msg": "请先登录后再修改数值规则",
                                    "data": {"errorCode": "UNAUTHORIZED"}}, status=401)
                    return True
                if not stat_cid:
                    self.send_json({"code": 400, "msg": "缺少角色 ID", "data": None},
                                   status=400)
                    return True
                try:
                    scope = db.set_stat_rule_scope(stat_cid, payload.get("scope"))
                except ValueError as exc:
                    self.send_json({"code": 400, "msg": str(exc), "data": None},
                                   status=400)
                    return True
                return self._local_ok(stat_rule_view({"scope": scope}), "stat-scope")
            if sub == "saveStatMeta":
                if uid is None:
                    self.send_json({"code": 401, "msg": "请先登录后再编辑平台数值规则",
                                    "data": {"errorCode": "UNAUTHORIZED"}}, status=401)
                    return True
                stat = str(payload.get("stat") or "").strip()
                if not stat:
                    self.send_json({"code": 400, "msg": "属性名不能为空",
                                    "data": None}, status=400)
                    return True
                try:
                    meta = db.save_stat_meta(stat, payload.get("initial_value"),
                                             payload.get("min_value"),
                                             payload.get("max_value"),
                                             character_id=stat_cid)
                except ValueError as exc:
                    self.send_json({"code": 400, "msg": str(exc), "data": None},
                                   status=400)
                    return True
                return self._local_ok(stat_rule_view(
                    {"saved": {"stat": stat, **meta}}), "stat-meta-save")
            if sub in ("saveStatRule", "deleteStatRule"):
                if uid is None:
                    self.send_json({"code": 401, "msg": "请先登录后再编辑平台数值规则",
                                    "data": {"errorCode": "UNAUTHORIZED"}}, status=401)
                    return True
                stat = str(payload.get("stat") or "").strip()
                rng = re.sub(r"\s*[~～]\s*", " ~ ",
                             str(payload.get("range") or "")).strip()
                if not stat or not re.fullmatch(r"-?\d+ ~ -?\d+", rng):
                    self.send_json({"code": 400,
                                    "msg": "属性名不能为空，数值区间格式为「最小 ~ 最大」",
                                    "data": None}, status=400)
                    return True
                if sub == "saveStatRule":
                    raw = payload.get("keywords")
                    if isinstance(raw, str):
                        kws = [x.strip() for x in re.split(r"[,，、]", raw) if x.strip()]
                    elif isinstance(raw, list):
                        kws = [str(x).strip() for x in raw if str(x).strip()]
                    else:
                        kws = None
                    try:
                        saved = db.save_stat_rule(
                            stat, rng, payload.get("emotion") or "",
                            payload.get("behavior") or "", kws,
                            old_stat=str(payload.get("oldStat") or ""),
                            old_range=re.sub(
                                r"\s*[~～]\s*", " ~ ",
                                str(payload.get("oldRange") or "")).strip(),
                            character_id=stat_cid)
                    except ValueError as exc:
                        self.send_json({"code": 400, "msg": str(exc), "data": None},
                                       status=400)
                        return True
                else:
                    db.delete_stat_rule(stat, rng, character_id=stat_cid)
                    saved = None
                return self._local_ok(stat_rule_view({"saved": saved}),
                                      "stat-rule-save")
            return self._local_ok(stat_rule_view(), "stat-rules")

        # 世界书读取（不需要登录，支持离线镜像数据）
        if rel.startswith("narratiumWorldbookEntry/"):
            sub = rel.split("/", 1)[1]
            cid = (params.get("characterId") or params.get("characterID")
                   or payload.get("characterId") or payload.get("characterID") or "")
            if sub == "getEntriesByCharacterID":
                entries = db.list_worldbook(uid, cid, custom=False) if uid else []
                if not entries and cid:
                    entries = db.worldbook_mirror_get(cid, custom=False)
                return self._local_ok(entries, "wb-list")
            if sub == "getUserCustomEntries":
                entries = db.list_worldbook(uid, cid, custom=True) if uid else []
                if not entries and cid:
                    entries = db.worldbook_mirror_get(cid, custom=True)
                return self._local_ok(entries, "wb-custom-list")

        # 绘图模型目录（本地导入的 MediaGen 配方；空库用 json/ 下的内置模板文件播种）
        if rel == "aiMedia/creator/catalog":
            return self._local_ok(mediagen.get_catalog(seed=True), "catalog")

        # 配方生成：提交建任务（后台线程出图），前端 4s 一次轮询 /generations/<id>
        if rel.startswith("aiMedia/creator/recipes/") and rel.endswith("/generations"):
            if self.command not in ("POST", "PUT"):
                return False
            key = rel[len("aiMedia/creator/recipes/"):-len("/generations")]
            recipe = mediagen.find_recipe(unquote(key))
            if not recipe:
                self.send_json({"code": 404, "data": {"errorCode": "RECIPE_NOT_FOUND"},
                                "msg": "模板不存在，请刷新后重试。"}, status=404)
                return True
            if uid is None:
                self.send_json({"code": 401, "data": {"errorCode": "UNAUTHORIZED"},
                                "msg": "请先登录后再开始生成。"}, status=401)
                return True
            sess = self.current_session() or {}
            author = str(sess.get("nick_name") or sess.get("user_name") or "本地用户")
            row, code, msg = mediagen.create_generation(
                uid, author, recipe, payload,
                str(self.headers.get("Idempotency-Key") or ""))
            if not row:
                status = 500 if code == "MEDIA_INTERNAL_ERROR" else 400
                self.send_json({"code": status, "data": {"errorCode": code}, "msg": msg},
                               status=status)
                return True
            return self._local_ok(mediagen.payload_of(row), "media-generate")

        # 生成任务查询（轮询）：assetStatus=pending → available/missing
        if rel.startswith("aiMedia/generations/"):
            gid = unquote(rel[len("aiMedia/generations/"):]).strip("/")
            row = mediagen.get_generation(gid, uid)
            if not row:
                self.send_json({"code": 404, "data": {"errorCode": "MEDIA_GENERATION_NOT_FOUND"},
                                "msg": "生成任务不存在或已失效。"}, status=404)
                return True
            return self._local_ok(mediagen.payload_of(row), "media-generation")

        if uid is None:                      # 没登录 → 交给线上（保存本来就要求登录）
            return False

        if rel == "work/my":                 # 判作品是否已存在：没有必须回 data:null
            cid = params.get("characterID") or params.get("characterId") or ""
            return self._local_ok(db.get_work(uid, cid), "work-my")

        if rel == "works/my":
            return self._local_ok(db.list_my_works(
                uid, self._int(params.get("page"), 1), self._int(params.get("pageSize"), 10),
                params.get("status") or "", params.get("keyword") or ""), "works-my")

        if rel == "work":
            if self.command == "DELETE":
                cid = db.work_identity(payload) or params.get("characterId") \
                    or params.get("characterID") or ""
                cid = str(cid or "").strip()
                # 连带清理该作品的图片资源：封面 + 所有 CG 图（磁盘文件 + config_cache）
                if cid:
                    try:
                        # 1) 封面图：先从 my_works 拿 cover_url
                        cover_row = db.one(
                            "SELECT cover_url FROM my_works WHERE user_id=? AND character_id=?",
                            (uid, cid)) if uid else None
                        cover_url = str((cover_row or {}).get("cover_url") or "")
                        extra_files = []
                        for cu in (cover_url,):
                            if cu and isinstance(cu, str) and "/image/" in cu:
                                extra_files.append(os.path.basename(cu.split("/image/", 1)[-1]))
                        # 2) CG 图：扫所有 cg:images:*:<cid>
                        cg_rows = db.query(
                            "SELECT key, payload FROM config_cache WHERE key LIKE ?",
                            (f"cg:images:%:{cid}",))
                        for row in cg_rows:
                            try:
                                arr = json.loads(row["payload"] or "[]")
                            except Exception:  # noqa: BLE001
                                arr = []
                            if isinstance(arr, list):
                                for x in arr:
                                    if not isinstance(x, dict):
                                        continue
                                    for field in ("url", "imageURL", "imageUrl"):
                                        p = x.get(field)
                                        if p and isinstance(p, str) and "/image/" in p:
                                            extra_files.append(os.path.basename(
                                                p.split("/image/", 1)[-1]))
                            # 顺手把 config_cache 里这条 CG 列表也清掉
                            db.execute("DELETE FROM config_cache WHERE key=?", (row["key"],))
                        # 3) hintRule 也清
                        db.execute(
                            "DELETE FROM config_cache WHERE key LIKE ?",
                            (f"cg:hintRule:%:{cid}",))
                        # 4) 真删磁盘文件（去重、不存在跳过）
                        for fname in set(extra_files):
                            try:
                                f = IMAGE_DIR / fname
                                if f.exists():
                                    f.unlink()
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception:  # noqa: BLE001
                        pass
                return self._local_ok({"characterId": cid,
                                       "deleted": db.delete_work(uid, cid)}, "work-delete")
            if not db.work_identity(payload):
                payload = dict(payload)
                payload["characterId"] = f"char-{uuid.uuid4()}"
            saved = db.save_work(uid, payload)
            if saved is None:
                return False
            return self._local_ok(saved, "work-create" if self.command == "POST"
                                  else "work-update")

        if rel == "work/content":            # PUT：保存草稿整包
            saved = db.save_work(uid, payload)
            if saved is None:
                return False
            return self._local_ok(saved, "work-content")

        if rel == "work/content/confirm":
            return self._local_ok({"confirmed": True}, "work-confirm")

        if rel == "narratium/work/publish-policy":      # 发布资格：返回真实等级与门槛
            rules = db.get_rules()
            min_level = db.publish_min_level(rules)
            growth = db.user_level_info(uid) if uid else db.level_info(0)
            return self._local_ok({
                "enabled": True, "minimumLevel": min_level,
                "currentLevel": growth["level"], "currentLevelName": growth["levelName"],
                "currentExp": growth["experience"], "nextLevelExp": growth["nextLevelExp"],
                "levelProgress": growth["levelProgress"], "isMaxLevel": growth["isMaxLevel"],
                "rewardExperience": db.exp_reward("publish", rules),
            }, "publish-policy")

        if rel in ("work/publish", "work/unpublish",
                   "work/schedule", "work/schedule/cancel"):
            cid = db.work_identity(payload) or params.get("characterId") \
                or params.get("characterID") or ""
            status = {"work/publish": "published", "work/unpublish": "draft",
                      "work/schedule": "scheduled",
                      "work/schedule/cancel": "draft"}[rel]
            if rel in ("work/publish", "work/schedule"):
                rules = db.get_rules()
                min_level = db.publish_min_level(rules)
                growth = db.user_level_info(uid)
                if growth["level"] < min_level:
                    return self._creative_fail(
                        f"发布作品需要 Lv.{min_level}，当前为 Lv.{growth['level']}"
                        f"（经验 {growth['experience']}/{growth['nextLevelExp']}）。"
                        "可在账号中心查看成长进度。", code=2)
            was_published = str((db.get_work(uid, cid) or {}).get("publishStatus")
                                or "") == "published"
            when = payload.get("scheduledTime") if rel == "work/schedule" else ""
            saved = db.set_work_status(uid, cid, status, when)
            if saved is None:
                saved = {"characterId": cid, "publishStatus": status,
                         "updatedAt": db.now_iso()}
            if rel == "work/publish" and status == "published":
                db.publish_to_public(uid, cid)      # 发布 → 同步进公开库（探索/首页可见）
                if not was_published:               # 首次发布才给经验，重复发布不刷
                    exp_gain = db.exp_reward("publish")
                    growth = db.add_experience(uid, exp_gain)
                    saved = dict(saved or {})
                    saved.update({
                        "experienceGained": exp_gain,
                        "experience": growth["experience"], "level": growth["level"],
                        "levelName": growth["levelName"],
                        "nextLevelExp": growth["nextLevelExp"],
                        "levelProgress": growth["levelProgress"],
                        "isMaxLevel": growth["isMaxLevel"],
                    })
            elif rel == "work/unpublish" and status == "draft":
                db.unpublish_from_public(cid)       # 下架 → 从公开库移除
            return self._local_ok(saved, "work-status")

        if rel == "work/creativeRefs":
            if self.command == "PUT":
                return self._local_ok(db.put_creative_refs(uid, payload), "refs-put")
            cid = params.get("characterID") or params.get("characterId") or ""
            return self._local_ok(db.get_creative_refs(uid, cid), "refs-get")

        if rel == "narratium/work/public":
            cid = params.get("characterId") or params.get("characterID") or ""
            local = db.get_work(uid, cid)
            if local is None:
                return False                # 本地没有 → 反代去线上读作品页
            return self._local_ok(local, "work-public")

        if rel == "workFilter/config":
            return self._local_ok(db.get_config("workFilter/config") or {}, "config")

        if rel == "llmModelRate/getModelRateList":
            # 模型列表 = 中转站检测到的模型（连上就有几条）；没接模型才回本地兜底那条
            items = db.llm_rate_models()
            return self._local_ok({"list": items, "total": len(items)}, "model-rate")

        if rel == "llmModelRate/getModelStatusList":
            items = db.llm_rate_models()
            return self._local_ok({"list": items, "total": len(items)}, "model-status")

        if rel == "fileUploadAndDownload/upload":
            return self._local_upload(body)

        # ---- 世界书写入（需要登录，已在上面处理读取）----
        if rel.startswith("narratiumWorldbookEntry/"):
            sub = rel.split("/", 1)[1]
            if sub in ("createNarratiumWorldbookEntry", "updateNarratiumWorldbookEntry"):
                eid = str(payload.get("worldbookID") or "")
                return self._local_ok(
                    db.save_worldbook_entry(uid, payload, custom=False, entry_id=eid),
                    "wb-save")
            if sub in ("createUserCustomEntry", "updateUserCustomEntry"):
                eid = str(payload.get("id") or payload.get("worldbookID") or "")
                return self._local_ok(
                    db.save_worldbook_entry(uid, payload, custom=True, entry_id=eid),
                    "wb-custom-save")
            if sub == "deleteNarratiumWorldbookEntry":
                eid = str(payload.get("worldbookID") or payload.get("ID") or "")
                return self._local_ok({"deleted": db.delete_worldbook_entry(
                    uid, payload, entry_id=eid)}, "wb-del")
            if sub == "deleteUserCustomEntry":
                eid = str(payload.get("id") or payload.get("ID") or "")
                return self._local_ok({"deleted": db.delete_worldbook_entry(
                    uid, payload, custom=True, entry_id=eid)}, "wb-custom-del")
            # 其他世界书子端点：返回空成功，不回源
            return self._local_ok([], "wb-unknown")

        # ---- AI 媒体/CG 管理 ----
        if rel.startswith("aiMedia/"):
            # CG 开关/规则等设置：本地返回空成功，不回源
            return self._local_ok({}, "aiMedia-local")

        return False

    # ---------------------------------------------- 聊天界面
    def _sse_start(self, note: str) -> None:
        """开一条**真流式**的 SSE（HTTP/1.1，无 Content-Length，靠 close 收尾）"""
        self._note = note
        self._bytes = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse_frame(self, frame) -> None:
        """写一帧；frame=None 表示结束标记 data: [DONE]

        有事件类型时同时写 `event:` 行：正文走 message，状态/图片/计费各自成
        独立事件，前端按事件名分流，不用再从 JSON 里猜 type。
        """
        if frame is None:
            payload = b"data: [DONE]\n\n"
        else:
            name = SSE_EVENT_NAMES.get(str(frame.get("type") or ""), "")
            raw = json.dumps(frame, ensure_ascii=False)
            head = f"event: {name}\n" if name else ""
            payload = f"{head}data: {raw}\n\n".encode("utf-8")
        self.wfile.write(payload)
        self._bytes = int(getattr(self, "_bytes", 0) or 0) + len(payload)
        try:
            self.wfile.flush()             # 必须立刻落 socket，否则就不叫流式了
        except Exception:                  # noqa: BLE001
            pass

    def _emit_frame(self, frame) -> bool:
        """给编排层用的发帧端口：客户端断开时返回 False，不打断收尾落库。"""
        try:
            self._sse_frame(frame)
            return True
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return False

    def _stream_pieces(self, messages: list[dict], endpoint, fb: bool,
                       uid: int | None, rate, name: str, message: str,
                       on_send=None, meta: dict | None = None,
                       should_stop=None, stream_key: str = "",
                       cid: str = "", rebuild=None, summarize=None):
        """模型流式分片；没配模型接入 → 直接给本地兜底文本。

        上下文超限时：当轮同步压缩一次（最早历史并入滚动摘要）→ 重建 prompt
        → 只重试一次；压缩失败则回退确定性裁剪（保留最近 6 轮原文）。
        再超限就把 context_overflow 抛给上层明确报错。
        """
        if not endpoint:
            if meta is not None:
                meta["finish_reason"] = "local"
            yield self._local_reply(name, message)
            return
        base, key, model = endpoint
        # 开关开：短超时快速抛 LLMError → 回退本地，避免 300s 挂起被客户端 abort；
        # 关：多等一会（模型还在连接就不放弃），失败也不切本地、直接把错误抛给用户
        include_usage = self._include_usage()
        cur_msgs = messages
        overflow_retried = False
        while True:
            if should_stop and should_stop():
                if meta is not None:
                    meta["finish_reason"] = "stopped"
                    meta["stopped"] = True
                return
            started = False
            try:
                # 参数只算一次：既用于下发，也用于面板展示（真实值，不估算）
                params = self._model_params(uid, rate, cur_msgs)
                Handler._note_sent_params(uid, stream_key, rate, params,
                                          estimate_messages(cur_msgs))
                for piece in llm.chat_stream(base, key, model, cur_msgs,
                                             timeout=(10.0 if fb else 60.0),
                                             on_send=on_send,
                                             should_stop=should_stop,
                                             on_finish=(meta.update
                                                        if meta is not None else None),
                                             stream_key=stream_key,
                                             include_usage=include_usage,
                                             **params):
                    started = True
                    yield piece
                usage = (meta or {}).get("usage") if isinstance(meta, dict) else {}
                actual = (int(usage.get("prompt_tokens") or 0)
                          if isinstance(usage, dict) else 0)
                if actual:
                    Handler._calibrate_tokens(base, model,
                                              estimate_messages(cur_msgs), actual)
                return
            except llm.LLMError as exc:
                # 已下发内容不重画（<2000 字回滚由 chat_router 负责）
                if started or exc.code != "context_overflow" or overflow_retried:
                    raise
                overflow_retried = True
                before = estimate_messages(messages)
                keep = Handler._compress_keep_rounds(uid, cid) * 2
                compressed = False
                if rebuild and summarize:
                    try:
                        compressed = bool(summarize())   # 同步压缩（只做一次）
                        cur_msgs = rebuild()
                    except Exception as cexc:            # noqa: BLE001
                        print(f"[compress] 同步压缩失败，回退确定性裁剪：{cexc}",
                              file=sys.stderr)
                if not compressed:
                    cur_msgs = shrink_for_overflow(messages, 3, keep)
                    Handler._log_compression("sync-fallback", uid, sid, cid,
                                             before, estimate_messages(cur_msgs),
                                             "确定性裁剪", f"keep={keep}条")
                elif estimate_messages(cur_msgs) >= before:
                    # 摘要已写入，但重建后没变小 → 仍需裁剪才能重试成功
                    cur_msgs = shrink_for_overflow(cur_msgs, 3, keep)
                    Handler._log_compression("sync-trim", uid, sid, cid, before,
                                             estimate_messages(cur_msgs),
                                             "摘要未减量，补裁剪", f"keep={keep}条")
                print(f"[llm] 上下文超限 → {'已压缩' if compressed else '裁剪'}后重试一次：{exc}",
                      file=sys.stderr)

    def _decide_stream_error(self, exc: Exception, raw: str, fb: bool,
                             name: str, message: str) -> StreamDecision:
        """模型流出错：已有内容就保留，允许回退则用本地兜底，否则报错收尾。"""
        print(f"[llm] 流式失败：{exc}", file=sys.stderr)
        if isinstance(exc, llm.LLMError) and exc.code == "context_overflow":
            # 五级降级都试过仍超限：明确报错，不静默截断、也不换成本地糊过去
            return StreamDecision(
                fatal=True, src="chat-llm-overflow",
                error="上下文超出模型上限，当轮压缩重试后仍失败，"
                      "请在控制台调大上下文窗口或缩短角色设定/历史")
        if raw:
            return StreamDecision(src="chat-llm-partial")   # 绝不中途换本地
        if fb:
            return StreamDecision(src="chat-llm-failed",
                                  text=self._local_reply(name, message))
        return StreamDecision(
            fatal=True, src="chat-llm-failed",
            error=(self._stream_error_text(exc)))

    @staticmethod
    def _stream_error_text(exc: Exception) -> str:
        """上游错误 → 用户可读文案：429 是免费额度限流，不把整段 JSON 抛给前端。"""
        if isinstance(exc, llm.LLMError) and exc.status == 429:
            return "上游模型限流（免费额度已用尽，HTTP 429），请稍后重试"
        return (f"模型回复失败：{exc}"
                "（未自动切换本地模式，可在控制台「模型接入」开启超时回退）")

    @staticmethod
    def _inject_custom(msgs: list[dict], payload: dict,
                       message: str) -> None:
        """输入栏「自定」的自定义设定按位置注入到 messages。

        前端随请求带 customSettings（[{content,position,constant,keywords,enabled}]，
        position: beforeMain=主提示词前 / afterMain=主提示词后 / afterUser=用户回复后）。
        常量条目始终生效；非常量按关键词命中当前消息。
        """
        items = payload.get("customSettings")
        before: list[str] = []
        after: list[str] = []
        after_user: list[str] = []
        for it in (items if isinstance(items, list) else []):
            if not isinstance(it, dict) or not it.get("enabled", True):
                continue
            content = str(it.get("content") or "").strip()
            if not content:
                continue
            if not it.get("constant"):
                kws = [str(k).strip() for k in (it.get("keywords") or [])
                       if str(k).strip()]
                if not kws or not any(k in message for k in kws):
                    continue
            pos = str(it.get("position") or "afterMain")
            if pos == "beforeMain":
                before.append(content)
            elif pos == "afterUser":
                after_user.append(content)
            else:
                after.append(content)
        if not (before or after or after_user) or not msgs:
            return
        if msgs[0].get("role") == "system":
            body = str(msgs[0].get("content") or "")
            if before:
                body = "\n\n".join(before) + "\n\n" + body
            if after:
                body = body + "\n\n" + "\n\n".join(after)
            msgs[0]["content"] = body
        if after_user:
            for m in reversed(msgs):
                if m.get("role") == "user":
                    m["content"] = (str(m.get("content") or "") + "\n\n" +
                                    "\n\n".join(after_user))
                    break

    @staticmethod
    def _maybe_summarize(uid: int, sid: str, cid: str,
                         keep: int | None = None) -> bool:
        """滚动摘要：只总结淘汰出历史窗口的区间，游标按会话隔离。

        默认窗口 SUMMARY_WINDOW=24 条；历史压缩时会传更小的 keep（最近 N 条原文）。
        返回是否真的推进了摘要游标（供同步兜底判断压缩是否成功）。
        模型不可用 / 关闭记忆 → 返回 False，不阻塞回复。
        """
        try:
            if not Handler._memory_enabled(uid, cid) or not sid:
                return False
            with _SUMMARY_LOCK:
                rows = db.query(
                    "SELECT m.id, m.role, m.content, m.content_llm FROM chat_messages m"
                    " JOIN chat_threads t ON t.id=m.thread_id"
                    " WHERE t.user_id IS ? AND t.session_id=? ORDER BY m.id ASC",
                    (uid, sid))
                msgs = []
                for r in rows:
                    text = str(r["content_llm"] or "").strip()
                    if not text:
                        text = clean_history_for_llm(r["content"])
                    if text:
                        msgs.append({"id": int(r["id"]),
                                     "role": "user" if r["role"] == "user" else "assistant",
                                     "content": text})
                return Handler._summarize_slots(uid, sid, cid, msgs, keep=keep)
        except Exception as exc:                     # noqa: BLE001
            print(f"[memory] 摘要生成失败：{exc}", file=sys.stderr)
            return False

    # 滚动摘要：历史窗口内保留最近 24 条，只总结更早的淘汰区间
    SUMMARY_WINDOW = 24
    SUMMARY_OVERLAP = 15

    @staticmethod
    def _summarize_slots(uid: int, sid: str, cid: str,
                         msgs: list[dict], keep: int | None = None,
                         batch: int | None = None) -> bool:
        """四固定槽位滚动摘要（调用方已持有 _SUMMARY_LOCK）。

        keep：窗口内保留最近多少条原文（默认 SUMMARY_WINDOW=24）；
        batch：一次最多总结多少条淘汰消息（默认 6）。
        返回是否推进了游标；失败返回 False，由调用方回退确定性裁剪。
        """
        try:
            from roleplay_core.prompt import SLOT_BUDGET, trim_tokens
            st = db.summary_state(uid, sid)
            pending = [m for m in msgs if m["id"] > st["covered_until"]]
            window = Handler.SUMMARY_WINDOW if keep is None else max(2, int(keep))
            limit = SUMMARY_BATCH if batch is None else max(1, int(batch))
            outside = pending[:-window] if len(pending) > window else []
            older = outside[:limit]          # 24 条窗口外，且一次最多 6 条
            turns = sum(1 for m in pending if m["role"] == "assistant")
            if not older or (len(older) < limit and turns < 4):
                return False                 # 还没淘汰出窗口 / 轮数不够
            endpoint = Handler._llm_endpoint(None)
            if not endpoint:
                return False
            base, key, model = endpoint
            overlap = pending[len(older):][-Handler.SUMMARY_OVERLAP:]

            def _line(m):
                who = "用户" if m["role"] == "user" else "AI"
                return f"{who}：{m['content']}"

            seg_text = "\n".join(_line(m) for m in older)[:6000]
            overlap_text = "\n".join(_line(m) for m in overlap)[:2000]
            prompt = (
                "以下是一段角色扮演对话。请输出一个 JSON 对象，包含四个记忆槽位：\n"
                '{"short_term":"最近2~4轮剧情推进（≤300 token）",'
                '"long_term":"稳定事实/关系变化/世界观确认（≤300 token）",'
                '"user_info":"用户偏好/身份/禁忌（≤150 token）",'
                '"promise":"约定/待办/承诺（≤150 token）"}\n'
                "只输出 JSON，不要解释。\n\n"
                + (f"【上一版记忆】\n{st['rolling_summary']}\n\n"
                   if st["rolling_summary"] else "")
                + "【本次需要总结的对话】\n" + seg_text + "\n\n"
                + "【最近上下文，仅帮助理解】\n" + overlap_text)
            out = ""
            # 后台摘要参数写死，不受「对话设置 → 模型参数」影响（面板只管正文一轮）
            for piece in llm.chat_stream(base, key, model,
                                         [{"role": "user", "content": prompt}],
                                         timeout=20.0, max_tokens=900,
                                         profile=db.llm_profile(None)):
                out += piece
                if len(out) > 3000:
                    break
            text = out.strip()
            if not text:
                return False                # 模型空回复 → 不推进游标，下轮重试
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
            obj = {}
            if "{" in text and "}" in text:
                try:
                    obj = json.loads(text[text.find("{"):text.rfind("}") + 1])
                except (ValueError, TypeError):
                    obj = {}
            if not isinstance(obj, dict) or not any(
                    str(obj.get(s) or "").strip() for s in db.MEMORY_SLOTS):
                obj = {"short_term": text}  # JSON 解析失败 → 整段进短期
            cur = db.memory_slots(uid, sid)
            final = {s: str((cur.get(s) or {}).get("content") or "")
                     for s in db.MEMORY_SLOTS}
            for slot in db.MEMORY_SLOTS:
                val = str(obj.get(slot) or "").strip()
                if val:
                    final[slot] = trim_tokens(val, SLOT_BUDGET[slot])
            if not any(final.values()):
                return False
            last_id = older[-1]["id"]
            covered = st["covered_count"] + sum(
                1 for m in older if m["role"] == "assistant")
            for slot, val in final.items():
                if val:
                    db.set_memory_slot(uid, cid, sid, slot, val, last_id)
            db.update_summary_state(
                uid, cid, sid,
                rolling_summary=json.dumps(final, ensure_ascii=False),
                turns_since_update=0, covered_until=last_id,
                covered_count=covered)
            Handler._absorb_evicted(uid, cid, sid, final)
            Handler._log_compression(
                "summary", uid, sid, cid, count_tokens(seg_text),
                count_tokens(json.dumps(final, ensure_ascii=False)),
                f"{older[0]['id']}..{last_id}", last_id)
            return True
        except Exception as exc:                     # noqa: BLE001
            print(f"[memory] 槽位摘要失败：{exc}", file=sys.stderr)
            return False

    # ---------------------------------------------------- 算法记忆
    @staticmethod
    def _write_memories(uid: int, sid: str, cid: str, turn_id: str,
                        user_text: str, ai_text: str) -> None:
        """写入层：LLM 结构化抽取（失败/未到轮次回落规则）→ 去重合并 → 入库 → 整合。"""
        try:
            if not uid or not sid or not cid:
                return
            cfg = Handler._memory_cfg(uid, cid)
            if not cfg.get("enabled", True) or not Handler._memory_enabled(uid, cid):
                return
            with _MEMORY_LOCK:
                # 抽稀：每 extractEvery 轮才调一次 LLM，其余轮只用规则（零成本）
                key = f"memoryTurns:{int(uid)}:{sid}"
                st = db.get_config(key) or {}
                n = int(st.get("turns") or 0) + 1
                db.set_config(key, {"turns": n})
                every = max(1, int(cfg.get("extractEvery", 2) or 2))
                items: list[dict] = []
                if cfg.get("llmExtract", True) and n % every == 0:
                    endpoint = Handler._llm_endpoint(None)
                    if endpoint:
                        base, key_, model = endpoint
                        convo = f"用户：{user_text}\nAI：{ai_text}"[:3000]
                        try:
                            # 后台记忆抽取参数写死，不受「对话设置 → 模型参数」影响
                            out = llm.chat(
                                base, key_, model,
                                [{"role": "user",
                                  "content": memory_engine.EXTRACT_PROMPT + convo}],
                                timeout=20.0, temperature=0.2, max_tokens=600,
                                profile=db.llm_profile(None))
                            items = memory_engine.parse_extract(out)
                        except Exception as exc:         # noqa: BLE001
                            print(f"[memory] 抽取失败，回落规则：{exc}",
                                  file=sys.stderr)
                            items = []
                if not items:
                    items = memory_engine.extract_rules(user_text, ai_text)
                if not items:
                    return
                dim = int(cfg.get("embedDim", memory_engine.DEFAULT_DIM)
                          or memory_engine.DEFAULT_DIM)
                active = db.memory_items(
                    uid, cid, sid, status="active",
                    limit=int(cfg.get("capacity", 300) or 300) + 100)
                for it in items:
                    content = str(it.get("content") or "").strip()
                    if not content:
                        continue
                    it["vec"] = memory_engine.embed(content, dim)
                    action, target = memory_engine.classify_duplicate(
                        active, it, dim, float(cfg.get("dedupThreshold", 0.92)))
                    if action == "merge":
                        old = next((x for x in active
                                    if int(x["id"]) == target), None)
                        if old:
                            new_text = (content if len(content) > len(
                                str(old.get("content") or ""))
                                else str(old.get("content") or ""))
                            db.update_memory_item(
                                target, content=new_text,
                                importance=max(float(old.get("importance") or 0.0),
                                               float(it.get("importance") or 0.5)),
                                confidence=max(float(old.get("confidence") or 0.0),
                                               float(it.get("confidence") or 0.7)),
                                source_turn=turn_id, event_time=db.now_iso())
                            db.set_memory_vector(
                                target, memory_engine.embed(new_text, dim))
                            continue
                    mid = db.add_memory_item(
                        uid, cid, sid, str(it.get("kind") or "fact"), content,
                        importance=float(it.get("importance") or 0.5),
                        confidence=float(it.get("confidence") or 0.7),
                        subject=str(it.get("subject") or ""),
                        object_=str(it.get("object") or ""),
                        event_time=db.now_iso(), source_turn=turn_id,
                        embedding=it["vec"])
                    if mid and action == "replace" and target:
                        db.merge_memory_items([target], mid)   # 冲突：旧条目归档指向新条目
                    if mid:
                        active.append({**it, "id": mid,
                                       "created_at": db.now_iso(),
                                       "status": "active"})
                Handler._compact_memories(uid, cid, sid, cfg)
                Handler._ensure_vectors(uid, cid, sid, cfg, limit=32)
                db.archive_stale_memories(
                    uid, cid, sid,
                    days=float(cfg.get("archiveDays", 60) or 60),
                    max_importance=float(cfg.get("archiveImportance", 0.35)))
        except Exception as exc:                     # noqa: BLE001
            print(f"[memory] 写入失败：{exc}", file=sys.stderr)

    @staticmethod
    def _compact_memories(uid: int, cid: str, sid: str, cfg: dict) -> None:
        """整合层：容量触顶时按同主题余弦聚类归并（归档旧条目，不物理删除）。"""
        capacity = max(20, int(cfg.get("capacity", 300) or 300))
        threshold = float(cfg.get("clusterThreshold", 0.85) or 0.85)
        dim = int(cfg.get("embedDim", memory_engine.DEFAULT_DIM)
                  or memory_engine.DEFAULT_DIM)
        for _ in range(6):
            active = db.memory_items(uid, cid, sid, status="active",
                                     limit=capacity + 200)
            if len(active) <= capacity:
                return
            groups = memory_engine.cluster_items(active, threshold)
            if not groups:
                # 没有可归并主题 → 归档最低综合分的一批（保留行，不物理删除）
                spare = len(active) - capacity
                scored = memory_engine.rank_items(active, cfg)
                scored.sort(key=lambda x: x["score"])
                db.archive_memory_items([int(x["id"]) for x in scored[:spare]])
                return
            for group in groups:
                text = memory_engine.union_content(group)
                if not text:
                    continue
                mid = db.add_memory_item(
                    uid, cid, sid, str(group[0].get("kind") or "fact"), text,
                    importance=max(float(x.get("importance") or 0.0) for x in group),
                    confidence=max(float(x.get("confidence") or 0.5) for x in group),
                    event_time=db.now_iso(), source_turn="cluster",
                    embedding=memory_engine.embed(text, dim))
                if mid:
                    db.merge_memory_items([int(x["id"]) for x in group], mid)

    @staticmethod
    def _absorb_evicted(uid: int, cid: str, sid: str, final: dict) -> None:
        """整合层：滚动摘要淘汰出窗口的区间 → 并入长期记忆（结构化条目）。"""
        try:
            if not uid or not sid or not cid:
                return
            text = "；".join(str(final.get(k) or "").strip()
                            for k in ("short_term", "long_term")
                            if str(final.get(k) or "").strip())
            if not text:
                return
            content = "【已淘汰区间归档】" + text
            cfg = Handler._memory_cfg(uid, cid)
            dim = int(cfg.get("embedDim", memory_engine.DEFAULT_DIM)
                      or memory_engine.DEFAULT_DIM)
            existing = db.memory_items(uid, cid, sid, status="active",
                                       limit=int(cfg.get("capacity", 300) or 300))
            item = {"kind": "event", "content": content, "importance": 0.55,
                    "confidence": 0.75, "subject": "", "object": ""}
            action, target = memory_engine.classify_duplicate(
                existing, item, dim, float(cfg.get("dedupThreshold", 0.92)))
            if action == "merge":
                return                          # 已覆盖同类剧情，不重复写
            db.add_memory_item(uid, cid, sid, "event", content,
                               importance=0.55, confidence=0.75,
                               source_turn="summary",
                               embedding=memory_engine.embed(content, dim))
            Handler._ensure_vectors(uid, cid, sid, cfg, limit=8)
        except Exception as exc:                     # noqa: BLE001
            print(f"[memory] 淘汰区间归档失败：{exc}", file=sys.stderr)

    @staticmethod
    def _record_recalls(uid: int | None, sid: str, turn_id: str,
                        vmeta: dict | None) -> None:
        """可观测层：记录本轮召回条目 / 分项得分 / 注入深度，并累加召回次数。"""
        if not uid or not sid:
            return
        retrieved = (vmeta or {}).get("retrieved") or []
        if not retrieved:
            return
        try:
            db.log_memory_recalls(uid, sid, turn_id, retrieved)
            db.bump_memory_recall([int(x.get("id") or 0)
                                   for x in retrieved if x.get("id")])
        except Exception as exc:                     # noqa: BLE001
            print(f"[memory] 召回日志写入失败：{exc}", file=sys.stderr)

    @staticmethod
    def _memory_log(vmeta: dict | None) -> dict:
        vmeta = vmeta or {}
        return {"slots": vmeta.get("memory") or {},
                "retrieved": vmeta.get("retrieved") or []}

    @staticmethod
    def _ensure_vectors(uid: int, cid: str, sid: str, cfg: dict,
                        limit: int = 32) -> int:
        """把还没建当前模型向量的活跃记忆补齐；同内容 hash 先直接复用。"""
        eb = cfg.get("embedding") or {}
        if str(eb.get("provider") or "").strip().lower() == "local":
            from roleplay_core import local_embedder
            meta = local_embedder.active_model(
                str(eb.get("local_model") or local_embedder.DEFAULT_MODEL_NAME))
            if not meta:
                return 0
            model_id = local_embedder.model_id(meta)
            todo = db.items_missing_vector(uid, cid, sid, model_id, limit=limit)
            stored = 0
            for it in todo:
                text = " ".join(str(it.get(k) or "")
                                for k in ("content", "subject", "object"))
                vec = local_embedder.project(text, meta)
                if vec:
                    db.set_memory_vector(int(it["id"]), vec, model_id)
                    stored += 1
            if stored:
                local_embedder.maybe_retrain_async(
                    str(eb.get("local_model") or local_embedder.DEFAULT_MODEL_NAME))
            return stored
        if not eb.get("enabled") or not eb.get("base_url") or not eb.get("model"):
            return 0
        model_id = memory_engine.embedding_model_id(cfg)
        todo = db.items_missing_vector(uid, cid, sid, model_id, limit=limit)
        if not todo:
            return 0
        pending = []
        for it in todo:
            cached = db.memory_vector_by_hash(str(it.get("hash") or ""), model_id)
            if cached:
                db.set_memory_vector(int(it["id"]), cached, model_id)
            else:
                pending.append(it)
        if not pending:
            return len(todo)
        size = max(1, int(eb.get("batch_size") or 16))
        stored = 0
        for start in range(0, len(pending), size):
            batch = pending[start:start + size]
            mid, vecs, degraded = memory_engine.provider_embeddings(
                [str(x.get("content") or "") for x in batch], cfg)
            if degraded or mid == "hash":
                print("[memory] embedding 不可用，保留哈希基线", file=sys.stderr)
                break
            for it, vec in zip(batch, vecs):
                db.set_memory_vector(int(it["id"]), vec, mid)
                stored += 1
        return stored

    @staticmethod
    def _backfill_embeddings(uid: int, cid: str, sid: str = "") -> int:
        """补算入口：历史记忆按当前 embedding 模型重新向量化（后台执行）。"""
        cfg = Handler._memory_cfg(uid, cid)
        model_id = memory_engine.embedding_model_id(cfg)
        done = 0
        for _ in range(20):
            todo = db.items_missing_vector(uid, cid, sid, model_id, limit=64)
            if not todo:
                break
            Handler._ensure_vectors(uid, cid, sid, cfg, limit=64)
            rest = len(db.items_missing_vector(uid, cid, sid, model_id, limit=1))
            if rest == len(todo):
                break                      # 没有进展：provider 降级，不空转
            done += len(todo) - rest
        print(f"[memory] 向量补算完成 {done} 条（模型 {model_id}）", file=sys.stderr)
        return done

    def _image_endpoint_info(self) -> tuple:
        """绘图接入：默认接入优先，没有就本地占位（与绘图页一致）。"""
        base = key = ""
        model = "local-placeholder"
        provider_name = "本地占位绘图"
        local = True
        endpoint = db.active_image_endpoint()
        if endpoint:
            base, key, model, _sizes = endpoint
            prov = db.one("SELECT name, kind, base_url FROM image_providers"
                          " WHERE enabled=1 ORDER BY is_default DESC, id ASC")
            provider_name = str((prov or {}).get("name") or "")
            local = (str((prov or {}).get("kind") or "") == "local"
                     or str(base or "").startswith("local://"))
        return base, key, model, provider_name, local

    @staticmethod
    def _stat_rule(file_name: str) -> dict:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "rules", file_name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:                            # noqa: BLE001
            return {}

    @staticmethod
    def _stat_emotion_text(file_name: str, value: float) -> str:
        """把状态数值按规则档位翻成情绪描述（如 好感度 42 → 略有亲近感）。"""
        rule = Handler._stat_rule(file_name)
        for r in (rule.get("rules") or []):
            m = re.match(r"^\s*(-?\d+)\s*~\s*(-?\d+)\s*$", str(r.get("range") or ""))
            if not m:
                continue
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= value <= hi:
                return str(r.get("emotion") or "")
        return ""

    @staticmethod
    def _plain_text(raw, limit: int = 0) -> str:
        """富文本 → 纯文本：去掉 style/script 与标签、解实体、压空白，可截断。

        角色卡 description 是 HTML 富文本，直接进绘图提示词会把整段标签
        喂给模型，这里统一清洗后再用。
        """
        txt = str(raw or "")
        txt = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", txt, flags=re.S | re.I)
        txt = re.sub(r"<br\s*/?>|</(p|div|li|h\d)>", " ", txt, flags=re.I)
        txt = re.sub(r"<[^>]+>", " ", txt)
        txt = re.sub(r"\s+", " ", html.unescape(txt)).strip()
        return txt[:limit] if limit > 0 else txt

    def _chat_cg_defaults(self, uid, sid: str) -> dict | None:
        """聊天页生成 CG 的自动取值：角色外观 / 场景 / 情绪 / 动作状态。"""
        thread = db.one("SELECT id, character_id, character_name FROM chat_threads"
                        " WHERE user_id IS ? AND session_id=?", (uid, sid))
        if not thread:
            return None
        cid = str(thread["character_id"] or "")
        name = str(thread["character_name"] or "")
        ch = db.work_as_character(db.find_work(cid) or {}) if cid else {}
        appearance = Handler._plain_text(ch.get("description"), 200)
        scene = Handler._plain_text(ch.get("summary"), 80)
        role = (name + "：" if name else "") + appearance

        last = db.one("SELECT id, content FROM chat_messages WHERE thread_id=?"
                      " AND role!='user' ORDER BY id DESC LIMIT 1",
                      (int(thread["id"]),))
        values, say = {}, ""
        if last:
            parsed = process_output(last["content"] or "", None, True)
            values = status_dict(parsed.get("status") or []) or {}
            for seg in re.findall(r"<say>(.*?)</say>", last["content"] or "", re.S):
                if seg.strip():
                    say = Handler._plain_text(seg, 80)
        emo = []
        for stat, fname in (("好感度", "affection_rules.json"),
                            ("SAN值", "san_rules.json")):
            if stat not in values:
                continue
            try:
                v = float(values[stat])
            except (TypeError, ValueError):
                continue
            txt = Handler._stat_emotion_text(fname, v)
            emo.append(f"{stat} {int(v)}" + (f"（{txt}）" if txt else ""))
        nums = "、".join(f"{k} {values[k]}" for k in values)
        action = say[:80]
        if nums:
            action = (action + "；" if action else "") + "当前数值：" + nums
        return {"threadId": int(thread["id"]),
                "messageId": int(last["id"]) if last else 0,
                "characterId": cid,
                "role": role, "scene": scene[:80],
                "emotion": "；".join(emo), "action": action}

    # 提示词分段：顺序固定，备注永远最前（核心约束）
    CG_SEGMENTS = (("note", "备注·核心约束", "手动填写 · 权重最高"),
                   ("role", "角色外观", "角色卡 description"),
                   ("scene", "场景", "作品简介"),
                   ("emotion", "情绪", "状态面板 + 档位规则"),
                   ("action", "动作/状态", "最近回复 + 状态面板"))

    def _chat_cg_segments(self, fields: dict) -> tuple:
        segs = []
        for key, label, source in Handler.CG_SEGMENTS:
            text = re.sub(r"\s+", " ", str(fields.get(key) or "")).strip()
            if text:
                segs.append({"key": key, "label": label,
                             "source": source, "text": text})
        prompt = "\n".join(f"【{s['label']}】{s['text']}" for s in segs)
        return segs, prompt

    def _chat_cg(self, uid, payload: dict) -> bool:
        """POST /chatSession/aiCg：聊天页 AI 生成 CG。

        dryRun=true 只回拼装结果（字段预填 + prompt 预览），不生成、不扣分。
        """
        if not uid:
            return self._local_ok({"ok": False, "error": "请先登录后再生成 CG"},
                                  "ai-cg-login")
        sid = str(payload.get("sessionId") or payload.get("threadId") or "")
        base_info = self._chat_cg_defaults(uid, sid)
        if not base_info:
            return self._local_ok({"ok": False, "error": "会话不存在"}, "ai-cg-nosid")

        base, key, model, provider_name, local = self._image_endpoint_info()
        bill = db.image_charge(1, local=local)
        balance = db.points_balance(uid)
        can_afford = bill["total"] <= 0 or balance >= bill["total"]
        auto = {k: base_info[k] for k in ("role", "scene", "emotion", "action")}

        if payload.get("dryRun"):
            # 预览：用前端已编辑的字段，空段回落自动取值
            fields = {}
            for k in ("note", "role", "scene", "emotion", "action"):
                fields[k] = str(payload.get(k) or "").strip() or auto.get(k, "")
            segs, prompt = self._chat_cg_segments(fields)
            return self._local_ok({
                "ok": True, "defaults": {"note": "", **auto},
                "fields": fields, "segments": segs, "prompt": prompt,
                "cost": bill["total"], "balance": balance, "canAfford": can_afford,
                "local": local, "providerName": provider_name,
                "messageId": base_info["messageId"],
            }, "ai-cg-preview")

        fields = {}
        for k in ("note", "role", "scene", "emotion", "action"):
            fields[k] = str(payload.get(k) or "").strip()
        if not fields["note"]:
            return self._local_ok({"ok": False, "error": "请填写备注（核心约束）"},
                                  "ai-cg-nonote")
        fields = {k: (v or auto.get(k, "")) for k, v in fields.items()}
        segs, prompt = self._chat_cg_segments(fields)
        if not can_afford:
            return self._local_ok({
                "ok": False,
                "error": f"积分不足：本次需要 {bill['total']} 积分，当前余额 {balance}",
                "need": bill["total"], "balance": balance}, "ai-cg-points")

        # 默认 1:1 1K 走数据库尺寸表解析，不再在代码里硬编码像素映射
        size_row, size_err = db.image_size_lookup("1024x1024")
        if not size_row:
            return self._local_ok({"ok": False, "error": size_err}, "ai-cg-size")
        try:
            if local:
                data = imagegen.placeholder_png(
                    prompt, size_row["tier"], 0, width=int(size_row["width"]),
                    height=int(size_row["height"]))
            else:
                data = imagegen.generate(
                    base, key, model, prompt, "", n=1,
                    size=size_row["tier"], ratio=size_row["ratio"],
                    width=int(size_row["width"]), height=int(size_row["height"]),
                    tier=size_row["tier"])[0]
        except Exception as exc:                     # noqa: BLE001
            db.db_log("CHAT_CG_FAIL", "chat_cg",
                      json.dumps({"model": model, "error": str(exc)[:300]},
                                 ensure_ascii=False), uid)
            return self._local_ok({"ok": False, "error": f"生成失败：{exc}"},
                                  "ai-cg-fail")

        out = self._cg_save_image(
            uid, base_info["characterId"] or sid, data,
            f"ai_cg_{time.strftime('%Y%m%d_%H%M%S')}"
            f"{imagegen.detect_ext(data)}", "image/png", "", source="ai")
        cg_msg_id = int(payload.get("messageId") or base_info["messageId"] or 0)
        # 关联表存相对路径，回包时再按当前 Host 补全（换端口/域名也不会挂错）
        rel_url = urlparse(out["url"]).path or out["url"]
        db.chat_cg_add(uid, base_info["threadId"], cg_msg_id,
                       out["id"], rel_url, out["imageName"], prompt)
        points = min(bill["total"], balance)
        if points > 0:
            db.add_points(uid, -points)
            db.log_consumption(uid, "image", "", prompt[:60], "image", 0, points)
        out.update({"ok": True, "prompt": prompt, "segments": segs,
                    "model": model, "providerName": provider_name, "local": local,
                    "pointsDeducted": points, "balance": db.points_balance(uid),
                    "messageId": cg_msg_id, "threadId": base_info["threadId"]})
        db.db_log("CHAT_CG_GENERATE", "chat_cg",
                  json.dumps({"model": model, "local": local, "points": points},
                             ensure_ascii=False), uid)
        return self._local_ok(out, "ai-cg-generate")

    def _local_chat(self, rel: str, params: dict, payload: dict,
                    uid: int | None) -> bool:
        if rel == "narratium/guestChat/chatStream":
            return ChatTurnRunner(self, payload, uid,
                                  ChatTurnRunner.MODE_GUEST).run()
        if not rel.startswith("narratium/chatSession/"):
            return False
        sub = rel.split("/", 2)[2]

        if sub == "aiCg":
            return self._chat_cg(uid, payload)

        if sub == "list":
            cid = str(payload.get("characterId") or payload.get("characterID") or "")
            page, size = self._int(payload.get("page"), 1), self._int(payload.get("pageSize"), 20)
            data = db.chat_sessions(uid, cid, page, size)
            if not data["list"] and cid:
                # 空列表会让前端卡在「正在打开对话…」→ 直接建一个本地会话
                db.ensure_chat_session(uid, cid, title=str(payload.get("title") or ""),
                                       initial_stats=Handler._initial_stats(cid))
                data = db.chat_sessions(uid, cid, page, size)
            return self._local_ok(data, "chat-list")

        if sub == "userHistory":
            return self._local_ok(db.chat_history(
                uid, self._int(payload.get("page"), 1),
                self._int(payload.get("pageSize"), 30),
                str(payload.get("filter") or "")), "chat-history")

        if sub == "messagesOptimized":
            sid = str(payload.get("sessionId") or params.get("sessionId") or "")
            data = db.chat_messages(
                uid, sid,
                self._int(payload.get("pageSize") or params.get("pageSize"), 20),
                str(payload.get("orderBy") or params.get("orderBy") or "desc"),
                payload.get("lastMessageId") or params.get("lastMessageId"))
            # 浏览器 sessionStorage 里存的 sessionId 已经过期（历史被删过、
            # 换过账号）→ 会查不到会话行、回空消息。前端拿到 0 条消息就切到
            # 「角色正文」全屏沙箱，聊天区变成白色大块。只在**会话行不存在**时
            # 兜底回最近一个会话；新会话行存在但没消息 → 照实回空。
            if sid and not data["messages"]:
                own = db.one("SELECT 1 AS a FROM chat_threads WHERE user_id IS ?"
                             " AND session_id=?", (uid, sid))
                if own is None:
                    latest = db.one("SELECT session_id FROM chat_threads"
                                    " WHERE user_id IS ? AND session_id IS NOT NULL"
                                    " ORDER BY updated_at DESC LIMIT 1", (uid,))
                    if latest:
                        data = db.chat_messages(
                            uid, str(latest["session_id"]),
                            self._int(payload.get("pageSize"), 20),
                            str(payload.get("orderBy") or "desc"),
                            payload.get("lastMessageId"))
                        self._note = "chat-messages-fallback"
            # 历史消息也按新契约下发：content 仍是落库原文（含标签），
            # displayText 去掉状态栏/选项，status/options 单独给前端重建 DOM
            for m in (data.get("messages") or []):
                if str(m.get("role")) == "user":
                    m["displayText"] = m.get("content") or ""
                    m["status"] = []
                    m["statusValues"] = {}
                    m["options"] = []
                    continue
                r = process_output(m.get("content") or "", None, True)
                m["displayText"] = r["display_text"]
                m["status"] = r["status"]
                m["statusValues"] = status_dict(r["status"])
                m["options"] = r["options"]
            # 回填聊天里 AI 生成的 CG，刷新后仍挂在对应回复下方
            msgs = data.get("messages") or []
            if msgs:
                real_sid = str(msgs[0].get("sessionId") or sid)
                thr = db.one("SELECT id FROM chat_threads WHERE user_id IS ?"
                             " AND session_id=?", (uid, real_sid))
                if thr:
                    cg_map = db.chat_cg_for_messages(
                        int(thr["id"]), [m.get("id") for m in msgs])
                    if cg_map:
                        host = self.headers.get("Host") or "127.0.0.1:4173"
                        for items in cg_map.values():
                            for it in items:
                                it["url"] = f"http://{host}{it['url']}"
                                it["imageURL"] = it["url"]
                        for m in msgs:
                            got = cg_map.get(self._int(m.get("id"), 0))
                            if got:
                                m["cgImages"] = got
            return self._local_ok(data, self._note or "chat-messages")

        if sub == "messages":                     # 老接口名，和上面同义
            payload = {**params, **payload}
            return self._local_chat("narratium/chatSession/messagesOptimized",
                                    {}, payload, uid)

        if sub in ("update", "updateTitle"):      # 改会话标题（重命名对话）
            sid = str(payload.get("sessionId") or payload.get("id")
                       or params.get("sessionId") or "")
            title = str(payload.get("title") or params.get("title") or "").strip()
            row = db.one("SELECT * FROM chat_threads WHERE user_id IS ?"
                         " AND session_id=?", (uid, sid))
            if row is None:
                return self._local_ok(None, "chat-update-none")
            if title:
                db.execute("UPDATE chat_threads SET title=?, updated_at=?"
                           " WHERE id=?", (title, db.now_iso(), row["id"]))
                row = dict(row)
                row["title"] = title
            return self._local_ok(db._thread_view(dict(row)), "chat-update")

        if sub in ("deleteMessage", "editMessage"):   # 删一条 / 改一条消息
            raw = payload.get("messageId") or payload.get("id") \
                or params.get("messageId") or params.get("id")
            extra = payload.get("messageIds") or payload.get("ids") or []
            if not isinstance(extra, (list, tuple)):
                extra = [extra]
            ids: list[int] = []
            for v in [raw, *extra]:
                try:
                    ids.append(int(v))
                except (TypeError, ValueError):       # noqa: PERF203
                    continue
            ids = sorted(set(i for i in ids if i > 0))
            if not ids:
                return self._local_ok({"deleted": 0, "ok": False}, "chat-msg-none")
            marks = ",".join("?" * len(ids))
            # 只动**当前用户自己会话**里的消息（user_id IS ? 游客为 NULL 也对）
            scope = f"thread_id IN (SELECT id FROM chat_threads WHERE user_id IS ?)"
            if sub == "editMessage":
                text = str(payload.get("content") or payload.get("text") or "")
                # content_llm 置空 → 下次拼 Prompt 时按新原文重新清洗
                n = db.execute(f"UPDATE chat_messages SET content=?, content_llm=''"
                               f" WHERE id IN ({marks})"
                               f" AND {scope}", (text, *ids, uid)) if text else 0
                return self._local_ok({"ok": bool(n), "updated": int(n), "deleted": 0},
                                      "chat-msg-edit")
            n = db.execute(f"DELETE FROM chat_messages WHERE id IN ({marks})"
                           f" AND {scope}", (*ids, uid))
            if n:
                db.execute("UPDATE chat_threads SET message_count="
                           "(SELECT COUNT(*) FROM chat_messages WHERE thread_id=id)"
                           " WHERE user_id IS ?", (uid,))
            return self._local_ok({"deleted": int(n), "ok": bool(n)}, "chat-msg-del")


        if sub == "delete":                       # 删整个会话
            sid = str(payload.get("sessionId") or payload.get("id")
                       or params.get("sessionId") or "")
            row = db.one("SELECT id FROM chat_threads WHERE user_id IS ?"
                         " AND session_id=?", (uid, sid))
            if row is None:
                return self._local_ok({"deleted": 0}, "chat-del-none")
            with _SUMMARY_LOCK:                # 防止后台摘要线程刚写完记忆又被删
                db.delete_chat_memory_state(uid, session_id=sid)
            db.execute("DELETE FROM chat_messages WHERE thread_id=?", (row["id"],))
            db.execute("DELETE FROM chat_threads WHERE id=?", (row["id"],))
            return self._local_ok({"deleted": 1}, "chat-del")

        if sub == "getList":                      # 带 query 的会话列表
            return self._local_chat("narratium/chatSession/list",
                                    {}, {**params, **payload}, uid)

        if sub == "summaries":                    # 本地不做记忆摘要
            page = self._int(params.get("page") or payload.get("page"), 1)
            size = self._int(params.get("pageSize") or payload.get("pageSize"), 30)
            return self._local_ok({"list": [], "records": [], "total": 0,
                                   "page": page, "pageSize": size}, "chat-summaries")

        if sub == "summary":
            return self._local_ok({"summary": "", "enabled": False,
                                   "sessionId": str(payload.get("sessionId")
                                                    or params.get("sessionId") or "")},
                                  "chat-summary")

        if sub in ("stopStatus", "resumeStream"):
            # 本地永远没有在跑的流 → 无流可续、无状态要同步
            return self._local_ok({"status": "idle", "chatState": "idle",
                                   "stopped": False, "synced": True,
                                   "content": "", "messages": [],
                                   "hasMore": False}, f"chat-{sub}")

        if sub == "chatSync":
            return ChatTurnRunner(self, payload, uid,
                                  ChatTurnRunner.MODE_SYNC).run()

        if sub == "resolve":
            sid = str(params.get("sessionId") or payload.get("sessionId") or "")
            row = db.one("SELECT 1 AS a FROM chat_threads WHERE user_id IS ? AND session_id=?",
                         (uid, sid)) if sid else None
            return self._local_ok({"resolved": bool(row), "sessionId": sid,
                                   "characterId": params.get("characterId") or ""},
                                  "chat-resolve")

        if sub == "claimGuest":
            cid = str(payload.get("characterId") or "")
            view = db.ensure_chat_session(
                uid, cid, initial_stats=Handler._initial_stats(cid))
            return self._local_ok({
                "characterId": cid, "status": "ready",
                "sessionId": view["sessionId"],
                "sourceSessionId": str(uuid.uuid4()),
                "identityScope": str(uuid.uuid4()),   # ≥16 字符、UUID 形状
                "messageCount": int(view.get("messageCount") or 0),
                "title": view.get("title") or "",
            }, "chat-claim")

        if sub == "create":
            cid = str(payload.get("characterId") or "")
            # 新前端「新对话 / 重开」会带 fresh=True：强制新建一条会话行，
            # 不带时沿用最近一条（旧前端行为保持不变）。
            view = db.ensure_chat_session(
                uid, cid, title=str(payload.get("title") or ""),
                fresh=bool(payload.get("fresh") or payload.get("newSession")),
                initial_stats=Handler._initial_stats(cid))
            try:
                sid_num = int(view["sessionId"])
            except (TypeError, ValueError):  # noqa: PERF203
                sid_num = 0
            return self._local_ok({
                "sessionId": view["sessionId"], "id": sid_num, "characterId": cid,
                "userId": uid, "modelId": payload.get("modelRateId"),
                "title": view.get("title") or "", "createdAt": view.get("createdAt"),
                "updatedAt": view.get("updatedAt"), "acquisitionReused": True,
            }, "chat-create")

        if sub == "find":
            raw = str(payload.get("id") or "")
            row = db.one("SELECT * FROM chat_threads WHERE id=? AND user_id IS ?",
                         (int(raw), uid)) if raw.isdigit() else None
            view = db._thread_view(dict(row)) if row else None
            if not view:
                return self._local_ok(None, "chat-find")
            return self._local_ok({
                "sessionId": view["sessionId"], "id": self._int(view["sessionId"], 0),
                "characterId": view["characterId"], "userId": uid,
                "modelId": None, "title": view["title"],
                "createdAt": view["createdAt"], "updatedAt": view["updatedAt"],
            }, "chat-find")

        if sub == "deleteUserHistory":       # 游玩历史页·单条删除（按角色）
            cid = str(payload.get("characterId") or payload.get("characterID") or "")
            if not cid:
                return self._local_ok({"deleted": 0}, "chat-delete-none")
            with _SUMMARY_LOCK:                # 记忆随历史一起清，避免删完还能“记住”
                n = db.delete_chat_history(uid, cid)
            return self._local_ok({"deleted": n}, "chat-delete")

        if sub == "state":                    # 处理中才需要续传 → 永远空闲
            return self._local_ok({"chatState": "idle", "status": "idle"}, "chat-state")

        if sub == "partialContent":
            return self._local_ok({"status": "idle", "content": "",
                                   "messages": [], "hasMore": False}, "chat-partial")

        if sub == "chatStream":
            return ChatTurnRunner(self, payload, uid,
                                  ChatTurnRunner.MODE_STREAM).run()

        if sub == "chatStream/cancel" or sub == "stop":
            sid = str(payload.get("sessionId") or params.get("sessionId") or "")
            return self._local_ok({"stopped": _cancel_stream(uid, sid)}, "chat-stop")

        return False

    # ================================================== 创意广场（本地读写）
    def _creative_fail(self, msg: str, code: int = 1) -> bool:
        self._note = "creative-error"
        self.send_json({"code": code, "msg": msg, "data": None})
        return True

    def _creative_params(self, query: str) -> dict:
        q = parse_qs(query, keep_blank_values=True)
        out = {k: v[0] for k, v in q.items()}
        raw = q.get("tags") or []
        if raw:
            out["tags"] = raw
        return out

    def handle_creative_plaza(self, path: str, query: str) -> bool:
        """创意广场：所有读写先走本地库，缺数据时才回源线上补一次"""
        rel = path[len("/api/gva/creativePlaza"):].strip("/")
        parts = [unquote(p) for p in rel.split("/") if p != ""]
        params = self._creative_params(query)
        method = self.command
        uid = (self.current_session() or {}).get("id")
        body = b"" if method in ("GET", "HEAD") else self.read_body()
        payload = {}
        if body:
            try:
                obj = json.loads(body.decode("utf-8") or "{}")
                if isinstance(obj, dict):
                    payload = obj
            except (ValueError, UnicodeDecodeError):
                payload = {}

        def iid_at(idx: int) -> int:
            try:
                return int(parts[idx])
            except (IndexError, TypeError, ValueError):
                return 0

        item_id = iid_at(1)

        # ---------------- 公开读接口 ----------------
        if not parts or parts[0] in ("items",):
            if len(parts) == 1 and method == "GET":
                # 本地化模式：只读本地库。库里没有商品就回空列表，
                # 不走代理（否则会落到内容库的“角色列表”兜底，字段完全对不上）
                return self._local_ok(db.creative_list(params), "creative-list")
            if len(parts) == 1 and method == "POST":
                if not uid:
                    return self._creative_fail("请先登录后再发布")
                saved = db.creative_create_item(uid, payload)
                return self._local_ok(saved, "creative-create")

        if len(parts) >= 3 and parts[0] == "items" and parts[1] == "byWork" and method == "GET":
            return self._local_ok(db.creative_by_work(parts[2], params), "creative-by-work")

        if len(parts) >= 2 and parts[0] == "items":
            tail = parts[2] if len(parts) > 2 else ""

            if not tail and method == "GET":
                detail = db.creative_detail(item_id, uid)
                if detail:
                    return self._local_ok(detail, "creative-detail")
                return self._creative_fail("商品不存在")

            if not tail and method == "PUT":
                if not uid or not db.creative_update_item(uid, item_id, payload):
                    return self._creative_fail("商品不存在或无权修改")
                return self._local_ok({"itemId": item_id}, "creative-update")

            if tail == "prompt" and method == "GET":
                prompt = db.creative_prompt(item_id, uid)
                if prompt is not None:
                    return self._local_ok(prompt, "creative-prompt")
                if not db.creative_item_row(item_id):
                    return self.proxy(ORIGIN + path)
                return self._creative_fail("未购买或源码未缓存", 7)

            if tail == "comments" and method == "GET":
                # 评论只读本地库：原站按需抓取已下线，mirror.py 独立于 server.py
                size = self._int(params.get("pageSize") or params.get("page_size") or 20, 20)
                return self._local_ok(db.creative_comments(item_id, size), "creative-comments")

            if tail == "myVote" and method == "GET":
                return self._local_ok(db.creative_my_vote(uid, item_id), "creative-my-vote")

            if tail == "voteEligibility" and method == "GET":
                return self._local_ok(db.creative_vote_eligibility(uid, item_id),
                                      "creative-vote-eligibility")

            if tail == "compatibility" and method == "GET":
                return self._local_ok({"compatible": True, "checks": [],
                                       "message": "本地模式：默认兼容"}, "creative-compat")

            if tail == "export" and method == "GET":
                detail = db.creative_detail(item_id, uid)
                if not detail:
                    return self._creative_fail("商品不存在")
                return self._local_ok(detail["item"], "creative-export")

            if tail == "purchase" and method == "POST":
                idem = (payload.get("idempotencyKey")
                        or self.headers.get("Idempotency-Key") or "")[:120]
                data, err = db.creative_purchase(uid, item_id, idem)
                if err:
                    return self._creative_fail(err)
                return self._local_ok(data, "creative-purchase")

            if tail == "trial" and method == "POST":
                data, err = db.creative_start_trial(uid, item_id)
                if err:
                    return self._creative_fail(err)
                return self._local_ok(data, "creative-trial")

            if tail == "vote" and method == "POST":
                data, err = db.creative_vote(uid, item_id,
                                             str(payload.get("voteType")
                                                 or payload.get("vote_type") or ""))
                if err:
                    return self._creative_fail(err)
                return self._local_ok(data, "creative-vote")

            if tail == "comment" and method == "POST":
                data, err = db.creative_add_comment(
                    uid, item_id, str(payload.get("content") or ""),
                    self._int(payload.get("rating") or 0, 0),
                    str(payload.get("userName") or payload.get("user_name") or ""))
                if err:
                    return self._creative_fail(err)
                return self._local_ok(data, "creative-comment")

            if tail in ("delist", "relist") and method == "PUT":
                ok = db.creative_set_status(uid, item_id,
                                            "inactive" if tail == "delist" else "active")
                return self._local_ok({"success": ok}, f"creative-{tail}") if ok \
                    else self._creative_fail("商品不存在或无权操作")

            if tail == "report" and method == "POST":
                return self._local_ok({"success": True}, "creative-report")

        if parts and parts[0] == "comments":
            cid = iid_at(1)
            if len(parts) == 2 and method == "PUT":
                ok = db.creative_update_comment(uid, cid, str(payload.get("content") or ""))
                return self._local_ok({"success": ok}, "creative-comment-update") if ok \
                    else self._creative_fail("评论不存在或无权修改")
            if len(parts) == 2 and method == "DELETE":
                ok = db.creative_delete_comment(uid, cid)
                return self._local_ok({"success": ok}, "creative-comment-delete") if ok \
                    else self._creative_fail("评论不存在或无权删除")
            if len(parts) == 3 and parts[2] == "reply" and method == "POST":
                return self._local_ok({"success": True}, "creative-comment-reply")

        if parts and parts[0] == "userItems":
            if len(parts) == 1 and method == "GET":
                items = db.creative_user_items(uid)
                page = max(1, self._int(params.get("page") or 1, 1))
                size = max(1, min(self._int(params.get("pageSize")
                                            or params.get("page_size") or 50, 50), 100))
                start = (page - 1) * size
                return self._local_ok({"items": items[start:start + size],
                                       "total": len(items), "page": page,
                                       "pageSize": size}, "creative-user-items")
            uid2 = 0
            try:
                uid2 = int(parts[1])
            except (IndexError, ValueError):
                uid2 = 0
            if len(parts) == 3 and uid2:
                act = parts[2]
                if act == "enable":
                    db.creative_toggle_user_item(uid, uid2, True)
                    return self._local_ok({"activeMode": "single", "disabledItemIds": []},
                                          "creative-enable")
                if act == "disable":
                    db.creative_toggle_user_item(uid, uid2, False)
                    return self._local_ok({"success": True}, "creative-disable")
                if act == "trialConvert":
                    data, err = db.creative_trial_convert(uid, uid2)
                    if err:
                        return self._creative_fail(err)
                    return self._local_ok(data, "creative-trial-convert")
                if act == "trialAbandon":
                    db.creative_trial_abandon(uid, uid2)
                    return self._local_ok({"success": True}, "creative-trial-abandon")

        if parts and parts[0] == "myItems" and method == "GET":
            page = self._int(params.get("page") or 1, 1)
            size = self._int(params.get("pageSize") or params.get("page_size") or 20, 20)
            return self._local_ok(db.creative_my_items(uid, page, size), "creative-my-items")

        if parts and parts[0] == "catalog" and method == "GET":
            return self._local_ok(db.creative_catalog(), "creative-catalog")

        if parts and parts[0] == "tags" and method == "GET":
            return self._local_ok({"tags": db.creative_tags()}, "creative-tags")

        if parts and parts[0] == "editorChoices" and method == "GET":
            return self._local_ok({"items": db.creative_editor_choices()}, "creative-editor")

        if parts and parts[0] == "listSpotlight" and method == "GET":
            return self._local_ok(db.creative_spotlight(), "creative-spotlight")

        if parts and parts[0] == "defaults":
            if len(parts) == 1 and method == "GET":
                return self._local_ok({"defaults": db.creative_defaults(uid)}, "creative-defaults")
            if len(parts) == 2 and parts[1] == "set" and method == "POST":
                ok = db.creative_set_default(uid, str(payload.get("itemType") or ""),
                                             self._int(payload.get("itemId") or 0, 0))
                return self._local_ok({"success": ok}, "creative-default-set")
            if len(parts) == 2 and method == "DELETE":
                ok = db.creative_delete_default(uid, parts[1])
                return self._local_ok({"success": ok}, "creative-default-delete")

        if parts and parts[0] == "equip" and method == "POST":
            cid = self._int(payload.get("creativeItemId")
                            or payload.get("creative_item_id") or 0, 0)
            sid = str(payload.get("sessionId") or payload.get("session_id") or "").strip()
            wid = str(payload.get("characterId") or payload.get("character_id") or "").strip()
            if sid:
                data, err = db.creative_equip(uid, cid, "session", sid)
            elif wid:
                data, err = db.creative_equip(uid, cid, "character", wid)
            else:
                return self._creative_fail("缺少会话或角色")
            if err:
                return self._creative_fail(err)
            return self._local_ok(data, "creative-equip")

        if len(parts) >= 3 and parts[0] in ("session", "character") and parts[2] == "bindings":
            target = parts[1]
            if method == "GET":
                return self._local_ok(db.creative_bindings(uid, parts[0], target),
                                      "creative-bindings")
            if method == "DELETE" and len(parts) >= 4:
                ok = db.creative_unequip(uid, parts[0], target, iid_at(3))
                return self._local_ok({"success": ok}, "creative-unbind")

        if len(parts) == 3 and parts[0] == "session" and parts[2] == "shareConfig" \
                and method == "GET":
            return self._local_ok({"configs": db.creative_share_configs(uid, parts[1])},
                                  "creative-share")

        if parts and parts[0] == "seller":
            if parts[1:2] == ["dashboard"]:
                return self._local_ok(db.creative_seller_dashboard(uid), "creative-seller")
            if parts[1:2] == ["salesTrend"]:
                days = self._int(params.get("days") or 7, 7)
                return self._local_ok({"trend": db.creative_seller_trend(uid, days)},
                                      "creative-seller-trend")
            if parts[1:2] == ["itemPerformance"]:
                return self._local_ok({"items": db.creative_seller_items(uid)},
                                      "creative-seller-items")

        if parts and parts[0] == "tip" and method == "POST":
            return self._local_ok({"tipId": 0, "tipCount": 0, "tipTotal": 0,
                                   "balanceLeft": db.points_balance(uid) if uid else 0,
                                   "remainingTips": 0}, "creative-tip")

        if parts and parts[0] == "cliffhanger" and parts[1:2] == ["unlock"]:
            return self._local_ok({"success": True}, "creative-cliffhanger")

        if parts and parts[0] == "contextTrigger" and method == "POST":
            return self._local_ok({"triggered": False}, "creative-context")

        # 没接住的子路由 → 回源（同时由代理层落库）
        return self.proxy(ORIGIN + path + ("?" + query if query else ""))

    def handle_local_data_api(self, path: str, query: str) -> bool:
        """创作页 + 聊天界面的本地读写：命中就直接回包（True），否则继续反代（False）"""
        rel = local_data_rel(path)
        if not rel:
            return False

        params = {k: v[0] for k, v in parse_qs(query).items()}
        body = b"" if self.command in ("GET", "HEAD") else self.read_body()
        payload = {}
        if body:
            try:
                obj = json.loads(body.decode("utf-8") or "{}")
                if isinstance(obj, dict):
                    payload = obj
            except (ValueError, UnicodeDecodeError):  # noqa: BLE001
                payload = {}
        uid = (self.current_session() or {}).get("id")

        if rel.startswith(("narratium/chatSession/", "narratium/guestChat/")):
            return self._local_chat(rel, params, payload, uid)
        if rel.startswith(("narratium/modelConfig/", "narratium/summaryConfig/",
                           "narratium/promptConfig/",
                           "narratium/embeddingConfig/")):
            return self._local_prefs(rel, params, payload, uid)
        if rel.startswith("checkin/"):
            return self._local_checkin(rel, params, uid)
        if rel.startswith(("user/", "oauth/", "email/")):
            return self._local_account(rel, params, payload, uid)
        return self._local_creation(rel, params, payload, uid, body)

    def _local_prefs(self, rel: str, params: dict, payload: dict,
                     uid: int | None) -> bool:
        """对话设置里的两组参数（线上存服务端，本地按用户存 config_cache）。

        前端拿到 `data` 后直接 `value.toFixed()`，**字段一个都不能少** ——
        回 `{}` 会让「对话设置」一打开就 `Cannot read properties of undefined`。
        """
        who = int(uid or 0)                      # 配置按用户分（游客共用 0 号）

        if rel.endswith("modelConfig/getByModelRate"):
            mid = str(params.get("modelRateId") or "0").strip() or "0"
            saved = db.get_config(f"modelConfig:{who}:{mid}") or {}
            # 默认值与源站一致：温度 0.7 / TopP 0.8 / 惩罚 0 / 最长 8192。
            # 存量脏值按同一套范围钳制，避免滑条位置与数字显示不一致。
            data = {"modelRateId": self._int(mid, 0),
                    "temperature": 0.7, "topP": 0.8, "presencePenalty": 0.0,
                    "frequencyPenalty": 0.0, "maxTokens": 8192,
                    "contextWindow": 8192}
            data.update(Handler._model_config_clean(saved)[0])
            return self._local_ok(data, "model-config-get")

        if rel.endswith("modelConfig/saveByModelRate"):
            mid = str(payload.get("modelRateId") or "0").strip() or "0"
            keep, err = Handler._model_config_clean(payload, strict=True)
            if err:
                self._note = "model-config-save-bad"
                self.send_json({"code": 400, "msg": err, "data": None})
                return True
            # 深合并：面板只提交这 6 个键，不能把同键下别处写入的配置（如 contextWindow
            # 的旧值 / 未来新增字段）整包覆盖掉
            cur = db.get_config(f"modelConfig:{who}:{mid}") or {}
            cur = cur if isinstance(cur, dict) else {}
            cur.update(keep)
            db.set_config(f"modelConfig:{who}:{mid}", cur)
            return self._local_ok({"modelRateId": self._int(mid, 0), **cur},
                                  "model-config-save")

        if rel.endswith("promptConfig/getByCharacter"):
            cid = str(params.get("characterID")
                      or params.get("characterId") or "").strip()
            saved = db.get_config(f"promptEngine:{who}:{cid}") or {}
            data = {"characterId": cid,
                    "anchor": {"enabled": False, "depth": 2, "frequency": 4,
                               "text": ""},
                    "memory": self._memory_cfg(who, cid)}
            # 压缩保留轮数按「角色级 → db 键 → 常量」解析成实际生效值
            data["memory"]["compressKeepRounds"] = Handler._compress_keep_rounds(who, cid)
            # memory 已是「默认值 + 迁移后的存量配置」，不能再被原始存量覆盖
            data.update({k: v for k, v in (saved or {}).items()
                         if k in data and k != "memory"})
            return self._local_ok(data, "prompt-config-get")

        if rel.endswith("promptConfig/saveByCharacter"):
            cid = str(payload.get("characterID")
                      or payload.get("characterId") or "").strip()
            key = f"promptEngine:{who}:{cid}"
            cur = db.get_config(key) or {}
            if isinstance(payload.get("anchor"), dict):
                cur["anchor"] = payload["anchor"]
            if isinstance(payload.get("memory"), dict):
                inc = payload["memory"]
                old = cur.get("memory") if isinstance(cur.get("memory"), dict) else {}
                merged = {**old, **inc}
                if isinstance(inc.get("recall"), dict):
                    old_recall = (old.get("recall")
                                  if isinstance(old.get("recall"), dict) else {})
                    r = {**old_recall, **inc["recall"]}
                    # 打上新版本标记：之后不再走旧默认值迁移，用户改的值才生效
                    r["version"] = Handler.MEMORY_DEFAULTS["recall"]["version"]
                    for k, lo, hi in (("topK", 1, 20), ("waitMs", 1000, 15000)):
                        if k not in inc["recall"]:
                            continue
                        try:
                            r[k] = max(lo, min(hi, int(inc["recall"][k])))
                        except (TypeError, ValueError):
                            if k in old_recall:
                                r[k] = old_recall[k]
                            else:
                                r.pop(k, None)
                    merged["recall"] = r
                elif isinstance(old.get("recall"), dict):
                    merged["recall"] = old["recall"]
                # 记忆面板写入的数值统一钳制，保证前后端显示一致
                for k, lo, hi in (("compressKeepRounds", 1, 24),
                                  ("injectDepth", 0, 10),
                                  ("maxTokens", 200, 2000)):
                    if k not in inc:
                        continue
                    try:
                        merged[k] = max(lo, min(hi, int(inc[k])))
                    except (TypeError, ValueError):
                        if k in old:
                            merged[k] = old[k]
                        else:
                            merged.pop(k, None)
                cur["memory"] = merged
            db.set_config(key, cur)
            return self._local_ok({"characterId": cid, **cur},
                                  "prompt-config-save")

        if rel.endswith("embeddingConfig/get"):
            return self._local_ok({**self._emb_cfg(who), "userId": who},
                                  "embedding-config-get")

        if rel.endswith("embeddingConfig/save"):
            target = int(payload.get("userId") or who or 0)
            cur = db.get_config(f"embeddingConfig:{target}") or {}
            for k in Handler.EMBEDDING_DEFAULTS:
                if k in payload:
                    cur[k] = payload[k]
            db.set_config(f"embeddingConfig:{target}", cur)
            return self._local_ok({**self._emb_cfg(who), "userId": target},
                                  "embedding-config-save")

        if rel.endswith("embeddingConfig/backfill"):
            cid = str(payload.get("characterId") or payload.get("characterID")
                      or params.get("characterId") or params.get("characterID")
                      or "").strip()
            sid2 = str(payload.get("sessionId") or params.get("sessionId")
                       or "").strip()
            threading.Thread(target=Handler._backfill_embeddings,
                             args=(who, cid, sid2), daemon=True).start()
            return self._local_ok({"started": True, "characterId": cid,
                                   "sessionId": sid2}, "embedding-backfill")

        if rel.endswith("summaryConfig/getByCharacter"):
            cid = str(params.get("characterId") or params.get("characterID") or "").strip()
            saved = db.get_config(f"summaryConfig:{who}:{cid}") or {}
            data = {"characterId": cid, "summaryEnabled": True}
            data.update({k: v for k, v in (saved or {}).items() if k in data})
            return self._local_ok(data, "summary-config-get")

        if rel.endswith("summaryConfig/saveByCharacter"):
            cid = str(payload.get("characterId") or payload.get("characterID") or "").strip()
            keep = {k: payload.get(k) for k in ("summaryEnabled",) if k in payload}
            db.set_config(f"summaryConfig:{who}:{cid}", keep)
            return self._local_ok({"characterId": cid, **keep},
                                  "summary-config-save")

        return False

    def _local_checkin(self, rel: str, params: dict, uid: int | None) -> bool:
        """签到：状态 / 日历 / 签到 / 奖励配置全部走本地 SQLite。

        `checkin/calendar` 必须回 `{days: [{date, hasCheckin}]}`：
        设置页会按月调用再把 `data.days` 摊平成日历，回 `{}` 会让 `days`
        变成 undefined，渲染时读 `.date` 直接整页崩掉。
        """
        if uid is None:
            return False

        if rel == "checkin/reward-config":
            return self._local_ok(db.checkin_config(), "checkin-config")
        if rel == "checkin/status":
            return self._local_ok(db.checkin_status(uid), "checkin-status")
        if rel == "checkin/calendar":
            month = str(params.get("month") or "").strip()
            return self._local_ok(
                {"month": month, "days": db.checkin_calendar(uid, month)},
                "checkin-calendar")
        if rel == "checkin/sign":
            return self._local_ok(db.checkin_sign(uid), "checkin-sign")
        return False

    def _local_account(self, rel: str, params: dict, payload: dict,
                       uid: int | None) -> bool:
        """账户设置：个人资料 / 改密码 / 邮箱状态 / OAuth 绑定状态。

        头像 `headerImg` 与昵称 `nickName` 存 users 表，刷新后 bootstrap
        直接读数据库；邮箱状态也由 users.email 推导。
        """
        if uid is None:
            return False                      # 未登录 → 交给线上登录流程
        user = db.get_user(uid)
        if user is None:
            return False

        if rel == "user/getUserInfo":
            return self._local_ok({"userInfo": db.public_user(user)}, "account-info")

        if rel == "user/setSelfInfo":
            updated = db.update_user_fields(uid, payload) or user
            return self._local_ok(db.public_user(updated), "account-update")

        if rel == "user/changeMyPassword":
            old = str(payload.get("password") or "")
            new = str(payload.get("newPassword") or payload.get("new_password") or "")
            if not old or not new:
                self.send_json({"code": 1, "msg": "请输入当前密码和新密码", "data": None})
                return True
            if len(new) < 8:
                self.send_json({"code": 1, "msg": "密码至少 8 个字符", "data": None})
                return True
            if len(new.encode("utf-8")) > 72:
                self.send_json({"code": 1, "msg": "密码过长，最多 72 字节", "data": None})
                return True
            if not db.verify_password(uid, old):
                self.send_json({"code": 1, "msg": "当前密码不正确", "data": None})
                return True
            db.set_password(uid, new)
            token = self.cookies().get(SESSION_COOKIE) or self.headers.get("x-token") or ""
            return self._local_ok({"token": token, "user": db.public_user(db.get_user(uid))},
                                  "account-password")

        if rel == "oauth/bindings":
            return self._local_ok([], "oauth-bindings")
        if rel == "oauth/bind/factors":
            email = user.get("email") or ""
            return self._local_ok({
                "hasPassword": True,
                "hasEmail": bool(email),
                "maskedEmail": self._mask_email(email),
            }, "oauth-factors")
        if rel == "oauth/status":
            return self._local_ok({"google": False}, "oauth-status")

        if rel == "email/getVerificationStatus":
            email = user.get("email") or ""
            return self._local_ok({
                "email": email,
                "emailVerified": bool(email),
                "emailVerifiedAt": (user.get("created_at") or "") if email else "",
                "emailProvider": "email" if email else "",
                "selfRewardGranted": False,
                "rewardPendingRetry": False,
            }, "email-status")
        if rel == "email/getRewardConfig":
            return self._local_ok({
                "enabled": True, "selfRewardPoints": 100, "inviterRewardPoints": 50,
                "selfReward": 100, "inviterReward": 50,
            }, "email-reward-config")

        return False

    @staticmethod
    def _mask_email(email: str) -> str:
        if not email or "@" not in email:
            return ""
        name, domain = email.split("@", 1)
        keep = name[:2] if len(name) > 2 else name[:1]
        return f"{keep}{'*' * max(1, len(name) - len(keep))}@{domain}"

    # ================================================== 页面噪音接口本地化
    def _local_noise_stub(self, path: str) -> bool:
        """把反代会挂 90-210s 的页面噪音接口改成本地秒回，避免拖慢整页加载。

        覆盖：通知 SSE / 未读数 / 交互摘要 / UI 状态 / 支持未读 / 下载配置。
        这些是页面加载时并行拉取的次要接口，本地空桩即可，前端都容错。
        """
        if path == "/api/gva/narratium/notification/stream":
            # SSE 通知流：本地不推送，回 200 空流并立即结束
            # （前端把 0 字节结束当流关闭，不再像反代那样挂 90-210s）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Local", "local-sse")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self._note = "local-sse"
            return True
        if path == "/api/gva/narratium/notification/unread-count":
            return self._local_ok({"count": 0, "total": 0, "unreadCount": 0},
                                  "local-unread")
        if path == "/api/gva/narratium/interaction/v2/me/summary":
            return self._local_ok({}, "local-summary")
        if path == "/api/gva/ui/state":
            return self._local_ok({}, "local-ui-state")
        # 角色详情页：点赞/收藏状态（本地模式默认未点赞、未收藏）
        if path == "/api/gva/narratium/vote/status":
            qs = getattr(self, "query_string", "") or ""
            cid = parse_qs(qs).get("character_id", [""])[0]
            return self._local_ok({
                "characterId": cid,
                "liked": False,
                "disliked": False,
                "likeCount": 0,
                "dislikeCount": 0,
            }, "local-vote-status")
        if path == "/api/gva/narratium/favorites/check":
            try:
                body = self.read_body()
                payload = json.loads(body.decode("utf-8") or "{}") if body else {}
            except Exception:
                payload = {}
            return self._local_ok({
                "characterId": payload.get("characterId") or "",
                "favorited": False,
                "following": False,
            }, "local-fav-check")
        if path == "/api/support/unread":
            return self._local_ok({"count": 0, "unread": 0}, "local-support")
        if path == "/api/app/download-config":
            return self._local_ok({"list": [], "items": [], "config": {}},
                                  "local-download")
        return False

    # ================================================== CG 功能本地化（创作页 / 聊天）
    def _local_cg_api(self, path: str, query: str) -> bool:
        """本地化 /api/gva/narratium/cg/*：list / hintRule / toggleActive / update /
        delete / sort / upload / my-images-stats / upload-capabilities / preference /
        triggerHistory / prototypes。

        线上这些接口反代会挂 90s+（甚至超时），本地直接读 config_cache 持久化，
        秒回结构正确的数据；hintRule（CG 开关规则）与上传的 CG 图都按「用户:角色」落库。
        """
        P = "/api/gva/narratium/cg"
        if not path.startswith(P):
            return False
        sub = path[len(P):].strip("/")
        params = {k: v[0] for k, v in parse_qs(query).items()}
        uid = self._user_id
        cid = params.get("characterId") or params.get("characterID") or ""

        def bcid(p):  # body 里的角色 id（部分接口以 body 传）
            return p.get("characterId") or p.get("characterID") or cid

        # 把 /image/xxx 补成 http://host/image/xxx，前端"复制图片链接"才能拿到可用 URL
        _host = self.headers.get("Host") or "127.0.0.1:4173"
        def _abs(path):
            if not path or not isinstance(path, str):
                return path
            if path.startswith("http://") or path.startswith("https://"):
                return path
            if path.startswith("/"):
                return f"http://{_host}{path}"
            return path
        def _abs_item(x):
            if isinstance(x, dict):
                for k in ("url", "imageURL", "imageUrl"):
                    if k in x:
                        x[k] = _abs(x[k])
            return x

        if self.command in ("GET", "HEAD"):
            if sub == "list":
                return self._local_ok([_abs_item(x) for x in
                                       db.get_config_list(f"cg:images:{uid or 0}:{cid}")],
                                      "cg-list")
            if sub == "export":
                # 该角色全部 CG（含标签 / 关键词 / 触发设置）+ 平台规则选择
                images = [_abs_item(x) for x in
                          db.get_config_list(f"cg:images:{uid or 0}:{cid}")]
                rule = db.get_config(f"cg:hintRule:{uid or 0}:{cid}") or {}
                weights = _norm_cg_weights(db.get_config(f"cg:weights:{uid or 0}:{cid}"))
                return self._local_ok({
                    "version": 1,
                    "characterId": cid,
                    "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "count": len(images),
                    "weights": weights,
                    "hintRule": {
                        "mode": rule.get("mode", "platform_default"),
                        "customHintPrompt": rule.get("customHintPrompt", ""),
                    },
                    "images": images,
                }, "cg-export")
            if sub == "hintRule":
                r = db.get_config(f"cg:hintRule:{uid or 0}:{cid}") or {}
                return self._local_ok({"mode": r.get("mode", "platform_default"),
                                       "customHintPrompt": r.get("customHintPrompt", ""),
                                       "platformRule": CG_PLATFORM_RULE,
                                       "characterId": cid}, "cg-hint")
            if sub == "my-images-stats":
                images = [_abs_item(x) for x in
                          db.get_config_list(f"cg:images:{uid or 0}:{cid}")]
                active = sum(1 for x in images if x.get("isActive"))
                return self._local_ok({"summary": {"totalImages": len(images),
                                                   "activeImages": active,
                                                   "exposedImages": 0,
                                                   "missingTagImages": 0,
                                                   "totalExposures": 0},
                                       "images": images}, "cg-stats")
            if sub == "upload-capabilities":
                return self._local_ok({"version": 1, "enabled": True, "idempotent": True},
                                      "cg-upload-cap")
            if sub == "preference":
                return self._local_ok(db.get_config(f"cg:preference:{uid or 0}") or {},
                                      "cg-preference")
            if sub == "triggerHistory":
                # 前端读 data.triggers —— 给空数组，别让聊天页 CG 历史崩
                return self._local_ok({"triggers": []}, "cg-trigger-history")
            if sub == "prototypes":
                return self._local_ok([_abs_item(x) for x in
                                       db.get_config_list(f"cg:images:{uid or 0}:{cid}")],
                                      "cg-prototypes")
            if sub == "" or sub == "cg":
                return self._local_ok({
                    "list": [_abs_item(x) for x in
                             db.get_config_list(f"cg:images:{uid or 0}:{cid}")],
                    "total": 0}, "cg-base")
            return False

        # ---- 写接口 ----
        if sub == "upload":
            return self._local_cg_upload(uid, bcid(self._json_body()))
        payload = self._json_body()
        if sub == "aiGenerate" and self.command == "POST":
            # AI 生成：备注为主提示词，标签辅助，生成后原地替换该条 CG
            return self._local_cg_ai_generate(uid, bcid(payload), payload)
        if sub == "import" and self.command == "POST":
            # 导入：用 JSON 里的 images 整体覆盖该角色的 CG（字段做白名单与取值兜底）
            ch = bcid(payload)
            items = payload.get("images")
            if not ch or not isinstance(items, list):
                return self._local_ok({"imported": 0, "error": "缺少 characterId 或 images"},
                                      "cg-import-bad")
            clean, seen = [], set()
            for x in items[:300]:
                if not isinstance(x, dict):
                    continue
                pid = str(x.get("id") or x.get("imageId") or "").strip()
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                row = {"id": pid, "isActive": bool(x.get("isActive", True))}
                for k in ("url", "imageURL", "imageUrl", "imageName", "imageId",
                          "mimeType", "source", "description", "comment",
                          "managementNote", "position", "triggerMode",
                          "createdAt", "updatedAt"):
                    if isinstance(x.get(k), str):
                        row[k] = x[k]
                if isinstance(x.get("imageSize"), (int, float)) \
                        and not isinstance(x.get("imageSize"), bool):
                    row["imageSize"] = int(x["imageSize"])
                if "isPublic" in x:
                    row["isPublic"] = bool(x.get("isPublic"))
                # 库里的图片地址统一存相对路径（和上传时一致），换端口/域名也不会失效
                for k in ("url", "imageURL", "imageUrl"):
                    v = row.get(k)
                    if isinstance(v, str) and v.startswith(("http://", "https://")):
                        p = urlparse(v).path
                        if p:
                            row[k] = p
                for k in ("characterTags", "sceneTags", "emotionTags", "actionTags", "keywords"):
                    v = x.get(k)
                    if isinstance(v, list):
                        row[k] = [str(i).strip() for i in v if str(i).strip()][:10]
                    elif isinstance(v, str) and v.strip():
                        row[k] = [v.strip()]
                for k, lo, hi, dft in (("scanDepth", 1, 20, 1),
                                       ("displayDuration", 0, 3600, 0),
                                       ("priority", 1, 1000, 100),
                                       ("sortOrder", 0, 9999, 0)):
                    try:
                        row[k] = max(lo, min(hi, int(x.get(k, dft))))
                    except (TypeError, ValueError):
                        row[k] = dft
                row["triggerOnce"] = bool(x.get("triggerOnce", False))
                row["allowMulti"] = bool(x.get("allowMulti", True))
                src = x.get("triggerSource")
                if isinstance(src, list):
                    row["triggerSource"] = [str(s) for s in src if s] or ["ai"]
                elif isinstance(src, str) and src:
                    row["triggerSource"] = src
                else:
                    row["triggerSource"] = "ai"
                clean.append(row)
            db.set_config(f"cg:images:{uid or 0}:{ch}", clean)
            # 一并还原平台规则选择：默认规则 / 自定义提示词 / 关闭 CG
            rule = payload.get("hintRule")
            if not isinstance(rule, dict):
                rule = {}
            mode = str(rule.get("mode") or payload.get("mode") or "platform_default")
            if mode not in ("platform_default", "custom", "off"):
                mode = "platform_default"
            prompt = rule.get("customHintPrompt", payload.get("customHintPrompt", ""))
            db.set_config(f"cg:hintRule:{uid or 0}:{ch}", {
                "mode": mode,
                "customHintPrompt": str(prompt or ""),
            })
            # 全局权重：文件里带了才覆盖，不带就保留当前角色的设置
            if isinstance(payload.get("weights"), dict):
                db.set_config(f"cg:weights:{uid or 0}:{ch}",
                              _norm_cg_weights(payload.get("weights")))
            return self._local_ok({"imported": len(clean), "characterId": ch,
                                   "hintRule": {"mode": mode}}, "cg-import")
        if sub == "hintRule" and self.command == "PUT":
            mode = payload.get("mode", "platform_default")
            prompt = payload.get("customHintPrompt", "")
            ch = bcid(payload)
            db.set_config(f"cg:hintRule:{uid or 0}:{ch}",
                          {"mode": mode, "customHintPrompt": prompt})
            return self._local_ok({"mode": mode, "customHintPrompt": prompt,
                                   "characterId": ch}, "cg-hint-save")
        if sub == "preference" and self.command == "PUT":
            # AICG 插图开关（showCGInChat）：用户级，按 uid 存
            prefs = db.get_config(f"cg:preference:{uid or 0}") or {}
            if "showCGInChat" in payload:
                prefs["showCGInChat"] = bool(payload.get("showCGInChat"))
            db.set_config(f"cg:preference:{uid or 0}", prefs)
            return self._local_ok(prefs, "cg-preference-save")
        # 写接口的 ckey 以 body 里的 characterId 为准（前端 PUT update 带整个对象），
        # fallback 到 query 的 cid；否则 ckey 会变成 cg:images:0:（空），读写全错位。
        # 前端 delete/toggleActive 只传 {id, isActive} 不带 characterId → 反查 ckey。
        ch = bcid(payload) or cid
        if not ch and payload:
            pid_lookup = str(payload.get("id") or payload.get("imageId") or "")
            if pid_lookup:
                try:
                    rows = db.query(
                        "SELECT key, payload FROM config_cache WHERE key LIKE 'cg:images:%'")
                except Exception:  # noqa: BLE001
                    rows = []
                for row in rows:
                    try:
                        arr = json.loads(row["payload"] or "[]")
                    except Exception:  # noqa: BLE001
                        continue
                    if not isinstance(arr, list):
                        continue
                    hit = False
                    for x in arr:
                        if isinstance(x, dict) and \
                                str(x.get("id") or x.get("imageId") or "") == pid_lookup:
                            hit = True
                            break
                    if hit:
                        # key 格式: cg:images:<uid>:<cid>
                        parts = row["key"].split(":")
                        if len(parts) >= 4:
                            ch = parts[3]
                            try:
                                uid = int(parts[2])
                            except Exception:  # noqa: BLE001
                                pass
                        break
        ckey = f"cg:images:{uid or 0}:{ch}"
        images = db.get_config_list(ckey)

        def img_key(x):
            return str(x.get("id") or x.get("imageId") or "")

        if sub == "toggleActive" and self.command == "PUT":
            pid = str(payload.get("id") or payload.get("imageId") or "")
            act = bool(payload.get("isActive"))
            for x in images:
                if img_key(x) == pid:
                    x["isActive"] = act
            db.set_config(ckey, images)
            return self._local_ok({"id": pid, "isActive": act}, "cg-toggle")
        if sub == "update" and self.command == "PUT":
            pid = str(payload.get("id") or payload.get("imageId") or "")
            updated = None
            for x in images:
                if img_key(x) == pid:
                    for k, v in payload.items():
                        if k not in ("id", "characterId", "characterID"):
                            x[k] = v
                    updated = x
            db.set_config(ckey, images)
            # 返回完整更新后对象（前端会用返回值替换列表项；只回 {"id":..} 会让
            # url/imageURL 等字段丢失，列表里图片变 0 B 损坏图标）
            if updated is not None:
                return self._local_ok(_abs_item(dict(updated)), "cg-update")
            return self._local_ok({"id": pid}, "cg-update")
        if sub == "delete" and self.command == "DELETE":
            pid = str(payload.get("id") or payload.get("imageId") or "")
            # 先找到被删的那条，拿到它的文件名，同步清掉磁盘文件
            doomed = [x for x in images if img_key(x) == pid]
            for x in doomed:
                for field in ("url", "imageURL", "imageUrl"):
                    p = x.get(field)
                    if not p or not isinstance(p, str):
                        continue
                    # 去掉 scheme/host，只留路径，再取 basename
                    path = p.split("://", 1)[-1].split("/", 1)[-1]
                    fname = os.path.basename(path)
                    if not fname:
                        continue
                    try:
                        f = IMAGE_DIR / fname
                        if f.exists():
                            f.unlink()
                    except Exception:  # noqa: BLE001
                        pass
            images = [x for x in images if img_key(x) != pid]
            db.set_config(ckey, images)
            return self._local_ok({"id": pid}, "cg-delete")
        if sub == "sort" and self.command == "PUT":
            ordered = payload.get("ids") or payload.get("items") or []
            if ordered:
                merged = list(ordered)
                used = {img_key(i) for i in ordered}
                merged += [x for x in images if img_key(x) not in used]
                db.set_config(ckey, merged)
            return self._local_ok({"ok": True}, "cg-sort")
        return False

    def _local_cg_upload(self, uid, characterId: str) -> bool:
        """POST /cg/upload：multipart(characterId + file) → 落 html/image + image.db，
        生成图片对象存 config_cache，返回给前端。"""
        raw = self.read_body()
        try:
            # 手动按 boundary 分割（email.message_from_bytes 无法解析无邮件头的
            # HTTP multipart body，会把整包当纯文本，iter_parts() 返回空 → no file）
            ctype = self.headers.get("Content-Type") or ""
            m = re.search(r'boundary="?([^";]+)"?', ctype)
            if not raw or not m:
                return self._local_ok({"ok": False, "error": "缺少 boundary 或 body"},
                                      "cg-upload-nobody")
            boundary = b"--" + m.group(1).encode("utf-8", "replace")
            data = b""
            fname = "cg.png"
            mime = "image/png"
            replace_id = ""  # 编辑弹窗里"替换 CG 图片"传进来的原图 id
            for part in raw.split(boundary):
                part = part.strip(b"\r\n")
                if not part or part.startswith(b"--"):
                    continue
                if b"\r\n\r\n" not in part:
                    continue
                head, payload = part.split(b"\r\n\r\n", 1)
                head_txt = head.decode("utf-8", "replace")
                # 文本字段：characterId（body 里带就覆盖入参）
                if re.search(r'name="characterId"', head_txt) and \
                        not re.search(r'filename=', head_txt):
                    cid = payload.strip().decode("utf-8", "replace")
                    if cid:
                        characterId = cid
                    continue
                # 文本字段：replaceId（编辑弹窗替换图片时，原地替换这条 CG）
                if re.search(r'name="replaceId"', head_txt) and \
                        not re.search(r'filename=', head_txt):
                    replace_id = payload.strip().decode("utf-8", "replace")
                    continue
                # 文件字段
                fm = re.search(r'filename="([^"]*)"', head_txt)
                if not fm:
                    continue
                fname = fm.group(1) or "cg.png"
                ct = re.search(r'Content-Type:\s*([^\r\n]+)', head_txt, re.I)
                if ct:
                    mime = ct.group(1).strip()
                data = payload.rstrip(b"\r\n")
            if not data:
                return self._local_ok({"ok": False, "error": "no file"}, "cg-upload-nofile")
            if len(data) > 0xa00000:
                return self._local_ok({"ok": False, "error": "超过10MB"}, "cg-upload-size")
            return self._local_ok(
                self._cg_save_image(uid, characterId, data, fname, mime, replace_id),
                "cg-replace" if replace_id else "cg-upload")
        except Exception as exc:  # noqa: BLE001
            return self._local_ok({"ok": False, "error": str(exc)}, "cg-upload-err")

    def _cg_save_image(self, uid, characterId: str, data: bytes, display_name: str,
                       mime: str, replace_id: str = "",
                       source: str = "upload") -> dict:
        """图片落 html/image + image.db，并写入该角色的 CG 列表。

        传 replace_id 时原地替换那条 CG（保留 id / 标签 / 触发配置，删旧文件）；
        否则追加一条新 CG。返回值补全 host，供前端直接预览与复制链接。
        """
        import uuid
        ckey = f"cg:images:{uid or 0}:{characterId}"
        ext = os.path.splitext(display_name)[1].lower() or ".png"
        fname_local = "cg_" + uuid.uuid4().hex[:12] + ext
        dest = IMAGE_DIR / fname_local
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        imagedb.register("/image/" + fname_local, dest, source)
        obj = {"id": fname_local, "imageId": fname_local,
               "url": "/image/" + fname_local, "imageURL": "/image/" + fname_local,
               "imageName": display_name, "mimeType": mime,
               "source": "user" if source == "upload" else "ai",
               "isActive": True, "isPublic": False,
               "keywords": [], "characterTags": [], "sceneTags": [],
               "emotionTags": [], "actionTags": [], "description": "",
               "imageSize": len(data), "displayDuration": 0, "position": "center",
               "triggerOnce": False, "triggerSource": ["ai"], "scanDepth": 1}
        images = db.get_config_list(ckey)
        host = self.headers.get("Host") or "127.0.0.1:4173"

        def _abs(d):
            d = dict(d)
            d["url"] = f"http://{host}{d['url']}"
            d["imageURL"] = f"http://{host}{d['imageURL']}"
            return d

        if replace_id:
            target = None
            for x in images:
                if str(x.get("id") or x.get("imageId") or "") == replace_id:
                    target = x
                    break
            if target is not None:
                old_files = set()
                for k in ("url", "imageURL", "imageUrl"):
                    p = target.get(k)
                    if isinstance(p, str) and p:
                        name = os.path.basename(urlparse(p).path)
                        if name:
                            old_files.add(name)
                for name in old_files - {fname_local}:
                    try:
                        stale = IMAGE_DIR / name
                        if stale.exists():
                            stale.unlink()
                    except Exception as exc:  # noqa: BLE001
                        print(f"[cg] 旧图清理失败 {name}: {exc}")
                for k in ("url", "imageURL", "imageName", "mimeType", "imageSize"):
                    target[k] = obj[k]
                if "imageUrl" in target:  # 兼容旧条目的驼峰字段
                    target["imageUrl"] = obj["url"]
                db.set_config(ckey, images)
                out = _abs(target)
                out["replaceId"] = replace_id
                return out
            # 目标已不存在（别处删掉了）→ 退化成新增
        images.append(obj)
        db.set_config(ckey, images)
        return _abs(obj)

    # AI 生成 CG 的提示词分段：备注固定第一段（核心约束），其余按标签补充
    CG_AI_SEGMENTS = (("characterTags", "角色", "角色标签"),
                      ("sceneTags", "场景", "场景标签"),
                      ("emotionTags", "情绪", "情绪标签"),
                      ("actionTags", "动作/状态", "动作标签"))

    @staticmethod
    def _cg_ai_tags(payload: dict, field: str) -> list:
        v = payload.get(field)
        if isinstance(v, str):
            v = re.split(r"[,，]", v)
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()]

    def _cg_ai_compose(self, desc: str, payload: dict) -> tuple:
        """拼提示词：备注最前（核心约束），角色→场景→情绪→动作补在后。

        备注为空时不占位，直接按剩余要素降级拼装；标题只给标签段，
        备注保持自然语言，避免模型把标签当画面文字。
        """
        segs = []
        if desc:
            segs.append({"key": "note", "label": "备注（核心约束）",
                         "source": "手动填写", "text": desc})
        for field, label, source in Handler.CG_AI_SEGMENTS:
            vals = Handler._cg_ai_tags(payload, field)
            if vals:
                segs.append({"key": field, "label": label, "source": source,
                             "text": "、".join(vals)})
        lines = [s["text"] if s["key"] == "note" else f"{s['label']}：{s['text']}"
                 for s in segs]
        return segs, "\n".join(lines).strip()

    @staticmethod
    def _image_error_text(exc: Exception) -> str:
        """把绘图上游报错翻成看得懂的中文（401/超时/地址错/网络不通分开说）。"""
        text = str(exc)
        low = text.lower()
        if ("http 401" in low or "unauthorized" in low
                or "invalid api key" in low or "invalid_api_key" in low):
            return ("绘图服务鉴权失败（401）：API Key 无效或已过期，"
                    "请到控制台「模型接入 → 绘图」重新填写")
        if "http 403" in low:
            return "绘图服务拒绝访问（403）：请检查 API Key 权限或账户额度"
        if "http 404" in low:
            return "绘图接口地址不正确（404）：请检查绘图接入的 API 地址"
        if "超时" in text or "timeout" in low or "timed out" in low:
            return "绘图服务请求超时：请稍后重试"
        if "连不上" in text or "connection refused" in low:
            return "连不上绘图服务：请检查网络与绘图接入地址"
        return "绘图服务报错：" + text[:200]

    def _local_cg_ai_generate(self, uid, characterId: str, payload: dict) -> bool:
        """POST /cg/aiGenerate：以备注为主提示词、标签为辅，调绘图模型生成一张 CG。

        dryRun=true 只回拼装结果（提示词预览 + 费用/余额），不生成、不扣分；
        正式生成可用 payload["prompt"] 提交用户在预览里改过的完整提示词。
        传 replaceId 则原地替换该条 CG（保留备注 / 标签 / 触发配置），否则追加。
        """
        if not characterId:
            return self._local_ok({"ok": False, "error": "缺少 characterId"}, "cg-ai-nocid")

        if not db.active_image_endpoint():
            return self._local_ok({
                "ok": False,
                "error": ("未配置绘图模型：请先在控制台「模型接入 → 绘图」里"
                          "添加并启用一个绘图接入"),
                "code": "no_model"}, "cg-ai-nomodel")

        desc = str(payload.get("description") or "").strip()
        segs, prompt = self._cg_ai_compose(desc, payload)
        dry_run = bool(payload.get("dryRun"))
        if dry_run:
            # 预览阶段允许直接用前端拼好的草稿（用于回显编辑后的内容）
            prompt = str(payload.get("prompt") or prompt).strip()
        else:
            override = str(payload.get("prompt") or "").strip()
            if override:
                prompt = override
        if not prompt:
            return self._local_ok({
                "ok": False, "error": "备注和标签都为空，先填一项再生成"}, "cg-ai-noprompt")

        base, key, model, provider_name, local = self._image_endpoint_info()
        bill = db.image_charge(1, local=local)
        balance = db.points_balance(uid or 0)
        can_afford = bill["total"] <= 0 or balance >= bill["total"]

        if dry_run:
            return self._local_ok({
                "ok": True, "prompt": prompt, "segments": segs,
                "cost": bill["total"], "balance": balance,
                "canAfford": can_afford, "local": local,
                "providerName": provider_name, "model": model,
            }, "cg-ai-preview")

        if not can_afford:
            return self._local_ok({
                "ok": False,
                "error": (f"积分不足：本次需要 {bill['total']} 积分，"
                          f"当前余额 {balance}"),
                "code": "no_points",
                "need": bill["total"], "balance": balance}, "cg-ai-points")

        size_row, size_err = db.image_size_lookup("1024x1024")
        if not size_row:
            return self._local_ok({"ok": False, "error": size_err,
                                   "code": "size_invalid"}, "cg-ai-size")
        try:
            if local:
                data = imagegen.placeholder_png(
                    prompt, size_row["tier"], 0, width=int(size_row["width"]),
                    height=int(size_row["height"]))
            else:
                data = imagegen.generate(
                    base, key, model, prompt, "", n=1,
                    size=size_row["tier"], ratio=size_row["ratio"],
                    width=int(size_row["width"]), height=int(size_row["height"]),
                    tier=size_row["tier"])[0]
        except imagegen.ImageGenError as exc:
            db.db_log("CG_AI_GENERATE_FAIL", "cg_images",
                      json.dumps({"model": model, "error": str(exc)[:300]},
                                 ensure_ascii=False), uid)
            return self._local_ok({
                "ok": False, "error": Handler._image_error_text(exc),
                "code": "upstream"}, "cg-ai-fail")

        out = self._cg_save_image(
            uid, characterId, data,
            f"ai_cg_{time.strftime('%Y%m%d_%H%M%S')}.{imagegen.detect_ext(data).lstrip('.')}",
            "image/png", str(payload.get("replaceId") or ""), source="ai")
        out["prompt"] = prompt
        out["segments"] = segs
        out["model"] = model
        out["providerName"] = provider_name
        out["local"] = local
        points = min(bill["total"], balance) if uid else 0
        if points > 0:
            db.add_points(uid, -points)
            db.log_consumption(uid, "image", "", prompt[:60], "image", 0, points)
        out["pointsDeducted"] = points
        out["balance"] = db.points_balance(uid or 0)
        db.db_log("CG_AI_GENERATE", "cg_images",
                  json.dumps({"model": model, "local": local, "points": points},
                             ensure_ascii=False), uid)
        return self._local_ok(out, "cg-ai-generate")

    def route(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parsed.query

        # 分段依次尝试，任一段处理完即返回 True；全部未命中 → 兜底反代线上。
        # 段顺序有先后依赖（管理员接口在静态资源前、账号会话在业务接口前）：
        #   控制台 → 聊天配套接口 → 管理员 API → 新前端页面 → 根级页面 →
        #   旧路径跳转 → 静态资源 → 媒体文件 → 账号会话 → 业务 API → 反代兜底
        for seg in (self._route_console, self._route_chat_api,
                    self._route_admin_api, self._route_app_pages,
                    self._route_root_pages, self._route_legacy_redirects,
                    self._route_static, self._route_media, self._route_auth,
                    self._route_business_api):
            if seg(path, query):
                return
        # --- 其余（线上读接口、没抓到的 chunk）→ 代理到线上 ---
        return self.proxy(ORIGIN + path + ("?" + query if query else ""))

    def _redirect(self, location: str) -> None:
        """302 跳转（旧路径 → 新自建页）。"""
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- 1. 本地控制台：页面 + 控制台登录（仅管理员可见） ---
    def _route_console(self, path: str, query: str) -> bool:
        if path in ("/local", "/local/", "/local/login", "/local/login/"):
            # 控制台页面已合并到自建页 /app/console（原 html/console.html 已删除）；
            # 旧地址保留 302，书签与站点侧栏入口不失效，管理员校验由 /app/console 负责。
            self._redirect("/app/console")
            return True
        if path == "/local/api/login" and self.command == "POST":
            self.handle_console_login()
            return True
        return False

    # --- 2. 聊天页用户级接口（无需管理员）：选模型 / 记忆区 ---
    def _route_chat_api(self, path: str, query: str) -> bool:
        if path == "/api/chat/models" and self.command in ("GET", "HEAD"):
            self._note = "chat-models"
            self.send_json({"code": 0, "msg": "ok", "data": {
                "models": db.llm_rate_models()}})
            return True
        if path == "/api/chat/memory-status" and self.command in ("GET", "HEAD"):
            self._note = "chat-memory-status"
            sess = self.current_session() or {}
            uid = sess.get("id")
            qs = parse_qs(query)
            cid = (qs.get("characterId") or qs.get("characterID") or [""])[0]
            sid = (qs.get("sessionId") or [""])[0]
            st = (_RECALL_STATS.get((int(uid), str(sid), str(cid)))
                  if uid else None)
            data = dict(st or {})
            data["available"] = bool(st)
            self.send_json({"code": 0, "msg": "ok", "data": data})
            return True
        if path == "/api/chat/model-params-status" and self.command in ("GET", "HEAD"):
            self._note = "chat-model-params-status"
            sess = self.current_session() or {}
            uid = sess.get("id")
            qs = parse_qs(query)
            mid = str((qs.get("modelRateId") or ["0"])[0] or "0").strip() or "0"
            sid = (qs.get("sessionId") or [""])[0]
            rec = (_LAST_MODEL_PARAMS.get((int(uid), str(sid), mid))
                   if uid else None)
            data = dict(rec or {})
            data["available"] = bool(rec)
            self.send_json({"code": 0, "msg": "ok", "data": data})
            return True
        if path == "/api/chat/memories" and self.command in ("GET", "HEAD"):
            self._note = "chat-memories"
            sess = self.current_session() or {}
            uid = sess.get("id")
            cid = (parse_qs(query).get("characterId")
                   or parse_qs(query).get("characterID") or [""])[0]
            items = db.list_chat_memories(uid, cid) if uid and cid else []
            self.send_json({"code": 0, "msg": "ok",
                            "data": {"memories": items}})
            return True
        if path == "/api/chat/memories" and self.command == "DELETE":
            self._note = "chat-memories-del"
            sess = self.current_session() or {}
            uid = sess.get("id")
            mid = int((parse_qs(query).get("id") or ["0"])[0] or 0)
            n = db.delete_chat_memory_item(uid, mid) if uid and mid else 0
            self.send_json({"code": 0, "msg": "ok", "data": {"deleted": n}})
            return True
        return False

    # --- 3. /local/api/*：控制台数据接口，一律要管理员会话 ---
    def _route_admin_api(self, path: str, query: str) -> bool:
        if not path.startswith("/local/api/"):
            return False
        if not self.admin_session():
            self._note = "console-401"
            self.send_json({"code": 401, "msg": "请先用管理员账号登录",
                            "data": None}, status=401)
            return True
        self.handle_local_api(path, query)
        return True

    # --- 4. 新自建前端：/app/<name> 统一分发到 html/app/<name>.html ---
    def _route_app_pages(self, path: str, query: str) -> bool:
        if path in ("/app", "/app/"):
            self._redirect("/app/explore")
            return True
        if path.startswith("/app/"):
            rel = path[len("/app/"):]
            # 页面依赖的静态子目录（/app/css、/app/js、/app/img、/app/assets）仍走 safe 校验
            if rel.startswith(("css/", "js/", "img/", "assets/")):
                f = safe(APP_DIR, rel)
                if f:
                    self.send_file(f)
                else:
                    self.not_found()
                return True
            name = rel[:-1] if rel.endswith("/") else rel
            if not name:
                self.not_found()
                return True
            if name.endswith(".html"):
                # /app/chat.html → 302 /app/chat，统一到无扩展名形式
                base = name[:-5]
                if page_target(APP_DIR, base) is not None:
                    self._redirect("/app/" + base + ("?" + query if query else ""))
                    return True
                self.not_found()
                return True
            f = page_target(APP_DIR, name)
            if "/" in name or f is None:
                self.not_found()
                return True
            if name == "console" and not self.admin_session():
                # 控制台与 /local/api/* 一样只给管理员：未登录先出管理员登录页
                self.send_console_login()
                return True
            self.send_file(f)
            return True
        if path == "/creation-guide" or path.startswith("/creation-guide/"):
            self._redirect("/app/creation-guide")
            return True
        return False

    # --- 4b. 根级页面：/<name> → html/<name>.html（文件存在才分发，否则交给后续段） ---
    def _route_root_pages(self, path: str, query: str) -> bool:
        name = path[:-1] if path.endswith("/") else path[1:]
        if not name or "/" in name or name in ROOT_PAGE_RESERVED:
            return False
        if name.endswith(".html"):
            # /foo.html → 302 /foo（仅当 html/foo.html 确实存在）
            base = name[:-5]
            if base in ROOT_PAGE_RESERVED or page_target(HTML_DIR, base) is None:
                return False
            self._redirect("/" + base + ("?" + query if query else ""))
            return True
        f = page_target(HTML_DIR, name)
        if f is None:
            return False
        self.send_file(f)
        return True

    # --- 5. 旧镜像页路径（页面已删除）→ 302 到新自建页，老书签不失效 ---
    def _route_legacy_redirects(self, path: str, query: str) -> bool:
        base = path.rstrip("/") or "/"
        target = {
            "/": "/app/explore",
            "/home": "/app/explore",
            "/index.html": "/app/explore",
            "/favorites": "/app/explore",
            "/hot-chart": "/app/explore",
            "/profile": "/app/chat",
            "/history": "/app/chat",
            "/works": "/app/works",
            "/creators": "/app/plaza",
            "/creative-plaza": "/app/plaza",
            "/ai-creator": "/app/draw",
            "/ai-gallery": "/app/draw",
            "/ai-history": "/app/draw",
        }.get(base)
        if target is None:
            if base.startswith("/creation-guide"):
                target = "/app/creation-guide"      # /creation-guide/html-css 等子路径
            elif base.startswith("/creation/worldbook"):
                target = "/app/worldbook"
            elif base.startswith("/creation"):
                target = "/app/creation"            # /creation/* 其余 tab 同属创作页
            elif base.startswith("/creators"):
                target = "/app/plaza"               # /creators/:id 作者详情
            elif base.startswith(("/profile", "/history")):
                target = "/app/chat"
            elif base.startswith("/works"):
                target = "/app/works"
            else:
                return False
        self._redirect(target + ("?" + query if query else ""))
        return True

    # --- 6. 静态资源：前端脚本 / 样式 / 字体 / 根目录固定文件 ---
    # 只保留 /app/js/*、/app/css/* 一套路径（由 _route_app_pages 分发）；
    # 历史的 /js/*、/css/* 已下线，按未知路径 404，不再提供别名。
    def _route_static(self, path: str, query: str) -> bool:
        if path.startswith(("/js/", "/css/")):
            # 历史路径已下线：明确 404，避免落到反代回源拿到线上同名文件
            self.not_found()
            return True
        if path.startswith("/html/assets/"):
            f = safe(HTML_DIR, "assets/" + path[len("/html/assets/"):])
            if f:
                self.send_file(f)
                return True
        if path.startswith("/fonts/"):
            f = safe(ASSET_DIR / "fonts", path[len("/fonts/"):])
            if f:
                self.send_file(f)
                return True
            # 本地缺该字体 → 快速 404，让浏览器走回退字体；不再回源挂 90s
            self.not_found()
            return True
        if path in ROOT_FILES and ROOT_FILES[path].is_file():
            self.send_file(ROOT_FILES[path])
            return True
        return False

    # --- 7. 媒体文件：上传 / 图片库 / CDN 抓取 / 分析信标丢弃 ---
    def _route_media(self, path: str, query: str) -> bool:
        # 上传：本地读写（读走图片库）
        if path.startswith("/api/uploads/file/"):
            if self.command in ("POST", "PUT"):
                self.handle_upload(path)
                return True
            local = imagedb.resolve(path)
            if local:
                self._note = "image-db"
                self.send_file(local)
                return True
            rel = unquote(path[len("/api/uploads/file/"):]).lstrip("/")
            local = safe(db.UPLOAD_DIR, rel)
            if local:
                self.send_file(local)
                return True
        # 图片统一出口：先查 image.db 拿本地实际路径
        if path.startswith("/image/"):
            local = imagedb.resolve(path)
            if local:
                self._note = "image-db"
                self.send_file(local)
                return True
            cand = safe(IMAGE_DIR, unquote(path[len("/image/"):]).lstrip("/"))
            if cand and cand.is_file():
                imagedb.register(path, cand, "local")
                self.send_file(cand)
                return True
            self.not_found()
            return True
        # CDN 图片：抓一次落到 html/image/，之后全走本地图片库
        if path.startswith("/cdn/"):
            self.handle_cdn_image(CDN, path[4:])
            return True
        if path.startswith("/cdn2/"):
            self.handle_cdn_image(CDN2, path[6:])
            return True
        # Cloudflare RUM 分析信标：本地无意义，直接丢弃（不转发，避免 501 崩页面）
        if path.startswith("/cdn-cgi/") or path.startswith("/cdn-cgi-"):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self._note = "rum-dropped"
            return True
        return False

    # --- 8. 本地账号 / 会话 / 数字加减法验证码 ---
    def _route_auth(self, path: str, query: str) -> bool:
        if path in SESSION_PATHS:
            self.handle_session(path, query)
            return True
        if path in VERIFY_PATHS and self.command == "POST":
            self.handle_captcha_verify()
            return True
        if path in CHALLENGE_PATHS and self.command == "POST":
            self.handle_captcha_challenge()
            return True
        if path in LOGIN_PATHS and self.command == "POST":
            self.handle_login(path)
            return True
        if path in LOGOUT_PATHS and self.command in ("POST", "GET"):
            self.handle_logout()
            return True
        if path in CODE_PATHS and self.command == "POST":
            self.handle_code(path)
            return True
        return False

    # --- 9. 业务 API：画廊 / 探索 / 创意广场 / CG / 创作聊天本地读写 / 噪音桩 ---
    def _route_business_api(self, path: str, query: str) -> bool:
        # 画廊（绘图作品墙：列表 / 生成 / 点赞 / 删除）
        if path.startswith("/api/local/gallery"):
            self.handle_gallery(path, query)
            return True
        # 探索页：本地已发布作品列表（公开，不依赖登录）
        if path == "/api/app/explore":
            self.handle_explore(query)
            return True
        # 创意广场：商品 / 源码 / 购买 / 评论 / 装备全部本地
        if path.startswith("/api/gva/creativePlaza"):
            self.handle_creative_plaza(path, query)
            return True
        # CG 功能（创作页 CG 图 / 开关规则 / 上传）：反代会挂 90s，改本地秒回
        if path.startswith("/api/gva/narratium/cg"):
            self._local_cg_api(path, query)
            return True
        # 创作页 / 聊天界面：本地 SQLite 直接读写
        if self.handle_local_data_api(path, query):
            return True
        # 页面噪音接口本地化：反代会挂 90s，改本地秒回
        if self._local_noise_stub(path):
            return True
        return False

    def _dispatch(self) -> None:
        self._t0 = time.time()
        self._bytes = 0
        self._note = ""
        self._user_id = None
        self.response_status = None
        try:
            sess = self.current_session()
            if sess:
                self._user_id = sess["id"]
        except Exception:  # noqa: BLE001
            pass
        try:
            self.route()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # 浏览器刷新 / 关页面把还没写完的连接掐了 —— 不是服务端故障，
            # 记成 499 客户端主动断开，别混进 5xx 里吓人
            self._note = "client-abort"
            self.response_status = 499
        except Exception as exc:  # noqa: BLE001
            try:                      # 打到 stderr，方便下次直接看到是哪一行炸的
                sys.stderr.write(
                    f"500 {self.command} {self.path[:200]} -> "
                    f"{type(exc).__name__}: {exc}\n")
                sys.stderr.write(traceback.format_exc())
                sys.stderr.flush()
            except Exception:  # noqa: BLE001
                pass
            # 接口路径抛异常 → 必须把原因告诉前端（以前一律回 405，
            # 页面只能显示「保存失败」，日志里也只剩 method-not-allowed）。
            api = self.path.startswith(("/api/", "/local/api/"))
            if api:
                return self._server_error(exc)
            # 非 GET/HEAD 打到「页面路由」（本地只按 GET 处理）→ 上游会回
            # 501「Unsupported method」，统一改成本地 405，语义正确且前端可 catch
            if self.command in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                self._method_not_allowed()
            else:
                try:
                    msg = f"500 {exc}".encode("utf-8", "replace")
                    self._bytes = len(msg)
                    self.send_response(500)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                self._finish_log()
            except Exception:  # noqa: BLE001
                pass

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()


class ChatTurnRunner:
    """一次对话回合的唯一执行器（流式 / 非流式 / 试玩共用）。

    链路只有一条：prepare → build prompt → provider 流式原语 → 后处理 → 落库/计费。
    三种出口只是「输出适配器」不同，执行与后处理完全同一条：
      · stream：边收边推 SSE，收尾补 usage / done
      · sync  ：同一条流只累积文本，读完一次性回 JSON
      · guest ：同一条流只累积文本，关掉记忆/状态/计费，按试玩协议回单帧
    """

    MODE_STREAM = "stream"
    MODE_SYNC = "sync"
    MODE_GUEST = "guest"

    def __init__(self, handler, payload: dict, uid: int | None, mode: str):
        self.h = handler
        self.payload = payload if isinstance(payload, dict) else {}
        self.uid = uid
        self.mode = mode
        self.guest = mode == self.MODE_GUEST
        self.sid = ""
        self.cid = ""
        self.name = "角色"
        self.message = ""
        self.rate = None
        self.aicg: bool | None = None
        self.turn_id = ""
        self.endpoint = None
        self.fb = False
        self.meta: dict = {}
        self.sent: dict = {"messages": None}
        self.ctx_box: dict = {}
        self.prompt_hits: list[dict] = []
        self.send_hook = None
        self.user_mid = 0
        self.stop_event: threading.Event | None = None

    # ------------------------------------------------------------ 入口
    def run(self) -> bool:
        self._setup()
        if not self._check_quota():
            return True
        self._record_user()
        self._prefetch_recall()
        self.turn_id = uuid.uuid4().hex[:16]
        self.stop_event = _register_stream(self.uid, self.sid)
        self.endpoint = Handler._llm_endpoint(self.rate)
        self.fb = Handler._timeout_local_fallback()
        if (not self.guest and self.endpoint
                and self.payload.get("promptLog")):
            self.send_hook = self.prompt_hits.append
        try:
            if self.mode == self.MODE_STREAM:
                return self._run_stream()
            return self._run_collect()
        finally:
            _unregister_stream(self.uid, self.sid, self.stop_event)
            self._write_prompt_log()

    # ------------------------------------------------------------ 准备
    def _setup(self) -> None:
        p = self.payload
        self.sid = str(p.get("sessionId") or "").strip()
        self.message = str(p.get("message") or p.get("content") or "")
        self.cid = str(p.get("characterId") or p.get("characterID") or "")
        if not self.sid:
            view = db.ensure_chat_session(
                self.uid, self.cid, initial_stats=Handler._initial_stats(self.cid))
            self.sid = view["sessionId"]
        else:
            view = db.ensure_chat_session(
                self.uid, self.cid, session_id=self.sid,
                initial_stats=Handler._initial_stats(self.cid))
        self.name = str(view.get("characterName") or "角色")
        self.rate = Handler._rate_id(p)
        aicg = p.get("enableAICG")
        self.aicg = aicg if isinstance(aicg, bool) else None
        # D3：会话还是空的 → 后端补写一次角色开场白（只写一次）
        if not self.guest:
            Handler._maybe_first_mes(self.uid, self.sid, self.cid)

    def _check_quota(self) -> bool:
        """先校验积分再落库：余额不足时不能留下孤立 user 消息。"""
        if self.guest or not self.uid:
            return True
        quote = db.chat_quote(self.uid, self.message)
        if quote["cost"] <= 0 or quote["balance"] >= quote["cost"]:
            return True
        msg = (f"积分不足：本次需要 {quote['cost']} 积分，当前余额 "
               f"{quote['balance']}。可在账号中心签到或联系管理员充值。")
        if self.mode == self.MODE_STREAM:
            self.h._sse_start("chat-stream/insufficient-points")
            self.h._sse_frame({"type": "error", "message": msg})
            self.h._sse_frame({"type": "done", "assistantMessageId": 0,
                               "userMessageId": 0})
            self.h._sse_frame(None)
        else:
            self.h._creative_fail(msg, code=2)
        return False

    def _record_user(self) -> None:
        if not self.message.strip():
            return
        self.user_mid = int(db.record_chat_message(
            self.uid, self.sid, "user", self.message) or 0)

    def _prefetch_recall(self) -> None:
        """召回预取前移：与历史加载 / 上下文准备并行。"""
        if self.guest or not self.uid or not self.message.strip():
            return
        try:
            q_early, segs = Handler._recall_query_early(
                self.uid, self.sid, self.message)
            Handler._recall_prefetch(self.uid, self.sid, self.cid, q_early, segs)
        except Exception as exc:                     # noqa: BLE001
            print(f"[recall] 入口预取失败：{exc}", file=sys.stderr)

    # ------------------------------------------------------------ Prompt
    def _context(self, req: ChatRequest) -> ChatContext:
        return Handler._chat_context(self.uid, req.session_id, req.character_id,
                                     req.character_name, req.user_input,
                                     req.enable_aicg)

    def _prepare(self, ctx: ChatContext, _msgs: list[dict]) -> list[dict]:
        """整段重建：世界书/角色卡/记忆 + 输入栏自定 + 发送前预算裁剪。"""
        self.ctx_box["ctx"] = ctx
        msgs, meta = Handler._prompt_messages(
            self.uid, self.sid, self.cid, ctx, self.rate)
        self.sent["prompt"] = meta
        Handler._inject_custom(msgs, self.payload, self.message)
        # 发送前预判：先给输出预算留出空间，输入超了就确定性裁剪（无网络、无延迟）。
        # _output_budget 已保证 target ≥ 输入保底（窗口 1/4，最低 512），
        # 所以这里不再出现「maxTokens 调大 → target ≤ 0 → 跳过裁剪」的反直觉分支。
        try:
            window = int(Handler._prompt_config(self.uid, self.cid, self.rate)
                         .get("contextWindow") or 8192)
            target = window - Handler._output_budget(self.uid, self.rate) - 64
            before = estimate_messages(msgs)
            if before > target:
                keep_rounds = Handler._compress_keep_rounds(self.uid, self.cid)
                msgs, dropped = compress_for_budget(msgs, target, keep_rounds)
                after = estimate_messages(msgs)
                Handler._log_compression(
                    "pre", self.uid, self.sid, self.cid, before, after,
                    f"-{dropped}条历史", f"keep={keep_rounds}轮")
                if dropped:
                    # 被裁掉的历史进后台压缩，下一轮以摘要/记忆槽生效
                    Handler._enqueue_compress(self.uid, self.sid, self.cid)
        except Exception as exc:                     # noqa: BLE001  裁剪失败不影响本轮
            print(f"[compress] 发送前裁剪失败：{exc}", file=sys.stderr)
        if os.environ.get("MISSAI_DEBUG_PROMPT") == "1":
            print("[prompt]", json.dumps(msgs, ensure_ascii=False),
                  file=sys.stderr, flush=True)
        self.sent["messages"] = msgs
        return msgs

    def _post(self, text: str, ctx: ChatContext) -> dict:
        # 收尾拦截：模型点的 <image>ID</image> 换真实地址；没点图则按权重兜底
        cg_data = Handler._cg_payload(self.uid, self.cid, ctx.enable_aicg)
        return process_output(text, cg_data, enable_cg=bool(cg_data),
                              current_user_msg=self.message, matcher=_match_cg,
                              schema=Handler._clamp_schema(ctx))

    # ------------------------------------------------------------ 落库 / 模型
    def _save(self, ctx: ChatContext, display: str, tagged: str) -> int:
        aid = int(db.record_chat_message(self.uid, ctx.session_id, "assistant",
                                         tagged, clean_for_storage(tagged)) or 0)
        # 试玩只落消息本身：不写记忆、不推进摘要、不记召回
        if self.guest or not (self.uid and self.cid and aid):
            return aid
        Handler._record_recalls(self.uid, ctx.session_id, self.turn_id,
                                self.sent.get("prompt"))
        # 后台线程跑记忆摘要（LLM 调用最长十几秒，不能卡住收尾）
        threading.Thread(target=Handler._maybe_summarize,
                         args=(self.uid, ctx.session_id, self.cid),
                         daemon=True).start()
        if (self.sent.get("prompt") or {}).get("config"):
            threading.Thread(
                target=Handler._write_memories,
                args=(self.uid, ctx.session_id, self.cid, self.turn_id,
                      self.message, display),
                daemon=True).start()
        return aid

    def _ensure(self, ctx: ChatContext, msgs: list[dict], text: str) -> str:
        base, key, model = self.endpoint
        return self.h._ensure_format(base, key, model, msgs, text,
                                     self.uid, self.sid, on_send=self.send_hook)

    def _stream_llm(self, msgs: list[dict]):
        """provider 流式原语：唯一的下行实现（sync 也消费这条流）。"""
        return self.h._stream_pieces(
            msgs, self.endpoint, self.fb, self.uid, self.rate,
            self.name, self.message, self.send_hook,
            meta=self.meta, should_stop=self.stop_event.is_set,
            stream_key=_stream_key(self.uid, self.sid), cid=self.cid,
            summarize=(lambda: Handler._maybe_summarize(
                self.uid, self.sid, self.cid,
                keep=Handler._compress_keep_rounds(self.uid, self.cid) * 2)
                       if self.uid and self.cid else None),
            rebuild=(lambda: self._prepare(self.ctx_box["ctx"], [])))

    def _on_stream_error(self, exc: Exception, raw: str,
                         req: ChatRequest) -> StreamDecision:
        if self.guest:
            # 试玩端没有 error 正文协议：失败也当一条回复发出去
            if self.fb and not raw:
                return StreamDecision(
                    src="chat-llm-failed",
                    text=Handler._local_reply(self.name, self.message))
            return StreamDecision(src="chat-llm-failed",
                                  text=f"模型回复失败：{exc}")
        return Handler._decide_stream_error(exc, raw, self.fb,
                                            self.name, self.message)

    def _ports(self) -> ChatPorts:
        return ChatPorts(
            load_context=self._context,
            build_messages=lambda ctx: self._prepare(ctx, []),
            stream_llm=self._stream_llm,
            on_stream_error=self._on_stream_error,
            post_process=self._post,
            save_assistant=self._save,
            emit=(self.h._emit_frame if self.mode == self.MODE_STREAM
                  else (lambda frame: True)),
            local_reply=lambda req: Handler._local_reply(self.name, self.message),
            ensure_format=(self._ensure if self.endpoint else None),
            cancelled=self.stop_event.is_set,
        )

    def _request(self) -> ChatRequest:
        return ChatRequest(
            session_id=self.sid, character_id=self.cid, character_name=self.name,
            user_input=self.message, user_message_id=self.user_mid,
            enable_aicg=self.aicg,
            src=(f"chat-llm:{self.endpoint[2]}" if self.endpoint else "chat-local"),
            fail_on_empty=(bool(self.endpoint) and not self.fb
                           and not self.guest),
        )

    def _delete_user(self) -> None:
        if not self.user_mid:
            return
        try:
            db.delete_chat_message(self.uid, self.user_mid)
        except Exception:                            # noqa: BLE001
            pass

    def _persist_status(self, status) -> None:
        if self.guest or not self.uid or not status:
            return
        try:
            db.update_thread_stats(self.uid, self.sid, status_dict(status))
        except Exception as _e:                      # noqa: BLE001
            print(f"[status] 写回失败：{_e}", file=sys.stderr)

    def _write_prompt_log(self) -> None:
        if not (self.send_hook and self.prompt_hits):
            return
        final_msgs = self.sent.get("messages") or []
        try:
            db.record_chat_prompt(
                self.uid, self.sid, self.cid,
                self.endpoint[2] if self.endpoint else "",
                self.prompt_hits,
                turn_id=self.turn_id,
                finish_reason=str(self.meta.get("finish_reason") or ""),
                sections=sectionize(final_msgs),
                prompt_hash=prompt_prefix_hash(final_msgs),
                worldbook=(self.sent.get("prompt") or {}).get("worldbook"),
                memory=Handler._memory_log(self.sent.get("prompt")),
                diff=Handler._prompt_diff(self.uid, self.sid, final_msgs))
        except Exception as _e:                      # noqa: BLE001
            print(f"[prompt-log] 写入失败：{_e}", file=sys.stderr)

    # ------------------------------------------------------------ 出口：流式
    def _run_stream(self) -> bool:
        h = self.h
        h._sse_start("chat-stream/self")
        try:
            turn = ChatRouter(self._ports()).chat_stream(self._request())
        except Exception as exc:                     # noqa: BLE001
            # 生成链路异常：清掉本轮 user 消息，避免下一轮连续两条 user
            # 同时把堆栈打到 stderr：此前这里只回错误帧、不落日志，无法定位
            import traceback
            traceback.print_exc(file=sys.stderr)
            print(f"[chat] 生成链路异常：{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            self._delete_user()
            h._sse_frame({"type": "error", "message": f"生成失败：{exc}"})
            h._sse_frame({"type": "done", "assistantMessageId": 0,
                          "userMessageId": 0})
            h._sse_frame(None)
            h._note = "chat-stream/error"
            return True

        src = "chat-stream-aborted" if turn.aborted else turn.src
        try:
            if not turn.ok:
                # 开关关闭：不切本地 → 报错收尾，不落库、不扣费
                self._delete_user()
                h._sse_frame({"type": "error", "message": turn.error})
                h._sse_frame({"type": "done", "assistantMessageId": 0,
                              "userMessageId": 0,
                              "stopped": bool(turn.aborted)})
                h._sse_frame(None)
                h._note = f"chat-stream/{turn.src}"
                return True

            stopped = bool(turn.aborted)
            self._persist_status(turn.status)
            empty_stop = stopped and not turn.display_text.strip()
            if empty_stop:
                # 空取消：assistant 没落库，user 也不能留成孤立消息
                self._delete_user()
            cg = ([] if empty_stop else
                  Handler._match_cg(self.uid, self.cid, turn.display_text,
                                    self.sid))
            if turn.has_image:
                cg = []          # 正文已内嵌 <image>，不再推 cg 条避免重复
            if cg:
                host = h.headers.get("Host") or "127.0.0.1:4173"
                for c in cg:
                    u = c.get("url") or ""
                    if u.startswith("/"):
                        c["url"] = f"http://{host}{u}"
                h._sse_frame({"type": "cg", "cg": cg})
            if turn.images:
                host = h.headers.get("Host") or "127.0.0.1:4173"
                for u in turn.images:
                    if u.startswith("/"):
                        u = f"http://{host}{u}"
                    h._sse_frame({"type": "image_trigger", "url": u})

            if empty_stop:
                # 用户停止且没有产出：不落 assistant、不计费
                charge = {"deducted": 0, "balance": 0, "cost": 0,
                          "mode": "stopped", "tokens": 0, "extra": 0}
            else:
                charge = (db.charge_chat(self.uid, self.message,
                                         turn.display_text,
                                         CG_IMAGE_POINTS if turn.has_image else 0)
                          if self.uid else
                          {"deducted": 0, "balance": 0, "cost": 0,
                           "mode": "guest", "tokens": 0, "extra": 0})
            if self.uid and not empty_stop:
                db.log_consumption(self.uid, "chat", self.cid, self.name,
                                   charge["mode"], charge["tokens"],
                                   charge["deducted"])
            h._sse_frame({
                "type": "usage",
                "totalPointsDeducted": charge["deducted"],
                "pointsConsumed": charge["deducted"],
                "balance": charge["balance"],
                "billingMode": charge["mode"],
                "tokens": charge["tokens"],
                "cost": charge["cost"],
                "cgPoints": charge.get("extra", 0),
                "upstreamUsage": self.meta.get("usage") or {},
            })
            h._sse_frame({
                "type": "done",
                "assistantMessageId": turn.assistant_id,
                "userMessageId": self.user_mid,
                "displayText": turn.display_text,
                "status": turn.status,
                "statusValues": status_dict(turn.status),
                "options": turn.options,
                "finishReason": self.meta.get("finish_reason", ""),
                "truncated": (str(self.meta.get("finish_reason") or "")
                              == "length"),
                "upstreamUsage": self.meta.get("usage") or {},
                "stopped": stopped,
            })
            h._sse_frame(None)               # data: [DONE]
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            h._note = "chat-stream/aborted"
            src = "chat-stream-aborted"
        if not h._note.startswith("chat-stream/aborted"):
            h._note = f"chat-stream/{src}"
        return True

    # -------------------------------------------------- 出口：sync / guest
    def _run_collect(self) -> bool:
        """非流式 / 试玩：消费同一条流，读完再按各自协议回包。"""
        h = self.h
        try:
            turn = ChatRouter(self._ports()).chat_stream(self._request())
        except Exception as exc:                     # noqa: BLE001
            self._delete_user()
            if self.guest:
                self._guest_error(f"生成失败：{exc}")
                return True
            return h._creative_fail(f"生成失败：{exc}")

        if not turn.ok:
            self._delete_user()
            if self.guest:
                self._guest_error(turn.error)
                return True
            return h._creative_fail(turn.error)

        stopped = bool(turn.aborted)
        src = "chat-stream-aborted" if stopped else turn.src
        self._persist_status(turn.status)
        empty_stop = stopped and not turn.display_text.strip()
        if empty_stop:
            # 空取消：assistant 没落库，user 也不能留成孤立消息
            self._delete_user()
        cg = ([] if empty_stop else
              Handler._match_cg(self.uid, self.cid, turn.display_text, self.sid))
        if turn.has_image:
            cg = []

        if self.guest:
            h._sse_start(f"guest-chat/{src}")
            h._sse_frame({"type": "message", "content": turn.display_text})
            h._sse_frame({"type": "guest_info_updated", "maxChats": 20,
                          "remainingChats": 19, "totalChats": 20})
            h._sse_frame({"type": "done",
                          "assistantMessageId": turn.assistant_id,
                          "userMessageId": self.user_mid,
                          "finishReason": self.meta.get("finish_reason", ""),
                          "truncated": (str(self.meta.get("finish_reason") or "")
                                        == "length")})
            h._sse_frame(None)
            return True

        if empty_stop:
            charge = {"deducted": 0, "balance": 0, "cost": 0,
                      "mode": "stopped", "tokens": 0, "extra": 0}
        else:
            charge = (db.charge_chat(self.uid, self.message, turn.display_text,
                                     CG_IMAGE_POINTS if turn.has_image else 0)
                      if self.uid else
                      {"deducted": 0, "balance": 0, "cost": 0, "mode": "guest",
                       "tokens": 0, "extra": 0})
        if self.uid and not empty_stop:
            db.log_consumption(self.uid, "chat", self.cid, self.name,
                               charge["mode"], charge["tokens"],
                               charge["deducted"])
        return h._local_ok({
            "assistantMessageId": turn.assistant_id,
            "userMessageId": self.user_mid,
            "message": turn.display_text,
            "displayText": turn.display_text,
            "status": turn.status,
            "statusValues": status_dict(turn.status),
            "options": turn.options,
            "cg": cg,
            "pointsConsumed": charge["deducted"],
            "cgPoints": charge.get("extra", 0),
            "balance": charge["balance"],
            "billingMode": charge["mode"],
            "tokens": charge["tokens"],
            "finishReason": self.meta.get("finish_reason", ""),
            "truncated": str(self.meta.get("finish_reason") or "") == "length",
            "upstreamUsage": self.meta.get("usage") or {},
            "stoppedByUser": stopped,
            "sessionId": self.sid,
        }, f"chat-sync/{src}")

    def _guest_error(self, msg: str) -> None:
        self.h._sse_start("guest-chat/error")
        self.h._sse_frame({"type": "error", "message": msg})
        self.h._sse_frame({"type": "done", "assistantMessageId": 0,
                           "userMessageId": 0})
        self.h._sse_frame(None)


class _Server(ThreadingHTTPServer):
    """刷新页面时浏览器会同时开几十条连接 —— 默认 listen 队列只有 5，
    挤不进去的直接 ConnectionRefused（页面上表现为某个请求 failed）。"""
    request_queue_size = 128
    daemon_threads = True


# 前端是编译产物，改不了源码，只能对产物做定点替换（幂等，启动时自动补）
# 生成结果的 assetUrl：严格版只认 https + 线上白名单域名，本地 /image/*.png 会被
# 判成「生成任务数据不完整」，轮询连续失败 3 次后页面就报「暂时无法查询生成进度」。
# 换成同模块导出的宽松版（放行 / 开头的相对路径），本地图片才能正常出图。
CLIENT_PATCHES = (
    ("l=e.assetStatus,o=(0,a.l)(e.assetUrl);",
     "l=e.assetStatus,o=(0,a.v)(e.assetUrl);"),
)


def patch_client_js() -> None:
    """给 html/app/js 下的产物打补丁（幂等）：内容没变就不写盘，重启多少次都安全。"""
    for js in JS_DIR.rglob("*.js"):
        try:
            text = js.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new = text
        for old, repl in CLIENT_PATCHES:
            if old in new:
                new = new.replace(old, repl)
        if new == text:
            continue
        try:
            js.write_bytes(new.encode("utf-8"))
        except OSError:
            continue
        print(f"  补丁: {js.relative_to(ROOT)} → 本地图片地址按宽松规则校验")


# ------------------------------------------------- 平台数值规则（首次启动导入）
# data/rules 是规则的初始数据源；导入后以数据库为准（用户后续修改不会被覆盖）。
_STAT_RULE_FILES = {"affection_rules.json": "好感度", "san_rules.json": "SAN值"}


def _seed_stat_rules() -> None:
    for fname, stat in _STAT_RULE_FILES.items():
        path = ROOT / "data" / "rules" / fname
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rules = payload.get("rules") or []
            keywords = payload.get("keywords") or []
            meta = {k: payload.get(k) for k in
                    ("initial_value", "min_value", "max_value")}
        except Exception as exc:                        # noqa: BLE001
            print(f"[rules] {fname} 解析失败：{exc}", file=sys.stderr)
            continue
        n = db.seed_stat_rules(stat, rules, keywords, meta)
        if n:
            print(f"[rules] 已从 {fname} 导入 {stat} {n} 条"
                  f"（关键词 {'/'.join(keywords) or '无'}）")


def main() -> None:
    db.init()
    imagedb.init()
    # 记忆召回模型预热：后台线程，不阻塞首个请求
    threading.Thread(target=memory_recall.warmup, kwargs={"wait": 0.0},
                     name="memory-recall-warmup", daemon=True).start()
    admin = db.ensure_admin()
    _seed_stat_rules()
    imagedb.register_dir()            # html/image/ 里已有的图片先登记进图片库
    patch_client_js()                 # 前端产物补丁：本地产出的图片要能被认成合法资源
    server = _Server(("0.0.0.0", PORT), Handler)
    print("MissAI 本地镜像已启动（Python + SQLite）")
    print("  前端目录: html/  ├── js/  ├── css/  ├── image/  └── assets/")
    print(f"  站点:     http://localhost:{PORT}/home")
    print(f"  控制台:   http://localhost:{PORT}/local/")
    print(f"  数据库:   {db.DB_PATH}")
    print(f"  图片库:   {imagedb.DB_PATH} → {imagedb.IMAGE_DIR}")
    print(f"  内容库:   作品 {db.one('SELECT COUNT(*) AS c FROM works')['c']} 个 · "
          f"标签 {db.one('SELECT COUNT(DISTINCT tag) AS c FROM work_tags')['c']} 种"
          f" · 接口载荷 {db.one('SELECT COUNT(*) AS c FROM content_api')['c']} 条")
    if admin:
        print(f"  默认管理员: {db.ADMIN_USER} / {db.ADMIN_PASSWORD}"
              f"（id={admin['id']}，改过密码后不再覆盖）")
    print(f"  接口/图片反代到 {ORIGIN}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
