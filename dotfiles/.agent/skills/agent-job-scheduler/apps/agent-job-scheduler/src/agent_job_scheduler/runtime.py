from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .fileio import atomic_write_json
from .models import JOB_FIELDNAMES

RUNTIME_ROOT_ENV = "AGENT_JOB_SCHEDULER_HOME"


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    jobs_csv: Path
    agent_state_json: Path
    active_runs_json: Path
    settings_json: Path
    prompts_dir: Path
    runs_dir: Path
    logs_dir: Path
    ledger_lock: Path
    scheduler_lock: Path


def default_runtime_root() -> Path:
    configured = os.environ.get(RUNTIME_ROOT_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".agent" / "agent-job-scheduler"


def build_runtime_paths(runtime_root: Path | None = None) -> RuntimePaths:
    root = (runtime_root or default_runtime_root()).expanduser()
    return RuntimePaths(
        root=root,
        jobs_csv=root / "jobs.csv",
        agent_state_json=root / "agent_state.json",
        active_runs_json=root / "active_runs.json",
        settings_json=root / "settings.json",
        prompts_dir=root / "prompts",
        runs_dir=root / "runs",
        logs_dir=root / "logs",
        ledger_lock=root / ".ledger.lock",
        scheduler_lock=root / ".scheduler.lock",
    )


def ensure_runtime_layout(paths: RuntimePaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.prompts_dir.mkdir(parents=True, exist_ok=True)
    paths.runs_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    if not paths.jobs_csv.exists():
        with paths.jobs_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=JOB_FIELDNAMES)
            writer.writeheader()

    if not paths.agent_state_json.exists():
        atomic_write_json(paths.agent_state_json, {})

    if not paths.active_runs_json.exists():
        atomic_write_json(paths.active_runs_json, {})

    if not paths.settings_json.exists():
        atomic_write_json(
            paths.settings_json,
            {
                "allowed_workdirs": [],
                "enforce_workdir_allowlist": False,
                "stale_running_timeout_seconds": 1800,
                "store_prompt_body_in_csv": False,
                "prompt_preview_chars": 160,
                "rate_limit_profiles": {},
            },
        )
