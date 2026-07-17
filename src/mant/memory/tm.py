"""翻译记忆库（TM, Translation Memory）存储。

设计说明：
- 句对（源文 / 译文）存放在 sqlite 表 ``tm_pairs`` 中，按 work_id 隔离；
  可与 GlossaryStore 共用同一个 sqlite 文件（不同表互不干扰）。
- ``search`` 当前为**模糊匹配占位实现**：基于标准库 difflib 计算字符串
  相似度取 top-k，保证骨架阶段全流程可跑通。
- TODO(检索升级)：替换为 VectorStore / FAISS 向量检索（见 vectorstore.py），
  即“句对写入时同步写向量库，查询时按 embedding 最近邻召回，
  再用 difflib 做精排”的两段式方案。
"""

from __future__ import annotations

import sqlite3
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from .models import TMMatch

__all__ = ["TMStore"]

_DDL = """
CREATE TABLE IF NOT EXISTS tm_pairs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id     TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    target      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tm_pairs_work_id ON tm_pairs (work_id);
"""


class TMStore:
    """翻译记忆库存储：负责句对写入（add_pairs）与模糊检索（search）。

    参数:
        db_path: sqlite 数据库文件路径；父目录不存在时自动创建。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _ensure_schema(self) -> None:
        """建表与索引（幂等）。"""
        self._conn.executescript(_DDL)
        self._conn.commit()

    @staticmethod
    def _normalize_pair(pair: tuple[str, str] | dict) -> tuple[str, str]:
        """把 (source, target) 元组或 {"source", "target"} 字典统一为元组。"""
        if isinstance(pair, dict):
            return str(pair.get("source", "")), str(pair.get("target", ""))
        source, target = pair
        return str(source), str(target)

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """占位相似度：difflib 序列匹配比率（0~1）。"""
        return SequenceMatcher(None, a, b).ratio()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def add_pairs(
        self, pairs: Iterable[tuple[str, str] | dict], work_id: str
    ) -> int:
        """批量写入句对。

        参数:
            pairs: ``(源文, 译文)`` 元组或 ``{"source", "target"}`` 字典的可迭代对象。
            work_id: 作品 ID（句对按作品隔离）。

        返回:
            实际写入的句对数。
        """
        rows = [self._normalize_pair(p) for p in pairs]
        # 过滤空句对（源文或译文为空无检索价值）
        rows = [(s, t) for s, t in rows if s and t]
        if not rows:
            return 0
        self._conn.executemany(
            "INSERT INTO tm_pairs (work_id, source, target) VALUES (?, ?, ?)",
            [(work_id, s, t) for s, t in rows],
        )
        self._conn.commit()
        return len(rows)

    def search(self, source_text: str, work_id: str, k: int = 5) -> list[TMMatch]:
        """模糊检索与 source_text 最相似的历史句对（占位实现）。

        参数:
            source_text: 待翻译的源语言句子。
            work_id: 作品 ID。
            k: 返回的最大命中数。

        返回:
            按相似度降序的 TMMatch 列表；库为空时返回空列表。

        TODO(检索升级)：全表扫描 + difflib 仅适合小规模骨架验证；
        语料增长后切换为向量召回 + 精排（见模块 docstring）。
        """
        rows = self._conn.execute(
            "SELECT source, target FROM tm_pairs WHERE work_id = ?",
            (work_id,),
        ).fetchall()
        scored = [
            TMMatch(
                source=row["source"],
                target=row["target"],
                score=self._similarity(source_text, row["source"]),
            )
            for row in rows
        ]
        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:k]

    def close(self) -> None:
        """关闭底层 sqlite 连接。"""
        self._conn.close()

    # 支持 with 语法
    def __enter__(self) -> "TMStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
