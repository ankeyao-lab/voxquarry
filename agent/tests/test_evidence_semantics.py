from __future__ import annotations

import copy
import unittest

from agent.evidence_semantics import exact_source_quote, model_evidence_view
from reporting.analysis import _atom_rows, _kano_candidates, _table
from reporting.factors import _rating_polarity


class EvidenceSemanticsTests(unittest.TestCase):
    def test_typographic_quote_recovers_actual_original_without_fuzzy_matching(self):
        original = 'The reviewer wrote: "It\'s easy to turn, but only when self-propel is off."'
        self.assertEqual(exact_source_quote('“It’s easy to turn, but only when self-propel is off.”', original),
                         '"It\'s easy to turn, but only when self-propel is off."')
        for changed in ("It's easy to turn.", "It's easy to turn, even when self-propel is off.",
                        "It's easy to turn … self-propel is off.", "it's easy to turn", "  "):
            self.assertIsNone(exact_source_quote(changed, original))

    def factor_rows(self, ratings=(5, 4, 2)):
        atoms = [{"entity_ref": f"review-{index}", "factor_namespace": "attribute",
                  "factor_id": "power", "factor_label": "动力", "framework": None,
                  "polarity": _rating_polarity(rating)} for index, rating in enumerate(ratings)]
        return _atom_rows(atoms, "attribute", len(atoms), {("attribute", "power"): ["evidence-1"]})

    def test_actual_factor_table_replaces_misleading_labels_and_preserves_values(self):
        table = _table("attribute_performance", "产品属性表现", [
            {"field": "positive_share", "label": "好评占比"},
            {"field": "negative_share", "label": "差评占比"},
            {"field": "label", "label": "因子"},
        ], self.factor_rows(), denominator_note="原分母说明",
            default_sort={"field": "mention_count", "direction": "desc"})
        view = model_evidence_view({"section": "/tables", "content": [table]})
        transformed = view["content"][0]
        self.assertIn("整机4–5星", transformed["columns"][0]["label"])
        self.assertIn("整机1–2星", transformed["columns"][1]["label"])
        self.assertEqual(transformed["columns"][2], table["columns"][2])
        row = transformed["rows"][0]
        self.assertEqual(row["positive_share"], table["rows"][0]["positive_share"])
        semantic = row["_evidence_semantics"]["whole_product_rating"]
        self.assertEqual(semantic["denominator_field"], "rated_mentions")
        self.assertIn("net_rating_polarity", semantic["fields"])
        self.assertIn("不是对所提功能的态度", semantic["meaning"])

    def test_rating_change_does_not_change_feature_expression(self):
        row = {"aspect_id": "drive", "label": "自走", "review_entities": 2,
               "rating_n": 2, "high_star_entities": 0, "high_star_rate": 0,
               "low_star_entities": 2, "low_star_rate": 1,
               "explicit_positive_entities": 0, "explicit_positive_rate": 0,
               "explicit_negative_entities": 2, "explicit_negative_rate": 1,
               "negative_evidence_ids": ["evidence-1", "evidence-2"]}
        high_star = {**row, "high_star_entities": 2, "high_star_rate": 1,
                     "low_star_entities": 0, "low_star_rate": 0}
        low, high = model_evidence_view(row), model_evidence_view(high_star)
        self.assertEqual(low["_evidence_semantics"]["rule_aspect_expression"],
                         high["_evidence_semantics"]["rule_aspect_expression"])
        self.assertEqual(low["explicit_negative_entities"], high["explicit_negative_entities"])
        self.assertEqual(high["high_star_rate"], 1)
        self.assertIn("规则可能误读", high["_evidence_semantics"]["rule_aspect_expression"]["meaning"])

    def test_same_positive_name_has_different_meanings_in_real_shapes(self):
        samples = {"valid_rating_entities": 4, "positive_entities": 3, "negative_entities": 1,
                   "positive_rate": .75, "negative_rate": .25, "histogram": {"1": 1, "5": 3}}
        experience = {"item_id": "drive", "review_entities": 4, "valid_rating_entities": 4,
                      "positive_entities": 1, "negative_entities": 3,
                      "explicit_positive_rate": .25, "explicit_negative_rate": .75,
                      "positive_rate": .75, "negative_rate": .25}
        first, second = model_evidence_view([samples, experience])
        self.assertIn("positive_entities", first["_evidence_semantics"]["whole_product_rating"]["fields"])
        self.assertNotIn("positive_entities", second["_evidence_semantics"]["whole_product_rating"]["fields"])
        self.assertIn("positive_entities", second["_evidence_semantics"]["rule_aspect_expression"]["fields"])

    def test_kano_candidate_is_a_report_inference(self):
        row = model_evidence_view(_kano_candidates(self.factor_rows()))[0]
        self.assertEqual(row["positive_share"], self.factor_rows()[0]["positive_share"])
        self.assertIn("report_classification", row["_evidence_semantics"])
        self.assertNotIn("denominator_field", row["_evidence_semantics"]["whole_product_rating"])

    def test_keywords_are_not_semantic_complaint_counts(self):
        data = {"query": {"term_groups": [{"label": "drive", "terms": ["drive"]}]},
                "intersection_count": 2, "group_match_counts": [{"label": "drive", "count": 2}],
                "summary": {"count": 2, "valid_rating_count": 2, "positive_rating_count": 2,
                            "negative_rating_count": 0, "average_rating": 5},
                "items": [{"review_text": "The drive is excellent.", "rating": 5}]}
        view = model_evidence_view(data)
        self.assertIn("不是体验、投诉或赞扬", view["_evidence_semantics"]["keyword_matching"]["meaning"])
        self.assertEqual(view["summary"]["positive_rating_count"], 2)
        self.assertEqual(view["items"], data["items"])
        word = model_evidence_view({"term": "drive", "ngram": "unigram", "document_count": 3, "document_rate": .3})
        self.assertIn("keyword_matching", word["_evidence_semantics"])

    def test_unknown_positive_fields_and_original_prose_are_not_reinterpreted(self):
        value = {"positive_count": 3, "positive_rate": .75, "negative_share": .25,
                 "positive_entities": 3, "comment": "My old mower failed after two years.",
                 "columns": [{"field": "positive_rate", "label": "赞扬占比"}],
                 "metadata": {"factor_id": "opaque", "positive_share": .4}}
        self.assertEqual(model_evidence_view(value), value)

    def test_field_definitions_are_not_mistaken_for_numeric_results(self):
        definitions = {"positive_rating_count": "4–5星数", "high_star_rate": "高星数除以有效评分数"}
        self.assertEqual(model_evidence_view(definitions), definitions)
        self.assertEqual(model_evidence_view({"high_star_rate": True}), {"high_star_rate": True})

    def test_source_is_immutable_and_view_is_independent_and_idempotent(self):
        value = {"report_id": "report-1", "source_sha256": "sha256:fixture",
                 "content": {"nested": [self.factor_rows(), {"unknown": [{"count": 1}]}]}}
        before = copy.deepcopy(value)
        view = model_evidence_view(value)
        self.assertEqual(value, before)
        self.assertEqual(model_evidence_view(view), view)
        view["content"]["nested"][1]["unknown"][0]["count"] = 9
        self.assertEqual(value, before)

    def test_nested_table_context_does_not_leak_to_unknown_metadata(self):
        value = {"table_id": "pain_points", "columns": [{"field": "negative_share", "label": "差评占比"}],
                 "rows": [{"mention_count": 2, "negative_share": .5}],
                 "metadata": {"negative_share": .4}}
        view = model_evidence_view(value)
        self.assertIn("整机1–2星", view["columns"][0]["label"])
        self.assertIn("whole_product_rating", view["rows"][0]["_evidence_semantics"])
        self.assertEqual(view["metadata"], value["metadata"])

    def test_intrinsic_metric_and_reference_preserve_original_identity(self):
        metric = {"metric_id": "positive_rating_rate", "name": "好评率", "value": .8,
                  "numerator": 8, "denominator": 10}
        view = model_evidence_view(metric)
        self.assertEqual(view["metric_id"], metric["metric_id"])
        self.assertEqual(view["value"], .8)
        self.assertIn("整机4–5星", view["name"])
        ref = model_evidence_view({"section": "profiles", "field": "high_star_rate", "value": .8})
        self.assertEqual(ref["_evidence_semantics"]["whole_product_rating"]["source_field"], "high_star_rate")


if __name__ == "__main__":
    unittest.main()
