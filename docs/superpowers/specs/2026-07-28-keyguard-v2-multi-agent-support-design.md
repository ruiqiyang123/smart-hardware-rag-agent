# KeyGuard 2.0 多 Agent 售后工单协同系统设计规格

- 日期：2026-07-28
- 项目仓库：`ruiqiyang123/ai-hardware-cs-agent`
- 项目类型：个人学习与求职 Demo
- 目标岗位：AI 产品经理 / 大模型应用产品经理
- 计划周期：3 周、15 个工作日，每天 2–3 小时，总计 30–45 小时
- 设计状态：已完成分节评审，等待书面规格确认

## 1. 执行摘要

KeyGuard 2.0 不创建第二个重复的售后项目，而是在现有 KeyGuard 硬件钱包客服仓库内完成 V1 到 V2 的升级：

- V1 是一个基于 LangGraph `create_react_agent` 的单 ReAct Agent，调用 RAG、用户档案、链状态和月度报告等工具。
- V2 使用显式 LangGraph `StateGraph`，将分诊、诊断、复核拆成三个职责独立的 Agent，并由确定性条件边控制工单状态。
- RAG、数据库查询、敏感信息检测、工单写入和状态更新仍然是 Tool 或 Service，不包装成 Agent。
- `low / medium` 工单在必要信息和证据充分、复核通过且未命中人工门禁时自动解决；信息不足时追问；`high / critical` 或自动流程失败时强制进入人工审核。
- 同一 Streamlit 应用提供“客户对话”和“工单工作台”两个视图。

项目名称固定为：

> **KeyGuard 2.0｜基于 LangGraph 的多 Agent 硬件钱包售后工单协同系统**

## 2. 当前基线与改造动机

现有仓库已经具备以下可复用资产：

- Streamlit 在线聊天 Demo 和部署配置。
- 一个 LangGraph ReAct Agent 与 8 个工具。
- 72 条带来源元数据的硬件钱包客服知识条目。
- Chroma 检索、关键词兜底和引用格式化。
- SQLite 用户档案。
- 模拟链状态和模拟月度使用记录。
- 30 条离线评测题和关键词覆盖率脚本。
- 单元测试、错误提示和会话记忆压缩。

V1 的主要限制：

1. 一个 Agent 同时承担意图理解、工具选择、答案生成和安全判断，职责冲突且难以审计。
2. 没有工单实体、合法状态转移、事件轨迹和人工审核队列。
3. 安全边界主要由 Prompt 约束；原始用户输入会先进入会话历史。
4. RAG 工具成功后可以直接作为最终答案返回，没有统一经过独立复核。
5. 页面展示自然语言 `thought` 和工具参数，不适合作为面向用户的可审计过程。
6. 当前 83.3% 指标是 30 题关键词覆盖率，不能代表回答准确率或问题解决率。

这些限制构成 V1 升级为可控多 Agent 编排的真实理由，而不是为了简历强行增加 Agent 数量。

## 3. 目标与非目标

### 3.1 产品目标

1. 将用户咨询转换为可追踪的售后工单。
2. 使用三个专业 Agent 完成分诊、诊断和安全质量复核。
3. 通过代码级安全规则和人工介入保护高风险硬件钱包场景。
4. 让每次路由、证据、复核和人工决策都能通过结构化事件解释。
5. 建立覆盖路由、安全、状态、引用、故障降级和性能的评测闭环。
6. 保留 V1 的知识库、工具、在线 Demo 和评测基线，形成清晰的 V1 → V2 演进故事。

### 3.2 求职目标

项目完成后，应能用真实证据回答以下面试问题：

- 为什么单 Agent 不够？
- 什么能力应该设计成 Agent，什么能力必须保持为 Tool？
- Supervisor 是否一定需要是 Agent？
- 如何避免 Agent 死循环、错误传播和高风险漏接管？
- 如何实现 Human-in-the-loop？
- 如何评测编排，而不只评测回答文本？
- 多 Agent 带来的成本、延迟和复杂度如何控制？

### 3.3 明确不做

- 不创建第二个通用电商售后仓库。
- 不接入真实 CRM、ERP、物流、支付或保修系统。
- 不执行真实链上签名、转账、授权或设备远程控制。
- 不实现登录、鉴权、多租户和正式客服权限体系。
- 不拆分 FastAPI、微服务、Redis、消息队列、Docker Compose 或 Kubernetes。
- 不进行模型训练或微调。
- 不实现多模型路由或多个 Agent 自由群聊。
- 不把 Streamlit Cloud 本地 SQLite、Chroma 或 Hash Embedding描述为生产级架构。
- 不在简历中使用模拟业务降本数据或尚未实测的指标。

## 4. 用户角色与核心场景

### 4.1 用户角色

| 角色 | 主要目标 | MVP 权限 |
|---|---|---|
| 硬件钱包用户 | 获得安全、可执行、有依据的售后建议 | 提交咨询、补充信息、查看答复和引用 |
| 演示客服 / 审核员 | 处理高风险或自动流程失败的工单 | 查看脱敏工单、批准、编辑、追问、驳回 |

MVP 不实现真实身份认证。审核员由同一 Streamlit 应用中的工单工作台模拟。

### 4.2 四个端到端演示场景

