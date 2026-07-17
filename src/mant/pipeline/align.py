"""M1 离线语料管道 · 第三步：章节配对与句对齐（align）。

功能：
    1. 章节标题配对（``pair_chapters``）：按章节号（阿拉伯数字/中文数字/Chapter N）
       对齐双语章节，序号缺失时按顺序兜底；
    2. 句对齐（``align_sentences``）：标准库实现的长度+标点启发式对齐
       （Gale-Church 思路的简化 DP：支持 1-1 / 1-0 / 0-1 / 2-1 / 1-2 五种对齐操作）；
    3. 产物写出为 JSONL 句对 ``{"src", "tgt", "chapter", "index"}``。

TODO(高质量对齐): 可选接入 vecalign / LASER 句向量对齐（第三方库，必须延迟导入），
见 ``align_with_vecalign``。

第三方依赖规则：本模块仅使用标准库。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .clean import Chapter, split_chapters

__all__ = [
    "SentencePair",
    "align_documents",
    "align_sentences",
    "align_with_vecalign",
    "estimate_char_ratio",
    "pair_chapters",
    "parse_chapter_number",
    "read_jsonl",
    "split_sentences",
    "write_jsonl",
]

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class SentencePair:
    """一对对齐的双语句子（JSONL 产物的单行）。

    属性:
        src: 源语言句子。
        tgt: 目标语言句子。
        chapter: 所属章节标题（源语言侧）。
        index: 句对在该章节内的序号（从 0 开始）。
    """

    src: str
    tgt: str
    chapter: str
    index: int

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSONL 行字典（键序固定为 src/tgt/chapter/index）。"""
        return {"src": self.src, "tgt": self.tgt, "chapter": self.chapter, "index": self.index}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SentencePair":
        """从 JSONL 行字典反序列化，缺失字段宽容处理。"""
        return cls(
            src=str(data.get("src", "")),
            tgt=str(data.get("tgt", "")),
            chapter=str(data.get("chapter", "")),
            index=int(data.get("index", 0)),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 句子切分（标点启发式）
# ---------------------------------------------------------------------------

# 中文句末标点：。！？；…（省略号）加可选收尾引号/括号
_ZH_SENT_RX = re.compile(r"[^。！？；…]+[。！？；…]*[\"'”’」』)]*")
# 英文句末标点：. ! ? 加可选收尾引号/括号
_EN_SENT_RX = re.compile(r"[^.!?]+[.!?]*[\"'”’)\]]*")


def split_sentences(text: str, lang: str = "zh") -> list[str]:
    """按标点启发式切句（空行分段，句不跨段）。

    参数:
        text: 输入文本（建议已清洗）。
        lang: ``"zh"`` 用中文句末标点集合，其余语言用英文集合。

    返回:
        句子列表（保留句末标点，去除首尾空白；空句被丢弃）。

    TODO(精度): 对话引号内句末标点、英文缩写（Mr./U.S.A.）等边界场景需更细的规则。
    """
    rx = _ZH_SENT_RX if lang == "zh" else _EN_SENT_RX
    sents: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        sents.extend(m.group(0).strip() for m in rx.finditer(para) if m.group(0).strip())
    return sents


# ---------------------------------------------------------------------------
# 章节号解析与章节配对
# ---------------------------------------------------------------------------

_ZH_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_ZH_UNITS = {"十": 10, "百": 100, "千": 1000}


def _chinese_numeral_to_int(s: str) -> int | None:
    """把中文数字串（如 一百二十三 / 十二 / 三千零五）转为 int；无法解析返回 None。

    支持到万级；非法字符直接判失败。纯启发式，不处理"亿万"等更大单位。
    """
    total = 0
    section = 0
    number = 0
    for ch in s:
        if ch in _ZH_DIGITS:
            number = _ZH_DIGITS[ch]
        elif ch in _ZH_UNITS:
            section += (number or 1) * _ZH_UNITS[ch]  # "十二" = (空 or 1)*10 + 2
            number = 0
        elif ch == "万":
            section = (section + number) * 10000
            total += section
            section = 0
            number = 0
        else:
            return None
    return total + section + number


def parse_chapter_number(title: str) -> int | None:
    """从章节标题解析章节号；无法解析（序章/楔子/番外等）返回 None。

    支持：``第12章`` / ``第十二章`` / ``第 3 节`` / ``Chapter 12`` / ``Ch.12``。
    """
    m = re.search(r"第\s*([0-9]+)\s*[章节卷回]", title)
    if m:
        return int(m.group(1))
    m = re.search(r"第\s*([零一二两三四五六七八九十百千万]+)\s*[章节卷回]", title)
    if m:
        return _chinese_numeral_to_int(m.group(1))
    m = re.search(r"(?:Chapter|Ch\.?)\s*([0-9]+)", title, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def pair_chapters(
    src_chapters: list[Chapter],
    tgt_chapters: list[Chapter],
) -> list[tuple[Chapter, Chapter | None]]:
    """章节标题配对：优先按章节号精确配对，序号缺失/未命中时按出现顺序兜底。

    参数:
        src_chapters: 源语言章节列表。
        tgt_chapters: 目标语言章节列表。

    返回:
        ``(源章节, 目标章节或 None)`` 列表，长度与 ``src_chapters`` 一致；
        None 表示该章未找到译文（后续步骤应跳过或标记）。

    TODO(精度): 双语章节数差异大（合章/拆章）时引入标题相似度与人工映射表。
    """
    tgt_by_no: dict[int, Chapter] = {}
    for ch in tgt_chapters:
        no = parse_chapter_number(ch.title)
        if no is not None and no not in tgt_by_no:
            tgt_by_no[no] = ch

    used: set[int] = set()  # 已配对的译文章节（按对象 id，避免重复配对）
    pairs: list[tuple[Chapter, Chapter | None]] = []
    for idx, sch in enumerate(src_chapters):
        tch: Chapter | None = None
        no = parse_chapter_number(sch.title)
        if no is not None:
            cand = tgt_by_no.get(no)
            if cand is not None and id(cand) not in used:
                tch = cand
        if tch is None and idx < len(tgt_chapters):
            cand = tgt_chapters[idx]  # 顺序兜底
            if id(cand) not in used:
                tch = cand
        if tch is not None:
            used.add(id(tch))
        pairs.append((sch, tch))
    return pairs


# ---------------------------------------------------------------------------
# 句对齐（长度启发式 DP，Gale-Church 简化版）
# ---------------------------------------------------------------------------


def _move_cost(
    src_sents: list[str],
    i: int,
    di: int,
    tgt_sents: list[str],
    j: int,
    dj: int,
    char_ratio: float,
    gap_penalty: float,
    group_penalty: float,
) -> float:
    """计算一次对齐操作的代价。

    - 1-0 / 0-1（句子的删除/插入）：固定 ``gap_penalty``；
    - 1-1 / 2-1 / 1-2：长度失配代价 ``|log(实际译长 / 期望译长)|``，
      期望译长 = 源句总字符数 × ``char_ratio``；多句合并（2-1/1-2）附加 ``group_penalty``。
    """
    if di == 0 or dj == 0:
        return gap_penalty
    src_len = sum(len(s) for s in src_sents[i : i + di])
    tgt_len = sum(len(s) for s in tgt_sents[j : j + dj])
    expected = src_len * char_ratio
    cost = abs(math.log((tgt_len + 1.0) / (expected + 1.0)))
    if di + dj > 2:
        cost += group_penalty
    return cost


def estimate_char_ratio(
    src_sents: list[str],
    tgt_sents: list[str],
    *,
    minimum: float = 0.5,
    maximum: float = 8.0,
) -> float:
    """按当前章节两侧总字符数估算目标/源长度比。

    固定的中英字符比会随标点、专名和文本风格大幅波动。章节级估算能在
    不依赖语义模型的前提下避免系统性错位；上下限用于隔离异常空文本。
    """
    src_len = sum(len(s) for s in src_sents)
    tgt_len = sum(len(s) for s in tgt_sents)
    if src_len <= 0 or tgt_len <= 0:
        return 1.0
    return max(minimum, min(maximum, tgt_len / src_len))


def align_sentences(
    src_sents: list[str],
    tgt_sents: list[str],
    *,
    char_ratio: float | None = None,
    gap_penalty: float = 0.6,
    group_penalty: float = 0.15,
) -> list[tuple[str, str]]:
    """句对齐：标准库实现的长度+标点启发式 DP（Gale-Church 简化版）。

    参数:
        src_sents / tgt_sents: 源 / 目标语言句子列表。
        char_ratio: 期望"目标侧字符数 / 源侧字符数"比例；``None`` 时按
            当前章节两侧总字符数自动估算，避免固定经验值造成系统性错位。
        gap_penalty: 单句删除/插入的固定代价。
        group_penalty: 2-1 / 1-2 合并对齐的附加代价。

    返回:
        ``(源句, 译句)`` 列表；合并对齐时多句用 ``""``（源）/ ``" "``（目标）拼接。

    复杂度 O(n×m×5)，章节规模（数百句）适用；
    TODO(性能): 整书级对齐先做分块/束搜索（beam），或直接换 vecalign。
    """
    n, m = len(src_sents), len(tgt_sents)
    if n == 0 or m == 0:
        return []
    effective_ratio = (
        estimate_char_ratio(src_sents, tgt_sents)
        if char_ratio is None
        else max(float(char_ratio), 0.01)
    )

    inf = float("inf")
    # dp[i][j] = 对齐完前 i 句源文、前 j 句译文的最小总代价
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    moves = ((1, 1), (1, 0), (0, 1), (2, 1), (1, 2))

    for i in range(n + 1):
        for j in range(m + 1):
            if dp[i][j] == inf:
                continue
            for di, dj in moves:
                ni, nj = i + di, j + dj
                if ni > n or nj > m:
                    continue
                cost = dp[i][j] + _move_cost(
                    src_sents, i, di, tgt_sents, j, dj,
                    effective_ratio, gap_penalty, group_penalty,
                )
                if cost < dp[ni][nj]:
                    dp[ni][nj] = cost
                    back[ni][nj] = (i, j)

    # 回溯最优路径
    aligned: list[tuple[str, str]] = []
    i, j = n, m
    while (i, j) != (0, 0):
        prev = back[i][j]
        if prev is None:  # 防御：DP 必然有解，正常不会走到
            break
        pi, pj = prev
        di, dj = i - pi, j - pj
        if di > 0 and dj > 0:
            src = "".join(src_sents[pi:i])   # 中文多句直接拼接
            tgt = " ".join(tgt_sents[pj:j])  # 英文多句空格拼接
            aligned.append((src, tgt))
        i, j = pi, pj
    aligned.reverse()
    return aligned


# ---------------------------------------------------------------------------
# 文档级编排与 JSONL 读写
# ---------------------------------------------------------------------------


def align_documents(
    src_text: str,
    tgt_text: str,
    *,
    src_lang: str = "zh",
    tgt_lang: str = "en",
    chapter_pattern: str | None = None,
    char_ratio: float | None = None,
) -> list[SentencePair]:
    """文档级对齐编排：切章 → 章节配对 → 逐章句对齐。

    参数:
        src_text / tgt_text: 已清洗的源 / 目标语言全文。
        src_lang / tgt_lang: 语言代码（决定切句标点集合）。
        chapter_pattern: 自定义章节标题正则（双语共用，见 clean.split_chapters）。
        char_ratio: 期望字符数比例；默认按每章自动估算（见 align_sentences）。

    返回:
        ``SentencePair`` 列表；未配对成功的源章节被跳过（tgt=None）。
    """
    src_chapters = split_chapters(src_text, chapter_pattern)
    tgt_chapters = split_chapters(tgt_text, chapter_pattern)
    pairs: list[SentencePair] = []
    for sch, tch in pair_chapters(src_chapters, tgt_chapters):
        if tch is None:
            continue
        src_sents = split_sentences(sch.text, src_lang)
        tgt_sents = split_sentences(tch.text, tgt_lang)
        for k, (s, t) in enumerate(align_sentences(src_sents, tgt_sents, char_ratio=char_ratio)):
            pairs.append(SentencePair(src=s, tgt=t, chapter=sch.title, index=k))
    return pairs


def write_jsonl(pairs: Iterable[SentencePair], path: str | Path) -> int:
    """把句对写出为 JSONL（UTF-8，一行一个对象，非 ASCII 不转义）。

    返回写入的行数；父目录自动创建。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for p in pairs:
            f.write(json.dumps(p.to_dict(), ensure_ascii=False) + "\n")
            count += 1
    return count


def read_jsonl(path: str | Path) -> Iterator[SentencePair]:
    """逐行读取 JSONL 句对文件（生成器，空行跳过）。"""
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield SentencePair.from_dict(json.loads(line))


def align_with_vecalign(src_path: str | Path, tgt_path: str | Path, out_path: str | Path) -> None:
    """TODO(可选增强): 基于 vecalign / LASER 句向量的高质量对齐。

    计划：函数内延迟导入 vecalign / laser（第三方可选依赖，未安装时给出
    ``pip install vecalign laserembeddings`` 提示并回退到 ``align_documents``），
    用句向量余弦相似度替代本模块的长度启发式代价。骨架阶段不实现。
    """
    raise NotImplementedError(
        "vecalign/LASER 对齐尚未接入：请使用 align_documents（长度启发式），"
        "或安装 vecalign/laserembeddings 后补充本函数实现。"
    )
