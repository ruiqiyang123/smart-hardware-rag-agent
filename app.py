import hashlib
import hmac
import os
import re
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from utils.env_bootstrap import bootstrap_environment

bootstrap_environment()

import streamlit as st

from agent.orchestration.graph import build_configured_graph, sqlite_checkpointer
from agent.orchestration.runtime import (
    CheckpointRestoreError,
    CommandFailedError,
    CommandInProgressError,
    SupportOrchestrator,
)
from agent.policies.security import IngressGuard, PolicyGuard
from agent.react_agent import ReactAgent
from agent.security.trusted_sources import TrustedSourcePolicy
from agent.tools.agent_tools import configure_rag_model
from agent.ui_progress import (
    append_processing_turn,
    project_customer_phases,
    project_workbench_events,
    replace_processing_answer,
)
from database.profile_db import ProfileDatabase, UserProfile
from database.ticket_db import IdempotencyConflictError, TicketRepository
from model.factory import build_chat_model
from utils.config_handler import load_orchestration_config, load_security_policy, rag_conf
from utils.logger_handler import logger
from utils.model_config import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_CHAT_MODEL,
    DEFAULT_DEEPSEEK_THINKING,
    DEFAULT_MIMO_BASE_URL,
    DEFAULT_MIMO_CHAT_MODEL,
    build_chat_config,
    normalize_provider,
)
from utils.session_context import set_location, set_user_id
from utils.ui_command_state import (
    ACTION_ID_SESSION_PREFIX,
    EDITOR_SESSION_PREFIX,
    REQUEST_ID_SESSION_KEY,
    clear_editor_state,
    clear_frozen_action,
    clear_frozen_actions,
    clear_frozen_request,
    get_or_freeze_action_command,
    get_or_freeze_request_command,
)


