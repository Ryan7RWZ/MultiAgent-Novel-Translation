"""M2 单 Agent 基线包（多智能体翻译系统的实验对照组）。

导出 ``BaselineTranslator`` 与基线 Prompt 模板。本包仅依赖 stdlib，
保证仅 stdlib(+numpy) 环境下 ``import mant.baseline`` 必然成功。
"""

from .translate import (
    BASELINE_SYSTEM_PROMPT_TEMPLATE,
    BASELINE_USER_PROMPT_TEMPLATE,
    BaselineTranslator,
)

__all__ = [
    "BaselineTranslator",
    "BASELINE_SYSTEM_PROMPT_TEMPLATE",
    "BASELINE_USER_PROMPT_TEMPLATE",
]
