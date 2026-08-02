import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from agent.orchestration.structured_output import unwrap_structured_output


def wrapped_output(
    tool_name="TriageResult",
    args=None,
    *,
    parsed=None,
    parsing_error=None,
    tool_calls=None,
):
    if tool_calls is None:
        tool_calls = [
            {
                "name": tool_name,
                "args": {"value": "raw"} if args is None else args,
                "id": "call-1",
                "type": "tool_call",
            }
        ]
    return {
        "raw": AIMessage(content="", tool_calls=tool_calls),
        "parsed": parsed,
        "parsing_error": parsing_error,
    }


class StructuredOutputCompatibilityTest(unittest.TestCase):
    def test_direct_results_remain_supported_for_injected_runners(self):
        candidate = {"value": "direct"}

        self.assertIs(
            unwrap_structured_output(candidate, "TriageResult", agent_name="triage"),
            candidate,
        )

    def test_successfully_parsed_result_uses_fast_path(self):
        parsed = {"value": "parsed"}
        wrapper = wrapped_output(
            args={"value": "raw"},
            parsed=parsed,
            parsing_error=None,
        )

        self.assertIs(
            unwrap_structured_output(wrapper, "TriageResult", agent_name="triage"),
            parsed,
        )

    def test_validation_failure_extracts_unique_expected_tool_args(self):
        args = {"value": "recoverable"}
        wrapper = wrapped_output(args=args, parsing_error=ValueError("invalid"))

        self.assertEqual(
            unwrap_structured_output(wrapper, "TriageResult", agent_name="triage"),
            args,
        )

    def test_unknown_multiple_or_missing_tool_calls_are_rejected(self):
        invalid_wrappers = (
            wrapped_output(tool_name="OtherResult", parsing_error=ValueError()),
            wrapped_output(tool_calls=[], parsing_error=ValueError()),
            wrapped_output(
                tool_calls=[
                    {
                        "name": "TriageResult",
                        "args": {"value": "one"},
                        "id": "call-1",
                        "type": "tool_call",
                    },
                    {
                        "name": "TriageResult",
                        "args": {"value": "two"},
                        "id": "call-2",
                        "type": "tool_call",
                    },
                ],
                parsing_error=ValueError(),
            ),
        )

        for wrapper in invalid_wrappers:
            with self.subTest(wrapper=wrapper), self.assertRaises(ValueError):
                unwrap_structured_output(
                    wrapper, "TriageResult", agent_name="triage"
                )

    def test_plain_text_and_non_mapping_args_are_rejected(self):
        plain_text = {
            "raw": AIMessage(content="plain text", tool_calls=[]),
            "parsed": None,
            "parsing_error": ValueError(),
        }
        invalid_args = {
            "raw": SimpleNamespace(
                tool_calls=[
                    {
                        "name": "TriageResult",
                        "args": "not-an-object",
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ]
            ),
            "parsed": None,
            "parsing_error": ValueError(),
        }

        for wrapper in (plain_text, invalid_args):
            with self.subTest(wrapper=wrapper), self.assertRaises(
                (TypeError, ValueError)
            ):
                unwrap_structured_output(
                    wrapper, "TriageResult", agent_name="triage"
                )


if __name__ == "__main__":
    unittest.main()
