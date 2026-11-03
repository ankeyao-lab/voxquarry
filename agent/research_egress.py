"""Persisted public-search and URL admission, independent of SSRF checks.

Only trusted coordination code may register catalog metadata, original user
URLs and unmodified search-provider responses. These registration methods must
never become model tools. The model can choose original user product hints,
existing public catalog facts and fixed topics; it cannot contribute bytes to
an outbound search or URL.

This is provenance-based admission, not DLP. Product hints are extracted only
from the saved original question. Obvious credentials/internal IDs are denied,
but lexical checks cannot prove that every user-supplied model name is public.
DNS, redirects, byte limits and cancellation remain ResearchWeb's duties.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from .contracts import AgentError, IdentityContext, canonical_json
from .research_web import public_url


SCHEMA = "voxquarry.research-egress/v1"
TOPICS = {
    "product": ("产品链接", "product"),
    "specifications": ("规格参数", "specifications"),
    "manual": ("说明书", "manual"),
    "battery": ("电池", "battery"),
    "charging": ("充电", "charging"),
    "runtime": ("续航", "battery runtime"),
    "self_propelled": ("自走", "self propelled"),
    "maneuverability": ("操控转向", "maneuverability turning"),
    "cutting": ("切割表现", "cutting performance"),
    "noise": ("噪音", "noise"),
    "durability": ("耐用性", "durability"),
    "warranty": ("保修", "warranty"),
    "parts": ("配件", "replacement parts"),
    "troubleshooting": ("故障排查", "troubleshooting"),
    "reviews": ("公开评价", "reviews"),
}
MAX_PRODUCTS, MAX_URLS, MAX_SEARCHES = 32, 256, 64
REVIEW_CHANNELS = {'all': '', 'lowes': 'lowes.com', 'ace': 'acehardware.com',
                  'ego': 'egopowerplus.com', 'homedepot': 'homedepot.com',
                  'walmart': 'walmart.com', 'amazon': 'amazon.com'}
CATEGORIES = {
    "割草机": "lawn mower", "lawn mower": "lawn mower", "mower": "lawn mower",
    "吹叶机": "leaf blower", "leaf blower": "leaf blower", "blower": "leaf blower",
    "打草机": "string trimmer", "string trimmer": "string trimmer",
    "绿篱机": "hedge trimmer", "hedge trimmer": "hedge trimmer",
    "链锯": "chainsaw", "chainsaw": "chainsaw",
    "高压清洗机": "pressure washer", "pressure washer": "pressure washer",
    "除雪机": "snow blower", "snow blower": "snow blower",
    "电池": "power tool battery", "充电器": "power tool charger",
}
_SENSITIVE_CONTEXT = re.compile(r"api[ _-]?key|access[ _-]?token|bearer\s|password|passwd|secret|cookie|authorization|密钥|密码|令牌|机密|保密|内部编号|身份证|手机号|邮箱|客户地址|家庭住址|邮寄地址", re.I)
_INTERNAL_ID = re.compile(r"^(?:sk|pk|api|key|token|rpt|report|src|ds|job|run|attempt|session|attachment|auth|trace)[_-]|^[a-f0-9]{16,}$", re.I)
_MODEL = re.compile(r"(?<![A-Za-z0-9_./:-])([A-Za-z]{1,10}[A-Za-z0-9-]{1,22})(?![A-Za-z0-9_./:-])")


def _user_hints(question, known_brands):
    """Small product grammar, not a claim that regex can detect private data."""
    if not isinstance(question, str) or len(question) > 20000 or _SENSITIVE_CONTEXT.search(question):
        return []
    text = re.sub(r"https?://\S+", " ", question)
    lower = text.casefold()
    categories = list(dict.fromkeys(value for key, value in CATEGORIES.items() if key.casefold() in lower))[:2]
    brands = [brand for brand in known_brands if brand.casefold() in lower][:2]
    models = []
    for match in _MODEL.finditer(text):
        value = match[1]
        if (not 4 <= len(value) <= 24 or not any(char.isdigit() for char in value) or _INTERNAL_ID.search(value)
                or len(re.sub(r"\D", "", value)) > 8):
            continue
        models.append(value)
    hints = [" ".join([*brands, value, *categories[:1]]) for value in dict.fromkeys(models)][:4]
    # Quoted product names must contain a recognizable product/model shape,
    # not merely an arbitrary quote from a report or a user instruction.
    for quoted in re.findall(r'["“「]([^"”」\n]{2,64})["”」]', text):
        words = quoted.split()
        if len(words) > 6 or any(char in quoted for char in ":：;；=/@{}[]") or _SENSITIVE_CONTEXT.search(quoted):
            continue
        is_product = False
        for model in models:
            if re.fullmatch(r"(?:[A-Za-z][A-Za-z._-]{0,23} )?" + re.escape(model), quoted):
                is_product = True
        for category in CATEGORIES:
            # A product-like name ends with the category. A sentence merely
            # mentioning a mower (e.g. an internal defect-rate finding) is not
            # admitted as a quoted product name.
            if quoted.casefold().endswith(category.casefold()):
                prefix = quoted[:len(quoted) - len(category)].strip()
                if (not prefix or re.fullmatch(r"[\u3400-\u9fff]{1,12}", prefix)
                        or re.fullmatch(r"[A-Za-z0-9._-]{1,24}(?: [A-Za-z0-9._-]{1,24}){0,2}", prefix)):
                    is_product = True
        if is_product:
            hints.append(" ".join(quoted.split()))
    if not hints and (brands or categories):
        hints.append(" ".join([*brands, *categories[:1]]))
    return list(dict.fromkeys(hints))[:4]


def _fingerprint(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _match(value):
    return " ".join(value.split()).casefold()


def _url(value):
    checked = public_url(value)
    parsed = urlsplit(checked)
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = "[" + host + "]"
    port = parsed.port
    authority = host + (":" + str(port) if port is not None and (parsed.scheme, port) not in {("https", 443), ("http", 80)} else "")
    # Query/path bytes must match a registered original. Never discard query
    # parameters, decode/re-encode payloads or permit a prefix/host-wide match.
    return urlunsplit((parsed.scheme.lower(), authority, parsed.path, parsed.query, ""))


def _term(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise AgentError("public_product_invalid", "公开产品元数据不完整")
    return " ".join(value.split())


def _search_links(result):
    """Read explicit result-link fields, not URLs invented from page snippets."""
    links, visited = [], 0
    def walk(value, depth=0):
        nonlocal visited
        visited += 1
        if depth > 12 or visited > 10000:
            raise AgentError("search_result_limit", "公开搜索结果超过登记上限")
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"url", "link", "href"} and isinstance(item, str):
                    links.append(item)
                elif isinstance(item, (list, dict)):
                    walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)
    for block in result.get("content", []):
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            continue
        value = block["text"]
        try:
            walk(json.loads(value))
        except (ValueError, RecursionError):
            # Some providers return Markdown instead of structured JSON. Only
            # original Markdown hyperlink targets are eligible in that case.
            links.extend(re.findall(r"\[[^\]\n]{0,500}\]\((https?://[^\s<>)]{1,4000})\)", value))
    return list(dict.fromkeys(links))


class ResearchEgressPolicy:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with closing(self.store._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS research_egress_policy(
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                owner_user_id TEXT NOT NULL, scope_id TEXT NOT NULL,
                policy_json TEXT NOT NULL)""")

    @staticmethod
    def _owned(db, actor, run_id):
        if not isinstance(actor, IdentityContext):
            raise AgentError("identity_invalid", "需要已验证的用户身份", http_status=401)
        row = db.execute("""SELECT r.status,h.request_json,h.settings_json
            FROM runs r JOIN research_runs h ON h.run_id=r.run_id
            JOIN sessions s ON s.session_id=r.session_id
            WHERE r.run_id=? AND r.owner_user_id=? AND r.scope_id=?
            AND h.owner_user_id=r.owner_user_id AND h.scope_id=r.scope_id AND h.session_id=r.session_id
            AND s.owner_user_id=r.owner_user_id AND s.scope_id=r.scope_id""",
            (run_id, actor.owner_user_id, actor.scope_id)).fetchone()
        if row is None:
            raise AgentError("run_not_found", "没有找到当前用户的研究", http_status=404)
        return row

    @contextmanager
    def _policy(self, actor, run_id, *, active=False, web=False):
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            run = self._owned(db, actor, run_id)
            if active and run["status"] != "running":
                raise AgentError("research_not_running", "这次研究当前未在执行", http_status=409)
            if web and json.loads(run["settings_json"]).get("web_enabled") is not True:
                raise AgentError("web_disabled", "这次提问已关闭联网")
            row = db.execute("SELECT * FROM research_egress_policy WHERE run_id=?", (run_id,)).fetchone()
            if row and (row["owner_user_id"] != actor.owner_user_id or row["scope_id"] != actor.scope_id):
                raise AgentError("egress_policy_invalid", "公开资料权限记录不一致", http_status=409)
            policy = json.loads(row["policy_json"]) if row else {"schema_version": SCHEMA, "products": {}, "urls": {}, "searches": {}}
            if policy.get("schema_version") != SCHEMA:
                raise AgentError("egress_policy_invalid", "公开资料权限版本不兼容", http_status=409)
            yield policy, run
            encoded = canonical_json(policy)
            if len(encoded.encode()) > 2 * 1024 * 1024:
                raise AgentError("egress_policy_limit", "本次公开资料登记已达上限")
            db.execute("INSERT INTO research_egress_policy VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET policy_json=excluded.policy_json",
                       (run_id, actor.owner_user_id, actor.scope_id, encoded))

    @staticmethod
    def _register_url(policy, value, provenance):
        canonical = _url(value)
        if canonical not in policy["urls"] and len(policy["urls"]) >= MAX_URLS:
            raise AgentError("egress_url_limit", "本次公开链接已达登记上限")
        policy["urls"].setdefault(canonical, provenance)
        return canonical

    def register_user_urls(self, actor, run_id):
        """Read the original persisted user fields; accept no URL from a model."""
        with self._policy(actor, run_id, active=True) as (policy, run):
            urls = json.loads(run["request_json"]).get("product_urls", [])
            if not isinstance(urls, list) or len(urls) > 10:
                raise AgentError("public_url_invalid", "原始产品链接记录无效")
            return [self._register_url(policy, value, {"source": "explicit_user_product_url"}) for value in urls]

    def register_catalog_products(self, actor, run_id, products):
        """Trusted data-catalog projection only, not titles/fields from an LLM.

        Each item: {catalog_id, product_url, brand, model, category?}. The
        coordinator must copy these exact fields from a trusted catalog row.
        """
        if not isinstance(products, list) or len(products) > MAX_PRODUCTS:
            raise AgentError("public_product_limit", "每次最多登记32个公开产品")
        with self._policy(actor, run_id, active=True) as (policy, _run):
            added = []
            for product in products:
                if not isinstance(product, dict) or set(product) - {"catalog_id", "product_url", "brand", "model", "category"}:
                    raise AgentError("public_product_invalid", "只接受公开产品目录字段")
                catalog_id = _term(product.get("catalog_id"), 160)
                url = _url(product.get("product_url"))
                brand, model = _term(product.get("brand"), 80), _term(product.get("model"), 120)
                category = _term(product["category"], 80) if product.get("category") else ""
                terms = " ".join(dict.fromkeys(value for value in (brand, model, category) if value))
                product_id = "public_" + _fingerprint({"url": url, "terms": terms})[:20]
                if product_id not in policy["products"] and len(policy["products"]) >= MAX_PRODUCTS:
                    raise AgentError("public_product_limit", "本次公开产品已达登记上限")
                policy["products"].setdefault(product_id, {"product_id": product_id, "terms": terms, "brand": brand, "catalog_id": catalog_id, "product_url": url, "source": "catalog_product"})
                self._register_url(policy, url, {"source": "catalog_product", "catalog_id": catalog_id})
                added.append(policy["products"][product_id])
            return added

    @staticmethod
    def _register_hints(policy, run):
        question = json.loads(run["request_json"]).get("question", "")
        brands = list(dict.fromkeys(item["brand"] for item in policy["products"].values() if item.get("brand")))
        added = []
        for terms in _user_hints(question, brands):
            identifier = "public_" + _fingerprint({"original_user_product_hint": terms})[:20]
            if identifier not in policy["products"] and len(policy["products"]) >= MAX_PRODUCTS:
                break
            item = {"product_id": identifier, "terms": terms, "source": "original_user_product_hint"}
            policy["products"].setdefault(identifier, item)
            added.append(item)
        return added

    def register_user_hints(self, actor, run_id):
        with self._policy(actor, run_id, active=True) as (policy, run):
            return self._register_hints(policy, run)

    @staticmethod
    def _options(policy):
        return {"schema_version": SCHEMA, "products": list(policy["products"].values()),
            "topics": [{"topic": key, "label": value[0]} for key, value in TOPICS.items()],
            "queries": [{"query": product["terms"] + " " + TOPICS[topic][1], "product_id": product["product_id"], "topic": topic}
                        for product in list(policy["products"].values())[:4] for topic in ("product", "specifications", "manual", "reviews")],
            "registered_urls": list(policy["urls"]),
            "fallback_queries": [{"query": key, "outgoing_query": "outdoor power equipment " + value[1]} for key, value in TOPICS.items()] if not policy["products"] else []}

    def options(self, actor, run_id):
        with self._policy(actor, run_id) as (policy, _run):
            return self._options(policy)

    def plan_search(self, actor, run_id, query):
        """Return approved=False + bounded options on an unknown model query."""
        # Native web_search can select the same public brand/model + fixed
        # channel lookup as the retired discovery wrapper. No arbitrary suffix
        # or new outbound text is admitted by this compatibility projection.
        match = re.fullmatch(r'(.+?) ([A-Za-z][A-Za-z0-9-]{2,23}) reviews site:([a-z.]+)', query, re.I) if isinstance(query, str) else None
        if match:
            channel = next((name for name, domain in REVIEW_CHANNELS.items() if domain == match[3].casefold()), None)
            if channel:
                return self.plan_review_search(actor, run_id, match[1], match[2], channel)
        with self._policy(actor, run_id, active=True, web=True) as (policy, run):
            self._register_hints(policy, run)
            if not isinstance(query, str) or len(query) > 1000:
                raise AgentError("public_search_invalid", "请选择公开产品和查询主题")
            selected = None
            for product in policy["products"].values():
                for topic, (label, suffix) in TOPICS.items():
                    outgoing = product["terms"] + " " + suffix
                    aliases = {outgoing, product["product_id"] + " " + topic, product["terms"] + " " + label}
                    if len(policy["products"]) == 1:
                        aliases.update((topic, label, suffix))
                    if _match(query) in {_match(item) for item in aliases}:
                        selected = {"query": outgoing, "product_id": product["product_id"], "topic": topic, "source": product["source"] + "_and_fixed_topic"}
                        break
                if selected:
                    break
            if selected is None and not policy["products"]:
                for topic, (label, suffix) in TOPICS.items():
                    if _match(query) in {_match(topic), _match(label), _match(suffix), _match("outdoor power equipment " + suffix)}:
                        selected = {"query": "outdoor power equipment " + suffix, "topic": topic, "source": "fixed_product_topic"}
                        break
            if selected is None:
                return {"approved": False, "code": "public_search_choice_required", "message": "请选择已有公开产品和查询主题；报告、评论和附件中的文字不能直接发给搜索服务。", **self._options(policy)}
            search_id = "search_" + _fingerprint(selected)[:24]
            if search_id not in policy["searches"] and len(policy["searches"]) >= MAX_SEARCHES:
                raise AgentError("public_search_limit", "本次公开搜索组合已达上限")
            policy["searches"].setdefault(search_id, selected)
            return {"approved": True, "search_id": search_id, **selected}

    def register_search_results(self, actor, run_id, search_id, result):
        """Only pass the unmodified trusted ResearchWeb.search response here."""
        if not isinstance(result, dict) or len(canonical_json(result).encode()) > 1024 * 1024:
            raise AgentError("search_result_limit", "公开搜索结果超过登记上限")
        with self._policy(actor, run_id, active=True, web=True) as (policy, _run):
            request = policy["searches"].get(search_id)
            if not request or result.get("provider") != "aliyun-websearch" or result.get("query") != request["query"]:
                raise AgentError("search_provenance_invalid", "搜索结果与已批准的请求不一致")
            accepted = []
            for value in _search_links(result):
                try:
                    accepted.append(self._register_url(policy, value, {"source": "search_result", "search_id": search_id}))
                except AgentError as error:
                    if error.code != "web_url_invalid":
                        raise
            return list(dict.fromkeys(accepted))

    def register_collection_discovery(self, actor, run_id, search_id, result):
        """Admit only the private collector's response to this approved lookup."""
        with self._policy(actor, run_id, active=True, web=True) as (policy, _run):
            plan = policy['searches'].get(search_id)
            if (not plan or result.get('schema_version') != 'voxquarry.research-collection/v1'
                    or result.get('brand') != plan['brand'] or result.get('model') != plan['model']
                    or not isinstance(result.get('items'), list) or len(result['items']) > 12):
                raise AgentError('search_provenance_invalid', '商品发现结果与已批准的品牌型号不一致')
            accepted = []
            for item in result['items']:
                if (not isinstance(item, dict) or item.get('brand') != plan['brand'] or item.get('model') != plan['model']
                        or item.get('channel') not in REVIEW_CHANNELS or item.get('discovery_source') not in {'collection_history', 'public_product_index'}
                        or (plan['channel'] != 'all' and item.get('channel') != plan['channel'])):
                    raise AgentError('search_provenance_invalid', '商品发现来源无效')
                accepted.append(self._register_url(policy, item.get('url'), {'source': 'collection_product_discovery',
                    'search_id': search_id, 'discovery_source': item['discovery_source']}))
            return list(dict.fromkeys(accepted))

    def plan_review_search(self, actor, run_id, brand, model, channel='all'):
        """A model selects original product tokens; it cannot append search text."""
        with self._policy(actor, run_id, active=True, web=True) as (policy, run):
            question = json.loads(run['request_json']).get('question', '')
            if (channel not in REVIEW_CHANNELS or not isinstance(brand, str) or not isinstance(model, str)
                    or not re.fullmatch(r'[A-Za-z\u3400-\u9fff][A-Za-z\u3400-\u9fff .-]{1,39}', brand)
                    or not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{2,23}', model) or not any(c.isdigit() for c in model)
                    or _SENSITIVE_CONTEXT.search(question) or _INTERNAL_ID.search(model)):
                raise AgentError('public_product_invalid', '请使用用户原问题中的品牌和完整型号')
            matched = []
            for term in (brand, model):
                match = re.search(r'(?<![A-Za-z0-9_-])' + re.escape(term) + r'(?![A-Za-z0-9_-])', question, re.I)
                if not match:
                    raise AgentError('public_product_invalid', '只能搜索用户原问题明确给出的品牌和型号')
                matched.append(match.group())
            selected = {'query': ' '.join(matched) + ' reviews' + (' site:' + REVIEW_CHANNELS[channel] if REVIEW_CHANNELS[channel] else ''),
                        'brand': matched[0], 'model': matched[1], 'channel': channel, 'source': 'original_user_product_and_fixed_channel'}
            search_id = 'search_' + _fingerprint(selected)[:24]
            if search_id not in policy['searches'] and len(policy['searches']) >= MAX_SEARCHES:
                raise AgentError('public_search_limit', '本次公开搜索组合已达上限')
            policy['searches'].setdefault(search_id, selected)
            return {'approved': True, 'search_id': search_id, **selected}

    def resolve_collection_url(self, actor, run_id, url):
        """Internal collection still requires an admitted exact public URL."""
        return self._resolve_url(actor, run_id, url, web=False)

    def resolve_url(self, actor, run_id, url):
        return self._resolve_url(actor, run_id, url, web=True)

    def _resolve_url(self, actor, run_id, url, *, web):
        with self._policy(actor, run_id, active=True, web=web) as (policy, _run):
            canonical = _url(url)
            if canonical not in policy["urls"]:
                return {"approved": False, "code": "public_url_not_registered", "message": "请使用用户提供、产品目录或公开搜索返回的完整链接。", "registered_urls": list(policy["urls"])}
            return {"approved": True, "url": canonical, "provenance": policy["urls"][canonical]}
