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

    def test_app_imports_json_for_persisted_ticket_fields(self):
        self.assertIn("import json", self.source)

    def test_explicit_empty_environment_secret_does_not_probe_streamlit_secrets(self):
        runtime_secret = self.function_source("_runtime_secret")

        self.assertIn("if name in os.environ", runtime_secret)
        self.assertIn("st.secrets.load_if_toml_exists()", runtime_secret)
        self.assertLess(
            runtime_secret.index("if name in os.environ"),
            runtime_secret.index("st.secrets.load_if_toml_exists()"),
        )
        self.assertLess(
            runtime_secret.index("st.secrets.load_if_toml_exists()"),
            runtime_secret.index("st.secrets.get"),
        )

    def test_provider_secrets_are_loaded_only_inside_selected_provider_branches(self):
        provider_loader = self.function_source("_load_provider_runtime_config")
        for provider, secret in (
            ("deepseek", "DEEPSEEK_API_KEY"),
            ("mimo", "MIMO_API_KEY"),
            ("dashscope", "DASHSCOPE_API_KEY"),
        ):
            self.assertIn(f'provider == "{provider}"', provider_loader)
            self.assertIn(f'_runtime_secret("{secret}")', provider_loader)

        top_level_provider_secret_calls = []
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for call in (
                item for item in ast.walk(node) if isinstance(item, ast.Call)
            ):
                if not (
                    isinstance(call.func, ast.Name)
                    and call.func.id == "_runtime_secret"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                ):
                    continue
                name = call.args[0].value
                if isinstance(name, str) and name.startswith(
                    ("DEEPSEEK_", "MIMO_", "DASHSCOPE_")
                ):
                    top_level_provider_secret_calls.append(name)

        self.assertEqual(top_level_provider_secret_calls, [])

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
                "orchestrator.prepare_user_input(" in call
                or "get_or_freeze_request_command(" in call
                for call in prompt_calls
            ),
            prompt_calls,
        )
        self.assertIn("orchestrator.submit_prepared(", source)
        self.assertIn("orchestrator.resume_user_prepared(", source)
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

    def test_orchestrator_cache_key_includes_contract_version(self):
        source = self.function_source("get_or_build_orchestrator")

        self.assertIn(
            'ORCHESTRATOR_CONTRACT_VERSION = "2026-08-03-low-risk-fallback-v10"',
            self.source,
        )
        self.assertIn("contract_version: str", source)
        self.assertIn(
            "contract_version != ORCHESTRATOR_CONTRACT_VERSION",
            source,
        )
        self.assertIn(
            "get_or_build_orchestrator(\n"
            "            current_model_signature, ORCHESTRATOR_CONTRACT_VERSION\n"
            "        )",
            self.source,
        )

    def test_workbench_only_lists_safe_event_fields_and_human_actions(self):
        source = self.function_source("_render_workbench")
        for field in ("event_type", "summary", "from_status", "to_status"):
            self.assertIn(field, source)
        for label in ("Approve", "Edit & Send", "Ask User", "Reject"):
            self.assertIn(label, source)
        self.assertIn("orchestrator.get_verified_citations(", source)
        self.assertNotIn("orchestrator.get_state(", source)
        self.assertIn("orchestrator.human_action_prepared(", self.source)
        for forbidden in (
            "metadata_json",
            "draft_prompt",
            "tool_args",
            "model_output",
        ):
            self.assertNotIn(forbidden, source)

    def test_human_results_are_projected_back_to_customer_chat(self):
        action = self.function_source("_run_human_action")
        sync = self.function_source("_sync_human_result_to_customer_chat")
        self.assertIn("result = orchestrator.human_action_prepared(", action)
        self.assertIn("_sync_human_result_to_customer_chat(result)", action)
        self.assertIn('result.status == "resolved"', sync)
        self.assertIn('result.status == "pending_user"', sync)
        self.assertIn('messages.append({"role": "assistant"', sync)

    def test_workbench_explains_queue_scope_and_follow_up_links(self):
        source = self.function_source("_render_workbench")
        for label in ("待处理", "全部", "已解决历史", "conversation_id", "parent_ticket_id"):
            self.assertIn(label, source)

    def test_workbench_defaults_to_demo_access_without_operator_token(self):
        gate = self.function_source("_render_operator_gate")
        workbench = self.function_source("_render_workbench")
        action = self.function_source("_run_human_action")
        self.assertIn("KEYGUARD_OPERATOR_TOKEN", self.source)
        self.assertIn("hmac.compare_digest", gate)
        self.assertIn("if not configured_token", gate)
        self.assertIn("return True", gate)
        self.assertIn("退出工作台", gate)
        self.assertLess(
            workbench.index("_operator_is_authorized"),
            workbench.index("list_workbench_tickets"),
        )
        self.assertLess(
            action.index("_operator_is_authorized"),
            action.index("orchestrator.human_action"),
        )

    def test_customer_ticket_ownership_is_checked_on_submit_and_render(self):
        submit = self.function_source("_run_v2_prompt")
        render = self.function_source("_render_active_ticket")
        self.assertIn('active_ticket.get("user_id") != user_id', submit)
        self.assertIn('ticket.get("user_id") != user_id', render)
        self.assertIn("_clear_customer_workflow_state()", submit)
        self.assertIn("_clear_customer_workflow_state()", render)

    def test_ui_persists_and_passes_stable_idempotency_ids(self):
        submit = self.function_source("_run_v2_prompt")
        action = self.function_source("_run_human_action")
        self.assertIn("_stable_request_id()", submit)
        self.assertEqual(submit.count("request_id=request_id"), 3)
        self.assertIn("get_or_freeze_request_command(", submit)
        self.assertIn("orchestrator.prepare_user_input(prompt)", submit)
        self.assertIn("orchestrator.submit_prepared(", submit)
        self.assertIn("orchestrator.resume_user_prepared(", submit)
        self.assertIn("safe_history=safe_history", submit)
        self.assertIn("clear_frozen_request(st.session_state)", submit)
        self.assertIn("CommandInProgressError", submit)
        self.assertIn("IdempotencyConflictError", submit)
        self.assertIn("_stable_action_id(", action)
        self.assertIn("get_or_freeze_action_command(", action)
        self.assertIn("orchestrator.prepare_human_action(", action)
        self.assertIn("orchestrator.human_action_prepared(", action)
        self.assertIn("action_id=action_id", action)
        self.assertIn("clear_frozen_action(st.session_state, action_key)", action)
        self.assertIn("CommandInProgressError", action)
        self.assertIn("IdempotencyConflictError", action)
        conflict_handler = submit.split("except IdempotencyConflictError", 1)[1].split(
            "except", 1
        )[0]
        action_conflict_handler = action.split(
            "except IdempotencyConflictError", 1
        )[1].split("except", 1)[0]
        self.assertNotIn("clear_frozen_request", conflict_handler)
        self.assertNotIn("clear_frozen_action", action_conflict_handler)
        self.assertIn("RECOVERING_REQUEST_NOTICE", submit)
        self.assertIn("RECOVERED_REQUEST_NOTICE", submit)
        terminal_handler = submit.split(
            "except (CommandFailedError, CheckpointRestoreError)", 1
        )[1].split("except", 1)[0]
        self.assertIn(
            "st.session_state.pop(RECOVERY_NOTICE_SESSION_KEY, None)",
            terminal_handler,
        )
        self.assertIn("RECOVERING_REQUEST_NOTICE", action)
        for cleanup in (
            'st.session_state.pop("active_ticket_id", None)',
            "clear_frozen_request(st.session_state)",
            "clear_frozen_actions(st.session_state)",
        ):
            self.assertIn(cleanup, self.function_source("_clear_customer_workflow_state"))

    def test_citation_markdown_fields_are_escaped(self):
        source = self.function_source("_answer_with_citations")
        self.assertIn("_escape_markdown_text", source)
        self.assertIn("_escape_markdown_url", source)
        self.assertIn("[", source)

    def test_answer_projection_tolerates_stale_cached_result_contract(self):
        source = self.function_source("_answer_with_citations")

        for field in (
            "status",
            "user_notice",
            "final_answer",
            "clarification_question",
            "clarification_options",
            "missing_fields",
            "citations",
        ):
            self.assertIn(f'getattr(result, "{field}"', source)

    def test_workbench_displays_only_repository_projected_draft(self):
        source = self.function_source("_render_workbench")
        self.assertIn("list_workbench_tickets", source)
        self.assertNotIn("repository.list_tickets", source)
        self.assertIn('draft_answer = ticket.get("draft_answer")', source)
        self.assertIn("st.text(draft_answer)", source)
        self.assertIn("value=draft_answer or", source)

    def test_editor_uses_clear_on_submit_form_and_safe_prefix_cleanup(self):
        workbench = self.function_source("_render_workbench")
        workflow_cleanup = self.function_source("_clear_customer_workflow_state")
        operator_gate = self.function_source("_render_operator_gate")

        self.assertIn("with st.form(", workbench)
        self.assertIn("clear_on_submit=True", workbench)
        self.assertIn("st.form_submit_button(", workbench)
        self.assertIn("EDITOR_SESSION_PREFIX", workbench)
        self.assertNotIn("st.session_state.pop", workbench)
        self.assertIn("clear_editor_state(st.session_state)", workflow_cleanup)
        self.assertEqual(
            operator_gate.count("clear_editor_state(st.session_state)"), 2
        )

    def test_profiles_are_read_and_saved_by_user_id_without_module_global(self):
        self.assertNotIn("utils.user_profile", self.source)
        self.assertIn("profile_db.get_profile(uid)", self.source)
        self.assertIn("profile_db.save_profile(new_profile)", self.source)
        for key in (
            "profile_experience_level_{uid}",
            "profile_passphrase_enabled_{uid}",
            "profile_connection_method_{uid}",
            "profile_backup_verified_{uid}",
            "profile_region_{uid}",
            "profile_device_{uid}",
            "profile_chains_{uid}",
            "save_profile_{uid}",
        ):
            self.assertIn(key, self.source)

    def test_env_example_documents_operator_token(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("KEYGUARD_OPERATOR_TOKEN=", env_example)

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

    def test_customer_progress_has_persisted_placeholder_and_safe_projection(self):
        submit = self.function_source("_run_v2_prompt")
        render = self.function_source("_render_active_ticket")
        self.assertIn("append_processing_turn", submit)
        self.assertIn("replace_processing_answer", submit)
        self.assertIn("PENDING_UI_REQUEST_SESSION_KEY", submit)
        self.assertIn('ui_container.chat_message("user"', submit)
        self.assertIn('ui_container.chat_message("assistant"', submit)
        self.assertIn("ui_container.status(", submit)
        self.assertIn("project_customer_phases", render)
        self.assertIn("project_workbench_events", self.function_source("_render_workbench"))

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
