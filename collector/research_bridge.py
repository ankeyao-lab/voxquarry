"""Trusted Wenquan-to-collector bridge. This is not a model-facing HTTP proxy.

The caller authenticates the user and collect.command capability. A dedicated
service token authenticates this hop; no paths, commands, IDs from another
channel, or arbitrary collector actions are accepted in mutation requests.
"""
import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
from collections import OrderedDict
from contextlib import ExitStack
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from .store import canonical_target_url

SCHEMA = "voxquarry.research-collection/v1"
ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
ARTIFACTS = ("validation_report.json", "reviews.csv", "reviews.raw.jsonl")
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
_READY_CACHE = OrderedDict()
_READY_LOCK = threading.Lock()


def _error(code, message, status=422):
    raise HTTPException(status, {"code": code, "message": message})


def authenticate(request):
    expected = os.getenv("VOXQUARRY_RESEARCH_BRIDGE_TOKEN", "").strip()
    if not expected:
        _error("research_bridge_unconfigured", "Research bridge is not configured", 503)
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + expected).encode("utf-8")):
        _error("research_bridge_unauthorized", "Invalid research bridge credential", 401)


def _identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        _error("invalid_identifier", "Invalid research identifier")
    return value


def _body(value, allowed, required):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        _error("invalid_request", "Unexpected or missing request fields")
    if value.get("schema_version", SCHEMA) != SCHEMA:
        _error("unsupported_schema", "Unsupported research collection schema")
    return value


def _public_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        _error("invalid_product_url", "Invalid product URL")
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password and parsed.port in {None, 80, 443}
    except ValueError:
        valid = False
    if not valid:
        _error("invalid_product_url", "A public product URL is required")
    return value


