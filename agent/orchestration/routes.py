"""Deterministic, side-effect-free routing for the support workflow."""

from types import MappingProxyType
from typing import FrozenSet, Mapping

from agent.orchestration.state import MissingField, RiskLevel, Status


class IllegalTransition(ValueError):
    """Raised when a workflow status transition is not explicitly allowed."""


class IllegalRoute(ValueError):
    """Raised when routing inputs are incomplete or internally inconsistent."""


ALLOWED_TRANSITIONS: Mapping[Status, FrozenSet[Status]] = MappingProxyType(
    {
        Status.NEW: frozenset({Status.TRIAGED, Status.ESCALATED}),
        Status.TRIAGED: frozenset(
            {Status.PENDING_USER, Status.DIAGNOSING, Status.ESCALATED}
        ),
        Status.PENDING_USER: frozenset(
            {Status.TRIAGED, Status.ESCALATED, Status.RESOLVED}
        ),
        Status.DIAGNOSING: frozenset(
            {Status.PENDING_USER, Status.REVIEWING, Status.ESCALATED}
        ),
        Status.REVIEWING: frozenset(
            {
                Status.PENDING_USER,
                Status.RESOLVED,
                Status.DIAGNOSING,
                Status.ESCALATED,
            }
        ),
        Status.ESCALATED: frozenset(
            {Status.RESOLVED, Status.PENDING_USER, Status.ESCALATED}
        ),
        Status.RESOLVED: frozenset(),
    }
)

