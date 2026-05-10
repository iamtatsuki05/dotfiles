from __future__ import annotations

import re
from datetime import timedelta

from .models import Agent, RateLimitWindow
from .settings import RateLimitProfile, SchedulerSettings
from .timeutil import format_timestamp, parse_timestamp

ISO_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
DURATION_RE = re.compile(
    r"(?P<value>\d+)\s*(?P<unit>hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)",
    re.IGNORECASE,
)


def detect_rate_limit(
    agent: Agent,
    stdout: str,
    stderr: str,
    observed_at: str,
    settings: SchedulerSettings,
) -> RateLimitWindow | None:
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    if not combined:
        return None

    lowered = combined.lower()
    profile = settings.rate_limit_profiles.get(agent.value)
    markers = profile.markers if profile is not None else []
    if not any(marker in lowered for marker in markers):
        return None

    observed = parse_timestamp(observed_at)
    blocked_until = _extract_blocked_until(combined, observed, profile)
    reason = _extract_reason(combined, markers)
    return RateLimitWindow(
        blocked_until=format_timestamp(blocked_until),
        reason=reason,
    )


def _extract_blocked_until(message: str, observed_at, profile: RateLimitProfile | None):
    for match in ISO_TIMESTAMP_RE.finditer(message):
        try:
            return parse_timestamp(match.group(0))
        except ValueError:
            continue

    matches = list(DURATION_RE.finditer(message))
    if matches:
        delta = timedelta()
        for match in matches:
            value = int(match.group("value"))
            unit = match.group("unit").lower()
            if unit.startswith("h"):
                delta += timedelta(hours=value)
            elif unit.startswith("m"):
                delta += timedelta(minutes=value)
            elif unit.startswith("s"):
                delta += timedelta(seconds=value)
        if delta.total_seconds() > 0:
            return observed_at + delta

    backoff_seconds = profile.default_backoff_seconds if profile is not None else 900
    return observed_at + timedelta(seconds=backoff_seconds)


def _extract_reason(message: str, markers: list[str]) -> str:
    for line in message.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in markers):
            return line.strip()[:500]
    return "rate limit detected"
