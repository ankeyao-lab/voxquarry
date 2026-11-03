"""Version-pinned task references to the single shared energy/platform catalog.

This module never invents catalog values or rewrites source observations. The
schema/backfill is explicit; runtime writes only resolve values already present
in a verified catalog snapshot. Inactive historical references remain readable.
"""
import hashlib
import re

from .master_data import canonical_json, normalize_identity
from .product_identity import normalize_voltage

SNAPSHOT_SCHEMA = 'voxquarry.shared-master-data-snapshot/v1'
BINDING_COLUMNS = {
    'energy_type_id': 'TEXT',
    'voltage_platform_id': 'TEXT',
    'master_data_catalog_revision': 'INTEGER',
    'master_data_catalog_hash': 'TEXT',
    'master_data_binding_fingerprint': 'TEXT',
}
FACT_FIELDS = ('brand', 'energy_type', 'energy_readiness_status',
               'voltage_platform_status', 'voltage_platform_key',
               'voltage_platform_brand', 'voltage_platform_voltage')


class TaskCatalogError(ValueError):
    code = 'task_catalog_value_missing'
    status_code = 422


class TaskCatalogUnavailable(TaskCatalogError):
    code = 'task_catalog_unavailable'
    status_code = 503


def fact_fingerprint(job):
    return hashlib.sha256(canonical_json({k: job.get(k) for k in FACT_FIELDS}).encode()).hexdigest()


