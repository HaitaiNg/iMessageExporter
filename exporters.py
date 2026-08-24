"""
exporters.py — High-level export operations shared by the CLI and GUI.

Each function takes an open connection + chat_id (+ backup, for attachment
resolution) and performs one export, returning a small result rather than
printing anything — callers decide how to present progress/results.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass

from ios_backup import Backup, resolve_attachment
from messages_library import iter_attachments, iter_messages, iter_messages_full
from pdf_export import build_pdf


def export_text(conn: sqlite3.Connection, chat_id: int, output_path: str | None) -> int:
    """Write text messages to output_path (or stdout if None, for CLI use).
    Returns the number of messages written."""
    out = open(output_path, "w") if output_path else sys.stdout
    written = 0
    try:
        for msg in iter_messages(conn, chat_id):
            if not msg["text"]:
                continue
            who = "Me" if msg["is_from_me"] else (msg["sender"] or "Unknown")
            when = msg["dt"].strftime("%Y-%m-%d %H:%M:%S") if msg["dt"] else "?"
            out.write(f"[{when}] {who}: {msg['text']}\n")
            written += 1
    finally:
        if out is not sys.stdout:
            out.close()
    return written


def unique_filename(seen_names: dict[str, int], name: str) -> str:
    """Disambiguate collisions (e.g. multiple "IMG_1234.HEIC" at the same
    second) by appending an incrementing suffix, keeping the extension."""
    if name not in seen_names:
        seen_names[name] = 0
        return name
    seen_names[name] += 1
    root, ext = os.path.splitext(name)
    return f"{root}_{seen_names[name]}{ext}"


@dataclass
class AttachmentExportResult:
    copied: int
    skipped: int


def export_attachments(
    conn: sqlite3.Connection, chat_id: int, backup: Backup | None, output_dir: str
) -> AttachmentExportResult:
    os.makedirs(output_dir, exist_ok=True)
    copied = 0
    skipped = 0
    seen_names: dict[str, int] = {}

    for att in iter_attachments(conn, chat_id):
        if not att["filename"]:
            skipped += 1
            continue

        who = "me" if att["is_from_me"] else "them"
        when = att["dt"].strftime("%Y%m%d_%H%M%S") if att["dt"] else "unknown"
        base = os.path.basename(att["transfer_name"] or att["filename"])
        name = unique_filename(seen_names, f"{when}_{who}_{base}")
        dest = os.path.join(output_dir, name)

        if resolve_attachment(att["filename"], backup, dest):
            copied += 1
        else:
            skipped += 1

    return AttachmentExportResult(copied=copied, skipped=skipped)


def export_pdf(
    conn: sqlite3.Connection, chat_id: int, backup: Backup | None, output_path: str, title: str
) -> None:
    build_pdf(iter_messages_full(conn, chat_id), backup, output_path, title=title)
