#!/usr/bin/env python3
"""Turn MeetScribe's export Markdown into a Word (.docx) or PDF file.

Runs in MeetScribe's own Python (needs python-docx and fpdf2): export.py INPUT.md OUTPUT.docx|OUTPUT.pdf
Understands the small Markdown subset the app writes: # headings, - bullets, **bold**, *italic*, paragraphs.
"""
import pathlib
import re
import sys

FONT_DIR = pathlib.Path("/usr/share/fonts/truetype/dejavu")
INLINE = re.compile(r"(\*\*.+?\*\*|(?<![*\w])\*(?![\s*]).+?(?<!\s)\*(?![*\w]))")


def blocks(md):
    """Yield (kind, level, text): kind is 'h', 'li' or 'p'."""
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("```"):
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            yield "h", len(m.group(1)), m.group(2).replace("**", "")
            continue
        m = re.match(r"^(\s*)(?:[-*]|\d+\.)\s+(.*)$", line)
        if m:
            yield "li", len(m.group(1)) // 2, m.group(2)
            continue
        yield "p", 0, line


def runs(text):
    """Split text into (piece, bold, italic) runs."""
    for piece in INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**"):
            yield piece[2:-2], True, False
        elif piece.startswith("*") and piece.endswith("*") and len(piece) > 2:
            yield piece[1:-1], False, True
        else:
            yield piece, False, False


def to_docx(md, out):
    import docx
    doc = docx.Document()
    for kind, level, text in blocks(md):
        if kind == "h":
            doc.add_heading(text, level=min(level, 4) if level > 1 else 0)
            continue
        para = doc.add_paragraph(style="List Bullet" if kind == "li" else None)
        for piece, bold, italic in runs(text):
            r = para.add_run(piece)
            r.bold, r.italic = bold, italic
    doc.save(out)


def to_pdf(md, out):
    from fpdf import FPDF
    pdf = FPDF(format="A4")
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(True, 18)
    pdf.add_font("DejaVu", "", str(FONT_DIR / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
    oblique = FONT_DIR / "DejaVuSans-Oblique.ttf"
    pdf.add_font("DejaVu", "I", str(oblique if oblique.exists() else FONT_DIR / "DejaVuSans.ttf"))
    pdf.add_page()
    sizes = {1: 18, 2: 14, 3: 12}
    for kind, level, text in blocks(md):
        if kind == "h":
            pdf.ln(3)
            pdf.set_font("DejaVu", "B", sizes.get(level, 11))
            pdf.multi_cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)
            continue
        indent = 6 + level * 5 if kind == "li" else 0
        pdf.set_x(pdf.l_margin + indent)
        if kind == "li":
            pdf.set_font("DejaVu", "", 10.5)
            pdf.write(5.6, "•  ")
        for piece, bold, italic in runs(text):     # plain text runs: no markup surprises from "--" or "__"
            pdf.set_font("DejaVu", "B" if bold else "I" if italic else "", 10.5)
            pdf.write(5.6, piece)
        pdf.ln(5.6)
        pdf.ln(1.5)
    pdf.output(out)


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: export.py INPUT.md OUTPUT.docx|OUTPUT.pdf")
    md = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    out = sys.argv[2]
    if out.lower().endswith(".docx"):
        to_docx(md, out)
    elif out.lower().endswith(".pdf"):
        to_pdf(md, out)
    else:
        sys.exit("output must end in .docx or .pdf")


if __name__ == "__main__":
    main()
