# Agent 实时监测与 LLM 流式输出

## 1. 使用方式

Windows 一键启动：双击根目录的 `start_mant.bat` 即可打开浏览器翻译工作台。
在页面中可以直接粘贴中文，或把 UTF-8 TXT 拖进输入框，然后点击“开始翻译”。
页面同时展示六个 Agent、LLM 流式输出、QA 路由和最终译文。

仍可把章节文件拖到 BAT 上，或通过命令行指定作品和章节：

```bat
start_mant.bat "data\raw\demo_work\src\0004.txt" demo_work 0004
```

终端 A 启动独立监控进程：

```bash
mant monitor --config config/settings.yaml
```

浏览器打开 `http://127.0.0.1:8765` 后即可直接输入并启动翻译。也可以继续在
终端 B 启动翻译：

```bash
mant translate-chapter --config config/settings.yaml \
  --work-id demo --chapter-id 0001 --input chapter.txt \
  --stream --verbose
```

工作台显示六个 Agent 的 waiting/running/completed/failed 状态、耗时、模型档位、
segment、LLM 原始增量、节点时间线、QA 分数和返工路由。两个进程通过
`data/traces/*.jsonl` 解耦，不要求翻译进程内嵌 Web 服务。

浏览器通过 `POST /api/translate` 提交文本，后端把输入写入已忽略的
`data/inputs/`，再启动正式 `mant translate-chapter` 子进程。任务状态由
`GET /api/jobs/<job_id>` 查询，完成后页面显示 `data/exports/web/` 中的成品。
输入、路径片段、字符上限和 `max_rework` 均在服务端再次校验；同一时间只接受
一个运行任务，避免误点导致并发计费。

## 2. 工作台界面

工作台是零前端构建链的单页应用（Python 标准库 HTTP 服务 + 内嵌
HTML/CSS/JS），整体采用浅色简约设计：浅灰白背景 `#f7f8fa`、纯白面板、
细边框，强调色为蓝色 `#2563eb`。

**布局**

- 顶部为白色 sticky header：标题、SSE 连接状态、运行状态与运行选择器；
- 宽屏下为双栏：左主列自上而下依次是输入区、运行概览、Agent 状态
  （六张卡片）、LLM 流式输出与最终译文；右侧为事件时间线，最新事件
  在最上方；
- 窄屏（窗口宽度 <900px）时自动折叠为单栏，事件时间线移到主列下方。

**Agent 状态徽章**

六张卡片对应调度 / 术语 / 翻译 / 审校 / 润色 / QA 终审。卡片用状态点
徽章标示 waiting（灰）/ running（蓝色脉冲）/ completed（绿）/ failed
（红），并显示模型档位、耗时与 segment；点击卡片可切换下方 LLM 流式
输出的来源 Agent。

**使用流程**

1. 粘贴中文原文，或把 UTF-8 TXT 拖进输入区：载入后字符计数实时更新，
   章节 ID 自动填充为文件名（可再修改）；作品 ID 默认 `demo_work`，
   最大返工默认 2 次；
2. 点击“开始翻译”，任务期间按钮禁用（同一时间只执行一个任务）；
3. 实时观察 Agent 状态点、LLM 流式增量、运行概览中的 QA 判定 / 分数与
   返工次数，以及右侧事件时间线；
4. 完成后“最终译文”面板显示译文全文、输出路径与 QA 结果；header 的
   运行选择器可回看历史运行。

界面的人工验收清单见 [ui-acceptance.md](ui-acceptance.md)。

## 3. 事件链路

```text
LangGraph / BaseAgent / LLMClient
              │ emit_event（ContextVar 自动补 run/agent/node）
              ▼
          EventBus
          ├── TerminalSink  ── 实时 token 与状态
          ├── JsonlSink     ── 原始可回放事件（token 合批）
          └── SqliteSink    ── 运行摘要与可查询事件（不存 token）
                                  │ 文件增量追踪
                                  ▼
                           TraceBroker → SSE → 浏览器
```

主要事件类型：

| 范围 | 事件 |
| --- | --- |
| 运行 | `run.started` / `run.completed` / `run.failed` |
| LangGraph | `node.started` / `node.completed` / `node.failed` / `workflow.route` |
| Agent | `agent.started` / `agent.completed` / `agent.failed` |
| LLM | `llm.started` / `llm.token` / `llm.retry` / `llm.completed` / `llm.failed` / `llm.fallback` |
| 记忆 | `memory.retrieved` |
| 分片流水线 | `segmentation.completed` / `segmentation.hard_split` / `stage.segment_completed` / `output.integrity_failed` / `qa.aggregated` |

每条事件都含 `run_id`、递增 `sequence`、UTC 时间、work/chapter、node、agent、
segment、round、tier、payload 和 metrics。`run_id` 可用 CLI `--run-id` 指定，
否则自动按时间与随机后缀生成。

## 4. 流式语义与重试

`LLMClient.stream_complete()` 使用 OpenAI 兼容协议的 `stream=True`，每收到一个
delta 就 yield 并发出 `llm.token`。`complete()` 通过收集该迭代器保持原接口。

- 普通文本 Agent：用户立即看到逐增量文本，完成后写入 state。
- JSON Agent：逐增量只用于展示；必须等流结束后才统一解析 JSON，半截 JSON
  不进入业务状态。
- 首 token 之前失败：按 provider 的 `max_retries` 指数退避，并发出 retry 事件。
- `stream_complete()` 已输出部分文本后失败：停止当前流，不在同一迭代器内续接，
  避免“半截文本 + 重试全文”进入同一业务结果。
- 普通 Agent 使用的 `complete()` 会识别流中断和 `finish_reason=length`，丢弃
  残稿并从头完整重试（默认 1 次）；重试仍不完整则返回空结果，由工作流按片
  回退并记录 `segment_failures`。
- SDK 内部重试关闭，只有项目这一层重试，确保 attempt 和耗时可解释。

## 5. 配置

```yaml
observability:
  enabled: true
  trace_dir: data/traces
  sqlite_path: data/traces/runs.db
  terminal:
    enabled: true
    stream_tokens: false
    verbose: false
  trace:
    enabled: true
    sqlite_enabled: true
    token_batch_chars: 80
  dashboard:
    host: 127.0.0.1
    port: 8765
    max_input_chars: 200000
```

CLI `--stream`、`--verbose`、`--trace/--no-trace` 会覆盖对应的单次运行行为。
`llm.providers.<tier>.stream_include_usage: true` 可请求兼容 provider 在流末返回
精确 token usage；不兼容该参数的 provider 应保持默认 `false`。
`llm.providers.<tier>.partial_retries` 控制非流式 Agent 对残稿的完整重试次数，
默认 1。

## 6. 安全与容量

- 监控默认只绑定本机；若改为 `0.0.0.0`，应由反向代理补认证和 TLS。
- 事件仅记录 Prompt 字符数，不记录 system/user Prompt 正文。
- `api_key`、authorization、password、secret、access/refresh token 等字段在
  JSONL/SQLite 写入前递归替换为 `[REDACTED]`。
- token 文本会写 JSONL，便于实时展示和回放；它可能包含原文/译文内容，
  `data/` 已 gitignore，但仍应按作品授权和本机文件权限管理。
- JSONL token 默认每 80 字符合批；SQLite 不保存 token，仅保存调用完成时的
  字符数、耗时和可用 usage，防止数据库被高频 delta 放大。
