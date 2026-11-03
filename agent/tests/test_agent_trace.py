from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent.agent_trace import AgentTraceStore, sanitize_log_value
from agent.contracts import AgentError, IdentityContext


def actor(owner="user-a", *, actor_id=None, roles=()):
    return IdentityContext(actor_id=actor_id or owner, owner_user_id=owner, scope_id="shared-data",
                           authority_id="test-gateway", roles=roles, mode="synthetic")


class AgentTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1000.0
        self.store = self.make_store()
        self.user = actor()

    def tearDown(self):
        self.temp.cleanup()

    def make_store(self, **kwargs):
        store = AgentTraceStore(self.root / "trace" / "trace.sqlite3", clock=lambda: self.now, **kwargs)
        store.initialize()
        return store

    def start(self, **kwargs):
        return self.store.start_trace(self.user, session_id="session-a", run_id="run-a", question="为什么厚草时留下草条？", **kwargs)

    def assert_code(self, code, method, *args, **kwargs):
        with self.assertRaises(AgentError) as result:
            method(*args, **kwargs)
        self.assertEqual(result.exception.code, code)

    def test_owner_boundary_not_actor_or_admin_role_and_no_leak_in_lists(self):
        trace = self.start()
        other = actor("user-b", roles=("admin",))
        for method, args in ((self.store.get_trace, (trace["trace_id"],)),
                             (self.store.list_events, (trace["trace_id"],)),
                             (self.store.read_payload, (trace["trace_id"], "pay_missing"))):
            self.assert_code("trace_not_found", method, other, *args)
        self.assert_code("trace_not_found", self.store.append_event, other, trace["trace_id"], event_id="other", event_type="model.started")
        self.assert_code("trace_not_found", self.store.record_feedback, other, trace["trace_id"], feedback_id="f", rating="helpful")
        self.assertEqual(self.store.list_traces(other)["items"], [])
        same_owner = actor(actor_id="a-later-login", roles=("admin",))
        self.assertEqual(self.store.get_trace(same_owner, trace["trace_id"])["run_id"], "run-a")

    def test_default_keeps_fingerprints_and_structured_metadata_without_full_text(self):
        trace = self.start(model_config={"requested_model": "model-alias", "effort": "high"},
                           data_manifest=[{"snapshot_id": "snapshot-1", "sha256": "a" * 64}])
        event = self.store.append_event(self.user, trace["trace_id"], event_id="call1", event_type="model.finished", attempt_id="attempt1",
            data={"actual_model": "fixed-model-v1", "effort": "high", "usage": {"prompt_tokens": 23, "completion_tokens": 9}, "elapsed_ms": 100,
                  "messages": [{"role": "user", "content": "secret-review-text"}]}, payload={"messages": [{"content": "secret-review-text"}]})
        final = self.store.finish_trace(self.user, trace["trace_id"], status="completed", answer="只在厚草出现，并非整块草坪。")
        self.assertEqual(event["payload_status"], "disabled")
        self.assertEqual(event["data"]["actual_model"], "fixed-model-v1")
        self.assertEqual(event["data"]["usage"]["prompt_tokens"], 23)
        self.assertEqual(final["latest_attempt_id"], "attempt1")
        self.assertEqual(final["final_status"], "completed")
        self.assertEqual(final["answer"]["content"], "omitted")
        self.assertNotIn("secret-review-text", self.store.database_path.read_bytes().decode("latin-1"))
        self.assertEqual(list(self.store.payload_dir.iterdir()), [])

    def test_recursive_redaction_including_urls_and_reasoning_preserves_usage(self):
        value = {"api_key": "fixture-api-secret", "nested": [{"PASSWORD": "fixture-password", "X-Api-Key": "fixture-x-key",
                 "url": "https://user:fixture-url-pass@example.test/v1?token=fixture-query-secret#fixture-fragment",
                 "summary": "failed Authorization=fixture-header-secret Bearer fixture-bearer-secret",
                 "reasoning_content": "private-chain", "unknown": {"reasoningContent": "private-chain2"}}],
                 "usage": {"prompt_tokens": 5, "reasoning_tokens": 8}, "model_config": {"max_tokens": 1234}}
        encoded = json.dumps(sanitize_log_value(value, include_content=True))
        for secret in ("fixture-api-secret", "fixture-password", "fixture-x-key", "fixture-url-pass", "fixture-query-secret", "fixture-fragment", "fixture-header-secret", "fixture-bearer-secret", "private-chain"):
            self.assertNotIn(secret, encoded)
        self.assertIn("example.test/v1", encoded)
        self.assertEqual(sanitize_log_value(value)["usage"], {"prompt_tokens": 5, "reasoning_tokens": 8})

    def test_free_text_auth_json_and_unknown_reasoning_fields_are_removed(self):
        clean = sanitize_log_value({"summary": 'Authorization: Bearer credential-one\nCookie: session=credential-two; csrf=credential-three\n"api_key": "credential-four"',
            "unknown_reasoning_content": "hidden reasoning", "reasoning_effort": "high", "enable_thinking": True}, include_content=True)
        encoded = json.dumps(clean)
        for number in ("one", "two", "three", "four"):
            self.assertNotIn("credential-" + number, encoded)
        self.assertNotIn("hidden reasoning", encoded)
        self.assertEqual(clean["reasoning_effort"], "high")
        self.assertTrue(clean["enable_thinking"])

    def test_enabled_payload_is_private_bounded_opt_in_and_sanitized_before_writing(self):
        self.store = self.make_store(content_enabled=True, max_payload_bytes=512)
        trace = self.start()
        event = self.store.append_event(self.user, trace["trace_id"], event_id="raw", event_type="model.finished",
                   payload={"content": "A synthetic answer", "request_headers": {"Authorization": "fixture-secret"},
                            "reasoning_content": "private reasoning"})
        self.assert_code("trace_content_opt_in_required", self.store.read_payload, self.user, trace["trace_id"], event["payload_ref"])
        payload = self.store.read_payload(self.user, trace["trace_id"], event["payload_ref"], include_content=True)
        self.assertEqual(payload["data"]["content"], "A synthetic answer")
        self.assertNotIn("fixture-secret", json.dumps(payload))
        self.assertNotIn("private reasoning", json.dumps(payload))
        for path in self.store.payload_dir.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(b"fixture-secret", path.read_bytes())
        self.assertEqual(stat.S_IMODE(self.store.payload_dir.stat().st_mode), 0o700)
        oversized = self.store.append_event(self.user, trace["trace_id"], event_id="huge", event_type="model.finished", payload={"content": "x" * 2000})
        self.assertEqual(oversized["payload_status"], "omitted_size_limit")
        self.assertIsNone(oversized["payload_ref"])

    def test_event_idempotency_and_conflicts_are_atomic(self):
        trace = self.start()
        self.assertEqual(self.start()["trace_id"], trace["trace_id"])
        kwargs = {"event_id": "tool1", "event_type": "tool.started", "data": {"tool": "reviews", "args": {"offset": 0}}}
        one = self.store.append_event(self.user, trace["trace_id"], **kwargs)
        self.now += 4
        self.assertEqual(self.store.append_event(self.user, trace["trace_id"], **kwargs), one)
        self.assert_code("trace_event_conflict", self.store.append_event, self.user, trace["trace_id"], **{**kwargs, "data": {"offset": 24}})
        self.assertEqual(self.store.get_trace(self.user, trace["trace_id"])["last_seq"], 2)
        self.assert_code("trace_conflict", self.store.start_trace, self.user, session_id="session-a", run_id="run-a", question="different")

    def test_two_store_instances_concurrent_events_have_contiguous_sequence(self):
        trace = self.start()
        second = self.make_store()
        def append(index):
            store = self.store if index % 2 else second
            return store.append_event(self.user, trace["trace_id"], event_id="event" + str(index), event_type="tool.finished", data={"record_count": index})
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append, range(40)))
        page = self.store.list_events(self.user, trace["trace_id"], limit=100)
        self.assertEqual([row["seq"] for row in page["items"]], list(range(1, 42)))
        first_page = self.store.list_events(self.user, trace["trace_id"], limit=20)
        self.assertEqual(first_page["next_after_seq"], 20)

    def test_reopening_store_preserves_revision_and_checks_full_saved_payload_identity(self):
        self.store = self.make_store(content_enabled=True)
        trace = self.start()
        payload = {"items": [{"value": index} for index in range(101)]}
        self.store.append_event(self.user, trace["trace_id"], event_id="p", event_type="tool.finished", payload=payload)
        reopened = self.make_store(content_enabled=True)
        self.assertEqual(reopened.get_trace(self.user, trace["trace_id"])["last_seq"], 2)
        payload["items"][100]["value"] = 999
        self.assert_code("trace_event_conflict", reopened.append_event, self.user, trace["trace_id"], event_id="p", event_type="tool.finished", payload=payload)

    def test_artifact_tampering_and_missing_file_do_not_return_unverified_content(self):
        self.store = self.make_store(content_enabled=True)
        trace = self.start()
        ref = trace["question"]["payload_ref"]
        path = self.store.payload_dir / (ref + ".json")
        path.write_text('{"question":"tampered"}')
        self.assert_code("trace_payload_changed", self.store.read_payload, self.user, trace["trace_id"], ref, include_content=True)
        path.unlink()
        self.assert_code("trace_payload_unavailable", self.store.read_payload, self.user, trace["trace_id"], ref, include_content=True)

    def test_cancelled_trace_keeps_late_results_separate_and_does_not_publish_them(self):
        trace = self.start()
        cancelled = self.store.cancel_trace(self.user, trace["trace_id"])
        self.assertEqual(cancelled["final_status"], "cancelled")
        event = self.store.append_event(self.user, trace["trace_id"], event_id="late", event_type="model.finished", attempt_id="late-attempt", data={"actual_model": "model1"})
        self.assertTrue(event["late_after_cancel"])
        late_finish = self.store.finish_trace(self.user, trace["trace_id"], status="completed", answer="Must not become final", event_id="late-finish")
        self.assertEqual(late_finish["final_status"], "cancelled")
        self.assertIsNone(late_finish["latest_attempt_id"])
        self.assertEqual(late_finish["answer"], cancelled["answer"])

    def test_resume_accepts_new_attempt_and_old_results_cannot_finish_or_cancel_it(self):
        trace = self.start()
        self.store.append_event(self.user, trace["trace_id"], event_id="first-call", event_type="model.started", attempt_id="attempt1")
        self.store.cancel_trace(self.user, trace["trace_id"], attempt_id="attempt1")
        resumed = self.store.resume_trace(self.user, trace["trace_id"], attempt_id="attempt2")
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(resumed["latest_attempt_id"], "attempt2")
        self.assertEqual(self.store.resume_trace(self.user, trace["trace_id"], attempt_id="attempt2"), resumed)
        late = self.store.append_event(self.user, trace["trace_id"], event_id="old-reply", event_type="model.finished", attempt_id="attempt1")
        self.assertEqual(late["disposition"], "late_attempt")
        duplicate_old_cancel = self.store.cancel_trace(self.user, trace["trace_id"], attempt_id="attempt1")
        self.assertEqual(duplicate_old_cancel["status"], "running")
        ignored = self.store.finish_trace(self.user, trace["trace_id"], status="completed", answer="old result", attempt_id="attempt1", event_id="old-finish")
        self.assertEqual(ignored["status"], "running")
        finished = self.store.finish_trace(self.user, trace["trace_id"], status="completed", answer="new result", attempt_id="attempt2")
        self.assertEqual(finished["answer"]["attempt_id"], "attempt2")
        self.assertEqual(finished["status"], "completed")

    def test_retention_blocks_reads_before_cleanup_and_deletes_artifacts(self):
        self.store = self.make_store(content_enabled=True, payload_retention_seconds=10, trace_retention_seconds=30)
        trace = self.start()
        ref = trace["question"]["payload_ref"]
        self.now += 11
        self.assert_code("trace_payload_unavailable", self.store.read_payload, self.user, trace["trace_id"], ref, include_content=True)
        self.assertEqual(self.store.purge_expired()["payloads_deleted"], 1)
        self.assertEqual(list(self.store.payload_dir.iterdir()), [])
        self.assertEqual(self.store.get_trace(self.user, trace["trace_id"])["run_id"], "run-a")
        self.now += 20
        self.assertEqual(self.store.list_traces(self.user)["items"], [])
        self.assert_code("trace_not_found", self.store.get_trace, self.user, trace["trace_id"])
        self.assertEqual(self.store.purge_expired()["traces_deleted"], 1)

    def test_no_private_content_or_large_values_in_summary_and_cycle_is_bounded(self):
        cyclic = {"response": "x" * 10000, "answer": "private-answer"}
        cyclic["self"] = cyclic
        clean = sanitize_log_value(cyclic)
        self.assertLess(len(json.dumps(clean)), 1000)
        self.assertNotIn("private-answer", json.dumps(clean))
        self.assertIn("cycle", clean["self"])

    def test_feedback_is_explicit_user_submission_idempotent_and_owner_bound(self):
        trace = self.start()
        self.store.append_event(self.user, trace["trace_id"], event_id="model-review", event_type="model.review", data={"rating": "helpful"})
        feedback = self.store.record_feedback(self.user, trace["trace_id"], feedback_id="feedback-1", rating="unhelpful", labels=["wrong_condition"], comment="草种说反了")
        duplicate = self.store.record_feedback(self.user, trace["trace_id"], feedback_id="feedback-1", rating="unhelpful", labels=["wrong_condition"], comment="草种说反了")
        self.assertEqual(feedback, duplicate)
        recorded = self.store.get_trace(self.user, trace["trace_id"])["human_feedback"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["provenance"], "user_submitted")
        self.assertEqual(recorded[0]["rating"], "unhelpful")

    def test_storage_rejects_shared_directory_and_symlink_payload(self):
        shared = self.root / "shared"
        shared.mkdir(mode=0o755)
        os.chmod(shared, 0o755)
        self.assert_code("trace_storage_invalid", AgentTraceStore(shared / "trace.sqlite3").initialize)
        self.store = self.make_store(content_enabled=True)
        trace = self.start()
        ref = trace["question"]["payload_ref"]
        path = self.store.payload_dir / (ref + ".json")
        path.unlink()
        outside = self.root / "other.json"
        outside.write_text("{}")
        path.symlink_to(outside)
        self.assert_code("trace_storage_invalid", self.store.read_payload, self.user, trace["trace_id"], ref, include_content=True)


if __name__ == "__main__":
    unittest.main()
