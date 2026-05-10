from __future__ import annotations

import plistlib
from pathlib import Path

from .runtime import build_runtime_paths

DEFAULT_LABEL = "io.github.iamtatsuki05.agent-job-scheduler"
DEFAULT_INTERVAL_SECONDS = 60


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_bin_path() -> Path:
    return project_root() / "bin" / "agent-job-scheduler"


def default_launch_agent_path(label: str = DEFAULT_LABEL, *, home: Path | None = None) -> Path:
    root = (home or Path.home()).expanduser()
    return root / "Library" / "LaunchAgents" / f"{label}.plist"


def render_launch_agent_plist(
    *,
    runtime_root: Path,
    label: str = DEFAULT_LABEL,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    bin_path: Path | None = None,
) -> str:
    if interval_seconds <= 0:
        msg = "interval_seconds must be greater than zero"
        raise ValueError(msg)

    runtime_paths = build_runtime_paths(runtime_root)
    stdout_path = runtime_paths.logs_dir / "launchd.stdout.log"
    stderr_path = runtime_paths.logs_dir / "launchd.stderr.log"
    command_path = (bin_path or default_bin_path()).expanduser().resolve()

    payload = {
        "Label": label,
        "ProgramArguments": [
            str(command_path),
            "--runtime-root",
            str(runtime_paths.root),
            "run-once",
        ],
        "WorkingDirectory": str(project_root()),
        "RunAtLoad": True,
        "StartInterval": interval_seconds,
        "ProcessType": "Background",
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")
