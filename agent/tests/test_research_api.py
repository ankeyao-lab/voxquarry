"""HTTP integration over real Reporting tools and synthetic frozen artifacts.

Only the model response is scripted. Business rows, paging, aggregates, source
fingerprints and authorization pass through the production implementations.
"""
from __future__ import annotations

import copy
import json
import re
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent.app import create_app
from agent.contracts import AgentError
from agent.identity import TrustedHeaderIdentityProvider
from agent.research import ReportResearchQA
from agent.service import WenquanService
from agent.store import WenquanStore
from agent.tests.helpers import SyntheticZhuojianTransport, identity, signed_headers
from agent.tool_registry import ToolRegistry
from agent.upstream import ToolExecutor
from reporting.agent_tools import AgentToolService
from reporting.source import canonical_json
from reporting.tests.test_research_data_tools import build_research_fixture


QUERY = "zhuojian.query_report_reviews@v1"
AGGREGATE = "zhuojian.aggregate_report_reviews@v1"
SECTION = "zhuojian.get_report_section@v1"


def tool(name: str, **args: object) -> dict:
    return {"type": "tool", "tool_name": name, "args": args}


def answer(text: str, sources: list[dict], *, quotations: list[str] | None = None) -> dict:
    references = []
    for index, source in enumerate(sources):
        ref = {"source_id": source["source_id"]}
        if quotations is not None:
            ref["quote"] = quotations[index]
        references.append(ref)
    return {
        "type": "answer", "status": "answered",
        "answer": text + "".join("[" + ref["source_id"] + "]" for ref in references),
        "citations": references, "limitations": [],
    }


def decoded_sources(context: dict, kind: str) -> list[tuple[dict, dict]]:
    return [(source, source["data"] if "data" in source else json.loads(source["text"]))
            for source in context["sources"] if source["kind"] == kind]


class ScriptedResearchRuntime:
    status = "ready"

    def __init__(self) -> None:
        self.contexts: list[dict] = []
        self.callback = self.count_answer
        self.failures: list[Exception] = []
        self._settings = SimpleNamespace(model="synthetic-research-api")

    @staticmethod
    def count_answer(context: dict) -> dict:
        source, data = next((source, data) for source, data in decoded_sources(context, "report")
                            if data.get("section") == "/samples")
        samples = data["content"]
        return answer(f"这份报告有 {samples['review_entities']} 条评论，其中 {samples['written_review_entities']} 条有文字。", [source])

    def complete(self, *, messages: list[dict], tools: list, response_format: dict) -> dict:
        assert tools == []
        assert response_format == {"type": "json_object"}
        context = json.loads(messages[-1]["content"])
        self.contexts.append(context)
        try:
            # Advisory review is scripted too; this suite does not evaluate LLM quality.
            if "required_tasks" in context:
                action = {"tasks": []}
            elif "retained_answer" in context:
                assert "sources" not in context and "findings" not in context
                checks = []
                for task in context["answer_tasks"]:
                    ids = [row["statement_id"] for row in context["retained_answer"]
                           if task["task_id"] in row["task_ids"] and row["text"].strip()]
                    checks.append({"task_id": task["task_id"], "status": "answered" if ids else "unanswered",
                        "statement_ids": ids, "reason": "" if ids else "保留正文中尚未回答该要求。"})
                action = {"items": [], "completion_feedback": [], "task_checks": checks}
            elif "reviews" in context and "categories" in context:
                action = {"results": [{"record_id": row["source_id"], "status": "classified", "unknown_reason": "", "labels": []}
                                      for row in context["reviews"]]}
            elif "review_units" in context:
                if context.get("review_batch"):
                    assert 1 <= len(context["review_units"]) <= 12
                    assert "findings" not in context
                key = "statement_id" if "statement_id" in context["review_units"][0] else "finding_id"
                action = {"items": [{key: row[key], "status": "pass", "feedback": ""} for row in context["review_units"]], "completion_feedback": []}
                if "answer_tasks" in context:
                    action["task_checks"] = [{"task_id": task["task_id"], "status": "answered", "reason": "",
                        "statement_ids": [row["statement_id"] for row in context["parents"]]}
                        for task in context["answer_tasks"]]
            elif "findings" in context:
                action = {"statements": [{"statement_id": f"P{index}", "text": row["statement"],
                    "task_ids": [task["task_id"] for task in context["answer_tasks"]], "evidence": row["evidence"]}
                    for index, row in enumerate(context["findings"], 1)], "unanswered": []}
            else:
                action = {"status": "pass", "feedback": []} if "answer_action" in context else self.callback(context)
                branches = context.get("response_contract", {}).get("schema", {}).get("oneOf", [])
                grounded = any(b.get("properties", {}).get("type", {}).get("const") == "findings" for b in branches)
                if grounded and action.get("type") == "answer":
                    by_id = {s["source_id"]: s for s in context["sources"]}
                    evidence = []
                    for ref in action["citations"]:
                        source = by_id[ref["source_id"]]
                        evidence.append({"source_id": ref["source_id"], **(
                            {"quote": ref.get("quote") or source["data"]["review_text"][:1000]}
                            if source["kind"] == "review" else {"pointer": ""})})
                    action = {"type": "findings", "findings": [{"finding_id": "F1", "kind": "interpretation",
                        "statement": re.sub(r"\[[^\]]+\]", "", action["answer"]), "context": "", "evidence": evidence}]}
        except Exception as error:
            self.failures.append(error)
            raise
        return {
            "model": "synthetic-research-api", "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(action, ensure_ascii=False)}}],
        }


class ResearchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.fixture = build_research_fixture(root / "reporting")
        self.report_id = self.fixture["report"]["report_id"]
        self.actor = replace(identity(), allowed_report_ids=frozenset({self.report_id}))
        self.secret = "synthetic-api-identity-only-" * 3
        self.headers = signed_headers(self.secret, self.actor)
        self.store = WenquanStore(root / "agent.sqlite3")
        self.store.initialize()
        self.reporting = AgentToolService(self.fixture["store"], self.fixture["evidence"])
        self.transport = SyntheticZhuojianTransport(self.reporting)
        registry = ToolRegistry(zhuojian_status="synthetic_test_only", enable_research=True)
        executor = ToolExecutor(
            registry=registry, transport=self.transport, store=self.store, allow_synthetic=True,
            envelope_schema_path=str(Path(__file__).resolve().parents[2] / "reporting/schemas/zhuojian-tool-envelope-v1.schema.json"),
        )
        self.runtime = ScriptedResearchRuntime()
        service = WenquanService(
            store=self.store, registry=registry, executor=executor,
            planner=SimpleNamespace(runtime=self.runtime), identity_status="synthetic_test_only",
        )
        app = create_app(service=service, identity_provider=TrustedHeaderIdentityProvider(self.secret))
        self.assertIsInstance(app.state.readonly, ReportResearchQA)
        self.client = TestClient(app)
        created = self.client.post("/wenquan/api/v1/sessions", headers=self.headers, json={"title": "冻结资料集成验证"})
        self.assertEqual(created.status_code, 201, created.text)
        self.session_id = created.json()["session"]["session_id"]
        self.session_path = f"/wenquan/api/v1/sessions/{self.session_id}"
        self.request_number = 0

    def tearDown(self) -> None:
        self.client.app.state.readonly.close()
        self.client.close()
        self.temporary.cleanup()

    def turn(self, question: str, *, explicit_report: bool = True) -> tuple[dict, dict]:
        self.request_number += 1
        payload = {"question": question, "request_id": f"research-api-{self.request_number:03}"}
        if explicit_report:
            payload["report_id"] = self.report_id
        accepted = self.client.post(self.session_path + "/turns", headers=self.headers, json=payload)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertTrue(accepted.json()["accepted"])
        run_id = accepted.json()["run"]["run_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            restored = self.client.get(self.session_path, headers=self.headers)
            self.assertEqual(restored.status_code, 200, restored.text)
            bundle = restored.json()
            run = next(item for item in bundle["runs"] if item["run_id"] == run_id)
            if run["status"] != "running":
                if self.runtime.failures:
                    raise self.runtime.failures[0]
                message = next(item for item in bundle["messages"] if item.get("run_id") == run_id and item["role"] == "assistant")
                return run, message
            time.sleep(0.01)
        self.fail(f"Synthetic HTTP turn did not finish: {run_id}")

    def source_response(self, citation: dict, *, headers: dict | None = None):
        return self.client.get(
            self.session_path + "/sources/" + citation["citation_id"],
            headers=self.headers if headers is None else headers,
        )

    def read_long_review(self) -> tuple[dict, dict]:
        original = self.fixture["facts"][0]

        def respond(context: dict) -> dict:
            reviews = decoded_sources(context, "review")
            if not reviews:
                return tool("query_report_reviews", review_ids=[original["review_occurrence_id"]], limit=1)
            source, row = reviews[0]
            self.assertEqual(row["review_text"], original["review_text"])
            self.assertTrue(row["review_text"].endswith("FULL_TEXT_SENTINEL"))
            return answer("已完整读取这条长评论，其末尾仍包含原始标记 FULL_TEXT_SENTINEL。", [source], quotations=["FULL_TEXT_SENTINEL"])

        self.runtime.callback = respond
        return self.turn("请读第一条评论的完整原文，确认末尾。")

    def test_turn_to_completed_then_authenticated_source_preserves_full_long_body(self) -> None:
        run, message = self.read_long_review()
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual(message["answer_status"], "answered")
        self.assertIn("FULL_TEXT_SENTINEL。 [1]", message["content"])
        metadata = message["metadata"]
        self.assertEqual(metadata["contract"], "voxquarry-wenquan-research-answer/v1")
        self.assertEqual(metadata["report_id"], self.report_id)
        self.assertEqual(metadata["research"]["review_records_read"], 1)
        self.assertEqual(metadata["model_calls"], 6)
        batch = next(context for context in self.runtime.contexts if context.get("review_batch"))
        self.assertTrue(batch["review_units"])
        self.assertEqual(decoded_sources(batch, "review")[0][1]["review_text"], self.fixture["facts"][0]["review_text"])
        coverage = next(context for context in self.runtime.contexts if "retained_answer" in context)
        self.assertEqual(coverage["retained_answer"], [{key: row[key] for key in ("statement_id", "text", "task_ids")}
            for row in metadata["grounded_statements"]["statements"]])
        self.assertEqual(metadata["question_coverage"][0]["status"], "answered")
        citation = metadata["citations"][0]
        self.assertTrue(citation["href"].startswith(self.session_path + "/sources/"))
        opened = self.client.get(citation["href"], headers=self.headers)
        self.assertEqual(opened.status_code, 200, opened.text)
        actual, original = opened.json()["review"], self.fixture["facts"][0]
        self.assertEqual(actual["review_text"], original["review_text"])
        self.assertGreater(len(actual["review_text"]), 20_000)
        self.assertTrue(actual["review_text"].endswith("FULL_TEXT_SENTINEL"))
        self.assertEqual(actual["record_id"], original["review_occurrence_id"])
        self.assertEqual(actual["source_artifact_sha256"], original["source_artifact_sha256"])
        self.assertNotIn("complete detail", json.dumps(metadata))
        self.assertGreaterEqual(self.transport.calls.count(QUERY), 3)  # read, prepublish validation, source GET

    def test_next_cursor_reaches_tail_and_last_physical_record_without_repeating_entities(self) -> None:
        pages: list[dict] = []

        def respond(context: dict) -> dict:
            reads = [item for item in context["observations"] if item.get("tool_name") == "query_report_reviews"]
            if not reads:
                return tool("query_report_reviews", limit=100)
            latest = reads[-1]
            page_source, page_data = next((source, data) for source, data in decoded_sources(context, "analysis")
                                          if source["source_id"] in latest["source_ids"])
            del page_source
            pages.append(copy.deepcopy(page_data["page"]))
            if latest["page"]["has_more"]:
                self.assertTrue(page_data["next_cursor"])
                return tool("query_report_reviews", limit=100, cursor=page_data["next_cursor"])
            reviews = decoded_sources(context, "review")
            self.assertEqual(len(reviews), 124)
            self.assertEqual(len({row["record_id"] for _, row in reviews}), 124)
            tail = next(source for source, row in reviews if row["record_id"] == f"occ_{122:024x}")
            last = next(source for source, row in reviews if row["record_id"] == f"occ_{123:024x}")
            return answer("续页已读到包含 Zephyrneedle 的末段评论及最后一条 ordinary review 123。", [tail, last],
                          quotations=["tailonly Zephyrneedle battery quiet", "ordinary review 123"])

        self.runtime.callback = respond
        run, message = self.turn("继续翻页读到最后一条，确认末段原文。")
        self.assertEqual(run["status"], "completed", message)
        self.assertEqual([page["returned"] for page in pages], [100, 24])
        self.assertEqual([page["has_more"] for page in pages], [True, False])
        self.assertEqual(message["metadata"]["research"]["review_records_read"], 124)
        final_ref = next(c for c in message["metadata"]["citations"] if c["record_id"] == f"occ_{123:024x}")
        self.assertIn("cursor", final_ref["source_request"]["payload"])
        self.assertNotIn("offset", final_ref["source_request"]["payload"])
        opened = self.source_response(final_ref)
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(opened.json()["review"]["review_text"], "ordinary review 123")

    def test_topic_change_aggregates_real_frozen_intersection_and_binds_numbers_to_source(self) -> None:
        first_run, first_message = self.turn("这份报告有多少评论？")
        self.assertEqual(first_run["status"], "completed", first_message)
        boundary = len(self.runtime.contexts)

        def respond(context: dict) -> dict:
            aggregates = [(source, data) for source, data in decoded_sources(context, "analysis") if "intersection_count" in data]
            if not aggregates:
                self.assertIn("这份报告有多少评论？", str(context["previous_turns"]))
                self.assertFalse(any(source["kind"] == "review" for source in context["sources"]))
                return tool("aggregate_report_reviews", term_groups=[
                    {"label": "battery", "terms": ["battery"]}, {"label": "quiet", "terms": ["quiet", "silent"]},
                ], group_by="channel")
            source, data = aggregates[-1]
            counts = {item["label"]: item["count"] for item in data["group_match_counts"]}
            groups = {item["key"]: item["count"] for item in data["groups"]}
            return answer(
                f"换到电池与安静体验的关键词交叉：battery 命中 {counts['battery']} 条，quiet/silent 命中 {counts['quiet']} 条，"
                f"交集 {data['intersection_count']} 条；其中 lowes {groups['lowes']} 条、ego {groups['ego']} 条。"
                "这是全文关键词命中统计，不能直接称作投诉或表扬次数。", [source],
            )

        self.runtime.callback = respond
        run, message = self.turn("换个话题，battery 和 quiet/silent 的评论有多少交集？按渠道拆分。", explicit_report=False)
        self.assertEqual(run["status"], "completed", message)
        self.assertIn("battery 命中 4 条", message["content"])
        self.assertIn("quiet/silent 命中 4 条", message["content"])
        self.assertIn("交集 3 条", message["content"])
        self.assertIn("lowes 2 条、ego 1 条", message["content"])
        citation = message["metadata"]["citations"][0]
        self.assertEqual(citation["source_request"]["tool"], "aggregate_report_reviews")
        opened = self.source_response(citation)
        self.assertEqual(opened.status_code, 200, opened.text)
        data = opened.json()["data"]
        self.assertEqual(data["intersection_count"], 3)
        self.assertEqual(data["summary"]["count"], 3)
        self.assertTrue(data["scan_complete"])
        self.assertEqual(data["provenance"]["snapshot_id"], self.fixture["report"]["data_snapshot_id"])
        self.assertEqual(data["provenance"]["fact_file_sha256"], self.fixture["manifest"]["hashes"]["preview_fact"])
        self.assertEqual(len(self.runtime.contexts[boundary:]), 2)

    def test_source_and_turns_require_authenticated_session_owner_and_current_report_permission(self) -> None:
        run, message = self.read_long_review()
        self.assertEqual(run["status"], "completed", message)
        citation = message["metadata"]["citations"][0]
        other = replace(identity("user-b"), allowed_report_ids=frozenset({self.report_id}))
        other_scope = replace(self.actor, scope_id="scope-other")
        baseline = (len(self.runtime.contexts), len(self.transport.calls))
        for headers, expected in (({}, 401), (signed_headers(self.secret, other), 404),
                                  (signed_headers(self.secret, other_scope), 404)):
            with self.subTest(expected=expected, authenticated=bool(headers)):
                source = self.source_response(citation, headers=headers)
                self.assertEqual(source.status_code, expected, source.text)
                submitted = self.client.post(self.session_path + "/turns", headers=headers, json={
                    "question": "读取别人的评论", "report_id": self.report_id, "request_id": "foreign-attempt-001",
                })
                self.assertEqual(submitted.status_code, expected, submitted.text)
                self.assertNotIn("FULL_TEXT_SENTINEL", source.text)
        self.assertEqual((len(self.runtime.contexts), len(self.transport.calls)), baseline)
        revoked = signed_headers(self.secret, replace(self.actor, allowed_report_ids=frozenset()))
        source = self.source_response(citation, headers=revoked)
        self.assertEqual(source.status_code, 403, source.text)
        self.assertNotIn("FULL_TEXT_SENTINEL", source.text)

    def test_source_open_rejects_report_metadata_drift_after_answer_completed(self) -> None:
        run, message = self.read_long_review()
        self.assertEqual(run["status"], "completed", message)
        citation = message["metadata"]["citations"][0]
        self.assertEqual(self.source_response(citation).status_code, 200)
        changed = copy.deepcopy(self.fixture["report"])
        changed["generated_at"] = "2026-09-10T00:00:00+00:00"
        path = self.fixture["store"].reports_dir / f"{self.report_id}.json"
        path.write_text(canonical_json(changed), encoding="utf-8")
        denied = self.source_response(citation)
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "source_changed")
        self.assertNotIn("FULL_TEXT_SENTINEL", denied.text)
        self.assertEqual(message["metadata"]["report"]["generated_at"], self.fixture["report"]["generated_at"])

    def test_missing_frozen_reviews_still_allows_saved_counts_but_raw_request_fails_truthfully(self) -> None:
        self.fixture["fact_path"].unlink()
        run, message = self.turn("这份报告有多少评论？")
        self.assertEqual(run["status"], "completed", message)
        self.assertIn("124 条评论，其中 123 条有文字", message["content"])
        self.assertEqual(set(self.transport.calls), {SECTION})
        self.assertEqual(message["metadata"]["model_calls"], 1)
        self.runtime.callback = lambda context: tool("query_report_reviews", limit=1)
        raw_run, failure = self.turn("再读相关评论的完整原文。", explicit_report=False)
        self.assertIn(raw_run["status"], {"blocked", "failed"}, failure)
        self.assertEqual(failure["metadata"]["error"]["code"], "research_source_unavailable")
        self.assertEqual(failure["metadata"]["citations"], [])
        self.assertNotIn("contract", failure["metadata"])
        self.assertEqual(failure["metadata"]["model_calls"], 2)

    def test_semantic_analysis_resumes_remaining_pages_and_keeps_old_citation_fixed(self):
        def respond(context):
            analysis = next((source for source in context["sources"]
                if source.get("data", {}).get("contract") == "wenquan-semantic-summary/v1"), None)
            if analysis is None:
                return tool("classify_report_reviews", query={}, categories=[{"category_id": "fault", "definition": "Actual current product faults"}])
            return answer(f"已逐条分类{analysis['data']['processed_records']}条评论。", [analysis])
        self.runtime.callback = respond
        with patch("agent.research.MAX_MODEL_CALLS", 12):
            run, partial = self.turn("逐条分类全部有文字评论，统计实际投诉数量。")
        self.assertEqual(run["status"], "blocked", partial)
        self.assertTrue(partial["metadata"]["can_resume"])
        job = partial["metadata"]["semantic_jobs"][0]
        self.assertEqual(job["processed"], 120)
        original_citation = partial["metadata"]["citations"][0]
        old_source = self.source_response(original_citation).json()
        self.assertEqual(old_source["data"]["processed_records"], 120)
        self.assertEqual(len(old_source["data"]["items"]), 24)
        original_call = self.transport.call
        def interrupted_read(name, *args, **kwargs):
            if name == QUERY:
                raise AgentError("upstream_timeout", "暂时无法读取原文。", http_status=503, retryable=True)
            return original_call(name, *args, **kwargs)
        with patch.object(self.transport, "call", side_effect=interrupted_read):
            failed_restore = self.client.post(self.session_path + f"/runs/{run['run_id']}/resume", headers=self.headers,
                                             json={"request_id": "resume-disconnected-001"})
            failed_id = failed_restore.json()["run"]["run_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                bundle = self.client.get(self.session_path, headers=self.headers).json()
                if next(row for row in bundle["runs"] if row["run_id"] == failed_id)["status"] != "running":
                    break
                time.sleep(0.01)
        failed_message = next(row for row in bundle["messages"] if row.get("run_id") == failed_id and row["role"] == "assistant")
        self.assertTrue(failed_message["metadata"]["can_resume"])
        saved = self.client.app.state.readonly.turns.draft(self.actor, failed_id)
        self.assertEqual(saved["action"]["semantic_jobs"][0]["processed"], 120)
        resumed = self.client.post(self.session_path + f"/runs/{failed_id}/resume", headers=self.headers,
                                   json={"request_id": "resume-batch-001"})
        self.assertEqual(resumed.status_code, 202, resumed.text)
        rid = resumed.json()["run"]["run_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            bundle = self.client.get(self.session_path, headers=self.headers).json()
            completed = next(row for row in bundle["runs"] if row["run_id"] == rid)
            if completed["status"] != "running":
                break
            time.sleep(0.01)
        result = next(row for row in bundle["messages"] if row.get("run_id") == rid and row["role"] == "assistant")
        self.assertEqual(completed["status"], "completed", result)
        self.assertEqual(result["metadata"]["semantic_jobs"][0]["processed"], 123)
        self.assertEqual(result["metadata"]["model_calls"], 5)
        self.assertEqual(self.source_response(original_citation).json(), old_source)
        source = self.source_response(result["metadata"]["citations"][0]).json()
        self.assertTrue(source["data"]["complete"])
        self.assertEqual(source["data"]["categories"][0]["actual_negative_count"], 0)
        tail = self.client.get(self.session_path + "/sources/" + result["metadata"]["citations"][0]["citation_id"] + "?offset=120", headers=self.headers).json()
        self.assertEqual(len(tail["data"]["items"]), 3)
        self.assertEqual(tail["data"]["items"][-1]["review_text"], "ordinary review 123")


if __name__ == "__main__":
    unittest.main()
