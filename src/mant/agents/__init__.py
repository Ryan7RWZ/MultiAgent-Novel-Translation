"""mant.agents — 多智能体角色层。

对外暴露统一契约：

- ``AgentTask`` / ``AgentResult``: 全项目统一的任务与结果数据模型；
- ``BaseAgent``: 所有角色 Agent（调度/术语/翻译/审校/润色/QA 终审）的抽象基类；
- ``parse_json_output``: 模型 JSON 输出的稳健提取工具。

具体角色 Agent 由各自负责人以子模块形式加入本包（如
``mant.agents.translator``），子类约定见 ``mant.agents.base`` 模块 docstring。
"""

from mant.agents.base import (
    AgentResult,
    AgentTask,
    BaseAgent,
    parse_json_output,
)

__all__ = [
    "AgentResult",
    "AgentTask",
    "BaseAgent",
    "parse_json_output",
]
