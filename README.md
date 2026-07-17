# MANT · 基于大数据与多智能体协作的网络小说自主翻译系统

> Multi-Agent Novel Translation：多智能体分工 + LangGraph 状态机回环 +
> 记忆与数据层 + 离线语料资产沉淀的网络小说自动翻译系统。

## 一、项目简介

网络小说动辄数百万字，人名、地名、功法、境界等设定成百上千且跨章节演化，
把整章"一次性丢给大模型"会在一致性、文风、质量与成本上同时失控。MANT 的思路：

- **多智能体分工**：调度 / 术语 / 翻译 / 审校 / 润色 / QA 终审六个 Agent 各司其职；
- **状态机回环**：用 LangGraph 把翻译流程建模为状态机，QA 不达标时**携带批注回退返工**；
- **记忆与数据层**：术语库（SQLite，可切 Postgres）、小说圣经、翻译记忆库 TM、
  FAISS 向量检索 RAG，保证跨章节一致性；
- **语料资产先行**：M1 离线管道把平行语料沉淀为术语库与 TM，越译越准；
- **实验对照**：M2 单 Agent 基线与多智能体方案同口径对比，用数据验证收益。

### 四大难点

1. **超长程一致性** —— 数百章之后人名 / 设定不能漂移：术语库 + 小说圣经 + TM / RAG 记忆层。
2. **文风与"爽感"保持** —— 网文口语化节奏与文化负载词难以直译：润色 Agent + 语料风格参照。
3. **无参考的质量闭环** —— 没有人工参考译文也要自动评判：QA 终审打分 + 状态机批注回退。
4. **成本与吞吐** —— 数百万字的翻译预算必须可控：fast / strong 双档模型调度 + TM 命中复用。

## 二、系统架构

```text
┌───────────────────────────────────────────────────────────────────────────┐
│ CLI 入口（mant.cli）：m1-pipeline / baseline / translate-chapter          │
└───────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ LangGraph 状态机（mant.workflow）                                         │
│ 状态 TranslationState │ QA 不达标 → 携带批注回退返工（≤ max_rework 次）   │
└───────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ 多智能体层（mant.agents）：统一经 LLMClient（fast/strong 双档）调用大模型 │
│ 调度 │ 术语 │ 翻译 │ 审校 │ 润色 │ QA 终审                                │
└───────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ 记忆与数据层（MemoryHub · mant.memory）                                   │
│ 术语库 SQLite/可切 Postgres │ 小说圣经 │ 翻译记忆 TM │ FAISS 向量检索 RAG │
└───────────────────────────────────────────────────────────────────────────┘
                                      ▲  语料资产沉淀（术语库 / TM / 小说圣经）
                                      │
┌───────────────────────────────────────────────────────────────────────────┐
│ M1 离线语料管道（mant.pipeline）：采集 → 清洗 → 句对齐 → 术语抽取         │
└───────────────────────────────────────────────────────────────────────────┘
```

## 三、目录结构

```text
MultiAgent-Novel-Translation/
├── config/
│   └── settings.example.yaml   # 配置模板（复制为 settings.yaml 使用）
├── data/                       # 数据目录（内容不入库，仅 .gitkeep 占位）
│   ├── raw/                    # M1 采集的原始语料
│   ├── aligned/                # 句对齐后的平行语料
│   ├── glossary/               # 术语抽取结果 / 术语库导出
│   └── exports/                # 译文导出
├── docs/                       # 设计文档
├── src/
│   └── mant/                   # 包名 mant（src 布局）
│       ├── __init__.py         # __version__ 与包级说明
│       ├── cli.py              # 命令行入口（三个子命令）
│       ├── llm/                # LLMClient（fast/strong 双档，未配 key 时 [DRAFT] 占位）
│       ├── agents/             # BaseAgent / AgentTask / AgentResult 与各角色 Agent
│       ├── memory/             # MemoryHub 门面：术语库 / 小说圣经 / TM / FAISS
│       ├── workflow/           # LangGraph 状态机与 TranslationState
│       └── pipeline/           # M1 离线语料管道
├── tests/
│   ├── __init__.py             # 将 src 加入 sys.path（python -m unittest 可用）
│   └── conftest.py             # pytest 通用：同上注入 sys.path
├── pyproject.toml              # 打包与依赖（Python>=3.11，extras: faiss / dev）
├── requirements.txt            # 依赖清单（与 pyproject 一致）
└── README.md
```

