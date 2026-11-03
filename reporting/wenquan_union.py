"""Deterministic cross-batch counts, using the existing normalized identities."""
from contextlib import closing
import sqlite3
import tempfile
import time
from pathlib import Path

from .wenquan_datasets import DatasetError, CONTRACT, _hash, _sqlite_code


def aggregate_union(backend, dataset_ids, **arguments):
    if (not isinstance(dataset_ids, list) or not 1 <= len(dataset_ids) <= 400
            or any(not isinstance(v, str) for v in dataset_ids) or len(set(dataset_ids)) != len(dataset_ids)):
        raise DatasetError('invalid_request', '跨批次统计需要 1 至 400 个不重复数据集', 422)
    # Validate selectors before any disk work.
    backend._where(arguments.get('filters'), arguments.get('query'), population=arguments.get('population', 'entities'))
    started, read, versions, normalization, compatibility = time.monotonic(), 0, [], None, None
    def guard():
        if time.monotonic() - started > backend.limits.max_duration_seconds:
            raise DatasetError('dataset_timeout', '跨批次统计超过时限，未返回部分统计')
    with tempfile.TemporaryDirectory(prefix='.union-', dir=backend.storage_root) as temporary:
        with closing(sqlite3.connect(Path(temporary) / 'union.sqlite3')) as db:
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('PRAGMA temp_store=FILE')
            db.execute('PRAGMA max_page_count=' + str(max(1, backend.limits.max_storage_bytes // 4096)))
            db.set_progress_handler(lambda: int(time.monotonic() - started > backend.limits.max_duration_seconds), 10000)
            db.execute('CREATE TABLE facts(occurrence TEXT PRIMARY KEY, entity TEXT, review_id TEXT, channel TEXT, brand TEXT, model TEXT, market_region TEXT, category_id TEXT, rating INTEGER, has_text INTEGER, job_id TEXT, submission_date TEXT, submission_month TEXT, search_text TEXT, sort_key TEXT, canonical INTEGER, payload TEXT, row_hash TEXT)')
            columns = 'occurrence,entity,review_id,channel,brand,model,market_region,category_id,rating,has_text,job_id,submission_date,submission_month,search_text,sort_key'
            try:
                for dataset_id in sorted(dataset_ids):
                    guard()
                    with backend._index(dataset_id) as (index, manifest):
                        version = manifest['normalization_version']
                        current = [version, manifest.get('normalization_contracts', 'legacy-unversioned-contracts')]
                        if compatibility is not None and compatibility != current:
                            raise DatasetError('invalid_source', '数据集归一化版本不同，不能合并计数；请使用同一版本重新准备', reason='mixed_normalization_versions')
                        compatibility = current
                        normalization = version
                        versions.append({'dataset_id': dataset_id, 'manifest_sha256': manifest['manifest_sha256']})
                        # Re-rank globally; adding per-batch canonical totals
                        # would count entities repeated across batches twice.
                        rows = index.execute('SELECT ' + columns + ",0,json_object('source_product_id',json_extract(payload,'$.source_product_id')) FROM facts ORDER BY sort_key")
                        for row in rows:
                            read += 1
                            if read > backend.limits.max_records:
                                raise DatasetError('dataset_too_large', '跨批次统计超过记录预算，未返回部分统计')
                            if read % 1000 == 0:
                                guard()
                            digest = _hash(row)
                            old = db.execute('SELECT row_hash FROM facts WHERE occurrence=?', (row[0],)).fetchone()
                            if old and old[0] != digest:
                                raise DatasetError('invalid_source', '同一评论出现记录存在不同冻结版本，请核对后再合并统计', reason='overlapping_version_conflict')
                            values = (*row, digest)
                            db.execute('INSERT OR IGNORE INTO facts VALUES(' + ','.join('?' for _ in values) + ')', values)
                db.execute('CREATE INDEX facts_entity ON facts(entity,sort_key)')
                db.execute('UPDATE facts SET canonical=1 WHERE occurrence IN (SELECT occurrence FROM (SELECT occurrence,ROW_NUMBER() OVER(PARTITION BY entity ORDER BY sort_key,occurrence) AS rank FROM facts) WHERE rank=1)')
                result = backend._aggregate_index(db, **arguments)
                guard()
            except sqlite3.OperationalError as exc:
                code = 'dataset_timeout' if time.monotonic() - started > backend.limits.max_duration_seconds else 'dataset_storage_unavailable'
                raise DatasetError(code, '跨批次统计未完成，未返回部分统计',
                                   reason='dataset_sqlite_error', phase='union', sqlite_code=_sqlite_code(exc)) from exc
    return {'contract': CONTRACT, 'dataset_ids': sorted(dataset_ids), 'sources': versions,
            'manifest_sha256': _hash(versions), 'normalization_version': normalization, **result}
