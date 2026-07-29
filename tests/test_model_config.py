import unittest

from utils.model_config import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_CHAT_MODEL,
    DEFAULT_DEEPSEEK_THINKING,
    DEFAULT_EMBEDDING_PROVIDER,
    DEFAULT_LOCAL_EMBEDDING_DIMENSION,
    DEFAULT_MIMO_CHAT_MODEL,
    build_chat_config,
    normalize_provider,
)


class ModelConfigTest(unittest.TestCase):
    def test_deepseek_config_uses_v4_flash_non_thinking_defaults(self):
        config = build_chat_config(
            provider="deepseek",
            dashscope_key="dashscope-key",
            mimo_key="mimo-key",
            mimo_base_url=None,
            mimo_model_name=None,
            deepseek_key="deepseek-test-key",
        )

        self.assertTrue(config.is_configured)
        self.assertEqual(config.provider, "deepseek")
        self.assertEqual(config.kwargs["provider"], "deepseek")
        self.assertEqual(config.kwargs["base_url"], DEFAULT_DEEPSEEK_BASE_URL)
        self.assertEqual(config.kwargs["model_name"], DEFAULT_DEEPSEEK_CHAT_MODEL)
        self.assertEqual(config.kwargs["thinking"], DEFAULT_DEEPSEEK_THINKING)
        self.assertEqual(DEFAULT_DEEPSEEK_BASE_URL, "https://api.deepseek.com")
        self.assertEqual(DEFAULT_DEEPSEEK_CHAT_MODEL, "deepseek-v4-flash")
        self.assertEqual(DEFAULT_DEEPSEEK_THINKING, "disabled")

    def test_deepseek_is_a_distinct_provider(self):
        self.assertEqual(normalize_provider("deepseek"), "deepseek")
        self.assertEqual(normalize_provider("mimo"), "mimo")
        self.assertEqual(normalize_provider("dashscope"), "dashscope")

    def test_deepseek_does_not_borrow_another_provider_key(self):
        config = build_chat_config(
            provider="deepseek",
            dashscope_key="dashscope-key",
            mimo_key="mimo-key",
            mimo_base_url=None,
            mimo_model_name=None,
            deepseek_key="",
        )

        self.assertFalse(config.is_configured)
        self.assertEqual(config.kwargs, {})

    def test_deepseek_rejects_invalid_thinking_mode(self):
        with self.assertRaises(ValueError):
            build_chat_config(
                provider="deepseek",
                dashscope_key=None,
                mimo_key=None,
                mimo_base_url=None,
                mimo_model_name=None,
                deepseek_key="deepseek-test-key",
                deepseek_thinking="sometimes",
            )

    def test_deepseek_signature_does_not_contain_raw_api_key(self):
        config = build_chat_config(
            provider="deepseek",
            dashscope_key=None,
            mimo_key=None,
            mimo_base_url=None,
            mimo_model_name=None,
            deepseek_key="deepseek-test-key",
        )

        self.assertIn("deepseek:", config.signature)
        self.assertNotIn("deepseek-test-key", config.signature)

    def test_mimo_config_uses_token_plan_default_model(self):
        config = build_chat_config(
            provider="mimo",
            dashscope_key="expired-dashscope-key",
            mimo_key="tp-secret-key",
            mimo_base_url=None,
            mimo_model_name=None,
        )

        self.assertTrue(config.is_configured)
        self.assertEqual(config.kwargs["provider"], "mimo")
        self.assertEqual(config.kwargs["model_name"], DEFAULT_MIMO_CHAT_MODEL)
        self.assertEqual(DEFAULT_MIMO_CHAT_MODEL, "mimo-v2.5-pro")

    def test_signature_does_not_contain_raw_api_key(self):
        config = build_chat_config(
            provider="mimo",
            dashscope_key=None,
            mimo_key="tp-secret-key",
            mimo_base_url="https://token-plan-sgp.xiaomimimo.com/v1",
            mimo_model_name="mimo-v2.5-pro",
        )

        self.assertIn("mimo:", config.signature)
        self.assertNotIn("tp-secret-key", config.signature)

    def test_selected_dashscope_does_not_get_overridden_by_mimo_key(self):
        config = build_chat_config(
            provider="dashscope",
            dashscope_key="dashscope-key",
            mimo_key="tp-secret-key",
            mimo_base_url=None,
            mimo_model_name=None,
        )

        self.assertTrue(config.is_configured)
        self.assertEqual(config.kwargs["provider"], "dashscope")
        self.assertEqual(config.kwargs["api_key"], "dashscope-key")

    def test_missing_selected_provider_key_is_unconfigured(self):
        config = build_chat_config(
            provider="mimo",
            dashscope_key="dashscope-key",
            mimo_key="",
            mimo_base_url=None,
            mimo_model_name=None,
        )

        self.assertFalse(config.is_configured)
        self.assertEqual(config.kwargs, {})

    def test_default_embedding_provider_runs_without_dashscope_key(self):
        self.assertEqual(DEFAULT_EMBEDDING_PROVIDER, "local")

    def test_default_local_embedding_dimension_matches_existing_vector_store(self):
        self.assertEqual(DEFAULT_LOCAL_EMBEDDING_DIMENSION, 1024)


if __name__ == "__main__":
    unittest.main()
