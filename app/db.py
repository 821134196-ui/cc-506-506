"""SQLite 数据层：建表、连接、迁移。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 工作人员：策展人 / 编辑 / 保管员 / 现场更换人员
CREATE TABLE IF NOT EXISTS staff (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL CHECK (role IN ('curator','editor','keeper','installer')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 展品
CREATE TABLE IF NOT EXISTS exhibit (
    id            INTEGER PRIMARY KEY,
    catalog_no    TEXT NOT NULL UNIQUE,          -- 展品编号，保管员核对用
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 展柜
CREATE TABLE IF NOT EXISTS case_table (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,            -- 展柜编号，如 A-01
    hall        TEXT NOT NULL DEFAULT '常设展厅',
    exhibit_id  INTEGER REFERENCES exhibit(id) ON DELETE SET NULL  -- 柜内当前展品
);

-- 展签版本（同一展品多版本；正文内容以 content_hash 判定是否变化）
CREATE TABLE IF NOT EXISTS label_version (
    id            INTEGER PRIMARY KEY,
    exhibit_id    INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
    version_no    INTEGER NOT NULL,
    title         TEXT NOT NULL,                 -- 名称
    dynasty       TEXT NOT NULL,                 -- 年代
    material      TEXT NOT NULL,                  -- 材质
    body          TEXT NOT NULL,                 -- 说明文字（正文）
    qr_url        TEXT NOT NULL,                 -- 二维码指向链接
    lang          TEXT NOT NULL DEFAULT 'zh',
    content_hash  TEXT NOT NULL,
    created_by    INTEGER NOT NULL REFERENCES staff(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (exhibit_id, lang, version_no)
);

-- 校对/事实核对结论
-- kind=proofread 编辑校对用语；kind=factcheck 保管员事实核对（展品编号）
CREATE TABLE IF NOT EXISTS review (
    id           INTEGER PRIMARY KEY,
    version_id   INTEGER NOT NULL REFERENCES label_version(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('proofread','factcheck')),
    passed       INTEGER NOT NULL,               -- 1 通过 / 0 存疑
    reviewer_id  INTEGER NOT NULL REFERENCES staff(id),
    note         TEXT NOT NULL DEFAULT '',
    reviewed_hash TEXT NOT NULL DEFAULT '',          -- 结论锚定的正文哈希
    status       TEXT NOT NULL DEFAULT 'valid'
                 CHECK (status IN ('valid','superseded')),  -- 被新版本取代
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 校对批注（锚定版本；正文一改，旧批注随结论一并失效）
CREATE TABLE IF NOT EXISTS annotation (
    id           INTEGER PRIMARY KEY,
    version_id   INTEGER NOT NULL REFERENCES label_version(id) ON DELETE CASCADE,
    author_id    INTEGER NOT NULL REFERENCES staff(id),
    quote        TEXT NOT NULL DEFAULT '',       -- 引用的原文片段
    comment      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open','resolved','stale')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 打印任务：打印清单中的每一张
CREATE TABLE IF NOT EXISTS print_job (
    id           INTEGER PRIMARY KEY,
    version_id   INTEGER NOT NULL REFERENCES label_version(id) ON DELETE CASCADE,
    proof_path   TEXT NOT NULL,                  -- 本地展签样图（SVG）
    qr_payload   TEXT NOT NULL,                  -- 二维码编码内容
    color_delta  REAL NOT NULL DEFAULT 0,        -- 模拟印刷色差 ΔE
    status       TEXT NOT NULL DEFAULT 'printed'
                 CHECK (status IN ('printed','installed','void')),
    printed_by   INTEGER NOT NULL REFERENCES staff(id),
    printed_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 展柜与展签版本的绑定（装柜记录）。
-- 有效绑定 = valid=1；同一展柜同时最多一条有效绑定（部分唯一索引）。
CREATE TABLE IF NOT EXISTS case_binding (
    id           INTEGER PRIMARY KEY,
    case_id      INTEGER NOT NULL REFERENCES case_table(id),
    version_id   INTEGER NOT NULL REFERENCES label_version(id),
    print_job_id INTEGER REFERENCES print_job(id),
    valid        INTEGER NOT NULL DEFAULT 1,
    installed_at TEXT,
    removed_at   TEXT,
    note         TEXT NOT NULL DEFAULT ''
);
-- 同一展柜至多一个有效展签
CREATE UNIQUE INDEX IF NOT EXISTS ux_case_active
    ON case_binding(case_id) WHERE valid = 1;

-- 现场扫码记录（每次扫码都留痕，无论通过与否）
CREATE TABLE IF NOT EXISTS scan_log (
    id           INTEGER PRIMARY KEY,
    qr_payload   TEXT NOT NULL,
    scanned_case INTEGER NOT NULL REFERENCES case_table(id),
    scanner_id   INTEGER NOT NULL REFERENCES staff(id),
    expected_case INTEGER REFERENCES case_table(id),
    accepted     INTEGER NOT NULL,               -- 1 放行安装 / 0 拒绝（装错柜等）
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 问题记录：临时撤展 / 事实更正 / 旧签未撤 / 二维码错误 / 多语言缺失 / 印刷色差
CREATE TABLE IF NOT EXISTS issue (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN (
                     'temporary_removal','fact_correction','old_label_remains',
                     'qr_wrong_target','missing_translation','print_color_delta')),
    exhibit_id   INTEGER REFERENCES exhibit(id) ON DELETE SET NULL,
    case_id      INTEGER REFERENCES case_table(id) ON DELETE SET NULL,
    version_id   INTEGER REFERENCES label_version(id) ON DELETE SET NULL,
    reported_by  INTEGER REFERENCES staff(id),
    detail       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open','resolved')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
