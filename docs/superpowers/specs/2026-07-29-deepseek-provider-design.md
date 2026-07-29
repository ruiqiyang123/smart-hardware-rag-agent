# KeyGuard DeepSeek Provider 设计

**日期：** 2026-07-29  
**状态：** 已获用户口头批准，等待书面规格复核  
**目标：** 将 DeepSeek 作为 KeyGuard 2.0 的正式聊天模型 Provider，默认使用低成本的 `deepseek-v4-flash` 非思考模式，同时保留 MiMo 与 DashScope 兼容路径。

## 1. 范围与成功标准

本次只改聊天模型接入，不改现有三 Agent 编排、RAG Embedding、工单状态机、数据库或评测案例。

完成后应满足：

- `CHAT_PROVIDER=deepseek` 时，应用读取独立的 `DEEPSEEK_*` 配置并构建 `ChatOpenAI` 客户端。
- 默认模型为 `deepseek-v4-flash`，OpenAI 兼容地址为 `https://api.deepseek.com`。
- 请求显式携带 `extra_body={"thinking": {"type": "disabled"}}`，温度为 `0`。
- Streamlit 页面正确显示 `DeepSeek · deepseek-v4-flash`，不再把 DeepSeek 标成 MiMo。
- MiMo 与 DashScope 仍可通过显式 Provider 配置使用，现有兼容性不被破坏。
- 真实密钥只进入被 Git 忽略的本地 `.env` 或部署平台 Secrets；代码、测试、文档和日志均不保存或输出密钥。
- 单元测试、全量测试及一次最小真实 API 调用均通过。

## 2. 方案选择

采用“独立 DeepSeek Provider”方案，不把 DeepSeek 塞进 `MIMO_*` 变量，也暂不扩展成任意 OpenAI-compatible endpoint 管理平台。

原因：

- Provider 名称、环境变量和 UI 展示一致，便于调试与面试说明。
- 保留 MiMo/DashScope 可快速回滚。
- 改动集中在现有模型配置边界，避免无关重构。

## 3. 配置契约

新增配置：

```dotenv
CHAT_PROVIDER=deepseek
DEEPSEEK_API_KEY=replace-with-your-deepseek-api-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_CHAT_MODEL=deepseek-v4-flash
DEEPSEEK_THINKING=disabled
```

默认值：

| 配置 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek OpenAI-compatible API |
| `DEEPSEEK_CHAT_MODEL` | `deepseek-v4-flash` | 低成本模型 |
| `DEEPSEEK_THINKING` | `disabled` | 客服场景减少额外推理输出 |
| `CHAT_TEMPERATURE` | `0` | 保持确定性；非思考模式可用 |

`DEEPSEEK_THINKING` 只接受 `enabled` 或 `disabled`。非法值应在本地配置解析阶段明确失败，不静默回退。

`.env.example` 只包含占位密钥；用户提供的真实密钥写入未跟踪 `.env`。Streamlit Cloud 使用同名根级 TOML Secrets。

## 4. 组件设计

### 4.1 `utils/model_config.py`

- 增加 DeepSeek 默认常量。
- 将 Provider 规范化扩展为 `deepseek`、`mimo`、`dashscope`。
- `build_chat_config` 接收 DeepSeek key、base URL、model 和 thinking 配置。
- 返回结构继续使用现有 `ChatConfig`，其中 `kwargs` 包含构建模型所需的窄字段；缓存签名只使用密钥指纹，不包含原始密钥。
- 对 thinking 值做白名单校验。

### 4.2 `model/factory.py`

- `provider == "deepseek"` 使用 `ChatOpenAI`。
- 参数包括 `model`、`api_key`、`base_url`、`temperature=0` 与 `extra_body={"thinking": {"type": ...}}`。
- 日志只记录 Provider 与模型名，不记录密钥、请求正文或完整异常响应。
- 现有 `mimo/openai` 和 `dashscope` 分支保持兼容。

### 4.3 `app.py`

- 从环境变量或 Streamlit Secrets 读取 `CHAT_PROVIDER`。
- 当 Provider 为 DeepSeek 时读取 `DEEPSEEK_*`；MiMo/DashScope 继续读取各自配置。
- 侧边栏根据实际 Provider 显示名称和模型，不硬编码 MiMo。
- 未配置所选 Provider 的密钥时保持 fail closed，显示安全的配置缺失提示，不回退到其他 Provider。

### 4.4 文档与样例

- `.env.example` 默认展示 DeepSeek 配置，同时保留 MiMo/DashScope 兼容说明。
- README 本地启动说明改为 DeepSeek 默认路径。
- DEPLOYMENT 增加 DeepSeek dotenv 与 Streamlit TOML Secrets 示例。
- 文档不包含真实密钥，也不声称实际 API 余额、成本或线上部署已经更新。

## 5. 数据流

1. 应用启动时先加载本地 `.env`。
2. `app.py` 解析 `CHAT_PROVIDER` 与对应 Provider 的配置。
3. `build_chat_config` 校验配置并生成安全缓存签名和模型工厂参数。
4. `build_chat_model` 构建 DeepSeek `ChatOpenAI` 客户端。
5. Triage、Diagnosis、Review 节点继续通过现有依赖注入使用同一聊天模型边界；编排层无需知道具体厂商。
6. 请求发送到 DeepSeek 时显式关闭 thinking；最终回答仍经过现有 Review 与 Policy Guard。

## 6. 错误处理与安全

- 缺少 DeepSeek key、非法 thinking 值或空 base URL：配置阶段失败，不调用网络。
- 401/403：向用户显示安全的模型配置失败提示，日志不得包含密钥。
- 404/model not found：提示核对 `deepseek-v4-flash`，不自动切换模型。
- 429/5xx/超时：沿用现有有限重试与 fail-closed 人工升级路径。
- 真实验证请求使用固定、无客户数据的短消息；响应只用于确认连接，不写入仓库。
- `.env` 与 `.streamlit/secrets.toml` 必须继续被 `.gitignore` 排除。

## 7. 测试与验收

### 自动测试

- Provider 规范化：`deepseek` 不被归到 MiMo。
- DeepSeek 默认 base URL、模型和 disabled thinking。
- 非法 thinking 值失败。
- 工厂构建的 `ChatOpenAI` 参数包含 DeepSeek endpoint、模型及 `extra_body`。
- runtime env 优先于 `.env`，真实密钥不出现在缓存签名、日志与异常文本中。
- Streamlit Provider/标题/Secrets 契约。
- MiMo/DashScope 回归测试。
- 全量 pytest、compileall、`git diff --check`。

### 真实验证

使用用户授权的密钥执行一次最小调用：

- 模型：`deepseek-v4-flash`
- thinking：`disabled`
- 输入：不含任何客户信息的固定健康检查文本
- 成功标准：HTTP 请求成功且返回非空最终文本；不输出密钥或思考内容。

### 界面验证

本地启动 Streamlit，确认：

- 页面加载无错误。
- 侧边栏显示 `DeepSeek · deepseek-v4-flash`。
- “客户对话”和“工单工作台”仍正常呈现。
- 不进行包含真实资产或秘密的测试。

## 8. 非目标

- 不删除 MiMo 或 DashScope。
- 不实现运行时 UI 模型切换器。
- 不把 DeepSeek 用作 Embedding Provider；RAG 继续使用本地 Hash Embedding。
- 不开启思考模式，不存储或显示 chain-of-thought。
- 不在本次改动中执行部署、推送 GitHub 或修改线上 Streamlit Secrets。
- 不生成未经完整评测 runner 执行的 V2 指标。
