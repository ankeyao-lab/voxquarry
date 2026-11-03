import copy
import unittest

from agent.review_read_inventory import review_read_inventory


class ReviewReadInventoryTests(unittest.TestCase):
    def page(self, rows, total=3, fingerprint="frozen-a", query="query-a"):
        return {"query_sha256": query, "provenance": {"source_fingerprint": fingerprint},
                "intersection_count": total, "items": rows}

    def row(self, identifier, sku, rating):
        return {"review_occurrence_id": identifier, "source_product_id": sku,
                "channel": "official", "rating": rating, "rating_valid": True, "has_text": True}

    def saved(self, data, report="report-a"):
        return {"source": {"source_type": "report", "report_id": report,
                "provenance": {"tool": "query_report_reviews"}, "content": data}}

    def test_overlap_does_not_inflate_count_and_partial_is_explicit(self):
        a, b, c = self.row("a", "sku-a", 5), self.row("b", "sku-a", 1), self.row("c", "sku-b", 4)
        first = self.page([a, b]); original = copy.deepcopy(first)
        partial = review_read_inventory("report-a", first, [])
        self.assertFalse(partial["complete"])
        result = review_read_inventory("report-a", self.page([b, c]), [self.saved(first)])
        self.assertTrue(result["complete"])
        self.assertEqual(result["read_records"], 3)
        self.assertEqual(result["products"][0], {"channel": "official", "source_product_id": "sku-a",
                         "count": 2, "written_count": 2, "ratings": {"1": 1, "5": 1}, "unknown_rating_count": 0})
        self.assertEqual(first, original)

    def test_other_scope_snapshot_and_report_are_not_combined(self):
        a, b = self.row("a", "sku-a", 5), self.row("b", "sku-b", 1)
        excluded = [self.saved(self.page([b], fingerprint="frozen-b")),
                    self.saved(self.page([b], query="query-b")), self.saved(self.page([b]), report="report-b")]
        result = review_read_inventory("report-a", self.page([a]), excluded)
        self.assertEqual(result["read_records"], 1)
        self.assertFalse(result["complete"])

    def test_conflicting_or_unidentified_rows_do_not_produce_statistics(self):
        a = self.row("a", "sku-a", 5)
        self.assertIsNone(review_read_inventory("report-a", self.page([a]), [self.saved(self.page([{**a, "rating": 1}]))]))
        self.assertIsNone(review_read_inventory("report-a", self.page([{}]), []))
        self.assertIsNone(review_read_inventory("report-a", {"items": [a]}, []))
