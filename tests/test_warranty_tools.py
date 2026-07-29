import copy
import json
import os
from pathlib import Path

import pytest

from agent.orchestration.state import EvidenceItem
import agent.tools.warranty_tools as warranty_module
from agent.tools.warranty_tools import WarrantyRepository


def test_default_warranty_repository_returns_simulated_evidence(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    repository = WarrantyRepository()

    record = repository.find_by_last4(" a1b2 ")
    assert record is not None
    assert record["device_model"] == "KeyGuard Pro"
    assert record["warranty_status"] == "active"
    evidence = repository.as_evidence("A1B2")
    assert evidence["kind"] == "warranty"
    assert evidence["evidence_id"] == "warranty:A1B2"
    assert "模拟" in evidence["content"]
    assert evidence["source_url"] is None
    EvidenceItem.model_validate(evidence)


def test_default_dataset_has_exactly_eight_explicitly_simulated_records():
    records = default_records()

    assert len(records) == 8
    assert len({record["serial_last4"] for record in records}) == 8
    assert all("模拟" in record["note"] for record in records)


def test_unknown_serial_and_invalid_serial_contract():
    repository = WarrantyRepository()

    assert repository.find_by_last4("ZZZZ") is None
    assert repository.as_evidence("ZZZZ") is None
    for invalid in (None, "", "ABC", "ABCDE", "A-12", "Ａ1B2", "x" * 33):
        with pytest.raises((TypeError, ValueError)):
            repository.find_by_last4(invalid)


def test_records_are_loaded_once_and_results_are_deep_copies(tmp_path):
    payload = default_records()
    path = write_payload(tmp_path, payload)
    repository = WarrantyRepository(path)

    first = repository.find_by_last4("A1B2")
    first["device_model"] = "mutated"
    payload[0]["device_model"] = "changed-on-disk"
    path.write_text(json.dumps(snapshot_payload(payload)), encoding="utf-8")

    assert repository.find_by_last4("A1B2")["device_model"] == "KeyGuard Pro"


def default_records():
    path = Path(__file__).resolve().parents[1] / "data" / "warranty_records.json"
    return json.loads(path.read_text(encoding="utf-8"))["records"]


def write_payload(tmp_path, payload):
    path = tmp_path / "records.json"
    if isinstance(payload, list):
        payload = snapshot_payload(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def snapshot_payload(records, snapshot_as_of="2026-07-29"):
    return {"snapshot_as_of": snapshot_as_of, "records": records}


def invalid_cases():
    def mutate(**changes):
        def apply(records):
            records[0].update(changes)

        return apply

    def duplicate(records):
        records[1]["serial_last4"] = records[0]["serial_last4"]

    def extra(records):
        records[0]["extra"] = "forbidden"

    return [
        extra,
        mutate(serial_last4="abc-"),
        mutate(device_model=""),
        mutate(warranty_status="unknown"),
        mutate(purchase_date="2028-01-01", warranty_until="2027-01-01"),
        mutate(purchase_date="2026-02-30"),
        mutate(warranty_status="missing_info"),
        mutate(purchase_date="", warranty_until="", warranty_status="active"),
        mutate(note="private key: " + "1" * 64),
        mutate(note="bad\x00note"),
        duplicate,
    ]


@pytest.mark.parametrize("mutator", invalid_cases())
def test_invalid_schema_is_rejected(tmp_path, mutator):
    payload = default_records()
    mutator(payload)
    path = write_payload(tmp_path, payload)

    with pytest.raises((TypeError, ValueError)):
        WarrantyRepository(path)


def test_missing_info_requires_both_dates_to_be_empty_strings(tmp_path):
    payload = default_records()
    payload[0].update(
        purchase_date="",
        warranty_until="",
        warranty_status="missing_info",
    )
    path = write_payload(
        tmp_path,
        payload,
    )

    assert WarrantyRepository(path).find_by_last4("A1B2")["purchase_date"] == ""


@pytest.mark.parametrize("payload", [{}, [], ["not-an-object"] * 8])
def test_top_level_and_exact_record_count_are_strict(tmp_path, payload):
    path = write_payload(tmp_path, payload)

    with pytest.raises((TypeError, ValueError)):
        WarrantyRepository(path)


@pytest.mark.parametrize("delta", [-1, 1])
def test_requires_exactly_eight_records(tmp_path, delta):
    payload = default_records()
    if delta < 0:
        payload.pop()
    else:
        extra = copy.deepcopy(payload[-1])
        extra["serial_last4"] = "Z9Z9"
        payload.append(extra)
    path = write_payload(tmp_path, payload)

    with pytest.raises(ValueError, match="8"):
        WarrantyRepository(path)


def test_rejects_oversize_non_utf8_and_non_regular_files(tmp_path):
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"[" + b" " * (128 * 1024) + b"]")
    invalid_utf8 = tmp_path / "invalid.json"
    invalid_utf8.write_bytes(b"\xff")

    for path in (oversized, invalid_utf8, tmp_path):
        with pytest.raises(ValueError):
            WarrantyRepository(path)


def test_evidence_json_is_stable_and_explicitly_simulated():
    repository = WarrantyRepository()

    first = repository.as_evidence("A1B2")
    second = repository.as_evidence("a1b2")
    content = json.loads(first["content"])

    assert first == second
    assert "模拟" in content["simulation_notice"]
    assert content["snapshot_as_of"] == repository.snapshot_as_of
    assert repository.snapshot_as_of in content["simulation_notice"]
    assert content["warranty_record"]["serial_last4"] == "A1B2"
    assert set(content["warranty_record"]) == {
        "serial_last4",
        "device_model",
        "purchase_date",
        "warranty_until",
        "warranty_status",
        "note",
    }


def test_dataset_declares_a_strict_snapshot_date():
    path = Path(__file__).resolve().parents[1] / "data" / "warranty_records.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert set(payload) == {"snapshot_as_of", "records"}
    assert payload["snapshot_as_of"] == "2026-07-29"
    assert WarrantyRepository().snapshot_as_of == "2026-07-29"


def test_repository_does_not_use_path_read_bytes(monkeypatch):
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: (_ for _ in ()).throw(AssertionError("必须从同一 fd 读取")),
    )

    assert WarrantyRepository().find_by_last4("A1B2") is not None


