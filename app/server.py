"""本地 Web 服务（标准库实现）。

路由：
  GET  /                      单页界面
  GET  /static/*              样式与脚本
  GET  /api/state             全量数据（目录/版本/校对/打印/绑定/扫码/问题）
  POST /api/submit            策展人提交新版本
  POST /api/annotate          编辑批注
  POST /api/review            编辑校对 / 保管员事实核对
  POST /api/print             生成可打印版本（二维码 + 120×80mm SVG 样图）
  POST /api/scan              现场扫码复核
  POST /api/remove            临时撤展
  POST /api/issue             登记问题
  GET  /api/preview?vid=      按真实标签比例渲染预览 SVG
  GET  /proof/<filename>      读取已生成的本地展签样图
"""
from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .adapters.printer import LABEL_H_MM, LABEL_W_MM
from .db import connect, init_db
from .service import DomainError, MuseumService

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web" / "static"

ISSUE_LABELS = {
    "temporary_removal": "临时撤展",
    "fact_correction": "文字事实更正",
    "old_label_remains": "旧展签未撤",
    "qr_wrong_target": "二维码指向错误",
    "missing_translation": "多语言版本缺失",
    "print_color_delta": "印刷色差",
}
ROLE_LABELS = {
    "curator": "策展人", "editor": "编辑",
    "keeper": "保管员", "installer": "现场更换",
}