1. **蓝牙连接失败**：低风险，自动完成分诊、证据检索、诊断、复核并解决。
2. **固件升级中断**：根据证据和信息完整度给出修复建议，或进入 `pending_user`。
3. **钱包秘密疑似泄露**：输入先脱敏，立即展示固定安全建议，工单强制进入人工审核。
4. **设备损坏与保修**：查询模拟设备和保修信息；记录不存在时生成最少必要追问。

## 5. 总体架构

```text
用户输入
  ↓
Ingress Guard（确定性规则：检测、脱敏、硬风险标记）
  ↓
创建脱敏工单与 TicketState
  ├─ 命中 critical → 固定即时安全建议 + escalated
  └─ 其余 → Triage Agent（意图、优先级、风险、缺失信息）
              ↓
         LangGraph 确定性路由
              ├─ low / medium 且信息不足 → pending_user
              ├─ high / critical 或分诊失败 → escalated
              └─ low / medium 且信息充分 → Diagnosis Agent
                    ├─ Knowledge Search Tool
                    ├─ User Profile Tool
                    ├─ Chain Status Tool
                    └─ Device / Warranty Tool
                         ↓
                    生成证据化回复草稿
                         ↓
                    Review Agent + Policy Guard
                         ├─ approve 且无人工门禁 → resolved
                         ├─ approve 且命中人工门禁 → escalated
                         ├─ revise → Diagnosis（最多一次）
                         └─ escalate → 人工审核
                                          ↓
                                approve / edit / ask / reject
```

LangGraph Orchestrator 不是第四个 Agent。它只负责状态初始化、条件边、最大步数、重试、暂停、恢复和事件聚合。

## 6. Agent 与 Tool 边界

### 6.1 三个 Agent

#### Triage Agent

职责：理解自然语言中的模糊意图，输出结构化分诊建议。

输入：

- `sanitized_input`
- `safe_history`
- 代码级 `sensitive_flags`

输出契约：

```text
intent
category
priority
risk_level
risk_flags
missing_fields
suggested_route
summary
```

约束：Agent 只能建议路由；代码级条件边做最终决定。Agent 不得降低 Ingress Guard 已判定的风险等级。

#### Diagnosis Agent

职责：选择必要的只读工具，整合事实与证据，生成可执行的回复草稿。

输入：

- 分诊结果
- 用户档案
- 知识库证据
- 设备、保修或链状态事实
- Review Agent 的结构化修改意见（仅返工时）

输出契约：

```text
diagnosis_summary
recommended_actions
evidence_refs
citations
draft_answer
remaining_unknowns
```

约束：知识型事实必须有可验证来源；检索失败时不得凭常识补写高风险操作。

#### Review Agent

职责：独立检查草稿的安全性、完整性、证据、引用和越权承诺。

输出契约：

```text
decision: approve | revise | escalate
issues
required_changes
safety_flags
reason_codes
```

约束：Review Agent 是最后一道自动质量门，不进行自动重试；异常、超时或结构解析失败时直接 `escalate`。它不能绕过代码级硬规则。

#### 结构化契约的精确定义

三个输出都使用 Pydantic 严格模式：所有字段必填、禁止额外字段、枚举值区分大小写、字符串自动去除首尾空格、列表去重。枚举外的值、缺字段、错误类型或违反下述跨字段规则，均视为结构化输出失败。

Triage 输出：

| 字段 | 类型与约束 |
|---|---|
| `intent` | `troubleshoot \| recovery \| warranty \| transaction_boundary \| security_incident \| security_report \| other` |
| `category` | `power \| usb_connection \| mobile_connection \| bluetooth_connection \| screen_buttons \| pin_lock \| firmware_repair \| backup_recovery \| device_loss_damage \| warranty_service \| transaction_boundary \| security_incident \| security_report \| other` |
| `priority` | `P0 \| P1 \| P2` |
| `risk_level` | `low \| medium \| high \| critical` |
| `risk_flags` | `list[RiskFlag]`，可空；`RiskFlag` 为 `secret_exposure \| phishing \| asset_loss \| unofficial_firmware \| address_mismatch \| suspicious_signature \| device_auth_failure \| remote_control` |
| `missing_fields` | `list[MissingField]`，可空；`MissingField` 为 `device_model \| app_os \| connection_type \| firmware_version \| error_state \| serial_last4 \| purchase_date \| transaction_hash \| chain_name` |
| `suggested_route` | `ask_user \| diagnose \| escalate` |
| `summary` | `str`，1–300 字符，不得包含被脱敏内容 |

Diagnosis 输出：

| 字段 | 类型与约束 |
|---|---|
| `outcome` | `draft \| need_user \| escalate`，仅为建议，Router 决定真实状态 |
| `diagnosis_summary` | `str`，1–500 字符 |
| `recommended_actions` | 0–6 个 `{text: str, evidence_refs: list[str]}`；`text` 为 1–300 字符 |
| `evidence_refs` | `list[str]`，引用本次 Tool 返回的证据 ID，不得自造 |
| `citations` | `list[{source_id, source_title, source_url}]`；三个字段均为非空字符串 |
| `draft_answer` | `str`，0–2000 字符 |
| `remaining_unknowns` | `list[MissingField]`，可空 |

Diagnosis 跨字段规则：

- `outcome = draft` 时，`draft_answer`、`recommended_actions` 和 `evidence_refs` 均非空，`remaining_unknowns` 为空；每条 action 至少绑定一个存在于顶层 `evidence_refs` 的证据 ID。
- `outcome = need_user` 时，`remaining_unknowns` 非空，`draft_answer` 为空；Router 进入 `pending_user`。
- `outcome = escalate` 时，`draft_answer` 不会自动发送；Router 进入 `escalated`。

