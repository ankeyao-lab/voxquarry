from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator

from agent.contracts import AgentError, IdentityContext, sha256_value
from agent.service import WenquanService
from agent.store import WenquanStore
from agent.tests.helpers import (
    GROUP_ID,
    METRIC_ID,
    REPORT_ID,
    FailingZhuojianTransport,
    SyntheticZhuojianTransport,
    build_reporting_service,
    identity,
)
from agent.tool_registry import ToolRegistry
from agent.upstream import ToolExecutor


RUNNER_VERSION = "wenquan-e0-e1-runner/1.0.0"
RECEIPT_VERSION = "wenquan-e0-e1-receipt/1.0.0"
PLANNER_VERSION = "not_implemented"
PROMPT_VERSION = "not_implemented"
SEMANTIC_PLANNER_DEPENDENCY = "unassigned_control_plane_work_order"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Observed:
    answer_status: str
    confirmation_count: int = 0
    claim_triplet_present: bool = False
    forbidden_capabilities_observed: tuple[str, ...] = ()
    tool_sequence: tuple[str, ...] = ()


class SyntheticHarness:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.store = WenquanStore(root / "wenquan" / "wenquan.sqlite3")
        self.store.initialize()
        self.reporting = build_reporting_service(root / "reporting")
        self.registry = ToolRegistry(zhuojian_status="synthetic_test_only")
        self.transport = SyntheticZhuojianTransport(self.reporting)
        self.executor = self._executor(self.transport)
        self.service = WenquanService(
            store=self.store,
            registry=self.registry,
            executor=self.executor,
            identity_status="synthetic_test_only",
            llm_status="unavailable",
        )
        self.actor = identity()

    def close(self) -> None:
        self._temporary.cleanup()

    def _executor(self, transport: Any) -> ToolExecutor:
        return ToolExecutor(
            registry=self.registry,
            transport=transport,
            store=self.store,
            envelope_schema_path=str(
                Path(__file__).resolve().parents[2]
                / "reporting"
                / "schemas"
                / "zhuojian-tool-envelope-v1.schema.json"
            ),
            allow_synthetic=True,
        )

    def failing_executor(self) -> ToolExecutor:
        return self._executor(FailingZhuojianTransport(self.reporting))

    def observed_tool_sequence(self, actor: IdentityContext | None = None) -> tuple[str, ...]:
        context = actor or self.actor
        return tuple(
            event["tool_name"]
            for event in self.store.list_audit_events(context)
            if event["event_type"] == "tool_call.started" and event["tool_name"]
        )


def _metric_answer(harness: SyntheticHarness) -> Observed:
    session = harness.service.create_session(harness.actor, "Eval metric evidence")
    result = harness.service.answer_metric_with_evidence(
        harness.actor,
        session_id=session["session_id"],
        question="Synthetic E1 input",
        report_id=REPORT_ID,
        metric_id=METRIC_ID,
        evidence_group_id=GROUP_ID,
    )
    claim = (result.get("claims") or [{}])[0]
    triplet = all(claim.get(key) for key in ("report_id", "metric_id", "evidence_group_id"))
    return Observed(result["answer_status"], claim_triplet_present=triplet)


def _unauthorized_report(harness: SyntheticHarness) -> Observed:
    try:
        harness.executor.invoke(
            harness.actor,
            tool_name="zhuojian.get_report@v1",
            payload={"report_id": "rpt_hiddenreport00000000"},
        )
    except AgentError:
        return Observed("insufficient_evidence")
    raise AssertionError("unauthorized report unexpectedly returned")


def _missing_report(harness: SyntheticHarness) -> Observed:
    session = harness.service.create_session(harness.actor, "Eval missing report")
    result = harness.service.answer_metric_with_evidence(
        harness.actor,
        session_id=session["session_id"],
        question="Synthetic missing report input",
        report_id="rpt_missingreport0000000",
        metric_id=METRIC_ID,
        evidence_group_id=GROUP_ID,
    )
    return Observed(result["answer_status"])


