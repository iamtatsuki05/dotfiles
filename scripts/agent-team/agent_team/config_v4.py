"""Pure parser and selector for the explicit version-4 team configuration."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

from .registry import HARNESS_REGISTRY
from .topology import (
    AgentNode,
    Edge,
    EdgeKind,
    NodeId,
    Permission,
    ProfileRef,
    ProfileResolver,
    TeamDefinition,
    TeamId,
    ValidationResult,
    render_topology,
    validate_team,
)

V4_CONFIG_VERSION: Final = 4
V4_RUNTIME: Final = "orca"
MAX_V4_CONFIG_BYTES: Final = 1_048_576
MAX_V4_TEAMS: Final = 64
MAX_V4_NODES_PER_TEAM: Final = 128
MAX_V4_EDGES_PER_TEAM: Final = 256
MAX_V4_IDENTIFIER_CHARS: Final = 64
MAX_V4_NAME_CHARS: Final = 128
MAX_V4_LABEL_CHARS: Final = 128
MAX_V4_ERROR_COUNT: Final = 64
MAX_V4_ERROR_MESSAGE_CHARS: Final = 512
MAX_V4_ERROR_TOTAL_CHARS: Final = 16_384
MAX_V4_DIAGNOSTIC_CHARS: Final = 160
_TOP_LEVEL_FIELDS: Final = frozenset({"version", "runtime", "teams"})
_TEAM_FIELDS: Final = frozenset({"name", "nodes", "edges"})
_NODE_FIELDS: Final = frozenset({"id", "label", "main", "profile"})
_PROFILE_FIELDS: Final = frozenset({"provider", "transport", "permission"})
_EDGE_FIELDS: Final = frozenset({"source", "target", "kind"})
_PERMISSIONS: Final = frozenset({"orchestrator", "read-only", "workspace-write"})
_UNSAFE_RANGES: Final = ((0x00, 0x1F), (0x7F, 0x9F), (0xD800, 0xDFFF))


class V4ConfigError(ValueError):
    """Raised when a version-4 config cannot be safely interpreted."""


def _diagnostic(value: object) -> str:
    """Return a bounded JSON-escaped value for user-visible diagnostics."""

    raw = str(value)
    truncated = len(raw) > MAX_V4_DIAGNOSTIC_CHARS
    if truncated:
        raw = raw[:MAX_V4_DIAGNOSTIC_CHARS]
    suffix = "...<truncated>" if truncated else ""
    return json.dumps(raw, ensure_ascii=True) + suffix


def _diagnostic_list(values: Sequence[object]) -> str:
    """Join escaped diagnostics without allowing an unbounded error string."""

    parts: list[str] = []
    total = 0
    for value in values:
        part = _diagnostic(value)
        extra = len(part) + (2 if parts else 0)
        if total + extra > MAX_V4_ERROR_TOTAL_CHARS:
            parts.append("...<truncated>")
            break
        parts.append(part)
        total += extra
    return ", ".join(parts)


def _bounded_message(prefix: str, detail: str) -> str:
    """Keep a complete diagnostic below the aggregate character bound."""

    separator = ": "
    available = MAX_V4_ERROR_TOTAL_CHARS - len(prefix) - len(separator)
    if len(detail) <= available:
        return prefix + separator + detail
    suffix = "...<truncated>"
    detail_limit = max(0, available - len(suffix))
    return prefix + separator + detail[:detail_limit] + suffix


def read_config_file(config_path: Path) -> tuple[Path, dict[str, object]]:
    """Read one bounded TOML document and return its canonical path and data."""

    resolved_path = config_path.expanduser().resolve()
    try:
        if not resolved_path.is_file():
            if resolved_path.exists():
                raise V4ConfigError(
                    f"config is not a regular file: {_diagnostic(resolved_path)}"
                )
            raise V4ConfigError(f"config does not exist: {_diagnostic(resolved_path)}")
        with resolved_path.open("rb") as config_file:
            raw = config_file.read(MAX_V4_CONFIG_BYTES + 1)
    except V4ConfigError:
        raise
    except FileNotFoundError as exc:
        raise V4ConfigError(
            f"config does not exist: {_diagnostic(resolved_path)}"
        ) from exc
    except OSError as exc:
        raise V4ConfigError(
            f"config is unavailable: {_diagnostic(resolved_path)}"
        ) from exc
    if len(raw) > MAX_V4_CONFIG_BYTES:
        raise V4ConfigError(
            f"config exceeds maximum of {MAX_V4_CONFIG_BYTES} bytes: "
            f"{_diagnostic(resolved_path)}"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise V4ConfigError(
            f"config is not valid UTF-8: {_diagnostic(resolved_path)}"
        ) from exc
    return resolved_path, tomllib.loads(text)


class RegistryProfileResolver:
    """Resolve only permissions recorded in the verified harness registry."""

    def resolve(self, profile: ProfileRef) -> frozenset[Permission] | None:
        capability = HARNESS_REGISTRY.get(profile.provider)
        if capability is None:
            return None
        permissions = {
            cast(Permission, permission)
            for _role, transport, permission in capability.runnable_profiles
            if transport == profile.transport
        }
        return frozenset(permissions) if permissions else None


@dataclass(frozen=True, slots=True)
class V4Team:
    """A named v4 team and its pure topology validation result."""

    team_id: TeamId
    name: str
    definition: TeamDefinition
    validation: ValidationResult


@dataclass(frozen=True, slots=True)
class V4Config:
    """Parsed v4 config; no runtime or filesystem resource is owned here."""

    config_path: Path
    runtime: str
    teams: tuple[V4Team, ...]
    resolver: ProfileResolver = field(repr=False, compare=False)

    def team(self, team_id: str) -> V4Team:
        return select_v4_team(self, (team_id,))

    def require_valid(self) -> None:
        invalid = [team for team in self.teams if not team.validation.valid]
        if not invalid:
            return
        _validate_config_diagnostic_budget(invalid)
        details: list[str] = []
        total = 0
        for team in invalid:
            team_detail = ", ".join(
                f"{_diagnostic(issue.code)}: {_diagnostic(issue.message)}"
                for issue in team.validation.errors
            )
            part = f"{_diagnostic(team.team_id)}: {team_detail}"
            extra = len(part) + (2 if details else 0)
            if total + extra > MAX_V4_ERROR_TOTAL_CHARS:
                raise V4ConfigError(
                    "v4 config validation diagnostics exceed maximum of "
                    f"{MAX_V4_ERROR_TOTAL_CHARS} characters"
                )
            details.append(part)
            total += extra
        detail_text = "; ".join(details)
        raise V4ConfigError(
            _bounded_message("v4 config topology is invalid", detail_text)
        )


@dataclass(frozen=True, slots=True)
class V4LaunchPlan:
    """The narrow, pure handoff to a later runtime/store implementation."""

    config_path: Path
    team_id: TeamId
    workspace: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "config_path": str(self.config_path),
            "team_id": str(self.team_id),
            "workspace": str(self.workspace),
        }


def _table(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise V4ConfigError(f"{context} must be a table")
    return cast(dict[str, object], value)


def _unsupported(
    table: dict[str, object], allowed: frozenset[str], context: str
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        fields = _diagnostic_list(unknown)
        raise V4ConfigError(
            _bounded_message(f"{context} has unsupported fields", fields)
        )


def _safe_string(value: object, context: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str):
        raise V4ConfigError(f"{context} must be a string")
    if not value or not value.strip():
        raise V4ConfigError(f"{context} must not be empty")
    if maximum is not None and len(value) > maximum:
        raise V4ConfigError(
            f"{context} exceeds maximum of {maximum} characters: {_diagnostic(value)}"
        )
    if value != value.strip() or any(
        any(start <= ord(character) <= end for start, end in _UNSAFE_RANGES)
        or ord(character) in {0x2028, 0x2029}
        for character in value
    ):
        raise V4ConfigError(
            f"{context} contains unsafe whitespace or control characters: "
            f"{_diagnostic(value)}"
        )
    return value


def _required_string(
    table: dict[str, object],
    key: str,
    context: str,
    *,
    maximum: int | None = None,
) -> str:
    if key not in table:
        raise V4ConfigError(f"{context} is missing {key}")
    return _safe_string(table[key], f"{context}.{key}", maximum=maximum)


def _parse_profile(value: object, context: str) -> ProfileRef:
    table = _table(value, context)
    _unsupported(table, _PROFILE_FIELDS, context)
    provider = _required_string(
        table, "provider", context, maximum=MAX_V4_IDENTIFIER_CHARS
    )
    transport = _required_string(
        table, "transport", context, maximum=MAX_V4_IDENTIFIER_CHARS
    )
    permission_value = _required_string(
        table, "permission", context, maximum=MAX_V4_IDENTIFIER_CHARS
    )
    if permission_value not in _PERMISSIONS:
        supported = ", ".join(sorted(_PERMISSIONS))
        raise V4ConfigError(f"{context}.permission must be one of: {supported}")
    return ProfileRef(
        provider,
        transport,
        cast(Permission, permission_value),
    )


def _parse_nodes(value: object, context: str) -> tuple[AgentNode, ...]:
    if not isinstance(value, list):
        raise V4ConfigError(f"{context} must be an array of tables")
    if not value:
        raise V4ConfigError(f"{context} must not be empty")
    if len(value) > MAX_V4_NODES_PER_TEAM:
        raise V4ConfigError(
            f"{context} exceeds maximum of {MAX_V4_NODES_PER_TEAM} nodes"
        )
    nodes: list[AgentNode] = []
    for index, raw_node in enumerate(value):
        node_context = f"{context}[{index}]"
        table = _table(raw_node, node_context)
        _unsupported(table, _NODE_FIELDS, node_context)
        node_id = _required_string(
            table, "id", node_context, maximum=MAX_V4_IDENTIFIER_CHARS
        )
        label = _required_string(
            table, "label", node_context, maximum=MAX_V4_LABEL_CHARS
        )
        main = table.get("main")
        if not isinstance(main, bool):
            raise V4ConfigError(f"{node_context}.main must be a boolean")
        nodes.append(
            AgentNode(
                NodeId(node_id),
                label,
                _parse_profile(table.get("profile"), f"{node_context}.profile"),
                main,
            )
        )
    return tuple(nodes)


def _parse_edges(value: object, context: str) -> tuple[Edge, ...]:
    if not isinstance(value, list):
        raise V4ConfigError(f"{context} must be an array of tables")
    if len(value) > MAX_V4_EDGES_PER_TEAM:
        raise V4ConfigError(
            f"{context} exceeds maximum of {MAX_V4_EDGES_PER_TEAM} edges"
        )
    edges: list[Edge] = []
    for index, raw_edge in enumerate(value):
        edge_context = f"{context}[{index}]"
        table = _table(raw_edge, edge_context)
        _unsupported(table, _EDGE_FIELDS, edge_context)
        source = _required_string(
            table, "source", edge_context, maximum=MAX_V4_IDENTIFIER_CHARS
        )
        target = _required_string(
            table, "target", edge_context, maximum=MAX_V4_IDENTIFIER_CHARS
        )
        kind_value = _required_string(
            table, "kind", edge_context, maximum=MAX_V4_IDENTIFIER_CHARS
        )
        try:
            kind = EdgeKind(kind_value)
        except ValueError as exc:
            supported = ", ".join(item.value for item in EdgeKind)
            raise V4ConfigError(
                f"{edge_context}.kind must be one of: {supported}"
            ) from exc
        edges.append(Edge(NodeId(source), NodeId(target), kind))
    return tuple(edges)


def _parse_team(
    team_id: str,
    value: object,
    resolver: ProfileResolver,
) -> V4Team:
    context = f"teams.{team_id}"
    table = _table(value, context)
    _unsupported(table, _TEAM_FIELDS, context)
    name = _required_string(table, "name", context, maximum=MAX_V4_NAME_CHARS)
    if "nodes" not in table:
        raise V4ConfigError(f"{context} is missing nodes")
    if "edges" not in table:
        raise V4ConfigError(f"{context} is missing edges")
    nodes = _parse_nodes(table["nodes"], f"{context}.nodes")
    edges = _parse_edges(table["edges"], f"{context}.edges")
    definition = TeamDefinition(TeamId(team_id), nodes, edges)
    validation = validate_team(definition, resolver)
    _validate_diagnostic_budget(validation, context)
    return V4Team(TeamId(team_id), name, definition, validation)


def _validate_diagnostic_budget(result: ValidationResult, context: str) -> None:
    if len(result.errors) > MAX_V4_ERROR_COUNT:
        raise V4ConfigError(
            f"{context} validation diagnostics exceed maximum of "
            f"{MAX_V4_ERROR_COUNT} errors"
        )
    total = 0
    for issue in result.errors:
        if len(issue.message) > MAX_V4_ERROR_MESSAGE_CHARS:
            raise V4ConfigError(
                f"{context} validation diagnostic exceeds maximum of "
                f"{MAX_V4_ERROR_MESSAGE_CHARS} characters"
            )
        total += len(issue.code) + len(issue.message) + 2
    if total > MAX_V4_ERROR_TOTAL_CHARS:
        raise V4ConfigError(
            f"{context} validation diagnostics exceed maximum of "
            f"{MAX_V4_ERROR_TOTAL_CHARS} characters"
        )


def _validate_config_diagnostic_budget(teams: Sequence[V4Team]) -> None:
    count = 0
    total = 0
    for team in teams:
        for issue in team.validation.errors:
            count += 1
            if count > MAX_V4_ERROR_COUNT:
                raise V4ConfigError(
                    "v4 config validation diagnostics exceed maximum of "
                    f"{MAX_V4_ERROR_COUNT} errors"
                )
            if len(issue.message) > MAX_V4_ERROR_MESSAGE_CHARS:
                raise V4ConfigError(
                    "v4 config validation diagnostic exceeds maximum of "
                    f"{MAX_V4_ERROR_MESSAGE_CHARS} characters"
                )
            total += len(issue.code) + len(issue.message) + 2
            if total > MAX_V4_ERROR_TOTAL_CHARS:
                raise V4ConfigError(
                    "v4 config validation diagnostics exceed maximum of "
                    f"{MAX_V4_ERROR_TOTAL_CHARS} characters"
                )


def load_v4_config_data(
    config_path: Path,
    data: dict[str, object],
    resolver: ProfileResolver | None = None,
) -> V4Config:
    """Parse already-read v4 data without reading or starting any resource."""

    resolved_path = config_path.expanduser().resolve()
    version = data.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != V4_CONFIG_VERSION
    ):
        raise V4ConfigError(f"version must be integer {V4_CONFIG_VERSION}")
    runtime = data.get("runtime")
    if not isinstance(runtime, str) or runtime != V4_RUNTIME:
        raise V4ConfigError("runtime must be 'orca'")
    _unsupported(data, _TOP_LEVEL_FIELDS, "config")
    raw_teams = data.get("teams")
    if not isinstance(raw_teams, dict) or not raw_teams:
        raise V4ConfigError("teams must be a non-empty table")
    if len(raw_teams) > MAX_V4_TEAMS:
        raise V4ConfigError(f"teams exceeds maximum of {MAX_V4_TEAMS} teams")
    profile_resolver = resolver if resolver is not None else RegistryProfileResolver()
    team_ids: dict[str, str] = {}
    teams: list[V4Team] = []
    for raw_team_id, raw_team in raw_teams.items():
        if not isinstance(raw_team_id, str):
            raise V4ConfigError("team IDs must be strings")
        team_id = _safe_string(raw_team_id, "team.id", maximum=MAX_V4_IDENTIFIER_CHARS)
        key = team_id.casefold()
        if key in team_ids:
            raise V4ConfigError(
                "team IDs are ambiguous: "
                f"{_diagnostic(team_ids[key])} and {_diagnostic(team_id)}"
            )
        team_ids[key] = team_id
        teams.append(_parse_team(team_id, raw_team, profile_resolver))
    teams.sort(key=lambda team: (str(team.team_id).casefold(), str(team.team_id)))
    _validate_config_diagnostic_budget(teams)
    return V4Config(resolved_path, runtime, tuple(teams), profile_resolver)


def load_v4_config(
    config_path: Path, resolver: ProfileResolver | None = None
) -> V4Config:
    """Read and parse one bounded v4 file without starting any resource."""

    resolved_path, data = read_config_file(config_path)
    return load_v4_config_data(resolved_path, data, resolver)


def _selection_values(team: str | Sequence[str] | None) -> tuple[object, ...]:
    if team is None:
        return ()
    if isinstance(team, str):
        return (team,)
    return tuple(team)


def select_v4_team(config: V4Config, team: str | Sequence[str] | None) -> V4Team:
    """Select one exact map key; no default or case-folded alias is accepted."""

    values = _selection_values(team)
    if not values:
        raise V4ConfigError("--team is required for config version 4")
    if len(values) != 1:
        raise V4ConfigError("exactly one --team must be specified")
    selected = values[0]
    if not isinstance(selected, str) or not selected:
        raise V4ConfigError("--team must be a non-empty string")
    selected = _safe_string(selected, "--team", maximum=MAX_V4_IDENTIFIER_CHARS)
    for candidate in config.teams:
        if str(candidate.team_id) == selected:
            return candidate
    raise V4ConfigError(f"unknown team: {_diagnostic(selected)}")


def build_v4_launch_plan(
    config: V4Config,
    workspace: Path,
    team: str | Sequence[str] | None,
) -> V4LaunchPlan:
    """Build only the typed metadata needed by a future runtime/store seam."""

    config.require_valid()
    selected = select_v4_team(config, team)
    resolved_workspace = workspace.expanduser().resolve()
    if not resolved_workspace.is_dir():
        raise V4ConfigError(
            f"workspace is not a directory: {_diagnostic(resolved_workspace)}"
        )
    return V4LaunchPlan(
        config.config_path,
        selected.team_id,
        resolved_workspace,
    )


def v4_team_rows(config: V4Config) -> tuple[dict[str, object], ...]:
    """Return deterministic machine-readable team status rows."""

    return tuple(
        {
            "id": str(team.team_id),
            "name": team.name,
            "valid": team.validation.valid,
            "errors": [
                {"code": issue.code, "message": issue.message}
                for issue in team.validation.errors
            ],
        }
        for team in config.teams
    )


def render_v4_team(
    config: V4Config,
    team: str | Sequence[str] | None,
    output_format: str,
) -> str:
    """Render one validated team through the PR #20 topology renderer."""

    config.require_valid()
    selected = select_v4_team(config, team)
    return render_topology(selected.definition, output_format, config.resolver)


def v4_teams_json(config: V4Config) -> str:
    """Serialize team rows with stable JSON formatting for the CLI."""

    return (
        json.dumps(
            {"teams": list(v4_team_rows(config))},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
