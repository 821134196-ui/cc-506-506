"""领域服务：全部业务规则集中在此。

规则要点
--------
1. 修改正文 → 先前的校对结论失效（content_hash 锚定，hash 不匹配即失效）；
   失效版本不得打印、不得安装（现场不能安装过期版本）。
2. 同一展柜同时只允许一个有效展签（DB 部分唯一索引 + 服务层前置检查）。
3. 策展人不能批准自己提交版本的事实核对（角色分离 + 本人回避）。
4. 现场扫码：展签二维码与展柜码配对，装错柜拦截，不得安装。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

from .db import log_event


class DomainError(Exception):
    """业务规则冲突，HTTP 层映射为 409。"""


# ---------------------------------------------------------------- 工具

def content_hash(title: str, era: str, material: str, body: str, qr_url: str) -> str:
    raw = "\x1f".join([title.strip(), era.strip(), material.strip(), body.strip(), qr_url.strip()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise DomainError(f"用户 {user_id} 不存在")
    return row


# ---------------------------------------------------------------- 展品 / 展柜

def list_exhibits(conn: sqlite3.Connection) -> list[dict]:
    """展品目录：同时给出当前有效版次与待换版次。"""
    rows = conn.execute(
        """
        SELECT e.id AS exhibit_id, e.title AS exhibit_title,
               c.id AS case_id, c.name AS case_name, c.hall,
               lv.id AS cur_version_id, lv.version_no AS cur_version_no,
               lv.status AS cur_status,
               (SELECT id FROM label_versions v2
                 WHERE v2.exhibit_id=e.id AND v2.status IN ('approved','printed')
                   AND (lv.id IS NULL OR v2.id <> lv.id)
                 ORDER BY v2.version_no DESC LIMIT 1) AS pending_version_id
        FROM exhibits e
        JOIN cases c ON c.id = e.case_id
        LEFT JOIN installations ins ON ins.case_id=c.id AND ins.end_time IS NULL
        LEFT JOIN label_versions lv ON lv.id=ins.version_id
        ORDER BY c.id, e.id
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["pending_version_id"]:
            p = conn.execute(
                "SELECT version_no FROM label_versions WHERE id=?",
                (d["pending_version_id"],),
            ).fetchone()
            d["pending_version_no"] = p["version_no"] if p else None
        else:
            d["pending_version_no"] = None
        out.append(d)
    return out


# ---------------------------------------------------------------- 版本

