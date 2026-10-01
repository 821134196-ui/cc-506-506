"""领域服务：版本、校对、打印、绑定、扫码的全部业务规则。

角色：
  curator 策展人 —— 提交展签内容；不能批准自己提交版本的事实核对
  editor  编辑   —— 校对用语（proofread）
  keeper  保管员 —— 事实核对、核对展品编号（factcheck）
  installer 现场 —— 扫码安装、撤换

关键规则：
  1. 正文（或任一展示字段）修改 → content_hash 变化 → 旧校对结论失效；
     编辑一律生成新版本，现场只能安装展品的最新版本（过期版本拒装）。
  2. 同一展柜同时只允许一个有效展签（DB 部分唯一索引 + 服务层换版逻辑）。
  3. 角色分离：proofread 仅 editor；factcheck 仅 keeper，且不得是版本提交人。
  4. 扫码复核：二维码解析出的目标展柜必须与现场展柜一致，且版本仍有效。
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from .adapters.printer import PrinterAdapter
from .adapters.qr import QRAdapter

# 印刷色差容差（ΔE2000 量级的模拟值）
COLOR_TOLERANCE = 3.0


class DomainError(Exception):
    """业务规则拒绝。"""


def _hash_content(title: str, dynasty: str, material: str,
                  body: str, qr_url: str, lang: str) -> str:
    raw = "\x1f".join([title, dynasty, material, body, qr_url, lang])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class ScanResult:
    accepted: bool
    reason: str
    binding_id: int | None = None


class MuseumService:
    def __init__(self, conn: sqlite3.Connection,
                 qr: QRAdapter | None = None,
                 printer: PrinterAdapter | None = None):
        self.db = conn
        self.qr = qr or QRAdapter()
        self.printer = printer or PrinterAdapter()

    # ---------- 人员与目录 ----------
    def add_staff(self, name: str, role: str) -> int:
        cur = self.db.execute(
            "INSERT INTO staff(name, role) VALUES (?,?)", (name, role))
        self.db.commit()
        return cur.lastrowid

    def staff(self, staff_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()
        if not row:
            raise DomainError(f"人员 {staff_id} 不存在")
        return row

    def add_exhibit(self, catalog_no: str, name: str) -> int:
        cur = self.db.execute(
            "INSERT INTO exhibit(catalog_no, name) VALUES (?,?)",
            (catalog_no, name))
        self.db.commit()
        return cur.lastrowid

    def add_case(self, code: str, hall: str, exhibit_id: int | None) -> int:
        cur = self.db.execute(
            "INSERT INTO case_table(code, hall, exhibit_id) VALUES (?,?,?)",
            (code, hall, exhibit_id))
        self.db.commit()
        return cur.lastrowid

    # ---------- 展签版本 ----------
    def submit_version(self, exhibit_id: int, *, title: str, dynasty: str,
                       material: str, body: str, qr_url: str, lang: str,
                       staff_id: int) -> int:
        """策展人提交新版本。任何内容修改都产生新版本（旧版随即成为过期版本）。"""
        user = self.staff(staff_id)
        if user["role"] != "curator":
            raise DomainError("只有策展人可以提交展签内容")
        row = self.db.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM label_version "
            "WHERE exhibit_id=? AND lang=?", (exhibit_id, lang)).fetchone()
        version_no = row["n"]
        h = _hash_content(title, dynasty, material, body, qr_url, lang)
        cur = self.db.execute(
            """INSERT INTO label_version
               (exhibit_id, version_no, title, dynasty, material, body,
                qr_url, lang, content_hash, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (exhibit_id, version_no, title, dynasty, material, body,
             qr_url, lang, h, staff_id))
        # 同语言旧版本上未解决的批注标记为 stale（正文已变，结论失效）；
        # 新增其他语言版本不应使在展版本失效
        self.db.execute(
            """UPDATE annotation SET status='stale'
               WHERE version_id IN (
                   SELECT id FROM label_version
                   WHERE exhibit_id=? AND lang=? AND id<>?) AND status='open'""",
            (exhibit_id, lang, cur.lastrowid))
        # 旧版本上的通过/存疑结论一律 superseded——正文修改后必须重新双签
        self.db.execute(
            """UPDATE review SET status='superseded'
               WHERE version_id IN (
                   SELECT id FROM label_version
                   WHERE exhibit_id=? AND lang=? AND id<>?)""",
            (exhibit_id, lang, cur.lastrowid))
        self.db.commit()
        return cur.lastrowid

    def get_version(self, version_id: int) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM label_version WHERE id=?", (version_id,)).fetchone()
        if not row:
            raise DomainError(f"版本 {version_id} 不存在")
        return row

    def latest_version(self, exhibit_id: int, lang: str = "zh") -> sqlite3.Row | None:
        # 版本按语言并行：中文版的修订不会使英文版过期，反之亦然
        return self.db.execute(
            "SELECT * FROM label_version WHERE exhibit_id=? AND lang=? "
            "ORDER BY version_no DESC LIMIT 1", (exhibit_id, lang)).fetchone()

    def is_latest(self, version_id: int) -> bool:
        v = self.get_version(version_id)
        latest = self.latest_version(v["exhibit_id"], v["lang"])
        return latest is not None and latest["id"] == version_id

    # ---------- 校对 ----------
    def annotate(self, version_id: int, staff_id: int, comment: str,
                 quote: str = "") -> int:
        user = self.staff(staff_id)
        if user["role"] != "editor":
            raise DomainError("只有编辑可以撰写校对批注")
        cur = self.db.execute(
            """INSERT INTO annotation(version_id, author_id, quote, comment)
               VALUES (?,?,?,?)""", (version_id, staff_id, quote, comment))
        self.db.commit()
        return cur.lastrowid

    def _valid_review(self, version_id: int, kind: str) -> sqlite3.Row | None:
        """有效结论：通过且未被后续版本取代。"""
        return self.db.execute(
            """SELECT r.* FROM review r JOIN label_version v ON v.id=r.version_id
               WHERE r.version_id=? AND r.kind=? AND r.passed=1
                 AND r.status='valid'
               ORDER BY r.id DESC LIMIT 1""",
            (version_id, kind)).fetchone()

    def proofread(self, version_id: int, editor_id: int, passed: bool,
                  note: str = "") -> int:
        user = self.staff(editor_id)
        if user["role"] != "editor":
            raise DomainError("用语校对只能由编辑完成")
        return self._record_review(version_id, "proofread", editor_id, passed, note)

    def factcheck(self, version_id: int, keeper_id: int, passed: bool,
                  note: str = "", catalog_no_observed: str | None = None) -> int:
        """保管员事实核对。核对展品编号；策展人不能批准自己提交的版本。"""
        user = self.staff(keeper_id)
        if user["role"] != "keeper":
            raise DomainError("事实核对只能由保管员完成")
        v = self.get_version(version_id)
        if v["created_by"] == keeper_id:
            raise DomainError("策展人不能批准自己提交版本的事实核对（角色分离）")
        if catalog_no_observed is not None:
            ex = self.db.execute(
                "SELECT * FROM exhibit WHERE id=?", (v["exhibit_id"],)).fetchone()
            if ex["catalog_no"] != catalog_no_observed:
                raise DomainError(
                    f"展品编号不符：扫码/标签 {catalog_no_observed}，"
                    f"台账 {ex['catalog_no']}")
        return self._record_review(version_id, "factcheck", keeper_id, passed, note)

    def _record_review(self, version_id: int, kind: str, reviewer_id: int,
                       passed: bool, note: str) -> int:
        v = self.get_version(version_id)
        cur = self.db.execute(
            """INSERT INTO review(version_id, kind, passed, reviewer_id,
                                  note, reviewed_hash)
               VALUES (?,?,?,?,?,?)""",
            (version_id, kind, 1 if passed else 0, reviewer_id, note,
             v["content_hash"]))
        self.db.commit()
        return cur.lastrowid

    def review_state(self, version_id: int) -> dict:
        """版本当前的校对状态（过期版本一律视为不可安装）。"""
        v = self.get_version(version_id)
        pr = self._valid_review(version_id, "proofread")
        fc = self._valid_review(version_id, "factcheck")
        stale = [r for r in self.db.execute(
            "SELECT * FROM review WHERE version_id=? AND status='superseded'",
            (version_id,)).fetchall()]
        return {
            "version": v,
            "is_latest": self.is_latest(version_id),
            "proofread": pr,
            "factcheck": fc,
            "stale_reviews": stale,
            "ready": bool(pr and fc and self.is_latest(version_id)),
        }

    # ---------- 问题记录 ----------
    def report_issue(self, kind: str, detail: str, reporter_id: int,
                     exhibit_id: int | None = None,
                     case_id: int | None = None,
                     version_id: int | None = None) -> int:
        cur = self.db.execute(
            """INSERT INTO issue(kind, detail, reported_by, exhibit_id,
                                 case_id, version_id)
               VALUES (?,?,?,?,?,?)""",
            (kind, detail, reporter_id, exhibit_id, case_id, version_id))
        self.db.commit()
        return cur.lastrowid

    def resolve_issue(self, issue_id: int) -> None:
        self.db.execute("UPDATE issue SET status='resolved' WHERE id=?",
                        (issue_id,))
        self.db.commit()

    # ---------- 打印 ----------
    def expected_case(self, exhibit_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM case_table WHERE exhibit_id=?",
            (exhibit_id,)).fetchone()

    def print_label(self, version_id: int, staff_id: int) -> dict:
        """校对双签通过且为最新版才可打印；生成二维码与真实比例 SVG 样图。"""
        user = self.staff(staff_id)
        if user["role"] not in ("editor", "keeper", "curator"):
            raise DomainError("现场人员无打印清单权限")
        st = self.review_state(version_id)
        if not st["is_latest"]:
            raise DomainError("该版本已被新版取代，不能打印过期版本")
        if not st["proofread"]:
            raise DomainError("编辑校对未通过，不能打印")
        if not st["factcheck"]:
            raise DomainError("保管员事实核对未通过，不能打印")

        v = st["version"]
        ex = self.db.execute(
            "SELECT * FROM exhibit WHERE id=?", (v["exhibit_id"],)).fetchone()
        case = self.expected_case(v["exhibit_id"])
        payload = self.qr.encode(
            version_id=version_id, catalog_no=ex["catalog_no"],
            case_code=case["code"] if case else None, url=v["qr_url"])
        delta = self.printer.measure_color_delta(version_id)
        path = self.printer.render_proof(
            version_id=version_id, title=v["title"], dynasty=v["dynasty"],
            material=v["material"], body=v["body"], catalog_no=ex["catalog_no"],
            qr_svg=self.qr.render_matrix(payload))
        cur = self.db.execute(
            """INSERT INTO print_job(version_id, proof_path, qr_payload,
                                     color_delta, printed_by)
               VALUES (?,?,?,?,?)""",
            (version_id, str(path), payload, delta, staff_id))
        self.db.commit()
        job_id = cur.lastrowid
        if delta > COLOR_TOLERANCE:
            # 色差超差的印件立即作废，禁止流入现场；重印须另起修订版重新双签
            self.db.execute("UPDATE print_job SET status='void' WHERE id=?",
                            (job_id,))
            self.report_issue(
                "print_color_delta",
                f"版本 v{v['version_no']} 印刷色差 ΔE={delta:.1f}，超过容差 "
                f"{COLOR_TOLERANCE}，需重印",
                reporter_id=staff_id, exhibit_id=v["exhibit_id"],
                version_id=version_id)
        return {"job_id": job_id, "payload": payload, "proof_path": str(path),
                "color_delta": delta,
                "quarantined": delta > COLOR_TOLERANCE}

    # ---------- 扫码安装 ----------
    def scan_install(self, qr_payload: str, scanned_case_code: str,
                     scanner_id: int) -> ScanResult:
        """现场扫码：校验版本有效性与目标展柜；通过则换版并写撤换记录。"""
        user = self.staff(scanner_id)
        if user["role"] != "installer":
            return ScanResult(False, "只有现场更换人员可以扫码安装")

        case = self.db.execute(
            "SELECT * FROM case_table WHERE code=?",
            (scanned_case_code,)).fetchone()
        if not case:
            return ScanResult(False, f"展柜 {scanned_case_code} 不存在")

        # 临时撤展未销案前，该展柜禁止安装任何展签
        if self.db.execute(
                "SELECT 1 FROM issue WHERE case_id=? AND kind='temporary_removal'"
                " AND status='open' LIMIT 1", (case["id"],)).fetchone():
            self._log_scan(qr_payload, case["id"], scanner_id, None, False,
                           f"展柜 {case['code']} 临时撤展中，禁止安装")
            return ScanResult(False, f"展柜 {case['code']} 临时撤展中，禁止安装")

        info = self.qr.decode(qr_payload)
        if info is None:
            self._log_scan(qr_payload, case["id"], scanner_id, None, False,
                           "二维码无法识别")
            return ScanResult(False, "二维码无法识别")

        version_id = info["version_id"]
        v = self.db.execute(
            "SELECT * FROM label_version WHERE id=?", (version_id,)).fetchone()
        if not v:
            self._log_scan(qr_payload, case["id"], scanner_id, None, False,
                           "二维码指向的展签版本不存在")
            return ScanResult(False, "二维码指向的展签版本不存在")

        expected = self.expected_case(v["exhibit_id"])
        expected_id = expected["id"] if expected else None

        # 二维码指向错误：payload 中声明的展柜与台账不一致
        if info.get("case_code") and expected and \
                info["case_code"] != expected["code"]:
            self._log_scan(qr_payload, case["id"], scanner_id, expected_id, False,
                           f"二维码指向错误展柜 {info['case_code']}，"
                           f"应为 {expected['code']}")
            return ScanResult(False,
                              f"二维码指向错误：标签指向 {info['case_code']}，"
                              f"台账展柜为 {expected['code']}")

        # 装错柜：扫的是正确的标签，但人站在错误的展柜前
        if not expected or case["id"] != expected_id:
            reason = (f"装错柜：该展签属于 {expected['code'] if expected else '（未知展柜）'}，"
                      f"当前扫描位置为 {case['code']}")
            self._log_scan(qr_payload, case["id"], scanner_id, expected_id, False,
                           reason)
            return ScanResult(False, reason)

        # 过期版本 / 校对失效
        st = self.review_state(version_id)
        if not st["is_latest"]:
            self._log_scan(qr_payload, case["id"], scanner_id, expected_id, False,
                           "过期版本：该展签已被新版取代，禁止安装")
            return ScanResult(False, "过期版本，禁止安装")
        if not st["proofread"] or not st["factcheck"]:
            self._log_scan(qr_payload, case["id"], scanner_id, expected_id, False,
                           "校对结论已失效（正文修改后未重新双签）")
            return ScanResult(False, "校对结论已失效，禁止安装")

        job = self.db.execute(
            "SELECT * FROM print_job WHERE version_id=? "
            "ORDER BY id DESC LIMIT 1", (version_id,)).fetchone()
        binding_id = self._activate_binding(case["id"], version_id,
                                            job["id"] if job else None,
                                            scanner_id)
        self._log_scan(qr_payload, case["id"], scanner_id, expected_id, True,
                       f"安装确认：{case['code']} ↔ v{v['version_no']}")
        return ScanResult(True, "扫码复核通过，新展签已绑定展柜", binding_id)

    def _activate_binding(self, case_id: int, version_id: int,
                          print_job_id: int | None, installer_id: int) -> int:
        """换版：旧有效绑定置失效（撤换记录），再插入新有效绑定。

        同事务内先撤旧再装新，绕开『同柜唯一有效』的部分唯一索引。
        """
        old = self.db.execute(
            "SELECT * FROM case_binding WHERE case_id=? AND valid=1",
            (case_id,)).fetchone()
        if old:
            self.db.execute(
                "UPDATE case_binding SET valid=0, removed_at=datetime('now','localtime'),"
                "note=COALESCE(NULLIF(note,''),'') || ? WHERE id=?",
                (f"；被 v{self.get_version(version_id)['version_no']} 换版撤下",
                 old["id"]))
            self.db.execute(
                "UPDATE print_job SET status='void' WHERE id IN "
                "(SELECT print_job_id FROM case_binding WHERE id=? "
                " AND print_job_id IS NOT NULL)", (old["id"],))
        cur = self.db.execute(
            """INSERT INTO case_binding(case_id, version_id, print_job_id,
                                        valid, installed_at)
               VALUES (?,?,?,1,datetime('now','localtime'))""",
            (case_id, version_id, print_job_id))
        if print_job_id:
            self.db.execute(
                "UPDATE print_job SET status='installed' WHERE id=?",
                (print_job_id,))
        self.db.commit()
        return cur.lastrowid

    def _log_scan(self, payload: str, case_id: int, scanner_id: int,
                  expected_case_id: int | None, accepted: bool,
                  reason: str) -> None:
        self.db.execute(
            """INSERT INTO scan_log(qr_payload, scanned_case, scanner_id,
                                    expected_case, accepted, reason)
               VALUES (?,?,?,?,?,?)""",
            (payload, case_id, scanner_id, expected_case_id,
             1 if accepted else 0, reason))
        self.db.commit()

    # ---------- 撤展 / 撤签 ----------
    def remove_label(self, case_code: str, staff_id: int,
                     reason: str = "临时撤展") -> None:
        """临时撤展：撤下当前有效展签，记录问题与撤换痕迹。"""
        case = self.db.execute(
            "SELECT * FROM case_table WHERE code=?", (case_code,)).fetchone()
        if not case:
            raise DomainError(f"展柜 {case_code} 不存在")
        old = self.db.execute(
            "SELECT * FROM case_binding WHERE case_id=? AND valid=1",
            (case["id"],)).fetchone()
        if not old:
            raise DomainError(f"展柜 {case_code} 当前没有有效展签")
        self.db.execute(
            "UPDATE case_binding SET valid=0, removed_at=datetime('now','localtime'),"
            "note=COALESCE(NULLIF(note,''),'') || ? WHERE id=?",
            (f"；{reason}", old["id"]))
        if old["print_job_id"]:
            self.db.execute("UPDATE print_job SET status='void' WHERE id=?",
                            (old["print_job_id"],))
        v = self.get_version(old["version_id"])
        self.report_issue("temporary_removal",
                          f"{case_code} 临时撤展（{reason}），v{v['version_no']} 展签已撤下",
                          reporter_id=staff_id, exhibit_id=v["exhibit_id"],
                          case_id=case["id"], version_id=v["id"])
        self.db.commit()
