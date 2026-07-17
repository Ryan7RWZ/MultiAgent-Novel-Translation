"""M1 离线语料管道 · 第四步：术语候选抽取与复核入库（extract_terms）。

流程：
    1. ``tfidf_candidates``：标准库实现的 TF-IDF 候选词抽取
       （中文用字级 n-gram + 功能字/停用词过滤，**不使用 jieba**；英文用词级 n-gram）；
    2. ``llm_review_candidates``：LLM 复核 stub —— 通过统一接口
       ``mant.llm.client.LLMClient`` 让模型翻译/甄别候选词；
    3. ``build_term_entries`` + 入库：结果写入 ``mant.memory.glossary.GlossaryStore``
       （``upsert(entries)``；也兼容 MemoryHub 的 ``record_terms(entries)``）；
    4. ``offline_fallback_entries``：**无 LLM 环境的降级行为** —— 未注入
       ``LLMClient``，或 LLM 返回 [DRAFT] 占位 / 输出解析失败导致复核后零入库时，
       把 TF-IDF 排名前 N 的候选词以 ``confidence=0.5``、``category="offline"``
       直接入库，避免术语库空转（条目留待后续 LLM/人工复核补译）。

第三方依赖规则：本模块仅使用标准库；``mant.memory.models`` 为项目统一数据契约
（stdlib dataclass），``LLMClient`` 由调用方注入，本模块不做 LLM 客户端的构造。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Protocol, runtime_checkable

from mant.memory.models import TermEntry  # 统一数据契约（stdlib dataclass）

__all__ = [
    "TERM_REVIEW_SYSTEM_PROMPT",
    "TERM_REVIEW_USER_TEMPLATE",
    "TermCandidate",
    "TermStoreLike",
    "build_term_entries",
    "char_ngrams",
    "extract_terms_for_work",
    "llm_review_candidates",
    "offline_fallback_entries",
    "save_terms",
    "tfidf_candidates",
    "word_ngrams_en",
]

# ---------------------------------------------------------------------------
# 数据模型与结构化类型
# ---------------------------------------------------------------------------


@dataclass
class TermCandidate:
    """TF-IDF 抽取出的候选术语。

    属性:
        term: 候选词文本。
        score: TF-IDF 得分（越高越可能是领域术语）。
        freq: 在语料中的总词频。
        lang: 语言代码（``"zh"`` / ``"en"``）。
    """

    term: str
    score: float
    freq: int
    lang: str = "zh"


@runtime_checkable
class TermStoreLike(Protocol):
    """术语库结构化类型：与 ``mant.memory.glossary.GlossaryStore`` 的 ``upsert`` 兼容。

    MemoryHub 门面（``record_terms``）同样可传入，见 ``save_terms`` 的适配逻辑。
    """

    def upsert(self, entries: Iterable[TermEntry]) -> int:
        """批量写入术语条目，返回写入条数。"""
        ...


# ---------------------------------------------------------------------------
# 停用词与功能字（不依赖 jieba 的降级方案）
# ---------------------------------------------------------------------------

# 中文功能字：候选 n-gram 的首/尾字落在此集合内则判为非术语（如 "…的" / "在…"）
_ZH_FUNC_CHARS = frozenset(
    "的了是在和就都也不与及或很更最被把让向着从到又再还因为所以如果"
    "这那你我他她它们个种样吧呢吗啊嘛哦嗯哪只才刚将已曾经于之以而其然"
)

# 中文多字功能词：与候选词完全相等时过滤
_ZH_STOPWORDS = frozenset(
    "什么 怎么 怎样 为什么 时候 现在 可以 可能 应该 没有 我们 你们 他们 她们 它们 "
    "自己 别人 大家 已经 因为 所以 但是 可是 不过 然而 只是 就是 还是 或者 而且 "
    "如果 虽然 即使 既然 由于 于是 接着 然后 这才 一个 一些 一股 一道 一种 一样 "
    "如此 这般 这样 那样 甚至 几乎 似乎 仿佛 好像 显然 顿时 旋即 随后 此时 此刻 "
    "当下 如今 之前 之后 以前 以后 以上 以下 起来 下去 出来 过来 进去 回来 回去 "
    "知道 看到 听到 说道 笑道 想到 感觉 觉得 发现 明白 清楚 东西 事情 地方 问题".split()
)

# 英文停用词：候选 n-gram 全为停用词、或首尾词为停用词时过滤
_EN_STOPWORDS = frozenset(
    "a an the and or but if of to in on at for with by from as is are was were be been "
    "being do does did have has had i you he she it we they them his her its our your "
    "their my me him us this that these those there here not no yes so too very can "
    "could will would shall should may might must what when where who whom which how "
    "why all any both each few more most other some such only own same than then now "
    "just also over under again once out up down off about into through during before "
    "after above below between".split()
)

# CJK 连续片段（n-gram 只在片段内滑动，天然避开标点/空白）
_CJK_RUN_RX = re.compile(r"[一-鿿㐀-䶿]+")
# 英文 token（小写化后取 n-gram）
_EN_TOKEN_RX = re.compile(r"[A-Za-z][A-Za-z'\-]*")


# ---------------------------------------------------------------------------
# n-gram 统计
# ---------------------------------------------------------------------------


def char_ngrams(text: str, ns: tuple[int, ...] = (2, 3, 4)) -> Counter[str]:
    """中文字级 n-gram 计数：在 CJK 连续片段内滑动窗口。

    参数:
        text: 输入文本。
        ns: n-gram 长度集合，默认 2~4 字（覆盖大多数人名/功法名/地名）。
    """
    counter: Counter[str] = Counter()
    for run in _CJK_RUN_RX.findall(text):
        length = len(run)
        for n in ns:
            if length < n:
                continue
            for k in range(length - n + 1):
                counter[run[k : k + n]] += 1
    return counter


def word_ngrams_en(text: str, ns: tuple[int, ...] = (1, 2, 3)) -> Counter[str]:
    """英文词级 n-gram 计数（小写化，空格连接）。"""
    tokens = _EN_TOKEN_RX.findall(text.lower())
    counter: Counter[str] = Counter()
    for n in ns:
        for i in range(len(tokens) - n + 1):
            counter[" ".join(tokens[i : i + n])] += 1
    return counter


def _keep_zh_candidate(gram: str) -> bool:
    """中文候选词过滤：去掉功能词与"首尾为功能字"的片段。"""
    if gram in _ZH_STOPWORDS:
        return False
    if gram[0] in _ZH_FUNC_CHARS or gram[-1] in _ZH_FUNC_CHARS:
        return False
    # 单字重复（如 "啊啊"）基本不可能是术语
    if len(set(gram)) == 1:
        return False
    return True


def _keep_en_candidate(gram: str) -> bool:
    """英文候选词过滤：去掉全停用词、首尾停用词、过短单词。"""
    words = gram.split()
    if all(w in _EN_STOPWORDS for w in words):
        return False
    if words[0] in _EN_STOPWORDS or words[-1] in _EN_STOPWORDS:
        return False
    if len(words) == 1 and len(words[0]) <= 2:
        return False
    return True


def tfidf_candidates(
    docs: list[str],
    *,
    lang: str = "zh",
    top_k: int = 200,
    ns: tuple[int, ...] = (2, 3, 4),
    min_freq: int = 3,
) -> list[TermCandidate]:
    """TF-IDF 候选词抽取（标准库实现，作为 jieba/分词器的降级方案）。

    参数:
        docs: 文档列表（建议按章节切分后传入，IDF 以章节为文档单位）；
            单文档时 IDF 退化为常数，排序近似按词频。
        lang: ``"zh"`` 走字级 n-gram，``"en"`` 走词级 n-gram。
        top_k: 返回候选词上限。
        ns: n-gram 长度集合。
        min_freq: 最低总词频（低于则丢弃，过滤偶然片段）。

    返回:
        按得分降序的 ``TermCandidate`` 列表；同频的子串碎片会被去重。

    TODO(精度): 接入分词器（jieba/spaCy，延迟导入）后可改用词级 TF-IDF，
    并叠加左右邻接熵/互信息做新词发现。
    """
    docs = [d for d in docs if d and d.strip()]
    if not docs:
        return []

    tf: Counter[str] = Counter()
    df: Counter[str] = Counter()
    for doc in docs:
        counts = char_ngrams(doc, ns) if lang == "zh" else word_ngrams_en(doc, ns)
        tf.update(counts)
        for gram in counts:
            df[gram] += 1

    keep = _keep_zh_candidate if lang == "zh" else _keep_en_candidate
    n_docs = len(docs)
    scored: list[TermCandidate] = []
    for gram, freq in tf.items():
        if freq < min_freq or not keep(gram):
            continue
        # 平滑 IDF：log((1+N)/(1+df)) + 1，单文档时恒为 log(2)+1
        idf = math.log((1 + n_docs) / (1 + df[gram])) + 1.0
        scored.append(TermCandidate(term=gram, score=round(freq * idf, 4), freq=freq, lang=lang))

    # 同分时优先长词：短前缀碎片（如 "澜大"）若先入选，下方子串去重会因
    # "父词不是碎片子串"（方向相反）而失效；长词优先可保证碎片被父词去重
    scored.sort(key=lambda c: (-c.score, -len(c.term), c.term))

    # 子串去重：若候选词是已入选高分词的子串且词频不超过之，判为碎片丢弃
    selected: list[TermCandidate] = []
    for cand in scored:
        if any(
            cand.term != kept.term and cand.term in kept.term and cand.freq <= kept.freq
            for kept in selected
        ):
            continue
        selected.append(cand)
        if len(selected) >= top_k:
            break
    return selected


# ---------------------------------------------------------------------------
# LLM 复核（stub）：调用统一接口 LLMClient 翻译/甄别候选词
# ---------------------------------------------------------------------------

TERM_REVIEW_SYSTEM_PROMPT = (
    "你是资深网络文学翻译与术语管理专家，正在为机器翻译系统建设作品级术语库。"
    "你的任务是从自动抽取的候选词中甄别出真正的术语（人名、地名、功法、法宝、"
    "势力、境界、专有名词等），过滤掉普通词语，并为保留的术语给出规范、"
    "符合网络文学作品语境的英文译法。只输出 JSON，不要输出任何解释。"
)

TERM_REVIEW_USER_TEMPLATE = """作品 ID：{work_id}

