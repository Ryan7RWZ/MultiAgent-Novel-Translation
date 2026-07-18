"""术语 Agent（TerminologistAgent）：术语抽取入库与译后一致性回归检查。

两种工作模式（经 ``AgentTask.context["mode"]`` 切换，默认 ``extract``）：

``extract`` —— 译前扫描整章原文（``task.source_text`` 为整章原文，
``segment_id`` 用章级占位 ``"chapter"``）：
1. LLM 抽取疑似术语（人名/地名/功法/法宝/组织/头衔等）并给出建议译名；
2. 与术语库比对：同一 ``work_id`` 内"先入库者为准"，命中条目直接作为
   本章生效译名；未命中条目经 ``MemoryHub.record_terms`` 回写入库；
3. 冲突条目（新抽取 ``confidence`` 高出库内 0.2 以上）仅记"待人工裁决"，
   不自动覆盖（docs/agent-design.md §3.2）。
output 键：``glossary``（``dict[str, str]``，本章生效映射，对齐
``TranslationState.glossary``）、``new_terms``（``list[dict]``，
元素结构对齐 ``TermEntry``）。

``check`` —— 译后回归检查（启发式骨架）：
对照术语表检查译文是否使用了规范译名。
output 键：``term_violations``（``list[dict]``，每条含
``type / term_source / expected_target / detail``）。

模型档位建议：fast（结构化抽取、输出空间小，docs/agent-design.md §4）。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from mant.agents.base import AgentResult, AgentTask, BaseAgent
from mant.memory.models import TermEntry

if TYPE_CHECKING:  # 仅类型标注，避免运行时循环依赖
    from mant.llm.client import LLMClient
    from mant.memory import MemoryHub

__all__ = ["TerminologistAgent"]

#: 冲突仲裁阈值：新抽取 confidence 高出库内条目该值时记"待人工裁决"
_ARBITRATION_CONFIDENCE_GAP = 0.2


class TerminologistAgent(BaseAgent):
    """术语 Agent：extract / check 双模式（详见模块 docstring）。"""

    name = "terminologist"

    #: 期望模型档位（结构化抽取，高频轻量调用）
    tier = "fast"
    temperature: float = 0.2
    max_tokens: int = 1024
    structured_json: bool = True
    thinking: str | None = None

    #: 支持的运行模式
    MODE_EXTRACT = "extract"
    MODE_CHECK = "check"

    #: 术语类别枚举（写入 Prompt 的硬约束，docs/agent-design.md §3.2）
    CATEGORIES = ("person", "place", "skill", "item", "faction", "title", "other")

    SYSTEM_PROMPT = """你是一名网络小说术语抽取专家，熟悉修仙/玄幻/都市等中文网文类型。
从给定原文中抽取需要在全书中保持译名一致的术语：人名、地名、功法/技能名、
法宝/器物名、势力/组织名、头衔/境界称谓及其他反复出现的专有名词。

硬约束：
1. 只输出紧凑 JSON 对象，禁止任何额外文本；无新术语时输出 {"terms": []}；
2. 格式固定为 {"terms": [{"source": 源术语, "target": 建议译名, "category": 类别, "confidence": 0~1}]}；
3. category 仅可取 person/place/skill/item/faction/title/other；
4. 禁止臆造原文中不存在的术语；拿不准的译名给低 confidence。
TODO: 补 2–3 组网文风格 few-shot（境界名、法宝名）。"""

    #: 用户提示词模板：{target_lang} 目标语言 / {chapter_id} 章节 / {source_text} 原文
    USER_PROMPT_TEMPLATE = """目标语言：{target_lang}
