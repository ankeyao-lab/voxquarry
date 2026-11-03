"""Parse product facts from one already-fetched, exact public product document.

No network, filesystem, catalog writes, review requests, or URL-slug inference.
Retailer IDs identify a document; they never become manufacturer model numbers.
Missing facts stay missing so admission can request better evidence.
"""
from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

from .category_vocabulary import MASTER_CATEGORY_NAMES, canonical_category_name, is_sprayer_product
from .product_identity import normalize_voltage
from .product_metadata import normalize_brand, normalize_model


SOURCE_METADATA_SCHEMA = "voxquarry.source-product-metadata/v1"

# This registry describes metadata sources, not permission to collect reviews.
# Home Depot, Walmart and amazon_live retain their external-import policy.
CHANNEL_REGISTRY = {
    "lowes": {"hosts": ("lowes.com",), "region": "North America"},
    "homedepot": {"hosts": ("homedepot.com",), "region": "North America"},
    "ace": {"hosts": ("acehardware.com",), "region": "North America"},
    "ego": {"hosts": ("egopowerplus.com", "egopowerplus.co.uk"), "brand": "EGO", "region": "North America"},
    "ryobi": {"hosts": ("ryobitools.com", "ryobitools.eu"), "brand": "RYOBI", "region": "North America"},
    "milwaukee": {"hosts": ("milwaukeetool.com",), "brand": "Milwaukee", "region": "North America"},
    "dewalt": {"hosts": ("dewalt.com",), "brand": "DEWALT", "region": "North America"},
    "einhell": {"hosts": ("einhell.de", "einhell.co", "einhell.it", "einhell.fr", "einhell.co.uk"), "brand": "Einhell", "region": "Europe"},
    "harborfreight": {"hosts": ("harborfreight.com",), "region": "North America"},
    "tractorsupply": {"hosts": ("tractorsupply.com",), "region": "North America"},
    "amazon_reviews_2023": {"hosts": ("amazon.com",), "region": "North America", "source_kind": "archive_metadata"},
    "skil": {"hosts": ("skil.com",), "brand": "SKIL", "region": "North America"},
    "walmart": {"hosts": ("walmart.com",), "region": "North America"},
    "stihl": {"hosts": ("stihlusa.com",), "brand": "STIHL", "region": "North America"},
    "husqvarna": {"hosts": ("husqvarna.com",), "brand": "Husqvarna", "region": None},
    "toro": {"hosts": ("toro.com",), "brand": "Toro", "region": None},
    "johndeere": {"hosts": ("deere.com",), "brand": "John Deere", "region": None},
    "exmark": {"hosts": ("exmark.com",), "brand": "Exmark", "region": "North America"},
    "greenworks": {"hosts": ("greenworkstools.com",), "brand": "Greenworks", "region": "North America"},
    "amazon_live": {"hosts": ("amazon.com", "amazon.ca", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.it", "amazon.es", "amazon.nl", "amazon.se", "amazon.pl", "amazon.com.au"), "region": "North America", "source_kind": "external_import_pdp"},
}
CHANNEL_METADATA_SOURCES = CHANNEL_REGISTRY


class SourceProductMetadataError(ValueError):
    def __init__(self, message, code="source_metadata_missing"):
        super().__init__(message)
        self.code = code


def _text(value):
    if isinstance(value, dict):
        value = value.get("name") or value.get("value") or value.get("@value") or ""
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip()


def _source_brand(value):
    cleaned = normalize_brand(_text(value).replace("®", "").replace("™", ""))
    aliases = {"ego power+": "EGO", "ego power +": "EGO", "ego power plus": "EGO", "egopowerplus": "EGO",
               "milwaukee tool": "Milwaukee", "greenworks tools": "Greenworks", "greenworkstools": "Greenworks",
               "ryobi tools": "RYOBI", "skil tools": "SKIL",
               "einhell expert": "Einhell"}
    return aliases.get((cleaned or "").casefold(), cleaned)


def _host(url):
    return (urlsplit(url).hostname or "").casefold().removeprefix("www.")


def _check_url(channel, url):
    parsed = urlsplit(str(url or ""))
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 80, 443)):
        raise SourceProductMetadataError("来源必须为不含凭据的官方 HTTP(S) 商品 URL", "source_identity_mismatch")
    host = _host(url)
    if not any(host == allowed or host.endswith("." + allowed) for allowed in CHANNEL_REGISTRY[channel]["hosts"]):
        raise SourceProductMetadataError("来源域名与渠道不一致", "source_identity_mismatch")


def _variant(url):
    parsed = urlsplit(url)
    fragment = parsed.fragment.split("?", 1)[-1]
    values = parse_qs(fragment).get("aID", []) or parse_qs(parsed.query).get("aID", [])
    if len(values) > 1:
        raise SourceProductMetadataError("来源选择了多个商品变体", "source_identity_mismatch")
    return values[0] if values else None


