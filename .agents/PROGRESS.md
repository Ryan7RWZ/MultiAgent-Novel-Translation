# MANT 项目交接与当前进度

> 快照日期：2026-07-18（Asia/Shanghai）
>
> 分支：`agent/observable-streaming-workbench`，目标分支为 `main`
>
> 状态：核心 M1、章节多 Agent 链路、流式事件、实时监控和浏览器输入工作台已经形成，并随当前功能分支提交；合入状态以 GitHub PR 为准。

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
├─ start_mant.bat               # Windows 一键启动/拖放入口（当前未提交）
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
│  └─ observability.md          # 实时监控设计（当前未提交）
├─ scripts/
│  ├─ run_m1_pipeline.py        # M1 兼容脚本入口
│  └─ verify_m1_output.py       # M1 八项验证
├─ src/mant/
│  ├─ agents/                   # BaseAgent 与六个角色
│  ├─ baseline/                 # 单模型基线翻译
│  ├─ llm/                      # LLMClient、重试和流式输出
│  ├─ memory/                   # Glossary、StoryBible、TM、VectorStore、MemoryHub
│  ├─ observability/            # 事件、sink、运行上下文、SSE dashboard（当前未提交目录）
│  ├─ pipeline/                 # 采集、清洗、对齐、术语提取和统一 runner
│  ├─ workflow/                 # TranslationState 与 LangGraph 章节工作流
│  └─ cli.py                    # m1-pipeline/baseline/translate-chapter/monitor
└─ tests/                       # 20 个离线测试，覆盖记忆、流程、返工、事件和质量规则
```

`data/` 中的运行内容默认被忽略；目录中现有 M1 演示产物是本地验证用事实，不应误认为待提交的数据集。

## 4. Git 与最近提交

最近提交（由新到旧）：

1. `6dcf2bc`（2026-07-17）— 修复 baseline prompt 预览，让首个注入片段可见。
2. `3cacd79`（2026-07-17）— 完成 M1 verifier、离线回退与相关修复。
3. `cb202eb`（2026-07-17）— 初始 MANT 工程骨架。
4. `16e456e` — 仓库初始提交。

重要提示：下面第 5～9 节描述的许多能力位于 `agent/observable-streaming-workbench`，尚未进入目标分支 `main`，不能只看 `origin/main` 判断项目能力。本次发布把先前 25 个已跟踪修改、13 个未跟踪业务/测试文件以及 `.agents/` 中的两份交接文档整理为一个功能提交；该分支还包含此前领先 `origin/main` 的 3 个本地提交。

## 5. 已完成功能

### 5.1 工程与配置

- 已建立 Python 包、CLI、配置示例、文档、数据目录说明和测试骨架。
- 已创建真实 `config/settings.yaml`，并通过 `.gitignore` 排除；配置支持 fast/strong 模型层、内存、流程和可观测性参数。
- 当前本机配置通过 `AW4W_API_KEY` 环境变量取密钥；文档和日志未保存实际值。
- CLI 已有四个正式子命令：`m1-pipeline`、`baseline`、`translate-chapter`、`monitor`。

### 5.2 M1 数据与记忆

- 已实现清洗、章节切分、确定性动态规划句对齐、术语提取/合并和统一 pipeline runner。
- 人工 `terminology.md` 被视为高置信术语；离线自动提取为空时会剔除空译名，避免污染词库。
- 术语与 TM 可写入同一个 SQLite 数据库；M1 对齐结果会同步到 `tm_pairs`，可由 `MemoryHub` 直接检索。
- `verify_m1_output.py` 当前检查 JSONL schema、句对/章节数、terms、TM 同步、非空译名率、术语命中率和双语锚点一致性，共 8 项。
- 当前本地演示产物：38 个句对、4 章、16 条非空术语、38 条 TM；人工术语命中 11/11，锚点 69/69。

### 5.3 多 Agent 翻译链路

- 六种角色已具备统一 Agent 执行边界：Orchestrator、Terminologist、Translator、Editor、Polisher、QA。
- Translator 使用 strong tier，并在返工时把 QA 的 `review_notes` 作为最高优先级要求。
- QA 的解析失败回退会提供可执行建议，而不是只有空泛的低分结果。
- LangGraph 已实现 QA pass/rework 分支和有限返工；默认最大返工次数为 2。
- 检索到的术语、故事设定和 TM 显式进入 `TranslationState`，避免把每次运行数据藏进共享闭包。
- 章节运行结果会导出译文和 JSON 元数据。

### 5.4 流式 LLM 与实时监控

- `LLMClient` 已使用 Chat Completions `stream=True`；`complete()` 通过收集 `stream_complete()` 保持兼容。
- 已处理流式重试边界：首 token 前可重试，产生部分文本后不从头拼接重试；SDK 内部自动重试关闭，避免双重重试。
- 已实现 `RunEvent`、运行上下文和 EventBus，以及终端、JSONL、SQLite sink。
- 高频 token 可批量写入 JSONL；SQLite 不保存 token 正文；sink 失败不会中断主流程。
- 已实现本地 SSE dashboard，可实时显示运行、Agent 状态、LLM token、重试、QA 和结果事件。

### 5.5 浏览器输入与一键启动

- 浏览器页面支持直接粘贴文本或拖入 UTF-8 `.txt` 文件。
- 页面支持作品 ID、章节 ID、最大返工次数，显示六个 Agent 状态、流式文本和最终译文。
- 服务端提供 `POST /api/translate` 和 `GET /api/jobs/{id}`，校验 ID、文件类型和输入长度；任务通过正式 CLI 子进程执行。
- 当前服务限制同一时间一个任务，关闭监控服务时会回收子进程。
- `start_mant.bat` 支持双击打开浏览器、拖放 TXT 直接翻译以及 `--check`；优先项目虚拟环境，并在启动时从 Windows 用户环境刷新 `AW4W_API_KEY`（不打印值）。

### 5.6 已有验证

2026-07-18 本次交接实际执行：

```text
python -m pytest -q                 → 20 passed
python -m unittest discover -q     → Ran 20 tests, OK
python -m compileall -q ...        → 通过
git diff --check                   → 通过，仅有 LF/CRLF 提示
verify_m1_output.py                → 8 PASS / 0 FAIL
start_mant.bat --check             → CONFIG / IMPORT / PYTHON 全部 ok
```

此前已用 fake/DRAFT 链路完成浏览器 API 端到端验证：结果非空，记录到 46 个事件、全部 6 个 Agent、5 个 `llm.token` 事件，以及 run start/end。此前也曾用真实供应商完成一次短文本非流式/基础链路 smoke test（5 次真实 Agent 调用，QA 9 分 pass），但**当前这批流式与浏览器整合改动尚未在本次环境中完成真实供应商端到端验收**。

## 6. 正在开发或尚未稳定的部分

- 当前流式 LLM、事件系统、Dashboard、浏览器工作台、一键启动、统一 pipeline runner 及新增测试已纳入本功能分支，但在 PR 合并前仍未进入 `main`，需要重点审查集成边界和真实供应商验收缺口。
- 真实供应商 + 流式 token + 多 Agent + 浏览器的组合链路仍需在用户正常网络环境做一次小规模验收。当前受控运行环境访问供应商时曾出现 `APIConnectionError`，不能据此判定用户机器配置错误。
- 浏览器工作台通过 API 和 JavaScript 语法测试，但当前会话没有可用的浏览器自动化运行时，因此尚缺一次真实点击、拖放、滚动和视觉布局检查。
- 现有架构/路线图文档有少量“骨架阶段”“三个子命令”等旧描述，需要在功能提交稳定后统一校正。
- `config/settings.example.yaml` 与真实本地设置的章节不完全一致：本地已有 baseline 配置，而示例仍需补齐。此项本次遵守“只改交接文档”要求，没有修改。

## 7. 未完成功能

### 翻译质量与长文本

- 没有面向超长章节的 token 预算、分块、上下文压缩与跨块合并策略；浏览器虽限制字符数，但不等于模型上下文安全。
- Polisher 尚未实现严格的专有名词保护和风格指纹；Terminologist 缺 few-shot、缓存和置信度校准。
- 流式结构化响应若最终是残缺 JSON，目前没有通用修复/再询问机制。
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

1. **真实集成验收缺口**：流式、多 Agent、监控和浏览器已分别通过离线测试，但组合后的真实付费链路没有在本次受控环境跑通；网络错误与应用错误需要在用户环境区分。
2. **成本与上下文风险**：直接粘贴大文本可能触发大 prompt、超上下文或多轮返工费用；现在只有字符上限和单任务限制，没有精确 token 预算。
3. **记忆闭环不完整**：能读术语/TM/故事设定，但章节成功后不会自动形成下一章可用的新记忆。
4. **重试策略粗糙**：外层重试尚未按 HTTP 状态/异常类型分类，也没有 jitter、熔断或供应商限流退避策略。
5. **本地服务边界**：Dashboard 只适合 `127.0.0.1` 本机使用；若直接暴露到局域网/公网，会缺少认证和安全防护。
6. **工作区较大且未提交**：多功能混合在同一 diff；继续开发前应先审查并按功能拆分提交，降低回归和丢失风险。
7. **配置示例漂移**：示例、本地设置、BAT 环境变量刷新逻辑不是完全由一个 schema 驱动。
8. **行尾提示**：Windows 下 `git diff --check` 会输出 LF 将转 CRLF 的提示；目前没有实际 whitespace error，不应因此全仓改行尾。

## 9. 重要设计决策及原因

| 决策 | 原因 |
|---|---|
| 六角色分工，但用统一 BaseAgent 契约 | 让提示词和职责可独立演进，同时统一执行、重试、观察和测试边界 |
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

以下是写交接文档前工作区的业务变更清单。

### 已跟踪且修改

- 文档与配置：`README.md`、`config/settings.example.yaml`、`data/README.md`、`docs/agent-design.md`、`docs/architecture.md`、`docs/evaluation-plan.md`、`docs/roadmap.md`。
- M1 脚本与管道：`scripts/run_m1_pipeline.py`、`scripts/verify_m1_output.py`、`src/mant/pipeline/__init__.py`、`src/mant/pipeline/align.py`、`src/mant/pipeline/extract_terms.py`。
- Agent 与工作流：`src/mant/agents/base.py`、`src/mant/agents/qa.py`、`src/mant/agents/terminologist.py`、`src/mant/agents/translator.py`、`src/mant/workflow/graph.py`、`src/mant/workflow/state.py`。
- LLM/CLI/baseline：`src/mant/llm/client.py`、`src/mant/cli.py`、`src/mant/baseline/translate.py`。
- Memory：`src/mant/memory/__init__.py`、`src/mant/memory/glossary.py`、`src/mant/memory/tm.py`。
- 测试：`tests/test_glossary.py`。

### 新增且尚未跟踪

- 观察与浏览器：`docs/observability.md`、`src/mant/observability/__init__.py`、`dashboard.py`、`events.py`、`factory.py`、`runtime.py`、`sinks.py`。
- 统一 M1 runner：`src/mant/pipeline/runner.py`。
- Windows 入口：`start_mant.bat`。
- 新测试：`tests/test_observability.py`、`tests/test_pipeline_quality.py`、`tests/test_translator_feedback.py`、`tests/test_workflow.py`。

### 本次交接新增（仅文档）

- `.agents/AGENTS.md`：长期、跨会话的仓库规则。
- `.agents/PROGRESS.md`：当前事实、问题和后续顺序。

## 11. 下一步最合理的开发顺序

1. **先审查并合入当前 PR**：确认没有密钥或运行数据，保持 20 个测试、M1 8 项验证和 BAT 自检通过；审查 M1、工作流、可观测性与浏览器之间的集成边界后再合入 `main`。
2. **做一次最小真实集成验收**：在用户正常网络环境用 1～2 句非敏感文本，从浏览器启动，确认六 Agent 事件、实时 token、QA、最终导出均来自真实供应商且没有 DRAFT 回退。记录故障分类，不记录 prompt 或密钥。
3. **补长文本安全边界**：实现 token 估算、章节分块、上下文预算、TM/术语条数上限和总调用成本保护；随后再开放较大 TXT。
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
