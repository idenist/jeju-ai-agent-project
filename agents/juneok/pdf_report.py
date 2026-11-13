"""Offline PDF export of the exact canonical report, with bundled Korean fonts."""
from __future__ import annotations

from functools import lru_cache
from html import escape
from io import BytesIO
from pathlib import Path
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .reporting import render_report, render_result, REPORT_FOOTER, REPORT_NOTICES_HEADING

FONT_DIR = Path(__file__).resolve().parents[2] / "assets/fonts"
REGULAR = "JejuNanumGothic"
BOLD = "JejuNanumGothicBold"
INK = colors.HexColor("#182b3a")
ACCENT = colors.HexColor("#c76420")


@lru_cache(maxsize=1)
def _register_fonts():
    """Only local bundled files; exporting never fetches fonts or reads OS fonts."""
    for name, filename in [(REGULAR, "NanumGothic-Regular.ttf"), (BOLD, "NanumGothic-Bold.ttf")]:
        path = FONT_DIR / filename
        if not path.is_file():
            raise ValueError("PDF 한글 글꼴이 없습니다. 배포 패키지의 assets/fonts를 확인하세요.")
        pdfmetrics.registerFont(TTFont(name, str(path)))
    pdfmetrics.registerFontFamily(REGULAR, normal=REGULAR, bold=BOLD, italic=REGULAR, boldItalic=BOLD)


def _visible_text(text):
    # Preserve unsupported characters as an explicit code point, never a blank/tofu glyph.
    supported = pdfmetrics.getFont(REGULAR).face.charToGlyph
    return "".join(c if ord(c) in supported or c in "\n\t" else f"[U+{ord(c):04X}]" for c in str(text))


def _inline(text):
    text = _visible_text(text)
    # Escape source data before introducing our own small, known formatting vocabulary.
    from urllib.parse import urlsplit
    chunks, cursor = [], 0
    for match in re.finditer(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", text):
        chunks.append(escape(text[cursor:match.start()], quote=False))
        label, url = match.groups()
        visible = escape(f"{label} ({url})", quote=False)
        try:
            parsed = urlsplit(url)
            allowed = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
        except ValueError:
            allowed = False
        chunks.append(f'<link href="{escape(url, quote=True)}">{visible}</link>' if allowed else visible)
        cursor = match.end()
    chunks.append(escape(text[cursor:], quote=False))
    escaped = "".join(chunks)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped).replace("`", "")


def _cells(line):
    return [x.strip().replace(r"\|", "|") for x in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _separator(line):
    return bool(line.strip().startswith("|") and all(re.fullmatch(r":?-{3,}:?", x) for x in _cells(line)))


def _styles():
    base = dict(fontName=REGULAR, fontSize=10, leading=14.5, textColor=INK,
                alignment=TA_LEFT, wordWrap="CJK", splitLongWords=True, spaceAfter=3.5)
    return {
        "body": ParagraphStyle("body", **base),
        "title": ParagraphStyle("title", **{**base, "fontName": BOLD, "fontSize": 20, "leading": 28,
                                              "spaceAfter": 14, "keepWithNext": True}),
        "heading": ParagraphStyle("heading", **{**base, "fontName": BOLD, "fontSize": 13, "leading": 20,
                                                  "spaceBefore": 13, "spaceAfter": 7, "keepWithNext": True}),
        "cell": ParagraphStyle("cell", **{**base, "fontSize": 8.5, "leading": 13, "spaceAfter": 0}),
        "bullet": ParagraphStyle("bullet", **{**base, "leftIndent": 9, "firstLineIndent": -7}),
        "caption": ParagraphStyle("caption", **{**base, "keepWithNext": True}),
        "table_caption": ParagraphStyle("table_caption", **{**base, "keepWithNext": False}),
        "notice": ParagraphStyle("notice", **{**base, "fontSize": 8.5, "leading": 12, "spaceAfter": 0,
                                                 "textColor": colors.HexColor("#445563")}),
        "notice_title": ParagraphStyle("notice_title", **{**base, "fontName": BOLD, "fontSize": 10, "leading": 14, "spaceAfter": 0}),
    }


def _table(lines, styles, width):
    raw = [_cells(line) for line in lines]
    count = len(raw[0])
    if any(len(row) != count for row in raw):
        raise ValueError("보고서 표의 열 수가 다릅니다. PDF를 생성하지 않았습니다.")
    if count == 2:
        weights = [0.31, 0.69]
    elif count == 6:
        weights = [0.16, 0.14, 0.16, 0.20, 0.18, 0.16]
    else:
        weights = [1 / count] * count
    data = [[Paragraph(_inline(value), styles["cell"]) for value in row] for row in raw]
    table = Table(data, colWidths=[width * w for w in weights], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f4")),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK),
        ("LINEBELOW", (0, 0), (-1, 0), 1, ACCENT),
        ("LINEBELOW", (0, 1), (-1, -1), 0.35, colors.HexColor("#dce2e5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _footer(canvas, document):
    canvas.saveState()
    width, height = A4
    canvas.setStrokeColor(colors.HexColor("#dce2e5"))
    canvas.line(document.leftMargin, 17 * mm, width - document.rightMargin, 17 * mm)
    canvas.setFont(REGULAR, 8)
    canvas.setFillColor(colors.HexColor("#586a76"))
    canvas.drawString(document.leftMargin, 12 * mm, REPORT_FOOTER)
    canvas.drawRightString(width - document.rightMargin, 12 * mm, f"{document.page:02d}")
    canvas.setFont(REGULAR, 8)
    canvas.drawString(document.leftMargin, height - 10 * mm, "감귤 출하 검토")
    canvas.drawRightString(width - document.rightMargin, height - 10 * mm, "분석 보고서")
    canvas.restoreState()


def _notice_box(lines, styles, width):
    rows = [[Paragraph(REPORT_NOTICES_HEADING, styles["notice_title"])]]
    for line in lines:
        if line.strip() and line.strip() != REPORT_FOOTER:
            rows.append([Paragraph(_inline(line.strip()), styles["notice"])])
    box = Table(rows, colWidths=[width], repeatRows=1, hAlign="LEFT")
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f3f5f7")),
        ("LINEBEFORE", (0, 0), (0, -1), 2, colors.HexColor("#8b9ba8")),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, 0), 6), ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
        ("TOPPADDING", (0, 1), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -2), 2),
    ]))
    return box


