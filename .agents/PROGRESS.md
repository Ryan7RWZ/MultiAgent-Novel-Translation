# MANT 项目交接与当前进度

> 快照日期：2026-07-18（Asia/Shanghai）
>
> 分支：`main`，与 `origin/main` 同步
>
> 状态：核心 M1、章节多 Agent 链路、流式事件、实时监控和浏览器输入工作台已经合入 `main`；真实供应商的 CLI 与浏览器 API 最小端到端链路已在本机验收通过。当前工作区新增了无 LLM 的确定性初始切片、逐片下游处理及输出完整性保护，尚未提交。

## 1. 项目目标

MANT（Multi-Agent Novel Translation）用于把中文网络小说翻译为英文。项目不是把一段文本直接交给单一模型，而是让不同角色分别负责术语、初译、编辑、文学润色和质量审查，并用记忆系统维护跨章节一致性。

当前预期主链路为：

```text
中文章节
  → 编排器 Orchestrator
  → 术语专家 Terminologist
  → 翻译专家 Translator
  → 编辑 Editor
  → 润色 Polisher
  → 质量审查 QA
      ├─ pass：导出译文
      └─ rework：携带 review_notes 返回 Translator（最多 max_rework 次）
```

另有两个前置/辅助方向：

- M1 数据管道：从获得授权的中英双语章节生成对齐句对、术语库和翻译记忆库。
- Baseline 与评估：提供单模型基线，之后与多 Agent、消融实验和自动/人工指标比较。

## 2. 技术栈

| 层 | 当前技术 |
|---|---|
| 语言与打包 | Python 3.11+、setuptools、`src/` layout |
| 工作流 | LangGraph，显式 `TranslationState`，QA 条件回环 |
| LLM | OpenAI Python SDK，兼容 OpenAI Chat Completions 的供应商；fast/strong 双层模型配置 |
| 配置 | YAML；密钥用 `api_key_env` 指向环境变量；本地 `settings.yaml` 被 Git 忽略 |
| 数据与记忆 | SQLite、JSONL、Markdown；术语表、StoryBible、TM；NumPy，FAISS 可选 |
| 可观测性 | 类型化 EventBus、ContextVar run scope、终端/JSONL/SQLite sink、SSE 实时推送 |
| 浏览器界面 | Python 标准库 HTTP 服务 + 内嵌 HTML/CSS/JavaScript；无需 Node 构建 |
| 测试 | `unittest` 测试用例，可由 pytest 运行；fake LLM 与临时目录隔离外部依赖 |
| Windows 启动 | `start_mant.bat`，支持浏览器监控、自检和拖入 TXT 直译 |

项目元数据与依赖入口见 `pyproject.toml`，命令行入口为 `mant = mant.cli:main`。

## 3. 目录结构

```text
MultiAgent-Novel-Translation/
├─ .agents/
│  ├─ AGENTS.md                # 仓库长期协作规则（本次新增）
│  └─ PROGRESS.md              # 当前工作区交接（本文件）
├─ README.md                    # 用户入口、阶段说明和运行命令
├─ pyproject.toml               # Python 包、依赖和 mant CLI
├─ start_mant.bat               # Windows 一键启动/拖放入口
├─ config/
│  ├─ settings.example.yaml     # 可提交的无密钥示例
│  └─ settings.yaml             # 真实本地配置，已被 .gitignore 忽略
├─ data/
│  ├─ raw/                      # 本地原始双语章节和人工术语
│  ├─ aligned/                  # M1 JSONL 对齐结果
│  ├─ memory/                   # SQLite 术语/TM 运行库
│  ├─ inputs/                   # 浏览器或 CLI 输入
│  ├─ exports/                  # 翻译结果与运行元数据
│  └─ traces/                   # JSONL/SQLite 可观测性事件
├─ docs/
│  ├─ architecture.md
│  ├─ agent-design.md
│  ├─ evaluation-plan.md
│  ├─ roadmap.md
│  ├─ segmentation.md           # 无 LLM 初始切片的不变量、算法与配置
│  └─ observability.md          # 实时监控设计
├─ scripts/
│  ├─ run_m1_pipeline.py        # M1 兼容脚本入口
│  └─ verify_m1_output.py       # M1 八项验证
├─ src/mant/
│  ├─ agents/                   # BaseAgent 与六个角色
│  ├─ baseline/                 # 单模型基线翻译
│  ├─ llm/                      # LLMClient、重试和流式输出
│  ├─ memory/                   # Glossary、StoryBible、TM、VectorStore、MemoryHub
│  ├─ observability/            # 事件、sink、运行上下文、SSE dashboard
│  ├─ pipeline/                 # 采集、清洗、对齐、术语提取和统一 runner
│  ├─ segmentation.py           # 结构优先、token 预算约束的确定性初始切片
│  ├─ workflow/                 # TranslationState 与 LangGraph 章节工作流
│  └─ cli.py                    # m1-pipeline/baseline/translate-chapter/monitor
└─ tests/                       # 31 个离线测试，覆盖切片、记忆、流程、返工、事件和质量规则
```

