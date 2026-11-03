from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .contracts import format_utc, utc_now
from .filesystem import (
    FilesystemSecurityError,
    absolute_path,
    create_secure_file,
    path_lexists,
    secure_directory,
    secure_file,
    secure_sqlite_family,
    write_secure_text,
)


class BackupError(RuntimeError):
    pass


DATABASES = ("auth.sqlite3", "audit.sqlite3")


def sha256_file(path: Path) -> str:
    path = secure_file(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity(path: Path) -> str:
    path = secure_file(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()
        secure_file(path)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    path = secure_file(path)
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def create_backup(data_dir: Path, destination: Path) -> dict[str, Any]:
    data_dir = absolute_path(Path(data_dir))
    destination = absolute_path(Path(destination))
    if path_lexists(destination):
        raise BackupError("backup destination already exists")
    manifest = {
        "schema_version": "account-admin-backup/v1",
        "non_production": True,
        "created_at": format_utc(utc_now()),
        "files": [],
    }
    destination_created = False
    try:
        secure_directory(data_dir, create=False)
        for filename in DATABASES:
            secure_sqlite_family(data_dir / filename)
        secure_directory(destination, create=True, exclusive=True)
        destination_created = True
        for filename in DATABASES:
            source_path = data_dir / filename
            target_path = destination / filename
            create_secure_file(target_path)
            source = _readonly_connection(source_path)
            target = sqlite3.connect(target_path)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
                secure_sqlite_family(source_path)
                secure_sqlite_family(target_path)
            integrity = _integrity(target_path)
            if integrity != "ok":
                raise BackupError(f"backup integrity failed: {filename}")
            manifest["files"].append(
                {
                    "name": filename,
                    "bytes": target_path.stat().st_size,
                    "sha256": sha256_file(target_path),
                    "integrity_check": integrity,
                }
            )
        write_secure_text(
            destination / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return manifest
    except FilesystemSecurityError as exc:
        if destination_created:
            shutil.rmtree(destination, ignore_errors=True)
        raise BackupError(str(exc)) from exc
    except Exception:
        if destination_created:
            shutil.rmtree(destination, ignore_errors=True)
        raise


def restore_backup(backup_dir: Path, target_data_dir: Path) -> dict[str, Any]:
    backup_dir = absolute_path(Path(backup_dir))
    target_data_dir = absolute_path(Path(target_data_dir))
    try:
        secure_directory(backup_dir, create=False)
        manifest_path = secure_file(backup_dir / "manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != "account-admin-backup/v1":
            raise BackupError("unsupported backup schema")
        expected = {item["name"]: item for item in manifest.get("files") or []}
        if set(expected) != set(DATABASES):
            raise BackupError("backup file set is incomplete")

        for filename in DATABASES:
            source = secure_file(backup_dir / filename)
            if sha256_file(source) != expected[filename]["sha256"]:
                raise BackupError(f"backup hash mismatch: {filename}")
            if _integrity(source) != "ok":
                raise BackupError(f"backup integrity failed: {filename}")

        secure_directory(target_data_dir, create=True)
        if any(path_lexists(target_data_dir / filename) for filename in DATABASES):
            raise BackupError("restore target must not contain account-admin databases")
        with tempfile.TemporaryDirectory(prefix="account-admin-restore-", dir=target_data_dir) as temp_name:
            temp_dir = secure_directory(Path(temp_name), create=False)
            for filename in DATABASES:
                source = backup_dir / filename
                temporary_target = temp_dir / filename
                create_secure_file(temporary_target)
                shutil.copyfile(source, temporary_target)
                secure_file(temporary_target)
                if _integrity(temporary_target) != "ok":
                    raise BackupError(f"restored database integrity failed: {filename}")
            for filename in DATABASES:
                target = target_data_dir / filename
                if path_lexists(target):
                    raise BackupError("restore target must not contain account-admin databases")
                os.replace(temp_dir / filename, target)
                secure_sqlite_family(target)
        secure_directory(target_data_dir, create=False)
        return manifest
    except FilesystemSecurityError as exc:
        raise BackupError(str(exc)) from exc
