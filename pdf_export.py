"""
pdf_export.py — Render a chat (text + inline photos) to a single PDF.

Uses fpdf2 for layout and Pillow to measure images before placing them (so
we can compute page breaks ourselves rather than rely on library auto-flow).
HEIC photos (the default iPhone camera format) aren't decodable by fpdf2 or
stock Pillow, so we convert them to JPEG first via macOS's built-in `sips`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from fpdf import FPDF
from PIL import Image

from ios_backup import Backup, extract_file

MARGIN_MM = 15
MAX_IMAGE_WIDTH_MM = 120
MAX_IMAGE_HEIGHT_MM = 150

# macOS ships this Unicode-coverage TrueType font system-wide. iMessage text
# routinely contains em-dashes, curly quotes, and non-Latin scripts that the
# built-in PDF core fonts (Helvetica etc.) can't encode at all — they only
# support latin-1 and raise on anything else. We need a real Unicode font.
FONT_FILE = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
FONT_FAMILY = "AppleUnicode"


def _convert_heic_to_jpeg(src: str, tmpdir: str) -> str | None:
    dst = os.path.join(tmpdir, os.path.basename(src) + ".jpg")
    try:
        subprocess.run(
            ["sips", "-s", "format", "jpeg", src, "--out", dst],
            check=True,
            capture_output=True,
        )
        return dst if os.path.isfile(dst) else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _resolve_attachment_path(filename: str, backup: Backup | None, tmpdir: str) -> str | None:
    """Get a local, readable path to an attachment, extracting from a backup
    if needed. Returns None if the file can't be found."""
    if backup is not None:
        rel = filename[2:] if filename.startswith("~/") else filename.lstrip("/")
        dst = os.path.join(tmpdir, os.path.basename(filename))
        return dst if extract_file(backup, "MediaDomain", rel, dst) else None
    src = os.path.expanduser(filename)
    return src if os.path.isfile(src) else None


def _line(pdf: FPDF, page_width: float, text: str) -> None:
    """multi_cell() that always leaves the cursor at the left margin on the
    next line — its default leaves x at the cell's right edge, which pushes
    every subsequent line/image further right until it runs off the page."""
    pdf.multi_cell(page_width, 5, text, new_x="LMARGIN", new_y="NEXT")


def _place_image(pdf: FPDF, path: str, page_width: float) -> None:
    with Image.open(path) as img:
        px_w, px_h = img.size
    w_mm = min(MAX_IMAGE_WIDTH_MM, page_width)
    h_mm = w_mm * (px_h / px_w)
    if h_mm > MAX_IMAGE_HEIGHT_MM:
        h_mm = MAX_IMAGE_HEIGHT_MM
        w_mm = h_mm * (px_w / px_h)

    if pdf.get_y() + h_mm > pdf.page_break_trigger:
        pdf.add_page()

    pdf.image(path, x=pdf.l_margin, y=pdf.get_y(), w=w_mm, h=h_mm)
    pdf.set_y(pdf.get_y() + h_mm + 3)


def build_pdf(
    messages,
    backup: Backup | None,
    output_path: str,
    title: str = "Conversation",
) -> None:
    """messages: iterable of dicts from messages_library.iter_messages_full()."""
    if not os.path.isfile(FONT_FILE):
        raise RuntimeError(f"Unicode font not found at {FONT_FILE!r} — can't safely render arbitrary message text.")

    pdf = FPDF(format="A4")
    pdf.add_font(FONT_FAMILY, "", FONT_FILE)
    pdf.set_auto_page_break(auto=True, margin=MARGIN_MM)
    pdf.set_margins(MARGIN_MM, MARGIN_MM, MARGIN_MM)
    pdf.add_page()

    # Arial Unicode ships as a single regular weight (no bold/italic file), so
    # style differences below come from size/color, not font style flags.
    pdf.set_font(FONT_FAMILY, "", 16)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    page_width = pdf.w - 2 * MARGIN_MM

    with tempfile.TemporaryDirectory(prefix="msgexport_pdf_") as tmpdir:
        for msg in messages:
            if not msg["text"] and not msg["attachments"]:
                continue

            who = "Me" if msg["is_from_me"] else (msg["sender"] or "Unknown")
            when = msg["dt"].strftime("%Y-%m-%d %H:%M:%S") if msg["dt"] else "?"

            pdf.set_font(FONT_FAMILY, "", 9)
            pdf.set_text_color(110, 110, 110)
            _line(pdf, page_width, f"{when} — {who}")

            if msg["text"]:
                pdf.set_font(FONT_FAMILY, "", 10)
                pdf.set_text_color(0, 0, 0)
                _line(pdf, page_width, msg["text"])

            for att in msg["attachments"]:
                mime = att["mime_type"] or ""
                label = att["transfer_name"] or os.path.basename(att["filename"] or "attachment")

                if not mime.startswith("image/") or not att["filename"]:
                    pdf.set_font(FONT_FAMILY, "", 9)
                    pdf.set_text_color(150, 100, 0)
                    _line(pdf, page_width, f"[Attachment: {label} ({mime or 'unknown type'})]")
                    continue

                path = _resolve_attachment_path(att["filename"], backup, tmpdir)
                if path and mime in ("image/heic", "image/heif"):
                    path = _convert_heic_to_jpeg(path, tmpdir) or path

                if not path:
                    pdf.set_font(FONT_FAMILY, "", 9)
                    pdf.set_text_color(150, 100, 0)
                    _line(pdf, page_width, f"[Missing attachment: {label}]")
                    continue

                try:
                    _place_image(pdf, path, page_width)
                except Exception:
                    pdf.set_font(FONT_FAMILY, "", 9)
                    pdf.set_text_color(150, 100, 0)
                    _line(pdf, page_width, f"[Could not embed image: {label}]")

            pdf.ln(3)

    pdf.output(output_path)