章节：{chapter_id}
原文：
<source>
{source_text}
</source>"""

    # ------------------------------------------------------------------
    # BaseAgent 接口：按 mode 分派
    # ------------------------------------------------------------------
    def run(self, task: AgentTask) -> AgentResult:
        """按 ``task.context["mode"]`` 分派到 extract / check 模式。"""
        mode = str(task.context.get("mode", self.MODE_EXTRACT)).strip().lower()
        if mode == self.MODE_EXTRACT:
            return self._run_extract(task)
        if mode == self.MODE_CHECK:
            return self._run_check(task)
        return self._result(
            ok=False,
            output={},
            notes=[f"未知模式 {mode!r}：仅支持 {self.MODE_EXTRACT!r} / {self.MODE_CHECK!r}。"],
        )

    # ------------------------------------------------------------------
    # extract 模式：抽取 → 比对 → 回写
    # ------------------------------------------------------------------
    def _run_extract(self, task: AgentTask) -> AgentResult:
        """从原文抽取候选术语，去重后回写术语库并产出本章生效映射。

        TODO:
        - 与 M1 管道（mant.pipeline.extract_terms）的离线初筛结果合并，
          线上只做增量抽取，降低 LLM 调用量（docs §5.3 降本）；
        - 抽取结果按章缓存，避免同章重复抽取；
        - 低 confidence 条目批量送 LLM/人工复核工作流。
        """
        notes: list[str] = []
        target_lang = str(task.context.get("target_lang", "English"))

        # 1) LLM 抽取候选术语（JSON 数组；[DRAFT] 占位时走降级路径）
        resp = self.llm.complete(
            self.SYSTEM_PROMPT,
            self.build_user_prompt(
                self.USER_PROMPT_TEMPLATE,
                target_lang=target_lang,
                chapter_id=task.chapter_id,
                source_text=task.source_text,
            ),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            response_format=(
                {"type": "json_object"} if self.structured_json else None
            ),
            thinking=self.thinking,
        )
        notes.extend(self.llm.last_notes)
        parsed = self.parse_json_output(resp, notes)
        return self.reconcile_candidates(
            task,
            parsed,
            notes=notes,
            extraction_valid=parsed is not None,
        )

    def reconcile_candidates(
        self,
        task: AgentTask,
        raw_candidates: Any,
        *,
        notes: list[str] | None = None,
        extraction_valid: bool = True,
    ) -> AgentResult:
        """确定性去重候选，再与术语库仲裁并一次性写入。"""
        collected_notes = list(notes or [])
        parsed_candidates = self._parse_candidates(
            raw_candidates, task.work_id, collected_notes
        )
        by_source: dict[str, TermEntry] = {}
        for candidate in parsed_candidates:
            previous = by_source.get(candidate.source)
            if previous is None or candidate.confidence > previous.confidence:
                by_source[candidate.source] = candidate
        candidates = list(by_source.values())

        # 确定性命中已有术语。此路径不依赖 LLM，保证离线术语仍能注入。
        known: dict[str, TermEntry] = {}
        if self.memory is not None and hasattr(self.memory, "match_terms"):
            try:
                known = self.memory.match_terms(task.source_text, task.work_id)
            except Exception as exc:  # noqa: BLE001
                collected_notes.append(
                    f"match_terms 调用失败，跳过已有术语全文匹配：{exc!r}"
                )
        glossary: dict[str, str] = {
            source: entry.target for source, entry in known.items() if entry.target
        }

        if not candidates:
            collected_notes.append(
                f"本轮未获得新术语候选；已从术语库直接命中 {len(glossary)} 条。"
            )
            return self._result(
                ok=extraction_valid or bool(glossary),
                output={"glossary": glossary, "new_terms": []},
                notes=collected_notes,
            )

        # 2) 与库内条目比对（先入库者为准）
        new_terms: list[TermEntry] = []
        arbitration: list[str] = []
        existing: dict[str, TermEntry] = {}
        if self.memory is not None:
            try:
                existing = self.memory.lookup_terms(
                    [c.source for c in candidates], task.work_id
                )
                existing.update(known)
            except Exception as exc:  # noqa: BLE001 —— 记忆层故障不阻断主流程
                collected_notes.append(
                    f"lookup_terms 调用失败，跳过库内比对：{exc!r}"
                )
        else:
            collected_notes.append(
                "memory 为 None，候选术语全部视为新术语且不落库。"
            )

        for cand in candidates:
            hit = existing.get(cand.source)
            if hit is not None:
                # 先入库者为准：本章生效译名取库内译名
                glossary[hit.source] = hit.target
                if cand.confidence > hit.confidence + _ARBITRATION_CONFIDENCE_GAP:
                    arbitration.append(
                        f"待人工裁决：{cand.source!r} 新译 {cand.target!r}"
                        f"(conf={cand.confidence:.2f}) vs 库内 {hit.target!r}"
                        f"(conf={hit.confidence:.2f})，未自动覆盖。"
                    )
            else:
                glossary[cand.source] = cand.target
                new_terms.append(cand)

        # 3) 新术语回写术语库
        if new_terms and self.memory is not None:
            try:
                self.memory.record_terms(new_terms)
                collected_notes.append(f"新术语入库 {len(new_terms)} 条。")
            except Exception as exc:  # noqa: BLE001
                collected_notes.append(
                    f"record_terms 调用失败，本轮新术语未持久化：{exc!r}"
                )
                collected_notes.extend(arbitration)
                return self._result(
                    ok=False,
                    output={
                        "glossary": glossary,
                        "new_terms": [asdict(t) for t in new_terms],
                    },
                    notes=collected_notes,
                )

        collected_notes.insert(
            0,
            f"术语扫描完成：候选 {len(candidates)} 条，新入库 {len(new_terms)} 条，"
            f"命中库内 {len(candidates) - len(new_terms)} 条。",
        )
        collected_notes.extend(arbitration)
        return self._result(
            ok=True,
            output={
                "glossary": glossary,
                "new_terms": [asdict(t) for t in new_terms],
            },
            notes=collected_notes,
        )

    def _parse_candidates(
        self, parsed: Any, work_id: str, notes: list[str]
    ) -> list[TermEntry]:
        """把 JSON 解析结果规整为 ``TermEntry`` 列表（宽容处理脏数据）。"""
        if parsed is None:
            return []
        if isinstance(parsed, dict) and isinstance(parsed.get("terms"), list):
            items = parsed["terms"]
        else:
            items = parsed if isinstance(parsed, list) else [parsed]
        candidates: list[TermEntry] = []
        dropped = 0
        for item in items:
            if not isinstance(item, dict):
                dropped += 1
                continue
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            if not source or not target:
                dropped += 1
                continue
            category = str(item.get("category", "other")).strip() or "other"
            if category not in self.CATEGORIES:
                category = "other"
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            candidates.append(
                TermEntry(
                    source=source,
                    target=target,
                    category=category,
                    work_id=work_id,
                    confidence=max(0.0, min(1.0, confidence)),
                )
            )
        if dropped:
            notes.append(f"丢弃畸形候选条目 {dropped} 条。")
        return candidates

    # ------------------------------------------------------------------
    # check 模式：译后术语一致性回归检查（启发式骨架）
    # ------------------------------------------------------------------
    def _run_check(self, task: AgentTask) -> AgentResult:
        """对照术语表检查译文是否使用了规范译名。

        约定（check 模式）：
        - ``task.source_text`` 为**待检译文**；
        - ``task.context["source_text"]`` 可选：对应原文，用于判断术语是否
          应出现（缺省时退化为"规范译名必须在译文中出现"的强约束，可能误报）；
        - ``task.context["glossary"]`` 为术语表：``{源术语: 规范译名}`` 扁平
          dict（兼容 ``{"terms": {...}}`` 嵌套形态及 ``TermEntry``/dict 值）。

        TODO:
        - 接入 LLM 深度检查：译名变形、大小写、所有格、部分匹配；
        - 与 QA Agent 的 terminology 维度（docs §3.6）对齐，避免重复报告；
        - glossary 缺省时自动从 MemoryHub 拉取本章术语表。
        """
        glossary = self._normalize_glossary(task.context.get("glossary") or {})
        translation = task.source_text
        original = str(task.context.get("source_text", ""))

        violations: list[dict] = []
        for term_source, expected in glossary.items():
            if not term_source or not expected:
                continue
            # 有原文且原文不含该术语 → 本轮不应出现，跳过
            if original and term_source not in original:
                continue
            if expected.lower() not in translation.lower():
                detail = (
                    f"原文包含术语 {term_source!r}，但译文未使用规范译名 {expected!r}。"
                    if original
                    else f"规范译名 {expected!r}（源术语 {term_source!r}）未在译文中出现"
                    "（缺原文对照，可能误报）。"
                )
                violations.append(
                    {
                        "type": "term_inconsistency",
                        "term_source": term_source,
                        "expected_target": expected,
                        "detail": detail,
                    }
                )
        notes = [
            f"术语回归检查：检查 {len(glossary)} 条，发现 {len(violations)} 处不一致。"
        ]
        return self._result(
            ok=True, output={"term_violations": violations}, notes=notes
        )

    @staticmethod
    def _normalize_glossary(raw: Any) -> dict[str, str]:
        """把多种形态的术语表输入规整为 ``{源术语: 规范译名}``。"""
        if not isinstance(raw, dict):
            return {}
        terms = raw.get("terms") if isinstance(raw.get("terms"), dict) else raw
        result: dict[str, str] = {}
        for key, value in terms.items():
            if isinstance(value, str):
                result[str(key)] = value
            elif isinstance(value, TermEntry):
                result[str(key)] = value.target
            elif isinstance(value, dict) and value.get("target"):
                result[str(key)] = str(value["target"])
        return result
