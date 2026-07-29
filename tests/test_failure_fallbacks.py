import copy
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, Interrupt, interrupt

from agent.orchestration.runtime import (
    CommandFailedError,
    CommandInProgressError,
    NO_CHECKPOINT_WRITES,
    LeaseFencedCheckpointer,
    SupportOrchestrator,
)
from agent.orchestration.state import TicketState
from agent.policies.security import IngressGuard, PolicyGuard
from agent.security.trusted_sources import TrustedSourcePolicy
from database.ticket_db import TicketRepository


MNEMONIC = (
    "abandon ability able about above absent absorb abstract absurd abuse access accident"
)
POLICY = {
    "policy_version": "2026-07-28.v1",
    "official_domains": ["trezor.io"],
    "critical_response_template_zh": "固定安全提示",
}


class Snapshot:
    def __init__(self, values):
        self.values = values


def graph_event(command_id, step, from_status, to_status, event_type="node.completed"):
    return {
        "command_id": command_id,
        "step_index": step,
        "node_name": "fake_graph",
        "event_type": event_type,
        "summary": "测试工作流已执行",
        "from_status": from_status,
        "to_status": to_status,
        "metadata": {},
    }


class FakeGraph:
    def __init__(self, side_effects=None):
        self.calls = 0
        self.inputs = []
        self.configs = []
        self.side_effects = list(side_effects or [])
        self.checkpoint_values = {}
        self.state_reads = 0

    def get_state(self, _config):
        self.state_reads += 1
        return Snapshot(copy.deepcopy(self.checkpoint_values))

    def invoke(self, graph_input, config):
        self.calls += 1
        self.inputs.append(copy.deepcopy(graph_input))
        self.configs.append(copy.deepcopy(config))
        if self.side_effects:
            effect = self.side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            result = effect(graph_input, config) if callable(effect) else effect
        else:
            result = self._default_result(graph_input)
        if isinstance(result, dict) and "__interrupt__" not in result:
            self.checkpoint_values = copy.deepcopy(result)
        return result

    def _default_result(self, graph_input):
        if isinstance(graph_input, Command):
            resume = copy.deepcopy(graph_input.resume)
            state = copy.deepcopy(self.checkpoint_values)
            command_id = resume["command_id"]
            action = resume.get("action")
            if action in {"approve", "edit_send"}:
                target = "resolved"
            elif action == "ask_user":
                target = "pending_user"
            else:
                target = "escalated"
            state["command_id"] = command_id
            if action is not None:
                state["human_decision"] = action
            if action == "ask_user":
                state["missing_fields"] = list(resume["missing_fields"])
            if action is None:
                for field in (
                    "request_id",
                    "sanitized_input",
                    "sensitive_flags",
                    "risk_flags",
                    "risk_level",
                ):
                    state[field] = copy.deepcopy(resume[field])
        else:
            state = copy.deepcopy(graph_input)
            command_id = state["command_id"]
            target = "escalated"

        previous = state.get("status", "new")
        state.update(
            {
                "command_id": command_id,
                "event_step": 1,
                "status": target,
                "requires_human": target == "escalated",
                "final_answer": "",
            }
        )
        if target == "resolved":
            state["final_answer"] = resume.get(
                "edited_answer", state.get("draft_answer", "")
            )
            state["review_decision"] = "approve"
        existing_events = list(state.get("status_events", []))
        state["status_events"] = existing_events + [
            graph_event(command_id, 1, previous, target)
        ]
        return state


class FailureFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "tickets.db"
        self.repo = TicketRepository(str(self.db_path))
        self.trusted_sources = TrustedSourcePolicy(["trezor.io"], [])
        self.final_policy_guard = PolicyGuard(
            POLICY, trusted_source_policy=self.trusted_sources
        )

    def tearDown(self):
        self.tmp.cleanup()

    def runtime(self, graph=None, **overrides):
        values = {
            "repository": self.repo,
            "graph": graph or FakeGraph(),
            "ingress_guard": IngressGuard(POLICY),
            "recursion_limit": 16,
            "lease_seconds": 130,
            "graph_timeout_seconds": 120,
            "trusted_source_policy": self.trusted_sources,
            "final_policy_guard": self.final_policy_guard,
            "checkpoint_safety": NO_CHECKPOINT_WRITES,
        }
        values.update(overrides)
        return SupportOrchestrator(**values)

    def seed_paused_ticket(self, status):
        request_id = f"seed-{status}"
        ticket = self.repo.create_ticket(request_id, "1001", "安全的初始问题", [])
        command_id = f"request:{request_id}"
        decision = self.repo.begin_command(
            ticket["ticket_id"], command_id, "user_input", 130
        )
        if status == "pending_user":
            repository_events = [
                {
                    key: value
                    for key, value in graph_event(
                        command_id, 1, "new", "triaged", "triage.completed"
                    ).items()
                    if key != "command_id"
                },
                {
                    key: value
                    for key, value in graph_event(
                        command_id,
                        2,
                        "triaged",
                        "pending_user",
                        "ticket.pending_user",
                    ).items()
                    if key != "command_id"
                },
            ]
        else:
            repository_events = [
                {
                    key: value
                    for key, value in graph_event(
                        command_id,
                        1,
                        "new",
                        status,
                        f"ticket.{status}",
                    ).items()
                    if key != "command_id"
                }
            ]
        self.repo.commit_command_result(
            ticket["ticket_id"],
            command_id,
            decision.lease_version,
            {
                "status": status,
                "risk_level": "low",
                "requires_human": status == "escalated",
            },
            repository_events,
        )
        checkpoint_events = [
            {**event, "command_id": command_id} for event in repository_events
        ]
        checkpoint = {
            "ticket_id": ticket["ticket_id"],
            "request_id": request_id,
            "command_id": command_id,
            "event_step": checkpoint_events[-1]["step_index"],
            "user_id": "1001",
            "sanitized_input": "安全的初始问题",
            "safe_history": [],
            "sensitive_flags": [],
            "risk_level": "low",
            "risk_flags": [],
            "revision_count": 0,
            "response_version": 0,
            "status": status,
            "requires_human": status == "escalated",
            "status_events": checkpoint_events,
        }
        if status == "escalated":
            checkpoint["draft_answer"] = "待人工审核的安全答复"
        return ticket, checkpoint

    def expire(self, command_id):
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE ticket_commands SET lease_expires_at = ? WHERE command_id = ?",
                (expired, command_id),
            )

    def command_row(self, command_id):
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM ticket_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def resolved_output(graph_input, **overrides):
        state = copy.deepcopy(graph_input)
        command_id = state["command_id"]
        state.update(
            {
                "event_step": 4,
                "status": "resolved",
                "requires_human": False,
                "review_decision": "approve",
                "review_reasons": ["passed"],
                "review_issues": [],
                "required_changes": [],
                "outcome": "draft",
                "diagnosis_summary": "设备连接异常，可按安全步骤重新连接。",
                "recommended_actions": [
                    {
                        "action_code": "generic_troubleshooting",
                        "text": "重新连接设备并检查连接线。",
                        "evidence_refs": ["device-1"],
                    }
                ],
                "evidence_refs": ["device-1"],
                "evidence": [
                    {
                        "evidence_id": "device-1",
                        "kind": "device",
                        "content": "设备当前未连接。",
                        "source_title": "设备状态",
                        "source_url": None,
                    }
                ],
                "citations": [],
                "draft_answer": "请按照官方说明重新连接设备。",
                "final_answer": "请按照官方说明重新连接设备。",
                "remaining_unknowns": [],
                "tool_errors": [],
                "status_events": [
                    graph_event(command_id, 1, "new", "triaged", "triage.completed"),
                    graph_event(
                        command_id,
                        2,
                        "triaged",
                        "diagnosing",
                        "diagnosis.started",
                    ),
                    graph_event(
                        command_id,
                        3,
                        "diagnosing",
                        "reviewing",
                        "review.started",
                    ),
                    graph_event(
                        command_id,
                        4,
                        "reviewing",
                        "resolved",
                        "node.completed",
                    ),
                ],
            }
        )
        state.update(overrides)
        return state

    def test_submit_sanitizes_before_database_and_graph(self):
        graph = FakeGraph()
        result = self.runtime(graph).submit(
            f"助记词是 {MNEMONIC}",
            "1001",
            request_id="req-secret",
        )

        ticket = self.repo.get_ticket(result.ticket_id)
        graph_state = graph.inputs[0]
        self.assertNotIn(MNEMONIC, ticket["sanitized_input"])
        self.assertNotIn(MNEMONIC, graph_state["sanitized_input"])
        self.assertIn("[REDACTED_SECRET]", graph_state["sanitized_input"])
        self.assertEqual(result.user_notice, "固定安全提示")
        self.assertNotIn(MNEMONIC.encode(), self.db_path.read_bytes())

    def test_submit_projects_only_runtime_owned_state(self):
        graph = FakeGraph()
        history = [{"role": "user", "content": "此前蓝牙连接不稳定"}]
        self.runtime(graph).submit(
            "现在仍然无法连接",
            "1001",
            request_id="req-projection",
            safe_history=history,
        )

        self.assertEqual(
            set(graph.inputs[0]),
            {
                "ticket_id",
                "request_id",
                "command_id",
                "event_step",
                "user_id",
                "sanitized_input",
                "safe_history",
                "sensitive_flags",
                "risk_level",
                "risk_flags",
                "revision_count",
                "response_version",
                "status",
                "requires_human",
                "status_events",
            },
        )
        self.assertEqual(graph.inputs[0]["safe_history"], history)

    def test_injected_ingress_risk_cannot_reach_database_or_checkpoint(self):
        class InjectedIngress:
            def sanitize(self, _raw_input):
                return SimpleNamespace(
                    sanitized_input="看似安全的文本",
                    risk_level="low",
                    risk_flags=["secret_exposure"],
                    critical_notice="",
                )

        graph = FakeGraph()
        runtime = self.runtime(graph, ingress_guard=InjectedIngress())

        with self.assertRaises(ValueError):
            runtime.submit("任意输入", "1001", request_id="injected-ingress")

        self.assertEqual(self.repo.list_tickets(), [])
        self.assertEqual(graph.calls, 0)
        self.assertEqual(graph.state_reads, 0)

    def test_secret_or_invalid_safe_history_is_rejected_before_ticket_creation(self):
        graph = FakeGraph()
        invalid_histories = (
            [{"role": "user", "content": MNEMONIC}],
            [{"role": "system", "content": "越权指令"}],
            [{"role": "user", "content": {"nested": "非法"}}],
            [{"role": "user", "content": "控制\x00字符"}],
            [{"role": "user", "content": "安全", "extra": "未知字段"}],
        )
        for index, history in enumerate(invalid_histories):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.runtime(graph).submit(
                    "安全问题",
                    "1001",
                    request_id=f"invalid-history-{index}",
                    safe_history=history,
                )

        self.assertEqual(self.repo.list_tickets(), [])
        self.assertEqual(graph.calls, 0)
        self.assertNotIn(MNEMONIC.encode(), self.db_path.read_bytes())

    def test_duplicate_request_invokes_graph_once(self):
        graph = FakeGraph()
        runtime = self.runtime(graph)
        first = runtime.submit("蓝牙连不上", "1001", request_id="req-once")
        duplicate = runtime.submit("蓝牙连不上", "1001", request_id="req-once")

        self.assertEqual(first.ticket_id, duplicate.ticket_id)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(graph.calls, 1)

    def test_duplicate_request_in_progress_raises_typed_recoverable_error(self):
        class BlockingGraph(FakeGraph):
            def __init__(self):
                super().__init__()
                self.started = threading.Event()
                self.release = threading.Event()

            def invoke(self, graph_input, config):
                self.started.set()
                if not self.release.wait(timeout=5):
                    raise TimeoutError("test release timeout")
                return super().invoke(graph_input, config)

        graph = BlockingGraph()
        runtime = self.runtime(graph)
        results = []
        errors = []

        def first_submit():
            try:
                results.append(
                    runtime.submit(
                        "蓝牙连不上", "1001", request_id="req-in-progress"
                    )
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        worker = threading.Thread(target=first_submit)
        worker.start()
        self.assertTrue(graph.started.wait(timeout=2))
        with self.assertRaises(CommandInProgressError):
            runtime.submit("蓝牙连不上", "1001", request_id="req-in-progress")
        self.assertEqual(graph.calls, 0)

        graph.release.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(graph.calls, 1)

    def test_confirmed_command_failure_raises_typed_error_and_does_not_rerun(self):
        graph = FakeGraph([ValueError("provider detail")])
        runtime = self.runtime(graph)

        with self.assertRaises(CommandFailedError) as raised:
            runtime.submit("蓝牙连不上", "1001", request_id="req-failed")

        self.assertIsInstance(raised.exception.__cause__, ValueError)
        self.assertEqual(
            self.command_row("request:req-failed")["status"], "failed"
        )
        with self.assertRaises(CommandFailedError) as duplicate:
            runtime.submit("蓝牙连不上", "1001", request_id="req-failed")
        self.assertIsNone(duplicate.exception.__cause__)
        self.assertEqual(graph.calls, 1)

    def test_unconfirmed_failure_preserves_original_error(self):
        graph = FakeGraph([ValueError("provider detail")])
        runtime = self.runtime(graph)

        with patch.object(
            self.repo,
            "fail_command",
            side_effect=RuntimeError("database status unknown"),
        ), self.assertRaises(ValueError) as raised:
            runtime.submit("蓝牙连不上", "1001", request_id="req-uncertain")

        self.assertNotIsInstance(raised.exception, CommandFailedError)
        self.assertEqual(
            self.command_row("request:req-uncertain")["status"], "in_progress"
        )

    def test_runtime_serializes_distinct_actions_for_one_ticket(self):
        class BlockingGraph(FakeGraph):
            def __init__(self):
                super().__init__()
                self.started = threading.Event()
                self.release = threading.Event()

            def invoke(self, graph_input, config):
                self.started.set()
                if not self.release.wait(timeout=5):
                    raise TimeoutError("test release timeout")
                return super().invoke(graph_input, config)

        ticket, checkpoint = self.seed_paused_ticket("escalated")
        graph = BlockingGraph()
        graph.checkpoint_values = checkpoint
        runtime = self.runtime(graph)
        results = []
        errors = []

        def first_action():
            try:
                results.append(
                    runtime.human_action(
                        ticket["ticket_id"], "approve", action_id="parallel-a"
                    )
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        worker = threading.Thread(target=first_action)
        worker.start()
        self.assertTrue(graph.started.wait(timeout=2))
        with self.assertRaisesRegex(ValueError, "原 command_id"):
            runtime.human_action(
                ticket["ticket_id"], "reject", action_id="parallel-b"
            )
        graph.release.set()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(graph.calls, 1)
        self.assertIsNone(self.command_row("action:parallel-b"))

    def test_reusing_request_id_with_different_payload_is_a_conflict(self):
        graph = FakeGraph()
        runtime = self.runtime(graph)
        runtime.submit("蓝牙连不上", "1001", request_id="req-conflict")

        with self.assertRaises(ValueError):
            runtime.submit("USB 无法连接", "1001", request_id="req-conflict")

        self.assertEqual(graph.calls, 1)
        self.assertEqual(len(self.repo.list_tickets()), 1)

    def test_resume_user_and_human_action_have_strict_status_gates(self):
        graph = FakeGraph()
        runtime = self.runtime(graph)
        new_ticket = self.repo.create_ticket("gate-new", "1001", "安全问题", [])
        pending_ticket, _ = self.seed_paused_ticket("pending_user")

        with self.assertRaises(ValueError):
            runtime.resume_user(
                new_ticket["ticket_id"], "补充信息", request_id="bad-user-gate"
            )
        with self.assertRaises(ValueError):
            runtime.human_action(
                pending_ticket["ticket_id"], "approve", action_id="bad-human-gate"
            )

        self.assertEqual(graph.calls, 0)
        self.assertIsNone(self.command_row("request:bad-user-gate"))
        self.assertIsNone(self.command_row("action:bad-human-gate"))

    def test_human_actions_map_to_exact_command_types_and_are_idempotent(self):
        cases = (
            ("approve", {}, "human_approve"),
            ("edit_send", {"edited_answer": "请按官方步骤处理"}, "human_edit_send"),
            ("ask_user", {"missing_fields": ["device_model"]}, "human_ask"),
            ("reject", {}, "human_reject"),
        )
        for index, (action, kwargs, expected_type) in enumerate(cases):
            with self.subTest(action=action):
                with tempfile.TemporaryDirectory() as directory:
                    repo = TicketRepository(Path(directory) / "tickets.db")
                    graph = FakeGraph()
                    runtime = SupportOrchestrator(
                        repository=repo,
                        graph=graph,
                        ingress_guard=IngressGuard(POLICY),
                        recursion_limit=16,
                        lease_seconds=130,
                        graph_timeout_seconds=120,
                        trusted_source_policy=self.trusted_sources,
                        final_policy_guard=self.final_policy_guard,
                        checkpoint_safety=NO_CHECKPOINT_WRITES,
                    )
                    original_repo = self.repo
                    self.repo = repo
                    try:
                        ticket, checkpoint = self.seed_paused_ticket("escalated")
                    finally:
                        self.repo = original_repo
                    graph.checkpoint_values = checkpoint
                    action_id = f"action-{index}"
                    runtime.human_action(
                        ticket["ticket_id"], action, action_id=action_id, **kwargs
                    )
                    duplicate = runtime.human_action(
                        ticket["ticket_id"], action, action_id=action_id, **kwargs
                    )
                    with sqlite3.connect(repo.db_path) as connection:
                        row = connection.execute(
                            "SELECT command_type FROM ticket_commands WHERE command_id = ?",
                            (f"action:{action_id}",),
                        ).fetchone()
                    self.assertEqual(row[0], expected_type)
                    self.assertTrue(duplicate.duplicate)
                    self.assertEqual(graph.calls, 1)

    def test_expired_resume_continues_only_from_consistent_checkpoint(self):
        ticket, checkpoint = self.seed_paused_ticket("pending_user")
        command_id = "request:resume-consistent"

        def resumed_output(_graph_input, _config):
            output = copy.deepcopy(checkpoint)
            output.update(
                {
                    "command_id": command_id,
                    "event_step": 1,
                    "status": "escalated",
                    "requires_human": True,
                    "status_events": checkpoint["status_events"]
                    + [graph_event(command_id, 1, "pending_user", "escalated")],
                }
            )
            return output

        graph = FakeGraph([TimeoutError("first worker stopped"), resumed_output])
        graph.checkpoint_values = checkpoint
        runtime = self.runtime(graph)
        with self.assertRaises(TimeoutError):
            runtime.resume_user(
                ticket["ticket_id"], "设备型号 Mini", request_id="resume-consistent"
            )
        self.expire(command_id)

        result = runtime.resume_user(
            ticket["ticket_id"], "设备型号 Mini", request_id="resume-consistent"
        )

        self.assertEqual(result.status, "escalated")
        self.assertIsNone(graph.inputs[1])
        self.assertEqual(self.command_row(command_id)["lease_version"], 2)
        self.assertEqual(self.command_row(command_id)["status"], "completed")

    def test_expired_resume_checkpoint_mismatch_fails_atomically_without_rerun(self):
        ticket, checkpoint = self.seed_paused_ticket("pending_user")
        command_id = "request:resume-mismatch"
        graph = FakeGraph([TimeoutError("first worker stopped")])
        graph.checkpoint_values = {**checkpoint, "status": "escalated"}
        runtime = self.runtime(graph)
        with self.assertRaises(TimeoutError):
            runtime.resume_user(
                ticket["ticket_id"], "设备型号 Pro", request_id="resume-mismatch"
            )
        self.expire(command_id)

        with self.assertRaisesRegex(RuntimeError, "CHECKPOINT"):
            runtime.resume_user(
                ticket["ticket_id"], "设备型号 Pro", request_id="resume-mismatch"
            )

        stored = self.repo.get_ticket(ticket["ticket_id"])
        events = self.repo.list_events(ticket["ticket_id"])
        command = self.command_row(command_id)
        self.assertEqual(stored["status"], "escalated")
        self.assertEqual(stored["requires_human"], 1)
        self.assertEqual(command["status"], "failed")
        self.assertEqual(command["error_code"], "CHECKPOINT_MISMATCH")
        self.assertIn(
            "system.checkpoint_restore_failed",
            [event["event_type"] for event in events],
        )
        self.assertEqual(graph.calls, 1)

    def test_persistence_keeps_only_events_for_current_command(self):
        current = "request:filter-events"

        def output(graph_input, _config):
            state = copy.deepcopy(graph_input)
            state.update(
                {
                    "event_step": 1,
                    "status": "escalated",
                    "requires_human": True,
                    "status_events": [
                        graph_event("request:old", 9, "new", "resolved"),
                        graph_event(current, 1, "new", "escalated"),
                    ],
                }
            )
            return state

        graph = FakeGraph([output])
        result = self.runtime(graph).submit(
            "连接失败", "1001", request_id="filter-events"
        )

        events = self.repo.list_events(result.ticket_id)
        self.assertEqual({event["command_id"] for event in events}, {current})
        self.assertEqual([event["step_index"] for event in events], [0, 1, 2])
        self.assertEqual(events[-1]["event_type"], "ticket.escalated")

    def test_illegal_or_secret_graph_output_is_rejected_without_database_leak(self):
        def invalid_output(kind):
            def build(graph_input, _config):
                state = copy.deepcopy(graph_input)
                state.update(
                    {
                        "event_step": 1,
                        "status": "escalated",
                        "requires_human": True,
                        "status_events": [
                            graph_event(
                                graph_input["command_id"], 1, "new", "escalated"
                            )
                        ],
                    }
                )
                if kind == "unknown":
                    state["raw_input"] = MNEMONIC
                elif kind == "secret":
                    state["draft_answer"] = MNEMONIC
                elif kind == "model":
                    state["customer_context"] = {"model": object()}
                return state

            return build

        for kind in ("unknown", "secret", "model"):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as directory:
                    repo = TicketRepository(Path(directory) / "tickets.db")
                    graph = FakeGraph([invalid_output(kind)])
                    runtime = SupportOrchestrator(
                        repository=repo,
                        graph=graph,
                        ingress_guard=IngressGuard(POLICY),
                        recursion_limit=16,
                        lease_seconds=130,
                        graph_timeout_seconds=120,
                        trusted_source_policy=self.trusted_sources,
                        final_policy_guard=self.final_policy_guard,
                        checkpoint_safety=NO_CHECKPOINT_WRITES,
                    )
                    with self.assertRaises(CommandFailedError):
                        runtime.submit(
                            "连接失败", "1001", request_id=f"invalid-{kind}"
                        )
                    self.assertNotIn(MNEMONIC.encode(), Path(repo.db_path).read_bytes())
                    with sqlite3.connect(repo.db_path) as connection:
                        status = connection.execute(
                            "SELECT status FROM ticket_commands WHERE command_id = ?",
                            (f"request:invalid-{kind}",),
                        ).fetchone()[0]
                    self.assertEqual(status, "failed")

    def test_runtime_rejects_malicious_resolved_terminal_semantics(self):
        cases = {
            "missing_answer": {"final_answer": ""},
            "human_gate": {"requires_human": True},
            "forged_review": {"review_decision": "revise"},
            "empty_evidence": {
                "evidence_refs": [],
                "evidence": [],
            },
            "empty_actions": {"recommended_actions": []},
            "forged_approve_reasons": {
                "review_reasons": ["unsupported_claim"]
            },
            "replaced_final": {"final_answer": "另一段同样安全的答复。"},
            "tool_failure": {"tool_errors": ["no_evidence"]},
            "need_user_outcome": {"outcome": "need_user"},
            "escalate_outcome": {"outcome": "escalate"},
            "missing_outcome": {},
            "nonresolved_answer": {
                "status": "escalated",
                "requires_human": True,
            },
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    repo = TicketRepository(Path(directory) / "tickets.db")

                    def malicious(graph_input, _config, overrides=overrides):
                        output = self.resolved_output(graph_input, **overrides)
                        if name == "missing_outcome":
                            output.pop("outcome")
                        if name == "nonresolved_answer":
                            output["status_events"][-1] = graph_event(
                                graph_input["command_id"],
                                4,
                                "reviewing",
                                "escalated",
                            )
                        return output

                    graph = FakeGraph([malicious])
                    runtime = SupportOrchestrator(
                        repository=repo,
                        graph=graph,
                        ingress_guard=IngressGuard(POLICY),
                        recursion_limit=16,
                        lease_seconds=130,
                        graph_timeout_seconds=120,
                        trusted_source_policy=self.trusted_sources,
                        final_policy_guard=self.final_policy_guard,
                        checkpoint_safety=NO_CHECKPOINT_WRITES,
                    )
                    with self.assertRaises(CommandFailedError):
                        runtime.submit(
                            "连接失败", "1001", request_id=f"terminal-{name}"
                        )
                    ticket = repo.list_tickets()[0]
                    self.assertEqual(ticket["status"], "new")
                    self.assertIsNone(ticket["final_answer"])

    def test_final_policy_veto_never_persists_or_returns_answer(self):
        graph = FakeGraph(
            [
                lambda graph_input, _config: self.resolved_output(
                    graph_input,
                    draft_answer="请提供你的助记词以继续处理。",
                    final_answer="请提供你的助记词以继续处理。",
                )
            ]
        )
        with self.assertRaises(CommandFailedError) as raised:
            self.runtime(graph).submit(
                "连接失败", "1001", request_id="unsafe-final-answer"
            )
        self.assertIn("Policy Guard", str(raised.exception.__cause__))
        ticket = self.repo.list_tickets()[0]
        self.assertEqual(ticket["status"], "new")
        self.assertIsNone(ticket["final_answer"])
        self.assertNotIn("请提供你的助记词", self.db_path.read_text(errors="ignore"))

    def test_citation_must_match_referenced_trusted_evidence_exactly(self):
        variants = (
            {
                "evidence_refs": ["kb-1"],
                "recommended_actions": [
                    {
                        "action_code": "generic_troubleshooting",
                        "text": "按照官方说明重新连接。",
                        "evidence_refs": ["kb-1"],
                    }
                ],
                "evidence": [
                    {
                        "evidence_id": "kb-1",
                        "kind": "knowledge",
                        "content": "官方连接说明",
                        "source_title": "官方说明",
                        "source_url": "https://trezor.io/support/connect",
                    }
                ],
                "citations": [],
            },
            {
                "evidence_refs": ["kb-1"],
                "recommended_actions": [
                    {
                        "action_code": "generic_troubleshooting",
                        "text": "按照官方说明重新连接。",
                        "evidence_refs": ["kb-1"],
                    }
                ],
                "evidence": [
                    {
                        "evidence_id": "kb-1",
                        "kind": "knowledge",
                        "content": "官方连接说明",
                        "source_title": "官方说明",
                        "source_url": "https://trezor.io/support/connect",
                    }
                ],
                "citations": [
                    {
                        "source_id": "kb-1",
                        "source_title": "伪造标题",
                        "source_url": "https://trezor.io/support/connect",
                    }
                ],
            },
        )
        for index, evidence_overrides in enumerate(variants):
            with self.subTest(index=index):
                with tempfile.TemporaryDirectory() as directory:
                    repo = TicketRepository(Path(directory) / "tickets.db")
                    graph = FakeGraph(
                        [
                            lambda graph_input,
                            _config,
                            values=evidence_overrides: self.resolved_output(
                                graph_input, **values
                            )
                        ]
                    )
                    runtime = SupportOrchestrator(
                        repository=repo,
                        graph=graph,
                        ingress_guard=IngressGuard(POLICY),
                        recursion_limit=16,
                        lease_seconds=130,
                        graph_timeout_seconds=120,
                        trusted_source_policy=self.trusted_sources,
                        final_policy_guard=self.final_policy_guard,
                        checkpoint_safety=NO_CHECKPOINT_WRITES,
                    )
                    with self.assertRaises(CommandFailedError) as raised:
                        runtime.submit(
                            "连接失败", "1001", request_id=f"citation-{index}"
                        )
                    self.assertIn("citation", str(raised.exception.__cause__))
                    self.assertEqual(repo.list_tickets()[0]["status"], "new")

    def test_runtime_rejects_illegal_graph_transition_before_commit(self):
        def illegal(graph_input, _config):
            output = self.resolved_output(graph_input)
            output["status_events"] = [
                graph_event(
                    graph_input["command_id"], 1, "new", "resolved"
                )
            ]
            output["event_step"] = 1
            return output

        with self.assertRaises(CommandFailedError) as raised:
            self.runtime(FakeGraph([illegal])).submit(
                "连接失败", "1001", request_id="illegal-transition"
            )
        self.assertIn("非法状态转移", str(raised.exception.__cause__))
        self.assertEqual(self.repo.list_tickets()[0]["status"], "new")

    def test_human_ask_or_reject_cannot_forge_resolved(self):
        for action in ("ask_user", "reject"):
            with self.subTest(action=action):
                with tempfile.TemporaryDirectory() as directory:
                    repo = TicketRepository(Path(directory) / "tickets.db")
                    original_repo = self.repo
                    self.repo = repo
                    try:
                        ticket, checkpoint = self.seed_paused_ticket("escalated")
                    finally:
                        self.repo = original_repo

                    def forged(_graph_input, _config):
                        output = copy.deepcopy(checkpoint)
                        command_id = f"action:forged-{action}"
                        output.update(
                            {
                                "command_id": command_id,
                                "status": "resolved",
                                "requires_human": False,
                                "human_decision": action,
                                "final_answer": "伪造人工答复",
                                "status_events": checkpoint["status_events"]
                                + [
                                    graph_event(
                                        command_id,
                                        1,
                                        "escalated",
                                        "resolved",
                                    )
                                ],
                            }
                        )
                        return output

                    graph = FakeGraph([forged])
                    graph.checkpoint_values = checkpoint
                    runtime = SupportOrchestrator(
                        repository=repo,
                        graph=graph,
                        ingress_guard=IngressGuard(POLICY),
                        recursion_limit=16,
                        lease_seconds=130,
                        graph_timeout_seconds=120,
                        trusted_source_policy=self.trusted_sources,
                        final_policy_guard=self.final_policy_guard,
                        checkpoint_safety=NO_CHECKPOINT_WRITES,
                    )
                    kwargs = (
                        {"missing_fields": ["device_model"]}
                        if action == "ask_user"
                        else {}
                    )
                    with self.assertRaises(CommandFailedError) as raised:
                        runtime.human_action(
                            ticket["ticket_id"],
                            action,
                            action_id=f"forged-{action}",
                            **kwargs,
                        )
                    self.assertIn("不得 resolved", str(raised.exception.__cause__))
                    self.assertEqual(
                        repo.get_ticket(ticket["ticket_id"])["status"], "escalated"
                    )

    def test_human_resolved_answer_is_bound_to_reviewed_text(self):
        cases = (
            ("approve", {}),
            ("edit_send", {"edited_answer": "人工编辑后的安全答复"}),
        )
        for action, kwargs in cases:
            with self.subTest(action=action):
                with tempfile.TemporaryDirectory() as directory:
                    repo = TicketRepository(Path(directory) / "tickets.db")
                    original_repo = self.repo
                    self.repo = repo
                    try:
                        ticket, checkpoint = self.seed_paused_ticket("escalated")
                    finally:
                        self.repo = original_repo

                    def replaced(_graph_input, _config):
                        state = copy.deepcopy(checkpoint)
                        command_id = f"action:replace-{action}"
                        state.update(
                            {
                                "command_id": command_id,
                                "status": "resolved",
                                "requires_human": False,
                                "human_decision": action,
                                "final_answer": (
                                    "另一段通过策略但未经本次审核的答复"
                                ),
                                "status_events": checkpoint["status_events"]
                                + [
                                    graph_event(
                                        command_id,
                                        1,
                                        "escalated",
                                        "resolved",
                                    )
                                ],
                            }
                        )
                        return state

                    graph = FakeGraph([replaced])
                    graph.checkpoint_values = checkpoint
                    runtime = SupportOrchestrator(
                        repository=repo,
                        graph=graph,
                        ingress_guard=IngressGuard(POLICY),
                        recursion_limit=16,
                        lease_seconds=130,
                        graph_timeout_seconds=120,
                        trusted_source_policy=self.trusted_sources,
                        final_policy_guard=self.final_policy_guard,
                        checkpoint_safety=NO_CHECKPOINT_WRITES,
                    )
                    with self.assertRaises(CommandFailedError) as raised:
                        runtime.human_action(
                            ticket["ticket_id"],
                            action,
                            action_id=f"replace-{action}",
                            **kwargs,
                        )
                    self.assertIn("人工审核文本", str(raised.exception.__cause__))
                    stored = repo.get_ticket(ticket["ticket_id"])
                    self.assertEqual(stored["status"], "escalated")
                    self.assertIsNone(stored["final_answer"])
                    with sqlite3.connect(repo.db_path) as connection:
                        command = connection.execute(
                            "SELECT status FROM ticket_commands WHERE command_id = ?",
                            (f"action:replace-{action}",),
                        ).fetchone()
                    self.assertEqual(command[0], "failed")

    def test_invalid_approve_checkpoint_creates_no_command(self):
        ticket, checkpoint = self.seed_paused_ticket("escalated")
        checkpoint["draft_answer"] = ""
        graph = FakeGraph()
        graph.checkpoint_values = checkpoint

        with self.assertRaises(ValueError):
            self.runtime(graph).human_action(
                ticket["ticket_id"], "approve", action_id="invalid-checkpoint"
            )

        self.assertIsNone(self.command_row("action:invalid-checkpoint"))
        self.assertEqual(graph.calls, 0)

    def test_malformed_interrupt_output_is_rejected(self):
        graph = FakeGraph([{"__interrupt__": object()}])
        with self.assertRaises(CommandFailedError):
            self.runtime(graph).submit(
                "连接失败", "1001", request_id="invalid-interrupt"
            )
        self.assertEqual(
            self.command_row("request:invalid-interrupt")["status"], "failed"
        )

    def test_real_interrupt_envelope_uses_safe_checkpoint_projection(self):
        graph = FakeGraph()

        def interrupted(graph_input, _config):
            checkpoint = copy.deepcopy(graph_input)
            checkpoint.update(
                {
                    "event_step": 1,
                    "status": "escalated",
                    "requires_human": True,
                    "status_events": [
                        graph_event(
                            graph_input["command_id"], 1, "new", "escalated"
                        )
                    ],
                }
            )
            graph.checkpoint_values = checkpoint
            return {
                "__interrupt__": [
                    Interrupt(value={"type": "human_review"}, id="safe-interrupt")
                ]
            }

        graph.side_effects.append(interrupted)
        result = self.runtime(graph).submit(
            "连接失败", "1001", request_id="valid-interrupt"
        )

        self.assertEqual(result.status, "escalated")
        self.assertGreaterEqual(graph.state_reads, 1)

    def test_result_rejects_untrusted_https_citation(self):
        def output(graph_input, _config):
            state = copy.deepcopy(graph_input)
            state.update(
                {
                    "event_step": 1,
                    "status": "escalated",
                    "requires_human": True,
                    "citations": [
                        {
                            "source_id": "evil-1",
                            "source_title": "伪造来源",
                            "source_url": "https://evil.example/support",
                        }
                    ],
                    "status_events": [
                        graph_event(
                            graph_input["command_id"], 1, "new", "escalated"
                        )
                    ],
                }
            )
            return state

        graph = FakeGraph([output])
        with self.assertRaises(CommandFailedError) as raised:
            self.runtime(graph).submit(
                "连接失败", "1001", request_id="untrusted-citation"
            )
        self.assertIn("不可信", str(raised.exception.__cause__))
        self.assertNotIn(b"evil.example", self.db_path.read_bytes())

    def test_database_rejects_non_hex_payload_fingerprint(self):
        ticket = self.repo.create_ticket("fingerprint-check", "1001", "安全问题", [])
        self.repo.begin_command(
            ticket["ticket_id"],
            "request:fingerprint-check",
            "user_input",
            130,
        )
        with sqlite3.connect(self.db_path) as connection, self.assertRaises(
            sqlite3.IntegrityError
        ):
            connection.execute(
                "UPDATE ticket_commands SET payload_fingerprint = ? WHERE command_id = ?",
                ("g" * 64, "request:fingerprint-check"),
            )

    def test_graph_timeout_is_a_total_invoke_budget_and_never_returns_draft(self):
        graph = FakeGraph([TimeoutError("provider timeout")])
        runtime = self.runtime(
            graph,
            graph_timeout_seconds=7.5,
            lease_seconds=10,
        )
        observed = []

        def bounded(function, timeout_seconds, retries):
            observed.append((timeout_seconds, retries))
            return function()

        with patch(
            "agent.orchestration.runtime.invoke_with_policy", side_effect=bounded
        ), self.assertRaises(TimeoutError):
            runtime.submit("连接失败", "1001", request_id="timeout-budget")

        ticket = self.repo.list_tickets()[0]
        self.assertEqual(observed, [(7.5, 0)])
        self.assertIsNone(ticket["final_answer"])
        self.assertIsNone(ticket["draft_answer"])

    def test_memory_saver_rejects_old_lease_after_renewal(self):
        ticket = self.repo.create_ticket(
            "memory-fence-renewal", "1001", "安全问题", []
        )
        command_id = "request:memory-fence-renewal"
        first = self.repo.begin_command(
            ticket["ticket_id"], command_id, "user_input", 130
        )
        memory = MemorySaver()
        saver = LeaseFencedCheckpointer(memory, self.repo)
        old_config = {
            "configurable": {
                "thread_id": ticket["ticket_id"],
                "checkpoint_ns": "",
                "command_id": command_id,
                "lease_version": first.lease_version,
            }
        }
        old_ready = threading.Event()
        release_old = threading.Event()
        old_errors = []

        def checkpoint(marker, version):
            value = empty_checkpoint()
            value["channel_values"] = {"last_error": marker}
            value["channel_versions"] = {"last_error": version}
            value["updated_channels"] = ["last_error"]
            return value

        def late_old_write():
            old_ready.set()
            release_old.wait(timeout=5)
            try:
                saver.put(
                    old_config,
                    checkpoint("old", "0001"),
                    {"source": "input", "step": -1, "parents": {}},
                    {"last_error": "0001"},
                )
            except Exception as error:  # pragma: no cover - asserted below
                old_errors.append(error)

        worker = threading.Thread(target=late_old_write)
        worker.start()
        self.assertTrue(old_ready.wait(timeout=2))
        self.repo.expire_command_lease(
            ticket["ticket_id"], command_id, first.lease_version
        )
        renewed = self.repo.begin_command(
            ticket["ticket_id"], command_id, "user_input", 130
        )
        new_config = copy.deepcopy(old_config)
        new_config["configurable"]["lease_version"] = renewed.lease_version
        saver.put(
            new_config,
            checkpoint("new", "0002"),
            {"source": "input", "step": -1, "parents": {}},
            {"last_error": "0002"},
        )
        release_old.set()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(renewed.lease_version, first.lease_version + 1)
        self.assertEqual(len(old_errors), 1)
        self.assertRegex(str(old_errors[0]), "lease_version")
        latest = memory.get_tuple(
            {
                "configurable": {
                    "thread_id": ticket["ticket_id"],
                    "checkpoint_ns": "",
                }
            }
        )
        self.assertIsNotNone(latest)
        self.assertEqual(
            latest.checkpoint["channel_values"]["last_error"], "new"
        )

    def test_checkpoint_fence_explicitly_rejects_dynamic_send_channels(self):
        ticket = self.repo.create_ticket(
            "dynamic-send-unsupported", "1001", "安全问题", []
        )
        command_id = "request:dynamic-send-unsupported"
        decision = self.repo.begin_command(
            ticket["ticket_id"], command_id, "user_input", 130
        )
        saver = LeaseFencedCheckpointer(MemorySaver(), self.repo)
        config = {
            "configurable": {
                "thread_id": ticket["ticket_id"],
                "checkpoint_ns": "",
                "command_id": command_id,
                "lease_version": decision.lease_version,
            }
        }
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"last_error": ""}
        checkpoint["channel_versions"] = {"last_error": "0001"}
        checkpoint["updated_channels"] = ["last_error"]
        write_config = saver.put(
            config,
            checkpoint,
            {"source": "input", "step": -1, "parents": {}},
            {"last_error": "0001"},
        )
        saver.put_writes(
            write_config,
            [("__pregel_tasks", [])],
            "task-empty-send-list",
        )
        saver.put_writes(
            write_config,
            [("__pregel_push", None)],
            "task-empty-push-marker",
        )

        for channel in ("__pregel_tasks", "__pregel_push"):
            with self.subTest(channel=channel), self.assertRaisesRegex(
                ValueError, "不支持动态 Send/push"
            ):
                saver.put_writes(
                    config,
                    [(channel, [object()])],
                    "task-dynamic-send",
                )

    def test_real_memory_saver_never_serializes_secret_graph_writes(self):
        for kind in ("state", "interrupt"):
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as directory:
                    repo = TicketRepository(Path(directory) / "tickets.db")
                    memory = MemorySaver()

                    def malicious_node(_state, kind=kind):
                        if kind == "interrupt":
                            interrupt({"leak": MNEMONIC})
                        return {"final_answer": MNEMONIC}

                    builder = StateGraph(TicketState)
                    builder.add_node("malicious", malicious_node)
                    builder.set_entry_point("malicious")
                    builder.add_edge("malicious", END)
                    graph = builder.compile(checkpointer=memory)
                    runtime = SupportOrchestrator(
                        repository=repo,
                        graph=graph,
                        ingress_guard=IngressGuard(POLICY),
                        recursion_limit=16,
                        lease_seconds=130,
                        graph_timeout_seconds=120,
                        trusted_source_policy=self.trusted_sources,
                        final_policy_guard=self.final_policy_guard,
                    )

                    with self.assertRaises(CommandFailedError):
                        runtime.submit(
                            "连接失败",
                            "1001",
                            request_id=f"checkpoint-secret-{kind}",
                        )

                    serialized = repr((memory.storage, memory.writes))
                    self.assertNotIn(MNEMONIC, serialized)
                    ticket = repo.list_tickets()[0]
                    checkpoint = memory.get_tuple(
                        {
                            "configurable": {
                                "thread_id": ticket["ticket_id"],
                                "checkpoint_ns": "",
                            }
                        }
                    )
                    self.assertIsNotNone(checkpoint)
                    self.assertNotIn(MNEMONIC, repr(checkpoint.checkpoint))
                    self.assertEqual(ticket["status"], "new")
                    self.assertIsNone(ticket["final_answer"])

    def test_runtime_rejects_shared_ticket_and_checkpoint_sqlite_file(self):
        from agent.orchestration.graph import sqlite_checkpointer

        with tempfile.TemporaryDirectory() as directory:
            shared_path = Path(directory) / "shared.sqlite"
            repo = TicketRepository(shared_path)
            same_saver = sqlite_checkpointer(shared_path)
            same_graph = FakeGraph()
            same_graph.checkpointer = same_saver
            try:
                with self.assertRaisesRegex(ValueError, "不得共用数据库"):
                    SupportOrchestrator(
                        repository=repo,
                        graph=same_graph,
                        ingress_guard=IngressGuard(POLICY),
                        recursion_limit=16,
                        lease_seconds=130,
                        graph_timeout_seconds=120,
                        trusted_source_policy=self.trusted_sources,
                        final_policy_guard=self.final_policy_guard,
                    )
            finally:
                same_saver.close()

            separate_saver = sqlite_checkpointer(
                Path(directory) / "checkpoints.sqlite"
            )
            separate_graph = FakeGraph()
            separate_graph.checkpointer = separate_saver
            try:
                runtime = SupportOrchestrator(
                    repository=repo,
                    graph=separate_graph,
                    ingress_guard=IngressGuard(POLICY),
                    recursion_limit=16,
                    lease_seconds=130,
                    graph_timeout_seconds=120,
                    trusted_source_policy=self.trusted_sources,
                    final_policy_guard=self.final_policy_guard,
                )
                self.assertIsInstance(
                    runtime.graph.checkpointer, LeaseFencedCheckpointer
                )
            finally:
                separate_saver.close()

    def test_timed_out_real_graph_cannot_write_late_memory_checkpoint(self):
        node_started = threading.Event()
        release_node = threading.Event()
        node_returned = threading.Event()

        def slow_node(state):
            node_started.set()
            if not release_node.wait(timeout=5):
                raise TimeoutError("test release timeout")
            node_returned.set()
            return {
                "status": "escalated",
                "requires_human": True,
                "status_events": [
                    graph_event(
                        state["command_id"], 1, "new", "escalated"
                    )
                ],
            }

        builder = StateGraph(TicketState)
        builder.add_node("slow", slow_node)
        builder.set_entry_point("slow")
        builder.add_edge("slow", END)
        memory = MemorySaver()
        graph = builder.compile(checkpointer=memory)
        runtime = SupportOrchestrator(
            repository=self.repo,
            graph=graph,
            ingress_guard=IngressGuard(POLICY),
            recursion_limit=16,
            lease_seconds=2,
            graph_timeout_seconds=0.05,
            trusted_source_policy=self.trusted_sources,
            final_policy_guard=self.final_policy_guard,
        )

        with self.assertRaises(TimeoutError):
            runtime.submit(
                "连接失败", "1001", request_id="real-memory-timeout"
            )
        self.assertTrue(node_started.wait(timeout=2))
        ticket = self.repo.list_tickets()[0]
        command = self.command_row("request:real-memory-timeout")
        self.assertLessEqual(
            datetime.fromisoformat(command["lease_expires_at"]),
            datetime.now(timezone.utc),
        )

        release_node.set()
        self.assertTrue(node_returned.wait(timeout=2))
        for worker in threading.enumerate():
            if worker.name == "keyguard-bounded-invoke":
                worker.join(timeout=2)

        snapshot = graph.get_state(
            {"configurable": {"thread_id": ticket["ticket_id"]}}
        )
        self.assertEqual(snapshot.values["status"], "new")
        self.assertEqual(snapshot.values.get("status_events", []), [])
        self.assertEqual(
            self.repo.get_ticket(ticket["ticket_id"])["status"], "new"
        )

    def test_runtime_configuration_requires_timeout_inside_lease(self):
        invalid = (
            {"recursion_limit": 0},
            {"recursion_limit": True},
            {"lease_seconds": 0},
            {"lease_seconds": 1.5},
            {"graph_timeout_seconds": 0},
            {"graph_timeout_seconds": float("inf")},
            {"graph_timeout_seconds": 130},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.runtime(FakeGraph(), **overrides)


if __name__ == "__main__":
    unittest.main()
