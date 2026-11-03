"""Authenticated shared-data routes; no user selects a disk path or SQL text."""
from __future__ import annotations

import json
import logging
import socket
import sqlite3
import uuid
import os
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError:  # Pure data contracts also run in the lightweight gate.
    APIRouter = JSONResponse = StreamingResponse = None
    Request = object

from .agent_tools import AgentToolError, authority_from_gateway
from .wenquan_datasets import DatasetError, DatasetLimits, WenquanDatasets, VERSION, _expected_artifacts, _hash, _identity, _json, _normalization_contracts, _scope, _sqlite_code
from .wenquan_catalog import CONTRACT as CATALOG_CONTRACT, Catalogs, entries, _version
from .category_vocabulary import CATEGORY_BY_ALIAS
from . import source


def shared_data_authority(headers, token):
    authority = authority_from_gateway(headers, token)
    if authority.allowed_report_ids is not None:
        raise AgentToolError("shared_data_forbidden", "当前凭据只允许指定报告，不能读取独立原始数据", http_status=403)
    return authority


class LiveSelection(list):
    def __init__(self):
        super().__init__()
        self.unavailable = []


def _collector_scope(scope):
    """Push only exact metadata predicates; never infer row-level scope upstream."""
    result = {k: v for k, v in scope.items() if k in {'brands', 'models', 'channels', 'market_regions'} and v}
    if scope.get('category_ids') or scope.get('categories'):
        ids, names = set(scope.get('category_ids', [])), set(scope.get('categories', []))
        aliases = {alias for alias, definition in CATEGORY_BY_ALIAS.items()
                   if (not ids or definition.category_id in ids) and (not names or definition.display_name in names)}
        if not ids:
            aliases.update(name for name in names if source.category_id_for(name) is None)
        # An unknown category matches nothing, rather than accidentally dropping
        # the predicate and scanning every task. Reporting rechecks exact scope.
        result['categories'] = sorted(aliases) or ['__unknown_research_category__']
    return result


def _source_diagnostic(value):
    if not isinstance(value, dict):
        return {}
    result = DatasetError('invalid_source', '', **{key: value[key] for key in
        ('reason', 'phase', 'field', 'expected', 'observed', 'limit', 'sqlite_code', 'source_reason', 'counts')
        if key in value}).diagnostic
    if isinstance(value.get('error_type'), str) and value['error_type'] in {'OSError', 'FileNotFoundError', 'PermissionError', 'ValueError', 'TypeError',
            'KeyError', 'Error', 'UnicodeError', 'UnicodeDecodeError', 'RecursionError', 'OperationalError', 'IntegrityError'}:
        result['error_type'] = value['error_type']
    return result


