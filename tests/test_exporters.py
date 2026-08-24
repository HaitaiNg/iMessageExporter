from __future__ import annotations

import os

import pytest

from exporters import export_attachments, export_text, unique_filename


@pytest.mark.parametrize(
    "_, seen_before, name, expected",
    [
        ("first time seeing a name, unchanged", {}, "IMG_1234.HEIC", "IMG_1234.HEIC"),
        ("second occurrence gets _1 suffix before the extension", {"IMG_1234.HEIC": 0}, "IMG_1234.HEIC", "IMG_1234_1.HEIC"),
        ("third occurrence gets _2", {"IMG_1234.HEIC": 1}, "IMG_1234.HEIC", "IMG_1234_2.HEIC"),
    ],
)
def test_unique_filename(_, seen_before, name, expected):
    seen = dict(seen_before)
    assert unique_filename(seen, name) == expected


def test_export_text_writes_file_and_returns_count(chat_db, tmp_path):
    conn, db = chat_db
    db.add_chat(1, "chat-guid-1")
    db.add_message(100, chat_id=1, date=1000, is_from_me=True, text="hi")
    db.add_message(101, chat_id=1, date=2000, is_from_me=False, text=None)  # no text -> not counted
    conn.commit()

    out = tmp_path / "out.txt"
    count = export_text(conn, 1, str(out))
    assert count == 1
    assert "hi" in out.read_text()


def test_export_attachments_copies_and_dedupes_filenames(chat_db, tmp_path):
    conn, db = chat_db
    src = tmp_path / "src"
    src.mkdir()
    photo = src / "photo.jpg"
    photo.write_bytes(b"fake jpeg")

    db.add_chat(1, "chat-guid-1")
    db.add_message(100, chat_id=1, date=1000, is_from_me=True, text=None)
    db.add_attachment(1000, message_id=100, filename=str(photo), mime_type="image/jpeg")
    db.add_message(101, chat_id=1, date=1000, is_from_me=True, text=None)  # same timestamp -> collision
    db.add_attachment(1001, message_id=101, filename=str(photo), mime_type="image/jpeg")
    conn.commit()

    out_dir = tmp_path / "out"
    result = export_attachments(conn, 1, backup=None, output_dir=str(out_dir))

    assert result.copied == 2
    assert result.skipped == 0
    assert len(os.listdir(out_dir)) == 2  # collision was disambiguated, not overwritten
