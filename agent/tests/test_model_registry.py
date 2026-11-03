from __future__ import annotations

import json
import unittest
from datetime import date

from agent.contracts import AgentError
from agent.model_registry import ModelRegistry


class ModelRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic-private-provider-key"})

    def assert_invalid(self, method, *args, **kwargs):
        with self.assertRaises(AgentError):
            method(*args, **kwargs)

    def test_catalog_and_freeze_do_not_expose_credentials_or_provider_urls(self):
        catalog = self.registry.catalog()
        self.assertEqual(len(catalog["items"]), 6)
        self.assertEqual(catalog["default_model_key"], "deepseek-v4.1-flash")
        frozen = self.registry.freeze({})
        self.assertEqual(frozen["model_id"], "deepseek-v4.1-flash-1017")
        self.assertEqual(frozen["model_label"], "DeepSeek V4.1 Flash 1017")
        encoded = json.dumps({"catalog": catalog, "frozen": frozen})
        self.assertNotIn("synthetic-private-provider-key", encoded)
        self.assertNotIn("base_url", encoded)
        self.assertEqual(frozen["effective_effort"], "low")
        self.assertTrue(all(item["verification"] == "configured" for item in catalog["items"]))

    def test_retired_overrides_are_upgraded_without_reusing_permission_receipts(self):
        from agent.model_registry import matches_model_receipt
        for old_id in ("deepseek-v4-flash-0731", "deepseek-v4.1-flash"):
            for status in ("configured", "verified", "permission_denied"):
                registry = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic",
                    "VOXQUARRY_WENQUAN_MODELS_JSON": json.dumps({
                        "deepseek-v4.1-flash": {"model_id": old_id, "verification": status}})})
                item = registry.catalog()["items"][0]
                self.assertEqual(item["model_id"], "deepseek-v4.1-flash-1017")
                self.assertEqual(item["verification"], "configured")
                self.assertEqual(registry.runtime(registry.freeze({})).model, item["model_id"])
                self.assertFalse(matches_model_receipt(item["model_id"], old_id))
        self.assertTrue(matches_model_receipt("deepseek-v4.1-flash-1017", "deepseek-v4.1-flash-1017"))
        registry = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic", "VOXQUARRY_WENQUAN_MODELS_JSON": json.dumps({
            "deepseek-v4.1-flash": {"verification": "verified"}})})
        self.assertEqual(registry.catalog()["items"][0]["verification"], "configured")

    def test_saved_preferences_upgrade_new_questions_without_rewriting_frozen_runs(self):
        from unittest.mock import patch
        saved = {"model_key": "deepseek-v4.1-flash", "effort": "high"}
        with patch("agent.model_registry.upgrade_retired_deepseek_model", side_effect=lambda value: value):
            old = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic-private-provider-key",
                "VOXQUARRY_WENQUAN_MODELS_JSON": json.dumps({
                    "deepseek-v4.1-flash": {"model_id": "deepseek-v4-flash-0731"}})})
            frozen = old.freeze(saved)
        original = json.dumps(frozen, sort_keys=True)
        with self.assertRaises(AgentError) as caught:
            self.registry.runtime(frozen)
        self.assertEqual(caught.exception.code, "model_config_changed")
        self.assertEqual(json.dumps(frozen, sort_keys=True), original)
        current = self.registry.freeze(self.registry.saved_preferences(saved))
        self.assertEqual(current["model_id"], "deepseek-v4.1-flash-1017")
        self.assertEqual(current["effective_effort"], "high")

    def test_1017_price_has_its_own_date_source_and_exact_beijing_endpoint(self):
        registry = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic",
            "VOXQUARRY_LLM_BASE_URL": "https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1"})
        price = registry.pricing({"model_key": "deepseek-v4.1-flash"}, today=date(2026, 10, 23))
        self.assertEqual(price["status"], "verified_list_price")
        self.assertEqual(price["checked_at"], "2026-10-23")
        self.assertEqual(price["source_url"], "https://www.qianwenai.com/models/deepseek-v4.1-flash-1017")
        self.assertEqual(registry.pricing({"model_key": "deepseek-v4.1-flash"}, today=date(2026, 10, 22))["reason"], "pricing_not_yet_verified")
        qwen = registry.pricing({"model_key": "qwen3.8-max"}, today=date(2026, 10, 23))
        self.assertEqual(qwen["checked_at"], "2026-10-08")
        self.assertNotEqual(qwen["version"], price["version"])

    def test_all_declared_model_efforts_validate_and_invalid_cross_family_efforts_fail(self):
        for item in self.registry.catalog()["items"]:
            for effort in item["efforts"]:
                if not item["available"]:
                    self.assertEqual(self.registry.selection({"model_key": item["key"], "effort": effort["value"]})["effort"], effort["value"])
                    continue
                frozen = self.registry.freeze({"model_key": item["key"], "effort": effort["value"]})
                self.assertEqual(self.registry.runtime(frozen).reasoning_effort, effort["value"])
        self.assert_invalid(self.registry.selection, {"model_key": "qwen3.8-max", "effort": "max"})
        self.assert_invalid(self.registry.selection, {"model_key": "deepseek-v4.1-flash", "effort": "xhigh"})
        self.assert_invalid(self.registry.selection, {"model_key": "glm-5.3", "thinking_enabled": False})

    def test_previous_defaults_resume_without_accepting_other_config_changes(self):
        from dataclasses import replace
        from unittest.mock import patch
        from agent.model_registry import MODELS
        for effort in ("max", "high"):
            with self.subTest(effort=effort), patch("agent.model_registry.MODELS", tuple(replace(m, default_effort=effort) if m.key == "deepseek-v4.1-flash" else m for m in MODELS)):
                old = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic-private-provider-key"})
                frozen = old.freeze({})
                changed = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic-private-provider-key", "VOXQUARRY_LLM_BASE_URL": "https://other.test/v1"})
            self.assertEqual(self.registry.runtime(frozen).reasoning_effort, effort)
            self.assertEqual(self.registry.runtime(old.freeze({"effort": "high"})).reasoning_effort, "high")
            self.assert_invalid(self.registry.runtime, {**frozen, "effective_effort": "low"})
            self.assert_invalid(self.registry.runtime, changed.freeze({}))
        self.assertEqual(self.registry.runtime(self.registry.freeze({"effort": "max"})).reasoning_effort, "max")
        self.assert_invalid(self.registry.runtime, {**self.registry.freeze({}), "effective_effort": "max"})

    def test_qwen_snapshot_and_explicit_thinking_choice_remain_fixed(self):
        frozen = self.registry.freeze({"model_key": "qwen3.8-max", "thinking_enabled": False, "effort": "low", "web_enabled": False})
        self.assertEqual(frozen["model_id"], "qwen3.8-max-0902")
        runtime = self.registry.runtime(frozen)
        self.assertFalse(runtime.enable_thinking)
        self.assertEqual(runtime.reasoning_effort, "low")

    def test_qwen_excludes_efforts_rejected_by_pinned_harness_initialization(self):
        for key in ("qwen3.8-max", "qwen3.8-flash"):
            item = next(item for item in self.registry.catalog()["items"] if item["key"] == key)
            self.assertEqual(item["efforts"], [{"value": "low", "label": "轻量"}])
            self.assertEqual(self.registry.freeze({"model_key": key})["effective_effort"], "low")
            for effort in ("medium", "xhigh"):
                self.assert_invalid(self.registry.freeze, {"model_key": key, "effort": effort})

    def test_kimi_uses_verified_aliyun_route_and_only_verified_effort(self):
        item = next(item for item in self.registry.catalog()["items"] if item["key"] == "kimi-k3")
        self.assertEqual(item["model_id"], "kimi-k3")
        self.assertEqual(item["efforts"], [{"value": "low", "label": "轻量"}])
        self.assertTrue(item["available"])
        self.assertIn("轻量", item["capability_note"])
        frozen = self.registry.freeze({"model_key": "kimi-k3"})
        self.assertEqual(self.registry.runtime(frozen).reasoning_effort, "low")
        self.assertTrue(self.registry.runtime(frozen).enable_thinking)
        for effort in ("high", "max", "medium"):
            self.assert_invalid(self.registry.freeze, {"model_key": "kimi-k3", "effort": effort})

    def test_retired_qwen_catalog_keeps_compatible_frozen_runs_without_rewriting_them(self):
        from dataclasses import replace
        from unittest.mock import patch
        from agent.model_registry import MODELS
        with patch("agent.model_registry.MODELS", tuple(replace(m, efforts=("low", "medium", "xhigh"), default_effort="xhigh") if m.key.startswith("qwen") else m for m in MODELS)):
            previous = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic-private-provider-key"})
        for selection in ({"model_key": "glm-5.3", "effort": "high"}, {"model_key": "qwen3.8-flash", "effort": "low"}, {}):
            frozen = previous.freeze(selection)
            self.assertEqual(self.registry.runtime(frozen).reasoning_effort, frozen["effective_effort"])
        for effort in ("default", "medium", "xhigh"):
            frozen = previous.freeze({"model_key": "qwen3.8-flash", "effort": effort})
            with self.assertRaises(AgentError) as caught:
                self.registry.runtime(frozen)
            self.assertEqual(caught.exception.code, "model_config_changed")
            self.assertEqual(frozen["effort"], effort)

    def test_only_saved_retired_preferences_are_normalized(self):
        saved = {"model_key": "qwen3.8-flash", "effort": "medium", "effort_by_model": {"qwen3.8-flash": "medium", "qwen3.8-max": "xhigh", "glm-5.3": "high"}}
        result = self.registry.saved_preferences(saved)
        self.assertEqual(result["effort"], "default")
        self.assertEqual(result["effort_by_model"]["glm-5.3"], "high")
        self.assertEqual(result["effort_by_model"]["qwen3.8-max"], "default")
        self.assertEqual(saved["effort"], "medium")
        self.assert_invalid(self.registry.selection, saved)
        self.assert_invalid(self.registry.saved_preferences, {"model_key": "missing"})

    def test_legacy_kimi_supplier_override_is_not_silently_rerouted_or_budgeted_as_native(self):
        registry = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic", "VOXQUARRY_WENQUAN_MODELS_JSON": json.dumps({
            "kimi-k3": {"model_id": "kimi/kimi-k3", "verification": "verified"}})})
        item = next(item for item in registry.catalog()["items"] if item["key"] == "kimi-k3")
        self.assertEqual(item["model_id"], "kimi/kimi-k3")
        self.assertFalse(item["available"])
        self.assertFalse(item["pricing"]["output_limit_includes_thinking"])
        self.assertNotEqual(registry.config_version, self.registry.config_version)
        with self.assertRaises(AgentError) as caught:
            registry.freeze({"model_key": "kimi-k3"})
        self.assertEqual(caught.exception.code, "model_budget_unavailable")

    def test_native_kimi_pricing_distinguishes_model_capability_from_request_limit(self):
        price = self.registry.pricing({"model_key": "kimi-k3"}, today=date(2026, 10, 8))
        self.assertEqual(price["maximum_thinking_tokens"], 1048576)
        self.assertTrue(price["output_limit_includes_thinking"])
        self.assertEqual(price["source_url"], "https://www.qianwenai.com/models/kimi-k3")

    def test_verified_prices_use_beijing_peak_exact_model_and_safe_cache_rates(self):
        expected = {"deepseek-v4.1-flash": ("2", "8", "0.2", 0),
                    "qwen3.8-max": ("12", "36", "1.5", 0),
                    "qwen3.8-flash": ("0.8", "2.7", "0.1", 0),
                    "kimi-k3": ("20", "100", "2", 1048576),
                    "glm-5.3": ("8", "28", None, 131072),
                    "glm-5.3-flash": ("0.8", "2.8", None, 131072)}
        for key, rates in expected.items():
            result = self.registry.pricing({"model_key": key}, today=date(2026, 10, 23))
            self.assertEqual(result["status"], "verified_list_price")
            self.assertEqual(tuple(result[field] for field in ("input_per_million", "output_per_million", "cached_input_per_million", "maximum_thinking_tokens")), rates)

    def test_last_verified_prices_remain_usable_after_former_review_deadline(self):
        for key in (item["key"] for item in self.registry.catalog()["items"]):
            verified_at = date(2026, 10, 23) if key == "deepseek-v4.1-flash" else date(2026, 10, 8)
            original = self.registry.pricing({"model_key": key}, today=verified_at)
            for day in (date(2026, 10, 24), date(2027, 1, 1)):
                with self.subTest(model=key, day=day):
                    price = self.registry.pricing({"model_key": key}, today=day)
                    self.assertEqual(price, original)
                    self.assertEqual(price["status"], "verified_list_price")
                    self.assertEqual(price["checked_at"], verified_at.isoformat())
                    self.assertNotIn("valid_through", price)
                    self.assertNotIn("review_due", price)

    def test_price_unknown_for_unverified_region_model_or_before_review(self):
        for override in ({"base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
                         {"base_url": "https://other.test/compatible-mode/v1"}, {"model_id": "unverified-model"}):
            registry = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic", "VOXQUARRY_WENQUAN_MODELS_JSON": json.dumps({"qwen3.8-max": override})})
            price = registry.pricing({"model_key": "qwen3.8-max"}, today=date(2026, 10, 8))
            self.assertEqual(price["status"], "unknown")
            self.assertNotIn("input_per_million", price)
        self.assertEqual(self.registry.pricing({"model_key": "qwen3.8-max"}, today=date(2026, 10, 7))["reason"], "pricing_not_yet_verified")
        registry = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic", "VOXQUARRY_LLM_BASE_URL": "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"})
        self.assertEqual(registry.pricing({"model_key": "qwen3.8-max"}, today=date(2026, 10, 8))["status"], "verified_list_price")

    def test_malformed_selection_or_frozen_shape_gets_domain_error(self):
        for value in (1, ["not-a-mapping"], "bad"):
            self.assert_invalid(self.registry.selection, value)
        for value in ({}, {"model_key": []}, None):
            self.assert_invalid(self.registry.runtime, value)
        self.assert_invalid(self.registry.selection, {"web_enabled": "false"})
        self.assert_invalid(self.registry.selection, {"base_url": "https://other.test"})

    def test_runtime_rejects_changed_model_config_and_tampered_effective_effort(self):
        frozen = self.registry.freeze({"model_key": "glm-5.3", "effort": "high"})
        for field, value in (("effective_effort", "max"), ("effective_effort", "invalid"), ("model_config_version", "changed"), ("model_id", "changed")):
            self.assert_invalid(self.registry.runtime, {**frozen, field: value})

    def test_unconfigured_models_remain_visible_but_cannot_start(self):
        registry = ModelRegistry({})
        self.assertEqual(len(registry.catalog()["items"]), 6)
        self.assertFalse(any(item["available"] for item in registry.catalog()["items"]))
        self.assert_invalid(registry.freeze, {})

    def test_permission_denied_keeps_configuration_but_blocks_selection_freeze_and_runtime(self):
        registry = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic-private-provider-key",
                                  "VOXQUARRY_WENQUAN_MODELS_JSON": json.dumps({
                                      "deepseek-v4.1-flash": {"verification": "permission_denied"}})})
        item = next(item for item in registry.catalog()["items"] if item["key"] == "deepseek-v4.1-flash")
        self.assertFalse(item["available"])
        self.assertEqual(item["verification"], "permission_denied")
        self.assertEqual(item["unavailable_reason"], "云账号尚未开通此模型")
        # Permission failure does not erase the provider configuration or its known prices.
        self.assertEqual(registry.pricing({"model_key": item["key"]}, today=date(2026, 10, 23))["status"], "verified_list_price")
        self.assertEqual(registry.selection({})["model_key"], "deepseek-v4.1-flash")
        frozen = self.registry.freeze({})
        for method, args, kwargs in ((registry.freeze, ({},), {}),
                                     (registry.selection, ({},), {"require_available": True}),
                                     (registry.runtime, ({**frozen, "model_config_version": registry.config_version},), {})):
            with self.subTest(method=method.__name__):
                with self.assertRaises(AgentError) as caught:
                    method(*args, **kwargs)
                self.assertEqual(caught.exception.code, "model_permission_denied")
        qwen = registry.freeze({"model_key": "qwen3.8-max"})
        self.assertEqual(registry.runtime(qwen).model, "qwen3.8-max-0902")

    def test_verification_changes_invalidate_previously_frozen_runs(self):
        frozen = self.registry.freeze({})
        versions = {self.registry.config_version}
        for verification in ("verified", "permission_denied"):
            registry = ModelRegistry({"VOXQUARRY_LLM_API_KEY": "synthetic-private-provider-key",
                                      "VOXQUARRY_WENQUAN_MODELS_JSON": json.dumps({
                                          "deepseek-v4.1-flash": {"model_id": "deepseek-v4.1-flash-1017", "verification": verification}})})
            versions.add(registry.config_version)
            with self.assertRaises(AgentError) as caught:
                registry.runtime(frozen)
            self.assertEqual(caught.exception.code, "model_config_changed")
            if verification == "verified":
                item = next(item for item in registry.catalog()["items"] if item["key"] == "deepseek-v4.1-flash")
                self.assertEqual(item["verification"], "verified")
                self.assertEqual(registry.runtime(registry.freeze({})).model, "deepseek-v4.1-flash-1017")
        self.assertEqual(len(versions), 3)

    def test_disabled_and_missing_credentials_have_distinct_status_and_versions(self):
        base = {"VOXQUARRY_WENQUAN_MODELS_JSON": json.dumps({
            "deepseek-v4.1-flash": {"verification": "verified"}})}
        unconfigured = ModelRegistry(base)
        disabled = ModelRegistry({"VOXQUARRY_WENQUAN_MODELS_JSON": json.dumps({
                                      "deepseek-v4.1-flash": {"enabled": False, "verification": "verified"}})})
        for registry, status, reason in ((unconfigured, "unconfigured", "此模型尚未配置"),
                                         (disabled, "disabled", "此模型已停用")):
            item = next(item for item in registry.catalog()["items"] if item["key"] == "deepseek-v4.1-flash")
            self.assertEqual(item["verification"], status)
            self.assertEqual(item["unavailable_reason"], reason)
            self.assertFalse(item["available"])
            with self.assertRaises(AgentError) as caught:
                registry.freeze({})
            self.assertEqual(caught.exception.code, "model_unavailable")
            self.assertEqual(str(caught.exception), reason)
        self.assertNotEqual(unconfigured.config_version, disabled.config_version)

    def test_invalid_verification_fails_at_startup_even_for_disabled_model(self):
        for verification in (None, True, 0, [], {}, "", "disabled", "unconfigured", "approved"):
            with self.subTest(verification=verification):
                with self.assertRaises(AgentError) as caught:
                    ModelRegistry({"VOXQUARRY_WENQUAN_MODELS_JSON": json.dumps({
                        "deepseek-v4.1-flash": {"enabled": False, "verification": verification}})})
                self.assertEqual(caught.exception.code, "dependency_config_invalid")


if __name__ == "__main__":
    unittest.main()
