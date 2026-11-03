"""Browser paging must retain the full filter scope without bulk payloads."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from collector.store import Store
from collector.task_view import TaskView, compact


class TaskViewTests(unittest.TestCase):
    def test_compact_projection_preserves_legacy_energy_display(self):
        self.assertEqual(compact({'channel':'ego', 'product_observation':{'energy_type':'battery', 'large':'unused'}})['energy_type'], 'battery')
        self.assertEqual(compact({'channel':'ego', 'product_master_record':{'energy_type':'corded'}})['energy_type'], 'corded')
        self.assertEqual(compact({'channel':'ego', 'energy_type':'fuel', 'product_observation':{'energy_type':'battery'}})['energy_type'], 'fuel')
        self.assertNotIn('product_observation', compact({'channel':'ego','product_observation':{'energy_type':'battery'}}))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'DATA_DIR': self.temp.name, 'MASTER_DATA_TASK_BINDINGS_REQUIRED': 'false'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.store = Store()
        with self.store._connect() as db:
            db.executemany('INSERT INTO jobs(id,action,channel,status,created_at,target_url,brand,model,category,market_region,result_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                [(f'job-{i:05}', 'collect', 'ego' if i % 2 else 'lowes', 'completed', '2026-09-18T00:00:00Z',
                  f'https://fixture.invalid/{i}', 'EGO' if i % 2 else 'Fixture', 'LM-' + str(i % 5), 'Mower', 'North America',
                  json.dumps({'ego' if i % 2 else 'lowes': {'unique_reviews': 7, 'source_total': 8, 'unneeded': 'large' * 1000}})) for i in range(4370)])
        self.view = TaskView(self.store)

    def test_full_scope_filters_and_page_transport_are_bounded(self):
        first = self.view.page()
        self.assertEqual((first['total_count'], first['page_count'], len(first['items'])), (4370, 146, 30))
        self.assertLess(len(json.dumps(first).encode()), 80000)
        self.assertNotIn('unneeded', json.dumps(first))
        self.assertEqual(self.view.queue()['jobs'], [], 'history must not travel through queue endpoint')
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET status='running',target_url=NULL,result_json=NULL WHERE id='job-00001'")
        self.assertEqual([j['id'] for j in self.view.queue()['jobs']], ['job-00001'])
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET status='completed',target_url='https://fixture.invalid/1' WHERE id='job-00001'")
        selected = self.view.page(filters={'brand': 'EGO', 'channel': 'EGO 官网'})
        self.assertEqual(selected['total_count'], 2185)
        self.assertEqual(set(selected['facets']['model']), {'LM-' + str(i) for i in range(5)})
        self.assertEqual(len(self.view.page(page=146)['items']), 20)
        self.assertEqual(len({r['id'] for r in first['items']} & {r['id'] for r in self.view.page(page=2)['items']}), 0)
        with patch.object(self.store, 'summarize_job_scope', return_value={'summary_status':'available'}) as summarize:
            self.view.summary({'brand':'EGO'})
        self.assertEqual(len(summarize.call_args.args[0]), 2185)

    def test_scope_changes_invalidate_cache_and_invalid_filters_fail(self):
        first = self.view.page()
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET brand='Changed' WHERE id='job-04369'")
        changed = self.view.page(filters={'brand':'Changed'})
        self.assertNotEqual(first['scope_token'], changed['scope_token'])
        self.assertEqual(changed['total_count'], 1)
        for filters in ([], {'invalid':'x'}, {'brand':1}):
            with self.assertRaises(ValueError): self.view.page(filters=filters)
        for limit in (0,101,True):
            with self.assertRaises(ValueError): self.view.page(limit=limit)
