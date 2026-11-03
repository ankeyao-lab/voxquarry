from __future__ import annotations

import unittest
from unittest.mock import patch

from agent.config import LLMSettings
from agent.contracts import AgentError


class LLMConfigTests(unittest.TestCase):
    def environment(self, **overrides: str) -> dict[str, str]:
        values = {
            "VOXQUARRY_LLM_API_KEY": "synthetic-key-never-log",
            "VOXQUARRY_LLM_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1/",
            "VOXQUARRY_LLM_MODEL": "ZHIPU/GLM-5.3-Flash",
        }
        values.update(overrides)
        return values

    def test_glm_defaults_thinking_on_low_effort_and_redacts_key(self) -> None:
        with patch.dict("os.environ", self.environment(), clear=True):
            settings = LLMSettings.from_environment()
        self.assertTrue(settings.enable_thinking)
        self.assertEqual(settings.reasoning_effort, "high")
        self.assertTrue(settings.is_glm_53_flash)
        self.assertFalse(settings.base_url.endswith("/"))
        self.assertNotIn("synthetic-key-never-log", repr(settings))

    def test_old_false_thinking_flag_cannot_disable_glm_reasoning(self) -> None:
        with patch.dict("os.environ", self.environment(VOXQUARRY_LLM_ENABLE_THINKING="false"), clear=True):
            settings = LLMSettings.from_environment()
        self.assertTrue(settings.enable_thinking)

    def test_other_models_keep_existing_constructor_and_environment_behavior(self) -> None:
        settings = LLMSettings("synthetic", "https://provider.invalid/v1", "other-model", False)
        self.assertIsNone(settings.reasoning_effort)
        with patch.dict("os.environ", self.environment(
            VOXQUARRY_LLM_MODEL="other-model", VOXQUARRY_LLM_ENABLE_THINKING="false",
        ), clear=True):
            settings = LLMSettings.from_environment()
        self.assertFalse(settings.enable_thinking)
        self.assertIsNone(settings.reasoning_effort)
        with patch.dict("os.environ", self.environment(VOXQUARRY_LLM_MODEL="other-model"), clear=True):
            self.assertFalse(LLMSettings.from_environment().enable_thinking)

    def test_retired_deepseek_environment_settings_only_call_1017(self) -> None:
        from agent.llm import OpenAICompatibleLLMRuntime
        for model in ("deepseek-v4-flash-0731", "deepseek-v4.1-flash", "deepseek-v4.1-flash-1017"):
            with self.subTest(model=model), patch.dict("os.environ", self.environment(
                VOXQUARRY_LLM_MODEL=model, VOXQUARRY_LLM_ENABLE_THINKING="true",
                VOXQUARRY_LLM_REASONING_EFFORT="low",
            ), clear=True):
                settings = LLMSettings.from_environment()
                body = OpenAICompatibleLLMRuntime(settings).request_payload(messages=[], tools=[])
                self.assertEqual(body["model"], "deepseek-v4.1-flash-1017")
                self.assertEqual(body["reasoning_effort"], "low")
                self.assertTrue(body["enable_thinking"])

    def test_invalid_effort_and_thinking_are_rejected_without_config_values(self) -> None:
        for override in (
            {"VOXQUARRY_LLM_REASONING_EFFORT": "medium"},
            {"VOXQUARRY_LLM_REASONING_EFFORT": "private-invalid"},
            {"VOXQUARRY_LLM_ENABLE_THINKING": "private-invalid"},
        ):
            with self.subTest(override=list(override)):
                with patch.dict("os.environ", self.environment(**override), clear=True):
                    with self.assertRaises(AgentError) as closed:
                        LLMSettings.from_environment()
                self.assertEqual(closed.exception.code, "dependency_config_invalid")
                self.assertNotIn("private-invalid", str(closed.exception))
                self.assertNotIn("synthetic-key-never-log", str(closed.exception))

    def test_empty_and_incomplete_configuration_remain_distinct(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(LLMSettings.from_environment())
        with patch.dict("os.environ", {"VOXQUARRY_LLM_API_KEY": "synthetic-key-never-log"}, clear=True):
            with self.assertRaises(AgentError) as closed:
                LLMSettings.from_environment()
        self.assertEqual(closed.exception.code, "dependency_config_invalid")
        self.assertNotIn("synthetic-key-never-log", str(closed.exception))


if __name__ == "__main__":
    unittest.main()
