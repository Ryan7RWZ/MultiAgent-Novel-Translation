# MANT 项目交接与当前进度

> 快照日期：2026-07-19（Asia/Shanghai）
>
> 分支：`agent/normalize-txt-encoding`，基于 `origin/main` 的 `d581597`
>
> 状态：核心 M1、章节多 Agent 链路、流式事件、实时监控、浏览器工作台、并发执行、checkpoint/manifest 与质量闭环已合入 `main`。当前分支已完成所有用户 TXT 入口的编码识别与 UTF-8 归一、配套文档、离线回归和一次完整 DeepSeek 实译，等待 PR 审查。

## 1. 项目目标

MANT（Multi-Agent Novel Translation）用于把中文网络小说翻译为英文。项目不是把一段文本直接交给单一模型，而是让不同角色分别负责术语、初译、编辑、文学润色和质量审查，并用记忆系统维护跨章节一致性。

当前预期主链路为：

```text
中文章节
  → 编排器 Orchestrator
  → 术语专家 Terminologist
  → 翻译专家 Translator
  → 编辑 Editor
  → 定点修订 Translator（revision mode，仅有事实性意见时调用）
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
| 浏览器界面 | Python 标准库 HTTP 服务 + 随包发布的 `dashboard.html`；无需 Node 构建 |
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
│  ├─ observability/            # 事件、sink、运行上下文、SSE 服务与 dashboard.html
│  ├─ pipeline/                 # 采集、清洗、对齐、术语提取和统一 runner
│  ├─ segmentation.py           # 结构优先、token 预算约束的确定性初始切片
│  ├─ textio.py                 # 用户 TXT 编码识别与 UTF-8 工作副本转换
│  ├─ workflow/                 # TranslationState 与 LangGraph 章节工作流
│  └─ cli.py                    # m1-pipeline/baseline/translate-chapter/resume-run/monitor
└─ tests/                       # 71 项 pytest / 44 项 unittest，含多编码 TXT、切片、记忆、流程、返工、事件和质量规则
```

`data/` 中的运行内容默认被忽略；目录中现有 M1 演示产物是本地验证用事实，不应误认为待提交的数据集。

## 4. Git 与最近提交

最近提交（由新到旧）：

1. `d581597`（2026-07-19）— 合并 PR #5：质量闭环加固与浏览器工作台重建。
2. `adb8d56`（2026-07-19）— 修复 Editor/revision/QA 质量闭环并记录真实 20 片复验。
3. `578be8c`（2026-07-18）— 合并 PR #4：并发执行、checkpoint 与 manifest 加固。
4. `4a3cf9c`（2026-07-18）— 完成长文本有界并发与定点恢复。
5. `51f340e`（2026-07-18）— 合并 PR #3：安全的逐片长文本工作流。

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

### 5.5 确定性初始切片

- 新增 `mant.segmentation`，初始切片不调用 LLM；按标题/场景、空行/段落、句子、分句、空白和最终硬切的顺序生成候选边界，并在每个强边界区间做确定性动态规划。
- 单片正文使用机械 token 估算并受 `max_core_tokens` 硬约束；相邻上/下文有独立预算，且不能跨标题/场景强边界。
- 线上翻译不再复用 M1 的 `clean_text`，避免删除小说中的重复行；只统一换行、去 BOM 和控制字符。片段按序拼接必须精确还原规范化原文。
- `TranslationState` 新增 `source_text`、`segment_meta`、`segmentation_stats`；章级 Agent 使用无损原文，Translator 只翻译核心片，相邻上下文明确标为不可输出。
- CLI 已读取 `segmentation.*`，导出切片统计；事件层新增 `segmentation.completed` 和 `segmentation.hard_split`。
- 下游 Editor、Polisher、QA 已改为逐片执行和确定性顺序归并；润色稿相对初稿的异常缩短/膨胀会触发片段级回退，阈值由 `workflow.min_polished_segment_ratio` / `max_polished_segment_ratio` 配置。

### 5.6 浏览器输入与一键启动

- 浏览器页面支持直接粘贴文本或拖入常见编码的 `.txt` 文件；
  上传时保留原始字节，服务端识别编码并返回 UTF-8 预览。
- 页面支持作品 ID、章节 ID、最大返工次数，显示六个 Agent 状态、流式文本和最终译文。
- 服务端提供 `POST /api/decode-text`、`POST /api/translate` 和
  `GET /api/jobs/{id}`，校验 ID、文件类型、原始字节数和输入长度；任务通过正式
  CLI 子进程执行。
