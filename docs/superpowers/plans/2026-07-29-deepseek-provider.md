# KeyGuard DeepSeek Provider 实现计划

**目标：** 按已批准规格把 DeepSeek 接入为正式聊天模型 Provider，默认使用 `deepseek-v4-flash` 非思考模式，保留 MiMo/DashScope，并完成本地密钥配置、真实最小调用和 Streamlit 验收。

**规格：** `docs/superpowers/specs/2026-07-29-deepseek-provider-design.md`

**架构：** Provider 选择与配置校验集中在 `utils/model_config.py`，模型实例化集中在 `model/factory.py`，`app.py` 只解析运行环境并展示实际 Provider。DeepSeek 使用现有 `ChatOpenAI` 依赖，通过 `extra_body` 显式关闭 thinking；编排、RAG 和持久化边界不变。

---

## 任务 1：以测试固定 DeepSeek 配置契约

**修改：**

- `tests/test_model_config.py`
- `tests/test_demo_readiness.py`
- 必要时新增 `tests/test_model_factory.py`

### 步骤

1. 增加失败测试：
   - `normalize_provider("deepseek") == "deepseek"`。
   - DeepSeek 缺省 base URL、模型与 thinking 分别为官方值、`deepseek-v4-flash`、`disabled`。
   - 缺少所选 Provider key 时 `is_configured=False`，不能借用 MiMo/DashScope key。
   - thinking 非 `enabled/disabled` 时抛 `ValueError`。
   - 缓存签名不包含原始 DeepSeek key。
   - 工厂传给 `ChatOpenAI` 的参数包含正确 model/base URL/temperature/`extra_body`。
2. 增加 App/文档失败测试：DeepSeek 是默认 Demo Provider，UI 显示 DeepSeek，`.env.example` 和部署文档只出现占位密钥。
3. 运行定向测试并确认先失败：

```bash
DEEPSEEK_API_KEY=test /private/tmp/keyguard-v2-deps/bin/python -m pytest tests/test_model_config.py tests/test_demo_readiness.py -q
```

## 任务 2：实现 Provider、工厂与 Streamlit 配置

**修改：**

- `utils/model_config.py`
- `model/factory.py`
- `app.py`

### 步骤

1. 在 `utils/model_config.py` 增加：
   - `DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"`
   - `DEFAULT_DEEPSEEK_CHAT_MODEL = "deepseek-v4-flash"`
   - `DEFAULT_DEEPSEEK_THINKING = "disabled"`
   - 独立 DeepSeek 配置分支及 thinking 白名单。
2. 保持 `build_chat_config` 返回现有 `ChatConfig`，DeepSeek `kwargs` 仅包含 `provider/api_key/base_url/model_name/thinking`；签名只包含 key 指纹。
3. 在 `ChatModelFactory.create` 增加 `thinking` 参数和 DeepSeek 分支：

```python
ChatOpenAI(
    model=model_name,
    api_key=api_key,
    base_url=base_url,
    temperature=0,
    extra_body={"thinking": {"type": thinking}},
)
```

4. `app.py` 从环境变量或 Streamlit Secrets 读取实际 `CHAT_PROVIDER`，分别解析 DeepSeek/MiMo/DashScope，不允许缺失配置时跨 Provider 回退。
5. 侧边栏标题根据真实 Provider 与模型生成。
6. 运行任务 1 定向测试直至通过，并运行现有 app/factory 回归测试。

## 任务 3：更新安全样例和部署文档

**修改：**

- `.env.example`
- `README.md`
- `DEPLOYMENT.md`
- `tests/test_demo_readiness.py`

### 步骤

1. `.env.example` 默认配置 DeepSeek，真实密钥位置只写占位值；MiMo/DashScope 作为注释兼容项保留。
2. README 本地启动、当前模型和已知限制同步为 DeepSeek 默认路径。
3. DEPLOYMENT 的 dotenv/TOML Secrets、故障排查和发布检查同步为 `DEEPSEEK_*`。
4. 扫描并确保用户真实 key 不存在于 Git diff、已跟踪文件、日志和测试输出。
5. 运行文档合约测试。

## 任务 4：写入本地密钥并做真实调用

**本地未跟踪文件：**

- `.env`

### 步骤

1. 通过补丁写入本地 `.env`，包含：
   - `CHAT_PROVIDER=deepseek`
   - 用户授权的 `DEEPSEEK_API_KEY`
   - `DEEPSEEK_BASE_URL=https://api.deepseek.com`
   - `DEEPSEEK_CHAT_MODEL=deepseek-v4-flash`
   - `DEEPSEEK_THINKING=disabled`
   - `.env.example` 中其余 V2/本地 embedding 配置。
2. 用 `git check-ignore .env` 和 `git status` 确认密钥文件不被跟踪。
3. 使用项目模型工厂发起一次固定健康检查请求，不包含客户数据，不输出请求头、key 或思考内容。
4. 若网络沙箱阻止请求，按权限流程申请仅本次 DeepSeek API 访问。
5. 验证返回非空最终文本及实际模型标识；不把响应写入仓库。

## 任务 5：全量回归与界面验收

### 自动验证

```bash
DEEPSEEK_API_KEY=test /private/tmp/keyguard-v2-deps/bin/python -m pytest -q
/private/tmp/keyguard-v2-deps/bin/python -m compileall -q agent database rag eval tests utils
git diff --check
git status --short
```

### 界面验证

1. 用本地 `.env` 启动 Streamlit，不在命令行重复真实 key。
2. 浏览器确认页面无错误、双 Tab 存在、侧边栏显示 `DeepSeek · deepseek-v4-flash`。
3. 不使用真实钱包数据，不把 API 响应或 key 截图进仓库。

### 完成条件

- 所有测试、编译与 diff 检查通过。
- 真实最小 API 调用通过。
- 浏览器冒烟通过。
- `.env` 被忽略，工作树除有意提交外干净。
- 代码与文档提交完成，未 push、未修改线上 Streamlit Secrets。
