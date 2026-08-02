import re

import yaml
from utils.path_tool import get_abs_path


def load_rag_config(config_path: str = get_abs_path("config/rag.yml"), encoding: str = "utf-8"):
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.safe_load(f)


def load_chroma_config(config_path: str = get_abs_path("config/chroma.yml"), encoding: str = "utf-8"):
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.safe_load(f)


def load_prompts_config(config_path: str = get_abs_path("config/prompts.yml"), encoding: str = "utf-8"):
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.safe_load(f)


def load_agent_config(config_path: str = get_abs_path("config/agent.yml"), encoding: str = "utf-8"):
    with open(config_path, "r", encoding=encoding) as f:
        return yaml.safe_load(f)


def _load_required_yaml(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file)
    if not isinstance(data, dict):
        raise TypeError(f"配置文件不是对象: {config_path}")
    return data


def _require_keys(data: dict, keys: set, source: str) -> None:
    missing = sorted(keys.difference(data))
    if missing:
        raise ValueError(f"{source} 缺少字段: {', '.join(missing)}")


def _require_dict(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"{field} 必须是对象")
    return value


def _require_integer(value, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} 必须是整数")
    if value < minimum:
        comparator = "正整数" if minimum == 1 else "非负整数"
        raise ValueError(f"{field} 必须是{comparator}")
    return value


