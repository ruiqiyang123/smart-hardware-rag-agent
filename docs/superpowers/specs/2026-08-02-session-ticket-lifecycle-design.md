# 会话型工单、30 分钟空闲结束与人工接管设计

日期：2026-08-02  
状态：等待用户审阅  
适用项目：KeyGuard 硬件钱包智能客服 2.0

## 1. 背景

当前客户页把一次 AI 回答后的工单保持为 `pending_user`，只有客户明确确认后才进入 `resolved`。这一改动避免了“AI 刚回答，工单就自动解决”的错误体验，但仍有三个问题：

1. `pending_user` 工单会把下一条输入无条件当作同一问题的补充，跨主题输入可能触发不合适的人工门禁；
2. `wallet_recovery` 被配置为无条件人工操作，导致低风险的“设备丢失、备份仍在”也进入人工审核；
3. 工单缺少自然的会话结束条件，用户不再回复时会长期停留在待补充状态。

本设计把“工单”重新定义为一次在线客服会话，而不是单一问题主题。会话活跃期间，用户可以连续询问不同主题；每一轮仍独立执行安全分诊。AI 最后一条面向客户的消息发出后 30 分钟没有新输入，工单进入“已结束”，但不标记为“问题已解决”。

## 2. 已确认的产品决策

### 2.1 工单按客服会话组织

- 一次活跃客服会话只使用一张工单；
- 30 分钟内，用户可以连续询问开机、蓝牙、固件、备份恢复等不同主题；
- 系统不再判断“这是同一问题还是新问题”，也不会因为主题变化创建新工单；
- 每条用户输入都重新经过入站安全检查、Triage、Diagnosis、Review 和出站策略检查；
- 工单上的 `category` 表示最近一轮分诊结果，完整的轮次分类通过事件时间线保留。

### 2.2 30 分钟空闲后进入“已结束”

- 超时时间固定为可配置的 1800 秒；
- 只有 `pending_user` 工单参与空闲结束；
- 计时从 AI 或人工最后一条面向客户的消息成功持久化后开始；
- 用户输入被接受时立即清除当前空闲截止时间，避免处理期间被关闭；
- 下一条面向客户的消息成功发送后重新设置 30 分钟截止时间；
- 到期后状态从 `pending_user` 转为 `closed`；
- `closed` 在客户页显示为“已结束”，不能显示为“问题已解决”；
- `resolved` 仅用于客户明确确认解决，或人工审核后按现有受控路径完成；
- `escalated` 工单不会因空闲自动结束；人工追问把工单恢复为 `pending_user` 后，新的 30 分钟计时重新生效。

### 2.3 超时后再次提问

- `closed` 是终态，旧工单不再恢复；
- 用户再次输入时创建新工单和新的 `conversation_id`；
- 新工单的 `parent_ticket_id` 指向上一张 `closed` 工单，便于工作台追溯；
- 客户页仍可显示原有聊天记录；
- 新工单只接收最近最多 6 轮已脱敏历史作为辅助上下文，并沿用现有压缩规则；
- 高风险标记和秘密内容不通过历史恢复机制降级或重新暴露。

### 2.4 人工入口和风险门禁

- 客户页从第一轮起显示人工入口和当前 AI 处理进度；
- 普通场景下，AI 成功发送 3 次安全回答后解锁人工接入；
- 计数按当前会话工单累计，不按主题重置；
- 只统计成功持久化的客户可见回答事件；澄清按钮点击、系统报错、重复命令不重复计数；
- 用户在 3 次之前输入“找人工”或点击入口时，系统说明当前进度并继续引导其描述问题，不创建人工队列；
- 第 3 次安全回答完成后，按钮和自然语言“找人工”都可将工单转为 `escalated`；
- high/critical 风险、关键风险标记、系统安全失败和受控人工动作不受 3 次门槛限制；
- `wallet_recovery` 不再作为低风险场景的无条件人工门禁；助记词泄露、资产丢失、钓鱼、地址异常、可疑签名等仍立即转人工；
- `device_reset`、`bootloader_recovery`、`warranty_decision` 等受控动作继续保留人工门禁。

