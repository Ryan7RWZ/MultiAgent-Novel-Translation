"""LangGraph QA 回环与正式 CLI 的端到端测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mant.cli import main as cli_main
from mant.workflow.graph import build_graph
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


class TestWorkflow(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
