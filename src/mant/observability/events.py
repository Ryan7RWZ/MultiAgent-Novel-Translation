"""运行期可观测事件的数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RunEvent:
    """一个可排序、可持久化的工作流事件。"""

    run_id: str
    sequence: int
    timestamp: str
    event_type: str
    work_id: str = ""
    chapter_id: str = ""
    node: str = ""
    agent: str = ""
    segment_id: str = ""
    round: int = 0
    tier: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """返回 JSON 可序列化字典。"""
        return asdict(self)