async def _request_json(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16_384:
            _error("request_too_large", "Research request exceeds the size limit", 413)
    try:
        return json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        _error("invalid_json", "Research request must be valid JSON")


def _stat_key(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _open_artifacts(data_dir, parts, stack):
    # Open every path component without following links. Keep directory FDs
    # until verification finishes, including when the worker archives a run.
    directory = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stack.callback(os.close, directory)
    for part in parts:
        if part in {"", ".", ".."} or "/" in part or "\\" in part:
            raise ValueError("unsafe artifact component")
        directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        stack.callback(os.close, directory)
    handles = {}
    for name in ARTIFACTS:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        handle = stack.enter_context(os.fdopen(descriptor, "rb"))
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("unsupported or oversized artifact")
        handles[name] = (handle, _stat_key(info))
    return handles


def verify_attempt(store, job, attempt, *, metadata_only=False):
    """Verify exact terminal-attempt files; never substitute an old success."""
    number = int(attempt.get("attempt_no") or 0)
    status = attempt.get("status")
    unavailable = {"ready": False, "reason": "attempt_not_complete"}
    if number < 1 or status not in {"completed", "partial_success"}:
        return unavailable
    job_id, channel = str(job["id"]), str(job["channel"])
    if not ID.fullmatch(job_id) or not ID.fullmatch(channel):
        return {"ready": False, "reason": "invalid_artifact_identity"}
    stable = f"{job_id}/{channel}"
    archived = f"{job_id}/attempts/{number}/{channel}"
    if int(job.get("active_attempt_no") or 0) == number:
        relative = job.get("active_run_path")
        if relative not in {stable, archived}:
            return {"ready": False, "reason": "invalid_attempt_path"}
    elif int(job.get("attempt_count") or 0) == number:
        relative = stable
    else:
        relative = archived
    if metadata_only:
        # This is a task/attempt pointer, not an availability claim. Reporting
        # verifies the physical manifest/header in its mounted source root once;
        # opening still performs the exact-attempt full hash/count checks below.
        selected_job = {k: v for k, v in job.items() if k not in {'result', 'result_json'}}
        selected_job.update(status=status, active_attempt_no=number,
            active_result_status=status, active_run_path=relative,
            finished_at=attempt.get('finished_at'))
        return {'ready': False, 'verification': 'deferred',
                'selected_run': {'job': selected_job, 'run_dir': 'runs/' + relative}}
    report = (attempt.get("result") or {}).get(channel)
    if not isinstance(report, dict):
        return {"ready": False, "reason": "attempt_result_missing"}
    phase = 'artifact_access'
    detail = {}
    try:
        with ExitStack() as stack:
            handles = _open_artifacts(store.data_dir, ["runs", *relative.split("/")], stack)
            metadata_hash = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
            cache_key = (str(store.data_dir), job_id, number, status, relative, job.get('source_product_id'), metadata_hash,
                         tuple((n, info) for n, (_, info) in handles.items()))
            with _READY_LOCK:
                cached = _READY_CACHE.get(cache_key)
                if cached is not None:
                    _READY_CACHE.move_to_end(cache_key)
            if cached is None:
                phase = 'validation_manifest'
                validation_handle = handles[ARTIFACTS[0]][0]
                if handles[ARTIFACTS[0]][1][2] > 5_000_000:
                    raise ValueError("validation too large")
                physical = json.load(validation_handle)
                expected = {"completed": {"COMPLETE", "COMPLETE_FALLBACK_SOURCE"},
                            "partial_success": {"PARTIAL_SUCCESS"}}[status]
                if physical.get("status") not in expected or physical.get("channel", channel) != channel:
                    raise ValueError("validation status mismatch")
                physical_product = physical.get("product") or {}
                cached_product = report.get("product") or {}
                if physical_product.get("id") != cached_product.get("id"):
                    raise ValueError("attempt product identity mismatch")
                if job.get("source_product_id") and str(physical_product.get("id") or "") != str(job["source_product_id"]):
                    raise ValueError("task product identity mismatch")
                for field in ("status", "unique_reviews", "source_unique_reviews", "source_total"):
                    if report.get(field) != physical.get(field):
                        raise ValueError("attempt validation mismatch")
                expected_reviews = physical.get("unique_reviews")
                if type(expected_reviews) is not int or expected_reviews < 0:
                    detail = {'field': 'unique_reviews'}
                    raise ValueError("invalid valid-review count")
                csv_handle = io.TextIOWrapper(handles["reviews.csv"][0], encoding="utf-8-sig", newline="")
                try:
                    phase = 'normalized_reviews'
                    reader = csv.DictReader(csv_handle)
                    if not reader.fieldnames or not {"review_id", "review_text"} <= set(reader.fieldnames):
                        raise ValueError("unsupported reviews CSV schema")
                    count = 0
                    # Prove the declared unique count without retaining every
                    # review ID in Python memory. Bound the temporary index.
                    directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="gt-research-verify-"))
                    index = sqlite3.connect(os.path.join(directory, "ids.sqlite3"))
                    stack.callback(index.close)
                    index.execute("PRAGMA journal_mode=OFF")
                    index.execute("PRAGMA max_page_count=65536")
                    index.execute("CREATE TABLE ids(id TEXT PRIMARY KEY) WITHOUT ROWID")
                    for row in reader:
                        if not row.get("review_id") or None in row:
                            raise ValueError("invalid normalized review")
                        index.execute("INSERT INTO ids VALUES(?)", (row["review_id"],))
                        count += 1
                    if count != expected_reviews:
                        detail = {'field': 'unique_reviews', 'expected': count, 'observed': expected_reviews}
                        raise ValueError("CSV count does not match validation")
                finally:
                    csv_handle.detach()
                raw_handle = handles["reviews.raw.jsonl"][0]
                phase = 'raw_reviews'
                raw_count = 0
                while True:
                    line = raw_handle.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_LINE_BYTES or not line.strip():
                        raise ValueError("invalid raw JSONL record")
                    json.loads(line)
                    raw_count += 1
                declared_raw = physical.get("source_unique_reviews")
                if type(declared_raw) is int and raw_count != declared_raw:
                    detail = {'field': 'source_unique_reviews', 'expected': raw_count, 'observed': declared_raw}
                    raise ValueError("raw record count does not match validation")
                artifact_metadata = []
                phase = 'artifact_hashing'
                for name, (handle, before) in handles.items():
                    handle.seek(0)
                    digest = hashlib.sha256()
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                    if before != _stat_key(os.fstat(handle.fileno())):
                        raise ValueError("artifact changed during verification")
                    artifact_metadata.append({"name": name, "bytes": before[2], "sha256": "sha256:" + digest.hexdigest()})
                cached = {"artifacts": artifact_metadata, "valid_reviews": count,
                          "raw_records": raw_count, "completeness": "partial" if status == "partial_success" else "complete"}
                with _READY_LOCK:
                    _READY_CACHE[cache_key] = cached
                    while len(_READY_CACHE) > 1024:
                        _READY_CACHE.popitem(last=False)
            # Detect a retry/archive while files were being read. A caller can
            # retry status; it must not receive a now-unowned stable path.
            phase = 'attempt_identity'
            latest = store.get_job(job_id, metadata_only=True)
            if not latest or any(latest.get(k) != job.get(k) for k in ("attempt_count", "active_attempt_no", "active_run_path", "last_attempt_status", "source_product_id")):
                raise ValueError("attempt changed during verification")
            selected_job = {key: value for key, value in job.items() if key not in {'result', 'result_json'}}
            selected_job.update(status=status, active_attempt_no=number,
                active_result_status=status, active_run_path=relative,
                finished_at=attempt.get("finished_at"))
            return {"ready": True, **cached, "selected_run": {"job": selected_job, "run_dir": "runs/" + relative}}
    except (OSError, ValueError, TypeError, KeyError, csv.Error, UnicodeError, RecursionError, sqlite3.Error) as exc:
        allowed_types = {'OSError', 'FileNotFoundError', 'PermissionError', 'ValueError', 'TypeError', 'KeyError',
                         'Error', 'UnicodeError', 'UnicodeDecodeError', 'RecursionError', 'OperationalError', 'IntegrityError'}
        diagnostic = {'phase': phase, 'error_type': type(exc).__name__ if type(exc).__name__ in allowed_types else 'Error'}
        if detail.get('field') in {'unique_reviews', 'source_unique_reviews'}:
            diagnostic['field'] = detail['field']
        diagnostic.update({key: value for key, value in detail.items()
                           if key in {'expected', 'observed'} and type(value) is int and 0 <= value <= 2**63 - 1})
        if isinstance(exc, sqlite3.Error):
            code = getattr(exc, 'sqlite_errorcode', {'database or disk is full': 13, 'database is locked': 5,
                'database table is locked': 6, 'interrupted': 9}.get(str(exc), 0)) & 255
            if 0 <= code <= 28:
                diagnostic['sqlite_code'] = code
        return {"ready": False, "reason": "artifact_verification_failed",
                "diagnostic": diagnostic}


