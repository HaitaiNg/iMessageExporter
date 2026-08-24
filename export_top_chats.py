"""
export_top_chats.py — Export the top N chats (by local message count) to
PDF + attachment images.

Each chat gets its own folder:

    <output_dir>/<PhoneNumber>_<EarliestTimestamp>_<LatestTimestamp>/
        chat.pdf
        images/            (photos, videos, and other attachments)

Usage:
    python3 export_top_chats.py
    python3 export_top_chats.py -n 20 -o ~/Desktop/imessage_export
    python3 export_top_chats.py --backup <udid-or-prefix>
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime

from messages_library import DEFAULT_DB, Chat, chat_date_range, connect, list_chats, snapshot_db
from ios_backup import Backup, extract_sms_db, find_backup
from exporters import export_attachments, export_pdf


def sanitize(name: str) -> str:
    """Make a string safe to use as a file/folder name component."""
    name = re.sub(r"[^A-Za-z0-9+_.-]+", "_", name.strip())
    return name.strip("_") or "unknown"


def chat_identifier(chat: Chat) -> str:
    """Best-effort phone number for naming. Falls back to an email
    identifier, then the display name, then the chat guid (group chats
    with no display name are named after their first member)."""
    phone_ids = [i for i in chat.identifiers if i and "@" not in i]
    if phone_ids:
        return phone_ids[0]
    if chat.identifiers:
        return chat.identifiers[0]
    return chat.display_name or chat.guid


def timestamp(dt: datetime | None) -> str:
    return dt.strftime("%Y%m%d-%H%M%S") if dt else "unknown"


def export_top_chats(
    conn,
    backup: Backup | None,
    count: int,
    output_dir: str,
) -> None:
    chats = list_chats(conn)[:count]  # already ordered by message_count DESC
    os.makedirs(output_dir, exist_ok=True)

    for i, chat in enumerate(chats, start=1):
        first, last = chat_date_range(conn, chat.chat_id)
        folder_name = f"{sanitize(chat_identifier(chat))}_{timestamp(first)}_{timestamp(last)}"
        chat_dir = os.path.join(output_dir, folder_name)
        os.makedirs(chat_dir, exist_ok=True)

        print(f"[{i}/{len(chats)}] {chat.label} ({chat.message_count} msgs) -> {chat_dir}")

        pdf_path = os.path.join(chat_dir, "chat.pdf")
        try:
            export_pdf(conn, chat.chat_id, backup, pdf_path, title=chat.label)
        except Exception as e:
            print(f"  ! PDF export failed: {e}", file=sys.stderr)

        images_dir = os.path.join(chat_dir, "images")
        try:
            result = export_attachments(conn, chat.chat_id, backup, images_dir)
            print(f"  images: {result.copied} copied, {result.skipped} skipped")
        except Exception as e:
            print(f"  ! attachment export failed: {e}", file=sys.stderr)

    print(f"\nDone. Exported {len(chats)} chats to {output_dir}/")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the top N chats to PDF + images")
    parser.add_argument("--db", default=DEFAULT_DB, help="path to chat.db (default: live Mac DB)")
    parser.add_argument("--backup", help="read from a local iPhone backup instead (UDID or prefix; see list-backups)")
    parser.add_argument("-n", "--count", type=int, default=20, help="number of top chats to export (default: 20)")
    parser.add_argument("-o", "--output-dir", default="export", help="directory to write exports into (default: ./export)")
    args = parser.parse_args()

    if args.backup:
        backup = find_backup(args.backup)
        db_path = extract_sms_db(backup)
    else:
        backup = None
        db_path = snapshot_db(args.db)

    conn = connect(db_path)
    export_top_chats(conn, backup, args.count, args.output_dir)


if __name__ == "__main__":
    main()
