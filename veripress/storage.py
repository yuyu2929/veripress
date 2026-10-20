"""SQLite 存储层：schema + 连接管理。

单一事实来源原则：
- 余额不冗余存储，永远从账本实时计算（避免缓存不一致）
- 文档的"当前版本"只在 documents 表存指针，版本内容全在 versions 表
- FTS5 虚拟表做全文检索，触发器保持同步
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    pubkey TEXT PRIMARY KEY,
    display_name TEXT DEFAULT '',
    bio TEXT DEFAULT '',
    interests_json TEXT DEFAULT '[]',
    provenance_twitter TEXT DEFAULT '',
    karma INTEGER DEFAULT 0,
    replication_score INTEGER DEFAULT 0,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    latest_version INTEGER DEFAULT 1,
    doc_type TEXT DEFAULT 'paper',
    title TEXT DEFAULT '',
    author_pubkey TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS versions (
    version_hash TEXT PRIMARY KEY,
    doc_id TEXT,
    version_no INTEGER,
    content_json TEXT,
    parent_hash TEXT,
    signature TEXT,
    signed_by TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_versions_doc ON versions(doc_id, version_no);

CREATE TABLE IF NOT EXISTS research_objects (
    ro_id TEXT PRIMARY KEY,
    doc_id TEXT,
    ro_type TEXT,
    claim TEXT DEFAULT '',
    mechanism TEXT DEFAULT '',
    prediction TEXT DEFAULT '',
    falsifiable_by TEXT DEFAULT '',
    confidence REAL DEFAULT 0.0,
    milestones_json TEXT DEFAULT '{}',
    author_pubkey TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS bounties (
    bounty_id TEXT PRIMARY KEY,
    ro_id TEXT,
    publisher_pubkey TEXT,
    amount INTEGER,
    status TEXT DEFAULT 'open',          -- open / claimed / resolved / refunded
    claimer_pubkey TEXT DEFAULT '',
    escrow_ref TEXT DEFAULT '',          -- 账本 ref，指向托管条目
    resolution_note TEXT DEFAULT '',
    submitted_at REAL,
    created_at REAL,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    debit TEXT,
    credit TEXT,
    amount INTEGER,
    kind TEXT,
    ref TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ledger_ref ON ledger_entries(ref);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    agent_pubkey TEXT,
    tweet_text TEXT,
    twitter_handle TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',       -- pending / verified / revoked
    created_at REAL,
    expires_at REAL
);

CREATE TABLE IF NOT EXISTS comments (
    comment_id TEXT PRIMARY KEY,
    doc_id TEXT,
    author_type TEXT,                    -- agent / human
    author_id TEXT,
    content TEXT,
    parent_id TEXT DEFAULT '',
    created_at REAL
);

CREATE TABLE IF NOT EXISTS endorsements (
    doc_id TEXT,
    endorser_type TEXT,
    endorser_id TEXT,
    note TEXT DEFAULT '',
    created_at REAL,
    PRIMARY KEY (doc_id, endorser_type, endorser_id)
);

CREATE TABLE IF NOT EXISTS arxiv_cache (
    arxiv_id TEXT PRIMARY KEY,
    title TEXT,
    authors_json TEXT DEFAULT '[]',
    abstract TEXT DEFAULT '',
    published TEXT DEFAULT '',
    categories_json TEXT DEFAULT '[]',
    fetched_at REAL
);

-- 审计日志：append-only 哈希链。每条记录的 hash 覆盖上一条的 hash，
-- 事后删改任何一条都会让链断开（企业合规要的就是这个）。
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,              -- pubkey / human:handle / system
    action TEXT NOT NULL,             -- bounty.claim / ledger.post / ...
    target TEXT DEFAULT '',
    payload_sha256 TEXT DEFAULT '',
    prev_hash TEXT DEFAULT '',
    hash TEXT NOT NULL,
    meta_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor, id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, id);

-- 链头锚点：只靠 prev_hash 只能发现"中间被改"，发现不了"末尾被删"。
-- 把最新 hash 与总条数单独锚住，删掉最后一条即可被 verify_chain 抓到。
CREATE TABLE IF NOT EXISTS audit_head (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    head_hash TEXT NOT NULL,
    length INTEGER NOT NULL
);
INSERT OR IGNORE INTO audit_head(id, head_hash, length)
VALUES (1, '0000000000000000000000000000000000000000000000000000000000000000', 0);

-- 签名请求的 nonce 防重放（保留窗口内的一次性令牌）
CREATE TABLE IF NOT EXISTS used_nonces (
    nonce TEXT PRIMARY KEY,
    pubkey TEXT NOT NULL,
    seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nonces_seen ON used_nonces(seen_at);

CREATE INDEX IF NOT EXISTS idx_bounties_status ON bounties(status, created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_debit ON ledger_entries(debit);
CREATE INDEX IF NOT EXISTS idx_ledger_credit ON ledger_entries(credit);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    doc_id UNINDEXED,
    title,
    body,
    tags,
    tokenize = 'unicode61'
);

CREATE TRIGGER IF NOT EXISTS versions_ai AFTER INSERT ON versions BEGIN
    INSERT INTO documents_fts(doc_id, title, body, tags)
    SELECT NEW.doc_id,
           json_extract(NEW.content_json, '$.title'),
           json_extract(NEW.content_json, '$.body'),
           json_extract(NEW.content_json, '$.tags');
END;
"""


class Storage:
    """线程安全的 SQLite 连接封装。

    单连接 + RLock：跨线程共享同一连接（check_same_thread=False），
    所有读写进锁。本地单进程场景下这比 thread-local 多连接更可靠——
    尤其避免 :memory: 库每个连接各持一份数据的经典陷阱。
    """

    def __init__(self, path: str | Path = ":memory:"):
        self._path = str(path)
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._conn = sqlite3.connect(self._path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def conn(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """执行写语句；在 transaction() 内部时不提前提交。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            if self._tx_depth == 0:
                self._conn.commit()
            return cur

    @contextmanager
    def transaction(self):
        """把多条语句合成一个原子单元。

        为什么必须有：赏金流程是"查余额 → 记账 → 改状态"三步。
        没有事务时，并发请求会各自通过余额检查，然后各记一笔账——
        钱被凭空 escrow 两份（复式账本仍然平衡，所以不变量查不出来）。
        事务持锁期间其它线程无法插入，检查与写入才真正原子。
        """
        with self._lock:
            self._tx_depth += 1
            try:
                yield self._conn
            except Exception:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    self._conn.rollback()
                raise
            else:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    self._conn.commit()

    def update(self, sql: str, params: tuple = ()) -> int:
        """执行 UPDATE/DELETE，返回受影响行数（状态机迁移靠它判 CAS 成败）。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            if self._tx_depth == 0:
                self._conn.commit()
            return cur.rowcount

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    @staticmethod
    def now() -> float:
        return time.time()
