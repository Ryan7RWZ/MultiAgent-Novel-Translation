#!/usr/bin/env python
"""M1 管道产出验证脚本（仅标准库）：aligned JSONL + 术语库 sqlite 逐项体检。

用法::

    python scripts/verify_m1_output.py \
        --aligned-dir data/aligned \
        --glossary-db data/memory/mant.db \
        --terminology docs/sample-terminology.md \
        --min-chapters 3

检查项（逐项打印 PASS/FAIL，结尾输出总结）：
    1. aligned 目录存在且至少包含一个 ``*.jsonl`` 句对文件；
    2. 逐行 ``json.loads`` 校验：每行必须是含必需键 ``{src, tgt, chapter, index}`` 的对象；
    3. 句对/章节统计：句对总数 > 0，且章节总数（按作品内去重累计）>= ``--min-chapters``；
    4. 术语库可打开、``terms`` 表存在，并统计总条数；
    5. 检查对齐句对已经同步写入运行库 ``tm_pairs``；
    6. 统计术语库非空译名率，防止把空译名候选误当成可用术语资产；
    7. （可选）检查 Markdown 术语表源词在 ``terms`` 表中的命中率；
    8. （可选）以人工术语对作为跨语言语义锚点，检查源句含术语时译句是否
       含约定译名，用于自动发现长度对齐造成的系统性错位。

退出码：0 = 全部 PASS；1 = 存在 FAIL 或输入路径有误。
输入目录/文件不存在时打印清晰中文报错（不抛 traceback）。

第三方依赖规则：本脚本仅使用标准库，可在托管 Python（stdlib+numpy）环境直接运行。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

# 句对 JSONL 每行必需键（与 mant.pipeline.align.SentencePair.to_dict 的约定一致）
REQUIRED_KEYS = ("src", "tgt", "chapter", "index")

# 术语命中率及格线（命中条数 / 术语表解析出的源词条数）
MIN_HIT_RATE = 0.5
MIN_NONEMPTY_TARGET_RATE = 0.5
MIN_ANCHOR_MATCH_RATE = 0.8

# 单个检查项最多展示的样例条数（坏行 / 未命中词条），避免刷屏
MAX_SAMPLES = 10

# Markdown 表格分隔行单元格，如 --- / :--- / ---:
_MD_TABLE_SEP_CELL_RX = re.compile(r"^:?-{2,}:?$")
# Markdown 列表项（- / * / + / 1. / 1) 开头）
_MD_LIST_ITEM_RX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
# 列表项内"源词 → 译法"的候选分隔符（按优先级从左到右匹配）
_TERM_SEPARATORS = ("→", "->", "：", ":", "—", "–", "=")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """定义并解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="M1 管道产出验证：句对齐 JSONL + 术语库 sqlite（仅标准库）",
    )
    parser.add_argument("--aligned-dir", default="data/aligned",
                        help="句对齐 JSONL 目录（默认 data/aligned，与 run_m1_pipeline.py 一致）")
    parser.add_argument("--glossary-db", default="data/memory/mant.db",
                        help="术语/TM 运行库 sqlite 路径（默认 data/memory/mant.db）")
    parser.add_argument("--terminology", default=None,
                        help="Markdown 术语表路径（可选；提供时检查源词在 terms 表中的命中率）")
    parser.add_argument("--min-chapters", type=int, default=3,
                        help="最少章节数（默认 3，按作品内去重后累计）")
    parser.add_argument(
        "--min-nonempty-target-rate", type=float, default=MIN_NONEMPTY_TARGET_RATE,
        help="术语库非空译名率下限（默认 0.5）",
    )
    parser.add_argument(
        "--min-anchor-match-rate", type=float, default=MIN_ANCHOR_MATCH_RATE,
        help="术语锚点对齐命中率下限（默认 0.8，仅提供 --terminology 时检查）",
    )
    return parser.parse_args(argv)


