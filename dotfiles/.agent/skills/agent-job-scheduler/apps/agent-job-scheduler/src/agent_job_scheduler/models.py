from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Agent(StrEnum):
    ANTIGRAVITY = "antigravity"
    CLAUDE = "claude"
    CODEX = "codex"
    COPILOT = "copilot"
    CURSOR = "cursor"
    DEVIN = "devin"
    HERMES = "hermes"
    OPENCODE = "opencode"
    OPENCLAW = "openclaw"


class JobStatus(StrEnum):
    CANCELLED = "cancelled"
    FAILED = "failed"
    QUEUED = "queued"
    RETRY_WAITING = "retry_waiting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"


JOB_FIELDNAMES = [
    "job_id",
    "created_at",
    "scheduled_at",
    "status",
    "agent",
    "workdir",
    "prompt",
    "prompt_path",
    "conversation_hash",
    "last_response",
    "started_at",
    "finished_at",
    "run_count",
    "next_retry_at",
    "last_error",
    "result_path",
    "transcript_path",
]


class SchedulerModel(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)


class JobRecord(SchedulerModel):
    job_id: str
    created_at: str
    scheduled_at: str
    status: JobStatus
    agent: Agent
    workdir: str
    prompt: str
    prompt_path: str = ""
    conversation_hash: str = ""
    last_response: str = ""
    started_at: str = ""
    finished_at: str = ""
    run_count: int = 0
    next_retry_at: str = ""
    last_error: str = ""
    result_path: str = ""
    transcript_path: str = ""

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "JobRecord":
        return cls.model_validate(
            {
                "job_id": row["job_id"],
                "created_at": row["created_at"],
                "scheduled_at": row["scheduled_at"],
                "status": row["status"],
                "agent": row["agent"],
                "workdir": row["workdir"],
                "prompt": row["prompt"],
                "prompt_path": row.get("prompt_path", ""),
                "conversation_hash": row.get("conversation_hash", ""),
                "last_response": row.get("last_response", ""),
                "started_at": row.get("started_at", ""),
                "finished_at": row.get("finished_at", ""),
                "run_count": row.get("run_count", "0") or "0",
                "next_retry_at": row.get("next_retry_at", ""),
                "last_error": row.get("last_error", ""),
                "result_path": row.get("result_path", ""),
                "transcript_path": row.get("transcript_path", ""),
            }
        )

    def to_row(self) -> dict[str, str]:
        return {
            "job_id": self.job_id,
            "created_at": self.created_at,
            "scheduled_at": self.scheduled_at,
            "status": self.status.value,
            "agent": self.agent.value,
            "workdir": self.workdir,
            "prompt": self.prompt,
            "prompt_path": self.prompt_path,
            "conversation_hash": self.conversation_hash,
            "last_response": self.last_response,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "run_count": str(self.run_count),
            "next_retry_at": self.next_retry_at,
            "last_error": self.last_error,
            "result_path": self.result_path,
            "transcript_path": self.transcript_path,
        }


class AgentState(SchedulerModel):
    agent: Agent
    blocked_until: str = ""
    observed_at: str = ""
    reason: str = ""

    @classmethod
    def from_dict(cls, agent: Agent, payload: dict[str, Any]) -> "AgentState":
        return cls.model_validate({"agent": agent, **payload})

    def to_dict(self) -> dict[str, str]:
        return {
            "blocked_until": self.blocked_until,
            "observed_at": self.observed_at,
            "reason": self.reason,
        }


class CommandSpec(SchedulerModel):
    argv: tuple[str, ...]
    cwd: Path
    display_command: str
    env: dict[str, str] = Field(default_factory=dict)


class ExecutionResult(SchedulerModel):
    command: CommandSpec
    exit_code: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    pid: int | None = None


class ActiveRun(SchedulerModel):
    job_id: str
    agent: Agent
    pid: int
    started_at: str
    workdir: str
    command: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ActiveRun":
        return cls.model_validate(payload)

    def to_dict(self) -> dict[str, str | int]:
        return {
            "job_id": self.job_id,
            "agent": self.agent.value,
            "pid": self.pid,
            "started_at": self.started_at,
            "workdir": self.workdir,
            "command": self.command,
        }


class RateLimitWindow(SchedulerModel):
    blocked_until: str
    reason: str


class JobRunOutcome(SchedulerModel):
    job_id: str
    agent: Agent
    status: JobStatus
    exit_code: int
    message: str
    result_path: str


class SchedulerSnapshot(SchedulerModel):
    jobs: list[JobRecord]
    due_jobs: list[JobRecord]
    runnable_jobs: list[JobRecord]
    agent_states: dict[Agent, AgentState]
