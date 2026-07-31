import unittest

from agent.orchestration.resolution_intent import is_resolution_confirmation


class ResolutionIntentTest(unittest.TestCase):
    def test_recognizes_short_explicit_confirmations(self):
        for value in (
            "解决了",
            "谢谢",
            "谢谢，已经解决了。",
            "可以了",
            "没问题了",
            "OK",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_resolution_confirmation(value))

    def test_continuation_and_questions_never_close_ticket(self):
        for value in (
            "谢谢，但是还是连不上",
            "可以再问一个问题吗？",
            "好的，我还有个问题",
            "问题解决了吗？",
            "不行，还是没解决",
            "我该怎么办",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_resolution_confirmation(value))

    def test_unknown_or_invalid_values_are_conservative(self):
        for value in ("", "知道了", "蓝牙", None, 123, "好" * 81):
            with self.subTest(value=value):
                self.assertFalse(is_resolution_confirmation(value))


if __name__ == "__main__":
    unittest.main()
