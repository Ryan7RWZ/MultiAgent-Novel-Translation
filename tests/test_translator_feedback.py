"""翻译 Agent 必须消费返工批注。"""

from __future__ import annotations

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
        system, _ = llm.calls[0]
        self.assertIn("第 1 轮", system)
        self.assertIn("补回拔剑动作", system)
        self.assertIn("最高优先级", system)
        self.assertEqual(TranslatorAgent.tier, "strong")

    def test_existing_glossary_matches_without_llm_candidates(self) -> None:
        llm = RecordingLLM()
        # 术语 Agent 收到不可解析的普通文本，相当于无 key 的 [DRAFT] 降级。
        agent = TerminologistAgent(llm, ExistingTermMemory())  # type: ignore[arg-type]
        result = agent.run(
            AgentTask("demo", "1", "chapter", "他拜入青玄宗。", {"mode": "extract"})
        )
        self.assertEqual(result.output["glossary"], {"青玄宗": "Azure Profound Sect"})


if __name__ == "__main__":
    unittest.main()