### 2.5 客户页输入框

- 消息列表、处理进度和工单状态位于同一上方容器；
- `st.chat_input` 始终是客户对话区域的最后一个交互元素；
- 处理中产生的临时用户消息、AI 状态和最终回复必须写入输入框上方的消息容器；
- 回答完成、进入人工或会话结束后，输入框位置均不能跳到消息中间；
- `closed` 后输入框继续可用，下一条输入创建新工单；
- “继续追问”文案改为“未解决，继续追问”，同时保留“已解决”。

## 3. 状态模型

### 3.1 状态集合

状态集合从七个扩展为八个：

```text
new
triaged
diagnosing
reviewing
pending_user
escalated
resolved
closed
```

`resolved` 和 `closed` 都是终态，但含义不同：

| 状态 | 含义 | 客户页文案 |
| --- | --- | --- |
| `resolved` | 客户明确确认问题解决，或人工完成受控处理 | 已解决 |
| `closed` | 30 分钟没有新输入，客服会话自然结束 | 已结束 |
| `escalated` | 风险、系统失败或客户满足条件后主动申请人工 | 待人工处理 |

### 3.2 新增状态转移

```text
pending_user -> closed   system_idle_close
```

不允许以下转移：

- `closed -> pending_user`
- `closed -> resolved`
- `resolved -> closed`
- `escalated -> closed`

超时后再输入通过新建工单处理，不恢复 `closed` 工单。

### 3.3 并发规则

用户输入和超时关闭可能在截止时间附近竞争。必须通过数据库条件更新和命令租约保证只有一个动作成功：

1. 用户命令成功领取 `pending_user` 工单后，清除 `idle_expires_at`；
2. 超时命令只能更新 `status='pending_user' AND idle_expires_at <= now` 的记录；
3. 如果用户命令先完成，超时关闭影响 0 行并安全退出；
4. 如果超时命令先完成，用户输入创建新工单；
5. 两条路径都写入幂等命令记录，重复轮询不能生成重复事件。

## 4. 数据模型和审计事件

### 4.1 `tickets` 新字段

```text
idle_expires_at TEXT NULL
closed_at TEXT NULL
close_reason TEXT NULL CHECK(close_reason IS NULL OR close_reason = 'inactivity')
```

约束：

- `status='closed'` 时必须有 `closed_at` 和 `close_reason='inactivity'`；
- 非 `closed` 状态不得保存 `closed_at` 或 `close_reason`；
- `idle_expires_at` 只允许存在于 `pending_user`；
- `resolved` 和 `closed` 不得同时表达在同一行。

### 4.2 命令类型

新增两个可审计命令：

```text
customer_request_human
system_idle_close
```

`customer_request_human` 负责满足 3 次 AI 回答后的主动人工升级；`system_idle_close` 负责幂等关闭过期会话。

### 4.3 事件类型

新增事件：

```text
customer_requested_human
session_idle_deadline_set
session_idle_deadline_cleared
session_closed_inactive
```

每轮已有的分诊、诊断、审核事件继续记录当轮分类和风险，工作台据此展示跨主题会话时间线。

### 4.4 数据库迁移

- `SCHEMA_VERSION` 从 7 提升到 8；
- 使用向前迁移增加字段并重建受状态 CHECK 约束影响的表；
- 现有 `resolved`、`pending_user`、`escalated` 工单语义保持不变；
- 迁移不能删除现有工单、命令或事件；
- 旧的 `pending_user` 工单迁移后不立即关闭，第一次客户页或工作台扫描时设置新的 30 分钟截止时间。

## 5. 空闲结束服务

新增独立 `SessionExpiryService`，职责仅限时间计算和过期关闭，不负责生成 AI 内容。

建议接口：

```python
set_deadline(ticket_id, responded_at)
clear_deadline(ticket_id, accepted_at)
close_expired(now, limit=100)
```

实现要求：

