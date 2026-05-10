from __future__ import annotations

import csv
import json
from pathlib import Path

from .fileio import atomic_write_csv, atomic_write_json, atomic_write_text
from .models import ActiveRun, Agent, AgentState, JOB_FIELDNAMES, JobRecord
from .runtime import RuntimePaths, ensure_runtime_layout


class Ledger:
    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths

    def list_jobs(self) -> list[JobRecord]:
        ensure_runtime_layout(self.paths)
        with self.paths.jobs_csv.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            return [JobRecord.from_row(row) for row in reader]

    def write_jobs(self, jobs: list[JobRecord]) -> None:
        ensure_runtime_layout(self.paths)
        atomic_write_csv(
            self.paths.jobs_csv,
            JOB_FIELDNAMES,
            [job.to_row() for job in jobs],
        )

    def append_job(self, job: JobRecord) -> None:
        jobs = self.list_jobs()
        jobs.append(job)
        self.write_jobs(jobs)

    def load_agent_states(self) -> dict[Agent, AgentState]:
        ensure_runtime_layout(self.paths)
        raw = self.paths.agent_state_json.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        payload = json.loads(raw)
        states: dict[Agent, AgentState] = {}
        for name, state_payload in payload.items():
            agent = Agent(name)
            states[agent] = AgentState.from_dict(agent, state_payload)
        return states

    def save_agent_states(self, states: dict[Agent, AgentState]) -> None:
        ensure_runtime_layout(self.paths)
        payload = {
            agent.value: state.to_dict()
            for agent, state in sorted(states.items(), key=lambda item: item[0].value)
        }
        atomic_write_json(self.paths.agent_state_json, payload)

    def load_active_runs(self) -> dict[str, ActiveRun]:
        ensure_runtime_layout(self.paths)
        raw = self.paths.active_runs_json.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        payload = json.loads(raw)
        return {
            str(job_id): ActiveRun.from_dict(active_run)
            for job_id, active_run in payload.items()
        }

    def save_active_runs(self, active_runs: dict[str, ActiveRun]) -> None:
        ensure_runtime_layout(self.paths)
        payload = {
            job_id: active_run.to_dict()
            for job_id, active_run in sorted(active_runs.items())
        }
        atomic_write_json(self.paths.active_runs_json, payload)

    def find_job(self, jobs: list[JobRecord], job_id: str) -> tuple[int, JobRecord]:
        for index, job in enumerate(jobs):
            if job.job_id == job_id:
                return index, job
        msg = f"job not found: {job_id}"
        raise KeyError(msg)

    def prompt_path(self, run_dir: Path) -> Path:
        return run_dir / "prompt.txt"

    def scheduler_prompt_path(self, job_id: str) -> Path:
        return self.paths.prompts_dir / f"{job_id}.txt"

    def write_prompt(self, path: Path, prompt: str) -> None:
        atomic_write_text(path, prompt)
