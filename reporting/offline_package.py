from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from reporting.config import DEFAULT_FRONTEND_ROOT
from reporting.category_vocabulary import category_catalog
from reporting.contracts import (
    FACTOR_IDS,
    SCENE_FACTORS,
    SCENE_IDS,
    UNAVAILABLE_FACTOR_IDS,
    default_scope,
)
from reporting.validation import validate_report


SCENE_COPY = {
    "satisfaction-overview": (
        "满意度总览",
        "评分、好差评率、推荐率和趋势如何？",
    ),
    "pain-and-failure": (
        "痛点与故障分析",
        "哪些故障、症状与使用后果最突出？",
    ),
    "product-attribute-performance": (
        "产品属性表现",
        "各属性的提及、Pros / Cons 和 Kano 候选如何？",
    ),
    "needs-and-use-cases": (
        "用户需求和使用场景",
        "用户要完成什么任务，受什么环境和能力约束？",
    ),
    "product-opportunity": (
        "产品机会",
        "哪些未满足结果形成可追溯的网评证据机会？",
    ),
    "brand-product-comparison": (
        "品牌 / 产品竞品对比",
        "品牌和产品在评分、覆盖和效应量上有何差异？",
    ),
    "channel-comparison": (
        "渠道差异",
        "渠道样本、正文覆盖与表现有何差异？",
    ),
    "model-time-comparison": (
        "型号 / 时间段对比",
        "型号和时期的表现是否有稳定变化？",
    ),
    "user-profile": (
        "用户特征与画像",
        "评论中明确表达了哪些年龄、特征和规则群组？",
    ),
    "smart-opportunity": (
        "智能化机会",
        "A1–A8 中哪些感知、判断、动作与反馈机会有证据？",
    ),
}

FACTOR_COPY = {
    "ratings": ("评分、好评率与趋势", "available"),
    "recommendation": ("推荐率与字段覆盖", "available"),
    "written-coverage": ("正文与字段覆盖", "available"),
    "attributes": ("属性提及与 Pros / Cons", "available"),
    "pain-points": ("痛点、故障与后果", "available"),
    "use-cases": ("需求与使用场景", "available"),
    "explicit-age": ("明确年龄与用户特征", "conditional"),
    "user-traits": ("规则用户群组", "conditional"),
    "keyword-document-frequency": ("n-gram 文档频次", "available"),
    "factor-cooccurrence": ("原子因子文档共现", "available"),
    "kano-candidates": ("网评 Kano 候选", "conditional"),
    "product-opportunities": ("产品机会证据", "available"),
    "smart-signals": ("A1–A8 智能化信号", "conditional"),
    "verified-purchaser": ("验证购买", "unavailable"),
}

PACKAGE_VERSION = "voxquarry-reporting-offline/2.2.1"
REPORTING_PREFIX = "/reporting/api/v1"
SAFE_EVIDENCE_GROUP_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _json_text(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        sort_keys=pretty,
    )


def _table_groups(report: dict[str, Any], table_id: str) -> list[str]:
    table = next(
        (item for item in report.get("tables", []) if item["table_id"] == table_id),
        None,
    )
    if table is None:
        return []
    return sorted(
        {
            str(row["group"])
            for row in table.get("rows", [])
            if row.get("group") not in {None, ""}
        }
    )


