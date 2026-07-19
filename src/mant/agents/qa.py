"""QA 终审 Agent（QAAgent）：四维评分与放行 / 返工裁决。

骨架级别实现：按 accuracy / fluency / terminology / style 四维 0-10 评分，
``qa_score`` 由代码按权重确定性计算（不直接信任模型自报的均分），
``qa_verdict`` 经阈值校验后给出，解析失败时安全默认为 rework。
"""

from __future__ import annotations

from typing import Any

from mant.agents.base import AgentResult, AgentTask, BaseAgent

__all__ = ["QAAgent"]


# ----------------------------------------------------------------------
# 输出规整 / 上下文渲染辅助
# ----------------------------------------------------------------------
def _clamp_score(value: Any) -> float | None:
    """把维度得分规整为 0-10 的 float；无法解析返回 None。"""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(10.0, score))


def _extract_qa_detail(parsed: Any) -> dict[str, Any] | None:
    """从 JSON 解析结果中提取并校验 QA 明细；结构不符返回 None。

    标准结构：四维得分 + verdict + suggestions；缺任一维度得分即视为无效。
    """
    if not isinstance(parsed, dict):
        return None
    detail: dict[str, Any] = {}
    for dim in ("accuracy", "fluency", "terminology", "style"):
        score = _clamp_score(parsed.get(dim))
        if score is None:
            return None
        detail[dim] = score
    verdict = str(parsed.get("verdict", "")).strip().lower()
    detail["verdict"] = verdict if verdict in ("pass", "rework") else ""
    suggestions = parsed.get("suggestions", [])
    if isinstance(suggestions, list):
        detail["suggestions"] = [str(s) for s in suggestions]
    elif suggestions:
        detail["suggestions"] = [str(suggestions)]
    else:
        detail["suggestions"] = []
    return detail


def _render_glossary(glossary: Any) -> str:
    """把 context 中的术语材料渲染为 Prompt 文本（缺省返回占位说明）。

    TODO: 与 translator.py / editor.py 中的同名辅助重复，待基础设施组
    抽到公共模块（如 mant.agents.prompts）后统一替换。
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


def _render_review_notes(review_notes: Any) -> str:
    """把审校意见列表渲染为 Prompt 文本；空列表返回空串。"""
    if not review_notes:
        return ""
    lines: list[str] = []
    for note in review_notes:
        if isinstance(note, dict):
            note_id = note.get("note_id", "?")
            severity = note.get("severity", "?")
            issue_type = note.get("issue_type", "?")
            suggestion = note.get("suggestion", "")
            span = note.get("span", "")
            resolution = note.get("resolution", "pending")
            lines.append(
                f"- [id={note_id}; {severity}/{issue_type}; state={resolution}] "
                f"{suggestion}（片段：{span}）"
            )
        else:
            lines.append(f"- {note}")
    return "\n".join(lines)


_HIGH_RISK_ISSUE_TYPES = {
    "omission",
    "mistranslation",
    "proper_noun",
    "accuracy",
    "terminology",
    "qa",
}
_RESOLVED_NOTE_STATES = {"translation_applied", "revision_applied", "resolved"}
_REVISION_QA_PENDING_STATES = {
    "revision_applied_pending_qa",
    "revision_no_change_pending_qa",
}


def _unresolved_high_risk_notes(review_notes: Any) -> list[dict[str, Any]]:
    """找出未进入翻译/定点修订的 high 事实性意见。"""
    if not isinstance(review_notes, list):
        return []
    unresolved: list[dict[str, Any]] = []
    for note in review_notes:
        if not isinstance(note, dict):
            continue
        severity = str(note.get("severity") or "").strip().lower()
        issue_type = str(note.get("issue_type") or "").strip().lower()
        resolution = str(note.get("resolution") or "pending").strip().lower()
        if (
            severity == "high"
            and issue_type in _HIGH_RISK_ISSUE_TYPES
            and resolution not in _RESOLVED_NOTE_STATES
            and resolution not in _REVISION_QA_PENDING_STATES
        ):
            unresolved.append(note)
    return unresolved


# ----------------------------------------------------------------------
# QA 终审 Agent
# ----------------------------------------------------------------------
class QAAgent(BaseAgent):
    """QA 终审 Agent：四维评分 + pass / rework 裁决。

    - 模型档位：``strong``（终审结论直接驱动 LangGraph 的回退回环）。
    - 输入：``task.source_text`` 原文；``task.context["polished"]``（优先）
      或 ``task.context["draft"]`` 待终审译文；``glossary`` /
      ``review_notes`` 可选。
    - 输出：``AgentResult.output = {"qa_score": float, "qa_verdict": str,
      "qa_detail": dict}``；``qa_detail`` 含四维得分、verdict、suggestions。
    - 裁决确定性：``qa_score`` 由代码按权重计算；阈值为放行必要条件，模型
      明确 ``rework`` 或未落实 high 事实性意见时不得被代码覆盖为 pass。
    """

    name: str = "qa"
    #: 期望模型档位，供调度 / 工厂用 ``LLMClient.with_tier`` 挑选客户端
    tier: str = "strong"
    #: 采样温度：评分要求低发散、可复现
    temperature: float = 0.1
    #: 单次补全最大 token 数；schema 很小，限制冗长推理与失控输出。
    max_tokens: int = 768
    #: 请求 OpenAI 兼容端点启用 JSON object 模式；可由角色配置关闭。
    structured_json: bool = True
    #: 首次 JSON 无效时，只做一次短修复，避免反复产生高额调用。
    repair_attempts: int = 1
    repair_max_tokens: int = 384
    thinking: str | None = None

    #: 四维权重（和为 1.0）：忠实度权重最高
    DIMENSION_WEIGHTS: dict[str, float] = {
        "accuracy": 0.4,
        "fluency": 0.2,
        "terminology": 0.2,
        "style": 0.2,
    }
    #: 放行阈值：加权总分 >= PASS_SCORE_THRESHOLD 且各维度 >= MIN_DIMENSION_SCORE
    PASS_SCORE_THRESHOLD: float = 7.0
    MIN_DIMENSION_SCORE: float = 6.0
    #: 实例级阈值可由 agents.qa 配置注入；保留上方常量兼容外部引用。
    pass_score_threshold: float = PASS_SCORE_THRESHOLD
    min_dimension_score: float = MIN_DIMENSION_SCORE

    # 系统提示词：定义终审角色、评分维度、裁决规则与 JSON 输出 schema。
    # 不含槽位（直接作为 system 传入），因此可以安全包含 JSON 花括号示例。
    SYSTEM_PROMPT: str = """你是一名资深翻译质量终审专家，为网络小说译文的发布质量把关。

