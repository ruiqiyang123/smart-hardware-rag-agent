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
        "clarity": "clear",
        "clarification_question": "",
        "clarification_options": [],
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

    def test_low_risk_ticket_waits_for_customer_confirmation(self):
        graph = self._graph()
        config = {"configurable": {"thread_id": "low-risk"}}
        result = graph.invoke(
            _initial_state(),
            config,
        )

        self.assertEqual(result["status"], "pending_user")
        self.assertEqual(result["waiting_reason"], "resolution_confirmation")
        self.assertEqual(result["outcome"], "draft")
        self.assertEqual(
            result["final_answer"], "请先更换可信电源线，然后重新连接设备。"
        )
        self.assertEqual(result["response_version"], 1)
        self.assertIn("__interrupt__", result)

        confirmed = graph.invoke(
            Command(
                resume={
                    "command_id": "confirm-low-risk",
                    "action": "confirm_resolved",
                }
            ),
            config,
        )
        self.assertEqual(confirmed["status"], "resolved")
        self.assertIsNone(confirmed["waiting_reason"])
        self.assertTrue(confirmed["resolution_confirmed"])

    def test_ambiguous_ticket_asks_one_question_without_running_diagnosis(self):
        diagnosis_calls = []

        def triage(state):
            return {
                **_triage(state),
                "category": "other",
                "clarity": "ambiguous",
                "clarification_question": "你遇到的是哪一类问题？",
                "clarification_options": ["设备无法开机", "设备无法连接"],
                "suggested_route": "clarify",
            }

        def diagnosis(state):
            diagnosis_calls.append(state)
            return _diagnosis(state)

        from agent.orchestration.graph import build_support_graph

        result = build_support_graph(
            triage=triage,
            diagnosis=diagnosis,
            review=_review,
            policy_guard=lambda _text, _citations: True,
            checkpointer=MemorySaver(),
        ).invoke(
            _initial_state(sanitized_input="我的钱包有问题"),
            {"configurable": {"thread_id": "ambiguous"}},
        )

        self.assertEqual(result["status"], "pending_user")
        self.assertEqual(result["clarity"], "ambiguous")
        self.assertEqual(
            result["clarification_options"], ["设备无法开机", "设备无法连接"]
        )
        self.assertEqual(diagnosis_calls, [])
        self.assertIn("__interrupt__", result)

    def test_partial_ticket_sends_reviewed_guidance_before_asking_for_details(self):
        def triage(state):
            if "KeyGuard Mini" in state["sanitized_input"]:
                return {
                    **_triage(state),
                    "category": "firmware_repair",
                    "summary": "固件升级中断，信息已补充完整",
                }
            return {
                **_triage(state),
                "category": "firmware_repair",
                "clarity": "partial",
                "missing_fields": ["device_model", "error_state"],
                "summary": "固件升级中断，需要补充设备信息",
            }

        def diagnosis(state):
            if not state.get("missing_fields"):
                return {
                    **_diagnosis(state),
                    "diagnosis_summary": "设备信息完整，可以继续官方恢复流程",
                    "draft_answer": "请在 KeyGuard Mini 上保持供电，并按照官方应用提示重新执行恢复。",
                }
            return {
                **_diagnosis(state),
                "outcome": "need_user",
                "diagnosis_summary": "先保持设备供电并使用官方应用恢复",
                "draft_answer": "请先保持设备供电，不要反复插拔，并在官方应用中重新进入恢复流程。",
                "remaining_unknowns": ["device_model", "error_state"],
            }

        from agent.orchestration.graph import build_support_graph

        graph = build_support_graph(
            triage=triage,
            diagnosis=diagnosis,
            review=_review,
            policy_guard=lambda _text, _citations: True,
            checkpointer=MemorySaver(),
        )
        config = {"configurable": {"thread_id": "partial"}}
        result = graph.invoke(
            _initial_state(sanitized_input="固件升级中断了怎么办？"),
            config,
        )

        self.assertEqual(result["status"], "pending_user")
        self.assertEqual(result["clarity"], "partial")
        self.assertEqual(
            result["final_answer"],
            "请先保持设备供电，不要反复插拔，并在官方应用中重新进入恢复流程。",
        )
        self.assertEqual(
            result["remaining_unknowns"], ["device_model", "error_state"]
        )
        self.assertEqual(
            result["status_events"][-1]["event_type"], "guidance_finalized"
        )
        self.assertIn("__interrupt__", result)

        resumed = graph.invoke(
            Command(
                resume={
                    "command_id": "partial-user-reply",
                    "request_id": "partial-request-2",
                    "sanitized_input": "设备是 KeyGuard Mini，屏幕显示 Update failed",
                    "sensitive_flags": [],
                    "risk_flags": [],
                    "risk_level": "low",
                }
            ),
            config,
        )
        self.assertEqual(resumed["ticket_id"], result["ticket_id"])
        self.assertEqual(resumed["status"], "pending_user")
        self.assertEqual(resumed["waiting_reason"], "resolution_confirmation")
        self.assertEqual(resumed["clarity"], "clear")
        self.assertEqual(resumed["missing_fields"], [])
        self.assertEqual(
            resumed["final_answer"],
            "请在 KeyGuard Mini 上保持供电，并按照官方应用提示重新执行恢复。",
        )

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

        self.assertEqual(result["status"], "pending_user")
        self.assertEqual(result["waiting_reason"], "resolution_confirmation")
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

        self.assertEqual(result["status"], "pending_user")
        self.assertEqual(result["waiting_reason"], "resolution_confirmation")
        self.assertEqual(result["revision_count"], 1)
        self.assertEqual(len(diagnosis_states), 2)
        self.assertEqual(diagnosis_states[1]["review_reasons"], ["incomplete_steps"])
        self.assertEqual(diagnosis_states[1]["required_changes"], ["补充重连步骤"])

    def test_low_risk_review_exception_and_final_policy_veto_keep_ticket_open(self):
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
        self.assertEqual(broken["status"], "pending_user")
        self.assertEqual(broken["waiting_reason"], "clarification")
        self.assertEqual(broken["last_error"], "REVIEW_FALLBACK_CLARIFICATION")
        self.assertFalse(broken["requires_human"])
        self.assertEqual(broken.get("final_answer", ""), "")
        self.assertNotIn("provider leaked", str(broken))

        vetoed = self._graph(policy_guard=lambda _text, _citations: False).invoke(
            _initial_state(),
            {"configurable": {"thread_id": "policy-veto"}},
        )
        self.assertEqual(vetoed["status"], "pending_user")
        self.assertEqual(vetoed["waiting_reason"], "clarification")
        self.assertEqual(vetoed["last_error"], "FINALIZE_FALLBACK_CLARIFICATION")
        self.assertFalse(vetoed["requires_human"])
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
        self.assertEqual(triage_timeout["status"], "pending_user")
        self.assertEqual(
            triage_timeout["last_error"], "TRIAGE_FALLBACK_CLARIFICATION"
        )
        self.assertFalse(triage_timeout["requires_human"])
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
                self.assertEqual(failed["status"], "pending_user")
                self.assertEqual(
                    failed["last_error"], "DIAGNOSIS_FALLBACK_CLARIFICATION"
                )
                self.assertFalse(failed["requires_human"])
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
                self.assertEqual(statuses, ["pending_user"] * 8)
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

    def test_controlled_action_guidance_does_not_force_human_handoff(self):
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
            {"configurable": {"thread_id": "controlled-guidance"}},
        )

        self.assertEqual(result["status"], "pending_user")
        self.assertFalse(result["requires_human"])
        self.assertFalse(result.get("manual_gate_reason"))
        self.assertEqual(
            result["final_answer"], "请先更换可信电源线，然后重新连接设备。"
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

    def test_low_medium_triage_validation_failure_uses_safe_clarification(self):
        downgraded = self._graph().invoke(
            _initial_state(risk_level="medium"),
            {"configurable": {"thread_id": "risk-downgrade"}},
        )
        self.assertEqual(downgraded["status"], "pending_user")
        self.assertEqual(
            downgraded["last_error"], "TRIAGE_FALLBACK_CLARIFICATION"
        )
        self.assertEqual(downgraded["category"], "other")
        self.assertEqual(downgraded["priority"], "P2")
        self.assertEqual(downgraded["risk_level"], "medium")
        self.assertEqual(len(downgraded["clarification_options"]), 5)

    def test_triage_fallback_clears_stale_answer_and_evidence(self):
        def invalid_triage(_state):
            raise ValueError("provider raw output must not be persisted")

        from agent.orchestration.graph import build_support_graph

        result = build_support_graph(
            triage_node=invalid_triage,
            diagnosis_node=_diagnosis,
            review_node=_review,
            policy_guard=lambda _text, _citations: True,
            checkpointer=MemorySaver(),
        ).invoke(
            _initial_state(
                status="pending_user",
                category="power",
                priority="P2",
                summary="上一轮摘要",
                outcome="draft",
                draft_answer="上一轮草稿",
                final_answer="上一轮回答",
                evidence_refs=["old-1"],
                evidence=[{"evidence_id": "old-1"}],
                citations=[{"source_id": "old-1"}],
            ),
            {"configurable": {"thread_id": "triage-fallback-clear"}},
        )

        self.assertEqual(result["status"], "pending_user")
        self.assertEqual(result["category"], "other")
        self.assertEqual(result["summary"], "等待用户选择问题类型")
        self.assertEqual(result["draft_answer"], "")
        self.assertEqual(result["final_answer"], "")
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["citations"], [])
        fallback_events = [
            event
            for event in result["status_events"]
            if event["event_type"] == "triage_fallback_clarification"
        ]
        self.assertEqual(len(fallback_events), 1)
        self.assertNotIn("provider raw output", str(result))

    def test_triage_failure_with_advisory_flag_keeps_ticket_open(self):

        dropped = self._graph().invoke(
            _initial_state(
                risk_level="low", risk_flags=["device_auth_failure"]
            ),
            {"configurable": {"thread_id": "flag-drop"}},
        )
        self.assertEqual(dropped["status"], "pending_user")
        self.assertEqual(
            dropped["last_error"], "TRIAGE_FALLBACK_CLARIFICATION"
        )
        self.assertFalse(dropped["requires_human"])

    def test_triage_failure_with_automatic_risk_still_fails_closed(self):
        escalated = self._graph().invoke(
            _initial_state(
                risk_level="critical",
                risk_flags=["asset_loss"],
                requires_human=True,
            ),
            {"configurable": {"thread_id": "critical-flag-fail-closed"}},
        )

        self.assertEqual(escalated["status"], "escalated")
        self.assertTrue(escalated["requires_human"])

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

        self.assertEqual(result["status"], "pending_user")
        self.assertEqual(
            result["last_error"], "DIAGNOSIS_FALLBACK_CLARIFICATION"
        )
        self.assertFalse(result["requires_human"])
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["citations"], [])
        self.assertNotIn("evil.example", str(result))

    def test_configured_timeout_rejects_nan_and_infinity(self):
        from agent.orchestration.graph import _configured_positive_number

        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _configured_positive_number(value, "timeout")


if __name__ == "__main__":
    unittest.main()