def _product_id(channel, url):
    path = unquote(urlsplit(url).path).rstrip("/")
    patterns = {
        "milwaukee": r"/products/details/[^/]+/((?=[A-Za-z0-9-]*[0-9])[A-Za-z0-9][A-Za-z0-9-]{2,63})$",
        "lowes": r"/pd/(?:[^/]+/)?(\d{6,})$",
        "homedepot": r"/p/(?:[^/]+/)?(\d{6,})$",
        "ace": r"/departments/(?:[^/]+/)+(\d{5,})$",
        "walmart": r"/ip/(?:[^/]+/)?(\d+)$",
        "harborfreight": r"-(\d{4,})\.html$",
        "amazon_live": r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/.*)?$",
        "amazon_reviews_2023": r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/.*)?$",
        "einhell": r"/p/(\d{6,})(?:[-/].*)?$",
        "stihl": r"-(\d{5,})$",
    }
    match = re.search(patterns.get(channel, r"(?!)"), path, re.I)
    return match.group(1).casefold() if match else None


def _url_key(url):
    parsed = urlsplit(url)
    path = unquote(parsed.path).rstrip("/")
    if "/products/" in path and _host(url) == "greenworkstools.com":
        path = "/products/" + path.rsplit("/products/", 1)[1]
    return (_host(url), path.casefold())


def _same_product(channel, left, right):
    if _host(left) != _host(right):
        return False
    left_id, right_id = _product_id(channel, left), _product_id(channel, right)
    same = left_id == right_id if left_id and right_id else _url_key(left) == _url_key(right)
    left_variant, right_variant = _variant(left), _variant(right)
    return same and (not left_variant or not right_variant or left_variant == right_variant)


def _region(channel, url):
    host = _host(url)
    parts = [part.casefold() for part in urlsplit(url).path.split("/") if part]
    first = parts[0] if parts else ""
    if first in {"au", "en-au", "en_au"} or host.endswith((".com.au", ".au")):
        return "Australia"
    if first in {"uk", "gb", "en-gb", "en-uk", "de", "de-de", "fr", "fr-fr", "it", "it-it", "es", "es-es", "nl", "nl-nl", "se", "sv-se", "pl", "pl-pl"}:
        return "Europe"
    if first in {"us", "en-us", "en_us", "ca", "en-ca", "fr-ca", "mx", "es-mx"}:
        return "North America"
    if channel == "toro" and first == "en":
        return "North America"
    if host == "einhell.co" or host.endswith((".co.uk", ".uk", ".eu", ".de", ".fr", ".it", ".es", ".nl", ".se", ".pl")):
        return "Europe"
    return CHANNEL_REGISTRY[channel].get("region")


def infer_source_market_region(channel, url):
    """Use an admitted channel's market URL; an unscoped global host is unknown."""
    if channel not in CHANNEL_REGISTRY:
        raise SourceProductMetadataError("不支持的商品证据渠道", "unsupported_channel")
    _check_url(channel, url)
    return _region(channel, url)


class _Node:
    def __init__(self, tag="root", attrs=None, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs or []), parent, []


_EXCLUDED = re.compile(r"review|recommend|related|recently[-_ ]?viewed|upsell|cross[-_ ]?sell|compare|accessor(?:y|ies)|carousel|other[-_ ]?products|what.?s[-_ ]?included|in[-_ ]?the[-_ ]?box|product[-_ ]?(?:card|tile)|card[-_ ]?product", re.I)
_SPEC = re.compile(r"specification|technical|product[-_ ]?(?:description|details|overview|specs|information)|^overview$|^features$|^description$|detailBullets|prodDetails", re.I)
_MODEL_LABEL = r"(?:(?:manufacturer|mfr\.?|item) )?model(?: number| no\.?)?|manufacturer part number|mfr\.? part number"


class _Document(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]
        self.nodes = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _blocked_node(node):
    return (node.tag in {"nav", "footer", "aside", "script", "style", "noscript"}
            or bool(_EXCLUDED.search(" ".join(str(node.attrs.get(k, "")) for k in ("id", "class", "data-testid", "itemprop")))))


def _node_text(node, *, raw=False):
    if not raw and _blocked_node(node):
        return ""
    return _text(" ".join(child if isinstance(child, str) else _node_text(child, raw=raw) for child in node.children))


def _eligible(node):
    while node is not None:
        if _blocked_node(node):
            return False
        node = node.parent
    return True


