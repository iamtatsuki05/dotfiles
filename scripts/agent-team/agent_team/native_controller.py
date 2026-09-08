"""Mode-specific identity keys for native controller lifecycle state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn


@dataclass(frozen=True, slots=True)
class ControllerKeys:
    """The state keys that identify one native controller child."""

    terminal: str
    argv: str
    process: str
    pid: str


_AGENT_KEYS = ControllerKeys(
    terminal="main_terminal",
    argv="main_argv",
    process="main_process",
    pid="agent_pid",
)
_PROGRAM_KEYS = ControllerKeys(
    terminal="coordinator_terminal",
    argv="coordinator_argv",
    process="coordinator_process",
    pid="coordinator_pid",
)


def _fail(message: str) -> NoReturn:
    # Import lazily so runtime.py can use this module without a module cycle.
    from .runtime import RuntimeValidationError

    raise RuntimeValidationError(message)


def controller_keys(state: Mapping[str, object]) -> ControllerKeys:
    """Return the exact native controller fields for a validated state shape.

    Version 3 is the fixed-role agent contract.  Version 4 derives the mode
    from its parsed graph; version 5 is reserved for an explicitly parallel
    program graph.  A program graph never acquires a fabricated Main identity
    and an agent graph never uses coordinator aliases.
    """

    if not isinstance(state, Mapping):
        _fail("agent-team native controller state must be an object")
    version = state.get("version")
    if version == 3:
        if "graph" in state:
            _fail("version-3 state must not contain a graph")
        return _AGENT_KEYS
    if version not in {4, 5}:
        _fail("agent-team native controller state version is unsupported")

    from .named_graph import GraphSpec

    try:
        graph = GraphSpec.from_dict(state.get("graph"))
    except (TypeError, ValueError) as exc:
        _fail(f"agent-team native controller graph is invalid: {exc}")
    if graph.coordination.mode == "agent":
        if version == 5:
            _fail("parallel state requires program parallel coordination")
        if graph.coordination.dispatch_mode != "serial":
            _fail("named state requires serial coordination")
        return _AGENT_KEYS
    if graph.coordination.mode == "program" and (
        (version == 4 and graph.coordination.dispatch_mode == "serial")
        or (version == 5 and graph.coordination.dispatch_mode == "parallel")
    ):
        return _PROGRAM_KEYS
    if version == 4:
        _fail("named state requires serial coordination")
    if version == 5:
        _fail("parallel state requires program parallel coordination")
    _fail("agent-team native controller coordination mode is invalid")


__all__ = ("ControllerKeys", "controller_keys")
