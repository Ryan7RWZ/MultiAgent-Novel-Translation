"""tests/test_pipeline_clean.py：M1 语料清洗纯函数用例。

说明：
- 目标函数由管道负责人在 ``mant.pipeline.clean`` 中实现（``clean_text`` /
  ``clean`` / ``clean_line`` 任一命名均可，本测试动态解析）；
- 模块未就绪、函数未定义、或以 NotImplementedError 占位时，按团队约定
  skip 而非报错；
- 使用 stdlib unittest（不用 pytest）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# src 布局：保证未安装 mant 包时也能被 unittest discover 导入
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _resolve_clean():
    """动态解析管道清洗纯函数；模块或函数不存在时返回 None。"""
    try:
        from mant.pipeline import clean as clean_mod
    except Exception:  # pragma: no cover - 取决于并行开发进度
        return None
    for name in ("clean_text", "clean", "clean_line"):
        fn = getattr(clean_mod, name, None)
        if callable(fn):
            return fn
    return None


_CLEAN = _resolve_clean()


@unittest.skipIf(_CLEAN is None, "mant.pipeline.clean 的清洗纯函数未就绪")
class TestPipelineClean(unittest.TestCase):
    """clean 纯函数：噪声去除 / 空白规整 与 幂等性（两个用例）。"""

    def _clean(self, text: str) -> str:
        """调用清洗函数；骨架占位（NotImplementedError）时 skip。"""
        try:
            return _CLEAN(text)
        except NotImplementedError:
            self.skipTest("clean 纯函数尚未实现（骨架占位）")

    def test_clean_removes_ad_lines_and_collapses_blank_lines(self) -> None:
        """去广告行 / 去重复行 / 压缩连续空行，正文内容保留（降噪不改写）。"""
        raw = (
            "第一章 风雪夜归人\r\n"
            "少年握紧手中的剑，眼中闪过一丝决然。\n"
            "请记住本站域名 www.example-novel.com\n"  # 广告 / 水印行
            "求月票，求推荐票！\n"                     # 求票行
            "\n\n\n"
            "雪落无声。\n"
            "雪落无声。\n"  # 分页抓取常见的重复行
        )
        out = self._clean(raw)
        self.assertIsInstance(out, str)
        self.assertNotIn("请记住本站域名", out)   # 广告行被整行删除
        self.assertNotIn("求月票", out)           # 求票行被整行删除
        self.assertNotIn("\r", out)               # 行尾 \r 被规整
        self.assertNotIn("\n\n\n", out)           # 连续空行被压缩
        self.assertIn("少年握紧手中的剑", out)     # 正文保留且不改写
        self.assertEqual(out.count("雪落无声。"), 1)  # 重复行去重，保留首次出现

    def test_clean_idempotent_and_empty_input(self) -> None:
        """空输入安全返回空串；清洗幂等（二次清洗结果不再变化）。"""
        self.assertEqual(self._clean(""), "")
        once = self._clean(" 你好，世界！ \n\n")
        self.assertEqual(self._clean(once), once)


if __name__ == "__main__":
    unittest.main()
