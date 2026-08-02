# DeepSeek 结构化输出兼容层设计

## 背景与问题

KeyGuard 2.0 使用 DeepSeek `deepseek-v4-flash` 驱动 Triage、Diagnosis 和 Review Agent，并通过 Pydantic 契约约束模型输出。当前 DeepSeek 能正确识别用户意图、问题分类、风险等级和下一步路由，但偶尔会产生字段之间的轻微矛盾。

已复现的真实返回为：

```text
category = bluetooth_connection
clarity = partial
missing_fields = []
suggested_route = diagnose
summary = 意图明确，进入诊断流程
```

其中 `clarity=partial` 与 `missing_fields=[]` 不一致。现有 `with_structured_output(TriageResult)` 会在 LangChain 输出解析阶段直接抛出 `ValidationError`，业务层无法取得原始字段，也无法进行安全修正。Graph 随后 fail closed，把正常的低风险咨询转为人工工单。结果是用户看到“自动分诊暂不可用”，后续诊断、回答和用户确认关闭流程均不会运行。

## 目标

1. 对 DeepSeek 结构化输出中可确定、低风险的字段矛盾进行白名单归一化。
2. 保持风险等级、风险标记、安全转人工和枚举边界的严格校验。
3. 让常见低风险问题稳定通过 Triage 和 Diagnosis，并进入正常回答流程。
4. 保留失败可观测性，使日志能够区分接口错误、原始解析错误、归一化失败和最终契约失败。
5. 不增加额外模型调用，不因兼容处理增加正常请求成本。

## 非目标

- 不自动修复未知枚举、非法类型、缺失关键安全字段或无法解释的模型输出。
- 不降低高风险与严重风险的转人工规则。
- 不用规则系统替代多 Agent 编排。
- 不改变已经完成的“仅由用户确认后关闭工单”状态逻辑。
- 不在数据库、前端或日志中展示模型的隐藏推理内容。

## 方案选择

采用“原始结构化结果 + 确定性白名单归一化 + 严格终态校验”。不采用仅修改提示词的方案，因为提示词无法保证第三方模型每次都满足跨字段约束；也不采用规则分诊，因为会削弱多 Agent Demo 的真实性。

## 架构与职责

### 1. 原始结构化调用

Agent 构建 runner 时使用 `with_structured_output(..., include_raw=True)`。调用结果包含：

- `raw`：模型原始工具调用消息；
- `parsed`：框架成功解析后的 Pydantic 对象；
- `parsing_error`：框架解析失败信息。

若 `parsed` 存在，继续沿用现有严格处理。若仅因 Pydantic 跨字段校验导致 `parsed` 为空，则从 `raw.tool_calls` 中提取目标工具唯一一次调用的 `args`。不接受普通文本、多个同名工具调用或未知工具名。

### 2. 兼容层边界

新增一个小型、无模型依赖的结构化输出适配模块。它只负责：

1. 校验包装结果形状；
2. 安全提取目标工具参数；
3. 调用具体 Agent 的白名单归一化函数；
4. 使用原 Pydantic Schema 做最终校验。

Triage 与 Diagnosis 各自维护本领域的归一化规则，通用适配器不理解业务枚举，也不自行猜测缺失内容。

### 3. Triage 白名单规则

允许以下确定性修正：

- `clarity=partial`、`missing_fields=[]`、`suggested_route=diagnose`：改为 `clarity=clear`。
- `clarity=clear` 且 `missing_fields` 非空：改为 `clarity=partial`，保留模型列出的缺失字段并继续诊断。
- `clarity=ambiguous`：必须仍然提供安全、非空的澄清问题和选项；否则不修复并 fail closed。

归一化不得修改：

- `risk_level`；
- `risk_flags`；
- `category`；
- `priority`；
- 任何高风险或严重风险所要求的升级路线。

现有 Triage 后置安全策略继续拥有最终权威：入口风险和风险标记只能维持或提升风险等级，不能被模型降低。

### 4. Diagnosis 白名单规则

Diagnosis 只修正等价的控制字段矛盾，例如“没有缺失字段但声明需要用户补充”与“已有缺失字段但声明可直接出草稿”。修正方向由已有的 `missing_fields`、证据集合和 action 枚举共同决定。