`data/` 中的运行内容默认被忽略；目录中现有 M1 演示产物是本地验证用事实，不应误认为待提交的数据集。

## 4. Git 与最近提交

最近提交（由新到旧）：

1. `3de85e5`（2026-07-18）— 合并 PR #2：浏览器工作台浅色简约界面。
2. `52c2ef6`（2026-07-18）— 重构浏览器工作台前端。
3. `1f8d20d`（2026-07-18）— 合并 PR #1：可观测流式翻译工作台。
4. `54cf988`（2026-07-18）— 完成流式、多 Agent、可观测性和浏览器工作台功能提交。

重要提示：`agent/observable-streaming-workbench` 的功能已经通过 PR #1/#2 合入 `main`。此前关于“尚未进入 main”或“新增文件尚未跟踪”的描述均为历史状态。

## 5. 已完成功能

### 5.1 工程与配置

- 已建立 Python 包、CLI、配置示例、文档、数据目录说明和测试骨架。
- 已创建真实 `config/settings.yaml`，并通过 `.gitignore` 排除；配置支持 fast/strong 模型层、内存、流程和可观测性参数。
- 当前本机配置通过 `api_key_env` 引用环境变量；文档、事件与日志均未保存实际密钥值。
- CLI 已有四个正式子命令：`m1-pipeline`、`baseline`、`translate-chapter`、`monitor`。

### 5.2 M1 数据与记忆

- 已实现清洗、章节切分、确定性动态规划句对齐、术语提取/合并和统一 pipeline runner。
- 人工 `terminology.md` 被视为高置信术语；离线自动提取为空时会剔除空译名，避免污染词库。
- 术语与 TM 可写入同一个 SQLite 数据库；M1 对齐结果会同步到 `tm_pairs`，可由 `MemoryHub` 直接检索。
- `verify_m1_output.py` 当前检查 JSONL schema、句对/章节数、terms、TM 同步、非空译名率、术语命中率和双语锚点一致性，共 8 项。
- 此前交接环境的演示产物统计为：38 个句对、4 章、16 条非空术语、38 条 TM；人工术语命中 11/11，锚点 69/69。当前 macOS 工作区未包含这些被 Git 忽略的本地夹具，因此本次未单独复跑 verifier；M1 runner 的离线质量测试已通过。

### 5.3 多 Agent 翻译链路

- 六种角色已具备统一 Agent 执行边界：Orchestrator、Terminologist、Translator、Editor、Polisher、QA。
- Translator 使用 strong tier，并在返工时把 QA 的 `review_notes` 作为最高优先级要求。
- QA 的解析失败回退会提供可执行建议，而不是只有空泛的低分结果。
- LangGraph 已实现 QA pass/rework 分支和有限返工；默认最大返工次数为 2。
- 检索到的术语、故事设定和 TM 显式进入 `TranslationState`，避免把每次运行数据藏进共享闭包。
- Translator、Editor、Polisher 和 QA 均按同一机械片段序列执行；章级 QA 分数按源片 token 加权，只有全部片段通过且无阶段失败才判全章通过。
- 任一片段调用失败只回退该片，不会用残稿覆盖整章；失败详情、逐片 QA 和章级输出长度比进入运行元数据。
- 章节运行结果会导出译文和 JSON 元数据。

