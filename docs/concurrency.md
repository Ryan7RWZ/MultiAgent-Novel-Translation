# 长文本片段并发执行与断点恢复

## 1. 目标与边界

本执行层解决五个片段阶段（Terminologist、Translator、Editor、Polisher、QA）在长文本中逐片
串行导致的吞吐问题，同时保持以下不变量：

1. 每个任务只处理一个 `segment_id + stage + round`。
2. 模型完成顺序可以任意，写回 `TranslationState` 时必须按 `segment_index` 排序。
3. 一个任务失败只降级该片，不取消已经完成的其他片段。
4. 每个并发任务使用独立 Agent 和独立真实 `LLMClient`，不共享 `last_*` 可变状态。
5. token 只进入事件流；工作流状态只接收完整且已校验的 Agent 结果。
6. QA 返工只重跑不通过或发生完整性错误的片段，其他片段沿用上一轮结果。

Terminologist 复用机械正文片段并发抽取候选，随后按源术语确定性去重（置信度
高者优先），最后统一与术语库仲裁并一次性写入，避免整章 Prompt 和并发写库竞争。

## 2. 执行模型

```text
LangGraph 节点（每个阶段仍保持顺序）
    │
    ├─ 生成 StageTask(run, segment, stage, round, input_hash)
    │
    ├─ checkpoint 命中 ───────────────────────┐
    │                                         │
    └─ 未命中 → 有界 ThreadPoolExecutor       │
                   ├─ 独立 Agent/LLMClient    │
                   ├─ 独立 Agent/LLMClient    │
                   └─ 独立 Agent/LLMClient    │
                         │ 任意顺序完成        │
                         ▼                     │
              按 segment_index 确定性排序 ◀───┘
                         │
                         ▼
              单线程归并 TranslationState
```

LangGraph 的阶段依赖不变：`terminology → translate → edit → polish → qa`。并发发生在同一阶段
的不同片段之间，不会让 Editor 读取尚未完成的 Translator 结果。

`global_max_in_flight` 是单次章节运行内所有阶段的总上限；由于当前阶段不会重叠，
实际 worker 数为 `min(global_max_in_flight, stages.<stage>)`。`enabled: false` 时
所有阶段恒为单 worker，可随时回退到原来的串行语义。

## 3. 配置

```yaml
agents:
  terminologist: {thinking: disabled}
  translator: {thinking: disabled}
  editor: {thinking: disabled, max_tokens: 1536, structured_json: true}
  polisher: {thinking: disabled}
  qa:
    thinking: disabled
    max_tokens: 768
    structured_json: true
    repair_attempts: 1
    repair_max_tokens: 384

concurrency:
  enabled: true
  global_max_in_flight: 4
  stages:
    terminology: 2
    translate: 4
    edit: 4
    polish: 6
    qa: 4
  budget:
    max_segment_calls: 3200  # 0 表示不限；包含 Terminologist
    max_failures: 0          # 可选全局上限；0 表示不限
    max_failures_per_stage:  # 阶段独立熔断，互不挤占
      translate: 8
      edit: 8
      polish: 8
      qa: 20
  checkpoint:
    enabled: true
    sqlite_path: data/runtime/checkpoints.db
  manifest:
    enabled: true
    directory: data/runtime/runs
```

DeepSeek 官方当前要求通过 OpenAI SDK 的
`extra_body={"thinking":{"type":"disabled"}}` 关闭默认开启的思考模式；JSON
Output 使用 `response_format={"type":"json_object"}`。MANT 将二者做成角色级配置，
仅在显式配置 `thinking` 时发送供应商扩展字段，其他 OpenAI 兼容供应商不受影响。
参考 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) 和
[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)。

建议先从 2–4 个在途请求开始，根据供应商的并发限制、429 比例、平均延迟和费用
逐步调整。阶段配置可以高于全局值，但不会突破全局上限。

调用预算按片段 Agent 任务计数，包含 Terminologist，但不展开计算
`LLMClient` 内部的 HTTP/残稿重试。达到 `max_segment_calls` 后，未派发片段以
`BudgetExceeded` 失败结果进入现有片段级降级逻辑；全局或对应阶段达到失败上限后
熔断该范围的新任务。已经在途的请求允许完成，避免供应商侧仍计费但本地丢失结果。

## 4. checkpoint 与恢复

checkpoint 使用 SQLite WAL，每次读写打开独立连接并显式关闭，线程之间不共享
连接。表的逻辑主键为：

```text
(run_id, segment_id, stage, round)
```

