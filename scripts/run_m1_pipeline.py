#!/usr/bin/env python
"""M1 离线语料管道一键运行脚本：collect → clean → align → extract_terms。

用法::

    python scripts/run_m1_pipeline.py \
        --raw-dir data/raw \
        --aligned-dir data/aligned \
        --glossary-db data/glossary/terms.sqlite3

说明：
- 全流程仅需标准库即可跑通（LLM 复核默认关闭，候选词按"未复核"入库策略处理）；
- ``--with-llm`` 开启 LLM 术语复核：需安装 openai 并配置 API key（第三方库延迟导入，
  未安装/未配置时自动降级为占位，流程不中断）；
- 输入目录规范与版权合规要求见 ``data/README.md``；
- 本脚本是唯一允许直接读配置文件的入口（``--config``，需 pyyaml，同样延迟导入）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# src 布局：未 pip install 时也能直接运行本脚本
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mant.pipeline.align import align_documents, write_jsonl  # noqa: E402
from mant.pipeline.clean import clean_document, split_chapters  # noqa: E402
from mant.pipeline.collect import LocalTxtCollector, RawDocument  # noqa: E402
from mant.pipeline.extract_terms import extract_terms_for_work  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """定义并解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="M1 离线语料管道：采集 → 清洗 → 句对齐 → 术语抽取",
    )
    parser.add_argument("--raw-dir", default="data/raw",
                        help="原始语料根目录（默认 data/raw，结构见 data/README.md）")
    parser.add_argument("--aligned-dir", default="data/aligned",
                        help="句对齐 JSONL 输出目录（默认 data/aligned）")
    parser.add_argument("--glossary-db", default="data/glossary/terms.sqlite3",
                        help="术语库 sqlite 路径（默认 data/glossary/terms.sqlite3）")
    parser.add_argument("--src-lang", default="zh", help="源语言代码（默认 zh）")
    parser.add_argument("--tgt-lang", default="en", help="目标语言代码（默认 en）")
    parser.add_argument("--top-k", type=int, default=200, help="每作品术语候选上限（默认 200）")
    parser.add_argument("--min-freq", type=int, default=3, help="候选词最低词频（默认 3）")
    parser.add_argument("--with-llm", action="store_true",
                        help="开启 LLM 术语复核（需 openai + API key，未配置自动降级）")
    parser.add_argument("--config", default=None,
                        help="YAML 配置路径（可选，读取 llm.providers.*，需 pyyaml）")
    return parser.parse_args(argv)


def _open_glossary_store(db_path: str):
    """打开统一术语库（mant.memory.glossary.GlossaryStore，stdlib sqlite3 实现）。

    记忆层不可用时返回 None 并打印提示（术语抽取结果跳过入库，流程不中断）。
    """
    try:
        from mant.memory.glossary import GlossaryStore  # 记忆层统一存储
    except ImportError as exc:
        print(f"[M1][WARN] 无法导入 mant.memory.glossary（{exc}），术语结果将跳过入库。")
        return None
    return GlossaryStore(db_path)


def _load_llm(config_path: str | None):
    """按需构造 LLMClient（延迟导入 openai/pyyaml 相关链路）。

    - 未传 --with-llm：直接返回 None（离线模式，术语按未复核透传）；
    - 未安装 pyyaml / openai、未配置 API key：LLMClient 内部自动降级为
      [DRAFT] 占位响应，本函数不抛异常。
    """
    cfg: dict = {}
    if config_path:
        try:
            import yaml  # 延迟导入第三方库 pyyaml
        except ImportError:
            print("[M1][WARN] 未安装 pyyaml（pip install pyyaml），--config 被忽略，使用空配置。")
        else:
            cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    try:
        from mant.llm.client import LLMClient  # 统一 LLM 接口（其内部再延迟导入 openai）
    except ImportError as exc:
        print(f"[M1][WARN] 无法导入 mant.llm.client（{exc}），跳过 LLM 复核。")
        return None
    return LLMClient.from_config(cfg)


def _group_by_work(docs: list[RawDocument]) -> dict[str, dict[str, list[RawDocument]]]:
    """把文档按 work_id → role 分组，便于逐作品处理。"""
    works: dict[str, dict[str, list[RawDocument]]] = {}
    for doc in docs:
        works.setdefault(doc.work_id, {}).setdefault(doc.role, []).append(doc)
    return works


