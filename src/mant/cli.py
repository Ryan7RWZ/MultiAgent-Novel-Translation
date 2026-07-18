"""mant 命令行入口。

子命令一览：

- ``m1-pipeline``       M1 离线语料管道：采集 → 清洗 → 句对齐 → 术语抽取。
- ``baseline``          M2 单 Agent 基线翻译，用作多智能体方案的实验对照。
- ``translate-chapter`` 多智能体协作翻译单个章节（LangGraph 状态机，
  QA 不达标时携带批注回退返工，上限 ``workflow.max_rework`` 次）。
- ``monitor``           监听 JSONL 追踪目录并提供本地 SSE 实时监控页。

约定：第三方库与其他 mant 子模块一律延迟导入（函数内 import），
保证骨架阶段在仅含 stdlib 的环境下 ``python -m mant.cli --help`` 可用。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
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


def _build_llm(cfg: dict[str, Any]):
    """按统一配置构造 LLMClient；无 key 时由客户端自动降级为 [DRAFT]。"""
    from mant.llm.client import LLMClient

    return LLMClient.from_config(cfg)


def _build_memory(cfg: dict[str, Any]):
    """按统一配置构造 MemoryHub。"""
    from mant.memory import MemoryHub

    return MemoryHub.from_config(cfg)


def _default_export_path(kind: str, work_id: str, chapter_id: str, suffix: str) -> Path:
    return Path("data/exports") / kind / work_id / f"{chapter_id}{suffix}"


def _max_trace_sequence(path: Path) -> int:
    """读取已有 JSONL 的最大事件序号；坏行不阻断恢复。"""
    maximum = 0
    if not path.is_file():
        return maximum
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                    maximum = max(maximum, int(event.get("sequence", 0) or 0))
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    continue
    except OSError:
        return 0
    return maximum


def cmd_m1_pipeline(args: argparse.Namespace) -> int:
    """运行 M1 离线语料管道。"""
    cfg = load_settings(args.config)
    pipe_cfg = cfg.get("pipeline") or {}
    raw_dir = args.raw_dir or pipe_cfg.get("raw_dir", "data/raw")
    aligned_dir = args.aligned_dir or pipe_cfg.get("aligned_dir", "data/aligned")
    glossary_db = args.glossary_db
    if not glossary_db:
        glossary_db = (
            pipe_cfg.get("glossary_db")
            or (cfg.get("memory") or {}).get("sqlite_path")
            or "data/memory/mant.db"
        )
    try:
        from mant.pipeline.runner import run_pipeline

        run_pipeline(
            raw_dir=raw_dir,
            aligned_dir=aligned_dir,
            glossary_db=glossary_db,
            src_lang=args.src_lang,
            tgt_lang=args.tgt_lang,
            top_k=args.top_k,
            min_freq=args.min_freq,
            llm=_build_llm(cfg) if args.with_llm else None,
        )
    except (ImportError, OSError, ValueError) as exc:
        print(f"[错误] M1 管道运行失败：{exc}", file=sys.stderr)
        return 1
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    """运行 M2 单 Agent 基线并导出 JSONL。"""
    cfg = load_settings(args.config)
    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"[错误] 章节文件不存在：{input_path}", file=sys.stderr)
        return 1
    memory = None
    try:
        from mant.baseline import BaselineTranslator

        memory = _build_memory(cfg)
        result = BaselineTranslator(cfg.get("baseline") or {}).translate_chapter(
            args.work_id, input_path, memory, _build_llm(cfg)
        )
        result["chapter_id"] = args.chapter_id
        output_path = Path(args.output) if args.output else _default_export_path(
            "baseline", args.work_id, args.chapter_id, ".jsonl"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8", newline="\n") as fh:
            for index, (source, translation) in enumerate(
                zip(result["segments"], result["translations"])
            ):
                row = {
                    "type": "segment",
                    "index": index,
                    "source": source,
                    "translation": translation,
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            stats = {
                "type": "injection_stats",
                "work_id": result["work_id"],
                "chapter_id": result["chapter_id"],
                **result["injection_stats"],
            }
            fh.write(json.dumps(stats, ensure_ascii=False) + "\n")
        print(f"[baseline] 已导出：{output_path}")
        return 0
    except (ImportError, OSError, ValueError) as exc:
        print(f"[错误] 基线翻译失败：{exc}", file=sys.stderr)
        return 1
    finally:
        close = getattr(memory, "close", None)
        if callable(close):
            close()


def cmd_translate_chapter(args: argparse.Namespace) -> int:
    """运行多智能体单章工作流并导出译文与元数据。"""
    cfg = load_settings(args.config)
    resume_manifest = getattr(args, "resume_manifest", None)
    resume_state = (
        dict(resume_manifest.get("state") or {})
        if isinstance(resume_manifest, dict)
        else None
    )
    work_id = str(
        (resume_manifest or {}).get("work_id") or args.work_id
    )
    chapter_id = str(
        (resume_manifest or {}).get("chapter_id") or args.chapter_id
    )
    max_rework = args.max_rework
    if max_rework is None and resume_state is not None:
        max_rework = int(resume_state.get("max_rework", 0) or 0)
    if max_rework is None:
        max_rework = cfg.get("workflow", {}).get("max_rework", 2)
    if max_rework < 0:
        print("[错误] max_rework 不能小于 0。", file=sys.stderr)
        return 1
    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"[错误] 章节文件不存在：{input_path}", file=sys.stderr)
        return 1

    memory = None
    observer = None
    try:
        from mant.observability import create_observer
        from mant.workflow import run_chapter

        obs_cfg = dict(cfg.get("observability") or {})
        if args.trace_dir:
            obs_cfg["trace_dir"] = args.trace_dir
            obs_cfg["sqlite_path"] = str(Path(args.trace_dir) / "runs.db")
        observer = create_observer(
            obs_cfg,
            terminal_enabled=True if (args.stream or args.verbose) else None,
            stream_tokens=True if args.stream else None,
            trace_enabled=args.trace,
            verbose=args.verbose,
        )
        if resume_state is not None and observer is not None and args.run_id:
            trace_dir = Path(obs_cfg.get("trace_dir", "data/traces"))
            observer.continue_after(
                _max_trace_sequence(trace_dir / f"{args.run_id}.jsonl")
            )
        memory = _build_memory(cfg)
        final = run_chapter(
            work_id,
            input_path,
            _build_llm(cfg),
            memory,
            chapter_id=chapter_id,
            max_rework=max_rework,
            observer=observer,
            run_id=args.run_id,
            segmentation_config=cfg.get("segmentation") or {},
            workflow_config=cfg.get("workflow") or {},
            execution_config=(
                getattr(args, "execution_config_override", None)
                or cfg.get("concurrency")
                or {}
            ),
            agent_config=cfg.get("agents") or {},
            resume_state=resume_state,
            start_stage=getattr(args, "resume_stage", "retrieve"),
        )
        output_path = Path(args.output) if args.output else _default_export_path(
            "multi_agent", work_id, chapter_id, ".txt"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        translated = str(final.get("polished") or final.get("draft") or "")
        output_path.write_text(translated.rstrip() + "\n", encoding="utf-8")

        metadata_path = (
            Path(args.metadata_output)
            if args.metadata_output
            else output_path.with_suffix(".json")
        )
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "run_id": (
                observer.last_run_id
                if observer is not None
                else str(final.get("run_id") or args.run_id or "")
            ),
            "work_id": work_id,
            "chapter_id": chapter_id,
            "output": str(output_path),
            "segments": len(final.get("segments") or []),
            "segmentation": final.get("segmentation_stats") or {},
            "qa_score": final.get("qa_score", 0.0),
            "qa_verdict": final.get("qa_verdict", ""),
            "qa_summary": final.get("qa_summary") or {},
            "rework_count": final.get("rework_count", 0),
            "max_rework": final.get("max_rework", max_rework),
            "needs_human_review": any(
                "needs_human_review" in str(note)
                for note in (final.get("review_notes") or [])
            ),
            "review_notes": final.get("review_notes") or [],
            "segment_failures": final.get("segment_failures") or [],
            "rework_segment_indices": final.get("rework_segment_indices") or [],
            "execution": final.get("execution_stats") or {},
            "resume": {
                "resumed": resume_state is not None,
                "stage": getattr(args, "resume_stage", "") if resume_state else "",
                "failed_only": bool(
                    getattr(args, "resume_failed_only", False)
                ) if resume_state else False,
            },
            "qa_segments": {
                "count": len(final.get("segment_qa") or []),
                "pass": sum(
                    item.get("qa_verdict") == "pass"
                    for item in (final.get("segment_qa") or [])
                ),
                "rework": sum(
                    item.get("qa_verdict") == "rework"
                    for item in (final.get("segment_qa") or [])
                ),
            },
            "output_integrity": {
                "draft_chars": len(str(final.get("draft") or "")),
                "polished_chars": len(str(final.get("polished") or "")),
                "polished_draft_ratio": round(
                    len(str(final.get("polished") or ""))
                    / max(1, len(str(final.get("draft") or ""))),
                    4,
                ),
            },
            "runtime_notes": final.get("runtime_notes") or [],
            "observability_errors": (
                list(observer.bus.errors) if observer is not None else []
            ),
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"[translate] 已导出：{output_path}；QA={metadata['qa_verdict']} "
            f"score={metadata['qa_score']} rework={metadata['rework_count']}/"
            f"{metadata['max_rework']}"
        )
        print(f"[translate] 运行元数据：{metadata_path}")
        if observer is not None and observer.last_run_id:
            trace_dir = obs_cfg.get("trace_dir", "data/traces")
            trace_path = Path(trace_dir) / f"{observer.last_run_id}.jsonl"
            if trace_path.exists():
                print(f"[translate] 事件追踪：{trace_path}")
        if observer is not None and observer.bus.errors:
            print(
                f"[警告] 可观测接收器出现 {len(observer.bus.errors)} 个错误，"
                "详见运行元数据 observability_errors。",
                file=sys.stderr,
            )
        return 0
    except ImportError as exc:
        print(f"[错误] 多智能体工作流依赖缺失：{exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"[错误] 多智能体翻译失败：{exc}", file=sys.stderr)
        return 1
    finally:
        close = getattr(memory, "close", None)
        if callable(close):
            close()
        if observer is not None:
            observer.close()


def cmd_resume_run(args: argparse.Namespace) -> int:
    """从本地 manifest 恢复同一 run，只重新执行目标阶段失败/缺失片。"""
    cfg = load_settings(args.config)
    try:
        from mant.execution import ExecutionConfig, RunManifestStore

        current_execution = dict(cfg.get("concurrency") or {})
        parsed = ExecutionConfig.from_mapping(current_execution)
        manifest = RunManifestStore(parsed.manifest_dir).load(args.run_id)
        stored_execution = dict(
            ((manifest.get("settings") or {}).get("execution") or {})
        )
        stored_checkpoint = dict(stored_execution.get("checkpoint") or {})
        if not stored_checkpoint.get("enabled"):
            raise ValueError("该运行没有启用 checkpoint，无法定向恢复")
        current_execution["checkpoint"] = stored_checkpoint
        current_execution["manifest"] = dict(
            stored_execution.get("manifest")
            or {"enabled": True, "directory": str(parsed.manifest_dir)}
        )
        current_execution["resume"] = {
            "stage": args.stage,
            "failed_only": args.failed_only,
        }
        forwarded = argparse.Namespace(
            config=args.config,
            work_id=str(manifest.get("work_id") or ""),
            chapter_id=str(manifest.get("chapter_id") or ""),
            input=str(manifest.get("chapter_path") or ""),
            max_rework=None,
            output=args.output,
            metadata_output=args.metadata_output,
            stream=args.stream,
            verbose=args.verbose,
            trace=args.trace,
            run_id=args.run_id,
            trace_dir=args.trace_dir,
            resume_manifest=manifest,
            resume_stage=args.stage,
            resume_failed_only=args.failed_only,
            execution_config_override=current_execution,
        )
        return cmd_translate_chapter(forwarded)
    except (OSError, ValueError) as exc:
        print(f"[错误] 恢复运行失败：{exc}", file=sys.stderr)
        return 1


def cmd_monitor(args: argparse.Namespace) -> int:
    """启动本地 SSE 监控页，实时追踪 JSONL 运行事件。"""
    cfg = load_settings(args.config)
    obs_cfg = cfg.get("observability") or {}
    dashboard_cfg = obs_cfg.get("dashboard") or {}
    trace_dir = args.trace_dir or obs_cfg.get("trace_dir", "data/traces")
    host = args.host or dashboard_cfg.get("host", "127.0.0.1")
    port = args.port or int(dashboard_cfg.get("port", 8765))
    max_input_chars = int(dashboard_cfg.get("max_input_chars", 200_000))
    try:
        from mant.observability.dashboard import serve_dashboard

        serve_dashboard(
            trace_dir,
            host=host,
            port=port,
            config_path=Path(args.config).resolve() if args.config else DEFAULT_CONFIG,
            max_input_chars=max_input_chars,
        )
        return 0
    except OSError as exc:
        print(f"[错误] 监控服务启动失败：{exc}", file=sys.stderr)
        return 1


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
    p_pipe.add_argument("--aligned-dir", default=None,
                        help="对齐 JSONL 输出目录（覆盖配置 pipeline.aligned_dir）")
    p_pipe.add_argument("--glossary-db", default=None, help="术语库 SQLite 路径")
    p_pipe.add_argument("--src-lang", default="zh", help="源语言代码（默认 zh）")
    p_pipe.add_argument("--tgt-lang", default="en", help="目标语言代码（默认 en）")
    p_pipe.add_argument("--top-k", type=int, default=200, help="术语候选上限")
    p_pipe.add_argument("--min-freq", type=int, default=3, help="术语最低词频")
    p_pipe.add_argument("--with-llm", action="store_true", help="使用 LLM 复核术语")
    p_pipe.set_defaults(func=cmd_m1_pipeline)

    p_base = sub.add_parser(
        "baseline", parents=[common],
        help="M2 单 Agent 基线翻译（实验对照组）",
    )
    p_base.add_argument("--work-id", required=True, help="作品 ID")
    p_base.add_argument("--chapter-id", required=True, help="章节 ID")
    p_base.add_argument("--input", required=True, help="源文章节文件路径")
    p_base.add_argument("--output", default=None, help="输出 JSONL 路径")
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
    p_trans.add_argument("--output", default=None, help="成品译文输出路径")
    p_trans.add_argument("--metadata-output", default=None, help="运行元数据 JSON 路径")
    p_trans.add_argument(
        "--stream", action="store_true",
        help="在终端实时显示各 Agent 的 LLM token 增量",
    )
    p_trans.add_argument(
        "--verbose", action="store_true",
        help="显示节点、LLM 重试与路由等详细实时事件",
    )
    p_trans.add_argument(
        "--trace", action=argparse.BooleanOptionalAction, default=None,
        help="启用/关闭 JSONL 与 SQLite 追踪（默认取 observability 配置）",
    )
    p_trans.add_argument(
        "--run-id", default=None,
        help="自定义本次运行 ID（缺省时自动生成）",
    )
    p_trans.add_argument(
        "--trace-dir", default=None,
        help="覆盖本次运行的追踪目录（供浏览器工作台后台任务使用）",
    )
    p_trans.set_defaults(func=cmd_translate_chapter)

    p_resume = sub.add_parser(
        "resume-run", parents=[common],
        help="从运行 manifest 恢复，只重跑目标阶段失败或缺失的片段",
    )
    p_resume.add_argument("--run-id", required=True, help="要恢复的原运行 ID")
    p_resume.add_argument(
        "--stage", choices=("qa",), default="qa",
        help="恢复起点（当前支持 qa）",
    )
    p_resume.add_argument(
        "--failed-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="复用目标阶段成功 checkpoint；--no-failed-only 会重跑目标阶段全部片段",
    )
    p_resume.add_argument("--output", default=None, help="恢复后成品译文输出路径")
    p_resume.add_argument(
        "--metadata-output", default=None, help="恢复后运行元数据 JSON 路径"
    )
    p_resume.add_argument(
        "--stream", action="store_true", help="实时显示各 Agent 的 LLM token 增量"
    )
    p_resume.add_argument(
        "--verbose", action="store_true", help="显示节点、重试与路由等详细事件"
    )
    p_resume.add_argument(
        "--trace", action=argparse.BooleanOptionalAction, default=None,
        help="启用/关闭 JSONL 与 SQLite 追踪",
    )
    p_resume.add_argument("--trace-dir", default=None, help="覆盖本次追踪目录")
    p_resume.set_defaults(func=cmd_resume_run)

    p_monitor = sub.add_parser(
        "monitor", parents=[common],
        help="启动各 Agent 的本地实时监控页（JSONL + SSE）",
    )
    p_monitor.add_argument("--trace-dir", default=None, help="追踪 JSONL 目录")
    p_monitor.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1）")
    p_monitor.add_argument("--port", type=int, default=None, help="监听端口（默认 8765）")
    p_monitor.set_defaults(func=cmd_monitor)

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
