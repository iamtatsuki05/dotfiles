from __future__ import annotations

from pathlib import Path

from agent_job_scheduler.adapters import AdapterRegistry
from agent_job_scheduler.models import Agent, JobRecord, JobStatus


def test_default_registry_supports_all_agents(tmp_path: Path) -> None:
    registry = AdapterRegistry()

    for agent in Agent:
        spec = registry.build(_job(agent=agent, workdir=tmp_path, prompt="do the task"))

        assert spec.cwd == tmp_path
        assert "do the task" in spec.argv


def test_default_agent_commands_match_cli_contracts(tmp_path: Path) -> None:
    registry = AdapterRegistry()

    assert registry.build(_job(agent=Agent.CLAUDE, workdir=tmp_path)).argv == (
        "claude",
        "--dangerously-skip-permissions",
        "-p",
        "prompt",
    )
    assert registry.build(_job(agent=Agent.CODEX, workdir=tmp_path)).argv == (
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(tmp_path),
        "prompt",
    )
    assert registry.build(_job(agent=Agent.COPILOT, workdir=tmp_path)).argv == (
        "copilot",
        "-C",
        str(tmp_path),
        "--allow-all",
        "--no-remote",
        "--output-format",
        "text",
        "-p",
        "prompt",
    )
    assert registry.build(_job(agent=Agent.CURSOR, workdir=tmp_path)).argv == (
        "cursor-agent",
        "--workspace",
        str(tmp_path),
        "--print",
        "--force",
        "--trust",
        "prompt",
    )
    assert registry.build(_job(agent=Agent.DEVIN, workdir=tmp_path)).argv == (
        "devin",
        "--permission-mode",
        "dangerous",
        "--respect-workspace-trust",
        "true",
        "-p",
        "prompt",
    )
    assert registry.build(_job(agent=Agent.GEMINI, workdir=tmp_path)).argv == (
        "gemini",
        "-m",
        "gemini-3.1-pro-preview",
        "--yolo",
        "--skip-trust",
        "-p",
        "prompt",
    )
    assert registry.build(_job(agent=Agent.OPENCODE, workdir=tmp_path)).argv == (
        "opencode",
        "run",
        "--dir",
        str(tmp_path),
        "--dangerously-skip-permissions",
        "prompt",
    )
    hermes_spec = registry.build(_job(agent=Agent.HERMES, workdir=tmp_path))
    assert hermes_spec.argv == (
        "hermes",
        "--accept-hooks",
        "--yolo",
        "-z",
        "prompt",
    )
    assert hermes_spec.env == {"HERMES_ACCEPT_HOOKS": "1"}
    assert registry.build(_job(agent=Agent.OPENCLAW, workdir=tmp_path)).argv == (
        "openclaw",
        "agent",
        "--local",
        "--session-id",
        "agent-job-scheduler-openclaw-job",
        "--message",
        "prompt",
        "--timeout",
        "600",
    )


def _job(*, agent: Agent, workdir: Path, prompt: str = "prompt") -> JobRecord:
    return JobRecord(
        job_id=f"{agent.value}-job",
        created_at="2026-04-19T09:00:00+09:00",
        scheduled_at="2026-04-19T09:00:00+09:00",
        status=JobStatus.QUEUED,
        agent=agent,
        workdir=str(workdir),
        prompt=prompt,
    )