- 接受可注入时钟，测试不依赖真实等待；
- 一次最多处理 100 张过期工单；
- 单张工单关闭失败不能阻止其他工单处理；
- 不记录提示词、客户原文、密钥或模型输出；
- 每次关闭都写入 `system_idle_close` 命令和 `session_closed_inactive` 事件；
- 无后台线程时也能通过显式扫描正确收敛状态。

### 5.1 Streamlit 触发方式

采用适合 Demo 的“页面轮询 + 懒清理”，不引入生产级任务队列：

- 客户页使用 Streamlit 1.40.1 支持的 `st.fragment(run_every="30s")` 定期扫描；
- 工作台渲染前执行一次过期扫描；
- 新用户输入路由前执行一次过期扫描；
- 页面关闭后不依赖浏览器关闭事件；下次客户页、工作台或新输入访问时补做关闭；
- Streamlit Cloud 休眠期间不承诺实时执行，但截止时间保存在数据库中，恢复后会立即收敛为 `closed`。

## 6. 每轮对话处理

### 6.1 普通活跃会话

```text
用户输入
-> 清除空闲截止时间
-> 入站脱敏与风险检查
-> 恢复当前 pending_user 工单
-> Triage 重新识别本轮问题
-> Diagnosis 重新检索证据
-> Review 和 Policy Guard
-> 发送客户可见回答
-> 设置 now + 30 分钟截止时间
```

跨主题时不创建新工单，最近的 `category`、`summary` 和证据被本轮结果更新，历史通过事件保留。

### 6.2 高风险输入

```text
用户输入
-> 入站安全检查命中 high/critical
-> 清除空闲截止时间
-> 当前工单直接 escalated
-> 工作台显示风险原因
```

风险粘性保持不变，后续模型不能自动降级已经进入人工的工单。

### 6.3 会话已结束后输入

```text
用户输入
-> 发现 active_ticket.status == closed
-> 创建新 ticket_id 和 conversation_id
-> parent_ticket_id 指向上一张 closed 工单
-> 继续完整多 Agent 流程
```

### 6.4 用户确认解决

自然语言“解决了、可以了、谢谢”等和“已解决”按钮继续走现有受审计确认路径：

```text
pending_user -> resolved
```

该路径立即清除 `idle_expires_at`，且不能被空闲关闭覆盖。

## 7. 人工入口规则

### 7.1 AI 回答计数

不新增独立可变计数字段，优先从当前 ticket 的客户可见成功回复事件计算：

```text
ai_attempt_count = count(response_finalized events for current ticket)
```

上限展示为 3；超过 3 次仍显示 `3/3` 和已解锁状态。

### 7.2 三次之前

客户点击或输入“找人工”时：

- 不改变工单状态；
- 返回固定说明，例如“为了更快处理，请先让我继续尝试。当前 AI 已处理 1/3 次”；
- 如果用户尚未描述问题，给出开机、连接、固件、备份安全等快捷入口；
- 不把“找人工”文本作为硬件故障送入 Diagnosis。

### 7.3 三次之后

- 按钮显示“转人工客服”；
- 自然语言请求与按钮调用同一个 `customer_request_human` 命令；
- 工单变为 `escalated`；
- `manual_gate_reason='customer_requested_after_ai'`；
- 工作台显示“用户在 AI 完成 3 次处理后主动申请”。

## 8. 人工门禁收窄

`config/orchestration.yml` 调整为：

```yaml
customer_session:
  inactivity_timeout_seconds: 1800
  expiry_poll_seconds: 30

customer_handoff:
  ai_attempt_threshold: 3

manual_gate_actions:
  - device_reset
  - bootloader_recovery
  - warranty_decision
```

`wallet_recovery` 从无条件人工动作列表移除，但以下入口仍会升级人工：

- `secret_exposure`
- `phishing`
- `asset_loss`
- `unofficial_firmware`
- `address_mismatch`
- `suspicious_signature`
- `device_auth_failure`
- `remote_control`
- `security_incident`
- 自动分诊、诊断、取证、审核或最终策略失败

低风险“设备丢失但离线备份还在”允许 AI 基于可信来源先回答安全恢复原则，禁止要求客户提供助记词、私钥、PIN 或 Passphrase。

