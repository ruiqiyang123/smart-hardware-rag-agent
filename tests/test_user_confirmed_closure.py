import tempfile
import unittest
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from agent.orchestration.graph import build_support_graph
from agent.orchestration.runtime import SupportOrchestrator
from agent.policies.security import IngressGuard, PolicyGuard
from agent.security.trusted_sources import TrustedSourcePolicy
from database.ticket_db import TicketRepository


POLICY = {
    "policy_version": "2026-07-31.user-confirmed-closure",
    "official_domains": ["support.example.test"],
    "critical_response_template_zh": "固定安全提示",
}


def triage(_state):
    return {
        "intent": "troubleshoot",
        "category": "bluetooth_connection",
        "priority": "P2",
        "risk_level": "low",
        "risk_flags": [],
        "clarity": "clear",
        "clarification_question": "",
        "clarification_options": [],
        "missing_fields": [],
        "suggested_route": "diagnose",
        "summary": "低风险蓝牙连接问题",
    }


def diagnosis(_state):
    return {
        "outcome": "draft",
        "diagnosis_summary": "建议删除旧配对并重新连接。",
        "recommended_actions": [
            {
                "action_code": "generic_troubleshooting",
                "text": "删除旧配对记录后重新连接。",
                "evidence_refs": ["kb-1"],
            }
        ],
        "evidence_refs": ["kb-1"],
        "evidence": [
            {
                "evidence_id": "kb-1",
                "kind": "knowledge",
                "content": "重新配对前需要删除旧配对记录。",
                "source_title": "官方蓝牙排查",
                "source_url": "https://support.example.test/bluetooth",
            }
        ],
        "citations": [
            {
                "source_id": "kb-1",
                "source_title": "官方蓝牙排查",
                "source_url": "https://support.example.test/bluetooth",
            }
        ],
        "draft_answer": "请删除手机和应用内的旧配对记录，然后重新连接。",
        "remaining_unknowns": [],
        "tool_errors": [],
    }


def review(_state):
    return {
        "review_decision": "approve",
        "review_reasons": ["passed"],
        "review_issues": [],
        "required_changes": [],
    }


class UserConfirmedClosureTest(unittest.TestCase):
    @staticmethod
    def _runtime(directory: str) -> SupportOrchestrator:
        trusted = TrustedSourcePolicy(["support.example.test"], [])
        guard = PolicyGuard(POLICY, trusted_source_policy=trusted)
        graph = build_support_graph(
            triage_node=triage,
            diagnosis_node=diagnosis,
            review_node=review,
            policy_guard=guard,
            checkpointer=MemorySaver(),
        )
        return SupportOrchestrator(
            repository=TicketRepository(str(Path(directory) / "tickets.db")),
            graph=graph,
            ingress_guard=IngressGuard(POLICY),
            recursion_limit=16,
            lease_seconds=130,
            graph_timeout_seconds=120,
            trusted_source_policy=trusted,
            final_policy_guard=guard,
        )

    def test_follow_up_stays_open_and_explicit_confirmation_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime(directory)

            first = runtime.submit(
                "蓝牙无法连接手机，怎么排查？",
                "1001",
                request_id="first-question",
            )
            self.assertEqual(first.status, "pending_user")
            self.assertEqual(first.waiting_reason, "resolution_confirmation")
            self.assertIsNotNone(
                runtime.repository.get_ticket(first.ticket_id)["idle_expires_at"]
            )

            follow_up = runtime.resume_user(
                first.ticket_id,
                "谢谢，但是还是不行",
                request_id="follow-up",
            )
            self.assertEqual(follow_up.ticket_id, first.ticket_id)
            self.assertEqual(follow_up.status, "pending_user")
            self.assertEqual(
                follow_up.waiting_reason,
                "resolution_confirmation",
            )

            resolved = runtime.resume_user(
                first.ticket_id,
                "谢谢，已经解决了",
                request_id="confirm-resolution",
            )
            self.assertEqual(resolved.ticket_id, first.ticket_id)
            self.assertEqual(resolved.status, "resolved")
            self.assertEqual(resolved.waiting_reason, "")
            self.assertTrue(
                any(
                    event["event_type"] == "user_confirmed_resolution"
                    for event in resolved.events
                )
            )

    def test_three_cross_topic_ai_answers_unlock_customer_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime(directory)
            result = runtime.submit(
                "蓝牙无法连接手机，怎么排查？",
                "1001",
                request_id="topic-one",
            )
            for request_id, question in (
                ("topic-two", "电脑识别不到设备，怎么排查？"),
                ("topic-three", "固件升级前需要检查什么？"),
            ):
                result = runtime.resume_user(
                    result.ticket_id, question, request_id=request_id
                )
                self.assertEqual(result.status, "pending_user")

            allowed, attempts = runtime.can_request_human(result.ticket_id)
            self.assertTrue(allowed)
            self.assertEqual(attempts, 3)
            escalated = runtime.request_human(
                result.ticket_id,
                "我想转人工客服",
                request_id="customer-handoff",
            )
            self.assertEqual(escalated.ticket_id, result.ticket_id)
            self.assertEqual(escalated.status, "escalated")
            ticket = runtime.repository.get_ticket(result.ticket_id)
            self.assertEqual(ticket["manual_gate_reason"], "customer_requested_human")
            self.assertIsNone(ticket["idle_expires_at"])

    def test_handoff_stays_locked_before_three_ai_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = self._runtime(directory)
            result = runtime.submit(
                "蓝牙无法连接手机，怎么排查？",
                "1001",
                request_id="only-answer",
            )
            allowed, attempts = runtime.can_request_human(result.ticket_id)
            self.assertFalse(allowed)
            self.assertEqual(attempts, 1)
            with self.assertRaisesRegex(ValueError, "尚未达到"):
                runtime.request_human(
                    result.ticket_id,
                    "转人工",
                    request_id="too-early",
                )


if __name__ == "__main__":
    unittest.main()
