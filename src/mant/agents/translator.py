"""译者 Agent（TranslatorAgent）：术语注入式初译。

骨架级别实现：定义中文角色 Prompt 模板与 ``run(task)`` 流程框架。
记忆注入材料（术语表 / 小说圣经 / 翻译记忆 / 前情提要）一律从
``task.context`` 读取（由调度层 / MemoryHub 提前注入），具体业务细节
以 TODO 标出，待后续迭代补全。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from mant.agents.base import AgentResult, AgentTask, BaseAgent

__all__ = ["TranslatorAgent"]


@dataclass(frozen=True, slots=True)
class _RevisionUnit:
    """程序生成的不可歧义修订单元；start/end 只指向正文，不吞并空白间隔。"""

    unit_id: str
    start: int
    end: int
    text: str
    expected_hash: str


@dataclass(frozen=True, slots=True)
class _RevisionDecision:
    """结构化补丁的确定性校验结果。"""

    status: str
    draft: str | None = None
    note_ids: tuple[str, ...] = ()
    operation_count: int = 0
    error: str = ""


_REVISION_SENTENCE_ENDINGS = frozenset(".!?。！？")
_REVISION_SENTENCE_CLOSERS = frozenset("\"'”’)]}）】》」』")


def _revision_units(draft: str) -> list[_RevisionUnit]:
    """按换行/句末确定性切分译文，同时让所有原始间隔留在单元之间。"""
    units: list[_RevisionUnit] = []
    cursor = 0
    size = len(draft)
    while cursor < size:
        while cursor < size and draft[cursor].isspace():
            cursor += 1
        if cursor >= size:
            break
        start = cursor
        end = size
        index = cursor
        while index < size:
            char = draft[index]
            if char in "\r\n":
                end = index
                break
            if char in _REVISION_SENTENCE_ENDINGS:
                boundary = index + 1
                while (
                    boundary < size
                    and draft[boundary] in _REVISION_SENTENCE_CLOSERS
                ):
                    boundary += 1
                if boundary == size or draft[boundary].isspace():
                    end = boundary
                    break
            index += 1
        text = draft[start:end]
        if text:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            units.append(
                _RevisionUnit(
                    unit_id=f"u{len(units) + 1:04d}",
                    start=start,
                    end=end,
                    text=text,
                    expected_hash=digest,
                )
            )
        cursor = max(end, start + 1)
    return units


def _render_revision_units(units: list[_RevisionUnit]) -> str:
    """用 JSON 展示只读单元表，避免自然语言标记混入真实译文。"""
    return json.dumps(
        [
            {
                "unit_id": unit.unit_id,
                "expected_hash": unit.expected_hash,
                "text": unit.text,
            }
            for unit in units
        ],
        ensure_ascii=False,
        indent=2,
    )


def _normalized_revision_notes(review_notes: Any) -> list[dict[str, Any]]:
    """为旧批注补本次请求内稳定 ID；工作流会提供跨阶段持久 ID。"""
    normalized: list[dict[str, Any]] = []
    used: set[str] = set()
    for ordinal, raw in enumerate(review_notes or [], start=1):
        note = dict(raw) if isinstance(raw, dict) else {"suggestion": str(raw)}
        base = str(note.get("note_id") or f"note-{ordinal:04d}").strip()
        note_id = base or f"note-{ordinal:04d}"
        suffix = 2
        while note_id in used:
            note_id = f"{base}-{suffix}"
            suffix += 1
        note["note_id"] = note_id
        used.add(note_id)
        normalized.append(note)
    return normalized


# ----------------------------------------------------------------------
# 记忆注入材料渲染辅助
# TODO: 四个业务 Agent 均用到类似渲染逻辑，待基础设施组抽到公共模块
#       （如 mant.agents.prompts）后统一替换。
# ----------------------------------------------------------------------
def _term_line(source: Any, target: Any, category: str = "") -> str | None:
    """生成单行术语文本；源词或译法为空时返回 None。"""
    source, target = str(source).strip(), str(target).strip()
    if not source or not target:
        return None
    suffix = f"（{category}）" if category else ""
    return f"- {source} → {target}{suffix}"


def _render_glossary(glossary: Any) -> str:
    """把 context 中的术语材料渲染为 Prompt 文本（缺省返回占位说明）。

    兼容形态：
        A. ``dict[str, str]`` —— {源词: 译法}；
        B. ``dict[str, TermEntry | dict]`` —— {源词: 条目}；
        C. ``list[TermEntry | dict]`` —— 条目列表。
    """
    empty_hint = "（暂无术语，按通用译法处理）"
    if not glossary:
        return empty_hint
    lines: list[str] = []
    if isinstance(glossary, dict):
        for source, value in glossary.items():
            if isinstance(value, str):
                line = _term_line(source, value)
            else:
                target = getattr(value, "target", None) or (
                    value.get("target", "") if isinstance(value, dict) else ""
                )
                category = getattr(value, "category", "") or (
                    value.get("category", "") if isinstance(value, dict) else ""
                )
                line = _term_line(source, target, str(category))
            if line:
                lines.append(line)
    else:
        for entry in glossary:
            source = getattr(entry, "source", None) or (
                entry.get("source", "") if isinstance(entry, dict) else ""
            )
            target = getattr(entry, "target", None) or (
                entry.get("target", "") if isinstance(entry, dict) else ""
            )
            category = getattr(entry, "category", "") or (
                entry.get("category", "") if isinstance(entry, dict) else ""
            )
            line = _term_line(source, target, str(category))
            if line:
                lines.append(line)
    # TODO: 术语量大时按 source_text 相关性截断，避免超出上下文窗口
    return "\n".join(lines) or empty_hint


def _render_story_bible(story_bible: Any) -> str:
    """渲染小说圣经（StoryBible dataclass 或 dict），缺省返回占位说明。"""
    empty_hint = "（暂无设定资料）"
    if not story_bible:
        return empty_hint
    if isinstance(story_bible, dict):
        characters = story_bible.get("characters") or []
        settings = story_bible.get("settings") or []
        timeline = story_bible.get("timeline") or []
    else:
        characters = getattr(story_bible, "characters", []) or []
        settings = getattr(story_bible, "settings", []) or []
        timeline = getattr(story_bible, "timeline", []) or []
    # TODO: 人物卡 / 设定条目结构稳定后改为字段化渲染（name/description 等）
    parts: list[str] = []
    if characters:
        parts.append("人物：" + "；".join(str(c) for c in characters))
    if settings:
        parts.append("设定：" + "；".join(str(s) for s in settings))
    if timeline:
        parts.append("时间线：" + "；".join(str(t) for t in timeline))
    return "\n".join(parts) or empty_hint


def _render_tm_matches(tm_matches: Any) -> str:
    """渲染翻译记忆命中列表（TMMatch 或 dict），附相似度分数。"""
    empty_hint = "（暂无相似历史译文）"
    if not tm_matches:
        return empty_hint
    lines: list[str] = []
    for match in tm_matches:
        if isinstance(match, dict):
            source = match.get("source", "")
            target = match.get("target", "")
            score = match.get("score")
        else:
            source = getattr(match, "source", "")
            target = getattr(match, "target", "")
            score = getattr(match, "score", None)
        if not source:
            continue
        prefix = f"[相似度 {score:.2f}] " if isinstance(score, (int, float)) else ""
        lines.append(f"{prefix}{source} → {target}")
    return "\n".join(lines) or empty_hint


def _render_review_notes(review_notes: Any) -> str:
    """把审校/QA 批注渲染为返工硬约束；首次翻译返回明确占位。"""
    if not review_notes:
        return "（首次翻译，无返工批注）"
    lines: list[str] = []
    for note in review_notes:
        if isinstance(note, dict):
            note_id = str(note.get("note_id") or "").strip()
            severity = str(note.get("severity", "?")).strip()
            issue_type = str(note.get("issue_type", "?")).strip()
            span = str(note.get("span", "")).strip()
            suggestion = str(note.get("suggestion", "")).strip()
            detail = suggestion or str(note.get("detail", "")).strip()
            id_part = f"id={note_id}; " if note_id else ""
            lines.append(
                f"- [{id_part}{severity}/{issue_type}] "
                f"{detail}（位置：{span or '未标注'}）"
            )
        else:
            text = str(note).strip()
            if text:
                lines.append(f"- {text}")
    return "\n".join(lines) or "（首次翻译，无返工批注）"


# ----------------------------------------------------------------------
# 译者 Agent
# ----------------------------------------------------------------------
class TranslatorAgent(BaseAgent):
    """初译 Agent：在术语 / 小说圣经 / 翻译记忆注入下产出译文初稿。

    - 模型档位：``strong``（初译质量优先，降低后续审校与返工负担）。
    - 输入：``task.source_text`` 待译原文；记忆注入材料从 ``task.context``
      读取，键为 ``glossary`` / ``story_bible`` / ``tm_matches`` /
      ``prev_summary`` / ``review_notes`` / ``round`` / ``context_before`` /
      ``context_after``（与工作流上下文对齐）。
    - 输出：``AgentResult.output = {"draft": str}`` —— 译文初稿。
    """

    name: str = "translator"
    #: 期望模型档位，供调度 / 工厂用 ``LLMClient.with_tier`` 挑选客户端
    tier: str = "strong"
    #: 采样温度：初译保留少量灵活性，但不发散
    temperature: float = 0.3
    #: 单次补全最大 token 数
    max_tokens: int = 4096
    thinking: str | None = None
    #: 定点修订只允许少量、可验证的局部补丁，避免模型重写或截断整段译文。
    revision_max_operations: int = 8
    revision_max_tokens: int = 1536
    revision_repair_attempts: int = 1
    revision_repair_max_tokens: int = 1024

    # 系统提示词：定义译者角色与翻译规范。
    # 槽位（由 run 内经 build_user_prompt 渲染）：
    #   {glossary} 术语表 / {story_bible} 小说圣经 /
    #   {tm_matches} 翻译记忆 / {prev_summary} 前情提要。
    # 注意：模板内不得出现未转义的裸花括号（build_user_prompt 基于 format_map）。
    SYSTEM_PROMPT: str = """你是一名资深网络小说译者，擅长把中文网文翻译成地道的英文，译文深受海外读者喜爱。

