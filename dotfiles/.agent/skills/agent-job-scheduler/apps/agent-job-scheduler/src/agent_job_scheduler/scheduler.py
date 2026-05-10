from __future__ import annotations

import hashlib
import os
import signal
import time
import uuid
from collections import Counter
from datetime import timedelta
from pathlib import Path

from .adapters import AdapterRegistry
from .fileio import atomic_write_json, atomic_write_text
from .ledger import Ledger
from .locking import exclusive_lock
from .masking import mask_text
from .models import ActiveRun, Agent, AgentState, ExecutionResult, JobRecord, JobRunOutcome, JobStatus, SchedulerSnapshot
from .rate_limit import detect_rate_limit
from .runner import CommandRunner, SubprocessRunner
from .runtime import RuntimePaths, build_runtime_paths, ensure_runtime_layout
from .settings import (
    SchedulerSettings,
    is_workdir_allowed,
    load_settings,
    normalize_workdir,
    prompt_preview,
    save_settings,
)
from .timeutil import format_timestamp, now_local, parse_timestamp

LAST_RESPONSE_LIMIT = 4000
READY_STATUSES = {JobStatus.QUEUED, JobStatus.RETRY_WAITING}


class Scheduler:
    def __init__(
        self,
        runtime_root: Path | None = None,
        *,
        paths: RuntimePaths | None = None,
        registry: AdapterRegistry | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self.paths = paths or build_runtime_paths(runtime_root)
        self.ledger = Ledger(self.paths)
        self.registry = registry or AdapterRegistry()
        self.runner = runner or SubprocessRunner()

    def enqueue(
        self,
        *,
        agent: Agent,
        workdir: Path,
        prompt: str,
        scheduled_at=None,
    ) -> JobRecord:
        ensure_runtime_layout(self.paths)
        resolved_workdir = workdir.expanduser().resolve()
        if not resolved_workdir.is_dir():
            msg = f"workdir does not exist or is not a directory: {resolved_workdir}"
            raise ValueError(msg)

        settings = self.load_settings()
        _ensure_workdir_allowed(resolved_workdir, settings)

        scheduled = scheduled_at or now_local()
        created = now_local()
        job_id = uuid.uuid4().hex
        prompt_file = self.ledger.scheduler_prompt_path(job_id)
        self.ledger.write_prompt(prompt_file, prompt)
        job = JobRecord(
            job_id=job_id,
            created_at=format_timestamp(created),
            scheduled_at=format_timestamp(scheduled),
            status=JobStatus.QUEUED,
            agent=agent,
            workdir=str(resolved_workdir),
            prompt=_stored_prompt_value(prompt, settings),
            prompt_path=str(prompt_file),
        )

        with exclusive_lock(self.paths.ledger_lock):
            self.ledger.append_job(job)
        return job

    def status(self, *, now=None) -> SchedulerSnapshot:
        ensure_runtime_layout(self.paths)
        reference = now or now_local()
        with exclusive_lock(self.paths.ledger_lock):
            jobs = self.ledger.list_jobs()
            states = self.ledger.load_agent_states()
            active_runs = self.ledger.load_active_runs()
            settings = self.load_settings()
            if _recover_stale_running_jobs(jobs, active_runs, reference, settings):
                self.ledger.write_jobs(jobs)
                self.ledger.save_active_runs(active_runs)
        due_jobs = [job for job in jobs if _is_job_due(job, states, reference)]
        runnable_jobs = _select_due_jobs(jobs, states, reference)
        return SchedulerSnapshot(
            jobs=jobs,
            due_jobs=due_jobs,
            runnable_jobs=runnable_jobs,
            agent_states=states,
        )

    def run_once(self, *, now=None) -> list[JobRunOutcome]:
        ensure_runtime_layout(self.paths)
        reference = now or now_local()
        outcomes: list[JobRunOutcome] = []

        with exclusive_lock(self.paths.scheduler_lock):
            with exclusive_lock(self.paths.ledger_lock):
                jobs = self.ledger.list_jobs()
                states = self.ledger.load_agent_states()
                active_runs = self.ledger.load_active_runs()
                settings = self.load_settings()
                if _recover_stale_running_jobs(jobs, active_runs, reference, settings):
                    self.ledger.write_jobs(jobs)
                    self.ledger.save_active_runs(active_runs)
                selected_jobs = _select_due_jobs(jobs, states, reference)

            for job in selected_jobs:
                outcomes.append(self._run_single_job(job))

        return outcomes

    def show_job(self, job_id: str, *, tail_lines: int = 20, include_prompt: bool = False) -> dict[str, object]:
        ensure_runtime_layout(self.paths)
        with exclusive_lock(self.paths.ledger_lock):
            jobs = self.ledger.list_jobs()
            _, job = self.ledger.find_job(jobs, job_id)

        payload: dict[str, object] = job.to_row()
        payload["tail_lines"] = tail_lines
        payload["result_excerpt"] = _read_tail(job.result_path, tail_lines)
        payload["transcript_excerpt"] = _read_tail(job.transcript_path, tail_lines)
        payload["prompt_preview"] = job.prompt
        if not include_prompt:
            payload.pop("prompt", None)
        else:
            payload["prompt"] = mask_text(self._resolve_prompt(job))
        return payload

    def cancel_job(self, job_id: str) -> JobRecord:
        ensure_runtime_layout(self.paths)
        target_pid: int | None = None
        with exclusive_lock(self.paths.ledger_lock):
            jobs = self.ledger.list_jobs()
            active_runs = self.ledger.load_active_runs()
            index, job = self.ledger.find_job(jobs, job_id)
            if job.status == JobStatus.RUNNING:
                active_run = active_runs.get(job_id)
                if active_run is not None and _pid_is_alive(active_run.pid):
                    target_pid = active_run.pid
            job.status = JobStatus.CANCELLED
            job.finished_at = format_timestamp(now_local())
            job.next_retry_at = ""
            job.last_error = "cancelled by user"
            jobs[index] = job
            self.ledger.write_jobs(jobs)

        if target_pid is not None:
            _terminate_pid(target_pid)

        with exclusive_lock(self.paths.ledger_lock):
            active_runs = self.ledger.load_active_runs()
            active_runs.pop(job_id, None)
            self.ledger.save_active_runs(active_runs)
            jobs = self.ledger.list_jobs()
            _, latest_job = self.ledger.find_job(jobs, job_id)
            return latest_job

    def retry_job(self, job_id: str, *, scheduled_at=None) -> JobRecord:
        ensure_runtime_layout(self.paths)
        when = scheduled_at or now_local()
        with exclusive_lock(self.paths.ledger_lock):
            jobs = self.ledger.list_jobs()
            index, job = self.ledger.find_job(jobs, job_id)
            if job.status == JobStatus.RUNNING:
                msg = "cannot retry a running job"
                raise ValueError(msg)
            job.status = JobStatus.QUEUED
            job.scheduled_at = format_timestamp(when)
            job.started_at = ""
            job.finished_at = ""
            job.next_retry_at = ""
            job.last_error = ""
            job.result_path = ""
            job.transcript_path = ""
            job.last_response = ""
            job.conversation_hash = ""
            jobs[index] = job
            self.ledger.write_jobs(jobs)
            return job

    def requeue_job(self, job_id: str, *, scheduled_at=None) -> JobRecord:
        ensure_runtime_layout(self.paths)
        with exclusive_lock(self.paths.ledger_lock):
            jobs = self.ledger.list_jobs()
            _, source = self.ledger.find_job(jobs, job_id)

        return self.enqueue(
            agent=source.agent,
            workdir=Path(source.workdir),
            prompt=self._resolve_prompt(source),
            scheduled_at=scheduled_at,
        )

    def load_settings(self) -> SchedulerSettings:
        ensure_runtime_layout(self.paths)
        return load_settings(self.paths)

    def show_settings(self) -> dict[str, object]:
        settings = self.load_settings()
        return settings.to_dict()

    def list_active_runs(self) -> list[ActiveRun]:
        ensure_runtime_layout(self.paths)
        with exclusive_lock(self.paths.ledger_lock):
            active_runs = self.ledger.load_active_runs()
        return sorted(active_runs.values(), key=lambda run: (run.started_at, run.job_id))

    def allow_workdir(self, workdir: Path) -> SchedulerSettings:
        ensure_runtime_layout(self.paths)
        resolved = workdir.expanduser().resolve()
        if not resolved.is_dir():
            msg = f"workdir does not exist or is not a directory: {resolved}"
            raise ValueError(msg)
        normalized = normalize_workdir(resolved)
        with exclusive_lock(self.paths.ledger_lock):
            settings = self.load_settings()
            if normalized not in settings.allowed_workdirs:
                settings.allowed_workdirs.append(normalized)
                settings.allowed_workdirs.sort()
                save_settings(self.paths, settings)
            return settings

    def disallow_workdir(self, workdir: Path) -> SchedulerSettings:
        ensure_runtime_layout(self.paths)
        normalized = normalize_workdir(workdir)
        with exclusive_lock(self.paths.ledger_lock):
            settings = self.load_settings()
            settings.allowed_workdirs = [item for item in settings.allowed_workdirs if item != normalized]
            save_settings(self.paths, settings)
            return settings

    def set_allowlist_enforcement(self, *, enabled: bool) -> SchedulerSettings:
        ensure_runtime_layout(self.paths)
        with exclusive_lock(self.paths.ledger_lock):
            settings = self.load_settings()
            settings.enforce_workdir_allowlist = enabled
            save_settings(self.paths, settings)
            return settings

    def set_stale_running_timeout(self, *, seconds: int) -> SchedulerSettings:
        ensure_runtime_layout(self.paths)
        if seconds < 0:
            msg = "stale running timeout must be zero or greater"
            raise ValueError(msg)
        with exclusive_lock(self.paths.ledger_lock):
            settings = self.load_settings()
            settings.stale_running_timeout_seconds = seconds
            save_settings(self.paths, settings)
            return settings

    def _resolve_prompt(self, job: JobRecord) -> str:
        if job.prompt_path:
            prompt_path = Path(job.prompt_path)
            if prompt_path.is_file():
                return prompt_path.read_text(encoding="utf-8")
        return job.prompt

    def _run_single_job(self, job: JobRecord) -> JobRunOutcome:
        started_at = now_local()
        settings = self.load_settings()

        with exclusive_lock(self.paths.ledger_lock):
            jobs = self.ledger.list_jobs()
            job_index, current_job = self.ledger.find_job(jobs, job.job_id)
            current_job.status = JobStatus.RUNNING
            current_job.started_at = format_timestamp(started_at)
            current_job.finished_at = ""
            current_job.last_error = ""
            current_job.next_retry_at = ""
            current_job.run_count += 1
            jobs[job_index] = current_job
            self.ledger.write_jobs(jobs)

        execution = self._execute_job(current_job)
        rate_limit_window = detect_rate_limit(
            current_job.agent,
            execution.stdout,
            execution.stderr,
            execution.finished_at,
            settings,
        )
        artifacts = _write_artifacts(self.paths, current_job, execution)

        with exclusive_lock(self.paths.ledger_lock):
            jobs = self.ledger.list_jobs()
            states = self.ledger.load_agent_states()
            active_runs = self.ledger.load_active_runs()
            active_runs.pop(current_job.job_id, None)
            job_index, latest_job = self.ledger.find_job(jobs, current_job.job_id)
            latest_job.finished_at = execution.finished_at
            latest_job.result_path = artifacts["result_path"]
            latest_job.transcript_path = artifacts["transcript_path"]
            latest_job.conversation_hash = artifacts["conversation_hash"]
            latest_job.last_response = artifacts["last_response"]

            if latest_job.status == JobStatus.CANCELLED:
                latest_job.next_retry_at = ""
                states.pop(latest_job.agent, None)
                message = latest_job.last_error or "cancelled by user"
            elif rate_limit_window is not None:
                latest_job.status = JobStatus.RETRY_WAITING
                latest_job.next_retry_at = rate_limit_window.blocked_until
                latest_job.last_error = rate_limit_window.reason
                states[latest_job.agent] = AgentState(
                    agent=latest_job.agent,
                    blocked_until=rate_limit_window.blocked_until,
                    observed_at=execution.finished_at,
                    reason=rate_limit_window.reason,
                )
                message = rate_limit_window.reason
            elif execution.exit_code == 0:
                latest_job.status = JobStatus.SUCCEEDED
                latest_job.next_retry_at = ""
                latest_job.last_error = ""
                states.pop(latest_job.agent, None)
                message = "completed"
            else:
                latest_job.status = JobStatus.FAILED
                latest_job.next_retry_at = ""
                latest_job.last_error = _extract_failure_message(execution)
                states.pop(latest_job.agent, None)
                message = latest_job.last_error

            jobs[job_index] = latest_job
            self.ledger.write_jobs(jobs)
            self.ledger.save_agent_states(states)
            self.ledger.save_active_runs(active_runs)

        return JobRunOutcome(
            job_id=current_job.job_id,
            agent=current_job.agent,
            status=latest_job.status,
            exit_code=execution.exit_code,
            message=message,
            result_path=latest_job.result_path,
        )

    def _execute_job(self, job: JobRecord) -> ExecutionResult:
        workdir = Path(job.workdir)
        if not workdir.is_dir():
            spec = self.registry.build(job)
            now = format_timestamp(now_local())
            return ExecutionResult(
                command=spec,
                exit_code=1,
                stdout="",
                stderr=f"workdir does not exist or is not a directory: {workdir}",
                started_at=now,
                finished_at=now,
                pid=None,
            )

        execution_job = job.model_copy(update={"prompt": self._resolve_prompt(job)})
        spec = self.registry.build(execution_job)
        return self.runner.run(spec, on_start=lambda pid: self._register_active_run(job, pid, spec.display_command))

    def _register_active_run(self, job: JobRecord, pid: int, display_command: str) -> None:
        with exclusive_lock(self.paths.ledger_lock):
            active_runs = self.ledger.load_active_runs()
            active_runs[job.job_id] = ActiveRun(
                job_id=job.job_id,
                agent=job.agent,
                pid=pid,
                started_at=format_timestamp(now_local()),
                workdir=job.workdir,
                command=display_command,
            )
            self.ledger.save_active_runs(active_runs)


def _select_due_jobs(
    jobs: list[JobRecord],
    agent_states: dict[Agent, AgentState],
    now,
) -> list[JobRecord]:
    selected: dict[Agent, JobRecord] = {}
    for job in sorted(jobs, key=_job_sort_key):
        if job.agent in selected:
            continue
        if not _is_job_due(job, agent_states, now):
            continue
        selected[job.agent] = job
    return sorted(selected.values(), key=_job_sort_key)


def _is_job_due(job: JobRecord, agent_states: dict[Agent, AgentState], now) -> bool:
    if job.status not in READY_STATUSES:
        return False
    if parse_timestamp(job.scheduled_at) > now:
        return False
    if job.next_retry_at and parse_timestamp(job.next_retry_at) > now:
        return False
    agent_state = agent_states.get(job.agent)
    if agent_state and agent_state.blocked_until:
        if parse_timestamp(agent_state.blocked_until) > now:
            return False
    return True


def _job_sort_key(job: JobRecord) -> tuple:
    return (parse_timestamp(job.created_at), job.job_id)


def _recover_stale_running_jobs(
    jobs: list[JobRecord],
    active_runs: dict[str, ActiveRun],
    reference,
    settings: SchedulerSettings,
) -> bool:
    timeout_seconds = settings.stale_running_timeout_seconds
    if timeout_seconds <= 0:
        return False

    recovered = False
    cutoff = reference - timedelta(seconds=timeout_seconds)
    recovery_time = format_timestamp(reference)
    for job in jobs:
        if job.status != JobStatus.RUNNING or not job.started_at:
            continue
        active_run = active_runs.get(job.job_id)
        if active_run is not None and _pid_is_alive(active_run.pid):
            continue
        if active_run is None and parse_timestamp(job.started_at) > cutoff:
            continue
        job.status = JobStatus.RETRY_WAITING
        job.finished_at = recovery_time
        job.next_retry_at = recovery_time
        if active_run is not None:
            job.last_error = f"running process {active_run.pid} disappeared; recovered to retry_waiting"
            active_runs.pop(job.job_id, None)
        else:
            job.last_error = f"stale running job recovered after {timeout_seconds} seconds"
        recovered = True
    return recovered


def _write_artifacts(paths: RuntimePaths, job: JobRecord, execution: ExecutionResult) -> dict[str, str]:
    started = parse_timestamp(execution.started_at)
    run_dir = paths.runs_dir / job.job_id / started.strftime("%Y%m%dT%H%M%S%f%z")
    run_dir.mkdir(parents=True, exist_ok=True)

    prompt_path = run_dir / "prompt.txt"
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    result_path = run_dir / "result.txt"
    transcript_path = run_dir / "transcript.txt"
    metadata_path = run_dir / "metadata.json"

    masked_prompt = mask_text(_resolved_prompt_for_artifact(job))
    masked_stdout = mask_text(execution.stdout)
    masked_stderr = mask_text(execution.stderr)

    atomic_write_text(prompt_path, masked_prompt)
    atomic_write_text(stdout_path, masked_stdout)
    atomic_write_text(stderr_path, masked_stderr)

    result_text = masked_stdout.strip() or masked_stderr.strip()
    atomic_write_text(result_path, result_text)

    transcript = _build_transcript(execution, stdout_text=masked_stdout, stderr_text=masked_stderr)
    atomic_write_text(transcript_path, transcript)

    metadata = {
        "agent": job.agent.value,
        "job_id": job.job_id,
        "workdir": job.workdir,
        "command": [mask_text(str(part)) for part in execution.command.argv],
        "display_command": mask_text(execution.command.display_command),
        "cwd": str(execution.command.cwd),
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
        "exit_code": execution.exit_code,
        "pid": execution.pid,
    }
    atomic_write_json(metadata_path, metadata)

    conversation_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    last_response = result_text[:LAST_RESPONSE_LIMIT]

    return {
        "result_path": str(result_path),
        "transcript_path": str(transcript_path),
        "conversation_hash": conversation_hash,
        "last_response": last_response,
    }


def _build_transcript(execution: ExecutionResult, *, stdout_text: str, stderr_text: str) -> str:
    return (
        f"$ {mask_text(execution.command.display_command)}\n"
        f"cwd: {execution.command.cwd}\n"
        f"started_at: {execution.started_at}\n"
        f"finished_at: {execution.finished_at}\n"
        f"exit_code: {execution.exit_code}\n\n"
        "## stdout\n"
        f"{stdout_text}\n"
        "## stderr\n"
        f"{stderr_text}"
    )


def _extract_failure_message(execution: ExecutionResult) -> str:
    for candidate in (mask_text(execution.stderr).strip(), mask_text(execution.stdout).strip()):
        if candidate:
            return candidate.splitlines()[-1][:500]
    return f"command exited with status {execution.exit_code}"


def _ensure_workdir_allowed(workdir: Path, settings: SchedulerSettings) -> None:
    if is_workdir_allowed(workdir, settings):
        return
    msg = f"workdir is not in allowlist: {workdir}"
    raise ValueError(msg)


def _read_tail(path_str: str, tail_lines: int) -> str:
    if not path_str or tail_lines <= 0:
        return ""
    path = Path(path_str)
    if not path.is_file():
        return ""
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    return "\n".join(lines[-tail_lines:])


def _stored_prompt_value(prompt: str, settings: SchedulerSettings) -> str:
    if settings.store_prompt_body_in_csv:
        return prompt
    return prompt_preview(prompt, max_chars=settings.prompt_preview_chars)


def _resolved_prompt_for_artifact(job: JobRecord) -> str:
    if job.prompt_path:
        prompt_path = Path(job.prompt_path)
        if prompt_path.is_file():
            return prompt_path.read_text(encoding="utf-8")
    return job.prompt


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_pid(pid: int, *, grace_seconds: float = 5.0) -> None:
    if not _pid_is_alive(pid):
        return
    _signal_process_or_group(pid, signal.SIGTERM)
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            return
        time.sleep(0.1)
    if _pid_is_alive(pid):
        _signal_process_or_group(pid, signal.SIGKILL)


def _signal_process_or_group(pid: int, sig: signal.Signals) -> None:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return

    try:
        if pgid == pid:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        return


def format_status_output(snapshot: SchedulerSnapshot, runtime_root: Path) -> str:
    counts = Counter(job.status.value for job in snapshot.jobs)
    lines = [f"runtime_root: {runtime_root}", "jobs:"]
    for status in JobStatus:
        lines.append(f"  {status.value}: {counts.get(status.value, 0)}")
    lines.append(f"  due_now: {len(snapshot.due_jobs)}")
    lines.append(f"  runnable_now: {len(snapshot.runnable_jobs)}")
    lines.append("cooldowns:")
    reference = now_local()
    for agent in Agent:
        state = snapshot.agent_states.get(agent)
        if state and state.blocked_until and parse_timestamp(state.blocked_until) > reference:
            lines.append(f"  {agent.value}: blocked_until={state.blocked_until}")
        else:
            lines.append(f"  {agent.value}: ready")

    if snapshot.runnable_jobs:
        lines.append("next_jobs:")
        for job in snapshot.runnable_jobs[:10]:
            lines.append(
                f"  {job.job_id} agent={job.agent.value} scheduled_at={job.scheduled_at} workdir={job.workdir}"
            )
    return "\n".join(lines)
