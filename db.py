#!/usr/bin/env python3
"""
db.py — 本地 SQLite 数据层（纯标准库 sqlite3）

表：
  users         本地账号
  sessions      登录会话（Cookie / x-token）
  request_log   访问与操作日志
  favorites     本地收藏
  checkin_records 签到记录（每人每天一条）
  chat_threads  对话会话
  chat_messages 对话消息
  uploads       上传/落地的图片，记录本地路径
  works         内容库：卡片文字（角色名、简介、作者、评分、标签）
  work_tags     内容库：作品 ↔ 标签
  tag_catalog   内容库：标签字典（/api/gva/creativePlaza/tags）
  content_api   内容库：各内容接口的形状 + 原始文字载荷（离线还原用）
  meta          schema 版本等键值

> 账号 / 会话 / 普通接口**不进库**（缓存放浏览器）；这里只有「文字与标签」这类内容数据。
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import struct
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "missai.db"
DB_MARKER = DATA_DIR / ".current_db"      # 控制台切换数据库后记住当前文件
UPLOAD_DIR = ROOT / "html" / "image"      # 上传图片统一落到 html/image/

# 这些表是控制台的“骨架”，清空 / 删除所有表时必须保留
SYSTEM_TABLES = ("meta", "db_op_log", "users", "sessions")
# 清空数据时额外保留模型接入配置（聊天 + 绘图 + 视频）
PROTECTED_CLEAR = SYSTEM_TABLES + ("model_providers",)
# 模型接入三表合一：model_providers 为唯一物理表，旧表名保留为视图 + 写兼容触发器。
# kind 仍表示预设键（openai/dashscope/...），model_type 才是「聊天/绘图/视频」的分流键。
_PROVIDER_BASE_COLS = (
    ("kind", "'openai'"),
    ("name", "''"),
    ("base_url", "''"),
    ("api_key", "''"),
    ("models", "'[]'"),
    ("status", "'unknown'"),
    ("last_error", "''"),
    ("last_check_at", "NULL"),
    ("enabled", "1"),
    ("is_default", "0"),
    ("default_model", "''"),
    ("created_at", "''"),
    ("updated_at", "''"),
)
_PROVIDER_EXTRA_COLS = {
    "llm": (("profile", "'{}'"),),
    "image": (("sizes", "'[\"1024x1024\",\"1024x1536\",\"1536x1024\"]'"),
              ("extra", "'{}'")),
    "video": (("specs", "'[\"5s\",\"10s\"]'"), ("extra", "'{}'")),
}
_PROVIDER_TABLES = {
    "llm": "llm_providers",
    "image": "image_providers",
    "video": "video_providers",
}
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SCHEMA_VERSION = 1

# 默认管理员账号（首次启动自动写入，见 ensure_admin()）
ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin123456"

_local = threading.local()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    """按线程缓存连接；控制台切换数据库后，各线程会自动重连到新文件。"""
    conn = getattr(_local, "conn", None)
    want = str(DB_PATH)
    if conn is None or getattr(_local, "conn_path", None) != want:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(want, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        _local.conn_path = want
    return conn


def _migrate_model_providers(conn: sqlite3.Connection) -> None:
    """把三张旧接入表合并进 model_providers；旧表名改建成可读写视图。

    幂等：旧表已迁完时只重建视图；重复执行不会重复插入，也不会报错。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_providers (
            model_type    TEXT NOT NULL,
            id            INTEGER NOT NULL,
            kind          TEXT NOT NULL DEFAULT 'openai',
            name          TEXT NOT NULL DEFAULT '',
            base_url      TEXT NOT NULL DEFAULT '',
            api_key       TEXT NOT NULL DEFAULT '',
            models        TEXT NOT NULL DEFAULT '[]',
            status        TEXT NOT NULL DEFAULT 'unknown',
            last_error    TEXT DEFAULT '',
            last_check_at TEXT,
            enabled       INTEGER NOT NULL DEFAULT 1,
            is_default    INTEGER NOT NULL DEFAULT 0,
            default_model TEXT DEFAULT '',
            profile       TEXT NOT NULL DEFAULT '{}',
            sizes         TEXT NOT NULL DEFAULT '["1024x1024","1024x1536","1536x1024"]',
            specs         TEXT NOT NULL DEFAULT '["5s","10s"]',
            extra         TEXT NOT NULL DEFAULT '{}',
            created_at    TEXT NOT NULL DEFAULT '',
            updated_at    TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (model_type, id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_providers"
                 " ON model_providers(model_type, is_default DESC, id)")

    for mtype, table in _PROVIDER_TABLES.items():
        extra = _PROVIDER_EXTRA_COLS[mtype]
        cols_with_default = list(_PROVIDER_BASE_COLS) + list(extra)
        obj = conn.execute("SELECT type FROM sqlite_master WHERE name=?",
                           (table,)).fetchone()
        # 1) 旧物理表 → 搬数据后删除（只在还是 table 时执行）
        if obj and obj["type"] == "table":
            old_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            names = ",".join(c for c, _ in cols_with_default)
            selects = ",".join(c if c in old_cols else d for c, d in cols_with_default)
            conn.execute(
                f"INSERT OR IGNORE INTO model_providers(model_type,id,{names})"
                f" SELECT ?,id,{selects} FROM {table}", (mtype,))
            conn.execute(f"DROP TABLE {table}")

        # 2) 旧表名 → 视图（顺序与旧表一致）
        view_cols = ["id"] + [c for c, _ in cols_with_default]
        conn.execute(f"DROP VIEW IF EXISTS {table}")
        conn.execute(f"CREATE VIEW {table} AS SELECT {','.join(view_cols)}"
                     f" FROM model_providers WHERE model_type='{mtype}'")

        # 3) 写兼容：INSTEAD OF 触发器把旧 SQL 转到新表
        defaults = dict(cols_with_default)
        data_cols = [c for c in view_cols if c != "id"]
        ins_vals = ", ".join(
            f"COALESCE(NEW.{c},{defaults[c]})" if defaults[c] != "NULL" else f"NEW.{c}"
            for c in data_cols)
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_ins INSTEAD OF INSERT ON {table} BEGIN "
            f"INSERT INTO model_providers(model_type,id,{','.join(data_cols)})"
            f" VALUES('{mtype}',"
            f" COALESCE(NEW.id,(SELECT COALESCE(MAX(id),0)+1 FROM model_providers"
            f" WHERE model_type='{mtype}')), {ins_vals}); END")
        set_vals = ", ".join(
            f"{c}=COALESCE(NEW.{c},{defaults[c]})" if defaults[c] != "NULL" else f"{c}=NEW.{c}"
            for c in data_cols)
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_upd INSTEAD OF UPDATE ON {table} BEGIN "
            f"UPDATE model_providers SET {set_vals}"
            f" WHERE model_type='{mtype}' AND id=OLD.id; END")
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_del INSTEAD OF DELETE ON {table} BEGIN "
            f"DELETE FROM model_providers WHERE model_type='{mtype}' AND id=OLD.id; END")


def _migrate_chat_memories(conn: sqlite3.Connection) -> int:
    """旧 chat_memories → chat_memory_items 一次性回填（幂等）。

    以 source_turn='legacy:chat_memories:<id>' 作为迁移标记，重复执行只补新行。
    旧表保留不删，仍可只读兼容。
    """
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "chat_memories" not in names or "chat_memory_items" not in names:
        return 0
    rows = conn.execute(
        "SELECT m.* FROM chat_memories m WHERE NOT EXISTS ("
        " SELECT 1 FROM chat_memory_items i"
        " WHERE i.source_turn='legacy:chat_memories:'||m.id)"
        " ORDER BY m.id").fetchall()
    n = 0
    for r in rows:
        content = str(r["content"] or "").strip()
        if not content:
            continue
        ts = str(r["created_at"] or "") or now_iso()
        h = hashlib.sha1(re.sub(r"\s+", " ", content.lower()).encode("utf-8")).hexdigest()
        cur = conn.execute(
            "INSERT INTO chat_memory_items(user_id, character_id, session_id,"
            " kind, content, subject, object, importance, confidence,"
            " source_turn, source_msg_ids, status, hash, created_at, updated_at)"
            " VALUES(?,?,?,?,?, '', '', 0.6, 0.8, ?, '[]', 'active', ?, ?, ?)",
            (r["user_id"], str(r["character_id"] or ""),
             str(r["session_id"] or ""), "summary", content,
             f"legacy:chat_memories:{int(r['id'])}", h, ts, ts))
        mid = int(cur.lastrowid or 0)
        if mid:
            try:
                conn.execute(
                    "INSERT INTO chat_memory_fts(memory_id, content, subject, object)"
                    " VALUES(?,?, '', '')", (mid, content))
            except sqlite3.OperationalError:
                pass
        n += 1
    return n


