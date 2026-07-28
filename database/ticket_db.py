import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


class CommandDisposition(str, Enum):
    START = "start"
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    RESUME = "resume"
    FAILED = "failed"


@dataclass(frozen=True)
class CommandDecision:
    disposition: CommandDisposition
    result_status: Optional[str] = None
    result_event_id: Optional[int] = None


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    category TEXT,
    priority TEXT,
    risk_level TEXT,
    sanitized_input TEXT NOT NULL,
    summary TEXT,
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    draft_answer TEXT,
    final_answer TEXT,
    review_decision TEXT,
    revision_count INTEGER NOT NULL DEFAULT 0,
    response_version INTEGER NOT NULL DEFAULT 0,
    requires_human INTEGER NOT NULL DEFAULT 0,
    manual_gate_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_commands (
    command_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    command_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('in_progress', 'completed', 'failed')),
    lease_expires_at TEXT NOT NULL,
    result_status TEXT,
    result_event_id INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    command_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    node_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    summary TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(ticket_id, command_id, step_index)
);
"""


class TicketRepository:
    _BUSY_TIMEOUT_MS = 5000
    _UPDATABLE_FIELDS = {
        "status",
        "category",
        "priority",
        "risk_level",
        "summary",
        "risk_flags_json",
        "missing_fields_json",
        "evidence_refs_json",
        "draft_answer",
        "final_answer",
        "review_decision",
        "revision_count",
        "response_version",
        "requires_human",
        "manual_gate_reason",
    }

    def __init__(self, db_path: str = "data/keyguard_v2.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self._BUSY_TIMEOUT_MS / 1000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._BUSY_TIMEOUT_MS}")
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def create_ticket(
        self,
        request_id: str,
        user_id: str,
        sanitized_input: str,
        risk_flags: List[str],
    ) -> Dict[str, object]:
        risk_flags_json = self._json(risk_flags)
        now = self._now()
        ticket_id = f"KG-{uuid.uuid4().hex[:12].upper()}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM tickets WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                existing_flags = json.loads(existing["risk_flags_json"])
                if (
                    existing["user_id"] != user_id
                    or existing["sanitized_input"] != sanitized_input
                    or existing_flags != risk_flags
                ):
                    raise ValueError(
                        "request_id 已绑定到不同的 user_id、sanitized_input 或 risk_flags"
                    )
                return dict(existing)

            connection.execute(
                """
                INSERT INTO tickets (
                    ticket_id, request_id, user_id, status, sanitized_input,
                    risk_flags_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'new', ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    request_id,
                    user_id,
                    sanitized_input,
                    risk_flags_json,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            return dict(row)

    def get_ticket(self, ticket_id: str) -> Optional[Dict[str, object]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_tickets(self) -> List[Dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tickets
                ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END,
                         updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def begin_command(
        self,
        ticket_id: str,
        command_id: str,
        command_type: str,
        lease_seconds: int,
    ) -> CommandDecision:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds 必须是正整数")

        now = datetime.now(timezone.utc)
        lease = (now + timedelta(seconds=lease_seconds)).isoformat()
        stamp = now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM ticket_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["ticket_id"] != ticket_id
                    or existing["command_type"] != command_type
                ):
                    raise ValueError(
                        "command_id 已绑定到不同的 ticket_id 或 command_type"
                    )
                if existing["status"] == "completed":
                    return CommandDecision(
                        CommandDisposition.COMPLETED,
                        existing["result_status"],
                        existing["result_event_id"],
                    )
                if existing["status"] == "failed":
                    return CommandDecision(CommandDisposition.FAILED)

                expires = datetime.fromisoformat(existing["lease_expires_at"])
                if expires <= now:
                    connection.execute(
                        """
                        UPDATE ticket_commands
                        SET lease_expires_at = ?, updated_at = ?
                        WHERE command_id = ? AND status = 'in_progress'
                        """,
                        (lease, stamp, command_id),
                    )
                    return CommandDecision(CommandDisposition.RESUME)
                return CommandDecision(CommandDisposition.IN_PROGRESS)

            ticket = connection.execute(
                "SELECT status FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if ticket is None:
                raise ValueError(f"工单不存在: {ticket_id}")

            connection.execute(
                """
                INSERT INTO ticket_commands (
                    command_id, ticket_id, command_type, status,
                    lease_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'in_progress', ?, ?, ?)
                """,
                (command_id, ticket_id, command_type, lease, stamp, stamp),
            )
            connection.execute(
                """
                INSERT INTO ticket_events (
                    ticket_id, idempotency_key, command_id, step_index,
                    node_name, event_type, from_status, to_status,
                    summary, metadata_json, created_at
                ) VALUES (?, ?, ?, 0, 'runtime', 'command.accepted', ?, ?, ?, '{}', ?)
                """,
                (
                    ticket_id,
                    f"command:{command_id}:accepted",
                    command_id,
                    ticket["status"],
                    ticket["status"],
                    f"接受命令 {command_type}",
                    stamp,
                ),
            )
            return CommandDecision(CommandDisposition.START)

    def complete_command(
        self,
        command_id: str,
        result_status: str,
        result_event_id: Optional[int],
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM ticket_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
            if existing is None:
                raise ValueError(f"command 不存在: {command_id}")
            if existing["status"] == "completed":
                if (
                    existing["result_status"] == result_status
                    and existing["result_event_id"] == result_event_id
                ):
                    return
                raise ValueError("completed command 不得用不同结果覆盖")
            if existing["status"] != "in_progress":
                raise ValueError("非 in_progress command 不得标记 completed")
            connection.execute(
                """
                UPDATE ticket_commands
                SET status = 'completed', result_status = ?, result_event_id = ?,
                    updated_at = ?
                WHERE command_id = ?
                """,
                (result_status, result_event_id, self._now(), command_id),
            )

    def fail_command(self, command_id: str, error_code: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM ticket_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
            if existing is None:
                raise ValueError(f"command 不存在: {command_id}")
            if existing["status"] == "failed":
                if existing["error_code"] == error_code:
                    return
                raise ValueError("failed command 不得用不同 error_code 覆盖")
            if existing["status"] != "in_progress":
                raise ValueError("非 in_progress command 不得标记 failed")
            connection.execute(
                """
                UPDATE ticket_commands
                SET status = 'failed', error_code = ?, updated_at = ?
                WHERE command_id = ?
                """,
                (error_code, self._now(), command_id),
            )

    def append_event(
        self,
        ticket_id: str,
        command_id: str,
        step_index: int,
        node_name: str,
        event_type: str,
        from_status: Optional[str],
        to_status: Optional[str],
        summary: str,
        metadata: Dict[str, object],
    ) -> int:
        key = f"command:{command_id}:{step_index}:{event_type}"
        metadata_json = self._json(metadata)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM ticket_events
                WHERE idempotency_key = ?
                   OR (ticket_id = ? AND command_id = ? AND step_index = ?)
                """,
                (key, ticket_id, command_id, step_index),
            ).fetchone()
            if existing is not None:
                same_event = (
                    existing["ticket_id"] == ticket_id
                    and existing["idempotency_key"] == key
                    and existing["command_id"] == command_id
                    and existing["step_index"] == step_index
                    and existing["node_name"] == node_name
                    and existing["event_type"] == event_type
                    and existing["from_status"] == from_status
                    and existing["to_status"] == to_status
                    and existing["summary"] == summary
                    and json.loads(existing["metadata_json"]) == metadata
                )
                if not same_event:
                    raise ValueError("event 幂等键已绑定到不同 payload")
                return int(existing["event_id"])

            ticket = connection.execute(
                "SELECT 1 FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if ticket is None:
                raise ValueError(f"工单不存在: {ticket_id}")
            cursor = connection.execute(
                """
                INSERT INTO ticket_events (
                    ticket_id, idempotency_key, command_id, step_index,
                    node_name, event_type, from_status, to_status,
                    summary, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    key,
                    command_id,
                    step_index,
                    node_name,
                    event_type,
                    from_status,
                    to_status,
                    summary,
                    metadata_json,
                    self._now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_events(self, ticket_id: str) -> List[Dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY event_id",
                (ticket_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_ticket(self, ticket_id: str, updates: Dict[str, object]) -> None:
        if not updates:
            raise ValueError("updates 不能为空")
        unknown = set(updates) - self._UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"禁止更新字段: {sorted(unknown)}")

        values = dict(updates)
        values["updated_at"] = self._now()
        assignments = ", ".join(f"{column} = ?" for column in values)
        parameters = list(values.values()) + [ticket_id]
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE tickets SET {assignments} WHERE ticket_id = ?",
                parameters,
            )
            if cursor.rowcount != 1:
                raise ValueError(f"工单不存在: {ticket_id}")
