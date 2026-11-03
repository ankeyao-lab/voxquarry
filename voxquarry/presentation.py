"""Localized rendering never rewrites canonical metric values or evidence IDs."""
METRICS = {
    "sample_size": ("评论样本", "Review sample"), "written_review_count": ("文字评论", "Written reviews"),
    "written_coverage": ("文字覆盖率", "Written coverage"), "average_rating": ("平均评分", "Average rating"),
    "positive_rating_rate": ("好评率（4–5 星）", "Positive ratings (4–5 stars)"),
    "negative_rating_rate": ("差评率（1–2 星）", "Negative ratings (1–2 stars)"),
    "recommendation_rate": ("推荐率", "Recommendation rate"), "factor_extraction_coverage": ("主题命中覆盖率", "Topic match coverage"),
    "atomic_v2_review_coverage": ("规则覆盖率", "Rule coverage")}


def label(identifier):
    return identifier.replace("_", " ").capitalize()


def report_markdown(report, locale="en"):
    zh = locale == "zh-CN"
    pick = lambda a, b: a if zh else b
    title = report.get('display_name') or report['title']
    if title == 'M40 · Synthetic review study':
        title = pick('M40 · 合成评论研究', title)
    lines = [f"# {title}", "",
        pick("确定性分析 · 英文原始评论规则", "Deterministic analysis · rules validated for English source reviews"), "",
        f"{pick('报告', 'Report')}: `{report['report_id']}`",
        f"{pick('数据快照', 'Snapshot')}: `{report['data_snapshot_id']}`",
        f"{pick('事实哈希', 'Facts hash')}: `{report['hashes']['core_result']}`", "",
        pick("## 样本与指标", "## Sample and metrics"), "",
        pick("| 指标 | 数值 | 分子 / 分母 |", "| Metric | Value | Numerator / denominator |"), "|---|---:|---:|"]
    for metric in report['metrics']:
        name = METRICS.get(metric['metric_id'], (label(metric['metric_id']), label(metric['metric_id'])))[0 if zh else 1]
        value = metric['value']
        value = pick('未知', 'Unknown') if value is None else value
        lines.append(f"| {name} | {value} | {metric.get('numerator') if metric.get('numerator') is not None else '—'} / {metric.get('denominator') if metric.get('denominator') is not None else '—'} |")
    insight = report.get('product_insight') or {}
    lines += ['', pick('## 主题提及', '## Topic mentions'), '', pick('| 主题 | 评论数 | 占正文比例 |', '| Topic | Reviews | Share of written reviews |'), '|---|---:|---:|']
    for row in insight.get('aspect_experience', []):
        lines.append(f"| {row['label'] if zh else label(row['aspect_id'])} | {row['review_entities']} | {row['coverage']:.1%} |")
    lines += ['', pick('## 阅读边界', '## Interpretation'), '',
        pick('主题允许多重命中，比例不能相加。未提及不代表没有需求。低样本结果仅供探索；局部采集不代表站点总体。', 'Topics may overlap; shares do not sum to 100%. No mention does not imply no need. Small samples are exploratory; partial collection does not represent the entire site.'),
        pick('原文与译文分开，切换显示语言不会改变分母、数值或引用。完整规则与证据保留在规范 JSON 中。', 'Original text and translations remain separate. Changing display language never changes denominators, values or citations. The canonical JSON retains the full rule outputs and evidence.'), '',
        pick('## 证据引用', '## Evidence references'), '']
    for item in insight.get('evidence_group_catalog', []):
        lines.append(f"- `{item['evidence_group_id']}` · {item.get('full_evidence_entities', 0)} {pick('条评论', 'reviews')}")
    return '\n'.join(lines) + '\n'
