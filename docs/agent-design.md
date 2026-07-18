# 多智能体设计

> 本文档定义六个 Agent（调度 / 术语 / 翻译 / 审校 / 润色 / QA 终审）的职责、输入输出契约、Prompt 设计要点，以及 fast/strong 模型档位建议与成本估算思路。
> 所有契约与团队约定保持一致：`AgentTask` / `AgentResult` / `BaseAgent` 见 `mant.agents.base`，状态字段见 `mant.workflow.state.TranslationState`。

---

## 1. 统一接口回顾（团队约定，勿重复定义）

```python
# mant.agents.base（骨架签名）
@dataclass
class AgentTask:
    work_id: str
    chapter_id: str
    segment_id: str        # 章级任务用 "*" 或 "chapter" 占位
    source_text: str       # 待处理原文（段或整章拼接）
    context: dict          # 注入的上下文：glossary / tm_hits / bible / review_notes 等

@dataclass
class AgentResult:
    agent: str             # Agent 名，如 "translator"
    ok: bool               # 是否成功（异常时 ok=False，notes 记原因）
    output: dict           # 各 Agent 约定的输出键，见第 2 节
    notes: list[str]       # 日志性批注，写入 state.review_notes 或留档

class BaseAgent(ABC):
    def __init__(self, llm: LLMClient, memory: MemoryHub | None = None): ...
    @abstractmethod
    def run(self, task: AgentTask) -> AgentResult: ...
```

`context` 的约定键（各 Agent 按需取用，缺省即空）：

| context 键 | 内容 | 主要消费方 |
| --- | --- | --- |
| `glossary` | `dict[str, str]` 本章术语映射 | 翻译 / 审校 / QA |
| `tm_matches` | `list[dict]`，`search_tm` 结果序列化 | 翻译 |
| `story_bible` | `StoryBible` 摘要字典 | 翻译 / 审校 / QA |
| `review_notes` | `list[str]` 审校+QA 批注 | 翻译（返工轮） |
| `prev_summary` | 上一章/相邻段摘要（衔接用） | 翻译 |
| `round` | 当前返工轮次（0 起） | 全部 |

## 2. 输出契约总表（`AgentResult.output` 键 → `TranslationState` 字段）

> 下表是各模块拼接的唯一事实来源：output 键名不得随意更改，新增键需在团队内同步。

| Agent | `agent` 名 | output 键 | 对应 state 字段 | 模型档 |
| --- | --- | --- | --- | --- |
| 调度 Agent | `orchestrator` | `segments: list[str]`、`segment_meta: list[dict]`、`normalized_text: str`、`segmentation_stats: dict`、`plan: list[dict]`、`dispatch: dict` | `source_text`、`segments`、`segment_meta`、`segmentation_stats` | 纯规则（开放式计划可选 fast） |
| 术语 Agent | `terminologist` | `glossary: dict[str, str]`、`new_terms: list[dict]` | `glossary` | fast |
| 翻译 Agent | `translator` | `draft: str` | `draft` | strong |
| 审校 Agent | `editor` | `review_notes: list[dict]` | `review_notes` | strong |
| 润色 Agent | `polisher` | `polished: str` | `polished` | strong |
| QA 终审 | `qa` | `qa_score: float`、`qa_verdict: str`、`qa_detail: dict` | `qa_score`、`qa_verdict`、建议并入 `review_notes` | strong |

`new_terms` 元素结构对齐 `TermEntry`：`{"source", "target", "category", "work_id", "confidence"}`。
`qa_verdict` 取值仅 `"pass"` / `"rework"`；`qa_score` 为 0–10。

## 3. 各 Agent 详设

### 3.1 调度 Agent（orchestrator）

- **职责**：把整章原文机械切成可逆、受 token 预算约束的段级任务序列；决定串行/并发；驱动 LangGraph 状态机；维护 `rework_count` 与 `max_rework` 兜底。
- **输入**：作品/章节元信息 + 完整章节原文。
- **输出**：`segments`、不重复正文的 `segment_meta`、`normalized_text`、`segmentation_stats`，以及 `plan` 和 `dispatch`。
- **实现策略**：初始切片完全不调用 LLM；按标题/场景/段落/句子/分句优先级和 token 预算做确定性动态规划，最后才硬切。详见 [segmentation.md](./segmentation.md)。状态转移由 LangGraph 条件边表达；仅在需要"下一章优先级排序"等开放式判断时调用 fast 档模型。
- **Prompt 设计要点**：若启用 LLM 计划，要求只输出 JSON 计划，禁止自由文本；输入含当前 `rework_count / max_rework`，输出含明确的 `next_node` 与理由。

### 3.2 术语 Agent（terminologist）

