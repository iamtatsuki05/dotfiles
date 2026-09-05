"""Static harness capability registry.

The registry deliberately distinguishes names we know from launch profiles we
have verified.  Adding a harness name must never make an unverified command
executable through a provider fallback.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True)
class HarnessCapability:
    harness_id: str
    command: str
    description: str
    acp_adapter: str | None
    acp_status: str
    runnable_profiles: tuple[tuple[str, str, str], ...]
    rejection_reason: str | None = None


CANONICAL_HARNESSES: Final[tuple[str, ...]] = (
    "claude",
    "codex",
    "copilot",
    "cursor",
    "devin",
    "antigravity",
    "hermes",
    "opencode",
    "openclaw",
    "grok",
)

_READ_ONLY: Final[tuple[tuple[str, str, str], ...]] = (
    ("planner", "direct", "read-only"),
    ("reviewer", "direct", "read-only"),
)
_CLAUDE_PROFILES: Final[tuple[tuple[str, str, str], ...]] = (
    ("main", "direct", "orchestrator"),
    *_READ_ONLY,
    ("planner", "acp", "read-only"),
    ("reviewer", "acp", "read-only"),
)
_CODEX_PROFILES: Final[tuple[tuple[str, str, str], ...]] = (
    ("main", "direct", "orchestrator"),
    *_READ_ONLY,
    ("worker", "direct", "workspace-write"),
)
HARNESS_REGISTRY: Final[dict[str, HarnessCapability]] = {
    "claude": HarnessCapability(
        "claude",
        "claude",
        "Anthropic Claude Code",
        "@agentclientprotocol/claude-agent-acp@0.70.0",
        "verified",
        _CLAUDE_PROFILES,
    ),
    "codex": HarnessCapability(
        "codex",
        "codex",
        "OpenAI Codex CLI",
        "@agentclientprotocol/codex-acp",
        "known-but-rejected",
        _CODEX_PROFILES,
        "Codex ACP read-only negative test showed internal writes are not blocked; direct only.",
    ),
    "copilot": HarnessCapability(
        "copilot",
        "copilot",
        "GitHub Copilot CLI",
        "copilot --acp; acpx built-in: copilot",
        "verified",
        _READ_ONLY,
    ),
    "cursor": HarnessCapability(
        "cursor",
        "cursor-agent",
        "Cursor Agent",
        "cursor-agent acp; acpx built-in: cursor",
        "known-unverified",
        (),
        "The current CLI is unauthenticated; permission negative tests are incomplete.",
    ),
    "devin": HarnessCapability(
        "devin",
        "devin",
        "Devin CLI",
        "devin acp",
        "known-unverified",
        (),
        "Only no-tool smoke completed; tool turns and the permission matrix are incomplete.",
    ),
    "antigravity": HarnessCapability(
        "antigravity",
        "agy",
        "Antigravity CLI",
        None,
        "not-observed",
        (),
        "A read-only probe read a sibling path outside the workspace.",
    ),
    "hermes": HarnessCapability(
        "hermes",
        "hermes",
        "Hermes Agent",
        "hermes acp",
        "known-unverified",
        (),
        "A read-only probe wrote normal, .git, and outside-workspace paths.",
    ),
    "opencode": HarnessCapability(
        "opencode",
        "opencode",
        "OpenCode",
        "opencode acp; acpx built-in: opencode",
        "known-unverified",
        (),
        "A raw-workspace symlink escape was observed; snapshot profile E2E is incomplete.",
    ),
    "openclaw": HarnessCapability(
        "openclaw",
        "openclaw",
        "OpenClaw",
        "openclaw acp; acpx built-in: openclaw",
        "known-unverified",
        (),
        "The sandbox needs an unavailable Docker daemon; negative tests are incomplete.",
    ),
    "grok": HarnessCapability(
        "grok",
        "grok",
        "Grok CLI",
        "grok agent stdio; acpx built-in: grok-build",
        "known-unverified",
        (),
        "The current CLI is unauthenticated; direct/ACP negative tests are incomplete.",
    ),
}


def capability_for(provider: str) -> HarnessCapability:
    try:
        return HARNESS_REGISTRY[provider]
    except KeyError as exc:
        supported = ", ".join(CANONICAL_HARNESSES)
        raise ValueError(
            f"unknown harness {provider!r}; recognized harnesses: {supported}"
        ) from exc


def require_profile(
    provider: str, role: str, transport: str, permission: str
) -> HarnessCapability:
    capability = capability_for(provider)
    profile = (role, transport, permission)
    if profile not in capability.runnable_profiles:
        reason = (
            capability.rejection_reason
            or "this role/transport/permission profile is not verified"
        )
        raise ValueError(
            f"{provider} profile {role}/{transport}/{permission} is not runnable: {reason}"
        )
    return capability


def profile_execution(provider: str, role: str, transport: str, permission: str) -> str:
    """Return the fixed execution kind for a verified profile.

    Provider configuration never supplies this value.  The registry decides
    whether a direct profile is an interactive TUI or a background adapter.
    """

    require_profile(provider, role, transport, permission)
    if provider == "claude" and transport == "acp":
        return "background"
    if provider == "copilot":
        return "background"
    if provider in {"claude", "codex"} and transport == "direct":
        return "tui_direct"
    raise ValueError(f"{provider} profile has no execution profile")


def adapter_id_for_profile(
    provider: str, role: str, transport: str, permission: str
) -> str | None:
    profile_execution(provider, role, transport, permission)
    if provider == "claude" and transport == "acp":
        return "claude-acp-0.70.0"
    if provider == "copilot":
        return "github-copilot-direct-readonly-1.0.81"
    if provider == "opencode":
        return "opencode-direct-readonly-1.18.25"
    return None


def command_resolution() -> dict[str, str | bool | None]:
    """Resolve command names only; no version, login, or startup is attempted."""
    result: dict[str, str | bool | None] = {}
    for harness_id, capability in HARNESS_REGISTRY.items():
        command_path = shutil.which(capability.command)
        if (
            harness_id == "copilot"
            and command_path is not None
            and not _github_copilot_identity(Path(command_path))
        ):
            command_path = None
        result[harness_id] = str(command_path) if command_path else None
    return result


def _github_copilot_identity(path: str | Path) -> bool:
    """Recognize GitHub Copilot without executing a command.

    `copilot` is also the AWS Copilot executable.  A filename or PATH hit is
    not sufficient evidence, so only a binary/script carrying an unambiguous
    GitHub Copilot marker is considered available.
    """
    try:
        data = Path(path).resolve().read_bytes()[:4_000_000].lower()
    except OSError:
        return False
    return b"github copilot" in data or b"github.com/github/copilot" in data


def status_rows() -> list[dict[str, object]]:
    resolved = command_resolution()
    rows: list[dict[str, object]] = []
    for harness_id in CANONICAL_HARNESSES:
        capability = HARNESS_REGISTRY[harness_id]
        command_path = resolved[harness_id]
        implemented = bool(capability.runnable_profiles)
        resolution_status = (
            "resolved" if command_path is not None else "not-found-or-rejected"
        )
        if (
            harness_id == "copilot"
            and command_path is None
            and shutil.which(capability.command) is not None
        ):
            resolution_status = "path-collision; runtime-preflight-checks-pinned-mise"
        rows.append(
            {
                **asdict(capability),
                "recognized": True,
                "available": command_path is not None,
                "command_path": command_path,
                "command_resolution_status": resolution_status,
                "implemented": implemented,
                "runnable": implemented and command_path is not None,
            }
        )
    return rows