## 9. 客户页设计

### 9.1 活跃状态

```text
当前工单 KG-... · 服务中 · AI 已处理 1/3 次
30 分钟内可以继续询问其他问题

[✅ 已解决] [❌ 未解决，继续追问] [👩‍💼 联系人工]
```

三次之前点击人工入口显示当前进度；三次之后执行人工升级。

### 9.2 已结束状态

```text
本次客服会话已结束（30 分钟无新消息）。
这不代表问题已经解决；如需继续，请直接发送新消息。
```

输入框保持可用，下一条消息创建新工单。

### 9.3 输入框布局

在客户标签页中依次渲染：

1. 聊天消息容器；
2. 工单状态和按钮容器；
3. 处理状态占位容器；
4. `st.chat_input`。

`_run_v2_prompt` 接受上方容器或占位器作为渲染目标，不能在 `st.chat_input` 之后向根页面追加消息。最终 `st.rerun()` 后结构保持一致。

## 10. 工单工作台

工作台范围增加：

- 待人工：`escalated`；
- 等待客户：`pending_user`；
- 已结束：`closed`；
- 已解决：`resolved`。

每张工单展示：

- 最近问题分类；
- 风险等级和风险标记；
- AI 成功回答次数；
- 30 分钟截止时间或关闭时间；
- 结束原因；
- 人工接管原因；
- 各轮 Triage、Diagnosis、Review 事件时间线；
- 前序 `parent_ticket_id`（存在时）。

`closed` 工单只读，不显示人工批准、编辑发送、追问和拒绝按钮。

## 11. 代码影响范围

预计修改：

| 文件 | 修改内容 |
| --- | --- |
| `agent/orchestration/state.py` | 新增 `closed` 状态和会话截止字段 |
| `agent/orchestration/routes.py` | 允许 `pending_user -> closed`，保持终态约束 |
| `agent/orchestration/graph.py` | 清理/设置截止时间并支持系统关闭事件 |
| `agent/orchestration/runtime.py` | 新增空闲关闭和客户请求人工命令 |
| `database/ticket_db.py` | Schema v8、字段迁移、过期查询与条件关闭 |
| `config/orchestration.yml` | 30 分钟、30 秒轮询、3 次人工门槛、收窄人工动作 |
| `utils/config_handler.py` | 严格校验新配置 |
| `app.py` | 会话恢复、超时扫描、人工入口、状态文案和输入框布局 |
| `agent/ui_progress.py` | “已结束”事件和状态映射 |
| `README.md` | 更新状态机、客户流程、人工接管和演示步骤 |

预计新增：

| 文件 | 职责 |
| --- | --- |
| `agent/orchestration/session_expiry.py` | 截止时间计算和过期关闭服务 |
| `agent/orchestration/human_intent.py` | 确定性识别客户主动人工请求 |
| `tests/test_session_expiry.py` | 可注入时钟、边界和幂等测试 |
| `tests/test_session_ticket_flow.py` | 跨主题连续对话、超时新工单、人工门槛测试 |

## 12. 实施顺序

### 阶段 1：锁定契约

1. 为 `closed` 状态、非法恢复和超时并发编写失败测试；
2. 为 30 分钟配置和 Schema v8 迁移编写测试；
3. 为三次 AI 回答后的人工入口编写测试；
4. 保持现有高风险、客户确认结案和结构化输出测试全部通过。

### 阶段 2：数据库和状态机

1. 完成 Schema v8 无损迁移；
2. 新增 `closed` 状态和字段约束；
3. 新增 `system_idle_close` 与 `customer_request_human` 命令；
4. 实现条件关闭和并发保护；
5. 完成 Repository 单元测试。

### 阶段 3：空闲结束服务

1. 实现可注入时钟；
2. 实现设置、清除和批量关闭；
3. 验证重复扫描幂等；
4. 验证页面关闭后下次访问能够补做关闭。

### 阶段 4：对话和人工策略