### 5.4 流式 LLM 与实时监控

- `LLMClient` 已使用 Chat Completions `stream=True`；`complete()` 通过收集 `stream_complete()` 保持兼容。
- 已处理流式重试边界：首 token 前按普通策略重试；产生部分文本后若网络中断，或供应商以 `finish_reason=length` 截断，丢弃残稿并发起一次完整新调用。重试仍失败时返回空结果，由片段级工作流显式降级，绝不把部分文本当成完成稿。
- 已实现 `RunEvent`、运行上下文和 EventBus，以及终端、JSONL、SQLite sink。
- 高频 token 可批量写入 JSONL；SQLite 不保存 token 正文；sink 失败不会中断主流程。
- 已实现本地 SSE dashboard，可实时显示运行、Agent 状态、LLM token、重试、QA 和结果事件。

### 5.5 确定性初始切片（当前工作区，尚未提交）

- 新增 `mant.segmentation`，初始切片不调用 LLM；按标题/场景、空行/段落、句子、分句、空白和最终硬切的顺序生成候选边界，并在每个强边界区间做确定性动态规划。
- 单片正文使用机械 token 估算并受 `max_core_tokens` 硬约束；相邻上/下文有独立预算，且不能跨标题/场景强边界。
- 线上翻译不再复用 M1 的 `clean_text`，避免删除小说中的重复行；只统一换行、去 BOM 和控制字符。片段按序拼接必须精确还原规范化原文。
- `TranslationState` 新增 `source_text`、`segment_meta`、`segmentation_stats`；章级 Agent 使用无损原文，Translator 只翻译核心片，相邻上下文明确标为不可输出。
- CLI 已读取 `segmentation.*`，导出切片统计；事件层新增 `segmentation.completed` 和 `segmentation.hard_split`。
- 下游 Editor、Polisher、QA 已改为逐片执行和确定性顺序归并；润色稿相对初稿的异常缩短/膨胀会触发片段级回退，阈值由 `workflow.min_polished_segment_ratio` / `max_polished_segment_ratio` 配置。

### 5.6 浏览器输入与一键启动

- 浏览器页面支持直接粘贴文本或拖入 UTF-8 `.txt` 文件。
- 页面支持作品 ID、章节 ID、最大返工次数，显示六个 Agent 状态、流式文本和最终译文。
- 服务端提供 `POST /api/translate` 和 `GET /api/jobs/{id}`，校验 ID、文件类型和输入长度；任务通过正式 CLI 子进程执行。
- 当前服务限制同一时间一个任务，关闭监控服务时会回收子进程。
- `start_mant.bat` 支持双击打开浏览器、拖放 TXT 直接翻译以及 `--check`；优先项目虚拟环境，并在启动时从 Windows 用户环境刷新 `AW4W_API_KEY`（不打印值）。

### 5.7 已有验证

2026-07-18 本次交接实际执行：

```text
python -m pytest -q                 → 20 passed
python -m unittest discover -q     → Ran 20 tests, OK
python -m compileall -q ...        → 通过
git diff --check                   → 通过，仅有 LF/CRLF 提示
verify_m1_output.py                → 8 PASS / 0 FAIL
start_mant.bat --check             → CONFIG / IMPORT / PYTHON 全部 ok
```

2026-07-18 又在 macOS 本机安装 Python 3.11.15、创建 `.venv` 并执行：

```text
.venv/bin/python -m pytest -q             → 20 passed
.venv/bin/python -m unittest discover -q → Ran 20 tests, OK
compileall + git diff --check             → 通过
```

确定性初始切片实现后再次离线执行：

```text
.venv/bin/python -m pytest -q             → 26 passed
.venv/bin/python -m compileall -q src tests → 通过
git diff --check                          → 通过
```

逐片下游处理、流式残稿重试与完整性保护实现后执行：

```text
.venv/bin/python -m pytest -q             → 31 passed
.venv/bin/python -m unittest discover -q → Ran 31 tests, OK
.venv/bin/python -m compileall -q src tests → 通过
git diff --check                          → 通过
```

