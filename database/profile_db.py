"""
用户档案 SQLite 数据库服务

存储硬件钱包用户基础信息（经验等级、地区、设备型号、常用链、连接方式、
是否开启 Passphrase、是否完成备份验证），用于个性化安全建议和故障排查。
"""

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent.security.secrets import contains_unredacted_secret
from utils.logger_handler import logger


@dataclass
class UserProfile:
    """硬件钱包用户档案数据结构"""
    user_id: str
    experience_level: Optional[str] = None   # 新手 / 进阶 / 资深
    region: Optional[str] = None             # 地区
    device_model: Optional[str] = None       # KeyGuard 型号
    preferred_chains: Optional[str] = None   # 常用链，如 "BTC, ETH, SOL"
    connection_method: Optional[str] = None  # USB-C / 蓝牙
    passphrase_enabled: Optional[bool] = None
    backup_verified: Optional[bool] = None


class ProfileDatabase:
    """硬件钱包用户档案数据库服务"""

    _TEXT_LIMITS = {
        "experience_level": 16,
        "region": 100,
        "device_model": 100,
        "preferred_chains": 500,
        "connection_method": 50,
    }
    _USER_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}", re.ASCII)

    def __init__(self, db_path: str = "data/profiles.db"):
        """
        Args:
            db_path: SQLite 数据库文件路径
        """
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        expected_columns = {
            "user_id",
            "experience_level",
            "region",
            "device_model",
            "preferred_chains",
            "connection_method",
            "passphrase_enabled",
            "backup_verified",
            "updated_at",
        }
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'user_profiles'
        """)
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(user_profiles)")
            existing_columns = {row[1] for row in cursor.fetchall()}
            if not expected_columns.issubset(existing_columns):
                logger.info("[ProfileDB] 检测到旧版用户档案表，重建为硬件钱包档案结构")
                cursor.execute("DROP TABLE user_profiles")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                experience_level TEXT,
                region TEXT,
                device_model TEXT,
                preferred_chains TEXT,
                connection_method TEXT,
                passphrase_enabled BOOLEAN,
                backup_verified BOOLEAN,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
        conn.close()
        logger.info("[ProfileDB] 数据库初始化完成")

    @classmethod
    def _safe_user_id(cls, value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("user_id 类型非法")
        normalized = value.strip()
        if (
            not normalized
            or cls._USER_ID_PATTERN.fullmatch(normalized) is None
            or any(unicodedata.category(char).startswith("C") for char in normalized)
            or contains_unredacted_secret(normalized)
        ):
            raise ValueError("user_id 非法")
        return normalized

    @classmethod
    def _safe_optional_text(cls, value: object, field: str) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError(f"{field} 类型非法")
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > cls._TEXT_LIMITS[field]
            or any(unicodedata.category(char).startswith("C") for char in normalized)
            or contains_unredacted_secret(normalized)
        ):
            raise ValueError(f"{field} 非法")
        return normalized

    @staticmethod
    def _safe_optional_bool(value: object, field: str) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        raise TypeError(f"{field} 类型非法")

    @staticmethod
    def _database_optional_bool(value: object, field: str) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        raise TypeError(f"{field} 类型非法")

    @classmethod
    def _project_profile(cls, profile: object) -> UserProfile:
        if not isinstance(profile, UserProfile):
            raise TypeError("profile 类型非法")
        return UserProfile(
            user_id=cls._safe_user_id(profile.user_id),
            experience_level=cls._safe_optional_text(
                profile.experience_level, "experience_level"
            ),
            region=cls._safe_optional_text(profile.region, "region"),
            device_model=cls._safe_optional_text(
                profile.device_model, "device_model"
            ),
            preferred_chains=cls._safe_optional_text(
                profile.preferred_chains, "preferred_chains"
            ),
            connection_method=cls._safe_optional_text(
                profile.connection_method, "connection_method"
            ),
            passphrase_enabled=cls._safe_optional_bool(
                profile.passphrase_enabled, "passphrase_enabled"
            ),
            backup_verified=cls._safe_optional_bool(
                profile.backup_verified, "backup_verified"
            ),
        )

    def save_profile(self, profile: UserProfile) -> bool:
        """保存或更新用户档案

        Args:
            profile: 用户档案对象

        Returns:
            是否保存成功
        """
        try:
            projected = self._project_profile(profile)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO user_profiles
                    (user_id, experience_level, region, device_model,
                     preferred_chains, connection_method, passphrase_enabled, backup_verified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    projected.user_id,
                    projected.experience_level,
                    projected.region,
                    projected.device_model,
                    projected.preferred_chains,
                    projected.connection_method,
                    projected.passphrase_enabled,
                    projected.backup_verified,
                ))
            logger.info("[ProfileDB] 用户档案已保存")
            return True
        except Exception as error:
            logger.error(
                "[ProfileDB] 保存失败 error_type=%s",
                type(error).__name__,
            )
            return False

    def get_profile(self, user_id: str) -> Optional[UserProfile]:
        """获取用户档案

        Args:
            user_id: 用户 ID

        Returns:
            用户档案对象，不存在则返回 None
        """
        try:
            safe_user_id = self._safe_user_id(user_id)
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("""
                    SELECT user_id, experience_level, region, device_model,
                           preferred_chains, connection_method, passphrase_enabled, backup_verified
                    FROM user_profiles WHERE user_id = ?
                """, (safe_user_id,)).fetchone()

            if row:
                return self._project_profile(UserProfile(
                    user_id=row[0],
                    experience_level=row[1],
                    region=row[2],
                    device_model=row[3],
                    preferred_chains=row[4],
                    connection_method=row[5],
                    passphrase_enabled=self._database_optional_bool(
                        row[6], "passphrase_enabled"
                    ),
                    backup_verified=self._database_optional_bool(
                        row[7], "backup_verified"
                    ),
                ))
            return None
        except Exception as error:
            logger.error(
                "[ProfileDB] 查询失败 error_type=%s",
                type(error).__name__,
            )
            return None
