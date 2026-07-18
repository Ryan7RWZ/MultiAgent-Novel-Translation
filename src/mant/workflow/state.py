"""LangGraph 工作流状态定义（团队约定，修改需全员同步）。

本模块只依赖标准库，保证任何环境（仅 stdlib + numpy）下均可导入。
各 Agent 节点通过读写 ``TranslationState`` 完成协作；
字段含义与 docs/architecture.md §3.1 的状态表一一对应。
"""

from typing import Any, TypedDict

__all__ = ["TranslationState", "init_state", "DEFAULT_MAX_REWORK"]

#: 返工上限默认值；正式运行应由配置键 ``workflow.max_rework``
#: （config/settings.example.yaml）覆盖，此处仅作骨架期兜底。
DEFAULT_MAX_REWORK = 2


class TranslationState(TypedDict):
    """单章翻译流水线的共享状态。

    字段:
        work_id: 作品 ID（贯穿记忆层：术语库 / TM / 小说圣经的命名空间）。
        chapter_id: 章节 ID（一般为文件名或章节号）。
        run_id: 运行标识；checkpoint 恢复时必须沿用同一个值。
        source_text: 安全规范化后的完整原文。所有章级 Agent 使用该字段，避免
            重新拼接片段时增删空白。
        segments: 调度 Agent 切分后的原文片段列表，按序翻译；入口写入后不变。
        segment_meta: 与 ``segments`` 等长的定位/边界/邻接上下文元数据。
        segmentation_stats: 切片预算、边界和可逆性统计，供观测与导出。
        glossary: 本章生效的术语映射 ``{源术语: 规范译名}``（retrieve 节点
            经术语 Agent 注入）。
        draft_segments / draft: 分片初稿和它的确定性章级拼接结果。
        review_notes: 审校 / QA 批注列表；QA 判返工时追加结构化批注，
            作为回退到 translate 节点的返工输入。
        revised_segments / revised: Editor 之后由 Translator 定点修订模式产出的
            分片事实修订稿；没有事实性意见的片段直接沿用初稿。
        polished_segments / polished: 分片润色稿和确定性章级拼接结果。
        segment_failures: 各阶段按 segment 记录的失败/完整性告警；存在任一
            条目时章级 QA 不得放行。
        segment_qa: 每个 segment 的 QA 分数、裁决与明细，章级结果由代码归并。
        rework_segment_indices: QA 或完整性检查标记的定点返工片段序号。
        execution_stats: 本次运行累计的派发、失败、拒绝和 checkpoint 统计。
        qa_summary: QA 的实际评估覆盖率、已评估片通过率与技术失败分类。
        qa_score: QA 终审质量分（由 QAAgent 按维度权重确定性计算）。
        qa_verdict: QA 终审结论：``"pass"`` 放行 / ``"rework"`` 返工
            （状态机兼容 docs 旧称 ``"fail"``）。
        rework_count: 已实际返工次数（回退到 translate 时 +1）。
        max_rework: 返工上限，取自配置 ``workflow.max_rework``，防止死循环。
        story_bible / tm_matches: retrieve 节点的检索结果。它们属于单次运行
            状态，不放在 compiled graph 闭包中，因此同一图可安全并发复用。
        runtime_notes: 各 Agent 与记忆层产生的运行说明。
    """

    work_id: str
    chapter_id: str
    run_id: str
    source_text: str
    segments: list[str]
    segment_meta: list[dict]
    segmentation_stats: dict[str, Any]
    glossary: dict
    draft_segments: list[str]
    draft: str
    review_notes: list
    revised_segments: list[str]
    revised: str
    polished_segments: list[str]
    polished: str
    segment_failures: list[dict[str, Any]]
    segment_qa: list[dict[str, Any]]
    rework_segment_indices: list[int]
    execution_stats: dict[str, Any]
    qa_summary: dict[str, Any]
    qa_score: float
    qa_verdict: str
    rework_count: int
    max_rework: int
    story_bible: Any
    tm_matches: list[dict]
    runtime_notes: list[str]


def init_state(
    work_id: str,
    chapter_id: str,
    segments: list[str],
    *,
    max_rework: int = DEFAULT_MAX_REWORK,
    source_text: str | None = None,
    segment_meta: list[dict] | None = None,
    segmentation_stats: dict[str, Any] | None = None,
    run_id: str = "",
) -> TranslationState:
    """构造初始状态的便捷工厂（骨架）。

    TODO: ``max_rework`` 由配置层（``workflow.max_rework``）注入后，
    调用方应显式传参，本默认值仅保留给离线联调。
    """
    return TranslationState(
        work_id=work_id,
        chapter_id=chapter_id,
        run_id=str(run_id),
        source_text="".join(segments) if source_text is None else str(source_text),
        segments=list(segments),
        segment_meta=list(segment_meta or []),
        segmentation_stats=dict(segmentation_stats or {}),
        glossary={},
        draft_segments=[],
        draft="",
        review_notes=[],
        revised_segments=[],
        revised="",
        polished_segments=[],
        polished="",
        segment_failures=[],
        segment_qa=[],
        rework_segment_indices=[],
        execution_stats={},
        qa_summary={},
        qa_score=0.0,
        qa_verdict="",
        rework_count=0,
        max_rework=int(max_rework),
        story_bible=None,
        tm_matches=[],
        runtime_notes=[],
    )