1. 删除按主题拆票需求；
2. 让活跃 `pending_user` 工单接收跨主题输入；
3. 收窄 `wallet_recovery` 人工门禁；
4. 增加客户主动人工意图和 3 次门槛；
5. 确保 high/critical 和系统失败直接人工。

### 阶段 5：客户页和工作台

1. 重构消息容器和输入框顺序；
2. 增加服务中、已结束、已解决和待人工文案；
3. 增加 AI 处理次数和人工入口；
4. 增加 Streamlit 30 秒轮询；
5. 增加已结束筛选和跨主题事件时间线。

### 阶段 6：文档、全量测试和浏览器验收

1. 更新 README 架构图、状态机和演示脚本；
2. 运行全量单元测试和静态编译；
3. 运行数据库迁移测试；
4. 用真实 DeepSeek 配置完成浏览器端验收；
5. 确认输入框全程位于对话底部；
6. 提交实现，不在用户明确要求前推送或部署。

## 13. 验收用例

### 用例 A：跨主题连续对话

1. 输入“蓝牙没法连接手机怎么办？”；
2. AI 正常回答，工单保持 `pending_user`；
3. 输入“设备丢了但备份还在，资产能恢复吗？”；
4. 使用同一 ticket_id；
5. AI 重新分诊并正常回答，不因 `wallet_recovery` 自动转人工；
6. 输入“固件升级中断了怎么办？”；
7. 仍使用同一 ticket_id，并给出经审核的安全建议。

### 用例 B：30 分钟空闲结束

1. AI 回答完成于 10:00:00；
2. `idle_expires_at` 为 10:30:00；
3. 10:29:59 扫描不关闭；
4. 10:30:00 扫描关闭；
5. 状态为 `closed`，文案为“已结束”；
6. 不显示“已解决”；
7. 下一条消息创建新 ticket_id、新 conversation_id，并关联旧 ticket_id。

### 用例 C：用户确认解决

1. AI 回答后点击“已解决”或输入“已经解决了”；
2. 工单立即变为 `resolved`；
3. 清除空闲截止时间；
4. 后续过期扫描不得改为 `closed`。

### 用例 D：人工入口

1. 第一次 AI 回答后点击“联系人工”；
2. 显示 `1/3` 进度，不升级；
3. 第三次安全回答完成后显示 `3/3`；
4. 再次点击或输入“转人工”，工单变为 `escalated`；
5. 工作台原因显示 `customer_requested_after_ai`。

### 用例 E：风险绕过门槛

1. 第一轮输入测试用敏感词串模拟助记词泄露；
2. 不等待三次回答；
3. 立即进入 `escalated`；
4. 入库内容保持脱敏；
5. 工作台展示风险原因但不展示秘密原文。

### 用例 F：输入框顺序

1. 提交问题；
2. 处理状态出现在消息区域而非输入框下方；
3. AI 完成回答；
4. 工单状态和按钮位于消息后；
5. 输入框始终是客户对话区域最后一个交互元素。

## 14. 完成标准

以下条件全部满足才视为完成：

- 客户可在同一活跃工单内连续询问不同主题；
- 低风险恢复问题不会因动作名称直接转人工；
- 每轮仍独立安全分诊，高风险立即人工；
- 30 分钟到期进入 `closed/已结束`，不进入 `resolved/已解决`；
- `resolved` 仍只能来自客户确认或受控人工路径；
- 超时关闭和用户输入并发时不丢消息、不重复关闭；
- 三次 AI 回答后人工入口可用，风险场景可绕过门槛；
- 输入框在处理前、处理中和回答后都位于对话底部；
- 工作台能区分等待客户、待人工、已结束和已解决；
- 数据库迁移无损；
- 全量自动化测试、编译检查和浏览器验收通过；
- README 与实际行为一致。

## 15. 非目标

本轮不实现：

- 生产级任务队列或分布式定时器；
- 客服排班、SLA、坐席自动分配；
- WebSocket 在线状态；
- 浏览器 `beforeunload` 强制关闭工单；
- 多租户权限系统；
- 将内部模型思维过程展示给客户或操作员；
- 自动推送 GitHub 或重新部署线上环境。
