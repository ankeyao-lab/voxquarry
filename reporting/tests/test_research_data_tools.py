from __future__ import annotations

import copy
import gc
import gzip
import hashlib
import json
import os
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reporting.agent_tools import AgentToolError, AgentToolService, ToolAuthority
from reporting.config import (
    FACT_SCHEMA_VERSION,
    NORMALIZATION_VERSION,
    PARSER_VERSION,
    SELECTION_POLICY_VERSION,
)
from reporting.source import canonical_json
from reporting.storage import ReportStore
from reporting.tests.test_agent_tool_contracts import fixture_report


QUERY = "zhuojian.query_report_reviews@v1"
AGGREGATE = "zhuojian.aggregate_report_reviews@v1"
SECTION = "zhuojian.get_report_section@v1"


def digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return "sha256:" + hashlib.sha256(value).hexdigest()


def research_facts() -> list[dict]:
    """125 physical rows: 124 entity observations, one with unknown identity."""
    rows = []
    texts = {
        0: "Battery quiet " + "complete detail " * 2_000 + "FULL_TEXT_SENTINEL",
        1: "battery heavy",
        2: "",
        3: "quiet compact",
        4: "ordinary review",
        5: "battery quiet heavy",
        122: "tailonly Zephyrneedle battery quiet",
    }
    for index in range(124):
        rating = [5, 2, None, 4, 3, 1][index] if index < 6 else 5
        recommendation = [True, False, None, True, None, False][index] if index < 6 else None
        text = texts.get(index, f"ordinary review {index}")
        unknown = index == 123
        fact = {
            "review_occurrence_id": f"occ_{index:024x}",
            "review_entity_id": None if unknown else f"rev_{index:024x}",
            "review_id": "" if unknown else f"source-review-{index}",
            "canonical_entity_rank": 1,
            "is_duplicate_occurrence": False,
            "job_id": "fixture-job",
            "job_finished_at": "2026-09-09T00:00:00Z",
            "channel": "lowes" if index % 2 == 0 else "ego",
            "product_id": "fixture-product",
            "product_title": "Synthetic mower",
            "product_url": "https://example.test/mower",
            "brand": "EGO",
            "category_id": "walk_behind_mower",
            "category": "Walk Behind Mower",
            "model": "LM-SYNTHETIC",
            "market_region": "North America",
            "market_region_evidence": "source_market",
            "display_site": "example.test",
            "source_site_raw": "example.test",
            "source_site_normalized": "example.test",
            "origin_source_name": "Synthetic source",
            "origin_source_url": "https://example.test/reviews",
            "origin_source_site": "example.test",
            "review_locale": "en_US",
            "source_product_id": "fixture-product",
            "collection_method": "primary",
            "validation_status": "COMPLETE",
            "is_syndicated_raw": "false",
            "is_syndicated": False,
            "syndication_parse_status": "parsed",
            "submission_time_raw": "2026-08-01" if index < 6 else "2026-09-01",
            "submission_date": "2026-08-01" if index < 6 else "2026-09-01",
            "time_parse_status": "parsed",
            "time_parse_rule": "iso_date",
            "review_title": f"Synthetic title {index}",
            "review_text": text,
            "has_text": bool(text),
            "review_word_count": len(text.split()),
            "is_eligible": True,
            "is_valid_text_review": bool(text),
            "exclusion_reason": "",
            "is_translated": False,
            "rating_raw": "" if rating is None else str(rating),
            "rating": rating,
            "rating_valid": rating is not None,
            "is_recommended_raw": "" if recommendation is None else str(recommendation).lower(),
            "is_recommended": recommendation,
            "recommended_parse_status": "unknown" if recommendation is None else "parsed",
            "verified_purchaser_raw": "true",
            "verified_purchaser": True,
            "verified_parse_status": "parsed",
            "where_purchase": "",
            "age": "",
            "business_key_status": "incomplete" if unknown else "complete",
            "eligibility_status": "eligible_text" if text else "eligible_non_text",
            "exclusion_reasons": [],
            "source_artifact_path": "data/runs/fixture-job/lowes/reviews.csv",
            "source_row_number": index + 2,
            "source_artifact_sha256": digest("synthetic normalized CSV"),
            "market_contract_mode": "current",
            "fact_schema_version": FACT_SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "normalization_version": NORMALIZATION_VERSION,
        }
        fact["row_content_sha256"] = digest(canonical_json({
            "source_row": index,
            "body": text,
            "rating": rating,
            "synthetic_source_nickname": "not retained in Fact",
        }))
        rows.append(fact)
    duplicate = copy.deepcopy(rows[0])
    duplicate.update({
        "review_occurrence_id": f"occ_{124:024x}",
        "canonical_entity_rank": 2,
        "is_duplicate_occurrence": True,
        "source_row_number": 126,
    })
    rows.insert(1, duplicate)
    return rows


