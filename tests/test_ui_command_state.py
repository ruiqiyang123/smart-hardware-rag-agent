import unittest

from utils.ui_command_state import (
    REQUEST_HISTORY_ID_SESSION_KEY,
    REQUEST_HISTORY_SESSION_KEY,
    REQUEST_ID_SESSION_KEY,
    clear_frozen_request,
    get_or_freeze_safe_history,
)


class UICommandStateTest(unittest.TestCase):
    def test_retries_reuse_pre_request_history_despite_failure_notice(self):
        state = {REQUEST_ID_SESSION_KEY: "request-a"}
        messages = [{"role": "user", "content": "此前蓝牙连接失败"}]

        first = get_or_freeze_safe_history(state, "request-a", messages)
        messages.append({"role": "assistant", "content": "安全失败提示"})
        retry = get_or_freeze_safe_history(state, "request-a", messages)

        self.assertEqual(first, retry)
        self.assertEqual(len(retry), 1)
        self.assertNotIn("安全失败提示", repr(retry))

        first[0]["content"] = "调用方修改"
        self.assertEqual(
            get_or_freeze_safe_history(state, "request-a", messages), retry
        )

    def test_new_request_gets_new_snapshot_and_clear_removes_all_retry_state(self):
        state = {REQUEST_ID_SESSION_KEY: "request-a"}
        messages = [{"role": "assistant", "content": "第一轮回复"}]
        get_or_freeze_safe_history(state, "request-a", messages)
        messages.append({"role": "user", "content": "第二轮问题"})

        second = get_or_freeze_safe_history(state, "request-b", messages)

        self.assertEqual(second, messages)
        self.assertEqual(state[REQUEST_HISTORY_ID_SESSION_KEY], "request-b")
        clear_frozen_request(state)
        self.assertNotIn(REQUEST_ID_SESSION_KEY, state)
        self.assertNotIn(REQUEST_HISTORY_ID_SESSION_KEY, state)
        self.assertNotIn(REQUEST_HISTORY_SESSION_KEY, state)

    def test_unsafe_history_is_rejected_without_freezing(self):
        mnemonic = (
            "abandon ability able about above absent absorb abstract absurd abuse "
            "access accident"
        )
        state = {REQUEST_ID_SESSION_KEY: "request-secret"}

        with self.assertRaises(ValueError):
            get_or_freeze_safe_history(
                state,
                "request-secret",
                [{"role": "user", "content": mnemonic}],
            )

        self.assertNotIn(REQUEST_HISTORY_ID_SESSION_KEY, state)
        self.assertNotIn(REQUEST_HISTORY_SESSION_KEY, state)


if __name__ == "__main__":
    unittest.main()
