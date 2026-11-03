import json
import unittest
from unittest.mock import Mock

from agent.review_link_discovery import candidates
from agent.tests.test_harness_research import HarnessFixture


URL = 'https://www.lowes.com/pd/EGO-Blower/5012345678'


class ReviewLinkTests(unittest.TestCase):
    def test_exact_variants_product_paths_and_channel_support(self):
        rows = [{'url': URL, 'title': 'EGO LB8803 leaf blower'},
                {'url': 'https://www.lowes.com/pd/Other/5011111111', 'title': 'EGO LB8800 blower'},
                {'url': 'https://www.lowes.com/search?searchTerm=LB8803', 'title': 'EGO LB8803'},
                {'url': 'https://www.homedepot.com/p/EGO/123456789', 'title': 'EGO LB8803'},
                {'url': 'https://evil-lowes.com/pd/EGO/5011111111', 'title': 'EGO LB8803'}]
        result = {'content': [{'type': 'text', 'text': json.dumps({'results': rows})}]}
        items = candidates(result, [r['url'] for r in rows], 'EGO', 'LB8803', ['lowes'])
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]['product_url'], URL)
        self.assertEqual(items[0]['identity_status'], 'exact_search_match')
        self.assertFalse(next(i for i in items if i['channel'] == 'homedepot')['auto_collectable'])
        self.assertEqual(next(i for i in items if '/Other/' in i['product_url'])['identity_status'], 'needs_verification')


class ReviewLinkFlowTests(HarnessFixture, unittest.TestCase):
    def test_empty_web_search_uses_trusted_collection_discovery_and_reuses_lookup(self):
        run = self.accept(question='查找 EGO LB8803 网评并采集', web_enabled=True)
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run['run_id'], question=run['request']['question'])
        cap = self.research.broker.issue(self.user, run, trace['trace_id'])[1]
        self.research.web.search = Mock(side_effect=lambda query, **kw: {'provider': 'aliyun-websearch', 'query': query,
            'content': [{'type': 'text', 'text': '{"pages":[]}'}]})
        response = {'schema_version': 'voxquarry.research-collection/v1', 'brand': 'EGO', 'model': 'LB8803',
            'items': [{'url': URL, 'title': 'EGO LB8803', 'brand': 'EGO', 'model': 'LB8803',
                       'channel': 'lowes', 'discovery_source': 'collection_history'}], 'coverage': 'returned_history', 'warnings': []}
        self.collection.call = Mock(side_effect=lambda op, args=None: {'auto_collection_channels': ['lowes']} if op == 'capabilities'
            else {'product': {'brand': 'Other', 'model': 'LB8803'}} if op == 'resolve' else response)
        first = self.research.tool(cap, 'find_review_links', {'brand': 'EGO', 'model': 'LB8803'})
        self.assertEqual(first['items'][0]['product_url'], URL)
        self.assertEqual(first['items'][0]['identity_evidence'], 'collection_history')
        self.assertTrue(self.research.egress.resolve_collection_url(self.user, run['run_id'], URL)['approved'])
        self.research.tool(cap, 'find_review_links', {'brand': 'EGO', 'model': 'LB8803'})
        self.assertEqual(sum(c.args[0] == 'discover' for c in self.collection.call.call_args_list), 1)
        # A resumed pre-fix run must refresh its empty/old discovery receipt.
        searches = self.research.state.get(self.user, run['run_id'])['checkpoint']['review_link_searches']
        for value in searches.values():
            value.pop('discovery_version')
        self.research._checkpoint(cap, review_link_searches=searches)
        self.research.tool(cap, 'find_review_links', {'brand': 'EGO', 'model': 'LB8803'})
        self.assertEqual(sum(c.args[0] == 'discover' for c in self.collection.call.call_args_list), 2)
        resolved = self.research.tool(cap, 'resolve_product', {'channel': 'lowes', 'product_url': URL})
        self.assertEqual(resolved['requested_identity_status'], 'model_conflict')
        response['model'] = 'LB8800'
        with self.assertRaisesRegex(Exception, '品牌型号不一致'):
            plan = self.research.egress.plan_review_search(self.user, run['run_id'], 'EGO', 'LB8803')
            self.research.egress.register_collection_discovery(self.user, run['run_id'], plan['search_id'], response)

    def test_discovery_provenance_cache_and_identity_gate(self):
        run = self.accept(question='查找 EGO LB8803 各渠道网评并采集生成报告', web_enabled=True)
        self.research.egress.register_user_urls(self.user, run['run_id'])
        trace = self.traces.start_trace(self.user, session_id=self.sid, run_id=run['run_id'], question=run['request']['question'])
        cap = self.research.broker.issue(self.user, run, trace['trace_id'])[1]
        self.research.web.search = Mock(side_effect=lambda query, **kw: {'provider': 'aliyun-websearch', 'query': query,
            'content': [{'type': 'text', 'text': json.dumps({'results': [{'url': URL, 'title': 'EGO LB8803 blower'}]})}]})
        original = self.collection.call
        self.collection.call = Mock(side_effect=lambda op, args=None: {'auto_collection_channels': ['lowes']} if op == 'capabilities'
            else {'product': {'brand': 'EGO', 'model': 'LB8800'}} if op == 'resolve' else original(op, args))
        first = self.research.tool(cap, 'find_review_links', {'brand': 'EGO', 'model': 'LB8803'})
        self.assertEqual(first['items'][0]['product_url'], URL)
        second = self.research.tool(cap, 'find_review_links', {'brand': 'EGO', 'model': 'LB8803'})
        self.assertEqual(second['items'], first['items'])
        self.assertEqual(second['source_id'], first['source_id'])
        self.assertEqual(self.research.web.search.call_count, 1)
        rejected = self.research.tool(cap, 'collect_product', {'channel': 'lowes', 'product_url': URL})
        self.assertEqual(rejected['error']['code'], 'product_identity_unverified')
        resolved = self.research.tool(cap, 'resolve_product', {'channel': 'lowes', 'product_url': URL})
        self.assertEqual(resolved['requested_identity_status'], 'model_conflict')
        rejected = self.research.tool(cap, 'collect_product', {'channel': 'lowes', 'product_url': URL})
        self.assertEqual(rejected['error']['code'], 'product_identity_unverified')
        self.assertFalse(any(call.args[0] == 'ensure' for call in self.collection.call.call_args_list))
