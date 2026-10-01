"""种子数据：预置完整工作流、六类问题记录与一个『装错柜』被拒案例。

用法：python -m app.seed [db路径]
"""
from __future__ import annotations

import sys
from pathlib import Path

from .db import connect, init_db
from .service import MuseumService

DEFAULT_DB = "data/museum.db"


def seed(db_path: str = DEFAULT_DB) -> MuseumService:
    path = Path(db_path)
    if path.exists():
        path.unlink()
    proofs = Path("data/label_proofs")
    if proofs.exists():
        for f in proofs.glob("*.svg"):
            f.unlink()

    svc = MuseumService(connect(path))
    init_db(svc.db)

    # ---- 人员（四种角色）----
    curator = svc.add_staff("陈策", "curator")       # 策展人
    editor = svc.add_staff("林编", "editor")          # 编辑
    keeper = svc.add_staff("王管", "keeper")          # 保管员
    installer = svc.add_staff("赵装", "installer")    # 现场更换

    # ---- 展品目录 ----
    e1 = svc.add_exhibit("CG-2019-0142", "青花缠枝莲纹罐")
    e2 = svc.add_exhibit("FT-2015-0087", "鎏金铜佛坐像")
    e3 = svc.add_exhibit("SC-2008-0301", "三彩骆驼")
    e4 = svc.add_exhibit("YB-2011-0199", "白玉龙纹璧")

    # ---- 展柜 ----
    c_a1 = svc.add_case("A-01", "陶瓷厅", e1)
    c_a2 = svc.add_case("A-02", "佛造像厅", e2)
    c_b1 = svc.add_case("B-01", "丝路厅", e3)
    c_b2 = svc.add_case("B-02", "玉器厅", e4)

    # ---- 首批展签 v1~v4：提交 → 批注/校对 → 打印 → 扫码入柜 ----
    v1 = svc.submit_version(
        e1, title="青花缠枝莲纹罐", dynasty="明宣德", material="景德镇窑青花瓷",
        body="直口丰肩，器身绘缠枝莲纹，发色浓艳。\n宣德年间官窑典型器。",
        qr_url="https://museum.example/c/CG-2019-0142", lang="zh",
        staff_id=curator)
    v2 = svc.submit_version(
        e2, title="鎏金铜佛坐像", dynasty="唐", material="铜鎏金",
        body="螺发高髻，结跏趺坐，衣纹流畅。\n表面鎏金多有脱落。",
        qr_url="https://museum.example.org/old-event/signup",   # 指向错误（旧活动页）
        lang="zh", staff_id=curator)
    v3 = svc.submit_version(
        e3, title="三彩骆驼", dynasty="唐", material="陶质三彩釉",
        body="昂首嘶鸣，双峰高耸。\n釉色以黄、绿、白为主。",
        qr_url="https://museum.example/c/SC-2008-0301", lang="zh",
        staff_id=curator)
    v4 = svc.submit_version(
        e4, title="白玉龙纹璧", dynasty="西汉", material="和田白玉",
        body="扁平圆璧，内外缘各有弦纹一周。\n璧面浮雕谷纹与龙纹。",
        qr_url="https://museum.example/c/YB-2011-0199", lang="zh",
        staff_id=curator)

    svc.annotate(v1, editor, "『宣德年间』纪年建议再核《藏品总目》",
                 quote="宣德年间官窑典型器")

    for vid in (v1, v2, v3, v4):
        svc.proofread(vid, editor, True, "用语规范，通过")
        ex_id = svc.get_version(vid)["exhibit_id"]
        cat = {e1: "CG-2019-0142", e2: "FT-2015-0087",
               e3: "SC-2008-0301", e4: "YB-2011-0199"}[ex_id]
        svc.factcheck(vid, keeper, True, "展品编号核对无误",
                      catalog_no_observed=cat)
        svc.print_label(vid, editor)

    # 首版正确扫码入柜
    payload1 = svc.db.execute(
        "SELECT qr_payload FROM print_job WHERE version_id=?", (v1,)).fetchone()[0]
    svc.scan_install(payload1, "A-01", installer)
    payload2 = svc.db.execute(
        "SELECT qr_payload FROM print_job WHERE version_id=?", (v2,)).fetchone()[0]
    svc.scan_install(payload2, "A-02", installer)
    payload3 = svc.db.execute(
        "SELECT qr_payload FROM print_job WHERE version_id=?", (v3,)).fetchone()[0]
    svc.scan_install(payload3, "B-01", installer)
    payload4 = svc.db.execute(
        "SELECT qr_payload FROM print_job WHERE version_id=?", (v4,)).fetchone()[0]
    svc.scan_install(payload4, "B-02", installer)

    # ============ 六类问题 + 装错柜案例 ============

    # ① 文字事实更正：保管员发现 v1 纪年有误（宣德→永乐）
    iss_fact = svc.report_issue(
        "fact_correction",
        "v1 纪年误作『明宣德』，核《藏品总目》当为『明永乐』，须更正正文",
        reporter_id=keeper, exhibit_id=e1, case_id=c_a1, version_id=v1)
    v5 = svc.submit_version(
        e1, title="青花缠枝莲纹罐", dynasty="明永乐", material="景德镇窑青花瓷",
        body="直口丰肩，器身绘缠枝莲纹，发色浓艳。\n永乐年间官窑典型器。",
        qr_url="https://museum.example/c/CG-2019-0142", lang="zh",
        staff_id=curator)
    # v1 的批注随正文修改自动转为 stale；v5 须重新双签
    svc.proofread(v5, editor, True, "正文更正后重新校对，通过")
    svc.factcheck(v5, keeper, True, "编号无误，纪年已改永乐",
                  catalog_no_observed="CG-2019-0142")
    svc.print_label(v5, editor)
    svc.resolve_issue(iss_fact)

    # ② 旧展签未撤：v5 已印出待换，A-01 柜面仍是 v1
    iss_old = svc.report_issue(
        "old_label_remains",
        "A-01 新版 v5 已在打印清单中，柜面旧签 v1（宣德）尚未撤下",
        reporter_id=installer, exhibit_id=e1, case_id=c_a1, version_id=v5)
    payload5 = svc.db.execute(
        "SELECT qr_payload FROM print_job WHERE version_id=? ORDER BY id DESC",
        (v5,)).fetchone()[0]
    svc.scan_install(payload5, "A-01", installer)   # 换版：v1 自动撤下
    svc.resolve_issue(iss_old)

    # ③ 二维码指向错误：v2 链到旧活动报名页
    iss_qr = svc.report_issue(
        "qr_wrong_target",
        "v2 二维码指向旧活动报名页 .../old-event/signup，应为展品详情页",
        reporter_id=keeper, exhibit_id=e2, case_id=c_a2, version_id=v2)
    v6 = svc.submit_version(
        e2, title="鎏金铜佛坐像", dynasty="唐", material="铜鎏金",
        body="螺发高髻，结跏趺坐，衣纹流畅。\n表面鎏金多有脱落。",
        qr_url="https://museum.example/c/FT-2015-0087", lang="zh",
        staff_id=curator)
    svc.proofread(v6, editor, True, "仅改链接，用语无误")
    svc.factcheck(v6, keeper, True, "编号核对无误",
                  catalog_no_observed="FT-2015-0087")
    svc.print_label(v6, editor)
    payload6 = svc.db.execute(
        "SELECT qr_payload FROM print_job WHERE version_id=? ORDER BY id DESC",
        (v6,)).fetchone()[0]

    # ★ 装错柜案例：本该去 A-02，现场误在 A-01 扫码 → 拒绝
    r_wrong = svc.scan_install(payload6, "A-01", installer)
    assert not r_wrong.accepted, "装错柜必须被拒绝"
    # 复核后到正确展柜扫码 → 通过
    svc.scan_install(payload6, "A-02", installer)
    svc.resolve_issue(iss_qr)

    # ④ 多语言版本缺失：白玉璧只有中文签
    iss_lang = svc.report_issue(
        "missing_translation",
        "B-02 白玉龙纹璧缺少英文展签，外宾团体到访前须补齐",
        reporter_id=curator, exhibit_id=e4, case_id=c_b2, version_id=v4)
    v7 = svc.submit_version(
        e4, title="White Jade Bi with Dragon Pattern",
        dynasty="Western Han", material="Hetian white jade",
        body="A flat circular jade disc with bow-string rims.\nRaised grain and dragon motifs on the face.",
        qr_url="https://museum.example/c/YB-2011-0199?lang=en", lang="en",
        staff_id=curator)
    svc.proofread(v7, editor, True, "英文译稿校订通过")
    svc.factcheck(v7, keeper, True, "编号无误",
                  catalog_no_observed="YB-2011-0199")
    svc.print_label(v7, editor)   # 英文版补印，随中文签同卡套，不另占绑定
    svc.resolve_issue(iss_lang)

    # ⑤ 印刷色差：v8 首印 ΔE 超差（适配器确定性模拟），重印 v9
    v8 = svc.submit_version(
        e3, title="三彩骆驼", dynasty="唐", material="陶质三彩釉",
        body="昂首嘶鸣，双峰高耸，背负丝绸。\n釉色以黄、绿、白为主。",
        qr_url="https://museum.example/c/SC-2008-0301", lang="zh",
        staff_id=curator)
    svc.proofread(v8, editor, True, "补『背负丝绸』一句，通过")
    svc.factcheck(v8, keeper, True, "编号无误",
                  catalog_no_observed="SC-2008-0301")
    p8 = svc.print_label(v8, editor)
    assert p8["color_delta"] > 3.0, "种子预期 v8 色差超差"
    iss_color = svc.db.execute(
        "SELECT id FROM issue WHERE kind='print_color_delta' "
        "ORDER BY id DESC LIMIT 1").fetchone()[0]
    # 重印：另起修订版 v9（同内容微调措辞），色差合格 → 待换版次挂在 B-01
    v9 = svc.submit_version(
        e3, title="三彩骆驼", dynasty="唐", material="陶质三彩釉",
        body="昂首嘶鸣，双峰高耸，背负丝绢。\n釉色以黄、绿、白三色为主。",
        qr_url="https://museum.example/c/SC-2008-0301", lang="zh",
        staff_id=curator)
    svc.proofread(v9, editor, True, "重印版校对通过")
    svc.factcheck(v9, keeper, True, "编号无误",
                  catalog_no_observed="SC-2008-0301")
    svc.print_label(v9, editor)   # 注意：v9 不安装 → B-01 显示当前 v3 / 待换 v9
    svc.resolve_issue(iss_color)

    # ⑥ 展品临时撤展：白玉璧赴外展，撤下 B-02 当前签
    svc.remove_label("B-02", installer, reason="文物赴外展借展 3 个月")

    return svc


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    s = seed(db)
    print(f"种子数据已写入 {db}")
    print("展柜：", [r["code"] for r in s.db.execute(
        "SELECT code FROM case_table ORDER BY code")])
    print("扫码记录：")
    for r in s.db.execute(
            "SELECT scanned_case, (SELECT code FROM case_table WHERE id=scanned_case) AS code,"
            "accepted, reason FROM scan_log ORDER BY id"):
        print(f"  [{'通过' if r['accepted'] else '拒绝'}] {r['reason']}")
