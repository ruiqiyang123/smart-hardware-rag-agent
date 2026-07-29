"""Deterministic orchestration fault cases with real durable boundaries."""

from __future__ import annotations

import copy
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

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

        state = copy.deepcopy(graph_input)
        command_id = state["command_id"]
        error_code = (
            "REVIEW_FAILURE"
            if self.fault == "reviewer_validation_error"
            else "DIAGNOSIS_NO_EVIDENCE"
        )
        state.update(
            {
                "event_step": 1,
                "status": "escalated",
                "requires_human": True,
                "draft_answer": "",
                "final_answer": "",
                "last_error": error_code,
                "status_events": [
                    _event(
                        command_id,
                        1,
                        "new",
                        "escalated",
                        "node_failure",
                    )
                ],
            }
        )
        if self.fault == "rag_empty":
            state["tool_errors"] = ["no_evidence"]
        self.snapshot = copy.deepcopy(state)
        return state


def _runtime(directory: str, graph: FaultGraph):
    repository = TicketRepository(str(Path(directory) / "tickets.db"))
    trusted_sources = TrustedSourcePolicy(["trezor.io"], [])
    runtime = SupportOrchestrator(
        repository=repository,
        graph=graph,
        ingress_guard=IngressGuard(_POLICY),
        recursion_limit=16,
        lease_seconds=130,
        graph_timeout_seconds=120,
        trusted_source_policy=trusted_sources,
        final_policy_guard=PolicyGuard(
            _POLICY, trusted_source_policy=trusted_sources
        ),
        checkpoint_safety=NO_CHECKPOINT_WRITES,
    )
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
        graph = FaultGraph(name)
        repository, runtime = _runtime(directory, graph)
        if name in {"reviewer_validation_error", "rag_empty"}:
            result = runtime.submit(
                "蓝牙连接失败", "1001", request_id=f"fault-{name}"
            )
            ticket_id = result.ticket_id
            status = result.status
            final_answer = result.final_answer
        else:
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
            status = stored["status"]
            final_answer = stored.get("final_answer") or ""

        return FaultCaseResult(
            status=status,
            final_answer=final_answer,
            event_types=[
                event["event_type"]
                for event in repository.list_events(ticket_id)
            ],
            graph_start_count=graph.start_count,
            graph_resume_count=graph.resume_count,
        )