以下是 TF-IDF 自动抽取的候选术语（每行一个）：
{candidates}

请逐一甄别并输出 JSON 数组，每个元素形如：
{{"source": "候选词原文", "target": "规范英文译法", "category": "person|place|skill|item|faction|realm|other", "confidence": 0.0~1.0}}

要求：
1. 只保留真正的术语，普通词语直接丢弃（不要出现在输出中）；
2. 译法需符合该作品类型（玄幻/仙侠/都市等）的通行译名习惯；
3. confidence 表示你对该译法的把握（0~1）；
4. 输出必须是合法 JSON 数组，不要包含 Markdown 代码围栏或其他文字。"""

# 未复核条目的统一标记
_NOTE_UNREVIEWED = "unreviewed: 待 LLM 复核"
_NOTE_DRAFT = "unreviewed: LLM 占位/解析失败"


def _parse_review_response(resp: str) -> list[dict[str, Any]] | None:
    """解析 LLM 复核响应为词条字典列表；失败返回 None。

    TODO(鲁棒性): 输出 schema 校验、截断重试、围栏/杂文本的更宽容提取。
    """
    text = resp.strip()
    if not text or text.startswith("[DRAFT]"):
        return None  # LLMClient 降级占位响应，视为未复核
    # 宽容提取第一个 JSON 数组（模型可能带前后杂文本）
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return [item for item in data if isinstance(item, dict)]


def _unreviewed(cand: TermCandidate, note: str = _NOTE_UNREVIEWED) -> dict[str, Any]:
    """构造"未复核"词条字典：target 留空、confidence 置 0，等待后续复核。"""
    return {
        "source": cand.term,
        "target": "",
        "category": "auto",
        "confidence": 0.0,
        "score": cand.score,
        "freq": cand.freq,
        "note": note,
    }


def llm_review_candidates(
    candidates: list[TermCandidate],
    llm: Any | None,
    *,
    work_id: str,
    batch_size: int = 50,
    temperature: float = 0.2,
) -> list[dict[str, Any]]:
    """LLM 复核 stub：分批调用 ``LLMClient.complete`` 翻译/甄别候选词。

    参数:
        candidates: TF-IDF 抽取的候选词列表。
        llm: 统一接口 ``LLMClient`` 实例；传 None 表示离线模式，直接透传为未复核词条。
        work_id: 作品 ID（写入提示词，便于模型结合作品语境）。
        batch_size: 每批复核的候选词数。
        temperature: 采样温度（复核任务取低值求稳）。

    返回:
        词条字典列表，键：source/target/category/confidence/score/freq/note。
        解析失败或 [DRAFT] 占位的批次按未复核透传（target 为空）。

    TODO: 失败批次重试、复核结果缓存（同一候选词不重复调用）、置信度校准。
    """
    if llm is None:
        return [_unreviewed(c) for c in candidates]

    results: list[dict[str, Any]] = []
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        user = TERM_REVIEW_USER_TEMPLATE.format(
            work_id=work_id,
            candidates="\n".join(f"{i + 1}. {c.term}" for i, c in enumerate(batch)),
        )
        resp = llm.complete(TERM_REVIEW_SYSTEM_PROMPT, user, temperature=temperature)
        parsed = _parse_review_response(resp)
        if parsed is None:
            results.extend(_unreviewed(c, _NOTE_DRAFT) for c in batch)
            continue
        by_source = {str(item.get("source", "")): item for item in parsed}
        for cand in batch:
            item = by_source.get(cand.term)
            if item is None:
                results.append(_unreviewed(cand, _NOTE_DRAFT))
                continue
            results.append(
                {
                    "source": cand.term,
                    "target": str(item.get("target", "")),
                    "category": str(item.get("category", "other")),
                    "confidence": float(item.get("confidence", 0.6)),
                    "score": cand.score,
                    "freq": cand.freq,
                    "note": "llm_reviewed",
                }
            )
    return results


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------


def build_term_entries(
    reviewed: list[dict[str, Any]],
    work_id: str,
    *,
    include_untranslated: bool = False,
) -> list[TermEntry]:
    """把复核结果转换为统一数据模型 ``TermEntry`` 列表。

    参数:
        reviewed: ``llm_review_candidates`` 的输出。
        work_id: 作品 ID（术语按作品隔离）。
        include_untranslated: 是否保留 target 为空的未复核条目
            （默认 False：未翻译的候选词不入库，避免污染术语查询）。
    """
    entries: list[TermEntry] = []
    for item in reviewed:
        source = str(item.get("source", "")).strip()
        target = str(item.get("target", "")).strip()
        if not source:
            continue
        if not target and not include_untranslated:
            continue
        entries.append(
            TermEntry(
                source=source,
                target=target,
                category=str(item.get("category", "")),
                work_id=work_id,
                confidence=float(item.get("confidence", 0.0)),
            )
        )
    return entries


def save_terms(entries: list[TermEntry], store: Any) -> int:
    """把术语条目写入术语库。

    兼容两种统一接口：
        - ``GlossaryStore.upsert(entries) -> int``（mant.memory.glossary）；
        - ``MemoryHub.record_terms(entries) -> None``（mant.memory 门面）。

    返回写入条数。
    """
    if not entries:
        return 0
    if hasattr(store, "upsert"):
        return int(store.upsert(entries))
    if hasattr(store, "record_terms"):
        store.record_terms(entries)
        return len(entries)
    raise TypeError("store 需实现 upsert(entries) 或 record_terms(entries)（见 mant.memory）")


# ---------------------------------------------------------------------------
# 离线降级入库（无 LLM 环境的降级行为）
# ---------------------------------------------------------------------------

# 离线降级条目的 category 标记：标注该条目来自离线降级路径（区别于 LLM 复核/人工确认）
CATEGORY_OFFLINE = "offline"


def offline_fallback_entries(
    candidates: list[TermCandidate],
    work_id: str,
    *,
    top_n: int = 50,
    confidence: float = 0.5,
) -> list[TermEntry]:
    """离线降级入库：把 TF-IDF 排名前 ``top_n`` 的候选词直接转为术语条目。

    **这是无 LLM 环境的降级行为**：未注入 ``LLMClient``，或 LLM 返回 [DRAFT]
    占位响应 / 输出解析失败，导致复核后没有任何可入库条目时，为避免术语库
    空转，把排名靠前的候选词以低置信度直接入库，留待后续 LLM/人工复核补译。

    条目约定：
        - ``category`` 固定为 ``"offline"``，显式标注来源为离线降级路径；
        - ``confidence`` 默认 0.5（低于 LLM 复核条目，高于未复核的 0.0）；
        - ``target`` 留空（离线路径无译法，待复核补全；terms 表 target 列
          为 NOT NULL，空串不违反约束，且术语查询侧应按空译法=未确认处理）。
    """
    return [
        TermEntry(
            source=cand.term,
            target="",
            category=CATEGORY_OFFLINE,
            work_id=work_id,
            confidence=confidence,
        )
        for cand in candidates[: max(top_n, 0)]
    ]


def extract_terms_for_work(
    docs: list[str],
    work_id: str,
    *,
    llm: Any | None = None,
    store: Any | None = None,
    lang: str = "zh",
    top_k: int = 200,
    min_freq: int = 3,
    offline_fallback: bool = True,
    offline_top_n: int = 50,
) -> dict[str, int | str]:
    """单作品术语抽取编排：TF-IDF 抽取 → LLM 复核 → （离线降级）→ 入库。

    参数:
        docs: 该作品的文档列表（建议按章节切分后传入）。
        work_id: 作品 ID。
        llm: 可选 ``LLMClient``；None 时跳过复核（词条按未复核透传）。
        store: 可选术语库（GlossaryStore/MemoryHub）；None 时跳过入库。
        lang / top_k / min_freq: 透传给 ``tfidf_candidates``。
        offline_fallback: 无 LLM 环境的降级开关（默认开启）：复核后零可入库
            条目时（未注入 llm，或 LLM 占位/解析失败），把 TF-IDF 排名前
            ``offline_top_n`` 的候选词以 confidence=0.5、category="offline"
            直接入库，见 ``offline_fallback_entries``。
        offline_top_n: 离线降级入库的候选词上限。

    返回:
        统计字典：{work_id, docs, candidates, reviewed, entries, saved,
        offline_fallback}（offline_fallback=1 表示本次走了离线降级路径）。
    """
    candidates = tfidf_candidates(docs, lang=lang, top_k=top_k, min_freq=min_freq)
    reviewed = llm_review_candidates(candidates, llm, work_id=work_id)
    entries = build_term_entries(reviewed, work_id)
    used_fallback = 0
    if not entries and candidates and offline_fallback:
        # 离线降级：LLM 缺失/占位/解析失败导致零入库，改存 TF-IDF 头部候选
        entries = offline_fallback_entries(candidates, work_id, top_n=offline_top_n)
        used_fallback = 1
    saved = save_terms(entries, store) if store is not None else 0
    return {
        "work_id": work_id,
        "docs": len(docs),
        "candidates": len(candidates),
        "reviewed": len(reviewed),
        "entries": len(entries),
        "saved": saved,
        "offline_fallback": used_fallback,
    }
