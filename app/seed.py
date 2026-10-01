"""初始化演示数据：一条命令重置并填充。

场景
----
- C-101 青瓷莲瓣纹碗：v1 已安装（正常）；v2 已校对+核对+打印，待换；现场有人误扫 C-102（装错柜案例）。
- C-102 铜鎏金腰带扣：v1 已校对通过、保管员核对后，策展人修改了正文 → 校对结论失效（演示过期版本不可装）。
- C-103 石刻佛首：v1 草稿，曾用错误二维码，已登记"二维码指向错误"与"多语言版本缺失"事件。
- C-104 临时撤展空位：旧展签已撤，登记临时撤展。
- 另有"印刷色差""文字事实更正""旧展签未撤"事件覆盖全部异常类型。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import services as svc
from .adapters import PrinterAdapter
from .db import connect, init_db

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "labels.db"
SAMPLE_DIR = DATA_DIR / "samples"


def seed() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    if SAMPLE_DIR.exists():
        shutil.rmtree(SAMPLE_DIR)
    conn = connect(DB_PATH)
    init_db(conn)

    # 用户：策展人 / 编辑 / 保管员（角色分离）
    users = {}
    for uname, dname, role in [
        ("li", "李策展", "curator"),
        ("zhang", "张策展", "curator"),
        ("wen", "文编辑", "editor"),
        ("gu", "古保管", "keeper"),
    ]:
        cur = conn.execute("INSERT INTO users (username, display_name, role) VALUES (?,?,?)",
                           (uname, dname, role))
        users[uname] = cur.lastrowid
    li, zhang, wen, gu = users["li"], users["zhang"], users["wen"], users["gu"]

    # 展柜与展品
    cases = [
        ("C-101", "青瓷厅", "青瓷独立柜"),
        ("C-102", "金器厅", "腰带饰件通柜"),
        ("C-103", "石刻厅", "佛首壁柜"),
        ("C-104", "书画厅", "临时撤展空柜"),
    ]
    for cid, hall, name in cases:
        conn.execute("INSERT INTO cases (id, hall, name) VALUES (?,?,?)", (cid, hall, name))
    exhibits = [
        ("EX-0001", "青瓷莲瓣纹碗", "C-101"),
        ("EX-0002", "铜鎏金腰带扣", "C-102"),
        ("EX-0003", "石刻佛首", "C-103"),
        ("EX-0004", "纸本花鸟册页", "C-104"),
    ]
    for eid, title, cid in exhibits:
        conn.execute("INSERT INTO exhibits (id, title, case_id) VALUES (?,?,?)", (eid, title, cid))
    conn.commit()

    printer = PrinterAdapter(SAMPLE_DIR)

    # ---------- EX-0001：v1 正常在校；v2 已打印待换 + 装错柜案例 ----------
    v1 = svc.create_version(conn, actor_id=li, exhibit_id="EX-0001",
        title="青瓷莲瓣纹碗", era="北宋", material="瓷（青釉）",
        body="碗作敛口弧腹，外壁刻仰莲瓣纹，釉色温润如玉，为越窑系青瓷代表器。",
        qr_url="https://museum.example/label/EX-0001?c=C-101&v=1", note="初版")
    svc.add_proofread(conn, actor_id=wen, version_id=v1["id"],
                      comment="用语规范：年代统一作「北宋」；说明句末补句号。")
    svc.fact_check(conn, actor_id=gu, version_id=v1["id"], exhibit_id_ok=True,
                   note="EX-0001 与 C-101 绑定一致")
    path = printer.print(svc.get_version(conn, v1["id"]) | {})
    svc.mark_printed(conn, actor_id=gu, version_id=v1["id"], file_path=path)
    scan = svc.scan_label(conn, label_version_id=v1["id"], scanned_case_id="C-101", operator="王现场")
    svc.install_label(conn, actor_name="王现场", label_version_id=v1["id"], scan_id=scan["id"])

    # v2：策展人更新说明（事实补充）→ 重新校对、重新核对（古保管 ≠ 李策展）→ 打印，待现场更换
    v2 = svc.create_version(conn, actor_id=li, exhibit_id="EX-0001",
        title="青瓷莲瓣纹碗", era="北宋", material="瓷（青釉）",
        body="碗作敛口弧腹，外壁刻双层仰莲瓣纹，近足处饰一道弦纹；釉色温润如玉，为越窑系青瓷代表器。",
        qr_url="https://museum.example/label/EX-0001?c=C-101&v=2",
        base_version_id=v1["id"], note="补充莲瓣层数与弦纹")
    svc.add_proofread(conn, actor_id=wen, version_id=v2["id"],
                      comment="「双层仰莲瓣纹」表述清楚，用语通过。")
    svc.fact_check(conn, actor_id=gu, version_id=v2["id"], exhibit_id_ok=True)
    path2 = printer.print(svc.get_version(conn, v2["id"]) | {})
    svc.mark_printed(conn, actor_id=gu, version_id=v2["id"], file_path=path2)

    # ★ 装错柜案例：更换人员手持 v2 新展签，误走到 C-102 扫柜码 → matched=0，系统拒绝安装
    bad = svc.scan_label(conn, label_version_id=v2["id"], scanned_case_id="C-102",
                         operator="王现场")
    assert bad["matched"] is False
    try:
        svc.install_label(conn, actor_name="王现场",
                          label_version_id=v2["id"], scan_id=bad["id"])
        raise AssertionError("装错柜竟允许安装")
    except svc.DomainError:
        pass  # 预期：被规则拦截
    svc.record_incident(conn, actor_id=gu, kind="stale_label", case_id="C-102",
                        version_id=v2["id"],
                        description="更换 C-101 新展签时误扫 C-102 柜码，现场拦截；C-102 旧展签仍在原位待核。")

    # ---------- EX-0002：编辑校对后，策展人修改草稿正文 → 校对结论失效 ----------
    w1 = svc.create_version(conn, actor_id=zhang, exhibit_id="EX-0002",
        title="铜鎏金腰带扣", era="唐", material="铜鎏金",
        body="带扣以铜板锤揲成型，表面鎏金，扣面浮雕戏珠龙纹，背面设穿带与扣舌。",
        qr_url="https://museum.example/label/EX-0002?c=C-102&v=1")
    svc.add_proofread(conn, actor_id=wen, version_id=w1["id"],
                      comment="「锤揲」用字正确，建议年代补「唐代」全称。")
    # 保管员尚未编号核对；策展人随即修改草稿正文
    # → 编辑批注 anchor_hash 失配而失效；此后必须重新校对、再由保管员核对，缺一不可打印。
    svc.update_draft_content(conn, actor_id=zhang, version_id=w1["id"],
        title="铜鎏金腰带扣", era="唐代", material="铜鎏金",
        body="带扣以铜板锤揲成型，表面鎏金，扣面浮雕摩羯戏珠纹，背面设穿带与扣舌。",
        qr_url="https://museum.example/label/EX-0002?c=C-102&v=1",
        note="龙纹更正为摩羯纹（事实更正）")
    svc.record_incident(conn, actor_id=gu, kind="fact_correction", case_id="C-102",
                        version_id=w1["id"],
                        description="原说明「戏珠龙纹」经研究更正为「摩羯戏珠纹」；先前的编辑校对结论已随正文修改失效，须重新校对、再经保管员编号核对后方可打印，现场不得安装过期版本。")

    # ---------- EX-0003：二维码错误 + 多语言缺失（草稿） ----------
    f1 = svc.create_version(conn, actor_id=li, exhibit_id="EX-0003",
        title="石刻佛首", era="北齐", material="石灰岩",
        body="佛首肉髻低平，面相丰圆，衣纹洗练，为邺城北朝造像典型风格。",
        qr_url="https://museum.example/label/WRONG-EX-0099")
    svc.record_incident(conn, actor_id=wen, kind="wrong_qr", case_id="C-103",
                        version_id=f1["id"],
                        description="二维码误指向 EX-0099 的旧页面，已退回策展人修改链接后重走校对。")
    svc.record_incident(conn, actor_id=wen, kind="missing_translation", case_id="C-103",
                        version_id=f1["id"],
                        description="缺英文 / 日文版展签，外宾开放日前需补齐多语言版本。")

    # ---------- EX-0004：已打印并安装过，因临时撤展撤下 ----------
    b1 = svc.create_version(conn, actor_id=zhang, exhibit_id="EX-0004",
        title="纸本花鸟册页", era="清", material="纸本设色",
        body="册页共八开，写四时折枝花果，用笔设色清丽，款署「南沙蒋廷锡」。",
        qr_url="https://museum.example/label/EX-0004?c=C-104&v=1")
    svc.add_proofread(conn, actor_id=wen, version_id=b1["id"], comment="文字无误，用语通过。")
    svc.fact_check(conn, actor_id=gu, version_id=b1["id"], exhibit_id_ok=True)
    path4 = printer.print(svc.get_version(conn, b1["id"]) | {})
    svc.mark_printed(conn, actor_id=gu, version_id=b1["id"], file_path=path4)
    s4 = svc.scan_label(conn, label_version_id=b1["id"], scanned_case_id="C-104", operator="赵更换")
    svc.install_label(conn, actor_name="赵更换", label_version_id=b1["id"], scan_id=s4["id"])
    svc.remove_label(conn, actor_name="赵更换", case_id="C-104",
                     reason="展品借展出库，临时撤展")
    svc.record_incident(conn, actor_id=gu, kind="temporary_removal", case_id="C-104",
                        version_id=b1["id"],
                        description="纸本花鸟册页赴外馆借展三个月，展签随撤，复展时重新校对有效期。")
    svc.record_incident(conn, actor_id=wen, kind="print_color", case_id="C-101",
                        version_id=v1["id"],
                        description="首批 v1 打样铜色条偏红（ΔE 超阈值），已调整打印机色值重印。")

    conn.close()
    print(f"演示数据就绪：{DB_PATH}（样图目录 {SAMPLE_DIR}/）")
    print("内置案例：C-101 v2 待换；装错柜（误扫 C-102）已拦截；C-102 校对失效；六类异常均已登记。")


if __name__ == "__main__":
    seed()
