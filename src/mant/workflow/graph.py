"""LangGraph 状态机：retrieve → 分片 translate/edit/polish/qa → 归并主链路。

回环（QA 返工）：``qa_verdict == "rework"`` 且 ``rework_count < max_rework``
时，经条件边携带 ``review_notes`` 批注回退 translate 节点重译；否则进入 END。
达到上限仍判返工时强制放行并打 ``needs_human_review`` 标记
（docs/architecture.md §3.3）。

依赖约定
--------
- ``langgraph`` 为第三方依赖，**延迟导入**（仅在 ``build_graph`` 内 import）：
  未安装时 ``import mant.workflow.graph`` 不报错，``build_graph`` 抛出
  带安装提示的 ``ImportError``；
- 四个业务 Agent 按同一 segment 序列执行，output 键契约如下：
    - ``TranslatorAgent``：``{"draft": str}``；context 键 ``glossary`` /
      ``story_bible`` / ``tm_matches`` / ``prev_summary``；
    - ``EditorAgent``：``{"review_notes": list[dict]}``（只提意见不改稿）；
      context 键 ``draft``（必需）/ ``glossary``；
    - ``PolisherAgent``：``{"polished": str}``；context 键 ``draft``（必需）/
      ``review_notes``；
    - ``QAAgent``：``{"qa_score": float, "qa_verdict": "pass"|"rework",
      "qa_detail": dict}``（``qa_detail.suggestions`` 为返工建议，并入
      ``review_notes`` 驱动下一轮重译）；章级分数由工作流按源片 token 加权；
- 各 Agent 的模型档位按其 ``tier`` 类属性经 ``LLMClient.with_tier`` 选档。

TODO（待团队同步）：
- 回退落点当前固定为 translate（docs §3.3 预留改回 edit 的扩展点）；
- ``review_notes`` 按轮次标记/清理已解决批注，避免历史批注无限累积。
- 大量 segment 当前顺序执行；后续需在供应商限流约束下实现有界并发和断点续跑。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from mant.agents.base import AgentTask, BaseAgent
from mant.agents.editor import EditorAgent
from mant.agents.orchestrator import OrchestratorAgent
from mant.agents.polisher import PolisherAgent
from mant.agents.qa import QAAgent
from mant.agents.terminologist import TerminologistAgent
from mant.agents.translator import TranslatorAgent
from mant.observability import emit_event, event_scope, run_context
from mant.workflow.state import DEFAULT_MAX_REWORK, TranslationState, init_state

if TYPE_CHECKING:  # 仅类型标注，避免运行时循环依赖
    from mant.llm.client import LLMClient
    from mant.memory import MemoryHub
    from mant.observability import RunObserver

__all__ = ["build_graph", "run_chapter", "DEFAULT_MAX_REWORK"]

#: 判定为"返工"的 QA 结论取值；实现契约为 "rework"（兼容 docs 旧称 "fail"）
_REWORK_VERDICTS = ("rework", "fail")

#: 主链路业务 Agent：节点角色 → 类
_AGENT_CLASSES: dict[str, type[BaseAgent]] = {
    "translator": TranslatorAgent,
    "editor": EditorAgent,
    "polisher": PolisherAgent,
    "qa": QAAgent,
}


def _pick_client(llm: "LLMClient", agent_cls: type[BaseAgent]) -> "LLMClient":
    """按 Agent 声明的 ``tier`` 类属性选档（docs/agent-design.md §4）。"""
    tier = getattr(agent_cls, "tier", None)
    if tier and hasattr(llm, "with_tier"):
        return llm.with_tier(tier)
    return llm


def _build_agents(llm: "LLMClient", memory: "MemoryHub | None") -> dict[str, BaseAgent]:
    """实例化主链路四个业务 Agent（各自按 tier 选档后的客户端）。"""
    return {
        role: agent_cls(_pick_client(llm, agent_cls), memory)
        for role, agent_cls in _AGENT_CLASSES.items()
    }


def _resolve_limit(state: TranslationState, fallback: int) -> int:
    """返工上限取值：优先 state["max_rework"]，缺省回退到构图参数。"""
    raw = state.get("max_rework")
    return fallback if raw is None else int(raw)


def _source_text(state: TranslationState) -> str:
    """读取无损章级原文；兼容未带新字段的旧状态。"""
    source = state.get("source_text")
    return str(source) if source is not None else "".join(state.get("segments") or [])


def _segment_meta(state: TranslationState, index: int) -> dict[str, Any]:
    items = state.get("segment_meta") or []
    return dict(items[index]) if index < len(items) else {}


def _segment_id(state: TranslationState, index: int) -> str:
    meta = _segment_meta(state, index)
    return str(meta.get("segment_id") or f"{state['chapter_id']}#seg{index:04d}")


def _notes_for_segment(
    notes: list[Any], segment_id: str, segment_index: int
) -> list[Any]:
    """只向当前片注入匹配批注；未标片段的旧批注仍视为章级约束。"""
    selected: list[Any] = []
    for note in notes:
        if not isinstance(note, dict):
            selected.append(note)
            continue
        note_id = note.get("segment_id")
        note_index = note.get("segment_index")
        if note_id is None and note_index is None:
            selected.append(note)
        elif str(note_id or "") == segment_id or note_index == segment_index:
            selected.append(note)
    return selected


def _segment_failure(
    *, stage: str, segment_id: str, segment_index: int, reason: str
) -> dict[str, Any]:
    return {
        "stage": stage,
        "segment_id": segment_id,
        "segment_index": segment_index,
        "reason": reason,
    }


def build_graph(
    llm: "LLMClient",
    memory: "MemoryHub | None",
    max_rework: int = DEFAULT_MAX_REWORK,
    *,
    min_polished_segment_ratio: float = 0.5,
    max_polished_segment_ratio: float = 2.5,
) -> Any:
    """构建并编译单章翻译状态机。

    流程：retrieve（检索记忆注入）→ translate → edit → polish → qa；
    条件边：QA 判 ``rework`` 且未达上限 → 回 translate；否则 → END。

    参数:
        llm: 统一 LLM 客户端（``mant.llm.client.LLMClient``）。
        memory: 记忆层门面（``mant.memory.MemoryHub``）；可为 None，
            离线模式跳过检索注入，全链路仍可跑通。
        max_rework: 返工上限兜底值（优先以 ``state["max_rework"]`` 为准）。
        min_polished_segment_ratio / max_polished_segment_ratio: 润色稿相对初稿
            的字符长度完整性范围；越界时丢弃该片润色稿并回退初稿。

    返回:
        langgraph 编译后的可调用图（``app.invoke(initial_state)``）。
    """
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:  # 第三方依赖延迟导入 + 安装提示
        raise ImportError(
            "构建翻译工作流需要 langgraph，请先安装：pip install langgraph"
        ) from exc

    agents = _build_agents(llm, memory)
    terminologist = TerminologistAgent(_pick_client(llm, TerminologistAgent), memory)
    min_polished_segment_ratio = float(min_polished_segment_ratio)
    max_polished_segment_ratio = float(max_polished_segment_ratio)
    if not 0 < min_polished_segment_ratio <= 1:
        raise ValueError("min_polished_segment_ratio 必须位于 (0, 1] 区间")
    if max_polished_segment_ratio < 1:
        raise ValueError("max_polished_segment_ratio 必须至少为 1")
    if max_polished_segment_ratio < min_polished_segment_ratio:
        raise ValueError("润色稿最大长度比例不能小于最小比例")

    # ------------------------------------------------------------------
    # 节点定义（检索产物全部随 TranslationState 流转，可并发复用 compiled graph）
    # ------------------------------------------------------------------
    def retrieve_node(state: TranslationState) -> dict:
        """检索记忆注入：圣经 / TM / 运行说明与术语表均写入 state。"""
        runtime_notes: list[str] = []
        glossary: dict[str, Any] = dict(state.get("glossary") or {})
        if memory is None:
            runtime_notes.append("memory 为 None，跳过记忆检索注入（离线模式）")
            term_result = terminologist.execute(
                AgentTask(
                    work_id=state["work_id"],
                    chapter_id=state["chapter_id"],
                    segment_id="chapter",
                    source_text=_source_text(state),
                    context={"mode": "extract", "story_bible": None},
                )
            )
            runtime_notes.extend(term_result.notes)
            glossary.update(term_result.output.get("glossary") or {})
            emit_event(
                "memory.retrieved",
                payload={"offline": True, "glossary_terms": len(glossary)},
            )
            return {
                "glossary": glossary,
                "story_bible": None,
                "tm_matches": [],
                "runtime_notes": runtime_notes,
            }

        work_id = state["work_id"]

        # ① 小说圣经（StoryBible → dict，供 context["story_bible"] 使用）
        try:
            bible = memory.get_story_bible(work_id)
            story_bible = bible.to_dict() if hasattr(bible, "to_dict") else bible
        except Exception as exc:  # noqa: BLE001 —— 骨架期记忆层故障不阻断流程
            story_bible = None
            runtime_notes.append(f"get_story_bible 失败：{exc!r}")

        # ② 翻译记忆 TM：逐 segment 取 top-k 并序列化
        # TODO: 大章节检索开销控制（限量/去重/按 token 预算截断）；
        #       score ≥ 0.95 的高相似命中可直接复用译文跳过翻译（docs §5.3）
        tm_matches: list[dict[str, Any]] = []
        for idx, seg in enumerate(state["segments"]):
            try:
                for m in memory.search_tm(seg, work_id, k=3):
                    tm_matches.append(
                        {
                            "segment_index": idx,
                            "source": m.source,
                            "target": m.target,
                            "score": m.score,
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                runtime_notes.append(f"search_tm(seg#{idx}) 失败：{exc!r}")

        # ③ 术语注入：术语 Agent（extract 模式）扫描整章 → 本章生效映射
        term_result = terminologist.execute(
            AgentTask(
                work_id=work_id,
                chapter_id=state["chapter_id"],
                segment_id="chapter",  # 章级任务占位（docs §1 约定）
                source_text=_source_text(state),
                context={
                    "mode": "extract",
                    "story_bible": story_bible,
                },
            )
        )
        runtime_notes.extend(term_result.notes)
        glossary.update(term_result.output.get("glossary") or {})
        emit_event(
            "memory.retrieved",
            payload={
                "offline": False,
                "has_story_bible": bool(story_bible),
                "tm_matches": len(tm_matches),
                "glossary_terms": len(glossary),
            },
        )
        return {
            "glossary": glossary,
            "story_bible": story_bible,
            "tm_matches": tm_matches,
            "runtime_notes": runtime_notes,
        }

    def translate_node(state: TranslationState) -> dict:
        """逐 segment 初译并拼接；返工轮携带批注并累计 rework_count。"""
        notes = list(state.get("review_notes") or [])
        rework_count = int(state.get("rework_count", 0))
        if str(state.get("qa_verdict", "")).strip().lower() in _REWORK_VERDICTS:
            rework_count += 1  # 本轮由 QA 判返触发，记一次实际返工
        tm_matches = state.get("tm_matches") or []
        runtime_notes = list(state.get("runtime_notes") or [])
        segment_meta = state.get("segment_meta") or []
        previous_parts = state.get("draft_segments") or []
        parts: list[str] = []
        failures: list[dict[str, Any]] = []
        for idx, seg in enumerate(state["segments"]):
            meta = segment_meta[idx] if idx < len(segment_meta) else {}
            segment_id = str(
                meta.get("segment_id") or f"{state['chapter_id']}#seg{idx:04d}"
            )
            task = AgentTask(
                work_id=state["work_id"],
                chapter_id=state["chapter_id"],
                segment_id=segment_id,
                source_text=seg,
                context={
                    # 与 TranslatorAgent 已实现的 context 键对齐
                    "glossary": state.get("glossary") or {},
                    "story_bible": state.get("story_bible"),
                    "tm_matches": [
                        m for m in tm_matches if m.get("segment_index") == idx
                    ],
                    "prev_summary": "",  # TODO: 由圣经 timeline/上一章摘要生成
                    # 返工批注由 TranslatorAgent 作为最高优先级硬约束消费
                    "review_notes": _notes_for_segment(notes, segment_id, idx),
                    "round": rework_count,
                    # 仅作理解辅助，译者 Prompt 明确要求不得翻译或输出。
                    "context_before": str(meta.get("context_before") or ""),
                    "context_after": str(meta.get("context_after") or ""),
                },
            )
            result = agents["translator"].execute(task)
            runtime_notes.extend(result.notes)
            draft = str(result.output.get("draft") or "")
            if result.ok and draft.strip():
                parts.append(draft)
            else:
                fallback = previous_parts[idx] if idx < len(previous_parts) else seg
                parts.append(fallback)
                failures.append(
                    _segment_failure(
                        stage="translate",
                        segment_id=segment_id,
                        segment_index=idx,
                        reason="翻译调用失败或返回空/不完整结果，已使用上一轮译稿或原文兜底",
                    )
                )
            emit_event(
                "stage.segment_completed",
                payload={
                    "stage": "translate",
                    "segment_index": idx,
                    "segment_count": len(state["segments"]),
                    "ok": result.ok and bool(draft.strip()),
                },
            )
        return {
            "draft": "\n\n".join(parts),
            "draft_segments": parts,
            "segment_failures": failures,
            "rework_count": rework_count,
            "runtime_notes": runtime_notes,
        }

    def edit_node(state: TranslationState) -> dict:
        """逐 segment 对照审校；所有意见带 segment 定位后确定性归并。"""
        draft_parts = state.get("draft_segments") or []
        runtime_notes = list(state.get("runtime_notes") or [])
        merged = list(state.get("review_notes") or [])
        failures = list(state.get("segment_failures") or [])
        for idx, source in enumerate(state["segments"]):
            segment_id = _segment_id(state, idx)
            draft = draft_parts[idx] if idx < len(draft_parts) else ""
            result = agents["editor"].execute(
                AgentTask(
                    work_id=state["work_id"],
                    chapter_id=state["chapter_id"],
                    segment_id=segment_id,
                    source_text=source,
                    context={
                        "draft": draft,
                        "glossary": state.get("glossary") or {},
                        "round": int(state.get("rework_count", 0)),
                    },
                )
            )
            runtime_notes.extend(result.notes)
            if not result.ok:
                failures.append(
                    _segment_failure(
                        stage="edit",
                        segment_id=segment_id,
                        segment_index=idx,
                        reason="审校调用失败或输出无法解析",
                    )
                )
            for raw_note in result.output.get("review_notes") or []:
                note = dict(raw_note) if isinstance(raw_note, dict) else {
                    "suggestion": str(raw_note)
                }
                note["segment_id"] = segment_id
                note["segment_index"] = idx
                merged.append(note)
            emit_event(
                "stage.segment_completed",
                payload={
                    "stage": "edit",
                    "segment_index": idx,
                    "segment_count": len(state["segments"]),
                    "ok": result.ok,
                },
            )
        return {
            "review_notes": merged,
            "segment_failures": failures,
            "runtime_notes": runtime_notes,
        }

    def polish_node(state: TranslationState) -> dict:
        """逐 segment 润色并做长度完整性检查；异常片回退对应初稿。"""
        draft_parts = state.get("draft_segments") or []
        runtime_notes = list(state.get("runtime_notes") or [])
        failures = list(state.get("segment_failures") or [])
        polished_parts: list[str] = []
        all_notes = list(state.get("review_notes") or [])
        for idx, source in enumerate(state["segments"]):
            segment_id = _segment_id(state, idx)
            draft = draft_parts[idx] if idx < len(draft_parts) else ""
            result = agents["polisher"].execute(
                AgentTask(
                    work_id=state["work_id"],
                    chapter_id=state["chapter_id"],
                    segment_id=segment_id,
                    source_text=source,
                    context={
                        "draft": draft,
                        "review_notes": _notes_for_segment(all_notes, segment_id, idx),
                        "round": int(state.get("rework_count", 0)),
                    },
                )
            )
            runtime_notes.extend(result.notes)
            candidate = str(result.output.get("polished") or "")
            ratio = len(candidate.strip()) / max(1, len(draft.strip()))
            draft_mode = candidate.lstrip().startswith("[DRAFT]")
            complete = (
                result.ok
                and bool(candidate.strip())
                and (
                    draft_mode
                    or min_polished_segment_ratio
                    <= ratio
                    <= max_polished_segment_ratio
                )
            )
            if complete:
                polished_parts.append(candidate)
            else:
                polished_parts.append(draft)
                reason = (
                    "润色调用失败或返回空结果"
                    if not candidate.strip()
                    else f"润色稿长度比例 {ratio:.3f} 超出完整性范围"
                )
                failures.append(
                    _segment_failure(
                        stage="polish",
                        segment_id=segment_id,
                        segment_index=idx,
                        reason=reason + "，已回退该片初稿",
                    )
                )
                emit_event(
                    "output.integrity_failed",
                    payload={
                        "stage": "polish",
                        "segment_index": idx,
                        "draft_chars": len(draft),
                        "output_chars": len(candidate),
                        "ratio": round(ratio, 4),
                    },
                )
            emit_event(
                "stage.segment_completed",
                payload={
                    "stage": "polish",
                    "segment_index": idx,
                    "segment_count": len(state["segments"]),
                    "ok": complete,
                },
            )
        return {
            "polished_segments": polished_parts,
            "polished": "\n\n".join(polished_parts),
            "segment_failures": failures,
            "runtime_notes": runtime_notes,
        }

    def qa_node(state: TranslationState) -> dict:
        """逐 segment QA；分数按 token 权重汇总，任一失败则章级 rework。"""
        polished_parts = state.get("polished_segments") or []
        draft_parts = state.get("draft_segments") or []
        runtime_notes = list(state.get("runtime_notes") or [])
        merged = list(state.get("review_notes") or [])
        failures = list(state.get("segment_failures") or [])
        qa_segments: list[dict[str, Any]] = []
        weighted_score = 0.0
        total_weight = 0
        all_pass = True
        for idx, source in enumerate(state["segments"]):
            segment_id = _segment_id(state, idx)
            polished = (
                polished_parts[idx]
                if idx < len(polished_parts)
                else (draft_parts[idx] if idx < len(draft_parts) else "")
            )
            result = agents["qa"].execute(
                AgentTask(
                    work_id=state["work_id"],
                    chapter_id=state["chapter_id"],
                    segment_id=segment_id,
                    source_text=source,
                    context={
                        "polished": polished,
                        "glossary": state.get("glossary") or {},
                        "review_notes": _notes_for_segment(merged, segment_id, idx),
                        "round": int(state.get("rework_count", 0)),
                    },
                )
            )
            runtime_notes.extend(result.notes)
            out = result.output
            try:
                score = float(out.get("qa_score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            segment_verdict = (
                str(out.get("qa_verdict", "rework")).strip().lower() or "rework"
            )
            if segment_verdict not in ("pass", "rework"):
                segment_verdict = "rework"
            detail = out.get("qa_detail") if isinstance(out.get("qa_detail"), dict) else {}
            qa_segments.append(
                {
                    "segment_id": segment_id,
                    "segment_index": idx,
                    "qa_score": score,
                    "qa_verdict": segment_verdict,
                    "qa_detail": detail,
                    "ok": result.ok,
                }
            )
            weight = max(
                1,
                int(
                    _segment_meta(state, idx).get("estimated_tokens")
                    or len(source)
                ),
            )
            weighted_score += score * weight
            total_weight += weight
            if not result.ok or segment_verdict != "pass":
                all_pass = False
            if not result.ok:
                failures.append(
                    _segment_failure(
                        stage="qa",
                        segment_id=segment_id,
                        segment_index=idx,
                        reason="QA 调用失败或输出无法解析",
                    )
                )
            for suggestion in detail.get("suggestions") or []:
                merged.append(
                    {
                        "issue_type": "qa",
                        "severity": "high" if segment_verdict == "rework" else "low",
                        "span": segment_id,
                        "suggestion": str(suggestion),
                        "segment_id": segment_id,
                        "segment_index": idx,
                    }
                )
            emit_event(
                "stage.segment_completed",
                payload={
                    "stage": "qa",
                    "segment_index": idx,
                    "segment_count": len(state["segments"]),
                    "ok": result.ok,
                    "verdict": segment_verdict,
                },
            )

        score = round(weighted_score / total_weight, 2) if total_weight else 0.0
        verdict = "pass" if all_pass and not failures and qa_segments else "rework"
        emit_event(
            "qa.aggregated",
            payload={
                "segment_count": len(qa_segments),
                "pass_count": sum(
                    item["qa_verdict"] == "pass" for item in qa_segments
                ),
                "failure_count": len(failures),
                "verdict": verdict,
            },
            metrics={"qa_score": score},
        )
        # 上限兜底：判返但已达上限 → 强制放行并打人工复核标记（docs §3.3）
        if verdict in _REWORK_VERDICTS and int(
            state.get("rework_count", 0)
        ) >= _resolve_limit(state, max_rework):
            marker = "needs_human_review：已达返工上限仍判 rework，强制放行当前稿。"
            if marker not in merged:
                merged.append(marker)
        return {
            "qa_score": score,
            "qa_verdict": verdict,
            "review_notes": merged,
            "segment_failures": failures,
            "segment_qa": qa_segments,
            "runtime_notes": runtime_notes,
        }

    def _route_after_qa(state: TranslationState) -> str:
        """条件边：rework 且未达上限 → 回 translate 携带批注重译；否则 END。"""
        verdict = str(state.get("qa_verdict", "")).strip().lower()
        if verdict in _REWORK_VERDICTS and int(
            state.get("rework_count", 0)
        ) < _resolve_limit(state, max_rework):
            emit_event("workflow.route", payload={"route": "rework", "verdict": verdict})
            return "rework"
        emit_event("workflow.route", payload={"route": "end", "verdict": verdict})
        return "end"

    def _observed_node(name: str, fn):
        """为 LangGraph 节点统一补齐起止、耗时与更新字段事件。"""
        def wrapped(state: TranslationState) -> dict:
            started = time.perf_counter()
            with event_scope(node=name, round=int(state.get("rework_count", 0) or 0)):
                emit_event("node.started", payload={"state_segments": len(state["segments"])})
                try:
                    update = fn(state)
                except Exception as exc:
                    emit_event(
                        "node.failed",
                        payload={"error": type(exc).__name__},
                        metrics={
                            "duration_ms": round(
                                (time.perf_counter() - started) * 1000, 2
                            )
                        },
                    )
                    raise
                emit_event(
                    "node.completed",
                    payload={"updated_fields": sorted(update)},
                    metrics={
                        "duration_ms": round(
                            (time.perf_counter() - started) * 1000, 2
                        )
                    },
                )
                return update

        return wrapped

    # ------------------------------------------------------------------
    # 组装图
    # ------------------------------------------------------------------
    graph = StateGraph(TranslationState)
    graph.add_node("retrieve", _observed_node("retrieve", retrieve_node))
    graph.add_node("translate", _observed_node("translate", translate_node))
    graph.add_node("edit", _observed_node("edit", edit_node))
    graph.add_node("polish", _observed_node("polish", polish_node))
    graph.add_node("qa", _observed_node("qa", qa_node))
    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve", "translate")
    graph.add_edge("translate", "edit")
    graph.add_edge("edit", "polish")
    graph.add_edge("polish", "qa")
    # TODO: 回退落点扩展点——如实验证明定点修订优于整段重译，改为回退 edit
    graph.add_conditional_edges(
        "qa", _route_after_qa, {"rework": "translate", "end": END}
    )
    return graph.compile()


def run_chapter(
    work_id: str,
    chapter_path: str | Path,
    llm: "LLMClient",
    memory: "MemoryHub | None",
    *,
    chapter_id: str | None = None,
    max_rework: int = DEFAULT_MAX_REWORK,
    observer: "RunObserver | None" = None,
    run_id: str | None = None,
    segmentation_config: Mapping[str, Any] | None = None,
    workflow_config: Mapping[str, Any] | None = None,
) -> TranslationState:
    """便捷入口（骨架）：读章节文件 → 调度切分 → 跑状态机 → 返回最终状态。

    流程：
        1. 读取章节原文（UTF-8），``chapter_id`` 取文件名主干；
        2. ``OrchestratorAgent.segment_chapter`` 机械切分并校验可逆性；
        3. ``init_state`` 写入完整原文、segments、元数据和统计；
        4. ``build_graph`` 编译状态机并 ``invoke``，返回最终
           ``TranslationState``（含 ``polished`` / ``qa_score`` /
           ``qa_verdict`` / ``review_notes`` 等）。

    TODO:
    - 从配置读取 ``workflow.max_rework`` 等参数（当前为显式参数/默认值）；
    - 终审通过后：``memory.update_story_bible`` 回写章节摘要、优质句对
      回写 TM（docs/architecture.md §3.2 时序）；
    - 成品译文落盘（输出目录配置化）、运行日志与 token 成本统计；
    - 进度回调、断点续跑与失败重试策略。
    """
    path = Path(chapter_path)
    resolved_chapter_id = chapter_id or path.stem
    workflow_options = dict(workflow_config or {})
    min_polished_ratio = float(
        workflow_options.get("min_polished_segment_ratio", 0.5)
    )
    max_polished_ratio = float(
        workflow_options.get("max_polished_segment_ratio", 2.5)
    )
    started = time.perf_counter()
    with run_context(
        observer,
        run_id=run_id,
        work_id=work_id,
        chapter_id=resolved_chapter_id,
    ) as active_run_id:
        emit_event(
            "run.started",
            payload={"chapter_path": str(path), "max_rework": max_rework},
        )
        try:
            text = path.read_text(encoding="utf-8")
            # 在线翻译只做安全规范化；M1 的去重/广告清洗可能误删小说正文。
            orchestrator = OrchestratorAgent(
                llm, memory, segmentation_config=segmentation_config
            )
            split_started = time.perf_counter()
            with event_scope(agent="orchestrator", node="dispatch", segment_id="chapter"):
                emit_event("agent.started", payload={"input_chars": len(text)})
                try:
                    segmentation = orchestrator.segment_chapter(
                        text, chapter_id=resolved_chapter_id
                    )
                    segments = segmentation.texts
                    stats = segmentation.statistics.to_dict()
                    if not segments:
                        raise ValueError("章节原文为空或仅包含空白，无法开始翻译")
                except Exception as exc:
                    emit_event(
                        "agent.failed",
                        payload={"error": type(exc).__name__},
                        metrics={
                            "duration_ms": round(
                                (time.perf_counter() - split_started) * 1000, 2
                            )
                        },
                    )
                    raise
                emit_event(
                    "agent.completed",
                    payload={"ok": True, "segments": len(segments)},
                    metrics={
                        "duration_ms": round(
                            (time.perf_counter() - split_started) * 1000, 2
                        )
                    },
                )
                emit_event(
                    "segmentation.completed",
                    payload={
                        "source_hash": segmentation.source_hash,
                        "config": segmentation.config.to_dict(),
                        **stats,
                    },
                )
                for meta in segmentation.metadata:
                    if meta.get("boundary_after") == "hard":
                        emit_event(
                            "segmentation.hard_split",
                            payload={
                                "segment_id": meta.get("segment_id"),
                                "source_end": meta.get("source_end"),
                            },
                        )
            app = build_graph(
                llm,
                memory,
                max_rework=max_rework,
                min_polished_segment_ratio=min_polished_ratio,
                max_polished_segment_ratio=max_polished_ratio,
            )
            initial = init_state(
                work_id=work_id,
                chapter_id=resolved_chapter_id,
                segments=segments,
                max_rework=max_rework,
                source_text=segmentation.normalized_text,
                segment_meta=segmentation.metadata,
                segmentation_stats=stats,
            )
            final: TranslationState = app.invoke(initial)
        except Exception as exc:
            emit_event(
                "run.failed",
                payload={"error": type(exc).__name__, "run_id": active_run_id},
                metrics={
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2)
                },
            )
            raise
        emit_event(
            "run.completed",
            payload={
                "run_id": active_run_id,
                "segments": len(final.get("segments") or []),
                "qa_score": final.get("qa_score", 0.0),
                "qa_verdict": final.get("qa_verdict", ""),
                "rework_count": final.get("rework_count", 0),
            },
            metrics={
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)
            },
        )
        return final
