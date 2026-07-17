"""M1 离线语料管道 · 第二步：语料清洗（clean）。

功能：去广告行、去乱码、压缩连续空行、去重复行、繁体提示检测（占位）、
按章节标题正则切分。

设计约定：
- 全部为**纯函数**（不读写文件、不依赖全局状态），便于单元测试；
- 仅使用标准库；各函数可独立调用，也可由 ``clean_document`` 串成默认流水线；
- 清洗只做"降噪"，不改写正文内容（不改标点、不繁简转换）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "Chapter",
    "CleanResult",
    "DEFAULT_CHAPTER_PATTERN",
    "clean_document",
    "clean_text",
    "dedup_lines",
    "detect_traditional",
    "normalize_blank_lines",
    "remove_ad_lines",
    "remove_garbled",
    "split_chapters",
]

# ---------------------------------------------------------------------------
# 章节数据模型
# ---------------------------------------------------------------------------


@dataclass
class Chapter:
    """切分得到的单个章节。

    属性:
        index: 章序号（从 0 开始，按在文中出现顺序编号）。
        title: 章节标题行（已 strip）；正文前段（作品简介等）标题为 ``"<前言>"``，
            全文无任何章节标题时为 ``""``。
        text: 章节正文（不含标题行，首尾空行已去除）。
    """

    index: int
    title: str
    text: str


@dataclass
class CleanResult:
    """``clean_document`` 的返回结果。

    属性:
        text: 清洗后的文本。
        stats: 清洗统计（删除的广告行数 / 乱码行数 / 空行数 / 重复行数、
            繁体特征字比例 traditional_ratio、最终字符数 final_chars）。
    """

    text: str
    stats: dict[str, float | int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 默认规则
# ---------------------------------------------------------------------------

# 广告/水印行正则（命中即整行删除），针对小说站常见噪声；调用方可追加自定义规则
DEFAULT_AD_PATTERNS: tuple[str, ...] = (
    r"https?://\S+",                          # 链接
    r"www\.\S+",                              # 裸域名
    r".*(请记住|收藏).*(域名|网址|本站|网站).*",
    r".*(最新章节|全文阅读|无弹窗|免费阅读|手机阅读).*",
    r".*(小说网|阅读网|笔趣阁|顶点小说|txt\s*下载).*",
    r".*(求月票|求推荐票|求收藏|求订阅|求打赏).*",
    r"^(广告|推广)[:：].*",
)

# 章节标题正则：第X章/节/卷/回、序章/楔子/引子/尾声/番外、Chapter N
DEFAULT_CHAPTER_PATTERN: str = (
    r"^\s*(?:"
    r"第[0-9零一二两三四五六七八九十百千万]+[章节卷回][^\n]*"
    r"|序[章言]|楔子|引子|尾声|番外[^\n]*"
    r"|Chapter\s+\d+[^\n]*"
    r")$"
)

# 控制字符（保留 \n \t）与替换符 U+FFFD
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 常见中文标点（判定"怪异字符"时视为正常）
_CJK_PUNCT = "。，、；：？！…—·～「」『』（）《》〈〉“”‘’【】"

# 繁体特征字（简体文本中几乎不出现；用于"疑似繁体"占位检测，非穷举）
_TRADITIONAL_HINTS = frozenset(
    "這們麼說話見長門問聞間開關來時會對學經點從後愛國語體現實為裡裏與發習慣讓認識幾"
)


def _is_cjk(ch: str) -> bool:
    """判断字符是否属于 CJK 统一表意文字（基本区 U+4E00~U+9FFF + 扩展 A U+3400~U+4DBF）。"""
    return "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿"


# ---------------------------------------------------------------------------
# 纯函数清洗步骤
# ---------------------------------------------------------------------------


def remove_ad_lines(text: str, extra_patterns: list[str] | None = None) -> str:
    """删除命中广告/水印正则的整行。

    参数:
        text: 输入文本。
        extra_patterns: 追加的自定义正则（与默认规则取并集）。

    返回:
        删除广告行后的文本（其余行保持原顺序）。
    """
    patterns = [re.compile(p) for p in (*DEFAULT_AD_PATTERNS, *(extra_patterns or []))]
    lines = [ln for ln in text.split("\n") if not any(rx.search(ln) for rx in patterns)]
    return "\n".join(lines)


def remove_garbled(text: str, *, max_weird_ratio: float = 0.3) -> str:
    """去乱码：删除控制字符、U+FFFD，以及"怪异字符"比例过高的整行。

    参数:
        text: 输入文本。
        max_weird_ratio: 行内怪异字符（非 CJK / 非 ASCII 可见字符 / 非常见中文标点）
            占比阈值，超过则整行删除（典型如乱码段、乱码分隔线）。

    返回:
        清理后的文本。
    """

    def _weird_ratio(line: str) -> float:
        if not line:
            return 0.0
        weird = 0
        for ch in line:
            if _is_cjk(ch) or ch in _CJK_PUNCT:
                continue
            if ch.isascii() and (ch.isalnum() or ch.isspace() or ch.isprintable()):
                continue
            weird += 1
        return weird / len(line)

    cleaned = _CTRL_RE.sub("", text).replace("�", "")  # 同时删除替换符 U+FFFD
    lines = [ln for ln in cleaned.split("\n") if _weird_ratio(ln) <= max_weird_ratio]
    return "\n".join(lines)


def normalize_blank_lines(text: str, max_consecutive: int = 1) -> str:
    """压缩连续空行：最多保留 ``max_consecutive`` 个；去除行尾空白与首尾空行。"""
    out: list[str] = []
    blank = 0
    for line in text.split("\n"):
        if line.strip():
            blank = 0
            out.append(line.rstrip())
        else:
            blank += 1
            if blank <= max_consecutive:
                out.append("")
    # 去掉首尾空行
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def dedup_lines(text: str) -> str:
    """去重：删除重复出现的非空行（按 strip 后内容判重，保留首次出现）。

    注意：小说分页抓取常导致同一段落重复出现，默认开启；
    若作品本身有刻意重复的修辞段落，可在 ``clean_document(dedup=False)`` 关闭。
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in text.split("\n"):
        key = line.strip()
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(line)
    return "\n".join(out)


