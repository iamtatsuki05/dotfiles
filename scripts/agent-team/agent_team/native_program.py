"""Drive declared native tasks without a Main model or a second task ledger."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from . import program_driver
from .contracts import ErrorCode, RuntimeFailure
from .native_controller import controller_keys
from .runtime import read_state


def _ready_state(path: Path, run_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5.0
    while True:
        state = read_state(path)
        keys = controller_keys(state)
        if state.get("run_id") != run_id or keys.pid != "coordinator_pid":
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "program run identity changed"
            )
        native = cast(Mapping[str, object], state["native"])
        if native.get("phase") == "stopping":
            raise RuntimeFailure(ErrorCode.BUSY, "program run is stopping")
        process = native.get(keys.process)
        if isinstance(process, Mapping):
            if native.get("phase") != "running" or process.get(keys.pid) != os.getpid():
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "program child receipt does not match this process",
                )
            return state
        if time.monotonic() >= deadline:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH, "program child receipt was not published"
            )
        time.sleep(0.05)


def run(path: Path, run_id: str) -> int:
    from .cli import _management_plan_from_state, _runtime_engine, _start_spec
    from .native_backend import NativeBackend

    try:
        state = _ready_state(path, run_id)
        plan = _management_plan_from_state(state)
        _engine, backend = _runtime_engine(plan, resume_existing=True)
        if not isinstance(backend, NativeBackend):
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "program coordinator requires a native runtime",
            )
        backend.start(_start_spec(plan, attach=False))
        return program_driver.drive(backend)
    except (RuntimeFailure, OSError, TypeError, ValueError) as exc:
        program_driver._emit(
            {
                "status": "unresolved",
                "reason": str(exc)
                if isinstance(exc, RuntimeFailure)
                else type(exc).__name__,
            }
        )
        return 1