另用 fake LLM 对 253 片完整 LangGraph 压力验证：Translator、Editor、Polisher、QA
各执行 253 次，`draft_segments`、`polished_segments`、`segment_qa` 均为 253 项，
无片段失败，顺序拼接结果精确符合预期，工作流本身耗时约 0.4 秒。

另用 29 字非敏感样例经正式 CLI 验证新入口：场景线被保留并形成 2 片，回拼通过、
无硬切，六 Agent、QA 与导出均完成。当前 Codex 进程及 macOS `launchctl` 用户会话
均看不到配置指定的 `DEEPSEEK_API_KEY`，因此该次为 DRAFT 降级验证；上文记录的
真实供应商短文本验收仍是此前已完成的独立事实。

随后用自拟的极短非敏感文本和真实 DeepSeek OpenAI 兼容接口完成两次最小验收：

- 正式 CLI 流式链路：6 个 Agent 全部完成，5 次 LLM 调用全部成功，46 个事件、10 个 token 合批事件，QA 10.0 `pass`，耗时约 12.9 秒，0 次返工，且没有 `llm.fallback` / `llm.failed`。
- 浏览器工作台 API 链路：`POST /api/translate` → 正式 CLI 子进程 → trace/SSE → 结果导出完整成功；任务退出码 0，47 个 SSE 事件、6 个 Agent、QA 10.0 `pass`，没有 DRAFT 或 LLM 失败。验收后监控进程已关闭并释放 8765 端口。

以上验证确认了真实供应商、流式 token、多 Agent、浏览器任务和导出之间的组合链路。它仍只代表短文本 smoke test，不代表长章节质量、成本和上下文安全已经验收。

### 5.8 整本级长文本真实验收（2026-07-18）

使用 `data/raw/斗罗大陆外传神界传说..txt` 的 GB18030 原文生成 UTF-8 副本，
对规范化后 215,938 字文本执行真实 DeepSeek 全链路。为避免 QA 失败触发最多三轮
253 片重译，本次固定 `max_rework=0`。总耗时 2675.3 秒（约 44 分 35 秒）。

- 机械切片：估算 244,527 token，253 片，单片最大 1,174 token，0 次硬切，
  精确回拼通过，耗时 0.382 秒。
- 调用：257 次启动，256 次 `llm.completed`，0 次 DRAFT fallback；已知用量
  1,071,141 prompt + 252,694 completion token，不含一次失败调用的 usage。
- Translator：253 片均进入 Agent 完成状态；其中 `seg0210` 在产生 1,635 字符后
  ReadTimeout，部分输出被保留且没有定点重试。
- Editor/QA：分别耗尽 4096/2048 completion token 但没有可解析正文，走 schema
  安全降级。
- Polisher：Translator 拼接初稿约 686,601 字符，章级润色仅导出 17,900 字符，
  约为初稿 2.61%，确认被输出上限截断。
- 最终 QA 为 0.0/rework，并标记 `needs_human_review`。这不是可交付译文。

完整统计位于 `data/exports/long_text_test/TEST_REPORT.md`。该次失败是本轮修复的直接
依据；上述“章级下游、无完整性检查、保留部分残稿”均描述当时版本，不再是当前
工作区行为。

修复后没有直接重跑 21.6 万字全文：新实现会产生 Terminologist 1 次加四阶段各
253 次，即约 1,013 次业务调用；在 checkpoint/resume、并发限流和总费用保护尚未
完成前，重复付费长跑风险过高。替代验证包括 31 项离线测试、253 片 fake LLM 全图
压力测试，以及 2 片真实 DeepSeek 全链路冒烟：共 9 次 LLM 调用全部完成，四阶段
各执行 2 次，8 个逐片事件齐全，2/2 QA 通过，章级 9.15/pass，无片段失败或完整性
告警，耗时 26.4 秒，润色/初稿字符比 1.1846。

当前判断：已消除第一次长跑中确认的“章级截断后仍导出”和“网络残稿被接受”两类
正确性故障；但整本级生产能力仍受顺序调用、无断点续跑、无总费用上限和章级术语
提取输入预算等运维边界限制，不能宣称已经完成同规模真实复验。

