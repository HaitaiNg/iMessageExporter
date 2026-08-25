from __future__ import annotations

import hashlib
import os
import plistlib
import sqlite3

import pytest

from ios_backup import extract_file, extract_sms_db, find_backup, list_backups


def _file_id(domain: str, relative_path: str) -> str:
    return hashlib.sha1(f"{domain}-{relative_path}".encode()).hexdigest()


def _make_backup(
    root,
    udid: str,
    encrypted: bool = False,
    device_name: str = "Test iPhone",
    files: dict | None = None,
) -> str:
    """Build a synthetic Finder-backup directory under `root`. `files` maps
    (domain, relative_path) -> file content bytes."""
    backup_dir = os.path.join(root, udid)
    os.makedirs(backup_dir, exist_ok=True)

    with open(os.path.join(backup_dir, "Manifest.plist"), "wb") as f:
        plistlib.dump({"IsEncrypted": encrypted}, f)
    with open(os.path.join(backup_dir, "Info.plist"), "wb") as f:
        plistlib.dump({"Device Name": device_name, "Last Backup Date": "2024-01-01"}, f)

    manifest_db = os.path.join(backup_dir, "Manifest.db")
    conn = sqlite3.connect(manifest_db)
    # Real Finder backups write Manifest.db in WAL mode — match that here so
    # these tests actually exercise the WAL-vs-read-only-open interaction
    # (see the immutable=1 comment in ios_backup._file_id_for) rather than
    # a plain rollback-journal file that doesn't hit that code path at all.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT)")
    for (domain, relative_path), content in (files or {}).items():
        file_id = _file_id(domain, relative_path)
        conn.execute(
            "INSERT INTO Files (fileID, domain, relativePath) VALUES (?, ?, ?)",
            (file_id, domain, relative_path),
        )
        subdir = os.path.join(backup_dir, file_id[:2])
        os.makedirs(subdir, exist_ok=True)
        with open(os.path.join(subdir, file_id), "wb") as f:
            f.write(content)
    conn.commit()
    conn.close()

    return backup_dir


# ---------------------------------------------------------------------------
# list_backups / find_backup
# ---------------------------------------------------------------------------


def test_list_backups_empty_root_returns_empty_list(tmp_path):
    assert list_backups(str(tmp_path / "does-not-exist")) == []


def test_list_backups_skips_dirs_without_manifest(tmp_path):
    os.makedirs(tmp_path / "not-a-backup")
    _make_backup(tmp_path, "AAAA1111")
    backups = list_backups(str(tmp_path))
    assert [b.udid for b in backups] == ["AAAA1111"]


def test_list_backups_reads_metadata(tmp_path):
    _make_backup(tmp_path, "AAAA1111", encrypted=True, device_name="Old iPhone")
    [backup] = list_backups(str(tmp_path))
    assert backup.is_encrypted is True
    assert backup.device_name == "Old iPhone"
    assert backup.last_backup_date == "2024-01-01"


@pytest.mark.parametrize(
    "_, query, expect_match",
    [
        ("exact UDID matches", "AAAA1111", True),
        ("unambiguous prefix matches", "AAAA", True),
        ("no match raises", "ZZZZ", False),
    ],
)
def test_find_backup_single_backup_present(tmp_path, _, query, expect_match):
    _make_backup(tmp_path, "AAAA1111")
    if expect_match:
        backup = find_backup(query, root=str(tmp_path))
        assert backup.udid == "AAAA1111"
    else:
        with pytest.raises(RuntimeError, match="No backup found"):
            find_backup(query, root=str(tmp_path))


def test_find_backup_ambiguous_prefix_raises(tmp_path):
    _make_backup(tmp_path, "AAAA1111")
    _make_backup(tmp_path, "AAAA2222")
    with pytest.raises(RuntimeError, match="multiple"):
        find_backup("AAAA", root=str(tmp_path))


# ---------------------------------------------------------------------------
# extract_file / extract_sms_db
# ---------------------------------------------------------------------------


def test_extract_file_copies_content_to_destination(tmp_path):
    backup_dir = _make_backup(
        tmp_path, "AAAA1111", files={("HomeDomain", "Library/SMS/sms.db"): b"fake sqlite content"}
    )
    [backup] = list_backups(str(tmp_path))
    dst = tmp_path / "out.db"
    ok = extract_file(backup, "HomeDomain", "Library/SMS/sms.db", str(dst))
    assert ok is True
    assert dst.read_bytes() == b"fake sqlite content"


def test_extract_file_returns_false_when_not_in_manifest(tmp_path):
    _make_backup(tmp_path, "AAAA1111", files={})
    [backup] = list_backups(str(tmp_path))
    dst = tmp_path / "out.db"
    ok = extract_file(backup, "HomeDomain", "Library/SMS/sms.db", str(dst))
    assert ok is False
    assert not dst.exists()


def test_extract_sms_db_encrypted_backup_raises(tmp_path):
    _make_backup(tmp_path, "AAAA1111", encrypted=True)
    [backup] = list_backups(str(tmp_path))
    with pytest.raises(RuntimeError, match="encrypted"):
        extract_sms_db(backup)


def test_extract_sms_db_missing_database_raises(tmp_path):
    _make_backup(tmp_path, "AAAA1111", encrypted=False, files={})
    [backup] = list_backups(str(tmp_path))
    with pytest.raises(RuntimeError, match="No Messages database"):
        extract_sms_db(backup)


def test_extract_sms_db_copies_db_and_wal_sidecar(tmp_path):
    _make_backup(
        tmp_path,
        "AAAA1111",
        files={
            ("HomeDomain", "Library/SMS/sms.db"): b"db content",
            ("HomeDomain", "Library/SMS/sms.db-wal"): b"wal content",
        },
    )
    [backup] = list_backups(str(tmp_path))
    dst = extract_sms_db(backup)
    assert os.path.basename(dst) == "sms.db"
    with open(dst, "rb") as f:
        assert f.read() == b"db content"
    with open(dst + "-wal", "rb") as f:
        assert f.read() == b"wal content"
    assert not os.path.exists(dst + "-shm")  # wasn't in the manifest, correctly skipped
