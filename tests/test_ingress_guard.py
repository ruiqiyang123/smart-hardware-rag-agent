import tempfile
import unittest
from pathlib import Path

from agent.policies.security import (
    MINIMAL_FAILURE_NOTICE,
    IngressGuard,
    PolicyGuard,
)
from database.ticket_db import TicketRepository


MNEMONIC_WORDS = (
    "abandon ability able about above absent absorb abstract absurd abuse access accident"
)
HEX_SECRET = "1" * 64
WIF_SECRET = "5" + "H" * 50
TRANSACTION_HASH = "0x" + "a" * 64


class WalletSafetyGuardTest(unittest.TestCase):
    def setUp(self):
        self.policy = {
            "policy_version": "2026-07-28.v1",
            "official_domains": ["support.ledger.com", "trezor.io"],
            "critical_response_template_zh": "固定安全提示",
        }
        self.ingress = IngressGuard(self.policy)
        self.output = PolicyGuard(self.policy)

    def test_redacts_bip39_before_persistence(self):
        result = self.ingress.sanitize(f"助记词是 {MNEMONIC_WORDS}")

        self.assertEqual(result.risk_level, "critical")
        self.assertEqual(result.risk_flags, ["secret_exposure"])
        self.assertNotIn(MNEMONIC_WORDS, result.sanitized_input)
        self.assertIn("[REDACTED_SECRET]", result.sanitized_input)
        self.assertEqual(result.critical_notice, "固定安全提示")

    def test_redacts_all_bip39_word_counts_and_common_separators(self):
        words = MNEMONIC_WORDS.split()
        for word_count in (12, 15, 18, 21, 24):
            phrase = ", ".join(
                words[index % len(words)].upper() for index in range(word_count)
            )
            with self.subTest(word_count=word_count):
                result = self.ingress.sanitize(f"recovery phrase: {phrase}")
                self.assertEqual(result.risk_level, "critical")
                self.assertEqual(result.risk_flags, ["secret_exposure"])
                self.assertNotIn(phrase, result.sanitized_input)

    def test_normal_twelve_word_english_sentence_is_not_a_secret(self):
        message = "this support message contains twelve ordinary words but is not wallet recovery data"

        result = self.ingress.sanitize(message)

        self.assertEqual(result.sanitized_input, message)
        self.assertEqual(result.risk_level, "low")
        self.assertEqual(result.risk_flags, [])
        self.assertEqual(result.critical_notice, "")

    def test_redacts_private_key_wif_pin_and_passphrase(self):
        cases = (
            f"private key: {HEX_SECRET}",
            f"裸值 {HEX_SECRET}",
            f"裸值 0x{HEX_SECRET}",
            f"WIF: {WIF_SECRET}",
            "PIN 123456",
            "PIN 是 123456",
            "passphrase: correct horse battery staple",
            "password 为 do-not-store-this",
        )
        for index, raw_value in enumerate(cases):
            with self.subTest(case=index):
                result = self.ingress.sanitize(raw_value)
                self.assertEqual(result.risk_level, "critical")
                self.assertIn("secret_exposure", result.risk_flags)
                self.assertIn("[REDACTED_SECRET]", result.sanitized_input)
                self.assertNotEqual(result.sanitized_input, raw_value)

        prefixed = self.ingress.sanitize(f"裸值 0x{HEX_SECRET}")
        self.assertEqual(prefixed.sanitized_input, "裸值 [REDACTED_SECRET]")

    def test_classifies_deterministic_risk_flags_with_sticky_severity(self):
        result = self.ingress.sanitize(
            "陌生客服让我共享屏幕并安装非官方固件，签名地址不一致，资产已转出"
        )

        self.assertEqual(result.risk_level, "critical")
        self.assertEqual(
            result.risk_flags,
            [
                "phishing",
                "remote_control",
                "unofficial_firmware",
                "address_mismatch",
                "suspicious_signature",
                "asset_loss",
            ],
        )
        self.assertEqual(result.critical_notice, "固定安全提示")

    def test_high_risk_does_not_receive_critical_notice(self):
        result = self.ingress.sanitize("客服让我共享屏幕并远程控制设备")

        self.assertEqual(result.risk_level, "high")
        self.assertEqual(result.risk_flags, ["remote_control"])
        self.assertEqual(result.critical_notice, "")

    def test_phishing_is_sticky_critical_risk(self):
        result = self.ingress.sanitize("我可能进入了钓鱼网站")

        self.assertEqual(result.risk_level, "critical")
        self.assertEqual(result.risk_flags, ["phishing"])
        self.assertEqual(result.critical_notice, "固定安全提示")

    def test_pre_redacted_secret_is_not_reported_again(self):
        result = self.ingress.sanitize(
            "passphrase: [REDACTED_SECRET]，后续请走官方支持"
        )

        self.assertEqual(result.risk_level, "low")
        self.assertEqual(result.risk_flags, [])
        self.assertIn("[REDACTED_SECRET]", result.sanitized_input)

    def test_labeled_transaction_hash_is_preserved_and_can_be_persisted(self):
        raw_input = f"请查询交易哈希: {TRANSACTION_HASH} 的状态"

        result = self.ingress.sanitize(raw_input)

        self.assertEqual(result.sanitized_input, raw_input)
        self.assertEqual(result.risk_level, "low")
        self.assertEqual(result.risk_flags, [])
        with tempfile.TemporaryDirectory() as directory:
            repository = TicketRepository(Path(directory) / "tickets.db")
            ticket = repository.create_ticket(
                "transaction-request", "user-1", result.sanitized_input, []
            )
        self.assertEqual(ticket["sanitized_input"], raw_input)

    def test_transaction_hash_is_not_whitelisted_near_dangerous_secret_context(self):
        dangerous_inputs = (
            f"my private key is transaction hash: {TRANSACTION_HASH}",
            f"私钥如下\n交易哈希：{TRANSACTION_HASH}",
            f"交易哈希: {TRANSACTION_HASH} is my private key",
            f"seed phrase reference transaction hash: {TRANSACTION_HASH}",
            f"mnemonic reference transaction hash: {TRANSACTION_HASH}",
            f"助记词如下 transaction hash: {TRANSACTION_HASH}",
            f"passphrase reference transaction hash: {TRANSACTION_HASH}",
            f"PIN reference transaction hash: {TRANSACTION_HASH}",
            f"密码如下 transaction hash: {TRANSACTION_HASH}",
            f"口令如下 transaction hash: {TRANSACTION_HASH}",
            f"private key. transaction hash: {TRANSACTION_HASH}",
            f"交易哈希: {TRANSACTION_HASH}；这是我的私钥",
            f"private key {'x' * 300} transaction hash: {TRANSACTION_HASH}",
            f"私钥\n\n\n交易哈希：{TRANSACTION_HASH}",
        )
        for index, raw_input in enumerate(dangerous_inputs):
            with self.subTest(case=index):
                ingress = self.ingress.sanitize(raw_input)
                output = self.output.evaluate(raw_input, citation_urls=[])

                self.assertEqual(ingress.risk_level, "critical")
                self.assertEqual(ingress.risk_flags, ["secret_exposure"])
                self.assertNotIn(TRANSACTION_HASH, ingress.sanitized_input)
                self.assertFalse(output.passed)
                self.assertEqual(output.reason_codes, ["secret_exposure"])

    def test_dangerous_context_revokes_all_labeled_hash_exemptions(self):
        unsafe_hash = "0x" + "b" * 64
        raw_input = (
            f"请查询交易哈希: {TRANSACTION_HASH} 的状态。"
            f"my private key is transaction hash: {unsafe_hash}"
        )

        result = self.ingress.sanitize(raw_input)

        self.assertEqual(result.risk_level, "critical")
        self.assertNotIn(TRANSACTION_HASH, result.sanitized_input)
        self.assertNotIn(unsafe_hash, result.sanitized_input)
        self.assertEqual(result.sanitized_input.count("[REDACTED_SECRET]"), 2)

    def test_labeled_hash_does_not_whitelist_a_separate_bare_hash(self):
        unsafe_hash = "0x" + "c" * 64
        raw_input = (
            f"请查询交易哈希: {TRANSACTION_HASH} 的状态。"
            f"另一个未标记值是 {unsafe_hash}"
        )

        result = self.ingress.sanitize(raw_input)

        self.assertEqual(result.risk_level, "critical")
        self.assertIn(TRANSACTION_HASH, result.sanitized_input)
        self.assertNotIn(unsafe_hash, result.sanitized_input)
        self.assertEqual(result.sanitized_input.count("[REDACTED_SECRET]"), 1)

    def test_policy_allows_labeled_transaction_hash_in_normal_answer(self):
        text = f"交易哈希：{TRANSACTION_HASH}，可在官方浏览器中查询。"

        result = self.output.evaluate(text, citation_urls=[])

        self.assertTrue(result.passed)
        self.assertEqual(result.reason_codes, [])

    def test_transaction_hash_context_rejects_non_whitelisted_labels(self):
        for label in ("transaction_hash", "transaction-hash", "hash"):
            with self.subTest(label=label):
                result = self.ingress.sanitize(f"{label}: {TRANSACTION_HASH}")
                self.assertEqual(result.risk_level, "critical")
                self.assertEqual(result.risk_flags, ["secret_exposure"])
                self.assertNotIn(TRANSACTION_HASH, result.sanitized_input)

    def test_transaction_hash_label_cannot_hide_private_key_context(self):
        disguised_inputs = (
            f"private key: transaction hash: {TRANSACTION_HASH}",
            f"private key: txid: {TRANSACTION_HASH}",
            f"私钥：交易哈希：{TRANSACTION_HASH}",
            f"私钥：交易ID：{TRANSACTION_HASH}",
        )
        for index, disguised in enumerate(disguised_inputs):
            with self.subTest(case=index):
                sanitized = self.ingress.sanitize(disguised)
                policy = self.output.evaluate(disguised, citation_urls=[])

                self.assertEqual(sanitized.risk_level, "critical")
                self.assertEqual(sanitized.risk_flags, ["secret_exposure"])
                self.assertNotIn(TRANSACTION_HASH, sanitized.sanitized_input)
                self.assertFalse(policy.passed)
                self.assertEqual(policy.reason_codes, ["secret_exposure"])

    def test_empty_and_non_string_ingress_fail_closed_without_echo(self):
        for raw_value in ("", "   ", None, 123):
            with self.subTest(value_type=type(raw_value).__name__):
                with self.assertRaisesRegex(ValueError, "输入") as caught:
                    self.ingress.sanitize(raw_value)
                self.assertNotIn(repr(raw_value), str(caught.exception))

    def test_policy_rejects_unofficial_action_url(self):
        result = self.output.evaluate(
            "请从 https://evil.example/firmware 下载固件",
            citation_urls=[],
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_codes, ["official_source_violation"])

    def test_policy_allows_official_hosts_and_exact_retrieved_citation(self):
        citation = "https://ethereum.org/developers/docs/gas/"
        ledger_citation = "https://ledger.com/academy/security"
        text = (
            "参考 https://help.support.ledger.com/device?mode=safe，"
            f"以及检索资料：{citation} 和 {ledger_citation}。"
        )

        result = self.output.evaluate(
            text, citation_urls=[citation, ledger_citation]
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.reason_codes, [])

    def test_policy_allows_multiple_evidence_items_from_same_trusted_url(self):
        citation = "https://support.ledger.com/article/360019138694-zd"

        result = self.output.evaluate(
            "请按已验证的官方排查步骤操作。",
            citation_urls=[citation, citation],
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.reason_codes, [])

    def test_policy_rejects_suffix_attack_userinfo_http_and_unknown_query_url(self):
        unsafe_urls = (
            "https://ledger.com.evil.example/update",
            "https://ledger.com@evil.example/update",
            "https://attacker@support.ledger.com/update",
            "http://support.ledger.com/update",
            "ftp://support.ledger.com/update",
            "https://evil.example/update?source=ledger.com",
        )
        for index, url in enumerate(unsafe_urls):
            with self.subTest(case=index):
                result = self.output.evaluate(
                    f"下载地址：{url}。",
                    citation_urls=[],
                )
                self.assertFalse(result.passed)
                self.assertEqual(
                    result.reason_codes, ["official_source_violation"]
                )

    def test_policy_rejects_insecure_or_userinfo_citations(self):
        citations = (
            "http://ethereum.org/developers/docs/gas/",
            "https://attacker@ethereum.org/developers/docs/gas/",
            "https://evil.example/invented-source",
            "https://github.com/evil/project/blob/main/fake.md",
        )
        for index, citation in enumerate(citations):
            with self.subTest(case=index):
                result = self.output.evaluate(
                    f"参考：{citation}", citation_urls=[citation]
                )
                self.assertFalse(result.passed)
                self.assertEqual(
                    result.reason_codes, ["official_source_violation"]
                )

    def test_policy_rejects_untrusted_citation_even_when_answer_omits_it(self):
        result = self.output.evaluate(
            "这是一段没有链接的安全回答。",
            citation_urls=["https://evil.example/invented-source"],
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_codes, ["official_source_violation"])

    def test_policy_rejects_secrets_requests_and_unsafe_promises_stably(self):
        text = (
            f"请发送助记词，我可以破解 PIN 并保证追回。private key: {HEX_SECRET} "
            "然后远程控制你的设备。"
        )

        result = self.output.evaluate(text, citation_urls=[])

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_codes, ["secret_exposure", "unsafe_action"])

    def test_policy_allows_device_local_recovery_verification(self):
        text = (
            "请使用一台新的 KeyGuard 设备完成恢复。"
            "恢复后需要输入同一个 Passphrase 才能看到对应账户。"
            "不要在电脑、手机或网页输入，也不要向客服提供任何钱包秘密。"
        )

        result = self.output.evaluate(text, citation_urls=[])

        self.assertTrue(result.passed)
        self.assertEqual(result.reason_codes, [])

    def test_policy_still_rejects_recovery_secret_input_on_webpage(self):
        text = "使用新 KeyGuard 设备恢复前，请在网页输入 Passphrase。"

        result = self.output.evaluate(text, citation_urls=[])

        self.assertFalse(result.passed)
        self.assertEqual(result.reason_codes, ["unsafe_action"])

    def test_policy_empty_and_non_string_inputs_fail_closed_without_echo(self):
        for text, citations in (("", []), (None, []), (123, []), ("安全内容", None)):
            with self.subTest(text_type=type(text).__name__):
                result = self.output.evaluate(text, citations)
                self.assertFalse(result.passed)
                self.assertEqual(result.reason_codes, ["invalid_input"])

    def test_policy_configuration_is_strict_and_has_safe_errors(self):
        invalid_policies = (
            {},
            {
                "official_domains": ["ledger.com"],
                "critical_response_template_zh": "提示",
            },
            {
                "policy_version": "",
                "official_domains": ["ledger.com"],
                "critical_response_template_zh": "提示",
            },
            {
                "policy_version": 1,
                "official_domains": ["ledger.com"],
                "critical_response_template_zh": "提示",
            },
            {
                "policy_version": "2026-07-28.v1",
                "official_domains": [],
                "critical_response_template_zh": "提示",
            },
            {
                "policy_version": "2026-07-28.v1",
                "official_domains": ["ledger.com"],
                "critical_response_template_zh": "",
            },
            {
                "policy_version": "2026-07-28.v1",
                "official_domains": ["https://ledger.com"],
                "critical_response_template_zh": "提示",
            },
        )
        for index, policy in enumerate(invalid_policies):
            with self.subTest(case=index):
                with self.assertRaises((TypeError, ValueError)) as caught:
                    IngressGuard(policy)
                self.assertNotIsInstance(caught.exception, KeyError)
                self.assertNotEqual(str(caught.exception), "")

        invalid_domains = (
            " ledger.com",
            "ledger.com ",
            "Ledger.com",
            "ledger.com.",
            "ledger.com/path",
            "ledger.com:443",
        )
        for constructor in (IngressGuard, PolicyGuard):
            for domain in invalid_domains:
                policy = {
                    "policy_version": "2026-07-28.v1",
                    "official_domains": [domain],
                    "critical_response_template_zh": "提示",
                }
                with self.subTest(constructor=constructor.__name__, domain=domain):
                    with self.assertRaises(ValueError):
                        constructor(policy)

    def test_minimal_failure_notice_is_fixed_and_non_empty(self):
        self.assertIn("系统暂时无法安全处理", MINIMAL_FAILURE_NOTICE)
        self.assertIn("官方网站", MINIMAL_FAILURE_NOTICE)

    def test_only_sanitized_output_can_be_persisted(self):
        raw_input = f"助记词是 {MNEMONIC_WORDS}"
        result = self.ingress.sanitize(raw_input)
        with tempfile.TemporaryDirectory() as directory:
            repository = TicketRepository(Path(directory) / "tickets.db")
            ticket = repository.create_ticket(
                "safe-request",
                "user-1",
                result.sanitized_input,
                result.risk_flags,
            )
            with self.assertRaisesRegex(ValueError, "敏感信息") as caught:
                repository.create_ticket(
                    "unsafe-request", "user-1", raw_input, ["secret_exposure"]
                )

            self.assertNotIn(MNEMONIC_WORDS, ticket["sanitized_input"])
            self.assertNotIn(MNEMONIC_WORDS, str(caught.exception))

    def test_fixed_secret_is_absent_from_durable_and_renderable_surfaces(self):
        from agent.orchestration.graph import build_support_graph, sqlite_checkpointer
        from agent.orchestration.runtime import SupportOrchestrator
        from agent.security.trusted_sources import TrustedSourcePolicy

        hostile_type = type("TemporaryGraphError", (RuntimeError,), {})
        hostile_type.__name__ = f"{MNEMONIC_WORDS}\nforged-log-line"

        def broken_triage(_state):
            raise hostile_type(f"provider body contained {MNEMONIC_WORDS}")

        with tempfile.TemporaryDirectory() as directory:
            ticket_db_path = Path(directory) / "tickets.db"
            checkpoint_db_path = Path(directory) / "checkpoints.db"
            repository = TicketRepository(ticket_db_path)
            trusted_sources = TrustedSourcePolicy(
                self.policy["official_domains"], []
            )
            final_guard = PolicyGuard(
                self.policy, trusted_source_policy=trusted_sources
            )
            saver = sqlite_checkpointer(checkpoint_db_path)
            graph = build_support_graph(
                triage_node=broken_triage,
                diagnosis_node=lambda _state: {},
                review_node=lambda _state: {},
                policy_guard=final_guard,
                checkpointer=saver,
            )
            runtime = SupportOrchestrator(
                repository=repository,
                graph=graph,
                ingress_guard=self.ingress,
                recursion_limit=16,
                lease_seconds=130,
                graph_timeout_seconds=120,
                trusted_source_policy=trusted_sources,
                final_policy_guard=final_guard,
            )
            with self.assertLogs("agent", level="ERROR") as captured:
                secret_result = runtime.submit(
                    f"助记词是 {MNEMONIC_WORDS}",
                    "user-1",
                    request_id="secret-surface",
                )
                fault_result = runtime.submit(
                    "蓝牙连接失败",
                    "user-1",
                    request_id="secret-exception-body",
                )
            # These are the exact safe fields consumed by app.py's existing
            # session-message rendering contract.
            renderable_session_messages = [
                {"role": "user", "content": secret_result.sanitized_input},
                {
                    "role": "assistant",
                    "content": secret_result.user_notice
                    or secret_result.final_answer,
                },
                {"role": "user", "content": fault_result.sanitized_input},
                {
                    "role": "assistant",
                    "content": fault_result.user_notice or fault_result.final_answer,
                },
            ]
            saver.close()

            secret_bytes = MNEMONIC_WORDS.encode("utf-8")
            self.assertNotIn(secret_bytes, ticket_db_path.read_bytes())
            self.assertNotIn(secret_bytes, checkpoint_db_path.read_bytes())
            self.assertNotIn(MNEMONIC_WORDS, str(renderable_session_messages))
            log_output = "\n".join(captured.output)
            self.assertNotIn(MNEMONIC_WORDS, log_output)
            self.assertNotIn("forged-log-line", log_output)
            self.assertIn("exception_type=Exception", log_output)


if __name__ == "__main__":
    unittest.main()
