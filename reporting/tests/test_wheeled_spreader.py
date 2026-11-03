import unittest

from reporting.analysis import build_report
from reporting.ope_product_report import build_ope_product_report, category_profile
from reporting.product_insight import product_insight_support
from reporting.tests.test_reporting import fixture_facts, manifest


def spreader_fixture():
    texts = [
        "The self-propelled drive is too fast. The tires slip on wet grass.",
        "The hopper tips over on a slope. Turning around is difficult.",
        "The shut-off gate leaks fertilizer when stopped. Calibration is difficult.",
        "The axle broke and the gearbox stopped working.",
        "The spread width is great. The edge guard keeps salt off the path.",
        "The gas engine is easy to start. Fuel lasts all day.",
        "Die Räder blockieren. Die Streumenge lässt sich einstellen.",
        "The handle folds for storage. The handlebar causes hand fatigue.",
        "My old handheld spreader hurt my wrist. This spreader works well.",
    ]
    facts = fixture_facts()[:len(texts)]
    for fact, text in zip(facts, texts):
        fact.update(category="Spreader", category_id="spreader", brand="Fixture",
                    product_title="Synthetic wheeled spreader", model="TEST-SP1",
                    product_form_factor="wheeled", review_title="", review_text=text,
                    age="", has_text=True, rating=2, rating_valid=True)
    snapshot = manifest(facts)
    snapshot["scope"].update(categories=["Spreader"], category_id="spreader",
                             product_form_factors=["wheeled"])
    return facts, build_report(facts, snapshot, report_name="轮式撒播机测试样本（非真实网评）")


class WheeledSpreaderTests(unittest.TestCase):
    def test_form_selection_preserves_family_and_handheld_profile(self):
        general = category_profile("Spreader")
        wheel = category_profile("spreader", product_form_factors=["wheeled"])
        self.assertEqual(wheel.category_id, "spreader")
        self.assertEqual(wheel.profile_id, "spreader-wheeled/1.0.0")
        for forms in ([], ["handheld"], ["unknown"], ["wheeled", "handheld"]):
            self.assertIs(category_profile("Spreader", product_form_factors=forms), general)
        self.assertEqual(category_profile("Handheld Spreader").profile_id, general.profile_id)

    def test_wheel_experiences_have_evidence_and_no_handheld_wrist_dimension(self):
        facts, source = spreader_fixture()
        report, ledger = build_ope_product_report(facts, source)
        self.assertEqual(report["category_profile_id"], "spreader-wheeled/1.0.0")
        aspects = {row["aspect_id"]: row for row in report["aspect_experience"]}
        for aspect in ("wheel_mobility", "drive_control", "stability", "gate_control",
                       "transmission", "metering", "edge_control", "engine", "transport"):
            with self.subTest(aspect=aspect):
                self.assertGreater(aspects[aspect]["review_entities"], 0)
                self.assertTrue(any(row.get("kind") == f"aspect:{aspect}" for row in report["evidence"]))
        self.assertNotIn("grip", aspects)
        self.assertEqual(aspects["ergonomics"]["label"], "扶手握持与操控疲劳")
        self.assertEqual(aspects["ergonomics"]["review_entities"], 1)
        self.assertEqual(aspects["wheel_mobility"]["review_entities"], 2)
        wheel = aspects["wheel_mobility"]
        self.assertEqual(wheel["evidence_population"], 2)
        self.assertEqual(wheel["full_evidence_entities"], 2)
        self.assertTrue(wheel["evidence_group_id"])
        evidence = {row["evidence_id"]: row for row in report["evidence"]}
        self.assertTrue(any("Räder" in evidence[key]["quote"]
                            for key in wheel["evidence_ids"]))
        self.assertTrue(ledger)
        self.assertIn("轮式撒播", report["hero_title"])

    def test_mixed_or_unknown_facts_cannot_enter_wheel_report(self):
        for form in ("handheld", "unknown", None):
            facts, source = spreader_fixture()
            facts[0]["product_form_factor"] = form
            with self.subTest(form=form), self.assertRaisesRegex(ValueError, "product_form"):
                build_ope_product_report(facts, source)

    def test_zero_and_small_samples_remain_unpublishable(self):
        scope = {"categories": ["Spreader"], "product_form_factors": ["wheeled"]}
        for count in (0, 149, 499):
            support = product_insight_support({"scope": scope}, valid_text_reviews=count,
                                              eligible_reviews=count)
            self.assertEqual(support["can_generate"], count > 0)
            self.assertEqual(support["category_profile_id"], "spreader-wheeled/1.0.0")
        self.assertTrue(product_insight_support({"scope": scope}, valid_text_reviews=500,
                                                eligible_reviews=500)["can_generate"])


if __name__ == "__main__":
    unittest.main()
