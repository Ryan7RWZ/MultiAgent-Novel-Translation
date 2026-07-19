"""tests/test_baseline.py：M2 单 Agent 基线翻译器用例。

说明：
- 使用 FakeLLMClient 桩（不触网、不依赖 openai）+ tmp 目录的真实记忆层；
- 记忆层门面 MemoryHub 由其他负责人并行开发：优先使用真实 MemoryHub，
  未就绪时自动降级为"基于真实 GlossaryStore（tmp sqlite）的适配器"，
  二者都不可用时按团队约定 skip 而非报错；
- 使用 stdlib unittest（不用 pytest）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

# src 布局：保证未安装 mant 包时也能被 unittest discover 导入
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mant.baseline import BaselineTranslator  # 本任务模块必须始终可导入

try:  # 数据模型（其他负责人的模块），缺失时相关断言降级为 skip
    from mant.memory.models import TermEntry
except Exception:  # pragma: no cover - 取决于并行开发进度
    TermEntry = None  # type: ignore[assignment]


class FakeLLMClient:
    """LLMClient 测试桩：接口一致、确定性输出、记录每次调用的 prompt。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        self.calls.append({"system": system, "user": user})
        return f"[FAKE-TRANSLATION-{len(self.calls)}]"


class _GlossaryBackedMemory:
    """MemoryHub 未就绪时的临时适配器：

    ``lookup_terms`` / ``record_terms`` 走真实 ``GlossaryStore``（tmp sqlite），
    ``search_tm`` 返回空列表（TM 存储尚未落地）。

    TODO(记忆层联调): MemoryHub 落地后删除本适配器，测试统一走真实门面。
    """

    def __init__(self, tmpdir: str) -> None:
        from mant.memory.glossary import GlossaryStore

        self._store = GlossaryStore(Path(tmpdir) / "glossary.db")

    def lookup_terms(self, terms: list[str], work_id: str) -> dict[str, Any]:
        return self._store.lookup(terms, work_id)

    def record_terms(self, entries: list[Any]) -> None:
        self._store.upsert(entries)

    def search_tm(self, source_text: str, work_id: str, k: int = 5) -> list[Any]:
        return []

    def close(self) -> None:
        self._store.close()


def _build_memory(tmpdir: str) -> Any:
    """构造指向 tmp 目录的记忆层：优先真实 MemoryHub，其次适配器。"""
    mem_cfg = {
        "sqlite_path": str(Path(tmpdir) / "glossary.db"),
        "faiss_index_dir": str(Path(tmpdir) / "faiss"),
        # StoryBibleStore 构造时会立即 mkdir，必须一并指向 tmp，避免污染仓库
        "story_bible_dir": str(Path(tmpdir) / "story_bible"),
    }
    try:
        from mant.memory import MemoryHub
    except Exception:  # pragma: no cover - MemoryHub 尚未落地时走这里
        MemoryHub = None  # type: ignore[assignment]
    if MemoryHub is not None:
        if hasattr(MemoryHub, "from_config"):
            try:
                return MemoryHub.from_config(mem_cfg)
            except Exception:
                pass
        for args, kwargs in (
            ((), mem_cfg),
            ((mem_cfg["sqlite_path"],), {}),
        ):
            try:
                return MemoryHub(*args, **kwargs)
            except Exception:
                continue
    try:
        return _GlossaryBackedMemory(tmpdir)
    except Exception:  # pragma: no cover - 记忆层完全不可用
        return None


class TestBaselineTranslator(unittest.TestCase):
    """translate_chapter 返回结构 与 RAG 注入统计（两个用例）。"""

    WORK_ID = "w_baseline_test"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.memory = _build_memory(self._tmp.name)
        if self.memory is None:
            self.skipTest("记忆层（MemoryHub / GlossaryStore）均不可用")
        close = getattr(self.memory, "close", None)
        if callable(close):
            self.addCleanup(close)  # 先关连接再删临时目录（Windows 文件锁）
        self.llm = FakeLLMClient()
        self.translator = BaselineTranslator()
        self.chapter = Path(self._tmp.name) / "chapter_001.txt"
        self.chapter.write_bytes(
            "雪乃看着窗外的雪。\n\nYuki 没有说话，只是微微一笑。\n".encode(
                "gb18030"
            )
        )

    def test_translate_chapter_returns_expected_structure(self) -> None:
        """返回结构完整：segments / translations / injection_stats / prompt_preview。"""
        result = self.translator.translate_chapter(
            self.WORK_ID, self.chapter, self.memory, self.llm
        )
        # 约定键齐全
        for key in ("segments", "translations", "injection_stats", "prompt_preview"):
            self.assertIn(key, result)
        self.assertEqual(result["work_id"], self.WORK_ID)
        self.assertEqual(result["chapter_id"], "chapter_001")
        self.assertEqual(result["input_encoding"], "gb18030")
        # 空行分隔的两个段落；每段恰好一次 LLM 调用（单 Agent 直译的特征）
        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual(len(result["translations"]), len(result["segments"]))
        self.assertEqual(len(self.llm.calls), len(result["segments"]))
        self.assertTrue(
            all(t.startswith("[FAKE-TRANSLATION-") for t in result["translations"])
        )
        # prompt 预览为首段的完整用户 prompt
        self.assertIsInstance(result["prompt_preview"], str)
        self.assertIn("雪乃", result["prompt_preview"])
        # 注入统计字段齐全
        stats = result["injection_stats"]
        self.assertEqual(stats["segments_total"], 2)
        for field in (
            "terms_injected", "tm_injected",
            "segments_with_terms", "segments_with_tm",
        ):
            self.assertIn(field, stats)

    def test_injection_stats_reflect_term_hits(self) -> None:
        """术语注入：命中的约定译法写入 prompt，injection_stats 正确计数。"""
        record = getattr(self.memory, "record_terms", None)
        if not callable(record) or TermEntry is None:
            self.skipTest("记忆层不支持 record_terms，无法预置术语")
        try:
            record([
                TermEntry(
                    source="雪乃", target="Yukino", category="character",
                    work_id=self.WORK_ID, confidence=1.0,
                ),
                TermEntry(
                    source="Yuki", target="Yuki", category="character",
                    work_id=self.WORK_ID, confidence=1.0,
                ),
            ])
            seeded = self.memory.lookup_terms(["雪乃"], self.WORK_ID)
        except NotImplementedError:
            self.skipTest("记忆层术语接口尚未实现（骨架占位）")
        if not seeded:
            self.skipTest("记忆层 lookup_terms 未返回已写入术语（骨架未就绪）")

        result = self.translator.translate_chapter(
            self.WORK_ID, self.chapter, self.memory, self.llm
        )
        stats = result["injection_stats"]
        # 第 1 段命中"雪乃"，第 2 段命中"Yuki"；TM 为空库
        self.assertGreaterEqual(stats["terms_injected"], 2)
        self.assertEqual(stats["segments_with_terms"], 2)
        self.assertEqual(stats["tm_injected"], 0)
        # 注入的约定译法必须真实出现在对应段的用户 prompt 中
        self.assertIn("雪乃 → Yukino", self.llm.calls[0]["user"])
        self.assertIn("Yuki", self.llm.calls[1]["user"])


if __name__ == "__main__":
    unittest.main()
