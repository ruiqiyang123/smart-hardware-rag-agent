# DeepSeek 分诊 Schema 兼容修复实施计划

**目标：** 在不放宽 fail-closed 校验的前提下，让 DeepSeek V4 Flash 遵守 `category -> required_fields` 映射，并完成真实 V2 与线上回归。

**规格：** `docs/superpowers/specs/2026-07-30-deepseek-triage-schema-compatibility-design.md`

## 任务 1：用测试固定 Schema 与 Prompt 契约

**修改：** `tests/test_agent_contracts.py`

1. 增加 JSON Schema 描述测试：`missing_fields` 必须声明只能来自当前分类的 `required_fields`，分类未配置时返回空数组。
2. 增加 Prompt 契约测试：不得凭常识新增必要字段。
3. 保留配置外字段仍抛错的现有测试。
4. 运行定向测试并确认新增断言先失败。

## 任务 2：实现最小兼容修复

**修改：**

- `agent/orchestration/state.py`
- `prompts/triage_prompt.txt`

1. 为 `TriageResult.missing_fields` 增加靠近 Tool Calling 参数的 Schema 描述。
2. 强化分诊 Prompt 的白名单与空数组规则。
3. 不修改 `agent/nodes/triage.py` 的严格校验，不修改 `config/orchestration.yml`。
4. 运行定向 Agent/Graph 测试。

## 任务 3：真实 DeepSeek 与完整回归

1. 使用已授权、未跟踪的本地 Key 执行结构化 Triage 调用。
2. 用临时 ticket/checkpoint 数据库执行完整 V2 Runtime。
3. 运行全量 pytest、compileall 与 `git diff --check`。
4. 扫描提交内容，确认没有 Key、Secrets 或运行时数据库。

## 任务 4：发布与线上验收

1. 提交实现并推送 `main`。
2. 等待 GitHub CI 和 Streamlit 自动部署。
3. 清空旧测试会话的恢复状态。
4. 在线提交“硬件钱包开不了机怎么办？”，确认不再出现 `TRIAGE_FAILURE`。
5. 确认 DeepSeek V4 Flash、公开访问、客户对话和工单工作台保持正常。
