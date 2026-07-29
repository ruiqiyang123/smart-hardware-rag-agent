import unittest
from operator import add
from typing import get_args, get_type_hints

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import ValidationError

import agent.orchestration.state as state_module
from agent.orchestration.state import (
    Citation,
    DiagnosisAction,
    DiagnosisResult,
    ReviewResult,
    TicketState,
    TriageResult,
)


class TicketStateContractTest(unittest.TestCase):
    def _triage(self, **overrides):
        values = {
            "intent": "troubleshoot",
            "category": "bluetooth_connection",
            "priority": "P2",
            "risk_level": "low",
            "risk_flags": [],
            "missing_fields": [],
            "suggested_route": "diagnose",
            "summary": "蓝牙连接失败",
        }
        values.update(overrides)
        return TriageResult(**values)

    def _diagnosis(self, **overrides):
        values = {
            "outcome": "draft",
            "diagnosis_summary": "连接问题",
            "recommended_actions": [
                DiagnosisAction(
                    action_code="generic_troubleshooting",
                    text="删除旧配对",
                    evidence_refs=["kb:bluetooth:1"],
                )
            ],
            "evidence_refs": ["kb:bluetooth:1"],
            "citations": [],
            "draft_answer": "请删除旧配对。",
            "remaining_unknowns": [],
        }
        values.update(overrides)
        return DiagnosisResult(**values)

    def test_triage_rejects_unknown_enum(self):
        with self.assertRaises(ValidationError):
            self._triage(priority="P9")

    def test_triage_rejects_inconsistent_high_and_critical_routing(self):
        invalid_cases = [
            {"risk_level": "critical", "priority": "P1", "suggested_route": "escalate"},
            {"risk_level": "critical", "priority": "P0", "suggested_route": "diagnose"},
            {"risk_level": "high", "priority": "P2", "suggested_route": "escalate"},
            {"risk_level": "high", "priority": "P1", "suggested_route": "diagnose"},
            {
                "risk_level": "high",
                "priority": "P1",
                "risk_flags": ["secret_exposure"],
                "suggested_route": "escalate",
            },
        ]
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self._triage(**values)

    def test_triage_high_risk_flags_cannot_be_downgraded(self):
        for flag in [
            "phishing",
            "asset_loss",
            "unofficial_firmware",
            "address_mismatch",
            "suspicious_signature",
            "device_auth_failure",
            "remote_control",
        ]:
            with self.subTest(flag=flag), self.assertRaises(ValidationError):
                self._triage(
                    priority="P1",
                    risk_level="medium",
                    risk_flags=[flag],
                    suggested_route="escalate",
                )

    def test_phishing_and_asset_loss_require_critical_contract(self):
        for flag in ["phishing", "asset_loss"]:
            with self.subTest(flag=flag, risk_level="high"), self.assertRaises(
                ValidationError
            ):
                self._triage(
                    priority="P1",
                    risk_level="high",
                    risk_flags=[flag],
                    suggested_route="escalate",
                )

            with self.subTest(flag=flag, risk_level="critical"):
                result = self._triage(
                    priority="P0",
                    risk_level="critical",
                    risk_flags=[flag],
                    suggested_route="escalate",
                )
                self.assertEqual(result.risk_level, "critical")

    def test_triage_rejects_duplicate_enum_lists(self):
        with self.assertRaises(ValidationError):
            self._triage(risk_flags=["phishing", "phishing"])
        with self.assertRaises(ValidationError):
            self._triage(missing_fields=["device_model", "device_model"])

    def test_draft_requires_bound_evidence(self):
        with self.assertRaises(ValidationError):
            self._diagnosis(
                recommended_actions=[
                    DiagnosisAction(
                        action_code="generic_troubleshooting",
                        text="删除旧配对",
                        evidence_refs=["kb:missing"],
                    )
                ],
                evidence_refs=["kb:bluetooth:1"],
            )

    def test_evidence_refs_reject_blank_duplicate_and_overlong_values(self):
        invalid_cases = [
            {
                "evidence_refs": [" "],
                "recommended_actions": [
                    {
                        "action_code": "generic_troubleshooting",
                        "text": "删除旧配对",
                        "evidence_refs": [" "],
                    }
                ],
            },
            {"evidence_refs": ["kb:1", "kb:1"]},
            {
                "evidence_refs": ["x" * 129],
                "recommended_actions": [
                    {
                        "action_code": "generic_troubleshooting",
                        "text": "删除旧配对",
                        "evidence_refs": ["x" * 129],
                    }
                ],
            },
            {
                "evidence_refs": ["kb:1"],
                "recommended_actions": [
                    {
                        "action_code": "generic_troubleshooting",
                        "text": "删除旧配对",
                        "evidence_refs": ["kb:1", "kb:1"],
                    }
                ],
            },
        ]
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self._diagnosis(**values)

    def test_citations_must_be_unique_and_bound_to_top_level_evidence(self):
        unbound = Citation(
            source_id="kb:missing",
            source_title="蓝牙指南",
            source_url="https://support.example/ble",
        )
        with self.assertRaises(ValidationError):
            self._diagnosis(citations=[unbound])

        citation = Citation(
            source_id="kb:bluetooth:1",
            source_title="蓝牙指南",
            source_url="https://support.example/ble",
        )
        with self.assertRaises(ValidationError):
            self._diagnosis(citations=[citation, citation])

    def test_review_approve_has_no_issues(self):
        result = ReviewResult(
            decision="approve",
            issues=[],
            required_changes=[],
            safety_flags=[],
            reason_codes=["passed"],
        )
        self.assertEqual(result.decision, "approve")

    def test_review_accepts_valid_revise_and_escalate_decisions(self):
        revise = ReviewResult(
            decision="revise",
            issues=["缺少证据"],
            required_changes=["补充官方来源"],
            safety_flags=[],
            reason_codes=["missing_evidence"],
        )
        escalate = ReviewResult(
            decision="escalate",
            issues=["检测到敏感信息"],
            required_changes=[],
            safety_flags=["secret_exposure"],
            reason_codes=["secret_exposure"],
        )
        self.assertEqual(revise.decision, "revise")
        self.assertEqual(escalate.decision, "escalate")

    def test_review_revise_requires_non_passed_reason(self):
        for reasons in ([], ["passed"], ["passed", "missing_evidence"]):
            with self.subTest(reasons=reasons), self.assertRaises(ValidationError):
                ReviewResult(
                    decision="revise",
                    issues=["缺少证据"],
                    required_changes=["补充官方来源"],
                    safety_flags=[],
                    reason_codes=reasons,
                )

    def test_review_safety_flags_force_escalation(self):
        with self.assertRaises(ValidationError):
            ReviewResult(
                decision="revise",
                issues=["危险动作"],
                required_changes=["删除危险动作"],
                safety_flags=["asset_loss"],
                reason_codes=["unsafe_action"],
            )

    def test_review_rejects_blank_overlong_and_duplicate_items(self):
        invalid_cases = [
            {
                "issues": [" "],
                "required_changes": ["补充来源"],
                "safety_flags": [],
                "reason_codes": ["missing_evidence"],
            },
            {
                "issues": ["缺少证据"],
                "required_changes": ["x" * 501],
                "safety_flags": [],
                "reason_codes": ["missing_evidence"],
            },
            {
                "issues": ["缺少证据", "缺少证据"],
                "required_changes": ["补充来源"],
                "safety_flags": [],
                "reason_codes": ["missing_evidence"],
            },
            {
                "issues": ["缺少证据"],
                "required_changes": ["补充来源", "补充来源"],
                "safety_flags": [],
                "reason_codes": ["missing_evidence"],
            },
            {
                "issues": ["缺少证据"],
                "required_changes": ["补充来源"],
                "safety_flags": [],
                "reason_codes": ["missing_evidence", "missing_evidence"],
            },
        ]
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ReviewResult(decision="revise", **values)

        with self.assertRaises(ValidationError):
            ReviewResult(
                decision="escalate",
                issues=["安全风险"],
                required_changes=[],
                safety_flags=["asset_loss", "asset_loss"],
                reason_codes=["unsafe_action"],
            )

    def test_review_item_accepts_300_characters_and_rejects_301(self):
        result = ReviewResult(
            decision="revise",
            issues=["i" * 300],
            required_changes=["c" * 300],
            safety_flags=[],
            reason_codes=["missing_evidence"],
        )
        self.assertEqual(len(result.issues[0]), 300)
        self.assertEqual(len(result.required_changes[0]), 300)

        for field_name in ("issues", "required_changes"):
            values = {
                "issues": ["缺少证据"],
                "required_changes": ["补充来源"],
                "safety_flags": [],
                "reason_codes": ["missing_evidence"],
            }
            values[field_name] = ["x" * 301]
            with self.subTest(field_name=field_name), self.assertRaises(ValidationError):
                ReviewResult(decision="revise", **values)

    def test_review_schema_and_runtime_bound_every_output_list(self):
        properties = ReviewResult.model_json_schema()["properties"]
        self.assertEqual(properties["issues"]["maxItems"], 6)
        self.assertEqual(properties["required_changes"]["maxItems"], 6)
        self.assertEqual(properties["safety_flags"]["maxItems"], 8)
        self.assertEqual(properties["reason_codes"]["maxItems"], 9)

        cases = (
            {
                "issues": [f"问题 {index}" for index in range(7)],
                "safety_flags": [],
                "reason_codes": ["unsafe_action"],
            },
            {
                "issues": ["安全风险"],
                "safety_flags": ["remote_control"] * 9,
                "reason_codes": ["unsafe_action"],
            },
            {
                "issues": ["安全风险"],
                "safety_flags": [],
                "reason_codes": ["unsafe_action"] * 10,
            },
        )
        for values in cases:
            lengths = {key: len(value) for key, value in values.items()}
            with self.subTest(field_lengths=lengths), self.assertRaises(
                ValidationError
            ):
                ReviewResult(
                    decision="escalate",
                    required_changes=[],
                    **values,
                )

    def test_ticket_state_uses_explicit_serializable_state_types_and_add_reducer(self):
        annotations = get_type_hints(TicketState, include_extras=True)
        evidence_type = get_args(annotations["evidence"])[0]
        action_type = get_args(annotations["recommended_actions"])[0]
        events_type, reducer = get_args(annotations["status_events"])
        event_type = get_args(events_type)[0]

        self.assertIs(evidence_type, state_module.EvidenceState)
        self.assertIs(action_type, state_module.DiagnosisActionState)
        self.assertIn("review_issues", annotations)
        self.assertIn("required_changes", annotations)
        self.assertIs(event_type, state_module.StatusEventState)
        self.assertIs(reducer, add)
        self.assertEqual(reducer([{"step_index": 1}], [{"step_index": 2}]), [
            {"step_index": 1},
            {"step_index": 2},
        ])
        typed_state = (
            get_type_hints(state_module.EvidenceState, include_extras=True),
            get_type_hints(state_module.StatusEventState, include_extras=True),
        )
        self.assertNotIn("<class 'object'>", repr(typed_state))

    def test_checkpoint_serializer_strictly_round_trips_representative_state(self):
        event = {
            "command_id": "request:req-1",
            "step_index": 1,
            "node_name": "triage",
            "event_type": "triage.completed",
            "summary": "分诊完成",
            "from_status": "new",
            "to_status": "triaged",
            "metadata": {
                "score": 0.98,
                "labels": ["safe", "official"],
                "checks": {"secret": False, "note": None},
            },
        }
        state = {
            "ticket_id": "ticket-1",
            "status": "triaged",
            "evidence": [
                {
                    "evidence_id": "kb:bluetooth:1",
                    "kind": "knowledge",
                    "content": "删除旧配对后重试。",
                    "source_title": "蓝牙指南",
                    "source_url": "https://support.example/ble",
                    "metadata": {"rank": 1, "official": True},
                }
            ],
            "status_events": [event],
        }
        serializer = JsonPlusSerializer(
            pickle_fallback=False,
            allowed_json_modules=None,
            allowed_msgpack_modules=None,
        )

        encoded = serializer.dumps_typed(state)
        self.assertNotEqual(encoded[0], "pickle")
        self.assertEqual(serializer.loads_typed(encoded), state)

        class UnsafeCheckpointValue:
            pass

        with self.assertRaises(TypeError):
            serializer.dumps_typed(UnsafeCheckpointValue())


if __name__ == "__main__":
    unittest.main()
