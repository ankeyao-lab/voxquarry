"""Background work must retain ownership without retaining login credentials."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent.contracts import AgentError
from agent.tests.test_harness_research import HarnessFixture


class CurrentAuthority:
    def __init__(self):
        self.version = "authv_" + "a" * 64
        self.roles, self.calls, self.error = ["user"], [], None

    def authorize(self, principal, capability):
        self.calls.append((principal, capability))
        if self.error:
            raise self.error
        return {"principal_id": principal, "owner_scope": "principal:" + principal,
                "authorization_version": self.version, "roles": list(self.roles), "ttl_seconds": 30}


class ResearchAuthorizationTests(HarnessFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.user = replace(self.user, scope_id="principal:" + self.user.owner_user_id, mode="authenticated", allowed_report_ids=None)
        self.sid = self.store.create_session(self.user, "账号隔离测试")["session_id"]
        self.authority = self.research.authority = CurrentAuthority()
        self.research.broker.authorize = self.research._authorize_run

    def accepted(self, **kwargs):
        result = self.research.submit(self.user, session_id=self.sid, question="分析现有评论", request_id="authorized-question", **kwargs)
        self.pool.calls.clear()
        return result["run"]

    def waiting(self):
        run = self.accepted()
        self.state.checkpoint(self.user, run["run_id"], {**run["checkpoint"], "waiting_operation_ids": ["operation-a"]},
                              status="waiting_collection", attempt_id=run["attempt_id"])
        return self.state.get(self.user, run["run_id"])

    def test_authority_failure_prevents_admission_and_model_or_tool_execution(self):
        self.authority.error = AgentError("research_authorization_denied", "账号已停用", http_status=403)
        self.assert_code("research_authorization_denied", self.accepted)
        self.assertEqual(self.store.list_messages(self.user, self.sid), [])
        self.assertEqual(self.manager.starts, [])
        self.assertEqual(self.pool.calls, [])

    def test_queued_attempt_does_not_repeat_authority_requests_until_execution(self):
        run = self.accepted()
        self.authority.calls.clear()
        self.research._restore_waiting()
        self.research._restore_waiting()
        self.assertEqual(self.authority.calls, [])
        self.assertEqual(self.pool.calls, [])
        # A cached admission must not let a permission change during the queue
        # bypass the fresh check immediately before the native executor starts.
        self.authority.version = "authv_" + "b" * 64
        self.research.execute(self.user, run)
        self.assertEqual(len(self.authority.calls), 1)
        self.assertEqual(self.manager.starts, [])
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "blocked")

    def test_restart_schedules_saved_queue_once_with_current_authority(self):
        run = self.accepted()
        self.research._scheduled.clear()
        self.authority.calls.clear()
        self.research._restore_waiting()
        calls = len(self.authority.calls)
        self.assertGreater(calls, 0)
        self.assertEqual(len(self.pool.calls), 1)
        self.research._restore_waiting()
        self.assertEqual(len(self.authority.calls), calls)
        self.assertEqual(len(self.pool.calls), 1)
        self.assertEqual(self.state.get(self.user, run["run_id"])["execution_status"], "queued")

    def test_current_permission_change_invalidates_existing_run_capability(self):
        run = self.accepted()
        self.assertEqual(run["checkpoint"]["authorization_version"], self.authority.version)
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question="fixture")
        token, cap = self.research.broker.issue(self.user, run, trace["trace_id"])
        self.research.broker.check(cap)
        self.authority.version = "authv_" + "b" * 64
        self.research._authority_cache.clear()
        cap.authority_checked_at = 0
        self.assert_code("research_authority_changed", self.research.broker.resolve, "Bearer " + token)
        self.assertEqual(self.data.calls, [])

    def test_restart_restores_wait_from_fresh_account_roles_without_browser(self):
        run = self.waiting()
        # Even a changed historical role snapshot is not current permission.
        with self.store._connect() as db:
            db.execute("UPDATE research_runs SET actor_snapshot_json=replace(actor_snapshot_json, 'user\\\"', 'admin\\\"') WHERE run_id=?", (run["run_id"],))
        self.research._pending.clear()
        self.research._authority_cache.clear()
        self.research._restore_waiting()
        restored = self.research._pending[run["run_id"]]
        self.assertEqual(restored.roles, ("user",))
        self.assertEqual(restored.scope_id, self.user.scope_id)
        self.collection.ready = True
        with patch.object(self.research, "_stopped", SimpleNamespace(wait=Mock(side_effect=[False, True]), is_set=lambda: False)):
            self.research._monitor_waiting()
        self.assertEqual(len(self.pool.calls), 1)
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "running")
        self.assertIn((self.user.owner_user_id, "chat.session.own"), self.authority.calls)

    def test_revoked_or_changed_account_does_not_read_collector_on_restart(self):
        run = self.waiting()
        self.authority.version = "authv_" + "b" * 64
        with patch.object(self.research, "_stopped", SimpleNamespace(wait=Mock(side_effect=[False, True]), is_set=lambda: False)):
            self.research._monitor_waiting()
        self.assertEqual(self.research._pending, {})
        self.assertEqual(self.collection.calls, [])
        self.assertEqual(self.pool.calls, [])
        stopped = self.state.get(self.user, run["run_id"])
        self.assertEqual(stopped["status"], "blocked")
        self.assertFalse(stopped["can_resume"])
        self.assertEqual(stopped["allowed_actions"], [])
        self.authority.error = AgentError("research_authorization_denied", "账号已停用", http_status=403)
        self.assert_code("research_authorization_denied", self.research.resume, self.user, self.sid, run["run_id"], "manual", action="continue_available")

    def test_restart_preserves_report_restriction_instead_of_granting_all_reports(self):
        self.user = replace(self.user, allowed_report_ids=frozenset({"rpt_allowed_fixture"}))
        run = self.waiting()
        self.research._restore_waiting()
        restored = self.research._pending[run["run_id"]]
        self.assertEqual(restored.allowed_report_ids, frozenset({"rpt_allowed_fixture"}))
        self.assertFalse(restored.allow_archived_reports)

    def test_permission_change_after_last_tool_cannot_publish_prepared_answer(self):
        run = self.accepted()
        self.manager.on_start = self.read_and_answer
        self.manager.on_status = lambda _attempt: setattr(self.authority, "version", "authv_" + "b" * 64)
        self.research.execute(self.user, run)
        current = self.state.get(self.user, run["run_id"])
        self.assertEqual(current["status"], "blocked")
        message = self.store.list_messages(self.user, self.sid)[-1]
        self.assertEqual(message["metadata"]["error"]["code"], "research_authority_changed")
        self.assertNotIn("25 分钟", message["content"])
        self.assertEqual(message["metadata"]["citations"], [])

    def test_chat_permission_does_not_substitute_for_collection_permission(self):
        run = self.accepted(product_urls=["https://www.lowes.com/pd/test/12345"])
        self.research.egress.register_user_urls(self.user, run["run_id"])
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question="fixture")
        _token, cap = self.research.broker.issue(self.user, run, trace["trace_id"])
        original = self.authority.authorize
        def deny_collect(principal, capability):
            if capability == "collect.command":
                raise AgentError("research_authorization_denied", "无采集权限", http_status=403)
            return original(principal, capability)
        with patch.object(self.authority, "authorize", side_effect=deny_collect):
            result = self.research.tool(cap, "collect_product", {"channel": "lowes", "product_url": "https://www.lowes.com/pd/test/12345"})
        self.assertEqual(result["error"]["code"], "research_authorization_denied")
        self.assertEqual(self.collection.calls, [])

    def test_explicit_revocation_without_fresh_identity_settles_wait_once_without_admission(self):
        waiting = self.waiting()
        # This same session now has a different active question. Settling the
        # revoked wait must neither obtain an execution slot nor block this one.
        active = self.state.accept(self.user, self.sid, {"question": "新问题", "request_id": "different-question"}, waiting["settings"])["run"]
        self.authority.error = AgentError("research_authorization_denied", "已停用", http_status=403)
        self.research._restore_waiting()
        self.research._restore_waiting()
        blocked = self.state.get(self.user, waiting["run_id"])
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["attempt_id"], waiting["attempt_id"])
        self.assertEqual(blocked["attempt_number"], waiting["attempt_number"])
        self.assertFalse(blocked["can_resume"])
        self.assertEqual(self.state.get(self.user, active["run_id"])["status"], "running")
        messages = [m for m in self.store.list_messages(self.user, self.sid) if m["run_id"] == waiting["run_id"] and m["role"] == "assistant"]
        self.assertEqual(len(messages), 1)
        self.assertIn("新建问题", messages[0]["content"])
        self.assertFalse(messages[0]["metadata"]["can_resume"])
        bundle = self.state.enrich_bundle(self.user, self.store.session_bundle(self.user, self.sid))
        public = next(run for run in bundle["runs"] if run["run_id"] == waiting["run_id"])
        self.assertFalse(public["can_resume"])
        self.assertEqual(public["allowed_actions"], [])
        self.assertEqual(self.collection.calls, [])
        self.assertEqual(self.pool.calls, [])
        # An eventual re-enabled account still needs a new question; it cannot
        # bypass the persisted terminal decision through the lower-level API.
        self.authority.error = None
        self.assert_code("resume_unavailable", self.state.resume, self.user, waiting["run_id"], "resume-later")

    def test_temporary_authority_failure_preserves_wait_and_retryability(self):
        run = self.waiting()
        self.authority.error = AgentError("authority_unavailable", "暂时无法连接", http_status=503, retryable=True)
        self.research._authority_cache.clear()
        self.research._restore_waiting()
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "waiting_collection")
        self.research._pending[run["run_id"]] = self.user
        with patch.object(self.research, "_stopped", SimpleNamespace(wait=Mock(side_effect=[False, True]), is_set=lambda: False)):
            self.research._monitor_waiting()
        current = self.state.get(self.user, run["run_id"])
        self.assertEqual(current["status"], "waiting_collection")
        self.assertTrue(current["can_resume"])
        self.assertNotIn("resume_disabled", current["checkpoint"])
        self.assertIn(run["run_id"], self.research._pending)
        self.assertEqual(len(self.store.list_messages(self.user, self.sid)), 1)
        self.assertEqual(self.collection.calls, [])

    def test_active_monitor_revocation_settles_report_wait_without_querying_report(self):
        run = self.accepted()
        self.state.checkpoint(self.user, run["run_id"], {**run["checkpoint"], "report_job": {"job": {"job_id": "private-job"}, "job_token": "private-token"}},
                              status="waiting_data", attempt_id=run["attempt_id"])
        self.research._pending[run["run_id"]] = self.user
        self.research._authority_cache.clear()
        self.authority.version = "authv_" + "b" * 64
        with patch.object(self.research, "_stopped", SimpleNamespace(wait=Mock(side_effect=[False, True]), is_set=lambda: False)):
            self.research._monitor_waiting()
        self.assertEqual(self.state.get(self.user, run["run_id"])["status"], "blocked")
        self.assertFalse(self.state.get(self.user, run["run_id"])["can_resume"])
        self.assertNotIn(run["run_id"], self.research._pending)
        self.assertNotIn("private-token", self.store.list_messages(self.user, self.sid)[-1]["content"])
        self.assertEqual(self.pool.calls, [])

    def test_old_waiting_snapshot_cannot_block_new_attempt_after_user_resume(self):
        waiting = self.waiting()
        self.state.cancel(self.user, waiting["run_id"])
        resumed = self.state.resume(self.user, waiting["run_id"])["run"]
        error = AgentError("research_authority_changed", "权限变化", http_status=403)
        self.assertFalse(self.research._block_waiting_authority(self.user, waiting, error))
        current = self.state.get(self.user, waiting["run_id"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["attempt_id"], resumed["attempt_id"])
        self.assertNotIn("resume_disabled", current["checkpoint"])


if __name__ == "__main__":
    unittest.main()
