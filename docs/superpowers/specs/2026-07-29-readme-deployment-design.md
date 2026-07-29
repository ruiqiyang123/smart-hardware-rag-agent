# KeyGuard 2.0 README 重写与线上部署设计

## 背景

KeyGuard 2.0 已从单 ReAct 客服扩展为基于 LangGraph 的多 Agent 工单协同 Demo，并增加 DeepSeek V4 Flash Provider、安全门禁、工单状态机、checkpoint、人工工作台和离线评测框架。现有 README 技术信息完整，但首屏信息密度较高，对第一次打开仓库的面试官和多 Agent 初学者不够友好。

本次改写参考 `bcefghj/multi-agent-ecommerce-system` 的教学型叙事：先用一句话解释项目，再用痛点对比、架构图、Agent 拆解和“小白解读”逐步展开；不复制其具体内容，也不使用未经本项目验证的性能、业务或准确率数据。

## 目标与读者

README 同时服务两类读者，并按阅读顺序分层：

1. 面试官或招聘者应在前两屏理解项目价值、核心编排方式、可演示能力和技术亮点。
2. 开发者或学习者应能继续找到架构边界、核心代码、本地运行、测试、评测和部署信息。

README 是项目主页，不替代 `DEPLOYMENT.md`、`docs/DEMO_SCRIPT.md` 和代码级文档。详细部署故障排查、完整面试准备和实现计划继续留在各自文档中。

## 内容结构

README 使用以下固定顺序：

1. 标题、副标题、技术徽章和在线体验入口。
2. 目录。
3. “这个项目是什么”：一句话解释多 Agent 售后工单。
4. “为什么需要它”：普通单 Agent Demo 与 KeyGuard 2.0 的问题对比。
5. 精简架构图：展示用户输入、Ingress Guard、三个 Agent、确定性 Router、Policy Guard 和 Human-in-the-loop。
6. 三个 Agent 说明：每个角色均包含职责、输入、输出和明确边界。
7. “Router 为什么不是 Agent”：说明模型生成与控制面分离。
8. 核心实现：选择状态图、路由、安全脱敏、工单恢复和人工接管等真实代码或伪代码片段，并为关键片段提供简短“小白解读”。
9. 在线演示：列出蓝牙故障、固件中断、敏感信息和保修判断四条可操作路径。
10. 快速开始：Python 3.11、依赖安装、`.env`、知识库初始化、测试和 Streamlit 启动。
11. 模型与配置：以 DeepSeek V4 Flash 为默认 Demo Provider，解释 Provider 配置隔离和 fail-closed 行为。
12. 测试与评测：只写仓库内可复现的测试命令、案例规模和指标含义。
13. 项目结构。
14. 面试高频问题与可改写的简历项目描述。
15. 已知限制、部署入口和免责声明。

## 表达与真实性规则

- 使用中文为主，保留必要的英文技术名词。
- 每个核心概念先用通俗语言解释，再提供技术细节。
- 保留 Mermaid 架构图和状态图，但减少重复图表。
- 代码片段必须与当前仓库真实接口和文件一致；如果为了阅读简化，明确标注为简化示意。
- 不使用“企业级”“生产可用”等无法证明的标签。
- 不宣称 CTR、成本下降、SLA、准确率或 P99 等未通过本项目真实运行得到的数据。
- 测试数量只引用本次发布前重新运行得到的结果；V1 关键词覆盖率和 V2 编排案例不能混写为答案准确率。
- 明确所有用户、品牌、保修、链状态和资产场景均为虚构或模拟数据。
- README 不包含 API Key、操作员令牌、真实 `.env` 内容、本地数据库或日志。

## 视觉材料

优先使用 GitHub 可直接渲染的 Mermaid 图。只有当前 KeyGuard 2.0 页面截图与实际线上功能一致时，才把截图加入 README；旧版 V1 截图不作为 V2 能力证据。图片应放在 `assets/`，文件名能说明页面和版本。

## 部署设计

发布目标为现有 GitHub 仓库 `ruiqiyang123/ai-hardware-cs-agent` 的 `main` 分支，以及已存在的 Streamlit Community Cloud 应用 `https://ai-hardware-cs-agent.streamlit.app/`。

部署顺序：

1. 完成 README，检查 Markdown 链接、Mermaid、命令和事实陈述。
2. 运行与发布风险相匹配的自动化测试和本地 Streamlit 冒烟验证。
3. 检查 `git status`、待推送提交、忽略规则和仓库对象，确认 `.env`、API Key、数据库、checkpoint、日志及缓存未被跟踪。
4. 将 README 修改提交到 `main`，把本地领先的完整 KeyGuard 2.0 提交历史推送到 `origin/main`。
5. 由现有 Streamlit Cloud 与 GitHub 的关联触发重新部署；如果自动部署未触发，则进入 Streamlit 应用管理页执行重启或重新部署。
6. DeepSeek Key 只进入 Streamlit Secrets，不写入文件或 Git。若必须把本地私密 Key 传入 Streamlit Cloud，在实际提交 Secrets 的动作前再次取得用户确认。
7. 线上验收页面标题、`DeepSeek · deepseek-v4-flash`、客户对话、工单工作台和至少一条安全的非敏感演示路径；同时检查部署日志和浏览器错误。

## 失败处理

- GitHub 推送失败时保留本地提交，不强制覆盖远端；先判断认证、分支保护或远端新提交。
- 远端出现新提交时不执行 force push，先拉取并评估能否安全整合。
- Streamlit 构建失败时检查 Python 版本、依赖安装、入口文件和 Secrets 配置，修复后重新验证。
- 未配置或配置错误的 Provider Secrets 应保持 fail closed，不通过把 Key 提交到仓库解决。
- 线上 SQLite 和 checkpoint 丢失属于 Community Cloud 易失文件系统限制，不能宣称已恢复历史工单。

## 验收标准

- GitHub README 首屏能在两屏内说明项目对象、三个 Agent、确定性 Router、安全边界和在线体验入口。
- README 中所有本地相对链接存在，关键命令与当前项目一致。
- 测试和评测表述可由仓库命令复现，没有未验证的业务指标。
- Git 历史中不存在本地 `.env`、DeepSeek Key、操作员令牌或运行时数据库。
- `origin/main` 指向本次发布提交。
- Streamlit 在线地址可以打开并展示 KeyGuard 2.0；DeepSeek 配置、两个标签页和页面基本交互通过冒烟检查。

## 非目标

本次不重构多 Agent 业务代码，不迁移 SQLite，不新增生产级认证，不连接真实钱包、厂商售后或链上系统，也不把 Streamlit Community Cloud 描述成生产部署。