def test_snapshot_property_is_read_only():
    repository = WarrantyRepository()

    with pytest.raises(AttributeError):
        repository.snapshot_as_of = "2099-01-01"


def test_rejects_symlink_and_does_not_disclose_path(tmp_path):
    target = write_payload(tmp_path, default_records())
    link = tmp_path / "sensitive-link-name.json"
    link.symlink_to(target)

    with pytest.raises(ValueError) as captured:
        WarrantyRepository(link)

    assert "sensitive-link-name" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_rejects_file_replacement_between_lstat_and_open(monkeypatch, tmp_path):
    expected = write_payload(tmp_path, default_records())
    replacement = tmp_path / "replacement-sensitive-name.json"
    replacement.write_text(
        json.dumps(snapshot_payload(default_records()), ensure_ascii=False),
        encoding="utf-8",
    )
    real_open = os.open

    def open_replacement(path, flags):
        return real_open(replacement, flags)

    monkeypatch.setattr(warranty_module.os, "open", open_replacement)
    with pytest.raises(ValueError) as captured:
        WarrantyRepository(expected)

    assert "replacement-sensitive-name" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_fd_reader_is_bounded_even_if_file_grows_after_fstat(monkeypatch):
    requests = []

    def endless_read(descriptor, size):
        requests.append(size)
        return b"x" * size

    monkeypatch.setattr(warranty_module.os, "read", endless_read)
    with pytest.raises(ValueError, match="大小限制"):
        WarrantyRepository()

    assert sum(requests) == warranty_module._MAX_FILE_SIZE + 1
    assert all(size <= 64 * 1024 for size in requests)


def test_secure_open_requests_nofollow_and_cloexec_when_available(monkeypatch):
    observed_flags = []
    real_open = os.open

    def recording_open(path, flags):
        observed_flags.append(flags)
        return real_open(path, flags)

    monkeypatch.setattr(warranty_module.os, "open", recording_open)
    WarrantyRepository()

    assert observed_flags
    for flag_name in ("O_NOFOLLOW", "O_CLOEXEC"):
        flag = getattr(os, flag_name, 0)
        if flag:
            assert observed_flags[0] & flag


@pytest.mark.parametrize(
    "changes",
    [
        {"warranty_status": "active", "warranty_until": "2026-07-28"},
        {"warranty_status": "expired", "warranty_until": "2026-07-29"},
        {"purchase_date": "2026-07-30", "warranty_until": "2027-07-30"},
    ],
)
def test_warranty_status_is_validated_against_snapshot(tmp_path, changes):
    records = default_records()
    records[0].update(changes)

    with pytest.raises(ValueError):
        WarrantyRepository(write_payload(tmp_path, records))


@pytest.mark.parametrize("snapshot", ["", "2026-02-30", "29-07-2026"])
def test_snapshot_date_is_strict_iso(tmp_path, snapshot):
    path = write_payload(tmp_path, snapshot_payload(default_records(), snapshot))

    with pytest.raises(ValueError):
        WarrantyRepository(path)


def test_snapshot_object_rejects_extra_fields(tmp_path):
    payload = snapshot_payload(default_records())
    payload["generated_by"] = "untrusted"

    with pytest.raises(ValueError):
        WarrantyRepository(write_payload(tmp_path, payload))


def test_legacy_top_level_record_list_is_rejected(tmp_path):
    path = tmp_path / "legacy-list.json"
    path.write_text(json.dumps(default_records()), encoding="utf-8")

    with pytest.raises(ValueError, match="快照对象"):
        WarrantyRepository(path)
