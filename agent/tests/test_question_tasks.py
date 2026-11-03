import unittest

from agent.contracts import AgentError
from agent.question_tasks import explicit_requirements, merge_plan, retained_coverage


class QuestionTasksTests(unittest.TestCase):
    def test_explicit_facets_and_original_question_cannot_disappear(self):
        question = "分别说 pros 和 cons，在哪些场景发生，排序依据是什么？"
        tasks = merge_plan(question, [])
        self.assertEqual(tasks[0]["question"], question)
        self.assertEqual({t["task_id"] for t in tasks}, {"Q1", "Q2", "Q3", "Q4", "Q5"})
        with self.assertRaises(AgentError):
            merge_plan(question, [{"question": "总体故障率是多少", "anchor": "总体故障率"}])

    def test_excluded_facets_and_time_period_do_not_create_new_tasks(self):
        self.assertNotIn("Q2", {t["task_id"] for t in explicit_requirements("不要优点，只说缺点")})
        self.assertNotIn("Q5", {t["task_id"] for t in explicit_requirements("使用前2年是否故障")})
        self.assertIn("Q5", {t["task_id"] for t in explicit_requirements("给出前10项优点")})
        self.assertEqual({t["task_id"] for t in explicit_requirements("分别给我排名前10的pros和cons")},
                         {"Q1", "Q2", "Q3", "Q5"})

    def test_review_must_link_each_completed_task_to_retained_prose(self):
        tasks = [{"task_id": "Q1", "question": "优点"}, {"task_id": "Q2", "question": "缺点"}]
        checked = {"task_checks": [{"task_id": t["task_id"], "status": "answered", "reason": "", "statement_ids": [sid]}
                                  for t, sid in zip(tasks, ["P1", "P2"])]}
        kept = [{"statement_id": "P1", "task_ids": ["Q1"]}]
        coverage = retained_coverage(tasks, checked, kept)
        self.assertEqual([t["status"] for t in coverage], ["answered", "unanswered"])
        kept[0]["task_ids"] = ["Q2"]
        self.assertEqual(retained_coverage(tasks, checked, kept)[0]["status"], "answered")

    def test_reviewed_overall_and_scenario_coverage_can_span_writer_specialized_tags(self):
        # Mirrors the real nine-paragraph answer: comparison and hypothesis
        # paragraphs carry their own writer tags but also answer the whole ask.
        tasks = [{"task_id": tid, "question": title} for tid, title in (
            ("Q1", "分析全部问题"), ("Q4", "说明操作场景"), ("Q7", "统计数量"),
            ("Q8", "旧机器比较"), ("Q9", "假设与不确定判断"))]
        kept = [{"statement_id": "P1", "task_ids": ["Q1", "Q7"]}]
        kept.extend({"statement_id": f"P{i}", "task_ids": ["Q1", "Q4"]} for i in range(2, 8))
        kept.extend([{"statement_id": "P8", "task_ids": ["Q8"]}, {"statement_id": "P9", "task_ids": ["Q9"]}])
        ids = {"Q1": [f"P{i}" for i in range(1, 10)], "Q4": [f"P{i}" for i in range(2, 8)] + ["P9"],
               "Q7": ["P1"], "Q8": ["P8"], "Q9": ["P9"]}
        review = {"task_checks": [{"task_id": task["task_id"], "status": "answered",
            "statement_ids": ids[task["task_id"]], "reason": ""} for task in tasks]}
        result = retained_coverage(tasks, review, kept)
        self.assertTrue(all(task["status"] == "answered" for task in result))
        self.assertEqual(result[0]["statement_ids"], ids["Q1"])
        removed = retained_coverage(tasks, review, kept[:-1])
        self.assertEqual({task["task_id"] for task in removed if task["status"] == "answered"}, {"Q7", "Q8"})

    def test_coverage_requires_explicit_approval_and_unique_retained_paragraph_ids(self):
        tasks = [{"task_id": "Q1", "question": "问题"}]
        kept = [{"statement_id": "P1", "task_ids": ["Q8"]}]
        for ids in ([], ["P404"], ["P1", "P404"], ["P1", "P1"], "P1", [None], [{"id": "P1"}]):
            with self.subTest(ids=ids):
                review = {"task_checks": [{"task_id": "Q1", "status": "answered", "statement_ids": ids, "reason": ""}]}
                result = retained_coverage(tasks, review, kept)[0]
                self.assertEqual(result["status"], "unanswered")
                self.assertEqual(result["statement_ids"], [])
        review = {"task_checks": [{"task_id": "Q1", "status": "unanswered", "statement_ids": ["P1"], "reason": "场景没有回答。"}]}
        result = retained_coverage(tasks, review, kept)[0]
        self.assertEqual(result["status"], "unanswered")
        self.assertEqual(result["reason"], "场景没有回答。")

    def test_classification_and_cross_question_keep_requested_statistics_and_conditions(self):
        tasks = explicit_requirements("分别统计当前产品实际操控问题、旧机器比较、假设和不确定判断，并说明具体问题发生在什么操作下。")
        self.assertTrue({"Q1", "Q4", "Q7", "Q8", "Q9"}.issubset({row["task_id"] for row in tasks}))
        self.assertIn("Q7", {row["task_id"] for row in explicit_requirements("区分实际统计和你的判断")})
        self.assertNotIn("Q7", {row["task_id"] for row in explicit_requirements("不需要统计，只看实际体验")})
