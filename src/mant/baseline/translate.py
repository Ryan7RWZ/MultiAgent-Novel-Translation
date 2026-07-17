"""M2 单 Agent 基线翻译器（实验对照组）。

本模块实现 **单模型直译 + RAG 注入** 基线，是完整多智能体翻译系统
（调度 / 术语 / 翻译 / 审校 / 润色 / QA 终审 + LangGraph 返工回环，
见 ``mant.agents`` 与 ``mant.workflow``）的 **实验对照基线**：

- 不经过多智能体分工，也没有 QA 不达标携带批注回退返工的回环，
  每个 segment 仅独立调用一次 LLM 直译；
- 调用前逐 segment 通过记忆层做 RAG 注入：``MemoryHub.lookup_terms``
  精确命中术语、``MemoryHub.search_tm`` 召回相似历史句对（翻译记忆），
  一并拼入 prompt；
- 返回 ``injection_stats`` 暴露 RAG 注入量，供 M5 评测做基线对比与
  消融分析（多智能体系统 vs 本基线）。

第三方依赖规则：本模块仅依赖 stdlib；``LLMClient`` / ``MemoryHub``
仅做类型标注（``TYPE_CHECKING`` 导入），保证骨架期仅 stdlib(+numpy)
环境下 ``import mant.baseline.translate`` 必然成功。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型标注，避免运行时依赖其他负责人的模块
    from mant.llm.client import LLMClient
    from mant.memory import MemoryHub
    from mant.memory.models import TermEntry, TMMatch

__all__ = [
    "BaselineTranslator",
    "BASELINE_SYSTEM_PROMPT_TEMPLATE",
    "BASELINE_USER_PROMPT_TEMPLATE",
]

# ----------------------------------------------------------------------
# Prompt 模板（骨架级；M5 实验期可按需迭代，槽位用 str.format 渲染）
# ----------------------------------------------------------------------

BASELINE_SYSTEM_PROMPT_TEMPLATE = (
    "你是一名专业的网络小说翻译引擎（单 Agent 基线，无术语 / 审校 / 润色分工）。"
    "请将源文本忠实、流畅地翻译为{target_lang}，保持人物语气与专有名词一致；"
    "若用户消息中提供了术语表与翻译记忆，必须优先遵循其中的译法。"
    "只输出译文本身，不要附加任何解释。"
)

BASELINE_USER_PROMPT_TEMPLATE = """# 术语表（必须遵循的约定译法）
{glossary_block}

# 翻译记忆（相似历史句对，仅供参考）
{tm_block}

# 待译原文
{segment}

