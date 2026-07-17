"""审校 Agent（EditorAgent）：对照原文逐段审校，产出结构化审校意见。

骨架级别实现：只找问题、不改译文；模型输出受 JSON schema 约束，
经 ``parse_json_output`` 稳健解析，失败时降级为空意见列表并记录 notes。
"""

from __future__ import annotations

from typing import Any

from mant.agents.base import AgentResult, AgentTask, BaseAgent

__all__ = ["EditorAgent"]

#: 允许的审校问题类型（与 EditorAgent.SYSTEM_PROMPT 保持一致）
ISSUE_TYPES: tuple[str, ...] = ("omission", "mistranslation", "proper_noun", "other")
#: 允许的严重程度（与 EditorAgent.SYSTEM_PROMPT 保持一致）
SEVERITIES: tuple[str, ...] = ("high", "medium", "low")


# ----------------------------------------------------------------------
# 输出规整 / 上下文渲染辅助
# ----------------------------------------------------------------------
def _normalize_review_note(item: Any) -> dict[str, Any] | None:
    """把单条审校意见规整为标准 dict；无法识别时返回 None。

    标准结构：``{"issue_type", "span", "suggestion", "severity"}``；
    非法的 issue_type / severity 分别降级为 ``other`` / ``medium``。
    """
    if not isinstance(item, dict):
        return None
    issue_type = str(item.get("issue_type", "other")).strip().lower()
    if issue_type not in ISSUE_TYPES:
        issue_type = "other"
    severity = str(item.get("severity", "medium")).strip().lower()
    if severity not in SEVERITIES:
        severity = "medium"
    return {
        "issue_type": issue_type,
        "span": str(item.get("span", "")).strip(),
        "suggestion": str(item.get("suggestion", "")).strip(),
        "severity": severity,
    }


def _extract_review_notes(parsed: Any) -> list[dict[str, Any]] | None:
    """从 JSON 解析结果中提取审校意见列表；结构不符返回 None。

    兼容两种形态：``{"review_notes": [...]}`` 或模型直接输出的裸数组。
    """
    raw_notes: Any = None
    if isinstance(parsed, dict):
        raw_notes = parsed.get("review_notes")
    elif isinstance(parsed, list):
        raw_notes = parsed
    if not isinstance(raw_notes, list):
        return None
    return [n for n in (_normalize_review_note(item) for item in raw_notes) if n]


def _render_glossary(glossary: Any) -> str:
    """把 context 中的术语材料渲染为 Prompt 文本（缺省返回占位说明）。

    TODO: 与 translator.py 中的同名辅助重复，待基础设施组抽到公共模块
    （如 mant.agents.prompts）后统一替换。
    """
    empty_hint = "（未提供术语表）"
    if not glossary:
        return empty_hint
    lines: list[str] = []
    if isinstance(glossary, dict):
        for source, value in glossary.items():
            target = value if isinstance(value, str) else (
                getattr(value, "target", None)
                or (value.get("target", "") if isinstance(value, dict) else "")
            )
            if source and target:
                lines.append(f"- {source} → {target}")
    else:
        for entry in glossary:
            source = getattr(entry, "source", None) or (
                entry.get("source", "") if isinstance(entry, dict) else ""
            )
            target = getattr(entry, "target", None) or (
                entry.get("target", "") if isinstance(entry, dict) else ""
            )
            if source and target:
                lines.append(f"- {source} → {target}")
    return "\n".join(lines) or empty_hint


