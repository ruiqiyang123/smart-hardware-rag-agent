import tempfile
import unittest
from pathlib import Path

from utils.config_handler import load_orchestration_config, load_security_policy


class OrchestrationConfigTest(unittest.TestCase):
    def test_loads_fixed_execution_limits(self):
        config = load_orchestration_config()
        self.assertEqual(config["timeouts"]["agent_seconds"], 20)
        self.assertEqual(config["timeouts"]["readonly_tool_seconds"], 8)
        self.assertEqual(config["timeouts"]["graph_seconds"], 120)
        self.assertEqual(config["recursion_limit"], 16)
        self.assertEqual(config["command_lease_seconds"], 130)
        self.assertEqual(config["retries"]["review"], 0)

    def test_security_policy_rejects_empty_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "security.yml"
            path.write_text(
                'policy_version: "test"\nofficial_domains: []\n'
                'critical_response_template_zh: "安全提示"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_security_policy(str(path))


if __name__ == "__main__":
    unittest.main()