def render_pdf_report(results: list[dict]) -> bytes:
    """Export canonical Python results; no LLM, HTTP, browser, or OS font dependency."""
    _register_fonts()
    markdown = render_report(results)
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=17 * mm, rightMargin=17 * mm,
                                 topMargin=18 * mm, bottomMargin=23 * mm, pageCompression=1,
                                 title="제주 감귤 출하 비교 보고서", author="제주 감귤 Agent", invariant=1)
    styles = _styles()
    story = []
    last_section_start = None
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line == REPORT_FOOTER:
            index += 1
            continue
        if line == "## " + REPORT_NOTICES_HEADING:
            notes = [Spacer(1, 8), _notice_box(lines[index + 1:], styles, document.width)]
            last = results[-1].get("result", results[-1]) if results and isinstance(results[-1], dict) else {}
            if len(results) > 1 and last.get("status") != "OK" and len(render_result(last)) < 1000 and last_section_start is not None:
                tail = story[last_section_start:]
                story[last_section_start:] = [KeepTogether([*tail, *notes])]
            else:
                # Short notices must not leave one last bullet on an otherwise empty page.
                story.append(KeepTogether(notes))
            break
        if line.startswith("- ") and "하루 보관비" in line:
            group = []
            while index < len(lines) and lines[index].strip().startswith("- "):
                group.append(Paragraph(_inline(lines[index].strip()), styles["bullet"]))
                index += 1
            story.append(KeepTogether(group))
            continue
        if line.startswith("|") and index + 1 < len(lines) and _separator(lines[index + 1]):
            table_lines = [line]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index])
                index += 1
            table = _table(table_lines, styles, document.width)
            # Keep small input tables intact; large evidence/sensitivity tables repeat headers.
            if len(table_lines) <= 8:
                prefix = []
                while story and isinstance(story[-1], Paragraph) and story[-1].getKeepWithNext():
                    prefix.insert(0, story.pop())
                story += [KeepTogether([*prefix, table]), Spacer(1, 8)]
            else:
                story += [table, Spacer(1, 8)]
            continue
        if line.startswith("```"):
            index += 1
            continue
        style = styles["body"]
        if line.startswith("문서 ID:"):
            style = styles["notice"]
        if line.startswith("# "):
            line, style = line[2:], styles["title"]
        elif line.startswith("## "):
            line, style = line[3:], styles["heading"]
            story.append(CondPageBreak(240))
            last_section_start = len(story)
        elif line.startswith("- "):
            line, style = "- " + line[2:], styles["bullet"]
        elif line.endswith(":"):
            style = styles["caption"]
        elif re.fullmatch(r"\*\*.+\*\*", line):
            style = styles["caption"]
            if line.startswith("**표 "):
                look = index + 1
                while look < len(lines) and not lines[look].strip():
                    look += 1
                end = look
                while end < len(lines) and lines[end].strip().startswith("|"):
                    end += 1
                if end - look > 9:
                    # Keep a caption with the first rows, never the complete 49-row table.
                    style = styles["table_caption"]
                    story.append(CondPageBreak(110))
        story.append(Paragraph(_inline(line), style))
        index += 1
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()
