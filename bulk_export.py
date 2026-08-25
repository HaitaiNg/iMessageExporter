"""
bulk_export.py — one-off batch export: PDF + attachments for the top N
chats (by message count) from a chat.db or iPhone backup.

Not part of the installed CLI — this is a throwaway script for a specific
one-time job, reusing the same exporters.py functions the CLI/GUI use.

Usage:
    python3 bulk_export.py --backup <udid-or-prefix> --top 30 -o ~/Desktop/imessage_backup_export
    python3 bulk_export.py --top 30 -o ~/Desktop/imessage_backup_export   # live Mac DB instead
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from exporters import export_attachments, export_pdf
from ios_backup import find_backup, extract_sms_db
from messages_library import DEFAULT_DB, connect, list_chats, snapshot_db


def _safe_folder_name(label: str, chat_id: int) -> str:
    name = re.sub(r"[^\w\s.@-]", "_", label).strip()
    name = re.sub(r"\s+", "_", name)
    name = name[:80] or "chat"
    return f"{chat_id:04d}_{name}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk export PDF + attachments for the top N chats")
    parser.add_argument("--db", default=DEFAULT_DB, help="path to chat.db (default: live Mac DB)")
    parser.add_argument("--backup", help="read from a local iPhone backup instead (UDID or prefix)")
    parser.add_argument("--top", type=int, default=30, help="number of chats, by message count (default: 30)")
    parser.add_argument("-o", "--output-dir", required=True)
    args = parser.parse_args()

    if args.backup:
        backup = find_backup(args.backup)
        print(f"Extracting sms.db from backup {backup.udid}...")
        db_path = extract_sms_db(backup)
    else:
        backup = None
        db_path = snapshot_db(args.db)

    conn = connect(db_path)
    chats = list_chats(conn)[: args.top]

    out_root = os.path.expanduser(args.output_dir)
    os.makedirs(out_root, exist_ok=True)
    print(f"Exporting {len(chats)} chats to {out_root}\n")

    for i, chat in enumerate(chats, 1):
        folder = os.path.join(out_root, _safe_folder_name(chat.label, chat.chat_id))
        os.makedirs(folder, exist_ok=True)
        print(f"[{i}/{len(chats)}] chat {chat.chat_id}: {chat.label} ({chat.message_count} msgs)")

        pdf_path = os.path.join(folder, "conversation.pdf")
        try:
            export_pdf(conn, chat.chat_id, backup, pdf_path, title=chat.label)
            print(f"    PDF: wrote {pdf_path}")
        except Exception as e:
            print(f"    PDF FAILED: {e}", file=sys.stderr)

        att_dir = os.path.join(folder, "attachments")
        try:
            result = export_attachments(conn, chat.chat_id, backup, att_dir)
            print(f"    attachments: {result.copied} copied, {result.skipped} skipped")
        except Exception as e:
            print(f"    ATTACHMENTS FAILED: {e}", file=sys.stderr)

    print("\nDone.")


if __name__ == "__main__":
    main()
