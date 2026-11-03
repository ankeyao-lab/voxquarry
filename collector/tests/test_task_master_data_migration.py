import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from collector.master_data import MasterDataStore
from collector.product_identity import build_product_observation, build_task_energy, build_voltage_platform
from collector.store import Store
from collector.task_master_data_migration import make_plan, apply_catalog, apply_jobs, read_jobs, digest, seal_catalog_result


class TaskMasterMigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {'DATA_DIR': str(self.root), 'VOXQUARRY_MASTER_DATA_URL': '', 'MASTER_DATA_TASK_BINDINGS_REQUIRED': 'false'})
        env.start()
        self.addCleanup(env.stop)
        store = Store()
        for index, (brand, voltage, energy) in enumerate([('Fixture', '20V', 'battery'), ('Separate', '20V', 'battery'), ('Fixture', '12V/20V', 'other'), ('Fixture', None, 'fuel')]):
            obs = build_product_observation(brand=brand, voltage=voltage, energy_type=energy, evidence_source='synthetic_fixture')
            platform = build_voltage_platform(obs)
            store.create_job(str(index), 'scrape', 'ego', brand=brand, product_observation=obs, voltage_platform=platform, task_energy=build_task_energy(obs, platform))
        with store._connect() as conn:
            conn.execute("UPDATE jobs SET status='completed'")
        self.catalog = MasterDataStore(self.root, initialize=True)
        for kind, code in [('brands','Fixture'), ('brands','Separate'), ('energy-types','battery'), ('energy-types','fuel'), ('energy-types','other')]:
            self.catalog.create(kind, {'code':code.lower(), 'name':code, 'active':code != 'other'}, actor='test:migration')
        self.path = self.root / 'jobs.sqlite3'
        self.plan = make_plan(self.catalog, self.path)

    def test_atomic_catalog_and_task_replay_with_no_source_change(self):
        before = read_jobs(self.path)
        snapshot = apply_catalog(self.catalog, self.path, self.plan)
        plan = seal_catalog_result(self.plan, snapshot)
        self.assertEqual(len([r for r in snapshot['records'] if r['type'] == 'voltage-platforms']), 3)
        first = apply_jobs(self.path, snapshot, plan)
        self.assertEqual(first['counts'], {'energy':4, 'platform':3})
        self.assertEqual(apply_jobs(self.path, snapshot, plan)['changed'], 0)
        self.assertEqual(read_jobs(self.path), before)
        with patch.dict(os.environ, {'MASTER_DATA_TASK_BINDINGS_REQUIRED': 'true'}):
            Store()
        with self.assertRaisesRegex(ValueError, 'MASTER_DATA_TASK_BINDINGS_REQUIRED'):
            Store()

    def test_bad_catalog_batch_rolls_back_every_record(self):
        bad = copy.deepcopy(self.plan)
        bad['creations'][-1]['payload']['brand_id'] = 'brd_missing'
        bad['plan_hash'] = digest({k:v for k,v in bad.items() if k != 'plan_hash'})
        before = self.catalog.snapshot()
        with self.assertRaises(ValueError):
            apply_catalog(self.catalog, self.path, bad)
        self.assertEqual(self.catalog.snapshot(), before)

    def test_drift_and_tampered_plan_are_zero_write(self):
        bad = copy.deepcopy(self.plan)
        bad['job_count'] = 5
        with self.assertRaisesRegex(ValueError, 'seal'):
            apply_catalog(self.catalog, self.path, bad)
        snapshot = apply_catalog(self.catalog, self.path, self.plan)
        plan = seal_catalog_result(self.plan, snapshot)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE jobs SET model='changed' WHERE id='0'")
        with self.assertRaisesRegex(ValueError, 'drift'):
            apply_jobs(self.path, snapshot, plan)
        with sqlite3.connect(self.path) as conn:
            self.assertNotIn('energy_type_id', [r[1] for r in conn.execute('PRAGMA table_info(jobs)')])

    def test_wrong_catalog_prediction_fails_closed(self):
        snapshot = apply_catalog(self.catalog, self.path, self.plan)
        plan = seal_catalog_result(self.plan, snapshot)
        wrong = copy.deepcopy(snapshot)
        wrong['records'][0]['name'] = 'Changed'
        wrong['catalog_content_hash'] = digest(wrong['records'])
        with self.assertRaisesRegex(ValueError, 'exact'):
            apply_jobs(self.path, wrong, plan)


if __name__ == '__main__':
    unittest.main()
