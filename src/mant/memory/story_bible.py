"""小说圣经（Story Bible）存储：按 work_id 以 JSON 文件持久化。

设计说明：
- 每部作品对应一个 JSON 文件：``<root_dir>/<work_id>.json``，
  内容即 :class:`~mant.memory.models.StoryBible` 的 ``to_dict`` 结果。
- 选用 JSON 文件而非 sqlite：小说圣经需要频繁整体读写给 LLM 做上下文，
  且需要人工可审阅、可纳入 git diff，文件化更合适。
- ``merge_chapter_summary`` 当前为骨架实现（仅追加时间线），
  人物 / 设定的 LLM 增量抽取留作 TODO。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import StoryBible

__all__ = ["StoryBibleStore"]

# 文件名安全字符白名单：避免 work_id 中的路径分隔符造成目录穿越
_SAFE_CHARS = re.compile(r"[^\w\-.]")


class StoryBibleStore:
    """小说圣经存储：负责 StoryBible 的加载、保存与章节摘要的增量合并。

    参数:
        root_dir: JSON 文件根目录；不存在时自动创建。
    """

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _path_for(self, work_id: str) -> Path:
        """把 work_id 映射为安全的 JSON 文件路径（防目录穿越）。"""
        safe_name = _SAFE_CHARS.sub("_", work_id)
        return self.root_dir / f"{safe_name}.json"

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def load(self, work_id: str) -> StoryBible:
        """加载指定作品的小说圣经；文件不存在时返回空的 StoryBible。"""
        path = self._path_for(work_id)
        if not path.exists():
            return StoryBible(work_id=work_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        bible = StoryBible.from_dict(data)
        bible.work_id = work_id  # 以入参为准，避免文件内字段漂移
        return bible

    def save(self, bible: StoryBible) -> None:
        """整体保存小说圣经（覆盖写，ensure_ascii=False 保留中文可读性）。"""
        path = self._path_for(bible.work_id)
        path.write_text(
            json.dumps(bible.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def merge_chapter_summary(
        self, work_id: str, chapter_id: str, summary: str
    ) -> StoryBible:
        """增量合并一章的摘要信息到小说圣经（骨架实现）。

        当前行为：仅把 ``{"chapter_id", "summary"}`` 追加到 timeline。

        TODO(剧情抽取)：调用 LLM 从 summary 中抽取新出场人物 / 新设定，
        增量合并进 characters / settings：
        - 人物按 name 去重，aliases 做别名归并（同人多译名问题）；
        - 设定按 topic 去重，内容冲突时保留最新章节版本并记录来源章节；
        - 抽取结果需经术语库校准译名后再落库。
        """
        bible = self.load(work_id)
        bible.timeline.append({"chapter_id": chapter_id, "summary": summary})
        self.save(bible)
        return bible
