# 渐进式澄清与回答实现计划

**对应规格：** `docs/superpowers/specs/2026-07-31-progressive-clarification-design.md`

## 1. Secrets 回归

- `_runtime_secret` 先调用 `load_if_toml_exists`，不存在时直接返回。
- 增加契约测试，确认环境变量优先且不会无条件读取 Secrets。

## 2. 分诊结构化契约

- 扩展 `TriageResult`、`TicketState` 和 triage prompt。
- 对 `ambiguous / partial / clear` 的问题、选项、缺失字段和路由组合做严格校验。
- 保持 high/critical 风险的确定性升级规则。

## 3. 图路由与渐进式回答

- `clarify` 从 triage 直接进入 `pending_user`。
- `partial` 即使有 `missing_fields` 也进入 Diagnosis。
- Diagnosis `need_user` 允许产生带证据的基础草稿。
- 基础草稿进入 Review；通过后转 `pending_user` 并保存可展示答案。
- 无草稿的 `need_user` 保留安全兜底路径。

## 4. Runtime 与数据库

- schema v6 增加 clarity、clarification question/options。
- v5 数据库自动迁移。
- `OrchestrationResult` 返回缺失字段和澄清内容。
- 终态验证只允许经过 Review 的 `pending_user` 答案。

## 5. Streamlit 客户页与工作台

- 客户页渲染基础回答、动态字段标签、示例和澄清选项。
- 客户继续输入时恢复同一工单。
- 工作台分别统计待客户补充和待人工处理。
- 客户与工作台都只展示安全投影。

## 6. 测试与发布

- 更新 agent contract、route、repository、runtime 和 app contract 测试。
- 增加含糊、部分清楚、完整、高风险和恢复路径回归。
- 运行全量 pytest、compileall、diff check。
- 重启本地 Streamlit，实际验证 Secrets、含糊问题、固件问题和正常回答。
- 提交并推送 GitHub，检查线上地址。
