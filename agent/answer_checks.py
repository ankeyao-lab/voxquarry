"""Small delivery checks derived from the user's question, not answer length."""
from __future__ import annotations

import re
from decimal import Decimal


def percentage_relation_conflicts(answer: str) -> list[str]:
    """Check explicit percentage inequalities, not qualitative recommendations.

    Labels may separate the values from their operators. Quoted source text,
    code and explicitly identified priority orders are not numerical claims by
    the answer. This deliberately does not infer units, denominators or ranks.
    """
    number = r"(?<![\d.,–—~至])(?<!\d-)-?\d+(?:\.\d+)?"
    percent = re.compile(number + r"[%％]")
    relation = re.compile(
        rf"(?P<left>{number})[%％][^%％<>＜＞≤≥=＝\n；;。|]{{0,100}}?"
        r"(?P<operator>>=|<=|[<>＜＞≤≥=＝])"
        rf"[^%％<>＜＞≤≥=＝\n；;。|]{{0,100}}?(?P<right>{number})[%％]"
    )
    operators = {"＜": "<", "＞": ">", "≤": "<=", "≥": ">=", "＝": "="}
    conflicts, fence = [], None
    for original in answer.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", original)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token[0]
            elif token[0] == fence:
                fence = None
            continue
        if fence or re.match(r"^\s*>", original) or re.search(r"优先级|优先顺序|priority order", original, re.I):
            continue
        line = re.sub(r"`[^`]*`|“[^”]*”|‘[^’]*’|\"[^\"]*\"", "", original)
        # Start at every value so that a valid first pair cannot hide an
        # invalid second pair in A > B > C.
        for candidate in percent.finditer(line):
            match = relation.match(line, candidate.start())
            if not match:
                continue
            # An explicitly refuted relation is not asserted as true. Keep
            # the check local to this comparison, so an unrelated correction
            # elsewhere on the line cannot hide a second wrong comparison.
            suffix = line[match.end():]
            if re.match(r"\s*[）)]?\s*(?:不成立|是错的|是错误的|错误|写反了)", suffix):
                continue
            left, right = Decimal(match['left']), Decimal(match['right'])
            op = operators.get(match['operator'], match['operator'])
            valid = {">": left > right, "<": left < right, ">=": left >= right,
                     "<=": left <= right, "=": left == right}[op]
            if not valid:
                conflicts.append(f"{match['left']}% {op} {match['right']}%")
    return list(dict.fromkeys(conflicts))


def simple_count_question(question: str) -> bool:
    value = question.strip().casefold()
    return (
        len(value) <= 80
        and bool(re.search(r"评论|评价|reviews?", value))
        and bool(re.search(r"多少|几条|数量|总数|how many|review count|number of reviews", value))
        and not re.search(r"负面|正面|优缺点|为什么|原因|哪些|分别|比较|对比|交叉|建议|排名|top|pros|cons|negative|positive", value)
    )


def dual_ranking_size(question: str) -> int | None:
    value = question.casefold()
    dual = bool(re.search(r"优缺点|利弊|正负面|正负向", value)) or (
        bool(re.search(r"(?<![a-z])pros(?![a-z])|优点|优势|正面", value))
        and bool(re.search(r"(?<![a-z])cons(?![a-z])|缺点|劣势|负面", value))
    )
    found = re.search(r"(?:前|top)\s*(\d{1,2}|十)(?!\d|\s*(?:年|个月|月|周|天|日|小时|分钟|秒|米|英寸))", value)
    if not dual or found is None:
        return None
    count = 10 if found.group(1) == "十" else int(found.group(1))
    return count if count > 0 else None


