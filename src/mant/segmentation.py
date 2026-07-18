"""在线翻译使用的确定性初始切片器。

本模块完全使用标准库，不调用 LLM。它把单章原文安全规范化后，按章节/场景、
段落、句子、分句和空白的优先级生成候选切点，再用 token 预算和确定性动态规划
选择最终片段。所有片段覆盖互不重叠的连续区间，按顺序拼接可精确还原规范化原文。

注意：这里的安全规范化不同于 M1 语料清洗。在线翻译不得删除重复行、广告疑似行
或改写标点，因为这些内容可能是小说正文的一部分。
"""

from __future__ import annotations

import hashlib
import math
import re
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

__all__ = [
    "DeterministicSegmenter",
    "Segment",
    "SegmentationConfig",
    "SegmentationResult",
    "SegmentationStats",
    "estimate_tokens",
    "safe_normalize",
    "segment_text",
]


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SCENE_BREAK_RE = re.compile(
    r"^\s*(?:\*{3,}|-{3,}|_{3,}|={3,}|…{2,}|※+|◇+|◆+|○+|●+|·{3,})\s*$"
)
_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第[0-9零一二两三四五六七八九十百千万]+[章节卷回][^\n]*"
    r"|序[章言]|楔子|引子|尾声|番外[^\n]*"
    r"|(?:Chapter|Ch\.?)\s+\d+[^\n]*"
    r")\s*$",
    re.IGNORECASE,
)
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n+")
_SENTENCE_END_RE = re.compile(
    r"(?:[。！？!?]+|…+|[.!?]+[\"'”’」』）》)\]]*(?=\s|$))"
    r"[\"'”’」』）》)\]]*"
)
_SEMICOLON_RE = re.compile(r"[；;]+[\"'”’」』）》)\]]*")
_CLAUSE_RE = re.compile(r"[，、,:：]+")
_WHITESPACE_RE = re.compile(r"[ \t]+")

# 边界代价越低越优先。forced 边界会先把文档分区，任何片段都不能跨越。
_BOUNDARY_PENALTIES = {
    "document_start": 0.0,
    "document_end": 0.0,
    "heading": 0.0,
    "scene": 0.0,
    "blank_line": 1.0,
    "paragraph": 2.0,
    "sentence": 5.0,
    "semicolon": 10.0,
    "clause": 20.0,
    "whitespace": 40.0,
    "hard": 100.0,
}


def safe_normalize(text: str) -> str:
    """做在线翻译安全的最小规范化，不删除任何可见正文内容。"""
    normalized = str(text or "")
    if normalized.startswith("\ufeff"):
        normalized = normalized[1:]
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _CONTROL_RE.sub("", normalized)
    return normalized if normalized.strip() else ""


def _char_token_units(ch: str) -> int:
    """返回十分之一 token 为单位的保守字符权重，保持估算可做前缀和。"""
    if "\u3400" <= ch <= "\u9fff":
        return 13
    if ch.isascii():
        if ch.isalnum() or ch in "_'":
            return 3
        if ch.isspace():
            return 1
        return 5
    if ch.isspace():
        return 1
    if ch.isalpha() or ch.isdigit():
        return 10
    return 8


class _TokenBudget:
    """基于字符权重前缀和的 O(1) token 估算器。"""

    def __init__(self, text: str) -> None:
        prefix = [0]
        total = 0
        for ch in text:
            total += _char_token_units(ch)
            prefix.append(total)
        self.prefix = prefix

    def tokens(self, start: int, end: int) -> int:
        if end <= start:
            return 0
        units = self.prefix[end] - self.prefix[start]
        return max(1, math.ceil(units / 10))

    def end_for_tokens(self, start: int, end_limit: int, max_tokens: int) -> int:
        """返回不超过预算的最远字符位置，至少前进一个字符。"""
        if start >= end_limit:
            return start
        target = self.prefix[start] + max(1, max_tokens) * 10
        end = bisect_right(self.prefix, target, lo=start + 1, hi=end_limit + 1) - 1
        if end <= start:
            end = start + 1
        return min(end, end_limit)

    def start_for_tokens(self, start_limit: int, end: int, max_tokens: int) -> int:
        """返回 end 之前、预算内尽可能靠前的字符位置。"""
        if end <= start_limit:
            return end
        target = self.prefix[end] - max(1, max_tokens) * 10
        start = bisect_left(self.prefix, target, lo=start_limit, hi=end)
        return max(start_limit, min(start, end))


