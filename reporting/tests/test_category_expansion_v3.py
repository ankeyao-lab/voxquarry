from __future__ import annotations

import json
import re
from pathlib import Path
import unittest

from reporting.category_vocabulary import (
    CATEGORY_DEFINITIONS,
    category_catalog,
    category_id_for,
)
from reporting.analysis import build_report
from reporting.ope_product_report import CATEGORY_PROFILES, category_profile
from reporting.product_insight import build_product_insight_bundle
from reporting.tests.test_reporting import fixture_facts, manifest


EXPANSION_PROFILES = {
    "Air Compressor": ("air_compressor", "air-compressor/1.0.0"),
    "Auger": ("auger", "auger/1.0.0"),
    "Battery Pack": ("battery_pack", "battery-pack/1.0.0"),
    "Charger": ("charger", "charger/1.0.0"),
    "Cordless Stick Vacuum": (
        "cordless_stick_vacuum",
        "cordless-stick-vacuum/1.0.0",
    ),
    "Cultivator": ("cultivator", "cultivator/1.0.0"),
    "Cooler": ("cooler", "cooler/1.0.0"),
    "Edger": ("edger", "edger/1.0.0"),
    "Fan": ("fan", "fan/1.0.0"),
    "Generator": ("generator", "generator/1.0.0"),
    "Grass Shrub/Shear": (
        "grass_shrub_shear",
        "grass-shrub-shear/1.0.0",
    ),
    "Heater": ("heater", "heater/1.0.0"),
    "Hard Floor Cleaner": (
        "hard_floor_cleaner",
        "hard-floor-cleaner/1.0.0",
    ),
    "Light Bulb": ("light_bulb", "light-bulb/1.0.0"),
    "Log Splitter": ("log_splitter", "log-splitter/1.0.0"),
    "Misting Fan": ("misting_fan", "misting-fan/1.1.0"),
    "Mulcher": ("mulcher", "mulcher/1.0.0"),
    "Pool Vacuum": ("pool_vacuum", "pool-vacuum/1.0.0"),
    "Power Cleaner": ("power_cleaner", "power-cleaner/1.0.0"),
    "Power Scrubber": ("power_scrubber", "power-scrubber/1.0.0"),
    "Pruner": ("pruner", "pruner/1.0.0"),
    "Pump": ("pump", "pump/1.0.0"),
    "Raker": ("raker", "raker/1.0.0"),
    "Radio/Speaker": ("radio_speaker", "radio-speaker/1.0.0"),
    "Scarifier": ("scarifier", "scarifier/1.0.0"),
    "Seeder": ("seeder", "seeder/1.0.0"),
    "Shredder": ("shredder", "shredder/1.0.0"),
    "Sprayer": ("sprayer", "sprayer/1.1.0"),
    "Spot Cleaner": ("spot_cleaner", "spot-cleaner/1.0.0"),
    "Tiller/Cultivator": (
        "tiller_cultivator",
        "tiller-cultivator/1.0.0",
    ),
    "Vacuum Accessory": (
        "vacuum_accessory",
        "vacuum-accessory/1.0.0",
    ),
    "Weed Remover": ("weed_remover", "weed-remover/1.0.0"),
}


