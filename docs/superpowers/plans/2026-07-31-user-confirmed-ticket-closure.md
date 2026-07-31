# 用户确认后结案实施计划

**状态：** 已完成

**对应规格：** `docs/superpowers/specs/2026-07-31-user-confirmed-ticket-closure-design.md`

## 1. 状态与路由契约

- 在 `TicketState` 增加 `waiting_reason`。
- 保持七状态集合不变，允许 `pending_user → resolved` 的用户确认转移。
- 完整自动回答通过 Review 后进入 `pending_user / resolution_confirmation`。
- 含糊和信息不足路径分别写入 `clarification` 与 `missing_information`。
- 增加路由、图节点和状态事件测试。

## 2. 结案意图分类器

- 新建无副作用的确定性分类函数。
- 明确解决短语与独立感谢语返回确认解决。
- 否定、转折、继续提问和问句信号优先返回继续处理。
- 对超长、非字符串和含糊输入 fail safe 为继续处理。
- 覆盖正例、反例和组合语句。

## 3. Runtime 与恢复命令

- 增加 `confirm_resolution` / `confirm_resolution_prepared` 命令入口。
- 扩展 `_await_user` 支持 `confirm_resolved`，保留已审核回答并结案。
- `resume_user_prepared` 在等待确认时先运行确定性分类器；确认语句走结案命令，其余输入恢复同一 checkpoint 并重新分诊。
- 更新终态验证、幂等、租约、事件持久化和结果投影。
- 确保普通 AI 回答不能直接提交 `resolved`。

## 4. 数据库 v7

- `tickets` 增加受约束的 `waiting_reason` 列。
- 实现 v6 → v7 自动迁移，历史工单状态不变。
- 增加工作台安全投影与 JSON/枚举校验测试。

## 5. Streamlit 客户页与工作台

- 完整回答后显示“等待用户确认”及“已解决 / 继续追问”按钮。
- “已解决”使用稳定动作 ID 调用 Runtime；“继续追问”保持工单开放并提示直接输入。
- 自然语言确认复用普通聊天输入框。
- 工作台按 `waiting_reason` 展示三类等待状态，并删除“自动结案”文案。
- 提升 Orchestrator contract version，避免热更新复用旧缓存。

## 6. 文档与验证

- 更新 README 状态图、架构图、演示路径和项目边界。
- 更新 Demo 脚本中的低风险结案话术。
- 运行针对性测试、全量 pytest、compileall、diff check 和 Streamlit AppTest。
- 确认 8503 仍在监听并提交本地 Git commit。
