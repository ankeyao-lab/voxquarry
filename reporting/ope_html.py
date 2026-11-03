from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Iterable


def _h(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%"


def _num(value: int | float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and digits:
        return f"{value:,.{digits}f}"
    return f"{value:,.0f}"


def _evidence_button(
    evidence_ids: Iterable[str],
    *,
    label: str = "查看原文",
    context: str = "",
    population: int | None = None,
) -> str:
    values = list(dict.fromkeys(evidence_ids))
    if not values:
        return '<span class="no-evidence">暂无可展示原文</span>'
    population_text = (
        f"<em>/ 命中 {_num(population)}</em>"
        if population is not None
        else ""
    )
    return (
        '<button class="evidence-btn" type="button" '
        f'data-evidence="{_h(",".join(values))}" '
        f'data-context="{_h(context)}">'
        f'<span>{_h(label)}</span><b>{len(values)}</b>{population_text}</button>'
    )


def _grade_label(grade: str) -> str:
    return {
        "usable": "可用于本报告",
        "directional": "仅方向性",
        "sparse": "稀疏",
        "missing": "缺失",
    }.get(grade, grade)


def _grade_class(grade: str) -> str:
    return {
        "usable": "good",
        "directional": "warn",
        "sparse": "risk",
        "missing": "muted",
    }.get(grade, "muted")


def _metric_card(
    label: str,
    value: str,
    note: str,
    tone: str = "",
) -> str:
    return (
        f'<div class="metric-card {tone}">'
        f'<div class="metric-label">{_h(label)}</div>'
        f'<div class="metric-value">{_h(value)}</div>'
        f'<div class="metric-note">{_h(note)}</div>'
        "</div>"
    )


def _section_intro(
    number: str,
    eyebrow: str,
    title: str,
    body: str,
    takeaway: str,
) -> str:
    return (
        '<div class="section-intro">'
        f'<div class="section-number">{_h(number)}</div>'
        f'<div class="eyebrow">{_h(eyebrow)}</div>'
        f"<h2>{_h(title)}</h2>"
        f"<p>{_h(body)}</p>"
        f'<div class="takeaway"><span>本章判断</span>{_h(takeaway)}</div>'
        "</div>"
    )


def _dimension_card(row: dict[str, Any]) -> str:
    values = row["values"][:10]
    bars = []
    max_n = max(
        (value["review_entities"] for value in values), default=1
    )
    for value in values:
        width = max(3, value["review_entities"] / max_n * 100)
        bars.append(
            '<div class="bar-row">'
            '<div class="bar-head">'
            f'<span>{_h(value["label"])}</span>'
            f'<b>{_num(value["review_entities"])} '
            f'<small>/ {_pct(value["coverage"])}</small></b>'
            "</div>"
            '<div class="bar-track">'
            f'<i style="width:{width:.2f}%"></i></div>'
            f'{_evidence_button(value["evidence_ids"], label="代表性证据", context=row["title"] + " · " + value["label"], population=value.get("evidence_population"))}'
            "</div>"
        )
    empty = (
        '<div class="empty-state">当前样本没有可复用的明确表达。'
        "这应进入下一轮数据采集字段，而不是由模型推断。</div>"
        if not values
        else ""
    )
    return (
        '<details class="dimension-card">'
        "<summary>"
        '<div class="dimension-title">'
        f'<span class="coverage-dot {_grade_class(row["coverage_grade"])}"></span>'
        f'<div><h3>{_h(row["title"])}</h3>'
        f'<p>{_h(row["question"])}</p></div></div>'
        '<div class="dimension-stat">'
        f'<b>{_num(row["review_entities"])}</b>'
        f'<span>{_pct(row["coverage"])}</span>'
        f'<em class="{_grade_class(row["coverage_grade"])}">'
        f'{_h(_grade_label(row["coverage_grade"]))}</em>'
        "</div></summary>"
        f'<div class="dimension-body">{"".join(bars)}{empty}'
        f'<p class="boundary">{_h(row["boundary"])}</p></div>'
        "</details>"
    )


def _persona_card(row: dict[str, Any], rank: int) -> str:
    lifts = []
    for item in row["distinctive_aspects"][:4]:
        lift = (
            f'{item["lift"]:.1f}×' if item["lift"] is not None else "—"
        )
        lifts.append(
            '<div class="lift-row">'
            f'<span>{_h(item["label"])}</span>'
            f'<b>{lift}</b>'
            f'<small>群内 {_pct(item["cohort_rate"])}'
            f' · 总体 {_pct(item["overall_rate"])}</small></div>'
        )
    return (
        '<article class="persona-card">'
        '<div class="persona-top">'
        f'<span class="persona-index">P{rank:02d}</span>'
        f'<span class="sample-chip">显式样本 {_num(row["review_entities"])}</span>'
        "</div>"
        f'<h3>{_h(row["title"])}</h3>'
        f'<p class="definition">{_h(row["definition"])}</p>'
        '<div class="persona-jtbd"><span>核心任务</span>'
        f'{_h(row["jtbd"])}</div>'
        '<div class="mini-kpis">'
        f'<span>覆盖 <b>{_pct(row["coverage"])}</b></span>'
        f'<span>均分 <b>{row["average_rating"]:.2f}</b></span>'
        f'<span>1–2★ <b>{_pct(row["low_star_rate"])}</b></span>'
        "</div>"
        '<div class="lift-title">相对总体更常提及</div>'
        f'<div class="lift-list">{"".join(lifts)}</div>'
        f'{_evidence_button(row["evidence_ids"], context=row["title"], population=row.get("evidence_population"))}'
        f'<p class="boundary">{_h(row["boundary"])}</p>'
        "</article>"
    )


def _rank_rows(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    limit: int = 8,
) -> str:
    if mode == "positive":
        ranked = sorted(
            rows,
            key=lambda row: (
                -row["explicit_positive_entities"],
                -row["explicit_positive_rate"],
            ),
        )[:limit]
        max_n = max(
            (row["explicit_positive_entities"] for row in ranked),
            default=1,
        )
        count_key = "explicit_positive_entities"
        rate_key = "explicit_positive_rate"
        evidence_key = "positive_evidence_ids"
    else:
        ranked = sorted(
            rows,
            key=lambda row: (
                -row["explicit_negative_entities"],
                -row["low_star_rate"],
            ),
        )[:limit]
        max_n = max(
            (row["explicit_negative_entities"] for row in ranked),
            default=1,
        )
        count_key = "explicit_negative_entities"
        rate_key = "explicit_negative_rate"
        evidence_key = "negative_evidence_ids"
    rendered = []
    for rank, row in enumerate(ranked, 1):
        count = row[count_key]
        width = max(3, count / max_n * 100)
        rendered.append(
            '<div class="rank-row">'
            f'<span class="rank-num">{rank:02d}</span>'
            '<div class="rank-main">'
            '<div class="rank-head">'
            f'<b>{_h(row["label"])}</b>'
            f'<span>{_num(count)} 条明确'
            f' · 占该属性 {_pct(row[rate_key])}</span></div>'
            '<div class="rank-track">'
            f'<i style="width:{width:.2f}%"></i></div>'
            '<div class="rank-foot">'
            f'<span>提及 {_num(row["review_entities"])}'
            f' · 均分 {row["average_rating"]:.2f}'
            f' · 1–2★ {_pct(row["low_star_rate"])}</span>'
            f'{_evidence_button(row[evidence_key], label="看引用", context=row["label"], population=row.get("positive_evidence_population" if mode == "positive" else "negative_evidence_population"))}'
            "</div></div></div>"
        )
    return "".join(rendered)


def _aspect_table(rows: list[dict[str, Any]]) -> str:
    body = []
    for row in rows:
        balance = (
            row["explicit_positive_entities"]
            - row["explicit_negative_entities"]
        )
        body.append(
            "<tr>"
            f'<td><b>{_h(row["label"])}</b>'
            f'<small>{_h(row["aspect_id"])}</small></td>'
            f'<td>{_num(row["review_entities"])}</td>'
            f'<td>{_pct(row["coverage"])}</td>'
            f'<td>{row["average_rating"]:.2f}</td>'
            f'<td class="positive">{_num(row["explicit_positive_entities"])}</td>'
            f'<td class="negative">{_num(row["explicit_negative_entities"])}</td>'
            f'<td>{_pct(row["low_star_rate"])}</td>'
            f'<td><span class="balance {"up" if balance >= 0 else "down"}">'
            f'{"+" if balance >= 0 else ""}{balance}</span></td>'
            "<td>"
            f'{_evidence_button(row["positive_evidence_ids"] + row["negative_evidence_ids"], label="证据", context=row["label"], population=row.get("positive_evidence_population", 0) + row.get("negative_evidence_population", 0))}'
            "</td></tr>"
        )
    return (
        '<div class="table-wrap"><table class="data-table">'
        "<thead><tr><th>属性</th><th>提及</th><th>覆盖</th>"
        "<th>均分</th><th>明确正向</th><th>明确负向</th>"
        "<th>低星关联</th><th>净显式声量</th><th></th></tr></thead>"
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def _kano_lane(row: dict[str, Any]) -> str:
    items = []
    for item in row["items"]:
        items.append(
            '<article class="kano-item">'
            f'<h4>{_h(item["statement"])}</h4>'
            f'<p>{_h(item["label"])}</p>'
            '<div class="kano-stats">'
            f'<span>提及 <b>{_num(item["mention_entities"])}</b></span>'
            f'<span>支持信号 <b>{_num(item["support_entities"])}</b></span>'
            f'<span>均分 <b>{item["average_rating"]:.2f}</b></span>'
            f'<span>1–2★ <b>{_pct(item["low_star_rate"])}</b></span>'
            "</div>"
            f'{_evidence_button(item["evidence_ids"], label="分类证据", context=row["title"] + " · " + item["statement"], population=item.get("evidence_population"))}'
            "</article>"
        )
    return (
        f'<section class="kano-lane {row["category_id"]}">'
        '<div class="kano-head">'
        f'<span>{_h(row["category_id"].upper())}</span>'
        f'<h3>{_h(row["title"])}</h3>'
        f'<p>{_h(row["principle"])}</p></div>'
        f'<div class="kano-items">{"".join(items)}</div>'
        f'<p class="boundary">{_h(row["boundary"])}</p>'
        "</section>"
    )


def _opportunity_card(row: dict[str, Any], rank: int) -> str:
    return (
        '<article class="opportunity-card">'
        '<div class="opp-rank">'
        f'<span>OPPORTUNITY {rank:02d}</span>'
        f'<b>{_h(row["confidence"])}</b></div>'
        f'<h3>{_h(row["title"])}</h3>'
        f'<p class="problem">{_h(row["problem"])}</p>'
        '<div class="opp-metrics">'
        f'<div><b>{_num(row["review_entities"])}</b><span>问题空间评论</span></div>'
        f'<div><b>{_num(row["negative_entities"])}</b><span>负向 / 低星信号</span></div>'
        f'<div><b>{_pct(row["low_star_rate"])}</b><span>1–2★ 关联</span></div>'
        f'<div><b>{_num(row["explicit_need_entities"])}</b><span>明确需求表达</span></div>'
        "</div>"
        '<div class="opp-direction"><span>产品方向</span>'
        f'<p>{_h(row["product_direction"])}</p></div>'
        '<div class="opp-validation"><span>下一步验证</span>'
        f'<p>{_h(row["validation"])}</p></div>'
        f'{_evidence_button(row["evidence_ids"], label="打开问题证据", context=row["title"], population=row.get("evidence_population"))}'
        f'<p class="boundary">{_h(row["boundary"])}</p>'
        "</article>"
    )


def _smart_card(row: dict[str, Any]) -> str:
    basis = (
        "直接智能表达"
        if row["claim_basis"] == "direct_signal"
        else "由问题推导"
    )
    return (
        '<article class="smart-card">'
        '<div class="smart-code">'
        f'<b>{_h(row["code"])}</b><span>{_h(row["method"])}</span>'
        f'<em>{_h(basis)}</em></div>'
        f'<h3>{_h(row["title"])}</h3>'
        f'<p>{_h(row["value_hypothesis"])}</p>'
        '<dl>'
        f'<div><dt>问题证据</dt><dd>{_num(row["source_problem_entities"])} 条</dd></div>'
        f'<div><dt>所需数据</dt><dd>{_h(row["required_data"])}</dd></div>'
        f'<div><dt>产品护栏</dt><dd>{_h(row["guardrail"])}</dd></div>'
        "</dl>"
        f'{_evidence_button(row["evidence_ids"], label="查看源问题", context=row["code"] + " · " + row["title"], population=row.get("evidence_population"))}'
        "</article>"
    )


def _get_aspect(
    report: dict[str, Any], aspect_id: str
) -> dict[str, Any]:
    return next(
        row
        for row in report["aspect_experience"]
        if row["aspect_id"] == aspect_id
    )


def _get_persona(
    report: dict[str, Any], persona_id: str
) -> dict[str, Any]:
    return next(
        row for row in report["personas"] if row["persona_id"] == persona_id
    )


def _get_dimension(
    report: dict[str, Any], dimension_id: str
) -> dict[str, Any]:
    return next(
        row
        for row in report["dimension_census"]
        if row["dimension_id"] == dimension_id
    )


def build_html(report: dict[str, Any]) -> str:
    sample = report["sample"]
    brand_label = str(report.get("brand_label") or "OPE")
    channel_names = {
        "ego": "EGO.com",
        "ryobi": "RYOBI.com",
        "homedepot": "Home Depot",
        "lowes": "Lowe's",
        "ace": "Ace",
    }
    channel_label = " + ".join(
        channel_names.get(row["channel"], row["channel"])
        for row in sample.get("channels", [])
    ) or "全部入选渠道"
    latest_date = max(
        (
            str(row.get("submission_date") or "")
            for row in report["evidence"]
        ),
        default="",
    )
    evidence_audit = report.get("evidence_audit", {})

    def persona_lift(persona: dict[str, Any], aspect_id: str) -> float | None:
        return next(
            (
                row.get("lift")
                for row in persona.get("distinctive_aspects", [])
                if row.get("aspect_id") == aspect_id
            ),
            None,
        )
    aspects = report["aspect_experience"]
    personas = report["personas"]
    profile_dimensions = [
        row
        for row in report["dimension_census"]
        if row["section"] == "profile"
    ]
    scenario_dimensions = [
        row
        for row in report["dimension_census"]
        if row["section"] == "scenario"
    ]
    battery = _get_aspect(report, "battery_charger")
    runtime = _get_aspect(report, "runtime")
    charging = _get_aspect(report, "charging")
    reliability = _get_aspect(report, "reliability")
    service = _get_aspect(report, "service")
    safety = _get_aspect(report, "safety")
    noise = _get_aspect(report, "noise")
    gasfree = _get_aspect(report, "maintenance_gasfree")
    self_propel = _get_aspect(report, "self_propel")
    physical_constraints = _get_persona(
        report, "explicit_physical_constraints"
    )
    smart = _get_aspect(report, "smart_connectivity")
    yard_size = _get_dimension(report, "yard_size")
    grass = _get_dimension(report, "grass_condition")
    charging_flow = _get_dimension(report, "charging_workflow")
    clipping = _get_dimension(report, "clipping_mode")
    storage = _get_dimension(report, "storage")
    occupation = _get_dimension(report, "occupation")
    age = _get_dimension(report, "explicit_age")
    geography = _get_dimension(report, "geography")
    grass_species = _get_dimension(report, "grass_species")
    cut_height = _get_dimension(report, "cut_height")
    time_of_day = _get_dimension(report, "time_of_day")
    constraint_self_propel_lift = persona_lift(
        physical_constraints, "self_propel"
    )
    constraint_handling_lift = persona_lift(
        physical_constraints, "handling"
    )

    used_evidence_ids: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("evidence_ids") and isinstance(item, list):
                    used_evidence_ids.update(str(x) for x in item)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(report)
    evidence_map = {
        row["evidence_id"]: row
        for row in report["evidence"]
        if row["evidence_id"] in used_evidence_ids
    }
    embedded = json.dumps(
        {
            "report": {
                "report_id": report["report_id"],
                "source_report_id": report["source_report_id"],
                "data_snapshot_id": report["data_snapshot_id"],
                "content_hash": report["content_hash"],
            },
            "evidence": evidence_map,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")

    executive_cards = [
        (
            "01",
            "电动化价值已经成立，优势不是参数而是生活方式变化",
            (
                f"噪音相关评论均分 {noise['average_rating']:.2f}，"
                f"明确正向 {noise['explicit_positive_entities']:,} 条；"
                f"免燃油与低维护均分 {gasfree['average_rating']:.2f}。"
                "“安静、免拉绳、免油污”是应继续守住的体验资产。"
            ),
            noise["positive_evidence_ids"]
            + gasfree["positive_evidence_ids"],
            "strength",
        ),
        (
            "02",
            "完成度必须回到具体任务与负载，不等于差评集中",
            (
                f"续航与充电均分 {runtime['average_rating']:.2f} / "
                f"{charging['average_rating']:.2f}；明确院子面积 "
                f"{yard_size['review_entities']:,} 条、草况 "
                f"{grass['review_entities']:,} 条。"
                "报告把完成度与面积、草况和充电流程交叉核验，不把任务场景包装成人群身份。"
            ),
            runtime["negative_evidence_ids"]
            + charging["negative_evidence_ids"],
            "focus",
        ),
        (
            "03",
            "自走不是附加功能，而是可达性与信任功能",
            (
                f"明确体力 / 行动限制样本 "
                f"{physical_constraints['review_entities']:,} 条；"
                "群体内自走与操控的提及密度约为总体 "
                f"{(constraint_self_propel_lift or 0):.1f}× / "
                f"{(constraint_handling_lift or 0):.1f}×。"
                "速度曲线、转向和坡地控制应按人因任务验证。"
            ),
            physical_constraints["evidence_ids"],
            "people",
        ),
        (
            "04",
            "可靠性与服务是口碑均值下面真正的信任断点",
            (
                f"可靠性均分 {reliability['average_rating']:.2f}、"
                f"1–2★ {_pct(reliability['low_star_rate'])}；售后 / 保修均分 "
                f"{service['average_rating']:.2f}、1–2★ "
                f"{_pct(service['low_star_rate'])}；安全样本均分 "
                f"{safety['average_rating']:.2f}。"
            ),
            reliability["negative_evidence_ids"]
            + service["negative_evidence_ids"]
            + safety["negative_evidence_ids"],
            "risk",
        ),
        (
            "05",
            "智能化应从已证实的问题出发，不从 App 功能清单出发",
            (
                f"直接智能 / 连接表达仅 {smart['review_entities']:,} 条，"
                f"覆盖正文 {_pct(smart['coverage'])}。因此 A1–A8 "
                "全部以“问题→能力假设→数据→护栏”表达，尚未把任何概念写成用户需求。"
            ),
            smart["positive_evidence_ids"]
            + smart["negative_evidence_ids"],
            "smart",
        ),
    ]
    executive_html = "".join(
        (
            f'<article class="decision-card {tone}">'
            f'<span class="decision-index">{number}</span>'
            f"<h3>{_h(title)}</h3><p>{_h(body)}</p>"
            f'{_evidence_button(evidence_ids, label="证据", context=title)}'
            "</article>"
        )
        for number, title, body, evidence_ids, tone in executive_cards
    )

    completeness = sorted(
        report["dimension_census"],
        key=lambda row: row["coverage"],
        reverse=True,
    )
    completeness_rows = "".join(
        (
            '<div class="coverage-row">'
            f'<span>{_h(row["title"])}</span>'
            '<div class="coverage-track">'
            f'<i class="{_grade_class(row["coverage_grade"])}" '
            f'style="width:{max(1, row["coverage"] * 100):.2f}%"></i></div>'
            f'<b>{_num(row["review_entities"])}</b>'
            f'<small>{_pct(row["coverage"])}</small>'
            f'<em class="{_grade_class(row["coverage_grade"])}">'
            f'{_h(_grade_label(row["coverage_grade"]))}</em></div>'
        )
        for row in completeness
    )

    methodology_checks = [
        (
            "全量逐条",
            f'{report["coverage"]["entity_extractor_executions"]:,} '
            "条画像 / 场景提取状态",
        ),
        (
            "核心数字",
            "来自确定性 Report JSON 与规则 / 数值解析",
        ),
        (
            "解释层",
            "Codex 只做产品判断，不覆盖计数、分母和原文",
        ),
        (
            "可展开证据",
            f'{len(evidence_map):,} 条代表性证据；'
            f'涉及 {evidence_audit.get("referenced_review_entities", 0):,} 个评论实体',
        ),
        (
            "去重门禁",
            f'正文原文精确唯一率 '
            f'{_pct(evidence_audit.get("exact_quote_uniqueness_rate"))}；'
            f'同入口实体 / 原文重复 '
            f'{evidence_audit.get("within_group_duplicate_entities", 0)} / '
            f'{evidence_audit.get("within_group_duplicate_exact_quotes", 0)}',
        ),
    ]
    methodology_html = "".join(
        f'<div><b>{_h(title)}</b><span>{_h(body)}</span></div>'
        for title, body in methodology_checks
    )

    scenario_chain = [
        (
            "01",
            "物业与草况",
            f'{yard_size["review_entities"]:,} 条面积表达 · '
            f'{grass["review_entities"]:,} 条草况',
            "先确定要割什么，再判断机器是否适配。",
        ),
        (
            "02",
            "电池与充电",
            f'{charging_flow["review_entities"]:,} 条流程表达',
            "电池组合和充电节奏决定能否连续作业。",
        ),
        (
            "03",
            "割草与操控",
            f'{self_propel["review_entities"]:,} 条自走提及',
            "速度、转向、坡地和切质共同定义任务成功。",
        ),
        (
            "04",
            "草屑处理",
            f'{clipping["review_entities"]:,} 条模式表达',
            "集草、碎草和排草决定停机与二次整理。",
        ),
        (
            "05",
            "清理与收纳",
            f'{storage["review_entities"]:,} 条收纳表达',
            "折叠、搬运和占地决定下次还愿不愿意用。",
        ),
    ]
    chain_html = "".join(
        (
            '<article class="chain-step">'
            f'<span>{_h(number)}</span><h3>{_h(title)}</h3>'
            f'<b>{_h(metric)}</b><p>{_h(body)}</p></article>'
        )
        for number, title, metric, body in scenario_chain
    )

    limitations_html = "".join(
        f"<li>{_h(item)}</li>" for item in report["limitations"]
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0b0e0c">
<title>{_h(brand_label)} Mower｜产品决策洞察报告</title>
<style>
:root {{
  --bg:#090c0a; --bg-soft:#0e1210; --surface:#131815; --surface-2:#18201b;
  --line:#273129; --line-soft:#1d251f; --text:#f4f7f3; --muted:#aab5ac;
  --subtle:#758078; --lime:#9bd62f; --lime-2:#c4ef70; --green:#3ca868;
  --amber:#e1a846; --red:#ef6a62; --cyan:#5bbac2; --ink:#071006;
  --shadow:0 28px 80px rgba(0,0,0,.34); --radius:22px;
  font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Hiragino Sans GB","Microsoft YaHei",sans-serif;
}}
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth;background:var(--bg)}}
body{{margin:0;background:
  radial-gradient(circle at 90% -10%,rgba(119,180,43,.12),transparent 34rem),
  radial-gradient(circle at -10% 24%,rgba(35,125,80,.10),transparent 28rem),
  var(--bg);color:var(--text);font-size:16px;line-height:1.65}}
button,a{{font:inherit}} a{{color:inherit}}
.topbar{{position:sticky;top:0;z-index:40;height:64px;padding:0 max(20px,env(safe-area-inset-left));
  display:flex;align-items:center;gap:26px;border-bottom:1px solid rgba(255,255,255,.07);
  background:rgba(9,12,10,.88);backdrop-filter:blur(18px)}}
.brand{{display:flex;align-items:center;gap:11px;white-space:nowrap;font-weight:750}}
.brand-mark{{width:25px;height:25px;border:6px solid var(--lime);transform:rotate(45deg);
  border-radius:4px;box-shadow:0 0 24px rgba(155,214,47,.28)}}
.brand small{{color:var(--muted);font-weight:500;margin-left:4px}}
.nav{{display:flex;gap:5px;overflow-x:auto;scrollbar-width:none;flex:1}}
.nav::-webkit-scrollbar{{display:none}} .nav a{{text-decoration:none;color:var(--muted);
  padding:7px 11px;border-radius:999px;white-space:nowrap;font-size:13px}}
.nav a:hover,.nav a:focus{{background:var(--surface-2);color:var(--text)}}
.download{{text-decoration:none;color:var(--lime);border:1px solid rgba(155,214,47,.3);
  padding:7px 12px;border-radius:999px;font-size:13px;white-space:nowrap}}
.container{{width:min(1360px,calc(100% - 48px));margin:0 auto}}
.hero{{min-height:calc(100vh - 64px);display:grid;grid-template-columns:minmax(0,1.2fr) minmax(360px,.8fr);
  gap:70px;align-items:center;padding:72px 0 60px}}
.eyebrow{{text-transform:uppercase;letter-spacing:.16em;font-size:12px;font-weight:800;color:var(--lime)}}
.hero h1{{font-size:clamp(44px,5.4vw,84px);line-height:1.02;letter-spacing:-.055em;margin:18px 0 22px;
  max-width:980px}} .hero h1 span{{color:var(--lime)}}
.hero-lead{{font-size:clamp(17px,1.55vw,22px);line-height:1.62;color:var(--muted);max-width:800px;margin:0}}
.hero-meta{{display:flex;flex-wrap:wrap;gap:10px;margin-top:30px}}
.hero-meta span{{padding:8px 12px;border:1px solid var(--line);background:rgba(19,24,21,.72);
  border-radius:999px;color:var(--muted);font-size:13px}}
.hero-visual{{position:relative;min-height:480px;border:1px solid var(--line);border-radius:32px;
  overflow:hidden;background:linear-gradient(150deg,#192219 0%,#0d120f 55%,#131815 100%);box-shadow:var(--shadow)}}
.hero-visual:before{{content:"";position:absolute;inset:-10%;background:
  repeating-linear-gradient(115deg,transparent 0 18px,rgba(155,214,47,.04) 18px 20px);
  transform:rotate(-5deg)}}
.scope-badge{{position:absolute;left:32px;top:30px;padding:7px 10px;border:1px solid rgba(155,214,47,.3);
  background:rgba(7,16,6,.55);border-radius:8px;color:var(--lime);font-size:12px;letter-spacing:.12em}}
.hero-ring{{position:absolute;width:310px;height:310px;border-radius:50%;border:1px solid rgba(155,214,47,.22);
  right:-30px;top:70px;box-shadow:inset 0 0 80px rgba(155,214,47,.05)}}
.hero-ring:before,.hero-ring:after{{content:"";position:absolute;border-radius:50%;border:1px solid rgba(155,214,47,.13)}}
.hero-ring:before{{inset:42px}} .hero-ring:after{{inset:88px;background:rgba(155,214,47,.04)}}
.mower-art{{position:absolute;right:46px;top:158px;width:310px;height:170px;filter:drop-shadow(0 30px 28px rgba(0,0,0,.45))}}
.mower-body{{position:absolute;left:34px;top:52px;width:216px;height:74px;border-radius:38px 48px 14px 12px;background:linear-gradient(180deg,#acd957,#78b92d);border:4px solid #bce96b}}
.mower-deck{{position:absolute;left:18px;top:116px;width:270px;height:10px;border-radius:99px;background:#647067}}
.mower-battery{{position:absolute;left:92px;top:31px;width:98px;height:34px;border:4px solid #bce96b;border-radius:7px 7px 0 0;background:#252f27}}
.mower-battery:after{{content:"";position:absolute;inset:9px 20px;background:#101512;border-radius:4px}}
.mower-wheel{{position:absolute;top:103px;width:67px;height:67px;border:8px solid #6d7a70;border-radius:50%;background:#101512}}
.mower-wheel.one{{left:38px}} .mower-wheel.two{{left:214px}}
.mower-handle{{position:absolute;left:251px;top:4px;width:11px;height:121px;background:#bce96b;border-radius:99px;transform:rotate(-14deg);transform-origin:bottom}}
.mower-handle:before{{content:"";position:absolute;left:-2px;top:-4px;width:66px;height:11px;background:#bce96b;border-radius:99px;transform:rotate(-28deg);transform-origin:left}}
.hero-statement{{position:absolute;left:32px;right:32px;bottom:30px;padding:22px;border-top:1px solid var(--line)}}
.hero-statement span{{font-size:11px;letter-spacing:.16em;color:var(--subtle)}}
.hero-statement b{{display:block;margin-top:8px;font-size:22px;line-height:1.35}}
.hero-statement p{{margin:7px 0 0;color:var(--muted);font-size:14px}}
.metric-strip{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:-28px auto 100px}}
.metric-card{{padding:20px 21px;background:rgba(19,24,21,.9);border:1px solid var(--line);
  border-radius:17px;min-height:132px}} .metric-card.focus{{border-color:rgba(155,214,47,.35)}}
.metric-label{{color:var(--muted);font-size:13px}} .metric-value{{font-size:34px;font-weight:780;
  letter-spacing:-.04em;margin:7px 0 4px}} .metric-note{{font-size:12px;color:var(--subtle)}}
.section{{padding:98px 0;border-top:1px solid var(--line-soft);scroll-margin-top:64px}}
.section-layout{{display:grid;grid-template-columns:300px minmax(0,1fr);gap:70px;align-items:start}}
.section-intro{{position:sticky;top:96px}} .section-number{{font-size:12px;font-weight:850;color:var(--subtle);
  border-bottom:1px solid var(--line);padding-bottom:13px;margin-bottom:26px}}
.section-intro h2{{font-size:clamp(30px,3.2vw,49px);line-height:1.08;letter-spacing:-.04em;margin:13px 0 18px}}
.section-intro>p{{color:var(--muted);font-size:15px;margin:0}}
.takeaway{{margin-top:28px;padding:17px 18px;border-left:3px solid var(--lime);background:var(--surface);
  color:var(--text);font-size:14px;border-radius:0 12px 12px 0}}
.takeaway span{{display:block;color:var(--lime);font-size:10px;letter-spacing:.14em;font-weight:800;margin-bottom:6px}}
.content-stack{{display:grid;gap:26px;min-width:0;max-width:100%}}
.decision-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}
.decision-card{{position:relative;padding:28px;border:1px solid var(--line);border-radius:var(--radius);
  background:linear-gradient(145deg,var(--surface),rgba(15,20,17,.72));min-height:242px}}
.decision-card:first-child{{grid-column:1/-1;min-height:220px}}
.decision-card h3{{font-size:22px;line-height:1.34;margin:20px 0 12px;max-width:750px}}
.decision-card p{{color:var(--muted);margin:0 0 22px;font-size:14px}}
.decision-index{{font-size:12px;font-weight:850;color:var(--subtle)}} .decision-card.strength{{border-top:3px solid var(--lime)}}
.decision-card.focus{{border-top:3px solid var(--cyan)}} .decision-card.people{{border-top:3px solid var(--amber)}}
.decision-card.risk{{border-top:3px solid var(--red)}} .decision-card.smart{{border-top:3px solid #a58af0}}
.method-banner{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;border:1px solid var(--line);
  background:var(--line);border-radius:17px;overflow:hidden}}
.method-banner>div{{padding:17px;background:var(--surface)}} .method-banner b{{display:block;color:var(--lime);font-size:12px}}
.method-banner span{{display:block;color:var(--muted);font-size:12px;margin-top:5px}}
.coverage-panel,.panel{{padding:28px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);min-width:0;max-width:100%}}
.panel-head{{display:flex;justify-content:space-between;gap:20px;align-items:end;margin-bottom:22px}}
.panel-head h3{{font-size:22px;margin:0}} .panel-head p{{margin:0;color:var(--muted);font-size:13px;max-width:580px}}
.coverage-list{{display:grid;gap:11px}} .coverage-row{{display:grid;grid-template-columns:160px minmax(90px,1fr) 55px 55px 94px;
  align-items:center;gap:12px;font-size:13px}}
.coverage-row>span{{color:var(--muted)}} .coverage-track{{height:7px;background:#202821;border-radius:99px;overflow:hidden}}
.coverage-track i{{display:block;height:100%;border-radius:99px}} .coverage-track i.good{{background:var(--green)}}
.coverage-track i.warn{{background:var(--amber)}} .coverage-track i.risk{{background:var(--red)}} .coverage-track i.muted{{background:var(--subtle)}}
.coverage-row b{{text-align:right}} .coverage-row small{{color:var(--subtle);text-align:right}}
.coverage-row em{{font-style:normal;text-align:center;padding:3px 6px;border-radius:99px;font-size:10px}}
em.good,.coverage-dot.good{{color:#6dda93;background:rgba(60,168,104,.14)}} em.warn,.coverage-dot.warn{{color:#f2c46f;background:rgba(225,168,70,.14)}}
em.risk,.coverage-dot.risk{{color:#ff8b83;background:rgba(239,106,98,.14)}} em.muted,.coverage-dot.muted{{color:var(--subtle);background:#1b211d}}
.dimension-list{{display:grid;gap:10px}} .dimension-card{{border:1px solid var(--line);border-radius:16px;background:var(--surface);overflow:hidden}}
.dimension-card summary{{list-style:none;display:flex;justify-content:space-between;gap:20px;align-items:center;padding:19px 21px;cursor:pointer}}
.dimension-card summary::-webkit-details-marker{{display:none}} .dimension-card summary:after{{content:"＋";color:var(--subtle);font-size:20px}}
.dimension-card[open] summary:after{{content:"−"}} .dimension-title{{display:flex;gap:13px;align-items:start}}
.coverage-dot{{width:10px;height:10px;border-radius:50%;margin-top:7px;flex:0 0 auto}}
.dimension-title h3{{margin:0;font-size:16px}} .dimension-title p{{margin:3px 0 0;color:var(--subtle);font-size:12px}}
.dimension-stat{{display:grid;grid-template-columns:60px 58px 82px;gap:8px;align-items:center;text-align:right;margin-left:auto}}
.dimension-stat b{{font-size:17px}} .dimension-stat span{{color:var(--muted);font-size:12px}}
.dimension-stat em{{font-style:normal;text-align:center;padding:3px 6px;border-radius:99px;font-size:10px}}
.dimension-body{{padding:4px 21px 20px;border-top:1px solid var(--line-soft)}} .bar-row{{display:grid;grid-template-columns:minmax(0,1fr) auto;
  gap:7px 14px;align-items:center;padding:14px 0;border-bottom:1px solid var(--line-soft)}}
.bar-head{{grid-column:1/-1;display:flex;justify-content:space-between;gap:14px;font-size:13px}}
.bar-head span{{color:var(--muted)}} .bar-head small{{color:var(--subtle);font-weight:500}}
.bar-track{{height:6px;background:#202821;border-radius:99px;overflow:hidden}} .bar-track i{{display:block;height:100%;background:linear-gradient(90deg,var(--green),var(--lime));border-radius:99px}}
.evidence-btn{{display:inline-flex;align-items:center;gap:8px;width:max-content;color:var(--lime-2);background:transparent;
  border:1px solid rgba(155,214,47,.22);border-radius:999px;padding:6px 10px;cursor:pointer;font-size:11px}}
.evidence-btn:hover,.evidence-btn:focus{{border-color:var(--lime);background:rgba(155,214,47,.08)}} .evidence-btn b{{background:rgba(155,214,47,.12);padding:0 5px;border-radius:99px}}
.evidence-btn em{{font-style:normal;color:var(--subtle);font-size:10px}}
.no-evidence{{color:var(--subtle);font-size:11px}} .boundary{{color:var(--subtle)!important;font-size:11px!important;margin:14px 0 0!important}}
.empty-state{{padding:18px;color:var(--muted);font-size:13px}}
.persona-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}} .persona-card{{padding:25px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface)}}
.persona-card:first-child{{grid-column:1/-1}} .persona-top{{display:flex;justify-content:space-between;align-items:center}}
.persona-index{{color:var(--lime);font-weight:850;letter-spacing:.12em;font-size:12px}} .sample-chip{{font-size:11px;color:var(--muted);background:var(--bg-soft);padding:5px 8px;border-radius:99px}}
.persona-card h3{{font-size:23px;line-height:1.3;margin:17px 0 8px}} .definition{{font-size:13px;color:var(--muted);min-height:42px}}
.persona-jtbd{{margin:19px 0;padding:15px;border-left:3px solid var(--lime);background:var(--bg-soft);font-size:14px}}
.persona-jtbd span,.lift-title{{display:block;color:var(--subtle);font-size:10px;letter-spacing:.12em;font-weight:800;margin-bottom:5px}}
.mini-kpis{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}} .mini-kpis span{{font-size:11px;color:var(--muted);border:1px solid var(--line);padding:5px 8px;border-radius:8px}}
.mini-kpis b{{color:var(--text)}} .lift-list{{display:grid;gap:8px;margin:10px 0 20px}} .lift-row{{display:grid;grid-template-columns:1fr auto;gap:1px 10px;padding:9px 0;border-bottom:1px solid var(--line-soft);font-size:12px}}
.lift-row span{{color:var(--muted)}} .lift-row b{{color:var(--lime)}} .lift-row small{{grid-column:1/-1;color:var(--subtle)}}
.scenario-chain{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;position:relative}}
.scenario-chain:before{{content:"";position:absolute;left:8%;right:8%;top:36px;height:1px;background:linear-gradient(90deg,var(--green),var(--lime))}}
.chain-step{{position:relative;padding:60px 15px 20px;border:1px solid var(--line);border-radius:16px;background:var(--surface);min-height:220px}}
.chain-step>span{{position:absolute;top:21px;left:18px;width:30px;height:30px;display:grid;place-items:center;border-radius:50%;background:var(--lime);color:var(--ink);font-size:10px;font-weight:900}}
.chain-step h3{{font-size:16px;margin:0 0 12px}} .chain-step b{{display:block;color:var(--lime-2);font-size:12px}}
.chain-step p{{font-size:12px;color:var(--muted);margin:10px 0 0}}
.split-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;min-width:0;max-width:100%}} .rank-panel{{padding:25px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);min-width:0;max-width:100%}}
.rank-panel h3{{margin:0;font-size:23px}} .rank-panel>p{{margin:7px 0 23px;color:var(--muted);font-size:12px}}
.rank-row{{display:grid;grid-template-columns:28px minmax(0,1fr);gap:10px;padding:13px 0;border-bottom:1px solid var(--line-soft)}}
.rank-num{{color:var(--subtle);font-size:11px;padding-top:2px}} .rank-head{{display:flex;justify-content:space-between;gap:12px;align-items:center}}
.rank-head b{{font-size:13px}} .rank-head span{{color:var(--subtle);font-size:10px;text-align:right}}
.rank-track{{height:5px;background:#212922;border-radius:99px;margin:8px 0}} .rank-track i{{display:block;height:100%;border-radius:99px;background:var(--green)}}
.rank-panel.cons .rank-track i{{background:var(--red)}} .rank-foot{{display:flex;align-items:center;justify-content:space-between;gap:10px;color:var(--subtle);font-size:10px}}
.table-wrap{{overflow:auto;min-width:0;max-width:100%;border:1px solid var(--line);border-radius:16px;background:var(--surface)}} .data-table{{border-collapse:collapse;width:100%;min-width:900px;font-size:12px}}
.data-table th{{position:sticky;top:0;background:#171d19;color:var(--subtle);font-size:10px;letter-spacing:.06em;text-align:right;padding:12px 14px;border-bottom:1px solid var(--line)}}
.data-table th:first-child,.data-table td:first-child{{text-align:left}} .data-table td{{padding:12px 14px;text-align:right;border-bottom:1px solid var(--line-soft);white-space:nowrap}}
.data-table td:first-child b{{display:block}} .data-table td:first-child small{{display:block;color:var(--subtle);font-size:9px}}
.data-table .positive{{color:#6dda93}} .data-table .negative{{color:#ff8b83}} .balance{{font-size:10px;padding:3px 6px;border-radius:8px}}
.balance.up{{color:#6dda93;background:rgba(60,168,104,.1)}} .balance.down{{color:#ff8b83;background:rgba(239,106,98,.1)}}
.kano-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}} .kano-lane{{border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);overflow:hidden}}
.kano-head{{padding:24px;border-bottom:1px solid var(--line)}} .kano-head>span{{display:inline-block;color:var(--ink);background:var(--lime);font-size:10px;font-weight:900;padding:4px 7px;border-radius:5px}}
.kano-lane.must_be .kano-head>span{{background:var(--red)}} .kano-lane.performance .kano-head>span{{background:var(--cyan)}}
.kano-lane.attractive .kano-head>span{{background:var(--lime)}} .kano-lane.unclear .kano-head>span{{background:var(--subtle)}}
.kano-head h3{{font-size:22px;margin:13px 0 6px}} .kano-head p{{color:var(--muted);font-size:12px;margin:0}}
.kano-items{{padding:0 24px}} .kano-item{{padding:18px 0;border-bottom:1px solid var(--line-soft)}} .kano-item h4{{margin:0;font-size:15px}}
.kano-item>p{{margin:4px 0 11px;color:var(--muted);font-size:11px}} .kano-stats{{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:12px}}
.kano-stats span{{font-size:10px;color:var(--subtle);border:1px solid var(--line);padding:4px 6px;border-radius:7px}} .kano-stats b{{color:var(--text)}}
.kano-lane>.boundary{{padding:0 24px 20px}}
.opportunity-list{{display:grid;gap:16px}} .opportunity-card{{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:12px 35px;padding:31px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface)}}
.opp-rank{{grid-column:1/-1;display:flex;justify-content:space-between;color:var(--subtle);font-size:10px;letter-spacing:.12em}}
.opp-rank b{{color:var(--lime)}} .opportunity-card h3{{font-size:27px;line-height:1.25;margin:7px 0 3px;letter-spacing:-.02em}}
.opportunity-card .problem{{color:var(--muted);font-size:14px;margin:0 0 12px}} .opp-metrics{{grid-row:2/6;grid-column:2;display:grid;grid-template-columns:1fr 1fr;gap:8px;align-content:start}}
.opp-metrics div{{padding:14px;background:var(--bg-soft);border:1px solid var(--line-soft);border-radius:12px}} .opp-metrics b{{display:block;font-size:22px}}
.opp-metrics span{{font-size:9px;color:var(--subtle)}} .opp-direction,.opp-validation{{padding:15px 0;border-top:1px solid var(--line-soft)}}
.opp-direction span,.opp-validation span{{display:block;color:var(--lime);font-size:10px;letter-spacing:.12em;font-weight:800}}
.opp-direction p,.opp-validation p{{margin:4px 0 0;font-size:13px;color:var(--muted)}} .opportunity-card>.evidence-btn{{margin-top:4px}}
.smart-principle{{padding:28px;border:1px solid rgba(165,138,240,.32);border-radius:var(--radius);background:linear-gradient(120deg,rgba(165,138,240,.09),var(--surface))}}
.smart-principle h3{{font-size:24px;margin:0 0 9px}} .smart-principle p{{color:var(--muted);margin:0}}
.smart-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}} .smart-card{{padding:25px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface)}}
.smart-code{{display:flex;align-items:center;gap:9px}} .smart-code b{{color:var(--ink);background:#a58af0;border-radius:6px;padding:4px 7px;font-size:11px}}
.smart-code span{{color:#cbbdfa;font-size:11px}} .smart-code em{{margin-left:auto;color:var(--subtle);font-size:9px;font-style:normal;border:1px solid var(--line);padding:3px 6px;border-radius:99px}}
.smart-card h3{{font-size:20px;margin:18px 0 7px}} .smart-card>p{{color:var(--muted);font-size:13px;min-height:43px}}
.smart-card dl{{margin:17px 0;display:grid;gap:8px}} .smart-card dl>div{{display:grid;grid-template-columns:76px 1fr;gap:9px;padding-top:8px;border-top:1px solid var(--line-soft)}}
.smart-card dt{{font-size:10px;color:var(--subtle)}} .smart-card dd{{font-size:11px;color:var(--muted);margin:0}}
.next-grid{{display:grid;grid-template-columns:1.25fr .75fr;gap:14px}} .next-card{{padding:27px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface)}}
.next-card h3{{font-size:23px;margin:0 0 14px}} .next-card ol,.next-card ul{{padding-left:20px;margin:0;color:var(--muted)}}
.next-card li{{padding:7px 0}} .next-card li::marker{{color:var(--lime);font-weight:800}}
.report-footer{{padding:50px 0 80px;border-top:1px solid var(--line);color:var(--subtle);font-size:11px}}
.footer-grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}} .footer-grid code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);word-break:break-all}}
dialog{{padding:0;border:1px solid var(--line);border-radius:24px;background:var(--surface);color:var(--text);
  width:min(760px,calc(100% - 32px));max-height:min(82vh,900px);box-shadow:var(--shadow)}}
dialog::backdrop{{background:rgba(0,0,0,.72);backdrop-filter:blur(5px)}} .drawer-head{{position:sticky;top:0;z-index:2;display:flex;justify-content:space-between;gap:20px;padding:20px 22px;border-bottom:1px solid var(--line);background:rgba(19,24,21,.94);backdrop-filter:blur(15px)}}
.drawer-head span{{color:var(--lime);font-size:10px;letter-spacing:.12em}} .drawer-head h3{{margin:4px 0 0;font-size:19px}} .close-btn{{width:36px;height:36px;border-radius:50%;border:1px solid var(--line);background:var(--bg-soft);color:var(--text);cursor:pointer}}
.drawer-body{{padding:10px 22px 24px;overflow:auto}} .quote-card{{padding:19px 0;border-bottom:1px solid var(--line-soft)}} .quote-card blockquote{{margin:0;font-size:15px;line-height:1.65;color:var(--text)}}
.quote-meta{{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}} .quote-meta span{{font-size:10px;color:var(--subtle);background:var(--bg-soft);border:1px solid var(--line-soft);padding:4px 6px;border-radius:7px}}
.quote-match{{color:var(--lime-2)!important}} .drawer-empty{{padding:30px 0;color:var(--muted)}}
.source-field-card blockquote{{color:var(--lime-2)}} .source-field-note{{margin:8px 0 0;color:var(--muted);font-size:12px}}
@media(max-width:1050px){{
  .hero{{grid-template-columns:1fr;gap:36px;padding-top:54px}} .hero-visual{{min-height:420px}}
  .metric-strip{{grid-template-columns:repeat(3,1fr)}} .section-layout{{grid-template-columns:1fr;gap:38px}}
  .section-intro{{position:static;max-width:760px}} .scenario-chain{{grid-template-columns:repeat(3,1fr)}}
  .opportunity-card{{grid-template-columns:1fr}} .opp-metrics{{grid-row:auto;grid-column:auto;grid-template-columns:repeat(4,1fr)}}
}}
@media(max-width:760px){{
  body{{font-size:15px}} .topbar{{height:58px;gap:13px;padding-left:14px;padding-right:14px}}
  .brand small,.download{{display:none}} .nav{{margin-left:auto}} .nav a{{font-size:11px;padding:6px 8px}}
  .container{{width:min(100% - 28px,1360px)}} .hero{{min-height:auto;padding:48px 0 42px}}
  .hero h1{{font-size:43px}} .hero-lead{{font-size:16px}} .hero-visual{{min-height:360px;border-radius:24px}}
  .hero-ring{{width:250px;height:250px;right:-58px;top:60px}} .mower-art{{right:0;top:125px;transform:scale(.82);transform-origin:right top}}
  .scope-badge{{left:20px;top:20px}} .hero-statement{{left:20px;right:20px;bottom:18px;padding:16px 0 0}}
  .hero-statement b{{font-size:18px}} .metric-strip{{grid-template-columns:1fr 1fr;margin:0 auto 70px;gap:8px}}
  .metric-card{{min-height:116px;padding:16px}} .metric-value{{font-size:27px}}
  .section{{padding:70px 0}} .section-intro h2{{font-size:35px}} .decision-grid,.persona-grid,.split-grid,.kano-grid,.smart-grid,.next-grid{{grid-template-columns:1fr}}
  .decision-card:first-child,.persona-card:first-child{{grid-column:auto}} .decision-card{{min-height:auto;padding:22px}}
  .method-banner{{grid-template-columns:1fr 1fr}} .coverage-panel,.panel{{padding:19px;overflow:hidden}}
  .content-stack,.split-grid,.rank-panel,.table-wrap{{width:100%;max-width:100%;min-width:0}}
  .coverage-row{{grid-template-columns:110px minmax(50px,1fr) 42px 74px}} .coverage-row small{{display:none}}
  .dimension-card summary{{padding:16px;align-items:flex-start}} .dimension-title p{{display:none}}
  .dimension-stat{{grid-template-columns:42px 49px;margin-top:1px}} .dimension-stat em{{display:none}}
  .dimension-body{{padding-left:16px;padding-right:16px}} .persona-card{{padding:21px}}
  .scenario-chain{{display:flex;overflow-x:auto;scroll-snap-type:x mandatory;padding-bottom:6px}}
  .scenario-chain:before{{display:none}} .chain-step{{min-width:225px;scroll-snap-align:start}}
  .rank-panel{{padding:20px}} .rank-head{{align-items:start}} .rank-head span{{max-width:120px}}
  .rank-foot>span{{max-width:170px}} .kano-head,.kano-items{{padding-left:19px;padding-right:19px}}
  .opportunity-card{{padding:23px;gap:12px}} .opportunity-card h3{{font-size:23px}} .opp-metrics{{grid-template-columns:1fr 1fr}}
  .smart-card{{padding:21px}} .footer-grid{{grid-template-columns:1fr}} dialog{{width:100%;max-width:none;max-height:88vh;margin:auto 0 0;border-radius:24px 24px 0 0}}
}}
@media(max-width:410px){{
  .hero h1{{font-size:37px}} .metric-strip{{grid-template-columns:1fr 1fr}} .metric-card{{min-height:105px}}
  .metric-value{{font-size:24px}} .method-banner{{grid-template-columns:1fr}} .coverage-row{{grid-template-columns:98px minmax(40px,1fr) 38px 70px;gap:7px}}
  .dimension-title h3{{font-size:14px}} .dimension-stat{{grid-template-columns:38px 44px;gap:4px}}
}}
@media print{{
  .topbar,.evidence-btn,dialog{{display:none!important}} body{{background:white;color:#111}} .section{{break-inside:avoid;border-color:#ddd}}
  .section-intro{{position:static}} .surface,.panel,.metric-card,.decision-card,.persona-card,.rank-panel,.kano-lane,.opportunity-card,.smart-card{{background:#fff;color:#111;border-color:#ddd}}
}}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand"><span class="brand-mark"></span>数据采集 VoxQuarry <small>Product Insight</small></div>
  <nav class="nav" aria-label="报告章节">
    <a href="#summary">决策摘要</a><a href="#profile">用户画像</a><a href="#scenario">使用场景</a>
    <a href="#experience">Pros & Cons</a><a href="#kano">Kano</a>
    <a href="#opportunity">产品机会</a><a href="#smart">智能化机会</a><a href="#next">行动与边界</a>
  </nav>
  <a class="download" href="report-share.json" download>下载结构化数据</a>
</header>
<main>
  <section class="container hero">
    <div>
      <div class="eyebrow">{_h(brand_label)} · Walk-behind mower · Voice of customer</div>
      <h1>从 {_num(sample["review_entities"])} 条网评<br><span>{html.escape(str(report.get("hero_title") or "还原完整产品任务"))}</span></h1>
      <p class="hero-lead">这不是 28 个脚本的执行日志，而是一份面向产品决策的洞察报告：
      先把“谁在用、在什么草况下用、怎么充电、能否一次割完、哪里值得守、哪里必须改”说清楚，
      再讨论产品机会与智能化机会。</p>
      <div class="hero-meta">
        <span>品牌 {_h(brand_label)}</span><span>品类 Walk-behind mower</span>
        <span>渠道 {_h(channel_label)}</span><span>{_num(sample["written_review_entities"])} 条正文</span>
        <span>截至 {_h(latest_date or "日期未知")}</span>
      </div>
    </div>
    <div class="hero-visual" aria-hidden="true">
      <div class="scope-badge">TASK-FIRST REPORT</div><div class="hero-ring"></div>
      <div class="mower-art"><i class="mower-handle"></i><i class="mower-battery"></i>
        <i class="mower-body"></i><i class="mower-deck"></i>
        <i class="mower-wheel one"></i><i class="mower-wheel two"></i></div>
      <div class="hero-statement"><span>核心产品命题</span>
        <b>电动化的优势已被证明；下一步要把“省事”兑现成可预期、可完成、可恢复。</b>
        <p>Strength → Trust → Finishability → Intelligence</p>
      </div>
    </div>
  </section>
  <section class="container metric-strip" aria-label="核心数据">
    {_metric_card("唯一评论", _num(sample["review_entities"]), "分析单位：去重 review entity")}
    {_metric_card("总体口碑", f'{sample["average_rating"]:.2f}', f'好评率 {_pct(sample["positive_rate"])}')}
    {_metric_card("差评率", _pct(sample["negative_rate"]), "1–2★ / 有效评分", "focus")}
    {_metric_card("OPE 维度", _num(len(report["dimension_census"])), "画像 + 场景专属维度")}
    {_metric_card("全量执行", _num(report["coverage"]["entity_extractor_executions"]), "画像 / 场景状态，失败 0")}
  </section>

  <section class="section" id="summary"><div class="container section-layout">
    {_section_intro("01 / 08","Executive summary","先给产品团队的五个判断",
      f'总体 {sample["average_rating"]:.2f} 星说明用户接受电动化，不代表每个任务环节都健康。本章把需要守住的优势、优先研究的断点和不能越界的结论先说透。',
      "保留安静、免燃油和易用优势；优先解决完成度、自走可控与可靠性闭环；智能化从问题出发。")}
    <div class="content-stack">
      <div class="decision-grid">{executive_html}</div>
      <div class="method-banner">{methodology_html}</div>
      <div class="coverage-panel">
        <div class="panel-head"><div><div class="eyebrow">Coverage before conclusion</div><h3>先看我们到底知道多少</h3></div>
        <p>覆盖率是“评论中明确写出该维度”的比例，不是人群或场景的真实发生率。低覆盖项在报告中保留为缺口。</p></div>
        <div class="coverage-list">{completeness_rows}</div>
      </div>
    </div>
  </div></section>

  <section class="section" id="profile"><div class="container section-layout">
    {_section_intro("02 / 08","Who","用户是谁：先回答身份，再分析任务",
      "只有明确职业、工作责任、年龄 / 体能和经验进入画像；家庭代买、赠礼、共用和使用场景不作为用户身份。",
      "职业未知必须保留未知；任务、材料和工作流在下一章节独立量化。")}
    <div class="content-stack">
      <div class="panel">
        <div class="panel-head"><div><div class="eyebrow">Dimension census</div><h3>画像维度完整度</h3></div>
        <p>职业不能从普通“my yard”、家庭成员或产品用途推断；年龄只认来源结构化字段或正文明确自述。当前年龄证据 {_num(age["review_entities"])} 条，明确职业正文证据 {_num(occupation["review_entities"])} 条，其余职业保持未知。</p></div>
        <div class="dimension-list">{"".join(_dimension_card(row) for row in profile_dimensions)}</div>
      </div>
      <div><div class="panel-head"><div><div class="eyebrow">Evidence-based cohorts</div><h3>有明确身份依据的用户群</h3></div>
      <p>每张卡片只使用职业、责任、年龄 / 体能或经验信号。它们可支持研究招募，不是市场分群占比。</p></div>
      <div class="persona-grid">{"".join(_persona_card(row, i) for i,row in enumerate(personas,1))}</div></div>
    </div>
  </div></section>

  <section class="section" id="scenario"><div class="container section-layout">
    {_section_intro("03 / 08","Context & jobs","使用场景：还原完整割草任务链",
      "不同品类的场景维度必须不同。对割草机，地理、草种、面积、地形、草况、割高、时长、电池组合、充电、草屑处理和收纳共同定义任务。",
      f'现有数据中面积 {_num(yard_size["review_entities"])} 条、草况 {_num(grass["review_entities"])} 条、充电流程 {_num(charging_flow["review_entities"])} 条；地理、草种、割高和时段按实际覆盖展示，不做补齐。')}
    <div class="content-stack">
      <div class="scenario-chain">{chain_html}</div>
      <div class="panel">
        <div class="panel-head"><div><div class="eyebrow">Mower-specific context</div><h3>割草机场景维度全景</h3></div>
        <p>点击任一维度可展开量化分布和原文。稀疏项不会被隐藏，也不会用大模型补全。</p></div>
        <div class="dimension-list">{"".join(_dimension_card(row) for row in scenario_dimensions)}</div>
      </div>
    </div>
  </div></section>

  <section class="section" id="experience"><div class="container section-layout">
    {_section_intro("04 / 08","Experience","产品体验：Pros & Cons 必须落到属性",
      "整条评论的星级不能代替句级属性评价。本章同时展示属性提及、同句明确正负向和评论级低星关联，避免把高频关键词当痛点。",
      "安静、操控、动力和易装配是强项；电池 / 充电、可靠性、自走和售后是需要拆解的压力点。")}
    <div class="content-stack">
      <div class="split-grid">
        <div class="rank-panel pros"><div class="eyebrow">Pros · explicit language</div><h3>用户明确说好的是什么</h3>
          <p>按“同句明确正向评论数”排名；不是用 4–5 星自动当作好评。</p>{_rank_rows(aspects, mode="positive")}
        </div>
        <div class="rank-panel cons"><div class="eyebrow">Cons · explicit language</div><h3>用户明确说不好的是什么</h3>
          <p>按“同句明确负向评论数”排名，并同时显示 1–2★ 关联。</p>{_rank_rows(aspects, mode="negative")}
        </div>
      </div>
      <div class="panel">
        <div class="panel-head"><div><div class="eyebrow">Attribute evidence matrix</div><h3>21 个属性的完整量化表</h3></div>
        <p>“净显式声量”只用于快速浏览，不是满意度分数；产品判断仍需结合样本量、均分、低星和原文。</p></div>
        {_aspect_table(aspects)}
      </div>
    </div>
  </div></section>

  <section class="section" id="kano"><div class="container section-layout">
    {_section_intro("05 / 08","Kano hypothesis","Kano：区分底线、竞争力与记忆点",
      "网评能观察到缺失后的愤怒、表现变化后的满意度和意外惊喜，但不能替代标准 Kano 双问卷。因此这里给的是可追溯的产品假设。",
      "先守住可靠启动、安全与电池工作；用续航、切质、自走和草屑处理竞争；把安静、免燃油与收纳变成记忆点。")}
    <div class="content-stack">
      <div class="kano-grid">{"".join(_kano_lane(row) for row in report["kano"])}</div>
    </div>
  </div></section>

  <section class="section" id="opportunity"><div class="container section-layout">
    {_section_intro("06 / 08","Product opportunity","产品机会：从症状走到可验证的问题",
      "机会不是一个黑盒总分。每个机会都同时保留问题空间样本、负向 / 低星信号、明确需求、产品方向、验证方法和原文。",
      "优先建立可靠性恢复与整院完成度闭环，再把自走、复杂草况、草屑处理和收纳做成端到端任务。")}
    <div class="content-stack">
      <div class="opportunity-list">{"".join(_opportunity_card(row,i) for i,row in enumerate(report["product_opportunities"],1))}</div>
    </div>
  </div></section>

  <section class="section" id="smart"><div class="container section-layout">
    {_section_intro("07 / 08","Smart opportunity","智能化机会：问题先于功能",
      "直接的 App、连接或智能需求只占很小比例。真正可用的方法是把已经量化的问题映射到能力补全、摩擦消除、认知降负等八种价值创造，再定义数据和护栏。",
      "最强方向是诊断 / 预测维护、整院完成度预测、草况自适应与以人步速为中心的自走控制；全部仍需概念验证。")}
    <div class="content-stack">
      <div class="smart-principle"><h3>明确区分两类证据</h3>
        <p><b>直接需求：</b>用户明确提到 App、自动化、连接或传感。
        <b>推导机会：</b>用户描述问题，我们再判断哪些智能能力可能解决。
        后者必须标记为假设，不能反写成“用户要求”。</p>
      </div>
      <div class="smart-grid">{"".join(_smart_card(row) for row in report["smart_opportunities"])}</div>
    </div>
  </div></section>

  <section class="section" id="next"><div class="container section-layout">
    {_section_intro("08 / 08","Decision & research","下一步：把洞察变成可验证决策",
      "这份报告已经把问题空间、证据和假设分开。下一步不是继续堆摘要，而是补齐关键字段、连接工程与服务事实、建立概念验证和竞品对照。",
      "先验证整院完成度、可靠性恢复和自走人因；再进入真正的竞品 GAP 与智能概念测试。")}
    <div class="content-stack">
      <div class="next-grid">
        <div class="next-card"><h3>建议的产品研究顺序</h3><ol>
          <li>建立“面积 × 草种 / 草况 × 割高 × 电池组合”的完整任务测试矩阵。</li>
          <li>把可靠性评论按型号、部件、购后时长、触发条件与服务结果做根因复核。</li>
          <li>招募银发、体能敏感、坡地和大院用户，验证自走起步、转弯、步速与收纳任务。</li>
          <li>加入至少 2–3 个直接竞品和自家新旧型号，才能生成真正的属性 SOV 与 GAP。</li>
          <li>对 A1–A8 概念做价值、信任、数据授权和误报容忍度测试，再决定是否进入 PRD。</li>
        </ol></div>
        <div class="next-card"><h3>必须补齐的数据</h3><ul>
          <li>评论者地理位置 / 气候区：当前 {_num(geography["review_entities"])} 条明确文本。</li>
          <li>草种：{_num(grass_species["review_entities"])} 条；割高：{_num(cut_height["review_entities"])} 条；割草时段：{_num(time_of_day["review_entities"])} 条。</li>
          <li>结构化院子面积、坡度、障碍密度与真实单次作业时长。</li>
          <li>设备序列、购后时长、保修工单、部件故障与处理结果。</li>
          <li>竞品同口径评论与官方性能声明。</li>
        </ul></div>
      </div>
      <div class="panel"><div class="panel-head"><div><div class="eyebrow">Confidence & limits</div><h3>置信度与边界</h3></div>
      <p>每次面向新受众重新包装，都必须重新执行证据门槛审查。</p></div><ul>{limitations_html}</ul></div>
    </div>
  </div></section>
</main>
<footer class="report-footer"><div class="container footer-grid">
  <div><b>数据采集 VoxQuarry · {_h(brand_label)} Mower Product Insight</b><br>
  结构化数据与本页来自同一报告对象；第三方 LLM API 未使用；核心指标未被大模型修改。</div>
  <div>Report <code>{_h(report["report_id"])}</code><br>
  Snapshot <code>{_h(report["data_snapshot_id"])}</code><br>
  Hash <code>{_h(report["content_hash"])}</code></div>
</div></footer>

<dialog id="evidence-dialog">
  <div class="drawer-head"><div><span>VERBATIM EVIDENCE</span><h3 id="drawer-title">原文证据</h3></div>
  <button class="close-btn" id="drawer-close" type="button" aria-label="关闭">×</button></div>
  <div class="drawer-body" id="drawer-body"></div>
</dialog>
<script id="report-runtime" type="application/json">{embedded}</script>
<script>
(() => {{
  "use strict";
  const payload = JSON.parse(document.getElementById("report-runtime").textContent);
  const dialog = document.getElementById("evidence-dialog");
  const body = document.getElementById("drawer-body");
  const title = document.getElementById("drawer-title");
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]));
  const stars = (rating) => rating == null ? "未提供评分" : `${{rating}} / 5★`;
  function openEvidence(button) {{
    const ids = (button.dataset.evidence || "").split(",").filter(Boolean);
    const rows = ids.map(id => payload.evidence[id]).filter(Boolean);
    title.textContent = button.dataset.context || "原文证据";
    body.innerHTML = rows.length ? rows.map((row,index) => {{
      const sourceField = row.evidence_type === "source_field";
      return `
      <article class="quote-card ${{sourceField ? "source-field-card" : ""}}">
        <blockquote>${{sourceField ? `来源字段：${{esc(row.source_field)}} = ${{esc(row.source_value)}}` : `“${{esc(row.quote)}}”`}}</blockquote>
        ${{sourceField ? '<p class="source-field-note">这是来源站点提供的结构化用户档案，不是从正文推断，也不伪装成用户原话。</p>' : ''}}
        <div class="quote-meta">
          <span>${{index + 1}} / ${{rows.length}}</span>
          <span>${{sourceField ? "结构化来源字段" : "评论正文原文"}}</span>
          <span>${{esc(stars(row.rating))}}</span>
          <span>${{esc(row.channel || "未知渠道")}}</span><span>${{esc(row.model || "未知型号")}}</span>
          <span>${{esc(row.submission_date || "日期未知")}}</span>
          ${{row.source_context && row.source_context.age ? `<span>来源年龄：${{esc(row.source_context.age)}}</span>` : ""}}
          <span class="quote-match">命中：${{esc(row.matched_text || row.label)}}</span>
        </div>
      </article>`;
    }}).join("") : '<div class="drawer-empty">当前按钮没有可显示的证据。</div>';
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open","");
  }}
  document.addEventListener("click", event => {{
    const button = event.target.closest("[data-evidence]");
    if (button) openEvidence(button);
  }});
  document.getElementById("drawer-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", event => {{
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  }});
}})();
</script>
</body></html>
"""


def write_html(report: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(build_html(report), encoding="utf-8")
    temporary.replace(output_path)
