import json
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


class CommandDisposition(str, Enum):
    START = "start"
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    RESUME = "resume"
    FAILED = "failed"


@dataclass(frozen=True)
class CommandDecision:
    disposition: CommandDisposition
    lease_version: int
    result_status: Optional[str] = None
    result_event_id: Optional[int] = None


STATUS_VALUES = {
    "new",
    "triaged",
    "diagnosing",
    "reviewing",
    "pending_user",
    "escalated",
    "resolved",
}
PRIORITY_VALUES = {"P0", "P1", "P2"}
RISK_LEVEL_VALUES = {"low", "medium", "high", "critical"}
REVIEW_DECISION_VALUES = {"approve", "revise", "escalate"}
CATEGORY_VALUES = {
    "power",
    "usb_connection",
    "mobile_connection",
    "bluetooth_connection",
    "screen_buttons",
    "pin_lock",
    "firmware_repair",
    "backup_recovery",
    "device_loss_damage",
    "warranty_service",
    "transaction_boundary",
    "security_incident",
    "security_report",
    "other",
}


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY CHECK(length(trim(ticket_id)) > 0),
    request_id TEXT NOT NULL UNIQUE CHECK(length(trim(request_id)) > 0),
    user_id TEXT NOT NULL CHECK(length(trim(user_id)) > 0),
    status TEXT NOT NULL CHECK(status IN (
        'new', 'triaged', 'diagnosing', 'reviewing', 'pending_user',
        'escalated', 'resolved'
    )),
    category TEXT CHECK(category IS NULL OR category IN (
        'power', 'usb_connection', 'mobile_connection', 'bluetooth_connection',
        'screen_buttons', 'pin_lock', 'firmware_repair', 'backup_recovery',
        'device_loss_damage', 'warranty_service', 'transaction_boundary',
        'security_incident', 'security_report', 'other'
    )),
    priority TEXT CHECK(priority IS NULL OR priority IN ('P0', 'P1', 'P2')),
    risk_level TEXT CHECK(
        risk_level IS NULL OR risk_level IN ('low', 'medium', 'high', 'critical')
    ),
    sanitized_input TEXT NOT NULL CHECK(length(trim(sanitized_input)) > 0),
    summary TEXT CHECK(summary IS NULL OR length(trim(summary)) > 0),
    risk_flags_json TEXT NOT NULL DEFAULT '[]'
        CHECK(json_valid(risk_flags_json) AND json_type(risk_flags_json) = 'array'),
    missing_fields_json TEXT NOT NULL DEFAULT '[]'
        CHECK(json_valid(missing_fields_json) AND json_type(missing_fields_json) = 'array'),
    evidence_refs_json TEXT NOT NULL DEFAULT '[]'
        CHECK(json_valid(evidence_refs_json) AND json_type(evidence_refs_json) = 'array'),
    draft_answer TEXT,
    final_answer TEXT,
    review_decision TEXT CHECK(
        review_decision IS NULL OR review_decision IN ('approve', 'revise', 'escalate')
    ),
    revision_count INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(revision_count) = 'integer' AND revision_count >= 0),
    response_version INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(response_version) = 'integer' AND response_version >= 0),
    requires_human INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(requires_human) = 'integer' AND requires_human IN (0, 1)),
    manual_gate_reason TEXT,
    created_at TEXT NOT NULL CHECK(length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK(length(trim(updated_at)) > 0)
);
CREATE TABLE IF NOT EXISTS ticket_commands (
    command_id TEXT PRIMARY KEY CHECK(length(trim(command_id)) > 0),
    ticket_id TEXT NOT NULL CHECK(length(trim(ticket_id)) > 0),
    command_type TEXT NOT NULL CHECK(length(trim(command_type)) > 0),
    status TEXT NOT NULL CHECK(status IN ('in_progress', 'completed', 'failed')),
    lease_expires_at TEXT NOT NULL CHECK(length(trim(lease_expires_at)) > 0),
    lease_version INTEGER NOT NULL DEFAULT 1
        CHECK(typeof(lease_version) = 'integer' AND lease_version >= 1),
    result_status TEXT CHECK(
        result_status IS NULL OR result_status IN (
            'new', 'triaged', 'diagnosing', 'reviewing', 'pending_user',
            'escalated', 'resolved'
        )
    ),
    result_event_id INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL CHECK(length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK(length(trim(updated_at)) > 0),
    UNIQUE(ticket_id, command_id),
    FOREIGN KEY(ticket_id) REFERENCES tickets(ticket_id),
    FOREIGN KEY(ticket_id, command_id, result_event_id)
        REFERENCES ticket_events(ticket_id, command_id, event_id)
);
CREATE TABLE IF NOT EXISTS ticket_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL CHECK(length(trim(ticket_id)) > 0),
    idempotency_key TEXT NOT NULL UNIQUE CHECK(length(trim(idempotency_key)) > 0),
    command_id TEXT NOT NULL CHECK(length(trim(command_id)) > 0),
    step_index INTEGER NOT NULL
        CHECK(typeof(step_index) = 'integer' AND step_index >= 0),
    node_name TEXT NOT NULL CHECK(length(trim(node_name)) > 0),
    event_type TEXT NOT NULL CHECK(length(trim(event_type)) > 0),
    from_status TEXT CHECK(from_status IS NULL OR from_status IN (
        'new', 'triaged', 'diagnosing', 'reviewing', 'pending_user',
        'escalated', 'resolved'
    )),
    to_status TEXT CHECK(to_status IS NULL OR to_status IN (
        'new', 'triaged', 'diagnosing', 'reviewing', 'pending_user',
        'escalated', 'resolved'
    )),
    summary TEXT NOT NULL CHECK(length(trim(summary)) > 0),
    metadata_json TEXT NOT NULL DEFAULT '{}'
        CHECK(json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
    created_at TEXT NOT NULL CHECK(length(trim(created_at)) > 0),
    UNIQUE(ticket_id, command_id, step_index),
    UNIQUE(ticket_id, command_id, event_id),
    FOREIGN KEY(ticket_id, command_id)
        REFERENCES ticket_commands(ticket_id, command_id)
);
"""


_REDACTED_PATTERN = re.compile(r"\[REDACTED_[A-Z0-9_]+\]", re.IGNORECASE)
_MNEMONIC_PATTERN = re.compile(
    r"(?<![A-Za-z])(?:[a-z]{3,12}[ \t\r\n]+){11,23}[a-z]{3,12}(?![A-Za-z])",
    re.ASCII,
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"(?:private[_ -]?key|私钥)\s*[:=：]\s*(?:0x)?[0-9a-fA-F]{64}\b",
    re.IGNORECASE,
)
_PIN_PATTERN = re.compile(r"\bpin\s*[:=：]\s*\d{4,8}\b", re.IGNORECASE)
_PASSPHRASE_PATTERN = re.compile(
    r"(?:passphrase|password|seed[_ -]?phrase|mnemonic|助记词|密码|口令)"
    r"\s*[:=：]\s*\S+",
    re.IGNORECASE,
)
_SENSITIVE_METADATA_KEYS = {
    "pin",
    "passphrase",
    "password",
    "seed_phrase",
    "mnemonic",
    "private_key",
    "secret",
    "助记词",
    "私钥",
    "密码",
    "口令",
}


class TicketRepository:
    _BUSY_TIMEOUT_MS = 5000
    _JSON_LIST_FIELDS = {
        "risk_flags_json",
        "missing_fields_json",
        "evidence_refs_json",
    }
    _UPDATABLE_FIELDS = {
        "status",
        "category",
        "priority",
        "risk_level",
        "summary",
        *_JSON_LIST_FIELDS,
        "draft_answer",
        "final_answer",
        "review_decision",
        "revision_count",
        "response_version",
        "requires_human",
        "manual_gate_reason",
    }
    _EVENT_FIELDS = {
        "step_index",
        "node_name",
        "event_type",
        "from_status",
        "to_status",
        "summary",
        "metadata",
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

    @staticmethod
    def _nonempty_text(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} 必须是非空字符串")
        return value

    @classmethod
    def _optional_text(cls, value: object, field_name: str) -> Optional[str]:
        if value is None:
            return None
        return cls._nonempty_text(value, field_name)

    @staticmethod
    def _positive_integer(value: object, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field_name} 必须是正整数")
        return value

    @staticmethod
    def _nonnegative_integer(value: object, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field_name} 必须是非负整数")
        return value

    @classmethod
    def _normalize_string_list(cls, value: object, field_name: str) -> List[str]:
        if not isinstance(value, list):
            raise ValueError(f"{field_name} 必须是 list[str]")
        normalized = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{field_name} 必须是 list[str]")
            normalized.append(item.strip())
        return normalized

    @staticmethod
    def _assert_no_secret(text: str) -> None:
        candidate = _REDACTED_PATTERN.sub("", text)
        if any(
            pattern.search(candidate)
            for pattern in (
                _MNEMONIC_PATTERN,
                _PRIVATE_KEY_PATTERN,
                _PIN_PATTERN,
                _PASSPHRASE_PATTERN,
            )
        ):
            raise ValueError("检测到未脱敏的敏感信息")

    @classmethod
    def _assert_safe_metadata(cls, value: object, parent_key: str = "") -> None:
        if isinstance(value, str):
            if parent_key.lower() in _SENSITIVE_METADATA_KEYS:
                if _REDACTED_PATTERN.fullmatch(value.strip()) is None:
                    raise ValueError("检测到未脱敏的敏感信息")
            cls._assert_no_secret(value)
            return
        if isinstance(value, dict):
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise ValueError("metadata 的 key 必须是字符串")
                cls._assert_safe_metadata(nested, key)
            return
        if isinstance(value, list):
            for nested in value:
                cls._assert_safe_metadata(nested, parent_key)
            return
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("metadata 只能包含有限 JSON 数值")
        if value is not None and not isinstance(value, (bool, int, float)):
            raise ValueError("metadata 只能包含 JSON 值")

    @staticmethod
    def _enum_value(
        value: object,
        field_name: str,
        allowed: set,
        allow_none: bool = True,
    ) -> Optional[str]:
        if value is None and allow_none:
            return None
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(f"{field_name} 非法")
        return value

    @classmethod
    def _prepare_updates(cls, updates: Dict[str, object]) -> Dict[str, object]:
        if not isinstance(updates, dict) or not updates:
            raise ValueError("updates 不能为空")
        unknown = set(updates) - cls._UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"禁止更新字段: {sorted(unknown)}")

        prepared: Dict[str, object] = {}
        for field, value in updates.items():
            if field == "status":
                prepared[field] = cls._enum_value(
                    value, field, STATUS_VALUES, allow_none=False
                )
            elif field == "category":
                prepared[field] = cls._enum_value(value, field, CATEGORY_VALUES)
            elif field == "priority":
                prepared[field] = cls._enum_value(value, field, PRIORITY_VALUES)
            elif field == "risk_level":
                prepared[field] = cls._enum_value(value, field, RISK_LEVEL_VALUES)
            elif field == "review_decision":
                prepared[field] = cls._enum_value(
                    value, field, REVIEW_DECISION_VALUES
                )
            elif field in cls._JSON_LIST_FIELDS:
                prepared[field] = cls._json(
                    cls._normalize_string_list(value, field)
                )
            elif field in {"revision_count", "response_version"}:
                prepared[field] = cls._nonnegative_integer(value, field)
            elif field == "requires_human":
                if not isinstance(value, bool):
                    raise ValueError("requires_human 必须是 bool")
                prepared[field] = int(value)
            else:
                text = cls._optional_text(value, field)
                if text is not None:
                    cls._assert_no_secret(text)
                prepared[field] = text
        return prepared

    @staticmethod
    def _event_key(command_id: str, step_index: int, event_type: str) -> str:
        return f"command:{command_id}:{step_index}:{event_type}"

    @classmethod
    def _prepare_event(cls, event: Dict[str, object]) -> Dict[str, object]:
        if not isinstance(event, dict) or set(event) != cls._EVENT_FIELDS:
            raise ValueError("event 字段不完整或包含未知字段")
        step_index = cls._nonnegative_integer(event["step_index"], "step_index")
        node_name = cls._nonempty_text(event["node_name"], "node_name")
        event_type = cls._nonempty_text(event["event_type"], "event_type")
        summary = cls._nonempty_text(event["summary"], "summary")
        cls._assert_no_secret(summary)
        from_status = event["from_status"]
        to_status = event["to_status"]
        if from_status is not None and from_status not in STATUS_VALUES:
            raise ValueError("from_status 非法")
        if to_status is not None and to_status not in STATUS_VALUES:
            raise ValueError("to_status 非法")
        metadata = event["metadata"]
        if not isinstance(metadata, dict):
            raise ValueError("metadata 必须是 dict")
        cls._assert_safe_metadata(metadata)
        return {
            "step_index": step_index,
            "node_name": node_name,
            "event_type": event_type,
            "from_status": from_status,
            "to_status": to_status,
            "summary": summary,
            "metadata": metadata,
            "metadata_json": cls._json(metadata),
        }

    @staticmethod
    def _command_row(
        connection: sqlite3.Connection, command_id: str
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM ticket_commands WHERE command_id = ?", (command_id,)
        ).fetchone()

    @staticmethod
    def _check_command_owner(
        command: sqlite3.Row,
        ticket_id: Optional[str],
        lease_version: int,
    ) -> None:
        if ticket_id is not None and command["ticket_id"] != ticket_id:
            raise ValueError("command 不属于指定工单")
        if command["lease_version"] != lease_version:
            raise ValueError("lease_version 已过期")

    @staticmethod
    def _event_matches(
        existing: sqlite3.Row,
        ticket_id: str,
        command_id: str,
        key: str,
        event: Dict[str, object],
    ) -> bool:
        return (
            existing["ticket_id"] == ticket_id
            and existing["command_id"] == command_id
            and existing["idempotency_key"] == key
            and existing["step_index"] == event["step_index"]
            and existing["node_name"] == event["node_name"]
            and existing["event_type"] == event["event_type"]
            and existing["from_status"] == event["from_status"]
            and existing["to_status"] == event["to_status"]
            and existing["summary"] == event["summary"]
            and json.loads(existing["metadata_json"]) == event["metadata"]
        )

    def create_ticket(
        self,
        request_id: str,
        user_id: str,
        sanitized_input: str,
        risk_flags: List[str],
    ) -> Dict[str, object]:
        request_id = self._nonempty_text(request_id, "request_id")
        user_id = self._nonempty_text(user_id, "user_id")
        sanitized_input = self._nonempty_text(sanitized_input, "sanitized_input")
        self._assert_no_secret(sanitized_input)
        normalized_flags = self._normalize_string_list(risk_flags, "risk_flags")
        risk_flags_json = self._json(normalized_flags)
        now = self._now()
        ticket_id = f"KG-{uuid.uuid4().hex[:12].upper()}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM tickets WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["user_id"] != user_id
                    or existing["sanitized_input"] != sanitized_input
                    or json.loads(existing["risk_flags_json"]) != normalized_flags
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
        ticket_id = self._nonempty_text(ticket_id, "ticket_id")
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
        ticket_id = self._nonempty_text(ticket_id, "ticket_id")
        command_id = self._nonempty_text(command_id, "command_id")
        command_type = self._nonempty_text(command_type, "command_type")
        self._assert_no_secret(command_type)
        lease_seconds = self._positive_integer(lease_seconds, "lease_seconds")
        now = datetime.now(timezone.utc)
        lease = (now + timedelta(seconds=lease_seconds)).isoformat()
        stamp = now.isoformat()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._command_row(connection, command_id)
            if existing is not None:
                if (
                    existing["ticket_id"] != ticket_id
                    or existing["command_type"] != command_type
                ):
                    raise ValueError(
                        "command_id 已绑定到不同的 ticket_id 或 command_type"
                    )
                version = int(existing["lease_version"])
                if existing["status"] == "completed":
                    return CommandDecision(
                        CommandDisposition.COMPLETED,
                        version,
                        existing["result_status"],
                        existing["result_event_id"],
                    )
                if existing["status"] == "failed":
                    return CommandDecision(CommandDisposition.FAILED, version)

                expires = datetime.fromisoformat(existing["lease_expires_at"])
                if expires <= now:
                    version += 1
                    cursor = connection.execute(
                        """
                        UPDATE ticket_commands
                        SET lease_expires_at = ?, lease_version = ?, updated_at = ?
                        WHERE command_id = ? AND status = 'in_progress'
                        """,
                        (lease, version, stamp, command_id),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("command lease 续租失败")
                    return CommandDecision(CommandDisposition.RESUME, version)
                return CommandDecision(CommandDisposition.IN_PROGRESS, version)

            ticket = connection.execute(
                "SELECT status FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if ticket is None:
                raise ValueError(f"工单不存在: {ticket_id}")
            connection.execute(
                """
                INSERT INTO ticket_commands (
                    command_id, ticket_id, command_type, status,
                    lease_expires_at, lease_version, created_at, updated_at
                ) VALUES (?, ?, ?, 'in_progress', ?, 1, ?, ?)
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
                    self._event_key(command_id, 0, "command.accepted"),
                    command_id,
                    ticket["status"],
                    ticket["status"],
                    f"接受命令 {command_type}",
                    stamp,
                ),
            )
            return CommandDecision(CommandDisposition.START, 1)

    def _validate_result_event(
        self,
        connection: sqlite3.Connection,
        command: sqlite3.Row,
        result_event_id: Optional[int],
    ) -> None:
        if result_event_id is None:
            return
        result_event_id = self._positive_integer(result_event_id, "result_event_id")
        event = connection.execute(
            """
            SELECT 1 FROM ticket_events
            WHERE event_id = ? AND ticket_id = ? AND command_id = ?
            """,
            (result_event_id, command["ticket_id"], command["command_id"]),
        ).fetchone()
        if event is None:
            raise ValueError("result_event_id 不属于当前 command")

    def complete_command(
        self,
        command_id: str,
        lease_version: int,
        result_status: str,
        result_event_id: Optional[int],
    ) -> None:
        command_id = self._nonempty_text(command_id, "command_id")
        lease_version = self._positive_integer(lease_version, "lease_version")
        if result_status not in STATUS_VALUES:
            raise ValueError("result_status 非法")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._command_row(connection, command_id)
            if existing is None:
                raise ValueError(f"command 不存在: {command_id}")
            self._check_command_owner(existing, None, lease_version)
            if existing["status"] == "completed":
                if (
                    existing["result_status"] == result_status
                    and existing["result_event_id"] == result_event_id
                ):
                    return
                raise ValueError("completed command 不得用不同结果覆盖")
            if existing["status"] != "in_progress":
                raise ValueError("非 in_progress command 不得标记 completed")
            self._validate_result_event(connection, existing, result_event_id)
            cursor = connection.execute(
                """
                UPDATE ticket_commands
                SET status = 'completed', result_status = ?, result_event_id = ?,
                    updated_at = ?
                WHERE command_id = ? AND status = 'in_progress' AND lease_version = ?
                """,
                (
                    result_status,
                    result_event_id,
                    self._now(),
                    command_id,
                    lease_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("command 完成时 lease 已失效")

    def fail_command(
        self, command_id: str, lease_version: int, error_code: str
    ) -> None:
        command_id = self._nonempty_text(command_id, "command_id")
        lease_version = self._positive_integer(lease_version, "lease_version")
        error_code = self._nonempty_text(error_code, "error_code")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._command_row(connection, command_id)
            if existing is None:
                raise ValueError(f"command 不存在: {command_id}")
            self._check_command_owner(existing, None, lease_version)
            if existing["status"] == "failed":
                if existing["error_code"] == error_code:
                    return
                raise ValueError("failed command 不得用不同 error_code 覆盖")
            if existing["status"] != "in_progress":
                raise ValueError("非 in_progress command 不得标记 failed")
            cursor = connection.execute(
                """
                UPDATE ticket_commands
                SET status = 'failed', error_code = ?, updated_at = ?
                WHERE command_id = ? AND status = 'in_progress' AND lease_version = ?
                """,
                (error_code, self._now(), command_id, lease_version),
            )
            if cursor.rowcount != 1:
                raise ValueError("command 失败写入时 lease 已失效")

    def _append_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        ticket_id: str,
        command_id: str,
        lease_version: int,
        event: Dict[str, object],
    ) -> int:
        command = self._command_row(connection, command_id)
        if command is None:
            raise ValueError(f"command 不存在: {command_id}")
        self._check_command_owner(command, ticket_id, lease_version)
        if command["status"] != "in_progress":
            raise ValueError("只有 in_progress command 可以写事件")

        key = self._event_key(
            command_id, int(event["step_index"]), str(event["event_type"])
        )
        existing = connection.execute(
            """
            SELECT * FROM ticket_events
            WHERE idempotency_key = ?
               OR (ticket_id = ? AND command_id = ? AND step_index = ?)
            """,
            (key, ticket_id, command_id, event["step_index"]),
        ).fetchone()
        if existing is not None:
            if not self._event_matches(existing, ticket_id, command_id, key, event):
                raise ValueError("event 幂等键已绑定到不同 payload")
            return int(existing["event_id"])

        last_step = connection.execute(
            """
            SELECT MAX(step_index) FROM ticket_events
            WHERE ticket_id = ? AND command_id = ?
            """,
            (ticket_id, command_id),
        ).fetchone()[0]
        if last_step is not None and event["step_index"] <= last_step:
            raise ValueError("step_index 必须严格递增")
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
                event["step_index"],
                event["node_name"],
                event["event_type"],
                event["from_status"],
                event["to_status"],
                event["summary"],
                event["metadata_json"],
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    def append_event(
        self,
        ticket_id: str,
        command_id: str,
        lease_version: int,
        step_index: int,
        node_name: str,
        event_type: str,
        from_status: Optional[str],
        to_status: Optional[str],
        summary: str,
        metadata: Dict[str, object],
    ) -> int:
        ticket_id = self._nonempty_text(ticket_id, "ticket_id")
        command_id = self._nonempty_text(command_id, "command_id")
        lease_version = self._positive_integer(lease_version, "lease_version")
        event = self._prepare_event(
            {
                "step_index": step_index,
                "node_name": node_name,
                "event_type": event_type,
                "from_status": from_status,
                "to_status": to_status,
                "summary": summary,
                "metadata": metadata,
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._append_event_in_transaction(
                connection, ticket_id, command_id, lease_version, event
            )

    def list_events(self, ticket_id: str) -> List[Dict[str, object]]:
        ticket_id = self._nonempty_text(ticket_id, "ticket_id")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY event_id",
                (ticket_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _update_ticket_in_transaction(
        connection: sqlite3.Connection,
        ticket_id: str,
        prepared: Dict[str, object],
        updated_at: str,
    ) -> None:
        values = dict(prepared)
        values["updated_at"] = updated_at
        assignments = ", ".join(f"{column} = ?" for column in values)
        parameters = list(values.values()) + [ticket_id]
        cursor = connection.execute(
            f"UPDATE tickets SET {assignments} WHERE ticket_id = ?", parameters
        )
        if cursor.rowcount != 1:
            raise ValueError(f"工单不存在: {ticket_id}")

    def update_ticket(self, ticket_id: str, updates: Dict[str, object]) -> None:
        ticket_id = self._nonempty_text(ticket_id, "ticket_id")
        prepared = self._prepare_updates(updates)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._update_ticket_in_transaction(
                connection, ticket_id, prepared, self._now()
            )

    def _completed_commit_matches(
        self,
        connection: sqlite3.Connection,
        command: sqlite3.Row,
        prepared_updates: Dict[str, object],
        events: Iterable[Dict[str, object]],
    ) -> Tuple[bool, Optional[int]]:
        ticket = connection.execute(
            "SELECT * FROM tickets WHERE ticket_id = ?", (command["ticket_id"],)
        ).fetchone()
        if ticket is None or any(
            ticket[field] != value for field, value in prepared_updates.items()
        ):
            return False, None

        last_event_id = None
        for event in events:
            key = self._event_key(
                command["command_id"], event["step_index"], event["event_type"]
            )
            existing = connection.execute(
                """
                SELECT * FROM ticket_events
                WHERE ticket_id = ? AND command_id = ? AND step_index = ?
                """,
                (command["ticket_id"], command["command_id"], event["step_index"]),
            ).fetchone()
            if existing is None or not self._event_matches(
                existing,
                command["ticket_id"],
                command["command_id"],
                key,
                event,
            ):
                return False, None
            last_event_id = int(existing["event_id"])
        return (
            command["result_status"] == ticket["status"]
            and command["result_event_id"] == last_event_id,
            last_event_id,
        )

    def commit_command_result(
        self,
        ticket_id: str,
        command_id: str,
        lease_version: int,
        updates: Dict[str, object],
        events: List[Dict[str, object]],
    ) -> CommandDecision:
        ticket_id = self._nonempty_text(ticket_id, "ticket_id")
        command_id = self._nonempty_text(command_id, "command_id")
        lease_version = self._positive_integer(lease_version, "lease_version")
        prepared_updates = self._prepare_updates(updates)
        if not isinstance(events, list) or not events:
            raise ValueError("events 必须是非空 list")
        prepared_events = [self._prepare_event(event) for event in events]

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            command = self._command_row(connection, command_id)
            if command is None:
                raise ValueError(f"command 不存在: {command_id}")
            self._check_command_owner(command, ticket_id, lease_version)
            if command["status"] == "completed":
                matches, last_event_id = self._completed_commit_matches(
                    connection, command, prepared_updates, prepared_events
                )
                if not matches:
                    raise ValueError("completed command 的结果 payload 冲突")
                return CommandDecision(
                    CommandDisposition.COMPLETED,
                    lease_version,
                    command["result_status"],
                    last_event_id,
                )
            if command["status"] != "in_progress":
                raise ValueError("只有 in_progress command 可以提交结果")

            stamp = self._now()
            self._update_ticket_in_transaction(
                connection, ticket_id, prepared_updates, stamp
            )
            last_event_id = None
            for event in prepared_events:
                last_event_id = self._append_event_in_transaction(
                    connection, ticket_id, command_id, lease_version, event
                )
            ticket_status = connection.execute(
                "SELECT status FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()["status"]
            cursor = connection.execute(
                """
                UPDATE ticket_commands
                SET status = 'completed', result_status = ?, result_event_id = ?,
                    updated_at = ?
                WHERE command_id = ? AND ticket_id = ?
                  AND status = 'in_progress' AND lease_version = ?
                """,
                (
                    ticket_status,
                    last_event_id,
                    stamp,
                    command_id,
                    ticket_id,
                    lease_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("提交结果时 command lease 已失效")
            return CommandDecision(
                CommandDisposition.COMPLETED,
                lease_version,
                ticket_status,
                last_event_id,
            )
