#!/usr/bin/env python3
"""
mirror.py — 抓取 https://missai01.com 首页到本地（构建工具，只需运行一次）

输出：
    html/          index.html（原样页面）+ assets/（图片、字体、图标）+ app/js、app/css

用法： python mirror.py
"""

import gzip
import json
import re
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

ORIGIN = "https://missai01.com"
ROOT = Path(__file__).resolve().parent.parent   # 项目根（本脚本在 tools/ 下）
HTML_DIR = ROOT / "html"
JS_DIR = HTML_DIR / "app" / "js"
CSS_DIR = HTML_DIR / "app" / "css"
ASSET_DIR = HTML_DIR / "assets"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CTX = ssl.create_default_context()

# 首屏加载完成后才拉取的 chunk（浏览器抓包得到，HTML 里没有）
RUNTIME_CHUNKS = [
    "/_next/static/chunks/5797.00c26fafb34bb236.js",
    "/_next/static/chunks/7783-0abb7329dc79102a.js",
    "/_next/static/chunks/7805-f555a868612b70bb.js",
    "/_next/static/chunks/1068-f86a46ba89658690.js",
    "/_next/static/chunks/9570.34d608265665711c.js",
    "/_next/static/chunks/9436.7e31e21761d5c41f.js",
    "/_next/static/chunks/4930.c9cdfb2be1fd1440.js",
    "/_next/static/chunks/4190.61e0a77420fefd37.js",
    "/_next/static/chunks/2554-40cc10f0802d1401.js",
    "/_next/static/chunks/8442.a4fe680eb358d86b.js",
    "/_next/static/chunks/2516.ab5a66625c5deb67.js",
    "/_next/static/chunks/6129.0921c8bda02251ca.js",
]


def fetch(url: str, referer: str | None = None) -> bytes:
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60, context=CTX) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        return data


def url_to_local(url: str) -> Path | None:
    """远端 URL → 本地文件路径；返回 None 表示不落地（由后端代理）"""
    clean = url.split("?")[0].split("#")[0]
    rel = clean[len(ORIGIN):] if clean.startswith(ORIGIN) else clean

    m = re.match(r"^/_next/static/chunks/(.+)$", rel)
    if m:
        return JS_DIR / m.group(1)
    m = re.match(r"^/_next/static/css/(.+)$", rel)
    if m:
        return CSS_DIR / m.group(1)
    m = re.match(r"^/fonts/(.+)$", rel)
    if m:
        return ASSET_DIR / "fonts" / m.group(1)
    if re.match(r"^/(res\.js|legacy-polyfills\.js)$", rel):
        return JS_DIR / rel[1:]
    if re.match(r"^/[^/]+\.(webp|png|svg|ico|gif|jpg|jpeg|txt|webmanifest)$", rel, re.I):
        return ASSET_DIR / rel[1:]
    return None


def collect_urls(html: str) -> set[str]:
    urls: set[str] = set()
    for pattern in (r'<script[^>]+src="([^"]+)"',
                    r'<link[^>]+href="([^"]+)"',
                    r'<img[^>]+src="([^"]+)"'):
        for m in re.finditer(pattern, html):
            urls.add(urljoin(ORIGIN, m.group(1)))
    for chunk in RUNTIME_CHUNKS:
        urls.add(ORIGIN + chunk)
    return urls


def download(url: str) -> tuple[str, int | None, str | None]:
    target = url_to_local(url)
    if target is None:
        return (url, None, "skip")
    if target.exists() and target.stat().st_size > 0:
        return (url, None, "cached")
    try:
        data = fetch(url, referer=ORIGIN + "/home")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return (url, len(data), None)
    except Exception as exc:  # noqa: BLE001
        return (url, None, str(exc))


