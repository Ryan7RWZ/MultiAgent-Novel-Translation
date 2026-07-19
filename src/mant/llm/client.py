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
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

from mant.observability import emit_event

# 占位响应前缀：未配置真实模型时所有输出都带此前缀，便于下游识别"草稿"
DRAFT_PREFIX = "[DRAFT]"

# 支持的模型档位
TIER_FAST = "fast"
TIER_STRONG = "strong"
SUPPORTED_TIERS = (TIER_FAST, TIER_STRONG)

# 重试默认值（指数退避 stub 用）
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 1.0  # 秒，第 n 次重试等待 base * 2**n
_CHAT_OVERHEAD_TOKEN_RESERVE = 64


class LLMBudgetExceeded(RuntimeError):
    """真实供应商请求在发出前超过共享硬预算。"""


@dataclass
class _RequestBudget:
    """跨 tier/worker 共享的保守请求预算。"""

    max_requests: int = 0
    max_reserved_tokens: int = 0
    _requests: int = 0
    _reserved_tokens: int = 0
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.max_requests < 0:
            raise ValueError("llm.budget.max_requests 不能小于 0")
        if self.max_reserved_tokens < 0:
            raise ValueError("llm.budget.max_reserved_tokens 不能小于 0")

    def reserve(self, system: str, user: str, max_tokens: int) -> dict[str, int]:
        # 供应商 tokenizer 未知时，以 UTF-8 字节数保守预留 prompt token；再为
        # chat 消息封装留固定余量。completion 按请求 max_tokens 全额预留。
        requested_tokens = (
            len(system.encode("utf-8"))
            + len(user.encode("utf-8"))
            + _CHAT_OVERHEAD_TOKEN_RESERVE
            + max(0, int(max_tokens))
        )
        with self._lock:
            next_requests = self._requests + 1
            next_tokens = self._reserved_tokens + requested_tokens
            if self.max_requests > 0 and next_requests > self.max_requests:
                raise LLMBudgetExceeded(
                    f"供应商请求数将超过硬上限 {self.max_requests}"
                )
            if (
                self.max_reserved_tokens > 0
                and next_tokens > self.max_reserved_tokens
            ):
                raise LLMBudgetExceeded(
                    "供应商请求的保守 token 预留将超过硬上限 "
                    f"{self.max_reserved_tokens}"
                )
            self._requests = next_requests
            self._reserved_tokens = next_tokens
            return self.snapshot()

    def snapshot(self) -> dict[str, int]:
        return {
            "requests": self._requests,
            "reserved_tokens": self._reserved_tokens,
            "max_requests": self.max_requests,
            "max_reserved_tokens": self.max_reserved_tokens,
        }


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

    def semantic_config(self) -> dict[str, Any]:
        """返回影响模型输出、但不包含密钥值的可持久化配置。"""
        safe_base_url = ""
        if self.base_url:
            parsed = urlsplit(self.base_url)
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            safe_base_url = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        return {
            "model": self.model,
            "base_url": safe_base_url,
            "api_key_env": self.api_key_env or "",
            "timeout": float(self.timeout),
            "max_retries": int(self.max_retries),
            "partial_retries": int(self.extra.get("partial_retries", 1) or 0),
            "stream_include_usage": bool(
                self.extra.get("stream_include_usage", False)
            ),
        }


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
        request_budget: _RequestBudget | None = None,
    ) -> None:
        if tier not in SUPPORTED_TIERS:
            raise ValueError(f"未知模型档位: {tier!r}，仅支持 {SUPPORTED_TIERS}")
        self._providers = providers
        self.tier = tier
        self.default_tier = default_tier
        self._request_budget = request_budget or _RequestBudget()
        # token 用量累计
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._last_notes: list[str] = []
        self._last_call_incomplete = False
        self._last_call_id = ""

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
        budget_raw = dict(llm_cfg.get("budget") or {})
        request_budget = _RequestBudget(
            max_requests=int(budget_raw.get("max_requests", 0) or 0),
            max_reserved_tokens=int(
                budget_raw.get("max_reserved_tokens", 0) or 0
            ),
        )
        return cls(
            providers=providers,
            tier=tier or default_tier,
            default_tier=default_tier,
            request_budget=request_budget,
        )

    def with_tier(self, tier: str) -> "LLMClient":
        """返回另一档位客户端；调用状态独立，但真实请求硬预算全局共享。"""
        return LLMClient(
            providers=self._providers,
            tier=tier,
            default_tier=self.default_tier,
            request_budget=self._request_budget,
        )

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

    def semantic_config(self, *, include_all_tiers: bool = False) -> dict[str, Any]:
        """返回 checkpoint/manifest 使用的脱敏模型语义配置。"""
        if include_all_tiers:
            providers = {
                name: provider.semantic_config()
                for name, provider in sorted(self._providers.items())
            }
        else:
            providers = {self.tier: self.provider.semantic_config()}
        return {
            "tier": self.tier,
            "default_tier": self.default_tier,
            "providers": providers,
            "request_budget": {
                "max_requests": self._request_budget.max_requests,
                "max_reserved_tokens": self._request_budget.max_reserved_tokens,
            },
        }

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
    def request_budget_usage(self) -> dict[str, int]:
        """跨 tier/worker 共享的供应商请求与保守 token 预留。"""
        with self._request_budget._lock:
            return self._request_budget.snapshot()

    @property
    def last_notes(self) -> list[str]:
        """最近一次 ``complete`` 调用的说明（降级原因 / 重试记录等）。"""
        return list(self._last_notes)

    @property
    def last_call_incomplete(self) -> bool:
        """最近一次调用是否因流中断或输出上限只得到不完整文本。"""
        return self._last_call_incomplete

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
        response_format: dict[str, Any] | None = None,
        thinking: str | None = None,
    ) -> str:
        """执行一次 chat completion，兼容性地收集 ``stream_complete`` 全部增量。

        降级策略（不抛异常）：
            - 未安装 ``openai`` SDK → 返回 ``[DRAFT]`` 占位响应并记录 notes；
            - 未配置 API key → 同上；
            - 请求重试耗尽 → 同上。
            - 已产生文本后中断或被长度上限截断 → 丢弃残稿并完整重试；
              完整重试耗尽时返回空字符串，交由片段级工作流处理。

        参数:
            system: 系统提示词（各 Agent 的 ``SYSTEM_PROMPT``）。
            user: 用户提示词（经 ``build_user_prompt`` 格式化后的槽位文本）。
            temperature: 采样温度。
            max_tokens: 最大输出 token 数。

        返回:
            模型输出文本；降级时为 ``[DRAFT]`` 前缀的占位文本。
        """
        # ``stream_complete`` 已把增量发往观测层，但 complete() 尚未把它交给
        # 业务 Agent，因此可以安全丢弃半截结果并从头重试，避免残稿进入 state。
        partial_retries = max(
            0, int(self.provider.extra.get("partial_retries", 1) or 0)
        )
        accumulated_notes: list[str] = []
        for retry_index in range(partial_retries + 1):
            text = "".join(
                self.stream_complete(
                    system,
                    user,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    thinking=thinking,
                )
            )
            accumulated_notes.extend(self._last_notes)
            if not self._last_call_incomplete:
                self._last_notes = accumulated_notes
                return text
            if retry_index < partial_retries:
                note = (
                    f"检测到不完整输出，已丢弃 {len(text)} 字符并进行第 "
                    f"{retry_index + 1} 次完整重试。"
                )
                accumulated_notes.append(note)
                emit_event(
                    "llm.retry",
                    tier=self.tier,
                    payload={
                        "call_id": self._last_call_id,
                        "attempt": retry_index + 1,
                        "error": "IncompleteOutput",
                        "discarded_chars": len(text),
                        "wait_seconds": 0,
                    },
                )
        accumulated_notes.append("不完整输出重试耗尽，已丢弃残稿并返回空结果。")
        self._last_notes = accumulated_notes
        return ""

    def stream_complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
        thinking: str | None = None,
    ) -> Iterator[str]:
        """执行真正的流式 chat completion，逐段产出模型文本。

        ``complete`` 是本方法的收集器，因此所有现有 Agent 无需改接口即可
        获得底层流式传输。本方法不会在已经产出文本后原地重试；收集器可丢弃
        该次全部残稿后发起新的完整调用，绝不拼接两次输出。首 token 前失败仍
        按配置退避重试。
        """
        self._last_notes = []
        self._last_call_incomplete = False
        if thinking not in {None, "", "enabled", "disabled"}:
            raise ValueError("thinking 只能为 enabled、disabled 或 None")
        cfg = self.provider
        call_id = uuid.uuid4().hex[:12]
        self._last_call_id = call_id
        started = time.perf_counter()
        start_prompt_tokens = self._prompt_tokens
        start_completion_tokens = self._completion_tokens
        emit_event(
            "llm.started",
            tier=self.tier,
            payload={
                "call_id": call_id,
                "model": cfg.model,
                "system_chars": len(system),
                "user_chars": len(user),
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": str(response_format or ""),
                "thinking": thinking or "provider_default",
            },
        )

        client = self._build_openai_client(cfg)
        if client is None:
            draft = self._draft_response(system, user)
            emit_event(
                "llm.fallback",
                tier=self.tier,
                payload={"call_id": call_id, "reason": "provider_unavailable"},
            )
            emit_event(
                "llm.token",
                tier=self.tier,
                payload={"call_id": call_id, "delta": draft},
            )
            yield draft
            emit_event(
                "llm.completed",
                tier=self.tier,
                payload={"call_id": call_id, "model": cfg.model, "fallback": True},
                metrics={
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "output_chars": len(draft),
                },
            )
            return

        attempts = max(0, int(cfg.max_retries)) + 1
        output_chars = 0
        for attempt in range(attempts):
            emitted_this_attempt = False
            finish_reason: str | None = None
            stream = None
            try:
                request: dict[str, Any] = {
                    "model": cfg.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "timeout": cfg.timeout,
                    "stream": True,
                }
                if response_format:
                    request["response_format"] = dict(response_format)
                if thinking:
                    request["extra_body"] = {"thinking": {"type": thinking}}
                if bool(cfg.extra.get("stream_include_usage", False)):
                    request["stream_options"] = {"include_usage": True}
                # 放在 SDK 请求前，计入供应商级重试和 complete() 的残稿重试；
                # 无 key / 无 SDK 的本地 DRAFT 降级不会消耗真实请求预算。
                self._request_budget.reserve(system, user, max_tokens)
                stream = client.chat.completions.create(**request)
                for chunk in stream:
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        self._prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                        self._completion_tokens += (
                            getattr(usage, "completion_tokens", 0) or 0
                        )
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    choice = choices[0]
                    chunk_finish_reason = getattr(choice, "finish_reason", None)
                    if chunk_finish_reason:
                        finish_reason = str(chunk_finish_reason)
                    delta = getattr(getattr(choice, "delta", None), "content", None)
                    if not delta:
                        continue
                    if not isinstance(delta, str):
                        delta = str(delta)
                    emitted_this_attempt = True
                    output_chars += len(delta)
                    emit_event(
                        "llm.token",
                        tier=self.tier,
                        payload={"call_id": call_id, "delta": delta},
                    )
                    yield delta
                if finish_reason == "length":
                    self._last_call_incomplete = True
                    self._last_notes.append(
                        f"模型输出达到 max_tokens={max_tokens} 上限，结果不完整。"
                    )
                    emit_event(
                        "llm.failed",
                        tier=self.tier,
                        payload={
                            "call_id": call_id,
                            "error": "OutputTruncated",
                            "partial": bool(output_chars),
                        },
                        metrics={
                            "duration_ms": round(
                                (time.perf_counter() - started) * 1000, 2
                            ),
                            "output_chars": output_chars,
                            "prompt_tokens": (
                                self._prompt_tokens - start_prompt_tokens
                            ),
                            "completion_tokens": (
                                self._completion_tokens - start_completion_tokens
                            ),
                        },
                    )
                    return
                emit_event(
                    "llm.completed",
                    tier=self.tier,
                    payload={
                        "call_id": call_id,
                        "model": cfg.model,
                        "fallback": False,
                        "attempt": attempt + 1,
                    },
                    metrics={
                        "duration_ms": round(
                            (time.perf_counter() - started) * 1000, 2
                        ),
                        "output_chars": output_chars,
                        "prompt_tokens": self._prompt_tokens - start_prompt_tokens,
                        "completion_tokens": (
                            self._completion_tokens - start_completion_tokens
                        ),
                    },
                )
                return
            except LLMBudgetExceeded as exc:
                self._last_notes.append(f"LLM 硬预算已耗尽：{exc}")
                emit_event(
                    "budget.exhausted",
                    tier=self.tier,
                    payload={
                        "scope": "llm_provider_request",
                        **self.request_budget_usage,
                    },
                )
                raise
            except Exception as exc:  # noqa: BLE001 - 统一降级，不向上抛
                if emitted_this_attempt:
                    self._last_call_incomplete = True
                    note = (
                        f"流式输出已开始后调用中断（{type(exc).__name__}），"
                        "本次残稿已标记为不完整，等待收集器决定完整重试。"
                    )
                    self._last_notes.append(note)
                    emit_event(
                        "llm.failed",
                        tier=self.tier,
                        payload={
                            "call_id": call_id,
                            "error": type(exc).__name__,
                            "partial": True,
                        },
                        metrics={"output_chars": output_chars},
                    )
                    return
                if attempt + 1 < attempts:
                    wait = _DEFAULT_BACKOFF_BASE * (2**attempt)
                    self._last_notes.append(
                        f"第 {attempt + 1} 次调用失败（{type(exc).__name__}），"
                        f"{wait:.1f}s 后重试。"
                    )
                    emit_event(
                        "llm.retry",
                        tier=self.tier,
                        payload={
                            "call_id": call_id,
                            "attempt": attempt + 1,
                            "error": type(exc).__name__,
                            "wait_seconds": wait,
                        },
                    )
                    time.sleep(wait)
                    continue
                self._last_notes.append(
                    f"重试 {cfg.max_retries} 次后仍失败，降级为占位响应: {exc!r}"
                )
                emit_event(
                    "llm.failed",
                    tier=self.tier,
                    payload={
                        "call_id": call_id,
                        "error": type(exc).__name__,
                        "partial": False,
                    },
                )
                draft = self._draft_response(system, user)
                emit_event(
                    "llm.fallback",
                    tier=self.tier,
                    payload={"call_id": call_id, "reason": "retries_exhausted"},
                )
                emit_event(
                    "llm.token",
                    tier=self.tier,
                    payload={"call_id": call_id, "delta": draft},
                )
                yield draft
                emit_event(
                    "llm.completed",
                    tier=self.tier,
                    payload={"call_id": call_id, "model": cfg.model, "fallback": True},
                    metrics={
                        "duration_ms": round(
                            (time.perf_counter() - started) * 1000, 2
                        ),
                        "output_chars": len(draft),
                    },
                )
                return
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as exc:  # noqa: BLE001 - 关闭流不覆盖业务结果
                        self._last_notes.append(
                            f"关闭流式响应时出现 {type(exc).__name__}，已忽略。"
                        )

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

        # SDK 内建重试关闭，避免与本类的可观测退避重试叠加。
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": cfg.timeout,
            "max_retries": 0,
        }
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
