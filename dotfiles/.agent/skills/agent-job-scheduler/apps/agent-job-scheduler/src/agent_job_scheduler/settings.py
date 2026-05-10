from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator

from .fileio import atomic_write_json
from .models import Agent, SchedulerModel
from .runtime import RuntimePaths

DEFAULT_STALE_RUNNING_TIMEOUT_SECONDS = 1800
DEFAULT_PROMPT_PREVIEW_CHARS = 160
DEFAULT_RATE_LIMIT_PROFILES = {
    Agent.CLAUDE.value: {
        "markers": [
            "rate limit",
            "too many requests",
            "usage limit",
            "try again in",
            "retry after",
        ],
        "default_backoff_seconds": 900,
    },
    Agent.CODEX.value: {
        "markers": [
            "rate limit",
            "too many requests",
            "usage limit",
            "retry after",
            "tokens per minute",
        ],
        "default_backoff_seconds": 900,
    },
    Agent.COPILOT.value: {
        "markers": [
            "rate limit",
            "too many requests",
            "quota exceeded",
            "usage limit",
            "retry after",
        ],
        "default_backoff_seconds": 900,
    },
    Agent.CURSOR.value: {
        "markers": [
            "rate limit",
            "too many requests",
            "quota exceeded",
            "usage limit",
            "retry after",
        ],
        "default_backoff_seconds": 900,
    },
    Agent.DEVIN.value: {
        "markers": [
            "rate limit",
            "too many requests",
            "quota exceeded",
            "usage limit",
            "retry after",
        ],
        "default_backoff_seconds": 900,
    },
    Agent.GEMINI.value: {
        "markers": [
            "rate limit",
            "too many requests",
            "quota exceeded",
            "resource has been exhausted",
            "retry after",
        ],
        "default_backoff_seconds": 900,
    },
    Agent.HERMES.value: {
        "markers": [
            "rate limit",
            "too many requests",
            "quota exceeded",
            "usage limit",
            "retry after",
        ],
        "default_backoff_seconds": 900,
    },
    Agent.OPENCODE.value: {
        "markers": [
            "rate limit",
            "too many requests",
            "quota exceeded",
            "usage limit",
            "retry after",
        ],
        "default_backoff_seconds": 900,
    },
}


class RateLimitProfile(SchedulerModel):
    markers: list[str] = Field(default_factory=list)
    default_backoff_seconds: int = 900

    @field_validator("markers", mode="before")
    @classmethod
    def _normalize_markers(cls, value: Any) -> list[str]:
        return [str(item).lower() for item in (value or [])]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class SchedulerSettings(SchedulerModel):
    allowed_workdirs: list[str] = Field(default_factory=list)
    enforce_workdir_allowlist: bool = False
    stale_running_timeout_seconds: int = DEFAULT_STALE_RUNNING_TIMEOUT_SECONDS
    store_prompt_body_in_csv: bool = False
    prompt_preview_chars: int = DEFAULT_PROMPT_PREVIEW_CHARS
    rate_limit_profiles: dict[str, RateLimitProfile] = Field(default_factory=dict)

    @field_validator("allowed_workdirs", mode="before")
    @classmethod
    def _normalize_allowed_workdirs(cls, value: Any) -> list[str]:
        return sorted({str(item) for item in (value or [])})

    @model_validator(mode="before")
    @classmethod
    def _apply_defaults(cls, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        normalized = dict(payload)
        profiles = dict(DEFAULT_RATE_LIMIT_PROFILES)
        profiles.update(normalized.get("rate_limit_profiles") or {})
        normalized["rate_limit_profiles"] = profiles
        return normalized

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def default_settings() -> SchedulerSettings:
    return SchedulerSettings.model_validate({})


def load_settings(paths: RuntimePaths) -> SchedulerSettings:
    raw = paths.settings_json.read_text(encoding="utf-8").strip()
    if not raw:
        return default_settings()
    return SchedulerSettings.model_validate(json.loads(raw))


def save_settings(paths: RuntimePaths, settings: SchedulerSettings) -> None:
    atomic_write_json(paths.settings_json, settings.to_dict())


def normalize_workdir(path: Path) -> str:
    return str(path.expanduser().resolve())


def is_workdir_allowed(workdir: Path, settings: SchedulerSettings) -> bool:
    if not settings.enforce_workdir_allowlist:
        return True
    candidate = workdir.expanduser().resolve()
    for allowed in settings.allowed_workdirs:
        allowed_path = Path(allowed).expanduser().resolve()
        if candidate == allowed_path or allowed_path in candidate.parents:
            return True
    return False


def prompt_preview(prompt: str, *, max_chars: int) -> str:
    compact = " ".join(prompt.split())
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 3]}..."