【翻译准则】
1. 忠实原文：剧情、人物、设定、对白含义不得遗漏或篡改，禁止自行增删情节。
2. 网文文风：节奏明快、对白生动、画面感强，保留爽点与悬念感；避免逐字硬译与翻译腔。
3. 术语一致：下列术语表中的约定译法必须严格遵循，不得另造译名。
4. 切片边界：只翻译“待译核心原文”；相邻上文和下文仅用于消歧，禁止把它们翻译、复述或输出。

【返工轮次】
第 {round} 轮。若下方存在返工批注，必须逐条修复；返工批注的优先级高于
翻译记忆和一般文风偏好，但不得违反原文事实与术语表。

【返工批注（最高优先级）】
{review_notes}

【术语表】
{glossary}

【小说圣经（背景设定，仅供理解上下文，不要复述进译文）】
{story_bible}

【翻译记忆（相似句对的历史译文，可参考但不必照搬）】
{tm_matches}

【前情提要（保证上下文连贯）】
{prev_summary}

只输出译文正文，不要输出任何解释、注释、标签或前后缀。"""

    #: 用户提示词模板：相邻上下文只帮助消歧，只有 source_text 可以进入译文。
    USER_PROMPT_TEMPLATE: str = """【相邻上文（仅供理解，禁止翻译或输出）】
{context_before}

