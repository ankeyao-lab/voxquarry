from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, Mapping

from reporting.source import canonical_json, sha256_text
from reporting.research_data import (
    QUERY_KEYS,
    MAX_RESEARCH_RESPONSE_BYTES,
    FrozenReviewData,
    ResearchDataError,
    aggregate_reviews,
    query_reviews,
    report_section,
)


TOOL_CONTRACT_VERSION = "zhuojian-agent-tools/v1"
CITATION_CONTRACT_VERSION = "zhuojian-citation/v1"
CITATION_VALIDATION_VERSION = "zhuojian-citation-validation/v1"
MAX_TOOL_PAGE_SIZE = 50
DEFAULT_TOOL_PAGE_SIZE = 20
TOOL_NAMES = (
    "zhuojian.list_reports@v1",
    "zhuojian.get_report@v1",
    "zhuojian.get_metric@v1",
    "zhuojian.list_evidence_groups@v1",
    "zhuojian.query_evidence@v1",
    "zhuojian.get_data_quality@v1",
    "zhuojian.get_report_section@v1",
    "zhuojian.query_report_reviews@v1",
    "zhuojian.aggregate_report_reviews@v1",
)

_REPORT_ID = re.compile(r"^rpt_[A-Za-z0-9_-]{8,80}$")


class AgentToolError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 422,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable


@dataclass(frozen=True)
class ToolAuthority:
    authority_id: str
    principal_id: str
    allowed_report_ids: frozenset[str] | None
    allow_archived: bool = False

    def permits(self, report_id: str) -> bool:
        return (
            self.allowed_report_ids is None
            or report_id in self.allowed_report_ids
        )

    def public(self) -> dict[str, Any]:
        return {
            "authority_id": self.authority_id,
            "principal_id": self.principal_id,
            "scope_mode": (
                "all_reports"
                if self.allowed_report_ids is None
                else "report_allowlist"
            ),
            "allowed_report_count": (
                None
                if self.allowed_report_ids is None
                else len(self.allowed_report_ids)
            ),
            "allow_archived": self.allow_archived,
        }


def authority_from_gateway(
    headers: Mapping[str, str], expected_token: str | None
) -> ToolAuthority:
    """Build authority only from trusted Gateway-injected HTTP headers.

    None of these fields are part of a model-callable tool input schema.  The
    shared token is configured out-of-repository on both services.
    """

    expected = str(expected_token or "")
    supplied = str(headers.get("x-voxquarry-gateway-token") or "")
    if not expected:
        raise AgentToolError(
            "gateway_unconfigured",
            "可信 Gateway 尚未配置",
            http_status=503,
            retryable=True,
        )
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise AgentToolError(
            "gateway_unauthorized",
            "请求不是由可信 Gateway 注入",
            http_status=401,
        )
    authority_id = " ".join(
        str(headers.get("x-voxquarry-authority-id") or "").split()
    )
    principal_id = " ".join(
        str(headers.get("x-voxquarry-principal-id") or "").split()
    )
    raw_allowed = str(
        headers.get("x-voxquarry-allowed-report-ids") or ""
    ).strip()
    if not authority_id or len(authority_id) > 128:
        raise AgentToolError(
            "invalid_authority", "Gateway authority_id 无效", http_status=403
        )
    if not principal_id or len(principal_id) > 128:
        raise AgentToolError(
            "invalid_authority", "Gateway principal_id 无效", http_status=403
        )
    if raw_allowed == "*":
        allowed: frozenset[str] | None = None
    else:
        values = [value.strip() for value in raw_allowed.split(",") if value.strip()]
        if not values or any(not _REPORT_ID.fullmatch(value) for value in values):
            raise AgentToolError(
                "invalid_authority",
                "Gateway 报告允许范围无效",
                http_status=403,
            )
        allowed = frozenset(values)
    raw_archived = str(
        headers.get("x-voxquarry-allow-archived") or "false"
    ).strip().lower()
    if raw_archived not in {"true", "false"}:
        raise AgentToolError(
            "invalid_authority",
            "Gateway archived 权限必须是 true 或 false",
            http_status=403,
        )
    return ToolAuthority(
        authority_id=authority_id,
        principal_id=principal_id,
        allowed_report_ids=allowed,
        allow_archived=raw_archived == "true",
    )


