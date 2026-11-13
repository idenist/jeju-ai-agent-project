"""Render the reviewable personal proposal/results drafts from their Markdown sources."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepTogether
from pypdf import PdfReader
from agents.juneok.config import PROJECT_ROOT
from agents.juneok.pdf_report import _register_fonts, _styles, _inline, _table, _separator, REGULAR


def footer(canvas, document):
    canvas.saveState()
    canvas.setFont(REGULAR, 8)
    canvas.setFillColor(colors.HexColor("#586a76"))
    canvas.drawString(18 * mm, 12 * mm, "개인 구현 후보 · 2026-11-09 · 팀 최종 합의 전")
    canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, str(document.page))
    canvas.restoreState()


def render(source, output):
    _register_fonts()
    styles = _styles()
    story = []
    document = SimpleDocTemplate(str(output), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                                 topMargin=18 * mm, bottomMargin=21 * mm, title=source.stem,
                                 author="임준억 개인 구현 후보", invariant=1)
    lines = source.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line == "<!-- page -->":
            story.append(PageBreak()); i += 1; continue
        if not line:
            story.append(Spacer(1, 5)); i += 1; continue
        if line.startswith("|") and i + 1 < len(lines) and _separator(lines[i + 1]):
            rows = [line]; i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip()); i += 1
            story.extend([KeepTogether([_table(rows, styles, document.width)]), Spacer(1, 8)])
            continue
        style = styles["body"]
        if line.startswith("# "):
            line = line[2:]; style = styles["title"]
        elif line.startswith("## "):
            line = line[3:]; style = styles["heading"]
        story.append(Paragraph(_inline(line), style)); i += 1
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return len(PdfReader(output).pages)


def main():
    folder = PROJECT_ROOT / "artifacts/submission-documents" / datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%dT%H%M%S")
    folder.mkdir(parents=True)
    documents = []
    for name in ["기획서", "결과설명자료"]:
        source = PROJECT_ROOT / "docs/임준억/제출" / (name + ".md")
        pdf = folder / (name + "_개인후보.pdf")
        pages = render(source, pdf)
        if name == "기획서" and pages > 15:
            raise ValueError("기획서가 공식 15페이지 제한을 넘습니다.")
        documents.append({"source": source.relative_to(PROJECT_ROOT).as_posix(), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                          "pdf": pdf.relative_to(PROJECT_ROOT).as_posix(), "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "pages": pages})
    result = {"scope": "PERSONAL_DRAFT_NOT_TEAM_APPROVAL_OR_SUBMITTED", "documents": documents,
              "data_license_final_check": "PENDING", "human_validation": "NOT_PERFORMED"}
    (PROJECT_ROOT / "artifacts/submission-documents/latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
