# 客户进度与工单工作台实现计划

**对应规格：** `docs/superpowers/specs/2026-07-30-agent-progress-workbench-design.md`

## 1. 契约测试先行

文件：`tests/test_app_v2_contract.py`、`tests/test_agent_contracts.py`、新增必要的 UI projection 测试。

- 锁定客户消息必须在 Runtime 返回前写入且只能写脱敏文本。
- 锁定处理中占位消息、成功替换和异常替换行为。
- 锁定客户阶段只允许安全白名单事件。
- 锁定 Review revise payload 的字段边界和返工 prompt 约束。
- 锁定操作员令牌未配置、错误令牌、正确令牌三条路径。
- 先运行新增测试，确认它们在实现前按预期失败。

## 2. 客户页消息与进度实现

文件：`app.py`，必要时新增纯函数模块以便测试。

- 增加基于稳定 `request_id` 的会话消息 upsert helper，避免 Streamlit rerun 重复追加。
- `prepare_user_input` 成功后立即写入脱敏用户消息和助手处理中占位消息。
- 使用 `st.status` 展示安全阶段提示；不展示 Prompt、工具参数、模型原始输出或隐藏推理。
- Runtime 返回后从 `ticket_events` 投影阶段，替换占位消息为最终答案、pending_user 文案或 escalated 文案。
- 所有异常路径保留用户消息和安全失败提示；若已创建工单，保留工单 ID。
- 检查用户切换、清空对话、恢复请求和重复提交不会污染消息列表。

## 3. Diagnosis 返工稳定性

文件：`agent/nodes/diagnosis.py`、`prompts/diagnosis_prompt.txt`、必要时
`agent/orchestration/graph.py` 与相关测试。

- 为 Review revise 构造受限、结构化的返工 payload。
- 在返工 prompt 中明确 category、risk、missing_fields、evidence 和 Review 修改项的权威边界。
- 为 USB/连接类低风险 draft 强制使用 `generic_troubleshooting`，draft 的 `remaining_unknowns` 必须为空。
- 保持严格 schema 校验和 fail-closed，不添加静默后处理归一化。
- 增加一个 Review revise → Diagnosis → Review 的回归 fixture，覆盖第一次草稿被要求修改的路径。
- 使用真实 DeepSeek 做少量 USB 长句 smoke test，确认不因返工协议失败而无解释升级。

## 4. 工作台安全投影与人工动作

文件：`app.py`、`agent/orchestration/runtime.py`，必要时 `database/ticket_db.py`。

- 复用现有 `ticket_events` 和 `list_workbench_tickets`，增加安全事件/证据投影 helper。
- 工作台显示工单摘要、阶段时间线、工具白名单、证据数量、引用、Review 结构化结果和草稿。
- 禁止渲染 `metadata_json`、`draft_prompt`、`tool_args`、`model_output` 和未投影模型字段。
- 保持现有 HMAC 操作员会话、prepare/freeze/submit、租约和幂等行为。
- 保持未配置 `KEYGUARD_OPERATOR_TOKEN` 时 fail-closed；不在代码或仓库生成令牌。

## 5. 本地验证

- 运行新增聚焦测试。
- 运行完整 pytest、compileall、git diff --check。
- 运行本地真实 DeepSeek：USB、开不了机、pending 交易各至少一次；敏感风险用 mock/规则测试，不发送真实秘密。
- 启动本地 Streamlit，浏览器验证：
  - 客户提交后消息和处理中状态立即可见。
  - USB 正常路径 resolved。
  - 失败/转人工路径保留工单和阶段。
  - 工作台令牌登录和一条安全 Edit & Send。

## 6. 配置、发布与线上验收

- 生成/确认独立 Demo 操作员令牌；写入本地 `.env` 前和写入 Streamlit Secrets 前分别确认，不提交 Git。
- 推送代码和文档到 `main`，等待 Streamlit 自动部署。
- 线上验证客户正常路径、USB 返工路径、风险升级路径和工作台登录。
- 检查部署日志没有新的结构化失败；保留 commit、测试结果和线上工单 ID。

## 7. 完成标准

- 全量测试通过，新增测试覆盖所有新契约。
- 客户输入不再在执行期间消失。
- USB 普通问题不会因一次 Review 返工 schema 失败而无解释转人工。
- 客户页和工作台符合 C + A 设计。
- 操作员令牌和敏感数据未进入 Git 或日志。
