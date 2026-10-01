"""SQLite 持久化：展签版本、校对流程与展柜绑定。

同一展柜同时只允许一个有效展签 —— 由 active_installations 部分唯一索引保证。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('curator','editor','keeper'))
);

CREATE TABLE IF NOT EXISTS cases (
    id          TEXT PRIMARY KEY,                 -- 展柜编号，如 C-101
    hall        TEXT NOT NULL,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exhibits (
    id          TEXT PRIMARY KEY,                 -- 展品编号，如 EX-0001
    title       TEXT NOT NULL,
    case_id     TEXT NOT NULL REFERENCES cases(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 展签版本：每次提交生成新版本；正文修订使旧校对结论失效。
CREATE TABLE IF NOT EXISTS label_versions (
    id              INTEGER PRIMARY KEY,
    exhibit_id      TEXT NOT NULL REFERENCES exhibits(id),
    version_no      INTEGER NOT NULL,
    title           TEXT NOT NULL,
    era             TEXT NOT NULL,
    material        TEXT NOT NULL,
    body            TEXT NOT NULL,
    qr_url          TEXT NOT NULL,
    lang            TEXT NOT NULL DEFAULT 'zh',
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    revision_of     INTEGER REFERENCES label_versions(id),
    content_hash    TEXT NOT NULL,                -- 正文指纹，修订即变化
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','approved','printed','superseded')),
    UNIQUE (exhibit_id, version_no)
);

-- 并排修订：原文与修订逐字段保存，供编辑器比对。
CREATE TABLE IF NOT EXISTS revisions (
    id              INTEGER PRIMARY KEY,
    new_version_id  INTEGER NOT NULL REFERENCES label_versions(id),
    base_version_id INTEGER REFERENCES label_versions(id),
    old_title       TEXT, new_title TEXT,
    old_era         TEXT, new_era TEXT,
    old_material    TEXT, new_material TEXT,
    old_body        TEXT, new_body TEXT,
    old_qr_url      TEXT, new_qr_url TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 校对批注（编辑：用语）。anchor = 校对时的 content_hash；正文改动后 hash 不再匹配 → 失效。
CREATE TABLE IF NOT EXISTS proofreads (
    id              INTEGER PRIMARY KEY,
    version_id      INTEGER NOT NULL REFERENCES label_versions(id),
    editor_id       INTEGER NOT NULL REFERENCES users(id),
    anchor_hash     TEXT NOT NULL,
    comment         TEXT NOT NULL,
    resolved        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_proof_version ON proofreads(version_id);

-- 保管员事实核对（展品编号）。curator 提交的版本不能由该 curator 本人批准。
CREATE TABLE IF NOT EXISTS fact_checks (
    id              INTEGER PRIMARY KEY,
    version_id      INTEGER NOT NULL UNIQUE REFERENCES label_versions(id),
    keeper_id       INTEGER NOT NULL REFERENCES users(id),
    exhibit_id_ok   INTEGER NOT NULL,            -- 展品编号与展柜是否一致
    anchor_hash     TEXT NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS print_jobs (
    id              INTEGER PRIMARY KEY,
    version_id      INTEGER NOT NULL REFERENCES label_versions(id),
    file_path       TEXT NOT NULL,                -- 本地展签样图
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 现场扫码：扫展签码 + 扫展柜码做配对复核。
CREATE TABLE IF NOT EXISTS scans (
    id              INTEGER PRIMARY KEY,
    label_version_id INTEGER NOT NULL REFERENCES label_versions(id),
    scanned_case_id TEXT NOT NULL REFERENCES cases(id),
    expected_case_id TEXT NOT NULL,
    matched         INTEGER NOT NULL,             -- 0 = 装错柜
    operator        TEXT NOT NULL,
    detail          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 安装/撤换记录。每展柜至多一条 active（end_time IS NULL）——部分唯一索引强制。
CREATE TABLE IF NOT EXISTS installations (
    id              INTEGER PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(id),
    exhibit_id      TEXT NOT NULL REFERENCES exhibits(id),
    version_id      INTEGER NOT NULL REFERENCES label_versions(id),
    scan_id         INTEGER REFERENCES scans(id),
    start_time      TEXT NOT NULL DEFAULT (datetime('now')),
    end_time        TEXT,
    reason          TEXT,                         -- 撤展原因
    operator        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_label_per_case
    ON installations(case_id) WHERE end_time IS NULL;

-- 异常事件：临时撤展 / 事实更正 / 旧展签未撤 / 二维码错误 / 多语言缺失 / 印刷色差
CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN (
                        'temporary_removal','fact_correction','stale_label',
                        'wrong_qr','missing_translation','print_color')),
    case_id         TEXT REFERENCES cases(id),
    version_id      INTEGER REFERENCES label_versions(id),
    description     TEXT NOT NULL,
    recorded_by     INTEGER NOT NULL REFERENCES users(id),
    resolved        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def log_event(conn: sqlite3.Connection, actor: str, action: str, payload: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (actor, action, payload) VALUES (?,?,?)",
        (actor, action, json.dumps(payload or {}, ensure_ascii=False)),
    )
