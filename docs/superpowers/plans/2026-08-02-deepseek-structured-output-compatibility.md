# DeepSeek 结构化输出兼容层实施计划

## 目标

在不增加模型调用、不放宽安全字段的前提下，让 Triage 与 Diagnosis 能处理 DeepSeek `deepseek-v4-flash` 的少量、可确定的跨字段矛盾，并继续通过原有 Pydantic 与安全策略终态校验。

## 实施顺序

### 1. 锁定失败行为

- 在 `tests/test_structured_output_compatibility.py` 增加 LangChain `include_raw=True` 包装结果夹具。
- 覆盖唯一工具调用提取、成功 parsed 快路径、多工具调用、未知工具、普通文本和非法参数拒绝。
- 在 `tests/test_agent_contracts.py` 增加已复现的 Triage `partial + [] + diagnose` 回归。
- 增加 Diagnosis 在“已有必要缺失字段”和“没有必要缺失字段”两种上下文下的控制字段矛盾回归。

### 2. 实现通用适配器

- 新建 `agent/orchestration/structured_output.py`。
- 提供只读的 `unwrap_structured_output`：兼容测试注入的裸 Pydantic/字典结果，以及 LangChain 的 `raw/parsed/parsing_error` 包装。
- 只接受目标 Schema 对应的唯一工具调用和字典参数；其他情况抛出类型或值校验错误。
- 日志只记录 Agent、阶段、异常类型和归一化规则编号。

### 3. 接入 Triage

- 模型 runner 改为 `include_raw=True`。
- 在严格 `TriageResult.model_validate` 前执行白名单归一化。
- 规则 T1：`partial + empty missing + diagnose` 改为 `clear`。
- 规则 T2：`clear + non-empty missing + diagnose` 改为 `partial`。
- 不修改风险、分类、优先级、路线或澄清内容；高风险继续由现有策略兜底。

### 4. 接入 Diagnosis

- plan 与 answer runner 均改为 `include_raw=True`，plan 仅解包不修正。
- 在 answer 终态校验前使用 Graph 已确定的 `missing_fields` 作为唯一权威上下文。
- 规则 D1：存在既定缺失字段，但模型输出完整 evidence-backed draft 时，改为 `need_user` 并恢复既定缺失字段。
- 规则 D2：不存在既定缺失字段，模型输出 evidence-backed `need_user + []` 时，改为 `draft`。
- 规则 D3：模型引用了已验证的可信知识证据但遗漏 citation 时，从 Evidence 的标题和 URL 确定性补齐。
- 修正 Policy Guard 对重复可信 URL 的误判，改为逐条可信性验证。
- 其他未知字段、证据、引用、敏感操作和枚举错误继续失败关闭。

### 5. 验证与提交

- 先运行新增测试，再运行 Agent 契约、Graph、Runtime 和用户确认关闭相关测试。
- 运行全量 `pytest`、`compileall` 和 `git diff --check`。
- 使用真实 DeepSeek 复测模糊蓝牙问题、清晰蓝牙问题和高风险问题。
- 刷新本地 Streamlit 页面完成端到端验收。
- 更新必要文档与测试基线，提交单独实现 commit。
