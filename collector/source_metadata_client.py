"""Bounded, credential-free exact-PDP metadata reads; never collects reviews."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from .source_product_metadata import (
    CHANNEL_METADATA_SOURCES, SourceProductMetadataError,
    parse_source_product_metadata,
)
from .product_metadata import normalize_product_metadata, require_governed_metadata
from .product_identity import build_product_observation, build_voltage_platform, build_task_energy
from .reviewed_product_metadata import reviewed_product_metadata

MAX_SOURCE_BYTES = 4 * 1024 * 1024


class SourceMetadataClient:
    def __init__(self, data_dir, *, transport=None, ttl=300):
        self.data_dir = Path(data_dir)
        self.transport = transport or self._public_get
        self.ttl = ttl
        self.cache = OrderedDict()
        self.lock = threading.RLock()
        self.fetch_slots = threading.BoundedSemaphore(4)

    @staticmethod
    def _validate_url(channel, url):
        parsed = urlsplit(url)
        hosts = CHANNEL_METADATA_SOURCES.get(channel, {}).get('hosts', ())
        host = (parsed.hostname or '').casefold()
        if (parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443}
                or not any(host == h or host.endswith('.' + h) for h in hosts)):
            raise ValueError('商品来源域名或链接不受支持')
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))

    def _public_get(self, channel, url):
        import requests
        started = time.monotonic()
        current = self._validate_url(channel, url)
        # No paid transport, cookie/session reuse, environment proxy, or blind
        # redirects. Only the registered official storefront hosts are allowed.
        for _ in range(3):
            remaining = 12 - (time.monotonic() - started)
            if remaining <= 0:
                raise ValueError('商品页读取超过时间限制')
            session = requests.Session()
            session.trust_env = False
            try:
                response = session.get(current, timeout=(min(3, remaining), min(3, remaining)), allow_redirects=False, stream=True,
                                       headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html,application/json'})
                if response.status_code in {301, 302, 303, 307, 308}:
                    current = self._validate_url(channel, urljoin(current, response.headers.get('Location', '')))
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                # read1 returns available data after one underlying read; a
                # large iter_content chunk can wait forever on a trickle body.
                while True:
                    chunk = response.raw.read1(65536, decode_content=True)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES or time.monotonic() - started > 12:
                        raise ValueError('商品页超过读取大小或时间限制')
                    chunks.append(chunk)
                return b''.join(chunks).decode('utf-8', errors='replace'), current
            finally:
                session.close()
        raise ValueError('商品页面重定向次数超过限制')

    def remember(self, channel, url, source, *, final_url=None):
        """Reuse HTML already obtained by a connector without another request."""
        key = (channel, self._validate_url(channel, url))
        metadata = parse_source_product_metadata(channel, url, source, final_url=final_url or url)
        with self.lock:
            self.cache[key] = (time.monotonic() + self.ttl, deepcopy(metadata))
            self.cache.move_to_end(key)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
        return metadata

    def get(self, channel, url, *, deadline=None):
        key = (channel, self._validate_url(channel, url))
        with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] > time.monotonic():
                return deepcopy(cached[1])
        deadline = deadline if deadline is not None else time.monotonic() + 10
        budget_error = {'source_metadata_error': {'code': 'source_metadata_budget',
                        'message': '本批商品规格读取达到时间上限，请稍后重试未完成的链接'}}
        if not self.fetch_slots.acquire(timeout=max(0, deadline - time.monotonic())):
            return budget_error
        try:
            if time.monotonic() >= deadline:
                return budget_error
            with self.lock:
                cached = self.cache.get(key)
                if cached and cached[0] > time.monotonic():
                    return deepcopy(cached[1])
            try:
                if channel == 'amazon_reviews_2023':
                    match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)', urlsplit(url).path, re.I)
                    if not match:
                        raise ValueError('归档来源需要精确ASIN')
                    path = self.data_dir / 'archive_cache' / channel / 'products' / (match[1].upper() + '.json')
                    if path.is_symlink() or path.stat().st_size > MAX_SOURCE_BYTES:
                        raise ValueError('归档商品元数据无效')
                    source, final_url = path.read_text(), url
                else:
                    source, final_url = self.transport(channel, url)
                return self.remember(channel, url, source, final_url=final_url)
            except SourceProductMetadataError as exc:
                result = {'source_metadata_error': {'code': exc.code, 'message': str(exc)}}
            except Exception:
                # Do not expose response bodies, credentials, proxy URLs or
                # requests internals through the API. URL hints remain marked
                # as such; a failed page read is never official evidence.
                result = {'source_metadata_error': {'code': 'source_metadata_unavailable',
                          'message': '暂时无法读取该商品的公开规格；缺失字段需来源补证据'}}
            if result['source_metadata_error']['code'] in {'source_metadata_unavailable', 'source_metadata_missing'}:
                reviewed = reviewed_product_metadata(channel, url)
                if reviewed:
                    reviewed['source_metadata_evidence']['live_read_error'] = result['source_metadata_error']['code']
                    result = reviewed
            with self.lock:
                self.cache[key] = (time.monotonic() + 30, result)
                while len(self.cache) > 256:
                    self.cache.popitem(last=False)
            return deepcopy(result)
        finally:
            self.fetch_slots.release()

    def get_many(self, targets, *, budget_seconds=10):
        """At most four public metadata reads; review workers stay per-channel serial.

        Queued reads stop at the batch deadline. Running reads have their own
        12-second cap; a large paste cannot multiply the interactive timeout.
        """
        deadline = time.monotonic() + budget_seconds
        def read(target):
            if time.monotonic() >= deadline:
                return {'source_metadata_error': {'code': 'source_metadata_budget',
                        'message': '本批商品规格读取达到时间上限，请稍后重试未完成的链接'}}
            return self.get(*target, deadline=deadline)
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(read, targets))


def merge_source_metadata(hints, source):
    """Source fields outrank URL guesses; retain explicit gaps and provenance."""
    result = dict(hints)
    error = source.get('source_metadata_error')
    if error:
        result['source_metadata_error'] = error
        return result
    for field in ('brand', 'model', 'category', 'energy_type', 'voltage', 'market_region', 'product_title'):
        value = source.get(field)
        if value is not None and value not in ('', 'unknown', 'Unknown Category', 'Other'):
            result[field] = value
    for field in ('brand', 'model', 'category', 'energy_type', 'voltage', 'market_region'):
        if field in source and source[field] is None and field not in hints.get('source_typed_fields', []):
            result[field] = None
    result['source_metadata_evidence'] = source.get('source_metadata_evidence')
    return result


def preserve_source_gaps(metadata, source, protected=()):
    """Do not promote brand-family/URL defaults into proven source fields."""
    protected = set(protected) | set(source.get('source_typed_fields') or [])
    for field in (source.get('source_metadata_evidence') or {}).get('gaps', []):
        if field in protected:
            continue
        if field in {'brand', 'model', 'voltage'}:
            metadata[field] = None
        elif field == 'category':
            metadata[field] = 'Unknown Category'
        elif field == 'energy_type':
            metadata[field] = 'unknown'
    return metadata


def collected_product_context(job, product):
    """Never turn a review product ID or URL-derived title into a model fact."""
    previous = job.get('product_observation') or {}
    manual = set(previous.get('manual_fields') or [])
    if job.get('product_master_model_id') or previous.get('evidence_source') in {'manual_edit', 'request', 'task_metadata_edit', 'user_confirmed', 'governed_external_import', 'product_master_exact_match'}:
        manual.update({'brand', 'model', 'category', 'energy_type', 'voltage'})
    proven = bool(product.get('source_metadata_evidence'))
    prior_proofs = {**(previous.get('retained_source_fields') or {}),
                    **(previous.get('source_metadata_evidence') or {}).get('fields', {})}
    gaps = (product.get('source_metadata_evidence') or {}).get('gaps', [])
    retained = {field: proof for field, proof in prior_proofs.items()
                if field in gaps and field in {'brand', 'model', 'category', 'energy_type', 'voltage'}
                and proof.get('value') and field not in manual
                and (field != 'voltage' or product.get('energy_type') in {None, 'battery', 'other'})}
    protected = manual | set(retained)
    old = {**job, 'voltage': job.get('energy_canonical_voltage')}
    values = {}
    for field in ('brand', 'model', 'category', 'energy_type', 'voltage'):
        value = product.get(field)
        # Typed source fields are usable; raw ids and title guesses are not.
        source_model_gap = (field == 'model' and proven
                            and 'model' in (product.get('source_metadata_evidence') or {}).get('gaps', [])
                            and 'model' not in product.get('source_typed_fields', []))
        if field in protected or (value in (None, '', 'unknown', 'Unknown Category') and not source_model_gap):
            value = old.get(field)
        values[field] = value
    metadata = normalize_product_metadata(
        target_url=job.get('target_url') or product.get('url'),
        product_title=product.get('title') or product.get('product_title'),
        market_region=product.get('market_region') or job.get('market_region'),
        **values,
    )
    preserve_source_gaps(metadata, product, protected)
    if job.get('action', 'scrape') == 'scrape':
        require_governed_metadata(metadata)
    observation = build_product_observation(
        brand=metadata['brand'], model=metadata['model'], category=metadata['category'],
        voltage=metadata['voltage'], energy_type=metadata['energy_type'],
        evidence_source='source_product_page' if proven else previous.get('evidence_source', 'request_or_url'),
    )
    evidence = product.get('source_metadata_evidence') or previous.get('source_metadata_evidence')
    if evidence:
        observation['source_metadata_evidence'] = evidence
    if product.get('source_typed_field_evidence'):
        observation['source_typed_field_evidence'] = deepcopy(product['source_typed_field_evidence'])
    if retained:
        observation['retained_source_fields'] = deepcopy(retained)
    if manual:
        observation['manual_fields'] = sorted(manual)
    platform = build_voltage_platform(observation)
    return {'metadata': metadata, 'product_observation': observation, 'voltage_platform': platform,
            'task_energy': build_task_energy(observation, platform), 'source_verified': proven}
