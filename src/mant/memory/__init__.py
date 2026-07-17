"""mant.memory —— 记忆与数据层门面（MemoryHub）。

聚合四个底层存储，对上（agents / workflow / pipeline）暴露统一接口：

- :class:`GlossaryStore`   术语库（sqlite，可切 Postgres）
- :class:`TMStore`         翻译记忆库（sqlite 句对）
- :class:`StoryBibleStore` 小说圣经（JSON 文件）
- :class:`VectorStore`     向量检索（FAISS 优先，numpy 降级）

统一接口签名（跨模块契约，勿擅自修改）：
``lookup_terms / search_tm / get_story_bible / record_terms / update_story_bible``

构造方式：直接传入各 store 实例（便于测试注入），或传配置字典由门面
代为构建。配置键（取自 config/settings.example.yaml 的 memory 节）：
``sqlite_path`` / ``faiss_index_dir`` / ``story_bible_dir``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .glossary import GlossaryStore
from .models import StoryBible, TermEntry, TMMatch
from .story_bible import StoryBibleStore
from .tm import TMStore
from .vectorstore import VectorHit, VectorStore

__all__ = [
    "MemoryHub",
    "GlossaryStore",
    "StoryBibleStore",
    "TMStore",
    "VectorStore",
    "VectorHit",
    "TermEntry",
    "TMMatch",
    "StoryBible",
]

# 各 store 未显式注入且配置缺失时的默认落盘位置
_DEFAULTS = {
    "sqlite_path": "data/memory.sqlite3",
    "faiss_index_dir": "data/faiss",
    "story_bible_dir": "data/story_bible",
}


class MemoryHub:
    """记忆层门面：按约定签名聚合四个 store。

    参数:
        glossary: 术语库实例；为 None 时按 config 构建。
        tm: 翻译记忆库实例；为 None 时按 config 构建。
        story_bible: 小说圣经实例；为 None 时按 config 构建。
        vector: 向量库实例；为 None 时按 config 构建。
        config: 配置字典（完整 settings 或其中的 memory 节均可，
            门面会自动下钻到 memory 节）。
    """

    def __init__(
        self,
        glossary: Optional[GlossaryStore] = None,
        tm: Optional[TMStore] = None,
        story_bible: Optional[StoryBibleStore] = None,
        vector: Optional[VectorStore] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        mem_cfg = self._extract_memory_config(config)
        sqlite_path = Path(mem_cfg.get("sqlite_path", _DEFAULTS["sqlite_path"]))

        # 术语库与 TM 共用同一个 sqlite 文件（不同表）
        self.glossary = glossary or GlossaryStore(sqlite_path)
        self.tm = tm or TMStore(sqlite_path)
        self.story_bible = story_bible or StoryBibleStore(
            mem_cfg.get("story_bible_dir", _DEFAULTS["story_bible_dir"])
        )
        # TODO(注入 embedding)：当前 VectorStore 使用占位哈希向量；
        # 接入真实 embedding 模型后应在此处通过 embed_fn 注入。
        self.vector = vector or VectorStore(
            index_dir=mem_cfg.get("faiss_index_dir", _DEFAULTS["faiss_index_dir"])
        )

    @staticmethod
    def _extract_memory_config(config: Optional[dict[str, Any]]) -> dict[str, Any]:
        """从完整 settings 或 memory 节中提取 memory 配置（两种形态兼容）。"""
        if not config:
            return {}
        if "memory" in config and isinstance(config["memory"], dict):
            return config["memory"]
        return config

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "MemoryHub":
        """便捷构造：等价于 MemoryHub(config=config)。"""
        return cls(config=config)

    # ------------------------------------------------------------------
    # 统一对外接口（跨模块契约）
    # ------------------------------------------------------------------
    def lookup_terms(self, terms: list[str], work_id: str) -> dict[str, TermEntry]:
        """批量查询术语在指定作品下的约定译法；未命中术语不出现在结果中。"""
        return self.glossary.lookup(terms, work_id)

    def match_terms(self, source_text: str, work_id: str) -> dict[str, TermEntry]:
        """直接匹配原文中已入库的术语，不依赖 LLM 先抽取候选。"""
        return self.glossary.match_text(source_text, work_id)

    def search_tm(self, source_text: str, work_id: str, k: int = 5) -> list[TMMatch]:
        """检索翻译记忆库中与 source_text 最相似的 k 条历史句对。

        TODO(TM 升级)：当前为 difflib 占位模糊匹配；后续叠加
        VectorStore 向量召回，形成“向量召回 + 精排”的两段式检索。
        """
        return self.tm.search(source_text, work_id, k=k)

    def get_story_bible(self, work_id: str) -> StoryBible:
        """获取指定作品的小说圣经（不存在时返回空 StoryBible）。"""
        return self.story_bible.load(work_id)

    def record_terms(self, entries: list[TermEntry]) -> None:
        """写入 / 覆盖一批术语条目（冲突键 source+work_id 已存在时更新）。"""
        self.glossary.upsert(entries)

    def update_story_bible(self, work_id: str, chapter_id: str, summary: str) -> None:
        """把一章的摘要增量合并进小说圣经（骨架：仅追加时间线，详见 store TODO）。"""
        self.story_bible.merge_chapter_summary(work_id, chapter_id, summary)

    # ------------------------------------------------------------------
    # 资源管理
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭持有 sqlite 连接的 store。"""
        self.glossary.close()
        self.tm.close()

    def __enter__(self) -> "MemoryHub":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
