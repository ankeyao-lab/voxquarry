"""Frozen, bounded source directories and compatible open batches.

This is data preparation, not a research planner. The caller still chooses the
scope and which batches to read. A directory never establishes review counts.
"""
from contextlib import closing
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from . import source
from .wenquan_datasets import DatasetError, _hash, _identity, _json, _scope, _sqlite_code, compact_job

CONTRACT = 'voxquarry.wenquan-catalog/v2'
CATALOG_ID = re.compile(r'cat_[0-9a-f]{64}\Z')
PAGE_BYTES = 700_000
BATCH_RUNS = 50


def _version(backend, item, run):
    files = {}
    for name in ('validation_report.json', 'reviews.csv', 'reviews.raw.jsonl'):
        try:
            with backend._read(backend.source_root, run['relative'] / name,
                               backend.limits.max_source_bytes) as (_, info):
                files[name] = list(_identity(info))
        except DatasetError as exc:
            if name == 'reviews.raw.jsonl' and isinstance(exc.__cause__, FileNotFoundError):
                continue
            raise
    return {'job_sha256': _hash(compact_job(item['job'])),
            'validation_sha256': run['report_hash'], 'files': files}


def entries(backend, scope, selected=None):
    """Release each physical validation report before loading the next one."""
    jobs = backend._iter_jobs(selected, job_ids=scope.get('job_ids'), scope=scope)
    for item in jobs:
        job = item['job']
        if scope.get('job_ids') and job['id'] not in scope['job_ids']:
            continue
        one_scope = {**scope, 'job_ids': [job['id']]} if scope.get('job_ids') else scope
        phase = 'validation'
        try:
            runs = backend._runs(one_scope, [item])
            if not runs:
                continue
            run = runs[0]
            phase = 'artifact'
            # Only the bounded CSV header is read; rows remain deferred until
            # the exact selected batch is frozen and reconciled.
            with backend._read(backend.source_root, run['relative'] / 'reviews.csv',
                               backend.limits.max_source_bytes) as (handle, _):
                line = handle.readline(65_537)
                if len(line) > 65_536:
                    raise source.MarketContractError('CSV header exceeds catalog limit')
                fields = set(next(csv.reader(io.StringIO(line.decode('utf-8-sig'))), []))
            compatibility = source._contract_mode_for_fields(run, fields)
            version = _version(backend, item, run)
            report = run['report']
            public = {'job_id': job['id'], 'channel': run['channel'],
                      'brand': job.get('brand'), 'model': job.get('model'),
                      'category_id': source._job_category_id(job), 'market_region': job.get('market_region'),
                      'product_id': run['product_identity'], 'product_url': job.get('target_url'),
                      'attempt_no': job.get('active_attempt_no'), 'collected_at': job.get('finished_at'),
                      'status': run['job']['status'], 'eligible_reviews': report.get('eligible_reviews', report.get('unique_reviews')),
                      'written_reviews': report.get('valid_text_reviews'), 'counts_verified': False,
                      'voltage_platform': source.voltage_platform_fact(job),
                      'validation_sha256': run['report_hash'], 'compatibility': compatibility,
                      'source_version': _hash(version), 'available': True}
            selection = {'job': compact_job(job), 'run_dir': str(run['relative']), 'version': version,
                         'metadata_bytes': len(_json(job).encode()) + run['report_stat'][2],
                         'source_bytes': sum(info[2] for info in version['files'].values())}
            yield public, selection, (run['channel'], run['product_identity']), (
                run['job']['status'] == 'completed', str(job.get('finished_at') or ''),
                str(job.get('created_at') or ''), job['id'])
        except (source.MarketContractError, UnicodeError, csv.Error) as exc:
            yield {'job_id': job['id'], 'channel': job['channel'], 'available': False,
                   'reason': 'review_contract_invalid'}, None, None, None
        except DatasetError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                reason = 'validation_manifest_missing' if phase == 'validation' else 'source_artifact_missing'
            elif exc.code in {'invalid_source', 'source_unavailable'}:
                reason = exc.diagnostic.get('reason', 'invalid_source')
            else:
                # Links, version races and unsafe file types are never gaps.
                raise
            yield {'job_id': job['id'], 'channel': job['channel'], 'available': False,
                   'reason': reason}, None, None, None
        except ValueError:
            yield {'job_id': job['id'], 'channel': job['channel'], 'available': False,
                   'reason': 'source_contract_invalid'}, None, None, None


