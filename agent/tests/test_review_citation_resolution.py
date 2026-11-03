"""Reference recovery must use owned saved originals, never invented links."""
from __future__ import annotations

import json
import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from agent.review_citation_resolution import entity_ids, source_citation
from agent.tests.test_agent_trace import actor
from agent.tests.test_harness_research import HarnessFixture


ENTITY = 'rev_' + 'a' * 24
MISSING = 'rev_' + 'f' * 24


class CitationDeliveryTests(HarnessFixture, unittest.TestCase):
    def review(self, run, entity=ENTITY, text='A synthetic original review for citation tests.'):
        return self.state.add_source(self.user, run['run_id'],
            {'source_type': 'review', 'report_id': 'rpt_synthetic', 'title': 'Synthetic original',
             'content': {'review_entity_id': entity, 'record_id': 'occ_synthetic', 'review_text': text}},
            attempt_id=run['attempt_id'])

    def saved_sources(self):
        run = self.accept()
        report = self.state.add_source(self.user, run['run_id'],
            {'source_type': 'report', 'report_id': 'rpt_synthetic', 'title': 'Synthetic summary', 'content': {}},
            attempt_id=run['attempt_id'])
        review = self.review(run)
        return run, report, review

    def finish_legacy(self, run, content, citations=()):
        self.state.finish(self.user, run['run_id'], content,
            metadata={'citations': list(citations)}, attempt_id=run['attempt_id'])
        return self.store.list_messages(self.user, self.sid)[-1]

    def project(self, messages=None):
        return self.state.project_message_citations(self.user, self.sid,
            self.store.list_messages(self.user, self.sid) if messages is None else messages)

    def live_cap(self, run):
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run['run_id'], question=run['request']['question'])
        return self.research.broker.issue(self.user, run, trace['trace_id'])[1]

    def test_report_citation_does_not_hide_an_unresolved_review_id(self):
        run, report, _ = self.saved_sources()
        self.assert_code('citation_unknown', self.research.validate_answer, self.user, run['run_id'],
                         '概要[' + report['source_id'] + ']；原评（' + MISSING + '）')

    def test_followup_raw_entity_reuses_owned_prior_original_without_new_source(self):
        run, _, review = self.saved_sources()
        self.finish_legacy(run, '此前原评 ' + ENTITY)
        later = self.accept(request_id='followup-raw')
        answer, citations = self.research.validate_answer(self.user, later['run_id'], '继续分析（' + ENTITY + '）')
        self.assertEqual(answer, '继续分析（[1]）')
        self.assertEqual(citations[0]['source_id'], review['source_id'])
        self.assertEqual(self.state.list_sources(self.user, later['run_id']), [])
        self.assertEqual(self.data.calls, [])

    def test_unbound_numbers_never_pass_as_citations_in_answers_or_markdown(self):
        run = self.accept()
        for body in ('结论 [1]', '| 结论 | 来源 |\n| --- | --- |\n| 合成 | [27] |'):
            self.assert_code('citation_unknown', self.research.validate_answer, self.user, run['run_id'], body)
        cap = self.live_cap(run)
        self.manager.read_output = Mock(return_value=b'# Report\n\nClaim [1]')
        result = self.research.tool(cap, 'publish_artifact', {'path': '/workspace/bad.md', 'filename': 'bad.md'})
        self.assertEqual(result['error']['code'], 'citation_unknown')
        self.assertEqual(self.store.get_run(self.user, run['run_id'])['artifacts'], [])
        literal = '`array[1]`\n\n> original [1]\n\n[1](https://example.com/reference)'
        self.assertEqual(self.research.validate_answer(self.user, run['run_id'], literal), (literal, []))

    def test_rendered_markdown_must_match_the_server_validated_body(self):
        run, _, _ = self.saved_sources()
        cap = self.live_cap(run)
        self.manager.tool = Mock(return_value={'files': [{'path': '/workspace/report.md', 'filename': 'report.md'}]})
        self.manager.read_output = Mock(return_value=b'# changed\n\nUnbound [1]')
        result = self.research.tool(cap, 'publish_report', {'title': 'Report', 'filename': 'report',
            'markdown': ENTITY, 'formats': ['md']})
        self.assertEqual(result['error']['code'], 'report_render_failed')
        self.assertEqual(self.store.get_run(self.user, run['run_id'])['artifacts'], [])

    def test_legacy_followup_17_positions_recover_16_prior_reviews_read_only(self):
        run = self.accept()
        rows = [self.review(run, 'rev_' + format(i, '024x')) for i in range(16)]
        self.finish_legacy(run, '原评研究已保存。')
        later = self.accept(request_id='followup-17')
        ids = [r['source']['content']['review_entity_id'] for r in rows]
        raw = self.finish_legacy(later, ' '.join(ids + ids[:1]))
        with self.state._connect() as db:
            before = '\n'.join(db.iterdump())
        projected = self.project([raw])[0]
        self.assertFalse(entity_ids(projected['content']))
        self.assertNotIn('暂不可用', projected['content'])
        self.assertEqual(projected['content'].count('['), 17)
        self.assertEqual([c['source_id'] for c in projected['metadata']['citations']], [r['source_id'] for r in rows])
        self.assertEqual(self.project([projected]), [projected])
        with self.state._connect() as db:
            self.assertEqual('\n'.join(db.iterdump()), before)

    def test_prior_lookup_ignores_later_runs_and_prior_sources_saved_after_run_start(self):
        run, _, review = self.saved_sources()
        self.finish_legacy(run, ENTITY)
        later = self.accept(request_id='followup-time')
        raw = self.finish_legacy(later, ENTITY)
        first = self.project([raw])[0]
        self.assertEqual(first['metadata']['citations'][0]['source_id'], review['source_id'])
        future = self.accept(request_id='future-version')
        changed = self.review(future, text='Later version must not rewrite earlier citations.')
        # Even a backdated source must not cross a later run boundary.
        with self.state._connect() as db:
            db.execute('UPDATE research_sources SET created_at=? WHERE source_id=?', (review['created_at'], changed['source_id']))
        self.assertEqual(self.project([raw])[0], first)
        # A source added to an old run after the follow-up started is also ineligible.
        with self.state._connect() as db:
            db.execute('UPDATE research_sources SET run_id=?,created_at=? WHERE source_id=?',
                       (run['run_id'], '2100-01-01T00:00:00+00:00', changed['source_id']))
        self.assertEqual(self.project([raw])[0], first)

    def test_identical_prior_copies_are_one_version_but_conflicting_versions_fail(self):
        run, _, original = self.saved_sources()
        self.finish_legacy(run, ENTITY)
        second = self.accept(request_id='copy')
        self.state.add_source(self.user, second['run_id'], original['source'], attempt_id=second['attempt_id'])
        self.finish_legacy(second, ENTITY)
        third = self.accept(request_id='reuse-copies')
        answer, citations = self.research.validate_answer(self.user, third['run_id'], ENTITY)
        self.assertEqual(answer, '[1]')
        self.assertEqual(citations[0]['source_id'], original['source_id'])
        self.finish_legacy(third, ENTITY)
        fourth = self.accept(request_id='changed-version')
        changed = self.review(fourth, text='A distinct immutable source version.')
        # A source explicitly read in this run remains authoritative for raw IDs.
        self.assertEqual(self.research.validate_answer(self.user, fourth['run_id'], ENTITY)[1][0]['source_id'], changed['source_id'])
        self.finish_legacy(fourth, ENTITY)
        fifth = self.accept(request_id='ambiguous-prior')
        self.assert_code('citation_unknown', self.research.validate_answer, self.user, fifth['run_id'], ENTITY)
        self.finish_legacy(fifth, ENTITY)
        self.assertIn('存在多个来源版本', self.project()[-1]['content'])

    def test_corrupt_current_copy_cannot_fall_back_to_a_good_prior_source(self):
        run, _, original = self.saved_sources()
        self.finish_legacy(run, ENTITY)
        later = self.accept(request_id='bad-copy')
        current = self.review(later)
        with self.state._connect() as db:
            db.execute('UPDATE research_sources SET source_hash=? WHERE source_id=?', ('sha256:' + '0' * 64, current['source_id']))
        self.assert_code('citation_unknown', self.research.validate_answer, self.user, later['run_id'], ENTITY)
        self.finish_legacy(later, ENTITY)
        self.assertIn('原文校验失败', self.project()[-1]['content'])

    def test_cross_run_sources_are_exported_with_originals_and_native_delivery(self):
        run, _, review = self.saved_sources()
        self.finish_legacy(run, ENTITY)
        later = self.accept(request_id='cross-export')
        def generate(payload):
            cap = self.cap(payload)
            self.manager.read_output = Mock(return_value=('# Report\n\n' + ENTITY).encode())
            published = self.research.tool(cap, 'publish_artifact', {'path': '/workspace/ok.md', 'filename': 'ok.md'})
            self.assertIn('artifact_id', published, published)
            _, path = self.research.artifact(self.user, self.sid, published['artifact_id'])
            self.assertIn(review['source']['content']['review_text'], path.read_text())
        self.manager.on_start = generate
        self.manager.outcome['result']['answer'] = '原评 ' + ENTITY
        self.research.execute(self.user, later)
        final = self.store.list_messages(self.user, self.sid)[-1]
        self.assertEqual(final['content'], '原评 [1]')
        self.assertEqual(final['metadata']['citations'][0]['source_id'], review['source_id'])

    def test_raw_review_is_resolved_to_the_saved_original_in_final_answer(self):
        run, report, review = self.saved_sources()
        answer, citations = self.research.validate_answer(self.user, run['run_id'],
            '概要[' + report['source_id'] + ']；原评（' + ENTITY + '），重复 ' + ENTITY)
        self.assertEqual(answer, '概要[1]；原评（[2]），重复 [2]')
        self.assertEqual([c['source_id'] for c in citations], [report['source_id'], review['source_id']])

    def test_source_and_raw_spelling_share_one_citation(self):
        run, _, review = self.saved_sources()
        answer, citations = self.research.validate_answer(self.user, run['run_id'],
            '[' + review['source_id'] + ']、`' + ENTITY + '`、[' + ENTITY + ']、**' + ENTITY + '**、__' + ENTITY + '__')
        self.assertEqual(answer, '[1]、[1]、[1]、**[1]**、__[1]__')
        self.assertEqual(len(citations), 1)

    def test_literal_markdown_and_urls_are_not_rewritten(self):
        run, report, review = self.saved_sources()
        literals = [
            f'```json\n{{"entity":"{ENTITY}","source":"[{review["source_id"]}]"}}\n```',
            f'~~~text\n{MISSING}\n~~~', f'    {MISSING}', f'\t{MISSING}',
            f'`review_entity_id={ENTITY}`', f'`[{review["source_id"]}]`',
            f'[{ENTITY}](https://example.com/{ENTITY})', f'![{ENTITY}](image.png)',
            f'[example][{ENTITY}]', f'[{ENTITY}]: https://example.com/{ENTITY}',
            f'https://example.com/{MISSING}', f'HTTPS://example.com/{MISSING}', f'<https://example.com/{MISSING}>',
            '`rev_`', '`rev_example`',
            f'<!-- {MISSING}\nstill {ENTITY} -->', f'> 原文标识 {MISSING}',
            f'<span data-id="{MISSING}">literal</span>', f'\\{MISSING}',
        ]
        original = '\n\n'.join(literals)
        answer, citations = self.research.validate_answer(self.user, run['run_id'],
            original + '\n\n结论（' + ENTITY + '）[' + report['source_id'] + ']')
        self.assertTrue(answer.startswith(original + '\n\n'), answer)
        self.assertEqual(len(citations), 2)
        self.assertFalse(entity_ids(original))

    def test_unknown_and_malformed_entities_fail_even_with_a_report(self):
        run, report, _ = self.saved_sources()
        for value in (MISSING, 'rev_', '[rev_]', 'rev_wrong', 'rev_' + 'a' * 23, ENTITY + '0', ENTITY + '-suffix'):
            with self.subTest(value=value):
                self.assert_code('citation_unknown', self.research.validate_answer, self.user, run['run_id'],
                                 '[' + report['source_id'] + '] (' + value + ')')

    def test_multiple_source_versions_are_not_guessed(self):
        run, _, _ = self.saved_sources()
        self.review(run, text='A different version of the same entity.')
        self.assert_code('citation_unknown', self.research.validate_answer, self.user, run['run_id'], ENTITY)
        self.finish_legacy(run, ENTITY)
        self.assertIn('存在多个来源版本', self.project()[-1]['content'])
        self.assertEqual(self.project()[-1]['metadata']['citations'], [])

    def test_empty_body_and_hash_mismatch_are_rejected_for_both_spellings(self):
        run, _, review = self.saved_sources()
        empty = self.review(run, MISSING, text='  ')
        with self.state._connect() as db:
            db.execute('UPDATE research_sources SET source_hash=? WHERE source_id=?', ('sha256:' + '0' * 64, review['source_id']))
        for value in (ENTITY, MISSING, '[' + review['source_id'] + ']', '[' + empty['source_id'] + ']'):
            with self.subTest(value=value):
                self.assert_code('citation_unknown', self.research.validate_answer, self.user, run['run_id'], value)
        self.finish_legacy(run, ENTITY + ' ' + MISSING)
        result = self.project()[-1]
        self.assertIn('原文校验失败', result['content'])
        self.assertIn('原文缺失', result['content'])
        self.assertEqual(result['metadata']['citations'], [])

    def test_reference_limit_is_explicit_and_legacy_answer_stays_readable(self):
        run, _, _ = self.saved_sources()
        content = ' '.join('rev_' + format(index, '024x') for index in range(101))
        self.assert_code('citation_limit', self.research.validate_answer, self.user, run['run_id'], content)
        self.finish_legacy(run, '保留结论。' + content)
        result = self.project()[-1]
        self.assertTrue(result['content'].startswith('保留结论。'))
        self.assertEqual(result['content'].count('引用数量超限'), 101)
        self.assertEqual(result['metadata']['citations'], [])

    def test_legacy_33_positions_recover_31_reviews_preserving_four_original_citations(self):
        run = self.accept()
        originals = [self.state.add_source(self.user, run['run_id'],
            {'source_type': 'report' if index < 3 else 'dataset', 'title': f'Synthetic original {index}', 'content': {}},
            attempt_id=run['attempt_id']) for index in range(4)]
        reviews = [self.review(run, 'rev_' + format(index, '024x')) for index in range(31)]
        old = [source_citation(row, index + 1) for index, row in enumerate(originals)]
        entities = [row['source']['content']['review_entity_id'] for row in reviews]
        body = '概述 [1] [2] [3] [4] [2]\n\n| 主题 | 原评 |\n| --- | --- |\n'
        body += '\n'.join('| 合成主题 | (' + value + ') |' for value in entities + entities[:2])
        raw = self.finish_legacy(run, body, old)
        bundle = self.store.session_bundle(self.user, self.sid)
        with self.state._connect() as db:
            before = '\n'.join(db.iterdump())
        with patch.object(self.state, '_review_citation_sources', wraps=self.state._review_citation_sources) as lookup:
            first = self.state.enrich_bundle(self.user, bundle)['messages'][-1]
            self.assertEqual(lookup.call_count, 1)
            self.assertEqual(len(lookup.call_args.args[3]), 31)
        second = self.state.enrich_bundle(self.user, bundle)['messages'][-1]
        self.assertEqual(first, second)
        self.assertEqual(self.project([first]), [first])
        self.assertEqual(first['metadata']['citations'][:4], old)
        self.assertEqual(len(first['metadata']['citations']), 35)
        self.assertEqual(first['content'].count('(['), 33)
        self.assertFalse(entity_ids(first['content']))
        expected = body
        for index, value in enumerate(entities, 5):
            expected = expected.replace(value, '[' + str(index) + ']')
        self.assertEqual(first['content'], expected)
        for citation, review in zip(first['metadata']['citations'][4:], reviews):
            self.assertEqual(citation['source_id'], review['source_id'])
            saved = self.research.source(self.user, self.sid, citation['source_id'])
            self.assertEqual(saved['source_sha256'], citation['source_sha256'])
            self.assertEqual(saved['content'], review['source']['content'])
        with self.state._connect() as db:
            self.assertEqual('\n'.join(db.iterdump()), before)
        self.assertEqual(self.store.list_messages(self.user, self.sid)[-1], raw)
        self.assertEqual(bundle['messages'][-1], raw)
        self.assertEqual(self.data.calls, [])
        self.assertEqual(self.manager.starts, [])

    def test_existing_review_citation_is_reused_and_conflicting_hash_is_not(self):
        run, _, review = self.saved_sources()
        old = [source_citation(review, 1)]
        raw = self.finish_legacy(run, '[1] (' + ENTITY + ')', old)
        projected = self.project([raw])[0]
        self.assertEqual(projected['content'], '[1] ([1])')
        self.assertEqual(projected['metadata']['citations'], old)
        raw['metadata']['citations'][0]['source_sha256'] = 'sha256:' + '0' * 64
        projected = self.project([raw])[0]
        self.assertIn('引用版本不一致', projected['content'])
        self.assertEqual(projected['metadata']['citations'], raw['metadata']['citations'])

    def test_late_sources_and_other_runs_do_not_change_historical_binding(self):
        run, _, review = self.saved_sources()
        late = self.review(run, text='Saved after the answer; not historical evidence.')
        with self.state._connect() as db:
            db.execute('UPDATE research_sources SET created_at=? WHERE source_id=?', ('2100-01-01T00:00:00+00:00', late['source_id']))
        self.finish_legacy(run, ENTITY + ' ' + MISSING)
        first = self.project()[-1]
        self.assertEqual(first['metadata']['citations'][0]['source_id'], review['source_id'])
        self.assertIn('来源缺失', first['content'])
        later_run = self.accept(request_id='later')
        self.review(later_run, MISSING)
        self.review(later_run, ENTITY, text='Other run, same entity.')
        self.assertEqual(self.project()[-2], first)

    def test_lookup_is_scoped_by_owner_scope_session_and_run(self):
        run, _, review = self.saved_sources()
        raw = self.finish_legacy(run, ENTITY)
        for owner in (actor('another-user'), replace(self.user, scope_id='another-scope')):
            with self.subTest(owner=owner):
                self.assert_code('session_not_found', self.state.project_message_citations, owner, self.sid, [raw])
                self.assert_code('session_not_found', self.research.source, owner, self.sid, review['source_id'])
        other_session = self.store.create_session(self.user, 'Another session')['session_id']
        request, settings = self.research.validate_request(self.user, other_session, {'question': 'Another question', 'request_id': 'other'})
        other_run = self.state.accept(self.user, other_session, request, settings)['run']
        self.assert_code('citation_unknown', self.research.validate_answer, self.user, other_run['run_id'], ENTITY)
        self.assert_code('source_not_found', self.research.source, self.user, other_session, review['source_id'])
        forged = {**raw, 'session_id': other_session}
        result = self.state.project_message_citations(self.user, other_session, [forged])[0]
        self.assertEqual(result['metadata']['citations'], [])
        self.assertIn('来源读取未完成', result['content'])

    def test_unavailable_or_malformed_source_does_not_break_history_or_write_it(self):
        run, _, review = self.saved_sources()
        raw = self.finish_legacy(run, '保留结论 ' + ENTITY)
        with patch.object(self.state, '_review_citation_sources', side_effect=sqlite3.OperationalError('interrupted')):
            result = self.project()[-1]
        self.assertIn('来源读取未完成', result['content'])
        self.assertEqual(self.store.list_messages(self.user, self.sid)[-1], raw)
        with self.state._connect() as db:
            db.execute('UPDATE research_sources SET source_json=? WHERE source_id=?', ('malformed', review['source_id']))
        self.assertIn('来源读取未完成', self.project()[-1]['content'])
        self.assertEqual(self.store.list_messages(self.user, self.sid)[-1], raw)

    def test_unknown_metadata_and_timestamp_fail_closed_without_breaking_history(self):
        run, _, _ = self.saved_sources()
        raw = self.finish_legacy(run, ENTITY)
        for changes in ({'metadata': None}, {'metadata': {'citations': 'old-format'}}, {'created_at': 'bad-time'},
                        {'created_at': '2026-10-13T00:00:00'}):
            with self.subTest(changes=changes):
                result = self.project([{**raw, **changes}])[0]
                self.assertIn('来源读取未完成', result['content'])
        user_message = {**raw, 'role': 'user'}
        self.assertEqual(self.project([user_message]), [user_message])

    def test_authenticated_history_and_source_endpoints_share_the_recovered_identity(self):
        from fastapi.testclient import TestClient
        from agent.app import create_app
        from agent.identity import TrustedHeaderIdentityProvider
        from agent.tests.helpers import signed_headers
        run, _, review = self.saved_sources()
        raw = self.finish_legacy(run, '| 主题 | 原评 |\n| --- | --- |\n| 合成 | ' + ENTITY + ' |')
        secret = 'synthetic-hmac-key-for-citation-test'
        app = create_app(service=self.service, identity_provider=TrustedHeaderIdentityProvider(secret), research_backend=self.research)
        headers = signed_headers(secret, self.user)
        path = '/wenquan/api/v1/sessions/' + self.sid
        with TestClient(app) as client:
            first = client.get(path, headers=headers)
            self.assertEqual(first.status_code, 200, first.text)
            message = first.json()['messages'][-1]
            self.assertIn('[1]', message['content'])
            citation = message['metadata']['citations'][0]
            source = client.get(citation['source_href'], headers=headers)
            self.assertEqual(source.status_code, 200, source.text)
            self.assertEqual(source.json()['source_sha256'], citation['source_sha256'])
            self.assertEqual(source.json()['content'], review['source']['content'])
            self.assertEqual(client.get(path, headers=headers).json()['messages'][-1], message)
            for url in (path, citation['source_href']):
                self.assertEqual(client.get(url).status_code, 401)
                self.assertEqual(client.get(url, headers=signed_headers(secret, actor('other'))).status_code, 404)
        self.assertEqual(self.store.list_messages(self.user, self.sid)[-1], raw)

    def test_native_final_and_compatibility_submit_use_the_same_validator(self):
        for mode in ('native', 'submit'):
            with self.subTest(mode=mode):
                run = self.accept(request_id=mode)
                review = self.review(run)
                def generate(payload):
                    if mode == 'native':
                        self.manager.outcome['result']['answer'] = '合成结论 (' + ENTITY + ')'
                    else:
                        result = self.research.tool(self.cap(payload), 'submit_answer', {'answer': '合成结论 (' + ENTITY + ')'})
                        self.assertTrue(result['accepted'], result)
                self.manager.on_start = generate
                self.research.execute(self.user, run)
                self.assertEqual(self.state.get(self.user, run['run_id'])['status'], 'completed')
                message = self.store.list_messages(self.user, self.sid)[-1]
                self.assertEqual(message['content'], '合成结论 ([1])')
                self.assertEqual(message['metadata']['citations'][0]['source_id'], review['source_id'])

    def test_followup_context_uses_recovered_citations_without_rewriting_old_messages(self):
        run, _, review = self.saved_sources()
        raw = self.finish_legacy(run, '旧结论 (' + ENTITY + ')')
        later = self.accept(request_id='followup')
        def generate(payload):
            history = json.loads(payload['prompt'])['conversation']
            previous = next(item for item in history if item['role'] == 'assistant')
            self.assertEqual(previous['content'], '旧结论 ([' + review['source_id'] + '])')
            self.assertEqual(previous['citations'][0]['source_id'], review['source_id'])
            self.manager.outcome['result']['answer'] = '沿用已核验来源 [' + review['source_id'] + ']'
        self.manager.on_start = generate
        self.research.execute(self.user, later)
        self.assertEqual(self.state.get(self.user, later['run_id'])['status'], 'completed')
        self.assertEqual(self.store.list_messages(self.user, self.sid)[-3], raw)

    def test_markdown_with_only_raw_entities_is_validated_and_carries_originals(self):
        run, _, review = self.saved_sources()
        cap = self.live_cap(run)
        self.manager.read_output = Mock(return_value=('# 合成报告\n\n原评（' + ENTITY + '）').encode())
        result = self.research.tool(cap, 'publish_artifact', {'path': '/workspace/report.md', 'filename': 'report.md'})
        self.assertIn('artifact_id', result, result)
        _, saved = self.research.artifact(self.user, self.sid, result['artifact_id'])
        content = saved.read_text()
        self.assertIn(review['source']['content']['review_text'], content)
        self.assertNotIn('（' + ENTITY + '）', content)
        self.assertEqual(cap.model_calls, 0)
        self.manager.read_output = Mock(return_value=('# Invalid\n\n' + MISSING).encode())
        invalid = self.research.tool(cap, 'publish_artifact', {'path': '/workspace/invalid.md', 'filename': 'invalid.md'})
        self.assertEqual(invalid['error']['code'], 'citation_unknown')
        self.assertEqual(len(self.store.get_run(self.user, run['run_id'])['artifacts']), 1)

    def test_literal_markdown_and_non_markdown_artifacts_remain_byte_identical(self):
        run, _, _ = self.saved_sources()
        cap = self.live_cap(run)
        for filename, content in [('method.md', '```json\n{"review_entity_id":"' + MISSING + '"}\n```'),
                                  ('data.csv', 'review_entity_id\n' + MISSING), ('data.jsonl', '{"id":"' + MISSING + '"}')]:
            with self.subTest(filename=filename):
                self.manager.read_output = Mock(return_value=content.encode())
                result = self.research.tool(cap, 'publish_artifact', {'path': '/workspace/' + filename, 'filename': filename})
                self.assertIn('artifact_id', result, result)
                _, saved = self.research.artifact(self.user, self.sid, result['artifact_id'])
                self.assertEqual(saved.read_bytes(), content.encode())

    def test_report_renderer_receives_checked_markdown_for_both_md_and_pdf(self):
        run, _, review = self.saved_sources()
        cap = self.live_cap(run)
        rendered, inputs = {}, []
        def render(attempt, name, args):
            self.assertEqual(name, 'render_report')
            inputs.append(args['markdown'])
            files = []
            for form in args['formats']:
                filename = args['filename'] + '.' + form
                path = '/workspace/' + filename
                rendered[path] = args['markdown'].encode() if form == 'md' else b'%PDF-synthetic-transport-only'
                files.append({'path': path, 'filename': filename})
            return {'files': files}
        self.manager.tool = Mock(side_effect=render)
        self.manager.read_output = Mock(side_effect=lambda attempt, path: rendered[path])
        args = {'title': '合成报告', 'filename': 'synthetic', 'markdown': '原评（' + ENTITY + '）', 'formats': ['md', 'pdf']}
        result = self.research.tool(cap, 'publish_report', args)
        self.assertEqual(result['status'], 'available', result)
        self.assertEqual(len(inputs), 1)
        self.assertIn('原评（[1]）', inputs[0])
        self.assertIn(review['source']['content']['review_text'], inputs[0])
        self.assertEqual({item['filename'] for item in result['files']}, {'synthetic.md', 'synthetic.pdf'})
        invalid = self.research.tool(cap, 'publish_report', {**args, 'markdown': '未知（' + MISSING + '）'})
        self.assertEqual(invalid['error']['code'], 'citation_unknown')
        self.assertEqual(len(inputs), 1)
        self.assertEqual(cap.model_calls, 0)


if __name__ == '__main__':
    unittest.main()
