"""LLM 客户端（OpenAI 兼容协议）。

本模块提供统一的 LLM 调用门面 ``LLMClient``，供各 Agent（术语/翻译/审校/润色/QA）
通过 ``mant.agents.base.BaseAgent`` 持有并使用。

设计约定（全项目统一，勿重复定义）：
    - ``LLMClient.from_config(cfg: dict) -> LLMClient``
    - ``complete(system: str, user: str, *, temperature: float = 0.3,
      max_tokens: int = 4096) -> str``
    - 配置分 fast / strong 两档模型（见 ``config/settings.example.yaml`` 的
      ``llm.providers.*``），fast 用于高频轻量调用，strong 用于审校/终审等
      高质量场景。
    - 通过 ``base_url`` 配置即可接入任意 OpenAI 兼容服务（DeepSeek / Qwen /
      本地 vLLM 等）。
    - 未安装 ``openai`` SDK 或未配置 API key 时，不抛异常，返回 ``[DRAFT]``
      前缀的占位响应，并在 ``last_notes`` 中说明原因，保证离线/骨架环境可跑通。

第三方依赖规则：
    ``openai`` 必须在函数内延迟导入；仅用 stdlib（+numpy）的环境下
    ``import mant.llm.client`` 必须成功。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

# 占位响应前缀：未配置真实模型时所有输出都带此前缀，便于下游识别"草稿"
DRAFT_PREFIX = "[DRAFT]"

# 支持的模型档位
TIER_FAST = "fast"
TIER_STRONG = "strong"
SUPPORTED_TIERS = (TIER_FAST, TIER_STRONG)

# 重试默认值（指数退避 stub 用）
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 1.0  # 秒，第 n 次重试等待 base * 2**n


@dataclass
class ProviderConfig:
    """单个模型档位的配置（对应 ``llm.providers.<tier>``）。

    字段:
        model: 模型名，如 ``deepseek-chat`` / ``qwen-plus`` / ``gpt-4o-mini``。
        base_url: OpenAI 兼容端点；留空则走 openai 默认（官方 API）。
        api_key: 明文 key（不推荐提交到仓库）。
        api_key_env: 环境变量名，优先于 ``api_key`` 读取（推荐方式）。
        timeout: 单次请求超时秒数。
        max_retries: 失败重试次数（指数退避）。
        extra: 其他透传字段（如 organization、默认 headers 等），TODO 备用。
    """

    model: str = ""
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    timeout: float = 60.0
    max_retries: int = _DEFAULT_MAX_RETRIES
    extra: dict[str, Any] = field(default_factory=dict)

    def resolve_api_key(self) -> str | None:
        """解析 API key：环境变量优先，其次明文配置。"""
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if key:
                return key
        return self.api_key


class LLMClient:
    """OpenAI 兼容协议的 LLM 客户端（骨架实现）。

    用法::

        cfg = {...}  # 整个 settings 字典，或其 "llm" 子节
        client = LLMClient.from_config(cfg)                 # 默认 fast 档
        strong = LLMClient.from_config(cfg, tier="strong")  # strong 档
        text = client.complete(system="你是翻译……", user="原文：……")

    属性:
        tier: 当前档位（``fast`` / ``strong``）。
        model: 当前模型名。
        total_prompt_tokens / total_completion_tokens / total_tokens:
            token 用量累计统计（真实调用成功后累加；占位响应不计）。
        last_notes: 最近一次调用的说明列表（降级原因、重试记录等）。
    """

    def __init__(
        self,
        providers: dict[str, ProviderConfig],
        tier: str = TIER_FAST,
        default_tier: str = TIER_FAST,
    ) -> None:
        if tier not in SUPPORTED_TIERS:
            raise ValueError(f"未知模型档位: {tier!r}，仅支持 {SUPPORTED_TIERS}")
        self._providers = providers
        self.tier = tier
        self.default_tier = default_tier
        # token 用量累计
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._last_notes: list[str] = []

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: dict, tier: str | None = None) -> "LLMClient":
        """从配置字典构造客户端。

        参数:
            cfg: 完整的 settings 字典（含 ``llm`` 键），或直接是 ``llm`` 子节；
                两种形态都接受。期望结构::

                    llm:
                      default_tier: fast            # 可选
                      providers:
                        fast:
                          model: deepseek-chat
                          base_url: https://api.deepseek.com
                          api_key_env: DEEPSEEK_API_KEY
                        strong:
                          model: qwen-max
                          base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
                          api_key_env: DASHSCOPE_API_KEY

            tier: 指定档位；缺省时读 ``llm.default_tier``，再缺省为 ``fast``。

        返回:
            ``LLMClient`` 实例。配置缺失的档位会以空 ``ProviderConfig`` 占位，
            调用时走 ``[DRAFT]`` 降级而不是构造期报错。
        """
        llm_cfg = cfg.get("llm", cfg) if isinstance(cfg, dict) else {}
        providers_raw = llm_cfg.get("providers", {}) or {}
        providers: dict[str, ProviderConfig] = {}
        for name in SUPPORTED_TIERS:
            raw = providers_raw.get(name) or {}
            known = {k: v for k, v in raw.items() if k in {
                "model", "base_url", "api_key", "api_key_env", "timeout", "max_retries",
            }}
            extra = {k: v for k, v in raw.items() if k not in known}
            providers[name] = ProviderConfig(extra=extra, **known)
        default_tier = llm_cfg.get("default_tier", TIER_FAST)
        if default_tier not in SUPPORTED_TIERS:
            default_tier = TIER_FAST
        return cls(providers=providers, tier=tier or default_tier, default_tier=default_tier)

    def with_tier(self, tier: str) -> "LLMClient":
        """返回同一组配置下另一档位的新客户端（token 统计独立）。"""
        return LLMClient(providers=self._providers, tier=tier, default_tier=self.default_tier)

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def provider(self) -> ProviderConfig:
        """当前档位的配置。"""
        return self._providers[self.tier]

    @property
    def model(self) -> str:
        """当前档位的模型名。"""
        return self.provider.model

    @property
    def total_prompt_tokens(self) -> int:
        """累计 prompt token 数。"""
        return self._prompt_tokens

    @property
    def total_completion_tokens(self) -> int:
        """累计 completion token 数。"""
        return self._completion_tokens

    @property
    def total_tokens(self) -> int:
        """累计 token 总数（prompt + completion）。"""
        return self._prompt_tokens + self._completion_tokens

    @property
    def usage(self) -> dict[str, int]:
        """token 用量汇总字典，便于写入实验日志。"""
        return {
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "total_tokens": self.total_tokens,
        }

    @property
    def last_notes(self) -> list[str]:
        """最近一次 ``complete`` 调用的说明（降级原因 / 重试记录等）。"""
        return list(self._last_notes)

    # ------------------------------------------------------------------
    # 核心调用
    # ------------------------------------------------------------------
    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        """执行一次 chat completion，返回文本结果。

        降级策略（不抛异常）：
            - 未安装 ``openai`` SDK → 返回 ``[DRAFT]`` 占位响应并记录 notes；
            - 未配置 API key → 同上；
            - 请求重试耗尽 → 同上。

        参数:
            system: 系统提示词（各 Agent 的 ``SYSTEM_PROMPT``）。
            user: 用户提示词（经 ``build_user_prompt`` 格式化后的槽位文本）。
            temperature: 采样温度。
            max_tokens: 最大输出 token 数。

        返回:
            模型输出文本；降级时为 ``[DRAFT]`` 前缀的占位文本。
        """
        self._last_notes = []
        cfg = self.provider

        client = self._build_openai_client(cfg)
        if client is None:
            # _build_openai_client 已把原因写入 _last_notes
            return self._draft_response(system, user)

        def _call() -> str:
            resp = client.chat.completions.create(
                model=cfg.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=cfg.timeout,
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self._prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                self._completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            return resp.choices[0].message.content or ""

        try:
            return self._retry_with_backoff(_call, max_retries=cfg.max_retries)
        except Exception as exc:  # noqa: BLE001 - 骨架期统一降级，不向上抛
            self._last_notes.append(f"重试 {cfg.max_retries} 次后仍失败，降级为占位响应: {exc!r}")
            return self._draft_response(system, user)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _build_openai_client(self, cfg: ProviderConfig) -> Any | None:
        """延迟导入 openai 并构造 SDK 客户端；不可用时记录原因并返回 None。"""
        try:
            from openai import OpenAI  # 函数内延迟导入，保证无 SDK 环境可 import 本模块
        except ImportError:
            self._last_notes.append(
                "未安装 openai SDK，无法真实调用；请执行 `pip install openai` 后重试。"
            )
            return None

        api_key = cfg.resolve_api_key()
        if not api_key:
            self._last_notes.append(
                f"未配置 API key（档位 {self.tier}，期望环境变量 "
                f"{cfg.api_key_env or '<未指定 api_key_env>'}），使用占位响应。"
            )
            return None
        if not cfg.model:
            self._last_notes.append(f"档位 {self.tier} 未配置 model，使用占位响应。")
            return None

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": cfg.timeout}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url  # 接入 DeepSeek/Qwen 等兼容端点
        # TODO: 按需透传 organization / default_headers 等 extra 字段
        return OpenAI(**kwargs)

    def _retry_with_backoff(self, fn, max_retries: int = _DEFAULT_MAX_RETRIES):
        """指数退避重试 stub：第 n 次失败 sleep base * 2**n 秒后重试。

        TODO(M1 后): 引入抖动（jitter）、区分可重试错误（限流/超时）与
        不可重试错误（鉴权/参数错误）、接入熔断与结构化日志。
        """
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - stub 期统一重试
                last_exc = exc
                if attempt >= max_retries:
                    break
                wait = _DEFAULT_BACKOFF_BASE * (2 ** attempt)
                self._last_notes.append(
                    f"第 {attempt + 1} 次调用失败（{type(exc).__name__}），{wait:.1f}s 后重试。"
                )
                time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    def _draft_response(self, system: str, user: str) -> str:
        """生成占位响应：保留输入片段，便于联调时核对提示词链路。"""
        user_preview = user.strip().replace("\n", " ")[:120]
        self._last_notes.append("已返回 [DRAFT] 占位响应（未进行真实模型调用）。")
        return f"{DRAFT_PREFIX} 模型未实际调用。system 长度={len(system)}，user 摘要: {user_preview}"
