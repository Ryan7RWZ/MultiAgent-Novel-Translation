"""mant.llm — LLM 接入层。

对外统一暴露 ``LLMClient``（OpenAI 兼容协议，fast/strong 双档）。
各 Agent 不直接依赖 openai SDK，一律通过本包获取客户端::

    from mant.llm import LLMClient

    client = LLMClient.from_config(cfg)                 # fast 档
    strong = LLMClient.from_config(cfg, tier="strong")  # strong 档
"""

from mant.llm.client import (
    DRAFT_PREFIX,
    SUPPORTED_TIERS,
    TIER_FAST,
    TIER_STRONG,
    LLMClient,
    ProviderConfig,
)

__all__ = [
    "DRAFT_PREFIX",
    "SUPPORTED_TIERS",
    "TIER_FAST",
    "TIER_STRONG",
    "LLMClient",
    "ProviderConfig",
]
