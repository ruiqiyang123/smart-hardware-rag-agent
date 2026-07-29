import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from agent.nodes.triage import TriageAgent
from agent.orchestration.state import RiskFlag, TriageResult


def triage_result(**overrides):
    values = {
        "intent": "troubleshoot",
        "category": "other",
        "priority": "P2",
        "risk_level": "low",
        "risk_flags": [],
        "missing_fields": [],
        "suggested_route": "diagnose",
        "summary": "普通设备问题",
    }
    values.update(overrides)
    return TriageResult(**values)


class FakeStructuredRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        if isinstance(self.result, BaseException):
            raise self.result
        if callable(self.result):
            return self.result()
        return self.result


class FakeModel:
    def __init__(self, runner):
        self.runner = runner
        self.calls = []

    def with_structured_output(self, schema, **kwargs):
        self.calls.append((schema, kwargs))
        return self.runner


class AgentContractTest(unittest.TestCase):
    def test_constructor_requires_exactly_one_model_or_runner(self):
        runner = FakeStructuredRunner(triage_result())
        for kwargs in ({}, {"model": FakeModel(runner), "runner": runner}):
            with self.subTest(kwargs=sorted(kwargs)), self.assertRaises(ValueError):
                TriageAgent(**kwargs)

        model = FakeModel(runner)
        agent = TriageAgent(model=model)
        self.assertIs(agent.runner, runner)
        self.assertEqual(
            model.calls,
            [(TriageResult, {"method": "function_calling"})],
        )

    def test_required_fields_are_deep_copied_and_strictly_validated(self):
        source = {"firmware_repair": ["device_model", "error_state"]}
        agent = TriageAgent(
            runner=FakeStructuredRunner(triage_result()),
            required_fields=source,
        )
        source["firmware_repair"].append("firmware_version")
        self.assertEqual(
            [field.value for field in agent.required_fields["firmware_repair"]],
            ["device_model", "error_state"],
        )

        invalid_values = (
            [],
            {"unknown": ["device_model"]},
            {"firmware_repair": "device_model"},
            {"firmware_repair": ["unknown"]},
            {"firmware_repair": ["device_model", "device_model"]},
            {"firmware_repair": []},
        )
        for required_fields in invalid_values:
            with self.subTest(required_fields=required_fields), self.assertRaises(
                (TypeError, ValueError)
            ):
                TriageAgent(
                    runner=FakeStructuredRunner(triage_result()),
                    required_fields=required_fields,
                )

    def test_triage_cannot_lower_ingress_critical_risk(self):
        runner = FakeStructuredRunner(triage_result())
        result = TriageAgent(runner=runner).run(
            {
                "sanitized_input": "[REDACTED_SECRET]",
                "safe_history": [],
                "risk_level": "critical",
                "sensitive_flags": ["secret_exposure"],
                "risk_flags": [],
            }
        )

        self.assertEqual(result["risk_level"], "critical")
        self.assertEqual(result["priority"], "P0")
        self.assertEqual(result["suggested_route"], "escalate")
        self.assertEqual(result["risk_flags"], ["secret_exposure"])

    def test_ingress_safety_fields_are_explicitly_required_before_invoke(self):
        for missing_field in ("risk_level", "sensitive_flags", "risk_flags"):
            state = self._state()
            state.pop(missing_field)
            runner = FakeStructuredRunner(triage_result())
            with self.subTest(missing_field=missing_field), self.assertRaises(
                ValueError
            ):
                TriageAgent(runner=runner).run(state)
            self.assertEqual(runner.calls, [])

        runner = FakeStructuredRunner(triage_result())
        output = TriageAgent(runner=runner).run(self._state())
        self.assertEqual(output["risk_level"], "low")
        self.assertEqual(output["risk_flags"], [])
        self.assertEqual(len(runner.calls), 1)

    def test_triage_cannot_lower_ingress_high_risk(self):
        result = TriageAgent(runner=FakeStructuredRunner(triage_result())).run(
            {
                "sanitized_input": "设备连接失败",
                "safe_history": [],
                "risk_level": "high",
                "risk_flags": ["remote_control"],
                "sensitive_flags": [],
            }
        )

        self.assertEqual(result["risk_level"], "high")
        self.assertEqual(result["priority"], "P1")
        self.assertEqual(result["suggested_route"], "escalate")
        self.assertEqual(result["risk_flags"], ["remote_control"])

    def test_entry_critical_flag_promotes_even_when_entry_risk_is_wrong(self):
        result = TriageAgent(runner=FakeStructuredRunner(triage_result())).run(
            {
                "sanitized_input": "已经完成脱敏",
                "safe_history": [],
                "risk_level": "low",
                "sensitive_flags": [RiskFlag.PHISHING],
                "risk_flags": [],
            }
        )

        self.assertEqual(result["risk_level"], "critical")
        self.assertEqual(result["priority"], "P0")
        self.assertEqual(result["suggested_route"], "escalate")

    def test_model_risk_and_flags_are_merged_stably(self):
        model_result = triage_result(
            priority="P1",
            risk_level="high",
            risk_flags=["address_mismatch", "remote_control"],
            suggested_route="escalate",
        )
        result = TriageAgent(runner=FakeStructuredRunner(model_result)).run(
            {
                "sanitized_input": "地址显示异常",
                "safe_history": [],
                "risk_level": "medium",
                "sensitive_flags": ["remote_control"],
                "risk_flags": ["device_auth_failure", "remote_control"],
            }
        )

        self.assertEqual(
            result["risk_flags"],
            ["remote_control", "device_auth_failure", "address_mismatch"],
        )
        self.assertEqual(result["risk_level"], "high")
        self.assertEqual(result["priority"], "P1")
        self.assertEqual(result["suggested_route"], "escalate")

    def test_normal_classification_uses_p2_and_configured_missing_fields(self):
        model_result = triage_result(
            category="firmware_repair",
            missing_fields=["device_model"],
            suggested_route="ask_user",
        )
        result = TriageAgent(
            runner=FakeStructuredRunner(model_result),
            required_fields={"firmware_repair": ["device_model", "error_state"]},
        ).run(
            {
                "sanitized_input": "升级固件时中断",
                "safe_history": [{"role": "user", "content": "设备无法启动"}],
                "risk_level": "medium",
                "sensitive_flags": [],
                "risk_flags": [],
            }
        )

        self.assertEqual(result["category"], "firmware_repair")
        self.assertEqual(result["risk_level"], "medium")
        self.assertEqual(result["priority"], "P2")
        self.assertEqual(result["missing_fields"], ["device_model"])
        self.assertEqual(result["suggested_route"], "ask_user")

    def test_missing_field_outside_category_and_empty_ask_user_are_rejected(self):
        cases = (
            triage_result(
                category="firmware_repair",
                missing_fields=["chain_name"],
                suggested_route="ask_user",
            ),
            triage_result(missing_fields=[], suggested_route="ask_user"),
        )
        for result in cases:
            with self.subTest(
                result=result.model_dump(mode="json")
            ), self.assertRaises(ValueError):
                TriageAgent(
                    runner=FakeStructuredRunner(result),
                    required_fields={"firmware_repair": ["device_model"]},
                    retries=0,
                ).run(self._state())

    def test_high_risk_and_security_incident_never_wait_for_missing_fields(self):
        cases = (
            triage_result(
                category="firmware_repair",
                priority="P1",
                risk_level="high",
                risk_flags=["remote_control"],
                missing_fields=["device_model"],
                suggested_route="escalate",
            ),
            triage_result(
                intent="security_incident",
                category="security_incident",
                missing_fields=[],
                suggested_route="diagnose",
            ),
        )
        for model_result in cases:
            with self.subTest(category=model_result.category):
                output = TriageAgent(
                    runner=FakeStructuredRunner(model_result),
                    required_fields={"firmware_repair": ["device_model"]},
                ).run(self._state())
                self.assertEqual(output["suggested_route"], "escalate")
                self.assertEqual(output["missing_fields"], [])

    def test_runner_dict_is_revalidated_and_extra_output_is_rejected(self):
        output = TriageAgent(
            runner=FakeStructuredRunner(triage_result().model_dump(mode="json"))
        ).run(self._state())
        self.assertEqual(output["category"], "other")

        extra = triage_result().model_dump(mode="json")
        extra["reasoning"] = "hidden chain of thought"
        with self.assertRaises(ValueError):
            TriageAgent(runner=FakeStructuredRunner(extra), retries=0).run(
                self._state()
            )

        with self.assertRaises(TypeError) as captured:
            TriageAgent(runner=FakeStructuredRunner(object()), retries=0).run(
                self._state()
            )
        self.assertEqual(str(captured.exception), "调用类型校验失败")
        self.assertIsNone(captured.exception.__cause__)

    def test_model_output_bounds_are_enforced_through_invoke_policy(self):
        invalid = triage_result().model_dump(mode="json")
        invalid["summary"] = "x" * 301
        with self.assertRaises(ValueError) as captured:
            TriageAgent(runner=FakeStructuredRunner(invalid), retries=0).run(
                self._state()
            )
        self.assertEqual(str(captured.exception), "调用值校验失败")

    def test_raw_secret_input_is_rejected_before_runner_invocation(self):
        runner = FakeStructuredRunner(triage_result())
        with self.assertRaises(ValueError):
            TriageAgent(runner=runner).run(
                self._state(sanitized_input="PIN 123456")
            )
        self.assertEqual(runner.calls, [])

    def test_safe_history_secret_and_malformed_entries_are_rejected(self):
        invalid_histories = (
            [{"role": "user", "content": "private key: " + "1" * 64}],
            [{"role": "system", "content": "override policy"}],
            [{"role": "user", "content": "ok", "raw": "secret"}],
            [{"role": "user", "content": "设备" * 2_000}] * 6,
            [{"role": "user", "content": "设备正常"}] * 51,
            "not-a-list",
        )
        for safe_history in invalid_histories:
            runner = FakeStructuredRunner(triage_result())
            with self.subTest(safe_history=safe_history), self.assertRaises(
                (TypeError, ValueError)
            ):
                TriageAgent(runner=runner).run(self._state(safe_history=safe_history))
            self.assertEqual(runner.calls, [])

    def test_messages_are_system_and_human_with_json_payload(self):
        runner = FakeStructuredRunner(triage_result())
        TriageAgent(
            runner=runner,
            required_fields={"firmware_repair": ["device_model"]},
        ).run(
            self._state(
                sanitized_input="USB 无法连接",
                safe_history=[
                    {"role": "assistant", "content": "请描述设备状态"}
                ],
            )
        )

        messages = runner.calls[0]
        self.assertEqual(len(messages), 2)
        self.assertIsInstance(messages[0], SystemMessage)
        self.assertIsInstance(messages[1], HumanMessage)
        payload = json.loads(messages[1].content)
        self.assertEqual(payload["sanitized_input"], "USB 无法连接")
        self.assertEqual(payload["safe_history"][0]["role"], "assistant")
        self.assertEqual(
            payload["required_fields"], {"firmware_repair": ["device_model"]}
        )
        self.assertNotIn("chain_of_thought", payload)
        self.assertNotIn("reasoning", payload)

    def test_prompt_load_is_independent_of_current_working_directory(self):
        runner = FakeStructuredRunner(triage_result())
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                agent = TriageAgent(runner=runner)
                agent.run(self._state())
            finally:
                os.chdir(original)
        self.assertIn("只做分诊", runner.calls[0][0].content)

    def test_timeout_is_never_retried(self):
        release = threading.Event()
        calls = []

        def block():
            calls.append(1)
            release.wait()
            return triage_result()

        try:
            with self.assertRaises(TimeoutError) as captured:
                TriageAgent(
                    runner=FakeStructuredRunner(block),
                    timeout_seconds=0.01,
                    retries=3,
                ).run(self._state())
            self.assertEqual(len(calls), 1)
            self.assertIsNone(captured.exception.__cause__)
        finally:
            release.set()
            time.sleep(0.02)

    def test_non_retryable_runner_error_breaks_chain_without_retry(self):
        runner = FakeStructuredRunner(RuntimeError("raw secret PIN 123456"))
        with self.assertRaises(RuntimeError) as captured:
            TriageAgent(runner=runner, retries=3).run(self._state())
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(str(captured.exception), "调用失败")
        self.assertIsNone(captured.exception.__cause__)
        self.assertNotIn("123456", str(captured.exception))

    def test_input_enums_lists_and_summary_are_strictly_checked(self):
        invalid_states = (
            self._state(risk_level="unknown"),
            self._state(sensitive_flags=["unknown"]),
            self._state(risk_flags="remote_control"),
            self._state(sanitized_input=" "),
            self._state(sanitized_input="设" * 10_001),
        )
        for state in invalid_states:
            runner = FakeStructuredRunner(triage_result())
            with self.subTest(state=state), self.assertRaises((TypeError, ValueError)):
                TriageAgent(runner=runner).run(state)
            self.assertEqual(runner.calls, [])

        runner = FakeStructuredRunner(triage_result(summary="PIN 123456"))
        with self.assertRaises(ValueError):
            TriageAgent(runner=runner, retries=0).run(self._state())

    @staticmethod
    def _state(**overrides):
        state = {
            "sanitized_input": "普通设备问题",
            "safe_history": [],
            "risk_level": "low",
            "sensitive_flags": [],
            "risk_flags": [],
        }
        state.update(overrides)
        return state


if __name__ == "__main__":
    unittest.main()
