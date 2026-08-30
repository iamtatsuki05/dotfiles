from __future__ import annotations

import contextlib
import io
import json
import multiprocessing
import os
import selectors
import shutil
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import ClassVar, cast
from unittest import mock

import agent_team.adapters as adapters_module
import agent_team.backend as backend_module
import agent_team.cleanup as cleanup_module
import agent_team.cli as cli_module
import agent_team.mcp_server as mcp_module
import agent_team.orca as orca_module
import agent_team.runtime as runtime_module
from agent_team.adapters import (
    ExecutionError,
    ProcessResult,
    ProcessRunner,
    _bounded_communicate,
)
from agent_team.backend import (
    OrcaBackend,
    OrcaClient,
    OrcaCommandError,
    OrcaError,
    OrcaProtocolError,
    OrcaTransportError,
    TerminalCloseVerdict,
    TerminalSwitchVerdict,
    WorkerStopAlreadySettledVerdict,
    WorkerStopContextOnlyVerdict,
    WorkerStopOwnedVerdict,
    WorkerStopUnknownVerdict,
    WorkerStopVerdict,
    _LifecycleReservation,
)
from agent_team.cleanup import StartupCleanup
from agent_team.contracts import (
    Attach,
    ErrorCode,
    Role,
    RoleSpec,
    RuntimeFailure,
    StartSpec,
    Status,
)
from agent_team.runtime import write_state
from agent_team.workflow import WorkflowEngine

ORCA_COMMAND = orca_module.orca_executable()


def _reservation_probe(
    state_path: str,
    barrier: multiprocessing.synchronize.Barrier,
    result_queue: multiprocessing.queues.Queue[str],
) -> None:
    reservation = _LifecycleReservation(Path(state_path), create_parent=True)
    barrier.wait()
    try:
        reservation.acquire()
    except RuntimeFailure as exc:
        result_queue.put(exc.code.value)
        return
    result_queue.put("acquired")
    time.sleep(0.3)
    reservation.release()


class RecordingRunner:
    def __init__(self, responses: Mapping[tuple[str, ...], ProcessResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], Path, float]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_text: str | None = None,
        timeout_seconds: float = 900.0,
    ) -> ProcessResult:
        del env, input_text
        call = (tuple(argv), cwd, timeout_seconds)
        self.calls.append(call)
        try:
            return self.responses[tuple(argv)]
        except KeyError as exc:
            raise AssertionError(f"unexpected Orca command: {argv!r}") from exc


class FakeOrcaClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.close_error: Exception | None = None
        self.run_create_result: str | Exception = OrcaCommandError("run-create")
        self.switch_error: Exception | None = None
        self.switch_result: TerminalSwitchVerdict | None = None
        self.worktree_result: dict[str, object] | None = None
        self.terminal_result: dict[str, object] | None = None
        self.terminal_create_result: dict[str, object] | None = None
        self.terminal_create_error: Exception | None = None
        self.worker_worktree_id: str | None = "repo::project"
        self.worker_stop_result: WorkerStopVerdict | None = None
        self.worker_stop_error: Exception | None = None
        self.close_result: TerminalCloseVerdict | None = None
        self.terminal_show_error: Exception | None = None
        self.worker_show_error: Exception | None = None
        self.run_show_error: Exception | None = None
        self.run_show_mutator: Callable[[Path], None] | None = None

    def status(self, cwd: Path) -> dict[str, object]:
        self.calls.append(("status",))
        return {"runtime": {"state": "ready"}, "graph": {"state": "ready"}}

    def worktree_current(self, cwd: Path) -> dict[str, object]:
        self.calls.append(("worktree-current",))
        return self.worktree_result or {
            "worktree": {"id": "repo::project", "path": str(cwd)}
        }

    def terminal_create(
        self, *, worktree_id: str, title: str, command: str, cwd: Path
    ) -> dict[str, object]:
        self.calls.append(("terminal-create", worktree_id, title, command))
        if self.terminal_create_error is not None:
            raise self.terminal_create_error
        return self.terminal_create_result or {
            "terminal": {
                "handle": "term_main",
                "worktreeId": worktree_id,
                "title": title,
            }
        }

    def terminal_wait(self, *, terminal_id: str, cwd: Path) -> None:
        self.calls.append(("terminal-wait", terminal_id))

    def run_create(self, *, objective: str, terminal_id: str, cwd: Path) -> str:
        self.calls.append(("run-create", terminal_id))
        if isinstance(self.run_create_result, Exception):
            raise self.run_create_result
        return self.run_create_result

    def terminal_switch(self, *, terminal_id: str, cwd: Path) -> TerminalSwitchVerdict:
        self.calls.append(("terminal-switch", terminal_id))
        if self.switch_error is not None:
            raise self.switch_error
        return self.switch_result or TerminalSwitchVerdict(
            handle=terminal_id, navigated=True
        )

    def terminal_close(self, *, terminal_id: str, cwd: Path) -> TerminalCloseVerdict:
        self.calls.append(("terminal-close", terminal_id))
        if self.close_error is not None:
            raise self.close_error
        return self.close_result or TerminalCloseVerdict(
            handle=terminal_id, close_mode=None, pty_killed=True
        )

    def worker_stop(self, *, dispatch_id: str, cwd: Path) -> WorkerStopVerdict:
        self.calls.append(("worker-stop", dispatch_id))
        if self.worker_stop_error is not None:
            raise self.worker_stop_error
        return self.worker_stop_result or WorkerStopOwnedVerdict(
            dispatch_id=dispatch_id,
            state="stopped",
            process_action="closed_agent_terminal",
            already_settled=False,
            pty_killed=True,
        )

    def run_show(self, *, run_id: str, cwd: Path) -> dict[str, object]:
        self.calls.append(("run-show", run_id))
        if self.run_show_error is not None:
            error = self.run_show_error
            self.run_show_error = None
            raise error
        if self.run_show_mutator is not None:
            self.run_show_mutator(cwd)
        return {
            "run": {
                "id": run_id,
                "objective": (
                    f"team-project: Planner / Worker / Reviewer coordination for {cwd}"
                ),
                "coordinator_handle": "term_main",
            }
        }

    def terminal_show(self, *, terminal_id: str, cwd: Path) -> dict[str, object]:
        self.calls.append(("terminal-show", terminal_id))
        if self.terminal_show_error is not None:
            error = self.terminal_show_error
            self.terminal_show_error = None
            raise error
        if self.terminal_result is not None:
            return self.terminal_result
        role = "main" if terminal_id == "term_main" else "planner"
        return {
            "terminal": {
                "handle": terminal_id,
                "worktreeId": "repo::project",
                "title": f"team-project-{role}",
                "worktreePath": str(cwd),
            }
        }

    def worker_list(self, *, run_id: str, cwd: Path) -> dict[str, object]:
        self.calls.append(("worker-list", run_id))
        return {"workers": []}

    def worker_show(self, *, dispatch_id: str, cwd: Path) -> dict[str, object]:
        self.calls.append(("worker-show", dispatch_id))
        if self.worker_show_error is not None:
            error = self.worker_show_error
            self.worker_show_error = None
            raise error
        return {
            "dispatch": {
                "id": dispatch_id,
                "task_id": "task_worker",
                "run_id": "run_1",
                "assignee_handle": "term_planner",
            },
            "worker": {
                "dispatch_id": dispatch_id,
                "worktree_id": self.worker_worktree_id,
                "agent_terminal_handle": "term_planner",
                "state": "ready",
            },
            "terminal": {
                "handle": "term_planner",
                "worktreePath": str(cwd),
            },
        }


def start_spec(root: Path) -> StartSpec:
    state_path = root / "state" / "team-project" / "state.json"
    roles = {
        role: RoleSpec(
            provider="codex",
            transport="direct",
            model="gpt-test",
            effort="medium",
            permission="read-only",
            instructions=role.value,
            execution="tui_direct",
        )
        for role in Role
    }
    return StartSpec(
        team_id="team-project",
        workspace=root / "project",
        config_path=root / "config.toml",
        state_path=state_path,
        role_specs=roles,
        attach=False,
    )


def existing_state(spec: StartSpec) -> dict[str, object]:
    return {
        "version": 3,
        "runtime": "orca",
        "team_id": spec.team_id,
        "workspace": str(spec.workspace),
        "config_path": str(spec.config_path),
        "state_path": str(spec.state_path),
        "launcher_path": "/tmp/agent-team",
        "worktree_id": "repo::project",
        "orca_socket": "/tmp/orca.sock",
        "run_id": "run_1",
        "main_terminal": "term_main",
        "role_specs": {
            role.value: {
                "provider": role_spec.provider,
                "transport": role_spec.transport,
                "model": role_spec.model,
                "effort": role_spec.effort,
                "permission": role_spec.permission,
                "instructions": role_spec.instructions,
                "execution": role_spec.execution,
            }
            for role, role_spec in spec.role_specs.items()
        },
        "roles": {},
    }