class SharedResearchData:
    def __init__(self, service, *, live_root=None, live_catalog=None):
        storage = service.var_root / "wenquan-datasets"
        storage.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Sibling staging and output directories preserve source/output
        # separation while avoiding writes to the shared runtime parent.
        info = storage.stat()
        if storage.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise DatasetError('unsafe_path', '原评工作目录必须由服务用户独占')
        self.workspace = storage.resolve(strict=True)
        storage = storage / 'datasets'
        self.snapshot = WenquanDatasets(service.snapshot_root, storage,
            limits=DatasetLimits(page_bytes=800_000))
        self.live = WenquanDatasets(live_root, storage, limits=DatasetLimits(page_bytes=800_000)) if live_root else None
        self.live_catalog = live_catalog
        self._opening = threading.BoundedSemaphore(2)
        self._analyzing = threading.BoundedSemaphore(1)
        self._cataloging = threading.BoundedSemaphore(1)
        self.catalogs = Catalogs(self.snapshot)

    def _iter_live_runs(self, job_ids=None, scope=None, *, metadata_only=False):
        if self.live is None or self.live_catalog is None:
            return
        count = 0
        requested = set(job_ids or [])
        scoped = sorted(requested) if hasattr(self.live_catalog, 'for_job') else []
        batched = bool(scoped and hasattr(self.live_catalog, 'for_jobs'))
        groups = [scoped[n:n + 200] for n in range(0, len(scoped), 200)] if batched else [[job] for job in scoped] or [[]]
        base_filters = _collector_scope(scope or {})
        started = time.monotonic()
        for group in groups:
            cursor = None
            filters = {**base_filters, **({'job_ids': group} if batched else {})}
            for _ in range(2000):
                if time.monotonic() - started > 240:
                    raise DatasetError('wotu_catalog_timeout', '资料目录读取超过本次时限，未缩小范围', 503,
                                       reason='catalog_total_deadline', observed=count)
                if metadata_only and hasattr(self.live_catalog, 'metadata'):
                    page = self.live_catalog.metadata(cursor, group[0] if group and not batched else None, filters)
                elif batched:
                    page = self.live_catalog.for_jobs(cursor, group, base_filters)
                elif group:
                    page = self.live_catalog.for_job(group[0])
                elif filters and hasattr(self.live_catalog, 'for_scope'):
                    page = self.live_catalog.for_scope(cursor, filters)
                else:
                    page = self.live_catalog(cursor)
                if page.get("schema_version") != "voxquarry.research-collection/v1" or not isinstance(page.get("items"), list):
                    raise DatasetError("invalid_catalog", "数据采集资料目录版本不兼容", 503)
                for item in page.get("items", []):
                    if requested and item.get('job_id') not in requested:
                        continue
                    if item.get('scope_job') and not self.live._matches_job(scope or {}, item['scope_job']):
                        continue
                    count += 1
                    if count > self.live.limits.max_runs:
                        raise DatasetError('catalog_too_large', '资料目录超过本次读取容量，未缩小范围')
                    data = item.get("data") or {}
                    if data.get('ready') is not True and data.get('verification') != 'deferred' and item.get('status') in {'completed', 'partial_success'}:
                        yield None, {'job_id': item['job_id'], 'channel': item.get('channel'),
                            'reason': data.get('reason', 'artifact_unavailable'), 'available': False,
                            'diagnostic': _source_diagnostic(data.get('diagnostic'))}
                    deferred = metadata_only and data.get("verification") == "deferred"
                    if data.get("ready") is True or deferred:
                        selected = data.get("selected_run")
                        if not isinstance(selected, dict) or not isinstance(selected.get("job"), dict) or not isinstance(selected.get("run_dir"), str):
                            raise DatasetError("invalid_catalog", "数据采集资料版本标识不完整", 503)
                        if selected["job"].get("id") != item.get("job_id") or type(item.get("attempt_no")) is not int or selected["job"].get("active_attempt_no") != item["attempt_no"]:
                            raise DatasetError("invalid_catalog", "数据采集资料与采集次数不一致", 503)
                        if not deferred and not _expected_artifacts(data.get("artifacts")):
                            raise DatasetError("invalid_catalog", "数据采集资料缺少工件哈希", 503)
                        yield {**selected, "expected_artifacts": data.get("artifacts")}, None
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            else:
                raise DatasetError("catalog_too_large", "资料目录超过本次读取容量")

    def _live_runs(self, job_ids=None, scope=None, *, metadata_only=False):
        result = LiveSelection()
        for selected, missing in self._iter_live_runs(job_ids, scope, metadata_only=metadata_only):
            if selected:
                result.append(selected)
            if missing:
                result.unavailable.append(missing)
        return result

    def _live_entries(self, scope):
        for selected, missing in self._iter_live_runs(scope.get('job_ids'), scope, metadata_only=True):
            if selected:
                yield from entries(self.live, scope, [selected])
            elif missing:
                yield missing, None, None, None

    def _catalog(self, scope, origin='auto'):
        if not self._cataloging.acquire(blocking=False):
            raise DatasetError('catalog_busy', '已有目录正在准备，请稍后重试', 429)
        try:
            sources = []
            if origin != 'wotu':
                sources.append(('snapshot', entries(self.snapshot, scope)))
            if origin != 'snapshot' and self.live and self.live_catalog:
                sources.append(('wotu', self._live_entries(scope)))
            elif origin == 'wotu':
                raise DatasetError('source_unavailable', '数据采集原始数据接口尚不可用', 503)
            return self.catalogs.build(scope, sources, origin=origin)
        finally:
            self._cataloging.release()

    def catalog(self, scope=None, *, cursor=None, limit=50):
        if cursor is not None:
            return self.catalogs.page(scope=scope, cursor=cursor, limit=limit)
        return self.catalogs.page(self._catalog(_scope(scope)), limit=limit)

    def open(self, scope=None, *, origin="auto", catalog_id=None, batch_id=None, _check_active=None):
        if not isinstance(origin, str) or origin not in {"auto", "snapshot", "wotu"}:
            raise DatasetError("invalid_request", "数据来源无效", 422)
        if not self._opening.acquire(blocking=False):
            raise DatasetError("dataset_busy", "已有资料正在准备，请稍后重试", 429)
        try:
            if _check_active:
                _check_active()
            if bool(catalog_id) != bool(batch_id):
                raise DatasetError('invalid_request', '目录版本和批次须同时提供', 422)
            if not catalog_id:
                scope = _scope(scope)
                catalog_id = self._catalog(scope, origin)
                page = self.catalogs.page(catalog_id)
                if scope.get('job_ids') and page['unavailable_count']:
                    raise DatasetError('source_unavailable', '部分指定任务不可用，未缩小范围；请从目录核对缺口并选择可用批次',
                                       reason='requested_sources_unavailable', observed=page['unavailable_count'])
                if page['batch_count'] != 1:
                    if not page['batch_count']:
                        raise DatasetError('source_unavailable', '所选范围没有可用原文', reason='no_available_sources')
                    return {**page, 'status': 'requires_batches',
                            'instruction': '按此目录的批次逐组打开；有 next_cursor 时继续枚举同一目录。原范围及缺口保持不变。'}
                batch_id = page['batches'][0]['batch_id'] if page['batches'] else self._first_batch(catalog_id)
            info, rows = self.catalogs.batch(catalog_id, batch_id)
            if scope is not None and _scope(scope) != info['scope']:
                raise DatasetError('invalid_request', '批次必须保留原目录范围', 422)
            if origin != 'auto' and origin != info['origin']:
                raise DatasetError('invalid_request', '批次不能改变来源', 422)
            snapshot_ids = [p['job_id'] for name, p, _ in rows if name == 'snapshot']
            live_ids = [p['job_id'] for name, p, _ in rows if name == 'wotu']
            snapshot_runs = list(self.snapshot._iter_jobs(job_ids=snapshot_ids)) if snapshot_ids else []
            live_runs = self._live_runs(live_ids, info['scope']) if live_ids else LiveSelection()
            selected = {'snapshot': {r['job']['id']: r for r in snapshot_runs},
                        'wotu': {r['job']['id']: r for r in live_runs}}
            self._validate_batch(rows, selected)
            if _check_active:
                _check_active()
            reused = self._prepared(catalog_id, batch_id)
            if reused is not None:
                return {**reused, 'preparation_reused': True}
            selected_scope = {**info['scope'], 'job_ids': sorted(p['job_id'] for _, p, _ in rows)}
            if snapshot_runs and live_runs:
                result = self._combined_open(selected_scope, live_runs, snapshot_runs=snapshot_runs, check_active=_check_active)
            else:
                backend, runs = (self.live, live_runs) if live_runs else (self.snapshot, snapshot_runs)
                result = backend.open(selected_scope, selected_runs=runs, check_active=_check_active)
            self._validate_batch(rows, selected)
            result = self._with_coverage(result, live_runs, selected_scope)
            # Opening a batch proves that batch only. Overall completeness is
            # derived from the bound directory and all successful batch receipts.
            result['catalog_binding'] = {k: info[k] for k in ('catalog_id', 'scope', 'origin', 'total', 'total_sources', 'unavailable_count', 'batch_count')}
            result['catalog_binding'].update(batch_id=batch_id, job_ids=selected_scope['job_ids'])
            if info['batch_count'] == 1 and info['unavailable_count']:
                result['source_coverage']['complete'] = False
                result['source_coverage']['unavailable_sources'] = self.catalogs.gaps(catalog_id)
                result['source_coverage']['unavailable_count'] = info['unavailable_count']
            self._remember_prepared(catalog_id, batch_id, result)
            return result
        except DatasetError:
            raise
        except source.MarketContractError as exc:
            raise DatasetError("invalid_source", "资料契约校验失败", reason="review_contract_invalid") from exc
        except OSError as exc:
            raise DatasetError("dataset_storage_unavailable", "资料物化未完成，请检查可用空间") from exc
        finally:
            self._opening.release()

    def _receipt_name(self, catalog_id, batch_id):
        return 'prepared-' + _hash([catalog_id, batch_id, VERSION, _normalization_contracts(),
                                   self.snapshot.limits.shard_bytes])[7:] + '.json'

    def _prepared(self, catalog_id, batch_id):
        """Read only receipts for a proven frozen directory and current writer."""
        name = self._receipt_name(catalog_id, batch_id)
        try:
            with self.snapshot._read(self.snapshot.storage_root, name, self.snapshot.limits.max_metadata_bytes) as (handle, _):
                receipt = json.load(handle)
        except DatasetError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return None
            raise
        except (ValueError, UnicodeError) as exc:
            raise DatasetError('dataset_changed', '准备回执损坏，未复用数据集', reason='preparation_receipt_invalid') from exc
        if (not isinstance(receipt, dict) or receipt.get('catalog_id') != catalog_id
                or receipt.get('batch_id') != batch_id
                or receipt.get('receipt_sha256') != _hash({key: value for key, value in receipt.items() if key != 'receipt_sha256'})):
            raise DatasetError('dataset_changed', '准备回执与目录批次不一致', reason='preparation_receipt_invalid')
        manifest = self.snapshot.manifest(receipt.get('dataset_id'))
        if manifest['manifest_sha256'] != receipt.get('manifest_sha256'):
            raise DatasetError('dataset_changed', '准备回执与冻结清单不一致', reason='preparation_receipt_invalid')
        binding = receipt.get('catalog_binding') or {}
        if (binding.get('catalog_id') != catalog_id or binding.get('batch_id') != batch_id
                or set(binding.get('job_ids', [])) != {item['job']['id'] for item in manifest['inputs']}):
            raise DatasetError('dataset_changed', '准备回执与任务集合不一致', reason='preparation_receipt_invalid')
        return {**manifest, 'catalog_binding': receipt['catalog_binding'], 'source_coverage': receipt['source_coverage']}

    def _remember_prepared(self, catalog_id, batch_id, result):
        receipt = {key: result[key] for key in ('dataset_id', 'manifest_sha256', 'catalog_binding', 'source_coverage')}
        receipt.update(catalog_id=catalog_id, batch_id=batch_id)
        receipt['receipt_sha256'] = _hash(receipt)
        encoded = _json(receipt).encode()
        with tempfile.NamedTemporaryFile(prefix='.prepared-', dir=self.snapshot.storage_root, delete=False) as handle:
            path = Path(handle.name)
            try:
                handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
                path.chmod(0o400)
                os.replace(path, self.snapshot.storage_root / self._receipt_name(catalog_id, batch_id))
                (self.snapshot.storage_root / self._failure_name(catalog_id, batch_id)).unlink(missing_ok=True)
            finally:
                path.unlink(missing_ok=True)

    def _failure_name(self, catalog_id, batch_id):
        return self._receipt_name(catalog_id, batch_id).replace('.json', '-failure.json')

    def _preparation_failure(self, catalog_id, batch_id, failure=None):
        name = self._failure_name(catalog_id, batch_id)
        if failure is not None:
            failure = {**failure, 'receipt_sha256': _hash(failure)}
            with tempfile.NamedTemporaryFile(prefix='.prepared-', dir=self.snapshot.storage_root, delete=False) as handle:
                path = Path(handle.name)
                try:
                    handle.write(_json(failure).encode()); handle.flush(); os.fsync(handle.fileno())
                    path.chmod(0o400)
                    os.replace(path, self.snapshot.storage_root / name)
                finally:
                    path.unlink(missing_ok=True)
            return {key: value for key, value in failure.items() if key != 'receipt_sha256'}
        try:
            with self.snapshot._read(self.snapshot.storage_root, name, 64_000) as (handle, _):
                result = json.load(handle)
            if (not isinstance(result, dict) or result.get('batch_id') != batch_id
                    or result.get('receipt_sha256') != _hash({key: value for key, value in result.items() if key != 'receipt_sha256'})):
                raise ValueError()
            return {key: value for key, value in result.items() if key != 'receipt_sha256'}
        except DatasetError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return None
            raise
        except (ValueError, UnicodeError) as exc:
            raise DatasetError('dataset_changed', '批次失败回执校验失败', reason='preparation_receipt_invalid') from exc

    def prepare(self, scope=None, *, origin='auto', catalog_id=None, max_batches=4):
        """Prepare bounded compatible batches, with durable successes and gaps.

        This selects no research method: it fulfills the caller's unchanged data
        scope. Failed batches are durable gaps, not an automatic retry loop;
        callers can explicitly open the exact batch after resolving its cause.
        """
        started = time.monotonic()
        if type(max_batches) is not int or not 1 <= max_batches <= 8:
            raise DatasetError('invalid_request', '每次准备批次数应为 1 至 8', 422)
        if origin not in {'auto', 'snapshot', 'wotu'}:
            raise DatasetError('invalid_request', '数据来源无效', 422)
        if catalog_id is None:
            catalog_id = self._catalog(_scope(scope), origin)
        with self.catalogs.read(catalog_id) as (db, info):
            if scope is not None and _scope(scope) != info['scope']:
                raise DatasetError('invalid_request', '准备批次不能改变原目录范围', 422)
            if origin != 'auto' and origin != info['origin']:
                raise DatasetError('invalid_request', '准备批次不能改变来源', 422)
            batches = [json.loads(row[0]) for row in db.execute('SELECT payload FROM batches ORDER BY batch_id')]
        datasets, pending, failed, prepared_jobs = [], [], [], set()
        attempted = 0
        def check_active():
            if time.monotonic() - started >= 180:
                raise DatasetError('preparation_yield', '本次准备时间已用完，已完成批次可复用', reason='preparation_deadline')
        for batch in batches:
            batch_id = batch['batch_id']
            # A receipt is already bound to verified immutable data. Continuing
            # that directory is allowed after live source files move; a fresh
            # open still checks current source versions before reusing it.
            result = self._prepared(catalog_id, batch_id)
            if result is None:
                failure = self._preparation_failure(catalog_id, batch_id)
                if failure is not None:
                    failed.append(failure)
                    continue
                if attempted >= max_batches or time.monotonic() - started >= 180:
                    pending.append(batch_id)
                    continue
                attempted += 1
                try:
                    result = self.open(catalog_id=catalog_id, batch_id=batch_id, _check_active=check_active)
                except DatasetError as exc:
                    if exc.code == 'preparation_yield':
                        pending.append(batch_id)
                        continue
                    failure = {'batch_id': batch_id, 'job_ids': batch['job_ids'],
                               'code': exc.code, 'diagnostic': exc.diagnostic}
                    failed.append(self._preparation_failure(catalog_id, batch_id, failure))
                    continue
            prepared_jobs.update(result['catalog_binding']['job_ids'])
            coverage = result['source_coverage']
            datasets.append({**{key: result[key] for key in ('dataset_id', 'manifest_sha256', 'version',
                'normalization_version', 'record_counts', 'source_complete', 'export_complete')},
                'batch_id': batch_id, 'source_coverage': {'complete': coverage['complete'],
                    'unavailable_count': coverage.get('unavailable_count', len(coverage.get('unavailable_sources', [])))}})
        complete = not pending and not failed and info['unavailable_count'] == 0
        result = {'contract': 'voxquarry.wenquan-preparation/v1', 'status': 'ready' if complete else 'incomplete',
                'catalog_id': catalog_id, 'scope': info['scope'], 'origin': info['origin'], 'datasets': datasets,
                'catalog_binding': {key: info[key] for key in ('catalog_id', 'scope', 'origin', 'total',
                    'total_sources', 'unavailable_count', 'batch_count')},
                'coverage': {'requested_sources': info['total_sources'], 'available_sources': info['total'],
                    'prepared_sources': len(prepared_jobs), 'unavailable_count': info['unavailable_count'],
                    'total_batches': info['batch_count'], 'prepared_batches': len(datasets), 'complete': complete},
                'unavailable_sources': self.catalogs.gaps(catalog_id), 'failed_batches': failed,
                'pending_batch_ids': pending, 'next': {'catalog_id': catalog_id} if pending else None,
                'preparation': {'attempted_batches': attempted},
                'instruction': '各数据集保留原目录范围；跨批统计须按 review_entity_id 去重。complete 仅表示所选目录已全部准备，不表示来源采集无缺失。'}
        size = len(_json(result).encode())
        if size > 800_000:
            raise DatasetError('preparation_response_too_large', '准备回执超过响应容量，已完成数据集仍保留；请读取原目录批次',
                               reason='preparation_receipt_bytes', observed=size, limit=800_000)
        return result

    def _first_batch(self, catalog_id):
        with self.catalogs.read(catalog_id) as (db, _):
            return db.execute('SELECT batch_id FROM batches ORDER BY batch_id LIMIT 1').fetchone()[0]

    def _validate_batch(self, rows, selected):
        for name, public, private in rows:
            backend = self.snapshot if name == 'snapshot' else self.live
            item = selected[name].get(public['job_id'])
            if item is None:
                raise DatasetError('source_changed', '目录来源已不可用，请按原范围重新获取目录',
                                   reason='catalog_source_unavailable', job_id=public['job_id'])
            run = backend._runs({'job_ids': [public['job_id']]}, [item])[0]
            if _version(backend, item, run) != private['version']:
                raise DatasetError('source_changed', '来源版本已变化，请按原范围重新获取目录',
                                   reason='catalog_source_version_changed', job_id=public['job_id'])

    @staticmethod
    def _with_coverage(result, runs, scope):
        opened = {item.get('job', {}).get('id') for item in result.get('inputs', [])}
        unavailable = list({item['job_id']: item for item in [
            *result.get('source_coverage', {}).get('unavailable_sources', []),
            *getattr(runs, 'unavailable', [])]}.values())
        return {**result, 'source_coverage': {'scope': scope,
            'opened_job_ids': sorted(value for value in opened if value),
            'unavailable_sources': unavailable, 'complete': not unavailable}}

    def _combined_open(self, scope, live_runs, *, snapshot_runs=None, check_active=None):
        """Freeze a union of trusted roots before running one shared normalizer.

        Temporary source paths are generated here, never accepted over HTTP.
        Each origin's package contract and collector hashes are retained.
        """
        requested = set(scope.get("job_ids", []))
        available_scope = {k: v for k, v in scope.items() if k != "job_ids"}
        live_ids = {item["job"]["id"] for item in live_runs}
        # Discover available historical files before exact freezing. Missing
        # archived manifests remain explicit coverage gaps; unsafe paths and
        # changed versions still fail closed in catalog() and _runs().
        unavailable = []
        if snapshot_runs is None:
            raise DatasetError('invalid_request', '合并读取需要已核对的目录批次', 422)
        live_runs = [item for item in live_runs if not requested or item["job"]["id"] in requested]
        groups = [("snapshot", self.snapshot, snapshot_runs), ("wotu", self.live, live_runs)]
        prepared = [(name, backend, backend._runs(available_scope, selected)) for name, backend, selected in groups if selected]
        chosen_ids = {run["job"]["id"] for _, _, runs in prepared for run in runs}
        if requested - chosen_ids:
            raise DatasetError("source_unavailable", "部分指定任务没有可用原文，未缩小范围")
        if len(chosen_ids) > self.snapshot.limits.max_runs:
            raise DatasetError("dataset_too_large", "选中任务超过本次容量")
        started, total, identities, selected = time.monotonic(), 0, [], []
        def guard():
            if check_active:
                check_active()
            if time.monotonic() - started > self.snapshot.limits.max_duration_seconds:
                raise DatasetError("dataset_timeout", "资料物化超过本次时间")
        with tempfile.TemporaryDirectory(prefix=".wenquan-combined-", dir=self.workspace) as directory:
            stage = Path(directory)
            for origin, backend, runs in prepared:
                for run in runs:
                    guard()
                    relative = Path(origin) / run["relative"]
                    names = ["validation_report.json", "reviews.csv"]
                    raw = run["relative"] / "reviews.raw.jsonl"
                    if run["expected_artifacts"] or (backend.source_root / raw).exists() or (backend.source_root / raw).is_symlink():
                        names.append("reviews.raw.jsonl")
                    evidence = []
                    for name in names:
                        info = backend._copy(run["relative"] / name, stage / relative / name,
                            self.snapshot.limits.max_source_bytes - total, check_active=guard)
                        total += info["bytes"]
                        expected = run["expected_artifacts"].get(name)
                        if expected and expected != {"sha256": info["sha256"], "bytes": info["bytes"]}:
                            raise DatasetError("source_changed", "采集工件版本已经变化")
                        if name == "validation_report.json" and info["sha256"] != run["report_hash"]:
                            raise DatasetError("source_changed", "来源目录与导出版本不一致")
                        evidence.append({"name": name, "sha256": info["sha256"], "bytes": info["bytes"]})
                        identities.append((backend, info))
                    run["csv_path"] = stage / relative / "reviews.csv"
                    run["market_contract_mode"] = source._run_contract_mode(run)
                    selected.append({"job": run["job"], "run_dir": relative,
                        **({"expected_artifacts": evidence} if len(evidence) == 3 else {})})
                if (backend.source_root / "snapshot-manifest.json").exists():
                    backend._copy(Path("snapshot-manifest.json"), stage / origin / "snapshot-manifest.json", backend.limits.max_metadata_bytes)
                    source._enforce_snapshot_package_contract(stage / origin, runs)
            for backend, info in identities:
                with backend._read(backend.source_root, info["path"], backend.limits.max_source_bytes) as (_, current):
                    if _identity(current) != info["identity"]:
                        raise DatasetError("source_changed", "合并导出期间来源发生变化")
            combined = WenquanDatasets(stage, self.snapshot.storage_root, limits=self.snapshot.limits)
            result = combined.open(scope, selected_runs=selected, check_active=guard)
            return {**result, 'source_coverage': {'unavailable_sources': unavailable}}

    def invoke(self, operation, payload):
        contracts = {
            "catalog": ({"scope", "cursor", "limit", "catalog_contract"}, set()),
            "open": ({"scope", "origin", "catalog_id", "batch_id", "catalog_contract"}, set()),
            "prepare": ({"scope", "origin", "catalog_id", "max_batches", "catalog_contract"}, set()),
            "manifest": ({"dataset_id"}, {"dataset_id"}),
            "query": ({"dataset_id", "filters", "query", "review_ids", "population", "offset", "limit", "cursor"}, {"dataset_id"}),
            "aggregate": ({"dataset_id", "dataset_ids", "group_by", "filters", "query", "population"}, set()),
            "export": ({"dataset_id", "shard", "source_type"}, {"dataset_id"}),
            "definition": ({"category_id"}, {"category_id"}),
            "analyze": ({"dataset_id", "definition_id"}, {"dataset_id"}),
        }
        if operation not in contracts:
            raise DatasetError("operation_not_found", "没有此数据操作", 404)
        allowed, required = contracts[operation]
        if not isinstance(payload, dict) or set(payload) - allowed or required - set(payload):
            raise DatasetError("invalid_request", "数据请求参数无效", 422)
        if operation == 'aggregate':
            if ('dataset_id' in payload) == ('dataset_ids' in payload):
                raise DatasetError('invalid_request', '统计须选择单个或一组数据集', 422)
            if 'dataset_ids' in payload:
                from .wenquan_union import aggregate_union
                if not self._analyzing.acquire(blocking=False):
                    raise DatasetError('analysis_busy', '已有分析正在运行，请稍后重试', 429)
                try:
                    return aggregate_union(self.snapshot, **payload)
                finally:
                    self._analyzing.release()
        if operation == "definition":
            from .research_definition import get_research_definition
            return get_research_definition(**payload)
        if operation == "analyze":
            from .dataset_analysis import analyze_dataset
            if not self._analyzing.acquire(blocking=False):
                raise DatasetError("analysis_busy", "已有规则分析正在运行，请稍后重试", 429)
            try:
                return analyze_dataset(self.snapshot, **payload)
            finally:
                self._analyzing.release()
        if operation in {"catalog", "open", "prepare"}:
            arguments = dict(payload)
            contract = arguments.pop('catalog_contract', None)
            if contract not in {None, CATALOG_CONTRACT}:
                raise DatasetError('invalid_request', '原始数据目录版本不受支持', 422)
            result = getattr(self, operation)(**arguments)
            if contract is None and (result.get('next_cursor') or result.get('status') == 'requires_batches'):
                raise DatasetError('catalog_client_upgrade_required', '此范围需要分页或分批读取，请更新AI 研究数据客户端；未返回部分范围')
            return result
        return getattr(self.snapshot, operation)(**payload)


