"""Backend-neutral launch commands for already validated harness profiles."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from .registry import require_profile
from .runtime import RuntimeValidationError

_ROLES: Final = frozenset({"main", "planner", "worker", "reviewer"})
_PERMISSIONS: Final = frozenset({"orchestrator", "read-only", "workspace-write"})
_ENV_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class LaunchValidationError(RuntimeValidationError):
    """Raised when a frozen role launch snapshot is incomplete or unsafe."""


def _required_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise LaunchValidationError(f"launch snapshot is missing {context}")
    return value


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _project_trust_root(workspace: Path) -> Path:
    for candidate in (workspace, *workspace.parents):
        git_marker = candidate / ".git"
        if git_marker.is_dir() or git_marker.is_file():
            return candidate
    return workspace


def build_claude_argv(
    *,
    role: str,
    model: str,
    effort: str,
    permission: str,
    instructions: str,
    state_path: Path,
    mcp_server_path: Path | None = None,
) -> tuple[str, ...]:
    if role not in _ROLES:
        raise LaunchValidationError(f"unsupported launch role: {role}")
    if permission not in _PERMISSIONS:
        raise LaunchValidationError(f"unsupported launch permission: {permission}")
    model = _required_text(model, f"{role}.model")
    effort = _required_text(effort, f"{role}.effort")
    instructions = _required_text(instructions, f"{role}.instructions")
    argv = [
        "claude",
        "--name",
        f"team-{role}",
        "--model",
        model,
        "--effort",
        effort,
    ]
    if role == "main":
        if mcp_server_path is None:
            raise LaunchValidationError("main MCP server path is required")
        argv.extend(["--append-system-prompt", instructions])
        mcp_config = json.dumps(
            {
                "mcpServers": {
                    "agent_team": {
                        "command": str(mcp_server_path),
                        "args": ["_mcp-server"],
                        "env": {"AGENT_TEAM_STATE_PATH": str(state_path)},
                    }
                }
            },
            separators=(",", ":"),
        )
        mcp_tools = [
            "mcp__agent_team__role_get",
            "mcp__agent_team__role_prompt",
            "mcp__agent_team__role_wait",
            "mcp__agent_team__role_read",
            "mcp__agent_team__role_release",
            "mcp__agent_team__delivery_ack",
            "mcp__agent_team__message_reply",
        ]
        argv.extend(
            [
                "--tools",
                ",".join(["Read", "Grep", "Glob", *mcp_tools]),
                "--allowedTools",
                "Read",
                "Grep",
                "Glob",
                *mcp_tools,
                "--permission-mode",
                "dontAsk",
                "--mcp-config",
                mcp_config,
                "--strict-mcp-config",
            ]
        )
    elif permission == "read-only":
        argv.extend(
            [
                "--append-system-prompt",
                instructions,
                "--tools",
                "Read,Grep,Glob",
                "--allowedTools",
                "Read",
                "Grep",
                "Glob",
                "--permission-mode",
                "dontAsk",
            ]
        )
    else:
        argv.extend(
            [
                "--append-system-prompt",
                instructions,
                "--tools",
                "default",
                "--permission-mode",
                "auto",
            ]
        )
    return tuple(argv)


def build_codex_argv(
    *,
    role: str,
    model: str,
    effort: str,
    permission: str,
    instructions: str,
    state_path: Path,
    workspace: Path,
    control_socket: Path | None = None,
    mcp_server_path: Path | None = None,
) -> tuple[str, ...]:
    if role not in _ROLES:
        raise LaunchValidationError(f"unsupported launch role: {role}")
    if permission not in _PERMISSIONS:
        raise LaunchValidationError(f"unsupported launch permission: {permission}")
    model = _required_text(model, f"{role}.model")
    effort = _required_text(effort, f"{role}.effort")
    instructions = _required_text(instructions, f"{role}.instructions")
    base_permission_profile = (
        ":workspace" if permission == "workspace-write" else ":read-only"
    )
    permission_profile = base_permission_profile
    trust_root = _project_trust_root(workspace)
    project_trust = (
        "projects={" + _toml_string(str(trust_root)) + '={trust_level="untrusted"}}'
    )
    argv = [
        "codex",
        "-m",
        model,
        "-c",
        f"model_reasoning_effort={_toml_string(effort)}",
        "-c",
        f"developer_instructions={_toml_string(instructions)}",
    ]
    if role == "main" and mcp_server_path is None:
        raise LaunchValidationError("main MCP server path is required")
    if control_socket is not None:
        permission_profile = (
            "agent_team_workspace"
            if permission == "workspace-write"
            else "agent_team_readonly"
        )
        socket_table = "{" + _toml_string(str(control_socket)) + '="allow"}'
        argv.extend(
            [
                "-c",
                "features.network_proxy=true",
                "-c",
                f"permissions.{permission_profile}.extends="
                + _toml_string(base_permission_profile),
                "-c",
                f"permissions.{permission_profile}.network.enabled=true",
                "-c",
                f"permissions.{permission_profile}.network.unix_sockets={socket_table}",
            ]
        )
    argv.extend(
        [
            "-c",
            f"default_permissions={_toml_string(permission_profile)}",
            "-c",
            project_trust,
        ]
    )
    if role == "main":
        argv.extend(
            [
                "-c",
                f"mcp_servers.agent_team.command={_toml_string(str(mcp_server_path))}",
                "-c",
                'mcp_servers.agent_team.args=["_mcp-server"]',
                "-c",
                "mcp_servers.agent_team.env.AGENT_TEAM_STATE_PATH="
                + _toml_string(str(state_path)),
                "-c",
                'mcp_servers.agent_team.default_tools_approval_mode="approve"',
            ]
        )
    argv.extend(["-a", "never"])
    return tuple(argv)


def build_direct_argv(
    *,
    role: str,
    provider: str,
    model: str,
    effort: str,
    permission: str,
    instructions: str,
    state_path: Path,
    workspace: Path,
    control_socket: Path | None = None,
    mcp_server_path: Path | None = None,
) -> tuple[str, ...]:
    try:
        require_profile(provider, role, "direct", permission)
    except ValueError as exc:
        raise LaunchValidationError(str(exc)) from exc
    if provider == "claude":
        return build_claude_argv(
            role=role,
            model=model,
            effort=effort,
            permission=permission,
            instructions=instructions,
            state_path=state_path,
            mcp_server_path=mcp_server_path,
        )
    if provider == "codex":
        return build_codex_argv(
            role=role,
            model=model,
            effort=effort,
            permission=permission,
            instructions=instructions,
            state_path=state_path,
            workspace=workspace,
            control_socket=control_socket,
            mcp_server_path=mcp_server_path,
        )
    raise LaunchValidationError(
        f"{provider} direct profile has no registered harness launch builder"
    )


def build_shell_command(
    argv: Sequence[str], environment: Mapping[str, str] | None = None
) -> str:
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise LaunchValidationError("launch argv must be non-empty strings")
    env = {} if environment is None else dict(environment)
    assignments: list[str] = []
    for name in sorted(env):
        value = env[name]
        if not isinstance(name, str) or _ENV_NAME_RE.fullmatch(name) is None:
            raise LaunchValidationError(f"launch environment name is invalid: {name!r}")
        if not isinstance(value, str):
            raise LaunchValidationError(f"launch environment value is invalid: {name}")
        assignments.append(f"{name}={value}")
    command = [*assignments, *argv]
    if assignments:
        command.insert(0, "env")
    return shlex.join(command)


def _role_spec(
    state: Mapping[str, object], role: str
) -> tuple[Mapping[str, object], Path, Path, Path | None]:
    if role not in _ROLES:
        raise LaunchValidationError(f"unsupported launch role: {role}")
    raw_specs = state.get("role_specs")
    if not isinstance(raw_specs, Mapping):
        raise LaunchValidationError("launch state is missing role_specs")
    raw_spec = raw_specs.get(role)
    if not isinstance(raw_spec, Mapping):
        raise LaunchValidationError(f"launch state is missing role_specs.{role}")
    if (
        raw_spec.get("transport") != "direct"
        or raw_spec.get("execution") != "tui_direct"
    ):
        raise LaunchValidationError(f"role {role} is not a direct TUI role")
    workspace = Path(_required_text(state.get("workspace"), "workspace"))
    state_path = Path(_required_text(state.get("state_path"), "state_path"))
    raw_socket = state.get("orca_socket")
    control_socket = (
        Path(raw_socket) if isinstance(raw_socket, str) and raw_socket else None
    )
    return raw_spec, workspace, state_path, control_socket


def _role_environment(provider: str, role: str, state_path: Path) -> dict[str, str]:
    if provider == "codex":
        return {"CODEX_HOME": str(state_path.parent / "codex" / role)}
    if provider == "claude":
        return {}
    raise LaunchValidationError(
        f"{provider} direct profile has no registered environment builder"
    )


def build_snapshot_role_command(
    state: Mapping[str, object],
    role: str,
    *,
    mcp_server_path: Path | None = None,
) -> str:
    """Build a direct role command from persisted role metadata only.

    The caller supplies a state snapshot already validated by the runtime.
    This helper never reads the config or prompt paths.
    """

    spec, workspace, state_path, control_socket = _role_spec(state, role)
    provider = _required_text(spec.get("provider"), f"{role}.provider")
    model = _required_text(spec.get("model"), f"{role}.model")
    effort = _required_text(spec.get("effort"), f"{role}.effort")
    permission = _required_text(spec.get("permission"), f"{role}.permission")
    instructions = _required_text(spec.get("instructions"), f"{role}.instructions")
    argv = build_direct_argv(
        role=role,
        provider=provider,
        model=model,
        effort=effort,
        permission=permission,
        instructions=instructions,
        state_path=state_path,
        workspace=workspace,
        control_socket=control_socket,
        mcp_server_path=mcp_server_path,
    )
    return build_shell_command(argv, _role_environment(provider, role, state_path))


def build_plan_role_command(
    plan: Mapping[str, object],
    role: str,
    *,
    control_socket: Path | None = None,
    mcp_server_path: Path | None = None,
) -> str:
    """Shell-quote a validated plan launch, rebuilding only for a socket override."""

    roles = plan.get("roles")
    if not isinstance(roles, Mapping):
        raise LaunchValidationError("launch plan is missing roles")
    launch = roles.get(role)
    if not isinstance(launch, Mapping):
        raise LaunchValidationError(f"launch plan is missing role: {role}")
    if launch.get("transport") != "direct" or launch.get("execution") != "tui_direct":
        raise LaunchValidationError(f"role {role} is not a direct TUI role")
    raw_argv = launch.get("argv")
    raw_env = launch.get("env")
    if not isinstance(raw_env, Mapping):
        raise LaunchValidationError(f"launch plan is missing {role}.env")
    if control_socket is None or launch.get("provider") == "claude":
        if not isinstance(raw_argv, Sequence) or isinstance(raw_argv, (str, bytes)):
            raise LaunchValidationError(f"launch plan is missing {role}.argv")
        argv = tuple(raw_argv)
    else:
        workspace = Path(_required_text(plan.get("workspace"), "workspace"))
        state_path = Path(_required_text(plan.get("state_path"), "state_path"))
        argv = build_direct_argv(
            role=role,
            provider=_required_text(launch.get("provider"), f"{role}.provider"),
            model=_required_text(launch.get("model"), f"{role}.model"),
            effort=_required_text(launch.get("effort"), f"{role}.effort"),
            permission=_required_text(launch.get("permission"), f"{role}.permission"),
            instructions=_required_text(
                launch.get("instructions"), f"{role}.instructions"
            ),
            state_path=state_path,
            workspace=workspace,
            control_socket=control_socket,
            mcp_server_path=mcp_server_path,
        )
    return build_shell_command(argv, raw_env)
