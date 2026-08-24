"""
ios_backup.py — Locate and read the Messages database out of a local,
unencrypted iPhone backup made via Finder.

Finder stores backups at:
    ~/Library/Application Support/MobileSync/Backup/<UDID>/

Each backup (iOS 10+) has a Manifest.db (SQLite) mapping a stable fileID to
the original (domain, relativePath) on the phone. For unencrypted backups,
the actual file content is stored on disk at <backup_dir>/<fileID[:2]>/<fileID>.
The Messages database lives at domain "HomeDomain", relativePath
"Library/SMS/sms.db" — same schema as ~/Library/Messages/chat.db.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass

BACKUP_ROOT = os.path.expanduser("~/Library/Application Support/MobileSync/Backup")

SMS_DOMAIN = "HomeDomain"
SMS_RELATIVE_PATH = "Library/SMS/sms.db"


@dataclass
class Backup:
    udid: str
    path: str
    device_name: str
    is_encrypted: bool
    last_backup_date: str | None


def list_backups(root: str = BACKUP_ROOT) -> list[Backup]:
    """Enumerate local device backups Finder has created on this Mac."""
    backups = []
    if not os.path.isdir(root):
        return backups
    for udid in sorted(os.listdir(root)):
        backup_dir = os.path.join(root, udid)
        manifest_plist = os.path.join(backup_dir, "Manifest.plist")
        if not os.path.isfile(manifest_plist):
            continue
        with open(manifest_plist, "rb") as f:
            manifest = plistlib.load(f)

        device_name = udid
        last_backup_date = None
        info_plist_path = os.path.join(backup_dir, "Info.plist")
        if os.path.isfile(info_plist_path):
            with open(info_plist_path, "rb") as f:
                info = plistlib.load(f)
            device_name = info.get("Device Name", udid)
            last_backup_date = str(info.get("Last Backup Date", "")) or None

        backups.append(
            Backup(
                udid=udid,
                path=backup_dir,
                device_name=device_name,
                is_encrypted=bool(manifest.get("IsEncrypted", False)),
                last_backup_date=last_backup_date,
            )
        )
    return backups


def find_backup(udid_or_prefix: str, root: str = BACKUP_ROOT) -> Backup:
    """Find a backup by exact UDID or unambiguous prefix."""
    matches = [b for b in list_backups(root) if b.udid.startswith(udid_or_prefix)]
    if not matches:
        raise RuntimeError(f"No backup found matching {udid_or_prefix!r} in {root}")
    if len(matches) > 1:
        options = ", ".join(b.udid for b in matches)
        raise RuntimeError(f"{udid_or_prefix!r} matches multiple backups: {options}")
    return matches[0]


def _file_id_for(backup_dir: str, domain: str, relative_path: str) -> str | None:
    manifest_db = os.path.join(backup_dir, "Manifest.db")
    if not os.path.isfile(manifest_db):
        raise RuntimeError(
            f"{manifest_db} not found — this looks like a pre-iOS-10 backup "
            "format (Manifest.mbdb), which isn't supported."
        )
    conn = sqlite3.connect(f"file:{manifest_db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT fileID FROM Files WHERE domain = ? AND relativePath = ?",
            (domain, relative_path),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def extract_file(backup: Backup, domain: str, relative_path: str, dst: str) -> bool:
    """Copy one backed-up file out to `dst`. Returns False if not found."""
    file_id = _file_id_for(backup.path, domain, relative_path)
    if file_id is None:
        return False
    src = os.path.join(backup.path, file_id[:2], file_id)
    if not os.path.isfile(src):
        return False
    shutil.copy2(src, dst)
    return True


def resolve_attachment(filename: str, backup: Backup | None, dest: str) -> bool:
    """Copy an attachment (as stored in the `attachment` table's `filename`
    column) to `dest`, regardless of whether it's coming from the live Mac
    filesystem or an iOS backup. Returns False if the file can't be found.
    """
    if backup is not None:
        rel = filename[2:] if filename.startswith("~/") else filename.lstrip("/")
        return extract_file(backup, "MediaDomain", rel, dest)
    src = os.path.expanduser(filename)
    if not os.path.isfile(src):
        return False
    shutil.copy2(src, dest)
    return True


def extract_sms_db(backup: Backup) -> str:
    """Copy sms.db (+ -wal/-shm sidecars, if present) out of the backup into
    a fresh temp dir and return the path to the copy.

    Raises RuntimeError if the backup is encrypted (not supported) or
    doesn't contain a Messages database.
    """
    if backup.is_encrypted:
        raise RuntimeError(
            f"Backup {backup.udid} is encrypted; decrypting it isn't supported. "
            "Re-create the backup in Finder with 'Encrypt local backup' unchecked."
        )
    tmpdir = tempfile.mkdtemp(prefix="msgexport_ios_")
    dst = os.path.join(tmpdir, "sms.db")
    if not extract_file(backup, SMS_DOMAIN, SMS_RELATIVE_PATH, dst):
        raise RuntimeError(f"No Messages database found in backup {backup.udid}")
    for suffix in ("-wal", "-shm"):
        extract_file(backup, SMS_DOMAIN, SMS_RELATIVE_PATH + suffix, dst + suffix)
    return dst