def collector_catalog_from_environment():
    base = os.environ.get("VOXQUARRY_RESEARCH_COLLECTOR_URL", "").rstrip("/")
    token = os.environ.get("VOXQUARRY_RESEARCH_BRIDGE_TOKEN", "")
    if not base or not token:
        return None
    # Only fixed paths, an issued cursor and validated structured selectors.
    from urllib.parse import urlencode, urlsplit
    if urlsplit(base).scheme != "http" or not urlsplit(base).hostname or urlsplit(base).username or urlsplit(base).password:
        raise ValueError("invalid trusted collector URL")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            raise ValueError("trusted collector must not redirect")
    opener = urllib.request.build_opener(NoRedirect())
    def catalog(cursor, job_id=None, filters=None, *, verify=True):
        request_id = uuid.uuid4().hex
        def failure(code, message, status):
            error = DatasetError(code, message, status)
            error.diagnostic = {'request_id': request_id, 'verify': verify, 'continued_page': bool(cursor)}
            return error
        request = urllib.request.Request(base + "/api/research/tasks?" + urlencode({"limit": 5 if verify else 50, "verify": str(verify).lower(), **({"cursor": cursor} if cursor else {}), **({'job_id': job_id} if job_id else {}), **({'filters': json.dumps(filters)} if filters else {})}),
            headers={"Authorization": "Bearer " + token, "X-Request-ID": request_id})
        started = time.monotonic()
        status, size = None, 0
        try:
            with opener.open(request, timeout=20) as response:
                status = response.status
                data = response.read(8_000_001)
                size = len(data)
            if size > 8_000_000:
                raise failure('wotu_catalog_too_large', '数据采集单页目录超过读取容量', 502)
            try:
                value = json.loads(data)
            except (ValueError, UnicodeError) as exc:
                raise failure('wotu_catalog_invalid', '数据采集目录响应格式无效', 502) from exc
            if not isinstance(value, dict):
                raise failure('wotu_catalog_invalid', '数据采集目录响应形状无效', 502)
            return value
        except urllib.error.HTTPError as exc:
            status = exc.code
            code = 'wotu_catalog_authorization' if exc.code in {401, 403} else 'wotu_catalog_http_error'
            raise failure(code, '数据采集目录服务返回错误（HTTP ' + str(exc.code) + '）', 503) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise failure('wotu_catalog_timeout', '数据采集目录单页读取超时', 503) from exc
        except urllib.error.URLError as exc:
            code = 'wotu_catalog_timeout' if isinstance(exc.reason, (TimeoutError, socket.timeout)) else 'wotu_unavailable'
            raise failure(code, '数据采集目录连接暂时不可用', 503) from exc
        except OSError as exc:
            raise failure('wotu_unavailable', '数据采集目录连接暂时不可用', 503) from exc
        finally:
            logging.getLogger(__name__).info('wotu_catalog request_id=%s verify=%s cursor=%s status=%s bytes=%s seconds=%.3f',
                request_id, verify, bool(cursor), status, size, time.monotonic() - started)
    catalog.metadata = lambda cursor, job_id=None, filters=None: catalog(cursor, job_id, filters, verify=False)
    catalog.for_job = lambda job_id: catalog(None, job_id)
    catalog.for_jobs = lambda cursor, job_ids, filters=None: catalog(cursor, filters={**(filters or {}), 'job_ids': job_ids})
    catalog.for_scope = lambda cursor, filters: catalog(cursor, filters=filters)
    return catalog


