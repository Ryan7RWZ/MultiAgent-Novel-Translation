"""Agent 基类与统一任务/结果数据模型。

本模块定义多智能体协作的公共契约，所有具体 Agent（调度 Dispatcher、
术语 Terminology、翻译 Translator、审校 Reviewer、润色 Polisher、QA 终审）
都继承 ``BaseAgent`` 并实现 ``run``。

子类约定（务必遵守，保证 LangGraph 各节点可互换）：
    1. 每个子类定义类常量 ``SYSTEM_PROMPT: str``，作为该角色的系统提示词；
       涉及槽位的用户提示词模板建议定义为 ``USER_PROMPT_TEMPLATE``，
       在 ``run`` 内通过 ``self.build_user_prompt(USER_PROMPT_TEMPLATE, **slots)``
       渲染。
    2. 实现 ``run(task: AgentTask) -> AgentResult``：
       - 成功时 ``ok=True``，业务产物放入 ``output`` 字典；
       - 失败/降级时 ``ok=False``，原因写入 ``notes``，不得直接抛异常中断图。
    3. ``output`` 的键由各子类在自己的 docstring 中声明，例如：
       - Translator: ``{"draft": str, "used_terms": list[str]}``
       - Reviewer:   ``{"review_notes": list[str], "fixed_draft": str}``
       - QA:         ``{"qa_score": float, "qa_verdict": str, "issues": list[dict]}``
       状态字段与 ``mant.workflow.state.TranslationState`` 对齐，键名变更
       需同步更新状态机。
    4. 需要记忆/检索能力时通过 ``self.memory``（``MemoryHub`` 门面，
       可能为 None）调用 ``lookup_terms`` / ``search_tm`` / ``get_story_bible``
       等，子类须能容忍 ``memory is None`` 的离线场景。
    5. 模型输出要求 JSON 时，用 ``parse_json_output`` 稳健提取；
       解析失败返回 None 并在 notes 中记录，随后按各角色策略降级。

第三方依赖规则：本模块仅依赖 stdlib；``MemoryHub`` 仅做类型标注，
采用 ``TYPE_CHECKING`` 导入，避免循环依赖与重依赖。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型标注，避免运行时循环依赖
    from mant.llm.client import LLMClient
    from mant.memory import MemoryHub


# ----------------------------------------------------------------------
# 统一任务 / 结果模型（全项目约定，勿重复定义）
# ----------------------------------------------------------------------
@dataclass
class AgentTask:
    """派发给单个 Agent 的任务单元。

    字段:
        work_id: 作品标识（关联术语库 / 小说圣经 / TM 的命名空间）。
        chapter_id: 章节标识。
        segment_id: 句段标识（一章切分为若干 segment）。
        source_text: 待处理源文本（中文原文句段，或返工时的相关材料）。
        context: 上下文字典，承载跨节点信息，如 ``draft``、``review_notes``、
            ``glossary``、``rework_count`` 等，与 TranslationState 对齐。
    """

    work_id: str
    chapter_id: str
    segment_id: str
    source_text: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """Agent 执行结果。

    字段:
        agent: Agent 名（建议取子类 ``name`` 类属性，如 ``"translator"``）。
        ok: 是否成功；False 时调度方依据 ``notes`` 决定重试/降级/回退。
        output: 业务产物字典，键由各子类文档声明。
        notes: 附加说明（降级原因、解析失败、返工批注等），人可读。
    """

    agent: str
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# JSON 输出稳健提取工具
# ----------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def parse_json_output(text: str, notes: list[str] | None = None) -> dict | list | None:
    """从模型输出中稳健提取 JSON 对象/数组，失败返回 None。

    依次尝试：
        1. 全文直接 ``json.loads``；
        2. 提取 ```json ... ``` 围栏代码块后解析；
        3. 截取首个 ``{``/``[`` 到末个 ``}``/``]`` 的子串解析（容忍前后散文）。

    参数:
        text: 模型原始输出文本。
        notes: 可选的失败记录列表；解析失败时会 append 一条中文说明，
            便于调用方（BaseAgent 子类）把原因带入 ``AgentResult.notes``。

    返回:
        解析出的 ``dict`` 或 ``list``；全部尝试失败时返回 None。

    TODO: 支持流式半截 JSON 的修复（如补全括号）、对常见问题模型输出
    （单引号、尾逗号）的宽松解析。
    """
    if not text or not text.strip():
        if notes is not None:
            notes.append("JSON 解析失败：模型输出为空。")
        return None

    candidates: list[str] = [text.strip()]

    # 围栏代码块优先于裸文本之外的散文
    candidates.extend(m.group(1).strip() for m in _FENCE_RE.finditer(text))

    # 首尾括号截取（应对"好的，以下是 JSON：{...} 希望对你有帮助"式输出）
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if starts:
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            result = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(result, (dict, list)):
            return result

    if notes is not None:
        preview = text.strip().replace("\n", " ")[:120]
        notes.append(f"JSON 解析失败：未能从模型输出提取合法 JSON。输出摘要: {preview}")
    return None


# ----------------------------------------------------------------------
# Agent 抽象基类
# ----------------------------------------------------------------------
class BaseAgent(ABC):
    """所有翻译流水线 Agent 的抽象基类。

    子类约定见模块 docstring。骨架期仅提供公共能力与契约，
    具体业务逻辑在各子类 ``run`` 中实现。

    属性:
        llm: LLM 客户端（``mant.llm.client.LLMClient``）。
        memory: 记忆门面（``mant.memory.MemoryHub``），可为 None。
        name: Agent 名，默认取类名转小写；子类建议显式覆盖
            （如 ``name = "translator"``），写入 ``AgentResult.agent``。
    """

    #: 子类必须覆盖：角色系统提示词
    SYSTEM_PROMPT: str = ""

    name: str = "base"

    def __init__(self, llm: "LLMClient", memory: "MemoryHub | None" = None) -> None:
        self.llm = llm
        self.memory = memory

    @abstractmethod
    def run(self, task: AgentTask) -> AgentResult:
        """执行任务并返回结构化结果。

        实现要求：
            - 不抛出未捕获异常；失败时 ``ok=False`` 并在 ``notes`` 说明；
            - 真实模型不可用时（``[DRAFT]`` 占位响应）照常走完流程，
              把 LLMClient.last_notes 并入结果 notes，保证离线联调可跑通。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 公共辅助
    # ------------------------------------------------------------------
    def build_user_prompt(self, template: str, **slots: Any) -> str:
        """渲染用户提示词模板：``{槽位}`` 替换为传入值。

        缺失的槽位保留原样 ``{name}``（不抛 KeyError），并在渲染结果中
        可见，便于骨架期提示词尚未收敛时安全联调；多余槽位被忽略。

        参数:
            template: 含 ``{name}`` 槽位的模板字符串。
            **slots: 槽位值；非字符串值经 ``str()`` 转换。

        返回:
            渲染后的用户提示词。
        """

        class _Missing(dict):
            def __missing__(self, key: str) -> str:
                return "{" + key + "}"

        safe_slots = {k: str(v) for k, v in slots.items()}
        return template.format_map(_Missing(safe_slots))

    def parse_json_output(self, text: str, notes: list[str] | None = None) -> dict | list | None:
        """实例级包装：调用模块函数 ``parse_json_output`` 提取 JSON。

        失败时返回 None 并向 ``notes`` 记录原因；子类应随后按角色策略降级
        （如 QA 解析失败视为不达标并携带批注回退）。
        """
        return parse_json_output(text, notes=notes)

    def complete(self, user: str, *, temperature: float = 0.3, max_tokens: int = 4096) -> str:
        """便捷调用：以子类 ``SYSTEM_PROMPT`` 为系统提示词请求 LLM。"""
        return self.llm.complete(
            self.SYSTEM_PROMPT, user, temperature=temperature, max_tokens=max_tokens
        )

    def _result(
        self,
        ok: bool,
        output: dict[str, Any] | None = None,
        notes: list[str] | None = None,
    ) -> AgentResult:
        """构造 ``AgentResult`` 的辅助工厂，自动填入 ``agent`` 名。"""
        return AgentResult(agent=self.name, ok=ok, output=output or {}, notes=notes or [])