def main() -> None:
    for d in (HTML_DIR, JS_DIR, CSS_DIR, ASSET_DIR):
        d.mkdir(parents=True, exist_ok=True)

    print("1/3 抓取首页 HTML ...")
    html = fetch(ORIGIN + "/home").decode("utf-8", "replace")
    print(f"      {len(html)} 字符")

    print("2/3 收集资源 ...")
    urls = sorted(u for u in collect_urls(html) if url_to_local(u))
    print(f"      {len(urls)} 个待下载资源")

    print("3/3 下载 ...")
    ok = fail = bytes_total = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for url, size, err in pool.map(download, urls):
            if err and err not in ("skip", "cached"):
                fail += 1
                print(f"      x {err}  ({url})")
            else:
                ok += 1
                bytes_total += size or 0
    print(f"      成功 {ok}，失败 {fail}，新增 {bytes_total / 1024:.1f} KB")

    # 页面原样保存：改写 flight 数据里的资源清单会破坏 React 水合，
    # 所以 /_next/*、/fonts/* 等引用统一由 server.py 做路径映射。
    (HTML_DIR / "index.html").write_text(html, encoding="utf-8")
    print("写出 html/index.html（原样页面）")

    print("\n完成。目录：")
    print("  html/   index.html + assets/(图片、字体)")
    print("  app/js/ 全部脚本")
    print("  app/css/ 全部样式")
    print("\n下一步： python server.py  然后访问 http://localhost:4173/home")


# =====================================================================
# 运行时：反代镜像引擎（独立脚本，server.py 用子进程拉起；不经过浏览器）
#   · 按数据面（works/creative/drawing）分页抓原站接口
#   · 每条回包交给 db.ingest_content 拆进内容库（works/creative/content_api）
#   · 进度写入 MIRROR_STATE 并同步到 config_cache 的 mirror_state，
#     控制台进度条读库（server.py 不 import 本模块）
# =====================================================================
import threading  # noqa: E402
import time  # noqa: E402

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import db  # noqa: E402  (db.py 在项目根下)

MIRROR_LOCK = threading.Lock()
MIRROR_STATE = {
    "running": False, "surface": "", "phase": "idle", "message": "",
    "done": 0, "total": 0, "items": 0, "errors": 0,
    "started_at": "", "finished_at": "",
}

# 每个数据面的接口模板；含 "{page}" 的会分页翻抓，最多累计 limit 条
SURFACE_ENDPOINTS = {
    "works": [  # 探索页：新作榜 / 热门榜 / 推荐 / 展示 / 内容偏好
        "/api/gva/narratium/ranking/new?rankType=daily&genderPreference=all&page={page}&pageSize=30",
        "/api/gva/narratium/ranking/new?rankType=monthly&genderPreference=all&page={page}&pageSize=30",
        "/api/gva/narratium/ranking/hot?page={page}&pageSize=30&gender=",
        "/api/gva/narratium/discovery/hot_chart?limit=30",
        "/api/gva/narratium/discovery/home_personalized?limit=9",
        "/api/gva/narratium/showcase/list",
        "/api/gva/narratium/personalizedRecommendation/list?page={page}&pageSize=9",
        "/api/gva/narratium/content-preferences",
    ],
    "creative": [  # 创意广场：目录 / 标签 / 商品列表
        "/api/gva/creativePlaza/catalog",
        "/api/gva/creativePlaza/tags",
        "/api/gva/creativePlaza/items?page={page}&pageSize=24&sortBy=latest",
    ],
    "drawing": [  # 绘图广场：图库（按浏览 / 最新）+ 创作配方目录
        "/api/gva/aiImage/gallery?page={page}&pageSize=24&sort=views",
        "/api/gva/aiImage/gallery?page={page}&pageSize=24&sort=latest",
        "/api/gva/aiMedia/creator/catalog?lang=zh",
    ],
    "creators": [  # 大神作者榜单：热度 / 收藏 / 新锐
        "/api/gva/narratium/ranking/author/hot?page={page}&pageSize=30",
        "/api/gva/narratium/ranking/author/follower?page={page}&pageSize=30",
        "/api/gva/narratium/ranking/author/new?page={page}&pageSize=30",
    ],
}


