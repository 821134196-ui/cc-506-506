"""二维码模拟适配器。

不联网、不依赖第三方库：
- encode/decode 使用应用自定义的 museum:// URI（query 串携带版本与展柜信息），
  真实系统里对应二维码的编码内容；
- render_matrix 依据内容哈希生成确定性的类 QR 点阵 SVG（含三角定位图形），
  仅用于样图与打印预览的可视化。
"""
from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlencode, urlparse

SCHEME = "museum"


class QRAdapter:
    def encode(self, *, version_id: int, catalog_no: str,
               case_code: str | None, url: str) -> str:
        query = [("vid", version_id), ("cat", catalog_no), ("u", url)]
        if case_code:
            query.append(("case", case_code))
        return f"{SCHEME}://label?{urlencode(query)}"

    def decode(self, payload: str) -> dict | None:
        try:
            p = urlparse(payload)
            if p.scheme != SCHEME or p.netloc != "label":
                return None
            q = parse_qs(p.query)
            if "vid" not in q or "cat" not in q:
                return None
            return {
                "version_id": int(q["vid"][0]),
                "catalog_no": q["cat"][0],
                "url": q.get("u", [""])[0],
                "case_code": q.get("case", [None])[0],
            }
        except (ValueError, KeyError):
            return None

    def render_matrix(self, payload: str, modules: int = 29) -> str:
        """返回可内嵌的 <svg> 片段：确定性点阵 + 三角定位符。"""
        bits = hashlib.sha256(payload.encode("utf-8")).digest()
        n = modules
        size = 116  # px，由外层 viewBox 缩放到毫米

        def bit(r: int, c: int) -> bool:
            return bits[(r * n + c) % len(bits)] & (1 << ((r + c) % 8)) > 0

        def in_finder(r: int, c: int) -> bool:
            for ox, oy in ((0, 0), (0, n - 7), (n - 7, 0)):
                if ox <= r < ox + 7 and oy <= c < oy + 7:
                    return True
            return False

        cells: list[str] = []
        for r in range(n):
            for c in range(n):
                if in_finder(r, c):
                    continue
                if bit(r, c):
                    cells.append(
                        f'<rect x="{c}" y="{r}" width="1.02" height="1.02"/>')

        finder = ""
        for ox, oy in ((0, 0), (0, n - 7), (n - 7, 0)):
            finder += (
                f'<rect x="{ox}" y="{oy}" width="7" height="7" fill="none" '
                f'stroke="#2b2b2b" stroke-width="1.4"/>'
                f'<rect x="{ox + 2}" y="{oy + 2}" width="3" height="3"/>')
        return (
            f'<svg viewBox="0 0 {n} {n}" width="{size}" height="{size}" '
            f'xmlns="http://www.w3.org/2000/svg" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#ffffff"/>'
            f'<g fill="#2b2b2b">{"".join(cells)}{finder}</g></svg>')
