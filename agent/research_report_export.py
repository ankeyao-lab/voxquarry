"""Portable report text built from the exact sources already read by a run."""
from __future__ import annotations

import re

from .contracts import AgentError
from .research_web import public_url


def literal_label(value):
    text = " ".join(str(value or "").split())
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|])", r"\\\1", text)


def literal_block(text):
    fence = "`" * max(3, max((len(m) + 1 for m in re.findall(r"`+", text)), default=3))
    return ["", fence + "text", text, fence]


def data_summary(source, row):
    """A portable reference must identify the dataset and actual statistics.

    Do not copy entire datasets or reinterpret rule matches as sentiment counts.
    Originals and membership remain in the saved source in the user's session.
    """
    provenance = source.get("provenance") or {}
    lines = []
    for label, value in (("数据集 ID", source.get("dataset_id") or row.get("dataset_id")),
                         ("数据版本", row.get("manifest_sha256") or provenance.get("manifest_sha256")),
                         ("分析 ID", row.get("analysis_id")),
                         ("归类版本", row.get("classification_sha256"))):
        if value:
            lines += ["", label + "：" + literal_label(value)]
    sample = row.get("sample") or {}
    for label, keys in (("评论总数", ("review_entities", "total_entities")),
                        ("有正文评论", ("written_entities",)), ("有评分评论", ("rating_n",)),
                        ("平均星级", ("average_rating",))):
        value = next((sample[key] for key in keys if key in sample), None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            lines += ["", label + "：" + str(value)]
    if row.get("schema_version") == "wenquan-review-topics/v1":
        lines += ["", "按评论ID去重统计；同一条评论可以提到多项体验。归类由模型完成并复核。",
                  "", "| 体验 | 类型 | 支持评论数 | 有正文评论总数 |", "| --- | --- | ---: | ---: |"]
        for item in row.get("counts", []):
            lines.append(f"| {literal_label(item['title'])} | {'优点' if item['section'] == 'pros' else '缺点'} | {item['count']} | {item['denominator']} |")
    elif row.get("schema_version") == "wenquan-dataset-analysis/v1":
        lines += ["", "以下为分析程序初筛结果，需核对原文后才能用于优缺点评论数排名。",
                  "", "| 主题 | 相关评论 | 正向候选 | 负向候选 |", "| --- | ---: | ---: | ---: |"]
        for item in row.get("aspect_experience", []):
            values = [item.get(key) for key in ("review_entities", "explicit_positive_entities", "explicit_negative_entities")]
            if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
                lines.append("| " + literal_label(item.get("label") or item.get("aspect_id")) + " | " + " | ".join(str(value) for value in values) + " |")
    return lines


def portable_report(title, body, citations, sources):
    """Keep review originals literal and data references independently useful."""
    # The title argument owns the document title. Strip only a leading H1,
    # never an H2 section, a quote, or a heading inside a fenced code sample.
    body = re.sub(r"\A\s*(?:#(?!#)[ \t]+[^\n]*(?:\n|$)|[^\n]+\n[ \t]*=+[ \t]*(?:\n|$))", "", body, count=1)
    result = "# " + literal_label(title) + "\n\n" + body.strip()
    references = []
    for citation in citations:
        source = sources[citation["source_id"]]["source"]
        content = source.get("content")
        row = content if isinstance(content, dict) else {}
        lines = ["### 来源 " + citation["label"] + "：" + literal_label(source.get("title") or "研究资料")]
        url = source.get("url") or row.get("review_url") or row.get("product_url")
        if url:
            try:
                lines += ["", "<" + public_url(url) + ">"]
            except AgentError:
                pass
        if source.get("source_type") == "review":
            rating = row.get("rating")
            if isinstance(rating, (int, float)) and not isinstance(rating, bool) and 1 <= rating <= 5:
                lines += ["", "原始评分：" + str(rating) + " 星"]
            text = row.get("review_text")
            if isinstance(text, str) and text:
                # Fences longer than any run in source keep HTML, links and
                # embedded Markdown as literal original text in the download.
                lines += literal_block(text)
        elif source.get("source_type") in {"dataset", "analysis"}:
            lines += data_summary(source, row)
        elif source.get("source_type") in {"report", "reference"}:
            if source.get("source_type") == "reference":
                locator, provenance = source.get("locator") or {}, source.get("provenance") or {}
                for label, value in (("原件", locator.get("original_filename")),
                                     ("章节", locator.get("section_id")), ("原件 SHA-256", provenance.get("sha256"))):
                    if value:
                        lines += ["", label + "：" + literal_label(value)]
            text = content if isinstance(content, str) else row.get("text") or row.get("markdown")
            if isinstance(text, str) and text:
                lines += literal_block(text)
        references.append("\n".join(lines))
    if references:
        result += "\n\n## 引用来源\n\n" + "\n\n".join(references)
    result += "\n"
    if len(result) > 100000:
        raise AgentError("report_export_too_large", "报告与引用原文超过单份文件容量，请拆成正文与独立原文附件；不会截断内容")
    return result


def report_bundle(title, body, citations, sources, filename):
    """Keep normal exports unchanged; place large originals in bounded appendices."""
    import copy
    import hashlib
    import json
    from urllib.parse import quote
    try:
        return [{'filename': filename, 'markdown': portable_report(title, body, citations, sources), 'role': 'body'}]
    except AgentError as error:
        if error.code != 'report_export_too_large':
            raise
    main = portable_report(title, body, [], {})
    if len(main) > 85000:
        raise AgentError('report_export_too_large', '报告正文超过单份容量，请按章节拆分正文；引用原文会自动另存附录')
    sections = []
    for citation in citations:
        original = sources[citation['source_id']]['source']
        content = original.get('content')
        key = next((k for k in ('review_text', 'text', 'markdown') if isinstance(content, dict) and isinstance(content.get(k), str)), None)
        text = content if isinstance(content, str) else content[key] if key else ''
        digest = hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        for index, offset in enumerate(range(0, max(1, len(text)), 60000), 1):
            source = copy.deepcopy(original)
            if isinstance(content, str):
                source['content'] = text[offset:offset+60000]
            elif key:
                source['content'][key] = text[offset:offset+60000]
            section = portable_report('来源 ' + citation['label'] + ' 原文第 ' + str(index) + ' 部分', '', [citation],
                                      {citation['source_id']: {'source': source}})
            section += '\n来源 ID：' + literal_label(citation['source_id']) + '\n\n完整来源 SHA-256：' + digest + '\n'
            sections.append(section)
    appendices, current = [], ''
    for section in sections:
        if current and len(current) + len(section) > 90000:
            appendices.append(current); current = ''
        current += section + '\n'
    if current:
        appendices.append(current)
    if len(appendices) > 128:
        raise AgentError('report_export_too_large', '原文附录超过本次文件容量，请按来源分批导出；未截断内容')
    documents = [{'filename': filename + '-原文附录-' + str(i), 'markdown': text, 'role': 'appendix'}
                 for i, text in enumerate(appendices, 1)]
    main += '\n## 完整引用原文\n\n原文按来源编号保存在以下附录；正文编号与附录来源编号一致。\n\n'
    main += '\n'.join('- [' + literal_label(doc['filename'] + '.md') + '](' + quote(doc['filename'] + '.md') + ')' for doc in documents) + '\n'
    return [{'filename': filename, 'markdown': main, 'role': 'body'}, *documents]
