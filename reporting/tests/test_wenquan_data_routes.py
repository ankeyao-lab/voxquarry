"""Shared source union and authorization tests, independent of API extras."""
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from reporting import source
from reporting.agent_tools import AgentToolError
from reporting.tests.test_market_region import SnapshotFixture, _row
from reporting.wenquan_data_routes import SharedResearchData, shared_data_authority
from reporting.wenquan_datasets import DatasetError


class SharedResearchDataTests(unittest.TestCase):
    def test_unhandled_sqlite_error_at_route_keeps_primary_code_without_sql_or_path(self):
        import sqlite3
        from reporting.wenquan_data_routes import research_data_router
        endpoint = research_data_router(self.service).routes[0].endpoint
        with patch('reporting.wenquan_data_routes.shared_data_authority'), \
             patch('reporting.wenquan_data_routes.SharedResearchData') as backend:
            backend.return_value.invoke.side_effect = sqlite3.OperationalError('private database /path query text')
            response = endpoint('aggregate', {}, types.SimpleNamespace(headers={}))
        value = json.loads(response.body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(value['error']['diagnostic'],
                         {'reason': 'dataset_sqlite_error', 'phase': 'aggregate', 'sqlite_code': 0})
        self.assertNotIn('/path', response.body.decode())

    def test_category_scope_and_exact_job_groups_reach_upstream_metadata_query(self):
        _, item = self.add_live()
        item['data']['selected_run']['job']['category'] = 'Misting Fan'
        calls = []
        def catalog(cursor):
            raise AssertionError('unfiltered history must not be read')
        def metadata(cursor, job_id, filters):
            calls.append((job_id, filters))
            page = json.loads(json.dumps(self.page))
            page['items'][0]['data'].update(ready=False, verification='deferred')
            return page
        catalog.metadata = metadata
        catalog.for_job = lambda job_id: self.page
        catalog.for_jobs = lambda cursor, job_ids, filters: self.page
        self.backend.live_catalog = catalog
        result = self.backend.catalog({'category_ids': ['misting_fan'], 'job_ids': ['new']})
        self.assertEqual(result['total'], 1)
        self.assertEqual(calls[0][0], None)
        self.assertEqual(calls[0][1]['job_ids'], ['new'])
        self.assertIn('misting fan', calls[0][1]['categories'])
        self.assertNotIn('fan', calls[0][1]['categories'])

    def test_metadata_catalog_is_separate_and_missing_current_artifact_is_visible(self):
        self.add_live()
        def catalog(cursor):
            return self.page
        calls = []
        def metadata(cursor, job_id, filters):
            calls.append(filters)
            page = json.loads(json.dumps(self.page))
            page['items'][0]['data'].update(ready=False, verification='deferred')
            page['items'][0]['data'].pop('artifacts')
            page['items'].append({'job_id':'unavailable', 'status':'completed', 'channel':'ego', 'data':{'ready':False,'reason':'artifact_unavailable'}})
            return page
        catalog.metadata = metadata
        self.backend.live_catalog = catalog
        result = self.backend.catalog()
        self.assertEqual(len(calls), 1)
        self.assertFalse(result['catalog_complete'])
        self.assertEqual(result['unavailable_sources'][0]['job_id'], 'unavailable')
        manifest = self.backend.open(origin='wotu')
        self.assertFalse(manifest['source_coverage']['complete'])
        self.assertEqual(manifest['catalog_binding']['unavailable_count'], 1)
        self.assertEqual(manifest['source_coverage']['opened_job_ids'], ['new'])

    def test_trusted_catalog_classifies_timeout_invalid_json_and_authorization(self):
        import os, urllib.error
        from unittest.mock import Mock
        from reporting.wenquan_data_routes import collector_catalog_from_environment
        env={'VOXQUARRY_RESEARCH_COLLECTOR_URL':'http://collector.invalid','VOXQUARRY_RESEARCH_BRIDGE_TOKEN':'synthetic-token'}
        for exception, code in [(TimeoutError(), 'wotu_catalog_timeout'),
                (urllib.error.HTTPError('http://fixture.invalid',401,'no',{},None), 'wotu_catalog_authorization')]:
            with patch.dict(os.environ,env), patch('urllib.request.build_opener') as build:
                build.return_value.open.side_effect=exception
                catalog=collector_catalog_from_environment()
                with self.assertRaises(DatasetError) as caught: catalog.metadata(None,None,{})
                self.assertEqual(caught.exception.code,code)
                request=build.return_value.open.call_args.args[0]
                self.assertIn('verify=false',request.full_url)
                self.assertIn('limit=50',request.full_url)
        with patch.dict(os.environ,env), patch('urllib.request.build_opener') as build:
            response=build.return_value.open.return_value.__enter__.return_value
            response.status=200; response.read.return_value=b'not json'
            catalog=collector_catalog_from_environment()
            with self.assertRaises(DatasetError) as caught: catalog(None)
            self.assertEqual(caught.exception.code,'wotu_catalog_invalid')
            self.assertIn('limit=5',build.return_value.open.call_args.args[0].full_url)

    def test_model_catalog_prefilters_live_history_before_artifact_verification(self):
        self.add_live()
        def catalog(cursor):
            raise AssertionError('unscoped live catalog')
        seen = []
        def filtered(cursor, filters):
            seen.append(filters)
            return self.page
        catalog.for_scope = filtered
        self.backend.live_catalog = catalog
        self.backend.catalog({'models':['LB8803-2'], 'brands':['EGO']})
        self.assertEqual(seen, [{'models':['LB8803-2'], 'brands':['EGO']}])

    def test_workspace_permission_error_is_structured_and_not_retryable(self):
        from reporting.wenquan_data_routes import research_data_router
        router = research_data_router(self.service)
        endpoint = router.routes[0].endpoint
        with patch('reporting.wenquan_data_routes.shared_data_authority'), patch('reporting.wenquan_data_routes.SharedResearchData', side_effect=PermissionError('private filesystem path')):
            response = endpoint('catalog', {}, types.SimpleNamespace(headers={}))
        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload['error']['code'], 'dataset_storage_unavailable')
        self.assertFalse(payload['error']['retryable'])
        self.assertNotIn('private filesystem', response.body.decode())

    def test_exact_job_open_does_not_scan_unrelated_collection_history(self):
        self.add_live()
        def catalog(cursor):
            raise AssertionError('must not enumerate all collection history')
        seen = []
        def one(job_id):
            seen.append(job_id)
            return self.page
        catalog.for_job = one
        self.backend.live_catalog = catalog
        manifest = self.backend.open({'job_ids': ['new']}, origin='wotu')
        self.assertEqual(seen, ['new', 'new'])  # plan metadata, then exact version verification
        self.assertEqual(manifest['record_counts']['occurrences'], 1)

    def test_combined_temporary_files_stay_in_private_workspace(self):
        self.add_snapshot()
        self.add_live()
        original = tempfile.TemporaryDirectory
        def checked(*args, **kwargs):
            self.assertTrue(Path(kwargs['dir']).is_relative_to(self.backend.workspace))
            return original(*args, **kwargs)
        with patch('reporting.wenquan_data_routes.tempfile.TemporaryDirectory', side_effect=checked):
            result = self.backend.open({'job_ids': ['old', 'new']})
        self.assertEqual(result['record_counts']['occurrences'], 2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = SnapshotFixture(self.root / "snapshot")
        self.live = SnapshotFixture(self.root / "live")
        self.service = types.SimpleNamespace(snapshot_root=self.snapshot.root, var_root=self.root / "var")
        self.page = {"schema_version": "voxquarry.research-collection/v1", "items": [], "next_cursor": None}
        self.backend = SharedResearchData(self.service, live_root=self.live.root, live_catalog=lambda cursor: self.page)

    def add_snapshot(self, job_id="old"):
        self.snapshot.add_run(job_id, "North America", [_row("old-review", "North America")])

    def add_live(self, job_id="new"):
        self.live.add_run(job_id, "North America", [_row("new-review", "North America")])
        job = next(job for job in source.load_jobs(self.live.root) if job["id"] == job_id)
        job["active_attempt_no"] = 4
        directory = self.live.root / "data" / "runs" / job_id / "ego"
        (directory / "reviews.raw.jsonl").write_text('{"id":"new-review","text":"all raw fields"}\n')
        artifacts = [{"name": name, "bytes": (directory / name).stat().st_size,
            "sha256": "sha256:" + hashlib.sha256((directory / name).read_bytes()).hexdigest()}
            for name in ("validation_report.json", "reviews.csv", "reviews.raw.jsonl")]
        item = {"job_id": job_id, "attempt_no": 4, "data": {"ready": True, "artifacts": artifacts,
                "selected_run": {"job": job, "run_dir": str(directory.relative_to(self.live.root))}}}
        self.page["items"].append(item)
        return directory, item

    def test_new_job_absent_from_snapshot_catalog_and_open_uses_live(self):
        self.add_snapshot()
        self.add_live()
        self.live.database.unlink()  # It must never be read by this route.
        catalog = self.backend.catalog({"job_ids": ["new"]})
        self.assertEqual([item["job_id"] for item in catalog["items"]], ["new"])
        self.assertEqual(catalog["items"][0]["origin"], "wotu")
        manifest = self.backend.open({"job_ids": ["new"]})
        self.assertEqual(manifest["inputs"][0]["job"]["active_attempt_no"], 4)
        self.assertTrue(manifest["raw_payload_complete"])

    def test_mixed_snapshot_live_selection_freezes_both_and_keeps_counts(self):
        self.add_snapshot()
        self.add_live()
        manifest = self.backend.open({"job_ids": ["old", "new"]})
        self.assertEqual({item["job"]["id"] for item in manifest["inputs"]}, {"old", "new"})
        self.assertEqual(manifest["record_counts"]["occurrences"], 2)
        self.assertEqual(self.backend.invoke("query", {"dataset_id": manifest["dataset_id"]})["total"], 2)
        self.assertEqual(self.backend.open({"job_ids": ["old", "new"]})["dataset_id"], manifest["dataset_id"])

    def test_auto_unfiltered_open_includes_new_live_sources(self):
        self.add_snapshot()
        self.add_live()
        manifest = self.backend.open()
        self.assertEqual({item["job"]["id"] for item in manifest["inputs"]}, {"old", "new"})

    def test_current_live_version_replaces_snapshot_same_job(self):
        self.add_snapshot("same")
        self.add_live("same")
        manifest = self.backend.open({"job_ids": ["same"]})
        self.assertEqual(len(manifest["inputs"]), 1)
        self.assertEqual(manifest["inputs"][0]["job"]["active_attempt_no"], 4)

    def test_live_recovery_removes_only_that_jobs_snapshot_missing_entry(self):
        for job_id in ("restored", "stillmissing"):
            self.add_snapshot(job_id)
            (self.snapshot.root / "data/runs" / job_id / "ego/validation_report.json").unlink()
        self.add_live("restored")
        catalog = self.backend.catalog()
        self.assertEqual([item["job_id"] for item in catalog["items"]], ["restored"])
        self.assertEqual(catalog["items"][0]["origin"], "wotu")
        self.assertEqual([item["job_id"] for item in catalog["unavailable_sources"]], ["stillmissing"])
        self.assertEqual(catalog["unavailable_sources"][0]["reason"], "validation_manifest_missing")
        self.assertFalse(catalog["catalog_complete"])
        selected = self.backend.catalog({"job_ids": ["restored"]})
        self.assertEqual(selected["unavailable_sources"], [])
        self.assertTrue(selected["catalog_complete"])
        selected = self.backend.catalog({"job_ids": ["restored", "stillmissing"]})
        self.assertFalse(selected['catalog_complete'])
        self.assertEqual(selected['unavailable_count'], 1)
        self.add_live("stillmissing")
        restored = self.backend.catalog()
        self.assertEqual({item["job_id"] for item in restored["items"]}, {"restored", "stillmissing"})
        self.assertEqual(restored["unavailable_sources"], [])
        self.assertTrue(restored["catalog_complete"])

    def test_missing_union_job_is_not_silently_dropped(self):
        self.add_snapshot()
        self.add_live()
        page = self.backend.catalog({'job_ids': ['new', 'nonexistent']})
        self.assertEqual(page['unavailable_sources'][0]['job_id'], 'nonexistent')
        self.assertFalse(page['catalog_complete'])
        with self.assertRaises(DatasetError):
            self.backend.open({'job_ids': ['new', 'nonexistent']})

    def test_broad_union_opens_valid_data_and_retains_missing_source_coverage(self):
        self.add_snapshot('missing')
        (self.snapshot.root / 'data/runs/missing/ego/validation_report.json').unlink()
        self.add_snapshot('old')
        self.add_live('new')
        self.page['items'].append({'job_id': 'missing', 'status': 'completed', 'channel': 'ego',
            'data': {'ready': False, 'reason': 'validation_manifest_mismatch'}})
        catalog = self.backend.catalog()
        self.assertFalse(catalog['catalog_complete'])
        self.assertEqual(len(catalog['unavailable_sources']), 1)
        result = self.backend.open()
        self.assertEqual(result['record_counts']['occurrences'], 2)
        self.assertEqual(result['source_coverage']['opened_job_ids'], ['new', 'old'])
        self.assertFalse(result['source_coverage']['complete'])
        self.assertEqual(result['source_coverage']['unavailable_sources'], catalog['unavailable_sources'])
        with self.assertRaises(DatasetError) as caught:
            self.backend.open({'job_ids': ['missing', 'new']})
        self.assertEqual(caught.exception.code, 'source_unavailable')

    def test_broad_union_does_not_hide_unsafe_historical_files(self):
        self.add_snapshot('unsafe')
        path = self.snapshot.root / 'data/runs/unsafe/ego/validation_report.json'
        saved = path.read_bytes()
        path.unlink()
        target = self.root / 'outside.json'
        target.write_bytes(saved)
        path.symlink_to(target)
        self.add_live()
        with self.assertRaises(DatasetError) as caught:
            self.backend.open()
        self.assertEqual(caught.exception.code, 'unsafe_source')

    def test_catalog_requires_matching_attempt_and_artifact_hashes(self):
        _, item = self.add_live()
        item["attempt_no"] = 5
        with self.assertRaises(DatasetError):
            self.backend.catalog()
        item["attempt_no"] = 4
        item["data"].pop("artifacts")
        with self.assertRaises(DatasetError):
            self.backend.catalog()

    def test_live_file_changes_after_catalog_are_rejected_during_copy(self):
        directory, _ = self.add_live()
        path = directory / "reviews.csv"
        path.write_text(path.read_text().replace("new-review", "bad-review"))
        with self.assertRaises(DatasetError) as raised:
            self.backend.open({"job_ids": ["new"]})
        self.assertEqual(raised.exception.code, "source_changed")

    def test_mixed_origin_also_pins_live_hashes(self):
        self.add_snapshot()
        directory, _ = self.add_live()
        path = directory / "reviews.raw.jsonl"
        path.write_text('{"id":"same-count-other-attempt"}\n')
        with self.assertRaises(DatasetError) as raised:
            self.backend.open({"job_ids": ["old", "new"]})
        self.assertEqual(raised.exception.code, "source_changed")

    def test_partial_or_not_ready_entry_never_becomes_selected_run(self):
        self.page["items"] = [{"job_id": "waiting", "attempt_no": 2, "data": {"ready": False}}]
        self.assertEqual(self.backend._live_runs(), [])

    def test_raw_export_type_is_available_but_paths_and_sql_are_rejected(self):
        self.add_live()
        manifest = self.backend.open({"job_ids": ["new"]})
        raw = b"".join(self.backend.invoke("export", {"dataset_id": manifest["dataset_id"], "source_type": "channel_raw_payload"}))
        self.assertEqual(json.loads(raw)["text"], "all raw fields")
        for payload in ({"scope": {}, "selected_runs": []}, {"scope": {}, "run_dir": "/etc"}):
            with self.assertRaises(DatasetError):
                self.backend.invoke("open", payload)

    def test_shared_data_requires_all_report_authority_and_is_not_owner_scoped(self):
        headers = {"x-voxquarry-gateway-token": "fixture", "x-voxquarry-authority-id": "gateway",
            "x-voxquarry-principal-id": "user-one", "x-voxquarry-allowed-report-ids": "*"}
        first = shared_data_authority(headers, "fixture")
        second = shared_data_authority({**headers, "x-voxquarry-principal-id": "user-two"}, "fixture")
        self.assertIsNone(first.allowed_report_ids)
        self.assertIsNone(second.allowed_report_ids)
        with self.assertRaises(AgentToolError) as raised:
            shared_data_authority({**headers, "x-voxquarry-allowed-report-ids": "rpt_agentfixture1234567890"}, "fixture")
        self.assertEqual(raised.exception.http_status, 403)
        with self.assertRaises(AgentToolError):
            shared_data_authority(headers, "wrong-token")

    def test_analysis_of_new_live_dataset_needs_no_saved_report(self):
        self.add_live()
        manifest = self.backend.invoke("open", {"scope": {"job_ids": ["new"]}})
        definition = self.backend.invoke("definition", {"category_id": "leaf_blower"})
        result = self.backend.invoke("analyze", {"dataset_id": manifest["dataset_id"], "definition_id": definition["definition_id"]})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["dataset_id"], manifest["dataset_id"])
        self.assertEqual(result["sample"]["written_entities"], 1)
        self.assertEqual(result["schema_version"], "wenquan-dataset-analysis/v1")
        self.assertFalse((self.service.var_root / "reports").exists())
        for operation, payload in (("definition", {"category_id": "leaf_blower", "path": "/etc"}),
                                   ("analyze", {"dataset_id": manifest["dataset_id"], "product_url": "https://example.test/other"}),
                                   ("analyze", {"dataset_id": manifest["dataset_id"], "script": "arbitrary.py"})):
            with self.assertRaises(DatasetError):
                self.backend.invoke(operation, payload)

    def test_analysis_concurrency_slot_is_released_after_error(self):
        self.backend._analyzing.acquire()
        try:
            with self.assertRaises(DatasetError) as raised:
                self.backend.invoke("analyze", {"dataset_id": "invalid"})
            self.assertEqual(raised.exception.code, "analysis_busy")
        finally:
            self.backend._analyzing.release()
        with self.assertRaises(DatasetError):
            self.backend.invoke("analyze", {"dataset_id": "invalid"})
        self.assertTrue(self.backend._analyzing.acquire(blocking=False))
        self.backend._analyzing.release()


if __name__ == "__main__":
    unittest.main()