def _walk_json(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk_json(item)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        types = [types] if isinstance(types, str) else types
        types = types if isinstance(types, list) else []
        if any(str(kind).rsplit("/", 1)[-1] in {"Product", "ProductGroup"} for kind in types):
            yield value
        # Reviews, recommendation lists and offers can contain other Product
        # objects. They are candidates only when their own identity matches.
        for key, child in value.items():
            if key not in {"review", "reviews", "aggregateRating"} and isinstance(child, (dict, list)):
                yield from _walk_json(child)


def _product_urls(product):
    for field in ("url", "@id", "mainEntityOfPage"):
        value = product.get(field)
        if isinstance(value, dict):
            value = value.get("@id") or value.get("url")
        if isinstance(value, str) and not value.startswith("#"):
            yield value


def _matching_microdata_product(node, channel, url, final_url):
    parent = node.parent
    while parent is not None:
        if "itemscope" in parent.attrs and parent.attrs.get("itemtype", "").rstrip("/").endswith("/Product"):
            identity = parent.attrs.get("itemid")
            return bool(identity and _same_product(channel, url, urljoin(final_url, identity)))
        parent = parent.parent
    return False


def _select_product(channel, url, final_url, canonical, products, h1):
    variant = _variant(url)
    matches = []
    for product in products:
        urls = [urljoin(final_url, value) for value in _product_urls(product)]
        identity_match = any(_same_product(channel, url, value) for value in urls)
        requested_id = _product_id(channel, url)
        if not urls and requested_id and canonical and _same_product(channel, url, canonical):
            identity_match = any(str(product.get(key) or "").casefold() == requested_id for key in ("productID", "sku"))
        if variant:
            identity_match = (str(product.get("sku") or product.get("productID") or "") == variant
                              and (identity_match or bool(canonical and _same_product(channel, url, canonical))))
        if identity_match:
            matches.append(product)
    if not matches and len(products) == 1 and not variant:
        product = products[0]
        # A URL-less main Product is acceptable only when the document's own
        # canonical URL and primary heading independently agree.
        if not list(_product_urls(product)) and canonical and _same_product(channel, url, canonical):
            if _text(product.get("name")).casefold() == h1.casefold() and h1:
                matches.append(product)
    factual_keys = ("name", "brand", "mpn", "model", "category", "description", "additionalProperty")
    # Separate SEO blocks can repeat one Product with different offers or
    # aggregate counts. Only conflicts in the product facts matter here.
    unique = {json.dumps({key: product.get(key) for key in factual_keys}, sort_keys=True, ensure_ascii=False): product for product in matches}
    if len(unique) > 1:
        raise SourceProductMetadataError("同一商品存在冲突的结构化产品记录", "source_metadata_conflict")
    if unique:
        return next(iter(unique.values()))
    if products:
        raise SourceProductMetadataError("结构化产品未唯一匹配请求商品，拒绝推荐商品或其他变体", "source_identity_mismatch")
    if variant:
        raise SourceProductMetadataError("缺少所选变体的精确结构化产品证据", "source_identity_mismatch")
    return None


def _specs(product, document):
    result = []
    if product:
        props = product.get("additionalProperty", [])
        props = [props] if isinstance(props, dict) else props
        for prop in props if isinstance(props, list) else []:
            if isinstance(prop, dict) and _text(prop.get("name")) and _text(prop.get("value")):
                result.append((_text(prop["name"]), _text(prop["value"]), "jsonld.additionalProperty"))
    for node in document.nodes:
        if not _eligible(node):
            continue
        marker = " ".join(str(node.attrs.get(key, "")) for key in ("id", "class", "data-testid", "itemprop"))
        if not _SPEC.search(marker):
            continue
        for child in node.children:
            if not isinstance(child, _Node):
                continue
            if child.tag in {"tr", "div", "li"}:
                cells = [item for item in child.children if isinstance(item, _Node)]
                if len(cells) == 2:
                    label, value = (_node_text(item) for item in cells)
                    if label and value and len(label) < 100 and len(value) < 500:
                        result.append((label, value, "pdp.specification"))
        descendants = [item for item in document.nodes if item.parent is node]
        for index, child in enumerate(descendants[:-1]):
            if child.tag == "dt" and descendants[index + 1].tag == "dd":
                result.append((_node_text(child), _node_text(descendants[index + 1]), "pdp.specification"))
        # Tables may have a tbody between the named specification container
        # and rows; only rows inside this already-scoped container count.
        for row in document.nodes:
            if row.tag != "tr" or not _eligible(row):
                continue
            parent = row.parent
            while parent is not None and parent is not node:
                parent = parent.parent
            if parent is node:
                cells = [child for child in row.children if isinstance(child, _Node) and child.tag in {"td", "th"}]
                if len(cells) == 2:
                    result.append((_node_text(cells[0]), _node_text(cells[1]), "pdp.specification"))
    return list(dict.fromkeys(result))


def _field(specs, pattern):
    return [(value, source + ":" + label) for label, value, source in specs if re.fullmatch(pattern, label.strip(" :#"), re.I)]


def _one(values, field, transform=_text):
    normalized = {}
    for value, source in values:
        clean = transform(value)
        if clean:
            normalized.setdefault(str(clean).casefold(), (clean, source, _text(value)))
    if len(normalized) > 1:
        raise SourceProductMetadataError(f"商品{field}存在冲突证据", "source_metadata_conflict")
    return next(iter(normalized.values())) if normalized else (None, None, None)


_CATEGORIES = (
    ("Pressure Washer Accessory", r"pressure washer (?:hose|nozzle|accessor|attachment)|surface cleaner attachment"),
    ("Vacuum Accessory", r"vacuum (?:accessor|attachment|filter)"),
    ("Battery Pack", r"battery pack|replacement battery"),
    ("Charger", r"^(?:[\w +.-]+ )?battery charger$"),
    ("Robot Mower", r"robot(?:ic)? (?:lawn ?)?mower|automower"),
    ("Riding Lawnmower", r"zero.turn|riding (?:lawn ?)?mower|lawn tractor"),
    ("Power Cleaner", r"power cleaner|(?:portable|handheld|hand.held).*(?:cordless|battery).*(?:pressure|power) washer"),
    ("Pressure Washer", r"pressure washer|high.pressure cleaner|pressure cleaner"),
    ("Snow Shovel", r"snow shovel"),
    ("Snow Blower", r"snow (?:blower|thrower)"),
    ("Walk behind Lawnmower", r"lawn ?mower|walk.behind mower|push mower|self.propelled mower|\bmower\b"),
    ("Wheeled Blower", r"wheeled blower|walk.behind blower"),
    ("Hedge Trimmer", r"hedge trimmer"),
    ("String Trimmer", r"string trimmer|grass trimmer|brushcutter"),
    ("Leaf Blower/Vacuum", r"leaf blower|backpack blower|handheld blower|\bblower\b"),
    ("Chain Saw", r"chain ?saw"),
    ("Wet/Dry Vacuum", r"wet[ /-]*(?:and[ /-]*)?dry vac|shop vac"),
    ("Cordless Stick Vacuum", r"stick vac"),
    ("Cordless Handheld Vacuum", r"handheld vac|hand vac"),
    ("Window Vacuum", r"window vac"),
    ("Air Compressor", r"air compressor"),
    ("LED Lighting", r"work light|area light|tripod light|task light"),
    ("Radio/Speaker", r"radio|bluetooth speaker"),
    ("Fan", r"\bfan\b"),
    ("Generator", r"\bgenerator\b"),
    ("Edger", r"\bedger\b"),
    ("Drill", r"\bdrill\b"),
    ("Impact Wrench", r"impact wrench"),
    ("Tiller/Cultivator", r"tiller|cultivator"),
)


def _category(raw, title):
    if is_sprayer_product(raw, title):
        return "Sprayer"
    normalized = canonical_category_name(_text(raw)) if raw else None
    if normalized in MASTER_CATEGORY_NAMES and normalized != "Unknown Category":
        return normalized
    for name, pattern in _CATEGORIES:
        if re.search(pattern, " ".join((_text(raw), title)), re.I):
            return name
    return None


def _energy(title, specs, description):
    explicit = _field(specs, r"power source|power type|energy type|fuel type")
    aliases = {"battery": "battery", "battery powered": "battery", "cordless": "battery", "cordless electric": "battery", "battery/cordless": "battery", "electric (cordless)": "battery", "corded": "corded", "corded electric": "corded", "electric (corded)": "corded", "ac": "corded", "gas": "fuel", "gasoline": "fuel", "petrol": "fuel", "diesel": "fuel", "fuel": "fuel", "manual": "other", "other": "other", "hybrid": "other", "ac/dc": "other"}
    selected = _one(explicit, "能源", lambda value: aliases.get(_text(value).casefold()))
    cleaned_title = re.sub(r"\b(?:m12|m18|mx)\s+fuel\b", "battery platform", title, flags=re.I)
    hybrid_description = " ".join(line for line in re.split(r"[.;\n]", description)
                                  if re.search(r"hybrid|dual.source|powered.*ac\s*[/&+]\s*dc", line, re.I)
                                  and not re.search(r"compatib|works with|accessor", line, re.I))
    hybrid_text = title + " " + " ".join(value for value, _ in explicit) + " " + hybrid_description
    hybrid = bool(re.search(r"\bac\s*[/&+]\s*dc\b", hybrid_text, re.I)) or (
        bool(re.search(r"\bhybrid\b", hybrid_text, re.I))
        and bool(re.search(r"\b(?:ac|corded|mains)\b", hybrid_text, re.I))
        and bool(re.search(r"\b(?:dc|battery|cordless|\d+\s*v)\b", hybrid_text, re.I))
    )
    if hybrid:
        if selected[0] and selected[0] not in {"battery", "corded", "other"}:
            raise SourceProductMetadataError("混动与能源字段冲突", "source_metadata_conflict")
        return "other", "pdp.exact_product_hybrid", "AC/DC hybrid"
    title_energy = None
    for value, pattern in (
        ("fuel", r"\bgas(?:oline)?[- ]powered\b|\bpetrol\b|\bdiesel\b|\b\d+(?:\.\d+)?\s*cc\b|\bgas(?:oline)? (?:pressure washer|mower|blower|chain ?saw)\b"),
        ("battery", r"\bcordless\b|\bbattery(?:[- ]powered)?\b|\bakku\b|\b(?:m12|m18|mx)\s+fuel\b"),
        ("corded", r"\bcorded\b|\bac[- ]powered\b|\bmains[- ]powered\b|\belectric pressure washer\b|\b\d+(?:\.\d+)?[- ]?amp\b"),
        ("other", r"\bmanual\b|\bhand[- ]powered\b"),
    ):
        if re.search(pattern, cleaned_title, re.I):
            title_energy = value
            break
    if selected[0]:
        if title_energy and title_energy != selected[0]:
            raise SourceProductMetadataError("商品标题与能源规格冲突", "source_metadata_conflict")
        return selected
    if title_energy:
        return title_energy, "pdp.product_title", title
    engine_specs = _field(specs, r"cylinder displacement|engine displacement|fuel tank (?:volume|capacity)")
    for raw, source in engine_specs:
        measure = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:cc|cm[³3]|cu\.?\s*in\.?|in[³3]|l|liters?|litres?|gal(?:lons?)?|fl\.?\s*oz\.?)\s*", raw, re.I)
        if measure and float(measure.group(1)) > 0:
            return "fuel", source, raw
    ac_specs = _field(specs, r"ac volts|ac voltage|amperage|rated input power")
    if any(_voltages(value) or re.fullmatch(r"\d+(?:\.\d+)?(?:\s*(?:amps?|a|w|watts?))?", value, re.I) for value, _ in ac_specs):
        return "corded", ac_specs[0][1], ac_specs[0][0]
    # A factual power-source sentence can supply missing energy, but general
    # comparisons ('without fumes of gas') and compatibility text cannot.
    for line in re.split(r"[.;\n]", description):
        if re.search(r"compatib|works with|without|gas.like|gas.like", line, re.I):
            continue
        if re.search(r"\b(?:battery[- ]powered|powered by (?:\w+\s+){0,4}batter(?:y|ies))\b", line, re.I):
            return "battery", "pdp.exact_product_description", _text(line)
        if re.search(r"\b(?:gas[- ]powered|petrol[- ]powered|diesel[- ]powered)\b", line, re.I):
            return "fuel", "pdp.exact_product_description", _text(line)
    return None, None, None