st.set_page_config(
    page_title="KeyGuard 2.0 · 多 Agent 工单客服",
    page_icon="🔐",
    layout="centered",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .block-container {
        padding-top: 2rem;
        padding-bottom: 5rem;
        max-width: 820px;
      }
      [data-testid="stChatMessage"] {
        padding: 0.6rem 0.85rem;
        border-radius: 0.75rem;
      }
      [data-testid="stSidebar"] h2 {
        margin-top: 0.4rem;
        margin-bottom: 0.5rem;
        font-size: 1.02rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🔐 KeyGuard 硬件钱包智能客服 2.0")
st.caption("LangGraph 多 Agent 工单协同 · 安全分诊、证据诊断、复核与人工接管")


def _runtime_secret(name: str) -> Optional[str]:
    """读取环境变量 / Streamlit Secrets，避免 Cloud 与本地配置方式不一致。"""
    if name in os.environ:
        value = os.environ[name]
        return value if value else None

    try:
        secret_value = st.secrets.get(name)
    except Exception:
        return None
    return str(secret_value) if secret_value else None


AGENT_VERSION = (_runtime_secret("KEYGUARD_AGENT_VERSION") or "v2").strip().lower()
USE_V1_AGENT = AGENT_VERSION == "v1"
ORCHESTRATOR_CONTRACT_VERSION = "2026-07-30-structured-output-v2"
SAFE_FAILURE_NOTICE = "⚠️ 当前请求未能安全完成，请稍后重试或联系人工客服。"
PENDING_USER_NOTICE = "为了继续处理，请补充工单中标记的必要信息。"
ESCALATED_NOTICE = "工单已进入人工审核，自动流程不会关闭该问题。"
RECOVERING_REQUEST_NOTICE = (
    "正在恢复上次请求；本次输入不会作为一个新问题处理。"
)
RECOVERED_REQUEST_NOTICE = (
    "已按原内容恢复上次请求；本次输入未作为新问题处理。"
)
RECOVERY_NOTICE_SESSION_KEY = "keyguard_v2_recovery_notice"
PENDING_UI_REQUEST_SESSION_KEY = "keyguard_v2_pending_ui_request"
PROCESSING_UI_NOTICE = "⏳ 正在安全检查、取证并生成回复…"
OPERATOR_AUTH_SESSION_KEY = "keyguard_operator_auth"
_UI_ID_PATTERN = re.compile(r"[0-9a-f]{32}", re.ASCII)
_MARKDOWN_SPECIALS = frozenset(r"\`*_{}[]()#+-.!|<>")


def _stable_session_id(key: str) -> str:
    value = st.session_state.get(key)
    if not isinstance(value, str) or _UI_ID_PATTERN.fullmatch(value) is None:
        value = uuid.uuid4().hex
        st.session_state[key] = value
    return value


def _stable_request_id() -> str:
    return _stable_session_id(REQUEST_ID_SESSION_KEY)


def _stable_action_id(ticket_id: str, action: str) -> tuple[str, str]:
    key = f"{ACTION_ID_SESSION_PREFIX}{ticket_id}:{action}"
    return key, _stable_session_id(key)


def _clear_customer_workflow_state() -> None:
    st.session_state.pop("active_ticket_id", None)
    clear_frozen_request(st.session_state)
    clear_frozen_actions(st.session_state)
    clear_editor_state(st.session_state)
    st.session_state.pop(RECOVERY_NOTICE_SESSION_KEY, None)
    st.session_state.pop(PENDING_UI_REQUEST_SESSION_KEY, None)
    st.session_state.pop("pending_prompt", None)


def _operator_token_fingerprint(token: str) -> str:
    return hmac.new(
        token.encode("utf-8"),
        b"keyguard-operator-session-v1",
        hashlib.sha256,
    ).hexdigest()


def _operator_is_authorized() -> bool:
    configured_token = _runtime_secret("KEYGUARD_OPERATOR_TOKEN")
    proof = st.session_state.get(OPERATOR_AUTH_SESSION_KEY)
    if not configured_token or not isinstance(proof, str):
        return False
    expected = _operator_token_fingerprint(configured_token)
    return hmac.compare_digest(proof, expected)


def _escape_markdown_text(value: str) -> str:
    return "".join(f"\\{char}" if char in _MARKDOWN_SPECIALS else char for char in value)


def _escape_markdown_url(value: str) -> str:
    return quote(value, safe=":/?#@!$&'*+,;=%")

def _load_provider_runtime_config(provider: str) -> dict:
    """只读取当前 Provider 的 Secrets，避免泄漏或无关配置告警。"""
    provider = normalize_provider(provider)
    config = {
        "provider": provider,
        "dashscope_key": None,
        "mimo_key": None,
        "mimo_base_url": DEFAULT_MIMO_BASE_URL,
        "mimo_model_name": DEFAULT_MIMO_CHAT_MODEL,
        "deepseek_key": None,
        "deepseek_base_url": DEFAULT_DEEPSEEK_BASE_URL,
        "deepseek_model_name": DEFAULT_DEEPSEEK_CHAT_MODEL,
        "deepseek_thinking": DEFAULT_DEEPSEEK_THINKING,
    }
    if provider == "deepseek":
        config.update(
            deepseek_key=_runtime_secret("DEEPSEEK_API_KEY"),
            deepseek_base_url=(
                _runtime_secret("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL
            ),
            deepseek_model_name=(
                _runtime_secret("DEEPSEEK_CHAT_MODEL")
                or DEFAULT_DEEPSEEK_CHAT_MODEL
            ),
            deepseek_thinking=(
                _runtime_secret("DEEPSEEK_THINKING") or DEFAULT_DEEPSEEK_THINKING
            ),
        )
    elif provider == "mimo":
        config.update(
            mimo_key=_runtime_secret("MIMO_API_KEY"),
            mimo_base_url=(
                _runtime_secret("MIMO_BASE_URL") or DEFAULT_MIMO_BASE_URL
            ),
            mimo_model_name=(
                _runtime_secret("MIMO_CHAT_MODEL") or DEFAULT_MIMO_CHAT_MODEL
            ),
        )
    elif provider == "dashscope":
        config["dashscope_key"] = _runtime_secret("DASHSCOPE_API_KEY")
    return config


provider_runtime = _load_provider_runtime_config(
    (_runtime_secret("CHAT_PROVIDER") or "deepseek").strip().lower()
)
selected_provider = provider_runtime["provider"]
dashscope_key = provider_runtime["dashscope_key"]
mimo_key = provider_runtime["mimo_key"]
mimo_base_url = provider_runtime["mimo_base_url"]
mimo_model_name = provider_runtime["mimo_model_name"]
deepseek_key = provider_runtime["deepseek_key"]
deepseek_base_url = provider_runtime["deepseek_base_url"]
deepseek_model_name = provider_runtime["deepseek_model_name"]
deepseek_thinking = provider_runtime["deepseek_thinking"]

if selected_provider == "deepseek":
    provider_label = "DeepSeek"
    selected_model_name = deepseek_model_name
elif selected_provider == "mimo":
    provider_label = "MiMo"
    selected_model_name = mimo_model_name
elif selected_provider == "dashscope":
    provider_label = "DashScope"
    selected_model_name = rag_conf["chat_model_name"]

with st.sidebar:
    st.caption(f"模型：{provider_label} · `{selected_model_name}`")
    st.divider()


def resolve_chat_config() -> tuple[dict, str]:
    """从环境变量解析当前应使用的 chat 模型配置。"""
    config = build_chat_config(
        provider=selected_provider,
        dashscope_key=dashscope_key,
        mimo_key=mimo_key,
        mimo_base_url=mimo_base_url,
        mimo_model_name=mimo_model_name,
        deepseek_key=deepseek_key,
        deepseek_base_url=deepseek_base_url,
        deepseek_model_name=deepseek_model_name,
        deepseek_thinking=deepseek_thinking,
    )
    return config.kwargs, config.signature


@st.cache_resource(show_spinner="📚 首次启动正在初始化硬件钱包知识库（约 30-60 秒）...")
def _ensure_knowledge_base_loaded() -> bool:
    """首次部署时自动把 data/ 下的文档加载进 Chroma。"""
    from rag.vector_store import VectorStoreService

    VectorStoreService().load_document()
    return True


DEFAULT_PROFILES = {
    "1001": UserProfile(
        user_id="1001",
        experience_level="新手",
        region="深圳",
        device_model="KeyGuard Mini",
        preferred_chains="BTC, ETH",
        connection_method="USB-C",
        passphrase_enabled=False,
        backup_verified=True,
    ),
    "1002": UserProfile(
        user_id="1002",
        experience_level="进阶",
        region="上海",
        device_model="KeyGuard Pro",
        preferred_chains="ETH, Polygon, Arbitrum",
        connection_method="蓝牙",
        passphrase_enabled=True,
        backup_verified=True,
    ),
    "1003": UserProfile(
        user_id="1003",
        experience_level="新手",
        region="杭州",
        device_model="KeyGuard Mini",
        preferred_chains="USDT-TRC20, ETH",
        connection_method="USB-C",
        passphrase_enabled=False,
        backup_verified=False,
    ),
    "1004": UserProfile(
        user_id="1004",
        experience_level="资深",
        region="北京",
        device_model="KeyGuard Max",
        preferred_chains="BTC, ETH, SOL",
        connection_method="USB-C",
        passphrase_enabled=True,
        backup_verified=True,
    ),
    "1005": UserProfile(
        user_id="1005",
        experience_level="进阶",
        region="成都",
        device_model="KeyGuard Pro",
        preferred_chains="ETH, BTC, SOL",
        connection_method="蓝牙",
        passphrase_enabled=True,
        backup_verified=True,
    ),
}


@st.cache_resource(show_spinner=False)
def _init_default_profiles() -> bool:
    """预填测试用户档案到 SQLite，进程级只跑一次。"""
    from database.profile_db import ProfileDatabase

    db = ProfileDatabase()
    for uid, profile in DEFAULT_PROFILES.items():
        existing = db.get_profile(uid)
        if existing is None:
            db.save_profile(profile)
    return True


PREBUILT_HISTORY_1005 = [
    {
        "role": "user",
        "content": "KeyGuard Pro 蓝牙连接不上手机，App 一直显示未发现设备，怎么排查？",
    },
    {
        "role": "assistant",
        "content": "可以按这个顺序排查：\n\n1. **确认设备已解锁并停留在主页**，锁屏状态下 App 可能无法完成配对。\n2. **删除旧配对记录**，在手机系统蓝牙设置和 KeyGuard App 内都移除旧设备后重新配对。\n3. **检查系统权限**，确认 App 已获得蓝牙和附近设备权限。\n4. **升级 App 与固件**，旧版本可能存在连接兼容问题。\n5. **改用 USB-C 交叉验证**，如果 USB-C 正常，问题更可能在蓝牙模块或手机权限。\n\n安全提醒：连接排查过程中不要在手机或网页里输入助记词、私钥或 PIN。\n\n📚 参考来源：故障排除.txt、安全使用指南.txt",
    },
    {
        "role": "user",
        "content": "如果我怀疑助记词被别人看到了，还能继续用这个钱包吗？",
    },
    {
        "role": "assistant",
        "content": "不建议继续使用这组助记词。助记词疑似泄露时，要按资产已经暴露来处理：\n\n1. **立即创建新钱包**，在安全环境中生成新的助记词并完成离线备份。\n2. **把旧钱包资产转移到新地址**，优先转移价值较高和流动性高的资产。\n3. **撤销旧钱包授权**，检查 DeFi、NFT 市场和跨链桥的长期授权。\n4. **废弃旧助记词**，不要再接收新资产到旧地址。\n\n客服不会、也不能要求你提供助记词、私钥或 PIN。\n\n📚 参考来源：助记词与备份.txt、安全使用指南.txt",
    },
    {
        "role": "user",
        "content": "设备今天突然开不了机，屏幕完全不亮，是不是硬件坏了？",
    },
    {
        "role": "assistant",
        "content": "先不要判断为设备损坏，可以按“供电、线缆、端口、设备状态”逐步排查：\n\n1. **换一根支持数据传输的 USB-C 线**，不要只用充电线。\n2. **换电脑 USB 端口或 USB 电源**，优先直连，不经过扩展坞。\n3. **保持连接一段时间后再长按电源键**，排除长期未使用导致的低电量。\n4. **观察是否只有某根线或某个端口无法唤醒**，如果是，优先判断为线缆或供电问题。\n5. **多线、多端口、多电源都无响应**，再记录型号和现象进入售后检测。\n\n安全提醒：不要拆机、加热、电池改装，也不要安装第三方“维修工具”。\n\n📚 参考来源：故障排除.txt",
    },
    {
        "role": "user",
        "content": "如果 USB-C 连接后反复断开重连，要怎么判断问题在哪？",
    },
    {
        "role": "assistant",
        "content": "可以用交叉验证把问题缩小：\n\n1. **先换高质量数据线**，很多硬件钱包亮屏但无法稳定传输，是因为线只适合充电或接触不稳。\n2. **直连电脑 USB 端口**，暂时不要用 USB Hub、扩展坞和过长延长线。\n3. **换另一台电脑测试**，如果只在一台电脑断连，重点排查系统权限、驱动、杀毒软件或 USB 端口。\n4. **检查设备 USB-C 接口是否松动或有异物**，不要用尖锐金属工具清理。\n5. **记录 App 版本、系统版本、设备型号和断连时机**，多设备复现时再进入售后检测。\n\n📚 参考来源：故障排除.txt",
    },
    {
        "role": "user",
        "content": "固件升级前要注意什么？",
    },
    {
        "role": "assistant",
        "content": "固件升级前建议先做这几件事：\n\n1. **确认助记词已完成离线备份和恢复验证**。\n2. **只使用官方 App 或官网入口升级**，不要安装第三方工具提供的固件。\n3. **保证连接稳定**，USB-C 用户建议使用原装数据线，蓝牙用户保持设备电量充足。\n4. **升级前暂停交易操作**，避免升级和签名同时进行。\n5. **升级后做小额验证**，确认地址、签名和常用链账户正常。\n\n安全边界：固件升级不需要输入助记词，任何要求你输入助记词的升级页面都应视为风险。\n\n📚 参考来源：固件升级.txt、安全使用指南.txt",
    },
    {
        "role": "user",
        "content": "帮我生成本月安全使用报告。",
    },
    {
        "role": "assistant",
        "content": "## 📊 赵先生 2026-06 硬件钱包安全使用报告\n\n### 基本信息\n- **用户**：赵先生（成都）\n- **设备**：KeyGuard Pro\n- **常用链**：ETH, BTC, SOL\n- **连接方式**：蓝牙\n\n### 使用概况\n- **签名次数**：26 次/月\n- **主要操作**：ETH 转账、SOL 收款、DeFi 授权确认\n- **失败交易**：1 笔，疑似 Gas 设置偏低\n\n### 安全状态\n- **固件状态**：已是最新版\n- **助记词备份**：已完成验证\n- **Passphrase**：已开启\n- **高风险授权**：发现 1 个长期授权，建议复核\n\n### 建议\n1. 保持固件和 App 同步更新。\n2. 每月复查一次 DeFi 授权，撤销不用的授权。\n3. 大额转账继续坚持先小额测试。\n4. 蓝牙连接异常时优先使用 USB-C 做交叉验证。",
    },
]


def get_or_build_agent() -> ReactAgent | None:
    kwargs, sig = resolve_chat_config()
    if sig == "default":
        return None

    cached_sig = st.session_state.get("agent_config_sig")
    if "agent" in st.session_state and cached_sig == sig:
        return st.session_state["agent"]

    try:
        chat_model = build_chat_model(**kwargs)
        configure_rag_model(chat_model)
        _ensure_knowledge_base_loaded()
        st.session_state["agent"] = ReactAgent(chat_model=chat_model)
        st.session_state["agent_config_sig"] = sig
        logger.info("[app]V1 兼容 Agent 已初始化")
        return st.session_state["agent"]
    except Exception as error:
        logger.error(
            "[app]V1 初始化失败 stage=builder error_type=%s",
            type(error).__name__,
        )
        st.error(SAFE_FAILURE_NOTICE)
        return None


@st.cache_resource(show_spinner=False)
def get_or_build_orchestrator(
    model_signature: str,
    contract_version: str,
) -> SupportOrchestrator | None:
    """构建 V2 Runtime；安全策略对象在 Graph 与 Runtime 间按身份共享。"""
    if contract_version != ORCHESTRATOR_CONTRACT_VERSION:
        raise ValueError("编排契约版本不匹配")
    kwargs, signature = resolve_chat_config()
    if signature == "default" or signature != model_signature:
        return None

    ticket_db_path = _runtime_secret("KEYGUARD_TICKET_DB") or "data/keyguard_v2.db"
    checkpoint_db_path = (
        _runtime_secret("KEYGUARD_CHECKPOINT_DB")
        or "data/keyguard_v2_checkpoints.sqlite3"
    )
    if Path(ticket_db_path).resolve() == Path(checkpoint_db_path).resolve():
        raise ValueError("工单数据库与 checkpoint 数据库必须使用不同路径")

    chat_model = build_chat_model(**kwargs)
    configure_rag_model(chat_model)
    _ensure_knowledge_base_loaded()
    policy = load_security_policy()
    orchestration = load_orchestration_config()
    trusted_sources = TrustedSourcePolicy(
        policy.get("trusted_evidence_domains"),
        policy.get("trusted_evidence_url_prefixes"),
    )
    policy_guard = PolicyGuard(policy, trusted_source_policy=trusted_sources)
    repository = TicketRepository(ticket_db_path)
    checkpointer = sqlite_checkpointer(checkpoint_db_path)
    try:
        graph = build_configured_graph(
            chat_model,
            policy,
            orchestration,
            checkpointer,
            trusted_source_policy=trusted_sources,
            policy_guard=policy_guard,
        )
        return SupportOrchestrator(
            repository=repository,
            graph=graph,
            ingress_guard=IngressGuard(policy),
            recursion_limit=orchestration["recursion_limit"],
            lease_seconds=orchestration["command_lease_seconds"],
            graph_timeout_seconds=orchestration["timeouts"]["graph_seconds"],
            trusted_source_policy=trusted_sources,
            final_policy_guard=policy_guard,
        )
    except Exception:
        checkpointer.close()
        raise


USER_OPTIONS = {
    "1001 - 张先生（深圳）": ("1001", "深圳"),
    "1002 - 李女士（上海）": ("1002", "上海"),
    "1003 - 王女士（杭州）": ("1003", "杭州"),
    "1004 - 陈先生（北京）": ("1004", "北京"),
    "1005 - 赵先生（成都） · 展示记忆功能": ("1005", "成都"),
}
MEMORY_DEMO_USER_ID = "1005"
MEMORY_DEMO_SUMMARY = """赵先生此前咨询过蓝牙连接失败、助记词疑似泄露、设备开不了机、USB-C 断连、固件升级前检查和月度安全报告。

当前记忆要点：
- 设备：KeyGuard Pro，常用蓝牙连接，连接异常时可用 USB-C 交叉验证。
- 安全：Passphrase 已开启，备份已验证，需要定期复核高风险授权。
- 这段摘要只用于解释记忆机制，不会作为聊天回答展示。"""

EXPERIENCE_LEVELS = ["新手", "进阶", "资深"]
CONNECTION_METHODS = ["USB-C", "蓝牙", "USB-C + 蓝牙"]

with st.sidebar:
    _init_default_profiles()

    st.header("👤 测试用户")
    selected = st.selectbox("当前登录用户", list(USER_OPTIONS.keys()), key="user_select")
    uid, loc = USER_OPTIONS[selected]
    set_user_id(uid)
    set_location(loc)
    st.caption(f"用户 ID `{uid}` · 地区 `{loc}`")
    if uid == MEMORY_DEMO_USER_ID:
        st.info("展示记忆功能：已预置 6 轮历史对话，继续提问会触发对话压缩。", icon="🧠")

    if st.session_state.get("active_user") != selected:
        st.session_state["active_user"] = selected
        st.session_state["messages"] = list(PREBUILT_HISTORY_1005) if uid == MEMORY_DEMO_USER_ID else []
        _clear_customer_workflow_state()

    if st.button("🗑️ 清空对话", use_container_width=True):
        st.session_state["messages"] = []
        _clear_customer_workflow_state()
        st.rerun()

    with st.expander("📝 用户档案（可选）", expanded=False):
        st.caption("填写后可获得个性化安全建议")
        profile_db = ProfileDatabase()
        profile = profile_db.get_profile(uid) or UserProfile(user_id=uid)

        col1, col2 = st.columns(2)
        with col1:
            experience_default = (
                EXPERIENCE_LEVELS.index(profile.experience_level)
                if profile.experience_level in EXPERIENCE_LEVELS
                else 1
            )
            experience_level = st.selectbox(
                "使用经验",
                EXPERIENCE_LEVELS,
                index=experience_default,
                key=f"profile_experience_level_{uid}",
            )
            passphrase_enabled = st.checkbox(
                "已开启 Passphrase",
                value=profile.passphrase_enabled if profile.passphrase_enabled is not None else False,
                key=f"profile_passphrase_enabled_{uid}",
            )
        with col2:
            connection_default = (
                CONNECTION_METHODS.index(profile.connection_method)
                if profile.connection_method in CONNECTION_METHODS
                else 0
            )
            connection_method = st.selectbox(
                "常用连接方式",
                CONNECTION_METHODS,
                index=connection_default,
                key=f"profile_connection_method_{uid}",
            )
            backup_verified = st.checkbox(
                "已完成备份验证",
                value=profile.backup_verified if profile.backup_verified is not None else False,
                key=f"profile_backup_verified_{uid}",
            )

        region = st.text_input(
            "地区",
            value=profile.region or loc,
            key=f"profile_region_{uid}",
            placeholder="例如：深圳",
        )
        device_model = st.text_input(
            "设备型号",
            value=profile.device_model or "",
            key=f"profile_device_{uid}",
            placeholder="例如：KeyGuard Pro",
        )
        preferred_chains = st.text_input(
            "常用链",
            value=profile.preferred_chains or "",
            key=f"profile_chains_{uid}",
            placeholder="例如：BTC, ETH, SOL",
        )

        if st.button(
            "💾 保存档案",
            key=f"save_profile_{uid}",
            use_container_width=True,
        ):
            new_profile = UserProfile(
                user_id=uid,
                experience_level=experience_level,
                region=region if region else None,
                device_model=device_model if device_model else None,
                preferred_chains=preferred_chains if preferred_chains else None,
                connection_method=connection_method,
                passphrase_enabled=passphrase_enabled,
                backup_verified=backup_verified,
            )
            if profile_db.save_profile(new_profile):
                st.success("✅ 档案已保存")
                st.rerun()
            else:
                st.error("❌ 保存失败，请重试")

    with st.expander("🧠 记忆状态", expanded=True):
        _profile = ProfileDatabase().get_profile(uid)
        st.markdown("**📝 用户档案**")
        if _profile:
            _parts = []
            if _profile.experience_level:
                _parts.append(_profile.experience_level)
            if _profile.region:
                _parts.append(_profile.region)
            if _profile.device_model:
                _parts.append(_profile.device_model)
            if _profile.preferred_chains:
                _parts.append(_profile.preferred_chains)
            if _profile.connection_method:
                _parts.append(_profile.connection_method)
            if _profile.passphrase_enabled is not None:
                _parts.append("Passphrase 已开启" if _profile.passphrase_enabled else "Passphrase 未开启")
            if _profile.backup_verified is not None:
                _parts.append("备份已验证" if _profile.backup_verified else "备份未验证")
            st.caption(" · ".join(_parts))
        else:
            st.caption("未填写，可在侧边栏填写以获取个性化安全建议")

        st.markdown("**💬 对话记忆**")
        _messages = st.session_state.get("messages", [])
        _user_turns = sum(1 for m in _messages if m.get("role") == "user")
        _max_turns = 6
        if _user_turns > _max_turns:
            st.caption(f"对话轮数：{_user_turns} 轮 · 已触发压缩（保留最近 {_max_turns} 轮）")
        else:
            st.caption(f"对话轮数：{_user_turns} 轮 · 压缩阈值：{_max_turns} 轮")

    if uid == MEMORY_DEMO_USER_ID:
        with st.expander("🧠 压缩摘要（演示）", expanded=False):
            if _user_turns > _max_turns:
                st.caption(f"状态：已触发压缩，保留最近 {_max_turns} 轮完整对话。")
            elif _user_turns:
                st.caption("状态：预置历史已就绪，继续提问后会触发压缩。")
            else:
                st.caption("状态：对话已清空，重新选择 1005 可恢复预置历史。")
            st.markdown(MEMORY_DEMO_SUMMARY)

    st.divider()
    st.markdown("**💡 试试这些问题：**")
    EXAMPLE_QUESTIONS = [
        ("🔋", "硬件钱包开不了机怎么办？"),
        ("🔵", "蓝牙没法连接手机怎么办？"),
        ("🔌", "电脑识别不到设备，怎么排查？"),
        ("🔄", "固件升级中断了怎么办？"),
        ("🧳", "设备丢了或坏了，资产还能恢复吗？"),
        ("🧩", "Passphrase 忘了，为什么恢复后余额为零？"),
    ]
    for icon, q in EXAMPLE_QUESTIONS:
        if st.button(f"{icon}  {q}", key=f"q_{q[:6]}", use_container_width=True):
            st.session_state["pending_prompt"] = q
            st.rerun()


PHASE_LABELS = {
    "entry_checked": "🛡️ 安全检查完成",
    "triage_completed": "🧭 问题分诊完成",
    "diagnosis_completed": "📚 诊断取证完成",
    "review_completed": "✅ 安全复核完成",
    "escalated": "👩‍💼 已转人工审核",
    "response_finalized": "📨 安全回复已完成",
    "ingress_guard.redacted": "🛡️ 安全检查完成",
    "triage.completed": "🧭 问题分诊完成",
    "diagnosis.evidence_loaded": "📚 诊断取证完成",
    "review.completed": "✅ 安全复核完成",
    "routing.escalated": "👩‍💼 已转人工审核",
}
ASK_USER_FIELDS = [
    "device_model",
    "error_state",
    "serial_last4",
    "transaction_hash",
    "chain_name",
]


def _answer_with_citations(result) -> str:
    answer = result.user_notice or result.final_answer
    if not answer and result.status == "pending_user":
        answer = PENDING_USER_NOTICE
    if not answer and result.status == "escalated":
        answer = ESCALATED_NOTICE
    if not answer:
        answer = "工单已记录，我们会继续安全处理。"
    if result.citations:
        source_lines = [
            "- "
            f"[{_escape_markdown_text(item['source_title'])}]"
            f"({_escape_markdown_url(item['source_url'])})"
            for item in result.citations
        ]
        answer += "\n\n📚 已验证参考来源：\n" + "\n".join(source_lines)
    return answer


def _run_v2_prompt(orchestrator: SupportOrchestrator, prompt: str, user_id: str) -> None:
    active_ticket_id = st.session_state.get("active_ticket_id")
    restoring = False
    request_id = _stable_request_id()

    def _finish_ui_message(content: str) -> None:
        if st.session_state.get(PENDING_UI_REQUEST_SESSION_KEY) == request_id:
            replace_processing_answer(st.session_state["messages"], content)
            st.session_state.pop(PENDING_UI_REQUEST_SESSION_KEY, None)
        else:
            st.session_state["messages"].append(
                {"role": "assistant", "content": content}
            )

    try:
        active_ticket = (
            orchestrator.repository.get_ticket(active_ticket_id)
            if active_ticket_id
            else None
        )
        if active_ticket_id and (
            active_ticket is None or active_ticket.get("user_id") != user_id
        ):
            _clear_customer_workflow_state()
            active_ticket_id = None
            active_ticket = None
        request_route = (
            ("resume_user", active_ticket_id)
            if active_ticket and active_ticket.get("status") == "pending_user"
            else ("submit", None)
        )
        frozen_command, safe_history, restoring = get_or_freeze_request_command(
            st.session_state,
            request_id,
            st.session_state.get("messages", []),
            lambda: (request_route, orchestrator.prepare_user_input(prompt)),
        )
        if (
            not isinstance(frozen_command, tuple)
            or len(frozen_command) != 2
            or not isinstance(frozen_command[0], tuple)
            or len(frozen_command[0]) != 2
        ):
            raise ValueError("冻结请求结构非法")
        frozen_route, prepared = frozen_command
        route_kind, route_ticket_id = frozen_route
        if st.session_state.get(PENDING_UI_REQUEST_SESSION_KEY) != request_id:
            append_processing_turn(
                st.session_state["messages"],
                prepared.sanitized_input,
                PROCESSING_UI_NOTICE,
            )
            st.session_state[PENDING_UI_REQUEST_SESSION_KEY] = request_id
            with st.chat_message("user", avatar="🧑"):
                st.markdown(prepared.sanitized_input)
            with st.chat_message("assistant", avatar="🔐"):
                st.markdown(PROCESSING_UI_NOTICE)

        with st.status("正在安全处理工单…", expanded=True) as processing_status:
            try:
                if route_kind == "resume_user":
                    if route_ticket_id != active_ticket_id:
                        raise ValueError("冻结请求工单不一致")
                    result = orchestrator.resume_user_prepared(
                        route_ticket_id,
                        prepared,
                        request_id=request_id,
                    )
                elif route_kind == "submit" and route_ticket_id is None:
                    result = orchestrator.submit_prepared(
                        prepared,
                        user_id=user_id,
                        request_id=request_id,
                        safe_history=safe_history,
                    )
                else:
                    raise ValueError("冻结请求路由非法")
            except Exception:
                processing_status.update(label="安全处理未完成", state="error")
                raise
            processing_status.update(label="安全处理完成", state="complete")
    except IdempotencyConflictError as error:
        if restoring:
            st.session_state[RECOVERY_NOTICE_SESSION_KEY] = (
                RECOVERING_REQUEST_NOTICE
            )
        logger.error(
            "[app]V2 恢复状态异常 stage=runtime error_type=%s",
            type(error).__name__,
        )
        _finish_ui_message(SAFE_FAILURE_NOTICE)
        st.rerun()
        return
    except (CommandFailedError, CheckpointRestoreError) as error:
        clear_frozen_request(st.session_state)
        st.session_state.pop(RECOVERY_NOTICE_SESSION_KEY, None)
        logger.error(
            "[app]V2 请求终止 stage=runtime error_type=%s",
            type(error).__name__,
        )
        _finish_ui_message(SAFE_FAILURE_NOTICE)
        st.rerun()
        return
    except (CommandInProgressError, TimeoutError) as error:
        if restoring:
            st.session_state[RECOVERY_NOTICE_SESSION_KEY] = (
                RECOVERING_REQUEST_NOTICE
            )
        logger.error(
            "[app]V2 请求待恢复 stage=runtime error_type=%s",
            type(error).__name__,
        )
        _finish_ui_message(SAFE_FAILURE_NOTICE)
        st.rerun()
        return
    except Exception as error:
        if restoring:
            st.session_state[RECOVERY_NOTICE_SESSION_KEY] = (
                RECOVERING_REQUEST_NOTICE
            )
        logger.error(
            "[app]V2 请求失败 stage=runtime error_type=%s",
            type(error).__name__,
        )
        _finish_ui_message(SAFE_FAILURE_NOTICE)
        st.rerun()
        return

    clear_frozen_request(st.session_state)
    if restoring:
        st.session_state[RECOVERY_NOTICE_SESSION_KEY] = RECOVERED_REQUEST_NOTICE
    st.session_state["active_ticket_id"] = result.ticket_id
    safe_result_message = {"role": "user", "content": result.sanitized_input}
    if (
        st.session_state["messages"][-2].get("content")
        != safe_result_message["content"]
    ):
        raise ValueError("安全结果与已展示问题不一致")
    replace_processing_answer(
        st.session_state["messages"], _answer_with_citations(result)
    )
    st.session_state.pop(PENDING_UI_REQUEST_SESSION_KEY, None)
    st.rerun()


def _run_v1_prompt(
    agent: ReactAgent,
    ingress_guard: IngressGuard,
    prompt: str,
    user_id: str,
    location: str,
) -> None:
    """显式 v1 兼容路径；输入仍先脱敏，且不展示内部流事件。"""
    try:
        sanitized = ingress_guard.sanitize(prompt)
        safe_prompt = sanitized.sanitized_input
        if sanitized.critical_notice:
            answer_text = sanitized.critical_notice
        else:
            active_profile = ProfileDatabase().get_profile(user_id)
            answer_text = "".join(
                content
                for event_name, content in agent.execute_stream(
                    safe_prompt,
                    st.session_state.get("messages", []),
                    user_id=user_id,
                    location=location,
                    user_profile=active_profile,
                )
                if event_name == "answer"
            )
            if not answer_text:
                answer_text = SAFE_FAILURE_NOTICE
    except Exception as error:
        logger.error(
            "[app]V1 请求失败 stage=compat_runtime error_type=%s",
            type(error).__name__,
        )
        st.session_state["messages"].append(
            {"role": "assistant", "content": SAFE_FAILURE_NOTICE}
        )
        st.rerun()
        return

    st.session_state["messages"].append({"role": "user", "content": safe_prompt})
    st.session_state["messages"].append(
        {"role": "assistant", "content": answer_text}
    )
    st.rerun()


def _run_human_action(
    orchestrator: SupportOrchestrator,
    ticket_id: str,
    action: str,
    *,
    edited_answer: str = "",
    missing_fields: Optional[list[str]] = None,
) -> None:
    if not _operator_is_authorized():
        st.error("工单工作台未授权，操作已拒绝。")
        return
    action_key, action_id = _stable_action_id(ticket_id, action)
    try:
        prepared, restoring = get_or_freeze_action_command(
            st.session_state,
            action_key,
            action_id,
            lambda: orchestrator.prepare_human_action(
                action,
                edited_answer=edited_answer,
                missing_fields=missing_fields,
            ),
        )
        if restoring:
            st.info(RECOVERING_REQUEST_NOTICE)
        orchestrator.human_action_prepared(
            ticket_id,
            prepared,
            action_id=action_id,
        )
    except IdempotencyConflictError as error:
        logger.error(
            "[app]人工操作恢复状态异常 stage=human_action error_type=%s",
            type(error).__name__,
        )
        st.error(SAFE_FAILURE_NOTICE)
        return
    except (CommandFailedError, CheckpointRestoreError) as error:
        clear_frozen_action(st.session_state, action_key)
        logger.error(
            "[app]人工操作终止 stage=human_action error_type=%s",
            type(error).__name__,
        )
        st.error(SAFE_FAILURE_NOTICE)
        return
    except (CommandInProgressError, TimeoutError) as error:
        logger.error(
            "[app]人工操作待恢复 stage=human_action error_type=%s",
            type(error).__name__,
        )
        st.error(SAFE_FAILURE_NOTICE)
        return
    except Exception as error:
        logger.error(
            "[app]人工操作失败 stage=human_action error_type=%s",
            type(error).__name__,
        )
        st.error(SAFE_FAILURE_NOTICE)
        return
    clear_frozen_action(st.session_state, action_key)
    st.success("操作已安全提交。")
    st.rerun()


def _render_active_ticket(
    orchestrator: SupportOrchestrator,
    user_id: str,
) -> None:
    ticket_id = st.session_state.get("active_ticket_id")
    if not ticket_id:
        return
    try:
        ticket = orchestrator.repository.get_ticket(ticket_id)
        if ticket is None or ticket.get("user_id") != user_id:
            _clear_customer_workflow_state()
            return
        events = orchestrator.repository.list_events(ticket_id)
    except Exception as error:
        logger.error(
            "[app]活动工单读取失败 stage=customer_view error_type=%s",
            type(error).__name__,
        )
        st.warning(SAFE_FAILURE_NOTICE)
        return
    st.caption(
        f"当前工单 `{ticket_id}` · 状态 `{ticket.get('status') or '-'}` · "
        f"优先级 `{ticket.get('priority') or 'P2'}` · 风险 `{ticket.get('risk_level') or '-'}`"
    )
    completed_phases = project_customer_phases(events)
    if completed_phases:
        st.caption(" · ".join(item["label"] for item in completed_phases))


def _render_operator_gate() -> bool:
    configured_token = _runtime_secret("KEYGUARD_OPERATOR_TOKEN")
    if not configured_token:
        st.session_state.pop(OPERATOR_AUTH_SESSION_KEY, None)
        clear_frozen_actions(st.session_state)
        clear_editor_state(st.session_state)
        st.warning("工单工作台未启用：服务端未配置独立操作员令牌。")
        return False

    if _operator_is_authorized():
        st.success("操作员已授权。")
        if st.button("退出工作台", key="operator_logout"):
            st.session_state.pop(OPERATOR_AUTH_SESSION_KEY, None)
            clear_frozen_actions(st.session_state)
            clear_editor_state(st.session_state)
            st.rerun()
        return True

    with st.form("operator_login", clear_on_submit=True):
        candidate = st.text_input(
            "操作员令牌",
            type="password",
            autocomplete="off",
        )
        submitted = st.form_submit_button("进入工作台")
    if submitted:
        token_matches = bool(candidate) and hmac.compare_digest(
            candidate.encode("utf-8"),
            configured_token.encode("utf-8"),
        )
        if token_matches:
            st.session_state[OPERATOR_AUTH_SESSION_KEY] = (
                _operator_token_fingerprint(configured_token)
            )
            st.rerun()
        else:
            st.error("操作员令牌无效。")
    return False


def _render_workbench(orchestrator: SupportOrchestrator) -> None:
    if not _operator_is_authorized():
        st.error("工单工作台未授权。")
        return
    try:
        tickets = orchestrator.repository.list_workbench_tickets()
    except Exception as error:
        logger.error(
            "[app]工单队列读取失败 stage=workbench error_type=%s",
            type(error).__name__,
        )
        st.error(SAFE_FAILURE_NOTICE)
        return

    if not tickets:
        st.info("暂无工单。先在“客户对话”中提交一个问题。")
        return

    for ticket in tickets:
        ticket_id = ticket["ticket_id"]
        label = (
            f"{ticket.get('priority') or 'P2'} · {ticket_id} · "
            f"{ticket.get('status') or '-'}"
        )
        with st.expander(label):
            st.text(ticket.get("summary") or ticket.get("sanitized_input") or "-")
            st.caption(
                f"类别 `{ticket.get('category') or '-'}` · "
                f"优先级 `{ticket.get('priority') or 'P2'}` · "
                f"风险 `{ticket.get('risk_level') or '-'}`"
            )
            try:
                events = orchestrator.repository.list_events(ticket_id)
                for event in project_workbench_events(events):
                    event_type = event["event_type"]
                    summary = event["summary"]
                    from_status = event["from_status"]
                    to_status = event["to_status"]
                    st.text(
                        f"{event['label']} · {event['node_name']} · "
                        f"{event_type} · {summary} · "
                        f"{from_status} → {to_status}"
                    )
                citations = orchestrator.get_verified_citations(ticket_id)
            except Exception as error:
                logger.error(
                    "[app]工单详情读取失败 stage=workbench_detail error_type=%s",
                    type(error).__name__,
                )
                st.warning(SAFE_FAILURE_NOTICE)
                citations = []
            for citation in citations:
                st.link_button(
                    f"📚 {_escape_markdown_text(citation['source_title'])}",
                    _escape_markdown_url(citation["source_url"]),
                )

            if ticket.get("status") != "escalated":
                continue

            st.caption("人工审核区不会展示模型内部提示、工具参数或未审核的原始输出。")
            draft_answer = ticket.get("draft_answer")
            if draft_answer:
                st.markdown("**Repository 安全投影草稿**")
                st.text(draft_answer)
            missing_fields = st.multiselect(
                "需要客户补充的字段",
                ASK_USER_FIELDS,
                default=["error_state"],
                key=f"missing_{ticket_id}",
            )
            approve_col, edit_col = st.columns(2)
            with approve_col:
                if st.button(
                    "Approve",
                    key=f"approve_{ticket_id}",
                    disabled=not bool(draft_answer),
                    use_container_width=True,
                ):
                    _run_human_action(orchestrator, ticket_id, "approve")
            with edit_col:
                with st.form(
                    f"edit_send_form_{ticket_id}",
                    clear_on_submit=True,
                    border=False,
                ):
                    edited = st.text_area(
                        "编辑后回复",
                        value=draft_answer or "",
                        placeholder="输入审核后可直接发送给客户的安全回复",
                        key=f"{EDITOR_SESSION_PREFIX}{ticket_id}",
                    )
                    edit_submitted = st.form_submit_button(
                        "Edit & Send",
                        disabled=not bool(edited.strip()),
                        use_container_width=True,
                    )
                if edit_submitted:
                    _run_human_action(
                        orchestrator,
                        ticket_id,
                        "edit_send",
                        edited_answer=edited,
                    )
            ask_col, reject_col = st.columns(2)
            with ask_col:
                if st.button(
                    "Ask User",
                    key=f"ask_{ticket_id}",
                    disabled=not bool(missing_fields),
                    use_container_width=True,
                ):
                    _run_human_action(
                        orchestrator,
                        ticket_id,
                        "ask_user",
                        missing_fields=missing_fields,
                    )
            with reject_col:
                if st.button(
                    "Reject",
                    key=f"reject_{ticket_id}",
                    use_container_width=True,
                ):
                    _run_human_action(orchestrator, ticket_id, "reject")


if "messages" not in st.session_state:
    st.session_state["messages"] = []

orchestrator = None
agent = None
v1_ingress_guard = None
if USE_V1_AGENT:
    agent = get_or_build_agent()
    try:
        v1_ingress_guard = IngressGuard(load_security_policy())
    except Exception as error:
        logger.error(
            "[app]V1 安全配置失败 stage=compat_builder error_type=%s",
            type(error).__name__,
        )
        st.error(SAFE_FAILURE_NOTICE)
else:
    _, current_model_signature = resolve_chat_config()
    try:
        orchestrator = get_or_build_orchestrator(
            current_model_signature, ORCHESTRATOR_CONTRACT_VERSION
        )
    except Exception as error:
        logger.error(
            "[app]V2 初始化失败 stage=builder error_type=%s",
            type(error).__name__,
        )
        st.error(SAFE_FAILURE_NOTICE)

customer_tab, workbench_tab = st.tabs(["客户对话", "工单工作台"])

with customer_tab:
    recovery_notice = st.session_state.pop(RECOVERY_NOTICE_SESSION_KEY, None)
    if recovery_notice in {RECOVERING_REQUEST_NOTICE, RECOVERED_REQUEST_NOTICE}:
        st.info(recovery_notice)
    if not st.session_state["messages"]:
        with st.chat_message("assistant", avatar="🔐"):
            st.markdown(
                f"""你好，我是 **KeyGuard 硬件钱包智能客服**。我可以帮你：

- 🔋 **设备故障**：开不了机、屏幕不亮、按键卡死、设备锁定
- 🔌 **连接排查**：USB-C、蓝牙、App 识别不到设备等问题
- 🔄 **固件修复**：升级中断、恢复模式、固件验证失败
- 🛡️ **备份安全**：助记词备份、设备丢失、恢复钱包
- ⛓️ **交易边界**：签名后 pending、手续费和链网络状态的区分
- 📊 **安全报告**：基于模拟使用记录生成月度风险建议

> 当前登录身份：**{selected}**，切换用户会自动开新对话。"""
            )

    for message in st.session_state["messages"]:
        avatar = "🔐" if message["role"] == "assistant" else "🧑"
        with st.chat_message(message["role"], avatar=avatar):
            st.markdown(message["content"])

    if orchestrator is not None:
        _render_active_ticket(orchestrator, uid)

    prompt = st.chat_input(
        "输入你的问题，例如：硬件钱包开不了机或蓝牙连不上怎么办？"
    )
    if not prompt and st.session_state.get("pending_prompt"):
        prompt = st.session_state["pending_prompt"]
        st.session_state["pending_prompt"] = None

    if prompt:
        if USE_V1_AGENT:
            if agent is None or v1_ingress_guard is None:
                st.error(SAFE_FAILURE_NOTICE)
            else:
                _run_v1_prompt(agent, v1_ingress_guard, prompt, uid, loc)
        elif orchestrator is None:
            st.error(SAFE_FAILURE_NOTICE)
        else:
            _run_v2_prompt(orchestrator, prompt, uid)

with workbench_tab:
    if USE_V1_AGENT:
        st.info("当前为显式 V1 兼容模式；工单工作台仅在 V2 Runtime 中启用。")
    elif orchestrator is None:
        st.error(SAFE_FAILURE_NOTICE)
    elif _render_operator_gate():
        _render_workbench(orchestrator)