def detect_traditional(text: str) -> float:
    """繁体提示检测（占位实现）：返回文本中"繁体特征字"占 CJK 字符的比例。

    占位说明：仅基于少量繁体特征字估计，用于"疑似繁体语料"提示；
    比例超过约 0.05 时建议人工确认语料。
    TODO(繁简转换): 需要转换时接入 OpenCC（第三方库，延迟导入），本函数不做转换。
    """
    cjk_count = 0
    hits = 0
    for ch in text:
        if _is_cjk(ch):
            cjk_count += 1
            if ch in _TRADITIONAL_HINTS:
                hits += 1
    return hits / cjk_count if cjk_count else 0.0


def split_chapters(text: str, pattern: str | None = None) -> list[Chapter]:
    """按章节标题正则切分全文。

    参数:
        text: 输入文本（建议先经过 clean_document 清洗）。
        pattern: 自定义章节标题正则（MULTILINE，匹配整行标题）；
            缺省用 ``DEFAULT_CHAPTER_PATTERN``。

    返回:
        ``Chapter`` 列表。规则：
        - 第一章之前的非空正文（作品简介等）作为 ``title="<前言>"`` 的第 0 章；
        - 全文无章节标题时，整个文本作为 ``title=""`` 的单一章节返回；
        - 空文本返回空列表。
    """
    stripped = text.strip("\n")
    if not stripped.strip():
        return []
    rx = re.compile(pattern or DEFAULT_CHAPTER_PATTERN, re.MULTILINE)
    matches = list(rx.finditer(stripped))
    if not matches:
        return [Chapter(index=0, title="", text=stripped.strip())]

    chapters: list[Chapter] = []
    head = stripped[: matches[0].start()].strip()
    if head:
        chapters.append(Chapter(index=0, title="<前言>", text=head))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(stripped)
        body = stripped[start:end].strip()
        chapters.append(Chapter(index=len(chapters), title=m.group(0).strip(), text=body))
    return chapters


# ---------------------------------------------------------------------------
# 默认流水线
# ---------------------------------------------------------------------------


def clean_document(
    text: str,
    *,
    remove_ads: bool = True,
    remove_garbled_chars: bool = True,
    collapse_blank: bool = True,
    dedup: bool = True,
    extra_ad_patterns: list[str] | None = None,
    max_blank: int = 1,
) -> CleanResult:
    """默认清洗流水线：去广告 → 去乱码 → 压缩空行 → 去重，并输出统计。

    参数:
        text: 原始文本。
        remove_ads / remove_garbled_chars / collapse_blank / dedup: 各步骤开关。
        extra_ad_patterns: 追加的广告行正则。
        max_blank: 最多保留的连续空行数。

    返回:
        ``CleanResult(text=清洗后文本, stats=各步骤统计)``。
    """
    stats: dict[str, float | int] = {}
    cur = text

    def _n_lines(s: str) -> int:
        return s.count("\n") + 1 if s else 0

    if remove_ads:
        before = _n_lines(cur)
        cur = remove_ad_lines(cur, extra_ad_patterns)
        stats["removed_ad_lines"] = before - _n_lines(cur)
    if remove_garbled_chars:
        before = _n_lines(cur)
        cur = remove_garbled(cur)
        stats["removed_garbled_lines"] = before - _n_lines(cur)
    if collapse_blank:
        before = _n_lines(cur)
        cur = normalize_blank_lines(cur, max_blank)
        stats["removed_blank_lines"] = max(0, before - _n_lines(cur))
    if dedup:
        before = _n_lines(cur)
        cur = dedup_lines(cur)
        stats["removed_dup_lines"] = before - _n_lines(cur)

    stats["traditional_ratio"] = round(detect_traditional(cur), 4)
    stats["final_chars"] = len(cur)
    return CleanResult(text=cur, stats=stats)


def clean_text(text: str, **kwargs: object) -> str:
    """``clean_document`` 的便捷封装：只返回清洗后的文本。"""
    return clean_document(text, **kwargs).text  # type: ignore[arg-type]
