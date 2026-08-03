import copy
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


_TRIAGE_POLICY_FIELDS = {
    "policy_version",
    "automatic_escalation",
    "advisory_flags",
    "definitions",
    "clarification",
    "contrastive_examples",
}
_TRIAGE_CRITICAL_FLAGS = {"secret_exposure", "phishing", "asset_loss"}
_TRIAGE_HIGH_FLAGS = {"address_mismatch", "suspicious_signature"}
_TRIAGE_ADVISORY_FLAGS = {
    "unofficial_firmware",
    "device_auth_failure",
    "remote_control",
}
_TRIAGE_DEFINITION_KEYS = {
    "device_loss_damage",
    "asset_loss",
    "ordinary_transaction_issue",
    "confirmed_secret_exposure",
}
_TRIAGE_EXAMPLE_FIELDS = {
    "input",
    "category",
    "risk_level",
    "route",
}
_TRIAGE_EXAMPLE_OPTIONAL_FIELDS = {"required_flags", "forbidden_flags"}


def _require_exact_keys(data: dict, expected: set, source: str) -> None:
    _require_keys(data, expected, source)
    extras = sorted(set(data).difference(expected))
    if extras:
        raise ValueError(f"{source} 包含未知字段: {', '.join(extras)}")


def _require_unique_string_list(value, field: str, *, expected: set | None = None) -> list:
    if not isinstance(value, list):
        raise TypeError(f"{field} 必须是字符串列表")
    if not value:
        raise ValueError(f"{field} 不能为空")
    for item in value:
        _require_non_empty_string(item, field)
    if len(value) != len(set(value)):
        raise ValueError(f"{field} 不得包含重复项")
    if expected is not None and set(value) != expected:
        raise ValueError(f"{field} 必须包含固定值")
    return list(value)


def validate_triage_policy(data: object) -> dict:
    if not isinstance(data, dict):
        raise TypeError("triage_policy 必须是对象")
    _require_exact_keys(data, _TRIAGE_POLICY_FIELDS, "triage_policy.yml")
    _require_non_empty_string(data["policy_version"], "policy_version")

    escalation = _require_dict(
        data["automatic_escalation"], "automatic_escalation"
    )
    _require_exact_keys(
        escalation,
        {"critical_flags", "high_flags"},
        "automatic_escalation",
    )
    _require_unique_string_list(
        escalation["critical_flags"],
        "automatic_escalation.critical_flags",
        expected=_TRIAGE_CRITICAL_FLAGS,
    )
    _require_unique_string_list(
        escalation["high_flags"],
        "automatic_escalation.high_flags",
        expected=_TRIAGE_HIGH_FLAGS,
    )
    _require_unique_string_list(
        data["advisory_flags"],
        "advisory_flags",
        expected=_TRIAGE_ADVISORY_FLAGS,
    )

    definitions = _require_dict(data["definitions"], "definitions")
    _require_exact_keys(definitions, _TRIAGE_DEFINITION_KEYS, "definitions")
    for key, value in definitions.items():
        _require_non_empty_string(value, f"definitions.{key}")

    clarification = _require_dict(data["clarification"], "clarification")
    _require_exact_keys(
        clarification,
        {"mode", "min_options", "max_options", "requirements"},
        "clarification",
    )
    if clarification["mode"] != "dynamic_contextual":
        raise ValueError("clarification.mode 必须是 dynamic_contextual")
    if (
        type(clarification["min_options"]) is not int
        or type(clarification["max_options"]) is not int
        or clarification["min_options"] != 2
        or clarification["max_options"] != 5
    ):
        raise ValueError("clarification 选项数量必须是 2-5")
    requirements = _require_unique_string_list(
        clarification["requirements"], "clarification.requirements"
    )
    if len(requirements) < 3:
        raise ValueError("clarification.requirements 至少包含 3 条规则")

    examples = data["contrastive_examples"]
    if not isinstance(examples, list):
        raise TypeError("contrastive_examples 必须是对象列表")
    if len(examples) < 4:
        raise ValueError("contrastive_examples 至少包含 4 个案例")
    for index, example in enumerate(examples):
        field = f"contrastive_examples[{index}]"
        if not isinstance(example, dict):
            raise TypeError(f"{field} 必须是对象")
        keys = set(example)
        if not _TRIAGE_EXAMPLE_FIELDS.issubset(keys) or not keys.issubset(
            _TRIAGE_EXAMPLE_FIELDS | _TRIAGE_EXAMPLE_OPTIONAL_FIELDS
        ):
            raise ValueError(f"{field} 字段非法")
        for key in _TRIAGE_EXAMPLE_FIELDS:
            _require_non_empty_string(example[key], f"{field}.{key}")
        for key in _TRIAGE_EXAMPLE_OPTIONAL_FIELDS.intersection(example):
            _require_unique_string_list(example[key], f"{field}.{key}")
        if not _TRIAGE_EXAMPLE_OPTIONAL_FIELDS.intersection(example):
            raise ValueError(f"{field} 必须声明 required_flags 或 forbidden_flags")
    return copy.deepcopy(data)


def load_triage_policy(
    config_path: str = get_abs_path("config/triage_policy.yml"),
) -> dict:
    return validate_triage_policy(_load_required_yaml(config_path))


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
_FIXED_CUSTOMER_SESSION = {
    "inactivity_timeout_seconds": 1800,
    "expiry_poll_seconds": 30,
}
_REMOVED_ORCHESTRATION_SECTIONS = {
    "customer_handoff",
    "manual_gate_actions",
}


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
            "required_fields",
        },
        "orchestration.yml",
    )
    legacy_sections = sorted(_REMOVED_ORCHESTRATION_SECTIONS.intersection(data))
    if legacy_sections:
        raise ValueError(
            "orchestration.yml 包含已移除字段: " + ", ".join(legacy_sections)
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
