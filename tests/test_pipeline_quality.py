"""M1 对齐与人工术语资产的质量回归测试。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mant.pipeline.runner import run_pipeline


class TestPipelineQuality(unittest.TestCase):
    def test_auto_ratio_alignment_and_manual_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "raw" / "demo"
            (work / "src").mkdir(parents=True)
            (work / "tgt").mkdir(parents=True)
            (work / "src" / "0001.txt").write_text(
                "第一章 初见\n\n"
                "苍澜大陆东境，坐落着青玄宗。"
                "沈孤鸿站在落霞谷中。"
                "他取出了玄火镜。\n",
                encoding="utf-8",
            )
            (work / "tgt" / "0001.txt").write_text(
                "Chapter 1 First Encounter\n\n"
                "In the eastern reaches of the Canglan Continent stood the Azure Profound Sect. "
                "Shen Guhong waited in Sunset Valley. "
                "He took out the Profound Fire Mirror.\n",
                encoding="utf-8",
            )
            (work / "terminology.md").write_text(
                "| 源词 | 译法 | 类别 |\n"
                "| --- | --- | --- |\n"
                "| 苍澜大陆 | Canglan Continent | place |\n"
                "| 青玄宗 | Azure Profound Sect | place |\n"
                "| 沈孤鸿 | Shen Guhong | person |\n"
                "| 落霞谷 | Sunset Valley | place |\n"
                "| 玄火镜 | Profound Fire Mirror | artifact |\n",
                encoding="utf-8",
            )

            stats = run_pipeline(
                raw_dir=root / "raw",
                aligned_dir=root / "aligned",
                glossary_db=root / "terms.db",
                min_freq=99,
                log=lambda _: None,
            )
            self.assertEqual(stats["pairs"], 3)
            pairs = [
                json.loads(line)
                for line in (root / "aligned" / "demo.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertIn("Canglan Continent", pairs[0]["tgt"])
            self.assertIn("Shen Guhong", pairs[1]["tgt"])
            self.assertIn("Profound Fire Mirror", pairs[2]["tgt"])

            conn = sqlite3.connect(root / "terms.db")
            try:
                rows = dict(conn.execute("SELECT source, target FROM terms"))
            finally:
                conn.close()
            self.assertEqual(rows["沈孤鸿"], "Shen Guhong")
            self.assertEqual(rows["玄火镜"], "Profound Fire Mirror")
            conn = sqlite3.connect(root / "terms.db")
            try:
                tm_count = conn.execute("SELECT COUNT(*) FROM tm_pairs").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(tm_count, 3)


if __name__ == "__main__":
    unittest.main()