【待译核心原文（只翻译此区域）】
{source_text}

【相邻下文（仅供理解，禁止翻译或输出）】
{context_after}

【译文】"""

    REVISION_SYSTEM_PROMPT: str = """你是一名资深网络小说译者，当前只负责定点修订译文。

【职责边界】
1. 逐条落实审校意见中的漏译、误译和专名错误；不得遗漏任何 high 问题。
2. 对照原文保留现有译文中已经正确的全部事实、段落顺序和专名，不得删减或重复。
3. 只修正审校指出的事实性问题和为保证语法所必需的局部表达；文学润色由下游负责。
4. 相邻上下文仅用于消歧，禁止把它们翻译、复述或输出。

【必须落实的审校意见】
{review_notes}

【术语表】
{glossary}

【输出协议】
只输出一个 JSON object，不要输出完整译文、解释、代码块或前后缀：
{{"operations":[{{"action":"replace|insert_before|insert_after|delete","anchor":"当前译文中逐字复制的唯一连续片段","text":"替换或插入文本"}}]}}
1. anchor 必须逐字来自当前译文，且在应用该操作时只出现一次；不得改写、概括或凭空制造 anchor。
2. 操作按数组顺序执行，最多 {max_operations} 个；只提交落实审校意见所需的最小局部修改。
3. replace、insert_before、insert_after 必须提供非空 text；delete 可以省略 text。
4. 不要用一个覆盖整篇译文的 replace 操作。"""

    REVISION_REPAIR_SYSTEM_PROMPT: str = """上一次定点修订输出不是可安全应用的结构化补丁。