Review 输出：

| 字段 | 类型与约束 |
|---|---|
| `decision` | `approve \| revise \| escalate` |
| `issues` | `list[str]`，每项 1–300 字符，可空 |
| `required_changes` | `list[str]`，每项 1–300 字符，可空，最多 6 项 |
| `safety_flags` | `list[RiskFlag]`，可空 |
| `reason_codes` | `list[ReviewReason]`；`ReviewReason` 为 `passed \| secret_exposure \| unsafe_action \| unsupported_claim \| missing_evidence \| invalid_citation \| incomplete_steps \| overpromise \| official_source_violation` |

Review 跨字段规则：`approve` 必须只有 `passed` 且 `issues / required_changes / safety_flags` 为空；`revise` 必须至少有一个 issue 和一项 required change；`escalate` 必须至少有一个非 `passed` reason code。Policy Guard 可以将任何 Agent 决策提升为 `escalate`，不得降低风险。

### 6.2 确定性 Tool 与 Service

| 组件 | 类型 | 原因 |
|---|---|---|
| Ingress Guard | Rule / Service | 钱包秘密检测和脱敏不能只依赖 LLM |
| Knowledge Search | Tool | 检索文档和 metadata 是事实查询 |
| User Profile | Tool | SQLite 查询不需要推理 |
| Chain Status | Tool | 返回模拟链状态事实 |
| Device / Warranty | Tool | 返回模拟设备和保修记录，禁止模型编造 |
| Ticket Repository | Tool / Service | 创建、更新、查询工单属于受控副作用 |
| Policy Guard | Rule / Service | 敏感信息、危险动作和官方来源是硬约束 |
| LangGraph Router | Orchestrator | 状态转移、重试和循环上限必须确定 |

月度安全报告保留为 V1 兼容能力，不进入 V2 核心演示路径。

### 6.3 必要信息与证据判定

Router 从 `config/orchestration.yml` 读取各类别的最低必要字段。MVP 固定规则如下：

| 类别 | 必要字段 | 自动处理的最低证据 |
|---|---|---|
| `power / usb_connection / mobile_connection / bluetooth_connection / screen_buttons / pin_lock / backup_recovery / device_loss_damage` | 无硬性字段；可先给通用安全排查 | 至少 1 条相关知识库证据 |
| `firmware_repair` | `device_model`、`error_state` | 至少 1 条固件知识证据；真实性失败命中人工门禁 |
| `warranty_service` | `serial_last4` | 1 条与序列号匹配的模拟设备 / 保修记录 |
| `transaction_boundary` | 调用链状态时需要 `transaction_hash`、`chain_name` | 1 条链状态记录和 1 条边界知识证据 |
| `security_incident` | 无；禁止为了补字段而延迟升级 | 固定安全模板；直接人工，不自动解决 |
| `security_report` | 沿用 V1 报告输入 | 不进入 V2 核心工单自动解决率 |
| `other` | 无 | 无可靠证据时人工处理 |

必要字段可以从当轮脱敏输入或已验证档案推导；否则记入 `missing_fields`。证据充分意味着：满足表中最低数量、证据 ID 来自本次只读 Tool 结果、每条事实性操作都绑定至少一个证据 ID。知识库引用有效意味着 `source_id`、标题和 URL 与检索 metadata 完全匹配；模型生成但检索结果中不存在的 URL 一律无效。没有达到这些条件的草稿不能自动进入 `resolved`。

“安全草稿”固定指：Diagnosis 契约通过、达到证据要求、Review 返回 `approve`、Policy Guard 通过、草稿不包含钱包秘密或禁止行为，且所有 URL 均通过官方来源策略。只有同时满足这些条件的草稿才能自动发送或供人工 `Approve`。

## 7. TicketState 设计

TicketState 只包含脱敏数据：

| 分组 | 字段 |
|---|---|
| 身份与输入 | `ticket_id`, `request_id`, `user_id`, `sanitized_input`, `safe_history`, `sensitive_flags` |
| 分诊结果 | `intent`, `category`, `priority`, `risk_level`, `risk_flags`, `missing_fields`, `suggested_route` |
| 事实与证据 | `customer_context`, `evidence`, `citations`, `tool_errors` |
| 草稿与复核 | `draft_answer`, `review_decision`, `review_reasons`, `revision_count`, `response_version` |
| 生命周期 | `status`, `requires_human`, `manual_gate_reason`, `human_decision`, `final_answer`, `status_events`, `last_error` |

不得定义或持久化以下 State 字段：

- `raw_input`
- 助记词、私钥、PIN 或 Passphrase 的原文
- 未脱敏的工具参数
- 完整内部 Prompt
- 模型 chain-of-thought

原始输入只作为 Ingress Guard 函数的瞬时参数存在。若检测到疑似钱包秘密，页面显示脱敏后的用户消息和固定安全警示。

## 8. 工单状态机

状态集合固定为：

```text
new
triaged
diagnosing
reviewing
pending_user
escalated
resolved
```

### 8.1 合法状态转移

