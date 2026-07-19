"""LangGraph 状态机：retrieve → translate/edit/revise/polish/qa → 归并主链路。

回环（QA 返工）：``qa_verdict == "rework"`` 且 ``rework_count < max_rework``
时，经条件边携带 ``review_notes`` 批注回退 translate 节点重译；否则进入 END。
达到上限仍判返工时强制放行并打 ``needs_human_review`` 标记
（docs/architecture.md §3.3）。

依赖约定
--------
- ``langgraph`` 为第三方依赖，**延迟导入**（仅在 ``build_graph`` 内 import）：
  未安装时 ``import mant.workflow.graph`` 不报错，``build_graph`` 抛出
  带安装提示的 ``ImportError``；
- 五个业务 Agent 按同一 segment 序列执行，output 键契约如下：
    - ``TranslatorAgent``：``{"draft": str}``；context 键 ``glossary`` /
      ``story_bible`` / ``tm_matches`` / ``prev_summary``；
    - ``EditorAgent``：``{"review_notes": list[dict]}``（只提意见不改稿）；
      context 键 ``draft``（必需）/ ``glossary``；
    - ``TranslatorAgent`` 的 revision mode：``{"draft": str,
      "revision_status": str}``；只按 Editor 的事实性意见引用程序生成 unit ID
      定点修订，不承担文学润色；
    - ``PolisherAgent``：``{"polished": str}``；context 键 ``draft``（必需）/
      语言层 ``review_notes``；
    - ``QAAgent``：``{"qa_score": float, "qa_verdict": "pass"|"rework",
      "qa_detail": dict}``（``qa_detail.suggestions`` 为返工建议，并入
      ``review_notes`` 驱动下一轮重译）；章级分数由工作流按源片 token 加权；
- 各 Agent 的模型档位按其 ``tier`` 类属性经 ``LLMClient.with_tier`` 选档。

TODO（待团队同步）：
- 回退落点当前固定为 translate（docs §3.3 预留改回 edit 的扩展点）；
- Terminologist 已按正文片段抽取并确定性归并；跨章全局别名仲裁仍待完善。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from mant.agents.base import AgentResult, AgentTask, BaseAgent
from mant.agents.editor import EditorAgent
from mant.agents.orchestrator import OrchestratorAgent
from mant.agents.polisher import PolisherAgent
from mant.agents.qa import QAAgent
from mant.agents.terminologist import TerminologistAgent
from mant.agents.translator import TranslatorAgent
from mant.execution import ExecutionConfig, RunManifestStore, StageExecutor, StageTask
from mant.observability import emit_event, event_scope, run_context
from mant.textio import read_text_file
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
    "terminologist": TerminologistAgent,
    "translator": TranslatorAgent,
    "editor": EditorAgent,
    "polisher": PolisherAgent,
    "qa": QAAgent,
}

_STAGE_TO_ROLE = {
    "terminology": "terminologist",
    "translate": "translator",
    "edit": "editor",
    "revise": "translator",
    "polish": "polisher",
    "qa": "qa",
}


def _pick_client(
    llm: "LLMClient",
    agent_cls: type[BaseAgent],
    tier_override: str | None = None,
) -> "LLMClient":
    """按 Agent 声明的 ``tier`` 类属性选档（docs/agent-design.md §4）。"""
    tier = tier_override or getattr(agent_cls, "tier", None)
    if tier and hasattr(llm, "with_tier"):
        return llm.with_tier(tier)
    return llm


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


_RESOLVED_NOTE_STATES = {"translation_applied", "revision_applied", "resolved"}
_REVISION_QA_PENDING_STATES = {
    "revision_applied_pending_qa",
    "revision_no_change_pending_qa",
}
_FACTUAL_ISSUE_TYPES = {
    "omission",
    "mistranslation",
    "proper_noun",
    "accuracy",
    "terminology",
    "qa",
}


def _pending_notes_for_segment(
    notes: list[Any], segment_id: str, segment_index: int
) -> list[Any]:
    """返回当前片尚未落实的批注，避免旧意见在返工轮无限重复注入。"""
    return [
        note
        for note in _notes_for_segment(notes, segment_id, segment_index)
        if not isinstance(note, dict)
        or str(note.get("resolution") or "pending") not in _RESOLVED_NOTE_STATES
    ]


def _needs_factual_revision(note: Any) -> bool:
    """事实、专名或 high 问题必须经过定点修订；语言小问题留给 Polisher。"""
    if not isinstance(note, dict):
        return False
    issue_type = str(note.get("issue_type") or "").strip().lower()
    severity = str(note.get("severity") or "").strip().lower()
    return issue_type in _FACTUAL_ISSUE_TYPES or severity == "high"


def _language_notes_for_segment(
    notes: list[Any], segment_id: str, segment_index: int
) -> list[dict[str, Any]]:
    """只把非 high 的语言类意见交给 Polisher，阻止其承担补译职责。"""
    return [
        note
        for note in _pending_notes_for_segment(notes, segment_id, segment_index)
        if isinstance(note, dict)
        and str(note.get("issue_type") or "").strip().lower() == "other"
        and str(note.get("severity") or "").strip().lower() != "high"
    ]


def _segment_failure(
    *,
    stage: str,
    segment_id: str,
    segment_index: int,
    reason: str,
    kind: str = "technical",
) -> dict[str, Any]:
    return {
        "stage": stage,
        "segment_id": segment_id,
        "segment_index": segment_index,
        "kind": kind,
        "reason": reason,
    }


def _stable_hash(value: Any) -> str:
    """为 checkpoint 生成稳定输入指纹；不把正文写入事件或键名。"""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensure_review_note_id(
    note: dict[str, Any],
    *,
    segment_id: str,
    segment_index: int,
    source: str,
    round_no: int,
    ordinal: int,
) -> str:
    """为 Editor/QA/旧 manifest 批注生成跨阶段稳定、无正文暴露的 ID。"""
    existing = str(note.get("note_id") or "").strip()
    if existing:
        return existing
    digest = _stable_hash(
        {
            "segment_id": segment_id,
            "segment_index": segment_index,
            "source": source,
            "round": round_no,
            "ordinal": ordinal,
            "issue_type": note.get("issue_type"),
            "severity": note.get("severity"),
            "span": note.get("span"),
            "suggestion": note.get("suggestion"),
        }
    )[:12]
    note_id = f"note-{digest}"
    note["note_id"] = note_id
    return note_id


def _selected_indices(state: TranslationState) -> list[int]:
    """首轮处理全部片段；返工轮只处理 QA/完整性标记的片段。"""
    all_indices = list(range(len(state.get("segments") or [])))
    verdict = str(state.get("qa_verdict", "")).strip().lower()
    if verdict not in _REWORK_VERDICTS:
        return all_indices
    selected = {
        int(index)
        for index in (state.get("rework_segment_indices") or [])
        if isinstance(index, int) or str(index).isdigit()
    }
    valid = sorted(index for index in selected if 0 <= index < len(all_indices))
    return valid or all_indices


def _prepare_resume_state(
    state: Mapping[str, Any],
    *,
    run_id: str,
    start_stage: str,
) -> TranslationState:
    """清理目标阶段的旧派生产物，同时保留可复用的上游状态。"""
    if start_stage != "qa":
        raise ValueError("当前定向恢复只支持 qa 阶段")
    prepared = dict(state)
    current_round = int(prepared.get("rework_count", 0) or 0)
    prepared["run_id"] = run_id
    prepared["qa_score"] = 0.0
    prepared["qa_verdict"] = ""
    prepared["rework_segment_indices"] = []
    prepared["execution_stats"] = {}
    prepared["segment_failures"] = [
        dict(item)
        for item in (prepared.get("segment_failures") or [])
        if isinstance(item, dict) and item.get("stage") != "qa"
    ]
    prepared["review_notes"] = [
        item
        for item in (prepared.get("review_notes") or [])
        if not (
            isinstance(item, dict)
            and item.get("issue_type") == "qa"
            and int(item.get("created_round", -1) or 0) == current_round
        )
    ]
    return prepared  # type: ignore[return-value]


def build_graph(
    llm: "LLMClient",
    memory: "MemoryHub | None",
    max_rework: int = DEFAULT_MAX_REWORK,
    *,
    min_polished_segment_ratio: float = 0.5,
    max_polished_segment_ratio: float = 2.5,
    execution_config: Mapping[str, Any] | None = None,
    agent_config: Mapping[str, Any] | None = None,
    start_stage: str = "retrieve",
    cancel_event: threading.Event | None = None,
) -> Any:
    """构建并编译单章翻译状态机。

    流程：retrieve（检索记忆注入）→ translate → edit → revise → polish → qa；
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

    execution = ExecutionConfig.from_mapping(execution_config)
    executor = StageExecutor(execution, cancel_event=cancel_event)
    agent_options = {
        str(role): dict(options)
        for role, options in dict(agent_config or {}).items()
        if isinstance(options, Mapping)
    }
    allowed_agent_options = {
        "tier",
        "temperature",
        "max_tokens",
        "max_terms",
        "structured_json",
        "thinking",
        "repair_attempts",
        "repair_max_tokens",
        "max_review_notes",
        "max_span_chars",
        "max_suggestion_chars",
        "compact_recovery_attempts",
        "compact_recovery_max_tokens",
        "compact_recovery_max_notes",
        "revision_max_operations",
        "revision_max_tokens",
        "revision_repair_attempts",
        "revision_repair_max_tokens",
        "pass_score_threshold",
        "min_dimension_score",
    }

    def options_for(role: str) -> dict[str, Any]:
        options = agent_options.get(role, {})
        unknown = sorted(set(options) - allowed_agent_options)
        if unknown:
            raise ValueError(f"Agent {role!r} 含未知配置：{', '.join(unknown)}")
        return options

    def client_for(role: str, agent_cls: type[BaseAgent]) -> "LLMClient":
        tier = str(options_for(role).get("tier") or "") or None
        return _pick_client(llm, agent_cls, tier)

    def configure_agent(agent: BaseAgent, role: str) -> BaseAgent:
        for key, value in options_for(role).items():
            if key in {"tier", "thinking"}:
                if key == "thinking" and str(value) not in {"enabled", "disabled"}:
                    raise ValueError("thinking 只能为 enabled 或 disabled")
                setattr(agent, key, str(value))
            elif key in {
                "max_tokens",
                "max_terms",
                "repair_attempts",
                "repair_max_tokens",
                "max_review_notes",
                "max_span_chars",
                "max_suggestion_chars",
                "compact_recovery_attempts",
                "compact_recovery_max_tokens",
                "compact_recovery_max_notes",
                "revision_max_operations",
                "revision_max_tokens",
                "revision_repair_attempts",
                "revision_repair_max_tokens",
            }:
                parsed = int(value)
                if key.endswith("tokens") and parsed < 1:
                    raise ValueError(f"Agent {role!r} 的 {key} 必须至少为 1")
                if key.endswith("attempts") and parsed < 0:
                    raise ValueError(f"Agent {role!r} 的 {key} 不能小于 0")
                if key not in {
                    "repair_attempts",
                    "compact_recovery_attempts",
                    "revision_repair_attempts",
                } and parsed < 1:
                    raise ValueError(f"Agent {role!r} 的 {key} 必须至少为 1")
                setattr(agent, key, parsed)
            elif key in {"temperature", "pass_score_threshold", "min_dimension_score"}:
                parsed_float = float(value)
                upper = 2 if key == "temperature" else 10
                if not 0 <= parsed_float <= upper:
                    raise ValueError(f"Agent {role!r} 的 {key} 必须位于 [0, {upper}] 区间")
                setattr(agent, key, parsed_float)
            else:
                setattr(agent, key, bool(value))
        return agent

    def new_agent(role: str) -> BaseAgent:
        """每个任务创建独立 Agent/LLMClient，避免并发污染 last_* 状态。"""
        agent_cls = _AGENT_CLASSES[role]
        return configure_agent(agent_cls(client_for(role, agent_cls), None), role)

    def client_semantics(client: Any) -> dict[str, Any]:
        describe = getattr(client, "semantic_config", None)
        if callable(describe):
            return dict(describe())
        return {
            "tier": str(getattr(client, "tier", "") or ""),
            "model": str(getattr(client, "model", "") or ""),
        }

    def agent_semantics(role: str, agent_cls: type[BaseAgent]) -> dict[str, Any]:
        defaults = {
            key: getattr(agent_cls, key)
            for key in allowed_agent_options
            if key != "tier" and hasattr(agent_cls, key)
        }
        defaults.update(options_for(role))
        return {
            "class": f"{agent_cls.__module__}.{agent_cls.__name__}",
            "tier": str(
                options_for(role).get("tier")
                or getattr(agent_cls, "tier", "")
            ),
            "runtime": defaults,
            "prompts": {
                name: str(getattr(agent_cls, name, ""))
                for name in (
                    "SYSTEM_PROMPT",
                    "USER_PROMPT_TEMPLATE",
                    "JSON_REPAIR_SYSTEM_PROMPT",
                    "COMPACT_RECOVERY_SYSTEM_PROMPT",
                    "REVISION_SYSTEM_PROMPT",
                    "REVISION_USER_PROMPT_TEMPLATE",
                    "REVISION_REPAIR_SYSTEM_PROMPT",
                )
                if hasattr(agent_cls, name)
            },
        }

    def stage_task(
        state: TranslationState,
        *,
        stage: str,
        index: int,
        round_no: int,
        task: AgentTask,
    ) -> StageTask:
        role = _STAGE_TO_ROLE[stage]
        agent_cls = _AGENT_CLASSES[role]
        selected_client = client_for(role, agent_cls)
        fingerprint_payload: dict[str, Any] = {
            "fingerprint_version": 4,
            "stage": stage,
            "round": round_no,
            "agent": agent_semantics(role, agent_cls),
            "provider": client_semantics(selected_client),
            "task": {
                "work_id": task.work_id,
                "chapter_id": task.chapter_id,
                "segment_id": task.segment_id,
                "source_text": task.source_text,
                "context": task.context,
            },
        }
        # v4 旧字符串锚点仍保留在通用 Agent 语义里，确保 Translate/Edit
        # checkpoint 不因 revision 协议单独升级而失效。v5 仅进入 revise 键。
        if stage == "revise":
            fingerprint_payload["stage_protocol"] = {
                "name": "revision_unit_patch",
                "version": 5,
                "splitter": "sentence_or_newline_preserve_gaps_v1",
                "prompts": {
                    "system": TranslatorAgent.REVISION_UNIT_SYSTEM_PROMPT,
                    "user": TranslatorAgent.REVISION_UNIT_USER_PROMPT_TEMPLATE,
                    "repair": TranslatorAgent.REVISION_UNIT_REPAIR_SYSTEM_PROMPT,
                },
                "validation": {
                    "replacement_ratio": [0.2, 5.0],
                    "one_operation_per_unit": True,
                    "all_notes_must_be_covered": True,
                },
            }
        fingerprint = _stable_hash(fingerprint_payload)
        return StageTask(
            run_id=str(state.get("run_id") or "unscoped"),
            segment_id=task.segment_id,
            segment_index=index,
            stage=stage,
            round=round_no,
            input_hash=fingerprint,
        )

    min_polished_segment_ratio = float(min_polished_segment_ratio)
    max_polished_segment_ratio = float(max_polished_segment_ratio)
    if not 0 < min_polished_segment_ratio <= 1:
        raise ValueError("min_polished_segment_ratio 必须位于 (0, 1] 区间")
    if max_polished_segment_ratio < 1:
        raise ValueError("max_polished_segment_ratio 必须至少为 1")
    if max_polished_segment_ratio < min_polished_segment_ratio:
        raise ValueError("润色稿最大长度比例不能小于最小比例")
    if start_stage not in {
        "retrieve",
        "translate",
        "edit",
        "revise",
        "polish",
        "qa",
    }:
        raise ValueError(f"不支持的工作流起始阶段：{start_stage!r}")

    # ------------------------------------------------------------------
    # 节点定义（检索产物全部随 TranslationState 流转；run_chapter 每次运行独立构图）
    # ------------------------------------------------------------------
    def retrieve_node(state: TranslationState) -> dict:
        """检索记忆注入：圣经 / TM / 运行说明与术语表均写入 state。"""
        runtime_notes: list[str] = []
        glossary: dict[str, Any] = dict(state.get("glossary") or {})

        def extract_terminology(story_bible: Any) -> AgentResult:
            agent_tasks: dict[int, AgentTask] = {}
            stage_tasks: list[StageTask] = []
            for idx, source in enumerate(state["segments"]):
                agent_task = AgentTask(
                    work_id=state["work_id"],
                    chapter_id=state["chapter_id"],
                    segment_id=_segment_id(state, idx),
                    source_text=source,
                    context={
                        "mode": "extract",
                        "story_bible": story_bible,
                    },
                )
                agent_tasks[idx] = agent_task
                stage_tasks.append(
                    stage_task(
                        state,
                        stage="terminology",
                        index=idx,
                        round_no=0,
                        task=agent_task,
                    )
                )
            results = executor.run(
                "terminology",
                stage_tasks,
                lambda scheduled: new_agent("terminologist").execute(
                    agent_tasks[scheduled.segment_index]
                ),
            )
            raw_candidates: list[dict[str, Any]] = []
            successful_slices = 0
            for scheduled in results:
                if scheduled.value.ok:
                    successful_slices += 1
                raw_candidates.extend(
                    item
                    for item in (scheduled.value.output.get("new_terms") or [])
                    if isinstance(item, dict)
                )
                runtime_notes.extend(
                    note
                    for note in scheduled.value.notes
                    if "memory 为 None" not in note
                )
            reconciler = configure_agent(
                TerminologistAgent(
                    client_for("terminologist", TerminologistAgent), memory
                ),
                "terminologist",
            )
            reconciled = reconciler.reconcile_candidates(
                AgentTask(
                    work_id=state["work_id"],
                    chapter_id=state["chapter_id"],
                    segment_id="chapter",
                    source_text=_source_text(state),
                    context={"mode": "extract", "story_bible": story_bible},
                ),
                raw_candidates,
                extraction_valid=successful_slices == len(stage_tasks),
            )
            runtime_notes.append(
                "术语分片抽取："
                f"{successful_slices}/{len(stage_tasks)} 片有效，"
                f"归并前候选 {len(raw_candidates)} 条。"
            )
            return reconciled

        if memory is None:
            runtime_notes.append("memory 为 None，跳过记忆检索注入（离线模式）")
            term_result = extract_terminology(None)
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

        # ③ 术语注入：按正文机械片段并发抽取，再确定性去重和一次性入库。
        term_result = extract_terminology(story_bible)
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
        notes = [
            dict(note) if isinstance(note, dict) else note
            for note in (state.get("review_notes") or [])
        ]
        rework_count = int(state.get("rework_count", 0))
        is_rework = (
            str(state.get("qa_verdict", "")).strip().lower()
            in _REWORK_VERDICTS
        )
        if is_rework:
            rework_count += 1  # 本轮由 QA 判返触发，记一次实际返工
        tm_matches = state.get("tm_matches") or []
        runtime_notes = list(state.get("runtime_notes") or [])
        segment_meta = state.get("segment_meta") or []
        previous_parts = state.get("draft_segments") or []
        parts = [
            previous_parts[idx] if idx < len(previous_parts) else ""
            for idx in range(len(state["segments"]))
        ]
        failures: list[dict[str, Any]] = []
        agent_tasks: dict[int, AgentTask] = {}
        task_notes: dict[int, list[Any]] = {}
        stage_tasks: list[StageTask] = []
        for idx in _selected_indices(state):
            seg = state["segments"][idx]
            meta = segment_meta[idx] if idx < len(segment_meta) else {}
            segment_id = str(
                meta.get("segment_id") or f"{state['chapter_id']}#seg{idx:04d}"
            )
            pending_notes = _pending_notes_for_segment(notes, segment_id, idx)
            task_notes[idx] = pending_notes
            agent_task = AgentTask(
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
                    "review_notes": pending_notes,
                    "round": rework_count,
                    # 仅作理解辅助，译者 Prompt 明确要求不得翻译或输出。
                    "context_before": str(meta.get("context_before") or ""),
                    "context_after": str(meta.get("context_after") or ""),
                },
            )
            agent_tasks[idx] = agent_task
            stage_tasks.append(
                stage_task(
                    state,
                    stage="translate",
                    index=idx,
                    round_no=rework_count,
                    task=agent_task,
                )
            )

        results = executor.run(
            "translate",
            stage_tasks,
            lambda scheduled: new_agent("translator").execute(
                agent_tasks[scheduled.segment_index]
            ),
        )
        for scheduled in results:
            idx = scheduled.task.segment_index
            seg = state["segments"][idx]
            segment_id = scheduled.task.segment_id
            result = scheduled.value
            runtime_notes.extend(result.notes)
            draft = str(result.output.get("draft") or "")
            if result.ok and draft.strip():
                parts[idx] = draft
                if is_rework:
                    for note in task_notes.get(idx, []):
                        if isinstance(note, dict):
                            note["resolution"] = "translation_applied"
                            note["resolved_round"] = rework_count
                            note["status"] = "addressed"
            else:
                fallback = previous_parts[idx] if idx < len(previous_parts) else seg
                parts[idx] = fallback
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
                    "from_checkpoint": scheduled.from_checkpoint,
                },
            )
        return {
            "draft": "\n\n".join(parts),
            "draft_segments": parts,
            "review_notes": notes,
            "segment_failures": failures,
            "rework_count": rework_count,
            "runtime_notes": runtime_notes,
            "execution_stats": executor.stats(),
        }

    def edit_node(state: TranslationState) -> dict:
        """逐 segment 对照审校；所有意见带 segment 定位后确定性归并。"""
        draft_parts = state.get("draft_segments") or []
        runtime_notes = list(state.get("runtime_notes") or [])
        merged = list(state.get("review_notes") or [])
        failures = list(state.get("segment_failures") or [])
        round_no = int(state.get("rework_count", 0))
        agent_tasks: dict[int, AgentTask] = {}
        stage_tasks: list[StageTask] = []
        for idx in _selected_indices(state):
            source = state["segments"][idx]
            segment_id = _segment_id(state, idx)
            draft = draft_parts[idx] if idx < len(draft_parts) else ""
            agent_task = AgentTask(
                work_id=state["work_id"],
                chapter_id=state["chapter_id"],
                segment_id=segment_id,
                source_text=source,
                context={
                    "draft": draft,
                    "glossary": state.get("glossary") or {},
                    "round": round_no,
                },
            )
            agent_tasks[idx] = agent_task
            stage_tasks.append(
                stage_task(
                    state,
                    stage="edit",
                    index=idx,
                    round_no=round_no,
                    task=agent_task,
                )
            )

        results = executor.run(
            "edit",
            stage_tasks,
            lambda scheduled: new_agent("editor").execute(
                agent_tasks[scheduled.segment_index]
            ),
        )
        for scheduled in results:
            idx = scheduled.task.segment_index
            segment_id = scheduled.task.segment_id
            result = scheduled.value
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
            for ordinal, raw_note in enumerate(
                result.output.get("review_notes") or [], start=1
            ):
                note = dict(raw_note) if isinstance(raw_note, dict) else {
                    "suggestion": str(raw_note)
                }
                note["segment_id"] = segment_id
                note["segment_index"] = idx
                note["source"] = "editor"
                note["created_round"] = round_no
                note["status"] = "open"
                note["scope"] = "segment"
                note["resolution"] = "pending"
                _ensure_review_note_id(
                    note,
                    segment_id=segment_id,
                    segment_index=idx,
                    source="editor",
                    round_no=round_no,
                    ordinal=ordinal,
                )
                merged.append(note)
            emit_event(
                "stage.segment_completed",
                payload={
                    "stage": "edit",
                    "segment_index": idx,
                    "segment_count": len(state["segments"]),
                    "ok": result.ok,
                    "from_checkpoint": scheduled.from_checkpoint,
                },
            )
        return {
            "review_notes": merged,
            "segment_failures": failures,
            "runtime_notes": runtime_notes,
            "execution_stats": executor.stats(),
        }

    def revise_node(state: TranslationState) -> dict:
        """只对含事实性审校意见的片段定点修订；其余片段零调用沿用初稿。"""
        draft_parts = state.get("draft_segments") or []
        previous_parts = state.get("revised_segments") or []
        revised_parts = [
            previous_parts[idx]
            if idx < len(previous_parts)
            else (draft_parts[idx] if idx < len(draft_parts) else "")
            for idx in range(len(state["segments"]))
        ]
        merged = [
            dict(note) if isinstance(note, dict) else note
            for note in (state.get("review_notes") or [])
        ]
        runtime_notes = list(state.get("runtime_notes") or [])
        failures = list(state.get("segment_failures") or [])
        round_no = int(state.get("rework_count", 0))
        agent_tasks: dict[int, AgentTask] = {}
        task_notes: dict[int, list[dict[str, Any]]] = {}
        stage_tasks: list[StageTask] = []

        for idx in _selected_indices(state):
            source = state["segments"][idx]
            segment_id = _segment_id(state, idx)
            draft = draft_parts[idx] if idx < len(draft_parts) else ""
            pending = [
                note
                for note in _pending_notes_for_segment(merged, segment_id, idx)
                if _needs_factual_revision(note)
            ]
            if not pending:
                revised_parts[idx] = draft
                continue
            for ordinal, note in enumerate(pending, start=1):
                _ensure_review_note_id(
                    note,
                    segment_id=segment_id,
                    segment_index=idx,
                    source=str(note.get("source") or "legacy"),
                    round_no=int(note.get("created_round", round_no) or 0),
                    ordinal=ordinal,
                )
            meta = _segment_meta(state, idx)
            task_notes[idx] = pending
            agent_task = AgentTask(
                work_id=state["work_id"],
                chapter_id=state["chapter_id"],
                segment_id=segment_id,
                source_text=source,
                context={
                    "mode": "revision",
                    "draft": draft,
                    "review_notes": pending,
                    "glossary": state.get("glossary") or {},
                    "round": round_no,
                    "context_before": str(meta.get("context_before") or ""),
                    "context_after": str(meta.get("context_after") or ""),
                },
            )
            agent_tasks[idx] = agent_task
            stage_tasks.append(
                stage_task(
                    state,
                    stage="revise",
                    index=idx,
                    round_no=round_no,
                    task=agent_task,
                )
            )

        results = executor.run(
            "revise",
            stage_tasks,
            lambda scheduled: new_agent("translator").execute(
                agent_tasks[scheduled.segment_index]
            ),
        )
        for scheduled in results:
            idx = scheduled.task.segment_index
            segment_id = scheduled.task.segment_id
            result = scheduled.value
            runtime_notes.extend(result.notes)
            candidate = str(result.output.get("draft") or "")
            revision_status = str(
                result.output.get("revision_status") or "provider_error"
            ).strip().lower()
            original_draft = (
                draft_parts[idx] if idx < len(draft_parts) else ""
            )
            ratio = len(candidate.strip()) / max(1, len(original_draft.strip()))
            draft_mode = candidate.lstrip().startswith("[DRAFT]")
            output_complete = (
                result.ok
                and bool(candidate.strip())
                and (draft_mode or ratio >= min_polished_segment_ratio)
            )
            if output_complete and revision_status in {"applied", "draft_fallback"}:
                revised_parts[idx] = candidate
                for note in task_notes.get(idx, []):
                    note["revision_resolution"] = "applied"
                    note["resolution"] = "revision_applied_pending_qa"
                    note["revised_round"] = round_no
                    note["status"] = "verification_pending"
            elif output_complete and revision_status == "no_change":
                revised_parts[idx] = original_draft
                for note in task_notes.get(idx, []):
                    note["revision_resolution"] = "no_change"
                    note["resolution"] = "revision_no_change_pending_qa"
                    note["revised_round"] = round_no
                    note["status"] = "verification_pending"
            elif result.ok and revision_status == "protocol_rejected":
                revised_parts[idx] = original_draft
                for note in task_notes.get(idx, []):
                    note["revision_resolution"] = "protocol_rejected"
                    note["resolution"] = "protocol_rejected"
                    note["status"] = "open"
                failures.append(
                    _segment_failure(
                        stage="revise",
                        segment_id=segment_id,
                        segment_index=idx,
                        kind="semantic",
                        reason=(
                            "unit-ID 修订补丁未通过确定性校验，已保留修订前初稿："
                            + str(result.output.get("revision_error") or "未知协议错误")
                        ),
                    )
                )
                emit_event(
                    "revision.protocol_rejected",
                    payload={
                        "stage": "revise",
                        "segment_index": idx,
                        "safe_fallback": True,
                    },
                )
            else:
                revised_parts[idx] = original_draft
                failures.append(
                    _segment_failure(
                        stage="revise",
                        segment_id=segment_id,
                        segment_index=idx,
                        reason=(
                            "定点修订调用失败、返回空结果或异常缩短，"
                            "已保留修订前初稿"
                        ),
                    )
                )
                emit_event(
                    "output.integrity_failed",
                    payload={
                        "stage": "revise",
                        "segment_index": idx,
                        "draft_chars": len(original_draft),
                        "output_chars": len(candidate),
                        "ratio": round(ratio, 4),
                    },
                )
            emit_event(
                "stage.segment_completed",
                payload={
                    "stage": "revise",
                    "segment_index": idx,
                    "segment_count": len(state["segments"]),
                    "ok": result.ok,
                    "revision_status": revision_status,
                    "output_complete": output_complete,
                    "from_checkpoint": scheduled.from_checkpoint,
                },
            )
        return {
            "revised_segments": revised_parts,
            "revised": "\n\n".join(revised_parts),
            "review_notes": merged,
            "segment_failures": failures,
            "runtime_notes": runtime_notes,
            "execution_stats": executor.stats(),
        }

    def polish_node(state: TranslationState) -> dict:
        """逐 segment 润色并做长度完整性检查；异常片回退对应初稿。"""
        draft_parts = (
            state.get("revised_segments") or state.get("draft_segments") or []
        )
        runtime_notes = list(state.get("runtime_notes") or [])
        failures = list(state.get("segment_failures") or [])
        previous_polished = state.get("polished_segments") or []
        polished_parts = [
            previous_polished[idx] if idx < len(previous_polished) else ""
            for idx in range(len(state["segments"]))
        ]
        all_notes = list(state.get("review_notes") or [])
        round_no = int(state.get("rework_count", 0))
        agent_tasks: dict[int, AgentTask] = {}
        stage_tasks: list[StageTask] = []
        for idx in _selected_indices(state):
            source = state["segments"][idx]
            segment_id = _segment_id(state, idx)
            draft = draft_parts[idx] if idx < len(draft_parts) else ""
            agent_task = AgentTask(
                work_id=state["work_id"],
                chapter_id=state["chapter_id"],
                segment_id=segment_id,
                source_text=source,
                context={
                    "draft": draft,
                    "review_notes": _language_notes_for_segment(
                        all_notes, segment_id, idx
                    ),
                    "round": round_no,
                },
            )
            agent_tasks[idx] = agent_task
            stage_tasks.append(
                stage_task(
                    state,
                    stage="polish",
                    index=idx,
                    round_no=round_no,
                    task=agent_task,
                )
            )

        results = executor.run(
            "polish",
            stage_tasks,
            lambda scheduled: new_agent("polisher").execute(
                agent_tasks[scheduled.segment_index]
            ),
        )
        for scheduled in results:
            idx = scheduled.task.segment_index
            segment_id = scheduled.task.segment_id
            draft = draft_parts[idx] if idx < len(draft_parts) else ""
            result = scheduled.value
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
                polished_parts[idx] = candidate
            else:
                polished_parts[idx] = draft
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
                    "from_checkpoint": scheduled.from_checkpoint,
                },
            )
        return {
            "polished_segments": polished_parts,
            "polished": "\n\n".join(polished_parts),
            "segment_failures": failures,
            "runtime_notes": runtime_notes,
            "execution_stats": executor.stats(),
        }

    def qa_node(state: TranslationState) -> dict:
        """逐 segment QA；分数按 token 权重汇总，任一失败则章级 rework。"""
        polished_parts = state.get("polished_segments") or []
        draft_parts = (
            state.get("revised_segments") or state.get("draft_segments") or []
        )
        runtime_notes = list(state.get("runtime_notes") or [])
        merged = list(state.get("review_notes") or [])
        failures = list(state.get("segment_failures") or [])
        previous_qa = {
            int(item.get("segment_index")): dict(item)
            for item in (state.get("segment_qa") or [])
            if isinstance(item, dict) and isinstance(item.get("segment_index"), int)
        }
        round_no = int(state.get("rework_count", 0))
        active_indices = set(_selected_indices(state))
        active_indices.update(
            idx for idx in range(len(state["segments"])) if idx not in previous_qa
        )
        agent_tasks: dict[int, AgentTask] = {}
        stage_tasks: list[StageTask] = []
        for idx in sorted(active_indices):
            source = state["segments"][idx]
            segment_id = _segment_id(state, idx)
            polished = (
                polished_parts[idx]
                if idx < len(polished_parts)
                else (draft_parts[idx] if idx < len(draft_parts) else "")
            )
            agent_task = AgentTask(
                work_id=state["work_id"],
                chapter_id=state["chapter_id"],
                segment_id=segment_id,
                source_text=source,
                context={
                    "polished": polished,
                    "glossary": state.get("glossary") or {},
                    "review_notes": _notes_for_segment(merged, segment_id, idx),
                    "round": round_no,
                },
            )
            agent_tasks[idx] = agent_task
            stage_tasks.append(
                stage_task(
                    state,
                    stage="qa",
                    index=idx,
                    round_no=round_no,
                    task=agent_task,
                )
            )

        results = executor.run(
            "qa",
            stage_tasks,
            lambda scheduled: new_agent("qa").execute(
                agent_tasks[scheduled.segment_index]
            ),
        )
        for scheduled in results:
            idx = scheduled.task.segment_index
            segment_id = scheduled.task.segment_id
            result = scheduled.value
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
            verification_notes = [
                note
                for note in _notes_for_segment(merged, segment_id, idx)
                if isinstance(note, dict)
                and str(note.get("resolution") or "").strip().lower()
                in _REVISION_QA_PENDING_STATES
            ]
            for note in verification_notes:
                if result.ok and segment_verdict == "pass":
                    note["resolution"] = "resolved"
                    note["qa_resolution"] = "verified"
                    note["verified_round"] = round_no
                    note["status"] = "closed"
                elif result.ok:
                    note["resolution"] = "qa_rejected"
                    note["qa_resolution"] = "rejected"
                    note["verified_round"] = round_no
                    note["status"] = "open"
            previous_qa[idx] = {
                "segment_id": segment_id,
                "segment_index": idx,
                "qa_score": score,
                "qa_verdict": segment_verdict,
                "qa_detail": detail,
                "ok": result.ok,
                "error_type": (
                    scheduled.error_type
                    or ("AgentOutputInvalid" if not result.ok else "")
                ),
            }
            if not result.ok:
                failures.append(
                    _segment_failure(
                        stage="qa",
                        segment_id=segment_id,
                        segment_index=idx,
                        reason="QA 调用失败或输出无法解析",
                    )
                )
            for ordinal, suggestion in enumerate(
                detail.get("suggestions") or [], start=1
            ):
                note = {
                    "issue_type": "qa",
                    "severity": "high" if segment_verdict == "rework" else "low",
                    "span": segment_id,
                    "suggestion": str(suggestion),
                    "segment_id": segment_id,
                    "segment_index": idx,
                    "source": "qa",
                    "created_round": round_no,
                    "status": "open",
                    "scope": "segment",
                    "resolution": "pending",
                }
                _ensure_review_note_id(
                    note,
                    segment_id=segment_id,
                    segment_index=idx,
                    source="qa",
                    round_no=round_no,
                    ordinal=ordinal,
                )
                merged.append(note)
            emit_event(
                "stage.segment_completed",
                payload={
                    "stage": "qa",
                    "segment_index": idx,
                    "segment_count": len(state["segments"]),
                    "ok": result.ok,
                    "verdict": segment_verdict,
                    "from_checkpoint": scheduled.from_checkpoint,
                },
            )

        qa_segments = [
            previous_qa[idx]
            for idx in range(len(state["segments"]))
            if idx in previous_qa
        ]
        weighted_score = 0.0
        evaluated_weight = 0
        total_weight = 0
        evaluated_count = 0
        pass_count = 0
        failure_categories: dict[str, int] = {}
        all_pass = True
        for item in qa_segments:
            idx = int(item["segment_index"])
            source = state["segments"][idx]
            score = float(item.get("qa_score", 0.0) or 0.0)
            weight = max(
                1,
                int(
                    _segment_meta(state, idx).get("estimated_tokens")
                    or len(source)
                ),
            )
            total_weight += weight
            if item.get("ok"):
                weighted_score += score * weight
                evaluated_weight += weight
                evaluated_count += 1
                if item.get("qa_verdict") == "pass":
                    pass_count += 1
            else:
                category = str(item.get("error_type") or "AgentOutputInvalid")
                failure_categories[category] = failure_categories.get(category, 0) + 1
            if not item.get("ok") or item.get("qa_verdict") != "pass":
                all_pass = False

        score = (
            round(weighted_score / evaluated_weight, 2)
            if evaluated_weight
            else 0.0
        )
        qa_summary = {
            "segment_count": len(state["segments"]),
            "evaluated_count": evaluated_count,
            "pass_count": pass_count,
            "coverage": round(evaluated_count / max(1, len(state["segments"])), 4),
            "token_coverage": round(evaluated_weight / max(1, total_weight), 4),
            "pass_ratio": round(pass_count / max(1, evaluated_count), 4),
            "failure_categories": failure_categories,
        }
        verdict = "pass" if all_pass and not failures and qa_segments else "rework"
        rework_indices = {
            int(item["segment_index"])
            for item in qa_segments
            if not item.get("ok") or item.get("qa_verdict") != "pass"
        }
        rework_indices.update(
            int(item["segment_index"])
            for item in failures
            if isinstance(item.get("segment_index"), int)
        )
        emit_event(
            "qa.aggregated",
            payload={
                "segment_count": len(qa_segments),
                "pass_count": pass_count,
                "evaluated_count": evaluated_count,
                "coverage": qa_summary["coverage"],
                "pass_ratio": qa_summary["pass_ratio"],
                "failure_categories": failure_categories,
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
            "qa_summary": qa_summary,
            "review_notes": merged,
            "segment_failures": failures,
            "segment_qa": qa_segments,
            "rework_segment_indices": sorted(rework_indices),
            "runtime_notes": runtime_notes,
            "execution_stats": executor.stats(),
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
    graph.add_node("revise", _observed_node("revise", revise_node))
    graph.add_node("polish", _observed_node("polish", polish_node))
    graph.add_node("qa", _observed_node("qa", qa_node))
    graph.set_entry_point(start_stage)
    graph.add_edge("retrieve", "translate")
    graph.add_edge("translate", "edit")
    graph.add_edge("edit", "revise")
    graph.add_edge("revise", "polish")
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
    execution_config: Mapping[str, Any] | None = None,
    agent_config: Mapping[str, Any] | None = None,
    resume_state: Mapping[str, Any] | None = None,
    start_stage: str = "retrieve",
    cancel_event: threading.Event | None = None,
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
    - 成品译文落盘（输出目录配置化）、可靠 token 成本统计；
    - 在浏览器入口暴露主动取消和显式 resume 操作。
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
    execution = ExecutionConfig.from_mapping(execution_config)
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
            decoded_input = read_text_file(path)
            text = decoded_input.text
            emit_event(
                "input.decoded",
                payload={
                    "encoding": decoded_input.encoding,
                    "input_bytes": decoded_input.byte_length,
                    "had_bom": decoded_input.had_bom,
                    "converted_to": "utf-8",
                },
            )
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
                event_type = (
                    "resume.source_validated"
                    if resume_state is not None
                    else "segmentation.completed"
                )
                emit_event(
                    event_type,
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
                execution_config=execution_config,
                agent_config=agent_config,
                start_stage=start_stage,
                cancel_event=cancel_event,
            )
            if resume_state is None:
                initial = init_state(
                    work_id=work_id,
                    chapter_id=resolved_chapter_id,
                    segments=segments,
                    max_rework=max_rework,
                    source_text=segmentation.normalized_text,
                    segment_meta=segmentation.metadata,
                    segmentation_stats=stats,
                    run_id=active_run_id,
                    source_encoding=decoded_input.encoding,
                )
            else:
                if str(resume_state.get("work_id") or "") != work_id:
                    raise ValueError("manifest 的 work_id 与恢复请求不一致")
                if str(resume_state.get("chapter_id") or "") != resolved_chapter_id:
                    raise ValueError("manifest 的 chapter_id 与恢复请求不一致")
                if str(resume_state.get("source_text") or "") != segmentation.normalized_text:
                    raise ValueError("原文已变化，拒绝使用旧运行 manifest 恢复")
                initial = _prepare_resume_state(
                    resume_state,
                    run_id=active_run_id,
                    start_stage=start_stage,
                )
                emit_event(
                    "resume.started",
                    payload={
                        "stage": start_stage,
                        "failed_only": execution.resume_failed_only,
                    },
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
        if execution.manifest_enabled:
            try:
                describe = getattr(llm, "semantic_config", None)
                llm_settings = (
                    dict(describe(include_all_tiers=True))
                    if callable(describe)
                    else {}
                )
                manifest_path = RunManifestStore(execution.manifest_dir).save(
                    run_id=active_run_id,
                    chapter_path=path,
                    state=final,
                    settings={
                        "llm": llm_settings,
                        "agents": dict(agent_config or {}),
                        "workflow": workflow_options,
                        "segmentation": dict(segmentation_config or {}),
                        "execution": execution.to_dict(),
                    },
                )
                emit_event(
                    "manifest.saved",
                    payload={"path": str(manifest_path)},
                )
            except Exception as exc:  # noqa: BLE001 - manifest 不覆盖已完成译文
                final.setdefault("runtime_notes", []).append(
                    f"运行 manifest 保存失败：{exc!r}"
                )
                emit_event(
                    "manifest.failed",
                    payload={"error": type(exc).__name__},
                )
        emit_event(
            "run.completed",
            payload={
                "run_id": active_run_id,
                "segments": len(final.get("segments") or []),
                "qa_score": final.get("qa_score", 0.0),
                "qa_verdict": final.get("qa_verdict", ""),
                "qa_summary": final.get("qa_summary") or {},
                "rework_count": final.get("rework_count", 0),
                "execution": final.get("execution_stats") or {},
            },
            metrics={
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)
            },
        )
        return final