def create_version(
    conn: sqlite3.Connection, *, actor_id: int, exhibit_id: str,
    title: str, era: str, material: str, body: str, qr_url: str,
    lang: str = "zh", base_version_id: int | None = None, note: str = "",
) -> dict:
    """策展人提交（或修订）展签内容，生成新版本草稿。"""
    user = _user(conn, actor_id)
    exhibit = conn.execute("SELECT * FROM exhibits WHERE id=?", (exhibit_id,)).fetchone()
    if not exhibit:
        raise DomainError(f"展品 {exhibit_id} 不存在")
    if not all([title.strip(), era.strip(), material.strip(), body.strip(), qr_url.strip()]):
        raise DomainError("名称、年代、材质、说明文字、二维码链接均不能为空")

    row = conn.execute(
        "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM label_versions WHERE exhibit_id=?",
        (exhibit_id,),
    ).fetchone()
    version_no = row["n"]
    chash = content_hash(title, era, material, body, qr_url)

    cur = conn.execute(
        """INSERT INTO label_versions
           (exhibit_id, version_no, title, era, material, body, qr_url, lang,
            created_by, revision_of, content_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (exhibit_id, version_no, title.strip(), era.strip(), material.strip(),
         body.strip(), qr_url.strip(), lang, actor_id, base_version_id, chash),
    )
    new_id = cur.lastrowid

    if base_version_id:
        base = conn.execute("SELECT * FROM label_versions WHERE id=?", (base_version_id,)).fetchone()
        if not base or base["exhibit_id"] != exhibit_id:
            raise DomainError("修订基线版本无效")
        conn.execute(
            """INSERT INTO revisions
               (new_version_id, base_version_id,
                old_title,new_title, old_era,new_era, old_material,new_material,
                old_body,new_body, old_qr_url,new_qr_url, note)
               VALUES (?,?, ?,?, ?,?, ?,?, ?,?, ?,?,?)""",
            (new_id, base_version_id,
             base["title"], title.strip(), base["era"], era.strip(),
             base["material"], material.strip(), base["body"], body.strip(),
             base["qr_url"], qr_url.strip(), note),
        )
    # 正文改动的修订留痕；是否"过期"在打印/安装环节按是否已有更新的批准版本判定，
    # 旧版在被新版实际撤换前仍可保留在展柜上（见 mark_printed / install_label）。

    log_event(conn, user["username"], "version.create",
              {"version_id": new_id, "exhibit_id": exhibit_id, "version_no": version_no})
    conn.commit()
    return get_version(conn, new_id)


def update_draft_content(
    conn: sqlite3.Connection, *, actor_id: int, version_id: int,
    title: str, era: str, material: str, body: str, qr_url: str, note: str = "",
) -> dict:
    """草稿阶段修改正文：版本号不变、正文指纹变化 → 先前校对结论立即失效。

    已批准/已打印的版本不可改（现场不能出现被悄悄改动的过期版本），
    需要改内容必须走 create_version 开新版。
    """
    user = _user(conn, actor_id)
    v = conn.execute("SELECT * FROM label_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise DomainError("版本不存在")
    if v["created_by"] != actor_id:
        raise DomainError("只能修改自己提交的草稿")
    if v["status"] != "draft":
        raise DomainError(f"版本已 {v['status']}，正文锁定；请基于它创建新版本")
    if not all([title.strip(), era.strip(), material.strip(), body.strip(), qr_url.strip()]):
        raise DomainError("各字段均不能为空")

    new_hash = content_hash(title, era, material, body, qr_url)
    old = dict(v)
    conn.execute(
        """UPDATE label_versions SET title=?, era=?, material=?, body=?, qr_url=?,
               content_hash=? WHERE id=?""",
        (title.strip(), era.strip(), material.strip(), body.strip(), qr_url.strip(),
         new_hash, version_id),
    )
    # 记录并排修订；anchor_hash 留在旧 hash 上的批注自动失效（见 proofread_state）。
    conn.execute(
        """INSERT INTO revisions
           (new_version_id, base_version_id,
            old_title,new_title, old_era,new_era, old_material,new_material,
            old_body,new_body, old_qr_url,new_qr_url, note)
           VALUES (?,?, ?,?, ?,?, ?,?, ?,?, ?,?,?)""",
        (version_id, version_id,
         old["title"], title.strip(), old["era"], era.strip(),
         old["material"], material.strip(), old["body"], body.strip(),
         old["qr_url"], qr_url.strip(), note or "草稿正文修改，旧校对结论失效"),
    )
    conn.execute("DELETE FROM fact_checks WHERE version_id=?", (version_id,))
    log_event(conn, user["username"], "version.edit_draft",
              {"version_id": version_id, "old_hash": old["content_hash"],
               "new_hash": new_hash})
    conn.commit()
    return get_version(conn, version_id)


def list_versions(conn: sqlite3.Connection, exhibit_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT lv.*, u.display_name AS author_name FROM label_versions lv
           JOIN users u ON u.id=lv.created_by
           WHERE lv.exhibit_id=? ORDER BY lv.version_no DESC""",
        (exhibit_id,),
    ).fetchall()
    return [get_version(conn, r["id"]) for r in rows]


