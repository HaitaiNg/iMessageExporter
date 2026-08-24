from __future__ import annotations

import os
from datetime import datetime

import pytest
from fpdf import FPDF
from PIL import Image

import pdf_export
from pdf_export import FONT_FAMILY, FONT_FILE, _line, _place_image, _resolve_attachment_path, build_pdf

pytestmark = pytest.mark.skipif(
    not os.path.isfile(FONT_FILE),
    reason=f"Unicode font not present at {FONT_FILE!r} on this machine",
)


def _new_pdf(page_width: float = 180) -> FPDF:
    pdf = FPDF(format="A4")
    pdf.add_font(FONT_FAMILY, "", FONT_FILE)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    pdf.set_font(FONT_FAMILY, "", 10)
    return pdf


# ---------------------------------------------------------------------------
# _line — regression test for the cursor-drift bug (multi_cell's default
# leaves x at the cell's right edge, pushing later content off the page)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "_, text",
    [
        ("short single-line text", "hello"),
        ("long text that wraps across multiple lines", "word " * 100),
        ("unicode text", "héllo 你好 مرحبا"),
    ],
)
def test_line_returns_cursor_to_left_margin(_, text):
    pdf = _new_pdf()
    _line(pdf, 180, text)
    assert pdf.get_x() == pdf.l_margin


def test_line_called_repeatedly_does_not_drift_right():
    pdf = _new_pdf()
    for i in range(20):
        _line(pdf, 180, f"line {i}")
    assert pdf.get_x() == pdf.l_margin


# ---------------------------------------------------------------------------
# _place_image
# ---------------------------------------------------------------------------


def test_place_image_starts_at_left_margin(tmp_path):
    img_path = tmp_path / "photo.png"
    Image.new("RGB", (800, 400), color=(100, 150, 200)).save(img_path)

    pdf = _new_pdf()
    _place_image(pdf, str(img_path), page_width=180)
    assert pdf.get_x() == pdf.l_margin


def test_place_image_triggers_page_break_when_it_would_overflow(tmp_path):
    img_path = tmp_path / "photo.png"
    Image.new("RGB", (800, 800), color=(100, 150, 200)).save(img_path)  # square -> tall at full width

    pdf = _new_pdf()
    pdf.set_y(pdf.page_break_trigger - 5)  # leave almost no room on the current page
    page_before = pdf.page_no()
    _place_image(pdf, str(img_path), page_width=180)
    assert pdf.page_no() == page_before + 1


# ---------------------------------------------------------------------------
# _resolve_attachment_path
# ---------------------------------------------------------------------------


def test_resolve_attachment_path_no_backup_existing_file(tmp_path):
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"fake jpeg")
    assert _resolve_attachment_path(str(f), backup=None, tmpdir=str(tmp_path)) == str(f)


def test_resolve_attachment_path_no_backup_missing_file(tmp_path):
    missing = tmp_path / "nope.jpg"
    assert _resolve_attachment_path(str(missing), backup=None, tmpdir=str(tmp_path)) is None


# ---------------------------------------------------------------------------
# build_pdf — end-to-end smoke tests
# ---------------------------------------------------------------------------


def test_build_pdf_missing_font_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_export, "FONT_FILE", str(tmp_path / "does-not-exist.ttf"))
    out = tmp_path / "out.pdf"
    with pytest.raises(RuntimeError, match="Unicode font not found"):
        build_pdf([], backup=None, output_path=str(out))


def test_build_pdf_writes_a_nonempty_file(tmp_path):
    messages = [
        {"dt": datetime(2024, 1, 1, 10, 0, 0), "is_from_me": True, "sender": "Me", "text": "hi", "attachments": []},
        {"dt": datetime(2024, 1, 1, 10, 1, 0), "is_from_me": False, "sender": "them", "text": "hey", "attachments": []},
    ]
    out = tmp_path / "out.pdf"
    build_pdf(messages, backup=None, output_path=str(out), title="Test Chat")
    assert out.exists()
    assert out.stat().st_size > 0


def test_build_pdf_skips_messages_with_no_text_and_no_attachments(tmp_path):
    # A tapback/reaction-only message has neither text nor attachments —
    # should be silently dropped rather than producing an empty block.
    messages = [
        {"dt": datetime(2024, 1, 1, 10, 0, 0), "is_from_me": True, "sender": "Me", "text": None, "attachments": []},
    ]
    out = tmp_path / "out.pdf"
    build_pdf(messages, backup=None, output_path=str(out))
    assert out.exists()  # doesn't raise, just produces a near-empty PDF


def test_build_pdf_embeds_real_image_and_placeholders_missing_one(tmp_path):
    img_path = tmp_path / "photo.png"
    Image.new("RGB", (400, 300), color=(0, 200, 0)).save(img_path)

    messages = [
        {
            "dt": datetime(2024, 1, 1, 10, 0, 0),
            "is_from_me": False,
            "sender": "them",
            "text": None,
            "attachments": [{"filename": str(img_path), "transfer_name": "photo.png", "mime_type": "image/png"}],
        },
        {
            "dt": datetime(2024, 1, 1, 10, 1, 0),
            "is_from_me": False,
            "sender": "them",
            "text": None,
            "attachments": [
                {"filename": "/nowhere/gone.jpg", "transfer_name": "gone.jpg", "mime_type": "image/jpeg"}
            ],
        },
        {
            "dt": datetime(2024, 1, 1, 10, 2, 0),
            "is_from_me": False,
            "sender": "them",
            "text": None,
            "attachments": [
                {"filename": "/nowhere/clip.mov", "transfer_name": "clip.mov", "mime_type": "video/quicktime"}
            ],
        },
    ]
    out = tmp_path / "out.pdf"
    build_pdf(messages, backup=None, output_path=str(out))
    assert out.exists()
    assert out.stat().st_size > 0