## 6. 正在开发或尚未稳定的部分

- 流式 LLM、事件系统、Dashboard、浏览器工作台和统一 pipeline runner 已进入 `main`；机械切片、逐片下游、残稿重试及其新增测试仍在当前未提交工作区。
- 253 片离线全图与 2 片真实接口已验收；同规模真实长文、供应商限流和成本边界仍需在断点续跑与费用保护完成后专项验证。
- macOS 命令行与浏览器 API 已验证；Windows `start_mant.bat` 的真实供应商启动、拖放与浏览器视觉交互仍需在 Windows 环境人工验收。
- 浏览器工作台通过 API 和 JavaScript 语法测试，但当前会话没有可用的浏览器自动化运行时，因此尚缺一次真实点击、拖放、滚动和视觉布局检查。
- 现有架构/路线图文档有少量“骨架阶段”“三个子命令”等旧描述，需要在功能提交稳定后统一校正。
- `config/settings.example.yaml` 与真实本地设置仍有 baseline 章节漂移；本次新增的 `segmentation` 章节已经同时写入示例和本地忽略配置。

## 7. 未完成功能

### 翻译质量与长文本

- Translator/Editor/Polisher/QA 已逐片运行并顺序归并；Terminologist 仍读取整章，且尚无层级术语归并、总 Prompt/调用费用预算、供应商精确 tokenizer、并发限流、断点续跑与跨章节任务队列，因此还不能宣称超长文本生产链路完备。
- Polisher 尚未实现严格的专有名词保护和风格指纹；Terminologist 缺 few-shot、缓存和置信度校准。
- 流中断或 `finish_reason=length` 会完整重试并拒绝残稿；但语法完整、语义却不满足 schema 的结构化响应仍只有各 Agent 的解析降级，没有通用 JSON 修复/再询问机制。
- QA 及格阈值仍有硬编码，返工意见没有“已解决/未解决”生命周期，循环只固定返回 Translator，不能按问题类型做局部编辑。

### 记忆与检索

- 章节完成后还没有把高质量译文自动回写 TM，也没有从章节生成角色、地点、事件与摘要并更新 StoryBible。
- 工作流中的 `prev_summary` 尚未真正从历史章节加载。
- TM 仍使用 SQLite 全表扫描 + `difflib`；VectorStore 默认是哈希嵌入。真实 embedding、FAISS 持久化和可扩展检索尚未完成。
- Glossary 尚无 PostgreSQL 后端。

### 数据与评估

- vecalign/LASER 适配器仍未实现；目前只有确定性 DP 对齐，对章节切分和复杂对话标点的鲁棒性有限。
- WebNovelCollector 有意保持未实现，必须先解决数据授权、robots 和法律合规。
- `mant.eval`、COMET、LLM-as-judge、MQM 人评、消融实验、成本/时延看板均尚未落地。

### 运行与产品化

- 没有多章节批处理、持久化任务队列、checkpoint/resume、浏览器取消任务和崩溃恢复。
- 浏览器任务表位于内存，重启后 job 状态丢失；trace 文件仍在，但没有完整的运行历史 UI。
- 当前只允许一个并发任务；没有用户认证、TLS、租户隔离或部署方案，因此只能作为本机工具使用。
- 只在供应商返回 stream usage 且配置启用时才能得到 token 使用；尚无可靠的价格表和金额计算。
- `python-dotenv` 已声明为依赖，但 CLI 配置加载尚未显式调用 `load_dotenv()`；当前可靠方式仍是系统/进程环境变量。
- `start_mant.bat` 当前针对 `AW4W_API_KEY` 做用户环境刷新，若改用其他 `api_key_env`，批处理不会自动跟随配置。

## 8. 当前已知问题与风险

