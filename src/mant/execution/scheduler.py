"""同步 LLM 链路使用的有界线程调度器。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from typing import Any, Callable, Iterable

from mant.observability import emit_event

from .checkpoint import CheckpointStore
from .models import ExecutionConfig, StageTask, StageTaskResult, TaskValue


class StageExecutor:
    """按阶段有界派发任务，并以输入顺序返回结果。"""

    def __init__(
        self,
        config: ExecutionConfig,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.cancel_event = cancel_event or threading.Event()
        self.checkpoints: CheckpointStore | None = None
        self._checkpoint_errors = 0
        if config.checkpoint_enabled:
            try:
                self.checkpoints = CheckpointStore(config.checkpoint_path)
            except Exception as exc:  # noqa: BLE001 - checkpoint 故障不得中断翻译
                self._checkpoint_errors = 1
                emit_event(
                    "checkpoint.failed",
                    payload={
                        "stage": "init",
                        "operation": "open",
                        "error": type(exc).__name__,
                    },
                )
        self._lock = threading.Lock()
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._failed_by_stage: dict[str, int] = {}
        self._rejected = 0
        self._checkpoint_hits = 0
        self._peak_in_flight = 0

    def cancel(self) -> None:
        self.cancel_event.set()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.config.enabled,
                "submitted": self._submitted,
                "completed": self._completed,
                "failed": self._failed,
                "failed_by_stage": dict(self._failed_by_stage),
                "rejected": self._rejected,
                "checkpoint_hits": self._checkpoint_hits,
                "checkpoint_errors": self._checkpoint_errors,
                "peak_in_flight": self._peak_in_flight,
                "cancelled": self.cancel_event.is_set(),
            }

    def run(
        self,
        stage: str,
        tasks: Iterable[StageTask],
        worker: Callable[[StageTask], Any],
    ) -> list[StageTaskResult]:
        """执行一批任务；无论完成顺序如何，都按 segment_index 返回。"""
        ordered = sorted(list(tasks), key=lambda item: item.segment_index)
        if len({task.segment_index for task in ordered}) != len(ordered):
            raise ValueError(f"阶段 {stage!r} 含重复 segment_index")
        if any(task.stage != stage for task in ordered):
            raise ValueError("StageTask.stage 与 run(stage=...) 不一致")
        if not ordered:
            return []

        results: dict[int, StageTaskResult] = {}
        pending: list[StageTask] = []
        for task in ordered:
            cached = self._load_checkpoint(task)
            if cached is not None:
                with self._lock:
                    self._checkpoint_hits += 1
                results[task.segment_index] = StageTaskResult(
                    task=task,
                    value=cached,
                    from_checkpoint=True,
                )
                emit_event(
                    "checkpoint.hit",
                    segment_id=task.segment_id,
                    round=task.round,
                    payload={"stage": stage, "segment_index": task.segment_index},
                )
            else:
                pending.append(task)

        workers = self.config.workers_for(stage)
        if workers == 1:
            for task in pending:
                result = self._admit_and_execute(task, worker)
                results[task.segment_index] = result
        else:
            self._run_concurrent(stage, pending, worker, workers, results)
        return [results[task.segment_index] for task in ordered]

    def _run_concurrent(
        self,
        stage: str,
        pending: list[StageTask],
        worker: Callable[[StageTask], Any],
        workers: int,
        results: dict[int, StageTaskResult],
    ) -> None:
        iterator = iter(pending)
        futures: dict[Future[StageTaskResult], StageTask] = {}
        exhausted = False
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"mant-{stage}",
        ) as pool:
            while futures or not exhausted:
                while not exhausted and len(futures) < workers:
                    try:
                        task = next(iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    blocked = self._blocked_result(task)
                    if blocked is not None:
                        results[task.segment_index] = blocked
                        continue
                    self._reserve_submission(task)
                    context = copy_context()
                    future = pool.submit(context.run, self._execute, task, worker)
                    futures[future] = task
                    with self._lock:
                        self._peak_in_flight = max(
                            self._peak_in_flight, len(futures)
                        )
                if not futures:
                    continue
                done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    task = futures.pop(future)
                    result = future.result()
                    results[task.segment_index] = result

    def _admit_and_execute(
        self,
        task: StageTask,
        worker: Callable[[StageTask], Any],
    ) -> StageTaskResult:
        blocked = self._blocked_result(task)
        if blocked is not None:
            return blocked
        self._reserve_submission(task)
        with self._lock:
            self._peak_in_flight = max(self._peak_in_flight, 1)
        return self._execute(task, worker)

    def _blocked_result(self, task: StageTask) -> StageTaskResult | None:
        if self.cancel_event.is_set():
            with self._lock:
                self._rejected += 1
            return self._synthetic_failure(task, "Cancelled", "运行已取消，任务未执行")
        with self._lock:
            over_calls = (
                self.config.max_segment_calls > 0
                and self._submitted >= self.config.max_segment_calls
            )
            circuit_open = (
                self.config.max_failures > 0
                and self._failed >= self.config.max_failures
            )
            stage_limit = int(
                self.config.stage_max_failures.get(task.stage, 0) or 0
            )
            stage_failures = self._failed_by_stage.get(task.stage, 0)
            stage_circuit_open = stage_limit > 0 and stage_failures >= stage_limit
        if over_calls:
            with self._lock:
                self._rejected += 1
            emit_event(
                "budget.exhausted",
                segment_id=task.segment_id,
                round=task.round,
                payload={"stage": task.stage, "limit": "max_segment_calls"},
            )
            return self._synthetic_failure(
                task, "BudgetExceeded", "已达到片段调用预算，任务未执行"
            )
        if circuit_open or stage_circuit_open:
            with self._lock:
                self._rejected += 1
            emit_event(
                "circuit.opened",
                segment_id=task.segment_id,
                round=task.round,
                payload={
                    "stage": task.stage,
                    "failures": stage_failures if stage_circuit_open else self._failed,
                    "scope": "stage" if stage_circuit_open else "global",
                },
            )
            return self._synthetic_failure(
                task, "CircuitOpen", "失败数量达到熔断阈值，任务未执行"
            )
        return None

    def _reserve_submission(self, task: StageTask) -> None:
        with self._lock:
            self._submitted += 1
        emit_event(
            "task.queued",
            segment_id=task.segment_id,
            round=task.round,
            payload={
                "stage": task.stage,
                "segment_index": task.segment_index,
            },
        )

    def _execute(
        self,
        task: StageTask,
        worker: Callable[[StageTask], Any],
    ) -> StageTaskResult:
        started = time.perf_counter()
        emit_event(
            "task.started",
            segment_id=task.segment_id,
            round=task.round,
            payload={"stage": task.stage, "segment_index": task.segment_index},
        )
        error_type = ""
        try:
            raw = worker(task)
            value = TaskValue(
                ok=bool(getattr(raw, "ok", False)),
                output=dict(getattr(raw, "output", {}) or {}),
                notes=[str(item) for item in (getattr(raw, "notes", []) or [])],
            )
        except Exception as exc:  # noqa: BLE001 - 调度边界隔离单片异常
            error_type = type(exc).__name__
            value = TaskValue(
                ok=False,
                notes=[f"并发任务未捕获异常已隔离：{exc!r}"],
            )
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        with self._lock:
            self._completed += 1
            if not value.ok:
                self._failed += 1
                self._failed_by_stage[task.stage] = (
                    self._failed_by_stage.get(task.stage, 0) + 1
                )
        self._save_checkpoint(task, value)
        emit_event(
            "task.completed" if value.ok else "task.failed",
            segment_id=task.segment_id,
            round=task.round,
            payload={
                "stage": task.stage,
                "segment_index": task.segment_index,
                "error": error_type,
            },
            metrics={"duration_ms": duration_ms},
        )
        return StageTaskResult(
            task=task,
            value=value,
            duration_ms=duration_ms,
            error_type=error_type,
        )

    def _load_checkpoint(self, task: StageTask) -> TaskValue | None:
        if self.checkpoints is None:
            return None
        if self.config.resume_stage == "revise" and task.stage in {
            "revise",
            "polish",
            "qa",
        }:
            # Revision 定向恢复的语义是一条完整的新验证链：选中的片段必须
            # 重新经过 Revise → Polish → QA，不能命中旧的下游产物。
            return None
        if (
            self.config.resume_stage == task.stage
            and not self.config.resume_failed_only
        ):
            return None
        try:
            cached = self.checkpoints.load(task)
            if (
                cached is not None
                and self.config.resume_stage == "qa"
                and self.config.resume_failed_only
                and str(cached.output.get("qa_verdict") or "").strip().lower()
                in {"rework", "fail"}
            ):
                return None
            return cached
        except Exception as exc:  # noqa: BLE001 - 观测/缓存不阻断业务
            with self._lock:
                self._checkpoint_errors += 1
            emit_event(
                "checkpoint.failed",
                segment_id=task.segment_id,
                round=task.round,
                payload={
                    "stage": task.stage,
                    "operation": "load",
                    "error": type(exc).__name__,
                },
            )
            return None

    def _save_checkpoint(self, task: StageTask, value: TaskValue) -> None:
        if self.checkpoints is None:
            return
        try:
            self.checkpoints.save(task, value)
        except Exception as exc:  # noqa: BLE001 - checkpoint 故障不得中断翻译
            with self._lock:
                self._checkpoint_errors += 1
            emit_event(
                "checkpoint.failed",
                segment_id=task.segment_id,
                round=task.round,
                payload={
                    "stage": task.stage,
                    "operation": "save",
                    "error": type(exc).__name__,
                },
            )
            return
        emit_event(
            "checkpoint.saved",
            segment_id=task.segment_id,
            round=task.round,
            payload={"stage": task.stage, "ok": value.ok},
        )

    @staticmethod
    def _synthetic_failure(
        task: StageTask,
        error_type: str,
        note: str,
    ) -> StageTaskResult:
        return StageTaskResult(
            task=task,
            value=TaskValue(ok=False, notes=[note]),
            error_type=error_type,
        )