def main(argv: list[str] | None = None) -> int:
    """串联四步并打印每步统计，返回进程退出码。"""
    args = _parse_args(argv)
    raw_dir = Path(args.raw_dir)
    aligned_dir = Path(args.aligned_dir)

    # ---------------- 第 1 步：采集 ----------------
    collector = LocalTxtCollector(raw_dir, src_lang=args.src_lang, tgt_lang=args.tgt_lang)
    docs = collector.collect()
    works = _group_by_work(docs)
    n_src = sum(1 for d in docs if d.role == collector.src_role)
    n_tgt = sum(1 for d in docs if d.role == collector.tgt_role)
    print(f"[M1][1/4 collect] 作品 {len(works)} 部，文档 {len(docs)} 个"
          f"（原文 {n_src} / 译文 {n_tgt}）<- {raw_dir}")
    if not docs:
        print("[M1] 未发现语料：请按 data/README.md 的目录规范放置 txt 后重试。")
        return 1

    # ---------------- 第 2 步：清洗 ----------------
    cleaned: dict[tuple[str, str, str], str] = {}  # (work_id, role, doc_id) -> 清洗后文本
    stat_sum: dict[str, int] = {}
    n_chapters = 0
    for doc in docs:
        result = clean_document(doc.text)
        cleaned[(doc.work_id, doc.role, doc.doc_id)] = result.text
        n_chapters += len(split_chapters(result.text))
        for key, val in result.stats.items():
            if isinstance(val, int):
                stat_sum[key] = stat_sum.get(key, 0) + val
        ratio = result.stats.get("traditional_ratio", 0.0)
        if isinstance(ratio, float) and ratio > 0.05:
            print(f"[M1][WARN] {doc.path} 疑似繁体语料（特征字比例 {ratio:.2%}），"
                  f"建议人工确认或后续接入 OpenCC 转换。")
    print(f"[M1][2/4 clean] 清洗文档 {len(docs)} 个，切出章节 {n_chapters} 个；"
          f"删广告行 {stat_sum.get('removed_ad_lines', 0)}、"
          f"乱码行 {stat_sum.get('removed_garbled_lines', 0)}、"
          f"空行 {stat_sum.get('removed_blank_lines', 0)}、"
          f"重复行 {stat_sum.get('removed_dup_lines', 0)}")

    # ---------------- 第 3 步：句对齐 ----------------
    total_pairs = 0
    for work_id, roles in sorted(works.items()):
        tgt_by_id = {d.doc_id: d for d in roles.get(collector.tgt_role, [])}
        work_pairs = []
        for src_doc in roles.get(collector.src_role, []):
            tgt_doc = tgt_by_id.get(src_doc.doc_id)
            if tgt_doc is None:
                continue  # 无配对译文，跳过（并在下方统计）
            work_pairs.extend(align_documents(
                cleaned[(work_id, collector.src_role, src_doc.doc_id)],
                cleaned[(work_id, collector.tgt_role, tgt_doc.doc_id)],
                src_lang=args.src_lang,
                tgt_lang=args.tgt_lang,
            ))
        if work_pairs:
            out_path = aligned_dir / f"{work_id}.jsonl"
            write_jsonl(work_pairs, out_path)
            total_pairs += len(work_pairs)
            print(f"[M1][3/4 align] 作品 {work_id}：句对 {len(work_pairs)} 条 -> {out_path}")
        else:
            print(f"[M1][3/4 align] 作品 {work_id}：无双语配对文档，跳过对齐。")
    print(f"[M1][3/4 align] 合计输出句对 {total_pairs} 条 -> {aligned_dir}")

    # ---------------- 第 4 步：术语抽取 ----------------
    llm = _load_llm(args.config) if args.with_llm else None
    store = _open_glossary_store(args.glossary_db)
    total_saved = 0
    try:
        for work_id, roles in sorted(works.items()):
            src_docs = roles.get(collector.src_role, [])
            if not src_docs:
                continue
            # 以章节为文档单位做 TF-IDF（IDF 更稳定）
            chapters: list[str] = []
            for d in src_docs:
                chapters.extend(ch.text for ch in split_chapters(cleaned[(work_id, d.role, d.doc_id)]))
            stats = extract_terms_for_work(
                chapters, work_id,
                llm=llm, store=store, lang=args.src_lang,
                top_k=args.top_k, min_freq=args.min_freq,
            )
            total_saved += int(stats["saved"])
            print(f"[M1][4/4 extract] 作品 {work_id}：候选 {stats['candidates']}、"
                  f"复核 {stats['reviewed']}、入库 {stats['saved']}")
    finally:
        if hasattr(store, "close"):
            store.close()
    print(f"[M1][4/4 extract] 合计入库术语 {total_saved} 条 -> {args.glossary_db}"
          + ("" if args.with_llm else "（未开启 --with-llm，仅候选统计）"))

    print("[M1] 管道运行完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
