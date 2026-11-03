"""Observable completion evidence for explicitly requested full raw-data work."""
import re
import json


def _catalog(evidence, result):
    catalog_id = result.get('catalog_id')
    if not catalog_id:
        return
    catalogs = dict(evidence.get('catalogs', {}))
    previous = catalogs.get(catalog_id, {})
    current = {**previous, **{k: result[k] for k in ('scope', 'origin', 'total', 'total_sources', 'unavailable_count', 'batch_count')}}
    current['job_ids'] = sorted(set(previous.get('job_ids', [])) | {r['job_id'] for r in result.get('items', [])})
    current['batches'] = {**previous.get('batches', {}), **{b['batch_id']: b['job_ids'] for b in result.get('batches', [])}}
    current['unavailable_sources'] = {**previous.get('unavailable_sources', {}),
        **{r['job_id']: r for r in result.get('unavailable_sources', [])}}
    page = result.get('page', {})
    ranges = sorted([*previous.get('page_ranges', []), [page.get('offset', 0), page.get('next_offset', 0)]])
    merged = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    current['page_ranges'] = merged
    current['enumeration_complete'] = merged == [[0, current['total_sources']]]
    catalogs[catalog_id] = current
    evidence['catalogs'] = catalogs


def _plan(evidence, binding):
    plans = dict(evidence.get('plans', {}))
    catalog_id = binding['catalog_id']
    plan = {**plans.get(catalog_id, {}), **{k: binding[k] for k in
        ('catalog_id', 'scope', 'origin', 'total', 'total_sources', 'unavailable_count', 'batch_count')}}
    # A refreshed version of the same exact scope replaces the earlier plan.
    # Opening a smaller scope cannot discharge the original request.
    key = lambda p: json.dumps([p.get('scope', {}), p.get('origin', 'auto')], sort_keys=True)
    for old_id, old in plans.items():
        if old_id != catalog_id and key(old) == key(plan):
            plans[old_id] = {**old, 'superseded_by': catalog_id}
    plan.pop('superseded_by', None)
    plans[catalog_id] = plan
    evidence['plans'] = plans
    return plan


def coverage(evidence):
    results = []
    for catalog_id, plan in evidence.get('plans', {}).items():
        if plan.get('superseded_by'):
            continue
        opened, job_ids = {}, set()
        for batch, item in plan.get('opened_batches', {}).items():
            if item.get('complete') is True:
                opened[batch] = item['dataset_id']
                job_ids.update(item.get('job_ids', []))
        results.append({'catalog_id': catalog_id, 'scope': plan['scope'],
            'batch_count': plan['batch_count'], 'opened_batch_count': len(opened),
            'total_sources': plan['total_sources'], 'opened_source_count': len(job_ids),
            'unavailable_count': plan['unavailable_count'], 'failed_batches': plan.get('failed_batches', {}),
            'complete': (len(opened) == plan['batch_count'] and len(job_ids) == plan['total']
                         and plan['unavailable_count'] == 0 and plan['total'] > 0)})
    return results


def failure(checkpoint, args, error):
    evidence = dict(checkpoint.get('raw_data_evidence', {}))
    catalog_id, batch_id = args.get('catalog_id'), args.get('batch_id')
    catalog = evidence.get('catalogs', {}).get(catalog_id) or evidence.get('plans', {}).get(catalog_id)
    if catalog and batch_id:
        plan = _plan(evidence, {**catalog, 'catalog_id': catalog_id})
        plan['failed_batches'] = {**plan.get('failed_batches', {}), batch_id: error['code']}
    evidence['coverage'] = coverage(evidence)
    return evidence


def required(question):
    return bool(re.search(r'(全量|全部|所有|完整).{0,45}(原始|原评|评论|数据)', question, re.S)
                and re.search(r'分析|研究|计算|统计', question)
                and not re.search(r'(不需要|不用|无需).{0,12}(全量|全部|所有|完整)', question))


