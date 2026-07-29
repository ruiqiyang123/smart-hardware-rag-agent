"""Chat 模型配置解析。

把 Streamlit UI 的选择、环境变量里的共享 Key、以及模型工厂需要的 kwargs
分开处理，避免页面逻辑里散落 provider 判断，也避免把完整 API Key 写进日志。
"""
from dataclasses import dataclass
from hashlib import sha256
from typing import Dict, Optional


DEFAULT_MIMO_BASE_URL = "https://token-plan-sgp.xiaomimimo.com/v1"
DEFAULT_MIMO_CHAT_MODEL = "mimo-v2.5-pro"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_CHAT_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_THINKING = "disabled"
DEFAULT_EMBEDDING_PROVIDER = "local"
DEFAULT_LOCAL_EMBEDDING_DIMENSION = 1024


@dataclass(frozen=True)
class ChatConfig:
    kwargs: Dict[str, Optional[str]]
    signature: str
    is_configured: bool
    provider: str


def _key_fingerprint(api_key: str) -> str:
    return sha256(api_key.encode("utf-8")).hexdigest()[:12]


def normalize_provider(provider: Optional[str]) -> str:
    normalized = (provider or "").strip().lower()
    if normalized == "deepseek":
        return "deepseek"
    if normalized in {"mimo", "openai"}:
        return "mimo"
    if normalized in {"dashscope", "qwen", "tongyi"}:
        return "dashscope"
    if not normalized:
        return "deepseek"
    raise ValueError("不支持的聊天模型 Provider")


def build_chat_config(
    provider: Optional[str],
    dashscope_key: Optional[str],
    mimo_key: Optional[str],
    mimo_base_url: Optional[str],
    mimo_model_name: Optional[str],
    deepseek_key: Optional[str] = None,
    deepseek_base_url: Optional[str] = None,
    deepseek_model_name: Optional[str] = None,
    deepseek_thinking: Optional[str] = None,
) -> ChatConfig:
    """根据选中的 provider 构建模型 kwargs 和安全缓存签名。"""
    provider = normalize_provider(provider)

    if provider == "deepseek":
        api_key = (deepseek_key or "").strip()
        thinking = (
            DEFAULT_DEEPSEEK_THINKING
            if deepseek_thinking is None
            else deepseek_thinking.strip().lower()
        )
        if thinking not in {"enabled", "disabled"}:
            raise ValueError("DEEPSEEK_THINKING 只支持 enabled 或 disabled")
        if not api_key:
            return ChatConfig(
                kwargs={},
                signature="default",
                is_configured=False,
                provider=provider,
            )

        base_url = (
            DEFAULT_DEEPSEEK_BASE_URL
            if deepseek_base_url is None
            else deepseek_base_url.strip()
        )
        model_name = (
            DEFAULT_DEEPSEEK_CHAT_MODEL
            if deepseek_model_name is None
            else deepseek_model_name.strip()
        )
        if not base_url or not model_name:
            raise ValueError("DeepSeek base URL 和模型名不能为空")
        signature = (
            f"deepseek:{_key_fingerprint(api_key)}:{base_url}:{model_name}:{thinking}"
        )
        return ChatConfig(
            kwargs={
                "provider": "deepseek",
                "api_key": api_key,
                "base_url": base_url,
                "model_name": model_name,
                "thinking": thinking,
            },
            signature=signature,
            is_configured=True,
            provider=provider,
        )

    if provider == "mimo":
        api_key = (mimo_key or "").strip()
        if not api_key:
            return ChatConfig(kwargs={}, signature="default", is_configured=False, provider=provider)

        base_url = (mimo_base_url or DEFAULT_MIMO_BASE_URL).strip()
        model_name = (mimo_model_name or DEFAULT_MIMO_CHAT_MODEL).strip()
        signature = f"mimo:{_key_fingerprint(api_key)}:{base_url}:{model_name}"
        return ChatConfig(
            kwargs={
                "provider": "mimo",
                "api_key": api_key,
                "base_url": base_url,
                "model_name": model_name,
            },
            signature=signature,
            is_configured=True,
            provider=provider,
        )

    api_key = (dashscope_key or "").strip()
    if not api_key:
        return ChatConfig(kwargs={}, signature="default", is_configured=False, provider=provider)

    return ChatConfig(
        kwargs={"provider": "dashscope", "api_key": api_key},
        signature=f"dashscope:{_key_fingerprint(api_key)}",
        is_configured=True,
        provider=provider,
    )
