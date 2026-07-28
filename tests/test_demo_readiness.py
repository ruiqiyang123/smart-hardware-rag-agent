import os
import unittest
from pathlib import Path


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
        chroma_config = read_text("config/chroma.yml")
        gitignore = read_text(".gitignore")

        self.assertIn("persist_directory : chroma_db_v1", chroma_config)
        self.assertIn("chroma_db_v*/", gitignore)

    def test_type_annotations_remain_backwards_compatible(self):
        model_factory = read_text("model/factory.py")

        self.assertIn("Union[Embeddings, BaseChatModel]", model_factory)
        self.assertNotIn("Embeddings | BaseChatModel", model_factory)

    def test_runtime_environment_overrides_local_dotenv(self):
        model_factory = read_text("model/factory.py")

        self.assertIn("load_dotenv(override=False)", model_factory)

    def test_streamlit_demo_is_fixed_to_mimo(self):
        app = read_text("app.py")

        self.assertIn('selected_provider = "mimo"', app)
        self.assertNotIn('st.radio(\n        "聊天模型"', app)
        self.assertNotIn("阿里云 DashScope", app)
        self.assertNotIn("MiMo API Key", app)
        self.assertNotIn("MiMo Base URL", app)
        self.assertNotIn("如何获取 API Key", app)
        self.assertNotIn("系统状态", app)
        self.assertNotIn("后台 MiMo", app)
        self.assertNotIn("访客无需配置", app)
        self.assertIn('st.caption(f"模型：MiMo · `{mimo_model_name}`")', app)

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
        self.assertIn("MIMO_API_KEY=", env_example)
        self.assertIn("MIMO_BASE_URL=", env_example)
        self.assertIn("MIMO_CHAT_MODEL=mimo-v2.5-pro", env_example)
        self.assertIn("CHAT_PROVIDER=mimo", env_example)
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

    def test_readme_has_demo_script_without_internal_positioning_sections(self):
        readme = read_text("README.md")

        self.assertIn("72 条 source-backed 客服条目", readme)
        self.assertIn("设备丢了或坏了，资产还能恢复吗？", readme)
        self.assertIn("Passphrase 忘了，为什么恢复后余额为零？", readme)
        self.assertNotIn("作品集 / 简历口径", readme)
        self.assertNotIn("适合在简历或面试中强调的表达", readme)
        self.assertNotIn("产品边界", readme)

    def test_only_readme_markdown_is_changed_for_wallet_migration(self):
        protected_docs = [
            "PROJECT_ANCHOR_CARD.md",
            "PROJECT_CHAIN_CARD.md",
            "PROJECT_EVAL_BADCASE_CARD.md",
            "MEMORY_IMPLEMENTATION.md",
            "DEPLOYMENT.md",
        ]
        changed_markdown = set(
            p for p in os.popen("git diff --name-only -- '*.md'").read().splitlines()
        )

        self.assertFalse(changed_markdown.intersection(protected_docs))

    def test_root_license_file_is_not_exposed(self):
        self.assertFalse((ROOT / "LICENSE").exists())


if __name__ == "__main__":
    unittest.main()
