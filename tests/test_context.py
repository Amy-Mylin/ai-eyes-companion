"""检查当前图像、过去描述和已听回答的边界，不替代真实模型识物。"""

import unittest

from eyes_companion.context import build_messages, text_frame_needed, visual_task


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.history = [
            {"role": "user", "content": "看看这个", "visual": True, "turn_id": "old"},
            {"role": "assistant", "content": "这是白色充电器。", "visual": True,
             "turn_id": "old", "frame_id": "old-frame", "at": "past"},
            {"role": "user", "content": "现在这个是什么", "visual": True, "turn_id": "new"},
        ]
        self.frame = {"data": "current-jpeg-only", "frame_id": "new-frame", "at": "now",
                      "width": 960, "height": 720}

    def test_new_identification_does_not_receive_old_visual_answer(self):
        messages = build_messages(self.history, "现在这个是什么", [], self.frame)
        self.assertNotIn("白色充电器", str(messages))
        self.assertEqual(messages[-1]["images"], ["current-jpeg-only"])
        self.assertNotIn("old-frame", str(messages))

    def test_comparison_retains_explicitly_past_description(self):
        messages = build_messages(self.history, "和刚才的有什么区别", [], self.frame)
        self.assertIn("白色充电器", messages[0]["content"])
        self.assertIn("未经独立核实", messages[0]["content"])
        self.assertIn("old-frame", messages[0]["content"])
        self.assertEqual(sum("images" in item for item in messages), 1)

    def test_visual_followup_keeps_referent_as_background_not_assistant_fact(self):
        messages = build_messages(self.history, "它是什么颜色", [], self.frame)
        self.assertIn("白色充电器", messages[0]["content"])
        self.assertIn("不证明当前外观", messages[0]["content"])
        self.assertFalse(any(item["role"] == "assistant" for item in messages))

    def test_chat_recall_keeps_user_correction_without_claiming_vision(self):
        history = [{"role": "user", "content": "杯子蓝色"},
                   {"role": "user", "content": "不对，是绿色"},
                   {"role": "user", "content": "我刚才改成什么颜色"}]
        messages = build_messages(history, history[-1]["content"], [])
        self.assertEqual(messages[-2]["content"], "不对，是绿色")
        self.assertIn("本轮没有图片", messages[0]["content"])
        self.assertFalse(any("images" in item for item in messages))

    def test_no_image_does_not_promote_old_visual_answer(self):
        messages = build_messages(self.history, "刚才看到了什么", [])
        self.assertIn("过去画面的回答", messages[-2]["content"])

    def test_reading_dates_does_not_use_generic_identification_prompt(self):
        self.assertEqual(visual_task("这个包装生产日期是什么"), "read_text")
        messages = build_messages(self.history, "生产日期是什么", [{"content": "我喜欢听详细故事"}], self.frame)
        self.assertIn("没有明确字段", messages[0]["content"])
        self.assertNotIn("白色充电器", str(messages))
        self.assertNotIn("详细故事", str(messages))
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[-1]["images"], ["current-jpeg-only"])

    def test_explicit_detail_or_scene_question_is_not_limited_to_identification(self):
        self.assertEqual(visual_task("详细告诉我这个是什么"), "describe_current")
        self.assertEqual(visual_task("前面有什么"), "describe_current")
        self.assertEqual(visual_task("这个是什么，放在哪里"), "describe_current")

    def test_text_comparison_and_recall_keep_past_source_without_lowering_frame_quality(self):
        for question in ("这个品牌和刚才那个一样吗", "刚才读出的生产日期是什么"):
            with self.subTest(question=question):
                history = self.history[:-1] + [{"role": "user", "content": question}]
                messages = build_messages(history, question, [], self.frame)
                self.assertEqual(visual_task(question), "compare_or_recall")
                self.assertTrue(text_frame_needed(question))
                self.assertIn("白色充电器", messages[0]["content"])
                self.assertIn("未经独立核实", messages[0]["content"])
                self.assertEqual(messages[-1]["images"], ["current-jpeg-only"])


if __name__ == "__main__":
    unittest.main()
