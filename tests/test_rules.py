"""核心规则测试：版本失效、展柜唯一、角色分离、扫码复核。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.adapters.printer import PrinterAdapter
from app.db import connect, init_db
from app.service import DomainError, MuseumService


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.svc = MuseumService(
            connect(self.dir / "test.db"),
            printer=PrinterAdapter(self.dir / "proofs"))
        init_db(self.svc.db)
        s = self.svc
        self.curator = s.add_staff("策展人甲", "curator")
        self.curator2 = s.add_staff("策展人乙", "curator")
        self.editor = s.add_staff("编辑甲", "editor")
        self.keeper = s.add_staff("保管员甲", "keeper")
        self.installer = s.add_staff("安装员甲", "installer")
        self.ex = s.add_exhibit("CG-0001", "测试陶罐")
        self.case = s.add_case("T-01", "测试厅", self.ex)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ready_version(self, body: str = "器形古朴。", **kw) -> int:
        """提交并双签通过、打印，返回版本 id。"""
        s = self.svc
        vid = s.submit_version(
            self.ex, title=kw.get("title", "测试陶罐"),
            dynasty=kw.get("dynasty", "新石器时代"),
            material=kw.get("material", "彩陶"),
            body=body,
            qr_url=kw.get("qr_url", "https://m.example/c/CG-0001"),
            lang=kw.get("lang", "zh"), staff_id=self.curator)
        s.proofread(vid, self.editor, True, "通过")
        s.factcheck(vid, self.keeper, True, "编号无误",
                    catalog_no_observed="CG-0001")
        s.print_label(vid, self.editor)
        return vid

    def _payload(self, version_id: int) -> str:
        return self.svc.db.execute(
            "SELECT qr_payload FROM print_job WHERE version_id=? "
            "ORDER BY id DESC LIMIT 1", (version_id,)).fetchone()[0]


class TestVersionInvalidation(ServiceTestBase):
    def test_body_change_invalidates_reviews(self):
        """正文修改 → 哈希变化 → 旧校对结论失效、新批注 stale。"""
        s = self.svc
        v1 = self._ready_version("原始说明文字。")
        s.annotate(v1, self.editor, "措辞存疑", quote="原始")
        # 修改前：双签有效
        self.assertTrue(s.review_state(v1)["ready"])

        v2 = s.submit_version(
            self.ex, title="测试陶罐", dynasty="新石器时代", material="彩陶",
            body="修改后的说明文字。",
            qr_url="https://m.example/c/CG-0001", lang="zh",
            staff_id=self.curator)

        # v1 变为过期版本；其校对结论锚定旧哈希 → 失效
        st1 = s.review_state(v1)
        self.assertFalse(st1["is_latest"])
        self.assertFalse(st1["ready"])
        self.assertEqual(len(st1["stale_reviews"]), 2)
        self.assertIsNone(st1["proofread"])
        # 旧批注被标记 stale
        self.assertEqual(
            s.db.execute("SELECT status FROM annotation WHERE version_id=?",
                         (v1,)).fetchone()["status"], "stale")
        # 新版未双签前不可打印
        with self.assertRaises(DomainError):
            s.print_label(v2, self.editor)

    def test_expired_version_cannot_be_installed(self):
        """现场不能安装过期版本：v1 已打印，出 v2 后持旧签扫码被拒。"""
        s = self.svc
        v1 = self._ready_version("第一版。")
        old_payload = self._payload(v1)
        # 出修订版 v2 并完成全流程
        self._ready_version("第二版。")
        # 现场手里仍是 v1 的旧签 → 拒绝
        r = s.scan_install(old_payload, "T-01", self.installer)
        self.assertFalse(r.accepted)
        self.assertIn("过期版本", r.reason)
        # 柜内没有装上任何展签
        self.assertIsNone(s.db.execute(
            "SELECT id FROM case_binding WHERE case_id=? AND valid=1",
            (self.case,)).fetchone())

    def test_non_text_field_change_also_invalidates(self):
        """二维码链接修改同样使结论失效。"""
        s = self.svc
        v1 = self._ready_version(qr_url="https://m.example/a")
        self.assertTrue(s.review_state(v1)["ready"])
        v2 = s.submit_version(
            self.ex, title="测试陶罐", dynasty="新石器时代", material="彩陶",
            body="器形古朴。", qr_url="https://m.example/b", lang="zh",
            staff_id=self.curator)
        self.assertFalse(s.review_state(v1)["is_latest"])
        self.assertFalse(s.review_state(v2)["ready"])

    def test_parallel_language_versions(self):
        """英文版的出现不使在展中文版过期。"""
        s = self.svc
        zh = self._ready_version(body="中文说明。", lang="zh")
        en = self._ready_version(body="English text.", lang="en")
        self.assertTrue(s.review_state(zh)["is_latest"])
        self.assertTrue(s.review_state(en)["is_latest"])


class TestCaseUniqueness(ServiceTestBase):
    def test_only_one_active_binding_per_case(self):
        """同一展柜同时只允许一个有效展签（换版后旧签自动失效）。"""
        s = self.svc
        v1 = self._ready_version("第一版。")
        s.scan_install(self._payload(v1), "T-01", self.installer)
        v2 = self._ready_version("第二版。")
        s.scan_install(self._payload(v2), "T-01", self.installer)

        active = s.db.execute(
            "SELECT * FROM case_binding WHERE case_id=? AND valid=1",
            (self.case,)).fetchall()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["version_id"], v2)
        # 旧绑定保留为撤换记录
        old = s.db.execute(
            "SELECT * FROM case_binding WHERE case_id=? AND valid=0",
            (self.case,)).fetchall()
        self.assertEqual(len(old), 1)
        self.assertIsNotNone(old[0]["removed_at"])

    def test_db_partial_unique_index(self):
        """DB 层部分唯一索引兜底：直接插两条有效绑定必失败。"""
        s = self.svc
        v1 = self._ready_version("第一版。")
        s.db.execute(
            "INSERT INTO case_binding(case_id, version_id, valid) VALUES (?,?,1)",
            (self.case, v1))
        with self.assertRaises(sqlite3.IntegrityError):
            s.db.execute(
                "INSERT INTO case_binding(case_id, version_id, valid) VALUES (?,?,1)",
                (self.case, v1))

    def test_temporary_removal_leaves_no_active(self):
        """临时撤展后柜内无有效展签且有问题记录。"""
        s = self.svc
        v1 = self._ready_version()
        s.scan_install(self._payload(v1), "T-01", self.installer)
        s.remove_label("T-01", self.installer, "外借")
        self.assertIsNone(s.db.execute(
            "SELECT id FROM case_binding WHERE case_id=? AND valid=1",
            (self.case,)).fetchone())
        self.assertTrue(s.db.execute(
            "SELECT 1 FROM issue WHERE kind='temporary_removal' "
            "AND case_id=?", (self.case,)).fetchone())


class TestRoleSeparation(ServiceTestBase):
    def test_curator_cannot_factcheck_own_version(self):
        """策展人不能批准自己提交版本的事实核对。"""
        s = self.svc
        vid = s.submit_version(
            self.ex, title="测试陶罐", dynasty="新石器时代", material="彩陶",
            body="说明。", qr_url="https://m.example/c", lang="zh",
            staff_id=self.curator)
        with self.assertRaises(DomainError):
            s.factcheck(vid, self.curator, True)

    def test_other_curator_still_rejected_by_role(self):
        """即便不是提交者，策展人角色也不能做事实核对。"""
        s = self.svc
        vid = s.submit_version(
            self.ex, title="测试陶罐", dynasty="新石器时代", material="彩陶",
            body="说明。", qr_url="https://m.example/c", lang="zh",
            staff_id=self.curator)
        with self.assertRaises(DomainError):
            s.factcheck(vid, self.curator2, True)

    def test_only_editor_proofreads(self):
        s = self.svc
        vid = s.submit_version(
            self.ex, title="测试陶罐", dynasty="新石器时代", material="彩陶",
            body="说明。", qr_url="https://m.example/c", lang="zh",
            staff_id=self.curator)
        with self.assertRaises(DomainError):
            s.proofread(vid, self.keeper, True)
        with self.assertRaises(DomainError):
            s.annotate(vid, self.curator, "编辑批注")

    def test_only_curator_submits(self):
        with self.assertRaises(DomainError):
            self.svc.submit_version(
                self.ex, title="x", dynasty="x", material="x", body="x",
                qr_url="https://m.example", lang="zh", staff_id=self.editor)

    def test_only_installer_scans(self):
        v1 = self._ready_version()
        r = self.svc.scan_install(self._payload(v1), "T-01", self.curator)
        self.assertFalse(r.accepted)

    def test_catalog_number_mismatch_rejected(self):
        """保管员核对展品编号不符 → 拒绝事实核对。"""
        s = self.svc
        vid = s.submit_version(
            self.ex, title="测试陶罐", dynasty="新石器时代", material="彩陶",
            body="说明。", qr_url="https://m.example/c", lang="zh",
            staff_id=self.curator)
        with self.assertRaises(DomainError):
            s.factcheck(vid, self.keeper, True,
                        catalog_no_observed="CG-9999")

    def test_print_requires_both_reviews(self):
        s = self.svc
        vid = s.submit_version(
            self.ex, title="测试陶罐", dynasty="新石器时代", material="彩陶",
            body="说明。", qr_url="https://m.example/c", lang="zh",
            staff_id=self.curator)
        with self.assertRaises(DomainError):
            s.print_label(vid, self.editor)
        s.proofread(vid, self.editor, True)
        with self.assertRaises(DomainError):
            s.print_label(vid, self.editor)


class TestScanVerification(ServiceTestBase):
    def test_correct_scan_binds(self):
        s = self.svc
        v1 = self._ready_version()
        r = s.scan_install(self._payload(v1), "T-01", self.installer)
        self.assertTrue(r.accepted)
        self.assertIsNotNone(r.binding_id)

    def test_wrong_case_rejected(self):
        """装错柜：标签属于 T-01，拿到另一个展柜扫 → 拒绝且不绑定。"""
        s = self.svc
        other_case = s.add_case("T-02", "测试厅", s.add_exhibit("X-1", "别的"))
        v1 = self._ready_version()
        r = s.scan_install(self._payload(v1), "T-02", self.installer)
        self.assertFalse(r.accepted)
        self.assertIn("装错柜", r.reason)
        self.assertIsNone(s.db.execute(
            "SELECT id FROM case_binding WHERE case_id=? AND valid=1",
            (other_case,)).fetchone())
        # 正确展柜随后可扫
        self.assertTrue(
            s.scan_install(self._payload(v1), "T-01", self.installer).accepted)

    def test_qr_wrong_target_detected(self):
        """二维码声明的展柜与台账不符 → 判为二维码指向错误。"""
        s = self.svc
        v1 = self._ready_version()
        bad = s.qr.encode(version_id=v1, catalog_no="CG-0001",
                          case_code="T-09", url="https://m.example/c")
        r = s.scan_install(bad, "T-01", self.installer)
        self.assertFalse(r.accepted)
        self.assertIn("二维码指向错误", r.reason)

    def test_unrecognized_qr_rejected(self):
        r = self.svc.scan_install("https://evil.example/x", "T-01",
                                  self.installer)
        self.assertFalse(r.accepted)
        self.assertIn("无法识别", r.reason)

    def test_rejected_scan_kept_in_log(self):
        """无论通过与否，扫码都留痕。"""
        s = self.svc
        v1 = self._ready_version()
        s.scan_install("https://evil.example/x", "T-01", self.installer)
        row = s.db.execute(
            "SELECT * FROM scan_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["accepted"], 0)
        self.assertIn("无法识别", row["reason"])

    def test_stale_review_blocks_install(self):
        """双签后正文又被修改，未重新双签即打印/安装 → 拒绝。"""
        s = self.svc
        v1 = self._ready_version("第一版。")
        v2 = s.submit_version(
            self.ex, title="测试陶罐", dynasty="新石器时代", material="彩陶",
            body="第二版。", qr_url="https://m.example/c/CG-0001",
            lang="zh", staff_id=self.curator)
        # 只过编辑关，缺保管员事实核对
        s.proofread(v2, self.editor, True)
        # 伪造一张打印件尝试安装（绕过打印环节直接扫其 payload）
        payload = s.qr.encode(version_id=v2, catalog_no="CG-0001",
                              case_code="T-01", url="https://m.example/c")
        r = s.scan_install(payload, "T-01", self.installer)
        self.assertFalse(r.accepted)
        self.assertIn("校对结论已失效", r.reason)


    def test_install_blocked_during_temporary_removal(self):
        """临时撤展销案前，同柜任何扫码安装都被拒绝。"""
        s = self.svc
        v1 = self._ready_version()
        s.scan_install(self._payload(v1), "T-01", self.installer)
        s.remove_label("T-01", self.installer, "外借")
        r = s.scan_install(self._payload(v1), "T-01", self.installer)
        self.assertFalse(r.accepted)
        self.assertIn("临时撤展", r.reason)
        # 销案后恢复安装
        iid = s.db.execute(
            "SELECT id FROM issue WHERE kind='temporary_removal'").fetchone()[0]
        s.resolve_issue(iid)
        r2 = s.scan_install(self._payload(v1), "T-01", self.installer)
        self.assertTrue(r2.accepted)


class TestColorQuarantine(ServiceTestBase):
    def test_over_delta_print_is_voided(self):
        """色差超差印件作废且登记问题，不得出现在待安装清单。"""
        import app.service as mod
        s = self.svc
        orig = PrinterAdapter.measure_color_delta
        PrinterAdapter.measure_color_delta = lambda self, vid: 3.8
        try:
            vid = self._ready_version()
            r = s.print_label(vid, self.editor)
        finally:
            PrinterAdapter.measure_color_delta = orig
        self.assertTrue(r["quarantined"])
        job = s.db.execute(
            "SELECT * FROM print_job WHERE version_id=?", (vid,)).fetchone()
        self.assertEqual(job["status"], "void")
        self.assertTrue(s.db.execute(
            "SELECT 1 FROM issue WHERE kind='print_color_delta' "
            "AND version_id=?", (vid,)).fetchone())


class TestAdapters(unittest.TestCase):
    def test_qr_roundtrip(self):
        from app.adapters.qr import QRAdapter
        qr = QRAdapter()
        p = qr.encode(version_id=7, catalog_no="CG-1", case_code="A-01",
                      url="https://m.example")
        self.assertEqual(qr.decode(p)["case_code"], "A-01")
        self.assertIn("<svg", qr.render_matrix(p))

    def test_proof_file_has_real_label_ratio(self):
        from app.adapters.qr import QRAdapter
        with tempfile.TemporaryDirectory() as d:
            pr = PrinterAdapter(d)
            p = pr.render_proof(
                version_id=1, title="罐", dynasty="明", material="瓷",
                body="说明", catalog_no="CG-1",
                qr_svg=QRAdapter().render_matrix("museum://label?vid=1"))
            text = Path(p).read_text(encoding="utf-8")
            self.assertIn('width="120mm"', text)
            self.assertIn('height="80mm"', text)


if __name__ == "__main__":
    unittest.main()
