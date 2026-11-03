import unittest
from agent.research_limits import ResearchLimits


class ResearchLimitTests(unittest.TestCase):
    def test_trusted_environment_can_configure_allowances(self):
        limits = ResearchLimits.from_environment({"VOXQUARRY_WENQUAN_DEEPSEEK_OUTPUT_TOKENS": "262144",
            "VOXQUARRY_WENQUAN_ACTIVE_SECONDS": "5400"})
        self.assertEqual(limits.runtime("deepseek-v4.1-flash-1017")["max_output_tokens"], 262144)
        self.assertEqual(limits.runtime("deepseek-v4.1-flash-1017")["context_window"], 1_000_000)
        self.assertEqual(limits.runtime("deepseek-v4.1-flash-1017")["research_context_tokens"], 200_000)
        self.assertEqual(limits.active_seconds, 5400)
        self.assertEqual(limits.runtime("qwen3.8-max-0902")["max_output_tokens"], 16384)

    def test_invalid_or_unsupported_configuration_fails_at_startup(self):
        for values in ({"deepseek_output_tokens": 400000}, {"deepseek_context_tokens": 2000000},
                       {"model_budget_cny": 0}, {"model_calls": True}, {"model_timeout_seconds": 2000}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ResearchLimits(**values)

    def test_working_context_is_separate_from_provider_capability_and_clamped(self):
        limits = ResearchLimits.from_environment({"VOXQUARRY_WENQUAN_RESEARCH_CONTEXT_TOKENS": "300000"})
        self.assertEqual(limits.runtime("deepseek-v4.1-flash-1017")["research_context_tokens"], 300000)
        self.assertEqual(limits.runtime("deepseek-v4.1-flash-1017")["context_window"], 1000000)
        self.assertEqual(limits.runtime("qwen3.8-max")["research_context_tokens"], 200000)
        for value in (0, True, 1000001):
            with self.assertRaises(ValueError):
                ResearchLimits(research_context_tokens=value)
