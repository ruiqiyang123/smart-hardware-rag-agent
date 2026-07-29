import copy
import json
from pathlib import Path

import pytest

from agent.orchestration.state import EvidenceItem
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
    path = tmp_path / "warranty.json"
    payload = default_records()
    path.write_text(json.dumps(payload), encoding="utf-8")
    repository = WarrantyRepository(path)

    first = repository.find_by_last4("A1B2")
    first["device_model"] = "mutated"
    payload[0]["device_model"] = "changed-on-disk"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert repository.find_by_last4("A1B2")["device_model"] == "KeyGuard Pro"


def default_records():
    path = Path(__file__).resolve().parents[1] / "data" / "warranty_records.json"
    return json.loads(path.read_text(encoding="utf-8"))


def write_payload(tmp_path, payload):
    path = tmp_path / "records.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


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
    assert content["warranty_record"]["serial_last4"] == "A1B2"
