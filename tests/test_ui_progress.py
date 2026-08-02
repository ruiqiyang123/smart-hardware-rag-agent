import unittest

from agent.ui_progress import (
    append_processing_turn,
    project_customer_phases,
    project_workbench_events,
    replace_processing_answer,
)


class UiProgressProjectionTest(unittest.TestCase):
    def test_customer_projection_is_short_deduplicated_and_whitelisted(self):
        events = [
            {"event_type": "entry_checked", "summary": "内部摘要"},
            {"event_type": "triage_completed", "summary": "内部摘要"},
            {"event_type": "triage_completed", "summary": "重复"},
            {"event_type": "node_failure", "summary": "不应展示"},
            {"event_type": "unknown", "summary": "不应展示"},
            {"event_type": "ticket.closed", "summary": "30 分钟无新消息"},
        ]

        self.assertEqual(
            project_customer_phases(events),
            [
                {"event_type": "entry_checked", "label": "🛡️ 安全检查完成"},
                {"event_type": "triage_completed", "label": "🧭 问题分诊完成"},
                {"event_type": "ticket.closed", "label": "⏱️ 会话已结束"},
            ],
        )

    def test_workbench_projection_excludes_metadata_and_bounds_text(self):
        events = [
            {
                "event_type": "diagnosis_completed",
                "node_name": "diagnosis",
                "summary": "证据诊断完成",
                "from_status": "triaging",
                "to_status": "diagnosing",
                "metadata": {"tool_args": "secret", "raw": "不要展示"},
                "metadata_json": "{\"raw\": \"不要展示\"}",
            },
            {
                "event_type": "node_failure",
                "node_name": "diagnosis",
                "summary": "x" * 1000,
                "from_status": "diagnosing",
                "to_status": "escalated",
            },
        ]

        projected = project_workbench_events(events)
        self.assertEqual(projected[0]["event_type"], "diagnosis_completed")
        self.assertNotIn("metadata", projected[0])
        self.assertNotIn("metadata_json", projected[0])
        self.assertNotIn("tool_args", str(projected))
        self.assertLessEqual(len(projected[1]["summary"]), 240)

    def test_processing_turn_keeps_user_input_and_replaces_placeholder(self):
        messages = [{"role": "assistant", "content": "你好"}]
        append_processing_turn(messages, "电脑识别不到设备", "正在安全处理…")
        self.assertEqual(
            messages[-2:],
            [
                {"role": "user", "content": "电脑识别不到设备"},
                {"role": "assistant", "content": "正在安全处理…"},
            ],
        )
        replace_processing_answer(messages, "已进入人工审核")
        self.assertEqual(messages[-1], {"role": "assistant", "content": "已进入人工审核"})

    def test_processing_helpers_reject_extra_message_fields(self):
        messages = [{"role": "user", "content": "问题", "ui_id": "secret"}]
        with self.assertRaises(ValueError):
            append_processing_turn(messages, "新的问题", "处理中")


if __name__ == "__main__":
    unittest.main()