def build_research_fixture(root: Path) -> dict:
    """Write only synthetic immutable artifacts; no source database is needed.

    Returns store, evidence, report, facts, manifest and artifact paths for
    integration callers. Callers own the temporary directory and its lifetime.
    """
    root = Path(root)
    store = ReportStore(root)
    facts = research_facts()
    scope = {
        "category_id": "walk_behind_mower",
        "categories": ["Walk Behind Mower"],
        "brands": ["EGO"],
        "models": ["LM-SYNTHETIC"],
        "channels": ["lowes", "ego"],
        "market_regions": ["North America"],
        "job_ids": ["fixture-job"],
        "date_range": {},
        "has_text": "any",
        "source_completeness": [],
        "non_reportable_exclusions": [],
    }
    logical_hash = digest("\n".join(row["row_content_sha256"] for row in facts))
    identity_hash = digest(canonical_json({
        "fact_schema_version": FACT_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "database_sha256": digest("synthetic source database identity"),
        "inputs": [],
        "scope": scope,
        "logical_content_sha256": logical_hash,
        "source_completeness": [],
        "non_reportable_exclusions": [],
    }))
    snapshot_id = "snap_" + identity_hash.removeprefix("sha256:")[:24]
    snapshot_dir = root / "snapshots" / snapshot_id
    snapshot_dir.mkdir(parents=True)
    fact_path = snapshot_dir / "review_fact.preview.jsonl.gz"
    manifest_path = snapshot_dir / "manifest.json"
    manifest = {
        "schema_version": "snapshot-manifest/v2",
        "content_hash": identity_hash,
        "snapshot_id": snapshot_id,
        "created_at": "2026-09-09T00:00:00Z",
        "analysis_input_version": FACT_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "job_ids": ["fixture-job"],
        "filters": {"job_ids": ["fixture-job"], "market_regions": ["North America"], "energy_type": None},
        "scope": scope,
        "syndication_policy": "include",
        "record_counts": {
            "occurrences": 125,
            "complete_entity_keys": 124,
            "unique_entities": 123,
            "written_reviews": 124,
            "eligible_valid_text": 124,
            "raw_records": 125,
            "legacy_contract_records": 0,
            "missing_contract_records": 0,
            "excluded_occurrences": 0,
        },
        "quality_summary": {
            "market_contract_mode": "current",
            "market_region_summary": {},
            "legacy_contract_record_count": 0,
            "missing_contract_record_count": 0,
            "unknown_identity_occurrences": 1,
            "duplicate_occurrences": 1,
        },
        "inputs": [],
        "artifacts": [{"path": fact_path.name, "format": "jsonl-gzip-preview", "target_contract": "parquet"}],
        "hashes": {"input_manifest": identity_hash, "logical_content": logical_hash},
    }
    compressed = gzip.compress(("\n".join(canonical_json(row) for row in facts) + "\n").encode("utf-8"), mtime=0)
    fact_path.write_bytes(compressed)
    manifest["hashes"]["preview_fact"] = digest(compressed)
    manifest_path.write_text(canonical_json(manifest), encoding="utf-8")
    report = fixture_report()
    report.update({
        "report_id": "rpt_researchfixture00000001",
        "data_snapshot_id": snapshot_id,
        "scope": copy.deepcopy(scope),
        "samples": {"occurrences": 125, "review_entities": 124, "written_review_entities": 123, "valid_rating_entities": 123, "unknown_identity_occurrences": 1, "duplicate_occurrences": 1},
        "tables": [{"table_id": "all_rows", "title": "All saved rows", "columns": [{"key": "index", "label": "Index"}], "rows": [{"index": index} for index in range(125)]}],
        "product_insight": {"a/b": {"til~de": {"text": "Full saved section", "nested": [1, 2, 3]}}},
    })
    report["hashes"]["data"] = logical_hash
    store.store_report(report)
    return {
        "store": store,
        "evidence": SimpleNamespace(root=root),
        "report": report,
        "facts": facts,
        "manifest": manifest,
        "snapshot_dir": snapshot_dir,
        "fact_path": fact_path,
        "manifest_path": manifest_path,
    }