def _fail_input(msg: str) -> int:
    """输入路径类错误：打印清晰中文报错并返回退出码 1（不抛 traceback）。"""
    print(f"错误：{msg}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# 检查 1~3：aligned JSONL
# ---------------------------------------------------------------------------


def _scan_aligned(
    aligned_dir: Path,
) -> tuple[list[str], dict[str, dict[str, int]], list[dict[str, object]]]:
    """逐行扫描全部 JSONL，返回 (坏行样例列表, {作品: {章节: 句对数}})。

    坏行样例格式：``文件名:行号 原因``，最多保留 ``MAX_SAMPLES`` 条；
    完整坏行数通过返回的列表长度无法得知时，以样例末条标注"等 N 处"。
    """
    bad_lines: list[str] = []
    n_bad = 0
    # work_id -> chapter_title -> pair_count
    stats: dict[str, dict[str, int]] = {}
    pairs: list[dict[str, object]] = []

    for path in sorted(aligned_dir.glob("*.jsonl")):
        work_id = path.stem
        chapters = stats.setdefault(work_id, {})
        try:
            f = path.open("r", encoding="utf-8")
        except OSError as exc:
            bad_lines.append(f"{path.name} 无法读取：{exc}")
            n_bad += 1
            continue
        with f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue  # 空行跳过（与 mant.pipeline.align.read_jsonl 行为一致）
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    n_bad += 1
                    if len(bad_lines) < MAX_SAMPLES:
                        bad_lines.append(f"{path.name}:{lineno} JSON 解析失败（{exc.msg}）")
                    continue
                if not isinstance(obj, dict):
                    n_bad += 1
                    if len(bad_lines) < MAX_SAMPLES:
                        bad_lines.append(f"{path.name}:{lineno} 行内容不是 JSON 对象")
                    continue
                missing = [k for k in REQUIRED_KEYS if k not in obj]
                if missing:
                    n_bad += 1
                    if len(bad_lines) < MAX_SAMPLES:
                        bad_lines.append(f"{path.name}:{lineno} 缺少必需键 {missing}")
                    continue
                chapter = str(obj["chapter"]).strip() or "（无章节标题）"
                chapters[chapter] = chapters.get(chapter, 0) + 1
                pairs.append(obj)
    if n_bad > len(bad_lines):
        bad_lines.append(f"…… 其余坏行从略，共 {n_bad} 处")
    return bad_lines, stats, pairs


# ---------------------------------------------------------------------------
# 检查 4：术语库 terms 表
# ---------------------------------------------------------------------------


def _open_glossary_ro(db_path: Path) -> sqlite3.Connection:
    """以只读模式打开术语库（避免误写；文件不存在时 sqlite3 不会静默建库）。"""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _count_terms(conn: sqlite3.Connection) -> int | None:
    """查询 terms 表总条数；表不存在返回 None，其它错误向上抛出。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='terms'"
    ).fetchone()
    if row is None:
        return None
    return int(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0])


def _load_term_sources(conn: sqlite3.Connection) -> set[str]:
    """读出 terms 表全部 source 词，供命中率比对（骨架规模下全量读入足够）。"""
    return {str(r[0]) for r in conn.execute("SELECT source FROM terms")}


def _count_nonempty_targets(conn: sqlite3.Connection) -> int:
    """统计已有可用译名（去除纯空白）的术语条数。"""
    return int(
        conn.execute("SELECT COUNT(*) FROM terms WHERE TRIM(target) <> ''").fetchone()[0]
    )


def _count_tm_pairs(conn: sqlite3.Connection) -> int | None:
    """统计运行库 TM 句对；表不存在返回 None。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tm_pairs'"
    ).fetchone()
    if row is None:
        return None
    return int(conn.execute("SELECT COUNT(*) FROM tm_pairs").fetchone()[0])


# ---------------------------------------------------------------------------
# 检查 5：Markdown 术语表源词命中率
# ---------------------------------------------------------------------------


