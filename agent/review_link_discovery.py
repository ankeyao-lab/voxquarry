"""Project trusted search hits into bounded product-link candidates."""
import json
import re
from urllib.parse import urlsplit, unquote

from .research_egress import REVIEW_CHANNELS


def candidates(result, admitted_urls, brand, model, auto_channels):
    titles = {}
    def walk(value, depth=0):
        if depth > 12:
            return
        if isinstance(value, dict):
            url = next((value[k] for k in ('url', 'link', 'href') if isinstance(value.get(k), str)), None)
            if url:
                titles[url] = str(value.get('title') or value.get('name') or '')[:1000]
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)
    for block in result.get('content', []):
        text = block.get('text', '') if isinstance(block, dict) else ''
        try:
            walk(json.loads(text))
        except (ValueError, TypeError, RecursionError):
            for title, url in re.findall(r'\[([^\]\n]{0,500})\]\((https?://[^\s<>)]{1,4000})\)', text):
                titles[url] = title
    items = []
    patterns = {'lowes': r'/pd/[^/]+/\d+', 'ace': r'/departments/.+/\d+',
                'ego': r'/[^/]+', 'homedepot': r'/p/[^/]+/\d+', 'walmart': r'/ip/.+/\d+',
                'amazon': r'/(?:[^/]+/)?(?:dp|gp/product)/[A-Z0-9]{10}'}
    for url in admitted_urls:
        parts = urlsplit(url)
        channel = next((key for key, domain in REVIEW_CHANNELS.items() if domain and (parts.hostname == domain or parts.hostname.endswith('.' + domain))), None)
        if not channel or not re.fullmatch(patterns[channel], parts.path.rstrip('/'), re.I):
            continue
        title = titles.get(url, '')
        text = title + ' ' + unquote(parts.path).replace('-', ' ')
        # Match the full model token: LB8800, LB8803 and LB8803-2 are not interchangeable.
        matched = bool(re.search(r'(?<![A-Za-z0-9])' + re.escape(model) + r'(?![A-Za-z0-9-])', title + ' ' + unquote(parts.path), re.I))
        matched = matched and brand.casefold() in text.casefold()
        items.append({'product_url': url, 'channel': channel, 'title': title,
            'identity_status': 'exact_search_match' if matched else 'needs_verification',
            'identity_evidence': 'search_title_or_product_url', 'auto_collectable': channel in auto_channels,
            'next_action': 'resolve_product' if channel in auto_channels else 'external_collection_required'})
    return sorted(items, key=lambda item: (item['identity_status'] != 'exact_search_match', not item['auto_collectable']))[:12]
