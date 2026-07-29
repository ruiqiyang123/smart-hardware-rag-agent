"""Safe runtime boundary for the checkpointed support graph.

Raw caller input is sanitized before this module touches the repository or the
graph.  Commands are fenced by durable leases and graph results are projected
onto the repository's narrow atomic completion API.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command, Interrupt

from agent.orchestration.events import make_event, to_repository_event
from agent.orchestration.invoke import invoke_with_policy
from agent.orchestration.routes import assert_transition
from agent.orchestration.state import (
    DiagnosisResult,
    MissingField,
    RiskFlag,
    RiskLevel,
    Status,
    TicketState,
)
from agent.security.secrets import TransactionHash, contains_unredacted_secret
from agent.security.trusted_sources import TrustedSourcePolicy
from database.ticket_db import (
    CommandDecision,
    CommandDisposition,
    CommandType,
)
from utils.logger_handler import logger


_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}", re.ASCII)
_STATE_FIELDS = frozenset(TicketState.__annotations__)
_INITIAL_STATE_FIELDS = frozenset(
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
    }
)
_HISTORY_ROLES = frozenset({"user", "assistant"})
_HUMAN_COMMAND_TYPES = {
    "approve": CommandType.HUMAN_APPROVE,
    "edit_send": CommandType.HUMAN_EDIT_SEND,
    "ask_user": CommandType.HUMAN_ASK,
    "reject": CommandType.HUMAN_REJECT,
}
_MISSING_FIELDS = frozenset(item.value for item in MissingField)
_RISK_FLAGS = frozenset(item.value for item in RiskFlag)
_RISK_LEVELS = frozenset(item.value for item in RiskLevel)
_CRITICAL_FLAGS = frozenset(
    {
        RiskFlag.SECRET_EXPOSURE.value,
        RiskFlag.PHISHING.value,
        RiskFlag.ASSET_LOSS.value,
    }
)
_HIGH_FLAGS = frozenset(
    {
        RiskFlag.UNOFFICIAL_FIRMWARE.value,
        RiskFlag.ADDRESS_MISMATCH.value,
        RiskFlag.SUSPICIOUS_SIGNATURE.value,
        RiskFlag.DEVICE_AUTH_FAILURE.value,
        RiskFlag.REMOTE_CONTROL.value,
    }
)
_DB_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "ticket_id",
        "idempotency_key",
        "command_id",
        "step_index",
        "node_name",
        "event_type",
        "from_status",
        "to_status",
        "summary",
        "metadata_json",
        "created_at",
    }
)

ERROR_USER_MESSAGES = {
    "TRIAGE_TIMEOUT": "自动分诊暂不可用，工单已保留并转人工。",
    "TRIAGE_FAILURE": "自动分诊暂不可用，工单已保留并转人工。",
    "DIAGNOSIS_NO_EVIDENCE": "未找到足够依据，未自动生成处理结论。",
    "DIAGNOSIS_FAILURE": "自动诊断暂不可用，工单已保留并转人工。",
    "TOOL_FAILURE": "事实查询失败，工单已保留并转人工。",
    "REVIEW_FAILURE": "安全复核失败，草稿未发送。",
    "CHECKPOINT_MISMATCH": "恢复状态异常，已停止自动执行。",
    "GRAPH_TIMEOUT": "自动流程超时，未发送草稿，可用同一请求确认结果。",
    "GRAPH_EXECUTION_FAILED": "自动流程执行失败，草稿未发送。",
    "PERSISTENCE_FAILURE": "结果状态暂时无法确认，未发送草稿，可用同一请求确认结果。",
}


@dataclass(frozen=True)
class OrchestrationResult:
    ticket_id: str
    status: str
    sanitized_input: str
    user_notice: str
    final_answer: str
    citations: List[dict]
    events: List[dict]
    duplicate: bool = False


class CheckpointRestoreError(RuntimeError):
    """A leased command could not safely continue from its checkpoint."""


class CommandInProgressError(RuntimeError):
    """The same idempotent command is still running."""


class CommandFailedError(RuntimeError):
    """The same idempotent command already reached a failed terminal state."""


class PersistenceFailureError(RuntimeError):
    """Durable command state could not be safely confirmed."""


@dataclass(frozen=True, init=False, slots=True)
class PreparedUserInput:
    """Ingress-validated, secret-free user command payload."""

    sanitized_input: str
    risk_level: str
    risk_flags: tuple[str, ...]
    critical_notice: str
    _issuer: object = field(repr=False, compare=False)
    signature: bytes = field(repr=False, compare=False)

    @classmethod
    def _issue(
        cls,
        value: _ValidatedIngress,
        issuer: object,
        signature: bytes,
    ) -> "PreparedUserInput":
        prepared = object.__new__(cls)
        object.__setattr__(prepared, "sanitized_input", value.sanitized_input)
        object.__setattr__(prepared, "risk_level", value.risk_level)
        object.__setattr__(prepared, "risk_flags", tuple(value.risk_flags))
        object.__setattr__(prepared, "critical_notice", value.critical_notice)
        object.__setattr__(prepared, "_issuer", issuer)
        object.__setattr__(prepared, "signature", signature)
        return prepared


@dataclass(frozen=True, init=False, slots=True)
class PreparedHumanAction:
    """Strictly projected, secret-free human action payload."""

    action: str
    edited_answer: str
    missing_fields: tuple[str, ...]
    _issuer: object = field(repr=False, compare=False)
    signature: bytes = field(repr=False, compare=False)

    @classmethod
    def _issue(
        cls,
        *,
        action: str,
        edited_answer: str,
        missing_fields: tuple[str, ...],
        issuer: object,
        signature: bytes,
    ) -> "PreparedHumanAction":
        prepared = object.__new__(cls)
        object.__setattr__(prepared, "action", action)
        object.__setattr__(prepared, "edited_answer", edited_answer)
        object.__setattr__(prepared, "missing_fields", missing_fields)
        object.__setattr__(prepared, "_issuer", issuer)
        object.__setattr__(prepared, "signature", signature)
        return prepared


class _NoCheckpointWritesSafety:
    """Explicit test-only assertion that a graph never writes checkpoints."""


NO_CHECKPOINT_WRITES = _NoCheckpointWritesSafety()


class LeaseFencedCheckpointer(BaseCheckpointSaver):
    """Fence writes for KeyGuard's fixed graph; dynamic Send/push is unsupported."""

    def __init__(self, delegate: object, repository: object) -> None:
        if not callable(getattr(delegate, "get_tuple", None)) or not callable(
            getattr(delegate, "put", None)
        ) or not callable(getattr(delegate, "put_writes", None)):
            raise TypeError("delegate checkpointer 接口非法")
        if not callable(getattr(repository, "guarded_checkpoint_write", None)):
            raise TypeError("repository checkpoint lease guard 接口非法")
        super().__init__(serde=getattr(delegate, "serde", None))
        self.delegate = delegate
        self.repository = repository

    @property
    def config_specs(self) -> list:
        return list(getattr(self.delegate, "config_specs", []))

    @staticmethod
    def _lease(config: object) -> tuple[str, str, int]:
        if not isinstance(config, Mapping):
            raise TypeError("checkpoint config 非法")
        configurable = config.get("configurable")
        if not isinstance(configurable, Mapping):
            raise ValueError("checkpoint config 缺少 configurable")
        ticket_id = _identifier(configurable.get("thread_id"), "thread_id")
        command_id = _identifier(configurable.get("command_id"), "command_id")
        lease_version = configurable.get("lease_version")
        if (
            isinstance(lease_version, bool)
            or not isinstance(lease_version, int)
            or lease_version <= 0
        ):
            raise ValueError("checkpoint lease_version 非法")
        return ticket_id, command_id, lease_version

    def get_tuple(self, config):
        return self.delegate.get_tuple(config)

    def list(self, config, *, filter=None, before=None, limit=None):
        return self.delegate.list(
            config, filter=filter, before=before, limit=limit
        )

    def put(self, config, checkpoint, metadata, new_versions):
        ticket_id, command_id, lease_version = self._lease(config)
        projected_checkpoint = _project_checkpoint(checkpoint)
        projected_metadata = _strict_json(metadata)
        projected_versions = _strict_json(new_versions)
        if not isinstance(projected_metadata, dict) or not isinstance(
            projected_versions, dict
        ):
            raise TypeError("checkpoint metadata/version 非法")

        def write():
            return self.delegate.put(
                config,
                projected_checkpoint,
                projected_metadata,
                projected_versions,
            )

        result = self.repository.guarded_checkpoint_write(
            ticket_id, command_id, lease_version, write
        )
        if not isinstance(result, Mapping):
            raise TypeError("delegate checkpoint put 输出非法")
        projected = copy.deepcopy(dict(result))
        configurable = dict(projected.get("configurable", {}))
        configurable.update(
            {"command_id": command_id, "lease_version": lease_version}
        )
        projected["configurable"] = configurable
        return projected

    def put_writes(self, config, writes, task_id, task_path="") -> None:
        ticket_id, command_id, lease_version = self._lease(config)
        projected_writes = _project_checkpoint_writes(writes)
        if (
            _strict_json(task_id) != task_id
            or _strict_json(task_path) != task_path
        ):
            raise ValueError("checkpoint task 标识非法")

        def write():
            return self.delegate.put_writes(
                config, projected_writes, task_id, task_path
            )

        self.repository.guarded_checkpoint_write(
            ticket_id, command_id, lease_version, write
        )

    def delete_thread(self, thread_id: str) -> None:
        return self.delegate.delete_thread(thread_id)

    def get_next_version(self, current, channel):
        return self.delegate.get_next_version(current, channel)

    def get_delta_channel_history(self, *, config, channels):
        method = getattr(self.delegate, "get_delta_channel_history", None)
        if callable(method):
            return method(config=config, channels=channels)
        return super().get_delta_channel_history(config=config, channels=channels)


