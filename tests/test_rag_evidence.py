import logging
import threading
import time

import pytest
from langchain_core.documents import Document

from agent.orchestration.state import EvidenceItem
from rag.rag_service import RagSummarizeService


class FakeRetriever:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def invoke(self, query):
        self.calls.append(query)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def service_with(result):
    service = object.__new__(RagSummarizeService)
    service.retriever = FakeRetriever(result)
    service.chain = type(
        "ForbiddenChain",
        (),
        {"invoke": lambda self, value: (_ for _ in ()).throw(AssertionError("不得调用 LLM"))},
    )()
    return service


def doc(content, source="故障排除.txt", entry_id="1", **metadata):
    return Document(
        page_content=content,
        metadata={"source": source, "entry_id": entry_id, **metadata},
    )


def test_search_evidence_returns_structured_items_without_llm(monkeypatch):
    service = service_with(
        [
            doc(
                "请使用原装数据线重新连接设备。",
                source="/private/knowledge/故障排除.txt",
                entry_id="usb-1",
            )
        ]
    )
    monkeypatch.setattr("rag.rag_service.get_keyword_fallback_docs", lambda *_: [])

    assert service.search_evidence("设备无法连接", fallback_docs=[]) == [
        {
            "evidence_id": "kb:故障排除.txt:usb-1",
            "kind": "knowledge",
            "content": "请使用原装数据线重新连接设备。",
            "source_title": "故障排除.txt",
            "source_url": None,
        }
    ]
    assert service.retriever.calls == ["设备无法连接"]


def test_vector_failure_falls_back_and_logs_only_exception_type(caplog):
    secret_query_fragment = "NEVER_LOG_THIS_QUERY"
    service = service_with(RuntimeError("NEVER_LOG_THIS_EXCEPTION"))
    fallback = [doc("删除旧蓝牙配对后重新连接。", entry_id="bt-1")]

    with caplog.at_level(logging.WARNING):
        result = service.search_evidence(secret_query_fragment, fallback_docs=fallback)

    assert [item["evidence_id"] for item in result] == ["kb:故障排除.txt:bt-1"]
    assert "RuntimeError" in caplog.text
    assert secret_query_fragment not in caplog.text
    assert "NEVER_LOG_THIS_EXCEPTION" not in caplog.text


def test_keyword_failure_still_returns_vector_result(monkeypatch, caplog):
    service = service_with([doc("保持设备解锁并重新插拔数据线。")])

    def fail_keyword(*_):
        raise LookupError("NEVER_LOG_DOCUMENT_OR_QUERY")

    monkeypatch.setattr("rag.rag_service.get_keyword_fallback_docs", fail_keyword)
    with caplog.at_level(logging.WARNING):
        result = service.search_evidence("电脑无法识别设备")

    assert len(result) == 1
    assert "LookupError" in caplog.text
    assert "电脑无法识别设备" not in caplog.text
    assert "NEVER_LOG_DOCUMENT_OR_QUERY" not in caplog.text


def test_explicit_empty_fallback_skips_keyword_lookup(monkeypatch):
    service = service_with([doc("向量证据")])
    monkeypatch.setattr(
        "rag.rag_service.get_keyword_fallback_docs",
        lambda *_: (_ for _ in ()).throw(AssertionError("不应调用关键词检索")),
    )

    assert service.search_evidence("普通问题", fallback_docs=[])[0]["content"] == "向量证据"


def test_keyword_precedes_vector_deduplicates_stably_and_caps_at_eight():
    repeated = doc("重复证据", source="/tmp/data/共同.txt", entry_id="same")
    vector_docs = [repeated] + [
        doc(f"向量证据 {index}", source=f"/tmp/vector-{index}.txt", entry_id=str(index))
        for index in range(10)
    ]
    keyword_docs = [
        doc("关键词证据", source="/tmp/keyword.txt", entry_id="kw"),
        repeated,
    ]
    service = service_with(vector_docs)

    first = service.search_evidence("连接问题", fallback_docs=keyword_docs)
    second = service.search_evidence("连接问题", fallback_docs=keyword_docs)

    assert first == second
    assert len(first) == 8
    assert [item["content"] for item in first[:2]] == ["关键词证据", "重复证据"]
    assert [item["content"] for item in first].count("重复证据") == 1


