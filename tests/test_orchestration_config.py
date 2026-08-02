import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from utils.config_handler import load_orchestration_config, load_security_policy


VALID_ORCHESTRATION = {
    "timeouts": {
        "agent_seconds": 20,
        "readonly_tool_seconds": 8,
        "graph_seconds": 120,
    },
    "retries": {"triage": 1, "diagnosis": 1, "readonly_tool": 1, "review": 0},
    "recursion_limit": 16,
    "command_lease_seconds": 130,
    "customer_session": {
        "inactivity_timeout_seconds": 1800,
        "expiry_poll_seconds": 30,
    },
    "customer_handoff": {"ai_attempt_threshold": 3},
    "required_fields": {
        "firmware_repair": ["device_model", "error_state"],
        "warranty_service": ["serial_last4"],
        "transaction_boundary": ["transaction_hash", "chain_name"],
    },
    "manual_gate_actions": [
        "device_reset",
        "bootloader_recovery",
        "warranty_decision",
    ],
}

VALID_SECURITY_POLICY = {
    "policy_version": "2026-07-28.v1",
    "official_domains": ["support.ledger.com", "trezor.io"],
    "critical_response_template_zh": "安全提示",
}


class ConfigTestCase(unittest.TestCase):
    def write_yaml(self, data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.yml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return str(path)

    def write_raw(self, content):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.yml"
        path.write_text(content, encoding="utf-8")
        return str(path)


class OrchestrationConfigTest(ConfigTestCase):
    def load_changed(self, mutator):
        data = copy.deepcopy(VALID_ORCHESTRATION)
        mutator(data)
        return load_orchestration_config(self.write_yaml(data))

    def test_loads_fixed_execution_limits(self):
        config = load_orchestration_config()
        self.assertEqual(config["timeouts"]["agent_seconds"], 20)
        self.assertEqual(config["timeouts"]["readonly_tool_seconds"], 8)
        self.assertEqual(config["timeouts"]["graph_seconds"], 120)
        self.assertEqual(config["recursion_limit"], 16)
        self.assertEqual(config["command_lease_seconds"], 130)
        self.assertEqual(
            config["customer_session"]["inactivity_timeout_seconds"], 1800
        )
        self.assertEqual(config["customer_session"]["expiry_poll_seconds"], 30)
        self.assertEqual(config["customer_handoff"]["ai_attempt_threshold"], 3)
        self.assertEqual(config["retries"]["review"], 0)

    def test_rejects_missing_file_and_malformed_yaml(self):
        with self.assertRaises(FileNotFoundError):
            load_orchestration_config("/definitely/missing/orchestration.yml")
        with self.assertRaises(yaml.YAMLError):
            load_orchestration_config(self.write_raw("timeouts: [unterminated"))

    def test_rejects_non_mapping_root_and_nested_sections(self):
        with self.assertRaises(TypeError):
            load_orchestration_config(self.write_yaml([]))
        for section, bad_value in (("timeouts", "20"), ("retries", [])):
            with self.subTest(section=section), self.assertRaises(TypeError):
                self.load_changed(lambda data, s=section, v=bad_value: data.__setitem__(s, v))

    def test_rejects_missing_top_level_and_nested_keys(self):
        top_level = (
            "timeouts",
            "retries",
            "recursion_limit",
            "command_lease_seconds",
            "customer_session",
            "customer_handoff",
            "required_fields",
            "manual_gate_actions",
        )
        for key in top_level:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load_changed(lambda data, k=key: data.pop(k))
        for section, key in (
            ("timeouts", "agent_seconds"),
            ("timeouts", "readonly_tool_seconds"),
            ("timeouts", "graph_seconds"),
            ("retries", "triage"),
            ("retries", "diagnosis"),
            ("retries", "readonly_tool"),
            ("retries", "review"),
            ("customer_session", "inactivity_timeout_seconds"),
            ("customer_session", "expiry_poll_seconds"),
            ("customer_handoff", "ai_attempt_threshold"),
        ):
            with self.subTest(section=section, key=key), self.assertRaises(ValueError):
                self.load_changed(lambda data, s=section, k=key: data[s].pop(k))

    def test_rejects_bool_and_non_integer_limits(self):
        mutations = (
            lambda data: data["timeouts"].__setitem__("agent_seconds", True),
            lambda data: data["timeouts"].__setitem__("graph_seconds", 1.5),
            lambda data: data["retries"].__setitem__("triage", False),
            lambda data: data["retries"].__setitem__("diagnosis", "1"),
            lambda data: data.__setitem__("recursion_limit", True),
            lambda data: data.__setitem__("command_lease_seconds", "130"),
            lambda data: data["customer_session"].__setitem__("inactivity_timeout_seconds", True),
            lambda data: data["customer_handoff"].__setitem__("ai_attempt_threshold", "3"),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(TypeError):
                self.load_changed(mutation)

    def test_rejects_non_positive_limits_and_negative_retries(self):
        mutations = (
            lambda data: data["timeouts"].__setitem__("agent_seconds", 0),
            lambda data: data["timeouts"].__setitem__("readonly_tool_seconds", -1),
            lambda data: data["timeouts"].__setitem__("graph_seconds", 0),
            lambda data: data["retries"].__setitem__("triage", -1),
            lambda data: data.__setitem__("recursion_limit", 0),
            lambda data: data.__setitem__("command_lease_seconds", -1),
            lambda data: data["customer_session"].__setitem__("expiry_poll_seconds", 0),
            lambda data: data["customer_handoff"].__setitem__("ai_attempt_threshold", 0),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.load_changed(mutation)

    def test_review_retry_must_be_zero(self):
        with self.assertRaises(ValueError):
            self.load_changed(lambda data: data["retries"].__setitem__("review", 1))

    def test_command_lease_must_exceed_graph_timeout(self):
        for lease in (119, 120):
            with self.subTest(lease=lease), self.assertRaises(ValueError):
                self.load_changed(lambda data, value=lease: data.__setitem__("command_lease_seconds", value))

    def test_rejects_positive_but_non_fixed_execution_limits(self):
        mutations = (
            lambda data: data["timeouts"].__setitem__("agent_seconds", 21),
            lambda data: data["timeouts"].__setitem__("readonly_tool_seconds", 9),
            lambda data: data["timeouts"].__setitem__("graph_seconds", 121),
            lambda data: data["retries"].__setitem__("triage", 2),
            lambda data: data["retries"].__setitem__("diagnosis", 2),
            lambda data: data["retries"].__setitem__("readonly_tool", 2),
            lambda data: data.__setitem__("recursion_limit", 17),
            lambda data: data.__setitem__("command_lease_seconds", 131),
            lambda data: data["customer_session"].__setitem__("inactivity_timeout_seconds", 1799),
            lambda data: data["customer_session"].__setitem__("expiry_poll_seconds", 31),
            lambda data: data["customer_handoff"].__setitem__("ai_attempt_threshold", 4),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.load_changed(mutation)

    def test_required_fields_must_be_non_empty_string_list_mapping(self):
        bad_values = (
            [],
            {},
            {1: ["device_model"]},
            {"": ["device_model"]},
            {"firmware_repair": "device_model"},
            {"firmware_repair": []},
            {"firmware_repair": [1]},
            {"firmware_repair": [" "]},
        )
        for value in bad_values:
            expected = TypeError if value == [] or value == {1: ["device_model"]} or value == {"firmware_repair": "device_model"} or value == {"firmware_repair": [1]} else ValueError
            with self.subTest(value=value), self.assertRaises(expected):
                self.load_changed(lambda data, v=value: data.__setitem__("required_fields", v))

    def test_required_fields_rejects_each_missing_intent_and_field(self):
        for intent in VALID_ORCHESTRATION["required_fields"]:
            with self.subTest(intent=intent), self.assertRaises(ValueError):
                self.load_changed(lambda data, key=intent: data["required_fields"].pop(key))

        for intent, fields in VALID_ORCHESTRATION["required_fields"].items():
            for field in fields:
                with self.subTest(intent=intent, field=field), self.assertRaises(ValueError):
                    self.load_changed(
                        lambda data, key=intent, value=field: data["required_fields"][key].remove(value)
                    )

    def test_required_fields_rejects_extra_and_duplicate_entries(self):
        mutations = (
            lambda data: data["required_fields"].__setitem__("other", ["field"]),
            lambda data: data["required_fields"]["firmware_repair"].append("extra"),
            lambda data: data["required_fields"]["firmware_repair"].append("device_model"),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.load_changed(mutation)

    def test_manual_gate_actions_must_be_non_empty_strings(self):
        bad_values = ("device_reset", [], [1], [""], [" "])
        for value in bad_values:
            expected = TypeError if isinstance(value, str) or value == [1] else ValueError
            with self.subTest(value=value), self.assertRaises(expected):
                self.load_changed(lambda data, v=value: data.__setitem__("manual_gate_actions", v))

    def test_manual_gate_actions_rejects_each_missing_extra_and_duplicate(self):
        for action in VALID_ORCHESTRATION["manual_gate_actions"]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.load_changed(lambda data, value=action: data["manual_gate_actions"].remove(value))

        for value in ("factory_reset", "device_reset"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load_changed(lambda data, action=value: data["manual_gate_actions"].append(action))


class SecurityPolicyTest(ConfigTestCase):
    def load_changed(self, mutator):
        data = copy.deepcopy(VALID_SECURITY_POLICY)
        mutator(data)
        return load_security_policy(self.write_yaml(data))

    def test_loads_canonical_official_domains(self):
        policy = load_security_policy()
        self.assertIn("support.ledger.com", policy["official_domains"])

    def test_rejects_missing_file_and_malformed_yaml(self):
        with self.assertRaises(FileNotFoundError):
            load_security_policy("/definitely/missing/security.yml")
        with self.assertRaises(yaml.YAMLError):
            load_security_policy(self.write_raw("official_domains: [unterminated"))

    def test_rejects_missing_required_fields(self):
        for field in (
            "policy_version",
            "official_domains",
            "critical_response_template_zh",
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load_changed(lambda data, key=field: data.pop(key))

    def test_policy_version_and_template_must_be_non_empty_strings(self):
        for field in ("policy_version", "critical_response_template_zh"):
            for value in (None, 1, "", " "):
                expected = TypeError if not isinstance(value, str) else ValueError
                with self.subTest(field=field, value=value), self.assertRaises(expected):
                    self.load_changed(lambda data, f=field, v=value: data.__setitem__(f, v))

    def test_official_domains_must_be_non_empty_string_list(self):
        for value in ("ledger.com", 1, [], [1], [""], [" "]):
            expected = TypeError if isinstance(value, (str, int)) or value == [1] else ValueError
            with self.subTest(value=value), self.assertRaises(expected):
                self.load_changed(lambda data, v=value: data.__setitem__("official_domains", v))

    def test_rejects_non_canonical_hostnames(self):
        invalid_domains = (
            "https://ledger.com",
            "ledger.com/help",
            "ledger.com:443",
            "Ledger.com",
            " ledger.com",
            "ledger.com ",
            "ledger..com",
            "-ledger.com",
            "ledger-.com",
            "localhost",
        )
        for domain in invalid_domains:
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                self.load_changed(lambda data, value=domain: data.__setitem__("official_domains", [value]))


if __name__ == "__main__":
    unittest.main()
