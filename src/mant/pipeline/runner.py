"""M1 离线语料管道的可复用编排入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from mant.memory.glossary import GlossaryStore
from mant.memory.tm import TMStore
from mant.pipeline.align import align_documents, write_jsonl
from mant.pipeline.clean import clean_document, split_chapters
from mant.pipeline.collect import LocalTxtCollector, RawDocument
from mant.pipeline.extract_terms import extract_terms_for_work, load_manual_terminology

__all__ = ["run_pipeline"]

LogFn = Callable[[str], None]


def _group_by_work(
    docs: list[RawDocument],
) -> dict[str, dict[str, list[RawDocument]]]:
    works: dict[str, dict[str, list[RawDocument]]] = {}
    for doc in docs:
        works.setdefault(doc.work_id, {}).setdefault(doc.role, []).append(doc)
    return works


def run_pipeline(
    *,
    raw_dir: str | Path = "data/raw",
    aligned_dir: str | Path = "data/aligned",
    glossary_db: str | Path = "data/memory/mant.db",
    src_lang: str = "zh",
    tgt_lang: str = "en",
    top_k: int = 200,
    min_freq: int = 3,
    llm: Any | None = None,
    log: LogFn = print,
) -> dict[str, Any]:
    """执行 collect → clean → align → extract，并返回结构化统计。"""
    raw_root = Path(raw_dir)
    aligned_root = Path(aligned_dir)
    collector = LocalTxtCollector(raw_root, src_lang=src_lang, tgt_lang=tgt_lang)
    docs = collector.collect()
    works = _group_by_work(docs)
    n_src = sum(d.role == collector.src_role for d in docs)
    n_tgt = sum(d.role == collector.tgt_role for d in docs)
    log(
        f"[M1][1/4 collect] 作品 {len(works)} 部，文档 {len(docs)} 个"
        f"（原文 {n_src} / 译文 {n_tgt}）<- {raw_root}"
    )
    if not docs:
        raise ValueError("未发现语料；请按 data/README.md 放置双语 txt 文件。")

    cleaned: dict[tuple[str, str, str], str] = {}
    stat_sum: dict[str, int] = {}
    n_chapters = 0
    for doc in docs:
        result = clean_document(doc.text)
        cleaned[(doc.work_id, doc.role, doc.doc_id)] = result.text
        n_chapters += len(split_chapters(result.text))
        for key, value in result.stats.items():
            if isinstance(value, int):
                stat_sum[key] = stat_sum.get(key, 0) + value
    log(
        f"[M1][2/4 clean] 清洗文档 {len(docs)} 个，切出章节 {n_chapters} 个；"
        f"删广告行 {stat_sum.get('removed_ad_lines', 0)}、"
        f"乱码行 {stat_sum.get('removed_garbled_lines', 0)}、"
        f"重复行 {stat_sum.get('removed_dup_lines', 0)}"
    )

    pair_counts: dict[str, int] = {}
    pairs_by_work: dict[str, list[Any]] = {}
    for work_id, roles in sorted(works.items()):
        tgt_by_id = {doc.doc_id: doc for doc in roles.get(collector.tgt_role, [])}
        work_pairs = []
        for src_doc in roles.get(collector.src_role, []):
            tgt_doc = tgt_by_id.get(src_doc.doc_id)
            if tgt_doc is None:
                continue
            work_pairs.extend(
                align_documents(
                    cleaned[(work_id, collector.src_role, src_doc.doc_id)],
                    cleaned[(work_id, collector.tgt_role, tgt_doc.doc_id)],
                    src_lang=src_lang,
                    tgt_lang=tgt_lang,
                    # 默认按章节自动估算字符比，避免固定比例导致系统性错位。
                    char_ratio=None,
                )
            )
        pair_counts[work_id] = len(work_pairs)
        pairs_by_work[work_id] = work_pairs
        if work_pairs:
            out_path = aligned_root / f"{work_id}.jsonl"
            write_jsonl(work_pairs, out_path)
            log(f"[M1][3/4 align] 作品 {work_id}：句对 {len(work_pairs)} 条 -> {out_path}")
        else:
            log(f"[M1][3/4 align] 作品 {work_id}：无双语配对文档，跳过对齐。")

    # 对齐句对与术语写入同一个运行时 SQLite，供 MemoryHub 直接消费。
    with TMStore(glossary_db) as tm_store:
        for work_id, work_pairs in pairs_by_work.items():
            tm_store.replace_pairs(
                [(pair.src, pair.tgt) for pair in work_pairs], work_id
            )

    term_stats: dict[str, dict[str, Any]] = {}
    with GlossaryStore(glossary_db) as store:
        for work_id, roles in sorted(works.items()):
            src_docs = roles.get(collector.src_role, [])
            if not src_docs:
                continue
            chapters: list[str] = []
            for doc in src_docs:
                text = cleaned[(work_id, doc.role, doc.doc_id)]
                chapters.extend(chapter.text for chapter in split_chapters(text))
            manual_entries = load_manual_terminology(
                raw_root / work_id / "terminology.md", work_id
            )
            stats = extract_terms_for_work(
                chapters,
                work_id,
                llm=llm,
                store=store,
                lang=src_lang,
                top_k=top_k,
                min_freq=min_freq,
                # 已有可信人工术语时，不再把空译名 TF-IDF 候选写入可用术语库。
                offline_fallback=not manual_entries,
            )
            if manual_entries:
                store.delete_empty_offline(work_id)
            manual_saved = store.upsert(manual_entries) if manual_entries else 0
            stats["manual_terms"] = manual_saved
            stats["saved_total"] = int(stats["saved"]) + manual_saved
            term_stats[work_id] = stats
            log(
                f"[M1][4/4 extract] 作品 {work_id}：候选 {stats['candidates']}、"
                f"自动入库 {stats['saved']}、人工术语 {manual_saved}"
            )

    summary = {
        "works": len(works),
        "documents": len(docs),
        "chapters": n_chapters,
        "pairs": sum(pair_counts.values()),
        "pair_counts": pair_counts,
        "term_stats": term_stats,
        "glossary_db": str(glossary_db),
    }
    log(
        f"[M1] 管道完成：{summary['pairs']} 个句对，"
        f"术语库 -> {summary['glossary_db']}"
    )
    return summary