@dataclass(frozen=True)
class _ValidatedIngress:
    sanitized_input: str
    risk_level: str
    risk_flags: list[str]
    critical_notice: str


def _forbidden_control(value: str) -> bool:
    return any(
        (ord(character) < 0x20 and character not in {"\n", "\t"})
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    )


def _safe_text(
    value: object,
    field: str,
    max_length: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError(f"{field} 非法")
    if _forbidden_control(value):
        raise ValueError(f"{field} 非法")
    normalized = " ".join(value.split())
    if not allow_empty and not normalized:
        raise ValueError(f"{field} 非法")
    if contains_unredacted_secret(normalized):
        raise ValueError(f"{field} 包含未脱敏内容")
    return normalized


def _identifier(value: object, field: str, max_length: int = 128) -> str:
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or _IDENTIFIER_PATTERN.fullmatch(value) is None
        or contains_unredacted_secret(value)
    ):
        raise ValueError(f"{field} 非法")
    return value


def _strict_json(value: object, path: tuple[str, ...] = (), depth: int = 0):
    if depth > 32:
        raise ValueError("JSON 嵌套过深")
    if isinstance(value, str):
        if len(value) > 32 * 1024 or _forbidden_control(value):
            raise ValueError("JSON 文本非法")
        if path and path[-1] == "transaction_hash":
            return TransactionHash(value).value
        if contains_unredacted_secret(value):
            raise ValueError("JSON 包含未脱敏内容")
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON 包含非有限数")
        return value
    if isinstance(value, list):
        return [
            _strict_json(item, path + ("[]",), depth + 1) for item in value
        ]
    if isinstance(value, dict):
        copied = {}
        for key, nested in value.items():
            if not isinstance(key, str) or _forbidden_control(key):
                raise ValueError("JSON key 非法")
            if contains_unredacted_secret(key):
                raise ValueError("JSON key 包含未脱敏内容")
            copied[key] = _strict_json(nested, path + (key,), depth + 1)
        return copied
    raise TypeError("只允许严格 JSON 值")