重新对照当前译文和审校意见，只输出符合既定协议的 JSON object。anchor 必须从当前译文逐字复制并且唯一；不得输出完整译文。"""

    REVISION_USER_PROMPT_TEMPLATE: str = """【相邻上文（仅供理解，禁止输出）】
{context_before}

【原文】
{source_text}

【当前译文】
{draft}

【相邻下文（仅供理解，禁止输出）】
{context_after}

【结构化局部补丁 JSON】"""

    # v4 字符串锚点 Prompt 保留为旧 checkpoint 的 translate 阶段兼容语义；
    # revision 实际使用下列 v5 unit-ID 协议，并由工作流只写入 revise 指纹。
    REVISION_UNIT_SYSTEM_PROMPT: str = """你是一名资深网络小说译者，当前只负责定点修订译文。

【职责边界】
1. 逐条落实审校意见中的漏译、误译和专名错误；不得遗漏任何 high 问题。
2. 只改动程序提供的译文单元；不得生成 unit_id 或 expected_hash。
3. 只修正事实问题和必要语法；文学润色由下游负责。
4. 相邻上下文仅用于消歧，禁止把它们翻译、复述或输出。

【必须落实的审校意见】
{review_notes}

【术语表】
{glossary}

【输出协议】
需要修改时只输出：
{{"status":"apply","operations":[{{"action":"replace_unit|insert_before_unit|insert_after_unit|delete_unit","unit_id":"u0001","expected_hash":"程序给出的哈希","note_ids":["批注ID"],"text":"替换或插入文本"}}]}}
确认现有译文已经落实全部批注、无需修改时只输出：
{{"status":"no_change","operations":[],"evidence":[{{"note_id":"批注ID","unit_id":"u0001","expected_hash":"程序给出的哈希","quote":"该单元中逐字复制的证据"}}]}}

硬约束：
1. unit_id、expected_hash 和 note_id 只能逐字复制程序给出的值。
2. 每个 apply operation 必须关联至少一个 note_id，全部批注 ID 必须被覆盖。
3. 同一 unit_id 最多一个 operation；操作总数最多 {max_operations} 个。
4. replace/insert 必须提供非空 text；delete 可以省略 text。
5. text 只包含新正文及所需空白，不得包含单元 ID、解释、代码块或完整片段译文。"""

    REVISION_UNIT_REPAIR_SYSTEM_PROMPT: str = """上一次输出未通过确定性校验。
