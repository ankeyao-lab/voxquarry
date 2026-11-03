"""Deterministic text-only Markdown/PDF rendering in the analysis container.

This module never opens input paths, resolves links, or loads user-supplied
fonts/images. The worker supplies its existing no-follow output opener.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import html
import io
import os
import re
import signal


RENDER_VERSION = "wenquan-report-render/v1"
FONT_PATH = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PAGES = 200
RENDER_TIMEOUT = 60


class RenderError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"markdown", "title", "filename", "formats"}:
        raise RenderError("report_render_arguments_invalid")
    markdown, title, filename, formats = (arguments[key] for key in ("markdown", "title", "filename", "formats"))
    if not isinstance(markdown, str) or not markdown.strip() or len(markdown) > 100000:
        raise RenderError("report_render_markdown_invalid")
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or any(ord(char) < 32 for char in title):
        raise RenderError("report_render_title_invalid")
    if (not isinstance(filename, str) or not re.fullmatch(r"[^/\\\x00-\x1f:]{1,120}", filename)
            or filename.startswith(".") or filename != filename.strip() or filename.endswith(".")):
        raise RenderError("report_render_filename_invalid")
    if (not isinstance(formats, list) or not 1 <= len(formats) <= 2
            or any(value not in ("md", "pdf") for value in formats) or len(set(formats)) != len(formats)):
        raise RenderError("report_render_formats_invalid")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in markdown + title + filename):
        raise RenderError("report_render_text_invalid")


@contextmanager
def bounded_render():
    # The worker handles exactly one request in its main thread. The manager
    # independently kills the disposable container if the request overruns.
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    def expired(_signal, _frame):
        raise RenderError("report_render_timeout")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, RENDER_TIMEOUT)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def _text(value):
    return "".join(char for char in value if ord(char) >= 32 or char in "\n\t")


def _markdown_text(value):
    safe = re.sub(r"([\\`*{}\[\]()#+.!_|>~-])", r"\\\1", value)
    return safe.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(children):
    """Return escaped PDF markup, safe Markdown, and plain display text."""
    pdf, markdown, plain, links = [], [], [], []
    marks = {"strong_open": ("<b>", "**"), "strong_close": ("</b>", "**"),
             "em_open": ("<i>", "*"), "em_close": ("</i>", "*"),
             "s_open": ("<strike>", "~~"), "s_close": ("</strike>", "~~")}
    for token in children or []:
        kind = token.type
        if kind in {"html_inline", "image"}:
            continue
        if kind in marks:
            pdf.append(marks[kind][0]); markdown.append(marks[kind][1])
        elif kind in {"softbreak", "hardbreak"}:
            pdf.append("<br/>" if kind == "hardbreak" else " ")
            markdown.append("  \n" if kind == "hardbreak" else "\n"); plain.append(" ")
        elif kind in {"text", "code_inline"}:
            value = _text(token.content)
            escaped = html.escape(value)
            pdf.append(escaped)
            # Escaping also stops literal text that resembles HTML or images
            # from becoming active markup when the downloaded MD is opened.
            markdown.append(_markdown_text(value))
            plain.append(value)
        elif kind == "link_open":
            url = token.attrGet("href") or ""
            links.append((url if re.match(r"^https?://", url, flags=re.I) else "", len(plain)))
        elif kind == "link_close":
            # Preserve source addresses as plain text, never PDF actions or
            # resources. Only an explicit user's later click could open one.
            url, start = links.pop() if links else ("", 0)
            if url and url != "".join(plain[start:]):
                suffix = " (" + _text(url) + ")"
                pdf.append(html.escape(suffix)); markdown.append(_markdown_text(suffix)); plain.append(suffix)
    return "".join(pdf), "".join(markdown), "".join(plain)


def parse_markdown(markdown):
    from markdown_it import MarkdownIt
    tokens = MarkdownIt("commonmark", {"html": True, "linkify": False}).enable("table").enable("strikethrough").parse(markdown)
    if len(tokens) > 30000:
        raise RenderError("report_render_complexity_limit")
    blocks, markdown_lines, lists = [], [], []
    quote_depth, heading, item_prefix, table, row, cell = 0, 0, None, None, None, None
    for token in tokens:
        kind = token.type
        if kind == "html_block":
            continue
        if kind in {"bullet_list_open", "ordered_list_open"}:
            lists.append({"ordered": kind == "ordered_list_open", "next": int(token.attrGet("start") or 1)})
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop(); markdown_lines.append("")
        elif kind == "list_item_open":
            current = lists[-1]
            item_prefix = f"{current['next']}. " if current["ordered"] else "- "
            current["next"] += 1
        elif kind == "list_item_close":
            item_prefix = None
        elif kind == "blockquote_open":
            quote_depth += 1
        elif kind == "blockquote_close":
            quote_depth -= 1
        elif kind == "heading_open":
            heading = int(token.tag[1:])
        elif kind == "heading_close":
            heading = 0
        elif kind == "table_open":
            table = []
        elif kind == "tr_open":
            row = []
        elif kind in {"th_open", "td_open"}:
            cell = ("", "", "")
        elif kind in {"th_close", "td_close"}:
            row.append(cell); cell = None
        elif kind == "tr_close":
            table.append(row); row = None
        elif kind == "table_close":
            if not table or max(map(len, table)) > 24 or len(table) > 2000:
                raise RenderError("report_render_table_limit")
            blocks.append({"kind": "table", "rows": table})
            for index, values in enumerate(table):
                markdown_lines.append("| " + " | ".join(value[1].replace("\n", " ") for value in values) + " |")
                if index == 0:
                    markdown_lines.append("| " + " | ".join("---" for _ in values) + " |")
            markdown_lines.append(""); table = None
        elif kind == "inline":
            content = _inline(token.children)
            if cell is not None:
                cell = content
                continue
            if not content[2].strip():
                continue
            prefix = item_prefix or ""
            indent = max(0, len(lists) - 1) * 16
            blocks.append({"kind": "heading" if heading else "paragraph", "level": heading,
                           "content": content, "prefix": prefix, "indent": indent, "quote": bool(quote_depth)})
            mark_prefix = ("> " * quote_depth) + ("    " * max(0, len(lists) - 1))
            mark_prefix += "#" * heading + " " if heading else prefix
            markdown_lines.extend([mark_prefix + content[1], ""])
            item_prefix = None
        elif kind in {"fence", "code_block"}:
            value = _text(token.content).rstrip("\n")
            blocks.append({"kind": "code", "text": value})
            fence = "`" * max(3, max((len(match[0]) + 1 for match in re.finditer(r"`+", value)), default=3))
            markdown_lines.extend([fence, value, fence, ""])
        elif kind == "hr":
            blocks.append({"kind": "rule"}); markdown_lines.extend(["---", ""])
    if not blocks:
        raise RenderError("report_render_empty")
    return blocks, "\n".join(markdown_lines).strip() + "\n"


class LimitedBytesIO(io.BytesIO):
    def write(self, value):
        if self.tell() + len(value) > MAX_FILE_BYTES:
            raise RenderError("report_render_file_limit")
        return super().write(value)


def render_pdf(blocks, title, *, font_path=FONT_PATH):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle, HRFlowable, CondPageBreak

    if not os.path.isfile(font_path):
        raise RenderError("report_render_font_unavailable")
    font = "WenquanCJK"
    try:
        embedded_font = TTFont(font, font_path, subfontIndex=0)
        # The bundled CJK font lacks U+2212. Without a glyph, a negative value
        # becomes visually positive. Reuse its hyphen outline while preserving
        # the original minus codepoint in the PDF's Unicode map and in Markdown.
        face = embedded_font.face
        if not face.charToGlyph.get(0x2212):
            face.charToGlyph[0x2212] = face.charToGlyph[0x2D]
            face.charWidths[0x2212] = face.charWidths[0x2D]
        pdfmetrics.registerFont(embedded_font)
        pdfmetrics.registerFontFamily(font, normal=font, bold=font, italic=font, boldItalic=font)
    except Exception as error:
        raise RenderError("report_render_font_unavailable") from error
    ink, muted, accent = colors.HexColor("#18362D"), colors.HexColor("#69766F"), colors.HexColor("#25856B")
    body = ParagraphStyle("body", fontName=font, fontSize=10.5, leading=17, textColor=ink,
                          spaceAfter=8, alignment=TA_LEFT, wordWrap="CJK", splitLongWords=True, allowWidows=0, allowOrphans=0)
    title_style = ParagraphStyle("title", parent=body, fontSize=22, leading=31, spaceAfter=18)
    headings = {level: ParagraphStyle(f"h{level}", parent=body, fontSize=17 if level == 1 else 13.5 if level == 2 else 11.5,
                   leading=24 if level == 1 else 20, spaceBefore=15, spaceAfter=8, keepWithNext=True) for level in range(1, 7)}
    table_style = ParagraphStyle("cell", parent=body, fontSize=9, leading=14, spaceAfter=0, allowWidows=1, allowOrphans=1)
    code_style = ParagraphStyle("code", parent=body, fontSize=9, leading=14, backColor=colors.HexColor("#F1F5F2"),
                                borderPadding=6, spaceBefore=4)
    stream = LimitedBytesIO()
    width, height = A4
    content_width = width - 40 * mm
    document = SimpleDocTemplate(stream, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                                 topMargin=23 * mm, bottomMargin=22 * mm, title=title, author="AI 研究", invariant=1,
                                 pageCompression=1, allowSplitting=1)
    class BoundedCanvas(Canvas):
        def showPage(self):
            if self.getPageNumber() > MAX_PAGES:
                raise RenderError("report_render_page_limit")
            super().showPage()
    def page_frame(canvas, _document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#DCE5DF")); canvas.setLineWidth(0.5)
        canvas.line(20 * mm, height - 16 * mm, width - 20 * mm, height - 16 * mm)
        canvas.setFillColor(muted); canvas.setFont(font, 8)
        canvas.drawString(20 * mm, height - 13 * mm, "AI 研究 / 研究简报")
        canvas.drawRightString(width - 20 * mm, 12 * mm, str(canvas.getPageNumber()))
        canvas.restoreState()
    story = []
    if not (blocks[0]["kind"] == "heading" and blocks[0]["content"][2].strip() == title.strip()):
        story.append(Paragraph(html.escape(title), title_style))
    for block_index, block in enumerate(blocks):
        kind = block["kind"]
        if kind in {"heading", "paragraph"}:
            style = headings[block["level"]] if kind == "heading" else body
            if kind == "heading" and block_index + 1 < len(blocks) and blocks[block_index + 1]["kind"] == "table":
                # Keep enough room for the heading, header and first row, but
                # do not move the entire long table to a new, mostly empty page.
                story.append(CondPageBreak(100))
                style = ParagraphStyle("table-heading", parent=style, keepWithNext=False)
            if kind == "paragraph" and (block["indent"] or block["prefix"] or block["quote"]):
                style = ParagraphStyle("indented", parent=style, leftIndent=block["indent"] + (12 if block["prefix"] else 0)
                                       + (12 if block["quote"] else 0), textColor=muted if block["quote"] else ink,
                                       bulletIndent=block["indent"], bulletFontName=font, bulletFontSize=10)
            story.append(Paragraph(block["content"][0], style, bulletText=block["prefix"].strip() or None))
        elif kind == "code":
            for line in block["text"].split("\n"):
                story.append(Paragraph(html.escape(line).replace(" ", "&#160;") or "&#160;", code_style))
        elif kind == "rule":
            story.extend([HRFlowable(width="100%", color=accent, thickness=0.7), Spacer(1, 9)])
        elif kind == "table":
            rows = block["rows"]
            columns = max(map(len, rows))
            if columns > 6:
                # Wide tables become labeled records, keeping every cell while
                # avoiding unreadable columns or a clipped landscape export.
                headers = rows[0]
                for index, row in enumerate(rows[1:], 1):
                    story.append(Paragraph(str(index), headings[3]))
                    for column, cell in enumerate(row):
                        label = headers[column][0] if column < len(headers) else str(column + 1)
                        story.append(Paragraph(f"<b>{label}</b>: {cell[0]}", body))
                continue
            data = [[Paragraph(cell[0] or "&#160;", table_style) for cell in row] + [""] * (columns - len(row)) for row in rows]
            weights = [max(40, min(240, max(pdfmetrics.stringWidth(row[column][2], font, 9)
                        for row in rows if column < len(row)))) for column in range(columns)]
            table = LongTable(data, colWidths=[content_width * weight / sum(weights) for weight in weights], repeatRows=1,
                              splitByRow=1, splitInRow=1, hAlign="LEFT")
            table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F0EB")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#DCE5DF")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
            story.extend([table, Spacer(1, 12)])
    document.build(story, onFirstPage=page_frame, onLaterPages=page_frame, canvasmaker=BoundedCanvas)
    return stream.getvalue()


def render_report(arguments, *, open_output, work_root, font_path=FONT_PATH):
    validate(arguments)
    with bounded_render():
        blocks, markdown = parse_markdown(arguments["markdown"])
        if not (blocks[0]["kind"] == "heading" and blocks[0]["content"][2].strip() == arguments["title"].strip()):
            markdown = "# " + _markdown_text(arguments["title"]) + "\n\n" + markdown
        outputs = []
        # Render all requested formats before writing any output. A PDF error
        # must not be reported as a successful partial MD/PDF delivery.
        for format_name in arguments["formats"]:
            raw = markdown.encode("utf-8") if format_name == "md" else render_pdf(blocks, arguments["title"], font_path=font_path)
            if len(raw) > MAX_FILE_BYTES:
                raise RenderError("report_render_file_limit")
            filename = arguments["filename"] + "." + format_name
            outputs.append((filename, raw, "text/markdown" if format_name == "md" else "application/pdf"))
        files = []
        for filename, raw, media_type in outputs:
            path = work_root + "/" + filename
            with os.fdopen(open_output(path, write=True), "wb") as output:
                output.write(raw)
            files.append({"path": path, "filename": filename, "media_type": media_type,
                          "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        return {"render_version": RENDER_VERSION, "files": files}


def main():
    """Optional native Bash utility; no research or section generation."""
    import argparse
    import json
    from analysis_worker import secure_file, WORK_ROOT
    parser = argparse.ArgumentParser(description='Render your own Markdown in the research workspace.')
    parser.add_argument('--input', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--filename', required=True)
    parser.add_argument('--formats', nargs='+', choices=('md', 'pdf'), default=['md'])
    args = parser.parse_args()
    with os.fdopen(secure_file(args.input), 'r', encoding='utf-8') as source:
        markdown = source.read(100001)
    files, failures = [], []
    for form in dict.fromkeys(sorted(args.formats, key=lambda value: value != 'md')):
        try:
            result = render_report({'markdown': markdown, 'title': args.title,
                'filename': args.filename, 'formats': [form]}, open_output=secure_file, work_root=WORK_ROOT)
            files.extend(result['files'])
        except RenderError as error:
            failures.append({'format': form, 'code': error.code})
    print(json.dumps({'files': files, 'failures': failures}, ensure_ascii=False))
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