- 当前服务限制同一时间一个任务，关闭监控服务时会回收子进程。
- `start_mant.bat` 支持双击打开浏览器、拖放 TXT 直接翻译以及 `--check`；优先项目虚拟环境，并在启动时从 Windows 用户环境刷新 `DEEPSEEK_API_KEY`（不打印值）。

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

片段并发、checkpoint、定点返工和并发监控实现后在 Windows 执行：

```text
python -m pytest -q                       → 44 passed
python -m unittest discover -q           → Ran 34 tests, OK
python -m compileall -q src tests         → 通过
git diff --check                          → 通过，仅有 LF/CRLF 提示
start_mant.bat --check                    → CONFIG / IMPORT / PYTHON 全部 ok
dashboard 内嵌 JavaScript new Function   → 语法通过
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
正确性故障；片段并发、调用次数预算和 checkpoint 已在当前工作区实现，但尚未完成
同规模真实复验，且仍缺精确 RPM/TPM、金额上限和章级术语输入预算。

### 5.9 长文本并发与断点恢复（本分支）

- 新增 `mant.execution`：阶段级有界线程池、全局在途上限、调用预算、失败熔断、
  取消信号、确定性结果归并和执行统计。
- 每个片段任务创建独立 Agent 与真实 `LLMClient`；worker 使用
  `copy_context()` 继承观测上下文，多个并发调用的事件仍准确归属 run/segment/round。
- 新增 SQLite WAL checkpoint；按 `run_id + segment_id + stage + round + input_hash`
  复用成功产物。连接逐操作创建并显式关闭；缓存读写失败只发事件，不中断翻译。
- QA 生成 `rework_segment_indices`，返工轮只重跑失败片段，其他片段的初稿、润色稿
  和 QA 结果保持原位；所有阶段即使乱序完成，也按 `segment_index` 写回。
- Dashboard 现在按 `call_id` 隔离并发 token，可按片段和轮次切换具体调用；Agent 卡片
  汇总正在运行、完成和失败的片段数。
- 本地忽略配置当时启用 4 个全局在途请求、3200 次片段调用上限、20 次全局失败
  熔断和 checkpoint；5.11 已改为分阶段熔断并增加 manifest。
- 新增并发上限、乱序归并、ContextVar、调用预算、checkpoint 命中/故障降级和定点
  返工测试；该阶段 `pytest` 为 44 passed，Dashboard JavaScript 语法检查通过。

### 5.10 完整 TXT 真实 4 并发验收（2026-07-18/19）

用户明确授权把 `data/raw/斗罗大陆外传神界传说.txt` 全文发送给官方 DeepSeek 并
承担费用后，使用 `run-concurrency4-full-20260718-v1`、`max_rework=0` 完成真实
长文本测试。原 GB18030 文件保留不动，运行副本转换为 UTF-8；机械切片仍为 253
片、215,938 字、244,527 估算 token、0 硬切、精确回拼。

- 总耗时 3,183.03 秒（约 53 分钟），执行器峰值并发准确为 4；Translator、
  Editor、Polisher、QA 的有效并行度分别为 3.96、3.95、3.96、3.95。
- Translator 253/253 成功；Editor 252/253；Polisher 253/253 且 0 完整性失败；
  QA 提交 174 片，154 成功、20 失败，累计失败达到阈值后熔断剩余 79 片。
- 片段任务提交 933，成功 912、失败 21、熔断拒绝 79；checkpoint 933 行、0 写入
  错误。实际失败可略高于阈值 20，因为达到阈值时已有任务仍在途并允许完成。
- 共有 975 个逻辑 LLM 调用，916 completed、59 OutputTruncated、42 retry、
  0 fallback、0 个 429；Translator `seg0076` 有一次 ReadTimeout，等待后重试成功。
- Terminologist 两次整章输出均截断并降级；QA 的 2048 输出上限产生 54 次截断尝试，
  是最终失败/熔断的主要原因。trace 已知 usage 共 1,354,924 prompt + 1,366,810
  completion = 2,721,734 token；实际金额以供应商账单为准。
- 初稿 685,778 字符，润色稿 680,680，比例 0.9926；253 个 Polisher checkpoint
  按 segment ID 拼接后与导出文件逐字符一致，0 DRAFT、0 NUL，仅粗检出 4 个 CJK
  字符。确定性归并与产物完整性通过。
- 最终 QA 5.1/rework，129 片 pass、124 片 rework，100 个 segment failure，标记
  `needs_human_review`；该译文完整但不是已审定可发布版本。
- 27,084 条 trace 中有 44 个 sequence 局部倒序点，全部来自不同并发 call 的 token
  合批写入；975 个 call ID 均无跨 Agent/segment 身份串扰。回放端仍应按 sequence
  排序或调整 token flush 策略。

完整数字和建议位于 `data/exports/concurrency4-full/TEST_REPORT.md`；译文、metadata
与 trace 位于同目录/`data/traces/`，均被 Git 忽略。未自动用相同 run ID 继续恢复，
避免在 QA 配置未修正前产生额外费用。

### 5.11 真实验收后的加固（本分支，2026-07-19，该加固阶段未产生 API 调用）

- checkpoint 指纹升级为 v2：除正文/上下文/Prompt/模型外，现覆盖角色 tier、
  temperature、max_tokens、结构化 JSON 与修复策略、端点、timeout、供应商重试和
  残稿重试。密钥值不进入指纹或 manifest；修改 QA 参数只使相关阶段安全失效。
- 新增 `data/runtime/runs/<run_id>.json` 本地 manifest 和 `mant resume-run`。
  当前支持 `--stage qa --failed-only`：校验原文件未变化，从最终状态直接进入 QA，
  复用成功 QA checkpoint，只重跑技术失败或缺失片；上游四个角色不再调用。
- QA 默认改为 768 token 的紧凑 JSON object，最多 3 条短建议；首次 schema 无效时
  最多做一次 384-token JSON 修复。参数可由 `agents.qa.*` 覆盖。
- Editor 默认改为 1536-token JSON object，取代真实长跑时容易产生高额冗长输出的
  4096-token 自由格式边界；可由 `agents.editor.*` 覆盖并纳入 v2 指纹。
- 已按 DeepSeek 2026 官方文档增加角色级 `thinking`，本地官方配置对五个业务角色
  显式 `disabled`，客户端按官方 OpenAI SDK 方式写入 `extra_body.thinking.type`。
  类默认仍为 `None`，其他兼容供应商不会无条件收到 DeepSeek 扩展字段。
- `qa_score` 现在只对真实评估成功片加权；`qa_summary` 单独报告片段/token 覆盖率、
  已评估片通过率和 `CircuitOpen`/`AgentOutputInvalid` 等技术失败分类。
- 失败熔断新增 `max_failures_per_stage`，本地真实配置已取消全局 20 次共享额度，
  改为各阶段独立上限，避免 Editor 失败占用 QA 额度。
- Terminologist 已复用机械片段并发抽取，候选按源术语和置信度确定性去重，再统一
  与术语库仲裁并一次性写入；不再把 21.6 万字整章送入一次术语输出。
- 当前离线验收：`pytest` 54 passed、`unittest` 40 passed、compileall、Dashboard
  JavaScript 语法、`start_mant.bat --check` 和
  `git diff --check` 通过。覆盖 manifest 往返、QA 失败定向恢复、角色参数缓存失效、
  分阶段熔断和术语分片归并。没有再次调用 DeepSeek，也没有新增 API 费用。
- 同 run 恢复会读取既有 JSONL 的最大 `sequence` 并继续编号，避免恢复事件因
  SQLite `(run_id, sequence)` 重复键而被忽略。
- 2026-07-18 的真实运行使用旧指纹且没有 manifest，不能由新 `resume-run` 安全
  定向恢复；需要用新代码启动一个新 run，之后才能验证真实失败恢复。

### 5.12 新代码真实 20 片、4 并发验收（2026-07-19）

用户明确授权把原 TXT 的前 20 个正常大小切片及处理中间文本发送给官方 DeepSeek，
并承担 API 费用。宿主账户 `CASTORICE\\32415` 的用户作用域中存在有效的
`DEEPSEEK_API_KEY`；普通 Codex 命令运行在 `CodexSandboxOffline` 隔离账户下，
不能直接读取宿主 HKCU，真实测试因此在获准联网的宿主子进程中执行，密钥未写入
配置、日志或仓库。

运行使用 `run-concurrency4-20-20260719-v3`、`max_rework=0` 和本地官方
`deepseek-v4-flash` 配置：

- 输入 15,978 字、17,868 估算 token，共 20 片；单片最大 1,126 token，0 次硬切，
  规范化后精确回拼通过。
- 总耗时 143.1 秒；术语、翻译、编辑、润色、QA 分别耗时 30.86、33.69、44.11、
  23.44、10.38 秒；执行器和 LLM 调用峰值并发均准确为 4。
- 100 个片段任务中 99 个成功、1 个失败；102 次 LLM 启动，99 次完成、3 次
  `OutputTruncated`、2 次完整重试、0 次 DRAFT fallback。
- trace 可归集的真实用量（含三次截断响应）为 158,433 prompt + 42,645
  completion = 201,078 token；实际费用以供应商账单为准。
- Terminologist 20/20、Translator 20/20、Editor 19/20、Polisher 20/20、QA 20/20
  完成。最终英文 46,408 字符，0 个 `[DRAFT]`、0 个 CJK 字符、0 个代码围栏。
- QA 覆盖率和 token 覆盖率均为 1.0，代码侧 20/20 片判 pass，加权 8.73；但两个
  管线级失败仍使章级 verdict 为 `rework`，并标记片 0、13 需要复核。未开启返工，
  因此该产物用于验收链路，不视为已审定发布稿。

本次确认两个尚未修复的根因：

1. **Editor 输出契约与预算不匹配**：片 13 有 19 个段落、1,126 估算 token，Editor
   提示要求逐段穷举问题且 `review_notes` 数量无上限，但输出上限固定为 1,536
   token。两次响应分别生成约 17/19 个问题，均精确在 1,536 completion token 以
   `finish_reason=length` 截断；相同提示和预算的一次完整重试不能解决结构性超长，
   残缺 JSON 被正确丢弃，最终该片 Editor 失败。片 1 也曾截断一次，但重试缩短为
   706 token 后成功，说明当前重试只能覆盖偶发冗长，不能覆盖稳定超预算输出。
2. **片 0 的上游漏译被误表现为润色膨胀**：原片 171 字符，包含站点声明、分隔线
   和书名；Translator 只输出 47 字符书名。Editor 正确记录 3 处遗漏，其中声明为
   high；Polisher 随后越过“只改语言”的角色边界，补入声明和分隔线，但保留原书名
   并再次追加书名，得到 476 字符、顺序和重复均异常的候选。长度完整性保护以残缺
   初稿为分母，检测到比例 10.128 超过上限 2.5 后正确拒绝候选，却只能回退到同一
   残缺初稿。更深层原因是缺少真正按 Editor 意见修订事实性遗漏的阶段，Polisher
   与长度保护被迫承担了不适合的纠错职责。

片 0 还暴露 QA 边界风险：模型原始 verdict 为 `rework`，四维分数 6/8/8/7；代码
计算恰好 7.0 且最低项恰好 6.0，按 `>= 7.0` / `>= 6.0` 强制改判 pass，并覆盖模型
裁决。章级失败标记最终阻止了整体 pass，但单片 QA 元数据仍会显示 pass。以上是
该次真实运行时的历史诊断；对应代码修复见 5.13，尚待新的真实供应商小样复验。

### 5.13 Editor/定点修订/QA 质量闭环修复（本分支，2026-07-19）

- Editor 正常输出改为最多 6 条，按 high → medium → low 确定性排序，并对 span /
  suggestion 执行 80 / 160 字符硬限制；截断或 schema 无效时，不再原样重复相同
  契约，而是用最多 3 条、768 token 的紧凑恢复请求。参数均可由
  `agents.editor.*` 配置并进入 checkpoint 指纹。
- 工作流新增 `revise` 阶段，但不增加第七个 Agent：复用 Translator 的 revision
  mode，只为存在漏译、误译、专名或 high 意见的片段创建任务；无事实性意见的片段
  零调用沿用初稿。结果写入 `revised_segments/revised`。
- Editor/QA 批注新增来源、轮次和 `resolution`；Translator 返工或定点修订成功后
  分别标记 `translation_applied` / `revision_applied`，已落实意见不会在后续轮次
  无限重复注入。
- Polisher 只接收非 high 的 `other` 语言类意见，以修订稿为输入；事实性补译不再
  依赖 Polisher，也不会在完整性回退时退回遗漏初稿。
- QA 阈值改为 `agents.qa.pass_score_threshold/min_dimension_score` 可配置；阈值只
  是必要条件，模型明确判 `rework` 或存在未进入修订的 high 事实性意见时不得被
  代码覆盖成 `pass`。
- checkpoint 指纹升级到 v3，覆盖 Editor 紧凑恢复 Prompt、Translator revision
  Prompt 和新增生成参数，旧阶段结果会安全失效而不是跨语义复用。
- 新增 `tests/test_quality_loop.py`，用脱敏 fake 覆盖片 13 式截断恢复/硬裁剪、片 0
  式前置声明漏译后的定点补全、Polisher 职责隔离、QA 7.0/6.0 临界 rework 以及
  未落实 high omission 禁止放行。当前 `pytest` 为 58 passed、`unittest` 为 44
  tests，compileall 与 `git diff --check` 通过；未调用真实供应商。

### 5.14 质量闭环真实 20 片复验（2026-07-19）

用户在知情第三方数据传输风险后，明确同意把
`data/inputs/concurrency4-20/source-first20.txt` 发送给 DeepSeek 并承担 API 费用。
本次使用本地 `deepseek-v4-flash`、全局并发 4、`max_rework=0`，run ID 为
`run-quality-loop-20-20260719-v1`：

- 输入 15,978 字、17,868 估算 token，共 20 片；单片最大 1,126 token，0 次硬切，
  原文回拼通过。总耗时 151.9 秒。
- 执行器提交并完成 119 个任务，0 个调度失败/拒绝/checkpoint 错误，并发峰值 4。
  Terminologist 20、Translator 初译 20、Editor 20、revision 19、Polisher 20、
  QA 20；仅一个没有事实性意见的片段跳过 revision。
- 119 次 LLM 调用全部 `llm.completed`；0 次 `llm.failed`、`llm.retry`、DRAFT
  fallback 或输出截断。trace 汇总 189,931 prompt + 45,959 completion =
  235,890 token；实际费用以供应商账单为准。
- Editor 20/20 成功，旧 run 中片 13 的 1,536-token 结构性截断未再出现，也没有
  触发紧凑恢复；数量/字段硬限制在真实模型上生效。
- revision 18/19 通过完整性保护。片 13 的模型调用正常完成，但只返回 951 字符，
  相对 3,071 字符初稿的比例为 0.3097；代码正确拒绝该局部修订并回退完整初稿，
  将其记录为 `revise` 阶段失败。该片 4 条事实意见保持 pending，没有被误标为已落实。
- 导出译文 49,860 字符、492 行，0 个 `[DRAFT]`、0 个 CJK 字符、0 个代码围栏；
  metadata 完整性统计为 draft 46,739 / polished 49,368 字符，比例 1.0562。
- QA 20/20 技术成功、覆盖率/token 覆盖率均为 1.0；16 片 pass、4 片明确 rework
  （序号 3、5、11、16），加权分 8.57。片 13 的 QA 自身为 pass，但上游 revision
  完整性失败仍把它加入返工集合，因此最终返工序号为 3、5、11、13、16。
- 因本次固定 `max_rework=0`，最终章级 verdict 为 `rework` 并标记人工复核；这证明
  QA 明确 rework 不再被高分覆盖、上游完整性失败也不能被 QA pass 掩盖。

产物位于 `data/exports/quality-loop-20-v1/`，trace 位于
`data/traces/run-quality-loop-20-20260719-v1.jsonl`，manifest 位于
`data/runtime/runs/run-quality-loop-20-20260719-v1.json`；均为 Git 忽略的本地数据。
下一项质量加固应针对 revision“只返回修改片段而非完整译文”的真实行为，采用更
强的完整输出契约或可确定性应用的结构化 patch，再用同一 run 的 checkpoint 做低成本复验。

### 5.15 浏览器翻译制作台重建（已合入 main，2026-07-19）

- 保留 Python 标准库 HTTP、`POST /api/translate`、`GET /api/jobs/<id>`、
  `GET /api/health` 和 SSE `/events` 契约，不引入 Node 构建链或第三方前端依赖。
- 新页面拆到 `src/mant/observability/dashboard.html` 并通过 setuptools package-data
  随包发布；`dashboard.py` 保留旧内嵌页面作为资源缺失时的源码安全回退。
- 首屏重组为原文任务区、总进度、运行指标和六 Agent 协作链路；新增片段/模型调用/
  Token/耗时汇总、QA 待返工状态、异常事件筛选、完整性错误比例展示，以及译文复制/
  下载操作。
- 每个 `call_id` 继续使用独立输出缓冲；高频 `llm.token` 不再进入事件时间线，DOM
  更新通过 `requestAnimationFrame` 合并，避免数万 token 事件逐条重绘。
- `tests/test_observability.py` 已补页面关键控制和降噪/刷新契约断言；HTML 中 38 个 ID
  无重复，JavaScript 可由 Node 解析，观测测试 12/12 通过，`start_mant.bat --check`
  通过。当前 Codex 会话没有可用浏览器实例，尚未完成截图、拖放和响应式视觉验收。

### 5.16 用户 TXT 编码统一（当前分支，2026-07-19）

- 新增 `mant.textio`：按 BOM、严格 UTF-8、chardet 和确定性候选识别用户
  TXT，覆盖 GBK/GB18030、Big5、UTF-16/32、Shift-JIS/CP932、EUC-JP、
  EUC-KR/CP949 及常见 Windows 西文编码。解码全程使用 strict，不以
  replacement character 静默产生乱码；明显二进制输入会被拒绝。
- 原文件保持不变；`convert_text_file_to_utf8` 只写新的 UTF-8 工作副本。
  章节工作流在 `TranslationState.source_encoding`、CLI metadata 和
  `input.decoded` 事件中保留原编码。
- 已接入 `translate-chapter`/BAT 拖放、Baseline、M1 `LocalTxtCollector`
  和浏览器工作台。浏览器不再调用 `File.text()`，而是把 `arrayBuffer()`
  的原始字节发给 `POST /api/decode-text`，避免 GBK/Big5 在到达服务端前就
  被浏览器按 UTF-8 损坏。
- 新增编码参数化回归和工作副本/二进制拒绝测试，并用 GB18030
  运行 Baseline/正式 workflow、用 GB18030 + UTF-16 运行 M1、用 Big5 验证
  浏览器解码边界。`.venv/bin/pytest -q` 为 71 passed，`unittest`
  为 44 tests，`compileall` 和 `git diff --check` 通过；未调用真实 LLM。

### 5.17 DeepSeek V4 完整 TXT 实译（当前分支，2026-07-19）

用户明确授权将 `data/raw/斗罗大陆外传神界传说._副本.txt` 全文发送给
DeepSeek 并承担费用。本次配置 fast=`deepseek-v4-flash`、
strong=`deepseek-v4-pro`、全局并发 4、checkpoint/manifest 开启、
`max_rework=0`：

- 原文自动识别为 GB18030，414,427 bytes / 219,688 字符；机械规范化后
  215,938 字符、244,527 估算 token，生成 253 片。最大 1,174 token，
  0 次硬切，原文回拼通过；源文件保持不变。
- 首次运行在本地 1 小时命令上限处被终止；同 run ID 恢复命中
  978 个成功 checkpoint，只重试 2 个初译失败片、受其影响的 Editor/
  revision 和未完成下游。最终 checkpoint 为 Terminology 253、Translate
  253、Editor 253、Revision 252（1 片无需修订）、Polish 253、QA 253，
  全部成功；有效执行并发峰值 4，0 个 checkpoint 错误。
- 两段 trace 共记录 1,520 次成功 LLM 响应，成功响应 usage 合计
  6,810,905 prompt + 682,232 completion = 7,493,137 token。共有 25 条
  可恢复的 `llm.failed` 技术事件（主要为长流读超时/协议中断），
  残稿均被丢弃；首次命令被终止时另有 4 条在途请求。准确费用以
  DeepSeek 账单为准。主运行 3,599.6 s，恢复 941.5 s。
- UTF-8 译文为 711,256 字符 / 6,903 行；`[DRAFT]`、`[SEGMENT_ERROR]`、
  CJK 残留和代码围栏均为 0。润色/初稿长度比 0.9951，最终
  `segment_failures=[]`。
- QA 技术覆盖率与 token 覆盖率均为 1.0；加权分 8.37，128 片
  pass、125 片 rework。因本次限定 `max_rework=0`，结果正确标记
  `needs_human_review=true`，未继续产生第二轮费用。产物位于
  `data/exports/deepseek-v4-full/`，trace 分别位于 `data/traces/` 与
  `data/traces/recovery-v1/`，manifest 位于 `data/runtime/runs/`。

## 6. 正在开发或尚未稳定的部分

- 流式 LLM、机械切片、逐片下游、并发执行层、checkpoint、定点返工、质量闭环
  和浏览器工作台已进入 `main`；当前分支增加 TXT 编码识别与 UTF-8 归一。
- 253 片完整 GB18030 TXT 已使用 DeepSeek V4 Flash/Pro 跑完 1,520 次成功
  LLM 响应；并发调度、编码归一、确定性归并、跨进程 checkpoint 恢复、
  v3 指纹和 manifest 均由真实 run 验证。最终六阶段无失败且 QA 覆盖 100%，
  但 125/253 片被 QA 判 rework，尚未进行第二轮质量返工。
  `resume-run --stage qa --failed-only` 仍尚未在真实失败 run 上执行。
- macOS 命令行与浏览器 API 已验证；Windows `start_mant.bat` 的真实供应商启动、拖放与浏览器视觉交互仍需在 Windows 环境人工验收。
- 浏览器工作台通过 API 和 JavaScript 语法测试，但当前会话没有可用的浏览器自动化运行时，因此尚缺一次真实点击、拖放、滚动和视觉布局检查。
- 现有架构/路线图文档有少量“骨架阶段”“三个子命令”等旧描述，需要在功能提交稳定后统一校正。
- `config/settings.example.yaml` 与真实本地设置仍有 baseline 章节漂移；本次新增的 `segmentation` 章节已经同时写入示例和本地忽略配置。

## 7. 未完成功能

### 翻译质量与长文本

- Terminologist/Translator/Editor/revise/Polisher/QA 已逐片有界并发并确定性归并，支持
  调用次数预算、阶段失败熔断、v3 checkpoint 和 QA manifest 恢复；仍缺精确
  RPM/TPM、总 Prompt/金额预算、供应商 tokenizer 与跨章节任务队列。
- Polisher 尚未实现严格的专有名词保护和风格指纹；Terminologist 缺 few-shot、缓存和置信度校准。
- 流中断或 `finish_reason=length` 会完整重试并拒绝残稿；QA 有一次 JSON 修复，
  Editor 有紧凑恢复，Terminologist 等其他结构化 Agent 仍只有解析降级。
- QA 阈值已配置化，返工意见有基础的“待落实/已由翻译落实/已由修订落实”状态；
  仍缺 QA 验证后关闭、人工重开以及按更多问题类型选择回环落点的完整生命周期。

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

- 没有多章节批处理、持久化任务队列和浏览器取消/恢复按钮；CLI 已提供
  `resume-run --stage qa --failed-only`，但浏览器尚未暴露该入口。
- 浏览器任务表位于内存，重启后 job 状态丢失；trace 文件仍在，但没有完整的运行历史 UI。
- 当前只允许一个并发任务；没有用户认证、TLS、租户隔离或部署方案，因此只能作为本机工具使用。
- 只在供应商返回 stream usage 且配置启用时才能得到 token 使用；尚无可靠的价格表和金额计算。
- `python-dotenv` 已声明为依赖，但 CLI 配置加载尚未显式调用 `load_dotenv()`；当前可靠方式仍是系统/进程环境变量。
- `start_mant.bat` 与当前 DeepSeek 官方配置统一使用 `DEEPSEEK_API_KEY`；若以后改用其他 `api_key_env`，需同步调整批处理刷新逻辑。

## 8. 当前已知问题与风险

1. **Editor 紧凑恢复分支尚缺真实触发**：真实 20 片中 20/20 正常审校均在首个
   有界响应内完成，证明硬限制解决旧截断；但本次没有触发 768-token 紧凑恢复，
   该异常分支仍只有 fake 回归证据。
2. **成本与上下文风险**：五个角色已有片段调用次数上限，Terminologist 已分片；
   系统仍没有供应商精确 tokenizer、可靠 token/金额上限。已经在途的请求
   不会被调用预算强制中止，LLMClient 内部重试也不单独计数，因此预算是片段任务
   派发上限而不是绝对供应商请求或费用上限。
3. **记忆闭环不完整**：能读术语/TM/故事设定，但章节成功后不会自动形成下一章可用的新记忆。
4. **重试策略仍需细化**：残稿/截断已经安全重试并拒收，片段失败数量可熔断新派发；
   但外层重试尚未按 HTTP 状态细分，也没有共享速率桶或 jitter 退避。
5. **本地服务边界**：Dashboard 只适合 `127.0.0.1` 本机使用；若直接暴露到局域网/公网，会缺少认证和安全防护。
6. **进度与设计文档漂移**：部分章节仍保留“骨架阶段”“三个子命令”和旧分支状态，不能作为当前实现的唯一事实来源。
7. **配置示例漂移**：示例、本地设置、BAT 环境变量刷新逻辑不是完全由一个 schema 驱动。
8. **行尾提示**：Windows 下 `git diff --check` 会输出 LF 将转 CRLF 的提示；目前没有实际 whitespace error，不应因此全仓改行尾。
9. **旧运行无法直接使用新恢复入口**：2026-07-18 的真实 run 没有 manifest 且使用
   v1 指纹；不能在新代码下安全复用。manifest 当前也只在正常返回后写入，中途崩溃
   仍缺增量恢复清单。
10. **并发 token 持久化局部乱序**：本次 27,084 条 trace 有 44 个 sequence 局部
    倒序点，均为不同 call 的 token 合批 flush；call 身份无串扰、业务状态不受影响，
    但严格历史回放应按 sequence 排序。
11. **revision 完整输出契约仍不够强**：真实片 13 调用正常完成，却只返回完整初稿
    的 30.97%；完整性保护正确拒绝了局部内容，但事实意见没有得到落实。下一版应
    使用更强的完整译文契约，或让模型返回可由代码确定性应用的结构化 patch。
12. **QA 现在采取保守放行**：7.0/6.0 临界且模型判 rework 的回归已修复；代价是
    模型偶发过严时会增加返工/人工复核，需要后续评估误拒率而不能直接放宽安全规则。

## 9. 重要设计决策及原因

| 决策 | 原因 |
|---|---|
| 六角色分工，但用统一 BaseAgent 契约 | 让提示词和职责可独立演进，同时统一执行、重试、观察和测试边界 |
| 初始切片使用确定性规则而非 LLM | 零额外调用成本、可复现且可精确回拼；结构边界和硬切降级都能测试与审计 |
| 完整原文与片段元数据同时进入 state | 保留旧 `segments: list[str]` 兼容性，同时让章级 Agent 无需有损重拼，译者可安全获得不重复输出的相邻上下文 |
| 五个业务 Agent 共用同一片段序列 | 把所有模型输入/输出限制在片段预算内；按 ordinal 确定性归并，并能把失败和 QA 精确定位到单片 |
| 并发只发生在同一阶段的不同片段间 | 保持 translate→edit→polish→QA 数据依赖清晰，同时获得主要吞吐收益 |
| 每个 worker 创建独立 Agent/LLMClient | 避免 `last_notes`、调用 ID 和 token 计数等可变状态在线程间串扰 |
| checkpoint 使用 v3 语义指纹 + manifest | 同 run 可定向恢复，生成参数、Prompt 或输入变化时安全失效；manifest 冻结恢复所需上游状态 |
| Editor 后按需进入 Translator revision mode | 让事实性漏译/误译有明确改稿责任；无问题片段零调用，Polisher 只负责语言 |
| QA 只返工失败片段 | 长文本中局部问题不应放大为全章四阶段重跑，显著降低费用与延迟 |
| 截断或流中断后丢弃残稿并完整重试 | 部分译文没有可靠的自动续接边界；拒绝残稿比把缺失内容悄悄导出更安全 |
| 润色稿做片段长度比例检查 | 快速拦截最常见的截断和异常膨胀；越界时只回退对应定点修订稿，并迫使 QA/人工复核 |
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

以下是当前分支相对 `main`（`578be8c`）的变更清单。

### 本分支修改

- 文档与配置：`.agents/PROGRESS.md`、`README.md`、
  `config/settings.example.yaml`、`docs/architecture.md`、`docs/agent-design.md`、
  `docs/concurrency.md`、`docs/observability.md`、`docs/ui-acceptance.md`、
  `pyproject.toml`。
- Agent：`src/mant/agents/editor.py`、`qa.py`、`translator.py`。
- 工作流与执行：`src/mant/workflow/graph.py`、`state.py`、
  `src/mant/execution/models.py`。
- 浏览器工作台：`src/mant/observability/dashboard.py`、
  `tests/test_observability.py`。

### 本分支新增

- `tests/test_quality_loop.py`：Editor 紧凑恢复、定点修订、Polisher 职责隔离和
  QA 保守放行回归测试。
- `src/mant/observability/dashboard.html`：零构建链浏览器翻译制作台页面。

## 11. 下一步最合理的开发顺序

1. **加固 revision 完整输出契约**：针对真实片 13 的 0.3097 长度比例，优先设计
   可确定性应用的结构化 patch 或更强的完整译文返回/恢复协议；补 fake 回归后，
   利用同一 run 的 checkpoint 只重试失败及受影响片段，避免重复支付全部 20 片。
2. **真实验证失败恢复**：用新的小样 run 覆盖 checkpoint 命中和失败阶段恢复；当前
   CLI 只支持 QA failed-only，需先决定是否把安全恢复扩展到 edit/polish。
3. **补精确供应商限流与费用保护**：实现共享 RPM/TPM 速率桶、429 Retry-After +
   jitter、stream usage 聚合、模型价格表和金额上限，再考虑 253 片真实复验。
4. **完成 Windows/浏览器人工视觉验收**：`start_mant.bat --check` 已通过；仍需在
   可用浏览器中实际点击、拖放、缩放、复制/下载并核对布局、密钥继承和子进程回收。
5. **完善运行控制**：增加浏览器取消/恢复按钮、任务表持久化、运行中增量 manifest 和从 trace 重建历史。
6. **闭合章节记忆**：生成章节摘要、实体/事件更新，成功后把审定译文回写 TM，并让下一章真实加载 `prev_summary`。
7. **补全 QA 批注生命周期**：在现有 resolution 基础上增加 QA 验证关闭、人工重开，
   并用实验决定不同问题类型的回环落点。
8. **建立评估闭环**：实现固定测试集、baseline 对照、质量/时延/成本统计。
9. **升级对齐与检索**：引入 vecalign/LASER、真实 embedding 和 FAISS 持久化。
10. **最后处理采集与部署**：授权明确后再实现 collector；认证、TLS、隔离和队列
    就绪后才把 Dashboard 暴露到本机以外。

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
