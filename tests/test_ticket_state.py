import unittest

from pydantic import ValidationError

from agent.orchestration.state import (
    DiagnosisAction,
    DiagnosisResult,
    ReviewResult,
    TriageResult,
)


class TicketStateContractTest(unittest.TestCase):
    def test_triage_rejects_unknown_enum(self):
        with self.assertRaises(ValidationError):
            TriageResult(
                intent="troubleshoot",
                category="bluetooth_connection",
                priority="P9",
                risk_level="low",
                risk_flags=[],
                missing_fields=[],
                suggested_route="diagnose",
                summary="蓝牙连接失败",
            )

    def test_draft_requires_bound_evidence(self):
        with self.assertRaises(ValidationError):
            DiagnosisResult(
                outcome="draft",
                diagnosis_summary="连接问题",
                recommended_actions=[
                    DiagnosisAction(
                        action_code="generic_troubleshooting",
                        text="删除旧配对",
                        evidence_refs=["kb:missing"],
                    )
                ],
                evidence_refs=["kb:bluetooth:1"],
                citations=[],
                draft_answer="请删除旧配对。",
                remaining_unknowns=[],
            )

    def test_review_approve_has_no_issues(self):
        result = ReviewResult(
            decision="approve",
            issues=[],
            required_changes=[],
            safety_flags=[],
            reason_codes=["passed"],
        )
        self.assertEqual(result.decision, "approve")


if __name__ == "__main__":
    unittest.main()
