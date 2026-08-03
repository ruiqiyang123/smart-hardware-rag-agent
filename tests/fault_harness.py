"""Deterministic orchestration fault cases with real durable boundaries."""

from __future__ import annotations

import copy
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from langgraph.checkpoint.memory import MemorySaver

from agent.nodes.diagnosis import DiagnosisAgent
from agent.orchestration.graph import build_support_graph
from agent.orchestration.runtime import (
    CheckpointRestoreError,
    NO_CHECKPOINT_WRITES,
    SupportOrchestrator,
)
from agent.policies.security import IngressGuard, PolicyGuard
from agent.security.trusted_sources import TrustedSourcePolicy
from database.ticket_db import TicketRepository


_POLICY = {
    "policy_version": "2026-07-28.v1",
    "official_domains": ["trezor.io"],
    "critical_response_template_zh": "固定安全提示",
}


@dataclass(frozen=True)
class FaultCaseResult:
    status: str
    waiting_reason: str
    requires_human: bool
    final_answer: str
    event_types: list[str]
    graph_start_count: int
    graph_resume_count: int


def _event(
    command_id: str,
    step_index: int,
    from_status: str,
    to_status: str,
    event_type: str,
) -> dict:
    return {
        "command_id": command_id,
        "step_index": step_index,
        "node_name": "fault_graph",
        "event_type": event_type,
        "summary": "故障注入已安全收敛",
        "from_status": from_status,
        "to_status": to_status,
        "metadata": {},
    }


class FaultGraph:
    """A no-model graph double that still obeys the runtime state contract."""

    def __init__(self, fault: str):
        self.fault = fault
        self.snapshot: dict = {}
        self.start_count = 0
        self.resume_count = 0

    def get_state(self, _config):
        return SimpleNamespace(values=copy.deepcopy(self.snapshot))

    def invoke(self, graph_input, config):
        if graph_input is None:
            self.resume_count += 1
            state = copy.deepcopy(self.snapshot)
            command_id = config["configurable"]["command_id"]
            previous = state["status"]
            state.update(
                {
                    "command_id": command_id,
                    "event_step": 1,
                    "status": "escalated",
                    "requires_human": True,
                    "final_answer": "",
                    "status_events": list(state.get("status_events", []))
                    + [
                        _event(
                            command_id,
                            1,
                            previous,
                            "escalated",
                            "runtime.resume_completed",
                        )
                    ],
                }
            )
            self.snapshot = copy.deepcopy(state)
            return state

        self.start_count += 1
        if self.fault in {"expired_command_lease", "checkpoint_status_mismatch"}:
            raise TimeoutError("injected provider body must stay private")
        raise AssertionError("FaultGraph only handles resume-boundary cases")


class _StaticRunner:
    def __init__(self, value: object):
        self.value = value

    def invoke(self, _messages):
        return copy.deepcopy(self.value)


class _CountingGraph:
    def __init__(self, delegate: object):
        self.delegate = delegate
        self.start_count = 0
        self.resume_count = 0

    @property
    def checkpointer(self):
        return self.delegate.checkpointer

    @checkpointer.setter
    def checkpointer(self, value):
        self.delegate.checkpointer = value

    def get_state(self, config):
        return self.delegate.get_state(config)

    def invoke(self, graph_input, config):
        if graph_input is None:
            self.resume_count += 1
        else:
            self.start_count += 1
        return self.delegate.invoke(graph_input, config)


def _triage(_state: dict) -> dict:
    return {
        "intent": "troubleshoot",
        "category": "bluetooth_connection",
        "priority": "P2",
        "risk_level": "low",
        "risk_flags": [],
        "clarity": "clear",
        "clarification_question": "",
        "clarification_options": [],
        "missing_fields": [],
        "suggested_route": "diagnose",
        "summary": "低风险蓝牙连接问题",
    }


def _diagnosis(_state: dict) -> dict:
    return {
        "outcome": "draft",
        "diagnosis_summary": "建议重新建立蓝牙连接",
        "recommended_actions": [
            {
                "action_code": "generic_troubleshooting",
                "text": "关闭并重新开启蓝牙后重试",
                "evidence_refs": ["device-1"],
            }
        ],
        "evidence_refs": ["device-1"],
        "evidence": [
            {
                "evidence_id": "device-1",
                "kind": "device",
                "content": "设备当前未建立蓝牙连接",
                "source_title": "设备状态",
                "source_url": None,
            }
        ],
        "citations": [],
        "draft_answer": "请关闭并重新开启蓝牙后重试。",
        "remaining_unknowns": [],
        "tool_errors": [],
    }


def _review(_state: dict) -> dict:
    return {
        "review_decision": "approve",
        "review_reasons": ["passed"],
        "review_issues": [],
        "required_changes": [],
    }


def _real_fault_graph(
    name: str,
    policy_guard: PolicyGuard,
    trusted_sources: TrustedSourcePolicy,
):
    if name == "reviewer_validation_error":

        def invalid_review(_state: dict) -> dict:
            return {
                "review_decision": "approve",
                "review_reasons": ["passed"],
                "review_issues": [],
            }

        diagnosis_node = _diagnosis
        review_node = invalid_review
    else:
        diagnosis_node = DiagnosisAgent(
            plan_runner=_StaticRunner(
                {
                    "requests": [
                        {
                            "name": "knowledge_search",
                            "query": "bluetooth connection",
                        }
                    ]
                }
            ),
            answer_runner=_StaticRunner({}),
            tool_registry={"knowledge_search": lambda _context, _query: []},
            timeout_seconds=1,
            retries=0,
            tool_timeout_seconds=1,
            tool_retries=0,
            trusted_source_policy=trusted_sources,
        )
        review_node = _review
    return build_support_graph(
        triage_node=_triage,
        diagnosis_node=diagnosis_node,
        review_node=review_node,
        policy_guard=policy_guard,
        checkpointer=MemorySaver(),
    )


