from __future__ import annotations

import html
from typing import Any, Iterable


def _h(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _n(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.1%}"
    except (TypeError, ValueError):
        return "—"


def _evidence(
    ids: Iterable[str], evidence_by_id: dict[str, dict[str, Any]], limit: int = 4
) -> str:
    rows = []
    seen_entities: set[str] = set()
    for evidence_id in ids:
        row = evidence_by_id.get(str(evidence_id))
        if not row:
            continue
        entity = str(row.get("review_entity_id") or evidence_id)
        if entity in seen_entities:
            continue
        seen_entities.add(entity)
        quote = " ".join(str(row.get("quote") or "").split())
        if len(quote) > 340:
            quote = quote[:337].rstrip() + "…"
        rows.append(
            "<blockquote><p>“"
            + _h(quote)
            + "”</p><footer>"
            + _h(row.get("category") or "未分类")
            + " · "
            + _h(row.get("model") or "未明确型号")
            + " · "
            + _h(row.get("rating") or "—")
            + "★ · <code>"
            + _h(evidence_id)
            + "</code></footer></blockquote>"
        )
        if len(rows) >= limit:
            break
    return "".join(rows) or '<p class="empty">当前没有可展示的代表原文。</p>'


def _metric_cards(rows: list[dict[str, Any]], evidence: dict[str, dict[str, Any]], kind: str) -> str:
    cards = []
    for index, row in enumerate(rows[:14], start=1):
        count = row.get("review_entities", row.get("negative_entities", 0))
        rate = row.get("coverage", row.get("mention_rate", row.get("negative_share_of_text", 0)))
        if kind == "experience":
            sub = f"正向 {_n(row.get('positive_entities'))} · 负向 {_n(row.get('negative_entities'))}"
            ids = [*(row.get("positive_evidence_ids") or []), *(row.get("negative_evidence_ids") or [])]
        elif kind == "failure":
            sub = f"恢复 {_n(row.get('service_recovery_entities'))} · 退出 {_n(row.get('return_exit_entities'))}"
            ids = row.get("evidence_ids") or []
        else:
            sub = f"覆盖 {_pct(rate)} · {row.get('category_count', 0)} 个品类"
            ids = row.get("evidence_ids") or []
        cards.append(
            f'<article class="metric"><span class="rank">{index:02d}</span>'
            f'<div><h3>{_h(row.get("title"))}</h3><strong>{_n(count)}</strong>'
            f'<p>{_h(sub)}</p><i><b style="width:{max(1.0, min(100.0, float(rate or 0) * 100)):.2f}%"></b></i>'
            f'<details><summary>代表证据</summary>{_evidence(ids, evidence, 3)}</details></div></article>'
        )
    return '<div class="metric-grid">' + "".join(cards) + "</div>"


def _persona_cards(analysis: dict[str, Any], evidence: dict[str, dict[str, Any]]) -> str:
    cards = []
    for index, row in enumerate(analysis.get("personas") or [], start=1):
        def signals(key: str) -> str:
            items = row.get(key) or []
            if not items:
                return '<span class="empty">证据不足</span>'
            return "".join(
                f'<li><b>{_h(item.get("title"))}</b><span>{_n(item.get("review_entities"))} 条 · 指数 {_h(item.get("index_vs_brand") or "—")}</span></li>'
                for item in items[:4]
            )

        hypotheses = "".join(
            f'<li><p><b>问题：</b>{_h(item.get("problem"))}</p><p><b>假设：</b>{_h(item.get("hypothesis"))}</p><p><b>成功指标：</b>{_h(item.get("success_metrics"))}</p>{_evidence(item.get("evidence_ids") or [], evidence, 2)}</li>'
            for item in row.get("product_hypotheses") or []
        ) or '<li class="empty">负向证据不足，不机械生成机会。</li>'
        strength = row.get("evidence_strength") or {}
        cards.append(
            f'<article class="persona"><header><span>P{index:02d}</span><em>证据 {_h(strength.get("grade") or "limited")} · {_n(row.get("review_entities"))} 条显式样本 · {len(row.get("claim_ids") or [])} claims</em></header>'
            f'<h2>{_h(row.get("title"))}</h2><p>{_h(row.get("summary") or row.get("definition"))}</p>'
            f'<div class="jtbd"><small>核心 JTBD</small><strong>{_h(row.get("jtbd"))}</strong></div>'
            f'<div class="persona-grid"><section><h4>选择标准</h4><ul>{signals("decision_drivers")}</ul></section>'
            f'<section><h4>关键体验</h4><ul>{signals("experience_priorities")}</ul></section>'
            f'<section><h4>痛点与风险</h4><ul>{signals("pain_points")}</ul></section>'
            f'<section><h4>生命周期</h4><ul>{signals("lifecycle_stages")}</ul></section></div>'
            f'<details class="deep"><summary>展开完整研究档案</summary><h4>待验证产品假设</h4><ol>{hypotheses}</ol>'
            f'<h4>下一轮研究问题</h4><ol>{"".join(f"<li>{_h(item)}</li>" for item in row.get("research_questions") or [])}</ol>'
            f'<h4>研究招募条件</h4><p>{_h(row.get("recruiting_criteria"))}</p>'
            f'<h4>身份原文</h4>{_evidence(row.get("evidence_ids") or [], evidence, 5)}'
            f'<h4>不能推断</h4><ul>{"".join(f"<li>{_h(item)}</li>" for item in row.get("cannot_infer") or [])}</ul></details></article>'
        )
    return '<div class="personas">' + "".join(cards) + "</div>"


def _task_context(analysis: dict[str, Any], evidence: dict[str, dict[str, Any]]) -> str:
    rows = []
    for category in analysis.get("task_context") or []:
        if not category.get("dimensions"):
            continue
        dimensions = []
        for dimension in category.get("dimensions") or []:
            values = "".join(
                f'<li><b>{_h(item.get("label"))}</b><span>{_n(item.get("review_entities"))}</span>'
                f'{_evidence(item.get("evidence_ids") or [], evidence, 1)}</li>'
                for item in dimension.get("values") or []
            )
            dimensions.append(
                f'<article><header><b>{_h(dimension.get("title"))}</b><span>{_n(dimension.get("review_entities"))} · {_pct(dimension.get("coverage"))}</span></header><ul>{values}</ul></article>'
            )
        rows.append(
            f'<details class="context"><summary><b>{_h(category.get("category"))}</b><span>{_n(category.get("written_review_entities"))} 条正文 · {len(category.get("dimensions") or [])} 个维度</span></summary><div>{"".join(dimensions)}</div></details>'
        )
    return "".join(rows) or '<p class="empty">当前 Profile 场景证据不足。</p>'


def _comparisons(analysis: dict[str, Any]) -> str:
    rows = []
    for lens in analysis.get("comparison_lenses") or []:
        if lens.get("status") != "available":
            continue
        groups = []
        for group in lens.get("groups") or []:
            signals = "".join(
                f'<li><b>{_h(item.get("title"))}</b><span>{_n(item.get("review_entities"))} · {_h(item.get("index_vs_brand"))}×</span></li>'
                for item in (group.get("top_signals") or [])[:6]
            )
            date_range = group.get("date_range") or {}
            groups.append(
                f'<article><header><b>{_h(group.get("label"))}</b><span>{_n(group.get("written_review_entities"))} · {_pct(group.get("share_of_written"))}</span></header><small>{_h(date_range.get("start"))} — {_h(date_range.get("end"))}</small><ul>{signals}</ul></article>'
            )
        rows.append(
            f'<details class="context"><summary><b>{_h(lens.get("title"))}描述性下钻</b><span>{_n(lens.get("eligible_group_count"))} 组 · 门槛 {_n(lens.get("minimum_written_per_group"))}</span></summary><div>{"".join(groups)}</div><small>{_h(lens.get("boundary"))}</small></details>'
        )
    return "".join(rows) or '<p class="empty">没有两个以上通过门槛的可比较组。</p>'


def render_brand_persona_html(analysis: dict[str, Any]) -> str:
    evidence = {str(row.get("evidence_id")): row for row in analysis.get("evidence") or []}
    sample = analysis.get("sample") or {}
    categories = (analysis.get("sample_composition") or {}).get("categories") or []
    readouts = "".join(
        f'<article><span>{index:02d}</span><h3>{_h(row.get("title"))}</h3><p>{_h(row.get("statement"))}</p>'
        f'<dl><dt>产品应用</dt><dd>{_h(row.get("product_use"))}</dd><dt>解释边界</dt><dd>{_h(row.get("boundary"))}</dd></dl>'
        f'{_evidence(row.get("evidence_ids") or [], evidence, 2)}</article>'
        for index, row in enumerate(analysis.get("executive_readout") or [], start=1)
    )
    category_rows = "".join(
        f'<tr><td>{_h(row.get("category"))}</td><td>{_n(row.get("review_entities"))}</td><td>{_n(row.get("written_review_entities"))}</td><td>{_h(row.get("average_rating") or "—")}</td></tr>'
        for row in categories
    )
    lifecycle = _metric_cards(analysis.get("lifecycle_stages") or [], evidence, "lifecycle")
    failure = _metric_cards(analysis.get("failure_chains") or [], evidence, "failure")
    cross = "".join(
        '<article class="cross"><h3>' + _h(row.get("title")) + '</h3><p>'
        + ("跨品类共同信号" if row.get("brand_level_signal") else "品类特定信号")
        + "</p>"
        + "".join(
            f'<div><b>{_h(item.get("category"))}</b><span>{_n(item.get("review_entities"))}/{_n(item.get("denominator"))}</span><em>{_h(item.get("index_vs_brand"))}×</em></div>'
            for item in (row.get("top_over_indexed") or [])[:4]
        )
        + "</article>"
        for row in (analysis.get("cross_category_signals") or [])[:12]
        if row.get("top_over_indexed")
    )
    cooccurrence = "".join(
        f'<li><b>{_h(row.get("left_title"))} + {_h(row.get("right_title"))}</b><span>{_n(row.get("joint_entities"))} 条 · Lift {_h(row.get("lift"))}×</span></li>'
        for row in (analysis.get("cooccurrence_signals") or [])[:16]
    )
    dimensions = "".join(
        f'<details><summary><b>{_h(row.get("title"))}</b><span>{_n(row.get("review_entities"))} · {_pct(row.get("coverage"))}</span></summary>'
        + "".join(
            f'<div class="dim"><b>{_h(item.get("label"))}</b><span>{_n(item.get("review_entities"))} · {_pct(item.get("coverage"))}</span>{_evidence(item.get("evidence_ids") or [], evidence, 2)}</div>'
            for item in (row.get("values") or [])[:8]
        )
        + f'<small>{_h(row.get("boundary"))}</small></details>'
        for row in analysis.get("dimensions") or []
    )
    quality = analysis.get("quality") or {}
    richness = quality.get("information_richness") or {}
    data_quality = analysis.get("data_quality") or {}
    ledger = analysis.get("execution_ledger") or {}
    brand = (analysis.get("scope") or {}).get("brand")
    semantic_extractor = (analysis.get("semantic_versions") or {}).get(
        "semantic_extractor"
    )
    task_context = _task_context(analysis, evidence)
    comparisons = _comparisons(analysis)
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_h(brand)} 品牌用户画像</title>
<style>
:root{{--bg:#070b08;--surface:#0d1510;--line:#26372c;--text:#f5f7f5;--muted:#92a098;--green:#8ccc50;--pink:#c72c70;--danger:#ff8d95}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.65 Inter,system-ui,-apple-system,"PingFang SC",sans-serif}}main{{max-width:1320px;margin:auto;padding:44px 28px 100px}}nav{{position:sticky;top:0;z-index:5;display:flex;gap:10px;overflow:auto;padding:12px 20px;background:rgba(7,11,8,.94);border-bottom:1px solid var(--line);backdrop-filter:blur(14px)}}nav a{{white-space:nowrap;color:var(--muted);text-decoration:none;padding:7px 11px;border-radius:99px}}nav a:hover{{color:#fff;background:#162219}}.hero{{padding:44px;border:1px solid var(--line);border-radius:22px;background:radial-gradient(circle at 88% 12%,rgba(199,44,112,.2),transparent 30%),radial-gradient(circle at 10% 90%,rgba(140,204,80,.15),transparent 34%),var(--surface)}}.eyebrow{{color:var(--green);font-weight:800;letter-spacing:.16em;text-transform:uppercase}}h1{{font-size:clamp(38px,6vw,74px);line-height:1;margin:20px 0}}.hero>p{{max-width:820px;color:var(--muted);font-size:17px}}.stats{{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;margin-top:34px;background:var(--line);border:1px solid var(--line);border-radius:14px;overflow:hidden}}.stats div{{padding:18px;background:#09100b}}.stats b,.stats span{{display:block}}.stats b{{font-size:24px}}.stats span{{color:var(--muted);font-size:11px}}section.block{{padding:70px 0;border-bottom:1px solid var(--line)}}section.block>header{{display:flex;justify-content:space-between;gap:30px;align-items:end;margin-bottom:22px}}section.block h2{{font-size:clamp(28px,4vw,48px);line-height:1.15;margin:8px 0}}section.block header p{{max-width:720px;color:var(--muted)}}.readouts{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}.readouts>article,.metric,.cross,.persona,.method{{border:1px solid var(--line);border-radius:14px;background:var(--surface)}}.readouts>article{{padding:18px;min-height:220px}}.readouts>article>span,.rank{{color:var(--green);font-weight:900;letter-spacing:.1em}}.readouts dl{{display:grid;grid-template-columns:64px 1fr;gap:6px 9px;padding-top:10px;border-top:1px solid var(--line)}}.readouts dt{{color:var(--green)}}.readouts dd{{margin:0;color:var(--muted)}}blockquote{{margin:12px 0 0;padding:12px;border-left:2px solid var(--pink);background:#0a100c}}blockquote p{{margin:0;font-size:12px}}blockquote footer{{margin-top:7px;color:var(--muted);font-size:9px}}.personas{{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}}.persona{{padding:24px}}.persona>header{{display:flex;justify-content:space-between;color:var(--green)}}.persona>header em{{color:var(--muted);font-size:10px;font-style:normal}}.persona h2{{font-size:27px}}.persona>p{{color:var(--muted)}}.jtbd{{padding:15px;border-left:3px solid var(--pink);background:rgba(199,44,112,.08)}}.jtbd small,.jtbd strong{{display:block}}.persona-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:18px}}.persona-grid section{{padding:13px;border:1px solid var(--line);border-radius:10px}}h4{{margin:0 0 9px;color:var(--green)}}ul,ol{{margin:0;padding-left:20px}}.persona-grid ul{{padding:0;list-style:none}}.persona-grid li{{display:flex;justify-content:space-between;gap:8px;padding:7px 0;border-top:1px solid var(--line);font-size:11px}}.persona-grid li span{{color:var(--muted);text-align:right}}details.deep{{margin-top:15px;border:1px solid var(--line);border-radius:10px;padding:12px}}summary{{cursor:pointer}}.deep h4{{margin-top:20px}}.metric-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}}.metric{{display:grid;grid-template-columns:42px 1fr;gap:12px;padding:16px}}.metric h3{{margin:0}}.metric strong{{display:block;font-size:24px;margin:5px 0}}.metric p{{margin:0;color:var(--muted)}}.metric i{{display:block;height:3px;margin:10px 0;background:#1c2a21}}.metric i b{{display:block;height:100%;background:linear-gradient(90deg,var(--pink),var(--green))}}.metric details{{margin-top:10px}}.two{{display:grid;grid-template-columns:1fr 1fr;gap:28px}}.cross-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}.cross{{padding:16px}}.cross p{{color:var(--green)}}.cross div{{display:flex;justify-content:space-between;padding:7px 0;border-top:1px solid var(--line)}}.cross span{{color:var(--muted)}}.cross em{{color:#df78a8;font-style:normal}}.co li{{display:flex;justify-content:space-between;padding:10px;border-bottom:1px solid var(--line)}}.co span{{color:var(--muted)}}.dimensions{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}}.dimensions>details{{border:1px solid var(--line);border-radius:12px;background:var(--surface);padding:14px}}.dimensions summary{{display:flex;justify-content:space-between}}.dim{{padding:14px 0;border-top:1px solid var(--line)}}.dim>span{{float:right;color:var(--muted)}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left}}td:not(:first-child){{text-align:right}}.method{{padding:22px}}.empty,.method p,.method li{{color:var(--muted)}}code{{color:var(--green);overflow-wrap:anywhere}}@media(max-width:900px){{.stats{{grid-template-columns:repeat(3,1fr)}}.readouts{{grid-template-columns:repeat(2,1fr)}}.personas,.metric-grid,.two,.cross-grid,.dimensions{{grid-template-columns:1fr}}}}@media(max-width:560px){{main{{padding:24px 14px 70px}}.hero{{padding:24px}}.stats{{grid-template-columns:repeat(2,1fr)}}.readouts{{grid-template-columns:1fr}}.readouts>article{{min-height:0}}.persona{{padding:18px}}.persona-grid{{grid-template-columns:1fr}}section.block{{padding:48px 0}}}}
details.context{{margin:10px 0;border:1px solid var(--line);border-radius:12px;background:var(--surface);overflow:hidden}}details.context>summary{{display:flex;justify-content:space-between;padding:18px}}details.context>summary span,details.context>small{{color:var(--muted)}}details.context>div{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;padding:0 14px 14px}}details.context article{{padding:14px;border:1px solid var(--line);border-radius:10px;background:#0a100c}}details.context article header,details.context li{{display:flex;justify-content:space-between;gap:12px}}details.context ul{{padding:0;list-style:none}}details.context li{{padding:8px 0;border-top:1px solid var(--line)}}details.context li span{{color:var(--muted)}}details.context>small{{display:block;padding:0 18px 16px}}@media(max-width:700px){{details.context>div{{grid-template-columns:1fr}}}}
</style></head><body><nav><a href="#personas">Persona</a><a href="#readout">判断</a><a href="#drivers">选择标准</a><a href="#experience">体验</a><a href="#journey">生命周期</a><a href="#cross">跨品类</a><a href="#context">任务场景</a><a href="#compare">比较</a><a href="#dimensions">原子维度</a><a href="#method">方法</a></nav><main>
<header class="hero"><div class="eyebrow">VoxQuarry · Brand audience intelligence</div><h1>{_h(brand)}<br>跨品类用户画像</h1><p>先以全量确定性脚本识别身份、责任、决策驱动、系统体验、生命周期和失败恢复链，再把多个已锁定 claim 组织成 Persona。未知保持未知，所有核心数字与原文可独立复核。</p><div class="stats"><div><b>{_n(sample.get("review_entities"))}</b><span>可分析评论实体</span></div><div><b>{_n(sample.get("written_review_entities"))}</b><span>正文</span></div><div><b>{_pct(sample.get("written_coverage"))}</b><span>正文覆盖</span></div><div><b>{_n(sample.get("category_count"))}</b><span>品类</span></div><div><b>{_n(sample.get("model_count"))}</b><span>型号</span></div><div><b>{_h(sample.get("average_rating") or "—")}</b><span>平均评分</span></div></div></header>
<section class="block" id="personas"><header><div><div class="eyebrow">Persona first</div><h2>有证据支持的核心 Persona</h2></div><p>每张卡片由多条独立 claim 构成；Qwen 只能改写标题和摘要，不能决定群组、数字或产品机会。</p></header>{_persona_cards(analysis, evidence)}</section>
<section class="block" id="readout"><header><div><div class="eyebrow">Answer first</div><h2>给产品团队的可验证判断</h2></div></header><div class="readouts">{readouts}</div></section>
<section class="block" id="drivers"><header><div><div class="eyebrow">Choice architecture</div><h2>购买与选型驱动</h2></div><p>按评论实体去重，保留评分上下文、品类覆盖和代表原文。</p></header>{_metric_cards(analysis.get("decision_drivers") or [], evidence, "driver")}</section>
<section class="block" id="experience"><header><div><div class="eyebrow">Experience system</div><h2>跨品类系统体验</h2></div><p>把不同工具的局部属性映射到品牌级体验系统，同时分开明确正向和负向表达。</p></header>{_metric_cards(analysis.get("experience_systems") or [], evidence, "experience")}</section>
<section class="block" id="journey"><header><div><div class="eyebrow">Ownership journey</div><h2>生命周期与失败恢复链</h2></div></header><div class="two"><div>{lifecycle}</div><div>{failure}</div></div></section>
<section class="block" id="cross"><header><div><div class="eyebrow">Cross-category structure</div><h2>品牌共同点与品类修正</h2></div><p>相对指数只描述语言集中度，不是市场渗透率。</p></header><div class="cross-grid">{cross}</div><h3>决策标准共现</h3><ul class="co">{cooccurrence}</ul></section>
<section class="block" id="context"><header><div><div class="eyebrow">Task & context atlas</div><h2>不同品类的任务、场地与使用上下文</h2></div><p>回到品类 Profile，避免品牌 Persona 抹平真实任务差异。</p></header>{task_context}</section>
<section class="block" id="compare"><header><div><div class="eyebrow">Comparison guardrail</div><h2>品类、渠道、市场与型号下钻</h2></div><p>只展示通过门槛的描述性差异，不自动写成优劣或因果。</p></header>{comparisons}</section>
<section class="block" id="dimensions"><header><div><div class="eyebrow">Atomic evidence</div><h2>画像原子维度与未知覆盖</h2></div></header><div class="dimensions">{dimensions}</div></section>
<section class="block"><header><div><div class="eyebrow">Sample structure</div><h2>跨品类样本构成</h2></div></header><table><thead><tr><th>品类</th><th>评论</th><th>正文</th><th>平均评分</th></tr></thead><tbody>{category_rows}</tbody></table></section>
<section class="block" id="method"><div class="method"><div class="eyebrow">Method & audit</div><h2>方法、执行与边界</h2><p><code>{_h(analysis.get("method_version"))}</code> · <code>{_h(semantic_extractor)}</code> · snapshot <code>{_h(analysis.get("snapshot_id"))}</code></p><p>分析总体由 {_n(data_quality.get("unique_complete_review_entities"))} 个完整唯一实体与 {_n(data_quality.get("incomplete_identity_occurrences"))} 条无法形成跨任务实体键、因而按独立记录保留的数据组成；另排除 {_n(data_quality.get("duplicate_occurrences_excluded"))} 条重复出现。</p><p>执行 {_n(ledger.get("extractor_count"))} 个确定性提取器，共 {_n(ledger.get("completed_entity_executions"))} 次“正文实体 × 脚本”执行，失败 {_n(ledger.get("failed_extractors"))}；SKIL 信息丰富度门槛：<b>{_h(richness.get("status"))}</b>。</p><ul>{"".join(f"<li>{_h(item)}</li>" for item in quality.get("limitations") or [])}</ul></div></section>
</main></body></html>'''
