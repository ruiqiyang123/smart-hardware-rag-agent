"""Deterministic schema validation and scoring for the V2 ticket evaluation.

This module deliberately does not call a model.  It scores structured runtime
observations and keeps every metric denominator explicit.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from agent.security.secrets import contains_unredacted_secret


class CaseValidationError(ValueError):
    """The checked-in evaluation case schema is invalid."""


CASE_FIELDS = frozenset(
    {
        "case_id",
        "scope",
        "group",
        "turns",
        "expected_intent",
        "expected_priority",
        "expected_route",
        "expected_final_status",
        "expected_risk_level",
        "expected_missing_fields",
        "expected_retry_count",
        "requires_knowledge_citation",
        "contains_secret",
        "fault",
        "required_behaviors",
        "forbidden_behaviors",
    }
)
TURN_FIELDS = frozenset({"turn_index", "input", "expected_status"})
SCOPES = frozenset({"v2", "v2_non_core"})
GROUPS = frozenset(
    {
        "legacy",
        "high_risk",
        "insufficient_info",
        "multi_turn",
        "warranty",
        "device",
        "fault",
    }
)
INTENTS = frozenset(
    {
        "troubleshoot",
        "recovery",
        "warranty",
        "transaction_boundary",
        "security_incident",
        "security_report",
        "other",
    }
)
PRIORITIES = frozenset({"P0", "P1", "P2"})
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
FINAL_STATUSES = frozenset({"resolved", "pending_user", "escalated"})
MISSING_FIELDS = frozenset(
    {
        "device_model",
        "app_os",
        "connection_type",
        "firmware_version",
        "error_state",
        "serial_last4",
        "purchase_date",
        "transaction_hash",
        "chain_name",
    }
)
FAULTS = frozenset(
    {
        "triage_timeout",
        "rag_empty",
        "warranty_tool_exception",
        "reviewer_validation_error",
    }
)
_EXPECTED_FAULT_RETRIES = {
    "triage_timeout": 1,
    "rag_empty": 0,
    "warranty_tool_exception": 1,
    "reviewer_validation_error": 0,
}
_CASE_ID = re.compile(r"KG-EVAL-\d{3}\Z", re.ASCII)


def _fail(message: str) -> None:
    raise CaseValidationError(message)


def _string(
    value: object,
    field: str,
    *,
    max_length: int = 2_000,
    allow_secret: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        _fail(f"{field} 必须是非空字符串")
    if any(
        (ord(character) < 0x20 and character not in {"\n", "\t"})
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        _fail(f"{field} 包含非法控制字符")
    if not allow_secret and contains_unredacted_secret(value):
        _fail(f"{field} 包含未脱敏敏感信息")
    return value.strip()


def _unique_strings(
    value: object,
    field: str,
    *,
    allowed: frozenset[str] | None = None,
) -> list[str]:
    if not isinstance(value, list):
        _fail(f"{field} 必须是列表")
    normalized = [_string(item, field, max_length=300) for item in value]
    if len(normalized) != len(set(normalized)):
        _fail(f"{field} 不得包含重复值")
    if allowed is not None and not set(normalized).issubset(allowed):
        _fail(f"{field} 包含未知值")
    return normalized


def _validate_turns(value: object, case_id: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        _fail(f"{case_id}.turns 必须是非空列表")
    turns: list[dict[str, object]] = []
    indexes: list[int] = []
    for raw_turn in value:
        if not isinstance(raw_turn, dict) or set(raw_turn) != TURN_FIELDS:
            _fail(f"{case_id}.turns 字段非法")
        index = raw_turn["turn_index"]
        if isinstance(index, bool) or not isinstance(index, int) or index <= 0:
            _fail(f"{case_id}.turn_index 必须是正整数")
        status = raw_turn["expected_status"]
        if status not in FINAL_STATUSES:
            _fail(f"{case_id}.turn.expected_status 非法")
        turns.append(
            {
                "turn_index": index,
                "input": _string(
                    raw_turn["input"],
                    f"{case_id}.turn.input",
                    max_length=10_000,
                    allow_secret=True,
                ),
                "expected_status": status,
            }
        )
        indexes.append(index)
    if len(indexes) != len(set(indexes)) or indexes != list(range(1, len(indexes) + 1)):
        _fail(f"{case_id}.turn_index 必须唯一且从 1 连续递增")
    return turns


def validate_cases(cases: object) -> list[dict[str, object]]:
    """Validate and defensively copy an evaluation case collection.

    The validator accepts custom case files for ``--cases`` but never silently
    drops unknown fields or normalizes duplicate IDs/turns/behaviors.
    """

    if not isinstance(cases, list) or not cases:
        _fail("评测集必须是非空列表")
    validated: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for raw_case in cases:
        if not isinstance(raw_case, dict) or set(raw_case) != CASE_FIELDS:
            _fail("case 字段必须与 V2 schema 完全一致")
        case_id = _string(raw_case["case_id"], "case_id", max_length=32)
        if _CASE_ID.fullmatch(case_id) is None:
            _fail("case_id 格式非法")
        if case_id in seen_ids:
            _fail("case_id 不得重复")
        seen_ids.add(case_id)

        scope = raw_case["scope"]
        group = raw_case["group"]
        intent = raw_case["expected_intent"]
        priority = raw_case["expected_priority"]
        route = raw_case["expected_route"]
        final_status = raw_case["expected_final_status"]
        risk_level = raw_case["expected_risk_level"]
        if scope not in SCOPES:
            _fail(f"{case_id}.scope 非法")
        if group not in GROUPS:
            _fail(f"{case_id}.group 非法")
        if intent not in INTENTS:
            _fail(f"{case_id}.expected_intent 非法")
        if priority not in PRIORITIES:
            _fail(f"{case_id}.expected_priority 非法")
        if route not in FINAL_STATUSES or final_status not in FINAL_STATUSES:
            _fail(f"{case_id}.expected route/status 非法")
        if route != final_status:
            _fail(f"{case_id}.expected_route 必须与 expected_final_status 一致")
        if risk_level not in RISK_LEVELS:
            _fail(f"{case_id}.expected_risk_level 非法")
        if risk_level == "critical" and priority != "P0":
            _fail(f"{case_id}.critical 必须使用 P0")
        if risk_level == "high" and priority not in {"P0", "P1"}:
            _fail(f"{case_id}.high 必须使用 P0/P1")

        turns = _validate_turns(raw_case["turns"], case_id)
        if turns[-1]["expected_status"] != final_status:
            _fail(f"{case_id}.最后一轮状态必须等于 expected_final_status")

        missing_fields = _unique_strings(
            raw_case["expected_missing_fields"],
            f"{case_id}.expected_missing_fields",
            allowed=MISSING_FIELDS,
        )
        retry_count = raw_case["expected_retry_count"]
        if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
            _fail(f"{case_id}.expected_retry_count 非法")
        fault = raw_case["fault"]
        if fault is not None and fault not in FAULTS:
            _fail(f"{case_id}.fault 非法")
        if (group == "fault") != (fault is not None):
            _fail(f"{case_id}.fault 与 group 不一致")
        expected_retries = _EXPECTED_FAULT_RETRIES.get(fault, 0)
        if retry_count != expected_retries:
            _fail(f"{case_id}.expected_retry_count 与 fault 不一致")

        citation_required = raw_case["requires_knowledge_citation"]
        contains_secret = raw_case["contains_secret"]
        if not isinstance(citation_required, bool) or not isinstance(contains_secret, bool):
            _fail(f"{case_id}.布尔标注非法")
        turn_contains_secret = any(
            contains_unredacted_secret(turn["input"]) for turn in turns
        )
        if turn_contains_secret != contains_secret:
            _fail(f"{case_id}.contains_secret 与 turns 输入不一致")
        required = _unique_strings(
            raw_case["required_behaviors"], f"{case_id}.required_behaviors"
        )
        forbidden = _unique_strings(
            raw_case["forbidden_behaviors"], f"{case_id}.forbidden_behaviors"
        )
        if not required or not forbidden:
            _fail(f"{case_id}.required/forbidden_behaviors 不得为空")
        if set(required) & set(forbidden):
            _fail(f"{case_id}.required/forbidden_behaviors 不得重叠")

        validated.append(
            {
                "case_id": case_id,
                "scope": scope,
                "group": group,
                "turns": turns,
                "expected_intent": intent,
                "expected_priority": priority,
                "expected_route": route,
                "expected_final_status": final_status,
                "expected_risk_level": risk_level,
                "expected_missing_fields": missing_fields,
                "expected_retry_count": retry_count,
                "requires_knowledge_citation": citation_required,
                "contains_secret": contains_secret,
                "fault": fault,
                "required_behaviors": required,
                "forbidden_behaviors": forbidden,
            }
        )
    return copy.deepcopy(validated)


def _answer(actual: Mapping[str, object]) -> str:
    value = actual.get("answer", actual.get("final_answer", ""))
    return value if isinstance(value, str) else ""


def _string_values(value: object, *, depth: int = 0) -> list[str]:
    if depth > 16:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        values: list[str] = []
        for item in value.values():
            values.extend(_string_values(item, depth=depth + 1))
        return values
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = []
        for item in value:
            values.extend(_string_values(item, depth=depth + 1))
        return values
    return []


def _valid_citation(citation: object, evidence_refs: set[str] | None) -> bool:
    if not isinstance(citation, Mapping):
        return False
    source_id = citation.get("source_id")
    source_url = citation.get("source_url")
    if not isinstance(source_id, str) or not source_id.strip():
        return False
    if not isinstance(source_url, str) or not source_url.strip():
        return False
    parsed = urlparse(source_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    return evidence_refs is None or source_id in evidence_refs


def score_case(expected: Mapping[str, object], actual: Mapping[str, object]) -> dict[str, Any]:
    """Score one structured observation without fuzzy or model-based judging."""

    if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
        raise TypeError("expected/actual 必须是映射")
    case_id = expected.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("expected.case_id 非法")

    answer = _answer(actual)
    observed = actual.get("observed_behaviors", [])
    observed_behaviors = (
        {item for item in observed if isinstance(item, str)}
        if isinstance(observed, list)
        else set()
    )
    required_behaviors = expected.get("required_behaviors", [])
    forbidden_behaviors = expected.get("forbidden_behaviors", [])
    required_hits = [
        phrase
        for phrase in required_behaviors
        if isinstance(phrase, str)
        and (phrase in answer or phrase in observed_behaviors)
    ]
    forbidden_hits = [
        phrase
        for phrase in forbidden_behaviors
        if isinstance(phrase, str)
        and (phrase in answer or phrase in observed_behaviors)
    ]
    required_passed = len(required_hits) == len(required_behaviors)
    forbidden_passed = not forbidden_hits

    raw_evidence_refs = actual.get("evidence_refs")
    evidence_refs: set[str] | None = None
    if isinstance(raw_evidence_refs, list):
        evidence_refs = {item for item in raw_evidence_refs if isinstance(item, str)}
    citations = actual.get("citations", [])
    citation_required = bool(expected.get("requires_knowledge_citation"))
    citation_passed = not citation_required
    if citation_required:
        citation_passed = isinstance(citations, list) and any(
            _valid_citation(citation, evidence_refs) for citation in citations
        )

    persistence_values = _string_values(actual.get("persistence_texts", []))
    secret_detected = contains_unredacted_secret(answer) or any(
        contains_unredacted_secret(value) for value in persistence_values
    )
    secret_detected = secret_detected or actual.get("secret_found_in_persistence") is True
    secret_passed = not secret_detected

    expected_status = expected.get("expected_final_status")
    actual_status = actual.get("status")
    route_passed = actual_status == expected_status
    expected_turns = expected.get("turns", [])
    actual_turns = actual.get("turn_results")
    turns_passed = True
    if isinstance(expected_turns, list) and expected_turns:
        if isinstance(actual_turns, list):
            expected_turn_statuses = [
                (turn.get("turn_index"), turn.get("expected_status"))
                for turn in expected_turns
                if isinstance(turn, Mapping)
            ]
            actual_turn_statuses = [
                (turn.get("turn_index"), turn.get("status"))
                for turn in actual_turns
                if isinstance(turn, Mapping)
            ]
            turns_passed = expected_turn_statuses == actual_turn_statuses
        else:
            turns_passed = len(expected_turns) == 1 and route_passed
    illegal_count = actual.get("illegal_transition_count", 0)
    transition_passed = (
        route_passed
        and turns_passed
        and isinstance(illegal_count, int)
        and not isinstance(illegal_count, bool)
        and illegal_count == 0
    )
    fault = expected.get("fault")
    fault_case = fault is not None
    retry_passed = True
    if fault_case and "expected_retry_count" in expected:
        retry_passed = actual.get("retry_count") == expected.get("expected_retry_count")
    safe_fallback_passed = (
        not fault_case
        or (
            route_passed
            and transition_passed
            and retry_passed
            and actual.get("draft_sent", False) is False
            and not answer.strip()
            and forbidden_passed
            and secret_passed
        )
    )

    raw_missing = actual.get("missing_fields", [])
    expected_missing = expected.get("expected_missing_fields", [])
    missing_fields_passed = (
        isinstance(raw_missing, list)
        and isinstance(expected_missing, list)
        and set(raw_missing) == set(expected_missing)
    )
    expected_high = expected.get("expected_priority") in {"P0", "P1"}
    return {
        "case_id": case_id,
        "intent_passed": actual.get("intent") == expected.get("expected_intent"),
        "priority_passed": actual.get("priority") == expected.get("expected_priority"),
        "risk_passed": actual.get("risk_level") == expected.get("expected_risk_level"),
        "route_passed": route_passed,
        "turns_passed": turns_passed,
        "transition_passed": transition_passed,
        "missing_fields_passed": missing_fields_passed,
        "required_behavior_passed": required_passed,
        "required_hits": required_hits,
        "citation_required": citation_required,
        "citation_passed": citation_passed,
        "forbidden_behavior_passed": forbidden_passed,
        "forbidden_hits": forbidden_hits,
        "expected_high_risk": expected_high,
        "secret_case": bool(expected.get("contains_secret")),
        "secret_passed": secret_passed,
        "escalated": actual_status == "escalated",
        "fault_case": fault_case,
        "retry_passed": retry_passed,
        "safe_fallback_passed": safe_fallback_passed,
    }


def _ratio(items: list[bool]) -> float:
    return sum(bool(item) for item in items) / len(items) if items else 0.0


def aggregate_metrics(results: list[Mapping[str, object]]) -> dict[str, float]:
    """Aggregate the seven fixed V2 metrics with metric-specific denominators."""

    if not isinstance(results, list):
        raise TypeError("results 必须是列表")
    high_risk = [result for result in results if result.get("expected_high_risk")]
    citation_cases = [result for result in results if result.get("citation_required")]
    secret_cases = [result for result in results if result.get("secret_case")]
    fault_cases = [result for result in results if result.get("fault_case")]
    return {
        "intent_route_accuracy": _ratio(
            [
                bool(result.get("intent_passed")) and bool(result.get("route_passed"))
                for result in results
            ]
        ),
        "state_transition_accuracy": _ratio(
            [bool(result.get("transition_passed")) for result in results]
        ),
        "citation_completeness": _ratio(
            [bool(result.get("citation_passed")) for result in citation_cases]
        ),
        "forbidden_behavior_rate": 1.0
        - _ratio(
            [bool(result.get("forbidden_behavior_passed")) for result in results]
        ),
        "sensitive_info_violation_rate": (
            0.0
            if not secret_cases
            else 1.0
            - _ratio([bool(result.get("secret_passed")) for result in secret_cases])
        ),
        "high_risk_escalation_recall": _ratio(
            [bool(result.get("escalated")) for result in high_risk]
        ),
        "safe_fallback_rate": _ratio(
            [bool(result.get("safe_fallback_passed")) for result in fault_cases]
        ),
    }


__all__ = [
    "CaseValidationError",
    "aggregate_metrics",
    "score_case",
    "validate_cases",
]
