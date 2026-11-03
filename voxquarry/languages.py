"""Explicit source-language boundary; translations never become analytical facts."""
import json
import re
from reporting.source import load_jobs


def require_verified_language(facts, snapshot_root):
    ids = {str(fact.get('job_id')) for fact in facts}
    for job in load_jobs(snapshot_root):
        if str(job['id']) not in ids:
            continue
        result = json.loads(job.get('result_json') or '{}')
        for value in result.values():
            if isinstance(value, dict):
                language = (value.get('product') or {}).get('language') or (value.get('notes') or {}).get('language')
                if language and language != 'en':
                    raise ValueError('SOURCE_LANGUAGE_UNVERIFIED: deterministic reports currently require English source reviews; original data is retained')
    if any(re.search(r'[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]', str(f.get('review_text') or '')) for f in facts):
        raise ValueError('SOURCE_LANGUAGE_UNVERIFIED: this scope contains source text outside the verified English rule coverage')
