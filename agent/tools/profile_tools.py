"""
用户档案相关工具：让 Agent 能访问硬件钱包用户档案信息

通过 get_user_profile 工具，Agent 可以获取用户的经验等级、地区、设备型号、
常用链、连接方式、Passphrase 状态和备份验证状态，用于个性化安全建议。
"""

from typing import Optional

from langchain_core.tools import tool
from utils.user_profile import current_profile
from database.profile_db import UserProfile


def format_user_profile(profile: Optional[UserProfile]) -> str:
    """格式化硬件钱包用户档案，供工具和会话快照复用。"""
    if not profile:
        return "当前用户未填写档案信息，无法提供个性化建议"

    parts = []
    if profile.experience_level:
        parts.append(f"经验等级：{profile.experience_level}")
    if profile.region:
        parts.append(f"地区：{profile.region}")
    if profile.device_model:
        parts.append(f"设备型号：{profile.device_model}")
    if profile.preferred_chains:
        parts.append(f"常用链：{profile.preferred_chains}")
    if profile.connection_method:
        parts.append(f"连接方式：{profile.connection_method}")
    if profile.passphrase_enabled is not None:
        parts.append(f"Passphrase：{'已开启' if profile.passphrase_enabled else '未开启'}")
    if profile.backup_verified is not None:
        parts.append(f"备份验证：{'已完成' if profile.backup_verified else '未完成'}")

    return "；".join(parts) if parts else "用户档案未填写详细信息"


@tool(description="获取当前用户的档案信息（经验等级、地区、设备型号、常用链、连接方式、是否开启 Passphrase、是否完成备份验证），返回结构化字符串")
def get_user_profile() -> str:
    """获取当前硬件钱包用户档案，用于个性化安全建议

    当用户询问设备使用建议、安全配置、备份策略等个性化问题时，调用此工具获取用户档案。

    Returns:
        用户档案的结构化描述，如"经验等级：进阶；地区：深圳；设备型号：KeyGuard Pro；常用链：BTC, ETH, SOL；连接方式：USB-C；Passphrase：已开启；备份验证：已完成"
        如果用户未填写档案，返回"当前用户未填写档案信息"
    """
    return format_user_profile(current_profile())
