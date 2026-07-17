"""M1 离线语料管道子包（pipeline）。

流水线四步：
    采集 collect → 清洗 clean → 句对齐 align → 术语抽取 extract_terms

约定：
- 全部模块仅依赖标准库（第三方库一律延迟导入），保证骨架阶段即可导入、可单测；
- 模块之间通过 dataclass 数据模型传递数据，不直接读写全局配置；
- 各模块通过参数接收配置/路径，配置文件只在 scripts/run_m1_pipeline.py 入口解析；
- LLM 复核、术语入库分别对接统一接口 ``mant.llm.client.LLMClient`` 与
  ``mant.memory.glossary.GlossaryStore``（本包不重复定义这些接口）。
"""

from __future__ import annotations

from .collect import Collector, LocalTxtCollector, RawDocument, WebNovelCollector
from .clean import (
    Chapter,
    CleanResult,
    clean_document,
    clean_text,
    dedup_lines,
    detect_traditional,
    normalize_blank_lines,
    remove_ad_lines,
    remove_garbled,
    split_chapters,
)
from .align import (
    SentencePair,
    align_documents,
    align_sentences,
    estimate_char_ratio,
    pair_chapters,
    parse_chapter_number,
    read_jsonl,
    split_sentences,
    write_jsonl,
)
from .extract_terms import (
    TERM_REVIEW_SYSTEM_PROMPT,
    TERM_REVIEW_USER_TEMPLATE,
    TermCandidate,
    build_term_entries,
    extract_terms_for_work,
    llm_review_candidates,
    load_manual_terminology,
    tfidf_candidates,
)
from .runner import run_pipeline

__all__ = [
    # collect
    "Collector",
    "LocalTxtCollector",
    "RawDocument",
    "WebNovelCollector",
    # clean
    "Chapter",
    "CleanResult",
    "clean_document",
    "clean_text",
    "dedup_lines",
    "detect_traditional",
    "normalize_blank_lines",
    "remove_ad_lines",
    "remove_garbled",
    "split_chapters",
    # align
    "SentencePair",
    "align_documents",
    "align_sentences",
    "estimate_char_ratio",
    "pair_chapters",
    "parse_chapter_number",
    "read_jsonl",
    "split_sentences",
    "write_jsonl",
    # extract_terms
    "TERM_REVIEW_SYSTEM_PROMPT",
    "TERM_REVIEW_USER_TEMPLATE",
    "TermCandidate",
    "build_term_entries",
    "extract_terms_for_work",
    "llm_review_candidates",
    "load_manual_terminology",
    "tfidf_candidates",
    "run_pipeline",
]
