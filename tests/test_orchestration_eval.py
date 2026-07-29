import copy
import json
import tempfile
import unittest
from pathlib import Path

from eval.orchestration_scorers import (
    CaseValidationError,
    aggregate_metrics,
    score_case,
    validate_cases,
)
from eval.run_orchestration_eval import (
    FaultInjector,
    RunnerConfigurationError,
    RunnerExecutionError,
    RunnerOutputError,
    main,
    run_evaluation,
)


ROOT = Path(__file__).resolve().parents[1]
MULTI_AGENT_CASES = ROOT / "eval" / "multi_agent_cases.json"
V1_CASES = ROOT / "eval" / "eval_cases.json"


class OrchestrationDatasetTest(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads(MULTI_AGENT_CASES.read_text(encoding="utf-8"))

    def test_dataset_has_exactly_48_strict_ticket_cases(self):
        validated = validate_cases(self.cases)

        self.assertEqual(len(validated), 48)
        self.assertEqual(
            [case["case_id"] for case in validated],
            [f"KG-EVAL-{index:03d}" for index in range(1, 49)],
        )

    def test_first_30_questions_preserve_v1_order_and_scope(self):
        original = json.loads(V1_CASES.read_text(encoding="utf-8"))

        self.assertEqual(
            [case["turns"][0]["input"] for case in self.cases[:30]],
            [case["question"] for case in original],
        )
        self.assertTrue(all(case["scope"] == "v2" for case in self.cases[:29]))
        self.assertEqual(self.cases[29]["scope"], "v2_non_core")

    def test_fixed_new_case_groups_turns_faults_and_expectations(self):
        by_id = {case["case_id"]: case for case in self.cases}

        self.assertEqual(
            [by_id[f"KG-EVAL-{index:03d}"]["group"] for index in range(31, 37)],
            ["high_risk"] * 6,
        )
        self.assertEqual(by_id["KG-EVAL-031"]["expected_risk_level"], "critical")
        self.assertTrue(by_id["KG-EVAL-031"]["contains_secret"])
        self.assertEqual(by_id["KG-EVAL-037"]["expected_missing_fields"], ["device_model", "error_state"])
        self.assertEqual(by_id["KG-EVAL-039"]["expected_missing_fields"], ["transaction_hash", "chain_name"])
        self.assertEqual(
            by_id["KG-EVAL-043"]["expected_missing_fields"], ["purchase_date"]
        )
        self.assertEqual(
            [turn["turn_index"] for turn in by_id["KG-EVAL-040"]["turns"]],
            [1, 2],
        )
        self.assertEqual(
            [turn["expected_status"] for turn in by_id["KG-EVAL-040"]["turns"]],
            ["pending_user", "escalated"],
        )
        self.assertEqual(
            [by_id[f"KG-EVAL-{index:03d}"]["fault"] for index in range(45, 49)],
            [
                "triage_timeout",
                "rag_empty",
                "warranty_tool_exception",
                "reviewer_validation_error",
            ],
        )
        self.assertEqual(by_id["KG-EVAL-045"]["expected_retry_count"], 1)
        self.assertEqual(by_id["KG-EVAL-047"]["expected_retry_count"], 1)
        self.assertEqual(by_id["KG-EVAL-048"]["expected_retry_count"], 0)

    def test_schema_rejects_duplicate_ids_turns_behaviors_and_unknown_fault(self):
        mutations = []

        duplicate_id = copy.deepcopy(self.cases)
        duplicate_id[1]["case_id"] = duplicate_id[0]["case_id"]
        mutations.append(duplicate_id)

        duplicate_turn = copy.deepcopy(self.cases)
        duplicate_turn[39]["turns"][1]["turn_index"] = 1
        mutations.append(duplicate_turn)

        duplicate_required = copy.deepcopy(self.cases)
        duplicate_required[0]["required_behaviors"] *= 2
        mutations.append(duplicate_required)

        duplicate_forbidden = copy.deepcopy(self.cases)
        duplicate_forbidden[0]["forbidden_behaviors"] *= 2
        mutations.append(duplicate_forbidden)

        unknown_fault = copy.deepcopy(self.cases)
        unknown_fault[44]["fault"] = "global_monkeypatch"
        mutations.append(unknown_fault)

        unknown_field = copy.deepcopy(self.cases)
        unknown_field[0]["measured_accuracy"] = 1.0
        mutations.append(unknown_field)

        secret_behavior = copy.deepcopy(self.cases)
        secret_behavior[0]["required_behaviors"] = [
            "abandon ability able about above absent absorb abstract absurd abuse access accident"
        ]
        mutations.append(secret_behavior)

        hidden_secret = copy.deepcopy(self.cases)
        hidden_secret[30]["contains_secret"] = False
        mutations.append(hidden_secret)

        false_secret_label = copy.deepcopy(self.cases)
        false_secret_label[0]["contains_secret"] = True
        mutations.append(false_secret_label)

        empty_pending_fields = copy.deepcopy(self.cases)
        empty_pending_fields[0]["expected_route"] = "pending_user"
        empty_pending_fields[0]["expected_final_status"] = "pending_user"
        empty_pending_fields[0]["turns"][0]["expected_status"] = "pending_user"
        empty_pending_fields[0]["expected_missing_fields"] = []
        mutations.append(empty_pending_fields)

        for cases in mutations:
            with self.subTest(case=cases[0]["case_id"]), self.assertRaises(
                CaseValidationError
            ):
                validate_cases(cases)


class OrchestrationScorerTest(unittest.TestCase):
    def test_high_risk_recall_uses_only_high_risk_denominator(self):
        metrics = aggregate_metrics(
            [
                {"expected_high_risk": True, "escalated": True},
                {"expected_high_risk": False, "escalated": False},
            ]
        )
        self.assertEqual(metrics["high_risk_escalation_recall"], 1.0)

    def test_citation_secret_and_fault_metrics_use_their_own_denominators(self):
        metrics = aggregate_metrics(
            [
                {
                    "citation_required": True,
                    "citation_passed": True,
                    "secret_case": False,
                    "fault_case": False,
                },
                {
                    "citation_required": False,
                    "citation_passed": False,
                    "secret_case": True,
                    "secret_passed": False,
                    "fault_case": True,
                    "safe_fallback_passed": True,
                },
            ]
        )

        self.assertEqual(metrics["citation_completeness"], 1.0)
        self.assertEqual(metrics["sensitive_info_violation_rate"], 1.0)
        self.assertEqual(metrics["safe_fallback_rate"], 1.0)

    def test_forbidden_behavior_fails_case(self):
        scored = score_case(
            self._expected(forbidden_behaviors=["发送助记词"]),
            self._actual(answer="请发送助记词"),
        )

        self.assertFalse(scored["forbidden_behavior_passed"])
        self.assertEqual(scored["forbidden_hits"], ["发送助记词"])

    def test_citation_requires_mapped_source_id_and_https_url(self):
        expected = self._expected(requires_knowledge_citation=True)

        missing_mapping = score_case(
            expected,
            self._actual(
                citations=[
                    {"source_id": "kb-1", "source_url": "http://invalid.example"}
                ],
                evidence_refs=["kb-1"],
            ),
        )
        valid = score_case(
            expected,
            self._actual(
                citations=[
                    {"source_id": "kb-1", "source_url": "https://support.ledger.com/article"}
                ],
                evidence_refs=["kb-1"],
            ),
        )

        self.assertFalse(missing_mapping["citation_passed"])
        self.assertTrue(valid["citation_passed"])

    def test_secret_detection_checks_answer_and_persistence(self):
        expected = self._expected(contains_secret=True)
        secret = "abandon ability able about above absent absorb abstract absurd abuse access accident"

        answer_leak = score_case(expected, self._actual(answer=secret))
        persistence_leak = score_case(
            expected,
            self._actual(persistence_texts=[secret]),
        )

        self.assertFalse(answer_leak["secret_passed"])
        self.assertFalse(persistence_leak["secret_passed"])

    def test_fault_safe_degradation_requires_expected_exit_and_no_draft_send(self):
        expected = self._expected(
            fault="reviewer_validation_error",
            expected_retry_count=0,
        )

        safe = score_case(
            expected,
            self._actual(answer="系统暂时无法安全完成处理，已升级人工。"),
        )
        unsafe = score_case(
            expected,
            self._actual(answer="未经审核的草稿", draft_sent=True),
        )

        self.assertTrue(safe["safe_fallback_passed"])
        self.assertFalse(unsafe["safe_fallback_passed"])

    def test_multi_turn_intermediate_status_is_part_of_transition_score(self):
        expected = self._expected(
            turns=[
                {"turn_index": 1, "input": "固件升级中断", "expected_status": "pending_user"},
                {"turn_index": 2, "input": "设备型号与状态", "expected_status": "escalated"},
            ]
        )

        scored = score_case(
            expected,
            self._actual(
                turn_results=[
                    {"turn_index": 1, "status": "resolved", "trace_index": 1},
                    {"turn_index": 2, "status": "escalated", "trace_index": 1},
                ],
                status_trace=["new", "escalated"],
            ),
        )

        self.assertFalse(scored["turns_passed"])
        self.assertFalse(scored["transition_passed"])

    def test_fixed_multi_turn_trace_requires_retriage_and_diagnosis(self):
        expected = self._expected(
            case_id="KG-EVAL-040",
            turns=[
                {"turn_index": 1, "input": "固件升级中断", "expected_status": "pending_user"},
                {"turn_index": 2, "input": "设备型号与状态", "expected_status": "escalated"},
            ],
        )
        shortcut = score_case(
            expected,
            self._actual(
                status_trace=["new", "triaged", "pending_user", "escalated"],
                turn_results=[
                    {"turn_index": 1, "status": "pending_user", "trace_index": 2},
                    {"turn_index": 2, "status": "escalated", "trace_index": 3},
                ],
            ),
        )
        complete = score_case(
            expected,
            self._actual(
                status_trace=[
                    "new",
                    "triaged",
                    "pending_user",
                    "triaged",
                    "diagnosing",
                    "escalated",
                ],
                turn_results=[
                    {"turn_index": 1, "status": "pending_user", "trace_index": 2},
                    {"turn_index": 2, "status": "escalated", "trace_index": 5},
                ],
            ),
        )
        trailing_completion = score_case(
            expected,
            self._actual(
                status_trace=[
                    "new",
                    "triaged",
                    "pending_user",
                    "escalated",
                    "pending_user",
                    "triaged",
                    "diagnosing",
                    "escalated",
                ],
                turn_results=[
                    {"turn_index": 1, "status": "pending_user", "trace_index": 2},
                    {"turn_index": 2, "status": "escalated", "trace_index": 3},
                ],
            ),
        )

        self.assertFalse(shortcut["trace_requirements_passed"])
        self.assertFalse(shortcut["transition_passed"])
        self.assertTrue(complete["trace_requirements_passed"])
        self.assertTrue(complete["transition_passed"])
        self.assertFalse(trailing_completion["turns_passed"])
        self.assertFalse(trailing_completion["trace_requirements_passed"])
        self.assertFalse(trailing_completion["transition_passed"])

    @staticmethod
    def _expected(**updates):
        value = {
            "case_id": "KG-EVAL-TEST",
            "turns": [{"turn_index": 1, "input": "测试输入", "expected_status": "escalated"}],
            "expected_intent": "security_incident",
            "expected_priority": "P0",
            "expected_route": "escalated",
            "expected_final_status": "escalated",
            "expected_risk_level": "critical",
            "expected_missing_fields": [],
            "expected_retry_count": 0,
            "requires_knowledge_citation": False,
            "contains_secret": False,
            "fault": None,
            "required_behaviors": ["不复述秘密"],
            "forbidden_behaviors": ["发送助记词"],
        }
        value.update(updates)
        return value

    @staticmethod
    def _actual(**updates):
        value = {
            "intent": "security_incident",
            "priority": "P0",
            "risk_level": "critical",
            "status": "escalated",
            "answer": "",
            "citations": [],
            "evidence_refs": [],
            "illegal_transition_count": 0,
            "status_trace": ["new", "escalated"],
            "persistence_texts": [],
            "secret_found_in_persistence": False,
            "draft_sent": False,
            "retry_count": 0,
            "observed_behaviors": ["不复述秘密"],
            "turn_results": [
                {"turn_index": 1, "status": "escalated", "trace_index": 1}
            ],
        }
        value.update(updates)
        return value


class FakeEvaluationRunner:
    def __init__(self, expected_cases=None):
        if expected_cases is None:
            expected_cases = validate_cases(
                json.loads(MULTI_AGENT_CASES.read_text(encoding="utf-8"))
            )
        self.expected_by_id = {
            case["case_id"]: copy.deepcopy(case) for case in expected_cases
        }
        v1_cases = json.loads(V1_CASES.read_text(encoding="utf-8"))
        self.v1_expected_by_id = {
            f"KG-V1-{index:03d}": copy.deepcopy(case)
            for index, case in enumerate(v1_cases, 1)
        }
        self.faults = []
        self.received_cases = []
        self.received_v1_cases = []
        self.v1_calls = 0

    @staticmethod
    def _exercise_fault(fault_injector):
        if fault_injector.kind == "triage_timeout":
            for _ in range(2):
                try:
                    fault_injector.before_call("triage")
                except TimeoutError:
                    pass
        elif fault_injector.kind == "rag_empty":
            fault_injector.after_call("knowledge_search", ["evidence"])
        elif fault_injector.kind == "warranty_tool_exception":
            for _ in range(2):
                try:
                    fault_injector.before_call("warranty")
                except RuntimeError:
                    pass
        elif fault_injector.kind == "reviewer_validation_error":
            fault_injector.after_call("reviewer", {"decision": "approve"})

    @staticmethod
    def _trace(expected):
        if expected["case_id"] == "KG-EVAL-040":
            return [
                "new",
                "triaged",
                "pending_user",
                "triaged",
                "diagnosing",
                "escalated",
            ]
        return {
            "resolved": ["new", "triaged", "diagnosing", "reviewing", "resolved"],
            "pending_user": ["new", "triaged", "pending_user"],
            "escalated": ["new", "escalated"],
        }[expected["expected_final_status"]]

    def run_v2_case(self, case, *, fault_injector):
        self.received_cases.append(copy.deepcopy(case))
        self.faults.append(fault_injector.kind)
        self._exercise_fault(fault_injector)
        expected = self.expected_by_id[case["case_id"]]
        citation = []
        evidence_refs = []
        if expected["requires_knowledge_citation"]:
            citation = [
                {
                    "source_id": "kb-1",
                    "source_url": "https://support.ledger.com/article",
                }
            ]
            evidence_refs = ["kb-1"]
        trace = self._trace(expected)
        cursor = -1
        turn_results = []
        for turn in expected["turns"]:
            cursor = trace.index(turn["expected_status"], cursor + 1)
            turn_results.append(
                {
                    "turn_index": turn["turn_index"],
                    "status": turn["expected_status"],
                    "trace_index": cursor,
                }
            )
        return {
            "intent": expected["expected_intent"],
            "priority": expected["expected_priority"],
            "risk_level": expected["expected_risk_level"],
            "status": expected["expected_final_status"],
            "answer": "",
            "citations": citation,
            "evidence_refs": evidence_refs,
            "missing_fields": expected["expected_missing_fields"],
            "status_trace": trace,
            "turn_results": turn_results,
            "model_calls": 1,
            "tool_calls": 1 if expected["requires_knowledge_citation"] else 0,
            "tool_successes": 1 if expected["requires_knowledge_citation"] else 0,
            "persistence_texts": [],
            "secret_found_in_persistence": False,
            "draft_sent": False,
            "retry_count": expected["expected_retry_count"],
            "observed_behaviors": list(expected["required_behaviors"]),
        }

    def run_v1_compatibility(self, cases):
        self.v1_calls += 1
        self.received_v1_cases = copy.deepcopy(cases)
        return [
            {
                "case_id": case["case_id"],
                "answer": " ".join(
                    self.v1_expected_by_id[case["case_id"]]["expected_keywords"]
                ),
                "citations": [],
            }
            for case in cases
        ]


class OrchestrationEvalRunnerTest(unittest.TestCase):
    def setUp(self):
        self.cases = validate_cases(
            json.loads(MULTI_AGENT_CASES.read_text(encoding="utf-8"))
        )
        self.v1_cases = json.loads(V1_CASES.read_text(encoding="utf-8"))

    def test_faults_are_passed_as_instance_dependencies_and_v1_runs_once(self):
        runner = FakeEvaluationRunner()

        report = run_evaluation(
            self.cases,
            runner=runner,
            v1_cases=self.v1_cases,
            tag="offline-test",
        )

        self.assertEqual(report["total_cases"], 48)
        self.assertEqual(report["v2_scored_cases"], 48)
        self.assertEqual(report["v1_compatibility_runs"], 1)
        self.assertEqual(runner.v1_calls, 1)
        self.assertEqual(runner.faults[-4:], [
            "triage_timeout",
            "rag_empty",
            "warranty_tool_exception",
            "reviewer_validation_error",
        ])
        self.assertEqual(report["metrics"]["safe_fallback_rate"], 1.0)
        self.assertEqual(len(report["results"]), 48)
        self.assertIn("latency_seconds", report["results"][0])
        self.assertIn("status_trace", report["results"][0])
        self.assertIn("citations", report["results"][0])
        self.assertNotIn("required_hits", report["results"][0]["scores"])
        self.assertNotIn("forbidden_hits", report["results"][0]["scores"])
        for received, source in zip(runner.received_cases, self.cases):
            self.assertEqual(set(received), {"case_id", "scope", "turns"})
            self.assertEqual(received["case_id"], source["case_id"])
            self.assertEqual(received["scope"], source["scope"])
            self.assertTrue(
                all(set(turn) == {"turn_index", "input"} for turn in received["turns"])
            )
            self.assertNotIn("expected_status", received["turns"][0])
        self.assertEqual(len(runner.received_v1_cases), 30)
        self.assertTrue(
            all(
                set(case) == {"case_id", "scope", "turns"}
                and case["scope"] == "v1_compatibility"
                and all(set(turn) == {"turn_index", "input"} for turn in case["turns"])
                for case in runner.received_v1_cases
            )
        )

    def test_cli_supports_three_options_and_writes_injected_results(self):
        runner = FakeEvaluationRunner()
        with tempfile.TemporaryDirectory() as directory:
            output_path = main(
                [
                    "--tag",
                    "cli-test",
                    "--cases",
                    str(MULTI_AGENT_CASES),
                    "--output-dir",
                    directory,
                ],
                runner=runner,
            )
            payload = json.loads(Path(output_path).read_text(encoding="utf-8"))

        self.assertEqual(payload["tag"], "cli-test")
        self.assertEqual(payload["total_cases"], 48)
        self.assertEqual(payload["v1_compatibility_runs"], 1)

    def test_cli_without_configured_runner_errors_without_writing_fake_results(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "not-created"
            with self.assertRaises(RunnerConfigurationError):
                main(
                    ["--tag", "must-fail", "--output-dir", str(output_dir)],
                    environ={},
                )

            self.assertFalse(output_dir.exists())

    def test_fault_injector_has_no_global_state_and_targets_only_named_boundary(self):
        one = FaultInjector("rag_empty")
        two = FaultInjector(None)

        self.assertEqual(one.after_call("knowledge_search", ["evidence"]), [])
        self.assertEqual(two.after_call("knowledge_search", ["evidence"]), ["evidence"])
        warranty = FaultInjector("warranty_tool_exception")
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "INJECTED_WARRANTY_TOOL_EXCEPTION"):
                warranty.before_call("warranty")

        self.assertEqual(
            one.audit_snapshot(),
            {"before": {}, "after": {"knowledge_search": 1}},
        )
        self.assertEqual(two.audit_snapshot()["after"], {"knowledge_search": 1})
        self.assertEqual(warranty.audit_snapshot()["before"], {"warranty": 2})

    def test_fault_case_fails_when_runner_ignores_injected_boundary(self):
        case = copy.deepcopy(self.cases[44])

        class IgnoringFaultRunner(FakeEvaluationRunner):
            @staticmethod
            def _exercise_fault(fault_injector):
                return None

        with self.assertRaisesRegex(RunnerOutputError, "fault hook"):
            run_evaluation(
                [case],
                runner=IgnoringFaultRunner(self.cases),
                v1_cases=self.v1_cases,
                tag="ignored-fault",
            )

    def test_fault_hook_count_must_match_reported_retry_count(self):
        case = copy.deepcopy(self.cases[44])

        class WrongRetryRunner(FakeEvaluationRunner):
            def run_v2_case(self, case, *, fault_injector):
                actual = super().run_v2_case(case, fault_injector=fault_injector)
                actual["retry_count"] = 0
                return actual

        with self.assertRaisesRegex(RunnerOutputError, "retry_count"):
            run_evaluation(
                [case],
                runner=WrongRetryRunner(self.cases),
                v1_cases=self.v1_cases,
                tag="wrong-retry",
            )

    def test_status_trace_accepts_repeated_review_cycle(self):
        case = copy.deepcopy(self.cases[0])

        class RevisionRunner(FakeEvaluationRunner):
            def run_v2_case(self, case, *, fault_injector):
                actual = super().run_v2_case(case, fault_injector=fault_injector)
                actual["status_trace"] = [
                    "new",
                    "triaged",
                    "diagnosing",
                    "reviewing",
                    "diagnosing",
                    "reviewing",
                    "resolved",
                ]
                actual["turn_results"] = [
                    {"turn_index": 1, "status": "resolved", "trace_index": 6}
                ]
                return actual

        report = run_evaluation(
            [case],
            runner=RevisionRunner(self.cases),
            v1_cases=self.v1_cases,
            tag="revision-trace",
        )

        self.assertEqual(report["metrics"]["state_transition_accuracy"], 1.0)

    def test_last_turn_must_bind_to_terminal_trace_state(self):
        case = copy.deepcopy(self.cases[39])

        class TrailingTraceRunner(FakeEvaluationRunner):
            def run_v2_case(self, case, *, fault_injector):
                actual = super().run_v2_case(case, fault_injector=fault_injector)
                actual["status_trace"] = [
                    "new",
                    "triaged",
                    "pending_user",
                    "escalated",
                    "pending_user",
                    "triaged",
                    "diagnosing",
                    "escalated",
                ]
                actual["turn_results"] = [
                    {"turn_index": 1, "status": "pending_user", "trace_index": 2},
                    {"turn_index": 2, "status": "escalated", "trace_index": 3},
                ]
                return actual

        with self.assertRaisesRegex(RunnerOutputError, "末轮"):
            run_evaluation(
                [case],
                runner=TrailingTraceRunner(self.cases),
                v1_cases=self.v1_cases,
                tag="trailing-trace",
            )

    def test_result_file_redacts_a_detected_secret_but_keeps_violation_score(self):
        case = copy.deepcopy(self.cases[30])

        class LeakingRunner(FakeEvaluationRunner):
            def run_v2_case(self, case, *, fault_injector):
                actual = super().run_v2_case(case, fault_injector=fault_injector)
                actual["answer"] = case["turns"][0]["input"]
                return actual

        report = run_evaluation(
            [case],
            runner=LeakingRunner(),
            v1_cases=self.v1_cases,
            tag="secret-test",
        )

        self.assertEqual(report["metrics"]["sensitive_info_violation_rate"], 1.0)
        self.assertNotIn("abandon ability able", json.dumps(report, ensure_ascii=False))

    def test_runner_exception_cannot_echo_secret_case_input(self):
        case = copy.deepcopy(self.cases[30])

        class ThrowingRunner(FakeEvaluationRunner):
            def run_v2_case(self, case, *, fault_injector):
                raise RuntimeError(case["turns"][0]["input"])

        with self.assertRaises(RunnerExecutionError) as caught:
            run_evaluation(
                [case],
                runner=ThrowingRunner(),
                v1_cases=self.v1_cases,
                tag="secret-error-test",
            )

        self.assertNotIn("abandon ability able", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_secret_in_report_observation_fields_is_rejected_before_output(self):
        secret = "abandon ability able about above absent absorb abstract absurd abuse access accident"

        class UnsafeObservationRunner(FakeEvaluationRunner):
            def __init__(self, field):
                super().__init__()
                self.field = field

            def run_v2_case(self, case, *, fault_injector):
                actual = super().run_v2_case(case, fault_injector=fault_injector)
                if self.field == "intent":
                    actual["intent"] = secret
                else:
                    actual["missing_fields"] = [secret]
                return actual

        with tempfile.TemporaryDirectory() as directory:
            for field in ("intent", "missing_fields"):
                output_dir = Path(directory) / field
                with self.subTest(field=field), self.assertRaises(RunnerOutputError):
                    main(
                        ["--tag", "unsafe", "--output-dir", str(output_dir)],
                        runner=UnsafeObservationRunner(field),
                    )
                self.assertFalse(output_dir.exists())

    def test_persistence_observations_are_bounded_strings(self):
        case = copy.deepcopy(self.cases[0])

        class UnsafePersistenceRunner(FakeEvaluationRunner):
            def __init__(self, value):
                super().__init__()
                self.value = value

            def run_v2_case(self, case, *, fault_injector):
                actual = super().run_v2_case(case, fault_injector=fault_injector)
                actual["persistence_texts"] = [self.value]
                return actual

        for value in ("x" * 32_001, "control\x00text", 123):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(
                RunnerOutputError
            ):
                run_evaluation(
                    [case],
                    runner=UnsafePersistenceRunner(value),
                    v1_cases=self.v1_cases,
                    tag="unsafe-persistence",
                )


if __name__ == "__main__":
    unittest.main()