PROFILE_SMOKE_TEXT = {
    "Air Compressor": "The portable air compressor fills truck tires and runs a nailer at steady pressure.",
    "Auger": "The earth auger drills post holes through clay soil.",
    "Battery Pack": "The battery pack holds power under load and shows remaining charge.",
    "Charger": "The rapid charger cools a hot battery and reports charging status.",
    "Cordless Stick Vacuum": "The cordless stick vacuum picks pet hair from carpet and hardwood floors.",
    "Cultivator": "The cultivator loosens compact garden soil between rows.",
    "Cooler": "The portable cooler keeps food and drinks cold through a full jobsite day.",
    "Edger": "The edger cuts a clean edge along my driveway.",
    "Fan": "The portable fan moves quiet airflow through my garage.",
    "Generator": "The generator powers my refrigerator during an outage.",
    "Grass Shrub/Shear": "The grass shear trims boxwood and lawn edges.",
    "Heater": "The heater warms my workshop with adjustable heat.",
    "Hard Floor Cleaner": "The hard floor cleaner washes tile and picks up dirty water without streaks.",
    "Light Bulb": "The LED bulb is bright with warm color temperature.",
    "Log Splitter": "The log splitter splits oak rounds without stalling.",
    "Misting Fan": "The misting fan cools our patio with a fine mist.",
    "Mulcher": "The leaf mulcher shreds dry leaves into a collection bag.",
    "Pool Vacuum": "The pool vacuum picks up sand from the pool floor.",
    "Power Cleaner": "The portable power cleaner draws water from a bucket and rinses mud from my truck.",
    "Power Scrubber": "The power scrubber removes soap scum from shower tile and grout.",
    "Pruner": "The cordless pruner cuts thick orchard branches cleanly.",
    "Pump": "The transfer pump drains standing water from my basement.",
    "Raker": "The power rake removes thatch from the lawn.",
    "Radio/Speaker": "The jobsite radio pairs over Bluetooth and stays loud in the workshop.",
    "Scarifier": "The scarifier cuts grooves and removes moss from turf.",
    "Seeder": "The spreader seeds an acre with even coverage.",
    "Shredder": "The garden shredder chips branches into mulch.",
    "Sprayer": "The backpack sprayer applies herbicide with steady pressure.",
    "Spot Cleaner": "The portable spot cleaner removes pet stains from carpet and extracts dirty water.",
    "Tiller/Cultivator": "The tiller breaks compact soil for a vegetable bed.",
    "Vacuum Accessory": "The crevice nozzle fits my shop vacuum hose.",
    "Weed Remover": "The weed remover pulls dandelions out by the root.",
}


