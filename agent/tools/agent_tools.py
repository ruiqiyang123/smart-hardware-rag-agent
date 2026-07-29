import os
import threading
from datetime import datetime
from typing import Optional

import requests
from utils.logger_handler import logger
from langchain_core.tools import tool

from rag.rag_service import RagSummarizeService
from utils.config_handler import agent_conf
from utils.path_tool import get_abs_path
from utils.session_context import current_user_id, current_location
from agent.services.chain_status_service import fetch_chain_status
from agent.services.usage_records import find_usage_record, load_usage_records

rag = None
_evidence_rag = None
_rag_lock = threading.RLock()

# 会话级用户上下文：替代原 random.choice(user_ids)
# 在 Streamlit 前端通过 session_state 设置当前登录用户，见 app.py 侧边栏

# 模块级缓存：避免每次调用 fetch_external_data 都重读 CSV。
# 纯解析逻辑已抽到 agent.services.usage_records，这里只保留缓存 + 委托。
external_data = {}
external_data_mtime = None


def configure_rag_model(model):
    """按会话重置 RAG 总结服务使用的 chat 模型。

    Web 前端切换 provider / API Key 后调用本函数，让 rag_summarize 工具与
    ReactAgent 共用同一会话模型，避免「主对话用 MiMo、RAG 总结却走 DashScope
    且无 key 报错」的割裂。
    """
    global rag
    with _rag_lock:
        rag = RagSummarizeService(model=model)


def _summary_rag() -> RagSummarizeService:
    global rag
    instance = rag
    if instance is None:
        with _rag_lock:
            if rag is None:
                rag = RagSummarizeService()
            instance = rag
    return instance


def _structured_evidence_rag() -> RagSummarizeService:
    global _evidence_rag
    instance = _evidence_rag
    if instance is None:
        with _rag_lock:
            if _evidence_rag is None:
                _evidence_rag = RagSummarizeService(evidence_only=True)
            instance = _evidence_rag
    return instance


def resolve_location_or_ip(location: Optional[str] = None) -> str:
    """优先返回会话地区，缺省时用 IP 粗定位兜底。"""
    if location:
        return location

    # 兜底：IP 定位（带重试，覆盖瞬时网络抖动）
    try:
        for attempt in range(3):
            try:
                resp = requests.get("http://ip-api.com/json/", params={"lang": "zh"}, timeout=5)
                resp.raise_for_status()
                data = resp.json()
                city = data.get("city") or data.get("regionName") or "未知"
                logger.info(f"[get_user_location]IP定位城市：{city}")
                return city
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt < 2:
                    logger.info(f"[get_user_location]IP定位第{attempt + 1}次超时，重试中...")
                    continue
                raise
    except Exception as e:
        logger.warning(f"[get_user_location]IP定位失败：{e}，使用默认城市")
        return "深圳"


@tool(description="从向量存储中检索硬件钱包相关的参考资料，返回包含答案与引用来源的结构化内容")
def rag_summarize(query: str) -> str:
    return _summary_rag().rag_summarize(query)


@tool(description="只读检索硬件钱包售后知识证据，返回结构化证据列表；不会调用总结模型")
def search_support_evidence(query: str) -> list[dict]:
    """V2 knowledge search. It never invokes the RAG summary LLM."""
    return _structured_evidence_rag().search_evidence(query)


@tool(description="获取指定区块链网络的模拟状态信息（网络拥堵、手续费区间、预计确认时间），用于演示交易 pending 与硬件签名边界。返回纯字符串")
def get_chain_status(chain: str) -> str:
    """获取模拟区块链网络状态，帮助用户理解交易确认与硬件设备的边界。

    支持的链：BTC/Bitcoin/比特币、ETH/Ethereum/以太坊、SOL/Solana、BSC/BNB、
    TRX/TRON/波场、ARB/Arbitrum、MATIC/Polygon。
    返回模拟网络拥堵程度、手续费区间、预计确认时间和客服建议。
    """
    return fetch_chain_status(chain)


@tool(description="获取当前用户所在地区，返回纯字符串地区名。优先使用前端设置的会话地区，兜底用 IP 粗定位")
def get_user_location() -> str:
    """获取当前用户所在地区，用于地区相关的个性化建议。"""
    return resolve_location_or_ip(current_location())


@tool(description="获取当前登录用户的ID，返回纯字符串。ID 来自前端会话上下文")
def get_user_id() -> str:
    """获取当前会话用户 ID，用于查询使用记录和生成报告。"""
    uid = current_user_id()
    logger.info(f"[get_user_id]当前会话用户ID：{uid}")
    return uid


@tool(description="获取系统当前月份，格式 YYYY-MM（如 2025-06），返回纯字符串")
def get_current_month() -> str:
    """获取系统当前月份，用于查询当月使用记录。"""
    now = datetime.now()
    month_str = now.strftime("%Y-%m")
    logger.info(f"[get_current_month]系统当前月份：{month_str}")
    return month_str


def generate_external_data():
    """加载外部使用记录数据到模块级缓存。

    纯解析逻辑委托给 agent.services.usage_records.load_usage_records。
    如果 CSV 文件发生变更，按 mtime 自动刷新缓存，避免本地演示时必须重启应用。
    """
    global external_data, external_data_mtime
    external_data_path = get_abs_path(agent_conf["external_data_path"])
    current_mtime = os.path.getmtime(external_data_path)
    if not external_data or external_data_mtime != current_mtime:
        external_data = load_usage_records(external_data_path)
        external_data_mtime = current_mtime


@tool(description="从外部系统中获取指定用户在指定月份的硬件钱包使用记录，以纯字符串形式返回。未检索到返回空字符串")
def fetch_external_data(user_id: str, month: str) -> str:
    generate_external_data()
    return find_usage_record(external_data, user_id, month, fallback_to_latest=True)


@tool(description="无入参，调用后触发中间件自动为报告生成场景动态注入上下文信息，为后续提示词切换提供上下文信息")
def fill_context_for_report():
    return "fill_context_for_report已调用"