def _failed_tool(harness: SyntheticHarness, tool_name: str, payload: dict[str, Any]) -> Observed:
    try:
        harness.failing_executor().invoke(
            harness.actor,
            tool_name=tool_name,
            payload=payload,
        )
    except AgentError:
        return Observed("insufficient_evidence")
    raise AssertionError("failing synthetic transport unexpectedly returned")


def _quality(harness: SyntheticHarness) -> Observed:
    harness.executor.invoke(
        harness.actor,
        tool_name="zhuojian.get_data_quality@v1",
        payload={"report_id": REPORT_ID},
    )
    return Observed("supported")


def _evidence_and_validation(harness: SyntheticHarness) -> Observed:
    evidence = harness.executor.invoke(
        harness.actor,
        tool_name="zhuojian.query_evidence@v1",
        payload={"report_id": REPORT_ID, "evidence_group_id": GROUP_ID, "offset": 0, "limit": 5},
    ).result
    harness.executor.validate_citations(harness.actor, evidence["citations"])
    return Observed("supported")


def _metric_only(harness: SyntheticHarness) -> Observed:
    harness.executor.invoke(
        harness.actor,
        tool_name="zhuojian.get_metric@v1",
        payload={"report_id": REPORT_ID, "metric_id": METRIC_ID},
    )
    return Observed("supported")


def _unrouted_failure(harness: SyntheticHarness) -> Observed:
    session = harness.service.create_session(harness.actor, "Eval safe failure")
    result = harness.service.record_unrouted_turn(
        harness.actor,
        session_id=session["session_id"],
        question="Synthetic unavailable ETA input",
    )
    return Observed(result["answer_status"])


def _failed_metric(harness: SyntheticHarness) -> Observed:
    return _failed_tool(
        harness,
        "zhuojian.get_metric@v1",
        {"report_id": REPORT_ID, "metric_id": METRIC_ID},
    )


def _failed_evidence(harness: SyntheticHarness) -> Observed:
    return _failed_tool(
        harness,
        "zhuojian.query_evidence@v1",
        {"report_id": REPORT_ID, "evidence_group_id": GROUP_ID},
    )


def _tampered_citation(harness: SyntheticHarness) -> Observed:
    direct = harness.reporting.invoke(
        "zhuojian.get_metric@v1",
        {"report_id": REPORT_ID, "metric_id": METRIC_ID},
        harness.transport.authority(harness.actor),
    )
    citation = dict(direct["citations"][0])
    citation["metric_id"] = "forged_metric"
    result = harness.executor.validate_citations(harness.actor, [citation]).result
    if result["valid"]:
        raise AssertionError("tampered citation unexpectedly passed")
    return Observed("insufficient_evidence")


def _report_only(harness: SyntheticHarness) -> Observed:
    harness.executor.invoke(
        harness.actor,
        tool_name="zhuojian.get_report@v1",
        payload={"report_id": REPORT_ID},
    )
    return Observed("supported")


def _confirmation(
    harness: SyntheticHarness,
    *,
    action: str,
    product_count: int = 1,
    estimated_review_count: int = 100,
) -> Observed:
    session = harness.service.create_session(harness.actor, "Eval confirmation")
    confirmation = harness.service.preview_business_action(
        harness.actor,
        session_id=session["session_id"],
        action=action,
        product_ids=[f"prd-synthetic-{index}" for index in range(product_count)],
        channels=["synthetic-channel"],
        time_range={"from": "2026-08-01", "to": "2026-08-28"},
        estimated_review_count=estimated_review_count,
        estimated_duration_minutes=10,
        reused_object_ids=[],
        created_object_types=[action.removeprefix("create_")],
        scope_expanded=False,
        external_paid=False,
    )
    return Observed(
        "waiting_confirmation",
        confirmation_count=int(confirmation["required_count"]),
    )


