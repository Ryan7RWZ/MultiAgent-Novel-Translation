"""译者 Agent（TranslatorAgent）：术语注入式初译。

骨架级别实现：定义中文角色 Prompt 模板与 ``run(task)`` 流程框架。
记忆注入材料（术语表 / 小说圣经 / 翻译记忆 / 前情提要）一律从
``task.context`` 读取（由调度层 / MemoryHub 提前注入），具体业务细节
以 TODO 标出，待后续迭代补全。
"""

from __future__ import annotations

from typing import Any

from mant.agents.base import AgentResult, AgentTask, BaseAgent

__all__ = ["TranslatorAgent"]


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
            severity = str(note.get("severity", "?")).strip()
            issue_type = str(note.get("issue_type", "?")).strip()
            span = str(note.get("span", "")).strip()
            suggestion = str(note.get("suggestion", "")).strip()
            detail = suggestion or str(note.get("detail", "")).strip()
            lines.append(f"- [{severity}/{issue_type}] {detail}（位置：{span or '未标注'}）")
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

    def run(self, task: AgentTask) -> AgentResult:
        """执行初译：读取记忆注入材料 → 渲染 Prompt → 调用 LLM → 返回初稿。

        约定：
            - 不抛出未捕获异常；失败时 ``ok=False`` 并在 ``notes`` 说明原因；
            - 未配置真实模型时透传 ``[DRAFT]`` 占位响应，保证离线联调可跑通。
        """
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
                system, user, temperature=self.temperature, max_tokens=self.max_tokens
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
