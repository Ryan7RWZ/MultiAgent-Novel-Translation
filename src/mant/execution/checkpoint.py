"""片段阶段 checkpoint；每次操作独立连接，避免跨线程共享 SQLite。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .models import StageTask, TaskValue


class CheckpointStore:
    """按 run/segment/stage/round/input_hash 保存成功的 Agent 产物。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS segment_checkpoints (
                    run_id TEXT NOT NULL,
                    segment_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    round_no INTEGER NOT NULL,
                    input_hash TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    output_json TEXT NOT NULL,
                    notes_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, segment_id, stage, round_no)
                );
                CREATE INDEX IF NOT EXISTS idx_segment_checkpoint_hash
                ON segment_checkpoints(input_hash);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def load(
        self,
        task: StageTask,
        *,
        successful_only: bool = True,
    ) -> TaskValue | None:
        success_clause = " AND ok=1" if successful_only else ""
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"""
                SELECT ok, output_json, notes_json
                FROM segment_checkpoints
                WHERE run_id=? AND segment_id=? AND stage=? AND round_no=?
                  AND input_hash=?{success_clause}
                """,
                (
                    task.run_id,
                    task.segment_id,
                    task.stage,
                    task.round,
                    task.input_hash,
                ),
            ).fetchone()
        if row is None:
            return None
        try:
            output = json.loads(row[1])
            notes = json.loads(row[2])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return TaskValue(
            ok=bool(row[0]),
            output=dict(output) if isinstance(output, dict) else {},
            notes=[str(item) for item in notes] if isinstance(notes, list) else [],
        )

    def counts(self, run_id: str, stage: str | None = None) -> dict[str, int]:
        """返回一次运行的 checkpoint 成功/失败数量，不读取正文产物。"""
        where = "WHERE run_id=?"
        params: tuple[object, ...] = (run_id,)
        if stage:
            where += " AND stage=?"
            params += (stage,)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT ok, COUNT(*) FROM segment_checkpoints {where} GROUP BY ok",
                params,
            ).fetchall()
        result = {"success": 0, "failed": 0, "total": 0}
        for ok, count in rows:
            key = "success" if bool(ok) else "failed"
            result[key] = int(count)
            result["total"] += int(count)
        return result

    def save(self, task: StageTask, value: TaskValue) -> None:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO segment_checkpoints(
                    run_id, segment_id, stage, round_no, input_hash, ok,
                    output_json, notes_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, segment_id, stage, round_no) DO UPDATE SET
                    input_hash=excluded.input_hash,
                    ok=excluded.ok,
                    output_json=excluded.output_json,
                    notes_json=excluded.notes_json,
                    updated_at=excluded.updated_at
                """,
                (
                    task.run_id,
                    task.segment_id,
                    task.stage,
                    task.round,
                    task.input_hash,
                    int(value.ok),
                    json.dumps(value.output, ensure_ascii=False, default=str),
                    json.dumps(value.notes, ensure_ascii=False),
                    now,
                ),
            )
