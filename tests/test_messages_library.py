from __future__ import annotations

from datetime import timedelta

import pytest

from messages_library import (
    APPLE_EPOCH,
    apple_time_to_dt,
    chat_date_range,
    decode_attributed_body,
    find_chats_for_identifier,
    iter_attachments,
    iter_messages,
    iter_messages_full,
    list_chats,
    message_text,
    normalize_phone,
)


def _typedstream_blob(text: str, extended: bool = False) -> bytes:
    """Build a minimal fake blob matching what decode_attributed_body scans
    for: an "NSString" marker, a '+' inline-string flag, a length prefix,
    then the UTF-8 text."""
    text_bytes = text.encode("utf-8")
    if extended:
        length_prefix = b"\x81" + len(text_bytes).to_bytes(2, "little")
    else:
        length_prefix = bytes([len(text_bytes)])
    return b"NSString" + b"\x2b" + length_prefix + text_bytes


# ---------------------------------------------------------------------------
# apple_time_to_dt
# ---------------------------------------------------------------------------


def test_apple_time_to_dt_zero_means_unset():
    assert apple_time_to_dt(0) is None


@pytest.mark.parametrize(
    "_, raw, expected_seconds_since_epoch",
    [
        ("legacy DB stores raw seconds since 2001 epoch", 500_000_000, 500_000_000),
        ("modern macOS stores nanoseconds since 2001 epoch", 500_000_000 * 1_000_000_000, 500_000_000),
        ("just after epoch, seconds form", 1, 1),
    ],
)
def test_apple_time_to_dt(_, raw, expected_seconds_since_epoch):
    assert apple_time_to_dt(raw) == APPLE_EPOCH + timedelta(seconds=expected_seconds_since_epoch)


def test_apple_time_to_dt_nanoseconds_vs_seconds_threshold():
    # Same instant (1000 seconds after epoch), expressed in both encodings,
    # must resolve to the same datetime.
    seconds_form = apple_time_to_dt(1000)
    nanosecond_form = apple_time_to_dt(1000 * 1_000_000_000)
    assert seconds_form == nanosecond_form


# ---------------------------------------------------------------------------
# decode_attributed_body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "_, blob, expected",
    [
        ("none blob returns none", None, None),
        ("empty blob returns none", b"", None),
        ("no NSString marker returns none", b"totally unrelated bytes", None),
        ("NSString marker with no plus byte returns none", b"NSString" + b"no plus here at all", None),
        ("truncated right after plus byte returns none", b"NSString" + b"\x2b", None),
    ],
)
def test_decode_attributed_body_bails_gracefully(_, blob, expected):
    assert decode_attributed_body(blob) == expected


@pytest.mark.parametrize(
    "_, text",
    [
        ("short plain-ascii text", "Hello there!"),
        ("unicode text", "héllo 你好"),
    ],
)
def test_decode_attributed_body_short_form(_, text):
    blob = _typedstream_blob(text)
    assert decode_attributed_body(blob) == text


def test_decode_attributed_body_extended_length_form():
    text = "x" * 200  # forces the 0x81 + 2-byte-length encoding
    blob = _typedstream_blob(text, extended=True)
    assert decode_attributed_body(blob) == text


# ---------------------------------------------------------------------------
# message_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "_, row, expected",
    [
        ("plain text column wins over attributedBody", {"text": "hi", "attributedBody": None}, "hi"),
        (
            "falls back to attributedBody when text column is empty",
            {"text": None, "attributedBody": _typedstream_blob("from blob")},
            "from blob",
        ),
        (
            "object-replacement char (attachment placeholder) stripped",
            {"text": "look ￼ at this", "attributedBody": None},
            "look  at this",
        ),
        (
            "text that is only an attachment placeholder yields none",
            {"text": "￼", "attributedBody": None},
            None,
        ),
        ("nothing usable anywhere yields none", {"text": None, "attributedBody": None}, None),
    ],
)
def test_message_text(_, row, expected):
    assert message_text(row) == expected


# ---------------------------------------------------------------------------
# normalize_phone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "_, raw, expected",
    [
        ("email lowercased, untouched otherwise", "Person@Example.com", "person@example.com"),
        ("phone number reduced to digits only", "+1 (555) 123-4567", "15551234567"),
        ("phone number with no formatting", "5551234567", "5551234567"),
    ],
)
def test_normalize_phone(_, raw, expected):
    assert normalize_phone(raw) == expected


# ---------------------------------------------------------------------------
# DB-query-backed functions
# ---------------------------------------------------------------------------


