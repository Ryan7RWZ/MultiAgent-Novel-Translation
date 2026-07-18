"""真实长文本验收暴露的 Editor/修订/QA 质量闭环回归测试。"""

from __future__ import annotations

import json
import unittest

from mant.agents.base import AgentTask
from mant.agents.editor import EditorAgent
from mant.agents.qa import QAAgent
from mant.workflow.graph import build_graph
from mant.workflow.state import init_state


class EditorRecoveryLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.last_notes: list[str] = []

    def complete(
        self, system: str, user: str, **kwargs: object
    ) -> str:
        self.calls.append((system, dict(kwargs)))
        if len(self.calls) == 1:
            return '{"review_notes": ['  # 模拟 finish_reason=length 后的残缺 JSON
        return json.dumps(
            {
                "review_notes": [
                    {
                        "issue_type": "other",
                        "span": "low-span-is-too-long",
                        "suggestion": "low suggestion is too long",
                        "severity": "low",
                    },
                    {
                        "issue_type": "omission",
                        "span": "high-span-is-too-long",
                        "suggestion": "high suggestion is too long",
                        "severity": "high",
                    },
                    {
                        "issue_type": "mistranslation",
                        "span": "medium-span-is-too-long",
                        "suggestion": "medium suggestion is too long",
                        "severity": "medium",
                    },
                ]
            }
        )


class BoundaryQALLM:
    def __init__(self, *, verdict: str = "rework") -> None:
        self.verdict = verdict
        self.last_notes: list[str] = []

    def complete(self, system: str, user: str, **kwargs: object) -> str:
        return json.dumps(
            {
                "accuracy": 6,
                "fluency": 8,
                "terminology": 8,
                "style": 7,
                "verdict": self.verdict,
                "suggestions": ["补回遗漏的声明。"] if self.verdict == "rework" else [],
            },
            ensure_ascii=False,
        )


class QualityLoopLLM:
    """模拟片 0：初译漏掉前置声明，Editor 发现后由 revise 定点补回。"""

    def __init__(self) -> None:
        self.last_notes: list[str] = []
        self.calls = {
            "terminologist": 0,
            "translate": 0,
            "revise": 0,
            "editor": 0,
            "polisher": 0,
            "qa": 0,
        }
        self.polisher_users: list[str] = []

    def with_tier(self, _: str) -> "QualityLoopLLM":
        return self

    def complete(self, system: str, user: str, **kwargs: object) -> str:
        if "术语抽取专家" in system:
            self.calls["terminologist"] += 1
            return "[]"
        if "当前只负责定点修订译文" in system:
            self.calls["revise"] += 1
            self.assert_revision_prompt(system, user)
            return "Authorized site notice.\n---\nBook Title"
        if "资深网络小说译者" in system:
            self.calls["translate"] += 1
            return "Book Title"
        if "翻译审校编辑" in system:
            self.calls["editor"] += 1
            return json.dumps(
                {
                    "review_notes": [
                        {
                            "issue_type": "omission",
                            "span": "Book Title",
                            "suggestion": "补回站点声明与分隔线，保留书名且不要重复。",
                            "severity": "high",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        if "英文网络小说润色师" in system:
            self.calls["polisher"] += 1
            self.polisher_users.append(user)
            return "Authorized site notice.\n---\nBook Title"
        if "翻译质量终审专家" in system:
            self.calls["qa"] += 1
            return json.dumps(
                {
                    "accuracy": 9,
                    "fluency": 9,
                    "terminology": 9,
                    "style": 9,
                    "verdict": "pass",
                    "suggestions": [],
                }
            )
        raise AssertionError(f"未识别的 prompt: {system[:80]}")

    @staticmethod
    def assert_revision_prompt(system: str, user: str) -> None:
        if "补回站点声明与分隔线" not in system:
            raise AssertionError("定点修订没有收到 Editor 的 high 意见")
        if "【当前译文】\nBook Title" not in user:
            raise AssertionError("定点修订没有收到原始初稿")


class TestQualityLoop(unittest.TestCase):
    def test_editor_uses_compact_recovery_and_enforces_hard_bounds(self) -> None:
        llm = EditorRecoveryLLM()
        agent = EditorAgent(llm)  # type: ignore[arg-type]
        agent.max_review_notes = 2
        agent.max_span_chars = 8
        agent.max_suggestion_chars = 12
        result = agent.run(
            AgentTask(
                work_id="demo",
                chapter_id="13",
                segment_id="13#seg0013",
                source_text="十九个段落的原文。",
                context={"draft": "A draft with many paragraphs."},
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("上一次审校输出过长或结构无效", llm.calls[1][0])
        self.assertEqual(llm.calls[1][1]["max_tokens"], 768)
        review_notes = result.output["review_notes"]
        self.assertEqual([item["severity"] for item in review_notes], ["high", "medium"])
        self.assertTrue(all(len(item["span"]) <= 8 for item in review_notes))
        self.assertTrue(all(len(item["suggestion"]) <= 12 for item in review_notes))
        self.assertTrue(any("紧凑恢复" in note for note in result.notes))

    def test_editor_high_omission_is_revised_before_polish(self) -> None:
        llm = QualityLoopLLM()
        app = build_graph(llm, None, max_rework=0)  # type: ignore[arg-type]
        final = app.invoke(
            init_state(
                "demo",
                "front-matter",
                ["授权站点声明。\n---\n《书名》"],
                max_rework=0,
            )
        )

        expected = "Authorized site notice.\n---\nBook Title"
        self.assertEqual(final["draft"], "Book Title")
        self.assertEqual(final["revised"], expected)
        self.assertEqual(final["polished"], expected)
        self.assertEqual(final["qa_verdict"], "pass")
        self.assertEqual(final["segment_failures"], [])
        self.assertEqual(llm.calls["translate"], 1)
        self.assertEqual(llm.calls["revise"], 1)
        high_note = next(
            note
            for note in final["review_notes"]
            if isinstance(note, dict) and note.get("severity") == "high"
        )
        self.assertEqual(high_note["resolution"], "revision_applied")
        self.assertNotIn("补回站点声明", llm.polisher_users[0])

    def test_qa_does_not_override_explicit_rework_at_threshold_boundary(self) -> None:
        agent = QAAgent(BoundaryQALLM())  # type: ignore[arg-type]
        result = agent.run(
            AgentTask(
                "demo",
                "0",
                "0#seg0000",
                "授权站点声明。\n---\n《书名》",
                {"polished": "Book Title"},
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.output["qa_score"], 7.0)
        self.assertEqual(result.output["qa_verdict"], "rework")

    def test_qa_blocks_unresolved_high_omission_even_when_model_passes(self) -> None:
        agent = QAAgent(BoundaryQALLM(verdict="pass"))  # type: ignore[arg-type]
        result = agent.run(
            AgentTask(
                "demo",
                "0",
                "0#seg0000",
                "授权站点声明。",
                {
                    "polished": "Authorized site notice.",
                    "review_notes": [
                        {
                            "issue_type": "omission",
                            "severity": "high",
                            "suggestion": "补回声明",
                            "resolution": "pending",
                        }
                    ],
                },
            )
        )

        self.assertEqual(result.output["qa_verdict"], "rework")
        self.assertTrue(any("未进入修订" in note for note in result.notes))


if __name__ == "__main__":
    unittest.main()
