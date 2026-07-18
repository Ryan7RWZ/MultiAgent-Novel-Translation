# 确定性初始切片

线上单章翻译的初始切片由 `mant.segmentation` 完成，完全不调用 LLM。它的目标
不是理解剧情，而是在模型预算内尽量沿自然结构切开，同时保留可定位、可审计、
可精确回拼的原文表示。

## 1. 硬性不变量

1. `"".join(segment.core_text) == normalized_text`，片段连续、互不重叠、无缺口。
2. 每个片段的估算 token 数不超过 `max_core_tokens`。
3. 同一输入、章节 ID 和配置总是得到完全相同的片段、ID、边界和哈希。
4. 场景线、标题、重复行和原始空白不作为“噪声”丢弃。
5. 相邻上下文只帮助当前片消歧，不属于待译核心，也不能跨越标题/场景强边界。

线上入口只做安全规范化：去 UTF-8 BOM、统一 CRLF/CR 为 LF、移除不可见控制
字符。它不会复用 M1 语料清洗中的去重或广告过滤，因为重复句可能是有意修辞。

## 2. token 预算

没有供应商 tokenizer 时，切片器使用可做前缀和的保守机械估算：CJK 字符权重
最高，ASCII 字母数字按词片近似，标点与其他 Unicode 字符使用固定权重。这样可
在任意区间 O(1) 计算预算，并在无网络、无模型依赖时稳定运行。

估算值不是供应商的精确 billing token。`max_core_tokens` 应给系统提示词、术语、
TM、故事设定和输出留出余量；真实供应商调用层后续仍需做最终上下文窗口校验。

## 3. 边界层级与选择

切片器先扫描候选边界，优先级从高到低为：

1. 文档起止、章节标题、场景分隔线；标题和场景是强边界，片段及相邻上下文均
   不得跨越。
2. 空行、普通段落换行。
3. 中英文句末标点及尾随引号。
4. 分号、逗号等分句标点。
5. 英文空白；只在高级候选间距已经超过硬预算时按小 token 间距抽样补充，
   防止逐词候选膨胀。
6. 机械硬切；仅在仍找不到预算内自然切点时按目标片大小生成。

每个强边界区间独立执行确定性动态规划。目标函数同时考虑片段大小偏离
`target_core_tokens` 的程度、过短片惩罚和边界类型代价；先最小化总代价，再用
片段数作稳定的次级排序。`min_core_tokens` 是软下限，`max_core_tokens` 是硬上限。
对极端短句或标点密集文本，候选会先在一个很小的 token 窗口内确定性聚类：每组
保留优先级最高的自然边界，同级取最靠后者。完整边界表仍用于上下文对齐，从而
避免动态规划因逐字符级候选而退化，同时不改变原文覆盖。

## 4. 相邻上下文和元数据

每个 `Segment` 保存：

- 稳定 ID、顺序号、原文 `[source_start, source_end)`、正文哈希；
- 正文估算 token、覆盖的段落 ID、前后边界类型、是否邻接硬切点；
- `context_before` 和 `context_after`，分别受独立 token 预算约束；
- `translatable` 标记。

`TranslationState.segments` 保持旧的 `list[str]` 契约，
`TranslationState.segment_meta` 存储不重复正文的元数据，完整规范化原文放在
`TranslationState.source_text`。术语抽取仍读取章级 `source_text`；翻译、审校、
润色和 QA 均沿用同一组 segment，分别保存 `draft_segments`、
`polished_segments` 和 `segment_qa`，不再把整章塞入一次输出受限的调用。译者只
翻译 `core_text`；相邻上下文在 Prompt 中明确标为“仅供理解，禁止翻译或输出”。

## 5. 配置、观测与保护

配置位于 `segmentation.*`：

| 键 | 默认值 | 作用 |
| --- | ---: | --- |
| `target_core_tokens` | 900 | 优化器偏好的正文大小 |
| `max_core_tokens` | 1200 | 单片正文硬上限 |
| `min_core_tokens` | 250 | 非末片软下限 |
| `context_before_tokens` | 160 | 相邻上文预算 |
| `context_after_tokens` | 80 | 相邻下文预算 |
| `max_segments` | 5000 | 异常碎片化保护上限 |

每章发出一个 `segmentation.completed` 事件，记录配置、哈希、片数、token 分布、
边界统计和回拼结果；每个实际硬切边界另发 `segmentation.hard_split`。空白章节、
片数超限或可逆性失败会在调用 LLM 前终止，不静默带病运行。

下游 Editor/Polisher/QA 已逐片处理并确定性归并，残稿和润色长度异常会显式失败。
尚待解决的是章级术语抽取预算、术语/TM/故事圣经注入上限、译文版式精确重建、
供应商精确 tokenizer、分片并发/断点续跑和全书级成本控制。