def _project_ticket_state(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise TypeError("checkpoint state 必须是映射")
    unknown = set(value) - _STATE_FIELDS
    if unknown:
        raise ValueError("checkpoint state 包含未知字段")
    projected = _strict_json(dict(value))
    if not isinstance(projected, dict):  # pragma: no cover - defensive
        raise TypeError("checkpoint state 非法")
    return projected


def _project_interrupts(value: object):
    if not isinstance(value, (list, tuple)):
        raise TypeError("checkpoint interrupt 通道非法")
    projected = []
    for item in value:
        if not isinstance(item, Interrupt):
            raise TypeError("checkpoint interrupt 条目非法")
        interrupt_id = _identifier(item.id, "interrupt.id")
        interrupt_value = _strict_json(item.value)
        projected.append(Interrupt(value=interrupt_value, id=interrupt_id))
    return tuple(projected) if isinstance(value, tuple) else projected


def _project_checkpoint_channel(channel: object, value: object):
    if (
        not isinstance(channel, str)
        or not channel
        or len(channel) > 512
        or _forbidden_control(channel)
        or contains_unredacted_secret(channel)
    ):
        raise ValueError("checkpoint channel 非法")
    if channel == "__start__":
        return _project_ticket_state(value)
    if channel in _STATE_FIELDS:
        return _project_ticket_state({channel: value})[channel]
    if channel == "__interrupt__":
        return _project_interrupts(value)
    if channel == "__error__":
        raise ValueError("checkpoint error 对象不得持久化")
    if channel in {"__pregel_push", "__pregel_tasks"}:
        empty_internal = value is None or (
            isinstance(value, (str, list, dict)) and len(value) == 0
        )
        if not empty_internal:
            raise ValueError("KeyGuard 不支持动态 Send/push checkpoint")
        return _strict_json(value)
    if channel in {
        "__input__",
        "__no_writes__",
        "__overwrite__",
        "__previous__",
        "__resume__",
        "__return__",
        "__scheduled__",
    }:
        return _strict_json(value)
    if channel.startswith("branch:"):
        return _strict_json(value)
    raise ValueError("checkpoint 包含未知持久化通道")


def _project_checkpoint(checkpoint: object) -> dict:
    if not isinstance(checkpoint, Mapping):
        raise TypeError("checkpoint 非法")
    channel_values = checkpoint.get("channel_values")
    if not isinstance(channel_values, Mapping):
        raise TypeError("checkpoint channel_values 非法")
    projected_channels = {
        channel: _project_checkpoint_channel(channel, value)
        for channel, value in channel_values.items()
    }
    projected = _strict_json(
        {key: value for key, value in checkpoint.items() if key != "channel_values"}
    )
    if not isinstance(projected, dict):  # pragma: no cover - defensive
        raise TypeError("checkpoint 非法")
    projected["channel_values"] = projected_channels
    return projected


def _project_checkpoint_writes(writes: object) -> list[tuple[str, object]]:
    if isinstance(writes, (str, bytes)):
        raise TypeError("checkpoint writes 非法")
    try:
        entries = list(writes)
    except TypeError:
        raise TypeError("checkpoint writes 非法") from None
    projected = []
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise TypeError("checkpoint write 条目非法")
        channel, value = entry
        projected.append((channel, _project_checkpoint_channel(channel, value)))
    return projected


def _sqlite_database_paths(checkpointer: object) -> set[Path]:
    connection = getattr(checkpointer, "_managed_connection", None)
    if not isinstance(connection, sqlite3.Connection):
        connection = getattr(checkpointer, "conn", None)
    if not isinstance(connection, sqlite3.Connection):
        return set()
    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        raise ValueError("无法验证 SQLite checkpointer 数据库路径") from None
    paths = set()
    for row in rows:
        filename = row[2]
        if isinstance(filename, str) and filename:
            paths.add(Path(filename).expanduser().resolve())
    return paths


def _assert_separate_checkpoint_database(
    repository: object, checkpointer: object
) -> None:
    repository_path = getattr(repository, "db_path", None)
    if not isinstance(repository_path, (str, Path)) or not str(repository_path):
        return
    saver_paths = _sqlite_database_paths(checkpointer)
    if not saver_paths:
        return
    resolved_repository = Path(repository_path).expanduser().resolve()
    if resolved_repository in saver_paths:
        raise ValueError("ticket repository 与 SQLite checkpointer 不得共用数据库")


def project_safe_history(value: object) -> list[dict[str, str]]:
    """Return the exact bounded, secret-free history accepted by the runtime."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 24:
        raise ValueError("safe_history 非法")
    projected: list[dict[str, str]] = []
    total_length = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise ValueError("safe_history 条目非法")
        role = item["role"]
        if not isinstance(role, str) or role not in _HISTORY_ROLES:
            raise ValueError("safe_history role 非法")
        content = _safe_text(item["content"], "safe_history.content", 10_000)
        total_length += len(content)
        if total_length > 32_000:
            raise ValueError("safe_history 总长度超限")
        projected.append({"role": role, "content": content})
    return projected


def _safe_history(value: object) -> list[dict[str, str]]:
    return project_safe_history(value)


def _validated_ingress(value: object) -> _ValidatedIngress:
    required = {
        "sanitized_input",
        "risk_level",
        "risk_flags",
        "critical_notice",
    }
    if any(not hasattr(value, field) for field in required):
        raise TypeError("ingress 输出契约非法")
    sanitized_input = _safe_text(
        getattr(value, "sanitized_input"), "sanitized_input", 10_000
    )
    risk_level = getattr(value, "risk_level")
    if not isinstance(risk_level, str) or risk_level not in _RISK_LEVELS:
        raise ValueError("ingress risk_level 非法")
    risk_flags = _string_list(
        getattr(value, "risk_flags"), "ingress risk_flags", 16
    )
    if any(flag not in _RISK_FLAGS for flag in risk_flags):
        raise ValueError("ingress risk_flags 非法")
    flags = set(risk_flags)
    if flags & _CRITICAL_FLAGS and risk_level != RiskLevel.CRITICAL.value:
        raise ValueError("ingress critical flag 与 risk_level 不一致")
    if flags & _HIGH_FLAGS and risk_level not in {
        RiskLevel.HIGH.value,
        RiskLevel.CRITICAL.value,
    }:
        raise ValueError("ingress high flag 与 risk_level 不一致")
    critical_notice = _safe_text(
        getattr(value, "critical_notice"),
        "critical_notice",
        2_000,
        allow_empty=True,
    )
    if risk_level == RiskLevel.CRITICAL.value and not critical_notice:
        raise ValueError("critical ingress 缺少固定安全提示")
    return _ValidatedIngress(
        sanitized_input=sanitized_input,
        risk_level=risk_level,
        risk_flags=risk_flags,
        critical_notice=critical_notice,
    )


def _payload_fingerprint(payload: dict) -> str:
    safe_payload = _strict_json(payload)
    canonical = json.dumps(
        safe_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _optional_output_text(
    value: object, field: str, max_length: int
) -> Optional[str]:
    if value is None or value == "":
        return None
    return _safe_text(value, field, max_length)


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} 非法")
    return value


def _string_list(value: object, field: str, maximum: int = 32) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} 非法")
    result = [_safe_text(item, field, 300) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{field} 不得重复")
    return result


def _status(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("status 非法")
    try:
        return Status(value).value
    except ValueError:
        raise ValueError("status 非法") from None


class SupportOrchestrator:
    def __init__(
        self,
        repository,
        graph,
        ingress_guard,
        recursion_limit: int,
        lease_seconds: int,
        graph_timeout_seconds: float = 120,
        trusted_source_policy: object | None = None,
        final_policy_guard: object | None = None,
        checkpoint_safety: object | None = None,
    ):
        if (
            isinstance(recursion_limit, bool)
            or not isinstance(recursion_limit, int)
            or recursion_limit <= 0
        ):
            raise ValueError("recursion_limit 必须是正整数")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds 必须是正整数")
        if (
            isinstance(graph_timeout_seconds, bool)
            or not isinstance(graph_timeout_seconds, (int, float))
            or not math.isfinite(graph_timeout_seconds)
            or graph_timeout_seconds <= 0
        ):
            raise ValueError("graph_timeout_seconds 必须是有限正数")
        if graph_timeout_seconds >= lease_seconds:
            raise ValueError("graph_timeout_seconds 必须严格小于 command lease")
        if not callable(getattr(repository, "begin_command", None)):
            raise TypeError("repository 接口非法")
        if not callable(getattr(graph, "invoke", None)):
            raise TypeError("graph 接口非法")
        if not callable(getattr(ingress_guard, "sanitize", None)):
            raise TypeError("ingress_guard 接口非法")
        if not callable(getattr(final_policy_guard, "evaluate", None)):
            raise TypeError("final_policy_guard 接口非法")

        missing = object()
        graph_checkpointer = getattr(graph, "checkpointer", missing)
        if graph_checkpointer is missing:
            if checkpoint_safety is not NO_CHECKPOINT_WRITES:
                raise TypeError(
                    "无 checkpointer 的测试图必须显式声明 NO_CHECKPOINT_WRITES"
                )
        else:
            if checkpoint_safety is not None:
                raise ValueError("生产图不得绕过 checkpoint lease fence")
            if graph_checkpointer is None:
                raise TypeError("生产图必须配置 checkpointer")
            if isinstance(graph_checkpointer, LeaseFencedCheckpointer):
                if graph_checkpointer.repository is not repository:
                    raise ValueError("checkpointer lease fence repository 不匹配")
                _assert_separate_checkpoint_database(
                    repository, graph_checkpointer.delegate
                )
            else:
                _assert_separate_checkpoint_database(repository, graph_checkpointer)
                graph.checkpointer = LeaseFencedCheckpointer(
                    graph_checkpointer, repository
                )

        self.repository = repository
        self.graph = graph
        self.ingress_guard = ingress_guard
        self.recursion_limit = recursion_limit
        self.lease_seconds = lease_seconds
        self.graph_timeout_seconds = float(graph_timeout_seconds)
        self._prepared_issuer = object()
        self._prepared_signing_key = secrets.token_bytes(32)
        self.trusted_sources = trusted_source_policy or TrustedSourcePolicy()
        if not callable(getattr(self.trusted_sources, "is_trusted", None)):
            raise TypeError("trusted_source_policy 接口非法")
        guard_sources = getattr(final_policy_guard, "trusted_sources", None)
        if guard_sources is not None and guard_sources is not self.trusted_sources:
            raise ValueError(
                "final_policy_guard 与 runtime 必须共享 trusted_source_policy"
            )
        self.final_policy_guard = final_policy_guard

    def _config(
        self,
        ticket_id: str,
        command_id: str | None = None,
        lease_version: int | None = None,
    ) -> dict:
        configurable = {"thread_id": _identifier(ticket_id, "ticket_id")}
        if (command_id is None) != (lease_version is None):
            raise ValueError("command_id 与 lease_version 必须同时提供")
        if command_id is not None:
            configurable["command_id"] = _identifier(command_id, "command_id")
            if (
                isinstance(lease_version, bool)
                or not isinstance(lease_version, int)
                or lease_version <= 0
            ):
                raise ValueError("lease_version 非法")
            configurable["lease_version"] = lease_version
        return {
            "configurable": configurable,
            "recursion_limit": self.recursion_limit,
        }

    def _checkpoint_values(self, ticket_id: str) -> dict:
        get_state = getattr(self.graph, "get_state", None)
        if not callable(get_state):
            return {}
        snapshot = get_state(self._config(ticket_id))
        values = getattr(snapshot, "values", None)
        if values is None:
            return {}
        if not isinstance(values, Mapping):
            raise TypeError("checkpoint state 非法")
        return copy.deepcopy(dict(values))

    def get_state(self, ticket_id: str) -> dict:
        values = self._checkpoint_values(ticket_id)
        return self._validated_graph_output(values) if values else {}

    def get_verified_citations(self, ticket_id: str) -> list[dict]:
        """Return citations only after revalidating their complete evidence bundle."""
        state = self.get_state(ticket_id)
        return self._validated_evidence_bundle(state) if state else []

    def _invoke_graph(
        self,
        graph_input,
        ticket_id: str,
        command_id: str,
        lease_version: int,
    ) -> dict:
        output = invoke_with_policy(
            lambda: self.graph.invoke(
                graph_input,
                config=self._config(ticket_id, command_id, lease_version),
            ),
            timeout_seconds=self.graph_timeout_seconds,
            retries=0,
        )
        if isinstance(output, Mapping) and "__interrupt__" in output:
            interrupts = output.get("__interrupt__")
            unknown = set(output) - _STATE_FIELDS - {"__interrupt__"}
            if (
                unknown
                or not isinstance(interrupts, (list, tuple))
                or not interrupts
                or any(not isinstance(item, Interrupt) for item in interrupts)
            ):
                raise ValueError("graph interrupt 输出非法")
            checkpoint = self._checkpoint_values(ticket_id)
            if not checkpoint:
                raise ValueError("graph interrupt 缺少 checkpoint state")
            return self._validated_graph_output(checkpoint)
        return self._validated_graph_output(output)

    @staticmethod
    def _validated_graph_output(output: object) -> dict:
        if not isinstance(output, Mapping):
            raise TypeError("graph 输出必须是映射")
        if "__interrupt__" in output:
            raise ValueError("graph 输出不得持久化 __interrupt__")
        try:
            return copy.deepcopy(_project_ticket_state(output))
        except ValueError as error:
            if "未知字段" in str(error):
                raise ValueError("graph 输出包含未知 state 字段") from None
            raise

    def _resume_expired_command(
        self,
        ticket_id: str,
        command_id: str,
        lease_version: int,
    ) -> dict:
        ticket = self.repository.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"工单不存在: {ticket_id}")
        ticket_status = _status(ticket.get("status"))
        consistent = False
        try:
            checkpoint = self._checkpoint_values(ticket_id)
            if checkpoint:
                checkpoint = self._validated_graph_output(checkpoint)
                consistent = (
                    checkpoint.get("ticket_id") == ticket_id
                    and checkpoint.get("status") == ticket_status
                    and ticket_status in {
                        Status.PENDING_USER.value,
                        Status.ESCALATED.value,
                    }
                )
        except Exception:
            consistent = False
        if not consistent:
            self.repository.fail_checkpoint_restore(
                ticket_id=ticket_id,
                command_id=command_id,
                lease_version=lease_version,
                expected_ticket_status=ticket_status,
            )
            raise CheckpointRestoreError("CHECKPOINT_MISMATCH") from None
        return self._invoke_graph(None, ticket_id, command_id, lease_version)

    def _fail_command_safely(
        self, command_id: str, lease_version: int, error_code: str
    ) -> bool:
        try:
            self.repository.fail_command(command_id, lease_version, error_code)
            return True
        except Exception:
            return False

    @staticmethod
    def _log_failure(
        ticket_id: str,
        node_name: str,
        error_code: str,
        error: BaseException,
    ) -> None:
        """Log stable metadata without request input or exception details."""

        logger.error(
            "[orchestration] error_code=%s ticket_id=%s node_name=%s exception_type=%s",
            error_code,
            ticket_id,
            node_name,
            type(error).__name__,
        )

    def _execute(
        self,
        *,
        ticket_id: str,
        command_id: str,
        decision: CommandDecision,
        graph_input,
        command_type: str,
        human_action: str | None = None,
        expected_final_answer: str | None = None,
    ) -> None:
        try:
            if decision.disposition == CommandDisposition.RESUME:
                output = self._resume_expired_command(
                    ticket_id,
                    command_id,
                    decision.lease_version,
                )
            elif decision.disposition == CommandDisposition.START:
                output = self._invoke_graph(
                    graph_input,
                    ticket_id,
                    command_id,
                    decision.lease_version,
                )
            else:
                raise ValueError("command disposition 不可执行")
        except TimeoutError as error:
            self._log_failure(
                ticket_id, "runtime", "GRAPH_TIMEOUT", error
            )
            try:
                self.repository.expire_command_lease(
                    ticket_id, command_id, decision.lease_version
                )
            except Exception as persistence_error:
                self._log_failure(
                    ticket_id,
                    "runtime",
                    "PERSISTENCE_FAILURE",
                    persistence_error,
                )
                raise PersistenceFailureError(
                    ERROR_USER_MESSAGES["PERSISTENCE_FAILURE"]
                ) from None
            raise TimeoutError(ERROR_USER_MESSAGES["GRAPH_TIMEOUT"]) from None
        except CheckpointRestoreError as error:
            self._log_failure(
                ticket_id, "runtime", "CHECKPOINT_MISMATCH", error
            )
            raise CheckpointRestoreError("CHECKPOINT_MISMATCH") from None
        except Exception as error:
            self._log_failure(
                ticket_id, "runtime", "GRAPH_EXECUTION_FAILED", error
            )
            failed = self._fail_command_safely(
                command_id,
                decision.lease_version,
                "GRAPH_EXECUTION_FAILED",
            )
            if failed:
                raise CommandFailedError(
                    ERROR_USER_MESSAGES["GRAPH_EXECUTION_FAILED"]
                ) from None
            raise PersistenceFailureError(
                ERROR_USER_MESSAGES["PERSISTENCE_FAILURE"]
            ) from None

        try:
            self._persist_graph_result(
                ticket_id,
                command_id,
                decision.lease_version,
                output,
                command_type,
                human_action,
                expected_final_answer,
            )
        except PersistenceFailureError:
            raise
        except Exception as error:
            self._log_failure(
                ticket_id, "runtime", "GRAPH_EXECUTION_FAILED", error
            )
            failed = self._fail_command_safely(
                command_id,
                decision.lease_version,
                "GRAPH_EXECUTION_FAILED",
            )
            if failed:
                raise CommandFailedError(
                    ERROR_USER_MESSAGES["GRAPH_EXECUTION_FAILED"]
                ) from None
            raise PersistenceFailureError(
                ERROR_USER_MESSAGES["PERSISTENCE_FAILURE"]
            ) from None

    def _persist_graph_result(
        self,
        ticket_id: str,
        command_id: str,
        lease_version: int,
        output: object,
        command_type: str = CommandType.USER_INPUT.value,
        human_action: str | None = None,
        expected_final_answer: str | None = None,
    ) -> None:
        state = self._validated_graph_output(output)
        if state.get("ticket_id") != ticket_id:
            raise ValueError("graph ticket_id 与 command 不一致")
        if state.get("command_id") != command_id:
            raise ValueError("graph command_id 与当前 command 不一致")
        final_status = _status(state.get("status"))
        citations = self._validated_evidence_bundle(state)
        self._validate_terminal_state(
            state,
            final_status,
            command_type,
            human_action,
            expected_final_answer,
            citations,
        )

        updates = {
            "status": final_status,
            "category": state.get("category"),
            "priority": state.get("priority"),
            "risk_level": state.get("risk_level"),
            "summary": _optional_output_text(state.get("summary"), "summary", 300),
            "risk_flags_json": _string_list(
                state.get("risk_flags", []), "risk_flags", 16
            ),
            "missing_fields_json": _string_list(
                state.get("missing_fields", []), "missing_fields", 16
            ),
            "evidence_refs_json": _string_list(
                state.get("evidence_refs", []), "evidence_refs", 32
            ),
            "draft_answer": _optional_output_text(
                state.get("draft_answer"), "draft_answer", 2_000
            ),
            "final_answer": _optional_output_text(
                state.get("final_answer"), "final_answer", 2_000
            ),
            "review_decision": state.get("review_decision") or None,
            "revision_count": _nonnegative_integer(
                state.get("revision_count", 0), "revision_count"
            ),
            "response_version": _nonnegative_integer(
                state.get("response_version", 0), "response_version"
            ),
            "requires_human": state.get("requires_human", False),
            "manual_gate_reason": _optional_output_text(
                state.get("manual_gate_reason"), "manual_gate_reason", 500
            ),
        }
        if not isinstance(updates["requires_human"], bool):
            raise ValueError("requires_human 非法")

        raw_events = state.get("status_events")
        if not isinstance(raw_events, list):
            raise ValueError("status_events 非法")
        events = []
        for raw_event in raw_events:
            if not isinstance(raw_event, Mapping):
                raise ValueError("status_event 非法")
            if raw_event.get("command_id") == command_id:
                events.append(to_repository_event(raw_event))
        if not events:
            raise ValueError("当前 command 缺少可提交事件")
        steps = [event["step_index"] for event in events]
        if steps != sorted(steps) or len(steps) != len(set(steps)):
            raise ValueError("当前 command 事件顺序非法")
        if events[-1]["event_type"] != f"ticket.{final_status}":
            terminal = make_event(
                command_id=command_id,
                step_index=steps[-1] + 1,
                node_name="runtime",
                event_type=f"ticket.{final_status}",
                summary="工作流结果已原子提交",
                from_status=final_status,
                to_status=final_status,
            )
            events.append(to_repository_event(terminal))

        ticket = self.repository.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"工单不存在: {ticket_id}")
        self._validate_runtime_event_chain(
            _status(ticket.get("status")), final_status, events
        )

        try:
            self.repository.commit_command_result(
                ticket_id=ticket_id,
                command_id=command_id,
                lease_version=lease_version,
                updates=updates,
                events=events,
            )
        except Exception as error:
            self._log_failure(
                ticket_id, "runtime", "PERSISTENCE_FAILURE", error
            )
            raise PersistenceFailureError(
                ERROR_USER_MESSAGES["PERSISTENCE_FAILURE"]
            ) from None

    @staticmethod
    def _validate_runtime_event_chain(
        current_status: str,
        final_status: str,
        events: list[dict],
    ) -> None:
        expected = current_status
        steps = [event["step_index"] for event in events]
        if steps != sorted(steps) or len(steps) != len(set(steps)):
            raise ValueError("runtime 事件 step_index 非法")
        for event in events:
            from_status = event["from_status"]
            to_status = event["to_status"]
            if from_status is None or to_status is None:
                raise ValueError("runtime 事件链缺少状态")
            if from_status != expected:
                raise ValueError("runtime 事件链 from_status 不连续")
            if to_status != from_status:
                assert_transition(from_status, to_status)
            expected = to_status
        if expected != final_status:
            raise ValueError("runtime 事件链末状态不匹配")

    def _validated_evidence_bundle(self, state: Mapping[str, object]) -> list[dict]:
        evidence_refs = _string_list(
            state.get("evidence_refs", []), "evidence_refs", 32
        )
        raw_evidence = state.get("evidence", [])
        if not isinstance(raw_evidence, list) or len(raw_evidence) > 16:
            raise ValueError("checkpoint evidence 非法")
        evidence_by_id: dict[str, dict] = {}
        for raw_item in raw_evidence:
            required = {
                "evidence_id",
                "kind",
                "content",
                "source_title",
                "source_url",
            }
            allowed = required | {"metadata"}
            if (
                not isinstance(raw_item, dict)
                or not required.issubset(raw_item)
                or not set(raw_item).issubset(allowed)
            ):
                raise ValueError("checkpoint evidence 字段非法")
            evidence_id = _safe_text(
                raw_item["evidence_id"], "evidence.evidence_id", 128
            )
            kind = raw_item["kind"]
            if kind not in {"knowledge", "profile", "device", "warranty", "chain"}:
                raise ValueError("checkpoint evidence kind 非法")
            content = _safe_text(raw_item["content"], "evidence.content", 32 * 1024)
            title = _safe_text(
                raw_item["source_title"], "evidence.source_title", 500
            )
            url = raw_item["source_url"]
            if url is not None:
                url = _safe_text(url, "evidence.source_url", 2_000)
                if not self.trusted_sources.is_trusted(url):
                    raise ValueError("checkpoint evidence URL 不可信")
            if "metadata" in raw_item:
                metadata = _strict_json(raw_item["metadata"])
                if not isinstance(metadata, dict):
                    raise ValueError("checkpoint evidence metadata 非法")
            if evidence_id in evidence_by_id:
                raise ValueError("checkpoint evidence_id 重复")
            evidence_by_id[evidence_id] = {
                "evidence_id": evidence_id,
                "kind": kind,
                "content": content,
                "source_title": title,
                "source_url": url,
            }
        if not set(evidence_refs).issubset(evidence_by_id):
            raise ValueError("checkpoint evidence_refs 包含未知证据")

        citations = self._citations(state.get("citations", []))
        citation_by_id = {item["source_id"]: item for item in citations}
        for citation in citations:
            source = evidence_by_id.get(citation["source_id"])
            if (
                source is None
                or citation["source_id"] not in evidence_refs
                or citation["source_title"] != source["source_title"]
                or citation["source_url"] != source["source_url"]
            ):
                raise ValueError("checkpoint citation 与 evidence 不匹配")
        for evidence_id in evidence_refs:
            source = evidence_by_id[evidence_id]
            if (
                source["kind"] == "knowledge"
                and source["source_url"] is not None
                and evidence_id not in citation_by_id
            ):
                raise ValueError("checkpoint knowledge evidence 缺少 citation")
        return citations

    def _final_policy_passes(self, final_answer: str, citations: list[dict]) -> None:
        urls = [citation["source_url"] for citation in citations]
        try:
            decision = self.final_policy_guard.evaluate(final_answer, urls)
        except Exception:
            raise ValueError("最终答复 Policy Guard 执行失败") from None
        passed = getattr(decision, "passed", None)
        reasons = getattr(decision, "reason_codes", None)
        if passed is not True or not isinstance(reasons, list) or reasons:
            raise ValueError("最终答复未通过 Policy Guard")

    def _validate_terminal_state(
        self,
        state: Mapping[str, object],
        final_status: str,
        command_type: str,
        human_action: str | None,
        expected_final_answer: str | None,
        citations: list[dict],
    ) -> None:
        requires_human = state.get("requires_human", False)
        if not isinstance(requires_human, bool):
            raise ValueError("requires_human 非法")
        final_answer = _safe_text(
            state.get("final_answer", ""),
            "final_answer",
            2_000,
            allow_empty=True,
        )
        if final_status != Status.RESOLVED.value:
            if final_answer:
                raise ValueError("非 resolved 状态不得包含 final_answer")
            if final_status == Status.ESCALATED.value and not requires_human:
                raise ValueError("escalated 状态必须 requires_human")
            return

        if not final_answer or requires_human:
            raise ValueError("resolved 状态必须包含答复且关闭人工门")
        review_decision = state.get("review_decision")
        human_decision = state.get("human_decision")
        if command_type == CommandType.USER_INPUT.value:
            if review_decision != "approve" or human_decision not in {None, ""}:
                raise ValueError("USER_INPUT resolved 必须由 Review approve")
            try:
                DiagnosisResult.model_validate(
                    {
                        "outcome": state.get("outcome"),
                        "diagnosis_summary": state.get("diagnosis_summary"),
                        "recommended_actions": state.get("recommended_actions"),
                        "evidence_refs": state.get("evidence_refs"),
                        "citations": state.get("citations"),
                        "draft_answer": state.get("draft_answer"),
                        "remaining_unknowns": state.get("remaining_unknowns"),
                    }
                )
            except (TypeError, ValueError):
                raise ValueError("USER_INPUT resolved diagnosis 语义非法") from None
            if state.get("tool_errors") != []:
                raise ValueError("USER_INPUT resolved tool_errors 必须为空")
            if (
                state.get("review_reasons") != ["passed"]
                or state.get("review_issues") != []
                or state.get("required_changes") != []
            ):
                raise ValueError("USER_INPUT resolved review 语义非法")
            if state.get("final_answer") != state.get("draft_answer"):
                raise ValueError("USER_INPUT resolved 答复与审核草稿不一致")
        else:
            if human_action not in {"approve", "edit_send"}:
                raise ValueError("ask_user/reject 不得 resolved")
            expected_type = _HUMAN_COMMAND_TYPES[human_action].value
            if command_type != expected_type or human_decision != human_action:
                raise ValueError("human resolved decision 与 action 不匹配")
            if (
                not isinstance(expected_final_answer, str)
                or not expected_final_answer
                or state.get("final_answer") != expected_final_answer
            ):
                raise ValueError("human resolved 答复与人工审核文本不一致")
        self._final_policy_passes(final_answer, citations)

    def _approved_checkpoint_answer(self, ticket_id: str) -> str:
        checkpoint = self._checkpoint_values(ticket_id)
        if not checkpoint:
            raise ValueError("approve 缺少 checkpoint")
        state = self._validated_graph_output(checkpoint)
        if state.get("ticket_id") != ticket_id:
            raise ValueError("checkpoint ticket_id 不匹配")
        status = _status(state.get("status"))
        if status not in {Status.ESCALATED.value, Status.RESOLVED.value}:
            raise ValueError("approve checkpoint 状态非法")
        citations = self._validated_evidence_bundle(state)
        raw_draft = state.get("draft_answer")
        draft = _safe_text(raw_draft, "checkpoint draft_answer", 2_000)
        if raw_draft != draft:
            raise ValueError("checkpoint draft_answer 必须是规范安全文本")
        if status == Status.ESCALATED.value:
            self._validate_terminal_state(
                state,
                status,
                CommandType.HUMAN_APPROVE.value,
                None,
                None,
                citations,
            )
        else:
            self._validate_terminal_state(
                state,
                status,
                CommandType.HUMAN_APPROVE.value,
                "approve",
                draft,
                citations,
            )
        return draft

    def _citations(self, value: object) -> list[dict]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 16:
            raise ValueError("checkpoint citations 非法")
        citations = []
        source_ids = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {
                "source_id",
                "source_title",
                "source_url",
            }:
                raise ValueError("checkpoint citation 字段非法")
            source_id = _safe_text(item["source_id"], "citation.source_id", 128)
            title = _safe_text(item["source_title"], "citation.source_title", 500)
            url = _safe_text(item["source_url"], "citation.source_url", 2_000)
            if not self.trusted_sources.is_trusted(url):
                raise ValueError("checkpoint citation URL 不可信")
            if source_id in source_ids:
                raise ValueError("checkpoint citation 重复")
            source_ids.add(source_id)
            citations.append(
                {
                    "source_id": source_id,
                    "source_title": title,
                    "source_url": url,
                }
            )
        return citations

    @staticmethod
    def _events(value: object) -> list[dict]:
        if not isinstance(value, list):
            raise ValueError("database events 非法")
        projected = []
        for row in value:
            if not isinstance(row, dict) or set(row) != _DB_EVENT_FIELDS:
                raise ValueError("database event 字段非法")
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise ValueError("database event metadata 非法") from error
            metadata = _strict_json(metadata)
            if not isinstance(metadata, dict):
                raise ValueError("database event metadata 非法")
            event_id = row["event_id"]
            step_index = row["step_index"]
            if (
                isinstance(event_id, bool)
                or not isinstance(event_id, int)
                or event_id <= 0
                or isinstance(step_index, bool)
                or not isinstance(step_index, int)
                or step_index < 0
            ):
                raise ValueError("database event index 非法")
            projected.append(
                {
                    "event_id": event_id,
                    "ticket_id": _identifier(row["ticket_id"], "event.ticket_id"),
                    "idempotency_key": _safe_text(
                        row["idempotency_key"], "event.idempotency_key", 512
                    ),
                    "command_id": _identifier(row["command_id"], "event.command_id"),
                    "step_index": step_index,
                    "node_name": _safe_text(row["node_name"], "event.node_name", 64),
                    "event_type": _safe_text(
                        row["event_type"], "event.event_type", 128
                    ),
                    "from_status": (
                        _status(row["from_status"])
                        if row["from_status"] is not None
                        else None
                    ),
                    "to_status": (
                        _status(row["to_status"])
                        if row["to_status"] is not None
                        else None
                    ),
                    "summary": _safe_text(row["summary"], "event.summary", 500),
                    "metadata": metadata,
                    "created_at": _safe_text(
                        row["created_at"], "event.created_at", 128
                    ),
                }
            )
        return projected

    def _result(
        self,
        ticket_id: str,
        sanitized_input: str,
        user_notice: str,
        duplicate: bool = False,
    ) -> OrchestrationResult:
        ticket = self.repository.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"工单不存在: {ticket_id}")
        checkpoint = self._checkpoint_values(ticket_id)
        citations = self._validated_evidence_bundle(checkpoint)
        final_answer = ticket.get("final_answer") or ""
        if final_answer:
            final_answer = _safe_text(final_answer, "final_answer", 2_000)
        result_status = _status(ticket.get("status"))
        requires_human = ticket.get("requires_human")
        if requires_human not in {0, 1}:
            raise ValueError("ticket requires_human 非法")
        if result_status == Status.RESOLVED.value:
            if not final_answer or requires_human != 0:
                raise ValueError("resolved ticket 终态非法")
            self._final_policy_passes(final_answer, citations)
        elif final_answer:
            raise ValueError("非 resolved ticket 不得返回 final_answer")
        sanitized_input = _safe_text(
            sanitized_input, "sanitized_input", 10_000, allow_empty=True
        )
        user_notice = _safe_text(
            user_notice, "user_notice", 2_000, allow_empty=True
        )
        error_code = checkpoint.get("last_error")
        if not user_notice and isinstance(error_code, str):
            user_notice = ERROR_USER_MESSAGES.get(error_code, "")
        return OrchestrationResult(
            ticket_id=_identifier(ticket_id, "ticket_id"),
            status=result_status,
            sanitized_input=sanitized_input,
            user_notice=user_notice,
            final_answer=final_answer,
            citations=citations,
            events=self._events(self.repository.list_events(ticket_id)),
            duplicate=duplicate,
        )

    def _existing_decision_result(
        self,
        decision: CommandDecision,
        *,
        ticket_id: str,
        sanitized_input: str,
        user_notice: str,
    ) -> Optional[OrchestrationResult]:
        if decision.disposition == CommandDisposition.COMPLETED:
            return self._result(
                ticket_id,
                sanitized_input,
                user_notice,
                duplicate=True,
            )
        if decision.disposition == CommandDisposition.IN_PROGRESS:
            raise CommandInProgressError("command 仍在执行")
        if decision.disposition == CommandDisposition.FAILED:
            raise CommandFailedError("command 已进入失败终态")
        return None

    def _sign_prepared_payload(self, kind: str, payload: dict) -> bytes:
        fingerprint = _payload_fingerprint(
            {"kind": kind, "payload": payload}
        )
        return hmac.new(
            self._prepared_signing_key,
            bytes.fromhex(fingerprint),
            hashlib.sha256,
        ).digest()

    @staticmethod
    def _prepared_user_fields(value: object) -> dict:
        risk_flags = getattr(value, "risk_flags")
        return {
            "sanitized_input": getattr(value, "sanitized_input"),
            "risk_level": getattr(value, "risk_level"),
            "risk_flags": (
                list(risk_flags)
                if isinstance(risk_flags, (list, tuple))
                else risk_flags
            ),
            "critical_notice": getattr(value, "critical_notice"),
        }

    @staticmethod
    def _prepared_human_fields(value: object) -> dict:
        missing_fields = getattr(value, "missing_fields")
        return {
            "action": getattr(value, "action"),
            "edited_answer": getattr(value, "edited_answer"),
            "missing_fields": (
                list(missing_fields)
                if isinstance(missing_fields, (list, tuple))
                else missing_fields
            ),
        }

    def prepare_user_input(self, raw_input: str) -> PreparedUserInput:
        """Sanitize caller text once, then issue a runtime-bound safe payload."""
        # This must remain the first operation involving caller-controlled text.
        validated = _validated_ingress(self.ingress_guard.sanitize(raw_input))
        fields = {
            "sanitized_input": validated.sanitized_input,
            "risk_level": validated.risk_level,
            "risk_flags": list(validated.risk_flags),
            "critical_notice": validated.critical_notice,
        }
        return PreparedUserInput._issue(
            validated,
            self._prepared_issuer,
            self._sign_prepared_payload("user_input", fields),
        )

    def _validated_prepared_user_input(
        self, value: object
    ) -> _ValidatedIngress:
        if type(value) is not PreparedUserInput:
            raise TypeError("prepared user input 类型非法")
        if getattr(value, "_issuer", None) is not self._prepared_issuer:
            raise ValueError("prepared user input 签发者非法")
        try:
            expected_signature = self._sign_prepared_payload(
                "user_input", self._prepared_user_fields(value)
            )
        except Exception:
            raise ValueError("prepared user input 签名载荷非法") from None
        signature = getattr(value, "signature", None)
        if not isinstance(signature, bytes) or not hmac.compare_digest(
            signature, expected_signature
        ):
            raise ValueError("prepared user input 签名非法")
        if not isinstance(value.risk_flags, tuple):
            raise TypeError("prepared risk_flags 类型非法")
        validated = _validated_ingress(
            _ValidatedIngress(
                sanitized_input=value.sanitized_input,
                risk_level=value.risk_level,
                risk_flags=list(value.risk_flags),
                critical_notice=value.critical_notice,
            )
        )
        flags = set(validated.risk_flags)
        if flags & _CRITICAL_FLAGS:
            expected_level = RiskLevel.CRITICAL.value
        elif flags & _HIGH_FLAGS:
            expected_level = RiskLevel.HIGH.value
        else:
            expected_level = RiskLevel.LOW.value
        if validated.risk_level != expected_level:
            raise ValueError("prepared risk_level 与 risk_flags 不一致")
        if expected_level != RiskLevel.CRITICAL.value and validated.critical_notice:
            raise ValueError("非 critical prepared input 不得包含安全提示")
        return validated

    def submit(
        self,
        raw_input: str,
        user_id: str,
        request_id: Optional[str] = None,
        safe_history: Optional[List[dict]] = None,
    ) -> OrchestrationResult:
        return self.submit_prepared(
            self.prepare_user_input(raw_input),
            user_id=user_id,
            request_id=request_id,
            safe_history=safe_history,
        )

    def submit_prepared(
        self,
        prepared: PreparedUserInput,
        user_id: str,
        request_id: Optional[str] = None,
        safe_history: Optional[List[dict]] = None,
    ) -> OrchestrationResult:
        sanitized = self._validated_prepared_user_input(prepared)
        history = _safe_history(safe_history)
        request_id = uuid.uuid4().hex if request_id is None else request_id
        request_id = _identifier(request_id, "request_id", 120)
        user_id = _identifier(user_id, "user_id")
        sanitized_input = sanitized.sanitized_input
        risk_flags = list(sanitized.risk_flags)
        fingerprint = _payload_fingerprint(
            {
                "kind": "submit",
                "user_id": user_id,
                "sanitized_input": sanitized_input,
                "risk_level": sanitized.risk_level,
                "risk_flags": risk_flags,
                "safe_history": history,
            }
        )
        ticket = self.repository.create_ticket(
            request_id,
            user_id,
            sanitized_input,
            risk_flags,
        )
        ticket_id = _identifier(ticket["ticket_id"], "ticket_id")
        command_id = _identifier(f"request:{request_id}", "command_id")
        decision = self.repository.begin_command(
            ticket_id,
            command_id,
            CommandType.USER_INPUT.value,
            self.lease_seconds,
            payload_fingerprint=fingerprint,
            required_status=Status.NEW.value,
        )
        duplicate = self._existing_decision_result(
            decision,
            ticket_id=ticket_id,
            sanitized_input=sanitized_input,
            user_notice=sanitized.critical_notice,
        )
        if duplicate is not None:
            return duplicate

        state = {
            "ticket_id": ticket_id,
            "request_id": request_id,
            "command_id": command_id,
            "event_step": 0,
            "user_id": user_id,
            "sanitized_input": sanitized_input,
            "safe_history": history,
            "sensitive_flags": list(risk_flags),
            "risk_level": sanitized.risk_level,
            "risk_flags": list(risk_flags),
            "revision_count": 0,
            "response_version": 0,
            "status": _status(ticket["status"]),
            "requires_human": sanitized.risk_level in {"high", "critical"},
            "status_events": [],
        }
        if set(state) != _INITIAL_STATE_FIELDS:
            raise AssertionError("runtime initial state 投影发生漂移")
        self._execute(
            ticket_id=ticket_id,
            command_id=command_id,
            decision=decision,
            graph_input=state,
            command_type=CommandType.USER_INPUT.value,
        )
        return self._result(
            ticket_id,
            sanitized_input,
            sanitized.critical_notice,
        )

    def resume_user(
        self,
        ticket_id: str,
        raw_input: str,
        request_id: Optional[str] = None,
    ) -> OrchestrationResult:
        return self.resume_user_prepared(
            ticket_id,
            self.prepare_user_input(raw_input),
            request_id=request_id,
        )

    def resume_user_prepared(
        self,
        ticket_id: str,
        prepared: PreparedUserInput,
        request_id: Optional[str] = None,
    ) -> OrchestrationResult:
        sanitized = self._validated_prepared_user_input(prepared)
        ticket_id = _identifier(ticket_id, "ticket_id")
        request_id = uuid.uuid4().hex if request_id is None else request_id
        request_id = _identifier(request_id, "request_id", 120)
        sanitized_input = sanitized.sanitized_input
        risk_flags = list(sanitized.risk_flags)
        fingerprint = _payload_fingerprint(
            {
                "kind": "resume_user",
                "ticket_id": ticket_id,
                "sanitized_input": sanitized_input,
                "risk_level": sanitized.risk_level,
                "risk_flags": risk_flags,
            }
        )
        command_id = _identifier(f"request:{request_id}", "command_id")
        decision = self.repository.begin_command(
            ticket_id,
            command_id,
            CommandType.USER_INPUT.value,
            self.lease_seconds,
            payload_fingerprint=fingerprint,
            required_status=Status.PENDING_USER.value,
        )
        duplicate = self._existing_decision_result(
            decision,
            ticket_id=ticket_id,
            sanitized_input=sanitized_input,
            user_notice=sanitized.critical_notice,
        )
        if duplicate is not None:
            return duplicate

        resume_command = Command(
            resume={
                "command_id": command_id,
                "request_id": request_id,
                "sanitized_input": sanitized_input,
                "sensitive_flags": list(risk_flags),
                "risk_flags": list(risk_flags),
                "risk_level": sanitized.risk_level,
            }
        )
        self._execute(
            ticket_id=ticket_id,
            command_id=command_id,
            decision=decision,
            graph_input=resume_command,
            command_type=CommandType.USER_INPUT.value,
        )
        return self._result(
            ticket_id,
            sanitized_input,
            sanitized.critical_notice,
        )

    def human_action(
        self,
        ticket_id: str,
        action: str,
        edited_answer: str = "",
        missing_fields: Optional[List[str]] = None,
        action_id: Optional[str] = None,
    ) -> OrchestrationResult:
        return self.human_action_prepared(
            ticket_id,
            self.prepare_human_action(
                action,
                edited_answer=edited_answer,
                missing_fields=missing_fields,
            ),
            action_id=action_id,
        )

    def prepare_human_action(
        self,
        action: str,
        edited_answer: str = "",
        missing_fields: Optional[List[str]] = None,
    ) -> PreparedHumanAction:
        if not isinstance(action, str) or action not in _HUMAN_COMMAND_TYPES:
            raise ValueError("human action 非法")
        safe_edited_answer = ""
        if action == "edit_send":
            sanitized_answer = _validated_ingress(
                self.ingress_guard.sanitize(edited_answer)
            )
            safe_edited_answer = _safe_text(
                sanitized_answer.sanitized_input, "edited_answer", 2_000
            )
        elif edited_answer != "":
            raise ValueError("该 human action 不接受 edited_answer")

        safe_missing_fields: tuple[str, ...] = ()
        if action == "ask_user":
            fields = _string_list(missing_fields, "missing_fields", 16)
            if not fields or any(field not in _MISSING_FIELDS for field in fields):
                raise ValueError("missing_fields 非法")
            safe_missing_fields = tuple(fields)
        elif missing_fields not in (None, []):
            raise ValueError("该 human action 不接受 missing_fields")
        return PreparedHumanAction._issue(
            action=action,
            edited_answer=safe_edited_answer,
            missing_fields=safe_missing_fields,
            issuer=self._prepared_issuer,
            signature=self._sign_prepared_payload(
                "human_action",
                {
                    "action": action,
                    "edited_answer": safe_edited_answer,
                    "missing_fields": list(safe_missing_fields),
                },
            ),
        )

    def _validated_prepared_human_action(
        self, value: object
    ) -> PreparedHumanAction:
        if type(value) is not PreparedHumanAction:
            raise TypeError("prepared human action 类型非法")
        if getattr(value, "_issuer", None) is not self._prepared_issuer:
            raise ValueError("prepared human action 签发者非法")
        try:
            expected_signature = self._sign_prepared_payload(
                "human_action", self._prepared_human_fields(value)
            )
        except Exception:
            raise ValueError("prepared human action 签名载荷非法") from None
        signature = getattr(value, "signature", None)
        if not isinstance(signature, bytes) or not hmac.compare_digest(
            signature, expected_signature
        ):
            raise ValueError("prepared human action 签名非法")
        if not isinstance(value.action, str) or value.action not in _HUMAN_COMMAND_TYPES:
            raise ValueError("prepared human action 非法")
        if not isinstance(value.edited_answer, str):
            raise TypeError("prepared edited_answer 类型非法")
        if not isinstance(value.missing_fields, tuple):
            raise TypeError("prepared missing_fields 类型非法")
        if value.action == "edit_send":
            safe_answer = _safe_text(
                value.edited_answer, "edited_answer", 2_000
            )
            if safe_answer != value.edited_answer:
                raise ValueError("prepared edited_answer 非规范化")
            if value.missing_fields:
                raise ValueError("edit_send 不接受 missing_fields")
        elif value.edited_answer:
            raise ValueError("该 prepared action 不接受 edited_answer")
        if value.action == "ask_user":
            fields = _string_list(
                list(value.missing_fields), "missing_fields", 16
            )
            if not fields or any(field not in _MISSING_FIELDS for field in fields):
                raise ValueError("prepared missing_fields 非法")
            if tuple(fields) != value.missing_fields:
                raise ValueError("prepared missing_fields 非规范化")
        elif value.missing_fields:
            raise ValueError("该 prepared action 不接受 missing_fields")
        return value

    def human_action_prepared(
        self,
        ticket_id: str,
        prepared: PreparedHumanAction,
        action_id: Optional[str] = None,
    ) -> OrchestrationResult:
        prepared = self._validated_prepared_human_action(prepared)
        action = prepared.action
        ticket_id = _identifier(ticket_id, "ticket_id")
        action_id = uuid.uuid4().hex if action_id is None else action_id
        action_id = _identifier(action_id, "action_id", 120)
        resume_payload = {"command_id": f"action:{action_id}", "action": action}
        expected_final_answer = None

        if action == "approve":
            expected_final_answer = self._approved_checkpoint_answer(ticket_id)
        elif action == "edit_send":
            resume_payload["edited_answer"] = prepared.edited_answer
            expected_final_answer = prepared.edited_answer
        elif action == "ask_user":
            resume_payload["missing_fields"] = list(prepared.missing_fields)

        command_id = _identifier(resume_payload["command_id"], "command_id")
        fingerprint = _payload_fingerprint(
            {
                "kind": "human_action",
                "ticket_id": ticket_id,
                "payload": resume_payload,
            }
        )
        decision = self.repository.begin_command(
            ticket_id,
            command_id,
            _HUMAN_COMMAND_TYPES[action].value,
            self.lease_seconds,
            payload_fingerprint=fingerprint,
            required_status=Status.ESCALATED.value,
        )
        duplicate = self._existing_decision_result(
            decision,
            ticket_id=ticket_id,
            sanitized_input="",
            user_notice="",
        )
        if duplicate is not None:
            return duplicate

        self._execute(
            ticket_id=ticket_id,
            command_id=command_id,
            decision=decision,
            graph_input=Command(resume=resume_payload),
            command_type=_HUMAN_COMMAND_TYPES[action].value,
            human_action=action,
            expected_final_answer=expected_final_answer,
        )
        return self._result(ticket_id, "", "")