def _fallback_catalog(report: dict[str, Any]) -> dict[str, Any]:
    scenes = [
        {
            "id": scene_id,
            "label": SCENE_COPY[scene_id][0],
            "question": SCENE_COPY[scene_id][1],
        }
        for scene_id in SCENE_IDS
    ]
    factors = []
    for factor_id in (*FACTOR_IDS, *UNAVAILABLE_FACTOR_IDS):
        label, status = FACTOR_COPY[factor_id]
        item = {
            "id": factor_id,
            "label": label,
            "status": status,
            "recommended_scenes": [
                scene_id
                for scene_id, factor_ids in SCENE_FACTORS.items()
                if factor_id in factor_ids
            ],
        }
        if factor_id == "verified-purchaser":
            item["reason"] = "当前数据无法区分 false 与 missing"
        factors.append(item)

    channels = _table_groups(report, "channel_comparison")
    brands = _table_groups(report, "brand_comparison")
    models = _table_groups(report, "model_comparison")
    market_regions = list(
        report.get("scope", {}).get("market_regions") or []
    )
    categories = list(report.get("scope", {}).get("categories") or ["割草机"])
    return {
        "channels": channels,
        "brands": brands,
        "categories": categories,
        "category_registry": category_catalog(),
        "models": models,
        "market_regions": market_regions,
        "models_by_category": {
            category: models for category in categories
        },
        "brands_by_category": {
            category: brands for category in categories
        },
        "channels_by_category": {
            category: channels for category in categories
        },
        "market_regions_by_category": {
            category: market_regions for category in categories
        },
        "capabilities": {
            "verified_purchaser": {
                "status": "unavailable",
                "reason": "当前采集契约无法区分 false 与 missing",
            },
            "explicit_age": {
                "status": "conditional",
                "reason": "只统计明确年龄；不推断缺失年龄",
            },
            "syndication": {
                "status": "conditional",
                "reason": "来源标志不完整；实体业务键始终参与去重",
            },
        },
        "scenes": scenes,
        "factors": factors,
        "defaults": {
            "scope": report.get("scope") or default_scope(),
            "primary_direction": report["research_plan"]["primary_direction"],
            "analysis_modules": report["research_plan"]["analysis_modules"],
            "factors": report["research_plan"]["factors"],
        },
    }


def _report_card(report: dict[str, Any]) -> dict[str, Any]:
    limitations = report.get("limitations") or []
    severities = {item.get("severity") for item in limitations}
    grade = (
        "C"
        if "blocking" in severities
        else "B"
        if severities & {"material", "blocking-for-metric"}
        else "A"
    )
    research_plan = report.get("research_plan") or {}
    return {
        "report_id": report["report_id"],
        "title": report["title"],
        "status": report.get("status", "completed"),
        "primary_direction": research_plan.get("primary_direction"),
        "analysis_modules": research_plan.get("analysis_modules", []),
        "factors": research_plan.get("factors", []),
        "scope": report.get("scope") or {},
        "samples": report.get("samples") or {},
        "summary_text": report.get("summary_text"),
        "quality": {
            "grade": grade,
            "limitation_count": len(limitations),
            "headline": (
                limitations[0]["message"]
                if limitations
                else "未发现主要限制"
            ),
        },
        "analysis_version": report.get("analysis_version"),
        "generated_at": report.get("generated_at"),
        "archived": False,
    }


def _estimate(report: dict[str, Any]) -> dict[str, Any]:
    samples = report.get("samples") or {}
    review_entities = int(samples.get("review_entities") or 0)
    written = int(samples.get("written_review_entities") or 0)
    metrics = {
        item["metric_id"]: item for item in report.get("metrics", [])
    }
    factor_samples = {
        "ratings": int(samples.get("valid_rating_entities") or review_entities),
        "recommendation": int(
            (metrics.get("recommendation_rate") or {}).get("sample_size") or 0
        ),
        "written-coverage": review_entities,
        "attributes": written,
        "pain-points": written,
        "use-cases": written,
        "explicit-age": int(samples.get("age_known_entities") or 0),
        "user-traits": written,
        "keyword-document-frequency": written,
        "factor-cooccurrence": written,
        "kano-candidates": written,
        "product-opportunities": written,
        "smart-signals": written,
    }
    return {
        "occurrences": int(samples.get("occurrences") or review_entities),
        "review_entities": review_entities,
        "written_review_entities": written,
        "written_coverage": written / review_entities if review_entities else 0,
        "factor_samples": factor_samples,
        "offline_fixed_scope": True,
    }