_CATEGORY_VALUES = frozenset(
    {
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
)
_SUGGESTED_ROUTES = frozenset({"clarify", "diagnose", "escalate"})
_CLARITY_VALUES = frozenset({"ambiguous", "partial", "clear"})
_REVIEW_DECISIONS = frozenset({"approve", "revise", "escalate"})
_MISSING_FIELD_VALUES = frozenset(field.value for field in MissingField)
_HUMAN_RISK_LEVELS = frozenset({RiskLevel.HIGH, RiskLevel.CRITICAL})


def _state_mapping(state: object) -> Mapping[str, object]:
    if not isinstance(state, Mapping):
        raise IllegalRoute("路由状态必须是映射")
    return state


def _status(value: object) -> Status:
    if not isinstance(value, str):
        raise IllegalRoute("状态值非法")
    try:
        return Status(value)
    except ValueError as error:
        raise IllegalRoute("状态值非法") from error


def _risk_level(value: object) -> RiskLevel:
    if not isinstance(value, str):
        raise IllegalRoute("风险等级非法")
    try:
        return RiskLevel(value)
    except ValueError as error:
        raise IllegalRoute("风险等级非法") from error


def _required(state: Mapping[str, object], field: str) -> object:
    if field not in state:
        raise IllegalRoute("路由状态缺少必要字段")
    return state[field]


def _optional_bool(state: Mapping[str, object], field: str) -> bool:
    if field not in state:
        return False
    value = state[field]
    if not isinstance(value, bool):
        raise IllegalRoute("布尔路由字段非法")
    return value


def _optional_risk(state: Mapping[str, object]) -> RiskLevel | None:
    if "risk_level" not in state:
        return None
    return _risk_level(state["risk_level"])


def _human_required(
    risk_level: RiskLevel | None, requires_human: bool
) -> bool:
    return requires_human or risk_level in _HUMAN_RISK_LEVELS


def assert_transition(current: object, target: object) -> None:
    """Reject every transition that is not present in the fixed state matrix."""

    try:
        current_status = _status(current)
        target_status = _status(target)
    except IllegalRoute as error:
        raise IllegalTransition("未知工作流状态") from error
    if target_status not in ALLOWED_TRANSITIONS[current_status]:
        raise IllegalTransition("非法状态转移")


def route_after_entry(state: object) -> str:
    values = _state_mapping(state)
    risk_level = _risk_level(_required(values, "risk_level"))
    requires_human = _optional_bool(values, "requires_human")
    status = _status(values["status"]) if "status" in values else None
    if status not in {
        None,
        Status.NEW,
        Status.PENDING_USER,
        Status.ESCALATED,
    }:
        raise IllegalRoute("入口状态非法")
    return (
        "human_review"
        if status == Status.ESCALATED
        or _human_required(risk_level, requires_human)
        else "triage"
    )


def route_after_triage(state: object) -> str:
    values = _state_mapping(state)
    status = _status(_required(values, "status"))
    risk_level = _optional_risk(values)
    requires_human = _optional_bool(values, "requires_human")
    category = values.get("category")
    if category is not None and (
        not isinstance(category, str) or category not in _CATEGORY_VALUES
    ):
        raise IllegalRoute("工单分类非法")
    suggested_route = values.get("suggested_route")
    if suggested_route is not None and (
        not isinstance(suggested_route, str)
        or suggested_route not in _SUGGESTED_ROUTES
    ):
        raise IllegalRoute("分诊建议路由非法")
    missing_fields = values.get("missing_fields")
    if missing_fields is not None:
        if not isinstance(missing_fields, list):
            raise IllegalRoute("缺失字段列表非法")
        if any(
            not isinstance(field, str) or field not in _MISSING_FIELD_VALUES
            for field in missing_fields
        ) or len(missing_fields) != len(set(missing_fields)):
            raise IllegalRoute("缺失字段列表非法")
    if status == Status.ESCALATED:
        return "human_review"

    clarity = values.get("clarity")
    if not isinstance(clarity, str) or clarity not in _CLARITY_VALUES:
        raise IllegalRoute("问题清晰度非法")
    clarification_question = values.get("clarification_question")
    clarification_options = values.get("clarification_options")
    if not isinstance(clarification_question, str) or not isinstance(
        clarification_options, list
    ):
        raise IllegalRoute("澄清内容非法")
    if any(
        not isinstance(option, str) or not option
        for option in clarification_options
    ) or len(clarification_options) != len(set(clarification_options)):
        raise IllegalRoute("澄清选项非法")

    if status != Status.TRIAGED:
        raise IllegalRoute("分诊后的状态非法")
    if risk_level is None or suggested_route is None or missing_fields is None:
        raise IllegalRoute("路由状态缺少必要字段")

    if _human_required(risk_level, requires_human):
        return "escalate"
    if category == "security_report" or suggested_route == "escalate":
        return "escalate"
    if suggested_route == "clarify":
        if (
            clarity != "ambiguous"
            or missing_fields
            or not clarification_question
            or not 2 <= len(clarification_options) <= 5
        ):
            raise IllegalRoute("分诊结果互相矛盾")
        return "pending_user"
    if clarification_question or clarification_options:
        raise IllegalRoute("分诊结果互相矛盾")
    if clarity == "partial":
        if suggested_route != "diagnose" or not missing_fields:
            raise IllegalRoute("分诊结果互相矛盾")
        return "start_diagnosis"
    if clarity != "clear" or missing_fields:
        raise IllegalRoute("分诊结果互相矛盾")
    return "start_diagnosis"


def route_after_diagnosis(state: object) -> str:
    values = _state_mapping(state)
    status = _status(_required(values, "status"))
    risk_level = _optional_risk(values)
    requires_human = _optional_bool(values, "requires_human")
    routes = {
        Status.PENDING_USER: "await_user",
        Status.REVIEWING: "review",
        Status.ESCALATED: "human_review",
    }
    if status not in routes:
        raise IllegalRoute("诊断后的状态非法")
    if _human_required(risk_level, requires_human):
        return "human_review"
    return routes[status]


def route_after_review(state: object) -> str:
    values = _state_mapping(state)
    status = _status(values["status"]) if "status" in values else None
    if status not in {None, Status.REVIEWING, Status.ESCALATED}:
        raise IllegalRoute("审核后的状态非法")
    decision = _required(values, "review_decision")
    if not isinstance(decision, str) or decision not in _REVIEW_DECISIONS:
        raise IllegalRoute("审核决定非法")
    revision_count = _required(values, "revision_count")
    if (
        isinstance(revision_count, bool)
        or not isinstance(revision_count, int)
        or revision_count < 0
    ):
        raise IllegalRoute("返工计数非法")
    requires_human = _required(values, "requires_human")
    if not isinstance(requires_human, bool):
        raise IllegalRoute("人工门禁字段非法")
    risk_level = _optional_risk(values)

    if status == Status.ESCALATED or _human_required(
        risk_level, requires_human
    ):
        return "escalate"
    if decision == "approve":
        return "finalize"
    if decision == "revise" and revision_count == 0:
        return "revision"
    return "escalate"


def route_after_human(state: object) -> str:
    values = _state_mapping(state)
    status = _status(_required(values, "status"))
    _optional_risk(values)
    _optional_bool(values, "requires_human")
    routes = {
        Status.RESOLVED: "end",
        Status.PENDING_USER: "await_user",
        Status.ESCALATED: "human_review",
    }
    if status not in routes:
        raise IllegalRoute("人工处理后的状态非法")
    return routes[status]
