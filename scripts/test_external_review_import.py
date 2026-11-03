#!/usr/bin/env python3
"""Contract tests for governed Home Depot/Amazon external review imports."""

import copy
import csv
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRAPER = ROOT / "Review Scraper" / "review-scraper-service"
sys.path.insert(0, str(SCRAPER))

from collector.external_import import (  # noqa: E402
    ExternalImportError,
    external_import_format_requirements,
    IMPORT_VALIDATION_SCHEMA_VERSION,
    validate_import_bundle,
    write_import_artifacts,
)
from collector.promote_external_import_partial import main as promotion_cli_main  # noqa: E402
from collector.store import EXTERNAL_IMPORT_ACTION, Store  # noqa: E402


def review(review_id="r-1", **overrides):
    row = {
        "review_id": review_id,
        "product_id": "300510172",
        "source_product_id": "300510172",
        "product_url": "https://www.homedepot.com/p/example/300510172",
        "submission_time": "2026-07-01T12:00:00Z",
        "title": "Works well",
        "user_nickname": "Reviewer",
        "review_text": "This tool works very well outdoors.",
        "rating": 5,
        "verified_purchaser": True,
        "is_recommended": True,
        "where_purchase": "Home Depot",
        "age": "",
        "review_locale": "en_US",
        "is_syndicated": False,
        "is_translated": False,
        "origin_source_name": "",
        "origin_source_url": "",
        "origin_source_site": "",
    }
    row.update(overrides)
    return row


def bundle():
    return {
        "schema_version": "voxquarry.review-import/v1",
        "import_id": "hd-300510172-20260701",
        "channel": "homedepot",
        "task_policy": "create_or_append_attempt",
        "target_url": "https://www.homedepot.com/p/example/300510172",
        "market_region": "North America",
        "product": {
            "id": "300510172",
            "title": "Example Cordless Tool",
            "brand": "DEWALT",
            "model": "EXAMPLE-1",
            "category": "Inflator",
        },
        "completeness": {
            "is_complete": True,
            "source_total": 2,
            "source_total_basis": "user_review_records",
            "collected_at": "2026-07-02T01:02:03Z",
            "method": "licensed_export",
            "evidence_url": "https://www.homedepot.com/p/example/300510172",
            "syndication_origin_complete": True,
        },
        "reviews": [
            review(),
            review(
                "r-2",
                review_text="Good.",
                is_syndicated=True,
                origin_source_name="EGO Power+",
                origin_source_url="https://egopowerplus.com/product/example",
                origin_source_site="egopowerplus.com",
            ),
        ],
    }


def partial_bundle(source_total=5):
    payload = bundle()
    payload["import_id"] = "hd-300510172-partial-20260814"
    payload["completeness"]["is_complete"] = False
    payload["completeness"]["source_total"] = source_total
    return payload


def walmart_bundle(*, complete=True, source_total=2):
    payload = bundle()
    payload.update(
        {
            "import_id": "walmart-1038662239-20260826",
            "channel": "walmart",
            "target_url": (
                "https://www.walmart.com/ip/Simpson-Pressure-Washer/1038662239"
            ),
        }
    )
    payload["market_region"] = "North America"
    payload["product"] = {
        "id": "1038662239",
        "title": "Simpson Pressure Washer",
        "brand": "Simpson",
        "model": "",
        "category": "Gas Pressure Washer",
    }
    payload["completeness"].update(
        {
            "is_complete": complete,
            "source_total": source_total,
            "evidence_url": payload["target_url"],
            "note": "Official total includes ratings-only records.",
        }
    )
    payload["reviews"] = [
        review(
            "w-1",
            product_id="1038662239",
            source_product_id="1038662239",
            product_url=payload["target_url"],
            submission_time="2026-06-14",
            origin_source_name="Walmart",
            origin_source_url=payload["target_url"],
            origin_source_site="walmart.com",
        ),
        review(
            "w-2",
            product_id="1038662239",
            source_product_id="1038662239",
            product_url=payload["target_url"],
            submission_time="2026-06-15",
            origin_source_name="Walmart",
            origin_source_url=payload["target_url"],
            origin_source_site="walmart.com",
        ),
    ]
    return payload


BROWSER_FALLBACK_URLS = {
    "ace": "https://www.acehardware.com/p/7039887",
    "tractorsupply": "https://www.tractorsupply.com/tsc/product/example-sprayer",
    "harborfreight": "https://www.harborfreight.com/example-sprayer-73147.html",
    "husqvarna": "https://www.husqvarna.com/us/sprayers/example-sprayer/",
    "stihl": "https://www.stihlusa.com/products/sprayers/backpack-sprayers/example/#aID=42440111600",
}


def browser_bundle(channel="homedepot"):
    payload = bundle()
    url = BROWSER_FALLBACK_URLS.get(channel, payload["target_url"])
    if channel == "walmart":
        url = "https://www.walmart.com/ip/example/300510172"
    elif channel == "amazon_live":
        url = "https://www.amazon.com/dp/B012345678"
    payload.update(
        schema_version="voxquarry.review-import/v2",
        channel=channel,
        import_id=f"browser-{channel}-fixture-20261010",
        target_url=url,
    )
    payload["product"].update(energy_type="battery", voltage="20V")
    payload["completeness"].update(
        method="browser_public_capture", evidence_url=url,
    )
    for row in payload["reviews"]:
        row["product_url"] = url
    return payload


