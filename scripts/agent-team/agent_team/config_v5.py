"""Pure parser, selector, and renderer for the explicit version-5 config."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .config_roles import ConfigError, RoleConfig, parse_role
from .contracts import NodeRef, Role
from .named_graph import GraphSpec, NamedGraphError, validate_graph
from .task_spec import TaskSpec, parse_task_specs

V5_CONFIG_VERSION: Final = 5
V5_RUNTIMES: Final = frozenset({"orca", "tmux", "zellij", "herdr"})
MAX_V5_TEAMS: Final = 64
MAX_V5_NODES_PER_TEAM: Final = 128
MAX_V5_TEAM_ID_CHARS: Final = 24
MAX_V5_NAME_CHARS: Final = 128
MAX_V5_LABEL_CHARS: Final = 128
_NODE_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_TEAM_ID = re.compile(r"[a-z][a-z0-9-]{0,23}\Z")
_TOP_LEVEL_FIELDS: Final = frozenset({"version", "runtime", "teams"})
_TEAM_FIELDS: Final = frozenset(
    {
        "name",
        "max_review_rounds",
        "nodes",
        "edges",
        "coordination",
        "tasks",
        "routes",
    }
)
_NODE_FIELDS: Final = frozenset({"id", "label", "kind", "role_spec"})
_ROLE_SPEC_FIELDS: Final = frozenset(
    {"provider", "transport", "model", "effort", "prompt", "permission"}
)
_ROUTE_FIELDS: Final = frozenset(
    {
        "task_id",
        "plan_writer",
        "plan_reviewer",
        "implementation_writer",
        "implementation_reviewer",
    }
)
_ROLE_VALUES: Final = frozenset(item.value for item in Role)


class V5ConfigError(ConfigError):
    """Raised when a version-5 configuration is malformed or invalid."""


@dataclass(frozen=True, slots=True)
class V5Node:
    ref: NodeRef
    label: str
    role_spec: RoleConfig


@dataclass(frozen=True, slots=True)
class V5Team:
    team_id: str
    name: str
    max_review_rounds: int
    nodes: tuple[V5Node, ...]
    graph: GraphSpec
    task_specs: tuple[TaskSpec, ...]


@dataclass(frozen=True, slots=True)
class V5Config:
    config_path: Path
    runtime: str
    teams: tuple[V5Team, ...]


def _table(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise V5ConfigError(f"{context} must be a table")
    return {key: item for key, item in value.items()}


def _check_fields(
    table: Mapping[str, object], allowed: frozenset[str], context: str
) -> None:
    keys = tuple(table)
    if any(not isinstance(key, str) for key in keys):
        raise V5ConfigError(f"{context} keys must be strings")
    unknown = sorted(set(keys) - allowed)
    if unknown:
        raise V5ConfigError(f"{context} has unsupported fields: {', '.join(unknown)}")
    missing = sorted(allowed - set(keys))
    if missing:
        raise V5ConfigError(f"{context} is missing fields: {', '.join(missing)}")


def _required_string(
    table: Mapping[str, object], key: str, context: str, *, maximum: int | None = None
) -> str:
    if key not in table:
        raise V5ConfigError(f"{context} is missing {key}")
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise V5ConfigError(f"{context}.{key} must be a non-empty string")
    if maximum is not None and len(value) > maximum:
        raise V5ConfigError(f"{context}.{key} exceeds maximum of {maximum} characters")
    if not value.isprintable():
        raise V5ConfigError(f"{context}.{key} must contain printable characters")
    return value


def _slug(value: object, context: str) -> str:
    if not isinstance(value, str) or _NODE_ID.fullmatch(value) is None:
        raise V5ConfigError(
            f"{context} must be a lowercase slug of at most 64 characters"
        )
    return value


def _team_slug(value: object, context: str) -> str:
    if not isinstance(value, str) or _TEAM_ID.fullmatch(value) is None:
        raise V5ConfigError(
            f"{context} must be a lowercase slug of at most "
            f"{MAX_V5_TEAM_ID_CHARS} characters"
        )
    return value


def _positive_int(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise V5ConfigError(f"{context} must be a positive integer")
    return value


def _array(value: object, context: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise V5ConfigError(f"{context} must be an array")
    return tuple(value)


def _kind(value: object, context: str) -> Role:
    if not isinstance(value, str) or value not in _ROLE_VALUES:
        supported = ", ".join(sorted(_ROLE_VALUES))
        raise V5ConfigError(f"{context} must be one of: {supported}")
    return Role(value)


def _parse_node(value: object, *, config_dir: Path, context: str) -> V5Node:
    table = _table(value, context)
    _check_fields(table, _NODE_FIELDS, context)
    node_id = _slug(table["id"], f"{context}.id")
    label = _required_string(table, "label", context, maximum=MAX_V5_LABEL_CHARS)
    kind = _kind(table["kind"], f"{context}.kind")
    role_table = _table(table["role_spec"], f"{context}.role_spec")
    _check_fields(role_table, _ROLE_SPEC_FIELDS, f"{context}.role_spec")
    try:
        role_spec = parse_role(
            role_table,
            context=f"{context}.role_spec",
            config_dir=config_dir,
            kind=kind,
        )
        ref = NodeRef(node_id=node_id, kind=kind)
    except ConfigError as exc:
        raise V5ConfigError(str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise V5ConfigError(f"{context} is invalid") from exc
    return V5Node(ref=ref, label=label, role_spec=role_spec)


def _normalize_route(value: object, *, index: int) -> dict[str, object]:
    context = f"routes[{index}]"
    table = _table(value, context)
    unknown = sorted(set(table) - _ROUTE_FIELDS)
    if unknown:
        raise V5ConfigError(f"{context} has unsupported fields: {', '.join(unknown)}")
    if "task_id" not in table:
        raise V5ConfigError(f"{context} is missing task_id")
    return {key: table.get(key) for key in _ROUTE_FIELDS}


def _parse_team(team_id: str, value: object, *, config_dir: Path) -> V5Team:
    context = f"teams.{team_id}"
    table = _table(value, context)
    _check_fields(table, _TEAM_FIELDS, context)
    name = _required_string(table, "name", context, maximum=MAX_V5_NAME_CHARS)
    max_review_rounds = _positive_int(
        table["max_review_rounds"], f"{context}.max_review_rounds"
    )
    node_values = _array(table["nodes"], f"{context}.nodes")
    if not node_values:
        raise V5ConfigError(f"{context}.nodes must not be empty")
    if len(node_values) > MAX_V5_NODES_PER_TEAM:
        raise V5ConfigError(
            f"{context}.nodes exceeds maximum of {MAX_V5_NODES_PER_TEAM} nodes"
        )
    nodes = tuple(
        _parse_node(
            value,
            config_dir=config_dir,
            context=f"{context}.nodes[{index}]",
        )
        for index, value in enumerate(node_values)
    )
    try:
        task_values = table["tasks"]
        task_specs = parse_task_specs(task_values)
    except ValueError as exc:
        raise V5ConfigError(f"{context}.tasks {exc}") from exc
    route_values = _array(table["routes"], f"{context}.routes")
    normalized_routes = tuple(
        _normalize_route(value, index=index) for index, value in enumerate(route_values)
    )
    graph_data = {
        "nodes": [
            {"node_id": node.ref.node_id, "kind": node.ref.kind.value} for node in nodes
        ],
        "edges": table["edges"],
        "coordination": table["coordination"],
        "routes": list(normalized_routes),
    }
    try:
        graph = GraphSpec.from_dict(graph_data)
        validate_graph(graph, task_specs)
    except (NamedGraphError, TypeError, ValueError) as exc:
        raise V5ConfigError(f"{context}.graph {exc}") from exc
    return V5Team(
        team_id=team_id,
        name=name,
        max_review_rounds=max_review_rounds,
        nodes=nodes,
        graph=graph,
        task_specs=task_specs,
    )


def load_v5_config_data(config_path: Path, data: dict[str, object]) -> V5Config:
    """Parse already-read version-5 data without probing a provider or runtime."""

    if not isinstance(data, dict):
        raise V5ConfigError("config must be a table")
    _check_fields(data, _TOP_LEVEL_FIELDS, "config")
    version = data["version"]
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != V5_CONFIG_VERSION
    ):
        raise V5ConfigError(f"version must be integer {V5_CONFIG_VERSION}")
    runtime = data["runtime"]
    if not isinstance(runtime, str) or runtime not in V5_RUNTIMES:
        supported = ", ".join(sorted(V5_RUNTIMES))
        raise V5ConfigError(f"runtime must be one of: {supported}")
    raw_teams = data["teams"]
    if not isinstance(raw_teams, dict) or not raw_teams:
        raise V5ConfigError("teams must be a non-empty table")
    if len(raw_teams) > MAX_V5_TEAMS:
        raise V5ConfigError(f"teams exceeds maximum of {MAX_V5_TEAMS} teams")

    resolved_path = config_path.expanduser().resolve()
    teams: list[V5Team] = []
    seen_ids: set[str] = set()
    for raw_team_id, raw_team in raw_teams.items():
        if not isinstance(raw_team_id, str):
            raise V5ConfigError("team IDs must be strings")
        team_id = _team_slug(raw_team_id, "team.id")
        if team_id in seen_ids:
            raise V5ConfigError(f"team IDs must be unique: {team_id}")
        seen_ids.add(team_id)
        teams.append(_parse_team(team_id, raw_team, config_dir=resolved_path.parent))
    teams.sort(key=lambda team: team.team_id)
    return V5Config(config_path=resolved_path, runtime=runtime, teams=tuple(teams))


def _selection_values(team: str | Sequence[str] | None) -> tuple[object, ...]:
    if team is None:
        return ()
    if isinstance(team, str):
        return (team,)
    return tuple(team)


def select_v5_team(config: V5Config, team: str | Sequence[str] | None) -> V5Team:
    """Select one exact team ID; aliases, case folding, and defaults are invalid."""

    values = _selection_values(team)
    if not values:
        raise V5ConfigError("--team is required for config version 5")
    if len(values) != 1:
        raise V5ConfigError("exactly one --team must be specified")
    selected = values[0]
    if not isinstance(selected, str) or not selected:
        raise V5ConfigError("--team must be a non-empty string")
    for candidate in config.teams:
        if candidate.team_id == selected:
            return candidate
    raise V5ConfigError(f"unknown team: {selected!r}")


def v5_team_rows(config: V5Config) -> tuple[dict[str, object], ...]:
    """Return deterministic, prompt-free team summaries for inspection."""

    return tuple(
        {
            "id": team.team_id,
            "name": team.name,
            "max_review_rounds": team.max_review_rounds,
            "nodes": [
                {
                    "id": node.ref.node_id,
                    "label": node.label,
                    "kind": node.ref.kind.value,
                    "model": node.role_spec.model,
                    "effort": node.role_spec.effort,
                }
                for node in sorted(team.nodes, key=lambda item: item.ref.node_id)
            ],
            "task_ids": [task.task_id for task in team.task_specs],
        }
        for team in config.teams
    )


def _json_safe(value: object) -> str:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    for source, escaped in (
        ("&", r"\u0026"),
        ("<", r"\u003c"),
        (">", r"\u003e"),
        ("\u2028", r"\u2028"),
        ("\u2029", r"\u2029"),
    ):
        rendered = rendered.replace(source, escaped)
    return rendered + "\n"


def _mermaid_text(value: str) -> str:
    escaped = frozenset("&<>\"'\\`[]{}|*_~()#")
    return "".join(
        f"#{ord(character)};"
        if character in escaped
        else f"\\u{ord(character):04x}"
        if not character.isprintable()
        else character
        for character in value
    )


def _validated_team(config: V5Config, team: str | Sequence[str] | None) -> V5Team:
    selected = select_v5_team(config, team)
    try:
        validate_graph(selected.graph, selected.task_specs)
    except (NamedGraphError, TypeError, ValueError) as exc:
        raise V5ConfigError(f"teams.{selected.team_id}.graph {exc}") from exc
    return selected


def _team_payload(team: V5Team) -> dict[str, object]:
    nodes = sorted(team.nodes, key=lambda item: item.ref.node_id)
    edges = sorted(
        team.graph.edges, key=lambda edge: (edge.kind, edge.source, edge.target)
    )
    routes = sorted(team.graph.routes, key=lambda route: route.task_id)
    return {
        "team_id": team.team_id,
        "name": team.name,
        "max_review_rounds": team.max_review_rounds,
        "nodes": [
            {
                "id": node.ref.node_id,
                "label": node.label,
                "kind": node.ref.kind.value,
                "role_spec": {
                    "provider": node.role_spec.provider,
                    "transport": node.role_spec.transport,
                    "model": node.role_spec.model,
                    "effort": node.role_spec.effort,
                    "permission": node.role_spec.permission,
                },
            }
            for node in nodes
        ],
        "coordination": team.graph.coordination.as_dict(),
        "edges": [edge.as_dict() for edge in edges],
        "routes": [route.as_dict() for route in routes],
    }


def _quote(value: str) -> str:
    rendered = json.dumps(value, ensure_ascii=False)
    for source, escaped in (
        ("&", r"\u0026"),
        ("<", r"\u003c"),
        (">", r"\u003e"),
        ("\u2028", r"\u2028"),
        ("\u2029", r"\u2029"),
    ):
        rendered = rendered.replace(source, escaped)
    return rendered


def _render_ascii(team: V5Team) -> str:
    nodes = sorted(team.nodes, key=lambda item: item.ref.node_id)
    edges = sorted(
        team.graph.edges, key=lambda edge: (edge.kind, edge.source, edge.target)
    )
    routes = sorted(team.graph.routes, key=lambda route: route.task_id)
    coordination = team.graph.coordination
    lines = [
        f"TEAM {_quote(team.team_id)} name={_quote(team.name)}",
        "NODES",
    ]
    for node in nodes:
        spec = node.role_spec
        lines.append(
            "NODE "
            f"id={_quote(node.ref.node_id)} "
            f"kind={_quote(node.ref.kind.value)} "
            f"label={_quote(node.label)} "
            f"provider={_quote(spec.provider)} "
            f"transport={_quote(spec.transport)} "
            f"model={_quote(spec.model)} "
            f"effort={_quote(spec.effort)} "
            f"permission={_quote(spec.permission)}"
        )
    lines.append(
        "COORDINATION "
        f"mode={_quote(coordination.mode)} "
        f"dispatch_mode={_quote(coordination.dispatch_mode)} "
        f"max_active={coordination.max_active} "
        f"entry_nodes={_quote(','.join(coordination.entry_nodes))}"
    )
    lines.append("EDGES")
    for edge in edges:
        lines.append(
            f"{_quote(edge.source)} --[{_quote(edge.kind)}]--> {_quote(edge.target)}"
        )
    lines.append("ROUTES")
    for route in routes:
        lines.append(
            f"ROUTE task_id={_quote(route.task_id)} "
            f"plan_writer={_quote(route.plan_writer or '')} "
            f"plan_reviewer={_quote(route.plan_reviewer or '')} "
            f"implementation_writer={_quote(route.implementation_writer or '')} "
            f"implementation_reviewer={_quote(route.implementation_reviewer or '')}"
        )
    return "\n".join(lines) + "\n"


def _render_mermaid(team: V5Team) -> str:
    nodes = sorted(team.nodes, key=lambda item: item.ref.node_id)
    edges = sorted(
        team.graph.edges, key=lambda edge: (edge.kind, edge.source, edge.target)
    )
    routes = sorted(team.graph.routes, key=lambda route: route.task_id)
    aliases = {node.ref.node_id: f"n{index}" for index, node in enumerate(nodes)}
    coordination = team.graph.coordination
    title = (
        f"{team.name} (id: {team.team_id}, mode: {coordination.mode}, "
        f"dispatch: {coordination.dispatch_mode}, "
        f"max_active: {coordination.max_active})"
    )
    lines = [
        "flowchart TD",
        (f'    subgraph team_graph["{_mermaid_text(title)}"]'),
    ]
    for node in nodes:
        spec = node.role_spec
        label = (
            f"{node.label} (id: {node.ref.node_id}, kind: {node.ref.kind.value}, "
            f"model: {spec.model}, effort: {spec.effort})"
        )
        lines.append(f'        {aliases[node.ref.node_id]}["{_mermaid_text(label)}"]')
    lines.append("    end")
    for edge in edges:
        lines.append(
            f"    {aliases[edge.source]} -->|{_mermaid_text(edge.kind)}| "
            f"{aliases[edge.target]}"
        )
    for route in routes:
        lines.append(
            "    %% route "
            f"task={_mermaid_text(route.task_id)} "
            f"plan={_mermaid_text(route.plan_writer or '-')}/"
            f"{_mermaid_text(route.plan_reviewer or '-')} "
            f"implementation={_mermaid_text(route.implementation_writer or '-')}/"
            f"{_mermaid_text(route.implementation_reviewer or '-')}"
        )
    return "\n".join(lines) + "\n"


def render_v5_team(
    config: V5Config,
    team: str | Sequence[str] | None,
    format: str,
) -> str:
    """Render a selected named graph without exposing prompt file contents."""

    selected = _validated_team(config, team)
    if format == "json":
        return _json_safe(_team_payload(selected))
    if format == "ascii":
        return _render_ascii(selected)
    if format == "mermaid":
        return _render_mermaid(selected)
    raise V5ConfigError("output format must be one of: ascii, json, mermaid")
