from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator

from agent.contracts import AgentError
from agent.readonly import PLAN, ReadonlyQA, compact_scope, scope_description
from agent.service import WenquanService
from agent.store import WenquanStore
from agent.tests.helpers import (
    GROUP_ID,
    METRIC_ID,
    REPORT_ID,
    SNAPSHOT_ID,
    FailingZhuojianTransport,
    SyntheticZhuojianTransport,
    build_reporting_service,
    fixture_report,
    identity,
)
from agent.tool_registry import ToolRegistry
from agent.upstream import ToolExecutor


def selection(*facts: str, groups: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"status": "answer", "fact_ids": list(facts), "evidence_group_ids": list(groups)}


class StaticRuntime:
    status = "ready"

    def __init__(self, output: dict[str, Any] | None = None, *, block: bool = False,
                 error: AgentError | None = None) -> None:
        self.output = output if output is not None else selection("metric:" + METRIC_ID)
        self.contexts: list[dict[str, Any]] = []
        self.prompts: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = block
        self.error = error

    def complete(self, **kwargs: Any) -> dict[str, Any]:
        self.contexts.append(json.loads(kwargs["messages"][-1]["content"]))
        self.prompts.append(kwargs["messages"][0]["content"])
        self.started.set()
        if self.block and not self.release.wait(8):
            raise AgentError("llm_unavailable", "合成模型等待超时", http_status=503)
        if self.error:
            raise self.error
        return {"model": "synthetic-fact-selector", "usage": {"total_tokens": 3},
                "choices": [{"message": {"content": json.dumps(self.output)}}]}


class AlteredTransport(SyntheticZhuojianTransport):
    def __init__(self, service: Any, mode: str) -> None:
        super().__init__(service)
        self.mode = mode
        self.report_reads = 0

    def call(self, tool_name: str, payload: Any, context: Any, definition: Any) -> dict[str, Any]:
        result = copy.deepcopy(super().call(tool_name, payload, context, definition))
        if tool_name == "zhuojian.get_report@v1":
            self.report_reads += 1
            if self.mode == "drift" and self.report_reads == 2:
                result["data"]["findings"][0]["body"] = "来源已变更，不应发布旧结论。"
            if self.mode == "wrong_report" and self.report_reads == 1:
                result["report"]["report_id"] = "rpt_wrongresponse1234567890"
            if self.mode == "large_tables":
                for suffix in ("a", "b"):
                    result["data"]["tables"].append({
                        "table_id": "large_" + suffix, "title": "较大合成表格 " + suffix,
                        "columns": [{"field": "label", "label": "名称"},
                                    {"field": "count", "label": "已有数量"}],
                        "denominator_note": "仅供两阶段路由回归测试。",
                        "rows": [{"label": f"row-{i}", "count": i} for i in range(50)],
                    })
            if self.mode == "truncated_table":
                result["data"]["tables"][0]["rows"] = [
                    {"label": f"已保存项目 {i}", "mention_count": i}
                    for i in range(85)
                ]
        if self.mode == "wrong_metric_citation" and tool_name == "zhuojian.get_metric@v1":
            alternate = self.service.invoke("zhuojian.get_metric@v1", {
                "report_id": REPORT_ID, "metric_id": "secondary_rate",
            }, self.authority(context))
            result["citations"] = alternate["citations"]
        if self.mode == "tampered_quote" and tool_name == "zhuojian.query_evidence@v1":
            # Keep the genuine, individually valid citation, but alter its payload.
            result["data"]["items"][0]["quote"] = "篡改后的评论声称 99.9% 满意，不能发布。"
        if self.mode == "empty_citations":
            result["citations"] = []
        return result


