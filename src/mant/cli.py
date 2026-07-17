"""mant 命令行入口（骨架）。

子命令一览：

- ``m1-pipeline``       M1 离线语料管道：采集 → 清洗 → 句对齐 → 术语抽取。
- ``baseline``          M2 单 Agent 基线翻译，用作多智能体方案的实验对照。
- ``translate-chapter`` 多智能体协作翻译单个章节（LangGraph 状态机，
  QA 不达标时携带批注回退返工，上限 ``workflow.max_rework`` 次）。

约定：第三方库与其他 mant 子模块一律延迟导入（函数内 import），
保证骨架阶段在仅含 stdlib 的环境下 ``python -m mant.cli --help`` 可用。
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

DEFAULT_CONFIG = Path("config/settings.yaml")


def load_settings(config_path: str | None) -> dict[str, Any]:
    """加载 YAML 配置为字典（pyyaml 延迟导入）。

    Args:
        config_path: 配置文件路径；``None`` 时使用 ``config/settings.yaml``。

    Returns:
        配置字典；文件缺失或 pyyaml 未安装时打印提示并返回空字典。
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if not path.is_file():
        print(f"[提示] 未找到配置文件 {path}，"
              f"请复制 config/settings.example.yaml 后按环境修改。")
        return {}
    try:
        import yaml  # 延迟导入：pyyaml 为第三方依赖
    except ImportError:
        print("[提示] 缺少依赖 pyyaml，请先执行：pip install pyyaml")
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _lazy_module(name: str) -> ModuleType | None:
    """延迟导入 mant 子模块；模块或依赖缺失时打印 TODO 提示。

    Args:
        name: 模块全名，如 ``mant.pipeline``。

    Returns:
        导入成功的模块对象；失败时返回 ``None``。
    """
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        print(f"[TODO] 模块 {name} 尚未就绪（{exc}），请等待对应负责人实现。")
        return None


def cmd_m1_pipeline(args: argparse.Namespace) -> int:
    """M1 离线语料管道入口（骨架）。

    TODO(M1 负责人): 接线管道主流程：采集 → 清洗 → 句对齐 → 术语抽取；
    约定配置键 ``cfg["pipeline"] = {raw_dir, aligned_dir, glossary_dir}``。
    """
    cfg = load_settings(args.config)
    pipeline = _lazy_module("mant.pipeline")
    if pipeline is None:
        print("[TODO] M1 管道主流程待实现：采集 → 清洗 → 句对齐 → 术语抽取。")
        return 0
    # TODO: 调用管道主函数，例如 pipeline.run(cfg.get("pipeline", {}))
    print("[TODO] 已加载 mant.pipeline，主流程接线待完成。")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    """M2 单 Agent 基线翻译入口（骨架）。

    TODO(M2 负责人): 接线单 Agent 基线：读取章节 → LLMClient 直译 → 导出；
    与多智能体工作流共用同一评测口径，作为实验对照组。
    """
    cfg = load_settings(args.config)
    baseline = _lazy_module("mant.baseline")
    if baseline is None:
        print(f"[TODO] M2 基线待实现：work_id={args.work_id} "
              f"chapter_id={args.chapter_id} input={args.input}")
        return 0
    # TODO: 调用基线主函数，例如 baseline.run(args, cfg)
    print("[TODO] 已加载 mant.baseline，主流程接线待完成。")
    return 0


def cmd_translate_chapter(args: argparse.Namespace) -> int:
    """多智能体协作翻译单章入口（骨架）。

    TODO(工作流负责人): 构建 TranslationState 并运行 LangGraph 状态机：
    术语 → 翻译 → 审校 → 润色 → QA 终审；QA 不达标携带批注回退返工，
    返工上限 ``max_rework`` 次（默认取配置 ``workflow.max_rework``，约定值 2）。
    """
    cfg = load_settings(args.config)
    workflow = _lazy_module("mant.workflow")
    max_rework = args.max_rework
    if max_rework is None:
        max_rework = cfg.get("workflow", {}).get("max_rework", 2)
    if workflow is None:
        print(f"[TODO] 多智能体工作流待实现：work_id={args.work_id} "
              f"chapter_id={args.chapter_id} input={args.input} "
              f"max_rework={max_rework}")
        return 0
    # TODO: 调用工作流主函数，例如 workflow.translate_chapter(args, cfg)
    print("[TODO] 已加载 mant.workflow，主流程接线待完成。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建顶层参数解析器与三个子命令。

    Returns:
        配置完成的 ``ArgumentParser``。
    """
    parser = argparse.ArgumentParser(
        prog="mant",
        description="基于大数据与多智能体协作的网络小说自主翻译系统",
    )
    # 公共参数：每个子命令都接受 --config，放在子命令之后也可解析
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", default=None,
        help="配置文件路径（默认 config/settings.yaml）",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<子命令>")

    p_pipe = sub.add_parser(
        "m1-pipeline", parents=[common],
        help="M1 离线语料管道：采集→清洗→句对齐→术语抽取",
    )
    p_pipe.add_argument("--raw-dir", default=None,
                        help="原始语料目录（覆盖配置 pipeline.raw_dir）")
    p_pipe.set_defaults(func=cmd_m1_pipeline)

    p_base = sub.add_parser(
        "baseline", parents=[common],
        help="M2 单 Agent 基线翻译（实验对照组）",
    )
    p_base.add_argument("--work-id", required=True, help="作品 ID")
    p_base.add_argument("--chapter-id", required=True, help="章节 ID")
    p_base.add_argument("--input", required=True, help="源文章节文件路径")
    p_base.set_defaults(func=cmd_baseline)

    p_trans = sub.add_parser(
        "translate-chapter", parents=[common],
        help="多智能体协作翻译单个章节（LangGraph 状态机）",
    )
    p_trans.add_argument("--work-id", required=True, help="作品 ID")
    p_trans.add_argument("--chapter-id", required=True, help="章节 ID")
    p_trans.add_argument("--input", required=True, help="源文章节文件路径")
    p_trans.add_argument("--max-rework", type=int, default=None,
                         help="最大返工次数（默认取配置 workflow.max_rework）")
    p_trans.set_defaults(func=cmd_translate_chapter)

    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行主入口。

    Args:
        argv: 参数列表；``None`` 时读取 ``sys.argv``。

    Returns:
        进程退出码。
    """
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
