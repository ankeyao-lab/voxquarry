"""Hash canonical JSON without retaining the complete serialized document."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_sha256(value: Any) -> str:
    """Match source.canonical_json UTF-8 bytes with bounded output buffering.

    Input objects and individual JSON string values remain in memory; this
    avoids the additional full-document Unicode and UTF-8 copies when hashing
    evidence collections. Keep encoder settings identical to canonical_json.
    """
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256()
    pending: list[str] = []
    size = 0
    for chunk in encoder.iterencode(value):
        pending.append(chunk)
        size += len(chunk)
        if size >= 65536:
            digest.update("".join(pending).encode("utf-8"))
            pending.clear()
            size = 0
    if pending:
        digest.update("".join(pending).encode("utf-8"))
    return digest.hexdigest()
