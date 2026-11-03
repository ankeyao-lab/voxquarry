"""Opt-in private report preparation over existing collection data only."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import defaultdict
from datetime import date
from typing import Any, Callable

from .agent_tools import AgentToolError, ToolAuthority
from .config import ANALYSIS_VERSION
from .source import catalog
from .product_insight import REPORT_READINESS_THRESHOLDS


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class AgentReportWorkflow:
    def __init__(self, service: Any, create: Callable[..., Any], token: str) -> None:
        self.service, self.create, self.token = service, create, token

    def _candidates(self, filters=None) -> list[dict[str, Any]]:
        filters = dict(filters or {}) if isinstance(filters, (dict, type(None))) else filters
        if isinstance(filters, dict):
            for scalar, plural in (('category_id', 'category_ids'), ('energy_type', 'energy_types')):
                if scalar in filters:
                    value = filters.pop(scalar)
                    if not isinstance(value, str) or not value or (plural in filters and filters[plural] != [value]):
                        raise AgentToolError('invalid_request', '报告范围无效。', http_status=400)
                    filters[plural] = [value]
        names = {"job_ids": "id", "brands": "brand", "models": "model", "category_ids": "category_id", "channels": "channel", "voltage_platform_keys": "voltage_platform_key", "market_regions": "market_region", "energy_types": "energy_type"}
        if not isinstance(filters, dict) or set(filters) - (names.keys() | {"source_product_ids"}) or any(
                not isinstance(v, list) or len(v) > 200 or any(not isinstance(x, str) or not x or len(x) > 160 for x in v)
                for v in filters.values()):
            raise AgentToolError("invalid_request", "报告范围无效。", http_status=400)
        source = catalog(self.service.snapshot_root)
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for task in source.get("tasks", []):
            if any(values and str(task.get(names[key]) or "").casefold() not in {v.casefold() for v in values} for key, values in filters.items() if key in names):
                continue
            if not task.get("eligible_reviews") or not task.get("category_id") or not task.get("model"):
                continue
            key = tuple(str(task.get(k) or "") for k in ("category_id", "brand", "model", "market_region", "energy_type"))
            if filters.get("voltage_platform_keys"):
                key = (*key[:2], "", *key[3:])
            groups[key].append(task)
        candidates = []
        for (category, brand, model, region, energy), tasks in groups.items():
            channels = sorted({t["channel"] for t in tasks})
            variants = [tasks] + ([[t for t in tasks if t["channel"] == channel] for channel in channels] if len(channels) > 1 else [])
            for selected in variants:
                scope = {"category_id": category, "brands": [brand] if brand else [], "models": sorted({t["model"] for t in selected}),
                         "channels": sorted({t["channel"] for t in selected}), "market_regions": [region] if region else [],
                         "job_ids": sorted({t["id"] for t in selected})}
                scope.update({k: sorted(set(filters[k])) for k in ("source_product_ids", "voltage_platform_keys") if filters.get(k)})
                # Reporting's explicit energy filter currently applies only to
                # pressure washers. Other categories stay bound by exact job IDs.
                if category == "pressure_washer":
                    scope["energy_type"] = energy
                key = digest({"scope": scope, "snapshot": source.get("source_snapshot"), "version": ANALYSIS_VERSION})
                candidates.append({"candidate_id": "rc_" + key, "scope": scope,
                                   "title": " · ".join([brand, model or "/".join(filters.get("voltage_platform_keys", [])), "/".join(scope["channels"])]),
                                   "review_count": sum(t["eligible_reviews"] for t in selected),
                                   "written_review_count": sum(t.get("eligible_valid_text_reviews", 0) for t in selected)})
        return candidates

    def _job_token(self, authority: ToolAuthority, job_id: str) -> str:
        return hmac.new(self.token.encode(), (authority.principal_id + ":" + job_id).encode(), hashlib.sha256).hexdigest()

    def _public(self, authority: ToolAuthority, job: dict[str, Any]) -> dict[str, Any]:
        result = {"contract": "wenquan-report-workflow/v1", "job": {
            k: job.get(k) for k in ("job_id", "status", "stage_label", "progress", "report_id", "cancel_requested")},
            "job_token": self._job_token(authority, job["job_id"])}
        if job.get("error"):
            error = job["error"]
            result["job"]["error"] = {"code": error.get("code"), "retryable": bool(error.get("retryable")),
                "message": "这些评论暂时不足以生成完整报告。" if error.get("code") == "REPORT_SCOPE_NOT_READY" else "报告生成没有完成，可以稍后继续这个问题。"}
        return result

    def invoke(self, operation: str, payload: dict[str, Any], authority: ToolAuthority) -> dict[str, Any]:
        # This opt-in workflow is available only to principals authorized for
        # the complete local report collection. An existing-report allowlist
        # must never become a grant to inspect all raw data or create reports.
        if authority.allowed_report_ids is not None:
            raise AgentToolError("report_generation_forbidden", "当前账号不能自动生成新报告。", http_status=403)
        fields = {"catalog": set(), "ensure": {"candidate_id", "date_range"}, "status": {"job_id", "job_token"}, "cancel": {"job_id", "job_token"}}
        optional = {"scope"} if operation in {"catalog", "ensure"} else set()
        if operation not in fields or not fields[operation] <= set(payload) or set(payload) - fields[operation] - optional or any(not isinstance(v, str) or len(v) > 150 for k, v in payload.items() if k not in {"date_range", "scope"}):
            raise AgentToolError("invalid_request", "报告准备请求无效。", http_status=400)
        if operation == "catalog":
            return {"contract": "wenquan-report-workflow/v1", "items": self._candidates(payload.get("scope"))}
        if operation == "ensure":
            dates = payload["date_range"]
            if not isinstance(dates, dict) or set(dates) - {"start", "end"} or any(not isinstance(v, str) or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", v) for v in dates.values()):
                raise AgentToolError("invalid_request", "报告时间无效。", http_status=400)
            try:
                if dates and (set(dates) != {"start", "end"} or date.fromisoformat(dates["start"]) > date.fromisoformat(dates["end"])):
                    raise ValueError
            except ValueError:
                raise AgentToolError("invalid_request", "报告时间无效。", http_status=400) from None
            candidate = next((c for c in self._candidates(payload.get("scope")) if c["candidate_id"] == payload["candidate_id"]), None)
            if candidate is None:
                raise AgentToolError("report_scope_changed", "可用资料发生变化，请重新确认产品。", http_status=409)
            minimum = REPORT_READINESS_THRESHOLDS["pilot"]
            if candidate["written_review_count"] < minimum:
                raise AgentToolError("report_insufficient_comments", f"目前可用的文字评论不足{minimum}条，暂时不能生成完整报告。可以用加号选择已有报告继续。", http_status=409)
            identity = "wenquan:" + digest({"principal": authority.principal_id, "candidate": candidate["candidate_id"], "dates": dates})
            base_identity = identity
            for attempt in range(3):
                identity = base_identity if attempt == 0 else base_identity + ":retry:" + str(attempt)
                expected_id = "rjob_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
                job = self.service.store.get_job(expected_id)
                if job and (job.get("error") or {}).get("code") == "REPORT_SCOPE_NOT_READY":
                    return self._public(authority, job)
                if job is None or job["status"] not in {"failed", "cancelled"}:
                    break
            else:
                raise AgentToolError("report_preparation_failed", "这份报告连续生成失败，请稍后再试或用加号选择其他报告。", http_status=409)
            if job is None:
                if sum(j.get("status") not in {"completed", "failed", "cancelled"} for j in self.service.store.list_jobs()) >= 8:
                    raise AgentToolError("report_queue_busy", "报告正在排队，请稍后继续。", http_status=409, retryable=True)
                job = self.create({"scope": {**candidate["scope"], "date_range": dates}}, idempotency_key=identity)
            return self._public(authority, job)
        job_id = payload["job_id"]
        if not re.fullmatch(r"rjob_[0-9a-f]{24}", job_id) or not hmac.compare_digest(self._job_token(authority, job_id), payload["job_token"]):
            raise AgentToolError("report_job_forbidden", "没有找到可访问的报告任务。", http_status=404)
        job = self.service.store.get_job(job_id)
        if not job:
            raise AgentToolError("report_job_not_found", "没有找到报告任务。", http_status=404)
        if operation == "cancel" and job["status"] not in {"completed", "failed", "cancelled"}:
            job = self.service.store.request_cancel(job_id)
        return self._public(authority, job)
