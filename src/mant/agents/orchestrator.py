"""调度 Agent（OrchestratorAgent）：机械章节切分、执行计划与任务分派。

职责定位（docs/agent-design.md §3.1）：
- 把"一部作品的一章"原文按结构/token 预算机械切分为 segment 序列；
- 产出执行计划 ``plan`` 与分派指令 ``dispatch``（规则优先；仅在需要开放式
  计划/排序判断时才调用 fast 档模型，见 ``SYSTEM_PROMPT``，骨架期不触发）；
- 状态转移本身由 ``mant.workflow.graph`` 的 LangGraph 状态机表达，本 Agent
  只做参数化决策（切分粒度、并发度、提前终止、人工接管）。

返工上限策略
------------
- 上限取自配置键 ``workflow.max_rework``（config/settings.example.yaml），
  由入口经 ``init_state`` 写入 ``TranslationState.max_rework``；
- QA 终审判 ``rework``（兼容旧称 ``fail``）且 ``rework_count < max_rework``
  时，状态机携带 ``review_notes`` 批注回退 translate 节点重译，每实际
  返工一轮 ``rework_count + 1``；
- 达到上限仍不达标：强制放行当前稿并在批注中打 ``needs_human_review``
  标记，避免 LLM 互相不认可导致的死循环与费用失控（docs/architecture.md §3.3）。

冲突仲裁策略
------------
多 Agent 产出冲突时按以下优先级裁决（高 → 低）：
1. 术语库既有条目：同一 ``work_id`` 内"先入库者为准"；新抽取术语仅当
   ``confidence`` 高出库内条目 0.2 以上时记为"待人工裁决"，不自动覆盖；
2. QA 终审：握有质量一票否决权（``qa_verdict``），其返工建议
   （``qa_detail.suggestions``）在下一轮翻译中具有最高执行优先级；
3. 审校 Agent：事实性/一致性问题（误译、漏译、术语不一致等）必须修复；
4. 润色 Agent：仅做语言风格改写，不得推翻术语与事实性内容；
5. 翻译 Agent 初稿。
调度 Agent 对切分方式与任务路由保留最终决定权；同优先级冲突时存量数据
（TM / 既有术语）优先于新产出。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from mant.agents.base import AgentResult, AgentTask, BaseAgent
from mant.segmentation import (
    DeterministicSegmenter,
    SegmentationConfig,
    SegmentationResult,
)

if TYPE_CHECKING:  # 仅类型标注，避免运行时循环依赖
    from mant.llm.client import LLMClient
    from mant.memory import MemoryHub

__all__ = ["OrchestratorAgent", "DEFAULT_MAX_SEGMENT_CHARS", "DEFAULT_MIN_SEGMENT_CHARS"]

#: 单个 segment 的默认最大字符数（经验值：兼顾模型上下文与段内连贯性）
DEFAULT_MAX_SEGMENT_CHARS = 1200

#: 过短的尾部 segment 并入前一段的阈值（避免碎片化翻译）
DEFAULT_MIN_SEGMENT_CHARS = 200

class OrchestratorAgent(BaseAgent):
    """调度 Agent：章节切分 + 静态计划/分派（策略说明见模块 docstring）。

    output 约定（``AgentResult.output``）：
    - ``segments``: ``list[str]``，按结构/token 预算切分后的原文片段；
    - ``segment_meta``: ``list[dict]``，片段定位、上下文、边界和哈希；
    - ``normalized_text`` / ``segmentation_stats``: 可逆原文与切片统计，
      供 ``mant.workflow.graph.run_chapter`` 写入 ``TranslationState``；
    - ``plan``: ``list[dict]``，流水线步骤清单与依赖（骨架：静态四步计划）；
    - ``dispatch``: ``dict``，分派指令（下一节点、segment 数、携带的 context 键）。
    """

    name = "orchestrator"

    #: 期望模型档位（规则优先，LLM 计划模式可选；docs/agent-design.md §4）
    tier = "fast"

    #: 可选的 LLM 计划模式系统提示词（骨架期默认规则路径，不调用）
    SYSTEM_PROMPT = (
        "你是翻译流水线的调度器。只输出 JSON 计划，禁止自由文本；"
        "输入含当前 rework_count / max_rework，"
        "输出含明确的 next_node 与理由。"
    )

    def __init__(
        self,
        llm: "LLMClient",
        memory: "MemoryHub | None" = None,
        *,
        segmentation_config: SegmentationConfig | Mapping[str, Any] | None = None,
        max_segment_chars: int | None = None,
        min_segment_chars: int | None = None,
    ) -> None:
        super().__init__(llm, memory)
        # 旧的字符预算参数保留为兼容入口；新调用应使用 token 预算配置。
        if segmentation_config is not None and (
            max_segment_chars is not None or min_segment_chars is not None
        ):
            raise ValueError("不能同时设置 segmentation_config 与旧字符预算参数")
        if max_segment_chars is not None or min_segment_chars is not None:
            legacy_max = max(
                100,
                int(
                    max_segment_chars
                    if max_segment_chars is not None
                    else DEFAULT_MAX_SEGMENT_CHARS
                ),
            )
            legacy_min = max(
                0,
                min(
                    int(
                        min_segment_chars
                        if min_segment_chars is not None
                        else DEFAULT_MIN_SEGMENT_CHARS
                    ),
                    legacy_max // 2,
                ),
            )
            segmentation_config = {
                "target_core_tokens": max(1, int(legacy_max * 0.75)),
                "max_core_tokens": legacy_max,
                "min_core_tokens": legacy_min,
            }
        self.segmenter = DeterministicSegmenter(segmentation_config)
        # 为只读旧属性的外部代码保留可解释值；切片实际以 token 为单位。
        self.max_segment_chars = self.segmenter.config.max_core_tokens
        self.min_segment_chars = self.segmenter.config.min_core_tokens

    # ------------------------------------------------------------------
    # 核心：确定性章节切分
    # ------------------------------------------------------------------
    def segment_chapter(
        self, text: str, *, chapter_id: str = "chapter"
    ) -> SegmentationResult:
        """返回完整机械切片结果；该路径不调用 LLM，也不访问记忆层。"""
        return self.segmenter.segment(text, chapter_id=chapter_id)

    def split_chapter(self, text: str) -> list[str]:
        """兼容旧接口，仅返回片段正文；完整元数据请用 ``segment_chapter``。"""
        return self.segment_chapter(text).texts

    # ------------------------------------------------------------------
    # BaseAgent 接口
    # ------------------------------------------------------------------
    def run(self, task: AgentTask) -> AgentResult:
        """切分 ``task.source_text`` 并给出静态执行计划与分派指令。

        TODO:
        - 段级并发度决策与失败跳步策略（如某 Agent 异常时跳过润色直送 QA）；
        - 需要开放式判断（如下一章优先级排序）时调用 fast 档模型
          （使用 ``SYSTEM_PROMPT``，只输出 JSON 计划）；
        - 结合小说圣经识别 POV 切换以优化切分点。
        """
        result = self.segment_chapter(task.source_text, chapter_id=task.chapter_id)
        segments = result.texts
        stats = result.statistics.to_dict()
        notes = [
            f"章节切分完成：{len(task.source_text)} 字 → {len(segments)} 个 segment"
            f"（max_core_tokens={self.segmenter.config.max_core_tokens}）"
        ]
        if not segments:
            notes.append("原文为空或仅剩空白字符，无 segment 产出。")
            return self._result(
                ok=False,
                output={
                    "segments": [],
                    "segment_meta": [],
                    "normalized_text": result.normalized_text,
                    "segmentation_stats": stats,
                    "plan": [],
                    "dispatch": {},
                },
                notes=notes,
            )

        # 骨架期静态计划：主链路四步（状态机与条件边见 mant.workflow.graph）
        plan = [
            {"step": 0, "node": "translate", "agent": "translator", "depends_on": []},
            {"step": 1, "node": "edit", "agent": "editor", "depends_on": ["translate"]},
            {"step": 2, "node": "polish", "agent": "polisher", "depends_on": ["edit"]},
            {"step": 3, "node": "qa", "agent": "qa", "depends_on": ["polish"]},
        ]
        dispatch = {
            "next_node": "translate",
            "segment_count": len(segments),
            # 各 Agent 已实现的 context 键（见各自模块 docstring）
            "context_keys": [
                "glossary",
                "story_bible",
                "tm_matches",
                "prev_summary",
                "review_notes",
            ],
        }
        return self._result(
            ok=True,
            output={
                "segments": segments,
                "segment_meta": result.metadata,
                "normalized_text": result.normalized_text,
                "segmentation_stats": stats,
                "plan": plan,
                "dispatch": dispatch,
            },
            notes=notes,
        )
