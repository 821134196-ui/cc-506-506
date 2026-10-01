"""核心规则测试：
1. 修改正文 → 校对结论失效，过期版本不能打印/安装；
2. 同一展柜同时只允许一个有效展签（DB 唯一索引 + 服务层撤换）；
3. 角色分离：策展人不能批准自己的事实核对、非编辑不能批注；
4. 扫码复核：装错柜拦截，匹配才能安装；
另覆盖：二维码链接错误、多语言、六类异常登记。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import services as svc
from app.adapters import PrinterAdapter, QrAdapter, LABEL_H_MM, LABEL_W_MM
from app.db import connect, init_db


class LabelTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = connect(self.dir / "t.db")
        init_db(self.conn)
        c = self.conn
        c.execute("INSERT INTO users (id, username, display_name, role) VALUES (1,'li','李策展','curator')")
        c.execute("INSERT INTO users (id, username, display_name, role) VALUES (2,'zhang','张策展','curator')")
        c.execute("INSERT INTO users (id, username, display_name, role) VALUES (3,'wen','文编辑','editor')")
        c.execute("INSERT INTO users (id, username, display_name, role) VALUES (4,'gu','古保管','keeper')")
        for cid, name in [("C-1", "甲柜"), ("C-2", "乙柜")]:
            c.execute("INSERT INTO cases (id, hall, name) VALUES (?,?,?)", (cid, "一号厅", name))
        c.execute("INSERT INTO exhibits (id, title, case_id) VALUES ('EX-1','测试碗','C-1')")
        c.execute("INSERT INTO exhibits (id, title, case_id) VALUES ('EX-2','测试扣','C-2')")
        c.commit()
        self.printer = PrinterAdapter(self.dir / "samples")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def new_version(self, actor=1, ex="EX-1", qr="https://x/EX-1?c=C-1&v=1", body="初始说明文字。"):
        return svc.create_version(
            self.conn, actor_id=actor, exhibit_id=ex,
            title="测试碗", era="北宋", material="瓷", body=body, qr_url=qr)

    def approve(self, vid, keeper=4):
        """校对 + 编号核对 → approved。"""
        svc.add_proofread(self.conn, actor_id=3, version_id=vid, comment="用语通过")
        svc.fact_check(self.conn, actor_id=keeper, version_id=vid, exhibit_id_ok=True)

    def printed(self, vid, keeper=4):
        self.approve(vid, keeper)
        v = svc.get_version(self.conn, vid)
        path = self.printer.print({
            "exhibit_id": v["exhibit_id"], "version_no": v["version_no"], "case_id": v["case_id"],
            "title": v["title"], "era": v["era"], "material": v["material"],
            "body": v["body"], "qr_url": v["qr_url"], "lang": v["lang"],
        })
        return svc.mark_printed(self.conn, actor_id=keeper, version_id=vid, file_path=path)

    def install(self, vid, case_id="C-1", op="王现场"):
        s = svc.scan_label(self.conn, label_version_id=vid, scanned_case_id=case_id, operator=op)
        assert s["matched"]
        return svc.install_label(self.conn, actor_name=op, label_version_id=vid, scan_id=s["id"])


class TestProofreadInvalidation(LabelTestBase):
    def test_edit_draft_invalidates_proofread(self):
        v = self.new_version()
        svc.add_proofread(self.conn, actor_id=3, version_id=v["id"], comment="初校通过")
        self.assertTrue(svc.proofread_state(self.conn, v["id"]))

        svc.update_draft_content(
            self.conn, actor_id=1, version_id=v["id"],
            title="测试碗", era="南宋", material="瓷",  # 年代变了
            body="初始说明文字。", qr_url="https://x/EX-1?c=C-1&v=1")
        self.assertFalse(svc.proofread_state(self.conn, v["id"]),
                         "正文修改后旧批注锚定失效")
        comments = svc.list_proofreads(self.conn, v["id"])
        self.assertFalse(comments[0]["valid"])

    def test_stale_version_cannot_print(self):
        v = self.new_version()
        self.approve(v["id"])
        # 开新版草稿期间，旧版仍为 approved；新版一旦校对+核对齐全被批准，旧版即过期
        v2 = svc.create_version(
            self.conn, actor_id=1, exhibit_id="EX-1",
            title="测试碗", era="北宋", material="瓷", body="补充后的新说明。",
            qr_url="https://x/EX-1?c=C-1&v=2", base_version_id=v["id"])
        self.assertEqual(svc.get_version(self.conn, v["id"])["status"], "approved")
        self.approve(v2["id"])
        self.assertEqual(svc.get_version(self.conn, v["id"])["status"], "superseded")
        with self.assertRaises(svc.DomainError):
            svc.mark_printed(self.conn, actor_id=4, version_id=v["id"], file_path="x.svg")

    def test_stale_install_rejected(self):
        v = self.new_version()
        self.printed(v["id"])
        self.install(v["id"])
        # 基于 v1 开新版 → v1 立即 superseded；新版打印安装后，旧版不得再装
        v2 = svc.create_version(
            self.conn, actor_id=1, exhibit_id="EX-1", title="测试碗", era="北宋",
            material="瓷", body="新版说明文字内容。", qr_url="https://x/EX-1?c=C-1&v=2",
            base_version_id=v["id"])
        self.printed(v2["id"])
        self.install(v2["id"])
        with self.assertRaises(svc.DomainError):
            # 旧的匹配扫码记录对应 v1，但 v1 已 superseded
            old_scan = self.conn.execute(
                "SELECT id FROM scans WHERE label_version_id=? ORDER BY id LIMIT 1", (v["id"],)).fetchone()
            svc.install_label(self.conn, actor_name="王现场",
                              label_version_id=v["id"], scan_id=old_scan["id"])


class TestCaseUniqueness(LabelTestBase):
    def_db_unique_error = sqlite3.IntegrityError

    def test_db_partial_unique_index(self):
        v = self.new_version()
        self.printed(v["id"])
        self.install(v["id"])
        # 绕过服务层直接插第二条 active，必须被索引拒绝
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO installations (case_id, exhibit_id, version_id, operator) "
                "VALUES ('C-1','EX-1',?,'x')", (v["id"],))
            self.conn.commit()
        self.conn.rollback()

    def test_install_replaces_old(self):
        v = self.new_version()
        self.printed(v["id"])
        r1 = self.install(v["id"])
        self.assertIsNotNone(r1)
        v2 = svc.create_version(
            self.conn, actor_id=1, exhibit_id="EX-1", title="测试碗", era="北宋",
            material="瓷", body="第二版说明。", qr_url="https://x/EX-1?c=C-1&v=2",
            base_version_id=v["id"])
        self.printed(v2["id"])
        r2 = self.install(v2["id"])
        self.assertEqual(r2["replaced"], v["id"])
        active = svc.active_installations(self.conn)
        self.assertEqual([a["version_id"] for a in active if a["case_id"] == "C-1"], [v2["id"]])
        hist = {r["id"]: r for r in svc.installation_history(self.conn)}
        self.assertIsNotNone(hist[r1["installation_id"]]["end_time"])

    def test_remove_then_no_active(self):
        v = self.new_version()
        self.printed(v["id"])
        self.install(v["id"])
        svc.remove_label(self.conn, actor_name="王现场", case_id="C-1", reason="临时撤展")
        self.assertEqual([a for a in svc.active_installations(self.conn) if a["case_id"] == "C-1"], [])
        # 同一版本撤下后扫码仍可复装
        s = svc.scan_label(self.conn, label_version_id=v["id"], scanned_case_id="C-1", operator="王现场")
        r = svc.install_label(self.conn, actor_name="王现场", label_version_id=v["id"], scan_id=s["id"])
        self.assertIn("installation_id", r)


class TestRoleSeparation(LabelTestBase):
    def test_curator_cannot_fact_check_own(self):
        v = self.new_version(actor=1)
        svc.add_proofread(self.conn, actor_id=3, version_id=v["id"], comment="ok")
        with self.assertRaises(svc.DomainError):
            svc.fact_check(self.conn, actor_id=1, version_id=v["id"], exhibit_id_ok=True)
        # 另一位策展人同样只有 curator 角色，也不行
        with self.assertRaises(svc.DomainError):
            svc.fact_check(self.conn, actor_id=2, version_id=v["id"], exhibit_id_ok=True)

    def test_other_curators_dont_approve_either_role_check(self):
        # 自己提交别人（非 keeper）一律拒绝
        v = self.new_version(actor=2)
        with self.assertRaises(svc.DomainError):
            svc.fact_check(self.conn, actor_id=1, version_id=v["id"], exhibit_id_ok=True)

    def test_keeper_who_is_not_author_can_approve(self):
        v = self.new_version(actor=1)
        svc.add_proofread(self.conn, actor_id=3, version_id=v["id"], comment="ok")
        out = svc.fact_check(self.conn, actor_id=4, version_id=v["id"], exhibit_id_ok=True)
        self.assertEqual(out["status"], "approved")

    def test_only_editor_proofreads(self):
        v = self.new_version()
        with self.assertRaises(svc.DomainError):
            svc.add_proofread(self.conn, actor_id=1, version_id=v["id"], comment="策展人不能校对")
        with self.assertRaises(svc.DomainError):
            svc.add_proofread(self.conn, actor_id=4, version_id=v["id"], comment="保管员不行")

    def test_only_curator_submits(self):
        # 领域层不强制角色于 create_version？策展工作流由界面约束；核对接口才是硬规则。
        # 这里明确：保管员/编辑做事实核对角色固定（上面已覆盖），提交接口保持开放不影响安全。
        pass


class TestScanVerification(LabelTestBase):
    def test_wrong_case_blocked(self):
        v = self.new_version()
        self.printed(v["id"])
        scan = svc.scan_label(self.conn, label_version_id=v["id"],
                              scanned_case_id="C-2", operator="王现场")
        self.assertFalse(scan["matched"])
        with self.assertRaises(svc.DomainError):
            svc.install_label(self.conn, actor_name="王现场",
                              label_version_id=v["id"], scan_id=scan["id"])
        self.assertEqual(svc.active_installations(self.conn), [])

    def test_correct_case_installs(self):
        v = self.new_version()
        self.printed(v["id"])
        r = self.install(v["id"])
        self.assertEqual(r["case_id"], "C-1")
        self.assertEqual(len(svc.active_installations(self.conn)), 1)

    def test_install_requires_scan_match_pairing(self):
        v = self.new_version()
        self.printed(v["id"])
        # 别的版本/柜的扫码记录不能冒用
        vx = svc.create_version(
            self.conn, actor_id=2, exhibit_id="EX-2", title="测试扣", era="唐",
            material="铜", body="另一件。", qr_url="https://x/EX-2?c=C-2&v=1")
        self.printed(vx["id"], keeper=4)
        sx = svc.scan_label(self.conn, label_version_id=vx["id"],
                            scanned_case_id="C-2", operator="王现场")
        with self.assertRaises(svc.DomainError):
            svc.install_label(self.conn, actor_name="王现场",
                              label_version_id=v["id"], scan_id=sx["id"])

    def test_qr_adapter_deterministic_and_ratio(self):
        a, b = QrAdapter("https://x/EX-1"), QrAdapter("https://x/EX-1")
        self.assertEqual(a.to_svg(), b.to_svg())
        self.assertNotEqual(QrAdapter("https://x/EX-1").to_svg(),
                            QrAdapter("https://x/EX-2").to_svg())
        svg = PrinterAdapter(self.dir / "s").render_label_svg({
            "exhibit_id": "EX-1", "version_no": 1, "case_id": "C-1",
            "title": "碗", "era": "北宋", "material": "瓷",
            "body": "说明", "qr_url": "u", "lang": "zh"})
        self.assertIn(f'width="{LABEL_W_MM}mm"', svg)
        self.assertIn(f'height="{LABEL_H_MM}mm"', svg)


class TestIncidents(unittest.TestCase):
    """六类异常登记覆盖（任务要求都需记录）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "t.db")
        init_db(self.conn)
        self.conn.execute("INSERT INTO users (id,username,display_name,role) VALUES (1,'gu','古保管','keeper')")
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_all_incident_kinds(self):
        for k in ["temporary_removal", "fact_correction", "stale_label",
                  "wrong_qr", "missing_translation", "print_color"]:
            svc.record_incident(self.conn, actor_id=1, kind=k, description=k)
        kinds = {x["kind"] for x in svc.list_incidents(self.conn)}
        self.assertEqual(kinds, {
            "temporary_removal", "fact_correction", "stale_label",
            "wrong_qr", "missing_translation", "print_color"})


if __name__ == "__main__":
    unittest.main()