def build_streaming_research_fixture(root: Path, count: int, body: str) -> dict:
    """Generate a large synthetic artifact without holding its rows in memory."""
    fixture = build_research_fixture(root)
    template = fixture["facts"][0]
    manifest = copy.deepcopy(fixture["manifest"])
    report = copy.deepcopy(fixture["report"])
    logical = hashlib.sha256()

    def rows():
        for index in range(count):
            fact = {
                **template,
                "review_occurrence_id": f"occ_{index:024x}",
                "review_entity_id": f"rev_{index:024x}",
                "review_id": f"source-streaming-{index}",
                "canonical_entity_rank": 1,
                "is_duplicate_occurrence": False,
                "business_key_status": "complete",
                "source_row_number": index + 2,
                "review_title": f"Generated review {index}",
                "review_text": body,
                "has_text": True,
                "is_valid_text_review": True,
                "review_word_count": len(body.split()),
                "rating": 5,
                "rating_valid": True,
                "is_recommended": None,
                "is_recommended_raw": "",
                "recommended_parse_status": "unknown",
            }
            fact["row_content_sha256"] = digest(canonical_json({
                "source_row": index, "body": body, "rating": 5,
                "synthetic_source_nickname": "not retained in Fact",
            }))
            if index:
                logical.update(b"\n")
            logical.update(fact["row_content_sha256"].encode("ascii"))
            yield fact

    staging = Path(root) / "generated-facts.jsonl.gz"
    with staging.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped:
            for fact in rows():
                zipped.write((canonical_json(fact) + "\n").encode("utf-8"))
    logical_hash = "sha256:" + logical.hexdigest()
    identity_hash = digest(canonical_json({
        "fact_schema_version": FACT_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "database_sha256": digest("synthetic source database identity"),
        "inputs": [], "scope": manifest["scope"],
        "logical_content_sha256": logical_hash,
        "source_completeness": [], "non_reportable_exclusions": [],
    }))
    snapshot_id = "snap_" + identity_hash.removeprefix("sha256:")[:24]
    snapshot_dir = Path(root) / "snapshots" / snapshot_id
    snapshot_dir.mkdir()
    fact_path = snapshot_dir / "review_fact.preview.jsonl.gz"
    staging.rename(fact_path)
    physical = hashlib.sha256()
    with fact_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            physical.update(chunk)
    manifest.update({
        "snapshot_id": snapshot_id,
        "content_hash": identity_hash,
        "hashes": {
            "input_manifest": identity_hash,
            "logical_content": logical_hash,
            "preview_fact": "sha256:" + physical.hexdigest(),
        },
    })
    for key in ("occurrences", "complete_entity_keys", "unique_entities", "written_reviews", "eligible_valid_text", "raw_records"):
        manifest["record_counts"][key] = count
    manifest["quality_summary"].update({"unknown_identity_occurrences": 0, "duplicate_occurrences": 0})
    manifest_path = snapshot_dir / "manifest.json"
    manifest_path.write_text(canonical_json(manifest), encoding="utf-8")
    report.update({
        "report_id": "rpt_" + identity_hash.removeprefix("sha256:")[:24],
        "data_snapshot_id": snapshot_id,
        "samples": {
            "occurrences": count, "review_entities": count,
            "written_review_entities": count, "valid_rating_entities": count,
            "unknown_identity_occurrences": 0, "duplicate_occurrences": 0,
        },
    })
    report["hashes"]["data"] = logical_hash
    fixture["store"].store_report(report)
    return {
        "store": fixture["store"], "evidence": fixture["evidence"],
        "report": report, "manifest": manifest,
        "snapshot_dir": snapshot_dir, "fact_path": fact_path,
        "manifest_path": manifest_path,
    }


class ResearchDataToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        fixture = build_research_fixture(self.root)
        self.store = fixture["store"]
        self.facts = fixture["facts"]
        self.report = fixture["report"]
        self.manifest = fixture["manifest"]
        self.snapshot_dir = fixture["snapshot_dir"]
        self.fact_path = fixture["fact_path"]
        self.manifest_path = fixture["manifest_path"]
        self.service = AgentToolService(self.store, fixture["evidence"])
        self.authority = ToolAuthority("research-fixture", "research-owner", frozenset({self.report["report_id"]}))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_manifest(self) -> None:
        self.manifest_path.write_text(canonical_json(self.manifest), encoding="utf-8")

    def write_report(self) -> None:
        (self.store.reports_dir / f"{self.report['report_id']}.json").write_text(canonical_json(self.report), encoding="utf-8")

    def write_facts(self, rows: list[dict], *, update_compressed_hash: bool) -> None:
        compressed = gzip.compress(("\n".join(canonical_json(row) for row in rows) + "\n").encode("utf-8"), mtime=0)
        self.fact_path.write_bytes(compressed)
        if update_compressed_hash:
            self.manifest["hashes"]["preview_fact"] = digest(compressed)
            self.write_manifest()

    def call(self, tool: str = QUERY, **payload) -> dict:
        payload.setdefault("report_id", self.report["report_id"])
        result = self.service.invoke(tool, payload, self.authority)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["report"]["report_id"], payload["report_id"])
        self.assertEqual(result["snapshot"]["snapshot_id"], self.report["data_snapshot_id"])
        provenance = result["data"]["provenance"]
        self.assertEqual(provenance["report_id"], payload["report_id"])
        self.assertEqual(provenance["snapshot_id"], self.report["data_snapshot_id"])
        self.assertEqual(provenance["analysis_version"], self.report["analysis_version"])
        self.assertEqual(provenance["scope_sha256"], digest(canonical_json(self.report["scope"])))
        self.assertEqual(provenance["logical_content_sha256"], self.report["hashes"]["data"])
        self.assertTrue(provenance["source_fingerprint"])
        if tool != SECTION:
            self.assertEqual(provenance["record_count"], 125)
            self.assertEqual(provenance["entity_count"], 124)
            self.assertEqual(provenance["written_entity_count"], 123)
            self.assertEqual(provenance["fact_file_sha256"], digest(self.fact_path.read_bytes()))
            self.assertEqual(provenance["manifest_sha256"], digest(self.manifest_path.read_bytes()))
        return result

    def rejected(self, tool: str = QUERY, **payload) -> AgentToolError:
        payload.setdefault("report_id", self.report["report_id"])
        with self.assertRaises(AgentToolError) as caught:
            self.service.invoke(tool, payload, self.authority)
        return caught.exception

    def test_full_normalized_body_and_record_identity_are_preserved(self) -> None:
        original = self.facts[0]
        result = self.call(review_ids=[original["review_occurrence_id"]])
        rows = result["data"]["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["review_text"], original["review_text"])
        self.assertGreater(len(rows[0]["review_text"]), 20_000)
        self.assertEqual(rows[0]["source_artifact_sha256"], original["source_artifact_sha256"])
        self.assertTrue(rows[0]["record_id"])
        self.assertEqual(rows[0]["fact_sha256"], digest(canonical_json(original)))
        self.assertTrue(rows[0]["source_id"])
        sources = {source["source_id"]: source for source in result["data"]["sources"]}
        self.assertEqual(sources[rows[0]["source_id"]]["content_sha256"], rows[0]["fact_sha256"])

    def test_pagination_covers_more_than_100_entities_without_duplicates(self) -> None:
        first = self.call(limit=100)
        second = self.call(offset=100, limit=100)
        rows = first["data"]["items"] + second["data"]["items"]
        self.assertEqual((len(first["data"]["items"]), len(second["data"]["items"])), (100, 24))
        self.assertEqual(len({row["record_id"] for row in rows}), 124)
        self.assertEqual(first["page"]["total"], 124)
        self.assertEqual(second["page"]["total"], 124)

    def test_text_search_scans_the_tail_of_the_complete_population(self) -> None:
        result = self.call(query="ZEPHYRNEEDLE", limit=1)
        self.assertEqual(result["page"]["total"], 1)
        self.assertEqual(result["data"]["items"][0]["review_occurrence_id"], f"occ_{122:024x}")

    def match_fixture(self) -> dict[int, str]:
        texts = {
            0: "Easy turn, self propel.",
            1: "return only; self propelled; axb.",
            3: "turning with self propel.",
            4: "TURN and self propel. Literal a.b and C++.",
            5: "turn_end with self propel.",
            122: "这款产品续航能力很好；某电池battery版本适用。",
        }
        for fact in self.facts:
            for index, text in texts.items():
                if fact.get("review_entity_id") == f"rev_{index:024x}":
                    fact["review_text"] = text
                    fact["review_word_count"] = len(text.split())
        self.write_facts(self.facts, update_compressed_hash=True)
        return texts

    def test_word_mode_excludes_return_and_requires_separate_word_forms(self) -> None:
        self.match_fixture()
        substring = self.call(query="turn")
        word = self.call(query="turn", match_mode="word")
        self.assertEqual(substring["page"]["total"], 5)
        self.assertEqual({row["record_id"] for row in word["data"]["items"]}, {f"occ_{index:024x}" for index in (0, 4)})
        self.assertEqual(word["data"]["query"]["match_mode"], "word")
        self.assertIn("不做词形归一", word["data"]["search_semantics"])
        self.assertIn("不代表语义分类准确", word["data"]["search_semantics"])
        self.assertEqual(self.call(query="turning", match_mode="word")["page"]["total"], 1)

    def test_word_mode_applies_to_phrases_groups_exclusions_and_full_aggregate(self) -> None:
        self.match_fixture()
        groups = [{"label": "turn", "terms": ["turn"]}, {"label": "drive", "terms": ["self propel"]}]
        result = self.call(AGGREGATE, match_mode="word", term_groups=groups)
        data = result["data"]
        self.assertEqual({row["label"]: row["count"] for row in data["group_match_counts"]}, {"turn": 2, "drive": 4})
        self.assertEqual(data["intersection_count"], 2)
        self.assertEqual(data["summary"]["count"], 2)
        self.assertEqual(data["summary"]["rating_sum"], 8)
        page = self.call(match_mode="word", term_groups=groups, limit=1)
        self.assertEqual(page["page"]["total"], 2)
        self.assertEqual(len(page["data"]["items"]), 1)
        excluded = self.call(query="self propel", match_mode="word", exclude_terms=["turn"])
        self.assertEqual({row["record_id"] for row in excluded["data"]["items"]}, {f"occ_{index:024x}" for index in (3, 5)})
        self.assertEqual(self.call(query="self propel", exclude_terms=["turn"])["page"]["total"], 0)

    def test_word_mode_escapes_regex_metacharacters_as_literal_text(self) -> None:
        self.match_fixture()
        for query in ("a.b", "C++"):
            with self.subTest(query=query):
                result = self.call(query=query, match_mode="word")
                self.assertEqual([row["record_id"] for row in result["data"]["items"]], [f"occ_{4:024x}"])
        for query in ("turn|return", "turn.*", "[a-z]+"):
            with self.subTest(query=query):
                self.assertEqual(self.call(query=query, match_mode="word")["page"]["total"], 0)

    def test_word_mode_keeps_han_terms_as_substrings_in_all_query_inputs(self) -> None:
        self.match_fixture()
        query = self.call(query="续航", match_mode="word")
        grouped = self.call(AGGREGATE, term_groups=[{"label": "续航", "terms": ["续航"]}], match_mode="word")
        self.assertEqual([row["record_id"] for row in query["data"]["items"]], [f"occ_{122:024x}"])
        self.assertEqual(grouped["data"]["summary"]["count"], 1)
        mixed = self.call(query="电池battery", match_mode="word")
        self.assertEqual(mixed["page"]["total"], 1)
        excluded = self.call(query="电池battery", exclude_terms=["续航"], match_mode="word")
        self.assertEqual(excluded["page"]["total"], 0)
        self.assertIn("含汉字且无空白的词条仍按子串匹配", grouped["data"]["search_semantics"])

    def test_default_and_explicit_substring_preserve_legacy_data_and_cursor_identity(self) -> None:
        for tool in (QUERY, AGGREGATE):
            with self.subTest(tool=tool):
                args = {"query": "battery", **({"limit": 1} if tool == QUERY else {})}
                old = self.call(tool, **args)
                explicit = self.call(tool, **args, match_mode="substring")
                self.assertEqual(old, explicit)
                legacy_query = {"population": "entities", "query": "battery", "review_ids": [], "filters": {}, "term_groups": [], "exclude_terms": []}
                self.assertEqual(old["data"]["query"], legacy_query)
                self.assertEqual(old["data"]["query_sha256"], digest(canonical_json(legacy_query)))
                self.assertEqual(old["data"]["search_semantics"], "标题/正文不区分大小写的词面子串匹配；组内 OR、组间 AND。各组计数使用相同基础筛选人口；零匹配不能证明相关语义不存在，星级不等于主题情感。")
        first = self.call(query="battery", limit=1)
        cursor = first["data"]["next_cursor"]
        self.call(query="battery", limit=1, cursor=cursor, match_mode="substring")
        self.assertEqual(self.rejected(query="battery", limit=1, cursor=cursor, match_mode="word").code, "invalid_request")
        word = self.call(query="battery", limit=1, match_mode="word")
        self.assertNotEqual(first["data"]["query_sha256"], word["data"]["query_sha256"])
        self.call(query="battery", limit=1, cursor=word["data"]["next_cursor"], match_mode="word")
        self.assertEqual(self.rejected(query="battery", limit=1, cursor=word["data"]["next_cursor"]).code, "invalid_request")

    def test_byte_limited_pages_keep_complete_records_and_advance_by_actual_returned(self) -> None:
        with patch("reporting.research_data.MAX_REVIEW_PAGE_BYTES", 6000):
            first = self.call(limit=100)
            self.assertEqual(first["page"]["returned"], 1)
            self.assertEqual(first["page"]["next_offset"], 1)
            self.assertEqual(first["data"]["page_limit_reason"], "bytes")
            self.assertEqual(first["data"]["items"][0]["review_text"], self.facts[0]["review_text"])
            second = self.call(limit=100, cursor=first["data"]["next_cursor"])
            self.assertEqual(second["page"]["offset"], 1)
            self.assertEqual(second["data"]["items"][0]["record_id"], self.facts[2]["review_occurrence_id"])
            self.assertEqual(second["page"]["total"], 124)
            self.assertGreater(second["page"]["returned"], 0)
            self.assertLess(second["page"]["returned"], 100)

    def test_oversized_single_review_and_envelope_fail_with_explicit_capacity_errors(self) -> None:
        with patch("reporting.research_data.MAX_SINGLE_REVIEW_BYTES", 1024):
            error = self.rejected(limit=1)
            self.assertEqual(error.code, "report_review_too_large")
        with patch("reporting.agent_tools.MAX_RESEARCH_RESPONSE_BYTES", 1024):
            error = self.rejected(SECTION)
            self.assertEqual(error.code, "research_result_too_large")

    def test_entities_and_occurrences_keep_unknown_identity_explicit(self) -> None:
        entities = self.call(AGGREGATE)["data"]["summary"]
        occurrences = self.call(AGGREGATE, population="occurrences")["data"]["summary"]
        self.assertEqual(entities["count"], 124)
        self.assertEqual(occurrences["count"], 125)
        self.assertEqual(entities["unknown_identity_count"], 1)
        self.assertEqual(occurrences["unknown_identity_count"], 1)

    def test_term_groups_return_independent_full_population_counts_and_intersection(self) -> None:
        result = self.call(term_groups=[{"label": "battery", "terms": ["battery"]}, {"label": "quiet", "terms": ["quiet", "silent"]}], limit=1)
        data = result["data"]
        self.assertEqual({row["label"]: row["count"] for row in data["group_match_counts"]}, {"battery": 4, "quiet": 4})
        self.assertEqual(data["intersection_count"], 3)
        self.assertEqual(result["page"]["total"], 3)
        self.assertEqual(len(data["items"]), 1)

    def test_excluded_terms_and_filters_narrow_the_frozen_population(self) -> None:
        result = self.call(query="battery", exclude_terms=["heavy"])
        self.assertEqual({row["review_occurrence_id"] for row in result["data"]["items"]}, {f"occ_{0:024x}", f"occ_{122:024x}"})
        filtered = self.call(filters={"ratings": [1, 2], "has_text": True, "channels": ["ego"], "brands": ["EGO"], "models": ["LM-SYNTHETIC"], "date_start": "2026-08-01", "date_end": "2026-08-01"})
        self.assertEqual({row["rating"] for row in filtered["data"]["items"]}, {1, 2})
        self.assertEqual(filtered["page"]["total"], 2)

    def test_aggregation_uses_distinct_rating_and_recommendation_denominators(self) -> None:
        result = self.call(AGGREGATE)
        summary = result["data"]["summary"]
        expected = {"count": 124, "written_count": 123, "valid_rating_count": 123, "unknown_rating_count": 1, "rating_sum": 605, "positive_rating_count": 120, "negative_rating_count": 2, "recommendation_numerator": 2, "recommendation_denominator": 4, "unknown_identity_count": 1}
        for key, value in expected.items():
            self.assertEqual(summary[key], value, key)
        self.assertAlmostEqual(summary["average_rating"], 605 / 123, places=5)
        self.assertEqual(sum(row["count"] for row in result["data"]["groups"]), 124)

    def test_aggregate_source_hash_covers_independent_group_and_intersection_counts(self) -> None:
        data = self.call(AGGREGATE, term_groups=[{"label": "battery", "terms": ["battery"]}, {"label": "quiet", "terms": ["quiet"]}])["data"]
        source = data["sources"][0]
        content = {key: data[key] for key in ("population", "population_count", "base_match_count", "group_match_counts", "intersection_count", "missing_review_ids", "search_semantics", "scan_complete", "summary", "group_by", "groups", "matched_record_ids_sha256")}
        self.assertEqual(source["content_sha256"], digest(canonical_json(content)))
        content["group_match_counts"][0]["count"] += 1
        self.assertNotEqual(source["content_sha256"], digest(canonical_json(content)))

    def test_invalid_grouping_and_contradictory_flags_fail_before_filter_or_aggregate(self) -> None:
        for field, value in (("channel", []), ("brand", {}), ("model", None), ("submission_date", 123), ("submission_date", "2026-02-30"), ("has_text", False), ("rating_valid", False)):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(self.facts)
                changed[0][field] = value
                self.write_facts(changed, update_compressed_hash=True)
                error = self.rejected(AGGREGATE, group_by="channel")
                self.assertEqual(error.code, "research_source_invalid")
        self.write_facts(self.facts, update_compressed_hash=True)

    def test_aggregation_groupings_and_empty_population_are_explicit(self) -> None:
        by_month = self.call(AGGREGATE, group_by="submission_month")["data"]["groups"]
        self.assertEqual({row["key"]: row["count"] for row in by_month}, {"2026-08": 6, "2026-09": 118})
        empty = self.call(AGGREGATE, query="term-that-does-not-occur")["data"]["summary"]
        self.assertEqual(empty["count"], 0)
        self.assertEqual(empty["recommendation_denominator"], 0)
        self.assertIsNone(empty["average_rating"])

    def test_missing_ids_are_reported_and_entity_ids_resolve(self) -> None:
        missing = "occ_" + "f" * 24
        known = self.facts[0]["review_entity_id"]
        result = self.call(review_ids=[known, missing])
        self.assertEqual(len(result["data"]["items"]), 1)
        self.assertEqual(result["data"]["items"][0]["review_entity_id"], known)
        self.assertEqual(result["data"]["missing_review_ids"], [missing])

    def test_report_section_pointer_preserves_nested_content_and_array_tail(self) -> None:
        result = self.call(SECTION, section="/product_insight/a~1b/til~0de")
        self.assertEqual(result["data"]["content"], self.report["product_insight"]["a/b"]["til~de"])
        tail = self.call(SECTION, section="/tables/0/rows", offset=100, limit=100)
        self.assertEqual(tail["data"]["content"], [{"index": index} for index in range(100, 125)])
        self.assertEqual(tail["page"]["total"], 125)

    def test_report_section_index_exposes_saved_section_descriptors(self) -> None:
        result = self.call(SECTION, section="index")
        descriptors = result["data"]["descriptors"]
        self.assertTrue(descriptors)
        self.assertTrue(any(row.get("section") == "/product_insight" for row in descriptors))
        self.assertTrue(result["data"]["sources"])

    def test_cursor_preserves_query_and_snapshot_identity(self) -> None:
        first = self.call(query="ordinary", limit=5)
        cursor = first["data"]["next_cursor"]
        self.assertTrue(cursor)
        second = self.call(query="ordinary", limit=5, cursor=cursor)
        first_ids = {row["record_id"] for row in first["data"]["items"]}
        self.assertFalse(first_ids & {row["record_id"] for row in second["data"]["items"]})
        self.rejected(query="battery", limit=5, cursor=cursor)
        alternate = copy.deepcopy(self.report)
        alternate["report_id"] = "rpt_researchfixture00000002"
        self.store.store_report(alternate)
        self.authority = ToolAuthority("research-fixture", "research-owner", frozenset({self.report["report_id"], alternate["report_id"]}))
        self.rejected(report_id=alternate["report_id"], query="ordinary", limit=5, cursor=cursor)

    def test_manifest_scope_drift_is_rejected(self) -> None:
        self.manifest["scope"]["models"] = ["OTHER-MODEL"]
        self.write_manifest()
        self.rejected()

    def test_manifest_snapshot_identity_drift_is_rejected(self) -> None:
        self.manifest["snapshot_id"] = "snap_" + "f" * 24
        self.write_manifest()
        self.rejected()

    def test_compressed_content_drift_is_rejected_even_with_valid_json(self) -> None:
        mutated = copy.deepcopy(self.facts)
        mutated[0]["review_text"] = "Altered body without updated artifact hash"
        self.write_facts(mutated, update_compressed_hash=False)
        self.rejected()

    def test_logical_content_drift_is_rejected_after_compressed_hash_update(self) -> None:
        mutated = copy.deepcopy(self.facts)
        mutated[0]["row_content_sha256"] = digest("new source row")
        self.write_facts(mutated, update_compressed_hash=True)
        self.rejected()

    def test_report_data_hash_drift_is_rejected(self) -> None:
        self.report["hashes"]["data"] = digest("other data")
        self.write_report()
        self.rejected()

    def test_declared_record_count_drift_is_rejected(self) -> None:
        self.manifest["record_counts"]["occurrences"] = 124
        self.write_manifest()
        self.rejected()

    def test_missing_fact_or_manifest_never_rebuilds_from_active_sources(self) -> None:
        for path in (self.fact_path, self.manifest_path):
            with self.subTest(artifact=path.name):
                original = path.read_bytes()
                path.unlink()
                with patch("reporting.source.build_review_fact", side_effect=AssertionError("active source must not be used")) as rebuild:
                    self.rejected()
                    rebuild.assert_not_called()
                path.write_bytes(original)

    def test_file_and_directory_symlinks_are_rejected_even_inside_runtime_root(self) -> None:
        for path in (self.fact_path, self.manifest_path):
            with self.subTest(artifact=path.name):
                target = self.root / (path.name + ".saved")
                path.rename(target)
                path.symlink_to(target)
                self.rejected()
                path.unlink()
                target.rename(path)
        target_dir = self.root / "held-snapshot"
        self.snapshot_dir.rename(target_dir)
        self.snapshot_dir.symlink_to(target_dir, target_is_directory=True)
        self.rejected()

    def test_malformed_records_fail_before_partial_results(self) -> None:
        original = self.fact_path.read_bytes()
        for suffix in ("not-json", "[]", "null"):
            with self.subTest(record=suffix):
                text = gzip.decompress(original).decode("utf-8") + suffix + "\n"
                compressed = gzip.compress(text.encode("utf-8"), mtime=0)
                self.fact_path.write_bytes(compressed)
                self.manifest["hashes"]["preview_fact"] = digest(compressed)
                self.write_manifest()
                self.rejected(limit=1)

    def test_first_page_match_cannot_hide_invalid_or_drifted_tail(self) -> None:
        first_id = self.facts[0]["review_occurrence_id"]
        for corruption in ("invalid_json", "logical_hash"):
            with self.subTest(corruption=corruption):
                mutated = copy.deepcopy(self.facts)
                if corruption == "logical_hash":
                    mutated[-1]["row_content_sha256"] = digest("changed final source row")
                self.write_facts(mutated, update_compressed_hash=True)
                if corruption == "invalid_json":
                    text = gzip.decompress(self.fact_path.read_bytes()) + b"invalid-final-record\n"
                    compressed = gzip.compress(text, mtime=0)
                    self.fact_path.write_bytes(compressed)
                    self.manifest["hashes"]["preview_fact"] = digest(compressed)
                    self.write_manifest()
                error = self.rejected(review_ids=[first_id], limit=1)
                self.assertEqual(error.code, "research_source_invalid")

    def test_replaced_path_between_physical_hash_and_scan_is_rejected(self) -> None:
        from reporting.research_data import FrozenReviewData

        original_iter = FrozenReviewData.iter_facts
        replacement = self.snapshot_dir / "replacement.gz"
        replacement.write_bytes(self.fact_path.read_bytes())
        original_inode = self.fact_path.stat().st_ino
        replaced = False

        def replace_before_scan(source):
            nonlocal replaced
            if not replaced:
                os.replace(replacement, self.fact_path)
                replaced = True
            yield from original_iter(source)

        with patch.object(FrozenReviewData, "iter_facts", replace_before_scan):
            error = self.rejected(limit=1)
        self.assertTrue(replaced, "race injection must run after the physical hash pass")
        self.assertNotEqual(self.fact_path.stat().st_ino, original_inode)
        self.assertEqual(error.code, "research_source_changed")

    def test_total_decompression_limit_fails_without_returning_truncated_page(self) -> None:
        with patch("reporting.research_data.MAX_FACT_BYTES", 256):
            query_error = self.rejected(limit=1)
            aggregate_error = self.rejected(AGGREGATE)
        self.assertEqual(query_error.code, "research_source_unavailable")
        self.assertEqual(aggregate_error.code, "research_source_unavailable")

    def test_large_query_and_aggregate_do_not_retain_total_body_text(self) -> None:
        # Keep identity count fixed while increasing body bytes by over 100x.
        # Sets of 10,000 IDs are expected; retaining all bodies is not.
        count = 10_000
        bodies = {
            "short": "word " * 12 + "BODY_END",
            "long": "complete detail " * 600 + "BODY_END",
        }
        peaks: dict[str, dict[str, int]] = {}
        for label, body in bodies.items():
            fixture = build_streaming_research_fixture(self.root / label, count, body)
            service = AgentToolService(fixture["store"], fixture["evidence"])
            report_id = fixture["report"]["report_id"]
            authority = ToolAuthority("large-fixture", "large-owner", frozenset({report_id}))
            peaks[label] = {}
            for name, tool, arguments in (
                ("query", QUERY, {"limit": 1, "query": "BODY_END"}),
                ("aggregate", AGGREGATE, {"group_by": "channel"}),
            ):
                gc.collect()
                tracemalloc.start()
                try:
                    result = service.invoke(tool, {"report_id": report_id, **arguments}, authority)
                    peaks[label][name] = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertEqual(result["status"], "ok")
                self.assertTrue(result["data"]["scan_complete"])
                self.assertEqual(result["data"]["provenance"]["record_count"], count)
                if name == "query":
                    self.assertEqual(result["page"]["total"], count)
                    self.assertEqual(len(result["data"]["items"]), 1)
                    self.assertEqual(result["data"]["items"][0]["review_text"], body)
                else:
                    self.assertEqual(result["data"]["summary"]["count"], count)
                    self.assertEqual(result["data"]["summary"]["average_rating"], 5)
                    self.assertEqual(result["data"]["summary"]["recommendation_denominator"], 0)
        long_body_bytes = count * len(bodies["long"].encode("utf-8"))
        for name in ("query", "aggregate"):
            with self.subTest(operation=name, memory_peaks=peaks):
                self.assertLess(peaks["long"][name], long_body_bytes // 4)
                self.assertLess(peaks["long"][name] - peaks["short"][name], 8 * 1024 * 1024)

    def test_invalid_inputs_cannot_inject_paths_sql_authority_or_unbounded_work(self) -> None:
        cases = [
            {"path": "/etc/passwd"},
            {"sql": "SELECT * FROM reviews"},
            {"authority": {"allowed_report_ids": "*"}},
            {"filters": {"sql": "1=1"}},
            {"filters": {"ratings": [True]}},
            {"filters": {"date_start": "2026-99-99"}},
            {"population": "raw_database"},
            {"limit": 101},
            {"offset": -1},
            {"limit": True},
            {"review_ids": ["../../data.db"]},
            {"query": {"$regex": ".*"}},
            {"term_groups": [{"label": "unbounded", "terms": []}]},
            {"term_groups": [{"label": "x", "terms": ["battery"], "sql": "1=1"}]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.rejected(**payload)
        self.rejected(AGGREGATE, group_by="__class__")
        for tool in (QUERY, AGGREGATE):
            for mode in ("regex", True, None, [], {}):
                with self.subTest(tool=tool, match_mode=mode):
                    self.assertEqual(self.rejected(tool, match_mode=mode).code, "invalid_request")
        self.rejected(AGGREGATE, offset=1)
        self.rejected(SECTION, section="../../manifest.json")
        self.rejected(SECTION, section="/tables/0/rows", limit=101)

    def test_report_authorization_precedes_any_artifact_or_report_read(self) -> None:
        forbidden = ToolAuthority("research-fixture", "other-owner", frozenset())
        with patch.object(self.store, "get_report", side_effect=AssertionError("unauthorized read")) as read:
            for tool, extra in ((QUERY, {}), (AGGREGATE, {}), (SECTION, {"section": "index"})):
                with self.subTest(tool=tool):
                    with self.assertRaises(AgentToolError) as caught:
                        self.service.invoke(tool, {"report_id": self.report["report_id"], **extra}, forbidden)
                    self.assertEqual(caught.exception.code, "report_forbidden")
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
