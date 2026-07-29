"""模型工厂：LLM 与 Embedding 的构建。

改造点（会话隔离）：
- 原版本只在模块导入时基于 os.environ 创建单例 chat_model / embed_model，
  导致 app.py 侧边栏切换 provider 后实际不生效（单例已构建，改 env 无用），
  且多会话共享进程级 env 存在串号风险。
- 现提供 create(**kwargs) / build_chat_model / build_embeddings 显式构建接口，
  Web 前端按会话构建模型并注入 Agent，实现隔离。
- 模块级单例保留，供 CLI / 评测脚本 / 向量库初始化等无 Web 上下文场景使用。
"""
from abc import ABC, abstractmethod
import os
from typing import Optional, Union

from dotenv import load_dotenv
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_community.chat_models.tongyi import ChatTongyi
from langchain_openai import ChatOpenAI
from model.local_embeddings import LocalHashEmbeddings
from utils.config_handler import rag_conf
from utils.logger_handler import logger
from utils.model_config import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_CHAT_MODEL,
    DEFAULT_DEEPSEEK_THINKING,
    DEFAULT_EMBEDDING_PROVIDER,
    DEFAULT_LOCAL_EMBEDDING_DIMENSION,
    DEFAULT_MIMO_BASE_URL,
    DEFAULT_MIMO_CHAT_MODEL,
)

load_dotenv(override=False)

_DEFAULT_CHAT_PROVIDER = "deepseek"
_DEFAULT_EMBEDDING_PROVIDER = DEFAULT_EMBEDDING_PROVIDER


class BaseModelFactory(ABC):
    @abstractmethod
    def generator(self) -> Optional[Union[Embeddings, BaseChatModel]]:
        pass


class ChatModelFactory(BaseModelFactory):
    @staticmethod
    def create(
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        thinking: Optional[str] = None,
    ) -> BaseChatModel:
        """显式构建 chat 模型；缺省值从环境变量 / 配置文件解析。

        Web 前端应通过本方法按会话构建并注入 ReactAgent，
        而非依赖 os.environ 路由（后者无法在运行时切换）。
        """
        provider = (provider or os.getenv("CHAT_PROVIDER", _DEFAULT_CHAT_PROVIDER)).lower()

        if provider == "deepseek":
            api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
            base_url = (
                (os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL)
                if base_url is None
                else base_url.strip()
            )
            model_name = (
                (os.getenv("DEEPSEEK_CHAT_MODEL") or DEFAULT_DEEPSEEK_CHAT_MODEL)
                if model_name is None
                else model_name.strip()
            )
            thinking = (
                os.getenv("DEEPSEEK_THINKING", DEFAULT_DEEPSEEK_THINKING)
                if thinking is None
                else thinking
            ).strip().lower()
            if thinking not in {"enabled", "disabled"}:
                raise ValueError("DEEPSEEK_THINKING 只支持 enabled 或 disabled")
            if not api_key or not base_url or not model_name:
                raise ValueError("使用 DeepSeek 模型时，请配置 api_key、base_url 和 model")
            logger.info(f"[ChatModelFactory]构建 DeepSeek 模型：{model_name}")
            return ChatOpenAI(
                model=model_name,
                api_key=api_key,
                base_url=base_url,
                temperature=0,
                extra_body={"thinking": {"type": thinking}},
            )

        if provider in {"mimo", "openai"}:
            api_key = api_key or os.getenv("MIMO_API_KEY") or os.getenv("OPENAI_API_KEY")
            base_url = base_url or os.getenv("MIMO_BASE_URL") or os.getenv("OPENAI_BASE_URL") or DEFAULT_MIMO_BASE_URL
            model_name = (
                model_name
                or os.getenv("MIMO_CHAT_MODEL")
                or os.getenv("OPENAI_CHAT_MODEL")
                or DEFAULT_MIMO_CHAT_MODEL
            )
            if not api_key or not base_url:
                raise ValueError("使用 MiMo/OpenAI 兼容模型时，请配置 api_key 和 base_url")
            logger.info(f"[ChatModelFactory]构建 MiMo/OpenAI 模型：{model_name}")
            return ChatOpenAI(
                model=model_name,
                api_key=api_key,
                base_url=base_url,
                temperature=float(os.getenv("CHAT_TEMPERATURE", "0")),
            )

        # dashscope（默认）
        api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        logger.info(f"[ChatModelFactory]构建 DashScope 模型：{rag_conf['chat_model_name']}")
        return ChatTongyi(model=rag_conf["chat_model_name"], api_key=api_key)

    def generator(self) -> BaseChatModel:
        """基于环境变量构建默认实例（CLI / 评测脚本用）。"""
        return self.create()


class EmbeddingsFactory(BaseModelFactory):
    @staticmethod
    def create(
        provider: Optional[str] = None,
        dimension: Optional[int] = None,
    ) -> Embeddings:
        """显式构建 embedding 模型；缺省值从环境变量解析。

        注意：embedding 模型一经构建即绑定 Chroma 向量库，不可在运行时切换
        （切换会导致向量维度不一致、检索失败）。因此 embedding 应在应用启动时
        确定一次，而非按用户会话切换。
        """
        provider = (provider or os.getenv("EMBEDDING_PROVIDER", _DEFAULT_EMBEDDING_PROVIDER)).lower()

        if provider == "local":
            dimension = dimension or int(os.getenv("LOCAL_EMBEDDING_DIMENSION", str(DEFAULT_LOCAL_EMBEDDING_DIMENSION)))
            logger.info(f"[EmbeddingsFactory]构建本地哈希 embedding，维度={dimension}")
            return LocalHashEmbeddings(dimension=dimension)

        logger.info(f"[EmbeddingsFactory]构建 DashScope embedding：{rag_conf['embedding_model_name']}")
        return DashScopeEmbeddings(
            model=rag_conf["embedding_model_name"],
            dashscope_api_key=os.getenv("DASHSCOPE_API_KEY"),
        )

    def generator(self) -> Embeddings:
        return self.create()


def build_chat_model(**kwargs) -> BaseChatModel:
    """便捷构建函数，供 Web 前端按会话注入。"""
    return ChatModelFactory.create(**kwargs)


def build_embeddings(**kwargs) -> Embeddings:
    return EmbeddingsFactory.create(**kwargs)


# 模块级默认实例：基于环境变量，供 CLI / 评测脚本 / 向量库初始化使用。
# Web 前端应通过 build_chat_model() 显式构建并注入 ReactAgent，实现会话隔离。
chat_model = ChatModelFactory().generator()
embed_model = EmbeddingsFactory().generator()
