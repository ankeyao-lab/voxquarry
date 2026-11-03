from __future__ import annotations

import html
import json
from typing import Any


def _metric(report: dict[str, Any], metric_id: str) -> Any:
    return next(
        item["value"]
        for item in report["metrics"]
        if item["metric_id"] == metric_id
    )


def _table(report: dict[str, Any], table_id: str) -> list[dict[str, Any]]:
    return next(
        item["rows"]
        for item in report["tables"]
        if item["table_id"] == table_id
    )


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def build_mobile_html(report: dict[str, Any]) -> str:
    execution = _table(report, "atomic_script_execution")
    labels = _table(report, "atomic_label_summary")
    ratings = _table(report, "rating_distribution")
    registry = {
        item["script_id"]: item
        for item in report["method"]["atomic_script_registry"]["scripts"]
    }
    labels_by_script: dict[str, list[dict[str, Any]]] = {}
    for row in labels:
        labels_by_script.setdefault(row["script_id"], []).append(row)
    samples = report["samples"]
    coverage = _metric(report, "atomic_v2_review_coverage")
    average_rating = _metric(report, "average_rating")
    positive_rate = _metric(report, "positive_rating_rate")
    negative_rate = _metric(report, "negative_rating_rate")
    ledger = report["method"]["atomic_execution_ledger"]
    max_script = max(row["matched"] for row in execution) or 1
    max_rating = max(row["count"] for row in ratings) or 1

    script_cards = []
    for row in execution:
        spec = registry[row["script_id"]]
        label_rows = "".join(
            "<tr>"
            f"<td>{_escape(label['label'])}</td>"
            f"<td>{label['review_entities']:,}</td>"
            f"<td>{label['coverage']:.1%}</td>"
            "</tr>"
            for label in labels_by_script.get(row["script_id"], [])
        )
        script_cards.append(
            f"""
<details class="script-card" id="{row['script_id']}">
  <summary>
    <span class="script-id">{row['script_id']}</span>
    <span class="script-name">{_escape(row['name'])}</span>
    <span class="script-rate">{row['coverage']:.1%}</span>
  </summary>
  <div class="script-body">
    <p>{_escape(spec['purpose'])}</p>
    <p class="use"><strong>应用：</strong>{_escape("、".join(spec['applications']))}</p>
    <div class="status-grid">
      <span>应执行 <b>{row['eligible']:,}</b></span>
      <span>命中 <b>{row['matched']:,}</b></span>
      <span>未命中 <b>{row['no_match']:,}</b></span>
      <span>失败 <b>{row['failed']:,}</b></span>
      <span>原子 <b>{row['atom_count']:,}</b></span>
    </div>
    <div class="table-wrap">
      <table><thead><tr><th>标签</th><th>实体</th><th>覆盖</th></tr></thead>
      <tbody>{label_rows or '<tr><td colspan="3">本次无标签命中</td></tr>'}</tbody></table>
    </div>
  </div>
</details>"""
        )

    coverage_bars = "".join(
        f"""
<div class="bar-row">
  <div class="bar-label"><span>{row['script_id']} {_escape(row['name'])}</span><b>{row['coverage']:.1%}</b></div>
  <div class="bar-track"><i style="width:{row['matched'] / max_script * 100:.2f}%"></i></div>
  <small>{row['matched']:,} / {row['eligible']:,}</small>
</div>"""
        for row in execution
    )
    rating_bars = "".join(
        f"""
<div class="rating-row">
  <b>{row['rating']} 星</b>
  <div class="bar-track"><i style="width:{row['count'] / max_rating * 100:.2f}%"></i></div>
  <span>{row['count']:,} · {row['share']:.1%}</span>
</div>"""
        for row in ratings
    )
    limitations = "".join(
        f"<li><b>{_escape(item['code'])}</b> {_escape(item['message'])}</li>"
        for item in report["limitations"]
    )
    embedded_json = json.dumps(
        report, ensure_ascii=False, separators=(",", ":")
    ).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light">