def estimate_tokens(text: str) -> int:
    """在没有供应商 tokenizer 时机械估算文本 token 数。"""
    normalized = str(text or "")
    return _TokenBudget(normalized).tokens(0, len(normalized))


@dataclass(frozen=True, slots=True)
class SegmentationConfig:
    """确定性切片配置，数值单位均为估算 token。"""

    target_core_tokens: int = 900
    max_core_tokens: int = 1200
    min_core_tokens: int = 250
    context_before_tokens: int = 160
    context_after_tokens: int = 80
    max_segments: int = 5000

    def __post_init__(self) -> None:
        if self.target_core_tokens <= 0:
            raise ValueError("target_core_tokens 必须大于 0")
        if self.max_core_tokens < 2:
            raise ValueError("max_core_tokens 必须至少为 2，才能容纳任意单个字符")
        if self.max_core_tokens < self.target_core_tokens:
            raise ValueError("max_core_tokens 不能小于 target_core_tokens")
        if not 0 <= self.min_core_tokens <= self.target_core_tokens:
            raise ValueError("min_core_tokens 必须位于 0 到 target_core_tokens 之间")
        if self.context_before_tokens < 0 or self.context_after_tokens < 0:
            raise ValueError("上下文 token 预算不能小于 0")
        if self.max_segments <= 0:
            raise ValueError("max_segments 必须大于 0")

    @classmethod
    def from_mapping(
        cls, value: "SegmentationConfig | Mapping[str, Any] | None"
    ) -> "SegmentationConfig":
        if isinstance(value, cls):
            return value
        raw = dict(value or {})
        known = {
            name: int(raw[name])
            for name in (
                "target_core_tokens",
                "max_core_tokens",
                "min_core_tokens",
                "context_before_tokens",
                "context_after_tokens",
                "max_segments",
            )
            if name in raw and raw[name] is not None
        }
        return cls(**known)

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Boundary:
    position: int
    kind: str
    penalty: float
    forced: bool = False


@dataclass(frozen=True, slots=True)
class Segment:
    """一个非重叠、可定位、带邻接上下文的翻译正文片段。"""

    segment_id: str
    ordinal: int
    core_text: str
    source_start: int
    source_end: int
    estimated_tokens: int
    context_before: str = ""
    context_after: str = ""
    paragraph_ids: tuple[int, ...] = ()
    boundary_before: str = ""
    boundary_after: str = ""
    hard_split: bool = False
    translatable: bool = True
    source_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["paragraph_ids"] = list(self.paragraph_ids)
        return data


@dataclass(frozen=True, slots=True)
class SegmentationStats:
    original_chars: int
    normalized_chars: int
    estimated_tokens: int
    segment_count: int
    min_segment_tokens: int
    max_segment_tokens: int
    average_segment_tokens: float
    hard_split_count: int
    boundary_counts: dict[str, int] = field(default_factory=dict)
    reconstruction_ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SegmentationResult:
    """一次切片的完整结果。"""

    normalized_text: str
    source_hash: str
    segments: tuple[Segment, ...]
    statistics: SegmentationStats
    config: SegmentationConfig

    @property
    def texts(self) -> list[str]:
        return [segment.core_text for segment in self.segments]

    @property
    def metadata(self) -> list[dict[str, Any]]:
        metadata: list[dict[str, Any]] = []
        for segment in self.segments:
            item = segment.to_dict()
            item.pop("core_text", None)  # 正文已在 segments 中，状态里不重复存一份。
            metadata.append(item)
        return metadata