def _offline_evidence_groups(
    report: dict[str, Any],
    *,
    full_evidence_available: bool = False,
) -> dict[str, dict[str, Any]]:
    insight = report.get("product_insight") or {}
    evidence_by_id = {
        str(row["evidence_id"]): row
        for row in insight.get("evidence") or []
    }
    catalog = {
        str(row["evidence_group_id"]): row
        for row in insight.get("evidence_group_catalog") or []
    }
    group_evidence_ids: dict[str, list[str]] = {}

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, group_id in value.items():
                if not key.endswith("evidence_group_id") or not group_id:
                    continue
                prefix = key[: -len("evidence_group_id")]
                evidence_key = f"{prefix}evidence_ids"
                ids = value.get(evidence_key) or []
                bucket = group_evidence_ids.setdefault(str(group_id), [])
                for evidence_id in ids:
                    normalized = str(evidence_id)
                    if normalized in evidence_by_id and normalized not in bucket:
                        bucket.append(normalized)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(insight)
    groups: dict[str, dict[str, Any]] = {}
    for group_id, metadata in catalog.items():
        evidence_ids = group_evidence_ids.get(group_id, [])
        items = [
            evidence_by_id[evidence_id]
            for evidence_id in evidence_ids
            if evidence_id in evidence_by_id
        ]
        groups[group_id] = {
            "group": {
                **metadata,
                "evidence_rows": metadata.get("full_evidence_rows", 0),
                "unique_review_entities": metadata.get(
                    "full_evidence_entities", 0
                ),
                "offline_representative_only": True,
                "full_static_evidence_available": (
                    full_evidence_available
                ),
                "full_evidence_asset": (
                    f"./evidence/groups/{group_id}.json.gz"
                    if full_evidence_available
                    else None
                ),
            },
            "items": items,
        }
    return groups


