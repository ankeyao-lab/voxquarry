from __future__ import annotations

import unittest

from reporting.voltage_platform import (
    VoltagePlatformError,
    platform_catalog,
    resolve_voltage_platform_scope,
    voltage_platform_fact,
)


def job(
    job_id: str,
    *,
    status: str | None,
    key: str | None = None,
    brand: str | None = None,
    voltage: str | None = None,
    label: str | None = None,
    evidence_source: str | None = None,
    null_reason: str | None = None,
) -> dict:
    return {
        "id": job_id,
        "brand": brand or "",
        "voltage_platform_schema_version": (
            "voxquarry.voltage-platform/v1" if status is not None else None
        ),
        "voltage_platform_status": status,
        "voltage_platform_key": key,
        "voltage_platform_brand": brand,
        "voltage_platform_voltage": voltage,
        "voltage_platform_label": label,
        "voltage_platform_evidence_source": evidence_source,
        "voltage_platform_null_reason": null_reason,
        "product_observation_hash": "sha256:" + "a" * 64,
    }


class WotuVoltagePlatformContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ego = job(
            "ego-56",
            status="known",
            key="ego|56V",
            brand="EGO",
            voltage="56V",
            label="EGO 56V",
            evidence_source="product_master_exact_model",
        )
        self.other = job(
            "other-56",
            status="known",
            key="greenworks|56V",
            brand="Greenworks",
            voltage="56V",
            label="Greenworks 56V",
            evidence_source="source_observation",
        )
        self.unknown = job(
            "unknown",
            status="unknown",
            brand="EGO",
            null_reason="voltage_unknown",
            evidence_source="source_observation",
        )
        self.non_dc = job(
            "gas",
            status="non_dc",
            brand="Toro",
            null_reason="not_dc",
            evidence_source="source_observation",
            label="非 DC",
        )

    def test_known_platform_identity_keeps_same_voltage_brands_separate(self) -> None:
        ego = voltage_platform_fact(self.ego)
        other = voltage_platform_fact(self.other)
        self.assertEqual(ego["platform_key"], "ego|56V")
        self.assertEqual(other["platform_key"], "greenworks|56V")
        self.assertNotEqual(ego["platform_key"], other["platform_key"])
        self.assertEqual(
            [row["display_name"] for row in platform_catalog([self.ego, self.other])],
            ["EGO 56V", "Greenworks 56V"],
        )

    def test_known_platform_accepts_governed_dual_voltage(self) -> None:
        dual = job(
            "milwaukee-dual",
            status="known",
            key="milwaukee|12V/18V",
            brand="Milwaukee",
            voltage="12V/18V",
            label="Milwaukee 12V/18V",
            evidence_source="source_observation",
        )
        fact = voltage_platform_fact(dual)
        self.assertEqual(fact["canonical_voltage"], "12V/18V")
        self.assertEqual(fact["platform_key"], "milwaukee|12V/18V")

    def test_unknown_is_explicit_and_non_dc_is_not_an_unknown_platform(self) -> None:
        self.assertEqual(voltage_platform_fact(self.unknown)["status"], "unknown")
        self.assertEqual(voltage_platform_fact(self.non_dc)["status"], "non_dc")
        catalog = platform_catalog([self.unknown, self.non_dc])
        self.assertEqual(catalog, [])

    def test_legacy_task_without_platform_columns_is_audited_as_unknown(self) -> None:
        fact = voltage_platform_fact({"id": "legacy", "brand": "RYOBI"})
        self.assertEqual(fact["status"], "unknown")
        self.assertEqual(fact["null_reason"], "legacy_unbound")
        self.assertEqual(fact["schema_version"], None)

    def test_contradictory_or_malformed_platform_columns_fail_closed(self) -> None:
        invalid_rows = [
            {**self.ego, "voltage_platform_key": "greenworks|56V"},
            {**self.ego, "voltage_platform_voltage": "M18"},
            {**self.ego, "voltage_platform_schema_version": "voxquarry.voltage-platform/v0"},
            {**self.unknown, "voltage_platform_key": "ego|56V"},
            {**self.non_dc, "voltage_platform_null_reason": "voltage_unknown"},
            {**self.ego, "product_observation_hash": None},
        ]
        for row in invalid_rows:
            with self.subTest(row=row):
                with self.assertRaises(VoltagePlatformError):
                    voltage_platform_fact(row)

    def test_brand_token_matches_wotu_punctuation_boundary(self) -> None:
        black_decker = job(
            "black-decker-20",
            status="known",
            key="black-decker|20V",
            brand="BLACK+DECKER",
            voltage="20V",
            label="BLACK+DECKER 20V",
            evidence_source="request",
        )
        self.assertEqual(
            voltage_platform_fact(black_decker)["platform_key"],
            "black-decker|20V",
        )

    def test_unknown_brand_may_preserve_observed_voltage(self) -> None:
        unknown_brand = job(
            "unknown-brand",
            status="unknown",
            voltage="18V",
            null_reason="brand_unknown",
            evidence_source="request",
        )
        self.assertEqual(
            voltage_platform_fact(unknown_brand)["canonical_voltage"], "18V"
        )

    def test_scope_resolves_known_and_unknown_union_and_freezes_task_audit(self) -> None:
        resolved = resolve_voltage_platform_scope(
            {
                "category_id": "string_trimmer",
                "job_ids": ["ego-56", "unknown"],
                "voltage_platform_keys": ["ego|56V"],
                "include_unknown_voltage_platform": True,
                "voltage_platforms": [{"platform_key": "client-forged"}],
                "voltage_platform_refs": [{"job_id": "client-forged"}],
            },
            [self.ego, self.other, self.unknown, self.non_dc],
        )
        self.assertEqual(resolved["voltage_platform_keys"], ["ego|56V"])
        self.assertEqual(
            resolved["voltage_platforms"],
            [{
                "schema_version": "voxquarry.voltage-platform/v1",
                "platform_key": "ego|56V",
                "brand": "EGO",
                "canonical_voltage": "56V",
                "display_name": "EGO 56V",
            }],
        )
        self.assertEqual(
            [row["job_id"] for row in resolved["voltage_platform_refs"]],
            ["ego-56", "unknown"],
        )
        self.assertEqual(
            resolved["voltage_platform_refs"][1]["null_reason"],
            "voltage_unknown",
        )

    def test_scope_rejects_unknown_key_and_nonmatching_or_non_dc_jobs(self) -> None:
        with self.assertRaisesRegex(VoltagePlatformError, "未知电压平台"):
            resolve_voltage_platform_scope(
                {"job_ids": ["ego-56"], "voltage_platform_keys": ["ego|40V"]},
                [self.ego],
            )
        with self.assertRaisesRegex(VoltagePlatformError, "不属于所选电压平台"):
            resolve_voltage_platform_scope(
                {"job_ids": ["other-56"], "voltage_platform_keys": ["ego|56V"]},
                [self.ego, self.other],
            )
        with self.assertRaisesRegex(VoltagePlatformError, "不属于所选电压平台"):
            resolve_voltage_platform_scope(
                {
                    "job_ids": ["gas"],
                    "voltage_platform_keys": [],
                    "include_unknown_voltage_platform": True,
                },
                [self.non_dc],
            )


if __name__ == "__main__":
    unittest.main()
