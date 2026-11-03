from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from .citations import validate_claims
from .contracts import (
    AgentError,
    IdentityContext,
    canonical_json,
    normalize_text,
    reject_sensitive_text,
    sha256_value,
)
from .planner import SemanticPlanner
from .store import WenquanStore
from .tool_registry import ToolRegistry
from .upstream import ToolExecutor


class WenquanService:
    def __init__(
        self,
        *,
        store: WenquanStore,
        registry: ToolRegistry,
        executor: ToolExecutor | None,
        planner: SemanticPlanner | None = None,
        identity_status: str = "unavailable",
        llm_status: str = "unavailable",
        chengmu_status: str = "unavailable",
    ) -> None:
        self.store = store
        self.registry = registry
        self.executor = executor
        self.planner = planner
        self.identity_status = identity_status
        self.llm_status = llm_status
        self.chengmu_status = chengmu_status

    def dependencies(self) -> dict[str, Any]:
        try:
            storage = self.store.health()
        except AgentError as error:
            storage = {"status": "unavailable", "error_code": error.code}
        return {
            "contract": "voxquarry-wenquan-dependencies/v1",
            "dependencies": {
                "storage": storage,
                "identity": {
                    "status": self.identity_status,
                    "contract": "identity-context/v1",
                },
                "zhuojian": {
                    "status": self.registry.dependency_status,
                    "contract": "zhuojian-agent-tools/v1",
                    "tool_count": len(self.registry.payload()["tools"]),
                },
                "chengmu": {
                    "status": self.chengmu_status,
                    "contract": "retail-product-master/v2",
                },
                "llm": {
                    "status": self.llm_status,
                    "fact_authority": False,
                    "readiness_gate": "separate",
                },
                "semantic_planner": {
                    "status": self.planner.status if self.planner is not None else "unavailable",
                    "contract": "voxquarry-wenquan-semantic-plan/v1",
                    "fact_authority": False,
                    "permission_authority": False,
                },
            },
        }

    def readiness(self) -> dict[str, Any]:
        dependencies = self.dependencies()
        items = dependencies["dependencies"]
        ready = (
            items["storage"].get("status") == "ready"
            and items["identity"]["status"] == "ready"
            and items["zhuojian"]["status"] == "ready"
        )
        return {
            "contract": "voxquarry-wenquan-readiness/v1",
            "ready": ready,
            "status": "ready" if ready else "blocked",
            "required": ["storage", "identity", "zhuojian"],
            "dependencies": items,
        }

    def create_session(self, identity: IdentityContext, title: str) -> dict[str, Any]:
        return self.store.create_session(identity, title)

    def session_bundle(self, identity: IdentityContext, session_id: str) -> dict[str, Any]:
        return self.store.session_bundle(identity, session_id)

    def record_unrouted_turn(
        self,
        identity: IdentityContext,
        *,
        session_id: str,
        question: str,
    ) -> dict[str, Any]:
        question_text = normalize_text(question, field="question", maximum=4000)
        self.store.add_message(
            identity, session_id, role="user", content=question_text
        )
        run = self.store.create_run(
            identity,
            session_id,
            intent="resolve_research_scope",
            plan=[{"title": "解析研究范围与证据来源", "status": "blocked"}],
            status="blocked",
        )
        message = self.store.add_message(
            identity,
            session_id,
            role="assistant",
            content="当前证据范围或语义规划依赖尚未就绪，证据不足，无法判断。",
            answer_status="insufficient_evidence",
            run_id=run["run_id"],
            metadata={
                "error_code": "semantic_planner_unavailable",
                "claims": [],
                "citations": [],
            },
        )
        return {
            "answer_status": "insufficient_evidence",
            "message": message,
            "run": self.store.get_run(identity, run["run_id"]),
        }

    def plan_turn(
        self,
        identity: IdentityContext,
        *,
        session_id: str,
        question: str,
        seed: int = 20260828,
    ) -> dict[str, Any]:
        if self.planner is None:
            return self.record_unrouted_turn(
                identity,
                session_id=session_id,
                question=question,
            )
        question_text = normalize_text(question, field="question", maximum=4000)
        self.store.add_message(
            identity, session_id, role="user", content=question_text
        )
        try:
            execution = self.planner.plan(question_text, seed=seed)
        except AgentError as error:
            run = self.store.create_run(
                identity,
                session_id,
                intent="semantic_planner_failure",
                plan=[{"title": "语义规划失败关闭", "status": "blocked"}],
                status="blocked",
            )
            message = self.store.add_message(
                identity,
                session_id,
                role="assistant",
                content="语义规划未形成有效受治理计划，当前证据不足，无法判断。",
                answer_status="insufficient_evidence",
                run_id=run["run_id"],
                metadata={
                    "error_code": error.code,
                    "claims": [],
                    "citations": [],
                },
            )
            return {
                "answer_status": "insufficient_evidence",
                "message": message,
                "run": self.store.get_run(identity, run["run_id"]),
            }

        semantic_plan = execution.plan.public()
        step_plan = [
            {
                "title": f"执行受治理工具 {tool_name}",
                "tool_name": tool_name,
                "status": "queued",
            }
            for tool_name in execution.plan.tool_sequence
        ]
        if not step_plan:
            step_plan = [{"title": "执行确定性政策门禁", "status": "completed"}]
        if execution.plan.answer_status == "waiting_confirmation":
            run_status = "waiting_confirmation"
        elif execution.plan.answer_status in {
            "blocked",
            "clarification_required",
            "insufficient_evidence",
        }:
            run_status = "blocked"
        elif execution.plan.answer_status == "completed":
            run_status = "completed"
        else:
            run_status = "planned"
        run = self.store.create_run(
            identity,
            session_id,
            intent=execution.plan.intent_code,
            plan=step_plan,
            status=run_status,
        )
        if execution.plan.answer_status == "waiting_confirmation":
            content = "已识别需要结构化确认的动作；尚未执行，也未调用任何上游写能力。"
        elif execution.plan.answer_status == "clarification_required":
            content = "当前研究对象或范围不明确，需要补充稳定报告、型号或证据范围。"
        elif execution.plan.answer_status == "blocked":
            content = "该请求违反AI 研究的权限、数据或指标边界，已阻止执行。"
        elif execution.plan.answer_status == "insufficient_evidence":
            content = "当前计划只能安全停止，证据不足，无法判断。"
        elif execution.plan.answer_status == "completed":
            content = "已识别为恢复或复用已有对象；不会创建重复对象。"
        else:
            content = "已生成受治理只读计划；在工具与引用门禁完成前不会输出业务结论。"
        message = self.store.add_message(
            identity,
            session_id,
            role="assistant",
            content=content,
            answer_status=(
                execution.plan.answer_status
                if execution.plan.answer_status != "supported"
                else "planned"
            ),
            run_id=run["run_id"],
            metadata={
                "semantic_plan": semantic_plan,
                "planner_request_sha256": execution.request_sha256,
                "planner_classification_sha256": execution.classification_sha256,
                "claims": [],
                "citations": [],
            },
        )
        return {
            "answer_status": message["answer_status"],
            "planned_answer_status": execution.plan.answer_status,
            "semantic_plan": semantic_plan,
            "message": message,
            "run": self.store.get_run(identity, run["run_id"]),
        }

    @staticmethod
    def _future_expiry(minutes: int = 30) -> str:
        return (datetime.now(tz=UTC) + timedelta(minutes=minutes)).isoformat(
            timespec="milliseconds"
        )

    def preview_preference(
        self, identity: IdentityContext, *, preference_key: str, value: Any
    ) -> dict[str, Any]:
        key = normalize_text(preference_key, field="preference_key", maximum=80)
        preview = {"preference_key": key, "value": value}
        return self.store.create_confirmation(
            identity,
            action="memory.preference",
            preview=preview,
            expires_at=self._future_expiry(),
        )

    def preview_preference_delete(
        self, identity: IdentityContext, *, preference_key: str
    ) -> dict[str, Any]:
        key = normalize_text(preference_key, field="preference_key", maximum=80)
        preview = {"preference_key": key, "delete": True}
        return self.store.create_confirmation(
            identity,
            action="memory.preference.delete",
            preview=preview,
            expires_at=self._future_expiry(),
        )

    def preview_business_action(
        self,
        identity: IdentityContext,
        *,
        session_id: str,
        action: str,
        product_ids: list[str],
        channels: list[str],
        time_range: Mapping[str, Any],
        estimated_review_count: int | None,
        estimated_duration_minutes: int | None,
        reused_object_ids: list[str],
        created_object_types: list[str],
        scope_expanded: bool,
        external_paid: bool,
    ) -> dict[str, Any]:
        allowed_actions = {
            "create_collection_task",
            "create_collection_retry",
            "create_structured_snapshot",
            "create_analysis_run",
            "create_product_snapshot",
            "create_report",
            "create_data_export",
        }
        if action not in allowed_actions:
            raise AgentError("capability_unavailable", "新建动作不在受治理目录中", http_status=404)
        if not isinstance(product_ids, list) or not 1 <= len(product_ids) <= 50:
            raise AgentError("invalid_request", "product_ids 必须包含 1–50 个稳定 ID")
        if not isinstance(channels, list) or len(channels) > 30:
            raise AgentError("invalid_request", "channels 无效")
        if not isinstance(reused_object_ids, list) or len(reused_object_ids) > 100:
            raise AgentError("invalid_request", "reused_object_ids 无效")
        if not isinstance(created_object_types, list) or not created_object_types:
            raise AgentError("invalid_request", "created_object_types 不能为空")
        for label, values in (
            ("product_ids", product_ids),
            ("channels", channels),
            ("reused_object_ids", reused_object_ids),
            ("created_object_types", created_object_types),
        ):
            if any(not isinstance(value, str) or not value or len(value) > 200 for value in values):
                raise AgentError("invalid_request", f"{label} 包含无效值")
        if not isinstance(time_range, Mapping) or set(time_range) != {"from", "to"}:
            raise AgentError("invalid_request", "time_range 必须包含 from/to")
        if estimated_review_count is not None and (
            isinstance(estimated_review_count, bool)
            or not isinstance(estimated_review_count, int)
            or estimated_review_count < 0
        ):
            raise AgentError("invalid_request", "estimated_review_count 无效")
        if estimated_duration_minutes is not None and (
            isinstance(estimated_duration_minutes, bool)
            or not isinstance(estimated_duration_minutes, int)
            or estimated_duration_minutes < 0
        ):
            raise AgentError("invalid_request", "estimated_duration_minutes 无效")
        preview = {
            "action": action,
            "product_ids": product_ids,
            "channels": channels,
            "time_range": dict(time_range),
            "estimated_review_count": estimated_review_count,
            "estimated_duration_minutes": estimated_duration_minutes,
            "reused_object_ids": reused_object_ids,
            "created_object_types": created_object_types,
            "scope_expanded": bool(scope_expanded),
            "external_paid": bool(external_paid),
            "execution_status": "capability_unavailable_until_domain_tool_contracts",
        }
        reject_sensitive_text(canonical_json(preview), field="business action preview")
        required_count = 2 if (
            len(product_ids) > 1
            or (estimated_review_count is not None and estimated_review_count >= 50_000)
            or scope_expanded
            or external_paid
        ) else 1
        return self.store.create_confirmation(
            identity,
            session_id=session_id,
            action=f"business.{action}",
            preview=preview,
            expires_at=self._future_expiry(),
            required_count=required_count,
        )

    def answer_metric_with_evidence(
        self,
        identity: IdentityContext,
        *,
        session_id: str,
        question: str,
        report_id: str,
        metric_id: str,
        evidence_group_id: str,
    ) -> dict[str, Any]:
        question_text = normalize_text(question, field="question", maximum=4000)
        report_id = normalize_text(report_id, field="report_id", maximum=100)
        metric_id = normalize_text(metric_id, field="metric_id", maximum=160)
        evidence_group_id = normalize_text(
            evidence_group_id, field="evidence_group_id", maximum=180
        )
        self.store.add_message(
            identity, session_id, role="user", content=question_text
        )
        plan = [
            {"title": "核对可访问报告", "tool_name": "zhuojian.list_reports@v1"},
            {"title": "读取固化指标", "tool_name": "zhuojian.get_metric@v1"},
            {"title": "分页读取证据", "tool_name": "zhuojian.query_evidence@v1"},
            {"title": "重读并校验引用", "tool_name": "zhuojian.citation_validator@v1"},
        ]
        run = self.store.create_run(
            identity,
            session_id,
            intent="answer_metric_with_evidence",
            plan=plan,
            status="running",
        )
        run_id = run["run_id"]
        steps = run["steps"]
        current_step = None
        if self.executor is None:
            return self._fail_answer(
                identity,
                session_id=session_id,
                run_id=run_id,
                step_id=steps[0]["step_id"],
                error=AgentError(
                    "capability_unavailable",
                    "分析报告只读工具尚未接入",
                    http_status=503,
                    retryable=True,
                ),
            )
        try:
            current_step = steps[0]["step_id"]
            self.store.update_step_status(identity, current_step, "running")
            offset = 0
            visible = False
            for _ in range(10):
                catalog = self.executor.invoke(
                    identity,
                    tool_name="zhuojian.list_reports@v1",
                    payload={"offset": offset, "limit": 50},
                    session_id=session_id,
                    run_id=run_id,
                    step_id=current_step,
                ).result
                items = (catalog.get("data") or {}).get("items") or []
                if any(item.get("report_id") == report_id for item in items if isinstance(item, dict)):
                    visible = True
                    break
                page = catalog.get("page") or {}
                if not page.get("has_more") or page.get("next_offset") is None:
                    break
                offset = int(page["next_offset"])
            if not visible:
                raise AgentError("report_not_found", "允许范围内没有该报告", http_status=404)
            self.store.update_step_status(identity, current_step, "completed")

            current_step = steps[1]["step_id"]
            self.store.update_step_status(identity, current_step, "running")
            metric_result = self.executor.invoke(
                identity,
                tool_name="zhuojian.get_metric@v1",
                payload={"report_id": report_id, "metric_id": metric_id},
                session_id=session_id,
                run_id=run_id,
                step_id=current_step,
            ).result
            self.store.update_step_status(identity, current_step, "completed")

            current_step = steps[2]["step_id"]
            self.store.update_step_status(identity, current_step, "running")
            evidence_result = self.executor.invoke(
                identity,
                tool_name="zhuojian.query_evidence@v1",
                payload={
                    "report_id": report_id,
                    "evidence_group_id": evidence_group_id,
                    "offset": 0,
                    "limit": 5,
                },
                session_id=session_id,
                run_id=run_id,
                step_id=current_step,
            ).result
            evidence_items = (evidence_result.get("data") or {}).get("items") or []
            if not evidence_items:
                raise AgentError("insufficient_evidence", "该证据组没有可引用原文", http_status=409)
            self.store.update_step_status(identity, current_step, "completed")

            metric_citations = metric_result.get("citations") or []
            evidence_citations = evidence_result.get("citations") or []
            citations = list(metric_citations[:1]) + list(evidence_citations[:3])
            current_step = steps[3]["step_id"]
            self.store.update_step_status(identity, current_step, "running")
            validation = self.executor.validate_citations(
                identity,
                citations,
                session_id=session_id,
                run_id=run_id,
                step_id=current_step,
            ).result

            metric = metric_result.get("data") or {}
            definition = metric.get("definition") or {}
            group = (evidence_result.get("data") or {}).get("group") or {}
            value_text = json.dumps(metric.get("value"), ensure_ascii=False)
            numerator = json.dumps(metric.get("numerator"), ensure_ascii=False)
            denominator = json.dumps(metric.get("denominator"), ensure_ascii=False)
            sample_size = json.dumps(metric.get("sample_size"), ensure_ascii=False)
            claim_text = (
                f"{definition.get('name') or metric_id}的固化值为 {value_text}；"
                f"分子 {numerator}，分母 {denominator}，样本量 {sample_size}。"
            )
            claims = validate_claims(
                [
                    {
                        "text": claim_text,
                        "report_id": report_id,
                        "metric_id": metric_id,
                        "evidence_group_id": evidence_group_id,
                        "citation_ids": [item["citation_id"] for item in citations],
                    }
                ],
                citations,
                validation,
            )
            self.store.update_step_status(identity, current_step, "completed")
            self.store.add_artifact(
                identity,
                run_id=run_id,
                artifact_type="report_reference",
                upstream_id=report_id,
                contract_version=str((metric_result.get("report") or {}).get("schema_version") or "report-json/v1"),
                status="available",
                display_name=str((metric_result.get("report") or {}).get("title") or report_id),
                uri=next((item.get("stable_uri") for item in metric_citations if item.get("stable_uri")), None),
                content_hash=(metric_result.get("snapshot") or {}).get("content_sha256"),
                metadata={"metric_id": metric_id},
            )
            self.store.add_artifact(
                identity,
                run_id=run_id,
                artifact_type="evidence_group",
                upstream_id=evidence_group_id,
                contract_version="zhuojian-citation/v1",
                status="available",
                display_name=str(group.get("label") or evidence_group_id),
                uri=next((item.get("stable_uri") for item in evidence_citations if item.get("stable_uri")), None),
                metadata={
                    "report_id": report_id,
                    "returned": len(evidence_items),
                    "representative_only": True,
                },
            )
            self.store.update_run_status(identity, run_id, "completed")
            limitations = list(metric_result.get("limitations") or [])
            answer_text = claim_text
            if limitations:
                answer_text += " 数据限制已随回答保留，代表证据不等于完整证据集合。"
            message = self.store.add_message(
                identity,
                session_id,
                role="assistant",
                content=answer_text,
                answer_status="supported",
                run_id=run_id,
                metadata={
                    "claims": claims,
                    "citations": citations,
                    "limitations": limitations,
                    "tool_source": "zhuojian-agent-tools/v1",
                },
            )
            return {
                "answer_status": "supported",
                "message": message,
                "claims": claims,
                "citations": citations,
                "limitations": limitations,
                "run": self.store.get_run(identity, run_id),
            }
        except AgentError as error:
            return self._fail_answer(
                identity,
                session_id=session_id,
                run_id=run_id,
                step_id=current_step,
                error=error,
            )

    def _fail_answer(
        self,
        identity: IdentityContext,
        *,
        session_id: str,
        run_id: str,
        step_id: str | None,
        error: AgentError,
    ) -> dict[str, Any]:
        if step_id is not None:
            try:
                self.store.update_step_status(identity, step_id, "failed", error_code=error.code)
            except AgentError:
                pass
        run = self.store.get_run(identity, run_id)
        for step in run["steps"]:
            if step["status"] == "queued":
                self.store.update_step_status(identity, step["step_id"], "skipped")
        self.store.update_run_status(identity, run_id, "failed")
        content = "当前证据不足，无法判断。"
        if error.safe_next_step:
            content += f" {error.safe_next_step}"
        message = self.store.add_message(
            identity,
            session_id,
            role="assistant",
            content=content,
            answer_status="insufficient_evidence",
            run_id=run_id,
            metadata={
                "error_code": error.code,
                "retryable": error.retryable,
                "claims": [],
                "citations": [],
            },
        )
        return {
            "answer_status": "insufficient_evidence",
            "message": message,
            "claims": [],
            "citations": [],
            "limitations": [{"code": error.code, "message": error.message}],
            "run": self.store.get_run(identity, run_id),
        }
