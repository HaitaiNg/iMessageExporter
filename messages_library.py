"""
Ported over from : https://github.com/rnorlund/imessage-relationship-analytics/blob/main/messages_lib.py 

messages_lib.py — Core utilities for reading the macOS Messages database.

The Messages app stores data in a local SQLite DB at ~/Library/Messages/chat.db.

  * Safely snapshotting the live DB (it has WAL/-shm sidecar files).
  * Apple's timestamp format (nanoseconds since 2001-01-01 UTC).
  * Decoding message text from the binary `attributedBody` blob, which is
    where modern macOS stores text when the plain `text` column is NULL.
  * Resolving handles (phone numbers / emails) and chat membership.

"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import groupby

# Apple epoch: 2001-01-01 00:00:00 UTC
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

DEFAULT_DB = os.path.expanduser("~/Library/Messages/chat.db")


def snapshot_db(src: str = DEFAULT_DB) -> str:
    """Copy the live chat.db (plus WAL/-shm sidecars) to a temp file.

    Messages keeps the DB open in WAL mode, so recent messages live in
    chat.db-wal until checkpointed. We copy all three so the snapshot is
    complete and we never touch the original.

    Returns the path to the temp copy. Caller is responsible for cleanup
    (or just let the OS clear /tmp).
    """
    tmpdir = tempfile.mkdtemp(prefix="msgexport_")
    dst = os.path.join(tmpdir, "chat.db")
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = src + suffix
        if os.path.exists(side):
            shutil.copy2(side, dst + suffix)
    return dst


def connect(db_path: str) -> sqlite3.Connection:
    """Open a read-only connection to a chat.db (or iOS backup sms.db) copy.

    These databases are written in WAL journal mode. If a "-wal" sidecar was
    copied alongside db_path (snapshot_db always does this for the live Mac
    DB, since that's exactly where the newest not-yet-checkpointed messages
    live), we connect normally so SQLite reads it. But an iOS backup's sms.db
    is often checkpointed on-device before the backup runs, so no sidecar
    exists to copy — and a bare mode=ro connection to a WAL-mode file with no
    sidecar fails ("unable to open database file"), because SQLite can't
    create the "-shm" file it needs without write access. immutable=1 tells
    SQLite to skip that machinery and just read the file directly, which is
    only safe when there's no pending WAL data to worry about missing.
    """
    immutable = "" if os.path.exists(db_path + "-wal") else "&immutable=1"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro{immutable}", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def apple_time_to_dt(raw: int) -> datetime | None:
    """Convert a Messages `date` value to a timezone-aware datetime.

    Modern macOS stores nanoseconds since the Apple epoch; very old DBs
    stored seconds. Detect by magnitude.
    """
    if not raw:
        return None
    # Nanosecond values are ~10^18; second values are ~10^9.
    seconds = raw / 1e9 if raw > 1e11 else raw
    return APPLE_EPOCH + timedelta(seconds=seconds)


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract plain text from a serialized NSAttributedString blob.

    This is the typedstream/NSArchiver format. We use the well-established
    heuristic: the message text is an inline byte string that follows the
    "NSString" class marker and a '+' (0x2b) byte, with a length prefix.
    Lengths >= 128 use a 0x81 + 2-byte-little-endian encoding.
    """
    if not blob:
        return None
    try:
        marker = blob.find(b"NSString")
        if marker == -1:
            return None
        plus = blob.find(b"\x2b", marker)  # '+' signals an inline string
        if plus == -1:
            return None
        i = plus + 1
        length = blob[i]
        if length == 0x81:  # extended length: next 2 bytes, little-endian
            length = int.from_bytes(blob[i + 1 : i + 3], "little")
            i += 3
        else:
            i += 1
        return blob[i : i + length].decode("utf-8", errors="replace")
    except Exception:
        return None


def message_text(row: sqlite3.Row) -> str | None:
    """Best-effort text for a message row: plain column first, then blob.

    Strips U+FFFC (object-replacement char) used as an attachment placeholder;
    returns None if nothing meaningful remains.
    """
    txt = row["text"] or decode_attributed_body(row["attributedBody"])
    if not txt:
        return None
    txt = txt.replace("￼", "").strip()
    return txt or None


@dataclass
class Chat:
    chat_id: int
    guid: str
    display_name: str
    identifiers: tuple[str, ...]  # phone numbers / emails in the chat
    message_count: int

    @property
    def label(self) -> str:
        if self.display_name:
            return self.display_name
        return ", ".join(self.identifiers) or self.guid


def _row_to_chat(r: sqlite3.Row) -> Chat:
    ids = tuple((r["identifiers"] or "").split(",")) if r["identifiers"] else ()
    return Chat(
        chat_id=r["chat_id"],
        guid=r["guid"],
        display_name=r["display_name"],
        identifiers=ids,
        message_count=r["msg_count"],
    )


def list_chats(conn: sqlite3.Connection) -> list[Chat]:
    """Return all chats with their member identifiers and message counts."""
    rows = conn.execute(
        """
        SELECT c.ROWID AS chat_id,
               c.guid AS guid,
               COALESCE(c.display_name, '') AS display_name,
               GROUP_CONCAT(DISTINCT h.id) AS identifiers,
               COUNT(DISTINCT cmj.message_id) AS msg_count
        FROM chat c
        LEFT JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
        LEFT JOIN handle h ON h.ROWID = chj.handle_id
        LEFT JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        GROUP BY c.ROWID
        ORDER BY msg_count DESC
        """
    ).fetchall()
    return [_row_to_chat(r) for r in rows]