请将以上原文翻译为{target_lang}，只输出译文。"""

#: 无注入时的占位行，让模型明确感知"本次没有参考材料"
_EMPTY_BLOCK = "（无）"

# 候选术语抽取的正则（骨架级简易策略，见 _extract_candidate_terms 的 TODO）
_CJK_RUN_RE = re.compile(r"[一-鿿㐀-䶿]+")
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'-]{1,}")

#: CJK 候选词滑窗 n-gram 的长度范围（连续中文子串按 2~4 字切候选）
_CJK_NGRAM_MIN = 2
_CJK_NGRAM_MAX = 4


class BaselineTranslator:
    """单 Agent 基线翻译器：单模型直译 + 记忆层 RAG 注入（实验对照组）。

    与多智能体系统的差异（即实验变量）：
        - 无术语 Agent 前置抽取与对齐、无审校 / 润色 / QA 终审，
          无"QA 不达标携带批注回退返工"的回环；
        - 仅保留"记忆层 RAG 注入"一个增强手段，便于消融对照。

    参数:
        config: 基线专用配置字典（对应 settings 中可选的 ``baseline.*`` 节，
            缺省也能工作）。支持的键：
            - ``target_lang``: 目标语言名（写入 prompt），默认 ``"English"``；
            - ``max_tm``: 每段注入的 TM 条数上限，默认 5；
            - ``max_candidates``: 每段送查术语库的候选词上限，默认 64；
            - ``max_prompt_preview``: 返回的 prompt 预览截断长度，默认 2000；
            - ``temperature`` / ``max_tokens``: 透传给 ``llm.complete``。

    TODO(配置): 待配置负责人在 settings.example.yaml 增补 ``baseline.*`` 示例节。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.target_lang: str = str(cfg.get("target_lang", "English"))
        self.max_tm: int = int(cfg.get("max_tm", 5))
        self.max_candidates: int = int(cfg.get("max_candidates", 64))
        self.max_prompt_preview: int = int(cfg.get("max_prompt_preview", 2000))
        self.temperature: float = float(cfg.get("temperature", 0.3))
        self.max_tokens: int = int(cfg.get("max_tokens", 4096))

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def translate_chapter(
        self,
        work_id: str,
        chapter_path: str | Path,
        memory: MemoryHub | None,
        llm: LLMClient,
    ) -> dict[str, Any]:
        """逐 segment 直译整章，并统计 RAG 注入量。

        流程：读文件 → 切分 segment → 逐段 ``lookup_terms`` / ``search_tm``
        → 拼 prompt → ``llm.complete`` → 汇总结果与注入统计。

        参数:
            work_id: 作品 ID（术语库 / TM 的命名空间，与记忆层一致）。
            chapter_path: 章节原文 txt 文件路径（UTF-8）。
            memory: 记忆层门面；传 None 时退化为无注入纯直译（统计为 0）。
            llm: LLM 客户端（未配置 API key 时按其约定返回 ``[DRAFT]`` 占位）。

        返回:
            dict，键：
            - ``work_id`` / ``chapter_id`` / ``target_lang``；
            - ``segments``: 切分后的原文段列表；
            - ``translations``: 与 segments 等长的译文列表；
            - ``injection_stats``: 注入统计（见下方代码内注释）；
            - ``prompt_preview``: 单个 segment 的完整用户 prompt（截断），
              供实验复盘 RAG 注入内容。默认取首个 segment；若首段没有任何
              术语 / TM 注入，则改取第一个有注入的 segment，避免预览中
              术语表 / 翻译记忆块恒为占位行而看不到实际注入的约定译法。
        """
        chapter_path = Path(chapter_path)
        text = chapter_path.read_text(encoding="utf-8")
        # 与 M1/多智能体主流程统一基础清洗，避免广告行进入模型与实验结果。
        from mant.pipeline.clean import clean_text

        text = clean_text(text)
        segments = self.split_segments(text)

        # 注入统计：术语/TM 命中总量、有注入的段数、降级计数
        stats: dict[str, int] = {
            "segments_total": len(segments),
            "terms_injected": 0,
            "tm_injected": 0,
            "segments_with_terms": 0,
            "segments_with_tm": 0,
            "memory_errors": 0,  # 记忆层异常/未实现的降级次数
            "llm_errors": 0,     # LLM 调用异常的段数（译文以 [SEGMENT_ERROR] 占位）
        }

        translations: list[str] = []
        prompt_preview = ""
        preview_has_injection = False
        for index, segment in enumerate(segments):
            term_hits = self._lookup_terms_safe(memory, work_id, segment, stats)
            tm_matches = self._search_tm_safe(memory, work_id, segment, stats)
            user_prompt = self.build_prompt(segment, term_hits, tm_matches)
            # prompt_preview 用于复盘 RAG 注入内容：默认取首段；若已取的预览
            # 不含任何注入，则改取第一个有术语 / TM 注入的段（只替换一次）。
            if index == 0 or (
                not preview_has_injection and (term_hits or tm_matches)
            ):
                prompt_preview = user_prompt[: self.max_prompt_preview]
                preview_has_injection = bool(term_hits or tm_matches)
            try:
                translation = llm.complete(
                    self.system_prompt,
                    user_prompt,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - 单段失败不应中断整章
                # TODO(鲁棒性): 引入重试 / 换档（fast→strong）策略后再降级
                stats["llm_errors"] += 1
                translation = f"[SEGMENT_ERROR] {type(exc).__name__}: {exc}"
            translations.append(translation)

        return {
            "work_id": work_id,
            "chapter_id": chapter_path.stem,
            "target_lang": self.target_lang,
            "segments": segments,
            "translations": translations,
            "injection_stats": stats,
            "prompt_preview": prompt_preview,
        }

    # ------------------------------------------------------------------
    # 切分与候选抽取（骨架级简易实现）
    # ------------------------------------------------------------------
    @staticmethod
    def split_segments(text: str) -> list[str]:
        """把章节原文切分为 segment 列表。

        骨架实现：按换行切分，去除空白行。

        TODO(M1 联调): 与管道侧统一句切分策略（标点级断句、对话引号合并、
        超长段二次切分），保证基线与多智能体系统使用同一套 segment 划分，
        实验对照才公平。
        """
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _extract_candidate_terms(self, segment: str) -> list[str]:
        """从 segment 抽取"疑似术语"候选，送术语库做精确匹配。

        骨架策略（故意从简）：
            - 拉丁字母 token（长度 >= 2，覆盖 romanization 人名等）；
            - 连续中文子串的 2~4 字滑窗 n-gram（覆盖中文人名 / 功法名等，
              单字术语不匹配，视为可接受损耗）。

        TODO(术语抽取): 接入 M1 管道产出的术语表做正向最大匹配，
        或改用 NER / LLM 预抽取替换滑窗，减少无效候选与术语库查询量。
        """
        candidates: list[str] = []
        seen: set[str] = set()

        def _add(term: str) -> None:
            if term and term not in seen:
                seen.add(term)
                candidates.append(term)

        for token in _LATIN_TOKEN_RE.findall(segment):
            _add(token)
        for run in _CJK_RUN_RE.findall(segment):
            for n in range(_CJK_NGRAM_MIN, _CJK_NGRAM_MAX + 1):
                for i in range(0, len(run) - n + 1):
                    _add(run[i : i + n])
        return candidates[: self.max_candidates]

    # ------------------------------------------------------------------
    # 记忆层安全调用（骨架期允许对端未实现 / 降级，不向上抛）
    # ------------------------------------------------------------------
    def _lookup_terms_safe(
        self,
        memory: MemoryHub | None,
        work_id: str,
        segment: str,
        stats: dict[str, int],
    ) -> dict[str, TermEntry]:
        """调用 ``MemoryHub.lookup_terms``；异常时降级为无注入并计数。"""
        if memory is None:
            return {}
        candidates = self._extract_candidate_terms(segment)
        if not candidates:
            return {}
        try:
            hits = memory.lookup_terms(candidates, work_id) or {}
        except Exception:  # noqa: BLE001 - 记忆层骨架期允许 NotImplementedError 等
            stats["memory_errors"] += 1
            return {}
        hits = {source: entry for source, entry in hits.items() if entry is not None}
        if hits:
            stats["terms_injected"] += len(hits)
            stats["segments_with_terms"] += 1
        return hits

    def _search_tm_safe(
        self,
        memory: MemoryHub | None,
        work_id: str,
        segment: str,
        stats: dict[str, int],
    ) -> list[TMMatch]:
        """调用 ``MemoryHub.search_tm``；异常时降级为无注入并计数。"""
        if memory is None:
            return []
        try:
            matches = memory.search_tm(segment, work_id, k=self.max_tm) or []
        except Exception:  # noqa: BLE001 - 同上，骨架期宽容降级
            stats["memory_errors"] += 1
            return []
        matches = [m for m in matches if m is not None]
        if matches:
            stats["tm_injected"] += len(matches)
            stats["segments_with_tm"] += 1
        return matches

    # ------------------------------------------------------------------
    # Prompt 拼装
    # ------------------------------------------------------------------
    @property
    def system_prompt(self) -> str:
        """渲染后的系统提示词（含目标语言）。"""
        return BASELINE_SYSTEM_PROMPT_TEMPLATE.format(target_lang=self.target_lang)

    def build_prompt(
        self,
        segment: str,
        term_hits: dict[str, TermEntry],
        tm_matches: list[TMMatch],
    ) -> str:
        """拼装单 segment 的用户提示词（术语表块 + TM 块 + 待译原文）。"""
        return BASELINE_USER_PROMPT_TEMPLATE.format(
            glossary_block=self._render_glossary_block(term_hits),
            tm_block=self._render_tm_block(tm_matches),
            segment=segment,
            target_lang=self.target_lang,
        )

    @staticmethod
    def _entry_field(entry: Any, name: str, default: Any = "") -> Any:
        """宽容读取 TermEntry / TMMatch 字段（兼容 dataclass 与 dict）。"""
        if isinstance(entry, dict):
            return entry.get(name, default)
        return getattr(entry, name, default)

    def _render_glossary_block(self, term_hits: dict[str, TermEntry]) -> str:
        """渲染术语表块：``- 源术语 → 约定译法（分类）``，无命中给占位行。"""
        lines = []
        for source, entry in term_hits.items():
            target = self._entry_field(entry, "target")
            category = self._entry_field(entry, "category")
            line = f"- {source} → {target}"
            if category:
                line += f"（{category}）"
            lines.append(line)
        return "\n".join(lines) if lines else _EMPTY_BLOCK

    def _render_tm_block(self, tm_matches: list[TMMatch]) -> str:
        """渲染 TM 块：相似历史句对的原文 / 译文 / 相似度，无命中给占位行。"""
        lines = []
        for match in tm_matches:
            source = self._entry_field(match, "source")
            target = self._entry_field(match, "target")
            score = self._entry_field(match, "score", None)
            line = f"- 原文：{source}\n  译文：{target}"
            if isinstance(score, (int, float)):
                line += f"（相似度 {score:.2f}）"
            lines.append(line)
        return "\n".join(lines) if lines else _EMPTY_BLOCK
