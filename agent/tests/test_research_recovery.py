"""Fault-injection evals of automatic continuation; no live provider calls."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent.contracts import AgentError
from agent.tests.test_harness_research import HarnessFixture, DATASET_ID
from agent.tests.test_research_authorization import CurrentAuthority


class RecoveryTests(HarnessFixture, unittest.TestCase):
    def tick(self):
        with patch.object(self.research, "_stopped", SimpleNamespace(wait=Mock(side_effect=[False, True]), is_set=lambda: False)):
            self.research._monitor_waiting()

    def inject_failure(self, run, code="model_stream_interrupted"):
        self.manager.status = Mock(side_effect=AgentError(code, "临时中断", http_status=503, retryable=True))
        self.research.execute(self.user, run)
        self.research._scheduled.discard(run["attempt_id"])
        return self.state.get(self.user, run["run_id"])

    def test_stream_loss_resumes_same_run_session_sources_and_budget_without_user(self):
        run = self.accept()
        saved = {}
        def read_once(payload):
            cap = self.cap(payload)
            result = self.research.tool(cap, "query_dataset", {"dataset_id": DATASET_ID, "limit": 1})
            saved.update(cap=cap, source=result["items"][0]["source_id"], session=cap.session_key)
        self.manager.on_start = read_once
        waiting = self.inject_failure(run)
        self.assertEqual(waiting["execution_status"], "recovering")
        self.assertFalse(waiting["can_resume"])
        self.assertEqual([m["role"] for m in self.store.list_messages(self.user, self.sid)], ["user"])
        before = self.research.broker.budget_status(saved["cap"])
        self.tick()
        current = self.state.get(self.user, run["run_id"])
        self.assertEqual(current["execution_status"], "queued")
        self.assertEqual(current["attempt_number"], 2)
        self.assertIn(saved["source"], json.dumps(self.research._resume_context(self.user, run["run_id"])))
        def finish(payload):
            cap = self.cap(payload)
            self.assertEqual(cap.session_key, saved["session"])
            self.assertEqual(self.research.broker.budget_status(cap)["tool_calls"], before["tool_calls"])
            self.research.tool(cap, "submit_answer", {"answer": "小草坪中档刀速耗时25分钟。[" + saved["source"] + "]"})
        self.manager.on_start = finish
        self.manager.status = Mock(return_value={"status": "completed", "events": [], "result": {}})
        self.research.execute(self.user, current)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "completed")
        self.assertEqual(len(self.data.calls), 1)
        self.assertEqual(len(self.store.list_messages(self.user, self.sid)), 2)
        self.assertEqual(self.collection.calls, [])

    def test_retry_caps_and_permanent_errors_do_not_loop(self):
        for code, allowance in (("model_stream_interrupted", 2), ("harness_execution_failed", 1),
                                ("citation_unknown", 1), ("citation_missing", 1),
                                ("model_auth_failed", 0), ("research_active_budget_reached", 0)):
            with self.subTest(code=code):
                run = self.accept(request_id=code)
                for count in range(allowance + 1):
                    run = self.inject_failure(run, code)
                    if count < allowance:
                        self.assertEqual(run["execution_status"], "recovering")
                        self.tick()
                        run = self.state.get(self.user, run["run_id"])
                    else:
                        self.assertEqual(run["status"], "blocked")
                attempts = run["attempt_number"]
                self.tick()
                self.assertEqual(self.state.get(self.user, run["run_id"])["attempt_number"], attempts)

    def test_native_final_citation_error_returns_feedback_and_repairs_without_research_replay(self):
        run = self.accept()
        saved = {}
        def read_once(payload):
            cap = self.cap(payload)
            source = self.research.tool(cap, "query_dataset", {"dataset_id": DATASET_ID, "limit": 1})
            saved.update(source=source["items"][0]["source_id"], session=cap.session_key)
        self.manager.on_start = read_once
        self.manager.outcome["result"]["answer"] = "小草坪中档刀速耗时25分钟。[src_mistyped]"
        self.research.execute(self.user, run)
        self.research._scheduled.discard(run["attempt_id"])
        current = self.state.get(self.user, run["run_id"])
        self.assertEqual(current["execution_status"], "recovering")
        self.assertEqual([m["role"] for m in self.store.list_messages(self.user, self.sid)], ["user"])
        context = self.research._resume_context(self.user, run["run_id"])
        self.assertIn("src_mistyped", context["answer_feedback"]["message"])
        self.assertIn("直接给出修正后的最终回答", context["continuation_instruction"])
        self.assertNotIn("submit_answer", context["continuation_instruction"])
        self.tick()
        current = self.state.get(self.user, run["run_id"])
        self.manager.on_start = lambda payload: self.assertEqual(self.cap(payload).session_key, saved["session"])
        self.manager.outcome["result"]["answer"] = "小草坪中档刀速耗时25分钟。[" + saved["source"] + "]"
        self.research.execute(self.user, current)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "completed")
        self.assertEqual(len(self.data.calls), 1)
        self.assertEqual(len(self.store.list_messages(self.user, self.sid)), 2)

    def test_different_citation_errors_share_one_repair_allowance(self):
        run = self.inject_failure(self.accept(), "citation_unknown")
        self.assertEqual(run["execution_status"], "recovering")
        self.tick()
        run = self.state.get(self.user, run["run_id"])
        run = self.inject_failure(run, "citation_requires_original_source")
        self.assertEqual(run["status"], "blocked")
        self.tick()
        self.assertEqual(self.state.get(self.user, run["run_id"])["attempt_number"], 2)

    def test_cleanup_or_full_queue_defers_without_losing_recovery(self):
        run = self.inject_failure(self.accept(), "harness_unavailable")
        self.manager.stop = Mock(side_effect=AgentError("cleanup_pending", "清理中", retryable=True))
        self.tick()
        self.assertEqual(self.pool.calls, [])
        self.assertIn(run["run_id"], self.research._pending)
        self.manager.stop = Mock(return_value={"status": "cancelled"})
        others = []
        for i in range(7):
            sid = self.store.create_session(self.user, str(i))["session_id"]
            others.append(self.state.accept(self.user, sid, {"question": "queue", "request_id": str(i)}, self.models.freeze({}))["run"])
        self.tick()
        self.assertEqual(self.pool.calls, [])
        self.assertEqual(self.state.get(self.user, run["run_id"])["attempt_number"], 1)
        self.state.cancel(self.user, others[0]["run_id"])
        self.tick()
        self.assertEqual(len(self.pool.calls), 1)
        self.assertEqual(self.state.get(self.user, run["run_id"])["attempt_number"], 2)

    def test_cancel_during_cleanup_wins_over_automatic_resume(self):
        run = self.inject_failure(self.accept())
        self.manager.on_stop = lambda _: self.state.cancel(self.user, run["run_id"])
        self.tick()
        self.assertEqual(self.pool.calls, [])
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "cancelled")
        self.assertEqual(self.state.get(self.user, run["run_id"])["attempt_number"], 1)

    def test_restart_recovers_active_only_with_current_authority_and_no_browser(self):
        from dataclasses import replace
        self.user = replace(self.user, scope_id="principal:" + self.user.owner_user_id, mode="authenticated", allowed_report_ids=None)
        self.sid = self.store.create_session(self.user, "restart")["session_id"]
        self.research.authority = CurrentAuthority()
        run = self.research.submit(self.user, session_id=self.sid, question="恢复", request_id="restart")["run"]
        self.pool.calls.clear(); self.research._scheduled.clear()
        self.state.claim(self.user, run["run_id"], attempt_id=run["attempt_id"])
        self.state.recover_interrupted()
        self.research._pending.clear()
        self.tick()
        self.assertEqual(len(self.pool.calls), 1)
        self.assertEqual(self.state.get(self.user, run["run_id"])["attempt_number"], 2)
        self.assertEqual(len(self.manager.starts), 0)
        self.assertIn(run["attempt_id"], self.manager.stops)

    def test_timing_uses_execution_windows_not_queue_or_recovery_wait(self):
        run = self.inject_failure(self.accept())
        before = self.state.get(self.user, run["run_id"])["timing"]["active_seconds"]
        with patch("agent.harness_broker.time.time", return_value=time.time() + 120):
            self.assertAlmostEqual(self.state.get(self.user, run["run_id"])["timing"]["active_seconds"], before, places=2)
        self.tick()
        queued = self.state.get(self.user, run["run_id"])
        self.assertEqual(queued["execution_status"], "queued")
        self.assertAlmostEqual(queued["timing"]["active_seconds"], before, places=2)
