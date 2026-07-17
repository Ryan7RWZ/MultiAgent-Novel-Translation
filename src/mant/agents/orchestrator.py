"""调度 Agent（OrchestratorAgent）：章节切分、执行计划与任务分派。

职责定位（docs/agent-design.md §3.1）：
- 把"一部作品的一章"原文按场景/字数切分为 segment 序列（``split_chapter``）；
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

import re
from typing import TYPE_CHECKING

from mant.agents.base import AgentResult, AgentTask, BaseAgent

if TYPE_CHECKING:  # 仅类型标注，避免运行时循环依赖
    from mant.llm.client import LLMClient
    from mant.memory import MemoryHub

__all__ = ["OrchestratorAgent", "DEFAULT_MAX_SEGMENT_CHARS", "DEFAULT_MIN_SEGMENT_CHARS"]

#: 单个 segment 的默认最大字符数（经验值：兼顾模型上下文与段内连贯性）
DEFAULT_MAX_SEGMENT_CHARS = 1200

#: 过短的尾部 segment 并入前一段的阈值（避免碎片化翻译）
DEFAULT_MIN_SEGMENT_CHARS = 200

#: 场景分隔线：单独成行、仅由分隔符号组成（网文常见场景切换标记）。
#: TODO: 单独成行的 "……" 也可能是对白省略号，存在误切风险，后续复核。
_SCENE_BREAK_RE = re.compile(
    r"^\s*(?:\*{3,}|-{3,}|_{3,}|={3,}|…+|※+|◇+|◆+|○+|●+|·{3,})\s*$"
)

#: 句末标点（超长段落做句子级二次切分）
_SENTENCE_END_RE = re.compile(r"(?<=[。！？!?；;…])")


class OrchestratorAgent(BaseAgent):
    """调度 Agent：章节切分 + 静态计划/分派（策略说明见模块 docstring）。

    output 约定（``AgentResult.output``）：
    - ``segments``: ``list[str]``，按场景/字数切分后的原文片段（核心产物，
      供 ``mant.workflow.graph.run_chapter`` 写入 ``TranslationState.segments``）；
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
        max_segment_chars: int = DEFAULT_MAX_SEGMENT_CHARS,
        min_segment_chars: int = DEFAULT_MIN_SEGMENT_CHARS,
    ) -> None:
        super().__init__(llm, memory)
        self.max_segment_chars = max(100, int(max_segment_chars))
        self.min_segment_chars = max(
            0, min(int(min_segment_chars), self.max_segment_chars // 2)
        )

    # ------------------------------------------------------------------
    # 核心：章节切分启发式
    # ------------------------------------------------------------------
    def split_chapter(self, text: str) -> list[str]:
        """把整章原文切分为 segment 列表（启发式实现）。

        规则（按优先级）：
        1. 统一换行符后按空行划分段落；
        2. 命中场景分隔线（如 ``***``、``……`` 单独成行）→ 强制硬切分，
           分隔线本身丢弃（TODO: 可配置为保留为独立 segment 以维持版式对应）；
        3. 按 ``max_segment_chars`` 字符预算顺序合并段落，超出预算另起 segment；
        4. 单段超过预算 → 按句末标点做句子级二次切分，单句仍超长则硬截断；
        5. 末段过短（< ``min_segment_chars``）时并入前一段，避免碎片化。

        TODO:
        - 引入 LLM/分类模型识别场景边界（视角切换、对话密度、时间跳跃）；
        - 依据 fast/strong 档模型上下文窗口动态调整 ``max_segment_chars``；
        - 保留原文缩进/换行结构，便于译文排版还原；
        - 段间重叠上下文（overlap）缓解切分处的指代断裂。
        """
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []

        segments: list[str] = []
        current: list[str] = []
        current_len = 0
        para_lines: list[str] = []

        def _flush_segment() -> None:
            nonlocal current, current_len
            if current:
                segments.append("\n\n".join(current))
                current = []
                current_len = 0

        def _flush_paragraph() -> None:
            """把段落缓冲按字符预算并入当前 segment（必要时先另起一段）。"""
            nonlocal current_len
            if not para_lines:
                return
            para = "\n".join(para_lines).strip()
            para_lines.clear()
            if not para:
                return
            for piece in self._split_long_paragraph(para):
                extra = len(piece) + (2 if current else 0)  # 2 = "\n\n" 连接符
                if current and current_len + extra > self.max_segment_chars:
                    _flush_segment()
                    extra = len(piece)
                current.append(piece)
                current_len += extra

        for line in normalized.split("\n"):
            stripped = line.strip()
            if not stripped:  # 空行：段落边界
                _flush_paragraph()
                continue
            if _SCENE_BREAK_RE.match(stripped):  # 场景分隔线：硬切分并丢弃
                _flush_paragraph()
                _flush_segment()
                continue
            para_lines.append(stripped)
        _flush_paragraph()
        _flush_segment()

        # 规则 5：过短尾段并入前一段
        if len(segments) > 1 and len(segments[-1]) < self.min_segment_chars:
            segments[-2] = segments[-2] + "\n\n" + segments[-1]
            segments.pop()
        return segments

    def _split_long_paragraph(self, para: str) -> list[str]:
        """超长段落 → 按句末标点聚合为不超长度的句子块；单句仍超长则硬截断。"""
        if len(para) <= self.max_segment_chars:
            return [para]
        chunks: list[str] = []
        buf = ""
        for sentence in _SENTENCE_END_RE.split(para):
            if not sentence:
                continue
            if buf and len(buf) + len(sentence) > self.max_segment_chars:
                chunks.append(buf)
                buf = ""
            buf += sentence
            while len(buf) > self.max_segment_chars:  # 单句超长：硬截断
                chunks.append(buf[: self.max_segment_chars])
                buf = buf[self.max_segment_chars :]
        if buf:
            chunks.append(buf)
        return chunks

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
        segments = self.split_chapter(task.source_text)
        notes = [
            f"章节切分完成：{len(task.source_text)} 字 → {len(segments)} 个 segment"
            f"（max_segment_chars={self.max_segment_chars}）"
        ]
        if not segments:
            notes.append("原文为空或仅剩空白字符，无 segment 产出。")
            return self._result(
                ok=False, output={"segments": [], "plan": [], "dispatch": {}}, notes=notes
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
            output={"segments": segments, "plan": plan, "dispatch": dispatch},
            notes=notes,
        )
