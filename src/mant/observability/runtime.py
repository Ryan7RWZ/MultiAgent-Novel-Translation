"""事件总线、运行上下文与统一发射入口。"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Iterator, Protocol

from mant.observability.events import RunEvent


class EventSink(Protocol):
    """同步事件接收器契约。"""

    def __call__(self, event: RunEvent) -> None: ...

    def close(self) -> None: ...


class EventBus:
    """进程内线程安全发布/订阅总线；单个接收器故障不会阻断翻译。"""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[RunEvent], None]] = []
        self._lock = threading.RLock()
        self.errors: list[str] = []

    def subscribe(self, subscriber: Callable[[RunEvent], None]) -> None:
        with self._lock:
            self._subscribers.append(subscriber)

    def publish(self, event: RunEvent) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception as exc:  # noqa: BLE001 - 观测不能打断业务链路
                self.errors.append(f"{type(subscriber).__name__}: {exc!r}")


class RunObserver:
    """持有事件序号、总线和持久化/展示接收器。"""

    def __init__(self, sinks: list[EventSink] | None = None) -> None:
        self.bus = EventBus()
        self._sequence = 0
        self._lock = threading.Lock()
        self._sinks = list(sinks or [])
        self.last_run_id = ""
        for sink in self._sinks:
            self.bus.subscribe(sink)

    def next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def close(self) -> None:
        for sink in reversed(self._sinks):
            try:
                sink.close()
            except Exception as exc:  # noqa: BLE001
                self.bus.errors.append(f"close {type(sink).__name__}: {exc!r}")


@dataclass(frozen=True, slots=True)
class _RunContext:
    observer: RunObserver
    run_id: str
    fields: dict[str, Any] = field(default_factory=dict)


_CURRENT_CONTEXT: ContextVar[_RunContext | None] = ContextVar(
    "mant_observability_context", default=None
)


def new_run_id() -> str:
    """生成便于按时间辨认且几乎不会冲突的运行 ID。"""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:8]}"


@contextmanager
def run_context(
    observer: RunObserver | None,
    *,
    run_id: str | None = None,
    **fields: Any,
) -> Iterator[str]:
    """建立一次运行的 ContextVar 上下文，供图节点、Agent 和 LLM 自动继承。"""
    active_run_id = run_id or new_run_id()
    if observer is None:
        yield active_run_id
        return
    observer.last_run_id = active_run_id
    token = _CURRENT_CONTEXT.set(
        _RunContext(observer=observer, run_id=active_run_id, fields=dict(fields))
    )
    try:
        yield active_run_id
    finally:
        _CURRENT_CONTEXT.reset(token)


@contextmanager
def event_scope(**fields: Any) -> Iterator[None]:
    """为当前事件上下文临时补充 node/agent/segment/round/tier 等字段。"""
    current = _CURRENT_CONTEXT.get()
    if current is None:
        yield
        return
    merged = dict(current.fields)
    merged.update({key: value for key, value in fields.items() if value is not None})
    token = _CURRENT_CONTEXT.set(
        _RunContext(current.observer, current.run_id, merged)
    )
    try:
        yield
    finally:
        _CURRENT_CONTEXT.reset(token)


def emit_event(
    event_type: str,
    *,
    payload: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    **fields: Any,
) -> RunEvent | None:
    """向当前运行发射类型化事件；未启用观测时为零副作用 no-op。"""
    current = _CURRENT_CONTEXT.get()
    if current is None:
        return None
    values = dict(current.fields)
    values.update({key: value for key, value in fields.items() if value is not None})
    event = RunEvent(
        run_id=current.run_id,
        sequence=current.observer.next_sequence(),
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        event_type=event_type,
        work_id=str(values.get("work_id", "")),
        chapter_id=str(values.get("chapter_id", "")),
        node=str(values.get("node", "")),
        agent=str(values.get("agent", "")),
        segment_id=str(values.get("segment_id", "")),
        round=int(values.get("round", 0) or 0),
        tier=str(values.get("tier", "")),
        payload=dict(payload or {}),
        metrics=dict(metrics or {}),
    )
    current.observer.bus.publish(event)
    return event
