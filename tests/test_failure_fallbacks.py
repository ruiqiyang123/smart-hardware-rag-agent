import copy
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langgraph.types import Command, Interrupt

from agent.orchestration.runtime import SupportOrchestrator
from agent.policies.security import IngressGuard
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
            state["final_answer"] = (
                resume.get("edited_answer", "人工已复核答复")
                if isinstance(graph_input, Command)
                else "已完成答复"
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
        self.repo.commit_command_result(
            ticket["ticket_id"],
            command_id,
            decision.lease_version,
            {
                "status": status,
                "risk_level": "low",
                "requires_human": status == "escalated",
            },
            [
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
            ],
        )
        checkpoint = {
            "ticket_id": ticket["ticket_id"],
            "request_id": request_id,
            "command_id": command_id,
            "event_step": 1,
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
            "status_events": [
                graph_event(command_id, 1, "new", status, f"ticket.{status}")
            ],
        }
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
                    )
                    with self.assertRaises((TypeError, ValueError)):
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

    def test_malformed_interrupt_output_is_rejected(self):
        graph = FakeGraph([{"__interrupt__": object()}])
        with self.assertRaises((TypeError, ValueError)):
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
        with self.assertRaisesRegex(ValueError, "不可信"):
            self.runtime(graph).submit(
                "连接失败", "1001", request_id="untrusted-citation"
            )
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
