from __future__ import annotations

from datetime import datetime


def now_local() -> datetime:
    return datetime.now().astimezone()


def format_timestamp(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if not normalized:
        msg = "timestamp is required"
        raise ValueError(msg)
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now_local().tzinfo)
    return parsed