| 当前状态 | 允许的下一状态 | 条件 |
|---|---|---|
| `new` | `triaged` | 输入已完成脱敏，Triage 成功且输出通过契约校验 |
| `new` | `escalated` | Ingress Guard 命中 `critical`，或 Triage 失败且不可恢复 |
| `triaged` | `pending_user` | `low / medium` 且缺少必要信息 |
| `triaged` | `diagnosing` | `low / medium`、信息足够且未触发人工门禁 |
| `triaged` | `escalated` | Triage 成功输出 `high / critical`，或命中确定性人工门禁 |
| `pending_user` | `triaged` | 用户新输入完成脱敏，且 Triage 成功 |
| `pending_user` | `escalated` | 补充输入命中 `critical`，或 Triage 失败且不可恢复 |
| `diagnosing` | `pending_user` | 工具成功但业务事实不足 |
| `diagnosing` | `reviewing` | 生成了有证据的回复草稿 |
| `diagnosing` | `escalated` | 工具失败、无证据或诊断失败 |
| `reviewing` | `resolved` | Review 与 Policy Guard 均通过，且 `requires_human = false` |
| `reviewing` | `diagnosing` | `revise` 且 `revision_count = 0` |
| `reviewing` | `escalated` | `requires_human = true`、复核失败、第二次未通过或 Reviewer 异常 |
| `escalated` | `resolved` | 人工批准已有安全草稿或编辑后重新通过 Policy Guard |
| `escalated` | `pending_user` | 人工决定向用户追问 |
| `escalated` | `escalated` | 人工驳回草稿并保留人工处理 |

`resolved` 是终态，不自动重新打开。用户提出新问题时创建新的 `ticket_id`。

### 8.2 禁止状态转移

- `new / triaged / diagnosing → resolved`，不得绕过 Review。
- `pending_user → diagnosing / reviewing / resolved`，用户新输入必须重新分诊。
- `escalated →` 任意自动执行状态，必须由人工命令恢复。
- `reviewing → diagnosing` 超过一次。
- 任意下游 Agent 降低代码级风险等级。
- 任意 Agent 自行写入最终状态；节点只返回建议字段，由 Router 改变状态。

## 9. 风险与安全策略

### 9.1 风险等级

| 风险 | 示例 | 自动化边界 |
|---|---|---|
| `low` | 蓝牙、USB、开机等普通排查 | 证据充分并通过复核后可自动解决 |
| `medium` | 固件中断、保修信息不完整 | 可自动诊断；证据不足时追问或人工 |
| `high` | 非官方固件、地址不一致、可疑签名 | 强制人工，风险不可自动降级 |
| `critical` | 钱包秘密泄露、钓鱼、盗币或资产已转出 | 立即固定安全提示并强制人工 |

### 9.2 优先级

- `P0`：`critical` 安全事件，需要即时安全提示。
- `P1`：`high` 风险或影响设备可信性的事件。
- `P2`：`low / medium` 普通售后事件。

`requires_human` 由代码根据风险和人工门禁配置生成，并在 Triage 后与 Diagnosis 后各计算一次。`high / critical` 始终为 `true` 并在诊断前升级；`medium` 若回复草稿包含设备重置、bootloader 恢复、钱包恢复或保修结论等需人工确认的动作，也设为 `true`，但允许先完成 Diagnosis 与 Review。这样，经过 Review 的安全草稿会通过 `reviewing → escalated` 进入人工队列，人工 `Approve` 在状态机中有明确可达路径。

### 9.3 硬安全规则

- 不索要、保存、复述助记词、私钥、PIN 或 Passphrase。
- 不代用户签名、转账、授权或远程控制设备。
- 不承诺追回链上资产、撤销不可逆交易或必然赔付。
- 不推荐非官方固件、第三方恢复工具或远程“资产恢复服务”。
- 固件、下载地址和联系方式只允许来自配置的官方来源白名单。
- 高风险标记具有粘性；LLM 不得将其自动降级。
- 人工编辑后的内容必须重新通过 Policy Guard 才能发送。

安全资产统一存放在 `config/security_policy.yml`，至少包含：

```yaml
policy_version: "2026-07-28.v1"
official_domains:
  - support.ledger.com
  - ledger.com
  - www.ledger.com
  - trezor.io
critical_response_template_zh: >-
  检测到疑似钱包秘密或资产安全风险。请立即停止继续分享；任何已经泄露的
  助记词或私钥都应视为不再安全。请只通过设备厂商官方应用和官方帮助中心
  创建新钱包并处理仍可控资产，不要接受远程控制，也不要再向任何人提供
  助记词、私钥、PIN 或 Passphrase。KeyGuard 已将本工单脱敏并转交人工审核。
```

该模板由 Ingress Guard 直接读取，不交给模型改写。`official_domains` 只控制固件下载、App 下载和联系支持等行动链接；普通知识引用必须与本次检索 metadata 中的完整 URL 精确匹配。域名比较使用规范化后的 hostname 精确匹配或其子域匹配，不使用字符串包含判断。

`agent/policies/security.py` 另有一条不可配置的最小故障提示：“系统暂时无法安全处理此请求。请不要继续分享助记词、私钥、PIN 或 Passphrase；请仅从设备厂商官方网站进入支持渠道。”配置文件缺失、YAML 无法解析、模板为空或白名单为空时，V2 在启动时 fail-closed：禁用模型调用与自动发送，页面只显示配置错误和这条最小故障提示。

## 10. Human-in-the-loop

MVP 使用 LangGraph checkpoint、`interrupt` 和恢复命令实现真实暂停与恢复。

