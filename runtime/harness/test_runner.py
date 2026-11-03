"""Committed event forwarding must be bounded and never leak on failure."""
import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import runner


class RunnerTrajectoryTests(unittest.TestCase):
    def test_archive_timing_counts_retries_without_changing_commit_order_or_payload(self):
        event = SimpleNamespace(method="session.event", payload={"sessionId": "attempt-a", "event": {"type": "tool/result", "private": "not-diagnostic"}})
        client = Mock()
        client.getresponse.side_effect = [OSError("synthetic"), SimpleNamespace(status=200, read=lambda _: b'{"saved":true}')]
        diagnostics = {}
        with patch.object(runner.http.client, "HTTPConnection", return_value=client), patch.object(runner.time, "sleep"), \
                patch.object(runner.time, "monotonic", side_effect=[10, 10.125]):
            runner.archive_event(event, "synthetic-capability", "attempt-a", diagnostics=diagnostics)
        self.assertEqual(diagnostics, {"events": 1, "attempts": 2, "total_ms": 125, "max_ms": 125})
        self.assertEqual(client.close.call_count, 2)
        self.assertEqual(client.request.call_args_list[0], client.request.call_args_list[1])

    def test_resume_keeps_new_materials_without_reinjecting_bootstrap_history(self):
        prompt = json.dumps({"user_question": "追问", "conversation": ["prior"], "selected_materials": {"report_ids": ["r1"]}})
        self.assertEqual(runner.continuation_prompt(prompt, False), prompt)
        self.assertEqual(json.loads(runner.continuation_prompt(prompt, True)),
                         {"user_question": "追问", "selected_materials": {"report_ids": ["r1"]}})
        self.assertEqual(runner.continuation_prompt("ordinary user text", True), "ordinary user text")

    def test_only_matching_committed_events_use_fixed_relay_and_retry_same_body(self):
        event = SimpleNamespace(method="session.event", payload={"sessionId": "attempt-a", "event": {"type": "tool/result", "seq": 1}})
        client = Mock()
        client.getresponse.side_effect = [OSError("synthetic"), SimpleNamespace(status=200, read=lambda _: b'{"saved":true}')]
        with patch.object(runner.http.client, "HTTPConnection", return_value=client) as connect, patch.object(runner.time, "sleep"):
            runner.archive_event(event, "synthetic-capability", "attempt-b")
            connect.assert_not_called()
            runner.archive_event(event, "synthetic-capability", "attempt-a")
        self.assertEqual(connect.call_args.args, ("relay", 9000))
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(client.request.call_args_list[0], client.request.call_args_list[1])
        self.assertEqual(client.request.call_args.args, ("POST", "/broker/trajectory"))
        self.assertEqual(json.loads(client.request.call_args.kwargs["body"])["session_id"], "attempt-a")

    def test_cancelled_attempt_stops_retry_and_emits_only_safe_gap_metadata(self):
        event = SimpleNamespace(method="session.event", payload={"sessionId": "attempt-a", "event": {"type": "assistant/message", "private": "never-print"}})
        client = Mock();client.getresponse.return_value = SimpleNamespace(status=403, read=lambda _: b'{}')
        output = io.StringIO()
        with patch.object(runner.http.client, "HTTPConnection", return_value=client), contextlib.redirect_stdout(output):
            runner.archive_event(event, "never-print-capability", "attempt-a")
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual(json.loads(output.getvalue()), {"event": {"kind": "trajectory.gap", "status": "archive_failed"}})
