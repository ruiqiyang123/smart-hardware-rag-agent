
"""
总结服务类：用户提问，搜索参考资料，将提问和参考资料提交给模型，让模型总结回复。

改造点：增加引用溯源——在答案末尾标注命中的知识库文档来源，提升回答可信度。
原版本只返回模型总结后的答案，用户无法判断依据；售后场景用户对 AI 回答信任度低，
加来源标注后，用户可以追溯答案出处。
"""
import hashlib
import os
import re
from typing import Optional
from urllib.parse import urlparse

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from agent.orchestration.state import EvidenceItem
from agent.security.secrets import contains_unredacted_secret
from rag.keyword_fallback import get_keyword_fallback_docs
from rag.source_formatter import format_reference_sources
from utils.prompt_loader import load_rag_prompts
from utils.logger_handler import logger
from utils.path_tool import get_abs_path


_MAX_QUERY_LENGTH = 500
_MAX_EVIDENCE_ITEMS = 8
_MAX_CONTENT_LENGTH = 4_000
_MAX_SOURCE_TITLE_LENGTH = 300
_MAX_SOURCE_URL_LENGTH = 2_048
_MAX_SOURCE_NAME_LENGTH = 80
_MAX_ENTRY_ID_LENGTH = 32
_UNSAFE_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SAFE_ID_PATTERN = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