- `thread_id` 使用 `ticket_id`。
- 图进入 `escalated` 时持久化完整但已脱敏的 TicketState。
- Streamlit 工单工作台展示等待审核的工单。
- 人工动作以结构化命令恢复图。

人工动作：

| 动作 | 规则 | 下一状态 |
|---|---|---|
| Approve | 仅当存在经过 Review 的安全草稿；发送前再次运行 Policy Guard | `resolved` |
| Edit & Send | 生成新 `response_version`；旧批准失效；编辑内容重新过 Policy Guard | 通过后 `resolved`，失败时保持 `escalated` |
| Ask User | 发送脱敏后的最少必要追问 | `pending_user` |
| Reject | 不发送草稿并记录原因 | 保持 `escalated` |

高风险用户不会无提示地等待：Ingress Guard 先展示固定、无模型生成的紧急安全建议，然后将详细工单交给人工。

## 11. 数据持久化

### 11.1 `tickets` 表

```text
ticket_id
request_id
user_id
status
category
priority
risk_level
sanitized_input
summary
risk_flags_json
missing_fields_json
evidence_refs_json
draft_answer
final_answer
review_decision
revision_count
response_version
requires_human
manual_gate_reason
created_at
updated_at
```

`ticket_id` 是主键，`request_id` 记录创建工单的首个提交 ID 并具有唯一约束；同一个首次提交最多创建一张工单。`pending_user` 的后续补充使用新的 `request_id`，沿用原 `ticket_id`，并作为 `ticket_events` 的新 command 记录。

### 11.2 `ticket_commands` 表

```text
command_id
ticket_id
command_type
status
lease_expires_at
result_status
result_event_id
error_code
created_at
updated_at
```

`command_id` 是主键，取值为 `request:{request_id}` 或 `action:{action_id}`；`ticket_id` 外键关联 `tickets`。`command_type` 为 `user_input \| human_approve \| human_edit_send \| human_ask \| human_reject`，`status` 为 `accepted \| in_progress \| completed \| failed`。

Repository 在任何模型或副作用调用前，以一个事务插入 command、将其从 `accepted` 更新为 `in_progress`，并写入 `command.accepted` 事件。正常结束时，业务状态、最终事件以及 command 的 `completed / result_status / result_event_id` 在同一事务提交；安全停止时标记 `failed` 并记录结构化错误码。

重复 `command_id` 的处理固定为：

- `completed`：返回已保存的 `result_status / result_event_id`，不重新执行。
- `in_progress` 且租约未过期：返回“处理中”，不并发执行。
- `in_progress` 且租约已过期：先核对业务状态和 checkpoint；一致时从最后已提交节点用同一 command 续跑，不一致时 fail-closed 到 `escalated`。
- `failed`：不自动续跑；用户或审核员显式重试时创建新的 `request_id / action_id`。

租约固定为 130 秒，比单次自动图 120 秒上限多 10 秒。模型和只读 Tool 可能在崩溃后重新调用，但所有状态写入、人工动作和最终回复都使用确定性幂等键；MVP 的“发送”是数据库中原子写入 `final_answer`，没有外部消息副作用。

### 11.3 `ticket_events` 表

```text
event_id
ticket_id
idempotency_key
command_id
step_index
node_name
event_type
from_status
to_status
summary
metadata_json
created_at
```

`event_id` 是自增主键，`ticket_id` 外键关联 `tickets`，`idempotency_key` 具有全局唯一约束；同一 `command_id` 内的 `step_index` 从 0 单调递增。

事件只记录结构化执行阶段，例如：

- `ingress_guard.redacted`
- `triage.completed`
- `diagnosis.evidence_loaded`
- `review.revision_requested`
- `routing.escalated`
- `human.edited_and_approved`

事件不记录自然语言思维过程、完整 Prompt 或未脱敏工具参数。

每次用户提交的 `command_id` 为 `request:{request_id}`，每次人工操作为 `action:{action_id}`。首个事件固定为 `command.accepted`，幂等键为 `command:{command_id}:accepted`；后续事件使用 `command:{command_id}:{step_index}:{event_type}`。这样一次提交可以产生多条事件，同时重复提交仍会命中唯一约束。

事件的 `step_index` 和幂等键由 Router 生成，不由 Agent 提供。重复事件键直接返回已保存事件；所有写操作在同一 SQLite transaction 中写业务状态和对应事件。模糊失败只允许 Repository 复用原幂等键确认或重试，不允许调用方构造新键绕过检查。

### 11.4 模拟设备与保修数据

MVP 新增 8 条模拟设备和保修记录，覆盖有效保修、过保、序列号不存在和信息缺失。数据必须在 README 中标明为模拟数据。

### 11.5 Checkpoint

MVP 固定使用 `langgraph-checkpoint-sqlite` 的 `SqliteSaver`，后端文件为 `data/keyguard_v2_checkpoints.sqlite3`；业务工单继续使用独立数据库文件，不共用连接。Checkpoint 仅保存已脱敏 TicketState，用于 Streamlit rerun 后的暂停与恢复，不能作为产品查询模型。

所有图调用都传入 `configurable.thread_id = ticket_id`。Streamlit rerun 后先按 `ticket_id` 查询业务工单，再通过 `graph.get_state` 恢复 checkpoint；只有处于 `pending_user` 或 `escalated` 且 checkpoint 与业务状态一致时，才接受新 `request_id / action_id` 恢复。若 checkpoint 不存在、损坏或状态不一致，工单保持或转为 `escalated`，写入 `system.checkpoint_restore_failed` 事件，页面显示安全错误；系统不得从头重跑模型或自动发送旧草稿。

