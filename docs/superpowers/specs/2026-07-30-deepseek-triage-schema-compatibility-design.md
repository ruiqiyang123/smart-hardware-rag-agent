# DeepSeek 分诊 Schema 兼容修复设计

**日期：** 2026-07-30  
**状态：** 用户已批准 A 方案，等待书面规格复核  
**目标：** 修复 DeepSeek V4 Flash 在普通售后问题中生成配置外 `missing_fields`、导致分诊 fail-closed 的兼容问题，同时保留现有安全校验和人工升级边界。

## 1. 问题与证据

线上使用 `deepseek-v4-flash` 处理“硬件钱包开不了机怎么办？”时，模型能够正确输出 `category=power`，但同时输出：

```json
{
  "missing_fields": ["device_model", "error_state"],
  "suggested_route": "ask_user"
}
```

当前 `config/orchestration.yml` 只允许以下分类声明必要字段：

- `firmware_repair`: `device_model`, `error_state`
- `warranty_service`: `serial_last4`
- `transaction_boundary`: `transaction_hash`, `chain_name`

因此 `TriageAgent` 正确拒绝了 `power` 分类的配置外字段，Graph 按 fail-closed 策略把工单升级为人工处理。连续失败还会触发已有请求恢复提示，使用户误以为输入框不能继续提问。

已确认：DeepSeek API、`deepseek-v4-flash`、非思考模式和基础 Tool Calling 均可用；失败发生在模型结构化结果与项目业务约束之间，而不是 API Key、余额或网络连接问题。

## 2. 方案选择

采用已批准的 A 方案：强化 Tool Schema 与分诊 Prompt，让模型在生成参数时直接看到必要字段的权威映射规则。

不采用以下方案：

- 不扩大 `power`、蓝牙等分类的必要字段配置；这会改变产品流程并增加无必要追问。
- 不在后处理阶段静默删除模型生成的配置外字段；这会掩盖模型违约并削弱 fail-closed 约束。
- 不切回 MiMo，也不自动回退其他 Provider；DeepSeek 继续作为默认聊天模型。

## 3. 行为契约

分诊输出必须满足：

1. `required_fields` 是必要字段的唯一权威来源。
2. 选中的 `category` 出现在 `required_fields` 时，`missing_fields` 只能是该分类配置值的子集。
3. 选中的 `category` 不在 `required_fields` 时，`missing_fields` 必须为 `[]`。
4. `missing_fields` 非空时使用 `suggested_route=ask_user`；普通低风险且无必要字段时使用 `diagnose`。
5. 代码端原有严格校验继续保留；模型仍然违约时必须失败并安全转人工，不做静默修正。

## 4. 组件改动

### 4.1 `agent/orchestration/state.py`

为 `TriageResult.missing_fields` 增加明确的 JSON Schema 描述：字段只能来自输入中当前分类对应的 `required_fields`；分类不存在时必须返回空数组。

该描述会进入 `ChatOpenAI.with_structured_output(..., method="function_calling")` 生成的工具参数 Schema，使约束靠近模型实际生成字段的位置。

### 4.2 `prompts/triage_prompt.txt`

把现有“必要字段只按配置判断”扩展为不可歧义规则：

- `required_fields` 是权威白名单；
- 分类未出现在映射中时，`missing_fields=[]`；
- 不得凭常识为其他分类新增必要字段。

不增加解决方案、推理过程或 Provider 专用业务逻辑。

### 4.3 安全校验

`agent/nodes/triage.py` 中对配置外必要字段的拒绝逻辑保持不变。修复目标是让 DeepSeek 更稳定地产生合规输出，而不是降低验证标准。

## 5. 数据流

1. Runtime 将脱敏后的问题、入口风险和 `required_fields` 传给 Triage Agent。
2. Tool Schema 在 `missing_fields` 字段旁声明权威映射规则。
3. System Prompt 再次声明“分类不在映射中必须返回空数组”。
4. DeepSeek 生成 `TriageResult`。
5. 现有 Pydantic 与业务校验验证枚举、风险一致性和分类字段白名单。
6. 合规结果进入 Diagnosis；违约结果继续 fail closed 并转人工。

## 6. 测试设计

### 自动测试

- `TriageResult` JSON Schema 的 `missing_fields` 包含权威映射描述。
- 分诊 Prompt 明确包含“分类不在 `required_fields` 时返回空数组”的规则。
- 现有测试继续证明配置外 `missing_fields` 会抛出错误，防止安全约束被弱化。
- Model factory、Provider 配置、Graph、人工接管、恢复和全量测试继续通过。
- 运行 `git diff --check` 与 Python 编译检查。

### 真实 DeepSeek 回归

使用用户已授权的私密 Key，在不输出 Key 的前提下验证：

1. 结构化 Triage 调用处理“硬件钱包开不了机怎么办？”，返回 `category=power` 且 `missing_fields=[]`。
2. 使用临时 ticket/checkpoint 数据库执行完整 V2 Runtime，不再返回 `TRIAGE_FAILURE`。
3. 线上 Streamlit 使用同一问题完成一次真实路径，不再显示“自动分诊暂不可用”。

真实回归只使用虚构测试问题，不提交响应、密钥、数据库或日志。

## 7. 部署与恢复

1. 单独提交实现和测试，推送 `main`。
2. 等待 GitHub CI 通过并由 Streamlit Cloud 自动重部署。
3. 清空当前测试会话，消除旧失败请求留下的恢复提示。
4. 验证页面仍显示 `DeepSeek · deepseek-v4-flash`、应用公开可访问、两个 Tab 正常。
5. 若真实 DeepSeek 仍产生配置外字段，保持线上 fail closed，不临时放宽校验；回到 Schema/Prompt 兼容层继续修正。

## 8. 非目标

- 不修改 `config/orchestration.yml` 的必要字段集合。
- 不改变工单状态机、幂等键、checkpoint 或恢复语义。
- 不新增运行时模型切换器或 Provider 回退。
- 不修改 Diagnosis、Review、Policy Guard 的安全规则。
- 不把在线失败消息改成伪成功，也不隐藏人工升级结果。