【评分维度】（每项 0-10，可给一位小数）
- accuracy 忠实度：是否漏译 / 误译，剧情事实与设定是否正确。
- fluency 流畅度：英文是否地道自然，语法、搭配是否正确。
- terminology 术语一致性：专名与术语是否全书统一、与术语表一致。
- style 文风：是否符合网文节奏与本书文风，阅读体验是否流畅。

【裁决规则】
- verdict 只能是 "pass" 或 "rework"。
- 总分 = accuracy×0.4 + fluency×0.2 + terminology×0.2 + style×0.2；
  总分与各维度必须达到本次运行给出的阈值方可判 "pass"，否则判 "rework"。
- 判 "rework" 时必须给出具体、可执行的返工建议（指出问题位置与修改方向）。
- state=revision_applied_pending_qa 或 revision_no_change_pending_qa 的审校意见
  必须对照原文和当前译文逐条验证；只有确已落实时才可判 pass。

【输出要求】
只输出一个紧凑 JSON 对象，不要解释、复述原文、输出代码块或思考过程。
suggestions 最多 3 条，每条不超过 120 个汉字。格式如下：
{
  "accuracy": 8.0,
  "fluency": 8.5,
  "terminology": 9.0,
  "style": 8.0,
  "verdict": "pass",
  "suggestions": ["返工建议（中文，逐条具体可执行）；判 pass 时可为空数组"]
}"""

    JSON_REPAIR_SYSTEM_PROMPT: str = """你是 JSON 修复器。只输出一个合法、紧凑的 JSON 对象。
保留输入中的四项分数、verdict 和 suggestions；禁止解释、补写分析或使用代码块。"""

    #: 用户提示词模板：{source_text} 原文 / {polished} 待终审译文 /
    #: {glossary} 术语表 / {review_notes} 审校意见
    USER_PROMPT_TEMPLATE: str = """【原文】
{source_text}

【待终审译文】
{polished}

【术语表】
{glossary}

【审校意见（如有）】
{review_notes}

