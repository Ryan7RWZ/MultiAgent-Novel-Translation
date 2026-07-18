"""LangGraph QA 回环与正式 CLI 的端到端测试。"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mant.cli import main as cli_main
from mant.execution import RunManifestStore
from mant.workflow.graph import build_graph, run_chapter
from mant.workflow.state import init_state


class ScriptedLLM:
    def __init__(
        self, *, always_rework: bool = False, first_rework: bool = True
    ) -> None:
        self.last_notes: list[str] = []
        self.qa_calls = 0
        self.translator_systems: list[str] = []
        self.always_rework = always_rework
        self.first_rework = first_rework
        self.role_calls = {
            "terminologist": 0,
            "translator": 0,
            "editor": 0,
            "polisher": 0,
            "qa": 0,
        }

    def with_tier(self, _: str) -> "ScriptedLLM":
        return self

    def complete(self, system: str, user: str, **_: object) -> str:
        if "术语抽取专家" in system:
            self.role_calls["terminologist"] += 1
            return "[]"
        if "资深网络小说译者" in system:
            self.role_calls["translator"] += 1
            self.translator_systems.append(system)
            return f"Draft {len(self.translator_systems)}"
        if "翻译审校编辑" in system:
            self.role_calls["editor"] += 1
            return "[]"
        if "英文网络小说润色师" in system:
            self.role_calls["polisher"] += 1
            return "Polished draft"
        if "翻译质量终审专家" in system:
            self.role_calls["qa"] += 1
            self.qa_calls += 1
            if self.always_rework or (self.first_rework and self.qa_calls == 1):
                return json.dumps(
                    {
                        "accuracy": 5,
                        "fluency": 5,
                        "terminology": 5,
                        "style": 5,
                        "verdict": "rework",
                        "suggestions": ["补回原文中的拔剑动作"],
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "accuracy": 8,
                    "fluency": 8,
                    "terminology": 8,
                    "style": 8,
                    "verdict": "pass",
                    "suggestions": [],
                }
            )
        raise AssertionError(f"未识别的 Agent prompt: {system[:50]}")


class SegmentAwareLLM:
    """线程安全脚本模型：只让“坏片”首轮 QA 返工。"""

    def __init__(self) -> None:
        self.last_notes: list[str] = []
        self.calls: dict[tuple[str, str], int] = {}
        self._active = 0
        self.peak_active = 0
        self._lock = threading.Lock()

    def with_tier(self, _: str) -> "SegmentAwareLLM":
        return self

    def complete(self, system: str, user: str, **_: object) -> str:
        if "术语抽取专家" in system:
            return "[]"
        if "资深网络小说译者" in system:
            role = "translator"
        elif "翻译审校编辑" in system:
            role = "editor"
        elif "英文网络小说润色师" in system:
            role = "polisher"
        elif "翻译质量终审专家" in system:
            role = "qa"
        else:
            raise AssertionError(f"未识别的 Agent prompt: {system[:50]}")

        segment = (
            "bad"
            if "BAD_SEGMENT" in user or " complete bad " in user.lower()
            else "good"
        )
        key = (role, segment)
        with self._lock:
            self.calls[key] = self.calls.get(key, 0) + 1
            call_no = self.calls[key]
            self._active += 1
            self.peak_active = max(self.peak_active, self._active)
        try:
            time.sleep(0.01 if segment == "bad" else 0.02)
            if role == "translator":
                return f"A complete {segment} translated paragraph with all details."
            if role == "editor":
                return "[]"
            if role == "polisher":
                return f"A polished {segment} translated paragraph with all details."
            verdict = "rework" if segment == "bad" and call_no == 1 else "pass"
            score = 5 if verdict == "rework" else 8
            return json.dumps(
                {
                    "accuracy": score,
                    "fluency": score,
                    "terminology": score,
                    "style": score,
                    "verdict": verdict,
                    "suggestions": ["修复坏片"] if verdict == "rework" else [],
                },
                ensure_ascii=False,
            )
        finally:
            with self._lock:
                self._active -= 1


class TestWorkflow(unittest.TestCase):
    def test_workflow_resume_reuses_successful_segment_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = ScriptedLLM(first_rework=False)
            config = {
                "checkpoint": {
                    "enabled": True,
                    "sqlite_path": str(Path(tmp) / "checkpoints.db"),
                }
            }
            first_app = build_graph(  # type: ignore[arg-type]
                llm,
                None,
                max_rework=0,
                execution_config=config,
            )

            def fresh_state():
                return init_state(
                    "demo",
                    "resume",
                    ["first", "second"],
                    max_rework=0,
                    run_id="run-resume",
                )

            first = first_app.invoke(fresh_state())
            calls_after_first = dict(llm.role_calls)
            resumed_app = build_graph(  # type: ignore[arg-type]
                llm,
                None,
                max_rework=0,
                execution_config=config,
            )
            second = resumed_app.invoke(fresh_state())

        for role in ("translator", "editor", "polisher", "qa"):
            self.assertEqual(llm.role_calls[role], calls_after_first[role])
        self.assertEqual(llm.role_calls["terminologist"], 2)
        self.assertEqual(first["polished"], second["polished"])
        self.assertEqual(second["execution_stats"]["checkpoint_hits"], 10)

    def test_terminology_is_extracted_per_segment_and_deduplicated(self) -> None:
        class TermMapLLM(ScriptedLLM):
            def complete(self, system: str, user: str, **kwargs: object) -> str:
                if "术语抽取专家" in system:
                    self.role_calls["terminologist"] += 1
                    confidence = 0.9 if "SECOND_TERM" in user else 0.6
                    target = "Preferred Name" if confidence > 0.8 else "Early Name"
                    return json.dumps(
                        {
                            "terms": [
                                {
                                    "source": "共同术语",
                                    "target": target,
                                    "category": "other",
                                    "confidence": confidence,
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                return super().complete(system, user, **kwargs)

        llm = TermMapLLM(first_rework=False)
        app = build_graph(  # type: ignore[arg-type]
            llm,
            None,
            max_rework=0,
            execution_config={
                "enabled": True,
                "global_max_in_flight": 2,
                "stages": {"terminology": 2},
            },
        )
        final = app.invoke(
            init_state(
                "demo",
                "terms",
                ["FIRST_TERM 共同术语。", "SECOND_TERM 共同术语。"],
                max_rework=0,
            )
        )

        self.assertEqual(llm.role_calls["terminologist"], 2)
        self.assertEqual(final["glossary"], {"共同术语": "Preferred Name"})

    def test_role_generation_settings_invalidate_only_affected_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = ScriptedLLM(first_rework=False)
            config = {
                "checkpoint": {
                    "enabled": True,
                    "sqlite_path": str(Path(tmp) / "checkpoints.db"),
                }
            }

            def invoke(max_tokens: int):
                app = build_graph(  # type: ignore[arg-type]
                    llm,
                    None,
                    max_rework=0,
                    execution_config=config,
                    agent_config={"qa": {"max_tokens": max_tokens}},
                )
                return app.invoke(
                    init_state(
                        "demo",
                        "fingerprint",
                        ["原文。"],
                        max_rework=0,
                        run_id="run-fingerprint",
                    )
                )

            invoke(768)
            invoke(512)

        self.assertEqual(llm.role_calls["translator"], 1)
        self.assertEqual(llm.role_calls["editor"], 1)
        self.assertEqual(llm.role_calls["polisher"], 1)
        self.assertEqual(llm.role_calls["qa"], 2)

    def test_manifest_resume_starts_at_qa_and_retries_failed_checkpoint(self) -> None:
        class FailFirstQALLM(ScriptedLLM):
            def complete(self, system: str, user: str, **kwargs: object) -> str:
                if "翻译质量终审专家" in system:
                    self.role_calls["qa"] += 1
                    self.qa_calls += 1
                    if self.qa_calls == 1:
                        return "invalid qa output"
                    return json.dumps(
                        {
                            "accuracy": 8,
                            "fluency": 8,
                            "terminology": 8,
                            "style": 8,
                            "verdict": "pass",
                            "suggestions": [],
                        }
                    )
                return super().complete(system, user, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chapter = root / "chapter.txt"
            chapter.write_text("他拔出了剑。\n", encoding="utf-8")
            checkpoint_path = root / "checkpoints.db"
            manifest_dir = root / "runs"
            config = {
                "checkpoint": {
                    "enabled": True,
                    "sqlite_path": str(checkpoint_path),
                },
                "manifest": {"enabled": True, "directory": str(manifest_dir)},
            }
            agents = {"qa": {"repair_attempts": 0}}
            llm = FailFirstQALLM(first_rework=False)
            first = run_chapter(  # type: ignore[arg-type]
                "demo",
                chapter,
                llm,
                None,
                chapter_id="resume-qa",
                max_rework=0,
                run_id="run-resume-qa",
                execution_config=config,
                agent_config=agents,
            )
            calls_after_first = dict(llm.role_calls)
            manifest = RunManifestStore(manifest_dir).load("run-resume-qa")
            resume_config = {
                **config,
                "resume": {"stage": "qa", "failed_only": True},
            }
            second = run_chapter(  # type: ignore[arg-type]
                "demo",
                chapter,
                llm,
                None,
                chapter_id="resume-qa",
                max_rework=0,
                run_id="run-resume-qa",
                execution_config=resume_config,
                agent_config=agents,
                resume_state=manifest["state"],
                start_stage="qa",
            )

        for role in ("terminologist", "translator", "editor", "polisher"):
            self.assertEqual(llm.role_calls[role], calls_after_first[role])
        self.assertEqual(llm.role_calls["qa"], calls_after_first["qa"] + 1)
        self.assertEqual(first["qa_verdict"], "rework")
        self.assertEqual(first["qa_summary"]["coverage"], 0.0)
        self.assertEqual(
            first["qa_summary"]["failure_categories"],
            {"AgentOutputInvalid": 1},
        )
        self.assertEqual(second["qa_verdict"], "pass")
        self.assertEqual(second["qa_summary"]["coverage"], 1.0)
        self.assertEqual(second["qa_summary"]["pass_ratio"], 1.0)

    def test_concurrent_rework_only_reruns_failed_segment(self) -> None:
        llm = SegmentAwareLLM()
        app = build_graph(  # type: ignore[arg-type]
            llm,
            None,
            max_rework=2,
            execution_config={
                "enabled": True,
                "global_max_in_flight": 2,
                "stages": {
                    "translate": 2,
                    "edit": 2,
                    "polish": 2,
                    "qa": 2,
                },
            },
        )
        final = app.invoke(
            init_state(
                "demo",
                "selective",
                ["GOOD_SEGMENT.", "BAD_SEGMENT."],
                max_rework=2,
                segment_meta=[
                    {"segment_id": "selective#seg0000", "estimated_tokens": 3},
                    {"segment_id": "selective#seg0001", "estimated_tokens": 3},
                ],
            )
        )

        for role in ("translator", "editor", "polisher", "qa"):
            self.assertEqual(llm.calls[(role, "good")], 1)
            self.assertEqual(llm.calls[(role, "bad")], 2)
        self.assertEqual(final["qa_verdict"], "pass")
        self.assertEqual(final["rework_count"], 1)
        self.assertEqual(final["rework_segment_indices"], [])
        self.assertEqual(len(final["draft_segments"]), 2)
        self.assertEqual(len(final["polished_segments"]), 2)
        self.assertEqual(len(final["segment_qa"]), 2)
        self.assertEqual(final["execution_stats"]["peak_in_flight"], 2)
        self.assertGreaterEqual(llm.peak_active, 2)

    def test_editor_polisher_and_qa_process_every_segment(self) -> None:
        llm = ScriptedLLM(first_rework=False)
        app = build_graph(llm, None, max_rework=0)  # type: ignore[arg-type]
        final = app.invoke(
            init_state(
                "demo",
                "multi",
                ["第一段。", "第二段。"],
                max_rework=0,
                segment_meta=[
                    {"segment_id": "multi#seg0000", "estimated_tokens": 5},
                    {"segment_id": "multi#seg0001", "estimated_tokens": 5},
                ],
            )
        )

        self.assertEqual(llm.role_calls["translator"], 2)
        self.assertEqual(llm.role_calls["editor"], 2)
        self.assertEqual(llm.role_calls["polisher"], 2)
        self.assertEqual(llm.role_calls["qa"], 2)
        self.assertEqual(len(final["draft_segments"]), 2)
        self.assertEqual(len(final["polished_segments"]), 2)
        self.assertEqual(len(final["segment_qa"]), 2)
        self.assertEqual(final["qa_score"], 8.0)
        self.assertEqual(final["qa_verdict"], "pass")
        self.assertEqual(final["segment_failures"], [])
        self.assertEqual(final["polished"], "Polished draft\n\nPolished draft")

    def test_short_polisher_output_falls_back_and_forces_rework(self) -> None:
        class ShortPolishLLM(ScriptedLLM):
            def complete(self, system: str, user: str, **kwargs: object) -> str:
                if "资深网络小说译者" in system:
                    self.role_calls["translator"] += 1
                    self.translator_systems.append(system)
                    return "A complete translated paragraph. " * 20
                if "英文网络小说润色师" in system:
                    self.role_calls["polisher"] += 1
                    return "Too short."
                return super().complete(system, user, **kwargs)

        llm = ShortPolishLLM(first_rework=False)
        app = build_graph(llm, None, max_rework=0)  # type: ignore[arg-type]
        final = app.invoke(init_state("demo", "ratio", ["原文。"], max_rework=0))

        self.assertEqual(final["polished"], final["draft"])
        self.assertEqual(final["qa_verdict"], "rework")
        self.assertTrue(
            any(item.get("stage") == "polish" for item in final["segment_failures"])
        )
        self.assertTrue(
            any("needs_human_review" in str(note) for note in final["review_notes"])
        )

    def test_qa_feedback_drives_one_rework_then_passes(self) -> None:
        llm = ScriptedLLM()
        app = build_graph(llm, None, max_rework=2)  # type: ignore[arg-type]
        final = app.invoke(init_state("demo", "1", ["他拔出了剑。"], max_rework=2))
        self.assertEqual(final["qa_verdict"], "pass")
        self.assertEqual(final["rework_count"], 1)
        self.assertEqual(len(llm.translator_systems), 2)
        self.assertIn("补回原文中的拔剑动作", llm.translator_systems[1])

    def test_rework_limit_marks_human_review(self) -> None:
        llm = ScriptedLLM(always_rework=True)
        app = build_graph(llm, None, max_rework=1)  # type: ignore[arg-type]
        final = app.invoke(init_state("demo", "1", ["原文。"], max_rework=1))
        self.assertEqual(final["rework_count"], 1)
        self.assertTrue(
            any("needs_human_review" in str(note) for note in final["review_notes"])
        )

    def test_translate_cli_without_api_key_exports_text_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chapter = root / "chapter.txt"
            chapter.write_text("他拔出了剑。\n", encoding="utf-8")
            config = root / "settings.yaml"
            config.write_text(
                "memory:\n"
                f"  sqlite_path: '{(root / 'memory.db').as_posix()}'\n"
                f"  faiss_index_dir: '{(root / 'faiss').as_posix()}'\n"
                f"  story_bible_dir: '{(root / 'bible').as_posix()}'\n"
                "workflow:\n"
                "  max_rework: 1\n",
                encoding="utf-8",
            )
            output = root / "translated.txt"
            metadata = root / "translated.json"
            code = cli_main(
                [
                    "translate-chapter",
                    "--config",
                    str(config),
                    "--work-id",
                    "demo",
                    "--chapter-id",
                    "0001",
                    "--input",
                    str(chapter),
                    "--output",
                    str(output),
                    "--metadata-output",
                    str(metadata),
                ]
            )
            self.assertEqual(code, 0)
            self.assertIn("[DRAFT]", output.read_text(encoding="utf-8"))
            run = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual(run["rework_count"], 1)
            self.assertTrue(run["needs_human_review"])

    def test_resume_run_cli_uses_manifest_and_exports_resume_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chapter = root / "chapter.txt"
            chapter.write_text("他拔出了剑。\n", encoding="utf-8")
            config = root / "settings.yaml"
            config.write_text(
                "memory:\n"
                f"  sqlite_path: '{(root / 'memory.db').as_posix()}'\n"
                f"  faiss_index_dir: '{(root / 'faiss').as_posix()}'\n"
                f"  story_bible_dir: '{(root / 'bible').as_posix()}'\n"
                "agents:\n"
                "  qa:\n"
                "    repair_attempts: 0\n"
                "concurrency:\n"
                "  checkpoint:\n"
                "    enabled: true\n"
                f"    sqlite_path: '{(root / 'checkpoints.db').as_posix()}'\n"
                "  manifest:\n"
                "    enabled: true\n"
                f"    directory: '{(root / 'runs').as_posix()}'\n",
                encoding="utf-8",
            )
            first_output = root / "first.txt"
            first_metadata = root / "first.json"
            first_code = cli_main(
                [
                    "translate-chapter",
                    "--config",
                    str(config),
                    "--work-id",
                    "demo",
                    "--chapter-id",
                    "resume-cli",
                    "--input",
                    str(chapter),
                    "--max-rework",
                    "0",
                    "--run-id",
                    "run-resume-cli",
                    "--output",
                    str(first_output),
                    "--metadata-output",
                    str(first_metadata),
                ]
            )
            resumed_output = root / "resumed.txt"
            resumed_metadata = root / "resumed.json"
            resumed_code = cli_main(
                [
                    "resume-run",
                    "--config",
                    str(config),
                    "--run-id",
                    "run-resume-cli",
                    "--stage",
                    "qa",
                    "--failed-only",
                    "--output",
                    str(resumed_output),
                    "--metadata-output",
                    str(resumed_metadata),
                ]
            )

            self.assertEqual(first_code, 0)
            self.assertEqual(resumed_code, 0)
            metadata = json.loads(resumed_metadata.read_text(encoding="utf-8"))
            self.assertEqual(metadata["run_id"], "run-resume-cli")
            self.assertEqual(
                metadata["resume"],
                {"resumed": True, "stage": "qa", "failed_only": True},
            )
            self.assertTrue(resumed_output.is_file())


if __name__ == "__main__":
    unittest.main()