class OrcaClientContractTest(unittest.TestCase):
    def test_orca_executable_is_platform_specific_and_no_windows_fallback(self) -> None:
        runner = RecordingRunner({})
        with mock.patch.object(orca_module.sys, "platform", "darwin"):
            self.assertEqual(orca_module.orca_executable(), "orca")
            self.assertEqual(OrcaClient(runner=runner).executable, "orca")
        with mock.patch.object(orca_module.sys, "platform", "linux"):
            self.assertEqual(orca_module.orca_executable(), "orca-ide")
            self.assertEqual(OrcaClient(runner=runner).executable, "orca-ide")
        with (
            mock.patch.object(orca_module.sys, "platform", "win32"),
            self.assertRaisesRegex(OrcaError, "POSIX runtime"),
        ):
            orca_module.orca_executable()

    def test_cli_prerequisites_use_the_shared_linux_orca_command(self) -> None:
        with (
            mock.patch.object(cli_module.sys, "platform", "linux"),
            mock.patch.object(cli_module, "require_binary") as require_binary,
            mock.patch.object(cli_module.os, "access", return_value=True),
        ):
            cli_module._start_prerequisites({"roles": {}})

        require_binary.assert_any_call("orca-ide")

    def test_cli_raw_reporter_uses_the_shared_linux_orca_command(self) -> None:
        completed = mock.Mock(returncode=0)
        with (
            mock.patch.object(
                cli_module,
                "orca_executable",
                return_value="orca-ide",
                create=True,
            ),
            mock.patch.object(
                cli_module.subprocess, "run", return_value=completed
            ) as run,
        ):
            cli_module.run_orca(["orchestration", "send"], cwd=Path("/tmp"))

        self.assertEqual(run.call_args.args[0], ["orca-ide", "orchestration", "send"])

    def test_client_uses_fixed_json_commands_and_bounded_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cwd = Path(temp_dir)
            runner = RecordingRunner(
                {
                    (ORCA_COMMAND, "status", "--json"): ProcessResult(
                        0,
                        json.dumps(
                            {
                                "ok": True,
                                "result": {
                                    "runtime": {"state": "ready"},
                                    "graph": {"state": "ready"},
                                },
                            }
                        ),
                        "",
                    ),
                    (ORCA_COMMAND, "worktree", "current", "--json"): ProcessResult(
                        0,
                        json.dumps(
                            {
                                "ok": True,
                                "result": {"worktree": {"id": "repo::project"}},
                            }
                        ),
                        "",
                    ),
                }
            )
            client = OrcaClient(runner=runner)

            self.assertEqual(
                client.status(cwd),
                {"runtime": {"state": "ready"}, "graph": {"state": "ready"}},
            )
            self.assertEqual(
                client.worktree_current(cwd),
                {"worktree": {"id": "repo::project"}},
            )

        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                (ORCA_COMMAND, "status", "--json"),
                (ORCA_COMMAND, "worktree", "current", "--json"),
            ],
        )
        self.assertTrue(all(call[2] <= 900 for call in runner.calls))

    def test_client_validates_run_create_objective_and_coordinator(self) -> None:
        objective = "team-project: Planner / Worker / Reviewer coordination"
        runner = RecordingRunner(
            {
                (
                    ORCA_COMMAND,
                    "orchestration",
                    "run-create",
                    "--objective",
                    objective,
                    "--from",
                    "term_main",
                    "--json",
                ): ProcessResult(
                    0,
                    json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "run": {
                                    "id": "run_1",
                                    "objective": "foreign objective",
                                    "coordinator_handle": "term_main",
                                }
                            },
                        }
                    ),
                    "",
                )
            }
        )
        client = OrcaClient(runner=runner)

        with self.assertRaises(OrcaProtocolError):
            client.run_create(
                objective=objective, terminal_id="term_main", cwd=Path("/tmp")
            )

    def test_client_requires_terminal_wait_success_and_close_identity(self) -> None:
        wait_argv = (
            ORCA_COMMAND,
            "terminal",
            "wait",
            "--terminal",
            "term_main",
            "--for",
            "tui-idle",
            "--timeout-ms",
            "180000",
            "--json",
        )
        close_argv = (
            ORCA_COMMAND,
            "terminal",
            "close",
            "--terminal",
            "term_main",
            "--json",
        )
        runner = RecordingRunner(
            {
                wait_argv: ProcessResult(
                    0,
                    json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "wait": {
                                    "handle": "term_main",
                                    "condition": "tui-idle",
                                    "satisfied": False,
                                }
                            },
                        }
                    ),
                    "",
                ),
                close_argv: ProcessResult(
                    0,
                    json.dumps(
                        {"ok": True, "result": {"close": {"handle": "foreign"}}}
                    ),
                    "",
                ),
            }
        )
        client = OrcaClient(runner=runner)

        with self.assertRaises(OrcaProtocolError):
            client.terminal_wait(terminal_id="term_main", cwd=Path("/tmp"))
        with self.assertRaises(OrcaProtocolError):
            client.terminal_close(terminal_id="term_main", cwd=Path("/tmp"))

    def test_client_returns_unknown_worker_stop_verdict_from_nonzero_json(self) -> None:
        argv = (
            ORCA_COMMAND,
            "orchestration",
            "worker-stop",
            "--dispatch",
            "dispatch_1",
            "--json",
        )
        runner = RecordingRunner(
            {
                argv: ProcessResult(
                    1,
                    json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "dispatchId": "dispatch_1",
                                "state": "stop_unknown",
                                "processAction": "none",
                                "alreadySettled": False,
                            },
                        }
                    ),
                    "",
                )
            }
        )
        client = OrcaClient(runner=runner)

        verdict = client.worker_stop(dispatch_id="dispatch_1", cwd=Path("/tmp"))
        self.assertEqual(verdict.state, "stop_unknown")

        runner.responses[argv] = ProcessResult(
            1,
            json.dumps(
                {
                    "ok": False,
                    "result": {
                        "dispatchId": "dispatch_1",
                        "state": "stopped",
                        "processAction": "closed_agent_terminal",
                        "alreadySettled": False,
                        "close": {"ptyKilled": True},
                    },
                    "error": {"code": "stop_failed"},
                }
            ),
            "",
        )
        with self.assertRaises(OrcaCommandError):
            client.worker_stop(dispatch_id="dispatch_1", cwd=Path("/tmp"))

    def test_client_rejects_worker_stop_without_a_verdict(self) -> None:
        argv = (
            ORCA_COMMAND,
            "orchestration",
            "worker-stop",
            "--dispatch",
            "dispatch_1",
            "--json",
        )
        runner = RecordingRunner(
            {
                argv: ProcessResult(
                    1,
                    json.dumps({"ok": False, "error": {"code": "failed"}}),
                    "",
                )
            }
        )
        client = OrcaClient(runner=runner)

        with self.assertRaises(OrcaProtocolError):
            client.worker_stop(dispatch_id="dispatch_1", cwd=Path("/tmp"))

    def test_client_decodes_strict_worker_stop_verdict(self) -> None:
        argv = (
            ORCA_COMMAND,
            "orchestration",
            "worker-stop",
            "--dispatch",
            "dispatch_1",
            "--json",
        )
        result = {
            "dispatchId": "dispatch_1",
            "state": "stopped",
            "processAction": "closed_agent_terminal",
            "alreadySettled": False,
            "close": {"ptyKilled": True},
        }
        runner = RecordingRunner(
            {argv: ProcessResult(0, json.dumps({"ok": True, "result": result}), "")}
        )
        client = OrcaClient(runner=runner)

        verdict = client.worker_stop(dispatch_id="dispatch_1", cwd=Path("/tmp"))

        self.assertEqual(
            verdict,
            WorkerStopOwnedVerdict(
                dispatch_id="dispatch_1",
                state="stopped",
                process_action="closed_agent_terminal",
                already_settled=False,
                pty_killed=True,
            ),
        )

        runner.responses[argv] = ProcessResult(
            0,
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "dispatchId": "dispatch_1",
                        "state": "stopped",
                        "processAction": "none",
                        "alreadySettled": False,
                    },
                }
            ),
            "",
        )
        context_only = client.worker_stop(dispatch_id="dispatch_1", cwd=Path("/tmp"))
        self.assertEqual(
            context_only,
            WorkerStopContextOnlyVerdict(
                dispatch_id="dispatch_1",
                state="stopped",
                process_action="none",
                already_settled=False,
            ),
        )

        runner.responses[argv] = ProcessResult(
            0,
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "dispatchId": "dispatch_1",
                        "state": "succeeded",
                        "processAction": "none",
                        "alreadySettled": True,
                    },
                }
            ),
            "",
        )
        settled = client.worker_stop(dispatch_id="dispatch_1", cwd=Path("/tmp"))
        self.assertEqual(
            settled,
            WorkerStopAlreadySettledVerdict(
                dispatch_id="dispatch_1",
                state="succeeded",
                process_action="none",
            ),
        )

        runner.responses[argv] = ProcessResult(
            0,
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "dispatchId": "dispatch_1",
                        "state": "stop_unknown",
                        "processAction": "none",
                        "alreadySettled": False,
                    },
                }
            ),
            "",
        )
        unknown = client.worker_stop(dispatch_id="dispatch_1", cwd=Path("/tmp"))
        self.assertEqual(
            unknown,
            WorkerStopUnknownVerdict(
                dispatch_id="dispatch_1",
                state="stop_unknown",
                process_action="none",
                already_settled=False,
            ),
        )

        runner.responses[argv] = ProcessResult(
            0,
            json.dumps(
                {
                    "ok": True,
                    "result": {**result, "state": "still_running"},
                }
            ),
            "",
        )
        with self.assertRaises(OrcaProtocolError):
            client.worker_stop(dispatch_id="dispatch_1", cwd=Path("/tmp"))

        invalid_results = (
            {**result, "processAction": "still_running"},
            {**result, "alreadySettled": "false"},
            {**result, "close": {"ptyKilled": "true"}},
        )
        for invalid in invalid_results:
            with self.subTest(invalid=invalid):
                runner.responses[argv] = ProcessResult(
                    0,
                    json.dumps({"ok": True, "result": invalid}),
                    "",
                )
                with self.assertRaises(OrcaProtocolError):
                    client.worker_stop(dispatch_id="dispatch_1", cwd=Path("/tmp"))

    def test_client_rejects_unknown_worker_stop_variants(self) -> None:
        argv = (
            ORCA_COMMAND,
            "orchestration",
            "worker-stop",
            "--dispatch",
            "dispatch_1",
            "--json",
        )
        runner = RecordingRunner({argv: ProcessResult(0, "", "")})
        client = OrcaClient(runner=runner)
        invalid_results = (
            {
                "dispatchId": "dispatch_1",
                "state": "stopped",
                "processAction": "closed_agent_terminal",
                "alreadySettled": False,
                "close": {"ptyKilled": False},
            },
            {
                "dispatchId": "dispatch_1",
                "state": "succeeded",
                "processAction": "none",
                "alreadySettled": False,
            },
            {
                "dispatchId": "dispatch_1",
                "state": "stop_unknown",
                "processAction": "none",
                "alreadySettled": True,
            },
            {
                "dispatchId": "dispatch_1",
                "state": "stopped",
                "processAction": "none",
                "alreadySettled": False,
                "close": None,
            },
            {
                "dispatchId": "dispatch_1",
                "state": "stop_unknown",
                "processAction": "none",
            },
        )
        for result in invalid_results:
            with self.subTest(result=result):
                runner.responses[argv] = ProcessResult(
                    0,
                    json.dumps({"ok": True, "result": result}),
                    "",
                )
                with self.assertRaises(OrcaProtocolError):
                    client.worker_stop(dispatch_id="dispatch_1", cwd=Path("/tmp"))

    def test_client_accepts_actual_unknown_and_context_settled_variants(self) -> None:
        argv = (
            ORCA_COMMAND,
            "orchestration",
            "worker-stop",
            "--dispatch",
            "dispatch_1",
            "--json",
        )
        runner = RecordingRunner({argv: ProcessResult(0, "", "")})
        client = OrcaClient(runner=runner)
        for process_action in ("none", "unknown", "closed_agent_terminal"):
            with self.subTest(process_action=process_action):
                runner.responses[argv] = ProcessResult(
                    0,
                    json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "dispatchId": "dispatch_1",
                                "state": "stop_unknown",
                                "processAction": process_action,
                                "alreadySettled": False,
                            },
                        }
                    ),
                    "",
                )
                verdict = client.worker_stop(dispatch_id="dispatch_1", cwd=Path("/tmp"))
                self.assertEqual(
                    verdict,
                    WorkerStopUnknownVerdict(
                        dispatch_id="dispatch_1",
                        state="stop_unknown",
                        process_action=process_action,
                        already_settled=False,
                    ),
                )
        for state in ("completed", "circuit_broken"):
            with self.subTest(state=state):
                runner.responses[argv] = ProcessResult(
                    0,
                    json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "dispatchId": "dispatch_1",
                                "state": state,
                                "processAction": "none",
                                "alreadySettled": True,
                            },
                        }
                    ),
                    "",
                )
                verdict = client.worker_stop(dispatch_id="dispatch_1", cwd=Path("/tmp"))
                self.assertEqual(
                    verdict,
                    WorkerStopAlreadySettledVerdict(
                        dispatch_id="dispatch_1",
                        state=state,
                        process_action="none",
                    ),
                )

    def test_terminal_close_false_without_mode_is_unconfirmed_process_stop(
        self,
    ) -> None:
        close_argv = (
            ORCA_COMMAND,
            "terminal",
            "close",
            "--terminal",
            "term_main",
            "--json",
        )
        runner = RecordingRunner(
            {
                close_argv: ProcessResult(
                    0,
                    json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "close": {
                                    "handle": "term_main",
                                    "ptyKilled": False,
                                }
                            },
                        }
                    ),
                    "",
                )
            }
        )
        client = OrcaClient(runner=runner)

        verdict = client.terminal_close(terminal_id="term_main", cwd=Path("/tmp"))

        self.assertEqual(verdict.close_mode, None)
        self.assertFalse(verdict.pty_killed)
        self.assertEqual(runner.calls[0][0], close_argv)

    def test_client_decodes_strict_terminal_close_and_switch_verdicts(self) -> None:
        close_argv = (
            ORCA_COMMAND,
            "terminal",
            "close",
            "--terminal",
            "term_main",
            "--json",
        )
        switch_argv = (
            ORCA_COMMAND,
            "terminal",
            "switch",
            "--terminal",
            "term_main",
            "--json",
        )
        runner = RecordingRunner(
            {
                close_argv: ProcessResult(
                    0,
                    json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "close": {
                                    "handle": "term_main",
                                    "ptyKilled": True,
                                }
                            },
                        }
                    ),
                    "",
                ),
                switch_argv: ProcessResult(
                    0,
                    json.dumps(
                        {
                            "ok": True,
                            "result": {
                                "focus": {
                                    "handle": "term_main",
                                    "navigated": True,
                                }
                            },
                        }
                    ),
                    "",
                ),
            }
        )
        client = OrcaClient(runner=runner)

        self.assertEqual(
            client.terminal_close(terminal_id="term_main", cwd=Path("/tmp")),
            TerminalCloseVerdict(handle="term_main", close_mode=None, pty_killed=True),
        )
        self.assertEqual(
            client.terminal_switch(terminal_id="term_main", cwd=Path("/tmp")),
            TerminalSwitchVerdict(handle="term_main", navigated=True),
        )

        runner.responses[close_argv] = ProcessResult(
            0,
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "close": {
                            "handle": "term_main",
                            "closeMode": 1,
                            "ptyKilled": True,
                        }
                    },
                }
            ),
            "",
        )
        with self.assertRaises(OrcaProtocolError):
            client.terminal_close(terminal_id="term_main", cwd=Path("/tmp"))

        runner.responses[close_argv] = ProcessResult(
            0,
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "close": {
                            "handle": "term_main",
                            "ptyKilled": True,
                            "ptyStopVerdict": "live",
                        }
                    },
                }
            ),
            "",
        )
        with self.assertRaises(OrcaProtocolError):
            client.terminal_close(terminal_id="term_main", cwd=Path("/tmp"))

        runner.responses[close_argv] = ProcessResult(
            1,
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "close": {
                            "handle": "term_main",
                            "ptyKilled": False,
                            "ptyStopVerdict": "unverifiable",
                        }
                    },
                }
            ),
            "",
        )
        verdict = client.terminal_close(terminal_id="term_main", cwd=Path("/tmp"))
        self.assertFalse(verdict.pty_killed)

        runner.responses[close_argv] = ProcessResult(
            1,
            json.dumps(
                {
                    "ok": False,
                    "result": {
                        "close": {
                            "handle": "term_main",
                            "ptyKilled": True,
                        }
                    },
                    "error": {"code": "close_failed"},
                }
            ),
            "",
        )
        with self.assertRaises(OrcaCommandError):
            client.terminal_close(terminal_id="term_main", cwd=Path("/tmp"))

        runner.responses[switch_argv] = ProcessResult(
            0,
            json.dumps(
                {
                    "ok": True,
                    "result": {"focus": {"handle": "foreign", "navigated": False}},
                }
            ),
            "",
        )
        with self.assertRaises(OrcaProtocolError):
            client.terminal_switch(terminal_id="term_main", cwd=Path("/tmp"))

    def test_client_normalizes_confirmed_stale_terminal_errors(self) -> None:
        for code in ("terminal_handle_stale", "terminal_gone"):
            with self.subTest(code=code):
                argv = (
                    ORCA_COMMAND,
                    "terminal",
                    "show",
                    "--terminal",
                    "term_stale",
                    "--json",
                )
                runner = RecordingRunner(
                    {
                        argv: ProcessResult(
                            1,
                            json.dumps(
                                {
                                    "ok": False,
                                    "error": {"code": code},
                                }
                            ),
                            "",
                        )
                    }
                )
                client = OrcaClient(runner=runner)

                with self.assertRaises(OrcaCommandError) as raised:
                    client.terminal_show(terminal_id="term_stale", cwd=Path("/tmp"))

                self.assertTrue(raised.exception.not_found)

    def test_client_normalizes_method_specific_absence_codes(self) -> None:
        terminal_show_argv = (
            ORCA_COMMAND,
            "terminal",
            "show",
            "--terminal",
            "term_stale",
            "--json",
        )
        worker_show_argv = (
            ORCA_COMMAND,
            "orchestration",
            "worker-show",
            "--dispatch",
            "dispatch_missing",
            "--json",
        )
        run_show_argv = (
            ORCA_COMMAND,
            "orchestration",
            "run-show",
            "--id",
            "run_missing",
            "--json",
        )
        runner = RecordingRunner(
            {
                terminal_show_argv: ProcessResult(
                    1,
                    json.dumps(
                        {
                            "ok": False,
                            "error": {"code": "terminal_gone"},
                        }
                    ),
                    "",
                ),
                worker_show_argv: ProcessResult(
                    1,
                    json.dumps(
                        {
                            "ok": False,
                            "error": {"code": "dispatch_not_found"},
                        }
                    ),
                    "",
                ),
                run_show_argv: ProcessResult(
                    1,
                    json.dumps(
                        {
                            "ok": False,
                            "error": {"code": "run_not_found"},
                        }
                    ),
                    "",
                ),
            }
        )
        client = OrcaClient(runner=runner)

        with self.assertRaises(OrcaCommandError) as terminal_error:
            client.terminal_show(terminal_id="term_stale", cwd=Path("/tmp"))
        with self.assertRaises(OrcaCommandError) as worker_error:
            client.worker_show(dispatch_id="dispatch_missing", cwd=Path("/tmp"))
        with self.assertRaises(OrcaCommandError) as run_error:
            client.run_show(run_id="run_missing", cwd=Path("/tmp"))

        self.assertTrue(terminal_error.exception.not_found)
        self.assertEqual(terminal_error.exception.absence_code, "terminal_gone")
        self.assertTrue(worker_error.exception.not_found)
        self.assertEqual(worker_error.exception.absence_code, "dispatch_not_found")
        self.assertTrue(run_error.exception.not_found)
        self.assertEqual(run_error.exception.absence_code, "run_not_found")

        runner.responses[run_show_argv] = ProcessResult(
            1,
            json.dumps({"ok": False, "error": {"code": "task_not_found"}}),
            "",
        )
        with self.assertRaises(OrcaCommandError) as unrelated_error:
            client.run_show(run_id="run_missing", cwd=Path("/tmp"))
        self.assertFalse(unrelated_error.exception.not_found)

        argv = (
            ORCA_COMMAND,
            "terminal",
            "show",
            "--terminal",
            "term_stale",
            "--json",
        )
        runner = RecordingRunner(
            {
                argv: ProcessResult(
                    1,
                    json.dumps({"ok": False, "error": {"code": {"unexpected": True}}}),
                    "",
                )
            }
        )
        client = OrcaClient(runner=runner)
        with self.assertRaises(OrcaCommandError) as raised:
            client.terminal_show(terminal_id="term_stale", cwd=Path("/tmp"))
        self.assertFalse(raised.exception.not_found)

        close_argv = (
            ORCA_COMMAND,
            "terminal",
            "close",
            "--terminal",
            "term_stale",
            "--json",
        )
        runner.responses[close_argv] = ProcessResult(
            1,
            json.dumps(
                {
                    "ok": False,
                    "result": {},
                    "error": {"code": "terminal_gone"},
                }
            ),
            "",
        )
        with self.assertRaises(OrcaCommandError) as raised:
            client.terminal_close(terminal_id="term_stale", cwd=Path("/tmp"))
        self.assertTrue(raised.exception.not_found)

        worker_argv = (
            ORCA_COMMAND,
            "orchestration",
            "worker-stop",
            "--dispatch",
            "dispatch_stale",
            "--json",
        )
        runner.responses[worker_argv] = ProcessResult(
            1,
            json.dumps(
                {
                    "ok": False,
                    "result": {},
                    "error": {"code": "terminal_gone"},
                }
            ),
            "",
        )
        with self.assertRaises(OrcaCommandError) as raised:
            client.worker_stop(dispatch_id="dispatch_stale", cwd=Path("/tmp"))
        self.assertTrue(raised.exception.not_found)