Streamlit Cloud 本地文件系统仅适合作为 Demo。生产化需迁移到持久数据库和正式审计存储。

## 12. UI 设计

同一 Streamlit 应用使用两个 Tab。

### 12.1 客户对话

显示：

- 当前用户和设备档案。
- 工单 ID、状态、风险和优先级。
- 安全检查、问题分诊、诊断取证和安全复核四阶段进度。
- 最终答复与知识来源。
- 高风险场景的固定即时安全提示。

不显示：

- 模型思维链。
- 原始工具参数。
- 未脱敏输入。
- Reviewer 内部 Prompt。

### 12.2 工单工作台

显示：

- 按优先级和状态排序的工单队列。
- 脱敏问题摘要、类别、风险与当前状态。
- 结构化节点事件时间线。
- 证据引用和回复草稿。
- Approve、Edit & Send、Ask User、Reject 四个操作。

MVP 不实现复杂筛选、统计仪表盘、权限系统和多人协作。

## 13. 错误处理与降级

系统采用“有限重试 + fail-closed”。重试上限按节点 / Tool 调用计算，不按整张工单重新执行。

| 故障 | 处理 | 出口 |
|---|---|---|
| Triage / Diagnosis 超时或结构化输出失败 | 同一脱敏输入重试一次 | 仍失败 → `escalated` |
| 向量检索失败 | 使用现有关键词兜底并验证来源 | 仍无证据 → `escalated` |
| 用户档案缺失 | 记录上下文缺失 | 低风险继续非个性化诊断 |
| 保修查询成功但无记录 | 生成最少必要追问 | `pending_user` |
| 只读 Tool 瞬时异常 | 使用相同参数重试一次，不伪装成用户信息不足 | 仍失败 → `escalated` |
| Reviewer 异常、超时或解析失败 | 不重试，不使用未审核草稿 | `escalated` |
| 自动返工后仍未通过 | 停止循环 | `escalated` |
| 工单持久化失败 | 不自动重试非幂等写入，不自动发送草稿 | 使用同一幂等键确认或重试；失败则安全停止 |

固定执行边界：

- Triage 和 Diagnosis 每次模型调用超时 20 秒，每节点最多 2 次尝试；Review 每次超时 20 秒且只尝试 1 次。
- 每个只读 Tool 调用超时 8 秒，最多 2 次尝试。Ticket Repository、发送回复和人工动作等写操作不做无条件自动重试，只能复用原幂等键确认结果或重试。
- 单次自动图调用的 wall-clock 上限为 120 秒，超过后转 `escalated`；高风险固定安全提示不受模型超时影响。
- `revision_count` 初始为 0；Router 第一次执行 `reviewing → diagnosing` 时在同一事务中原子加到 1。第二次 Review 再返回 `revise` 时直接 `escalated`。模型和 Tool 的瞬时重试不计入 `revision_count`。
- 每次 `invoke / resume` 使用 `recursion_limit = 16` 个 LangGraph supersteps。HITL 暂停结束本次调用；恢复时获得新的 16-step 预算，但持久化的 `revision_count` 不重置。

## 14. 评测设计

### 14.1 数据集

总评测集固定为 48 条：

- 现有 30 条题目保留，并补充 `expected_intent`、`expected_priority`、`expected_route`、`required_behaviors` 和 `forbidden_behaviors`。
- 新增 6 条高风险与对抗样本。
- 新增 4 条信息不足和多轮补充样本。
- 新增 4 条设备与保修工具样本。
- 新增 4 条故障注入样本，共 4 条而不是每类 4 条：模型超时、RAG 无结果、只读 Tool 异常、Reviewer 失败各 1 条。

计数单位是“工单 case”。多轮 case 的所有用户补充和人工动作共用一个 `case_id`，只计 1 条；每一轮另有 `turn_index`，用于断言中间状态。48 条中，现有 30 条与新增 18 条互不重复。

### 14.2 指标

| 指标 | 定义 | MVP 目标 |
|---|---|---|
| 高风险升级召回率 | 应升级且成功进入人工的高风险样本 / 全部高风险样本 | 100% |
| 敏感信息违规率 | 索要、复述或持久化钱包秘密的样本 / 全部相关样本 | 0 |
| 意图路由准确率 | 意图和主路径均与标注一致的样本 / 全部样本 | ≥85% |
| 状态流转正确率 | 最终状态正确且无非法跳转的样本 / 全部样本 | ≥95% |
| 引用完整率 | 需要知识库依据且至少带 1 个可映射有效引用的回答 / 全部需要知识库依据的回答 | ≥95% |
| 禁止行为触发率 | 出现危险操作、越权承诺或非官方建议的样本 / 全部样本 | 0 |
| 安全降级通过率 | 故障注入后进入预期安全出口的样本 / 全部故障样本 | 100% |

同时记录但不预设简历数字：

- 每张工单的模型调用数。
- median 和 P95 延迟。
- Tool 调用成功率。
- 人工接管比例。
- 离线样本自动解决率。

这些值只有完成实测后才能用于 README 或简历，并必须注明来自离线模拟工单。

现有 83.3% 只保留为 V1 在原 30 条上的关键词覆盖率回归基线。V1 只报告原 30 条的关键词覆盖率和引用情况；V2 在完整 48 条上报告路由、状态、安全、引用和降级指标。二者不合并为同一个“准确率”，也不声称是严格同口径的效果提升。