请按四个维度评分，并按要求只输出 JSON。"""

    def run(self, task: AgentTask) -> AgentResult:
        """执行终审：渲染 Prompt → 调用 LLM → 解析 JSON → 代码侧裁决。

        约定：
            - 不抛出未捕获异常；失败 / 解析异常时安全默认判 rework，
              原因写入 ``notes``（由 LangGraph 回退节点消费）；
            - 未配置真实模型时占位响应无法解析为 JSON，同样走安全降级。
        """
        notes: list[str] = []

        # 1) 读取输入：优先终审润色稿，缺润色稿时退回初稿（兼容未接润色节点的联调）
        polished = str(
            task.context.get("polished") or task.context.get("draft") or ""
        ).strip()
        if not polished:
            notes.append("task.context 缺少 polished/draft（待终审译文），QA 跳过并默认 rework")
            return self._result(
                ok=False,
                output={"qa_score": 0.0, "qa_verdict": "rework", "qa_detail": {}},
                notes=notes,
            )
        glossary_text = _render_glossary(task.context.get("glossary"))
        review_notes_text = _render_review_notes(task.context.get("review_notes")) or "（无）"

        # 2) 渲染 Prompt；实际阈值显式写入系统提示，避免配置与模型规则漂移。
        system = (
            self.SYSTEM_PROMPT
            + "\n\n【本次放行阈值】总分 >= "
            + f"{self.pass_score_threshold:g}，且四个维度均 >= "
            + f"{self.min_dimension_score:g}。阈值只是必要条件；发现严重遗漏时仍须判 rework。"
        )
        user = self.build_user_prompt(
            self.USER_PROMPT_TEMPLATE,
            source_text=task.source_text,
            polished=polished,
            glossary=glossary_text,
            review_notes=review_notes_text,
        )

        # 3) 调用 LLM
        try:
            raw = self.llm.complete(
                system,
                user,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format=(
                    {"type": "json_object"} if self.structured_json else None
                ),
                thinking=self.thinking,
            )
        except Exception as exc:  # noqa: BLE001 —— 骨架期统一降级，不向上抛
            notes.append(f"LLM 调用失败：{exc!r}")
            return self._result(
                ok=False,
                output={"qa_score": 0.0, "qa_verdict": "rework", "qa_detail": {}},
                notes=notes,
            )
        # 合并客户端侧说明（降级原因 / 重试记录等）
        notes.extend(self.llm.last_notes)

        # 4) 解析 JSON 输出；parse_json_output 失败时返回 None 并自动记录 notes
        parsed = self.parse_json_output(raw, notes)
        if parsed is None and not raw.lstrip().startswith("[DRAFT]"):
            for attempt in range(max(0, int(self.repair_attempts))):
                repair_user = (
                    "将以下内容修复为要求的 QA JSON。只保留 accuracy、fluency、"
                    "terminology、style、verdict、suggestions 六个字段：\n"
                    + raw[-6000:]
                )
                try:
                    repaired = self.llm.complete(
                        self.JSON_REPAIR_SYSTEM_PROMPT,
                        repair_user,
                        temperature=0.0,
                        max_tokens=max(64, int(self.repair_max_tokens)),
                        response_format=(
                            {"type": "json_object"}
                            if self.structured_json
                            else None
                        ),
                        thinking=self.thinking,
                    )
                except Exception as exc:  # noqa: BLE001 - 修复失败仍安全判返工
                    notes.append(f"QA JSON 第 {attempt + 1} 次修复调用失败：{exc!r}")
                    break
                notes.extend(self.llm.last_notes)
                parsed = self.parse_json_output(repaired, notes)
                if parsed is not None:
                    notes.append(f"QA JSON 已在第 {attempt + 1} 次短修复后恢复")
                    break
        detail = _extract_qa_detail(parsed)
        if detail is None:
            notes.append("QA 评分结构不符合 schema，安全默认判 rework")
            fallback_detail = {
                "verdict": "rework",
                "suggestions": ["QA 输出无法解析；请重新核对原文忠实度、术语与语言流畅度。"],
            }
            return self._result(
                ok=False,
                output={
                    "qa_score": 0.0,
                    "qa_verdict": "rework",
                    "qa_detail": fallback_detail,
                },
                notes=notes,
            )

        # 5) 代码侧确定性计算总分（不直接信任模型自报的均分）
        qa_score = round(
            sum(detail[dim] * weight for dim, weight in self.DIMENSION_WEIGHTS.items()),
            2,
        )

        # 6) 保守放行：阈值只是必要条件。模型明确判 rework，或仍有未进入
        #    修订流程的 high 事实性意见时，代码不得把它覆盖为 pass。
        llm_verdict = detail.get("verdict", "")
        unresolved = _unresolved_high_risk_notes(
            task.context.get("review_notes") or []
        )
        threshold_pass = (
            qa_score >= self.pass_score_threshold
            and min(detail[dim] for dim in self.DIMENSION_WEIGHTS)
            >= self.min_dimension_score
        )
        verdict = (
            "pass"
            if threshold_pass and llm_verdict != "rework" and not unresolved
            else "rework"
        )
        if llm_verdict and llm_verdict != verdict:
            notes.append(
                f"模型 verdict={llm_verdict!r} 与保守裁决 {verdict!r} 不一致，"
                "采用保守裁决"
            )
        if unresolved:
            notes.append(f"仍有 {len(unresolved)} 条 high 事实性意见未进入修订，禁止放行")
            detail.setdefault("suggestions", []).insert(
                0, "存在尚未落实的高严重度漏译/误译/专名问题，请先完成定点修订。"
            )
        detail["verdict"] = verdict
        if verdict == "rework" and not detail.get("suggestions"):
            detail["suggestions"] = ["（模型未给出具体返工建议，请人工复核）"]
            notes.append("判 rework 但模型未提供返工建议，已填占位建议")
        detail["suggestions"] = [
            str(item)[:120] for item in (detail.get("suggestions") or [])[:3]
        ]

        # TODO: qa_score / qa_detail 写入实验日志，供 M2 单 Agent 基线对照分析
        return self._result(
            ok=True,
            output={
                "qa_score": qa_score,
                "qa_verdict": verdict,
                "qa_detail": detail,
            },
            notes=notes,
        )