- **职责**：章节翻译前按机械片段并发扫描：① 每片抽取疑似术语；② 按源术语与置信度确定性去重；③ 用 `lookup_terms` 命中已有术语；④ 统一 `record_terms` 一次性入库；产出本章 `glossary`。
- **一致性原则**：同一 `work_id` 内"先入库者为准"；新抽取术语与库内冲突时保留库内译名，仅当 `confidence` 显著更高（如高 0.2 以上）时记为待人工裁决，不自动覆盖。
- **输入**：map 阶段 `AgentTask.source_text` 为单个机械片段；reduce/仲裁阶段使用无损章级原文命中已有术语，`context.bible` 辅助消歧。
- **输出**：`glossary`（本章生效的映射）、`new_terms`（本次新入库条目）。
- **Prompt 设计要点**：
  - 输出严格紧凑 JSON 对象 `{"terms": [...]}`；无新术语返回 `{"terms": []}`；
  - 明示类别枚举 `person / place / skill / item / faction / title / other`；
  - 附 2–3 组 few-shot（网文风格：境界名、法宝名）；
  - 硬约束："禁止臆造原文中不存在的术语；拿不准的译名给低 confidence"。

### 3.3 翻译 Agent（translator）

- **职责**：产出初译 `draft`；返工轮需携带 `review_notes` 批注做约束重译。
- **输入**：`source_text`（当前待译核心片段），`context.glossary / tm_matches / story_bible / prev_summary / review_notes / round / context_before / context_after`；相邻上下文只供消歧，不得输出。
- **输出**：`draft`。
- **Prompt 设计要点**：
  - system 设定角色："资深网络小说译者，熟悉中文网文类型学（修仙/玄幻/都市等）与目标语读者习惯"；
  - user 分区注入：`<glossary>` 术语硬约束（必须逐条遵守）、`<tm>` 相似句对参考（标注"参考而非照抄"）、`<bible>` 角色与设定摘要、`<notes>` 上轮批注（返工轮置于最前并声明优先级最高）；
  - 输出要求：只输出译文正文，不解释、不编号、不保留原文；
  - 明确禁区：不增删情节、不改变 POV 与时态、术语表外专名音译并保持全书一致。
- **分档建议**：strong。初译质量决定后续环节负担，是成本最不该省的位置。

### 3.4 审校 Agent（editor）

- **职责**：对照原文逐段审校 `draft`，产出结构化问题清单；只管"对不对"，不直接改稿，修改由后续润色/返工执行。
- **输入**：`source_text` = 原文段，`context` 携带 `draft`（从 state 注入）、`glossary`、`bible`。
- **输出**：`review_notes`（每条含严重度、类型、位置与修改建议）。
- **编排**：工作流逐 segment 调用，输出统一补 `segment_id/segment_index` 后归并；
  JSON/schema 失败记入 `segment_failures`，不得当成“无问题”。
- **检查清单（写入 Prompt）**：误译、漏译、增译；术语与 glossary 不一致；专名拼写前后不一；数字、方位、辈分、境界等级错误；事实与 bible 冲突。
- **Prompt 设计要点**：要求先逐条核对清单再修订；批注用固定格式 `[类型] 位置说明：问题 → 建议`；无问题时返回原稿 + 空 notes；禁止做风格性改写。

### 3.5 润色 Agent（polisher）

- **职责**：在审校后的 `draft` 上做目标语润色，产出 `polished`；提升流畅度与文学性，**不得改动事实与术语**。
- **输入**：`source_text` 可留原文段（供对照），`context` 携带 `draft`、`glossary`、`prev_segments`。
- **输出**：`polished`。
- **编排**：逐 segment 润色并与对应初稿做长度完整性检查；异常片回退初稿，
  不允许一次章级输出覆盖或截断全部译文。
- **Prompt 设计要点**：声明"只改表达，不改意思"；术语表内词条一字不许动；保持 POV、时态、段落划分与原文一致；网文特定要求（对白生动、战斗场面节奏、悬念句保留）。
- **分档建议**：strong。若成本紧张，可降为 fast 做轻润色并在消融实验中量化差异。

### 3.6 QA 终审 Agent（qa）

- **职责**：对 `polished` 终审，给出 `qa_score`（0–10）与 `qa_verdict`；rework 时产出可执行的结构化批注驱动回退返工。
- **输入**：`context` 携带原文、`polished`、`glossary`、`bible`。
- **输出**：`qa_score`、`qa_verdict`、`qa_detail.suggestions`（并入 `review_notes` 供下一轮翻译使用）。
- **编排**：逐 segment 独立 QA，章级分数按源片 token 加权；任一分片 rework、
  解析失败或前序 `segment_failures` 非空时，章级裁决必须为 rework。