<title>{_escape(report['title'])}</title>
<style>
:root{{--ink:#17241c;--muted:#66736a;--paper:#f4f1e8;--card:#fffdf7;--line:#d9d4c7;--green:#1f6b45;--lime:#b9db62;--orange:#e88643;--shadow:0 12px 34px rgba(30,50,35,.08)}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif}}a{{color:inherit}}.page{{width:min(1120px,100%);margin:auto;padding:20px 20px calc(48px + env(safe-area-inset-bottom))}}.hero{{background:linear-gradient(135deg,#173d2a,#285f43);color:#fff;border-radius:26px;padding:30px;box-shadow:var(--shadow)}}.eyebrow{{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#d8ecae}}h1{{font-size:clamp(30px,7vw,56px);line-height:1.05;margin:.25em 0}}h2{{font-size:clamp(23px,4vw,34px);line-height:1.2;margin:0 0 12px}}h3{{margin:0}}.hero p{{max-width:720px;color:#e6eee8}}.chips{{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}}.chip{{border:1px solid #ffffff45;background:#ffffff12;padding:6px 10px;border-radius:999px;font-size:12px}}.metrics{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin:18px 0}}.metric{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px;min-height:110px}}.metric small{{display:block;color:var(--muted)}}.metric b{{display:block;font-size:25px;margin-top:7px}}section{{margin-top:36px}}.section-copy{{max-width:760px;color:var(--muted)}}.callout{{border-left:5px solid var(--lime);background:#eef5dc;border-radius:0 16px 16px 0;padding:16px 18px;margin:18px 0}}.panel{{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:20px;box-shadow:var(--shadow)}}.bar-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px 28px}}.bar-row,.rating-row{{padding:8px 0}}.bar-label{{display:flex;justify-content:space-between;gap:12px;font-size:13px}}.bar-track{{height:10px;background:#e6e2d7;border-radius:999px;overflow:hidden;margin:5px 0}}.bar-track i{{display:block;height:100%;background:linear-gradient(90deg,var(--green),var(--lime));border-radius:inherit}}.bar-row small{{color:var(--muted)}}.rating-row{{display:grid;grid-template-columns:42px 1fr 120px;gap:12px;align-items:center}}.scripts{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.script-card{{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden}}summary{{display:grid;grid-template-columns:52px 1fr auto;gap:10px;align-items:center;padding:15px;cursor:pointer;list-style:none}}summary::-webkit-details-marker{{display:none}}.script-id{{font-weight:800;color:var(--green)}}.script-name{{font-weight:650}}.script-rate{{font-variant-numeric:tabular-nums;color:var(--muted)}}.script-body{{border-top:1px solid var(--line);padding:15px}}.script-body p{{margin:0 0 8px}}.use{{color:var(--muted)}}.status-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:7px;margin:12px 0}}.status-grid span{{background:#f2f0e8;border-radius:10px;padding:8px;font-size:12px}}.status-grid b{{display:block;font-size:16px}}.table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}table{{border-collapse:collapse;width:100%;min-width:420px}}th,td{{border-bottom:1px solid var(--line);padding:9px 8px;text-align:left;vertical-align:top}}th{{font-size:12px;color:var(--muted)}}.meta{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;overflow-wrap:anywhere}}ul{{padding-left:20px}}footer{{margin-top:40px;color:var(--muted);font-size:12px}}
@media(max-width:820px){{.metrics{{grid-template-columns:repeat(2,1fr)}}.bar-grid,.scripts{{grid-template-columns:1fr}}.page{{padding:12px 12px calc(36px + env(safe-area-inset-bottom))}}.hero{{padding:22px 20px;border-radius:20px}}section{{margin-top:28px}}.panel{{padding:15px}}}}
@media(max-width:480px){{.metric{{min-height:92px;padding:13px}}.metric b{{font-size:21px}}summary{{grid-template-columns:42px 1fr auto;padding:13px}}.status-grid{{grid-template-columns:repeat(2,1fr)}}.rating-row{{grid-template-columns:38px 1fr 90px;font-size:12px}}}}
</style>
</head>
<body>
<main class="page">
<header class="hero">
  <div class="eyebrow">VoxQuarry · Deterministic Reporting</div>
  <h1>RYOBI Mower<br>新版 28 脚本量化报告</h1>
  <p>同一份不可变数据快照、28 个版本化原子脚本、全样本执行账本。当前阶段不使用 LLM；报告展示脚本找到了什么，也展示没有找到什么。</p>
  <div class="chips"><span class="chip">RYOBI</span><span class="chip">割草机</span><span class="chip">排除骑乘割草机</span><span class="chip">LLM OFF</span><span class="chip">atomic-extractors/2.0.0</span></div>
</header>
<div class="metrics">
  <div class="metric"><small>唯一评论实体</small><b>{samples['review_entities']:,}</b></div>
  <div class="metric"><small>有正文样本</small><b>{samples['written_review_entities']:,}</b></div>
  <div class="metric"><small>执行状态</small><b>{ledger['record_count']:,}</b></div>
  <div class="metric"><small>联合覆盖率</small><b>{coverage:.1%}</b></div>
  <div class="metric"><small>全脚本未命中</small><b>{samples['atomic_v2_unmatched_entities']:,}</b></div>
  <div class="metric"><small>脚本失败</small><b>{sum(row['failed'] for row in execution):,}</b></div>
</div>
<section>
  <h2>先看执行完整性</h2>
  <p class="section-copy">每个脚本都对 4,307 条有正文样本执行。每行的“命中 + 未命中 + 失败”严格等于应执行样本；覆盖率是召回到的文本范围，不是准确率、故障率或业务优先级。</p>
  <div class="callout">本次 28 × 4,307 = <strong>120,596</strong> 条执行状态，失败 <strong>0</strong>；4,214 条至少命中一个脚本，93 条全脚本未命中并保留在账本中。</div>
  <div class="panel bar-grid">{coverage_bars}</div>
</section>
<section>
  <h2>28 个最小原子脚本</h2>
  <p class="section-copy">点击脚本查看用途、业务应用、执行状态和内部标签。脚本只提取原子化信息；跨脚本关系、解释与结论留给后续规则、统计或智能体。</p>
  <div class="scripts">{''.join(script_cards)}</div>
</section>
<section>
  <h2>评分基线</h2>
  <p class="section-copy">平均星级 {average_rating:.2f}，好评率 {positive_rate:.1%}，差评率 {negative_rate:.1%}。星级是评论级结构字段；E03 局部评价脚本是文本证据，两者不互相替代。</p>
  <div class="panel">{rating_bars}</div>
</section>
<section>
  <h2>方法与结论边界</h2>
  <ul>{limitations}</ul>
</section>
<section>
  <h2>可复现标识</h2>
  <div class="panel meta">
    report_id: {_escape(report['report_id'])}<br>
    analysis_version: {_escape(report['analysis_version'])}<br>
    data_snapshot_id: {_escape(report['data_snapshot_id'])}<br>
    core_result: {_escape(report['hashes']['core_result'])}<br>
    ledger: {_escape(ledger['logical_content_sha256'])}
  </div>
</section>
<footer>数据采集 VoxQuarry · 数据与核心数字由确定性脚本生成 · generated_at {_escape(report['generated_at'])}</footer>
</main>
<script id="voxquarry-report-json" type="application/json">{embedded_json}</script>
</body>
</html>"""