class CategoryExpansionV3Tests(unittest.TestCase):
    def test_pressure_washer_profile_is_category_pure_and_well_spaced(
        self,
    ) -> None:
        facts = fixture_facts()
        for fact in facts:
            fact["category"] = "Pressure Washer"
            fact["review_text"] = (
                "I run a pressure washing business. I clean concrete "
                "driveways with a pressure hose and turbo nozzle. "
                + fact["review_text"]
            )
            fact["has_text"] = True
        source_manifest = manifest(facts)
        source_manifest["scope"] = {
            **source_manifest["scope"],
            "category_id": "pressure_washer",
            "categories": ["Pressure Washer"],
            "exclude_categories": [],
        }
        source = build_report(facts, source_manifest)
        report, _, _, _ = build_product_insight_bundle(facts, source)
        insight = report["product_insight"]
        payload = json.dumps(insight, ensure_ascii=False)
        self.assertEqual(
            insight["category_profile_id"],
            "pressure-washer/1.2.1",
        )
        self.assertIn("完成 Pressure Washer 工作任务", payload)
        for forbidden in (
            "吹风机",
            "吹扫",
            "Pressure Washer任务",
            "Pressure Washer工作任务",
            "完成Pressure Washer",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, payload)

    def test_verified_expansion_categories_have_stable_full_profiles(
        self,
    ) -> None:
        self.assertEqual(len(CATEGORY_DEFINITIONS), 89)
        self.assertEqual(len(CATEGORY_PROFILES), 89)
        for display_name, (category_id, profile_id) in (
            EXPANSION_PROFILES.items()
        ):
            with self.subTest(category=display_name):
                self.assertEqual(category_id_for(display_name), category_id)
                profile = category_profile(display_name)
                self.assertIsNotNone(profile)
                self.assertEqual(profile.category_id, category_id)
                self.assertEqual(profile.profile_id, profile_id)
                self.assertTrue(profile.hero_title)
                self.assertGreaterEqual(len(profile.aspect_rules), 8)
                self.assertTrue(
                    {
                        "task_context",
                        "material_context",
                        "workflow_context",
                    }.issubset(
                        {
                            dimension.dimension_id
                            for dimension in profile.dimensions
                        }
                    )
                )

    def test_independent_master_categories_are_never_merged(self) -> None:
        self.assertNotEqual(
            category_id_for("Light Bulb"),
            category_id_for("LED Lighting"),
        )
        self.assertNotEqual(
            category_id_for("Misting Fan"),
            category_id_for("Fan"),
        )
        self.assertEqual(category_id_for("Jobsite Fan"), "fan")
        self.assertEqual(category_id_for("jobsite_fan"), "fan")
        self.assertNotEqual(
            category_id_for("Pump"),
            category_id_for("Inflator"),
        )
        self.assertEqual(category_id_for("Patio Cleaner"), "patio_cleaner")
        self.assertEqual(
            category_profile("Patio Cleaner").profile_id,
            "patio-cleaner/1.0.0",
        )
        self.assertEqual(category_id_for("Power Scrubber"), "power_scrubber")
        self.assertEqual(
            category_id_for("Cordless Stick Vacuum"),
            "cordless_stick_vacuum",
        )
        self.assertEqual(category_id_for("Radio/Speaker"), "radio_speaker")
        self.assertEqual(category_id_for("Speaker"), "radio_speaker")
        self.assertEqual(category_id_for("Spin Scrubber"), "power_scrubber")
        self.assertEqual(category_id_for("Floor Washer"), "hard_floor_cleaner")
        self.assertEqual(category_id_for("Rapid Charger"), "charger")
        self.assertEqual(category_id_for("Tool Battery"), "battery_pack")
        self.assertEqual(category_id_for("Jobsite Cooler"), "cooler")
        self.assertEqual(category_id_for("Portable Air Compressor"), "air_compressor")
        self.assertEqual(category_id_for("Carpet Spot Cleaner"), "spot_cleaner")
        self.assertEqual(
            category_id_for("Handstick Vacuum Cleaner"),
            "cordless_stick_vacuum",
        )
        self.assertNotEqual(
            category_id_for("Power Scrubber"),
            category_id_for("Patio Cleaner"),
        )
        self.assertNotEqual(
            category_id_for("Cordless Stick Vacuum"),
            category_id_for("Cordless Handheld Vacuum"),
        )
        self.assertNotEqual(
            category_id_for("Radio/Speaker"),
            category_id_for("Fan"),
        )
        self.assertNotEqual(
            category_id_for("Hard Floor Cleaner"),
            category_id_for("Spot Cleaner"),
        )
        self.assertNotEqual(
            category_id_for("Air Compressor"),
            category_id_for("Inflator"),
        )

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    def test_expansion_smoke_text_matches_category_specific_rules(
        self,
    ) -> None:
        for display_name, text in PROFILE_SMOKE_TEXT.items():
            with self.subTest(category=display_name):
                profile = category_profile(display_name)
                self.assertIsNotNone(profile)
                scenario_rules = [
                    rule
                    for dimension in profile.dimensions
                    if dimension.dimension_id
                    in {
                        "task_context",
                        "material_context",
                        "workflow_context",
                    }
                    for rule in dimension.rules
                ]
                self.assertTrue(
                    any(
                        re.search(rule.expression, text, re.IGNORECASE)
                        for rule in scenario_rules
                    )
                )
                self.assertTrue(
                    any(
                        re.search(rule.expression, text, re.IGNORECASE)
                        for rule in profile.aspect_rules
                    )
                )

    def test_every_expansion_profile_builds_a_full_traceable_report(
        self,
    ) -> None:
        for display_name, (category_id, profile_id) in (
            EXPANSION_PROFILES.items()
        ):
            with self.subTest(category=display_name):
                facts = fixture_facts()
                for fact in facts:
                    fact["category"] = display_name
                    fact["review_text"] = (
                        f"{PROFILE_SMOKE_TEXT[display_name]} "
                        f"{fact['review_text']}"
                    )
                    fact["has_text"] = True
                source_manifest = manifest(facts)
                source_manifest["scope"] = {
                    **source_manifest["scope"],
                    "category_id": category_id,
                    "categories": [display_name],
                    "exclude_categories": [],
                }
                source = build_report(facts, source_manifest)
                report, evidence, groups, ledger = (
                    build_product_insight_bundle(facts, source)
                )
                self.assertEqual(
                    report["product_insight_availability"]["status"],
                    "full",
                )
                self.assertEqual(
                    report["product_insight"]["category_profile_id"],
                    profile_id,
                )
                smart_rows = report["product_insight"][
                    "smart_opportunities"
                ]
                self.assertTrue(
                    all(
                        row.get("user_need")
                        and row.get("smart_function")
                        and row.get("opportunity_rank_within_type")
                        and row.get("methodology_core")
                        for row in smart_rows
                    )
                )
                self.assertLessEqual(
                    len({row["code"] for row in smart_rows}),
                    8,
                )
                self.assertGreater(len(evidence), 0)
                self.assertGreater(len(groups), 0)
                self.assertGreater(len(ledger), 0)

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.

    # Legacy presentation/deployment assertion replaced by public workbench integration tests.


if __name__ == "__main__":
    unittest.main()