def record(checkpoint, name, args, result, source_id=None):
    evidence = dict(checkpoint.get('raw_data_evidence', {}))
    if name == 'prepare':
        counts = result['coverage']
        plan = _plan(evidence, {'catalog_id': result['catalog_id'], 'scope': result['scope'], 'origin': result['origin'],
            'total': counts['available_sources'], 'total_sources': counts['requested_sources'],
            'unavailable_count': counts['unavailable_count'], 'batch_count': counts['total_batches']})
        plan['failed_batches'] = {item['batch_id']: item['code'] for item in result.get('failed_batches', [])}
        evidence['coverage'] = coverage(evidence)
        return evidence
    if result.get('catalog_id'):
        _catalog(evidence, result)
    if name == 'list_datasets':
        if result.get('catalog_id'):
            return evidence
        evidence['catalog'] = {'scope': args.get('scope', {}),
            'job_ids': sorted({row['job_id'] for row in result.get('items', []) if row.get('job_id')}),
            'complete': result.get('catalog_complete') is True,
            'unavailable_sources': result.get('unavailable_sources', [])}
    elif name == 'open_dataset':
        if result.get('status') == 'requires_batches':
            _plan(evidence, result)
            evidence['coverage'] = coverage(evidence)
            return evidence
        binding = result.get('catalog_binding', {})
        opened = dict(evidence.get('opened', {}))
        opened[result['dataset_id']] = {'scope': binding.get('scope', args.get('scope', {})),
            'catalog_id': binding.get('catalog_id'),
            'job_ids': sorted({row['job']['id'] for row in result.get('inputs', []) if row.get('job', {}).get('id')}),
            'manifest_sha256': result.get('manifest_sha256'), 'source_id': source_id,
            'complete': result.get('source_coverage', {}).get('complete', result.get('export_complete'))}
        evidence['opened'] = opened
        if binding:
            plan = _plan(evidence, binding)
            plan['opened_batches'] = {**plan.get('opened_batches', {}), binding['batch_id']: {
                **opened[result['dataset_id']], 'dataset_id': result['dataset_id'],
                'complete': result.get('export_complete', opened[result['dataset_id']]['complete'])}}
            plan['failed_batches'] = {k: v for k, v in plan.get('failed_batches', {}).items() if k != binding['batch_id']}
    elif name in {'aggregate_dataset', 'analyze_product_reviews'}:
        analyses = dict(evidence.get('analyses', {}))
        if args.get('dataset_ids'):
            for item in result.get('sources', []):
                analyses[item['dataset_id']] = {'source_id': source_id, 'manifest_sha256': item['manifest_sha256']}
        else:
            analyses[args['dataset_id']] = {'source_id': source_id, 'manifest_sha256': result.get('manifest_sha256')}
        evidence['analyses'] = analyses
    elif name in {'run_python', 'run_shell'} and result.get('exit_code') == 0:
        # Only an execution that actually names an exported dataset can supply
        # provenance; a placeholder execution must not unlock completion.
        code = args.get('code', '') + args.get('command', '')
        for dataset in checkpoint.get('exported_datasets', []):
            if dataset in code:
                analyses = dict(evidence.get('analyses', {}))
                analyses[dataset] = {'source_id': source_id,
                    'manifest_sha256': evidence.get('opened', {}).get(dataset, {}).get('manifest_sha256')}
                evidence['analyses'] = analyses
    evidence['coverage'] = coverage(evidence)
    return evidence


def outcome(question, checkpoint):
    if not required(question):
        return {'required': False, 'complete': True}
    evidence = checkpoint.get('raw_data_evidence', {})
    catalog = evidence.get('catalog', {})
    catalog_ids = set(catalog.get('job_ids', []))
    complete = False
    for dataset, opened in evidence.get('opened', {}).items():
        analysis = evidence.get('analyses', {}).get(dataset, {})
        if (catalog.get('complete') and opened.get('complete') and catalog_ids
            and catalog_ids == set(opened.get('job_ids', []))
            and catalog.get('scope') == opened.get('scope')
            and opened.get('manifest_sha256') and analysis.get('source_id')
            and analysis.get('manifest_sha256') == opened['manifest_sha256']):
            complete = True
    return {'required': True, 'complete': complete, 'catalog_job_count': len(catalog_ids),
        'reason': None if complete else 'full_raw_data_goal_incomplete',
        'unavailable_sources': catalog.get('unavailable_sources', [])}
