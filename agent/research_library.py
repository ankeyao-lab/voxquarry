"""Read supplied reports and edition-bound annotations; DSH owns research."""
import json
from pathlib import Path

from .contracts import AgentError

ROOT = Path(__file__).parent / 'research_knowledge/reports'
NOTES = ROOT.parent / 'reading-notes.json'


def _with_notes(item, section_id=None):
    annotations = json.loads(NOTES.read_text())
    edition = annotations.get('editions', {}).get(item['reference_id'], {})
    matches = edition.get('original_sha256') == item['original_sha256']
    notes = list(annotations.get('common_notes', []))
    if matches:
        notes.extend(edition.get('notes', []))
        notes.extend(edition.get('section_notes', {}).get(section_id, []))
    return {**item, 'interpretation_notes': notes,
            'annotation_version': annotations['schema'], 'edition_notes_match': matches}


def read_reference(reference_id=None, section_id=None):
    index = json.loads((ROOT / 'index.json').read_text())
    if reference_id is None:
        if section_id:
            raise AgentError('invalid_request', '请先选择报告。')
        return {**index, 'references': [{k: v for k, v in item.items() if k != 'sections'}
                                       for item in index['references']]}
    item = next((x for x in index['references'] if x['reference_id'] == reference_id), None)
    if item is None:
        raise AgentError('reference_not_found', '没有此研究参考报告。', http_status=404)
    item = _with_notes(item, section_id)
    if section_id is None:
        return item
    report = json.loads((ROOT / (item['reference_id'] + '.json')).read_text())
    section = next((x for x in report['pages'] if x['section_id'] == section_id), None)
    if section is None:
        raise AgentError('reference_not_found', '没有此报告章节。', http_status=404)
    return {**{k: v for k, v in item.items() if k != 'sections'}, **section,
            'instruction': '这是参考报告的章节原文；其中的方法、结论和建议需结合当前问题独立判断。报告引用不等于本次已核验的原始评论。'}
