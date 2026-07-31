"""Checkpointed, fail-closed support workflow.

The graph deliberately keeps agent nodes as untrusted dependencies.  Every
dependency result is converted into a legal state transition by a small
deterministic wrapper before it is written to a checkpoint.
"""

from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from agent.orchestration.events import make_event
from agent.orchestration.routes import (
    assert_transition,
    route_after_diagnosis,
    route_after_entry,
    route_after_human,
    route_after_review,
    route_after_triage,
)
from agent.orchestration.state import (
    DiagnosisResult,
    EvidenceItem,
    MissingField,
    RiskFlag,
    RiskLevel,
    Status,
    TicketState,
    TriageResult,
    WaitingReason,
)
from agent.security.secrets import contains_unredacted_secret
from agent.security.trusted_sources import TrustedSourcePolicy
from utils.logger_handler import logger


_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}", re.ASCII)
_EXCEPTION_TYPE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)
_MISSING_FIELDS = frozenset(item.value for item in MissingField)
_RISK_FLAGS = frozenset(item.value for item in RiskFlag)
_RISK_LEVELS = frozenset(item.value for item in RiskLevel)
_MANUAL_ACTIONS = frozenset(
    {"device_reset", "bootloader_recovery", "wallet_recovery", "warranty_decision"}
)
_RESUME_ACTIONS = frozenset({"approve", "edit_send", "ask_user", "reject"})
_CONNECTION_CATEGORIES = frozenset(
    {"usb_connection", "mobile_connection", "bluetooth_connection"}
)
_FIRMWARE_QUERY_MARKERS = frozenset({"固件", "升级", "更新", "恢复模式", "bootloader"})
_TRIAGE_FIELDS = frozenset(
    {
        "intent",
        "category",
        "priority",
        "risk_level",
        "risk_flags",
        "clarity",
        "clarification_question",
        "clarification_options",
        "missing_fields",
        "suggested_route",
        "summary",
    }
)
_DIAGNOSIS_FIELDS = frozenset(
    {
        "outcome",
        "diagnosis_summary",
        "recommended_actions",
        "evidence_refs",
        "citations",
        "draft_answer",
        "remaining_unknowns",
        "evidence",
        "tool_errors",
    }
)


def _filter_connection_evidence(
    state: Mapping[str, object], evidence: list[object]
) -> list[object]:
    """Drop firmware-only chunks from a non-firmware connection investigation."""
    if not isinstance(state, Mapping) or not isinstance(evidence, list):
        return evidence
    category = state.get("category")
    query = state.get("sanitized_input")
    if category not in _CONNECTION_CATEGORIES or not isinstance(query, str):
        return evidence
    if any(marker in query.lower() for marker in _FIRMWARE_QUERY_MARKERS):
        return evidence
    filtered: list[object] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            filtered.append(item)
            continue
        title = item.get("source_title")
        evidence_id = item.get("evidence_id")
        if any(
            isinstance(value, str) and "固件升级" in value
            for value in (title, evidence_id)
        ):
            continue
        filtered.append(item)
    return filtered