def _clean_term(text: str) -> str:
    """去除词条首尾空白与 Markdown 强调/代码标记（**、__、*、`）。"""
    text = text.strip()
    text = re.sub(r"^[*_`]+", "", text)
    text = re.sub(r"[*_`]+$", "", text)
    return text.strip()


def parse_terminology_md(md_path: Path) -> list[str]:
    """从 Markdown 术语表解析源词列表（去重、保序）。

    支持两种常见排版：
        1. 表格：``| 源词 | 译法 | … |`` —— 取首列（表头与 ``|---|`` 分隔行自动跳过）；
        2. 列表：``- 源词 → 译法`` / ``- **源词**：译法`` —— 取分隔符前部分；
           无分隔符的裸列表项整项按源词处理。
    """
    terms: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        term = _clean_term(term)
        if term and term not in seen:
            seen.add(term)
            terms.append(term)

    lines = md_path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("|"):
            # 收集连续表格块
            block: list[list[str]] = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                block.append(cells)
                i += 1
            # 定位分隔行（如 |---|---|），其上一行为表头，两者均跳过
            sep_idx = next(
                (k for k, cells in enumerate(block)
                 if cells and all(_MD_TABLE_SEP_CELL_RX.match(c) for c in cells if c)),
                None,
            )
            for k, cells in enumerate(block):
                if k == sep_idx or (sep_idx is not None and k == sep_idx - 1):
                    continue
                if cells and cells[0]:
                    _add(cells[0])
            continue
        m = _MD_LIST_ITEM_RX.match(line)
        if m:
            content = m.group(1).strip()
            term = content
            for sep in _TERM_SEPARATORS:
                if sep in content:
                    term = content.split(sep, 1)[0]
                    break
            _add(term)
        i += 1
    return terms