def test_metadata_is_bounded_source_backed_and_never_leaks_absolute_path():
    service = service_with(
        [
            doc(
                "蓝牙排查步骤",
                source="/Users/person/private/data/故障排除.txt",
                entry_id="4",
                entry_question="蓝牙配对失败",
                source_urls=", https://support.example.test/first , https://example.test/second",
            ),
            Document(page_content="无 metadata 证据", metadata={}),
            doc(
                "Windows 路径证据",
                source=r"C:\Users\person\private\固件升级.txt",
                entry_id="win",
                source_urls=(
                    "http://insecure.example.test, "
                    "https://user:password@support.example.test/private, "
                    "https://support.example.test:8443/wrong, "
                    "https://support.example.test:443/official"
                ),
            ),
        ]
    )

    result = service.search_evidence("蓝牙连接", fallback_docs=[])

    assert result[0] == {
        "evidence_id": "kb:故障排除.txt:4",
        "kind": "knowledge",
        "content": "蓝牙排查步骤",
        "source_title": "蓝牙配对失败",
        "source_url": "https://support.example.test/first",
    }
    assert result[1]["evidence_id"].startswith("kb:unknown:chunk-")
    assert result[2]["evidence_id"] == "kb:固件升级.txt:win"
    assert result[2]["source_url"] == "https://support.example.test:443/official"
    assert "/Users/" not in repr(result)
    assert "C:\\Users\\" not in repr(result)
    for item in result:
        EvidenceItem.model_validate(item)


def test_invalid_documents_are_skipped_without_secret_or_control_leakage():
    private_key = "1" * 64
    service = service_with(
        [
            doc(" "),
            doc("x" * 4001),
            doc(f"private key: {private_key}"),
            doc("bad\x00content"),
            doc("安全证据", entry_id="safe"),
        ]
    )

    result = service.search_evidence("普通连接问题", fallback_docs=[])

    assert [item["evidence_id"] for item in result] == ["kb:故障排除.txt:safe"]
    assert private_key not in repr(result)


@pytest.mark.parametrize(
    "query",
    [
        None,
        "",
        "   ",
        "x" * 501,
        "bad\x00query",
        "private key: " + "1" * 64,
    ],
)
def test_query_must_be_bounded_and_already_sanitized(query):
    service = service_with([])

    with pytest.raises((TypeError, ValueError)):
        service.search_evidence(query, fallback_docs=[])
    assert service.retriever.calls == []


def test_conflicting_duplicate_ids_are_stably_disambiguated():
    service = service_with(
        [
            doc("第一条", source="/tmp/same.txt", entry_id="7"),
            doc("第二条", source="/other/same.txt", entry_id="7"),
        ]
    )

    result = service.search_evidence("普通问题", fallback_docs=[])

    assert result[0]["evidence_id"] == "kb:same.txt:7"
    assert result[1]["evidence_id"].startswith("kb:same.txt:7:")
    assert len({item["evidence_id"] for item in result}) == 2


def test_v2_tool_is_lazy_and_calls_only_structured_search(monkeypatch):
    import agent.tools.agent_tools as tools_module

    class EvidenceOnlyService:
        def __init__(self):
            self.calls = []

        def search_evidence(self, query):
            self.calls.append(query)
            return [
                {
                    "evidence_id": "kb:test.txt:1",
                    "kind": "knowledge",
                    "content": "只读证据",
                    "source_title": "测试来源",
                    "source_url": None,
                }
            ]

        def rag_summarize(self, query):
            raise AssertionError("V2 工具不得调用摘要模型")

    fake = EvidenceOnlyService()
    monkeypatch.setattr(tools_module, "_evidence_rag", fake)

    result = tools_module.search_support_evidence.invoke({"query": "连接失败"})

    assert result[0]["kind"] == "knowledge"
    assert fake.calls == ["连接失败"]


def test_v1_rag_summary_behavior_remains_available(monkeypatch):
    class SummaryChain:
        def __init__(self):
            self.calls = []

        def invoke(self, payload):
            self.calls.append(payload)
            return "V1 摘要"

    service = service_with(
        [
            doc(
                "V1 向量内容",
                source="/tmp/故障排除.txt",
                entry_id="1",
                entry_question="连接失败",
            )
        ]
    )
    service.chain = SummaryChain()
    monkeypatch.setattr("rag.rag_service.get_keyword_fallback_docs", lambda *_: [])

    result = service.rag_summarize("连接失败")

    assert result.startswith("V1 摘要")
    assert "📚 参考来源：故障排除.txt 第1条「连接失败」" in result
    assert service.chain.calls[0]["input"] == "连接失败"


