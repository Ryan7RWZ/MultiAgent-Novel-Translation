#!/usr/bin/env python
"""M1 离线语料管道一键运行脚本：collect → clean → align → extract_terms。

用法::

    python scripts/run_m1_pipeline.py \
        --raw-dir data/raw \
        --aligned-dir data/aligned \
        --glossary-db data/memory/mant.db

说明：
- 全流程仅需标准库即可跑通（LLM 复核默认关闭，候选词按"未复核"入库策略处理）；
- ``--with-llm`` 开启 LLM 术语复核：需安装 openai 并配置 API key（第三方库延迟导入，
  未安装/未配置时自动降级为占位，流程不中断）；
- 输入目录规范与版权合规要求见 ``data/README.md``；
- 本脚本是唯一允许直接读配置文件的入口（``--config``，需 pyyaml，同样延迟导入）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# src 布局：未 pip install 时也能直接运行本脚本
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mant.pipeline.runner import run_pipeline  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """定义并解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="M1 离线语料管道：采集 → 清洗 → 句对齐 → 术语抽取",
    )
    parser.add_argument("--raw-dir", default="data/raw",
                        help="原始语料根目录（默认 data/raw，结构见 data/README.md）")
    parser.add_argument("--aligned-dir", default="data/aligned",
                        help="句对齐 JSONL 输出目录（默认 data/aligned）")
    parser.add_argument("--glossary-db", default="data/memory/mant.db",
                        help="术语/TM 运行库 sqlite 路径（默认 data/memory/mant.db）")
    parser.add_argument("--src-lang", default="zh", help="源语言代码（默认 zh）")
    parser.add_argument("--tgt-lang", default="en", help="目标语言代码（默认 en）")
    parser.add_argument("--top-k", type=int, default=200, help="每作品术语候选上限（默认 200）")
    parser.add_argument("--min-freq", type=int, default=3, help="候选词最低词频（默认 3）")
    parser.add_argument("--with-llm", action="store_true",
                        help="开启 LLM 术语复核（需 openai + API key，未配置自动降级）")
    parser.add_argument("--config", default=None,
                        help="YAML 配置路径（可选，读取 llm.providers.*，需 pyyaml）")
    return parser.parse_args(argv)


def _load_llm(config_path: str | None):
    """按需构造 LLMClient（延迟导入 openai/pyyaml 相关链路）。

    - 未传 --with-llm：直接返回 None（离线模式，术语按未复核透传）；
    - 未安装 pyyaml / openai、未配置 API key：LLMClient 内部自动降级为
      [DRAFT] 占位响应，本函数不抛异常。
    """
    cfg: dict = {}
    if config_path:
        try:
            import yaml  # 延迟导入第三方库 pyyaml
        except ImportError:
            print("[M1][WARN] 未安装 pyyaml（pip install pyyaml），--config 被忽略，使用空配置。")
        else:
            cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    try:
        from mant.llm.client import LLMClient  # 统一 LLM 接口（其内部再延迟导入 openai）
    except ImportError as exc:
        print(f"[M1][WARN] 无法导入 mant.llm.client（{exc}），跳过 LLM 复核。")
        return None
    return LLMClient.from_config(cfg)


def main(argv: list[str] | None = None) -> int:
    """串联四步并打印每步统计，返回进程退出码。"""
    args = _parse_args(argv)
    llm = _load_llm(args.config) if args.with_llm else None
    try:
        run_pipeline(
            raw_dir=args.raw_dir,
            aligned_dir=args.aligned_dir,
            glossary_db=args.glossary_db,
            src_lang=args.src_lang,
            tgt_lang=args.tgt_lang,
            top_k=args.top_k,
            min_freq=args.min_freq,
            llm=llm,
        )
    except (OSError, ValueError) as exc:
        print(f"[M1][ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