def _voltages(value):
    return set(filter(None, (normalize_voltage(match.group(1) + "V") for match in re.finditer(r"(?<![\w.])(\d+(?:\.\d+)?)\s*[- ]?\s*(?:volts?|v)\b", value, re.I))))


def _voltage(title, specs, description, energy):
    if energy in {"corded", "fuel"}:
        return (None, None, None), {}
    exact_fields = _field(specs, r"battery voltage|battery platform voltage|nominal voltage|rated voltage|voltage|total battery voltage")
    scoped = " ".join([title, *[value for value, _ in exact_fields]])
    # Descriptions contribute only explicit battery arrangements, never an
    # incidental compatible battery, charger input, or neighbouring product.
    arrangement_lines = [line for line in re.split(r"[.;\n]", description) if re.search(r"(?:two|2)\s+(?:\d+\s*[- ]?(?:v|volt)|batter)|(?:\d+\s*v\s*[x×]\s*2|2\s*[x×]\s*\d+\s*v)", line, re.I) and not re.search(r"compatib|works with|sold separately", line, re.I)]
    arrangement_text = scoped + " " + " ".join(arrangement_lines)
    cells = set()
    for pattern in (
        r"\b2\s*[x×]\s*(\d+(?:\.\d+)?)\s*[- ]?(?:volts?|v)\b",
        r"\b(\d+(?:\.\d+)?)\s*[- ]?(?:volts?|v)\s*[x×]\s*2\b",
        r"\b(?:two|2)\s+(\d+(?:\.\d+)?)\s*[- ]?(?:volts?|v)\s+(?:[\w+-]+\s+){0,3}batter(?:y|ies)\b",
    ):
        cells.update(normalize_voltage(value + "V") for value in re.findall(pattern, arrangement_text, re.I))
    if len(cells) > 1:
        raise SourceProductMetadataError("双电池生态电压存在冲突", "source_metadata_conflict")
    if cells:
        cell = next(iter(cells))
        systems = sorted(_voltages(scoped) - {cell})
        if any(float(value[:-1]) != 2 * float(cell[:-1]) for value in systems):
            raise SourceProductMetadataError("双电池与系统电压不能一致证明", "source_metadata_conflict")
        return (cell, "pdp.explicit_two_battery_ecosystem", arrangement_text[:350]), {"battery_count": 2, "battery_ecosystem_voltage": cell, "system_voltages": systems, "raw_voltage_fields": [{"value": value, "source": source} for value, source in exact_fields]}
    values = []
    for value, source in exact_fields:
        canonical = normalize_voltage(value)
        observed = _voltages(value)
        if canonical:
            values.append((canonical, source))
        elif len(observed) == 1:
            values.append((next(iter(observed)), source))
        elif len(observed) > 1:
            raise SourceProductMetadataError("同一电压字段包含冲突数值", "source_metadata_conflict")
    selected = _one(values, "电压")
    if selected[0]:
        title_without_charger = re.sub(r"\b(?:with|includes?|and)\b[^.;]*\bcharger\b.*", "", title, flags=re.I)
        title_voltages = _voltages(title_without_charger)
        allowed = set(selected[0].split("/"))
        if title_voltages - allowed:
            raise SourceProductMetadataError("商品标题与电压规格冲突", "source_metadata_conflict")
        return selected, {}
    observed = _voltages(title)
    dual = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:v|volts?)?\s*/\s*(\d+(?:\.\d+)?)\s*(?:v|volts?)\b", title, re.I)
    composite = normalize_voltage(f"{dual.group(1)}V/{dual.group(2)}V") if dual else None
    if composite and observed.issubset(set(composite.split("/"))):
        return (composite, "pdp.explicit_dual_voltage", dual.group(0)), {"dual_voltage": composite}
    if len(observed) > 1:
        raise SourceProductMetadataError("商品标题包含多个未解释电压", "source_metadata_conflict")
    return ((next(iter(observed)), "pdp.product_title", title) if observed else (None, None, None)), {}


