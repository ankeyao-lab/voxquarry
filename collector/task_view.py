"""Bounded browser pages over the existing versioned task scope."""
import math
import threading

CHANNELS = dict(lowes="Lowe's", homedepot='Home Depot', ace='Ace Hardware',
    ego='EGO 官网', ryobi='RYOBI 官网', milwaukee='Milwaukee 官网', dewalt='DEWALT 官网',
    einhell='Einhell 官网', harborfreight='Harbor Freight', tractorsupply='Tractor Supply',
    amazon_reviews_2023='Amazon 历史评论', amazon_live='Amazon 实时评论（导入）',
    skil='SKIL 官网', walmart='Walmart', stihl='STIHL 官网', husqvarna='Husqvarna 官网',
    toro='Toro 官网', johndeere='John Deere 官网', exmark='Exmark 官网', greenworks='Greenworks 官网')
STATUSES = dict(running='采集中', completed='已完成', partial_success='部分成功',
    failed='失败', queued='排队中', cancelling='正在移出')
FIELDS = ('channel', 'brand', 'category', 'platform', 'region', 'status', 'model')
LIST_FIELDS = ('id', 'action', 'channel', 'status', 'progress', 'created_at', 'started_at',
    'finished_at', 'target_url', 'brand', 'model', 'category', 'market_region', 'priority',
    'voltage_platform_label', 'voltage_platform_key', 'voltage_platform_status',
    'voltage_platform_id', 'energy_type_id', 'energy_type', 'energy_readiness_status',
    'energy_schema_version', 'energy_canonical_voltage', 'voltage_platform_voltage', 'source_product_id', 'source_product_identity_status',
    'product_master_binding_status', 'product_master_model_id', 'product_master_snapshot_id',
    'attempt_count', 'active_attempt_no', 'active_result_status', 'superseded_by')


def compact(job):
    result = {key: job.get(key) for key in LIST_FIELDS}
    # Match the existing table's legacy energy fallback without transporting
    # the full product observation or master record.
    result['energy_type'] = (job.get('energy_type') or (job.get('product_observation') or {}).get('energy_type')
                             or (job.get('product_master_record') or {}).get('energy_type'))
    reports = [r for r in (job.get('result') or {}).values()
               if isinstance(r, dict) and r.get('unique_reviews') is not None]
    if reports:
        report = reports[0]
        result['result'] = {job['channel']: {key: report.get(key) for key in
            ('unique_reviews', 'source_total', 'status', 'artifacts')}}
        result['result'][job['channel']]['product'] = {
            key: (report.get('product') or {}).get(key) for key in ('total', 'url')}
    return result


class TaskView:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.token = None
        self.rows = []

    def index(self):
        with self.lock:
            first = self.store.list_jobs_page(limit=1)
            if first['scope_token'] == self.token:
                return self.token, self.rows
            rows, page = [], self.store.list_jobs_page(limit=250)
            if page['scope_token'] != first['scope_token']:
                raise ValueError('stable task scope changed; restart pagination')
            while True:
                rows.extend(compact(job) for job in page['items'])
                if not page['has_more']:
                    break
                page = self.store.list_jobs_page(limit=250, cursor=page['next_cursor'])
            if self.store.list_jobs_page(limit=1)['scope_token'] != first['scope_token']:
                raise ValueError('stable task scope changed; restart pagination')
            self.token, self.rows = first['scope_token'], rows
            return self.token, self.rows

    @staticmethod
    def filters(value):
        if not isinstance(value, dict) or set(value) - set(FIELDS):
            raise ValueError('invalid task view filters')
        if any(not isinstance(v, str) or len(v) > 160 for v in value.values()):
            raise ValueError('invalid task view filter value')
        return {k: v.casefold() for k, v in value.items() if v and v != 'all'}

    def select(self, filters, catalog=None):
        filters = self.filters(filters)
        token, rows = self.index()
        platforms = {r['id']: r.get('name_zh') or r.get('name') for r in (catalog or {}).get('records', [])}
        def labels(row):
            return {'channel': CHANNELS.get(row['channel'], row['channel'] or '待确认'),
                'brand': row['brand'] or '未知', 'model': row['model'] or '未知',
                'category': row['category'] or '未知', 'region': row['market_region'] or 'Other',
                'status': STATUSES.get(row['status'], row['status'] or ''),
                'platform': platforms.get(row['voltage_platform_id']) or row['voltage_platform_label'] or '未知'}
        projected = [(r, labels(r)) for r in rows if r.get('target_url') or r.get('result')]
        def matches(row, display, excluded=None):
            return all(key == excluded or needle in (str(display[key]) + ' ' + str(
                row.get('voltage_platform_key') if key == 'platform' else row.get(key) or '')).casefold()
                for key, needle in filters.items())
        selected = [r for r, display in projected if matches(r, display)]
        facets = {key: sorted({display[key] for r, display in projected if matches(r, display, key)})
                  for key in FIELDS}
        return token, selected, facets

    def page(self, page=1, limit=30, filters=None, catalog=None):
        if type(page) is not int or not 1 <= page <= 100000 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid task view page')
        token, selected, facets = self.select({} if filters is None else filters, catalog)
        pages = max(1, math.ceil(len(selected) / limit))
        page = min(page, pages)
        return {'schema_version': 'voxquarry.task-view/v1', 'scope_token': token,
            'items': selected[(page-1)*limit:page*limit], 'total_count': len(selected),
            'page': page, 'page_size': limit, 'page_count': pages, 'facets': facets}

    def queue(self):
        token, rows = self.index()
        return {'schema_version': 'voxquarry.task-queue-view/v1', 'scope_token': token,
            'jobs': [row for row in rows if row['status'] in {'running', 'queued', 'cancelling'}]}

    def summary(self, filters=None, catalog=None):
        token, selected, _ = self.select({} if filters is None else filters, catalog)
        result = self.store.summarize_job_scope([row['id'] for row in selected])
        if self.store.list_jobs_page(limit=1)['scope_token'] != token:
            raise ValueError('stable task scope changed; restart pagination')
        return {**result, 'scope_token': token}