def normalize_phone(s: str) -> str:
    """Reduce a phone/email identifier to digits (or lowercased email) for matching."""
    s = s.strip().lower()
    if "@" in s:
        return s
    return "".join(ch for ch in s if ch.isdigit())


def _identifiers_match(target: str, member: str) -> bool:
    """True if two *already-normalized* identifiers refer to the same
    phone/email. Emails must match exactly; phone numbers match if either
    is a suffix of the other (handles +1-style country-code prefixes)."""
    if "@" in target:
        return member == target
    return bool(member) and (member.endswith(target) or target.endswith(member))


def find_chats_for_identifier(conn: sqlite3.Connection, identifier: str) -> list[Chat]:
    """Find chats whose members match a phone number or email.

    Matches loosely: phone numbers compared by trailing digits, emails by
    exact (case-insensitive) match.
    """
    target = normalize_phone(identifier)
    return [
        chat
        for chat in list_chats(conn)
        if any(_identifiers_match(target, normalize_phone(m)) for m in chat.identifiers)
    ]


def iter_messages(conn: sqlite3.Connection, chat_id: int):
    """Yield messages for a chat in chronological order.

    Each yielded dict has: dt (datetime), is_from_me (bool), sender (str),
    text (str), service (iMessage/SMS).
    """
    rows = conn.execute(
        """
        SELECT m.ROWID, m.text, m.attributedBody, m.date,
               m.is_from_me, m.service,
               COALESCE(h.id, '') AS sender_id
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id = ?
        ORDER BY m.date ASC
        """,
        (chat_id,),
    )
    for r in rows:
        yield {
            "dt": apple_time_to_dt(r["date"]),
            "is_from_me": bool(r["is_from_me"]),
            "sender": r["sender_id"],
            "text": message_text(r),
            "service": r["service"],
        }


def iter_attachments(conn: sqlite3.Connection, chat_id: int):
    """Yield attachment metadata for a chat in chronological order.

    Each yielded dict has: dt, is_from_me, sender, filename (the on-disk
    path as stored in the DB — '~/Library/...' on the live Mac DB, or a
    backup-relative path when reading from an iOS backup), transfer_name
    (original filename), mime_type, is_sticker.
    """
    rows = conn.execute(
        """
        SELECT m.date, m.is_from_me, COALESCE(h.id, '') AS sender_id,
               a.filename, a.transfer_name, a.mime_type, a.is_sticker
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN message_attachment_join maj ON maj.message_id = m.ROWID
        JOIN attachment a ON a.ROWID = maj.attachment_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id = ?
        ORDER BY m.date ASC
        """,
        (chat_id,),
    )
    for r in rows:
        yield {
            "dt": apple_time_to_dt(r["date"]),
            "is_from_me": bool(r["is_from_me"]),
            "sender": r["sender_id"],
            "filename": r["filename"],
            "transfer_name": r["transfer_name"],
            "mime_type": r["mime_type"],
            "is_sticker": bool(r["is_sticker"]),
        }


def _row_to_attachment_meta(r: sqlite3.Row) -> dict:
    return {
        "filename": r["att_filename"],
        "transfer_name": r["att_transfer_name"],
        "mime_type": r["att_mime_type"],
    }


def iter_messages_full(conn: sqlite3.Connection, chat_id: int):
    """Yield each message with its text AND any attachments, merged in
    chronological order — one dict per message (not per attachment row).

    Each dict: dt, is_from_me, sender, text, attachments (list of dicts with
    filename/transfer_name/mime_type).
    """
    rows = conn.execute(
        """
        SELECT m.ROWID AS msg_id, m.text, m.attributedBody, m.date,
               m.is_from_me, COALESCE(h.id, '') AS sender_id,
               a.filename AS att_filename, a.transfer_name AS att_transfer_name,
               a.mime_type AS att_mime_type
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        LEFT JOIN message_attachment_join maj ON maj.message_id = m.ROWID
        LEFT JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE cmj.chat_id = ?
        ORDER BY m.date ASC, m.ROWID ASC
        """,
        (chat_id,),
    )
    # A message with N attachments produces N joined rows sharing one
    # msg_id; the ORDER BY guarantees those rows are contiguous, so
    # groupby can collapse them back into one entry per message.
    for _msg_id, group in groupby(rows, key=lambda r: r["msg_id"]):
        group_rows = list(group)
        first = group_rows[0]
        yield {
            "dt": apple_time_to_dt(first["date"]),
            "is_from_me": bool(first["is_from_me"]),
            "sender": first["sender_id"],
            "text": message_text(first),
            "attachments": [_row_to_attachment_meta(r) for r in group_rows if r["att_filename"]],
        }


def chat_message_count(conn: sqlite3.Connection, chat_id: int) -> int:
    """Count of messages locally stored for a chat."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM chat_message_join WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    return row["n"]


def chat_date_range(conn: sqlite3.Connection, chat_id: int) -> tuple[datetime | None, datetime | None]:
    """Return (earliest, latest) message datetimes locally stored for a chat.

    Useful for spotting a chat.db that's missing older history (e.g. due to
    "Keep Messages" retention settings or messages not yet synced down from
    iCloud).
    """
    row = conn.execute(
        """
        SELECT MIN(m.date) AS first_date, MAX(m.date) AS last_date
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        WHERE cmj.chat_id = ?
        """,
        (chat_id,),
    ).fetchone()
    return apple_time_to_dt(row["first_date"]), apple_time_to_dt(row["last_date"])