def _archive(channel, url, source, digest):
    try:
        payload = json.loads(source)
    except ValueError as exc:
        raise SourceProductMetadataError("归档渠道要求精确 ASIN 的归档元数据 JSON") from exc
    if not isinstance(payload, dict) or str(payload.get("source_product_id") or payload.get("parent_asin") or "").casefold() != _product_id(channel, url):
        raise SourceProductMetadataError("归档元数据 ASIN 与 URL 不一致", "source_identity_mismatch")
    if payload.get("market_region") != "North America" or not payload.get("source_revision") or payload.get("archive_through") != "2023-09-30":
        raise SourceProductMetadataError("归档元数据缺少市场、版本或时间边界")
    title = _text(payload.get("title"))
    details = payload.get("details", {})
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError as exc:
            raise SourceProductMetadataError("归档 details 不是合法 JSON 对象") from exc
    details = details if isinstance(details, dict) else {}
    specs = [(key, _text(value), "archive.details") for key, value in details.items()]
    for field, label in (("energy_type", "Energy Type"), ("voltage", "Battery Voltage")):
        if payload.get(field):
            specs.append((label, _text(payload[field]), "archive.metadata"))
    description_parts = []
    for key in ("features", "description"):
        values = payload.get(key, [])
        values = [values] if isinstance(values, str) else values
        if isinstance(values, list):
            description_parts.extend(_text(value) for value in values if isinstance(value, str))
    description = ". ".join(description_parts)
    energy = _energy(title, specs, description)
    voltage, battery_evidence = _voltage(title, specs, description, energy[0])
    model = _one([(payload.get("model"), "archive.metadata.model"), *_field(specs, _MODEL_LABEL)], "型号", normalize_model)
    if model[0] and str(model[0]).casefold() in {str(payload.get(key) or "").casefold() for key in ("parent_asin", "source_product_id", "asin")}:
        model = (None, None, None)
    fields = {
        "brand": _one([(payload.get("brand"), "archive.metadata.brand"), *_field(specs, r"brand|brand name")], "品牌", _source_brand),
        "model": model,
        "category": (_category(payload.get("category"), title), "archive.metadata.category_or_title", title),
        "energy_type": energy,
        "voltage": voltage,
        "market_region": ("North America", "archive.market_region", "North America"),
        "product_title": (title or None, "archive.metadata.title", title),
    }
    return _result(channel, url, url, digest, "archive_exact_asin", fields, battery_evidence, {"source_revision": payload["source_revision"], "archive_through": payload["archive_through"]})


