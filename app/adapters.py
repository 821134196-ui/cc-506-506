"""模拟适配器：二维码与标签打印机。

均为模拟实现（无外部依赖）：
- QrAdapter 生成"可扫外观"的确定性矩阵 SVG（三个寻像角 + 由链接内容派生的数据点），
  现场扫码流程由界面模拟配对，不做真实图像解码。
- PrinterAdapter 按真实标签比例（100mm × 60mm）把展签渲染为 SVG 样图写入本地目录，
  并追加打印流水日志。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

LABEL_W_MM = 100
LABEL_H_MM = 60


class QrAdapter:
    """模拟二维码：25×25 矩阵，确定性生成。"""

    N = 25

    def __init__(self, payload: str):
        self.payload = payload

    def _matrix(self) -> list[list[int]]:
        n = self.N
        seed = hashlib.sha256(self.payload.encode("utf-8")).digest()
        bits = "".join(f"{b:08b}" for b in seed)
        m = [[0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                m[i][j] = int(bits[(i * n + j) % len(bits)])
        self._stamp_finder(m, 0, 0)
        self._stamp_finder(m, 0, n - 7)
        self._stamp_finder(m, n - 7, 0)
        return m

    @staticmethod
    def _stamp_finder(m: list[list[int]], r: int, c: int) -> None:
        for i in range(7):
            for j in range(7):
                ring = (i in (0, 6) or j in (0, 6))
                core = (2 <= i <= 4 and 2 <= j <= 4)
                m[r + i][c + j] = 1 if (ring or core) else 0

    def to_svg(self, mm: float = 20.0) -> str:
        n = self.N
        cell = mm / n
        m = self._matrix()
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{mm}mm" height="{mm}mm" '
            f'viewBox="0 0 {mm} {mm}">',
            '<rect width="100%" height="100%" fill="#ffffff"/>',
        ]
        for i in range(n):
            for j in range(n):
                if m[i][j]:
                    parts.append(
                        f'<rect x="{j*cell:.3f}" y="{i*cell:.3f}" '
                        f'width="{cell:.3f}" height="{cell:.3f}" fill="#2b2d2f"/>'
                    )
        parts.append("</svg>")
        return "".join(parts)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _wrap_cjk(text: str, width: int) -> list[str]:
    """按"字数"粗折行（中文标签排版足够；英文按字符保守折行）。"""
    lines, line = [], ""
    for ch in text:
        line += ch
        if len(line) >= width:
            lines.append(line)
            line = ""
    if line:
        lines.append(line)
    return lines


class PrinterAdapter:
    """模拟打印机：输出真实比例 SVG 展签样图到本地目录。"""

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.output_dir / "printer.log"

    def render_label_svg(self, label: dict) -> str:
        """label: title/era/material/body/qr_url/exhibit_id/version_no/case_id。"""
        W, H = LABEL_W_MM, LABEL_H_MM
        qr = QrAdapter(label["qr_url"]).to_svg(20)
        body_lines = _wrap_cjk(label["body"], 30)[:5]
        body_svg = "".join(
            f'<text x="8" y="{30 + i*4.6:.1f}" class="body">{_esc(ln)}</text>'
            for i, ln in enumerate(body_lines)
        )
        return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}mm" height="{H}mm"
 viewBox="0 0 {W} {H}">
<defs><style>
 .t{{font:bold 6.4px 'Noto Serif CJK SC','Songti SC',serif}}
 .m{{font:3.4px sans-serif;fill:#55595c}}
 .body{{font:3.6px sans-serif;fill:#2b2d2f}}
 .foot{{font:2.8px sans-serif;fill:#8a8f94}}
 .cu{{fill:#9a6a3a}}
</style></defs>
<rect x="0.4" y="0.4" width="{W-0.8}" height="{H-0.8}" fill="#fdfcfa" stroke="#2b2d2f" stroke-width="0.5"/>
<rect x="0.4" y="0.4" width="1.6" height="{H-0.8}" class="cu"/>
<text x="8" y="11" class="t">{_esc(label["title"])}</text>
<text x="8" y="17" class="m">{_esc(label["era"])} · {_esc(label["material"])}</text>
<line x1="8" y1="20.5" x2="68" y2="20.5" stroke="#c9c4bb" stroke-width="0.4"/>
{body_svg}
<g transform="translate(72,30)">{qr.split("<svg",1)[1].split(">",1)[1].rsplit("</svg>",1)[0]}</g>
<text x="72" y="55" class="foot">展品 {_esc(label["exhibit_id"])} · 展柜 {_esc(label["case_id"])}</text>
<text x="8" y="55" class="foot">版次 v{label["version_no"]} · {_esc(label.get("lang","zh"))} · 馆内展品导览</text>
</svg>'''

    def print(self, label: dict) -> str:
        """模拟打印：写样图文件并追加流水，返回文件路径。"""
        fname = f"label_{label['exhibit_id']}_v{label['version_no']}.svg"
        path = self.output_dir / fname
        path.write_text(self.render_label_svg(label), encoding="utf-8")
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\tPRINT\t{fname}\n")
        return str(path)
