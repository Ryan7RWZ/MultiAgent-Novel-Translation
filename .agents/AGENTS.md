# MANT 仓库协作规则

本文件存放于 `.agents/`，按项目约定适用于整个仓库。接手项目的 AI 应在修改代码前主动阅读；后续目录若增加更具体的规则文件，以更具体的规则为补充或覆盖。

## 1. 项目边界

- 项目目标是构建一个面向中文网络小说英译的多智能体系统（MANT），兼顾术语一致性、人物与剧情连续性、文学可读性、质量审查和可观测性。
- 当前主运行链路是：术语分析 → 翻译 → 编辑 → 润色 → QA；QA 可将可执行反馈送回翻译阶段，重做次数受限。
- Python 最低版本为 3.11，使用 `src/` 布局。主要依赖为 LangGraph、OpenAI Python SDK、PyYAML、NumPy 和 python-dotenv；FAISS 是可选依赖。
- 保持核心模块轻量。数据契约优先使用 `dataclass`、`TypedDict` 和标准库；除非经过明确设计决策，不要额外引入 Pydantic 或前端构建链。

## 2. 修改前的检查

- 先运行 `git status --short --branch`，再查看相关 diff 和最近提交。仓库可能包含用户尚未提交的工作，禁止覆盖、回退或顺手格式化无关修改。
- 使用 `rg` / `rg --files` 查找代码与文件；编辑文件使用补丁方式，避免会整体重写文件或改变无关行尾的工具。
- 用户只要求审查、诊断或文档时，不得修改业务代码。
- 不使用 `git reset --hard`、`git checkout --` 等破坏性命令，除非用户明确指定且目标已经核实。

## 3. 配置、密钥与本地数据

- `config/settings.yaml`、`config/settings.local.yaml` 和 `.env` 是本地配置，必须保持在 `.gitignore` 中。
- 不读取后输出、不记录、不提交 API key。示例配置只能保存环境变量名和无敏感默认值，不能保存真实凭据。
- LLM 凭据通过配置中的 `api_key_env` 间接引用。不要在 Python、批处理、日志、测试夹具或文档中硬编码密钥。
- 除 CLI、启动脚本等组合层外，模块应通过参数接收配置；不要让 Agent、Memory 或 Pipeline 自行读取全局 YAML。
- `data/inputs`、`data/exports`、`data/traces`、SQLite 运行库及语料属于本地运行产物，默认不提交。只提交脱敏、授权且明确用于测试的最小夹具。
- 网文采集器在获得明确的数据来源授权、许可范围和 robots/站点规则之前保持禁用；不得擅自抓取真实站点。

## 4. 架构与契约

- 维持清晰分层：CLI/浏览器工作台负责组合；Pipeline/Workflow 负责流程；Agent 负责角色任务；Memory 负责检索与持久化；LLMClient 负责供应商调用；Observability 只观察，不改变业务结果。
- Agent 使用统一的 `AgentTask`、`AgentResult`、`BaseAgent.execute()` / `run()` 契约。新增角色应接入相同执行、错误和事件边界。
- 章节工作流状态统一放在 `TranslationState`。不要把每次运行的可变对象藏在 LangGraph 节点闭包中；术语、故事设定和 TM 检索结果应显式进入状态。
- `review_notes` 是返工时的最高优先级输入。QA 反馈必须具体、可执行，并由 `max_rework` 限制循环，避免无限调用。
- M1 语料管道应保持可重复和离线可验证：人工术语是高置信来源，空译名不得视为可用术语；术语与 TM 默认写入同一 SQLite 运行库，保证 `MemoryHub` 可直接检索。
- 当前 TM 的可重复写入依赖稳定键和 replace/upsert 语义；修改 schema 或键生成方式时必须同时提供迁移或重建说明。

## 5. LLM 流式输出

- `LLMClient.stream_complete()` 是真实流式调用入口；`complete()` 应收集同一流并返回完整文本，避免维护两套供应商逻辑。
- 结构化 JSON 只能在完整响应收集后解析。token 增量只发送到可观测性事件，不写进 `TranslationState`，避免 LangGraph 状态膨胀和并发合并问题。
- 仅在首个 token 产生前执行整次请求重试；已产生部分文本后不得从头重试并拼接，防止重复内容。调整重试策略时要区分可重试错误并补测试。
- `DRAFT:` 离线回退是开发和测试兜底，不等同于真实翻译成功。验收真实链路时必须在结果和事件中确认未触发回退。

## 6. 可观测性与浏览器工作台

- Agent、LLM 和工作流通过统一 EventBus 发出类型化事件；终端、JSONL、SQLite 和 SSE 是事件消费者。新增 sink 的异常不能中断翻译主链路。
- 默认禁止记录密钥、请求头和完整敏感 prompt。日志字段须经过脱敏；高频 token 事件应批量写 JSONL，SQLite 只保存指标/元数据而不保存 token 正文。
- trace 默认放在 `data/traces`。事件需要保留 `run_id`，并尽量带 `agent`、`stage`、耗时、重试与 token 使用量，便于关联一次运行。
- 浏览器工作台默认仅绑定 `127.0.0.1`。所有作品 ID、章节 ID、文件名和输入长度都要在服务端校验，不能让用户输入直接形成任意路径。
- 在持久化队列、资源隔离和成本控制实现前，保持单任务并发限制。监控服务关闭时应回收它启动的子进程。
- `start_mant.bat` 应优先使用项目虚拟环境，保留 `--check` 自检，并且永远不能把环境变量值打印到终端。

## 7. 测试与验收

对业务改动至少执行：

```powershell
python -m pytest -q
python -m unittest discover -q
python -m compileall -q src tests scripts
git diff --check
```

修改 M1 语料流程后还要执行：

```powershell
python scripts/verify_m1_output.py --aligned-dir data/aligned --glossary-db data/memory/mant.db --terminology data/raw/demo_work/terminology.md --min-chapters 3
```

修改一键启动或浏览器入口后执行：

```powershell
cmd /c start_mant.bat --check
```

- 测试默认不得访问真实 LLM、产生付费调用或依赖公网；用 fake client、临时目录和固定输入覆盖流式、返工、事件与安全边界。
- 需要真实供应商 smoke test 时，先说明会产生网络调用和可能费用；只用极短、非敏感文本，并记录模型、是否流式、是否回退和最终结果，不记录密钥。
- Windows 上 Git 的 LF/CRLF 提示不等于 `diff --check` 失败；不要为了消除提示而批量改写全仓库行尾。

## 8. 文档与进度维护

- `README.md` 面向使用者；`docs/` 记录架构、Agent、评估和可观测性设计；`PROGRESS.md` 是下一位维护者的事实性工作区快照。
- 每个重要里程碑、运行入口、已知阻塞或验证结果发生变化后，同步更新 `PROGRESS.md`。不要把临时猜测写成已完成功能。
- 明确区分：已提交、工作区未提交、仅设计、已用 fake 验证、已用真实供应商验证。
- 命令、路径、测试数量和产物统计应来自实际检查；过期结果要标日期。