class AppHandler(BaseHTTPRequestHandler):
    svc: MuseumService = None  # 由 main 注入

    # ---------- 基础工具 ----------
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str | None = None) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         ctype or mimetypes.guess_type(str(path))[0]
                         or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def log_message(self, fmt, *args) -> None:  # 静音
        pass

    # ---------- GET ----------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route, qs = parsed.path, parse_qs(parsed.query)
        try:
            if route == "/":
                self._send_file(STATIC / "index.html", "text/html; charset=utf-8")
            elif route.startswith("/static/"):
                self._send_file(STATIC / route[len("/static/"):])
            elif route == "/api/state":
                self._send_json(self._state())
            elif route == "/api/preview":
                self._preview(int(qs["vid"][0]))
            elif route.startswith("/proof/"):
                name = Path(route[len("/proof/"):]).name
                self._send_file(Path("data/label_proofs") / name,
                                "image/svg+xml")
            else:
                self.send_error(404)
        except DomainError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"服务器错误：{e}"}, 500)

    # ---------- POST ----------
    def do_POST(self) -> None:
        route = urlparse(self.path).path
        try:
            data = self._read_json()
            if route == "/api/submit":
                vid = self.svc.submit_version(
                    int(data["exhibit_id"]), title=data["title"],
                    dynasty=data["dynasty"], material=data["material"],
                    body=data["body"], qr_url=data["qr_url"],
                    lang=data.get("lang", "zh"), staff_id=int(data["staff_id"]))
                self._send_json({"ok": True, "version_id": vid})
            elif route == "/api/annotate":
                aid = self.svc.annotate(
                    int(data["version_id"]), int(data["staff_id"]),
                    data["comment"], data.get("quote", ""))
                self._send_json({"ok": True, "annotation_id": aid})
            elif route == "/api/review":
                kind = data["kind"]
                if kind == "proofread":
                    rid = self.svc.proofread(
                        int(data["version_id"]), int(data["staff_id"]),
                        bool(data["passed"]), data.get("note", ""))
                else:
                    rid = self.svc.factcheck(
                        int(data["version_id"]), int(data["staff_id"]),
                        bool(data["passed"]), data.get("note", ""),
                        data.get("catalog_no") or None)
                self._send_json({"ok": True, "review_id": rid})
            elif route == "/api/print":
                self._send_json(self.svc.print_label(
                    int(data["version_id"]), int(data["staff_id"])))
            elif route == "/api/scan":
                r = self.svc.scan_install(
                    data["qr_payload"], data["case_code"],
                    int(data["staff_id"]))
                self._send_json({"ok": r.accepted, "accepted": r.accepted,
                                 "reason": r.reason, "binding_id": r.binding_id})
            elif route == "/api/remove":
                self.svc.remove_label(data["case_code"], int(data["staff_id"]),
                                      data.get("reason", "临时撤展"))
                self._send_json({"ok": True})
            elif route == "/api/issue":
                iid = self.svc.report_issue(
                    data["kind"], data["detail"], int(data["staff_id"]),
                    int(data["exhibit_id"]) if data.get("exhibit_id") else None,
                    int(data["case_id"]) if data.get("case_id") else None,
                    int(data["version_id"]) if data.get("version_id") else None)
                self._send_json({"ok": True, "issue_id": iid})
            elif route == "/api/issue-resolve":
                self.svc.resolve_issue(int(data["issue_id"]))
                self._send_json({"ok": True})
            else:
                self.send_error(404)
        except DomainError as e:
            self._send_json({"error": str(e)}, 400)
        except (KeyError, ValueError) as e:
            self._send_json({"error": f"请求参数有误：{e}"}, 400)

    # ---------- 视图数据 ----------
    def _preview(self, version_id: int) -> None:
        v = self.svc.get_version(version_id)
        ex = self.svc.db.execute(
            "SELECT * FROM exhibit WHERE id=?", (v["exhibit_id"],)).fetchone()
        case = self.svc.expected_case(v["exhibit_id"])
        payload = self.svc.qr.encode(
            version_id=version_id, catalog_no=ex["catalog_no"],
            case_code=case["code"] if case else None, url=v["qr_url"])
        path = self.svc.printer.render_proof(
            version_id=version_id, title=v["title"], dynasty=v["dynasty"],
            material=v["material"], body=v["body"],
            catalog_no=ex["catalog_no"],
            qr_svg=self.svc.qr.render_matrix(payload))
        self._send_json({
            "url": f"/proof/{path.name}",
            "width_mm": LABEL_W_MM, "height_mm": LABEL_H_MM,
            "payload": payload,
        })

    def _state(self) -> dict:
        db = self.svc.db

        def rows(sql, params=()):
            return [dict(r) for r in db.execute(sql, params).fetchall()]

        staff = rows("SELECT * FROM staff ORDER BY id")
        exhibits = rows("SELECT * FROM exhibit ORDER BY catalog_no")
        cases = rows(
            """SELECT c.*, e.catalog_no AS exhibit_catalog, e.name AS exhibit_name
               FROM case_table c LEFT JOIN exhibit e ON e.id=c.exhibit_id
               ORDER BY c.code""")
        versions = rows(
            """SELECT v.*, s.name AS created_name, e.catalog_no AS catalog_no,
                      e.name AS exhibit_name
               FROM label_version v
               JOIN staff s ON s.id=v.created_by
               JOIN exhibit e ON e.id=v.exhibit_id
               ORDER BY v.exhibit_id, v.lang, v.version_no""")
        reviews = rows(
            """SELECT r.*, s.name AS reviewer_name FROM review r
               JOIN staff s ON s.id=r.reviewer_id ORDER BY r.id""")
        annotations = rows(
            """SELECT a.*, s.name AS author_name FROM annotation a
               JOIN staff s ON s.id=a.author_id ORDER BY a.id""")
        jobs = rows(
            """SELECT p.*, s.name AS printer_name, v.version_no, v.lang,
                      v.title, e.catalog_no
               FROM print_job p
               JOIN staff s ON s.id=p.printed_by
               JOIN label_version v ON v.id=p.version_id
               JOIN exhibit e ON e.id=v.exhibit_id
               ORDER BY p.id""")
        bindings = rows(
            """SELECT b.*, c.code AS case_code, v.version_no, v.lang, v.title,
                      e.catalog_no
               FROM case_binding b
               JOIN case_table c ON c.id=b.case_id
               JOIN label_version v ON v.id=b.version_id
               JOIN exhibit e ON e.id=v.exhibit_id
               ORDER BY b.id""")
        scans = rows(
            """SELECT g.*, c.code AS case_code, s.name AS scanner_name,
                      ce.code AS expected_code
               FROM scan_log g
               JOIN case_table c ON c.id=g.scanned_case
               JOIN staff s ON s.id=g.scanner_id
               LEFT JOIN case_table ce ON ce.id=g.expected_case
               ORDER BY g.id""")
        issues = rows(
            """SELECT i.*, s.name AS reporter_name, e.name AS exhibit_name,
                      c.code AS case_code, v.version_no, v.lang
               FROM issue i
               LEFT JOIN staff s ON s.id=i.reported_by
               LEFT JOIN exhibit e ON e.id=i.exhibit_id
               LEFT JOIN case_table c ON c.id=i.case_id
               LEFT JOIN label_version v ON v.id=i.version_id
               ORDER BY i.id""")

        # 展柜：当前有效版次 + 待换版次
        latest_by_exhibit: dict[tuple, dict] = {}
        for v in versions:
            latest_by_exhibit[(v["exhibit_id"], v["lang"])] = v
        review_ok = {}
        for v in versions:
            rs = [r for r in reviews
                  if r["version_id"] == v["id"] and r["status"] == "valid"
                  and r["passed"]]
            kinds = {r["kind"] for r in rs}
            review_ok[v["id"]] = {"proofread", "factcheck"} <= kinds
        installed_vids = {b["version_id"] for b in bindings if b["valid"]}
        printed_jobs = {}
        for j in jobs:
            if j["status"] == "printed":
                printed_jobs.setdefault(j["version_id"], j)

        for c in cases:
            active = next((b for b in bindings
                           if b["case_id"] == c["id"] and b["valid"]), None)
            c["current"] = (f"v{active['version_no']}（{active['lang']}）"
                            if active else None)
            c["current_version_id"] = active["version_id"] if active else None
            # 待换：该展品最新中文版已双签、已打印且尚未装入此柜
            latest = latest_by_exhibit.get((c["exhibit_id"], "zh"))
            pending = None
            if latest and review_ok[latest["id"]] \
                    and latest["id"] in printed_jobs \
                    and latest["id"] != (active["version_id"] if active else -1):
                pending = latest
                pending["_job"] = printed_jobs[latest["id"]]
            c["pending"] = (f"v{pending['version_no']}" if pending else None)
            c["pending_version_id"] = pending["id"] if pending else None
            c["pending_payload"] = (printed_jobs[latest["id"]]["qr_payload"]
                                    if pending else None)

        for v in versions:
            v["review_ready"] = review_ok[v["id"]]
            v["is_latest"] = latest_by_exhibit[(v["exhibit_id"], v["lang"])]["id"] == v["id"]

        return {
            "staff": staff, "exhibits": exhibits, "cases": cases,
            "versions": versions, "reviews": reviews,
            "annotations": annotations, "jobs": jobs, "bindings": bindings,
            "scans": scans, "issues": issues,
            "labels": {"issue": ISSUE_LABELS, "role": ROLE_LABELS},
            "label_size": {"w": LABEL_W_MM, "h": LABEL_H_MM},
        }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="博物馆展签更换管理系统")
    ap.add_argument("--db", default="data/museum.db")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--seed", action="store_true", help="启动前重置种子数据")
    args = ap.parse_args()

    if args.seed:
        from .seed import seed
        seed(args.db)

    conn = connect(args.db)
    init_db(conn)
    AppHandler.svc = MuseumService(conn)
    srv = HTTPServer(("127.0.0.1", args.port), AppHandler)
    print(f"展签管理系统已启动：http://127.0.0.1:{args.port}")
    print("（Ctrl+C 停止）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