def install_binding_schema(conn):
    """Explicit migration only; caller owns a rollback-capable transaction."""
    existing = {r[1] for r in conn.execute('PRAGMA table_info(jobs)')}
    if not existing:
        raise ValueError('existing jobs schema required')
    for column, declaration in BINDING_COLUMNS.items():
        if column not in existing:
            conn.execute(f'ALTER TABLE jobs ADD COLUMN {column} {declaration}')
    conn.execute('CREATE TABLE IF NOT EXISTS task_master_data_binding_state (id INTEGER PRIMARY KEY CHECK(id=1), required INTEGER NOT NULL CHECK(required IN (0,1)))')
    changed = ' OR '.join(f'OLD.{name} IS NOT NEW.{name}' for name in FACT_FIELDS)
    # An out-of-band metadata correction must not leave apparently valid IDs.
    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS task_master_data_invalidate
        AFTER UPDATE OF {','.join(FACT_FIELDS)} ON jobs WHEN {changed}
        BEGIN
          UPDATE jobs SET energy_type_id=NULL,voltage_platform_id=NULL,
            master_data_catalog_revision=NULL,master_data_catalog_hash=NULL,
            master_data_binding_fingerprint=NULL WHERE id=NEW.id;
        END''')


class TaskCatalog:
    def __init__(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get('schema_version') != SNAPSHOT_SCHEMA:
            raise ValueError('invalid task catalog snapshot schema')
        records = snapshot.get('records')
        if not isinstance(records, list) or len(records) > 10000:
            raise ValueError('invalid task catalog records')
        digest = hashlib.sha256(canonical_json(records).encode()).hexdigest()
        if digest != snapshot.get('catalog_content_hash'):
            raise ValueError('task catalog snapshot hash mismatch')
        revision = snapshot.get('catalog_revision')
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError('invalid task catalog revision')
        self.snapshot = snapshot
        self.revision = revision
        self.content_hash = digest
        self.by_id = {}
        self.by_type = {}
        for record in records:
            if not isinstance(record, dict) or not all(record.get(k) for k in ('id', 'type', 'code', 'name')):
                raise ValueError('invalid task catalog record')
            if record['id'] in self.by_id or not isinstance(record.get('active'), bool):
                raise ValueError('duplicate task catalog ID or invalid status')
            self.by_id[record['id']] = record
            self.by_type.setdefault(record['type'], []).append(record)

    def _one(self, matches, label):
        rows = {r['id']: r for r in matches}
        if len(rows) != 1:
            raise TaskCatalogError(f'后台未维护唯一启用的{label}，请先在共享后台维护')
        return next(iter(rows.values()))

    def _brand(self, value):
        key = normalize_identity(value)
        return self._one([r for r in self.by_type.get('brands', []) if r['active'] and
                          key in {normalize_identity(v) for v in [r['code'], r['name'], *r.get('aliases', [])]}], '品牌')

    def bind(self, job, *, previous=None, historical=False):
        job = dict(job)
        previous = dict(previous or {})
        fingerprint = fact_fingerprint(job)
        unchanged = previous.get('master_data_binding_fingerprint') == fingerprint
        can_keep = historical or unchanged
        energy_code = job.get('energy_type')
        if not energy_code or energy_code == 'unknown':
            if job.get('action') in ('scrape', 'external_import') and job.get('energy_readiness_status') != 'metadata_incomplete':
                raise TaskCatalogError('后台能源关联要求明确的任务能源；未知值不能成为就绪任务')
            energy = None
        else:
            old = self.by_id.get(previous.get('energy_type_id')) if can_keep else None
            if old and old['type'] == 'energy-types' and old['code'] == energy_code:
                energy = old
            else:
                energy = self._one([r for r in self.by_type.get('energy-types', [])
                                    if r['code'] == energy_code and r['active']], '能源类型')
        voltage = normalize_voltage(job.get('voltage_platform_voltage'))
        key = job.get('voltage_platform_key')
        platform = None
        if key:
            if job.get('voltage_platform_status') != 'known' or not voltage or energy_code in ('corded', 'fuel', 'unknown', None):
                raise TaskCatalogError('任务能源与电压平台状态矛盾')
            old = self.by_id.get(previous.get('voltage_platform_id')) if can_keep else None
            if old and old['type'] == 'voltage-platforms' and old['canonical_voltage'] == voltage:
                platform = old
            else:
                active = [r for r in self.by_type.get('voltage-platforms', []) if r['active'] and r.get('canonical_voltage') == voltage]
                mapped = [r for r in active if any(m['system'] == 'wotu' and normalize_identity(m['source_value']) == normalize_identity(key) for m in r.get('source_mappings', []))]
                if mapped:
                    platform = self._one(mapped, '电压平台')
                else:
                    try:
                        brand = self._brand(job.get('voltage_platform_brand'))
                    except ValueError as exc:
                        raise TaskCatalogError('后台未维护电压平台所属品牌') from exc
                    platform = self._one([r for r in active if r.get('brand_id') == brand['id']], f"电压平台（{brand['name']} {voltage}）")
            # Renames retain the old name as an alias. Neither historical IDs
            # nor an administrator's source mapping can cross brand identity.
            brand_record = self.by_id.get(platform['brand_id'], {})
            brand_values = {normalize_identity(v) for v in [brand_record.get('name'), brand_record.get('code'), *brand_record.get('aliases', [])] if v}
            if any(normalize_identity(job.get(field)) not in brand_values for field in ('brand', 'voltage_platform_brand')):
                raise TaskCatalogError('后台电压平台品牌不匹配')
        elif energy_code == 'battery' and job.get('energy_readiness_status') == 'ready':
            raise TaskCatalogError('后台电压平台关联要求电池任务具有已证明的平台')
        return {
            'energy_type_id': energy['id'] if energy else None,
            'voltage_platform_id': platform['id'] if platform else None,
            'master_data_catalog_revision': self.revision,
            'master_data_catalog_hash': self.content_hash,
            'master_data_binding_fingerprint': fingerprint,
        }

    def verify_existing(self, job):
        job = dict(job)
        if job.get('master_data_binding_fingerprint') != fact_fingerprint(job):
            raise ValueError('task master-data fingerprint missing or stale; explicit backfill required')
        expected = self.bind(job, previous=job, historical=True)
        for field in ('energy_type_id', 'voltage_platform_id'):
            if expected[field] != job.get(field):
                raise ValueError('task master-data reference missing or invalid; explicit backfill required')
        if not isinstance(job.get('master_data_catalog_revision'), int) or not 0 <= job['master_data_catalog_revision'] <= self.revision or not re.fullmatch(r'[0-9a-f]{64}', job.get('master_data_catalog_hash') or ''):
            raise ValueError('task master-data catalog version missing or invalid')


def write_binding(conn, job_id, catalog, *, previous=None):
    row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        raise ValueError('task not found while binding')
    binding = catalog.bind(dict(row), previous=previous)
    conn.execute('UPDATE jobs SET ' + ','.join(f'{field}=?' for field in BINDING_COLUMNS) + ' WHERE id=?', [binding[k] for k in BINDING_COLUMNS] + [job_id])
    return binding