def _require_non_empty_string(value, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是字符串")
    if not value.strip():
        raise ValueError(f"{field} 不能为空")
    return value


_FIXED_TIMEOUTS = {
    "agent_seconds": 20,
    "readonly_tool_seconds": 8,
    "graph_seconds": 120,
}
_FIXED_RETRIES = {
    "triage": 1,
    "diagnosis": 1,
    "readonly_tool": 1,
    "review": 0,
}
_FIXED_REQUIRED_FIELDS = {
    "firmware_repair": ["device_model", "error_state"],
    "warranty_service": ["serial_last4"],
    "transaction_boundary": ["transaction_hash", "chain_name"],
}
_FIXED_MANUAL_GATE_ACTIONS = {
    "device_reset",
    "bootloader_recovery",
    "warranty_decision",
}
_FIXED_CUSTOMER_SESSION = {
    "inactivity_timeout_seconds": 1800,
    "expiry_poll_seconds": 30,
}
_FIXED_CUSTOMER_HANDOFF = {"ai_attempt_threshold": 3}


def load_orchestration_config(
    config_path: str = get_abs_path("config/orchestration.yml"),
) -> dict:
    data = _load_required_yaml(config_path)
    _require_keys(
        data,
        {
            "timeouts",
            "retries",
            "recursion_limit",
            "command_lease_seconds",
            "customer_session",
            "customer_handoff",
            "required_fields",
            "manual_gate_actions",
        },
        "orchestration.yml",
    )

    timeouts = _require_dict(data["timeouts"], "timeouts")
    timeout_keys = set(_FIXED_TIMEOUTS)
    _require_keys(timeouts, timeout_keys, "timeouts")
    for key in timeout_keys:
        _require_integer(timeouts[key], f"timeouts.{key}", minimum=1)
        if timeouts[key] != _FIXED_TIMEOUTS[key]:
            raise ValueError(f"timeouts.{key} 必须为 {_FIXED_TIMEOUTS[key]}")

    retries = _require_dict(data["retries"], "retries")
    retry_keys = set(_FIXED_RETRIES)
    _require_keys(retries, retry_keys, "retries")
    for key in retry_keys:
        _require_integer(retries[key], f"retries.{key}", minimum=0)
        if retries[key] != _FIXED_RETRIES[key]:
            raise ValueError(f"retries.{key} 必须为 {_FIXED_RETRIES[key]}")
    if retries["review"] != 0:
        raise ValueError("retries.review 必须为 0")

    recursion_limit = _require_integer(
        data["recursion_limit"], "recursion_limit", minimum=1
    )
    if recursion_limit != 16:
        raise ValueError("recursion_limit 必须为 16")
    lease = _require_integer(
        data["command_lease_seconds"], "command_lease_seconds", minimum=1
    )
    if lease <= timeouts["graph_seconds"]:
        raise ValueError("command_lease_seconds 必须大于 timeouts.graph_seconds")
    if lease != 130:
        raise ValueError("command_lease_seconds 必须为 130")

    customer_session = _require_dict(data["customer_session"], "customer_session")
    _require_keys(
        customer_session,
        set(_FIXED_CUSTOMER_SESSION),
        "customer_session",
    )
    for key, expected in _FIXED_CUSTOMER_SESSION.items():
        _require_integer(customer_session[key], f"customer_session.{key}", minimum=1)
        if customer_session[key] != expected:
            raise ValueError(f"customer_session.{key} 必须为 {expected}")

    customer_handoff = _require_dict(data["customer_handoff"], "customer_handoff")
    _require_keys(
        customer_handoff,
        set(_FIXED_CUSTOMER_HANDOFF),
        "customer_handoff",
    )
    for key, expected in _FIXED_CUSTOMER_HANDOFF.items():
        _require_integer(customer_handoff[key], f"customer_handoff.{key}", minimum=1)
        if customer_handoff[key] != expected:
            raise ValueError(f"customer_handoff.{key} 必须为 {expected}")

    required_fields = _require_dict(data["required_fields"], "required_fields")
    if not required_fields:
        raise ValueError("required_fields 不能为空")
    for intent, fields in required_fields.items():
        _require_non_empty_string(intent, "required_fields 的键")
        if not isinstance(fields, list):
            raise TypeError(f"required_fields.{intent} 必须是字符串列表")
        if not fields:
            raise ValueError(f"required_fields.{intent} 不能为空")
        for field in fields:
            _require_non_empty_string(field, f"required_fields.{intent} 的字段")
    if set(required_fields) != set(_FIXED_REQUIRED_FIELDS):
        raise ValueError("required_fields 必须包含且仅包含固定意图")
    for intent, expected_fields in _FIXED_REQUIRED_FIELDS.items():
        fields = required_fields[intent]
        if len(fields) != len(set(fields)):
            raise ValueError(f"required_fields.{intent} 不得包含重复字段")
        if set(fields) != set(expected_fields):
            raise ValueError(f"required_fields.{intent} 必须包含固定字段")

    manual_actions = data["manual_gate_actions"]
    if not isinstance(manual_actions, list):
        raise TypeError("manual_gate_actions 必须是字符串列表")
    if not manual_actions:
        raise ValueError("manual_gate_actions 不能为空")
    for action in manual_actions:
        _require_non_empty_string(action, "manual_gate_actions 的动作")
    if len(manual_actions) != len(set(manual_actions)):
        raise ValueError("manual_gate_actions 不得包含重复动作")
    if set(manual_actions) != _FIXED_MANUAL_GATE_ACTIONS:
        raise ValueError("manual_gate_actions 必须包含且仅包含固定动作")
    return data


_HOSTNAME_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


def load_security_policy(
    config_path: str = get_abs_path("config/security_policy.yml"),
) -> dict:
    data = _load_required_yaml(config_path)
    _require_keys(
        data,
        {"policy_version", "official_domains", "critical_response_template_zh"},
        "security_policy.yml",
    )
    _require_non_empty_string(data["policy_version"], "policy_version")
    _require_non_empty_string(
        data["critical_response_template_zh"], "critical_response_template_zh"
    )

    domains = data["official_domains"]
    if not isinstance(domains, list):
        raise TypeError("official_domains 必须是字符串列表")
    if not domains:
        raise ValueError("official_domains 不能为空")
    for domain in domains:
        if not isinstance(domain, str):
            raise TypeError("official_domains 的每一项必须是字符串")
        if not domain.strip():
            raise ValueError("official_domains 不能包含空项")
        if not _HOSTNAME_PATTERN.fullmatch(domain):
            raise ValueError(f"official_domains 包含非规范 hostname: {domain}")
    return data


rag_conf = load_rag_config()
chroma_conf = load_chroma_config()
agent_conf = load_agent_config()
prompts_conf = load_prompts_config()

if __name__ == '__main__':
    print(agent_conf["chat_model_name"])  # 输出对应配置
