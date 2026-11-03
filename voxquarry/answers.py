"""English presentation of validated report facts; source hashes remain unchanged."""
import json
import re
from .presentation import METRICS, label


def answer_locale(question):
    # Explicit response instructions win; otherwise infer from the question.
    if re.search(r'(用中文|中文回答|respond in Chinese|answer in Chinese)',question,re.I):return 'zh-CN'
    if re.search(r'(用英文|英文回答|respond in English|answer in English)',question,re.I):return 'en'
    return 'zh-CN' if re.search(r'[\u3400-\u9fff]',question) else 'en'


def english_answer(claims, report, coverage):
    data=report['data'];blocks=[]
    for claim in claims:
        pointer=claim.get('source_pointer') or {};kind=claim['claim_type']
        if kind=='metric':
            m=next(v for v in data['metrics'] if v['metric_id']==pointer['metric_id'])
            title=METRICS.get(m['metric_id'],(None,label(m['metric_id'])))[1]
            rows=[f"**{title}: {m['value']}**"]
            for key in ('numerator','denominator','sample_size','interval_95'):
                if m.get(key) is not None:rows.append(f"{label(key)}: {json.dumps(m[key],ensure_ascii=False)}")
            blocks.append('\n\n'.join(rows))
        elif kind=='scope':
            rows=[f"- {label(k)}: {json.dumps(v,ensure_ascii=False)}" for k,v in (data.get('scope') or {}).items() if v not in (None,[],{})]
            blocks.append('**Report scope**\n\n'+'\n'.join(rows))
        elif kind=='saved_table_row':
            table=next(t for t in data['tables'] if t['table_id']==pointer['table_id']);row=table['rows'][pointer['row_index']]
            rows=[f"- {label(k)}: {json.dumps(v,ensure_ascii=False)}" for k,v in row.items() if not isinstance(v,(dict,list))]
            blocks.append(f"**{label(table['table_id'])}, saved row {pointer['row_index']+1}**\n\n"+'\n'.join(rows)+'\n\nTextual category values are retained from the source report.')
        elif kind=='evidence':
            quote=claim['text'].split('\n',1)[-1]
            blocks.append('**Original review excerpt** (illustrative; it does not establish prevalence)\n\n'+ '\n'.join('> '+line for line in quote.splitlines()))
        elif kind=='saved_finding':
            blocks.append('**Saved finding — original report wording**\n\n'+'\n'.join('> '+line for line in claim['text'].splitlines()))
        elif kind=='data_quality':
            blocks.append('**Data quality and scope**\n\nSmall samples are exploratory. Partial sources do not represent the entire site. Topic mentions may overlap. Unmentioned topics do not imply absent needs.')
    if report.get('limitations'):
        codes=[x.get('code','source_note') if isinstance(x,dict) else 'source_note' for x in report['limitations']]
        blocks.append('**Source limitations**: '+', '.join(codes)+'. Inspect Data & method and the canonical report before generalizing.')
    if not coverage['complete']:
        blocks.append(f"Only the first {coverage['max_rows_per_table']} rows of up to {coverage['max_tables']} tables were read. This answer does not establish that omitted content is absent.")
    blocks.append(f"Source: {report['report']['title']} · {report['report']['generated_at']}. This answer uses the saved report and its validated citations.")
    return '\n\n'.join(blocks)


def english_error(code):
    messages={'llm_unavailable':'No model is configured or the model is unavailable. Your saved reports are still accessible.', 'insufficient_evidence':'The retrieved evidence does not support an answer to this question.', 'report_selection_required':'Select a saved report to continue.', 'scope_mismatch':'The question and report scope do not match.', 'out_of_scope':'This question is outside the available report scope.', 'citation_invalid':'Source citations could not be verified. No answer was published.', 'report_not_found':'The selected report was not found.', 'source_changed':'The source changed during validation. Please try again.'}
    return messages.get(code,'The research request could not be completed. Your existing data is retained.')+' ['+code+']'