def test_list_chats_labels_and_counts(chat_db):
    conn, db = chat_db
    db.add_chat(1, "chat-guid-1", display_name="Book Club")
    db.add_handle(10, "+15551234567")
    db.link_chat_handle(1, 10)
    db.add_message(100, chat_id=1, date=1000, is_from_me=True, text="hi")
    db.add_message(101, chat_id=1, date=2000, is_from_me=False, text="hey", handle_id=10)

    db.add_chat(2, "chat-guid-2")  # no display name -> falls back to identifiers
    db.add_handle(20, "friend@example.com")
    db.link_chat_handle(2, 20)
    db.add_message(200, chat_id=2, date=1500, is_from_me=True, text="yo")
    conn.commit()

    chats = list_chats(conn)
    by_id = {c.chat_id: c for c in chats}

    assert by_id[1].label == "Book Club"
    assert by_id[1].message_count == 2
    assert by_id[2].label == "friend@example.com"
    assert by_id[2].message_count == 1


@pytest.mark.parametrize(
    "_, identifier, expect_chat_ids",
    [
        ("matches by trailing digits, ignoring country code", "5551234567", [1]),
        ("matches full number with formatting", "+1 (555) 123-4567", [1]),
        ("matches email case-insensitively", "FRIEND@example.com", [2]),
        ("no match returns empty list", "+19998887777", []),
    ],
)
def test_find_chats_for_identifier(chat_db, _, identifier, expect_chat_ids):
    conn, db = chat_db
    db.add_chat(1, "chat-guid-1")
    db.add_handle(10, "+15551234567")
    db.link_chat_handle(1, 10)
    db.add_message(100, chat_id=1, date=1000, is_from_me=True, text="hi")

    db.add_chat(2, "chat-guid-2")
    db.add_handle(20, "friend@example.com")
    db.link_chat_handle(2, 20)
    db.add_message(200, chat_id=2, date=1000, is_from_me=True, text="hi")
    conn.commit()

    matches = find_chats_for_identifier(conn, identifier)
    assert sorted(c.chat_id for c in matches) == expect_chat_ids


def test_iter_messages_order_and_fields(chat_db):
    conn, db = chat_db
    db.add_chat(1, "chat-guid-1")
    db.add_handle(10, "+15551234567")
    db.add_message(100, chat_id=1, date=2000, is_from_me=False, text="second", handle_id=10)
    db.add_message(101, chat_id=1, date=1000, is_from_me=True, text="first")
    conn.commit()

    messages = list(iter_messages(conn, 1))
    assert [m["text"] for m in messages] == ["first", "second"]
    assert messages[0]["is_from_me"] is True
    assert messages[1]["sender"] == "+15551234567"


def test_iter_attachments_for_chat(chat_db):
    conn, db = chat_db
    db.add_chat(1, "chat-guid-1")
    db.add_message(100, chat_id=1, date=1000, is_from_me=True, text=None)
    db.add_attachment(1000, message_id=100, filename="~/Library/Messages/Attachments/a.jpg", mime_type="image/jpeg")
    conn.commit()

    atts = list(iter_attachments(conn, 1))
    assert len(atts) == 1
    assert atts[0]["filename"] == "~/Library/Messages/Attachments/a.jpg"
    assert atts[0]["mime_type"] == "image/jpeg"


def test_iter_messages_full_groups_multiple_attachments_under_one_message(chat_db):
    conn, db = chat_db
    db.add_chat(1, "chat-guid-1")
    db.add_message(100, chat_id=1, date=1000, is_from_me=True, text="photos incoming")
    db.add_attachment(1000, message_id=100, filename="a.jpg", mime_type="image/jpeg")
    db.add_attachment(1001, message_id=100, filename="b.jpg", mime_type="image/jpeg")
    db.add_message(101, chat_id=1, date=2000, is_from_me=False, text="text only, no attachments")
    conn.commit()

    messages = list(iter_messages_full(conn, 1))
    assert len(messages) == 2
    assert messages[0]["text"] == "photos incoming"
    assert len(messages[0]["attachments"]) == 2
    assert {a["filename"] for a in messages[0]["attachments"]} == {"a.jpg", "b.jpg"}
    assert messages[1]["attachments"] == []


def test_chat_date_range(chat_db):
    conn, db = chat_db
    db.add_chat(1, "chat-guid-1")
    db.add_message(100, chat_id=1, date=1_000_000_000, is_from_me=True, text="early")
    db.add_message(101, chat_id=1, date=2_000_000_000, is_from_me=True, text="late")
    conn.commit()

    first, last = chat_date_range(conn, 1)
    assert first < last
