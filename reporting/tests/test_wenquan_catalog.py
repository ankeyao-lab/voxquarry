"""Bounded catalog pages and version-bound compatible batches."""
import json
import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import patch

from reporting.tests import test_wenquan_data_routes as fixtures
from reporting.tests.test_market_region import _row
from reporting.wenquan_datasets import DatasetError
from reporting.wenquan_catalog import Catalogs


class CatalogTests(unittest.TestCase):
    setUp = fixtures.SharedResearchDataTests.setUp
    add_live = fixtures.SharedResearchDataTests.add_live
    add_snapshot = fixtures.SharedResearchDataTests.add_snapshot

    def test_prepare_continues_batches_preserves_gaps_and_reuses_frozen_successes(self):
        self.snapshot.add_run('legacy', '', [_row('old', '')], legacy=True)
        self.add_snapshot('modern'); self.add_snapshot('missing')
        (self.snapshot.root / 'data/runs/missing/ego/validation_report.json').unlink()
        first = self.backend.prepare(max_batches=1)
        self.assertEqual(first['coverage']['prepared_batches'], 1)
        self.assertFalse(first['coverage']['complete'])
        self.assertEqual(first['next'], {'catalog_id': first['catalog_id']})
        second = self.backend.prepare(**first['next'])
        self.assertEqual(second['coverage'], {'requested_sources': 3, 'available_sources': 2,
            'prepared_sources': 2, 'unavailable_count': 1, 'total_batches': 2,
            'prepared_batches': 2, 'complete': False})
        self.assertEqual(second['pending_batch_ids'], [])
        self.assertIsNone(second['next'])
        self.assertEqual({d['batch_id'] for d in second['datasets']},
                         {d['batch_id'] for d in first['datasets']} | set(first['pending_batch_ids']))
        with patch.object(self.backend, 'open', side_effect=AssertionError('already frozen')):
            reused = self.backend.prepare(catalog_id=first['catalog_id'])
        self.assertEqual(reused['preparation']['attempted_batches'], 0)
        self.assertEqual(reused['datasets'], second['datasets'])
        restored = type(self.backend)(self.service, live_root=self.live.root, live_catalog=lambda cursor: self.page)
        with patch.object(restored, 'open', side_effect=AssertionError('durable receipts must survive restart')):
            self.assertEqual(restored.prepare(catalog_id=first['catalog_id'])['datasets'], second['datasets'])
        with self.assertRaises(DatasetError):
            self.backend.prepare({'brands': ['OTHER']}, catalog_id=first['catalog_id'])

    def test_prepare_failure_is_a_durable_gap_and_not_automatically_retried(self):
        self.add_snapshot('one')
        with patch.object(self.backend, 'open', side_effect=DatasetError('invalid_source', 'private diagnostic', reason='review_text_flag_mismatch')) as opening:
            first = self.backend.prepare()
        self.assertEqual(opening.call_count, 1)
        self.assertFalse(first['coverage']['complete'])
        self.assertEqual(first['pending_batch_ids'], [])
        self.assertIsNone(first['next'])
        self.assertNotIn('private diagnostic', json.dumps(first))
        with patch.object(self.backend, 'open', side_effect=AssertionError('failure must not loop')):
            resumed = self.backend.prepare(catalog_id=first['catalog_id'])
        self.assertEqual(resumed['failed_batches'], first['failed_batches'])
        # An explicit exact-batch open after repair clears the failure receipt.
        self.backend.open(catalog_id=first['catalog_id'], batch_id=first['failed_batches'][0]['batch_id'])
        fixed = self.backend.prepare(catalog_id=first['catalog_id'])
        self.assertTrue(fixed['coverage']['complete'])
        self.assertEqual(fixed['failed_batches'], [])

    def test_open_reuses_materialized_batch_only_after_current_version_check(self):
        self.add_snapshot('one')
        first = self.backend.open()
        binding = first['catalog_binding']
        with patch.object(self.backend.snapshot, '_freeze', side_effect=AssertionError('must reuse frozen output')):
            second = self.backend.open(catalog_id=binding['catalog_id'], batch_id=binding['batch_id'])
        self.assertTrue(second['preparation_reused'])
        self.assertEqual(first['dataset_id'], second['dataset_id'])
        path = self.snapshot.root / 'data/runs/one/ego/reviews.csv'
        path.write_text(path.read_text() + '\n')
        with self.assertRaises(DatasetError) as caught:
            self.backend.open(catalog_id=binding['catalog_id'], batch_id=binding['batch_id'])
        self.assertEqual(caught.exception.code, 'source_changed')
        # Explicit continuation consumes the original immutable prepared data.
        continued = self.backend.prepare(catalog_id=binding['catalog_id'])
        self.assertEqual(continued['datasets'][0]['dataset_id'], first['dataset_id'])

    def test_prepare_validates_batch_limit_without_scanning(self):
        for value in (0, 9, True, '4'):
            with patch.object(self.backend, '_catalog', side_effect=AssertionError('invalid request')):
                with self.assertRaises(DatasetError):
                    self.backend.prepare(max_batches=value)

    def test_prepare_time_slice_keeps_batch_pending_without_failed_receipt(self):
        self.add_snapshot('one')
        with patch.object(self.backend, 'open', side_effect=DatasetError('preparation_yield', 'slice finished')):
            first = self.backend.prepare()
        self.assertEqual(first['failed_batches'], [])
        self.assertEqual(len(first['pending_batch_ids']), 1)
        self.assertFalse(first['coverage']['complete'])
        resumed = self.backend.prepare(**first['next'])
        self.assertTrue(resumed['coverage']['complete'])

    def test_prepared_receipt_does_not_cross_normalization_contract_changes(self):
        from reporting import source
        self.add_snapshot('one')
        first = self.backend.open()
        binding = first['catalog_binding']
        with patch.object(source, 'PARSER_VERSION', 'fixture-parser/new'):
            updated = self.backend.open(catalog_id=binding['catalog_id'], batch_id=binding['batch_id'])
        self.assertNotEqual(first['dataset_id'], updated['dataset_id'])
        with self.assertRaises(DatasetError) as caught:
            self.backend.invoke('aggregate', {'dataset_ids': [first['dataset_id'], updated['dataset_id']]})
        self.assertEqual(caught.exception.diagnostic['reason'], 'mixed_normalization_versions')
        self.assertEqual(self.backend.snapshot.query(first['dataset_id'])['total'], 1)

    def test_large_prepared_directory_receipt_does_not_repeat_scope_per_dataset(self):
        count = 9730
        jobs = ['job_%05d' % n for n in range(count)]
        def rows():
            for job in jobs:
                yield ({'job_id': job, 'available': True, 'compatibility': 'current', 'source_version': job},
                       {'metadata_bytes': 12000, 'source_bytes': 10000}, None, None)
        catalog_id = self.backend.catalogs.build({'job_ids': jobs}, [('wotu', rows())])
        def prepared(catalog, batch):
            info, selected = self.backend.catalogs.batch(catalog, batch)
            return {'dataset_id': 'ds_' + '0' * 64, 'manifest_sha256': 'sha256:' + '0' * 64,
                'version': 'wenquan-dataset/1.2.0', 'normalization_version': 'review-normalization/2.0.0',
                'record_counts': {'review_entities': 10}, 'source_complete': True, 'export_complete': True,
                'catalog_binding': {**info, 'batch_id': batch, 'job_ids': [row[1]['job_id'] for row in selected]},
                'source_coverage': {'complete': True, 'unavailable_sources': []}}
        with patch.object(self.backend, '_prepared', side_effect=prepared):
            result = self.backend.prepare(catalog_id=catalog_id)
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(result['coverage']['prepared_sources'], count)
        self.assertEqual(len(result['datasets']), 195)
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), 800_000)
        self.assertTrue(all('scope' not in item and 'catalog_binding' not in item for item in result['datasets']))

    def pages(self, **kwargs):
        result = self.backend.catalog(**kwargs)
        yield result
        while result.get('next_cursor'):
            result = self.backend.catalog(cursor=result['next_cursor'], **kwargs)
            yield result

    def test_pages_cover_union_once_and_live_version_wins(self):
        self.add_snapshot('same'); self.add_live('same')
        self.add_snapshot('historical'); self.add_live('current')
        pages = list(self.pages(limit=1))
        items = [i for p in pages for i in p['items']]
        self.assertEqual({i['job_id'] for i in items}, {'same', 'historical', 'current'})
        self.assertEqual(len(items), 3)
        self.assertEqual(next(i for i in items if i['job_id'] == 'same')['origin'], 'wotu')
        self.assertTrue(pages[-1]['page']['enumeration_complete'])
        self.assertTrue(all(p['catalog_id'] == pages[0]['catalog_id'] for p in pages))
        self.assertTrue(all(len(json.dumps(p).encode()) < 800_000 for p in pages))
        self.assertEqual(self.backend.catalog(limit=1, cursor=pages[0]['next_cursor']), pages[1])
        with self.assertRaises(DatasetError):
            self.backend.catalog({'brands': ['OTHER']}, cursor=pages[0]['next_cursor'])

    def test_catalog_does_not_sum_heavy_task_results_or_open_unrelated_exact_sources(self):
        for n in range(6):
            _, item = self.add_live('live' + str(n))
            item['data']['selected_run']['job'].update(result={'large': 'x' * 10_000}, result_json='x' * 10_000)
        self.backend.live.limits = replace(self.backend.live.limits, max_metadata_bytes=4096)
        result = list(self.pages(limit=2))
        self.assertEqual(sum(len(p['items']) for p in result), 6)
        self.add_snapshot('missing')
        (self.snapshot.root / 'data/runs/missing/ego/validation_report.json').unlink()
        result = self.backend.catalog({'job_ids': ['live0']})
        self.assertEqual([i['job_id'] for i in result['items']], ['live0'])
        self.assertTrue(result['catalog_complete'])

    def test_mixed_contracts_get_batches_and_scope_is_preserved(self):
        self.snapshot.add_run('legacy', '', [_row('old', '')], legacy=True)
        self.add_snapshot('modern')
        result = self.backend.open()
        self.assertEqual(result['status'], 'requires_batches')
        self.assertEqual({b['compatibility'] for b in result['batches']}, {'legacy', 'current'})
        opened = []
        for batch in result['batches']:
            manifest = self.backend.open(catalog_id=result['catalog_id'], batch_id=batch['batch_id'])
            opened.extend(i['job']['id'] for i in manifest['inputs'])
            self.assertEqual(manifest['catalog_binding']['scope'], {})
            self.assertEqual(manifest['catalog_binding']['catalog_id'], result['catalog_id'])
        self.assertEqual(set(opened), {'legacy', 'modern'})

    def test_changed_planned_source_is_rejected_before_freezing(self):
        self.add_snapshot('one')
        result = self.backend.catalog()
        p = self.snapshot.root / 'data/runs/one/ego/validation_report.json'
        value = json.loads(p.read_text()); value['notes'] = {'changed': True}; p.write_text(json.dumps(value))
        with patch.object(self.backend.snapshot, 'open', side_effect=AssertionError('must check version first')):
            with self.assertRaises(DatasetError) as caught:
                self.backend.open(catalog_id=result['catalog_id'], batch_id=result['batches'][0]['batch_id'])
        self.assertEqual(caught.exception.code, 'source_changed')

    def test_missing_sources_are_paginated_and_never_claim_complete(self):
        for job in ('good', 'missing'):
            self.add_snapshot(job)
        (self.snapshot.root / 'data/runs/missing/ego/validation_report.json').unlink()
        pages = list(self.pages(limit=1))
        self.assertFalse(any(p['catalog_complete'] for p in pages))
        self.assertEqual(sum(len(p['unavailable_sources']) for p in pages), 1)
        self.assertTrue(pages[-1]['page']['enumeration_complete'])

    def test_unsafe_source_does_not_turn_into_missing_row(self):
        self.add_snapshot('one')
        p = self.snapshot.root / 'data/runs/one/ego/validation_report.json'
        other = p.with_name('other.json'); p.rename(other); p.symlink_to(other)
        with self.assertRaises(DatasetError) as caught:
            self.backend.catalog()
        self.assertIn(caught.exception.code, {'unsafe_source', 'unsafe_path'})

    def test_double_observed_directory_streams_bounded_pages_with_no_omissions(self):
        # 2 x the observed 4,865 catalog jobs; no production data in fixtures.
        count = 9730
        def rows():
            for n in range(count):
                job = 'job_%05d' % n
                yield ({'job_id': job, 'available': True, 'compatibility': 'current',
                        'source_version': 'v' + str(n), 'model': 'synthetic-' + 'x' * 160},
                       {'metadata_bytes': 12000, 'source_bytes': 10000}, None, None)
        catalog = self.backend.catalogs
        catalog_id = catalog.build({}, [('wotu', rows())])
        seen, batches, sizes = set(), set(), []
        page = catalog.page(catalog_id, limit=100)
        while True:
            ids = {r['job_id'] for r in page['items']}
            self.assertFalse(seen & ids)
            seen.update(ids); batches.update(b['batch_id'] for b in page['batches'])
            sizes.append(len(json.dumps(page, ensure_ascii=False).encode()))
            if not page['next_cursor']:
                break
            page = self.backend.catalog(cursor=page['next_cursor'], limit=100)
        self.assertEqual(len(seen), count)
        self.assertEqual(len(batches), 195)
        self.assertLess(max(sizes), 700000)
        self.assertTrue(page['page']['enumeration_complete'])

    def test_cursor_rejects_bad_shape_and_cannot_follow_changed_source(self):
        for cursor in ([], {}, 1, '', 'not-a-cursor', 'e30=', 'W10='):
            with self.assertRaises(DatasetError):
                self.backend.catalog(cursor=cursor)
        self.add_live('first'); self.add_live('second')
        page = self.backend.catalog(limit=1)
        self.add_live('third')
        next_page = self.backend.catalog(cursor=page['next_cursor'], limit=1)
        self.assertEqual(next_page['total_sources'], 2)
        self.assertEqual([i['job_id'] for i in next_page['items']], ['second'])

    def test_cross_batch_count_deduplicates_entities_and_overlapping_exports(self):
        self.add_snapshot('a'); self.add_snapshot('b')  # same review entity
        a = self.backend.open({'job_ids': ['a']})
        b = self.backend.open({'job_ids': ['b']})
        both = self.backend.open({'job_ids': ['a', 'b']})
        ids = [a['dataset_id'], b['dataset_id'], both['dataset_id']]
        result = self.backend.invoke('aggregate', {'dataset_ids': ids})
        self.assertEqual(result['total'], 1)
        self.assertEqual(len(result['sources']), 3)
        occurrences = self.backend.invoke('aggregate', {'dataset_ids': ids, 'population': 'occurrences'})
        self.assertEqual(occurrences['total'], 2)
        self.assertEqual(result, self.backend.invoke('aggregate', {'dataset_ids': list(reversed(ids))}))
        with self.assertRaises(DatasetError):
            self.backend.invoke('aggregate', {'dataset_id': ids[0], 'dataset_ids': ids})

    def test_catalog_storage_budget_fails_without_partial_published_directory(self):
        self.backend.snapshot.limits = replace(self.backend.snapshot.limits, max_catalog_bytes=4096)
        with self.assertRaises(DatasetError) as caught:
            self.backend.catalog()
        self.assertEqual(caught.exception.code, 'catalog_storage_unavailable')
        self.assertEqual(caught.exception.diagnostic,
                         {'reason': 'catalog_sqlite_error', 'phase': 'catalog', 'sqlite_code': 13})
        self.assertEqual(list(self.backend.snapshot.storage_root.glob('cat_*.json')), [])

    def test_unavailable_task_outside_category_scope_is_not_a_coverage_gap(self):
        self.add_live('inside')
        self.page['items'].append({'job_id': 'outside', 'status': 'completed', 'channel': 'ego',
            'scope_job': {'id': 'outside', 'channel': 'ego', 'category': 'Pressure Washer'},
            'data': {'ready': False, 'reason': 'artifact_unavailable'}})
        result = self.backend.catalog({'categories': ['Leaf Blower']})
        self.assertEqual(result['total'], 1)
        self.assertTrue(result['catalog_complete'])

    def test_old_client_cannot_mistake_first_page_or_batch_plan_for_full_result(self):
        self.add_snapshot('first'); self.add_snapshot('second')
        with self.assertRaises(DatasetError) as caught:
            self.backend.invoke('catalog', {'limit': 1})
        self.assertEqual(caught.exception.code, 'catalog_client_upgrade_required')
        page = self.backend.invoke('catalog', {'limit': 1, 'catalog_contract': 'voxquarry.wenquan-catalog/v2'})
        self.assertIsNotNone(page['next_cursor'])
        self.snapshot.add_run('legacy', '', [_row('old', '')], legacy=True)
        with self.assertRaises(DatasetError) as caught:
            self.backend.invoke('open', {})
        self.assertEqual(caught.exception.code, 'catalog_client_upgrade_required')


if __name__ == '__main__':
    unittest.main()