def _input(
    payload: object,
    *,
    allowed: set[str],
    required: set[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AgentToolError("invalid_request", "工具输入必须是对象")
    extras = sorted(set(payload) - allowed)
    if extras:
        raise AgentToolError(
            "invalid_request", f"工具输入包含未知字段：{', '.join(extras)}"
        )
    missing = sorted(
        key for key in required if not str(payload.get(key) or "").strip()
    )
    if missing:
        raise AgentToolError(
            "invalid_request", f"工具输入缺少字段：{', '.join(missing)}"
        )
    return payload


def _page(payload: Mapping[str, Any]) -> tuple[int, int]:
    offset = payload.get("offset", 0)
    limit = payload.get("limit", DEFAULT_TOOL_PAGE_SIZE)
    if (
        isinstance(offset, bool)
        or isinstance(limit, bool)
        or not isinstance(offset, int)
        or not isinstance(limit, int)
    ):
        raise AgentToolError("invalid_request", "offset/limit 必须是整数")
    if offset < 0:
        raise AgentToolError("invalid_request", "offset 不能小于 0")
    if limit < 1 or limit > MAX_TOOL_PAGE_SIZE:
        raise AgentToolError("invalid_request", "limit 必须在 1–50")
    return offset, limit


def _string_field(
    payload: Mapping[str, Any], key: str, *, maximum: int
) -> str | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, str) or len(value) > maximum:
        raise AgentToolError(
            "invalid_request", f"{key} 必须是最长 {maximum} 字符的字符串"
        )
    return value


def _limitations(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(report.get("limitations") or []):
        if isinstance(raw, dict):
            message = " ".join(str(raw.get("message") or "").split())
            if not message:
                continue
            rows.append(
                {
                    "code": str(raw.get("code") or f"limitation_{index + 1}"),
                    "severity": str(raw.get("severity") or "informational"),
                    "message": message,
                }
            )
        else:
            message = " ".join(str(raw or "").split())
            if message:
                rows.append(
                    {
                        "code": f"limitation_{index + 1}",
                        "severity": "informational",
                        "message": message,
                    }
                )
    return rows


def _report_ref(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "report_id": str(report.get("report_id") or ""),
        "schema_version": str(report.get("schema_version") or ""),
        "analysis_version": str(report.get("analysis_version") or ""),
        "title": str(report.get("title") or ""),
        "status": str(report.get("status") or ""),
        "generated_at": str(report.get("generated_at") or ""),
    }


def _snapshot_ref(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_id": str(report.get("data_snapshot_id") or ""),
        "schema_version": str(
            (report.get("method") or {}).get("market_contract", {}).get("version")
            or "snapshot-manifest/unknown"
        ),
        "content_sha256": (report.get("hashes") or {}).get("data"),
    }


def _quality_ref(report: Mapping[str, Any]) -> dict[str, Any]:
    limitations = _limitations(report)
    source = dict(report.get("quality") or {})
    limited = bool(source.get("limited_source_jobs")) or any(
        row["severity"] in {"material", "blocking-for-metric"}
        for row in limitations
    )
    return {
        "status": "limited" if limited else "available",
        "limitation_count": len(limitations),
        "report_quality": source or None,
    }


def _citation_id(identity: Mapping[str, Any]) -> str:
    return f"cit_{sha256_text(canonical_json(dict(identity)))[:24]}"


def citation(
    report: Mapping[str, Any],
    citation_type: str,
    *,
    label: str,
    metric_id: str | None = None,
    evidence_group_id: str | None = None,
    evidence_id: str | None = None,
    source_url: str | None = None,
    quote: str | None = None,
) -> dict[str, Any]:
    report_id = str(report.get("report_id") or "")
    snapshot_id = str(report.get("data_snapshot_id") or "")
    path = f"voxquarry://zhuojian/reports/{report_id}"
    if metric_id:
        path += f"/metrics/{metric_id}"
    elif evidence_group_id:
        path += f"/evidence-groups/{evidence_group_id}"
        if evidence_id:
            path += f"/evidence/{evidence_id}"
    identity = {
        "contract": CITATION_CONTRACT_VERSION,
        "citation_type": citation_type,
        "report_id": report_id,
        "snapshot_id": snapshot_id,
        "metric_id": metric_id,
        "evidence_group_id": evidence_group_id,
        "evidence_id": evidence_id,
        "stable_uri": path,
        "quote_sha256": (
            f"sha256:{hashlib.sha256(str(quote).encode('utf-8')).hexdigest()}"
            if quote is not None
            else None
        ),
    }
    return {
        "contract": CITATION_CONTRACT_VERSION,
        "citation_id": _citation_id(identity),
        "citation_type": citation_type,
        "report_id": report_id,
        "snapshot_id": snapshot_id,
        "metric_id": metric_id,
        "evidence_group_id": evidence_group_id,
        "evidence_id": evidence_id,
        "stable_uri": path,
        "label": label,
        "source_url": source_url,
        "quote_sha256": identity["quote_sha256"],
    }


class AgentToolService:
    def __init__(self, store: Any, evidence: Any):
        self.store = store
        self.evidence = evidence

    def _authorized_report(
        self, report_id: str, authority: ToolAuthority
    ) -> dict[str, Any]:
        if not _REPORT_ID.fullmatch(report_id):
            raise AgentToolError("invalid_request", "report_id 格式无效")
        if not authority.permits(report_id):
            raise AgentToolError(
                "report_forbidden", "报告不在 Gateway 允许范围内", http_status=403
            )
        report = self.store.get_report(report_id)
        if report is None:
            raise AgentToolError(
                "report_not_found", "报告不存在", http_status=404
            )
        if self.store.is_archived(report_id) and not authority.allow_archived:
            raise AgentToolError(
                "report_forbidden", "Gateway 未授权读取归档报告", http_status=403
            )
        return report

    def _envelope(
        self,
        tool: str,
        authority: ToolAuthority,
        *,
        data: Any,
        report: Mapping[str, Any] | None = None,
        citations: list[dict[str, Any]] | None = None,
        page: dict[str, Any] | None = None,
        limitations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "contract": TOOL_CONTRACT_VERSION,
            "tool": tool,
            "status": "ok",
            "snapshot": _snapshot_ref(report) if report else None,
            "report": _report_ref(report) if report else None,
            "authority": authority.public(),
            "quality": (
                _quality_ref(report)
                if report
                else {
                    "status": "not_applicable",
                    "limitation_count": 0,
                    "report_quality": None,
                }
            ),
            "limitations": (
                limitations
                if limitations is not None
                else (_limitations(report) if report else [])
            ),
            "citations": citations or [],
            "page": page,
            "error": None,
            "data": data,
        }

    def error_envelope(
        self,
        tool: str,
        authority: ToolAuthority | None,
        error: AgentToolError,
    ) -> dict[str, Any]:
        return {
            "contract": TOOL_CONTRACT_VERSION,
            "tool": tool,
            "status": "error",
            "snapshot": None,
            "report": None,
            "authority": authority.public() if authority else None,
            "quality": {
                "status": "unknown",
                "limitation_count": 0,
                "report_quality": None,
            },
            "limitations": [],
            "citations": [],
            "page": None,
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
            },
            "data": None,
        }

    def invoke(
        self, tool: str, payload: object, authority: ToolAuthority
    ) -> dict[str, Any]:
        if tool not in TOOL_NAMES:
            raise AgentToolError(
                "tool_not_found", "未知或未开放的分析报告工具", http_status=404
            )
        handler = getattr(self, tool.split(".", 1)[1].split("@", 1)[0])
        return handler(payload, authority)

    def _research_tool(
        self, name: str, payload: object, authority: ToolAuthority
    ) -> dict[str, Any]:
        allowed = {
            "get_report_section": {"report_id", "section", "offset", "limit"},
            "query_report_reviews": QUERY_KEYS | {"offset", "limit", "cursor"},
            "aggregate_report_reviews": QUERY_KEYS | {"group_by"},
        }[name]
        value = _input(payload, allowed=allowed, required={"report_id"})
        if not isinstance(value["report_id"], str):
            raise AgentToolError("invalid_request", "report_id 必须是字符串")
        report = self._authorized_report(value["report_id"], authority)
        if report.get("report_id") != value["report_id"] or report.get("status") != "completed":
            raise AgentToolError("research_source_invalid", "只能研究身份一致的已完成报告", http_status=409)
        try:
            if name == "get_report_section":
                data, page = report_section(report, value)
            else:
                with FrozenReviewData(self.store.root, report) as source:
                    if name == "query_report_reviews":
                        data, page = query_reviews(source, value)
                    else:
                        data, page = aggregate_reviews(source, value), None
        except ResearchDataError as error:
            raise AgentToolError(error.code, error.message, http_status=error.http_status) from error
        result = self._envelope(
            f"zhuojian.{name}@v1", authority, data=data, report=report,
            page=page, citations=[citation(report, "report", label=str(report.get("title") or "报告"))],
        )
        if len(canonical_json(result).encode("utf-8")) > MAX_RESEARCH_RESPONSE_BYTES:
            raise AgentToolError(
                "research_result_too_large",
                "这次读取的完整资料超过单次传输容量；请改读更小的报告子节点、减少每页条数或缩小分组。内容未被截断",
                http_status=409,
            )
        return result

    def get_report_section(self, payload: object, authority: ToolAuthority) -> dict[str, Any]:
        return self._research_tool("get_report_section", payload, authority)

    def query_report_reviews(self, payload: object, authority: ToolAuthority) -> dict[str, Any]:
        return self._research_tool("query_report_reviews", payload, authority)

    def aggregate_report_reviews(self, payload: object, authority: ToolAuthority) -> dict[str, Any]:
        return self._research_tool("aggregate_report_reviews", payload, authority)

    def list_reports(
        self, payload: object, authority: ToolAuthority
    ) -> dict[str, Any]:
        value = _input(
            payload,
            allowed={"include_archived", "offset", "limit"},
        )
        if "include_archived" in value and not isinstance(
            value["include_archived"], bool
        ):
            raise AgentToolError(
                "invalid_request", "include_archived 必须是布尔值"
            )
        requested_archived = bool(value.get("include_archived", False))
        if requested_archived and not authority.allow_archived:
            raise AgentToolError(
                "report_forbidden", "Gateway 未授权归档报告", http_status=403
            )
        offset, limit = _page(value)
        cards = self.store.list_reports(include_archived=requested_archived)
        cards = [
            card
            for card in cards
            if authority.permits(str(card.get("report_id") or ""))
        ]
        total = len(cards)
        items = cards[offset : offset + limit]
        next_offset = offset + limit if offset + limit < total else None
        page = {
            "offset": offset,
            "limit": limit,
            "returned": len(items),
            "total": total,
            "next_offset": next_offset,
            "has_more": next_offset is not None,
        }
        citations = []
        for card in items:
            report = self.store.get_report(str(card["report_id"]))
            if report:
                citations.append(
                    citation(
                        report,
                        "report",
                        label=str(card.get("display_name") or report.get("title") or "报告"),
                    )
                )
        return self._envelope(
            "zhuojian.list_reports@v1",
            authority,
            data={"items": items},
            citations=citations,
            page=page,
        )

    def get_report(
        self, payload: object, authority: ToolAuthority
    ) -> dict[str, Any]:
        value = _input(payload, allowed={"report_id"}, required={"report_id"})
        report = self._authorized_report(str(value["report_id"]), authority)
        data = {
            key: report.get(key)
            for key in (
                "title",
                "summary_text",
                "executive_summary",
                "research_plan",
                "scope",
                "samples",
                "metric_definitions",
                "metrics",
                "tables",
                "findings",
                "sections",
                "product_insight_availability",
                "method",
            )
        }
        return self._envelope(
            "zhuojian.get_report@v1",
            authority,
            report=report,
            data=data,
            citations=[
                citation(report, "report", label=str(report.get("title") or "报告"))
            ],
        )

    def get_metric(
        self, payload: object, authority: ToolAuthority
    ) -> dict[str, Any]:
        value = _input(
            payload,
            allowed={"report_id", "metric_id"},
            required={"report_id", "metric_id"},
        )
        report = self._authorized_report(str(value["report_id"]), authority)
        metric_id = _string_field(value, "metric_id", maximum=160) or ""
        metric = next(
            (
                item
                for item in report.get("metrics") or []
                if str(item.get("metric_id") or "") == metric_id
            ),
            None,
        )
        definition = next(
            (
                item
                for item in report.get("metric_definitions") or []
                if str(item.get("metric_id") or "") == metric_id
            ),
            None,
        )
        if metric is None or definition is None:
            raise AgentToolError(
                "metric_not_found", "已保存报告中不存在该指标", http_status=404
            )
        data = {
            "metric_id": metric_id,
            "definition": definition,
            "value": metric.get("value"),
            "numerator": metric.get("numerator"),
            "denominator": metric.get("denominator"),
            "sample_size": metric.get("sample_size"),
            "interval_95": metric.get("interval_95"),
            "filters": metric.get("filters") or {},
            "quality_flags": metric.get("quality_flags") or [],
        }
        return self._envelope(
            "zhuojian.get_metric@v1",
            authority,
            report=report,
            data=data,
            citations=[
                citation(
                    report,
                    "metric",
                    label=str(definition.get("name") or metric_id),
                    metric_id=metric_id,
                )
            ],
        )

    def list_evidence_groups(
        self, payload: object, authority: ToolAuthority
    ) -> dict[str, Any]:
        value = _input(
            payload,
            allowed={"report_id", "offset", "limit"},
            required={"report_id"},
        )
        report = self._authorized_report(str(value["report_id"]), authority)
        _string_field(value, "evidence_group_id", maximum=180)
        offset, limit = _page(value)
        try:
            result = self.evidence.list_groups(
                str(report["report_id"]), offset=offset, limit=limit
            )
        except FileNotFoundError as error:
            raise AgentToolError(
                "evidence_unavailable",
                "该历史报告没有可读取的 Evidence 边车",
                http_status=409,
            ) from error
        citations = [
            citation(
                report,
                "evidence_group",
                label=str(item.get("label") or item["evidence_group_id"]),
                evidence_group_id=str(item["evidence_group_id"]),
            )
            for item in result["items"]
        ]
        return self._envelope(
            "zhuojian.list_evidence_groups@v1",
            authority,
            report=report,
            data={"items": result["items"]},
            citations=citations,
            page=result["page"],
        )

    def query_evidence(
        self, payload: object, authority: ToolAuthority
    ) -> dict[str, Any]:
        allowed = {
            "report_id",
            "evidence_group_id",
            "offset",
            "limit",
            "channel",
            "brand",
            "model",
            "market_region",
            "rating",
            "polarity",
            "evidence_type",
            "q",
        }
        value = _input(
            payload,
            allowed=allowed,
            required={"report_id", "evidence_group_id"},
        )
        report = self._authorized_report(str(value["report_id"]), authority)
        _string_field(value, "evidence_group_id", maximum=180)
        offset, limit = _page(value)
        rating = value.get("rating")
        if rating is not None and (
            isinstance(rating, bool)
            or not isinstance(rating, int)
            or rating not in {1, 2, 3, 4, 5}
        ):
            raise AgentToolError("invalid_request", "rating 必须在 1–5")
        for key, maximum in {
            "channel": 120,
            "brand": 160,
            "model": 200,
            "polarity": 80,
            "evidence_type": 120,
            "q": 200,
        }.items():
            _string_field(value, key, maximum=maximum)
        if value.get("market_region") not in {
            None,
            "North America",
            "Europe",
            "Australia",
            "Other",
        }:
            raise AgentToolError(
                "invalid_request", "market_region 不是规范来源市场"
            )
        try:
            result = self.evidence.query(
                str(report["report_id"]),
                str(value["evidence_group_id"]),
                offset=offset,
                limit=limit,
                channel=value.get("channel"),
                brand=value.get("brand"),
                model=value.get("model"),
                market_region=value.get("market_region"),
                rating=rating,
                polarity=value.get("polarity"),
                evidence_type=value.get("evidence_type"),
                q=value.get("q"),
                include_facets=False,
            )
        except FileNotFoundError as error:
            raise AgentToolError(
                "evidence_unavailable",
                "该历史报告没有可读取的 Evidence 边车",
                http_status=409,
            ) from error
        except KeyError as error:
            raise AgentToolError(
                "evidence_group_not_found", "证据组不存在", http_status=404
            ) from error
        group_id = str(value["evidence_group_id"])
        citations = [
            citation(
                report,
                "evidence",
                label=str(item.get("label") or item.get("kind") or "原文证据"),
                evidence_group_id=group_id,
                evidence_id=str(item.get("evidence_id") or ""),
                source_url=(str(item.get("source_url")) if item.get("source_url") else None),
                quote=str(item.get("quote") or item.get("snippet") or ""),
            )
            for item in result["items"]
        ]
        return self._envelope(
            "zhuojian.query_evidence@v1",
            authority,
            report=report,
            data={"group": result["group"], "items": result["items"]},
            citations=citations,
            page={
                "offset": result["page"]["offset"],
                "limit": result["page"]["limit"],
                "returned": result["page"]["returned"],
                "total": result["page"]["total_evidence_rows"],
                "next_offset": result["page"]["next_offset"],
                "has_more": result["page"]["has_more"],
            },
        )

    def get_data_quality(
        self, payload: object, authority: ToolAuthority
    ) -> dict[str, Any]:
        value = _input(payload, allowed={"report_id"}, required={"report_id"})
        report = self._authorized_report(str(value["report_id"]), authority)
        data = {
            "quality": report.get("quality") or {},
            "samples": report.get("samples") or {},
            "market_contract": (report.get("method") or {}).get("market_contract"),
            "source_completeness": (
                (report.get("quality") or {}).get("source_completeness") or []
            ),
        }
        return self._envelope(
            "zhuojian.get_data_quality@v1",
            authority,
            report=report,
            data=data,
            citations=[
                citation(report, "report", label=f"{report.get('title') or '报告'} · 数据质量")
            ],
        )

    def validate_citations(
        self,
        raw_citations: object,
        authority: ToolAuthority,
    ) -> dict[str, Any]:
        if not isinstance(raw_citations, list) or len(raw_citations) > 50:
            raise AgentToolError(
                "invalid_request", "citations 必须是最多 50 项的数组"
            )
        results = []
        citation_fields = {
            "contract",
            "citation_id",
            "citation_type",
            "report_id",
            "snapshot_id",
            "metric_id",
            "evidence_group_id",
            "evidence_id",
            "stable_uri",
            "label",
            "source_url",
            "quote_sha256",
        }
        for raw in raw_citations:
            valid = False
            code: str | None = None
            if not isinstance(raw, dict):
                results.append(
                    {"citation_id": None, "valid": False, "error_code": "invalid_citation"}
                )
                continue
            citation_id = str(raw.get("citation_id") or "") or None
            try:
                if set(raw) != citation_fields:
                    raise AgentToolError(
                        "invalid_citation",
                        "citation 字段不符合 zhuojian-citation/v1",
                    )
                report = self._authorized_report(str(raw.get("report_id") or ""), authority)
                citation_type = str(raw.get("citation_type") or "")
                if citation_type == "report":
                    expected = citation(report, "report", label=str(raw.get("label") or report.get("title") or "报告"))
                elif citation_type == "metric":
                    metric_id = str(raw.get("metric_id") or "")
                    definition = next(
                        (
                            item
                            for item in report.get("metric_definitions") or []
                            if str(item.get("metric_id") or "") == metric_id
                        ),
                        None,
                    )
                    if definition is None:
                        raise AgentToolError("citation_target_not_found", "指标引用目标不存在", http_status=404)
                    expected = citation(report, "metric", label=str(raw.get("label") or definition.get("name") or metric_id), metric_id=metric_id)
                elif citation_type == "evidence_group":
                    group_id = str(raw.get("evidence_group_id") or "")
                    group = self.evidence.group(str(report["report_id"]), group_id)
                    if group is None:
                        raise AgentToolError("citation_target_not_found", "证据组引用目标不存在", http_status=404)
                    expected = citation(report, "evidence_group", label=str(raw.get("label") or group.get("label") or group_id), evidence_group_id=group_id)
                elif citation_type == "evidence":
                    group_id = str(raw.get("evidence_group_id") or "")
                    evidence_id = str(raw.get("evidence_id") or "")
                    item = self.evidence.get(str(report["report_id"]), evidence_id)
                    if item is None or not self.evidence.is_group_member(str(report["report_id"]), group_id, evidence_id):
                        raise AgentToolError("citation_target_not_found", "原文引用目标不存在或不属于证据组", http_status=404)
                    expected = citation(
                        report,
                        "evidence",
                        label=str(raw.get("label") or item.get("label") or item.get("kind") or "原文证据"),
                        evidence_group_id=group_id,
                        evidence_id=evidence_id,
                        source_url=(str(item.get("source_url")) if item.get("source_url") else None),
                        quote=str(item.get("quote") or item.get("snippet") or ""),
                    )
                else:
                    raise AgentToolError("invalid_citation", "未知 citation_type")
                checked_fields = citation_fields - {"label"}
                valid = all(raw.get(key) == expected.get(key) for key in checked_fields)
                if not valid:
                    code = "citation_mismatch"
            except AgentToolError as error:
                code = error.code
            except FileNotFoundError:
                code = "evidence_unavailable"
            results.append(
                {"citation_id": citation_id, "valid": valid, "error_code": code}
            )
        return {
            "schema_version": CITATION_VALIDATION_VERSION,
            "valid": all(item["valid"] for item in results),
            "items": results,
        }