class ExternalReviewImportTests(unittest.TestCase):
    def test_browser_capture_preserves_method_and_original_channel(self):
        for channel in ("homedepot", "walmart", "amazon_live", *BROWSER_FALLBACK_URLS):
            with self.subTest(channel=channel):
                validated = validate_import_bundle(browser_bundle(channel))
                self.assertEqual(validated["channel"], channel)
                self.assertEqual(validated["completeness"]["method"], "browser_public_capture")
                self.assertEqual(validated["task_product_metadata"]["energy_type"], "battery")
                self.assertEqual(validated["task_product_metadata"]["voltage"], "20V")
                with tempfile.TemporaryDirectory() as temp_dir:
                    stage = Path(temp_dir) / channel
                    report = write_import_artifacts(stage, validated)
                    self.assertEqual(report["source_total"], 2)
                    raw = [json.loads(line) for line in (stage / "reviews.raw.jsonl").read_text().splitlines()]
                    self.assertEqual(raw, browser_bundle(channel)["reviews"])
                    with (stage / "reviews.csv").open(encoding="utf-8-sig") as handle:
                        self.assertTrue(all(row["channel"] == channel for row in csv.DictReader(handle)))

    def test_browser_capture_does_not_relax_identity_counts_or_metadata(self):
        mutations = (
            lambda p: p["product"].update(voltage=None),
            lambda p: p["reviews"][0].update(product_id="wrong-product"),
            lambda p: p["reviews"][0].update(product_url=BROWSER_FALLBACK_URLS["ace"] + "1"),
            lambda p: p["reviews"][0].update(review_id=""),
            lambda p: p["reviews"][0].update(submission_time="2026-10-10"),
            lambda p: p["reviews"][1].update(origin_source_site=""),
            lambda p: p["completeness"].update(source_total=3),
            lambda p: p["completeness"].update(source_total_basis="aggregate_ratings"),
            lambda p: p["completeness"].update(evidence_url="https://www.acehardware.com/p/9999999"),
            lambda p: p.update(cookie="must-not-upload"),
        )
        for mutate in mutations:
            payload = browser_bundle("ace")
            mutate(payload)
            with self.subTest(payload=payload), self.assertRaises(ExternalImportError):
                validate_import_bundle(payload)

    def test_new_import_channels_require_v2_and_exact_official_source(self):
        for channel in BROWSER_FALLBACK_URLS:
            payload = browser_bundle(channel)
            payload["schema_version"] = "voxquarry.review-import/v1"
            payload["product"].pop("energy_type")
            payload["product"].pop("voltage")
            payload["completeness"]["method"] = "licensed_export"
            with self.subTest(channel=channel), self.assertRaises(ExternalImportError):
                validate_import_bundle(payload)
            for url in ("https://example.com/p/product", "https://www.acehardware.com.evil.test/p/7039887"):
                payload = browser_bundle(channel)
                payload["target_url"] = url
                with self.assertRaises(ExternalImportError):
                    validate_import_bundle(payload)
        payload = browser_bundle()
        payload["schema_version"] = "voxquarry.review-import/v1"
        payload["product"].pop("energy_type")
        payload["product"].pop("voltage")
        with self.assertRaises(ExternalImportError):
            validate_import_bundle(payload)
        payload = browser_bundle("husqvarna")
        payload["target_url"] = payload["target_url"].replace("/us/", "/uk/")
        with self.assertRaises(ExternalImportError):
            validate_import_bundle(payload)

    def test_browser_fallback_import_keeps_failed_attempt_and_stable_task(self):
        from collector.runner import COLLECTION_CHANNELS
        self.assertTrue(set(BROWSER_FALLBACK_URLS) <= COLLECTION_CHANNELS)
        for channel in BROWSER_FALLBACK_URLS:
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as temp_dir:
                from unittest.mock import patch
                with patch.dict(os.environ, {"DATA_DIR": temp_dir}):
                    store = Store()
                    payload = browser_bundle(channel)
                    store.create_job(
                        "fallback-job", action="scrape", channel=channel,
                        target_url=payload["target_url"], brand="DEWALT",
                        model="EXAMPLE-1", category="Inflator", market_region="North America",
                    )
                    store.next_job(channel)
                    store.fail("fallback-job", "source access failed")
                    failed_attempts = store.list_attempts("fallback-job")
                    validated = validate_import_bundle(payload)
                    stage = Path(temp_dir) / "stage" / channel
                    report = write_import_artifacts(stage, validated)
                    result = store.publish_external_import(
                        "fallback-job", channel=channel, target_url=payload["target_url"],
                        brand="DEWALT", model="EXAMPLE-1", category="Inflator",
                        market_region="North America", source_product_id="300510172",
                        result={channel: report}, staged_run_dir=stage,
                        audit_detail={"import_id": payload["import_id"], "upload_sha256": "fixture"},
                    )
                    self.assertEqual(result["id"], "fallback-job")
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(result["attempt_count"], 2)
                    attempts = store.list_attempts("fallback-job")
                    self.assertIn(failed_attempts[0], attempts)
                    self.assertEqual(store.find_target(channel, payload["target_url"])["id"], "fallback-job")

    def test_stihl_current_us_product_path_preserves_native_product_identity(self):
        url = "https://www.stihlusa.com/en/p/mistblowers-sprayers-sr-450-gasoline-backpack-sprayer-1027378"
        payload = browser_bundle("stihl")
        payload["target_url"] = url
        payload["completeness"]["evidence_url"] = url
        for row in payload["reviews"]:
            row["product_url"] = url
            row["source_product_id"] = "42440112643US"
        validated = validate_import_bundle(payload)
        self.assertEqual(validated["source_rows"][0]["source_product_id"], "42440112643US")
        for invalid in (
            url.replace("stihlusa.com", "stihl.com"),
            url.replace("/en/p/", "/en-gb/p/"),
            "https://www.stihlusa.com/en/p/sprayers",
            "https://www.stihlusa.com/en/categories/sprayers-1027378",
        ):
            rejected = copy.deepcopy(payload)
            rejected["target_url"] = invalid
            with self.subTest(url=invalid), self.assertRaises(ExternalImportError):
                validate_import_bundle(rejected)

    def test_walmart_adapter_accepts_optional_metadata_and_governed_note(self):
        payload = walmart_bundle()
        payload["product"]["brand"] = ""
        payload["product"]["category"] = ""
        validated = validate_import_bundle(payload)
        self.assertEqual(validated["channel"], "walmart")
        self.assertEqual(validated["product"]["id"], "1038662239")
        self.assertEqual(validated["product"]["model"], "")
        self.assertEqual(validated["product"]["brand"], "")
        self.assertEqual(
            validated["completeness"]["note"],
            "Official total includes ratings-only records.",
        )
        schema = json.loads(
            (ROOT / "docs/connectors/external-review-import-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        example = json.loads(
            (ROOT / "docs/connectors/external-review-import-v1.walmart.example.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("walmart", schema["properties"]["channel"]["enum"])
        self.assertEqual(validate_import_bundle(example)["channel"], "walmart")

    def test_v2_normalizes_pressure_washer_metadata_and_fails_closed(self):
        payload = walmart_bundle()
        payload["schema_version"] = "voxquarry.review-import/v2"
        payload["product"].update({
            "brand": "Simpson",
            "model": "PS3228",
            "category": "Gas Pressure Washer",
            "energy_type": "fuel",
            "voltage": None,
        })
        validated = validate_import_bundle(payload)
        self.assertEqual(validated["product"]["category"], "Pressure Washer")
        self.assertEqual(validated["product"]["energy_type"], "fuel")
        self.assertEqual(
            validated["task_product_metadata"]["market_region"],
            "North America",
        )

        battery = copy.deepcopy(payload)
        battery["product"].update({
            "title": "Simpson Cordless Pressure Washer",
            "energy_type": "battery",
            "voltage": None,
        })
        with self.assertRaisesRegex(ExternalImportError, "source-proven voltage"):
            validate_import_bundle(battery)

        hybrid = copy.deepcopy(payload)
        hybrid["product"].update({
            "title": "60V/AC Hybrid Pressure Washer",
            "brand": "Greenworks",
            "model": "PW3000HYB",
            "energy_type": "battery",
            "voltage": "60V",
        })
        validated_hybrid = validate_import_bundle(hybrid)
        self.assertEqual(validated_hybrid["product"]["energy_type"], "other")

        schema = json.loads(
            (ROOT / "docs/connectors/external-review-import-v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        example = json.loads(
            (ROOT / "docs/connectors/external-review-import-v2.example.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["schema_version"]["const"], payload["schema_version"])
        self.assertEqual(validate_import_bundle(example)["product"]["energy_type"], "other")

    def test_walmart_adapter_fails_closed_on_host_identity_and_overage(self):
        wrong_host = walmart_bundle()
        wrong_host["target_url"] = "https://example.com/ip/item/1038662239"
        wrong_id = walmart_bundle()
        wrong_id["product"]["id"] = "999999"
        overage = walmart_bundle(complete=False, source_total=1)
        for payload, message in (
            (wrong_host, "host does not match walmart"),
            (wrong_id, "does not match target_url item identity"),
            (overage, "cannot be smaller"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ExternalImportError, message):
                    validate_import_bundle(payload)

    def test_walmart_complete_and_partial_publish_under_stable_cache_task(self):
        with tempfile.TemporaryDirectory(prefix="walmart-import-store-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                payload = walmart_bundle()
                validated = validate_import_bundle(payload)
                store.create_job(
                    "walmartcache1",
                    EXTERNAL_IMPORT_ACTION,
                    "walmart",
                    validated["target_url"],
                    None,
                    None,
                    None,
                    market_region="North America",
                )
                stage = Path(temp_dir) / "walmart-stage" / "walmart"
                report = write_import_artifacts(stage, validated)
                published = store.publish_external_import(
                    "walmartcache1",
                    channel="walmart",
                    target_url=validated["target_url"],
                    brand=None,
                    model=None,
                    category=None,
                    market_region="North America",
                    source_product_id="1038662239",
                    result={"walmart": report},
                    staged_run_dir=stage,
                    audit_detail={
                        "import_id": validated["import_id"],
                        "upload_sha256": "walmart-one",
                    },
                )
                self.assertEqual(published["id"], "walmartcache1")
                self.assertEqual(published["status"], "completed")
                self.assertEqual(published["attempt_count"], 1)
                self.assertEqual(store.list_external_import_queue("walmart"), [])
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir
    def test_validates_all_rows_and_recomputes_contract_fields(self):
        result = validate_import_bundle(bundle())
        self.assertEqual(result["source_total"], 2)
        self.assertEqual(len(result["source_rows"]), 2)
        self.assertEqual(len(result["eligible_rows"]), 1)
        self.assertEqual(result["eligible_rows"][0]["review_word_count"], 6)
        self.assertTrue(result["eligible_rows"][0]["is_valid_text_review"])
        excluded = result["source_rows"][1]
        self.assertFalse(excluded["is_eligible"])
        self.assertEqual(excluded["exclusion_reason"], "syndicated_from_ego_official")
        self.assertTrue(excluded["is_valid_text_review"])

    def test_writes_standard_artifacts_without_importing_ego_syndication(self):
        result = validate_import_bundle(bundle())
        with tempfile.TemporaryDirectory(prefix="review-import-test-") as temp_dir:
            artifact_dir = Path(temp_dir) / "homedepot"
            report = write_import_artifacts(artifact_dir, result)
            raw_lines = (artifact_dir / "reviews.raw.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            csv_lines = (artifact_dir / "reviews.csv").read_text(
                encoding="utf-8-sig"
            ).splitlines()
            self.assertEqual(len(raw_lines), 2)
            self.assertEqual(len(csv_lines), 2)  # header + one eligible row
            self.assertEqual(report["status"], "COMPLETE")
            self.assertEqual(report["source_unique_reviews"], 2)
            self.assertEqual(report["eligible_reviews"], 1)
            self.assertEqual(report["excluded_ego_origin_reviews"], 1)
            self.assertTrue((artifact_dir / "validation_report.json").is_file())

    def test_accepts_nonempty_declared_incomplete_import_as_partial_success(self):
        result = validate_import_bundle(partial_bundle())
        self.assertEqual(result["publication_status"], "partial_success")
        self.assertEqual(result["source_total"], 5)
        self.assertEqual(result["source_records_collected"], 2)
        self.assertEqual(result["source_missing_records"], 3)
        self.assertEqual(
            result["stop_reason"], "external_import_declared_incomplete"
        )

        with tempfile.TemporaryDirectory(prefix="review-import-partial-") as temp_dir:
            artifact_dir = Path(temp_dir) / "homedepot"
            report = write_import_artifacts(artifact_dir, result)
            self.assertEqual(report["status"], "PARTIAL_SUCCESS")
            self.assertEqual(
                report["validation_schema_version"], IMPORT_VALIDATION_SCHEMA_VERSION
            )
            self.assertFalse(report["source_reconciled"])
            self.assertEqual(report["source_total"], 5)
            self.assertEqual(report["source_records_collected"], 2)
            self.assertEqual(report["source_missing_records"], 3)
            self.assertEqual(
                report["stop_reason"], "external_import_declared_incomplete"
            )
            manifest = json.loads(
                (artifact_dir / "selection_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["publication_status"], "partial_success")
            self.assertEqual(manifest["source_records_collected"], 2)
            self.assertEqual(manifest["source_missing_records"], 3)

    def test_partial_validation_schema_and_example_lock_down_downstream_contract(self):
        schema_path = (
            ROOT
            / "docs"
            / "connectors"
            / "external-review-import-validation-v1.schema.json"
        )
        example_path = (
            ROOT
            / "docs"
            / "connectors"
            / "external-review-import-validation-v1.partial.example.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        example = json.loads(example_path.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["properties"]["validation_schema_version"]["const"],
            IMPORT_VALIDATION_SCHEMA_VERSION,
        )
        self.assertEqual(
            example["validation_schema_version"], IMPORT_VALIDATION_SCHEMA_VERSION
        )
        self.assertEqual(example["status"], "PARTIAL_SUCCESS")
        self.assertFalse(example["source_reconciled"])
        self.assertEqual(
            example["stop_reason"], "external_import_declared_incomplete"
        )
        self.assertIn(
            "partial_never_replaces_reportable_completed_active",
            schema["x-voxquarry-active-result-selection"],
        )

    def test_partial_export_requires_a_positive_provable_gap(self):
        no_gap = partial_bundle(source_total=2)
        reversed_gap = partial_bundle(source_total=1)
        empty = partial_bundle(source_total=2)
        empty["reviews"] = []
        falsely_complete = partial_bundle()
        falsely_complete["completeness"]["is_complete"] = True
        for payload, expected in (
            (no_gap, "must exceed reviews length"),
            (reversed_gap, "cannot be smaller"),
            (empty, "non-empty"),
            (falsely_complete, "does not equal reviews length"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ExternalImportError, expected):
                    validate_import_bundle(payload)

    def test_store_rejects_partial_artifacts_with_inconsistent_declared_gap(self):
        with tempfile.TemporaryDirectory(prefix="review-import-partial-gate-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                validated = validate_import_bundle(partial_bundle())
                stage = Path(temp_dir) / "partial-stage" / "homedepot"
                report = write_import_artifacts(stage, validated)
                artifact_report = json.loads(
                    (stage / "validation_report.json").read_text(encoding="utf-8")
                )
                artifact_report["source_reconciled"] = True
                (stage / "validation_report.json").write_text(
                    json.dumps(artifact_report), encoding="utf-8"
                )

                with self.assertRaisesRegex(
                    ValueError, "validation report does not match raw import evidence"
                ):
                    store.publish_external_import(
                        "partialgate01",
                        channel="homedepot",
                        target_url=validated["target_url"],
                        brand="DEWALT",
                        model="EXAMPLE-1",
                        category="Inflator",
                        market_region="North America",
                        source_product_id="300510172",
                        result={"homedepot": report},
                        staged_run_dir=stage,
                        audit_detail={
                            "import_id": validated["import_id"],
                            "upload_sha256": "partial-gate",
                        },
                    )
                self.assertTrue(stage.is_dir())
                self.assertIsNone(store.get_job("partialgate01"))
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir

    def test_historic_partial_stop_reason_is_read_only_compatible(self):
        validated = validate_import_bundle(partial_bundle())
        with tempfile.TemporaryDirectory(prefix="review-import-partial-legacy-") as temp_dir:
            stage = Path(temp_dir) / "homedepot"
            write_import_artifacts(stage, validated)
            for name in ("validation_report.json", "selection_manifest.json"):
                path = stage / name
                value = json.loads(path.read_text(encoding="utf-8"))
                value["stop_reason"] = "external_export_incomplete"
                if name == "validation_report.json":
                    del value["validation_schema_version"]
                path.write_text(json.dumps(value), encoding="utf-8")
            verified = Store._verify_external_import_artifact_projection(
                stage,
                expected_channel="homedepot",
                expected_target_key=validated["target_key"],
                expected_market_region="North America",
                expected_source_product_id="300510172",
                allow_legacy_declared_incomplete=True,
            )
            self.assertEqual(verified["report"]["stop_reason"], "external_export_incomplete")
            with self.assertRaisesRegex(
                ValueError, "(?:selection manifest|validation report) does not match raw import evidence"
            ):
                Store._verify_external_import_artifact_projection(
                    stage,
                    expected_channel="homedepot",
                    expected_target_key=validated["target_key"],
                    expected_market_region="North America",
                    expected_source_product_id="300510172",
                )
            with self.assertRaisesRegex(ValueError, "partial artifact counts"):
                Store._external_import_partial_artifact_counts(
                    stage,
                    require_declared_incomplete_reason=True,
                    require_current_validation_schema=True,
                )

    def test_store_rejects_partial_csv_identity_and_content_tampering(self):
        with tempfile.TemporaryDirectory(prefix="review-import-partial-csv-gate-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                validated = validate_import_bundle(partial_bundle())
                stage = Path(temp_dir) / "partial-stage" / "homedepot"
                report = write_import_artifacts(stage, validated)
                csv_path = stage / "reviews.csv"
                with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    fields = reader.fieldnames
                    rows = list(reader)
                rows[0].update(
                    {
                        "channel": "amazon_live",
                        "product_url": "https://www.amazon.com/dp/B000000000",
                        "market_region": "Europe",
                        "rating": "1",
                        "review_text": "tampered",
                    }
                )
                with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)

                with self.assertRaisesRegex(ValueError, "CSV does not match raw import evidence"):
                    store.publish_external_import(
                        "partialcsv01",
                        channel="homedepot",
                        target_url=validated["target_url"],
                        brand="DEWALT",
                        model="EXAMPLE-1",
                        category="Inflator",
                        market_region="North America",
                        source_product_id="300510172",
                        result={"homedepot": report},
                        staged_run_dir=stage,
                        audit_detail={
                            "import_id": validated["import_id"],
                            "upload_sha256": "partial-csv-gate",
                        },
                    )
                self.assertTrue(stage.is_dir())
                self.assertIsNone(store.get_job("partialcsv01"))
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir

    def test_store_rejects_partial_raw_manifest_and_validation_tampering(self):
        cases = (
            ("raw", "product_id does not match product.id"),
            ("manifest", "product_url does not match target_url"),
            ("validation", "validation report does not match raw import evidence"),
        )
        for kind, expected in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                prefix="review-import-partial-projection-gate-"
            ) as temp_dir:
                prior_data_dir = os.environ.get("DATA_DIR")
                os.environ["DATA_DIR"] = temp_dir
                try:
                    store = Store()
                    validated = validate_import_bundle(partial_bundle())
                    stage = Path(temp_dir) / "partial-stage" / "homedepot"
                    report = write_import_artifacts(stage, validated)
                    if kind == "raw":
                        raw_path = stage / "reviews.raw.jsonl"
                        rows = [
                            json.loads(line)
                            for line in raw_path.read_text(encoding="utf-8").splitlines()
                        ]
                        rows[0]["product_id"] = "other-product"
                        raw_path.write_text(
                            "".join(
                                json.dumps(row, ensure_ascii=False) + "\n" for row in rows
                            ),
                            encoding="utf-8",
                        )
                    elif kind == "manifest":
                        manifest_path = stage / "selection_manifest.json"
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                        manifest["target_url"] = (
                            "https://www.homedepot.com/p/other/999999999"
                        )
                        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    else:
                        report_path = stage / "validation_report.json"
                        artifact_report = json.loads(
                            report_path.read_text(encoding="utf-8")
                        )
                        artifact_report["rating_histogram"] = {"1": 2}
                        artifact_report["valid_text_reviews"] = 0
                        report_path.write_text(
                            json.dumps(artifact_report), encoding="utf-8"
                        )
                    with self.assertRaisesRegex(ValueError, expected):
                        store.publish_external_import(
                            f"projection{kind}",
                            channel="homedepot",
                            target_url=validated["target_url"],
                            brand="DEWALT",
                            model="EXAMPLE-1",
                            category="Inflator",
                            market_region="North America",
                            source_product_id="300510172",
                            result={"homedepot": report},
                            staged_run_dir=stage,
                            audit_detail={
                                "import_id": validated["import_id"] + kind,
                                "upload_sha256": "projection-" + kind,
                            },
                        )
                    self.assertTrue(stage.is_dir())
                    self.assertIsNone(store.get_job(f"projection{kind}"))
                finally:
                    if prior_data_dir is None:
                        os.environ.pop("DATA_DIR", None)
                    else:
                        os.environ["DATA_DIR"] = prior_data_dir

    def test_store_rejects_partial_result_json_that_differs_from_validation(self):
        with tempfile.TemporaryDirectory(prefix="review-import-partial-result-gate-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                validated = validate_import_bundle(partial_bundle())
                stage = Path(temp_dir) / "partial-stage" / "homedepot"
                report = write_import_artifacts(stage, validated)
                forged_result = copy.deepcopy(report)
                forged_result["source_reconciled"] = True
                forged_result["stop_reason"] = ""
                with self.assertRaisesRegex(
                    ValueError, "result does not match validated artifact report"
                ):
                    store.publish_external_import(
                        "partialresult01",
                        channel="homedepot",
                        target_url=validated["target_url"],
                        brand="DEWALT",
                        model="EXAMPLE-1",
                        category="Inflator",
                        market_region="North America",
                        source_product_id="300510172",
                        result={"homedepot": forged_result},
                        staged_run_dir=stage,
                        audit_detail={
                            "import_id": validated["import_id"],
                            "upload_sha256": "partial-result-gate",
                        },
                    )
                self.assertTrue(stage.is_dir())
                self.assertIsNone(store.get_job("partialresult01"))
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir

    def test_nonempty_short_text_is_valid_text_under_current_contract(self):
        payload = bundle()
        payload["reviews"] = [review(review_text="Good.")]
        payload["completeness"]["source_total"] = 1
        result = validate_import_bundle(payload)
        self.assertEqual(result["source_rows"][0]["review_word_count"], 1)
        self.assertTrue(result["source_rows"][0]["is_valid_text_review"])

    def test_rejects_missing_fields_duplicates_and_count_mismatch(self):
        cases = []
        missing = bundle()
        del missing["reviews"][0]["review_locale"]
        cases.append((missing, "review_locale"))
        duplicate = bundle()
        duplicate["reviews"][1] = copy.deepcopy(duplicate["reviews"][0])
        cases.append((duplicate, "duplicate"))
        mismatch = bundle()
        mismatch["completeness"]["source_total"] = 3
        cases.append((mismatch, "source_total"))
        conflicting = bundle()
        conflicting["reviews"][0]["is_eligible"] = False
        cases.append((conflicting, "is_eligible.*derived"))
        unknown = bundle()
        unknown["source"] = "typo"
        cases.append((unknown, "unsupported field source"))
        unknown_review = bundle()
        unknown_review["reviews"][0]["helpful_votes"] = 3
        cases.append((unknown_review, "unsupported field helpful_votes"))
        for payload, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ExternalImportError, expected):
                    validate_import_bundle(payload)

    def test_rejects_wrong_product_region_and_unproven_origin_evidence(self):
        wrong_product = bundle()
        wrong_product["reviews"][0]["product_id"] = "other"
        wrong_region = bundle()
        wrong_region["market_region"] = "Europe"
        no_origin_evidence = bundle()
        no_origin_evidence["completeness"]["syndication_origin_complete"] = False
        for payload, expected in (
            (wrong_product, "product_id"),
            (wrong_region, "market_region"),
            (no_origin_evidence, "syndication_origin_complete"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ExternalImportError, expected):
                    validate_import_bundle(payload)

    def test_accepts_live_amazon_for_europe_but_not_archive_channel(self):
        payload = bundle()
        payload["import_id"] = "amazon-b07fnwttx3-20260701"
        payload["channel"] = "amazon_live"
        payload["target_url"] = "https://www.amazon.co.uk/dp/B07FNWTTX3"
        payload["market_region"] = "Europe"
        payload["product"]["id"] = "B07FNWTTX3"
        payload["completeness"]["source_total"] = 1
        payload["completeness"]["evidence_url"] = payload["target_url"]
        payload["reviews"] = [review(
            product_id="B07FNWTTX3",
            source_product_id="B07FNWTTX3",
            product_url=payload["target_url"],
            where_purchase="Amazon UK",
        )]
        result = validate_import_bundle(payload)
        self.assertEqual(result["channel"], "amazon_live")
        self.assertEqual(result["market_region"], "Europe")

        payload["channel"] = "amazon_reviews_2023"
        with self.assertRaisesRegex(ExternalImportError, "channel"):
            validate_import_bundle(payload)

    def test_json_contract_has_no_secret_or_cookie_fields(self):
        serialized = json.dumps(bundle()).lower()
        for forbidden in ("cookie", "password", "authorization", "api_key"):
            self.assertNotIn(forbidden, serialized)

    def test_exported_format_requirements_are_complete_and_credential_free(self):
        requirements = external_import_format_requirements()
        self.assertEqual(
            requirements["schema_version"], "voxquarry.review-import/v2"
        )
        self.assertIn("review_id", requirements["required_review_fields"])
        self.assertIn("source_product_id", requirements["review_business_key"])
        self.assertIn("cookie", requirements["forbidden_input"])
        self.assertNotIn("task_id", requirements["required_review_fields"])

    def test_published_example_matches_runtime_contract(self):
        example = json.loads(
            (ROOT / "docs" / "connectors" / "external-review-import-v1.example.json").read_text(
                encoding="utf-8"
            )
        )
        schema = json.loads(
            (ROOT / "docs" / "connectors" / "external-review-import-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["schema_version"]["const"], example["schema_version"])
        self.assertEqual(validate_import_bundle(example)["source_total"], 1)

    def test_lawn_mower_import_resolves_to_production_master_category(self):
        payload = bundle()
        payload["product"]["category"] = "Lawn Mower"
        validated = validate_import_bundle(payload)
        self.assertEqual(validated["product"]["category"], "Lawn Mower")
        with tempfile.TemporaryDirectory(prefix="lawn-mower-master-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                self.assertEqual(
                    store.canonical_master_value(
                        "category", validated["product"]["category"]
                    ),
                    "Walk behind Lawnmower",
                )
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir

    def test_store_publishes_new_import_and_reuses_stable_task_id(self):
        with tempfile.TemporaryDirectory(prefix="review-import-store-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                validated = validate_import_bundle(bundle())
                first_stage = Path(temp_dir) / "stage-one" / "homedepot"
                report = write_import_artifacts(first_stage, validated)
                result = {"homedepot": report}
                created = store.publish_external_import(
                    "importjob001",
                    channel="homedepot",
                    target_url=validated["target_url"],
                    brand="DEWALT",
                    model="EXAMPLE-1",
                    category="Inflator",
                    market_region="North America",
                    source_product_id="300510172",
                    result=result,
                    staged_run_dir=first_stage,
                    audit_detail={"import_id": validated["import_id"], "upload_sha256": "one"},
                )
                self.assertEqual(created["status"], "completed")
                self.assertEqual(created["attempt_count"], 1)
                self.assertTrue(
                    (Path(temp_dir) / "runs" / "importjob001" / "homedepot" / "reviews.csv").is_file()
                )

                second_stage = Path(temp_dir) / "stage-two" / "homedepot"
                report_two = write_import_artifacts(second_stage, validated)
                updated = store.publish_external_import(
                    "importjob001",
                    channel="homedepot",
                    target_url=validated["target_url"],
                    brand="DEWALT",
                    model="EXAMPLE-1",
                    category="Inflator",
                    market_region="North America",
                    source_product_id="300510172",
                    result={"homedepot": report_two},
                    staged_run_dir=second_stage,
                    audit_detail={"import_id": validated["import_id"] + "-second", "upload_sha256": "two"},
                )
                self.assertEqual(updated["id"], "importjob001")
                self.assertEqual(updated["attempt_count"], 2)
                self.assertEqual(len(store.list_attempts("importjob001")), 2)
                self.assertTrue(
                    (
                        Path(temp_dir)
                        / "runs"
                        / "importjob001"
                        / "attempts"
                        / "1"
                        / "homedepot"
                        / "reviews.csv"
                    ).is_file()
                )
                duplicate_stage = Path(temp_dir) / "stage-three" / "homedepot"
                report_three = write_import_artifacts(duplicate_stage, validated)
                with self.assertRaisesRegex(ValueError, "already published"):
                    store.publish_external_import(
                        "importjob001",
                        channel="homedepot",
                        target_url=validated["target_url"],
                        brand="DEWALT",
                        model="EXAMPLE-1",
                        category="Inflator",
                        market_region="North America",
                        source_product_id="300510172",
                        result={"homedepot": report_three},
                        staged_run_dir=duplicate_stage,
                        audit_detail={
                            "import_id": validated["import_id"] + "-second",
                            "upload_sha256": "three",
                        },
                    )
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir

    def test_store_publishes_partial_import_and_keeps_attempt_auditable(self):
        with tempfile.TemporaryDirectory(prefix="review-import-partial-store-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                validated = validate_import_bundle(partial_bundle())
                stage = Path(temp_dir) / "partial-stage" / "homedepot"
                report = write_import_artifacts(stage, validated)
                published = store.publish_external_import(
                    "partialjob01",
                    channel="homedepot",
                    target_url=validated["target_url"],
                    brand="DEWALT",
                    model="EXAMPLE-1",
                    category="Inflator",
                    market_region="North America",
                    source_product_id="300510172",
                    result={"homedepot": report},
                    staged_run_dir=stage,
                    audit_detail={
                        "import_id": validated["import_id"],
                        "upload_sha256": "partial-one",
                    },
                )
                self.assertEqual(published["status"], "partial_success")
                self.assertEqual(published["active_result_status"], "partial_success")
                self.assertEqual(published["last_attempt_status"], "partial_success")
                attempts = store.list_attempts("partialjob01")
                self.assertEqual(attempts[0]["status"], "partial_success")
                self.assertIn("3 records missing", attempts[0]["error"])
                event = store.list_events("partialjob01")[0]
                self.assertEqual(event["code"], "external_import_partial")

                complete = validate_import_bundle(bundle())
                complete_stage = Path(temp_dir) / "complete-stage" / "homedepot"
                complete_report = write_import_artifacts(complete_stage, complete)
                upgraded = store.publish_external_import(
                    "partialjob01",
                    channel="homedepot",
                    target_url=complete["target_url"],
                    brand="DEWALT",
                    model="EXAMPLE-1",
                    category="Inflator",
                    market_region="North America",
                    source_product_id="300510172",
                    result={"homedepot": complete_report},
                    staged_run_dir=complete_stage,
                    audit_detail={
                        "import_id": complete["import_id"],
                        "upload_sha256": "complete-after-partial",
                    },
                )
                self.assertEqual(upgraded["status"], "completed")
                self.assertEqual(upgraded["active_attempt_no"], 2)
                self.assertEqual(upgraded["active_result_status"], "completed")
                archived_partial = (
                    Path(temp_dir)
                    / "runs"
                    / "partialjob01"
                    / "attempts"
                    / "1"
                    / "homedepot"
                    / "validation_report.json"
                )
                self.assertEqual(
                    json.loads(archived_partial.read_text(encoding="utf-8"))["status"],
                    "PARTIAL_SUCCESS",
                )
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir

    def _publish_archived_active_fixture(self, temp_dir, *, complete):
        store = Store()
        validated = validate_import_bundle(bundle() if complete else partial_bundle())
        stage = Path(temp_dir) / "initial-stage"
        report = write_import_artifacts(stage, validated)
        arguments = dict(channel="homedepot", target_url=validated["target_url"],
            brand="DEWALT", model="EXAMPLE-1", category="Inflator",
            market_region="North America", source_product_id="300510172")
        store.publish_external_import("archivedjob1", result={"homedepot": report},
            staged_run_dir=stage, **arguments)
        for _ in range(2):
            store.queue_retry("archivedjob1", action="scrape")
            store.fail("archivedjob1", "source temporarily unavailable")
        old = Path(temp_dir) / "runs/archivedjob1/attempts/1/homedepot"
        self.assertEqual(store.get_job("archivedjob1")["active_run_path"],
            "archivedjob1/attempts/1/homedepot")
        return store, arguments, old

    def test_import_after_failed_retry_preserves_archived_active_result(self):
        for prior_complete in (False, True):
            for incoming_complete in (False, True):
                with self.subTest(prior_complete=prior_complete, incoming_complete=incoming_complete):
                    with tempfile.TemporaryDirectory(prefix="review-import-archived-") as folder:
                        with patch.dict(os.environ, {"DATA_DIR": folder}):
                            store, args, old = self._publish_archived_active_fixture(
                                folder, complete=prior_complete)
                            before = {p.name: p.read_bytes() for p in old.iterdir()}
                            prior_attempts = store.list_attempts("archivedjob1")
                            incoming = validate_import_bundle(bundle() if incoming_complete else partial_bundle())
                            stage = Path(folder) / "new-stage"
                            report = write_import_artifacts(stage, incoming)
                            result = store.publish_external_import("archivedjob1",
                                result={"homedepot": report}, staged_run_dir=stage, **args)
                            self.assertEqual(result["attempt_count"], 4)
                            self.assertEqual(len(store.list_attempts("archivedjob1")), 4)
                            for attempt in prior_attempts:
                                self.assertIn(attempt, store.list_attempts("archivedjob1"))
                            self.assertEqual(before, {p.name: p.read_bytes() for p in old.iterdir()})
                            retain = prior_complete and not incoming_complete
                            self.assertEqual(result["active_attempt_no"], 1 if retain else 4)
                            self.assertEqual(result["active_run_path"],
                                "archivedjob1/attempts/1/homedepot" if retain else "archivedjob1/homedepot")
                            self.assertEqual(result["status"],
                                "completed" if prior_complete or incoming_complete else "partial_success")
                            new_path = Path(folder) / "runs/archivedjob1" / (
                                "attempts/4/homedepot" if retain else "homedepot")
                            self.assertTrue((new_path / "reviews.csv").is_file())

    def test_archived_active_import_failure_rolls_back_without_moving_history(self):
        with tempfile.TemporaryDirectory(prefix="review-import-archive-rollback-") as folder:
            with patch.dict(os.environ, {"DATA_DIR": folder}):
                store, args, old = self._publish_archived_active_fixture(folder, complete=False)
                original = store.get_job("archivedjob1")
                original_attempts = store.list_attempts("archivedjob1")
                old_bytes = {p.name: p.read_bytes() for p in old.iterdir()}
                stage = Path(folder) / "new-stage"
                report = write_import_artifacts(stage, validate_import_bundle(bundle()))
                with patch.object(store, "_append_event", side_effect=RuntimeError("synthetic audit failure")):
                    with self.assertRaisesRegex(RuntimeError, "synthetic audit failure"):
                        store.publish_external_import("archivedjob1", result={"homedepot": report},
                            staged_run_dir=stage, **args)
                self.assertEqual(store.get_job("archivedjob1"), original)
                self.assertEqual(store.list_attempts("archivedjob1"), original_attempts)
                self.assertEqual(old_bytes, {p.name: p.read_bytes() for p in old.iterdir()})
                self.assertFalse((Path(folder) / "runs/archivedjob1/homedepot").exists())
                self.assertTrue((stage / "reviews.csv").is_file())

    def test_archived_active_import_rejects_unproven_or_missing_path(self):
        for invalid in ("another-job/homedepot", "archivedjob1/attempts/2/homedepot"):
            with self.subTest(path=invalid):
                with tempfile.TemporaryDirectory(prefix="review-import-archive-path-") as folder:
                    with patch.dict(os.environ, {"DATA_DIR": folder}):
                        store, args, old = self._publish_archived_active_fixture(folder, complete=False)
                        with store._connect() as connection:
                            connection.execute("UPDATE jobs SET active_run_path=? WHERE id=?",
                                (invalid, "archivedjob1"))
                        stage = Path(folder) / "new-stage"
                        report = write_import_artifacts(stage, validate_import_bundle(bundle()))
                        with self.assertRaisesRegex(ValueError, "active result artifact"):
                            store.publish_external_import("archivedjob1", result={"homedepot": report},
                                staged_run_dir=stage, **args)
                        self.assertEqual(len(store.list_attempts("archivedjob1")), 3)
                        self.assertTrue((old / "reviews.csv").is_file())
                        self.assertTrue((stage / "reviews.csv").is_file())

    def test_archived_active_import_preserves_conflicting_root(self):
        with tempfile.TemporaryDirectory(prefix="review-import-archive-conflict-") as folder:
            with patch.dict(os.environ, {"DATA_DIR": folder}):
                store, args, old = self._publish_archived_active_fixture(folder, complete=False)
                root = Path(folder) / "runs/archivedjob1/homedepot"
                root.mkdir()
                (root / "diagnostic.json").write_text('{"unowned":"must preserve"}')
                stage = Path(folder) / "new-stage"
                report = write_import_artifacts(stage, validate_import_bundle(bundle()))
                with self.assertRaisesRegex(ValueError, "conflicts with active result"):
                    store.publish_external_import("archivedjob1", result={"homedepot": report},
                        staged_run_dir=stage, **args)
                self.assertEqual((root / "diagnostic.json").read_text(), '{"unowned":"must preserve"}')
                self.assertTrue((old / "reviews.csv").is_file())
                self.assertEqual(len(store.list_attempts("archivedjob1")), 3)

    def _publish_legacy_dual_root_fixture(self, folder):
        store = Store()
        args = dict(channel="homedepot", target_url=bundle()["target_url"],
            brand="DEWALT", model="EXAMPLE-1", category="Inflator",
            market_region="North America", source_product_id="300510172")
        for number in (1, 2):
            payload = bundle()
            payload["reviews"][0]["title"] = f"Capture {number}"
            stage = Path(folder) / f"stage-{number}"
            report = write_import_artifacts(stage, validate_import_bundle(payload))
            store.publish_external_import("legacydual1", result={"homedepot": report},
                staged_run_dir=stage, **args)
        store.queue_retry("legacydual1", action="scrape")
        store.fail("legacydual1", "source unavailable")
        root = Path(folder) / "runs/legacydual1/homedepot"
        root.mkdir()
        archive = Path(folder) / "runs/legacydual1/attempts/1/homedepot"
        for path in archive.iterdir():
            (root / path.name).write_bytes(path.read_bytes())
        # Older file reports omit their own report link; Store adds that link.
        report_path = root / "validation_report.json"
        report = json.loads(report_path.read_text())
        report["artifacts"].pop("report")
        report_path.write_text(json.dumps(report))
        return store, args, root

    def test_partial_import_preserves_owned_legacy_root_and_archived_complete(self):
        for audit_failure in (False, True):
            with self.subTest(audit_failure=audit_failure):
                with tempfile.TemporaryDirectory(prefix="review-import-dual-root-") as folder:
                    with patch.dict(os.environ, {"DATA_DIR": folder}):
                        store, args, root = self._publish_legacy_dual_root_fixture(folder)
                        before = store.get_job("legacydual1")
                        attempts = store.list_attempts("legacydual1")
                        files = {p: p.read_bytes() for p in root.parent.rglob("*") if p.is_file()}
                        stage = Path(folder) / "new-stage"
                        report = write_import_artifacts(stage, validate_import_bundle(partial_bundle()))
                        if audit_failure:
                            with patch.object(store, "_append_event", side_effect=RuntimeError("audit failed")):
                                with self.assertRaisesRegex(RuntimeError, "audit failed"):
                                    store.publish_external_import("legacydual1", result={"homedepot": report},
                                        staged_run_dir=stage, **args)
                            self.assertEqual(before, store.get_job("legacydual1"))
                            self.assertEqual(attempts, store.list_attempts("legacydual1"))
                            self.assertTrue((stage / "reviews.csv").is_file())
                            self.assertFalse((root.parent / "attempts/4/homedepot").exists())
                        else:
                            result = store.publish_external_import("legacydual1", result={"homedepot": report},
                                staged_run_dir=stage, **args)
                            self.assertEqual(result["attempt_count"], 4)
                            self.assertEqual(result["active_attempt_no"], 2)
                            self.assertEqual(result["active_run_path"], "legacydual1/attempts/2/homedepot")
                            self.assertEqual(result["active_result_status"], "completed")
                            self.assertEqual(result["result"], before["result"])
                            self.assertTrue((root.parent / "attempts/4/homedepot/reviews.csv").is_file())
                            with store._connect() as connection:
                                event = connection.execute("SELECT detail_json FROM job_events WHERE job_id=? AND attempt_no=4 AND code='external_import_partial'",
                                    ("legacydual1",)).fetchone()
                            preserved = json.loads(event["detail_json"])["preserved_legacy_root"]
                            self.assertEqual(preserved["owner_attempt_no"], 1)
                            self.assertEqual(preserved["action"], "preserved_in_place_not_selected")
                            self.assertEqual(len(preserved["files"]), 4)
                            for attempt in attempts:
                                self.assertIn(attempt, store.list_attempts("legacydual1"))
                        for path, data in files.items():
                            self.assertEqual(path.read_bytes(), data)

    def test_legacy_root_append_rejects_unproven_ownership_and_replacement(self):
        for invalid in ("unknown_owner", "ambiguous_owner", "symlink", "dangling_symlink", "complete_import"):
            with self.subTest(invalid=invalid):
                with tempfile.TemporaryDirectory(prefix="review-import-dual-root-reject-") as folder:
                    with patch.dict(os.environ, {"DATA_DIR": folder}):
                        store, args, root = self._publish_legacy_dual_root_fixture(folder)
                        if invalid == "unknown_owner":
                            path = root / "validation_report.json"
                            report = json.loads(path.read_text())
                            report["product"]["id"] = "unrelated"
                            path.write_text(json.dumps(report))
                        elif invalid == "ambiguous_owner":
                            with store._connect() as connection:
                                connection.execute("UPDATE job_attempts SET status='completed', result_json=(SELECT result_json FROM job_attempts WHERE job_id=? AND attempt_no=1) WHERE job_id=? AND attempt_no=3",
                                    ("legacydual1", "legacydual1"))
                        elif invalid in {"symlink", "dangling_symlink"}:
                            retained = root.with_name("retained-root")
                            root.rename(retained)
                            root.symlink_to(retained if invalid == "symlink" else retained.with_name("absent"),
                                target_is_directory=True)
                        before = store.get_job("legacydual1")
                        attempts = store.list_attempts("legacydual1")
                        files = {p: p.read_bytes() for p in root.parent.rglob("*") if p.is_file()}
                        stage = Path(folder) / "new-stage"
                        incoming = bundle() if invalid == "complete_import" else partial_bundle()
                        report = write_import_artifacts(stage, validate_import_bundle(incoming))
                        with self.assertRaisesRegex(ValueError, "conflicts with active result"):
                            store.publish_external_import("legacydual1", result={"homedepot": report},
                                staged_run_dir=stage, **args)
                        self.assertEqual(before, store.get_job("legacydual1"))
                        self.assertEqual(attempts, store.list_attempts("legacydual1"))
                        for path, data in files.items():
                            self.assertEqual(path.read_bytes(), data)
                        self.assertTrue((stage / "reviews.csv").is_file())

    def test_partial_import_does_not_replace_prior_completed_result(self):
        with tempfile.TemporaryDirectory(prefix="review-import-partial-retain-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                complete = validate_import_bundle(bundle())
                complete_stage = Path(temp_dir) / "complete-stage" / "homedepot"
                complete_report = write_import_artifacts(complete_stage, complete)
                store.publish_external_import(
                    "retainfull01",
                    channel="homedepot",
                    target_url=complete["target_url"],
                    brand="DEWALT",
                    model="EXAMPLE-1",
                    category="Inflator",
                    market_region="North America",
                    source_product_id="300510172",
                    result={"homedepot": complete_report},
                    staged_run_dir=complete_stage,
                    audit_detail={"import_id": complete["import_id"], "upload_sha256": "full"},
                )
                root = Path(temp_dir) / "runs" / "retainfull01" / "homedepot"
                original_report = (root / "validation_report.json").read_bytes()

                partial = validate_import_bundle(partial_bundle())
                partial_stage = Path(temp_dir) / "partial-stage" / "homedepot"
                partial_report = write_import_artifacts(partial_stage, partial)
                retained = store.publish_external_import(
                    "retainfull01",
                    channel="homedepot",
                    target_url=partial["target_url"],
                    brand="DEWALT",
                    model="EXAMPLE-1",
                    category="Inflator",
                    market_region="North America",
                    source_product_id="300510172",
                    result={"homedepot": partial_report},
                    staged_run_dir=partial_stage,
                    audit_detail={"import_id": partial["import_id"], "upload_sha256": "a" * 64},
                )
                self.assertEqual(retained["status"], "completed")
                self.assertEqual(retained["active_attempt_no"], 1)
                self.assertEqual(retained["active_result_status"], "completed")
                self.assertEqual(retained["last_attempt_status"], "partial_success")
                self.assertEqual((root / "validation_report.json").read_bytes(), original_report)
                partial_history = (
                    Path(temp_dir)
                    / "runs"
                    / "retainfull01"
                    / "attempts"
                    / "2"
                    / "homedepot"
                    / "validation_report.json"
                )
                self.assertEqual(
                    json.loads(partial_history.read_text(encoding="utf-8"))["status"],
                    "PARTIAL_SUCCESS",
                )
                with self.assertRaisesRegex(
                    ValueError, "active validation is already reportable"
                ):
                    store.promote_external_import_partial(
                        "retainfull01",
                        2,
                        expected_import_id=partial["import_id"],
                        expected_upload_sha256="a" * 64,
                    )
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir

    def test_promotes_latest_validated_partial_when_completed_active_is_invalid(self):
        with tempfile.TemporaryDirectory(prefix="review-import-partial-promote-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                complete = validate_import_bundle(bundle())
                complete_stage = Path(temp_dir) / "complete-stage" / "homedepot"
                complete_report = write_import_artifacts(complete_stage, complete)
                store.publish_external_import(
                    "promotepart1",
                    channel="homedepot",
                    target_url=complete["target_url"],
                    brand="DEWALT",
                    model="EXAMPLE-1",
                    category="Inflator",
                    market_region="North America",
                    source_product_id="300510172",
                    result={"homedepot": complete_report},
                    staged_run_dir=complete_stage,
                    audit_detail={
                        "import_id": complete["import_id"],
                        "upload_sha256": "a" * 64,
                    },
                )
                root = Path(temp_dir) / "runs" / "promotepart1" / "homedepot"
                invalid_report = json.loads(
                    (root / "validation_report.json").read_text(encoding="utf-8")
                )
                invalid_report["status"] = "INCOMPLETE_SOURCE_LIMIT"
                (root / "validation_report.json").write_text(
                    json.dumps(invalid_report), encoding="utf-8"
                )

                partial = validate_import_bundle(partial_bundle())
                partial_stage = Path(temp_dir) / "partial-stage" / "homedepot"
                partial_report = write_import_artifacts(partial_stage, partial)
                retained = store.publish_external_import(
                    "promotepart1",
                    channel="homedepot",
                    target_url=partial["target_url"],
                    brand="DEWALT",
                    model="EXAMPLE-1",
                    category="Inflator",
                    market_region="North America",
                    source_product_id="300510172",
                    result={"homedepot": partial_report},
                    staged_run_dir=partial_stage,
                    audit_detail={
                        "import_id": partial["import_id"],
                        "upload_sha256": "b" * 64,
                        "source_total": partial["source_total"],
                        "source_records_collected": partial["source_records_collected"],
                        "source_missing_records": partial["source_missing_records"],
                        "eligible_reviews": len(partial["eligible_rows"]),
                    },
                )
                self.assertEqual(retained["active_attempt_no"], 1)
                self.assertEqual(retained["last_attempt_status"], "partial_success")

                with self.assertRaisesRegex(ValueError, "latest external import attempt"):
                    store.promote_external_import_partial(
                        "promotepart1",
                        1,
                        expected_import_id=complete["import_id"],
                        expected_upload_sha256="a" * 64,
                    )
                with self.assertRaisesRegex(ValueError, "audit identity does not match"):
                    store.promote_external_import_partial(
                        "promotepart1",
                        2,
                        expected_import_id=partial["import_id"],
                        expected_upload_sha256="c" * 64,
                    )

                dry_run_output = StringIO()
                with redirect_stdout(dry_run_output):
                    self.assertEqual(
                        promotion_cli_main(
                            [
                                "--data-dir",
                                temp_dir,
                                "--job-id",
                                "promotepart1",
                                "--attempt-no",
                                "2",
                                "--expected-import-id",
                                partial["import_id"],
                                "--expected-upload-sha256",
                                "b" * 64,
                            ]
                        ),
                        0,
                    )
                self.assertEqual(
                    json.loads(dry_run_output.getvalue())["promotion_status"],
                    "ready",
                )
                self.assertEqual(store.get_job("promotepart1")["active_attempt_no"], 1)

                execute_output = StringIO()
                with redirect_stdout(execute_output):
                    self.assertEqual(
                        promotion_cli_main(
                            [
                                "--data-dir",
                                temp_dir,
                                "--job-id",
                                "promotepart1",
                                "--attempt-no",
                                "2",
                                "--expected-import-id",
                                partial["import_id"],
                                "--expected-upload-sha256",
                                "b" * 64,
                                "--execute",
                            ]
                        ),
                        0,
                    )
                promoted = json.loads(execute_output.getvalue())
                self.assertEqual(promoted["promotion_status"], "promoted")
                self.assertEqual(promoted["mode"], "execute")
                self.assertEqual(promoted["status"], "partial_success")
                self.assertEqual(promoted["active_attempt_no"], 2)
                self.assertEqual(promoted["active_result_status"], "partial_success")
                self.assertEqual(promoted["attempt_count"], 2)
                self.assertEqual(promoted["last_attempt_status"], "partial_success")
                self.assertEqual(
                    json.loads(
                        (root / "validation_report.json").read_text(encoding="utf-8")
                    )["status"],
                    "PARTIAL_SUCCESS",
                )
                archived_invalid = (
                    Path(temp_dir)
                    / "runs"
                    / "promotepart1"
                    / "attempts"
                    / "1"
                    / "homedepot"
                    / "validation_report.json"
                )
                self.assertEqual(
                    json.loads(archived_invalid.read_text(encoding="utf-8"))["status"],
                    "INCOMPLETE_SOURCE_LIMIT",
                )
                self.assertFalse(
                    (
                        Path(temp_dir)
                        / "runs"
                        / "promotepart1"
                        / "attempts"
                        / "2"
                        / "homedepot"
                    ).exists()
                )
                attempts = store.list_attempts("promotepart1")
                self.assertEqual(
                    [(item["attempt_no"], item["status"]) for item in attempts],
                    [(2, "partial_success"), (1, "completed")],
                )
                events_before = store.count_events(
                    "promotepart1", "external_import_partial_promoted"
                )
                self.assertEqual(events_before, 1)
                event = next(
                    item
                    for item in store.list_events("promotepart1")
                    if item["code"] == "external_import_partial_promoted"
                )
                self.assertEqual(event["attempt_no"], 2)
                self.assertEqual(event["detail"]["from_active_attempt_no"], 1)
                self.assertEqual(
                    event["detail"]["prior_validation_status"],
                    "INCOMPLETE_SOURCE_LIMIT",
                )

                repeated = store.promote_external_import_partial(
                    "promotepart1",
                    2,
                    expected_import_id=partial["import_id"],
                    expected_upload_sha256="b" * 64,
                )
                self.assertEqual(repeated["promotion_status"], "already_promoted")
                self.assertEqual(
                    store.count_events(
                        "promotepart1", "external_import_partial_promoted"
                    ),
                    events_before,
                )
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir

    def test_partial_promotion_rejects_incomplete_attempt_artifacts_without_moves(self):
        with tempfile.TemporaryDirectory(prefix="review-import-partial-promote-invalid-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                complete = validate_import_bundle(bundle())
                complete_stage = Path(temp_dir) / "complete-stage" / "homedepot"
                complete_report = write_import_artifacts(complete_stage, complete)
                store.publish_external_import(
                    "badpromote01",
                    channel="homedepot",
                    target_url=complete["target_url"],
                    brand="DEWALT",
                    model="EXAMPLE-1",
                    category="Inflator",
                    market_region="North America",
                    source_product_id="300510172",
                    result={"homedepot": complete_report},
                    staged_run_dir=complete_stage,
                    audit_detail={"import_id": complete["import_id"], "upload_sha256": "full"},
                )
                root = Path(temp_dir) / "runs" / "badpromote01" / "homedepot"
                invalid_report = json.loads(
                    (root / "validation_report.json").read_text(encoding="utf-8")
                )
                invalid_report["status"] = "INCOMPLETE_SOURCE_LIMIT"
                (root / "validation_report.json").write_text(
                    json.dumps(invalid_report), encoding="utf-8"
                )
                partial = validate_import_bundle(partial_bundle())
                partial_stage = Path(temp_dir) / "partial-stage" / "homedepot"
                partial_report = write_import_artifacts(partial_stage, partial)
                store.publish_external_import(
                    "badpromote01",
                    channel="homedepot",
                    target_url=partial["target_url"],
                    brand="DEWALT",
                    model="EXAMPLE-1",
                    category="Inflator",
                    market_region="North America",
                    source_product_id="300510172",
                    result={"homedepot": partial_report},
                    staged_run_dir=partial_stage,
                    audit_detail={
                        "import_id": partial["import_id"],
                        "upload_sha256": "d" * 64,
                        "source_total": partial["source_total"],
                        "source_records_collected": partial["source_records_collected"],
                        "source_missing_records": partial["source_missing_records"],
                        "eligible_reviews": len(partial["eligible_rows"]),
                    },
                )
                partial_dir = (
                    Path(temp_dir)
                    / "runs"
                    / "badpromote01"
                    / "attempts"
                    / "2"
                    / "homedepot"
                )
                corrupt = json.loads(
                    (partial_dir / "validation_report.json").read_text(encoding="utf-8")
                )
                corrupt["source_missing_records"] = 999
                (partial_dir / "validation_report.json").write_text(
                    json.dumps(corrupt), encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    ValueError, "validation report does not match raw import evidence"
                ):
                    store.promote_external_import_partial(
                        "badpromote01",
                        2,
                        expected_import_id=partial["import_id"],
                        expected_upload_sha256="d" * 64,
                    )
                self.assertTrue(root.is_dir())
                self.assertTrue(partial_dir.is_dir())
                self.assertEqual(store.get_job("badpromote01")["active_attempt_no"], 1)
                self.assertEqual(
                    store.count_events(
                        "badpromote01", "external_import_partial_promoted"
                    ),
                    0,
                )
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir

    def test_retired_home_depot_queue_fails_closed_without_new_task_id(self):
        with tempfile.TemporaryDirectory(prefix="retired-hd-store-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                store.create_job(
                    "retiredhd001",
                    "scrape",
                    "homedepot",
                    "https://www.homedepot.com/p/example/300510172",
                    "DEWALT",
                    "EXAMPLE-1",
                    "Inflator",
                    market_region="North America",
                )
                self.assertEqual(
                    store.retire_queued_channel("homedepot", "use external import"),
                    ["retiredhd001"],
                )
                retired = store.get_job("retiredhd001")
                self.assertEqual(retired["status"], "failed")
                self.assertEqual(retired["attempt_count"], 1)
                self.assertEqual(
                    store.list_events("retiredhd001")[0]["code"],
                    "channel_collection_retired",
                )
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir

    def test_home_depot_cache_task_never_runs_and_import_completes_attempt_in_place(self):
        with tempfile.TemporaryDirectory(prefix="cached-hd-store-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                store.create_job(
                    "cachedhd0001",
                    EXTERNAL_IMPORT_ACTION,
                    "homedepot",
                    "https://www.homedepot.com/p/example/300510172",
                    "DEWALT",
                    "EXAMPLE-1",
                    "Inflator",
                    market_region="North America",
                    source_product_id="300510172",
                )
                cached = store.get_job("cachedhd0001")
                self.assertEqual(cached["status"], "queued")
                self.assertEqual(cached["action"], EXTERNAL_IMPORT_ACTION)
                self.assertIn("awaiting validated import", cached["progress"])
                self.assertIsNone(store.next_job("homedepot"))
                self.assertEqual(
                    store.retire_queued_channel("homedepot", "worker retired"),
                    [],
                )
                self.assertEqual(
                    [item["id"] for item in store.list_external_import_queue()],
                    ["cachedhd0001"],
                )

                validated = validate_import_bundle(bundle())
                stage = Path(temp_dir) / "cached-stage" / "homedepot"
                report = write_import_artifacts(stage, validated)
                published = store.publish_external_import(
                    "cachedhd0001",
                    channel="homedepot",
                    target_url=validated["target_url"],
                    brand="DEWALT",
                    model="EXAMPLE-1",
                    category="Inflator",
                    market_region="North America",
                    source_product_id="300510172",
                    result={"homedepot": report},
                    staged_run_dir=stage,
                    audit_detail={
                        "import_id": validated["import_id"],
                        "upload_sha256": "cached-one",
                    },
                )
                self.assertEqual(published["status"], "completed")
                self.assertEqual(published["id"], "cachedhd0001")
                self.assertEqual(published["attempt_count"], 1)
                attempts = store.list_attempts("cachedhd0001")
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0]["status"], "completed")
                self.assertEqual(store.list_external_import_queue(), [])
                event_codes = {
                    item["code"] for item in store.list_events("cachedhd0001")
                }
                self.assertIn("external_import_cached", event_codes)
                self.assertIn("external_import_completed", event_codes)
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir

    def test_retired_home_depot_retry_retains_prior_completed_result(self):
        with tempfile.TemporaryDirectory(prefix="retired-hd-result-store-") as temp_dir:
            prior_data_dir = os.environ.get("DATA_DIR")
            os.environ["DATA_DIR"] = temp_dir
            try:
                store = Store()
                store.create_job(
                    "retiredhd002",
                    "scrape",
                    "homedepot",
                    "https://www.homedepot.com/p/example/300510172",
                    "DEWALT",
                    "EXAMPLE-1",
                    "Inflator",
                    market_region="North America",
                )
                store.finish("retiredhd002", {"eligible_reviews": 12})
                store.queue_retry("retiredhd002")

                self.assertEqual(
                    store.retire_queued_channel("homedepot", "use external import"),
                    ["retiredhd002"],
                )
                retired = store.get_job("retiredhd002")
                self.assertEqual(retired["status"], "completed")
                self.assertEqual(retired["active_result_status"], "completed")
                self.assertEqual(retired["last_attempt_status"], "failed")
                self.assertEqual(retired["attempt_count"], 2)
                self.assertIn("retained completed data", retired["progress"])
            finally:
                if prior_data_dir is None:
                    os.environ.pop("DATA_DIR", None)
                else:
                    os.environ["DATA_DIR"] = prior_data_dir


if __name__ == "__main__":
    unittest.main()
