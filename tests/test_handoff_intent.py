import unittest

from agent.orchestration.handoff_intent import is_human_handoff_request


class HumanHandoffIntentTest(unittest.TestCase):
    def test_recognizes_direct_handoff_requests(self):
        for text in (
            "转人工",
            "我想找人工客服",
            "请帮我联系真人客服",
            "能不能换一位人工处理？",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_human_handoff_request(text))

    def test_does_not_trigger_on_negation_or_unrelated_questions(self):
        for text in (
            "先不要转人工",
            "不用联系真人客服",
            "蓝牙为什么连不上？",
            "人工智能是怎么工作的？",
            None,
        ):
            with self.subTest(text=text):
                self.assertFalse(is_human_handoff_request(text))


if __name__ == "__main__":
    unittest.main()