def _merge_docs(
    primary_docs: list[Document], secondary_docs: list[Document]
) -> list[Document]:
    merged: list[Document] = []
    seen = set()
    for doc in [*primary_docs, *secondary_docs]:
        if not isinstance(doc, Document):
            continue
        source = doc.metadata.get("source")
        entry_id = doc.metadata.get("entry_id")
        key = (
            doc.page_content,
            source if isinstance(source, str) else repr(source),
            entry_id if isinstance(entry_id, (str, int)) else repr(entry_id),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(doc)
    return merged


def _bounded_safe_text(value: object, *, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if (
        not text
        or len(text) > limit
        or _UNSAFE_CONTROL_PATTERN.search(text)
        or contains_unredacted_secret(text)
    ):
        return None
    return text


def _safe_identifier(value: object, *, fallback: str, limit: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if (
        _UNSAFE_CONTROL_PATTERN.search(text)
        or contains_unredacted_secret(text)
    ):
        text = ""
    text = _SAFE_ID_PATTERN.sub("-", text).strip(".-_")
    if not text:
        text = fallback
    return text[:limit]


def _first_source_url(value: object) -> Optional[str]:
    """Return the first non-empty URL from the loader's CSV/pipe metadata."""
    if not isinstance(value, str):
        return None
    for candidate in re.split(r"\s*(?:,|\||、)\s*", value):
        candidate = candidate.strip()
        if not candidate:
            continue
        if (
            len(candidate) > _MAX_SOURCE_URL_LENGTH
            or _UNSAFE_CONTROL_PATTERN.search(candidate)
            or any(character.isspace() for character in candidate)
            or contains_unredacted_secret(candidate)
        ):
            continue
        try:
            parsed = urlparse(candidate)
            port = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme.lower() == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
        ):
            return candidate
    return None


def _document_to_evidence(doc: object) -> Optional[dict]:
    if not isinstance(doc, Document) or not isinstance(doc.metadata, dict):
        return None
    content = _bounded_safe_text(doc.page_content, limit=_MAX_CONTENT_LENGTH)
    if content is None:
        return None

    metadata = doc.metadata
    raw_source = metadata.get("source")
    source_basename = (
        raw_source.replace("\\", "/").rsplit("/", 1)[-1]
        if isinstance(raw_source, str)
        else ""
    )
    source_name = _safe_identifier(
        source_basename,
        fallback="unknown",
        limit=_MAX_SOURCE_NAME_LENGTH,
    )
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    entry_id = _safe_identifier(
        metadata.get("entry_id"),
        fallback=f"chunk-{content_hash}",
        limit=_MAX_ENTRY_ID_LENGTH,
    )

    source_title = _bounded_safe_text(
        metadata.get("entry_question"), limit=_MAX_SOURCE_TITLE_LENGTH
    )
    if source_title is not None and (
        os.path.isabs(source_title)
        or re.match(r"^[A-Za-z]:[\\/]", source_title)
    ):
        source_title = None
    if source_title is None:
        source_title = _bounded_safe_text(
            metadata.get("doc_title"), limit=_MAX_SOURCE_TITLE_LENGTH
        )
    if source_title is not None and (
        os.path.isabs(source_title)
        or re.match(r"^[A-Za-z]:[\\/]", source_title)
    ):
        source_title = None
    if source_title is None:
        source_title = source_name

    evidence = EvidenceItem(
        evidence_id=f"kb:{source_name}:{entry_id}",
        kind="knowledge",
        content=content,
        source_title=source_title,
        source_url=_first_source_url(metadata.get("source_urls")),
    )
    return evidence.model_dump(mode="json")


def _validate_search_query(query: object) -> str:
    if not isinstance(query, str):
        raise TypeError("query 必须是字符串")
    normalized = query.strip()
    if not normalized:
        raise ValueError("query 不能为空")
    if len(normalized) > _MAX_QUERY_LENGTH:
        raise ValueError("query 超过长度限制")
    if _UNSAFE_CONTROL_PATTERN.search(normalized):
        raise ValueError("query 包含控制字符")
    if contains_unredacted_secret(normalized):
        raise ValueError("query 必须先完成脱敏")
    return normalized


class RagSummarizeService:
    def __init__(self, model=None, *, evidence_only: bool = False):
        # 这些导入会构建默认 embedding/chat 客户端，因此推迟到实例化阶段；
        # 仅导入 V2 结构化证据工具不会再要求模型凭据。
        from rag.vector_store import VectorStoreService

        self.vector_store = VectorStoreService()
        self.retriever = self.vector_store.get_retriever()
        if evidence_only:
            self.prompt_text = None
            self.prompt_template = None
            self.model = None
            self.chain = None
            return

        self.prompt_text = load_rag_prompts()
        self.prompt_template = PromptTemplate.from_template(self.prompt_text)
        # model 可按会话注入，与 ReactAgent 共用同一 chat 模型；
        # 未传则用模块级默认实例（CLI / 评测脚本）。
        if model is None:
            from model.factory import chat_model as default_chat_model

            model = default_chat_model
        self.model = model
        self.chain = self._init_chain()

    def _init_chain(self):
        return self.prompt_template | self.model | StrOutputParser()

    def retriever_docs(self, query: str) -> list[Document]:
        return self.retriever.invoke(query)

    def search_evidence(
        self,
        query: str,
        fallback_docs: Optional[list[Document]] = None,
    ) -> list[dict]:
        """Return bounded read-only evidence without invoking an LLM or chain."""
        normalized_query = _validate_search_query(query)
        if fallback_docs is not None and not isinstance(fallback_docs, list):
            raise TypeError("fallback_docs 必须是 Document 列表或 None")

        vector_docs: list[Document] = []
        try:
            retrieved = self.retriever_docs(normalized_query)
            if isinstance(retrieved, list):
                vector_docs = retrieved
        except Exception as error:
            logger.warning(
                "[search_evidence]向量检索失败: %s", type(error).__name__
            )

        keyword_docs: list[Document] = []
        if fallback_docs is not None:
            keyword_docs = fallback_docs
        else:
            try:
                retrieved = get_keyword_fallback_docs(
                    normalized_query, get_abs_path("data")
                )
                if isinstance(retrieved, list):
                    keyword_docs = retrieved
            except Exception as error:
                logger.warning(
                    "[search_evidence]关键词检索失败: %s", type(error).__name__
                )

        results: list[dict] = []
        signatures_by_id: dict[str, tuple] = {}
        for doc in _merge_docs(keyword_docs, vector_docs):
            evidence = _document_to_evidence(doc)
            if evidence is None:
                continue
            signature = (
                evidence["content"],
                evidence["source_title"],
                evidence["source_url"],
            )
            evidence_id = evidence["evidence_id"]
            known_signature = signatures_by_id.get(evidence_id)
            if known_signature == signature:
                continue
            if known_signature is not None:
                suffix = hashlib.sha256(
                    "\x1f".join(str(value) for value in signature).encode("utf-8")
                ).hexdigest()[:8]
                prefix = evidence_id[: 128 - len(suffix) - 1]
                evidence["evidence_id"] = f"{prefix}:{suffix}"
                if signatures_by_id.get(evidence["evidence_id"]) == signature:
                    continue
            signatures_by_id[evidence["evidence_id"]] = signature
            results.append(evidence)
            if len(results) >= _MAX_EVIDENCE_ITEMS:
                break
        return results

    def rag_summarize(self, query: str) -> str:
        context_docs = self.retriever_docs(query)
        fallback_docs = get_keyword_fallback_docs(query, get_abs_path("data"))
        if fallback_docs:
            context_docs = _merge_docs(fallback_docs, context_docs)

        if not context_docs:
            logger.warning(f"[rag_summarize]未检索到相关资料，query={query}")
            return f"知识库中未检索到与「{query}」相关的内容，无法生成准确答复。"

        context = ""
        metadata_list = []
        counter = 0
        for doc in context_docs:
            counter += 1
            context += f"【参考资料{counter}】: 参考资料：{doc.page_content} | 参考元数据：{doc.metadata}\n"
            metadata_list.append(doc.metadata)

        answer = self.chain.invoke(
            {
                "input": query,
                "context": context,
            }
        )

        # 引用溯源：去重后把来源文件名附在答案末尾
        source_line = "\n\n📚 " + format_reference_sources(metadata_list)
        logger.info(f"[rag_summarize]query={query}，命中来源={[m.get('source') for m in metadata_list]}")
        return answer + source_line


if __name__ == '__main__':
    rag = RagSummarizeService()

    print(rag.rag_summarize("硬件钱包连接不上电脑怎么办"))