def _runtime(
    directory: str,
    graph: object,
    trusted_sources: TrustedSourcePolicy | None = None,
    final_policy_guard: PolicyGuard | None = None,
):
    repository = TicketRepository(str(Path(directory) / "tickets.db"))
    trusted_sources = trusted_sources or TrustedSourcePolicy(["trezor.io"], [])
    final_policy_guard = final_policy_guard or PolicyGuard(
        _POLICY, trusted_source_policy=trusted_sources
    )
    arguments = {
        "repository": repository,
        "graph": graph,
        "ingress_guard": IngressGuard(_POLICY),
        "recursion_limit": 16,
        "lease_seconds": 130,
        "graph_timeout_seconds": 120,
        "trusted_source_policy": trusted_sources,
        "final_policy_guard": final_policy_guard,
    }
    if not hasattr(graph, "checkpointer"):
        arguments["checkpoint_safety"] = NO_CHECKPOINT_WRITES
    runtime = SupportOrchestrator(**arguments)
    return repository, runtime


def _seed_pending_ticket(repository: TicketRepository, graph: FaultGraph) -> dict:
    ticket = repository.create_ticket(
        "fault-seed", "1001", "请补充设备型号", []
    )
    command_id = "request:fault-seed"
    decision = repository.begin_command(
        ticket["ticket_id"], command_id, "user_input", 130
    )
    events = [
        {
            key: value
            for key, value in _event(
                command_id,
                1,
                "new",
                "triaged",
                "triage.completed",
            ).items()
            if key != "command_id"
        },
        {
            key: value
            for key, value in _event(
                command_id,
                2,
                "triaged",
                "pending_user",
                "ticket.pending_user",
            ).items()
            if key != "command_id"
        },
    ]
    repository.commit_command_result(
        ticket["ticket_id"],
        command_id,
        decision.lease_version,
        {
            "status": "pending_user",
            "risk_level": "low",
            "requires_human": False,
        },
        events,
    )
    graph.snapshot = {
        "ticket_id": ticket["ticket_id"],
        "request_id": "fault-seed",
        "command_id": command_id,
        "event_step": 2,
        "user_id": "1001",
        "sanitized_input": "请补充设备型号",
        "safe_history": [],
        "sensitive_flags": [],
        "risk_level": "low",
        "risk_flags": [],
        "revision_count": 0,
        "response_version": 0,
        "status": "pending_user",
        "requires_human": False,
        "status_events": [
            _event(
                command_id,
                1,
                "new",
                "triaged",
                "triage.completed",
            ),
            _event(
                command_id,
                2,
                "triaged",
                "pending_user",
                "ticket.pending_user",
            ),
        ],
    }
    return ticket


def run_fault_case(name: str) -> FaultCaseResult:
    supported = {
        "reviewer_validation_error",
        "rag_empty",
        "expired_command_lease",
        "checkpoint_status_mismatch",
    }
    if name not in supported:
        raise ValueError("unknown fault case")

    with tempfile.TemporaryDirectory() as directory:
        if name in {"reviewer_validation_error", "rag_empty"}:
            trusted_sources = TrustedSourcePolicy(["trezor.io"], [])
            final_policy_guard = PolicyGuard(
                _POLICY, trusted_source_policy=trusted_sources
            )
            graph = _CountingGraph(
                _real_fault_graph(
                    name, final_policy_guard, trusted_sources
                )
            )
            repository, runtime = _runtime(
                directory,
                graph,
                trusted_sources,
                final_policy_guard,
            )
            result = runtime.submit(
                "蓝牙连接失败", "1001", request_id=f"fault-{name}"
            )
            ticket_id = result.ticket_id
            status = result.status
            final_answer = result.final_answer
            graph_start_count = graph.start_count
            graph_resume_count = graph.resume_count
        else:
            graph = FaultGraph(name)
            repository, runtime = _runtime(directory, graph)
            ticket = _seed_pending_ticket(repository, graph)
            ticket_id = ticket["ticket_id"]
            request_id = f"fault-{name}"
            try:
                runtime.resume_user(
                    ticket_id, "设备型号为 Safe Mini", request_id=request_id
                )
            except TimeoutError:
                pass
            else:  # pragma: no cover - harness invariant
                raise AssertionError("fault case must expire its first lease")
            graph.start_count = 0
            if name == "checkpoint_status_mismatch":
                graph.snapshot["status"] = "new"
                try:
                    runtime.resume_user(
                        ticket_id, "设备型号为 Safe Mini", request_id=request_id
                    )
                except CheckpointRestoreError:
                    pass
                else:  # pragma: no cover - harness invariant
                    raise AssertionError("checkpoint mismatch must fail closed")
            else:
                runtime.resume_user(
                    ticket_id, "设备型号为 Safe Mini", request_id=request_id
                )
            stored = repository.get_ticket(ticket_id)
            if stored is None:  # pragma: no cover - repository invariant
                raise AssertionError("ticket disappeared")
            graph_start_count = graph.start_count
            graph_resume_count = graph.resume_count

        stored = repository.get_ticket(ticket_id)
        if stored is None:  # pragma: no cover - repository invariant
            raise AssertionError("ticket disappeared")
        status = stored["status"]
        final_answer = stored.get("final_answer") or ""
        return FaultCaseResult(
            status=status,
            waiting_reason=stored.get("waiting_reason") or "",
            requires_human=bool(stored.get("requires_human")),
            final_answer=final_answer,
            event_types=[
                event["event_type"]
                for event in repository.list_events(ticket_id)
            ],
            graph_start_count=graph_start_count,
            graph_resume_count=graph_resume_count,
        )
