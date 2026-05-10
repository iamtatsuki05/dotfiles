from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from typing import Protocol

from .models import CommandSpec, ExecutionResult
from .timeutil import format_timestamp, now_local


class CommandRunner(Protocol):
    def run(self, spec: CommandSpec, *, on_start: Callable[[int], None] | None = None) -> ExecutionResult: ...


class SubprocessRunner:
    def run(self, spec: CommandSpec, *, on_start: Callable[[int], None] | None = None) -> ExecutionResult:
        started = now_local()
        try:
            env = os.environ.copy()
            env.update(spec.env)
            process = subprocess.Popen(
                spec.argv,
                cwd=spec.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            if on_start is not None:
                on_start(process.pid)
            stdout, stderr = process.communicate()
            return ExecutionResult(
                command=spec,
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                started_at=format_timestamp(started),
                finished_at=format_timestamp(now_local()),
                pid=process.pid,
            )
        except FileNotFoundError as exc:
            return ExecutionResult(
                command=spec,
                exit_code=127,
                stdout="",
                stderr=str(exc),
                started_at=format_timestamp(started),
                finished_at=format_timestamp(now_local()),
                pid=None,
            )
