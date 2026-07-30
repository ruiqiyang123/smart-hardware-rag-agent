import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command


def _initial_state(**overrides):
    state = {
        "ticket_id": "ticket-1",
        "request_id": "request-1",
        "command_id": "command-1",
        "event_step": 0,
        "sanitized_input": "设备无法开机",
        "safe_history": [],
        "sensitive_flags": [],
        "risk_level": "low",
        "risk_flags": [],
        "status": "new",
        "requires_human": False,
        "revision_count": 0,
        "response_version": 0,
        "status_events": [],
    }
    state.update(overrides)
    return state


def _triage(_state):
    return {
        "intent": "troubleshoot",
        "category": "power",
        "priority": "P2",
        "risk_level": "low",
        "risk_flags": [],
        "missing_fields": [],
        "suggested_route": "diagnose",
        "summary": "低风险电源问题",
    }


def _diagnosis(_state):
    return {
        "outcome": "draft",
        "diagnosis_summary": "建议检查供电连接",
        "recommended_actions": [
            {
                "action_code": "generic_troubleshooting",
                "text": "更换可信电源线后重试",
                "evidence_refs": ["kb-1"],
            }
        ],
        "evidence_refs": ["kb-1"],
        "evidence": [
            {
                "evidence_id": "kb-1",
                "kind": "knowledge",
                "content": "供电故障排查说明",
                "source_title": "官方帮助",
                "source_url": "https://support.example.test/power",
            }
        ],
        "citations": [
            {
                "source_id": "kb-1",
                "source_title": "官方帮助",
                "source_url": "https://support.example.test/power",
            }
        ],
        "draft_answer": "请先更换可信电源线，然后重新连接设备。",
        "remaining_unknowns": [],
        "tool_errors": [],
    }


def _review(_state):
    return {
        "review_decision": "approve",
        "review_reasons": ["passed"],
        "review_issues": [],
        "required_changes": [],
    }


