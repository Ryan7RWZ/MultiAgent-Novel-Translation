"""真实长文本验收暴露的 Editor/修订/QA 质量闭环回归测试。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mant.agents.base import AgentTask
from mant.agents.editor import EditorAgent
from mant.agents.qa import QAAgent
from mant.agents.translator import TranslatorAgent
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


class RevisionOnlyLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.last_notes: list[str] = []

    def complete(self, system: str, user: str, **kwargs: object) -> str:
        return self.response


class SequencedRevisionLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0
        self.users: list[str] = []
        self.last_notes: list[str] = []

    def complete(self, system: str, user: str, **kwargs: object) -> str:
        self.users.append(user)
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


class QualityLoopLLM:
    """模拟片 0：初译漏掉前置声明，Editor 发现后由 revise 定点补回。"""

    def __init__(
        self,
        revision_responses: list[str | None] | None = None,
        *,
        polisher_response: str = "Authorized site notice.\n---\nBook Title",
    ) -> None:
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
        self.revision_kwargs: list[dict[str, object]] = []
        self.revision_systems: list[str] = []
        self.revision_users: list[str] = []
        self.revision_responses = revision_responses or [None]
        self.polisher_response = polisher_response

    def with_tier(self, _: str) -> "QualityLoopLLM":
        return self

    def complete(self, system: str, user: str, **kwargs: object) -> str:
        if "术语抽取专家" in system:
            self.calls["terminologist"] += 1
            return "[]"
        if "当前只负责定点修订译文" in system:
            self.calls["revise"] += 1
            self.assert_revision_prompt(system, user)
            self.revision_kwargs.append(dict(kwargs))
            self.revision_systems.append(system)
            self.revision_users.append(user)
            response_index = min(
                self.calls["revise"] - 1,
                len(self.revision_responses) - 1,
            )
            response = self.revision_responses[response_index]
            return response or self.valid_revision_response(system)
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
            return self.polisher_response
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
        if '"unit_id": "u0001"' not in user or '"text": "Book Title"' not in user:
            raise AssertionError("定点修订没有收到程序生成的译文单元")
        if "unit_id、expected_hash 和 note_id" not in system:
            raise AssertionError("定点修订没有要求引用程序生成的稳定 ID")

    @staticmethod
    def valid_revision_response(system: str) -> str:
        match = re.search(r"id=([^;\]]+)", system)
        if match is None:
            raise AssertionError("定点修订 Prompt 缺少 note_id")
        return json.dumps(
            {
                "status": "apply",
                "operations": [
                    {
                        "action": "insert_before_unit",
                        "unit_id": "u0001",
                        "expected_hash": hashlib.sha256(
                            b"Book Title"
                        ).hexdigest()[:12],
                        "note_ids": [match.group(1)],
                        "text": "Authorized site notice.\n---\n",
                    }
                ],
            }
        )


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
        self.assertEqual(
            llm.revision_kwargs[0]["response_format"],
            {"type": "json_object"},
        )
        self.assertEqual(llm.revision_kwargs[0]["max_tokens"], 1536)
        high_note = next(
            note
            for note in final["review_notes"]
            if isinstance(note, dict) and note.get("severity") == "high"
        )
        self.assertEqual(high_note["revision_resolution"], "applied")
        self.assertEqual(high_note["resolution"], "resolved")
        self.assertEqual(high_note["qa_resolution"], "verified")
        self.assertNotIn("补回站点声明", llm.polisher_users[0])

    def test_partial_revision_text_is_rejected_then_recovered_as_patch(self) -> None:
        llm = QualityLoopLLM(
            revision_responses=[
                "Authorized site notice.",
                None,
            ]
        )
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
        self.assertEqual(final["revised"], expected)
        self.assertEqual(final["polished"], expected)
        self.assertEqual(final["segment_failures"], [])
        self.assertEqual(llm.calls["revise"], 2)
        self.assertEqual(llm.revision_kwargs[1]["max_tokens"], 1024)
        self.assertIn("【上次确定性校验错误】", llm.revision_users[1])
        self.assertIn("修订输出必须是 JSON object", llm.revision_users[1])
        self.assertTrue(
            any("进行错误感知重试" in note for note in final["runtime_notes"])
        )

    def test_duplicate_text_in_different_units_is_addressed_by_unit_id(self) -> None:
        second = "Book returns."
        llm = RevisionOnlyLLM(
            json.dumps(
                {
                    "status": "apply",
                    "operations": [
                        {
                            "action": "replace_unit",
                            "unit_id": "u0002",
                            "expected_hash": hashlib.sha256(
                                second.encode("utf-8")
                            ).hexdigest()[:12],
                            "note_ids": ["note-0001"],
                            "text": "The notice returns.",
                        }
                    ]
                }
            )
        )
        agent = TranslatorAgent(llm)  # type: ignore[arg-type]
        agent.revision_repair_attempts = 0
        result = agent.run(
            AgentTask(
                "demo",
                "front-matter",
                "front-matter#seg0000",
                "原文",
                {
                    "mode": "revision",
                    "draft": "Book appears. Book returns.",
                    "review_notes": [
                        {
                            "severity": "high",
                            "issue_type": "omission",
                            "suggestion": "补回声明",
                        }
                    ],
                },
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.output["revision_status"], "applied")
        self.assertEqual(result.output["draft"], "Book appears. The notice returns.")

    def test_stale_unit_hash_is_semantically_rejected_without_task_failure(self) -> None:
        llm = RevisionOnlyLLM(
            json.dumps(
                {
                    "status": "apply",
                    "operations": [
                        {
                            "action": "replace_unit",
                            "unit_id": "u0001",
                            "expected_hash": "stale",
                            "note_ids": ["note-0001"],
                            "text": "Notice.",
                        }
                    ],
                }
            )
        )
        agent = TranslatorAgent(llm)  # type: ignore[arg-type]
        agent.revision_repair_attempts = 0
        result = agent.run(
            AgentTask(
                "demo",
                "front-matter",
                "front-matter#seg0000",
                "原文",
                {
                    "mode": "revision",
                    "draft": "Book Title",
                    "review_notes": [
                        {
                            "severity": "high",
                            "issue_type": "omission",
                            "suggestion": "补回声明",
                        }
                    ],
                },
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.output["revision_status"], "protocol_rejected")
        self.assertEqual(result.output["draft"], "Book Title")
        self.assertIn("正确值为", result.output["revision_error"])

    def test_identical_replace_is_normalized_to_no_change_pending_qa(self) -> None:
        draft = "Book Title"
        llm = RevisionOnlyLLM(
            json.dumps(
                {
                    "status": "apply",
                    "operations": [
                        {
                            "action": "replace_unit",
                            "unit_id": "u0001",
                            "expected_hash": hashlib.sha256(
                                draft.encode("utf-8")
                            ).hexdigest()[:12],
                            "note_ids": ["note-0001"],
                            "text": draft,
                        }
                    ],
                }
            )
        )
        agent = TranslatorAgent(llm)  # type: ignore[arg-type]
        agent.revision_repair_attempts = 0

        result = agent.run(
            AgentTask(
                "demo",
                "identity",
                "identity#seg0000",
                "原文",
                {
                    "mode": "revision",
                    "draft": draft,
                    "review_notes": [
                        {
                            "note_id": "note-0001",
                            "severity": "high",
                            "issue_type": "mistranslation",
                            "suggestion": "核对现有译法",
                        }
                    ],
                },
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.output["revision_status"], "no_change")
        self.assertEqual(result.output["revision_operations"], 1)
        self.assertEqual(result.output["draft"], draft)
        self.assertTrue(any("安全归一" in note for note in result.notes))

    def test_missing_note_repair_preserves_valid_operations_and_only_fills_gap(self) -> None:
        first = "First."
        second = "Second."
        llm = SequencedRevisionLLM(
            [
                json.dumps(
                    {
                        "status": "apply",
                        "operations": [
                            {
                                "action": "replace_unit",
                                "unit_id": "u0001",
                                "expected_hash": hashlib.sha256(
                                    first.encode("utf-8")
                                ).hexdigest()[:12],
                                "note_ids": ["note-first"],
                                "text": "First fixed.",
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "status": "apply",
                        "operations": [
                            {
                                "action": "replace_unit",
                                "unit_id": "u0002",
                                "expected_hash": hashlib.sha256(
                                    second.encode("utf-8")
                                ).hexdigest()[:12],
                                "note_ids": ["note-second"],
                                "text": "Second fixed.",
                            }
                        ],
                    }
                ),
            ]
        )
        agent = TranslatorAgent(llm)  # type: ignore[arg-type]

        result = agent.run(
            AgentTask(
                "demo",
                "coverage",
                "coverage#seg0000",
                "原文",
                {
                    "mode": "revision",
                    "draft": f"{first} {second}",
                    "review_notes": [
                        {
                            "note_id": "note-first",
                            "severity": "high",
                            "issue_type": "mistranslation",
                            "suggestion": "修正第一句",
                        },
                        {
                            "note_id": "note-second",
                            "severity": "high",
                            "issue_type": "mistranslation",
                            "suggestion": "修正第二句",
                        },
                    ],
                },
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(llm.calls, 2)
        self.assertEqual(result.output["revision_status"], "applied")
        self.assertEqual(result.output["revision_operations"], 2)
        self.assertEqual(result.output["draft"], "First fixed. Second fixed.")
        self.assertIn("【已冻结的合法 operations", llm.users[1])
        self.assertIn("【本次必须覆盖的 note_id】\nnote-second", llm.users[1])
        self.assertNotIn(
            "【本次必须覆盖的 note_id】\nnote-first, note-second",
            llm.users[1],
        )

    def test_no_change_evidence_stays_pending_until_qa_passes(self) -> None:
        class NoChangeLLM(QualityLoopLLM):
            complete_text = "Authorized site notice.\n---\nBook Title"

            def complete(self, system: str, user: str, **kwargs: object) -> str:
                if "当前只负责定点修订译文" in system:
                    self.calls["revise"] += 1
                    self.revision_kwargs.append(dict(kwargs))
                    self.revision_systems.append(system)
                    self.revision_users.append(user)
                    match = re.search(r"id=([^;\]]+)", system)
                    assert match is not None
                    first = "Authorized site notice."
                    return json.dumps(
                        {
                            "status": "no_change",
                            "operations": [],
                            "evidence": [
                                {
                                    "note_id": match.group(1),
                                    "unit_id": "u0001",
                                    "expected_hash": hashlib.sha256(
                                        first.encode("utf-8")
                                    ).hexdigest()[:12],
                                    "quote": first,
                                }
                            ],
                        }
                    )
                if "资深网络小说译者" in system:
                    self.calls["translate"] += 1
                    return self.complete_text
                if "英文网络小说润色师" in system:
                    self.calls["polisher"] += 1
                    self.polisher_users.append(user)
                    return self.complete_text
                return super().complete(system, user, **kwargs)

        llm = NoChangeLLM()
        app = build_graph(llm, None, max_rework=0)  # type: ignore[arg-type]
        final = app.invoke(
            init_state(
                "demo",
                "front-matter",
                ["授权站点声明。\n---\n《书名》"],
                max_rework=0,
            )
        )

        note = next(item for item in final["review_notes"] if isinstance(item, dict))
        self.assertEqual(final["qa_verdict"], "pass")
        self.assertEqual(final["segment_failures"], [])
        self.assertEqual(note["revision_resolution"], "no_change")
        self.assertEqual(note["resolution"], "resolved")
        self.assertEqual(note["qa_resolution"], "verified")

    def test_protocol_rejection_is_semantic_and_does_not_open_circuit(self) -> None:
        invalid = json.dumps(
            {
                "operations": [
                    {
                        "action": "insert_before",
                        "anchor": "Book Title",
                        "text": "Notice.",
                    }
                ]
            }
        )
        llm = QualityLoopLLM(
            revision_responses=[invalid, invalid],
            polisher_response="Book Title",
        )
        app = build_graph(
            llm,
            None,
            max_rework=0,
            execution_config={
                "budget": {"max_failures": 1, "max_failures_per_stage": {"revise": 1}}
            },
        )  # type: ignore[arg-type]
        final = app.invoke(
            init_state(
                "demo",
                "front-matter",
                ["授权站点声明。\n---\n《书名》"],
                max_rework=0,
            )
        )

        self.assertEqual(final["execution_stats"]["failed"], 0)
        self.assertEqual(final["execution_stats"]["rejected"], 0)
        self.assertEqual(final["qa_verdict"], "rework")
        self.assertEqual(len(final["segment_failures"]), 1)
        self.assertEqual(final["segment_failures"][0]["kind"], "semantic")
        self.assertIn("unit-ID", final["segment_failures"][0]["reason"])

    def test_revision_protocol_change_reuses_non_revision_checkpoints(self) -> None:
        with TemporaryDirectory() as tmp:
            execution = {
                "checkpoint": {
                    "enabled": True,
                    "sqlite_path": str(Path(tmp) / "checkpoints.db"),
                }
            }
            state = init_state(
                "demo",
                "front-matter",
                ["授权站点声明。\n---\n《书名》"],
                max_rework=0,
                run_id="run-stage-local-revision-fingerprint",
            )
            first_llm = QualityLoopLLM()
            build_graph(
                first_llm, None, max_rework=0, execution_config=execution
            ).invoke(state)  # type: ignore[arg-type]

            original = TranslatorAgent.REVISION_UNIT_REPAIR_SYSTEM_PROMPT
            try:
                TranslatorAgent.REVISION_UNIT_REPAIR_SYSTEM_PROMPT = (
                    original + "\n协议版本测试标记。"
                )
                second_llm = QualityLoopLLM()
                final = build_graph(
                    second_llm, None, max_rework=0, execution_config=execution
                ).invoke(state)  # type: ignore[arg-type]
            finally:
                TranslatorAgent.REVISION_UNIT_REPAIR_SYSTEM_PROMPT = original

        self.assertEqual(final["execution_stats"]["checkpoint_hits"], 5)
        self.assertEqual(final["execution_stats"]["submitted"], 1)
        self.assertEqual(second_llm.calls["revise"], 1)
        self.assertEqual(second_llm.calls["translate"], 0)
        self.assertEqual(second_llm.calls["editor"], 0)
        self.assertEqual(second_llm.calls["polisher"], 0)
        self.assertEqual(second_llm.calls["qa"], 0)

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