class ReadonlyQATests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = WenquanStore(root / "wenquan.sqlite3")
        self.store.initialize()
        enriched = fixture_report()
        enriched["scope"].update({
            "date_range": {"start": "2026-08-01", "end": "2026-08-31"},
            "market_regions": ["North America"],
            "voltage_platform_keys": ["SYNTHETIC|56V"],
            "voltage_platform_ids": ["vpf_" + "1" * 20],
        })
        enriched["metric_definitions"].append({
            **enriched["metric_definitions"][0], "metric_id": "secondary_rate", "name": "另一项指标",
        })
        enriched["metrics"].append({
            **enriched["metrics"][0], "metric_id": "secondary_rate", "value": 0.5, "numerator": 10,
        })
        enriched["findings"] = [{
            "finding_id": "pain", "title": "已保存痛点", "body": "合成报告中的动力投诉。",
            "confidence": "low-sample", "metric_refs": [], "table_refs": ["pain_points"],
            "evidence_refs": ["ev_wenquan_1"],
        }]
        enriched["tables"] = [{
            "table_id": "pain_points", "title": "痛点与故障",
            "columns": [{"field": "label", "label": "因子"},
                        {"field": "mention_count", "label": "提及实体"}],
            "denominator_note": "分母为 18 条有正文唯一评论实体。",
            "rows": [{"factor_id": "power", "label": "动力", "mention_count": 2,
                      "quality_flag": "low-sample", "evidence_ids": ["ev_wenquan_1"]}],
        }]
        # ReportStore is immutable: seed the richer report on its first write.
        with patch("agent.tests.helpers.fixture_report", return_value=enriched):
            self.reporting = build_reporting_service(root / "reporting")
        self.registry = ToolRegistry(zhuojian_status="synthetic_test_only")
        self.transport = SyntheticZhuojianTransport(self.reporting)
        self.actor = identity()
        self.runtime = StaticRuntime()
        self.runtimes = [self.runtime]
        self.service = WenquanService(
            store=self.store, registry=self.registry, executor=self.executor(self.transport),
            planner=SimpleNamespace(runtime=self.runtime), identity_status="synthetic_test_only",
        )
        self.qa = ReadonlyQA(self.service)
        self.instances = [self.qa]
        self.session_id = self.service.create_session(self.actor, "只读问答测试")["session_id"]
        self.sequence = 0

    def tearDown(self) -> None:
        for runtime in self.runtimes:
            runtime.release.set()
        for qa in self.instances:
            qa.close()
        self.temporary.cleanup()

    def executor(self, transport: Any) -> ToolExecutor:
        return ToolExecutor(registry=self.registry, transport=transport, store=self.store,
                            envelope_schema_path=str(Path(__file__).resolve().parents[2] /
                                                     "reporting/schemas/zhuojian-tool-envelope-v1.schema.json"),
                            allow_synthetic=True)

    def use_runtime(self, runtime: StaticRuntime) -> None:
        self.runtime = runtime
        self.runtimes.append(runtime)
        self.service.planner.runtime = runtime

    def wait(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            run = self.store.get_run(self.actor, run_id)
            if run["status"] != "running":
                return run
            time.sleep(0.01)
        self.fail("只读回答未在合成测试时限内结束")

    def submit(self, question: str = "好评率是多少？", *, report_id: str | None = REPORT_ID) -> tuple[dict[str, Any], dict[str, Any]]:
        self.sequence += 1
        accepted = self.qa.submit(self.actor, session_id=self.session_id, question=question,
                                  report_id=report_id, request_id=f"request-{self.sequence:04d}")
        run = self.wait(accepted["run"]["run_id"])
        messages = [m for m in self.store.list_messages(self.actor, self.session_id)
                    if m.get("run_id") == run["run_id"] and m["role"] == "assistant"]
        self.assertEqual(len(messages), 1)
        return run, messages[0]

    def assert_failed(self, run: dict[str, Any], message: dict[str, Any], code: str) -> None:
        self.assertIn(run["status"], {"blocked", "failed"})
        self.assertEqual(message["metadata"]["error"]["code"], code)
        self.assertEqual(message["metadata"]["claims"], [])
        self.assertEqual(message["metadata"]["citations"], [])

    def test_english_answer_preserves_validated_metric_and_citations(self):
        run, message = self.submit("What is the positive rating rate? Respond in English.")
        self.assertEqual(run["status"], "completed")
        self.assertIn("Numerator: 15", message["content"])
        self.assertIn("Denominator: 20", message["content"])
        self.assertIn("0.75", message["content"])
        self.assertNotIn("分子", message["content"])
        self.assertEqual(message["metadata"]["claims"][0]["source_pointer"], {"metric_id": METRIC_ID})
        self.assertTrue(message["metadata"]["citations"])

    def test_metric_uses_saved_75_percent_15_of_20_without_requiring_evidence(self) -> None:
        run, message = self.submit()
        self.assertEqual(run["status"], "completed")
        self.assertIn("75.00%", message["content"])
        self.assertIn("分子：15", message["content"])
        self.assertIn("分母：20", message["content"])
        self.assertIn("样本量：20", message["content"])
        metadata = message["metadata"]
        claim = metadata["claims"][0]
        self.assertEqual(claim["report_id"], REPORT_ID)
        self.assertEqual(claim["snapshot_id"], SNAPSHOT_ID)
        self.assertEqual(claim["source_pointer"], {"metric_id": METRIC_ID})
        self.assertTrue(claim["source_sha256"].startswith("sha256:"))
        self.assertNotIn("zhuojian.query_evidence@v1", self.transport.calls)
        self.assertEqual(self.transport.calls[-1], "zhuojian.citation_validator@v1")
        self.assertTrue(all(step["status"] == "completed" for step in run["steps"]))
        self.assertTrue(any(c["citation_type"] == "metric" for c in metadata["citations"]))

    def test_metric_remains_answerable_when_legacy_report_has_no_evidence_sidecar(self) -> None:
        self.reporting.evidence.path_for(REPORT_ID).unlink()
        run, message = self.submit()
        self.assertEqual(run["status"], "completed", message["metadata"])
        self.assertIn("75.00%", message["content"])
        self.assertEqual(self.runtime.contexts[0]["evidence_groups"], [])
        self.assertNotIn("zhuojian.query_evidence@v1", self.transport.calls)

    def test_saved_finding_preserves_text_confidence_and_report_citation(self) -> None:
        self.use_runtime(StaticRuntime(selection("finding:pain")))
        run, message = self.submit("主要痛点是什么？")
        self.assertEqual(run["status"], "completed")
        self.assertIn("合成报告中的动力投诉。", message["content"])
        self.assertIn("low-sample", message["content"])
        self.assertEqual(message["metadata"]["claims"][0]["source_pointer"], {"finding_id": "pain"})
        self.assertEqual({c["citation_type"] for c in message["metadata"]["citations"]}, {"report"})

    def test_table_preserves_denominator_and_singular_quality_flag(self) -> None:
        self.use_runtime(StaticRuntime(selection("table:pain_points:0")))
        run, message = self.submit("痛点表的动力数据是什么？")
        self.assertEqual(run["status"], "completed")
        self.assertIn("提及实体：2", message["content"])
        self.assertIn("分母为 18 条有正文唯一评论实体。", message["content"])
        self.assertIn("low-sample", message["content"])
        self.assertEqual(message["metadata"]["claims"][0]["source_pointer"]["table_id"], "pain_points")

    def test_scope_preserves_current_contract_fields_and_readable_report_card(self) -> None:
        self.use_runtime(StaticRuntime(selection("scope")))
        run, message = self.submit("这份报告的市场、日期和电压平台范围是什么？")
        self.assertEqual(run["status"], "completed", message["metadata"])
        scope = self.runtime.contexts[0]["scope"]
        self.assertEqual(scope["date_range"], {"start": "2026-08-01", "end": "2026-08-31"})
        self.assertEqual(scope["market_regions"], ["North America"])
        self.assertEqual(scope["voltage_platform_keys"], ["SYNTHETIC|56V"])
        self.assertEqual(scope["voltage_platform_ids"], ["vpf_" + "1" * 20])
        card = self.qa.reports(self.actor)["items"][0]
        self.assertEqual(card["scope"], scope)
        for text in (message["content"], card["scope_label"]):
            self.assertIn("来源市场：北美", text)
            self.assertIn("起始 2026-08-01", text)
            self.assertIn("截至 2026-08-31", text)
            self.assertIn("电压平台：SYNTHETIC 56V", text)
            self.assertNotIn("未命名平台", text)

    def test_legacy_scope_with_only_platform_ids_remains_identifiable(self) -> None:
        source = {"voltage_platform_ids": ["vpf_" + "2" * 20]}
        self.assertEqual(compact_scope(source), source)
        self.assertEqual(scope_description(source), "电压平台 ID：vpf_" + "2" * 20)

    def test_truncated_table_answer_exposes_coverage_and_full_report_entry(self) -> None:
        self.service.executor = self.executor(AlteredTransport(self.reporting, "truncated_table"))
        self.use_runtime(StaticRuntime(selection("table:pain_points:79")))
        run, message = self.submit("展示可读取的最后一行")
        self.assertEqual(run["status"], "completed", message["metadata"])
        context = self.runtime.contexts[0]
        coverage = context["retrieval_coverage"]
        self.assertEqual(coverage["max_rows_per_table"], 80)
        self.assertEqual(coverage["truncated_tables"][0]["total_rows"], 85)
        self.assertFalse(coverage["complete"])
        self.assertNotIn("table:pain_points:80", [f["fact_id"] for f in context["facts"]])
        self.assertIn("前 80 行", self.runtime.prompts[0])
        self.assertIn("不代表完整报告不存在", self.runtime.prompts[0])
        self.assertIn("已保存项目 79", message["content"])
        self.assertIn("部分表格未完整读取", message["content"])
        self.assertIn("打开报告", message["content"])
        self.assertTrue(any(x["code"] == "retrieval_coverage_limited"
                            for x in message["metadata"]["limitations"]))
        self.assertEqual(message["metadata"]["citations"][0]["href"], coverage["report_href"])

    def test_missing_fact_beyond_scanned_rows_does_not_claim_absence_from_report(self) -> None:
        self.service.executor = self.executor(AlteredTransport(self.reporting, "truncated_table"))
        self.use_runtime(StaticRuntime({"status": "insufficient_evidence", "fact_ids": [], "evidence_group_ids": []}))
        run, message = self.submit("第 85 行的已保存项目 84 是否不存在？")
        self.assert_failed(run, message, "insufficient_evidence")
        self.assertEqual(run["status"], "blocked")
        self.assertIn("本次已读取内容中", message["content"])
        self.assertIn("不能据此断言完整报告没有", message["content"])
        self.assertNotIn("这份报告没有", message["content"])
        self.assertIn("/?report=" + REPORT_ID + "#insights",
                      message["metadata"]["error"]["safe_next_step"])

    def test_forged_fact_and_extra_model_prose_are_rejected(self) -> None:
        invalid = [selection("metric:forged_metric"),
                   {**selection("metric:" + METRIC_ID), "text": "伪造 99.9% 结论"}]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.use_runtime(StaticRuntime(payload))
                before = len(self.transport.calls)
                run, message = self.submit()
                self.assert_failed(run, message, "llm_contract_invalid")
                self.assertNotIn("99.9", message["content"])
                self.assertNotIn("zhuojian.get_metric@v1", self.transport.calls[before:])

    def test_explicit_selection_contract_is_last_and_permits_exact_ids_with_digits(self) -> None:
        runtime = StaticRuntime(selection("table:pain_points:0"))
        self.use_runtime(runtime)
        with patch.object(runtime, "complete", wraps=runtime.complete) as complete:
            run, message = self.submit("显示已保存痛点表中的动力数据")
        self.assertEqual(run["status"], "completed", message["metadata"])
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(complete.call_args.kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(complete.call_args.kwargs["tools"], [])
        context = runtime.contexts[0]
        self.assertEqual(list(context)[-1], "response_contract")
        contract = context["response_contract"]
        schema = contract["schema"]
        Draft202012Validator.check_schema(schema)
        self.assertEqual(set(schema["required"]), {"status", "fact_ids", "evidence_group_ids"})
        self.assertFalse(schema["additionalProperties"])
        validator = Draft202012Validator(schema)
        validator.validate(selection("table:pain_points:0"))
        for example in contract["examples"]:
            validator.validate(example)
        self.assertFalse(validator.is_valid(selection("table:pain_points:999")))
        self.assertNotIn(context["report"]["status"], schema["properties"]["status"]["enum"])
        self.assertIn("清单ID中的数字、冒号等必须原样复制", runtime.prompts[0])
        self.assertIn("销量、市场规模或未来趋势预测", runtime.prompts[0])
        self.assertIn("必须用insufficient_evidence", runtime.prompts[0])
        self.assertIn('"additionalProperties":false', runtime.prompts[0])
        self.assertNotIn("selection_diagnostics", message["metadata"])

    def test_selection_shape_diagnostics_distinguish_failures_without_relaxing_validation(self) -> None:
        cases = (
            ([], "root_shape"),
            ({"status": "answer", "fact_ids": ["metric:" + METRIC_ID]}, "field_set"),
            ({**selection("metric:" + METRIC_ID), "status": "supported"}, "status_value"),
            ({**selection("metric:" + METRIC_ID), "status": []}, "status_value"),
            (selection("metric:unknown_private_id"), "fact_ids"),
            (selection(groups=("evg_unknown_private_id",)), "evidence_group_ids"),
        )
        for payload, stage in cases:
            with self.subTest(stage=stage, kind=type(payload).__name__):
                self.use_runtime(StaticRuntime(payload))
                run, message = self.submit()
                self.assert_failed(run, message, "llm_contract_invalid")
                metadata = message["metadata"]
                self.assertEqual(metadata["model_calls"], 1)
                self.assertEqual(metadata["usage"], {"total_tokens": 3})
                diagnostics = metadata["selection_diagnostics"]
                self.assertEqual(diagnostics["failure_stage"], stage)
                self.assertEqual(len(diagnostics["calls"]), 1)
                self.assertEqual(diagnostics["calls"][0]["status"], "invalid")
                self.assertNotIn("unknown_private_id", json.dumps(metadata))
                self.assertEqual(len(self.runtime.contexts), 1, "格式失败不得自动重试")

    def test_extra_keys_raw_final_and_reasoning_never_enter_failure_diagnostics(self) -> None:
        private = "private-diagnostic-source-999"
        payload = {**selection("metric:" + METRIC_ID), private: "原始输出 " + private}

        class DiagnosticRuntime(StaticRuntime):
            def complete(self, **kwargs: Any) -> dict[str, Any]:
                response = super().complete(**kwargs)
                response["usage"].update({"provider_debug": private,
                    "completion_tokens_details": {"reasoning_tokens": 2, "reasoning_content": private}})
                response["choices"][0].update(finish_reason="stop")
                response["choices"][0]["message"]["reasoning_content"] = private
                response["reasoning_content"] = private
                return response

        self.use_runtime(DiagnosticRuntime(payload))
        run, message = self.submit()
        self.assert_failed(run, message, "llm_contract_invalid")
        metadata = message["metadata"]
        call = metadata["selection_diagnostics"]["calls"][0]
        self.assertEqual(call["missing_expected_keys"], [])
        self.assertEqual(call["extra_key_count"], 1)
        self.assertTrue(call["status_is_allowed"])
        self.assertEqual(call["root_type"], "object")
        self.assertEqual(call["finish_reason"], "stop")
        self.assertEqual(call["final_sha256"], "sha256:" + hashlib.sha256(json.dumps(payload).encode()).hexdigest())
        self.assertEqual(metadata["usage"], {"total_tokens": 3,
                                           "completion_tokens_details": {"reasoning_tokens": 2}})
        self.assertNotIn(private, json.dumps(metadata, ensure_ascii=False))
        self.assertNotIn("reasoning_content", json.dumps(metadata))
        self.assertNotIn("provider_debug", json.dumps(metadata))
        self.assertEqual(len(self.runtime.contexts), 1)

    def test_malformed_json_preserves_usage_and_safe_parse_stage(self) -> None:
        class MalformedRuntime(StaticRuntime):
            def complete(self, **kwargs: Any) -> dict[str, Any]:
                response = super().complete(**kwargs)
                response["choices"][0]["message"]["content"] = "private-invalid-json"
                return response

        self.use_runtime(MalformedRuntime())
        run, message = self.submit()
        self.assert_failed(run, message, "llm_contract_invalid")
        metadata = message["metadata"]
        self.assertEqual(metadata["selection_diagnostics"]["failure_stage"], "json_parse")
        self.assertEqual(metadata["usage"], {"total_tokens": 3})
        self.assertEqual(metadata["model_calls"], 1)
        self.assertNotIn("private-invalid-json", json.dumps(metadata))

    def test_two_stage_failure_keeps_first_call_usage_and_never_retries(self) -> None:
        for second in ("invalid_shape", "timeout"):
            with self.subTest(second=second):
                self.service.executor = self.executor(AlteredTransport(self.reporting, "large_tables"))

                class TwoStageRuntime(StaticRuntime):
                    def complete(self, **kwargs: Any) -> dict[str, Any]:
                        if not self.contexts:
                            self.output = selection("table-index:large_a")
                        elif second == "timeout":
                            self.error = AgentError("llm_timeout", "合成第二轮超时", http_status=504, retryable=True)
                        else:
                            self.output = {**selection("table:large_a:0"), "private-extra-key": "private final"}
                        return super().complete(**kwargs)

                self.use_runtime(TwoStageRuntime())
                run, message = self.submit("读取较大表格的第一行")
                self.assert_failed(run, message, "llm_timeout" if second == "timeout" else "llm_contract_invalid")
                metadata = message["metadata"]
                self.assertEqual(metadata["model_calls"], 2)
                self.assertEqual(metadata["usage"]["total_tokens"], 3 if second == "timeout" else 6)
                self.assertEqual(metadata["model"], "synthetic-fact-selector")
                calls = metadata["selection_diagnostics"]["calls"]
                self.assertEqual([call["call_index"] for call in calls], [1, 2])
                self.assertEqual(calls[0]["status"], "valid")
                self.assertEqual(calls[0]["usage"], {"total_tokens": 3})
                self.assertEqual(calls[1]["status"], "failed" if second == "timeout" else "invalid")
                self.assertEqual(len(self.runtime.contexts), 2)
                self.assertNotIn("private-extra-key", json.dumps(metadata))
                self.assertNotIn("private final", json.dumps(metadata))
                first, final = [c["response_contract"]["schema"] for c in self.runtime.contexts]
                self.assertIn("table-index:large_a", first["properties"]["fact_ids"]["items"]["enum"])
                self.assertIn("table:large_a:0", final["properties"]["fact_ids"]["items"]["enum"])
                self.assertNotIn("table-index:large_a", final["properties"]["fact_ids"]["items"]["enum"])
                self.assertEqual(final["properties"]["evidence_group_ids"]["maxItems"], 0)

    def test_two_stage_success_keeps_contract_clean_and_aggregates_usage_once(self) -> None:
        self.service.executor = self.executor(AlteredTransport(self.reporting, "large_tables"))

        class TwoStageRuntime(StaticRuntime):
            def complete(self, **kwargs: Any) -> dict[str, Any]:
                self.output = selection("table-index:large_a" if not self.contexts else "table:large_a:0")
                response = super().complete(**kwargs)
                response["usage"]["completion_tokens_details"] = {"reasoning_tokens": 2}
                return response

        self.use_runtime(TwoStageRuntime())
        run, message = self.submit("读取较大表格的第一行")
        self.assertEqual(run["status"], "completed", message["metadata"])
        metadata = message["metadata"]
        self.assertEqual(metadata["model_calls"], 2)
        self.assertEqual(metadata["usage"], {"total_tokens": 6,
                                           "completion_tokens_details": {"reasoning_tokens": 4}})
        self.assertNotIn("selection_diagnostics", metadata)
        self.assertTrue(self.qa.answer_validator.is_valid(metadata))

    def test_history_is_reused_within_selected_report_but_not_across_report_switch(self) -> None:
        other_id = "rpt_wenquansecond1234567890"
        other = fixture_report()
        other.update(report_id=other_id, data_snapshot_id="snap_wenquansecond1234567890", title="第二份合成报告")
        other["scope"] = {"category_id": "pressure_washer", "brands": ["SECOND"]}
        other["metrics"][0].update(value=0.25, numerator=5)
        self.reporting.store.store_report(other)
        self.reporting.evidence.store(other_id, [], {})
        self.actor = replace(self.actor, allowed_report_ids=frozenset({REPORT_ID, other_id}))
        self.submit("第一份报告独有标记")
        run, _ = self.submit("第二份报告独有标记", report_id=other_id)
        self.assertEqual(run["status"], "completed")
        second_context = self.runtime.contexts[-1]
        self.assertEqual(second_context["previous_turns"], [])
        self.assertEqual(second_context["scope"]["category_id"], "pressure_washer")
        run, message = self.submit("它的分母呢？", report_id=None)
        self.assertEqual(run["status"], "completed")
        history = json.dumps(self.runtime.contexts[-1]["previous_turns"], ensure_ascii=False)
        self.assertIn("第二份报告独有标记", history)
        self.assertNotIn("第一份报告独有标记", history)
        self.assertEqual(message["metadata"]["report_id"], other_id)

    def test_source_content_drift_fails_closed_even_if_report_identity_is_unchanged(self) -> None:
        self.use_runtime(StaticRuntime(selection("finding:pain")))
        self.service.executor = self.executor(AlteredTransport(self.reporting, "drift"))
        run, message = self.submit()
        self.assert_failed(run, message, "source_changed")

    def test_altered_quote_with_genuine_valid_citation_is_not_published(self) -> None:
        self.use_runtime(StaticRuntime(selection(groups=(GROUP_ID,))))
        transport = AlteredTransport(self.reporting, "tampered_quote")
        source = self.reporting.invoke("zhuojian.query_evidence@v1", {
            "report_id": REPORT_ID, "evidence_group_id": GROUP_ID, "limit": 3,
        }, transport.authority(self.actor))
        self.assertTrue(self.reporting.validate_citations(
            source["citations"], transport.authority(self.actor))["valid"])
        self.service.executor = self.executor(transport)
        run, message = self.submit("给出原文示例")
        self.assert_failed(run, message, "citation_invalid")
        self.assertNotIn("99.9", message["content"])

    def test_first_report_response_must_match_requested_report_before_model_runs(self) -> None:
        transport = AlteredTransport(self.reporting, "wrong_report")
        self.service.executor = self.executor(transport)
        run, message = self.submit()
        self.assert_failed(run, message, "upstream_contract_invalid")
        self.assertEqual(self.runtime.contexts, [])
        self.assertEqual(transport.calls, ["zhuojian.get_report@v1"])

    def test_valid_citation_to_different_metric_cannot_support_selected_metric(self) -> None:
        transport = AlteredTransport(self.reporting, "wrong_metric_citation")
        alternate = self.reporting.invoke("zhuojian.get_metric@v1", {
            "report_id": REPORT_ID, "metric_id": "secondary_rate",
        }, transport.authority(self.actor))
        self.assertTrue(self.reporting.validate_citations(
            alternate["citations"], transport.authority(self.actor))["valid"])
        self.service.executor = self.executor(transport)
        run, message = self.submit()
        self.assert_failed(run, message, "citation_invalid")

    def test_empty_citations_cannot_pass_vacuous_upstream_validation(self) -> None:
        self.service.executor = self.executor(AlteredTransport(self.reporting, "empty_citations"))
        run, message = self.submit()
        self.assert_failed(run, message, "citation_invalid")

    def test_cancelling_model_in_flight_prevents_later_reads_and_answer_publication(self) -> None:
        runtime = StaticRuntime(block=True)
        self.use_runtime(runtime)
        accepted = self.qa.submit(self.actor, session_id=self.session_id, question="等待中的问题",
                                  report_id=REPORT_ID, request_id="cancel-test-001")
        run_id = accepted["run"]["run_id"]
        self.assertTrue(runtime.started.wait(5))
        before = list(self.transport.calls)
        self.qa.cancel(self.actor, self.session_id, run_id)
        runtime.release.set()
        self.qa.close()
        run = self.store.get_run(self.actor, run_id)
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(self.transport.calls, before)
        messages = [m for m in self.store.list_messages(self.actor, self.session_id) if m["role"] == "assistant"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["answer_status"], "cancelled")
        self.assertEqual(messages[0]["metadata"]["claims"], [])
        self.assertTrue(all(s["status"] == "skipped" for s in run["steps"][1:]))

    def test_cancel_during_table_routing_prevents_second_model_call(self) -> None:
        runtime = StaticRuntime(selection("table-index:large_a"), block=True)
        self.use_runtime(runtime)
        transport = AlteredTransport(self.reporting, "large_tables")
        self.service.executor = self.executor(transport)
        accepted = self.qa.submit(self.actor, session_id=self.session_id, question="读取较大表格的数量",
                                  report_id=REPORT_ID, request_id="cancel-table-test-001")
        run_id = accepted["run"]["run_id"]
        self.assertTrue(runtime.started.wait(5))
        self.assertIn("table-index:large_a", [f["fact_id"] for f in runtime.contexts[0]["facts"]])
        before = list(transport.calls)
        self.qa.cancel(self.actor, self.session_id, run_id)
        runtime.release.set()
        self.qa.close()
        self.assertEqual(len(runtime.contexts), 1, "取消后不得继续第二次模型请求")
        self.assertEqual(transport.calls, before)
        self.assertEqual(self.store.get_run(self.actor, run_id)["status"], "cancelled")
        messages = [m for m in self.store.list_messages(self.actor, self.session_id)
                    if m.get("run_id") == run_id and m["role"] == "assistant"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["answer_status"], "cancelled")
        self.assertEqual(messages[0]["metadata"]["claims"], [])

    def test_implicit_report_followups_preserve_previous_user_words_in_model_history(self) -> None:
        self.submit("这份报告的好评率是什么？")
        run, _ = self.submit("只想进一步查看它的分母和口径", report_id=None)
        self.assertEqual(run["status"], "completed")
        run, message = self.submit("请继续解释上面这句追问", report_id=None)
        self.assertEqual(run["status"], "completed")
        history = self.runtime.contexts[-1]["previous_turns"]
        self.assertTrue(any(m["role"] == "user" and m["text"] == "只想进一步查看它的分母和口径"
                            for m in history), history)
        self.assertEqual(message["metadata"]["report_id"], REPORT_ID)

    def test_request_id_is_idempotent_and_conflicting_reuse_is_rejected(self) -> None:
        runtime = StaticRuntime(block=True)
        self.use_runtime(runtime)
        payload = dict(session_id=self.session_id, question="相同问题", report_id=REPORT_ID,
                       request_id="idempotent-test-001")
        first = self.qa.submit(self.actor, **payload)
        self.assertTrue(runtime.started.wait(5))
        repeated = self.qa.submit(self.actor, **payload)
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(first["run"]["run_id"], repeated["run"]["run_id"])
        with self.assertRaises(AgentError) as conflict:
            self.qa.submit(self.actor, **{**payload, "question": "不同问题"})
        self.assertEqual(conflict.exception.code, "request_conflict")
        runtime.release.set()
        self.wait(first["run"]["run_id"])
        repeated_after = self.qa.submit(self.actor, **payload)
        self.assertTrue(repeated_after["duplicate"])
        self.assertEqual(len(runtime.contexts), 1)
        self.assertEqual(len(self.store.list_messages(self.actor, self.session_id)), 2)

    def test_restart_recovery_only_terminalizes_new_readonly_runs_once(self) -> None:
        old = self.store.create_run(self.actor, self.session_id, intent="legacy_semantic_planner",
                                    plan=[{"title": "旧计划"}], status="running")
        pending, _ = self.qa.turns.accept(self.actor, self.session_id, question="重启前问题",
                                          report_id=REPORT_ID, request_id="restart-test-001", plan=PLAN)
        restarted = ReadonlyQA(self.service, recover=True)
        self.instances.append(restarted)
        self.assertEqual(self.store.get_run(self.actor, old["run_id"])["status"], "running")
        self.assertEqual(self.store.get_run(self.actor, pending["run_id"])["status"], "failed")
        restarted.turns.recover_interrupted()
        messages = [m for m in self.store.list_messages(self.actor, self.session_id) if m["role"] == "assistant"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["metadata"]["error"]["code"], "run_interrupted")

    def test_other_owner_cannot_submit_read_or_cancel_session(self) -> None:
        run, _ = self.submit()
        stranger = identity("user-b", roles=("admin",))
        with self.assertRaises(AgentError):
            self.qa.submit(stranger, session_id=self.session_id, question="读取他人问题",
                           report_id=REPORT_ID, request_id="owner-test-001")
        with self.assertRaises(AgentError):
            self.store.get_run(stranger, run["run_id"])
        with self.assertRaises(AgentError):
            self.qa.cancel(stranger, self.session_id, run["run_id"])

    def test_report_outside_owner_allowlist_is_blocked_before_model_selection(self) -> None:
        self.actor = replace(self.actor, allowed_report_ids=frozenset())
        run, message = self.submit()
        self.assert_failed(run, message, "report_forbidden")
        self.assertEqual(self.runtime.contexts, [])

    def test_upstream_and_model_failures_remain_distinguishable(self) -> None:
        self.service.executor = self.executor(FailingZhuojianTransport(self.reporting))
        run, message = self.submit()
        self.assert_failed(run, message, "upstream_unavailable")
        self.assertEqual(self.runtime.contexts, [])
        self.service.executor = self.executor(self.transport)
        self.use_runtime(StaticRuntime(error=AgentError("llm_unavailable", "合成模型不可用", http_status=503, retryable=True)))
        run, message = self.submit()
        self.assert_failed(run, message, "llm_unavailable")
        self.assertNotIn("证据不足", message["content"])

    def test_model_access_failure_preserves_safe_next_step(self) -> None:
        next_step = "请管理员核对模型 ID、百炼服务地域及模型访问权限。"
        self.use_runtime(StaticRuntime(error=AgentError(
            "llm_model_access_denied", "当前凭据无法使用指定模型", http_status=503,
            safe_next_step=next_step)))
        run, message = self.submit()
        self.assert_failed(run, message, "llm_model_access_denied")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(message["metadata"]["error"]["safe_next_step"], next_step)
        self.assertFalse(message["metadata"]["error"]["retryable"])

    def test_evidence_quote_is_separate_from_metric_and_validated(self) -> None:
        self.use_runtime(StaticRuntime(selection(groups=(GROUP_ID,))))
        run, message = self.submit("查看相关原文")
        self.assertEqual(run["status"], "completed")
        claim = message["metadata"]["claims"][0]
        self.assertEqual(claim["claim_type"], "evidence")
        self.assertEqual(claim["source_pointer"]["evidence_group_id"], GROUP_ID)
        self.assertIn("示例，不能据此推断占比", claim["text"])
        self.assertIn("zhuojian.query_evidence@v1", self.transport.calls)
        self.assertNotIn("zhuojian.get_metric@v1", self.transport.calls)

    def test_more_originals_followup_blocks_unsupported_paging_without_repeating_quotes(self) -> None:
        self.use_runtime(StaticRuntime(selection(groups=(GROUP_ID,))))
        first, original = self.submit("查看相关原文")
        self.assertEqual(first["status"], "completed")
        quoted = original["metadata"]["claims"][0]["text"].split("\n", 1)[1]
        reads = self.transport.calls.count("zhuojian.query_evidence@v1")
        run, message = self.submit("再给几条不重复的原文", report_id=None)
        self.assert_failed(run, message, "evidence_pagination_unavailable")
        self.assertEqual(run["status"], "blocked")
        self.assertIn("当前不支持继续翻页", message["content"])
        self.assertNotIn(quoted, message["content"])
        self.assertEqual(self.transport.calls.count("zhuojian.query_evidence@v1"), reads)
        self.assertIn("打开报告", message["metadata"]["error"]["safe_next_step"])
        self.assertTrue(any(GROUP_ID in m["evidence_group_ids"]
                            for m in self.runtime.contexts[-1]["previous_turns"]))

        # The same source is still readable in a separate session.
        self.session_id = self.service.create_session(self.actor, "另一个只读会话")["session_id"]
        run, _ = self.submit("查看相关原文")
        self.assertEqual(run["status"], "completed")


if __name__ == "__main__":
    unittest.main()
