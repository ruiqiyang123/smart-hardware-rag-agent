"""Validated event construction at the orchestration/persistence boundary."""

import math
from typing import Dict, Mapping, Optional, Set, Tuple, cast

from typing_extensions import TypedDict

from agent.orchestration.state import JSONValue, Status, StatusEventState
from agent.security.secrets import (
    TransactionHash,
    contains_unredacted_secret,
    is_fully_redacted,
)


class RepositoryEvent(TypedDict):
    step_index: int
    node_name: str
    event_type: str
    from_status: Optional[str]
    to_status: Optional[str]
    summary: str
    metadata: Dict[str, object]


_EVENT_FIELDS = frozenset(
    {
        "command_id",
        "step_index",
        "node_name",
        "event_type",
        "summary",
        "from_status",
        "to_status",
        "metadata",
    }
)
_SENSITIVE_METADATA_KEYS = frozenset(
    {
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
)
_MAX_JSON_DEPTH = 32


def _nonempty_safe_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    normalized = value.strip()
    if contains_unredacted_secret(normalized):
        raise ValueError("事件包含未脱敏的敏感信息")
    return normalized


def _optional_status(value: object, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} 非法")
    try:
        return Status(value).value
    except ValueError as error:
        raise ValueError(f"{field} 非法") from error


def _copy_json_value(
    value: object,
    path: Tuple[str, ...],
    active_containers: Set[int],
    depth: int,
    allow_serialized_transaction_hash: bool,
) -> JSONValue:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("metadata 嵌套过深")
    if isinstance(value, TransactionHash):
        if allow_serialized_transaction_hash or path != ("transaction_hash",):
            raise ValueError(
                "TransactionHash 只能在事件入口的顶层 transaction_hash 使用"
            )
        return value.value
    if isinstance(value, str):
        if path == ("transaction_hash",):
            if not allow_serialized_transaction_hash:
                raise ValueError(
                    "transaction_hash 必须使用显式 TransactionHash 类型"
                )
            try:
                return TransactionHash(value).value
            except ValueError as error:
                raise ValueError("transaction_hash 非法") from error
        if path and path[-1].lower() in _SENSITIVE_METADATA_KEYS:
            if not is_fully_redacted(value):
                raise ValueError("事件包含未脱敏的敏感信息")
        if contains_unredacted_secret(value):
            raise ValueError("事件包含未脱敏的敏感信息")
        return value
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("metadata 只能包含有限 JSON 数值")
        return value
    if isinstance(value, (dict, list)):
        identity = id(value)
        if identity in active_containers:
            raise ValueError("metadata 不得包含循环引用")
        active_containers.add(identity)
        try:
            if isinstance(value, dict):
                copied: Dict[str, JSONValue] = {}
                for key, nested in value.items():
                    if not isinstance(key, str):
                        raise ValueError("metadata 的 key 必须是字符串")
                    if contains_unredacted_secret(key):
                        raise ValueError("事件包含未脱敏的敏感信息")
                    copied[key] = _copy_json_value(
                        nested,
                        path + (key,),
                        active_containers,
                        depth + 1,
                        allow_serialized_transaction_hash,
                    )
                return copied
            return [
                _copy_json_value(
                    nested,
                    path + ("[]",),
                    active_containers,
                    depth + 1,
                    allow_serialized_transaction_hash,
                )
                for nested in value
            ]
        finally:
            active_containers.remove(identity)
    raise ValueError("metadata 只能包含 JSON 值")


def _metadata(
    value: object, allow_serialized_transaction_hash: bool
) -> Dict[str, JSONValue]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata 必须是 dict 或 None")
    copied = _copy_json_value(
        value, (), set(), 0, allow_serialized_transaction_hash
    )
    return cast(Dict[str, JSONValue], copied)


def _build_event(
    command_id: object,
    step_index: object,
    node_name: object,
    event_type: object,
    summary: object,
    from_status: object,
    to_status: object,
    metadata: object,
    allow_serialized_transaction_hash: bool,
) -> StatusEventState:
    if (
        isinstance(step_index, bool)
        or not isinstance(step_index, int)
        or step_index <= 0
    ):
        raise ValueError("step_index 必须是正整数")
    return {
        "command_id": _nonempty_safe_text(command_id, "command_id"),
        "step_index": step_index,
        "node_name": _nonempty_safe_text(node_name, "node_name"),
        "event_type": _nonempty_safe_text(event_type, "event_type"),
        "summary": _nonempty_safe_text(summary, "summary"),
        "from_status": _optional_status(from_status, "from_status"),
        "to_status": _optional_status(to_status, "to_status"),
        "metadata": _metadata(metadata, allow_serialized_transaction_hash),
    }


def make_event(
    command_id: str,
    step_index: int,
    node_name: str,
    event_type: str,
    summary: str,
    from_status: Optional[str],
    to_status: Optional[str],
    metadata: Optional[Dict[str, object]] = None,
) -> StatusEventState:
    """Create a checkpoint-safe event without retaining caller-owned metadata."""

    return _build_event(
        command_id,
        step_index,
        node_name,
        event_type,
        summary,
        from_status,
        to_status,
        metadata,
        allow_serialized_transaction_hash=False,
    )


def to_repository_event(event: Mapping[str, object]) -> RepositoryEvent:
    """Validate a checkpoint event and adapt it to repository-only types.

    This is a trusted internal boundary: callers must supply the output of
    :func:`make_event`, optionally after a JSON checkpoint round trip. The
    serialized top-level transaction hash is restored to ``TransactionHash``
    only after the complete event contract has been revalidated.
    """

    if not isinstance(event, Mapping) or set(event) != _EVENT_FIELDS:
        raise ValueError("event 字段不完整或包含未知字段")
    normalized = _build_event(
        event["command_id"],
        event["step_index"],
        event["node_name"],
        event["event_type"],
        event["summary"],
        event["from_status"],
        event["to_status"],
        event["metadata"],
        allow_serialized_transaction_hash=True,
    )
    repository_metadata: Dict[str, object] = dict(normalized["metadata"])
    if "transaction_hash" in repository_metadata:
        repository_metadata["transaction_hash"] = TransactionHash(
            cast(str, repository_metadata["transaction_hash"])
        )
    return {
        "step_index": normalized["step_index"],
        "node_name": normalized["node_name"],
        "event_type": normalized["event_type"],
        "from_status": normalized["from_status"],
        "to_status": normalized["to_status"],
        "summary": normalized["summary"],
        "metadata": repository_metadata,
    }
