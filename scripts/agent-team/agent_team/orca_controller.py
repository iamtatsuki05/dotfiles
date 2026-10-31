"""Mode-specific controller identity for Orca runtime state."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final, NoReturn

from .named_graph import GraphSpec

_LAUNCH_NONCE_RE: Final = re.compile(r"[a-z0-9]{8,64}\Z")
_ARGV_DIGEST_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_PROGRAM_VERSIONS: Final = {4: "serial", 5: "parallel"}
_PENDING_FIELDS: Final = frozenset(
    {"phase", "run_id", "coordinator_terminal", "argv_sha256", "launch_nonce"}
)
_PROCESS_FIELDS: Final = frozenset(
    {
        "pid",
        "process_group_id",
        "launch_nonce",
        "argv",
        "phase",
        "exit_code",
        "cli_cleanup_confirmed",
    }
)
_MISSING: Final = object()


def _fail(message: str) -> NoReturn:
    # Import lazily so runtime.py can import this module without a cycle.
    from .runtime import RuntimeValidationError

    raise RuntimeValidationError(message)


def _state_mapping(state: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(state, Mapping):
        _fail("Orca controller state must be an object")
    if state.get("runtime") != "orca":
        _fail("Orca controller state runtime must be orca")
    return state


def _graph(state: Mapping[str, object]) -> GraphSpec:
    try:
        return GraphSpec.from_dict(state.get("graph"))
    except (TypeError, ValueError) as exc:
        _fail(f"Orca controller graph is invalid: {exc}")


def _reject_aliases(state: Mapping[str, object], *, program: bool) -> None:
    native_fields = {
        "native",
        "native_result",
        "native_question",
        "pending_delivery",
        "main_argv",
        "main_process",
        "agent_pid",
        "coordinator_pid",
    }
    if native_fields.intersection(state):
        _fail("Orca controller state must not contain native metadata")
    if program:
        forbidden: tuple[str, ...] = ("main_terminal",)
    else:
        forbidden = (
            "coordinator_terminal",
            "coordinator_argv",
            "coordinator_process",
            "pending_coordinator_start",
        )
    if any(field in state for field in forbidden):
        mode = "program" if program else "agent"
        _fail(f"Orca {mode} state contains another controller identity")


def controller_key(state: Mapping[str, object]) -> str:
    """Return the exact top-level terminal field for an Orca controller."""

    state = _state_mapping(state)
    version = state.get("version")
    if type(version) is not int:
        _fail("Orca controller state version is invalid")
    if version == 3:
        if "graph" in state:
            _fail("Orca version-3 agent state must not contain a graph")
        _reject_aliases(state, program=False)
        return "main_terminal"
    if version not in _PROGRAM_VERSIONS:
        _fail("Orca controller state version is unsupported")
    graph = _graph(state)
    expected_dispatch = _PROGRAM_VERSIONS[version]
    if graph.coordination.dispatch_mode != expected_dispatch:
        _fail(f"Orca version-{version} state requires {expected_dispatch} coordination")
    if graph.coordination.mode == "agent":
        _reject_aliases(state, program=False)
        return "main_terminal"
    if graph.coordination.mode == "program":
        _reject_aliases(state, program=True)
        return "coordinator_terminal"
    _fail("Orca controller coordination mode is invalid")


def controller_terminal(state: Mapping[str, object]) -> str:
    """Return the saved terminal handle for the exact controller mode."""

    key = controller_key(state)
    value = state.get(key)
    if not isinstance(value, str) or not value or "\0" in value:
        _fail(f"Orca state is missing {key}")
    return value


def is_program(state: Mapping[str, object]) -> bool:
    """Return whether a validated Orca named state owns a program coordinator."""

    return controller_key(state) == "coordinator_terminal"


def _nonce(value: object, context: str) -> str:
    if not isinstance(value, str) or _LAUNCH_NONCE_RE.fullmatch(value) is None:
        _fail(f"Orca coordinator {context} is invalid")
    return value


def _argv(value: object, context: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item or "\0" in item for item in value)
        or not Path(value[0]).is_absolute()
    ):
        _fail(f"Orca coordinator {context} is invalid")
    return value


def _fixed_argv(state: Mapping[str, object], argv: object, nonce: str) -> None:
    actual = _argv(argv, "argv")
    state_path = state.get("state_path")
    run_id = state.get("run_id")
    if not isinstance(state_path, str) or not state_path:
        _fail("Orca coordinator state_path is invalid")
    if not isinstance(run_id, str) or not run_id:
        _fail("Orca coordinator run_id is invalid")
    expected = [
        actual[0],
        "-P",
        "-m",
        "agent_team",
        "_orca-program-run",
        "--state",
        state_path,
        "--run-id",
        run_id,
        "--launch-nonce",
        nonce,
    ]
    if actual != expected:
        _fail("Orca coordinator argv does not match its fixed launch contract")


def _argv_digest(argv: list[str]) -> str:
    encoded = json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _validate_pending(
    state: Mapping[str, object], pending: object, argv: list[str]
) -> str:
    if not isinstance(pending, Mapping) or set(pending) != _PENDING_FIELDS:
        _fail("Orca coordinator startup marker fields are invalid")
    phase = pending.get("phase")
    if phase not in {"prepared", "send_started", "sent"}:
        _fail("Orca coordinator startup marker phase is invalid")
    if pending.get("run_id") != state.get("run_id"):
        _fail("Orca coordinator startup marker Run identity changed")
    if pending.get("coordinator_terminal") != state.get("coordinator_terminal"):
        _fail("Orca coordinator startup marker terminal identity changed")
    digest = pending.get("argv_sha256")
    if (
        not isinstance(digest, str)
        or _ARGV_DIGEST_RE.fullmatch(digest) is None
        or digest != _argv_digest(argv)
    ):
        _fail("Orca coordinator startup marker argv identity changed")
    nonce = _nonce(pending.get("launch_nonce"), "startup launch nonce")
    _fixed_argv(state, argv, nonce)
    return nonce


def _validate_process(
    state: Mapping[str, object], process: object, coordinator_argv: list[str]
) -> str:
    if not isinstance(process, Mapping) or set(process) != _PROCESS_FIELDS:
        _fail("Orca coordinator process receipt fields are invalid")
    pid = process.get("pid")
    group = process.get("process_group_id")
    if type(pid) is not int or pid <= 0 or type(group) is not int or group <= 0:
        _fail("Orca coordinator process identity is invalid")
    nonce = _nonce(process.get("launch_nonce"), "process launch nonce")
    process_argv = _argv(process.get("argv"), "process argv")
    if process_argv[1:] != coordinator_argv[1:]:
        _fail("Orca coordinator process argv identity changed")
    phase = process.get("phase")
    if phase == "running":
        if (
            process.get("exit_code") is not None
            or process.get("cli_cleanup_confirmed") is not None
        ):
            _fail(
                "running Orca coordinator process must not have exit or cleanup evidence"
            )
    elif phase == "exited":
        if (
            type(process.get("exit_code")) is not int
            or type(process.get("cli_cleanup_confirmed")) is not bool
        ):
            _fail("exited Orca coordinator process has invalid exit evidence")
    else:
        _fail("Orca coordinator process phase is invalid")
    return nonce


def _validate_controller_state(state: Mapping[str, object]) -> None:
    """Validate Orca-only controller lifecycle fields at the state boundary."""

    key = controller_key(state)
    controller_terminal(state)
    if key != "coordinator_terminal":
        return

    coordinator_argv = _argv(state.get("coordinator_argv"), "argv")
    process = state.get("coordinator_process", _MISSING)
    if process is _MISSING:
        _fail("Orca program state is missing coordinator_process")

    pending = state.get("pending_coordinator_start", _MISSING)
    process_nonce: str | None = None
    if process is not None:
        process_nonce = _validate_process(state, process, coordinator_argv)

    argv_nonce = _nonce(coordinator_argv[-1], "argv launch nonce")
    _fixed_argv(state, coordinator_argv, argv_nonce)

    pending_nonce: str | None = None
    if pending is not _MISSING:
        pending_nonce = _validate_pending(state, pending, coordinator_argv)
    if process is None and pending is _MISSING:
        _fail("Orca program startup marker is required before coordinator readiness")
    if process_nonce is not None and process_nonce != argv_nonce:
        _fail("Orca coordinator process launch nonce does not match argv")
    if pending_nonce is not None and pending_nonce != argv_nonce:
        _fail("Orca coordinator startup nonce does not match argv")
    if (
        process_nonce is not None
        and pending_nonce is not None
        and process_nonce != pending_nonce
    ):
        _fail("Orca coordinator startup and process nonces differ")


__all__ = ("controller_key", "controller_terminal", "is_program")
