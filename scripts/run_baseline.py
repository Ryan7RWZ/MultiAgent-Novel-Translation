#!/usr/bin/env python
"""M2 单 Agent 基线翻译运行脚本（CLI 入口）。

用法::

    python scripts/run_baseline.py \
        --work-id demo_work \
        --chapter path/to/chapter_001.txt \
        --config config/settings.example.yaml \
        > out/baseline_ch001.jsonl

输出为 JSONL（UTF-8，逐行一个 JSON 对象）：
    - 每个 segment 一行：{"type": "segment", "index", "source", "translation"}
    - 最后一行为注入统计：{"type": "injection_stats", ...}

注意：本脚本属 CLI / 脚本入口，按团队约定允许直接读取配置文件；
``pyyaml`` 在使用处延迟导入，缺失时给出安装提示而非 ImportError 堆栈。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# src 布局：仓库未安装为包时，也能直接 `python scripts/run_baseline.py`
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="M2 单 Agent 基线翻译：单模型直译 + 记忆层 RAG 注入（实验对照组）。",
        epilog="输出为 JSONL，重定向到文件即可保存，如：... > out/baseline.jsonl",
    )
    parser.add_argument("--work-id", required=True, help="作品 ID（术语库 / TM 的命名空间）")
    parser.add_argument("--chapter", required=True, help="章节原文 txt 文件路径（UTF-8）")
    parser.add_argument("--config", required=True, help="YAML 配置文件路径（settings）")
    return parser.parse_args(argv)


def _load_config(config_path: str) -> dict[str, Any]:
    """读取 YAML 配置；pyyaml 延迟导入，缺失时给出安装提示。"""
    path = Path(config_path)
    if not path.is_file():
        raise SystemExit(f"配置文件不存在：{path}")
    try:
        import yaml  # 延迟导入：第三方库 pyyaml
    except ImportError:
        raise SystemExit("缺少依赖 pyyaml，请先安装：pip install pyyaml")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"配置文件顶层应为字典：{path}")
    return data


def _build_llm(cfg: dict[str, Any]) -> Any:
    """按统一接口构造 LLMClient（未配置 API key 时自动 [DRAFT] 降级）。"""
    try:
        from mant.llm.client import LLMClient
    except ImportError as exc:
        raise SystemExit(f"无法导入 mant.llm.client（LLM 模块未就绪？）：{exc}")
    return LLMClient.from_config(cfg)


def _build_memory(cfg: dict[str, Any]) -> Any:
    """尽力构造 MemoryHub；不可用时返回 None（基线退化为无注入直译）。

    TODO(记忆层联调): 以记忆层负责人最终落地的构造 API 为准
    （当前优先 from_config，其次关键字 / 位置参数构造）。
    """
    mem_cfg = cfg.get("memory") or {}
    try:
        from mant.memory import MemoryHub
    except ImportError:
        print("[警告] mant.memory 未就绪，本次运行不做 RAG 注入。", file=sys.stderr)
        return None
    if hasattr(MemoryHub, "from_config"):
        try:
            return MemoryHub.from_config(mem_cfg)
        except Exception as exc:  # noqa: BLE001 - 骨架期宽容降级
            print(f"[警告] MemoryHub.from_config 失败：{exc!r}，尝试其他构造方式。", file=sys.stderr)
    sqlite_path = mem_cfg.get("sqlite_path")
    if sqlite_path:
        for args, kwargs in (
            ((), {"sqlite_path": sqlite_path, "faiss_index_dir": mem_cfg.get("faiss_index_dir")}),
            ((sqlite_path,), {}),
        ):
            try:
                return MemoryHub(*args, **kwargs)
            except Exception:  # noqa: BLE001 - 尝试下一种构造形态
                continue
    print("[警告] 无法构造 MemoryHub，本次运行不做 RAG 注入。", file=sys.stderr)
    return None


def _emit_jsonl(result: dict[str, Any]) -> None:
    """把翻译结果按 JSONL 写到 stdout：逐段译文 + 末行注入统计。"""
    for index, (source, translation) in enumerate(
        zip(result["segments"], result["translations"])
    ):
        print(json.dumps(
            {"type": "segment", "index": index, "source": source, "translation": translation},
            ensure_ascii=False,
        ))
    print(json.dumps(
        {
            "type": "injection_stats",
            "work_id": result["work_id"],
            "chapter_id": result["chapter_id"],
            **result["injection_stats"],
        },
        ensure_ascii=False,
    ))


def main(argv: list[str] | None = None) -> int:
    """脚本主入口：读配置 → 构造 LLM / 记忆层 → 逐段直译 → 输出 JSONL。"""
    # Windows 控制台默认 GBK，强制 UTF-8 保证中文 JSONL 不乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = _parse_args(argv)
    chapter_path = Path(args.chapter)
    if not chapter_path.is_file():
        raise SystemExit(f"章节文件不存在：{chapter_path}")

    cfg = _load_config(args.config)
    llm = _build_llm(cfg)
    memory = _build_memory(cfg)

    # 延迟导入本项目模块，保证 --help 等路径不依赖其他模块就绪
    from mant.baseline import BaselineTranslator

    translator = BaselineTranslator(cfg.get("baseline") or {})
    result = translator.translate_chapter(args.work_id, chapter_path, memory, llm)
    _emit_jsonl(result)

    # 记忆层若持有连接（如 sqlite），尽力关闭
    close = getattr(memory, "close", None)
    if callable(close):
        close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
