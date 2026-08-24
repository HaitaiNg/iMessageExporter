"""
Regression coverage for gui_app.py's threading plumbing.

The bug this guards against: sqlite3 connections are pinned to the thread
that created them. `_load_source` and `_on_export_clicked` each spawn their
*own* background thread (via `_run_async`), so a connection opened during
load can't be reused during a later export — it has to be a fresh
`connect(db_path)` call in whichever thread actually needs it. This test
builds a real on-disk chat.db-shaped file and drives the actual GUI code
path end to end (not just inspecting the code) so a regression here fails
loudly.

`ExportApp.__init__` kicks off a real background load of the live Mac
chat.db immediately on construction, so `snapshot_db` is patched to target
our synthetic file *before* constructing the app — otherwise that real load
completes later and races with (and can silently overwrite) whatever state
the test sets up, independent of any bug in the app itself.
"""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

import gui_app

_SCHEMA = """
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, date INTEGER,
    is_from_me INTEGER, service TEXT, handle_id INTEGER
);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE attachment (
    ROWID INTEGER PRIMARY KEY, filename TEXT, transfer_name TEXT,
    mime_type TEXT, is_sticker INTEGER
);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""


@pytest.fixture
def temp_chat_db(tmp_path):
    """A real on-disk sqlite file (not :memory:) with one chat + one text
    message — needed because the whole point here is exercising real
    cross-thread connection reopening, which :memory: can't do."""
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO chat (ROWID, guid, display_name) VALUES (1, 'chat-guid-1', 'Test Chat')")
    conn.execute(
        "INSERT INTO message (ROWID, text, date, is_from_me) VALUES (100, 'hello world', 1000, 1)"
    )
    conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 100)")
    conn.commit()
    conn.close()
    return str(db_path)


def _pump(app: "gui_app.ExportApp", predicate, timeout_s: float = 5.0) -> None:
    """Drive the Tk event loop (so .after()-queued callbacks actually run)
    until `predicate()` is true or we time out."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        app.update()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out; last status was {app.status_var.get()!r}")


def test_export_after_load_does_not_hit_sqlite_thread_affinity_error(temp_chat_db, tmp_path, monkeypatch):
    # Redirect the automatic startup load (triggered inside __init__) to our
    # synthetic DB instead of the real live Mac chat.db.
    monkeypatch.setattr(gui_app, "snapshot_db", lambda: temp_chat_db)

    output_path = str(tmp_path / "out.txt")
    monkeypatch.setattr(gui_app.filedialog, "asksaveasfilename", lambda **kwargs: output_path)
    errors = []
    monkeypatch.setattr(gui_app.messagebox, "showerror", lambda title, message: errors.append(message))
    monkeypatch.setattr(gui_app.messagebox, "showinfo", lambda title, message: None)

    app = gui_app.ExportApp()
    try:
        _pump(app, lambda: app.status_var.get().endswith("chats loaded."))
        assert app.status_var.get() == "1 chats loaded."

        app.chat_listbox.selection_set(0)
        app.format_var.set(gui_app.FORMAT_TEXT)
        app._on_export_clicked()  # spawns a *second*, different background thread than the load did

        _pump(app, lambda: app.status_var.get() in ("Done.", "Export failed."))

        assert errors == [], f"export raised: {errors}"
        assert app.status_var.get() == "Done."
        assert os.path.isfile(output_path)
        assert "hello world" in open(output_path).read()
    finally:
        app.destroy()
