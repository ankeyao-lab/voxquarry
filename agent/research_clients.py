"""Trusted service clients. The model never supplies a service URL or token."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

from .config import validate_private_service_url
from .contracts import AgentError
from .upstream import private_http_error


# Versioned safe diagnostic vocabulary; never forward paths or exception text.
DATA_FIELDS = frozenset({'eligible_reviews', 'unique_reviews', 'valid_text_reviews',
    'collected_source_unique_reviews', 'source_records_collected', 'source_unique_reviews', 'source_total',
    'declared_source_total', 'source_missing_records', 'missing_review_count', 'excluded_reviews',
    'csv_eligible_rows', 'raw_source_rows'})
DATA_PHASES = frozenset({'catalog', 'snapshot_catalog', 'freeze', 'normalization', 'source_completeness',
    'query', 'aggregate', 'union', 'preparation', 'validation_manifest', 'artifact_access',
    'normalized_reviews', 'raw_reviews', 'artifact_hashing', 'attempt_identity'})
COMPLETENESS_REASONS = frozenset({'invalid_count', 'validation_notes_invalid', 'partial_validation_schema_invalid',
    'partial_import_schema_invalid', 'partial_channel_not_governed', 'partial_channel_mismatch',
    'collected_count_mismatch', 'eligible_excluded_mismatch', 'csv_count_mismatch', 'raw_count_mismatch',
    'declared_below_collected', 'missing_count_mismatch', 'unique_count_mismatch', 'source_reconciled_invalid',
    'partial_status_mismatch', 'partial_validation_status_mismatch', 'partial_empty_source',
    'partial_reconciled_true', 'partial_stop_reason_invalid', 'partial_missing_count_invalid',
    'complete_status_mismatch', 'complete_validation_status_invalid', 'complete_not_reconciled', 'source_contract_invalid'})


class ResearchDataClient:
    def __init__(self, transport):
        self.transport = transport

    def call(self, actor, operation, payload):
        if operation not in {"catalog", "open", "prepare", "manifest", "query", "aggregate", "definition", "analyze"}:
            raise AgentError("invalid_request", "数据操作无效")
        options = {"timeout_seconds": 300 if operation in {"open", "prepare", "analyze"} or (operation == 'aggregate' and 'dataset_ids' in payload) else 60}
        if operation == "analyze":
            # Full rule evidence is handed to the isolated workspace by
            # _tool_delivery; this is not a larger model context allowance.
            options["max_response_bytes"] = 8 * 1024**2
        if operation in {'catalog', 'open', 'prepare'}:
            payload = {**payload, 'catalog_contract': 'voxquarry.wenquan-catalog/v2'}
        value = self.transport._post("/reporting/api/v1/research-data/" + operation, payload, actor, **options)
        if value.get("error"):
            error = value["error"]
            code = error.get("code", "data_unavailable")
            retryable = False if code in {"unsafe_source", "invalid_source", "dataset_storage_unavailable", "catalog_storage_unavailable", "invalid_catalog", "source_changed", "catalog_expired"} else bool(error.get("retryable", code in {"dataset_busy", "analysis_busy", "catalog_busy", "catalog_unavailable", "wotu_catalog_timeout", "wotu_unavailable", "wotu_catalog_http_error"}))
            exception = AgentError(code, error.get("message", "暂时无法读取数据"), http_status=502, retryable=retryable)
            diagnostic = error.get('diagnostic') or {}
            request_id = diagnostic.get('request_id') if isinstance(diagnostic, dict) else None
            safe = {'code': code}
            if isinstance(request_id, str) and re.fullmatch(r'[a-f0-9]{32}', request_id):
                safe['request_id'] = request_id
            if isinstance(diagnostic, dict):
                if diagnostic.get('reason') in {'source_file_bytes', 'catalog_run_count', 'source_metadata_bytes',
                        'validation_json_invalid', 'source_not_published', 'source_status_mismatch',
                        'mixed_review_contracts', 'review_contract_invalid', 'catalog_sqlite_error',
                        'requested_sources_unavailable', 'no_available_sources', 'overlapping_version_conflict', 'mixed_normalization_versions',
                        'review_channel_mismatch', 'review_region_mismatch', 'review_text_flag_mismatch',
                        'validation_count_mismatch', 'source_completeness_mismatch', 'dataset_sqlite_error',
                        'catalog_source_unavailable', 'catalog_source_version_changed', 'catalog_total_deadline',
                        'preparation_deadline', 'preparation_receipt_bytes', 'preparation_receipt_invalid',
                        'identity_manifest_mismatch', 'normalization_invalid', 'product_form_invalid'}:
                    safe['reason'] = diagnostic['reason']
                    for key in ('observed', 'limit', 'expected'):
                        if type(diagnostic.get(key)) is int and 0 <= diagnostic[key] <= 2**53:
                            safe[key] = diagnostic[key]
                for key, allowed in (('phase', DATA_PHASES), ('field', DATA_FIELDS), ('source_reason', COMPLETENESS_REASONS)):
                    if isinstance(diagnostic.get(key), str) and diagnostic[key] in allowed:
                        safe[key] = diagnostic[key]
                if type(diagnostic.get('sqlite_code')) is int and 0 <= diagnostic['sqlite_code'] <= 28:
                    safe['sqlite_code'] = diagnostic['sqlite_code']
                if isinstance(diagnostic.get('counts'), dict):
                    safe['counts'] = {key: value for key, value in diagnostic['counts'].items()
                        if key in DATA_FIELDS and type(value) is int and 0 <= value <= 2**53}
                if isinstance(diagnostic.get('job_id'), str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', diagnostic['job_id']):
                    safe['job_id'] = diagnostic['job_id']
                if isinstance(diagnostic.get('validation_sha256'), str) and re.fullmatch(r'sha256:[a-f0-9]{64}', diagnostic['validation_sha256']):
                    safe['validation_sha256'] = diagnostic['validation_sha256']
            if len(safe) > 1:
                exception.upstream_diagnostic = safe
            raise exception
        return value

    def export(self, actor, dataset_id, shard, *, source_type="normalized_review"):
        if not isinstance(source_type, str) or source_type not in {"normalized_review", "channel_raw_payload"}:
            raise AgentError("dataset_export_invalid", "原文导出类型无效")
        maximum = 64 * 1024**2
        try:
            with httpx.Client(timeout=120, follow_redirects=False, trust_env=False) as client, client.stream("POST",
                    self.transport._base_url + "/reporting/api/v1/research-data/export",
                    headers=self.transport._headers(actor), json={"dataset_id": dataset_id, "shard": shard, "source_type": source_type}) as response:
                if response.status_code != 200:
                    raise AgentError("dataset_export_failed", "数据导出没有完成", http_status=502)
                size, chunks = 0, []
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > maximum:
                        raise AgentError("dataset_export_limit", "数据分片超过本次导出容量")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            raise AgentError("dataset_export_failed", "数据导出连接中断，请稍后重试", http_status=502, retryable=True) from exc


class ResearchCollectionClient:
    def __init__(self, base_url, token, *, client=None):
        self.base_url = validate_private_service_url(base_url, name="采集服务地址") if base_url else None
        self._token, self._client = token, client

    def call(self, operation, payload=None):
        if not self.base_url or not self._token:
            raise AgentError("collection_unavailable", "采集功能尚未连接", http_status=503)
        method, path = {
            "capabilities": ("GET", "/capabilities"), "resolve": ("POST", "/resolve"),
            "discover": ("POST", "/discover_products"),
            "ensure": ("POST", "/ensure_collection"), "status": ("GET", "/operations/"),
            "unsubscribe": ("POST", "/operations/")}.get(operation, (None, None))
        if method is None:
            raise AgentError("invalid_request", "采集操作无效")
        data = dict(payload or {})
        if operation in {"status", "unsubscribe"}:
            key = data.pop("request_key", "")
            if not isinstance(key, str) or not key or len(key) > 180:
                raise AgentError("invalid_request", "采集操作标识无效")
            path += quote(key, safe="") + ("/cancel_subscription" if operation == "unsubscribe" else "")
        try:
            with self._client or httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
                response = client.request(method, self.base_url + "/api/research" + path,
                    headers={"Authorization": "Bearer " + self._token}, **({"json": data} if method == "POST" else {"params": data}))
                if len(response.content) > 2_000_000:
                    raise AgentError("collection_response_limit", "采集服务响应过大", http_status=502)
                if response.status_code != 200:
                    code = None
                    try:
                        detail = response.json().get('detail', {})
                        candidate = detail.get('code') if isinstance(detail, dict) else None
                        if candidate in {'collection_schema_unavailable', 'invalid_retry', 'request_key_conflict', 'collection_conflict', 'operation_not_found', 'external_collection_required'}:
                            code = candidate
                    except (ValueError, AttributeError):
                        pass
                    error = private_http_error(response.status_code, response.headers, service='collection', code=code)
                    if code == 'collection_schema_unavailable':
                        error.retryable = False
                    raise error
                try:
                    value = response.json()
                except ValueError as exc:
                    raise AgentError('collection_contract_invalid', '采集服务响应不是有效 JSON', http_status=502) from exc
                if not isinstance(value, dict):
                    raise AgentError('collection_contract_invalid', '采集服务响应形状无效', http_status=502)
                return value
        except httpx.HTTPError as exc:
            raise AgentError("collection_unavailable", "采集服务暂时无法连接", http_status=503, retryable=True) from exc


class ResearchAuthorityClient:
    """Private server-side principal revalidation; never accepts browser claims."""
    def __init__(self, base_url, token, *, transport=None, clock=None, expected_mode="local_dev_account_admin"):
        if expected_mode not in {"local_dev_account_admin", "production_account_admin"}:
            raise AgentError("authority_config_invalid", "账号复验模式无效", http_status=503)
        self.expected_mode = expected_mode
        self.base_url = validate_private_service_url(base_url, name="账号复验服务地址") if base_url else None
        if self.base_url and urlsplit(self.base_url).path not in {"", "/"}:
            raise AgentError("authority_config_invalid", "账号复验服务只能配置固定根地址", http_status=503)
        self._token, self._transport = token, transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def authorize(self, principal_id, required_capability, *, expected_authorization_version=None):
        if not self.base_url or not isinstance(self._token, str) or len(self._token) < 32 or any(c.isspace() for c in self._token):
            raise AgentError("authority_unavailable", "账号复验服务尚未配置", http_status=503)
        if not isinstance(principal_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", principal_id) or not isinstance(required_capability, str) or required_capability not in {"chat.session.own", "collect.command"}:
            raise AgentError("authority_request_invalid", "账号复验请求无效")
        try:
            with httpx.Client(transport=self._transport, timeout=httpx.Timeout(30, connect=5), follow_redirects=False, trust_env=False) as client:
                with client.stream("POST", self.base_url + "/account-admin/internal/research-authorize",
                        headers={"Authorization": "Bearer " + self._token},
                        json={"principal_id": principal_id, "required_capability": required_capability}) as response:
                    if response.status_code != 200:
                        raise AgentError("research_authorization_denied", "账号授权需要重新确认，研究已停止自动继续", http_status=403 if response.status_code in {401, 403, 409} else 503, retryable=response.status_code in {429, 502, 503, 504})
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 16_384:
                            raise ValueError("oversized authority response")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("invalid authority response")
            roles, version, ttl = value.get("roles"), value.get("authorization_version"), value.get("ttl_seconds")
            if value.get("schema_version") != "voxquarry.research-authority/v1" or value.get("principal_id") != principal_id or value.get("required_capability") != required_capability or value.get("allowed") is not True or value.get("owner_scope") != "principal:" + principal_id or value.get("mode") != self.expected_mode:
                raise ValueError("authority identity mismatch")
            if not isinstance(roles, list) or not roles or any(role not in {"user", "admin"} for role in roles) or not isinstance(version, str) or not re.fullmatch(r"authv_[0-9a-f]{64}", version) or type(ttl) is not int or not 1 <= ttl <= 60:
                raise ValueError("authority claims invalid")
            checked = datetime.fromisoformat(str(value.get("checked_at", "")).replace("Z", "+00:00"))
            if checked.tzinfo is None:
                raise ValueError("authority timestamp missing timezone")
            age = (self._clock() - checked).total_seconds()
            if age < -5 or age >= ttl:
                raise ValueError("authority response expired")
            if expected_authorization_version is not None and version != expected_authorization_version:
                raise AgentError("research_authorization_changed", "账号授权已变化，请重新确认后继续研究", http_status=403)
            public = {"schema_version", "principal_id", "required_capability", "roles", "allowed", "owner_scope", "mode", "authorization_version", "checked_at", "ttl_seconds", "reason"}
            return {key: value[key] for key in public if key in value}
        except httpx.HTTPError as exc:
            raise AgentError("authority_unavailable", "账号复验暂时无法完成，研究不会自动继续", http_status=503, retryable=True) from exc
        except (ValueError, TypeError) as exc:
            raise AgentError("authority_response_invalid", "账号复验结果无效，研究不会自动继续", http_status=503) from exc
