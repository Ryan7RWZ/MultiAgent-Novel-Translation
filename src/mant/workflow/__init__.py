"""mant.workflow —— 翻译主流程层（LangGraph 状态机编排）。

对外暴露：

- :class:`TranslationState` / :func:`init_state` —— 状态定义与初始化工厂
  （纯 stdlib，任何环境可直接导入）；
- :func:`build_graph` / :func:`run_chapter` —— 经 PEP 562 ``__getattr__``
  延迟自 ``mant.workflow.graph`` 导入，隔离 langgraph 等下游依赖，
  保证未安装 langgraph 时 ``import mant.workflow`` 仍然成功。
"""

from mant.workflow.state import DEFAULT_MAX_REWORK, TranslationState, init_state

__all__ = [
    "TranslationState",
    "init_state",
    "DEFAULT_MAX_REWORK",
    "build_graph",
    "run_chapter",
]


def __getattr__(name: str):
    """按需延迟导入图构建入口（PEP 562），避免 langgraph 成为硬依赖。"""
    if name in ("build_graph", "run_chapter"):
        from mant.workflow import graph

        return getattr(graph, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