def test_v2_candidate_iteration_is_bounded():
    class GuardedList(list):
        def __iter__(self):
            for index, item in enumerate(super().__iter__()):
                if index >= 32:
                    raise AssertionError("单个证据源不得扫描超过 32 个候选")
                yield item

    invalid = GuardedList(["invalid"] * 100)
    service = service_with(invalid)

    assert service.search_evidence("连接问题", fallback_docs=invalid) == []


def test_agent_tool_module_exposes_process_wide_rag_lock():
    import agent.tools.agent_tools as tools_module

    assert isinstance(tools_module._rag_lock, type(threading.RLock()))


def test_concurrent_rag_accessors_construct_each_service_once(monkeypatch):
    import agent.tools.agent_tools as tools_module

    class FakeService:
        counts = {"summary": 0, "evidence": 0}
        count_lock = threading.Lock()

        def __init__(self, model=None, *, evidence_only=False):
            kind = "evidence" if evidence_only else "summary"
            with self.count_lock:
                self.counts[kind] += 1
            time.sleep(0.01)
            self.kind = kind

    monkeypatch.setattr(tools_module, "RagSummarizeService", FakeService)
    monkeypatch.setattr(tools_module, "rag", None)
    monkeypatch.setattr(tools_module, "_evidence_rag", None)
    barrier = threading.Barrier(24)
    results = []
    errors = []

    def access(accessor):
        try:
            barrier.wait(timeout=2)
            results.append(accessor())
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(
            target=access,
            args=(
                tools_module._summary_rag
                if index % 2 == 0
                else tools_module._structured_evidence_rag,
            ),
        )
        for index in range(24)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert FakeService.counts == {"summary": 1, "evidence": 1}
    assert len({id(item) for item in results if item.kind == "summary"}) == 1
    assert len({id(item) for item in results if item.kind == "evidence"}) == 1


def test_configure_rag_model_publishes_only_fully_constructed_service(monkeypatch):
    import agent.tools.agent_tools as tools_module

    old_service = object()
    construction_started = threading.Event()
    release_construction = threading.Event()

    class SlowConfiguredService:
        def __init__(self, model=None, *, evidence_only=False):
            self.ready = False
            self.model = model
            construction_started.set()
            assert release_construction.wait(timeout=2)
            self.ready = True

    monkeypatch.setattr(tools_module, "RagSummarizeService", SlowConfiguredService)
    monkeypatch.setattr(tools_module, "rag", old_service)
    thread = threading.Thread(
        target=tools_module.configure_rag_model,
        args=("configured-model",),
    )
    thread.start()
    assert construction_started.wait(timeout=2)

    # Readers may safely finish with the previous complete instance while the
    # replacement is being built, but must never observe the partial object.
    assert tools_module._summary_rag() is old_service
    release_construction.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert tools_module.rag.ready is True
    assert tools_module.rag.model == "configured-model"


def test_v2_stops_after_eight_valid_items_without_touching_vector_source():
    class CountingList(list):
        def __init__(self, values):
            super().__init__(values)
            self.visited = 0

        def __iter__(self):
            for item in super().__iter__():
                self.visited += 1
                yield item

    keyword_docs = CountingList(
        [doc(f"关键词 {index}", entry_id=f"kw-{index}") for index in range(100)]
    )
    vector_docs = CountingList(
        [doc(f"向量 {index}", entry_id=f"vec-{index}") for index in range(100)]
    )
    service = service_with(vector_docs)

    result = service.search_evidence("连接问题", fallback_docs=keyword_docs)

    assert len(result) == 8
    assert keyword_docs.visited == 8
    assert vector_docs.visited == 0


def test_v2_scans_at_most_32_invalid_candidates_from_each_source():
    class CountingList(list):
        def __init__(self):
            super().__init__(["invalid"] * 10_000)
            self.visited = 0

        def __iter__(self):
            for item in super().__iter__():
                self.visited += 1
                yield item

    keyword_docs = CountingList()
    vector_docs = CountingList()
    service = service_with(vector_docs)

    assert service.search_evidence("连接问题", fallback_docs=keyword_docs) == []
    assert keyword_docs.visited == 32
    assert vector_docs.visited == 32
