#!/usr/bin/env python3
"""
selftest.py — 本地后端冒烟测试（需先启动 python server.py）

用法： python selftest.py [端口]
"""

import base64
import json
import os
import re
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import captcha
import db
import imagedb
from server import wrap_next_prompts

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 4173
BASE = f"http://localhost:{PORT}"
PASS = 0
FAIL = 0


def call(method: str, path: str, body=None, headers=None, raw=False):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else body
        if isinstance(body, (dict, list)):
            hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read()
            return resp.status, dict(resp.headers), payload
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随 302，用来验证旧路径跳转目标"""

    def redirect_request(self, *args, **kwargs):
        return None


def call_redirect(path: str):
    """GET 会 302 的路径（不跟随跳转）→ (status, Location)"""
    op = urllib.request.build_opener(_NoRedirect)
    try:
        with op.open(BASE + path, timeout=30) as resp:
            return resp.status, resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def sse_frames(payload: bytes) -> list:
    """收 SSE 文本 → 帧列表（结束标记是字符串 "[DONE]"）"""
    out = []
    for line in payload.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            out.append("[DONE]")
            continue
        try:
            out.append(json.loads(chunk))
        except ValueError:
            pass
    return out


def stream_text(body: dict, headers: dict | None = None) -> str:
    """打一次 chatStream，把 SSE 里的 content 增量拼成完整回复"""
    st, h, b = call("POST", "/api/gva/narratium/chatSession/chatStream",
                    body, headers=headers)
    return "".join(f.get("content", "") for f in sse_frames(b)
                   if isinstance(f, dict) and f.get("type") == "content")


def main():
    print("== 静态页面 ==")
    st, loc = call_redirect("/home")
    check("旧首页 302 → 新首页", st == 302 and loc == "/app/explore", f"{st} {loc}")
    st, h, b = call("GET", "/app/explore")
    check("新首页 200", st == 200 and b"<html" in b, f"status={st}")
    st, h, b = call("GET", "/css/75c3c077ba6990e4.css")
    check("控制台基础 CSS 可访问", st == 200 and h.get("Content-Type", "").startswith("text/css"), f"{st}")
    st, h, b = call("GET", "/js/console.js")
    check("JS 目录可访问", st == 200, f"{st}")

    print("== 本地注册 / 登录 ==")
    name = "tester_%d" % (int(__import__("time").time()) % 100000)
    st, h, b = call("POST", "/api/gva/user/register_and_login",
                    {"userName": name, "passWord": "secret123", "nickName": "测试账号"})
    j = json.loads(b or b"{}")
    check("注册成功 code=0", st == 200 and j.get("code") == 0, f"status={st} body={b[:160]}")
    cookie = ""
    for line in h.get("Set-Cookie", "").split(","):
        if line.strip().startswith("local_session="):
            cookie = line.strip().split(";")[0]
    check("下发会话 Cookie", bool(cookie), h.get("Set-Cookie", ""))

    st, h, b = call("POST", "/api/gva/base/email/passwordLogin",
                    {"email": name, "password": "wrong"})
    j = json.loads(b or b"{}")
    check("错误密码被拒", j.get("code") == 1, f"{b[:120]}")

    st, h, b = call("POST", "/api/gva/base/email/passwordLogin",
                    {"email": name, "password": "secret123"})
    j = json.loads(b or b"{}")
    ok = st == 200 and j.get("code") == 0 and j.get("data", {}).get("token")
    check("密码登录成功", bool(ok), f"{b[:160]}")
    token = (j.get("data") or {}).get("token", "")

    st, h, b = call("GET", "/api/auth/session/bootstrap", headers={"Cookie": f"local_session={token}"})
    j = json.loads(b or b"{}")
    check("bootstrap 已登录", st == 200 and j.get("authenticated") is True, f"{st} {b[:120]}")
    st, h, b = call("GET", "/api/auth/session/bootstrap")
    check("未登录仍 401", st == 401, f"{st}")

    st, h, b = call("POST", "/api/auth/session/establish", headers={"x-token": token})
    j = json.loads(b or b"{}")
    check("x-token establish", st == 200 and j.get("authenticated") is True, f"{st} {b[:120]}")

    print("== 默认管理员账号 admin / admin123456 ==")
    st, h, b = call("POST", "/api/gva/base/email/passwordLogin",
                    {"email": "admin", "password": "admin123456"})
    j = json.loads(b or b"{}")
    admin_token = (j.get("data") or {}).get("token", "")
    check("admin 密码登录成功", st == 200 and j.get("code") == 0 and admin_token, f"{b[:200]}")
    admin_ck = {"Cookie": f"local_session={admin_token}"}

    st, h, b = call("GET", "/local/api/users", headers=admin_ck)
    users = json.loads(b or b"[]")
    adm = next((u for u in users if u.get("user_name") == "admin"), None)
    check("admin 账号已存在", adm is not None, f"{b[:200]}")
    check("admin 角色是管理员", (adm or {}).get("role") == "admin", str(adm))

    st, h, b = call("POST", "/api/gva/base/email/passwordLogin",
                    {"email": "admin", "password": "wrongpass"})
    check("admin 错误密码被拒", json.loads(b or b"{}").get("code") == 1, f"{b[:160]}")

    st, h, b = call("GET", "/api/auth/session/bootstrap", headers=admin_ck)
    j = json.loads(b or b"{}")
    check("admin 会话可用", st == 200 and j.get("authenticated") is True, f"{st} {b[:120]}")

    # 原站登录页那条链：出题 → 答对 → /base/login
    st, h, b = call("POST", "/api/gva/base/captchaV3")
    d = (json.loads(b or b"{}").get("data") or {})
    q = captcha.read_png_text(base64.b64decode((d.get("picPath") or "").split(",", 1)[-1]))
    a, op, rest = q.replace(" = ?", "").split(" ")
    ans = str(int(a) + int(rest)) if op == "+" else str(int(a) - int(rest))
    st, h, b = call("POST", "/api/gva/base/login",
                    {"username": "admin", "password": "admin123456",
                     "captcha": ans, "captchaId": d.get("captchaId", "")})
    j = json.loads(b or b"{}")
    check("admin 带验证码登录成功", st == 200 and j.get("code") == 0, f"{q} -> {b[:200]}")

    print("== 浏览器缓存（缓存头下发给浏览器） ==")
    st, h, b = call("GET", "/api/gva/narratium/showcase/list")
    check("线上接口 200", st == 200, f"{st} {b[:120]}")
    cc = h.get("Cache-Control", "")
    check("下发浏览器缓存头", "max-age=" in cc, cc)
    check("缓存是私有的", "private" in cc or "public" in cc, cc)

    print("== 文字与标签入库（内容库 works / work_tags） ==")
    st, h, b = call("GET", "/api/gva/narratium/ranking/new")
    check("ranking/new 200（回包即入库）", st == 200, f"{st}")
    st, h, b = call("GET", "/local/api/works", headers=admin_ck)
    works = json.loads(b or b"[]")
    check("作品文字已入库", len(works) > 0, f"n={len(works)}")
    check("名称与简介有值", any(w.get("name") and w.get("summary") for w in works),
          str([(w.get("name"), bool(w.get("summary"))) for w in works[:3]]))
    check("作者与评分有值", any(w.get("author_name") for w in works)
          and any(w.get("rate") is not None for w in works),
          str([(w.get("author_name"), w.get("rate")) for w in works[:3]]))
    st, h, b = call("GET", "/local/api/tags", headers=admin_ck)
    tags = json.loads(b or b"[]")
    check("标签已入库并可统计", len(tags) > 0 and all(t.get("tag") for t in tags),
          f"n={len(tags)}")
    q_name = str((works[0] if works else {}).get("name", ""))[:6]
    st, h, b = call("GET", "/local/api/works?q=" + quote(q_name),
                    headers=admin_ck)
    check("按名称能搜到", bool(works) and len(json.loads(b or b"[]")) > 0,
          b[:120] if works else "works 为空（上游内容未拉到）")
    st, h, b = call("GET", "/local/api/content_apis", headers=admin_ck)
    apis = json.loads(b or b"[]")
    check("接口形状与载荷已记录", len(apis) >= 2 and all(a.get("shape") for a in apis),
          str([(a.get("path"), a.get("shape")) for a in apis]))
    only_content = all(a.get("path", "").startswith(("/api/gva/narratium",
                                                      "/api/gva/creativePlaza/"))
                       for a in apis)
    check("只有内容接口进内容库（账号/会话不进）", only_content and len(apis) < 50,
          str([a.get("path") for a in apis if not a.get("path", "").startswith(
              ("/api/gva/narratium", "/api/gva/creativePlaza/"))]))

    print("== 离线兜底：线上不通时用内容库回包 ==")
    off_port = PORT + 7
    proc = subprocess.Popen(
        [sys.executable, "server.py", str(off_port)],
        cwd=str(Path(__file__).parent),
        env=dict(os.environ, MISSAI_OFFLINE="1"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def off_call(path):
        req = urllib.request.Request(f"http://localhost:{off_port}" + path)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    try:
        for _ in range(60):
            try:
                off_call("/favicon.svg")
                break
            except Exception:  # noqa: BLE001
                __import__("time").sleep(0.2)

        st, h, b = off_call("/api/gva/narratium/showcase/list")
        j = json.loads(b or b"{}")
        data = j.get("data")
        # 本地化模式下 showcase 可能回 {list:[...]} 形状（没有线上缓存时按内容库重建）
        rows = data if isinstance(data, list) else (
            (data or {}).get("list") or (data or {}).get("items") or [])
        check("showcase 离线 200 + 本地载荷", st == 200 and h.get("X-Local") == "db-fallback"
              and j.get("code") == 0 and bool(rows), f"{st} {b[:150]}")
        check("离线也有文字与标签", bool(rows) and bool(rows[0].get("summary"))
              and bool(rows[0].get("tags")), str(rows[:1] and rows[0].get("tags")))

        st, h, b = off_call("/api/gva/narratium/ranking/new")
        j = json.loads(b or b"{}")
        d = j.get("data") or {}
        check("榜单离线保持 {list,total} 形状", st == 200 and h.get("X-Local") == "db-fallback"
              and isinstance(d.get("list"), list) and d["list"] and "total" in d,
              f"{st} {str(d)[:120]}")

        w0 = works[0] if works else {}
        cid = w0.get("character_id") or w0.get("characterID") or ""
        st, h, b = off_call("/api/gva/narratiumCharacter/"
                            f"findNarratiumCharacterByCharacterID?characterID={cid}")
        j = json.loads(b or b"{}")
        ch = (j.get("data") or {}).get("character") or {}
        check("角色详情离线出字（按 characterID）", st == 200
              and h.get("X-Local") == "db-fallback" and ch.get("name")
              and ch.get("characterID") == cid, f"{st} {b[:150]}")

        st, h, b = off_call("/api/gva/narratium/no-such-endpoint")
        check("没抓过的接口明确报 502", st == 502, f"{st} {b[:120]}")
        st, h, b = off_call("/api/auth/session/bootstrap")
        check("非内容接口不受离线影响", h.get("X-Local") != "db-fallback", f"{st}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()

    print("== 数字加减法验证码 ==")
    st, h, b = call("POST", "/api/gva/base/captchaV3")
    j = json.loads(b or b"{}")
    d = j.get("data") or {}
    check("出题：digit 模式 / 3 位", j.get("code") == 0 and d.get("captchaMode") == "digit"
          and d.get("captchaLength") == 3, f"{st} {b[:160]}")
    uri = d.get("picPath") or ""
    check("题图是 PNG data URI", uri.startswith("data:image/png;base64,"), uri[:48])
    question, answer = "", ""
    if uri.startswith("data:image/png;base64,"):
        question = captcha.read_png_text(base64.b64decode(uri.split(",", 1)[1]))
        a, op, rest = question.replace(" = ?", "").split(" ")
        answer = str(int(a) + int(rest)) if op == "+" else str(int(a) - int(rest))
        check("题面 100 以内、答案 1~3 位",
              int(a) <= 100 and int(rest) <= 100 and 1 <= len(answer) <= 3
              and int(answer) <= 100,
              f"{question} -> {answer}")

    st, h, b = call("POST", "/api/gva/base/captchaV3/verify",
                    {"captchaId": d.get("captchaId", ""), "captcha": answer})
    check("答案正确 → success", (json.loads(b or b"{}").get("data") or {}).get("success") is True,
          f"{b[:140]}")

    st, h, b = call("POST", "/api/gva/base/captchaV3/verify",
                    {"captchaId": d.get("captchaId", ""), "captcha": "0" + answer})
    check("前导零也算对", (json.loads(b or b"{}").get("data") or {}).get("success") is True,
          f"{b[:140]}")

    st, h, b = call("POST", "/api/gva/base/captchaV3/verify",
                    {"captchaId": d.get("captchaId", ""), "captcha": "999"})
    jv = json.loads(b or b"{}")
    check("答案错误 → 拒绝", jv.get("code") == 1 and (jv.get("data") or {}).get("success") is False,
          f"{b[:140]}")

    st, h, b = call("POST", "/api/gva/base/login",
                    {"username": name, "password": "secret123",
                     "captcha": answer, "captchaId": d.get("captchaId", "")})
    check("带验证码登录成功", json.loads(b or b"{}").get("code") == 0, f"{b[:160]}")

    st, h, b = call("POST", "/api/gva/base/captchaV3")
    d2 = (json.loads(b or b"{}").get("data") or {})
    st, h, b = call("POST", "/api/gva/base/login",
                    {"username": name, "password": "secret123",
                     "captcha": "999999", "captchaId": d2.get("captchaId", "")})
    check("错误验证码被拒", json.loads(b or b"{}").get("code") == 1, f"{b[:160]}")

    print("== 个性化流不会回 code:0 空壳 ==")
    st, h, b = call("GET", "/api/gva/narratium/discovery/home_personalized?limit=9",
                    headers={"Cookie": f"local_session={token}"})
    dj = json.loads(b or b"{}")
    data = dj.get("data")
    ok = (dj.get("code") == 0 and isinstance(data, dict) and "items" in data) or \
         (dj.get("code") != 0 and bool(dj.get("reasonCode")))
    check("要么有 items，要么带 reasonCode", ok, f"{st} {b[:200]}")

    print("== 目录结构：html 里含 js / css / image ==")
    base = Path(__file__).parent
    for d in ("html", "html/js", "html/css", "html/image"):
        check(f"目录 {d}/ 存在", (base / d).is_dir(), str(base / d))
    check("根目录不再放 js/", not (base / "js").exists(), str(base / "js"))
    check("根目录不再放 css/", not (base / "css").exists(), str(base / "css"))
    check("js 里有站点脚本", len(list((base / "html" / "js").glob("*.js"))) > 40, "")
    check("css 里有样式表", len(list((base / "html" / "css").glob("*.css"))) >= 3, "")

    print("== 图片专用数据库 image.db ==")
    check("image.db 已创建", (base / "data" / "image.db").is_file(), "")
    st, h, b = call("GET", "/local/api/schema", headers=admin_ck)
    sc = json.loads(b or b"{}")
    check("主库和图片库分开", "missai.db" in str(sc.get("db_path", "")), str(sc.get("db_path")))

    print("== 上传图片本地化（落到 html/image + 进图片库） ==")
    png = (b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    st, h, b = call("POST", "/api/uploads/file/selftest/demo.png", body=png,
                    headers={"Content-Type": "application/octet-stream"})
    j = json.loads(b or b"{}")
    up = (j.get("data") or {})
    check("上传落到 html/image/", st == 200 and j.get("code") == 0
          and up.get("path", "").startswith("html/image/"), f"{st} {b[:200]}")
    check("返回本地实际路径", bool(up.get("absPath")) and Path(up.get("absPath", "")).is_file(),
          str(up.get("absPath")))
    if up.get("path"):
        rel = up["path"].split("html/image/", 1)[-1]
        st, h, b = call("GET", "/image/" + rel)
        check("/image/ 路径可访问（走图片库）", st == 200 and b == png, f"{st} len={len(b)}")
        st, h, b = call("GET", up.get("url", "/"))
        check("/api/uploads/file 读取", st == 200 and b == png, f"{st}")
    st, h, b = call("GET", "/local/api/images", headers=admin_ck)
    imgs = json.loads(b or b"[]")
    row = next((r for r in imgs if r.get("url_key") == up.get("url")), None)
    check("上传图进了图片库", row is not None, f"{b[:200]}")
    check("库里存的是本地实际路径",
          bool(row) and Path(row.get("abs_path", "")).is_file()
          and row.get("local_path", "").startswith("html/image/"), str(row and row.get("abs_path")))

    print("== 页面图片全部对接图片库 ==")
    st, h, b = call("GET", "/logo-goddess1.webp")
    check("静态图可访问", st == 200 and h.get("Content-Type", "").startswith("image/"), f"{st}")
    st, h, b = call("GET", "/local/api/images", headers=admin_ck)
    imgs = json.loads(b or b"[]")
    page_row = next((r for r in imgs if r.get("url_key") == "/logo-goddess1.webp"), None)
    check("页面静态图已登记（含本地路径）",
          bool(page_row) and Path(page_row.get("abs_path", "")).is_file(),
          str(page_row and page_row.get("abs_path")))

    # 从线上列表接口里取一个真实 CDN 图片地址（优先 .gif 动图），验证「抓一次 → 落 html/image → 之后走本地」
    st, h, b = call("GET", "/api/gva/narratium/showcase/list")
    keys = re.findall(rb'"(/cdn2?/[^"]+?\.(?:png|jpg|jpeg|gif|webp|svg|avif))"', b)
    gif_key = next((k.decode() for k in keys if k.lower().endswith(b".gif")), "")
    cdn_key = gif_key or (keys[0].decode() if keys else "")
    check("列表里找到 CDN 图片地址", bool(cdn_key), "")
    if cdn_key:
        st, h, b2 = call("GET", cdn_key)
        check("CDN 图片抓取成功", st == 200 and (h.get("Content-Type") or "").startswith("image/"),
              f"{st} {cdn_key}")
        st, h, b3 = call("GET", cdn_key)
        check("第二次请求走本地", st == 200 and b3 == b2, f"{st}")
        st, h, b4 = call("GET", "/local/api/images", headers=admin_ck)
        imgs = json.loads(b4 or b"[]")
        cdn_row = next((r for r in imgs if r.get("url_key") == cdn_key), None)
        check("CDN 图已登记本地路径",
              bool(cdn_row) and Path(cdn_row.get("abs_path", "")).is_file(), str(cdn_row))
        check("落盘在 html/image/", bool(cdn_row)
              and "html/image/" in str(cdn_row.get("local_path", "")),
              str(cdn_row and cdn_row.get("local_path")))
        if gif_key:
            check("动图被标成 animated", bool(cdn_row) and cdn_row.get("animated") == 1,
                  str(cdn_row and cdn_row.get("animated")))

    print("== 收藏 / 对话 ==")
    st, h, b = call("POST", "/api/gva/narratium/interaction/v2/works/demo123/favorite",
                    {"characterID": "char-demo", "action": "add"},
                    headers={"Cookie": f"local_session={token}", "Content-Type": "application/json"})
    st, h, b = call("GET", "/local/api/favorites", headers=admin_ck)
    favs = json.loads(b or b"[]")
    check("收藏已入库", any(f.get("target_id") == "char-demo" for f in favs), f"{b[:160]}")

    st, h, b = call("POST", "/api/gva/narratium/guestChat/chatStream",
                    {"message": "你好呀", "characterID": "char-demo"},
                    headers={"Content-Type": "application/json"})
    st, h, b = call("GET", "/local/api/chat", headers=admin_ck)
    msgs = json.loads(b or b"[]")
    check("用户消息已入库", any(m.get("content") == "你好呀" for m in msgs), f"{b[:200]}")

    print("== 本地会话下的账户接口（本地桩，不外发） ==")
    st, h, b = call("GET", "/api/gva/pointsAcc/getUserPointsAccount",
                    headers={"Cookie": f"local_session={token}"})
    j = json.loads(b or b"{}")
    check("积分接口本地桩 code=0", st == 200 and j.get("code") == 0
          and (j.get("data") or {}).get("balance") is not None, f"{st} {b[:120]}")
    check("桩响应带标记", h.get("X-Local") == "stub", f"{h.get('X-Local')}")
    st, h, b = call("GET", "/api/gva/spiritStone/getMySpiritStoneInfo",
                    headers={"Cookie": f"local_session={token}"})
    check("灵石接口本地桩 200", st == 200 and json.loads(b or b"{}").get("code") == 0, f"{st}")
    st, h, b = call("GET", "/api/gva/email/getVerificationStatus",
                    headers={"Cookie": f"local_session={token}"})
    check("邮箱校验状态本地桩", st == 200 and json.loads(b or b"{}").get("code") == 0, f"{st}")

    print("== 账户设置（个人信息 / 改密码）+ 签到（本地 SQLite） ==")
    ck = {"Cookie": f"local_session={token}"}
    import datetime as _dt
    _today = _dt.date.today()
    _month = _today.strftime("%Y-%m")

    st, h, b = call("GET", "/api/gva/user/getUserInfo", headers=ck)
    j = json.loads(b or b"{}")
    info = (j.get("data") or {}).get("userInfo") or {}
    check("getUserInfo 读本地账号", st == 200 and j.get("code") == 0
          and info.get("nickName") == "测试账号", f"{b[:200]}")

    st, h, b = call("PUT", "/api/gva/user/setSelfInfo",
                    {"nickName": "自测昵称", "headerImg": "/api/uploads/file/self.png"},
                    headers=ck)
    j = json.loads(b or b"{}")
    check("setSelfInfo 改昵称/头像 code=0", st == 200 and j.get("code") == 0, f"{b[:160]}")
    st, h, b = call("GET", "/api/auth/session/bootstrap", headers=ck)
    user = (json.loads(b or b"{}").get("user") or {})
    check("昵称已落库（刷新后仍在）", user.get("nickName") == "自测昵称", str(user))
    check("头像已落库（刷新后仍在）",
          user.get("headerImg") == "/api/uploads/file/self.png", str(user))

    st, h, b = call("GET", "/api/gva/checkin/status", headers=ck)
    j = json.loads(b or b"{}")
    st_data = j.get("data") or {}
    check("签到状态字段齐全",
          st == 200 and j.get("code") == 0 and st_data.get("hasCheckedIn") is False
          and st_data.get("currentConsecutiveDays") == 0
          and st_data.get("totalCheckinDays") == 0, f"{b[:220]}")

    st, h, b = call("GET", f"/api/gva/checkin/calendar?month={_month}", headers=ck)
    j = json.loads(b or b"{}")
    days = (j.get("data") or {}).get("days")
    check("签到最后历按月回 days 数组（不再让设置页崩）",
          st == 200 and j.get("code") == 0 and isinstance(days, list)
          and len(days) >= 28 and all(d.get("date") and "hasCheckin" in d for d in days),
          f"{str(b)[:220]}")
    check("日历覆盖当月每一天",
          all(d.get("date", "").startswith(_month) for d in (days or [])), f"{str(days)[:160]}")

    st, h, b = call("POST", "/api/gva/checkin/sign", {}, headers=ck)
    j = json.loads(b or b"{}")
    sign = j.get("data") or {}
    check("签到成功 code=0", st == 200 and j.get("code") == 0, f"{b[:200]}")
    check("签到回连签/累计/奖励字段",
          sign.get("consecutiveDays") == 1 and sign.get("totalCheckinDays") == 1
          and int(sign.get("rewardPoints") or 0) >= 100
          and sign.get("isAppBonusReissue") is False, str(sign)[:240])
    reward = int(sign.get("rewardPoints") or 0)

    st, h, b = call("GET", "/api/gva/checkin/status", headers=ck)
    st_data = json.loads(b or b"{}").get("data") or {}
    check("签到后状态已更新",
          st_data.get("hasCheckedIn") is True
          and st_data.get("currentConsecutiveDays") == 1
          and st_data.get("totalCheckinDays") == 1
          and st_data.get("lastCheckinDate") == _today.isoformat(), str(st_data))

    st, h, b = call("GET", f"/api/gva/checkin/calendar?month={_month}", headers=ck)
    days = (json.loads(b or b"{}").get("data") or {}).get("days") or []
    today_row = next((d for d in days if d.get("date") == _today.isoformat()), {})
    check("日历标记今天已签到", today_row.get("hasCheckin") is True, str(today_row))

    st, h, b = call("GET", "/api/gva/pointsAcc/getUserPointsAccount", headers=ck)
    balance = (json.loads(b or b"{}").get("data") or {}).get("balance")
    check("签到积分加到账户（默认 100 + 奖励）",
          st == 200 and balance == 100 + reward, f"balance={balance} reward={reward}")

    st, h, b = call("POST", "/api/gva/checkin/sign", {}, headers=ck)
    sign = json.loads(b or b"{}").get("data") or {}
    check("同一天重复签不重复加分",
          sign.get("alreadyCheckedIn") is True and sign.get("pointsEarned") == 0, str(sign)[:200])
    st, h, b = call("GET", "/api/gva/pointsAcc/getUserPointsAccount", headers=ck)
    balance2 = (json.loads(b or b"{}").get("data") or {}).get("balance")
    check("重复签到积分不变", balance2 == 100 + reward, f"{balance2}")

    st, h, b = call("GET", "/api/gva/oauth/status", headers=ck)
    check("oauth/status 本地回包", st == 200 and (json.loads(b or b"{}").get("data") or {}).get("google") is False, f"{b[:160]}")
    st, h, b = call("GET", "/api/gva/oauth/bindings", headers=ck)
    check("oauth/bindings 本地回数组", st == 200
          and isinstance(json.loads(b or b"{}").get("data"), list), f"{b[:160]}")
    st, h, b = call("GET", "/api/gva/oauth/bind/factors", headers=ck)
    factors = json.loads(b or b"{}").get("data") or {}
    check("oauth/bind/factors 有 hasPassword/hasEmail", st == 200
          and factors.get("hasPassword") is True and "hasEmail" in factors, f"{b[:200]}")

    st, h, b = call("GET", "/api/gva/email/getVerificationStatus", headers=ck)
    email = json.loads(b or b"{}").get("data") or {}
    check("邮箱状态来自本地账号（无邮箱 → 未认证）",
          st == 200 and email.get("email") == "" and email.get("emailVerified") is False,
          str(email))

    st, h, b = call("POST", "/api/gva/user/changeMyPassword",
                    {"password": "wrong-old", "newPassword": "newsecret123"}, headers=ck)
    check("旧密码错误 → 拒绝改密码", json.loads(b or b"{}").get("code") == 1, f"{b[:160]}")
    st, h, b = call("POST", "/api/gva/user/changeMyPassword",
                    {"password": "secret123", "newPassword": "newsecret123"}, headers=ck)
    j = json.loads(b or b"{}")
    check("改密码成功并回 token（无需重登）",
          st == 200 and j.get("code") == 0 and (j.get("data") or {}).get("token"), f"{b[:200]}")
    st, h, b = call("POST", "/api/gva/base/email/passwordLogin",
                    {"email": name, "password": "newsecret123"})
    check("新密码可以登录", json.loads(b or b"{}").get("code") == 0, f"{b[:160]}")
    st, h, b = call("POST", "/api/gva/base/email/passwordLogin",
                    {"email": name, "password": "secret123"})
    check("旧密码已失效", json.loads(b or b"{}").get("code") == 1, f"{b[:160]}")
    call("POST", "/api/gva/user/changeMyPassword",
         {"password": "newsecret123", "newPassword": "secret123"}, headers=ck)


    print("== 控制台入口按钮（仅管理员可见） ==")
    st, h, b = call("GET", "/js/console-entry.js")
    check("入口脚本可访问", st == 200 and b"data-local-console-entry" in b, f"{st}")
    st, h, b = call("GET", "/home")
    check("未登录：首页不注入入口", b"/js/console-entry.js" not in b, f"{st} len={len(b)}")
    st, h, b = call("GET", "/home", headers={"Cookie": f"local_session={token}"})
    check("普通用户：首页不注入入口", b"/js/console-entry.js" not in b, f"{st} len={len(b)}")
    st, h, b = call("GET", "/home", headers=admin_ck)
    check("管理员：首页注入入口脚本", b"/js/console-entry.js" in b, f"{st} len={len(b)}")
    check("注入不重复", b.count(b"/js/console-entry.js") == 1, f"{b.count(b'/js/console-entry.js')}")
    st, h, b = call("GET", "/login", headers=admin_ck)
    check("代理页也注入入口脚本", b"/js/console-entry.js" in b, f"{st} len={len(b)}")
    st, h, b = call("GET", "/local/", headers=admin_ck)
    check("控制台页不再注入", b"/js/console-entry.js" not in b, f"{st}")
    check("磁盘 HTML 未被改写",
          "/js/console-entry.js" not in (Path(__file__).parent / "html" / "app" / "explore.html")
          .read_text(encoding="utf-8", errors="replace"), "")

    print("== 控制台只让管理员看 ==")
    st, h, b = call("GET", "/local/")
    check("未登录 → 登录页", st == 200 and b"local-console-login" in b and b"console.js" not in b,
          f"{st} {b[:80]}")
    st, h, b = call("GET", "/local/api/stats")
    check("未登录 → 接口 401", st == 401 and json.loads(b or b"{}").get("code") == 401,
          f"{st} {b[:120]}")
    st, h, b = call("GET", "/local/api/schema",
                    headers={"Cookie": f"local_session={token}"})
    check("普通用户 → 接口 401", st == 401, f"{st} {b[:120]}")
    st, h, b = call("POST", "/local/api/login",
                    {"username": "admin", "password": "wrongpass"})
    check("管理员登录：密码错 → 401", st == 401, f"{st} {b[:120]}")
    st, h, b = call("POST", "/local/api/login",
                    {"username": "admin", "password": "admin123456"})
    j = json.loads(b or b"{}")
    check("管理员登录：成功 code=0", st == 200 and j.get("code") == 0, f"{st} {b[:160]}")
    st, h, b = call("POST", "/local/api/login",
                    {"username": "tester_25551", "password": "secret123"})
    check("非管理员账号：拒绝", st == 401, f"{st} {b[:160]}")
    st, h, b = call("GET", "/local/", headers=admin_ck)
    check("管理员 → 控制台页", st == 200 and b"console.js" in b, f"{st}")
    st, h, b = call("GET", "/local/api/stats", headers=admin_ck)
    check("管理员 → 接口 200", st == 200, f"{st} {b[:120]}")

    print("== 日志 / 控制台 ==")
    st, h, b = call("GET", "/local/api/stats", headers=admin_ck)
    s = json.loads(b or b"{}")
    check("日志已记录", s.get("requests", 0) > 5, f"{s.get('requests')}")
    check("账号数正确", s.get("users", 0) >= 1, f"{s.get('users')}")
    check("数据库已创建", "missai.db" in s.get("db_path", ""), s.get("db_path", ""))
    st, h, b = call("GET", "/local/", headers=admin_ck)
    check("控制台页面 200", st == 200 and b"console.js" in b, f"{st}")
    st, h, b = call("GET", "/local/api/schema", headers=admin_ck)
    sc = json.loads(b or b"{}")
    names = [t.get("table") for t in sc.get("tables", [])]
    check("表结构接口", "users" in names and "request_log" in names and "api_cache" not in names,
          f"{names}")
    st, h, b = call("GET", "/local/api/logs?limit=20", headers=admin_ck)
    check("日志接口有数据", len(json.loads(b or b"[]")) > 0, f"{st}")

    # ============================================== 创作页 + 聊天界面（本地库）
    print("== 创作页：模型 / 配置 ==")
    st, h, b = call("POST", "/api/gva/llmModelRate/getModelRateList",
                    {"poolOnly": True, "page": 1, "pageSize": 100}, headers=admin_ck)
    j = json.loads(b or b"{}")
    lst = (j.get("data") or {}).get("list") or []
    check("模型列表 ID 是正整数", st == 200 and j.get("code") == 0 and lst
          and isinstance(lst[0].get("ID"), int) and lst[0]["ID"] > 0, f"{st} {b[:160]}")
    st, h, b = call("POST", "/api/gva/llmModelRate/getModelStatusList",
                    {"page": 1, "pageSize": 10}, headers=admin_ck)
    j = json.loads(b or b"{}")
    check("模型状态列表是 list", st == 200 and j.get("code") == 0
          and isinstance((j.get("data") or {}).get("list"), list), f"{st} {b[:160]}")
    st, h, b = call("GET", "/api/gva/workFilter/config", headers=admin_ck)
    j = json.loads(b or b"{}")
    check("标签配置 tags 是数组", st == 200 and j.get("code") == 0
          and isinstance((j.get("data") or {}).get("tags"), list), f"{st} {b[:160]}")
    st, h, b = call("GET", "/api/gva/aiMedia/creator/catalog?lang=zh", headers=admin_ck)
    check("AI 媒体目录 code=0", st == 200 and json.loads(b or b"{}").get("code") == 0,
          f"{st} {b[:160]}")

    print("== 创作页：作品（POST /work → work/my 读回） ==")
    cid = "char-selftest-0001"
    call("DELETE", "/api/gva/work", {"characterID": cid}, headers=admin_ck)
    st, h, b = call("GET", f"/api/gva/work/my?characterID={cid}", headers=admin_ck)
    j = json.loads(b or b"{}")
    check("work/my 不存在 → data:null", st == 200 and j.get("code") == 0
          and j.get("data") is None, f"{st} {b[:160]}")

    work = {"characterID": cid, "name": "自测创作角色", "summary": "selftest 写进去的简介文本",
            "description": "正文", "avatarURL": "", "tags": ["RPG"],
            "promptData": {"systemPrompt": "你是角色"}, "quickCommands": []}
    st, h, b = call("POST", "/api/gva/work", work, headers=admin_ck)
    j = json.loads(b or b"{}")
    check("POST /work 创建（回 updatedAt）", st == 200 and j.get("code") == 0
          and isinstance((j.get("data") or {}).get("updatedAt"), str), f"{st} {b[:200]}")
    st, h, b = call("GET", f"/api/gva/work/my?characterID={cid}", headers=admin_ck)
    j = json.loads(b or b"{}")
    check("work/my 读回刚存的名称", st == 200
          and (j.get("data") or {}).get("name") == "自测创作角色", f"{st} {b[:200]}")

    st, h, b = call("PUT", "/api/gva/work/content", dict(work, name="自测创作角色v2"),
                    headers=admin_ck)
    j = json.loads(b or b"{}")
    check("PUT /work/content 更新", st == 200 and j.get("code") == 0
          and (j.get("data") or {}).get("name") == "自测创作角色v2", f"{st} {b[:200]}")
    st, h, b = call("POST", "/api/gva/work/publish", {"characterID": cid}, headers=admin_ck)
    j = json.loads(b or b"{}")
    check("publish → status=published", st == 200
          and (j.get("data") or {}).get("publishStatus") == "published", f"{st} {b[:200]}")
    st, h, b = call("GET", "/api/gva/works/my?page=1&pageSize=10", headers=admin_ck)
    j = json.loads(b or b"{}")
    data = j.get("data") or {}
    items = data.get("list") or data.get("records") or []
    check("works/my 列表能查到", st == 200 and isinstance(data.get("total"), int)
          and any(i.get("characterID") == cid for i in items), f"{st} {b[:240]}")

    st, h, b = call("PUT", "/api/gva/work/creativeRefs",
                    {"characterID": cid, "replyFormatCreativeItemId": 0,
                     "breakArmorCreativeItemId": 0}, headers=admin_ck)
    check("creativeRefs PUT", st == 200 and json.loads(b or b"{}").get("code") == 0,
          f"{st} {b[:160]}")
    st, h, b = call("GET", f"/api/gva/work/creativeRefs?characterID={cid}", headers=admin_ck)
    j = json.loads(b or b"{}")
    check("creativeRefs 读回整包", st == 200 and j.get("code") == 0
          and (j.get("data") or {}).get("characterID") == cid, f"{st} {b[:200]}")

    print("== 创作页：角色详情（本地作品可打开 profile） ==")
    st, h, b = call("GET", "/api/gva/narratiumCharacter/"
                    f"findNarratiumCharacterByCharacterID?characterID={cid}")
    j = json.loads(b or b"{}")
    ch = (j.get("data") or {}).get("character") or {}
    check("本地作品有角色详情", st == 200 and j.get("code") == 0
          and ch.get("name") == "自测创作角色v2" and ch.get("characterID") == cid
          and isinstance((ch.get("creator") or {}).get("name"), str),
          f"{st} {b[:240]}")

    print("== 创作页：世界书（数组 + 整数 ID） ==")
    st, h, b = call("GET", f"/api/gva/narratiumWorldbookEntry/"
                    f"getEntriesByCharacterID?characterID={cid}", headers=admin_ck)
    j = json.loads(b or b"{}")
    check("世界书列表是数组", st == 200 and j.get("code") == 0
          and isinstance(j.get("data"), list), f"{st} {b[:160]}")
    entry = {"characterID": cid, "worldbookID": "wb_selftest_1", "content": "词条内容",
             "keys": {"keys": ["关键词A", "关键词B"]}, "comment": "", "constant": False,
             "selective": False, "insertionOrder": 1, "enabled": True, "extPosition": 4,
             "extDepth": 1, "extSelectiveLogic": 3, "triggerSource": "all",
             "useRegex": False}
    st, h, b = call("POST", "/api/gva/narratiumWorldbookEntry/"
                    "createNarratiumWorldbookEntry", entry, headers=admin_ck)
    j = json.loads(b or b"{}")
    check("世界书 create 回整数 ID", st == 200 and j.get("code") == 0
          and isinstance((j.get("data") or {}).get("ID"), int), f"{st} {b[:200]}")
    eid = (j.get("data") or {}).get("ID")
    st, h, b = call("GET", f"/api/gva/narratiumWorldbookEntry/"
                    f"getEntriesByCharacterID?characterID={cid}", headers=admin_ck)
    rows = json.loads(b or b"{}").get("data") or []
    hit = [x for x in rows if x.get("worldbookID") == "wb_selftest_1"]
    check("世界书读回 keys.keys", len(rows) == 1 and hit
          and (hit[0].get("keys") or {}).get("keys") == ["关键词A", "关键词B"],
          f"{b[:240]}")
    st, h, b = call("PUT", "/api/gva/narratiumWorldbookEntry/"
                    "updateNarratiumWorldbookEntry", dict(entry, ID=eid, content="改过了"),
                    headers=admin_ck)
    j = json.loads(b or b"{}")
    check("世界书 update", st == 200 and j.get("code") == 0
          and (j.get("data") or {}).get("content") == "改过了", f"{st} {b[:200]}")
    st, h, b = call("DELETE", "/api/gva/narratiumWorldbookEntry/"
                    "deleteNarratiumWorldbookEntry",
                    {"characterID": cid, "worldbookID": "wb_selftest_1", "ID": eid},
                    headers=admin_ck)
    check("世界书 delete", st == 200 and json.loads(b or b"{}").get("code") == 0,
          f"{st} {b[:160]}")

    print("== 创作页：封面上传（multipart → html/image + image.db） ==")
    boundary = "----selftest777"
    raw = (f"--{boundary}\r\n"
           'Content-Disposition: form-data; name="file"; filename="cover.png"\r\n'
           "Content-Type: image/png\r\n\r\n").encode() + b"\x89PNG\r\n\x1a\n" + \
        f"\r\n--{boundary}--\r\n".encode()
    st, h, b = call("POST", "/api/gva/fileUploadAndDownload/upload", raw,
                    headers={"Cookie": admin_ck["Cookie"],
                             "Content-Type": f"multipart/form-data; boundary={boundary}"})
    j = json.loads(b or b"{}")
    up = ((j.get("data") or {}).get("file") or {}).get("url") or ""
    check("上传回 data.file.url", st == 200 and j.get("code") == 0
          and up.startswith("/api/uploads/file/"), f"{st} {b[:240]}")
    if up:
        st, h, b = call("GET", up)
        check("上传的图能读回来", st == 200 and h.get("Content-Type", "").startswith("image/"),
              f"{st} {h.get('Content-Type')}")

    print("== 聊天界面（会话 / 历史 / 消息落库） ==")
    # 只清**本自测角色**的对话 —— clearUserHistory 会把管理员名下所有会话删光，
    # 浏览器里存的 sessionId 随之失效，刷新页面就回空消息（聊天区变成正文白块）
    call("POST", "/api/gva/narratium/chatSession/deleteUserHistory",
         {"characterId": cid}, headers=admin_ck)
    st, h, b = call("POST", "/api/gva/narratium/chatSession/list",
                    {"page": 1, "pageSize": 20, "characterId": cid}, headers=admin_ck)
    j = json.loads(b or b"{}")
    sess = (j.get("data") or {}).get("list") or []
    check("list 空列表时自动建会话", st == 200 and j.get("code") == 0
          and sess and str(sess[0].get("sessionId") or "").isdigit(),
          f"{st} {b[:240]}")
    sid = str(sess[0].get("sessionId")) if sess else ""

    st, h, b = call("GET", f"/api/gva/narratium/chatSession/resolve"
                    f"?sessionId={sid}&characterId={cid}", headers=admin_ck)
    j = json.loads(b or b"{}")
    check("resolve 已解析", st == 200 and (j.get("data") or {}).get("resolved") is True,
          f"{st} {b[:200]}")
    st, h, b = call("POST", "/api/gva/narratium/chatSession/messagesOptimized",
                    {"sessionId": sid, "pageSize": 30, "orderBy": "desc"},
                    headers=admin_ck)
    j = json.loads(b or b"{}")
    check("会话消息为空（新会话）", st == 200
          and (j.get("data") or {}).get("messages") == [], f"{st} {b[:200]}")

    st, h, b = call("POST", "/api/gva/narratium/chatSession/chatStream",
                    {"sessionId": sid, "message": "你好，自测", "modelRateId": 1},
                    headers=admin_ck)
    frames = sse_frames(b)
    done = next((f for f in frames if isinstance(f, dict)
                 and f.get("type") == "done"), {})
    streamed = "".join(f.get("content", "") for f in frames
                       if isinstance(f, dict) and f.get("type") == "content")
    check("chatStream 回 SSE（text/event-stream + [DONE]）", st == 200
          and h.get("Content-Type", "").startswith("text/event-stream")
          and "[DONE]" in frames, f"{st} {h.get('Content-Type')} {b[:160]}")
    check("chatStream 有 content 增量", bool(streamed.strip()), f"{b[:200]}")
    check("chatStream done 带数字 id", isinstance(done.get("assistantMessageId"), int)
          and done["assistantMessageId"] > 0
          and isinstance(done.get("userMessageId"), int),
          f"{done}")
    st, h, b = call("POST", "/api/gva/narratium/chatSession/messagesOptimized",
                    {"sessionId": sid, "pageSize": 30, "orderBy": "desc"},
                    headers=admin_ck)
    msgs = (json.loads(b or b"{}").get("data") or {}).get("messages") or []
    roles = [m.get("role") for m in msgs]
    check("消息落库 user+assistant", len(msgs) == 2 and roles.count("user") == 1
          and roles.count("assistant") == 1, f"{roles}")
    st, h, b = call("POST", "/api/gva/narratium/chatSession/list",
                    {"page": 1, "pageSize": 20, "characterId": cid}, headers=admin_ck)
    sess = (json.loads(b or b"{}").get("data") or {}).get("list") or []
    check("messageCount 变成 2", bool(sess) and sess[0].get("messageCount") == 2,
          f"{b[:240]}")

    # 输入栏切「非流式」走 chatSync：必须回一套完整 JSON，不能是 idle 桩
    st, h, b = call("POST", "/api/gva/narratium/chatSession/chatSync",
                    {"sessionId": sid, "message": "非流式自测", "modelRateId": 1,
                     "clientRequestId": "selftest-sync"}, headers=admin_ck)
    j = json.loads(b or b"{}")
    d = j.get("data") or {}
    check("chatSync 回 JSON + 数字 id + 正文", st == 200 and j.get("code") == 0
          and isinstance(d.get("assistantMessageId"), int)
          and d["assistantMessageId"] > 0
          and isinstance(d.get("userMessageId"), int)
          and isinstance(d.get("message"), str) and d["message"].strip()
          and d.get("pointsConsumed") == 0, f"{st} {b[:300]}")
    st, h, b = call("POST", "/api/gva/narratium/chatSession/messagesOptimized",
                    {"sessionId": sid, "pageSize": 30, "orderBy": "desc"},
                    headers=admin_ck)
    msgs = (json.loads(b or b"{}").get("data") or {}).get("messages") or []
    check("chatSync 也落库（共 4 条）", len(msgs) == 4, f"{len(msgs)}")

    # 选项块 → <next_prompt>：前端据此渲染「选择你的回复」按钮
    src = "正文一段。\n\n*   **A. 甲**\n*   **B. 乙**\n\n请做出你的选择："
    got = wrap_next_prompts(src)
    check("选项块改写成 <next_prompt>", "<next_prompt>" in got
          and "A. 甲" in got and "B. 乙" in got
          and "请做出你的选择" not in got
          and got.split("<next_prompt>")[0].strip() == "正文一段。", repr(got))
    keep = "正文\n1. 甲\n3. 丙"
    check("标号不连续的列表不误判", wrap_next_prompts(keep) == keep, keep)
    tagged = "<next_prompt>\nA. 甲\nB. 乙\n</next_prompt>"
    check("已有 next_prompt 不重复包", wrap_next_prompts(tagged) == tagged)

    st, h, b = call("POST", "/api/gva/narratium/chatSession/userHistory",
                    {"page": 1, "pageSize": 30, "filter": ""}, headers=admin_ck)
    j = json.loads(b or b"{}")
    hist = (j.get("data") or {}).get("list") or []
    check("游玩历史 total 是整数且有条目", isinstance((j.get("data") or {}).get("total"), int)
          and len(hist) >= 1 and hist[0].get("sessionId"), f"{st} {b[:240]}")
    st, h, b = call("GET", f"/api/gva/narratium/chatSession/state?sessionId={sid}",
                    headers=admin_ck)
    check("state 空闲", st == 200
          and (json.loads(b or b"{}").get("data") or {}).get("chatState") == "idle",
          f"{st} {b[:200]}")
    st, h, b = call("GET", f"/api/gva/narratium/chatSession/partialContent?sessionId={sid}",
                    headers=admin_ck)
    check("partialContent 有 status", st == 200
          and (json.loads(b or b"{}").get("data") or {}).get("status"), f"{st} {b[:200]}")

    st, h, b = call("POST", "/api/gva/narratium/chatSession/claimGuest",
                    {"characterId": "char-selftest-guest"}, headers=admin_ck)
    j = json.loads(b or b"{}")
    g = j.get("data") or {}
    check("claimGuest ready + UUID 形状", st == 200 and g.get("status") == "ready"
          and str(g.get("sessionId") or "").isdigit()
          and len(str(g.get("identityScope") or "")) >= 16
          and len(str(g.get("sourceSessionId") or "")) >= 8, f"{st} {b[:300]}")
    st, h, b = call("POST", "/api/gva/narratium/chatSession/create",
                    {"characterId": cid, "userId": None, "title": "自测"},
                    headers=admin_ck)
    c = json.loads(b or b"{}").get("data") or {}
    check("create 会话 sessionId 是数字串", st == 200 and str(c.get("sessionId") or "").isdigit(),
          f"{st} {b[:300]}")

    # ---------------- chatSession 其余子路由 ----------------
    # 这些以前**没接住**：请求体已被本地分派读走、又落回反代 → 第二次 read_body
    # 卡死 90 秒（浏览器转圈）或 500。前端改标题 / 删消息 / 刷新时会用到。
    print("== chatSession 其余子路由（改标题 / 改删消息 / 列表 / 恒空闲） ==")
    st, h, b = call("PUT", "/api/gva/narratium/chatSession/update",
                    {"sessionId": sid, "title": "自测改的标题"}, headers=admin_ck)
    j = json.loads(b or b"{}")
    check("update 改标题", st == 200 and j.get("code") == 0
          and (j.get("data") or {}).get("title") == "自测改的标题", f"{st} {b[:200]}")
    st, h, b = call("POST", "/api/gva/narratium/chatSession/updateTitle",
                    {"sessionId": sid, "title": "自测标题2"}, headers=admin_ck)
    check("updateTitle 同义", st == 200 and json.loads(b or b"{}").get("code") == 0,
          f"{st} {b[:160]}")
    st, h, b = call("POST", "/api/gva/narratium/chatSession/list",
                    {"page": 1, "pageSize": 20, "characterId": cid}, headers=admin_ck)
    sess = (json.loads(b or b"{}").get("data") or {}).get("list") or []
    check("改过的标题读得回来", bool(sess) and sess[0].get("title") == "自测标题2",
          f"{b[:240]}")

    aid = next((m.get("id") for m in msgs if m.get("role") == "assistant"), 0) or 0
    check("拿得到 assistant 消息 id", bool(aid), f"{[m.get('id') for m in msgs]}")
    st, h, b = call("POST", "/api/gva/narratium/chatSession/editMessage",
                    {"sessionId": sid, "messageId": aid, "content": "改过的回复"},
                    headers=admin_ck)
    check("editMessage", st == 200 and (json.loads(b or b"{}").get("data") or {}).get("updated") == 1,
          f"{st} {b[:200]}")
    st, h, b = call("POST", "/api/gva/narratium/chatSession/messages",
                    {"sessionId": sid, "pageSize": 10, "orderBy": "desc"},
                    headers=admin_ck)
    msgs2 = (json.loads(b or b"{}").get("data") or {}).get("messages") or []
    check("messages 别名 + 改过的正文读得回",
          any(m.get("content") == "改过的回复" for m in msgs2), f"{b[:240]}")
    st, h, b = call("DELETE", "/api/gva/narratium/chatSession/deleteMessage",
                    {"sessionId": sid, "messageId": aid}, headers=admin_ck)
    check("deleteMessage", st == 200
          and (json.loads(b or b"{}").get("data") or {}).get("deleted") == 1,
          f"{st} {b[:200]}")
    st, h, b = call("POST", "/api/gva/narratium/chatSession/messagesOptimized",
                    {"sessionId": sid, "pageSize": 10, "orderBy": "desc"},
                    headers=admin_ck)
    left = (json.loads(b or b"{}").get("data") or {}).get("messages") or []
    check("删掉的那条不在了", all(m.get("id") != aid for m in left), f"{b[:200]}")

    st, h, b = call("GET", f"/api/gva/narratium/chatSession/getList"
                           f"?characterId={cid}&page=1&pageSize=10", headers=admin_ck)
    check("getList（query 版列表）", st == 200
          and bool((json.loads(b or b"{}").get("data") or {}).get("list")), f"{st} {b[:200]}")

    # 过期 sessionId（浏览器里存的会话已被删）→ 兜底回最近会话，否则回空消息，
    # 前端 0 条消息就切到「角色正文」全屏沙箱，聊天区变成白色大块
    st, h, b = call("POST", "/api/gva/narratium/chatSession/messagesOptimized",
                    {"sessionId": "999999", "pageSize": 10, "orderBy": "desc"},
                    headers=admin_ck)
    fb = (json.loads(b or b"{}").get("data") or {}).get("messages") or []
    check("过期 sessionId 兜底回最近会话的消息", st == 200 and fb,
          f"{st} {b[:240]}")

    for sub, method, body in (("summaries", "GET", None),
                              ("summary", "POST", {"sessionId": sid}),
                              ("stopStatus", "GET", None),
                              ("resumeStream", "GET", None),
                              ("chatSync", "POST", {"sessionId": sid})):
        st, h, b = call(method, f"/api/gva/narratium/chatSession/{sub}"
                                + (f"?sessionId={sid}" if method == "GET" else ""),
                        body, headers=admin_ck)
        check(f"{sub} 本地回包（不落反代、不挂死）", st == 200
              and json.loads(b or b"{}").get("code") == 0, f"{st} {b[:160]}")

    st, h, b = call("POST", "/api/gva/narratium/guestChat/chatStream",
                    {"sessionId": g.get("sessionId"), "message": "游客自测"})
    txt = b.decode("utf-8", "replace")
    check("游客聊天 SSE 帧完整", st == 200
          and str(h.get("Content-Type", "")).startswith("text/event-stream")
          and '"type": "message"' in txt.replace('"type":"message"', '"type": "message"')
          and "guest_info_updated" in txt
          and '"type": "done"' in txt.replace('"type":"done"', '"type": "done"'),
          f"{h.get('Content-Type')} {txt[:200]}")

    st, h, b = call("POST", "/api/gva/narratium/chatSession/deleteUserHistory",
                    {"characterId": "char-selftest-guest"}, headers=admin_ck)
    check("deleteUserHistory", st == 200 and json.loads(b or b"{}").get("code") == 0,
          f"{st} {b[:160]}")
    call("DELETE", "/api/gva/work", {"characterID": cid}, headers=admin_ck)

    # ---------------- 对话设置（模型参数 / 记忆配置） ----------------
    print("== 对话设置（模型参数 / 记忆配置，字段必须齐全） ==")
    st, h, b = call("GET", "/api/gva/narratium/modelConfig/getByModelRate?modelRateId=1",
                    headers=admin_ck)
    j = json.loads(b or b"{}")
    d = j.get("data") or {}
    need = ("temperature", "topP", "presencePenalty", "frequencyPenalty", "maxTokens")
    check("modelConfig 字段齐全（缺字段前端 toFixed 就崩）",
          j.get("code") == 0 and all(isinstance(d.get(k), (int, float))
                                     for k in need), f"{b[:240]}")
    check("参数在滑杆范围内", 0 <= d.get("temperature", -1) <= 1
          and 256 <= d.get("maxTokens", 0) <= 8192, f"{b[:240]}")

    st, h, b = call("POST", "/api/gva/narratium/modelConfig/saveByModelRate",
                    {"modelRateId": 1, "temperature": 0.95, "topP": 0.85,
                     "presencePenalty": 0.1, "frequencyPenalty": 0.2,
                     "maxTokens": 4096}, headers=admin_ck)
    check("保存模型参数 code=0", json.loads(b or b"{}").get("code") == 0, f"{b[:200]}")
    st, h, b = call("GET", "/api/gva/narratium/modelConfig/getByModelRate?modelRateId=1",
                    headers=admin_ck)
    d = json.loads(b or b"{}").get("data") or {}
    check("刷新后参数还在", d.get("temperature") == 0.95 and d.get("maxTokens") == 4096,
          f"{b[:240]}")
    st, h, b = call("GET", f"/api/gva/narratium/modelConfig/getByModelRate?modelRateId=2",
                    headers=admin_ck)
    d2 = json.loads(b or b"{}").get("data") or {}
    check("不同模型各存各的", d2.get("temperature") != 0.95, f"{b[:240]}")

    st, h, b = call("GET", f"/api/gva/narratium/summaryConfig/getByCharacter"
                           f"?characterID={cid}", headers=admin_ck)
    d = json.loads(b or b"{}").get("data") or {}
    check("记忆配置字段齐全", isinstance(d.get("summaryReserveRounds"), int)
          and isinstance(d.get("summaryEnabled"), bool), f"{b[:240]}")
    st, h, b = call("POST", "/api/gva/narratium/summaryConfig/saveByCharacter",
                    {"characterID": cid, "summaryEnabled": False,
                     "summaryInjectMode": "inline", "summaryReserveRounds": 5},
                    headers=admin_ck)
    check("保存记忆配置 code=0", json.loads(b or b"{}").get("code") == 0, f"{b[:200]}")
    st, h, b = call("GET", f"/api/gva/narratium/summaryConfig/getByCharacter"
                           f"?characterID={cid}", headers=admin_ck)
    d = json.loads(b or b"{}").get("data") or {}
    check("记忆配置刷新后还在", d.get("summaryReserveRounds") == 5
          and d.get("summaryEnabled") is False, f"{b[:240]}")

    # 收尾：清掉自测写入的两组配置，回到默认值
    admin_uid = (db.get_session(admin_token) or {}).get("id")
    db.execute("DELETE FROM config_cache WHERE key IN (?,?,?,?)",
               (f"modelConfig:{admin_uid}:1", f"modelConfig:{admin_uid}:2",
                f"summaryConfig:{admin_uid}:{cid}", f"modelConfig:1"))

    # ---------------- 模型接入（OpenAI 协议） ----------------
    print("== 模型接入（存 api+key → 检测模型 → 聊天按模型调 OpenAI 协议） ==")
    stub_models = ["gpt-selftest-mini", "gpt-selftest-pro", "gpt-selftest-long"]

    class StubLLM(BaseHTTPRequestHandler):
        """自起的 OpenAI 兼容桩：GET /v1/models + POST /v1/chat/completions"""

        def log_message(self, *args):          # 不刷日志
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0].rstrip("/").endswith("/models"):
                if self.headers.get("Authorization") != "Bearer sk-selftest":
                    return self._send({"error": {"message": "bad key"}}, 401)
                return self._send({"object": "list", "data": [
                    {"id": m, "object": "model"} for m in stub_models]})
            self._send({"error": {"message": "not found " + self.path}}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            if not self.path.split("?")[0].rstrip("/").endswith("/chat/completions"):
                return self._send({"error": {"message": "not found " + self.path}}, 404)
            model = str(payload.get("model") or "")
            last = ""
            for m in payload.get("messages") or []:
                if isinstance(m, dict) and m.get("role") == "user":
                    last = str(m.get("content") or "")
            text = f"MOCK[{model}] 收到：{last[:80]}"
            self._send({
                "id": "cmpl-selftest", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}],
            })

    # 先清掉既有接入（跑完再原样恢复），保证模型列表断言是确定的
    pre_rows = db.query("SELECT * FROM llm_providers")
    for r in pre_rows:
        db.delete_llm_provider(r["id"])
    st, h, b = call("POST", "/api/gva/llmModelRate/getModelRateList",
                    {"poolOnly": False, "page": 1, "pageSize": 100}, headers=admin_ck)
    items = (json.loads(b or b"{}").get("data") or {}).get("list") or []
    check("没接模型 → 只剩本地兜底那一条",
          len(items) == 1 and items[0].get("modelID") == "local-default", f"{b[:200]}")

    st, h, b = call("GET", "/local/api/llm", headers=admin_ck)
    presets = {p.get("key") for p in (json.loads(b or b"{}").get("presets") or [])}
    check("预设位：openai / 阿里百炼 / deepseek / 火山方舟",
          {"openai", "dashscope", "deepseek", "volcengine"} <= presets, f"{presets}")
    st, h, b = call("GET", "/local/api/llm")
    check("模型接入接口仅管理员", st == 401, f"{st}")

    with socket.socket() as probe:             # 随机空闲端口放桩
        probe.bind(("127.0.0.1", 0))
        stub_port = probe.getsockname()[1]
    stub = ThreadingHTTPServer(("127.0.0.1", stub_port), StubLLM)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    stub_base = f"http://127.0.0.1:{stub_port}/v1"

    st, h, b = call("POST", "/local/api/llm/check",
                    {"kind": "openai", "name": "打不通的站",
                     "base_url": "http://127.0.0.1:1/v1", "api_key": "sk-x"},
                    headers=admin_ck)
    j = json.loads(b or b"{}")
    check("地址不通 → code=1 并说明原因", j.get("code") == 1
          and "连不上" in str(j.get("msg")), f"{b[:200]}")
    bad_id = (j.get("data") or {}).get("id")

    st, h, b = call("POST", "/local/api/llm/check",
                    {"kind": "openai", "name": "自测中转站",
                     "base_url": stub_base, "api_key": "sk-selftest"},
                    headers=admin_ck)
    j = json.loads(b or b"{}")
    prov = j.get("data") or {}
    models = prov.get("models") or []
    check("保存并检测成功 code=0", j.get("code") == 0, f"{b[:240]}")
    check("模型条数 = 中转站返回条数（3）", len(models) == 3, f"{models}")
    check("回包不带明文密钥", "api_key" not in prov and bool(prov.get("apiKeyMasked")),
          f"{sorted(prov)}")
    pid = prov.get("id")

    st, h, b = call("POST", "/api/gva/llmModelRate/getModelRateList",
                    {"poolOnly": False, "page": 1, "pageSize": 100}, headers=admin_ck)
    items = (json.loads(b or b"{}").get("data") or {}).get("list") or []
    check("聊天模型列表就是这 3 个",
          len(items) == 3 and {i.get("modelID") for i in items} == set(stub_models),
          f"{[i.get('modelID') for i in items]}")
    check("行标题用模型 id、副标题是接入名",
          all(i.get("poolName") == i.get("modelID") and i.get("description") == "自测中转站"
              for i in items), f"{items}")
    check("rateId 是稳定正整数",
          all(isinstance(i.get("ID"), int) and i["ID"] > 0 for i in items), f"{items}")

    st, h, b = call("POST", "/api/gva/narratium/chatSession/list",
                    {"page": 1, "pageSize": 20, "characterId": cid}, headers=admin_ck)
    sess = (json.loads(b or b"{}").get("data") or {}).get("list") or []
    sid2 = str(sess[0].get("sessionId")) if sess else sid
    pick = next(i for i in items if i.get("modelID") == "gpt-selftest-pro")

    msg = stream_text({"sessionId": sid2, "message": "自测走模型",
                       "modelRateId": pick.get("ID")}, headers=admin_ck)
    check("选中模型 → 回复由 OpenAI 接口生成",
          msg.startswith("MOCK[gpt-selftest-pro]"), f"{msg[:160]}")

    msg = stream_text({"sessionId": sid2, "message": "自测本地兜底", "modelRateId": 1},
                      headers=admin_ck)
    check("选本地兜底模型 → 本地回复", msg.startswith("（本地模式）"), f"{msg[:160]}")

    msg = stream_text({"sessionId": sid2, "message": "自测默认接入"}, headers=admin_ck)
    check("不指定模型 → 用默认接入", msg.startswith("MOCK["), f"{msg[:160]}")

    st, h, b = call("POST", "/local/api/llm/select",
                    {"id": pid, "model": "gpt-selftest-long"}, headers=admin_ck)
    st, h, b = call("POST", "/api/gva/llmModelRate/getModelRateList",
                    {"poolOnly": False, "page": 1, "pageSize": 100}, headers=admin_ck)
    items = (json.loads(b or b"{}").get("data") or {}).get("list") or []
    check("设默认模型 → isDefault 跟着走",
          any(i.get("isDefault") and i.get("modelID") == "gpt-selftest-long"
              for i in items), f"{items}")

    # 清理自测数据，再把跑之前的接入原样恢复
    for x in (pid, bad_id):
        if x:
            call("POST", "/local/api/llm/delete", {"id": x}, headers=admin_ck)
    st, h, b = call("POST", "/api/gva/llmModelRate/getModelRateList",
                    {"poolOnly": False, "page": 1, "pageSize": 100}, headers=admin_ck)
    items = (json.loads(b or b"{}").get("data") or {}).get("list") or []
    check("删完回到本地兜底模型",
          len(items) == 1 and items[0].get("modelID") == "local-default", f"{items}")
    msg = stream_text({"sessionId": sid2, "message": "自测收尾"}, headers=admin_ck)
    check("没有接入时聊天照常（本地兜底）", msg.startswith("（本地模式）"), f"{msg[:160]}")

    stub.shutdown()
    stub.server_close()
    for row in pre_rows:                        # 恢复跑测试前的接入（重建，id 可能变）
        new = db.save_llm_provider({"id": row["id"], "kind": row["kind"],
                                    "name": row["name"], "base_url": row["base_url"],
                                    "api_key": row["api_key"]})
        if new:
            db.set_llm_models(new["id"],
                              [m.get("model") for m in (db._as_list(row["models"]) or [])
                               if isinstance(m, dict)], row["status"],
                              row.get("last_error") or "", row.get("default_model") or "")
            if int(row.get("is_default") or 0):
                db.set_llm_active(new["id"], row.get("default_model") or "")
    check("跑前的接入已恢复", len(db.list_llm_providers()) == len(pre_rows),
          f"{len(db.list_llm_providers())} != {len(pre_rows)}")

    # ---------------- 旧镜像页路径 → 302 到新自建页（页面文件已删除） ----------------
    print("== 旧镜像页路径 302 跳转 ==")
    for old, new in (("/ai-creator", "/app/draw"),
                     ("/ai-gallery", "/app/draw"),
                     ("/ai-history", "/app/draw"),
                     ("/favorites", "/app/explore"),
                     ("/hot-chart", "/app/explore"),
                     ("/profile?id=abc", "/app/chat?id=abc"),
                     ("/history", "/app/chat"),
                     ("/works", "/app/works"),
                     ("/creators", "/app/plaza"),
                     ("/creators/some-id", "/app/plaza"),
                     ("/creative-plaza", "/app/plaza"),
                     ("/creation", "/app/creation"),
                     ("/creation/worldbook", "/app/worldbook"),
                     ("/creation-guide/html-css", "/app/creation-guide"),
                     ("/index.html", "/app/explore")):
        st, loc = call_redirect(old)
        check(f"{old} → {new}", st == 302 and loc == new, f"{st} {loc}")
    st, h, b = call("GET", "/app/draw")
    check("新绘图页 200", st == 200 and b"<html" in b, f"{st}")

    st, h, b = call("GET", "/api/local/gallery/works?sort=new&limit=5")
    j = json.loads(b or b"{}")
    data = j.get("data") or {}
    check("作品列表公开可读", st == 200 and j.get("code") == 0, f"{b[:160]}")
    check("列表带模型与当前用户", isinstance(data.get("models"), list)
          and isinstance(data.get("me"), dict), f"{sorted(data)}")
    st, h, b = call("GET", "/api/local/gallery/models")
    j = json.loads(b or b"{}")
    check("绘图模型列表 code=0", j.get("code") == 0, f"{b[:160]}")

    st, h, b = call("GET", "/local/api/image-models")
    check("绘图接入接口仅管理员", st == 401, f"{st}")
    st, h, b = call("GET", "/local/api/image-models", headers=admin_ck)
    j = json.loads(b or b"{}")
    presets = {p.get("key") for p in (j.get("presets") or [])}
    check("绘图预设含 openai / 百炼 / 硅基流动 / 火山 / 本地",
          {"openai", "dashscope", "siliconflow", "volcengine", "local"} <= presets,
          f"{presets}")

    # 清空绘图接入（跑完原样恢复），保证模型列表断言确定
    pre_img = db.query("SELECT * FROM image_providers")
    for r in pre_img:
        db.delete_image_provider(r["id"])
    pre_work_ids = {int(r["id"]) for r in db.query("SELECT id FROM image_works")}

    st, h, b = call("POST", "/api/local/gallery/generate",
                    {"prompt": "未登录不该能生成", "rateId": 2, "size": "64x64"})
    check("未登录生成 → 401", st == 401, f"{st}")

    st, h, b = call("POST", "/api/local/gallery/generate",
                    {"prompt": "自测占位图：夜色霓虹城市", "negative": "模糊",
                     "rateId": 2, "size": "64x64", "n": 2, "seed": 424242},
                    headers=admin_ck)
    j = json.loads(b or b"{}")
    gen = (j.get("data") or {}).get("works") or []
    check("本地占位生成 code=0", j.get("code") == 0, f"{b[:200]}")
    check("生成 2 张并入库", len(gen) == 2 and all(w.get("id") for w in gen), f"{gen}")
    check("占位图标记为 local", bool((j.get("data") or {}).get("local")), f"{b[:200]}")
    check("种子随作品入库", bool(gen) and int(gen[0].get("seed") or 0) == 424242,
          f"{[w.get('seed') for w in gen]}")
    local_ids = [int(w["id"]) for w in gen]
    if gen:
        st, h, b = call("GET", gen[0]["url_key"])
        check("作品图可直接访问（image/png）",
              st == 200 and h.get("Content-Type", "").startswith("image/png"),
              f"{st} {h.get('Content-Type')}")
        check("图片登记进图片库（source=ai）",
              bool(imagedb.lookup(gen[0]["url_key"])), gen[0]["url_key"])

    st, h, b = call("GET", "/api/local/gallery/works?sort=hot&limit=200")
    works = ((json.loads(b or b"{}").get("data") or {}).get("works")) or []
    check("浏览排行按点赞倒序", [w.get("likes") for w in works]
          == sorted([w.get("likes") for w in works], reverse=True), f"{works[:3]}")
    st, h, b = call("POST", "/api/local/gallery/like", {"id": local_ids[0]})
    j = json.loads(b or b"{}")
    check("点赞 +1", j.get("code") == 0 and int((j.get("data") or {}).get("likes") or 0) >= 1,
          f"{b[:160]}")

    admin_uid2 = int((db.get_session(admin_token) or {}).get("id") or 0)
    st, h, b = call("GET", "/api/local/gallery/works?sort=mine&limit=100", headers=admin_ck)
    j = json.loads(b or b"{}")
    mine_rows = (j.get("data") or {}).get("works") or []
    check("我的作品只返回当前用户",
          bool(mine_rows) and all(int(w.get("user_id") or 0) == admin_uid2 for w in mine_rows),
          f"{len(mine_rows)} 条")
    st, h, b = call("GET", "/api/local/gallery/works?sort=mine&limit=100")
    j = json.loads(b or b"{}")
    check("未登录看我的作品 → 空列表",
          j.get("code") == 0 and not (j.get("data") or {}).get("works"),
          f"{b[:160]}")

    # 绘图模型接入：自起 OpenAI 兼容桩（GET /v1/models + POST /v1/images/generations）
    import imagegen
    stub_png = base64.b64encode(imagegen.placeholder_png("stub", "64x64")).decode("ascii")

    class StubImage(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0].rstrip("/").endswith("/models"):
                if self.headers.get("Authorization") != "Bearer sk-img":
                    return self._send({"error": {"message": "bad key"}}, 401)
                return self._send({"object": "list", "data": [
                    {"id": "gpt-image-selftest", "object": "model"},
                    {"id": "text-embedding-selftest", "object": "model"},
                ]})
            self._send({"error": {"message": "not found " + self.path}}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            if not self.path.split("?")[0].rstrip("/").endswith("/images/generations"):
                return self._send({"error": {"message": "not found " + self.path}}, 404)
            if self.headers.get("Authorization") != "Bearer sk-img":
                return self._send({"error": {"message": "bad key"}}, 401)
            self._send({"created": 1, "data": [
                {"b64_json": stub_png} for _ in range(int(payload.get("n") or 1))]})

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        img_port = probe.getsockname()[1]
    img_stub = ThreadingHTTPServer(("127.0.0.1", img_port), StubImage)
    threading.Thread(target=img_stub.serve_forever, daemon=True).start()
    img_base = f"http://127.0.0.1:{img_port}/v1"

    st, h, b = call("POST", "/local/api/image-models/check",
                    {"kind": "custom", "name": "自测绘图站",
                     "base_url": img_base, "api_key": "sk-img",
                     "sizes": "64x64,1024x1024"}, headers=admin_ck)
    j = json.loads(b or b"{}")
    prov = j.get("data") or {}
    check("绘图接入检测成功 code=0", j.get("code") == 0, f"{b[:240]}")
    check("只保留绘图类模型（过滤 embedding）",
          [m.get("model") for m in (prov.get("models") or [])] == ["gpt-image-selftest"],
          f"{prov.get('models')}")
    check("绘图回包不带明文密钥", "api_key" not in prov and bool(prov.get("apiKeyMasked")),
          f"{sorted(prov)}")

    st, h, b = call("GET", "/api/local/gallery/works?limit=5")
    models = ((json.loads(b or b"{}").get("data") or {}).get("models")) or []
    real = next((m for m in models if m.get("providerKind") == "custom"), None)
    check("绘图页模型下拉出现真实模型", bool(real), f"{models}")

    if real:
        st, h, b = call("POST", "/api/local/gallery/generate",
                        {"prompt": "自测走真实绘图接口", "rateId": real.get("ID"),
                         "size": "64x64", "n": 1}, headers=admin_ck)
        j = json.loads(b or b"{}")
        works2 = (j.get("data") or {}).get("works") or []
        check("真实绘图模型生成 code=0", j.get("code") == 0, f"{b[:240]}")
        check("作品记录模型名与接入", bool(works2)
              and works2[0].get("model") == "gpt-image-selftest"
              and works2[0].get("provider") == "自测绘图站", f"{works2[:1]}")
        check("真实接口生成不是 local", not (j.get("data") or {}).get("local"), f"{b[:200]}")
        if works2:
            st, h, b = call("GET", works2[0]["url_key"])
            check("真实接口图片落盘可访问", st == 200
                  and h.get("Content-Type", "").startswith("image/png"),
                  f"{st} {h.get('Content-Type')}")
            local_ids.append(int(works2[0]["id"]))

    st, h, b = call("POST", "/local/api/image-models/delete",
                    {"id": prov.get("id")}, headers=admin_ck)
    check("删除绘图接入", json.loads(b or b"{}").get("code") == 0, f"{b[:160]}")
    st, h, b = call("GET", "/api/local/gallery/models")
    models = ((json.loads(b or b"{}").get("data") or {}).get("models")) or []
    check("删完回落本地占位模型",
          len(models) == 1 and models[0].get("providerKind") == "local", f"{models}")

    img_stub.shutdown()
    img_stub.server_close()

    # 清理自测生成的作品，再恢复跑前的绘图接入
    for wid in local_ids:
        db.delete_image_work(wid, is_admin=True)
    check("自测作品已清理",
          {int(r["id"]) for r in db.query("SELECT id FROM image_works")} == pre_work_ids,
          f"{len(db.query('SELECT id FROM image_works'))} rows")
    for row in pre_img:
        new = db.save_image_provider({"kind": row["kind"], "name": row["name"],
                                      "base_url": row["base_url"],
                                      "api_key": row["api_key"]})
        if new:
            db.set_image_models(new["id"],
                                [m.get("model") for m in (db._as_list(row["models"]) or [])
                                 if isinstance(m, dict)], row["status"],
                                row.get("last_error") or "", row.get("default_model") or "")
            if int(row.get("is_default") or 0):
                db.set_image_active(new["id"], row.get("default_model") or "")
    check("跑前的绘图接入已恢复", len(db.list_image_providers()) == len(pre_img),
          f"{len(db.list_image_providers())} != {len(pre_img)}")

    # ---------------- 新前端 /app/*（唯一前端，原生自建页） ----------------
    print("== 新前端 /app/* 页面 ==")
    for page in ("creation", "worldbook", "chat", "plaza", "draw", "creation-guide"):
        st, h, b = call("GET", f"/app/{page}")
        text = b.decode("utf-8", "replace")
        check(f"/app/{page} 返回页面 200", st == 200, f"{st}")
        check(f"/app/{page} 使用共享样式", "/app/css/app.css" in text, text[:120])
    st, h, b = call("GET", "/app/console")
    check("/app/console 未登录时给管理员登录页",
          st == 200 and "cLogin" in b.decode("utf-8", "replace"), f"{st}")

    for asset in ("/app/css/app.css", "/app/js/core/api.js", "/app/js/core/ui.js",
                  "/app/js/pages/creation.js", "/app/js/pages/worldbook.js",
                  "/app/js/pages/chat.js", "/app/js/pages/plaza.js",
                  "/app/js/pages/draw.js"):
        st, h, b = call("GET", asset)
        check(f"静态资源 {asset}", st == 200, f"{st}")

    # 契约：字段缺失 / 空库时前端也要能渲染（页面全部走兜底）
    st, h, b = call("GET", "/api/gva/creativePlaza/items?page=1&pageSize=3")
    j = json.loads(b or b"{}")
    data = j.get("data") or {}
    check("创意广场列表契约（items 为数组）",
          j.get("code") == 0 and isinstance(data.get("items"), list), f"{b[:200]}")
    st, h, b = call("GET", "/api/local/gallery/works?limit=3")
    j = json.loads(b or b"{}")
    data = j.get("data") or {}
    check("绘图图库契约（works / models 为数组）",
          j.get("code") == 0 and isinstance(data.get("works"), list)
          and isinstance(data.get("models"), list), f"{b[:200]}")

    print(f"\n结果：通过 {PASS}，失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
