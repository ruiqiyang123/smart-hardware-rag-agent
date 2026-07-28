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
        raise ValueError(f"配置文件不是对象: {config_path}")
    return data


def load_orchestration_config(
    config_path: str = get_abs_path("config/orchestration.yml"),
) -> dict:
    data = _load_required_yaml(config_path)
    required = {"timeouts", "retries", "recursion_limit", "command_lease_seconds"}
    if not required.issubset(data):
        raise ValueError("orchestration.yml 缺少执行边界")
    return data


def load_security_policy(
    config_path: str = get_abs_path("config/security_policy.yml"),
) -> dict:
    data = _load_required_yaml(config_path)
    if not str(data.get("policy_version", "")).strip():
        raise ValueError("security_policy.yml 缺少 policy_version")
    if not data.get("official_domains"):
        raise ValueError("security_policy.yml 的 official_domains 不能为空")
    if not str(data.get("critical_response_template_zh", "")).strip():
        raise ValueError("security_policy.yml 缺少 critical_response_template_zh")
    return data


rag_conf = load_rag_config()
chroma_conf = load_chroma_config()
agent_conf = load_agent_config()
prompts_conf = load_prompts_config()

if __name__ == '__main__':
    print(agent_conf["chat_model_name"])  # 输出对应配置