_REVIEW_FIELDS = frozenset(
    {"review_decision", "review_reasons", "review_issues", "required_changes"}
)
_CRITICAL_FLAGS = frozenset(
    {RiskFlag.SECRET_EXPOSURE.value, RiskFlag.PHISHING.value, RiskFlag.ASSET_LOSS.value}
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
_REVIEW_REASON_PATTERN = re.compile(r"[a-z_]{1,64}", re.ASCII)
_TOOL_ERROR_VALUES = frozenset(
    {
        "no_evidence",
        "tool_failure:knowledge_search",
        "tool_failure:profile",
        "tool_failure:warranty",
        "tool_failure:chain_status",
    }
)
_RISK_ORDER = {
    RiskLevel.LOW.value: 0,
    RiskLevel.MEDIUM.value: 1,
    RiskLevel.HIGH.value: 2,
    RiskLevel.CRITICAL.value: 3,
}


def _forbidden_control(value: str) -> bool:
    return any(
        (ord(character) < 0x20 and character not in {"\n", "\t"})
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    )


def _safe_text(value: object, field: str, max_length: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError(f"{field} 非法")
    if (not empty and not value.strip()) or _forbidden_control(value):
        raise ValueError(f"{field} 非法")
    normalized = " ".join(value.split())
    if (not empty and not normalized) or contains_unredacted_secret(normalized):
        raise ValueError(f"{field} 非法")
    return normalized


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} 非法")
    if contains_unredacted_secret(value):
        raise ValueError(f"{field} 非法")
    return value


def _status(value: object) -> Status:
    if not isinstance(value, str):
        raise ValueError("status 非法")
    try:
        return Status(value)
    except ValueError:
        raise ValueError("status 非法") from None


def _node_callable(node: object, name: str) -> Callable[[dict], object]:
    if callable(node):
        return node
    method = getattr(node, "run", None)
    if callable(method):
        return method
    raise TypeError(f"{name} 必须可调用或提供 run")


def _checkpointer(checkpointer: object) -> object:
    if checkpointer is None:
        raise TypeError("checkpointer 不能为空")
    if not callable(getattr(checkpointer, "get_tuple", None)) or not callable(
        getattr(checkpointer, "put", None)
    ):
        raise TypeError("checkpointer 接口非法")
    return checkpointer


def _policy_interface(policy_guard: object) -> object:
    if not callable(policy_guard) and not callable(getattr(policy_guard, "evaluate", None)):
        raise TypeError("policy_guard 接口非法")
    return policy_guard


def _citation_urls(citations: object) -> list[str]:
    if not isinstance(citations, list) or len(citations) > 16:
        raise ValueError("citations 非法")
    urls: list[str] = []
    for item in citations:
        if not isinstance(item, Mapping):
            raise ValueError("citations 非法")
        url = item.get("source_url")
        if not isinstance(url, str) or not url.strip() or len(url) > 2_000:
            raise ValueError("citations 非法")
        urls.append(url)
    return urls


def _policy_passes(policy_guard: object, text: object, citations: object) -> bool:
    try:
        safe_text = _safe_text(text, "answer", 2_000)
        urls = _citation_urls(citations)
        evaluate = getattr(policy_guard, "evaluate", None)
        decision = evaluate(safe_text, urls) if callable(evaluate) else policy_guard(safe_text, copy.deepcopy(citations))
        if isinstance(decision, bool):
            return decision
        passed = getattr(decision, "passed", None)
        reasons = getattr(decision, "reason_codes", None)
        return isinstance(passed, bool) and isinstance(reasons, list) and passed and not reasons
    except Exception:
        return False


def with_event(
    state: Mapping[str, object],
    update: Mapping[str, object],
    *,
    node_name: str,
    event_type: str,
    summary: str,
    command_id: str | None = None,
    step_index: int | None = None,
) -> dict:
    """Attach exactly one validated event to an update.

    Ordinary graph steps increment the current command's step.  Resume commands
    must supply a new command id and explicitly restart at step one.
    """

    if not isinstance(state, Mapping) or not isinstance(update, Mapping):
        raise TypeError("state/update 必须是映射")
    current_command = _identifier(state.get("command_id"), "command_id")
    current_step = state.get("event_step")
    if isinstance(current_step, bool) or not isinstance(current_step, int) or current_step < 0:
        raise ValueError("event_step 非法")
    if command_id is None:
        if step_index is not None:
            raise ValueError("普通事件不得覆盖 step_index")
        event_command = current_command
        event_step = current_step + 1
    else:
        event_command = _identifier(command_id, "command_id")
        if event_command == current_command or step_index != 1:
            raise ValueError("恢复命令必须使用新 command_id 并从 step 1 开始")
        event_step = 1

    current_status = _status(state.get("status"))
    target_status = _status(update.get("status", current_status.value))
    event = make_event(
        event_command,
        event_step,
        _safe_text(node_name, "node_name", 64),
        _safe_text(event_type, "event_type", 64),
        _safe_text(summary, "summary", 300),
        current_status.value,
        target_status.value,
    )
    result = dict(update)
    result["command_id"] = event_command
    result["event_step"] = event_step
    result["status_events"] = [event]
    return result


def _reject_unsafe_dependency_value(value: object, *, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("节点输出嵌套过深")
    if isinstance(value, str):
        if (
            len(value) > 32 * 1024
            or _forbidden_control(value)
            or contains_unredacted_secret(value)
        ):
            raise ValueError("节点输出包含未脱敏内容")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("节点输出包含非有限数")
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("节点输出 key 非法")
            _reject_unsafe_dependency_value(key, depth=depth + 1)
            _reject_unsafe_dependency_value(nested, depth=depth + 1)
        return
    if isinstance(value, list):
        for nested in value:
            _reject_unsafe_dependency_value(nested, depth=depth + 1)
        return
    raise ValueError("节点输出必须是严格 JSON 值")


def _dependency_update(raw: object, expected_fields: frozenset[str]) -> dict:
    if not isinstance(raw, dict):
        raise TypeError("节点输出必须是对象")
    if set(raw) != expected_fields:
        raise ValueError("节点输出字段非法")
    _reject_unsafe_dependency_value(raw)
    return copy.deepcopy(raw)


def _validated_triage_update(raw: object) -> dict:
    candidate = _dependency_update(raw, _TRIAGE_FIELDS)
    result = TriageResult.model_validate(candidate).model_dump(mode="json")
    result["summary"] = _safe_text(result["summary"], "summary", 300)
    return result


def _validated_diagnosis_update(raw: object, trusted_sources: object | None) -> dict:
    candidate = _dependency_update(raw, _DIAGNOSIS_FIELDS)
    core_fields = _DIAGNOSIS_FIELDS - {"evidence", "tool_errors"}
    result = DiagnosisResult.model_validate(
        {field: candidate[field] for field in core_fields}
    )
    evidence_raw = candidate["evidence"]
    if not isinstance(evidence_raw, list) or len(evidence_raw) > 16:
        raise ValueError("evidence 非法")
    evidence: list[dict] = []
    total_content = 0
    for raw_item in evidence_raw:
        item = EvidenceItem.model_validate(raw_item)
        content = _safe_text(item.content, "evidence.content", 32 * 1024)
        total_content += len(content)
        if total_content > 32 * 1024:
            raise ValueError("evidence content 总量超限")
        title = _safe_text(item.source_title, "evidence.source_title", 500)
        url = item.source_url
        if url is not None:
            url = _safe_text(url, "evidence.source_url", 2_000)
            if not TrustedSourcePolicy.is_safe_https_url(url):
                raise ValueError("evidence.source_url 非法")
            is_trusted = getattr(trusted_sources, "is_trusted", None)
            if callable(is_trusted) and not is_trusted(url):
                raise ValueError("evidence.source_url 来源不可信")
        evidence.append(
            item.model_copy(
                update={"content": content, "source_title": title, "source_url": url}
            ).model_dump(mode="json")
        )
    evidence_by_id = {item["evidence_id"]: item for item in evidence}
    if len(evidence_by_id) != len(evidence):
        raise ValueError("evidence_id 不得重复")
    if not set(result.evidence_refs).issubset(evidence_by_id):
        raise ValueError("evidence_refs 包含未知证据")
    for citation in result.citations:
        source = evidence_by_id.get(citation.source_id)
        if (
            source is None
            or citation.source_title != source["source_title"]
            or citation.source_url != source["source_url"]
        ):
            raise ValueError("citation 与证据不匹配")

    errors = candidate["tool_errors"]
    if (
        not isinstance(errors, list)
        or len(errors) > 4
        or any(not isinstance(item, str) or item not in _TOOL_ERROR_VALUES for item in errors)
        or len(errors) != len(set(errors))
    ):
        raise ValueError("tool_errors 非法")
    output = result.model_dump(mode="json")
    output["evidence"] = evidence
    output["tool_errors"] = list(errors)
    return output


def _validated_review_update(raw: object) -> dict:
    candidate = _dependency_update(raw, _REVIEW_FIELDS)
    decision = candidate["review_decision"]
    if decision not in {"approve", "revise", "escalate"}:
        raise ValueError("review_decision 非法")

    def items(field: str, maximum: int, length: int) -> list[str]:
        values = candidate[field]
        if not isinstance(values, list) or len(values) > maximum:
            raise ValueError(f"{field} 非法")
        normalized = [_safe_text(value, field, length) for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"{field} 不得重复")
        return normalized

    reasons = items("review_reasons", 9, 64)
    if any(_REVIEW_REASON_PATTERN.fullmatch(reason) is None for reason in reasons):
        raise ValueError("review_reasons 非法")
    issues = items("review_issues", 6, 300)
    changes = items("required_changes", 6, 300)
    if decision == "approve" and (
        reasons != ["passed"] or issues or changes
    ):
        raise ValueError("approve 审核输出非法")
    if decision == "revise" and (
        not reasons or "passed" in reasons or not issues or not changes
    ):
        raise ValueError("revise 审核输出非法")
    if decision == "escalate" and (not reasons or "passed" in reasons):
        raise ValueError("escalate 审核输出非法")
    return {
        "review_decision": decision,
        "review_reasons": reasons,
        "review_issues": issues,
        "required_changes": changes,
    }


def _fail_closed(state: Mapping[str, object], node_name: str, code: str) -> dict:
    current = _status(state.get("status"))
    if current != Status.ESCALATED:
        assert_transition(current.value, Status.ESCALATED.value)
    return with_event(
        state,
        {
            "status": Status.ESCALATED.value,
            "waiting_reason": None,
            "requires_human": True,
            "draft_answer": "",
            "final_answer": "",
            "last_error": code,
            "manual_gate_reason": code,
        },
        node_name=node_name,
        event_type="node_failure",
        summary="节点执行失败，已安全转人工",
    )


def _log_node_failure(
    state: Mapping[str, object],
    node_name: str,
    error_code: str,
    error: BaseException,
) -> None:
    """Log only stable orchestration metadata, never dependency error text."""

    ticket_id = state.get("ticket_id")
    if not isinstance(ticket_id, str) or _ID_PATTERN.fullmatch(ticket_id) is None:
        ticket_id = "invalid_ticket_id"
    try:
        exception_type = getattr(type(error), "__name__", None)
    except Exception:
        exception_type = None
    if (
        not isinstance(exception_type, str)
        or _EXCEPTION_TYPE_PATTERN.fullmatch(exception_type) is None
        or contains_unredacted_secret(exception_type)
    ):
        exception_type = "Exception"
    logger.error(
        "[orchestration] error_code=%s ticket_id=%s node_name=%s exception_type=%s",
        error_code,
        ticket_id,
        node_name,
        exception_type,
    )


def _entry(state: TicketState) -> dict:
    _identifier(state.get("ticket_id"), "ticket_id")
    _identifier(state.get("command_id"), "command_id")
    event_step = state.get("event_step")
    if isinstance(event_step, bool) or not isinstance(event_step, int) or event_step < 0:
        raise ValueError("event_step 非法")
    current = _status(state.get("status"))
    risk = state.get("risk_level")
    requires_human = state.get("requires_human", False)
    if risk not in _RISK_LEVELS or not isinstance(requires_human, bool):
        raise ValueError("入口安全字段非法")
    if current not in {Status.NEW, Status.PENDING_USER, Status.ESCALATED}:
        raise ValueError("入口状态非法")
    if current != Status.ESCALATED and (
        risk in {RiskLevel.HIGH.value, RiskLevel.CRITICAL.value} or requires_human
    ):
        assert_transition(current.value, Status.ESCALATED.value)
        return with_event(
            state,
            {
                "status": Status.ESCALATED.value,
                "waiting_reason": None,
                "requires_human": True,
            },
            node_name="entry",
            event_type="risk_gate",
            summary="风险门禁要求人工处理",
        )
    return with_event(
        state,
        {},
        node_name="entry",
        event_type="entry_checked",
        summary="入口状态与风险字段已校验",
    )


def _triage_node(dependency: Callable[[dict], object]) -> Callable[[TicketState], dict]:
    def run(state: TicketState) -> dict:
        try:
            current = _status(state.get("status"))
            raw = _validated_triage_update(
                dependency(copy.deepcopy(dict(state)))
            )
            ingress_risk = state.get("risk_level")
            ingress_flags = state.get("risk_flags", [])
            if (
                ingress_risk not in _RISK_ORDER
                or not isinstance(ingress_flags, list)
                or any(
                    not isinstance(item, str) or item not in _RISK_FLAGS
                    for item in ingress_flags
                )
                or len(ingress_flags) != len(set(ingress_flags))
                or _RISK_ORDER[raw["risk_level"]] < _RISK_ORDER[ingress_risk]
                or not set(ingress_flags).issubset(raw["risk_flags"])
            ):
                raise ValueError("分诊不得降低入口风险")
            target = Status.TRIAGED
            assert_transition(current.value, target.value)
            raw["status"] = target.value
            raw["waiting_reason"] = None
            route_after_triage({**dict(state), **raw})
            return with_event(
                state,
                raw,
                node_name="triage",
                event_type="triage_completed",
                summary="分诊已完成",
            )
        except Exception as error:
            error_code = (
                "TRIAGE_TIMEOUT"
                if isinstance(error, TimeoutError)
                else "TRIAGE_FAILURE"
            )
            _log_node_failure(state, "triage", error_code, error)
            return _fail_closed(state, "triage", error_code)

    return run


def _start_diagnosis(state: TicketState) -> dict:
    assert_transition(state.get("status"), Status.DIAGNOSING.value)
    return with_event(
        state,
        {"status": Status.DIAGNOSING.value, "waiting_reason": None},
        node_name="start_diagnosis",
        event_type="diagnosis_started",
        summary="诊断已开始",
    )


def _diagnosis_node(
    dependency: Callable[[dict], object],
    manual_actions: frozenset[str],
    trusted_sources: object | None,
) -> Callable[[TicketState], dict]:
    def run(state: TicketState) -> dict:
        try:
            current = _status(state.get("status"))
            if current != Status.DIAGNOSING:
                raise ValueError("诊断入口状态非法")
            raw = _validated_diagnosis_update(
                dependency(copy.deepcopy(dict(state))), trusted_sources
            )
            tool_errors = raw.get("tool_errors", [])
            if "no_evidence" in tool_errors:
                error_code = "DIAGNOSIS_NO_EVIDENCE"
            elif tool_errors:
                error_code = "TOOL_FAILURE"
            else:
                error_code = ""
            if error_code:
                _log_node_failure(
                    state, "diagnosis", error_code, RuntimeError(error_code)
                )
                failed = _fail_closed(state, "diagnosis", error_code)
                failed["tool_errors"] = list(tool_errors)
                return failed
            outcome = raw.get("outcome")
            outcomes = {
                "draft": Status.REVIEWING,
                "need_user": Status.PENDING_USER,
                "escalate": Status.ESCALATED,
            }
            if outcome not in outcomes:
                raise ValueError("诊断 outcome 非法")
            target = outcomes[outcome]
            if outcome == "need_user" and raw.get("draft_answer"):
                target = Status.REVIEWING
            actions = raw.get("recommended_actions", [])
            if not isinstance(actions, list):
                raise ValueError("recommended_actions 非法")
            action_codes = {
                item.get("action_code")
                for item in actions
                if isinstance(item, Mapping) and isinstance(item.get("action_code"), str)
            }
            gated = sorted(action_codes & manual_actions)
            if gated:
                target = Status.ESCALATED
                raw["requires_human"] = True
                raw["manual_gate_reason"] = ",".join(gated)
            raw["status"] = target.value
            raw["waiting_reason"] = (
                WaitingReason.MISSING_INFORMATION.value
                if target == Status.PENDING_USER
                else None
            )
            if target == Status.ESCALATED:
                raw["requires_human"] = True
                if outcome == "escalate":
                    raw["draft_answer"] = ""
            assert_transition(current.value, target.value)
            route_after_diagnosis({**dict(state), **raw})
            return with_event(
                state,
                raw,
                node_name="diagnosis",
                event_type="diagnosis_completed",
                summary="诊断已完成",
            )
        except Exception as error:
            _log_node_failure(
                state, "diagnosis", "DIAGNOSIS_FAILURE", error
            )
            return _fail_closed(state, "diagnosis", "DIAGNOSIS_FAILURE")

    return run


def _pending_user(state: TicketState) -> dict:
    current = _status(state.get("status"))
    if current != Status.PENDING_USER:
        assert_transition(current.value, Status.PENDING_USER.value)
    return with_event(
        state,
        {
            "status": Status.PENDING_USER.value,
            "waiting_reason": WaitingReason.CLARIFICATION.value,
        },
        node_name="pending_user",
        event_type="user_information_required",
        summary="等待用户补充必要信息",
    )


def _review_node(dependency: Callable[[dict], object]) -> Callable[[TicketState], dict]:
    def run(state: TicketState) -> dict:
        try:
            if _status(state.get("status")) != Status.REVIEWING:
                raise ValueError("审核入口状态非法")
            raw = _validated_review_update(
                dependency(copy.deepcopy(dict(state)))
            )
            decision = raw.get("review_decision")
            if decision not in {"approve", "revise", "escalate"}:
                raise ValueError("审核决定非法")
            if decision == "escalate":
                raw["manual_gate_reason"] = ",".join(
                    raw.get("review_reasons") or ["review_escalated"]
                )[:500]
            route_after_review({**dict(state), **raw})
            return with_event(
                state,
                raw,
                node_name="review",
                event_type="review_completed",
                summary="独立审核已完成",
            )
        except Exception as error:
            _log_node_failure(state, "review", "REVIEW_FAILURE", error)
            failed = _fail_closed(state, "review", "REVIEW_FAILURE")
            failed.update(
                {
                    "review_decision": "escalate",
                    "review_reasons": ["review_failure"],
                    "review_issues": ["独立审核执行失败"],
                    "required_changes": [],
                }
            )
            return failed

    return run


def _revision(state: TicketState) -> dict:
    count = state.get("revision_count", 0)
    if isinstance(count, bool) or not isinstance(count, int) or count != 0:
        return _fail_closed(state, "revision", "REVISION_LIMIT")
    assert_transition(state.get("status"), Status.DIAGNOSING.value)
    return with_event(
        state,
        {"status": Status.DIAGNOSING.value, "revision_count": 1},
        node_name="revision",
        event_type="revision_requested",
        summary="审核要求一次受限返工",
    )


def _escalate(state: TicketState) -> dict:
    current = _status(state.get("status"))
    if current != Status.ESCALATED:
        assert_transition(current.value, Status.ESCALATED.value)
    return with_event(
        state,
        {
            "status": Status.ESCALATED.value,
            "waiting_reason": None,
            "requires_human": True,
            "final_answer": "",
        },
        node_name="escalate",
        event_type="escalated",
        summary="工单已升级人工处理",
    )


def _finalize_node(policy_guard: object) -> Callable[[TicketState], dict]:
    def run(state: TicketState) -> dict:
        if _status(state.get("status")) != Status.REVIEWING or not _policy_passes(
            policy_guard, state.get("draft_answer"), state.get("citations", [])
        ):
            return _fail_closed(state, "finalize", "FINAL_POLICY_FAILURE")
        waiting_for_information = (
            state.get("outcome") == "need_user"
            and bool(state.get("remaining_unknowns"))
        )
        target = Status.PENDING_USER
        assert_transition(Status.REVIEWING.value, target.value)
        version = state.get("response_version", 0)
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            return _fail_closed(state, "finalize", "FINALIZE_FAILURE")
        return with_event(
            state,
            {
                "status": target.value,
                "waiting_reason": (
                    WaitingReason.MISSING_INFORMATION.value
                    if waiting_for_information
                    else WaitingReason.RESOLUTION_CONFIRMATION.value
                ),
                "resolution_confirmed": False,
                "requires_human": False,
                "final_answer": state["draft_answer"],
                "response_version": version + 1,
            },
            node_name="finalize",
            event_type=(
                "guidance_finalized"
                if waiting_for_information
                else "response_finalized"
            ),
            summary=(
                "基础答复通过策略复检，等待客户补充"
                if waiting_for_information
                else "完整答复已发送，等待客户确认是否解决"
            ),
        )

    return run


def _human_payload(state: Mapping[str, object], invalid: bool) -> dict:
    draft = state.get("draft_answer", "")
    try:
        safe_draft = _safe_text(draft, "draft_answer", 2_000, empty=True)
    except ValueError:
        safe_draft = ""
    reasons = state.get("review_reasons", [])
    if not isinstance(reasons, list):
        reasons = []
    safe_reasons: list[str] = []
    for reason in reasons[:9]:
        try:
            safe_reasons.append(_safe_text(reason, "review_reason", 64))
        except ValueError:
            continue
    return {
        "type": "human_review",
        "ticket_id": _identifier(state.get("ticket_id"), "ticket_id"),
        "draft_answer": safe_draft,
        "review_reasons": safe_reasons,
        "validation_error": invalid,
    }


def _human_resume(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("恢复载荷非法")
    action = value.get("action")
    if action not in _RESUME_ACTIONS:
        raise ValueError("恢复动作非法")
    required = {"command_id", "action"}
    if action == "edit_send":
        required.add("edited_answer")
    elif action == "ask_user":
        required.add("missing_fields")
    if set(value) != required:
        raise ValueError("恢复载荷字段非法")
    result = {"command_id": _identifier(value["command_id"], "command_id"), "action": action}
    if action == "edit_send":
        result["edited_answer"] = _safe_text(value["edited_answer"], "edited_answer", 2_000)
    elif action == "ask_user":
        fields = value["missing_fields"]
        if (
            not isinstance(fields, list)
            or not fields
            or len(fields) > len(_MISSING_FIELDS)
            or any(not isinstance(item, str) or item not in _MISSING_FIELDS for item in fields)
            or len(fields) != len(set(fields))
        ):
            raise ValueError("missing_fields 非法")
        result["missing_fields"] = list(fields)
    return result


def _human_review_node(policy_guard: object) -> Callable[[TicketState], dict]:
    def run(state: TicketState) -> dict:
        if _status(state.get("status")) != Status.ESCALATED:
            raise ValueError("人工审核入口状态非法")
        raw_resume = interrupt(
            _human_payload(
                state,
                state.get("last_error")
                in {"INVALID_HUMAN_RESUME", "HUMAN_POLICY_REJECTED"},
            )
        )
        try:
            resume = _human_resume(raw_resume)
        except ValueError:
            return with_event(
                state,
                {"last_error": "INVALID_HUMAN_RESUME"},
                node_name="human_review",
                event_type="resume_rejected",
                summary="人工恢复命令结构非法，已拒绝",
            )

        action = resume["action"]
        if action in {"approve", "edit_send"}:
            answer = (
                state.get("draft_answer", "")
                if action == "approve"
                else resume["edited_answer"]
            )
            if not _policy_passes(policy_guard, answer, state.get("citations", [])):
                return with_event(
                    state,
                    {"last_error": "HUMAN_POLICY_REJECTED"},
                    node_name="human_review",
                    event_type="policy_rejected",
                    summary="人工答复未通过策略复检，工单保持升级状态",
                    command_id=resume["command_id"],
                    step_index=1,
                )
            assert_transition(Status.ESCALATED.value, Status.RESOLVED.value)
            version = state.get("response_version", 0)
            if isinstance(version, bool) or not isinstance(version, int) or version < 0:
                return with_event(
                    state,
                    {"last_error": "INVALID_RESPONSE_VERSION"},
                    node_name="human_review",
                    event_type="resume_rejected",
                    summary="响应版本非法，工单保持升级状态",
                    command_id=resume["command_id"],
                    step_index=1,
                )
            return with_event(
                state,
                {
                    "status": Status.RESOLVED.value,
                    "waiting_reason": None,
                    "requires_human": False,
                    "human_decision": action,
                    "final_answer": answer,
                    "response_version": version + 1,
                    "last_error": "",
                },
                node_name="human_review",
                event_type="human_resolved",
                summary="人工审核已完成并结案",
                command_id=resume["command_id"],
                step_index=1,
            )
        if action == "ask_user":
            assert_transition(Status.ESCALATED.value, Status.PENDING_USER.value)
            return with_event(
                state,
                {
                    "status": Status.PENDING_USER.value,
                    "waiting_reason": WaitingReason.MISSING_INFORMATION.value,
                    "requires_human": False,
                    "human_decision": action,
                    "missing_fields": resume["missing_fields"],
                    "last_error": "",
                },
                node_name="human_review",
                event_type="human_requested_information",
                summary="人工审核要求用户补充信息",
                command_id=resume["command_id"],
                step_index=1,
            )
        return with_event(
            state,
            {
                "status": Status.ESCALATED.value,
                "waiting_reason": None,
                "requires_human": True,
                "human_decision": "reject",
                "last_error": "",
            },
            node_name="human_review",
            event_type="human_rejected",
            summary="人工拒绝当前草稿，工单保持升级状态",
            command_id=resume["command_id"],
            step_index=1,
        )

    return run


def _await_user(state: TicketState) -> dict:
    if _status(state.get("status")) != Status.PENDING_USER:
        raise ValueError("等待用户入口状态非法")
    payload = {
        "type": "await_user",
        "ticket_id": _identifier(state.get("ticket_id"), "ticket_id"),
        "missing_fields": list(state.get("missing_fields", [])),
        "clarification_question": state.get("clarification_question", ""),
        "clarification_options": list(state.get("clarification_options", [])),
        "waiting_reason": state.get("waiting_reason"),
    }
    if state.get("last_error") == "INVALID_USER_RESUME":
        payload["validation_error"] = True
    resumed = interrupt(payload)
    try:
        if (
            isinstance(resumed, dict)
            and set(resumed) == {"command_id", "action"}
            and resumed.get("action") == "confirm_resolved"
        ):
            command_id = _identifier(resumed["command_id"], "command_id")
            if (
                state.get("waiting_reason")
                != WaitingReason.RESOLUTION_CONFIRMATION.value
                or not state.get("final_answer")
                or state.get("review_decision") != "approve"
                or state.get("requires_human", False)
            ):
                raise ValueError("当前工单不可确认结案")
            assert_transition(Status.PENDING_USER.value, Status.RESOLVED.value)
            return with_event(
                state,
                {
                    "status": Status.RESOLVED.value,
                    "waiting_reason": None,
                    "resolution_confirmed": True,
                    "last_error": "",
                },
                node_name="await_user",
                event_type="user_confirmed_resolution",
                summary="客户已确认问题解决，工单结案",
                command_id=command_id,
                step_index=1,
            )
        required = {
            "command_id",
            "request_id",
            "sanitized_input",
            "sensitive_flags",
            "risk_flags",
            "risk_level",
        }
        if not isinstance(resumed, dict) or set(resumed) != required:
            raise ValueError("恢复载荷字段非法")
        command_id = _identifier(resumed["command_id"], "command_id")
        request_id = _identifier(resumed["request_id"], "request_id")
        sanitized_input = _safe_text(resumed["sanitized_input"], "sanitized_input", 10_000)
        old_sensitive_flags = state.get("sensitive_flags", [])
        old_risk_flags = state.get("risk_flags", [])
        new_sensitive_flags = resumed["sensitive_flags"]
        new_risk_flags = resumed["risk_flags"]

        def validated_flags(value: object) -> list[str]:
            if (
                not isinstance(value, list)
                or any(
                    not isinstance(item, str) or item not in _RISK_FLAGS
                    for item in value
                )
                or len(value) != len(set(value))
            ):
                raise ValueError("恢复风险字段非法")
            return list(value)

        old_sensitive_flags = validated_flags(old_sensitive_flags)
        old_risk_flags = validated_flags(old_risk_flags)
        new_sensitive_flags = validated_flags(new_sensitive_flags)
        new_risk_flags = validated_flags(new_risk_flags)
        old_risk_level = state.get("risk_level")
        new_risk_level = resumed["risk_level"]
        if old_risk_level not in _RISK_LEVELS or new_risk_level not in _RISK_LEVELS:
            raise ValueError("恢复风险字段非法")

        sensitive_flags = list(
            dict.fromkeys(old_sensitive_flags + new_sensitive_flags)
        )
        risk_flags = list(dict.fromkeys(old_risk_flags + new_risk_flags))
        all_flags = set(sensitive_flags) | set(risk_flags)
        if all_flags & _CRITICAL_FLAGS:
            flag_floor = RiskLevel.CRITICAL.value
        elif all_flags & _HIGH_FLAGS:
            flag_floor = RiskLevel.HIGH.value
        else:
            flag_floor = RiskLevel.LOW.value
        effective_risk_level = max(
            (old_risk_level, new_risk_level, flag_floor),
            key=_RISK_ORDER.__getitem__,
        )
        return with_event(
            state,
            {
                "request_id": request_id,
                "sanitized_input": sanitized_input,
                "sensitive_flags": sensitive_flags,
                "risk_flags": risk_flags,
                "risk_level": effective_risk_level,
                "clarity": "clear",
                "clarification_question": "",
                "clarification_options": [],
                "missing_fields": [],
                "waiting_reason": None,
                "resolution_confirmed": False,
                "human_decision": "",
                "draft_answer": "",
                "final_answer": "",
                "last_error": "",
            },
            node_name="await_user",
            event_type="user_information_received",
            summary="已收到用户补充的脱敏信息",
            command_id=command_id,
            step_index=1,
        )
    except ValueError:
        return with_event(
            state,
            {"last_error": "INVALID_USER_RESUME"},
            node_name="await_user",
            event_type="resume_rejected",
            summary="用户恢复载荷非法，已拒绝",
        )


def build_support_graph(
    *,
    triage_node: object | None = None,
    diagnosis_node: object | None = None,
    review_node: object | None = None,
    policy_guard: object,
    checkpointer: object,
    manual_gate_actions: Sequence[str] = tuple(_MANUAL_ACTIONS),
    triage: object | None = None,
    diagnosis: object | None = None,
    review: object | None = None,
):
    """Build and compile the support graph with explicit dependencies."""

    if triage_node is not None and triage is not None:
        raise ValueError("triage 依赖不得重复提供")
    if diagnosis_node is not None and diagnosis is not None:
        raise ValueError("diagnosis 依赖不得重复提供")
    if review_node is not None and review is not None:
        raise ValueError("review 依赖不得重复提供")
    triage_call = _node_callable(triage_node if triage_node is not None else triage, "triage")
    diagnosis_call = _node_callable(
        diagnosis_node if diagnosis_node is not None else diagnosis, "diagnosis"
    )
    review_call = _node_callable(review_node if review_node is not None else review, "review")
    guard = _policy_interface(policy_guard)
    trusted_sources = getattr(guard, "trusted_sources", None)
    if trusted_sources is not None and not callable(
        getattr(trusted_sources, "is_trusted", None)
    ):
        raise TypeError("policy_guard.trusted_sources 接口非法")
    saver = _checkpointer(checkpointer)
    if (
        not isinstance(manual_gate_actions, Sequence)
        or isinstance(manual_gate_actions, (str, bytes))
        or not manual_gate_actions
        or any(not isinstance(item, str) or item not in _MANUAL_ACTIONS for item in manual_gate_actions)
        or len(manual_gate_actions) != len(set(manual_gate_actions))
    ):
        raise ValueError("manual_gate_actions 非法")
    manual_actions = frozenset(manual_gate_actions)

    builder = StateGraph(TicketState)
    builder.add_node("entry", _entry)
    builder.add_node("triage", _triage_node(triage_call))
    builder.add_node("pending_user", _pending_user)
    builder.add_node("start_diagnosis", _start_diagnosis)
    builder.add_node(
        "diagnosis",
        _diagnosis_node(diagnosis_call, manual_actions, trusted_sources),
    )
    builder.add_node("review", _review_node(review_call))
    builder.add_node("revision", _revision)
    builder.add_node("escalate", _escalate)
    builder.add_node("finalize", _finalize_node(guard))
    builder.add_node("human_review", _human_review_node(guard))
    builder.add_node("await_user", _await_user)

    builder.set_entry_point("entry")
    builder.add_conditional_edges(
        "entry", route_after_entry, {"triage": "triage", "human_review": "human_review"}
    )
    builder.add_conditional_edges(
        "triage",
        route_after_triage,
        {
            "pending_user": "pending_user",
            "start_diagnosis": "start_diagnosis",
            "escalate": "escalate",
            "human_review": "human_review",
        },
    )
    builder.add_edge("pending_user", "await_user")
    builder.add_edge("start_diagnosis", "diagnosis")
    builder.add_conditional_edges(
        "diagnosis",
        route_after_diagnosis,
        {"await_user": "await_user", "review": "review", "human_review": "human_review"},
    )
    builder.add_conditional_edges(
        "review",
        route_after_review,
        {"finalize": "finalize", "revision": "revision", "escalate": "escalate"},
    )
    builder.add_edge("revision", "diagnosis")
    builder.add_edge("escalate", "human_review")
    builder.add_conditional_edges(
        "finalize",
        lambda state: (
            "end"
            if state.get("status") == Status.RESOLVED.value
            else "await_user"
            if state.get("status") == Status.PENDING_USER.value
            else "human_review"
        ),
        {"end": END, "await_user": "await_user", "human_review": "human_review"},
    )
    builder.add_conditional_edges(
        "human_review",
        route_after_human,
        {"end": END, "await_user": "await_user", "human_review": "human_review"},
    )
    builder.add_conditional_edges(
        "await_user",
        lambda state: "end"
        if state.get("status") == Status.RESOLVED.value
        else "await_user"
        if state.get("last_error") == "INVALID_USER_RESUME"
        else "entry",
        {"end": END, "await_user": "await_user", "entry": "entry"},
    )
    return builder.compile(checkpointer=saver)


class ManagedSqliteSaver(SqliteSaver):
    """A thread-capable SQLite saver with an explicit lifecycle."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(connection)
        self._managed_connection = connection
        self._closed = False

    def close(self) -> None:
        with self.lock:
            if not self._closed:
                self._managed_connection.close()
                self._closed = True

    def __enter__(self) -> "ManagedSqliteSaver":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def sqlite_checkpointer(path: str | Path) -> ManagedSqliteSaver:
    """Create a durable SQLite checkpointer suitable for worker threads."""

    if not isinstance(path, (str, Path)):
        raise TypeError("checkpoint path 非法")
    checkpoint_path = Path(path)
    if not checkpoint_path.name or checkpoint_path.is_dir():
        raise ValueError("checkpoint path 非法")
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    return ManagedSqliteSaver(connection)


def _configured_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} 必须是对象")
    return value


def _configured_positive_number(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field} 必须是正数")
    return float(value)


def _configured_retry(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} 必须是非负整数")
    return value


def build_configured_graph(
    model: object,
    policy: dict,
    orchestration: dict,
    checkpointer: object,
    *,
    rag_service: object | None = None,
    profile_db: object | None = None,
    warranty_repository: object | None = None,
    chain_fetcher: object | None = None,
    trusted_source_policy: object | None = None,
    policy_guard: object | None = None,
):
    """Build the production graph while keeping all four read-only adapters injectable.

    Diagnosis owns the timeout/retry boundary around these direct adapters; an
    adapter must never add a nested worker or invoke another language model.
    """

    from agent.nodes.diagnosis import DiagnosisAgent
    from agent.nodes.review import ReviewAgent
    from agent.nodes.triage import TriageAgent
    from agent.policies.security import PolicyGuard
    from agent.security.trusted_sources import TrustedSourcePolicy

    if not isinstance(policy, dict):
        raise TypeError("policy 必须是对象")
    config = _configured_mapping(orchestration, "orchestration")
    timeouts = _configured_mapping(config.get("timeouts"), "timeouts")
    retries = _configured_mapping(config.get("retries"), "retries")
    required_fields = config.get("required_fields")
    manual_actions = config.get("manual_gate_actions")
    agent_timeout = _configured_positive_number(
        timeouts.get("agent_seconds"), "timeouts.agent_seconds"
    )
    tool_timeout = _configured_positive_number(
        timeouts.get("readonly_tool_seconds"), "timeouts.readonly_tool_seconds"
    )
    triage_retries = _configured_retry(retries.get("triage"), "retries.triage")
    diagnosis_retries = _configured_retry(
        retries.get("diagnosis"), "retries.diagnosis"
    )
    tool_retries = _configured_retry(
        retries.get("readonly_tool"), "retries.readonly_tool"
    )
    review_retries = _configured_retry(retries.get("review"), "retries.review")
    if review_retries != 0:
        raise ValueError("retries.review 必须为 0")
    if not isinstance(required_fields, dict) or not required_fields:
        raise ValueError("required_fields 非法")

    if trusted_source_policy is None:
        if policy_guard is None:
            trusted = TrustedSourcePolicy(
                policy.get("trusted_evidence_domains"),
                policy.get("trusted_evidence_url_prefixes"),
            )
        else:
            trusted = getattr(policy_guard, "trusted_sources", None)
            if not isinstance(trusted, TrustedSourcePolicy):
                raise TypeError("policy_guard 的 trusted_source_policy 非法")
    else:
        if not isinstance(trusted_source_policy, TrustedSourcePolicy):
            raise TypeError("trusted_source_policy 类型非法")
        trusted = trusted_source_policy

    if policy_guard is None:
        guard = PolicyGuard(policy, trusted_source_policy=trusted)
    else:
        if not isinstance(policy_guard, PolicyGuard):
            raise TypeError("policy_guard 类型非法")
        if policy_guard.trusted_sources is not trusted:
            raise ValueError("policy_guard 必须共享 trusted_source_policy")
        guard = policy_guard

    if rag_service is None:
        from rag.rag_service import RagSummarizeService

        rag_service = RagSummarizeService(evidence_only=True)
    if profile_db is None:
        from database.profile_db import ProfileDatabase

        profile_db = ProfileDatabase()
    if warranty_repository is None:
        from agent.tools.warranty_tools import WarrantyRepository

        warranty_repository = WarrantyRepository()
    if chain_fetcher is None:
        from agent.services.chain_status_service import fetch_chain_status

        chain_fetcher = fetch_chain_status

    search_evidence = getattr(rag_service, "search_evidence", None)
    get_profile = getattr(profile_db, "get_profile", None)
    as_evidence = getattr(warranty_repository, "as_evidence", None)
    if not callable(search_evidence):
        raise TypeError("rag_service 接口非法")
    if not callable(get_profile):
        raise TypeError("profile_db 接口非法")
    if not callable(as_evidence):
        raise TypeError("warranty_repository 接口非法")
    if not callable(chain_fetcher):
        raise TypeError("chain_fetcher 接口非法")

    def knowledge_tool(_state: dict, query: str) -> list[object]:
        result = search_evidence(query)
        if not isinstance(result, list):
            raise TypeError("knowledge adapter 输出非法")
        return _filter_connection_evidence(_state, result)

    def profile_tool(state: dict, _query: str) -> list[object]:
        user_id = _identifier(state.get("user_id"), "user_id")
        profile = get_profile(user_id)
        if profile is None:
            return []
        if is_dataclass(profile):
            payload = asdict(profile)
        elif isinstance(profile, Mapping):
            payload = dict(profile)
        else:
            raise TypeError("profile adapter 输出非法")
        content = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return [
            {
                "evidence_id": f"profile:{user_id}",
                "kind": "profile",
                "content": content,
                "source_title": "KeyGuard 模拟用户档案",
                "source_url": None,
            }
        ]

    def warranty_tool(_state: dict, query: str) -> list[object]:
        evidence = as_evidence(query)
        return [] if evidence is None else [evidence]

    def chain_tool(_state: dict, query: str) -> list[object]:
        content = chain_fetcher(query)
        if not isinstance(content, str):
            raise TypeError("chain adapter 输出非法")
        return [
            {
                "evidence_id": f"chain:{query.upper()}:simulated",
                "kind": "chain",
                "content": content,
                "source_title": "KeyGuard 模拟链状态",
                "source_url": None,
            }
        ]

    triage_agent = TriageAgent(
        model=model,
        timeout_seconds=agent_timeout,
        retries=triage_retries,
        required_fields=required_fields,
    )
    diagnosis_agent = DiagnosisAgent(
        model=model,
        tool_registry={
            "knowledge_search": knowledge_tool,
            "profile": profile_tool,
            "warranty": warranty_tool,
            "chain_status": chain_tool,
        },
        timeout_seconds=agent_timeout,
        retries=diagnosis_retries,
        tool_timeout_seconds=tool_timeout,
        tool_retries=tool_retries,
        trusted_source_policy=trusted,
    )
    reviewer = ReviewAgent(
        model=model,
        policy_guard=guard,
        timeout_seconds=agent_timeout,
    )
    return build_support_graph(
        triage_node=triage_agent,
        diagnosis_node=diagnosis_agent,
        review_node=reviewer,
        policy_guard=guard,
        checkpointer=checkpointer,
        manual_gate_actions=manual_actions,
    )