def upstream_json(path: str, timeout: int = 45, token: str = ""):
    """服务端直连线上抓 JSON（反代传输；不经浏览器）。
    token: 可选的登录态（世界书等需要认证的接口）。
           原站用 x-token 请求头携带登录态，不再用 Cookie。"""
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": ORIGIN + "/",
    }
    if token:
        headers["x-token"] = token
    req = urllib.request.Request(ORIGIN + path, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _post_json(path: str, payload: dict, timeout: int = 30) -> dict:
    """原站接口一律收 JSON 请求体（表单编码会让 Go 后端报
    invalid character 'u' looking for beginning of value）。"""
    req = urllib.request.Request(
        ORIGIN + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": ORIGIN + "/login",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def upstream_login(username: str, password: str) -> str:
    """登录源站获取 x-token。返回 token 字符串，失败返回空。

    原站登录链：captchaV3 出题 → captchaV3/verify 校验 → base/login。
    验证码有 slide（滑块）与 digit（算术题）两种模式，slide 用一次
    字符串坐标即可通过，失败 3 次原站会自动降级成 digit。"""
    try:
        cap = (_post_json("/api/gva/base/captchaV3", {}) or {}).get("data") or {}
        captcha_id = (cap.get("captchaId") or "").strip()
        if not captcha_id:
            print("登录失败：拿不到 captchaId")
            return ""

        answer = ""
        if cap.get("captchaMode") == "slide":
            # slide 模式的校验值是一个字符串坐标；用轨道中段即可通过
            answer = str(int((cap.get("trackWidth") or 300) // 2))
        else:
            pic = (cap.get("picPath") or "").split(",", 1)[-1]
            if pic:
                import base64
                import captcha as cap_mod
                q = cap_mod.read_png_text(base64.b64decode(pic))
                left, op, right = q.replace("=?", "").replace(" = ?", "").split(" ")
                answer = str(int(left) + int(right)) if op == "+" else str(int(left) - int(right))

        if answer:
            verified = _post_json("/api/gva/base/captchaV3/verify",
                                  {"captchaId": captcha_id, "captcha": answer})
            if not ((verified.get("data") or {}).get("success")):
                # 校验不过不影响登录：密码错误的提示才是用户真正需要看到的
                print(f"验证码校验未通过：{verified.get('msg') or ''}")

        obj = _post_json("/api/gva/base/login", {
            "username": username, "password": password,
            "captcha": answer, "captchaId": captcha_id,
        })
        token = ((obj.get("data") or {}).get("token") or "")
        if obj.get("code") == 0 and token:
            return token
        print(f"登录失败：{obj.get('msg') or obj.get('message') or '未知错误'}")
        return ""
    except Exception as exc:  # noqa: BLE001
        print(f"登录失败: {exc}")
        return ""


def _set(**kw):
    with MIRROR_LOCK:
        MIRROR_STATE.update(kw)
        snap = dict(MIRROR_STATE)
    # 进度同时写库：server.py 不 import 本模块，靠读这张表给控制台进度条
    try:
        db.set_config("mirror_state", snap)
    except Exception:  # noqa: BLE001
        pass


def _count_items(obj) -> int:
    """从回包里尽量数出条目数（列表接口用于翻页判断）。"""
    data = obj.get("data") if isinstance(obj, dict) and "data" in obj else obj
    if isinstance(data, dict):
        for k in ("items", "list", "records", "rows", "works"):
            v = data.get(k)
            if isinstance(v, list):
                return len(v)
    return 0


# 每个分页接口最多翻几页：保证一个数据面里各板块都有数据（先跑通用）
PAGE_CAP = 3


def _mirror_assets() -> int:
    """样式归档：把站点 css（html/app/css/*.css）存进 mirror_assets 表。
    只按线上的 /_next/static/css/ 原始 URL 登记；服务端不再从这张表出样式，
    对外样式统一走 /app/css/*。"""
    n = 0
    if CSS_DIR.is_dir():
        for p in sorted(CSS_DIR.glob("*.css")):
            try:
                data = p.read_bytes()
            except OSError:
                continue
            db.asset_save("/_next/static/css/" + p.name, data)
            n += 1
    return n


def _engine_characters(limit: int, full: bool = False, token: str = ""):
    """角色详情镜像：从 works 表取角色 id，逐个抓 findNarratiumCharacterByCharacterID 入库。
    full=True 时额外抓取世界书条目和 CG 图片。token 用于需要登录的接口。"""
    try:
        rows = db.query(
            "SELECT DISTINCT character_id FROM works"
            " WHERE character_id IS NOT NULL AND character_id<>''"
            " ORDER BY updated_at DESC LIMIT ?", (limit,))
    except Exception:  # noqa: BLE001
        rows = []
    ids = [r.get("character_id") for r in rows if r.get("character_id")]
    total = len(ids)
    surface = "characters-full" if full else "characters"
    _set(running=True, surface=surface, phase="start", message=f"共 {total} 个角色待镜像",
         done=0, total=total, items=0, errors=0,
         started_at=time.strftime("%Y-%m-%d %H:%M:%S"), finished_at="")
    done = items = errors = 0
    worldbook_count = 0
    image_count = 0
    for cid in ids:
        # 1. 角色详情
        path = f"/api/gva/narratiumCharacter/findNarratiumCharacterByCharacterID?characterID={cid}"
        try:
            obj = upstream_json(path, token=token)
            size = db.character_save(cid, obj)
            items += 1
            _set(phase=f"角色 {cid[:24]}", message=f"入库 {size} 字节", done=done + 1,
                 total=total, items=items, errors=errors)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            _set(phase=f"抓取失败 {cid[:24]}", message=str(exc), done=done + 1,
                 total=total, items=items, errors=errors)
            done += 1
            continue

        # 2. 世界书条目（full 模式）
        if full:
            for custom in (False, True):
                wb_path = (f"/api/gva/narratiumWorldbookEntry/"
                          f"{'getUserCustomEntries' if custom else 'getEntriesByCharacterID'}"
                          f"?characterID={cid}")
                try:
                    wb_obj = upstream_json(wb_path, token=token)
                    wb_data = wb_obj.get("data") or []
                    if isinstance(wb_data, list) and wb_data:
                        n = db.worldbook_mirror_save(cid, wb_data, custom=custom)
                        worldbook_count += n
                        _set(phase=f"世界书 {cid[:16]}",
                             message=f"{n} 条{'自定义' if custom else ''}条目入库",
                             done=done, total=total, items=items, errors=errors)
                except Exception as exc:  # noqa: BLE001
                    # 世界书抓取失败不影响主流程
                    pass

            # 3. CG 图片下载（full 模式）
            try:
                urls = db.character_image_urls(cid)
                for url in urls:
                    if _mirror_character_image(cid, url):
                        image_count += 1
                if image_count:
                    _set(phase=f"图片 {cid[:16]}",
                         message=f"已缓存 {image_count} 张图片",
                         done=done, total=total, items=items, errors=errors)
            except Exception:  # noqa: BLE001
                pass

        done += 1
    msg = f"角色详情镜像完成：{items} 个入库"
    if full:
        msg += f"，世界书 {worldbook_count} 条，图片 {image_count} 张"
    msg += f"，失败 {errors}"
    _set(phase="done", message=msg,
         done=done, total=total, items=items, errors=errors,
         finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    _set(running=False)


def _mirror_character_image(character_id: str, url: str) -> bool:
    """下载角色的 CG 图片并缓存到本地。返回是否成功。"""
    import hashlib
    import imagedb
    try:
        # 生成本地文件名
        url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:24]
        ext = ".png"
        for e in (".jpg", ".jpeg", ".gif", ".webp", ".svg"):
            if e in url.lower():
                ext = e
                break
        name = f"char_{character_id[:16]}_{url_hash}{ext}"
        target = imagedb.IMAGE_DIR / name
        if target.exists():
            # 已缓存，只登记
            imagedb.register(f"/cdn/char/{character_id}/{name}", target, "cdn")
            return True
        # 下载
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ORIGIN + "/"})
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            data = r.read()
        if not data:
            return False
        imagedb.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        imagedb.register(f"/cdn/char/{character_id}/{name}", target, "cdn")
        return True
    except Exception:  # noqa: BLE001
        return False


def _err_hint(exc: Exception, token: str) -> str:
    """把抓取异常转成用户看得懂的提示：401 说明该接口要登录态。"""
    text = str(exc)
    if "401" in text or "Unauthorized" in text:
        return ("该接口需要登录：请在镜像配置里填源站账号密码后重试"
                if not token else "登录态已失效，请重新启动镜像（会重新登录）")
    return text


def _engine(surface: str, limit: int, username: str = "", password: str = ""):
    limit = max(1, min(limit, 500))
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    _set(running=True, surface=surface, phase="start", message="启动镜像",
         done=0, total=0, items=0, errors=0, started_at=started, finished_at="")
    # 如果填了登录凭据，先登录拿 x-token（原站现在对热门榜/推荐/世界书等接口
    # 启用了鉴权，未登录一律 401），所有数据面都会带上这个登录态
    token = ""
    if username and password:
        _set(phase="login", message="正在登录源站...")
        token = upstream_login(username, password)
        if token:
            _set(message="登录成功，开始镜像")
        else:
            _set(message="登录失败，将以游客模式镜像（需登录的接口会失败）")
    try:
        if surface == "assets":
            n = _mirror_assets()
            _set(phase="done", message=f"样式入库完成：{n} 个 css 已写入 mirror_assets",
                 done=1, total=1, items=n, errors=0,
                 finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            return
        if surface == "characters":
            _engine_characters(limit, full=False, token=token)
            return
        if surface == "characters-full":
            _engine_characters(limit, full=True, token=token)
            return
        entries = SURFACE_ENDPOINTS.get(surface, [])
        total = len(entries)
        done = 0
        items = 0
        errors = 0
        for tpl in entries:
            if "{page}" in tpl:
                page = 1
                while page <= PAGE_CAP and items < limit:
                    path = tpl.format(page=page)
                    try:
                        obj = upstream_json(path, token=token)
                    except Exception as exc:  # noqa: BLE001
                        errors += 1
                        done += 1
                        _set(phase=f"抓取失败 {path}", message=_err_hint(exc, token),
                             done=done, total=total, items=items, errors=errors)
                        break
                    db.ingest_content(path, path, obj)
                    got = _count_items(obj)
                    items += got
                    done += 1
                    _set(phase=f"抓取 {path}", message="",
                         done=done, total=total, items=items, errors=errors)
                    if got < 24:
                        break
                    page += 1
            else:
                path = tpl
                done += 1
                try:
                    obj = upstream_json(path, token=token)
                    db.ingest_content(path, path, obj)
                    items += _count_items(obj)
                    _set(phase=f"抓取 {path}", done=done, total=total,
                         items=items, errors=errors)
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    _set(phase=f"抓取失败 {path}", message=_err_hint(exc, token),
                         done=done, total=total, items=items, errors=errors)
        _set(phase="done", message="镜像完成", done=done, total=total,
             items=items, errors=errors,
             finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as exc:  # noqa: BLE001
        _set(phase="error", message=str(exc),
             finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    finally:
        _set(running=False)


def creative_sync(limit: int = 60, username: str = "", password: str = "") -> dict:
    """从线上把创意广场抓进本地库（分类 + 标签 + 列表 + 详情/源码）。
    与手动镜像共用 upstream_login / upstream_json 的登录与 x-token 机制。"""
    limit = max(1, min(limit, 200))
    stats: dict = {"items": 0, "details": 0, "errors": 0}

    token = ""
    if username and password:
        token = upstream_login(username, password)
        if not token:
            stats["auth"] = "登录失败，请检查源站账号密码"

    def fetch(p: str):
        return upstream_json(p, timeout=45, token=token)

    for p in ("/api/gva/creativePlaza/catalog", "/api/gva/creativePlaza/tags"):
        try:
            obj = fetch(p)
            db.ingest_content(p, p, obj)
        except Exception:  # noqa: BLE001
            stats["errors"] += 1

    found: list[int] = []
    page = 1
    while len(found) < limit:
        p = f"/api/gva/creativePlaza/items?page={page}&pageSize=24&sortBy=latest"
        try:
            obj = fetch(p)
        except Exception:  # noqa: BLE001
            stats["errors"] += 1
            break
        db.ingest_content("/api/gva/creativePlaza/items", p, obj)
        rows = ((obj.get("data") or {}).get("items") or []) if isinstance(obj, dict) else []
        ids = [int(r.get("id") or 0) for r in rows if isinstance(r, dict)
               and int(r.get("id") or 0) > 0]
        found += [i for i in ids if i not in found]
        if len(rows) < 24 or not ids:
            break
        page += 1
    stats["items"] = len(found)

    def pull_detail(item_id: int):
        p = f"/api/gva/creativePlaza/items/{item_id}"
        try:
            obj = fetch(p)
            db.ingest_content("/api/gva/creativePlaza/items", p, obj)
            return 1
        except Exception:  # noqa: BLE001
            return 0

    with ThreadPoolExecutor(max_workers=6) as pool:
        stats["details"] = sum(pool.map(pull_detail, found[:limit]))

    # 开源商品的源码不在详情回包里，要单独调 /items/:id/prompt。
    # 之前同步只抓到详情就结束，导致「开源 8 条、有源码 0 条」——
    # 这里对仍缺源码的开源商品逐个补抓（详情抓回来的这批 + 库里历史缺的）。
    stats["sources"] = 0
    want = {i for i in db.creative_missing_source_ids(400)}
    for item_id in sorted(want):
        try:
            obj = fetch(f"/api/gva/creativePlaza/items/{item_id}/prompt")
        except Exception:  # noqa: BLE001
            stats["errors"] += 1
            continue
        d = obj.get("data") if isinstance(obj, dict) else None
        if isinstance(d, str):                      # 有的上游直接把源码放 data 字符串
            d = {"promptContent": d}
        if not isinstance(d, dict):
            continue
        saved = db.creative_save_prompt(
            item_id,
            content=d.get("promptContent", d.get("prompt_content")),
            data=d.get("promptData", d.get("prompt_data")),
            paired=d.get("pairedCssPrompt", d.get("paired_css_prompt")),
        )
        if saved:
            stats["sources"] += 1

    # 评论不在这里批量抓（接口限流很紧，会把同步拖到几分钟）：
    # 打开商品详情时由 handle_creative_plaza 按需抓一次并落库。
    stats.update(db.creative_counts())
    return stats


if __name__ == "__main__":
    # 独立运行入口（server.py 用子进程调用，两者互不 import）：
    #   python mirror.py                                    抓首页 HTML + 静态资源
    #   python mirror.py mirror <surface> <limit> [账号] [密码]   手动镜像落库
    #   python mirror.py sync [limit] [账号] [密码]               创意广场同步
    _argv = sys.argv[1:]
    if _argv:
        # 独立跑时没有 server.py 帮着建表，先自己 init（幂等）
        db.init()
    if _argv and _argv[0] == "mirror":
        _engine(_argv[1] if len(_argv) > 1 else "works",
                int(_argv[2]) if len(_argv) > 2 else 60,
                _argv[3] if len(_argv) > 3 else "",
                _argv[4] if len(_argv) > 4 else "")
    elif _argv and _argv[0] == "sync":
        creative_sync(int(_argv[1]) if len(_argv) > 1 else 60,
                      _argv[2] if len(_argv) > 2 else "",
                      _argv[3] if len(_argv) > 3 else "")
    else:
        main()
