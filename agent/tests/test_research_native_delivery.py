"""User-visible recovery regressions; all model and collector data is synthetic."""
import unittest
import re
from unittest.mock import Mock
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

from agent.contracts import AgentError
from agent.tests.test_harness_research import HarnessFixture, DATASET_ID


class NativeDeliveryTests(HarnessFixture, unittest.TestCase):
    def test_full_real_query_cache_does_not_block_cited_markdown_publication(self):
        from reporting.tests.test_market_region import SnapshotFixture, _row
        from reporting.wenquan_datasets import WenquanDatasets, DatasetLimits
        root = Path(self.temp.name)
        fixture = SnapshotFixture(root / 'source')
        fixture.add_run('sample', 'North America', [_row('r1', 'North America', text='Original battery review')])
        data = WenquanDatasets(fixture.root, root / 'datasets', limits=replace(DatasetLimits(), max_cached_queries=2))
        manifest = data.open()
        dataset = manifest['dataset_id']
        record = data.query(dataset)['items'][0]['record_id']
        for index in range(105):
            data.query(dataset, query=f'new search {index}')
        run, cap = self.live_cap()
        self.state.add_source(self.user, run['run_id'], {
            'source_type': 'dataset', 'title': 'Verified export', 'dataset_id': dataset,
            'content': {'manifest': manifest}, 'provenance': {'operation': 'manifest', 'manifest_sha256': manifest['manifest_sha256']}},
            attempt_id=cap.attempt_id)
        def call(actor, operation, args):
            self.assertEqual(operation, 'query')
            return data.query(**args)
        self.research.data = SimpleNamespace(call=call)
        self.manager.read_output = Mock(return_value=('# Report\n\nBattery evidence.[review:' + dataset + ':' + record + ']').encode())
        result = self.research.tool(cap, 'publish_artifact', {'path': '/workspace/report.md', 'filename': 'report.md'})
        self.assertIn('artifact_id', result, result)
        self.assertEqual(cap.model_calls, 0)
        self.assert_code('citation_unknown', self.research.validate_answer, self.user, run['run_id'],
                         'Invented.[review:' + dataset + ':missing]')

    def live_cap(self):
        run = self.accept()
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run['run_id'], question=run['request']['question'])
        return run, self.research.broker.issue(self.user, run, trace['trace_id'])[1]

    def test_export_record_citations_resolve_originals_without_another_model_round(self):
        run, cap = self.live_cap()
        original_call = self.data.call
        def data(actor, operation, args):
            value = original_call(actor, operation, args)
            if operation == 'manifest':
                value.update(contract='voxquarry.wenquan-dataset/v1', export_complete=True)
            return value
        self.data.call = data
        exported = self.research.tool(cap, 'export_dataset', {'dataset_id': DATASET_ID})
        handle = '[' + exported['citation_prefix'] + 'record-a]'
        body, citations = self.research.validate_answer(self.user, run['run_id'], '原评明确给出使用条件。' + handle)
        self.assertIn('[1]', body)
        self.assertEqual(len(citations), 1)
        source = self.state.source(self.user, self.sid, citations[0]['source_id'])['source']
        self.assertEqual(source['content']['record_id'], 'record-a')
        self.assertEqual(source['provenance']['manifest_sha256'], 'sha256:' + 'b' * 64)
        self.assertEqual(self.data.calls[-1][2]['review_ids'], ['record-a'])
        self.assertEqual(cap.model_calls, 0)
        self.assert_code('citation_unknown', self.research.validate_answer, self.user, run['run_id'],
                         '伪造的记录。[review:' + DATASET_ID + ':missing]')

    def test_unexported_or_malformed_record_reference_is_not_a_citation(self):
        run, _cap = self.live_cap()
        for value in ['[review:' + DATASET_ID + ':record-a]', '[review:invented:record-a]']:
            self.assert_code('citation_unknown', self.research.validate_answer, self.user, run['run_id'], value)

    def test_markdown_publication_appends_originals_and_retries_without_duplicate_files(self):
        run, cap = self.live_cap()
        review = self.research.tool(cap, 'query_dataset', {'dataset_id': DATASET_ID})['items'][0]
        self.manager.read_output = Mock(return_value=('# 研究\n\n使用条件明确。[' + review['source_id'] + ']').encode())
        args = {'path': '/workspace/report.md', 'filename': 'report.md'}
        first = self.research.tool(cap, 'publish_artifact', args)
        second = self.research.tool(cap, 'publish_artifact', args)
        self.assertEqual(first['artifact_id'], second['artifact_id'])
        self.assertTrue(second['reused'])
        self.assertEqual(len(self.store.get_run(self.user, run['run_id'])['artifacts']), 1)
        self.assertTrue(first['portable_path'].startswith('/inputs/'))
        _, saved = self.research.artifact(self.user, self.sid, first['artifact_id'])
        self.assertIn(review['review_text'], saved.read_text())

    def test_native_web_link_resolves_only_a_saved_public_source(self):
        run, cap = self.live_cap()
        sid = self.research._source(cap, {'source_type': 'web', 'title': '公开手册',
            'url': 'https://example.com/manual', 'content': 'Synthetic manual'})
        _body, citations = self.research.validate_answer(self.user, run['run_id'], '[手册](https://example.com/manual)')
        self.assertEqual(citations[0]['source_id'], sid)
        self.assert_code('citation_missing', self.research.validate_answer, self.user, run['run_id'], '[未读](https://example.com/other)')

    def test_large_native_markdown_preserves_full_originals_in_downloadable_appendices(self):
        run, cap = self.live_cap()
        original = '原文条件A。' * 20000 + '唯一结尾验证标记'
        sid = self.research._source(cap, {'source_type': 'review', 'title': '合成原评',
            'content': {'record_id': 'long-review', 'review_text': original, 'rating': 3},
            'provenance': {'manifest_sha256': 'sha256:' + 'b' * 64}})
        self.manager.read_output = Mock(return_value=('# 研究\n\n原文条件明确。[' + sid + ']').encode())
        result = self.research.tool(cap, 'publish_artifact', {'path': '/workspace/long.md', 'filename': 'long.md'})
        appendices = result['related_files']
        self.assertGreater(len(appendices), 1)
        _, main = self.research.artifact(self.user, self.sid, result['artifact_id'])
        fragments = []
        for item in appendices:
            self.assertIn(item['download_url'], main.read_text())
            _, file = self.research.artifact(self.user, self.sid, item['artifact_id'])
            fragments.extend(re.findall(r'```text\n(.*?)\n```', file.read_text(), re.S))
        self.assertEqual(''.join(fragments), original)
        repeated = self.research.tool(cap, 'publish_artifact', {'path': '/workspace/long.md', 'filename': 'long.md'})
        self.assertEqual(result['artifact_id'], repeated['artifact_id'])
        self.assertEqual(len(self.store.get_run(self.user, run['run_id'])['artifacts']), 1 + len(appendices))

    def test_native_final_with_verified_data_reuse_needs_no_submit_or_collection_receipt(self):
        run = self.accept(question='采集并分析 https://www.lowes.com/pd/Fixture/5012345678')
        def model(payload):
            cap = self.cap(payload)
            result = self.research.tool(cap, 'query_dataset', {'dataset_id': DATASET_ID})
            sid = result['items'][0]['source_id']
            self.manager.outcome['result']['answer'] = '基于现有评论，本次未重新采集。原评描述了使用条件。[' + sid + ']'
        self.manager.on_start = model
        self.research.execute(self.user, run)
        self.assertEqual(self.state.get(self.user, run['run_id'])['status'], 'completed')
        message = self.store.list_messages(self.user, self.sid)[-1]
        self.assertIn('本次未重新采集', message['content'])
        self.assertEqual(len(message['metadata']['citations']), 1)
        self.assertEqual(self.collection.calls, [])

    def test_unfinished_report_and_collection_keep_answer_citations_and_actual_gaps(self):
        run = self.accept()
        def model(payload):
            self.read_and_answer(payload)
            self.research._checkpoint(self.cap(payload), report_job={'job': {'job_id': 'report-one', 'status': 'failed'}},
                operations={'op-one': {'result': {'status': 'failed', 'data': {'ready': False}}}})
        self.manager.on_start = model
        self.research.execute(self.user, run)
        self.assertEqual(self.state.get(self.user, run['run_id'])['status'], 'partial')
        message = self.store.list_messages(self.user, self.sid)[-1]
        self.assertIn('中档刀速', message['content'])
        self.assertEqual(len(message['metadata']['citations']), 1)
        self.assertEqual({i['kind'] for i in message['metadata']['delivery']['issues']}, {'report', 'collection'})

    def test_accepted_draft_survives_a_status_transport_failure(self):
        run = self.accept()
        self.manager.on_start = self.read_and_answer
        def fail(_):
            raise AgentError('harness_unavailable', '状态连接暂时失败', http_status=503, retryable=True)
        self.manager.on_status = fail
        self.research.execute(self.user, run)
        message = self.store.list_messages(self.user, self.sid)[-1]
        self.assertEqual(self.state.get(self.user, run['run_id'])['status'], 'partial')
        self.assertIn('中档刀速', message['content'])
        self.assertTrue(message['metadata']['error']['retryable'])

    def test_provider_400_diagnostic_disables_repeat_continue(self):
        run = self.accept()
        def failed(payload):
            cap = self.cap(payload)
            for kind, data in [('model.started', {'call': 1}), ('error.model', {'call': 1, 'code': 'model_unavailable',
                    'http_status': 400, 'provider_status': 400, 'retryable': False}),
                    ('model.finished', {'call': 1, 'complete': False})]:
                self.research.broker.event(cap, kind, data=data)
            self.manager.outcome = {'status': 'failed', 'events': [], 'result': {}}
        self.manager.on_start = failed
        self.research.execute(self.user, run)
        finished = self.state.get(self.user, run['run_id'])
        self.assertEqual(finished['status'], 'blocked')
        self.assertTrue(finished['checkpoint']['resume_disabled'])
        message = self.store.list_messages(self.user, self.sid)[-1]
        self.assertFalse(message['metadata']['can_resume'])
        self.assertEqual(message['metadata']['error']['http_status'], 400)
        self.assert_code('resume_unavailable', self.research.resume, self.user, self.sid, run['run_id'], request_id='no-loop')
