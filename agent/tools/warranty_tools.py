import copy
import json
import re
import stat
from datetime import date
from pathlib import Path
from typing import Optional, Union

from agent.orchestration.state import EvidenceItem
from agent.security.secrets import contains_unredacted_secret


_MAX_FILE_SIZE = 128 * 1024
_EXPECTED_FIELDS = frozenset(
    {
        "serial_last4",
        "device_model",
        "purchase_date",
        "warranty_until",
        "warranty_status",
        "note",
    }
)
_SERIAL_PATTERN = re.compile(r"[A-Z0-9]{4}")
_UNSAFE_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_STATUS_VALUES = frozenset({"active", "expired", "missing_info"})


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("保修数据包含重复 JSON 字段")
        result[key] = value
    return result


def _safe_required_text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是字符串")
    if not value or value != value.strip():
        raise ValueError(f"{field} 必须是已规范化的非空字符串")
    if len(value) > limit:
        raise ValueError(f"{field} 超过长度限制")
    if _UNSAFE_CONTROL_PATTERN.search(value):
        raise ValueError(f"{field} 包含控制字符")
    if contains_unredacted_secret(value):
        raise ValueError(f"{field} 包含敏感信息")
    return value


def _iso_date(value: object, field: str) -> date:
    text = _safe_required_text(value, field, 10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{field} 必须是 ISO 日期") from error
    if parsed.isoformat() != text:
        raise ValueError(f"{field} 必须是规范 ISO 日期")
    return parsed


class WarrantyRepository:
    """Read-only repository for the eight explicitly simulated demo records."""

    def __init__(self, path: Optional[Union[str, Path]] = None):
        default_path = Path(__file__).resolve().parents[2] / "data" / "warranty_records.json"
        self.path = Path(path) if path is not None else default_path
        self._records = self._load_records()
        self._by_last4 = {
            record["serial_last4"]: record for record in self._records
        }

    def _load_records(self) -> list[dict]:
        try:
            info = self.path.lstat()
        except OSError:
            raise ValueError("保修数据文件不可用") from None
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("保修数据路径必须是普通文件")
        if info.st_size > _MAX_FILE_SIZE:
            raise ValueError("保修数据文件超过大小限制")

        try:
            raw = self.path.read_bytes()
            if len(raw) > _MAX_FILE_SIZE:
                raise ValueError("保修数据文件超过大小限制")
            text = raw.decode("utf-8", errors="strict")
            payload = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("保修数据包含非标准 JSON 数值")
                ),
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("保修数据文件不是有效 UTF-8 JSON") from None

        if not isinstance(payload, list):
            raise TypeError("保修数据顶层必须是列表")
        if len(payload) != 8:
            raise ValueError("保修数据必须恰好包含 8 条模拟记录")

        records: list[dict] = []
        seen: set[str] = set()
        for item in payload:
            record = self._validate_record(item)
            serial = record["serial_last4"]
            if serial in seen:
                raise ValueError("保修数据 serial_last4 不得重复")
            seen.add(serial)
            records.append(record)
        return records

    @staticmethod
    def _validate_record(item: object) -> dict:
        if not isinstance(item, dict):
            raise TypeError("保修记录必须是对象")
        if set(item) != _EXPECTED_FIELDS:
            raise ValueError("保修记录字段不符合契约")

        serial = _safe_required_text(item["serial_last4"], "serial_last4", 4)
        if _SERIAL_PATTERN.fullmatch(serial) is None:
            raise ValueError("serial_last4 必须是四位大写字母或数字")
        device_model = _safe_required_text(item["device_model"], "device_model", 80)
        note = _safe_required_text(item["note"], "note", 300)

        status_value = item["warranty_status"]
        if not isinstance(status_value, str):
            raise TypeError("warranty_status 必须是字符串")
        if status_value not in _STATUS_VALUES:
            raise ValueError("warranty_status 不受支持")

        purchase_value = item["purchase_date"]
        until_value = item["warranty_until"]
        if status_value == "missing_info":
            if purchase_value != "" or until_value != "":
                raise ValueError("missing_info 记录的日期必须为空字符串")
        else:
            purchase_date = _iso_date(purchase_value, "purchase_date")
            warranty_until = _iso_date(until_value, "warranty_until")
            if purchase_date > warranty_until:
                raise ValueError("purchase_date 不得晚于 warranty_until")

        return {
            "serial_last4": serial,
            "device_model": device_model,
            "purchase_date": purchase_value,
            "warranty_until": until_value,
            "warranty_status": status_value,
            "note": note,
        }

    @staticmethod
    def _normalize_serial(serial_last4: object) -> str:
        if not isinstance(serial_last4, str):
            raise TypeError("serial_last4 必须是字符串")
        if len(serial_last4) > 32:
            raise ValueError("serial_last4 超过长度限制")
        normalized = serial_last4.strip().upper()
        if _SERIAL_PATTERN.fullmatch(normalized) is None:
            raise ValueError("serial_last4 必须是四位字母或数字")
        return normalized

    def find_by_last4(self, serial_last4: str) -> Optional[dict]:
        target = self._normalize_serial(serial_last4)
        record = self._by_last4.get(target)
        return copy.deepcopy(record) if record is not None else None

    def find(self, serial_last4: str) -> Optional[dict]:
        """Compatibility-friendly shorthand with the same strict contract."""
        return self.find_by_last4(serial_last4)

    def as_evidence(self, serial_last4: str) -> Optional[dict]:
        record = self.find_by_last4(serial_last4)
        if record is None:
            return None
        content = json.dumps(
            {
                "simulation_notice": "仅用于 KeyGuard 演示的模拟保修记录，不代表真实设备权益",
                "warranty_record": record,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        evidence = EvidenceItem(
            evidence_id=f"warranty:{record['serial_last4']}",
            kind="warranty",
            content=content,
            source_title="KeyGuard 模拟设备与保修记录",
            source_url=None,
        )
        return evidence.model_dump(mode="json")
