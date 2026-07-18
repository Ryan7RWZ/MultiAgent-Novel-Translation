"""并发执行层的数据契约与配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


_DEFAULT_STAGE_WORKERS = {
    "terminology": 2,
    "translate": 4,
    "edit": 4,
    "polish": 6,
    "qa": 4,
}


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """一次章节运行的有界并发与保护配置。"""

    enabled: bool = False
    global_max_in_flight: int = 4
    stage_workers: dict[str, int] = field(
        default_factory=lambda: dict(_DEFAULT_STAGE_WORKERS)
    )
    max_segment_calls: int = 0
    max_failures: int = 0
    stage_max_failures: dict[str, int] = field(default_factory=dict)
    checkpoint_enabled: bool = False
    checkpoint_path: Path = Path("data/runtime/checkpoints.db")
    manifest_enabled: bool = False
    manifest_dir: Path = Path("data/runtime/runs")
    resume_stage: str = ""
    resume_failed_only: bool = True

    def __post_init__(self) -> None:
        if self.global_max_in_flight < 1:
            raise ValueError("global_max_in_flight 必须至少为 1")
        if self.max_segment_calls < 0:
            raise ValueError("max_segment_calls 不能小于 0")
        if self.max_failures < 0:
            raise ValueError("max_failures 不能小于 0")
        for stage, limit in self.stage_max_failures.items():
            if int(limit) < 0:
                raise ValueError(f"阶段 {stage!r} 的失败上限不能小于 0")
        for stage, workers in self.stage_workers.items():
            if int(workers) < 1:
                raise ValueError(f"阶段 {stage!r} 的并发数必须至少为 1")
        if self.resume_stage and self.resume_stage not in {
            "translate", "edit", "polish", "qa"
        }:
            raise ValueError(f"不支持从阶段 {self.resume_stage!r} 恢复")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "ExecutionConfig":
        data = dict(raw or {})
        stages = dict(_DEFAULT_STAGE_WORKERS)
        for stage, value in dict(data.get("stages") or {}).items():
            stages[str(stage)] = int(value)
        budget = dict(data.get("budget") or {})
        checkpoint = dict(data.get("checkpoint") or {})
        manifest = dict(data.get("manifest") or {})
        resume = dict(data.get("resume") or {})
        checkpoint_enabled = bool(checkpoint.get("enabled", False))
        return cls(
            enabled=bool(data.get("enabled", False)),
            global_max_in_flight=int(data.get("global_max_in_flight", 4)),
            stage_workers=stages,
            max_segment_calls=int(budget.get("max_segment_calls", 0) or 0),
            max_failures=int(budget.get("max_failures", 0) or 0),
            stage_max_failures={
                str(stage): int(limit or 0)
                for stage, limit in dict(
                    budget.get("max_failures_per_stage") or {}
                ).items()
            },
            checkpoint_enabled=checkpoint_enabled,
            checkpoint_path=Path(
                checkpoint.get("sqlite_path", "data/runtime/checkpoints.db")
            ),
            manifest_enabled=bool(manifest.get("enabled", checkpoint_enabled)),
            manifest_dir=Path(manifest.get("directory", "data/runtime/runs")),
            resume_stage=str(resume.get("stage", "") or "").strip().lower(),
            resume_failed_only=bool(resume.get("failed_only", True)),
        )

    def workers_for(self, stage: str) -> int:
        """返回阶段实际 worker 数；禁用并发时恒为 1。"""
        if not self.enabled:
            return 1
        requested = int(self.stage_workers.get(stage, 1))
        return max(1, min(requested, self.global_max_in_flight))

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "global_max_in_flight": self.global_max_in_flight,
            "stages": dict(self.stage_workers),
            "budget": {
                "max_segment_calls": self.max_segment_calls,
                "max_failures": self.max_failures,
                "max_failures_per_stage": dict(self.stage_max_failures),
            },
            "checkpoint": {
                "enabled": self.checkpoint_enabled,
                "sqlite_path": str(self.checkpoint_path),
            },
            "manifest": {
                "enabled": self.manifest_enabled,
                "directory": str(self.manifest_dir),
            },
            "resume": {
                "stage": self.resume_stage,
                "failed_only": self.resume_failed_only,
            },
        }


@dataclass(frozen=True, slots=True)
class StageTask:
    """可调度的单片、单阶段任务。"""

    run_id: str
    segment_id: str
    segment_index: int
    stage: str
    round: int
    input_hash: str


@dataclass(slots=True)
class TaskValue:
    """AgentResult 的线程/持久化安全快照。"""

    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StageTaskResult:
    """调度器对单个任务的完整结果封装。"""

    task: StageTask
    value: TaskValue
    duration_ms: float = 0.0
    from_checkpoint: bool = False
    error_type: str = ""