1. **真实集成验收范围有限**：两片真实付费链路和 253 片离线全图已经跑通，但修复后尚未重跑 253 片真实长文，也未覆盖 QA 实际返工、供应商限流/中断和 Windows 一键启动。
2. **成本与上下文风险**：四个正文 Agent 已逐片受控，Terminologist 仍是章级输入；系统没有供应商精确 tokenizer、全调用预算或费用上限，253 片一轮约产生 1,013 次正文阶段调用，返工还会继续放大费用。
3. **记忆闭环不完整**：能读术语/TM/故事设定，但章节成功后不会自动形成下一章可用的新记忆。
4. **重试策略仍需细化**：残稿/截断已经安全重试并拒收，但外层重试尚未按 HTTP 状态细分，也没有 jitter、熔断或供应商限流退避策略。
5. **本地服务边界**：Dashboard 只适合 `127.0.0.1` 本机使用；若直接暴露到局域网/公网，会缺少认证和安全防护。
6. **进度与设计文档漂移**：部分章节仍保留“骨架阶段”“三个子命令”和旧分支状态，不能作为当前实现的唯一事实来源。
7. **配置示例漂移**：示例、本地设置、BAT 环境变量刷新逻辑不是完全由一个 schema 驱动。
8. **行尾提示**：Windows 下 `git diff --check` 会输出 LF 将转 CRLF 的提示；目前没有实际 whitespace error，不应因此全仓改行尾。

## 9. 重要设计决策及原因

| 决策 | 原因 |
|---|---|
| 六角色分工，但用统一 BaseAgent 契约 | 让提示词和职责可独立演进，同时统一执行、重试、观察和测试边界 |
| 初始切片使用确定性规则而非 LLM | 零额外调用成本、可复现且可精确回拼；结构边界和硬切降级都能测试与审计 |
| 完整原文与片段元数据同时进入 state | 保留旧 `segments: list[str]` 兼容性，同时让章级 Agent 无需有损重拼，译者可安全获得不重复输出的相邻上下文 |
| 四个正文 Agent 共用同一片段序列 | 把所有模型输入/输出限制在片段预算内；按 ordinal 确定性归并，并能把失败和 QA 精确定位到单片 |
| 截断或流中断后丢弃残稿并完整重试 | 部分译文没有可靠的自动续接边界；拒绝残稿比把缺失内容悄悄导出更安全 |
| 润色稿做片段长度比例检查 | 快速拦截最常见的截断和异常膨胀；越界时只回退对应初稿，并迫使 QA/人工复核 |
| 用 LangGraph 表达 QA 回环 | 返工是显式状态机而非隐藏递归，可限制次数、保存状态并测试路由 |
| 返工意见优先于普通翻译要求 | 若反馈埋在上下文末尾，模型容易忽略；最高优先级能让下一轮真正修复问题 |
| 运行检索结果进入 TranslationState | 避免共享闭包在并发图执行时出现可变状态冲突，也便于 trace 与复现 |
| M1 先用确定性 DP 对齐并保留离线回退 | 当前里程碑优先可复现、无需 GPU/外网；之后可用 vecalign/LASER 替换适配层 |
| 术语与 TM 共用 SQLite | 降低本地部署复杂度，并保证 M1 产物能立即被 MemoryHub 使用 |
| `stream_complete()` 为唯一供应商流入口 | 非流式调用只收集同一流，避免两套 API 行为、重试和事件逻辑漂移 |
| token 走事件、不进工作流状态 | 防止状态体积按 token 增长，也不让高频更新干扰 LangGraph 合并语义 |
| 观察 sink best-effort 且脱敏 | 日志系统不能让翻译失败；同时避免密钥、完整 prompt 和 token 正文进入持久库 |
| Dashboard 使用标准库 + SSE | 保持一键本地运行，避免 Node/前端构建依赖；SSE 足以覆盖服务端单向实时事件 |
| 浏览器先限制单任务 | 在没有队列、限流和成本隔离前，避免多个昂贵多 Agent 链路并发失控 |
| DRAFT 只作为离线开发兜底 | 让测试和 UI 可离线联调，但必须显式区分，不能把假结果当真实供应商成功 |
| 配置由组合层读取后注入 | 降低全局状态和模块耦合，使单元测试可用内存配置和 fake client |

## 10. 最近修改的文件

以下是当前工作区相对 `main` 的业务变更清单；`.agents/PROGRESS.md` 还包含前一次
真实供应商与仓库状态核验留下的未提交更新。

