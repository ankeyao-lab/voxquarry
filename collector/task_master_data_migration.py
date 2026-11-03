"""Explicit, plan-sealed energy/platform catalog and task-reference migration.

Run only against stopped consumers after a verified off-host backup. The plan
is generated/dry-run on consistent copies, never by inspecting and mutating a
live database opportunistically. No source fact or task lifecycle is rewritten.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from .master_data import MasterDataStore, canonical_json, stable_id, utc_now
from .task_master_data import BINDING_COLUMNS, TaskCatalog, install_binding_schema, write_binding

SCHEMA = 'voxquarry.task-master-data-migration/v1'
ACTOR = 'migration:voxquarry-migration'


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def source_jobs(conn):
    columns = [r[1] for r in conn.execute('PRAGMA table_info(jobs)') if r[1] not in BINDING_COLUMNS]
    rows = [dict(r) for r in conn.execute('SELECT ' + ','.join('"' + c + '"' for c in columns) + ' FROM jobs ORDER BY id')]
    if not rows or any(r['status'] in ('queued', 'running', 'cancelling') for r in rows):
        raise ValueError('nonempty quiescent jobs required')
    return rows


def read_jobs(path):
    conn = sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return source_jobs(conn)
    finally:
        conn.close()


def make_plan(catalog, jobs_path):
    snapshot = catalog.snapshot()
    context = TaskCatalog(snapshot)
    jobs = read_jobs(jobs_path)
    for kind in ('voltage-platforms', 'dimensions', 'units'):
        if context.by_type.get(kind):
            raise ValueError('initial bounded migration requires empty platform/dimension/unit catalogs')
    creations = [
        {'type': 'dimensions', 'payload': {'code': 'voltage', 'name': 'Voltage', 'name_zh': '电压', 'definition': '电势差；电压平台采用 V 单位。'}},
        {'type': 'units', 'payload': {'code': 'volt', 'name': 'Volt', 'name_zh': '伏特', 'symbol': 'V', 'dimension_id': stable_id('dimensions', 'voltage'), 'is_base': True, 'conversion_factor': 1, 'conversion_offset': 0}},
    ]
    platforms = {}
    for job in jobs:
        if not job['voltage_platform_key']:
            continue
        if job['voltage_platform_status'] != 'known':
            raise ValueError('platform status/key mismatch')
        brand = context._brand(job['voltage_platform_brand'])
        if brand['id'] != context._brand(job['brand'])['id']:
            raise ValueError('source brand conflict')
        key = job['voltage_platform_key']
        value = (brand['id'], job['voltage_platform_voltage'], brand['name'])
        if key in platforms and platforms[key] != value:
            raise ValueError('platform key has multiple identities')
        platforms[key] = value
    for key, (brand_id, voltage, brand_name) in sorted(platforms.items()):
        code = 'wotu_' + hashlib.sha256(key.encode()).hexdigest()[:20]
        creations.append({'type': 'voltage-platforms', 'payload': {
            'code': code, 'name': f'{brand_name} {voltage}', 'brand_id': brand_id,
            'canonical_voltage': voltage, 'unit_id': stable_id('units', 'volt'),
            'definition': '来自已治理的数据采集平台字段；不推断不同标称电压等价。',
            'source_mappings': [{'system': 'wotu', 'source_value': key}],
        }})
    other = context._one([r for r in context.by_type.get('energy-types', []) if r['code'] == 'other'], '既有 other 能源身份')
    plan = {'schema_version': SCHEMA, 'actor': ACTOR, 'created_at': utc_now(),
            'catalog_before_hash': snapshot['catalog_content_hash'], 'catalog_before_revision': snapshot['catalog_revision'],
            'jobs_source_hash': digest(jobs), 'job_count': len(jobs),
            'platform_bindings': sum(bool(r['voltage_platform_key']) for r in jobs),
            'energy_bindings': sum(r['energy_type'] not in (None, 'unknown') for r in jobs),
            'creations': creations, 'other_id': other['id'],
            'other_definition': '已证明的混合动力或其他能源类型；不是未知能源的兜底。电压平台独立按来源证据维护。'}
    plan['plan_hash'] = digest(plan)
    return plan


def validate_plan(plan):
    content = {k: v for k, v in plan.items() if k != 'plan_hash'}
    if plan.get('schema_version') != SCHEMA or plan.get('actor') != ACTOR or plan.get('plan_hash') != digest(content):
        raise ValueError('migration plan seal invalid')


def seal_catalog_result(plan, snapshot):
    """Pin the predicted catalog bytes after the first copy-only dry run."""
    validate_plan(plan)
    plan = {k: v for k, v in plan.items() if k != 'plan_hash'}
    plan['catalog_after_hash'] = TaskCatalog(snapshot).content_hash
    plan['plan_hash'] = digest(plan)
    return plan


def apply_catalog(catalog, jobs_path, plan):
    validate_plan(plan)
    if digest(read_jobs(jobs_path)) != plan['jobs_source_hash']:
        raise ValueError('source jobs drift; stop and replan')
    actor, now = plan['actor'], plan['created_at']
    with catalog.lock, catalog._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = catalog._snapshot_in(conn)
        if current['catalog_content_hash'] != plan['catalog_before_hash']:
            raise ValueError('catalog drift or migration already applied; verify explicit result instead')
        before_other = catalog._entity(conn, plan['other_id'])
        if before_other['type'] != 'energy-types' or before_other['code'] != 'other':
            raise ValueError('other stable identity mismatch')
        events = []
        for item in plan['creations']:
            kind = item['type']
            if kind not in ('dimensions', 'units', 'voltage-platforms'):
                raise ValueError('unapproved catalog type')
            payload = catalog._normalize_payload(conn, kind, item['payload'], creating=True)
            entity_id = stable_id(kind, payload['code'])
            catalog._insert_entity(conn, kind, entity_id, payload, actor, now)
            events.append((kind, entity_id, None, catalog._entity(conn, entity_id)))
        # Deliberately keep every other field, alias, mapping and stable ID.
        conn.execute('UPDATE master_entities SET active=1,definition=?,revision=revision+1,updated_at=?,updated_by=? WHERE id=?', (plan['other_definition'], now, actor, plan['other_id']))
        events.append(('energy-types', plan['other_id'], before_other, catalog._entity(conn, plan['other_id'])))
        revision, content_hash = catalog._advance_catalog(conn, None, None, 'task_energy_voltage_activation', actor, now)
        if plan.get('catalog_after_hash') and content_hash != plan['catalog_after_hash']:
            raise ValueError('catalog prediction mismatch; transaction rolled back')
        for kind, entity_id, before, after in events:
            catalog._audit(conn, catalog_revision=revision, entity_type=kind, entity_id=entity_id, entity_revision=after['revision'], action='task_energy_voltage_activation', actor=actor, before=before, after=after, now=now)
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('catalog integrity failed')
        conn.commit()
    return catalog.snapshot()


def apply_jobs(jobs_path, snapshot, plan):
    validate_plan(plan)
    context = TaskCatalog(snapshot)
    if context.revision != plan['catalog_before_revision'] + 1 or context.content_hash != plan.get('catalog_after_hash'):
        raise ValueError('expected exact post-migration catalog revision')
    conn = sqlite3.connect(jobs_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE')
        if digest(source_jobs(conn)) != plan['jobs_source_hash']:
            raise ValueError('source jobs drift; zero writes permitted')
        install_binding_schema(conn)
        changed = 0
        for row in conn.execute('SELECT * FROM jobs ORDER BY id').fetchall():
            expected = context.bind(dict(row), previous=dict(row))
            if all(row[k] == expected[k] for k in BINDING_COLUMNS):
                continue
            binding = write_binding(conn, row['id'], context, previous=dict(row))
            conn.execute('INSERT INTO job_events(job_id,attempt_no,level,code,message,detail_json,created_at) VALUES(?,?,?,?,?,?,?)', (row['id'], row['attempt_count'], 'info', 'shared_master_data_bound', 'Bound governed energy/platform facts to shared catalog IDs', canonical_json({'plan_hash': plan['plan_hash'], 'binding': binding}), plan['created_at']))
            changed += 1
        conn.execute('INSERT INTO task_master_data_binding_state(id,required) VALUES(1,1) ON CONFLICT(id) DO UPDATE SET required=1')
        for row in conn.execute('SELECT * FROM jobs'):
            context.verify_existing(dict(row))
        if digest(source_jobs(conn)) != plan['jobs_source_hash']:
            raise ValueError('migration changed a source field')
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('jobs integrity failed')
        counts = dict(conn.execute('SELECT "energy" AS kind,count(*) FROM jobs WHERE energy_type_id IS NOT NULL UNION ALL SELECT "platform",count(*) FROM jobs WHERE voltage_platform_id IS NOT NULL'))
        if counts != {'energy': plan['energy_bindings'], 'platform': plan['platform_bindings']}:
            raise ValueError('binding counts differ from approved plan')
        conn.commit()
        return {'changed': changed, 'counts': counts, 'catalog_revision': context.revision, 'catalog_content_hash': context.content_hash, 'source_facts_unchanged': True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['plan', 'catalog', 'jobs'])
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--snapshot', type=Path)
    args = parser.parse_args()
    jobs_path = args.data_dir / 'jobs.sqlite3'
    if args.phase == 'plan':
        result = make_plan(MasterDataStore(args.data_dir, initialize=False), jobs_path)
    else:
        plan = json.loads(args.plan.read_text())
        result = apply_catalog(MasterDataStore(args.data_dir, initialize=False), jobs_path, plan) if args.phase == 'catalog' else apply_jobs(jobs_path, json.loads(args.snapshot.read_text()), plan)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == '__main__':
    main()
