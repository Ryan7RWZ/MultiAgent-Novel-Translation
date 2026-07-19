"""有界并发调度、预算保护与 checkpoint 测试。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from mant.execution import (
    CheckpointStore,
    ExecutionConfig,
    RunManifestStore,
    StageExecutor,
    StageTask,
    TaskValue,
)
from mant.observability import RunObserver, emit_event, run_context
from mant.observability.events import RunEvent


class _CollectSink:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []
        self._lock = threading.Lock()

    def __call__(self, event: RunEvent) -> None:
        with self._lock:
            self.events.append(event)

    def close(self) -> None:
        pass


def _tasks(
    count: int,
    *,
    run_id: str = "run-execution",
    stage: str = "translate",
) -> list[StageTask]:
    return [
        StageTask(
            run_id=run_id,
            segment_id=f"chapter#seg{index:04d}",
            segment_index=index,
            stage=stage,
            round=0,
            input_hash=f"{stage}-hash-{index}",
        )
        for index in range(count)
    ]


def test_concurrent_stage_is_bounded_and_merges_in_segment_order() -> None:
    config = ExecutionConfig.from_mapping(
        {
            "enabled": True,
            "global_max_in_flight": 3,
            "stages": {"translate": 3},
        }
    )
    executor = StageExecutor(config)
    active = 0
    peak = 0
    lock = threading.Lock()

    def worker(task: StageTask) -> SimpleNamespace:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            # 前面的片段更慢，确保实际完成顺序与输入顺序不同。
            time.sleep((6 - task.segment_index) * 0.004)
            return SimpleNamespace(
                ok=True,
                output={"draft": f"draft-{task.segment_index}"},
                notes=[],
            )
        finally:
            with lock:
                active -= 1

    results = executor.run("translate", reversed(_tasks(6)), worker)

    assert [item.task.segment_index for item in results] == list(range(6))
    assert [item.value.output["draft"] for item in results] == [
        f"draft-{index}" for index in range(6)
    ]
    assert 2 <= peak <= 3
    assert executor.stats()["peak_in_flight"] == 3


def test_twenty_workers_across_six_stages_preserve_order_and_checkpoint() -> None:
    stages = ("terminology", "translate", "edit", "revise", "polish", "qa")
    with TemporaryDirectory() as tmp:
        checkpoint_path = Path(tmp) / "checkpoints.db"
        executor = StageExecutor(
            ExecutionConfig.from_mapping(
                {
                    "enabled": True,
                    "global_max_in_flight": 20,
                    "stages": {stage: 20 for stage in stages},
                    "checkpoint": {
                        "enabled": True,
                        "sqlite_path": str(checkpoint_path),
                    },
                }
            )
        )
        active_by_stage = {stage: 0 for stage in stages}
        peak_by_stage = {stage: 0 for stage in stages}
        lock = threading.Lock()

        def worker(task: StageTask) -> SimpleNamespace:
            with lock:
                active_by_stage[task.stage] += 1
                peak_by_stage[task.stage] = max(
                    peak_by_stage[task.stage],
                    active_by_stage[task.stage],
                )
            try:
                time.sleep(0.01)
                return SimpleNamespace(
                    ok=True,
                    output={"value": f"{task.stage}-{task.segment_index}"},
                    notes=[],
                )
            finally:
                with lock:
                    active_by_stage[task.stage] -= 1

        for stage in stages:
            tasks = _tasks(21, run_id="run-concurrency-20", stage=stage)
            results = executor.run(stage, reversed(tasks), worker)
            assert [item.task.segment_index for item in results] == list(range(21))
            assert [item.value.output["value"] for item in results] == [
                f"{stage}-{index}" for index in range(21)
            ]
            assert executor.checkpoints is not None
            assert executor.checkpoints.counts("run-concurrency-20", stage) == {
                "success": 21,
                "failed": 0,
                "total": 21,
            }

        assert peak_by_stage == {stage: 20 for stage in stages}
        assert executor.stats() == {
            "enabled": True,
            "submitted": 21 * len(stages),
            "completed": 21 * len(stages),
            "failed": 0,
            "failed_by_stage": {},
            "rejected": 0,
            "checkpoint_hits": 0,
            "checkpoint_errors": 0,
            "peak_in_flight": 20,
            "cancelled": False,
        }


def test_context_is_copied_to_workers_and_events_keep_segment_identity() -> None:
    sink = _CollectSink()
    observer = RunObserver([sink])
    executor = StageExecutor(
        ExecutionConfig.from_mapping(
            {
                "enabled": True,
                "global_max_in_flight": 2,
                "stages": {"translate": 2},
            }
        )
    )

    def worker(task: StageTask) -> SimpleNamespace:
        emit_event("test.worker", segment_id=task.segment_id)
        return SimpleNamespace(ok=True, output={}, notes=[])

    with run_context(
        observer,
        run_id="run-context",
        work_id="demo",
        chapter_id="chapter",
    ):
        executor.run("translate", _tasks(4, run_id="run-context"), worker)
    observer.close()

    worker_events = [event for event in sink.events if event.event_type == "test.worker"]
    assert len(worker_events) == 4
    assert {event.run_id for event in worker_events} == {"run-context"}
    assert {event.segment_id for event in worker_events} == {
        f"chapter#seg{index:04d}" for index in range(4)
    }


def test_successful_checkpoint_is_reused_without_calling_worker_again() -> None:
    with TemporaryDirectory() as tmp:
        checkpoint_path = Path(tmp) / "checkpoints.db"
        config = ExecutionConfig.from_mapping(
            {
                "checkpoint": {
                    "enabled": True,
                    "sqlite_path": str(checkpoint_path),
                }
            }
        )
        calls = 0

        def worker(_: StageTask) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            return SimpleNamespace(ok=True, output={"draft": "cached"}, notes=[])

        first = StageExecutor(config).run("translate", _tasks(1), worker)
        second_executor = StageExecutor(config)
        second = second_executor.run("translate", _tasks(1), worker)

        assert calls == 1
        assert not first[0].from_checkpoint
        assert second[0].from_checkpoint
        assert second[0].value.output == {"draft": "cached"}
        assert second_executor.stats()["checkpoint_hits"] == 1


def test_checkpoint_can_report_and_read_failed_task_for_diagnostics() -> None:
    with TemporaryDirectory() as tmp:
        store = CheckpointStore(Path(tmp) / "checkpoints.db")
        task = _tasks(1, run_id="run-failed")[0]
        store.save(task, TaskValue(ok=False, notes=["expected"]))

        assert store.load(task) is None
        failed = store.load(task, successful_only=False)
        assert failed is not None
        assert not failed.ok
        assert store.counts("run-failed", "translate") == {
            "success": 0,
            "failed": 1,
            "total": 1,
        }


def test_run_manifest_round_trip_and_rejects_unsafe_run_id() -> None:
    with TemporaryDirectory() as tmp:
        store = RunManifestStore(Path(tmp) / "runs")
        path = store.save(
            run_id="run-safe_01",
            chapter_path=Path(tmp) / "chapter.txt",
            state={
                "work_id": "demo",
                "chapter_id": "1",
                "source_text": "原文",
                "segments": ["原文"],
            },
            settings={"llm": {"model": "test"}},
        )

        loaded = store.load("run-safe_01")
        assert path.is_file()
        assert loaded["source_sha256"]
        assert loaded["state"]["segments"] == ["原文"]
        with pytest.raises(ValueError):
            store.path_for("../escape")


def test_call_budget_stops_new_segment_tasks() -> None:
    executor = StageExecutor(
        ExecutionConfig.from_mapping(
            {"budget": {"max_segment_calls": 2}}
        )
    )
    calls = 0

    def worker(_: StageTask) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return SimpleNamespace(ok=True, output={}, notes=[])

    results = executor.run("translate", _tasks(4), worker)

    assert calls == 2
    assert [item.value.ok for item in results] == [True, True, False, False]
    assert [item.error_type for item in results[2:]] == [
        "BudgetExceeded",
        "BudgetExceeded",
    ]
    assert executor.stats()["rejected"] == 2


def test_checkpoint_failure_does_not_abort_translation() -> None:
    executor = StageExecutor(ExecutionConfig())

    class BrokenCheckpoint:
        def load(self, _: StageTask):
            raise OSError("simulated checkpoint failure")

        def save(self, _task: StageTask, _value: object) -> None:
            raise OSError("simulated checkpoint failure")

    executor.checkpoints = BrokenCheckpoint()  # type: ignore[assignment]
    results = executor.run(
        "translate",
        _tasks(1),
        lambda _: SimpleNamespace(ok=True, output={"draft": "safe"}, notes=[]),
    )

    assert results[0].value.output == {"draft": "safe"}
    assert executor.stats()["checkpoint_errors"] == 2


def test_failure_threshold_opens_circuit_for_new_tasks() -> None:
    executor = StageExecutor(
        ExecutionConfig.from_mapping({"budget": {"max_failures": 1}})
    )
    calls = 0

    def worker(_: StageTask) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return SimpleNamespace(ok=False, output={}, notes=["expected failure"])

    results = executor.run("translate", _tasks(3), worker)

    assert calls == 1
    assert [item.error_type for item in results[1:]] == [
        "CircuitOpen",
        "CircuitOpen",
    ]
    assert executor.stats()["failed"] == 1
    assert executor.stats()["rejected"] == 2


def test_stage_failure_limit_does_not_consume_another_stage_budget() -> None:
    executor = StageExecutor(
        ExecutionConfig.from_mapping(
            {
                "budget": {
                    "max_failures_per_stage": {"edit": 1, "qa": 1}
                }
            }
        )
    )
    edit_task = StageTask(
        run_id="run-stage-budget",
        segment_id="chapter#seg0000",
        segment_index=0,
        stage="edit",
        round=0,
        input_hash="edit-hash",
    )
    qa_task = StageTask(
        run_id="run-stage-budget",
        segment_id="chapter#seg0000",
        segment_index=0,
        stage="qa",
        round=0,
        input_hash="qa-hash",
    )

    edit = executor.run(
        "edit",
        [edit_task],
        lambda _: SimpleNamespace(ok=False, output={}, notes=[]),
    )
    qa = executor.run(
        "qa",
        [qa_task],
        lambda _: SimpleNamespace(ok=True, output={}, notes=[]),
    )

    assert not edit[0].value.ok
    assert qa[0].value.ok
    assert executor.stats()["failed_by_stage"] == {"edit": 1}


@pytest.mark.parametrize(
    "raw",
    [
        {"global_max_in_flight": 0},
        {"stages": {"translate": 0}},
        {"budget": {"max_segment_calls": -1}},
        {"budget": {"max_failures": -1}},
        {"budget": {"max_failures_per_stage": {"qa": -1}}},
    ],
)
def test_invalid_execution_limits_are_rejected(raw: dict) -> None:
    with pytest.raises(ValueError):
        ExecutionConfig.from_mapping(raw)
