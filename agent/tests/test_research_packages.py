"""Research package, recovery and method-boundary integration without providers."""
import copy
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from agent.contracts import AgentError
from agent.harness_research import HarnessResearch
from agent.research_delivery import delivery_issues
from agent.tests.test_harness_research import HarnessFixture, DATASET_ID


class ResearchPackageTests(HarnessFixture, unittest.TestCase):
    def cap(self):
        run = self.accept()
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run['run_id'], question=run['request']['question'])
        return run, self.research.broker.issue(self.user, run, trace['trace_id'])[1]

    def package(self, *, pending=False, failed=False):
        raw = b'{"record_id":"occurrence-a","review_entity_id":"entity-a","review_text":"full original"}\n'
        binding = {'catalog_id': 'cat_fixture', 'scope': {'category_ids': ['fan']}, 'origin': 'snapshot',
            'total': 2 if pending or failed else 1, 'total_sources': 2 if pending or failed else 1,
            'unavailable_count': 0, 'batch_count': 2 if pending or failed else 1, 'batch_id': 'batch0', 'job_ids': ['job1']}
        manifest = {'dataset_id': DATASET_ID, 'manifest_sha256': 'sha256:' + 'a' * 64,
            'export_complete': True, 'source_coverage': {'complete': True}, 'catalog_binding': binding,
            'inputs': [{'job': {'id': 'job1'}}], 'shards': [{'path': 'part.jsonl', 'sha256': 'sha256:' + hashlib.sha256(raw).hexdigest()}]}
        prepared = {'catalog_id': 'cat_fixture', 'scope': binding['scope'], 'origin': 'snapshot',
            'status': 'incomplete' if pending or failed else 'ready', 'datasets': [manifest],
            'coverage': {'requested_sources': binding['total'], 'available_sources': binding['total'], 'prepared_sources': 1,
                'unavailable_count': 0, 'total_batches': binding['batch_count'], 'prepared_batches': 1, 'complete': not (pending or failed)},
            'pending_batch_ids': ['batch1'] if pending else [],
            'failed_batches': [{'batch_id': 'batch1', 'code': 'invalid_source'}] if failed else [],
            'next': {'catalog_id': 'cat_fixture'} if pending else None}
        def call(actor, operation, args):
            return copy.deepcopy(prepared if operation == 'prepare' else manifest)
        self.research.data = SimpleNamespace(call=Mock(side_effect=call), export=Mock(return_value=raw))
        return manifest, prepared

    def test_one_call_prepares_full_files_and_reuses_same_attempt(self):
        manifest, _ = self.package()
        run, cap = self.cap()
        first = self.research.tool(cap, 'open_dataset', {'scope': {'category_ids': ['fan']}})
        self.assertTrue(first['coverage']['complete'])
        self.assertEqual(first['datasets'][0]['manifest'], manifest)
        self.assertEqual(len(first['datasets'][0]['analysis_paths']), 1)
        files = {name: data for _, name, data in self.manager.inputs}
        package = json.loads(files[first['package_path'].split('/')[-1]])
        self.assertEqual(package['datasets'][0]['citation_prefix'], 'review:' + DATASET_ID + ':')
        second = self.research.tool(cap, 'open_dataset', {'catalog_id': 'cat_fixture'})
        self.assertEqual(first['package_path'], second['package_path'])
        self.assertEqual(self.research.data.export.call_count, 1)
        self.assertEqual(delivery_issues(self.state.get(self.user, run['run_id'])['checkpoint']), [])
        cached = self.state.get(self.user, run['run_id'])['checkpoint']['dataset_exports']
        self.assertTrue(all('manifest' not in item for item in cached.values()))

    def test_pending_and_failed_batches_remain_visible_in_delivery(self):
        self.package(pending=True, failed=True)
        run, cap = self.cap()
        result = self.research.tool(cap, 'open_dataset', {})
        self.assertFalse(result['coverage']['complete'])
        issue = delivery_issues(self.state.get(self.user, run['run_id'])['checkpoint'])[0]
        self.assertEqual((issue['opened_batch_count'], issue['batch_count']), (1, 2))
        self.assertEqual(issue['failed_batches'], {'batch1': 'invalid_source'})

    def test_raw_is_opt_in_and_reuses_normalized_files(self):
        manifest, _ = self.package()
        manifest['raw_artifacts'] = [{'shards': [{'path': 'raw.jsonl'}]}]
        self.research.data.call = Mock(return_value=manifest)
        _, cap = self.cap()
        first = self.research.tool(cap, 'export_dataset', {'dataset_id': DATASET_ID})
        self.assertEqual(first['raw_payload_paths'], [])
        second = self.research.tool(cap, 'export_dataset', {'dataset_id': DATASET_ID, 'include_raw': True})
        self.assertEqual(second['analysis_paths'], first['analysis_paths'])
        self.assertEqual(len(second['raw_payload_paths']), 1)
        self.assertEqual(self.research.data.export.call_count, 2)
        refreshed = self.research.tool(cap, 'export_dataset', {'dataset_id': DATASET_ID, 'force': True})
        self.assertEqual(self.research.data.export.call_count, 3)
        self.assertNotEqual(refreshed['analysis_paths'], first['analysis_paths'])
        self.assertNotEqual(refreshed['schema_path'], first['schema_path'])
        self.assertNotEqual(refreshed['manifest_path'], first['manifest_path'])
        augmented = self.research.tool(cap, 'export_dataset', {'dataset_id': DATASET_ID, 'include_raw': True})
        self.assertEqual(augmented['analysis_paths'], refreshed['analysis_paths'])
        self.assertEqual(self.research.data.export.call_count, 4)

    def test_new_attempt_cannot_reuse_old_paths(self):
        self.package()
        run, cap = self.cap()
        self.research.tool(cap, 'export_dataset', {'dataset_id': DATASET_ID})
        self.state.cancel(self.user, run['run_id'])
        resumed = self.state.resume(self.user, run['run_id'])['run']
        _, other = self.research.broker.issue(self.user, resumed, cap.trace_id)
        self.research.tool(other, 'export_dataset', {'dataset_id': DATASET_ID})
        self.assertEqual(self.research.data.export.call_count, 2)

    def test_invalid_argument_is_traced_before_dispatch(self):
        _, cap = self.cap()
        result = self.research.tool(cap, 'query_dataset', {'dataset_id': DATASET_ID, 'limit': 101}, 'bad-call')
        self.assertFalse(result['operation_success'])
        self.assertEqual(result['error']['origin'], 'caller')
        self.assertEqual(result['error']['fields'], ['limit'])
        self.assertEqual(self.data.calls, [])

    def test_source_diagnostic_reaches_model_with_recovery(self):
        _, cap = self.cap()
        error = AgentError('dataset_too_large', 'too large')
        error.upstream_diagnostic = {'reason': 'source_metadata_bytes', 'observed': 100, 'limit': 90}
        self.data.failures.append(error)
        result = self.research.tool(cap, 'list_datasets', {})
        self.assertEqual(result['error']['diagnostic'], error.upstream_diagnostic)
        self.assertEqual(result['error']['recovery_action'], 'continue_original_scope_batches')

    def test_batch_report_reads_keep_order_and_independent_errors(self):
        _, cap = self.cap()
        success = SimpleNamespace(result={'data': {'coverage': 1}, 'report': {}})
        executor = SimpleNamespace(invoke=Mock(side_effect=[success, AgentError('report_unavailable', 'unavailable')]))
        self.research.service.executor = executor
        result = self.research.tool(cap, 'get_data_quality', {'requests': [{'report_id': 'rpt_fixture1'}, {'report_id': 'rpt_fixture2'}]})
        self.assertFalse(result['complete'])
        self.assertFalse(result['operation_success'])
        self.assertEqual([item['operation_success'] for item in result['results']], [True, False])
        self.assertIn('source_id', result['results'][0]['result'])
        self.assertEqual(cap.tool_calls, 2)

    def test_candidate_scope_is_bound_and_cannot_expand(self):
        _, cap = self.cap()
        scope = {'category_ids': ['fan'], 'market_regions': ['North America']}
        candidate = {'candidate_id': 'rc_fixture', 'scope': {'category_id': 'fan', 'market_regions': ['North America'], 'job_ids': ['job1']}}
        workflow = Mock(side_effect=[{'items': [candidate]}, {'job': {'status': 'completed'}}])
        self.research.service.executor = SimpleNamespace(transport=SimpleNamespace(report_workflow=workflow))
        found = self.research.tool(cap, 'report_candidates', {'scope': scope})
        self.research.tool(cap, 'create_report', found['items'][0]['create_arguments'])
        self.assertEqual(workflow.call_args.args[1]['scope'], scope)
        wrong = self.research.tool(cap, 'create_report', {'candidate_id': 'rc_fixture', 'scope': {}})
        self.assertEqual(wrong['error']['code'], 'report_scope_changed')
        self.assertEqual(workflow.call_count, 2)

    def test_batch_budget_rejection_preserves_successful_reads(self):
        from dataclasses import replace
        _, cap = self.cap()
        self.research.broker.limits = replace(self.research.broker.limits, tool_calls=1)
        executor = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(result={'data': {'coverage': 1}, 'report': {}})))
        self.research.service.executor = executor
        result = self.research.tool(cap, 'get_data_quality', {'requests': [{'report_id': 'rpt_fixture1'}, {'report_id': 'rpt_fixture2'}]})
        self.assertFalse(result['operation_success'])
        self.assertTrue(result['results'][0]['operation_success'])
        self.assertIn('source_id', result['results'][0]['result'])
        self.assertFalse(result['results'][1]['operation_success'])
        self.assertEqual(result['results'][1]['error']['origin'], 'budget')
        self.assertEqual(executor.invoke.call_count, 1)

    def test_native_exit_signals_do_not_infer_failure_from_prose(self):
        failed = HarnessResearch._native_result_failed
        self.assertTrue(failed([{'content': [{'text': 'Traceback\n[exit code: 1]'}]}], bash=True))
        self.assertTrue(failed([{'result': {'exit_code': 2}}], bash=True))
        self.assertFalse(failed([{'text': 'A reviewer reported an error.\n[exit code: 0]'}], bash=True))
        self.assertFalse(failed([{'text': 'BrokenPipeError: downstream closed early'}], bash=True))
        self.assertTrue(failed([{'isError': True}], bash=False))

    def test_context_externalizes_only_assistant_prose_without_losing_user_constraints(self):
        document = {'user_question': 'compare the original range', 'conversation': [
            {'role': 'user', 'content': 'Keep every brand and channel.'},
            {'role': 'assistant', 'content': 'long prior answer ' * 20000, 'citations': [{'source_id': 'src_original'}]},
            {'role': 'user', 'content': 'Use the same range and method.'}]}
        prompt, original = HarnessResearch._context_prompt(document, '/inputs/original.json')
        self.assertEqual(json.loads(original), document)
        selected = json.loads(prompt)
        self.assertEqual(selected['conversation'][0], document['conversation'][0])
        self.assertEqual(selected['conversation'][2], document['conversation'][2])
        self.assertEqual(selected['conversation'][1]['json_pointer'], '/conversation/1')
        self.assertEqual(selected['conversation'][1]['citations'], document['conversation'][1]['citations'])
        self.assertLess(len(prompt), len(original) / 100)

    def test_long_review_citations_follow_pages_without_another_model_turn(self):
        run, cap = self.cap()
        digest = 'sha256:' + 'd' * 64
        self.research._source(cap, {'source_type': 'dataset', 'dataset_id': DATASET_ID,
            'provenance': {'operation': 'manifest', 'manifest_sha256': digest}, 'content': {'manifest': {
                'contract': 'voxquarry.wenquan-dataset/v1', 'manifest_sha256': digest, 'export_complete': True}}})
        rows = [{'record_id': name, 'review_text': name * 90000} for name in ('first', 'second')]
        self.research.data.call = Mock(side_effect=[
            {'manifest_sha256': digest, 'items': rows[:1], 'next_cursor': 'bounded-next'},
            {'manifest_sha256': digest, 'items': rows[1:], 'next_cursor': None}])
        answer = self.research._resolve_review_citations(self.user, run['run_id'],
            '[review:' + DATASET_ID + ':first] [review:' + DATASET_ID + ':second]')
        self.assertNotIn('[review:', answer)
        self.assertEqual(self.research.data.call.call_count, 2)
        self.assertEqual(self.research.data.call.call_args.args[2]['population'], 'occurrences')
