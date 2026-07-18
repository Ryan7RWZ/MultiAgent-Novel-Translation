"""本地运行 manifest：冻结恢复所需状态，并提供输入完整性校验。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


MANIFEST_SCHEMA_VERSION = 1


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RunManifestStore:
    """按 run_id 原子写入 JSON；目录应位于已忽略的 data/runtime 下。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def path_for(self, run_id: str) -> Path:
        safe = str(run_id).strip()
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        if not safe or any(char not in allowed for char in safe):
            raise ValueError("run_id 只能包含字母、数字、连字符和下划线")
        return self.directory / f"{safe}.json"

    def save(
        self,
        *,
        run_id: str,
        chapter_path: str | Path,
        state: Mapping[str, Any],
        settings: Mapping[str, Any],
    ) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        source_text = str(state.get("source_text") or "")
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": run_id,
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "work_id": str(state.get("work_id") or ""),
            "chapter_id": str(state.get("chapter_id") or ""),
            "chapter_path": str(Path(chapter_path).resolve()),
            "source_sha256": sha256_text(source_text),
            "settings": dict(settings),
            "state": dict(state),
        }
        target = self.path_for(run_id)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return target

    def load(self, run_id: str) -> dict[str, Any]:
        path = self.path_for(run_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"未找到运行 manifest：{path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"运行 manifest 损坏：{path}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"不支持的运行 manifest 版本：{path}")
        state = raw.get("state")
        if not isinstance(state, dict):
            raise ValueError(f"运行 manifest 缺少 state：{path}")
        return raw
