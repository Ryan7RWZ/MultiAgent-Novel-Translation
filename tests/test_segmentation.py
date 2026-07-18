"""确定性初始切片的可逆性、预算与结构边界测试。"""

from __future__ import annotations

import unittest

from mant.agents.base import AgentTask
from mant.agents.orchestrator import OrchestratorAgent
from mant.segmentation import SegmentationConfig, safe_normalize, segment_text


class NeverCalledLLM:
    last_notes: list[str] = []

    def complete(self, *_: object, **__: object) -> str:
        raise AssertionError("机械初始切片不应调用 LLM")


class TestDeterministicSegmentation(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SegmentationConfig(
            target_core_tokens=40,
            max_core_tokens=50,
            min_core_tokens=10,
            context_before_tokens=12,
            context_after_tokens=8,
            max_segments=100,
        )

    def test_safe_normalization_is_lossless_for_visible_repeated_lines(self) -> None:
        raw = "\ufeff重复行\r\n重复行\r结尾\x00"
        self.assertEqual(safe_normalize(raw), "重复行\n重复行\n结尾")
        result = segment_text(raw, chapter_id="c1", config=self.config)
        self.assertEqual("".join(result.texts), result.normalized_text)
        self.assertEqual(result.normalized_text.count("重复行"), 2)
        self.assertTrue(result.statistics.reconstruction_ok)

    def test_scene_separator_is_retained_and_context_does_not_cross_it(self) -> None:
        before = "甲说了一句话。" * 12 + "\n"
        after = "***\n" + "乙回答了一句话。" * 12
        result = segment_text(before + after, chapter_id="scene", config=self.config)

        self.assertEqual("".join(result.texts), before + after)
        scene_cuts = [
            index
            for index, segment in enumerate(result.segments)
            if segment.boundary_after == "scene"
        ]
        self.assertEqual(len(scene_cuts), 1)
        cut = scene_cuts[0]
        self.assertEqual(result.segments[cut].context_after, "")
        self.assertEqual(result.segments[cut + 1].context_before, "")
        self.assertTrue(result.segments[cut + 1].core_text.startswith("***\n"))

    def test_unpunctuated_text_uses_bounded_hard_splits(self) -> None:
        text = "甲" * 500
        result = segment_text(text, chapter_id="long", config=self.config)

        self.assertEqual("".join(result.texts), text)
        self.assertTrue(all(item.estimated_tokens <= 50 for item in result.segments))
        self.assertEqual(
            result.statistics.hard_split_count,
            sum(item.boundary_after == "hard" for item in result.segments),
        )
        self.assertGreater(result.statistics.hard_split_count, 0)

    def test_same_input_and_config_produce_identical_result(self) -> None:
        text = "第一章\n这是第一句。这是第二句！\n\n尾段。"
        first = segment_text(text, chapter_id="stable", config=self.config)
        second = segment_text(text, chapter_id="stable", config=self.config)
        self.assertEqual(first, second)
        self.assertEqual(
            [item.segment_id for item in first.segments],
            [f"stable#seg{i:04d}" for i in range(len(first.segments))],
        )

    def test_segment_limit_rejects_abnormal_input(self) -> None:
        config = SegmentationConfig(
            target_core_tokens=5,
            max_core_tokens=5,
            min_core_tokens=0,
            max_segments=1,
        )
        with self.assertRaisesRegex(ValueError, "超过上限"):
            segment_text("甲" * 20, config=config)

    def test_orchestrator_returns_metadata_without_calling_llm(self) -> None:
        agent = OrchestratorAgent(  # type: ignore[arg-type]
            NeverCalledLLM(), segmentation_config=self.config
        )
        task = AgentTask("demo", "c1", "chapter", "原文。\n原文。", {})
        result = agent.run(task)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.output["segments"]), len(result.output["segment_meta"]))
        self.assertEqual(
            "".join(result.output["segments"]), result.output["normalized_text"]
        )
        self.assertNotIn("core_text", result.output["segment_meta"][0])


if __name__ == "__main__":
    unittest.main()
