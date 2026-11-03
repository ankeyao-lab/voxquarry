import unittest
from collector.review_product_discovery import discover


class ReviewProductDiscoveryTests(unittest.TestCase):
    def test_history_is_exact_and_public_fallback_is_channel_bounded(self):
        history = [{'channel': 'lowes', 'url': 'https://www.lowes.com/pd/Fixture/1234567890',
                    'brand': 'EGO', 'model': 'LB8803-2', 'product_title': 'EGO kit'},
                   {'channel': 'ace', 'url': 'https://www.acehardware.com/departments/fixture/1234567',
                    'brand': 'EGO', 'model': 'LB8803', 'product_title': 'different kit'}]
        calls = []
        def search(query, channel):
            calls.append((query, channel))
            return [{'url': 'https://www.acehardware.com/departments/fixture/7654321', 'title': 'EGO LB8803-2 kit'}]
        result = discover('EGO', 'LB8803-2', ['lowes', 'ace'], history=lambda *_: history, search=search)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual([channel for _, channel in calls], ['ace'])
        self.assertEqual(result['items'][0]['discovery_source'], 'collection_history')
        self.assertNotIn('1234567', str(result['items'][1]))
        self.assertIn('"LB8803-2"', calls[0][0])

    def test_neighbor_model_in_search_title_is_not_exact_identity(self):
        result = discover('EGO', 'LB8803', ['lowes'], history=lambda *_: [],
            search=lambda *_: [{'url': 'https://www.lowes.com/pd/Fixture/1234567890', 'title': 'EGO LB8803-2 kit'}])
        self.assertEqual(result['items'], [])
