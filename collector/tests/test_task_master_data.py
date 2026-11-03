"""Synthetic task/catalog binding fixtures; no production data or network."""
import copy
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from collector.master_data import MasterDataStore
from collector.product_identity import build_product_observation, build_task_energy, build_voltage_platform
from collector.store import Store


class TaskMasterDataTests(unittest.TestCase):
    def test_research_upgrade_preserves_protected_legacy_tables(self):
        self.activate()
        with self.store._connect() as conn:
            conn.execute('INSERT INTO task_master_data_binding_state(id,required) VALUES(1,1)')
            conn.execute('DROP TABLE research_collection_operations')
            conn.execute('DROP TABLE research_attempt_policies')
            before = list(conn.iterdump())
        with patch.dict(os.environ, {'MASTER_DATA_TASK_BINDINGS_REQUIRED': 'true'}), patch.object(Store, '_init_db', side_effect=AssertionError('legacy migration forbidden')):
            upgraded = Store()
            self.assertTrue(upgraded.research_schema_ready())
            Store()
        with upgraded._connect() as conn:
            for table in ('research_collection_operations', 'research_attempt_policies'):
                conn.execute('DROP TABLE ' + table)
            self.assertEqual(list(conn.iterdump()), before)
        self.assertFalse(upgraded.research_schema_ready())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'DATA_DIR': str(self.root), 'VOXQUARRY_MASTER_DATA_URL': '', 'MASTER_DATA_TASK_BINDINGS_REQUIRED': 'false'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.catalog = MasterDataStore(self.root, initialize=True)
        self.brand = self.add('brands', code='fixture', name='Fixture')
        self.other_brand = self.add('brands', code='separate', name='Separate')
        self.add('energy-types', code='battery', name='Battery', name_zh='电池')
        self.add('energy-types', code='other', name='Other', name_zh='其他')
        self.add('energy-types', code='corded', name='Corded')
        dim = self.add('dimensions', code='voltage', name='Voltage')
        unit = self.add('units', code='volt', name='Volt', dimension_id=dim['id'], symbol='V', is_base=True, conversion_factor=1, conversion_offset=0)
        self.unit_id = unit['id']
        self.platform = self.add('voltage-platforms', code='fixture_20v', name='Fixture 20V', brand_id=self.brand['id'], unit_id=unit['id'], canonical_voltage='20V', source_mappings=[{'system':'wotu','source_value':'fixture|20V'}])
        self.store = Store()

    def add(self, kind, **payload):
        return self.catalog.create(kind, payload, actor='test:binding')

    def metadata(self, energy='battery', brand='Fixture', voltage='20V'):
        observation = build_product_observation(brand=brand, model='M1', category='Pressure Washer', voltage=voltage, energy_type=energy, evidence_source='synthetic_fixture')
        platform = build_voltage_platform(observation)
        return {'product_observation': observation, 'voltage_platform': platform, 'task_energy': build_task_energy(observation, platform)}

    def activate(self):
        from collector.task_master_data import install_binding_schema
        with sqlite3.connect(self.store.db_path) as conn:
            install_binding_schema(conn)
        self.store.task_master_data_required = True

    def test_snapshot_hash_and_tampering(self):
        from collector.task_master_data import TaskCatalog
        snapshot = self.catalog.snapshot()
        self.assertEqual(snapshot['catalog_content_hash'], self.catalog.summary()['catalog_content_hash'])
        TaskCatalog(snapshot)
        changed = copy.deepcopy(snapshot)
        changed['records'][0]['name'] = 'tampered'
        with self.assertRaisesRegex(ValueError, 'hash'):
            TaskCatalog(changed)

    def test_create_persists_ids_and_same_revision(self):
        self.activate()
        self.store.create_job('one', 'scrape', 'ego', brand='Fixture', category='Pressure Washer', **self.metadata())
        job = self.store.get_job('one')
        self.assertEqual(job['voltage_platform_id'], self.platform['id'])
        self.assertTrue(job['energy_type_id'].startswith('eng_'))
        self.assertEqual(job['master_data_catalog_hash'], self.catalog.summary()['catalog_content_hash'])

    def test_unknown_platform_zero_write_and_cross_brand_isolation(self):
        self.activate()
        for brand, voltage in [('Fixture', '18V'), ('Separate', '20V')]:
            with self.assertRaisesRegex(ValueError, '后台.*电压平台'):
                self.store.create_job('bad', 'scrape', 'ego', brand=brand, category='Pressure Washer', **self.metadata(brand=brand, voltage=voltage))
            self.assertIsNone(self.store.get_job('bad'))

    def test_preview_uses_create_catalog_and_reports_missing_platform(self):
        from collector.task_master_data import TaskCatalogError
        self.activate()
        with self.assertRaises(TaskCatalogError) as caught:
            self.store.validate_task_metadata('scrape', 'Fixture', **self.metadata(voltage='48V'))
        self.assertEqual(caught.exception.code, 'task_catalog_value_missing')
        valid = self.store.validate_task_metadata('scrape', 'Fixture', **self.metadata())
        self.assertEqual(valid['voltage_platform_id'], self.platform['id'])
        with sqlite3.connect(self.store.db_path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_catalog_unavailable_is_distinct_from_missing_value(self):
        from collector.task_master_data import TaskCatalogUnavailable
        self.activate()
        self.store.remote_master_data = type('Offline', (), {'snapshot': lambda _self: (_ for _ in ()).throw(ValueError('offline'))})()
        with self.assertRaises(TaskCatalogUnavailable):
            self.store.validate_task_metadata('scrape', 'Fixture', **self.metadata())

    def test_local_catalog_entry_failure_is_503_but_missing_platform_is_422(self):
        from collector.task_master_data import TaskCatalogUnavailable, TaskCatalogError
        self.activate()
        with patch.object(self.store.shared_master_data, 'task_snapshot_context', side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(TaskCatalogUnavailable):
                self.store.task_metadata_catalog()
        with self.assertRaises(TaskCatalogError) as caught:
            self.store.validate_task_metadata('scrape', 'Fixture', **self.metadata(voltage='99V'))
        self.assertEqual(caught.exception.status_code, 422)

    def test_other_and_non_dc_preserve_independent_dimensions(self):
        self.activate()
        self.store.create_job('hybrid', 'scrape', 'ego', brand='Fixture', category='Pressure Washer', **self.metadata(energy='other'))
        self.assertEqual(self.store.get_job('hybrid')['voltage_platform_id'], self.platform['id'])
        self.store.create_job('ac', 'scrape', 'ego', brand='Fixture', category='Pressure Washer', **self.metadata(energy='corded'))
        self.assertIsNone(self.store.get_job('ac')['voltage_platform_id'])

    def test_disabled_historical_reference_survives_unrelated_edit(self):
        self.activate()
        self.store.create_job('old', 'scrape', 'ego', brand='Fixture', model='M1', category='Pressure Washer', **self.metadata())
        self.catalog.update('voltage-platforms', self.platform['id'], {'active':False}, actor='test:binding', expected_revision=self.platform['revision'])
        self.store.update_job('old', model='M2')
        self.assertEqual(self.store.get_job('old')['voltage_platform_id'], self.platform['id'])
        with self.assertRaises(ValueError):
            self.store.create_job('new', 'scrape', 'ego', brand='Fixture', category='Pressure Washer', **self.metadata())
        self.assertIsNone(self.store.get_job('new'))

    def test_edit_and_connector_enrichment_bind_in_the_same_transaction(self):
        self.activate()
        self.store.create_job('edit', 'scrape', 'ego', brand='Fixture', category='Pressure Washer', **self.metadata())
        old = self.store.get_job('edit')
        with self.assertRaises(ValueError):
            self.store.update_job('edit', **self.metadata(voltage='99V'))
        self.assertEqual(self.store.get_job('edit'), old)
        with self.assertRaises(ValueError):
            self.store.enrich_metadata('edit', **self.metadata(voltage='99V'))
        self.assertEqual(self.store.get_job('edit'), old)
        self.store.update_job('edit', **self.metadata(energy='corded'))
        self.assertIsNone(self.store.get_job('edit')['voltage_platform_id'])

    def test_dual_voltage_and_active_platform_identity(self):
        dual = self.add('voltage-platforms', code='fixture_dual', name='Fixture 12V/20V', brand_id=self.brand['id'], unit_id=self.unit_id, canonical_voltage='12 volts / 20 V')
        self.assertEqual(dual['canonical_voltage'], '12V/20V')
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.catalog.update('voltage-platforms', dual['id'], {'canonical_voltage':'18V'}, actor='test:binding', expected_revision=dual['revision'])

    def test_active_binding_state_cannot_silently_disable_or_repair(self):
        from collector.task_master_data import install_binding_schema
        with sqlite3.connect(self.store.db_path) as conn:
            install_binding_schema(conn)
            conn.execute("INSERT INTO task_master_data_binding_state(id,required) VALUES(1,1)")
        with self.assertRaisesRegex(ValueError, 'MASTER_DATA_TASK_BINDINGS_REQUIRED'):
            Store()

    def test_remote_authority_failure_is_zero_write_and_ignores_archive(self):
        from collector.remote_master_data import RemoteMasterData
        self.activate()
        self.store.shared_master_data = None
        self.store.remote_master_data = RemoteMasterData('http://authority.invalid/api/master-data')
        with patch.object(RemoteMasterData, 'snapshot', return_value=self.catalog.snapshot()):
            self.store.create_job('remote', 'scrape', 'ego', brand='Fixture', **self.metadata())
        with patch.object(RemoteMasterData, 'snapshot', side_effect=ValueError('authority offline')):
            with self.assertRaisesRegex(ValueError, '共享主数据目录暂不可用'):
                self.store.create_job('offline', 'scrape', 'ego', brand='Fixture', **self.metadata())
        self.assertIsNone(self.store.get_job('offline'))
        self.assertEqual(self.store.get_job('remote')['voltage_platform_id'], self.platform['id'])

    def test_stale_or_cross_brand_historical_reference_is_detected(self):
        from collector.task_master_data import TaskCatalog
        self.activate()
        self.store.create_job('bound', 'scrape', 'ego', brand='Fixture', **self.metadata())
        alternate = self.add('voltage-platforms', code='separate20', name='Separate 20V', brand_id=self.other_brand['id'], unit_id=self.unit_id, canonical_voltage='20V')
        row = self.store.get_job('bound')
        row['voltage_platform_id'] = alternate['id']
        with self.assertRaisesRegex(ValueError, '品牌'):
            TaskCatalog(self.catalog.snapshot()).verify_existing(row)
        with self.store._connect() as conn:
            conn.execute("UPDATE jobs SET energy_type='other' WHERE id='bound'")
        self.assertIsNone(self.store.get_job('bound')['energy_type_id'])
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            self.store.task_master_data_status()


if __name__ == '__main__':
    unittest.main()
