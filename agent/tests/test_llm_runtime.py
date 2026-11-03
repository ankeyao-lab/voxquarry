from __future__ import annotations

import io
import json
import socket
import traceback
import unittest
import urllib.error
from unittest.mock import Mock, patch

from agent.config import LLMSettings
from agent.contracts import AgentError
from agent.llm import OpenAICompatibleLLMRuntime


class LLMRuntimeTests(unittest.TestCase):
    def runtime(self, **settings: object) -> OpenAICompatibleLLMRuntime:
        values = {
            "api_key": "synthetic-key-never-log",
            "base_url": "https://provider.invalid/v1",
            "model": "ZHIPU/GLM-5.3-Flash",
            "enable_thinking": False,
        }
        values.update(settings)
        return OpenAICompatibleLLMRuntime(LLMSettings(**values))

    def complete(self, runtime: OpenAICompatibleLLMRuntime | None = None) -> dict:
        return (runtime or self.runtime()).complete(
            messages=[{"role": "user", "content": "合成测试"}], tools=[],
            response_format={"type": "json_object"}, seed=7,
        )

    def test_glm_request_enables_thinking_low_effort_and_omits_empty_tools(self) -> None:
        raw = json.dumps({
            "model": "ZHIPU/GLM-5.3-Flash",
            "choices": [{"message": {"content": '{"intent":"scope"}'}, "finish_reason": "stop"}],
        }).encode()
        with patch("agent.llm.urllib.request.urlopen", return_value=io.BytesIO(raw)) as opened:
            result = self.complete()
        request = opened.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://provider.invalid/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-key-never-log")
        self.assertEqual(opened.call_args.kwargs["timeout"], 60.0)
        self.assertNotIn("tools", payload)
        self.assertIs(payload["enable_thinking"], True)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["seed"], 7)
        self.assertNotIn("synthetic-key-never-log", str(payload))
        self.assertEqual(result["choices"][0]["message"]["content"], '{"intent":"scope"}')

    def test_non_glm_preserves_configured_thinking_and_tools(self) -> None:
        tool = {"type": "function", "function": {"name": "safe_tool", "parameters": {}}}
        payload = self.runtime(model="other-model").request_payload(messages=[], tools=[tool])
        self.assertNotIn("enable_thinking", payload)
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["tools"], [tool])
        self.assertEqual(payload["max_tokens"], 4096)
        payload = self.runtime(model="other-model", reasoning_effort="medium").request_payload(
            messages=[], tools=[]
        )
        self.assertEqual(payload["reasoning_effort"], "medium")

    def test_glm_explicit_supported_effort_and_invalid_effort(self) -> None:
        for effort in ("low", "high", "max"):
            with self.subTest(effort=effort):
                payload = self.runtime(reasoning_effort=effort).request_payload(messages=[], tools=[])
                self.assertEqual(payload["reasoning_effort"], effort)
        with self.assertRaises(AgentError) as closed:
            self.runtime(reasoning_effort="medium").request_payload(messages=[], tools=[])
        self.assertEqual(closed.exception.code, "dependency_config_invalid")

    def test_only_final_text_and_token_counts_survive_response(self) -> None:
        value = {
            "id": "provider-diagnostic-must-not-persist",
            "model": "ZHIPU/GLM-5.3-Flash",
            "reasoning_content": "private-top-level-thought",
            "choices": [{
                "message": {
                    "role": "assistant", "content": '{"intent":"scope"}',
                    "reasoning_content": "private-model-thought",
                    "reasoning_details": ["private-detailed-thought"],
                    "provider_debug": "private-debug",
                },
                "reasoning_content": "private-choice-thought", "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17,
                "private_diagnostic": "private-usage-detail",
                "prompt_tokens_details": {"cached_tokens": 2, "reasoning_content": "private-cache"},
                "completion_tokens_details": {"reasoning_tokens": 4, "reasoning_content": "private-token"},
            },
        }
        with patch("agent.llm.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(value).encode())):
            result = self.complete()
        self.assertNotIn("private-", str(result))
        self.assertNotIn("provider-diagnostic", str(result))
        self.assertNotIn("reasoning_content", str(result))
        self.assertEqual(result["usage"], {
            "prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17,
            "prompt_tokens_details": {"cached_tokens": 2},
            "completion_tokens_details": {"reasoning_tokens": 4},
        })

    def test_invalid_usage_values_are_not_forwarded(self) -> None:
        value = {"choices": [{"message": {"content": "final"}}], "usage": {
            "prompt_tokens": True, "completion_tokens": -1, "total_tokens": "private-content",
            "prompt_tokens_details": {"cached_tokens": False},
        }}
        with patch("agent.llm.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(value).encode())):
            result = self.complete()
        self.assertEqual(result["usage"], {})

    def test_http_failure_classes_never_read_or_expose_provider_diagnostics(self) -> None:
        cases = {
            400: ("llm_upstream_error", False, 502),
            401: ("llm_authentication_failed", False, 503),
            403: ("llm_model_access_denied", False, 503),
            404: ("llm_model_access_denied", False, 503),
            408: ("llm_timeout", True, 504),
            429: ("llm_rate_limited", True, 503),
            500: ("llm_provider_unavailable", True, 503),
            503: ("llm_provider_unavailable", True, 503),
            504: ("llm_timeout", True, 504),
        }
        for status, (code, retryable, http_status) in cases.items():
            with self.subTest(status=status):
                body = io.BytesIO(b"private-provider-body")
                body.read = Mock(wraps=body.read)
                failure = urllib.error.HTTPError(
                    "https://provider.invalid/v1", status,
                    "private-provider-error", None, body,
                )
                with patch("agent.llm.urllib.request.urlopen", side_effect=failure):
                    try:
                        self.complete()
                    except AgentError as error:
                        self.assertEqual(error.code, code)
                        self.assertEqual(error.retryable, retryable)
                        self.assertEqual(error.http_status, http_status)
                        self.assertTrue(error.safe_next_step)
                        rendered = "".join(traceback.format_exception(error))
                        self.assertNotIn("private-provider", rendered)
                        self.assertNotIn("synthetic-key-never-log", rendered)
                    else:
                        self.fail("provider failure was accepted")
                body.read.assert_not_called()
                self.assertTrue(body.closed)

    def test_timeout_and_connection_failure_are_distinct(self) -> None:
        cases = (
            (TimeoutError("private-timeout"), "llm_timeout"),
            (urllib.error.URLError(socket.timeout("private-timeout")), "llm_timeout"),
            (urllib.error.URLError("connection timed out"), "llm_timeout"),
            (urllib.error.URLError("private-dns-failure"), "llm_unavailable"),
            (ConnectionResetError("private-connection-failure"), "llm_unavailable"),
        )
        for failure, code in cases:
            with self.subTest(code=code, kind=type(failure).__name__):
                with patch("agent.llm.urllib.request.urlopen", side_effect=failure):
                    with self.assertRaises(AgentError) as closed:
                        self.complete()
                self.assertEqual(closed.exception.code, code)
                self.assertTrue(closed.exception.retryable)
                self.assertTrue(closed.exception.__suppress_context__)

    def test_invalid_or_thinking_only_response_fails_without_leaking_content(self) -> None:
        cases = (
            b"private-invalid-json",
            b"[]",
            json.dumps({"choices": [{"message": {"reasoning_content": "private-thought"}}]}).encode(),
            json.dumps({"choices": [{"message": {"content": " "}}]}).encode(),
            json.dumps({"choices": [{"message": {"content": ["private-invalid"]}}]}).encode(),
        )
        for raw in cases:
            with self.subTest(shape=len(raw)):
                with patch("agent.llm.urllib.request.urlopen", return_value=io.BytesIO(raw)):
                    with self.assertRaises(AgentError) as closed:
                        self.complete()
                self.assertEqual(closed.exception.code, "llm_contract_invalid")
                self.assertNotIn("private-", str(closed.exception))

    def test_truncated_and_oversized_responses_fail_closed(self) -> None:
        raw = json.dumps({"choices": [{
            "message": {"content": '{"partial":'}, "finish_reason": "length",
        }]}).encode()
        with patch("agent.llm.urllib.request.urlopen", return_value=io.BytesIO(raw)):
            with self.assertRaises(AgentError) as closed:
                self.complete()
        self.assertEqual(closed.exception.code, "llm_response_incomplete")
        with patch("agent.llm.urllib.request.urlopen", return_value=io.BytesIO(b"x" * 1_000_001)):
            with self.assertRaises(AgentError) as closed:
                self.complete()
        self.assertEqual(closed.exception.code, "llm_contract_invalid")


if __name__ == "__main__":
    unittest.main()
