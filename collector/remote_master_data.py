"""Explicit read-only production authority for isolated local integration."""
import json
import unicodedata
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request, ProxyHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('shared master-data authority must not redirect')


class RemoteMasterData:
    def __init__(self, url):
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid shared master-data authority URL')
        self.url = url.rstrip('/')

    def list(self):
        # Never forward browser identity, credentials or environment HTTP proxies.
        try:
            request = Request(self.url, headers={'Accept': 'application/json'})
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError('oversized authority response')
            data = json.loads(raw)
            for kind in ('brands', 'categories'):
                if not isinstance(data.get(kind), list) or not all(isinstance(row, dict) and isinstance(row.get('name'), str) for row in data[kind]):
                    raise ValueError('invalid authority response')
            return data
        except Exception as exc:
            raise ValueError('生产共享主数据不可达或响应无效；本地禁止回退旧目录') from exc

    def canonical(self, kind, value):
        plural = {'brand': 'brands', 'category': 'categories'}.get(kind)
        if not plural:
            raise ValueError('master data kind must be brand or category')
        normalized = unicodedata.normalize('NFKC', value).strip().casefold()
        rows = [row['name'] for row in self.list()[plural] if unicodedata.normalize('NFKC', row['name']).strip().casefold() == normalized]
        if len(rows) != 1:
            raise ValueError('该值未在生产共享主数据中维护，请通过共享后台维护')
        return rows[0]

    def snapshot(self):
        """Fetch a fresh complete revision; never use the archived local catalog."""
        from .task_master_data import TaskCatalog
        if not self.url.endswith('/api/master-data'):
            raise ValueError('shared authority must use the explicit /api/master-data endpoint')
        url = self.url[:-len('/api/master-data')] + '/api/v2/master-data/snapshot'
        try:
            request = Request(url, headers={'Accept': 'application/json'})
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                etag = response.headers.get('ETag')
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError('oversized authority snapshot')
            data = json.loads(raw)
            TaskCatalog(data)
            if etag != '"' + data['catalog_content_hash'] + '"':
                raise ValueError('authority snapshot ETag mismatch')
            return data
        except Exception as exc:
            raise ValueError('生产共享主数据不可达或快照无效；禁止本地目录回退') from exc
