# KeyGuard 渐进式澄清与回答设计规格

**日期：** 2026-07-31
**状态：** 用户已批准
**范围：** 分诊清晰度、含糊问题澄清、低风险问题先回答再追问、客户引导和工作台状态

## 1. 问题

现有 `TriageAgent` 已能识别蓝牙、USB、固件、保修等意图，但把
`missing_fields` 当作诊断前的硬门槛。以“固件升级中断了怎么办？”为例，
系统在知识库检索前就进入 `pending_user`，客户只看到笼统的“补充必要信息”，
既没有基础帮助，也不知道要补充设备型号和错误状态。

## 2. 目标

- 区分含糊问题、部分清楚问题和完整问题。
- 只有无法确定问题方向时才先澄清。
- 意图明确但信息不完整的低风险问题先提供经证据支持和安全审核的基础回答，再精准追问。
- 客户补充后恢复同一工单。
- 高风险问题继续直接进入安全或人工流程。
- 页面展示安全阶段，不展示隐藏推理、Prompt 或 chain-of-thought。

## 3. 分诊契约

`TriageResult` 增加：

- `clarity`: `ambiguous | partial | clear`
- `clarification_question`: 面向客户的单个具体问题；非澄清路径为空字符串
- `clarification_options`: 2–5 个安全短选项；非澄清路径为空数组

路由语义：

- `ambiguous`：`suggested_route=clarify`，必须有问题和选项，不得伪造缺失业务字段。
- `partial`：意图和 category 已明确，允许携带 category 对应的 `missing_fields`，
  但低风险情况下仍进入 Diagnosis。
- `clear`：低风险且 `missing_fields=[]` 时直接诊断。
- `high/critical`：无论清晰度如何都 `escalate`。

不增加一个重复的 Intent Agent；清晰度判断属于现有 Triage Agent 的分诊职责。

## 4. 渐进式回答

`partial` 路径执行：

1. Triage 标记 `partial` 和缺失字段。
2. Diagnosis 使用知识库生成基础排查回答、引用和 `remaining_unknowns`。
3. Review 对基础回答执行同样的独立安全复核。
4. Review 通过后，把回答保存为 `final_answer`，但工单状态设为 `pending_user`。
5. 客户页展示基础回答、引用和明确的补充字段。
6. 客户补充后通过现有 resume 命令恢复同一工单。

`pending_user` 因此允许存在经过审核的 `final_answer`，但只限于
`USER_INPUT + review approve + outcome=need_user`。其他非 resolved 状态仍禁止返回
未经审核的最终文本。

## 5. 含糊问题澄清

`ambiguous` 路径不调用 Diagnosis，也不转人工。客户页显示：

- 一个具体澄清问题；
- 2–5 个可直接点击或复制的选项；
- 工单状态为 `pending_user`。

客户回复后恢复同一工单，Triage 使用安全对话历史重新识别意图。

## 6. 客户引导

字段采用固定中文标签和示例：

- `device_model`：设备型号，例如 KeyGuard Mini
- `error_state`：屏幕提示或错误码
- `serial_last4`：序列号后四位
- `transaction_hash`：交易哈希
- `chain_name`：区块链网络，例如 Ethereum

客户消息优先级：

1. 经审核的基础回答；
2. 澄清问题；
3. 缺失字段提示；
4. 安全兜底文案。

页面显示“已完成安全检查 / 已识别问题类型 / 已检索诊断资料 / 等待补充”等安全阶段，
不显示模型内部分析。

## 7. 工作台

- `pending_user` 单独标为“待客户补充”，不等同于人工任务。
- `escalated` 标为“待人工处理”。
- 展示清晰度、澄清问题、缺失字段中文标签和关联工单。
- 默认“待处理”范围可包含两者，但统计分别显示。

## 8. 安全与持久化

- 新字段进入 LangGraph checkpoint 和 SQLite 工单安全投影。
- 数据库从 schema v5 迁移到 v6，旧工单使用兼容默认值。
- 澄清问题和选项必须通过长度、控制字符和敏感信息检查。
- 任何自动展示的基础回答必须带验证证据并通过 Review 与 Policy Guard。
- 本地没有 `secrets.toml` 时使用 Streamlit 安全探测，不在页面渲染异常。

## 9. 验收案例

1. “我的钱包有问题” → 单个澄清问题和选项，`pending_user`，不转人工。
2. “固件升级中断了怎么办？” → 基础恢复步骤 + 设备型号/错误码追问，`pending_user`。
3. “KeyGuard Mini 升级中断，显示 Update failed” → 正常诊断回答。
4. 蓝牙、USB、开机问题 → 低风险自动回答。
5. 补充信息 → 恢复同一工单并保留会话关联。
6. 助记词泄露或钓鱼 → 高风险安全升级。
7. 无 `secrets.toml` → 页面无红色 Secrets 错误。
