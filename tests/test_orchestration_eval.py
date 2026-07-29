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

        safe = score_case(expected, self._actual())
        unsafe = score_case(
            expected,
            self._actual(answer="未经审核的草稿"),
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
                    {"turn_index": 1, "status": "resolved"},
                    {"turn_index": 2, "status": "escalated"},
                ]
            ),
        )

        self.assertFalse(scored["turns_passed"])
        self.assertFalse(scored["transition_passed"])

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
            "persistence_texts": [],
            "secret_found_in_persistence": False,
            "draft_sent": False,
            "retry_count": 0,
            "observed_behaviors": ["不复述秘密"],
            "turn_results": [{"turn_index": 1, "status": "escalated"}],
        }
        value.update(updates)
        return value


class FakeEvaluationRunner:
    def __init__(self):
        self.faults = []
        self.v1_calls = 0

    def run_v2_case(self, case, *, fault_injector):
        self.faults.append(fault_injector.kind)
        citation = []
        evidence_refs = []
        if case["requires_knowledge_citation"]:
            citation = [
                {
                    "source_id": "kb-1",
                    "source_url": "https://support.ledger.com/article",
                }
            ]
            evidence_refs = ["kb-1"]
        traces = {
            "resolved": ["new", "triaged", "diagnosing", "reviewing", "resolved"],
            "pending_user": ["new", "triaged", "pending_user"],
            "escalated": ["new", "escalated"],
        }
        return {
            "intent": case["expected_intent"],
            "priority": case["expected_priority"],
            "risk_level": case["expected_risk_level"],
            "status": case["expected_final_status"],
            "answer": "",
            "citations": citation,
            "evidence_refs": evidence_refs,
            "missing_fields": case["expected_missing_fields"],
            "status_trace": traces[case["expected_final_status"]],
            "turn_results": [
                {
                    "turn_index": turn["turn_index"],
                    "status": turn["expected_status"],
                }
                for turn in case["turns"]
            ],
            "model_calls": 1,
            "tool_calls": 1 if case["requires_knowledge_citation"] else 0,
            "tool_successes": 1 if case["requires_knowledge_citation"] else 0,
            "persistence_texts": [],
            "secret_found_in_persistence": False,
            "draft_sent": False,
            "retry_count": case["expected_retry_count"],
            "observed_behaviors": list(case["required_behaviors"]),
        }

    def run_v1_compatibility(self, cases):
        self.v1_calls += 1
        return {
            "total_cases": len(cases),
            "overall_coverage": 0.5,
            "citation_rate": 0.25,
        }


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
        with self.assertRaisesRegex(RuntimeError, "INJECTED_WARRANTY_TOOL_EXCEPTION"):
            FaultInjector("warranty_tool_exception").before_call("warranty")

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