def parse_terminology_pairs(md_path: Path) -> list[tuple[str, str]]:
    """从 Markdown 表格/列表中解析 ``(源词, 译名)``，用于对齐锚点质检。"""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in md_path.read_text(encoding="utf-8").splitlines():
        source = target = ""
        if line.lstrip().startswith("|"):
            cells = [_clean_term(c) for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2:
                source, target = cells[0], cells[1]
            if (
                source in {"源词", "source", "Source"}
                or not source
                or all(ch in "-:" for ch in source)
            ):
                continue
        else:
            match = _MD_LIST_ITEM_RX.match(line)
            if not match:
                continue
            content = match.group(1).strip()
            for separator in _TERM_SEPARATORS:
                if separator in content:
                    source, target = content.split(separator, 1)
                    source, target = _clean_term(source), _clean_term(target)
                    break
        if source and target and source not in seen:
            seen.add(source)
            pairs.append((source, target))
    return pairs


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """逐项执行检查并打印 PASS/FAIL，返回进程退出码（0 全过 / 1 有 FAIL）。"""
    args = _parse_args(argv)
    aligned_dir = Path(args.aligned_dir)
    db_path = Path(args.glossary_db)

    # ---- 输入路径预检：不存在则中文报错退出（不抛 traceback） ----
    if not aligned_dir.is_dir():
        return _fail_input(
            f"aligned 目录不存在：{aligned_dir}。"
            f"请先运行 scripts/run_m1_pipeline.py 生成产物，或检查 --aligned-dir 参数。"
        )
    if not db_path.is_file():
        return _fail_input(
            f"术语库文件不存在：{db_path}。"
            f"请确认 M1 管道第 4 步（extract_terms）已执行，或检查 --glossary-db 参数。"
        )
    md_path: Path | None = None
    if args.terminology is not None:
        md_path = Path(args.terminology)
        if not md_path.is_file():
            return _fail_input(
                f"Markdown 术语表不存在：{md_path}。请检查 --terminology 参数。"
            )

    results: list[tuple[str, bool, list[str]]] = []  # (检查项名称, 是否通过, 明细行)

    # ---- 检查 1：aligned 目录含 JSONL 文件 ----
    jsonl_files = sorted(aligned_dir.glob("*.jsonl"))
    ok1 = len(jsonl_files) > 0
    results.append((
        "aligned 目录含 JSONL 句对文件",
        ok1,
        [f"发现 {len(jsonl_files)} 个 .jsonl 文件 -> {aligned_dir}"]
        + ([f"  - {p.name}" for p in jsonl_files] if ok1 else
           ["提示：目录下没有任何 .jsonl，M1 第 3 步（align）可能未产出。"]),
    ))

    # ---- 扫描 JSONL（供检查 2/3 使用） ----
    bad_lines, stats, aligned_pairs = _scan_aligned(aligned_dir)
    total_pairs = sum(sum(ch.values()) for ch in stats.values())
    total_chapters = sum(len(ch) for ch in stats.values())

    # ---- 检查 2：逐行 JSON 与必需键校验 ----
    ok2 = not bad_lines
    results.append((
        "JSONL 逐行校验（json.loads + 必需键 {src,tgt,chapter,index}）",
        ok2,
        ([f"全部 {total_pairs} 行均为含必需键的 JSON 对象"] if ok2 else
         ["以下行校验未通过："] + [f"  ! {s}" for s in bad_lines]),
    ))

    # ---- 检查 3：句对与章节统计 ----
    ok3 = total_pairs > 0 and total_chapters >= args.min_chapters
    detail3 = [f"句对总数 {total_pairs}；章节总数 {total_chapters}（要求 >= {args.min_chapters}）"]
    for work_id in sorted(stats):
        chapters = stats[work_id]
        detail3.append(f"  作品 {work_id}：{sum(chapters.values())} 句对 / {len(chapters)} 章")
        for chapter, cnt in chapters.items():
            detail3.append(f"    - {chapter}：{cnt} 句对")
    if total_pairs == 0:
        detail3.append("提示：句对总数为 0，请检查 M1 第 3 步（align）是否正常产出。")
    elif total_chapters < args.min_chapters:
        detail3.append(f"提示：章节数不足 --min-chapters={args.min_chapters}，样例语料可能尚未覆盖足够章节。")
    results.append(("句对与章节统计（章节数 >= --min-chapters）", ok3, detail3))

    # ---- 检查 4：术语库 terms 表 ----
    try:
        conn = _open_glossary_ro(db_path)
    except sqlite3.Error as exc:
        return _fail_input(f"无法打开术语库 sqlite：{db_path}（{exc}）。")
    try:
        n_terms = _count_terms(conn)
        if n_terms is None:
            results.append((
                "术语库 terms 表",
                False,
                [f"{db_path} 中不存在 terms 表；",
                 "表结构应由 mant.memory.glossary.GlossaryStore 自动创建，"
                 "请确认 M1 第 4 步（extract_terms）已运行。"],
            ))
            term_sources: set[str] = set()
        else:
            note = ([f"terms 表共 {n_terms} 条 -> {db_path}"]
                    + (["（当前为 0 条：请提供 terminology.md 或开启 --with-llm，见 data/README.md）"]
                       if n_terms == 0 else []))
            results.append(("术语库 terms 表总条数", True, note))
            term_sources = _load_term_sources(conn)
            tm_pairs = _count_tm_pairs(conn)
            results.append((
                "运行库 TM 句对同步",
                tm_pairs is not None and tm_pairs > 0,
                [
                    f"tm_pairs 表共 {tm_pairs or 0} 条；M1 对齐产物已可被 MemoryHub 检索"
                    if tm_pairs is not None
                    else "缺少 tm_pairs 表；请用最新 M1 管道重建运行库"
                ],
            ))
            nonempty_targets = _count_nonempty_targets(conn)
            target_rate = nonempty_targets / n_terms if n_terms else 0.0
            results.append((
                "术语库非空译名率",
                target_rate >= args.min_nonempty_target_rate,
                [
                    f"非空译名 {nonempty_targets}/{n_terms}，比例 {target_rate:.1%}"
                    f"（要求 >= {args.min_nonempty_target_rate:.0%}）"
                ],
            ))
    except sqlite3.Error as exc:
        return _fail_input(f"查询术语库失败：{db_path}（{exc}）。")
    finally:
        conn.close()

    # ---- 检查 5（可选）：Markdown 术语表源词命中率 ----
    if md_path is not None:
        try:
            terms = parse_terminology_md(md_path)
        except (OSError, UnicodeDecodeError) as exc:
            return _fail_input(f"无法读取 Markdown 术语表：{md_path}（{exc}）。")
        if not terms:
            results.append((
                "术语命中率（Markdown 源词 vs terms 表）",
                False,
                [f"未能从 {md_path} 解析出任何源词；",
                 "支持格式：表格首列（| 源词 | 译法 |）或列表项（- 源词 → 译法 / - 源词）。"],
            ))
        else:
            hits = [t for t in terms if t in term_sources]
            misses = [t for t in terms if t not in term_sources]
            rate = len(hits) / len(terms)
            ok5 = rate >= MIN_HIT_RATE
            detail5 = [
                f"解析出源词 {len(terms)} 条，命中 {len(hits)} 条，"
                f"命中率 {rate:.1%}（及格线 {MIN_HIT_RATE:.0%}）",
            ]
            if misses:
                shown = "、".join(misses[:MAX_SAMPLES])
                more = f" 等 {len(misses)} 条" if len(misses) > MAX_SAMPLES else ""
                detail5.append(f"  未命中词条（供人工复核）：{shown}{more}")
            results.append(("术语命中率（Markdown 源词 vs terms 表）", ok5, detail5))

        # 术语是跨语言语义锚点：源句出现人工源词时，对齐译句应出现约定译名。
        terminology_pairs = parse_terminology_pairs(md_path)
        anchor_total = 0
        anchor_hits = 0
        anchor_misses: list[str] = []
        for pair in aligned_pairs:
            source_text = str(pair.get("src", ""))
            target_text = str(pair.get("tgt", ""))
            for source_term, expected_target in terminology_pairs:
                if source_term not in source_text:
                    continue
                anchor_total += 1
                if expected_target.casefold() in target_text.casefold():
                    anchor_hits += 1
                elif len(anchor_misses) < MAX_SAMPLES:
                    anchor_misses.append(
                        f"{source_term} → {expected_target}；译句摘要：{target_text[:80]}"
                    )
        anchor_rate = anchor_hits / anchor_total if anchor_total else 0.0
        anchor_ok = bool(anchor_total) and anchor_rate >= args.min_anchor_match_rate
        anchor_detail = [
            f"命中 {anchor_hits}/{anchor_total} 个术语锚点，比例 {anchor_rate:.1%}"
            f"（要求 >= {args.min_anchor_match_rate:.0%}）"
        ]
        anchor_detail.extend(f"  ! {item}" for item in anchor_misses)
        results.append(("双语句对术语锚点一致性（自动对齐质检）", anchor_ok, anchor_detail))

    # ---- 汇总输出 ----
    print("=" * 60)
    print("M1 管道产出验证")
    print(f"  aligned 目录 : {aligned_dir}")
    print(f"  术语库       : {db_path}")
    if md_path is not None:
        print(f"  术语表(md)   : {md_path}")
    print("=" * 60)
    n_pass = 0
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        for line in detail:
            print(f"       {line}")
        n_pass += int(ok)
    n_fail = len(results) - n_pass
    print("=" * 60)
    print(f"总结：共 {len(results)} 项检查，PASS {n_pass} 项，FAIL {n_fail} 项。")
    if n_fail:
        print("结论：存在未通过项，请按上方明细修复后重跑本脚本。")
        return 1
    print("结论：全部检查通过，M1 产出可用于下一阶段。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
