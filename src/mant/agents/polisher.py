"""润色 Agent（PolisherAgent）：目标语地道化与文风统一。

骨架级别实现：只改语言、不改事实，专名与术语原样保留；
可附带审校意见（review_notes）作为语言层面的润色参考。
"""

from __future__ import annotations

from typing import Any

from mant.agents.base import AgentResult, AgentTask, BaseAgent

__all__ = ["PolisherAgent"]


# ----------------------------------------------------------------------
# 上下文渲染辅助
# ----------------------------------------------------------------------
def _render_review_notes(review_notes: Any) -> str:
    """把审校意见列表渲染为 Prompt 文本；空列表返回空串。

    TODO: 与 qa.py 中的同名辅助重复，待基础设施组抽到公共模块
    （如 mant.agents.prompts）后统一替换。
    """
    if not review_notes:
        return ""
    lines: list[str] = []
    for note in review_notes:
        if isinstance(note, dict):
            severity = note.get("severity", "?")
            issue_type = note.get("issue_type", "?")
            suggestion = note.get("suggestion", "")
            span = note.get("span", "")
            lines.append(f"- [{severity}/{issue_type}] {suggestion}（片段：{span}）")
        else:
            lines.append(f"- {note}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 润色 Agent
# ----------------------------------------------------------------------
class PolisherAgent(BaseAgent):
    """润色 Agent：目标语地道化与文风统一。

    - 模型档位：``fast``（语言改写属高频轻量调用）。
    - 输入：``task.context["draft"]`` 待润色译文（必需）；
      ``task.context["review_notes"]`` 审校意见（可选，仅作语言层面参考）。
    - 输出：``AgentResult.output = {"polished": str}`` —— 润色后译文。
    - 红线：禁止改动剧情事实、设定与专有名词；段落结构保持不变。
    """

    name: str = "polisher"
    #: 期望模型档位，供调度 / 工厂用 ``LLMClient.with_tier`` 挑选客户端
    tier: str = "fast"
    #: 采样温度：润色需要略高的语言多样性
    temperature: float = 0.4
    #: 单次补全最大 token 数
    max_tokens: int = 4096

    # 系统提示词：定义润色师角色与红线规则。不含槽位。
    SYSTEM_PROMPT: str = """你是一名英文网络小说润色师，让译文读起来像英语母语者写的网文。

【润色规则】
1. 只改语言：提升地道度、流畅度与节奏感，统一人称、时态与全书文风。
2. 禁止改动事实：剧情、人物关系、设定、数值、对白含义一律不得改变。
3. 禁止改动专名：人名、地名、门派、功法等专有名词与术语必须原样保留。
4. 保留结构：段落划分、对话归属与换行结构不得重排。
5. 只输出润色后的译文正文，不要输出任何解释、批注或前后缀。"""

    #: 用户提示词模板：{draft} 待润色译文 / {review_notes_section} 审校意见区块（可空）
    USER_PROMPT_TEMPLATE: str = """【待润色译文】
{draft}
{review_notes_section}
【润色后译文】"""

    #: 审校意见区块模板（仅当 context 提供 review_notes 时拼接）
    REVIEW_NOTES_SECTION_TEMPLATE: str = """
【审校意见（仅供语言层面参考；其中涉及事实与专名的改动一律不采纳）】
{review_notes}
"""

    def run(self, task: AgentTask) -> AgentResult:
        """执行润色：渲染 Prompt → 调用 LLM → 返回润色稿。

        约定：
            - 不抛出未捕获异常；失败时 ``ok=False`` 并在 ``notes`` 说明原因；
            - 未配置真实模型时透传 ``[DRAFT]`` 占位响应，保证离线联调可跑通。
        """
        notes: list[str] = []

        # 1) 读取输入：待润色译文缺失时直接失败返回
        draft = str(task.context.get("draft") or "").strip()
        if not draft:
            notes.append("task.context 缺少 draft（待润色译文），润色跳过")
            return self._result(ok=False, output={"polished": ""}, notes=notes)

        # 2) 审校意见为可选输入：有则渲染为参考区块，无则置空
        review_notes_text = _render_review_notes(task.context.get("review_notes"))
        review_notes_section = ""
        if review_notes_text:
            review_notes_section = self.build_user_prompt(
                self.REVIEW_NOTES_SECTION_TEMPLATE, review_notes=review_notes_text
            )

        # 3) 渲染 Prompt（SYSTEM_PROMPT 无槽位，直接作为系统提示词）
        user = self.build_user_prompt(
            self.USER_PROMPT_TEMPLATE,
            draft=draft,
            review_notes_section=review_notes_section,
        )

        # TODO: 全书文风指纹（措辞偏好 / 句长分布）注入，保证跨章节文风统一

        # 4) 调用 LLM；未配置 API key 时透传 [DRAFT] 占位响应
        try:
            polished = self.llm.complete(
                self.SYSTEM_PROMPT,
                user,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 —— 骨架期统一降级，不向上抛
            notes.append(f"LLM 调用失败：{exc!r}")
            return self._result(ok=False, output={"polished": ""}, notes=notes)
        # 合并客户端侧说明（降级原因 / 重试记录等）
        notes.extend(self.llm.last_notes)

        # 5) 组装结果
        if not polished.strip():
            notes.append("LLM 返回空润色稿，请检查模型配置")
            return self._result(ok=False, output={"polished": ""}, notes=notes)
        # TODO: 专名保护校验（对比 draft/polished 中专名集合，发现被改动时告警）
        return self._result(ok=True, output={"polished": polished}, notes=notes)
