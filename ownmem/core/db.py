# -*- coding: utf-8 -*-
"""SQLite 存储层：连接管理、表结构、路径解析。

为什么用 SQLite 而不是 jsonl：
  - 三个写入方（hook 子进程 / MCP server / WebUI）会并发，SQLite 的 WAL 模式
    天然支持多进程读写，不需要自己维护文件锁
  - 审计日志需要「只增不改」的追加语义，单表 insert 最省心
  - 全部标准库，零依赖，数据仍 100% 在本地
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_HOME = Path.home() / ".ownmem"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=8000;

-- 记忆本体
-- kind：三本账（v2）。conclusion=结论账 / process=过程账 / pitfall=踩坑账
-- project：项目隔离（v2）。''=全局（所有项目可见），其余=只在该项目里召回
-- superseded_by：取代链（v3）。被推翻的旧条不删，指向取代它的新条 id；
--                非空 = 已作废，默认不再注入（但保留可查，历史可追溯）
-- supersedes：反向指针，新条记得自己推翻了谁
-- recurred / recurred_agents：踩坑计数器（v3）。同一坑被再次踩中 → 累计次数，
--                agents 记"谁栽过"，跨 agent 的坑权重更高
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    importance  TEXT NOT NULL DEFAULT 'normal',
    tags        TEXT NOT NULL DEFAULT '[]',
    source      TEXT NOT NULL DEFAULT 'unknown',
    agent       TEXT NOT NULL DEFAULT 'unknown',
    session_id  TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'conclusion',
    project     TEXT NOT NULL DEFAULT '',
    superseded_by     TEXT NOT NULL DEFAULT '',
    supersedes        TEXT NOT NULL DEFAULT '',
    recurred          INTEGER NOT NULL DEFAULT 0,
    recurred_agents   TEXT NOT NULL DEFAULT '[]',
    last_recurred_at  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT '',
    hits        INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active'   -- active | trashed
);

CREATE INDEX IF NOT EXISTS idx_mem_status  ON memories(status);
CREATE INDEX IF NOT EXISTS idx_mem_agent   ON memories(agent);
CREATE INDEX IF NOT EXISTS idx_mem_created ON memories(created_at);

-- 会话与模式（隐身/白纸的最小状态单元）
-- project：本会话归属项目，写入与召回都按它过滤（''=全局）
-- view：会话视角（v4）。normal=全量 / result-only=只看结论 / blind-review=盲审 /
--       cross-check=交叉验证。mode 管「能不能读」，view 管「读的时候看到什么」，
--       两个维度正交：blank/ghost 仍整体断读，view 只在可读时有意义。
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    agent       TEXT NOT NULL DEFAULT 'unknown',
    mode        TEXT NOT NULL DEFAULT 'normal',   -- normal | incognito | blank | ghost
    view        TEXT NOT NULL DEFAULT 'normal',   -- normal | result-only | blind-review | cross-check
    project     TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT ''
);

-- 会话沉淀（v2 3.1）：会话结束时把「这次干到哪了」落成一行，
-- 下次在同一项目里开新会话时由召回注入——这是跨对话延续感的来源。
CREATE TABLE IF NOT EXISTS session_digests (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL UNIQUE,
    agent       TEXT NOT NULL DEFAULT 'unknown',
    project     TEXT NOT NULL DEFAULT '',
    mode        TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL DEFAULT '',
    ended_at    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,                    -- 渲染好的沉淀正文（注入用）
    writes      TEXT NOT NULL DEFAULT '[]',       -- 本次写入的记忆摘要（JSON）
    topics      TEXT NOT NULL DEFAULT '[]',       -- 本次接触到的话题（JSON）
    recalls     INTEGER NOT NULL DEFAULT 0,
    writes_n    INTEGER NOT NULL DEFAULT 0,
    blocked_n   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- 审计日志：只增不改，产品可信度的地基
CREATE TABLE IF NOT EXISTS audit_log (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    event       TEXT NOT NULL,   -- recall | recall_blocked | write | write_blocked | promote | delete | wipe | mode | session_end
    agent       TEXT NOT NULL DEFAULT 'unknown',
    session_id  TEXT NOT NULL DEFAULT '',
    memory_id   TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    mode        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts      ON audit_log(ts);

-- 设置项
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# 表结构版本号。结构变了就 +1，connect() 会自动重跑一次 DDL。
# v1 → v2：三本账（kind）、项目隔离（project）、会话沉淀表（session_digests）
# v2 → v3：取代链（superseded_by / supersedes）、踩坑计数器（recurred / recurred_agents）
# v3 → v4：会话视角（sessions.view）
SCHEMA_VERSION = 4

# 探测时要确认存在的表（主判据是 user_version，这只是兜底）
_REQUIRED_TABLES = ("memories", "sessions", "audit_log", "settings", "session_digests")

# 补列之后才能建的索引：新列在 executescript 执行时还不存在（旧库），
# 所以这类索引必须等到 _upgrade() 跑完再建，否则 executescript 直接报错。
_POST_UPGRADE_DDL = """
CREATE INDEX IF NOT EXISTS idx_mem_project ON memories(project);
CREATE INDEX IF NOT EXISTS idx_mem_kind    ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_mem_super   ON memories(superseded_by);
CREATE INDEX IF NOT EXISTS idx_digest_project ON session_digests(project, created_at);
"""


def home_dir() -> Path:
    """数据目录：OWNMEM_HOME 环境变量优先，默认 ~/.ownmem。"""
    p = Path(os.environ.get("OWNMEM_HOME", str(DEFAULT_HOME)))
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return home_dir() / "ownmem.db"


_initialized: set[str] = set()


def _schema_ready(conn: sqlite3.Connection) -> bool:
    """库结构是否已是当前版本。

    每次连接都重跑一遍 DDL 要 ~95ms（实测），而探测只要 <1ms——
    hook 是每个用户回车都新起一个进程，这笔开销每天要付上千次，
    所以这里值得多写几行。

    主判据是 SQLite 自带的 user_version（4 字节，存在库头里，读一次即可）；
    再补四次极轻探测，防「版本号对但表被手工删过」这种极端情况。
    """
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            return False
        for table in _REQUIRED_TABLES:
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        return True
    except sqlite3.DatabaseError:
        return False


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _upgrade(conn: sqlite3.Connection) -> None:
    """旧库补列（幂等）。

    CREATE TABLE IF NOT EXISTS 只对「表不存在」生效，对有数据的旧表一个字都不会改——
    所以 v2 新增的 kind / project 必须显式 ALTER。加列对 SQLite 是纯元数据操作，
    不重写数据、不动已有行，旧库升级是安全的；已有 96 条记忆会落到默认值上
    （kind=conclusion，project='' 即全局），召回行为与升级前完全一致。
    """
    mem_cols = _columns(conn, "memories")
    if "kind" not in mem_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN kind TEXT NOT NULL DEFAULT 'conclusion'")
    if "project" not in mem_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN project TEXT NOT NULL DEFAULT ''")
    # v3：全部给空默认值 = 旧数据「没有被取代过、没复发过」，行为与升级前一致
    if "superseded_by" not in mem_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN superseded_by TEXT NOT NULL DEFAULT ''")
    if "supersedes" not in mem_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN supersedes TEXT NOT NULL DEFAULT ''")
    if "recurred" not in mem_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN recurred INTEGER NOT NULL DEFAULT 0")
    if "recurred_agents" not in mem_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN recurred_agents TEXT NOT NULL DEFAULT '[]'")
    if "last_recurred_at" not in mem_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN last_recurred_at TEXT NOT NULL DEFAULT ''")
    ses_cols = _columns(conn, "sessions")
    if "project" not in ses_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN project TEXT NOT NULL DEFAULT ''")
    if "view" not in ses_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN view TEXT NOT NULL DEFAULT 'normal'")
    conn.executescript(_POST_UPGRADE_DDL)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """打开连接并确保表结构存在（幂等）。

    「已初始化」有两层记忆：进程内用集合缓存（同一进程只探测一次），
    进程外靠库里的 user_version——新进程不必重跑 DDL。
    """
    p = path or db_path()
    conn = sqlite3.connect(str(p), timeout=8.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    key = str(p)
    if key not in _initialized:
        if not _schema_ready(conn):
            conn.executescript(SCHEMA)
            _upgrade(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _initialized.add(key)
    return conn


# ---------------------------------------------------------------------------
# 设置项（简单 KV）
# ---------------------------------------------------------------------------
def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