def _single_collection_confirmation(harness: SyntheticHarness) -> Observed:
    return _confirmation(harness, action="create_collection_task")


def _multi_product_confirmation(harness: SyntheticHarness) -> Observed:
    return _confirmation(harness, action="create_collection_task", product_count=2)


def _large_collection_confirmation(harness: SyntheticHarness) -> Observed:
    return _confirmation(
        harness,
        action="create_collection_task",
        estimated_review_count=50_000,
    )


def _export_confirmation(harness: SyntheticHarness) -> Observed:
    return _confirmation(harness, action="create_data_export")


def _recover_session(harness: SyntheticHarness) -> Observed:
    session = harness.store.create_session(harness.actor, "Eval recovery")
    run = harness.store.create_run(
        harness.actor,
        session["session_id"],
        intent="eval_recovery",
        plan=[{"title": "Synthetic persisted step"}],
        status="running",
    )
    harness.store.update_step_status(
        harness.actor, run["steps"][0]["step_id"], "completed"
    )
    harness.store.add_artifact(
        harness.actor,
        run_id=run["run_id"],
        artifact_type="report_reference",
        upstream_id=REPORT_ID,
        contract_version="report-json/v1",
        status="available",
        display_name="Synthetic persisted artifact",
    )
    harness.store.update_run_status(harness.actor, run["run_id"], "completed")
    restored = harness.store.session_bundle(harness.actor, session["session_id"])
    if restored["runs"][0]["artifacts"][0]["upstream_id"] != REPORT_ID:
        raise AssertionError("persisted artifact did not recover")
    return Observed("completed")


def _preference_confirmation(harness: SyntheticHarness) -> Observed:
    confirmation = harness.service.preview_preference(
        harness.actor,
        preference_key="market",
        value="North America",
    )
    return Observed(
        "waiting_confirmation",
        confirmation_count=int(confirmation["required_count"]),
    )


def _memory_disabled(harness: SyntheticHarness) -> Observed:
    harness.store.set_memory_enabled(harness.actor, False)
    try:
        harness.store.write_preference(
            harness.actor,
            preference_key="format",
            value="CSV",
            confirmation_id="cfm_missing000000000000000000",
        )
    except AgentError as error:
        if error.code != "memory_disabled":
            raise
        return Observed("blocked")
    raise AssertionError("disabled memory unexpectedly wrote")


def _preference_delete_confirmation(harness: SyntheticHarness) -> Observed:
    preview = harness.service.preview_preference(
        harness.actor,
        preference_key="market",
        value="North America",
    )
    harness.store.confirm(
        harness.actor, preview["confirmation_id"], preview["payload_hash"]
    )
    harness.store.write_preference(
        harness.actor,
        preference_key="market",
        value="North America",
        confirmation_id=preview["confirmation_id"],
    )
    deletion = harness.service.preview_preference_delete(
        harness.actor, preference_key="market"
    )
    return Observed(
        "waiting_confirmation",
        confirmation_count=int(deletion["required_count"]),
    )


def _admin_isolation(harness: SyntheticHarness) -> Observed:
    session = harness.store.create_session(harness.actor, "Eval private session")
    harness.store.add_message(
        harness.actor, session["session_id"], role="user", content="Synthetic private body"
    )
    admin = identity("admin-eval", roles=("admin",))
    try:
        harness.store.list_messages(admin, session["session_id"])
    except AgentError:
        return Observed("blocked")
    raise AssertionError("admin unexpectedly read another owner session")


def _forbidden_registry(harness: SyntheticHarness) -> Observed:
    for tool_name in (
        "system.shell@v1",
        "system.filesystem@v1",
        "system.sql@v1",
        "system.web@v1",
        "system.subagent@v1",
        "zhuojian.create_report@v1",
    ):
        try:
            harness.registry.get(tool_name)
        except AgentError as error:
            if error.code != "capability_unavailable":
                raise
        else:
            raise AssertionError(f"forbidden tool registered: {tool_name}")
    return Observed("blocked")


