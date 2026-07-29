import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AppV2ContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "app.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    @classmethod
    def function_source(cls, name: str) -> str:
        function = next(
            node
            for node in cls.tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        )
        return ast.get_source_segment(cls.source, function) or ""

    def test_app_has_customer_and_workbench_tabs(self):
        self.assertIn('"客户对话"', self.source)
        self.assertIn('"工单工作台"', self.source)

    def test_v2_is_default_and_v1_is_only_explicit_compatibility_path(self):
        self.assertIn('or "v2"', self.source)
        self.assertIn('AGENT_VERSION == "v1"', self.source)
        self.assertIn("get_or_build_orchestrator", self.source)
        self.assertIn("get_or_build_agent", self.source)

        old_builder_calls = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_or_build_agent"
        ]
        self.assertEqual(len(old_builder_calls), 1)

    def test_app_never_renders_internal_agent_stream_events(self):
        for forbidden in (
            'kind == "thought"',
            'kind == "tool_call"',
            'kind == "tool_result"',
            "status.markdown",
            "**💭 思考**",
        ):
            self.assertNotIn(forbidden, self.source)

    def test_raw_prompt_only_reaches_runtime_before_safe_result_is_stored(self):
        source = self.function_source("_run_v2_prompt")
        function = ast.parse(source).body[0]
        prompt_calls = []
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if not any(
                isinstance(name, ast.Name) and name.id == "prompt"
                for argument in [*call.args, *[item.value for item in call.keywords]]
                for name in ast.walk(argument)
            ):
                continue
            prompt_calls.append(ast.get_source_segment(source, call) or "")

        self.assertTrue(prompt_calls)
        self.assertTrue(
            all(
                "orchestrator.submit(" in call
                or "orchestrator.resume_user(" in call
                for call in prompt_calls
            ),
            prompt_calls,
        )
        self.assertIn('"content": result.sanitized_input', source)
        self.assertNotIn('"content": prompt', source)
        self.assertNotIn("st.markdown(prompt", source)
        self.assertNotIn("logger.", source.split("except", 1)[0])
        self.assertLess(source.index("try:"), source.index("repository.get_ticket"))

    def test_v2_failure_is_fail_closed_without_exception_or_v1_fallback(self):
        source = self.function_source("_run_v2_prompt")
        handler = source.split("except", 1)[1]
        self.assertIn("SAFE_FAILURE_NOTICE", handler)
        self.assertNotIn("get_or_build_agent", handler)
        self.assertNotIn("format_agent_error", handler)
        self.assertNotIn("str(error)", handler)
        self.assertNotIn("{error}", handler)
        self.assertNotIn("prompt", handler)

    def test_builder_separates_databases_and_shares_policy_objects(self):
        source = self.function_source("get_or_build_orchestrator")
        self.assertIn("KEYGUARD_TICKET_DB", source)
        self.assertIn("KEYGUARD_CHECKPOINT_DB", source)
        self.assertIn("keyguard_v2.db", source)
        self.assertIn("keyguard_v2_checkpoints.sqlite3", source)
        self.assertIn("TrustedSourcePolicy(", source)
        self.assertIn("PolicyGuard(", source)
        self.assertIn("trusted_source_policy=trusted_sources", source)
        self.assertIn("policy_guard=policy_guard", source)
        self.assertIn("final_policy_guard=policy_guard", source)
        self.assertIn("recursion_limit=orchestration[", source)
        self.assertIn("lease_seconds=orchestration[", source)
        self.assertIn("graph_timeout_seconds=orchestration[", source)
        self.assertIn("checkpointer.close()", source)

    def test_workbench_only_lists_safe_event_fields_and_human_actions(self):
        source = self.function_source("_render_workbench")
        for field in ("event_type", "summary", "from_status", "to_status"):
            self.assertIn(field, source)
        for label in ("Approve", "Edit & Send", "Ask User", "Reject"):
            self.assertIn(label, source)
        self.assertIn("orchestrator.get_verified_citations(", source)
        self.assertNotIn("orchestrator.get_state(", source)
        self.assertIn("orchestrator.human_action(", self.source)
        for forbidden in (
            "metadata_json",
            "draft_prompt",
            "tool_args",
            "model_output",
        ):
            self.assertNotIn(forbidden, source)

    def test_customer_phase_labels_match_graph_event_types(self):
        for event_type in (
            "entry_checked",
            "triage_completed",
            "diagnosis_completed",
            "review_completed",
            "escalated",
            "response_finalized",
        ):
            self.assertIn(f'"{event_type}"', self.source)

    def test_configured_graph_rejects_mismatched_policy_identity(self):
        from agent.orchestration.graph import build_configured_graph
        from agent.policies.security import PolicyGuard
        from agent.security.trusted_sources import TrustedSourcePolicy
        from utils.config_handler import (
            load_orchestration_config,
            load_security_policy,
        )

        policy = load_security_policy()
        orchestration = load_orchestration_config()
        graph_sources = TrustedSourcePolicy(
            policy.get("trusted_evidence_domains"),
            policy.get("trusted_evidence_url_prefixes"),
        )
        other_sources = TrustedSourcePolicy(
            policy.get("trusted_evidence_domains"),
            policy.get("trusted_evidence_url_prefixes"),
        )
        guard = PolicyGuard(policy, trusted_source_policy=other_sources)

        with self.assertRaisesRegex(ValueError, "必须共享"):
            build_configured_graph(
                object(),
                policy,
                orchestration,
                object(),
                trusted_source_policy=graph_sources,
                policy_guard=guard,
            )


if __name__ == "__main__":
    unittest.main()
