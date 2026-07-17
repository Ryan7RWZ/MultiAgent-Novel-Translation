"""记忆与数据层的数据模型（统一契约）。

约定：
- 一律使用标准库 dataclass（不使用 pydantic），保证在仅 stdlib + numpy
  的环境下 ``import mant.memory.models`` 必然成功。
- 每个模型提供 ``to_dict`` / ``from_dict``，用于 JSON 持久化与跨模块传递。
- 本模块中定义的字段为跨模块统一接口（agents / workflow / pipeline 均按此调用），
  修改字段前请先同步所有负责人。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["TermEntry", "TMMatch", "StoryBible"]


@dataclass
class TermEntry:
    """术语条目：一个源语言术语及其约定译法。

    属性:
        source: 源语言术语原文。
        target: 约定译法（目标语言）。
        category: 术语分类（如 人物 / 地名 / 功法 / 势力 等），默认空字符串。
        work_id: 所属作品 ID，术语库按作品隔离。
        confidence: 置信度（0~1），人工确认过的术语置为 1.0。
    """

    source: str
    target: str
    category: str = ""
    work_id: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为普通字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TermEntry":
        """从字典反序列化；缺失字段使用默认值，宽容处理脏数据。"""
        return cls(
            source=str(data.get("source", "")),
            target=str(data.get("target", "")),
            category=str(data.get("category", "")),
            work_id=str(data.get("work_id", "")),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass
class TMMatch:
    """翻译记忆库（TM）命中结果：一对相似的原文 / 译文及其匹配分。

    属性:
        source: 记忆库中的源语言句子。
        target: 记忆库中对应的目标语言译文。
        score: 相似度得分（0~1，越高越相似）。
    """

    source: str
    target: str
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为普通字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TMMatch":
        """从字典反序列化；缺失字段使用默认值。"""
        return cls(
            source=str(data.get("source", "")),
            target=str(data.get("target", "")),
            score=float(data.get("score", 0.0)),
        )


@dataclass
class StoryBible:
    """小说圣经（Story Bible）：一部作品的人物卡、世界观设定与时间线汇总。

    属性:
        work_id: 所属作品 ID。
        characters: 人物卡列表，每项为 dict，
            约定键示例：{"name", "aliases", "description", "first_seen_chapter"}。
        settings: 世界观 / 设定条目列表，每项为 dict，
            约定键示例：{"topic", "content", "source_chapter"}。
        timeline: 时间线条目列表，每项为 dict，
            约定键示例：{"chapter_id", "summary"}。
    """

    work_id: str
    characters: list[dict[str, Any]] = field(default_factory=list)
    settings: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为普通字典（供 JSON 持久化）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StoryBible":
        """从字典反序列化；缺失字段使用默认值，列表字段强制为 list。"""
        characters = data.get("characters") or []
        settings = data.get("settings") or []
        timeline = data.get("timeline") or []
        return cls(
            work_id=str(data.get("work_id", "")),
            characters=list(characters),
            settings=list(settings),
            timeline=list(timeline),
        )