def ranked_item_counts(answer: str) -> tuple[int, int]:
    """Count explicit rows in independently titled positive/negative sections."""
    counts = [0, 0]
    current: int | None = None
    for line in answer.splitlines():
        heading = re.sub(r"^[\s#*]+", "", line).strip()
        atx_heading = bool(re.match(r"^\s*#{1,6}\s+", line))
        bold_heading = bool(re.match(r"^\s*\*\*[^*]+\*\*\s*$", line))
        chinese_numbered_heading = bool(re.match(r"^[一二三四五六七八九十]+[、.．]\s*", heading))
        if atx_heading or bold_heading or chinese_numbered_heading:
            heading = re.sub(r"^(?:[一二三四五六七八九十]+[、.．]|\d+[、.．)]\s+)\s*", "", heading)
        bracket_heading = re.fullmatch(
            r"【([^【】]+)】(?:\s*(?:（[^（）]*）|\([^()]*\)))?", heading.rstrip("*").strip()
        )
        if bracket_heading:
            heading = bracket_heading.group(1).strip()
        heading = re.sub(r"^top\s*\d+\s*", "", heading, flags=re.I)
        positive = re.match(r"(?:pros\b|优点|优势|正面评价|正面反馈)(?!\s*\|)", heading, re.I)
        negative = re.match(r"(?:cons\b|缺点|劣势|负面评价|负面反馈)(?!\s*\|)", heading, re.I)
        if positive or negative:
            current = 0 if positive else 1
            continue
        # Other explicit headings end a ranking; explanatory numbered items
        # outside the two sections cannot fill missing ranking positions.
        if (bracket_heading or atx_heading or chinese_numbered_heading
                or (bold_heading and not re.match(r"^\s*\*\*\d+[.、)]\s*", line))):
            current = None
        if current is not None and re.match(
            r"^\s*(?:\|?\s*(?:\*\*)?\d+(?:\*\*)?(?:[（(][^）)]*[）)])?\s*\||(?:\*\*)?\d+[.、)]\s*|[-+*]\s+)", line
        ):
            counts[current] += 1
    return counts[0], counts[1]


def delivery_feedback(question: str, answer: str, *, review_records: int) -> list[str]:
    feedback: list[str] = []
    size = dual_ranking_size(question)
    if size:
        positive, negative = ranked_item_counts(answer)
        if positive < size or negative < size:
            feedback.append(
                f"用户要求优点和缺点分别前{size}。当前只找到{positive}项优点、{negative}项缺点。"
                f"请实际给出两个独立标题‘优点’和‘缺点’，每段列{size}条编号项目或有排名列的表格，并说明排序依据；不要只写开场白。"
                "资料确实不足时如实说明并使用insufficient_evidence，不能凑数。"
            )
        if review_records == 0:
            feedback.append("还没有读取本轮完整评论。请先用原文核对相关优缺点的含义，再给榜单；历史助手摘要不能代替原文。")
    return feedback


def style_feedback(question: str, answer: str) -> list[str]:
    """Editorial suggestions, never evidence that a substantive answer is absent."""
    feedback: list[str] = []
    internal = [word for word in ("评论实体", "发生记录", "显式正", "显式负", "口径", "净星级极性", "低样本", "has_more", "scan_complete", "source_id") if word in answer and word not in question]
    if re.search(r"脚本|执行记录|执行条数|星级关联正向属性", answer) and not re.search(r"方法|脚本|覆盖|执行|技术|架构", question):
        feedback.append("删除脚本执行量、分析覆盖和星级关联等过程说明；用户问的是产品结论。保留具体体验问题、实际场景和依据，不用统计背景填充产品发现，也不新增未经原文支持的事实。")
    if internal or re.search(r"(?<![\w\[])S\d+(?![\w\]])", answer):
        feedback.append("请具体改掉这些表达：" + "、".join(internal or ["正文中的来源代号"]) + "。"
                        "‘评论实体’写成‘评论’，‘显式正/显式负’写成‘报告识别的正面/负面提及’，‘口径’按含义写成‘统计方法’或‘这批评论’；删除工具字段和裸来源代号，保留合法编号引用。")
    if simple_count_question(question) and (len(answer) > 120 or "约" in answer):
        feedback.append("这是简单查数问题。只用一两句给精确评论总数和有文字的条数，不重复已选报告对象或附加技术信息；已知精确整数不要写‘约’。")
    elif len(answer) > 700 and not dual_ranking_size(question) and not re.search(r"详细|深入|逐条|展开|详尽|detail|in.depth", question, re.I):
        feedback.append("请选择2至3个最能回答本题的发现，每项选一个具体例子，删去次要发现、重复背景、星级直方图和长篇英文复述。保留所选例子的对象、条件、时间和用户猜测；不能通过合并不同案例来缩短，不新增频率、原因或数字。必要条件比凑足条目或达到固定字数更重要。")
    return feedback