class OrcaBackendSafetyTest(unittest.TestCase):
    @staticmethod
    def _metadata(root: Path) -> None:
        (root / "orca-runtime.json").write_text(
            json.dumps(
                {"transports": [{"kind": "unix", "endpoint": str(root / "orca.sock")}]}
            ),
            encoding="utf-8",
        )

    def test_partial_start_closes_only_the_created_main_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            client = FakeOrcaClient()
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure) as raised:
                backend.start(spec)

        self.assertEqual(raised.exception.code, ErrorCode.BACKEND_PROTOCOL_FAILURE)
        self.assertEqual(
            [call[0] for call in client.calls],
            [
                "status",
                "worktree-current",
                "terminal-create",
                "terminal-show",
                "terminal-wait",
                "run-create",
                "terminal-show",
                "terminal-close",
            ],
        )
        self.assertEqual(client.calls[-1], ("terminal-close", "term_main"))

    def test_start_reports_cleanup_failure_without_hiding_original_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            client = FakeOrcaClient()
            client.close_error = OrcaCommandError("close denied")
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure) as raised:
                backend.start(spec)

        self.assertEqual(raised.exception.code, ErrorCode.BACKEND_PROTOCOL_FAILURE)
        self.assertIn("team startup failed", str(raised.exception))
        self.assertIn("Orca command failed", str(raised.exception))
        self.assertIn("Main terminal cleanup failed", str(raised.exception))

    def test_foreign_worktree_is_rejected_before_terminal_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            client = FakeOrcaClient()
            client.worktree_result = {
                "worktree": {"id": "foreign", "path": str(root / "other")}
            }
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure) as raised:
                backend.start(spec)

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertNotIn(
            (
                "terminal-create",
                "foreign",
                "team-project-main",
                "agent-team _role-run main",
            ),
            client.calls,
        )

    def test_foreign_main_terminal_is_not_closed_on_start_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            client = FakeOrcaClient()
            client.run_create_result = "run_1"
            client.terminal_create_result = {
                "terminal": {
                    "handle": "term_main",
                    "worktreeId": "foreign-worktree",
                }
            }
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure) as raised:
                backend.start(spec)

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertNotIn(("terminal-close", "term_main"), client.calls)
        self.assertFalse(spec.state_path.exists())

    def test_dynamic_main_terminal_title_does_not_break_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            client = FakeOrcaClient()
            client.run_create_result = "run_1"
            client.terminal_create_result = {
                "terminal": {
                    "handle": "term_main",
                    "worktreeId": "repo::project",
                    "title": "other-team-main",
                }
            }
            client.terminal_result = {
                "terminal": {
                    "handle": "term_main",
                    "worktreeId": "repo::project",
                    "title": "provider-assigned-title",
                    "worktreePath": "",
                }
            }
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            result = backend.start(spec)
            self.assertTrue(spec.state_path.exists())

        self.assertEqual(result.run_id._value, "run_1")
        self.assertNotIn(("terminal-close", "term_main"), client.calls)

    def test_start_writes_recovery_marker_before_terminal_create(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            cleanup_called = False

            def cleanup() -> None:
                nonlocal cleanup_called
                cleanup_called = True

            client = FakeOrcaClient()
            client.terminal_create_error = OrcaTransportError("create response lost")
            original_create = client.terminal_create
            marker_seen = False

            def create_with_marker(
                *, worktree_id: str, title: str, command: str, cwd: Path
            ) -> dict[str, object]:
                nonlocal marker_seen
                marker_seen = (
                    spec.state_path.parent / ".startup-recovery.json"
                ).is_file()
                return original_create(
                    worktree_id=worktree_id, title=title, command=command, cwd=cwd
                )

            client.terminal_create = create_with_marker
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                prepare_start=lambda: cleanup,
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure):
                backend.start(spec)
            recovery_path = spec.state_path.parent / ".startup-recovery.json"
            recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
            recovery_exists = recovery_path.is_file()
            prior_calls = len(client.calls)
            retry = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                prepare_start=lambda: cleanup,
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure):
                retry.start(spec)

        self.assertTrue(marker_seen)
        self.assertTrue(recovery_exists)
        self.assertIsNone(recovery["main_terminal"])
        self.assertFalse(recovery["terminal_closed"])
        self.assertFalse(cleanup_called)
        self.assertNotIn(
            "terminal-create", [call[0] for call in client.calls[prior_calls:]]
        )

    def test_stop_skips_terminal_close_when_worker_stop_kills_agent_pty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.worker_stop_result = WorkerStopOwnedVerdict(
                dispatch_id="dispatch_worker",
                state="stopped",
                process_action="closed_agent_terminal",
                already_settled=False,
                pty_killed=True,
            )
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            engine.stop()

        self.assertNotIn(("terminal-close", "term_planner"), client.calls)
        self.assertIn(("terminal-close", "term_main"), client.calls)

    def test_stop_closes_context_only_terminal_with_process_stop_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.worker_stop_result = WorkerStopContextOnlyVerdict(
                dispatch_id="dispatch_worker",
                state="stopped",
                process_action="none",
                already_settled=False,
            )
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            engine.stop()

        self.assertIn(("terminal-close", "term_planner"), client.calls)
        self.assertIn(("terminal-close", "term_main"), client.calls)
        self.assertFalse(spec.state_path.parent.exists())

    def test_stop_keeps_state_when_terminal_pty_kill_is_unconfirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.worker_stop_result = WorkerStopContextOnlyVerdict(
                dispatch_id="dispatch_worker",
                state="stopped",
                process_action="none",
                already_settled=False,
            )
            client.close_result = TerminalCloseVerdict(
                handle="term_planner", close_mode=None, pty_killed=False
            )
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure):
                engine.stop()
            journal = json.loads(
                (spec.state_path.parent / ".cleanup.json").read_text(encoding="utf-8")
            )
            state_retained = spec.state_path.exists()

        self.assertTrue(state_retained)
        self.assertEqual(journal["assignments"][0]["remote"], "unknown")
        self.assertNotIn(("terminal-close", "term_main"), client.calls)

    def test_stop_keeps_state_when_main_pty_kill_is_unconfirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.close_result = TerminalCloseVerdict(
                handle="term_main", close_mode=None, pty_killed=False
            )
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure):
                engine.stop()
            journal = json.loads(
                (spec.state_path.parent / ".cleanup.json").read_text(encoding="utf-8")
            )
            state_retained = spec.state_path.exists()

        self.assertTrue(state_retained)
        self.assertEqual(journal["main"], "unknown")

    def test_stop_keeps_local_resources_until_main_pty_kill_is_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            prompt = spec.state_path.parent / "prompt-planner-nonce1234.md"
            prompt.write_text("prompt", encoding="utf-8")
            prompt.chmod(0o600)
            private = Path(tempfile.mkdtemp(prefix="agent-team-provider-"))
            snapshot = Path(tempfile.mkdtemp(prefix="agent-team-snapshot-"))
            self.addCleanup(shutil.rmtree, private, ignore_errors=True)
            self.addCleanup(shutil.rmtree, snapshot, ignore_errors=True)
            state = existing_state(spec)
            planner_spec = cast(dict[str, object], state["role_specs"])["planner"]
            planner_spec.update(
                {"execution": "background", "adapter_id": "copilot-readonly"}
            )
            cast(dict[str, object], state["roles"])["planner"] = {
                "task_id": "task_worker",
                "dispatch_id": "dispatch_worker",
                "terminal_handle": "term_planner",
                "completion_observed": False,
                "launcher_owned_terminal": True,
                "execution": "background",
                "adapter_id": "copilot-readonly",
                "launch_nonce": "nonce1234",
                "prompt_path": str(prompt),
                "provider_private_root": str(private),
                "snapshot_root": str(snapshot),
                "adapter_snapshot": {
                    "adapter_id": "copilot-readonly",
                    "revision": "test",
                    "executable": "/bin/echo",
                    "version": "test",
                    "identity": {
                        "device": 1,
                        "inode": 1,
                        "size": 1,
                        "mtime_ns": 1,
                        "sha256": "test",
                    },
                },
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.close_result = TerminalCloseVerdict(
                handle="term_main", close_mode=None, pty_killed=False
            )
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure):
                engine.stop()
            retained = (prompt.exists(), private.exists(), snapshot.exists())

        self.assertEqual(retained, (True, True, True))

    def test_stop_rejects_unknown_worker_stop_state_without_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.worker_stop_result = cast(WorkerStopVerdict, object())
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure):
                engine.stop()
            state_retained = spec.state_path.exists()

        self.assertTrue(state_retained)
        self.assertNotIn(("terminal-close", "term_planner"), client.calls)

    def test_start_terminal_show_rejects_foreign_worktree_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            client = FakeOrcaClient()
            client.run_create_result = "run_1"
            client.terminal_result = {
                "terminal": {
                    "handle": "term_main",
                    "worktreeId": "repo::project",
                    "title": "provider-assigned-title",
                    "worktreePath": str(root / "foreign"),
                }
            }
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure) as raised:
                backend.start(spec)

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertIn(("terminal-show", "term_main"), client.calls)
        self.assertNotIn(("terminal-close", "term_main"), client.calls)
        self.assertFalse(spec.state_path.exists())

    def test_start_failure_rechecks_main_terminal_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            client = FakeOrcaClient()
            client.run_create_result = OrcaCommandError("run-create")
            original_show = client.terminal_show
            show_calls = 0

            def show_after_failure(*, terminal_id: str, cwd: Path) -> dict[str, object]:
                nonlocal show_calls
                show_calls += 1
                if show_calls == 1:
                    return original_show(terminal_id=terminal_id, cwd=cwd)
                return {
                    "terminal": {
                        "handle": terminal_id,
                        "worktreeId": "repo::project",
                        "title": "provider-assigned-title",
                        "worktreePath": str(root / "foreign"),
                    }
                }

            client.terminal_show = show_after_failure
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure) as raised:
                backend.start(spec)
            recovery = json.loads(
                (spec.state_path.parent / ".startup-recovery.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(raised.exception.code, ErrorCode.BACKEND_PROTOCOL_FAILURE)
        self.assertGreaterEqual(show_calls, 2)
        self.assertNotIn(("terminal-close", "term_main"), client.calls)
        self.assertFalse(recovery["terminal_closed"])

    def test_stop_rechecks_main_terminal_before_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            write_state(spec.state_path, existing_state(spec))
            client = FakeOrcaClient()
            original_show = client.terminal_show
            show_calls = 0

            def show_before_close(*, terminal_id: str, cwd: Path) -> dict[str, object]:
                nonlocal show_calls
                show_calls += 1
                if show_calls == 1:
                    return original_show(terminal_id=terminal_id, cwd=cwd)
                return {
                    "terminal": {
                        "handle": terminal_id,
                        "worktreeId": "repo::project",
                        "title": "provider-assigned-title",
                        "worktreePath": str(root / "foreign"),
                    }
                }

            client.terminal_show = show_before_close
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure) as raised:
                engine.stop()
            state_retained = spec.state_path.exists()

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertGreaterEqual(show_calls, 2)
        self.assertTrue(state_retained)
        self.assertNotIn(("terminal-close", "term_main"), client.calls)

    def test_state_publish_survives_startup_marker_cleanup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            client = FakeOrcaClient()
            client.run_create_result = "run_1"
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            with mock.patch.object(
                backend_module,
                "remove_startup_recovery",
                side_effect=RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "marker cleanup failed",
                ),
            ):
                result = backend.start(spec)
            marker_path = spec.state_path.parent / ".startup-recovery.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            state_exists = spec.state_path.is_file()
            backend.request(Status())
            marker_removed_by_status = not marker_path.exists()

        self.assertEqual(result.run_id._value, "run_1")
        self.assertTrue(state_exists)
        self.assertTrue(marker["state_published"])
        self.assertTrue(marker_removed_by_status)
        self.assertNotIn(("terminal-close", "term_main"), client.calls)

    def test_published_startup_marker_cleanup_retries_before_duplicate_start(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            client = FakeOrcaClient()
            client.run_create_result = "run_1"
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            original_remove = backend_module.remove_startup_recovery
            with mock.patch.object(
                backend_module,
                "remove_startup_recovery",
                side_effect=RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "marker cleanup failed",
                ),
            ):
                backend.start(spec)
            marker_path = spec.state_path.parent / ".startup-recovery.json"
            self.assertTrue(marker_path.is_file())

            with mock.patch.object(
                backend_module,
                "remove_startup_recovery",
                side_effect=original_remove,
            ):
                retry = OrcaBackend(client, user_data_path=root)
                with self.assertRaises(RuntimeFailure) as raised:
                    retry.start(spec)
            marker_removed = not marker_path.exists()

        self.assertEqual(raised.exception.code, ErrorCode.TEAM_ALREADY_RUNNING)
        self.assertTrue(marker_removed)

    def test_pre_publish_marker_cleanup_failure_records_verified_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            client = FakeOrcaClient()
            client.run_create_result = OrcaCommandError("run-create")
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            with (
                mock.patch.object(
                    backend_module,
                    "remove_startup_recovery",
                    side_effect=RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "marker cleanup failed",
                    ),
                ),
                self.assertRaises(RuntimeFailure),
            ):
                backend.start(spec)
            recovery = json.loads(
                (spec.state_path.parent / ".startup-recovery.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertTrue(recovery["terminal_closed"])
        self.assertFalse(spec.state_path.exists())

    def test_state_publish_durability_failure_keeps_published_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            client = FakeOrcaClient()
            client.run_create_result = "run_1"
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            real_fsync = runtime_module.os.fsync
            fsync_calls = 0

            def fail_state_directory_fsync(fd: int) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 6:
                    raise OSError("state directory sync failed")
                real_fsync(fd)

            with (
                mock.patch.object(
                    runtime_module.os, "fsync", fail_state_directory_fsync
                ),
                self.assertRaises(RuntimeFailure),
            ):
                backend.start(spec)
            marker_path = spec.state_path.parent / ".startup-recovery.json"
            marker_exists = marker_path.is_file()
            recovery = (
                json.loads(marker_path.read_text(encoding="utf-8"))
                if marker_exists
                else {}
            )
            state_exists = spec.state_path.exists()

        self.assertGreaterEqual(fsync_calls, 6)
        self.assertTrue(state_exists)
        self.assertTrue(marker_exists)
        self.assertTrue(recovery["state_published"])
        self.assertFalse(recovery["terminal_closed"])
        self.assertNotIn(("terminal-close", "term_main"), client.calls)

    def test_start_close_unknown_preserves_preparation_and_blocks_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            cleanup_calls = 0

            def cleanup() -> None:
                nonlocal cleanup_calls
                cleanup_calls += 1

            client = FakeOrcaClient()
            client.run_create_result = OrcaCommandError("run-create")
            client.close_error = OrcaTransportError("terminal close")
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                prepare_start=lambda: cleanup,
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure):
                backend.start(spec)

            recovery = spec.state_path.parent / ".startup-recovery.json"
            self.assertTrue(recovery.is_file())
            self.assertEqual(cleanup_calls, 0)
            prior_calls = len(client.calls)

            retry = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                prepare_start=lambda: cleanup,
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure) as raised:
                retry.start(spec)

        self.assertEqual(raised.exception.code, ErrorCode.BACKEND_PROTOCOL_FAILURE)
        self.assertEqual(cleanup_calls, 0)
        self.assertGreater(len(client.calls), prior_calls)
        self.assertNotIn(("terminal-close", "term_main"), client.calls[prior_calls:])

    def test_start_recovery_records_prepared_local_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            local_path = spec.state_path.parent / "codex-home"
            cleanup_called = False

            def cleanup() -> None:
                nonlocal cleanup_called
                cleanup_called = True

            prepared = StartupCleanup(((str(local_path), False, "dir"),), cleanup)
            client = FakeOrcaClient()
            client.run_create_result = OrcaCommandError("run-create")
            client.close_error = OrcaTransportError("terminal close")
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                prepare_start=lambda: prepared,
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure):
                backend.start(spec)

            recovery = json.loads(
                (spec.state_path.parent / ".startup-recovery.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertFalse(cleanup_called)
        self.assertEqual(
            recovery["local_tracked"],
            [{"path": str(local_path), "existed": False, "kind": "dir"}],
        )

    def test_startup_recovery_removes_new_symlink_without_following_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_root = root / "state"
            state_root.mkdir(mode=0o700)
            outside = root / "outside-auth.json"
            outside.write_text("keep", encoding="utf-8")
            link = state_root / "auth.json"
            link.symlink_to(outside)
            payload = {
                "version": 1,
                "local_tracked": [
                    {"path": str(link), "existed": False, "kind": "link"}
                ],
            }

            cleanup_module.rollback_startup_recovery(state_root, payload)
            outside_survives = outside.exists()

        self.assertFalse(link.exists() or link.is_symlink())
        self.assertTrue(outside_survives)

    def test_start_local_rollback_failure_keeps_recovery_after_close_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)

            def failed_cleanup() -> None:
                raise RuntimeError("local cleanup failed")

            client = FakeOrcaClient()
            client.run_create_result = OrcaCommandError("run-create")
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                prepare_start=lambda: failed_cleanup,
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure):
                backend.start(spec)

            recovery = json.loads(
                (spec.state_path.parent / ".startup-recovery.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertTrue(recovery["terminal_closed"])

    def test_start_recovery_retains_marker_after_read_only_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            cleanup_calls = 0

            def cleanup() -> None:
                nonlocal cleanup_calls
                cleanup_calls += 1

            client = FakeOrcaClient()
            client.run_create_result = OrcaCommandError("run-create")
            client.close_error = OrcaTransportError("terminal close")
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                prepare_start=lambda: cleanup,
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure):
                backend.start(spec)

            client.close_error = None
            client.run_create_result = "run_1"
            client.terminal_show_error = OrcaCommandError(
                "terminal show", not_found=True
            )
            prior_calls = len(client.calls)
            retry = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                prepare_start=lambda: cleanup,
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure) as raised:
                retry.start(spec)
            marker_exists = (
                spec.state_path.parent / ".startup-recovery.json"
            ).is_file()
            state_exists = spec.state_path.exists()

        self.assertEqual(raised.exception.code, ErrorCode.BACKEND_PROTOCOL_FAILURE)
        self.assertEqual(cleanup_calls, 0)
        self.assertTrue(marker_exists)
        self.assertFalse(state_exists)
        self.assertNotIn(
            "terminal-create", [call[0] for call in client.calls[prior_calls:]]
        )

    def test_switch_protocol_failure_is_not_reported_as_focus_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            spec = StartSpec(
                team_id=spec.team_id,
                workspace=spec.workspace,
                config_path=spec.config_path,
                state_path=spec.state_path,
                role_specs=spec.role_specs,
                attach=True,
            )
            client = FakeOrcaClient()
            client.run_create_result = "run_1"
            client.switch_error = OrcaProtocolError("terminal switch")
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            with self.assertRaises(RuntimeFailure) as raised:
                backend.start(spec)
            state_retained = spec.state_path.exists()

        self.assertEqual(raised.exception.code, ErrorCode.BACKEND_PROTOCOL_FAILURE)
        self.assertIsNone(backend.last_start_response)
        self.assertTrue(state_retained)

    def test_switch_command_failure_keeps_a_bounded_focus_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            self._metadata(root)
            spec = StartSpec(
                team_id=spec.team_id,
                workspace=spec.workspace,
                config_path=spec.config_path,
                state_path=spec.state_path,
                role_specs=spec.role_specs,
                attach=True,
            )
            client = FakeOrcaClient()
            client.run_create_result = "run_1"
            client.switch_error = OrcaCommandError("secret terminal switch")
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                main_command_factory=lambda socket: "agent-team _role-run main",
                user_data_path=root,
            )
            result = backend.start(spec)

        self.assertEqual(result.run_id._value, "run_1")
        self.assertIsNotNone(backend.last_start_response)
        assert backend.last_start_response is not None
        self.assertEqual(
            backend.last_start_response.get("focus_warning"),
            "Orca could not focus Main (command failure)",
        )

    def test_status_rejects_a_foreign_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            write_state(spec.state_path, existing_state(spec))
            client = FakeOrcaClient()
            backend = OrcaBackend(
                client,
                launcher_path=Path("/tmp/agent-team"),
                resume_existing=True,
            )
            engine = WorkflowEngine(backend)
            engine.start(spec)

            client.run_show = lambda *, run_id, cwd: {"run": {"id": "foreign"}}
            with self.assertRaises(RuntimeFailure) as raised:
                engine.request(Status())

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)

    def test_stop_rejects_a_new_launch_generation_before_remote_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.run_show = lambda *, run_id, cwd: {
                "run": {
                    "id": run_id,
                    "objective": (
                        f"team-project: Planner / Worker / Reviewer coordination for {cwd}"
                    ),
                    "coordinator_handle": "term_new",
                }
            }
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            new_state = dict(state)
            new_state["run_id"] = "run_new"
            new_state["main_terminal"] = "term_new"
            write_state(spec.state_path, new_state, require_existing=True)

            with self.assertRaises(RuntimeFailure) as raised:
                engine.stop()
            state_retained = spec.state_path.exists()

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertTrue(state_retained)
        self.assertNotIn(("terminal-close", "term_new"), client.calls)

    def test_attach_refuses_a_foreign_main_terminal_path_before_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            write_state(spec.state_path, existing_state(spec))
            client = FakeOrcaClient()
            client.terminal_result = {
                "terminal": {
                    "handle": "term_main",
                    "worktreeId": "repo::project",
                    "title": "provider-assigned-title",
                    "worktreePath": str(root / "foreign"),
                }
            }
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure) as raised:
                engine.request(Attach(Role.MAIN))

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertNotIn(("terminal-switch", "term_main"), client.calls)

    def test_lifecycle_lock_inode_survives_state_root_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state" / "team" / "state.json"
            reservation = _LifecycleReservation(state_path, create_parent=True)
            reservation.acquire()
            lock_path = reservation._lock_path
            inode = lock_path.stat().st_ino
            reservation.release()

            shutil.rmtree(state_path.parent)
            self.assertTrue(lock_path.is_file())
            state_path.parent.mkdir(mode=0o700)
            replacement = _LifecycleReservation(state_path, create_parent=False)
            replacement.acquire()
            try:
                self.assertEqual(replacement._lock_path.stat().st_ino, inode)
            finally:
                replacement.release()

    def test_runtime_and_mcp_state_save_leave_the_shared_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            state = existing_state(spec)
            write_state(spec.state_path, state)
            lock_path = _LifecycleReservation(
                spec.state_path, create_parent=False
            )._lock_path
            self.assertTrue(lock_path.is_file())

            mcp_module.save_state(spec.state_path, state)
            self.assertTrue(lock_path.is_file())

    def test_cleanup_journal_fsyncs_parent_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / ".cleanup.json"
            payload = {
                "version": 1,
                "team_id": "team-project",
                "run_id": "run_1",
                "worktree_id": "repo::project",
                "main_terminal": "term_main",
                "main": "pending",
                "assignments": [],
            }
            with mock.patch.object(
                cleanup_module.os, "fsync", wraps=cleanup_module.os.fsync
            ) as fsync:
                cleanup_module.write_cleanup_journal(path, payload)

        self.assertGreaterEqual(fsync.call_count, 2)

    def test_runtime_state_fsyncs_parent_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            with mock.patch.object(
                runtime_module.os, "fsync", wraps=runtime_module.os.fsync
            ) as fsync:
                runtime_module.write_state(spec.state_path, existing_state(spec))

        self.assertGreaterEqual(fsync.call_count, 2)

    def test_status_does_not_recreate_a_removed_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            write_state(spec.state_path, existing_state(spec))
            client = FakeOrcaClient()
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)
            lock_path = backend_module._LifecycleReservation(
                spec.state_path, create_parent=False
            )._lock_path
            shutil.rmtree(spec.state_path.parent)

            with self.assertRaises(RuntimeFailure) as raised:
                engine.request(Status())
            self.assertTrue(lock_path.exists())

        self.assertEqual(raised.exception.code, ErrorCode.TEAM_NOT_RUNNING)
        self.assertFalse(spec.state_path.parent.exists())

    def test_stop_reloads_current_state_before_remote_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state, require_existing=True)

            engine.stop()

        self.assertIn(("worker-stop", "dispatch_worker"), client.calls)

    def test_stop_refuses_state_content_change_before_destructive_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            def mutate_state(_: Path) -> None:
                cast(dict[str, object], state["roles"])["worker"]["terminal_handle"] = (
                    "foreign-terminal"
                )
                spec.state_path.write_text(json.dumps(state), encoding="utf-8")
                spec.state_path.chmod(0o600)

            client.run_show_mutator = mutate_state
            with self.assertRaises(RuntimeFailure) as raised:
                engine.stop()

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertNotIn(("worker-stop", "dispatch_worker"), client.calls)
        self.assertNotIn(("terminal-close", "term_planner"), client.calls)

    def test_worker_context_only_worktree_id_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.worker_worktree_id = None
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            engine.stop()

        self.assertFalse(spec.state_path.parent.exists())

    def test_stop_records_unknown_worker_stop_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.worker_stop_result = WorkerStopUnknownVerdict(
                dispatch_id="dispatch_worker",
                state="stop_unknown",
                process_action="unknown",
                already_settled=False,
            )
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure) as raised:
                engine.stop()
            journal = json.loads(
                (spec.state_path.parent / ".cleanup.json").read_text(encoding="utf-8")
            )

        self.assertEqual(raised.exception.code, ErrorCode.BACKEND_PROTOCOL_FAILURE)
        self.assertEqual(journal["assignments"][0]["remote"], "unknown")
        self.assertNotIn(("terminal-close", "term_planner"), client.calls)

    def test_stop_records_unknown_worker_stop_absence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.worker_stop_error = OrcaCommandError(
                "missing dispatch", absence_code="dispatch_not_found"
            )
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure):
                engine.stop()
            journal = json.loads(
                (spec.state_path.parent / ".cleanup.json").read_text(encoding="utf-8")
            )

        self.assertEqual(journal["assignments"][0]["remote"], "unknown")
        self.assertNotIn(("terminal-close", "term_planner"), client.calls)

    def test_stop_records_unknown_main_absence_during_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            write_state(spec.state_path, existing_state(spec))
            client = FakeOrcaClient()
            client.terminal_show_error = OrcaCommandError(
                "missing Main terminal", absence_code="terminal_gone"
            )
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure):
                engine.stop()
            journal = json.loads(
                (spec.state_path.parent / ".cleanup.json").read_text(encoding="utf-8")
            )
            previous_calls = len(client.calls)
            with self.assertRaises(RuntimeFailure):
                engine.stop()
            retry_calls = client.calls[previous_calls:]

        self.assertEqual(journal["main"], "unknown")
        self.assertNotIn(("terminal-close", "term_main"), client.calls)
        self.assertNotIn(("terminal-show", "term_main"), retry_calls)

    def test_stop_records_unknown_run_absence_during_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            write_state(spec.state_path, existing_state(spec))
            client = FakeOrcaClient()
            client.run_show_error = OrcaCommandError(
                "missing Run", absence_code="run_not_found"
            )
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure):
                engine.stop()
            journal = json.loads(
                (spec.state_path.parent / ".cleanup.json").read_text(encoding="utf-8")
            )

        self.assertEqual(journal["main"], "unknown")
        self.assertNotIn(("terminal-close", "term_main"), client.calls)

    def test_stop_records_unknown_role_worker_absence_during_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.worker_show_error = OrcaCommandError(
                "missing Dispatch", absence_code="dispatch_not_found"
            )
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure):
                engine.stop()
            journal = json.loads(
                (spec.state_path.parent / ".cleanup.json").read_text(encoding="utf-8")
            )
            previous_calls = len(client.calls)
            with self.assertRaises(RuntimeFailure):
                engine.stop()
            retry_calls = client.calls[previous_calls:]

        self.assertEqual(journal["assignments"][0]["remote"], "unknown")
        self.assertNotIn(("worker-stop", "dispatch_worker"), client.calls)
        self.assertNotIn(("terminal-close", "term_planner"), client.calls)
        self.assertNotIn(("worker-show", "dispatch_worker"), retry_calls)

    def test_stop_records_unknown_role_terminal_absence_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.worker_stop_result = WorkerStopContextOnlyVerdict(
                dispatch_id="dispatch_worker",
                state="stopped",
                process_action="none",
                already_settled=False,
            )
            original_terminal_show = client.terminal_show

            def role_terminal_absence(
                *, terminal_id: str, cwd: Path
            ) -> dict[str, object]:
                if terminal_id == "term_planner":
                    raise OrcaCommandError(
                        "missing role terminal", absence_code="terminal_gone"
                    )
                return original_terminal_show(terminal_id=terminal_id, cwd=cwd)

            client.terminal_show = role_terminal_absence
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)
            journal = cleanup_module.load_cleanup_journal(state, spec.state_path)
            cleanup_module.journal_assignment(journal, "worker")["remote"] = (
                "worker_done"
            )
            cleanup_module.write_cleanup_journal(
                cleanup_module.cleanup_journal_path(spec.state_path), journal
            )

            with self.assertRaises(RuntimeFailure):
                engine.stop()
            saved = json.loads(
                (spec.state_path.parent / ".cleanup.json").read_text(encoding="utf-8")
            )
            previous_calls = len(client.calls)
            with self.assertRaises(RuntimeFailure):
                engine.stop()
            retry_calls = client.calls[previous_calls:]

        self.assertEqual(saved["assignments"][0]["remote"], "unknown")
        self.assertNotIn(("terminal-close", "term_planner"), client.calls)
        self.assertNotIn(("terminal-show", "term_planner"), retry_calls)

    def test_stop_records_unknown_role_terminal_close_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.worker_stop_result = WorkerStopContextOnlyVerdict(
                dispatch_id="dispatch_worker",
                state="stopped",
                process_action="none",
                already_settled=False,
            )
            client.close_error = OrcaTransportError("terminal close")
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure):
                engine.stop()
            journal = json.loads(
                (spec.state_path.parent / ".cleanup.json").read_text(encoding="utf-8")
            )

        self.assertEqual(journal["assignments"][0]["remote"], "unknown")

    def test_stop_treats_confirmed_stale_terminal_as_non_retryable_unknown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.worker_stop_result = WorkerStopContextOnlyVerdict(
                dispatch_id="dispatch_worker",
                state="stopped",
                process_action="none",
                already_settled=False,
            )
            client.close_error = OrcaCommandError("stale terminal", not_found=True)
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure):
                engine.stop()
            journal = json.loads(
                (spec.state_path.parent / ".cleanup.json").read_text(encoding="utf-8")
            )
            state_retained = spec.state_path.exists()

        self.assertTrue(state_retained)
        self.assertEqual(journal["assignments"][0]["remote"], "unknown")

    def test_lifecycle_reservation_allows_only_one_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state" / "team" / "state.json"
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_reservation_probe,
                    args=(str(path), barrier, result_queue),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            results = [result_queue.get(timeout=5) for _ in processes]
            for process in processes:
                process.join(timeout=5)

        self.assertEqual(sorted(results), ["TeamAlreadyRunning", "acquired"])
        self.assertTrue(all(process.exitcode == 0 for process in processes))

    def test_stop_refuses_foreign_dispatch_before_any_destructive_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "foreign-task",
                    "dispatch_id": "foreign-dispatch",
                    "terminal_handle": "foreign-terminal",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure) as raised:
                engine.stop()

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertNotIn(("worker-stop", "foreign-dispatch"), client.calls)
        self.assertNotIn(("terminal-close", "foreign-terminal"), client.calls)

    def test_stop_refuses_foreign_terminal_path_before_any_destructive_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.terminal_result = {
                "terminal": {
                    "handle": "term_planner",
                    "worktreeId": "repo::project",
                    "title": "provider-assigned-title",
                    "worktreePath": str(root / "foreign"),
                }
            }
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure) as raised:
                engine.stop()

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertNotIn(("worker-stop", "dispatch_worker"), client.calls)
        self.assertNotIn(("terminal-close", "term_planner"), client.calls)

    def test_stop_rechecks_dispatch_before_closing_after_worker_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)
            journal = cleanup_module.load_cleanup_journal(state, spec.state_path)
            cleanup_module.journal_assignment(journal, "worker")["remote"] = (
                "worker_done"
            )
            cleanup_module.write_cleanup_journal(
                cleanup_module.cleanup_journal_path(spec.state_path), journal
            )

            def foreign_worker_show(
                *, dispatch_id: str, cwd: Path
            ) -> dict[str, object]:
                del dispatch_id
                del cwd
                return {
                    "dispatch": {
                        "id": "foreign-dispatch",
                        "task_id": "foreign-task",
                        "run_id": "foreign-run",
                        "assignee_handle": "foreign-terminal",
                    },
                    "worker": {
                        "dispatch_id": "foreign-dispatch",
                        "worktree_id": "repo::project",
                        "agent_terminal_handle": "foreign-terminal",
                    },
                }

            client.worker_show = foreign_worker_show
            with self.assertRaises(RuntimeFailure) as raised:
                engine.stop()

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertNotIn(("terminal-close", "term_planner"), client.calls)

    def test_stop_rechecks_dispatch_after_worker_stop_before_first_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            state = existing_state(spec)
            state["roles"] = {
                "worker": {
                    "task_id": "task_worker",
                    "dispatch_id": "dispatch_worker",
                    "terminal_handle": "term_planner",
                    "completion_observed": False,
                    "launcher_owned_terminal": True,
                }
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            client.worker_stop_result = WorkerStopContextOnlyVerdict(
                dispatch_id="dispatch_worker",
                state="stopped",
                process_action="none",
                already_settled=False,
            )
            original_worker_show = client.worker_show
            worker_show_calls = 0

            def worker_show_after_stop(
                *, dispatch_id: str, cwd: Path
            ) -> dict[str, object]:
                nonlocal worker_show_calls
                worker_show_calls += 1
                if worker_show_calls == 1:
                    return original_worker_show(dispatch_id=dispatch_id, cwd=cwd)
                return {
                    "dispatch": {
                        "id": "foreign-dispatch",
                        "task_id": "foreign-task",
                        "run_id": "foreign-run",
                        "assignee_handle": "foreign-terminal",
                    },
                    "worker": {
                        "dispatch_id": "foreign-dispatch",
                        "worktree_id": "repo::project",
                        "agent_terminal_handle": "foreign-terminal",
                    },
                }

            client.worker_show = worker_show_after_stop
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            with self.assertRaises(RuntimeFailure) as raised:
                engine.stop()

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertGreaterEqual(worker_show_calls, 2)
        self.assertNotIn(("terminal-close", "term_planner"), client.calls)

    def test_stop_cleanup_failure_keeps_journal_and_retries_local_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            prompt = root / "state" / "team-project" / "prompt-planner-nonce1234.md"
            prompt.write_text("prompt", encoding="utf-8")
            prompt.chmod(0o600)
            private = Path(tempfile.mkdtemp(prefix="agent-team-provider-"))
            snapshot = Path(tempfile.mkdtemp(prefix="agent-team-snapshot-"))
            state = existing_state(spec)
            worker_spec = cast(dict[str, object], state["role_specs"])["planner"]
            worker_spec.update(
                {
                    "execution": "background",
                    "adapter_id": "copilot-readonly",
                }
            )
            cast(dict[str, object], state["roles"])["planner"] = {
                "task_id": "task_worker",
                "dispatch_id": "dispatch_worker",
                "terminal_handle": "term_planner",
                "completion_observed": False,
                "launcher_owned_terminal": True,
                "execution": "background",
                "adapter_id": "copilot-readonly",
                "launch_nonce": "nonce1234",
                "prompt_path": str(prompt),
                "provider_private_root": str(private),
                "snapshot_root": str(snapshot),
                "adapter_snapshot": {
                    "adapter_id": "copilot-readonly",
                    "revision": "test",
                    "executable": "/bin/echo",
                    "version": "test",
                    "identity": {
                        "device": 1,
                        "inode": 1,
                        "size": 1,
                        "mtime_ns": 1,
                        "sha256": "test",
                    },
                },
            }
            write_state(spec.state_path, state)
            client = FakeOrcaClient()
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            real_remove_owned_tree = backend_module.remove_owned_tree
            cleanup_calls = 0

            def fail_once(path: Path) -> None:
                nonlocal cleanup_calls
                cleanup_calls += 1
                if cleanup_calls == 1:
                    raise RuntimeError("private cleanup failed")
                real_remove_owned_tree(path)

            with mock.patch.object(backend_module, "remove_owned_tree", fail_once):
                with self.assertRaises(RuntimeFailure):
                    engine.stop()
                journal = spec.state_path.parent / ".cleanup.json"
                self.assertTrue(journal.is_file())
                self.assertTrue(prompt.is_file())
                engine.stop()

            self.assertFalse(spec.state_path.parent.exists())
            self.assertFalse(private.exists())
            self.assertFalse(snapshot.exists())
            self.assertNotIn(("worker-stop", "dispatch_worker"), client.calls[8:])

    def test_stop_treats_missing_background_roots_as_already_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            spec = start_spec(root)
            spec.workspace.mkdir()
            spec.state_path.parent.mkdir(mode=0o700, parents=True)
            prompt = root / "state" / "team-project" / "prompt-planner-nonce1234.md"
            prompt.write_text("prompt", encoding="utf-8")
            prompt.chmod(0o600)
            private = Path(tempfile.mkdtemp(prefix="agent-team-provider-"))
            snapshot = Path(tempfile.mkdtemp(prefix="agent-team-snapshot-"))
            state = existing_state(spec)
            worker_spec = cast(dict[str, object], state["role_specs"])["planner"]
            worker_spec.update(
                {"execution": "background", "adapter_id": "copilot-readonly"}
            )
            cast(dict[str, object], state["roles"])["planner"] = {
                "task_id": "task_worker",
                "dispatch_id": "dispatch_worker",
                "terminal_handle": "term_planner",
                "completion_observed": False,
                "launcher_owned_terminal": True,
                "execution": "background",
                "adapter_id": "copilot-readonly",
                "launch_nonce": "nonce1234",
                "prompt_path": str(prompt),
                "provider_private_root": str(private),
                "snapshot_root": str(snapshot),
                "adapter_snapshot": {
                    "adapter_id": "copilot-readonly",
                    "revision": "test",
                    "executable": "/bin/echo",
                    "version": "test",
                    "identity": {
                        "device": 1,
                        "inode": 1,
                        "size": 1,
                        "mtime_ns": 1,
                        "sha256": "test",
                    },
                },
            }
            write_state(spec.state_path, state)
            private.rmdir()
            prompt.unlink()
            client = FakeOrcaClient()
            backend = OrcaBackend(client, resume_existing=True)
            engine = WorkflowEngine(backend)
            engine.start(spec)

            cleanup_order: list[str] = []
            real_remove_owned_tree = backend_module.remove_owned_tree

            def record_cleanup(path: Path) -> None:
                cleanup_order.append(path.name)
                real_remove_owned_tree(path)

            with mock.patch.object(backend_module, "remove_owned_tree", record_cleanup):
                engine.stop()

        self.assertFalse(spec.state_path.parent.exists())
        self.assertFalse(snapshot.exists())
        self.assertFalse(prompt.exists())
        self.assertEqual(cleanup_order, [snapshot.name, private.name])

    def test_cli_runtime_failure_keeps_legacy_stderr_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    cli_module,
                    "start_team",
                    side_effect=RuntimeFailure(
                        ErrorCode.IDENTITY_MISMATCH, "agent-team state is foreign"
                    ),
                ),
                contextlib.redirect_stderr(stderr),
            ):
                result = cli_module.main(
                    [
                        "start",
                        "--config",
                        str(cli_module.default_config_path()),
                        "--cwd",
                        temp_dir,
                        "--no-attach",
                    ]
                )

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), "ERROR: agent-team state is foreign\n")

    def test_cli_runtime_failure_output_is_bounded_and_control_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    cli_module,
                    "start_team",
                    side_effect=RuntimeFailure(
                        ErrorCode.BACKEND_PROTOCOL_FAILURE,
                        "external\x1b[31mline\n\t\x7f\x85" + "x" * 1_000,
                    ),
                ),
                contextlib.redirect_stderr(stderr),
            ):
                result = cli_module.main(
                    [
                        "start",
                        "--config",
                        str(cli_module.default_config_path()),
                        "--cwd",
                        temp_dir,
                        "--no-attach",
                    ]
                )

        message = stderr.getvalue()
        self.assertEqual(result, 1)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\t", message)
        self.assertNotIn("\x7f", message)
        self.assertNotIn("\x85", message)
        self.assertEqual(message.count("\n"), 1)
        self.assertLessEqual(len(message), 248)

    def test_cli_rejects_windows_before_orca_prerequisites(self) -> None:
        with (
            mock.patch.object(cli_module.sys, "platform", "win32"),
            self.assertRaises(RuntimeFailure) as raised,
        ):
            cli_module.start_team({}, attach=False)

        self.assertEqual(raised.exception.code, ErrorCode.INVALID_REQUEST)

    def test_protocol_error_is_not_a_focus_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = mock.Mock(spec=ProcessRunner)
            runner.run.return_value = ProcessResult(
                0, '{"ok": false, "error": {"message": "secret"}}', ""
            )
            client = OrcaClient(runner=runner)
            with self.assertRaises(OrcaProtocolError):
                client.terminal_switch(terminal_id="term_main", cwd=root)

    def test_external_orca_diagnostics_are_redacted_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = mock.Mock(spec=ProcessRunner)
            runner.run.return_value = ProcessResult(
                1,
                json.dumps(
                    {
                        "ok": False,
                        "error": {"code": "secret", "message": "secret"},
                    }
                ),
                "stderr-secret\n/path/private\n\x1b[31m",
            )
            client = OrcaClient(runner=runner)
            with self.assertRaises(OrcaCommandError) as raised:
                client.terminal_switch(terminal_id="term_private", cwd=root)

        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("term_private", str(raised.exception))
        self.assertNotIn("/path/private", str(raised.exception))
        self.assertLessEqual(len(str(raised.exception)), 256)

    def test_transport_failure_is_distinct_from_command_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = mock.Mock(spec=ProcessRunner)
            runner.run.side_effect = TimeoutError("secret timeout")
            client = OrcaClient(runner=runner)
            with self.assertRaises(OrcaTransportError):
                client.terminal_switch(terminal_id="term_main", cwd=Path(temp_dir))

    def test_prepare_failure_rolls_back_new_codex_homes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "state" / "codex" / "worker"
            plan = {
                "roles": {
                    "worker": {
                        "provider": "codex",
                        "env": {"CODEX_HOME": str(home)},
                    }
                }
            }

            def prepare(_: dict[str, object]) -> None:
                home.mkdir(parents=True)
                (home / "auth.json").symlink_to("/tmp/auth.json")
                raise RuntimeError("preparation failed")

            with (
                mock.patch.object(cli_module, "prepare_codex_homes", prepare),
                self.assertRaises(RuntimeError),
            ):
                cli_module.prepare_codex_homes_with_rollback(plan)

            self.assertFalse(home.exists())