class ResearchBridge:
    def __init__(self, get_store, create_job, resolve, job_request, collection_channels, discover=None):
        self.get_store, self.create_job, self.resolve = get_store, create_job, resolve
        self.job_request, self.collection_channels = job_request, set(collection_channels)
        self.product_discovery = discover

    def discover(self, payload):
        data = _body(payload, {'brand', 'model', 'channel'}, {'brand', 'model'})
        brand, model, channel = data['brand'], data['model'], data.get('channel', 'all')
        if (not isinstance(brand, str) or not re.fullmatch(r'[A-Za-z\u3400-\u9fff][A-Za-z\u3400-\u9fff .-]{1,39}', brand)
                or not isinstance(model, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{2,23}', model)
                or not any(c.isdigit() for c in model) or channel not in {'all', 'lowes', 'ace', 'ego', 'homedepot', 'walmart', 'amazon'}):
            _error('invalid_product_search', 'Exact public brand and model are required')
        if self.product_discovery is None:
            _error('product_discovery_unavailable', 'Product discovery is not configured', 503)
        channels = ['lowes', 'ace', 'ego'] if channel == 'all' else [channel]
        return {'schema_version': SCHEMA, **self.product_discovery(brand, model, channels)}

    def ensure(self, payload):
        data = _body(payload, {"schema_version", "request_key", "research_run_id", "channel", "product_url", "retry", "expected_attempt_no"},
                     {"request_key", "research_run_id", "channel", "product_url"})
        request_key, run_id = _identifier(data["request_key"]), _identifier(data["research_run_id"])
        url = _public_url(data["product_url"])
        channel = _identifier(data["channel"])
        if channel not in self.collection_channels or channel == "amazon_reviews_2023":
            _error("external_collection_required", "This source cannot be automatically collected", 409)
        retry = data.get("retry", False)
        expected = data.get("expected_attempt_no")
        if type(retry) is not bool or (retry and (type(expected) is not int or expected < 1)) or (not retry and expected is not None):
            _error("invalid_retry", "Retry requires the exact previous attempt number")
        canonical = canonical_target_url(url)
        operation = {"request_key": request_key, "research_run_id": run_id,
                     "retry": retry, "expected_attempt_no": expected}
        operation["payload_hash"] = hashlib.sha256(json.dumps({**operation, "channel": channel, "target": canonical}, sort_keys=True).encode()).hexdigest()
        store = self.get_store()
        existing = store.research_operation(request_key, run_id)
        if existing:
            if existing["payload_hash"] != operation["payload_hash"]:
                _error("request_key_conflict", "Request key is already bound to different inputs", 409)
            return self.status(request_key, run_id)
        try:
            self.create_job(self.job_request(action="scrape", channel=channel, product_url=url), research_operation=operation)
        except ValueError as exc:
            _error("collection_conflict", str(exc), 409)
        return self.status(request_key, run_id)

    def status(self, request_key, research_run_id):
        store = self.get_store()
        operation = store.research_operation(_identifier(request_key), _identifier(research_run_id))
        if operation is None:
            _error("operation_not_found", "Research collection operation not found", 404)
        job = store.get_job(operation["job_id"])
        attempt = store.research_attempt(operation["job_id"], operation["attempt_no"])
        result = {"schema_version": SCHEMA, **{k: v for k, v in operation.items() if k != "payload_hash"},
                  "status": attempt.get("status") if attempt else "unavailable",
                  "attempt_policy": store.research_attempt_policy(operation["job_id"], operation["attempt_no"])}
        if job:
            result.update(channel=job.get("channel"), product_url=job.get("target_url"),
                          latest_attempt_no=job.get("attempt_count"),
                          latest_attempt_status=job.get("last_attempt_status"),
                          energy_readiness_status=job.get("energy_readiness_status"),
                          source_product_identity_status=job.get("source_product_identity_status"))
        result["data"] = verify_attempt(store, job, attempt) if job and attempt else {"ready": False, "reason": "attempt_missing"}
        return result

    def tasks(self, limit=25, cursor=None, job_id=None, filters=None, verify=True):
        store = self.get_store()
        if job_id:
            job = store.get_job(_identifier(job_id), metadata_only=not verify)
            page = {'items': [job] if job else [], 'next_cursor': None}
        else:
            page = store.list_jobs_page(limit=min(max(int(limit), 1), 50), cursor=cursor,
                                        filters=filters, metadata_only=not verify)
        items = []
        for job in page["items"]:
            number = int(job.get("active_attempt_no") or job.get("attempt_count") or 0)
            attempt = store.research_attempt(job["id"], number, metadata_only=not verify)
            items.append({"job_id": job["id"], "channel": job["channel"], "product_url": job.get("target_url"),
                          "scope_job": {k: v for k, v in job.items() if k in {'id', 'channel', 'brand', 'model', 'category', 'category_id', 'market_region', 'product_observation_hash'} or k.startswith(('voltage_platform_', 'energy_'))},
                          "brand": job.get("brand"), "model": job.get("model"), "category": job.get("category"),
                          "attempt_no": number or None, "status": attempt.get("status") if attempt else "unavailable",
                          "latest_attempt_no": job.get("attempt_count"), "latest_attempt_status": job.get("last_attempt_status"),
                          "data": (verify_attempt(store, job, attempt) if verify else verify_attempt(store, job, attempt, metadata_only=True)) if attempt else {"ready": False, "reason": "attempt_missing"}})
        return {**page, "schema_version": SCHEMA, "items": items}

    def detach(self, request_key, research_run_id):
        self.get_store().detach_research_subscription(_identifier(request_key), _identifier(research_run_id))
        return self.status(request_key, research_run_id)


def register_research_bridge(app, bridge):
    @app.post('/api/research/discover_products')
    async def discover_products(request: Request):
        authenticate(request)
        return await asyncio.to_thread(bridge.discover, await _request_json(request))

    @app.get("/api/research/capabilities")
    def capabilities(request: Request):
        authenticate(request)
        if not bridge.get_store().research_schema_ready():
            _error('collection_schema_unavailable', 'Research collection storage is not ready', 503)
        return {"schema_version": SCHEMA, "auto_collection_channels": sorted(bridge.collection_channels - {"amazon_reviews_2023"}),
                "operation_idempotency": True, "cancel_policy": "detach_subscription_only",
                "new_attempt_firecrawl_allowed": False, "user_authorization": "trusted_upstream_collect.command"}

    @app.post("/api/research/resolve")
    async def resolve(request: Request):
        authenticate(request)
        data = _body(await _request_json(request), {"schema_version", "product_url"}, {"product_url"})
        value = await asyncio.to_thread(bridge.resolve, _public_url(data["product_url"]))
        return {"schema_version": SCHEMA, "product": value,
                "auto_collectable": value.get("channel") in bridge.collection_channels - {"amazon_reviews_2023"}}

    @app.get("/api/research/tasks")
    def tasks(request: Request, limit: int = 25, cursor: str = None, job_id: str = None, filters: str = None, verify: bool = True):
        authenticate(request)
        try:
            if filters and len(filters) > 32000:
                raise ValueError('task filters too large')
            return bridge.tasks(limit, cursor, job_id, json.loads(filters) if filters else None, verify=verify)
        except ValueError as exc:
            _error("invalid_cursor", str(exc))

    @app.post("/api/research/ensure_collection")
    async def ensure(request: Request):
        authenticate(request)
        return await asyncio.to_thread(bridge.ensure, await _request_json(request))

    @app.get("/api/research/operations/{request_key}")
    def status(request_key: str, request: Request, research_run_id: str):
        authenticate(request)
        return bridge.status(request_key, research_run_id)

    @app.post("/api/research/operations/{request_key}/cancel_subscription")
    async def cancel(request_key: str, request: Request):
        authenticate(request)
        data = _body(await _request_json(request), {"schema_version", "research_run_id"}, {"research_run_id"})
        return await asyncio.to_thread(bridge.detach, request_key, data["research_run_id"])
