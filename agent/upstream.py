from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from .config import validate_private_service_url
from .contracts import AgentError, IdentityContext, new_id, sha256_value
from .store import WenquanStore
from .tool_registry import ToolDefinition, ToolRegistry


def private_http_error(status, headers, *, service="upstream", code=None):
    """Keep bounded transport facts, never a private response body or URL."""
    transient = status in {408, 429, 502, 503, 504}
    suffix = 'forbidden' if status in {401, 403} else 'server_error' if status >= 500 and not transient else 'unavailable' if transient else 'rejected'
    error = AgentError(code or service + '_' + suffix,
        '采集服务未能完成请求，请检查服务状态。' if service == 'collection' else '数据服务未能完成请求，请检查服务状态。',
        http_status=status if status in {401, 403, 409, 422, 429, 500, 502, 503, 504} else 502,
        retryable=transient)
    error.upstream_diagnostic = {'status': status}
    request_id = headers.get('x-request-id', '')
    if re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', request_id):
        error.upstream_diagnostic['request_id'] = request_id
    content_type = headers.get('content-type', '').split(';')[0]
    if re.fullmatch(r'[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+', content_type):
        error.upstream_diagnostic['content_type'] = content_type
    return error

try:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError
except ImportError:  # pragma: no cover
    Draft202012Validator = None  # type: ignore[assignment]
    ValidationError = Exception  # type: ignore[assignment,misc]


class ZhuojianTransport(Protocol):
    dependency_status: str

    def call(
        self,
        tool_name: str,
        payload: Mapping[str, Any],
        identity: IdentityContext,
        definition: ToolDefinition,
    ) -> dict[str, Any]: ...

    def validate_citations(
        self, citations: list[dict[str, Any]], identity: IdentityContext
    ) -> dict[str, Any]: ...


class HttpZhuojianTransport:
    dependency_status = "ready"

    def __init__(
        self,
        *,
        base_url: str,
        gateway_token: str,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        if not gateway_token:
            raise AgentError("dependency_config_invalid", "分析报告 Gateway token 未配置", http_status=503)
        self._base_url = validate_private_service_url(base_url, name="分析报告服务地址")
        self._token = gateway_token
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes

    def _headers(self, identity: IdentityContext) -> dict[str, str]:
        reports = (
            "*"
            if identity.allowed_report_ids is None
            else ",".join(sorted(identity.allowed_report_ids))
        )
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-VoxQuarry-Gateway-Token": self._token,
            "X-VoxQuarry-Authority-Id": identity.authority_id,
            "X-VoxQuarry-Principal-Id": identity.owner_user_id,
            "X-VoxQuarry-Allowed-Report-Ids": reports,
            "X-VoxQuarry-Allow-Archived": str(identity.allow_archived_reports).lower(),
        }

    def _post(self, path: str, payload: Mapping[str, Any], identity: IdentityContext, *, timeout_seconds: float | None = None, max_response_bytes: int | None = None) -> dict[str, Any]:
        # The private dataset analysis includes rule evidence for the whole
        # frozen product. Its caller supplies a bounded transport allowance;
        # legacy report tools keep their existing smaller response ceiling.
        maximum = self._max_response_bytes if max_response_bytes is None else max_response_bytes
        if type(maximum) is not int or not 1 <= maximum <= 8 * 1024**2:
            raise AgentError("upstream_contract_invalid", "分析报告响应容量配置无效", http_status=503)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self._base_url + path,
            data=body,
            headers=self._headers(identity),
            method="POST",
        )
        status, response_headers = 200, {}
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds or self._timeout) as response:
                raw = response.read(maximum + 1)
                response_headers = getattr(response, 'headers', {})
        except urllib.error.HTTPError as exc:
            raw = exc.read(maximum + 1)
            status, response_headers = exc.code, exc.headers
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentError(
                "upstream_unavailable",
                "分析报告工具暂不可用",
                http_status=503,
                retryable=True,
            ) from exc
        if len(raw) > maximum:
            raise AgentError("upstream_contract_invalid", "分析报告响应超过大小限制", http_status=502)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            if status >= 400:
                raise private_http_error(status, response_headers) from exc
            raise AgentError("upstream_contract_invalid", "分析报告响应不是有效 JSON", http_status=502) from exc
        if not isinstance(value, dict):
            raise AgentError("upstream_contract_invalid", "分析报告响应形状无效", http_status=502)
        if status >= 400 and not isinstance(value.get('error'), dict):
            raise private_http_error(status, response_headers)
        return value

    def call(
        self,
        tool_name: str,
        payload: Mapping[str, Any],
        identity: IdentityContext,
        definition: ToolDefinition,
    ) -> dict[str, Any]:
        encoded_name = urllib.parse.quote(tool_name, safe=".@_-" )
        return self._post(f"/reporting/api/v1/agent-tools/{encoded_name}", payload, identity,
                          timeout_seconds=definition.timeout_ms / 1000)

    def validate_citations(
        self, citations: list[dict[str, Any]], identity: IdentityContext
    ) -> dict[str, Any]:
        return self._post(
            "/reporting/api/v1/agent-citations/validate",
            {"citations": citations},
            identity,
        )

    def report_workflow(self, operation: str, payload: dict[str, Any], identity: IdentityContext) -> dict[str, Any]:
        if operation not in {"catalog", "ensure", "status", "cancel"}:
            raise AgentError("invalid_request", "报告准备动作无效。")
        result = self._post("/reporting/api/v1/agent-reports/" + operation, payload, identity, timeout_seconds=120)
        if result.get("error"):
            error = result["error"]
            raise AgentError(str(error.get("code") or "report_generation_failed"), str(error.get("message") or "报告准备未完成。"), http_status=409, retryable=bool(error.get("retryable")))
        if result.get("contract") != "wenquan-report-workflow/v1":
            raise AgentError("upstream_contract_invalid", "报告准备服务返回异常。", http_status=502)
        return result


