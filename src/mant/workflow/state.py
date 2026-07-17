"""LangGraph 工作流状态定义（团队约定，修改需全员同步）。

本模块只依赖标准库，保证任何环境（仅 stdlib + numpy）下均可导入。
各 Agent 节点通过读写 ``TranslationState`` 完成协作；
字段含义与 docs/architecture.md §3.1 的状态表一一对应。
"""

from typing import TypedDict

__all__ = ["TranslationState", "init_state", "DEFAULT_MAX_REWORK"]

#: 返工上限默认值；正式运行应由配置键 ``workflow.max_rework``
#: （config/settings.example.yaml）覆盖，此处仅作骨架期兜底。
DEFAULT_MAX_REWORK = 3


class TranslationState(TypedDict):
    """单章翻译流水线的共享状态。

    字段:
        work_id: 作品 ID（贯穿记忆层：术语库 / TM / 小说圣经的命名空间）。
        chapter_id: 章节 ID（一般为文件名或章节号）。
        segments: 调度 Agent 切分后的原文片段列表，按序翻译；入口写入后不变。
        glossary: 本章生效的术语映射 ``{源术语: 规范译名}``（retrieve 节点
            经术语 Agent 注入）。
        draft: 当前译稿（translate 产出初稿，edit 节点原地修订）。
        review_notes: 审校 / QA 批注列表；QA 判返工时追加结构化批注，
            作为回退到 translate 节点的返工输入。
        polished: 润色后的译稿（QA 终审对象）。
        qa_score: QA 终审质量分（由 QAAgent 按维度权重确定性计算）。
        qa_verdict: QA 终审结论：``"pass"`` 放行 / ``"rework"`` 返工
            （状态机兼容 docs 旧称 ``"fail"``）。
        rework_count: 已实际返工次数（回退到 translate 时 +1）。
        max_rework: 返工上限，取自配置 ``workflow.max_rework``，防止死循环。
    """

    work_id: str
    chapter_id: str
    segments: list[str]
    glossary: dict
    draft: str
    review_notes: list
    polished: str
    qa_score: float
    qa_verdict: str
    rework_count: int
    max_rework: int


def init_state(
    work_id: str,
    chapter_id: str,
    segments: list[str],
    *,
    max_rework: int = DEFAULT_MAX_REWORK,
) -> TranslationState:
    """构造初始状态的便捷工厂（骨架）。

    TODO: ``max_rework`` 由配置层（``workflow.max_rework``）注入后，
    调用方应显式传参，本默认值仅保留给离线联调。
    """
    return TranslationState(
        work_id=work_id,
        chapter_id=chapter_id,
        segments=list(segments),
        glossary={},
        draft="",
        review_notes=[],
        polished="",
        qa_score=0.0,
        qa_verdict="",
        rework_count=0,
        max_rework=int(max_rework),
    )