class DeterministicSegmenter:
    """结构优先、token 预算约束的机械切片器。"""

    def __init__(
        self, config: SegmentationConfig | Mapping[str, Any] | None = None
    ) -> None:
        self.config = SegmentationConfig.from_mapping(config)

    def segment(
        self,
        text: str,
        *,
        chapter_id: str = "chapter",
    ) -> SegmentationResult:
        original = str(text or "")
        normalized = safe_normalize(original)
        source_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if not normalized:
            stats = SegmentationStats(
                original_chars=len(original),
                normalized_chars=0,
                estimated_tokens=0,
                segment_count=0,
                min_segment_tokens=0,
                max_segment_tokens=0,
                average_segment_tokens=0.0,
                hard_split_count=0,
                boundary_counts={},
                reconstruction_ok=True,
            )
            return SegmentationResult(
                normalized_text="",
                source_hash=source_hash,
                segments=(),
                statistics=stats,
                config=self.config,
            )

        budget = _TokenBudget(normalized)
        boundaries = self._scan_boundaries(normalized)
        forced_positions = sorted(
            boundary.position for boundary in boundaries.values() if boundary.forced
        )
        minimum_segments = max(0, len(forced_positions) - 1)
        if minimum_segments > self.config.max_segments:
            raise ValueError(
                f"强边界已要求至少 {minimum_segments} 个切片，超过上限 "
                f"{self.config.max_segments}；请拆分输入章节。"
            )
        spans: list[tuple[int, int, _Boundary, _Boundary]] = []
        for region_start, region_end in zip(forced_positions, forced_positions[1:]):
            if region_end <= region_start:
                continue
            spans.extend(
                self._partition_region(
                    normalized,
                    budget,
                    boundaries,
                    region_start,
                    region_end,
                )
            )

        if len(spans) > self.config.max_segments:
            raise ValueError(
                f"切片数量 {len(spans)} 超过上限 {self.config.max_segments}；"
                "请提高单片预算或拆分输入章节。"
            )

        paragraph_spans = self._paragraph_spans(normalized)
        context_boundaries = sorted(
            position
            for position, boundary in boundaries.items()
            if boundary.kind
            in {"blank_line", "paragraph", "sentence", "semicolon", "heading", "scene"}
        )
        segment_items: list[Segment] = []
        boundary_counts: dict[str, int] = {}
        for ordinal, (start, end, before, after) in enumerate(spans):
            region_start, region_end = self._forced_region(
                forced_positions, start, end, len(normalized)
            )
            context_before = self._context_before(
                normalized,
                budget,
                context_boundaries,
                region_start,
                start,
            )
            context_after = self._context_after(
                normalized,
                budget,
                context_boundaries,
                end,
                region_end,
            )
            core = normalized[start:end]
            hard_split = before.kind == "hard" or after.kind == "hard"
            paragraph_ids = tuple(
                index
                for index, (para_start, para_end) in enumerate(paragraph_spans)
                if para_start < end and para_end > start
            )
            boundary_counts[after.kind] = boundary_counts.get(after.kind, 0) + 1
            segment_items.append(
                Segment(
                    segment_id=f"{chapter_id}#seg{ordinal:04d}",
                    ordinal=ordinal,
                    core_text=core,
                    source_start=start,
                    source_end=end,
                    estimated_tokens=budget.tokens(start, end),
                    context_before=context_before,
                    context_after=context_after,
                    paragraph_ids=paragraph_ids,
                    boundary_before=before.kind,
                    boundary_after=after.kind,
                    hard_split=hard_split,
                    translatable=bool(core.strip()),
                    source_hash=hashlib.sha256(core.encode("utf-8")).hexdigest(),
                )
            )

        reconstructed = "".join(item.core_text for item in segment_items)
        reconstruction_ok = reconstructed == normalized
        if not reconstruction_ok:
            raise RuntimeError("机械切片可逆性校验失败：片段无法还原规范化原文")
        token_sizes = [item.estimated_tokens for item in segment_items]
        stats = SegmentationStats(
            original_chars=len(original),
            normalized_chars=len(normalized),
            estimated_tokens=budget.tokens(0, len(normalized)),
            segment_count=len(segment_items),
            min_segment_tokens=min(token_sizes, default=0),
            max_segment_tokens=max(token_sizes, default=0),
            average_segment_tokens=(
                round(sum(token_sizes) / len(token_sizes), 2) if token_sizes else 0.0
            ),
            # 统计实际硬切边界数，而不是与硬切边界相邻的片段数。
            hard_split_count=boundary_counts.get("hard", 0),
            boundary_counts=boundary_counts,
            reconstruction_ok=True,
        )
        return SegmentationResult(
            normalized_text=normalized,
            source_hash=source_hash,
            segments=tuple(segment_items),
            statistics=stats,
            config=self.config,
        )

    def _scan_boundaries(self, text: str) -> dict[int, _Boundary]:
        boundaries: dict[int, _Boundary] = {}

        def add(position: int, kind: str, *, forced: bool = False) -> None:
            position = max(0, min(position, len(text)))
            candidate = _Boundary(
                position=position,
                kind=kind,
                penalty=_BOUNDARY_PENALTIES[kind],
                forced=forced,
            )
            existing = boundaries.get(position)
            if existing is None or (candidate.forced, -candidate.penalty) > (
                existing.forced,
                -existing.penalty,
            ):
                boundaries[position] = candidate

        add(0, "document_start", forced=True)
        add(len(text), "document_end", forced=True)

        offset = 0
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\n")
            if offset > 0 and _HEADING_RE.match(body):
                add(offset, "heading", forced=True)
            if offset > 0 and _SCENE_BREAK_RE.match(body):
                add(offset, "scene", forced=True)
            offset += len(line)
            if line.endswith("\n"):
                add(offset, "paragraph")

        for match in _BLANK_LINE_RE.finditer(text):
            add(match.end(), "blank_line")
        for match in _SENTENCE_END_RE.finditer(text):
            add(match.end(), "sentence")
        for match in _SEMICOLON_RE.finditer(text):
            add(match.end(), "semicolon")
        for match in _CLAUSE_RE.finditer(text):
            add(match.end(), "clause")
        return boundaries

    def _partition_region(
        self,
        text: str,
        budget: _TokenBudget,
        all_boundaries: dict[int, _Boundary],
        region_start: int,
        region_end: int,
    ) -> list[tuple[int, int, _Boundary, _Boundary]]:
        max_tokens = self.config.max_core_tokens
        local = {
            position: boundary
            for position, boundary in all_boundaries.items()
            if region_start <= position <= region_end
        }
        local[region_start] = all_boundaries[region_start]
        local[region_end] = all_boundaries[region_end]

        # 极端短句/标点密集文本会产生海量同级候选。按小 token 窗口聚类，
        # 每组只保留优先级最高（同级取最靠后）的自然边界，使 DP 不会退化；
        # 完整边界表仍供 context 对齐，不影响原文覆盖和回拼。
        spacing_tokens = max(
            1,
            min(
                32,
                max_tokens // 4,
                max(4, self.config.target_core_tokens // 20),
            ),
        )
        internal = sorted(
            position for position in local if region_start < position < region_end
        )
        thinned: dict[int, _Boundary] = {
            region_start: local[region_start],
            region_end: local[region_end],
        }
        group: list[int] = []

        def retain_group() -> None:
            if not group:
                return
            chosen = min(
                group,
                key=lambda position: (local[position].penalty, -position),
            )
            thinned[chosen] = local[chosen]

        for position in internal:
            if group and budget.tokens(group[0], position) >= spacing_tokens:
                retain_group()
                group = []
            group.append(position)
        retain_group()
        local = thinned

        # 只有高级边界间距超过硬预算时，才补空白切点，避免英文逐词候选膨胀。
        positions = sorted(local)
        for left, right in zip(positions, positions[1:]):
            if budget.tokens(left, right) <= max_tokens:
                continue
            last_retained = left
            for match in _WHITESPACE_RE.finditer(text, left, right):
                position = match.end()
                if (
                    left < position < right
                    and position not in local
                    and budget.tokens(last_retained, position) >= spacing_tokens
                ):
                    local[position] = _Boundary(
                        position, "whitespace", _BOUNDARY_PENALTIES["whitespace"]
                    )
                    last_retained = position

        # 任何仍超过预算的候选间隔都机械补硬切点，确保 DP 一定有解。
        positions = sorted(local)
        for left, right in zip(positions, positions[1:]):
            cursor = left
            while budget.tokens(cursor, right) > max_tokens:
                position = budget.end_for_tokens(
                    cursor, right, self.config.target_core_tokens
                )
                if position <= cursor:
                    position = min(cursor + 1, right)
                if position >= right:
                    break
                local.setdefault(
                    position,
                    _Boundary(position, "hard", _BOUNDARY_PENALTIES["hard"]),
                )
                cursor = position

        candidates = sorted(local)
        count = len(candidates)
        inf = float("inf")
        costs = [inf] * count
        segment_counts = [10**9] * count
        back = [-1] * count
        costs[0] = 0.0
        segment_counts[0] = 0
        target = self.config.target_core_tokens
        minimum = self.config.min_core_tokens

        for i in range(1, count):
            end = candidates[i]
            for j in range(i - 1, -1, -1):
                start = candidates[j]
                size = budget.tokens(start, end)
                if size > max_tokens:
                    break
                if costs[j] == inf:
                    continue
                deviation = (size - target) / target
                size_cost = deviation * deviation * 12.0
                short_cost = 0.0
                if minimum and size < minimum and i != count - 1:
                    short_cost = ((minimum - size) / minimum) * 20.0
                empty_cost = 30.0 if not text[start:end].strip() else 0.0
                boundary_cost = 0.0 if i == count - 1 else local[end].penalty
                candidate_cost = (
                    costs[j] + size_cost + short_cost + empty_cost + boundary_cost
                )
                candidate_segments = segment_counts[j] + 1
                current_key = (round(costs[i], 10), segment_counts[i])
                candidate_key = (round(candidate_cost, 10), candidate_segments)
                if candidate_key < current_key:
                    costs[i] = candidate_cost
                    segment_counts[i] = candidate_segments
                    back[i] = j

        if back[-1] < 0:
            raise RuntimeError("无法在配置的 token 上限内生成机械切片")

        result: list[tuple[int, int, _Boundary, _Boundary]] = []
        cursor = count - 1
        while cursor > 0:
            previous = back[cursor]
            if previous < 0:
                raise RuntimeError("机械切片回溯失败")
            start = candidates[previous]
            end = candidates[cursor]
            result.append((start, end, local[start], local[end]))
            cursor = previous
        result.reverse()
        return result

    @staticmethod
    def _paragraph_spans(text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        offset = 0
        for line in text.splitlines(keepends=True):
            end = offset + len(line)
            if line.strip():
                spans.append((offset, end))
            offset = end
        if offset < len(text) and text[offset:].strip():
            spans.append((offset, len(text)))
        return spans

    @staticmethod
    def _forced_region(
        forced_positions: list[int], start: int, end: int, text_length: int
    ) -> tuple[int, int]:
        left_index = max(0, bisect_right(forced_positions, start) - 1)
        right_index = bisect_left(forced_positions, end)
        region_start = forced_positions[left_index]
        region_end = (
            forced_positions[right_index]
            if right_index < len(forced_positions)
            else text_length
        )
        if region_end < end and right_index + 1 < len(forced_positions):
            region_end = forced_positions[right_index + 1]
        return region_start, region_end

    def _context_before(
        self,
        text: str,
        budget: _TokenBudget,
        candidates: list[int],
        region_start: int,
        start: int,
    ) -> str:
        if start <= region_start or self.config.context_before_tokens <= 0:
            return ""
        lower = budget.start_for_tokens(
            region_start, start, self.config.context_before_tokens
        )
        index = bisect_left(candidates, lower)
        context_start = lower
        if index < len(candidates) and candidates[index] < start:
            context_start = candidates[index]
        return text[context_start:start]

    def _context_after(
        self,
        text: str,
        budget: _TokenBudget,
        candidates: list[int],
        end: int,
        region_end: int,
    ) -> str:
        if end >= region_end or self.config.context_after_tokens <= 0:
            return ""
        upper = budget.end_for_tokens(end, region_end, self.config.context_after_tokens)
        index = bisect_right(candidates, upper) - 1
        context_end = upper
        if index >= 0 and end < candidates[index] <= upper:
            context_end = candidates[index]
        return text[end:context_end]


def segment_text(
    text: str,
    *,
    chapter_id: str = "chapter",
    config: SegmentationConfig | Mapping[str, Any] | None = None,
) -> SegmentationResult:
    """便捷入口：使用确定性切片器处理一章原文。"""
    return DeterministicSegmenter(config).segment(text, chapter_id=chapter_id)
