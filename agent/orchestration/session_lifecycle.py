"""Deterministic lifecycle operations for customer-service session tickets."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from database.ticket_db import CommandDisposition, CommandType


class SessionExpiryService:
    """Arm and close inactive ``pending_user`` tickets without claiming resolution."""

    def __init__(
        self,
        repository,
        *,
        inactivity_timeout_seconds: int = 1800,
        command_lease_seconds: int = 130,
    ) -> None:
        for value, name in (
            (inactivity_timeout_seconds, "inactivity_timeout_seconds"),
            (command_lease_seconds, "command_lease_seconds"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        self.repository = repository
        self.inactivity_timeout_seconds = inactivity_timeout_seconds
        self.command_lease_seconds = command_lease_seconds

    @staticmethod
    def _utc_now(value: datetime | None = None) -> datetime:
        current = value or datetime.now(timezone.utc)
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise ValueError("now 必须是带时区的 datetime")
        return current.astimezone(timezone.utc)

    def arm(self, ticket_id: str, *, now: datetime | None = None) -> str | None:
        current = self._utc_now(now)
        deadline = current + timedelta(seconds=self.inactivity_timeout_seconds)
        deadline_text = deadline.isoformat()
        if self.repository.set_idle_deadline(ticket_id, deadline_text):
            return deadline_text
        return None

    def clear(self, ticket_id: str) -> bool:
        return self.repository.clear_idle_deadline(ticket_id)

    @staticmethod
    def _command_id(ticket_id: str, deadline: str) -> str:
        suffix = hashlib.sha256(deadline.encode("utf-8")).hexdigest()[:16]
        return f"idle-close:{ticket_id}:{suffix}"

    @staticmethod
    def _fingerprint(ticket_id: str, deadline: str) -> str:
        return hashlib.sha256(
            f"{ticket_id}\n{deadline}\ninactivity".encode("utf-8")
        ).hexdigest()

    def close_expired(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> list[str]:
        current = self._utc_now(now)
        now_text = current.isoformat()
        closed: list[str] = []
        for due in self.repository.list_expired_pending(now_text, limit=limit):
            ticket_id = due["ticket_id"]
            deadline = due["idle_expires_at"]
            command_id = self._command_id(ticket_id, deadline)
            decision = self.repository.begin_command(
                ticket_id,
                command_id,
                CommandType.SYSTEM_IDLE_CLOSE.value,
                self.command_lease_seconds,
                payload_fingerprint=self._fingerprint(ticket_id, deadline),
                required_status="pending_user",
            )
            if decision.disposition == CommandDisposition.COMPLETED:
                closed.append(ticket_id)
                continue
            if decision.disposition not in {
                CommandDisposition.START,
                CommandDisposition.RESUME,
            }:
                continue

            latest = self.repository.get_ticket(ticket_id)
            if (
                latest is None
                or latest.get("status") != "pending_user"
                or latest.get("idle_expires_at") != deadline
                or datetime.fromisoformat(deadline) > current
            ):
                self.repository.fail_command(
                    command_id, decision.lease_version, "idle_deadline_changed"
                )
                continue

            self.repository.commit_command_result(
                ticket_id,
                command_id,
                decision.lease_version,
                {
                    "status": "closed",
                    "waiting_reason": None,
                    "idle_expires_at": None,
                    "closed_at": now_text,
                    "close_reason": "inactivity",
                    "requires_human": False,
                    "manual_gate_reason": None,
                },
                [
                    {
                        "step_index": 1,
                        "node_name": "session_expiry",
                        "event_type": "ticket.closed",
                        "from_status": "pending_user",
                        "to_status": "closed",
                        "summary": "客户 30 分钟未继续提问，会话已结束",
                        "metadata": {"close_reason": "inactivity"},
                    }
                ],
            )
            closed.append(ticket_id)
        return closed
