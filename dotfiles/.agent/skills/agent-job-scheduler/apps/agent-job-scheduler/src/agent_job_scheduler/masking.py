from __future__ import annotations

import re

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)([A-Za-z0-9._-]+)"),
    re.compile(
        r"(?i)\b(OPENAI_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY|GOOGLE_API_KEY|GITHUB_TOKEN|SLACK_WEBHOOK_URL|API_KEY|ACCESS_TOKEN|SECRET_KEY)\b(\s*[:=]\s*)([^\s\"']+)"
    ),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{10,})\b"),
    re.compile(r"\b(gsk_[A-Za-z0-9_-]{10,})\b"),
    re.compile(r"\b(AIza[0-9A-Za-z\-_]{20,})\b"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),
)


def mask_text(value: str) -> str:
    masked = value
    for pattern in _PATTERNS:
        masked = pattern.sub(_replace_match, masked)
    return masked


def _replace_match(match: re.Match[str]) -> str:
    if match.re.pattern.startswith("(?i)\\b(authorization"):
        return f"{match.group(1)}<redacted>"
    if match.re.pattern.startswith("(?i)\\b(OPENAI_API_KEY"):
        return f"{match.group(1)}{match.group(2)}<redacted>"
    return "<redacted>"
