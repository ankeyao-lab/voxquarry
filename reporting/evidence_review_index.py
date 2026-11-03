"""Disposable text lookup derived only from a verified immutable Review Fact.

The source and derived bytes are checked before use. Building scans the entire
fact, including duplicate identities, and publishes only a complete index.
It never changes the report, evidence database, source fact or its manifest.
"""
from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from functools import lru_cache
from pathlib import Path

VERSION = 1
FIELDS = ("review_occurrence_id", "review_entity_id")


def _signature(path):
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("text index must be a regular file")
    return value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@lru_cache(maxsize=64)
def _verify_index(path, digest, signature):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("text index hash mismatch")
    if _signature(path) != signature:
        raise ValueError("text index changed during verification")


def _valid_index(path, source_hash):
    manifest = path.with_suffix(".json")
    _signature(manifest)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if value.get("version") != VERSION or value.get("source_hash") != source_hash:
        raise ValueError("text index source mismatch")
    signature = _signature(path)
    _verify_index(path, value["sha256"], signature)
    return signature


def _build(path, raw, source_hash):
    descriptor, name = tempfile.mkstemp(prefix=".building-", suffix=".sqlite3", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    metadata = temporary.with_suffix(".json")
    db = None
    try:
        with sqlite3.connect(temporary) as db:
            db.executescript("""
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                PRAGMA temp_store=FILE;
                PRAGMA cache_size=-8192;
                CREATE TABLE comments (
                    review_occurrence_id TEXT NOT NULL,
                    review_entity_id TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL
                );
            """)
            raw.seek(0)
            batch = []
            with gzip.open(raw, "rt", encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    fact = json.loads(line)
                    title, body = str(fact.get("review_title") or ""), str(fact.get("review_text") or "")
                    if title or body:
                        batch.append((*(str(fact.get(field) or "") for field in FIELDS), title, body))
                    if len(batch) >= 256:
                        db.executemany("INSERT INTO comments VALUES (?,?,?,?)", batch)
                        batch.clear()
            db.executemany("INSERT INTO comments VALUES (?,?,?,?)", batch)
            for field in FIELDS:
                db.execute(f"CREATE INDEX idx_{field} ON comments({field})")
        # Close SQLite explicitly before atomic publication (its context manager
        # commits transactions, but does not close the connection).
        db.close()
        db = None
        raw.seek(0)
        if "sha256:" + hashlib.file_digest(raw, "sha256").hexdigest() != source_hash:
            raise ValueError("Review Fact changed while building text index")
        with temporary.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        metadata.write_text(json.dumps({"version": VERSION, "source_hash": source_hash, "sha256": digest}))
        metadata.chmod(0o600)
        os.replace(temporary, path)
        os.replace(metadata, path.with_suffix(".json"))
    finally:
        if db is not None:
            db.close()
        temporary.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)


def indexed_comments(raw, source_hash, cache_root, identities):
    """Read a page after the caller verified the held source descriptor's hash."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", source_hash):
        raise ValueError("invalid source hash")
    cache_root.mkdir(mode=0o700, exist_ok=True)
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise ValueError("unsafe text index directory")
    cache_root = cache_root.resolve()
    path = cache_root / (source_hash.removeprefix("sha256:") + ".sqlite3")
    lock = os.open(path.with_suffix(".lock"), os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise ValueError("unsafe text index lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            signature = _valid_index(path, source_hash)
        except (OSError, ValueError, KeyError, TypeError):
            # Never follow or replace an unexpected link in the cache.
            if path.is_symlink() or path.with_suffix(".json").is_symlink():
                raise ValueError("unsafe text index file")
            _build(path, raw, source_hash)
            signature = _valid_index(path, source_hash)
        result = {}
        db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            for field, identity in identities:
                if field not in FIELDS:
                    raise ValueError("unknown text identity")
                matches = set(db.execute(f"SELECT title, body FROM comments WHERE {field}=?", (identity,)))
                if matches:
                    result[(field, identity)] = matches
        finally:
            db.close()
        if _signature(path) != signature:
            raise ValueError("text index changed during query")
        return result
    finally:
        os.close(lock)
