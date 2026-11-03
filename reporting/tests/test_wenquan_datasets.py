from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from reporting import source
from reporting.tests.test_market_region import SnapshotFixture, _row
from reporting.tests.test_partial_source_completeness import governed_partial_report, review_row
from reporting.wenquan_datasets import DatasetError, DatasetLimits, WenquanDatasets


class WenquanDatasetTests(unittest.TestCase):
    def test_snapshot_scope_is_applied_before_run_limit_and_validation_reads(self):
        self.add(job_id='inside')
        self.add(job_id='outside')
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("UPDATE jobs SET category='Pressure Washer' WHERE id='outside'")
        outside = self.fixture.root / 'data/runs/outside/ego/validation_report.json'
        outside.unlink()
        self.service.limits = replace(self.service.limits, max_runs=1)
        result = self.service.catalog({'category_ids': ['leaf_blower']})
        self.assertEqual([item['job_id'] for item in result['items']], ['inside'])
        with patch.object(self.service, '_copy', wraps=self.service._copy) as copy:
            self.assertEqual([r['job']['id'] for r in self.service._iter_jobs(job_ids=['inside'])], ['inside'])
            self.assertEqual([r['job']['id'] for r in self.service._iter_jobs(job_ids=['outside'])], ['outside'])
        self.assertEqual(copy.call_count, 0)  # private snapshot copy already verified
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("UPDATE jobs SET model='Corrected' WHERE id='inside'")
        with patch.object(self.service, '_copy', wraps=self.service._copy) as copy:
            self.assertEqual(list(self.service._iter_jobs(job_ids=['inside']))[0]['job']['model'], 'Corrected')
        self.assertEqual(copy.call_count, 1)

    def test_product_form_metadata_and_contract_versions_change_identity(self):
        self.add()
        job = source.load_jobs(self.fixture.root)[0]
        selected = {'job': job, 'run_dir': 'data/runs/' + job['id'] + '/ego'}
        before = self.service.open(selected_runs=[selected])
        changed = {**selected, 'job': {**job, 'product_form_factor': 'handheld',
                   'product_form_evidence_source': 'fixture_metadata'}}
        after = self.service.open(selected_runs=[changed])
        self.assertNotEqual(before['dataset_id'], after['dataset_id'])
        self.assertEqual(self.service.query(before['dataset_id'])['items'][0]['product_form_factor'], 'unknown')
        self.assertEqual(self.service.query(after['dataset_id'])['items'][0]['product_form_factor'], 'handheld')
        self.assertEqual(after, self.service.open(selected_runs=[changed]))
        with patch.object(source, 'PARSER_VERSION', 'fixture-parser/new'):
            different_version = self.service.open(selected_runs=[changed])
        self.assertNotEqual(after['dataset_id'], different_version['dataset_id'])
        self.assertEqual(self.service.manifest(before['dataset_id']), before)

    def test_legacy_manifest_identity_recipe_remains_readable(self):
        import shutil
        from reporting.wenquan_datasets import _hash, _json
        self.add()
        current = self.service.open()
        legacy = {**current, 'version': 'wenquan-dataset/1.1.0'}
        legacy.pop('normalization_contracts')
        identity = {key: legacy[key] for key in ('contract', 'version', 'normalization_version', 'scope', 'inputs', 'shard_bytes')}
        old_id = 'ds_' + _hash(identity)[7:]
        legacy.update(dataset_id=old_id, dataset_version=old_id)
        legacy['manifest_sha256'] = _hash({key: value for key, value in legacy.items() if key != 'manifest_sha256'})
        destination = self.service.storage_root / old_id
        shutil.copytree(self.service.storage_root / current['dataset_id'], destination)
        manifest = destination / 'manifest.json'
        manifest.chmod(0o600); manifest.write_text(_json(legacy)); manifest.chmod(0o400)
        self.assertEqual(self.service.manifest(old_id), legacy)
        self.assertEqual(self.service.query(old_id)['total'], current['record_counts']['review_entities'])

    def test_cache_probe_is_read_only_and_recovery_counters_are_visible(self):
        self.add()
        root = self.service.storage_root
        self.assertEqual(self.service.cache_health(root)['status'], 'empty')
        self.assertFalse((root / 'query-cache.sqlite3').exists())
        self.service.limits = replace(self.service.limits, max_cached_queries=1)
        dataset = self.service.open()['dataset_id']
        self.service.query(dataset)
        self.service.query(dataset)
        self.service.query(dataset, query='new-filter')
        info = self.service.cache_health(root, self.service.limits)
        self.assertEqual(info['status'], 'capacity_reached_fallback_available')
        self.assertEqual(info['keys'], 1)
        before = (root / 'query-cache.sqlite3').read_bytes()
        self.service.cache_health(root, self.service.limits)
        self.assertEqual(before, (root / 'query-cache.sqlite3').read_bytes())
        self.service.limits = replace(self.service.limits, max_cache_bytes=1)
        self.assertEqual(self.service.query(dataset)['total'], 2)
        metrics = self.service.query_metrics()
        self.assertEqual({k: metrics[k] for k in ('hits', 'misses', 'evictions', 'fallbacks', 'queries')},
                         {'hits': 1, 'misses': 2, 'evictions': 1, 'fallbacks': 1, 'queries': 4})
        self.assertGreater(metrics['query_seconds'], 0)

    def test_snapshot_catalog_database_has_its_own_bounded_capacity(self):
        self.add()
        # A multi-task SQLite catalog is larger than one manifest. Keep the
        # manifest limit small without preventing discovery of valid jobs.
        self.service.limits = replace(DatasetLimits(), max_metadata_bytes=1024)
        self.assertEqual(len(self.service._jobs(None)), 1)
        self.service.limits = replace(self.service.limits, max_catalog_bytes=1024)
        with self.assertRaises(DatasetError) as caught:
            self.service._jobs(None)
        self.assertEqual(caught.exception.code, 'dataset_too_large')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = SnapshotFixture(self.root / "source")
        self.service = WenquanDatasets(self.fixture.root, self.root / "datasets")

    def tearDown(self):
        self.temporary.cleanup()

    def add(self, job_id="one", rows=None, **kwargs):
        rows = rows if rows is not None else [_row("r1", "North America"), _row("r2", "North America", text="", valid_text="false")]
        self.fixture.add_run(job_id, "North America", rows, **kwargs)
        return self.fixture.root / "data" / "runs" / job_id / kwargs.get("channel", "ego")

    def test_no_report_required_full_original_text_and_stable_exports(self):
        original = "  After rain,\nI turn slowly.  It does not jam. " + "complete detail " * 12000 + "FINAL_SENTINEL"
        self.add(rows=[_row("r1", "North America", text=original)])
        directory = self.service.catalog()
        self.assertFalse(directory["report_required"])
        self.assertFalse(directory["items"][0]["counts_verified"])
        first = self.service.open()
        self.assertNotIn("report_id", first)
        self.assertTrue(first["export_complete"])
        self.assertTrue(first["source_complete"])
        self.assertEqual(first["record_counts"]["review_entities"], 1)
        answer = self.service.query(first["dataset_id"])
        fact = answer["items"][0]
        self.assertEqual(fact["original_fields"]["review_text"], original)
        self.assertTrue(fact["review_text"].endswith("FINAL_SENTINEL"))
        self.assertIsNone(fact["attempt_no"])
        exported = [json.loads(line) for line in b"".join(self.service.export(first["dataset_id"])).splitlines()]
        self.assertEqual(exported, answer["items"])
        self.assertEqual(self.service.open(), first)

    def test_normalization_and_cross_channel_entity_dedup_match_reporting(self):
        row = _row("same", "North America")
        self.add("one", [row])
        twin = {**row, "channel": "lowes"}
        self.add("two", [twin], channel="lowes")
        manifest = self.service.open()
        self.assertEqual(manifest["record_counts"]["occurrences"], 2)
        self.assertEqual(manifest["record_counts"]["review_entities"], 1)
        expected, _, _ = source.build_review_fact(self.fixture.root, {})
        actual = self.service.query(manifest["dataset_id"], population="occurrences")["items"]
        actual.sort(key=lambda fact: fact["canonical_entity_rank"])
        for old, new in zip(expected, actual):
            for field in old:
                self.assertEqual(new[field], old[field], field)
        counts = self.service.aggregate(manifest["dataset_id"], group_by=["rating"])
        self.assertEqual(counts["total"], 1)

    def test_unknown_identity_is_not_accidentally_deduplicated(self):
        rows = [_row("", "North America"), _row("", "North America")]
        self.add(rows=rows)
        manifest = self.service.open()
        self.assertEqual(manifest["record_counts"]["review_entities"], 2)
        self.assertEqual(self.service.aggregate(manifest["dataset_id"])["total"], 2)

    def test_query_cursor_uses_cached_membership_without_source_rescan(self):
        self.add(rows=[_row(f"r{i}", "North America", text=f"Battery scene {i}") for i in range(8)])
        manifest = self.service.open()
        first = self.service.query(manifest["dataset_id"], query="battery", limit=3)
        self.assertEqual(first["total"], 8)
        with patch.object(self.service, "_jobs", side_effect=AssertionError("source rescan")), patch.object(source, "_normalized_fact", side_effect=AssertionError("source rescan")):
            second = self.service.query(manifest["dataset_id"], query="battery", cursor=first["next_cursor"], limit=3)
            third = self.service.query(manifest["dataset_id"], query="battery", cursor=second["next_cursor"], limit=3)
        self.assertEqual([first["page"]["returned"], second["page"]["returned"], third["page"]["returned"]], [3, 3, 2])
        self.assertFalse(third["page"]["has_more"])
        with sqlite3.connect(self.root / "datasets" / "query-cache.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM queries").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM members").fetchone()[0], 8)
        with self.assertRaises(DatasetError):
            self.service.query(manifest["dataset_id"], query="different", cursor=first["next_cursor"])

    def test_selected_runs_work_without_any_database(self):
        run_dir = self.add()
        job = source.load_jobs(self.fixture.root)[0]
        job["active_attempt_no"] = 3
        self.fixture.database.unlink()
        manifest = self.service.open(selected_runs=[{"job": job, "run_dir": run_dir}, {"job": job, "run_dir": run_dir}])
        self.assertEqual(manifest["record_counts"]["occurrences"], 2)
        self.assertEqual(self.service.query(manifest["dataset_id"])["items"][0]["attempt_no"], 3)
        self.assertEqual(len(manifest["inputs"]), 1)
        with self.assertRaisesRegex(DatasetError, "一致快照"):
            self.service.open()

    def test_exact_source_product_selection_does_not_pool_neighbor_skus(self):
        rows = [{**_row("target", "North America"), "source_product_id": "sku-target"},
                {**_row("neighbor", "North America"), "source_product_id": "sku-neighbor"}]
        self.add(rows=rows)
        full = self.service.open({"job_ids": ["one"]})
        selected = self.service.open({"job_ids": ["one"], "source_product_ids": ["sku-target"]})
        self.assertNotEqual(selected["dataset_id"], full["dataset_id"])
        self.assertEqual(selected["record_counts"]["review_entities"], 1)
        self.assertEqual(selected["eligible_source_records"], 2)
        reviews = self.service.query(selected["dataset_id"])["items"]
        self.assertEqual([r["review_id"] for r in reviews], ["target"])
        self.assertEqual(self.service.query(full["dataset_id"], filters={"source_product_ids": ["sku-neighbor"]})["items"][0]["review_id"], "neighbor")
        self.assertEqual(self.service.aggregate(selected["dataset_id"])["total"], 1)

    def test_scope_date_filters_and_grouped_counts(self):
        self.add(rows=[_row("good", "North America"), {**_row("bad", "North America"), "rating": "1", "submission_time": "2026-08-01"}])
        manifest = self.service.open({"date_range": {"start": "2026-07-01", "end": "2026-07-31"}})
        self.assertEqual(manifest["record_counts"]["occurrences"], 1)
        # Underlying source counts stay distinct from the selected population.
        self.assertEqual(manifest["eligible_source_records"], 2)
        full = self.service.open()
        result = self.service.aggregate(full["dataset_id"], group_by=["rating", "submission_month"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(self.service.query(full["dataset_id"], filters={"ratings": [1]})["total"], 1)
        self.assertEqual(self.service.query(full["dataset_id"], review_ids=["bad"])["items"][0]["rating"], 1)

    def test_explicit_missing_job_does_not_silently_shrink_scope(self):
        self.add()
        with self.assertRaisesRegex(DatasetError, "部分指定任务"):
            self.service.open({"job_ids": ["one", "missing"]})

    def test_catalog_reports_missing_historical_manifest_without_hiding_readable_jobs(self):
        missing = self.add("missing")
        self.add("available")
        (missing / "validation_report.json").unlink()
        result = self.service.catalog()
        self.assertEqual([item["job_id"] for item in result["items"]], ["available"])
        self.assertEqual(result["unavailable_sources"], [{"job_id": "missing", "channel": "ego", "reason": "validation_manifest_missing"}])
        self.assertFalse(result["catalog_complete"])
        self.assertNotIn(str(self.fixture.root), json.dumps(result))
        # Discovery does not authorize silently dropping a requested source.
        with self.assertRaises(DatasetError):self.service.catalog({"job_ids": ["available", "missing"]})
        with self.assertRaises(DatasetError):self.service.open({"job_ids": ["available", "missing"]})
        with self.assertRaises(DatasetError):self.service.open()
        result = self.service.catalog({"job_ids": ["available"]})
        self.assertTrue(result["catalog_complete"])
        self.assertEqual(self.service.open({"job_ids": ["available"]})["record_counts"]["review_entities"], 2)

    def test_catalog_missing_manifest_does_not_hide_unsafe_or_versioned_sources(self):
        directory = self.add()
        path = directory / "validation_report.json"
        job = source.load_jobs(self.fixture.root)[0]
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises(DatasetError):self.service.catalog(selected_runs=[{"job": job, "run_dir": directory}])
        elsewhere = self.root / "original.json";elsewhere.write_bytes(original)
        path.symlink_to(elsewhere)
        with self.assertRaises(DatasetError):self.service.catalog()
        path.unlink();os.link(elsewhere, path)
        with self.assertRaises(DatasetError):self.service.catalog()
        path.unlink();path.write_text("invalid JSON")
        with self.assertRaises(DatasetError):self.service.catalog()

    def test_changed_source_creates_new_dataset_and_old_citations_remain_frozen(self):
        directory = self.add(rows=[_row("r1", "North America", text="Original experience")])
        old = self.service.open()
        path = directory / "reviews.csv"
        path.write_text(path.read_text().replace("Original experience", "Updated experience"))
        new = self.service.open()
        self.assertNotEqual(old["dataset_id"], new["dataset_id"])
        self.assertEqual(self.service.query(old["dataset_id"])["items"][0]["review_text"], "Original experience")
        self.assertEqual(self.service.query(new["dataset_id"])["items"][0]["review_text"], "Updated experience")

    def test_source_mutation_during_freeze_never_publishes_dataset(self):
        directory = self.add()
        original_copy = self.service._copy
        def change(relative, *args, **kwargs):
            value = original_copy(relative, *args, **kwargs)
            if relative.name == "reviews.csv":
                with (directory / "reviews.csv").open("a") as handle:
                    handle.write("\n")
            return value
        with patch.object(self.service, "_copy", side_effect=change), self.assertRaisesRegex(DatasetError, "发生变化"):
            self.service.open()
        self.assertEqual(list((self.root / "datasets").glob("ds_*")), [])

    def test_cancelled_freeze_never_publishes(self):
        self.add()
        checks = 0
        def check_active():
            nonlocal checks
            checks += 1
            if checks >= 5:
                raise DatasetError("run_cancelled", "研究已取消")
        with self.assertRaisesRegex(DatasetError, "已取消"):
            self.service.open(check_active=check_active)
        self.assertEqual(list((self.root / "datasets").glob("ds_*")), [])

    def test_partial_counts_use_governed_source_contract(self):
        rows = [review_row("r1"), review_row("r2"), review_row("r3")]
        self.add(rows=rows, channel="homedepot", report_overrides=governed_partial_report())
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("UPDATE jobs SET status='partial_success'")
        manifest = self.service.open()
        self.assertTrue(manifest["export_complete"])
        self.assertFalse(manifest["source_complete"])
        self.assertEqual(manifest["source_completeness"][0]["missing_review_count"], 2)
        self.assertEqual(manifest["raw_record_count"], 3)
        self.assertEqual(manifest["record_counts"]["review_entities"], 3)

    def test_excluded_rows_do_not_become_valid_reviews(self):
        self.add(excluded_reviews=5)
        manifest = self.service.open()
        self.assertEqual(manifest["raw_record_count"], 7)
        self.assertEqual(manifest["source_excluded_records"], 5)
        self.assertEqual(manifest["record_counts"]["occurrences"], 2)
        self.assertEqual(manifest["record_counts"]["written_entities"], 1)
        self.assertEqual(self.service.aggregate(manifest["dataset_id"])["total"], 2)

    def test_wrong_validation_counts_rejected(self):
        self.add(report_overrides={"eligible_reviews": 999})
        with self.assertRaisesRegex(DatasetError, "实际 CSV") as caught:
            self.service.open()
        diagnostic = caught.exception.diagnostic
        self.assertEqual({key: diagnostic[key] for key in ('reason', 'phase', 'field', 'expected', 'observed')},
                         {'reason': 'validation_count_mismatch', 'phase': 'validation_manifest',
                          'field': 'eligible_reviews', 'expected': 2, 'observed': 999})
        self.assertNotIn('limit', diagnostic)

    def test_completeness_failure_has_safe_equation_and_counts(self):
        self.add(report_overrides={'source_unique_reviews': 7, 'source_total': 7})
        with self.assertRaises(DatasetError) as caught:
            self.service.open()
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic['reason'], 'source_completeness_mismatch')
        self.assertEqual(diagnostic['source_reason'], 'eligible_excluded_mismatch')
        self.assertEqual(diagnostic['field'], 'collected_source_unique_reviews')
        self.assertEqual((diagnostic['expected'], diagnostic['observed']), (2, 7))
        self.assertEqual(diagnostic['counts']['csv_eligible_rows'], 2)
        self.assertNotIn(str(self.root), json.dumps(diagnostic))

    def test_sqlite_failures_and_diagnostic_fields_are_bounded(self):
        self.add()
        with patch.object(self.service, '_freeze', side_effect=sqlite3.OperationalError('database or disk is full')):
            with self.assertRaises(DatasetError) as caught:
                self.service.open()
        self.assertEqual(caught.exception.diagnostic,
                         {'reason': 'dataset_sqlite_error', 'phase': 'freeze', 'sqlite_code': 13})
        error = DatasetError('invalid_source', 'safe', reason='/private/source', phase='/private/source',
            field='user_email', expected=True, observed=-1, sqlite_code=999,
            source_reason='private source detail', counts={'source_total': 2, 'body': 'private', 'unique_reviews': False})
        self.assertEqual(error.diagnostic, {'counts': {'source_total': 2}})

    def test_symlink_hardlink_and_parent_link_sources_rejected(self):
        directory = self.add()
        csv_path = directory / "reviews.csv"
        real = directory / "saved.csv"
        csv_path.rename(real)
        csv_path.symlink_to(real)
        with self.assertRaises(DatasetError):
            self.service.open()
        csv_path.unlink()
        os.link(real, csv_path)
        with self.assertRaises(DatasetError):
            self.service.open()
        csv_path.unlink()
        real.rename(csv_path)
        relocated = directory.parent / "actual"
        directory.rename(relocated)
        directory.symlink_to(relocated, target_is_directory=True)
        with self.assertRaises(DatasetError):
            self.service.open()

    def test_paths_and_ids_are_not_model_file_selectors(self):
        self.add()
        job = source.load_jobs(self.fixture.root)[0]
        for path in ("../../etc", self.root / "outside"):
            with self.subTest(path=path), self.assertRaises(DatasetError):
                self.service.open(selected_runs=[{"job": job, "run_dir": path}])
        with self.assertRaises(DatasetError):
            self.service.query("../source")
        manifest = self.service.open()
        with self.assertRaises(DatasetError):
            list(self.service.export(manifest["dataset_id"], shard="../manifest.json"))
        link = self.root / "linked-store"
        link.symlink_to(self.root / "datasets", target_is_directory=True)
        with self.assertRaises(DatasetError):
            WenquanDatasets(self.fixture.root, link)

    def test_tampered_index_or_shard_fails_closed(self):
        self.add()
        manifest = self.service.open()
        dataset = self.root / "datasets" / manifest["dataset_id"]
        self.service.query(manifest["dataset_id"])
        index = dataset / "reviews.sqlite3"
        index.chmod(0o600)
        with index.open("ab") as handle:
            handle.write(b"tampered")
        with self.assertRaisesRegex(DatasetError, "索引发生变化"):
            self.service.query(manifest["dataset_id"])
        shard = dataset / manifest["shards"][0]["name"]
        shard.chmod(0o600)
        shard.write_bytes(b"{}\n")
        with self.assertRaisesRegex(DatasetError, "分片校验"):
            list(self.service.export(manifest["dataset_id"]))

    def test_bounded_records_and_pages_do_not_truncate(self):
        self.add()
        restricted = WenquanDatasets(self.fixture.root, self.root / "limited", limits=replace(DatasetLimits(), max_records=1))
        with self.assertRaisesRegex(DatasetError, "评论数量"):
            restricted.open()
        manifest = self.service.open()
        tiny = WenquanDatasets(self.fixture.root, self.root / "datasets", limits=replace(DatasetLimits(), page_bytes=10))
        with self.assertRaisesRegex(DatasetError, "完整原文"):
            tiny.query(manifest["dataset_id"])
        for arguments in ({"limit": 0}, {"offset": -1}, {"filters": {"ratings": [True]}}, {"filters": {"sql": "1=1"}}, {"population": "everything"}):
            with self.subTest(arguments=arguments), self.assertRaises(DatasetError):
                self.service.query(manifest["dataset_id"], **arguments)

    def test_query_cache_eviction_preserves_old_cursor_and_bounded_membership(self):
        self.add(rows=[_row(f'r{i}', 'North America', text=f'Battery scene {i}') for i in range(8)])
        manifest = self.service.open()
        limited = WenquanDatasets(self.fixture.root, self.root / "datasets", limits=replace(DatasetLimits(), max_cached_queries=1))
        first = limited.query(manifest['dataset_id'], query='battery', limit=3)
        for i in range(105):
            limited.query(manifest['dataset_id'], query=f'other {i}')
        second = limited.query(manifest['dataset_id'], query='battery', limit=3, cursor=first['next_cursor'])
        third = limited.query(manifest['dataset_id'], query='battery', limit=3, cursor=second['next_cursor'])
        expected = self.service.query(manifest['dataset_id'], query='battery')['items']
        self.assertEqual(first['items'] + second['items'] + third['items'], expected)
        self.assertEqual((second['total'], third['total']), (8, 8))
        with sqlite3.connect(self.root / 'datasets/query-cache.sqlite3') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM queries').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM members WHERE key NOT IN (SELECT key FROM queries)').fetchone()[0], 0)

    def test_cache_byte_limit_and_busy_cache_fall_back_without_changing_results(self):
        self.add(rows=[_row(f'r{i}', 'North America', text='battery') for i in range(8)])
        manifest = self.service.open()
        tiny = WenquanDatasets(self.fixture.root, self.root / 'datasets', limits=replace(DatasetLimits(), max_cache_bytes=4096))
        first = tiny.query(manifest['dataset_id'], limit=3)
        second = tiny.query(manifest['dataset_id'], cursor=first['next_cursor'], limit=3)
        self.assertEqual((first['total'], second['page']['offset'], second['page']['returned']), (8, 3, 3))
        expected = self.service.query(manifest['dataset_id'])
        with sqlite3.connect(self.root / 'datasets/query-cache.sqlite3') as locked:
            locked.execute('BEGIN IMMEDIATE')
            actual = self.service.query(manifest['dataset_id'])
            self.assertEqual(actual, expected)

    def test_concurrent_eviction_never_loses_page_rows_or_totals(self):
        self.add(rows=[_row(f'r{i}', 'North America', text=f'battery {i}') for i in range(23)])
        manifest = self.service.open()
        expected = self.service.query(manifest['dataset_id'])['items']
        def read_pages(worker):
            service = WenquanDatasets(self.fixture.root, self.root / 'datasets', limits=replace(DatasetLimits(), max_cached_queries=2))
            items, cursor = [], None
            while True:
                service.query(manifest['dataset_id'], query=f'evict {worker} {len(items)}')
                page = service.query(manifest['dataset_id'], query='battery', limit=3, cursor=cursor)
                self.assertEqual(page['total'], 23)
                items.extend(page['items'])
                cursor = page['next_cursor']
                if not cursor:
                    return items
        with ThreadPoolExecutor(max_workers=6) as pool:
            for items in pool.map(read_pages, range(6)):
                self.assertEqual(items, expected)

    def test_query_rejects_corrupt_or_unsafe_cache_and_invalid_cursor_without_writes(self):
        self.add()
        manifest = self.service.open()
        path = self.root / 'datasets/query-cache.sqlite3'
        with self.assertRaises(DatasetError):
            self.service.query(manifest['dataset_id'], cursor='invalid')
        self.assertFalse(path.exists())
        path.write_bytes(b'not a database')
        with self.assertRaises(sqlite3.DatabaseError):
            self.service.query(manifest['dataset_id'])
        path.unlink()
        target = self.root / 'target'; target.write_text('unchanged')
        path.symlink_to(target)
        with self.assertRaisesRegex(DatasetError, '不安全'):
            self.service.query(manifest['dataset_id'])
        self.assertEqual(target.read_text(), 'unchanged')

    def test_query_timeout_is_bounded_and_cache_transaction_rolls_back(self):
        self.add(rows=[_row(f'r{i}', 'North America', text='battery') for i in range(400)])
        manifest = self.service.open()
        with patch('reporting.wenquan_datasets.time.monotonic', side_effect=[0] + [100] * 100):
            with self.assertRaises(DatasetError) as raised:
                self.service.query(manifest['dataset_id'])
        self.assertEqual(raised.exception.code, 'query_timeout')
        self.assertEqual(self.service.query(manifest['dataset_id'])['total'], 400)


