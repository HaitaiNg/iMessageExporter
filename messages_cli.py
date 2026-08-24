"""
messages_cli.py — Small command-line front end for messages_library.py.

Usage:
    python3 messages_cli.py list-chats
    python3 messages_cli.py export --chat-id 42
    python3 messages_cli.py export --identifier +15551234567 [-o out.txt]

    # Reading from a local iPhone backup (Finder) instead of the live Mac DB:
    python3 messages_cli.py list-backups
    python3 messages_cli.py list-chats --backup <udid-or-prefix>
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from messages_library import (
    DEFAULT_DB,
    chat_date_range,
    chat_message_count,
    connect,
    find_chats_for_identifier,
    iter_attachments,
    iter_messages,
    iter_messages_full,
    list_chats,
    snapshot_db,
)
from ios_backup import Backup, extract_sms_db, find_backup, list_backups, resolve_attachment
from pdf_export import build_pdf


def _resolve_source(args: argparse.Namespace) -> tuple[str, Backup | None]:
    """Return (db_path, backup). backup is None when reading the live Mac DB."""
    if getattr(args, "backup", None):
        backup = find_backup(args.backup)
        return extract_sms_db(backup), backup
    return snapshot_db(args.db), None


def _get_db_path(args: argparse.Namespace) -> str:
    return _resolve_source(args)[0]


def _resolve_chat_id(conn, args: argparse.Namespace) -> int:
    if args.chat_id is not None:
        return args.chat_id
    matches = find_chats_for_identifier(conn, args.identifier)
    if not matches:
        print(f"No chats found matching {args.identifier!r}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"Multiple chats match {args.identifier!r}:", file=sys.stderr)
        for chat in matches:
            print(f"  [{chat.chat_id}] {chat.label}", file=sys.stderr)
        print("Re-run with --chat-id to disambiguate.", file=sys.stderr)
        sys.exit(1)
    return matches[0].chat_id


def _open_chat(args: argparse.Namespace) -> tuple[sqlite3.Connection, int, Backup | None]:
    """Resolve --db/--backup and --chat-id/--identifier together: connect to
    the right database and pick the target chat. Shared by every subcommand
    that operates on a single chat."""
    db_path, backup = _resolve_source(args)
    conn = connect(db_path)
    chat_id = _resolve_chat_id(conn, args)
    return conn, chat_id, backup


def cmd_list_chats(args: argparse.Namespace) -> None:
    db_path = _get_db_path(args)
    conn = connect(db_path)
    chats = list_chats(conn)
    for chat in chats:
        print(f"[{chat.chat_id:>6}] {chat.message_count:>6} msgs  {chat.label}")


def cmd_export(args: argparse.Namespace) -> None:
    conn, chat_id, _backup = _open_chat(args)

    out = open(args.output, "w") if args.output else sys.stdout
    try:
        for msg in iter_messages(conn, chat_id):
            if not msg["text"]:
                continue
            who = "Me" if msg["is_from_me"] else (msg["sender"] or "Unknown")
            when = msg["dt"].strftime("%Y-%m-%d %H:%M:%S") if msg["dt"] else "?"
            out.write(f"[{when}] {who}: {msg['text']}\n")
    finally:
        if out is not sys.stdout:
            out.close()


def _unique_filename(seen_names: dict[str, int], name: str) -> str:
    """Disambiguate collisions (e.g. multiple "IMG_1234.HEIC" at the same
    second) by appending an incrementing suffix, keeping the extension."""
    if name not in seen_names:
        seen_names[name] = 0
        return name
    seen_names[name] += 1
    root, ext = os.path.splitext(name)
    return f"{root}_{seen_names[name]}{ext}"


def cmd_export_attachments(args: argparse.Namespace) -> None:
    conn, chat_id, backup = _open_chat(args)
    os.makedirs(args.output_dir, exist_ok=True)

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
        name = _unique_filename(seen_names, f"{when}_{who}_{base}")
        dest = os.path.join(args.output_dir, name)

        if resolve_attachment(att["filename"], backup, dest):
            copied += 1
        else:
            skipped += 1

    print(f"Copied {copied} attachments to {args.output_dir} ({skipped} missing/skipped)")


def cmd_export_pdf(args: argparse.Namespace) -> None:
    conn, chat_id, backup = _open_chat(args)
    title = args.identifier or f"Chat {chat_id}"
    build_pdf(iter_messages_full(conn, chat_id), backup, args.output, title=title)
    print(f"Wrote {args.output}")


def cmd_info(args: argparse.Namespace) -> None:
    conn, chat_id, _backup = _open_chat(args)
    first, last = chat_date_range(conn, chat_id)
    count = chat_message_count(conn, chat_id)
    print(f"chat_id: {chat_id}")
    print(f"messages stored locally: {count}")
    print(f"earliest: {first}")
    print(f"latest:   {last}")


def cmd_list_backups(args: argparse.Namespace) -> None:
    backups = list_backups()
    if not backups:
        print("No local device backups found.", file=sys.stderr)
        return
    for b in backups:
        enc = "encrypted" if b.is_encrypted else "unencrypted"
        print(f"{b.udid}  {enc:<12} {b.device_name!r:<20} last backup: {b.last_backup_date}")


def _add_db_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=DEFAULT_DB, help="path to chat.db (default: live Mac DB)")
    parser.add_argument(
        "--backup",
        help="read from a local iPhone backup instead (UDID or prefix; see list-backups)",
    )


def _add_chat_target_args(parser: argparse.ArgumentParser) -> None:
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--chat-id", type=int, help="chat ROWID (see list-chats)")
    target.add_argument("--identifier", help="phone number or email to match")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read an iMessage chat.db / iPhone backup")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-chats", help="list all chats with message counts")
    _add_db_source_args(p_list)
    p_list.set_defaults(func=cmd_list_chats)

    p_export = sub.add_parser("export", help="export one chat's text messages")
    _add_db_source_args(p_export)
    _add_chat_target_args(p_export)
    p_export.add_argument("-o", "--output", help="output file (default: stdout)")
    p_export.set_defaults(func=cmd_export)

    p_att = sub.add_parser("export-attachments", help="copy attachments (images, videos, etc.) out for one chat")
    _add_db_source_args(p_att)
    _add_chat_target_args(p_att)
    p_att.add_argument("-o", "--output-dir", required=True, help="directory to copy attachments into")
    p_att.set_defaults(func=cmd_export_attachments)

    p_pdf = sub.add_parser("export-pdf", help="render a chat (text + inline photos) to a single PDF")
    _add_db_source_args(p_pdf)
    _add_chat_target_args(p_pdf)
    p_pdf.add_argument("-o", "--output", required=True, help="output PDF path")
    p_pdf.set_defaults(func=cmd_export_pdf)

    p_info = sub.add_parser("info", help="show a chat's local message count and date range")
    _add_db_source_args(p_info)
    _add_chat_target_args(p_info)
    p_info.set_defaults(func=cmd_info)

    p_backups = sub.add_parser("list-backups", help="list local iPhone backups found on this Mac")
    p_backups.set_defaults(func=cmd_list_backups)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