必须针对用户消息末尾给出的具体错误修复补丁；只引用有效 unit_id、expected_hash
和 note_id，不得重复原无效操作，不得输出解释或完整译文。"""

    REVISION_UNIT_USER_PROMPT_TEMPLATE: str = """【相邻上文（仅供理解，禁止输出）】
{context_before}

【原文】
{source_text}

【程序生成的可修改译文单元 JSON】
{revision_units}

【相邻下文（仅供理解，禁止输出）】
{context_after}

【unit-ID 局部补丁 JSON】"""

    def _apply_revision_patch(
        self,
        draft: str,
        units: list[_RevisionUnit],
        payload: Any,
        expected_note_ids: list[str],
    ) -> _RevisionDecision:
        """基于原始 unit 快照校验并一次性重建；任何歧义都拒绝整批操作。"""
        if not isinstance(payload, dict):
            return _RevisionDecision("rejected", error="修订输出必须是 JSON object")
        status = str(payload.get("status") or "").strip().lower()
        if status not in {"apply", "no_change"}:
            return _RevisionDecision(
                "rejected", error="status 必须是 apply 或 no_change"
            )
        unit_by_id = {unit.unit_id: unit for unit in units}
        expected_notes = set(expected_note_ids)

        if status == "no_change":
            operations = payload.get("operations")
            if operations not in (None, []):
                return _RevisionDecision(
                    "rejected", error="no_change 的 operations 必须为空数组"
                )
            evidence = payload.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                return _RevisionDecision(
                    "rejected", error="no_change 必须提供非空 evidence 数组"
                )
            covered: set[str] = set()
            for index, item in enumerate(evidence, start=1):
                if not isinstance(item, dict):
                    return _RevisionDecision(
                        "rejected", error=f"第 {index} 个 evidence 不是 JSON object"
                    )
                note_id = str(item.get("note_id") or "").strip()
                unit_id = str(item.get("unit_id") or "").strip()
                expected_hash = str(item.get("expected_hash") or "").strip()
                quote = str(item.get("quote") or "")
                unit = unit_by_id.get(unit_id)
                if note_id not in expected_notes:
                    return _RevisionDecision(
                        "rejected",
                        error=f"第 {index} 个 evidence 引用了未知 note_id {note_id!r}",
                    )
                if unit is None:
                    return _RevisionDecision(
                        "rejected",
                        error=f"第 {index} 个 evidence 引用了未知 unit_id {unit_id!r}",
                    )
                if expected_hash != unit.expected_hash:
                    return _RevisionDecision(
                        "rejected",
                        error=(
                            f"第 {index} 个 evidence 的 expected_hash 与 {unit_id} 不匹配；"
                            f"正确值为 {unit.expected_hash}"
                        ),
                    )
                if not quote or quote not in unit.text:
                    return _RevisionDecision(
                        "rejected",
                        error=f"第 {index} 个 evidence 的 quote 不在 {unit_id} 中",
                    )
                covered.add(note_id)
            if covered != expected_notes:
                missing = sorted(expected_notes - covered)
                return _RevisionDecision(
                    "rejected", error=f"no_change evidence 未覆盖批注：{missing}"
                )
            return _RevisionDecision(
                "no_change",
                draft=draft,
                note_ids=tuple(sorted(covered)),
            )

        operations = payload.get("operations")
        if not isinstance(operations, list) or not operations:
            return _RevisionDecision("rejected", error="operations 必须是非空数组")
        if len(operations) > self.revision_max_operations:
            return _RevisionDecision(
                "rejected",
                error=(
                    f"operations 数量 {len(operations)} 超过上限 "
                    f"{self.revision_max_operations}"
                ),
            )

        allowed_actions = {
            "replace_unit",
            "insert_before_unit",
            "insert_after_unit",
            "delete_unit",
        }
        by_unit: dict[str, tuple[str, str]] = {}
        covered_notes: set[str] = set()
        for index, operation in enumerate(operations, start=1):
            if not isinstance(operation, dict):
                return _RevisionDecision(
                    "rejected", error=f"第 {index} 个 operation 不是 JSON object"
                )
            action = str(operation.get("action") or "").strip()
            if action not in allowed_actions:
                return _RevisionDecision(
                    "rejected",
                    error=f"第 {index} 个 operation 的 action 不受支持：{action!r}",
                )
            unit_id = str(operation.get("unit_id") or "").strip()
            unit = unit_by_id.get(unit_id)
            if unit is None:
                return _RevisionDecision(
                    "rejected",
                    error=(
                        f"第 {index} 个 operation 引用了未知 unit_id {unit_id!r}；"
                        f"有效值为 {sorted(unit_by_id)}"
                    ),
                )
            if unit_id in by_unit:
                return _RevisionDecision(
                    "rejected", error=f"同一 unit_id {unit_id} 最多一个 operation"
                )
            expected_hash = str(operation.get("expected_hash") or "").strip()
            if expected_hash != unit.expected_hash:
                return _RevisionDecision(
                    "rejected",
                    error=(
                        f"第 {index} 个 operation 的 expected_hash 与 {unit_id} 不匹配；"
                        f"正确值为 {unit.expected_hash}"
                    ),
                )
            raw_note_ids = operation.get("note_ids")
            if not isinstance(raw_note_ids, list) or not raw_note_ids:
                return _RevisionDecision(
                    "rejected", error=f"第 {index} 个 operation 缺少 note_ids"
                )
            operation_note_ids = {
                str(note_id).strip() for note_id in raw_note_ids if str(note_id).strip()
            }
            unknown_notes = sorted(operation_note_ids - expected_notes)
            if not operation_note_ids or unknown_notes:
                return _RevisionDecision(
                    "rejected",
                    error=(
                        f"第 {index} 个 operation 的 note_ids 无效；"
                        f"未知值为 {unknown_notes}"
                    ),
                )
            covered_notes.update(operation_note_ids)

            text = operation.get("text")
            if action != "delete_unit" and (
                not isinstance(text, str) or not text.strip()
            ):
                return _RevisionDecision(
                    "rejected",
                    error=f"第 {index} 个 {action} operation 缺少非空 text",
                )
            replacement = str(text or "")
            if action == "replace_unit":
                ratio = len(replacement.strip()) / max(1, len(unit.text.strip()))
                if not 0.2 <= ratio <= 5.0:
                    return _RevisionDecision(
                        "rejected",
                        error=(
                            f"第 {index} 个 replace_unit 长度比例 {ratio:.3f} "
                            "超出 [0.2, 5.0]"
                        ),
                    )
            elif action.startswith("insert_"):
                insert_limit = max(1000, len(unit.text) * 5)
                if len(replacement) > insert_limit:
                    return _RevisionDecision(
                        "rejected",
                        error=(
                            f"第 {index} 个 {action} 文本 {len(replacement)} 字符"
                            f"超过上限 {insert_limit}"
                        ),
                    )
            by_unit[unit_id] = (action, replacement)

        if covered_notes != expected_notes:
            missing = sorted(expected_notes - covered_notes)
            return _RevisionDecision(
                "rejected", error=f"operations 未覆盖批注：{missing}"
            )

        pieces: list[str] = []
        cursor = 0
        for unit in units:
            pieces.append(draft[cursor : unit.start])
            operation = by_unit.get(unit.unit_id)
            if operation is None:
                pieces.append(unit.text)
            else:
                action, text = operation
                if action == "replace_unit":
                    pieces.append(text)
                elif action == "insert_before_unit":
                    pieces.extend((text, unit.text))
                elif action == "insert_after_unit":
                    pieces.extend((unit.text, text))
                elif action == "delete_unit":
                    pieces.append("")
            cursor = unit.end
        pieces.append(draft[cursor:])
        revised = "".join(pieces)
        if revised == draft:
            return _RevisionDecision("rejected", error="补丁应用后译文没有变化")
        return _RevisionDecision(
            "applied",
            draft=revised,
            note_ids=tuple(sorted(covered_notes)),
            operation_count=len(operations),
        )

    def _run_revision(self, task: AgentTask) -> AgentResult:
        """按审校意见应用结构化局部补丁，输出仍沿用 ``{"draft": str}`` 契约。"""
        notes: list[str] = []
        draft = str(task.context.get("draft") or "").strip()
        if not draft:
            notes.append("task.context 缺少 draft（待修订译文），定点修订跳过")
            return self._result(ok=False, output={"draft": ""}, notes=notes)

        review_notes = _normalized_revision_notes(
            task.context.get("review_notes") or []
        )
        if not review_notes:
            notes.append("没有待处理的审校意见，沿用当前译文")
            return self._result(
                ok=True,
                output={"draft": draft, "revision_status": "not_required"},
                notes=notes,
            )

        units = _revision_units(draft)
        if not units:
            notes.append("当前译文无法生成非空修订单元")
            return self._result(
                ok=False,
                output={"draft": "", "revision_status": "provider_error"},
                notes=notes,
            )
        expected_note_ids = [str(note["note_id"]) for note in review_notes]

        system = self.build_user_prompt(
            self.REVISION_UNIT_SYSTEM_PROMPT,
            review_notes=_render_review_notes(review_notes),
            glossary=_render_glossary(task.context.get("glossary")),
            max_operations=self.revision_max_operations,
        )
        user = self.build_user_prompt(
            self.REVISION_UNIT_USER_PROMPT_TEMPLATE,
            source_text=task.source_text,
            revision_units=_render_revision_units(units),
            context_before=str(task.context.get("context_before") or "（无）"),
            context_after=str(task.context.get("context_after") or "（无）"),
        )
        attempts = max(0, self.revision_repair_attempts) + 1
        previous_reason = ""
        previous_raw = ""
        for attempt in range(attempts):
            active_system = system
            active_user = user
            max_tokens = self.revision_max_tokens
            if attempt:
                active_system = (
                    f"{system}\n\n{self.REVISION_UNIT_REPAIR_SYSTEM_PROMPT}"
                )
                active_user = (
                    f"{user}\n\n【上次确定性校验错误】\n{previous_reason}"
                    f"\n\n【上次无效输出（禁止原样重复）】\n{previous_raw[-4000:]}"
                    f"\n\n【有效 unit_id】\n{', '.join(unit.unit_id for unit in units)}"
                    f"\n\n【必须覆盖的 note_id】\n{', '.join(expected_note_ids)}"
                )
                max_tokens = self.revision_repair_max_tokens
            try:
                raw = self.llm.complete(
                    active_system,
                    active_user,
                    temperature=0.0,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    thinking=self.thinking,
                )
            except Exception as exc:  # noqa: BLE001 —— Agent 结果契约承接失败
                notes.append(f"LLM 调用失败：{exc!r}")
                return self._result(
                    ok=False,
                    output={"draft": "", "revision_status": "provider_error"},
                    notes=notes,
                )
            notes.extend(getattr(self.llm, "last_notes", []))
            if not raw.strip():
                notes.append("LLM 返回空修订补丁，按供应商/截断失败处理")
                return self._result(
                    ok=False,
                    output={"draft": "", "revision_status": "provider_error"},
                    notes=notes,
                )

            # 保留无 API key 的离线联调语义；真实模型的普通文本绝不作为译文覆盖。
            if raw.lstrip().startswith("[DRAFT]"):
                return self._result(
                    ok=True,
                    output={
                        "draft": raw,
                        "revision_protocol": "draft_fallback",
                        "revision_status": "draft_fallback",
                        "revision_note_ids": expected_note_ids,
                    },
                    notes=notes,
                )

            parse_notes: list[str] = []
            parsed = self.parse_json_output(raw, notes=parse_notes)
            decision = self._apply_revision_patch(
                draft, units, parsed, expected_note_ids
            )
            if decision.status in {"applied", "no_change"}:
                return self._result(
                    ok=True,
                    output={
                        "draft": str(decision.draft or draft),
                        "revision_protocol": "unit_patch_v5",
                        "revision_status": decision.status,
                        "revision_operations": decision.operation_count,
                        "revision_note_ids": list(decision.note_ids),
                    },
                    notes=notes,
                )

            reason = decision.error or "; ".join(parse_notes) or "未知协议错误"
            previous_reason = reason
            previous_raw = raw
            if attempt + 1 < attempts:
                notes.append(f"unit-ID 修订补丁无效，进行错误感知重试：{reason}")
            else:
                notes.extend(parse_notes)
                notes.append(f"unit-ID 修订补丁无效，拒绝覆盖当前译文：{reason}")

        return self._result(
            ok=True,
            output={
                "draft": draft,
                "revision_protocol": "unit_patch_v5",
                "revision_status": "protocol_rejected",
                "revision_operations": 0,
                "revision_note_ids": [],
                "revision_error": previous_reason or reason,
            },
            notes=notes,
        )

    def run(self, task: AgentTask) -> AgentResult:
        """执行初译：读取记忆注入材料 → 渲染 Prompt → 调用 LLM → 返回初稿。

        约定：
            - 不抛出未捕获异常；失败时 ``ok=False`` 并在 ``notes`` 说明原因；
            - 未配置真实模型时透传 ``[DRAFT]`` 占位响应，保证离线联调可跑通。
        """
        if str(task.context.get("mode") or "").strip().lower() == "revision":
            return self._run_revision(task)

        notes: list[str] = []

        # 1) 从 task.context 读取记忆注入材料（由调度层 / MemoryHub 提前注入）
        # TODO: 与调度层最终对齐 context 键名；必要时改为经 self.memory
        #       （MemoryHub，注意容忍 None）现取 lookup_terms / search_tm /
        #       get_story_bible。
        glossary_text = _render_glossary(task.context.get("glossary"))
        story_bible_text = _render_story_bible(task.context.get("story_bible"))
        tm_matches_text = _render_tm_matches(task.context.get("tm_matches"))
        prev_summary = str(task.context.get("prev_summary") or "（无前情提要）")
        review_notes_text = _render_review_notes(task.context.get("review_notes"))
        round_no = max(0, int(task.context.get("round", 0) or 0))
        context_before = str(task.context.get("context_before") or "（无）")
        context_after = str(task.context.get("context_after") or "（无）")

        # 2) 渲染 Prompt（系统提示词含记忆注入槽位，用户提示词承载原文）
        system = self.build_user_prompt(
            self.SYSTEM_PROMPT,
            glossary=glossary_text,
            story_bible=story_bible_text,
            tm_matches=tm_matches_text,
            prev_summary=prev_summary,
            review_notes=review_notes_text,
            round=round_no,
        )
        user = self.build_user_prompt(
            self.USER_PROMPT_TEMPLATE,
            source_text=task.source_text,
            context_before=context_before,
            context_after=context_after,
        )

        # 3) 调用 LLM；未配置 API key 时 LLMClient 返回 [DRAFT] 占位响应，照常透传
        try:
            draft = self.llm.complete(
                system,
                user,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                thinking=self.thinking,
            )
        except Exception as exc:  # noqa: BLE001 —— 骨架期统一降级，不向上抛
            notes.append(f"LLM 调用失败：{exc!r}")
            return self._result(ok=False, output={"draft": ""}, notes=notes)
        # 合并客户端侧说明（降级原因 / 重试记录等）
        notes.extend(self.llm.last_notes)

        # 4) 组装结果
        if not draft.strip():
            notes.append("LLM 返回空译文，请检查模型配置或原文内容")
            return self._result(ok=False, output={"draft": ""}, notes=notes)
        # TODO: 可选追加 output["used_terms"]，记录本段实际命中的术语，供实验分析
        return self._result(ok=True, output={"draft": draft}, notes=notes)
