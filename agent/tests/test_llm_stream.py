from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import patch

import httpx

from agent.config import LLMSettings
from agent.contracts import AgentError
from agent.llm import OpenAICompatibleLLMRuntime


def event(delta=None, finish=None, **extra):
    value = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **extra}
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n").encode()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay, self.closed = chunks, delay, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


class StreamTests(unittest.TestCase):
    def run_stream(self, stream, *, status=200, total=5, idle=2, active=None):
        runtime = OpenAICompatibleLLMRuntime(LLMSettings(api_key="private-key", model="synthetic",
            base_url="https://provider.invalid/v1", enable_thinking=True), timeout_seconds=idle, stream_total_seconds=total)
        requests, notices = [], []
        self.notices = notices

        def handle(request):
            requests.append(request)
            return httpx.Response(status, stream=stream)

        real_client = httpx.AsyncClient
        with patch("agent.llm.httpx.AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw)):
            result = runtime.complete_stream(messages=[{"role": "user", "content": "测试"}], tools=[],
                response_format={"type": "json_object"}, check_active=active, on_progress=notices.append)
        return result, requests, notices

    def test_fragmented_utf8_sse_keeps_only_complete_answer_and_usage(self):
        raw = (b": keepalive\n\n" + event({"reasoning_content": "private-thought"})
            + event({"content": '{"答案":'}) + event({"content": '"完整"}'}, "stop")
            + b'data: {"choices":[],"usage":{"total_tokens":42,"provider_debug":"private-debug"}}\n\n'
            + b"data: [DONE]\n\n")
        stream = Chunks([raw[index:index + 7] for index in range(0, len(raw), 7)])
        result, requests, notices = self.run_stream(stream)
        self.assertEqual(json.loads(result["choices"][0]["message"]["content"]), {"答案": "完整"})
        self.assertEqual(result["usage"], {"total_tokens": 42})
        self.assertNotIn("private", str(result) + str(notices))
        self.assertTrue(stream.closed)
        payload = json.loads(requests[0].content)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertNotIn("tools", payload)

    def test_closed_connection_with_parseable_partial_json_is_not_success(self):
        with self.assertRaises(AgentError) as error:
            self.run_stream(Chunks([event({"content": '{"looks":"complete"}'})]))
        self.assertEqual(error.exception.code, "llm_response_incomplete")

    def test_token_limit_and_invalid_sse_fail_without_leaking_provider_content(self):
        for raw, code in ((event({"content": "partial"}, "length") + b"data: [DONE]\n\n", "llm_response_incomplete"),
                          (b"data: private-provider-error\n\n", "llm_contract_invalid")):
            with self.subTest(code=code), self.assertRaises(AgentError) as error:
                self.run_stream(Chunks([raw]))
            self.assertEqual(error.exception.code, code)
            self.assertNotIn("private-provider", str(error.exception))

    def test_cancel_interrupts_waiting_socket_and_closes_stream(self):
        stream = Chunks([event({"content": "late"})], delay=5)
        calls = []

        def active():
            calls.append(True)
            if len(calls) == 2:
                raise AgentError("run_cancelled", "stopped", http_status=409)

        started = time.monotonic()
        with self.assertRaises(AgentError) as error:
            self.run_stream(stream, active=active)
        self.assertEqual(error.exception.code, "run_cancelled")
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(stream.closed)

    def test_idle_timeout_is_distinct_from_continuous_generation_total_limit(self):
        stream = Chunks([event({"content": "late"})], delay=5)
        with self.assertRaises(AgentError) as error:
            self.run_stream(stream, idle=.05)
        self.assertEqual(error.exception.code, "llm_idle_timeout")
        self.assertTrue(stream.closed)
        stream = Chunks([event({"reasoning_content": "private"})] * 100, delay=.02)
        with self.assertRaises(AgentError) as error:
            self.run_stream(stream, total=.1)
        self.assertEqual(error.exception.code, "llm_total_timeout")
        self.assertTrue(stream.closed)
        self.assertNotIn("private", str(self.notices))

    def test_http_failure_does_not_read_error_body(self):
        class ForbiddenBody(Chunks):
            async def __aiter__(self):
                raise AssertionError("must not read provider error body")
                yield b""
        stream = ForbiddenBody([])
        with self.assertRaises(AgentError) as error:
            self.run_stream(stream, status=403)
        self.assertEqual(error.exception.code, "llm_model_access_denied")
        self.assertTrue(stream.closed)

    def test_progress_changes_during_generation_without_exposing_text(self):
        stream = Chunks([event({"reasoning_content": "private-thinking"}),
                         event({"content": "private-answer"}, "stop"), b"data: [DONE]\n\n"], delay=.3)
        result, _, notices = self.run_stream(stream)
        self.assertEqual({n["phase"] for n in notices}, {"waiting", "thinking", "answering"})
        self.assertNotIn("private", str(notices))
        self.assertEqual(result["choices"][0]["message"]["content"], "private-answer")


if __name__ == "__main__":
    unittest.main()
