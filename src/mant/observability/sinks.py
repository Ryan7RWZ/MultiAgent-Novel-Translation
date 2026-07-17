"""终端、JSONL 与 SQLite 事件接收器。"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

from mant.observability.events import RunEvent

_SECRET_KEY = re.compile(
    r"(^|_)(api_?key|authorization|password|secret|access_?token|refresh_?token)($|_)",
    re.IGNORECASE,
)


def redact(value: Any, *, key: str = "") -> Any:
    """递归清除常见密钥字段，同时保留 token 数量等无敏感度量。"""
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


class JsonlSink:
    """按 run_id 写原始事件；细碎 token 会合批以控制 I/O 与 SSE 压力。"""

    def __init__(self, trace_dir: str | Path, *, token_batch_chars: int = 80) -> None:
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.token_batch_chars = max(1, int(token_batch_chars))
        self._lock = threading.RLock()
        self._files: dict[str, TextIO] = {}
        self._token_batches: dict[tuple[str, str], RunEvent] = {}

    def _file(self, run_id: str) -> TextIO:
        handle = self._files.get(run_id)
        if handle is None:
            path = self.trace_dir / f"{run_id}.jsonl"
            handle = path.open("a", encoding="utf-8", newline="\n")
            self._files[run_id] = handle
        return handle

    def _write(self, event: RunEvent) -> None:
        handle = self._file(event.run_id)
        handle.write(json.dumps(redact(event.to_dict()), ensure_ascii=False) + "\n")
        handle.flush()

    def _flush_token(self, key: tuple[str, str]) -> None:
        event = self._token_batches.pop(key, None)
        if event is not None:
            self._write(event)

    def __call__(self, event: RunEvent) -> None:
        with self._lock:
            call_id = str(event.payload.get("call_id", ""))
            key = (event.run_id, call_id)
            if event.event_type == "llm.token":
                existing = self._token_batches.get(key)
                if existing is None:
                    self._token_batches[key] = event
                else:
                    payload = dict(existing.payload)
                    payload["delta"] = str(payload.get("delta", "")) + str(
                        event.payload.get("delta", "")
                    )
                    self._token_batches[key] = RunEvent(
                        run_id=event.run_id,
                        sequence=event.sequence,
                        timestamp=event.timestamp,
                        event_type=event.event_type,
                        work_id=event.work_id,
                        chapter_id=event.chapter_id,
                        node=event.node,
                        agent=event.agent,
                        segment_id=event.segment_id,
                        round=event.round,
                        tier=event.tier,
                        payload=payload,
                        metrics=event.metrics,
                    )
                batched = self._token_batches[key]
                if len(str(batched.payload.get("delta", ""))) >= self.token_batch_chars:
                    self._flush_token(key)
                return
            if call_id:
                self._flush_token(key)
            self._write(event)

    def close(self) -> None:
        with self._lock:
            for key in list(self._token_batches):
                self._flush_token(key)
            for handle in self._files.values():
                handle.close()
            self._files.clear()


class SqliteSink:
    """保存可查询的运行摘要和非 token 事件。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                summary_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS run_events (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                node TEXT NOT NULL,
                agent TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                round INTEGER NOT NULL,
                tier TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_run_events_type
                ON run_events(run_id, event_type);
            """
        )
        self._conn.commit()

    def __call__(self, event: RunEvent) -> None:
        if event.event_type == "llm.token":
            return
        safe_payload = redact(event.payload)
        safe_metrics = redact(event.metrics)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO run_events
                (run_id, sequence, timestamp, event_type, node, agent, segment_id,
                 round, tier, payload_json, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.run_id,
                    event.sequence,
                    event.timestamp,
                    event.event_type,
                    event.node,
                    event.agent,
                    event.segment_id,
                    event.round,
                    event.tier,
                    json.dumps(safe_payload, ensure_ascii=False),
                    json.dumps(safe_metrics, ensure_ascii=False),
                ),
            )
            if event.event_type == "run.started":
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO runs
                    (run_id, work_id, chapter_id, started_at, completed_at, status, summary_json)
                    VALUES (?, ?, ?, ?, NULL, 'running', '{}')
                    """,
                    (event.run_id, event.work_id, event.chapter_id, event.timestamp),
                )
            elif event.event_type in {"run.completed", "run.failed"}:
                status = "completed" if event.event_type == "run.completed" else "failed"
                summary = {"payload": safe_payload, "metrics": safe_metrics}
                self._conn.execute(
                    """
                    UPDATE runs SET completed_at = ?, status = ?, summary_json = ?
                    WHERE run_id = ?
                    """,
                    (
                        event.timestamp,
                        status,
                        json.dumps(summary, ensure_ascii=False),
                        event.run_id,
                    ),
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class TerminalSink:
    """面向人的实时终端视图；--stream 时原样输出每个 Agent 的 LLM 增量。"""

    _ICONS = {
        "run.started": "[RUN]",
        "run.completed": "[OK]",
        "run.failed": "[FAIL]",
        "node.started": "[NODE>]",
        "node.completed": "[NODE<]",
        "agent.started": "[AGENT>]",
        "agent.completed": "[AGENT<]",
        "agent.failed": "[AGENT!]",
        "llm.retry": "[RETRY]",
        "llm.failed": "[LLM!]",
        "workflow.route": "[ROUTE]",
    }

    def __init__(
        self,
        *,
        show_tokens: bool = False,
        verbose: bool = False,
        stream: TextIO | None = None,
    ) -> None:
        self.show_tokens = show_tokens
        self.verbose = verbose
        self.stream = stream or sys.stdout
        self._lock = threading.RLock()
        self._open_call = ""

    def _line(self, text: str) -> None:
        if self._open_call:
            print(file=self.stream)
            self._open_call = ""
        print(text, file=self.stream, flush=True)

    def __call__(self, event: RunEvent) -> None:
        with self._lock:
            label = event.agent or event.node or "workflow"
            if event.event_type == "llm.token":
                if not self.show_tokens:
                    return
                call_id = str(event.payload.get("call_id", ""))
                if self._open_call != call_id:
                    if self._open_call:
                        print(file=self.stream)
                    print(
                        f"\n[{label} · {event.tier or 'default'}] ",
                        end="",
                        file=self.stream,
                        flush=True,
                    )
                    self._open_call = call_id
                print(
                    str(event.payload.get("delta", "")),
                    end="",
                    file=self.stream,
                    flush=True,
                )
                return
            if event.event_type == "llm.completed":
                if self._open_call == str(event.payload.get("call_id", "")):
                    print(file=self.stream, flush=True)
                    self._open_call = ""
                if not self.verbose:
                    return
            if event.event_type == "llm.started" and not self.verbose:
                return
            if event.event_type in {"node.started", "node.completed"} and not self.verbose:
                return
            if event.event_type == "agent.completed":
                ok = event.payload.get("ok", True)
                duration = float(event.metrics.get("duration_ms", 0.0))
                self._line(f"[AGENT<] [{label}] {'完成' if ok else '降级'} · {duration:.0f} ms")
                return
            if event.event_type == "run.completed":
                duration = float(event.metrics.get("duration_ms", 0.0))
                verdict = str(event.payload.get("qa_verdict", ""))
                self._line(
                    f"[OK] [workflow] 运行完成 · {duration / 1000:.1f} s"
                    + (f" · QA={verdict}" if verdict else "")
                )
                return
            icon = self._ICONS.get(event.event_type, "·")
            detail = ""
            if event.event_type == "workflow.route":
                detail = f" → {event.payload.get('route', '')}"
            elif event.event_type == "llm.retry":
                detail = f" · 第 {event.payload.get('attempt', '?')} 次重试"
            elif event.event_type in {"run.failed", "agent.failed", "llm.failed"}:
                detail = f" · {event.payload.get('error', '')}"
            self._line(f"{icon} [{label}] {event.event_type}{detail}")

    def close(self) -> None:
        with self._lock:
            if self._open_call:
                print(file=self.stream, flush=True)
                self._open_call = ""
