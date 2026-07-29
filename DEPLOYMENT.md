# KeyGuard 2.0 部署与复现

这份说明用于本地演示和 Streamlit Cloud 求职 Demo。KeyGuard 2.0 使用虚构品牌、模拟业务数据、SQLite 工单库和 checkpoint；部署完成不等于满足生产安全、可用性或持久化要求。

## 本地复现

推荐 Python 3.11。在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/init_knowledge_base.py
pytest -q
streamlit run app.py
```

浏览器打开 `http://localhost:8501`。第一次初始化会生成本地 Chroma 缓存；不要把缓存、数据库或 Secrets 提交到仓库。

## 配置项

`.env` 或 Streamlit Secrets 至少需要以下聊天模型配置：

```dotenv
MIMO_API_KEY=your-mimo-api-key
MIMO_BASE_URL=https://token-plan-sgp.xiaomimimo.com/v1
MIMO_CHAT_MODEL=mimo-v2.5-pro
CHAT_PROVIDER=mimo
```

V2 和人工工作台配置：

```dotenv
KEYGUARD_AGENT_VERSION=v2
KEYGUARD_OPERATOR_TOKEN=replace-with-a-long-random-token
KEYGUARD_TICKET_DB=data/keyguard_v2.db
KEYGUARD_CHECKPOINT_DB=data/keyguard_v2_checkpoints.sqlite3
```

- `KEYGUARD_OPERATOR_TOKEN` 未配置时，工单工作台默认关闭；这是一条 fail-closed 边界。
- `KEYGUARD_TICKET_DB` 保存业务工单和审计事件，`KEYGUARD_CHECKPOINT_DB` 保存 LangGraph 恢复点。两者**必须使用不同文件**，应用会拒绝相同的解析路径。
- 两个数据库路径是可选覆盖项；默认值分别为 `data/keyguard_v2.db` 和 `data/keyguard_v2_checkpoints.sqlite3`。
- 本地 Hash Embedding 可通过 `EMBEDDING_PROVIDER=local` 和 `LOCAL_EMBEDDING_DIMENSION=1024` 配置，不需要额外 Embedding Key。
- `LANGGRAPH_STRICT_MSGPACK=true` 用于收紧 checkpoint 序列化边界。

## Streamlit Cloud

1. 在 Streamlit Cloud 创建应用，选择仓库 `ruiqiyang123/ai-hardware-cs-agent`、目标分支和入口 `app.py`。
2. Python 版本选择 3.11。
3. 在应用的 Secrets 管理页填写配置，不要把真实 Key 写进代码或提交到 Git。
4. 完成部署后查看 Logs，确认知识库初始化、模型连接和数据库路径没有报错。

Secrets 示例采用 TOML 语法：

```toml
MIMO_API_KEY = "your-mimo-api-key"
MIMO_BASE_URL = "https://token-plan-sgp.xiaomimimo.com/v1"
MIMO_CHAT_MODEL = "mimo-v2.5-pro"
CHAT_PROVIDER = "mimo"
KEYGUARD_AGENT_VERSION = "v2"
KEYGUARD_OPERATOR_TOKEN = "replace-with-a-long-random-token"
KEYGUARD_TICKET_DB = "data/keyguard_v2.db"
KEYGUARD_CHECKPOINT_DB = "data/keyguard_v2_checkpoints.sqlite3"
LANGGRAPH_STRICT_MSGPACK = "true"
```

在线示例地址为 [https://ai-hardware-cs-agent.streamlit.app/](https://ai-hardware-cs-agent.streamlit.app/)。线上行为由实际部署的分支、提交和 Secrets 决定；发布前应在页面中再次确认版本和四条演示路径。

## 持久化边界

Streamlit Cloud 的本地文件系统是**易失**环境，实例休眠、迁移或重新部署后，SQLite 工单、checkpoint 和向量缓存都可能丢失。因此这种文件持久化只适合 Demo，**不能视为生产持久化**。

生产化至少需要把工单、checkpoint 和审计事件迁移到受控的外部数据库，增加备份恢复、并发控制、密钥轮换、企业身份认证、最小权限和数据保留策略。本项目没有实现这些能力。

## 部署前检查

```bash
pip install -r requirements.txt
python scripts/init_knowledge_base.py
pytest -q
streamlit run app.py
```

随后人工检查：

- “客户对话”和“工单工作台”两个标签页可见。
- 未配置或输入错误的操作员令牌时，工作台不可访问。
- 蓝牙案例能够自动完成并给出证据引用。
- 固件信息不足时停在 `pending_user`，补充信息后可以继续。
- 测试助记词在持久化前被脱敏，并进入 `escalated`。
- `A1B2` 保修案例能看到模拟证据，但结论仍受人工门禁约束。

## 常见故障

### 页面启动但模型不可用

检查 `MIMO_API_KEY`、`MIMO_BASE_URL`、`MIMO_CHAT_MODEL` 和 `CHAT_PROVIDER=mimo` 是否在当前部署环境生效。未声明 provider 时模型工厂可能回退到 DashScope；应用读取运行环境优先，不应在日志里打印 Key。

### 工单工作台打不开

这是未配置 `KEYGUARD_OPERATOR_TOKEN` 时的预期行为。配置令牌并重启应用后，用相同令牌进入工作台。

### 应用拒绝数据库配置

检查 `KEYGUARD_TICKET_DB` 和 `KEYGUARD_CHECKPOINT_DB`。它们必须使用不同文件，也不要通过 `..`、符号链接等方式让两个值解析到同一路径。

### 重部署后历史工单消失

这是 Streamlit Cloud 易失文件系统的限制，不应把本地 SQLite 当作远程可靠数据库。求职演示前重新执行初始化与冒烟检查；正式服务应改用外部持久化。
