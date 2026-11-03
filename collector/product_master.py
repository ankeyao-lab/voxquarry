import hashlib
import json
import math
import os
import re
import threading
import time
import unicodedata
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

try:
    import requests
except ImportError:  # Quality-gate runtime does not install service dependencies.
    requests = None


SCHEMA_VERSION = "retail-product-master/v2"
HASH_ALGORITHM = "sha256-canonical-json/v1"
AUTHORITY = "chengmu"
ELIGIBILITY_POLICY = "retail-analysis-eligibility/v1"
MODEL_ID_RE = re.compile(r"^pvr_[0-9a-f]{20}$")
SNAPSHOT_ID_RE = re.compile(r"^pms_[0-9a-f]{64}$")
RECORD_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PRODUCT_MASTER_MODELS_PATH = "/retail/api/v2/product-master/models"
PRODUCT_MASTER_READINESS_SCHEMA_VERSION = "voxquarry.product-master-readiness/v1"


class ProductMasterError(RuntimeError):
    def __init__(self, code, message, *, status_code=422, detail=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.detail = dict(detail or {})

    def api_detail(self):
        return {"code": self.code, "message": str(self), **self.detail}


def normalize_product_master_endpoint(value):
    """Accept only an origin base URL or the exact governed v2 models URL."""
    raw = str(value or "").strip()
    if not raw:
        raise ProductMasterError(
            "product_master_endpoint_missing",
            "产品库产品主数据接口未配置",
            status_code=503,
            detail={"expected_path": PRODUCT_MASTER_MODELS_PATH},
        )
    try:
        parsed = urlsplit(raw)
        # Accessing port also validates malformed values such as host:abc.
        parsed.port
    except ValueError as exc:
        raise ProductMasterError(
            "product_master_endpoint_invalid",
            "产品库产品主数据接口地址非法",
            status_code=503,
            detail={"expected_path": PRODUCT_MASTER_MODELS_PATH},
        ) from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProductMasterError(
            "product_master_endpoint_invalid",
            "产品库产品主数据接口必须是无凭据、无查询参数的 HTTP(S) 地址",
            status_code=503,
            detail={"expected_path": PRODUCT_MASTER_MODELS_PATH},
        )
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path == "/":
        endpoint_mode = "base_url"
        path = PRODUCT_MASTER_MODELS_PATH
    elif path.rstrip("/") == PRODUCT_MASTER_MODELS_PATH:
        endpoint_mode = "full_endpoint"
        path = PRODUCT_MASTER_MODELS_PATH
    else:
        raise ProductMasterError(
            "product_master_endpoint_invalid",
            "产品库产品主数据接口只能配置服务根地址或完整 v2 models endpoint",
            status_code=503,
            detail={"expected_path": PRODUCT_MASTER_MODELS_PATH},
        )
    netloc = parsed.netloc.casefold()
    return urlunsplit((parsed.scheme.casefold(), netloc, path, "", "")), endpoint_mode


def _canonical_number(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON rejects non-finite numbers")
    decimal_value = Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError("canonical JSON rejects non-finite numbers")
    rendered = format(decimal_value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", ""} else rendered


def canonical_json(value):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return _canonical_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda item: str(item))
        return "{" + ",".join(
            f"{canonical_json(str(key))}:{canonical_json(value[key])}" for key in keys
        ) + "}"
    raise ValueError(f"canonical JSON does not support {type(value).__name__}")


def canonical_sha256(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _identity_token(value):
    return "".join(char for char in str(value or "").casefold() if char.isalnum())


def _optional_exact_token(value):
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def validate_record(record):
    if not isinstance(record, dict):
        raise ProductMasterError("product_master_contract_invalid", "产品库产品记录不是对象")
    required = {
        "model_id", "entity_version", "product_kind", "category", "brand",
        "model_number", "product_name", "energy_type", "voltage_platform",
        "voltage_platform_null_reason", "bundle", "analysis_eligibility",
        "review_status", "reviewed_at", "record_snapshot_hash",
    }
    if set(record) != required:
        raise ProductMasterError("product_master_contract_invalid", "产品库产品记录字段不符合 v2 契约")
    if not MODEL_ID_RE.fullmatch(str(record["model_id"])):
        raise ProductMasterError("product_master_contract_invalid", "产品库 model_id 非法")
    if not isinstance(record["entity_version"], int) or record["entity_version"] < 1:
        raise ProductMasterError("product_master_contract_invalid", "产品库 entity_version 非法")
    if record["product_kind"] not in {"single_product", "bundle"}:
        raise ProductMasterError("product_master_contract_invalid", "产品库 product_kind 非法")
    category = record.get("category") or {}
    brand = record.get("brand") or {}
    if set(category) != {"category_id", "code", "name", "name_zh"} or set(brand) != {"brand_id", "canonical_name"}:
        raise ProductMasterError("product_master_contract_invalid", "产品库品牌或品类投影字段非法")
    if not re.fullmatch(r"cat_[a-z0-9_]+", str(category.get("category_id") or "")):
        raise ProductMasterError("product_master_contract_invalid", "产品库 category_id 非法")
    if not re.fullmatch(r"[a-z0-9_]+", str(category.get("code") or "")):
        raise ProductMasterError("product_master_contract_invalid", "产品库 category code 非法")
    if not re.fullmatch(r"brd_[0-9a-f]{20}", str(brand.get("brand_id") or "")):
        raise ProductMasterError("product_master_contract_invalid", "产品库 brand_id 非法")
    eligibility = record.get("analysis_eligibility") or {}
    if (
        set(eligibility) != {"eligible", "reason", "policy_version"}
        or eligibility.get("policy_version") != ELIGIBILITY_POLICY
        or not isinstance(eligibility.get("eligible"), bool)
        or eligibility.get("reason") not in {
            None, "cross_category_bundle", "bundle_primary_unresolved",
            "bundle_primary_category_mismatch",
        }
    ):
        raise ProductMasterError("product_master_contract_invalid", "产品库 analysis_eligibility 非法")
    if eligibility["eligible"] != (eligibility.get("reason") is None):
        raise ProductMasterError("product_master_contract_invalid", "产品库分析准入与原因互斥关系非法")
    platform = record.get("voltage_platform")
    null_reason = record.get("voltage_platform_null_reason")
    if record.get("energy_type") not in {None, "Gas", "AC", "DC"} or record.get("review_status") not in {"approved", "published"}:
        raise ProductMasterError("product_master_contract_invalid", "产品库能源或审核状态非法")
    if platform is None:
        if null_reason not in {"not_dc", "brand_unresolved", "voltage_unverified"}:
            raise ProductMasterError("product_master_contract_invalid", "产品库电压平台空值原因非法")
    else:
        if set(platform) != {
            "voltage_platform_id", "brand_id", "canonical_voltage",
            "voltage_value", "voltage_unit", "identity_version",
        }:
            raise ProductMasterError("product_master_contract_invalid", "产品库电压平台字段非法")
        expected_key = (brand.get("brand_id"), platform.get("canonical_voltage"))
        if (
            platform.get("brand_id") != expected_key[0]
            or platform.get("identity_version") != "brand-voltage/v1"
            or not re.fullmatch(r"vpf_[0-9a-f]{20}", str(platform.get("voltage_platform_id") or ""))
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?V", str(platform.get("canonical_voltage") or ""))
            or platform.get("voltage_unit") != "V"
            or not isinstance(platform.get("voltage_value"), (int, float))
            or isinstance(platform.get("voltage_value"), bool)
            or platform.get("voltage_value") <= 0
            or null_reason is not None
        ):
            raise ProductMasterError("product_master_contract_invalid", "产品库电压平台身份非法")
    if record.get("energy_type") in {None, "Gas", "AC"} and not (
        platform is None and null_reason == "not_dc"
    ):
        raise ProductMasterError("product_master_contract_invalid", "非 DC 产品不得生成电压平台")
    if record.get("energy_type") == "DC" and null_reason == "not_dc":
        raise ProductMasterError("product_master_contract_invalid", "DC 产品电压平台空值原因非法")
    bundle = record.get("bundle")
    if record["product_kind"] == "single_product" and not (
        bundle is None and eligibility["eligible"] and eligibility.get("reason") is None
    ):
        raise ProductMasterError("product_master_contract_invalid", "产品库单品组套或准入关系非法")
    if record["product_kind"] == "bundle" and not isinstance(bundle, dict):
        raise ProductMasterError("product_master_contract_invalid", "产品库组套结构缺失")
    if isinstance(bundle, dict):
        if set(bundle) != {"scope", "primary_model_id", "members"} or bundle.get("scope") not in {"same_category", "cross_category"} or not isinstance(bundle.get("members"), list):
            raise ProductMasterError("product_master_contract_invalid", "产品库组套字段非法")
        primary_model_id = bundle.get("primary_model_id")
        if primary_model_id is not None and not MODEL_ID_RE.fullmatch(str(primary_model_id)):
            raise ProductMasterError("product_master_contract_invalid", "产品库组套主型号身份非法")
    cross_scope = isinstance(bundle, dict) and bundle.get("scope") == "cross_category"
    if (category.get("code") == "cross_category_bundle") != cross_scope:
        raise ProductMasterError("product_master_contract_invalid", "跨品类组套 scope 与品类不一致")
    if category.get("code") == "cross_category_bundle" and not (
        record.get("product_kind") == "bundle"
        and cross_scope
        and eligibility.get("eligible") is False
        and eligibility.get("reason") == "cross_category_bundle"
    ):
        raise ProductMasterError("product_master_contract_invalid", "跨品类组套契约不完整")
    for member in (bundle or {}).get("members", []):
        if not isinstance(member, dict) or set(member) != {
            "role", "member_model_id", "member_label", "resolution_status",
            "quantity", "evidence",
        }:
            raise ProductMasterError("product_master_contract_invalid", "产品库组套成员字段非法")
        if member.get("role") not in {"primary_product", "included_product", "battery", "charger", "accessory", "other"} or member.get("resolution_status") not in {"resolved", "unresolved"} or not isinstance(member.get("quantity"), int) or member.get("quantity") < 1:
            raise ProductMasterError("product_master_contract_invalid", "产品库组套成员枚举或数量非法")
        evidence = member.get("evidence")
        if not isinstance(evidence, dict) or set(evidence) != {"source_url", "source_type", "source_snapshot_hash", "observed_at"}:
            raise ProductMasterError("product_master_contract_invalid", "产品库组套成员证据字段非法")
        resolved = member.get("resolution_status") == "resolved"
        member_model_id = member.get("member_model_id")
        if resolved and not MODEL_ID_RE.fullmatch(str(member_model_id or "")):
            raise ProductMasterError("product_master_contract_invalid", "已解析组套成员缺少稳定身份")
        if not resolved and (
            member_model_id is not None or not str(member.get("member_label") or "").strip()
        ):
            raise ProductMasterError("product_master_contract_invalid", "未解析组套成员身份或标签非法")
    supplied_hash = str(record.get("record_snapshot_hash") or "")
    if not RECORD_HASH_RE.fullmatch(supplied_hash):
        raise ProductMasterError("product_master_contract_invalid", "产品库记录哈希格式非法")
    content = {key: value for key, value in record.items() if key != "record_snapshot_hash"}
    expected_hash = "sha256:" + canonical_sha256(content)
    if supplied_hash != expected_hash:
        raise ProductMasterError("product_master_contract_invalid", "产品库记录哈希校验失败")
    return record


def validate_snapshot(payload):
    if not isinstance(payload, dict):
        raise ProductMasterError("product_master_contract_invalid", "产品库快照不是对象")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ProductMasterError("product_master_contract_invalid", "产品库 schema_version 不匹配")
    if payload.get("hash_algorithm") != HASH_ALGORITHM or payload.get("authority") != AUTHORITY:
        raise ProductMasterError("product_master_contract_invalid", "产品库权威或哈希算法不匹配")
    snapshot_id = str(payload.get("snapshot_id") or "")
    if not SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise ProductMasterError("product_master_contract_invalid", "产品库 snapshot_id 非法")
    scope = payload.get("scope") or {}
    if scope.get("review_status") != ["approved", "published"]:
        raise ProductMasterError("product_master_contract_invalid", "产品库快照不是 approved/published 投影")
    pagination = payload.get("pagination") or {}
    records = payload.get("records")
    if pagination.get("mode") != "complete" or not isinstance(records, list) or pagination.get("total_records") != len(records):
        raise ProductMasterError("product_master_contract_invalid", "产品库快照分页或记录总数不完整")
    platform_ids = {}
    platform_keys = {}
    for record in records:
        validate_record(record)
        platform = record.get("voltage_platform")
        if platform:
            platform_id = platform["voltage_platform_id"]
            platform_key = (platform["brand_id"], platform["canonical_voltage"])
            if platform_id in platform_ids and platform_ids[platform_id] != platform_key:
                raise ProductMasterError("product_master_contract_invalid", "产品库电压平台跨品牌或电压冲突")
            if platform_key in platform_keys and platform_keys[platform_key] != platform_id:
                raise ProductMasterError("product_master_contract_invalid", "产品库品牌电压平台身份不唯一")
            platform_ids[platform_id] = platform_key
            platform_keys[platform_key] = platform_id
    ordered = sorted(
        records,
        key=lambda record: (
            record["category"]["code"],
            record["brand"]["canonical_name"].casefold(),
            record["model_number"].casefold(),
            record["model_id"],
        ),
    )
    if [record["model_id"] for record in records] != [record["model_id"] for record in ordered]:
        raise ProductMasterError("product_master_contract_invalid", "产品库产品记录排序不符合 v2 契约")
    expected_snapshot = "pms_" + canonical_sha256({
        "schema_version": SCHEMA_VERSION,
        "authority": AUTHORITY,
        "scope": scope,
        "records": records,
    })
    if snapshot_id != expected_snapshot:
        raise ProductMasterError("product_master_contract_invalid", "产品库快照哈希校验失败")
    return payload


def binding_from_snapshot(payload, *, model_id=None, brand=None, model=None, category=None):
    validate_snapshot(payload)
    records = payload["records"]
    if model_id:
        matches = [record for record in records if record["model_id"] == model_id]
    else:
        brand_token = _identity_token(brand)
        model_token = _identity_token(model)
        category_token = _identity_token(category)
        identity_matches = [record for record in records if (
            _identity_token(record["brand"]["canonical_name"]) == brand_token
            and _identity_token(record["model_number"]) == model_token
        )]
        matches = [record for record in identity_matches if (
                not category_token
                or category_token in {
                    _identity_token(record["category"]["code"]),
                    _identity_token(record["category"]["name"]),
                }
        )]
        # Category text is a preferred consistency hint, not an identity key.
        # Source vocabularies may lag Chengmu's governed category registry. If
        # the hint does not match, only one unique approved brand+model record
        # may recover the authoritative category; ambiguity still fails closed.
        if category_token and not matches:
            matches = identity_matches
    if len(matches) != 1:
        code = "master_identity_not_found" if not matches else "product_master_identity_ambiguous"
        raise ProductMasterError(code, "无法唯一匹配产品库已审核产品身份", detail={"match_count": len(matches)})
    record = matches[0]
    eligibility = record["analysis_eligibility"]
    if not eligibility["eligible"]:
        code = (
            "cross_category_bundle_not_collectible"
            if eligibility.get("reason") == "cross_category_bundle"
            else "product_master_analysis_ineligible"
        )
        message = (
            "该商品是跨品类组套，当前无法做单品类分析，已阻止创建采集任务。"
            if code == "cross_category_bundle_not_collectible"
            else "该商品未通过产品库单品类分析准入，已阻止创建采集任务。"
        )
        raise ProductMasterError(code, message, detail={
            "model_id": record["model_id"],
            "category_code": record["category"]["code"],
            "reason": eligibility.get("reason"),
            "policy_version": eligibility["policy_version"],
            "snapshot_id": payload["snapshot_id"],
            "record_snapshot_hash": record["record_snapshot_hash"],
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": payload["snapshot_id"],
        "model_id": record["model_id"],
        "entity_version": record["entity_version"],
        "record_snapshot_hash": record["record_snapshot_hash"],
        "record": record,
    }


def optional_binding_from_snapshot(payload, *, brand=None, model=None):
    """Return optional exact brand+model enrichment without admission gating."""
    validate_snapshot(payload)
    brand_token = _optional_exact_token(brand)
    model_token = _optional_exact_token(model)
    if not brand_token or not model_token:
        return {
            "status": "unknown",
            "binding": None,
            "quality_flags": ["product_identity_incomplete"],
            "match_count": 0,
        }
    matches = [record for record in payload["records"] if (
        _optional_exact_token(record["brand"]["canonical_name"]) == brand_token
        and _optional_exact_token(record["model_number"]) == model_token
    )]
    if len(matches) != 1:
        return {
            "status": "not_found" if not matches else "ambiguous",
            "binding": None,
            "quality_flags": [
                "product_master_not_found" if not matches
                else "product_master_identity_ambiguous"
            ],
            "match_count": len(matches),
        }
    record = matches[0]
    flags = []
    eligibility = record.get("analysis_eligibility") or {}
    if not eligibility.get("eligible"):
        flags.append(
            eligibility.get("reason") or "product_master_analysis_ineligible"
        )
    return {
        "status": "enriched",
        "binding": {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": payload["snapshot_id"],
            "model_id": record["model_id"],
            "entity_version": record["entity_version"],
            "record_snapshot_hash": record["record_snapshot_hash"],
            "record": record,
        },
        "quality_flags": flags,
        "match_count": 1,
    }


class ProductMasterClient:
    def __init__(
        self, endpoint=None, *, timeout=5, session=None,
        optional_failure_ttl=30,
    ):
        raw_endpoint = str(
            endpoint
            if endpoint is not None
            else os.getenv("VOXQUARRY_PRODUCT_MASTER_URL", "")
        ).strip()
        self.endpoint = ""
        self.endpoint_mode = "missing" if not raw_endpoint else "invalid"
        self._configuration_error = None
        try:
            self.endpoint, self.endpoint_mode = normalize_product_master_endpoint(
                raw_endpoint
            )
        except ProductMasterError as exc:
            self._configuration_error = exc
        self.timeout = float(timeout)
        self.session = session or (requests.Session() if requests is not None else None)
        self._etag = None
        self._snapshot = None
        self._lock = threading.Lock()
        self._optional_state_lock = threading.Lock()
        self._optional_failure_ttl = max(0.0, float(optional_failure_ttl))
        self._optional_failed_until = 0.0
        self._optional_ready_until = 0.0
        self._optional_last_error = None

    def fetch(self, model_id=None):
        with self._lock:
            return self._fetch(model_id=model_id)

    def _fetch(self, model_id=None):
        if self._configuration_error is not None:
            raise ProductMasterError(
                self._configuration_error.code,
                str(self._configuration_error),
                status_code=self._configuration_error.status_code,
                detail=self._configuration_error.detail,
            )
        if self.session is None:
            raise ProductMasterError("product_master_unavailable", "产品库产品主数据 HTTP 运行时不可用", status_code=503)
        headers = {"Accept": "application/json"}
        params = []
        if model_id:
            if not MODEL_ID_RE.fullmatch(str(model_id)):
                raise ProductMasterError("master_identity_not_found", "产品库 model_id 格式非法")
            params.append(("model_id", str(model_id)))
        elif self._etag and self._snapshot:
            headers["If-None-Match"] = self._etag
        try:
            response = self.session.get(self.endpoint, params=params, headers=headers, timeout=self.timeout)
        except Exception as exc:
            raise ProductMasterError("product_master_unavailable", "产品库产品主数据接口不可用", status_code=503) from exc
        if response.status_code == 304 and self._snapshot is not None:
            return self._snapshot
        if response.status_code == 404:
            if model_id is None:
                raise ProductMasterError(
                    "product_master_endpoint_invalid",
                    "产品库产品主数据完整投影 endpoint 返回 HTTP 404",
                    status_code=503,
                    detail={"expected_path": PRODUCT_MASTER_MODELS_PATH},
                )
            try:
                error = response.json().get("error") or {}
            except (ValueError, AttributeError):
                error = {}
            code = error.get("code") or "master_identity_not_found"
            raise ProductMasterError(code, error.get("message") or "产品库未找到该产品身份", status_code=404)
        if response.status_code != 200:
            raise ProductMasterError("product_master_unavailable", f"产品库产品主数据接口返回 HTTP {response.status_code}", status_code=503)
        try:
            payload = validate_snapshot(response.json())
        except ValueError as exc:
            raise ProductMasterError("product_master_contract_invalid", "产品库产品主数据响应不是合法 JSON") from exc
        etag = str(response.headers.get("ETag") or "").strip()
        if etag != f'"{payload["snapshot_id"]}"':
            raise ProductMasterError(
                "product_master_contract_invalid",
                "产品库产品主数据 ETag 与 snapshot_id 不一致",
            )
        self._etag = etag
        self._snapshot = payload
        return payload

    def readiness(self):
        result = {
            "schema_version": PRODUCT_MASTER_READINESS_SCHEMA_VERSION,
            "status": "unavailable",
            "configured": self._configuration_error is None and bool(self.endpoint),
            "endpoint_mode": self.endpoint_mode,
            "expected_endpoint_path": PRODUCT_MASTER_MODELS_PATH,
            "upstream_schema_version": None,
            "authority": None,
            "snapshot_id": None,
            "record_count": None,
            "error": None,
        }
        try:
            payload = self._fetch_optional()
        except ProductMasterError as exc:
            result["error"] = exc.api_detail()
            return result
        result.update({
            "status": "ready",
            "configured": True,
            "upstream_schema_version": payload["schema_version"],
            "authority": payload["authority"],
            "snapshot_id": payload["snapshot_id"],
            "record_count": len(payload["records"]),
        })
        return result

    def resolve(self, *, model_id=None, brand=None, model=None, category=None, snapshot_id=None):
        # A UI round-trip pins the full discovery snapshot. Re-fetch that same
        # scope (normally 304) before selecting its exact Model ID; an exact
        # model query has a different scope and therefore a different valid
        # snapshot_id.
        payload = self.fetch() if snapshot_id else self.fetch(model_id=model_id)
        return binding_from_snapshot(payload, model_id=model_id, brand=brand, model=model, category=category)

    def resolve_many(self, candidates):
        """Resolve a preview batch from one validated full snapshot fetch."""
        payload = self.fetch()
        results = []
        for candidate in candidates:
            try:
                results.append(binding_from_snapshot(
                    payload,
                    model_id=candidate.get("model_id"),
                    brand=candidate.get("brand"),
                    model=candidate.get("model"),
                    category=candidate.get("category"),
                ))
            except ProductMasterError as exc:
                results.append(exc)
        return results

    def enrich_optional(self, *, brand=None, model=None):
        """Best-effort enrichment; every error becomes audit state, never a gate."""
        try:
            payload = self._fetch_optional()
            return optional_binding_from_snapshot(
                payload, brand=brand, model=model,
            )
        except ProductMasterError as exc:
            return {
                "status": "unavailable",
                "binding": None,
                "quality_flags": [exc.code],
                "match_count": None,
            }

    def enrich_optional_many(self, candidates):
        """Resolve one batch from one optional snapshot without blocking rows."""
        try:
            payload = self._fetch_optional()
        except ProductMasterError as exc:
            return [{
                "status": "unavailable",
                "binding": None,
                "quality_flags": [exc.code],
                "match_count": None,
            } for _candidate in candidates]
        return [optional_binding_from_snapshot(
            payload,
            brand=candidate.get("brand"),
            model=candidate.get("model"),
        ) for candidate in candidates]

    def _fetch_optional(self):
        """Fetch optional enrichment with a bounded failure circuit.

        One slow or invalid upstream probe must not impose the same timeout on
        every row of a bulk create. A successful fetch clears the circuit;
        failed probes are retried after the short TTL.
        """
        with self._optional_state_lock:
            now = time.monotonic()
            if self._snapshot is not None and now < self._optional_ready_until:
                return self._snapshot
            if (
                self._optional_last_error is not None
                and now < self._optional_failed_until
            ):
                exc = self._optional_last_error
                raise ProductMasterError(
                    exc.code,
                    str(exc),
                    status_code=exc.status_code,
                    detail=exc.detail,
                )
            try:
                payload = self.fetch()
            except ProductMasterError as exc:
                self._optional_last_error = exc
                self._optional_failed_until = (
                    time.monotonic() + self._optional_failure_ttl
                )
                raise
            self._optional_last_error = None
            self._optional_failed_until = 0.0
            self._optional_ready_until = (
                time.monotonic() + self._optional_failure_ttl
            )
            return payload
