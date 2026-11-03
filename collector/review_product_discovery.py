"""Bounded agent access to existing collection history and public discovery."""
from concurrent.futures import ThreadPoolExecutor
import re

DOMAINS = {'lowes': 'lowes.com/pd/', 'ace': 'acehardware.com/departments/', 'ego': 'egopowerplus.com'}


def discover(brand, model, channels, *, history, search):
    selected = [channel for channel in channels if channel in DOMAINS]
    items, seen = [], set()
    for row in history(brand, model, selected):
        if (row.get('channel') not in selected or str(row.get('brand', '')).casefold() != brand.casefold()
                or str(row.get('model', '')).casefold() != model.casefold() or not row.get('url')):
            continue
        key = (row['channel'], row['url'])
        if key in seen:
            continue
        seen.add(key)
        items.append({'channel': row['channel'], 'url': row['url'], 'title': row.get('product_title', ''),
                      'brand': brand, 'model': model, 'discovery_source': 'collection_history'})
        if len(items) == 12:
            break
    pending = [channel for channel in selected if not any(item['channel'] == channel for item in items)]
    def one(channel):
        query = f'site:{DOMAINS[channel]} "{brand}" "{model}"'
        try:
            rows = search(query, channel)
        except Exception:
            return [], {'channel': channel, 'code': 'public_product_search_unavailable'}
        found = []
        for row in rows[:6]:
            title = str(row.get('title') or '')
            if not re.search(r'(?<![A-Za-z0-9-])' + re.escape(model) + r'(?![A-Za-z0-9-])', title, re.I):
                continue
            found.append({'channel': channel, 'url': row['url'], 'title': title,
                          'brand': brand, 'model': model, 'discovery_source': 'public_product_index'})
        return found, None
    warnings = []
    if pending:
        # At most the three existing auto-collection channels, each using the
        # established 20-second credential-free search transport. No sitemap
        # recursion, paid fallback, new endpoint, or collection is started here.
        with ThreadPoolExecutor(max_workers=3) as pool:
            for found, warning in pool.map(one, pending):
                items.extend(found)
                if warning:
                    warnings.append(warning)
    return {'brand': brand, 'model': model, 'items': items[:12], 'warnings': warnings,
            'coverage': 'collection_history_and_returned_public_index_results'}
