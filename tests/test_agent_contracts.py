import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import HumanMessage, SystemMessage

import agent.nodes.triage as triage_module
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
    def test_diagnosis_filters_tool_plan_by_category(self):
        from agent.nodes.diagnosis import allowed_tools

        self.assertEqual(
            allowed_tools("warranty_service"),
            {"knowledge_search", "profile", "warranty"},
        )
        self.assertNotIn("chain_status", allowed_tools("warranty_service"))

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
            {},
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

    def test_default_required_fields_load_validated_orchestration_config(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                agent = TriageAgent(
                    runner=FakeStructuredRunner(triage_result())
                )
            finally:
                os.chdir(original)

        self.assertEqual(
            {
                category: [field.value for field in fields]
                for category, fields in agent.required_fields.items()
            },
            {
                "firmware_repair": ["device_model", "error_state"],
                "warranty_service": ["serial_last4"],
                "transaction_boundary": ["transaction_hash", "chain_name"],
            },
        )

    def test_prompt_rejects_oversize_controls_and_secrets_without_echo(self):
        invalid_prompts = (
            "x" * (16 * 1024 + 1),
            "triage\x00POLICY_PAYLOAD",
            "triage\rPOLICY_PAYLOAD",
            "triage\x85POLICY_PAYLOAD",
            "private key: " + "1" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            prompt_path = Path(directory) / "triage_prompt.txt"
            for prompt in invalid_prompts:
                prompt_path.write_text(prompt, encoding="utf-8")
                with self.subTest(prompt_length=len(prompt)), patch.object(
                    triage_module, "_PROMPT_PATH", prompt_path
                ), self.assertRaises((ValueError, RuntimeError)) as captured:
                    TriageAgent(
                        runner=FakeStructuredRunner(triage_result()),
                        required_fields={"firmware_repair": ["device_model"]},
                    )
                self.assertNotIn("POLICY_PAYLOAD", str(captured.exception))
                self.assertNotIn("111111", str(captured.exception))

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

    def test_summary_whitespace_is_normalized_and_controls_fail_closed(self):
        normalized = TriageAgent(
            runner=FakeStructuredRunner(
                triage_result(summary=" 第一行\n\t第二行  ")
            )
        ).run(self._state())
        self.assertEqual(normalized["summary"], "第一行 第二行")

        invalid_summaries = ("   ", "摘要\x00RAW_PAYLOAD", "摘要\rRAW_PAYLOAD")
        for summary in invalid_summaries:
            raw = triage_result().model_dump(mode="json")
            raw["summary"] = summary
            with self.subTest(summary=repr(summary)), self.assertRaises(
                ValueError
            ) as captured:
                TriageAgent(
                    runner=FakeStructuredRunner(raw), retries=0
                ).run(self._state())
            self.assertNotIn("RAW_PAYLOAD", str(captured.exception))

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


class DiagnosisContractTest(unittest.TestCase):
    @staticmethod
    def _evidence(**overrides):
        values = {
            "evidence_id": "kb:usb:1",
            "kind": "knowledge",
            "content": "请重新连接原装数据线。",
            "source_title": "连接帮助",
            "source_url": None,
        }
        values.update(overrides)
        return values

    @staticmethod
    def _answer(**overrides):
        values = {
            "outcome": "draft",
            "diagnosis_summary": "证据支持进行连接排查",
            "recommended_actions": [
                {
                    "action_code": "generic_troubleshooting",
                    "text": "重新连接原装数据线",
                    "evidence_refs": ["kb:usb:1"],
                }
            ],
            "evidence_refs": ["kb:usb:1"],
            "citations": [],
            "draft_answer": "请重新连接原装数据线。",
            "remaining_unknowns": [],
        }
        values.update(overrides)
        return values

    @staticmethod
    def _state(**overrides):
        values = {
            "sanitized_input": "设备无法连接",
            "category": "usb_connection",
            "risk_level": "low",
            "risk_flags": [],
            "missing_fields": [],
            "requires_human": False,
            "user_id": "1001",
        }
        values.update(overrides)
        return values

    def _agent(self, plan=None, answer=None, tools=None, **kwargs):
        from agent.nodes.diagnosis import DiagnosisAgent

        plan = (
            {"requests": [{"name": "knowledge_search", "query": "USB"}]}
            if plan is None
            else plan
        )
        answer = self._answer() if answer is None else answer
        tools = (
            {"knowledge_search": lambda state, query: [self._evidence()]}
            if tools is None
            else tools
        )
        return DiagnosisAgent(
            plan_runner=FakeStructuredRunner(plan),
            answer_runner=FakeStructuredRunner(answer),
            tool_registry=tools,
            **kwargs,
        )

    def test_allowed_tools_are_exact_and_unknown_category_is_rejected(self):
        from agent.nodes.diagnosis import allowed_tools

        self.assertEqual(allowed_tools("other"), {"knowledge_search", "profile"})
        self.assertEqual(
            allowed_tools("transaction_boundary"),
            {"knowledge_search", "profile", "chain_status"},
        )
        self.assertEqual(allowed_tools("security_incident"), set())
        self.assertEqual(allowed_tools("security_report"), set())
        with self.assertRaises(ValueError):
            allowed_tools("made_up")

    def test_plan_models_are_strict_safe_and_deduplicated(self):
        from agent.nodes.diagnosis import DiagnosisPlan, ToolRequest

        self.assertEqual(ToolRequest(name="warranty", query=" a1b2 ").query, "a1b2")
        invalid_requests = (
            {"name": "write_device", "query": "x"},
            {"name": "profile", "query": ""},
            {"name": "profile", "query": "x" * 301},
            {"name": "profile", "query": "x\x00hidden"},
            {"name": "profile", "query": "private key: " + "1" * 64},
            {"name": "profile", "query": "x", "reasoning": "hidden"},
        )
        for request in invalid_requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                ToolRequest.model_validate(request)
        with self.assertRaises(ValueError):
            DiagnosisPlan(
                requests=[
                    ToolRequest(name="profile", query="1001"),
                    ToolRequest(name="profile", query="1002"),
                ]
            )
        with self.assertRaises(ValueError):
            DiagnosisPlan.model_validate({"requests": [], "reasoning": "hidden"})

    def test_constructor_supports_model_or_two_runners_and_validates_registry(self):
        from agent.nodes.diagnosis import DiagnosisAgent, DiagnosisPlan
        from agent.orchestration.state import DiagnosisResult

        plan_runner = FakeStructuredRunner({"requests": []})
        answer_runner = FakeStructuredRunner(self._answer())

        class Model:
            def __init__(self):
                self.calls = []

            def with_structured_output(inner_self, schema, **kwargs):
                inner_self.calls.append((schema, kwargs))
                return plan_runner if schema is DiagnosisPlan else answer_runner

        model = Model()
        agent = DiagnosisAgent(model=model, tool_registry={"profile": lambda *_: []})
        self.assertIs(agent.plan_runner, plan_runner)
        self.assertEqual(
            model.calls,
            [
                (DiagnosisPlan, {"method": "function_calling"}),
                (DiagnosisResult, {"method": "function_calling"}),
            ],
        )
        invalid = (
            {},
            {"model": model, "plan_runner": plan_runner, "answer_runner": answer_runner},
            {"plan_runner": plan_runner},
        )
        for constructor in invalid:
            with self.subTest(constructor=constructor), self.assertRaises(ValueError):
                DiagnosisAgent(tool_registry={"profile": lambda *_: []}, **constructor)
        for registry in ({}, {"unknown": lambda *_: []}, {"profile": object()}):
            with self.subTest(registry=registry), self.assertRaises(ValueError):
                DiagnosisAgent(
                    plan_runner=plan_runner,
                    answer_runner=answer_runner,
                    tool_registry=registry,
                )

    def test_high_risk_human_security_and_missing_fields_short_circuit(self):
        cases = (
            (self._state(risk_level="high"), "escalate"),
            (self._state(risk_flags=["remote_control"]), "escalate"),
            (self._state(requires_human=True), "escalate"),
            (self._state(category="security_report"), "escalate"),
            (self._state(missing_fields=["device_model"]), "need_user"),
        )
        for state, expected in cases:
            plan = FakeStructuredRunner(AssertionError("model must not run"))
            answer = FakeStructuredRunner(AssertionError("model must not run"))
            tool_calls = []
            from agent.nodes.diagnosis import DiagnosisAgent

            agent = DiagnosisAgent(
                plan_runner=plan,
                answer_runner=answer,
                tool_registry={"knowledge_search": lambda *_: tool_calls.append(1)},
            )
            with self.subTest(expected=expected, state=state):
                result = agent.run(state)
                self.assertEqual(result["outcome"], expected)
                self.assertEqual(result["draft_answer"], "")
                self.assertEqual(plan.calls, [])
                self.assertEqual(answer.calls, [])
                self.assertEqual(tool_calls, [])

    def test_plan_and_answer_messages_are_json_without_reasoning(self):
        agent = self._agent()
        output = agent.run(self._state())
        plan_payload = json.loads(agent.plan_runner.calls[0][1].content)
        answer_payload = json.loads(agent.answer_runner.calls[0][1].content)
        self.assertEqual(plan_payload["sanitized_input"], "设备无法连接")
        self.assertEqual(
            plan_payload["allowed_tools"], ["knowledge_search", "profile"]
        )
        self.assertEqual(
            set(answer_payload), {"sanitized_input", "triage", "evidence"}
        )
        self.assertEqual(answer_payload["sanitized_input"], "设备无法连接")
        self.assertNotIn("reasoning", json.dumps(answer_payload))
        self.assertEqual(output["tool_errors"], [])

    def test_disallowed_missing_registry_and_invalid_special_queries_fail_closed(self):
        cases = (
            (
                self._state(category="warranty_service"),
                {"requests": [{"name": "chain_status", "query": "ETH"}]},
                {"chain_status": lambda *_: [self._evidence()]},
            ),
            (
                self._state(category="warranty_service"),
                {"requests": [{"name": "warranty", "query": "A1B2"}]},
                {"profile": lambda *_: [self._evidence()]},
            ),
            (
                self._state(category="warranty_service"),
                {"requests": [{"name": "warranty", "query": "12345"}]},
                {"warranty": lambda *_: [self._evidence()]},
            ),
            (
                self._state(category="transaction_boundary"),
                {"requests": [{"name": "chain_status", "query": "DOGE"}]},
                {"chain_status": lambda *_: [self._evidence()]},
            ),
        )
        for state, plan, tools in cases:
            with self.subTest(plan=plan), self.assertRaises(ValueError) as captured:
                self._agent(plan=plan, tools=tools, retries=0).run(state)
            self.assertNotIn("DOGE", str(captured.exception))

    def test_warranty_and_chain_queries_are_normalized_before_call(self):
        calls = []
        warranty_evidence = self._evidence(
            evidence_id="warranty:A1B2", kind="warranty"
        )
        answer = self._answer(
            recommended_actions=[
                {
                    "action_code": "warranty_decision",
                    "text": "检查模拟保修记录",
                    "evidence_refs": ["warranty:A1B2"],
                }
            ],
            evidence_refs=["warranty:A1B2"],
        )
        self._agent(
            plan={"requests": [{"name": "warranty", "query": "a1b2"}]},
            answer=answer,
            tools={"warranty": lambda state, query: calls.append(query) or [warranty_evidence]},
        ).run(self._state(category="warranty_service"))
        chain_evidence = self._evidence(evidence_id="chain:ETH", kind="chain")
        chain_answer = self._answer(
            recommended_actions=[
                {
                    "action_code": "transaction_check",
                    "text": "检查模拟链状态",
                    "evidence_refs": ["chain:ETH"],
                }
            ],
            evidence_refs=["chain:ETH"],
        )
        self._agent(
            plan={"requests": [{"name": "chain_status", "query": "以太坊"}]},
            answer=chain_answer,
            tools={"chain_status": lambda state, query: calls.append(query) or [chain_evidence]},
        ).run(self._state(category="transaction_boundary"))
        self.assertEqual(calls, ["A1B2", "ETH"])

    def test_tool_exception_and_no_evidence_escalate_without_answer(self):
        for tool, code in (
            (
                lambda *_: (_ for _ in ()).throw(
                    RuntimeError("raw PIN 123456")
                ),
                "tool_failure:knowledge_search",
            ),
            (lambda *_: [], "no_evidence"),
        ):
            agent = self._agent(tools={"knowledge_search": tool})
            output = agent.run(self._state())
            self.assertEqual(output["outcome"], "escalate")
            self.assertEqual(output["tool_errors"], [code])
            self.assertEqual(agent.answer_runner.calls, [])
            self.assertNotIn("123456", json.dumps(output))

    def test_evidence_conflicts_limits_secrets_and_extra_fields_are_rejected(self):
        conflict_plan = {
            "requests": [
                {"name": "knowledge_search", "query": "USB"},
                {"name": "profile", "query": "1001"},
            ]
        }
        conflict_tools = {
            "knowledge_search": lambda *_: [self._evidence(content="one")],
            "profile": lambda *_: [self._evidence(content="two")],
        }
        cases = (
            (conflict_plan, conflict_tools),
            (
                None,
                {
                    "knowledge_search": lambda *_: [
                        self._evidence(evidence_id=f"kb:{i}")
                        for i in range(17)
                    ]
                },
            ),
            (
                None,
                {
                    "knowledge_search": lambda *_: [
                        self._evidence(content="private key: " + "1" * 64)
                    ]
                },
            ),
            (
                None,
                {"knowledge_search": lambda *_: [dict(self._evidence(), reasoning="hidden")]},
            ),
        )
        for plan, tools in cases:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                self._agent(plan=plan, tools=tools, retries=0).run(self._state())

    def test_evidence_source_url_rejects_unsafe_or_invalid_authorities(self):
        unsafe_urls = (
            "http://support.example/help",
            "https://user@support.example/help",
            "https://support.example:444/help",
            "https://localhost/help",
            "https://127.0.0.1/help",
            "https://support.example /help",
            "https://support.example\\@evil.example/help",
            "https://support.example/%0aevil",
        )
        for source_url in unsafe_urls:
            tools = {
                "knowledge_search": lambda *_, source_url=source_url: [
                    self._evidence(source_url=source_url)
                ]
            }
            with self.subTest(source_url=source_url), self.assertRaises(ValueError):
                self._agent(tools=tools, retries=0).run(self._state())

    def test_unknown_refs_citation_mismatch_missing_citation_and_none_url_rejected(self):
        evidence = self._evidence(source_url="https://support.example/help")
        base_tools = {"knowledge_search": lambda *_: [evidence]}
        cases = (
            self._answer(
                evidence_refs=["kb:unknown"],
                recommended_actions=[
                    {
                        "action_code": "generic_troubleshooting",
                        "text": "安全操作",
                        "evidence_refs": ["kb:unknown"],
                    }
                ],
            ),
            self._answer(
                citations=[
                    {
                        "source_id": "kb:usb:1",
                        "source_title": "错误标题",
                        "source_url": "https://support.example/help",
                    }
                ]
            ),
            self._answer(citations=[]),
        )
        for answer in cases:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                self._agent(answer=answer, tools=base_tools, retries=0).run(self._state())
        none_url_answer = self._answer(
            citations=[
                {
                    "source_id": "kb:usb:1",
                    "source_title": "连接帮助",
                    "source_url": "https://invented.example",
                }
            ]
        )
        with self.assertRaises(ValueError):
            self._agent(answer=none_url_answer, retries=0).run(self._state())

    def test_runner_dict_extra_prompt_cwd_input_safety_and_tool_state_copy(self):
        extra_plan = {"requests": [], "reasoning": "hidden"}
        with self.assertRaises(ValueError):
            self._agent(plan=extra_plan, retries=0).run(self._state())

        original = Path.cwd()
        agent = None
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                agent = self._agent()
                agent.run(self._state())
            finally:
                os.chdir(original)
        self.assertIn("只依据工具返回证据", agent.prompt)

        import agent.nodes.diagnosis as diagnosis_module

        with tempfile.TemporaryDirectory() as directory:
            prompt_target = Path(directory) / "target.txt"
            prompt_target.write_text("只依据安全证据生成结构化诊断。", encoding="utf-8")
            prompt_link = Path(directory) / "linked-prompt.txt"
            prompt_link.symlink_to(prompt_target)
            with patch.object(
                diagnosis_module, "_PROMPT_PATH", prompt_link
            ), self.assertRaises(RuntimeError):
                self._agent()

        invalid_states = (
            self._state(sanitized_input="x\x00hidden"),
            self._state(sanitized_input="private key: " + "1" * 64),
            self._state(category="unknown"),
            self._state(risk_level="unknown"),
            self._state(risk_flags="remote_control"),
            self._state(missing_fields=["unknown"]),
            self._state(requires_human=1),
        )
        for state in invalid_states:
            with self.subTest(state=state), self.assertRaises((TypeError, ValueError)):
                self._agent().run(state)

        state = self._state()

        def mutate(tool_state, query):
            tool_state["user_id"] = "changed"
            return [self._evidence()]

        self._agent(tools={"knowledge_search": mutate}).run(state)
        self.assertEqual(state["user_id"], "1001")

        normalized_answer = self._answer()
        normalized_answer["recommended_actions"][0]["text"] = "  重新\n连接原装数据线  "
        normalized = self._agent(answer=normalized_answer).run(self._state())
        self.assertEqual(
            normalized["recommended_actions"][0]["text"], "重新 连接原装数据线"
        )

    def test_valid_https_knowledge_citation_is_bound_exactly(self):
        source_url = "https://support.example/help"
        evidence = self._evidence(source_url=source_url)
        answer = self._answer(
            citations=[
                {
                    "source_id": "kb:usb:1",
                    "source_title": "连接帮助",
                    "source_url": source_url,
                }
            ]
        )
        output = self._agent(
            answer=answer,
            tools={"knowledge_search": lambda *_: [evidence]},
        ).run(self._state())
        self.assertEqual(output["citations"][0]["source_url"], source_url)

    def test_timeout_is_not_retried_and_successful_warranty_draft_is_bound(self):
        release = threading.Event()
        calls = []

        def block():
            calls.append(1)
            release.wait()
            return {"requests": []}

        try:
            with self.assertRaises(TimeoutError):
                self._agent(
                    plan=block,
                    timeout_seconds=0.01,
                    retries=3,
                ).run(self._state())
            self.assertEqual(calls, [1])
        finally:
            release.set()
            time.sleep(0.02)

        evidence = self._evidence(
            evidence_id="warranty:A1B2",
            kind="warranty",
            content="模拟保修记录有效",
            source_title="模拟保修记录",
        )
        answer = self._answer(
            diagnosis_summary="模拟保修记录支持继续核验",
            recommended_actions=[
                {
                    "action_code": "warranty_decision",
                    "text": "核验购买凭证",
                    "evidence_refs": ["warranty:A1B2"],
                }
            ],
            evidence_refs=["warranty:A1B2"],
            draft_answer="模拟记录显示可继续核验购买凭证。",
        )
        output = self._agent(
            plan={"requests": [{"name": "warranty", "query": "a1b2"}]},
            answer=answer,
            tools={"warranty": lambda *_: [evidence]},
        ).run(self._state(category="warranty_service"))
        self.assertEqual(output["outcome"], "draft")
        self.assertEqual(output["evidence_refs"], ["warranty:A1B2"])
        self.assertEqual(output["evidence"], [evidence])
        self.assertEqual(output["tool_errors"], [])


if __name__ == "__main__":
    unittest.main()