# ----------------------------------------------------------------------
# 审校 Agent
# ----------------------------------------------------------------------
class EditorAgent(BaseAgent):
    """审校 Agent：对照原文逐段检查初稿，输出结构化审校意见。

    - 模型档位：``strong``（审校质量直接决定返工有效性）。
    - 输入：``task.source_text`` 原文；``task.context["draft"]`` 译文初稿
      （必需）；``task.context["glossary"]`` 术语表（可选）。
    - 输出：``AgentResult.output = {"review_notes": list[dict]}``，每条含
      ``issue_type`` / ``span`` / ``suggestion`` / ``severity`` 四键。
    - 纪律：只指出问题并给出修改建议，绝不直接改写译文。
    """

    name: str = "editor"
    #: 期望模型档位，供调度 / 工厂用 ``LLMClient.with_tier`` 挑选客户端
    tier: str = "strong"
    #: 采样温度：审校要求低发散、可复现
    temperature: float = 0.1
    #: 单次补全最大 token 数
    max_tokens: int = 4096

    # 系统提示词：定义审校角色、检查重点与 JSON 输出 schema。
    # 不含槽位（直接作为 system 传入），因此可以安全包含 JSON 花括号示例。
    SYSTEM_PROMPT: str = """你是一名严谨苛刻的翻译审校编辑，负责对照原文逐段审查译文初稿。

【审校重点】
1. 漏译（omission）：原文信息（情节、描写、对白、修饰成分）在译文中缺失。
2. 误译（mistranslation）：语义、逻辑、指代、语气的翻译错误。
3. 专名错误（proper_noun）：人名、地名、门派、功法等专名与术语表不一致或拼写有误。
4. 其他（other）：语法、搭配等影响阅读理解的问题。

【工作纪律】
- 逐段对照，不放过任何一处漏译；但没有问题也不要硬凑。
- 只指出问题并给出修改建议，绝不直接改写译文。

【输出要求】
只输出 JSON，不要输出任何其他文字、解释或代码块标记。格式如下：
{
  "review_notes": [
    {
      "issue_type": "omission | mistranslation | proper_noun | other",
      "span": "译文中存在问题片段的摘录（尽量短，便于定位）",
      "suggestion": "修改建议：说明问题并给出建议译法",
      "severity": "high | medium | low"
    }
  ]
}
severity 约定：high = 漏译 / 事实性误译 / 专名错误；medium = 明显影响理解的问题；low = 细微瑕疵。
若完全没有问题，输出：{"review_notes": []}"""

    #: 用户提示词模板：{source_text} 原文 / {draft} 初稿 / {glossary} 术语表
    USER_PROMPT_TEMPLATE: str = """【原文】
{source_text}

【译文初稿】
{draft}

【术语表】
{glossary}

请逐段对照审校，并按要求只输出 JSON。"""

    def run(self, task: AgentTask) -> AgentResult:
        """执行审校：渲染 Prompt → 调用 LLM → 解析 JSON → 规整意见列表。

        约定：
            - 不抛出未捕获异常；失败 / 解析异常时 ``ok=False`` 或降级为
              空意见列表，原因写入 ``notes``；
            - 未配置真实模型时占位响应无法解析为 JSON，同样走安全降级。
        """
        notes: list[str] = []

        # 1) 读取输入：初稿缺失时无法审校，安全默认空意见并明确说明
        draft = str(task.context.get("draft") or "").strip()
        if not draft:
            notes.append("task.context 缺少 draft（译文初稿），审校跳过")
            return self._result(ok=False, output={"review_notes": []}, notes=notes)
        glossary_text = _render_glossary(task.context.get("glossary"))

        # 2) 渲染用户提示词（SYSTEM_PROMPT 无槽位，直接作为系统提示词）
        user = self.build_user_prompt(
            self.USER_PROMPT_TEMPLATE,
            source_text=task.source_text,
            draft=draft,
            glossary=glossary_text,
        )

        # TODO: 超长章节分段审校后合并意见（span 定位需带段号前缀）

        # 3) 调用 LLM
        try:
            raw = self.llm.complete(
                self.SYSTEM_PROMPT,
                user,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 —— 骨架期统一降级，不向上抛
            notes.append(f"LLM 调用失败：{exc!r}")
            return self._result(ok=False, output={"review_notes": []}, notes=notes)
        # 合并客户端侧说明（降级原因 / 重试记录等）
        notes.extend(self.llm.last_notes)

        # 4) 解析 JSON 输出；parse_json_output 失败时返回 None 并自动记录 notes
        parsed = self.parse_json_output(raw, notes)
        review_notes = _extract_review_notes(parsed)
        if review_notes is None:
            notes.append("审校意见结构不符合 schema，降级为空意见列表（安全默认）")
            review_notes = []

        # TODO: 高 severity 意见可触发快捷回退通路（直接建议 QA 判 rework）
        return self._result(
            ok=True, output={"review_notes": review_notes}, notes=notes
        )
