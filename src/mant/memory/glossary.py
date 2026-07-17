"""术语库（Glossary）存储：仅使用标准库 sqlite3 实现。

设计说明：
- 表结构与 ``data/glossary/schema.sql`` 保持一致（schema.sql 是对外文档 /
  迁移基准，本模块内嵌 DDL 保证包在脱离仓库目录时仍可自建表）。
- 唯一索引 ``(source, work_id)`` 是 ``upsert`` 的冲突键：同一作品内同一
  源术语只保留一条记录，重复写入时覆盖译法 / 分类 / 置信度。
- TODO(存储切换)：后续可平滑切换到 Postgres —— 仅需将本类替换为基于
  psycopg 的实现，接口签名保持不变（MemoryHub 不感知底层差异）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .models import TermEntry

__all__ = ["GlossaryStore"]

# 建表语句：与 data/glossary/schema.sql 保持一致
_DDL = """
CREATE TABLE IF NOT EXISTS terms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    target      TEXT    NOT NULL,
    category    TEXT    NOT NULL DEFAULT '',
    work_id     TEXT    NOT NULL,
    confidence  REAL    NOT NULL DEFAULT 1.0
        CHECK (confidence BETWEEN 0.0 AND 1.0),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_terms_source_work ON terms (source, work_id);
CREATE INDEX IF NOT EXISTS idx_terms_work_id ON terms (work_id);
CREATE INDEX IF NOT EXISTS idx_terms_work_category ON terms (work_id, category);
"""


class GlossaryStore:
    """术语库存储：负责术语的写入（upsert）、精确查询（lookup）与按作品列出。

    参数:
        db_path: sqlite 数据库文件路径；父目录不存在时自动创建。
            可与 TMStore 共用同一个 sqlite 文件（不同表互不干扰）。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row  # 按列名访问查询结果
        self._ensure_schema()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _ensure_schema(self) -> None:
        """建表与索引（幂等，可重复执行）。"""
        self._conn.executescript(_DDL)
        self._conn.commit()

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> TermEntry:
        """把 sqlite 行转换为 TermEntry 数据模型。"""
        return TermEntry(
            source=row["source"],
            target=row["target"],
            category=row["category"],
            work_id=row["work_id"],
            confidence=row["confidence"],
        )

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def upsert(self, entries: Iterable[TermEntry]) -> int:
        """批量写入术语；冲突键 (source, work_id) 已存在时覆盖更新。

        参数:
            entries: TermEntry 可迭代对象。

        返回:
            实际写入（含更新）的条目数。
        """
        entries = list(entries)
        if not entries:
            return 0
        self._conn.executemany(
            """
            INSERT INTO terms (source, target, category, work_id, confidence)
            VALUES (:source, :target, :category, :work_id, :confidence)
            ON CONFLICT(source, work_id) DO UPDATE SET
                target     = excluded.target,
                category   = excluded.category,
                confidence = excluded.confidence
            """,
            [e.to_dict() for e in entries],
        )
        self._conn.commit()
        return len(entries)

    def lookup(self, terms: list[str], work_id: str) -> dict[str, TermEntry]:
        """精确查询一批术语在指定作品下的译法。

        参数:
            terms: 待查询的源语言术语列表。
            work_id: 作品 ID（术语按作品隔离）。

        返回:
            ``{源术语: TermEntry}`` 字典；未命中的术语不出现在结果中。
        """
        if not terms:
            return {}
        # 去重并保持顺序，减少 SQL 参数数量
        uniq_terms = list(dict.fromkeys(terms))
        placeholders = ",".join("?" for _ in uniq_terms)
        rows = self._conn.execute(
            f"SELECT source, target, category, work_id, confidence "
            f"FROM terms WHERE work_id = ? AND source IN ({placeholders})",
            [work_id, *uniq_terms],
        ).fetchall()
        return {row["source"]: self._row_to_entry(row) for row in rows}

    def list_by_work(self, work_id: str) -> list[TermEntry]:
        """列出指定作品下的全部术语（按 source 排序，便于导出审阅）。"""
        rows = self._conn.execute(
            "SELECT source, target, category, work_id, confidence "
            "FROM terms WHERE work_id = ? ORDER BY source",
            (work_id,),
        ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def close(self) -> None:
        """关闭底层 sqlite 连接。"""
        self._conn.close()

    # 支持 with 语法，便于脚本侧使用
    def __enter__(self) -> "GlossaryStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