def research_data_router(service):
    if APIRouter is None:
        raise RuntimeError("FastAPI is required to mount research data routes")
    router = APIRouter()
    backend = None
    lock = threading.Lock()
    def cache_health():
        result = WenquanDatasets.cache_health(service.var_root / "wenquan-datasets" / "datasets")
        if backend is not None:
            result['snapshot_queries'] = backend.snapshot.query_metrics()
            if backend.live is not None:
                result['live_queries'] = backend.live.query_metrics()
        return result
    service.wenquan_cache_health = cache_health
    def data():
        nonlocal backend
        with lock:
            if backend is None:
                backend = SharedResearchData(service, live_root=os.environ.get("VOXQUARRY_WENQUAN_WOTU_SOURCE_ROOT") or None,
                    live_catalog=collector_catalog_from_environment())
        return backend

    @router.post("/reporting/api/v1/research-data/{operation}")
    def invoke(operation: str, payload: dict, request: Request):
        try:
            shared_data_authority(request.headers, os.environ.get("VOXQUARRY_AGENT_GATEWAY_TOKEN"))
            result = data().invoke(operation, payload)
            if operation == "export":
                return StreamingResponse(result, media_type="application/x-ndjson", headers={"X-Content-Type-Options": "nosniff"})
            return JSONResponse(content=result)
        except (AgentToolError, DatasetError) as exc:
            return JSONResponse(status_code=exc.http_status, content={"error": {"code": exc.code, "message": exc.message,
                "diagnostic": getattr(exc, "diagnostic", {}), "retryable": exc.code in {"dataset_busy", "analysis_busy", "catalog_busy", "catalog_unavailable", "wotu_catalog_timeout", "wotu_catalog_http_error", "wotu_unavailable"}}})
        except OSError:
            return JSONResponse(status_code=503, content={"error": {"code": "dataset_storage_unavailable",
                "message": "原评工作目录不可用，请检查目录权限和可用空间。", "retryable": False}})
        except sqlite3.Error as exc:
            error = DatasetError('dataset_storage_unavailable', '数据索引读取未完成，未返回部分结果', 503,
                reason='dataset_sqlite_error', phase={'catalog': 'catalog', 'prepare': 'preparation',
                    'query': 'query', 'aggregate': 'aggregate'}.get(operation, 'freeze'), sqlite_code=_sqlite_code(exc))
            return JSONResponse(status_code=503, content={'error': {'code': error.code, 'message': error.message,
                'diagnostic': error.diagnostic, 'retryable': False}})
    return router
