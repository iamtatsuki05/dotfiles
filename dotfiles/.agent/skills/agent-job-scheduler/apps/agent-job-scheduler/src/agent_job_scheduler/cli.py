from __future__ import annotations

import json
import sys
from pathlib import Path

import fire

from .launchd import DEFAULT_INTERVAL_SECONDS, DEFAULT_LABEL, render_launch_agent_plist
from .models import Agent
from .runtime import build_runtime_paths
from .scheduler import Scheduler, format_status_output
from .timeutil import parse_timestamp

COMMAND_ALIASES = {
    "run-once": "run_once",
    "show-config": "show_config",
    "allow-workdir": "allow_workdir",
    "disallow-workdir": "disallow_workdir",
    "list-allowed-workdirs": "list_allowed_workdirs",
    "set-allowlist-enforcement": "set_allowlist_enforcement",
    "print-launchd-plist": "print_launchd_plist",
    "active-runs": "active_runs",
    "set-stale-running-timeout": "set_stale_running_timeout",
}


class SchedulerCLI:
    def __init__(self, runtime_root: Path | None = None) -> None:
        self.scheduler = Scheduler(paths=build_runtime_paths(runtime_root))

    def enqueue(
        self,
        agent: str,
        workdir: str,
        prompt: str | None = None,
        prompt_file: str | None = None,
        scheduled_at: str | None = None,
    ) -> str:
        prompt_text = _read_prompt(prompt, prompt_file)
        when = parse_timestamp(scheduled_at) if scheduled_at else None
        job = self.scheduler.enqueue(
            agent=Agent(agent),
            workdir=Path(workdir),
            prompt=prompt_text,
            scheduled_at=when,
        )
        return f"enqueued job_id={job.job_id} agent={job.agent.value} scheduled_at={job.scheduled_at}"

    def run_once(self) -> str:
        outcomes = self.scheduler.run_once()
        if not outcomes:
            return "no due jobs"
        return "\n".join(
            (
                f"{outcome.job_id} agent={outcome.agent.value} status={outcome.status.value} "
                f"exit_code={outcome.exit_code} result_path={outcome.result_path}"
            )
            for outcome in outcomes
        )

    def status(self) -> str:
        snapshot = self.scheduler.status()
        return format_status_output(snapshot, self.scheduler.paths.root)

    def show(self, job_id: str, tail_lines: int = 20, include_prompt: bool = False) -> str:
        payload = self.scheduler.show_job(
            job_id,
            tail_lines=tail_lines,
            include_prompt=include_prompt,
        )
        return _json_dump(payload)

    def retry(self, job_id: str, scheduled_at: str | None = None) -> str:
        when = parse_timestamp(scheduled_at) if scheduled_at else None
        job = self.scheduler.retry_job(job_id, scheduled_at=when)
        return f"retried job_id={job.job_id} scheduled_at={job.scheduled_at}"

    def requeue(self, job_id: str, scheduled_at: str | None = None) -> str:
        when = parse_timestamp(scheduled_at) if scheduled_at else None
        job = self.scheduler.requeue_job(job_id, scheduled_at=when)
        return f"requeued source_job_id={job_id} new_job_id={job.job_id} scheduled_at={job.scheduled_at}"

    def cancel(self, job_id: str) -> str:
        job = self.scheduler.cancel_job(job_id)
        return f"cancelled job_id={job.job_id}"

    def show_config(self) -> str:
        return _json_dump(self.scheduler.show_settings())

    def active_runs(self) -> str:
        return _json_dump([active_run.to_dict() for active_run in self.scheduler.list_active_runs()])

    def allow_workdir(self, workdir: str) -> str:
        settings = self.scheduler.allow_workdir(Path(workdir))
        return _json_dump(settings.to_dict())

    def disallow_workdir(self, workdir: str) -> str:
        settings = self.scheduler.disallow_workdir(Path(workdir))
        return _json_dump(settings.to_dict())

    def list_allowed_workdirs(self) -> str:
        settings = self.scheduler.load_settings()
        return "\n".join(settings.allowed_workdirs)

    def set_allowlist_enforcement(self, enabled: str | bool) -> str:
        settings = self.scheduler.set_allowlist_enforcement(enabled=_coerce_bool(enabled))
        return _json_dump(settings.to_dict())

    def set_stale_running_timeout(self, seconds: int) -> str:
        settings = self.scheduler.set_stale_running_timeout(seconds=seconds)
        return _json_dump(settings.to_dict())

    def print_launchd_plist(
        self,
        label: str = DEFAULT_LABEL,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        bin_path: str | None = None,
    ) -> str:
        return render_launch_agent_plist(
            runtime_root=self.scheduler.paths.root,
            label=label,
            interval_seconds=interval_seconds,
            bin_path=Path(bin_path) if bin_path else None,
        )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    runtime_root, command_argv = _extract_runtime_root(raw_argv)
    normalized_argv = _normalize_command_tokens(command_argv)
    fire.Fire(SchedulerCLI(runtime_root), command=normalized_argv, name="agent-job-scheduler")
    return 0


def _extract_runtime_root(argv: list[str]) -> tuple[Path | None, list[str]]:
    runtime_root: Path | None = None
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--runtime-root":
            if index + 1 >= len(argv):
                msg = "--runtime-root requires a value"
                raise ValueError(msg)
            runtime_root = Path(argv[index + 1])
            index += 2
            continue
        if token.startswith("--runtime-root="):
            runtime_root = Path(token.split("=", 1)[1])
            index += 1
            continue
        remaining.append(token)
        index += 1
    return runtime_root, remaining


def _normalize_command_tokens(argv: list[str]) -> list[str]:
    normalized = list(argv)
    for index, token in enumerate(normalized):
        if token == "--":
            break
        if token.startswith("-"):
            continue
        normalized[index] = COMMAND_ALIASES.get(token, token)
        break
    return normalized


def _read_prompt(prompt: str | None, prompt_file: str | None) -> str:
    if prompt is not None and prompt_file is not None:
        msg = "prompt and prompt_file are mutually exclusive"
        raise ValueError(msg)
    if prompt is not None:
        return prompt
    if prompt_file is not None:
        return Path(prompt_file).read_text(encoding="utf-8")
    msg = "either prompt or prompt_file is required"
    raise ValueError(msg)


def _coerce_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "on", "yes"}:
        return True
    if normalized in {"0", "false", "off", "no"}:
        return False
    msg = f"unsupported boolean value: {value}"
    raise ValueError(msg)


def _json_dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
