"""Offline, deterministic methods over complete frozen Wenquan exports.

Installed as ``research_methods`` in the analysis image. No HTTP, model calls,
reporting dependency or semantic classification. Selectors and canonical order
match reporting.wenquan_datasets / wenquan_union; oracle tests guard that contract.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile


CONTRACT = "voxquarry.research-methods/v1"
DATASET_CONTRACT = "voxquarry.wenquan-dataset/v1"
GROUP_FIELDS = ("channel", "brand", "model", "market_region", "category_id",
                "rating", "has_text", "job_id", "submission_month")
FILTER_FIELDS = {"source_product_ids": "json_extract(payload, '$.source_product_id')",
                 "ratings": "rating", "channels": "channel", "brands": "brand",
                 "models": "model", "market_regions": "market_region",
                 "category_ids": "category_id", "job_ids": "job_id"}
_BATCH_FIELDS = {"source_id", "canonical_entity_rank", "is_duplicate_occurrence"}


class ResearchDataError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _invalid_constant(value):
    raise ValueError("non-JSON constant: " + value)


def _parse(data):
    return json.loads(data, parse_constant=_invalid_constant)


def load_result(path, pointer=""):
    """Read complete JSON and an explicit RFC 6901 pointer; never guess wrappers.

    Use the result's declared schema/path, e.g. ``pointer='/exports'``. A missing
    path is an error, not an empty data set. Array indexes use their decimal form.
    """
    value = _parse(Path(path).read_bytes())
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ResearchDataError("invalid_pointer", "JSON pointer must be empty or start with /")
    for token in pointer.split("/")[1:]:
        if re.search(r"~(?![01])", token):
            raise ResearchDataError("invalid_pointer", "Invalid JSON pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
                value = value[int(token)]
            elif isinstance(value, dict):
                value = value[token]
            else:
                raise KeyError(token)
        except (KeyError, IndexError) as exc:
            raise ResearchDataError("result_path_missing", "JSON pointer not found: " + pointer) from exc
    return value


def iter_jsonl(path):
    """Stream every complete JSONL record, retaining full text and all fields.

    This generic reader makes no completeness/identity claim; use ReviewSet for
    manifest verification, population counts, cross-batch dedup and citations.
    """
    with Path(path).open("rb") as handle:
        for number, line in enumerate(handle, 1):
            try:
                yield _parse(line)
            except (ValueError, UnicodeError) as exc:
                raise ResearchDataError("invalid_jsonl", f"Invalid JSONL record at {path}:{number}") from exc


def schema():
    return {
        "contract": CONTRACT,
        "exports": "list of export_dataset results, each with dataset_id, manifest, analysis_paths",
        "paths": {"manifest": "/manifest", "shards": "/analysis_paths",
                  "full_text": "/review_text", "title": "/review_title",
                  "record_id": "/record_id", "entity_id": "/review_entity_id"},
        "group_fields": list(GROUP_FIELDS),
        "filter_fields": [*FILTER_FIELDS, "has_text", "date_start", "date_end"],
        "populations": {"entities": "global canonical entity, selected before filters",
                        "occurrences": "unique review_occurrence_id across all loaded exports"},
        "identity": "review_entity_id or review_occurrence_id; never text-based identity guessing",
        "compatibility": "same normalization_version and normalization_contracts; legacy missing contracts do not mix with current exports",
        "query": "casefold substring of review_title + space + review_text; lexical, not semantic",
        "rows": "full original fields plus dataset_id and analysis_entity_rank (global rank)",
        "citation": "citation(record_id, dataset_id=None), only for a verified loaded record",
        "completeness": "all shards verified before results; source completeness remains separate",
    }


def _where(filters=None, query=None, population="entities"):
    if filters is not None and not isinstance(filters, Mapping):
        raise ResearchDataError("invalid_request", "filters must be an object")
    filters = dict(filters or {})
    if (set(filters) - {*FILTER_FIELDS, "has_text", "date_start", "date_end"}
            or population not in {"entities", "occurrences"}):
        raise ResearchDataError("invalid_request", "Unknown filter or population")
    clauses, args = (["canonical=1"] if population == "entities" else []), []
    for key, field in FILTER_FIELDS.items():
        if key not in filters:
            continue
        values = filters[key]
        if not isinstance(values, list) or len(values) > 200 or any(
            (type(v) is not int or v not in range(1, 6)) if key == "ratings"
            else (not isinstance(v, str) or len(v) > 500) for v in values
        ):
            raise ResearchDataError("invalid_request", "Invalid filter list: " + key)
        if values:
            clauses.append(f"{field} IN ({','.join('?' for _ in values)})")
            args.extend(values)
    if "has_text" in filters:
        if type(filters["has_text"]) is not bool:
            raise ResearchDataError("invalid_request", "has_text must be boolean")
        clauses.append("has_text=?")
        args.append(int(filters["has_text"]))
    for key, operator in (("date_start", ">="), ("date_end", "<=")):
        if key in filters:
            try:
                if date.fromisoformat(filters[key]).isoformat() != filters[key]:
                    raise ValueError
            except (ValueError, TypeError) as exc:
                raise ResearchDataError("invalid_request", "Invalid date: " + key) from exc
            clauses.append(f"submission_date {operator} ?")
            args.append(filters[key])
    if query is not None:
        if not isinstance(query, str) or len(query) > 1000:
            raise ResearchDataError("invalid_request", "query must be text up to 1000 characters")
        if query:
            clauses.append("instr(search_text,?)>0")
            args.append(query.casefold())
    return " AND ".join(clauses) or "1", args


def _record(row):
    """Index the same normalized facts/order as reporting; preserve raw fields."""
    if not isinstance(row, dict):
        raise ResearchDataError("invalid_source", "Expected a normalized review object")
    required = {"record_id", "review_occurrence_id", "review_entity_id", "review_id",
                "channel", "brand", "model", "market_region", "category_id", "rating",
                "has_text", "job_id", "product_id", "submission_date", "review_title",
                "review_text", "source_row_number"}
    if required - row.keys() or row.get("record_id") != row.get("review_occurrence_id"):
        raise ResearchDataError("invalid_source", "Missing normalized fields or mismatched record identity")
    if (not isinstance(row["record_id"], str)
            or not re.fullmatch(r"[^\]\r\n]{1,200}", row["record_id"])
            or type(row["source_row_number"]) is not int or row["source_row_number"] < 1
            or type(row["has_text"]) is not bool
            or any(not isinstance(row[k], str) for k in ("review_title", "review_text", "channel", "product_id", "job_id"))
            or any(row[k] is not None and not isinstance(row[k], str) for k in (
                "review_entity_id", "review_id", "brand", "model", "market_region", "category_id", "submission_date"))
            or (row["rating"] is not None and (type(row["rating"]) is not int or row["rating"] not in range(1, 6)))):
        raise ResearchDataError("invalid_source", "Invalid normalized review field types")
    entity = row["review_entity_id"] or row["record_id"]
    order = _json([entity, row["channel"], row["product_id"], row["job_id"], f"{row['source_row_number']:020d}"])
    payload = _json({"source_product_id": row.get("source_product_id")})
    # Export-only source IDs and canonical ranks legitimately differ per batch.
    digest = _hash({k: v for k, v in row.items() if k not in _BATCH_FIELDS})
    return (row["record_id"], entity, row["review_id"], row["channel"], row["brand"],
            row["model"], row["market_region"], row["category_id"], row["rating"],
            int(row["has_text"]), row["job_id"], row["submission_date"],
            (row["submission_date"] or "")[:7],
            (row["review_title"] + " " + row["review_text"]).casefold(), order, payload, digest)


class ReviewSet:
    """Verify all shards once, then compose local deterministic calculations.

    The scratch SQLite index holds searchable facts and file offsets, not copies
    of original payloads. Loading is streamed one record at a time; use ``with``
    to remove scratch files. ``rows`` keeps original per-batch rank and adds
    analysis_entity_rank. Aggregate output grows with the number of groups.
    """
    def __init__(self, exports, *, temp_dir=None):
        if (not isinstance(exports, list) or not exports
                or any(not isinstance(item, dict) for item in exports)):
            raise ResearchDataError("invalid_request", "exports must be a non-empty list of export_dataset results")
        if temp_dir is None and Path("/workspace").is_dir():
            temp_dir = "/workspace"  # /tmp in the analysis image is only 256 MiB.
        self._scratch = tempfile.TemporaryDirectory(prefix=".research-methods-", dir=temp_dir)
        self._db = None
        self._sources = []
        self._read = 0
        self._normalization = None
        self._normalization_contracts = None
        try:
            self._db = sqlite3.connect(Path(self._scratch.name) / "facts.sqlite3")
            self._db.execute("PRAGMA journal_mode=OFF")
            self._db.execute("PRAGMA temp_store=FILE")
            self._db.execute("PRAGMA cache_size=-4096")
            self._db.execute("CREATE TABLE facts(occurrence TEXT PRIMARY KEY, entity TEXT, review_id TEXT, channel TEXT, brand TEXT, model TEXT, market_region TEXT, category_id TEXT, rating INTEGER, has_text INTEGER, job_id TEXT, submission_date TEXT, submission_month TEXT, search_text TEXT, sort_key TEXT, payload TEXT, row_hash TEXT, canonical INTEGER DEFAULT 0, entity_rank INTEGER DEFAULT 0)")
            self._db.execute("CREATE TABLE origins(occurrence TEXT, dataset_id TEXT, path TEXT, offset INTEGER, size INTEGER, PRIMARY KEY(occurrence,dataset_id))")
            self._load(exports)
            self._db.execute("CREATE INDEX facts_entity ON facts(entity,sort_key,occurrence)")
            self._db.execute("CREATE TABLE ranks AS SELECT occurrence,ROW_NUMBER() OVER(PARTITION BY entity ORDER BY sort_key,occurrence) AS rank FROM facts")
            self._db.execute("CREATE UNIQUE INDEX ranks_occurrence ON ranks(occurrence)")
            self._db.execute("UPDATE facts SET entity_rank=(SELECT rank FROM ranks WHERE ranks.occurrence=facts.occurrence),canonical=CASE WHEN (SELECT rank FROM ranks WHERE ranks.occurrence=facts.occurrence)=1 THEN 1 ELSE 0 END")
            self._db.execute("DROP TABLE ranks")
            self._db.commit()
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
        self._scratch.cleanup()

    def _load(self, exports):
        seen = set()
        for item in sorted(exports, key=lambda value: str(value.get("dataset_id", ""))):
            dataset, manifest, paths = item.get("dataset_id"), item.get("manifest"), item.get("analysis_paths")
            if (not isinstance(dataset, str) or not re.fullmatch(r"ds_[0-9a-f]{64}", dataset)
                    or dataset in seen or not isinstance(manifest, dict) or not isinstance(paths, list)):
                raise ResearchDataError("invalid_export", "Expected unique dataset IDs, manifest and analysis_paths")
            seen.add(dataset)
            if (manifest.get("dataset_id") != dataset or manifest.get("contract") != DATASET_CONTRACT
                    or manifest.get("export_complete") is not True
                    or manifest.get("manifest_sha256") != _hash({k: v for k, v in manifest.items() if k != "manifest_sha256"})
                    or item.get("manifest_sha256", manifest["manifest_sha256"]) != manifest["manifest_sha256"]):
                raise ResearchDataError("dataset_changed", "Incomplete export or manifest checksum mismatch")
            version = manifest.get("normalization_version")
            if not isinstance(version, str) or not version:
                raise ResearchDataError("invalid_source", "Missing normalization version")
            contracts = manifest.get("normalization_contracts", "legacy-unversioned-contracts")
            if self._normalization is not None and (self._normalization != version
                    or _json(self._normalization_contracts) != _json(contracts)):
                raise ResearchDataError("mixed_normalization_versions", "Cannot combine different normalization contracts; prepare all batches with the same versions")
            self._normalization = version
            self._normalization_contracts = contracts
            shards = manifest.get("shards")
            if (not isinstance(shards, list) or any(not isinstance(part, dict) for part in shards)
                    or any(not isinstance(path, str) or not path for path in paths)
                    or len(paths) != len(shards) or len(set(paths)) != len(paths)):
                raise ResearchDataError("incomplete_export", "analysis_paths must contain every manifest shard exactly once, in manifest order")
            loaded = 0
            for path, part in zip(paths, shards):
                loaded += self._load_shard(dataset, path, part)
            if loaded != manifest.get("record_counts", {}).get("occurrences"):
                raise ResearchDataError("incomplete_export", "Export occurrence count differs from manifest")
            self._sources.append({"dataset_id": dataset, "manifest_sha256": manifest["manifest_sha256"],
                                  "scope": manifest.get("scope", {}), "source_complete": manifest.get("source_complete"),
                                  "source_completeness": manifest.get("source_completeness", []),
                                  "limitations": manifest.get("limitations", [])})

    def _load_shard(self, dataset, path, part):
        digest, count, size = hashlib.sha256(), 0, 0
        with Path(path).open("rb") as handle:
            for line in handle:
                offset = size
                size += len(line)
                digest.update(line)
                try:
                    row = _parse(line)
                except (ValueError, UnicodeError) as exc:
                    raise ResearchDataError("invalid_jsonl", f"Invalid JSONL at {path}:{count + 1}") from exc
                values = _record(row)
                old = self._db.execute("SELECT row_hash FROM facts WHERE occurrence=?", (values[0],)).fetchone()
                if old and old[0] != values[-1]:
                    raise ResearchDataError("overlapping_version_conflict", "Same occurrence has different frozen content; cannot combine")
                self._db.execute("INSERT OR IGNORE INTO facts VALUES(" + ",".join("?" for _ in values) + ",0,0)", values)
                try:
                    self._db.execute("INSERT INTO origins VALUES(?,?,?,?,?)", (values[0], dataset, str(Path(path).resolve()), offset, len(line)))
                except sqlite3.IntegrityError as exc:
                    raise ResearchDataError("invalid_source", "An occurrence appears twice within the same export") from exc
                count += 1
                self._read += 1
        if (not isinstance(part, dict) or part.get("records") != count or part.get("bytes") != size
                or part.get("sha256") != "sha256:" + digest.hexdigest()):
            raise ResearchDataError("dataset_changed", "Shard checksum, byte count or record count differs from manifest")
        return count

    def coverage(self, *, expected_dataset_ids=None):
        if expected_dataset_ids is not None and (not isinstance(expected_dataset_ids, list)
                or any(not isinstance(value, str) for value in expected_dataset_ids)):
            raise ResearchDataError("invalid_request", "expected_dataset_ids must be a string list")
        actual = [source["dataset_id"] for source in self._sources]
        expected = None if expected_dataset_ids is None else sorted(set(expected_dataset_ids))
        occurrences, entities, written_occurrences, written_entities = self._db.execute(
            "SELECT COUNT(*),COALESCE(SUM(canonical),0),COALESCE(SUM(has_text),0),COUNT(DISTINCT CASE WHEN has_text=1 THEN entity END) FROM facts"
        ).fetchone()
        return {"contract": CONTRACT, "dataset_ids": actual, "sources": self._sources,
                "normalization_version": self._normalization, "export_complete": True,
                "normalization_contracts": self._normalization_contracts,
                "source_complete": all(s["source_complete"] is True for s in self._sources),
                "scope_coverage": "loaded_exports_only",
                "missing_dataset_ids": None if expected is None else sorted(set(expected) - set(actual)),
                "unexpected_dataset_ids": None if expected is None else sorted(set(actual) - set(expected)),
                "requested_datasets_complete": None if expected is None else set(expected) == set(actual),
                "record_counts": {"loaded_occurrences": self._read, "occurrences": occurrences,
                                  "overlapping_occurrences": self._read - occurrences, "review_entities": entities,
                                  "written_occurrences": written_occurrences, "written_entities": written_entities}}

    def aggregate(self, *, group_by=None, filters=None, query=None, population="entities"):
        if group_by is not None and (not isinstance(group_by, list) or any(not isinstance(v, str) for v in group_by)):
            raise ResearchDataError("invalid_request", "group_by must be a string list")
        groups = list(group_by or [])
        if len(groups) > 3 or len(set(groups)) != len(groups) or any(g not in GROUP_FIELDS for g in groups):
            raise ResearchDataError("invalid_request", "Invalid grouping fields")
        where, args = _where(filters, query, population)
        columns = ",".join(groups)
        rows = self._db.execute(f"SELECT {columns + ',' if columns else ''}COUNT(*) FROM facts WHERE {where}" + (f" GROUP BY {columns}" if groups else ""), args)
        result = [{"group": dict(zip(groups, row[:-1])), "count": row[-1]} for row in rows]
        result.sort(key=lambda item: (-item["count"], _json(item["group"])))
        return {"population": population, "groups": result, "total": sum(item["count"] for item in result),
                "count_basis": "去重评论实体" if population == "entities" else "评论出现记录"}

    def _full_row(self, occurrence, dataset_id=None):
        clause = " AND dataset_id=?" if dataset_id is not None else ""
        args = (occurrence, dataset_id) if dataset_id is not None else (occurrence,)
        located = self._db.execute("SELECT dataset_id,path,offset,size,row_hash,entity_rank FROM origins JOIN facts USING(occurrence) WHERE occurrence=?" + clause + " ORDER BY dataset_id LIMIT 1", args).fetchone()
        if located is None:
            raise ResearchDataError("citation_unknown", "Record was not present in the verified exports")
        dataset, path, offset, size, digest, rank = located
        with Path(path).open("rb") as handle:
            handle.seek(offset)
            row = _parse(handle.read(size))
        if _record(row)[-1] != digest:
            raise ResearchDataError("dataset_changed", "Record changed after export verification")
        return {**row, "dataset_id": dataset, "analysis_entity_rank": rank}

    def rows(self, *, filters=None, query=None, population="entities"):
        """Yield complete reviews in canonical order; no implicit sample or cap."""
        where, args = _where(filters, query, population)
        cursor = self._db.execute("SELECT occurrence FROM facts WHERE " + where + " ORDER BY sort_key,occurrence", args)
        try:
            for (occurrence,) in cursor:
                yield self._full_row(occurrence)
        finally:
            cursor.close()

    def citation(self, record_id, *, dataset_id=None):
        if not isinstance(record_id, str) or (dataset_id is not None and not isinstance(dataset_id, str)):
            raise ResearchDataError("invalid_request", "Citation identities must be strings")
        row = self._full_row(record_id, dataset_id)
        return f"[review:{row['dataset_id']}:{row['record_id']}]"

    def citations(self, record_ids):
        """Stable unique handles; missing records fail instead of silently dropping."""
        if not isinstance(record_ids, (list, tuple)) or any(not isinstance(value, str) for value in record_ids):
            raise ResearchDataError("invalid_request", "record_ids must be a string list")
        return [self.citation(record) for record in dict.fromkeys(record_ids)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("schema", "coverage", "aggregate", "citations"))
    parser.add_argument("--exports", help="JSON file containing export result list (or select it with --pointer)")
    parser.add_argument("--pointer", default="")
    parser.add_argument("--group-by", nargs="*", default=[])
    parser.add_argument("--filters", default="{}", help="JSON selector object")
    parser.add_argument("--query")
    parser.add_argument("--population", choices=("entities", "occurrences"), default="entities")
    parser.add_argument("--record-ids", nargs="+", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "schema":
            result = schema()
        else:
            if not args.exports:
                parser.error("--exports is required for data commands")
            with ReviewSet(load_result(args.exports, args.pointer)) as reviews:
                if args.command == "coverage":
                    result = reviews.coverage()
                elif args.command == "citations":
                    result = reviews.citations(args.record_ids)
                else:
                    result = reviews.aggregate(group_by=args.group_by, filters=_parse(args.filters), query=args.query, population=args.population)
        print(_json(result))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(_json({"error": {"code": getattr(exc, "code", "local_data_error"), "message": str(exc)}}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