def _offline_data_js(
    report: dict[str, Any],
    catalog: dict[str, Any],
    *,
    full_evidence_available: bool = False,
) -> str:
    payload = {
        "package_version": PACKAGE_VERSION,
        "report": report,
        "report_card": _report_card(report),
        "catalog": catalog,
        "estimate": _estimate(report),
        "evidence_groups": _offline_evidence_groups(
            report,
            full_evidence_available=full_evidence_available,
        ),
    }
    return f"""(function () {{
  'use strict';
  const bundle = {_json_text(payload)};
  const nativeFetch = window.fetch ? window.fetch.bind(window) : null;
  const reportPrefix = {json.dumps(REPORTING_PREFIX)};
  const fullEvidenceCache = new Map();

  window.__VOXQUARRY_REPORTING_OFFLINE__ = {{
    package_version: bundle.package_version,
    report_id: bundle.report.report_id,
    generated_at: bundle.report.generated_at
  }};

  function jsonResponse(value, status) {{
    return Promise.resolve(new Response(JSON.stringify(value), {{
      status: status || 200,
      headers: {{ 'Content-Type': 'application/json; charset=utf-8' }}
    }}));
  }}

  async function decodeFullEvidence(response) {{
    if (!response.ok) throw new Error(`full evidence HTTP ${{response.status}}`);
    const bytes = new Uint8Array(await response.arrayBuffer());
    let text;
    if (bytes.length >= 2 && bytes[0] === 0x1f && bytes[1] === 0x8b) {{
      if (typeof DecompressionStream === 'undefined') {{
        throw new Error('browser does not support gzip evidence assets');
      }}
      const stream = new Blob([bytes]).stream().pipeThrough(
        new DecompressionStream('gzip')
      );
      text = await new Response(stream).text();
    }} else {{
      text = new TextDecoder('utf-8').decode(bytes);
    }}
    return JSON.parse(text);
  }}

  async function evidenceGroupPayload(groupId, fallback) {{
    if (!fallback.group.full_static_evidence_available || !nativeFetch) {{
      return fallback;
    }}
    if (!fullEvidenceCache.has(groupId)) {{
      const asset = fallback.group.full_evidence_asset;
      fullEvidenceCache.set(groupId, nativeFetch(
        new URL(asset, window.location.href)
      ).then(decodeFullEvidence).catch(() => fallback));
    }}
    return fullEvidenceCache.get(groupId);
  }}

  async function reportingResponse(path, method) {{
    if (path === '/health' && method === 'GET') {{
      return jsonResponse({{ status: 'ok', offline: true }});
    }}
    if (path === '/catalog' && method === 'GET') {{
      return jsonResponse(bundle.catalog);
    }}
    if (path === '/estimate' && method === 'POST') {{
      return jsonResponse(bundle.estimate);
    }}
    if ((path === '/reports' || path.startsWith('/reports?')) && method === 'GET') {{
      return jsonResponse({{
        items: [bundle.report_card],
        summary: {{ total: 1, generating: 0, completed: 1, attention: 0 }}
      }});
    }}
    if (path === '/reports' && method === 'POST') {{
      return jsonResponse({{
        detail: '离线验收包不执行新计算；请连接本地报告服务'
      }}, 409);
    }}

    const evidenceMatch = path.match(/^\\/reports\\/([^/]+)\\/evidence\\/([^/?]+)$/);
    if (evidenceMatch && method === 'GET') {{
      const evidence = bundle.report.evidence.find((item) => item.evidence_id === decodeURIComponent(evidenceMatch[2]));
      return evidence
        ? jsonResponse(evidence)
        : jsonResponse({{ detail: 'evidence not found' }}, 404);
    }}

    const evidenceGroupMatch = path.match(/^\\/reports\\/([^/]+)\\/evidence-groups\\/([^/?]+)(?:\\?.*)?$/);
    if (evidenceGroupMatch && method === 'GET') {{
      const groupId = decodeURIComponent(evidenceGroupMatch[2]);
      const fallback = bundle.evidence_groups[groupId];
      if (!fallback) return jsonResponse({{ detail: 'evidence group not found' }}, 404);
      const payload = await evidenceGroupPayload(groupId, fallback);
      const query = new URLSearchParams(path.includes('?') ? path.split('?')[1] : '');
      const offset = Math.max(0, Number(query.get('offset') || 0));
      const limit = Math.max(1, Math.min(100, Number(query.get('limit') || 50)));
      const filterFields = ['market_region', 'channel', 'brand', 'model', 'rating', 'polarity', 'evidence_type'];
      const filtered = payload.items.filter((item) => {{
        const matchesFields = filterFields.every((field) => {{
          const expected = query.get(field);
          return !expected || String(item[field] == null ? 'unknown' : item[field]) === expected;
        }});
        const needle = String(query.get('q') || '').trim().toLocaleLowerCase();
        const haystack = `${{item.quote || ''}} ${{item.matched_text || ''}} ${{item.label || ''}}`.toLocaleLowerCase();
        return matchesFields && (!needle || haystack.includes(needle));
      }});
      const facets = Object.fromEntries(filterFields.map((field) => {{
        const counts = new Map();
        payload.items.forEach((item) => {{
          const value = String(item[field] == null ? 'unknown' : item[field]);
          counts.set(value, (counts.get(value) || 0) + 1);
        }});
        return [field, [...counts.entries()]
          .map(([value, count]) => ({{ value, count }}))
          .sort((a, b) => b.count - a.count || a.value.localeCompare(b.value))];
      }}));
      const items = filtered.slice(offset, offset + limit);
      const nextOffset = offset + limit < filtered.length ? offset + limit : null;
      const uniqueEntities = new Set(filtered.map((item) => item.review_entity_id || item.review_id || item.evidence_id));
      return jsonResponse({{
        report_id: bundle.report.report_id,
        group: payload.group,
        items,
        page: {{
          offset,
          limit,
          returned: items.length,
          total_evidence_rows: filtered.length,
          total_unique_review_entities: uniqueEntities.size,
          next_offset: nextOffset,
          has_more: nextOffset !== null
        }},
        facets
      }});
    }}

    const reportMatch = path.match(/^\\/reports\\/([^/?]+)$/);
    if (reportMatch && method === 'GET') {{
      return decodeURIComponent(reportMatch[1]) === bundle.report.report_id
        ? jsonResponse(bundle.report)
        : jsonResponse({{ detail: 'report not found' }}, 404);
    }}
    if (path.startsWith('/reports/') && method === 'POST') {{
      return jsonResponse({{ detail: '离线验收包为只读' }}, 409);
    }}
    return jsonResponse({{ detail: 'offline endpoint not found' }}, 404);
  }}

  window.fetch = function (input, init) {{
    const rawUrl = typeof input === 'string' ? input : input.url;
    const method = String((init && init.method) || 'GET').toUpperCase();
    let url;
    try {{
      url = new URL(rawUrl, window.location.href);
    }} catch (error) {{
      return jsonResponse({{ detail: String(error) }}, 400);
    }}
    const marker = url.pathname.indexOf(reportPrefix);
    if (marker >= 0) {{
      const path = url.pathname.slice(marker + reportPrefix.length) + url.search;
      return reportingResponse(path || '/', method);
    }}
    if (url.pathname.endsWith('/scraper/api/jobs')) return jsonResponse([]);
    if (url.pathname.endsWith('/scraper/api/feedback')) return jsonResponse([]);
    if (url.pathname.endsWith('/scraper/api/stats')) {{
      return jsonResponse({{ by: url.searchParams.get('by') || 'channel', groups: [] }});
    }}
    if (url.pathname.endsWith('/scraper/api/master-data')) {{
      return jsonResponse({{
        brands: bundle.catalog.brands.map((name, index) => ({{ id: `offline-brand-${{index + 1}}`, name }})),
        categories: bundle.catalog.categories.map((name, index) => ({{ id: `offline-category-${{index + 1}}`, name }}))
      }});
    }}
    if (url.pathname.endsWith('/queue-status')) return jsonResponse({{ jobs: [] }});
    if (nativeFetch) return nativeFetch(input, init);
    return jsonResponse({{ detail: 'fetch unavailable' }}, 503);
  }};
}})();
"""


