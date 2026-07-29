"""Structured and fail-closed ticket triage."""

import copy
import json
import math
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agent.orchestration.invoke import invoke_with_policy
from agent.nodes.prompt_loader import load_safe_prompt
from agent.orchestration.state import (
    MissingField,
    RiskFlag,
    RiskLevel,
    TriageResult,
)
from agent.security.secrets import contains_unredacted_secret


_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "triage_prompt.txt"
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
_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}
_CRITICAL_FLAGS = frozenset(
    {
        RiskFlag.SECRET_EXPOSURE,
        RiskFlag.PHISHING,
        RiskFlag.ASSET_LOSS,
    }
)
_HIGH_FLAGS = frozenset(
    {
        RiskFlag.UNOFFICIAL_FIRMWARE,
        RiskFlag.ADDRESS_MISMATCH,
        RiskFlag.SUSPICIOUS_SIGNATURE,
        RiskFlag.DEVICE_AUTH_FAILURE,
        RiskFlag.REMOTE_CONTROL,
    }
)
_SAFE_HISTORY_ROLES = frozenset({"user", "assistant"})
_MAX_SANITIZED_INPUT_LENGTH = 10_000
_MAX_SAFE_HISTORY_ITEMS = 50
_MAX_HISTORY_CONTENT_LENGTH = 4_000
_MAX_SAFE_HISTORY_TOTAL_LENGTH = 20_000


def _validate_execution_policy(timeout_seconds: object, retries: object) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds 必须是有限正数")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError("retries 必须是非负整数")


def _risk_level(value: object) -> RiskLevel:
    if isinstance(value, RiskLevel):
        return value
    if not isinstance(value, str):
        raise TypeError("risk_level 必须是已知枚举")
    try:
        return RiskLevel(value)
    except ValueError:
        raise ValueError("risk_level 必须是已知枚举") from None


def _risk_flags(value: object, field_name: str) -> list[RiskFlag]:
    if not isinstance(value, list):
        raise TypeError(f"{field_name} 必须是列表")
    normalized: list[RiskFlag] = []
    for item in value:
        if isinstance(item, RiskFlag):
            flag = item
        elif isinstance(item, str):
            try:
                flag = RiskFlag(item)
            except ValueError:
                raise ValueError(f"{field_name} 包含未知枚举") from None
        else:
            raise TypeError(f"{field_name} 包含非法值")
        if flag not in normalized:
            normalized.append(flag)
    return normalized


def _validate_required_fields(
    required_fields: object,
) -> dict[str, list[MissingField]]:
    if not isinstance(required_fields, dict):
        raise TypeError("required_fields 必须是 category 到字段列表的映射")
    if not required_fields:
        raise ValueError("required_fields 不能为空")
    validated: dict[str, list[MissingField]] = {}
    for category, fields in copy.deepcopy(required_fields).items():
        if not isinstance(category, str) or category not in _CATEGORY_VALUES:
            raise ValueError("required_fields 包含未知 category")
        if not isinstance(fields, list):
            raise TypeError("required_fields 的值必须是列表")
        if not fields:
            raise ValueError("required_fields 的字段列表不能为空")
        normalized: list[MissingField] = []
        for field in fields:
            if isinstance(field, MissingField):
                item = field
            elif isinstance(field, str):
                try:
                    item = MissingField(field)
                except ValueError:
                    raise ValueError("required_fields 包含未知字段") from None
            else:
                raise TypeError("required_fields 包含非法字段")
            if item in normalized:
                raise ValueError("required_fields 不得包含重复字段")
            normalized.append(item)
        validated[category] = normalized
    return validated


def _contains_forbidden_control(value: str) -> bool:
    return any(
        (ord(character) < 0x20 and character not in {"\n", "\t"})
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    )


def _load_prompt(path: Path) -> str:
    return load_safe_prompt(path, "Triage")


def _safe_history(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise TypeError("safe_history 必须是列表")
    if len(value) > _MAX_SAFE_HISTORY_ITEMS:
        raise ValueError("safe_history 超过最大条数")
    normalized: list[dict[str, str]] = []
    total_length = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise TypeError("safe_history 消息结构非法")
        role = item["role"]
        content = item["content"]
        if not isinstance(role, str) or role not in _SAFE_HISTORY_ROLES:
            raise ValueError("safe_history role 非法")
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > _MAX_HISTORY_CONTENT_LENGTH
        ):
            raise ValueError("safe_history content 非法")
        if contains_unredacted_secret(content):
            raise ValueError("safe_history 包含未脱敏内容")
        total_length += len(content)
        if total_length > _MAX_SAFE_HISTORY_TOTAL_LENGTH:
            raise ValueError("safe_history 总长度超限")
        normalized.append({"role": role, "content": content})
    return normalized