若模型已经引用一个通过可信来源校验的 knowledge evidence ID，但遗漏对应的 citation 展示对象，系统使用该 Evidence 中已经验证的 `source_title` 和 `source_url` 确定性补齐 citation。系统不得改写模型提供但与 Evidence 不一致的 citation，也不得为未知或不可信来源补齐引用。

多个不同 Evidence 允许指向同一个可信 URL。Policy Guard 必须逐条验证 URL 的可信性，不能因为去重后的 URL 数量小于 citation 数量就把重复的可信来源误判为违规。

以下情况一律不修复：

- 证据为空却生成事实性诊断；
- 引用来源不在受信策略内；
- 出现写操作、资产操作或敏感信息请求；
- 枚举、字段类型或工具参数非法；
- 无法通过单一确定规则得到唯一合法状态。

Review 目前不加入兼容白名单；其安全决定继续完全严格。只有真实回归证明存在同类、可确定的非安全字段矛盾时，才单独扩展。

## 数据流

```text
用户问题
  -> 入口安全检查
  -> DeepSeek 工具调用
  -> include_raw 结构化包装
      -> parsed 成功：直接严格校验
      -> parsed 失败：提取唯一工具 args
          -> 白名单归一化
          -> Pydantic 严格终态校验
  -> Triage / Diagnosis 现有安全后置策略
  -> 正常回答或安全转人工
```

兼容层不会写数据库。只有 Graph 现有原子提交逻辑可以更新工单状态。

## 错误处理与日志

日志只记录非敏感诊断信息：

- Agent 名称；
- 阶段：`provider_call`、`raw_extract`、`normalize`、`final_validate`；
- 异常类型；
- 是否应用了归一化规则及规则编号。

不得记录 API Key、完整提示词、用户敏感输入、模型隐藏推理或完整原始响应。

任何不在白名单内的错误继续使用现有 fail-closed 路径。用户端保留友好提示，工作台可看到节点失败类型，但不显示内部堆栈。

## 测试设计

### 单元测试

1. Triage：`partial + [] + diagnose` 归一化为 `clear`。
2. Triage：`clear + missing_fields` 归一化为 `partial`。
3. Triage：高风险字段在归一化前后完全不变，后置策略仍升级。
4. Triage：未知枚举、多个工具调用、缺少工具参数、普通文本输出均拒绝。
5. Diagnosis：只修正已定义的控制字段矛盾。
6. Diagnosis：已引用的可信知识证据缺少 citation 时，从验证后的 Evidence 补齐。
7. Diagnosis：无证据草稿、错误引用、不受信引用和敏感操作继续拒绝。
8. 已成功解析的 Pydantic 对象不经过原始提取路径。

### 集成测试

1. 模拟 DeepSeek 返回已复现的矛盾 Triage 字段，Graph 应继续进入 Diagnosis，而不是 `escalated`。
2. 清晰的低风险蓝牙问题完成回答后进入 `pending_user + resolution_confirmation`。
3. 用户继续追问仍复用原工单。
4. 用户点击“已解决”或明确说“解决了”后才进入 `resolved`。
5. 高风险输入仍直接或最终进入人工审核。

### 真实模型冒烟测试

使用本地已配置的 `deepseek-v4-flash`，至少验证：

- “蓝牙没法连接手机怎么办？”不再因 `partial + []` 转人工；
- 一条信息完整的蓝牙问题能够获得诊断回答；
- 一条高风险助记词泄露问题仍安全转人工或输出安全处置边界。

真实模型结果不替代确定性测试；若第三方模型波动，测试报告必须明确区分 Provider 波动和代码回归。

## 验收标准

1. 已复现问题不再显示“自动分诊暂不可用”。
2. 常见低风险问题能够到达正常回答和“等待用户确认”状态。
3. 兼容处理不增加模型调用次数。
4. 所有既有测试与新增测试通过。
5. 高风险、安全引用和工具权限测试保持原有严格结果。
6. 本地浏览器完成一次真实 DeepSeek 端到端验证，并在最终交付中如实报告未通过项。
