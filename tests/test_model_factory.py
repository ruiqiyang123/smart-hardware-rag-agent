import unittest
from unittest.mock import patch

from model.factory import ChatModelFactory


class ChatModelFactoryTest(unittest.TestCase):
    @patch("model.factory.ChatOpenAI")
    def test_deepseek_factory_sets_non_thinking_request_body(self, chat_openai):
        built = object()
        chat_openai.return_value = built

        result = ChatModelFactory.create(
            provider="deepseek",
            api_key="deepseek-test-key",
            base_url="https://api.deepseek.com",
            model_name="deepseek-v4-flash",
            thinking="disabled",
        )

        self.assertIs(result, built)
        chat_openai.assert_called_once_with(
            model="deepseek-v4-flash",
            api_key="deepseek-test-key",
            base_url="https://api.deepseek.com",
            temperature=0,
            extra_body={"thinking": {"type": "disabled"}},
        )

    def test_deepseek_factory_rejects_invalid_thinking_mode(self):
        with self.assertRaises(ValueError):
            ChatModelFactory.create(
                provider="deepseek",
                api_key="deepseek-test-key",
                base_url="https://api.deepseek.com",
                model_name="deepseek-v4-flash",
                thinking="sometimes",
            )


if __name__ == "__main__":
    unittest.main()