def _result(channel, url, final_url, digest, identity, fields, battery_evidence, extra=None):
    result = {key: value[0] for key, value in fields.items()}
    result["source_metadata_evidence"] = {
        "schema_version": SOURCE_METADATA_SCHEMA,
        "channel": channel,
        "source_url": url,
        "final_url": final_url,
        "source_sha256": digest,
        "identity": identity,
        "fields": {key: {"source": source, "raw": raw, "value": value} for key, (value, source, raw) in fields.items() if value is not None},
        "gaps": [key for key, value in result.items() if value is None],
        **battery_evidence,
        **(extra or {}),
    }
    return result


def parse_source_product_metadata(channel, url, source, *, final_url=None):
    """Return seven nullable facts plus field-level provenance; raise on conflict.

    ``source`` is exact PDP HTML (or archive metadata JSON for the archive
    channel). The caller performs bounded public HTTP reads and supplies the
    actual final URL. This function neither fetches nor admits a collection job.
    """
    if channel not in CHANNEL_REGISTRY:
        raise SourceProductMetadataError("不支持的商品证据渠道", "unsupported_channel")
    _check_url(channel, url)
    final_url = final_url or url
    _check_url(channel, final_url)
    if not _same_product(channel, url, final_url):
        raise SourceProductMetadataError("商品请求跳转到了其他商品或域名", "source_identity_mismatch")
    if isinstance(source, bytes):
        source_bytes = source
        source = source.decode("utf-8", errors="replace")
    elif isinstance(source, str):
        source_bytes = source.encode("utf-8")
    else:
        raise SourceProductMetadataError("商品证据必须为 HTML 或归档 JSON 文本")
    if not source.strip() or len(source_bytes) > 8 * 1024 * 1024:
        raise SourceProductMetadataError("商品证据为空或超过大小上限")
    digest = hashlib.sha256(source_bytes).hexdigest()
    if channel == "amazon_reviews_2023":
        return _archive(channel, url, source, digest)
    document = _Document(source)
    headings = [_node_text(node) for node in document.nodes if node.tag == "h1" and _eligible(node) and _node_text(node)]
    titles = [_node_text(node) for node in document.nodes if node.tag == "title"]
    page_heading = " ".join(headings + titles)
    if re.search(r"access denied|robot check|verify (?:you are|that you)|just a moment|attention required|captcha|request rejected|403 forbidden", page_heading, re.I):
        raise SourceProductMetadataError("来源返回访问验证或拒绝页面", "source_access_blocked")
    if len(set(headings)) > 1:
        raise SourceProductMetadataError("页面包含多个主商品标题", "source_identity_mismatch")
    h1 = headings[0] if headings else ""
    canonicals = {urljoin(final_url, node.attrs["href"]) for node in document.nodes if node.tag == "link" and "canonical" in node.attrs.get("rel", "").casefold().split() and node.attrs.get("href")}
    if len(canonicals) > 1:
        raise SourceProductMetadataError("来源 canonical URL 不唯一", "source_identity_mismatch")
    canonical = next(iter(canonicals), None)
    if canonical:
        _check_url(channel, canonical)
        if not _same_product(channel, url, canonical):
            raise SourceProductMetadataError("来源 canonical 指向其他商品", "source_identity_mismatch")
    products = []
    for node in document.nodes:
        if node.tag == "script" and node.attrs.get("type", "").casefold().split(";", 1)[0].strip() == "application/ld+json":
            try:
                products.extend(_walk_json(json.loads(_node_text(node, raw=True))))
            except (ValueError, TypeError):
                continue
    product = _select_product(channel, url, final_url, canonical, products, h1)
    official_model_route = channel == "milwaukee" and bool(_product_id(channel, final_url)) and bool(h1)
    if product is None and (not canonical or not h1) and not official_model_route:
        raise SourceProductMetadataError("来源未证明唯一主商品，需要 Product 结构化记录或 canonical 加主标题")
    title = _text(product.get("name")) if product else h1
    if not title:
        raise SourceProductMetadataError("主商品未公开产品标题")
    specs = _specs(product, document)
    descriptions = []
    if product and product.get("description"):
        descriptions.append(_node_text(_Document(str(product["description"])).root))
    for node in document.nodes:
        if _eligible(node) and re.search(r"product[-_ ]?(?:description|overview)|^overview$", " ".join(node.attrs.get(key, "") for key in ("id", "class", "itemprop")), re.I):
            descriptions.append(_node_text(node))
    description = " ".join(dict.fromkeys(descriptions))
    brand_values = _field(specs, r"brand|brand name|manufacturer brand")
    model_values = _field(specs, _MODEL_LABEL)
    if channel == "skil":
        # SKIL's primary PDP labels the sellable tool/kit manufacturer model
        # as Configuration. Included bare-tool SKUs live in excluded sections.
        model_values.extend(_field(specs, r"configuration"))
    if product:
        if product.get("brand"):
            brand_values.insert(0, (_text(product["brand"]), "jsonld.brand"))
        for key in ("mpn", "model"):
            if product.get(key):
                model_values.append((_text(product[key]), "jsonld." + key))
    if CHANNEL_REGISTRY[channel].get("brand"):
        brand_values.append((CHANNEL_REGISTRY[channel]["brand"], "official_brand_domain"))
    # Exact labelled microdata is permitted; a free title/retailer sku is not.
    for node in document.nodes:
        if (_eligible(node) and node.attrs.get("itemprop", "").casefold() in {"mpn", "model"}
                and _matching_microdata_product(node, channel, url, final_url)):
            model_values.append((node.attrs.get("content") or _node_text(node), "pdp.itemprop." + node.attrs["itemprop"]))
        elif _eligible(node) and re.search(r"^(?:model[-_ ]?number|manufacturer[-_ ]?model)$", node.attrs.get("id", "") or node.attrs.get("class", ""), re.I):
            labelled = re.sub(r"^(?:(?:manufacturer|mfr\.?)\s+)?model\s*(?:number|no\.?)?\s*[:#]?\s*", "", _node_text(node), flags=re.I)
            if labelled and len(labelled) < 80:
                model_values.append((labelled, "pdp.labelled_model_number"))
    if channel == "milwaukee" and not model_values and official_model_route:
        model_values.append((_product_id(channel, final_url).upper(), "pdp.official_manufacturer_model_route"))
    if channel == "ryobi" and not model_values:
        # The current US storefront places its own manufacturer model directly
        # after the primary heading. Recommendation/aside headings are excluded.
        primary_seen = False
        for node in document.nodes:
            if node.tag == "h1" and _eligible(node):
                primary_seen = True
            elif primary_seen and node.tag == "h2" and _eligible(node):
                value = _node_text(node)
                if re.fullmatch(r"[A-Z]{1,6}[0-9][A-Z0-9-]{1,24}", value):
                    model_values.append((value, "pdp.primary_model_heading"))
                break
    brand = _one(brand_values, "品牌", _source_brand)
    model = _one(model_values, "型号", normalize_model)
    raw_category = (product or {}).get("category")
    if isinstance(raw_category, list):
        raw_category = raw_category[-1] if raw_category else None
    category_values = _field(specs, r"product type|tool type|item type name|category|type")
    raw_category = _text(raw_category) or (category_values[0][0] if category_values else None)
    category = _category(raw_category, title)
    energy = _energy(title, specs, description)
    from .official_product_types import CHANNELS, PASSIVE_TYPES, classify
    if channel in CHANNELS:
        category = classify(title, raw_category, description) or category
        if category in PASSIVE_TYPES or category in {"OPE Accessory", "Vacuum Accessory", "Pressure Washer Accessory", "Cooler"}:
            energy = ("other", "pdp.passive_product_type", title)
        elif category == "Charger" and not _field(specs, r"power source|power type|energy type|fuel type"):
            energy = ("corded", "pdp.charger_product_type", title)
        elif not energy[0]:
            if re.search(r"(?:\bone\+|\b(?:4|8|18|24|40|48|60|80)\s*[- ]?\s*(?:v|volt)\b|\busb lithium\b)", title, re.I) or (channel == "milwaukee" and re.search(r"\b(?:m12|m18|mx fuel)\b", title, re.I)):
                energy = ("battery", "pdp.manufacturer_battery_platform", title)
            elif re.search(r"\b(?:[24][- ]cycle|[24][- ]stroke|propane)\b", title, re.I):
                energy = ("fuel", "pdp.explicit_engine_or_fuel", title)
            elif category in {"Battery Pack", "Charger"}:
                energy = ("battery" if category == "Battery Pack" else "corded", "pdp.product_type", title)
    if channel in CHANNELS and not energy[0] and description:
        intro = re.split(r"[.!?](?:\s|$)", re.sub(r"\bin\.\s", "inches ", description), maxsplit=1)[0]
        if not re.search(r"\b(?:compatible|compatibility|fits|works with|without|compared)\b", intro, re.I):
            introductory_energy = _energy(intro, [], "")
            if introductory_energy[0]:
                energy = (introductory_energy[0], "pdp.primary_description_intro", intro[:500])
    if channel in CHANNELS and not energy[0]:
        for sentence in re.split(r"[.!?](?:\s|$)", description):
            if re.search(r"compatib|works with|without|fits|accessor|included", sentence, re.I):
                continue
            if re.search(r"(?:powered by|features|equipped with) (?:a |an )?(?:powerful )?\d+(?:\.\d+)?[- ]?amp motor", sentence, re.I):
                energy = ("corded", "pdp.primary_product_motor", sentence[:500])
                break
    voltage_title = title
    if channel in CHANNELS and category == "Power Station/Inverter":
        voltage_title = re.sub(r"\bwith\s+\d+\s*v\s+output\b.*", "", title, flags=re.I).strip()
    named_dual_kit = channel == "greenworks" and category == "OPE Combo Kit" and re.search(
        r"\b(\d+)\s*v\b[^,]*\bchain\s?saw\b.*\band\s+(\d+)\s*v\b[^,]*\bpruner\b", title, re.I,
    )
    if named_dual_kit and not _field(specs, r"battery voltage|battery platform voltage|nominal voltage|rated voltage|voltage|total battery voltage"):
        known = normalize_voltage("/".join(value + "V" for value in sorted({named_dual_kit.group(1), named_dual_kit.group(2)}, key=float)))
        voltage = (known, "pdp.explicit_named_two_tool_platforms", named_dual_kit.group(0))
        battery_evidence = {"dual_voltage": known, "platform_scope": "two_named_tools"}
    else:
        voltage, battery_evidence = _voltage(voltage_title, specs, description, energy[0])
    if channel in CHANNELS:
        if category in PASSIVE_TYPES or category in {"OPE Accessory", "Vacuum Accessory", "Pressure Washer Accessory", "Cooler"}:
            voltage, battery_evidence = (None, None, None), {}
        elif channel == "ryobi" and energy[0] == "battery" and re.search(r"\bUSB Lithium\b", title, re.I):
            if voltage[0] and voltage[0] != "4V":
                raise SourceProductMetadataError("USB Lithium平台与电压规格冲突", "source_metadata_conflict")
            voltage = ("4V", "pdp.manufacturer_usb_lithium_platform", title)
        elif channel == "milwaukee" and energy[0] == "battery":
            platforms = {m.upper() for m in re.findall(r"\b(M12|M18|MX FUEL)\b", title, re.I)}
            if len(platforms) == 1:
                known = {"M12": "12V", "M18": "18V", "MX FUEL": "72V"}[next(iter(platforms))]
                if voltage[0] and voltage[0] != known:
                    raise SourceProductMetadataError("商品电池平台与电压规格冲突", "source_metadata_conflict")
                voltage = (known, "pdp.manufacturer_battery_platform", title)
    fields = {
        "brand": brand,
        "model": model,
        "category": (category, "jsonld.category" if (product or {}).get("category") else "pdp.exact_product_category_or_title", raw_category or title),
        "energy_type": energy,
        "voltage": voltage,
        "market_region": (_region(channel, final_url), "official_market_url", final_url),
        "product_title": (title, "jsonld.name" if product else "pdp.h1", title),
    }
    identity = {"method": "jsonld_exact_product" if product else "official_model_route_and_primary_heading" if official_model_route else "canonical_and_primary_heading", "canonical_url": canonical, "product_id": _product_id(channel, url), "variant_id": _variant(url)}
    return _result(channel, url, final_url, digest, identity, fields, battery_evidence)
