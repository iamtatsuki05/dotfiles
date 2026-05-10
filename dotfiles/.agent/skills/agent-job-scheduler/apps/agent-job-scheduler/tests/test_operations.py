from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_job_scheduler.adapters import AdapterRegistry
from agent_job_scheduler.ledger import Ledger
from agent_job_scheduler.models import ActiveRun, Agent, CommandSpec, JobRecord, JobStatus
from agent_job_scheduler.runtime import build_runtime_paths, ensure_runtime_layout
from agent_job_scheduler.scheduler import Scheduler
from agent_job_scheduler.settings import SchedulerSettings, load_settings
from agent_job_scheduler.timeutil import parse_timestamp


def test_retry_cancel_and_requeue_flow(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    paths = build_runtime_paths(runtime_root)
    ensure_runtime_layout(paths)
    ledger = Ledger(paths)
    workdir = tmp_path / "workspace"
    workdir.mkdir()

    failed_job = _job(
        job_id="failed-job",
        status=JobStatus.FAILED,
        agent=Agent.CODEX,
        workdir=workdir,
        prompt="fix it",
    )
    ledger.write_jobs([failed_job])
    scheduler = Scheduler(paths=paths)

    retried = scheduler.retry_job("failed-job", scheduled_at=parse_timestamp("2026-04-19T12:00:00+09:00"))
    assert retried.status == JobStatus.QUEUED

    cancelled = scheduler.cancel_job("failed-job")
    assert cancelled.status == JobStatus.CANCELLED

    new_job = scheduler.requeue_job("failed-job", scheduled_at=parse_timestamp("2026-04-19T12:30:00+09:00"))
    all_jobs = ledger.list_jobs()
    assert len(all_jobs) == 2
    assert new_job.job_id != "failed-job"
    assert new_job.status == JobStatus.QUEUED


def test_stale_running_job_is_recovered_on_status(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    paths = build_runtime_paths(runtime_root)
    ensure_runtime_layout(paths)
    ledger = Ledger(paths)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    ledger.write_jobs(
        [
            _job(
                job_id="stale-running",
                status=JobStatus.RUNNING,
                agent=Agent.CLAUDE,
                workdir=workdir,
                prompt="continue",
                started_at="2026-04-19T09:00:00+09:00",
            )
        ]
    )

    scheduler = Scheduler(paths=paths)
    snapshot = scheduler.status(now=parse_timestamp("2026-04-19T10:00:00+09:00"))
    job = snapshot.jobs[0]

    assert job.status == JobStatus.RETRY_WAITING
    assert job.last_error.startswith("stale running job recovered")
    assert snapshot.runnable_jobs[0].job_id == "stale-running"


def test_allowlist_can_be_enabled_and_blocks_unknown_workdir(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    scheduler = Scheduler(runtime_root=runtime_root)
    allowed = tmp_path / "allowed"
    blocked = tmp_path / "blocked"
    allowed.mkdir()
    blocked.mkdir()

    scheduler.allow_workdir(allowed)
    scheduler.set_allowlist_enforcement(enabled=True)

    with pytest.raises(ValueError, match="workdir is not in allowlist"):
        scheduler.enqueue(agent=Agent.GEMINI, workdir=blocked, prompt="nope")

    job = scheduler.enqueue(agent=Agent.GEMINI, workdir=allowed, prompt="ok")
    settings = load_settings(build_runtime_paths(runtime_root))

    assert job.workdir == str(allowed.resolve())
    assert settings.enforce_workdir_allowlist is True
    assert str(allowed.resolve()) in settings.allowed_workdirs


def test_set_stale_running_timeout_updates_settings(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    scheduler = Scheduler(runtime_root=runtime_root)

    settings = scheduler.set_stale_running_timeout(seconds=42)

    assert settings.stale_running_timeout_seconds == 42
    assert load_settings(build_runtime_paths(runtime_root)).stale_running_timeout_seconds == 42


def test_settings_merge_new_default_rate_limit_profiles_with_existing_overrides() -> None:
    settings = SchedulerSettings.model_validate(
        {
            "rate_limit_profiles": {
                "codex": {
                    "markers": ["custom marker"],
                    "default_backoff_seconds": 123,
                }
            }
        }
    )

    assert set(settings.rate_limit_profiles) == {agent.value for agent in Agent}
    assert settings.rate_limit_profiles["codex"].markers == ["custom marker"]
    assert settings.rate_limit_profiles["codex"].default_backoff_seconds == 123


def test_list_active_runs_returns_sorted_records(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    paths = build_runtime_paths(runtime_root)
    ensure_runtime_layout(paths)
    ledger = Ledger(paths)
    ledger.save_active_runs(
        {
            "later": ActiveRun(
                job_id="later",
                agent=Agent.GEMINI,
                pid=222,
                started_at="2026-04-19T12:01:00+09:00",
                workdir="/tmp/later",
                command="gemini -p later",
            ),
            "earlier": ActiveRun(
                job_id="earlier",
                agent=Agent.CODEX,
                pid=111,
                started_at="2026-04-19T12:00:00+09:00",
                workdir="/tmp/earlier",
                command="codex exec earlier",
            ),
        }
    )

    active_runs = Scheduler(paths=paths).list_active_runs()

    assert [run.job_id for run in active_runs] == ["earlier", "later"]


def test_show_job_returns_artifact_excerpt(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    paths = build_runtime_paths(runtime_root)
    ensure_runtime_layout(paths)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    result_path = tmp_path / "result.txt"
    transcript_path = tmp_path / "transcript.txt"
    result_path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    transcript_path.write_text("a\nb\nc\n", encoding="utf-8")

    Ledger(paths).write_jobs(
        [
            JobRecord(
                job_id="show-job",
                created_at="2026-04-19T09:00:00+09:00",
                scheduled_at="2026-04-19T09:00:00+09:00",
                status=JobStatus.SUCCEEDED,
                agent=Agent.CODEX,
                workdir=str(workdir),
                prompt="show",
                result_path=str(result_path),
                transcript_path=str(transcript_path),
            )
        ]
    )

    payload = Scheduler(paths=paths).show_job("show-job", tail_lines=2)

    assert payload["result_excerpt"] == "two\nthree"
    assert payload["transcript_excerpt"] == "b\nc"
    assert "prompt" not in payload


def test_show_job_can_include_masked_prompt(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    scheduler = Scheduler(runtime_root=runtime_root)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    job = scheduler.enqueue(
        agent=Agent.CODEX,
        workdir=workdir,
        prompt="OPENAI_API_KEY=sk-secret-value",
    )

    payload = scheduler.show_job(job.job_id, include_prompt=True)

    assert payload["prompt"] == "OPENAI_API_KEY=<redacted>"


def test_cancel_running_job_terminates_process_and_marks_cancelled(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    paths = build_runtime_paths(runtime_root)
    ensure_runtime_layout(paths)
    ledger = Ledger(paths)
    workdir = tmp_path / "workspace"
    workdir.mkdir()

    scheduler = Scheduler(
        runtime_root=runtime_root,
        registry=AdapterRegistry(
            {
                Agent.CODEX: _sleep_builder(),
                Agent.CLAUDE: _sleep_builder(),
                Agent.GEMINI: _sleep_builder(),
            }
        ),
    )
    job = scheduler.enqueue(
        agent=Agent.CODEX,
        workdir=workdir,
        prompt="sleep",
        scheduled_at=parse_timestamp("2026-04-19T11:59:00+09:00"),
    )
    done: dict[str, object] = {}

    def run_scheduler() -> None:
        outcomes = scheduler.run_once(now=parse_timestamp("2026-04-19T12:00:00+09:00"))
        done["outcomes"] = outcomes

    thread = threading.Thread(target=run_scheduler)
    thread.start()

    active_runs_path = build_runtime_paths(runtime_root).active_runs_json
    for _ in range(100):
        active_runs = json.loads(active_runs_path.read_text(encoding="utf-8"))
        if job.job_id in active_runs:
            break
        time.sleep(0.05)
    else:
        msg = "active run was not registered in time"
        raise AssertionError(msg)

    cancelled = scheduler.cancel_job(job.job_id)
    thread.join(timeout=10)
    if thread.is_alive():
        msg = "scheduler thread did not finish after cancellation"
        raise AssertionError(msg)

    latest_job = ledger.list_jobs()[0]

    assert cancelled.status == JobStatus.CANCELLED
    assert latest_job.status == JobStatus.CANCELLED
    assert latest_job.last_error == "cancelled by user"
    assert ledger.load_active_runs() == {}


def _job(
    *,
    job_id: str,
    status: JobStatus,
    agent: Agent,
    workdir: Path,
    prompt: str,
    started_at: str = "",
) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        created_at="2026-04-19T09:00:00+09:00",
        scheduled_at="2026-04-19T09:05:00+09:00",
        status=status,
        agent=agent,
        workdir=str(workdir),
        prompt=prompt,
        started_at=started_at,
    )


def _sleep_builder():
    def build(job: JobRecord) -> CommandSpec:
        argv = (
            sys.executable,
            "-c",
            "import time; time.sleep(10)",
        )
        return CommandSpec(argv=argv, cwd=Path(job.workdir), display_command=" ".join(argv))

    return build
