"""Lifecycle integration with real private stores and a scripted Harness manager.

No provider calls run here. These tests prove publication, isolation and recovery,
not whether a model's interpretation of a review is correct.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent.agent_trace import AgentTraceStore
from agent.contracts import AgentError
from agent.harness_research import HarnessResearch, tool_progress_summary
from agent.model_registry import ModelRegistry
from agent.research_attachments import AttachmentStore
from agent.research_state import ResearchState
from agent.service import WenquanService
from agent.store import WenquanStore
from agent.tests.test_agent_trace import actor
from agent.tool_registry import ToolRegistry


DATASET_ID = "ds_" + "a" * 64
REVIEW_TEXT = "On medium blade speed, I cut the small lawn in 25 minutes."


class RecordingPool:
    def __init__(self):
        self.calls = []

    def submit(self, function, *args, **kwargs):
        self.calls.append((function, args, kwargs))
        return Future()

    def shutdown(self, **kwargs):
        pass


class SyntheticData:
    def __init__(self):
        self.calls, self.failures = [], []

    def call(self, actor, operation, args):
        self.calls.append((actor, operation, copy.deepcopy(args)))
        if self.failures:
            raise self.failures.pop(0)
        if operation == "catalog":
            return {"items": [{"job_id": "sample-job", "brand": "SYNTHETIC", "model": "LM-test", "eligible_reviews": 1}], "total": 1}
        if operation == 'prepare':
            binding = {'catalog_id': 'cat_fixture', 'scope': args.get('scope', {}), 'origin': 'snapshot',
                'total': 1, 'total_sources': 1, 'unavailable_count': 0, 'batch_count': 1}
            return {'status': 'ready', 'catalog_id': binding['catalog_id'], 'scope': binding['scope'], 'origin': 'snapshot',
                'catalog_binding': binding, 'datasets': [{'dataset_id': DATASET_ID, 'manifest_sha256': 'sha256:' + 'b' * 64,
                    'batch_id': 'batch0', 'source_coverage': {'complete': True}}],
                'coverage': {'requested_sources': 1, 'available_sources': 1, 'prepared_sources': 1,
                    'unavailable_count': 0, 'total_batches': 1, 'prepared_batches': 1, 'complete': True},
                'pending_batch_ids': [], 'failed_batches': [], 'next': None}
        if operation in {"manifest", "open"}:
            return {"dataset_id": DATASET_ID, "manifest_sha256": "sha256:" + "b" * 64, "total": 1, "inputs": [{"job": {"id": "sample-job"}}]}
        if operation == "query":
            return {"contract": "voxquarry-research-data-query/v1", "dataset_id": DATASET_ID,
                "manifest_sha256": "sha256:" + "b" * 64, "population": "entities", "total": 1,
                "page": {"offset": 0, "returned": 1, "has_more": False}, "next_cursor": None,
                "items": [{"record_id": "record-a", "review_entity_id": "entity-a", "review_text": REVIEW_TEXT,
                           "review_title": "Medium speed on a small lawn", "rating": 5}]}
        raise AssertionError(operation)


class SyntheticCollection:
    def __init__(self):
        self.calls, self.ready = [], False

    def call(self, operation, args=None):
        self.calls.append((operation, copy.deepcopy(args)))
        if operation in {"ensure", "status"}:
            return {"status": "completed" if self.ready else "running", "attempt_no": 1,
                "data": {"ready": self.ready, "job_id": "sample-job"},
                "selected_run": {"internal_file_path": "/private/server.sqlite"}, "artifacts": ["internal"]}
        if operation == "unsubscribe":
            return {"unsubscribed": True}
        raise AssertionError(operation)


class SyntheticManager:
    def __init__(self):
        self.starts, self.stops = [], []
        self.inputs = []
        self.on_start, self.on_status, self.on_stop = None, None, None
        self.outcome = {"status": "completed", "cursor": 1, "events": [], "result": {"answer": "研究完成。", "finish_reason": "completed"}}

    def start(self, payload):
        self.starts.append(copy.deepcopy(payload))
        if self.on_start:
            self.on_start(payload)
        return {"status": "running"}

    def status(self, attempt_id, after=0):
        if self.on_status:
            self.on_status(attempt_id)
        return copy.deepcopy(self.outcome)

    def stop(self, attempt_id):
        self.stops.append(attempt_id)
        if self.on_stop:
            self.on_stop(attempt_id)
        return {"status": "stopped"}

    def put_input(self, attempt_id, filename, content):
        self.inputs.append((attempt_id, filename, content))
        return {"analysis_path": "/inputs/" + filename, "bytes": len(content)}


class HarnessFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WenquanStore(root / "wenquan.sqlite3")
        self.store.initialize()
        self.models = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic-never-transmitted"})
        self.state = ResearchState(self.store, self.models)
        self.traces = AgentTraceStore(root / "traces" / "traces.sqlite3", content_enabled=True)
        self.attachments = AttachmentStore(self.store, root / "attachments")
        self.service = WenquanService(store=self.store, registry=ToolRegistry(), executor=None)
        self.manager, self.data, self.collection = SyntheticManager(), SyntheticData(), SyntheticCollection()
        self.research = HarnessResearch(self.service, self.manager, models=self.models, state=self.state,
            traces=self.traces, attachments=self.attachments, data=self.data, collection=self.collection,
            web=SimpleNamespace(search=Mock(side_effect=AssertionError("No web in synthetic tests"))))
        # Keep the real monitor out of deterministic tests; individual tests tick
        # its production function explicitly rather than waiting 15 seconds.
        self.research._stopped.set()
        self.research._monitor.join(timeout=1)
        self.research._stopped.clear()
        self.research._pool.shutdown(wait=True)
        self.pool = self.research._pool = RecordingPool()
        self.user = actor(roles=("user",))
        self.sid = self.store.create_session(self.user, "独立研究测试")["session_id"]

    def tearDown(self):
        self.research.close()
        self.temp.cleanup()

    def accept(self, **overrides):
        payload = {"question": "这条评论具体说了什么？", "request_id": "request-1", **overrides}
        request, frozen = self.research.validate_request(self.user, self.sid, payload)
        return self.state.accept(self.user, self.sid, request, frozen)["run"]

    def cap(self, payload):
        return self.research.broker.resolve("Bearer " + payload["capability"])

    def read_and_answer(self, payload):
        cap = self.cap(payload)
        result = self.research.tool(cap, "query_dataset", {"dataset_id": DATASET_ID, "limit": 1})
        sid = result["items"][0]["source_id"]
        accepted = self.research.tool(cap, "submit_answer", {"answer": "用户用中档刀速割小草坪，耗时 25 分钟。[" + sid + "]"})
        self.assertTrue(accepted["accepted"])
        self.last_source_id = sid

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(AgentError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)


class HarnessResearchTests(HarnessFixture, unittest.TestCase):
    def test_batch_plan_has_no_fake_dataset_and_rejection_is_bound_to_version(self):
        from agent.tests.test_research_data_goal import catalog_page
        from agent.research_delivery import delivery_issues
        run, cap = self.live_cap()
        plan = {**catalog_page(), 'status': 'requires_batches'}
        cap.runtime_contract = 'wenquan-dsh/v1'
        self.research.data.call = Mock(return_value=plan)
        result = self.research.tool(cap, 'open_dataset', {})
        self.assertEqual(result['status'], 'requires_batches')
        self.assertNotIn('source_id', result)
        args = {'catalog_id': 'cat_test', 'batch_id': 'batch0'}
        self.research.data.call = Mock(side_effect=AgentError('invalid_source', 'invalid row', retryable=False))
        self.research.tool(cap, 'open_dataset', args)
        second = self.research.tool(cap, 'open_dataset', args)
        self.assertEqual(second['error']['code'], 'invalid_source')
        self.assertEqual(self.research.data.call.call_count, 1)
        checkpoint = self.state.get(self.user, run['run_id'])['checkpoint']
        self.assertEqual(delivery_issues(checkpoint)[0]['failed_batches'], {'batch0': 'invalid_source'})
        self.research.tool(cap, 'open_dataset', {**args, 'catalog_id': 'cat_refresh'})
        self.assertEqual(self.research.data.call.call_count, 2)

    def test_catalog_cursor_does_not_inherit_unrelated_single_model_search(self):
        _, cap = self.live_cap()
        self.research._checkpoint(cap, review_link_searches={'search': {'brand': 'EGO', 'model': 'LB8803-2'}})
        self.research._data_tool(cap, 'list_datasets', {'cursor': 'frozen-cursor'})
        self.assertEqual(self.data.calls[-1][2], {'cursor': 'frozen-cursor'})

    def test_union_statistic_source_is_readable_without_a_fake_dataset_id(self):
        _, cap = self.live_cap()
        self.research.data.call = Mock(return_value={'dataset_ids': ['a', 'b'], 'total': 1,
            'sources': [{'dataset_id': 'a', 'manifest_sha256': 'v1'}, {'dataset_id': 'b', 'manifest_sha256': 'v2'}],
            'manifest_sha256': 'combined'})
        result = self.research.tool(cap, 'aggregate_dataset', {'dataset_ids': ['a', 'b']})
        saved = self.research.source(self.user, self.sid, result['source_id'], offset=0)
        self.assertEqual(saved['source_type'], 'analysis')
        self.assertNotIn('dataset_id', saved)
        self.assertEqual(saved['provenance']['sources'][1]['manifest_sha256'], 'v2')
        self.assertEqual(self.research.data.call.call_count, 1)

    def test_discovered_single_model_catalog_keeps_original_model_scope(self):
        run, cap = self.live_cap(self.accept(question='查找 EGO LB8803-2 网评并生成报告'))
        self.research._checkpoint(cap, review_link_searches={'search': {'brand': 'EGO', 'model': 'LB8803-2'}})
        self.research._data_tool(cap, 'list_datasets', {'scope': {'brands': ['EGO']}})
        self.assertEqual(self.data.calls[-1][2]['scope'], {'brands': ['EGO'], 'models': ['LB8803-2']})
        for scope in ({'job_ids': ['exact']}, {'models': ['OTHER']}, {'brands': ['OTHER']}):
            self.research._data_tool(cap, 'list_datasets', {'scope': scope})
            self.assertEqual(self.data.calls[-1][2]['scope'], scope)
        self.research._checkpoint(cap, review_link_searches={'a': {'brand': 'EGO', 'model': 'LB8803-2'}, 'b': {'brand': 'EGO', 'model': 'LB7650'}})
        self.research._data_tool(cap, 'list_datasets', {'scope': {'brands': ['EGO']}})
        self.assertEqual(self.data.calls[-1][2]['scope'], {'brands': ['EGO']})

    def test_discovered_model_report_keeps_exact_source_ids_from_read_manifests(self):
        run, cap = self.live_cap(self.accept(question='查找 EGO LB8803-2 并采集生成报告'))
        self.research._checkpoint(cap, review_link_searches={'search': {}})
        for jid, pid in [('one','5012345678'),('two','LB8803-2')]:
            self.research._source(cap, {'source_type':'dataset','provenance':{'operation':'open'},
                'content':{'inputs':[{'job':{'id':jid,'source_product_id':pid,
                    'source_product_identity':{'status':'resolved'}}}]}})
        scope = self.research._report_workflow_scope(cap, {'job_ids':['one','two']}, creating=True)
        self.assertEqual(scope['source_product_ids'], ['5012345678','LB8803-2'])
        self.assert_code('report_scope_mismatch', self.research._report_workflow_scope, cap,
            {'job_ids':['one'],'source_product_ids':['wrong']}, creating=True)
        self.assert_code('report_scope_unverified', self.research._report_workflow_scope, cap, {}, creating=True)

    def test_read_formal_report_gets_browser_link_even_when_answer_uses_internal_uri(self):
        rid = 'rpt_' + 'a' * 24
        run, cap = self.live_cap(self.accept(question='生成并读取洞察报告，给出报告链接'))
        answer = '报告：`voxquarry://zhuojian/reports/' + rid + '`'
        self.assertEqual(self.research._with_report_links(self.user, run['run_id'], answer), answer)
        self.research._source(cap, {'source_type': 'report', 'report_id': rid, 'title': '测试报告', 'content': {}})
        linked = self.research._with_report_links(self.user, run['run_id'], answer)
        self.assertIn('](/?report=' + rid + '#insights)', linked)
        self.assertEqual(self.research._with_report_links(self.user, run['run_id'], linked), linked)

    def test_explicit_reuse_accepts_hash_verified_exact_dataset_without_new_collection(self):
        url = 'https://www.lowes.com/pd/Fixture/5012345678'
        run, cap = self.live_cap(self.accept(question='查找 EGO LB8803-2 并采集，已有可用数据就复用'))
        self.research._checkpoint(cap, review_link_searches={'search': {}}, read_job_ids=['target'],
            review_link_candidates={url: {'channel': 'lowes', 'auto_collectable': True, 'identity_status': 'resolved_match'}})
        manifest = {'contract': 'voxquarry.wenquan-dataset/v1', 'manifest_sha256': 'sha256:' + 'b' * 64,
            'export_complete': True, 'scope': {'source_product_ids': ['5012345678']},
            'inputs': [{'status': 'completed', 'job': {'id': 'target', 'target_url': url, 'source_product_id': '5012345678'}}]}
        source = {'source_type': 'dataset', 'content': manifest, 'provenance': {'operation': 'open', 'manifest_sha256': manifest['manifest_sha256']}}
        # Broad catalog entries and unread or mismatched datasets do not prove reuse.
        self.assert_code('collection_goal_incomplete', self.research._verify_collection_goal, self.user, run['run_id'])
        source['content']['inputs'][0]['job']['target_url'] = 'https://www.lowes.com/pd/Other/5099999999'
        self.research._source(cap, source)
        self.assert_code('collection_goal_incomplete', self.research._verify_collection_goal, self.user, run['run_id'])
        source['content']['inputs'][0]['job']['target_url'] = url
        self.research._source(cap, source)
        self.research._verify_collection_goal(self.user, run['run_id'])
        self.assertEqual(self.collection.calls, [])

    def test_resume_preserves_precise_repair_without_inventing_a_collection_workflow(self):
        run, cap = self.live_cap(self.accept(question='查找 EGO LB8803-2 网评链接并采集'))
        self.research._checkpoint(cap, review_link_searches={'old': {'items': []}},
            answer_feedback={'code': 'citation_missing', 'message': '旧引用反馈'})
        context = self.research._resume_context(self.user, run['run_id'])
        self.assertNotIn('functional_goal_feedback', context)
        self.assertEqual(context['answer_feedback']['code'], 'citation_missing')
        self.assertEqual(context['pending_deliverables'], [])
        self.assertIn('只修正', context['continuation_instruction'])

    def test_accepted_answer_clears_stale_repair_feedback(self):
        run, cap = self.live_cap()
        self.research._checkpoint(cap, answer_feedback={'code': 'citation_missing', 'message': '旧引用反馈'})
        self.research.tool(cap, 'submit_answer', {'answer': '暂无可确认结论。'})
        self.assertIsNone(self.state.get(self.user, run['run_id'])['checkpoint']['answer_feedback'])

    def test_report_data_wait_preserves_collection_receipts_and_resumes_once(self):
        run, cap = self.live_cap()
        self._set_report_wait_running(run, cap)
        workflow = Mock(return_value={'items': [{'candidate_id': 'rc_fixture'}]})
        self.service.executor = SimpleNamespace(transport=SimpleNamespace(report_workflow=workflow))
        result = self.research.tool(cap, 'wait_for_report_data', {'job_ids': ['sample-job'], 'research_note': '等待洞察候选'})
        self.assertEqual(result['status'], 'waiting_data')
        self.assertEqual(self.state.get(self.user, run['run_id'])['status'], 'waiting_data')
        with patch.object(self.research, '_stopped', SimpleNamespace(wait=Mock(side_effect=[False, True]), is_set=lambda: False)):
            self.research._monitor_waiting()
        current = self.state.get(self.user, run['run_id'])
        self.assertEqual(current['status'], 'running')
        self.assertEqual(current['checkpoint']['collection_outcomes'], [{'request_key': 'op', 'data': {'ready': True}}])
        self.assertIsNone(current['checkpoint']['report_scope_wait'])
        self.assertEqual(len(self.pool.calls), 1)

    def _set_report_wait_running(self, run, cap):
        self.state.checkpoint(self.user, run['run_id'], {'read_job_ids': ['sample-job'],
            'collection_outcomes': [{'request_key': 'op', 'data': {'ready': True}}]}, status='running', attempt_id=cap.attempt_id)

    def test_deterministic_service_failure_allows_model_to_deliver_available_evidence(self):
        run, cap = self.live_cap()
        self.data.failures = [AgentError('dataset_storage_unavailable', '工作目录不可用', http_status=503)]
        result = self.research.tool(cap, 'list_datasets', {})
        self.assertEqual(result['error']['code'], 'dataset_storage_unavailable')
        self.assertEqual(self.state.get(self.user, run['run_id'])['status'], 'running')
        self.assertIn('不要重复', result['error']['recovery_hint'])
        self.assertEqual(len(self.data.calls), 1)

    def test_explicit_collection_cannot_finish_using_an_unrelated_answer(self):
        run = self.accept(question='采集并分析 https://www.lowes.com/pd/Fixture/5012345678')
        run, cap = self.live_cap(run)
        with self.assertRaises(AgentError) as caught:
            self.research._verify_collection_goal(self.user, run['run_id'])
        self.assertEqual(caught.exception.code, 'collection_goal_incomplete')
        self.state.checkpoint(self.user, run['run_id'], {'operations': {'one': {
            'product_url': 'https://www.lowes.com/pd/Fixture/5012345678',
            'result': {'job_id': 'target', 'data': {'ready': True}}}}, 'read_job_ids': ['unrelated']}, status='running', attempt_id=run['attempt_id'])
        with self.assertRaises(AgentError):
            self.research._verify_collection_goal(self.user, run['run_id'])
        self.research._checkpoint(cap, read_job_ids=['target'])
        self.research._verify_collection_goal(self.user, run['run_id'])

    def test_wrong_percentage_relation_is_repairable_before_answer_or_report_save(self):
        run, cap = self.live_cap()
        report = {"title": "合成比例比较", "filename": "comparison", "formats": ["md"]}
        rejected = self.research.tool(cap, "submit_answer", {"answer": "A 8.2%＞B 9.3%", "report": report})
        self.assertEqual(rejected["error"]["code"], "answer_numeric_relation_conflict")
        checkpoint = self.state.get(self.user, run["run_id"])["checkpoint"]
        self.assertNotIn("answer_draft", checkpoint)
        self.assertEqual(self.store.get_run(self.user, run["run_id"])["artifacts"], [])
        accepted = self.research.tool(cap, "submit_answer", {"answer": "B 9.3%＞A 8.2%"})
        self.assertTrue(accepted["accepted"])
        self.assertEqual(self.state.get(self.user, run["run_id"])["checkpoint"]["answer_draft"]["answer"],
                         "B 9.3%＞A 8.2%")

    def test_queue_runs_one_native_attempt_and_cancelled_waiter_never_starts(self):
        entered, release = threading.Event(), threading.Event()
        self.research._pool = ThreadPoolExecutor(max_workers=1)
        self.manager.on_status = lambda _: (entered.set(), release.wait(5))
        first = self.research.submit(self.user, session_id=self.sid, question='第一项', request_id='queue-first')['run']
        self.assertTrue(entered.wait(3))
        second_sid = self.store.create_session(self.user, 'second')['session_id']
        second = self.research.submit(self.user, session_id=second_sid, question='第二项', request_id='queue-second')['run']
        self.assertEqual(second['execution_status'], 'queued')
        self.assertEqual(second['queue_position'], 1)
        self.assertEqual(len(self.manager.starts), 1)
        self.research.cancel(self.user, second_sid, second['run_id'])
        release.set()
        self.research._pool.shutdown(wait=True)
        self.assertEqual(len(self.manager.starts), 1)
        self.assertEqual(self.state.get(self.user, first['run_id'])['status'], 'completed')
        self.assertEqual(self.state.get(self.user, second['run_id'])['status'], 'cancelled')

    def live_cap(self, run=None):
        run = run or self.accept()
        self.research.egress.register_user_urls(self.user, run["run_id"])
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question=run["request"]["question"])
        return run, self.research.broker.issue(self.user, run, trace["trace_id"])[1]

    def test_valid_answer_does_not_require_a_legacy_analysis_tool_sequence(self):
        run = self.accept(question='请基于 EGO 全部原始评论数据重新分析用户群')
        self.manager.on_start = self.read_and_answer
        self.research.execute(self.user, run)
        finished = self.state.get(self.user, run['run_id'])
        self.assertEqual(finished['status'], 'completed')
        message = self.store.list_messages(self.user, self.sid)[-1]
        self.assertNotIn('全量原评分析尚未完成', message['content'])
        self.assertNotIn('data_goal', message['metadata'])
        self.assertTrue(message['metadata']['delivery']['answer_saved'])
        self.assertEqual(len(message['metadata']['citations']), 1)

    def test_whole_worker_publishes_answer_full_source_and_private_trace(self):
        run = self.accept()
        self.manager.on_start = self.read_and_answer
        self.research.execute(self.user, run)
        finished = self.state.get(self.user, run["run_id"])
        self.assertEqual(finished["status"], "completed")
        messages = self.store.list_messages(self.user, self.sid)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[-1]["content"], "用户用中档刀速割小草坪，耗时 25 分钟。[1]")
        citations = messages[-1]["metadata"]["citations"]
        self.assertEqual(citations[0]["source_id"], self.last_source_id)
        source = self.research.source(self.user, self.sid, self.last_source_id)
        self.assertEqual(source["review"]["review_text"], REVIEW_TEXT)
        self.assertEqual(source["provenance"]["manifest_sha256"], "sha256:" + "b" * 64)
        trace = self.traces.get_trace(self.user, finished["trace_id"])
        self.assertEqual(trace["final_status"], "completed")
        events = self.traces.list_events(self.user, finished["trace_id"])["items"]
        self.assertIn("tool.finished", {event["event_type"] for event in events})
        self.assertIn(run["attempt_id"], self.manager.stops)

    def test_code_preview_is_not_a_review_citation_and_can_be_repaired(self):
        run, cap = self.live_cap()
        self.manager.tool = Mock(return_value={"exit_code": 0, "stdout": "Old mower broke...", "stderr": "", "output_truncated": False})
        output = self.research.tool(cap, "run_python", {"code": "print(review_text[:600])"})
        self.assertNotIn("source_id", output)
        preview_id = output["analysis_result_id"]
        rejected = self.research.tool(cap, "submit_answer", {"answer": "这条评论说明机器有故障。[" + preview_id + "]"})
        self.assertEqual(rejected["error"]["code"], "citation_requires_original_source")
        self.assertNotIn("answer_draft", self.state.get(self.user, run["run_id"])["checkpoint"])
        original = self.research.tool(cap, "query_dataset", {"dataset_id": DATASET_ID, "limit": 1})["items"][0]
        accepted = self.research.tool(cap, "submit_answer", {"answer": "用户说中档刀速割小草坪用了25分钟。[" + original["source_id"] + "]"})
        self.assertTrue(accepted["accepted"])

    def test_legacy_execution_previews_cannot_be_promoted_by_saved_source_read(self):
        run, cap = self.live_cap()
        preview_id = self.research._source(cap, {"source_type": "artifact", "title": "分析计算结果", "content": {"stdout": "preview"}, "provenance": {"tool": "run_shell"}})
        saved = self.research.tool(cap, "read_saved_source", {"source_id": preview_id})
        self.assertFalse(saved["citation_eligible"])
        self.assertEqual(saved["analysis_result_id"], preview_id)
        self.assertNotIn("source_id", saved)
        self.assert_code("citation_requires_original_source", self.research.validate_answer, self.user, run["run_id"], "评论原话。[" + preview_id + "]")

    def test_published_calculation_artifact_still_supports_reproducible_results(self):
        run, cap = self.live_cap()
        source_id = self.research._source(cap, {"source_type": "artifact", "title": "calculation.json", "artifact_id": "artifact_fixture", "content": "{\"count\": 3}", "provenance": {"sha256": "sha256:" + "a" * 64}})
        answer, citations = self.research.validate_answer(self.user, run["run_id"], "已保存计算方法和结果。[" + source_id + "]")
        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0]["source_id"], source_id)
        self.assertEqual(answer, "已保存计算方法和结果。[1]")

    def test_saved_source_restores_full_content_for_followup_but_cannot_cross_sessions(self):
        original, cap = self.live_cap()
        result = self.research.tool(cap, "query_dataset", {"dataset_id": DATASET_ID, "limit": 1})
        source_id = result["items"][0]["source_id"]
        self.research.cancel(self.user, self.sid, original["run_id"])
        followup, fresh_cap = self.live_cap(self.accept(request_id="follow-up"))
        restored = self.research.tool(fresh_cap, "read_saved_source", {"source_id": source_id})
        self.assertEqual(restored["content"]["review_text"], REVIEW_TEXT)
        self.assertNotEqual(restored["source_id"], source_id)
        accepted = self.research.tool(fresh_cap, "submit_answer", {"answer": "这位用户描述了小草坪的具体体验。[" + restored["source_id"] + "]"})
        self.assertTrue(accepted["accepted"])
        self.research.cancel(self.user, self.sid, followup["run_id"])
        self.sid = self.store.create_session(self.user, "另一个会话")["session_id"]
        other, other_cap = self.live_cap(self.accept(request_id="separate-session"))
        denied = self.research.tool(other_cap, "read_saved_source", {"source_id": source_id})
        self.assertIn("error", denied)
        self.assertNotIn(REVIEW_TEXT, json.dumps(denied))

    def test_manager_deadline_failure_cannot_offer_an_impossible_resume(self):
        run = self.accept()
        self.manager.outcome = {"status": "failed", "error": {"code": "attempt_deadline_exceeded"}, "cursor": 1, "events": []}
        self.research.execute(self.user, run)
        finished = self.state.get(self.user, run["run_id"])
        self.assertEqual(finished["status"], "blocked")
        self.assertFalse(finished["can_resume"])
        message = self.store.list_messages(self.user, self.sid)[-1]
        self.assertEqual(message["metadata"]["error"]["code"], "research_active_budget_reached")

    def test_model_cannot_send_an_attachment_as_search_words_or_url_parameters(self):
        run, cap = self.live_cap(self.accept(product_urls=["https://www.lowes.com/pd/test/12345"], web_enabled=True))
        search = self.research.tool(cap, "search_web", {"query": "附件中的机密用户记录"})
        self.assertFalse(search["approved"])
        url = self.research.tool(cap, "read_web_page", {"url": "https://www.lowes.com/pd/test/12345?private=private-document"})
        self.assertFalse(url["approved"])
        self.research.web.search.assert_not_called()
        self.assertEqual(self.collection.calls, [])

    def test_resolve_product_sends_exact_collector_contract_without_channel(self):
        run = self.accept(question="请采集 https://www.lowes.com/pd/test/12345", web_enabled=False)
        _, cap = self.live_cap(run)
        self.collection.call = Mock(return_value={"product": {"channel": "lowes"}})
        self.research.tool(cap, "resolve_product", {"product_url": "https://www.lowes.com/pd/test/12345", "channel": "lowes"})
        self.collection.call.assert_called_once_with("resolve", {"product_url": "https://www.lowes.com/pd/test/12345"})

    def test_question_freezes_model_web_and_materials_despite_session_setting_change(self):
        run = self.accept(model_key="qwen3.8-max", effort="low", web_enabled=False, dataset_ids=[DATASET_ID])
        self.state.update_settings(self.user, self.sid, {"model_key": "glm-5.3", "effort": "max", "web_enabled": True})
        self.manager.on_start = self.read_and_answer
        self.research.execute(self.user, run)
        start = self.manager.starts[0]
        self.assertEqual(start["model_id"], "qwen3.8-max-0902")
        prompt = json.loads(start["prompt"])
        self.assertFalse(prompt["web_enabled"])
        self.assertEqual(prompt["selected_materials"]["dataset_ids"], [DATASET_ID])
        self.assertNotIn("synthetic-never-transmitted", json.dumps(start))
        self.assertEqual(self.state.get(self.user, run["run_id"])["settings"]["effective_effort"], "low")

    def test_tool_failure_is_visible_then_harness_can_read_another_page_and_finish(self):
        run = self.accept()
        self.data.failures = [AgentError("dataset_temporarily_unavailable", "暂时无法读取", retryable=True)]
        def act(payload):
            error = self.research.tool(self.cap(payload), "query_dataset", {"dataset_id": DATASET_ID})
            self.assertEqual(error["error"]["code"], "dataset_temporarily_unavailable")
            self.assertNotIn("recovery_hint", error["error"])
            self.read_and_answer(payload)
        self.manager.on_start = act
        self.research.execute(self.user, run)
        final = self.state.get(self.user, run["run_id"])
        self.assertEqual(final["status"], "completed")
        events = self.traces.list_events(self.user, final["trace_id"])["items"]
        self.assertEqual(sum(event["event_type"] == "tool.failed" for event in events), 1)
        self.assertEqual(len(self.data.calls), 2)

    def test_invalid_source_feedback_keeps_report_delivery_available(self):
        run = self.accept()
        def act(payload):
            cap = self.cap(payload)
            source = self.research.tool(cap, "read_research_reference", {"reference_id": "synthetic-study", "section_id": "home"})
            for code in ("unsafe_source", "invalid_source"):
                self.data.failures = [AgentError(code, "资料不能读取", retryable=False)]
                result = self.research.tool(cap, "open_dataset", {"scope": {"job_ids": ["sample-job"]}})
                self.assertEqual(result["error"]["code"], code)
                self.assertFalse(result["error"]["retryable"])
                self.assertIn("不代表数据不存在", result["error"]["recovery_hint"])
                self.assertIn("已读报告", result["error"]["recovery_hint"])
            accepted = self.research.tool(cap, "submit_answer", {"answer": "已读报告可用于品牌研究；本轮原始数据未能读取。[" + source["source_id"] + "]"})
            self.assertTrue(accepted["accepted"])
        self.manager.on_start = act
        self.research.execute(self.user, run)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "completed")
        self.assertEqual([call[1] for call in self.data.calls], ["prepare", "prepare"])

    def test_unknown_or_cross_run_citations_never_publish(self):
        run = self.accept()
        self.manager.outcome["result"]["answer"] = "不存在的证据。[src_made_up]"
        self.research.execute(self.user, run)
        final = self.state.get(self.user, run["run_id"])
        self.assertEqual(final["execution_status"], "recovering")
        self.assertEqual(final["checkpoint"]["answer_feedback"]["code"], "citation_unknown")
        self.assertEqual([m["role"] for m in self.store.list_messages(self.user, self.sid)], ["user"])
        self.assertNotIn("不存在的证据", self.store.list_messages(self.user, self.sid)[-1]["content"])

    def test_private_sources_and_runs_reject_other_owner_even_admin(self):
        run = self.accept()
        self.manager.on_start = self.read_and_answer
        self.research.execute(self.user, run)
        other = actor("user-b", roles=("admin",))
        for function, args in ((self.research.source, (self.sid, self.last_source_id)),
                               (self.research.cancel, (self.sid, run["run_id"])),
                               (self.research.resume, (self.sid, run["run_id"], "other-resume")),
                               (self.research.validate_request, (self.sid, {"question": "读取"}))):
            self.assert_code("session_not_found", function, other, *args)

    def test_cancel_then_resume_old_worker_cannot_publish_or_cancel_new_attempt(self):
        run = self.accept()
        self.manager.on_start = self.read_and_answer
        resumed = []
        def replace_attempt(_attempt):
            self.state.cancel(self.user, run["run_id"])
            resumed.append(self.state.resume(self.user, run["run_id"], request_id="resume1")["run"])
        self.manager.on_status = replace_attempt
        self.research.execute(self.user, run)
        current = self.state.get(self.user, run["run_id"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["attempt_id"], resumed[0]["attempt_id"])
        self.assertEqual(len(self.store.list_messages(self.user, self.sid)), 1)
        self.manager.on_status = None
        self.research.execute(self.user, current)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "completed")
        self.assertEqual(len(self.store.list_messages(self.user, self.sid)), 2)

    def test_old_attempt_draft_is_not_fallback_for_failed_new_attempt(self):
        run = self.accept()
        self.manager.on_start = self.read_and_answer
        self.manager.on_status = lambda _: self.state.cancel(self.user, run["run_id"])
        self.research.execute(self.user, run)
        resumed = self.state.resume(self.user, run["run_id"])["run"]
        self.manager.on_start = self.manager.on_status = None
        self.manager.outcome = {"status": "failed", "events": [], "result": {"answer": "不可信残留"}}
        self.recovery_used(resumed)
        self.research.execute(self.user, resumed)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "blocked")
        self.assertNotIn("25 分钟", self.store.list_messages(self.user, self.sid)[-1]["content"])
        self.assertNotIn("不可信残留", self.store.list_messages(self.user, self.sid)[-1]["content"])

    def test_cancel_trace_for_old_attempt_cannot_cancel_trace_resumed_during_manager_stop(self):
        run = self.accept()
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question=run["request"]["question"])
        self.state.attach_trace(self.user, run["run_id"], trace["trace_id"], attempt_id=run["attempt_id"])
        self.traces.append_event(self.user, trace["trace_id"], event_id="first-attempt", event_type="lifecycle.started", attempt_id=run["attempt_id"])
        resumed = []
        def resume_while_stopping(_attempt):
            newer = self.state.resume(self.user, run["run_id"], request_id="racing-resume")["run"]
            resumed.append(newer)
            self.traces.resume_trace(self.user, trace["trace_id"], attempt_id=newer["attempt_id"])
        self.manager.on_stop = resume_while_stopping
        self.research.cancel(self.user, self.sid, run["run_id"])
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "running")
        current = self.traces.get_trace(self.user, trace["trace_id"])
        self.assertEqual(current["latest_attempt_id"], resumed[0]["attempt_id"])
        self.assertEqual(current["status"], "running")
        self.assertIsNone(current["final_status"])

    def test_final_poll_rereads_current_attempt_draft_written_during_status_request(self):
        run = self.accept()
        self.manager.on_status = lambda _: self.read_and_answer(self.manager.starts[-1])
        self.manager.outcome["result"]["answer"] = "用户用中档刀速割小草坪，耗时 25 分钟。[1]"
        self.research.execute(self.user, run)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "completed")
        self.assertEqual(len(self.store.list_messages(self.user, self.sid)[-1]["metadata"]["citations"]), 1)

    def test_waiting_collection_releases_worker_and_resumes_original_question_once(self):
        run = self.accept(web_enabled=True, product_urls=["https://www.lowes.com/pd/synthetic/12345"])
        operation_ids = []
        def wait(payload):
            cap = self.cap(payload)
            collected = self.research.tool(cap, "collect_product", {"channel": "lowes", "product_url": "https://www.lowes.com/pd/synthetic/12345"})
            self.assertNotIn("selected_run", collected)
            self.assertNotIn("artifacts", collected)
            operation_ids.append(collected["operation_id"])
            result = self.research.tool(cap, "wait_for_collection", {"operation_ids": operation_ids, "research_note": "已读到一条评论，等新评论后核对续航。"})
            self.assertEqual(result["status"], "waiting_collection")
        self.manager.on_start = wait
        self.research.execute(self.user, run)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "waiting_collection")
        self.assertIn(run["attempt_id"], self.manager.stops)
        self.assertEqual(len(self.store.list_messages(self.user, self.sid)), 1)
        # An ordinary refresh/resume call must keep the pending subscription.
        self.assertTrue(self.research.resume(self.user, self.sid, run["run_id"], "no-action")["duplicate"])
        self.assertIn(run["run_id"], self.research._pending)
        self.collection.ready = True
        with patch.object(self.research, "_stopped", SimpleNamespace(wait=Mock(side_effect=[False, True]), is_set=lambda: False)):
            self.research._monitor_waiting()
        current = self.state.get(self.user, run["run_id"])
        self.assertEqual(current["status"], "running")
        self.assertNotEqual(current["attempt_id"], run["attempt_id"])
        self.assertEqual(current["request"]["question"], run["request"]["question"])
        self.assertEqual(current["checkpoint"]["waiting_operation_ids"], operation_ids)
        self.assertNotIn(run["run_id"], self.research._pending)
        self.assertEqual(len(self.pool.calls), 1)
        self.manager.on_start = self.read_and_answer
        function, args, kwargs = self.pool.calls.pop()
        function(*args, **kwargs)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "completed")
        self.assertIn("已读到一条评论", self.manager.starts[-1]["prompt"])

    def test_continue_available_is_explicit_and_duplicate_resume_keeps_current_capability(self):
        run = self.accept(web_enabled=True)
        self.state.checkpoint(self.user, run["run_id"], {"waiting_operation_ids": ["waiting-1"], "research_note": "准备等资料"},
                              status="waiting_collection", attempt_id=run["attempt_id"])
        self.research._pending[run["run_id"]] = self.user
        resumed = self.research.resume(self.user, self.sid, run["run_id"], "use-existing", action="continue_available")
        self.assertFalse(resumed["duplicate"])
        current = resumed["run"]
        self.assertEqual(current["checkpoint"]["waiting_operation_ids"], [])
        self.assertIn("现有资料", current["checkpoint"]["continuation_instruction"])
        self.assertIn("不要再次等待", current["checkpoint"]["continuation_instruction"])
        self.assertNotIn(run["run_id"], self.research._pending)
        self.assertEqual(self.collection.calls, [])

        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question=run["request"]["question"])
        token, cap = self.research.broker.issue(self.user, current, trace["trace_id"])
        duplicate = self.research.resume(self.user, self.sid, run["run_id"], "use-existing", action="continue_available")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(self.pool.calls), 1)
        self.assertIs(self.research.broker.resolve("Bearer " + token), cap)

    def test_expired_collection_wait_stops_its_subscription_without_starting_a_model(self):
        run = self.accept(web_enabled=True)
        self.state.checkpoint(self.user, run["run_id"], {"waiting_operation_ids": ["wait-expired"], "collection_deadline_at": 1},
                              status="waiting_collection", attempt_id=run["attempt_id"])
        self.research._pending[run["run_id"]] = self.user
        with patch.object(self.research, "_stopped", SimpleNamespace(wait=Mock(side_effect=[False, True]), is_set=lambda: False)):
            self.research._monitor_waiting()
        current = self.state.get(self.user, run["run_id"])
        self.assertEqual(current["status"], "blocked")
        self.assertEqual(self.pool.calls, [])
        self.assertEqual(self.collection.calls, [("unsubscribe", {"request_key": "wait-expired", "research_run_id": run["run_id"]})])
        resumed = self.research.resume(self.user, self.sid, run["run_id"], "after-expiry")
        self.assertEqual(resumed["run"]["checkpoint"]["collection_deadline_at"], 1)
        self.assertIn("不要再次等待", resumed["run"]["checkpoint"]["continuation_instruction"])

    def test_large_tool_result_remains_complete_in_analysis_file_without_flooding_context(self):
        run = self.accept()
        def receive(payload):
            cap = self.cap(payload)
            original = {"source_id": "src_synthetic", "content": [{"review_text": "完整原文" * 20000}], "page": {"has_more": False}}
            delivered = self.research._tool_delivery(cap, original)
            self.assertNotIn("content", delivered)
            self.assertIn("full_result_file", delivered)
            saved = self.manager.inputs[-1]
            self.assertEqual(json.loads(saved[2]), original)
            self.assertEqual(delivered["sha256"], "sha256:" + hashlib.sha256(saved[2]).hexdigest())
            self.assertEqual(delivered["source_id"], original["source_id"])
            self.assertLess(len(json.dumps(delivered)), 4000)
        self.manager.on_start = receive
        self.research.execute(self.user, run)

    def test_report_index_deduplicates_only_identical_descriptors_and_keeps_full_source(self):
        _, cap = self.live_cap()
        descriptors = [{"pointer": "/findings", "type": "array", "length": 3}]
        original = {"status": "ok", "report": {"title": "测试报告"}, "data": {
            "section": "index", "descriptors": copy.deepcopy(descriptors),
            "content": {"descriptors": descriptors, "top_keys": ["findings"]}}}
        before = copy.deepcopy(original)
        self.service.executor = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(result=original)))
        result = self.research.tool(cap, "get_report_section", {"report_id": "rpt_synthetic", "section": "index"})
        self.assertNotIn("descriptors", result["data"])
        self.assertEqual(result["data"]["content"]["descriptors"], descriptors)
        self.assertEqual(original, before)
        saved = self.research.source(self.user, self.sid, result["source_id"])
        self.assertEqual(saved["data"], before["data"])
        raw = json.loads(self.manager.inputs[-1][2])
        self.assertEqual(raw, {**before, "source_id": result["source_id"]})
        self.assertEqual(result["original_result_file"], "/inputs/" + self.manager.inputs[-1][1])

        different = copy.deepcopy(before)
        different["data"]["descriptors"][0]["length"] = 4
        delivered = self.research._tool_delivery(cap, different, name="get_report_section")
        self.assertEqual(delivered["data"], different["data"])
        self.assertNotIn("original_result_file", delivered)

        paragraph = {"data": {"section": "/findings/0", "content": "割草只在干燥草坪测试。" * 1000 + "湿草和坡地没有测试。",
            "descriptors": copy.deepcopy(descriptors)}}
        delivered = self.research._tool_delivery(cap, paragraph, name="get_report_section")
        self.assertEqual(json.loads(self.manager.inputs[-1][2]), paragraph)
        self.assertEqual(delivered["full_result_file"], "/inputs/" + self.manager.inputs[-1][1])
        self.assertNotIn("original_result_file", delivered)

    def test_large_reference_keeps_interpretation_boundaries_visible_with_file_delivery(self):
        _, cap = self.live_cap()
        notes = ["合成评论频次不是销量。", "场景没有原评定位时标注情境示意。"]
        for name, original in [
            ("read_research_reference", {"text": "章节原文" * 15000, "interpretation_notes": notes}),
            ("read_saved_source", {"source_type": "reference", "content": "章节原文" * 15000,
                                   "provenance": {"interpretation_notes": notes}}),
        ]:
            delivered = self.research._tool_delivery(cap, original, name=name)
            self.assertTrue(delivered["complete_content_in_file"])
            self.assertEqual(delivered["interpretation_notes"], notes)
            self.assertEqual(json.loads(self.manager.inputs[-1][2]), original)

    def test_report_query_keeps_model_scope_and_lexical_limits_with_saved_source(self):
        _, cap = self.live_cap()
        original = {"status": "ok", "report": self.known_report_metadata(),
            "data": {"base_match_count": 20, "group_match_counts": [{"label": "lighting", "count": 8}],
                     "search_semantics": "词面子串匹配", "padding": "完整原文" * 5000}}
        before = copy.deepcopy(original)
        self.service.executor = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(result=original)))
        result = self.research.tool(cap, "aggregate_report_reviews", {"report_id": "rpt_synthetic",
            "filters": {"models": ["MODEL-GROUP"], "ratings": [4, 5]},
            "term_groups": [{"label": "lighting", "terms": ["light"]}]})
        self.assertTrue(result["complete_content_in_file"])
        notes = result["interpretation_notes"]
        self.assertTrue(any("source_product_id" in note and "归并" in note for note in notes))
        self.assertTrue(any("定性" in note and "排名" in note for note in notes))
        self.assertTrue(any("light" in note and "灯" in note for note in notes))
        source = self.research.source(self.user, self.sid, result["source_id"])
        self.assertEqual(source["provenance"]["interpretation_notes"], notes)
        self.assertEqual(source["data"], before["data"])
        self.assertEqual(original, before)
        restored = self.research.tool(cap, "read_saved_source", {"source_id": result["source_id"]})
        self.assertEqual(restored["interpretation_notes"], notes)

    def test_report_query_does_not_infer_unrecognized_producer_semantics(self):
        _, cap = self.live_cap()
        original = {"status": "ok", "report": {**self.known_report_metadata(), "analysis_version": "future/1"},
                    "data": {"summary": {"count": 7}}}
        self.service.executor = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(result=original)))
        result = self.research.tool(cap, "aggregate_report_reviews", {"report_id": "rpt_synthetic",
            "filters": {"models": ["MODEL-GROUP"]}, "term_groups": [{"label": "lighting", "terms": ["light"]}]})
        self.assertNotIn("interpretation_notes", result)

    def test_read_inventory_survives_paging_saved_source_and_file_delivery(self):
        _, cap = self.live_cap()
        def page(identifier, rating):
            return {"status": "ok", "report": self.known_report_metadata(), "data": {
                "query_sha256": "query-a", "provenance": {"source_fingerprint": "frozen-a"},
                "intersection_count": 2, "padding": "原始字段" * 5000,
                "items": [{"review_occurrence_id": identifier, "source_product_id": "sku-a", "channel": "official",
                    "has_text": True, "rating_valid": True, "rating": rating, "review_text": "原始评论"}]}}
        pages = [page("a", 5), page("b", 1)]
        before = copy.deepcopy(pages)
        self.service.executor = SimpleNamespace(invoke=Mock(side_effect=[SimpleNamespace(result=p) for p in pages]))
        arguments = {"report_id": "rpt_synthetic", "filters": {"models": ["MODEL-GROUP"]}}
        first = self.research.tool(cap, "query_report_reviews", arguments)
        self.assertFalse(first["read_scope_summary"]["complete"])
        last = self.research.tool(cap, "query_report_reviews", {**arguments, "offset": 1})
        self.assertTrue(last["complete_content_in_file"])
        self.assertTrue(last["read_scope_summary"]["complete"])
        self.assertEqual(last["read_scope_summary"]["products"][0]["ratings"], {"1": 1, "5": 1})
        restored = self.research.tool(cap, "read_saved_source", {"source_id": last["source_id"]})
        self.assertEqual(restored["read_scope_summary"], last["read_scope_summary"])
        self.assertEqual(pages, before)


    def test_report_catalog_selects_by_scope_inline_and_keeps_complete_filters_in_original(self):
        _, cap = self.live_cap()
        original = {"status": "ok", "page": {"has_more": True, "next_offset": 20}, "citations": [{"label": "目录"}],
            "data": {"items": [{"report_id": "rpt_synthetic", "title": "品牌报告", "display_name": "自定义标题",
                "scope": {"models": ["MODEL-test"], "channels": ["test"], "include_unknown_voltage_platform": False,
                    "job_ids": ["job-test"], "source_completeness": [{"details": "metadata" * 10000}], "future_filter": {"exclude": "other"}},
                "samples": {"review_entities": 20, "written_review_entities": 0}, "quality": {"grade": "D", "limitation_count": 2},
                "future_field": "preserved in full catalog"}]}}
        before = copy.deepcopy(original)
        delivered = self.research._tool_delivery(cap, original, name="list_reports")
        self.assertNotIn("full_result_file", delivered)
        self.assertEqual(delivered["page"], before["page"])
        item = delivered["data"]["items"][0]
        self.assertEqual(item["title"], "自定义标题")
        self.assertEqual(item["scope"]["models"], ["MODEL-test"])
        self.assertIs(item["scope"]["include_unknown_voltage_platform"], False)
        self.assertEqual(item["samples"]["written_review_entities"], 0)
        self.assertIn("筛选摘要", delivered["instruction"])
        self.assertEqual(original, before)
        self.assertEqual(json.loads(self.manager.inputs[-1][2]), before)
        self.assertEqual(delivered["original_result_file"], "/inputs/" + self.manager.inputs[-1][1])

    def test_review_projection_preserves_full_context_unknown_fields_and_raw_json(self):
        _, cap = self.live_cap()
        original = self.data.call(self.user, "query", {})
        row = original["items"][0]
        row.update({"brand": "Synthetic", "model": "LM-test", "category": "mower", "channel": "test-store",
            "submission_date": "2026-09-01", "submission_time_raw": "2026-09-01T10:00:00Z",
            "product_url": "https://example.com/mower", "product_title": "Synthetic mower",
            "review_text": REVIEW_TEXT + " Only on dry, flat ground; I did not try slopes.",
            "verified_purchaser": False, "custom_channel_condition": "original battery only",
            "source_artifact_path": "/snapshot/sample.csv", "normalization_version": "synthetic-v1"})
        row["original_fields"] = {"review_text": row["review_text"], "title": row["review_title"],
            "rating": "5", "submission_time": row["submission_time_raw"],
            "verified_purchaser": 0, "additional_experience": "No experience with wet grass"}
        before = copy.deepcopy(original)
        self.data.call = Mock(return_value=original)
        result = self.research.tool(cap, "query_dataset", {"dataset_id": DATASET_ID, "limit": 1})
        compact = result["items"][0]
        self.assertEqual(original, before)
        for key, value in row.items():
            if key not in {"original_fields", "source_artifact_path", "normalization_version"}:
                self.assertEqual(compact[key], value, key)
        self.assertNotIn("source_artifact_path", compact)
        self.assertNotIn("normalization_version", compact)
        self.assertEqual(compact["original_fields"], {"rating": "5", "verified_purchaser": 0,
            "additional_experience": "No experience with wet grass"})
        full_source = self.research.source(self.user, self.sid, compact["source_id"])
        self.assertEqual(full_source["review"], before["items"][0])
        stored = self.manager.inputs[-1]
        self.assertEqual(json.loads(stored[2])["items"][0], {**row, "source_id": compact["source_id"]})
        self.assertEqual(result["original_result_sha256"], "sha256:" + hashlib.sha256(stored[2]).hexdigest())
        self.assertEqual(result["page"], before["page"])
        self.assertEqual(result["total"], before["total"])

    def test_review_duplicate_comparison_distinguishes_nested_json_scalar_types(self):
        for normalized, raw in ((False, 0), (True, 1), (1, 1.0), (0, False), (1.0, 1)):
            with self.subTest(normalized=repr(normalized), raw=repr(raw)):
                row = {"record_id": "record-a", "review_text": REVIEW_TEXT,
                    "conditions": {"checks": [{"value": normalized}]},
                    "original_fields": {"conditions": {"checks": [{"value": raw}]}}}
                view = self.research._review_reading_view(row)
                self.assertIn("original_fields", view)
                value = view["original_fields"]["conditions"]["checks"][0]["value"]
                self.assertIs(type(value), type(raw))
                self.assertEqual(json.dumps(view, sort_keys=True), json.dumps(row, sort_keys=True))

        equivalent = {"conditions": {"wet_grass": False, "measurements": [1, 1.0]},
            "original_fields": {"conditions": {"measurements": [1, 1.0], "wet_grass": False}}}
        self.assertNotIn("original_fields", self.research._review_reading_view(equivalent))

    def test_index_duplicate_comparison_distinguishes_nested_json_scalar_types(self):
        for normalized, raw in ((False, 0), (True, 1), (1, 1.0), (0, False), (1.0, 1)):
            with self.subTest(normalized=repr(normalized), raw=repr(raw)):
                original = {"data": {"section": "index",
                    "content": {"descriptors": [{"metadata": {"checks": [normalized]}}]},
                    "descriptors": [{"metadata": {"checks": [raw]}}]}}
                view = self.research._reading_view("get_report_section", original)
                self.assertIn("descriptors", view["data"])
                value = view["data"]["descriptors"][0]["metadata"]["checks"][0]
                self.assertIs(type(value), type(raw))
                self.assertEqual(json.dumps(view, sort_keys=True), json.dumps(original, sort_keys=True))

        equivalent = {"data": {"section": "index",
            "content": {"descriptors": [{"pointer": "/findings", "metadata": {"enabled": False, "counts": [1, 1.0]}}]},
            "descriptors": [{"metadata": {"counts": [1, 1.0], "enabled": False}, "pointer": "/findings"}]}}
        self.assertNotIn("descriptors", self.research._reading_view("get_report_section", equivalent)["data"])

    def test_differing_original_body_is_never_deduplicated_or_truncated(self):
        _, cap = self.live_cap()
        body = "Low speed worked on dry grass. " * 300 + "Wet grass was not tested."
        original_body = body + "\nOriginal channel formatting and an additional condition."
        original = {"items": [{"record_id": "record-1", "source_id": "src_original",
            "review_text": body, "review_title": "Complete title", "original_fields": {
                "review_text": original_body, "title": "Different original title"}}]}
        result = self.research._tool_delivery(cap, original, name="query_dataset")
        delivered = json.loads(self.manager.inputs[-1][2]) if "full_result_file" in result else result
        self.assertEqual(delivered["items"], original["items"])

    def test_large_review_projection_prefers_complete_reading_file_with_raw_file_separate(self):
        _, cap = self.live_cap()
        body = "Only dry grass at low blade speed. " * 500 + "The wet-grass condition was NOT tested."
        rows = [{"record_id": "record-" + str(index), "source_id": "src_" + str(index),
            "rating": index + 1, "review_text": body, "review_title": "All title text",
            "brand": "Synthetic", "model": "LM-test", "channel": "test-store", "submission_date": "2026-09-01",
            "original_fields": {"review_text": body, "title": "All title text"},
            "source_artifact_sha256": "sha256:" + "a" * 64} for index in range(3)]
        original = {"items": rows, "total": 3, "dataset_id": DATASET_ID, "source_id": "src_dataset",
            "page": {"returned": 3, "has_more": False}, "next_cursor": None}
        result = self.research._tool_delivery(cap, original, name="query_dataset")
        files = {"/inputs/" + name: content for _, name, content in self.manager.inputs}
        self.assertEqual(json.loads(files[result["original_result_file"]]), original)
        view = json.loads(files[result["full_result_file"]])
        self.assertNotEqual(result["full_result_file"], result["original_result_file"])
        self.assertEqual(len(view["items"]), len(rows))
        for index, item in enumerate(view["items"]):
            self.assertEqual(item, {key: value for key, value in rows[index].items()
                if key not in {"original_fields", "source_artifact_sha256"}})
        self.assertEqual(view["page"], original["page"])
        self.assertEqual(result["total"], 3)
        self.assertIn("优先读取 full_result_file", result["instruction"])
        self.assertEqual(result["sha256"], "sha256:" + hashlib.sha256(files[result["full_result_file"]]).hexdigest())
        self.assertLess(result["full_result_bytes"], result["original_result_bytes"])

    def test_report_review_projection_keeps_public_result_and_saved_citation_unchanged(self):
        _, cap = self.live_cap()
        row = {"record_id": "record-a", "review_title": "Full title", "review_text": REVIEW_TEXT,
            "rating": 3, "source_artifact_path": "/snapshot/test.csv", "original_fields": {"review_text": REVIEW_TEXT}}
        original = {"status": "ok", "report": {"title": "测试报告"}, "data": {"items": [row], "total": 1},
            "page": {"returned": 1, "has_more": False}}
        before = copy.deepcopy(original)
        self.service.executor = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(result=original)))
        result = self.research.tool(cap, "query_report_reviews", {"report_id": "rpt_synthetic", "limit": 1})
        compact = result["data"]["items"][0]
        self.assertEqual(original, before)
        self.assertEqual(compact["review_text"], REVIEW_TEXT)
        self.assertEqual(compact["record_id"], row["record_id"])
        self.assertNotIn("original_fields", compact)
        saved = self.research.source(self.user, self.sid, compact["source_id"])
        self.assertEqual(saved["review"], row)
        self.assertEqual(self.research.source(self.user, self.sid, result["source_id"])["data"], before["data"])

    def test_long_python_stdout_is_saved_complete_without_replaying_it_in_every_prompt(self):
        _, cap = self.live_cap()
        result = {"stdout": "x" * 30000, "stderr": "", "exit_code": 0, "output_truncated": False}
        delivered = self.research._tool_delivery(cap, result, name="run_python")
        self.assertNotIn("stdout", delivered)
        self.assertEqual(json.loads(self.manager.inputs[-1][2])["stdout"], result["stdout"])
        self.assertEqual(delivered["exit_code"], 0)
        self.assertFalse(delivered["output_truncated"])
        self.assertNotIn("original_result_file", delivered)

    @staticmethod
    def known_report_metadata():
        return {"title": "测试报告", "schema_version": "report-json/v1", "analysis_version": "voxquarry-reporting/0.12.0"}

    @staticmethod
    def report_meaning_fixture():
        return {"factor": {"table_id": "attribute_performance", "columns": [{"field": "negative_share", "label": "差评占比"}],
            "rows": [{"factor_id": "drive", "label": "自走", "mention_count": 4, "rated_mentions": 4,
                "negative_count": 1, "negative_share": .25, "positive_count": 3, "positive_share": .75}]},
            "aspect": {"aspect_id": "drive", "review_entities": 4, "rating_n": 4,
                "low_star_entities": 1, "low_star_rate": .25, "explicit_negative_entities": 2,
                "explicit_negative_rate": .5, "negative_evidence_ids": ["review-a", "review-b"]},
            "unknown": {"negative_count": 19, "negative_share": .95, "note": "Unspecified external metric"},
            "original_review": "The drive stopped only during sharp turns; straight-line mowing worked."}

    def test_report_model_boundary_defines_known_metrics_without_changing_sources(self):
        _, cap = self.live_cap()
        original = {"status": "ok", "report": self.known_report_metadata(),
            "data": {"section": "/findings", "content": self.report_meaning_fixture()},
            "page": {"has_more": False}}
        before = copy.deepcopy(original)
        self.service.executor = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(result=original)))
        for name in ("get_report_section", "aggregate_report_reviews"):
            with self.subTest(name=name):
                args = {"report_id": "rpt_synthetic"}
                if name == "get_report_section":
                    args["section"] = "/findings"
                result = self.research.tool(cap, name, args)
                content = result["data"]["content"]
                factor, aspect = content["factor"]["rows"][0], content["aspect"]
                rating = factor["_evidence_semantics"]["whole_product_rating"]
                self.assertIn("整机1–2星", rating["fields"]["negative_share"])
                self.assertEqual(rating["denominator_field"], "rated_mentions")
                expression = aspect["_evidence_semantics"]["rule_aspect_expression"]
                self.assertIn("同句规则", expression["fields"]["explicit_negative_entities"])
                self.assertIn("独立于整机星级", expression["meaning"])
                self.assertIn("规则可能误读", expression["meaning"])
                self.assertEqual((factor["negative_count"], factor["negative_share"], aspect["explicit_negative_entities"]), (1, .25, 2))
                self.assertEqual(content["unknown"], before["data"]["content"]["unknown"])
                self.assertEqual(content["original_review"], before["data"]["content"]["original_review"])
                self.assertIn("整机1–2星", content["factor"]["columns"][0]["label"])
                self.assertEqual(original, before)
                self.assertEqual(self.research.source(self.user, self.sid, result["source_id"])["data"], before["data"])
                files = {"/inputs/" + name: value for _, name, value in self.manager.inputs}
                raw = files[result["original_result_file"]]
                self.assertEqual(json.loads(raw), {**before, "source_id": result["source_id"]})
                self.assertEqual(result["original_result_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
                definitions = result["report_field_guide"]["definitions"]
                self.assertTrue(any(item["kind"] == "whole_product_rating" for item in definitions))
                self.assertTrue(any(item["kind"] == "rule_aspect_expression" for item in definitions))

    def test_large_report_keeps_field_guide_visible_and_definitions_in_full_reading_file(self):
        _, cap = self.live_cap()
        content = self.report_meaning_fixture()
        content["original_review"] *= 1500
        content["factor"]["rows"] *= 12
        original = {"report": self.known_report_metadata(), "data": {"section": "/tables", "content": content}, "source_id": "src_report"}
        result = self.research._tool_delivery(cap, original, name="get_report_section")
        self.assertNotIn("data", result)
        self.assertIn("report_field_guide", result)
        files = {"/inputs/" + name: value for _, name, value in self.manager.inputs}
        self.assertEqual(json.loads(files[result["original_result_file"]]), original)
        view = json.loads(files[result["full_result_file"]])
        self.assertEqual(view["data"]["content"]["original_review"], content["original_review"])
        self.assertEqual(len(view["data"]["content"]["factor"]["rows"]), 12)
        for row in view["data"]["content"]["factor"]["rows"]:
            self.assertIn("negative_share", row["_evidence_semantics"]["whole_product_rating"]["fields"])
            self.assertEqual(row["negative_share"], .25)
        self.assertIn("rule_aspect_expression", view["data"]["content"]["aspect"]["_evidence_semantics"])
        self.assertLessEqual(len(result["report_field_guide"]["definitions"]), 8)
        self.assertLess(len(json.dumps(result["report_field_guide"], ensure_ascii=False).encode()), 8192)
        for definition in result["report_field_guide"]["definitions"]:
            self.assertLessEqual(len(definition["example_paths"]), 2)
            for pointer in definition["example_paths"]:
                node = view
                for component in pointer.strip("/").split("/"):
                    component = component.replace("~1", "/").replace("~0", "~")
                    node = node[int(component)] if isinstance(node, list) else node[component]
                self.assertEqual(node["_evidence_semantics"][definition["kind"]], definition["definition"])

    def test_report_preview_marks_missing_rows_and_preserves_complete_counterevidence(self):
        _, cap = self.live_cap()
        original = {'source_id': 'src_report', 'data': {'content': {
            'scope': 'one product, 20 reviews',
            'rows': [{'stance': 'positive', 'text': 'evidence ' * 400} for _ in range(4)]
                    + [{'stance': 'negative', 'text': 'counterevidence retained in complete result'}]}}}
        result = self.research._tool_delivery(cap, original, name='get_report_section')
        self.assertTrue(result['preview_is_partial'])
        self.assertEqual(result['content_preview']['scope'], 'one product, 20 reviews')
        self.assertEqual(result['content_preview']['rows']['total_items'], 5)
        self.assertTrue(result['content_preview']['rows']['partial'])
        files = {'/inputs/' + name: value for _, name, value in self.manager.inputs}
        self.assertEqual(json.loads(files[result['full_result_file']]), original)
        self.assertIn('反例', result['preview_note'])

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    def test_saved_report_reapplies_definitions_without_rewriting_saved_content(self):
        _, cap = self.live_cap()
        original = {"source_type": "report", "title": "报告原文", "report_id": "rpt_synthetic",
            "content": self.report_meaning_fixture(), "provenance": {"report": self.known_report_metadata()}}
        source_id = self.research._source(cap, original)
        result = self.research.tool(cap, "read_saved_source", {"source_id": source_id})
        self.assertEqual(result["source_id"], source_id)
        self.assertIn("report_field_guide", result)
        self.assertIn("whole_product_rating", result["content"]["factor"]["rows"][0]["_evidence_semantics"])
        self.assertIn("rule_aspect_expression", result["content"]["aspect"]["_evidence_semantics"])
        self.assertEqual(self.state.source(self.user, self.sid, source_id)["source"], original)
        raw = {"/inputs/" + name: value for _, name, value in self.manager.inputs}[result["original_result_file"]]
        restored = json.loads(raw)
        self.assertEqual(restored["content"], original["content"])
        self.assertEqual(restored["source_id"], source_id)
        self.assertNotIn("_evidence_semantics", restored["content"]["aspect"])

    def test_review_attachment_and_code_results_do_not_receive_report_definitions(self):
        _, cap = self.live_cap()
        content = self.report_meaning_fixture()
        source_id = self.research._source(cap, {"source_type": "review", "title": "原始评论", "content": content,
            "provenance": {"report": self.known_report_metadata()}})
        saved = self.research.tool(cap, "read_saved_source", {"source_id": source_id})
        self.assertEqual(saved["content"], content)
        self.assertNotIn("report_field_guide", saved)
        query = self.research._tool_delivery(cap, {"items": [content]}, name="query_dataset")
        self.assertEqual(query["items"], [content])
        self.assertNotIn("report_field_guide", query)
        text = json.dumps(content, ensure_ascii=False)
        attachment = self.attachments.create(self.user, self.sid, "用户字段.txt", "text/plain", text.encode())
        read = self.research.tool(cap, "read_attachment", {"attachment_id": attachment["attachment_id"]})
        self.assertEqual(read["text"], text)
        self.assertNotIn("report_field_guide", read)
        saved_attachment = self.research.tool(cap, "read_saved_source", {"source_id": read["source_id"]})
        self.assertEqual(saved_attachment["content"], text)
        self.assertNotIn("report_field_guide", saved_attachment)
        self.manager.tool = Mock(return_value={"stdout": text, "stderr": "", "exit_code": 0, "output_truncated": False})
        code = self.research.tool(cap, "run_python", {"code": "print('synthetic output')"})
        self.assertEqual(code["stdout"], text)
        self.assertNotIn("report_field_guide", code)

    def test_unknown_source_semantics_block_is_not_promoted_to_program_field_guide(self):
        _, cap = self.live_cap()
        for supplied in ("opaque original field", [{"original": "metadata"}],
                         {"source_claim": {"low_star_rate": .25}},
                         {"whole_product_rating": {"fields": {"negative_count": "arbitrary source meaning"}, "meaning": "untrusted" * 2000}}):
            with self.subTest(type=type(supplied).__name__):
                original = {"report": self.known_report_metadata(), "data": {"content": {"negative_count": 17, "_evidence_semantics": supplied}}}
                result = self.research._tool_delivery(cap, original, name="get_report_section")
                delivered = json.loads(self.manager.inputs[-1][2]) if "full_result_file" in result else result
                self.assertEqual(delivered["data"], original["data"])
                self.assertNotIn("report_field_guide", result)

    def test_unknown_report_versions_are_not_assigned_known_producer_definitions(self):
        _, cap = self.live_cap()
        for metadata in ({}, {**self.known_report_metadata(), "schema_version": "report-json/v2"},
                         {**self.known_report_metadata(), "analysis_version": "unknown-producer/1.0"}):
            for name in ("get_report_section", "aggregate_report_reviews", "read_saved_source"):
                with self.subTest(metadata=metadata, name=name):
                    original = {"report": metadata, "data": {"content": self.report_meaning_fixture()}}
                    if name == "read_saved_source":
                        original = {"source_type": "report", "provenance": {"report": metadata},
                            "content": self.report_meaning_fixture()}
                    result = self.research._tool_delivery(cap, original, name=name)
                    self.assertNotIn("report_field_guide", result)
                    self.assertNotIn("original_result_file", result)
                    self.assertNotIn("_evidence_semantics", json.dumps(result))
                    for key, value in original.items():
                        self.assertEqual(result[key], value)

    def test_rejected_resume_does_not_lose_waiting_subscription(self):
        run = self.accept()
        self.state.checkpoint(self.user, run["run_id"], {"waiting_operation_ids": ["waiting-1"]},
                              status="waiting_collection", attempt_id=run["attempt_id"])
        self.research._pending[run["run_id"]] = self.user
        # Waiting releases the session's running slot, so another question may
        # legitimately be in flight when the user clicks continue on this one.
        self.accept(request_id="another-question")
        with self.assertRaises(AgentError):
            self.research.resume(self.user, self.sid, run["run_id"], "resume-busy", action="continue_available")
        self.assertIn(run["run_id"], self.research._pending)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "waiting_collection")

    def test_attachment_is_read_as_full_private_source_without_shared_data_import(self):
        item = self.attachments.create(self.user, self.sid, "用户说明.md", "text/markdown", "机器只在坡地停机。\n平地未遇到。".encode())
        run = self.accept(attachment_ids=[item["attachment_id"]])
        def answer_attachment(payload):
            cap = self.cap(payload)
            result = self.research.tool(cap, "read_attachment", {"attachment_id": item["attachment_id"]})
            self.last_source_id = result["source_id"]
            self.assertIn("平地未遇到", result["text"])
            self.research.tool(cap, "submit_answer", {"answer": "附件记录的停机只发生在坡地。[" + self.last_source_id + "]"})
        self.manager.on_start = answer_attachment
        self.research.execute(self.user, run)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "completed")
        self.assertEqual(self.data.calls, [])
        source = self.research.source(self.user, self.sid, self.last_source_id)
        self.assertIn("平地未遇到", source["content"])

    def test_default_trace_keeps_safe_queries_and_source_versions_without_raw_review_body(self):
        self.traces.content_enabled = False
        report_id, snapshot_id = "rpt_synthetictrace12345", "snapshot-synthetic-v1"
        self.service.executor = SimpleNamespace(invoke=lambda *_args, **_kwargs: SimpleNamespace(result={
            "status": "ok", "report": {"report_id": report_id, "title": "合成报告", "data_snapshot_id": snapshot_id},
            "snapshot": {"snapshot_id": snapshot_id, "manifest_sha256": "sha256:" + "c" * 64},
            "data": {"section": "/samples", "content": {"review_entities": 1}}, "page": None}))
        run = self.accept()
        def read(payload):
            cap = self.cap(payload)
            self.research.tool(cap, "get_report_section", {"report_id": report_id, "section": "/samples", "offset": 0, "limit": 10})
            self.read_and_answer(payload)
        self.manager.on_start = read
        self.research.execute(self.user, run)
        final = self.state.get(self.user, run["run_id"])
        self.assertEqual(final["status"], "completed")
        events = self.traces.list_events(self.user, final["trace_id"])["items"]
        started = [event["data"] for event in events if event["event_type"] == "tool.started"]
        query = next(event for event in started if event["name"] == "query_dataset")
        self.assertEqual(query["arguments"], {"dataset_id": DATASET_ID, "limit": 1})
        report = next(event for event in started if event["name"] == "get_report_section")
        self.assertEqual(report["arguments"]["report_id"], report_id)
        self.assertEqual(report["arguments"]["section"], "/samples")
        source_events = [event["data"] for event in events if event["event_type"] == "source.read"]
        encoded = json.dumps(source_events)
        for identifier in (report_id, snapshot_id, DATASET_ID, "sha256:" + "b" * 64, "sha256:" + "c" * 64):
            self.assertIn(identifier, encoded)
        persisted = self.traces.database_path.read_bytes()
        self.assertNotIn(REVIEW_TEXT.encode(), persisted)
        self.assertEqual(list(self.traces.payload_dir.iterdir()), [])

    def test_finished_collection_with_unreadable_artifacts_resumes_existing_materials(self):
        for status in ("completed", "partial_success"):
            with self.subTest(status=status):
                run = self.accept(request_id="unreadable-" + status)
                self.state.checkpoint(self.user, run["run_id"], {"waiting_operation_ids": ["operation-a"]},
                                      status="waiting_collection", attempt_id=run["attempt_id"])
                self.research._pending[run["run_id"]] = self.user
                def unavailable(operation, args=None):
                    self.assertEqual(operation, "status")
                    return {"status": status, "data": {"ready": False, "reason": "artifact_verification_failed"}}
                with patch.object(self.collection, "call", side_effect=unavailable), \
                     patch.object(self.research, "_stopped", SimpleNamespace(wait=Mock(side_effect=[False, True]), is_set=lambda: False)):
                    self.research._monitor_waiting()
                current = self.state.get(self.user, run["run_id"])
                self.assertEqual(current["status"], "running")
                self.assertFalse(current["checkpoint"]["collection_completed"])
                self.assertIn("artifact_verification_failed", json.dumps(current["checkpoint"]["collection_outcomes"]))
                self.assertNotIn(run["run_id"], self.research._pending)
                self.assertEqual(len(self.pool.calls), 1)
                self.pool.calls.clear()
                self.state.cancel(self.user, run["run_id"])

    def test_cancel_between_ensure_check_and_checkpoint_removes_only_own_subscription(self):
        run, cap = self.live_cap(self.accept(product_urls=["https://www.lowes.com/pd/test/12345"]))
        original = self.state.checkpoint
        def cancelled_before_save(actor, run_id, payload, **kwargs):
            if "operations" in payload:
                self.research.cancel(self.user, self.sid, run_id)
            return original(actor, run_id, payload, **kwargs)
        with patch.object(self.state, "checkpoint", side_effect=cancelled_before_save):
            result = self.research.tool(cap, "collect_product", {"channel": "lowes", "product_url": "https://www.lowes.com/pd/test/12345"})
        self.assertIn("error", result)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "cancelled")
        self.assertEqual([name for name, _ in self.collection.calls], ["ensure", "unsubscribe"])
        ensured, removed = self.collection.calls
        self.assertEqual(removed[1]["research_run_id"], run["run_id"])
        self.assertEqual(removed[1]["request_key"], ensured[1]["request_key"])
        self.assertNotIn("operations", self.state.get(self.user, run["run_id"])["checkpoint"])

    def test_parallel_collect_tools_preserve_both_operation_checkpoints(self):
        run, cap = self.live_cap(self.accept(product_urls=["https://www.lowes.com/pd/test/1", "https://www.lowes.com/pd/test/2"]))
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        original = self.collection.call
        calls = []
        def slow_ensure(operation, args=None):
            if operation == "ensure":
                calls.append(args["product_url"])
                if len(calls) == 1:
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError("test failed to release first collection")
            return original(operation, args)
        def collect(number):
            if number == 2:
                second_started.set()
            return self.research.tool(cap, "collect_product", {"channel": "lowes", "product_url": "https://www.lowes.com/pd/test/" + str(number)})
        with patch.object(self.collection, "call", side_effect=slow_ensure), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(collect, 1)
            self.assertTrue(entered.wait(1))
            second = pool.submit(collect, 2)
            try:
                self.assertTrue(second_started.wait(1))
            finally:
                release.set()
            results = [first.result(timeout=2), second.result(timeout=2)]
        self.assertTrue(all("operation_id" in result for result in results))
        operations = self.state.get(self.user, run["run_id"])["checkpoint"]["operations"]
        self.assertEqual(set(operations), {result["operation_id"] for result in results})
        self.assertEqual({value["product_url"] for value in operations.values()}, {"https://www.lowes.com/pd/test/1", "https://www.lowes.com/pd/test/2"})

    def export_fixture(self, *, mismatch=False):
        normalized = b'{"record_id":"eligible-a","review_text":"normalized full text"}\n'
        raw = b'{"review":"complete channel payload","extra":"preserved","excluded":true}\n'
        digest = lambda value: "sha256:" + hashlib.sha256(value).hexdigest()
        manifest = {"dataset_id": DATASET_ID, "manifest_sha256": "sha256:" + "d" * 64, "raw_payload_complete": True,
            "shards": [{"path": "normalized.jsonl", "sha256": digest(normalized)}],
            "raw_artifacts": [{"records": 1, "shards": [{"path": "raw.jsonl", "sha256": "sha256:" + "0" * 64 if mismatch else digest(raw)}]}]}
        exports = []
        def export(actor, dataset, shard, *, source_type):
            exports.append((dataset, shard, source_type))
            return raw if source_type == "channel_raw_payload" else normalized
        self.research.data = SimpleNamespace(call=lambda *_: copy.deepcopy(manifest), export=export)
        return manifest, normalized, raw, exports

    def test_raw_and_normalized_exports_stay_separate_and_restore_in_new_attempt(self):
        manifest, normalized, raw, exports = self.export_fixture()
        run, cap = self.live_cap()
        cap.runtime_contract = 'wenquan-dsh/v1'
        result = self.research.tool(cap, "export_dataset", {"dataset_id": DATASET_ID})
        self.assertEqual(result["manifest"], manifest)
        self.assertEqual(result["bytes"], len(normalized) + len(raw))
        self.assertEqual(len(result["analysis_paths"]), 1)
        self.assertEqual(len(result["raw_payload_paths"]), 1)
        self.assertNotEqual(result["analysis_paths"], result["raw_payload_paths"])
        self.assertEqual([item[2] for item in exports], ["normalized_review", "channel_raw_payload"])
        self.assertEqual([item[2] for item in self.manager.inputs], [normalized, raw])
        source = self.research.source(self.user, self.sid, result["source_id"])
        self.assertEqual(source["provenance"]["manifest_sha256"], manifest["manifest_sha256"])
        self.state.cancel(self.user, run["run_id"])
        resumed = self.state.resume(self.user, run["run_id"])["run"]
        self.assertEqual(resumed["checkpoint"]["exported_datasets"], [DATASET_ID])
        _token, new_cap = self.research.broker.issue(self.user, resumed, cap.trace_id)
        new_cap.runtime_contract = 'wenquan-dsh/v1'
        repeated = self.research.tool(new_cap, "export_dataset", {"dataset_id": DATASET_ID})
        self.assertEqual(repeated["source_id"], result["source_id"])
        self.assertEqual([item[0] for item in self.manager.inputs], [run["attempt_id"], run["attempt_id"], resumed["attempt_id"], resumed["attempt_id"]])

    def test_bad_raw_shard_cannot_mark_dataset_export_complete_or_create_source(self):
        _manifest, _normalized, _raw, _exports = self.export_fixture(mismatch=True)
        run, cap = self.live_cap()
        result = self.research.tool(cap, "export_dataset", {"dataset_id": DATASET_ID, "include_raw": True})
        self.assertEqual(result["error"]["code"], "dataset_changed")
        self.assertNotIn("exported_datasets", self.state.get(self.user, run["run_id"])["checkpoint"])
        self.assertEqual(self.state.list_sources(self.user, run["run_id"]), [])
        self.assertEqual(len(self.manager.inputs), 1)

    def test_prepare_product_research_is_light_and_reuses_scoped_candidates(self):
        manifest, normalized, raw, exports = self.export_fixture()
        calls = []
        def data(actor, operation, args):
            calls.append((operation, args))
            if operation == "analyze":
                return {"dataset_id": DATASET_ID, "definition_id": "fan-v1", "analysis_id": "analysis-one", "chapters": []}
            return copy.deepcopy(manifest)
        self.research.data.call = data
        self.research.service.executor = SimpleNamespace(transport=SimpleNamespace(report_workflow=Mock(return_value={"items": []})))
        _, cap = self.live_cap()
        one = self.research.tool(cap, "prepare_product_research", {"job_ids": ["sample-job"]})
        two = self.research.tool(cap, "prepare_product_research", {"job_ids": ["sample-job"]})
        self.assertEqual(one["dataset_id"], DATASET_ID)
        self.assertEqual(one["report_candidates"], two["report_candidates"])
        self.research.service.executor.transport.report_workflow.assert_called_once_with("catalog", {"scope": {"job_ids": ["sample-job"]}}, cap.actor)
        self.assertEqual(len(exports), 0)
        self.assertEqual(sum(op == "analyze" for op, _ in calls), 0)
        self.assertEqual(self.manager.inputs, [])

    def test_shared_report_chapter_has_owner_private_citation(self):
        _, cap = self.live_cap()
        result = self.research.tool(cap, 'read_research_reference', {'reference_id': 'synthetic-study', 'section_id': 'home'})
        self.assertIn('Synthetic study', result['text'])
        saved = self.research.source(self.user, self.sid, result['source_id'])
        self.assertEqual(saved['source_type'], 'reference')
        self.assertEqual(saved['locator']['section_id'], 'home')
        with self.assertRaises(AgentError):
            self.research.source(actor('other', roles=('user',)), self.sid, result['source_id'])

    def test_dataset_reader_uses_same_filter_version_and_actor(self):
        _, cap = self.live_cap()
        source = self.research.tool(cap, "query_dataset", {"dataset_id": DATASET_ID, "filters": {"ratings": [1, 2]}, "limit": 1})
        sid = source["source_id"]
        initial = self.research.source(self.user, self.sid, sid)
        self.assertIn("1 条", initial["dataset_summary"]["description"])
        page = self.research.source(self.user, self.sid, sid, offset=20)
        self.assertEqual(page["dataset_reviews"][0]["review_text"], REVIEW_TEXT)
        self.assertEqual(page["page"]["next_offset"], 21)
        user, operation, args = self.data.calls[-1]
        self.assertEqual(user, self.user)
        self.assertEqual(args["filters"], {"ratings": [1, 2]})
        self.assertEqual(args["offset"], 20)
        self.assertEqual(args["limit"], 20)
        fresh = self.data.call(self.user, "query", args)
        fresh["manifest_sha256"] = "sha256:" + "f" * 64
        with patch.object(self.data, "call", return_value=fresh), self.assertRaises(AgentError) as error:
            self.research.source(self.user, self.sid, sid, offset=0)
        self.assertEqual(error.exception.code, "source_changed")
        with patch.object(self.data, "call") as data, self.assertRaises(AgentError):
            self.research.source(actor("another-user", roles=("user",)), self.sid, sid, offset=0)
        data.assert_not_called()

    def recovery_used(self, run):
        # Final-diagnostic assertions run after both automatic attempts were used.
        self.state.checkpoint(self.user, run["run_id"],
            {**run["checkpoint"], "auto_recovery": {"pending": False, "codes": ["model_stream_interrupted"] * 2}},
            status="running", attempt_id=run["attempt_id"])

    def model_events(self, cap, call, *, code=None, complete=False, attempt_id=None):
        aid = attempt_id or cap.attempt_id
        def event(kind, data):
            self.traces.append_event(self.user, cap.trace_id, event_id=aid + ":" + str(call) + ":" + kind,
                event_type=kind, attempt_id=aid, data={"call": call, **data})
        event("model.started", {})
        if code:
            event("error.model", {"code": code, "message": "private-provider-body-must-not-be-published"})
        event("model.finished", {"complete": complete})

    def test_failed_harness_retains_safe_diagnostic_before_sandbox_cleanup(self):
        run = self.accept()
        self.recovery_used(run)
        self.manager.outcome = {"status": "failed", "events": [],
            "error": {"code": "harness_failed", "runner_error_type": "ReadTimeout", "message": "private provider payload"},
            "result": {"finish_reason": "unsafe message with credential"}}
        self.research.execute(self.user, run)
        current = self.state.get(self.user, run["run_id"])
        self.assertEqual(current["status"], "blocked")
        events = self.traces.list_events(self.user, current["trace_id"])["items"]
        diagnostic = next(e["data"] for e in events if e["event_type"] == "error.harness")
        self.assertEqual(diagnostic["runner_error_type"], "ReadTimeout")
        self.assertEqual(diagnostic["code"], "harness_failed")
        self.assertNotIn("finish_reason", diagnostic)
        self.assertNotIn("private provider payload", json.dumps(events))
        self.assertEqual(self.manager.stops, [current["attempt_id"]])

    def test_manager_transport_type_is_retained_without_private_exception_text(self):
        run = self.accept()
        self.recovery_used(run)
        error = AgentError("harness_unavailable", "研究执行服务暂时无法连接", http_status=503, retryable=True)
        error.transport_kind = "connect_timeout"
        self.manager.status = Mock(side_effect=error)
        self.research.execute(self.user, run)
        current = self.state.get(self.user, run["run_id"])
        events = self.traces.list_events(self.user, current["trace_id"])["items"]
        finished = next(event["data"] for event in events if event["event_type"] == "lifecycle.finished")
        self.assertEqual(finished["error"]["transport_kind"], "connect_timeout")
        self.assertNotIn("connect_timeout", self.store.list_messages(self.user, self.sid)[-1]["content"])
        self.assertEqual(self.manager.stops, [current["attempt_id"]])

    def test_failed_tool_retains_safe_transport_type_without_replaying_it(self):
        _, cap = self.live_cap()
        error = AgentError("harness_unavailable", "研究执行服务暂时无法连接", http_status=503, retryable=True)
        error.transport_kind = "read_timeout"
        self.manager.tool = Mock(side_effect=error)
        output = self.research.tool(cap, "run_python", {"code": "print(1)"})
        events = self.traces.list_events(self.user, cap.trace_id)["items"]
        diagnostic = next(event["data"] for event in events if event["event_type"] == "tool.failed")
        self.assertEqual(diagnostic["transport_kind"], "read_timeout")
        self.assertNotIn("read_timeout", json.dumps(output))
        self.assertEqual(self.manager.tool.call_count, 1)

    def test_terminal_model_failure_uses_safe_specific_reason_for_matching_call(self):
        cases = (("model_access_denied", "未开通", False), ("model_auth_failed", "API Key", False),
                 ("model_not_found", "找不到", False), ("model_rate_limited", "服务限制", True),
                 ("model_connection_failed", "无法连接", True), ("model_stream_interrupted", "连接中断", True))
        for code, phrase, retryable in cases:
            with self.subTest(code=code):
                run = self.accept(request_id="failure-" + code)
                self.recovery_used(run)
                self.manager.on_start = lambda payload: self.model_events(self.cap(payload), 1, code=code)
                self.manager.outcome = {"status": "failed", "events": [], "result": None}
                self.research.execute(self.user, run)
                self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "blocked")
                message = self.store.list_messages(self.user, self.sid)[-1]
                self.assertEqual(message["metadata"]["error"]["code"], code)
                self.assertEqual(message["metadata"]["error"]["retryable"], retryable)
                self.assertIn(phrase, message["content"])
                self.assertNotIn("private-provider", message["content"])

    def test_old_recovered_or_other_attempt_model_error_is_not_final_cause(self):
        for scenario in ("recovered", "other_attempt", "unfinished", "different_call", "late_old_completion"):
            with self.subTest(scenario=scenario):
                run = self.accept(request_id="not-final-" + scenario)
                self.recovery_used(run)
                def history(payload):
                    cap = self.cap(payload)
                    self.model_events(cap, 1, code="model_access_denied", attempt_id="previous-attempt" if scenario == "other_attempt" else None)
                    if scenario == "recovered":
                        self.model_events(cap, 2, complete=True)
                    elif scenario == "unfinished":
                        self.research.broker.event(cap, "model.started", data={"call": 2})
                    elif scenario in {"other_attempt", "different_call"}:
                        self.model_events(cap, 2, code="unknown_model_error")
                    elif scenario == "late_old_completion":
                        self.model_events(cap, 2, complete=True)
                        self.research.broker.event(cap, "model.finished", data={"call": 1, "complete": False})
                self.manager.on_start = history
                self.manager.outcome = {"status": "failed", "events": [], "result": None}
                self.research.execute(self.user, run)
                message = self.store.list_messages(self.user, self.sid)[-1]
                self.assertEqual(message["metadata"]["error"]["code"], "harness_execution_failed")
                self.assertNotIn("未开通", message["content"])

    def test_completed_sdk_residual_text_cannot_hide_stream_failure(self):
        for with_citation in (False, True):
            with self.subTest(with_citation=with_citation):
                run = self.accept(request_id="residual-" + str(with_citation))
                self.recovery_used(run)
                def interrupted(payload):
                    cap = self.cap(payload)
                    source = self.research.tool(cap, "query_dataset", {"dataset_id": DATASET_ID, "limit": 1})
                    text = "未完成的回复"
                    if with_citation:
                        text += "[" + source["items"][0]["source_id"] + "]"
                    self.manager.outcome = {"status": "completed", "events": [],
                        "result": {"answer": text, "finish_reason": "error"}}
                    self.model_events(cap, 18, code="model_stream_interrupted")
                self.manager.on_start = interrupted
                self.research.execute(self.user, run)
                current = self.state.get(self.user, run["run_id"])
                self.assertEqual(current["status"], "blocked")
                message = self.store.list_messages(self.user, self.sid)[-1]
                self.assertEqual(message["metadata"]["error"]["code"], "model_stream_interrupted")
                self.assertIn("连接中断", message["content"])
                self.assertNotIn("未完成的回复", message["content"])
                events = self.traces.list_events(self.user, current["trace_id"])["items"]
                diagnostic = next(e["data"] for e in events if e["event_type"] == "error.harness")
                self.assertEqual(diagnostic["manager_status"], "completed")
                self.assertEqual(diagnostic["finish_reason"], "error")

    def test_valid_submitted_answer_wins_over_later_failed_model_cleanup(self):
        run = self.accept()
        def submit_then_error(payload):
            self.read_and_answer(payload)
            self.model_events(self.cap(payload), 1, code="model_connection_failed")
        self.manager.on_start = submit_then_error
        self.manager.outcome = {"status": "failed", "events": [], "result": None}
        self.research.execute(self.user, run)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "completed")
        self.assertIn("25 分钟", self.store.list_messages(self.user, self.sid)[-1]["content"])
        self.assertNotIn("无法连接", self.store.list_messages(self.user, self.sid)[-1]["content"])

    def test_sdk_fallback_requires_native_successful_turn_reason(self):
        for reason in (None, "error", "aborted", "max-tokens", "stop", "completed"):
            with self.subTest(reason=reason):
                run = self.accept(request_id="reason-" + str(reason))
                self.recovery_used(run)
                def last_successful_step(payload):
                    cap = self.cap(payload)
                    source = self.research.tool(cap, "query_dataset", {"dataset_id": DATASET_ID, "limit": 1})
                    self.manager.outcome = {"status": "completed", "events": [], "result": {
                        "answer": "用户用中档刀速割小草坪，耗时 25 分钟。[" + source["items"][0]["source_id"] + "]",
                        "finish_reason": reason}}
                self.manager.on_start = last_successful_step
                self.research.execute(self.user, run)
                current = self.state.get(self.user, run["run_id"])
                self.assertEqual(current["status"], "completed" if reason == "completed" else "blocked")
                if reason != "completed":
                    self.assertEqual(self.store.list_messages(self.user, self.sid)[-1]["metadata"]["error"]["code"], "harness_execution_failed")

    def test_last_failed_call_error_can_be_found_across_trace_pages(self):
        run, cap = self.live_cap()
        self.research.broker.event(cap, "model.started", data={"call": 1})
        self.research.broker.event(cap, "error.model", data={"call": 1, "code": "model_rate_limited"})
        for number in range(1005):
            self.research.broker.event(cap, "lifecycle.harness", data={"sequence": number})
        self.research.broker.event(cap, "model.finished", data={"call": 1, "complete": False})
        error = self.research._last_model_failure(self.user, cap.trace_id, run["attempt_id"])
        self.assertEqual(error.code, "model_rate_limited")


if __name__ == "__main__":
    unittest.main()


class PublicToolProgressTests(unittest.TestCase):
    def test_summaries_describe_actions_without_exposing_code_or_payloads(self):
        secret = 'fixture-private-value-not-for-ui'
        result = tool_progress_summary('run_python', {'code': "print('" + secret + "'); Counter(reviews)"}, {'exit_code': 0, 'stdout': secret})
        self.assertEqual(result, '统计评论数量和分布；执行成功')
        self.assertNotIn(secret, result)
        self.assertEqual(tool_progress_summary('run_python', {'code': 'raise Exception()'}, {'exit_code': 1}), '运行 Python 分析；执行出错，正在调整')
        self.assertEqual(tool_progress_summary('read_file', {'path': '/workspace/summary.json'}), '读取 summary.json')
        self.assertEqual(tool_progress_summary('read_file', {'path': '/workspace/<script>'}), '')
        self.assertEqual(tool_progress_summary('rank_review_topics', {}, {'checked_reviews': 100, 'total_reviews': 428, 'needs_revision': [{}, {}]}), '已核对 100/428 条评论；本批 2 条需修正')
