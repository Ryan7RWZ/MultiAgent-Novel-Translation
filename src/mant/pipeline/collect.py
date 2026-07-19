"""M1 离线语料管道 · 第一步：语料采集（collect）。

职责：把原始语料导入为统一的 ``RawDocument`` 数据模型，供 clean / align 使用。

已实现：
    - ``Collector`` 抽象基类：约定 ``collect() -> list[RawDocument]``；
    - ``LocalTxtCollector``：本地 txt 语料导入（骨架阶段的主力语料来源）。

待办：
    - ``WebNovelCollector``：网络爬虫采集（仅留骨架，实现前必须阅读其 docstring
      顶部的版权合规红线）。

编码识别统一复用 ``mant.textio``；检测器不可用时仍保留常见编码降级路径。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mant.textio import read_text_file

__all__ = ["RawDocument", "Collector", "LocalTxtCollector", "WebNovelCollector"]


@dataclass
class RawDocument:
    """采集得到的一份原始文档（单语言、未清洗）。

    属性:
        work_id: 作品 ID（语料按作品隔离，与术语库/TM 的 work_id 一致）。
        doc_id: 文档 ID（文件名 stem）；同一作品的 src/tgt 文档按 doc_id 配对。
        role: 文档角色，``"src"`` 表示原文、``"tgt"`` 表示参考译文。
        lang: 语言代码（如 ``"zh"`` / ``"en"``）。
        title: 文档标题（缺省取 doc_id）。
        text: 原始文本全文（未清洗）。
        path: 来源路径或 URL（审计追溯用）。
        meta: 扩展元数据（网络采集时必须含 source_url / license / fetched_at）。
    """

    work_id: str
    doc_id: str
    role: str
    lang: str
    text: str
    title: str = ""
    path: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class Collector(abc.ABC):
    """语料采集器抽象基类。

    子类只需实现 ``collect``；采集器不做清洗/切章，原样返回文本，
    保持"原始语料不可变"原则，方便出问题后回溯。
    """

    name: str = "collector"

    @abc.abstractmethod
    def collect(self) -> list[RawDocument]:
        """执行采集，返回原始文档列表。"""
        raise NotImplementedError


class LocalTxtCollector(Collector):
    """本地 txt 语料导入器。

    目录约定（与 data/README.md 一致）::

        <raw_dir>/<work_id>/<src_role>/*.txt   # 原文，例：data/raw/demo/src/0001.txt
        <raw_dir>/<work_id>/<tgt_role>/*.txt   # 参考译文（可选），例：data/raw/demo/tgt/0001.txt

    - work_id 取一级子目录名；role 取二级目录名（默认 ``src`` / ``tgt``，可配置）；
    - doc_id 取文件名 stem，同一作品下 src/tgt 文件按 doc_id 一一配对（见 align 模块）；
    - ``raw_dir`` 根下的散文件会被忽略（必须放在作品子目录中，便于按作品隔离）。

    参数:
        raw_dir: 原始语料根目录。
        src_role / tgt_role: 原文 / 译文子目录名。
        src_lang / tgt_lang: 原文 / 译文语言代码。
        pattern: 文件 glob 模式，默认 ``*.txt``。
    """

    name: str = "local_txt"

    def __init__(
        self,
        raw_dir: str | Path,
        *,
        src_role: str = "src",
        tgt_role: str = "tgt",
        src_lang: str = "zh",
        tgt_lang: str = "en",
        pattern: str = "*.txt",
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.src_role = src_role
        self.tgt_role = tgt_role
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self.pattern = pattern

    def collect(self) -> list[RawDocument]:
        """扫描目录并导入全部 txt 文档；目录不存在时返回空列表（不抛异常）。"""
        docs: list[RawDocument] = []
        if not self.raw_dir.is_dir():
            return docs
        role_lang = ((self.src_role, self.src_lang), (self.tgt_role, self.tgt_lang))
        for work_dir in sorted(p for p in self.raw_dir.iterdir() if p.is_dir()):
            for role, lang in role_lang:
                role_dir = work_dir / role
                if not role_dir.is_dir():
                    continue
                for fp in sorted(role_dir.glob(self.pattern)):
                    if not fp.is_file():
                        continue
                    decoded = read_text_file(fp)
                    docs.append(
                        RawDocument(
                            work_id=work_dir.name,
                            doc_id=fp.stem,
                            role=role,
                            lang=lang,
                            title=fp.stem,
                            text=decoded.text,
                            path=str(fp),
                            meta={
                                "source_encoding": decoded.encoding,
                                "converted_to": "utf-8",
                            },
                        )
                    )
        return docs


class WebNovelCollector(Collector):
    """网络爬虫采集器（骨架，尚未实现）。

    ⚠️ 版权合规红线（任何实现必须逐条遵守，违者不得合入主干）：
        1. 仅采集已获得书面授权、公有领域、或明确允许抓取的开放授权（如 CC）作品；
        2. 严禁绕过付费墙、登录鉴权、DRM 或任何反爬技术保护措施；
        3. 严格遵守目标站点 robots.txt 与服务条款，请求间隔不低于 ``interval`` 秒，
           不得对站点造成负载压力；
        4. 采集结果仅用于学术研究 / 课程实验，不得二次分发、不得商用；
        5. 每条语料必须在 ``meta`` 中记录 ``source_url`` / ``license`` / ``fetched_at``，
           以便合规审计与追溯。

    TODO(待授权后实现):
        - 页面抓取（requests/httpx 延迟导入）与正文抽取（readability 规则）；
        - 增量采集与断点续传（已抓取 URL 落盘去重）；
        - 抓取结果写入 raw_dir，复用 LocalTxtCollector 的目录约定。
    """

    name: str = "web_novel"

    def __init__(
        self,
        start_urls: Iterable[str],
        *,
        raw_dir: str | Path,
        interval: float = 3.0,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.start_urls = list(start_urls)
        self.raw_dir = Path(raw_dir)
        self.interval = max(interval, 3.0)  # 限速下限，合规红线第 3 条
        self.meta = dict(meta or {})

    def collect(self) -> list[RawDocument]:
        """未实现：实现前必须逐条确认本类 docstring 的版权合规红线。"""
        raise NotImplementedError(
            "WebNovelCollector 尚未实现：请先确认版权授权与 robots.txt 合规，"
            "再补充抓取实现（见类 docstring 的版权合规红线与 TODO）。"
        )