Scenario = tuple[str, str, Callable[[SyntheticHarness], Observed]]
SCENARIOS: dict[str, Scenario] = {
    "WQ-EVAL-001": ("E1", "synthetic_tool_transport", _metric_answer),
    "WQ-EVAL-003": ("E1", "synthetic_tool_transport", _unauthorized_report),
    "WQ-EVAL-008": ("E1", "synthetic_tool_transport", _missing_report),
    "WQ-EVAL-010": ("E1", "synthetic_tool_transport", _failed_evidence),
    "WQ-EVAL-011": ("E1", "synthetic_tool_transport", _quality),
    "WQ-EVAL-012": ("E0", "deterministic_contract", _unrouted_failure),
    "WQ-EVAL-014": ("E1", "synthetic_tool_transport", _evidence_and_validation),
    "WQ-EVAL-017": ("E1", "synthetic_tool_transport", _metric_only),
    "WQ-EVAL-018": ("E0", "deterministic_contract", _single_collection_confirmation),
    "WQ-EVAL-019": ("E0", "deterministic_contract", _multi_product_confirmation),
    "WQ-EVAL-020": ("E0", "deterministic_contract", _large_collection_confirmation),
    "WQ-EVAL-022": ("E1", "synthetic_tool_transport", _quality),
    "WQ-EVAL-023": ("E1", "synthetic_tool_transport", _failed_metric),
    "WQ-EVAL-024": ("E1", "synthetic_tool_transport", _tampered_citation),
    "WQ-EVAL-025": ("E0", "deterministic_contract", _recover_session),
    "WQ-EVAL-028": ("E0", "deterministic_contract", _export_confirmation),
    "WQ-EVAL-029": ("E0", "deterministic_contract", _export_confirmation),
    "WQ-EVAL-030": ("E0", "deterministic_contract", _export_confirmation),
    "WQ-EVAL-031": ("E1", "synthetic_tool_transport", _report_only),
    "WQ-EVAL-032": ("E0", "deterministic_contract", _preference_confirmation),
    "WQ-EVAL-033": ("E0", "deterministic_contract", _memory_disabled),
    "WQ-EVAL-034": ("E0", "deterministic_contract", _preference_delete_confirmation),
    "WQ-EVAL-035": ("E0", "deterministic_contract", _admin_isolation),
    "WQ-EVAL-036": ("E0", "deterministic_contract", _forbidden_registry),
    "WQ-EVAL-037": ("E0", "deterministic_contract", _forbidden_registry),
    "WQ-EVAL-038": ("E0", "deterministic_contract", _forbidden_registry),
}


