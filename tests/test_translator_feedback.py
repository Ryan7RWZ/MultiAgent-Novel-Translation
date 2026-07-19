"""翻译 Agent 必须消费返工批注。"""

from __future__ import annotations

import json
import unittest

from mant.agents.base import AgentTask
from mant.agents.terminologist import TerminologistAgent
from mant.agents.translator import TranslatorAgent
from mant.memory.models import TermEntry


class RecordingLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.last_notes: list[str] = []

    def complete(self, system: str, user: str, **_: object) -> str:
        self.calls.append((system, user))
        return "Revised translation."


class ExistingTermMemory:
    def match_terms(self, source_text: str, work_id: str):
        if "青玄宗" not in source_text:
            return {}
        return {
            "青玄宗": TermEntry(
                source="青玄宗",
                target="Azure Profound Sect",
                category="place",
                work_id=work_id,
                confidence=1.0,
            )
        }


class TermListLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.last_notes: list[str] = []

    def complete(self, system: str, user: str, **kwargs: object) -> str:
        self.calls.append((system, dict(kwargs)))
        return json.dumps(
            {
                "terms": [
                    {
                        "source": f"术语{index}",
                        "target": f"Term {index}",
                        "category": "other",
                        "confidence": confidence,
                    }
                    for index, confidence in enumerate((0.2, 0.9, 0.7, 1.0, 0.8))
                ]
            },
            ensure_ascii=False,
        )


class TestTranslatorFeedback(unittest.TestCase):
    def test_review_notes_are_high_priority_prompt_input(self) -> None:
        llm = RecordingLLM()
        agent = TranslatorAgent(llm)  # type: ignore[arg-type]
        result = agent.run(
            AgentTask(
                work_id="demo",
                chapter_id="1",
                segment_id="1#0",
                source_text="他拔出了剑。",
                context={
                    "round": 1,
                    "context_before": "上文只用于消歧。",
                    "context_after": "下文只用于消歧。",
                    "review_notes": [
                        {
                            "severity": "major",
                            "issue_type": "accuracy",
                            "span": "拔出了剑",
                            "suggestion": "补回拔剑动作",
                        }
                    ],
                },
            )
        )
        self.assertTrue(result.ok)
        system, user = llm.calls[0]
        self.assertIn("第 1 轮", system)
        self.assertIn("补回拔剑动作", system)
        self.assertIn("最高优先级", system)
        self.assertIn("上文只用于消歧。", user)
        self.assertIn("他拔出了剑。", user)
        self.assertIn("下文只用于消歧。", user)
        self.assertIn("禁止翻译或输出", user)
        self.assertEqual(TranslatorAgent.tier, "strong")

    def test_existing_glossary_matches_without_llm_candidates(self) -> None:
        llm = RecordingLLM()
        # 术语 Agent 收到不可解析的普通文本，相当于无 key 的 [DRAFT] 降级。
        agent = TerminologistAgent(llm, ExistingTermMemory())  # type: ignore[arg-type]
        result = agent.run(
            AgentTask("demo", "1", "chapter", "他拜入青玄宗。", {"mode": "extract"})
        )
        self.assertEqual(result.output["glossary"], {"青玄宗": "Azure Profound Sect"})

    def test_terminologist_enforces_prompt_and_code_term_bounds(self) -> None:
        llm = TermListLLM()
        agent = TerminologistAgent(llm)  # type: ignore[arg-type]
        agent.max_terms = 3
        result = agent.run(
            AgentTask("demo", "1", "1#seg0000", "包含许多专名的原文。")
        )

        self.assertTrue(result.ok)
        self.assertIn("本次最多返回 3 条", llm.calls[0][0])
        self.assertEqual(llm.calls[0][1]["max_tokens"], 1536)
        self.assertEqual(
            [item["source"] for item in result.output["new_terms"]],
            ["术语3", "术语1", "术语4"],
        )
        self.assertTrue(any("稳定保留前 3 条" in note for note in result.notes))


if __name__ == "__main__":
    unittest.main()
