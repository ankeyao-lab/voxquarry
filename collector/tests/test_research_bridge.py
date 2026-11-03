"""Synthetic bridge contracts; no HTTP requests, credentials or real collectors."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import uuid

try:
    from fastapi import HTTPException
except ImportError:
    fastapi = types.ModuleType("fastapi")
    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(str(detail))
            self.status_code, self.detail = status_code, detail
    fastapi.HTTPException = HTTPException
    fastapi.Request = type("Request", (), {})
    sys.modules.setdefault("fastapi", fastapi)
try:
    import requests
except ImportError:
    requests = types.ModuleType("requests")
    requests.Session = lambda: types.SimpleNamespace(headers={})
    requests.RequestException = Exception
    sys.modules.setdefault("requests", requests)
try:
    import patchright.async_api
except ImportError:
    sys.modules.setdefault("patchright", types.ModuleType("patchright"))
    module = types.ModuleType("patchright.async_api")
    module.async_playwright = lambda: None
    sys.modules.setdefault("patchright.async_api", module)

from collector.research_bridge import ResearchBridge, SCHEMA, authenticate, verify_attempt
from collector.runner import Runner, LowesAccessError
from collector.store import Store

URL = "https://www.lowes.com/pd/Fixture-Mower/5012345678"


class ResearchBridgeTests(unittest.TestCase):
    def test_metadata_catalog_defers_physical_publication_checks_to_open(self):
        receipt = self.bridge.ensure(self.payload())
        self.publish(receipt['job_id'])
        initial = self.bridge.tasks(verify=False)['items'][0]['data']
        path = self.store.data_dir / initial['selected_run']['run_dir'] / 'validation_report.json'
        report = json.loads(path.read_text())
        report['status'] = 'INCOMPLETE_SOURCE_LIMIT'
        path.write_text(json.dumps(report))
        with patch('collector.research_bridge._open_artifacts', side_effect=AssertionError('directory must not read physical files')):
            data = self.bridge.tasks(verify=False)['items'][0]['data']
        self.assertFalse(data['ready'])
        self.assertEqual(data['verification'], 'deferred')
        self.assertFalse(self.bridge.tasks()['items'][0]['data']['ready'])

    def test_category_job_filters_are_applied_before_attempts_and_metadata_avoids_result_json(self):
        first = self.bridge.ensure(self.payload())
        second = self.bridge.ensure(self.payload('request_two', product_url=URL.replace('5012345678', '5012345679')))
        self.publish(first['job_id']); self.publish(second['job_id'])
        with self.store._connect() as db:
            db.execute('UPDATE jobs SET category=?,result_json=? WHERE id=?', ('Misting Fan', 'not parsed in directory', first['job_id']))
            db.execute('UPDATE jobs SET category=? WHERE id=?', ('Fan', second['job_id']))
            db.execute('UPDATE job_attempts SET result_json=? WHERE job_id=?', ('not parsed in directory', first['job_id']))
        with patch.object(self.store, 'research_attempt', wraps=self.store.research_attempt) as attempts, \
             patch('collector.research_bridge._open_artifacts', side_effect=AssertionError('metadata only')):
            page = self.bridge.tasks(filters={'categories': ['misting fan'], 'job_ids': [first['job_id'], second['job_id']]}, verify=False)
        self.assertEqual([item['job_id'] for item in page['items']], [first['job_id']])
        self.assertEqual(attempts.call_count, 1)
        self.assertEqual(page['items'][0]['data']['verification'], 'deferred')
        self.assertNotIn('result_json', page['items'][0]['data']['selected_run']['job'])

    def test_metadata_catalog_defers_heavy_checks_but_open_verification_still_rejects_corruption(self):
        result = self.bridge.ensure(self.payload())
        self.publish(result['job_id'])
        with patch('collector.research_bridge.csv.DictReader', side_effect=AssertionError('must not scan CSV')):
            page = self.bridge.tasks(verify=False)
        data = page['items'][0]['data']
        self.assertNotIn('result', data['selected_run']['job'])
        self.assertNotIn('result_json', data['selected_run']['job'])
        self.assertFalse(data['ready'])
        self.assertEqual(data['verification'], 'deferred')
        directory = self.store.data_dir / data['selected_run']['run_dir']
        (directory / 'reviews.csv').write_text('invalid-header\nbad-row\n')
        checked = self.bridge.tasks()
        self.assertFalse(checked['items'][0]['data']['ready'])
        self.assertEqual(checked['items'][0]['data']['diagnostic'],
                         {'phase': 'normalized_reviews', 'error_type': 'ValueError'})

    def test_filtered_catalog_verifies_only_matching_jobs_and_binds_cursor_scope(self):
        first = self.bridge.ensure(self.payload())
        second = self.bridge.ensure(self.payload('request_two', product_url=URL.replace('5012345678', '5012345679')))
        self.publish(first['job_id']); self.publish(second['job_id'])
        with self.store._connect() as db:
            db.execute('update jobs set brand=?,model=? where id=?', ('EGO','LB8803-2',first['job_id']))
            db.execute('update jobs set brand=?,model=? where id=?', ('EGO','LB8800',second['job_id']))
        with patch('collector.research_bridge.verify_attempt', wraps=verify_attempt) as verify:
            page = self.bridge.tasks(filters={'brands':['EGO'],'models':['LB8803-2']})
        self.assertEqual([r['job_id'] for r in page['items']], [first['job_id']])
        self.assertEqual(verify.call_count, 1)
        page = self.store.list_jobs_page(limit=1, filters={'brands':['EGO']})
        with self.assertRaises(ValueError):
            self.store.list_jobs_page(limit=1, cursor=page['next_cursor'], filters={'models':['LB8803-2']})

    def test_verified_attempt_cache_cannot_bypass_changed_product_identity(self):
        receipt = self.bridge.ensure(self.payload())
        self.publish(receipt['job_id'])
        self.assertTrue(self.bridge.tasks()['items'][0]['data']['ready'])
        with self.store._connect() as db:
            db.execute('UPDATE jobs SET source_product_id=? WHERE id=?', ('different-product', receipt['job_id']))
        value = self.bridge.tasks()['items'][0]['data']
        self.assertFalse(value['ready'])
        self.assertEqual(value['diagnostic']['phase'], 'validation_manifest')

    def test_discovery_accepts_only_bounded_product_identity_and_fixed_channels(self):
        self.bridge.product_discovery = Mock(return_value={'items': []})
        result = self.bridge.discover({'brand': 'EGO', 'model': 'LB8803-2'})
        self.assertEqual(result['schema_version'], SCHEMA)
        self.bridge.product_discovery.assert_called_once_with('EGO', 'LB8803-2', ['lowes', 'ace', 'ego'])
        self.bridge.product_discovery.reset_mock()
        for fields in ({'query': 'unrestricted'}, {'model': 'LB8803-2 site:internal'}, {'channel': 'internal'}):
            with self.assertRaises(HTTPException):
                self.bridge.discover({'brand': 'EGO', 'model': 'LB8803-2', **fields})
        self.bridge.product_discovery.assert_not_called()

    def test_exact_job_catalog_does_not_enumerate_other_jobs(self):
        receipt = self.bridge.ensure(self.payload())
        self.publish(receipt['job_id'])
        with patch.object(self.store, 'list_jobs_page', side_effect=AssertionError('unrelated catalog scan')):
            page = self.bridge.tasks(job_id=receipt['job_id'])
        self.assertEqual([item['job_id'] for item in page['items']], [receipt['job_id']])
        self.assertIsNone(page['next_cursor'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"DATA_DIR": self.temp.name, "VOXQUARRY_MASTER_DATA_URL": "", "MASTER_DATA_TASK_BINDINGS_REQUIRED": "false"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Store()
        self.bridge = self.make_bridge(self.store)

    def make_bridge(self, store):
        def create(req, research_operation=None):
            value = store.create_job(uuid.uuid4().hex[:12], "scrape", req.channel,
                                     req.product_url, research_operation=research_operation)
            if value.get("research_retry_job_id"):
                value = store.queue_retry(value["research_retry_job_id"], research_operation=research_operation)
            return value
        return ResearchBridge(lambda: store, create, lambda url: {"channel": "lowes"},
                              lambda **kw: types.SimpleNamespace(**kw), {"lowes", "ace", "amazon_reviews_2023"})

    def payload(self, key="request_one", **kw):
        return {"schema_version": SCHEMA, "request_key": key, "research_run_id": "research_one",
                "channel": "lowes", "product_url": URL, **kw}

    def publish(self, job_id, status="COMPLETE"):
        root = Path(self.temp.name) / "runs" / job_id / "lowes"
        root.mkdir(parents=True, exist_ok=True)
        report = {"channel": "lowes", "status": status, "unique_reviews": 1,
                  "source_unique_reviews": 2, "source_total": 2,
                  "product": {"id": "5012345678", "url": URL}}
        (root / "validation_report.json").write_text(json.dumps(report))
        (root / "reviews.csv").write_text("review_id,review_text,rating\nr1,Full fixture review,5\n")
        (root / "reviews.raw.jsonl").write_text('{"id":"r1"}\n{"id":"excluded"}\n')
        if status == "PARTIAL_SUCCESS":
            self.store.finish_partial(job_id, {"lowes": report}, "fixture partial")
        else:
            self.store.finish(job_id, {"lowes": report})
        return root

    def test_concurrent_idempotency_across_store_instances(self):
        stores = [Store(initialize=False) for _ in range(8)]
        bridges = [self.make_bridge(s) for s in stores]
        with ThreadPoolExecutor(max_workers=8) as executor:
            receipts = list(executor.map(lambda b: b.ensure(self.payload()), bridges))
        self.assertEqual(len({r["job_id"] for r in receipts}), 1)
        self.assertEqual({r["attempt_no"] for r in receipts}, {1})
        self.assertEqual(len(self.store.list_all_jobs()), 1)
        self.assertEqual(len(self.store.list_attempts(receipts[0]["job_id"])), 1)
        self.assertFalse(receipts[0]["attempt_policy"]["firecrawl_allowed"])

    def test_concurrent_different_requests_share_target(self):
        bridges = [self.make_bridge(Store(initialize=False)) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda item: item[1].ensure(self.payload("request_" + str(item[0]))), enumerate(bridges)))
        self.assertEqual(len({r["job_id"] for r in results}), 1)
        self.assertEqual({r["attempt_no"] for r in results}, {1})
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM research_collection_operations").fetchone()[0], 8)

    def test_request_and_attempt_insert_roll_back_together(self):
        with patch.object(self.store, "_research_bind", side_effect=RuntimeError("injected before receipt")):
            with self.assertRaises(RuntimeError):
                self.bridge.ensure(self.payload())
        self.assertEqual(self.store.list_all_jobs(), [])
        self.assertIsNone(self.store.research_operation("request_one", "research_one"))

    def test_payload_collision_does_not_create_another_task(self):
        original = self.bridge.ensure(self.payload())
        with self.assertRaises(HTTPException) as raised:
            self.bridge.ensure(self.payload(product_url=URL + "different"))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.bridge.ensure(self.payload())["job_id"], original["job_id"])

    def test_detach_preserves_shared_job_and_never_resubscribes_replay(self):
        first = self.bridge.ensure(self.payload())
        second = self.bridge.ensure(self.payload("second", research_run_id="other"))
        self.store.detach_research_subscription("request_one", "research_one")
        replay = self.bridge.ensure(self.payload())
        self.assertEqual(replay["subscribed"], 0)
        self.assertEqual(self.bridge.status("second", "other")["subscribed"], 1)
        self.assertEqual(self.store.get_job(first["job_id"])["status"], "queued")
        self.assertEqual(first["job_id"], second["job_id"])

    def test_retry_is_atomic_and_requires_exact_previous_attempt(self):
        first = self.bridge.ensure(self.payload())
        self.store.fail(first["job_id"], "fixture error")
        bad = self.payload("badretry", retry=True, expected_attempt_no=7)
        with self.assertRaises(HTTPException):
            self.bridge.ensure(bad)
        payload = self.payload("retry", retry=True, expected_attempt_no=1)
        bridges = [self.make_bridge(Store(initialize=False)) for _ in range(6)]
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda b: b.ensure(payload), bridges))
        self.assertEqual({r["attempt_no"] for r in results}, {2})
        self.assertEqual(len(self.store.list_attempts(first["job_id"])), 2)
        self.assertFalse(self.store.research_attempt_policy(first["job_id"], 2)["firecrawl_allowed"])

    def test_retry_receipt_failure_rolls_back_new_attempt(self):
        first = self.bridge.ensure(self.payload())
        self.store.fail(first["job_id"], "fixture error")
        with patch.object(self.store, "_research_bind", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.bridge.ensure(self.payload("retry", retry=True, expected_attempt_no=1))
        self.assertEqual(len(self.store.list_attempts(first["job_id"])), 1)
        self.assertEqual(self.store.get_job(first["job_id"])["status"], "failed")

    def test_retry_cannot_create_missing_target_or_adopt_unrelated_attempt(self):
        with self.assertRaises(HTTPException):
            self.bridge.ensure(self.payload("missing", retry=True, expected_attempt_no=1))
        self.assertEqual(self.store.list_all_jobs(), [])
        first = self.bridge.ensure(self.payload())
        with self.assertRaises(HTTPException):
            self.bridge.ensure(self.payload("active", retry=True, expected_attempt_no=1))
        self.store.fail(first["job_id"], "fixture failure")
        self.bridge.ensure(self.payload("retry", retry=True, expected_attempt_no=1))
        shared = self.bridge.ensure(self.payload("sharedretry", retry=True, expected_attempt_no=1))
        self.assertEqual(shared["attempt_no"], 2)
        with self.assertRaises(HTTPException):
            self.bridge.ensure(self.payload("stale", retry=True, expected_attempt_no=10))

    def test_missing_legacy_attempt_fails_closed(self):
        self.store.create_job("legacy", "scrape", "lowes", URL)
        with self.store._connect() as conn:
            conn.execute("DELETE FROM job_attempts WHERE job_id='legacy'")
        with self.assertRaises(HTTPException):
            self.bridge.ensure(self.payload())
        self.assertFalse(self.bridge.tasks()["items"][0]["data"]["ready"])

    def test_raw_count_is_not_valid_review_count_and_bytes_are_hashed(self):
        result = self.bridge.ensure(self.payload())
        self.publish(result["job_id"])
        data = self.bridge.status("request_one", "research_one")["data"]
        self.assertTrue(data["ready"])
        self.assertEqual((data["valid_reviews"], data["raw_records"]), (1, 2))
        self.assertEqual(data["selected_run"]["job"]["active_attempt_no"], 1)
        self.assertTrue(all(a["sha256"].startswith("sha256:") for a in data["artifacts"]))
        self.assertEqual(self.bridge.tasks()["items"][0]["data"], data)

    def test_new_failed_attempt_does_not_return_old_completed(self):
        first = self.bridge.ensure(self.payload())
        self.publish(first["job_id"])
        self.store.queue_retry(first["job_id"])  # Existing non-Wenquan producer.
        current = self.bridge.ensure(self.payload("second"))
        self.assertEqual(current["attempt_no"], 2)
        self.store.fail(first["job_id"], "new attempt failed")
        current = self.bridge.status("second", "research_one")
        self.assertEqual(current["status"], "failed")
        self.assertFalse(current["data"]["ready"])
        original = self.bridge.status("request_one", "research_one")
        self.assertTrue(original["data"]["ready"])
        self.assertIn("/attempts/1/", original["data"]["selected_run"]["run_dir"])
        self.assertEqual(self.bridge.tasks()["items"][0]["latest_attempt_status"], "failed")

    def test_partial_completion_keeps_partial_label(self):
        result = self.bridge.ensure(self.payload())
        self.publish(result["job_id"], "PARTIAL_SUCCESS")
        data = self.bridge.status("request_one", "research_one")["data"]
        self.assertTrue(data["ready"])
        self.assertEqual(data["completeness"], "partial")

    def test_artifact_changes_invalidate_cache_and_symlinks_fail(self):
        result = self.bridge.ensure(self.payload())
        root = self.publish(result["job_id"])
        self.assertTrue(self.bridge.status("request_one", "research_one")["data"]["ready"])
        (root / "reviews.csv").write_text("review_id,review_text\n")
        self.assertFalse(self.bridge.status("request_one", "research_one")["data"]["ready"])

    def test_declared_unique_count_rejects_duplicate_csv_ids(self):
        result = self.bridge.ensure(self.payload())
        root = self.publish(result["job_id"])
        report = json.loads((root / "validation_report.json").read_text())
        report["unique_reviews"] = 2
        (root / "validation_report.json").write_text(json.dumps(report))
        (root / "reviews.csv").write_text("review_id,review_text\nr1,First text\nr1,Duplicate ID\n")
        self.store.finish(result["job_id"], {"lowes": report})
        self.assertFalse(self.bridge.status("request_one", "research_one")["data"]["ready"])
        self.publish(result["job_id"])
        external = Path(self.temp.name) / "elsewhere.csv"
        (root / "reviews.csv").rename(external)
        (root / "reviews.csv").symlink_to(external)
        self.assertFalse(self.bridge.status("request_one", "research_one")["data"]["ready"])

    def test_new_attempt_forbids_firecrawl_even_if_preferred(self):
        receipt = self.bridge.ensure(self.payload())
        runner = Runner.__new__(Runner)
        runner.store = self.store
        runner.lowes_direct_api_page = Mock(side_effect=RuntimeError("direct blocked"))
        runner.firecrawl_lowes_page = Mock(side_effect=AssertionError("must not call Firecrawl"))
        with self.assertRaises(LowesAccessError):
            asyncio.run(runner.lowes_api_page("5012345678", 0, URL, preferred_method="firecrawl_enhanced_proxy", job_id=receipt["job_id"]))
        runner.lowes_direct_api_page.assert_called_once()
        runner.firecrawl_lowes_page.assert_not_called()

    def test_existing_task_policy_is_unchanged(self):
        self.store.create_job("legacy", "scrape", "lowes", URL)
        receipt = self.bridge.ensure(self.payload())
        self.assertIsNone(receipt["attempt_policy"])
        runner = Runner.__new__(Runner)
        runner.store = self.store
        self.assertTrue(runner.research_firecrawl_allowed("legacy"))

    def test_auth_fail_closed_and_input_rejects_paths_actions_and_source_id(self):
        request = types.SimpleNamespace(headers={"authorization": "Bearer test-only-credential"})
        with patch.dict(os.environ, {"VOXQUARRY_RESEARCH_BRIDGE_TOKEN": ""}):
            with self.assertRaises(HTTPException) as raised:
                authenticate(request)
            self.assertEqual(raised.exception.status_code, 503)
        with patch.dict(os.environ, {"VOXQUARRY_RESEARCH_BRIDGE_TOKEN": "test-only-credential"}):
            authenticate(request)
            with self.assertRaises(HTTPException):
                authenticate(types.SimpleNamespace(headers={}))
        for extra in ({"run_dir": "/etc"}, {"source_product_id": "different"}, {"action": "delete"}):
            with self.assertRaises(HTTPException):
                self.bridge.ensure(self.payload(**extra))
        self.assertEqual(self.store.list_all_jobs(), [])


if __name__ == "__main__":
    unittest.main()
