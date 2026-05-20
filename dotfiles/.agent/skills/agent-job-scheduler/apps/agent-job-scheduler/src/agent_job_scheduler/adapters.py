from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from pathlib import Path

from .models import Agent, CommandSpec, JobRecord

type CommandBuilder = Callable[[JobRecord], CommandSpec]


class AdapterRegistry:
    def __init__(self, builders: Mapping[Agent, CommandBuilder] | None = None) -> None:
        self._builders = dict(builders or default_builders())

    def build(self, job: JobRecord) -> CommandSpec:
        builder = self._builders.get(job.agent)
        if builder is None:
            msg = f"unsupported agent: {job.agent.value}"
            raise ValueError(msg)
        return builder(job)


def default_builders() -> dict[Agent, CommandBuilder]:
    return {
        Agent.ANTIGRAVITY: build_antigravity_command,
        Agent.CLAUDE: build_claude_command,
        Agent.CODEX: build_codex_command,
        Agent.COPILOT: build_copilot_command,
        Agent.CURSOR: build_cursor_command,
        Agent.DEVIN: build_devin_command,
        Agent.HERMES: build_hermes_command,
        Agent.OPENCODE: build_opencode_command,
        Agent.OPENCLAW: build_openclaw_command,
    }


def build_antigravity_command(job: JobRecord) -> CommandSpec:
    argv = (
        "agy",
        "chat",
        "--mode",
        "agent",
        job.prompt,
    )
    return CommandSpec(argv=argv, cwd=Path(job.workdir), display_command=shlex.join(argv))


def build_claude_command(job: JobRecord) -> CommandSpec:
    argv = (
        "claude",
        "--dangerously-skip-permissions",
        "-p",
        job.prompt,
    )
    return CommandSpec(argv=argv, cwd=Path(job.workdir), display_command=shlex.join(argv))


def build_codex_command(job: JobRecord) -> CommandSpec:
    argv = (
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        job.workdir,
        job.prompt,
    )
    return CommandSpec(argv=argv, cwd=Path(job.workdir), display_command=shlex.join(argv))


def build_copilot_command(job: JobRecord) -> CommandSpec:
    argv = (
        "copilot",
        "-C",
        job.workdir,
        "--allow-all",
        "--no-remote",
        "--output-format",
        "text",
        "-p",
        job.prompt,
    )
    return CommandSpec(argv=argv, cwd=Path(job.workdir), display_command=shlex.join(argv))


def build_devin_command(job: JobRecord) -> CommandSpec:
    argv = (
        "devin",
        "--permission-mode",
        "dangerous",
        "--respect-workspace-trust",
        "true",
        "-p",
        job.prompt,
    )
    return CommandSpec(argv=argv, cwd=Path(job.workdir), display_command=shlex.join(argv))


def build_cursor_command(job: JobRecord) -> CommandSpec:
    argv = (
        "cursor-agent",
        "--workspace",
        job.workdir,
        "--print",
        "--force",
        "--trust",
        job.prompt,
    )
    return CommandSpec(argv=argv, cwd=Path(job.workdir), display_command=shlex.join(argv))


def build_opencode_command(job: JobRecord) -> CommandSpec:
    argv = (
        "opencode",
        "run",
        "--dir",
        job.workdir,
        "--dangerously-skip-permissions",
        job.prompt,
    )
    return CommandSpec(argv=argv, cwd=Path(job.workdir), display_command=shlex.join(argv))


def build_hermes_command(job: JobRecord) -> CommandSpec:
    argv = (
        "hermes",
        "--accept-hooks",
        "--yolo",
        "-z",
        job.prompt,
    )
    return CommandSpec(
        argv=argv,
        cwd=Path(job.workdir),
        display_command=shlex.join(argv),
        env={"HERMES_ACCEPT_HOOKS": "1"},
    )


def build_openclaw_command(job: JobRecord) -> CommandSpec:
    argv = (
        "openclaw",
        "agent",
        "--local",
        "--session-id",
        f"agent-job-scheduler-{job.job_id}",
        "--message",
        job.prompt,
        "--timeout",
        "600",
    )
    return CommandSpec(argv=argv, cwd=Path(job.workdir), display_command=shlex.join(argv))