if __name__ == "__main__":
    unittest.main()

class WenquanRawAndPinnedTests(unittest.TestCase):
    setUp = WenquanDatasetTests.setUp
    tearDown = WenquanDatasetTests.tearDown
    add = WenquanDatasetTests.add
    def pinned_run(self, *, excluded=0):
        directory = self.add(rows=[_row("r1", "North America")], excluded_reviews=excluded)
        rows = [{"id": "r1", "text": "完整原始评论\nwith exact punctuation", "private_channel_field": {"nested": [1, 2]}}]
        rows.extend({"id": f"excluded-{i}", "raw_only": True} for i in range(excluded))
        raw = b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows)
        (directory / "reviews.raw.jsonl").write_bytes(raw)
        job = source.load_jobs(self.fixture.root)[0]
        job["active_attempt_no"] = 2
        expected = [{"name": name, "bytes": (directory / name).stat().st_size,
                     "sha256": self.service._file_hash(directory / name)}
                    for name in ("validation_report.json", "reviews.csv", "reviews.raw.jsonl")]
        return directory, {"job": job, "run_dir": directory, "expected_artifacts": expected}, rows

    def test_pinned_artifacts_reject_same_count_other_attempt_content(self):
        directory, selected, _ = self.pinned_run()
        path = directory / "reviews.csv"
        path.write_text(path.read_text().replace("North America", "Other"))
        with self.assertRaisesRegex(DatasetError, "已选版本"):
            self.service.open(selected_runs=[selected])
        self.assertEqual(list(self.service.storage_root.glob("ds_*")), [])

    def test_pinned_validation_change_is_rejected_before_freeze(self):
        directory, selected, _ = self.pinned_run()
        path = directory / "validation_report.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(DatasetError, "版本已经变化"):
            self.service.catalog(selected_runs=[selected])

    def test_raw_payload_preserves_fields_and_exclusions_outside_counts(self):
        _, selected, rows = self.pinned_run(excluded=1)
        manifest = self.service.open(selected_runs=[selected])
        self.assertTrue(manifest["raw_payload_complete"])
        self.assertEqual(manifest["raw_payload_records"], 2)
        self.assertEqual(manifest["record_counts"]["review_entities"], 1)
        raw = [json.loads(line) for line in b"".join(self.service.export(manifest["dataset_id"], source_type="channel_raw_payload")).splitlines()]
        self.assertEqual(raw, rows)
        artifact = manifest["raw_artifacts"][0]
        self.assertFalse(artifact["included_in_normalized_counts"])
        self.assertEqual(artifact["declared_excluded_records"], 1)
        self.assertEqual(artifact["attempt_no"], 2)
        self.assertEqual(len(b"".join(self.service.export(manifest["dataset_id"])).splitlines()), 1)

    def test_raw_mutation_creates_new_dataset_and_prior_raw_stays_frozen(self):
        directory, selected, rows = self.pinned_run()
        first = self.service.open(selected_runs=[selected])
        (directory / "reviews.raw.jsonl").write_text('{"id":"r1","text":"new raw value"}\n')
        with self.assertRaises(DatasetError):
            self.service.open(selected_runs=[selected])
        second = self.service.open(selected_runs=[{"job": selected["job"], "run_dir": directory}])
        self.assertNotEqual(first["dataset_id"], second["dataset_id"])
        self.assertEqual(json.loads(b"".join(self.service.export(first["dataset_id"], source_type="channel_raw_payload"))), rows[0])

    def test_raw_symlink_or_invalid_json_is_rejected(self):
        directory, selected, _ = self.pinned_run()
        path = directory / "reviews.raw.jsonl"
        path.write_text('not JSON\n')
        with self.assertRaises(DatasetError):
            self.service.open(selected_runs=[{"job": selected["job"], "run_dir": directory}])
        path.unlink()
        path.symlink_to(directory / "reviews.csv")
        with self.assertRaises(DatasetError):
            self.service.open(selected_runs=[{"job": selected["job"], "run_dir": directory}])

    def test_old_source_does_not_claim_raw_payload_export_complete(self):
        self.add()
        manifest = self.service.open()
        self.assertFalse(manifest["raw_payload_complete"])
        with self.assertRaisesRegex(DatasetError, "缺少渠道原始"):
            b"".join(self.service.export(manifest["dataset_id"], source_type="channel_raw_payload"))

    def test_raw_freeze_has_aggregate_size_and_record_limits(self):
        directory, selected, _ = self.pinned_run()
        source_bytes = sum((directory / name).stat().st_size for name in ("validation_report.json", "reviews.csv", "reviews.raw.jsonl"))
        limited = WenquanDatasets(self.fixture.root, self.root / "small", limits=replace(DatasetLimits(), max_source_bytes=source_bytes - 1))
        with self.assertRaises(DatasetError):
            limited.open(selected_runs=[selected])
        self.assertEqual(list(limited.storage_root.glob("ds_*")), [])

    def test_raw_shards_are_complete_and_tamper_is_rejected(self):
        _, selected, rows = self.pinned_run(excluded=1)
        small = WenquanDatasets(self.fixture.root, self.root / "sharded", limits=replace(DatasetLimits(), shard_bytes=80))
        manifest = small.open(selected_runs=[selected])
        shards = manifest["raw_artifacts"][0]["shards"]
        self.assertEqual(len(shards), 2)
        raw = b"".join(small.export(manifest["dataset_id"], source_type="channel_raw_payload"))
        self.assertEqual([json.loads(line) for line in raw.splitlines()], rows)
        path = small.storage_root / manifest["dataset_id"] / shards[0]["name"]
        path.chmod(0o600)
        path.write_text('{"tampered":true}\n')
        with self.assertRaisesRegex(DatasetError, "分片校验失败"):
            b"".join(small.export(manifest["dataset_id"], source_type="channel_raw_payload"))