### 14.3 测试层级

1. 单元测试：脱敏、硬规则、合法状态转移、数据库和事件。
2. Agent 契约测试：结构化输出字段、枚举、缺失字段和解析失败。
3. 端到端离线评测：输入、路由、工具、最终状态、回答和引用。
4. 故障注入：超时、空检索、异常、重复提交和返工上限。
5. 人工抽检：回答可执行性、表达质量和 Bad Case 复盘。

关键词覆盖率只作为辅助回归指标。LLM-as-Judge 可以作为可选辅助，不作为唯一评测依据。

## 15. 代码组织

新增以下文件：

```text
agent/orchestration/
  state.py
  graph.py
  routes.py
  events.py

agent/nodes/
  triage.py
  diagnosis.py
  review.py

agent/policies/
  security.py

agent/tools/
  warranty_tools.py

database/
  ticket_db.py

prompts/
  triage_prompt.txt
  diagnosis_prompt.txt
  review_prompt.txt

config/
  orchestration.yml
  security_policy.yml

eval/
  multi_agent_cases.json
  run_orchestration_eval.py
  orchestration_scorers.py

tests/
  test_ingress_guard.py
  test_ticket_state.py
  test_orchestration_routes.py
  test_agent_contracts.py
  test_risk_policy.py
  test_human_in_loop.py
  test_failure_fallbacks.py
```

重点修改：

```text
app.py
rag/rag_service.py
agent/tools/agent_tools.py
config/agent.yml
requirements.txt
README.md
DEPLOYMENT.md
```

`agent/react_agent.py` 保留为 V1 回归基线。应用默认使用 V2；配置项允许开发期间回退到 V1，但不在用户 UI 中暴露技术切换开关。

`rag/rag_service.py` 新增“只返回结构化证据”的接口；原 `rag_summarize` 在 V2 MVP 周期内保留供 V1 回归。

## 16. 三周执行路线

三周目标分为两个验收门：Day 10 达到“技术 MVP”，Day 15 达到“作品集就绪版”。30–45 小时是基于复用现有 RAG、SQLite、Streamlit 和测试资产的目标工时，不是对生产化交付的承诺。计划工时约为：状态 / 数据 6 小时，安全 / 路由 7 小时，三个 Agent 与 RAG 改造 10 小时，HITL / UI 7 小时，评测与故障测试 7 小时，部署与文档 5 小时，共 42 小时。

### Week 1：工单骨架与首个闭环

| 日程 | 任务 | 当日产出 |
|---|---|---|
| Day 1 | 一页 PRD、角色、场景、状态和风险定义 | PRD、验收清单、V1/V2 架构图 |
| Day 2 | TicketState、`tickets`、`ticket_events`、模拟保修数据 | 数据模型和 Repository 测试 |
| Day 3 | Ingress Guard、StateGraph 骨架和假节点 | 普通 / 高风险两条固定状态链 |
| Day 4 | Triage Agent 与结构化输出 | 10 条路由测试 |
| Day 5 | Streamlit 接入与首轮端到端演示 | 蓝牙工单 resolved；秘密泄露 escalated |

Week 1 验收：

- 原始钱包秘密不进入 Session State、日志、工单或模型。
- 普通工单与高风险工单均有持久化结构化轨迹。

### Week 2：三 Agent 闭环与人工审核

| 日程 | 任务 | 当日产出 |
|---|---|---|
| Day 6 | RAG 改造成结构化证据 Tool | 文档、metadata 和引用接口 |
| Day 7 | Diagnosis Agent 与设备 / 保修 Tool | 证据化草稿 |
| Day 8 | Review Agent、Policy Guard 和单次返工 | `approve / revise / escalate` 闭环 |
| Day 9 | 工单工作台与暂停 / 恢复 | 四个人工操作可演示 |
| Day 10 | 超时、重试、降级、四场景联调和部署 | KeyGuard 2.0 在线 Demo |

Week 2 验收：

- 三个 Agent 均有独立 Prompt 和结构化契约。
- 高风险工单不能自动关闭。
- 人工可以批准、编辑、追问或驳回。
- 所有自动发送内容先通过 Review 和 Policy Guard。

### Week 3：评测、可靠性和求职材料

| 日程 | 任务 | 当日产出 |
|---|---|---|
| Day 11 | 完成 48 条评测标注 | 多维评测集 |
| Day 12 | 运行基线和 V2，分类 Bad Case | 指标、混淆矩阵和问题清单 |
| Day 13 | 故障注入与可靠性修复 | 安全降级测试结果 |
| Day 14 | README、架构图、状态图和演示脚本 | 完整项目包装 |
| Day 15 | 简历三条、演示彩排和部署缓冲 | 求职交付包 |

Week 3 验收：

- 评测结果可从脚本复现。
- README 区分目标值、实测值和生产化差距。
- 5 分钟内可以演示普通自动解决、高风险人工接管和评测结果。

### 16.1 延期时的裁剪顺序

按以下顺序移除，不影响核心故事：

1. 复杂保修规则，仅保留 8 条模拟记录。
2. 月度报告进入 V2 主流程。
3. 复杂统计图和视觉美化。
4. 录屏和 10 个扩展面试问答，移到 post-MVP。
5. IP 定位、多模型切换和与核心工单闭环无关的重构。

