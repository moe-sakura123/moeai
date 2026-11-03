#!/usr/bin/env python3
"""
imagedb.py — 图片专用数据库（独立 SQLite：data/image.db）

页面里出现的每一张图（动图 gif/webp、静态图 png/jpg/svg、上传图、CDN 图）
都在这里有登记。数据库里**只存相对项目根的路径**，绝对路径由后端在读取时
按当前项目目录自动拼接（项目整体搬目录也不用改库）：

  images
    id           自增主键
    url_key      请求路径或原始地址（唯一）如 /image/a.gif、/cdn/xx/yy.png
    local_path   相对项目根的路径，如 html/image/ab12cd.gif
    kind         扩展名：gif/webp/apng 属动图，其余为静态图
    animated     1 = 动图，0 = 静态图
    bytes        文件大小
    sha256       内容指纹
    source       来源：upload / cdn / page / local
    created_at   首次登记时间
    last_seen_at 最近一次命中时间
"""

import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "image.db"
IMAGE_DIR = ROOT / "html" / "image"          # 所有落盘图片统一放这里

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
             ".avif", ".bmp", ".apng"}
ANIMATED_EXT = {".gif", ".webp", ".apng"}      # 动图

_local = threading.local()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def init() -> None:
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS images (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            url_key      TEXT UNIQUE NOT NULL,
            local_path   TEXT NOT NULL,
            kind         TEXT,
            animated     INTEGER NOT NULL DEFAULT 0,
            bytes        INTEGER,
            sha256       TEXT,
            source       TEXT NOT NULL DEFAULT 'local',
            created_at   TEXT NOT NULL,
            last_seen_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_images_url ON images(url_key);
        CREATE INDEX IF NOT EXISTS idx_images_created ON images(created_at DESC);
        """
    )
    # 旧库：abs_path 是历史遗留的绝对路径列，迁移掉，只保留 local_path
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)")}
        if "abs_path" in cols:
            conn.execute("ALTER TABLE images DROP COLUMN abs_path")
    except sqlite3.Error:
        pass
    fix_animated()


def abs_path_of(local_path: str | Path) -> Path:
    """把库里的相对路径按当前项目根拼接成绝对路径。

    兼容历史数据里可能出现的绝对路径（直接返回）；前缀永远以
    ``imagedb.ROOT``（即本文件所在目录）为准，项目搬家无需改库。
    """
    p = Path(str(local_path or ""))
    if p.is_absolute():
        return p
    return (ROOT / p).resolve()


def rel_path_of(file_path: str | Path) -> str | None:
    """文件 → 相对项目根的 posix 路径；不在项目内返回 None。"""
    try:
        return Path(file_path).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return None


def _decorate(row) -> dict:
    """数据库行 → 接口返回：补上运行时计算的 abs_path（不落库）。"""
    data = dict(row)
    data["abs_path"] = str(abs_path_of(data.get("local_path") or ""))
    return data


def kind_of(path: str | Path) -> str:
    return Path(str(path)).suffix.lower().lstrip(".") or "bin"


def is_animated(path: str | Path) -> bool:
    """判断是不是动图：gif 直接算；webp / apng 看文件头有没有动画块"""
    p = Path(str(path))
    ext = p.suffix.lower()
    if ext == ".gif":
        return True
    if ext in (".webp", ".apng", ".png"):
        try:
            head = p.open("rb").read(4096)
        except OSError:
            return False
        if ext == ".webp":
            return head[:4] == b"RIFF" and b"ANIM" in head
        if ext == ".apng":
            return b"acTL" in head
    return False


def fix_animated() -> int:
    """修正已登记记录的动图标记（webp 静态图不该算动图）"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, local_path, kind, animated FROM images WHERE kind IN ('webp','apng','png')"
    ).fetchall()
    n = 0
    for r in rows:
        want = 1 if is_animated(abs_path_of(r["local_path"])) else 0
        if want != r["animated"]:
            conn.execute("UPDATE images SET animated=? WHERE id=?", (want, r["id"]))
            n += 1
    return n


def lookup(url_key: str) -> dict | None:
    """按请求地址查图片记录（abs_path 由 local_path 实时拼接）"""
    if not url_key:
        return None
    row = get_conn().execute("SELECT * FROM images WHERE url_key=?", (url_key,)).fetchone()
    return _decorate(row) if row else None


def touch(url_key: str) -> None:
    get_conn().execute("UPDATE images SET last_seen_at=? WHERE url_key=?",
                       (now_iso(), url_key))


def register(url_key: str, file_path: str | Path, source: str = "local") -> dict | None:
    """把一张本地图片登记进数据库（url_key 唯一，重复登记只刷新最近命中时间）。

    库里只写相对项目根的 local_path；绝对路径不落库，读的时候再拼。
    """
    p = Path(file_path)
    if not p.is_file():
        return None
    rel = rel_path_of(p)
    if not rel:
        return None
    try:
        data = p.read_bytes()
    except OSError:
        return None
    now = now_iso()
    sha = hashlib.sha256(data).hexdigest()
    existing = lookup(url_key)
    if existing and existing.get("sha256") == sha:
        touch(url_key)
        return lookup(url_key)
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO images(url_key, local_path, kind, animated,
                           bytes, sha256, source, created_at, last_seen_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(url_key) DO UPDATE SET
            local_path=excluded.local_path,
            kind=excluded.kind, animated=excluded.animated, bytes=excluded.bytes,
            sha256=excluded.sha256, source=excluded.source,
            last_seen_at=excluded.last_seen_at
        """,
        (url_key, rel, kind_of(p), 1 if is_animated(p) else 0,
         len(data), sha, source, now, now),
    )
    return lookup(url_key)


def resolve(url_key: str) -> Path | None:
    """查 url_key → 本地文件；记录还在但文件没了返回 None"""
    row = lookup(url_key)
    if not row:
        return None
    p = abs_path_of(row["local_path"])
    return p if p.is_file() else None


def register_dir(source: str = "page") -> int:
    """把 image/ 目录下已有的图片批量登记（启动时跑一次）"""
    init()
    n = 0
    if not IMAGE_DIR.is_dir():
        return 0
    for p in IMAGE_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in IMAGE_EXT:
            key = "/image/" + p.relative_to(IMAGE_DIR).as_posix()
            if register(key, p, source) :
                n += 1
    return n


def list_images(limit: int = 200, offset: int = 0) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM images ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return [_decorate(r) for r in rows]


def clear_all() -> dict:
    """清空图片库：删除已登记的本地图片文件 + 清空登记记录。

    供控制台“清空所有数据”调用。只删除位于图片目录内的文件，
    避免误删项目内其他文件；返回 {records, files_removed}。
    """
    image_dir = IMAGE_DIR.resolve()
    conn = get_conn()
    rows = conn.execute("SELECT id, local_path FROM images").fetchall()
    removed_files = 0
    for r in rows:
        p = abs_path_of(r["local_path"]).resolve()
        try:
            p.relative_to(image_dir)
        except ValueError:
            # 文件不在图片目录内，保留（安全边界）
            continue
        if p.is_file():
            try:
                p.unlink()
                removed_files += 1
            except OSError:
                pass
    conn.execute("DELETE FROM images")
    try:
        conn.execute("DELETE FROM sqlite_sequence WHERE name='images'")
    except sqlite3.Error:
        pass
    return {"records": len(rows), "files_removed": removed_files}


def stats() -> dict:
    row = get_conn().execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b,"
        " COALESCE(SUM(animated),0) AS gif FROM images").fetchone()
    return {"images": row["n"], "image_bytes": row["b"], "animated_images": row["gif"]}


init()