def init() -> None:
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        -- 控制台：数据库操作日志（Python 数据库操作终端读它）
        CREATE TABLE IF NOT EXISTS db_op_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            op         TEXT NOT NULL,
            table_name TEXT,
            detail     TEXT,
            user_id    INTEGER,
            ok         INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_db_op_log_id ON db_op_log(id DESC);

        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name     TEXT UNIQUE NOT NULL,
            nick_name     TEXT,
            email         TEXT UNIQUE,
            password_hash TEXT NOT NULL,
            salt          TEXT NOT NULL,
            header_img    TEXT,
            signature     TEXT,
            sex           TEXT,
            role          TEXT NOT NULL DEFAULT 'user',
            experience    INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            user_agent TEXT,
            ip         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

        -- 签到：每人每天一条（UNIQUE 保证一天只能签一次），points 记当天拿到的积分
        CREATE TABLE IF NOT EXISTS checkin_records (
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            date       TEXT NOT NULL,
            points     INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, date)
        );
        CREATE INDEX IF NOT EXISTS idx_checkin_user_date
            ON checkin_records(user_id, date DESC);

        -- 控制台：消耗流水（聊天 token/积分、生图积分），供消耗管理统计
        CREATE TABLE IF NOT EXISTS consumption_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            kind         TEXT NOT NULL DEFAULT 'chat',
            target_id    TEXT NOT NULL DEFAULT '',
            target_name  TEXT NOT NULL DEFAULT '',
            billing_mode TEXT NOT NULL DEFAULT 'points',
            tokens       INTEGER NOT NULL DEFAULT 0,
            points       INTEGER NOT NULL DEFAULT 0,
            date         TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_consumption_date ON consumption_log(date DESC);
        CREATE INDEX IF NOT EXISTS idx_consumption_user
            ON consumption_log(user_id, date DESC);
        CREATE INDEX IF NOT EXISTS idx_consumption_target
            ON consumption_log(kind, target_id, date DESC);

        -- 控制台：积分 / 经验调整流水（每次人工调整写一条）
        CREATE TABLE IF NOT EXISTS points_ledger (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind       TEXT NOT NULL DEFAULT 'points',
            delta      INTEGER NOT NULL DEFAULT 0,
            balance    INTEGER NOT NULL DEFAULT 0,
            reason     TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_points_ledger_user
            ON points_ledger(user_id, id DESC);

        -- 经验事件去重：收藏 / 点赞 / 浏览每天每个目标只加一次经验
        CREATE TABLE IF NOT EXISTS exp_events (
            user_id    INTEGER NOT NULL,
            kind       TEXT NOT NULL,
            target_id  TEXT NOT NULL DEFAULT '',
            date       TEXT NOT NULL,
            amount     INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id, kind, target_id, date)
        );

        CREATE TABLE IF NOT EXISTS request_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            method      TEXT NOT NULL,
            path        TEXT NOT NULL,
            status      INTEGER,
            duration_ms REAL,
            user_id     INTEGER,
            ip          TEXT,
            bytes       INTEGER,
            note        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_log_ts ON request_log(ts DESC);

        CREATE TABLE IF NOT EXISTS favorites (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            kind       TEXT NOT NULL DEFAULT 'character',
            target_id  TEXT NOT NULL,
            title      TEXT,
            payload    TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, kind, target_id)
        );

        CREATE TABLE IF NOT EXISTS chat_threads (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            character_id TEXT,
            title       TEXT,
            stats_json  TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id   INTEGER NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            content_llm TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_msg_thread ON chat_messages(thread_id, id);

        -- 记忆区：AI 对对话自动生成的记忆摘要（按角色存，随消息注入）
        CREATE TABLE IF NOT EXISTS chat_memories (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            character_id TEXT,
            session_id   TEXT,
            content      TEXT NOT NULL,
            round_no     INTEGER,
            created_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mem_char ON chat_memories(user_id, character_id, id);

        -- 记忆摘要游标：按会话记录已经总结到的最后一条消息 id / 条数。
        -- 新会话从 0 开始，不会继承其他会话的进度；删除会话时一起删掉。
        CREATE TABLE IF NOT EXISTS chat_summary_state (
            session_id    TEXT PRIMARY KEY,
            user_id       INTEGER,
            character_id  TEXT,
            covered_until INTEGER NOT NULL DEFAULT 0,
            covered_count INTEGER NOT NULL DEFAULT 0,
            rolling_summary TEXT NOT NULL DEFAULT '',
            turns_since_update INTEGER NOT NULL DEFAULT 0,
            updated_at    TEXT NOT NULL
        );

        -- 记忆固定槽位：短期 / 长期 / 用户信息 / 承诺，按会话隔离。
        -- 旧表 chat_memories 继续只读兼容，新流程不再写新行到它。
        CREATE TABLE IF NOT EXISTS chat_memory_slots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER,
            character_id  TEXT,
            session_id    TEXT,
            slot          TEXT NOT NULL,
            content       TEXT NOT NULL,
            covered_until INTEGER NOT NULL DEFAULT 0,
            updated_at    TEXT NOT NULL,
            UNIQUE(session_id, slot)
        );
        CREATE INDEX IF NOT EXISTS idx_mem_slot
            ON chat_memory_slots(user_id, session_id, slot);

        -- 算法记忆：结构化条目 + 向量 + 召回日志。
        -- FTS5 虚拟表在 init 末尾单独建（旧 SQLite 不支持时自动降级为 Python BM25）。
        CREATE TABLE IF NOT EXISTS chat_memory_items (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER,
            character_id   TEXT NOT NULL DEFAULT '',
            session_id     TEXT NOT NULL DEFAULT '',
            kind           TEXT NOT NULL DEFAULT 'fact',
            content        TEXT NOT NULL,
            subject        TEXT NOT NULL DEFAULT '',
            object         TEXT NOT NULL DEFAULT '',
            importance     REAL NOT NULL DEFAULT 0.5,
            confidence     REAL NOT NULL DEFAULT 0.7,
            event_time     TEXT NOT NULL DEFAULT '',
            source_turn    TEXT NOT NULL DEFAULT '',
            source_msg_ids TEXT NOT NULL DEFAULT '[]',
            status         TEXT NOT NULL DEFAULT 'active',
            recall_count   INTEGER NOT NULL DEFAULT 0,
            last_recalled_at TEXT,
            merged_into    INTEGER,
            cluster_id     INTEGER,
            hash           TEXT NOT NULL DEFAULT '',
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mem_item_scope
            ON chat_memory_items(user_id, character_id, session_id, status, id DESC);
        CREATE INDEX IF NOT EXISTS idx_mem_item_hash ON chat_memory_items(hash);

        -- 向量按「记忆 × 模型」存：hash 基线向量始终有，openai 向量可补算。
        CREATE TABLE IF NOT EXISTS chat_memory_vectors (
            memory_id INTEGER NOT NULL,
            model     TEXT NOT NULL DEFAULT 'hash',
            dim       INTEGER NOT NULL,
            vec       BLOB NOT NULL,
            PRIMARY KEY(memory_id, model)
        );

        -- 本地 LSA embedding：模型元信息 / 词项与 IDF / 训练状态
        -- 向量本体仍存 chat_memory_vectors(model='local-lsa:vN')
        CREATE TABLE IF NOT EXISTS local_embed_models (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL DEFAULT 'local-lsa',
            version      INTEGER NOT NULL DEFAULT 1,
            kind         TEXT NOT NULL DEFAULT 'local',
            dim          INTEGER NOT NULL DEFAULT 0,
            corpus_count INTEGER NOT NULL DEFAULT 0,
            term_count   INTEGER NOT NULL DEFAULT 0,
            source       TEXT NOT NULL DEFAULT 'memory',
            params_json  TEXT NOT NULL DEFAULT '{}',
            active       INTEGER NOT NULL DEFAULT 1,
            trained_at   TEXT NOT NULL DEFAULT '',
            elapsed_ms   INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_local_model_active
            ON local_embed_models(name, active, id DESC);

        CREATE TABLE IF NOT EXISTS local_embed_terms (
            model_id INTEGER NOT NULL,
            term     TEXT NOT NULL,
            df       INTEGER NOT NULL DEFAULT 0,
            idf      REAL NOT NULL DEFAULT 0,
            vec      BLOB NOT NULL,          -- 该词项在 k 个主题轴上的分量（float32）
            PRIMARY KEY(model_id, term)
        );
        CREATE INDEX IF NOT EXISTS idx_local_term_model
            ON local_embed_terms(model_id);

        CREATE TABLE IF NOT EXISTS local_embed_state (
            model_id        INTEGER PRIMARY KEY,
            last_train_at    TEXT NOT NULL DEFAULT '',
            last_project_at  TEXT NOT NULL DEFAULT '',
            projected_count  INTEGER NOT NULL DEFAULT 0,
            corpus_at_train  INTEGER NOT NULL DEFAULT 0,
            updated_at       TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS chat_memory_recall_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            session_id  TEXT NOT NULL DEFAULT '',
            turn_id     TEXT NOT NULL DEFAULT '',
            memory_id   INTEGER NOT NULL,
            rank        INTEGER NOT NULL DEFAULT 0,
            injected_at INTEGER NOT NULL DEFAULT 0,
            score_json  TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mem_recall
            ON chat_memory_recall_log(user_id, session_id, id DESC);

        -- 发给 LLM 的完整 prompt 快照（每次 chatStream 调用前落盘，用于调试/复现/审计）
        CREATE TABLE IF NOT EXISTS chat_prompt_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            session_id   TEXT,
            character_id TEXT,
            model        TEXT,
            messages     TEXT NOT NULL,   -- JSON 序列化的完整 msgs 数组
            message_count INTEGER,
            bytes        INTEGER,
            requests     TEXT NOT NULL DEFAULT '[]',  -- 本回合实际发出的全部请求（URL + body）
            turn_id       TEXT NOT NULL DEFAULT '',
            finish_reason TEXT NOT NULL DEFAULT '',
            sections_json TEXT NOT NULL DEFAULT '{}',  -- 按 section 的 token 统计
            prompt_hash   TEXT NOT NULL DEFAULT '',    -- 固定前缀 hash（对照 prefix cache）
            worldbook_json TEXT NOT NULL DEFAULT '[]', -- 本轮命中的世界书条目
            memory_json   TEXT NOT NULL DEFAULT '[]',  -- 本轮注入的记忆槽位
            diff_json     TEXT NOT NULL DEFAULT '[]',  -- 与上一轮相比的分段 diff
            created_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_prompt_session ON chat_prompt_log(session_id, id);

        CREATE TABLE IF NOT EXISTS uploads (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT NOT NULL DEFAULT 'image',
            local_path TEXT NOT NULL,
            remote_url TEXT,
            sha256     TEXT,
            bytes      INTEGER,
            created_at TEXT NOT NULL
        );

        -- 内容库：页面卡片上的文字（名称 / 简介 / 作者 / 评分 / 标签）
        CREATE TABLE IF NOT EXISTS works (
            character_id TEXT PRIMARY KEY,
            name         TEXT,
            summary      TEXT,
            author_name  TEXT,
            cover_url    TEXT,
            rate         REAL,
            votes        INTEGER,
            tags         TEXT,
            raw          TEXT,
            path         TEXT,
            first_seen   TEXT,
            updated_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_works_updated ON works(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_works_name ON works(name);

        -- 内容库：作品与标签的对应（标签统计、离线标签筛选都读它）
        CREATE TABLE IF NOT EXISTS work_tags (
            work_id    TEXT NOT NULL,
            tag        TEXT NOT NULL,
            pos        INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (work_id, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_work_tags_tag ON work_tags(tag);

        -- 内容库：标签字典（创作广场的标签表）
        CREATE TABLE IF NOT EXISTS tag_catalog (
            tag_id      TEXT PRIMARY KEY,
            name        TEXT,
            category    TEXT,
            usage_count INTEGER,
            updated_at  TEXT
        );

        -- 内容库：每个内容接口的形状与原始文字载荷（线上不通时按它还原回包）
        CREATE TABLE IF NOT EXISTS content_api (
            url_key    TEXT PRIMARY KEY,
            path       TEXT,
            shape      TEXT,
            container  TEXT,
            extras     TEXT,
            payload    TEXT,
            items      INTEGER,
            fetched_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_content_api_path ON content_api(path);

        -- 创意广场：商品（列表 / 详情 / 开源源码都落这里，断网也能看）
        CREATE TABLE IF NOT EXISTS creative_items (
            id                   INTEGER PRIMARY KEY,
            item_type            TEXT,
            title                TEXT,
            description          TEXT,
            price                INTEGER DEFAULT 0,
            tags                 TEXT DEFAULT '[]',
            is_open_source       INTEGER DEFAULT 0,
            allow_trial          INTEGER DEFAULT 0,
            author_id            INTEGER DEFAULT 0,
            author_name          TEXT,
            author_avatar        TEXT,
            sales_count          INTEGER DEFAULT 0,
            like_count           INTEGER DEFAULT 0,
            dislike_count        INTEGER DEFAULT 0,
            approval_rate        REAL DEFAULT 0,
            tip_count            INTEGER DEFAULT 0,
            tip_total            INTEGER DEFAULT 0,
            heat_score           REAL DEFAULT 0,
            heat_tier            INTEGER DEFAULT 0,
            is_editor_choice     INTEGER DEFAULT 0,
            status               TEXT DEFAULT 'active',
            version              INTEGER DEFAULT 1,
            prompt_content       TEXT,
            prompt_data          TEXT,
            extracted_post_prompt TEXT,
            paired_css_prompt    TEXT,
            bound_work_id        TEXT,
            bound_work_name      TEXT,
            reference_work_id    TEXT,
            raw                  TEXT,
            source               TEXT DEFAULT 'remote',
            first_seen           TEXT,
            updated_at           TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_creative_items_updated
            ON creative_items(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_creative_items_type
            ON creative_items(item_type);
        CREATE INDEX IF NOT EXISTS idx_creative_items_author
            ON creative_items(author_id);

        -- 创意广场：我的创意（购买 / 试用 / 自建）
        CREATE TABLE IF NOT EXISTS creative_user_items (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id               INTEGER,
            item_id               INTEGER NOT NULL,
            acquisition_type      TEXT DEFAULT 'purchased',
            status                TEXT DEFAULT 'enabled',
            is_enabled            INTEGER DEFAULT 1,
            trial_rounds_total    INTEGER DEFAULT 0,
            trial_rounds_remaining INTEGER DEFAULT 0,
            trial_times_used      INTEGER DEFAULT 0,
            current_version       INTEGER DEFAULT 1,
            usage_rounds_count    INTEGER DEFAULT 0,
            created_at            TEXT,
            updated_at            TEXT,
            UNIQUE(user_id, item_id, acquisition_type)
        );
        CREATE INDEX IF NOT EXISTS idx_creative_user_items_user
            ON creative_user_items(user_id, item_id);

        -- 创意广场：评论（线上抓到的 + 本地写的）
        CREATE TABLE IF NOT EXISTS creative_comments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id       INTEGER NOT NULL,
            remote_id     INTEGER,
            user_id       INTEGER,
            user_name     TEXT,
            user_avatar   TEXT,
            content       TEXT,
            rating        INTEGER DEFAULT 0,
            reply_content TEXT,
            reply_at      TEXT,
            source        TEXT DEFAULT 'local',
            created_at    TEXT,
            updated_at    TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_creative_comment_remote
            ON creative_comments(item_id, remote_id);
        CREATE INDEX IF NOT EXISTS idx_creative_comment_item
            ON creative_comments(item_id, id DESC);

        -- 创意广场：点赞 / 点踩
        CREATE TABLE IF NOT EXISTS creative_votes (
            user_id    INTEGER,
            item_id    INTEGER,
            vote_type  TEXT,
            created_at TEXT,
            PRIMARY KEY (user_id, item_id)
        );

        -- 创意广场：购买记录（幂等键唯一）
        CREATE TABLE IF NOT EXISTS creative_purchases (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER,
            item_id         INTEGER,
            price           INTEGER DEFAULT 0,
            points_left     INTEGER DEFAULT 0,
            idempotency_key TEXT UNIQUE,
            created_at      TEXT
        );

        -- 创意广场：装备绑定（会话 / 角色）
        CREATE TABLE IF NOT EXISTS creative_bindings (
            user_id    INTEGER,
            scope      TEXT NOT NULL,
            target_id  TEXT NOT NULL,
            item_id    INTEGER NOT NULL,
            item_type  TEXT,
            created_at TEXT,
            PRIMARY KEY (user_id, scope, target_id, item_id)
        );

        -- 创意广场：全局默认装备
        CREATE TABLE IF NOT EXISTS creative_defaults (
            user_id    INTEGER,
            item_type  TEXT,
            item_id    INTEGER,
            created_at TEXT,
            PRIMARY KEY (user_id, item_type)
        );

        -- 创作页：我的作品（草稿整包 + 抽出的列表字段）
        CREATE TABLE IF NOT EXISTS my_works (
            user_id      INTEGER NOT NULL,
            character_id TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'draft',
            title        TEXT,
            summary      TEXT,
            cover_url    TEXT,
            scheduled_at TEXT,
            payload      TEXT NOT NULL DEFAULT '{}',
            refs         TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (user_id, character_id)
        );
        CREATE INDEX IF NOT EXISTS idx_my_works_updated ON my_works(updated_at DESC);

        -- 创作页：世界书条目（/creation/worldbook）
        CREATE TABLE IF NOT EXISTS worldbook_entries (
            user_id      INTEGER NOT NULL,
            entry_id     TEXT NOT NULL,
            character_id TEXT,
            custom       INTEGER NOT NULL DEFAULT 0,
            payload      TEXT NOT NULL DEFAULT '{}',
            position     INTEGER NOT NULL DEFAULT 0,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (user_id, entry_id)
        );
        CREATE INDEX IF NOT EXISTS idx_worldbook_char
            ON worldbook_entries(user_id, character_id, custom);

        -- 创作页：配置与模型列表（筛选项 / 模型费率 / 媒体目录）
        CREATE TABLE IF NOT EXISTS config_cache (
            key        TEXT PRIMARY KEY,
            payload    TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        -- 平台数值规则：每个属性一行 JSON（keywords + 全部分档），按 position 排序
        -- character_id='global' 为平台默认；角色专属规则同名属性直接覆盖全局
        CREATE TABLE IF NOT EXISTS stat_rule_docs (
            character_id TEXT NOT NULL DEFAULT 'global',
            stat         TEXT NOT NULL,
            payload      TEXT NOT NULL DEFAULT '{}',
            position     INTEGER NOT NULL DEFAULT 0,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (character_id, stat)
        );

        -- 模型接入：OpenAI 协议的 api + key（数据库只存最基础的两项，
        -- models 是连上之后拉回来的模型列表缓存，供控制台与聊天界面显示）
        -- kind 是预设键：openai / dashscope / deepseek / volcengine / custom，
        -- 协议都按 OpenAI 兼容来走，日后加预设只要往 LLM_PRESETS 里加一行
        CREATE TABLE IF NOT EXISTS llm_providers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            kind          TEXT NOT NULL DEFAULT 'openai',
            name          TEXT NOT NULL,
            base_url      TEXT NOT NULL,
            api_key       TEXT NOT NULL DEFAULT '',
            models        TEXT NOT NULL DEFAULT '[]',
            status        TEXT NOT NULL DEFAULT 'unknown',
            last_error    TEXT DEFAULT '',
            last_check_at TEXT,
            enabled       INTEGER NOT NULL DEFAULT 1,
            is_default    INTEGER NOT NULL DEFAULT 0,
            default_model TEXT DEFAULT '',
            profile       TEXT NOT NULL DEFAULT '{}',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        -- 绘图模型接入：与聊天接入分开存（地址 / 密钥 / 模型缓存 / 默认模型 / 可选尺寸）
        -- 协议按 OpenAI 兼容走：GET {base}/models、POST {base}/images/generations
        -- kind 是预设键：openai / dashscope / siliconflow / volcengine / custom / local
        CREATE TABLE IF NOT EXISTS image_providers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            kind          TEXT NOT NULL DEFAULT 'openai',
            name          TEXT NOT NULL,
            base_url      TEXT NOT NULL,
            api_key       TEXT NOT NULL DEFAULT '',
            models        TEXT NOT NULL DEFAULT '[]',
            status        TEXT NOT NULL DEFAULT 'unknown',
            last_error    TEXT DEFAULT '',
            last_check_at TEXT,
            enabled       INTEGER NOT NULL DEFAULT 1,
            is_default    INTEGER NOT NULL DEFAULT 0,
            default_model TEXT DEFAULT '',
            sizes         TEXT NOT NULL DEFAULT '["1024x1024","1024x1536","1536x1024"]',
            extra         TEXT NOT NULL DEFAULT '{}',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        -- 视频模型接入：与绘图接入分开存（地址 / 密钥 / 模型缓存 / 默认模型 / 可选时长规格）
        -- 检测走 GET {base}/models；kind 是预设键：openai / dashscope / volcengine / custom / local
        CREATE TABLE IF NOT EXISTS video_providers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            kind          TEXT NOT NULL DEFAULT 'openai',
            name          TEXT NOT NULL,
            base_url      TEXT NOT NULL,
            api_key       TEXT NOT NULL DEFAULT '',
            models        TEXT NOT NULL DEFAULT '[]',
            status        TEXT NOT NULL DEFAULT 'unknown',
            last_error    TEXT DEFAULT '',
            last_check_at TEXT,
            enabled       INTEGER NOT NULL DEFAULT 1,
            is_default    INTEGER NOT NULL DEFAULT 0,
            default_model TEXT NOT NULL DEFAULT '',
            specs         TEXT NOT NULL DEFAULT '["5s","10s"]',
            extra         TEXT NOT NULL DEFAULT '{}',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        -- 绘图作品（绘图广场展示 + 生成历史）
        CREATE TABLE IF NOT EXISTS image_works (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            author      TEXT NOT NULL DEFAULT '',
            prompt      TEXT NOT NULL DEFAULT '',
            negative    TEXT NOT NULL DEFAULT '',
            provider_id INTEGER,
            provider    TEXT NOT NULL DEFAULT '',
            model       TEXT NOT NULL DEFAULT '',
            size        TEXT NOT NULL DEFAULT '',
            seed        INTEGER NOT NULL DEFAULT 0,
            url_key     TEXT NOT NULL,
            image_id    INTEGER,
            kind        TEXT NOT NULL DEFAULT 'image',
            likes       INTEGER NOT NULL DEFAULT 0,
            status      TEXT NOT NULL DEFAULT 'done',
            error       TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_image_works_created ON image_works(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_image_works_likes ON image_works(likes DESC);

        -- 绘图模板生成任务（/api/gva/aiMedia/creator/recipes/<key>/generations）
        -- 源站这套接口是「提交 → 轮询」，这里用一张表存任务状态，
        -- 生成完把图片登记进 image_works，前端拿到 assetUrl 就算成功
        CREATE TABLE IF NOT EXISTS media_generations (
            id              TEXT PRIMARY KEY,
            user_id         INTEGER,
            recipe_key      TEXT NOT NULL DEFAULT '',
            recipe_version  INTEGER NOT NULL DEFAULT 0,
            media_type      TEXT NOT NULL DEFAULT 'image',
            status          TEXT NOT NULL DEFAULT 'submitted',
            asset_status    TEXT NOT NULL DEFAULT 'pending',
            asset_url       TEXT NOT NULL DEFAULT '',
            prompt          TEXT NOT NULL DEFAULT '',
            negative        TEXT NOT NULL DEFAULT '',
            model           TEXT NOT NULL DEFAULT '',
            size            TEXT NOT NULL DEFAULT '',
            seed            INTEGER NOT NULL DEFAULT 0,
            image_work_id   INTEGER,
            charged_credits INTEGER NOT NULL DEFAULT 0,
            refunded        INTEGER NOT NULL DEFAULT 0,
            failure_code    TEXT NOT NULL DEFAULT '',
            failure_msg     TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL DEFAULT '',
            request         TEXT NOT NULL DEFAULT '{}',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_media_gen_key
            ON media_generations(user_id, idempotency_key);
        CREATE INDEX IF NOT EXISTS idx_media_gen_created
            ON media_generations(created_at DESC);
        """
    )
    # 中文全文索引：trigram 分词支持子串匹配；建不起来就落回纯 Python BM25
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chat_memory_fts USING fts5("
            "memory_id UNINDEXED, content, subject, object,"
            " tokenize='trigram')")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    # 旧库没有 role 列 → 补一列（默认 user）
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "role" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
    # 个人资料：签名 / 性别（设置页的人个信息）
    for col, ddl in (("signature", "signature TEXT"), ("sex", "sex TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {ddl}")
    # 成长经验：旧库补列，并给老账号一笔起航经验，避免升级后被发布等级门槛卡住
    if "experience" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN experience INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE users SET experience=30")
    # 缓存已迁到浏览器 HTTP 缓存，旧库里的 api_cache 表清掉
    conn.execute("DROP TABLE IF EXISTS api_cache")

    # 旧库的 chat_threads 没有会话列 → 补齐（对接 chatSession 系列接口）
    tcols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_threads)").fetchall()}
    for col, ddl in (
        ("session_id", "session_id TEXT"),
        ("character_name", "character_name TEXT"),
        ("avatar_url", "avatar_url TEXT"),
        ("preview", "preview TEXT"),
        ("message_count", "message_count INTEGER NOT NULL DEFAULT 0"),
        ("status", "status TEXT NOT NULL DEFAULT 'active'"),
        ("stats_json", "stats_json TEXT NOT NULL DEFAULT '{}'"),
    ):
        if col not in tcols:
            conn.execute(f"ALTER TABLE chat_threads ADD COLUMN {ddl}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_session"
        " ON chat_threads(user_id, session_id)")

    # 消息表：content 存原文（前端渲染状态栏/选项/CG），content_llm 存喂给模型的
    # 纯文本。旧库没有该列 → 补一列，空值在读取时按 content 现算兼容。
    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
    if mcols and "content_llm" not in mcols:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN content_llm TEXT"
                     " NOT NULL DEFAULT ''")

    # 创作页：my_works 少 refs 列（早先建过表）→ 补
    wcols = {r["name"] for r in conn.execute("PRAGMA table_info(my_works)").fetchall()}
    if wcols and "refs" not in wcols:
        conn.execute("ALTER TABLE my_works ADD COLUMN refs TEXT")

    # 绘图作品：早先建过表没有 seed 列 → 补（种子，生成同款要用）
    icols = {r["name"] for r in conn.execute("PRAGMA table_info(image_works)").fetchall()}
    if icols and "seed" not in icols:
        conn.execute("ALTER TABLE image_works ADD COLUMN seed INTEGER NOT NULL DEFAULT 0")

    # Prompt 快照：旧表只有 messages → 补列（请求汇总 / 回合标识 / 分段统计）
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_prompt_log)").fetchall()}
    for col, ddl in (
        ("requests", "requests TEXT NOT NULL DEFAULT '[]'"),
        ("turn_id", "turn_id TEXT NOT NULL DEFAULT ''"),
        ("finish_reason", "finish_reason TEXT NOT NULL DEFAULT ''"),
        ("sections_json", "sections_json TEXT NOT NULL DEFAULT '{}'"),
        ("prompt_hash", "prompt_hash TEXT NOT NULL DEFAULT ''"),
        ("worldbook_json", "worldbook_json TEXT NOT NULL DEFAULT '[]'"),
        ("memory_json", "memory_json TEXT NOT NULL DEFAULT '[]'"),
        ("diff_json", "diff_json TEXT NOT NULL DEFAULT '[]'"),
    ):
        if col not in pcols:
            conn.execute(f"ALTER TABLE chat_prompt_log ADD COLUMN {ddl}")
    scols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_summary_state)").fetchall()}
    for col, ddl in (
        ("rolling_summary", "rolling_summary TEXT NOT NULL DEFAULT ''"),
        ("turns_since_update", "turns_since_update INTEGER NOT NULL DEFAULT 0"),
    ):
        if scols and col not in scols:
            conn.execute(f"ALTER TABLE chat_summary_state ADD COLUMN {ddl}")
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(llm_providers)").fetchall()}
    if "profile" not in pcols:
        conn.execute("ALTER TABLE llm_providers ADD COLUMN profile TEXT"
                     " NOT NULL DEFAULT '{}'")
    # 三张接入表 → model_providers（旧表名由本函数改建为视图，可重复执行）
    _migrate_model_providers(conn)
    # 旧记忆摘要 → 结构化记忆表（幂等；旧表保留只读）
    _migrate_chat_memories(conn)

    # 记忆向量：旧表是「一条记忆一个 JSON 向量」→ 重建为「记忆 × 模型」float32
    vcols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(chat_memory_vectors)").fetchall()}
    if vcols and "model" not in vcols:
        conn.execute("ALTER TABLE chat_memory_vectors"
                     " RENAME TO chat_memory_vectors_old")
        conn.execute("CREATE TABLE chat_memory_vectors ("
                     " memory_id INTEGER NOT NULL,"
                     " model TEXT NOT NULL DEFAULT 'hash',"
                     " dim INTEGER NOT NULL,"
                     " vec BLOB NOT NULL,"
                     " PRIMARY KEY(memory_id, model))")
        for r in conn.execute("SELECT memory_id, vec FROM chat_memory_vectors_old"
                              ).fetchall():
            try:
                vals = [float(x) for x in json.loads(r["vec"] or "[]")]
            except (ValueError, TypeError):
                vals = []
            if not vals:
                continue
            norm = (sum(x * x for x in vals)) ** 0.5 or 1.0
            conn.execute(
                "INSERT OR REPLACE INTO chat_memory_vectors(memory_id, model, dim, vec)"
                " VALUES(?, 'hash', ?, ?)",
                (int(r["memory_id"]), len(vals),
                 struct.pack(f"{len(vals)}f", *[x / norm for x in vals])))
        conn.execute("DROP TABLE chat_memory_vectors_old")

    # 创作页配置种子：筛选项与模型列表（线上没登录时也够用）
    now = now_iso()
    defaults = {
        "workFilter/config": {
            "orientation": ["男性向", "女性向", "双性恋", "男同性", "女同性"],
            "type": ["都市", "历史", "科幻", "恐怖", "悬疑", "玄幻", "修真", "同人",
                     "军事", "魔法", "后宫", "侦探", "纯聊"],
            "tags": ["R18", "RPG", "大世界", "纯爱", "低消耗", "自定义角色", "单人卡",
                     "萝莉", "妹妹", "乱伦", "出轨", "贞操", "绿帽", "催眠", "偷窥",
                     "性虐待", "绑架", "角色扮演", "女仆", "怀孕", "怪物"],
            "personality": ["支配", "顺从", "熟女", "恶霸", "女装男孩", "假小子",
                            "反派", "英雄", "傲娇", "病娇", "冷娇", "娇羞", "呆萌"],
            "source": ["原创角色", "游戏", "电影", "虚拟主播", "书籍", "民间传说"],
        },
        "llmModelRate/list": {
            "list": [{
                "ID": 1, "id": 1, "modelID": "local-default", "modelId": "local-default",
                "modelName": "本地默认模型", "poolName": "本地默认模型",
                "llmConfigID": 1, "isEnabled": True, "isDefault": True,
                "price": 0, "currency": "points",
            }],
            "total": 1,
        },
        "aiMedia/creator/catalog": {"list": [], "categories": [], "total": 0},
    }
    for key, payload in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO config_cache(key, payload, updated_at) VALUES(?,?,?)",
            (key, json.dumps(payload, ensure_ascii=False), now))

    # 内容接口形状种子：详情接口在线上没抓过时，离线也知道该把回包拼成
    # {"character": {...}} 这个形状（文字从 works 表里按 characterID 取）
    conn.execute(
        "INSERT OR IGNORE INTO content_api(url_key, path, shape, container, extras,"
        " payload, items, fetched_at) VALUES(?,?,?,?,?,?,?,?)",
        ("/api/gva/narratiumCharacter/findNarratiumCharacterByCharacterID",
         "/api/gva/narratiumCharacter/findNarratiumCharacterByCharacterID",
         "dict1:", "character", "{}", None, 0, now_iso()),
    )

    # 平台数值规则：旧结构 → 每个属性一行 JSON
    _migrate_stat_rule_docs()


# --------------------------------------------------------------- 通用查询

def query(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in get_conn().execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> dict | None:
    row = get_conn().execute(sql, params).fetchone()
    return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> int:
    cur = get_conn().execute(sql, params)
    return cur.rowcount


# --------------------------------------------------------------- 账号 / 会话

def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def create_user(user_name: str, password: str, nick_name: str = "", email: str = "") -> dict | None:
    salt = secrets.token_hex(16)
    try:
        cur = get_conn().execute(
            "INSERT INTO users(user_name, nick_name, email, password_hash, salt, experience, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (user_name, nick_name or user_name, email or None, _hash(password, salt), salt,
             exp_reward("register"), now_iso()),
        )
    except sqlite3.IntegrityError:
        return None
    return one("SELECT * FROM users WHERE id=?", (cur.lastrowid,))


def verify_user(identifier: str, password: str) -> dict | None:
    user = one(
        "SELECT * FROM users WHERE user_name=? OR email=? OR nick_name=?",
        (identifier, identifier, identifier),
    )
    if not user:
        return None
    if _hash(password, user["salt"]) != user["password_hash"]:
        return None
    execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), user["id"]))
    return user


def set_password(user_id: int, password: str) -> None:
    """重置某个账号的密码（重新加盐）"""
    salt = secrets.token_hex(16)
    execute(
        "UPDATE users SET password_hash=?, salt=? WHERE id=?",
        (_hash(password, salt), salt, user_id),
    )


def ensure_admin() -> dict | None:
    """默认管理员账号 admin / admin123456

    · 首次启动：不存在就创建；已存在但密码不是默认值 → 改成默认值（打上 meta 标记）
    · 之后启动：只保证账号还在、角色是 admin，**不再覆盖密码**（改过的密码能保住）
    """
    seeded = one("SELECT value FROM meta WHERE key='admin_seeded'")
    user = one("SELECT * FROM users WHERE user_name=?", (ADMIN_USER,))
    if user is None:
        user = create_user(ADMIN_USER, ADMIN_PASSWORD, "管理员")
    elif not seeded:
        set_password(user["id"], ADMIN_PASSWORD)
    if user is None:
        return None
    execute("UPDATE users SET role='admin' WHERE id=?", (user["id"],))
    # 管理员是本地所有者：经验不足时补到 Lv.3，避免自检/演示被发布门槛拦截
    if int(user.get("experience") or 0) < 100:
        execute("UPDATE users SET experience=100 WHERE id=?", (user["id"],))
        user = one("SELECT * FROM users WHERE id=?", (user["id"],)) or user
    if not seeded:
        execute("INSERT OR REPLACE INTO meta(key, value) VALUES('admin_seeded','1')")
    return one("SELECT * FROM users WHERE id=?", (user["id"],))


def create_session(user_id: int, user_agent: str = "", ip: str = "", days: int = 30) -> dict:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    execute(
        "INSERT INTO sessions(token, user_id, created_at, expires_at, user_agent, ip) VALUES(?,?,?,?,?,?)",
        (token, user_id, now_iso(), expires.isoformat(timespec="seconds"), user_agent[:200], ip[:64]),
    )
    return {"token": token, "expiresAt": int(expires.timestamp() * 1000)}


def get_session(token: str) -> dict | None:
    if not token:
        return None
    return one(
        "SELECT s.token, s.expires_at, s.user_id, u.* FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token=? AND s.expires_at > ?",
        (token, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )


def drop_session(token: str) -> None:
    execute("DELETE FROM sessions WHERE token=?", (token,))


def public_user(user: dict) -> dict:
    """前端 bootstrap / 登录响应里的 user 结构"""
    growth = level_info(user.get("experience") or 0)
    return {
        "id": user["id"],
        "userId": user["id"],
        "uuid": user["id"],
        "username": user["user_name"],
        "userName": user["user_name"],
        "nickName": user.get("nick_name") or user["user_name"],
        "email": user.get("email"),
        "headerImg": user.get("header_img") or "",
        "avatar": user.get("header_img") or "",
        "gender": user.get("sex") or "",
        "signature": user.get("signature") or "",
        "createdAt": user.get("created_at"),
        "experience": growth["experience"],
        "level": growth["level"],
        "levelName": growth["levelName"],
        "nextLevelExp": growth["nextLevelExp"],
        "levelProgress": growth["levelProgress"],
        "isMaxLevel": growth["isMaxLevel"],
    }


def get_user(user_id: int) -> dict | None:
    return one("SELECT * FROM users WHERE id=?", (user_id,))


def verify_password(user_id: int, password: str) -> bool:
    user = get_user(user_id)
    if not user:
        return False
    return _hash(password, user["salt"]) == user["password_hash"]


def update_user_fields(user_id: int, fields: dict) -> dict | None:
    """按白名单更新个人资料（设置页会传 nickName / headerImg 等驼峰字段）"""
    mapping = {
        "nickName": "nick_name", "nickname": "nick_name",
        "headerImg": "header_img", "avatar": "header_img",
        "signature": "signature", "sex": "sex", "gender": "sex",
    }
    sets, vals = [], []
    for key, value in fields.items():
        col = mapping.get(key)
        if col is None:
            continue
        if col == "nick_name" and (not isinstance(value, str) or not value.strip()):
            continue
        sets.append(f"{col}=?")
        vals.append(value.strip() if isinstance(value, str) else value)
    if sets:
        execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", (*vals, user_id))
    return get_user(user_id)


# --------------------------------------------------------------- 积分 / 签到
#
# 积分沿用 config_cache 里的 points:{uid}（默认 100）。签到记录进
# checkin_records（每人每天一条），签到时按「基础 + 周末 + 连签奖励」加分。

DEFAULT_POINTS = 100

# --------------------------------------------------------------- 成长规则（控制台「规则管理」）
#
# 全部规则都存在 config_cache 的 rules:global 里，控制台可改、立即生效：
#   等级门槛 / 发布门槛 / 各类经验奖励 / 签到积分 / 聊天计费 / 生图积分
RULES_KEY = "rules:global"

DEFAULT_LEVELS = (
    {"level": 1, "name": "新手", "exp": 0},
    {"level": 2, "name": "创作者", "exp": 30},
    {"level": 3, "name": "资深创作者", "exp": 100},
    {"level": 4, "name": "核心创作者", "exp": 250},
    {"level": 5, "name": "殿堂创作者", "exp": 500},
)

DEFAULT_RULES = {
    "levels": [dict(x) for x in DEFAULT_LEVELS],
    "publishMinLevel": 2,
    "exp": {
        "register": 20,
        "checkin": 10,
        "publish": 20,
        "favorite": 5,
        "like": 3,
        "view": 1,
    },
    "checkin": {
        "basePoints": 100,
        "weekendBonus": 50,
        "dailyGiftPoints": 0,
        "cycleDays": 30,
        "consecutiveRewards": [
            {"days": 3, "bonus": 500},
            {"days": 7, "bonus": 1000},
            {"days": 14, "bonus": 2000},
            {"days": 30, "bonus": 9999},
        ],
    },
    "chat": {
        "mode": "points",          # points=按次扣积分；token=按 token 折算成积分
        "pointsPerCall": 1,
        "tokensPerPoint": 100,     # 多少 token 折 1 积分
        "minTokensPerCall": 0,     # 每次调用至少按多少 token 计
    },
    "image": {
        "pointsPerImage": 5,
        "freeLocal": True,         # 本地占位绘图是否免费
    },
}


def default_rules() -> dict:
    return json.loads(json.dumps(DEFAULT_RULES))


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in (extra or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _int_range(value, default: int, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(low, min(high, n))


def normalize_rules(raw: dict) -> dict:
    """把任意输入收敛成结构完整、数值合理的规则（存库 / 读取都用它）"""
    rules = _deep_merge(default_rules(), raw if isinstance(raw, dict) else {})

    levels = []
    for i, item in enumerate(rules.get("levels") or []):
        if not isinstance(item, dict):
            continue
        level = _int_range(item.get("level"), i + 1, 1, 99)
        name = str(item.get("name") or f"Lv.{level}").strip()[:20] or f"Lv.{level}"
        exp = _int_range(item.get("exp"), 0, 0, 10 ** 7)
        levels.append({"level": level, "name": name, "exp": exp})
    if not levels:
        levels = [dict(x) for x in DEFAULT_LEVELS]
    levels = sorted(levels, key=lambda x: (x["exp"], x["level"]))
    levels[0]["exp"] = 0
    for i in range(1, len(levels)):
        if levels[i]["exp"] <= levels[i - 1]["exp"]:
            levels[i]["exp"] = levels[i - 1]["exp"] + 1
    for i, item in enumerate(levels):
        item["level"] = i + 1
    rules["levels"] = levels
    rules["publishMinLevel"] = _int_range(rules.get("publishMinLevel"), 2,
                                          1, len(levels))

    exp = rules.get("exp") if isinstance(rules.get("exp"), dict) else {}
    rules["exp"] = {k: _int_range(exp.get(k), DEFAULT_RULES["exp"][k], 0, 10 ** 6)
                    for k in DEFAULT_RULES["exp"]}

    ck = rules.get("checkin") if isinstance(rules.get("checkin"), dict) else {}
    rewards = []
    for item in (ck.get("consecutiveRewards") or []):
        if not isinstance(item, dict):
            continue
        days = _int_range(item.get("days"), 0, 1, 365)
        bonus = _int_range(item.get("bonus"), 0, 0, 10 ** 7)
        if days:
            rewards.append({"days": days, "bonus": bonus})
    rewards.sort(key=lambda x: x["days"])
    rewards = list({x["days"]: x for x in rewards}.values())
    rules["checkin"] = {
        "basePoints": _int_range(ck.get("basePoints"), 100, 0, 10 ** 7),
        "weekendBonus": _int_range(ck.get("weekendBonus"), 50, 0, 10 ** 7),
        "dailyGiftPoints": _int_range(ck.get("dailyGiftPoints"), 0, 0, 10 ** 7),
        "cycleDays": _int_range(ck.get("cycleDays"), 30, 1, 365),
        "consecutiveRewards": rewards,
    }

    chat = rules.get("chat") if isinstance(rules.get("chat"), dict) else {}
    rules["chat"] = {
        "mode": "token" if str(chat.get("mode") or "").lower() == "token" else "points",
        "pointsPerCall": _int_range(chat.get("pointsPerCall"), 1, 0, 10 ** 5),
        "tokensPerPoint": _int_range(chat.get("tokensPerPoint"), 100, 1, 10 ** 6),
        "minTokensPerCall": _int_range(chat.get("minTokensPerCall"), 0, 0, 10 ** 6),
    }

    img = rules.get("image") if isinstance(rules.get("image"), dict) else {}
    rules["image"] = {
        "pointsPerImage": _int_range(img.get("pointsPerImage"), 5, 0, 10 ** 5),
        "freeLocal": bool(img.get("freeLocal", True)),
    }
    return rules


def get_rules() -> dict:
    stored = get_config(RULES_KEY) or {}
    return normalize_rules(stored)


def save_rules(payload: dict) -> dict:
    rules = normalize_rules(payload)
    set_config(RULES_KEY, rules)
    return rules


def reset_rules() -> dict:
    return save_rules(default_rules())


def current_level_tiers(tiers: list[dict] | None = None) -> list[dict]:
    rows = tiers if isinstance(tiers, list) else get_rules().get("levels") or []
    return rows or [dict(x) for x in DEFAULT_LEVELS]


def exp_reward(kind: str, rules: dict | None = None) -> int:
    data = rules or get_rules()
    return int((data.get("exp") or {}).get(kind) or 0)


def publish_min_level(rules: dict | None = None) -> int:
    data = rules or get_rules()
    return _int_range(data.get("publishMinLevel"), 2, 1, len(data.get("levels") or DEFAULT_LEVELS))


def level_info(exp, tiers: list[dict] | None = None) -> dict:
    """累计经验 → 等级信息（当前等级、下一级门槛、进度百分比）"""
    total = max(0, int(exp or 0))
    rows = current_level_tiers(tiers)
    current = rows[0]
    index = 0
    for i, row in enumerate(rows):
        if total >= int(row.get("exp") or 0):
            current = row
            index = i
        else:
            break
    nxt = rows[index + 1] if index + 1 < len(rows) else None
    cur_exp = int(current.get("exp") or 0)
    if nxt:
        span = max(1, int(nxt.get("exp") or 0) - cur_exp)
        progress = max(0, min(100, int((total - cur_exp) * 100 / span)))
        next_exp = int(nxt.get("exp") or 0)
    else:
        progress = 100
        next_exp = total
    return {
        "level": int(current.get("level") or 1),
        "levelName": str(current.get("name") or "新手"),
        "experience": total,
        "currentLevelExp": cur_exp,
        "nextLevelExp": next_exp,
        "levelProgress": progress,
        "isMaxLevel": nxt is None,
    }


def user_level_info(user_id: int | None) -> dict:
    row = one("SELECT experience FROM users WHERE id=?", (user_id,)) if user_id else None
    return level_info(row.get("experience") if row else 0)


def add_experience(user_id: int, delta: int) -> dict:
    """增加（或扣减，实际不会被扣）经验，返回最新等级信息"""
    execute("UPDATE users SET experience=max(0, experience + ?) WHERE id=?",
            (int(delta), user_id))
    return user_level_info(user_id)


def award_exp_once(user_id: int | None, kind: str, target_id: str = "",
                   amount: int | None = None) -> dict | None:
    """收藏 / 点赞 / 浏览经验：每人每天每个目标只加一次；返回新等级或 None"""
    if not user_id:
        return None
    gain = exp_reward(kind) if amount is None else int(amount)
    if gain <= 0:
        return None
    today = date.today().isoformat()
    try:
        get_conn().execute(
            "INSERT INTO exp_events(user_id, kind, target_id, date, amount, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (user_id, str(kind)[:20], str(target_id or "")[:80], today, gain, now_iso()))
    except sqlite3.IntegrityError:
        return None
    return add_experience(user_id, gain)


_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_IMAGE_MARKUP_RE = re.compile(r"<image>.*?</image>", re.DOTALL)


def _strip_image_markup(text) -> str:
    """计费不计图片标签（<image>ID/URL</image>），避免 AICG 开关影响积分"""
    return _IMAGE_MARKUP_RE.sub("", str(text or ""))


def estimate_tokens(text) -> int:
    """粗估 token：中日韩字符按 1 token，其余字符按 4 字符 ≈ 1 token"""
    s = str(text or "")
    if not s:
        return 0
    cjk = len(_CJK_RE.findall(s))
    other = len(s) - cjk
    return max(1, cjk + (other + 3) // 4)


def chat_quote(user_id: int | None, prompt_text: str, reply_text: str = "",
               extra: int = 0) -> dict:
    """聊天计费试算：按规则模式算本次要扣多少积分（extra：AICG 插图附加费）"""
    chat = get_rules().get("chat") or {}
    tokens = estimate_tokens(_strip_image_markup(prompt_text)) + estimate_tokens(_strip_image_markup(reply_text))
    tokens = max(tokens, int(chat.get("minTokensPerCall") or 0))
    if str(chat.get("mode") or "points") == "token":
        rate = max(1, int(chat.get("tokensPerPoint") or 100))
        cost = ((tokens + rate - 1) // rate) if tokens else 0
    else:
        cost = int(chat.get("pointsPerCall") or 0)
    extra = max(0, int(extra or 0))
    return {
        "mode": "token" if str(chat.get("mode")) == "token" else "points",
        "tokens": tokens,
        "cost": max(0, cost) + extra,
        "extra": extra,
        "balance": points_balance(user_id) if user_id else 0,
    }


def charge_chat(user_id: int | None, prompt_text: str, reply_text: str = "",
                extra: int = 0) -> dict:
    """聊天结束后扣积分（余额不足时只扣到 0），返回扣费明细"""
    if not user_id:
        return {"mode": "guest", "tokens": 0, "cost": 0, "extra": 0, "deducted": 0,
                "balance": 0, "ok": True}
    quote = chat_quote(user_id, prompt_text, reply_text, extra)
    before = quote["balance"]
    deducted = min(quote["cost"], before)
    balance = add_points(user_id, -deducted) if deducted else before
    return {**quote, "deducted": deducted, "balance": balance,
            "ok": deducted >= quote["cost"]}


def image_charge(count: int = 1, local: bool = False) -> dict:
    """生图计费：按张数 × 单张积分；本地占位绘图可配置免费"""
    img = get_rules().get("image") or {}
    per = 0 if (local and img.get("freeLocal", True)) else int(img.get("pointsPerImage") or 0)
    total = per * max(0, int(count))
    return {"perImage": per, "total": total}


def log_consumption(user_id, kind: str, target_id="", target_name="", mode="points",
                    tokens: int = 0, points: int = 0) -> None:
    """记一条消耗流水（聊天 / 生图），控制台按日/周/月/作品聚合"""
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        uid = 0
    if not uid:
        return
    tokens = max(0, int(tokens or 0))
    points = max(0, int(points or 0))
    if not tokens and not points:
        return
    execute("INSERT INTO consumption_log(user_id, kind, target_id, target_name,"
            " billing_mode, tokens, points, date, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (uid, str(kind or "chat")[:20], str(target_id or "")[:80],
             str(target_name or "")[:120], str(mode or "points")[:20],
             tokens, points, date.today().isoformat(), now_iso()))


def points_balance(user_id: int) -> int:
    cfg = get_config(f"points:{user_id}") or {}
    try:
        return max(0, int(cfg.get("balance", DEFAULT_POINTS)))
    except (TypeError, ValueError):
        return DEFAULT_POINTS


def add_points(user_id: int, delta: int) -> int:
    cfg = get_config(f"points:{user_id}") or {}
    balance = max(0, points_balance(user_id) + int(delta))
    cfg["balance"] = balance
    cfg["points"] = balance
    set_config(f"points:{user_id}", cfg)
    return balance


def _checkin_rules() -> dict:
    ck = get_rules().get("checkin") or {}
    return {
        "base": int(ck.get("basePoints") or 0),
        "weekend": int(ck.get("weekendBonus") or 0),
        "gift": int(ck.get("dailyGiftPoints") or 0),
        "cycle": int(ck.get("cycleDays") or 30),
        "rewards": {int(x["days"]): int(x["bonus"])
                    for x in ck.get("consecutiveRewards") or []},
    }


def checkin_config() -> dict:
    ck = _checkin_rules()
    return {
        "enabled": True,
        "baseReward": ck["base"],
        "weekendBonus": ck["weekend"],
        "dailyGiftPoints": ck["gift"],
        "cycleDays": ck["cycle"],
        "consecutiveRewards": [{"days": d, "bonus": b}
                               for d, b in sorted(ck["rewards"].items())],
        "monthlyCardBonusPreview": [],
    }


def _checkin_rows(user_id: int) -> list[dict]:
    return query(
        "SELECT date, points FROM checkin_records WHERE user_id=? ORDER BY date DESC",
        (user_id,),
    )


def checkin_status(user_id: int) -> dict:
    today = date.today()
    rows = _checkin_rows(user_id)
    dates = {r["date"] for r in rows}
    last = rows[0]["date"] if rows else ""
    streak = 0
    if last:
        try:
            last_day = date.fromisoformat(last)
        except ValueError:
            last_day = None
        # 只有「今天或昨天」签过，连签才没断
        if last_day and (today - last_day).days <= 1:
            day = last_day
            while day.isoformat() in dates:
                streak += 1
                day -= timedelta(days=1)
    today_iso = today.isoformat()
    today_row = next((r for r in rows if r["date"] == today_iso), None)
    if today_row is not None:
        today_reward = int(today_row.get("points") or 0)
    else:
        ck = _checkin_rules()
        next_streak = streak + 1 if streak > 0 else 1
        today_reward = (ck["base"] + ck["gift"]
                        + (ck["weekend"] if today.weekday() >= 5 else 0)
                        + ck["rewards"].get(next_streak, 0))
    return {
        "hasCheckedIn": today_iso in dates,
        "currentConsecutiveDays": streak,
        "totalCheckinDays": len(rows),
        "lastCheckinDate": last,
        "todayReward": today_reward,
        "totalRewardEarned": sum(int(r.get("points") or 0) for r in rows),
        "appBonusPreview": None,
    }


def checkin_calendar(user_id: int, month: str = "") -> list[dict]:
    """某个月的签到日历：每天一条 {date, hasCheckin, points}"""
    today = date.today()
    first = today.replace(day=1)
    if month:
        try:
            y, m = month.split("-")[:2]
            first = date(int(y), int(m), 1)
        except (ValueError, TypeError):
            pass
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    rows = query(
        "SELECT date, points FROM checkin_records"
        " WHERE user_id=? AND date>=? AND date<?",
        (user_id, first.isoformat(), nxt.isoformat()),
    )
    found = {r["date"]: r.get("points") or 0 for r in rows}
    days = []
    day = first
    while day < nxt:
        iso = day.isoformat()
        days.append({"date": iso,
                     "hasCheckin": iso in found,
                     "points": found.get(iso, 0)})
        day += timedelta(days=1)
    return days


def checkin_sign(user_id: int) -> dict:
    """签到：一天只加一次分（重复请求幂等），返回签到结果"""
    today = date.today()
    iso = today.isoformat()
    if one("SELECT 1 FROM checkin_records WHERE user_id=? AND date=?", (user_id, iso)):
        data = checkin_status(user_id)
        growth = user_level_info(user_id)
        data.update({"isAppBonusReissue": False, "alreadyCheckedIn": True,
                     "consecutiveDays": data["currentConsecutiveDays"],
                     "rewardPoints": 0, "pointsEarned": 0, "expEarned": 0,
                     "balance": points_balance(user_id), "date": iso,
                     "experience": growth["experience"], "level": growth["level"],
                     "levelName": growth["levelName"], "nextLevelExp": growth["nextLevelExp"],
                     "levelProgress": growth["levelProgress"],
                     "isMaxLevel": growth["isMaxLevel"]})
        return data

    before = checkin_status(user_id)
    ck = _checkin_rules()
    streak = (before["currentConsecutiveDays"] + 1
              if before["currentConsecutiveDays"] > 0 else 1)
    weekend = ck["weekend"] if today.weekday() >= 5 else 0
    milestone = ck["rewards"].get(streak, 0)
    base = ck["base"]
    gift = ck["gift"]
    reward = base + gift + weekend + milestone

    try:
        execute(
            "INSERT INTO checkin_records(user_id, date, points, created_at)"
            " VALUES(?,?,?,?)",
            (user_id, iso, reward, now_iso()),
        )
    except sqlite3.IntegrityError:          # 并发下另一个请求刚签过
        growth = user_level_info(user_id)
        return {
            **checkin_status(user_id), "isAppBonusReissue": False,
            "alreadyCheckedIn": True, "rewardPoints": 0, "pointsEarned": 0,
            "consecutiveDays": checkin_status(user_id)["currentConsecutiveDays"],
            "balance": points_balance(user_id), "date": iso,
            "expEarned": 0, "experience": growth["experience"],
            "level": growth["level"], "levelName": growth["levelName"],
            "nextLevelExp": growth["nextLevelExp"],
            "levelProgress": growth["levelProgress"], "isMaxLevel": growth["isMaxLevel"],
        }

    balance = add_points(user_id, reward)
    exp_gain = exp_reward("checkin")
    growth = add_experience(user_id, exp_gain)
    after = checkin_status(user_id)
    return {
        **after,
        "isAppBonusReissue": False,
        "alreadyCheckedIn": False,
        "consecutiveDays": after["currentConsecutiveDays"],
        "rewardPoints": reward,
        "pointsEarned": reward,
        "expEarned": exp_gain,
        "baseReward": base,
        "dailyGiftPoints": gift,
        "weekendBonus": weekend,
        "bonusPoints": milestone,
        "balance": balance,
        "date": iso,
        "experience": growth["experience"],
        "level": growth["level"],
        "levelName": growth["levelName"],
        "nextLevelExp": growth["nextLevelExp"],
        "levelProgress": growth["levelProgress"],
        "isMaxLevel": growth["isMaxLevel"],
    }


# --------------------------------------------------------------- 控制台：积分管理

def points_admin_summary() -> dict:
    """控制台积分管理概览：账号数、积分总量、等级分布、今日签到、可发布账号"""
    rules = get_rules()
    tiers = rules.get("levels") or []
    min_level = publish_min_level(rules)
    rows = query("SELECT id, experience FROM users")
    level_counts = {}
    total_points = 0
    can_publish = 0
    for r in rows:
        total_points += points_balance(r["id"])
        growth = level_info(r.get("experience") or 0, tiers)
        level_counts[growth["level"]] = level_counts.get(growth["level"], 0) + 1
        if growth["level"] >= min_level:
            can_publish += 1
    today = date.today().isoformat()
    checked = one("SELECT COUNT(DISTINCT user_id) AS n FROM checkin_records WHERE date=?",
                  (today,)) or {}
    return {
        "totalUsers": len(rows),
        "totalPoints": total_points,
        "canPublish": can_publish,
        "checkedInToday": int(checked.get("n") or 0),
        "levelCounts": [{"level": t["level"], "count": level_counts.get(t["level"], 0)}
                        for t in tiers],
        "publishMinLevel": min_level,
        "rewardRegister": exp_reward("register", rules),
        "rewardCheckin": exp_reward("checkin", rules),
        "rewardPublish": exp_reward("publish", rules),
        "tiers": [{"level": t["level"], "name": t["name"], "exp": t["exp"]}
                  for t in tiers],
    }


def points_admin_rows(q: str = "", limit: int = 200) -> list[dict]:
    """控制台积分管理：账号的积分 / 经验 / 等级 / 签到 / 已发布作品"""
    limit = max(1, min(int(limit or 200), 500))
    sql = ("SELECT id, user_name, nick_name, email, role, experience, created_at,"
           " last_login_at FROM users")
    keyword = str(q or "").strip()
    if keyword:
        like = f"%{keyword}%"
        rows = query(sql + " WHERE user_name LIKE ? OR nick_name LIKE ? OR email LIKE ?"
                     " ORDER BY id DESC LIMIT ?", (like, like, like, limit))
    else:
        rows = query(sql + " ORDER BY id DESC LIMIT ?", (limit,))
    return _points_accounts(rows)


def _points_accounts(rows: list[dict]) -> list[dict]:
    """把 users 行补成积分管理需要的完整状态"""
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    checkins = {r["user_id"]: r for r in query(
        "SELECT user_id, COUNT(*) AS total, MAX(date) AS last_date"
        " FROM checkin_records WHERE user_id IN (%s) GROUP BY user_id"
        % ",".join("?" * len(ids)), tuple(ids))}
    works = {r["user_id"]: r["n"] for r in query(
        "SELECT user_id, COUNT(*) AS n FROM my_works WHERE status='published'"
        " AND user_id IN (%s) GROUP BY user_id" % ",".join("?" * len(ids)),
        tuple(ids))}
    rules = get_rules()
    tiers = rules.get("levels") or []
    min_level = publish_min_level(rules)
    out = []
    for r in rows:
        growth = level_info(r.get("experience") or 0, tiers)
        ck = checkins.get(r["id"]) or {}
        out.append({
            "id": r["id"],
            "userName": r["user_name"],
            "nickName": r["nick_name"] or "",
            "email": r["email"] or "",
            "role": r["role"],
            "points": points_balance(r["id"]),
            "experience": growth["experience"],
            "level": growth["level"],
            "levelName": growth["levelName"],
            "nextLevelExp": growth["nextLevelExp"],
            "levelProgress": growth["levelProgress"],
            "isMaxLevel": growth["isMaxLevel"],
            "canPublish": growth["level"] >= min_level,
            "checkinDays": int(ck.get("total") or 0),
            "lastCheckin": ck.get("last_date") or "",
            "publishedCount": int(works.get(r["id"]) or 0),
            "createdAt": r["created_at"],
            "lastLoginAt": r["last_login_at"] or "",
        })
    return out


def points_ledger_rows(limit: int = 40, user_id: int = 0) -> list[dict]:
    limit = max(1, min(int(limit or 40), 200))
    sql = ("SELECT l.id, l.user_id, l.kind, l.delta, l.balance, l.reason,"
           " l.created_at, u.user_name, u.nick_name FROM points_ledger l"
           " LEFT JOIN users u ON u.id=l.user_id")
    if user_id:
        rows = query(sql + " WHERE l.user_id=? ORDER BY l.id DESC LIMIT ?",
                     (user_id, limit))
    else:
        rows = query(sql + " ORDER BY l.id DESC LIMIT ?", (limit,))
    for r in rows:
        r["userName"] = r.get("user_name") or ""
        r["nickName"] = r.get("nick_name") or ""
    return rows


def admin_adjust_account(user_id: int, points_delta: int = 0, exp_delta: int = 0,
                         reason: str = "") -> dict | None:
    """控制台调整某账号的积分 / 经验；每次变动写入流水，返回账号最新状态"""
    user = get_user(user_id)
    if not user:
        return None
    note = str(reason or "").strip()[:200]
    if points_delta:
        balance = add_points(user_id, points_delta)
        execute("INSERT INTO points_ledger(user_id, kind, delta, balance, reason, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (user_id, "points", int(points_delta), balance, note, now_iso()))
    growth = user_level_info(user_id)
    if exp_delta:
        growth = add_experience(user_id, exp_delta)
        execute("INSERT INTO points_ledger(user_id, kind, delta, balance, reason, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (user_id, "exp", int(exp_delta), growth["experience"], note, now_iso()))
    db_log("ADJUST", "users",
           json.dumps({"userId": user_id, "pointsDelta": int(points_delta),
                       "expDelta": int(exp_delta), "reason": note}, ensure_ascii=False),
           user_id, 1)
    row = (_points_accounts([get_user(user_id) or user]) or [None])[0]
    if row:
        row = dict(row, levelProgress=growth["levelProgress"], level=growth["level"],
                   levelName=growth["levelName"], experience=growth["experience"],
                   nextLevelExp=growth["nextLevelExp"], isMaxLevel=growth["isMaxLevel"],
                   canPublish=growth["level"] >= publish_min_level())
    return row


# --------------------------------------------------------------- 控制台：消耗管理

def _range_bounds(kind: str) -> tuple[date, date]:
    """返回今天 / 本周（周一至周日）/ 本月的起止日期"""
    today = date.today()
    if kind == "week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if kind == "month":
        start = today.replace(day=1)
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start, nxt - timedelta(days=1)
    return today, today


def _cons_totals(start: date | None = None, end: date | None = None) -> dict:
    sql = ("SELECT COALESCE(SUM(tokens),0) AS tokens,"
           " COALESCE(SUM(points),0) AS points,"
           " COUNT(*) AS calls, COUNT(DISTINCT user_id) AS users FROM consumption_log")
    params: tuple = ()
    if start and end:
        sql += " WHERE date BETWEEN ? AND ?"
        params = (start.isoformat(), end.isoformat())
    row = one(sql, params) or {}
    return {"tokens": int(row.get("tokens") or 0),
            "points": int(row.get("points") or 0),
            "calls": int(row.get("calls") or 0),
            "users": int(row.get("users") or 0)}


def _cons_cases(ranges: list[tuple[str, date, date]]) -> tuple[str, list]:
    """生成 today/week/month 的 token / 积分 / 次数条件聚合列"""
    cols: list[str] = []
    params: list = []
    for key, start, end in ranges:
        for field, expr in (("tokens", "COALESCE(l.tokens,0)"),
                            ("points", "COALESCE(l.points,0)"),
                            ("calls", "1")):
            cols.append(f"COALESCE(SUM(CASE WHEN l.date BETWEEN ? AND ?"
                        f" THEN {expr} ELSE 0 END),0) AS {key}_{field}")
            params += [start.isoformat(), end.isoformat()]
    return ", ".join(cols), params


def _cons_series(granularity: str = "daily") -> list[dict]:
    """按日 / 周 / 月生成消耗趋势（日均取近 30 天，周取近 12 周，月取近 12 月）"""
    today = date.today()
    granularity = granularity if granularity in ("weekly", "monthly") else "daily"

    if granularity == "weekly":
        first = today - timedelta(days=today.weekday()) - timedelta(weeks=11)
        rows = query("SELECT date, COALESCE(SUM(tokens),0) AS tokens,"
                     " COALESCE(SUM(points),0) AS points, COUNT(*) AS calls"
                     " FROM consumption_log WHERE date>=? GROUP BY date",
                     (first.isoformat(),))
        by_day = {r["date"]: r for r in rows}
        out = []
        for i in range(12):
            bs = first + timedelta(weeks=i)
            be = bs + timedelta(days=6)
            tokens = points = calls = 0
            for j in range(7):
                row = by_day.get((bs + timedelta(days=j)).isoformat())
                if row:
                    tokens += int(row["tokens"] or 0)
                    points += int(row["points"] or 0)
                    calls += int(row["calls"] or 0)
            out.append({"label": f"{bs.strftime('%m-%d')}~{be.strftime('%m-%d')}",
                        "start": bs.isoformat(), "end": be.isoformat(),
                        "tokens": tokens, "points": points, "calls": calls})
        return out

    if granularity == "monthly":
        months = []
        y, m = today.year, today.month
        for _ in range(12):
            months.append(f"{y:04d}-{m:02d}")
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        months.reverse()
        rows = query("SELECT substr(date,1,7) AS bucket,"
                     " COALESCE(SUM(tokens),0) AS tokens,"
                     " COALESCE(SUM(points),0) AS points, COUNT(*) AS calls"
                     " FROM consumption_log WHERE date>=? GROUP BY bucket",
                     (months[0] + "-01",))
        by_month = {r["bucket"]: r for r in rows}
        out = []
        for key in months:
            y2, m2 = int(key[:4]), int(key[5:7])
            nxt = date(y2 + 1, 1, 1) if m2 == 12 else date(y2, m2 + 1, 1)
            row = by_month.get(key) or {}
            out.append({"label": key, "start": key + "-01",
                        "end": (nxt - timedelta(days=1)).isoformat(),
                        "tokens": int(row.get("tokens") or 0),
                        "points": int(row.get("points") or 0),
                        "calls": int(row.get("calls") or 0)})
        return out

    first = today - timedelta(days=29)
    rows = query("SELECT date, COALESCE(SUM(tokens),0) AS tokens,"
                 " COALESCE(SUM(points),0) AS points, COUNT(*) AS calls"
                 " FROM consumption_log WHERE date>=? GROUP BY date",
                 (first.isoformat(),))
    by_day = {r["date"]: r for r in rows}
    out = []
    for i in range(30):
        d = first + timedelta(days=i)
        row = by_day.get(d.isoformat()) or {}
        out.append({"label": d.strftime("%m-%d"), "start": d.isoformat(),
                    "end": d.isoformat(),
                    "tokens": int(row.get("tokens") or 0),
                    "points": int(row.get("points") or 0),
                    "calls": int(row.get("calls") or 0)})
    return out


def consumption_dashboard(granularity: str = "daily", limit: int = 200) -> dict:
    """控制台消耗管理：整体 / 每个用户 / 每个作品的今日、本周、本月、累计消耗"""
    limit = max(1, min(int(limit or 200), 1000))
    today = date.today()
    week = _range_bounds("week")
    month = _range_bounds("month")
    ranges = [("today", today, today), ("week", week[0], week[1]),
              ("month", month[0], month[1])]

    totals = {
        "today": _cons_totals(today, today),
        "week": _cons_totals(week[0], week[1]),
        "month": _cons_totals(month[0], month[1]),
        "all": _cons_totals(),
    }

    cases, case_params = _cons_cases(ranges)

    user_rows = query(
        "SELECT l.user_id AS id, u.user_name, u.nick_name,"
        " COALESCE(SUM(l.tokens),0) AS tokens,"
        " COALESCE(SUM(l.points),0) AS points, COUNT(*) AS calls, "
        + cases +
        " FROM consumption_log l LEFT JOIN users u ON u.id=l.user_id"
        " GROUP BY l.user_id ORDER BY points DESC, tokens DESC, id DESC LIMIT ?",
        tuple(case_params + [limit]))
    users = []
    for r in user_rows:
        user = {"id": int(r["id"]),
                "name": r.get("nick_name") or r.get("user_name") or f"#{r['id']}",
                "userName": r.get("user_name") or "",
                "total": {"tokens": int(r["tokens"] or 0),
                          "points": int(r["points"] or 0),
                          "calls": int(r["calls"] or 0)}}
        for key, _s, _e in ranges:
            user[key] = {"tokens": int(r.get(f"{key}_tokens") or 0),
                         "points": int(r.get(f"{key}_points") or 0),
                         "calls": int(r.get(f"{key}_calls") or 0)}
        users.append(user)

    work_rows = query(
        "SELECT l.kind, l.target_id, MAX(NULLIF(l.target_name,'')) AS target_name,"
        " COALESCE(SUM(l.tokens),0) AS tokens,"
        " COALESCE(SUM(l.points),0) AS points, COUNT(*) AS calls, "
        + cases +
        " FROM consumption_log l GROUP BY l.kind, l.target_id"
        " ORDER BY points DESC, tokens DESC, calls DESC LIMIT ?",
        tuple(case_params + [limit]))

    # 聊天目标的显示名优先用内容库里的角色名，其次用流水里记下的名字
    chat_ids = [r["target_id"] for r in work_rows
                if r["kind"] == "chat" and r["target_id"]]
    char_names: dict[str, str] = {}
    if chat_ids:
        names = query(
            "SELECT character_id, name FROM works WHERE character_id IN (%s)"
            % ",".join("?" * len(chat_ids)), tuple(chat_ids))
        char_names = {r["character_id"]: (r.get("name") or "") for r in names}

    works = []
    for r in work_rows:
        kind = str(r["kind"] or "chat")
        name = (char_names.get(r["target_id"] or "") if kind == "chat" else "") \
            or r.get("target_name") or ""
        works.append({
            "kind": kind,
            "kindName": "聊天" if kind == "chat" else ("生图" if kind == "image" else kind),
            "id": r.get("target_id") or "",
            "name": name or (r.get("target_id") or "未知作品"),
            "total": {"tokens": int(r["tokens"] or 0),
                      "points": int(r["points"] or 0),
                      "calls": int(r["calls"] or 0),
            },
            **{key: {"tokens": int(r.get(f"{key}_tokens") or 0),
                     "points": int(r.get(f"{key}_points") or 0),
                     "calls": int(r.get(f"{key}_calls") or 0)}
               for key, _s, _e in ranges},
        })

    by_kind = {}
    for r in query("SELECT kind, COALESCE(SUM(tokens),0) AS tokens,"
                   " COALESCE(SUM(points),0) AS points, COUNT(*) AS calls"
                   " FROM consumption_log GROUP BY kind"):
        by_kind[str(r["kind"] or "chat")] = {
            "tokens": int(r["tokens"] or 0), "points": int(r["points"] or 0),
            "calls": int(r["calls"] or 0)}

    return {
        "granularity": granularity if granularity in ("weekly", "monthly") else "daily",
        "totals": totals,
        "byKind": [{"kind": k, "kindName": "聊天" if k == "chat" else
                    ("生图" if k == "image" else k), **v}
                   for k, v in by_kind.items()],
        "series": _cons_series(granularity),
        "users": users,
        "works": works,
        "updatedAt": now_iso(),
    }


# --------------------------------------------------------------- 日志

def log_request(method: str, path: str, status: int | None, duration_ms: float,
                user_id: int | None, ip: str, size: int = 0, note: str = "") -> None:
    try:
        execute(
            "INSERT INTO request_log(ts, method, path, status, duration_ms, user_id, ip, bytes, note)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (now_iso(), method, path[:500], status, round(duration_ms, 2), user_id, ip[:64], size, note[:200]),
        )
    except sqlite3.Error:
        pass


# --------------------------------------------------------------- 收藏 / 对话 / 上传

def add_favorite(user_id: int | None, kind: str, target_id: str,
                 title: str = "", payload: str = "") -> bool:
    """写入收藏；返回是否为新收藏（用于「收藏 / 点赞」经验去重）"""
    existed = bool(one(
        "SELECT 1 FROM favorites WHERE user_id IS ? AND kind=? AND target_id=?",
        (user_id, kind, target_id)))
    execute(
        "INSERT INTO favorites(user_id, kind, target_id, title, payload, created_at) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(user_id, kind, target_id) DO UPDATE SET title=excluded.title,"
        " payload=excluded.payload, created_at=excluded.created_at",
        (user_id, kind, target_id, title, payload[:4000], now_iso()),
    )
    return not existed


def remove_favorite(user_id: int | None, kind: str, target_id: str) -> None:
    execute("DELETE FROM favorites WHERE user_id IS ? AND kind=? AND target_id=?",
            (user_id, kind, target_id))


def upsert_thread(user_id: int | None, character_id: str, title: str = "") -> int:
    row = one("SELECT id FROM chat_threads WHERE user_id IS ? AND character_id=?",
              (user_id, character_id))
    if row:
        if title:
            execute("UPDATE chat_threads SET title=?, updated_at=? WHERE id=?",
                    (title, now_iso(), row["id"]))
        return row["id"]
    cur = get_conn().execute(
        "INSERT INTO chat_threads(user_id, character_id, title, created_at, updated_at)"
        " VALUES(?,?,?,?,?)",
        (user_id, character_id, title, now_iso(), now_iso()),
    )
    return cur.lastrowid


def add_message(thread_id: int, role: str, content: str,
                content_llm: str | None = None) -> None:
    """content_llm：喂模型用的纯文本。assistant 不传则留空，读取时按原文现算；
    用户消息即原文。"""
    if content_llm is None:
        content_llm = content if role == "user" else ""
    execute("INSERT INTO chat_messages(thread_id, role, content, content_llm, created_at)"
            " VALUES(?,?,?,?,?)",
            (thread_id, role, content[:20000], str(content_llm or "")[:20000], now_iso()))
    execute("UPDATE chat_threads SET updated_at=? WHERE id=?", (now_iso(), thread_id))


def add_upload(kind: str, local_path: str, remote_url: str = "",
               sha: str = "", size: int = 0) -> int:
    cur = get_conn().execute(
        "INSERT INTO uploads(kind, local_path, remote_url, sha256, bytes, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (kind, local_path, remote_url, sha, size, now_iso()),
    )
    return cur.lastrowid


# --------------------------------------------------------------- 内容库（文字 / 标签）
#
# 站点卡片上的文字（角色名、简介、作者、评分）和标签都来自线上读接口。
# 每次接口回包都把条目拆进 works / work_tags / tag_catalog，
# 同时在 content_api 里记下该接口的形状与原始载荷 —— 线上不通时按形状还原回包。

WORK_ID_KEYS = ("characterID", "characterId", "character_id", "workId", "work_id")
WORK_NAME_KEYS = ("characterName", "name", "title")
WORK_SUMMARY_KEYS = ("summary", "description", "intro", "introduction")
WORK_AUTHOR_KEYS = ("authorName", "author_name", "author")
WORK_COVER_KEYS = ("coverURL", "coverUrl", "cover", "avatarURL", "avatar")
WORK_RATE_KEYS = ("bayesianRate", "averageRating", "rate", "score")
WORK_VOTE_KEYS = ("realVoteCount", "voteCount", "votes", "totalLikes")

MAX_ITEMS = 400          # 单次入库最多拆多少条
MAX_PAYLOAD = 1_000_000  # 单条载荷上限（1MB），超了只记形状不记正文
MAX_CONTENT_API = 600    # content_api 最多留多少条（超出丢最旧）


def _pick(d: dict, keys) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def _work_id(d: dict) -> str:
    return _pick(d, WORK_ID_KEYS)


def _is_work(d) -> bool:
    return (isinstance(d, dict) and bool(_work_id(d))
            and bool(_pick(d, WORK_NAME_KEYS) or _pick(d, WORK_SUMMARY_KEYS)
                     or d.get("tags")))


def _num(d: dict, keys) -> float | None:
    """从几个候选键里取第一个数字（评分 / 票数字段各接口叫法不同）"""
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v.strip())
            except ValueError:
                continue
    return None


def _norm_tags(v) -> list[str]:
    """标签可能是字符串数组，也可能是 [{name/key/tagName: ...}]"""
    out: list[str] = []
    if isinstance(v, str):
        return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
    if isinstance(v, list):
        for t in v:
            if isinstance(t, str):
                name = t
            elif isinstance(t, dict):
                name = _pick(t, ("name", "key", "tagName", "title", "label"))
            else:
                continue
            if name and name not in out:
                out.append(name)
    return out


def _walk_works(obj, out: list[dict], depth: int = 0) -> None:
    """递归找条目（有的接口把作品放在 data.list / data.items / data.character 里）"""
    if depth > 8 or len(out) >= MAX_ITEMS:
        return
    if isinstance(obj, dict):
        if _is_work(obj):
            out.append(obj)
            return
        for v in obj.values():
            _walk_works(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:MAX_ITEMS]:
            _walk_works(v, out, depth + 1)


def _upsert_work(item: dict, path: str, now: str) -> bool:
    cid = _work_id(item)
    if not cid:
        return False
    tags = _norm_tags(item.get("tags"))
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO works(character_id, name, summary, author_name, cover_url,
                          rate, votes, tags, raw, path, first_seen, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(character_id) DO UPDATE SET
            name=excluded.name, summary=excluded.summary, author_name=excluded.author_name,
            cover_url=excluded.cover_url, rate=excluded.rate, votes=excluded.votes,
            tags=excluded.tags, raw=excluded.raw, path=excluded.path,
            updated_at=excluded.updated_at
        """,
        (cid, _pick(item, WORK_NAME_KEYS), _pick(item, WORK_SUMMARY_KEYS)[:20000],
         _pick(item, WORK_AUTHOR_KEYS), _pick(item, WORK_COVER_KEYS),
         _num(item, WORK_RATE_KEYS),
         _num(item, WORK_VOTE_KEYS),
         json.dumps(tags, ensure_ascii=False),
         json.dumps(item, ensure_ascii=False), path, now, now),
    )
    conn.execute("DELETE FROM work_tags WHERE work_id=?", (cid,))
    for i, tag in enumerate(tags):
        conn.execute(
            "INSERT OR REPLACE INTO work_tags(work_id, tag, pos, updated_at) VALUES(?,?,?,?)",
            (cid, tag, i, now))
    return True


def _ingest_tag_catalog(data, now: str) -> int:
    """创作广场的标签字典：{tags: [{id, name, category, usageCount}]}"""
    if not (isinstance(data, dict) and isinstance(data.get("tags"), list)):
        return 0
    rows = [t for t in data["tags"] if isinstance(t, dict) and t.get("name")
            and not _is_work(t)]
    if not rows:
        return 0
    conn = get_conn()
    n = 0
    for t in rows[:500]:
        tag_id = str(t.get("id") or t.get("key") or t.get("name"))
        conn.execute(
            "INSERT INTO tag_catalog(tag_id, name, category, usage_count, updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(tag_id) DO UPDATE SET name=excluded.name,"
            " category=excluded.category, usage_count=excluded.usage_count,"
            " updated_at=excluded.updated_at",
            (tag_id, str(t.get("name")), str(t.get("category") or ""),
             int(t.get("usageCount") or 0) if isinstance(t.get("usageCount"), int) else 0, now))
        n += 1
    return n


def _shape_of(data, works: list[dict]) -> tuple[str, str, str]:
    """记录接口回包的形状：list / dict:list(容器) / dict1:x(单条) / object"""
    if isinstance(data, list):
        return "list", "", "{}"
    if isinstance(data, dict):
        if _is_work(data):
            return "object", "", "{}"
        for k, v in data.items():
            if isinstance(v, list) and any(_is_work(x) for x in v[:20]):
                return "dict:list", k, "{}"
        for k, v in data.items():
            if _is_work(v):
                return "dict1:", k, "{}"
        extras = {k: v for k, v in data.items()
                  if v is None or isinstance(v, (str, int, float, bool))}
        try:
            return "object", "", json.dumps(extras, ensure_ascii=False)
        except (TypeError, ValueError):  # noqa: PERF203
            return "object", "", "{}"
    return "object", "", "{}"


def ingest_content(path: str, url_key: str, payload) -> int:
    """把一次内容接口的回包拆进内容库（文字 + 标签），并记下离线还原需要的形状）"""
    data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
    now = now_iso()
    works: list[dict] = []
    _walk_works(data, works)
    n = 0
    for item in works[:MAX_ITEMS]:
        try:
            if _upsert_work(item, path, now):
                n += 1
        except sqlite3.Error:
            continue
    try:
        _ingest_tag_catalog(data, now)
    except sqlite3.Error:
        pass
    try:
        _ingest_creative(data, path, now)
    except sqlite3.Error:
        pass

    shape, container, extras = _shape_of(data, works)
    # 只有成功、还带得出内容的回包才配记形状：code!=0 / 空 data 会把详情接口的
    # 离线形状覆盖掉（线上对本地新建的作品回 code:7，就曾把 profile 离线形状冲掉）
    if isinstance(payload, dict) and payload.get("code") not in (0, None):
        return n
    if not works and not data:
        return n
    try:
        blob = json.dumps(data, ensure_ascii=False)
        if len(blob) > MAX_PAYLOAD:
            blob = None
    except (TypeError, ValueError):  # noqa: PERF203
        blob = None
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO content_api(url_key, path, shape, container, extras, payload, items, fetched_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(url_key) DO UPDATE SET path=excluded.path, shape=excluded.shape,
                container=excluded.container, extras=excluded.extras,
                payload=excluded.payload, items=excluded.items, fetched_at=excluded.fetched_at
            """,
            (url_key, path, shape, container, extras, blob, len(works), now),
        )
        total = conn.execute("SELECT COUNT(*) FROM content_api").fetchone()[0]
        if total > MAX_CONTENT_API:
            conn.execute(
                "DELETE FROM content_api WHERE url_key NOT IN"
                " (SELECT url_key FROM content_api ORDER BY fetched_at DESC LIMIT ?)",
                (MAX_CONTENT_API - 100,))
    except sqlite3.Error:
        pass
    return n


def _page_params(qs: str) -> tuple[int, int, int]:
    """从查询串解析分页参数 → (page, pageSize, limit)，缺省 (1, 0, 0)"""
    page, size, limit = 1, 0, 0
    for part in (qs or "").split("&"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            kl = k.lower()
            if kl == "page":
                page = max(1, int(v))
            elif kl in ("pagesize", "page_size", "size"):
                size = max(0, int(v))
            elif kl == "limit":
                limit = max(0, int(v))
        except ValueError:
            pass
    return page, size, limit


def _slice_page(items: list, qs: str) -> list:
    """按请求里的 page/pageSize/limit 切片。

    pageSize 优先（分页语义）；只有 limit 时按"取前 N 条"处理（探索流语义）。
    参数解析不出来或超出条数范围时原样返回，不会把列表清空。
    """
    page, size, limit = _page_params(qs)
    if size > 0:
        start = (page - 1) * size
        return items[start:start + size]
    if limit > 0:
        return items[:limit]
    return items


# 前端在调但内容库可能没抓过的列表类接口：只要 works 有货就用通用形状重建，
# 否则榜单页会拿到空列表（库里明明有几百条作品）。
_LIST_PATH_HINTS = ("list", "ranking", "discovery", "showcase", "recommendation",
                    "plaza", "gallery", "items", "hot", "new", "search")


def _looks_like_list(path: str) -> bool:
    low = str(path or "").lower()
    return any(h in low for h in _LIST_PATH_HINTS)


def _merge_payload_list(path: str, qs: str) -> dict | None:
    """把同一接口各页缓存的 dict 载荷合并成可分页整体，再按请求参数切片。

    有的接口（如 aiImage/gallery）载荷本身就是 {list,page,pageSize,total}，
    works 表里没有它的数据，只能从 payload 还原。缓存里存的 pageSize/sort
    与前端当前请求不一致时，整包回放会对不上分页，这里合并去重后重建。
    """
    rows = query("SELECT payload FROM content_api WHERE url_key LIKE ?"
                 " ORDER BY fetched_at DESC", (path + "%",))
    merged: list = []
    seen: set = set()
    base: dict | None = None
    list_key = "list"
    for r in rows or []:
        try:
            p = json.loads(r["payload"]) if r.get("payload") else None
        except (TypeError, ValueError):  # noqa: PERF203
            p = None
        if not isinstance(p, dict):
            continue
        key = next((k for k in ("list", "items", "records", "rows", "works")
                    if isinstance(p.get(k), list)), None)
        if not key:
            continue
        if base is None:
            base, list_key = dict(p), key
        for it in p[key]:
            if not isinstance(it, dict):
                continue
            idv = it.get("ID") or it.get("id")
            dedup = str(idv) if idv else json.dumps(it, ensure_ascii=False)[:300]
            if dedup in seen:
                continue
            seen.add(dedup)
            merged.append(it)
    if base is None or not merged:
        return None
    page, size, limit = _page_params(qs)
    data = base
    data[list_key] = _slice_page(merged, qs)
    if isinstance(data.get("total"), (int, float)):
        data["total"] = max(int(data["total"]), len(merged))
    else:
        data["total"] = len(merged)
    if size > 0:
        data["page"] = page
        data["pageSize"] = size
        data["hasMore"] = page * size < len(merged)
    return data


def _rebuild(path: str, qs: str) -> dict | None:
    """没有精确载荷时：按该接口记下的形状，用 works 表重建 data"""
    # 同一个 path 可能留下多条形状记录（带 query 的 url_key 各一条），
    # 优先取有明确容器的那条，别被后来的空壳 "object" 盖住
    # 注意：镜像入库时 url_key 是"带 query 的完整 URL"，前端请求可能带不同
    # 的 pageSize/gender → 这里用裸 path 前缀匹配，任意 query 变体都能还原
    meta = one("SELECT * FROM content_api WHERE url_key LIKE ?"
               " ORDER BY (shape='object' AND container='') ASC, fetched_at DESC LIMIT 1",
               (path + "%",))
    list_like = _looks_like_list(path)
    if meta is None:
        # 前端在调、但内容库没抓过形状的列表接口（如 ranking/list、
        # personalizedRecommendation/list）：works 有货就用通用形状重建，
        # 否则页面拿到空列表，库里几百条作品等于白存
        if not list_like:
            return None
        meta = {"shape": "dict:list", "container": "list", "extras": "{}"}
    rows = query(
        "SELECT * FROM works WHERE path LIKE ?"
        " ORDER BY updated_at DESC, character_id",
        (path + "%",))
    if not rows and list_like:
        rows = query("SELECT * FROM works ORDER BY updated_at DESC, character_id")
    if not rows:
        return None
    # 同一作品会以不同 path 重复入库，按 character_id 去重，
    # 否则重建出来的榜单里全是重复卡片，分页也对不上
    seen: set = set()
    uniq: list = []
    for r in rows:
        cid = r.get("character_id")
        if cid:
            if cid in seen:
                continue
            seen.add(cid)
        uniq.append(r)
    items = []
    for r in uniq:
        try:
            items.append(json.loads(r["raw"]) if r.get("raw") else {})
        except (TypeError, ValueError):  # noqa: PERF203
            continue
    if not items:
        return None
    shape, container = meta.get("shape") or "list", meta.get("container") or ""
    try:
        extras = json.loads(meta.get("extras") or "{}")
    except (TypeError, ValueError):  # noqa: PERF203
        extras = {}

    pool = len(items)                      # 分页前的总条数
    page, size, limit = _page_params(qs)
    page_items = _slice_page(items, qs)

    if shape == "list":
        data = page_items
    elif shape.startswith("dict:"):
        data = dict(extras) if isinstance(extras, dict) else {}
        if isinstance(data.get("total"), (int, float)):
            data["total"] = max(int(data["total"]), pool)
        else:
            data["total"] = pool
        data[container or "items"] = page_items
        if size > 0:
            data["page"] = page
            data["pageSize"] = size
            data["hasMore"] = page * size < pool
        elif limit > 0:
            data["hasMore"] = limit < pool
    elif shape.startswith("dict1:"):
        want = ""
        for part in (qs or "").split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.lower() in ("characterid", "character_id", "id"):
                    want = v
        # 先在该接口抓过的条目里找；找不到就整库按 characterID 找（否则会串到别的角色）
        picked = next((r for r in rows if want and r["character_id"] == want), None)
        if picked is None and want:
            picked = one("SELECT * FROM works WHERE character_id=? LIMIT 1", (want,))
        if picked is None:
            picked = rows[0]
        try:
            single = json.loads(picked["raw"]) if picked.get("raw") else {}
        except (TypeError, ValueError):  # noqa: PERF203
            single = {}
        # 详情接口用 name / summary / coverURL，榜单位用 characterName / characterName 封面，
        # 这里把两套叫法补齐，前端怎么读都有字
        single = dict(single)
        single.pop("characterID", None)
        single["characterId"] = single.get("characterId") or picked["character_id"]
        if not single.get("name"):
            single["name"] = single.get("characterName") or single.get("title") or ""
        if not single.get("characterName"):
            single["characterName"] = single.get("name") or ""
        if not single.get("summary"):
            single["summary"] = single.get("description") or single.get("intro") or ""
        if not single.get("coverURL"):
            single["coverURL"] = (single.get("cover") or single.get("avatarURL")
                                  or single.get("coverUrl") or "")
        try:
            stored_tags = json.loads(picked.get("tags") or "[]")
        except (TypeError, ValueError):  # noqa: PERF203
            stored_tags = []
        if not isinstance(single.get("tags"), list) or not single.get("tags"):
            single["tags"] = stored_tags
        data = dict(extras) if isinstance(extras, dict) else {}
        data[container or "data"] = single
    else:
        data = dict(extras) if isinstance(extras, dict) else {}
        data.update({k: v for k, v in (items[0] or {}).items()
                     if k not in ("description", "firstMes")})
    return {"code": 0, "msg": "本地内容库", "data": data, "local": True}


# 探索 / 首页列表接口：发布到公开库的本地作品会注入这些列表，让新作在站内可见
_EXPLORE_LIST_KEYS = ("home_personalized", "ranking/new", "ranking/hot", "hot_chart")


def _local_published_items() -> list[dict]:
    """公开库里用户本地发布的已发布作品（path='local/published'）"""
    rows = query("SELECT raw FROM works WHERE path=? ORDER BY updated_at DESC LIMIT 30",
                 (PUBLIC_WORK_PATH,))
    items: list[dict] = []
    for r in rows:
        try:
            it = json.loads(r["raw"])
        except (TypeError, ValueError):  # noqa: PERF203
            continue
        if isinstance(it, dict):
            items.append(it)
    return items


def content_fallback(url_key: str, path: str, qs: str = "") -> dict | None:
    """线上不通 → 用内容库还原这个接口的回包（文字 + 标签都还在）"""
    row = one("SELECT payload FROM content_api WHERE url_key=?", (url_key,))
    data = None
    if row and row.get("payload"):
        try:
            data = json.loads(row["payload"])
        except (TypeError, ValueError):  # noqa: PERF203
            data = None
    # 精确载荷是按"抓取时那一页的 page/pageSize"存的。请求参数一旦不同
    # （前端换了 pageSize、或翻到别的页），整包直接回给前端就会对不上分页。
    # 先尝试把该接口各页载荷合并去重再按请求切片（works 表没存这类接口的条目，
    # 如 aiImage/gallery，只能走这条）；不行再退回 works 全量重建。
    stored_qs = url_key.split("?", 1)[1] if "?" in url_key else ""
    if data is not None and _page_params(qs) != _page_params(stored_qs):
        merged = _merge_payload_list(path, qs)
        if merged is not None:
            data = merged
        else:
            rebuilt = _rebuild(path, qs)
            if rebuilt is not None:
                data = rebuilt["data"]
    if data is None:
        merged = _merge_payload_list(path, qs)
        if merged is not None:
            data = merged
        else:
            rebuilt = _rebuild(path, qs)
            if rebuilt is None:
                return None
            data = rebuilt["data"]
    # 探索 / 首页列表接口：把本地发布的公开作品注入列表头部（"最新发布"）
    if isinstance(data, (dict, list)) and any(k in path for k in _EXPLORE_LIST_KEYS):
        local = _local_published_items()
        if local:
            if isinstance(data, list):
                data = local + [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict):
                for k in ("items", "list", "records", "works", "rows"):
                    if isinstance(data.get(k), list):
                        data[k] = local + [x for x in data[k] if isinstance(x, dict)]
                        break
    return {"code": 0, "msg": "本地内容库", "data": data, "local": True}


# --------------------------------------------------------------- 静态资源入库（html/css 从数据库调用）
# mirror.py 的 "assets" 数据面把站点样式抓进这张表；服务器出 css 时先查库，命中就从库回。
ASSETS_TABLE = "mirror_assets"


def _ensure_assets():
    conn = get_conn()
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {ASSETS_TABLE} (
            path         TEXT PRIMARY KEY,
            kind         TEXT NOT NULL DEFAULT 'css',
            data         BLOB,
            content_type TEXT NOT NULL DEFAULT 'text/css',
            size         INTEGER NOT NULL DEFAULT 0,
            fetched_at   TEXT NOT NULL
        )""")
    conn.commit()


def asset_save(path: str, data: bytes, content_type: str = "text/css",
               kind: str = "css") -> None:
    _ensure_assets()
    conn = get_conn()
    conn.execute(f"""
        INSERT INTO {ASSETS_TABLE}(path, kind, data, content_type, size, fetched_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, data=excluded.data,
            content_type=excluded.content_type, size=excluded.size, fetched_at=excluded.fetched_at""",
        (path, kind, data, content_type, len(data or b""), now_iso()))
    conn.commit()


def asset_get(path: str):
    """按路径取静态资源：返回 (data_bytes, content_type) 或 None"""
    _ensure_assets()
    row = one(f"SELECT data, content_type FROM {ASSETS_TABLE} WHERE path=?", (path,))
    if row and row.get("data"):
        return row["data"], row.get("content_type") or "text/css"
    return None


# ---------------------------------------------------------------- 角色详情（/profile）
CHAR_TABLE = "character_details"


def _ensure_characters():
    conn = get_conn()
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {CHAR_TABLE} (
            character_id TEXT PRIMARY KEY,
            data         TEXT,            -- findNarratiumCharacterByCharacterID 完整回包 JSON
            name         TEXT DEFAULT '',
            avatar_url   TEXT DEFAULT '',
            size         INTEGER NOT NULL DEFAULT 0,
            fetched_at   TEXT NOT NULL
        )""")
    conn.commit()


def character_save(character_id: str, payload: dict) -> int:
    """存入角色详情。返回写入字节数。"""
    _ensure_characters()
    raw = json.dumps(payload, ensure_ascii=False)
    ch = payload.get("data", {}).get("character") or {}
    name = ch.get("name") or ch.get("title") or ""
    avatar = ch.get("avatarURL") or ""
    size = len(raw.encode("utf-8"))
    conn = get_conn()
    conn.execute(f"""
        INSERT INTO {CHAR_TABLE}(character_id, data, name, avatar_url, size, fetched_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(character_id) DO UPDATE SET data=excluded.data, name=excluded.name,
            avatar_url=excluded.avatar_url, size=excluded.size, fetched_at=excluded.fetched_at""",
        (character_id, raw, name, avatar, size, now_iso()))
    conn.commit()
    return size


def character_get(character_id: str):
    _ensure_characters()
    row = one(f"SELECT data, name, avatar_url, fetched_at FROM {CHAR_TABLE} WHERE character_id=?", (character_id,))
    if row and row.get("data"):
        try:
            return json.loads(row["data"])
        except (TypeError, ValueError):
            return None
    return None


def list_works(limit: int = 100, offset: int = 0, q: str = "") -> list[dict]:
    """控制台用：内容库条目（可按名称 / 简介搜）"""
    limit = max(1, min(limit, 1000))
    offset = max(0, min(offset, 100000))
    if q:
        like = f"%{q[:60]}%"
        rows = query(
            "SELECT character_id, name, summary, author_name, cover_url, rate, votes,"
            " tags, path, updated_at FROM works WHERE name LIKE ? OR summary LIKE ?"
            " OR character_id=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (like, like, q, limit, offset))
    else:
        rows = query(
            "SELECT character_id, name, summary, author_name, cover_url, rate, votes,"
            " tags, path, updated_at FROM works ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset))
    for r in rows:
        try:
            r["tags"] = json.loads(r.get("tags") or "[]")
        except (TypeError, ValueError):  # noqa: PERF203
            r["tags"] = []
    return rows


def top_tags(limit: int = 100) -> list[dict]:
    return query(
        "SELECT tag, COUNT(*) AS works, MAX(updated_at) AS updated_at FROM work_tags"
        " GROUP BY tag ORDER BY works DESC, tag LIMIT ?", (max(1, min(limit, 1000)),))


def content_apis() -> list[dict]:
    return query(
        "SELECT path, shape, items, fetched_at, length(payload) AS payload_bytes"
        " FROM content_api GROUP BY path ORDER BY fetched_at DESC LIMIT 100")


# --------------------------------------------------------------- 创意广场（商品 / 源码 / 购买 / 评论）
#
# 页面上能看到的一切都在本地：
#   creative_items       商品（列表字段 + 详情 + 开源源码 promptContent）
#   creative_user_items  购买 / 试用记录（聊天输入栏「创意库」读它）
#   creative_comments    评论（线上抓一次落库，本地也能发）
#   creative_votes       点赞 / 点踩
#   creative_purchases   购买流水（含幂等键）
#   creative_bindings    装备到会话 / 角色
#   creative_defaults    全局默认装备

CREATIVE_PREFIX = "/api/gva/creativePlaza"
CREATIVE_STATUS_ACTIVE = {"active", "on_sale", "published", "listed", "online", "1", "在售"}
CREATIVE_STATUS_INACTIVE = {"inactive", "delisted", "off_sale", "unlisted", "0", "已下架"}


def _creative_id(d: dict) -> int:
    for k in ("id", "ID", "creativeItemId", "creative_item_id", "CreativeItemId"):
        v = d.get(k)
        if isinstance(v, bool):
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return 0


def _is_creative_item(d) -> bool:
    if not isinstance(d, dict):
        return False
    if not isinstance(d.get("itemType") or d.get("item_type"), str):
        return False
    if not (d.get("title") or d.get("name")):
        return False
    return _creative_id(d) > 0


def _creative_status(v) -> str:
    if isinstance(v, bool):
        return "active" if v else "inactive"
    if isinstance(v, (int, float)):
        return "active" if int(v) == 1 else "inactive" if int(v) == 0 else "pending_review"
    s = str(v or "").strip().lower().replace("-", "_")
    if not s:
        return "pending_review"
    if s in CREATIVE_STATUS_ACTIVE:
        return "active"
    if s in CREATIVE_STATUS_INACTIVE:
        return "inactive"
    return s


def _walk_creative(obj, out: list[dict], depth: int = 0) -> None:
    if depth > 8 or len(out) >= MAX_ITEMS:
        return
    if isinstance(obj, dict):
        if _is_creative_item(obj):
            out.append(obj)
            # 用户创意库是 { id, creativeItem: {...} }，把内层也收下
        for k in ("creativeItem", "creative_item", "item", "items", "list", "rows", "data"):
            if k in obj:
                _walk_creative(obj[k], out, depth + 1)
        return
    if isinstance(obj, list):
        for v in obj[:MAX_ITEMS]:
            _walk_creative(v, out, depth + 1)


def _creative_tags(v) -> list[str]:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (TypeError, ValueError):
            v = [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
    return _norm_tags(v)


def _creative_bool(v) -> int:
    if isinstance(v, str):
        return 1 if v.strip().lower() in ("1", "true", "yes", "on") else 0
    return 1 if v else 0


def _creative_float(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _creative_upsert(item: dict, source: str = "remote", now: str | None = None) -> int:
    """把一条商品写进本地库（列表 / 详情 / 用户创意库内层都能直接喂）"""
    iid = _creative_id(item)
    if not iid:
        return 0
    now = now or now_iso()
    tags = _creative_tags(item.get("tags"))
    prompt = item.get("promptContent")
    if prompt is None:
        prompt = item.get("prompt_content")
    prompt_data = item.get("promptData")
    if prompt_data is None:
        prompt_data = item.get("prompt_data")
    paired = item.get("pairedCssPrompt")
    if paired is None:
        paired = item.get("paired_css_prompt")
    if not paired:
        css = item.get("pairedCss") or item.get("paired_css")
        if isinstance(css, dict):
            paired = css.get("promptContent") or css.get("prompt_content")
    try:
        prompt_blob = json.dumps(prompt_data, ensure_ascii=False) if prompt_data else None
    except (TypeError, ValueError):
        prompt_blob = None
    try:
        raw_blob = json.dumps(item, ensure_ascii=False)
    except (TypeError, ValueError):
        raw_blob = "{}"
    def s(key, alt=None):
        v = item.get(key, item.get(alt) if alt else None)
        return None if v is None else str(v)
    def n(key, alt=None):
        v = item.get(key, item.get(alt) if alt else None)
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    price = n("price")
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO creative_items(
            id, item_type, title, description, price, tags, is_open_source, allow_trial,
            author_id, author_name, author_avatar, sales_count, like_count, dislike_count,
            approval_rate, tip_count, tip_total, heat_score, heat_tier, is_editor_choice,
            status, version, prompt_content, prompt_data, extracted_post_prompt,
            paired_css_prompt, bound_work_id, bound_work_name, reference_work_id,
            raw, source, first_seen, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            item_type=COALESCE(NULLIF(excluded.item_type,''), creative_items.item_type),
            title=COALESCE(NULLIF(excluded.title,''), creative_items.title),
            description=COALESCE(NULLIF(excluded.description,''), creative_items.description),
            price=COALESCE(excluded.price, creative_items.price),
            tags=CASE WHEN excluded.tags='[]' THEN creative_items.tags ELSE excluded.tags END,
            is_open_source=MAX(excluded.is_open_source, creative_items.is_open_source),
            allow_trial=MAX(excluded.allow_trial, creative_items.allow_trial),
            author_id=COALESCE(NULLIF(excluded.author_id,0), creative_items.author_id),
            author_name=COALESCE(NULLIF(excluded.author_name,''), creative_items.author_name),
            author_avatar=COALESCE(NULLIF(excluded.author_avatar,''), creative_items.author_avatar),
            sales_count=MAX(excluded.sales_count, creative_items.sales_count),
            like_count=MAX(excluded.like_count, creative_items.like_count),
            dislike_count=MAX(excluded.dislike_count, creative_items.dislike_count),
            approval_rate=COALESCE(excluded.approval_rate, creative_items.approval_rate),
            tip_count=MAX(excluded.tip_count, creative_items.tip_count),
            tip_total=MAX(excluded.tip_total, creative_items.tip_total),
            heat_score=MAX(excluded.heat_score, creative_items.heat_score),
            heat_tier=MAX(excluded.heat_tier, creative_items.heat_tier),
            is_editor_choice=MAX(excluded.is_editor_choice, creative_items.is_editor_choice),
            status=CASE WHEN creative_items.source='local' THEN creative_items.status
                        ELSE excluded.status END,
            version=MAX(excluded.version, creative_items.version),
            prompt_content=COALESCE(NULLIF(excluded.prompt_content,''), creative_items.prompt_content),
            prompt_data=COALESCE(excluded.prompt_data, creative_items.prompt_data),
            extracted_post_prompt=COALESCE(NULLIF(excluded.extracted_post_prompt,''),
                                           creative_items.extracted_post_prompt),
            paired_css_prompt=COALESCE(NULLIF(excluded.paired_css_prompt,''),
                                       creative_items.paired_css_prompt),
            bound_work_id=COALESCE(NULLIF(excluded.bound_work_id,''), creative_items.bound_work_id),
            bound_work_name=COALESCE(NULLIF(excluded.bound_work_name,''), creative_items.bound_work_name),
            reference_work_id=COALESCE(NULLIF(excluded.reference_work_id,''),
                                       creative_items.reference_work_id),
            raw=CASE WHEN excluded.raw='{}' THEN creative_items.raw ELSE excluded.raw END,
            updated_at=excluded.updated_at
        """,
        (iid, s("itemType", "item_type"), s("title"), s("description"),
         price, json.dumps(tags, ensure_ascii=False),
         _creative_bool(item.get("isOpenSource", item.get("is_open_source"))),
         _creative_bool(item.get("allowTrial", item.get("allow_trial"))),
         n("authorId", "author_id") or 0, s("authorName", "author_name"),
         s("authorAvatar", "author_avatar"),
         n("salesCount", "sales_count") or 0, n("likeCount", "like_count") or 0,
         n("dislikeCount", "dislike_count") or 0,
         _creative_float(item.get("approvalRate", item.get("approval_rate"))),
         n("tipCount", "tip_count") or 0, n("tipTotal", "tip_total") or 0,
         _creative_float(item.get("heatScore", item.get("heat_score"))),
         n("heatTier", "heat_tier") or 0,
         _creative_bool(item.get("isEditorChoice", item.get("is_editor_choice"))),
         _creative_status(item.get("status", "active")),
         n("version") or 1,
         prompt, prompt_blob, s("extractedPostPrompt", "extracted_post_prompt"),
         paired, s("boundWorkId", "bound_work_id"), s("boundWorkName", "bound_work_name"),
         s("referenceWorkId", "reference_work_id"),
         raw_blob, source, now, now),
    )
    return iid


def _ingest_creative(data, path: str, now: str) -> int:
    """内容接口回包 → 创意广场商品 / 评论 / 分类目录"""
    n = 0
    if "/creativePlaza" not in path:
        return n
    items: list[dict] = []
    _walk_creative(data, items)
    for item in items:
        try:
            if _creative_upsert(item, "remote", now):
                n += 1
        except sqlite3.Error:
            continue
    if path.endswith("/catalog") and isinstance(data, dict):
        try:
            set_config("creative_catalog", data)
        except sqlite3.Error:
            pass
    if "/comments" in path:
        iid = 0
        for part in path.split("/"):
            if part.isdigit():
                iid = int(part)
        rows = data.get("items") if isinstance(data, dict) else None
        if iid and isinstance(rows, list):
            for c in rows[:200]:
                if not isinstance(c, dict) or not c.get("content"):
                    continue
                try:
                    conn = get_conn()
                    conn.execute(
                        "INSERT INTO creative_comments(item_id, remote_id, user_id, user_name,"
                        " user_avatar, content, rating, reply_content, reply_at, source,"
                        " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(item_id, remote_id) DO UPDATE SET"
                        " user_name=excluded.user_name, user_avatar=excluded.user_avatar,"
                        " content=excluded.content, rating=excluded.rating,"
                        " reply_content=excluded.reply_content, reply_at=excluded.reply_at,"
                        " updated_at=excluded.updated_at",
                        (iid, c.get("id"), c.get("userId") or 0, c.get("userName") or "",
                         c.get("userAvatar") or "", c.get("content") or "",
                         int(c.get("rating") or 0), c.get("replyContent"),
                         c.get("replyAt"), "remote", c.get("createdAt") or now, now))
                except (sqlite3.Error, TypeError, ValueError):
                    continue
    return n


def creative_item_row(item_id: int) -> dict | None:
    return one("SELECT * FROM creative_items WHERE id=?", (int(item_id),))


def creative_item_public(row: dict, include_prompt: bool = True) -> dict:
    """DB 行 → 前端商品对象（保留线上原字段，再覆盖本地字段）"""
    try:
        base = json.loads(row.get("raw") or "{}")
    except (TypeError, ValueError):
        base = {}
    if not isinstance(base, dict):
        base = {}
    try:
        tags = json.loads(row.get("tags") or "[]")
    except (TypeError, ValueError):
        tags = []
    item = {
        **base,
        "id": row["id"],
        "ID": row["id"],
        "itemType": row.get("item_type") or base.get("itemType") or "reply_example",
        "title": row.get("title") or "",
        "description": row.get("description") or "",
        "price": int(row.get("price") or 0),
        "tags": tags if isinstance(tags, list) else [],
        "isOpenSource": bool(row.get("is_open_source")),
        "allowTrial": bool(row.get("allow_trial")),
        "authorId": int(row.get("author_id") or 0),
        "authorName": row.get("author_name") or "",
        "authorAvatar": row.get("author_avatar") or "",
        "salesCount": int(row.get("sales_count") or 0),
        "likeCount": int(row.get("like_count") or 0),
        "dislikeCount": int(row.get("dislike_count") or 0),
        "approvalRate": float(row.get("approval_rate") or 0),
        "tipCount": int(row.get("tip_count") or 0),
        "tipTotal": int(row.get("tip_total") or 0),
        "heatScore": float(row.get("heat_score") or 0),
        "heatTier": int(row.get("heat_tier") or 0),
        "isEditorChoice": bool(row.get("is_editor_choice")),
        "status": row.get("status") or "active",
        "version": int(row.get("version") or 1),
        "createdAt": base.get("createdAt") or row.get("first_seen") or "",
        "updatedAt": row.get("updated_at") or "",
        "boundWorkId": row.get("bound_work_id") or base.get("boundWorkId"),
        "boundWorkName": row.get("bound_work_name") or base.get("boundWorkName"),
    }
    if row.get("prompt_content"):
        item["promptContent"] = row["prompt_content"]
    if row.get("prompt_data"):
        try:
            item["promptData"] = json.loads(row["prompt_data"])
        except (TypeError, ValueError):
            pass
    if row.get("extracted_post_prompt"):
        item["extractedPostPrompt"] = row["extracted_post_prompt"]
    if row.get("paired_css_prompt"):
        item["pairedCssPrompt"] = row["paired_css_prompt"]
    if not include_prompt:
        for k in ("promptContent", "promptData", "extractedPostPrompt",
                  "pairedCssPrompt", "pairedCss"):
            item.pop(k, None)
    return item


def creative_items_page(limit: int = 100, offset: int = 0, q: str = "",
                        open_only: bool = False) -> list[dict]:
    """控制台用：商品列表（不含源码正文）"""
    limit = max(1, min(limit, 1000))
    offset = max(0, min(offset, 100000))
    where, args = [], []
    if q:
        where.append("(title LIKE ? OR description LIKE ? OR author_name LIKE ? OR id=?)")
        like = f"%{q[:80]}%"
        args += [like, like, like, q if q.isdigit() else -1]
    if open_only:
        where.append("is_open_source=1")
    sql = ("SELECT id, item_type, title, description, price, tags, is_open_source,"
           " allow_trial, author_id, author_name, sales_count, like_count, status, version,"
           " length(prompt_content) AS source_bytes, source, updated_at"
           " FROM creative_items")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
    rows = query(sql, tuple(args + [limit, offset]))
    for r in rows:
        try:
            r["tags"] = json.loads(r.get("tags") or "[]")
        except (TypeError, ValueError):
            r["tags"] = []
    return rows


def creative_counts() -> dict:
    def c(sql):
        row = one(sql)
        return int(row["c"] or 0) if row else 0
    return {
        "items": c("SELECT COUNT(*) AS c FROM creative_items"),
        "open_source": c("SELECT COUNT(*) AS c FROM creative_items WHERE is_open_source=1"),
        "with_source": c("SELECT COUNT(*) AS c FROM creative_items"
                         " WHERE prompt_content IS NOT NULL AND prompt_content<>''"),
        "comments": c("SELECT COUNT(*) AS c FROM creative_comments"),
        "purchases": c("SELECT COUNT(*) AS c FROM creative_purchases"),
        "user_items": c("SELECT COUNT(*) AS c FROM creative_user_items"),
        "last_sync": (one("SELECT MAX(updated_at) AS c FROM creative_items") or {}).get("c") or "",
    }


def _creative_filter_sql(params: dict) -> tuple[str, list]:
    where, args = ["status='active'"], []
    item_type = (params.get("itemType") or params.get("item_type") or "").strip()
    if item_type:
        where.append("item_type=?")
        args.append(item_type)
    keyword = (params.get("keyword") or "").strip()
    if keyword:
        where.append("(title LIKE ? OR description LIKE ? OR author_name LIKE ?)")
        like = f"%{keyword[:80]}%"
        args += [like, like, like]
    tag = (params.get("tag") or "").strip()
    if tag:
        where.append("tags LIKE ?")
        args.append(f'%"{tag}"%')
    bound = (params.get("boundWorkId") or params.get("bound_work_id") or "").strip()
    if bound:
        where.append("bound_work_id=?")
        args.append(bound)
    return " AND ".join(where), args


def creative_list(params: dict) -> dict:
    """公开列表：筛选 / 排序 / 分页全在 SQL 里做"""
    page = max(1, int(params.get("page") or 1))
    size = max(1, min(int(params.get("pageSize") or params.get("page_size") or 24), 100))
    where, args = _creative_filter_sql(params)
    tags = params.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    for t in tags[:6]:
        where += " AND tags LIKE ?"
        args.append(f'%"{t}"%')
    sort = (params.get("sortBy") or params.get("sort_by") or "latest").lower()
    order = {
        "latest": "updated_at DESC, id DESC",
        "newest": "updated_at DESC, id DESC",
        "sales": "sales_count DESC, id DESC",
        "heat": "heat_score DESC, id DESC",
        "hot": "heat_score DESC, id DESC",
        "rating": "approval_rate DESC, id DESC",
        "price_asc": "price ASC, id DESC",
        "price_desc": "price DESC, id DESC",
    }.get(sort, "updated_at DESC, id DESC")
    total = int((one(f"SELECT COUNT(*) AS c FROM creative_items WHERE {where}", tuple(args)) or {}).get("c") or 0)
    rows = query(
        f"SELECT * FROM creative_items WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        tuple(args + [size, (page - 1) * size]))
    items = [creative_item_public(r, include_prompt=False) for r in rows]
    return {"items": items, "total": total, "page": page, "pageSize": size,
            "hasMore": page * size < total}


def creative_by_work(work_id: str, params: dict) -> dict:
    p = dict(params)
    p["boundWorkId"] = work_id
    return creative_list(p)


def creative_detail(item_id: int, uid: int | None) -> dict | None:
    row = creative_item_row(item_id)
    if not row:
        return None
    owned = creative_owned(uid, item_id) if uid else None
    author = bool(uid and int(row.get("author_id") or 0) == int(uid))
    include = bool(row.get("is_open_source")) or bool(owned) or author
    item = creative_item_public(row, include_prompt=include)
    return {"item": item, "hasPurchased": bool(owned and owned.get("acquisition_type") == "purchased")}


def creative_missing_source_ids(limit: int = 200) -> list[int]:
    """开源但源码还没落库的商品 id（同步时按需补抓 /prompt）"""
    rows = query("SELECT id FROM creative_items WHERE is_open_source=1"
                 " AND (prompt_content IS NULL OR prompt_content='')"
                 " ORDER BY id LIMIT ?", (int(limit),))
    return [int(r["id"]) for r in rows]


def creative_save_prompt(item_id: int, content=None, data=None, paired=None) -> bool:
    """只补源码相关字段，不动 raw。

    源码在独立的 /items/:id/prompt 接口里，详情回包通常不带；
    用只含 prompt 的对象走 _creative_upsert 会把 raw 覆盖成小对象，所以单独更新。
    """
    sets, vals = [], []
    if isinstance(content, str) and content.strip():
        sets.append("prompt_content=?")
        vals.append(content)
    if data:
        try:
            blob = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            blob = None
        if blob and blob != "null":
            sets.append("prompt_data=?")
            vals.append(blob)
    if isinstance(paired, str) and paired.strip():
        sets.append("paired_css_prompt=?")
        vals.append(paired)
    if not sets:
        return False
    vals.append(int(item_id))
    execute(f"UPDATE creative_items SET {','.join(sets)} WHERE id=?", tuple(vals))
    return True


def creative_prompt(item_id: int, uid: int | None) -> dict | None:
    row = creative_item_row(item_id)
    if not row:
        return None
    owned = creative_owned(uid, item_id) if uid else None
    author = bool(uid and int(row.get("author_id") or 0) == int(uid))
    if not (row.get("is_open_source") or owned or author):
        return None
    item = creative_item_public(row, include_prompt=True)
    return {"promptContent": item.get("promptContent"),
            "promptData": item.get("promptData"),
            "pairedCssPrompt": item.get("pairedCssPrompt")}


def creative_tags() -> list[dict]:
    rows = query("SELECT tag_id, name, category, usage_count, updated_at"
                 " FROM tag_catalog ORDER BY usage_count DESC, name LIMIT 500")
    return [{"id": int(r["tag_id"]) if str(r["tag_id"]).isdigit() else i + 1,
             "name": r["name"], "category": r["category"] or "default",
             "count": int(r["usage_count"] or 0),
             "usageCount": int(r["usage_count"] or 0)} for i, r in enumerate(rows)]


def creative_catalog() -> dict:
    cfg = get_config("creative_catalog") or {}
    return cfg if isinstance(cfg, dict) else {}


def creative_editor_choices() -> list[dict]:
    rows = query("SELECT * FROM creative_items WHERE status='active' AND is_editor_choice=1"
                 " ORDER BY heat_score DESC, id DESC LIMIT 24")
    return [creative_item_public(r, include_prompt=False) for r in rows]


def creative_spotlight(limit: int = 12) -> dict:
    rows = query("SELECT * FROM creative_items WHERE status='active'"
                 " ORDER BY heat_score DESC, sales_count DESC, id DESC LIMIT ?", (limit,))
    items = [creative_item_public(r, include_prompt=False) for r in rows]
    return {"items": items, "total": len(items)}


def creative_owned(uid: int | None, item_id: int) -> dict | None:
    if not uid:
        return None
    return one("SELECT * FROM creative_user_items WHERE user_id=? AND item_id=?"
               " ORDER BY (acquisition_type='purchased') DESC, id DESC LIMIT 1",
               (int(uid), int(item_id)))


def _creative_user_item_view(r: dict, include_prompt: bool = True) -> dict:
    item_row = creative_item_row(r["item_id"])
    item = creative_item_public(item_row, include_prompt=include_prompt) if item_row else {}
    trial_active = (r.get("acquisition_type") == "trial"
                    and r.get("status") == "trial_active"
                    and int(r.get("trial_rounds_remaining") or 0) > 0)
    status = r.get("status") or "disabled"
    if trial_active:
        status = "trial_active"
    elif r.get("acquisition_type") == "trial" and status == "trial_active":
        status = "trial_expired"
    return {
        "id": r["id"], "ID": r["id"], "userId": r.get("user_id") or 0,
        "creativeItemId": r["item_id"], "itemType": item.get("itemType"),
        "acquisitionType": r.get("acquisition_type") or "purchased",
        "acquiredAt": r.get("created_at") or "",
        "isEnabled": bool(r.get("is_enabled")),
        "status": status,
        "trialRoundsTotal": int(r.get("trial_rounds_total") or 0),
        "trialRoundsRemaining": int(r.get("trial_rounds_remaining") or 0),
        "trialTimesUsed": int(r.get("trial_times_used") or 0),
        "currentVersion": int(r.get("current_version") or 1),
        "usageRoundsCount": int(r.get("usage_rounds_count") or 0),
        "voteUnlocked": bool(r.get("usage_rounds_count") and int(r["usage_rounds_count"]) >= 5),
        "isAuthor": False,
        "creativeItem": item,
    }


def creative_user_items(uid: int | None) -> list[dict]:
    if not uid:
        return []
    rows = query("SELECT * FROM creative_user_items WHERE user_id=?"
                 " ORDER BY id DESC", (int(uid),))
    return [_creative_user_item_view(r) for r in rows]


def creative_purchase(uid: int | None, item_id: int, idem: str = "") -> tuple[dict | None, str]:
    if not uid:
        return None, "请先登录后再购买"
    row = creative_item_row(item_id)
    if not row:
        return None, "商品不存在"
    if str(row.get("status") or "") != "active":
        return None, "商品已下架"
    if idem:
        existed = one("SELECT * FROM creative_purchases WHERE idempotency_key=? AND user_id=?",
                      (idem[:120], int(uid)))
        if existed:
            return {"success": True, "userItemId": existed["id"],
                    "purchaseId": existed["id"], "creativeItemId": item_id,
                    "remainingPoints": existed["points_left"]}, ""
    owned = one("SELECT * FROM creative_user_items WHERE user_id=? AND item_id=?"
                " AND acquisition_type='purchased'", (int(uid), int(item_id)))
    if owned:
        return {"success": True, "userItemId": owned["id"], "purchaseId": owned["id"],
                "creativeItemId": item_id, "remainingPoints": points_balance(int(uid))}, ""
    price = max(0, int(row.get("price") or 0))
    balance = points_balance(int(uid))
    if balance < price:
        return None, f"积分不足：需要 {price}，当前 {balance}"
    if price:
        balance = add_points(int(uid), -price)
    now = now_iso()
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO creative_purchases(user_id, item_id, price, points_left,"
        " idempotency_key, created_at) VALUES(?,?,?,?,?,?)",
        (int(uid), int(item_id), price, balance, (idem or None), now))
    pid = cur.lastrowid
    conn.execute(
        "INSERT INTO creative_user_items(user_id, item_id, acquisition_type, status,"
        " is_enabled, current_version, created_at, updated_at)"
        " VALUES(?,?, 'purchased', 'enabled', 1, ?, ?, ?)"
        " ON CONFLICT(user_id, item_id, acquisition_type) DO UPDATE SET"
        " status='enabled', is_enabled=1, updated_at=excluded.updated_at",
        (int(uid), int(item_id), int(row.get("version") or 1), now, now))
    conn.execute("UPDATE creative_items SET sales_count=sales_count+1, updated_at=?"
                 " WHERE id=? AND source<>'local'", (now, int(item_id)))
    uit = one("SELECT * FROM creative_user_items WHERE user_id=? AND item_id=?"
              " AND acquisition_type='purchased'", (int(uid), int(item_id)))
    return {"success": True, "userItemId": (uit or {}).get("id") or pid,
            "purchaseId": pid, "creativeItemId": int(item_id),
            "remainingPoints": balance}, ""


def creative_start_trial(uid: int | None, item_id: int) -> tuple[dict | None, str]:
    if not uid:
        return None, "请先登录后再试用"
    row = creative_item_row(item_id)
    if not row:
        return None, "商品不存在"
    if not row.get("allow_trial"):
        return None, "该商品不支持试用"
    now = now_iso()
    existed = one("SELECT * FROM creative_user_items WHERE user_id=? AND item_id=?"
                  " AND acquisition_type='trial'", (int(uid), int(item_id)))
    if existed:
        return {"success": True, "trialId": existed["id"],
                "trialRounds": existed["trial_rounds_total"],
                "trialTimesUsed": existed["trial_times_used"],
                "disabledItemIds": []}, ""
    cur = get_conn().execute(
        "INSERT INTO creative_user_items(user_id, item_id, acquisition_type, status,"
        " is_enabled, trial_rounds_total, trial_rounds_remaining, current_version,"
        " created_at, updated_at) VALUES(?,?, 'trial', 'trial_active', 1, 3, 3, ?, ?, ?)",
        (int(uid), int(item_id), int(row.get("version") or 1), now, now))
    return {"success": True, "trialId": cur.lastrowid, "trialRounds": 3,
            "trialTimesUsed": 0, "disabledItemIds": []}, ""


def creative_trial_convert(uid: int | None, user_item_id: int) -> tuple[dict | None, str]:
    if not uid:
        return None, "请先登录"
    r = one("SELECT * FROM creative_user_items WHERE id=? AND user_id=?",
            (int(user_item_id), int(uid)))
    if not r:
        return None, "试用记录不存在"
    if r["acquisition_type"] == "purchased":
        return {"success": True, "userItemId": r["id"], "purchaseId": r["id"],
                "remainingPoints": points_balance(int(uid))}, ""
    bought = one("SELECT * FROM creative_user_items WHERE user_id=? AND item_id=?"
                 " AND acquisition_type='purchased'", (int(uid), int(r["item_id"])))
    if bought:
        execute("DELETE FROM creative_user_items WHERE id=?", (int(user_item_id),))
        return {"success": True, "userItemId": bought["id"], "purchaseId": bought["id"],
                "remainingPoints": points_balance(int(uid))}, ""
    row = creative_item_row(r["item_id"])
    price = max(0, int((row or {}).get("price") or 0))
    if points_balance(int(uid)) < price:
        return None, f"积分不足：需要 {price}，当前 {points_balance(int(uid))}"
    if price:
        add_points(int(uid), -price)
    now = now_iso()
    conn = get_conn()
    conn.execute("UPDATE creative_user_items SET acquisition_type='purchased',"
                 " status='enabled', is_enabled=1, trial_rounds_remaining=0,"
                 " updated_at=? WHERE id=?", (now, int(user_item_id)))
    conn.execute("UPDATE creative_items SET sales_count=sales_count+1 WHERE id=?",
                 (int(r["item_id"]),))
    return {"success": True, "userItemId": r["id"], "purchaseId": r["id"],
            "remainingPoints": points_balance(int(uid))}, ""


def creative_trial_abandon(uid: int | None, user_item_id: int) -> bool:
    if not uid:
        return False
    return execute("DELETE FROM creative_user_items WHERE id=? AND user_id=?"
                   " AND acquisition_type='trial'", (int(user_item_id), int(uid))) > 0


def creative_toggle_user_item(uid: int | None, user_item_id: int, enabled: bool) -> dict | None:
    if not uid:
        return None
    r = one("SELECT * FROM creative_user_items WHERE id=? AND user_id=?",
            (int(user_item_id), int(uid)))
    if not r:
        return None
    now = now_iso()
    if r["acquisition_type"] == "trial":
        status = "trial_active" if enabled and int(r.get("trial_rounds_remaining") or 0) > 0 \
            else ("trial_expired" if int(r.get("trial_rounds_remaining") or 0) <= 0 else "disabled")
    else:
        status = "enabled" if enabled else "disabled"
    execute("UPDATE creative_user_items SET is_enabled=?, status=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, status, now, int(user_item_id)))
    return {"activeMode": "single", "disabledItemIds": []}


def creative_vote(uid: int | None, item_id: int, vote_type: str) -> tuple[dict | None, str]:
    if not uid:
        return None, "请先登录后再点赞"
    if vote_type not in ("like", "dislike"):
        return None, "投票类型不正确"
    row = creative_item_row(item_id)
    if not row:
        return None, "商品不存在"
    owned = creative_owned(uid, item_id)
    if not owned and int(row.get("author_id") or 0) != int(uid):
        return None, "购买或试用后才能点赞"
    old = one("SELECT * FROM creative_votes WHERE user_id=? AND item_id=?",
              (int(uid), int(item_id)))
    conn = get_conn()
    likes = int(row.get("like_count") or 0)
    dislikes = int(row.get("dislike_count") or 0)
    if old:
        if old["vote_type"] == vote_type:
            return {"success": True}, ""
        if old["vote_type"] == "like":
            likes = max(0, likes - 1)
        else:
            dislikes = max(0, dislikes - 1)
    if vote_type == "like":
        likes += 1
    else:
        dislikes += 1
    total = likes + dislikes
    rate = round(likes * 100.0 / total, 2) if total else 0
    conn.execute("INSERT INTO creative_votes(user_id, item_id, vote_type, created_at)"
                 " VALUES(?,?,?,?) ON CONFLICT(user_id, item_id) DO UPDATE SET"
                 " vote_type=excluded.vote_type, created_at=excluded.created_at",
                 (int(uid), int(item_id), vote_type, now_iso()))
    conn.execute("UPDATE creative_items SET like_count=?, dislike_count=?, approval_rate=?"
                 " WHERE id=?", (likes, dislikes, rate, int(item_id)))
    return {"success": True}, ""


def creative_my_vote(uid: int | None, item_id: int) -> dict | None:
    if not uid:
        return None
    r = one("SELECT * FROM creative_votes WHERE user_id=? AND item_id=?",
            (int(uid), int(item_id)))
    if not r:
        return None
    return {"creativeItemId": int(item_id), "userId": int(uid),
            "voteType": r["vote_type"], "createdAt": r["created_at"]}


def creative_vote_eligibility(uid: int | None, item_id: int) -> dict:
    owned = creative_owned(uid, item_id) if uid else None
    rounds = int((owned or {}).get("usage_rounds_count") or 0)
    return {"eligible": bool(owned), "reason": "" if owned else "not_purchased",
            "currentUsageRounds": rounds, "requiredRounds": 5}


def creative_comments(item_id: int, page_size: int = 20) -> dict:
    rows = query("SELECT * FROM creative_comments WHERE item_id=?"
                 " ORDER BY id DESC LIMIT ?", (int(item_id), max(1, min(page_size, 100))))
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "userId": r.get("user_id") or 0,
            "userName": r.get("user_name") or "本地用户",
            "userAvatar": r.get("user_avatar") or "",
            "content": r.get("content") or "", "rating": int(r.get("rating") or 0),
            "replyContent": r.get("reply_content"), "replyAt": r.get("reply_at"),
            "createdAt": r.get("created_at") or "",
        })
    return {"items": items, "hasMore": len(items) >= page_size, "nextCursor": ""}


def creative_add_comment(uid: int | None, item_id: int, content: str,
                         rating: int = 0, user_name: str = "") -> tuple[dict | None, str]:
    content = (content or "").strip()
    if not content:
        return None, "评论内容不能为空"
    if len(content) > 2000:
        return None, "评论内容过长"
    if not creative_item_row(item_id):
        return None, "商品不存在"
    now = now_iso()
    avatar = ""
    if uid:
        u = one("SELECT nick_name, user_name, header_img FROM users WHERE id=?", (int(uid),))
        if u:
            user_name = u.get("nick_name") or u.get("user_name") or user_name
            avatar = u.get("header_img") or ""
    cur = get_conn().execute(
        "INSERT INTO creative_comments(item_id, user_id, user_name, user_avatar, content,"
        " rating, source, created_at, updated_at) VALUES(?,?,?,?,?,?, 'local', ?, ?)",
        (int(item_id), int(uid or 0), user_name or "本地用户", avatar,
         content, int(rating or 0), now, now))
    return {"commentId": cur.lastrowid}, ""


def creative_update_comment(uid: int | None, comment_id: int, content: str) -> bool:
    if not uid or not (content or "").strip():
        return False
    return execute("UPDATE creative_comments SET content=?, updated_at=?"
                   " WHERE id=? AND user_id=?", ((content or "").strip()[:2000],
                                                  now_iso(), int(comment_id), int(uid))) > 0


def creative_delete_comment(uid: int | None, comment_id: int) -> bool:
    if not uid:
        return False
    return execute("DELETE FROM creative_comments WHERE id=? AND user_id=?",
                   (int(comment_id), int(uid))) > 0


def creative_bindings(uid: int | None, scope: str, target_id: str) -> list[dict]:
    rows = query("SELECT * FROM creative_bindings WHERE user_id=? AND scope=? AND target_id=?"
                 " ORDER BY created_at DESC", (int(uid or 0), scope, str(target_id)))
    out = []
    for r in rows:
        item_row = creative_item_row(r["item_id"])
        item = creative_item_public(item_row, include_prompt=False) if item_row else {}
        out.append({"itemType": r.get("item_type") or item.get("itemType") or "reply_example",
                    "creativeItemId": r["item_id"], "title": item.get("title") or "",
                    "acquisitionType": "purchased", "trialRoundsTotal": 0,
                    "trialRoundsUsed": 0, "trialRoundsRemaining": 0,
                    "trialStatus": "purchased", "trialExpiredAt": None})
    return out


def creative_equip(uid: int | None, item_id: int, scope: str, target_id: str) -> tuple[dict, str]:
    if not creative_item_row(item_id):
        return {}, "商品不存在"
    now = now_iso()
    conn = get_conn()
    item = creative_item_row(item_id)
    conn.execute("INSERT INTO creative_bindings(user_id, scope, target_id, item_id,"
                 " item_type, created_at) VALUES(?,?,?,?,?,?)"
                 " ON CONFLICT(user_id, scope, target_id, item_id) DO UPDATE SET"
                 " item_type=excluded.item_type, created_at=excluded.created_at",
                 (int(uid or 0), scope, str(target_id), int(item_id),
                  (item or {}).get("item_type") or "reply_example", now))
    disabled = [int(x["item_id"]) for x in query(
        "SELECT item_id FROM creative_bindings WHERE user_id=? AND scope=? AND target_id=?"
        " AND item_type=(?) AND item_id<>?", (int(uid or 0), scope, str(target_id),
                                              (item or {}).get("item_type") or "", int(item_id)))]
    return {"success": True, "disabledItemIds": disabled}, ""


def creative_unequip(uid: int | None, scope: str, target_id: str, item_id: int) -> bool:
    return execute("DELETE FROM creative_bindings WHERE user_id=? AND scope=? AND target_id=?"
                   " AND item_id=?", (int(uid or 0), scope, str(target_id), int(item_id))) > 0


def creative_defaults(uid: int | None) -> list[dict]:
    rows = query("SELECT * FROM creative_defaults WHERE user_id=? ORDER BY item_type",
                 (int(uid or 0),))
    return [{"itemType": r["item_type"], "userItemId": 0,
             "creativeItemId": r["item_id"]} for r in rows]


def creative_set_default(uid: int | None, item_type: str, item_id: int) -> bool:
    if not item_type:
        return False
    execute("INSERT INTO creative_defaults(user_id, item_type, item_id, created_at)"
            " VALUES(?,?,?,?) ON CONFLICT(user_id, item_type) DO UPDATE SET"
            " item_id=excluded.item_id, created_at=excluded.created_at",
            (int(uid or 0), item_type, int(item_id), now_iso()))
    return True


def creative_delete_default(uid: int | None, item_type: str) -> bool:
    return execute("DELETE FROM creative_defaults WHERE user_id=? AND item_type=?",
                   (int(uid or 0), item_type)) > 0


def creative_share_configs(uid: int | None, session_id: str) -> list[dict]:
    rows = query("SELECT * FROM creative_bindings WHERE user_id=? AND scope='session'"
                 " AND target_id=? ORDER BY item_type", (int(uid or 0), str(session_id)))
    out = []
    for r in rows:
        item = creative_item_row(r["item_id"]) or {}
        out.append({"itemType": r.get("item_type") or "",
                    "itemId": r["item_id"], "itemTitle": item.get("title") or ""})
    return out


def creative_my_items(uid: int | None, page: int = 1, page_size: int = 20) -> dict:
    if not uid:
        return {"items": [], "total": 0, "page": page, "pageSize": page_size}
    page = max(1, page)
    size = max(1, min(page_size, 100))
    total = int((one("SELECT COUNT(*) AS c FROM creative_items WHERE source='local'"
                     " AND author_id=?", (int(uid),)) or {}).get("c") or 0)
    rows = query("SELECT * FROM creative_items WHERE source='local' AND author_id=?"
                 " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                 (int(uid), size, (page - 1) * size))
    return {"items": [creative_item_public(r, include_prompt=True) for r in rows],
            "total": total, "page": page, "pageSize": size}


def creative_create_item(uid: int, body: dict) -> dict:
    """本地发布：直接进 creative_items，状态 active（本地广场立刻能看到）"""
    now = now_iso()
    top = int((one("SELECT COALESCE(MAX(id), 0) AS m FROM creative_items") or {}).get("m") or 0)
    nid = top + 1 if top >= 900000 else 900001     # 本地商品从 900001 起，避开线上 id
    item = dict(body)
    item["id"] = nid
    item.setdefault("status", "active")
    item.setdefault("isOpenSource", bool(body.get("isOpenSource") or body.get("is_open_source")))
    user = one("SELECT nick_name, user_name, header_img FROM users WHERE id=?", (int(uid),)) or {}
    item.setdefault("authorId", int(uid))
    item.setdefault("authorName", user.get("nick_name") or user.get("user_name") or "本地作者")
    item.setdefault("authorAvatar", user.get("header_img") or "")
    item.setdefault("createdAt", now)
    item["updatedAt"] = now
    _creative_upsert(item, "local", now)
    row = creative_item_row(nid)
    return {"itemId": nid, "status": (row or {}).get("status") or "active"}


def creative_update_item(uid: int, item_id: int, body: dict) -> bool:
    row = creative_item_row(item_id)
    if not row or row.get("source") != "local" or int(row.get("author_id") or 0) != int(uid):
        return False
    item = dict(body)
    item["id"] = int(item_id)
    item.setdefault("authorId", int(uid))
    _creative_upsert(item, "local", now_iso())
    return True


def creative_set_status(uid: int, item_id: int, status: str) -> bool:
    return execute("UPDATE creative_items SET status=?, updated_at=?"
                   " WHERE id=? AND source='local' AND author_id=?",
                   (status, now_iso(), int(item_id), int(uid))) > 0


def creative_seller_dashboard(uid: int | None) -> dict:
    if not uid:
        return {"totalItems": 0, "activeItems": 0, "totalSales": 0, "totalRevenue": 0,
                "todaySales": 0, "todayRevenue": 0, "avgApprovalRate": 0, "topItem": None}
    rows = query("SELECT * FROM creative_items WHERE author_id=? ORDER BY sales_count DESC",
                 (int(uid),))
    active = [r for r in rows if r.get("status") == "active"]
    sales = sum(int(r.get("sales_count") or 0) for r in rows)
    revenue = sum(int(r.get("sales_count") or 0) * int(r.get("price") or 0) for r in rows)
    rates = [float(r.get("approval_rate") or 0) for r in rows if int(r.get("sales_count") or 0)]
    top = rows[0] if rows else None
    return {"totalItems": len(rows), "activeItems": len(active), "totalSales": sales,
            "totalRevenue": revenue, "todaySales": 0, "todayRevenue": 0,
            "avgApprovalRate": round(sum(rates) / len(rates), 2) if rates else 0,
            "topItem": {"id": top["id"], "title": top.get("title") or "",
                        "salesCount": int(top.get("sales_count") or 0)} if top else None}


def creative_seller_trend(uid: int | None, days: int = 7) -> list[dict]:
    # 本地只记总量，没有按日流水 → 给最近 N 天的空趋势，形状一致
    out = []
    today = date.today()
    for i in range(max(1, min(days, 90)) - 1, -1, -1):
        out.append({"date": (today - timedelta(days=i)).isoformat(), "sales": 0, "revenue": 0})
    return out


def creative_seller_items(uid: int | None) -> list[dict]:
    if not uid:
        return []
    rows = query("SELECT * FROM creative_items WHERE author_id=? ORDER BY updated_at DESC",
                 (int(uid),))
    return [{"id": r["id"], "title": r.get("title") or "",
             "itemType": r.get("item_type") or "reply_example",
             "salesCount": int(r.get("sales_count") or 0),
             "likeCount": int(r.get("like_count") or 0),
             "dislikeCount": int(r.get("dislike_count") or 0),
             "approvalRate": float(r.get("approval_rate") or 0),
             "heatScore": float(r.get("heat_score") or 0),
             "revenue": int(r.get("sales_count") or 0) * int(r.get("price") or 0),
             "createdAt": r.get("first_seen") or ""} for r in rows]


def creative_export_rows(open_only: bool = True) -> list[dict]:
    rows = query("SELECT * FROM creative_items"
                 + (" WHERE is_open_source=1" if open_only else "")
                 + " ORDER BY id")
    out = []
    for r in rows:
        item = creative_item_public(r, include_prompt=True)
        out.append({
            "id": item.get("id"), "itemType": item.get("itemType"),
            "title": item.get("title"), "description": item.get("description"),
            "price": item.get("price"), "tags": item.get("tags"),
            "authorName": item.get("authorName"), "isOpenSource": item.get("isOpenSource"),
            "allowTrial": item.get("allowTrial"), "promptContent": item.get("promptContent"),
            "promptData": item.get("promptData"),
            "pairedCssPrompt": item.get("pairedCssPrompt"),
            "updatedAt": r.get("updated_at"),
        })
    return out



# --------------------------------------------------------------- 创作（我的作品 / 世界书 / 配置）

WORK_STATUSES = ("draft", "published", "archived", "scheduled")


def _as_dict(payload) -> dict:
    if isinstance(payload, dict):
        return payload
    try:
        obj = json.loads(payload or "{}")
    except (TypeError, ValueError):  # noqa: PERF203
        obj = {}
    return obj if isinstance(obj, dict) else {}


def work_identity(payload: dict) -> str:
    """作品在接口里的主键（对外字段 characterId，兼容旧写法）"""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("characterId") or payload.get("characterID")
               or payload.get("character_id")
               or payload.get("workID") or "").strip()


def save_work(user_id: int, payload: dict, status: str | None = None) -> dict | None:
    """保存作品整包（POST /work 新建、PUT /work/content 更新）"""
    cid = work_identity(payload)
    if not cid:
        return None
    row = one("SELECT payload, status, scheduled_at FROM my_works"
              " WHERE user_id=? AND character_id=?", (user_id, cid))
    merged = _as_dict(row["payload"]) if row else {}
    merged.update(payload)

    body_status = str(payload.get("publishStatus") or "").strip()
    final = status or (body_status if body_status in WORK_STATUSES else "") \
        or (row["status"] if row else "draft")
    if final not in WORK_STATUSES:
        final = "draft"

    now = now_iso()
    merged.pop("characterID", None)
    merged["characterId"] = cid
    merged["publishStatus"] = final
    merged["updatedAt"] = now
    blob = json.dumps(merged, ensure_ascii=False)
    title = str(merged.get("name") or "")[:200]
    summary = str(merged.get("summary") or "")[:1000]
    cover = str(merged.get("avatarURL") or merged.get("coverURL") or "")[:500]

    if row:
        execute("UPDATE my_works SET status=?, title=?, summary=?, cover_url=?,"
                " payload=?, updated_at=? WHERE user_id=? AND character_id=?",
                (final, title, summary, cover, blob, now, user_id, cid))
    else:
        execute("INSERT INTO my_works(user_id, character_id, status, title, summary,"
                " cover_url, scheduled_at, payload, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (user_id, cid, final, title, summary, cover, None, blob, now, now))
    return merged


def get_work(user_id: int, character_id: str) -> dict | None:
    row = one("SELECT payload FROM my_works WHERE user_id=? AND character_id=?",
              (user_id, character_id))
    return _normalize_cid(_as_dict(row["payload"])) if row else None


def find_work(character_id: str) -> dict | None:
    """任何用户本地建的作品（profile 页按 characterId 找详情用）"""
    row = one("SELECT payload FROM my_works WHERE character_id=? LIMIT 1",
              (str(character_id or "").strip(),))
    return _normalize_cid(_as_dict(row["payload"])) if row else None


def _normalize_cid(payload: dict) -> dict:
    """存量 payload 只有 characterID：读出来时补一份 characterId（不改库）"""
    if isinstance(payload, dict) and "characterID" in payload \
            and not payload.get("characterId"):
        payload = dict(payload)
        payload["characterId"] = payload["characterID"]
    return payload


def work_as_character(payload: dict) -> dict:
    """把创作页存的整包拼成 profile 页要的 data.character 形状"""
    ch = _normalize_cid(dict(payload or {}))
    ch.setdefault("characterId", ch.get("characterID") or "")
    ch.pop("characterID", None)
    name = str(ch.get("name") or "")
    ch["characterName"] = name
    ch.setdefault("summary", "")
    ch.setdefault("description", "")
    ch.setdefault("avatarURL", ch.get("coverURL") or "")
    ch.setdefault("coverURL", ch.get("avatarURL") or "")
    ch.setdefault("genderOrientation", "")
    ch.setdefault("tags", [])
    ch.setdefault("alternateGreetings", [])
    ch.setdefault("firstMes", "")
    ch.setdefault("mesExample", "")
    ch.setdefault("creatorComment", "")
    ch.setdefault("customCSS", "")
    ch.setdefault("quickCommands", [])
    ch.setdefault("publishStatus", ch.get("publishStatus") or "draft")
    ch.setdefault("isPublic", False)
    ch.setdefault("averageRating", 0)
    ch.setdefault("ratingsCount", 0)
    ch.setdefault("viewCount", 0)
    ch.setdefault("version", 1)
    ch.setdefault("characterVersion", "1")
    creator = ch.get("creator")
    if not isinstance(creator, dict) or not creator.get("name"):
        # 前端先看 creator.name，没有才会退回 createdBy/authorName 字符串
        ch["creator"] = {"ID": 0, "name": str(ch.get("authorName") or "本地作者"),
                         "userName": "本地作者", "nickName": "本地作者",
                         "avatar": "", "charactersCount": 0, "followersCount": 0,
                         "characterId": ch.get("characterId")}
    return ch


def list_my_works(user_id: int, page: int = 1, page_size: int = 10,
                  status: str = "", keyword: str = "") -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 10), 100))
    where = ["user_id=?"]
    params: list = [user_id]
    if status:
        where.append("status=?")
        params.append(status)
    if keyword:
        like = f"%{str(keyword)[:60]}%"
        where.append("(title LIKE ? OR summary LIKE ?)")
        params += [like, like]
    base = "SELECT character_id, status, title, summary, cover_url, scheduled_at," \
           " payload, created_at, updated_at FROM my_works WHERE " + " AND ".join(where)
    total = one(f"SELECT COUNT(*) AS c FROM ({base})", tuple(params))["c"]
    rows = query(base + " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                 tuple(params) + (page_size, (page - 1) * page_size))
    items = []
    for r in rows:
        payload = _as_dict(r["payload"])
        items.append({
            "ID": r["character_id"],
            "id": r["character_id"], "characterId": r["character_id"],
            "workID": r["character_id"],
            "name": r["title"] or "", "title": r["title"] or "",
            "summary": r["summary"] or "",
            "avatarURL": r["cover_url"] or "", "coverURL": r["cover_url"] or "",
            "publishStatus": r["status"], "status": r["status"],
            "isPublished": r["status"] == "published",
            "scheduledPublishAt": r["scheduled_at"],
            "tags": payload.get("tags") or [],
            "createdAt": r["created_at"], "updatedAt": r["updated_at"],
        })
    return {"list": items, "records": items, "total": int(total),
            "page": page, "pageSize": page_size}


def _purge_work(user_id: int, character_id: str) -> tuple[bool, int]:
    """删作品本体 + 它的世界书条目（已发布的一并从公开库下架）。

    世界书是作品的一部分（worldbook_entries 按 user_id + character_id 归属），
    只删 my_works 会把条目留成孤儿：控制台里世界书计数虚高、重导同一个
    characterID 还会带出没人认领的旧条目，所以这里一起收。
    返回 (作品是否真的删掉了, 清掉的世界书条数)。
    """
    cid = str(character_id or "").strip()
    if not cid:
        return False, 0
    n = execute("DELETE FROM my_works WHERE user_id=? AND character_id=?",
                (user_id, cid))
    m = execute("DELETE FROM worldbook_entries WHERE user_id=? AND character_id=?",
                (user_id, cid))
    # 发布过的作品在公开库留了 path='local/published' 投影，作品没了投影也得撤；
    # unpublish_from_public 只删这条标记的记录，不会动镜像抓来的线上角色。
    unpublish_from_public(cid)
    return bool(n), int(m or 0)


def delete_work(user_id: int, character_id: str) -> bool:
    """创作页删除作品（DELETE /api/gva/work）→ 连带它的世界书一起删"""
    return _purge_work(user_id, character_id)[0]


def set_work_status(user_id: int, character_id: str, status: str,
                    scheduled_at: str | None = None) -> dict | None:
    """发布 / 下架 / 定时发布 → 改状态并回整包（前端会读 data.updatedAt）"""
    payload = get_work(user_id, character_id) or {"characterId": character_id}
    payload["publishStatus"] = status if status in WORK_STATUSES else "draft"
    if scheduled_at is not None:
        payload["scheduledPublishAt"] = scheduled_at
    saved = save_work(user_id, payload, status=payload["publishStatus"])
    if saved is None:
        return None
    if scheduled_at is not None:
        execute("UPDATE my_works SET scheduled_at=? WHERE user_id=? AND character_id=?",
                (scheduled_at, user_id, character_id))
        saved = get_work(user_id, character_id)
    return saved


# 本地发布的作品写入公开内容库（works 表）时的来源标记，
# 探索 / 首页接口服务时会用这个标记把已发布作品注入列表，让它们在站内可见。
PUBLIC_WORK_PATH = "local/published"


def publish_to_public(user_id: int, character_id: str) -> bool:
    """发布 → 公开库：把作品从 my_works 同步进 works 表（公开作品库）。

    探索 / 首页接口（home_personalized / ranking / hot_chart 等）在
    content_fallback 里会把这些 path='local/published' 的作品注入列表，
    让"已发布"的作品真正出现在站内各页。"""
    row = one("SELECT * FROM my_works WHERE user_id=? AND character_id=?",
              (user_id, character_id))
    if not row:
        return False
    payload = _as_dict(row["payload"])
    ch = work_as_character(payload) if isinstance(payload, dict) else {}
    now = now_iso()
    name = str(row["title"] or ch.get("name") or "")[:200]
    summary = str(row["summary"] or ch.get("summary") or "")[:20000]
    cover = str(row["cover_url"] or ch.get("coverURL") or ch.get("avatarURL") or "")[:500]
    tags = _norm_tags(payload.get("tags") or ch.get("tags") or [])
    author = "本地作者"
    try:
        u = one("SELECT nick_name, user_name FROM users WHERE id=?", (user_id,))
        if u:
            author = str(u.get("nick_name") or u.get("user_name") or "本地作者")
    except Exception:  # noqa: BLE001
        pass
    raw_item = {
        "characterId": character_id,
        "name": name, "characterName": ch.get("characterName") or name,
        "summary": summary, "description": ch.get("description") or "",
        "coverURL": cover, "avatarURL": ch.get("avatarURL") or cover,
        "tags": tags, "author": author, "authorName": author,
        "publishStatus": "published", "isLocal": True,
    }
    conn = get_conn()
    conn.execute(
        """INSERT INTO works(character_id, name, summary, author_name, cover_url,
                             rate, votes, tags, raw, path, first_seen, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(character_id) DO UPDATE SET
              name=excluded.name, summary=excluded.summary, author_name=excluded.author_name,
              cover_url=excluded.cover_url, tags=excluded.tags, raw=excluded.raw,
              path=excluded.path, updated_at=excluded.updated_at""",
        (character_id, name, summary, author, cover, 0, 0,
         json.dumps(tags, ensure_ascii=False), json.dumps(raw_item, ensure_ascii=False),
         PUBLIC_WORK_PATH, now, now))
    conn.commit()
    return True


def unpublish_from_public(character_id: str) -> bool:
    """下架 → 从公开库 works 表移除该作品（仅删除本地发布的 path='local/published'，
    不误删镜像角色）。"""
    n = execute("DELETE FROM works WHERE character_id=? AND path=?",
                (character_id, PUBLIC_WORK_PATH))
    return bool(n)


def get_creative_refs(user_id: int, character_id: str):
    row = one("SELECT refs FROM my_works WHERE user_id=? AND character_id=?",
              (user_id, character_id))
    if not row or not row["refs"]:
        return None
    try:
        return json.loads(row["refs"])
    except (TypeError, ValueError):  # noqa: PERF203
        return None


def put_creative_refs(user_id: int, payload):
    """PUT /work/creativeRefs：整包原样存（回读时原样返回）"""
    cid = work_identity(payload if isinstance(payload, dict) else {})
    if not cid:
        return None
    row = one("SELECT 1 FROM my_works WHERE user_id=? AND character_id=?",
              (user_id, cid))
    blob = json.dumps(payload, ensure_ascii=False)
    now = now_iso()
    if row:
        execute("UPDATE my_works SET refs=?, updated_at=? WHERE user_id=? AND character_id=?",
                (blob, now, user_id, cid))
    else:
        execute("INSERT INTO my_works(user_id, character_id, status, payload, refs,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (user_id, cid, "draft", "{}", blob, now, now))
    return payload


def get_config(key: str) -> dict | None:
    row = one("SELECT payload FROM config_cache WHERE key=?", (key,))
    return _as_dict(row["payload"]) if row else None


def get_config_list(key: str) -> list:
    """config_cache 里存的是数组（如 CG 图列表）时读取；兼容 dict/坏数据 → 空列表"""
    row = one("SELECT payload FROM config_cache WHERE key=?", (key,))
    if not row:
        return []
    try:
        obj = json.loads(row["payload"] or "[]")
    except (TypeError, ValueError):  # noqa: PERF203
        obj = []
    return obj if isinstance(obj, list) else []


def set_config(key: str, payload) -> None:
    execute("INSERT INTO config_cache(key, payload, updated_at) VALUES(?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,"
            " updated_at=excluded.updated_at",
            (key, json.dumps(payload, ensure_ascii=False), now_iso()))


# --------------------------------------------------------------- 模型接入（OpenAI 协议）
#
# 数据库里只存最基础的两项：API 的地址 + 密钥；模型列表是连上之后拉回来的缓存，
# 控制台的「模型接入」页与聊天界面的「选择模型」都读它。
# 日后加平台预设（阿里百炼 / DeepSeek / 火山方舟…）只需要在 LLM_PRESETS 里加一行，
# 协议一律按 OpenAI 兼容走：GET {base}/models、POST {base}/chat/completions。

LLM_PRESETS = {
    "openai": {
        "label": "OpenAI 官方 / 中转站",
        "short": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "hint": "OpenAI 官方协议；任何兼容 /v1/chat/completions 的中转站都选它",
    },
    "dashscope": {
        "label": "阿里百炼",
        "short": "阿里百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "hint": "百炼的 OpenAI 兼容模式（预留）",
    },
    "deepseek": {
        "label": "DeepSeek",
        "short": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "hint": "DeepSeek 官方接口（预留）",
    },
    "volcengine": {
        "label": "火山方舟",
        "short": "火山方舟",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "hint": "火山方舟 OpenAI 兼容接口（预留）",
    },
}

# 本地兜底模型占 rateId=1，接入模型从 provider_id*1000+1 起（不冲突、可回查）
RATE_ID_BASE = 1000
LOCAL_RATE_ID = 1
LOCAL_MODEL_ID = "local-default"


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            obj = json.loads(v)
        except (ValueError, TypeError):  # noqa
            return []
        return obj if isinstance(obj, list) else []
    return []


def _mask_key(key: str) -> str:
    k = str(key or "").strip()
    if not k:
        return ""
    if len(k) <= 10:
        return "*" * len(k)
    return f"{k[:5]}…{k[-4:]}"


def _provider_view(row: dict) -> dict:
    """给控制台看的接入信息：不含明文密钥（apiKeySet / apiKeyMasked 够用了）"""
    out = dict(row)
    models = [m for m in _as_list(out.get("models")) if isinstance(m, dict)]
    out["models"] = models
    out["modelCount"] = len(models)
    key = str(out.get("api_key") or "")
    out["apiKeySet"] = bool(key)
    out["apiKeyMasked"] = _mask_key(key)
    out.pop("api_key", None)
    out["enabled"] = bool(int(out.get("enabled") or 0))
    out["isDefault"] = bool(int(out.get("is_default") or 0))
    out["preset"] = LLM_PRESETS.get(str(out.get("kind") or ""), {})
    return out


def list_llm_providers() -> list[dict]:
    return [_provider_view(r) for r in
            query("SELECT * FROM llm_providers ORDER BY is_default DESC, id ASC")]


def get_llm_provider(pid) -> dict | None:
    row = one("SELECT * FROM llm_providers WHERE id=?", (int(pid),)) if pid else None
    return _provider_view(row) if row else None


def llm_credentials(pid) -> tuple[str, str] | None:
    """聊天要用的 (base_url, api_key) —— 数据库里的 api + 密钥从这里取"""
    row = one("SELECT base_url, api_key FROM llm_providers WHERE id=?",
              (int(pid),)) if pid else None
    if not row:
        return None
    return str(row["base_url"] or "").rstrip("/"), str(row["api_key"] or "")


def llm_endpoint(pid) -> tuple[str, str, str] | None:
    """(base_url, api_key, model) —— 检测过模型才有 model"""
    cred = llm_credentials(pid)
    row = one("SELECT models, default_model FROM llm_providers WHERE id=? AND enabled=1",
              (int(pid),)) if pid else None
    if not cred or not row:
        return None
    models = [m for m in _as_list(row["models"]) if isinstance(m, dict)]
    model = str(row["default_model"] or "").strip()
    if not model and models:
        model = str(models[0].get("model") or "")
    if not model:
        return None
    return cred[0], cred[1], model


def active_llm_endpoint() -> tuple[str, str, str] | None:
    """没指定模型时用默认接入；默认那条还没检测出模型就顺延到下一条可用的"""
    rows = query("SELECT id FROM llm_providers WHERE enabled=1"
                 " ORDER BY is_default DESC, id ASC")
    for row in rows:
        endpoint = llm_endpoint(row["id"])
        if endpoint:
            return endpoint
    return None


def llm_profile(rate_id=None) -> dict:
    """接入的格式适配配置（provider profile JSON，空字典表示用默认协议）。

    指定模型时按该接入；否则按默认接入。支持：
      max_tokens_field  max_tokens / max_completion_tokens
      stop              stop 序列（字符串或数组）
      extra             需要原样并入 payload 的额外字段
    """
    pid = None
    rid = str(rate_id or "").strip()
    if rid not in ("", "None"):
        picked = resolve_rate_id(rid)
        if picked:
            pid = picked[0]["id"]
    if pid is None:
        rows = query("SELECT id FROM llm_providers WHERE enabled=1"
                     " ORDER BY is_default DESC, id ASC")
        pid = rows[0]["id"] if rows else None
    if not pid:
        return {}
    row = one("SELECT profile FROM llm_providers WHERE id=?", (int(pid),))
    if not row or not row["profile"]:
        return {}
    try:
        obj = json.loads(row["profile"])
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def save_llm_provider(payload: dict) -> dict | None:
    """新建/更新一条接入。api_key 留空表示不改（控制台回显的是打码后的密钥）"""
    pid = payload.get("id")
    row = one("SELECT * FROM llm_providers WHERE id=?", (int(pid),)) if pid else None
    kind = str(payload.get("kind") or (row["kind"] if row else "") or "openai")
    preset = LLM_PRESETS.get(kind, {})
    name = (str(payload.get("name") or "").strip()
            or str(row["name"] if row else "").strip()
            or str(preset.get("label") or kind))
    base_url = str(payload.get("base_url") or "").strip() or (row["base_url"] if row else "")
    if not base_url:                                    # 没手填地址 → 回落到平台预设的官方地址
        base_url = str(preset.get("base_url") or "").strip()
    base_url = base_url.rstrip("/")
    if not base_url:
        return None
    key = str(payload.get("api_key") or "").strip()
    if not key and row:
        key = str(row["api_key"] or "")
    now = now_iso()
    if row:
        execute("UPDATE llm_providers SET kind=?, name=?, base_url=?, api_key=?,"
                " updated_at=? WHERE id=?",
                (kind, name, base_url, key, now, row["id"]))
        return get_llm_provider(row["id"])
    first = one("SELECT id FROM llm_providers LIMIT 1")     # 第一条自动当默认
    execute("INSERT INTO llm_providers(kind, name, base_url, api_key, models, status,"
            " enabled, is_default, created_at, updated_at)"
            " VALUES(?,?,?,?, '[]', 'unknown', 1, ?, ?, ?)",
            (kind, name, base_url, key, 0 if first else 1, now, now))
    row = one("SELECT id FROM llm_providers ORDER BY id DESC LIMIT 1")
    return get_llm_provider(row["id"])


def delete_llm_provider(pid) -> bool:
    # 视图 + INSTEAD OF 触发器下 rowcount 恒为 0，改用存在性判断
    if not one("SELECT id FROM llm_providers WHERE id=?", (int(pid),)):
        return False
    execute("DELETE FROM llm_providers WHERE id=?", (int(pid),))
    return True


def set_llm_models(pid, models: list[str], status: str, error: str = "",
                   default_model: str = "") -> dict | None:
    """存检测到的模型：已有的模型沿用旧 rateId（聊天里选过的模型不会漂）"""
    row = one("SELECT * FROM llm_providers WHERE id=?", (int(pid),))
    if not row:
        return None
    old = [m for m in _as_list(row["models"]) if isinstance(m, dict)]
    by_name = {str(m.get("model") or ""): int(m.get("rateId") or 0) for m in old}
    used = {v for v in by_name.values() if v}
    cursor = int(pid or 1) * RATE_ID_BASE + 1
    merged = []
    seen = set()
    for mid in [str(x) for x in models if str(x)]:
        if mid in seen:
            continue
        seen.add(mid)
        rid = by_name.get(mid) or 0
        if not rid:
            while cursor in used:
                cursor += 1
            rid = cursor
            used.add(rid)
        merged.append({"model": mid, "rateId": rid})
    keep = str(default_model or "").strip()
    if keep and keep not in seen:
        keep = ""
    if not keep:
        keep = str(row["default_model"] or "") if str(row["default_model"] or "") in seen else ""
    if not keep and merged:
        keep = merged[0]["model"]
    if str(status or "").lower() == "error" and not merged and old:
        # 重新检测失败（断网 / 超时 / 上游临时 5xx）不要把上次拉回来的模型清掉，
        # 只把状态标成 error：否则聊天里的模型列表会因为一次抖动退回「本地兜底」
        merged = old
        keep = str(row["default_model"] or "")
        if not keep or keep not in {str(m.get("model") or "") for m in merged}:
            keep = str(merged[0].get("model") or "")
    now = now_iso()
    execute("UPDATE llm_providers SET models=?, status=?, last_error=?, last_check_at=?,"
            " default_model=?, updated_at=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), str(status or "unknown"),
             str(error or "")[:500], now, keep, now, int(pid)))
    return get_llm_provider(pid)


def set_llm_active(pid, model: str = "") -> dict | None:
    """把某条接入设为默认（聊天默认用它），可同时指定默认模型"""
    pid = int(pid)
    row = one("SELECT id FROM llm_providers WHERE id=?", (pid,))
    if not row:
        return None
    execute("UPDATE llm_providers SET is_default=0")
    keep = ""
    if model:
        prov = get_llm_provider(pid)
        names = [str(m.get("model") or "") for m in (prov or {}).get("models") or []]
        keep = str(model) if str(model) in names else ""
    execute("UPDATE llm_providers SET is_default=1,"
            " default_model=CASE WHEN ?='' THEN default_model ELSE ? END WHERE id=?",
            (keep, keep, pid))
    return get_llm_provider(pid)


def llm_rate_models() -> list[dict]:
    """聊天界面「选择模型」的列表：中转站返回几个模型，这里就有几条"""
    items = []
    for row in query("SELECT * FROM llm_providers WHERE enabled=1"
                     " ORDER BY is_default DESC, id ASC"):
        models = [m for m in _as_list(row["models"]) if isinstance(m, dict)]
        for m in models:
            mid = str(m.get("model") or "")
            if not mid:
                continue
            items.append({
                "ID": int(m.get("rateId") or 0), "id": int(m.get("rateId") or 0),
                "modelID": mid, "modelId": mid,
                # 模型选择器（线上 chunk 7170）：行标题 = poolName || modelName，
                # 副标题 = description，分组按模型名关键词推断（gpt→OpenAI）
                "modelName": mid, "poolName": mid,
                "name": mid, "title": mid, "label": mid,
                "description": row["name"], "providerName": row["name"],
                "llmConfigID": int(row["id"]), "isEnabled": True,
                "isDefault": bool(int(row["is_default"] or 0))
                and str(row["default_model"] or "") == mid,
                "price": 0, "currency": "points",
                "providerKind": row["kind"], "providerId": int(row["id"]),
            })
    if not items:                       # 还没接模型 → 留一个本地兜底，聊天照样能用
        items.append({
            "ID": LOCAL_RATE_ID, "id": LOCAL_RATE_ID,
            "modelID": LOCAL_MODEL_ID, "modelId": LOCAL_MODEL_ID,
            "modelName": "本地默认模型", "poolName": "本地默认模型",
            "description": "本地兜底 · 未接模型时可用",
            "llmConfigID": 1, "isEnabled": True, "isDefault": True,
            "price": 0, "currency": "points",
            "providerKind": "local", "providerId": 0,
        })
    return items


def resolve_rate_id(rate_id) -> tuple[dict, str] | None:
    """聊天里传来的 modelRateId → (接入行, 真实模型名)；本地兜底返回 None"""
    try:
        rid = int(rate_id)
    except (TypeError, ValueError):
        return None
    if not rid or rid == LOCAL_RATE_ID:
        return None
    for row in query("SELECT * FROM llm_providers WHERE enabled=1"):
        for m in _as_list(row["models"]):
            if isinstance(m, dict) and int(m.get("rateId") or 0) == rid:
                return row, str(m.get("model") or "")
    return None


# --------------------------------------------------------------- 绘图模型接入（OpenAI 兼容）
#
# 与聊天接入分开一张表：绘图只要「地址 + 密钥 + 模型名 + 尺寸」，
# 检测走 GET {base}/models（挑出绘图类模型），生成走 POST {base}/images/generations。
# 日后加平台只需往 IMAGE_PRESETS 里加一行。

IMAGE_PRESETS = {
    "openai": {
        "label": "OpenAI 官方 / 中转站",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-image-1", "dall-e-3", "dall-e-2"],
        "sizes": ["1024x1024", "1024x1536", "1536x1024", "512x512"],
        "hint": "OpenAI 图像协议；兼容 /v1/images/generations 的中转站也选它",
    },
    "dashscope": {
        "label": "阿里百炼 · 通义万相",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["wanx2.1-t2i-turbo", "wanx2.1-t2i-plus", "wanx2.0-t2i-turbo"],
        "sizes": ["1024x1024", "1024x1536", "1536x1024"],
        "hint": "百炼 OpenAI 兼容模式（通义万相文生图）",
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["Kwai-Kolors/Kolors", "stabilityai/stable-diffusion-3-5-large"],
        "sizes": ["1024x1024", "1024x1536", "1536x1024"],
        "hint": "硅基流动的 OpenAI 兼容图像接口",
    },
    "volcengine": {
        "label": "火山方舟 · 豆包",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seedream-3-0-t2i-250415"],
        "sizes": ["1024x1024", "1024x1536", "1536x1024"],
        "hint": "火山方舟 OpenAI 兼容图像接口",
    },
    "custom": {
        "label": "自定义 OpenAI 兼容",
        "base_url": "",
        "models": [],
        "sizes": ["1024x1024", "1024x1536", "1536x1024", "512x512"],
        "hint": "任何实现 /v1/images/generations 的服务都能接",
    },
    "local": {
        "label": "本地占位绘图（不联网）",
        "base_url": "local://placeholder",
        "models": ["local-placeholder"],
        "sizes": ["1024x1024", "1024x1536", "1536x1024", "512x512"],
        "hint": "不调用任何 API，直接生成渐变占位图，用来先跑通绘图页面",
    },
}

# 绘图模型的 rateId 与聊天分开编号，避免撞号
IMAGE_RATE_BASE = 500000
LOCAL_IMAGE_RATE_ID = 2
LOCAL_IMAGE_MODEL_ID = "local-placeholder"


# --------------------------------------------------------------- 视频模型接入
#
# 与绘图接入一样按 OpenAI 兼容协议存（地址 + 密钥 + 模型缓存 + 默认模型），
# 只是「可选尺寸」换成「可选时长规格」。检测走 GET {base}/models 并优先挑视频类模型。
# 日后加平台只需往 VIDEO_PRESETS 里加一行。

VIDEO_PRESETS = {
    "openai": {
        "label": "OpenAI 官方 / 中转站 · Sora",
        "base_url": "https://api.openai.com/v1",
        "models": ["sora-2", "sora-2-pro"],
        "specs": ["5s", "10s", "15s"],
        "hint": "OpenAI 视频协议；兼容 /v1/videos 的中转站也选它",
    },
    "dashscope": {
        "label": "阿里百炼 · 通义万相视频",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["wan2.1-t2v-turbo", "wan2.1-t2v-plus"],
        "specs": ["5s", "10s"],
        "hint": "百炼 OpenAI 兼容模式（通义万相文生视频）",
    },
    "volcengine": {
        "label": "火山方舟 · 豆包 Seedance",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seedance-1-0-pro-250528"],
        "specs": ["5s", "10s"],
        "hint": "火山方舟 OpenAI 兼容视频接口",
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["Wan-AI/Wan2.1-T2V-14B"],
        "specs": ["5s"],
        "hint": "硅基流动的 OpenAI 兼容视频接口",
    },
    "custom": {
        "label": "自定义 OpenAI 兼容",
        "base_url": "",
        "models": [],
        "specs": ["5s", "10s"],
        "hint": "任何实现 /v1/videos 且能列 /v1/models 的服务都能接",
    },
    "local": {
        "label": "本地占位视频（不联网）",
        "base_url": "local://placeholder",
        "models": ["local-video-placeholder"],
        "specs": ["5s", "10s"],
        "hint": "不调用任何 API，先用本地占位跑通视频接入面板",
    },
}

# 检测时优先保留名字里带这些关键词的模型
VIDEO_HINTS = (
    "video", "sora", "veo", "kling", "seedance", "wan2", "wanx", "hailuo",
    "minimax", "runway", "luma", "pika", "vidu", "dreamina", "jimeng",
    "t2v", "i2v", "s2v",
)

VIDEO_RATE_BASE = 600000


def _image_provider_view(row: dict) -> dict:
    out = dict(row)
    models = [m for m in _as_list(out.get("models")) if isinstance(m, dict)]
    out["models"] = models
    out["modelCount"] = len(models)
    key = str(out.get("api_key") or "")
    out["apiKeySet"] = bool(key)
    out["apiKeyMasked"] = _mask_key(key)
    out.pop("api_key", None)
    out["enabled"] = bool(int(out.get("enabled") or 0))
    out["isDefault"] = bool(int(out.get("is_default") or 0))
    sizes = [str(s) for s in _as_list(out.get("sizes")) if str(s).strip()]
    preset = IMAGE_PRESETS.get(str(out.get("kind") or ""), {})
    out["sizes"] = sizes or list(preset.get("sizes") or ["1024x1024"])
    out["preset"] = preset
    try:
        out["extra"] = json.loads(out.get("extra") or "{}")
    except (ValueError, TypeError):
        out["extra"] = {}
    return out


def list_image_providers() -> list[dict]:
    return [_image_provider_view(r) for r in
            query("SELECT * FROM image_providers ORDER BY is_default DESC, id ASC")]


def get_image_provider(pid) -> dict | None:
    row = one("SELECT * FROM image_providers WHERE id=?", (int(pid),)) if pid else None
    return _image_provider_view(row) if row else None


def image_credentials(pid) -> tuple[str, str] | None:
    row = one("SELECT base_url, api_key FROM image_providers WHERE id=?",
              (int(pid),)) if pid else None
    if not row:
        return None
    return str(row["base_url"] or "").rstrip("/"), str(row["api_key"] or "")


def image_endpoint(pid) -> tuple[str, str, str, list[str]] | None:
    """(base_url, api_key, model, sizes) —— 检测过模型才有 model"""
    cred = image_credentials(pid)
    row = one("SELECT models, default_model, sizes FROM image_providers"
              " WHERE id=? AND enabled=1", (int(pid),)) if pid else None
    if not cred or not row:
        return None
    models = [m for m in _as_list(row["models"]) if isinstance(m, dict)]
    model = str(row["default_model"] or "").strip()
    if not model and models:
        model = str(models[0].get("model") or "")
    if not model:
        return None
    sizes = [str(s) for s in _as_list(row["sizes"]) if str(s).strip()]
    return cred[0], cred[1], model, sizes


def active_image_endpoint() -> tuple[str, str, str, list[str]] | None:
    rows = query("SELECT id FROM image_providers WHERE enabled=1"
                 " ORDER BY is_default DESC, id ASC")
    for row in rows:
        endpoint = image_endpoint(row["id"])
        if endpoint:
            return endpoint
    return None


def save_image_provider(payload: dict) -> dict | None:
    """新建/更新一条绘图接入。api_key 留空表示不改"""
    pid = payload.get("id")
    row = one("SELECT * FROM image_providers WHERE id=?", (int(pid),)) if pid else None
    kind = str(payload.get("kind") or (row["kind"] if row else "") or "openai")
    preset = IMAGE_PRESETS.get(kind, {})
    name = (str(payload.get("name") or "").strip()
            or str(row["name"] if row else "").strip()
            or str(preset.get("label") or kind))
    base_url = str(payload.get("base_url") or "").strip() or (row["base_url"] if row else "")
    if not base_url:                                    # 没手填地址 → 回落到平台预设的官方地址
        base_url = str(preset.get("base_url") or "").strip()
    base_url = base_url.rstrip("/")
    if not base_url:
        return None
    key = str(payload.get("api_key") or "").strip()
    if not key and row:
        key = str(row["api_key"] or "")
    sizes = payload.get("sizes")
    if isinstance(sizes, str):
        sizes = [s.strip() for s in sizes.replace("，", ",").split(",") if s.strip()]
    if not isinstance(sizes, list) or not sizes:
        sizes = ([str(s) for s in _as_list(row["sizes"])] if row else [])
    if not sizes:
        sizes = list(preset.get("sizes") or ["1024x1024", "1024x1536", "1536x1024"])
    now = now_iso()
    if row:
        execute("UPDATE image_providers SET kind=?, name=?, base_url=?, api_key=?,"
                " sizes=?, updated_at=? WHERE id=?",
                (kind, name, base_url, key, json.dumps(sizes, ensure_ascii=False),
                 now, row["id"]))
        return get_image_provider(row["id"])
    first = one("SELECT id FROM image_providers LIMIT 1")     # 第一条自动当默认
    execute("INSERT INTO image_providers(kind, name, base_url, api_key, models, status,"
            " enabled, is_default, sizes, created_at, updated_at)"
            " VALUES(?,?,?,?, '[]', 'unknown', 1, ?, ?, ?, ?)",
            (kind, name, base_url, key, 0 if first else 1,
             json.dumps(sizes, ensure_ascii=False), now, now))
    row = one("SELECT id FROM image_providers ORDER BY id DESC LIMIT 1")
    return get_image_provider(row["id"])


def delete_image_provider(pid) -> bool:
    if not one("SELECT id FROM image_providers WHERE id=?", (int(pid),)):
        return False
    execute("DELETE FROM image_providers WHERE id=?", (int(pid),))
    return True


def set_image_models(pid, models: list[str], status: str, error: str = "",
                     default_model: str = "") -> dict | None:
    """存检测到的绘图模型：已有的沿用旧 rateId"""
    row = one("SELECT * FROM image_providers WHERE id=?", (int(pid),))
    if not row:
        return None
    old = [m for m in _as_list(row["models"]) if isinstance(m, dict)]
    by_name = {str(m.get("model") or ""): int(m.get("rateId") or 0) for m in old}
    used = {v for v in by_name.values() if v}
    cursor = IMAGE_RATE_BASE + int(pid or 1) * 1000 + 1
    merged = []
    seen = set()
    for mid in [str(x) for x in models if str(x)]:
        if mid in seen:
            continue
        seen.add(mid)
        rid = by_name.get(mid) or 0
        if not rid:
            while cursor in used:
                cursor += 1
            rid = cursor
            used.add(rid)
        merged.append({"model": mid, "rateId": rid})
    keep = str(default_model or "").strip()
    if keep and keep not in seen:
        keep = ""
    if not keep:
        keep = str(row["default_model"] or "") if str(row["default_model"] or "") in seen else ""
    if not keep and merged:
        keep = merged[0]["model"]
    if str(status or "").lower() == "error" and not merged and old:
        # 重新检测失败（断网 / 超时 / 上游临时 5xx）不要把上次拉回来的模型清掉，
        # 只把状态标成 error：否则聊天里的模型列表会因为一次抖动退回「本地兜底」
        merged = old
        keep = str(row["default_model"] or "")
        if not keep or keep not in {str(m.get("model") or "") for m in merged}:
            keep = str(merged[0].get("model") or "")
    now = now_iso()
    execute("UPDATE image_providers SET models=?, status=?, last_error=?, last_check_at=?,"
            " default_model=?, updated_at=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), str(status or "unknown"),
             str(error or "")[:500], now, keep, now, int(pid)))
    return get_image_provider(pid)


def set_image_active(pid, model: str = "") -> dict | None:
    pid = int(pid)
    row = one("SELECT id FROM image_providers WHERE id=?", (pid,))
    if not row:
        return None
    execute("UPDATE image_providers SET is_default=0")
    keep = ""
    if model:
        prov = get_image_provider(pid)
        names = [str(m.get("model") or "") for m in (prov or {}).get("models") or []]
        keep = str(model) if str(model) in names else ""
    execute("UPDATE image_providers SET is_default=1,"
            " default_model=CASE WHEN ?='' THEN default_model ELSE ? END WHERE id=?",
            (keep, keep, pid))
    return get_image_provider(pid)


def image_rate_models() -> list[dict]:
    """绘图页「选择模型」：所有接入里检测到的模型 + 本地占位兜底"""
    items = []
    for row in query("SELECT * FROM image_providers WHERE enabled=1"
                     " ORDER BY is_default DESC, id ASC"):
        models = [m for m in _as_list(row["models"]) if isinstance(m, dict)]
        for m in models:
            mid = str(m.get("model") or "")
            if not mid:
                continue
            items.append({
                "ID": int(m.get("rateId") or 0), "id": int(m.get("rateId") or 0),
                "modelID": mid, "modelId": mid, "modelName": mid,
                "description": row["name"], "providerName": row["name"],
                "providerId": int(row["id"]), "providerKind": row["kind"],
                "sizes": [str(s) for s in _as_list(row["sizes"]) if str(s).strip()],
                "isDefault": bool(int(row["is_default"] or 0))
                and str(row["default_model"] or "") == mid,
                "price": 0, "currency": "points", "isEnabled": True,
            })
    if not items:
        items.append({
            "ID": LOCAL_IMAGE_RATE_ID, "id": LOCAL_IMAGE_RATE_ID,
            "modelID": LOCAL_IMAGE_MODEL_ID, "modelId": LOCAL_IMAGE_MODEL_ID,
            "modelName": "本地占位绘图", "description": "未接绘图模型时可用（不联网）",
            "providerName": "本地", "providerId": 0, "providerKind": "local",
            "sizes": ["1024x1024", "1024x1536", "1536x1024", "512x512"],
            "isDefault": True, "price": 0, "currency": "points", "isEnabled": True,
        })
    return items


def resolve_image_rate_id(rate_id) -> tuple[dict, str] | None:
    """绘图页传来的 rateId → (接入行, 真实模型名)；本地占位返回 None"""
    try:
        rid = int(rate_id)
    except (TypeError, ValueError):
        return None
    if not rid or rid == LOCAL_IMAGE_RATE_ID:
        return None
    for row in query("SELECT * FROM image_providers WHERE enabled=1"):
        for m in _as_list(row["models"]):
            if isinstance(m, dict) and int(m.get("rateId") or 0) == rid:
                return row, str(m.get("model") or "")
    return None


# --------------------------------------------------------------- 视频模型接入（读写）

def _video_provider_view(row: dict) -> dict:
    out = dict(row)
    models = [m for m in _as_list(out.get("models")) if isinstance(m, dict)]
    out["models"] = models
    out["modelCount"] = len(models)
    key = str(out.get("api_key") or "")
    out["apiKeySet"] = bool(key)
    out["apiKeyMasked"] = _mask_key(key)
    out.pop("api_key", None)
    out["enabled"] = bool(int(out.get("enabled") or 0))
    out["isDefault"] = bool(int(out.get("is_default") or 0))
    specs = [str(s) for s in _as_list(out.get("specs")) if str(s).strip()]
    preset = VIDEO_PRESETS.get(str(out.get("kind") or ""), {})
    out["specs"] = specs or list(preset.get("specs") or ["5s", "10s"])
    out["preset"] = preset
    try:
        out["extra"] = json.loads(out.get("extra") or "{}")
    except (ValueError, TypeError):
        out["extra"] = {}
    return out


def list_video_providers() -> list[dict]:
    return [_video_provider_view(r) for r in
            query("SELECT * FROM video_providers ORDER BY is_default DESC, id ASC")]


def get_video_provider(pid) -> dict | None:
    row = one("SELECT * FROM video_providers WHERE id=?", (int(pid),)) if pid else None
    return _video_provider_view(row) if row else None


def video_credentials(pid) -> tuple[str, str] | None:
    row = one("SELECT base_url, api_key FROM video_providers WHERE id=?",
              (int(pid),)) if pid else None
    if not row:
        return None
    return str(row["base_url"] or "").rstrip("/"), str(row["api_key"] or "")


def save_video_provider(payload: dict) -> dict | None:
    """新建/更新一条视频接入。api_key 留空表示不改"""
    pid = payload.get("id")
    row = one("SELECT * FROM video_providers WHERE id=?", (int(pid),)) if pid else None
    kind = str(payload.get("kind") or (row["kind"] if row else "") or "openai")
    preset = VIDEO_PRESETS.get(kind, {})
    name = (str(payload.get("name") or "").strip()
            or str(row["name"] if row else "").strip()
            or str(preset.get("label") or kind))
    base_url = str(payload.get("base_url") or "").strip() or (row["base_url"] if row else "")
    if not base_url:
        base_url = str(preset.get("base_url") or "").strip()
    base_url = base_url.rstrip("/")
    if not base_url:
        return None
    key = str(payload.get("api_key") or "").strip()
    if not key and row:
        key = str(row["api_key"] or "")
    specs = payload.get("specs")
    if isinstance(specs, str):
        specs = [s.strip() for s in specs.replace("，", ",").split(",") if s.strip()]
    if not isinstance(specs, list) or not specs:
        specs = ([str(s) for s in _as_list(row["specs"])] if row else [])
    if not specs:
        specs = list(preset.get("specs") or ["5s", "10s"])
    now = now_iso()
    if row:
        execute("UPDATE video_providers SET kind=?, name=?, base_url=?, api_key=?,"
                " specs=?, updated_at=? WHERE id=?",
                (kind, name, base_url, key, json.dumps(specs, ensure_ascii=False),
                 now, row["id"]))
        return get_video_provider(row["id"])
    first = one("SELECT id FROM video_providers LIMIT 1")
    execute("INSERT INTO video_providers(kind, name, base_url, api_key, models, status,"
            " enabled, is_default, specs, created_at, updated_at)"
            " VALUES(?,?,?,?, '[]', 'unknown', 1, ?, ?, ?, ?)",
            (kind, name, base_url, key, 0 if first else 1,
             json.dumps(specs, ensure_ascii=False), now, now))
    row = one("SELECT id FROM video_providers ORDER BY id DESC LIMIT 1")
    return get_video_provider(row["id"])


def delete_video_provider(pid) -> bool:
    if not one("SELECT id FROM video_providers WHERE id=?", (int(pid),)):
        return False
    execute("DELETE FROM video_providers WHERE id=?", (int(pid),))
    return True


def set_video_models(pid, models: list[str], status: str, error: str = "",
                     default_model: str = "") -> dict | None:
    """存检测到的视频模型：已有的沿用旧 rateId；检测失败不清掉上次的列表"""
    row = one("SELECT * FROM video_providers WHERE id=?", (int(pid),))
    if not row:
        return None
    old = [m for m in _as_list(row["models"]) if isinstance(m, dict)]
    by_name = {str(m.get("model") or ""): int(m.get("rateId") or 0) for m in old}
    used = {v for v in by_name.values() if v}
    cursor = VIDEO_RATE_BASE + int(pid or 1) * 1000 + 1
    merged = []
    seen = set()
    for mid in [str(x) for x in models if str(x)]:
        if mid in seen:
            continue
        seen.add(mid)
        rid = by_name.get(mid) or 0
        if not rid:
            while cursor in used:
                cursor += 1
            rid = cursor
            used.add(rid)
        merged.append({"model": mid, "rateId": rid})
    keep = str(default_model or "").strip()
    if keep and keep not in seen:
        keep = ""
    if not keep:
        keep = str(row["default_model"] or "") if str(row["default_model"] or "") in seen else ""
    if not keep and merged:
        keep = merged[0]["model"]
    if str(status or "").lower() == "error" and not merged and old:
        merged = old
        keep = str(row["default_model"] or "")
        if not keep or keep not in {str(m.get("model") or "") for m in merged}:
            keep = str(merged[0].get("model") or "")
    now = now_iso()
    execute("UPDATE video_providers SET models=?, status=?, last_error=?, last_check_at=?,"
            " default_model=?, updated_at=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), str(status or "unknown"),
             str(error or "")[:500], now, keep, now, int(pid)))
    return get_video_provider(pid)


def set_video_active(pid, model: str = "") -> dict | None:
    pid = int(pid)
    row = one("SELECT id FROM video_providers WHERE id=?", (pid,))
    if not row:
        return None
    execute("UPDATE video_providers SET is_default=0")
    keep = ""
    if model:
        prov = get_video_provider(pid)
        names = [str(m.get("model") or "") for m in (prov or {}).get("models") or []]
        keep = str(model) if str(model) in names else ""
    execute("UPDATE video_providers SET is_default=1,"
            " default_model=CASE WHEN ?='' THEN default_model ELSE ? END WHERE id=?",
            (keep, keep, pid))
    return get_video_provider(pid)


def video_rate_models() -> list[dict]:
    """所有视频接入里检测到的模型（控制台展示用）"""
    items = []
    for row in query("SELECT * FROM video_providers WHERE enabled=1"
                     " ORDER BY is_default DESC, id ASC"):
        models = [m for m in _as_list(row["models"]) if isinstance(m, dict)]
        for m in models:
            mid = str(m.get("model") or "")
            if not mid:
                continue
            specs = [str(s) for s in _as_list(row["specs"]) if str(s).strip()]
            items.append({
                "ID": int(m.get("rateId") or 0), "id": int(m.get("rateId") or 0),
                "modelID": mid, "modelId": mid, "modelName": mid,
                "description": row["name"], "providerName": row["name"],
                "providerId": int(row["id"]), "providerKind": row["kind"],
                "specs": specs, "sizes": specs,
                "isDefault": bool(int(row["is_default"] or 0))
                and str(row["default_model"] or "") == mid,
                "price": 0, "currency": "points", "isEnabled": True,
            })
    return items


# --------------------------------------------------------------- 绘图作品（绘图广场）

def add_image_work(user_id, author: str, prompt: str, negative: str,
                   provider_id, provider: str, model: str, size: str,
                   url_key: str, image_id=None, kind: str = "image",
                   seed: int = 0) -> dict | None:
    execute("INSERT INTO image_works(user_id, author, prompt, negative, provider_id,"
            " provider, model, size, seed, url_key, image_id, kind, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, str(author or ""), str(prompt or "")[:2000], str(negative or "")[:1000],
             provider_id, str(provider or ""), str(model or ""), str(size or ""),
             int(seed or 0), url_key, image_id, kind, now_iso()))
    row = one("SELECT * FROM image_works ORDER BY id DESC LIMIT 1")
    return row


def list_image_works(sort: str = "new", limit: int = 60, offset: int = 0,
                     user_id=None) -> list[dict]:
    order = "likes DESC, id DESC" if str(sort) == "hot" else "id DESC"
    where = ""
    params: tuple = ()
    if user_id:
        where = " WHERE user_id=?"
        params = (int(user_id),)
    return query(f"SELECT * FROM image_works{where} ORDER BY {order} LIMIT ? OFFSET ?",
                 params + (int(limit), int(offset)))


def count_image_works(user_id=None) -> int:
    if user_id:
        row = one("SELECT COUNT(*) AS c FROM image_works WHERE user_id=?", (int(user_id),))
    else:
        row = one("SELECT COUNT(*) AS c FROM image_works")
    return int(row["c"]) if row else 0


def like_image_work(work_id) -> dict | None:
    execute("UPDATE image_works SET likes=likes+1 WHERE id=?", (int(work_id),))
    return one("SELECT * FROM image_works WHERE id=?", (int(work_id),))


def delete_image_work(work_id, user_id=None, is_admin: bool = False) -> bool:
    """管理员可删任意作品；普通用户只能删自己的"""
    row = one("SELECT user_id FROM image_works WHERE id=?", (int(work_id),))
    if not row:
        return False
    if not is_admin and int(row["user_id"] or 0) != int(user_id or 0):
        return False
    return bool(execute("DELETE FROM image_works WHERE id=?", (int(work_id),)))


# --------------------------------------------------------------- 绘图模板任务

def media_generation_get(gid: str) -> dict | None:
    return one("SELECT * FROM media_generations WHERE id=?", (str(gid or ""),))


def media_generation_by_key(user_id, idempotency_key: str) -> dict | None:
    """同一用户的同一个 Idempotency-Key → 原任务（前端重试不会重复扣图）"""
    key = str(idempotency_key or "").strip()
    if not key:
        return None
    return one("SELECT * FROM media_generations WHERE user_id IS ? AND idempotency_key=?"
               " ORDER BY created_at DESC LIMIT 1", (user_id, key))


def media_generation_create(user_id, gid: str, recipe_key: str, recipe_version: int,
                            prompt: str, negative: str, size: str, seed: int,
                            idempotency_key: str = "",
                            request: dict | None = None) -> dict | None:
    now = now_iso()
    execute("INSERT INTO media_generations(id, user_id, recipe_key, recipe_version,"
            " prompt, negative, size, seed, idempotency_key, request, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(gid), user_id, str(recipe_key or ""), int(recipe_version or 0),
             str(prompt or "")[:2000], str(negative or "")[:1000], str(size or ""),
             int(seed or 0), str(idempotency_key or ""),
             json.dumps(request or {}, ensure_ascii=False), now, now))
    return media_generation_get(gid)


def media_generation_set(gid: str, **fields) -> dict | None:
    """更新任务状态（status / asset_status / asset_url / model / image_work_id …）"""
    allowed = ("status", "asset_status", "asset_url", "prompt", "negative", "model",
               "size", "seed", "image_work_id", "charged_credits", "refunded",
               "failure_code", "failure_msg", "idempotency_key")
    sets: list[str] = []
    params: list = []
    for key in allowed:
        if key in fields:
            sets.append(f"{key}=?")
            params.append(fields[key])
    if not sets:
        return media_generation_get(gid)
    sets.append("updated_at=?")
    params.append(now_iso())
    execute(f"UPDATE media_generations SET {', '.join(sets)} WHERE id=?",
            tuple(params) + (str(gid),))
    return media_generation_get(gid)


# --------------------------------------------------------------- 创作：世界书

def _worldbook_row(user_id: int, entry_id: str) -> dict | None:
    row = one("SELECT entry_id, payload, position FROM worldbook_entries"
              " WHERE user_id=? AND entry_id=?", (user_id, entry_id))
    if not row:
        return None
    payload = _as_dict(row["payload"])
    payload["ID"] = row["entry_id"]
    payload.setdefault("worldbookID", row["entry_id"])
    return payload


def list_worldbook(user_id: int, character_id: str, custom: bool = False) -> list[dict]:
    rows = query(
        "SELECT entry_id, payload FROM worldbook_entries"
        " WHERE user_id=? AND character_id=? AND custom=?"
        " ORDER BY position, entry_id",
        (user_id, character_id, 1 if custom else 0))
    out = []
    for r in rows:
        payload = _as_dict(r["payload"])
        payload.setdefault("ID", r["entry_id"])
        payload.setdefault("worldbookID", r["entry_id"])
        payload["characterId"] = character_id
        payload.pop("characterID", None)
        keys = payload.get("keys")
        if isinstance(keys, list):               # 读回时要 {keys:[...]}
            payload["keys"] = {"keys": keys}
        out.append(payload)
    return out


def _next_worldbook_id(user_id: int) -> int:
    """世界书条目的 ID 是存在 payload 里的整数 → 取当前最大值 +1"""
    mx = 0
    for r in query("SELECT payload FROM worldbook_entries WHERE user_id=?", (user_id,)):
        try:
            n = int(_as_dict(r["payload"]).get("ID") or 0)
        except (TypeError, ValueError):  # noqa: PERF203
            n = 0
        mx = max(mx, n)
    return mx + 1


# 酒馆 / 风月卡世界书的 position 是字符串（before_char / after_char …），
# 这里统一成整数；不认识的一律 0，绝不因为一个位置字段把整条保存搞挂。
_WB_POSITIONS = {
    "before_char": 0, "before": 0, "before_an": 0, "at_start": 0,
    "after_char": 1, "after": 1, "after_an": 1, "at_end": 1,
}
_WB_POS_AFTER_NAMES = ("after_char", "after", "after_an", "at_end", "1")


def _wb_position(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    return _WB_POSITIONS.get(str(value or "").strip().lower(), 0)


def save_worldbook_entry(user_id: int, payload: dict, custom: bool = False,
                         entry_id: str = "") -> dict:
    """存一条世界书条目（create / update 共用）。

    - 普通条目以 `worldbookID` 为主键（前端 `id = e.worldbookID`）
    - 自定义条目以 `id` 为主键
    - `ID` 是前端要的 `primaryKeyId`，本地补一个递增整数
    """
    cid = str(payload.get("characterID") or payload.get("characterId") or "").strip()
    if entry_id:
        eid = str(entry_id).strip()
    elif custom:
        eid = str(payload.get("id") or payload.get("worldbookID")
                  or payload.get("ID") or "").strip()
    else:
        eid = str(payload.get("worldbookID") or "").strip()
    if not eid or eid == "None":
        eid = f"wb_{secrets.token_hex(8)}"
    row = one("SELECT payload, position FROM worldbook_entries"
              " WHERE user_id=? AND entry_id=?", (user_id, eid))
    merged = _as_dict(row["payload"]) if row else {}
    merged.update(payload)
    # 主键与 ID（ID 必须是正整数，前端按 primaryKeyId 对账）
    rid = merged.get("ID")
    if not (isinstance(rid, int) and rid > 0):
        try:
            rid = int(str(rid)) if str(rid).strip().isdigit() else 0
        except (TypeError, ValueError):  # noqa: PERF203
            rid = 0
    if rid <= 0:
        rid = _next_worldbook_id(user_id)
    merged["ID"] = rid
    if not custom:
        merged["worldbookID"] = eid
    else:
        merged.setdefault("worldbookID", eid)
    merged["id"] = merged.get("id") or eid
    if cid:
        merged["characterId"] = cid
        merged.pop("characterID", None)
    # 世界书字段落地（旧 payload 没写 → 写回时补默认值，便于引擎直接读）
    merged.setdefault("constant", False)
    merged.setdefault("probability", 100)
    try:
        merged["order"] = int(merged.get("order") or 0)
    except (TypeError, ValueError):
        merged["order"] = 0
    try:
        merged["depth"] = int(merged.get("depth") or 0)
    except (TypeError, ValueError):
        merged["depth"] = 0
    merged.setdefault("group", "")
    merged.setdefault("recursive", False)
    pos_raw = merged.get("position") or merged.get("pos") or "before_char"
    merged["position"] = "at_depth" if str(pos_raw).strip().lower() in (
        "at_depth", "depth", "2") else (
        "after_char" if str(pos_raw).strip().lower() in _WB_POS_AFTER_NAMES
        else "before_char")
    now = now_iso()
    raw_pos = payload.get("order")
    if raw_pos in (None, ""):
        raw_pos = payload.get("position")
    pos = row["position"] if row else _wb_position(raw_pos)
    blob = json.dumps(merged, ensure_ascii=False)
    if row:
        execute("UPDATE worldbook_entries SET payload=?, character_id=?, updated_at=?"
                " WHERE user_id=? AND entry_id=?",
                (blob, cid or None, now, user_id, eid))
    else:
        execute("INSERT INTO worldbook_entries(user_id, entry_id, character_id, custom,"
                " payload, position, updated_at) VALUES(?,?,?,?,?,?,?)",
                (user_id, eid, cid or None, 1 if custom else 0, blob, pos, now))
    return merged


def delete_worldbook_entry(user_id: int, payload: dict, custom: bool = False,
                           entry_id: str = "") -> bool:
    if not isinstance(payload, dict):
        return False
    eid = str(entry_id or "").strip() or str(
        payload.get("worldbookID") or payload.get("id")
        or payload.get("ID") or "").strip()
    if not eid or eid == "None":
        return False
    n = execute("DELETE FROM worldbook_entries WHERE user_id=? AND entry_id=?",
                (user_id, eid))
    return bool(n)


# ------------------------------------------------- 平台数值规则（JSON 存储）
# 每个属性一行：stat_rule_docs(stat, payload, position)
#   payload = {"keywords": [...], "rules": [{range, emotion, behavior}, ...]}
# 属性之间彼此独立，改一个属性只重写它自己那一行。


def _parse_stat_doc(payload) -> dict:
    try:
        entry = json.loads(payload or "{}")
    except (ValueError, TypeError):
        return {}
    return entry if isinstance(entry, dict) else {}


def _scope_id(character_id=None) -> str:
    """规则作用域：空 / None / global 都视为平台全局。"""
    cid = str(character_id or "").strip()
    return "global" if not cid or cid.lower() == "global" else cid


def _stat_doc(stat: str, character_id=None) -> dict | None:
    """读某个作用域下某个属性的 JSON；不存在时返回 None。"""
    row = one("SELECT payload FROM stat_rule_docs WHERE character_id=? AND stat=?",
              (_scope_id(character_id), stat))
    if not row:
        return None
    entry = _parse_stat_doc(row["payload"])
    entry.setdefault("keywords", [])
    entry.setdefault("rules", [])
    return entry


def _save_stat_doc_row(stat: str, entry: dict, position: int | None = None,
                       character_id=None) -> None:
    """写回某个属性；position 不传就沿用原位置，新属性排到末尾。"""
    cid = _scope_id(character_id)
    if position is None:
        row = one("SELECT position FROM stat_rule_docs WHERE character_id=? AND stat=?",
                  (cid, stat))
        if row:
            position = int(row["position"] or 0)
        else:
            last = one("SELECT COALESCE(MAX(position), -1) AS p FROM stat_rule_docs"
                       " WHERE character_id=?", (cid,))
            position = int(last["p"] if last and last["p"] is not None else -1) + 1
    execute("INSERT OR REPLACE INTO stat_rule_docs"
            "(character_id, stat, payload, position, updated_at)"
            " VALUES(?,?,?,?,?)",
            (cid, stat, json.dumps(entry, ensure_ascii=False, indent=2),
             position, now_iso()))


def _delete_stat_doc_row(stat: str, character_id=None) -> None:
    execute("DELETE FROM stat_rule_docs WHERE character_id=? AND stat=?",
            (_scope_id(character_id), stat))


def _doc_rules(entry) -> list[dict]:
    rows = entry.get("rules") if isinstance(entry, dict) else None
    return rows if isinstance(rows, list) else []


def _legacy_stat_doc() -> dict:
    """更早版本两张表（stat_rules + stat_rule_meta）还原成 {属性: entry}。"""
    legacy = {r["name"] for r in query(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('stat_rules','stat_rule_meta')")}
    doc: dict[str, dict] = {}
    if "stat_rules" in legacy:
        for r in query("SELECT stat, range, emotion, behavior FROM stat_rules"
                       " ORDER BY stat, position"):
            entry = doc.setdefault(r["stat"], {"keywords": [], "rules": []})
            entry["rules"].append({"range": r["range"], "emotion": r["emotion"],
                                   "behavior": r["behavior"]})
    if "stat_rule_meta" in legacy:
        for r in query("SELECT stat, keywords FROM stat_rule_meta"):
            entry = doc.setdefault(r["stat"], {"keywords": [], "rules": []})
            entry["keywords"] = [x.strip() for x in
                                 str(r["keywords"] or "").split(",") if x.strip()]
    return doc


def _create_stat_doc_table() -> None:
    execute("CREATE TABLE IF NOT EXISTS stat_rule_docs ("
            "character_id TEXT NOT NULL DEFAULT 'global',"
            " stat TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',"
            " position INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,"
            " PRIMARY KEY (character_id, stat))")


def _migrate_stat_rule_docs() -> None:
    """旧结构一次性迁移。

    · 老表没有 character_id → 重建为 (character_id, stat) 复合主键，旧数据归入 global
    · 上一版：整份规则存在 id=1 的单行里 → 拆成每属性一行
    · 更早版本：stat_rules + stat_rule_meta 两张表 → 合并后写入
    """
    cols = {r["name"] for r in query("PRAGMA table_info(stat_rule_docs)")}
    if not cols:
        return
    if "character_id" not in cols:
        doc: dict[str, dict] = {}
        if "stat" in cols:
            for r in query("SELECT stat, payload FROM stat_rule_docs"
                           " ORDER BY position, stat"):
                entry = _parse_stat_doc(r["payload"])
                if isinstance(entry, dict):
                    doc[r["stat"]] = entry
        else:
            for r in query("SELECT payload FROM stat_rule_docs"):
                for stat, entry in _parse_stat_doc(r["payload"]).items():
                    if isinstance(entry, dict):
                        doc.setdefault(stat, entry)
        if not doc:
            doc = _legacy_stat_doc()
        execute("DROP TABLE stat_rule_docs")
        _create_stat_doc_table()
        for pos, (stat, entry) in enumerate(doc.items()):
            _save_stat_doc_row(stat, entry, pos)
    legacy = _legacy_stat_doc()
    if legacy:
        have = {r["stat"] for r in query(
            "SELECT stat FROM stat_rule_docs WHERE character_id='global'")}
        for stat, entry in legacy.items():
            if stat not in have:
                _save_stat_doc_row(stat, entry)
    execute("DROP TABLE IF EXISTS stat_rules")
    execute("DROP TABLE IF EXISTS stat_rule_meta")


def _stat_meta_of(entry) -> dict:
    """提取属性级的初始值 / 上下限（不参与档位匹配，只用于赋值和截断）。"""
    meta = {}
    for key in ("initial_value", "min_value", "max_value"):
        v = entry.get(key) if isinstance(entry, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            meta[key] = int(v) if float(v).is_integer() else v
    return meta


def _merged_docs(character_id=None) -> dict[str, dict]:
    """全局 + 角色专属合并：同名属性角色覆盖全局，全局没有的属性直接追加。"""
    cid = _scope_id(character_id)
    doc: dict[str, dict] = {}
    for r in query("SELECT stat, payload FROM stat_rule_docs"
                   " WHERE character_id='global' ORDER BY position, stat"):
        doc[r["stat"]] = _parse_stat_doc(r["payload"])
    if cid != "global":
        for r in query("SELECT stat, payload FROM stat_rule_docs"
                       " WHERE character_id=? ORDER BY position, stat", (cid,)):
            doc[r["stat"]] = _parse_stat_doc(r["payload"])
    return doc


def list_stat_rule_meta(character_id=None) -> dict[str, dict]:
    """{属性名: {initial_value, min_value, max_value}}，只返回配过这些字段的属性。"""
    out: dict[str, dict] = {}
    for stat, entry in _merged_docs(character_id).items():
        meta = _stat_meta_of(entry)
        if meta:
            out[stat] = meta
    return out


def list_stat_rules(character_id=None) -> dict[str, list[dict]]:
    """{属性名: [{range, emotion, behavior}]}，按 position、档位原顺序。"""
    out: dict[str, list[dict]] = {}
    for stat, entry in _merged_docs(character_id).items():
        rows = _doc_rules(entry)
        if not rows:
            continue
        out[stat] = [{"range": str(x.get("range") or ""),
                      "emotion": str(x.get("emotion") or ""),
                      "behavior": str(x.get("behavior") or "")}
                     for x in rows if isinstance(x, dict)]
    return out


def list_stat_rule_keywords(character_id=None) -> dict[str, list[str]]:
    """{属性名: [关键词]}；没有配置关键词的属性不返回（调用方按常驻处理）。"""
    out: dict[str, list[str]] = {}
    for stat, entry in _merged_docs(character_id).items():
        kws = entry.get("keywords")
        clean = [str(k).strip() for k in kws if str(k).strip()] \
            if isinstance(kws, list) else []
        if clean:
            out[stat] = clean
    return out


def stat_rule_scope(character_id=None) -> str:
    """该角色是否已建立专属规则快照：custom / global。"""
    cid = _scope_id(character_id)
    if cid == "global":
        return "global"
    row = one("SELECT 1 FROM stat_rule_docs WHERE character_id=? LIMIT 1", (cid,))
    return "custom" if row else "global"


def list_stat_rule_overrides(character_id=None) -> list[str]:
    """该角色单独存在的属性名（用于面板标记「已覆盖」）。"""
    cid = _scope_id(character_id)
    if cid == "global":
        return []
    return [r["stat"] for r in query(
        "SELECT stat FROM stat_rule_docs WHERE character_id=? ORDER BY position, stat",
        (cid,))]


def set_stat_rule_scope(character_id, scope: str) -> str:
    """切到自定义：把全局规则复制成该角色的快照；切回全局：删掉角色快照。"""
    cid = _scope_id(character_id)
    if cid == "global":
        raise ValueError("全局规则不支持切换作用域")
    want = "custom" if str(scope) == "custom" else "global"
    if want == stat_rule_scope(cid):
        return want   # 已经在该作用域，不重复复制 / 删除
    if want == "custom":
        for r in query("SELECT stat, payload, position FROM stat_rule_docs"
                       " WHERE character_id='global' ORDER BY position, stat"):
            _save_stat_doc_row(r["stat"], _parse_stat_doc(r["payload"]),
                               int(r["position"] or 0), cid)
    else:
        execute("DELETE FROM stat_rule_docs WHERE character_id=?", (cid,))
    return stat_rule_scope(cid)


def seed_stat_rules(stat: str, rules: list[dict],
                    keywords: list[str] | None = None,
                    meta: dict | None = None, character_id=None) -> int:
    """该属性在库里还没有任何档位时才导入（之后以库里的 JSON 为准）。"""
    stat = str(stat or "").strip()
    if not stat:
        return 0
    if _doc_rules(_stat_doc(stat, character_id)):
        return 0
    rows = [{"range": str(r.get("range")),
             "emotion": str(r.get("emotion") or ""),
             "behavior": str(r.get("behavior") or "")}
            for r in (rules or [])
            if isinstance(r, dict) and r.get("range")]
    if not rows:
        return 0
    entry = {
        "keywords": [str(k).strip() for k in (keywords or []) if str(k).strip()],
        "rules": rows,
    }
    entry.update(_stat_meta_of(meta))
    _save_stat_doc_row(stat, entry, character_id=character_id)
    return len(rows)


def save_stat_rule(stat: str, range_text: str, emotion: str = "",
                   behavior: str = "", keywords: list[str] | None = None,
                   old_stat: str = "", old_range: str = "",
                   character_id=None) -> dict:
    """新增 / 修改一档平台数值规则（只写该属性那一行 JSON）。

    old_stat + old_range 是编辑前的定位键：属性名或区间变了就先移除旧档位，
    再按原顺序写回，其他属性 / 档位不受影响。
    keywords=None 表示不改关键词；传空列表表示清空（该属性回到常驻注入）。
    """
    stat = str(stat or "").strip()
    range_text = str(range_text or "").strip()
    old_stat = str(old_stat or "").strip()
    old_range = str(old_range or "").strip()
    if not stat or not range_text:
        raise ValueError("属性名和数值区间都不能为空")

    if old_stat and old_range and (old_stat, old_range) != (stat, range_text):
        old = _stat_doc(old_stat, character_id)
        if old is not None:
            old["rules"] = [r for r in _doc_rules(old)
                            if str(r.get("range") or "") != old_range]
            if old["rules"]:
                _save_stat_doc_row(old_stat, old, character_id=character_id)
            else:
                _delete_stat_doc_row(old_stat, character_id)

    entry = _stat_doc(stat, character_id) or {"keywords": [], "rules": []}
    rows = entry.get("rules")
    if not isinstance(rows, list):
        entry["rules"] = rows = []

    value = {"range": range_text, "emotion": str(emotion or "").strip(),
             "behavior": str(behavior or "").strip()}
    hit = next((r for r in rows if isinstance(r, dict)
                and str(r.get("range") or "") == range_text), None)
    if hit is None:
        rows.append(value)          # 新档位排在末尾
    else:
        hit.update(value)           # 改内容时保持原顺序

    if keywords is not None:
        entry["keywords"] = [str(k).strip() for k in keywords if str(k).strip()]
    _save_stat_doc_row(stat, entry, character_id=character_id)

    return {
        "stat": stat, "range": range_text,
        "emotion": value["emotion"], "behavior": value["behavior"],
        "keywords": list(entry.get("keywords") or []),
        "meta": _stat_meta_of(entry),
    }


def save_stat_meta(stat: str, initial_value, min_value, max_value,
                   character_id=None) -> dict:
    """修改属性的初始值与上下限（档位、关键词保持不动）。"""
    stat = str(stat or "").strip()
    entry = _stat_doc(stat, character_id)
    if entry is None:
        raise ValueError("该属性还没有任何规则档位")
    try:
        iv, lo, hi = int(initial_value), int(min_value), int(max_value)
    except (TypeError, ValueError):
        raise ValueError("初始值、最小值、最大值都必须是整数") from None
    if lo > hi:
        raise ValueError("最小值不能大于最大值")
    if not lo <= iv <= hi:
        raise ValueError("初始值必须落在最小值与最大值之间")
    entry.update({"initial_value": iv, "min_value": lo, "max_value": hi})
    _save_stat_doc_row(stat, entry, character_id=character_id)
    return _stat_meta_of(entry)


def delete_stat_rule(stat: str, range_text: str, character_id=None) -> int:
    """删除一档；该属性最后一档被删掉时整行（含关键词）从库里移除。"""
    stat = str(stat or "").strip()
    entry = _stat_doc(stat, character_id)
    if entry is None:
        return 0
    rows = _doc_rules(entry)
    kept = [r for r in rows if str(r.get("range") or "") != str(range_text or "").strip()]
    n = len(rows) - len(kept)
    if not n:
        return 0
    if kept:
        entry["rules"] = kept
        _save_stat_doc_row(stat, entry, character_id=character_id)
    else:
        _delete_stat_doc_row(stat, character_id)
    return n


# ------------------------------------------------- 控制台：创作数据（作品 + 世界书）
# 控制台「创作数据」栏专用：按用户 / 状态列作品、读整包 + 世界书、导入导出 JSON。
# 数据只来自 my_works / worldbook_entries（创作页保存的就是这两张表）。

CREATION_SCHEMA = "missai.creation/v1"


def creation_users() -> list[dict]:
    """作者下拉：每个本地账号 + 作品数 / 已发布数"""
    return query("""
        SELECT u.id, u.user_name, u.nick_name, u.role,
               (SELECT COUNT(*) FROM my_works w WHERE w.user_id=u.id) AS works,
               (SELECT COUNT(*) FROM my_works w WHERE w.user_id=u.id
                  AND w.status='published') AS published
          FROM users u ORDER BY u.id""")


def creation_overview() -> dict:
    """卡片统计：作品 / 状态分布 / 世界书条目 / 作者数"""
    row = one("""
        SELECT COUNT(*) AS works,
               SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) AS published,
               SUM(CASE WHEN status='draft' THEN 1 ELSE 0 END) AS draft,
               SUM(CASE WHEN status='scheduled' THEN 1 ELSE 0 END) AS scheduled,
               SUM(CASE WHEN status='archived' THEN 1 ELSE 0 END) AS archived,
               COUNT(DISTINCT user_id) AS authors,
               MAX(updated_at) AS last_update
          FROM my_works""") or {}
    wb = one("""
        SELECT COUNT(*) AS entries,
               SUM(CASE WHEN custom=1 THEN 1 ELSE 0 END) AS custom,
               COUNT(DISTINCT character_id) AS works
          FROM worldbook_entries WHERE user_id > 0""") or {}
    return {
        "works": int(row.get("works") or 0),
        "published": int(row.get("published") or 0),
        "draft": int(row.get("draft") or 0),
        "scheduled": int(row.get("scheduled") or 0),
        "archived": int(row.get("archived") or 0),
        "authors": int(row.get("authors") or 0),
        "entries": int(wb.get("entries") or 0),
        "customEntries": int(wb.get("custom") or 0),
        "lastUpdate": row.get("last_update"),
    }


def creation_works(user_id: int = 0, q: str = "", status: str = "",
                   page: int = 1, page_size: int = 20) -> dict:
    """作品列表（控制台）：标题 / 作者 / 状态 / 世界书条数"""
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 200))
    where, params = [], []
    if int(user_id or 0) > 0:
        where.append("w.user_id=?")
        params.append(int(user_id))
    if status in WORK_STATUSES:
        where.append("w.status=?")
        params.append(status)
    if q:
        like = f"%{str(q)[:80]}%"
        where.append("(w.title LIKE ? OR w.character_id LIKE ? OR w.summary LIKE ?)")
        params += [like, like, like]
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    base = ("SELECT w.user_id, w.character_id, w.status, w.title, w.summary,"
            " w.cover_url, w.scheduled_at, w.created_at, w.updated_at,"
            " u.user_name, u.nick_name,"
            " (SELECT COUNT(*) FROM worldbook_entries e"
            "   WHERE e.user_id=w.user_id AND e.character_id=w.character_id) AS wb_count,"
            " (SELECT COUNT(*) FROM worldbook_entries e"
            "   WHERE e.user_id=w.user_id AND e.character_id=w.character_id"
            "     AND e.custom=1) AS wb_custom"
            " FROM my_works w LEFT JOIN users u ON u.id=w.user_id" + clause)
    total = int((one(f"SELECT COUNT(*) AS c FROM ({base})", tuple(params)) or {})
                .get("c") or 0)
    rows = query(base + " ORDER BY w.updated_at DESC LIMIT ? OFFSET ?",
                 tuple(params) + (page_size, (page - 1) * page_size))
    items = []
    for r in rows:
        items.append({
            "userId": int(r["user_id"]),
            "user": {"id": int(r["user_id"]),
                     "name": r.get("nick_name") or r.get("user_name")
                             or f"用户 #{r['user_id']}"},
            "characterId": r["character_id"],
            "status": r.get("status"),
            "title": r.get("title") or "",
            "summary": r.get("summary") or "",
            "coverURL": r.get("cover_url") or "",
            "scheduledAt": r.get("scheduled_at"),
            "createdAt": r.get("created_at"),
            "updatedAt": r.get("updated_at"),
            "worldbookCount": int(r.get("wb_count") or 0),
            "worldbookCustom": int(r.get("wb_custom") or 0),
        })
    return {"list": items, "total": total, "page": page, "pageSize": page_size,
            "pages": max(1, (total + page_size - 1) // page_size)}


def creation_work_detail(user_id: int, character_id: str) -> dict | None:
    """单件作品：整包 + 世界书条目（导入导出都以这个形状为准）"""
    row = one("SELECT * FROM my_works WHERE user_id=? AND character_id=?",
              (int(user_id), str(character_id)))
    if not row:
        return None
    user = one("SELECT id, user_name, nick_name FROM users WHERE id=?", (int(user_id),)) or {}
    refs = None
    if row.get("refs"):
        try:
            refs = json.loads(row["refs"])
        except (TypeError, ValueError):  # noqa: PERF203
            refs = None
    entries = query(
        "SELECT entry_id, character_id, custom, payload, position, updated_at"
        " FROM worldbook_entries WHERE user_id=? AND character_id=?"
        " ORDER BY custom, position, entry_id", (int(user_id), str(character_id)))
    return {
        "userId": int(user_id),
        "user": {"id": int(user_id),
                 "name": user.get("nick_name") or user.get("user_name")
                         or f"用户 #{user_id}"},
        "characterId": row["character_id"],
        "status": row.get("status"),
        "title": row.get("title") or "",
        "summary": row.get("summary") or "",
        "coverURL": row.get("cover_url") or "",
        "scheduledAt": row.get("scheduled_at"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
        "refs": refs,
        "payload": _as_dict(row.get("payload")),
        "worldbook": [{
            "entryId": e["entry_id"],
            "custom": bool(e["custom"]),
            "position": e["position"],
            "updatedAt": e["updated_at"],
            "payload": _as_dict(e.get("payload")),
        } for e in entries],
    }


def creation_export(user_id: int = 0, character_ids: list | None = None,
                    scope: str = "full") -> dict:
    """导出成一份可以原样再导入的 JSON；scope=worldbook 时只带世界书"""
    scope = "worldbook" if str(scope) == "worldbook" else "full"
    where, params = [], []
    if int(user_id or 0) > 0:
        where.append("user_id=?")
        params.append(int(user_id))
    ids = [str(x).strip() for x in (character_ids or []) if str(x).strip()]
    if ids:
        where.append("character_id IN (" + ",".join("?" for _ in ids) + ")")
        params += ids
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = query("SELECT user_id, character_id FROM my_works" + clause
                 + " ORDER BY user_id, updated_at DESC", tuple(params))
    works, entries_total = [], 0
    for r in rows:
        detail = creation_work_detail(int(r["user_id"]), r["character_id"])
        if not detail:
            continue
        entries_total += len(detail["worldbook"])
        item = {k: detail[k] for k in (
            "user", "userId", "characterId", "status", "title", "summary",
            "coverURL", "scheduledAt", "createdAt", "updatedAt", "worldbook")}
        if scope == "full":
            item["payload"] = detail["payload"]
            item["refs"] = detail["refs"]
        works.append(item)
    return {
        "schema": CREATION_SCHEMA,
        "exportedAt": now_iso(),
        "scope": scope,
        "counts": {"works": len(works), "entries": entries_total},
        "works": works,
    }


def _looks_like_entry(obj) -> bool:
    """世界书条目：有 keys / worldbookID / entryId，或只有 content 没有作品字段"""
    if not isinstance(obj, dict):
        return False
    if "keys" in obj or "worldbookID" in obj or "entryId" in obj:
        return True
    return ("content" in obj and "characterID" not in obj
            and "characterId" not in obj and "name" not in obj)


def _creation_payload(obj: dict) -> dict:
    """作品整包：payload 包着就用 payload，否则整个对象当整包

    导出时的外层字段（user / status / title / summary / coverURL …）不算作品内容，
    否则「只导世界书」那份会被误当成作品包。
    """
    inner = obj.get("payload")
    if isinstance(inner, dict):
        return dict(inner)
    skip = ("worldbook", "entries", "user", "userId", "createdAt", "updatedAt",
            "scheduledAt", "refs", "status", "title", "summary", "coverURL",
            "scope", "counts", "exportedAt", "schema")
    return {k: v for k, v in obj.items() if k not in skip}


def _creation_pack(obj: dict) -> dict:
    payload = _creation_payload(obj)
    cid = str(obj.get("characterID") or obj.get("characterId") or ""
              or payload.get("characterID") or payload.get("characterId")
              or payload.get("workID") or "").strip()
    raw = obj.get("worldbook")
    if not isinstance(raw, list):
        raw = obj.get("entries")
    entries = [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
    if not entries and _looks_like_entry(obj):
        # 裸条目（数组里每条一个世界书条目）→ 条目自己就是内容
        entries.append({k: v for k, v in obj.items()
                        if k not in ("characterID", "characterId")})
    body = bool(isinstance(obj.get("payload"), dict) or any(payload.get(k) for k in (
        "name", "characterName", "firstMes", "description", "personality",
        "scenario", "mesExample", "customCSS")))
    return {
        "user": obj.get("user") if isinstance(obj.get("user"), dict) else None,
        "userId": int(obj.get("userId") or 0),
        "characterId": cid,
        "status": str(obj.get("status") or ""),
        "body": bool(body or isinstance(obj.get("payload"), dict)),
        "payload": payload,
        "refs": obj.get("refs"),
        "worldbook": entries,
    }


def _creation_merge(items: list[dict]) -> list[dict]:
    """同一 characterId 的多条记录（作品 + 若干条目）合并成一条"""
    merged, index = [], {}
    for it in items:
        cid = it.get("characterId") or it.get("characterID") or ""
        if cid and cid in index:
            tgt = index[cid]
            tgt["worldbook"].extend(it.get("worldbook") or [])
            if it.get("body") and not tgt.get("body"):
                tgt["body"] = True
                tgt["payload"] = it.get("payload") or {}
                tgt["status"] = it.get("status") or tgt.get("status") or ""
                tgt["refs"] = it.get("refs") or tgt.get("refs")
            continue
        if cid:
            index[cid] = it
        merged.append(it)
    return merged


def _creation_normalize(data) -> tuple[list[dict], list[str]]:
    """把各种形状的导入数据归一成 [作品]：

    - 控制台导出的整包：{schema, works:[{characterId, payload, worldbook:[…]}]}
    - 单个作品对象：{characterId|workID, name, firstMes, …}
    - 只有世界书：{characterId, worldbook / entries:[…]} 或裸条目数组（需带 characterId）
    """
    warnings: list[str] = []
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict):
        works = data.get("works")
        if isinstance(works, list):
            items = [_creation_pack(w) for w in works if isinstance(w, dict)]
        elif _looks_like_entry(data):
            warnings.append("文件里只有一条世界书条目，缺少所属作品的 characterId")
            items = []
        else:
            items = [_creation_pack(data)]
    elif isinstance(data, list):
        items = [_creation_pack(x) for x in data if isinstance(x, dict)]
        if items and all(not x["body"] and not x["characterId"] for x in items):
            warnings.append("文件是裸世界书条目数组，缺少所属作品的 characterId")
            items = []
    else:
        raise ValueError("不是可识别的 JSON 结构（要对象或数组）")
    return _creation_merge(items), warnings


def creation_preview(data) -> dict:
    """导入前预览：会写几个作品、多少条目、缺什么（不落库）"""
    items, warnings = _creation_normalize(data)
    out = {"works": 0, "entries": 0, "total": len(items),
           "warnings": warnings, "items": []}
    for it in items:
        payload = it["payload"] if it["body"] else {}
        title = str(payload.get("name") or payload.get("characterName") or "")
        out["items"].append({
            "characterId": it["characterId"] or "（缺 characterId）",
            "title": title or "（只导世界书）",
            "status": it["status"] or "—",
            "body": bool(it["body"]),
            "worldbook": len(it["worldbook"]),
        })
        if it["body"]:
            out["works"] += 1
        out["entries"] += len(it["worldbook"])
    return out


def replace_work(user_id: int, character_id: str, payload: dict) -> dict | None:
    """整包覆盖（控制台导入用；created_at 保留）"""
    cid = str(character_id or work_identity(payload) or "").strip()
    if not cid:
        return None
    payload = dict(payload or {})
    payload.pop("characterID", None)
    payload["characterId"] = cid
    status = str(payload.get("publishStatus") or "").strip()
    if status not in WORK_STATUSES:
        status = "draft"
    now = now_iso()
    title = str(payload.get("name") or "")[:200]
    summary = str(payload.get("summary") or "")[:1000]
    cover = str(payload.get("avatarURL") or payload.get("coverURL") or "")[:500]
    blob = json.dumps(payload, ensure_ascii=False)
    row = one("SELECT 1 AS ok FROM my_works WHERE user_id=? AND character_id=?",
              (user_id, cid))
    if row:
        execute("UPDATE my_works SET status=?, title=?, summary=?, cover_url=?,"
                " payload=?, updated_at=? WHERE user_id=? AND character_id=?",
                (status, title, summary, cover, blob, now, user_id, cid))
    else:
        execute("INSERT INTO my_works(user_id, character_id, status, title, summary,"
                " cover_url, scheduled_at, payload, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (user_id, cid, status, title, summary, cover, None, blob, now, now))
    payload["updatedAt"] = now
    return payload


def creation_import(data, mode: str = "merge", target_user_id: int = 0,
                    actor_id: int | None = None) -> dict:
    """导入创作数据：mode=merge 合并（同 characterId 覆盖字段）/ replace 整包覆盖。

    归属用户优先级：target_user_id > 文件里的 userId > actor_id > 第一个管理员。
    """
    items, warnings = _creation_normalize(data)
    mode = "replace" if str(mode) == "replace" else "merge"
    if not items:
        raise ValueError("; ".join(warnings) or "没有可导入的作品")
    fallback = int(target_user_id or actor_id or 0)
    if not fallback:
        admin = one("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1")
        fallback = int(admin["id"]) if admin else 0
    report = {"mode": mode, "created": 0, "replaced": 0, "works": 0,
              "entries": 0, "skipped": [], "warnings": list(warnings), "items": []}
    for it in items:
        uid = int(target_user_id or it.get("userId") or 0)
        if uid and not one("SELECT 1 AS ok FROM users WHERE id=?", (uid,)):
            report["warnings"].append(f"用户 #{uid} 不存在，改写到当前账号")
            uid = 0
        uid = uid or fallback
        if not uid:
            report["skipped"].append("没有可写入的用户")
            continue
        cid = it["characterId"]
        if not cid:
            cid = f"char-{secrets.token_hex(8)}"
            report["warnings"].append(f"作品缺 characterId，已生成 {cid}")
        payload = dict(it["payload"] or {})
        payload.pop("characterID", None)
        payload["characterId"] = cid
        status = it["status"] if it["status"] in WORK_STATUSES else ""
        existing = one("SELECT 1 AS ok FROM my_works WHERE user_id=? AND character_id=?",
                       (uid, cid))
        if it["body"]:
            if mode == "replace":
                replace_work(uid, cid, payload)
            elif save_work(uid, payload) is None:
                report["skipped"].append(f"{cid}: 作品保存失败")
                continue
            if status:
                set_work_status(uid, cid, status)
            report["works"] += 1
            report["replaced" if existing else "created"] += 1
        elif not existing:
            report["skipped"].append(f"{cid}: 作品还不存在，世界书未写入")
            continue
        entries = [e for e in it["worldbook"] if isinstance(e, dict)]
        if entries and mode == "replace":
            execute("DELETE FROM worldbook_entries WHERE user_id=? AND character_id=?",
                    (uid, cid))
        for raw in entries:
            inner = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
            entry = dict(inner)
            entry.pop("characterID", None)
            entry["characterId"] = cid
            eid = str(raw.get("entryId") or entry.get("entryId") or ""
                      or entry.get("worldbookID") or entry.get("id") or "").strip()
            custom = bool(raw.get("custom")) if "custom" in raw \
                else bool(entry.get("custom"))
            if save_worldbook_entry(uid, entry, custom=custom, entry_id=eid):
                report["entries"] += 1
        report["items"].append({"characterId": cid,
                                "title": str(payload.get("name") or "")[:80],
                                "worldbook": len(entries)})
    return report


def creation_delete_work(user_id: int, character_id: str) -> dict:
    """控制台删除作品：作品 + 世界书 + 公开库投影（和控制台外的删除走同一套）"""
    ok, entries = _purge_work(int(user_id), character_id)
    return {"work": ok, "entries": entries}


# --------------------------------------------------------------- 世界书镜像（从源站同步）

def worldbook_mirror_save(character_id: str, entries: list[dict], custom: bool = False) -> int:
    """镜像世界书条目：把从源站抓到的条目存进 worldbook_entries（user_id=0 表示系统镜像）。
    返回写入的条目数。"""
    if not entries or not isinstance(entries, list):
        return 0
    n = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        eid = str(entry.get("worldbookID") or entry.get("id") or entry.get("ID") or "")
        if not eid or eid == "None":
            continue
        payload = json.dumps(entry, ensure_ascii=False)
        position = int(entry.get("position") or 0)
        try:
            execute("""
                INSERT INTO worldbook_entries(user_id, entry_id, character_id, custom, payload, position, updated_at)
                VALUES(0, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, entry_id) DO UPDATE SET
                    character_id=excluded.character_id,
                    custom=excluded.custom,
                    payload=excluded.payload,
                    position=excluded.position,
                    updated_at=excluded.updated_at
            """, (eid, character_id, 1 if custom else 0, payload, position, now_iso()))
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


def worldbook_mirror_get(character_id: str, custom: bool = False) -> list[dict]:
    """读取镜像的世界书条目（user_id=0 的系统镜像数据）。"""
    rows = query("""
        SELECT entry_id, payload, position FROM worldbook_entries
        WHERE user_id=0 AND character_id=? AND custom=?
        ORDER BY position
    """, (character_id, 1 if custom else 0))
    result = []
    for r in rows:
        try:
            entry = json.loads(r["payload"]) if r.get("payload") else {}
            entry.setdefault("worldbookID", r["entry_id"])
            entry.setdefault("ID", r["entry_id"])
            result.append(entry)
        except (TypeError, ValueError):
            pass
    return result


def character_image_urls(character_id: str) -> list[str]:
    """从角色详情数据里提取所有图片 URL（头像、CG、背景图等）。"""
    row = character_get(character_id)
    if not row:
        return []
    urls = []
    data = row.get("data") or row
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return []
    if not isinstance(data, dict):
        return []
    ch = data.get("character") or data.get("data", {}).get("character") or {}
    if not isinstance(ch, dict):
        return []
    # 头像
    avatar = ch.get("avatarURL") or ch.get("avatar") or ""
    if avatar and isinstance(avatar, str) and avatar.startswith("http"):
        urls.append(avatar)
    # 封面/背景图
    for key in ("coverURL", "coverUrl", "backgroundURL", "bannerURL", "cgURL"):
        val = ch.get(key)
        if val and isinstance(val, str) and val.startswith("http"):
            urls.append(val)
    # extensions 里可能有多张 CG
    ext = ch.get("extensions") or {}
    if isinstance(ext, dict):
        for key in ("cgList", "images", "gallery"):
            lst = ext.get(key)
            if isinstance(lst, list):
                for item in lst:
                    if isinstance(item, str) and item.startswith("http"):
                        urls.append(item)
                    elif isinstance(item, dict):
                        url = item.get("url") or item.get("src") or ""
                        if url and isinstance(url, str) and url.startswith("http"):
                            urls.append(url)
    return list(set(urls))  # 去重


# --------------------------------------------------------------- 聊天（会话 / 消息）

def _char_meta(character_id: str) -> dict:
    """角色名与头像：聊天历史列表要显示（内容库 works → 本地创作 my_works 兜底）"""
    row = one("SELECT name, cover_url FROM works WHERE character_id=?",
              (character_id,)) if character_id else None
    name = (row["name"] if row else "") or ""
    cover = (row["cover_url"] if row else "") or ""
    if not name and character_id:            # 本地创作的作品不在内容库 → 查 my_works
        own = one("SELECT title, cover_url, payload FROM my_works WHERE character_id=?",
                  (character_id,))
        if own:
            payload = _as_dict(own["payload"])
            name = str(payload.get("name") or own["title"] or "")
            cover = cover or str(payload.get("avatarURL") or own["cover_url"] or "")
    return {"characterName": name or "未知角色", "avatarUrl": cover or "/favicon-192.png"}


def _thread_view(row: dict) -> dict:
    cid = row.get("character_id") or ""
    meta = _char_meta(cid)
    known = meta["characterName"] != "未知角色"
    name = meta["characterName"] if known else (row.get("character_name") or "未知角色")
    sid = row.get("session_id") or ""
    title = row.get("title") or f"与{name}的对话"
    if known and title.startswith("与未知角色"):
        title = f"与{name}的对话"
    # 预览是原文 content[:200]，带 <say>/<book_status> 等标签：列表里要显示纯文本
    preview = re.sub(r"<[^>]+>", "", row.get("preview") or "")
    preview = re.sub(r"\s+", " ", preview).strip()[:80]
    return {
        "sessionId": sid, "id": sid,
        "characterId": cid,
        "characterName": name,
        "title": title, "preview": preview, "lastMessage": preview,
        "avatarUrl": meta["avatarUrl"] if known else (row.get("avatar_url") or meta["avatarUrl"]),
        "messageCount": int(row.get("message_count") or 0),
        "status": row.get("status") or "active",
        "createdAt": row.get("created_at") or now_iso(),
        "updatedAt": row.get("updated_at") or now_iso(),
    }


def list_chat_memories(user_id: int | None, character_id: str,
                       session_id: str | None = None,
                       limit: int = 300) -> list[dict]:
    """记忆区列表（新表 chat_memory_items，升序）。

    旧表 chat_memories 已由 _migrate_chat_memories 回填进新表；
    这里只读新表，字段形状保持 [{id, content, round, createdAt}]。
    """
    where = ["i.user_id IS ?", "i.status='active'"]
    args: list = [user_id]
    if character_id:
        where.append("i.character_id=?")
        args.append(str(character_id))
    if session_id is not None:
        where.append("i.session_id=?")
        args.append(str(session_id))
    # 回填行按 source_turn 回查旧表的轮次，保证 round 显示真实轮数
    sql = ("SELECT i.id, i.content, i.kind, i.created_at,"
           " COALESCE(m.round_no, 0) AS round_no"
           " FROM chat_memory_items i"
           " LEFT JOIN chat_memories m"
           " ON i.source_turn = 'legacy:chat_memories:' || m.id"
           " WHERE " + " AND ".join(where) + " ORDER BY i.id ASC")
    if limit and int(limit) > 0:
        sql += " LIMIT ?"
        args.append(int(limit))
    return [{"id": int(r["id"]), "content": str(r["content"] or ""),
             "round": int(r["round_no"] or 0), "kind": str(r["kind"] or ""),
             "createdAt": str(r["created_at"] or "")}
            for r in query(sql, tuple(args))]


def delete_chat_memory_item(user_id: int | None, memory_id: int) -> int:
    """删除新表里的一条记忆（向量 / FTS 一并清理）。"""
    mid = int(memory_id or 0)
    if not mid or not one("SELECT id FROM chat_memory_items"
                          " WHERE user_id IS ? AND id=?", (user_id, mid)):
        return 0
    _purge_memory_items("user_id IS ? AND id=?", (user_id, mid))
    return 1


def chat_memories(user_id: int | None, character_id: str,
                  session_id: str | None = None) -> list[dict]:
    """记忆区列表（升序——旧摘要在前，注入时新的排后面）。

    传 session_id 时只读该会话的记忆；不传则按角色读（兼容控制台旧视图）。
    """
    if session_id is not None:
        rows = query(
            "SELECT * FROM chat_memories WHERE user_id IS ? AND character_id=?"
            " AND session_id=? ORDER BY id ASC",
            (user_id, character_id or "", str(session_id)))
    else:
        rows = query(
            "SELECT * FROM chat_memories WHERE user_id IS ? AND character_id=?"
            " ORDER BY id ASC", (user_id, character_id or ""))
    return [{"id": int(r["id"]), "content": str(r["content"] or ""),
             "round": int(r["round_no"] or 0),
             "createdAt": str(r["created_at"] or "")} for r in rows]


def delete_chat_memory(user_id: int | None, memory_id: int) -> int:
    return int(execute("DELETE FROM chat_memories WHERE user_id IS ? AND id=?",
                       (user_id, int(memory_id or 0))) or 0)


def delete_chat_memory_state(user_id: int | None, *, session_id: str = "",
                             character_id: str = "") -> int:
    """删除会话/角色历史时同步清理记忆与游标，避免删了历史还能“记住”。"""
    n = 0
    if session_id:
        n += int(execute(
            "DELETE FROM chat_memories WHERE user_id IS ? AND session_id=?",
            (user_id, str(session_id))) or 0)
        execute("DELETE FROM chat_memory_slots WHERE user_id IS ? AND session_id=?",
                (user_id, str(session_id)))
        execute("DELETE FROM chat_summary_state WHERE session_id=? AND user_id IS ?",
                (str(session_id), user_id))
        _purge_memory_items("user_id IS ? AND session_id=?", (user_id, str(session_id)))
    elif character_id:
        n += int(execute(
            "DELETE FROM chat_memories WHERE user_id IS ? AND character_id=?",
            (user_id, character_id or "")) or 0)
        execute("DELETE FROM chat_memory_slots WHERE user_id IS ? AND character_id=?",
                (user_id, character_id or ""))
        execute("DELETE FROM chat_summary_state WHERE user_id IS ? AND character_id=?",
                (user_id, character_id or ""))
        _purge_memory_items("user_id IS ? AND character_id=?",
                            (user_id, character_id or ""))
    return n


# ------------------------------------------------- 记忆固定槽位
MEMORY_SLOTS = ("short_term", "long_term", "user_info", "promise")


def memory_slots(user_id: int | None, session_id: str) -> dict:
    """读该会话的四类记忆槽位（未写入的槽返回空串）。"""
    rows = query("SELECT slot, content, covered_until FROM chat_memory_slots"
                 " WHERE user_id IS ? AND session_id=?", (user_id, str(session_id)))
    out = {s: {"content": "", "covered_until": 0} for s in MEMORY_SLOTS}
    for r in rows:
        slot = str(r["slot"] or "")
        if slot in out:
            out[slot] = {"content": str(r["content"] or ""),
                         "covered_until": int(r["covered_until"] or 0)}
    return out


def set_memory_slot(user_id: int | None, character_id: str, session_id: str,
                    slot: str, content: str, covered_until: int = 0) -> None:
    if slot not in MEMORY_SLOTS:
        return
    execute(
        "INSERT INTO chat_memory_slots(user_id, character_id, session_id, slot,"
        " content, covered_until, updated_at) VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(session_id, slot) DO UPDATE SET"
        " user_id=excluded.user_id, character_id=excluded.character_id,"
        " content=excluded.content, covered_until=excluded.covered_until,"
        " updated_at=excluded.updated_at",
        (user_id, character_id or None, str(session_id), slot,
         str(content or "").strip(), int(covered_until or 0), now_iso()))


def summary_state(user_id: int | None, session_id: str) -> dict:
    row = one("SELECT * FROM chat_summary_state WHERE session_id=? AND user_id IS ?",
              (str(session_id or ""), user_id))
    if row is None:
        return {"covered_until": 0, "covered_count": 0,
                "rolling_summary": "", "turns_since_update": 0}
    return {"covered_until": int(row["covered_until"] or 0),
            "covered_count": int(row["covered_count"] or 0),
            "rolling_summary": str(row["rolling_summary"] or ""),
            "turns_since_update": int(row["turns_since_update"] or 0)}


def update_summary_state(user_id: int | None, character_id: str, session_id: str,
                         *, rolling_summary: str | None = None,
                         turns_since_update: int | None = None,
                         covered_until: int | None = None,
                         covered_count: int | None = None) -> None:
    cur = summary_state(user_id, session_id)
    execute(
        "INSERT INTO chat_summary_state(session_id, user_id, character_id,"
        " covered_until, covered_count, rolling_summary, turns_since_update,"
        " updated_at) VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(session_id) DO UPDATE SET"
        " user_id=excluded.user_id, character_id=excluded.character_id,"
        " covered_until=excluded.covered_until, covered_count=excluded.covered_count,"
        " rolling_summary=excluded.rolling_summary,"
        " turns_since_update=excluded.turns_since_update,"
        " updated_at=excluded.updated_at",
        (str(session_id), user_id, character_id or None,
         int(cur["covered_until"] if covered_until is None else covered_until),
         int(cur["covered_count"] if covered_count is None else covered_count),
         str(cur["rolling_summary"] if rolling_summary is None else rolling_summary),
         int(cur["turns_since_update"] if turns_since_update is None
             else turns_since_update), now_iso()))


def record_first_message_once(user_id: int | None, session_id: str,
                              content: str) -> int:
    """会话还没有任何消息时写入 firstMes（D3：后端只写一次）。"""
    text = str(content or "").strip()
    if not text or not session_id:
        return 0
    row = one("SELECT t.id FROM chat_threads t WHERE t.user_id IS ? AND t.session_id=?",
              (user_id, str(session_id)))
    if row is None:
        return 0
    tid = row["id"]
    cur = get_conn().execute(
        "INSERT INTO chat_messages(thread_id, role, content, content_llm, created_at)"
        " SELECT ?, 'assistant', ?, ?, ? WHERE NOT EXISTS"
        " (SELECT 1 FROM chat_messages WHERE thread_id=?)",
        (tid, text, "", now_iso(), tid))
    if int(cur.rowcount or 0) <= 0:      # 已有消息 → 不重复写开场白
        return 0
    mid = int(cur.lastrowid or 0)
    if mid:
        execute("UPDATE chat_threads SET message_count=(SELECT COUNT(*) FROM chat_messages"
                " WHERE thread_id=?), preview=?, updated_at=? WHERE id=?",
                (tid, text[:200], now_iso(), tid))
    return mid


def ensure_chat_session(user_id: int | None, character_id: str, session_id: str = "",
                        title: str = "", fresh: bool = False,
                        initial_stats: dict | None = None) -> dict:
    """拿（或建）一个本地会话：chatSession/list 空列表会让前端卡在「正在打开对话…」

    `session_id` 用会话行 id 的十进制字符串（前端 `parseInt(sessionId)` 要能过）。
    """
    sid = str(session_id or "").strip()
    row = None
    if sid:
        row = one("SELECT * FROM chat_threads WHERE user_id IS ? AND session_id=?",
                  (user_id, sid))
    elif not fresh:
        row = one("SELECT * FROM chat_threads WHERE user_id IS ? AND character_id=?"
                  " AND session_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
                  (user_id, character_id))
    if row is None:
        meta = _char_meta(character_id)
        now = now_iso()
        seed = initial_stats if isinstance(initial_stats, dict) else {}
        cur = get_conn().execute(
            "INSERT INTO chat_threads(user_id, character_id, session_id, title,"
            " character_name, avatar_url, preview, message_count, status,"
            " stats_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, character_id or None, sid or None,
             title or f"与{meta['characterName']}的对话",
             meta["characterName"], meta["avatarUrl"], "", 0, "active",
             json.dumps(seed, ensure_ascii=False), now, now))
        tid = int(cur.lastrowid or 0)
        if not sid:
            sid = str(tid)
            execute("UPDATE chat_threads SET session_id=? WHERE id=?", (sid, tid))
        row = dict(one("SELECT * FROM chat_threads WHERE id=?", (tid,)) or {})
    else:
        row = dict(row)
        if initial_stats and str(row.get("stats_json") or "").strip() in ("", "{}"):
            # 旧会话没有初始状态 → 补上（已有历史消息时仍以最新回复里的数值为准）
            seed_text = json.dumps(initial_stats, ensure_ascii=False)
            execute("UPDATE chat_threads SET stats_json=? WHERE id=?",
                    (seed_text, row["id"]))
            row["stats_json"] = seed_text
        if title and not row.get("title"):
            execute("UPDATE chat_threads SET title=? WHERE id=?", (title, row["id"]))
            row["title"] = title
        if character_id and not row.get("character_id"):
            execute("UPDATE chat_threads SET character_id=? WHERE id=?",
                    (character_id, row["id"]))
            row["character_id"] = character_id
    return _thread_view(row)


def thread_stats(user_id: int | None, session_id: str) -> dict:
    """会话建立时写入的初始状态（{属性: 数值}）；没有则回空字典。"""
    row = one("SELECT stats_json FROM chat_threads WHERE user_id IS ? AND session_id=?",
              (user_id, str(session_id or "")))
    if row is None:
        return {}
    try:
        data = json.loads(str(row["stats_json"] or "{}"))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        try:
            out[str(k)] = int(float(v))
        except (TypeError, ValueError):
            continue
    return out


def update_thread_stats(user_id: int | None, session_id: str, values: dict) -> dict:
    """把本轮状态合并回会话（stats_json 作为跨轮真相源）。

    只覆盖传进来的属性；缺字段保留上一轮的值，而不是回落到初始值。
    """
    if not session_id or not isinstance(values, dict) or not values:
        return thread_stats(user_id, session_id)
    row = one("SELECT id, stats_json FROM chat_threads WHERE user_id IS ? AND session_id=?",
              (user_id, str(session_id)))
    if row is None:
        return {}
    try:
        old = json.loads(str(row["stats_json"] or "{}"))
    except ValueError:
        old = {}
    if not isinstance(old, dict):
        old = {}
    merged = dict(old)
    for k, v in values.items():
        if v is None:
            continue
        try:
            merged[str(k)] = int(float(v))
        except (TypeError, ValueError):
            continue
    execute("UPDATE chat_threads SET stats_json=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), row["id"]))
    return {str(k): int(v) for k, v in merged.items()}


def delete_chat_message(user_id: int | None, message_id: int) -> bool:
    """删一条消息并回填 message_count / preview（失败路径清理孤立 user 消息）。"""
    try:
        mid = int(message_id or 0)
    except (TypeError, ValueError):
        return False
    if mid <= 0:
        return False
    row = one("SELECT m.thread_id FROM chat_messages m"
              " JOIN chat_threads t ON t.id=m.thread_id"
              " WHERE m.id=? AND t.user_id IS ?", (mid, user_id))
    if row is None:
        return False
    tid = row["thread_id"]
    n = execute("DELETE FROM chat_messages WHERE id=? AND thread_id=?", (mid, tid))
    if not n:
        return False
    last = one("SELECT content FROM chat_messages WHERE thread_id=?"
               " ORDER BY id DESC LIMIT 1", (tid,))
    execute("UPDATE chat_threads SET message_count=(SELECT COUNT(*) FROM chat_messages"
            " WHERE thread_id=?), preview=?, updated_at=? WHERE id=?",
            (tid, str((last or {}).get("content") or "")[:200], now_iso(), tid))
    return True


def record_chat_message(user_id: int, session_id: str, role: str, content: str,
                        content_llm: str | None = None) -> int | None:
    """写一条消息并刷新会话的 messageCount / 最后一条预览。

    content 保留原文（前端渲染状态栏/选项/CG 用）；content_llm 是喂模型的纯文本。
    assistant 不传该列则留空，读取时按原文现算；用户消息即原文。
    """
    content = (content or "").strip()
    if not content or not session_id:
        return None
    role = "user" if role == "user" else "assistant"
    if content_llm is None:
        # assistant 缺省留空 → 拼 Prompt 时按原文现算，避免把 UI 标签喂给模型
        content_llm = content if role == "user" else ""
    content_llm = str(content_llm or "").strip()
    row = one("SELECT id FROM chat_threads WHERE user_id IS ? AND session_id=?",
              (user_id, session_id))
    if not row:
        return None
    tid = row["id"]
    cur = get_conn().execute(
        "INSERT INTO chat_messages(thread_id, role, content, content_llm, created_at)"
        " VALUES(?,?,?,?,?)",
        (tid, role, content[:20000],
         content_llm[:20000], now_iso()))
    execute(
        "UPDATE chat_threads SET message_count=(SELECT COUNT(*) FROM chat_messages"
        " WHERE thread_id=?), preview=?, updated_at=? WHERE id=?",
        (tid, content[:200], now_iso(), tid))
    return int(cur.lastrowid or 0)


def record_chat_prompt(user_id: int | None, session_id: str, character_id: str,
                       model: str, requests: list, *, turn_id: str = "",
                       finish_reason: str = "", sections: list | None = None,
                       prompt_hash: str = "", worldbook: list | None = None,
                       memory: list | None = None,
                       diff: list | None = None) -> int | None:
    """把一次对话里**实际发出的全部请求**汇总成一条快照。

    requests 由发送钩子逐条收集，形如
    [{"url": "...", "stream": true, "payload": {"model": "...", "messages": [...]}}, ...]
    汇总口径：一次对话 = 一条记录；主请求的 messages 单独拆出来方便检索，完整数据在 requests。
    turn_id / finish_reason / sections / prompt_hash 是阶段 0 的回归对照字段。
    """
    if not requests:
        return None
    try:
        payload = json.dumps(requests, ensure_ascii=False)
    except Exception:
        return None
    main: dict = {}
    for rec in requests:
        body = (rec or {}).get("payload") or {}
        if isinstance(body.get("messages"), list):
            main = body
            break
    msgs = main.get("messages") or []
    if not model:
        model = str(main.get("model") or "")
    try:
        msgs_json = json.dumps(msgs, ensure_ascii=False)
    except Exception:
        msgs_json = "[]"
    try:
        sections_json = json.dumps(sections or [], ensure_ascii=False)
    except Exception:
        sections_json = "[]"
    def _dump(v):
        try:
            return json.dumps(v or [], ensure_ascii=False)
        except Exception:
            return "[]"
    cur = get_conn().execute(
        "INSERT INTO chat_prompt_log(user_id, session_id, character_id, model,"
        " messages, message_count, bytes, requests, turn_id, finish_reason,"
        " sections_json, prompt_hash, worldbook_json, memory_json, diff_json,"
        " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, session_id, character_id, model, msgs_json, len(msgs),
         len(payload.encode("utf-8")), payload, str(turn_id or ""),
         str(finish_reason or ""), sections_json, str(prompt_hash or ""),
         _dump(worldbook), _dump(memory), _dump(diff), now_iso()))
    return int(cur.lastrowid or 0)


def recent_chat_prompts(user_id: int | None = None, session_id: str = "",
                       limit: int = 10) -> list[dict]:
    """控制台只读：最近几轮 prompt 快照（含分段统计与本轮 diff）。"""
    where, args = [], []
    if user_id is not None:
        where.append("user_id IS ?")
        args.append(user_id)
    if session_id:
        where.append("session_id=?")
        args.append(str(session_id))
    sql = "SELECT * FROM chat_prompt_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = query(sql + " ORDER BY id DESC LIMIT ?",
                 tuple(args) + (max(1, min(int(limit or 10), 100)),))
    out = []
    for r in rows:
        item = dict(r)
        for k in ("messages", "requests", "sections_json", "worldbook_json",
                  "memory_json", "diff_json"):
            try:
                item[k] = json.loads(item.get(k) or "[]")
            except (ValueError, TypeError):
                pass
        out.append(item)
    return out


def chat_sessions(user_id: int | None, character_id: str = "", page: int = 1,
                  page_size: int = 20) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 100))
    where = ["user_id IS ?", "session_id IS NOT NULL"]
    params: list = [user_id]
    if character_id:
        where.append("character_id=?")
        params.append(character_id)
    base = "SELECT * FROM chat_threads WHERE " + " AND ".join(where)
    total = one(f"SELECT COUNT(*) AS c FROM ({base})", tuple(params))["c"]
    rows = query(base + " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                 tuple(params) + (page_size, (page - 1) * page_size))
    items = [_thread_view(dict(r)) for r in rows]
    return {"list": items, "records": items, "total": int(total),
            "page": page, "pageSize": page_size}


def chat_history(user_id: int | None, page: int = 1, page_size: int = 30,
                 flt: str = "") -> dict:
    """游玩历史（/history）：data.list + data.total（total 必须是整数）"""
    rows = query("SELECT * FROM chat_threads WHERE user_id IS ? AND session_id IS NOT NULL"
                 " ORDER BY updated_at DESC", (user_id,))
    items = [_thread_view(dict(r)) for r in rows]
    if flt == "played":
        items = [i for i in items if i["messageCount"] > 0]
    elif flt in ("unplayed", "browsed"):
        # 前端（app/history chunk）的 tab 值是 all / played / browsed，
        # 只认 unplayed 会让 browsed 落空 → 返回全量 → 前端校验
        # 「逛过的每条 messageCount 必须为 0」失败，弹「历史筛选暂不可用」
        items = [i for i in items if i["messageCount"] == 0]
    total = len(items)
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 30), 200))
    start = (page - 1) * page_size
    return {"list": items[start:start + page_size], "records": items[start:start + page_size],
            "total": int(total), "page": page, "pageSize": page_size}


def chat_messages(user_id: int | None, session_id: str, page_size: int = 20,
                  order_by: str = "desc", last_message_id=None) -> dict:
    """会话消息（messagesOptimized）：data.messages + data.hasMore"""
    page_size = max(1, min(int(page_size or 20), 200))
    row = one("SELECT id, message_count FROM chat_threads WHERE user_id IS ? AND session_id=?",
              (user_id, session_id))
    if not row:
        return {"messages": [], "list": [], "hasMore": False,
                "lastMessageId": 0, "pageSize": page_size}
    tid = row["id"]
    total = int(row["message_count"] or 0)
    if str(order_by).lower() == "asc":
        sql = "SELECT id, role, content, created_at FROM chat_messages WHERE thread_id=?"
        params: list = [tid]
        if last_message_id:
            sql += " AND id > ?"
            params.append(int(last_message_id))
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(page_size)
        rows = query(sql, tuple(params))
        older = one("SELECT COUNT(*) AS c FROM chat_messages WHERE thread_id=? AND id < ?",
                    (tid, rows[0]["id"]))["c"] if rows else 0
    else:
        sql = "SELECT id, role, content, created_at FROM chat_messages WHERE thread_id=?"
        params = [tid]
        if last_message_id:
            sql += " AND id < ?"
            params.append(int(last_message_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(page_size)
        rows = query(sql, tuple(params))
        older = one("SELECT COUNT(*) AS c FROM chat_messages WHERE thread_id=? AND id > ?",
                    (tid, rows[-1]["id"]))["c"] if rows else 0
    messages = [{
        "id": r["id"], "messageId": r["id"], "sessionId": session_id,
        "role": r["role"], "content": r["content"], "createdAt": r["created_at"],
    } for r in rows]
    if str(order_by).lower() != "asc":
        messages.reverse()
    last_id = min([m["id"] for m in messages], default=0)
    return {"messages": messages, "list": messages, "hasMore": bool(older),
            "lastMessageId": last_id, "pageSize": page_size, "total": total}


def delete_chat_history(user_id: int | None, character_id: str) -> int:
    n = one("SELECT COUNT(*) AS c FROM chat_threads WHERE user_id IS ? AND character_id=?",
            (user_id, character_id))["c"]
    delete_chat_memory_state(user_id, character_id=character_id)
    execute("DELETE FROM chat_threads WHERE user_id IS ? AND character_id=?",
            (user_id, character_id))
    return int(n)


# --------------------------------------------------------------- 统计 / 表结构

TABLES = (
    "users", "sessions", "request_log", "favorites", "checkin_records",
    "chat_threads", "chat_messages", "uploads",
    "works", "work_tags", "tag_catalog", "content_api",
    "creative_items", "creative_user_items", "creative_comments",
    "creative_votes", "creative_purchases", "creative_bindings",
    "creative_defaults",
    "my_works", "worldbook_entries", "config_cache",
    "llm_providers", "image_providers", "video_providers", "image_works",
    "meta", "db_op_log",
)


def schema() -> list[dict]:
    """数据库结构：每张表的列 + 行数（控制台用）"""
    out = []
    for t in TABLES:
        try:
            cols = [{"name": r["name"], "type": r["type"]}
                    for r in query(f"PRAGMA table_info({t})")]
            rows = one(f"SELECT COUNT(*) AS c FROM {t}")["c"]
        except sqlite3.Error:  # noqa: PERF203
            continue
        out.append({"table": t, "rows": rows, "columns": cols})
    return out


def stats() -> dict:
    def count(table: str) -> int:
        try:
            return int(one(f"SELECT COUNT(*) AS c FROM {table}")["c"])
        except (sqlite3.Error, TypeError):
            return 0

    def scalar(sql: str) -> int:
        try:
            row = one(sql)
            return int(row["c"]) if row and row.get("c") is not None else 0
        except (sqlite3.Error, TypeError):
            return 0

    def rows(sql: str) -> list[dict]:
        try:
            return query(sql)
        except sqlite3.Error:
            return []

    top_paths = rows(
        "SELECT path, COUNT(*) AS hits FROM request_log GROUP BY path"
        " ORDER BY hits DESC LIMIT 10"
    )
    recent = rows(
        "SELECT ts, method, path, status, duration_ms FROM request_log"
        " ORDER BY id DESC LIMIT 5"
    )
    status_mix = rows(
        "SELECT (status / 100) * 100 AS bucket, COUNT(*) AS c FROM request_log"
        " GROUP BY bucket ORDER BY bucket"
    )
    return {
        "users": count("users"),
        "sessions": count("sessions"),
        "favorites": count("favorites"),
        "chat_messages": count("chat_messages"),
        "chat_threads": count("chat_threads"),
        "uploads": count("uploads"),
        "works": count("works"),
        "work_tags": count("work_tags"),
        "tags": scalar("SELECT COUNT(DISTINCT tag) AS c FROM work_tags"),
        "tag_catalog": count("tag_catalog"),
        "content_apis": count("content_api"),
        "creative_items": count("creative_items"),
        "creative_open_source": scalar(
            "SELECT COUNT(*) AS c FROM creative_items WHERE is_open_source=1"),
        "creative_comments": count("creative_comments"),
        "creative_purchases": count("creative_purchases"),
        "image_providers": count("image_providers"),
        "video_providers": count("video_providers"),
        "image_works": count("image_works"),
        "requests": count("request_log"),
        "tables": {row["table"]: row["rows"] for row in schema()},
        "top_paths": top_paths,
        "recent": recent,
        "status_mix": status_mix,
        "db_path": str(DB_PATH),
        "upload_dir": str(UPLOAD_DIR),
        "time": now_iso(),
    }


# --------------------------------------------------------------- 数据库控制台

def _ident(name: str) -> str:
    """校验并返回 SQLite 标识符（表名 / 列名），避免拼接注入。"""
    name = str(name or "").strip()
    if not _IDENT_RE.match(name):
        raise ValueError(f"非法标识符：{name or '(空)'}")
    return name


def _existing_tables() -> set[str]:
    try:
        return {r["name"] for r in query(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")}
    except sqlite3.Error:
        return set()


def _table_columns(table: str) -> list[dict]:
    table = _ident(table)
    return [dict(r) for r in query(f'PRAGMA table_info("{table}")')]


def _size_human(n: int) -> str:
    n = max(0, int(n or 0))
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def db_log(op: str, table_name: str = "", detail: str = "",
           user_id: int | None = None, ok: int = 1) -> None:
    """写一条控制台操作日志；日志表不存在时静默失败。"""
    try:
        execute(
            "INSERT INTO db_op_log(op, table_name, detail, user_id, ok, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (str(op or "QUERY"), str(table_name or ""), str(detail or "")[:8000],
             user_id, int(ok), now_iso()),
        )
        # 只保留最近 2000 条，避免终端日志无限增长
        execute("DELETE FROM db_op_log WHERE id <= "
                "(SELECT MAX(id) - 2000 FROM db_op_log)")
    except sqlite3.Error:
        pass


def db_logs(limit: int = 60, offset: int = 0) -> list[dict]:
    limit = max(1, min(int(limit or 60), 500))
    offset = max(0, int(offset or 0))
    try:
        return query("SELECT * FROM db_op_log ORDER BY id DESC LIMIT ? OFFSET ?",
                     (limit, offset))
    except sqlite3.Error:
        return []


def db_overview() -> dict:
    tables = db_tables()
    total_rows = sum(int(t["rows"] or 0) for t in tables)
    total_cols = sum(int(t["columns"] or 0) for t in tables)
    size = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            size += Path(str(DB_PATH) + suffix).stat().st_size
        except OSError:
            pass
    return {
        "connected": True,
        "driver": "sqlite3",
        "database": DB_PATH.name,
        "path": str(DB_PATH),
        "host": "localhost",
        "port": 0,
        "pool": "QueuePool",
        "size_bytes": size,
        "size_human": _size_human(size),
        "tables": len(tables),
        "rows": total_rows,
        "columns": total_cols,
        "sqlite_version": sqlite3.sqlite_version,
        "writable": os.access(DB_PATH.parent, os.W_OK),
        "time": now_iso(),
    }


def db_tables() -> list[dict]:
    out = []
    for r in query("SELECT name FROM sqlite_master WHERE type='table'"
                   " AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        name = r["name"]
        try:
            n = int(one(f'SELECT COUNT(*) AS c FROM "{name}"')["c"])
            cols = len(query(f'PRAGMA table_info("{name}")'))
        except (sqlite3.Error, TypeError):
            n, cols = 0, 0
        out.append({"name": name, "rows": n, "columns": cols})
    return out


def db_table_data(table: str, limit: int = 50, offset: int = 0) -> dict:
    table = _ident(table)
    if table not in _existing_tables():
        raise ValueError("数据表不存在")
    cols = _table_columns(table)
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    total = int(one(f'SELECT COUNT(*) AS c FROM "{table}"')["c"])
    try:
        rows = query(f'SELECT rowid AS __rowid__, * FROM "{table}"'
                     f' ORDER BY rowid DESC LIMIT ? OFFSET ?', (limit, offset))
    except sqlite3.Error:
        rows = query(f'SELECT * FROM "{table}" LIMIT ? OFFSET ?', (limit, offset))
    for row in rows:
        for key, val in list(row.items()):
            if isinstance(val, (bytes, bytearray, memoryview)):
                row[key] = "0x" + bytes(val).hex()
    return {
        "table": table,
        "columns": [{
            "name": c["name"],
            "type": c["type"] or "",
            "pk": bool(c["pk"]),
            "notnull": bool(c["notnull"]),
            "default": c["dflt_value"],
        } for c in cols],
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _pk_where(pk, cols: list[dict]) -> tuple[str, tuple]:
    if not isinstance(pk, dict) or not pk:
        raise ValueError("缺少主键，无法定位这条记录")
    if set(pk.keys()) == {"__rowid__"}:
        return "rowid = ?", (pk["__rowid__"],)
    names = {c["name"] for c in cols}
    parts, args = [], []
    for key, val in pk.items():
        if key not in names:
            raise ValueError(f"主键列不存在：{key}")
        parts.append(f'"{key}" = ?')
        args.append(val)
    return " AND ".join(parts), tuple(args)


def db_save_row(table: str, values: dict, pk=None,
                user_id: int | None = None) -> dict:
    table = _ident(table)
    if table not in _existing_tables():
        raise ValueError("数据表不存在")
    cols = _table_columns(table)
    names = {c["name"] for c in cols}
    if not isinstance(values, dict) or not values:
        raise ValueError("没有要保存的字段")
    clean = {k: v for k, v in values.items() if k in names}
    if not clean:
        raise ValueError("字段都不属于这张表")

    if pk:
        where, args = _pk_where(pk, cols)
        sets = ", ".join(f'"{k}" = ?' for k in clean)
        cur = get_conn().execute(
            f'UPDATE "{table}" SET {sets} WHERE {where}',
            tuple(clean.values()) + args)
        db_log("SAVE_ROW", table,
               json.dumps({"pk": pk, "values": clean}, ensure_ascii=False), user_id)
        return {"updated": int(cur.rowcount), "table": table, "pk": pk}

    keys = list(clean.keys())
    sql = (f'INSERT INTO "{table}" (' + ", ".join(f'"{k}"' for k in keys) +
           ") VALUES (" + ", ".join("?" for _ in keys) + ")")
    cur = get_conn().execute(sql, tuple(clean.values()))
    db_log("SAVE_ROW", table,
           json.dumps({"id": cur.lastrowid, "values": clean}, ensure_ascii=False),
           user_id)
    return {"inserted": True, "id": cur.lastrowid, "table": table}


def db_delete_row(table: str, pk, user_id: int | None = None) -> dict:
    table = _ident(table)
    if table not in _existing_tables():
        raise ValueError("数据表不存在")
    cols = _table_columns(table)
    where, args = _pk_where(pk, cols)
    cur = get_conn().execute(f'DELETE FROM "{table}" WHERE {where}', args)
    db_log("DELETE_ROW", table,
           json.dumps({"pk": pk, "deleted": cur.rowcount}, ensure_ascii=False),
           user_id)
    return {"deleted": int(cur.rowcount), "table": table}


def _sql_default(value) -> str:
    s = str(value).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return s
    if s.upper() in ("NULL", "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME"):
        return s.upper()
    return "'" + s.replace("'", "''") + "'"


def db_create_table(name: str, columns: list[dict],
                    user_id: int | None = None) -> dict:
    name = _ident(name)
    if name.lower().startswith("sqlite_"):
        raise ValueError("不能创建 sqlite_ 开头的系统表")
    if name in _existing_tables():
        raise ValueError(f"数据表已存在：{name}")
    if not isinstance(columns, list) or not columns:
        raise ValueError("至少需要一个字段")

    parts: list[str] = []
    pk_cols: list[str] = []
    seen: set[str] = set()
    pk_count = sum(1 for c in columns if isinstance(c, dict) and c.get("pk"))
    for col in columns:
        if not isinstance(col, dict):
            continue
        col_name = _ident(col.get("name"))
        if col_name in seen:
            raise ValueError(f"字段重复：{col_name}")
        seen.add(col_name)
        col_type = str(col.get("type") or "TEXT").upper().strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+(\s*\(\s*\d+\s*(,\s*\d+\s*)?\))?", col_type):
            raise ValueError(f"非法字段类型：{col_type}")
        ddl = f'"{col_name}" {col_type}'
        if col.get("notnull"):
            ddl += " NOT NULL"
        if col.get("pk") and pk_count == 1 and col_type == "INTEGER" and col.get("auto"):
            ddl += " PRIMARY KEY AUTOINCREMENT"
        elif col.get("pk"):
            pk_cols.append(col_name)
        if col.get("default") not in (None, ""):
            ddl += " DEFAULT " + _sql_default(col["default"])
        parts.append(ddl)
    if not parts:
        raise ValueError("至少需要一个合法字段")
    if pk_cols:
        parts.append("PRIMARY KEY (" + ", ".join(f'"{c}"' for c in pk_cols) + ")")

    sql = f'CREATE TABLE "{name}" (\n  ' + ",\n  ".join(parts) + "\n)"
    get_conn().execute(sql)
    db_log("CREATE_TABLE", name, json.dumps({"sql": sql}, ensure_ascii=False), user_id)
    return {"table": name, "sql": sql}


def db_drop_table(name: str, user_id: int | None = None) -> dict:
    name = _ident(name)
    if name in SYSTEM_TABLES:
        raise ValueError(f"{name} 是控制台系统表，不能删除")
    if name not in _existing_tables():
        raise ValueError("数据表不存在")
    conn = get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute(f'DROP TABLE "{name}"')
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    db_log("DROP_TABLE", name, "", user_id)
    return {"dropped": name}


def db_clear_table(name: str, user_id: int | None = None) -> dict:
    name = _ident(name)
    if name in SYSTEM_TABLES:
        raise ValueError(f"{name} 是控制台系统表，不能清空")
    if name not in _existing_tables():
        raise ValueError("数据表不存在")
    conn = get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        cur = conn.execute(f'DELETE FROM "{name}"')
        deleted = int(cur.rowcount)
        try:
            conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (name,))
        except sqlite3.Error:
            pass
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    db_log("CLEAR_TABLE", name, json.dumps({"deleted": deleted}, ensure_ascii=False),
           user_id)
    return {"cleared": name, "deleted": deleted}


def _drop_tables(names: list[str]) -> None:
    if not names:
        return
    conn = get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        for name in names:
            conn.execute(f'DROP TABLE IF EXISTS "{_ident(name)}"')
        try:
            conn.execute("DELETE FROM sqlite_sequence")
        except sqlite3.Error:
            pass
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def db_clear_all(user_id: int | None = None) -> dict:
    names = [t["name"] for t in db_tables() if t["name"] not in PROTECTED_CLEAR]
    conn = get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        for name in names:
            conn.execute(f'DELETE FROM "{_ident(name)}"')
        try:
            conn.execute("DELETE FROM sqlite_sequence")
        except sqlite3.Error:
            pass
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    db_log("CLEAR_ALL", "", json.dumps({"tables": names}, ensure_ascii=False), user_id)
    return {"cleared": len(names), "tables": names}


def db_recreate(user_id: int | None = None) -> dict:
    names = [t["name"] for t in db_tables() if t["name"] not in PROTECTED_CLEAR]
    _drop_tables(names)
    init()
    ensure_admin()
    db_log("RECREATE", "", json.dumps({"dropped": names}, ensure_ascii=False), user_id)
    return {"recreated": len(names), "tables": len(db_tables())}


def db_drop_all(user_id: int | None = None) -> dict:
    # 旧接入表名现在是视图，先移除，避免留下指向已删物理表的悬空视图
    for view in _PROVIDER_TABLES.values():
        execute(f"DROP VIEW IF EXISTS {view}")
    names = [t["name"] for t in db_tables() if t["name"] not in SYSTEM_TABLES]
    _drop_tables(names)
    db_log("DROP_ALL", "", json.dumps({"dropped": names}, ensure_ascii=False), user_id)
    return {"dropped": len(names), "tables": names, "kept": list(SYSTEM_TABLES)}


def _safe_table_name(raw) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(raw or "").strip()).strip("_") or "json_data"
    if not re.match(r"^[A-Za-z_]", s):
        s = "t_" + s
    return s


def _json_rows(stem: str, data) -> tuple[str, list[dict]]:
    table = _safe_table_name(stem)
    if isinstance(data, list):
        return table, [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        list_keys = [k for k, v in data.items() if isinstance(v, list)]
        if len(data) == 1 and list_keys:
            return (_safe_table_name(list_keys[0]),
                    [x for x in data[list_keys[0]] if isinstance(x, dict)])
        if data and all(isinstance(v, dict) for v in data.values()):
            return table, [{"key": k, **v} for k, v in data.items()]
        return table, [data]
    return table, []


def _json_col_type(values: list) -> str:
    vals = [v for v in values if v is not None]
    if vals and all(isinstance(v, bool) or isinstance(v, int) for v in vals):
        return "INTEGER"
    if vals and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return "REAL"
    return "TEXT"


def _ensure_json_table(table: str, rows: list[dict]) -> bool:
    table = _ident(table)
    if table in _existing_tables():
        return False
    keys: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in keys:
                keys.append(key)
    cols: list[dict] = []
    for key in keys:
        if not _IDENT_RE.match(str(key)):
            continue
        cols.append({
            "name": str(key),
            "type": _json_col_type([r.get(key) for r in rows]),
        })
    if not cols:
        raise ValueError("没有合法字段")
    id_col = next((c for c in cols if c["name"].lower() == "id"), None)
    if id_col:
        id_col["pk"] = True
        id_col["auto"] = id_col["type"] == "INTEGER"
    else:
        cols.insert(0, {"name": "id", "type": "INTEGER", "pk": True, "auto": True})
    db_create_table(table, cols)
    return True


def _insert_json_rows(table: str, rows: list[dict]) -> int:
    table = _ident(table)
    cols = _table_columns(table)
    names = {c["name"] for c in cols}
    pk_cols = [c["name"] for c in cols if c["pk"]]
    conn = get_conn()
    inserted = 0
    conn.execute("BEGIN")
    try:
        for row in rows:
            vals = {k: v for k, v in row.items() if k in names}
            if not vals:
                continue
            keys = list(vals.keys())
            has_pk = bool(pk_cols) and all(k in vals for k in pk_cols)
            verb = "INSERT OR REPLACE" if has_pk else "INSERT"
            sql = (f'{verb} INTO "{table}" (' +
                   ", ".join(f'"{k}"' for k in keys) + ") VALUES (" +
                   ", ".join("?" for _ in keys) + ")")
            conn.execute(sql, tuple(vals[k] for k in keys))
            inserted += 1
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return inserted


def _is_template_json(data) -> bool:
    """data/json/ 里的绘图内置模板不是数据表（顶层 categories[].recipes[]）"""
    if not isinstance(data, dict):
        return False
    if str(data.get("schema") or "").startswith("missai.template"):
        return True
    cats = data.get("categories")
    if not isinstance(cats, list):
        return False
    return any(isinstance(c, dict) and (c.get("recipes") or c.get("templates"))
               for c in cats)


def db_scan_json(user_id: int | None = None) -> dict:
    """扫描 data/json/，把 JSON 数组导入同名数据表。

    data/json/ 里还放着绘图内置模板（ai-draw-*.json）—— 那些是模板库，不是数据表，直接跳过。
    """
    dirs = [DATA_DIR / "json"]
    files: list[Path] = []
    for d in dirs:
        if d.is_dir():
            files.extend(sorted(d.glob("*.json")))
    result = {
        "dirs": [str(d) for d in dirs],
        "files": 0,
        "imported": 0,
        "tables": [],
        "created": [],
        "skipped": [],
        "errors": [],
    }
    for path in files:
        result["files"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"{path.name}: {exc}")
            continue
        if _is_template_json(data):
            result["skipped"].append(path.name)
            continue
        table, rows = _json_rows(path.stem, data)
        if not rows:
            result["errors"].append(f"{path.name}: 没有可导入的对象")
            continue
        try:
            created = _ensure_json_table(table, rows)
            n = _insert_json_rows(table, rows)
            result["imported"] += n
            if table not in result["tables"]:
                result["tables"].append(table)
            if created:
                result["created"].append(table)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"{path.name}: {exc}")
    db_log("SCAN_JSON", "", json.dumps(result, ensure_ascii=False), user_id)
    return result


def switch_db(path: str, user_id: int | None = None) -> dict:
    """切换 SQLite 数据库文件（不存在会自动创建），并记住到 data/.current_db。"""
    global DB_PATH
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("请填写 SQLite 文件路径")
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = (ROOT / target).resolve()
    if target.is_dir():
        raise ValueError("请填写 SQLite 文件路径，而不是目录")
    target.parent.mkdir(parents=True, exist_ok=True)

    old = str(DB_PATH)
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None
        _local.conn_path = None
    DB_PATH = target
    try:
        init()
        ensure_admin()
    except Exception:
        DB_PATH = Path(old)
        _local.conn = None
        _local.conn_path = None
        raise
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DB_MARKER.write_text(str(target), encoding="utf-8")
    except OSError:
        pass
    db_log("CONNECT_DB", "",
           json.dumps({"from": old, "to": str(target)}, ensure_ascii=False), user_id)
    return db_overview()


def _load_current_db() -> None:
    global DB_PATH
    try:
        raw = DB_MARKER.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not raw:
        return
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = (ROOT / target).resolve()
    DB_PATH = target


_load_current_db()
init()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------- 算法记忆
_HAS_FTS = None


def has_fts5() -> bool:
    """FTS5 虚拟表是否可用；不可用则检索层自动落回纯 Python BM25。"""
    global _HAS_FTS
    if _HAS_FTS is None:
        try:
            get_conn().execute("SELECT 1 FROM chat_memory_fts LIMIT 1")
            _HAS_FTS = True
        except sqlite3.OperationalError:
            _HAS_FTS = False
    return bool(_HAS_FTS)


def _fts_insert(memory_id: int, content: str, subject: str = "",
                object_: str = "") -> None:
    if not has_fts5():
        return
    try:
        get_conn().execute(
            "INSERT INTO chat_memory_fts(memory_id, content, subject, object)"
            " VALUES(?,?,?,?)",
            (int(memory_id), str(content or ""), str(subject or ""),
             str(object_ or "")))
    except sqlite3.OperationalError:
        pass


def _fts_delete(memory_id: int) -> None:
    if not has_fts5():
        return
    try:
        execute("DELETE FROM chat_memory_fts WHERE memory_id=?", (int(memory_id),))
    except sqlite3.OperationalError:
        pass


def _decode_vec(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, (bytes, bytearray)):
        try:
            n = len(raw) // 4
            return list(struct.unpack(f"{n}f", raw[:n * 4]))
        except struct.error:
            return []
    try:
        obj = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [float(x) for x in obj] if isinstance(obj, list) else []


def _encode_vec(embedding) -> bytes:
    """L2 归一化后打包成 float32 二进制（体积减半且不丢精度）。"""
    vals = [float(x) for x in (embedding or [])]
    if not vals:
        return b""
    norm = (sum(x * x for x in vals)) ** 0.5 or 1.0
    return struct.pack(f"{len(vals)}f", *[x / norm for x in vals])


def set_memory_vector(memory_id: int, embedding, model: str = "hash") -> None:
    """写一条「记忆 × 模型」向量；同模型覆盖，换模型并存。"""
    blob = _encode_vec(embedding)
    if not memory_id or not blob:
        return
    execute(
        "INSERT INTO chat_memory_vectors(memory_id, model, dim, vec)"
        " VALUES(?,?,?,?) ON CONFLICT(memory_id, model)"
        " DO UPDATE SET dim=excluded.dim, vec=excluded.vec",
        (int(memory_id), str(model or "hash"), len(blob) // 4, blob))


# ------------------------------------------------------------ 本地 LSA 模型
def local_embedding_model(name: str = "local-lsa") -> dict | None:
    """取某个本地模型当前启用的版本（没有训练过返回 None）。"""
    return one("SELECT * FROM local_embed_models WHERE name=? AND active=1"
               " ORDER BY version DESC LIMIT 1", (str(name),))


def local_embedding_models(limit: int = 20) -> list[dict]:
    return query("SELECT * FROM local_embed_models ORDER BY id DESC LIMIT ?",
                 (int(limit or 20),))


def create_local_embedding_model(name: str, dim: int, corpus_count: int,
                                 term_count: int, source: str, params: dict,
                                 elapsed_ms: int = 0) -> int:
    """新建一个模型版本并把同名旧版本置为非启用。"""
    conn = get_conn()
    last = conn.execute(
        "SELECT version FROM local_embed_models WHERE name=?"
        " ORDER BY version DESC LIMIT 1", (str(name),)).fetchone()
    version = int(last["version"] if last else 0) + 1
    conn.execute("UPDATE local_embed_models SET active=0 WHERE name=?", (str(name),))
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO local_embed_models(name, version, kind, dim, corpus_count,"
        " term_count, source, params_json, active, trained_at, elapsed_ms,"
        " created_at) VALUES(?,?, 'local',?,?,?,?,?, 1,?,?,?)",
        (str(name), version, int(dim), int(corpus_count), int(term_count),
         str(source), json.dumps(params or {}, ensure_ascii=False),
         ts, int(elapsed_ms), ts))
    model_id = int(cur.lastrowid or 0)
    conn.execute(
        "INSERT OR REPLACE INTO local_embed_state(model_id, last_train_at,"
        " projected_count, corpus_at_train, updated_at) VALUES(?,?,0,?,?)",
        (model_id, ts, int(corpus_count), ts))
    conn.commit()
    return model_id


def save_local_embedding_terms(model_id: int, rows: list[tuple]) -> int:
    """批量写词项统计；rows = [(term, df, idf, vec_blob), ...]。"""
    if not model_id or not rows:
        return 0
    conn = get_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO local_embed_terms(model_id, term, df, idf, vec)"
        " VALUES(?,?,?,?,?)",
        [(int(model_id), str(t), int(df), float(idf), blob)
         for t, df, idf, blob in rows])
    conn.commit()
    return len(rows)


def local_embedding_terms(model_id: int) -> list[dict]:
    return query("SELECT term, df, idf, vec FROM local_embed_terms"
                 " WHERE model_id=? ORDER BY df DESC", (int(model_id),))


def local_embedding_state(model_id: int) -> dict | None:
    return one("SELECT * FROM local_embed_state WHERE model_id=?", (int(model_id),))


def update_local_embedding_state(model_id: int, *, projected_count=None,
                                 corpus_at_train=None) -> None:
    fields, args = ["updated_at=?"], [now_iso()]
    if projected_count is not None:
        fields.append("projected_count=?")
        args.append(int(projected_count))
        fields.append("last_project_at=?")
        args.append(now_iso())
    if corpus_at_train is not None:
        fields.append("corpus_at_train=?")
        args.append(int(corpus_at_train))
    execute("UPDATE local_embed_state SET " + ", ".join(fields)
            + " WHERE model_id=?", tuple(args) + (int(model_id),))


def local_vector_coverage(model: str) -> int:
    """某模型已经拥有向量的记忆条数。"""
    row = one("SELECT COUNT(*) AS c FROM chat_memory_vectors WHERE model=?",
              (str(model),))
    return int((row or {}).get("c") or 0)


def add_memory_item(user_id: int | None, character_id: str, session_id: str,
                    kind: str, content: str, *, importance: float = 0.5,
                    confidence: float = 0.7, subject: str = "",
                    object_: str = "", event_time: str = "",
                    source_turn: str = "", source_msg_ids=None,
                    embedding=None, embedding_model: str = "hash",
                    cluster_id=None) -> int:
    """新增一条结构化记忆，并同步向量表与 FTS5 索引。"""
    text = str(content or "").strip()
    if not text:
        return 0
    h = hashlib.sha1(re.sub(r"\s+", " ", text.lower()).encode("utf-8")).hexdigest()
    ts = now_iso()
    cur = get_conn().execute(
        "INSERT INTO chat_memory_items(user_id, character_id, session_id, kind,"
        " content, subject, object, importance, confidence, event_time,"
        " source_turn, source_msg_ids, status, hash, cluster_id, created_at,"
        " updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'active', ?,?,?,?)",
        (user_id, character_id or "", str(session_id), str(kind or "fact"),
         text, str(subject or ""), str(object_ or ""),
         float(importance or 0.5), float(confidence or 0.7),
         str(event_time or ""), str(source_turn or ""),
         json.dumps(list(source_msg_ids or []), ensure_ascii=False),
         h, cluster_id, ts, ts))
    mid = int(cur.lastrowid or 0)
    if mid and embedding:
        set_memory_vector(mid, embedding, embedding_model)
    _fts_insert(mid, text, subject, object_)
    return mid


def update_memory_item(memory_id: int, *, content: str | None = None,
                       importance: float | None = None,
                       confidence: float | None = None,
                       status: str | None = None,
                       source_turn: str | None = None,
                       event_time: str | None = None,
                       cluster_id: int | None = None,
                       merged_into: int | None = None,
                       last_recalled_at: str | None = None,
                       recall_count: int | None = None) -> None:
    fields, args = [], []
    for col, val in (("content", content), ("importance", importance),
                     ("confidence", confidence), ("status", status),
                     ("source_turn", source_turn), ("event_time", event_time),
                     ("cluster_id", cluster_id), ("merged_into", merged_into),
                     ("last_recalled_at", last_recalled_at),
                     ("recall_count", recall_count)):
        if val is not None:
            fields.append(f"{col}=?")
            args.append(val)
    if not fields:
        return
    fields.append("updated_at=?")
    args.append(now_iso())
    execute(f"UPDATE chat_memory_items SET {', '.join(fields)} WHERE id=?",
            tuple(args) + (int(memory_id),))
    if content is not None:
        row = one("SELECT id, content, subject, object FROM chat_memory_items"
                  " WHERE id=?", (int(memory_id),))
        if row:
            _fts_delete(memory_id)
            _fts_insert(memory_id, row["content"], row["subject"], row["object"])


def memory_items(user_id: int | None, character_id: str, session_id: str = "",
                 status: str = "active", limit: int = 300) -> list[dict]:
    """按会话/角色取结构化记忆（带向量）；status='' 表示全部状态。"""
    where = ["i.user_id IS ?"]
    args = [user_id]
    if character_id:
        where.append("i.character_id=?")
        args.append(str(character_id))
    if session_id:
        where.append("i.session_id=?")
        args.append(str(session_id))
    if status:
        where.append("i.status=?")
        args.append(str(status))
    sql = ("SELECT i.* FROM chat_memory_items i"
           " WHERE " + " AND ".join(where) + " ORDER BY i.id DESC")
    if limit and int(limit) > 0:
        sql += " LIMIT ?"
        args.append(int(limit))
    rows = query(sql, tuple(args))
    vecs: dict[int, dict] = {}
    ids = [int(r["id"]) for r in rows]
    if ids:
        marks = ",".join("?" * len(ids))
        for v in query("SELECT memory_id, model, vec FROM chat_memory_vectors"
                       f" WHERE memory_id IN ({marks})", tuple(ids)):
            vecs.setdefault(int(v["memory_id"]), {})[str(v["model"])] = \
                _decode_vec(v["vec"])
    out = []
    for r in rows:
        item = dict(r)
        item["vecs"] = vecs.get(int(item["id"]), {})
        # 兼容旧读取：vec 默认给 hash 基线向量
        item["vec"] = (item["vecs"].get("hash")
                       or next(iter(item["vecs"].values()), []))
        try:
            item["source_msg_ids"] = json.loads(item.get("source_msg_ids") or "[]")
        except (ValueError, TypeError):
            item["source_msg_ids"] = []
        out.append(item)
    return out


def memory_count(user_id: int | None, character_id: str, session_id: str = "",
                 status: str = "active") -> int:
    where = ["user_id IS ?"]
    args = [user_id]
    if character_id:
        where.append("character_id=?")
        args.append(str(character_id))
    if session_id:
        where.append("session_id=?")
        args.append(str(session_id))
    if status:
        where.append("status=?")
        args.append(str(status))
    row = one("SELECT COUNT(*) AS n FROM chat_memory_items WHERE "
              + " AND ".join(where), tuple(args))
    return int((row or {}).get("n") or 0) if row else 0


def memory_vector_by_hash(content_hash_value: str, model: str) -> list:
    """成本控制：同内容 hash + 同模型的向量直接复用，不重复请求。"""
    if not content_hash_value:
        return []
    row = one(
        "SELECT v.vec FROM chat_memory_vectors v"
        " JOIN chat_memory_items i ON i.id=v.memory_id"
        " WHERE i.hash=? AND v.model=? LIMIT 1",
        (str(content_hash_value), str(model or "hash")))
    return _decode_vec(row["vec"]) if row else []


def items_missing_vector(user_id: int | None, character_id: str, session_id: str,
                         model: str, limit: int = 64) -> list[dict]:
    """补算入口用：还没在当前模型空间建向量的活跃记忆。"""
    where = ["i.user_id IS ?", "i.status='active'"]
    args = [user_id]
    if character_id:
        where.append("i.character_id=?")
        args.append(str(character_id))
    if session_id:
        where.append("i.session_id=?")
        args.append(str(session_id))
    rows = query(
        "SELECT i.id, i.content, i.subject, i.object, i.hash FROM chat_memory_items i"
        " LEFT JOIN chat_memory_vectors v ON v.memory_id=i.id AND v.model=?"
        " WHERE v.memory_id IS NULL AND " + " AND ".join(where) +
        " ORDER BY i.id DESC LIMIT ?",
        (str(model or "hash"),) + tuple(args) + (max(1, int(limit or 64)),))
    return [dict(r) for r in rows]


def search_memory_fts(user_id: int | None, character_id: str, session_id: str,
                      query_text: str, limit: int = 50) -> dict[int, float]:
    """FTS5 检索；返回 {memory_id: 相关度}。不可用/无命中返回空 → 调用方用 BM25。

    FTS5 的 bm25() 越小越相关（通常为负），这里取反成“越大越相关”。
    """
    if not has_fts5():
        return {}
    # trigram 分词器要求查询词 ≥3 字；中文按 3 字滑窗切成候选词，
    # 英文按空白切分（短词交给纯 Python BM25 兜底）。
    terms: list[str] = []
    for word in re.findall(r"[A-Za-z0-9_]{3,}", str(query_text or "")):
        terms.append(word.lower())
    for run in re.findall(r"[\u4e00-\u9fff]{3,}", str(query_text or "")):
        terms.extend(run[i:i + 3] for i in range(len(run) - 2))
    seen, uniq = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    terms = uniq[:16]
    if not terms:
        return {}
    match = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
    where = ["i.user_id IS ?", "i.status='active'"]
    args = [user_id]
    if character_id:
        where.append("i.character_id=?")
        args.append(str(character_id))
    if session_id:
        where.append("i.session_id=?")
        args.append(str(session_id))
    try:
        rows = query(
            "SELECT f.memory_id AS memory_id, bm25(chat_memory_fts) AS score"
            " FROM chat_memory_fts f JOIN chat_memory_items i ON i.id=f.memory_id"
            " WHERE chat_memory_fts MATCH ? AND " + " AND ".join(where) +
            " ORDER BY score LIMIT ?", (match,) + tuple(args) + (int(limit),))
    except sqlite3.OperationalError:
        return {}
    return {int(r["memory_id"]): -float(r["score"] or 0.0) for r in rows}


def merge_memory_items(old_ids, new_id: int) -> None:
    """整合：旧条目归档并指向新条目（不物理删除）。"""
    for mid in old_ids or []:
        update_memory_item(int(mid), status="archived", merged_into=int(new_id))


def archive_memory_items(ids) -> None:
    ts = now_iso()
    for mid in ids or []:
        execute("UPDATE chat_memory_items SET status='archived', updated_at=?"
                " WHERE id=? AND status='active'", (ts, int(mid)))


def archive_stale_memories(user_id: int | None, character_id: str,
                           session_id: str, days: float = 60.0,
                           max_importance: float = 0.35) -> int:
    """遗忘层：长期未召回且低重要度 → 归档（保留行，召回时默认不参与）。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, float(days)))
              ).astimezone().isoformat(timespec="seconds")
    where = ["user_id IS ?", "status='active'", "importance < ?",
             "COALESCE(last_recalled_at, created_at) < ?"]
    args = [user_id, float(max_importance), cutoff]
    if character_id:
        where.append("character_id=?")
        args.append(str(character_id))
    if session_id:
        where.append("session_id=?")
        args.append(str(session_id))
    return int(execute(
        "UPDATE chat_memory_items SET status='archived', updated_at=?"
        " WHERE " + " AND ".join(where), (now_iso(),) + tuple(args)) or 0)


def bump_memory_recall(ids) -> None:
    ts = now_iso()
    for mid in ids or []:
        execute("UPDATE chat_memory_items SET recall_count=recall_count+1,"
                " last_recalled_at=?, updated_at=? WHERE id=?",
                (ts, ts, int(mid)))


def log_memory_recalls(user_id: int | None, session_id: str, turn_id: str,
                       retrieved: list[dict]) -> None:
    """可观测层：每轮记录召回了哪些条目、各分项得分、注入位置。"""
    if not retrieved:
        return
    ts = now_iso()
    for rank, item in enumerate(retrieved, 1):
        execute(
            "INSERT INTO chat_memory_recall_log(user_id, session_id, turn_id,"
            " memory_id, rank, injected_at, score_json, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (user_id, str(session_id or ""), str(turn_id or ""),
             int(item.get("id") or 0), rank,
             int(item.get("injected_at") or 0),
             json.dumps(item.get("scores") or {}, ensure_ascii=False), ts))


def memory_recalls(user_id: int | None, session_id: str = "",
                   limit: int = 20) -> list[dict]:
    """只读：最近几轮的记忆召回明细。"""
    where, args = ["user_id IS ?"], [user_id]
    if session_id:
        where.append("session_id=?")
        args.append(str(session_id))
    rows = query("SELECT * FROM chat_memory_recall_log WHERE "
                 + " AND ".join(where) + " ORDER BY id DESC LIMIT ?",
                 tuple(args) + (max(1, int(limit or 20)),))
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["scores"] = json.loads(item.pop("score_json") or "{}")
        except (ValueError, TypeError):
            item["scores"] = {}
        out.append(item)
    return out


def _purge_memory_items(where: str, args) -> None:
    ids = [int(r["id"]) for r in
           query("SELECT id FROM chat_memory_items WHERE " + where, args)]
    for mid in ids:
        execute("DELETE FROM chat_memory_vectors WHERE memory_id=?", (mid,))
        _fts_delete(mid)
    execute("DELETE FROM chat_memory_items WHERE " + where, args)