def _validate_runner_result(
    raw_result: object,
    required_fields: dict[str, list[MissingField]],
) -> TriageResult:
    if isinstance(raw_result, TriageResult):
        candidate: Any = raw_result.model_dump(mode="python")
    elif isinstance(raw_result, dict):
        candidate = raw_result
    else:
        raise TypeError("分诊输出类型非法")
    result = TriageResult.model_validate(candidate)

    if _contains_forbidden_control(result.summary):
        raise ValueError("分诊摘要包含非法控制字符")
    summary = " ".join(result.summary.split())
    if not summary or contains_unredacted_secret(summary):
        raise ValueError("分诊摘要包含未脱敏内容")
    result = result.model_copy(update={"summary": summary})
    allowed_missing = set(required_fields.get(result.category, []))
    if not set(result.missing_fields).issubset(allowed_missing):
        raise ValueError("分诊输出包含该 category 不需要的字段")
    if (
        result.risk_level in {RiskLevel.LOW, RiskLevel.MEDIUM}
        and result.category not in {"security_incident", "security_report"}
        and not result.missing_fields
        and result.suggested_route == "ask_user"
    ):
        raise ValueError("ask_user 必须包含缺失字段")
    return result


class TriageAgent:
    """Run structured triage with deterministic safety policy.

    ``retries`` applies only to completed, retryable validation/type failures in
    ``invoke_with_policy``. A timeout fails closed immediately and is never
    retried because its worker may still be running.
    """

    def __init__(
        self,
        model: object | None = None,
        runner: object | None = None,
        timeout_seconds: float = 20,
        retries: int = 1,
        required_fields: object | None = None,
    ) -> None:
        if (model is None) == (runner is None):
            raise ValueError("TriageAgent 必须且只能提供 model 或 runner")
        _validate_execution_policy(timeout_seconds, retries)
        if model is not None:
            factory = getattr(model, "with_structured_output", None)
            if not callable(factory):
                raise TypeError("model 不支持结构化输出")
            runner = factory(TriageResult, method="function_calling")
        if not callable(getattr(runner, "invoke", None)):
            raise TypeError("runner 必须提供 invoke")

        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        if required_fields is None:
            from utils.config_handler import load_orchestration_config

            required_fields = load_orchestration_config()["required_fields"]
        self.required_fields = _validate_required_fields(required_fields)
        self.prompt = _load_prompt(_PROMPT_PATH)

    def run(self, state: dict) -> dict:
        if not isinstance(state, dict):
            raise TypeError("state 必须是对象")
        ingress_fields = ("risk_level", "sensitive_flags", "risk_flags")
        if any(field not in state for field in ingress_fields):
            raise ValueError("state 缺少入口安全字段")
        sanitized_input = state.get("sanitized_input")
        if (
            not isinstance(sanitized_input, str)
            or not sanitized_input.strip()
            or len(sanitized_input) > _MAX_SANITIZED_INPUT_LENGTH
        ):
            raise ValueError("sanitized_input 必须是非空字符串")
        if contains_unredacted_secret(sanitized_input):
            raise ValueError("sanitized_input 包含未脱敏内容")

        history = _safe_history(state.get("safe_history", []))
        ingress_risk = _risk_level(state["risk_level"])
        sensitive_flags = _risk_flags(state["sensitive_flags"], "sensitive_flags")
        existing_risk_flags = _risk_flags(state["risk_flags"], "risk_flags")
        entry_flags = list(dict.fromkeys(sensitive_flags + existing_risk_flags))
        required_fields_json = {
            category: [field.value for field in fields]
            for category, fields in self.required_fields.items()
        }
        payload = {
            "sanitized_input": sanitized_input,
            "safe_history": history,
            "risk_level": ingress_risk.value,
            "sensitive_flags": [flag.value for flag in sensitive_flags],
            "risk_flags": [flag.value for flag in entry_flags],
            "required_fields": required_fields_json,
        }
        messages = [
            SystemMessage(content=self.prompt),
            HumanMessage(
                content=json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            ),
        ]

        result = invoke_with_policy(
            lambda: _validate_runner_result(
                self.runner.invoke(messages), self.required_fields
            ),
            timeout_seconds=self.timeout_seconds,
            retries=self.retries,
        )
        combined_flags = list(dict.fromkeys(entry_flags + list(result.risk_flags)))
        flag_set = set(combined_flags)
        flag_floor = RiskLevel.LOW
        if flag_set & _CRITICAL_FLAGS:
            flag_floor = RiskLevel.CRITICAL
        elif flag_set & _HIGH_FLAGS:
            flag_floor = RiskLevel.HIGH
        effective_risk = max(
            (ingress_risk, result.risk_level, flag_floor),
            key=_RISK_ORDER.__getitem__,
        )

        output = result.model_dump(mode="json")
        output["risk_level"] = effective_risk.value
        output["risk_flags"] = [flag.value for flag in combined_flags]
        if effective_risk == RiskLevel.CRITICAL:
            output["priority"] = "P0"
            output["suggested_route"] = "escalate"
            output["missing_fields"] = []
        elif effective_risk == RiskLevel.HIGH:
            output["priority"] = "P1"
            output["suggested_route"] = "escalate"
            output["missing_fields"] = []
        else:
            output["priority"] = "P2"
            if output["missing_fields"]:
                output["suggested_route"] = "ask_user"
            elif output["category"] in {"security_incident", "security_report"}:
                output["suggested_route"] = "escalate"
            else:
                output["suggested_route"] = "diagnose"
        return TriageResult.model_validate(output).model_dump(mode="json")
