from __future__ import annotations

import os
import sqlite3
from datetime import timedelta

import pytest

import messages_library
from messages_library import (
    APPLE_EPOCH,
    apple_time_to_dt,
    chat_date_range,
    connect,
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


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


def _make_wal_mode_db_without_sidecar(path: str) -> None:
    """Build a real on-disk sqlite file whose header declares journal_mode
    WAL but that has no accompanying -wal/-shm sidecar file — exactly what
    an iOS backup's sms.db looks like when it was checkpointed on-device
    before the backup ran (the sidecar never existed to copy). A bare
    mode=ro connection to a file in this state fails with "unable to open
    database file", since SQLite needs write access to create a -shm file
    it can't create in read-only mode; see connect()'s immutable=1 logic.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE chat (ROWID INTEGER PRIMARY KEY)")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = path + suffix
        if os.path.exists(sidecar):
            os.remove(sidecar)


def test_connect_uri_includes_immutable_when_no_wal_sidecar(tmp_path, monkeypatch):
    """Verifies connect()'s actual decision logic directly, by capturing the
    URI it builds, rather than relying on it causing an observable open
    failure — that failure turns out to be specific to the SQLite build
    linked into macOS system Python (3.51.0 here); the same reproduction
    doesn't fail at all under a newer/differently-built SQLite (e.g.
    Homebrew's 3.53.4, what the project's .venv uses), so a test that only
    checks "does opening succeed" can't reliably guard this regression."""
    db_path = str(tmp_path / "sms.db")  # connect() only checks for a "-wal" sidecar; the file needn't exist
    captured = {}
    real_connect = sqlite3.connect  # patching messages_library.sqlite3.connect below patches the
                                     # same module-global sqlite3.connect everywhere, so grab this first

    def fake_connect(database, **kwargs):
        captured["uri"] = database
        return real_connect(":memory:")

    monkeypatch.setattr(messages_library.sqlite3, "connect", fake_connect)
    messages_library.connect(db_path)
    assert "immutable=1" in captured["uri"]


def test_connect_uri_omits_immutable_when_wal_sidecar_present(tmp_path, monkeypatch):
    db_path = str(tmp_path / "chat.db")
    open(db_path + "-wal", "wb").close()
    captured = {}
    real_connect = sqlite3.connect

    def fake_connect(database, **kwargs):
        captured["uri"] = database
        return real_connect(":memory:")

    monkeypatch.setattr(messages_library.sqlite3, "connect", fake_connect)
    messages_library.connect(db_path)
    assert "immutable=1" not in captured["uri"]


def test_connect_opens_wal_mode_db_with_no_sidecar(tmp_path):
    db_path = str(tmp_path / "sms.db")
    _make_wal_mode_db_without_sidecar(db_path)
    assert not os.path.exists(db_path + "-wal")  # sanity: reproducing the real scenario

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM chat").fetchone()[0] == 0


def test_connect_does_not_use_immutable_when_wal_sidecar_present(tmp_path):
    """The live Mac chat.db snapshot always brings its -wal sidecar along
    (that's where the newest not-yet-checkpointed messages live) — connect()
    must NOT force immutable=1 in that case, or it would silently ignore
    that pending data. This just confirms the sidecar-present path still
    opens and reads correctly (immutable mode would also "work" for a
    trivial read, so this isn't an airtight behavioral proof, but it does
    guard against connect() raising on the common, most important case).

    A -wal file only persists on disk while some connection still holds the
    database open in WAL mode (SQLite auto-checkpoints and removes it once
    the last connection closes) — so, matching how Messages.app actually
    behaves (holding chat.db open continuously), we keep a writer connection
    open here rather than closing it before checking.
    """
    db_path = str(tmp_path / "chat.db")
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE chat (ROWID INTEGER PRIMARY KEY)")
        writer.execute("INSERT INTO chat (ROWID) VALUES (1)")
        writer.commit()
        assert os.path.exists(db_path + "-wal")

        result = connect(db_path)
        assert result.execute("SELECT COUNT(*) FROM chat").fetchone()[0] == 1
    finally:
        writer.close()