结果还必须匹配 `input_hash` 才能复用。版本 2 指纹覆盖 Agent 类与 Prompt 模板、
角色档位、temperature、max_tokens、结构化 JSON/修复策略、供应商模型、端点、
超时/重试参数、原文、上下文、轮次和阶段。密钥值绝不进入指纹或 manifest。
只有 `ok=true` 的结果会被业务恢复，失败结果只供诊断并在恢复时重新执行。

启用 manifest 后，每次完成的运行会把恢复所需状态写入
`data/runtime/runs/<run_id>.json`。仅重试 QA 技术失败或缺失片：

```powershell
mant resume-run --config config/settings.yaml `
  --run-id run-20260718-example --stage qa --failed-only `
  --stream --verbose
```

恢复时会重新读取原文件并核对规范化正文；正文变化会直接拒绝恢复。QA 恢复从
QA 节点进入，不再调用术语、翻译、审校或润色；成功 QA checkpoint 复用，失败/
缺失项重跑。角色配置改变后相关指纹失效，因此会安全重跑受影响的目标阶段结果。
版本 2 之前没有 manifest 且使用旧指纹的运行不能直接定向恢复。

浏览器提交目前每次生成新 `run_id`，因此浏览器尚未提供显式的“按原 run 恢复”
按钮；断点恢复先通过 CLI 使用。

## 5. QA 定点返工

首轮正文四个阶段处理全部片段。QA 汇总以下序号到
`TranslationState.rework_segment_indices`：

- QA 返回 `rework` 或 QA 调用失败的片段；
- Translator、Editor、Polisher 完整性检查记录失败的片段。

进入下一轮后，四个阶段只为这些序号创建任务。未选中的 `draft_segments`、
`polished_segments` 和 `segment_qa` 原位保留，最终仍按原始片段序号拼接。若章级
判定为 rework 但没有有效序号，为安全起见回退为全片返工。

`qa_score` 只对技术上成功完成的 QA 片按源片 token 加权，未执行/解析失败片不再
伪装成 0 分质量样本。`qa_summary` 同时给出 `coverage`、`token_coverage`、
`pass_ratio` 和 `failure_categories`；发布判定仍要求全覆盖、全通过且无上游失败。

## 6. 事件与实时监控

执行层新增事件：

| 事件 | 含义 |
|---|---|
| `task.queued` | 片段任务已占用调用预算并进入执行器 |
| `task.started` | worker 开始调用 Agent |
| `task.completed` / `task.failed` | 任务完成或片段级失败 |
| `checkpoint.hit` / `checkpoint.saved` | 结果复用或持久化成功 |
| `checkpoint.failed` | checkpoint 读写失败，业务继续 |
| `budget.exhausted` | 达到片段调用上限 |
| `circuit.opened` | 达到失败熔断阈值 |
| `manifest.saved` / `manifest.failed` | 恢复清单写入结果 |
| `resume.started` / `resume.source_validated` | 定向恢复及原文校验 |

worker 通过 `contextvars.copy_context()` 继承运行观测上下文，所以并发线程产生的
Agent 和 LLM 事件仍带有正确的 `run_id`、`segment_id`、`round` 和角色。

Dashboard 按 `call_id` 隔离 token，并把 `segment_id + round + status` 列在调用
选择器中。Agent 卡片展示该角色当前运行数、完成数和失败数；多个片段同时流式
输出时不会拼接到同一文本缓冲区。

## 7. 当前限制与后续工作

- worker 数限制的是并发量，不是精确 RPM/TPM；供应商 429 仍由 LLMClient 的
  请求重试处理，后续应增加带 jitter 的共享速率桶。
- `max_segment_calls` 是确定性的调用次数上限，不是金额上限。可靠费用预算需要
  供应商 stream usage、模型价格表和 prompt 预估共同支持。
- 主动取消会阻止新任务，但 Python 线程无法强制终止已经进入供应商请求的调用。
  浏览器当前仍以终止整个 CLI 子进程实现服务关闭时回收。
- checkpoint 只保存 Agent 阶段产物，不保存浏览器内存任务表；服务重启后运行历史
  仍需从 trace 重建。
- manifest 当前只在一次 `run_chapter` 正常返回后写入；进程在首轮中途崩溃时还没有
  可用 manifest，后续应在切片与检索完成后增量保存。
- review notes 已带轮次和片段定位，但“已解决/未解决”的完整生命周期仍待实现。

## 8. 验证要求

每次修改执行层至少验证：

```powershell
python -m pytest tests/test_execution.py tests/test_workflow.py -q
python -m unittest discover -q
python -m compileall -q src tests
git diff --check
```

测试必须覆盖并发上限、乱序完成后的确定性归并、ContextVar 事件归属、预算拒绝、
checkpoint 命中、checkpoint 故障降级，以及多片段只返工失败片段。
