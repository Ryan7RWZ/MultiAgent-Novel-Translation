"""统一事件流、LLM 流式输出与追踪落盘测试。"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mant.llm.client import LLMClient
from mant.observability import RunObserver, run_context
from mant.observability.events import RunEvent
from mant.observability.dashboard import DASHBOARD_HTML, TranslationJobManager, _safe_id
from mant.observability.sinks import JsonlSink, redact
from mant.workflow.graph import run_chapter
from tests.test_workflow import ScriptedLLM


class CollectSink:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def __call__(self, event: RunEvent) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


class FakeCompletions:
    def __init__(self) -> None:
        self.request: dict = {}

    def create(self, **kwargs):
        self.request = kwargs
        return iter(
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello "))],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="world"))],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[],
                    usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2),
                ),
            ]
        )


class PartialThenCompleteCompletions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **_):
        self.calls += 1
        if self.calls == 1:
            def interrupted():
                yield SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content="discard me"),
                        finish_reason=None,
                    )],
                    usage=None,
                )
                raise TimeoutError("simulated partial stream timeout")

            return interrupted()
        return iter(
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content="complete result"),
                        finish_reason=None,
                    )],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=None),
                        finish_reason="stop",
                    )],
                    usage=None,
                ),
            ]
        )


class TruncatedThenCompleteCompletions(PartialThenCompleteCompletions):
    def create(self, **_):
        self.calls += 1
        if self.calls == 1:
            return iter(
                [
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            delta=SimpleNamespace(content="truncated"),
                            finish_reason=None,
                        )],
                        usage=None,
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            delta=SimpleNamespace(content=None),
                            finish_reason="length",
                        )],
                        usage=None,
                    ),
                ]
            )
        return iter(
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content="complete result"),
                        finish_reason="stop",
                    )],
                    usage=None,
                )
            ]
        )


class AlwaysPartialCompletions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **_):
        self.calls += 1

        def interrupted():
            yield SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="never accept"),
                    finish_reason=None,
                )],
                usage=None,
            )
            raise TimeoutError("simulated repeated timeout")

        return interrupted()


class TestObservability(unittest.TestCase):
    def test_llm_stream_yields_deltas_and_emits_typed_events(self) -> None:
        client = LLMClient.from_config(
            {
                "llm": {
                    "providers": {
                        "fast": {"model": "fake", "api_key": "not-a-real-key", "max_retries": 0}
                    }
                }
            }
        )
        completions = FakeCompletions()
        fake_openai = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        client._build_openai_client = lambda _: fake_openai  # type: ignore[method-assign]
        sink = CollectSink()
        observer = RunObserver([sink])

        with run_context(observer, run_id="run-test", work_id="demo", chapter_id="1"):
            chunks = list(client.stream_complete("system", "user", max_tokens=20))
        observer.close()

        self.assertEqual(chunks, ["Hello ", "world"])
        self.assertTrue(completions.request["stream"])
        self.assertEqual(client.total_prompt_tokens, 7)
        self.assertEqual(client.total_completion_tokens, 2)
        event_types = [event.event_type for event in sink.events]
        self.assertEqual(event_types[0], "llm.started")
        self.assertEqual(event_types.count("llm.token"), 2)
        self.assertEqual(event_types[-1], "llm.completed")
        self.assertEqual([event.sequence for event in sink.events], list(range(1, 5)))

    def test_complete_discards_partial_stream_and_retries_from_scratch(self) -> None:
        client = LLMClient.from_config(
            {"llm": {"providers": {"fast": {
                "model": "fake",
                "api_key": "not-a-real-key",
                "max_retries": 0,
                "partial_retries": 1,
            }}}}
        )
        completions = PartialThenCompleteCompletions()
        client._build_openai_client = lambda _: SimpleNamespace(  # type: ignore[method-assign]
            chat=SimpleNamespace(completions=completions)
        )
        sink = CollectSink()
        observer = RunObserver([sink])
        with run_context(observer, run_id="partial-retry", work_id="demo", chapter_id="1"):
            result = client.complete("system", "user")
        observer.close()

        self.assertEqual(result, "complete result")
        self.assertEqual(completions.calls, 2)
        self.assertFalse(client.last_call_incomplete)
        self.assertTrue(any("已丢弃" in note for note in client.last_notes))
        event_types = [event.event_type for event in sink.events]
        self.assertEqual(event_types.count("llm.failed"), 1)
        self.assertEqual(event_types.count("llm.retry"), 1)
        self.assertEqual(event_types.count("llm.completed"), 1)

    def test_complete_retries_finish_reason_length(self) -> None:
        client = LLMClient.from_config(
            {"llm": {"providers": {"fast": {
                "model": "fake",
                "api_key": "not-a-real-key",
                "max_retries": 0,
                "partial_retries": 1,
            }}}}
        )
        completions = TruncatedThenCompleteCompletions()
        client._build_openai_client = lambda _: SimpleNamespace(  # type: ignore[method-assign]
            chat=SimpleNamespace(completions=completions)
        )
        result = client.complete("system", "user", max_tokens=10)
        self.assertEqual(result, "complete result")
        self.assertEqual(completions.calls, 2)
        self.assertTrue(any("达到 max_tokens=10" in note for note in client.last_notes))

    def test_complete_returns_empty_when_partial_retry_is_exhausted(self) -> None:
        client = LLMClient.from_config(
            {"llm": {"providers": {"fast": {
                "model": "fake",
                "api_key": "not-a-real-key",
                "max_retries": 0,
                "partial_retries": 1,
            }}}}
        )
        completions = AlwaysPartialCompletions()
        client._build_openai_client = lambda _: SimpleNamespace(  # type: ignore[method-assign]
            chat=SimpleNamespace(completions=completions)
        )
        result = client.complete("system", "user")
        self.assertEqual(result, "")
        self.assertEqual(completions.calls, 2)
        self.assertTrue(client.last_call_incomplete)
        self.assertTrue(any("重试耗尽" in note for note in client.last_notes))

    def test_jsonl_batches_tokens_and_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sink = JsonlSink(tmp, token_batch_chars=100)
            common = {
                "run_id": "run-redact",
                "timestamp": "2026-01-01T00:00:00Z",
                "event_type": "llm.token",
            }
            sink(
                RunEvent(
                    **common,
                    sequence=1,
                    payload={"call_id": "c1", "delta": "A", "api_key": "secret"},
                )
            )
            sink(
                RunEvent(
                    **common,
                    sequence=2,
                    payload={"call_id": "c1", "delta": "B"},
                )
            )
            sink(
                RunEvent(
                    run_id="run-redact",
                    sequence=3,
                    timestamp="2026-01-01T00:00:01Z",
                    event_type="llm.completed",
                    payload={"call_id": "c1"},
                )
            )
            sink.close()
            rows = [
                json.loads(line)
                for line in (Path(tmp) / "run-redact.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
        self.assertEqual(rows[0]["payload"]["delta"], "AB")
        self.assertEqual(rows[0]["payload"]["api_key"], "[REDACTED]")
        self.assertEqual(rows[1]["event_type"], "llm.completed")
        self.assertEqual(redact({"completion_tokens": 3})["completion_tokens"], 3)

    def test_workflow_reports_each_agent_and_rework_route(self) -> None:
        sink = CollectSink()
        observer = RunObserver([sink])
        with tempfile.TemporaryDirectory() as tmp:
            chapter = Path(tmp) / "chapter.txt"
            chapter.write_text("他拔出了剑。\n他拔出了剑。", encoding="utf-8")
            final = run_chapter(
                "demo",
                chapter,
                ScriptedLLM(),  # type: ignore[arg-type]
                None,
                chapter_id="1",
                max_rework=2,
                observer=observer,
                run_id="run-workflow",
            )
        observer.close()
        agents = {event.agent for event in sink.events if event.event_type == "agent.completed"}
        self.assertTrue(
            {
                "orchestrator",
                "terminologist",
                "translator",
                "editor",
                "polisher",
                "qa",
            }.issubset(agents)
        )
        routes = [
            event.payload.get("route")
            for event in sink.events
            if event.event_type == "workflow.route"
        ]
        self.assertEqual(routes, ["rework", "end"])
        self.assertEqual(final["qa_verdict"], "pass")
        self.assertTrue(
            any(event.event_type == "segmentation.completed" for event in sink.events)
        )
        self.assertEqual(final["source_text"], "他拔出了剑。\n他拔出了剑。")
        self.assertTrue(final["segmentation_stats"]["reconstruction_ok"])
        self.assertEqual(len(final["draft_segments"]), len(final["segments"]))
        self.assertEqual(len(final["polished_segments"]), len(final["segments"]))
        self.assertTrue(
            any(event.event_type == "qa.aggregated" for event in sink.events)
        )
        self.assertEqual(sink.events[0].event_type, "run.started")
        self.assertEqual(sink.events[-1].event_type, "run.completed")

    def test_browser_job_uses_formal_cli_and_returns_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config" / "settings.yaml"
            config.parent.mkdir(parents=True)
            config.write_text("workflow:\n  max_rework: 1\n", encoding="utf-8")
            manager = TranslationJobManager(
                config_path=config,
                trace_dir=root / "traces",
                project_root=root,
                max_input_chars=1000,
            )

            def fake_popen(command, **_):
                output = Path(command[command.index("--output") + 1])
                metadata = Path(command[command.index("--metadata-output") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("Translated in browser.\n", encoding="utf-8")
                metadata.write_text(
                    json.dumps({"qa_verdict": "pass", "qa_score": 9.0}),
                    encoding="utf-8",
                )
                self.assertIn("--trace-dir", command)
                self.assertIn("--run-id", command)
                return SimpleNamespace(
                    wait=lambda timeout=None: 0,
                    poll=lambda: 0,
                    terminate=lambda: None,
                    kill=lambda: None,
                )

            with patch(
                "mant.observability.dashboard.subprocess.Popen", side_effect=fake_popen
            ):
                submitted = manager.submit(
                    text="他拔出了剑。",
                    work_id="demo/../../unsafe",
                    chapter_id="第一章",
                    max_rework=1,
                )
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    result = manager.get(submitted["job_id"])
                    if result and result["status"] in {"completed", "failed"}:
                        break
                    time.sleep(0.01)
                else:
                    self.fail("浏览器翻译任务未在测试时限内完成")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["result_text"], "Translated in browser.\n")
            self.assertNotIn("/", result["work_id"])
            input_path = root / result["input_path"]
            self.assertEqual(input_path.read_text(encoding="utf-8"), "他拔出了剑。")

    def test_browser_input_validation_and_controls_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = TranslationJobManager(project_root=tmp, max_input_chars=5)
            with self.assertRaisesRegex(ValueError, "请输入"):
                manager.submit(text="   ")
            with self.assertRaisesRegex(ValueError, "输入过长"):
                manager.submit(text="123456")
        self.assertEqual(_safe_id("../../", fallback="safe"), "safe")
        self.assertIn('id="sourceText"', DASHBOARD_HTML)
        self.assertIn('id="fileInput"', DASHBOARD_HTML)
        self.assertIn("/api/translate", DASHBOARD_HTML)

    def test_closing_dashboard_terminates_active_translation(self) -> None:
        created = threading.Event()
        stopped = threading.Event()

        class BlockingProcess:
            def wait(self, timeout=None):
                if not stopped.wait(timeout):
                    raise subprocess.TimeoutExpired("fake", timeout)
                return -15

            def poll(self):
                return -15 if stopped.is_set() else None

            def terminate(self):
                stopped.set()

            def kill(self):
                stopped.set()

        def fake_popen(*_, **__):
            created.set()
            return BlockingProcess()

        with tempfile.TemporaryDirectory() as tmp:
            manager = TranslationJobManager(project_root=tmp)
            with patch(
                "mant.observability.dashboard.subprocess.Popen",
                side_effect=fake_popen,
            ):
                submitted = manager.submit(text="原文", max_rework=0)
                self.assertTrue(created.wait(1))
                manager.close()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    state = manager.get(submitted["job_id"])
                    if state and state["status"] == "failed":
                        break
                    time.sleep(0.01)
        self.assertTrue(stopped.is_set())
        self.assertEqual(state["status"], "failed")


if __name__ == "__main__":
    unittest.main()