def _inject_offline_script(index_html: str) -> str:
    marker = re.compile(
        r'<script\s+src="\./platform-logic\.js(?:\?v=[^"]+)?"></script>'
    )
    if marker.search(index_html) is None:
        raise ValueError("platform-logic script marker is missing")
    return marker.sub(
        lambda match: (
            '<script src="./offline-data.js"></script>\n' + match.group(0)
        ),
        index_html,
        count=1,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _export_full_evidence_assets(
    evidence_path: Path,
    staging: Path,
    report_id: str,
) -> dict[str, Any]:
    evidence_path = evidence_path.resolve()
    if not evidence_path.is_file():
        raise FileNotFoundError(evidence_path)
    connection = sqlite3.connect(
        f"file:{evidence_path}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        metadata = {
            row["key"]: row["value"]
            for row in connection.execute(
                "SELECT key, value FROM metadata"
            ).fetchall()
        }
        if metadata.get("report_id") != report_id:
            raise ValueError(
                "evidence sidecar report_id does not match report"
            )
        groups = connection.execute(
            """
            SELECT evidence_group_id, path, label, representative_count
            FROM evidence_group
            ORDER BY evidence_group_id
            """
        ).fetchall()
        target = staging / "evidence" / "groups"
        target.mkdir(parents=True)
        exported_rows = 0
        exported_bytes = 0
        group_manifest = []
        for group in groups:
            group_id = str(group["evidence_group_id"])
            if not SAFE_EVIDENCE_GROUP_RE.fullmatch(group_id):
                raise ValueError(
                    f"unsafe evidence_group_id: {group_id}"
                )
            rows = [
                json.loads(row["payload_json"])
                for row in connection.execute(
                    """
                    SELECT e.payload_json
                    FROM evidence_group_member gm
                    JOIN evidence e ON e.evidence_id = gm.evidence_id
                    WHERE gm.evidence_group_id = ?
                    ORDER BY gm.stable_rank, e.evidence_id
                    """,
                    (group_id,),
                )
            ]
            unique_entities = {
                str(
                    row.get("review_entity_id")
                    or row.get("review_id")
                    or row["evidence_id"]
                )
                for row in rows
            }
            payload = {
                "group": {
                    **dict(group),
                    "evidence_rows": len(rows),
                    "unique_review_entities": len(unique_entities),
                    "offline_representative_only": False,
                    "full_static_evidence_available": True,
                    "full_evidence_asset": (
                        f"./evidence/groups/{group_id}.json.gz"
                    ),
                },
                "items": rows,
            }
            encoded = _json_text(payload).encode("utf-8")
            compressed = gzip.compress(encoded, compresslevel=9, mtime=0)
            asset = target / f"{group_id}.json.gz"
            asset.write_bytes(compressed)
            exported_rows += len(rows)
            exported_bytes += len(compressed)
            group_manifest.append(
                {
                    "evidence_group_id": group_id,
                    "evidence_rows": len(rows),
                    "unique_review_entities": len(unique_entities),
                    "asset": (
                        f"evidence/groups/{group_id}.json.gz"
                    ),
                    "sha256": hashlib.sha256(compressed).hexdigest(),
                    "bytes": len(compressed),
                }
            )
    finally:
        connection.close()
    manifest = {
        "schema_version": "static-full-evidence/v1",
        "report_id": report_id,
        "source_logical_content_sha256": metadata.get(
            "logical_content_sha256"
        ),
        "evidence_rows": int(metadata.get("evidence_rows") or 0),
        "evidence_groups": len(group_manifest),
        "group_memberships": exported_rows,
        "compressed_bytes": exported_bytes,
        "groups": group_manifest,
    }
    _write_text(
        staging / "evidence" / "manifest.json",
        _json_text(manifest, pretty=True) + "\n",
    )
    return manifest


def build_package(
    report_path: Path,
    output_dir: Path,
    *,
    catalog_path: Path | None = None,
    frontend_root: Path = DEFAULT_FRONTEND_ROOT,
    evidence_path: Path | None = None,
) -> tuple[Path, Path]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report(report)
    catalog = (
        json.loads(catalog_path.read_text(encoding="utf-8"))
        if catalog_path
        else _fallback_catalog(report)
    )

    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="voxquarry-offline-", dir=output_dir.parent
    ) as temporary:
        staging = Path(temporary) / output_dir.name
        staging.mkdir()
        for name in (
            "support.js",
            "platform-logic.js",
            "reporting-app.js",
            "reporting.css",
        ):
            shutil.copy2(frontend_root / name, staging / name)
        theme = frontend_root / "voxquarry-theme.css"
        if theme.is_file():
            shutil.copy2(theme, staging / theme.name)
        shutil.copytree(frontend_root / "vendor", staging / "vendor")
        (staging / "images").mkdir()
        for name in ("voxquarry-logo.png", "voxquarry-wordmark.png"):
            shutil.copy2(frontend_root / "images" / name, staging / "images" / name)
        for name in ("voxquarry-v4", "voxquarry-v5", "voxquarry-v6"):
            brand_asset_root = frontend_root / "images" / name
            if brand_asset_root.is_dir():
                shutil.copytree(brand_asset_root, staging / "images" / name)
        shutil.copytree(
            frontend_root / "images" / "report-categories",
            staging / "images" / "report-categories",
        )

        index_html = _inject_offline_script(
            (frontend_root / "index.html").read_text(encoding="utf-8")
        )
        _write_text(staging / "index.html", index_html)
        full_evidence_manifest = (
            _export_full_evidence_assets(
                evidence_path,
                staging,
                report["report_id"],
            )
            if evidence_path
            else None
        )
        _write_text(
            staging / "START_HERE.html",
            """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0; url=./index.html#insights">
  <title>打开数据采集结构化洞察</title>
</head>
<body>
  <p><a href="./index.html#insights">点击打开数据采集结构化洞察离线验收包</a></p>
  <script>window.location.replace('./index.html#insights');</script>
</body>
</html>
""",
        )
        _write_text(
            staging / "offline-data.js",
            _offline_data_js(
                report,
                catalog,
                full_evidence_available=bool(full_evidence_manifest),
            ),
        )
        _write_text(staging / "report.json", _json_text(report, pretty=True) + "\n")
        _write_text(
            staging / "README.txt",
            f"""数据采集 VoxQuarry｜结构化洞察离线验收包

打开方式
1. 把整个文件夹复制到目标电脑，不要只复制单个 HTML。
2. 双击 START_HERE.html。
3. 推荐使用最新版 Chrome、Edge、Safari 或 Firefox。

本包能力
- 报告列表、筛选和完整单品类报告详情。
- 指标口径、原文证据、章节跳转、JSON 导出和浏览器打印/PDF。
- 创建报告单页数据范围表单可用于产品流程验收。

只读边界
- 不需要网络、Python、数据库或本地服务。
- {"包含每个统计项的压缩完整证据组；通过 HTTP/HTTPS 打开时可全量分页、筛选和搜索。" if full_evidence_manifest else "只包含固化 Report JSON 和页面代表证据；完整证据需连接报告服务。"}
- 不执行新报告计算、归档、重试或生产写入。

报告
- report_id: {report["report_id"]}
- schema_version: {report["schema_version"]}
- analysis_version: {report["analysis_version"]}
- data_snapshot_id: {report["data_snapshot_id"]}
- core_result_hash: {report["hashes"]["core_result"]}
- package_version: {PACKAGE_VERSION}
""",
        )

        files = sorted(
            path
            for path in staging.rglob("*")
            if path.is_file() and path.name not in {"MANIFEST.json", "SHA256SUMS.txt"}
        )
        manifest = {
            "package_version": PACKAGE_VERSION,
            "entrypoint": "START_HERE.html",
            "offline": True,
            "report_id": report["report_id"],
            "schema_version": report["schema_version"],
            "analysis_version": report["analysis_version"],
            "data_snapshot_id": report["data_snapshot_id"],
            "report_hashes": report["hashes"],
            "full_evidence": full_evidence_manifest,
            "files": {
                path.relative_to(staging).as_posix(): {
                    "sha256": _sha256(path),
                    "bytes": path.stat().st_size,
                }
                for path in files
            },
        }
        _write_text(staging / "MANIFEST.json", _json_text(manifest, pretty=True) + "\n")

        checksum_files = sorted(
            path for path in staging.rglob("*") if path.is_file()
        )
        _write_text(
            staging / "SHA256SUMS.txt",
            "".join(
                f"{_sha256(path)}  {path.relative_to(staging).as_posix()}\n"
                for path in checksum_files
                if path.name != "SHA256SUMS.txt"
            ),
        )

        if output_dir.exists():
            raise FileExistsError(f"output already exists: {output_dir}")
        shutil.move(str(staging), str(output_dir))

    zip_path = output_dir.with_suffix(".zip")
    if zip_path.exists():
        raise FileExistsError(f"zip already exists: {zip_path}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                archive.write(
                    path,
                    (Path(output_dir.name) / path.relative_to(output_dir)).as_posix(),
                )
    return output_dir, zip_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a server-free VoxQuarry reporting acceptance package"
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog-json", type=Path)
    parser.add_argument("--frontend-root", type=Path, default=DEFAULT_FRONTEND_ROOT)
    parser.add_argument(
        "--evidence-db",
        type=Path,
        help="Immutable report Evidence SQLite to publish as compressed full groups",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir, zip_path = build_package(
        args.report,
        args.output,
        catalog_path=args.catalog_json,
        frontend_root=args.frontend_root,
        evidence_path=args.evidence_db,
    )
    print(
        _json_text(
            {
                "folder": str(output_dir),
                "zip": str(zip_path),
                "entrypoint": str(output_dir / "START_HERE.html"),
            },
            pretty=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
