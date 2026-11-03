from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class BrandPersonaStore:
    """Durable local cache for exploratory analyses and immutable saves.

    Runtime artifacts live below the already ignored reporting var root.  The
    active source snapshot remains read-only and is never modified.
    """

    def __init__(self, var_root: Path) -> None:
        self.var_root = var_root
        self.root = var_root / "brand-persona"
        self.cache_root = self.root / "cache"
        self.saved_root = self.root / "saved"
        self.job_root = self.root / "jobs"
        self.request_root = self.root / "requests"
        self.featured_root = self.root / "featured"
        self.image_root = self.root / "images"

    def _heartbeat_path(self, job_id: str) -> Path:
        return self.var_root / "scheduler" / "heartbeats" / f"brand-persona-{job_id}.json"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _persona_image_type(asset_bytes: bytes) -> tuple[str, str]:
        if asset_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png", "image/png"
        if asset_bytes.startswith(b"\xff\xd8\xff"):
            return "jpg", "image/jpeg"
        if asset_bytes.startswith(b"RIFF") and asset_bytes[8:12] == b"WEBP":
            return "webp", "image/webp"
        if len(asset_bytes) >= 12 and asset_bytes[4:12] in {
            b"ftypavif",
            b"ftypavis",
        }:
            return "avif", "image/avif"
        raise ValueError("unsupported persona image content")

    def store_persona_image(
        self,
        *,
        analysis_id: str,
        persona_id: str,
        asset_bytes: bytes,
        mime_type: str,
        provider: str,
        model: str,
        prompt_sha256: str,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"bpa_[0-9a-f]{24}", analysis_id):
            raise ValueError("invalid brand persona analysis_id")
        if not re.fullmatch(r"[a-z0-9_]{2,80}", persona_id):
            raise ValueError("invalid brand persona persona_id")
        extension, detected_mime = self._persona_image_type(asset_bytes)
        if mime_type and mime_type.casefold() not in {
            detected_mime,
            "application/octet-stream",
        }:
            raise ValueError("persona image MIME does not match content")
        digest = hashlib.sha256(asset_bytes).hexdigest()
        target = self.image_root / f"{digest}.{extension}"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "wb", dir=target.parent, delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(asset_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        return {
            "schema_version": "brand-persona-image/v1",
            "provider": provider,
            "model": model,
            "image_url": (
                "/reporting/api/v1/brand-persona/analyses/"
                f"{analysis_id}/personas/{persona_id}/images/{digest}"
            ),
            "asset_sha256": "sha256:" + digest,
            "mime_type": detected_mime,
            "prompt_sha256": prompt_sha256,
            "generated_at": self._now(),
            "illustrative_only": True,
            "disclosure": "AI 生成情境示意图 · 非真实评论者",
            "alt": "Persona 专属任务情境示意图",
        }

    def get_persona_image_asset(
        self, asset_sha256: str
    ) -> tuple[Path, str]:
        digest = str(asset_sha256 or "").removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise KeyError(asset_sha256)
        for extension, mime_type in (
            ("png", "image/png"),
            ("jpg", "image/jpeg"),
            ("webp", "image/webp"),
            ("avif", "image/avif"),
        ):
            path = self.image_root / f"{digest}.{extension}"
            if path.is_file():
                return path, mime_type
        raise KeyError(asset_sha256)

    def create_job(
        self,
        scope: dict[str, Any],
        *,
        purpose: str = "custom",
        trigger: str = "user",
    ) -> dict[str, Any]:
        now = self._now()
        material = json.dumps(
            {"scope": scope, "created_at": now, "nonce": os.urandom(8).hex()},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        job_id = "bpjob_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        job = {
            "job_id": job_id,
            "status": "queued",
            "stage": "等待分析槽",
            "progress": 0,
            "scope": deepcopy(scope),
            "purpose": purpose,
            "trigger": trigger,
            "analysis_id": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "heartbeat_at": None,
            "worker_pid": None,
        }
        self._write_json(self.job_root / f"{job_id}.json", job)
        return job

    @staticmethod
    def _featured_key(brand: str) -> str:
        return hashlib.sha256(brand.strip().casefold().encode("utf-8")).hexdigest()[:24]

    def get_featured_pointer(self, brand: str) -> dict[str, Any] | None:
        key = self._featured_key(brand)
        path = self.featured_root / f"brand_{key}.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        if str(value.get("brand") or "").casefold() != brand.strip().casefold():
            return None
        return value

    def list_featured_pointers(self) -> list[dict[str, Any]]:
        if not self.featured_root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for path in self.featured_root.glob("brand_*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and value.get("brand"):
                rows.append(value)
        rows.sort(key=lambda row: str(row.get("brand") or "").casefold())
        return rows

    def publish_featured(
        self,
        *,
        brand: str,
        analysis_id: str,
        artifact_id: str,
        snapshot_id: str,
        content_hash: str,
        source_job_id: str,
        trigger: str,
    ) -> dict[str, Any]:
        pointer = {
            "schema_version": "brand-persona-featured/v1",
            "brand": brand,
            "analysis_id": analysis_id,
            "artifact_id": artifact_id,
            "snapshot_id": snapshot_id,
            "content_hash": content_hash,
            "source_job_id": source_job_id,
            "trigger": trigger,
            "updated_at": self._now(),
        }
        key = self._featured_key(brand)
        self._write_json(self.featured_root / f"brand_{key}.json", pointer)
        return pointer

    @staticmethod
    def request_key(
        *, snapshot_id: str, scope: dict[str, Any], cache_version: str
    ) -> str:
        material = json.dumps(
            {
                "snapshot_id": snapshot_id,
                "scope": scope,
                "cache_version": cache_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "bpreq_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def get_for_request(
        self, *, snapshot_id: str, scope: dict[str, Any], cache_version: str
    ) -> dict[str, Any] | None:
        key = self.request_key(
            snapshot_id=snapshot_id,
            scope=scope,
            cache_version=cache_version,
        )
        path = self.request_root / f"{key}.json"
        if not path.is_file():
            return None
        try:
            pointer = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        analysis = self.get(str(pointer.get("analysis_id") or ""))
        if analysis is None:
            return None
        if (
            analysis.get("snapshot_id") != snapshot_id
            or analysis.get("cache_version") != cache_version
            or analysis.get("scope") != scope
        ):
            return None
        return analysis

    def bind_request(
        self,
        *,
        snapshot_id: str,
        scope: dict[str, Any],
        cache_version: str,
        analysis_id: str,
    ) -> None:
        key = self.request_key(
            snapshot_id=snapshot_id,
            scope=scope,
            cache_version=cache_version,
        )
        self._write_json(
            self.request_root / f"{key}.json",
            {
                "request_key": key,
                "analysis_id": analysis_id,
                "snapshot_id": snapshot_id,
                "cache_version": cache_version,
                "scope": deepcopy(scope),
                "bound_at": self._now(),
            },
        )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        if not job_id.startswith("bpjob_"):
            return None
        path = self.job_root / f"{job_id}.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def list_jobs(self) -> list[dict[str, Any]]:
        if not self.job_root.is_dir():
            return []
        rows = [
            job
            for path in self.job_root.glob("bpjob_*.json")
            if (job := self.get_job(path.stem)) is not None
        ]
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows

    def update_job(
        self,
        job_id: str,
        *,
        status: str,
        stage: str,
        progress: int,
        analysis_id: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        job.update(
            {
                "status": status,
                "stage": stage,
                "progress": max(0, min(100, int(progress))),
                "updated_at": self._now(),
                "error": deepcopy(error),
            }
        )
        if analysis_id is not None:
            job["analysis_id"] = analysis_id
        self._write_json(self.job_root / f"{job_id}.json", job)
        return job

    def touch_worker(self, job_id: str, *, worker_pid: int | None = None) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.get("status") in {"completed", "failed"}:
            return job
        now = self._now()
        heartbeat = {
            "schema_version": "report-worker-heartbeat/v1",
            "kind": "brand-persona",
            "job_id": job_id,
            "heartbeat_at": now,
            "worker_pid": int(worker_pid) if worker_pid is not None else None,
        }
        self._write_json(self._heartbeat_path(job_id), heartbeat)
        return {**job, **heartbeat}

    def get_worker_heartbeat(self, job_id: str) -> dict[str, Any] | None:
        path = self._heartbeat_path(job_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def clear_worker_heartbeat(self, job_id: str) -> None:
        self._heartbeat_path(job_id).unlink(missing_ok=True)

    def requeue_interrupted_jobs(self) -> list[str]:
        recovered: list[str] = []
        for job in self.list_jobs():
            if job.get("status") in {"completed", "failed", "queued"}:
                continue
            previous = {
                "status": job.get("status"),
                "stage": job.get("stage"),
                "progress": int(job.get("progress") or 0),
            }
            job.update(
                {
                    "status": "queued",
                    "stage": "服务恢复，等待重新分析",
                    "progress": 0,
                    "last_status": previous["status"],
                    "last_stage": previous["stage"],
                    "last_progress": previous["progress"],
                    "recovered_from": previous,
                    "heartbeat_at": None,
                    "worker_pid": None,
                    "updated_at": self._now(),
                }
            )
            self._write_json(self.job_root / f"{job['job_id']}.json", job)
            recovered.append(str(job["job_id"]))
        recovered.sort()
        return recovered

    def _evidence_path(self, analysis_id: str) -> Path:
        return self.cache_root / f"{analysis_id}.evidence.sqlite3"

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _write_evidence(
        self, analysis_id: str, rows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        target = self._evidence_path(analysis_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=target.parent, suffix=".sqlite3", delete=False
        ) as handle:
            temporary = Path(handle.name)
        connection = sqlite3.connect(temporary)
        try:
            connection.executescript(
                """
                PRAGMA journal_mode=DELETE;
                PRAGMA synchronous=FULL;
                CREATE TABLE evidence (
                  evidence_id TEXT PRIMARY KEY,
                  review_entity_id TEXT,
                  kind TEXT,
                  label TEXT,
                  quote TEXT,
                  matched_text TEXT,
                  rating INTEGER,
                  category_id TEXT,
                  category TEXT,
                  model TEXT,
                  channel TEXT,
                  market_region TEXT,
                  submission_date TEXT,
                  payload_json TEXT NOT NULL
                );
                CREATE INDEX evidence_category_idx ON evidence(category_id);
                CREATE INDEX evidence_model_idx ON evidence(model);
                CREATE INDEX evidence_kind_idx ON evidence(kind, label);
                CREATE INDEX evidence_entity_idx ON evidence(review_entity_id);
                """
            )
            connection.executemany(
                """
                INSERT INTO evidence (
                  evidence_id, review_entity_id, kind, label, quote,
                  matched_text, rating, category_id, category, model,
                  channel, market_region, submission_date, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        str(row.get("evidence_id") or ""),
                        str(row.get("review_entity_id") or ""),
                        str(row.get("kind") or ""),
                        str(row.get("label") or ""),
                        str(row.get("quote") or ""),
                        str(row.get("matched_text") or ""),
                        row.get("rating"),
                        str(row.get("category_id") or ""),
                        str(row.get("category") or ""),
                        str(row.get("model") or ""),
                        str(row.get("channel") or ""),
                        str(row.get("market_region") or ""),
                        str(row.get("submission_date") or ""),
                        json.dumps(
                            row,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                    for row in rows
                    if row.get("evidence_id")
                ),
            )
            connection.commit()
            integrity = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
            count = int(connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0])
            if integrity != "ok" or count != len({str(row.get("evidence_id")) for row in rows if row.get("evidence_id")}):
                raise ValueError("brand persona evidence sidecar validation failed")
        finally:
            connection.close()
        os.replace(temporary, target)
        return {
            "schema_version": "brand-persona-evidence/v1",
            "evidence_rows": count,
            "sqlite_sha256": "sha256:" + self._sha256_file(target),
            "integrity_check": "ok",
            "pagination": True,
        }

    @staticmethod
    def _write_gzip_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as raw:
            temporary_path = Path(raw.name)
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                compressed.write(
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
        os.replace(temporary_path, path)

    @staticmethod
    def _read_gzip_json(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None

    def cache(self, analysis: dict[str, Any]) -> dict[str, Any]:
        analysis_id = str(analysis.get("analysis_id") or "")
        if not analysis_id.startswith("bpa_"):
            raise ValueError("invalid brand persona analysis_id")
        existing = self.get(analysis_id)
        if (
            existing
            and existing.get("content_hash") == analysis.get("content_hash")
            and (existing.get("persona_narrative") or {}).get("status")
            == "qwen_validated"
            and (analysis.get("persona_narrative") or {}).get("status")
            != "qwen_validated"
        ):
            analysis = deepcopy(analysis)
            incoming_personas = {
                str(row.get("persona_id") or ""): row
                for row in analysis.get("personas") or []
                if row.get("persona_id")
            }
            preserved_personas = deepcopy(existing.get("personas") or [])
            for row in preserved_personas:
                incoming = incoming_personas.get(str(row.get("persona_id") or ""))
                if not incoming:
                    continue
                for field in ("image", "context_image"):
                    if incoming.get(field):
                        row[field] = deepcopy(incoming[field])
            preserved_narrative = deepcopy(
                existing.get("persona_narrative") or {}
            )
            incoming_narrative = analysis.get("persona_narrative") or {}
            for field in (
                "images_provider",
                "image_providers",
                "persona_image_count",
                "persona_image_target",
                "image_status",
                "image_error",
                "images_are_illustrative",
            ):
                if field in incoming_narrative:
                    preserved_narrative[field] = deepcopy(incoming_narrative[field])
            analysis["personas"] = preserved_personas
            analysis["persona_narrative"] = preserved_narrative
        analysis = deepcopy(analysis)
        evidence = list(analysis.pop("evidence", []) or [])
        if evidence:
            analysis["evidence_storage"] = self._write_evidence(
                analysis_id, evidence
            )
        elif existing and existing.get("evidence_storage"):
            analysis["evidence_storage"] = deepcopy(existing["evidence_storage"])
        self._write_gzip_json(self.cache_root / f"{analysis_id}.json.gz", analysis)
        return analysis

    def get(self, analysis_id: str) -> dict[str, Any] | None:
        return self._read_gzip_json(self.cache_root / f"{analysis_id}.json.gz")

    def get_saved(self, artifact_id: str) -> dict[str, Any] | None:
        if not artifact_id.startswith("bpart_"):
            return None
        return self._read_gzip_json(self.saved_root / f"{artifact_id}.json.gz")

    def list_saved(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.saved_root.is_dir():
            return rows
        for path in self.saved_root.glob("bpart_*.json.gz"):
            artifact = self._read_gzip_json(path)
            if artifact is None:
                continue
            rows.append(
                {
                    "artifact_type": "brand_persona",
                    "artifact_id": artifact.get("artifact_id"),
                    "analysis_id": artifact.get("analysis_id"),
                    "title": artifact.get("title"),
                    "saved_at": artifact.get("saved_at"),
                    "schema_version": artifact.get("schema_version"),
                    "snapshot_id": artifact.get("snapshot_id"),
                    "content_hash": artifact.get("content_hash"),
                    "scope": deepcopy(artifact.get("scope") or {}),
                    "sample": deepcopy(artifact.get("sample") or {}),
                }
            )
        rows.sort(key=lambda row: str(row.get("saved_at") or ""), reverse=True)
        return rows

    def save(self, analysis_id: str, *, title: str = "") -> dict[str, Any]:
        analysis = self.get(analysis_id)
        if analysis is None:
            raise KeyError(analysis_id)
        artifact = deepcopy(analysis)
        artifact["artifact_type"] = "brand_persona"
        artifact["saved_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        artifact["title"] = title.strip() or f"{artifact['scope']['brand']} 品牌用户画像"
        artifact_id = "bpart_" + analysis_id.removeprefix("bpa_")
        artifact["artifact_id"] = artifact_id
        path = self.saved_root / f"{artifact_id}.json.gz"
        if path.exists():
            artifact = self._read_gzip_json(path) or artifact
        else:
            self._write_gzip_json(path, artifact)
            evidence_source = self._evidence_path(analysis_id)
            if evidence_source.is_file():
                shutil.copy2(
                    evidence_source,
                    self.saved_root / f"{artifact_id}.evidence.sqlite3",
                )
        return {
            "artifact_type": "brand_persona",
            "artifact_id": artifact_id,
            "analysis_id": analysis_id,
            "title": artifact["title"],
            "saved_at": artifact["saved_at"],
            "schema_version": artifact["schema_version"],
            "snapshot_id": artifact["snapshot_id"],
            "content_hash": artifact["content_hash"],
        }

    def update_personas(
        self,
        analysis_id: str,
        personas: list[dict[str, Any]],
        narrative: dict[str, Any],
    ) -> dict[str, Any]:
        analysis = self.get(analysis_id)
        if analysis is None:
            raise KeyError(analysis_id)
        analysis["personas"] = personas
        analysis["persona_narrative"] = narrative
        self.cache(analysis)
        return analysis

    def query_evidence(
        self,
        analysis_id: str,
        *,
        offset: int,
        limit: int,
        q: str = "",
        category_id: str = "",
        model: str = "",
        channel: str = "",
        market_region: str = "",
        rating: str = "",
        kind: str = "",
        label: str = "",
        evidence_ids: Iterable[str] = (),
    ) -> tuple[list[dict[str, Any]], int] | None:
        path = self._evidence_path(analysis_id)
        if not path.is_file():
            return None
        clauses: list[str] = []
        values: list[Any] = []
        requested = sorted({str(value) for value in evidence_ids if str(value)})
        if requested:
            clauses.append("evidence_id IN (" + ",".join("?" for _ in requested) + ")")
            values.extend(requested)
        if q:
            clauses.append(
                "(lower(quote) LIKE ? OR lower(matched_text) LIKE ? OR "
                "lower(label) LIKE ? OR lower(model) LIKE ? OR lower(category) LIKE ?)"
            )
            needle = f"%{q.casefold()}%"
            values.extend([needle] * 5)
        if category_id:
            clauses.append("category_id = ?")
            values.append(category_id)
        if model:
            clauses.append("model = ?")
            values.append(model)
        if channel:
            clauses.append("channel = ?")
            values.append(channel)
        if market_region:
            clauses.append("market_region = ?")
            values.append(market_region)
        if rating:
            clauses.append("rating = ?")
            values.append(int(rating))
        if kind:
            clauses.append("kind = ?")
            values.append(kind)
        if label:
            clauses.append("label = ?")
            values.append(label)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        uri = f"file:{path}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM evidence" + where, values
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT payload_json FROM evidence"
                + where
                + " ORDER BY evidence_id LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        finally:
            connection.close()
        return [json.loads(row[0]) for row in rows], total

    def evidence_facets(
        self,
        analysis_id: str,
        *,
        kind: str = "",
        label: str = "",
        evidence_ids: Iterable[str] = (),
    ) -> dict[str, list[dict[str, Any]]] | None:
        path = self._evidence_path(analysis_id)
        if not path.is_file():
            return None
        clauses: list[str] = []
        values: list[Any] = []
        requested = sorted({str(value) for value in evidence_ids if str(value)})
        if requested:
            clauses.append("evidence_id IN (" + ",".join("?" for _ in requested) + ")")
            values.extend(requested)
        if kind:
            clauses.append("kind = ?")
            values.append(kind)
        if label:
            clauses.append("label = ?")
            values.append(label)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        uri = f"file:{path}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            result: dict[str, list[dict[str, Any]]] = {}
            for field in ("category_id", "model", "channel", "market_region", "rating", "kind"):
                rows = connection.execute(
                    f"SELECT {field}, COUNT(*) FROM evidence{where} "
                    f"WHERE {field} != '' GROUP BY {field} ORDER BY COUNT(*) DESC, {field}",
                    values,
                ).fetchall() if not where else connection.execute(
                    f"SELECT {field}, COUNT(*) FROM evidence{where} "
                    f"AND {field} != '' GROUP BY {field} ORDER BY COUNT(*) DESC, {field}",
                    values,
                ).fetchall()
                result[field] = [
                    {"value": row[0], "count": int(row[1])} for row in rows
                ]
        finally:
            connection.close()
        return result

    def iter_evidence(
        self,
        analysis_id: str,
        *,
        q: str = "",
        category_id: str = "",
        model: str = "",
        channel: str = "",
        market_region: str = "",
        rating: str = "",
        kind: str = "",
        label: str = "",
        evidence_ids: Iterable[str] = (),
    ) -> Iterable[dict[str, Any]]:
        offset = 0
        while True:
            page = self.query_evidence(
                analysis_id,
                offset=offset,
                limit=200,
                q=q,
                category_id=category_id,
                model=model,
                channel=channel,
                market_region=market_region,
                rating=rating,
                kind=kind,
                label=label,
                evidence_ids=evidence_ids,
            )
            if page is None:
                return
            rows, _ = page
            if not rows:
                return
            yield from rows
            offset += len(rows)


def public_brand_persona_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """Remove full quote payloads; clients fetch evidence on demand."""
    value = deepcopy(analysis)
    evidence = value.pop("evidence", [])
    stored = value.get("evidence_storage") or {}
    value["evidence_access"] = {
        "endpoint": (
            "/reporting/api/v1/brand-persona/analyses/"
            f"{value['analysis_id']}/evidence"
        ),
        "evidence_rows": int(stored.get("evidence_rows") or len(evidence)),
        "pagination": True,
        "storage_schema_version": stored.get("schema_version"),
    }
    return value
