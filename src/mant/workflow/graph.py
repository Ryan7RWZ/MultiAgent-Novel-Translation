"""LangGraph 状态机：retrieve → translate → edit → polish → qa 主链路。

回环（QA 返工）：``qa_verdict == "rework"`` 且 ``rework_count < max_rework``
时，经条件边携带 ``review_notes`` 批注回退 translate 节点重译；否则进入 END。
达到上限仍判返工时强制放行并打 ``needs_human_review`` 标记
（docs/architecture.md §3.3）。

依赖约定
--------
- ``langgraph`` 为第三方依赖，**延迟导入**（仅在 ``build_graph`` 内 import）：
  未安装时 ``import mant.workflow.graph`` 不报错，``build_graph`` 抛出
  带安装提示的 ``ImportError``；
- 四个业务 Agent 已就位，output 键契约（docs/agent-design.md §2 + 各模块实现）：
    - ``TranslatorAgent``：``{"draft": str}``；context 键 ``glossary`` /
      ``story_bible`` / ``tm_matches`` / ``prev_summary``；
    - ``EditorAgent``：``{"review_notes": list[dict]}``（只提意见不改稿）；
      context 键 ``draft``（必需）/ ``glossary``；
    - ``PolisherAgent``：``{"polished": str}``；context 键 ``draft``（必需）/
      ``review_notes``；
    - ``QAAgent``：``{"qa_score": float, "qa_verdict": "pass"|"rework",
      "qa_detail": dict}``（``qa_detail.suggestions`` 为返工建议，并入
      ``review_notes`` 驱动下一轮重译）；
- 各 Agent 的模型档位按其 ``tier`` 类属性经 ``LLMClient.with_tier`` 选档。

TODO（待团队同步）：
- 回退落点当前固定为 translate（docs §3.3 预留改回 edit 的扩展点）；
- ``review_notes`` 按轮次标记/清理已解决批注，避免历史批注无限累积。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


def build_graph(
    llm: "LLMClient",
    memory: "MemoryHub | None",
    max_rework: int = DEFAULT_MAX_REWORK,
) -> Any:
    """构建并编译单章翻译状态机。

    流程：retrieve（检索记忆注入）→ translate → edit → polish → qa；
    条件边：QA 判 ``rework`` 且未达上限 → 回 translate；否则 → END。

    参数:
        llm: 统一 LLM 客户端（``mant.llm.client.LLMClient``）。
        memory: 记忆层门面（``mant.memory.MemoryHub``）；可为 None，
            离线模式跳过检索注入，全链路仍可跑通。
        max_rework: 返工上限兜底值（优先以 ``state["max_rework"]`` 为准）。

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
                    source_text="\n\n".join(state["segments"]),
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
                source_text="\n\n".join(state["segments"]),
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
        parts: list[str] = []
        for idx, seg in enumerate(state["segments"]):
            task = AgentTask(
                work_id=state["work_id"],
                chapter_id=state["chapter_id"],
                segment_id=f"{state['chapter_id']}#seg{idx:04d}",
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
                    "review_notes": notes,
                    "round": rework_count,
                },
            )
            result = agents["translator"].execute(task)
            runtime_notes.extend(result.notes)
            # output 键契约 {"draft": str}；失败/空稿时保留原文兜底，不中断链路
            draft = str(result.output.get("draft") or "")
            parts.append(draft if draft.strip() else seg)
        return {
            "draft": "\n\n".join(parts),
            "rework_count": rework_count,
            "runtime_notes": runtime_notes,
        }

    def edit_node(state: TranslationState) -> dict:
        """审校：对照原文检查初稿，结构化意见并入 review_notes（不改稿）。"""
        task = AgentTask(
            work_id=state["work_id"],
            chapter_id=state["chapter_id"],
            segment_id="chapter",
            source_text="\n\n".join(state["segments"]),  # 原文
            context={
                "draft": state.get("draft", ""),  # EditorAgent 必需键
                "glossary": state.get("glossary") or {},
            },
        )
        result = agents["editor"].execute(task)
        runtime_notes = list(state.get("runtime_notes") or []) + result.notes
        # output 键契约 {"review_notes": list[dict]}；TODO: 按轮次清理已解决批注
        merged = list(state.get("review_notes") or []) + list(
            result.output.get("review_notes") or []
        )
        return {"review_notes": merged, "runtime_notes": runtime_notes}

    def polish_node(state: TranslationState) -> dict:
        """润色：在审校后的 draft 上做语言润色（不改事实与术语）。"""
        draft = state.get("draft", "")
        task = AgentTask(
            work_id=state["work_id"],
            chapter_id=state["chapter_id"],
            segment_id="chapter",
            source_text="\n\n".join(state["segments"]),  # 原文（供对照）
            context={
                "draft": draft,  # PolisherAgent 必需键
                "review_notes": state.get("review_notes") or [],
            },
        )
        result = agents["polisher"].execute(task)
        runtime_notes = list(state.get("runtime_notes") or []) + result.notes
        polished = str(result.output.get("polished") or "")
        # 润色失败/空稿时以 draft 兜底，保证 QA 有稿可审
        return {
            "polished": polished if polished.strip() else draft,
            "runtime_notes": runtime_notes,
        }

    def qa_node(state: TranslationState) -> dict:
        """QA 终审：评分 + pass/rework 裁决；返工建议并入 review_notes。"""
        polished = state.get("polished") or state.get("draft", "")
        task = AgentTask(
            work_id=state["work_id"],
            chapter_id=state["chapter_id"],
            segment_id="chapter",
            source_text="\n\n".join(state["segments"]),  # 原文（QAAgent 约定）
            context={
                "polished": polished,  # 优先终审润色稿，缺省回退 draft
                "glossary": state.get("glossary") or {},
                "review_notes": state.get("review_notes") or [],
            },
        )
        result = agents["qa"].execute(task)
        runtime_notes = list(state.get("runtime_notes") or []) + result.notes
        out = result.output
        try:
            score = float(out.get("qa_score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        verdict = str(out.get("qa_verdict", "rework")).strip().lower() or "rework"
        # QA 的可执行返工建议（qa_detail.suggestions）并入批注，驱动下一轮重译
        detail = out.get("qa_detail")
        suggestions = detail.get("suggestions") if isinstance(detail, dict) else None
        merged = list(state.get("review_notes") or []) + [
            str(s) for s in (suggestions or [])
        ]
        # 上限兜底：判返但已达上限 → 强制放行并打人工复核标记（docs §3.3）
        if verdict in _REWORK_VERDICTS and int(
            state.get("rework_count", 0)
        ) >= _resolve_limit(state, max_rework):
            merged.append("needs_human_review：已达返工上限仍判 rework，强制放行当前稿。")
        return {
            "qa_score": score,
            "qa_verdict": verdict,
            "review_notes": merged,
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
) -> TranslationState:
    """便捷入口（骨架）：读章节文件 → 调度切分 → 跑状态机 → 返回最终状态。

    流程：
        1. 读取章节原文（UTF-8），``chapter_id`` 取文件名主干；
        2. ``OrchestratorAgent.split_chapter`` 切分为 segments；
        3. ``init_state`` 构造初始状态（写入 ``max_rework``）；
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
            # 翻译入口复用 M1 的确定性清洗，避免广告/重复行污染 Prompt。
            from mant.pipeline.clean import clean_text

            text = clean_text(text)
            orchestrator = OrchestratorAgent(llm, memory)
            split_started = time.perf_counter()
            with event_scope(agent="orchestrator", node="dispatch", segment_id="chapter"):
                emit_event("agent.started", payload={"input_chars": len(text)})
                segments = orchestrator.split_chapter(text)
                emit_event(
                    "agent.completed",
                    payload={"ok": True, "segments": len(segments)},
                    metrics={
                        "duration_ms": round(
                            (time.perf_counter() - split_started) * 1000, 2
                        )
                    },
                )
            app = build_graph(llm, memory, max_rework=max_rework)
            initial = init_state(
                work_id=work_id,
                chapter_id=resolved_chapter_id,
                segments=segments,
                max_rework=max_rework,
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
