"""从 settings 配置创建一次运行所需的 Observer。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mant.observability.runtime import RunObserver
from mant.observability.sinks import JsonlSink, SqliteSink, TerminalSink


def create_observer(
    cfg: dict[str, Any] | None,
    *,
    terminal_enabled: bool | None = None,
    stream_tokens: bool | None = None,
    trace_enabled: bool | None = None,
    verbose: bool = False,
) -> RunObserver | None:
    """按配置和 CLI 覆盖项组装观测接收器；全关闭时返回 None。"""
    obs = dict(cfg or {})
    enabled = bool(obs.get("enabled", False))
    terminal_cfg = dict(obs.get("terminal") or {})
    trace_cfg = dict(obs.get("trace") or {})

    terminal_on = (
        bool(terminal_cfg.get("enabled", enabled))
        if terminal_enabled is None
        else terminal_enabled
    )
    token_on = (
        bool(terminal_cfg.get("stream_tokens", False))
        if stream_tokens is None
        else stream_tokens
    )
    trace_on = (
        bool(trace_cfg.get("enabled", enabled))
        if trace_enabled is None
        else trace_enabled
    )

    sinks = []
    if terminal_on or token_on:
        sinks.append(
            TerminalSink(
                show_tokens=token_on,
                verbose=verbose or bool(terminal_cfg.get("verbose", False)),
            )
        )
    if trace_on:
        trace_dir = Path(str(obs.get("trace_dir", "data/traces")))
        sinks.append(
            JsonlSink(
                trace_dir,
                token_batch_chars=int(trace_cfg.get("token_batch_chars", 80)),
            )
        )
        if bool(trace_cfg.get("sqlite_enabled", True)):
            sqlite_path = obs.get("sqlite_path") or trace_dir / "runs.db"
            sinks.append(SqliteSink(sqlite_path))
    return RunObserver(sinks) if sinks else None