class Catalogs:
    def __init__(self, backend):
        self.backend = backend
        self.root = backend.storage_root
        self.verified = {}

    def build(self, scope, sources, *, origin='auto'):
        try:
            return self._build(scope, sources, origin=origin)
        except sqlite3.OperationalError as exc:
            raise DatasetError('catalog_storage_unavailable', '目录索引未完成，未发布部分范围',
                               reason='catalog_sqlite_error', phase='catalog', sqlite_code=_sqlite_code(exc)) from exc

    def _build(self, scope, sources, *, origin):
        scope = _scope(scope)
        deadline = time.monotonic() + self.backend.limits.max_duration_seconds
        with tempfile.TemporaryDirectory(prefix='.catalog-v2-', dir=self.root) as temporary:
            path = Path(temporary) / 'catalog.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('PRAGMA journal_mode=OFF')
                db.execute('PRAGMA temp_store=FILE')
                db.execute('PRAGMA max_page_count=' + str(max(1, self.backend.limits.max_catalog_bytes // 4096)))
                db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
                db.execute('CREATE TABLE entries (job_id TEXT PRIMARY KEY, origin TEXT, available INTEGER, compatibility TEXT, batch_id TEXT, payload TEXT, selection TEXT)')
                db.execute('CREATE TABLE chosen (product TEXT PRIMARY KEY, job_id TEXT, score TEXT)')
                for name, iterator in sources:
                    for public, selected, product, score in iterator:
                        if time.monotonic() > deadline:
                            raise DatasetError('catalog_timeout', '目录准备超过时限，未发布部分范围')
                        available = public['available']
                        # Snapshot discovery retains the existing complete-first
                        # product policy. Exact IDs and current collector runs do not.
                        if name == 'snapshot' and available and not scope.get('job_ids'):
                            key = _json(product)
                            previous = db.execute('SELECT job_id,score FROM chosen WHERE product=?', (key,)).fetchone()
                            if previous and tuple(json.loads(previous[1])) >= tuple(score):
                                continue
                            if previous:
                                db.execute('DELETE FROM entries WHERE job_id=?', (previous[0],))
                            db.execute('INSERT OR REPLACE INTO chosen VALUES (?,?,?)', (key, public['job_id'], _json(score)))
                        existing = db.execute('SELECT available FROM entries WHERE job_id=?', (public['job_id'],)).fetchone()
                        if existing and existing[0] and not available:
                            # A missing current artifact cannot silently make a
                            # historical version look current or fully covered.
                            previous = db.execute('SELECT payload FROM entries WHERE job_id=?', (public['job_id'],)).fetchone()
                            prior = json.loads(previous[0]); prior['current_source_unavailable'] = public['reason']
                            db.execute('UPDATE entries SET payload=? WHERE job_id=?', (_json(prior), public['job_id']))
                            continue
                        public = {**public, 'origin': name}
                        db.execute('INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?,?)',
                                   (public['job_id'], name, int(available), public.get('compatibility'), None,
                                    _json(public), _json(selected)))
                        if path.stat().st_size > self.backend.limits.max_catalog_bytes:
                            raise DatasetError('catalog_too_large', '目录索引超过磁盘容量，未缩小范围')
                # Exact requested but absent tasks are explicit gaps.
                for job in scope.get('job_ids', []):
                    if not db.execute('SELECT 1 FROM entries WHERE job_id=?', (job,)).fetchone():
                        row = {'job_id': job, 'available': False, 'reason': 'source_unavailable', 'origin': origin}
                        db.execute('INSERT INTO entries VALUES (?,?,0,NULL,NULL,?,NULL)', (job, origin, _json(row)))
                db.execute('DROP TABLE chosen')
                db.execute('CREATE TABLE batches (batch_id TEXT PRIMARY KEY, payload TEXT)')
                batch, metadata_bytes, source_bytes = [], 0, 0

                def finish():
                    if not batch:
                        return
                    batch_id = 'batch_' + _hash([(r[0], r[2]['source_version']) for r in batch])[7:]
                    payload = {'batch_id': batch_id, 'compatibility': batch[0][1],
                               'job_ids': [r[0] for r in batch], 'source_bytes': source_bytes}
                    db.execute('INSERT INTO batches VALUES (?,?)', (batch_id, _json(payload)))
                    db.executemany('UPDATE entries SET batch_id=? WHERE job_id=?', [(batch_id, r[0]) for r in batch])

                rows = db.execute('SELECT job_id,compatibility,payload,selection FROM entries WHERE available=1 ORDER BY compatibility,job_id')
                for job_id, compatibility, raw, private in rows:
                    public, selected = json.loads(raw), json.loads(private)
                    m, s = selected['metadata_bytes'], selected['source_bytes']
                    if batch and (compatibility != batch[0][1] or len(batch) >= BATCH_RUNS
                                  or metadata_bytes + m > self.backend.limits.max_metadata_bytes // 2
                                  or source_bytes + s > self.backend.limits.max_source_bytes // 2):
                        finish(); batch, metadata_bytes, source_bytes = [], 0, 0
                    batch.append((job_id, compatibility, public)); metadata_bytes += m; source_bytes += s
                finish()
                db.execute('CREATE INDEX entries_batch ON entries(batch_id,job_id)')
                totals = dict(db.execute('SELECT available,count(*) FROM entries GROUP BY available'))
                stale = db.execute("SELECT count(*) FROM entries WHERE json_extract(payload,'$.current_source_unavailable') IS NOT NULL").fetchone()[0]
                info = {'contract': CONTRACT, 'scope': scope, 'origin': origin, 'total': totals.get(1, 0),
                        'total_sources': sum(totals.values()), 'unavailable_count': totals.get(0, 0) + stale,
                        'batch_count': db.execute('SELECT count(*) FROM batches').fetchone()[0]}
                db.commit()
            if path.stat().st_size > self.backend.limits.max_catalog_bytes:
                raise DatasetError('catalog_too_large', '目录索引超过磁盘容量，未发布部分范围')
            digest = self.backend._file_hash(path)
            info['index_sha256'] = digest
            catalog_id = 'cat_' + _hash(info)[7:]
            info['catalog_id'] = catalog_id
            if (self.root / (catalog_id + '.json')).exists() and (self.root / (catalog_id + '.sqlite3')).exists():
                # Reuse the existing inode so concurrent cursors are not
                # invalidated by an identical catalog rebuild.
                with self.read(catalog_id):
                    pass
                return catalog_id
            manifest = Path(temporary) / 'manifest.json'
            manifest.write_text(_json(info), encoding='utf-8')
            path.chmod(0o400); manifest.chmod(0o400)
            os.replace(path, self.root / (catalog_id + '.sqlite3'))
            os.replace(manifest, self.root / (catalog_id + '.json'))
            self._prune(catalog_id)
            return catalog_id

    def _prune(self, keep):
        # Cached directories are rebuildable; datasets and user files are never
        # removed. Evicted cursors fail explicitly rather than changing scope.
        paths = []
        for path in self.root.glob('cat_*.sqlite3'):
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            paths.append((info.st_mtime, info.st_size, path))
        paths.sort(reverse=True)
        used = 0
        for n, (_, size, path) in enumerate(paths):
            used += size
            if path.stem != keep and (n >= 64 or used > 512 * 1024**2):
                path.unlink(missing_ok=True)
                path.with_suffix('.json').unlink(missing_ok=True)

    def _info(self, catalog_id):
        if not isinstance(catalog_id, str) or not CATALOG_ID.fullmatch(catalog_id):
            raise DatasetError('invalid_cursor', '目录版本标识无效', 422)
        try:
            with self.backend._read(self.root, catalog_id + '.json', PAGE_BYTES) as (handle, _):
                info = json.load(handle)
        except DatasetError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                raise DatasetError('catalog_expired', '目录已过期，请按原范围重新获取') from exc
            raise
        if not isinstance(info, dict) or 'cat_' + _hash({k: v for k, v in info.items() if k != 'catalog_id'})[7:] != catalog_id:
            raise DatasetError('source_changed', '目录版本校验失败')
        return info

    def read(self, catalog_id):
        """Keep the descriptor held and verify the immutable index like datasets."""
        from contextlib import contextmanager

        @contextmanager
        def opened():
            info = self._info(catalog_id)
            with self.backend._read(self.root, catalog_id + '.sqlite3', self.backend.limits.max_catalog_bytes) as (handle, before):
                key = catalog_id, _identity(before)
                if key not in self.verified:
                    digest = hashlib.sha256()
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                    if 'sha256:' + digest.hexdigest() != info['index_sha256']:
                        raise DatasetError('source_changed', '目录索引校验失败')
                    self.verified = {key: True}  # bounded validation cache
                with closing(sqlite3.connect(source.sqlite_uri(self.root / (catalog_id + '.sqlite3')), uri=True)) as db:
                    yield db, info
        return opened()

    def page(self, catalog_id=None, *, scope=None, cursor=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DatasetError('invalid_request', '目录每页条数应为 1 至 100', 422)
        offset = 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
                    raise ValueError()
                value = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')))
                if not isinstance(value, dict) or set(value) != {'catalog_id', 'offset'} or type(value['offset']) is not int or value['offset'] < 0:
                    raise ValueError()
                if catalog_id and catalog_id != value['catalog_id']:
                    raise ValueError()
                catalog_id, offset = value['catalog_id'], value['offset']
            except (ValueError, TypeError, UnicodeError):
                raise DatasetError('invalid_cursor', '目录游标无效', 422)
        with self.read(catalog_id) as (db, info):
            if scope is not None and _scope(scope) != info['scope']:
                raise DatasetError('invalid_cursor', '目录游标不能改变原范围', 422)
            if offset > info['total_sources']:
                raise DatasetError('invalid_cursor', '目录游标超出范围', 422)
            result = {k: v for k, v in info.items() if k != 'index_sha256'}
            result.update(items=[], unavailable_sources=[], batches=[], report_required=False,
                          catalog_complete=info['unavailable_count'] == 0)
            seen, returned = set(), 0
            for raw, batch_id in db.execute('SELECT payload,batch_id FROM entries ORDER BY job_id LIMIT ? OFFSET ?', (limit, offset)):
                item = json.loads(raw)
                target = 'items' if item['available'] else 'unavailable_sources'
                batch = None
                if batch_id and batch_id not in seen:
                    batch = json.loads(db.execute('SELECT payload FROM batches WHERE batch_id=?', (batch_id,)).fetchone()[0])
                    result['batches'].append(batch)
                if batch_id:
                    item['batch_id'] = batch_id
                result[target].append(item)
                if len(_json(result).encode()) > PAGE_BYTES - 4096:
                    result[target].pop()
                    if batch:
                        result['batches'].pop()
                    if not returned:
                        raise DatasetError('catalog_item_too_large', '单项目目录字段超过容量，未截断来源')
                    break
                seen.add(batch_id); returned += 1
            end = offset + returned
            result['next_cursor'] = (base64.urlsafe_b64encode(_json({'catalog_id': catalog_id, 'offset': end}).encode()).decode()
                                     if end < info['total_sources'] else None)
            result['page'] = {'offset': offset, 'returned': returned, 'next_offset': end,
                              'enumeration_complete': end == info['total_sources']}
            return result

    def gaps(self, catalog_id):
        with self.read(catalog_id) as (db, _):
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM entries WHERE available=0 OR json_extract(payload,'$.current_source_unavailable') IS NOT NULL ORDER BY job_id LIMIT 50")]

    def batch(self, catalog_id, batch_id):
        if not isinstance(batch_id, str) or not re.fullmatch(r'batch_[0-9a-f]{64}', batch_id):
            raise DatasetError('invalid_request', '目录批次无效', 422)
        with self.read(catalog_id) as (db, info):
            rows = db.execute('SELECT origin,payload,selection FROM entries WHERE batch_id=? ORDER BY job_id', (batch_id,)).fetchall()
            if not rows:
                raise DatasetError('source_unavailable', '该目录中没有此批次')
            return info, [(name, json.loads(public), json.loads(private)) for name, public, private in rows]