### 已跟踪且修改

- 文档与配置：`.agents/AGENTS.md`、`.agents/PROGRESS.md`、`README.md`、`config/settings.example.yaml`、`docs/agent-design.md`、`docs/architecture.md`、`docs/observability.md`。
- 包、Agent 与工作流：`src/mant/__init__.py`、`src/mant/agents/editor.py`、`src/mant/agents/orchestrator.py`、`src/mant/agents/translator.py`、`src/mant/llm/client.py`、`src/mant/workflow/graph.py`、`src/mant/workflow/state.py`。
- CLI 与测试：`src/mant/cli.py`、`tests/test_observability.py`、`tests/test_translator_feedback.py`、`tests/test_workflow.py`。
- 被 Git 忽略的 `config/settings.yaml` 也已加入 `segmentation`、逐片完整性阈值和 `partial_retries` 默认值；未写入密钥值。

### 新增且尚未跟踪

- `src/mant/segmentation.py`：确定性切片实现。
- `tests/test_segmentation.py`：可逆性、结构边界、预算、硬切、确定性和无 LLM 测试。
- `docs/segmentation.md`：完整设计与配置说明。

## 11. 下一步最合理的开发顺序

1. **补齐长文本运行控制**：四个正文 Agent 已逐片；下一步把 Terminologist 改为分片/层级术语归并，增加 checkpoint/resume、有界并发、供应商精确 token 预检和总调用/费用保护，再做授权长章节真实复验。
2. **完成 Windows/浏览器人工验收**：在 Windows 运行 `start_mant.bat --check`，实际点击、拖放、滚动并核对视觉布局、密钥继承和子进程回收。
3. **校正文档与配置漂移**：统一 README、架构/路线图、配置示例和 BAT 的环境变量约定，删除过期的骨架/分支状态描述。
4. **闭合章节记忆**：生成章节摘要、实体/事件更新，成功后把审定译文回写 TM，并让下一章真实加载 `prev_summary`。
5. **加固 QA 返工**：把阈值配置化，跟踪反馈是否解决，按术语/内容/文风问题路由到合适 Agent，增加最终残缺 JSON 的修复策略。
6. **完善运行控制**：浏览器取消、任务持久化、checkpoint/resume、历史 trace 查看；确认资源与成本模型后再考虑有限并发。
7. **建立评估闭环**：实现 `mant.eval`、固定测试集、baseline 与多 Agent 对照、COMET/LLM judge/MQM、时延和成本统计，然后用数据决定哪些 Agent/记忆策略值得保留。
8. **升级对齐与检索**：在可复现基线上引入 vecalign/LASER、真实 embedding 和 FAISS 持久化；用评估集验证收益，不只替换实现。
9. **最后处理采集与部署**：只有在授权明确后实现 collector；只有在认证、TLS、隔离和队列就绪后才把 Dashboard 暴露到本机以外。

## 12. 下一位 AI 的快速接手步骤

```powershell
git status --short --branch
git log -5 --oneline --decorate
git diff --stat
python -m pytest -q
python scripts/verify_m1_output.py --aligned-dir data/aligned --glossary-db data/memory/mant.db --terminology data/raw/demo_work/terminology.md --min-chapters 3
cmd /c start_mant.bat --check
```

然后按以下顺序阅读：

1. `.agents/AGENTS.md`（长期规则）
2. `.agents/PROGRESS.md`（当前工作区事实）
3. `README.md`（用户入口）
4. `src/mant/cli.py`（所有正式入口）
5. `src/mant/workflow/graph.py` 与 `state.py`（主翻译链）
6. `src/mant/llm/client.py`（供应商和流式边界）
7. `src/mant/observability/` 与 `dashboard.py`（监控/浏览器）
8. `tests/`（当前行为契约）

本地运行前确认 `config/settings.yaml` 存在，且配置指定的环境变量在**当前进程**可见。不要打印变量值。双击 `start_mant.bat` 可打开本地浏览器工作台；也可把 UTF-8 `.txt` 拖到该批处理文件上直接翻译。
