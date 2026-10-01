"""HTTP 服务：JSON API + 静态界面。仅用标准库，一条命令即可启动。"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import services as svc
from .adapters import PrinterAdapter, QrAdapter
from .db import connect, init_db

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

DB_PATH = os.environ.get("LABEL_DB", str(Path.cwd() / "data" / "labels.db"))
SAMPLE_DIR = os.environ.get("LABEL_SAMPLES", str(Path.cwd() / "data" / "samples"))


def _get_conn():
    return connect(DB_PATH)


class Handler(BaseHTTPRequestHandler):
    server_version = "MuseumLabel/1.0"

    # ------------------------------------------------------------ 基础
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_svg(self, svg: str, status: int = 200) -> None:
        body = svg.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        p, q = url.path, parse_qs(url.query)
        conn = _get_conn()
        try:
            if p == "/api/state":
                return self._send_json(self._state(conn))
            if p == "/api/version":
                return self._send_json(svc.get_version(conn, int(q["id"][0])))
            if p == "/api/versions":
                return self._send_json(svc.list_versions(conn, q["exhibit_id"][0]))
            if p == "/api/proofreads":
                return self._send_json(svc.list_proofreads(conn, int(q["version_id"][0])))
            if p == "/api/print-queue":
                return self._send_json(svc.print_queue(conn))
            if p == "/api/scans":
                return self._send_json(svc.list_scans(conn))
            if p == "/api/installations":
                return self._send_json(svc.installation_history(conn))
            if p == "/api/incidents":
                return self._send_json(svc.list_incidents(conn))
            if p == "/api/preview":
                v = svc.get_version(conn, int(q["id"][0]))
                svg = PrinterAdapter(SAMPLE_DIR).render_label_svg(self._label_payload(v))
                return self._send_svg(svg)
            if p == "/api/qr":
                return self._send_svg(QrAdapter(q.get("payload", [""])[0]).to_svg(20))
            if p.startswith("/samples/"):
                return self._serve_sample(p.split("/samples/", 1)[1])
            return self._serve_static(p)
        except svc.DomainError as e:
            return self._send_json({"error": str(e)}, 409)
        except (KeyError, ValueError) as e:
            return self._send_json({"error": f"请求参数无效：{e}"}, 400)
        finally:
            conn.close()

    def _state(self, conn) -> dict:
        users = conn.execute("SELECT id, username, display_name, role FROM users ORDER BY id").fetchall()
        cases = conn.execute("SELECT * FROM cases ORDER BY id").fetchall()
        return {
            "users": [dict(r) for r in users],
            "cases": [dict(r) for r in cases],
            "exhibits": svc.list_exhibits(conn),
            "active": svc.active_installations(conn),
            "incidents": svc.list_incidents(conn),
            "scans": svc.list_scans(conn)[:10],
            "prints": svc.print_queue(conn),
            "incident_kinds": svc.INCIDENT_LABELS,
        }

    @staticmethod
    def _label_payload(v: dict) -> dict:
        return {
            "exhibit_id": v["exhibit_id"], "version_no": v["version_no"],
            "case_id": v["case_id"], "title": v["title"], "era": v["era"],
            "material": v["material"], "body": v["body"], "qr_url": v["qr_url"],
            "lang": v["lang"],
        }

    def _serve_sample(self, name: str) -> None:
        # 只允许文件名，防路径穿越
        f = (Path(SAMPLE_DIR) / Path(name).name).resolve()
        if not str(f).startswith(str(Path(SAMPLE_DIR).resolve())) or not f.exists():
            return self._send_json({"error": "样图不存在"}, 404)
        return self._send_svg(f.read_text(encoding="utf-8"))

    def _serve_static(self, p: str) -> None:
        if p == "/":
            p = "/index.html"
        f = (STATIC_DIR / p.lstrip("/")).resolve()
        if not str(f).startswith(str(STATIC_DIR.resolve())) or not f.is_file():
            self.send_response(404)
            self.end_headers()
            return
        ctype = {
            ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
        }.get(f.suffix, "application/octet-stream")
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        p = urlparse(self.path).path
        data = self._body()
        conn = _get_conn()
        try:
            r = self._route_post(conn, p, data)
            self._send_json(r if r is not None else {"ok": True})
        except svc.DomainError as e:
            self._send_json({"error": str(e)}, 409)
        except (KeyError, TypeError, ValueError) as e:
            self._send_json({"error": f"请求参数无效：{e}"}, 400)
        finally:
            conn.close()

    def _route_post(self, conn, p: str, d: dict):
        def i(k, name):
            if name not in d or d[name] in ("", None):
                raise ValueError(f"缺少 {name}")
            return int(d[name])

        if p == "/api/versions":
            return svc.create_version(
                conn, actor_id=int(d["actor_id"]), exhibit_id=d["exhibit_id"],
                title=d["title"], era=d["era"], material=d["material"],
                body=d["body"], qr_url=d["qr_url"], lang=d.get("lang", "zh"),
                base_version_id=int(d["base_version_id"]) if d.get("base_version_id") else None,
                note=d.get("note", ""),
            )
        if p.startswith("/api/versions/") and p.endswith("/edit"):
            vid = int(p.split("/")[3])
            return svc.update_draft_content(
                conn, actor_id=int(d["actor_id"]), version_id=vid,
                title=d["title"], era=d["era"], material=d["material"],
                body=d["body"], qr_url=d["qr_url"], note=d.get("note", ""),
            )
        if p.startswith("/api/versions/") and p.endswith("/proofread"):
            vid = int(p.split("/")[3])
            return svc.add_proofread(conn, actor_id=int(d["actor_id"]),
                                     version_id=vid, comment=d["comment"])
        if p.startswith("/api/versions/") and p.endswith("/factcheck"):
            vid = int(p.split("/")[3])
            return svc.fact_check(conn, actor_id=int(d["actor_id"]), version_id=vid,
                                  exhibit_id_ok=bool(d.get("exhibit_id_ok")),
                                  note=d.get("note", ""))
        if p.startswith("/api/versions/") and p.endswith("/print"):
            vid = int(p.split("/")[3])
            v = svc.get_version(conn, vid)
            printer = PrinterAdapter(SAMPLE_DIR)
            path = printer.print(self._label_payload(v))
            return svc.mark_printed(conn, actor_id=int(d["actor_id"]),
                                    version_id=vid, file_path=path)
        if p == "/api/scan":
            return svc.scan_label(conn, label_version_id=int(d["version_id"]),
                                  scanned_case_id=d["case_id"], operator=d["operator"])
        if p == "/api/install":
            return svc.install_label(conn, actor_name=d["actor_name"],
                                     label_version_id=int(d["version_id"]),
                                     scan_id=int(d["scan_id"]))
        if p == "/api/remove":
            return svc.remove_label(conn, actor_name=d["actor_name"],
                                    case_id=d["case_id"], reason=d.get("reason", ""))
        if p == "/api/incidents":
            return svc.record_incident(
                conn, actor_id=int(d["actor_id"]), kind=d["kind"],
                description=d["description"],
                case_id=d.get("case_id") or None,
                version_id=int(d["version_id"]) if d.get("version_id") else None,
            )
        raise svc.DomainError(f"未知接口 {p}")


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    init_db(_get_conn())
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"展签管理系统已启动：http://{host}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    run()