def get_version(conn: sqlite3.Connection, version_id: int) -> dict:
    r = conn.execute(
        """SELECT lv.*, u.display_name AS author_name, e.title AS exhibit_title,
                  e.case_id, c.name AS case_name
           FROM label_versions lv
           JOIN users u ON u.id=lv.created_by
           JOIN exhibits e ON e.id=lv.exhibit_id
           JOIN cases c ON c.id=e.case_id
           WHERE lv.id=?""",
        (version_id,),
    ).fetchone()
    if not r:
        raise DomainError(f"版本 {version_id} 不存在")
    d = dict(r)
    d["proofread_ok"] = proofread_state(conn, version_id)
    fc = conn.execute(
        "SELECT fc.*, u.display_name AS keeper_name FROM fact_checks fc "
        "JOIN users u ON u.id=fc.keeper_id WHERE version_id=?", (version_id,),
    ).fetchone()
    d["fact_check"] = dict(fc) if fc else None
    rev = conn.execute("SELECT * FROM revisions WHERE new_version_id=?", (version_id,)).fetchone()
    d["revision"] = dict(rev) if rev else None
    d["ready_to_print"] = d["status"] == "approved" and d["proofread_ok"] and bool(fc and fc["exhibit_id_ok"])
    return d


# ---------------------------------------------------------------- 校对（编辑）

