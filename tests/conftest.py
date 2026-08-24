"""Shared fixtures for the test suite.

`chat_db` builds a synthetic, in-memory database with the subset of the
chat.db schema our queries touch — no real Messages data involved.
"""

from __future__ import annotations

import sqlite3

import pytest

SCHEMA = """
CREATE TABLE chat (
    ROWID INTEGER PRIMARY KEY,
    guid TEXT,
    display_name TEXT
);
CREATE TABLE handle (
    ROWID INTEGER PRIMARY KEY,
    id TEXT
);
CREATE TABLE chat_handle_join (
    chat_id INTEGER,
    handle_id INTEGER
);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY,
    text TEXT,
    attributedBody BLOB,
    date INTEGER,
    is_from_me INTEGER,
    service TEXT,
    handle_id INTEGER
);
CREATE TABLE chat_message_join (
    chat_id INTEGER,
    message_id INTEGER
);
CREATE TABLE attachment (
    ROWID INTEGER PRIMARY KEY,
    filename TEXT,
    transfer_name TEXT,
    mime_type TEXT,
    is_sticker INTEGER
);
CREATE TABLE message_attachment_join (
    message_id INTEGER,
    attachment_id INTEGER
);
"""


class ChatDbBuilder:
    """Small helper for populating the synthetic schema row by row."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add_chat(self, chat_id: int, guid: str, display_name: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO chat (ROWID, guid, display_name) VALUES (?, ?, ?)",
            (chat_id, guid, display_name),
        )

    def add_handle(self, handle_id: int, identifier: str) -> None:
        self.conn.execute("INSERT INTO handle (ROWID, id) VALUES (?, ?)", (handle_id, identifier))

    def link_chat_handle(self, chat_id: int, handle_id: int) -> None:
        self.conn.execute(
            "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)", (chat_id, handle_id)
        )

    def add_message(
        self,
        msg_id: int,
        chat_id: int,
        date: int,
        is_from_me: bool,
        text: str | None = None,
        attributed_body: bytes | None = None,
        handle_id: int | None = None,
        service: str = "iMessage",
    ) -> None:
        self.conn.execute(
            """INSERT INTO message
               (ROWID, text, attributedBody, date, is_from_me, service, handle_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, text, attributed_body, date, int(is_from_me), service, handle_id),
        )
        self.conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)", (chat_id, msg_id)
        )

    def add_attachment(
        self,
        attachment_id: int,
        message_id: int,
        filename: str,
        transfer_name: str | None = None,
        mime_type: str | None = None,
        is_sticker: bool = False,
    ) -> None:
        self.conn.execute(
            """INSERT INTO attachment (ROWID, filename, transfer_name, mime_type, is_sticker)
               VALUES (?, ?, ?, ?, ?)""",
            (attachment_id, filename, transfer_name, mime_type, int(is_sticker)),
        )
        self.conn.execute(
            "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (?, ?)",
            (message_id, attachment_id),
        )


@pytest.fixture
def chat_db():
    """Yields (conn, builder) for an in-memory synthetic chat.db."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn, ChatDbBuilder(conn)
    conn.close()