- **评分量规（rubric，写入 Prompt）**：

  | 维度 | 权重 | 要点 |
  | --- | --- | --- |
  | 准确性 | 40 | 误译/漏译/增译，出现 critical 错误直接 fail |
  | 流畅性 | 20 | 语法、搭配、可读性 |
  | 术语一致性 | 20 | 与 glossary/story_bible 的一致性 |
  | 风格贴合 | 20 | 文体、语域、网文类型感 |

  当前通过线：`qa_score ≥ 7.0` 且四维均不低于 6.0 → `pass`。
- **Prompt 设计要点**：先按维度打分再给总评；批注格式与审校一致以便翻译节点统一消费；温度取 0 附近保证判决稳定；**防共谋**：QA 不查看其他 Agent 的 notes，独立判决。
- **分档建议**：strong。QA 是回环的闸门，误判（过松/过严）直接决定质量与成本。

## 4. fast / strong 分档策略

| 档位 | 定位 | 适用 Agent | 理由 |
| --- | --- | --- | --- |
| strong | 质量关键、深度推理 | 翻译、审校、润色、QA 终审 | 直接影响成品质量与判决可靠性 |
| fast | 结构化抽取、轻量判断、批量预处理 | 术语、调度（可选）、M1 管道批量术语初筛、QA 预审（可选） | 输出空间小、可校验、调用量大 |

`LLMClient.from_config(cfg)` 读取 `llm.providers.*` 中 fast/strong 两档配置；各 Agent 在构造时按上表选档，档位可通过配置覆盖以便消融实验。

工作流不直接调用 `agent.run(task)`，而统一调用 `agent.execute(task)`。
`execute` 保留原有结果契约，同时自动发射 `agent.started/completed/failed`，并把
agent、segment、round、tier 写入当前运行上下文。Agent 内部仍调用兼容的
`LLMClient.complete`；其底层由 `stream_complete` 实时发射 `llm.token`。

## 5. 成本估算思路

### 5.1 估算模型

按章计费，逐 Agent 累加 token 成本，再乘回环放大系数：

```
章节成本 = Σ_agent [ (in_tokens / 10^6 × price_in) + (out_tokens / 10^6 × price_out) ] × (1 + p_fail × r)
```

- `p_fail`：QA 首轮 fail 概率（M4 联调期实测，初期按 0.3 估）；
- `r`：平均每次 fail 触发重新执行的节点比例（回退到翻译 ≈ 重跑 翻译+审校+润色+QA，即 r≈1）；
- token 估算：汉字 ≈ 1.3 token/字；英文输出 ≈ 原文汉字数 × 1.1 token。

### 5.2 参考测算（每章 2000 汉字，示例价目，实际以供应商报价为准）

| Agent | 档 | 输入 tokens（含上下文注入） | 输出 tokens | 说明 |
| --- | --- | --- | --- | --- |
| 术语 | fast | 按片预算 | 每片 ≤1,024 | 分片抽取 JSON，代码侧归并 |
| 翻译 | strong | 6,500 | 2,200 | 含 glossary/TM/bible 注入 |
| 审校 | strong | 9,000 | 2,600 | 原文+译文双输入 |
| 润色 | strong | 5,000 | 2,200 | 主要为译文 |
| QA | strong | 7,500 | 800 | 评分+批注 |
| **单轮合计** | — | 32,000 | 8,400 | — |

按示例价目（fast：输入 ¥2 / 输出 ¥8 每百万 tokens；strong：输入 ¥10 / 输出 ¥30 每百万 tokens）：

```
单轮成本 ≈ 术语(4k×2 + 0.6k×8)/1M + strong 部分(28k×10 + 7.8k×30)/1M
        ≈ ¥0.013 + ¥0.514 ≈ ¥0.53 / 章
含回环（p_fail=0.3, r=1）≈ ¥0.53 × 1.3 ≈ ¥0.69 / 章
```

一部 500 章的中篇网文全量翻译约 **¥350** 量级；相对地，M2 单 Agent 基线（仅一次 strong 直译，输入 4k/输出 2.2k）约 ¥0.11/章——本系统约 6 倍成本，需在 M5 用质量提升幅度论证性价比。

### 5.3 降本手段（TODO 挂钩）

- 段级去重：`search_tm` 高相似命中（score ≥ 0.95）时跳过翻译直接复用；
- 术语结果按章缓存，避免同章重复抽取；
- QA 两级制：fast 预审 + 仅可疑章节走 strong 终审（消融项）；
- 成本统计脚本（TODO：`mant.eval` 下挂日志解析器），每次运行落盘 token 用量，支撑论文成本分析章节。
