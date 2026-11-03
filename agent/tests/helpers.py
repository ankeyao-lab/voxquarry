from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Mapping

from agent.contracts import AgentError, IdentityContext
from agent.identity import IDENTITY_HEADER, SIGNATURE_HEADER
from reporting.agent_tools import AgentToolError, AgentToolService, ToolAuthority
from reporting.evidence_store import EvidenceRepository
from reporting.storage import ReportStore


REPORT_ID = "rpt_wenquanfixture1234567890"
SNAPSHOT_ID = "snap_wenquanfixture1234567890"
METRIC_ID = "positive_rating_rate"
GROUP_ID = "evg_wenquan_power"
QUOTE = "The synthetic unit moves wet leaves without slowing down."


def identity(
    owner: str = "user-a",
    *,
    roles: tuple[str, ...] = (),
    mode: str = "synthetic",
) -> IdentityContext:
    return IdentityContext(
        actor_id=owner,
        owner_user_id=owner,
        scope_id="scope-research",
        authority_id="wenquan-synthetic-gateway",
        roles=roles,
        allowed_report_ids=frozenset({REPORT_ID}),
        mode=mode,
    )


def fixture_report() -> dict[str, Any]:
    return {
        "report_id": REPORT_ID,
        "title": "Synthetic Leaf Blower Report",
        "schema_version": "report-json/v1",
        "analysis_version": "voxquarry-reporting/synthetic-test",
        "status": "completed",
        "generated_at": "2026-08-28T00:00:00+00:00",
        "data_snapshot_id": SNAPSHOT_ID,
        "hashes": {
            "data": "sha256:" + "1" * 64,
            "core_result": "sha256:" + "2" * 64,
        },
        "research_plan": {
            "primary_direction": "satisfaction-overview",
            "analysis_modules": ["overview"],
            "factors": ["ratings"],
        },
        "scope": {
            "category_id": "leaf_blower",
            "brands": ["SYNTHETIC"],
            "job_ids": ["fixture-job"],
        },
        "samples": {"review_entities": 20, "written_review_entities": 18},
        "quality": {
            "schema_version": "report-data-quality/v1",
            "source_completeness": [],
            "limited_source_jobs": 0,
        },
        "metric_definitions": [
            {
                "metric_id": METRIC_ID,
                "name": "好评率",
                "definition": "4–5 星实体数 / 有效评分实体数",
                "unit": "ratio",
                "population_unit": "review_entity",
                "version": "voxquarry-reporting/synthetic-test",
            }
        ],
        "metrics": [
            {
                "metric_id": METRIC_ID,
                "value": 0.75,
                "sample_size": 20,
                "numerator": 15,
                "denominator": 20,
                "interval_95": [0.53, 0.89],
                "filters": {},
                "quality_flags": [],
            }
        ],
        "tables": [],
        "findings": [],
        "sections": [],
        "summary_text": "Synthetic fixture only.",
        "limitations": [
            {
                "code": "synthetic_fixture",
                "severity": "informational",
                "message": "Only for Wenquan automated tests.",
            }
        ],
        "method": {
            "llm_used": False,
            "market_contract": {
                "mode": "current",
                "version": "review-market-eligibility/1.1.0",
            },
        },
    }


def build_reporting_service(root: Path) -> AgentToolService:
    report_store = ReportStore(root)
    evidence = EvidenceRepository(root)
    report_store.store_report(fixture_report())
    evidence.store(
        REPORT_ID,
        [
            {
                "evidence_id": "ev_wenquan_1",
                "evidence_type": "review_quote",
                "review_entity_id": "re_wenquan_1",
                "review_id": "review-wenquan-1",
                "channel": "ego",
                "brand": "SYNTHETIC",
                "model": "SYN-1",
                "market_region": "North America",
                "market_region_evidence": "source_market",
                "display_site": "example.test",
                "source_site": "example.test",
                "rating": 5,
                "submission_date": "2026-08-01",
                "kind": "aspect:power",
                "label": "动力",
                "polarity": "positive",
                "quote": QUOTE,
                "matched_text": "wet leaves",
                "source_url": "https://example.test/review-wenquan-1",
            }
        ],
        {
            GROUP_ID: {
                "path": "aspect.power.positive",
                "label": "动力 · 正向",
                "evidence_ids": ["ev_wenquan_1"],
                "representative_evidence_ids": ["ev_wenquan_1"],
            }
        },
    )
    return AgentToolService(report_store, evidence)


class SyntheticZhuojianTransport:
    dependency_status = "synthetic_test_only"

    def __init__(self, service: AgentToolService) -> None:
        self.service = service
        self.calls: list[str] = []

    @staticmethod
    def authority(context: IdentityContext) -> ToolAuthority:
        return ToolAuthority(
            authority_id=context.authority_id,
            principal_id=context.owner_user_id,
            allowed_report_ids=context.allowed_report_ids,
            allow_archived=context.allow_archived_reports,
        )

    def call(
        self,
        tool_name: str,
        payload: Mapping[str, Any],
        context: IdentityContext,
        _definition: Any,
    ) -> dict[str, Any]:
        self.calls.append(tool_name)
        authority = self.authority(context)
        try:
            return self.service.invoke(tool_name, dict(payload), authority)
        except AgentToolError as error:
            return self.service.error_envelope(tool_name, authority, error)

    def validate_citations(
        self, citations: list[dict[str, Any]], context: IdentityContext
    ) -> dict[str, Any]:
        self.calls.append("zhuojian.citation_validator@v1")
        return self.service.validate_citations(citations, self.authority(context))


class FailingZhuojianTransport(SyntheticZhuojianTransport):
    def call(
        self,
        tool_name: str,
        payload: Mapping[str, Any],
        context: IdentityContext,
        _definition: Any,
    ) -> dict[str, Any]:
        del tool_name, payload, context, _definition
        raise AgentError(
            "upstream_unavailable",
            "synthetic upstream unavailable",
            http_status=503,
            retryable=True,
        )


def signed_headers(secret: str, context: IdentityContext) -> dict[str, str]:
    payload = {
        "contract": "identity-context/v1",
        "actor_id": context.actor_id,
        "owner_user_id": context.owner_user_id,
        "scope_id": context.scope_id,
        "authority_id": context.authority_id,
        "roles": list(context.roles),
        "allowed_report_ids": (
            "*" if context.allowed_report_ids is None else sorted(context.allowed_report_ids)
        ),
        "allow_archived_reports": context.allow_archived_reports,
        "mode": context.mode,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = "sha256=" + hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return {IDENTITY_HEADER: encoded, SIGNATURE_HEADER: signature}