def add_proofread(conn: sqlite3.Connection, *, actor_id: int, version_id: int, comment: str) -> dict:
    user = _user(conn, actor_id)
    if user["role"] != "editor":
        raise DomainError("只有编辑可以添加校对批注")
    v = conn.execute("SELECT * FROM label_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise DomainError("版本不存在")
    cur = conn.execute(
        "INSERT INTO proofreads (version_id, editor_id, anchor_hash, comment) VALUES (?,?,?,?)",
        (version_id, actor_id, v["content_hash"], comment),
    )
    log_event(conn, user["username"], "proofread.add",
              {"version_id": version_id, "comment": comment})
    conn.commit()
    return {"id": cur.lastrowid, "valid": True, "comment": comment}


def proofread_state(conn: sqlite3.Connection, version_id: int) -> bool:
    """校对结论有效 = 至少一条批注，且所有批注锚定的 hash 与当前正文一致。"""
    v = conn.execute("SELECT content_hash FROM label_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        return False
    rows = conn.execute("SELECT anchor_hash FROM proofreads WHERE version_id=? AND resolved=0",
                        (version_id,)).fetchall()
    return bool(rows) and all(r["anchor_hash"] == v["content_hash"] for r in rows)


def list_proofreads(conn: sqlite3.Connection, version_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT p.*, u.display_name AS editor_name,
                  (p.anchor_hash = lv.content_hash) AS valid
           FROM proofreads p
           JOIN users u ON u.id=p.editor_id
           JOIN label_versions lv ON lv.id=p.version_id
           WHERE p.version_id=? ORDER BY p.id""",
        (version_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 事实核对（保管员）

def fact_check(conn: sqlite3.Connection, *, actor_id: int, version_id: int,
               exhibit_id_ok: bool, note: str = "") -> dict:
    """保管员核对展品编号与展柜绑定。策展人不能批准自己提交的版本。"""
    user = _user(conn, actor_id)
    if user["role"] != "keeper":
        raise DomainError("只有保管员可以进行展品编号事实核对（角色分离）")
    v = conn.execute("SELECT * FROM label_versions WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise DomainError("版本不存在")
    if v["created_by"] == actor_id:
        raise DomainError("策展人不能批准自己的事实核对（职责分离）")

    conn.execute("DELETE FROM fact_checks WHERE version_id=?", (version_id,))
    conn.execute(
        """INSERT INTO fact_checks (version_id, keeper_id, exhibit_id_ok, anchor_hash, note)
           VALUES (?,?,?,?,?)""",
        (version_id, actor_id, 1 if exhibit_id_ok else 0, v["content_hash"], note),
    )
    if exhibit_id_ok and proofread_state(conn, version_id):
        conn.execute("UPDATE label_versions SET status='approved' WHERE id=?", (version_id,))
        # 新版校对+编号核对齐全即生效：同展品旧的已批准/已打印版成为过期版本
        # （展柜上的旧签保留到物理撤换，但不得再打印、再新装）。
        conn.execute(
            """UPDATE label_versions SET status='superseded'
               WHERE exhibit_id=? AND version_no < ? AND status IN ('approved','printed')""",
            (v["exhibit_id"], v["version_no"]),
        )
    log_event(conn, user["username"], "factcheck.submit",
              {"version_id": version_id, "ok": bool(exhibit_id_ok)})
    conn.commit()
    return get_version(conn, version_id)


# ---------------------------------------------------------------- 打印（模拟打印机）

def mark_printed(conn: sqlite3.Connection, *, actor_id: int, version_id: int,
                 file_path: str) -> dict:
    v = get_version(conn, version_id)
    if not v["proofread_ok"]:
        raise DomainError("校对结论缺失或已因正文修改失效，不能打印过期版本")
    fc = v["fact_check"]
    if not fc or not fc["exhibit_id_ok"] or fc["anchor_hash"] != v["content_hash"]:
        raise DomainError("保管员编号核对未通过或已失效，不能打印")
    if v["status"] == "superseded":
        raise DomainError("此版已被更新版次取代（superseded），不能打印过期版本")
    if v["status"] not in ("approved", "printed"):
        raise DomainError(f"版本状态为 {v['status']}，未获批准，不能打印")
    conn.execute(
        "INSERT INTO print_jobs (version_id, file_path, created_by) VALUES (?,?,?)",
        (version_id, file_path, actor_id),
    )
    conn.execute("UPDATE label_versions SET status='printed' WHERE id=? AND status='approved'",
                 (version_id,))
    user = _user(conn, actor_id)
    log_event(conn, user["username"], "label.printed",
              {"version_id": version_id, "file": file_path})
    conn.commit()
    return get_version(conn, version_id)


def print_queue(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT pj.*, lv.version_no, lv.exhibit_id, u.display_name AS who
           FROM print_jobs pj JOIN label_versions lv ON lv.id=pj.version_id
           JOIN users u ON u.id=pj.created_by ORDER BY pj.id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 现场扫码 + 安装

def scan_label(conn: sqlite3.Connection, *, label_version_id: int, scanned_case_id: str,
               operator: str) -> dict:
    """现场扫码复核：展签码指向的展柜 vs 实际扫到的展柜码。"""
    v = get_version(conn, label_version_id)
    expected = v["case_id"]
    case = conn.execute("SELECT * FROM cases WHERE id=?", (scanned_case_id,)).fetchone()
    if not case:
        raise DomainError(f"展柜 {scanned_case_id} 不存在")
    matched = scanned_case_id == expected

    # 二维码指向错误 / 装错柜：记录明细，但仍允许登记事件（不产生安装）。
    detail = "展签与展柜匹配" if matched else (
        f"装错柜：展签 v{v['version_no']}（{v['exhibit_id']}）应装入 {expected}，"
        f"现场扫到 {scanned_case_id}（{case['name']}）"
    )
    cur = conn.execute(
        """INSERT INTO scans (label_version_id, scanned_case_id, expected_case_id,
                              matched, operator, detail)
           VALUES (?,?,?,?,?,?)""",
        (label_version_id, scanned_case_id, expected, 1 if matched else 0,
         operator, detail),
    )
    log_event(conn, operator, "scan." + ("match" if matched else "mismatch"),
              {"version_id": label_version_id, "expected": expected,
               "scanned": scanned_case_id})
    conn.commit()
    return {"id": cur.lastrowid, "matched": bool(matched), "detail": detail,
            "expected_case_id": expected, "scanned_case_id": scanned_case_id}


def install_label(conn: sqlite3.Connection, *, actor_name: str, label_version_id: int,
                  scan_id: int) -> dict:
    """凭"匹配"的扫码记录安装；同柜旧展签自动撤换，保证一柜一有效展签。"""
    v = get_version(conn, label_version_id)
    scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not scan or scan["label_version_id"] != label_version_id:
        raise DomainError("扫码记录与展签不符")
    if not scan["matched"]:
        raise DomainError("扫码复核未通过（装错柜），禁止安装")
    if not v["proofread_ok"]:
        raise DomainError("校对已失效，现场不能安装过期版本")
    fc = v["fact_check"]
    if not fc or not fc["exhibit_id_ok"] or fc["anchor_hash"] != v["content_hash"]:
        raise DomainError("编号核对失效，不能安装")
    if v["status"] == "superseded":
        raise DomainError("此版已被更新版次取代，现场不能安装过期版本")
    if v["status"] != "printed":
        raise DomainError(f"版本状态为 {v['status']}，展签尚未打印，不能安装")

    case_id = scan["scanned_case_id"]
    # 同柜旧有效展签 → 记为撤换（唯一索引兜底）。
    old = conn.execute(
        "SELECT * FROM installations WHERE case_id=? AND end_time IS NULL", (case_id,),
    ).fetchone()
    if old:
        conn.execute(
            "UPDATE installations SET end_time=?, reason=? WHERE id=?",
            (_now(), f"被 v{v['version_no']} 撤换", old["id"]),
        )
    # 若该展签此前装在别的柜子（装错后纠正），同步结束。
    conn.execute(
        "UPDATE installations SET end_time=?, reason='移至正确展柜' WHERE version_id=? AND end_time IS NULL",
        (_now(), label_version_id),
    )
    cur = conn.execute(
        """INSERT INTO installations (case_id, exhibit_id, version_id, scan_id, operator)
           VALUES (?,?,?,?,?)""",
        (case_id, v["exhibit_id"], label_version_id, scan_id, actor_name),
    )
    log_event(conn, actor_name, "label.install",
              {"case_id": case_id, "version_id": label_version_id})
    conn.commit()
    return {"installation_id": cur.lastrowid, "case_id": case_id,
            "replaced": old["version_id"] if old else None}


def remove_label(conn: sqlite3.Connection, *, actor_name: str, case_id: str,
                 reason: str) -> dict:
    """临时撤展 / 旧展签撤除。"""
    row = conn.execute(
        "SELECT * FROM installations WHERE case_id=? AND end_time IS NULL", (case_id,),
    ).fetchone()
    if not row:
        raise DomainError(f"展柜 {case_id} 当前没有有效展签")
    conn.execute("UPDATE installations SET end_time=?, reason=? WHERE id=?",
                 (_now(), reason, row["id"]))
    log_event(conn, actor_name, "label.remove", {"case_id": case_id, "reason": reason})
    conn.commit()
    return {"case_id": case_id, "removed_version_id": row["version_id"]}


def active_installations(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT ins.*, lv.version_no, lv.title, c.name AS case_name,
                  e.title AS exhibit_title
           FROM installations ins
           JOIN label_versions lv ON lv.id=ins.version_id
           JOIN exhibits e ON e.id=ins.exhibit_id
           JOIN cases c ON c.id=ins.case_id
           WHERE ins.end_time IS NULL ORDER BY ins.case_id"""
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 事件 & 撤换记录

INCIDENT_LABELS = {
    "temporary_removal": "展品临时撤展",
    "fact_correction": "文字事实更正",
    "stale_label": "旧展签未撤",
    "wrong_qr": "二维码指向错误",
    "missing_translation": "多语言版本缺失",
    "print_color": "印刷色差",
}


def record_incident(conn: sqlite3.Connection, *, actor_id: int, kind: str,
                    description: str, case_id: str | None = None,
                    version_id: int | None = None) -> dict:
    if kind not in INCIDENT_LABELS:
        raise DomainError(f"未知事件类型 {kind}")
    cur = conn.execute(
        """INSERT INTO incidents (kind, case_id, version_id, description, recorded_by)
           VALUES (?,?,?,?,?)""",
        (kind, case_id, version_id, description, actor_id),
    )
    user = _user(conn, actor_id)
    log_event(conn, user["username"], "incident.record",
              {"kind": kind, "case_id": case_id, "description": description})
    conn.commit()
    return {"id": cur.lastrowid, "kind": kind}


def list_incidents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT i.*, u.display_name AS who, c.name AS case_name
           FROM incidents i JOIN users u ON u.id=i.recorded_by
           LEFT JOIN cases c ON c.id=i.case_id
           ORDER BY i.id DESC"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["kind_label"] = INCIDENT_LABELS[d["kind"]]
        out.append(d)
    return out


def installation_history(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT ins.*, lv.version_no, c.name AS case_name
           FROM installations ins JOIN label_versions lv ON lv.id=ins.version_id
           JOIN cases c ON c.id=ins.case_id
           ORDER BY ins.start_time DESC, ins.id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def list_scans(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM scans ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]
