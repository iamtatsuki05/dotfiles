from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

from agent_job_scheduler.adapters import AdapterRegistry
from agent_job_scheduler.models import Agent, AgentState, CommandSpec, JobRecord, JobStatus
from agent_job_scheduler.ledger import Ledger
from agent_job_scheduler.scheduler import Scheduler
from agent_job_scheduler.runtime import build_runtime_paths, ensure_runtime_layout
from agent_job_scheduler.timeutil import format_timestamp, now_local, parse_timestamp


def test_enqueue_creates_runtime_and_job(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    scheduler = Scheduler(runtime_root=runtime_root)
    workdir = tmp_path / "workspace"
    workdir.mkdir()

    job = scheduler.enqueue(agent=Agent.CODEX, workdir=workdir, prompt="hello from test")

    paths = build_runtime_paths(runtime_root)
    ledger = Ledger(paths)
    jobs = ledger.list_jobs()

    assert paths.jobs_csv.exists()
    assert paths.agent_state_json.exists()
    assert paths.settings_json.exists()
    assert job.job_id == jobs[0].job_id
    assert jobs[0].agent == Agent.CODEX
    assert jobs[0].status == JobStatus.QUEUED
    assert jobs[0].prompt_path
    assert Path(jobs[0].prompt_path).read_text(encoding="utf-8") == "hello from test"


def test_run_once_executes_oldest_due_job_per_agent(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    paths = build_runtime_paths(runtime_root)
    ensure_runtime_layout(paths)
    ledger = Ledger(paths)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    now = parse_timestamp("2026-04-19T12:00:00+09:00")

    jobs = [
        _job(
            job_id="codex-old",
            created_at="2026-04-19T09:00:00+09:00",
            scheduled_at="2026-04-19T11:00:00+09:00",
            agent=Agent.CODEX,
            workdir=workdir,
            prompt="first",
        ),
        _job(
            job_id="codex-new",
            created_at="2026-04-19T10:00:00+09:00",
            scheduled_at="2026-04-19T11:05:00+09:00",
            agent=Agent.CODEX,
            workdir=workdir,
            prompt="second",
        ),
        _job(
            job_id="claude-old",
            created_at="2026-04-19T08:30:00+09:00",
            scheduled_at="2026-04-19T11:10:00+09:00",
            agent=Agent.CLAUDE,
            workdir=workdir,
            prompt="third",
        ),
    ]
    ledger.write_jobs(jobs)

    scheduler = Scheduler(
        paths=paths,
        registry=AdapterRegistry(
            {
                Agent.CODEX: _builder("codex ok"),
                Agent.CLAUDE: _builder("claude ok"),
                Agent.ANTIGRAVITY: _builder("antigravity ok"),
                Agent.HERMES: _builder("hermes ok"),
            }
        ),
    )

    outcomes = scheduler.run_once(now=now)
    jobs_after = {job.job_id: job for job in ledger.list_jobs()}

    assert {outcome.job_id for outcome in outcomes} == {"codex-old", "claude-old"}
    assert jobs_after["codex-old"].status == JobStatus.SUCCEEDED
    assert jobs_after["claude-old"].status == JobStatus.SUCCEEDED
    assert jobs_after["codex-new"].status == JobStatus.QUEUED
    assert Path(jobs_after["codex-old"].result_path).exists()
    assert Path(jobs_after["claude-old"].transcript_path).exists()


def test_run_once_marks_rate_limited_job_and_agent_state(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    paths = build_runtime_paths(runtime_root)
    ensure_runtime_layout(paths)
    ledger = Ledger(paths)
    workdir = tmp_path / "workspace"
    workdir.mkdir()

    ledger.write_jobs(
        [
            _job(
                job_id="hermes-rate-limit",
                created_at="2026-04-19T09:00:00+09:00",
                scheduled_at="2026-04-19T09:05:00+09:00",
                agent=Agent.HERMES,
                workdir=workdir,
                prompt="rate limit test",
            )
        ]
    )

    scheduler = Scheduler(
        paths=paths,
        registry=AdapterRegistry(
            {
                Agent.CODEX: _builder("codex ok"),
                Agent.CLAUDE: _builder("claude ok"),
                Agent.ANTIGRAVITY: _builder("antigravity ok"),
                Agent.HERMES: _failing_builder("rate limit hit, retry after 2 minutes"),
            }
        ),
    )

    outcomes = scheduler.run_once(now=parse_timestamp("2026-04-19T12:00:00+09:00"))
    job = ledger.list_jobs()[0]
    states = ledger.load_agent_states()
    state = states[Agent.HERMES]

    assert outcomes[0].status == JobStatus.RETRY_WAITING
    assert job.status == JobStatus.RETRY_WAITING
    assert state.reason.startswith("rate limit")
    assert parse_timestamp(state.blocked_until) > parse_timestamp(state.observed_at)


def test_status_excludes_agent_blocked_by_cooldown(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    paths = build_runtime_paths(runtime_root)
    ensure_runtime_layout(paths)
    ledger = Ledger(paths)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    blocked_until = format_timestamp(now_local() + timedelta(minutes=10))

    ledger.write_jobs(
        [
            _job(
                job_id="codex-blocked",
                created_at="2026-04-19T09:00:00+09:00",
                scheduled_at="2026-04-19T09:05:00+09:00",
                agent=Agent.CODEX,
                workdir=workdir,
                prompt="blocked job",
            )
        ]
    )
    ledger.save_agent_states(
        {
            Agent.CODEX: AgentState(
                agent=Agent.CODEX,
                blocked_until=blocked_until,
                observed_at=format_timestamp(now_local()),
                reason="rate limit",
            )
        }
    )

    snapshot = Scheduler(paths=paths).status()

    assert snapshot.due_jobs == []
    assert snapshot.runnable_jobs == []


def test_run_once_masks_secret_like_output(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    paths = build_runtime_paths(runtime_root)
    ensure_runtime_layout(paths)
    ledger = Ledger(paths)
    workdir = tmp_path / "workspace"
    workdir.mkdir()

    ledger.write_jobs(
        [
            _job(
                job_id="codex-secret",
                created_at="2026-04-19T09:00:00+09:00",
                scheduled_at="2026-04-19T09:05:00+09:00",
                agent=Agent.CODEX,
                workdir=workdir,
                prompt="print OPENAI_API_KEY=sk-secret-value",
            )
        ]
    )

    scheduler = Scheduler(
        paths=paths,
        registry=AdapterRegistry(
            {
                Agent.CODEX: _builder("OPENAI_API_KEY=sk-secret-value"),
                Agent.CLAUDE: _builder("claude ok"),
                Agent.ANTIGRAVITY: _builder("antigravity ok"),
                Agent.HERMES: _builder("hermes ok"),
            }
        ),
    )

    scheduler.run_once(now=parse_timestamp("2026-04-19T12:00:00+09:00"))
    job = ledger.list_jobs()[0]
    result = Path(job.result_path).read_text(encoding="utf-8")

    assert "<redacted>" in result
    assert "sk-secret-value" not in result
    assert "<redacted>" in job.last_response


def _job(
    *,
    job_id: str,
    created_at: str,
    scheduled_at: str,
    agent: Agent,
    workdir: Path,
    prompt: str,
) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        created_at=created_at,
        scheduled_at=scheduled_at,
        status=JobStatus.QUEUED,
        agent=agent,
        workdir=str(workdir),
        prompt=prompt,
    )


def _builder(message: str):
    def build(job: JobRecord) -> CommandSpec:
        argv = (sys.executable, "-c", f"print({message!r})")
        return CommandSpec(argv=argv, cwd=Path(job.workdir), display_command=" ".join(argv))

    return build


def _failing_builder(message: str):
    def build(job: JobRecord) -> CommandSpec:
        argv = (
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.stderr.write({message!r} + '\\n'); "
                "raise SystemExit(1)"
            ),
        )
        return CommandSpec(argv=argv, cwd=Path(job.workdir), display_command=" ".join(argv))

    return build
