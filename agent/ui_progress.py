"""Secret-free projections used by the Streamlit progress surfaces.

The runtime event store contains more information than a customer or an
operator needs to see.  This module keeps the UI boundary pure and small so
that it can be tested without importing Streamlit or a live model.
"""

from __future__ import annotations

from typing import Iterable


CUSTOMER_PHASE_LABELS = {
    "entry_checked": "🛡️ 安全检查完成",
    "triage_completed": "🧭 问题分诊完成",
    "diagnosis_completed": "📚 诊断取证完成",
    "review_completed": "✅ 安全复核完成",
    "guidance_finalized": "💬 基础建议已生成，等待补充",
    "escalated": "👩‍💼 已转人工审核",
    "response_finalized": "📨 安全回复已完成",
    "ticket.closed": "⏱️ 会话已结束",
}

_WORKBENCH_EVENT_TYPES = frozenset(
    {
        *CUSTOMER_PHASE_LABELS,
        "diagnosis_started",
        "revision_requested",
        "node_failure",
        "user_information_required",
        "user_information_received",
        "risk_gate",
        "policy_rejected",
        "resume_rejected",
        "human_resolved",
        "human_requested_information",
        "human_rejected",
        "ticket.escalated",
        "ticket.pending_user",
        "ticket.resolved",
    }
)
_MAX_SUMMARY_LENGTH = 240
_MAX_NODE_LENGTH = 80


def _bounded_text(value: object, *, default: str, maximum: int) -> str:
    if not isinstance(value, str):
        return default
    normalized = " ".join(value.split())
    if not normalized:
        return default
    return normalized[:maximum]


def project_customer_phases(events: Iterable[dict]) -> list[dict[str, str]]:
    """Return only the short, customer-safe completed phase list."""
    projected: list[dict[str, str]] = []
    seen: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        label = CUSTOMER_PHASE_LABELS.get(event_type)
        if label is None or event_type in seen:
            continue
        seen.add(event_type)
        projected.append({"event_type": event_type, "label": label})
    return projected


def project_workbench_events(events: Iterable[dict]) -> list[dict[str, str]]:
    """Project an allow-listed audit trace without metadata or model content."""
    projected: list[dict[str, str]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        if event_type not in _WORKBENCH_EVENT_TYPES:
            continue
        projected.append(
            {
                "event_type": event_type,
                "label": CUSTOMER_PHASE_LABELS.get(
                    event_type,
                    "📌 工作流结果已提交"
                    if event_type.startswith("ticket.")
                    else event_type,
                ),
                "node_name": _bounded_text(
                    event.get("node_name"), default="-", maximum=_MAX_NODE_LENGTH
                ),
                "summary": _bounded_text(
                    event.get("summary"), default="-", maximum=_MAX_SUMMARY_LENGTH
                ),
                "from_status": _bounded_text(
                    event.get("from_status"), default="-", maximum=32
                ),
                "to_status": _bounded_text(
                    event.get("to_status"), default="-", maximum=32
                ),
            }
        )
    return projected


def _validate_messages(messages: list[dict]) -> None:
    if not isinstance(messages, list):
        raise TypeError("messages 必须是 list")
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message.get("role") not in {"user", "assistant"}
            or not isinstance(message.get("content"), str)
        ):
            raise ValueError("messages 必须是安全的 role/content 对象")


def append_processing_turn(
    messages: list[dict], sanitized_input: str, processing_text: str
) -> None:
    """Persist the user turn and a visible assistant placeholder immediately."""
    _validate_messages(messages)
    if not isinstance(sanitized_input, str) or not sanitized_input.strip():
        raise ValueError("sanitized_input 非法")
    if not isinstance(processing_text, str) or not processing_text.strip():
        raise ValueError("processing_text 非法")
    messages.extend(
        [
            {"role": "user", "content": sanitized_input},
            {"role": "assistant", "content": processing_text},
        ]
    )


def replace_processing_answer(messages: list[dict], answer: str) -> None:
    """Replace the most recent assistant placeholder after the command returns."""
    _validate_messages(messages)
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("answer 非法")
    for message in reversed(messages):
        if message["role"] == "assistant":
            message["content"] = answer
            return
    raise ValueError("找不到 assistant 占位消息")
