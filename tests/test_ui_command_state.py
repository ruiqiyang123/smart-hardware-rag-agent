import unittest

from utils.ui_command_state import (
    ACTION_ID_SESSION_PREFIX,
    ACTION_PAYLOAD_SESSION_PREFIX,
    REQUEST_HISTORY_ID_SESSION_KEY,
    REQUEST_HISTORY_SESSION_KEY,
    REQUEST_ID_SESSION_KEY,
    REQUEST_PAYLOAD_SESSION_KEY,
    clear_frozen_action,
    clear_frozen_actions,
    clear_frozen_request,
    get_or_freeze_action_command,
    get_or_freeze_request_command,
)


class UICommandStateTest(unittest.TestCase):
    def test_request_retry_reuses_safe_payload_and_pre_request_history(self):
        state = {REQUEST_ID_SESSION_KEY: "request-a"}
        messages = [{"role": "user", "content": "此前蓝牙连接失败"}]
        safe_payload = ("submit", "已脱敏输入")

        first, first_history, restoring = get_or_freeze_request_command(
            state,
            "request-a",
            messages,
            lambda: safe_payload,
        )
        messages.append({"role": "assistant", "content": "安全失败提示"})
        calls = 0

        def changed_prompt():
            nonlocal calls
            calls += 1
            return ("submit", "改变后的输入")

        retry, retry_history, restoring_retry = get_or_freeze_request_command(
            state,
            "request-a",
            messages,
            changed_prompt,
        )

        self.assertEqual(first, retry)
        self.assertEqual(first_history, retry_history)
        self.assertFalse(restoring)
        self.assertTrue(restoring_retry)
        self.assertEqual(calls, 0)
        self.assertNotIn("安全失败提示", repr(retry_history))

    def test_request_snapshot_contains_no_raw_secret_and_clear_removes_payload(self):
        raw_secret = (
            "abandon ability able about above absent absorb abstract absurd abuse "
            "access accident"
        )
        state = {REQUEST_ID_SESSION_KEY: "request-secret"}
        get_or_freeze_request_command(
            state,
            "request-secret",
            [],
            lambda: ("submit", "[REDACTED_SECRET]"),
        )

        self.assertNotIn(raw_secret, repr(state))
        self.assertIn(REQUEST_PAYLOAD_SESSION_KEY, state)
        clear_frozen_request(state)
        self.assertNotIn(REQUEST_ID_SESSION_KEY, state)
        self.assertNotIn(REQUEST_HISTORY_ID_SESSION_KEY, state)
        self.assertNotIn(REQUEST_HISTORY_SESSION_KEY, state)
        self.assertNotIn(REQUEST_PAYLOAD_SESSION_KEY, state)

    def test_unsafe_history_is_rejected_before_payload_is_frozen(self):
        mnemonic = (
            "abandon ability able about above absent absorb abstract absurd abuse "
            "access accident"
        )
        state = {REQUEST_ID_SESSION_KEY: "request-secret"}
        prepared = False

        def prepare():
            nonlocal prepared
            prepared = True
            return "safe"

        with self.assertRaises(ValueError):
            get_or_freeze_request_command(
                state,
                "request-secret",
                [{"role": "user", "content": mnemonic}],
                prepare,
            )

        self.assertFalse(prepared)
        self.assertNotIn(REQUEST_HISTORY_ID_SESSION_KEY, state)
        self.assertNotIn(REQUEST_PAYLOAD_SESSION_KEY, state)

    def test_action_retry_replays_original_payload_and_clear_removes_both_keys(self):
        action_key = f"{ACTION_ID_SESSION_PREFIX}KG-1:edit_send"
        action_id = "action-a"
        state = {action_key: action_id}
        first, restoring = get_or_freeze_action_command(
            state,
            action_key,
            action_id,
            lambda: ("edit_send", "原安全答复"),
        )
        calls = 0

        def changed_edit():
            nonlocal calls
            calls += 1
            return ("edit_send", "改变后的答复")

        retry, restoring_retry = get_or_freeze_action_command(
            state,
            action_key,
            action_id,
            changed_edit,
        )

        self.assertEqual(first, retry)
        self.assertFalse(restoring)
        self.assertTrue(restoring_retry)
        self.assertEqual(calls, 0)
        clear_frozen_action(state, action_key)
        self.assertNotIn(action_key, state)
        self.assertFalse(
            any(key.startswith(ACTION_PAYLOAD_SESSION_PREFIX) for key in state)
        )

    def test_clear_all_actions_removes_id_and_payload_snapshots(self):
        state = {
            f"{ACTION_ID_SESSION_PREFIX}KG-1:ask_user": "action-a",
            f"{ACTION_PAYLOAD_SESSION_PREFIX}KG-1:ask_user": (
                "action-a",
                ("ask_user", ("device_model",)),
            ),
            "unrelated": "keep",
        }

        clear_frozen_actions(state)

        self.assertEqual(state, {"unrelated": "keep"})


if __name__ == "__main__":
    unittest.main()
