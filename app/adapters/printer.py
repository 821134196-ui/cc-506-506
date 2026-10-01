"""打印机模拟适配器。

- 按真实展签比例 120mm × 80mm 输出 SVG 样图到本地 data/label_proofs/；
- measure_color_delta 依据版本生成确定性的模拟色差 ΔE（模拟印刷批次波动），
  超过服务层容差时由领域服务登记 print_color_delta 问题。
"""
from __future__ import annotations

import hashlib
import html
from pathlib import Path

LABEL_W_MM = 120
LABEL_H_MM = 80
MARGIN_MM = 8


class PrinterAdapter:
    def __init__(self, out_dir: str | Path = "data/label_proofs"):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def measure_color_delta(self, version_id: int) -> float:
        h = hashlib.sha256(f"color-{version_id}".encode()).digest()
        # 确定性的批次色差：0.8 ~ 4.2 ΔE
        return round(0.8 + (h[0] % 35) / 10, 1)

    def render_proof(self, *, version_id: int, title: str, dynasty: str,
                     material: str, body: str, catalog_no: str,
                     qr_svg: str) -> Path:
        path = self.out_dir / f"label_v{version_id}.svg"
        chars_per_line = 30
        lines: list[str] = []
        for para in body.split("\n"):
            while para:
                lines.append(para[:chars_per_line])
                para = para[chars_per_line:]
            if not para and body.endswith("\n"):
                lines.append("")

        line_h = 5.2
        body_top = 40
        body_spans = []
        for i, line in enumerate(lines):
            body_spans.append(
                f'<tspan x="{MARGIN_MM}" y="{body_top + i * line_h}">'
                f'{html.escape(line)}</tspan>')

        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{LABEL_W_MM}mm"
     height="{LABEL_H_MM}mm" viewBox="0 0 {LABEL_W_MM} {LABEL_H_MM}">
  <rect x="0" y="0" width="{LABEL_W_MM}" height="{LABEL_H_MM}"
        fill="#fbfaf7" stroke="#8c7a5e" stroke-width="0.4"/>
  <text x="{MARGIN_MM}" y="18" font-family="Noto Serif CJK SC, serif"
        font-size="9" font-weight="700" fill="#2b2b2b">{html.escape(title)}</text>
  <text x="{MARGIN_MM}" y="28" font-family="Noto Sans CJK SC, sans-serif"
        font-size="4" fill="#6b6b6b">{html.escape(dynasty)} · {html.escape(material)}</text>
  <line x1="{MARGIN_MM}" y1="32" x2="{LABEL_W_MM - MARGIN_MM}" y2="32"
        stroke="#a87c4f" stroke-width="0.6"/>
  <text font-family="Noto Sans CJK SC, sans-serif" font-size="3.6"
        fill="#3a3a3a">{"".join(body_spans)}</text>
  <text x="{MARGIN_MM}" y="{LABEL_H_MM - 6}" font-size="3" fill="#8a8a8a"
        font-family="Noto Sans CJK SC, sans-serif">编号 {html.escape(catalog_no)} · v{version_id}</text>
  <g transform="translate({LABEL_W_MM - 34},{LABEL_H_MM - 34}) scale(0.22)">
    {qr_svg}
  </g>
</svg>
'''
        path.write_text(svg, encoding="utf-8")
        return path
