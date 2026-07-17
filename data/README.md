# 数据目录规范（M1 离线语料管道）

本文件约定 M1 离线语料管道（`mant.pipeline`）的输入/产物目录结构、文件格式与
版权合规要求。一键运行入口见 `scripts/run_m1_pipeline.py`。

## 目录结构

```text
data/
  raw/                        # 原始语料（采集产物，清洗前的唯一事实来源）
    <work_id>/                # 一部作品一个目录（work_id 建议用拼音/英文 slug，如 battle-through-heaven）
      src/*.txt               # 源语言原文（默认中文）
      tgt/*.txt               # 目标语言参考译文（人工/官方译本，可选；用于句对齐与 TM）
  aligned/                    # 句对齐产物：每作品一个 <work_id>.jsonl
  memory/mant.db              # 运行时 SQLite：术语库 + TM
  glossary/                   # 术语审阅/导出文件
  inputs/                     # 浏览器工作台提交的原文快照
  exports/                    # 基线与多 Agent 成品/元数据
  traces/                     # 每次运行的事件 JSONL + runs.db（本地监控数据源）
```

`traces/<run_id>.jsonl` 包含可回放的 Agent 状态和 LLM 输出增量，可能含授权
原文/译文内容；该目录已被 Git 忽略，仍需按本机敏感数据管理。事件不会记录
Prompt 正文或 API key，常见密钥字段在写盘前还会统一脱敏。

浏览器任务按 `inputs/<work>/<chapter>/<run_id>.txt` 保存输入，最终译文与元数据
按 `exports/web/<work>/<chapter>/<run_id>.txt|json` 保存，避免同章多次运行互相覆盖。

## 原始语料放置格式

- 每部作品一个目录：`data/raw/<work_id>/src/*.txt`、`data/raw/<work_id>/tgt/*.txt`；
- **文件名（不含扩展名）即 doc_id**：同一作品的 src/tgt 文件按 doc_id 一一配对，
  即 `src/0001.txt` 与 `tgt/0001.txt` 必须是同一章/同一段内容的双语版本；
- 章节标题请保持 `第X章 …`（中）或 `Chapter N …`（英）格式，便于自动切章与章节配对；
- 编码统一 **UTF-8**（采集器对 GB18030 旧语料做只读回退兼容，新语料请先转码）；
- 原始语料放入后不要手工改动：清洗/对齐都在副本上进行，保证可追溯、可重跑。

## JSONL 句对格式（aligned 产物）

每行一个 JSON 对象（UTF-8、非 ASCII 不转义），字段固定为
`src`（源句）、`tgt`（译句）、`chapter`（章节标题）、`index`（章内句对序号）：

```jsonl
{"src": "萧炎盘膝坐在床榻之上，缓缓吐出一口浊气。", "tgt": "Xiao Yan sat cross-legged on the bed, slowly exhaling a mouthful of stale air.", "chapter": "第一章 陨落的天才", "index": 0}
{"src": "三年了，整整三年，他受尽了嘲讽与白眼。", "tgt": "Three years. For three whole years, he had endured nothing but ridicule and scorn.", "chapter": "第一章 陨落的天才", "index": 1}
{"src": "“斗之力，三段！”", "tgt": "\"Dou Qi, third stage!\"", "chapter": "第二章 斗气大陆", "index": 0}
```

## 术语库

- 默认路径：`data/memory/mant.db`（与 `memory.sqlite_path` 一致，`--glossary-db` 可改）；
- 表结构由 `mant.memory.glossary.GlossaryStore` 自动创建（`terms` 表，
  `(source, work_id)` 唯一索引，重复写入即覆盖更新），字段：
  `source / target / category / work_id / confidence / created_at`；
- 若作品目录提供 `terminology.md`，其中的人工术语以 `confidence=1.0` 入库，
  并清理同作品的空译名离线候选；没有人工表且未开 `--with-llm` 时，TF-IDF
  候选可以空译名留库待复核，但 `lookup_terms` 不会把它们注入翻译 Prompt。
- 对齐后的句对会同时写入同一 SQLite 的 `tm_pairs` 表，供 MemoryHub 直接检索。

## 版权与合规（红线，务必遵守）

1. **只允许**放入：已获书面授权的作品、公有领域作品、明确允许学术使用的开放授权作品；
2. **禁止**存放任何盗版/破解来源语料；**禁止**绕过付费墙、登录鉴权、DRM 或反爬措施获取文本；
3. 网络采集必须遵守目标站点 robots.txt 与服务条款、限速抓取（≥ 3 秒间隔），
   并在元数据中记录 `source_url` / `license` / `fetched_at` 以便审计追溯；
4. 语料与全部产物仅限课程实验/学术研究使用，**不得二次分发、不得商用**；
5. 大体积原文不要提交进 git 仓库（建议 `data/raw/` 入 `.gitignore`，团队内网盘另存）。
