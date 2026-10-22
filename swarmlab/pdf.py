"""Export de ``report.md`` (con gráficas) a PDF con fpdf2 y la fuente DejaVu que trae matplotlib."""

from __future__ import annotations

import re
from pathlib import Path

_NON_BMP = re.compile(r"[\U00010000-\U0010FFFF]")
_IMG = re.compile(r'<img src="(charts/[^"]+)" alt="([^"]*)"\s*/?>')


def _font_dir() -> Path:
    import matplotlib

    return Path(matplotlib.get_data_path()) / "fonts" / "ttf"


def markdown_to_html(md_text: str, run_root: Path) -> str:
    from markdown_it import MarkdownIt

    html = MarkdownIt("commonmark", {"html": False}).enable("table").render(_NON_BMP.sub("", md_text))
    return _IMG.sub(lambda m: f'<img src="{(run_root / m.group(1)).as_posix()}" width="520">', html)


def report_to_pdf(run_root: Path, out: Path | None = None) -> Path:
    from fpdf import FPDF

    report = run_root / "report.md"
    if not report.is_file():
        raise FileNotFoundError(f"no hay informe en {run_root}; genera uno con `swarmlab report`")
    html = markdown_to_html(report.read_text(encoding="utf-8"), run_root)
    pdf = FPDF(format="A4")
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(True, margin=15)
    fonts = _font_dir()
    for style, name in (
        ("", "DejaVuSans.ttf"),
        ("B", "DejaVuSans-Bold.ttf"),
        ("I", "DejaVuSans-Oblique.ttf"),
        ("BI", "DejaVuSans-BoldOblique.ttf"),
    ):
        pdf.add_font("DejaVu", style, str(fonts / name))
    pdf.add_page()
    pdf.set_font("DejaVu", size=10)
    pdf.write_html(html, font_family="DejaVu", warn_on_tags_not_matching=False)
    path = out or run_root / "report.pdf"
    pdf.output(str(path))
    return path
