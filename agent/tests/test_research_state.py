from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agent.contracts import AgentError
from agent.model_registry import ModelRegistry
from agent.research_state import ResearchState
from agent.store import WenquanStore
from agent.tests.test_agent_trace import actor


class ResearchStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WenquanStore(Path(self.temp.name) / "wenquan.sqlite3")
        self.store.initialize()
        self.registry = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic-tests-only"})
        self.state = ResearchState(self.store, self.registry)
        self.state.initialize()
        self.user = actor()
        self.sid = self.store.create_session(self.user, "私有研究")["session_id"]
        self.request = {"request_id": "req1", "question": "草种甲和乙的表现有什么不同？", "report_ids": ["rpt1"], "attachment_ids": ["att1"], "web_enabled": False}
        self.frozen = self.registry.freeze({"web_enabled": False})

    def tearDown(self):
        self.temp.cleanup()

    def accept(self, **kwargs):
        return self.state.accept(self.user, self.sid, kwargs.get("request", self.request), kwargs.get("frozen", self.frozen))

    def assert_code(self, code, method, *args, **kwargs):
        with self.assertRaises(AgentError) as caught:
            method(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_identity_reader_does_not_take_write_lock_and_observes_committed_changes(self):
        accepted = self.accept()
        run_id = accepted["run"]["run_id"]
        statements = []
        connect = self.store._connect
        def traced_connection(**kwargs):
            db = connect(**kwargs)
            db.set_trace_callback(statements.append)
            return db
        writer = connect()
        try:
            writer.execute("BEGIN IMMEDIATE")
            with patch.object(self.store, "_connect", side_effect=traced_connection) as factory:
                with self.state.identity_reader() as read:
                    self.assertEqual(read(self.user, run_id)["status"], "running")
                    self.assertEqual(read(self.user, run_id)["attempt_id"], accepted["attempt_id"])
                    self.assert_code("run_not_found", read, actor("someone-else"), run_id)
                    factory.assert_called_once()
                self.assertFalse(any(sql.startswith("BEGIN") for sql in statements))
                self.assertIn("PRAGMA query_only=ON", statements)
        finally:
            writer.rollback()
            writer.close()
        with self.state.identity_reader() as read:
            self.state.cancel(self.user, run_id)
            self.assertEqual(read(self.user, run_id)["status"], "cancelled")
            resumed = self.state.resume(self.user, run_id)
            self.assertEqual(read(self.user, run_id)["attempt_id"], resumed["attempt_id"])
            changed = {**self.frozen, "effort": "low"}
            with self.state._connect() as db:
                db.execute("UPDATE research_runs SET settings_json=? WHERE run_id=?", (json.dumps(changed), run_id))
            self.assertEqual(read(self.user, run_id)["settings"], changed)

    def test_identity_reader_can_move_between_workers_without_caching_cancellation(self):
        run = self.accept()["run"]
        manager = self.state.identity_reader()
        with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1) as second:
            ordinary = self.store._connect()
            try:
                with self.assertRaises(sqlite3.ProgrammingError):
                    first.submit(ordinary.execute, "SELECT 1").result()
            finally:
                ordinary.close()
            read = first.submit(manager.__enter__).result()
            try:
                first_id = first.submit(threading.get_ident).result()
                second_id = second.submit(threading.get_ident).result()
                self.assertNotEqual(first_id, second_id)
                self.assertEqual(first.submit(read, self.user, run["run_id"]).result()["status"], "running")
                self.state.cancel(self.user, run["run_id"])
                self.assertEqual(second.submit(read, self.user, run["run_id"]).result()["status"], "cancelled")
            finally:
                second.submit(manager.__exit__, None, None, None).result()

    def test_batch_activity_and_queue_share_counts_without_leaking_other_users(self):
        self.assertFalse(self.state.settings(self.user, self.sid)['web_enabled'])
        first = self.accept()
        sid2 = self.store.create_session(self.user, '排队一')['session_id']
        second = self.state.accept(self.user, sid2, self.request, self.frozen)
        sid3 = self.store.create_session(self.user, '排队二')['session_id']
        third = self.state.accept(self.user, sid3, self.request, self.frozen)
        for r in [second, third]:
            self.state.enqueue(self.user, r['run']['run_id'], attempt_id=r['attempt_id'])
        activity = self.state.session_activity(self.user, [self.sid,sid2,sid3])
        self.assertEqual(activity[sid2]['queue_ahead'],1)
        self.assertEqual(activity[sid3]['queue_ahead'],2)
        detail=self.state.get(self.user,third['run']['run_id'])
        self.assertEqual(detail['queue_ahead'],2)
        self.assertTrue(detail['queued_at'])
        before=activity[self.sid]['activity_version']
        self.store.update_session(self.user,self.sid,title='改标题不算更新')
        self.assertEqual(self.state.session_activity(self.user,[self.sid])[self.sid]['activity_version'],before)
        self.state.event(self.user,first['run']['run_id'],'资料已就绪','completed',tool_name='read_research_reference',attempt_id=first['attempt_id'])
        self.assertNotEqual(self.state.session_activity(self.user,[self.sid])[self.sid]['activity_version'],before)
        other=self.state.session_activity(actor('someone-else'),[self.sid])[self.sid]
        self.assertEqual(other['execution_status'],'idle')
        self.assertNotIn(first['run']['run_id'],str(other))
        self.state.cancel(self.user,second['run']['run_id'])
        self.assertEqual(self.state.get(self.user,third['run']['run_id'])['queue_ahead'],1)
        self.state.finish(self.user,first['run']['run_id'],'完成',attempt_id=first['attempt_id'])
        self.assertEqual(self.state.get(self.user,third['run']['run_id'])['queue_ahead'],0)
        self.state.claim(self.user,third['run']['run_id'],attempt_id=third['attempt_id'])
        self.assertEqual(self.state.session_activity(self.user,[sid3])[sid3]['execution_status'],'running')

    def test_session_settings_are_private_validated_and_model_switch_defaults_are_valid(self):
        self.assertEqual(self.state.settings(self.user, self.sid)["model_key"], "deepseek-v4.1-flash")
        saved = self.state.update_settings(self.user, self.sid, {"effort": "max", "web_enabled": False})
        self.assertFalse(saved["web_enabled"])
        switched = self.state.update_settings(self.user, self.sid, {"model_key": "qwen3.8-max"})
        self.assertEqual(switched["effort"], "default")
        self.assert_code("invalid_request", self.state.update_settings, self.user, self.sid, {"effort": "max"})
        self.assert_code("invalid_request", self.state.update_settings, self.user, self.sid, {"owner_user_id": "other"})
        self.assert_code("session_not_found", self.state.settings, actor("admin", roles=("admin",)), self.sid)

    def test_retired_saved_effort_is_read_compatibly_without_a_migration(self):
        import json
        saved = {"model_key": "qwen3.8-flash", "effort": "medium", "thinking_enabled": True, "web_enabled": False,
                 "effort_by_model": {"qwen3.8-flash": "medium", "glm-5.3": "high"}}
        with self.state._connect() as db:
            db.execute("INSERT INTO research_session_settings VALUES(?,?,?,?,?)", (self.sid, self.user.owner_user_id, self.user.scope_id, json.dumps(saved), "2026-09-20T00:00:00Z"))
        self.assertEqual(self.state.settings(self.user, self.sid)["effort"], "default")
        with self.state._connect() as db:
            self.assertEqual(json.loads(db.execute("SELECT settings_json FROM research_session_settings WHERE session_id=?", (self.sid,)).fetchone()[0]), saved)
        self.assertEqual(self.state.update_settings(self.user, self.sid, {"web_enabled": True})["effort"], "default")
        self.assertEqual(self.state.update_settings(self.user, self.sid, {"model_key": "glm-5.3"})["effort"], "high")
        self.assert_code("invalid_request", self.state.update_settings, self.user, self.sid, {"model_key": "qwen3.8-flash", "effort": "medium"})

    def test_accept_atomic_idempotent_and_covers_entire_request_and_frozen_configuration(self):
        accepted = self.accept()
        self.assertTrue(accepted["created"])
        duplicate = self.accept()
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["attempt_id"], accepted["attempt_id"])
        bundle = self.store.session_bundle(self.user, self.sid)
        self.assertEqual(len(bundle["messages"]), 1)
        self.assertEqual(len(bundle["runs"]), 1)
        self.assertEqual(bundle["runs"][0]["intent"], "harness_research")
        for field, value in (("attachment_ids", ["att2"]), ("report_ids", ["rpt2"]), ("question", "不同的问题"), ("web_enabled", True)):
            self.assert_code("request_id_conflict", self.accept, request={**self.request, field: value})
        self.assert_code("request_id_conflict", self.accept, frozen={**self.frozen, "model_id": "changed-id"})

    def test_two_instances_concurrent_accept_create_one_run_and_message(self):
        other = ResearchState(self.store, self.registry)
        other.initialize()
        def submit(index):
            state = self.state if index % 2 else other
            return state.accept(self.user, self.sid, self.request, self.frozen)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(12)))
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(len({result["run"]["run_id"] for result in results}), 1)
        self.assertEqual(len(self.store.list_messages(self.user, self.sid)), 1)

    def test_request_and_model_settings_are_immutable_after_accept(self):
        original = copy.deepcopy(self.request)
        accepted = self.accept()
        run_id = accepted["run"]["run_id"]
        self.request["attachment_ids"].append("later")
        accepted["run"]["settings"]["model_id"] = "tampered-result"
        self.state.update_settings(self.user, self.sid, {"model_key": "glm-5.3", "effort": "high"})
        run = self.state.get(self.user, run_id)
        self.assertEqual(run["request"], original)
        self.assertEqual(run["settings"], self.frozen)
        enriched = self.state.enrich_bundle(self.user, self.store.session_bundle(self.user, self.sid))
        self.assertEqual(enriched["session"]["settings"]["model_key"], "glm-5.3")
        self.assertEqual(enriched["runs"][0]["settings"]["model_key"], "deepseek-v4.1-flash")
        self.assertNotIn("actor_snapshot", str(run))
        self.assertNotIn("actor_snapshot", str(enriched))

    def test_all_private_access_rejects_other_owner_admin_or_session(self):
        accepted = self.accept()
        run_id, aid = accepted["run"]["run_id"], accepted["attempt_id"]
        source = self.state.add_source(self.user, run_id, {"title": "原始资料", "quote": "草种甲表现好"}, attempt_id=aid)
        other = actor("admin", roles=("admin",))
        self.assert_code("run_not_found", self.state.get, other, run_id)
        self.assert_code("run_not_found", self.state.list_sources, other, run_id)
        self.assert_code("run_not_found", self.state.cancel, other, run_id)
        self.assert_code("run_not_found", self.state.finish, other, run_id, "steal", attempt_id=aid)
        self.assert_code("session_not_found", self.state.source, other, self.sid, source["source_id"])
        other_session = self.store.create_session(self.user, "another")["session_id"]
        self.assert_code("source_not_found", self.state.source, self.user, other_session, source["source_id"])

    def test_waiting_finish_is_internal_block_only_and_never_bypasses_owner_or_attempt(self):
        accepted = self.accept()
        run_id, aid = accepted["run"]["run_id"], accepted["attempt_id"]
        self.state.checkpoint(self.user, run_id, {"original_note": "保留资料"}, status="waiting_collection", attempt_id=aid)
        self.assert_code("research_not_running", self.state.finish, self.user, run_id, "未检查答案", attempt_id=aid)
        self.assert_code("invalid_request", self.state.finish, self.user, run_id, "未检查答案", attempt_id=aid, allow_waiting=True)
        self.assert_code("run_not_found", self.state.finish, actor("other", roles=("admin",)), run_id, "blocked", status="blocked", attempt_id=aid, allow_waiting=True)
        stopped = self.state.finish(self.user, run_id, "账号权限已变化，请新建问题", status="blocked", attempt_id=aid, allow_waiting=True,
            metadata={"resume_disabled": True, "error": {"code": "research_authority_changed"}})
        self.assertFalse(stopped["can_resume"])
        self.assertEqual(stopped["checkpoint"]["original_note"], "保留资料")
        self.assert_code("resume_unavailable", self.state.resume, self.user, run_id)

    def test_progress_start_and_finish_share_step_and_sequence_does_not_duplicate(self):
        accepted = self.accept()
        run_id, aid = accepted["run"]["run_id"], accepted["attempt_id"]
        first = self.state.event(self.user, run_id, "读取原始评论", "running", tool_name="reviews", event_id="tool1", detail={"summary": "第一批", "api_key": "private-fixture"}, attempt_id=aid)
        done = self.state.event(self.user, run_id, "读取原始评论", "completed", tool_name="reviews", event_id="tool1", detail={"summary": "已读取24条"}, attempt_id=aid)
        self.assertEqual(first["step_id"], done["step_id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["input_summary"]["detail"], "已读取24条")
        self.assertNotIn("private-fixture", str(first))
        self.assertEqual(len(self.state.get(self.user, run_id)["steps"]), 1)
        late = self.state.event(self.user, run_id, "旧进度", "running", event_id="tool1", attempt_id=aid)
        self.assertEqual(late["status"], "completed")

    def test_checkpoint_waiting_survives_startup_recovery_and_resume_preserves_source(self):
        accepted = self.accept()
        run_id, aid = accepted["run"]["run_id"], accepted["attempt_id"]
        source = self.state.add_source(self.user, run_id, {"quote": "合成引用", "snapshot": "snap1"}, attempt_id=aid)
        duplicate = self.state.add_source(self.user, run_id, {"snapshot": "snap1", "quote": "合成引用"}, attempt_id=aid)
        self.assertEqual(source, duplicate)
        checkpoint = {"collection_id": "operation1", "harness_session": {"messages": [{"role": "user", "content": "stored original"}]}}
        waiting = self.state.checkpoint(self.user, run_id, checkpoint, attempt_id=aid)
        self.assertEqual(waiting["status"], "waiting_collection")
        self.assertEqual(self.state.recover_interrupted()["interrupted_runs"], 0)
        pending = self.state.pending_runs()
        self.assertEqual(pending[0]["attempt_id"], aid)
        self.assertTrue(pending[0]["requires_identity_revalidation"])
        self.assertEqual(pending[0]["actor_snapshot"]["owner_user_id"], self.user.owner_user_id)
        resumed = self.state.resume(self.user, run_id)
        self.assertNotEqual(resumed["attempt_id"], aid)
        self.assertEqual(resumed["run"]["checkpoint"], checkpoint)
        self.assertEqual(resumed["run"]["request"], self.request)
        self.assertEqual(self.state.list_sources(self.user, run_id), [source])
        self.assertEqual(len(self.store.list_messages(self.user, self.sid)), 1)

    def test_cancel_resume_rejects_late_finish_checkpoint_event_source_and_trace(self):
        accepted = self.accept()
        run_id, aid = accepted["run"]["run_id"], accepted["attempt_id"]
        self.state.cancel(self.user, run_id)
        self.assert_code("research_cancelled", self.state.finish, self.user, run_id, "late", attempt_id=aid)
        resumed = self.state.resume(self.user, run_id)
        for method, args, kwargs in ((self.state.finish, ("late",), {}),
             (self.state.checkpoint, ({"stale": True},), {}),
             (self.state.event, ("late", "completed"), {}),
             (self.state.add_source, ({"quote": "stale"},), {}),
             (self.state.attach_trace, ("trace_old",), {})):
            self.assert_code("research_attempt_stale", method, self.user, run_id, *args, attempt_id=aid, **kwargs)
        self.assertEqual(len(self.store.list_messages(self.user, self.sid)), 1)
        self.assertEqual(self.state.get(self.user, run_id)["attempt_id"], resumed["attempt_id"])

    def test_finish_is_atomic_idempotent_and_cannot_change_a_completed_answer(self):
        accepted = self.accept()
        run_id, aid = accepted["run"]["run_id"], accepted["attempt_id"]
        self.state.attach_trace(self.user, run_id, "trace1", attempt_id=aid)
        self.state.finish(self.user, run_id, "保留原文条件的回答", metadata={"citations": []}, attempt_id=aid)
        self.state.finish(self.user, run_id, "保留原文条件的回答", metadata={"citations": []}, attempt_id=aid)
        self.assert_code("research_finish_conflict", self.state.finish, self.user, run_id, "different", attempt_id=aid)
        bundle = self.store.session_bundle(self.user, self.sid)
        self.assertEqual(bundle["runs"][0]["status"], "completed")
        self.assertEqual(len(bundle["messages"]), 2)
        self.assertEqual(bundle["messages"][1]["metadata"]["trace_id"], "trace1")
        self.assertEqual(self.state.cancel(self.user, run_id)["status"], "completed")
        self.assert_code("resume_unavailable", self.state.resume, self.user, run_id)

    def test_recover_defers_interrupted_harness_only_without_deleting_any_messages(self):
        accepted = self.accept()
        run_id = accepted["run"]["run_id"]
        legacy = self.store.create_run(self.user, self.sid, intent="legacy", plan=[], status="running")
        self.state.event(self.user, run_id, "正在分析", "running", attempt_id=accepted["attempt_id"])
        self.assertEqual(self.state.recover_interrupted()["interrupted_runs"], 1)
        interrupted = self.state.get(self.user, run_id)
        self.assertEqual(interrupted["execution_status"], "recovering")
        self.assertFalse(interrupted["can_resume"])
        self.assertEqual(interrupted["steps"][0]["status"], "blocked")
        self.assertEqual(self.store.get_run(self.user, legacy["run_id"])["status"], "running")
        self.assertEqual(len(self.store.list_messages(self.user, self.sid)), 1)

    def test_concurrent_resume_has_one_new_attempt_and_caller_cannot_inject_source_identity(self):
        accepted = self.accept()
        run_id = accepted["run"]["run_id"]
        self.state.cancel(self.user, run_id)
        def resume(_index):
            try:
                return self.state.resume(self.user, run_id)
            except AgentError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(resume, range(4)))
        self.assertEqual(sum(isinstance(value, dict) for value in values), 1)
        current = self.state.get(self.user, run_id)
        self.assertEqual(current["attempt_number"], 2)
        self.assert_code("invalid_request", self.state.add_source, self.user, run_id, {"source_id": "src_forged"}, attempt_id=current["attempt_id"])

    def test_same_session_legacy_or_harness_running_blocks_new_accept_but_waiting_does_not(self):
        accepted = self.accept()
        self.assert_code("research_busy", self.accept, request={**self.request, "request_id": "req2"})
        self.state.checkpoint(self.user, accepted["run"]["run_id"], {"operation": "pending"}, attempt_id=accepted["attempt_id"])
        second = self.accept(request={**self.request, "request_id": "req2"})
        self.assertTrue(second["created"])
        self.assert_code("research_busy", self.state.resume, self.user, accepted["run"]["run_id"])
        legacy_sid = self.store.create_session(self.user, "legacy")["session_id"]
        self.store.create_run(self.user, legacy_sid, intent="legacy", plan=[], status="running")
        self.assert_code("research_busy", self.state.accept, self.user, legacy_sid, self.request, self.frozen)

    def test_owner_and_global_running_limits_and_archived_admission(self):
        self.accept()
        second_sid = self.store.create_session(self.user, "second")["session_id"]
        self.state.accept(self.user, second_sid, self.request, self.frozen)
        third_sid = self.store.create_session(self.user, "third")["session_id"]
        self.assertTrue(self.state.accept(self.user, third_sid, self.request, self.frozen)["created"])
        for index in range(4):
            other = actor("other" + str(index))
            sid = self.store.create_session(other, "shared capacity")["session_id"]
            self.state.accept(other, sid, self.request, self.frozen)
        other = actor("last")
        sid = self.store.create_session(other, "full")["session_id"]
        self.assert_code("research_capacity_exceeded", self.state.accept, other, sid, self.request, self.frozen)
        self.store.update_session(other, sid, archived=True)
        self.assert_code("session_archived", self.state.accept, other, sid, self.request, self.frozen)

    def test_queue_survives_restart_but_active_attempt_requires_resume(self):
        accepted = self.accept()
        run_id, attempt = accepted["run"]["run_id"], accepted["attempt_id"]
        self.state.enqueue(self.user, run_id, attempt_id=attempt)
        self.state.checkpoint(self.user, run_id, {"authorization_version": "v1"}, status="running", attempt_id=attempt)
        self.assertEqual(self.state.get(self.user, run_id)["execution_status"], "queued")
        self.assertEqual(self.state.get(self.user, run_id)["queue_position"], 1)
        self.assertEqual(self.state.recover_interrupted()["interrupted_runs"], 0)
        self.assertEqual(self.state.pending_runs()[0]["execution_status"], "queued")
        self.state.claim(self.user, run_id, attempt_id=attempt)
        self.assertEqual(self.state.get(self.user, run_id)["execution_status"], "running")
        self.assertEqual(self.state.recover_interrupted()["interrupted_runs"], 1)

    def test_resume_request_id_is_durable_and_waiting_actions_use_frontend_list(self):
        accepted = self.accept()
        run_id = accepted["run"]["run_id"]
        waiting = self.state.checkpoint(self.user, run_id, {"operation": "pending"}, attempt_id=accepted["attempt_id"])
        self.assertEqual(waiting["allowed_actions"], [{"action": "continue_available", "label": "继续用现有资料分析"}])
        first = self.state.resume(self.user, run_id, request_id="resume1")
        second = self.state.resume(self.user, run_id, request_id="resume1")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(second["run"]["attempt_number"], 2)


if __name__ == "__main__":
    unittest.main()
