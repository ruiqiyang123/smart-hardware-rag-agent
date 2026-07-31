# KeyGuard 用户确认后结案设计规格

**日期：** 2026-07-31
**状态：** 用户已批准并完成实现
**范围：** 自动回答后的工单状态、用户确认结案、连续追问、客户页和工作台展示

## 1. 问题

当前完整的低风险回答通过 Review 和 Policy Guard 后，工单立即从
`reviewing` 进入 `resolved`。这把“AI 已经生成并发送回答”错误地等同于
“用户确认问题已经解决”。用户继续追问时只能创建关联的新工单，客户页也会在回答后
立刻显示“已解决”，不符合真实的对话体验。

## 2. Demo 目标

- AI 完成回答后，工单保持开放并等待用户确认。
- 用户可以在同一工单中继续追问，不创建新工单。
- 只有用户点击“已解决”或明确表达问题已解决时，工单才进入 `resolved`。
- 用户一直不回复时，工单一直保持等待状态。
- 不实现定时提醒、页面关闭检测、后台调度或生产级会话过期。

## 3. 状态模型

继续使用现有七个状态，不增加第八个状态。增加字段：

```text
waiting_reason = clarification | missing_information | resolution_confirmation | null
```

语义如下：

- `pending_user + clarification`：问题方向含糊，等待用户选择或描述问题类型。
- `pending_user + missing_information`：已经提供基础建议，等待必要诊断信息。
- `pending_user + resolution_confirmation`：已经提供完整安全回答，等待用户确认是否解决。
- `resolved + waiting_reason=null`：用户明确确认问题已经解决。

新的主流程：

```text
Triage → Diagnosis → Review → Policy Guard
                               ↓
                 pending_user / resolution_confirmation
                     ├─ 用户确认解决 → resolved
                     └─ 用户继续描述 → Triage（同一工单）
```

完整回答可以保存为 `final_answer`，但 `final_answer` 的存在不再代表工单已经结案。
`response_version` 在每次经审核的回答发送后增加。

## 4. 结案意图识别

结案意图不交给 Triage Agent，也不额外调用一次 LLM。Runtime 使用确定性、保守的
短文本分类器：

### 4.1 明确结案

以下类型视为确认解决：

- 点击客户页“✅ 已解决”；
- “解决了”“已经好了”“可以了”“没问题了”“问题已解决”；
- 独立的礼貌结束语，例如“谢谢”“感谢”。

### 4.2 继续处理优先

出现下列信号时，即使同时包含“谢谢”或“可以”，也不得结案：

- 否定或未解决：`没解决`、`没有`、`不行`、`还是`、`仍然`、`未`；
- 转折：`但是`、`不过`、`可是`；
- 继续意图：`继续`、`再问`、`还有`、`另外`；
- 新的问句、故障现象或问号。

例如：

- “谢谢，已经解决了” → 结案；
- “谢谢，但是还是连不上” → 继续原工单；
- “可以再问一个问题吗？” → 继续原工单；
- “好的，我还有个问题” → 继续原工单。

任何无法高置信判断为已解决的输入，都按继续追问处理，绝不自动关闭。

## 5. Runtime 与图恢复

- `finalize` 对完整低风险回答的目标状态从 `resolved` 改为 `pending_user`，并设置
  `waiting_reason=resolution_confirmation`。
- 部分清楚问题继续使用 `pending_user + missing_information`。
- 含糊问题使用 `pending_user + clarification`。
- `_await_user` 增加 `confirm_resolved` 恢复动作；该动作保留已审核回答并执行
  `pending_user → resolved`。
- `SupportOrchestrator` 提供显式 `confirm_resolution` 命令，按钮直接调用该命令。
- `resume_user_prepared` 只在当前原因为 `resolution_confirmation` 时运行确定性结案
  意图识别。明确结案则调用确认命令；其余输入按现有恢复路径重新进入 Triage。
- 结案命令继续使用幂等键、租约、checkpoint 和原子工单提交。
- Runtime 终态验证要求：`resolved` 必须来自明确的确认命令，或已有人工批准路径；
  普通 AI 回答不得直接提交 `resolved`。

## 6. 数据库

- schema 从 v6 升级到 v7。
- `tickets` 增加可空的 `waiting_reason` 枚举列。
- v6 数据库自动迁移；历史 `resolved` 工单保持原状态，不批量重新打开。
- 工作台安全投影增加 `waiting_reason`，不展示未受信任 checkpoint 内容。

## 7. 客户页

完整回答后展示：

```text
AI 回答正文与引用

这次回答解决你的问题了吗？
[✅ 已解决] [💬 继续追问]
```

- “已解决”调用 Runtime 结案命令，成功后追加简短确认消息。
- “继续追问”不创建新工单；页面提示用户直接在输入框补充现象。
- 用户直接输入文本时，Runtime 先判断是否为明确结案，否则继续原工单。
- 活动工单状态显示为“等待用户确认”，而不是“已解决”。

## 8. 工单工作台

- `pending_user + resolution_confirmation` 显示“等待用户确认”。
- `pending_user + missing_information` 显示“等待客户补充”。
- `pending_user + clarification` 显示“等待客户说明问题”。
- 统计中把三类等待状态分别解释，但都不算作待人工处理或已解决。
- 只有真正进入 `resolved` 的工单计入“已解决”。

## 9. 错误与安全边界

- 按钮重复点击必须幂等，不得重复生成结案事件。
- 非活动用户不能确认其他用户的工单。
- 已 `resolved`、`escalated` 或非等待确认工单不能调用用户确认结案。
- 自然语言先经过现有 Ingress Guard，分类器只读取脱敏文本。
- 分类器不记录原始输入，不输出模型思维过程。
- 结案失败时保留 `pending_user`，客户页使用安全错误提示。

## 10. 验收标准

1. 完整低风险回答发送后，状态为 `pending_user`，原因是
   `resolution_confirmation`，回答和引用正常展示。
2. 点击“已解决”后，同一工单进入 `resolved`。
3. 回复“已经解决了”后，同一工单进入 `resolved`。
4. 回复“谢谢，但还是没反应”后，同一工单重新进入 Triage，并生成后续回答。
5. 点击“继续追问”后，工单保持开放，输入框继续可用。
6. 用户一直不回复时，工单不会自动结案。
7. 含糊问题和缺失信息问题继续显示各自的澄清或补充引导。
8. 高风险与人工工作台流程不因本设计降低安全门槛。
9. v6 本地数据库能够无损迁移到 v7。
10. README、状态图、Demo 脚本和自动化测试与新语义一致。

## 11. 非目标

- 不检测浏览器标签关闭或网络断开。
- 不发送五分钟提醒或任何定时消息。
- 不引入 Celery、Redis、Cron、后台线程或外部队列。
- 不实现 SLA、生产级客服会话过期和跨设备推送。
