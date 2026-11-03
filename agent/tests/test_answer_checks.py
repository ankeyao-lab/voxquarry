import unittest

from agent.answer_checks import delivery_feedback, dual_ranking_size, ranked_item_counts, simple_count_question, style_feedback, percentage_relation_conflicts


class AnswerDeliveryChecks(unittest.TestCase):
    def test_percentage_relations_detect_reversed_values_in_labeled_chains(self):
        self.assertEqual(percentage_relation_conflicts("故障 12.5%＞续航 8.2%＞重量 9.3%"), ["8.2% > 9.3%"])
        self.assertEqual(percentage_relation_conflicts("续航 **6.2%**（样本内）＞重量/平衡 **7.7%**"), ["6.2% > 7.7%"])
        self.assertEqual(percentage_relation_conflicts("A 2.0% <= B 1.9%；C 3% = D 4%"), ["2.0% <= 1.9%", "3% = 4%"])

    def test_percentage_relations_keep_valid_math_and_do_not_grade_quoted_code_or_priorities(self):
        for text in ("A 12% > B 8% > C 3%", "A 1% ≤ B 1%", "A -2% < B 3%", "A 3.0%＝B 3%",
                     "> 原文错误：6% > 8%", "示例 `6% > 8%`", "```text\n6% > 8%\n```",
                     "改善优先级（根据影响程度）：A 6% > B 8%", "A 6%\nB 8%", "评分 4.2 > 3.1",
                     "范围 A 20-80% > B 70%", "范围 A 20–80% > B 70%"):
            with self.subTest(text=text):
                self.assertEqual(percentage_relation_conflicts(text), [])

    def test_percentage_relations_allow_quoted_and_explicitly_refuted_mistakes(self):
        for text in ('原文“6% > 8%”写反，应为 6% < 8%。', '原文 "6% > 8%" 写反。',
                     "6% > 8% 不成立，应为 6% < 8%。"):
            self.assertEqual(percentage_relation_conflicts(text), [])
        self.assertEqual(percentage_relation_conflicts("6% > 8% 不成立；另一个结论 3% > 5%"),
                         ["3% > 5%"])

    def test_fact_question_is_distinct_from_interpretation_and_comparison(self):
        self.assertTrue(simple_count_question("这份报告有多少评论？"))
        self.assertTrue(simple_count_question("How many reviews are in this report?"))
        for question in ("负面评论有多少？", "分别有哪些优缺点？", "为什么评论里这么多自走问题？", "多少评论提到了这个问题，比较它们的原因"):
            self.assertFalse(simple_count_question(question))

    def test_dual_top_n_requires_two_independent_rankings(self):
        self.assertEqual(dual_ranking_size("pros & cons 分别有哪些？分别给我前 10 的排名和依据。"), 10)
        self.assertEqual(dual_ranking_size("优缺点各前十"), 10)
        self.assertEqual(dual_ranking_size("利弊 top 3"), 3)
        self.assertIsNone(dual_ranking_size("主要负面是什么"))
        self.assertIsNone(dual_ranking_size("优点前十"))
        self.assertTrue(delivery_feedback("优缺点各前十", "下面是两份榜单，按提及人数排序。", review_records=1))

    def test_table_and_numbered_list_preserve_separate_counts(self):
        positive = "## 优点\n|排名|优点|人数|\n|---|---|---|\n" + "\n".join(f"|{i}|好处{i}|{10-i}|" for i in range(1, 4))
        negative = "## 缺点\n" + "\n".join(f"{i}. 问题{i}" for i in range(1, 4))
        self.assertEqual(ranked_item_counts(positive + "\n" + negative), (3, 3))
        self.assertEqual(delivery_feedback("优缺点 top 3", positive + "\n" + negative, review_records=1), [])
        self.assertTrue(delivery_feedback("优缺点 top 4", positive + "\n" + negative, review_records=1))
        self.assertTrue(delivery_feedback("优缺点 top 3", positive + "\n" + negative, review_records=0))
        self.assertEqual(ranked_item_counts((positive + "\n" + negative).replace("## 优点", "## 一、优点").replace("## 缺点", "## 2. 缺点")), (3, 3))

    def test_unrelated_numbered_recommendations_do_not_fill_a_missing_ranking(self):
        answer = "## 优点\n1. 动力好\n## 缺点\n1. 转弯费力\n## 下一步建议\n1. 验证\n2. 测试\n3. 访谈"
        self.assertEqual(ranked_item_counts(answer), (1, 1))

    def test_gfm_tables_with_optional_outer_pipes_preserve_both_rankings(self):
        def table(label, left_pipe, right_pipe):
            rows = [f"排名 | {label} | 人数", "--- | --- | ---"]
            rows += [f"{i} | 项目{i} | {10-i}" for i in range(1, 4)]
            return "\n".join(left_pipe + row + right_pipe for row in rows)
        for left, right in (("", ""), ("|", ""), ("", "|"), ("|", "|")):
            with self.subTest(left=left, right=right):
                answer = "## 优点\n" + table("优点", left, right) + "\n## 缺点\n" + table("缺点", left, right)
                self.assertEqual(ranked_item_counts(answer), (3, 3))
                self.assertEqual(delivery_feedback("优缺点 top 3", answer, review_records=1), [])
                self.assertTrue(delivery_feedback("优缺点 top 4", answer, review_records=1))
                self.assertTrue(delivery_feedback("优缺点 top 3", answer, review_records=0))

    def test_top_n_and_bold_numbered_titles_count_rows_without_counting_unrelated_explanations(self):
        for positive, negative in (("## Top 3 Pros", "## Top 3 Cons"),
                                   ("**一、优点**", "**二、缺点**"),
                                   ("**1. Pros**", "**2. Cons**")):
            with self.subTest(positive=positive):
                answer = positive + "\n1. 好处一\n2. 好处二\n3. 好处三\n" + negative + "\n1. 问题一"
                for explanation in ("## 下一步建议\n1. 验证\n2. 测试\n3. 访谈",
                                    "**三、下一步建议**\n1. 验证\n2. 测试\n3. 访谈"):
                    candidate = answer + "\n" + explanation
                    self.assertEqual(ranked_item_counts(candidate), (3, 1))
                    self.assertTrue(delivery_feedback("优缺点 top 3", candidate, review_records=1))

    def test_bold_numbered_items_and_bold_rank_cells_still_count_as_items(self):
        answer = "## 优点\n**1. 好处一**\n**2. 好处二**\n## 缺点\n排名 | 缺点 | 人数\n--- | --- | ---\n**1** | 问题一 | **2**\n**2** | 问题二 | **1**"
        self.assertEqual(ranked_item_counts(answer), (2, 2))

    def test_isolated_draft_with_chinese_bracket_titles_keeps_both_complete_rankings(self):
        # Shape captured in an isolated diagnostic, with synthetic claims/counts.
        # This does not establish how any earlier unsaved live draft was formatted.
        positive = "【优点 Top 10】（按显式正面提及条数）\n" + "\n".join(
            f"{i}. 合成优点{i}：正面提及{50-i}条，按保存统计排序。[S2]" for i in range(1, 11)
        )
        negative = "【缺点 Top 10】（按显式负面提及条数）\n" + "\n".join(
            f"{i}. 合成缺点{i}：负面提及{30-i}条，按保存统计排序。[S2]" for i in range(1, 11)
        )
        answer = "以下按报告保存的提及数排序，不能代替全量语义复核。\n\n" + positive + "\n\n" + negative
        self.assertEqual(ranked_item_counts(answer), (10, 10))
        self.assertEqual(delivery_feedback("pros & cons 分别有哪些？分别给我前 10 的排名和依据。", answer, review_records=1), [])
        feedback = delivery_feedback("优缺点各前十", answer, review_records=0)
        self.assertEqual(len(feedback), 1)
        self.assertIn("还没有读取本轮完整评论", feedback[0])

    def test_bracket_titles_are_independent_lines_not_mentions_in_body_text(self):
        for positive, negative in (("这条评论提到【优点】", "另一条评论提到【缺点】"),
                                   ("【优点】是这条评论的一个标签。", "【缺点】指下面这些词。"),
                                   ("这里的【优点 Top 10】不是独立标题", "这里的【缺点 Top 10】也不是标题")):
            with self.subTest(positive=positive):
                answer = positive + "\n1. 普通说明\n" + negative + "\n1. 普通说明"
                self.assertEqual(ranked_item_counts(answer), (0, 0))
                self.assertTrue(delivery_feedback("优缺点 top 1", answer, review_records=1))

    def test_other_chinese_bracket_headings_end_rankings_before_numbered_explanations(self):
        answer = "【优点】\n1. 好处\n【缺点】\n1. 问题\n【补充说明】（不是排名）\n1. 验证\n2. 访谈\n3. 建议"
        self.assertEqual(ranked_item_counts(answer), (1, 1))
        self.assertTrue(delivery_feedback("优缺点 top 3", answer, review_records=1))

    def test_isolated_draft_with_plain_chinese_numbered_pros_cons_titles_is_counted(self):
        # A second isolated draft used natural Chinese section numbering, not Markdown headings.
        positive = "一、Pros 前 10（按显式正向提及率降序）[S2]\n" + "\n".join(
            f"{i}. 合成优点{i}：正向占比{90-i}%，按报告统计排序。[S2]" for i in range(1, 11)
        )
        negative = "二、Cons 前 10（按显式负向提及率降序）[S2]\n" + "\n".join(
            f"{i}. 合成缺点{i}：负向占比{70-i}%，按报告统计排序。[S2]" for i in range(1, 11)
        )
        answer = positive + "\n\n" + negative + "\n三、下一步建议\n1. 验证\n2. 访谈"
        self.assertEqual(ranked_item_counts(answer), (10, 10))
        self.assertEqual(delivery_feedback("优缺点各前十", answer, review_records=1), [])
        self.assertEqual(ranked_item_counts("一、优点前3\n1. 优点1\n二、缺点前3\n1. 缺点1\n三、补充说明\n1. 解释\n2. 解释"), (1, 1))

    def test_ordinary_arabic_numbered_items_do_not_become_ranking_headings(self):
        self.assertEqual(ranked_item_counts("1. 优点1\n2. 普通说明\n3. 缺点1\n4. 普通说明"), (0, 0))
        self.assertEqual(ranked_item_counts("## 优点\n1. 优点1\n2. 优点2\n## 缺点\n1. 缺点1\n2. 缺点2"), (2, 2))

    def test_internal_language_and_approximate_exact_count_need_revision(self):
        answer = "约200条去重评论实体，has_more=false，见S4。"
        self.assertTrue(style_feedback("有多少评论？", answer))
        self.assertEqual(delivery_feedback("有多少评论？", answer, review_records=0), [])
        self.assertEqual(style_feedback("有多少评论？", "报告有200条评论，其中80条有文字。\n\n[S1] [S2]"), [])
        self.assertEqual(style_feedback("请解释你说的统计口径", "口径指统计时使用的计数方式。[S1]"), [])

    def test_style_feedback_names_the_problem_and_keeps_requested_detail(self):
        feedback = style_feedback("主要问题是什么", "显式负面反馈" + "体验问题。" * 150)
        self.assertTrue(any("显式负" in item for item in feedback))
        self.assertTrue(any("保留所选例子的对象、条件、时间和用户猜测" in item for item in feedback))
        self.assertEqual(style_feedback("请详细逐条展开", "体验问题。" * 150), [])


if __name__ == "__main__":
    unittest.main()
