from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}
MAX_FINDINGS = 20


def _paths(tool_name: str, args: Any) -> list[Path]:
    if tool_name not in {"write_file", "patch"} or not isinstance(args, dict):
        return []
    values = [args[key] for key in ("path", "file_path") if key in args]
    patch = args.get("patch_content") or args.get("patch")
    if isinstance(patch, str):
        values.extend(
            left or right
            for left, right in re.findall(
                r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$",
                patch,
                re.MULTILINE,
            )
        )
    paths: list[Path] = []
    for value in values:
        if isinstance(value, str):
            path = Path(value).expanduser()
            if path.suffix.lower() in SUPPORTED_SUFFIXES and path.is_file() and path not in paths:
                paths.append(path)
    return paths


def _on_transform_tool_result(tool_name: str = "", args: Any = None, result: Any = None, **_: Any) -> str | None:
    paths = _paths(tool_name, args)
    if not paths or not isinstance(result, str):
        return None
    command = [str(Path.home() / ".hermes/agent-hooks/japanese_prose_lint.sh"), "--check", *map(str, paths)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode == 0:
        return None
    if completed.returncode != 1:
        return result + "\n\n日本語 lint の実行に失敗しました。設定を確認してください。"
    lines = completed.stdout.splitlines()
    visible = lines[:MAX_FINDINGS]
    if len(lines) > MAX_FINDINGS:
        visible.append(f"ほか{len(lines) - MAX_FINDINGS}件あります。修正後に lint を再実行してください。")
    return result + "\n\n日本語 lint で修正候補が見つかりました。\n" + "\n".join(visible)


def register(ctx) -> None:
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