> 注：`mant.pipeline` / `mant.workflow` 等子包由对应负责人并行搭建，骨架阶段可能尚未出现；
> CLI 会对缺失模块延迟导入并打印 TODO 提示，不影响其他命令使用。

## 四、快速开始

### 4.1 安装（Python 3.11+）

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
# source .venv/bin/activate

pip install -e .            # 开发模式安装（src 布局）
pip install -e ".[faiss]"   # 可选：FAISS 向量检索（RAG）
pip install -e ".[dev]"     # 可选：pytest 等开发工具
# 或者：pip install -r requirements.txt
```

M1 句对齐可选 vecalign / LASER，通常需从源码安装（默认不装，不影响任何功能）：

```bash
pip install git+https://github.com/thompsonb/vecalign.git
```

### 4.2 配置

```bash
# Windows:
copy config\settings.example.yaml config\settings.yaml
set OPENAI_API_KEY=sk-你的key
# macOS / Linux:
# cp config/settings.example.yaml config/settings.yaml
# export OPENAI_API_KEY=sk-你的key
```

- `settings.yaml` 已加入 .gitignore；**API key 只走环境变量**（由 `api_key_env` 指定变量名）；
- 未配置 API key 时 `LLMClient` 返回 `[DRAFT]` 前缀的占位响应，流程仍可跑通。

### 4.3 三个子命令

```bash
# M1 离线语料管道：采集 → 清洗 → 句对齐 → 术语抽取
mant m1-pipeline --config config/settings.yaml

# M2 单 Agent 基线翻译（实验对照组）
mant baseline --work-id demo --chapter-id 0001 --input chapter.txt

# 多智能体协作翻译单章（LangGraph 状态机，QA 回环返工）
mant translate-chapter --work-id demo --chapter-id 0001 --input chapter.txt
```

骨架阶段子命令打印 TODO 提示；未安装也可直接运行：
`PYTHONPATH=src python -m mant.cli --help`（Windows CMD：`set PYTHONPATH=src`）。

开发自检：

```bash
python -m unittest          # 测试发现（tests/ 已注入 src 路径）
```

## 五、路线图（12 周）

| 周次    | 里程碑 | 内容 |
| ------- | ------ | ---- |
| W1–W2   | 骨架   | 统一接口约定、基础设施与打包、自检流程（当前阶段） |
| W3–W4   | **M1** | 离线语料管道：采集→清洗→句对齐→术语抽取，产出术语库 / TM 初版 |
| W5–W6   | 记忆层 | MemoryHub：术语库（SQLite）、小说圣经、TM、FAISS RAG |
| W7      | **M2** | 单 Agent 基线跑通并留档，作为实验对照 |
| W8–W9   | **M3** | 多智能体 + LangGraph 状态机，QA 批注回退返工回环 |
| W10     | M4     | 评测体系：基线 vs 多智能体（一致性 / 文风 / 人工抽检） |
| W11     | **M5** | 端到端集成：章节进、译文出，导出与演示 |
| W12     | 交付   | 打磨、文档、答辩 |

**MVP 压缩路径**：M1（小规模语料跑通管道）→ M2（基线对照）→ M3（多智能体最小闭环）→ M5（单章端到端导出）。
M4 评测在 MVP 中仅保留最小对照实验，完整评测后置。

## 六、协作约定（骨架阶段）

- 第三方库一律延迟导入（函数内 import 或 try/except 降级并给安装提示），
  保证仅 stdlib + numpy 环境下 `import mant.*` 全部成功、`python -m unittest` 可运行；
- 数据模型一律使用 dataclass（不用 pydantic）；各模块通过参数接收配置字典，不直接读配置文件；
- 统一接口：`mant.llm.client.LLMClient`、`mant.agents.base.BaseAgent`、
  `mant.memory.MemoryHub`、`mant.workflow.state.TranslationState`，按签名调用，不重复定义。

## 七、版权合规声明

- 本项目仅用于教学与科研目的，不提供、不传播任何受版权保护的小说原文或译文。
- M1 管道处理的语料必须来自**已获授权**、公有领域或许可证允许的来源；
  采集前须自行确认版权状态，并遵守目标站点的 robots 协议与使用条款。
- 术语库、翻译记忆等衍生数据仅保存在本地 `data/` 目录（已 gitignore，不进入版本库）。
- 译文的版权归属与使用范围遵循源文本许可证；未经授权不得将译文用于商业发布。
- 若权利方认为相关内容侵权，请联系项目组核实并移除。
