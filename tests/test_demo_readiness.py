import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


class DemoReadinessTest(unittest.TestCase):
    def test_agent_uses_langgraph_prebuilt_api_available_in_requirements(self):
        react_agent = read_text("agent/react_agent.py")
        middleware = read_text("agent/tools/middleware.py")

        self.assertIn("from langgraph.prebuilt import create_react_agent", react_agent)
        self.assertNotIn("from langchain.agents import create_agent", react_agent)
        self.assertNotIn("langchain.agents.middleware", middleware)

    def test_runtime_dependencies_are_declared(self):
        requirements = read_text("requirements.txt")

        self.assertIn("langchain==1.0.0", requirements)
        self.assertIn("langchain-core==1.2.22", requirements)
        self.assertIn("langchain-openai==1.0.0", requirements)
        self.assertIn("langgraph==1.0.10", requirements)
        self.assertIn("langgraph-prebuilt==1.0.8", requirements)
        self.assertIn("langgraph-checkpoint==4.1.1", requirements)
        self.assertIn("langgraph-checkpoint-sqlite==3.1.0", requirements)
        self.assertIn("socksio==1.0.0", requirements)
        self.assertIn("python-dotenv", requirements)
        self.assertIn("posthog<6.0.0", requirements)

    def test_chroma_major_version_uses_isolated_ignored_cache(self):
        chroma_config = yaml.safe_load(read_text("config/chroma.yml"))
        gitignore = read_text(".gitignore")

        self.assertEqual(chroma_config["persist_directory"], "chroma_db_v1")
        self.assertEqual(chroma_config["md5_hex_store"], "md5_v1.txt")
        index_version = chroma_config["persist_directory"].rsplit("_v", 1)[1]
        md5_version = chroma_config["md5_hex_store"].removeprefix("md5_v").removesuffix(".txt")
        self.assertEqual(index_version, md5_version)
        self.assertIn("chroma_db_v*/", gitignore)
        self.assertIn("md5_v*.txt", gitignore)

    def test_type_annotations_remain_backwards_compatible(self):
        model_factory = read_text("model/factory.py")

        self.assertIn("Union[Embeddings, BaseChatModel]", model_factory)
        self.assertNotIn("Embeddings | BaseChatModel", model_factory)

    def test_app_bootstraps_environment_before_framework_imports(self):
        tree = ast.parse(read_text("app.py"))
        bootstrap_call = next(
            index
            for index, node in enumerate(tree.body)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "bootstrap_environment"
        )
        framework_imports = []
        for index, node in enumerate(tree.body):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".", 1)[0]}
            else:
                continue
            if roots & {"streamlit", "agent", "langgraph"}:
                framework_imports.append(index)

        self.assertTrue(framework_imports)
        self.assertLess(bootstrap_call, min(framework_imports))

    def test_env_bootstrap_enables_strict_msgpack_before_langgraph_import(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(
                "LANGGRAPH_STRICT_MSGPACK=true\n",
                encoding="utf-8",
            )
            script = "\n".join(
                (
                    "import sys",
                    f"sys.path.insert(0, {str(ROOT)!r})",
                    "from utils.env_bootstrap import bootstrap_environment",
                    "bootstrap_environment()",
                    "import langgraph.checkpoint.serde.jsonplus as jsonplus",
                    "assert jsonplus._lg_msgpack.STRICT_MSGPACK_ENABLED is True",
                )
            )
            environment = dict(os.environ)
            environment.pop("LANGGRAPH_STRICT_MSGPACK", None)
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_env_bootstrap_does_not_override_runtime_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(
                "LANGGRAPH_STRICT_MSGPACK=true\n",
                encoding="utf-8",
            )
            script = "\n".join(
                (
                    "import os, sys",
                    f"sys.path.insert(0, {str(ROOT)!r})",
                    "from utils.env_bootstrap import bootstrap_environment",
                    "bootstrap_environment()",
                    "assert os.environ['LANGGRAPH_STRICT_MSGPACK'] == 'false'",
                )
            )
            environment = dict(os.environ)
            environment["LANGGRAPH_STRICT_MSGPACK"] = "false"
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_streamlit_demo_defaults_to_deepseek_without_runtime_key_form(self):
        app = read_text("app.py")

        self.assertIn('or "deepseek"', app)
        self.assertIn('provider_label = "DeepSeek"', app)
        self.assertNotIn('st.radio(\n        "聊天模型"', app)
        self.assertNotIn("阿里云 DashScope", app)
        self.assertNotIn("MiMo API Key", app)
        self.assertNotIn("MiMo Base URL", app)
        self.assertNotIn("如何获取 API Key", app)
        self.assertNotIn("系统状态", app)
        self.assertNotIn("后台 MiMo", app)
        self.assertNotIn("访客无需配置", app)
        self.assertIn('st.caption(f"模型：{provider_label} · `{selected_model_name}`")', app)

    def test_streamlit_demo_is_keyguard_wallet_scenario(self):
        app = read_text("app.py")

        self.assertIn("KeyGuard 硬件钱包智能客服", app)
        self.assertIn("硬件钱包开不了机怎么办？", app)
        self.assertIn("蓝牙没法连接手机怎么办？", app)
        self.assertIn("设备丢了或坏了，资产还能恢复吗？", app)
        self.assertIn("Passphrase 忘了，为什么恢复后余额为零？", app)
        self.assertIn("1005 - 赵先生（成都） · 展示记忆功能", app)
        self.assertIn("展示记忆功能：已预置 6 轮历史对话", app)
        self.assertIn("压缩摘要（演示）", app)
        self.assertIn("赵先生此前咨询过蓝牙连接失败", app)
        self.assertIn("不会作为聊天回答展示", app)
        self.assertIn("get_chain_status", read_text("agent/react_agent.py"))
        self.assertNotIn("扫地机器人", app)
        self.assertNotIn("是否有宠物", app)
        self.assertNotIn("是否有地毯", app)

    def test_init_script_and_env_example_exist(self):
        self.assertTrue((ROOT / "scripts/init_knowledge_base.py").exists())

        env_example = read_text(".env.example")
        self.assertIn("DASHSCOPE_API_KEY=", env_example)
        self.assertIn("DEEPSEEK_API_KEY=your-deepseek-api-key", env_example)
        self.assertIn("DEEPSEEK_BASE_URL=https://api.deepseek.com", env_example)
        self.assertIn("DEEPSEEK_CHAT_MODEL=deepseek-v4-flash", env_example)
        self.assertIn("DEEPSEEK_THINKING=disabled", env_example)
        self.assertIn("MIMO_API_KEY=", env_example)
        self.assertIn("MIMO_BASE_URL=", env_example)
        self.assertIn("MIMO_CHAT_MODEL=mimo-v2.5-pro", env_example)
        self.assertIn("CHAT_PROVIDER=deepseek", env_example)
        self.assertIn("EMBEDDING_PROVIDER=", env_example)
        self.assertTrue((ROOT / "model/local_embeddings.py").exists())

    def test_readme_has_external_runbook(self):
        readme = read_text("README.md")

        # 仓库已重命名为 ai-hardware-cs-agent，README 同步更新
        self.assertIn("git clone https://github.com/ruiqiyang123/ai-hardware-cs-agent.git", readme)
        self.assertIn("pip install -r requirements.txt", readme)
        self.assertIn("python scripts/init_knowledge_base.py", readme)
        self.assertIn("streamlit run app.py", readme)
        # 在线 / 本地两种体验路径
        self.assertIn("在线体验", readme)
        self.assertIn("本地启动", readme)

    def test_readme_documents_v2_evidence_chain(self):
        readme = read_text("README.md")

        self.assertIn(
            "KeyGuard 2.0｜多 Agent 硬件钱包售后工单协同系统",
            readme,
        )
        for heading in (
            "V1 → V2",
            "为什么是三个 Agent",
            "Agent 与 Tool 边界",
            "Human-in-the-loop",
            "48 条离线评测",
            "已知限制",
        ):
            self.assertIn(heading, readme)
        for evidence in (
            "Triage Agent",
            "Diagnosis Agent",
            "Review Agent",
            "Router 不是 Agent",
            "Ingress Guard",
            "Policy Guard",
            "风险粘性",
            "最多一次返工",
            "客户对话",
            "工单工作台",
            "30 条继承 + 18 条新增",
            "KEYGUARD_OPERATOR_TOKEN",
            "KEYGUARD_ORCHESTRATION_EVAL_RUNNER=module:attribute",
        ):
            self.assertIn(evidence, readme)
        self.assertIn("stateDiagram-v2", readme)
        self.assertIn("flowchart", readme)
        self.assertIn("模拟", readme)
        self.assertIn("72 条 source-backed 客服条目", readme)
        self.assertNotIn("作品集 / 简历口径", readme)
        self.assertNotIn("83.3% 回答准确率", readme)
        self.assertNotIn("当前线上已是 V2", readme)

    def test_readme_flow_and_state_diagrams_match_runtime_boundaries(self):
        readme = read_text("README.md")

        for edge in (
            'R2 -->|只能先澄清| PU',
            'RV --> R3{"确定性 Review Router"}',
            'R3 -->|一次返工| D',
            'R3 -->|升级人工| ES',
            'R3 -->|审查通过| PG',
            'PG -->|完整答复通过| OK',
            'PG -->|基础建议通过| PU',
            'PG -->|阻断| ES',
        ):
            self.assertIn(edge, readme)
        self.assertNotIn('PG -->|一次返工| D', readme)
        self.assertNotIn('RV --> PG', readme)
        flowchart = readme.split("```mermaid\nflowchart LR\n", 1)[1].split(
            "```", 1
        )[0]
        checkpoint_lines = [
            line.strip()
            for line in flowchart.splitlines()
            if line.strip().startswith("CP") and ".->" in line
        ]
        checkpoint_targets = {
            line.rsplit(" ", 1)[-1] for line in checkpoint_lines
        }
        self.assertEqual(len(checkpoint_lines), 2)
        self.assertEqual(checkpoint_targets, {"PU", "ES"})
        self.assertNotIn("-.暂停与恢复.->", readme)
        self.assertIn("命令恢复入口只接受 `pending_user` 和 `escalated`", readme)
        self.assertIn("不能任意从 Triage、Diagnosis 或 Review 节点恢复", readme)

        state_diagram = readme.split(
            "```mermaid\nstateDiagram-v2\n", 1
        )[1].split("```", 1)[0]
        documented = set()
        for line in state_diagram.splitlines():
            match = re.fullmatch(
                r"\s*([a-z_]+)\s+-->\s+([a-z_]+)(?::.*)?\s*",
                line,
            )
            if match:
                documented.add(match.groups())

        from agent.orchestration.routes import ALLOWED_TRANSITIONS

        runtime = {
            (source.value, target.value)
            for source, targets in ALLOWED_TRANSITIONS.items()
            for target in targets
        }
        self.assertEqual(documented, runtime)

    def test_deployment_documents_v2_secrets_and_ephemeral_storage(self):
        deployment = read_text("DEPLOYMENT.md")

        for command in (
            "pip install -r requirements.txt",
            "python scripts/init_knowledge_base.py",
            "pytest -q",
            "streamlit run app.py",
        ):
            self.assertIn(command, deployment)
        for secret in (
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_BASE_URL",
            "DEEPSEEK_CHAT_MODEL",
            "DEEPSEEK_THINKING",
            "MIMO_API_KEY",
            "MIMO_BASE_URL",
            "MIMO_CHAT_MODEL",
            "CHAT_PROVIDER",
            "KEYGUARD_OPERATOR_TOKEN",
            "KEYGUARD_TICKET_DB",
            "KEYGUARD_CHECKPOINT_DB",
        ):
            self.assertIn(secret, deployment)
        self.assertIn("CHAT_PROVIDER=deepseek", deployment)
        self.assertIn('CHAT_PROVIDER = "deepseek"', deployment)
        self.assertIn("必须使用不同文件", deployment)
        self.assertIn("Streamlit Cloud", deployment)
        self.assertIn("易失", deployment)
        self.assertIn("不能视为生产持久化", deployment)
        for persistence_boundary in (
            "TicketRepository",
            "SQLite checkpoint",
            "创建或重建空表",
            "不能恢复已丢失的历史",
            "Chroma 知识库初始化",
        ):
            self.assertIn(persistence_boundary, deployment)

    def test_five_minute_demo_script_has_honest_fixed_sections(self):
        script = read_text("docs/DEMO_SCRIPT.md")

        for timestamp in (
            "0:00–0:40",
            "0:40–2:00",
            "2:00–3:40",
            "3:40–4:30",
            "4:30–5:00",
        ):
            self.assertIn(timestamp, script)
        for scenario in (
            "蓝牙",
            "固件",
            "助记词",
            "A1B2",
        ):
            self.assertIn(scenario, script)
        self.assertIn("不得声称真实客户", script)
        self.assertIn("不得声称真实资产", script)
        self.assertIn("不得声称企业降本", script)
        self.assertIn("不得声称生产 SLA", script)
        self.assertIn("不得预填 V2", script)

    def test_portfolio_docs_do_not_fabricate_evaluation_or_production_claims(self):
        documents = "\n".join(
            read_text(path)
            for path in ("README.md", "DEPLOYMENT.md", "docs/DEMO_SCRIPT.md")
        )

        self.assertNotIn("83.3% 回答准确率", documents)
        for fabricated_claim in (
            "已服务真实客户",
            "使用真实客户数据",
            "处理真实客户资产",
            "已降低企业客服成本",
            "已达到生产 SLA",
        ):
            self.assertNotIn(fabricated_claim, documents)
        self.assertFalse((ROOT / "eval/eval_results/keyguard-v2.json").exists())
        self.assertIn(
            "KEYGUARD_ORCHESTRATION_EVAL_RUNNER=module:attribute",
            read_text("README.md"),
        )
        readme = read_text("README.md")
        self.assertIn("fail closed", readme)
        self.assertIn("stderr", readme)
        self.assertIn("零结果产物", readme)
        self.assertIn("aggregate metrics", readme)
        self.assertIn("逐 case scores", readme)
        self.assertIn("status_trace", readme)
        self.assertIn("citations", readme)
        self.assertIn("bad case", readme)
        self.assertNotIn("零输出", documents)
        self.assertNotIn("混淆矩阵", documents)

    def test_readme_assigns_policy_and_evidence_checks_to_real_boundaries(self):
        readme = read_text("README.md")

        self.assertIn("未脱敏秘密、不安全动作和不可信 URL", readme)
        self.assertIn("证据充分性由 Diagnosis / Review 验证链负责", readme)
        self.assertNotIn(
            "Policy Guard**：在答复离开系统前做确定性校验，阻断索要或复述秘密、证据不足",
            readme,
        )

    def test_root_license_file_is_not_exposed(self):
        self.assertFalse((ROOT / "LICENSE").exists())

    def test_runtime_ticket_and_checkpoint_databases_are_gitignored(self):
        for relative_path in (
            "data/keyguard_v2.db",
            "data/keyguard_v2_checkpoints.sqlite3",
        ):
            completed = subprocess.run(
                ["git", "check-ignore", "-q", relative_path],
                cwd=ROOT,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, relative_path)


if __name__ == "__main__":
    unittest.main()
