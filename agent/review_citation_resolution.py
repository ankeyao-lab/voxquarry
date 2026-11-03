"""Deterministic Markdown references to already-owned immutable review sources.

Callers supply authorized source rows with a frozen conversation/time boundary.
This module performs no I/O or mutations, and never guesses from URLs or quotes.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from urllib.parse import quote

from .contracts import AgentError, sha256_value


MAX_REVIEW_REFERENCES = 100  # Same bound as exported review handles.
_ENTITY = re.compile(r'rev_[0-9a-f]{24}\Z')
_RAW = re.compile(r'(?<![A-Za-z0-9_])rev_[A-Za-z0-9_-]*(?![A-Za-z0-9_])')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
_SOURCE = re.compile(r'src_[A-Za-z0-9_-]{1,100}\Z')


@dataclass(frozen=True)
class Marker:
    start: int
    end: int
    kind: str
    value: str


def markers(text: str) -> list[Marker]:
    """Return reference spans, respecting code, links and literal block quotes.

    Keep offsets into the original string so only reference spans are changed.
    Inline code containing solely an entity ID is a supported citation spelling;
    all other code, fenced originals, URLs and link destinations remain literal.
    """
    result, offset, fence, comment = [], 0, None, False
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not line[match.end():].strip():
                fence = None
            offset += len(line)
            continue
        if match and not comment:
            fence = match[1]
            offset += len(line)
            continue
        if line.startswith(('    ', '\t')) or re.match(r'^ {0,3}\[[^\]\n]+\]:\s', line):
            offset += len(line)
            continue
        raw_allowed = not re.match(r'^ {0,3}>', line)
        index = 0
        while index < len(line):
            if comment:
                end = line.find('-->', index)
                if end < 0:
                    break
                index, comment = end + 3, False
                continue
            if line.startswith('<!--', index):
                comment, index = True, index + 4
                continue
            if line[index] == '\\':
                index += 2
                continue
            if line[index:index + 8].lower().startswith(('https://', 'http://', 'www.', 'mailto:')):
                end = re.search(r'[\s<>`]', line[index:])
                index += end.start() if end else len(line) - index
                continue
            if line[index] == '<':
                end = line.find('>', index + 1)
                if end >= 0:
                    index = end + 1
                    continue
            if line[index] == '`':
                run = re.match(r'`+', line[index:])[0]
                end = line.find(run, index + len(run))
                if end >= 0:
                    value = line[index + len(run):end]
                    if raw_allowed and _ENTITY.fullmatch(value):
                        result.append(Marker(offset + index, offset + end + len(run), 'entity', value))
                    index = end + len(run)
                    continue
            if raw_allowed and line[index] in '*_':
                emphasis = re.match(r'\*{1,2}|_{1,2}', line[index:])[0]
                end = line.find(emphasis, index + len(emphasis))
                if end >= 0:
                    value = line[index + len(emphasis):end]
                    if _RAW.fullmatch(value):
                        result.append(Marker(offset + index + len(emphasis), offset + end, 'entity', value))
                        index = end + len(emphasis)
                        continue
            if line[index] == '[':
                end = line.find(']', index + 1)
                if end >= 0:
                    value = line[index + 1:end]
                    # Existing Markdown links/images (including their labels)
                    # and reference links have their own explicit destination.
                    if line[end + 1:end + 2] in ('(', '['):
                        opening = line[end + 1]
                        closing, cursor, balance = (')' if opening == '(' else ']'), end + 2, 1
                        while cursor < len(line) and balance:
                            if line[cursor] == '\\':
                                cursor += 2
                                continue
                            balance += (line[cursor] == opening) - (line[cursor] == closing)
                            cursor += 1
                        index = cursor
                        continue
                    kind = ('source' if value.startswith('src_') else 'review' if value.startswith('review:')
                            else 'number' if raw_allowed and re.fullmatch(r'[0-9]+', value)
                            else 'entity' if raw_allowed and _RAW.fullmatch(value) else None)
                    if kind:
                        result.append(Marker(offset + index, offset + end + 1, kind, value))
                        index = end + 1
                        continue
                elif line.startswith(('[src_', '[review:'), index):
                    value = line[index + 1:].rstrip('\r\n')
                    result.append(Marker(offset + index, offset + index + 1 + len(value),
                                         'source' if value.startswith('src_') else 'review', value))
                    break
            match = _RAW.match(line, index) if raw_allowed else None
            if match:
                value = match[0]
                # A closing underscore emphasis delimiter is not part of an ID.
                if value.endswith('_') and _ENTITY.fullmatch(value.rstrip('_')):
                    value = value.rstrip('_')
                result.append(Marker(offset + index, offset + index + len(value), 'entity', value))
                index += len(value)
            else:
                index += 1
        offset += len(line)
    return result


def replace_markers(text: str, kind: str, replacements: dict[str, str]) -> str:
    for item in reversed(markers(text)):
        if item.kind == kind and item.value in replacements:
            text = text[:item.start] + replacements[item.value] + text[item.end:]
    return text


def entity_ids(text: str) -> list[str]:
    return list(dict.fromkeys(m.value for m in markers(text) if m.kind == 'entity'))


def source_marked_content(text: str, citations: list) -> str:
    """Expand message-local numbers before presenting history to another run."""
    bindings = {}
    for index, citation in enumerate(citations if isinstance(citations, list) else []):
        if isinstance(citation, dict):
            sid = citation.get('source_id') or citation.get('citation_id')
            if isinstance(sid, str) and _SOURCE.fullmatch(sid):
                bindings[str(index + 1)] = '[' + sid + ']'
    return replace_markers(text, 'number', bindings)


def review_source_error(row: dict) -> str | None:
    source = row.get('source')
    if not isinstance(source, dict) or source.get('source_type') != 'review' or source.get('citation_eligible') is False:
        return '不是可引用原评'
    content = source.get('content')
    if not isinstance(content, dict) or not isinstance(content.get('review_text'), str) or not content['review_text'].strip():
        return '原文缺失'
    if not _SOURCE.fullmatch(str(row.get('source_id', ''))) or not row.get('session_id'):
        return '来源身份无效'
    if sha256_value(source) != row.get('source_hash'):
        return '原文校验失败'
    return None


def source_citation(row: dict, number: int) -> dict:
    source, sid = row['source'], row['source_id']
    href = f"/wenquan/api/v1/sessions/{quote(row['session_id'], safe='')}/sources/{quote(sid, safe='')}/content"
    return {'citation_id': sid, 'source_id': sid, 'label': str(number),
            'kind': source.get('source_type'), 'source_type': source.get('source_type'), 'title': source.get('title'),
            'source_sha256': row['source_hash'], 'report_id': source.get('report_id'),
            'source_href': href, 'source_url': href}


def resolve_entities(text: str, sources: list[dict], *, citations: list | None = None,
                     strict: bool = True, unavailable: bool = False, run_id: str | None = None) -> tuple[str, list]:
    """Resolve raw entities to source markers, or append numbered legacy refs.

    In strict mode every entity must resolve. Legacy mode preserves existing
    numbering and replaces only unresolved positions with an explicit notice.
    """
    ids = entity_ids(text)
    result = copy.deepcopy(citations) if citations is not None else []
    if not ids:
        return text, result
    excessive = len(ids) > MAX_REVIEW_REFERENCES
    if excessive and strict:
        raise AgentError('citation_limit', '一份正文最多解析100条独立原评；完整清单可保存为分析文件。')
    by_entity = {value: {} for value in ids}
    for row in sources:
        source = row.get('source') or {}
        if not isinstance(source, dict):
            continue
        content = source.get('content') or {}
        if source.get('source_type') == 'review' and isinstance(content, dict):
            value = content.get('review_entity_id')
            if isinstance(value, str) and value in by_entity:
                by_entity[value][row['source_id']] = row
    replacements, problems = {}, []
    explicit = {m.value for m in markers(text) if m.kind == 'source'}
    explicit.update((c.get('source_id') or c.get('citation_id')) for c in result if isinstance(c, dict))
    for value in ids:
        candidates = list(by_entity[value].values())
        current = [row for row in candidates if run_id and row.get('run_id') == run_id]
        if current:
            candidates = current
        # read_saved_source can register identical immutable contents in several
        # runs. Validate every copy before coalescing; a corrupt copy is not hidden.
        invalid = next((error for row in candidates if (error := review_source_error(row))), None)
        reason = ('来源读取未完成' if unavailable else '引用数量超限' if excessive else
                  '编号格式无效' if not _ENTITY.fullmatch(value) else
                  '来源缺失' if not candidates else invalid or
                  ('存在多个来源版本' if len({row['source_hash'] for row in candidates}) != 1 else None))
        if not reason:
            row = min(candidates, key=lambda row: (row['source_id'] not in explicit, row['created_at'], row['source_id']))
        if not reason and citations is not None:
            existing = [(i, c) for i, c in enumerate(result) if isinstance(c, dict)
                        and (c.get('source_id') or c.get('citation_id')) == row['source_id']]
            if existing and any(c.get('source_sha256') != row['source_hash'] for _, c in existing):
                reason = '引用版本不一致'
        if reason:
            problems.append(value + '：' + reason)
            replacements[value] = '原评引用暂不可用（' + reason + '）'
            continue
        if citations is None:
            replacements[value] = '[' + row['source_id'] + ']'
        else:
            if existing:
                number = existing[0][0] + 1
            else:
                number = len(result) + 1
                result.append(source_citation(row, number))
            replacements[value] = '[' + str(number) + ']'
    if problems and strict:
        raise AgentError('citation_unknown', '原评引用尚未核验：' + '；'.join(problems[:8])
                         + '。保留已完成的分析，用原评查询返回的完整source_id修正对应引用，不要猜测其他版本。')
    return replace_markers(text, 'entity', replacements), result
