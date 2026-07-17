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
    def __init__(self, *, always_rework: bool = False) -> None:
        self.last_notes: list[str] = []
        self.qa_calls = 0
        self.translator_systems: list[str] = []
        self.always_rework = always_rework

    def with_tier(self, _: str) -> "ScriptedLLM":
        return self

    def complete(self, system: str, user: str, **_: object) -> str:
        if "术语抽取专家" in system:
            return "[]"
        if "资深网络小说译者" in system:
            self.translator_systems.append(system)
            return f"Draft {len(self.translator_systems)}"
        if "翻译审校编辑" in system:
            return "[]"
        if "英文网络小说润色师" in system:
            return "Polished draft"
        if "翻译质量终审专家" in system:
            self.qa_calls += 1
            if self.always_rework or self.qa_calls == 1:
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