class ProcessRunnerPortabilityTest(unittest.TestCase):
    def test_invalid_input_encoding_is_rejected_before_child_creation(self) -> None:
        runner = ProcessRunner()
        with (
            mock.patch.object(adapters_module.subprocess, "Popen") as popen,
            self.assertRaisesRegex(ExecutionError, "UTF-8"),
        ):
            runner.run(
                ("/bin/echo", "ok"),
                cwd=Path("/tmp"),
                env={},
                input_text="\ud800",
                timeout_seconds=2,
            )

        popen.assert_not_called()

    def test_windows_runner_fails_before_starting_a_child(self) -> None:
        runner = ProcessRunner()
        cwd = Path("/tmp")
        with (
            mock.patch.object(adapters_module.os, "name", "nt"),
            self.assertRaisesRegex(Exception, "POSIX runtime"),
        ):
            runner.run(
                ("/bin/echo", "ok"),
                cwd=cwd,
                env={},
                timeout_seconds=2,
            )

    def test_nonfinite_timeout_is_rejected_before_starting_a_child(self) -> None:
        runner = ProcessRunner()
        for timeout in (float("nan"), float("inf"), float("-inf")):
            with (
                self.subTest(timeout=timeout),
                self.assertRaisesRegex(Exception, "finite"),
            ):
                runner.run(
                    ("echo", "ok"),
                    cwd=Path("/tmp"),
                    env={},
                    timeout_seconds=timeout,
                )

    def test_missing_poll_selector_uses_default_selector(self) -> None:
        runner = ProcessRunner()
        with mock.patch.object(selectors, "PollSelector", None):
            result = runner.run(
                ("/bin/echo", "ok"), cwd=Path("/tmp"), env={}, timeout_seconds=2
            )
        self.assertEqual(result.stdout, "ok\n")

    @unittest.skipIf(os.name == "nt", "POSIX process-group contract")
    def test_normal_exit_reaps_stdio_closed_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "child-marker"
            child = (
                "import pathlib,time; time.sleep(0.6); "
                f"pathlib.Path({str(marker)!r}).write_text('residual')"
            )
            parent = (
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable, '-c', "
                f"{child!r}], stdin=subprocess.DEVNULL, "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
            )
            runner = ProcessRunner()

            result = runner.run(
                (sys.executable, "-c", parent),
                cwd=Path(temp_dir),
                env=os.environ.copy(),
                timeout_seconds=2,
            )
            time.sleep(0.8)
            marker_exists = marker.exists()

        self.assertEqual(result.returncode, 0)
        self.assertFalse(marker_exists)

    @unittest.skipIf(os.name == "nt", "POSIX process-group contract")
    def test_parent_exit_does_not_wait_for_pipe_holding_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "child-marker"
            child = (
                "import pathlib,time; time.sleep(1.5); "
                f"pathlib.Path({str(marker)!r}).write_text('residual')"
            )
            parent = (
                "import subprocess,sys; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}])"
            )
            runner = ProcessRunner()
            started = time.monotonic()

            result = runner.run(
                (sys.executable, "-c", parent),
                cwd=Path(temp_dir),
                env=os.environ.copy(),
                timeout_seconds=2,
            )
            elapsed = time.monotonic() - started
            time.sleep(0.2)
            marker_exists = marker.exists()

        self.assertEqual(result.returncode, 0)
        self.assertLess(elapsed, 1.0)
        self.assertFalse(marker_exists)

    @unittest.skipIf(os.name == "nt", "POSIX process-group contract")
    def test_parent_final_output_is_drained_before_group_check(self) -> None:
        runner = ProcessRunner()

        result = runner.run(
            (
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('parent-final'); sys.stdout.flush()",
            ),
            cwd=Path("/tmp"),
            env=os.environ.copy(),
            timeout_seconds=2,
        )

        self.assertEqual(result.stdout, "parent-final")

    @unittest.skipIf(os.name == "nt", "POSIX process-group contract")
    def test_broken_pipe_returns_child_result_and_closes_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            child = (
                "import os; os.close(0); os.write(2, b'child stderr\\n'); os._exit(7)"
            )
            runner = ProcessRunner()

            result = runner.run(
                (sys.executable, "-c", child),
                cwd=Path(temp_dir),
                env=os.environ.copy(),
                input_text="x" * 1_000_000,
                timeout_seconds=2,
            )

        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stderr, "child stderr\n")

    def test_selector_setup_failure_closes_all_captured_streams(self) -> None:
        class FailingSelector:
            instances: ClassVar[list[FailingSelector]] = []

            def __init__(self) -> None:
                self.closed = False
                self.instances.append(self)

            def register(self, *_args: object, **_kwargs: object) -> None:
                raise OSError("selector registration failed")

            def close(self) -> None:
                self.closed = True

        stdin_read, stdin_write = os.pipe()
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        process = mock.Mock()
        process.stdin = os.fdopen(stdin_write, "wb")
        process.stdout = os.fdopen(stdout_read, "rb")
        process.stderr = os.fdopen(stderr_read, "rb")
        process.poll.return_value = 0
        process.wait.return_value = 0
        try:
            with (
                mock.patch.object(selectors, "PollSelector", FailingSelector),
                self.assertRaises(OSError),
            ):
                _bounded_communicate(
                    process,
                    b"input",
                    timeout_seconds=1,
                    max_output_bytes=100,
                )
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
            self.assertTrue(FailingSelector.instances[-1].closed)
        finally:
            os.close(stdin_read)
            os.close(stdout_write)
            os.close(stderr_write)

    @unittest.skipIf(os.name == "nt", "POSIX process-group contract")
    def test_timeout_terminates_descendant_after_parent_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "child-marker"
            child = (
                "import pathlib,time; time.sleep(0.8); "
                f"pathlib.Path({str(marker)!r}).write_text('residual')"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
            )
            runner = ProcessRunner()
            with self.assertRaisesRegex(Exception, "timed out"):
                runner.run(
                    (sys.executable, "-c", parent),
                    cwd=Path(temp_dir),
                    env=os.environ.copy(),
                    timeout_seconds=0.1,
                )
            time.sleep(1)
            self.assertFalse(marker.exists())

    @unittest.skipIf(os.name == "nt", "POSIX process-group contract")
    def test_timeout_kills_sigterm_ignoring_descendant_after_parent_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "child-marker"
            child = (
                "import pathlib,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(2.5); "
                f"pathlib.Path({str(marker)!r}).write_text('residual')"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
            )
            runner = ProcessRunner()
            with self.assertRaisesRegex(Exception, "timed out"):
                runner.run(
                    (sys.executable, "-c", parent),
                    cwd=Path(temp_dir),
                    env=os.environ.copy(),
                    timeout_seconds=0.1,
                )
            time.sleep(3)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