class HumanInLoopTest(unittest.TestCase):
    def test_connection_evidence_drops_firmware_only_chunks_without_upgrade_context(self):
        from agent.orchestration.graph import _filter_connection_evidence

        evidence = [
            {"evidence_id": "kb:故障排除.txt:usb", "source_title": "故障排除.txt"},
            {"evidence_id": "kb:固件升级.txt:11", "source_title": "固件升级.txt"},
        ]
        state = {
            "category": "usb_connection",
            "sanitized_input": "电脑识别不到设备，换线后仍无反应",
        }
        self.assertEqual(
            _filter_connection_evidence(state, evidence), evidence[:1]
        )
        self.assertEqual(
            _filter_connection_evidence(
                {**state, "sanitized_input": "固件升级后电脑识别不到设备"}, evidence
            ),
            evidence,
        )

    def test_support_graph_module_exists(self):
        from agent.orchestration.graph import build_support_graph

        self.assertTrue(callable(build_support_graph))

    def _graph(self, *, triage=_triage, policy_guard=lambda _text, _citations: True):
        from agent.orchestration.graph import build_support_graph

        return build_support_graph(
            triage=triage,
            diagnosis=_diagnosis,
            review=_review,
            policy_guard=policy_guard,
            checkpointer=MemorySaver(),
        )

    def test_low_risk_ticket_is_reviewed_and_resolved(self):
        result = self._graph().invoke(
            _initial_state(),
            {"configurable": {"thread_id": "low-risk"}},
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["outcome"], "draft")
        self.assertEqual(
            result["final_answer"], "请先更换可信电源线，然后重新连接设备。"
        )
        self.assertEqual(result["response_version"], 1)

    def test_critical_ticket_skips_triage_and_interrupts_for_human(self):
        calls = []

        def triage(state):
            calls.append(state)
            return _triage(state)

        result = self._graph(triage=triage).invoke(
            _initial_state(
                risk_level="critical",
                risk_flags=["secret_exposure"],
                sensitive_flags=["secret_exposure"],
            ),
            {"configurable": {"thread_id": "critical"}},
        )

        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "escalated")
        self.assertIn("__interrupt__", result)

    def test_human_can_edit_and_send_a_critical_ticket(self):
        graph = self._graph()
        config = {"configurable": {"thread_id": "human-edit"}}
        first = graph.invoke(
            _initial_state(risk_level="critical", risk_flags=["phishing"]),
            config,
        )
        self.assertIn("__interrupt__", first)

        result = graph.invoke(
            Command(
                resume={
                    "command_id": "human-command-1",
                    "action": "edit_send",
                    "edited_answer": "请停止操作，并通过设备厂商官方渠道联系支持。",
                }
            ),
            config,
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(
            result["final_answer"],
            "请停止操作，并通过设备厂商官方渠道联系支持。",
        )
        self.assertEqual(result["command_id"], "human-command-1")
        self.assertEqual(result["event_step"], 1)

    def test_invalid_human_edit_is_sanitized_and_reinterrupts(self):
        graph = self._graph()
        config = {"configurable": {"thread_id": "invalid-human-edit"}}
        graph.invoke(_initial_state(risk_level="critical"), config)

        rejected = graph.invoke(
            Command(
                resume={
                    "command_id": "bad-command",
                    "action": "edit_send",
                    "edited_answer": "seed phrase: abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
                }
            ),
            config,
        )

        self.assertEqual(rejected["status"], "escalated")
        self.assertEqual(rejected["last_error"], "INVALID_HUMAN_RESUME")
        self.assertIn("__interrupt__", rejected)
        self.assertNotIn("abandon abandon", str(rejected))
        self.assertEqual(rejected["status_events"][-1]["event_type"], "resume_rejected")

    def test_ask_user_then_sanitized_resume_reenters_triage(self):
        triage_calls = []

        def triage(state):
            triage_calls.append(state["sanitized_input"])
            return _triage(state)

        graph = self._graph(triage=triage)
        config = {"configurable": {"thread_id": "ask-user"}}
        graph.invoke(_initial_state(status="escalated"), config)

        waiting = graph.invoke(
            Command(
                resume={
                    "command_id": "human-ask-1",
                    "action": "ask_user",
                    "missing_fields": ["device_model"],
                }
            ),
            config,
        )
        self.assertEqual(waiting["status"], "pending_user")
        self.assertIn("__interrupt__", waiting)

        result = graph.invoke(
            Command(
                resume={
                    "command_id": "user-reply-1",
                    "request_id": "request-2",
                    "sanitized_input": "设备型号为 Nano X，当前无法开机",
                    "sensitive_flags": [],
                    "risk_flags": [],
                    "risk_level": "low",
                }
            ),
            config,
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(triage_calls, ["设备型号为 Nano X，当前无法开机"])
        self.assertGreaterEqual(result["event_step"], 2)

    def test_user_resume_cannot_downgrade_critical_human_gate(self):
        triage_calls = []

        def triage(state):
            triage_calls.append(state)
            return _triage(state)

        graph = self._graph(triage=triage)
        config = {"configurable": {"thread_id": "critical-ask-user"}}
        graph.invoke(
            _initial_state(
                risk_level="critical",
                risk_flags=["phishing", "asset_loss"],
                sensitive_flags=["phishing"],
                requires_human=True,
            ),
            config,
        )
        waiting = graph.invoke(
            Command(
                resume={
                    "command_id": "critical-human-ask-1",
                    "action": "ask_user",
                    "missing_fields": ["device_model"],
                }
            ),
            config,
        )
        self.assertEqual(waiting["status"], "pending_user")

        result = graph.invoke(
            Command(
                resume={
                    "command_id": "critical-user-reply-1",
                    "request_id": "critical-request-2",
                    "sanitized_input": "设备型号为 Nano X",
                    "sensitive_flags": [],
                    "risk_flags": [],
                    "risk_level": "low",
                }
            ),
            config,
        )

        self.assertEqual(result["status"], "escalated")
        self.assertEqual(result["risk_level"], "critical")
        self.assertEqual(result["risk_flags"], ["phishing", "asset_loss"])
        self.assertEqual(result["sensitive_flags"], ["phishing"])
        self.assertTrue(result["requires_human"])
        self.assertEqual(triage_calls, [])
        self.assertIn("__interrupt__", result)

    def test_user_resume_merges_flags_stably_and_applies_flag_floor(self):
        graph = self._graph()
        config = {"configurable": {"thread_id": "resume-risk-merge"}}
        graph.invoke(
            _initial_state(
                risk_level="high",
                risk_flags=["device_auth_failure", "address_mismatch"],
                sensitive_flags=["device_auth_failure"],
                requires_human=True,
            ),
            config,
        )
        graph.invoke(
            Command(
                resume={
                    "command_id": "merge-human-ask-1",
                    "action": "ask_user",
                    "missing_fields": ["device_model"],
                }
            ),
            config,
        )
        result = graph.invoke(
            Command(
                resume={
                    "command_id": "merge-user-reply-1",
                    "request_id": "merge-request-2",
                    "sanitized_input": "设备型号为 Nano X",
                    "sensitive_flags": ["remote_control"],
                    "risk_flags": ["address_mismatch", "phishing"],
                    "risk_level": "low",
                }
            ),
            config,
        )

        self.assertEqual(result["risk_level"], "critical")
        self.assertEqual(
            result["risk_flags"],
            ["device_auth_failure", "address_mismatch", "phishing"],
        )
        self.assertEqual(
            result["sensitive_flags"],
            ["device_auth_failure", "remote_control"],
        )
        self.assertTrue(result["requires_human"])
        self.assertEqual(result["status"], "escalated")
        self.assertIn("__interrupt__", result)

    def test_review_allows_exactly_one_revision_with_feedback(self):
        diagnosis_states = []
        review_calls = []

        def diagnosis(state):
            diagnosis_states.append(state)
            return _diagnosis(state)

        def review(_state):
            review_calls.append(True)
            if len(review_calls) == 1:
                return {
                    "review_decision": "revise",
                    "review_reasons": ["incomplete_steps"],
                    "review_issues": ["缺少重连步骤"],
                    "required_changes": ["补充重连步骤"],
                }
            return _review(_state)

        from agent.orchestration.graph import build_support_graph

        graph = build_support_graph(
            triage_node=_triage,
            diagnosis_node=diagnosis,
            review_node=review,
            policy_guard=lambda _text, _citations: True,
            checkpointer=MemorySaver(),
        )
        result = graph.invoke(
            _initial_state(),
            {"configurable": {"thread_id": "one-revision"}},
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["revision_count"], 1)
        self.assertEqual(len(diagnosis_states), 2)
        self.assertEqual(diagnosis_states[1]["review_reasons"], ["incomplete_steps"])
        self.assertEqual(diagnosis_states[1]["required_changes"], ["补充重连步骤"])

    def test_review_exception_and_final_policy_veto_fail_closed(self):
        def broken_review(_state):
            raise RuntimeError("provider leaked internal details")

        from agent.orchestration.graph import build_support_graph

        broken = build_support_graph(
            triage_node=_triage,
            diagnosis_node=_diagnosis,
            review_node=broken_review,
            policy_guard=lambda _text, _citations: True,
            checkpointer=MemorySaver(),
        ).invoke(
            _initial_state(),
            {"configurable": {"thread_id": "review-failure"}},
        )
        self.assertEqual(broken["status"], "escalated")
        self.assertEqual(broken["last_error"], "REVIEW_FAILURE")
        self.assertEqual(broken.get("final_answer", ""), "")
        self.assertNotIn("provider leaked", str(broken))

        vetoed = self._graph(policy_guard=lambda _text, _citations: False).invoke(
            _initial_state(),
            {"configurable": {"thread_id": "policy-veto"}},
        )
        self.assertEqual(vetoed["status"], "escalated")
        self.assertEqual(vetoed["last_error"], "FINAL_POLICY_FAILURE")
        self.assertEqual(vetoed.get("final_answer", ""), "")
        self.assertIn("__interrupt__", vetoed)

    def test_node_fault_codes_distinguish_timeout_no_evidence_and_tool_failure(self):
        from agent.orchestration.graph import build_support_graph

        def timed_out_triage(_state):
            raise TimeoutError("private provider request body")

        triage_timeout = build_support_graph(
            triage_node=timed_out_triage,
            diagnosis_node=_diagnosis,
            review_node=_review,
            policy_guard=lambda _text, _citations: True,
            checkpointer=MemorySaver(),
        ).invoke(
            _initial_state(),
            {"configurable": {"thread_id": "triage-timeout"}},
        )
        self.assertEqual(triage_timeout["status"], "escalated")
        self.assertEqual(triage_timeout["last_error"], "TRIAGE_TIMEOUT")
        self.assertEqual(triage_timeout.get("final_answer", ""), "")
        self.assertNotIn("private provider", str(triage_timeout))

        for name, tool_errors, error_code in (
            ("no-evidence", ["no_evidence"], "DIAGNOSIS_NO_EVIDENCE"),
            (
                "tool-failure",
                ["tool_failure:knowledge_search"],
                "TOOL_FAILURE",
            ),
        ):
            with self.subTest(name=name):
                def failed_diagnosis(_state, tool_errors=tool_errors):
                    result = _diagnosis(_state)
                    # A dependency must not smuggle a tool error through a
                    # superficially valid draft outcome.
                    result["tool_errors"] = tool_errors
                    return result

                failed = build_support_graph(
                    triage_node=_triage,
                    diagnosis_node=failed_diagnosis,
                    review_node=_review,
                    policy_guard=lambda _text, _citations: True,
                    checkpointer=MemorySaver(),
                ).invoke(
                    _initial_state(),
                    {"configurable": {"thread_id": name}},
                )
                self.assertEqual(failed["status"], "escalated")
                self.assertEqual(failed["last_error"], error_code)
                self.assertEqual(failed.get("final_answer", ""), "")

    def test_sqlite_checkpoint_recovers_interrupt_after_rebuild(self):
        from agent.orchestration.graph import build_support_graph, sqlite_checkpointer

        with TemporaryDirectory() as directory:
            path = Path(directory) / "support.sqlite"
            config = {"configurable": {"thread_id": "sqlite-recovery"}}
            first_saver = sqlite_checkpointer(path)
            first_graph = build_support_graph(
                triage_node=_triage,
                diagnosis_node=_diagnosis,
                review_node=_review,
                policy_guard=lambda _text, _citations: True,
                checkpointer=first_saver,
            )
            first = first_graph.invoke(
                _initial_state(risk_level="critical"), config
            )
            self.assertIn("__interrupt__", first)
            first_saver.close()

            second_saver = sqlite_checkpointer(path)
            try:
                second_graph = build_support_graph(
                    triage_node=_triage,
                    diagnosis_node=_diagnosis,
                    review_node=_review,
                    policy_guard=lambda _text, _citations: True,
                    checkpointer=second_saver,
                )
                result = second_graph.invoke(
                    Command(
                        resume={
                            "command_id": "sqlite-human-1",
                            "action": "edit_send",
                            "edited_answer": "请通过设备厂商官方支持渠道继续处理。",
                        }
                    ),
                    config,
                )
                self.assertEqual(result["status"], "resolved")
                self.assertEqual(result["command_id"], "sqlite-human-1")
            finally:
                second_saver.close()

    def test_sqlite_checkpointer_supports_multiple_worker_threads(self):
        from agent.orchestration.graph import build_support_graph, sqlite_checkpointer

        with TemporaryDirectory() as directory:
            saver = sqlite_checkpointer(Path(directory) / "workers.sqlite")
            try:
                graph = build_support_graph(
                    triage_node=_triage,
                    diagnosis_node=_diagnosis,
                    review_node=_review,
                    policy_guard=lambda _text, _citations: True,
                    checkpointer=saver,
                )

                def run(index):
                    return graph.invoke(
                        _initial_state(
                            ticket_id=f"ticket-{index}",
                            request_id=f"request-{index}",
                            command_id=f"command-{index}",
                        ),
                        {"configurable": {"thread_id": f"worker-{index}"}},
                    )["status"]

                with ThreadPoolExecutor(max_workers=4) as executor:
                    statuses = list(executor.map(run, range(8)))
                self.assertEqual(statuses, ["resolved"] * 8)
            finally:
                saver.close()

    def test_with_event_enforces_resume_command_step_reset(self):
        from agent.orchestration.graph import with_event

        state = _initial_state(event_step=7)
        update = with_event(
            state,
            {"status": "escalated"},
            node_name="test",
            event_type="test_event",
            summary="测试事件",
            command_id="resume-1",
            step_index=1,
        )
        self.assertEqual(update["event_step"], 1)
        self.assertEqual(update["status_events"][0]["step_index"], 1)
        with self.assertRaises(ValueError):
            with_event(
                state,
                {},
                node_name="test",
                event_type="test_event",
                summary="测试事件",
                command_id="resume-2",
                step_index=2,
            )

    def test_manual_gate_keeps_safe_draft_for_human_review(self):
        gated_output = _diagnosis(None)
        gated_output["recommended_actions"][0]["action_code"] = "device_reset"
        from agent.orchestration.graph import build_support_graph

        result = build_support_graph(
            triage_node=_triage,
            diagnosis_node=lambda _state: gated_output,
            review_node=_review,
            policy_guard=lambda _text, _citations: True,
            checkpointer=MemorySaver(),
        ).invoke(
            _initial_state(),
            {"configurable": {"thread_id": "manual-gate"}},
        )

        self.assertEqual(result["status"], "escalated")
        self.assertEqual(result["manual_gate_reason"], "device_reset")
        self.assertEqual(
            result["draft_answer"], "请先更换可信电源线，然后重新连接设备。"
        )
        self.assertIn("__interrupt__", result)

    def test_configured_graph_injects_four_direct_adapters_and_shared_policy(self):
        from agent.orchestration.graph import build_configured_graph

        calls = []

        class Rag:
            def search_evidence(self, query):
                calls.append(("knowledge", query))
                return []

        class Profiles:
            def get_profile(self, user_id):
                calls.append(("profile", user_id))
                return {"user_id": user_id, "device_model": "Nano X"}

        class Warranties:
            def as_evidence(self, query):
                calls.append(("warranty", query))
                return None

        def chain(query):
            calls.append(("chain", query))
            return "模拟网络状态正常"

        captured = {}

        def diagnosis_factory(**kwargs):
            captured["diagnosis"] = kwargs
            return _diagnosis

        class Guard:
            def __init__(self, trusted):
                self.trusted_sources = trusted

            def evaluate(self, _text, _urls):
                return type("Decision", (), {"passed": True, "reason_codes": []})()

        def guard_factory(_policy, trusted_source_policy=None):
            captured["guard_trusted"] = trusted_source_policy
            return Guard(trusted_source_policy)

        orchestration = {
            "timeouts": {"agent_seconds": 20, "readonly_tool_seconds": 8},
            "retries": {
                "triage": 1,
                "diagnosis": 1,
                "readonly_tool": 1,
                "review": 0,
            },
            "required_fields": {
                "firmware_repair": ["device_model", "error_state"],
                "warranty_service": ["serial_last4"],
                "transaction_boundary": ["transaction_hash", "chain_name"],
            },
            "manual_gate_actions": [
                "device_reset",
                "bootloader_recovery",
                "wallet_recovery",
                "warranty_decision",
            ],
        }
        policy = {
            "policy_version": "test.v1",
            "official_domains": ["support.ledger.com"],
            "critical_response_template_zh": "请通过官方渠道处理。",
        }
        with (
            patch("agent.nodes.triage.TriageAgent", return_value=_triage),
            patch("agent.nodes.diagnosis.DiagnosisAgent", side_effect=diagnosis_factory),
            patch("agent.nodes.review.ReviewAgent", return_value=_review),
            patch("agent.policies.security.PolicyGuard", side_effect=guard_factory),
        ):
            graph = build_configured_graph(
                object(),
                policy,
                orchestration,
                MemorySaver(),
                rag_service=Rag(),
                profile_db=Profiles(),
                warranty_repository=Warranties(),
                chain_fetcher=chain,
            )

        self.assertTrue(callable(graph.invoke))
        diagnosis_config = captured["diagnosis"]
        self.assertIs(
            diagnosis_config["trusted_source_policy"], captured["guard_trusted"]
        )
        self.assertEqual(diagnosis_config["tool_timeout_seconds"], 8.0)
        self.assertEqual(diagnosis_config["tool_retries"], 1)
        tools = diagnosis_config["tool_registry"]
        self.assertEqual(tools["knowledge_search"]({}, "usb"), [])
        self.assertEqual(
            tools["profile"]({"user_id": "user-1"}, "ignored")[0]["kind"],
            "profile",
        )
        self.assertEqual(tools["warranty"]({}, "A1B2"), [])
        self.assertEqual(tools["chain_status"]({}, "BTC")[0]["kind"], "chain")
        self.assertEqual(
            calls,
            [
                ("knowledge", "usb"),
                ("profile", "user-1"),
                ("warranty", "A1B2"),
                ("chain", "BTC"),
            ],
        )

    def test_triage_cannot_downgrade_ingress_risk_or_drop_flags(self):
        downgraded = self._graph().invoke(
            _initial_state(risk_level="medium"),
            {"configurable": {"thread_id": "risk-downgrade"}},
        )
        self.assertEqual(downgraded["status"], "escalated")
        self.assertEqual(downgraded["last_error"], "TRIAGE_FAILURE")

        dropped = self._graph().invoke(
            _initial_state(
                risk_level="low", risk_flags=["device_auth_failure"]
            ),
            {"configurable": {"thread_id": "flag-drop"}},
        )
        self.assertEqual(dropped["status"], "escalated")
        self.assertEqual(dropped["last_error"], "TRIAGE_FAILURE")

    def test_untrusted_unreferenced_evidence_is_not_checkpointed(self):
        malicious = _diagnosis(None)
        malicious["evidence"].append(
            {
                "evidence_id": "unused-evil",
                "kind": "knowledge",
                "content": "未引用的候选材料",
                "source_title": "未知来源",
                "source_url": "http://evil.example/firmware",
            }
        )
        from agent.orchestration.graph import build_support_graph

        result = build_support_graph(
            triage_node=_triage,
            diagnosis_node=lambda _state: malicious,
            review_node=_review,
            policy_guard=lambda _text, _citations: True,
            checkpointer=MemorySaver(),
        ).invoke(
            _initial_state(),
            {"configurable": {"thread_id": "untrusted-evidence"}},
        )

        self.assertEqual(result["status"], "escalated")
        self.assertEqual(result["last_error"], "DIAGNOSIS_FAILURE")
        self.assertNotIn("evil.example", str(result))

    def test_configured_timeout_rejects_nan_and_infinity(self):
        from agent.orchestration.graph import _configured_positive_number

        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _configured_positive_number(value, "timeout")


if __name__ == "__main__":
    unittest.main()
