"""Small, dated exact-product facts verified during connector incident repair.

These records are explicitly reviewed facts, not raw HTML or live-read claims.
Fresh exact-PDP metadata takes priority. Identity conflicts never use fallback.
"""
from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import urlsplit


KOBALT_URL = 'https://www.lowes.com/pd/Kobalt-48-volt-21-in-Self-propelled-Battery-Walk-Mower-2-Batteries-Included/5019587973'
KOBALT_SPRAYER_URL = 'https://www.lowes.com/pd/Kobalt-Kobalt-24V-Chemical-Sprayer-Kit/1002981480'
EGO_880_KIT_URL = 'https://www.lowes.com/pd/EGO-880CFM-Blower-Kit-2-4Ah-battery-320W-Charger/5015278625'


def _ego_880_kit_metadata(url):
    # Exact official PDP specifications verified 2026-09-18. Model includes
    # the kit suffix; neither neighboring SKUs nor URL capacity hints qualify.
    facts = {
        'brand': 'EGO', 'model': 'LB8803-2', 'category': 'Leaf Blower/Vacuum',
        'energy_type': 'battery', 'voltage': '56V', 'market_region': 'North America',
        'product_title': 'EGO POWER+ 56-volt 880-CFM 200-MPH Battery Handheld Leaf Blower, two 4.0Ah batteries and 320W charger',
    }
    sources = {
        'brand': ('brand', 'EGO'), 'model': ('model_number', 'LB8803-2'),
        'category': ('specifications.type', 'Handheld leaf blower'),
        'energy_type': ('specifications.power_source', 'Battery'),
        'voltage': ('specifications.battery_voltage', '56-volt'),
        'market_region': ('storefront', 'lowes.com US'),
        'product_title': ('product_title_summary', facts['product_title']),
    }
    record = {'source_url': EGO_880_KIT_URL, 'observed_on': '2026-09-18',
              'facts': facts, 'sources': sources}
    return {**facts, 'source_metadata_evidence': {
        'schema_version': 'voxquarry.source-product-metadata/v1',
        'channel': 'lowes', 'source_url': url, 'final_url': EGO_880_KIT_URL,
        'source_sha256': hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest(),
        'digest_scope': 'reviewed_fact_record', 'identity': 'reviewed_exact_product_id',
        'source_product_id': '5015278625', 'observed_on': '2026-09-18',
        'fields': {key: {'source': 'reviewed_pdp.' + sources[key][0],
                         'raw': sources[key][1], 'value': value} for key, value in facts.items()},
        'battery_count': 2, 'battery_ecosystem_voltage': '56V', 'system_voltages': ['56V'], 'gaps': [],
    }}


def _kobalt_sprayer_metadata(url):
    # Exact official PDP verified 2026-09-14. These are dated facts, not HTML.
    facts = {
        'brand': 'Kobalt', 'model': 'KSP 1124A-03', 'category': 'Sprayer',
        'energy_type': 'battery', 'voltage': '24V', 'market_region': 'North America',
        'product_title': 'Kobalt 2.11-Gallon 24-Volt Battery Operated Handheld Sprayer',
    }
    sources = {
        'brand': ('brand', 'Kobalt'), 'model': ('model_number', 'KSP 1124A-03'),
        'category': ('specifications.type', 'Handheld sprayer'),
        'energy_type': ('specifications.battery_operated', 'Yes'),
        'voltage': ('specifications.battery_voltage', '24-volt'),
        'market_region': ('storefront', 'lowes.com US'),
        'product_title': ('product_title', facts['product_title']),
    }
    record = {'source_url': KOBALT_SPRAYER_URL, 'observed_on': '2026-09-14',
              'facts': facts, 'sources': sources}
    return {**facts, 'source_metadata_evidence': {
        'schema_version': 'voxquarry.source-product-metadata/v1',
        'channel': 'lowes', 'source_url': url, 'final_url': KOBALT_SPRAYER_URL,
        'source_sha256': hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest(),
        'digest_scope': 'reviewed_fact_record', 'identity': 'reviewed_exact_product_id',
        'source_product_id': '1002981480', 'observed_on': '2026-09-14',
        'fields': {key: {'source': 'reviewed_pdp.' + sources[key][0],
                         'raw': sources[key][1], 'value': value} for key, value in facts.items()},
        'battery_count': 1, 'battery_ecosystem_voltage': '24V', 'system_voltages': ['24V'], 'gaps': [],
    }}


def reviewed_product_metadata(channel, url):
    parsed = urlsplit(url)
    if (channel == 'lowes' and (parsed.hostname or '').casefold() in {'lowes.com', 'www.lowes.com'}
            and re.fullmatch(r'/pd/[^/]+/5015278625/?', parsed.path)):
        return _ego_880_kit_metadata(url)
    if (channel == 'lowes' and (parsed.hostname or '').casefold() in {'lowes.com', 'www.lowes.com'}
            and re.fullmatch(r'/pd/[^/]+/1002981480/?', parsed.path)):
        return _kobalt_sprayer_metadata(url)
    if (channel != 'lowes' or (parsed.hostname or '').casefold() not in {'lowes.com', 'www.lowes.com'}
            or not re.fullmatch(r'/pd/[^/]+/5019587973/?', parsed.path)):
        return None
    facts = {
        'brand': 'Kobalt', 'model': 'KMS 6224A-06A',
        'category': 'Walk behind Lawnmower', 'energy_type': 'battery',
        'voltage': '24V', 'market_region': 'North America',
        'product_title': 'Kobalt self-propelled 21-inch battery mower, 48V system (2x24V)',
    }
    sources = {
        'brand': ('brand', 'Kobalt'), 'model': ('model_number', 'KMS 6224A-06A'),
        'category': ('specifications.type', 'Self-propelled lawn mower'),
        'energy_type': ('specifications.power_source', 'Battery'),
        'voltage': ('specifications.total_battery_voltage', '48v (2x24v)'),
        'market_region': ('storefront', 'lowes.com US'),
        'product_title': ('product_title_summary', facts['product_title']),
    }
    record = {'source_url': KOBALT_URL, 'observed_on': '2026-09-09', 'facts': facts, 'sources': sources}
    result = deepcopy(facts)
    result['source_metadata_evidence'] = {
        'schema_version': 'voxquarry.source-product-metadata/v1',
        'channel': channel, 'source_url': url, 'final_url': KOBALT_URL,
        'source_sha256': hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest(),
        'digest_scope': 'reviewed_fact_record', 'identity': 'reviewed_exact_product_id',
        'source_product_id': '5019587973', 'observed_on': '2026-09-09',
        'fields': {key: {'source': 'reviewed_pdp.' + sources[key][0],
                         'raw': sources[key][1], 'value': value} for key, value in facts.items()},
        'battery_count': 2, 'battery_ecosystem_voltage': '24V', 'system_voltages': ['48V'], 'gaps': [],
    }
    return result