@dataclass(frozen=True)
class ToolExecution:
    request_id: str
    audit_id: str
    result: dict[str, Any]


class ToolExecutor:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        transport: ZhuojianTransport,
        store: WenquanStore,
        envelope_schema_path: str,
        allow_synthetic: bool = False,
    ) -> None:
        self.registry = registry
        self.transport = transport
        self.store = store
        self.allow_synthetic = allow_synthetic
        self._envelope_schema = json.loads(
            Path(envelope_schema_path).read_text(encoding="utf-8")
        )

    def _allowed(self, definition: ToolDefinition) -> None:
        if definition.dependency_status == "ready":
            return
        if definition.dependency_status == "synthetic_test_only" and self.allow_synthetic:
            return
        raise AgentError(
            "capability_unavailable",
            "分析报告只读工具尚未接入可信运行边界",
            http_status=503,
            retryable=True,
        )

    def _validate_envelope(self, result: dict[str, Any], tool_name: str) -> None:
        if Draft202012Validator is None:
            raise AgentError("schema_validator_unavailable", "上游响应校验器不可用", http_status=503)
        try:
            Draft202012Validator(self._envelope_schema).validate(result)
        except ValidationError as exc:
            raise AgentError("upstream_contract_invalid", "分析报告响应不符合 envelope", http_status=502) from exc
        if result.get("tool") != tool_name:
            raise AgentError("upstream_contract_invalid", "分析报告响应工具身份不一致", http_status=502)
        if result.get("status") == "error":
            error = result.get("error") or {}
            code = str(error.get("code") or "upstream_error")
            raise AgentError(
                code,
                str(error.get("message") or "分析报告工具执行失败"),
                http_status={"report_forbidden": 403, "report_not_found": 404,
                             "report_section_not_found": 404, "research_source_unavailable": 409,
                             "research_source_invalid": 409, "research_source_changed": 409,
                             "report_review_too_large": 409}.get(code, 502),
                retryable=bool(error.get("retryable")),
            )

    def invoke(
        self,
        identity: IdentityContext,
        *,
        tool_name: str,
        payload: Mapping[str, Any],
        session_id: str | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> ToolExecution:
        definition = self.registry.validate_input(tool_name, payload)
        self._allowed(definition)
        request_id = new_id("req")
        audit_id = new_id("aud")
        input_hash = sha256_value(payload)
        self.store.append_audit_event(
            identity,
            audit_id=audit_id,
            event_type="tool_call.started",
            status="started",
            request_id=request_id,
            session_id=session_id,
            run_id=run_id,
            step_id=step_id,
            tool_name=tool_name,
            input_hash=input_hash,
        )
        started = time.monotonic()
        try:
            result = self.transport.call(tool_name, payload, identity, definition)
            self._validate_envelope(result, tool_name)
        except AgentError as error:
            self.store.append_audit_event(
                identity,
                audit_id=audit_id,
                event_type="tool_call.finished",
                status="error",
                request_id=request_id,
                session_id=session_id,
                run_id=run_id,
                step_id=step_id,
                tool_name=tool_name,
                input_hash=input_hash,
                error_code=error.code,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        upstream_ids = []
        report = result.get("report")
        snapshot = result.get("snapshot")
        if isinstance(report, dict) and report.get("report_id"):
            upstream_ids.append(str(report["report_id"]))
        if isinstance(snapshot, dict) and snapshot.get("snapshot_id"):
            upstream_ids.append(str(snapshot["snapshot_id"]))
        self.store.append_audit_event(
            identity,
            audit_id=audit_id,
            event_type="tool_call.finished",
            status="ok",
            request_id=request_id,
            session_id=session_id,
            run_id=run_id,
            step_id=step_id,
            tool_name=tool_name,
            input_hash=input_hash,
            output_hash=sha256_value(result),
            upstream_ids=upstream_ids,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return ToolExecution(request_id=request_id, audit_id=audit_id, result=result)

    def validate_citations(
        self,
        identity: IdentityContext,
        citations: list[dict[str, Any]],
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> ToolExecution:
        if self.registry.dependency_status != "ready" and not (
            self.registry.dependency_status == "synthetic_test_only" and self.allow_synthetic
        ):
            raise AgentError("capability_unavailable", "citation validator 不可用", http_status=503)
        if not isinstance(citations, list) or len(citations) > 50:
            raise AgentError("invalid_request", "citations 必须是最多 50 项的数组")
        request_id = new_id("req")
        audit_id = new_id("aud")
        input_hash = sha256_value(citations)
        self.store.append_audit_event(
            identity,
            audit_id=audit_id,
            event_type="tool_call.started",
            status="started",
            request_id=request_id,
            session_id=session_id,
            run_id=run_id,
            step_id=step_id,
            tool_name="zhuojian.citation_validator@v1",
            input_hash=input_hash,
        )
        started = time.monotonic()
        try:
            result = self.transport.validate_citations(citations, identity)
            if (
                not isinstance(result, dict)
                or result.get("schema_version") != "zhuojian-citation-validation/v1"
                or not isinstance(result.get("valid"), bool)
                or not isinstance(result.get("items"), list)
            ):
                raise AgentError("upstream_contract_invalid", "引用校验响应无效", http_status=502)
        except AgentError as error:
            self.store.append_audit_event(
                identity,
                audit_id=audit_id,
                event_type="tool_call.finished",
                status="error",
                request_id=request_id,
                session_id=session_id,
                run_id=run_id,
                step_id=step_id,
                tool_name="zhuojian.citation_validator@v1",
                input_hash=input_hash,
                error_code=error.code,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        self.store.append_audit_event(
            identity,
            audit_id=audit_id,
            event_type="tool_call.finished",
            status="ok" if result["valid"] else "error",
            request_id=request_id,
            session_id=session_id,
            run_id=run_id,
            step_id=step_id,
            tool_name="zhuojian.citation_validator@v1",
            input_hash=input_hash,
            output_hash=sha256_value(result),
            error_code=None if result["valid"] else "citation_invalid",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return ToolExecution(request_id=request_id, audit_id=audit_id, result=result)
