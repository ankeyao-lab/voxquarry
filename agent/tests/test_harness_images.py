"""Image reader uses the same owned inputs, provider transport and run budget."""
import base64
import json
import unittest
from unittest.mock import patch

import httpx

from agent.contracts import AgentError
from agent.research_attachments import IsolatedAttachmentParser
from agent.tests.test_agent_trace import actor
from agent.tests.test_harness_research import HarnessFixture

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY1kAAAAASUVORK5CYII=")


class HarnessImageTests(HarnessFixture, unittest.TestCase):
    def image(self, text=""):
        self.attachments.parser = IsolatedAttachmentParser(lambda args: {
            "text": text, "parser_version": "synthetic/v1", "locators": [], "source_sha256": args["source_sha256"]}, "synthetic-image")
        item = self.attachments.create(self.user, self.sid, filename="shape.png", content=PNG, media_type="image/png")
        return self.attachments.parse_pending(self.user, self.sid, item["attachment_id"])

    def cap(self, item):
        run = self.accept(attachment_ids=[item["attachment_id"]])
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run["run_id"], question="描述图片")
        return self.research.broker.issue(self.user, run, trace["trace_id"])[1]

    def client(self, requests, *, finish="stop", model="qwen3.8-flash"):
        def respond(request):
            requests.append(json.loads(request.content))
            items = [{"model": model, "choices": [{"delta": {"content": "白底上有一个蓝色圆形，没有文字。"}, "finish_reason": finish}]},
                     {"model": model, "choices": [], "usage": {"prompt_tokens": 1200, "completion_tokens": 30, "total_tokens": 1230}}]
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text="".join("data: " + json.dumps(value) + "\n\n" for value in items) + "data: [DONE]\n\n")
        return httpx.Client(transport=httpx.MockTransport(respond))

    def test_photo_without_ocr_uses_vision_once_and_cites_saved_interpretation(self):
        item = self.image(); cap = self.cap(item); requests = []
        self.research.broker.model_client = self.client(requests)
        first = self.research.tool(cap, "read_attachment", {"attachment_id": item["attachment_id"]})
        self.assertNotIn("error", first)
        self.assertIn("蓝色圆形", first["text"])
        self.assertEqual(first["total_chars"], len(first["text"]))
        self.assertNotEqual(first["text_sha256"], first["attachment"]["text_sha256"])
        self.assertEqual(first["image_analysis"], {"model": "qwen3.8-flash", "cached": False})
        second = self.research.tool(cap, "read_attachment", {"attachment_id": item["attachment_id"]})
        self.assertTrue(second["image_analysis"]["cached"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["model"], "qwen3.8-flash")
        self.assertFalse(requests[0]["enable_thinking"])
        self.assertNotIn("tools", requests[0])
        self.assertTrue(requests[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        content = self.research.source(self.user, self.sid, first["source_id"])
        self.assertEqual(content["content"], first["text"])
        self.assertTrue(content["full_url"].endswith("/download"))
        self.assertNotIn("image_analysis", self.attachments.get(self.user, self.sid, item["attachment_id"])["parse_details"])
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM research_model_charges WHERE run_id=?", (cap.run_id,)).fetchone()
        self.assertEqual(row["actual_model"], "qwen3.8-flash")
        self.assertEqual(row["charged_nano"], 1200 * 800 + 30 * 2700)
        self.assertLess(row["charged_nano"], row["reserved_nano"])

    def test_ocr_fallback_is_explicit_and_missing_vision_never_claims_to_see(self):
        item = self.image("电池很弱"); cap = self.cap(item)
        with patch.object(self.research.broker, "read_image", side_effect=AgentError("model_unavailable", "retry", retryable=True)):
            result = self.research.tool(cap, "read_attachment", {"attachment_id": item["attachment_id"]})
        self.assertEqual(result["text"], "电池很弱")
        self.assertEqual(result["image_analysis"]["status"], "unavailable")
        self.assertIn("仅为OCR", result["warning"])
        self.assertNotIn("image_reader", self.state.source(self.user, self.sid, result["source_id"])["source"]["provenance"])

    def test_incomplete_result_is_not_cached_and_owner_boundary_holds(self):
        item = self.image(); cap = self.cap(item); requests = []
        self.research.broker.model_client = self.client(requests, finish="length")
        result = self.research.tool(cap, "read_attachment", {"attachment_id": item["attachment_id"]})
        self.assertEqual(result["error"]["code"], "vision_response_incomplete")
        key = self.research.broker.image_reader_key()
        self.assertIsNone(self.attachments.image_analysis(self.user, self.sid, item["attachment_id"], key))
        with self.assertRaises(AgentError):
            self.attachments.image_analysis(actor("other"), self.sid, item["attachment_id"], key)
        self.state.cancel(self.user, cap.run_id)
        with self.assertRaises(AgentError):
            self.research.broker.read_image(cap, PNG, "image/png")
        self.assertEqual(len(requests), 1)

    def test_image_bound_is_reserved_before_network_and_wrong_model_keeps_reservation(self):
        item = self.image(); cap = self.cap(item); requests = []
        self.research.broker.model_budget = 1
        self.research.broker.model_client = self.client(requests, model="unexpected-model")
        result = self.research.tool(cap, "read_attachment", {"attachment_id": item["attachment_id"]})
        self.assertEqual(result["error"]["code"], "research_cost_budget_reached")
        self.assertEqual(requests, [])
        self.research.broker.model_budget = 100_000_000_000
        self.research.tool(cap, "read_attachment", {"attachment_id": item["attachment_id"]})
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM research_model_charges WHERE run_id=?", (cap.run_id,)).fetchone()
        self.assertIsNone(row["charged_nano"])