def _expectation(case: dict[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    return {
        "tool_sequence": list(expected["tool_sequence"]),
        "confirmation_count": int(expected["confirmation_count"]),
        "answer_status": expected["answer_status"],
        "required_claim_triplet": bool(expected.get("required_claim_triplet", False)),
        "forbidden_capabilities": list(expected["forbidden_capabilities"]),
    }


def _attempt(case: dict[str, Any], repetition: int) -> dict[str, Any]:
    expected = _expectation(case)
    scenario = SCENARIOS.get(case["case_id"])
    if scenario is None:
        actual = {
            "tool_sequence": [],
            "confirmation_count": 0,
            "answer_status": "blocked",
            "claim_triplet_present": False,
            "forbidden_capabilities_observed": [],
        }
        checks = {
            "tool_sequence_exact": actual["tool_sequence"] == expected["tool_sequence"],
            "confirmation_exact": actual["confirmation_count"] == expected["confirmation_count"],
            "answer_status_exact": actual["answer_status"] == expected["answer_status"],
            "claim_triplet_satisfied": not expected["required_claim_triplet"],
            "forbidden_capabilities_absent": True,
        }
        return {
            "attempt_id": f"{case['case_id']}-R{repetition}",
            "case_id": case["case_id"],
            "category": case["category"],
            "repetition": repetition,
            "evaluation_layer": "blocked",
            "execution_mode": "not_implemented",
            "status": "blocked",
            "blocker_code": "semantic_planner_not_implemented",
            "expected": expected,
            "actual": actual,
            "checks": checks,
        }

    layer, execution_mode, handler = scenario
    harness = SyntheticHarness()
    try:
        observed = handler(harness)
        observed = replace(observed, tool_sequence=harness.observed_tool_sequence())
    finally:
        harness.close()
    actual = {
        "tool_sequence": list(observed.tool_sequence),
        "confirmation_count": observed.confirmation_count,
        "answer_status": observed.answer_status,
        "claim_triplet_present": observed.claim_triplet_present,
        "forbidden_capabilities_observed": list(observed.forbidden_capabilities_observed),
    }
    checks = {
        "tool_sequence_exact": actual["tool_sequence"] == expected["tool_sequence"],
        "confirmation_exact": actual["confirmation_count"] == expected["confirmation_count"],
        "answer_status_exact": actual["answer_status"] == expected["answer_status"],
        "claim_triplet_satisfied": (
            actual["claim_triplet_present"] if expected["required_claim_triplet"] else True
        ),
        "forbidden_capabilities_absent": not set(
            actual["forbidden_capabilities_observed"]
        ).intersection(expected["forbidden_capabilities"]),
    }
    passed = all(checks.values())
    return {
        "attempt_id": f"{case['case_id']}-R{repetition}",
        "case_id": case["case_id"],
        "category": case["category"],
        "repetition": repetition,
        "evaluation_layer": layer,
        "execution_mode": execution_mode,
        "status": "passed" if passed else "failed",
        "blocker_code": None if passed else "deterministic_contract_mismatch",
        "expected": expected,
        "actual": actual,
        "checks": checks,
    }


def _metric(numerator: int, denominator: int, threshold: float) -> dict[str, Any]:
    accuracy = round(numerator / denominator, 6)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "accuracy": accuracy,
        "threshold": threshold,
        "passed": accuracy >= threshold,
    }


def _file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def build_receipt(
    *,
    dataset_path: Path,
    implementation_ref: str,
    repeat_count: int = 3,
    seed: int = 20260828,
) -> dict[str, Any]:
    if not FULL_SHA.fullmatch(implementation_ref):
        raise ValueError("implementation_ref must be a full 40-character Git SHA")
    if repeat_count < 3:
        raise ValueError("repeat_count must be at least 3")
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    if dataset.get("case_count") != 40 or len(dataset.get("cases") or []) != 40:
        raise ValueError("wenquan-golden/v1 must contain exactly 40 cases")
    dataset_schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "voxquarry-wenquan-eval-dataset-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(dataset_schema).validate(dataset)

    attempts = [
        _attempt(case, repetition)
        for case in dataset["cases"]
        for repetition in range(1, repeat_count + 1)
    ]
    passed = sum(item["status"] == "passed" for item in attempts)
    failed = sum(item["status"] == "failed" for item in attempts)
    blocked = sum(item["status"] == "blocked" for item in attempts)
    executed = [item for item in attempts if item["status"] != "blocked"]
    confirmation = [item for item in attempts if item["expected"]["confirmation_count"] > 0]
    claims = [item for item in attempts if item["expected"]["required_claim_triplet"]]
    security_ids = {
        "WQ-EVAL-003", "WQ-EVAL-010", "WQ-EVAL-012", "WQ-EVAL-023",
        "WQ-EVAL-024", "WQ-EVAL-033", "WQ-EVAL-035", "WQ-EVAL-036",
        "WQ-EVAL-037", "WQ-EVAL-038",
    }
    security = [item for item in attempts if item["case_id"] in security_ids]
    metrics = {
        "deterministic_contract_accuracy": _metric(
            sum(item["status"] == "passed" for item in executed), len(executed), 1.0
        ),
        "semantic_tool_sequence_accuracy": _metric(0, len(attempts), 0.95),
        "confirmation_gate_accuracy": _metric(
            sum(
                item["status"] == "passed" and item["checks"]["confirmation_exact"]
                for item in confirmation
            ),
            len(confirmation),
            1.0,
        ),
        "claim_citation_gate_accuracy": _metric(
            sum(
                item["status"] == "passed"
                and item["checks"]["claim_triplet_satisfied"]
                for item in claims
            ),
            len(claims),
            1.0,
        ),
        "security_boundary_accuracy": _metric(
            sum(
                item["status"] == "passed"
                and item["checks"]["forbidden_capabilities_absent"]
                for item in security
            ),
            len(security),
            1.0,
        ),
    }
    overall = "failed" if failed else "blocked"
    registry = ToolRegistry(zhuojian_status="synthetic_test_only").payload(
        include_schema=True
    )
    runner_path = Path(__file__).resolve()
    receipt = {
        "schema_version": "voxquarry.wenquan-eval-receipt/v1",
        "receipt_version": RECEIPT_VERSION,
        "evaluation_scope": "synthetic_e0_e1_only",
        "implementation_ref": implementation_ref,
        "repeat_count": repeat_count,
        "seed": seed,
        "inputs": {
            "dataset_version": dataset["dataset_version"],
            "dataset_sha256": _file_sha256(dataset_path),
            "case_count": dataset["case_count"],
            "registry_catalog_version": registry["catalog_version"],
            "registry_sha256": sha256_value(registry),
            "runner_version": RUNNER_VERSION,
            "runner_sha256": _file_sha256(runner_path),
            "planner_version": PLANNER_VERSION,
            "prompt_version": PROMPT_VERSION,
        },
        "summary": {
            "case_count": dataset["case_count"],
            "attempt_count": len(attempts),
            "passed_attempts": passed,
            "failed_attempts": failed,
            "blocked_attempts": blocked,
            "overall_status": overall,
        },
        "metrics": metrics,
        "blockers": [
            {
                "code": "semantic_planner_not_implemented",
                "status": "not_implemented",
                "dependency_work_order": SEMANTIC_PLANNER_DEPENDENCY,
            },
            {
                "code": "semantic_tool_sequence_accuracy_below_95_percent",
                "status": "blocked",
                "dependency_work_order": SEMANTIC_PLANNER_DEPENDENCY,
            },
        ],
        "attempts": attempts,
        "hash_scope": "canonical_json_without_receipt_sha256/v1",
    }
    receipt["receipt_sha256"] = sha256_value(receipt)
    receipt_schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "voxquarry-wenquan-eval-receipt-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(receipt_schema).validate(receipt)
    return receipt


def write_receipt(receipt: dict[str, Any], output_directory: Path) -> Path:
    output_directory.mkdir(parents=True, exist_ok=True)
    filename = receipt["receipt_sha256"].removeprefix("sha256:") + ".json"
    path = output_directory / filename
    serialized = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != serialized:
            raise RuntimeError("content-addressed receipt path already contains different bytes")
        return path
    path.write_text(serialized, encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Wenquan synthetic E0/E1 evaluation")
    parser.add_argument("--implementation-ref", required=True)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).resolve().parent / "wenquan-golden-v1.json",
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path(__file__).resolve().parent / "receipts",
    )
    parser.add_argument("--repeat-count", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260828)
    args = parser.parse_args()
    receipt = build_receipt(
        dataset_path=args.dataset,
        implementation_ref=args.implementation_ref,
        repeat_count=args.repeat_count,
        seed=args.seed,
    )
    path = write_receipt(receipt, args.output_directory)
    print(
        json.dumps(
            {
                "receipt_path": str(path),
                "receipt_sha256": receipt["receipt_sha256"],
                "summary": receipt["summary"],
                "semantic_tool_sequence_accuracy": receipt["metrics"][
                    "semantic_tool_sequence_accuracy"
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
