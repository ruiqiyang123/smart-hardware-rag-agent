"""Safe compatibility helpers for provider structured-output wrappers."""

import copy
from typing import Any

from utils.logger_handler import logger


_WRAPPER_KEYS = frozenset({"raw", "parsed", "parsing_error"})


def _is_wrapper(value: object) -> bool:
    return isinstance(value, dict) and _WRAPPER_KEYS.issubset(value)


def unwrap_structured_output(
    value: object,
    expected_tool_name: str,
    *,
    agent_name: str,
) -> object:
    """Return parsed output or safely recover one expected tool-call mapping.

    Injected runners used by tests and offline evaluation may still return a
    Pydantic object or a bare dictionary. Provider-backed runners return the
    LangChain ``include_raw=True`` wrapper. Recovery is deliberately limited to
    exactly one tool call with the expected schema name and mapping arguments.
    """

    if not isinstance(expected_tool_name, str) or not expected_tool_name:
        raise ValueError("expected_tool_name 非法")
    if not isinstance(agent_name, str) or not agent_name:
        raise ValueError("agent_name 非法")
    if not _is_wrapper(value):
        return value

    wrapper = value
    parsed = wrapper.get("parsed")
    if parsed is not None:
        return parsed

    parsing_error = wrapper.get("parsing_error")
    raw = wrapper.get("raw")
    tool_calls = getattr(raw, "tool_calls", None)
    if not isinstance(tool_calls, list) or len(tool_calls) != 1:
        logger.warning(
            "[structured_output] agent=%s stage=raw_extract error_type=%s",
            agent_name,
            type(parsing_error).__name__,
        )
        raise ValueError("结构化输出必须包含唯一工具调用")

    tool_call = tool_calls[0]
    if not isinstance(tool_call, dict):
        raise TypeError("结构化工具调用类型非法")
    if tool_call.get("name") != expected_tool_name:
        raise ValueError("结构化工具名称不匹配")
    arguments: Any = tool_call.get("args")
    if not isinstance(arguments, dict):
        raise TypeError("结构化工具参数必须是对象")

    logger.info(
        "[structured_output] agent=%s stage=raw_extract recovered=true "
        "error_type=%s",
        agent_name,
        type(parsing_error).__name__,
    )
    return copy.deepcopy(arguments)


def log_normalization(agent_name: str, rule_id: str) -> None:
    """Record a non-sensitive, allowlisted compatibility rule application."""

    logger.info(
        "[structured_output] agent=%s stage=normalize rule=%s",
        agent_name,
        rule_id,
    )


def log_structured_failure(
    agent_name: str,
    stage: str,
    error: Exception,
) -> None:
    """Log only the non-sensitive location and exception class."""

    logger.warning(
        "[structured_output] agent=%s stage=%s error_type=%s",
        agent_name,
        stage,
        type(error).__name__,
    )


def invoke_structured_runner(
    runner: object,
    messages: object,
    *,
    agent_name: str,
) -> object:
    """Invoke one structured runner with provider-boundary observability."""

    invoke = getattr(runner, "invoke", None)
    if not callable(invoke):
        raise TypeError("runner 必须提供 invoke")
    try:
        return invoke(messages)
    except Exception as error:
        log_structured_failure(agent_name, "provider_call", error)
        raise
