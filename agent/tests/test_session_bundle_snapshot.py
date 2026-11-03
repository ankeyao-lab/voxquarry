from __future__ import annotations

import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent.contracts import AgentError
from agent.readonly_store import ReadonlyTurns
from agent.store import WenquanStore
from agent.tests.helpers import REPORT_ID, identity


class SessionBundleSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        database = Path(self.tmp.name) / "wenquan.sqlite3"
        self.reader = WenquanStore(database)
        self.reader.initialize()
        self.writer = WenquanStore(database)
        self.turns = ReadonlyTurns(self.writer)
        self.actor = identity()

    def tearDown(self):
        self.tmp.cleanup()

    def test_finish_between_message_and_run_reads_cannot_hide_terminal_answer(self):
        for terminal in ("completed", "blocked"):
            with self.subTest(terminal=terminal):
                session = self.writer.create_session(self.actor, "一致的回答快照")
                sid = session["session_id"]
                run, _ = self.turns.accept(
                    self.actor, sid, question="请核对评论", report_id=REPORT_ID,
                    request_id="snapshot-regression", plan=[{"title": "读取原文"}],
                )
                rid = run["run_id"]
                original_connect = self.reader._connect
                triggered = False
                errors = []
                metadata = {"report_id": REPORT_ID, "nested": {"count": 2, "complete": terminal == "completed"}}

                def trace(statement):
                    nonlocal triggered
                    if triggered or not re.search(r"^\s*SELECT\b.*\bFROM\s+runs\b", statement, re.I | re.S):
                        return
                    triggered = True
                    try:
                        self.turns.finish(self.actor, rid, status=terminal, content="已核对的回答", metadata=metadata)
                    except Exception as error:
                        # SQLite suppresses callback exceptions; surface them in
                        # the test instead of treating a failed writer as a pass.
                        errors.append(error)

                def connect():
                    connection = original_connect()
                    connection.set_trace_callback(trace)
                    return connection

                with patch.object(self.reader, "_connect", side_effect=connect):
                    before = self.reader.session_bundle(self.actor, sid)
                self.assertTrue(triggered)
                self.assertEqual(errors, [])
                self.assertEqual(before["runs"][0]["status"], "running", before)
                self.assertEqual([item["role"] for item in before["messages"]], ["user"])
                self.assertEqual(before["runs"][0]["steps"][0]["status"], "queued")

                after = self.reader.session_bundle(self.actor, sid)
                self.assertEqual(after["runs"][0]["status"], terminal)
                replies = [item for item in after["messages"] if item["role"] == "assistant"]
                self.assertEqual(len(replies), 1)
                self.assertEqual(replies[0]["content"], "已核对的回答")
                self.assertEqual(replies[0]["metadata"], metadata)
                self.assertEqual(replies[0]["answer_status"], "answered" if terminal == "completed" else "insufficient_evidence")

    def test_steps_and_artifacts_share_the_run_snapshot_during_finish(self):
        for boundary in ("steps", "artifacts"):
            with self.subTest(boundary=boundary):
                sid = self.writer.create_session(self.actor, "步骤与资料快照")["session_id"]
                run, _ = self.turns.accept(
                    self.actor, sid, question="核对资料", report_id=REPORT_ID,
                    request_id="nested-snapshot", plan=[{"title": "读取原文"}],
                )
                rid = run["run_id"]
                step_id = run["steps"][0]["step_id"]
                self.turns.step(self.actor, rid, step_id, "running")
                original_connect = self.reader._connect
                triggered = False
                errors = []
                artifact_metadata = {"sources": [REPORT_ID], "coverage": {"read": 1, "complete": False}}

                def trace(statement):
                    nonlocal triggered
                    if triggered or not re.search(rf"^\s*SELECT\b.*\bFROM\s+{boundary}\b", statement, re.I | re.S):
                        return
                    triggered = True
                    try:
                        self.turns.step(self.actor, rid, step_id, "completed")
                        self.writer.add_artifact(
                            self.actor, run_id=rid, artifact_type="report_reference",
                            upstream_id=REPORT_ID, contract_version="report-json/v1",
                            status="available", display_name="本轮核对资料", metadata=artifact_metadata,
                        )
                        self.turns.finish(self.actor, rid, status="completed", content="核对完成", metadata={"report_id": REPORT_ID})
                    except Exception as error:
                        errors.append(error)

                def connect():
                    connection = original_connect()
                    connection.set_trace_callback(trace)
                    return connection

                with patch.object(self.reader, "_connect", side_effect=connect):
                    before = self.reader.session_bundle(self.actor, sid)
                self.assertTrue(triggered)
                self.assertEqual(errors, [])
                self.assertEqual(before["runs"][0]["status"], "running")
                self.assertEqual(before["runs"][0]["steps"][0]["status"], "running")
                self.assertEqual(before["runs"][0]["artifacts"], [])
                self.assertEqual([message["role"] for message in before["messages"]], ["user"])

                after = self.reader.session_bundle(self.actor, sid)
                self.assertEqual(after["runs"][0]["status"], "completed")
                self.assertEqual(after["runs"][0]["steps"][0]["status"], "completed")
                self.assertEqual(len(after["runs"][0]["artifacts"]), 1)
                self.assertEqual(after["runs"][0]["artifacts"][0]["metadata"], artifact_metadata)

    def test_bundle_preserves_owner_and_scope_isolation_including_admin(self):
        sid = self.writer.create_session(self.actor, "私有会话")["session_id"]
        self.writer.add_message(self.actor, sid, role="user", content="仅本人可见")
        for other in (
            identity("another-owner"),
            identity("admin-owner", roles=("admin",)),
            replace(self.actor, scope_id="another-scope"),
        ):
            with self.subTest(owner=other.owner_user_id, scope=other.scope_id):
                with self.assertRaises(AgentError) as rejected:
                    self.reader.session_bundle(other, sid)
                self.assertEqual(rejected.exception.code, "session_not_found")
                self.assertEqual(rejected.exception.http_status, 404)

    def test_bundle_keeps_existing_order_and_decoded_json_shape(self):
        sid = self.writer.create_session(self.actor, "兼容已有数据")["session_id"]
        for index in range(2):
            run = self.writer.create_run(
                self.actor, sid, intent=f"历史研究{index}",
                plan=[
                    {"title": "读取", "input_summary": {"filters": {"ratings": [1, 2]}, "offset": index}},
                    {"title": "核对", "input_summary": {"complete": False, "missing": None}},
                ],
            )
            self.writer.add_message(
                self.actor, sid, role="assistant", content=f"已有回答{index}", run_id=run["run_id"],
                metadata={"citations": [{"report_id": REPORT_ID}], "count": index, "checked": True},
            )
            self.writer.add_artifact(
                self.actor, run_id=run["run_id"], artifact_type="report_reference", upstream_id=REPORT_ID,
                contract_version="report-json/v1", status="available", display_name=f"来源{index}",
                metadata={"version": index, "nested": [True, None, {"count": 2}]},
            )
        expected = {
            "session": self.reader.get_session(self.actor, sid),
            "messages": self.reader.list_messages(self.actor, sid),
            "runs": self.reader.list_runs(self.actor, sid),
        }
        actual = self.reader.session_bundle(self.actor, sid)
        self.assertEqual(actual, expected)
        self.assertIs(actual["session"]["archived"], False)
        self.assertEqual(actual["runs"][0]["steps"][0]["input_summary"]["filters"], {"ratings": [1, 2]})
        self.assertNotIn("input_summary_json", actual["runs"][0]["steps"][0])
        self.assertNotIn("metadata_json", actual["messages"][0])
        self.assertNotIn("metadata_json", actual["runs"][0]["artifacts"][0])


if __name__ == "__main__":
    unittest.main()
