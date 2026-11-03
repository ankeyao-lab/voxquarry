import unittest
from agent.research_data_goal import outcome, record, failure
from agent.research_delivery import delivery_issues, delivery_notice


def catalog_page(offset=0, catalog_id='cat_test', scope=None):
    return {'catalog_id': catalog_id, 'scope': scope or {}, 'origin': 'auto',
        'total': 2, 'total_sources': 2, 'batch_count': 2, 'unavailable_count': 0,
        'items': [{'job_id': 'job' + str(offset)}],
        'batches': [{'batch_id': 'batch' + str(offset), 'job_ids': ['job' + str(offset)]}],
        'unavailable_sources': [], 'page': {'offset': offset, 'next_offset': offset + 1}}


class BatchedCoverageTests(unittest.TestCase):
    def setUp(self):
        self.checkpoint = {}

    def add(self, name, args, result):
        self.checkpoint['raw_data_evidence'] = record(self.checkpoint, name, args, result, 'src_test')
        return self.checkpoint['raw_data_evidence']

    def opened(self, n, page=None):
        page = page or catalog_page(n)
        return {'dataset_id': 'ds_' + str(n), 'manifest_sha256': 'version' + str(n),
            'inputs': [{'job': {'id': 'job' + str(n)}}], 'source_coverage': {'complete': True},
            'catalog_binding': {**{k: page[k] for k in ('catalog_id', 'scope', 'origin', 'total', 'total_sources', 'batch_count', 'unavailable_count')},
                                'batch_id': 'batch' + str(n)}}

    def test_last_page_alone_cannot_prove_enumeration_and_listing_does_not_force_reading(self):
        e = self.add('list_datasets', {'cursor': 'last'}, catalog_page(1))
        self.assertFalse(e['catalogs']['cat_test']['enumeration_complete'])
        self.assertEqual(delivery_issues(self.checkpoint), [])
        e = self.add('list_datasets', {}, catalog_page())
        self.assertTrue(e['catalogs']['cat_test']['enumeration_complete'])
        self.assertEqual(e['catalogs']['cat_test']['job_ids'], ['job0', 'job1'])

    def test_plan_partial_failure_and_all_batches_success_are_separate(self):
        self.add('open_dataset', {}, {**catalog_page(), 'status': 'requires_batches'})
        self.add('open_dataset', {}, self.opened(0))
        issues = delivery_issues(self.checkpoint)
        self.assertEqual(issues[0]['opened_batch_count'], 1)
        self.assertIn('1/2 个批次', delivery_notice(issues))
        self.checkpoint['raw_data_evidence'] = failure(self.checkpoint,
            {'catalog_id': 'cat_test', 'batch_id': 'batch1'}, {'code': 'invalid_source'})
        self.assertFalse(self.checkpoint['raw_data_evidence']['coverage'][0]['complete'])
        self.add('open_dataset', {}, self.opened(1))
        self.assertEqual(delivery_issues(self.checkpoint), [])

    def test_smaller_scope_cannot_discharge_original_plan_but_refresh_can(self):
        self.add('open_dataset', {}, {**catalog_page(), 'status': 'requires_batches'})
        small = {**catalog_page(scope={'job_ids': ['job0']}, catalog_id='cat_small'), 'total': 1, 'total_sources': 1, 'batch_count': 1}
        self.add('open_dataset', {}, self.opened(0, small))
        self.assertEqual(delivery_issues(self.checkpoint)[0]['catalog_id'], 'cat_test')
        for n in (0, 1):
            self.add('open_dataset', {}, self.opened(n, catalog_page(n, catalog_id='cat_refresh')))
        self.assertEqual(delivery_issues(self.checkpoint), [])

    def test_missing_sources_never_become_complete_after_available_batches(self):
        p = {**catalog_page(), 'unavailable_count': 1, 'total_sources': 3}
        for n in (0, 1):
            self.add('open_dataset', {}, self.opened(n, p))
        issue = delivery_issues(self.checkpoint)[0]
        self.assertEqual((issue['opened_batch_count'], issue['batch_count']), (2, 2))
        self.assertFalse(issue['complete'])

    def test_union_analysis_retains_each_dataset_version(self):
        result = {'sources': [{'dataset_id': 'a', 'manifest_sha256': 'v1'}, {'dataset_id': 'b', 'manifest_sha256': 'v2'}]}
        e = self.add('aggregate_dataset', {'dataset_ids': ['a', 'b']}, result)
        self.assertEqual(e['analyses']['b']['manifest_sha256'], 'v2')

    def test_one_batch_receipt_counts_opened_sources_despite_unrelated_catalog_gap(self):
        p = {**catalog_page(), 'total': 1, 'total_sources': 2, 'batch_count': 1, 'unavailable_count': 1}
        result = {**self.opened(0, p), 'export_complete': True, 'source_coverage': {'complete': False}}
        evidence = self.add('open_dataset', {}, result)
        self.assertEqual(evidence['coverage'][0]['opened_source_count'], 1)
        self.assertEqual(evidence['coverage'][0]['opened_batch_count'], 1)
        self.assertFalse(evidence['coverage'][0]['complete'])


class FullRawGoalTests(unittest.TestCase):
    question = '请基于EGO全部原始评论数据重新分析用户群'
    def test_reports_alone_and_failed_reads_cannot_complete_full_raw_goal(self):
        self.assertFalse(outcome(self.question, {'read_job_ids':['old'], 'published_report':{}})['complete'])
        self.assertTrue(outcome('请整理已有的 EGO 用户研究报告', {})['complete'])
        self.assertTrue(outcome('不用全量原评分析，请先总结旧报告', {})['complete'])

    def test_complete_scope_version_and_analysis_receipt_are_all_required(self):
        checkpoint = {}
        def add(name,args,result,sid=None):
            checkpoint['raw_data_evidence'] = record(checkpoint,name,args,result,sid)
        scope={'brands':['EGO']}
        add('list_datasets', {'scope':scope}, {'items':[{'job_id':'a'},{'job_id':'b'}], 'catalog_complete':True})
        opened={'dataset_id':'ds_1','manifest_sha256':'v1','inputs':[{'job':{'id':'a'}},{'job':{'id':'b'}}], 'source_coverage':{'complete':True}}
        add('open_dataset',{'scope':scope},opened,'src_open')
        self.assertFalse(outcome(self.question,checkpoint)['complete'])
        add('aggregate_dataset',{'dataset_id':'ds_1'},{'manifest_sha256':'v2'},'src_wrong')
        self.assertFalse(outcome(self.question,checkpoint)['complete'])
        add('aggregate_dataset',{'dataset_id':'ds_1'},{'manifest_sha256':'v1'},'src_analysis')
        self.assertTrue(outcome(self.question,checkpoint)['complete'])
        opened['source_coverage']['complete']=False
        add('open_dataset',{'scope':scope},opened,'src_open')
        self.assertFalse(outcome(self.question,checkpoint)['complete'])

    def test_placeholder_code_cannot_count_as_dataset_analysis(self):
        checkpoint={'exported_datasets':['ds_1']}
        evidence=record(checkpoint,'run_python',{'code':'pass'},{'exit_code':0},'src_noop')
        self.assertNotIn('analyses', evidence)
