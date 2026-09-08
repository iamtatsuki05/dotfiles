"""Backend-neutral role configuration parsing for versioned team configs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .contracts import Role
from .registry import CANONICAL_HARNESSES, require_profile

SUPPORTED_TRANSPORTS: Final = frozenset({"direct", "acp"})
SUPPORTED_PROVIDERS: Final = frozenset(CANONICAL_HARNESSES)
PROVIDER_EFFORTS: Final[dict[str, frozenset[str]]] = {
    "claude": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "codex": frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"}),
    "copilot": frozenset({"none", "low", "medium", "high", "xhigh", "max"}),
    "opencode": frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"}),
}
ROLE_PERMISSIONS: Final[dict[str, str]] = {
    "planner": "read-only",
    "worker": "workspace-write",
    "reviewer": "read-only",
}
ALL_ROLES: Final[tuple[str, ...]] = ("main", "planner", "worker", "reviewer")
_ROLE_PERMISSIONS_BY_KIND: Final[dict[Role, str]] = {
    Role.MAIN: "orchestrator",
    Role.PLANNER: ROLE_PERMISSIONS["planner"],
    Role.WORKER: ROLE_PERMISSIONS["worker"],
    Role.REVIEWER: ROLE_PERMISSIONS["reviewer"],
}


class ConfigError(ValueError):
    """Raised when a role or versioned config value is invalid."""


@dataclass(frozen=True)
class RoleConfig:
    provider: str
    transport: str
    model: str
    effort: str
    prompt_path: Path
    permission: str


def require_string(table: dict[str, object], key: str, context: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} must be a non-empty string")
    return value


def resolve_prompt(config_dir: Path, raw_path: str, context: str) -> Path:
    """Resolve a prompt using the version-3 catalog path contract."""

    prompt_path = (config_dir / raw_path).resolve()
    try:
        prompt_path.relative_to(config_dir)
    except ValueError as exc:
        raise ConfigError(f"{context}.prompt must stay within {config_dir}") from exc
    if not prompt_path.is_file():
        raise ConfigError(f"{context}.prompt does not exist: {prompt_path}")
    return prompt_path


def parse_role(
    table: object,
    *,
    context: str,
    config_dir: Path,
    kind: Role,
) -> RoleConfig:
    """Parse one role profile using an explicit fixed role kind.

    ``context`` is diagnostic text only.  It never selects the registry role;
    callers must pass the node's ``Role`` value explicitly.
    """

    if not isinstance(kind, Role):
        raise ConfigError(f"{context}.kind must be a Role")
    kind_permission = _ROLE_PERMISSIONS_BY_KIND[kind]
    if not isinstance(table, dict):
        raise ConfigError(f"{context} must be a table")
    provider = require_string(table, "provider", context)
    if provider not in SUPPORTED_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ConfigError(f"{context}.provider must be one of: {supported}")
    transport = require_string(table, "transport", context)
    if transport not in SUPPORTED_TRANSPORTS:
        supported = ", ".join(sorted(SUPPORTED_TRANSPORTS))
        raise ConfigError(f"{context}.transport must be one of: {supported}")
    model = require_string(table, "model", context)
    effort = require_string(table, "effort", context)
    if provider == "copilot":
        if model == "auto" and effort != "none":
            raise ConfigError(f"{context} Copilot model=auto requires effort=none")
        if model != "auto" and effort == "none":
            raise ConfigError(
                f"{context} explicit Copilot models cannot use effort=none"
            )
    if provider not in PROVIDER_EFFORTS:
        try:
            require_profile(provider, kind.value, transport, kind_permission)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        raise ConfigError(f"no effort profile is registered for {provider}")
    if effort not in PROVIDER_EFFORTS[provider]:
        supported = ", ".join(sorted(PROVIDER_EFFORTS[provider]))
        raise ConfigError(
            f"{context}.effort is not supported by {provider}; use one of: {supported}"
        )
    permission = require_string(table, "permission", context)
    if permission != kind_permission:
        raise ConfigError(f"{context}.permission must be {kind_permission!r}")
    try:
        require_profile(provider, kind.value, transport, permission)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if transport == "acp" and kind is Role.MAIN:
        raise ConfigError("main.transport='acp' is not supported")
    prompt = require_string(table, "prompt", context)
    return RoleConfig(
        provider=provider,
        transport=transport,
        model=model,
        effort=effort,
        prompt_path=resolve_prompt(config_dir, prompt, context),
        permission=permission,
    )
