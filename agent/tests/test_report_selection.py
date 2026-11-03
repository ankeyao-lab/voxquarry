import unittest
from agent.contracts import AgentError
from agent.report_selection import choose_report, question_dates
from agent.research_rankings import build_report_ranking, report_ranking_context, report_ranking_requested


def report(identifier="rpt_first", model="LM2236SP", channel="lowes", date_range=None):
    return {"report_id": identifier, "title": f"EGO {model} {channel}", "generated_at": "2026-09-08",
            "scope": {"models": [model], "brands": ["EGO"], "category_id": "walk_behind_mower",
                      "channels": [channel], "market_regions": ["North America"], "date_range": date_range or {}}}


class ReportSelectionTests(unittest.TestCase):
    def test_no_attachment_never_selects_first_report_without_product(self):
        with self.assertRaises(AgentError) as failure:
            choose_report("有哪些缺点？", [report()], [])
        self.assertEqual(failure.exception.code, "report_selection_required")

    def test_exact_model_and_aliases_match_without_word_boundary_whitespace(self):
        kind, chosen = choose_report("EGO 的LM2236SP在劳氏的评价怎样？", [report(), report("rpt_other", "LM2200")], [])
        self.assertEqual((kind, chosen["report_id"]), ("report", "rpt_first"))

    def test_multiple_models_require_clarification_and_unknown_product_does_not_inherit(self):
        reports = [report(), report("rpt_other", "LM2200")]
        for question in ("EGO有哪些缺点", "看看LM99999的评价", "换个产品看看"):
            with self.subTest(question=question), self.assertRaises(AgentError):
                choose_report(question, reports, [], "rpt_first")
        self.assertEqual(choose_report("这款在转弯时如何？", reports, [], "rpt_first")[1]["report_id"], "rpt_first")
        self.assertEqual(choose_report("换成LM2200", reports, [], "rpt_first")[1]["report_id"], "rpt_other")

    def test_brand_name_is_not_mistaken_for_its_official_channel(self):
        reports = [report(), report("rpt_official", channel="ego")]
        self.assertEqual(choose_report("总结 EGO LM2236SP 在 Lowe's 的报告", reports, [])[1]["report_id"], "rpt_first")
        self.assertEqual(choose_report("LM2236SP在ego官网的报告", reports, [])[1]["report_id"], "rpt_official")

    def test_dates_select_new_scope_and_never_silently_ignore_invalid_dates(self):
        candidate = {"candidate_id": "rc_one", "title": "EGO LM2236SP", "scope": {**report()["scope"], "job_ids": ["job_one"]}}
        kind, chosen = choose_report("LM2236SP 2026-01-01至2026-03-31的评论", [report()], [candidate])
        self.assertEqual(kind, "generate")
        self.assertEqual(chosen["date_range"], {"start": "2026-01-01", "end": "2026-03-31"})
        for question in ("2026-02-30至2026-03-31", "2026-09-01", "最近一个月", "最近0天", "2026-09-30至2026-01-01"):
            with self.subTest(question=question), self.assertRaises(AgentError):
                question_dates(question)

    def test_same_scope_uses_newest_report_and_candidate_superset_avoids_channel_ambiguity(self):
        older, newer = report("rpt_old"), report("rpt_new")
        newer["generated_at"] = "2026-09-09"
        self.assertEqual(choose_report("LM2236SP", [older, newer], [])[1]["report_id"], "rpt_new")
        one = {"candidate_id": "one", "scope": {**older["scope"], "job_ids": ["a"]}}
        all_channels = {"candidate_id": "all", "scope": {**older["scope"], "channels": ["lowes", "homedepot"], "job_ids": ["a", "b"]}}
        self.assertEqual(choose_report("LM2236SP", [], [one, all_channels])[1]["candidate_id"], "all")

    def test_rankings_use_all_rows_and_keep_ties_and_cutoff_correct(self):
        rows = [{"aspect_id": str(i), "label": f"主题{i}", "explicit_positive_entities": 100-i,
                 "explicit_negative_entities": 50-i} for i in range(9)]
        rows += [{"aspect_id": "storage", "label": "收纳", "explicit_positive_entities": 17, "explicit_negative_entities": 5},
                 {"aspect_id": "assembly", "label": "装配", "explicit_positive_entities": 18, "explicit_negative_entities": 5}]
        result = build_report_ranking(rows, 10, ["S4"])
        self.assertEqual(result["rankings"][0]["rows"][-1]["label"], "装配")
        self.assertIn("末位还有同数项目", result["answer"])
        self.assertNotIn("已核对原文", result["answer"])
        rows[1]["explicit_positive_entities"] = 100
        self.assertEqual([r["rank"] for r in build_report_ranking(rows, 3, ["S4"])["rankings"][0]["rows"]], [1, 1, 3])
        rows[0]["explicit_positive_entities"] = True
        self.assertIsNone(build_report_ranking(rows, 10, ["S4"]))

    def test_raw_review_correction_and_priority_questions_do_not_use_report_shortcut(self):
        for question in ("分别给我排名前10的pros and cons", "优缺点各前10和依据", "根据报告给我优缺点各前10"):
            self.assertEqual(report_ranking_requested(question), 0)
        for suffix in ("，结合原文纠错", "，按严重程度", "，解释发生场景", "，给出改进建议", "，说具体哪里好哪里差"):
            self.assertEqual(report_ranking_requested("只列报告优缺点各前10" + suffix), 0)

    def test_explicit_report_only_number_requests_can_use_shortcut(self):
        for question in ("只列报告已有的优缺点各前10主题和数量", "报告优缺点各前10，只要排名和数字",
                         "只给我报告的pros and cons前10榜单，不用解释", "Only list the report pros and cons top 10 counts"):
            with self.subTest(question=question):
                self.assertEqual(report_ranking_requested(question), 10)

    def test_ranking_context_preserves_counts_cutoff_and_original_source_identity(self):
        rows = [{"aspect_id": str(i), "label": f"主题{i}", "explicit_positive_entities": 3,
                 "explicit_negative_entities": 1, "high_star_entities": 99} for i in range(11)]
        action = build_report_ranking(rows, 10, ["S4", "S5"])
        context = report_ranking_context(action)
        self.assertEqual(context["source_ids"], ["S4", "S5"])
        self.assertEqual([len(r["rows"]) for r in context["rankings"]], [10, 10])
        self.assertTrue(all(row["count"] == 3 for row in context["rankings"][0]["rows"]))
        self.assertEqual(len(context["rankings"][0]["cutoff_ties"]), 1)
        self.assertIn("不是星级", context["basis"])
        self.assertIn("主题名称不能代替评价", context["instruction"])
        context["rankings"][0]["rows"][0]["count"] = 1000
        self.assertEqual(action["rankings"][0]["rows"][0]["count"], 3)
