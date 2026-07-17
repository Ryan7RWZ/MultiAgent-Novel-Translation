"""tests/test_glossary.py：术语库（GlossaryStore，SQLite）upsert / lookup 用例。

说明：
- 直接测试记忆层的术语库存储组件 ``mant.memory.glossary.GlossaryStore``，
  使用 tmp 目录下的独立 sqlite 文件，不污染仓库数据；
- 记忆层由其他负责人并行开发：模块未就绪、或以 NotImplementedError
  占位时，按团队约定 skip 而非报错；
- 使用 stdlib unittest（不用 pytest）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# src 布局：保证未安装 mant 包时也能被 unittest discover 导入
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:  # 记忆层存储组件（其他负责人的模块），缺失时整体 skip
    from mant.memory.glossary import GlossaryStore
    from mant.memory.models import TermEntry

    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - 取决于并行开发进度
    GlossaryStore = None  # type: ignore[assignment]
    TermEntry = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


@unittest.skipIf(GlossaryStore is None, f"mant.memory.glossary 未就绪：{_IMPORT_ERROR!r}")
class TestGlossaryStore(unittest.TestCase):
    """tmp 目录 sqlite 上验证术语 upsert 与 lookup（两个用例）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = GlossaryStore(Path(self._tmp.name) / "glossary.db")
        # 先关闭 sqlite 连接再删除临时目录（规避 Windows 文件锁）
        self.addCleanup(self.store.close)

    @staticmethod
    def _entry(
        source: str = "剑气",
        target: str = "Sword Qi",
        work_id: str = "w_demo",
        confidence: float = 0.9,
        category: str = "功法",
    ) -> "TermEntry":
        return TermEntry(
            source=source,
            target=target,
            category=category,
            work_id=work_id,
            confidence=confidence,
        )

    def test_upsert_then_lookup_roundtrip(self) -> None:
        """upsert 写入后，lookup 能按作品精确取回术语译法。"""
        try:
            written = self.store.upsert([
                self._entry(),
                self._entry(source="灵石", target="Spirit Stone", category="物品"),
            ])
            hits = self.store.lookup(["剑气", "灵石", "不存在的词"], "w_demo")
        except NotImplementedError:
            self.skipTest("GlossaryStore 尚未实现（骨架占位）")
        self.assertEqual(written, 2)
        self.assertEqual(set(hits), {"剑气", "灵石"})  # 未命中词不出现在结果中
        self.assertEqual(hits["剑气"].target, "Sword Qi")
        self.assertEqual(hits["剑气"].work_id, "w_demo")
        self.assertAlmostEqual(hits["剑气"].confidence, 0.9)

    def test_upsert_overwrites_and_lookup_isolated_by_work(self) -> None:
        """重复 upsert 覆盖同作品同术语；lookup 按 work_id 隔离。"""
        try:
            self.store.upsert([self._entry(target="Sword Qi", confidence=0.9)])
            # 冲突键 (source, work_id) 相同，后写覆盖先写
            self.store.upsert([self._entry(target="Sword Aura", confidence=0.95)])
            hits = self.store.lookup(["剑气"], "w_demo")
            other = self.store.lookup(["剑气"], "w_other")
        except NotImplementedError:
            self.skipTest("GlossaryStore 尚未实现（骨架占位）")
        self.assertEqual(hits["剑气"].target, "Sword Aura")
        self.assertAlmostEqual(hits["剑气"].confidence, 0.95)
        self.assertEqual(other, {})  # 术语按作品隔离，跨作品不可见

    def test_lookup_excludes_empty_unreviewed_targets(self) -> None:
        """空译名候选可留库待复核，但不能注入翻译 Prompt。"""
        self.store.upsert([
            self._entry(source="候选词", target="", category="offline", confidence=0.5)
        ])
        self.assertEqual(self.store.lookup(["候选词"], "w_demo"), {})

    def test_match_text_returns_only_present_usable_terms(self) -> None:
        self.store.upsert([
            self._entry(source="青玄宗", target="Azure Profound Sect"),
            self._entry(source="空候选", target="", category="offline"),
        ])
        hits = self.store.match_text("他拜入青玄宗。", "w_demo")
        self.assertEqual(set(hits), {"青玄宗"})


if __name__ == "__main__":
    unittest.main()