技术 MVP 不得裁剪三个 Agent、显式 StateGraph、输入脱敏、独立复核和人工接管。作品集就绪版必须补齐 48 条专项评测、README 和 5 分钟讲稿；录屏与 10 个扩展问答不是三周验收阻塞项。

## 17. 产品验收标准

### 17.1 Day 10 技术 MVP

1. 同一仓库和同一 Streamlit 应用提供客户对话与工单工作台。
2. Triage、Diagnosis、Review 三个 Agent 均有独立输入输出契约。
3. Router 只允许规格中定义的状态转移。
4. 普通蓝牙工单能自动进入 `resolved`，并展示有效引用。
5. 钱包秘密疑似泄露时，原文不持久化，页面立即提示并进入 `escalated`。
6. 固件和保修场景可进入 `resolved`、`pending_user` 或 `escalated` 的正确出口。
7. Reviewer 失败、无证据或返工超限时不会发送未审核草稿。
8. Approve、Edit & Send、Ask User、Reject 四种人工动作各有至少 1 个可达自动化测试；UI 至少现场演示 Approve 和 Edit & Send，编辑后必须重新通过 Policy Guard。
9. README 明确所有用户、设备、保修、链状态和业务结果均为模拟。

### 17.2 Day 15 作品集就绪版

技术 MVP 全部通过后，还需满足：

1. 48 条评测可以一条命令运行并保存结果。
2. 指标输出区分 V1 基线、V2 实测、目标值和生产化差距。
3. 在线 Demo、架构图、状态图、评测结果和 5 分钟演示脚本可访问。
4. 简历三条只引用已实现功能和可复现实测值。

## 18. 演示与求职交付物

### 18.1 5 分钟演示

| 时间 | 内容 |
|---|---|
| 0:00–0:40 | V1 的单 Agent 局限和 V2 目标 |
| 0:40–2:00 | 蓝牙工单：分诊、检索、诊断、复核、自动解决 |
| 2:00–3:40 | 钱包秘密泄露：脱敏、即时安全提示、暂停和人工恢复 |
| 3:40–4:30 | 路由、安全、引用、状态和故障注入评测 |
| 4:30–5:00 | 为什么只有三个 Agent，以及 Agent / Tool 的取舍 |

### 18.2 必须交付

- KeyGuard 2.0 在线 Demo。
- 同一 GitHub 仓库中的 V1 和 V2 代码演进。
- PRD、总体架构图、状态机和 Agent / Tool 边界图。
- 48 条评测、结果文件、混淆矩阵和 Bad Case。
- README、部署说明、四条演示场景和 5 分钟讲稿。
- 简历项目标题和三条基于实测结果的项目描述。

Post-MVP 可选交付：演示录屏和 10 个扩展面试问答，不阻塞 Day 15 验收。

### 18.3 简历证据链

| 可验证产物 | 支持的简历主张 |
|---|---|
| StateGraph、三个 Agent 契约、状态图 | 设计并实现显式多 Agent 编排 |
| Ingress Guard、Policy Guard、HITL 工作台 | 设计安全边界和人工接管机制 |
| 48 条评测、结果文件和 Bad Case | 建立多维评测与迭代闭环 |
| 在线 Demo、README、演示脚本 | 完成从定义、原型、实现到验证的产品闭环 |

简历标题使用：

> **KeyGuard 2.0｜多 Agent 硬件钱包售后工单协同系统｜个人项目**

三条项目描述在实现和实测完成后撰写，只使用真实功能和实测数字。禁止：

- 把个人 Demo 写成真实生产系统。
- 把模拟自动解决率写成企业降本结果。
- 把关键词覆盖率写成回答准确率。
- 声称操作过真实资产、钱包、CRM 或客户数据。

## 19. 已知限制与生产化差距

- 本地 Hash Embedding 不是生产级语义检索方案。
- Streamlit Cloud 的 SQLite 和 Chroma 不是可靠持久存储。
- 模拟用户、设备、保修、链状态和工单不能代表真实业务效果。
- 无鉴权的工单工作台只适合个人演示。
- 硬规则和模式匹配不能发现所有形式的敏感信息，需要持续测试。
- 多 Agent 增加模型调用、延迟和错误传播，因此 MVP 限制为三个 Agent、一次自动返工；Triage、Diagnosis 和只读 Tool 最多重试一次，Reviewer 不重试。
- 生产化还需要正式隐私评审、访问控制、审计、内容安全、可观测性、持久数据库和真实系统对接。

## 20. 设计决策摘要

本规格固定以下决策：

- 在原仓库升级，不另起项目。
- 同一 Streamlit 应用，不拆 FastAPI 或微服务。
- 三个 Agent：Triage、Diagnosis、Review。
- LangGraph Router 是确定性编排器，不是第四个 Agent。
- RAG、用户档案、链状态、设备 / 保修和工单写入均为 Tool。
- 七个工单状态：`new`、`triaged`、`diagnosing`、`reviewing`、`pending_user`、`escalated`、`resolved`。
- 代码级风险不可被 LLM 降级。
- 自动返工上限为 1；Triage、Diagnosis 和只读 Tool 的瞬时错误重试上限为 1。
- Reviewer 不重试，失败时 fail-closed。
- 总评测集固定为 48 条。
- 所有简历指标必须来自实现后的可复现实测。

该范围可以由一个实现计划覆盖，不需要拆分成多个子项目。
