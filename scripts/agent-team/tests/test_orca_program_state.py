from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Literal, cast

from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.orca_controller import controller_key, controller_terminal, is_program
from agent_team.orca_dispatch import _marker
from agent_team.runtime import (
    RuntimeValidationError,
    _validate_orca_effect,
    read_state,
    validate_state_object,
    write_state,
)
from agent_team.scoped_acp import native_profile
from agent_team.task_spec import TaskSpec


def _task() -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": "task-1",
            "objective": "implement the task",
            "acceptance_criteria": ["the task is complete"],
            "allowed_paths": ["src"],
            "forbidden_paths": [],
            "dependencies": [],
            "verification": [
                {"name": "check", "argv": ["/usr/bin/true"], "timeout_seconds": 1}
            ],
            "evidence_requirements": ["result"],
            "consultation_conditions": [],
        }
    )


def _graph(dispatch_mode: Literal["serial", "parallel"]) -> GraphSpec:
    return GraphSpec(
        nodes=(NodeRef("worker", Role.WORKER), NodeRef("reviewer", Role.REVIEWER)),
        edges=(GraphEdge("worker", "reviewer", "reviewed-by"),),
        coordination=Coordination("program", ("worker",), dispatch_mode, 1),
        routes=(TaskRoute("task-1", None, None, "worker", "reviewer"),),
    )


def _role_spec(kind: str) -> dict[str, object]:
    return {
        "kind": kind,
        **native_profile("claude", kind),
        "model": "fable",
        "effort": "high",
        "instructions": kind,
    }


def _argv(state: dict[str, object], nonce: str) -> list[str]:
    return [
        "/opt/venv/bin/python",
        "-P",
        "-m",
        "agent_team",
        "_orca-program-run",
        "--state",
        str(state["state_path"]),
        "--run-id",
        str(state["run_id"]),
        "--launch-nonce",
        nonce,
    ]


def _pending(
    state: dict[str, object], argv: list[str], nonce: str
) -> dict[str, object]:
    digest = hashlib.sha256(
        json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "phase": "prepared",
        "run_id": state["run_id"],
        "coordinator_terminal": state["coordinator_terminal"],
        "argv_sha256": digest,
        "launch_nonce": nonce,
    }


def _state(
    root: Path, *, version: int = 4, process: object = None
) -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir()
    task = _task()
    graph = _graph("serial" if version == 4 else "parallel")
    state: dict[str, object] = {
        "version": version,
        "runtime": "orca",
        "team_id": "program-team",
        "workspace": str(workspace),
        "config_path": str(root / "config.toml"),
        "state_path": str(root / "state.json"),
        "launcher_path": str(root / "agent-team"),
        "worktree_id": "repo::workspace",
        "orca_socket": str(root / "orca.sock"),
        "run_id": "run-1",
        "coordinator_terminal": "coordinator-terminal",
        "graph": graph.as_dict(),
        "task_specs": [task.as_dict()],
        "max_review_rounds": 2,
        "tasks": {},
        "program_wave": {"task_ids": ["task-1"], "phase": "writers", "revision": None},
        "role_specs": {
            "worker": _role_spec("worker"),
            "reviewer": _role_spec("reviewer"),
        },
        "roles": {},
    }
    nonce = "nonce1234"
    argv = _argv(state, nonce)
    state["coordinator_argv"] = argv
    state["coordinator_process"] = process
    if process is None:
        state["pending_coordinator_start"] = _pending(state, argv, nonce)
    return state


def _running_process(state: dict[str, object]) -> dict[str, object]:
    argv = list(cast(list[str], state["coordinator_argv"]))
    return {
        "pid": 42,
        "process_group_id": 43,
        "launch_nonce": "nonce1234",
        "argv": argv,
        "phase": "running",
        "exit_code": None,
        "cli_cleanup_confirmed": None,
    }


def _process_with_cli_cleanup(
    state: dict[str, object], *, phase: str, exit_code: object, cleanup: object
) -> dict[str, object]:
    return {
        **_running_process(state),
        "phase": phase,
        "exit_code": exit_code,
        "cli_cleanup_confirmed": cleanup,
    }


class OrcaProgramStateTest(unittest.TestCase):
    def test_controller_selection_keeps_agent_and_program_identities_separate(
        self,
    ) -> None:
        agent = {"version": 3, "runtime": "orca", "main_terminal": "main"}
        self.assertEqual(controller_key(agent), "main_terminal")
        self.assertEqual(controller_terminal(agent), "main")
        self.assertFalse(is_program(agent))

        with tempfile.TemporaryDirectory() as directory:
            state = _state(Path(directory))
            self.assertEqual(controller_key(state), "coordinator_terminal")
            self.assertEqual(controller_terminal(state), "coordinator-terminal")
            self.assertTrue(is_program(state))
            self.assertEqual(
                controller_key(
                    {**state, "version": 5, "graph": _graph("parallel").as_dict()}
                ),
                "coordinator_terminal",
            )

    def test_program_state_with_pending_coordinator_is_valid(self) -> None:
        for version in (4, 5):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as directory,
            ):
                state_path = Path(directory) / "state.json"
                state = _state(Path(directory), version=version)
                write_state(state_path, state)
                self.assertEqual(read_state(state_path), state)

    def test_program_state_with_running_coordinator_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            state["coordinator_process"] = _running_process(state)
            state.pop("pending_coordinator_start")
            validate_state_object(Path(str(state["state_path"])), state)

    def test_program_process_receipt_accepts_running_and_exited_cli_cleanup_states(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            running = _state(root)
            running["coordinator_process"] = _process_with_cli_cleanup(
                running, phase="running", exit_code=None, cleanup=None
            )
            running.pop("pending_coordinator_start")
            validate_state_object(Path(str(running["state_path"])), running)

        with tempfile.TemporaryDirectory() as directory:
            exited = _state(Path(directory))
            exited["coordinator_process"] = _process_with_cli_cleanup(
                exited, phase="exited", exit_code=0, cleanup=False
            )
            exited.pop("pending_coordinator_start")
            validate_state_object(Path(str(exited["state_path"])), exited)

            confirmed = copy.deepcopy(exited)
            confirmed["coordinator_process"] = _process_with_cli_cleanup(
                confirmed, phase="exited", exit_code=0, cleanup=True
            )
            validate_state_object(Path(str(confirmed["state_path"])), confirmed)

    def test_program_process_receipt_rejects_invalid_cli_cleanup_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = _state(root)
            cases: list[dict[str, object]] = []

            missing = copy.deepcopy(base)
            missing["coordinator_process"] = _running_process(missing)
            cast(dict[str, object], missing["coordinator_process"]).pop(
                "cli_cleanup_confirmed", None
            )
            missing.pop("pending_coordinator_start")
            cases.append(missing)

            alias = copy.deepcopy(base)
            alias["coordinator_process"] = {
                **_process_with_cli_cleanup(
                    alias, phase="exited", exit_code=0, cleanup=True
                ),
                "cleanup_confirmed": True,
            }
            alias.pop("pending_coordinator_start")
            cases.append(alias)

            running_false = copy.deepcopy(base)
            running_false["coordinator_process"] = _process_with_cli_cleanup(
                running_false, phase="running", exit_code=None, cleanup=False
            )
            running_false.pop("pending_coordinator_start")
            cases.append(running_false)

            exited_none = copy.deepcopy(base)
            exited_none["coordinator_process"] = _process_with_cli_cleanup(
                exited_none, phase="exited", exit_code=0, cleanup=None
            )
            exited_none.pop("pending_coordinator_start")
            cases.append(exited_none)

            exited_wrong_type = copy.deepcopy(base)
            exited_wrong_type["coordinator_process"] = _process_with_cli_cleanup(
                exited_wrong_type, phase="exited", exit_code=0, cleanup="yes"
            )
            exited_wrong_type.pop("pending_coordinator_start")
            cases.append(exited_wrong_type)

            for damaged in cases:
                with (
                    self.subTest(damaged=damaged),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(Path(str(base["state_path"])), damaged)

    def test_program_state_accepts_existing_background_role_start_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            state["pending_role_start"] = _marker(
                Path(str(state["state_path"])),
                NodeRef("worker", Role.WORKER),
                "nonce1234",
                prompt_path=root / "prompt-worker-nonce1234.md",
                private_root=root / "private-worker",
                snapshot_root=root / "snapshot-worker",
                phase="terminal-create",
                cleanup_confirmed=False,
                task_id="remote-task",
                terminal_handle="terminal-worker",
                dispatch_id="dispatch-worker",
            )
            write_state(Path(str(state["state_path"])), state)
            self.assertEqual(read_state(Path(str(state["state_path"]))), state)

    def test_program_pending_effect_binds_to_coordinator_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = _state(Path(directory))
            state.update(
                {
                    "pending_delivery_id": "delivery-1",
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "released",
                    "pending_question_ids": [],
                    "replied_question_ids": [],
                    "pending_orca_effect": {
                        "operation": "ack",
                        "run_id": state["run_id"],
                        "coordinator_terminal": state["coordinator_terminal"],
                        "delivery_id": "delivery-1",
                        "message_id": None,
                        "body_sha256": None,
                    },
                }
            )
            _validate_orca_effect(state)
            old_alias = copy.deepcopy(state)
            effect = cast(dict[str, object], old_alias["pending_orca_effect"])
            effect.pop("coordinator_terminal")
            effect["main_terminal"] = "main"
            with self.assertRaises(RuntimeValidationError):
                _validate_orca_effect(old_alias)

    def test_program_controller_rejects_wrong_mode_and_mixed_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            missing_marker = copy.deepcopy(state)
            missing_marker.pop("pending_coordinator_start")
            cases = (
                {**state, "main_terminal": "main"},
                {**state, "version": 5},
                {**state, "graph": _graph("serial").as_dict(), "version": 5},
                {**state, "native": {}},
                missing_marker,
            )
            for damaged in cases:
                with (
                    self.subTest(damaged=damaged),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(Path(str(state["state_path"])), damaged)

    def test_program_controller_rejects_nonce_argv_phase_and_process_errors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = _state(root)
            cases: list[dict[str, object]] = []

            bad_nonce = copy.deepcopy(base)
            bad_nonce["coordinator_argv"] = _argv(bad_nonce, "bad nonce!")
            cases.append(bad_nonce)

            bad_pending = copy.deepcopy(base)
            pending = dict(
                cast(dict[str, object], bad_pending["pending_coordinator_start"])
            )
            pending["launch_nonce"] = "different1"
            bad_pending["pending_coordinator_start"] = pending
            cases.append(bad_pending)

            bad_pending_phase = copy.deepcopy(base)
            pending_phase = cast(
                dict[str, object], bad_pending_phase["pending_coordinator_start"]
            )
            pending_phase["phase"] = "unknown"
            cases.append(bad_pending_phase)

            bad_process = copy.deepcopy(base)
            bad_process["coordinator_process"] = {
                **_running_process(bad_process),
                "phase": "running",
                "exit_code": 1,
                "launch_nonce": "different1",
            }
            cases.append(bad_process)

            bad_process_phase = copy.deepcopy(base)
            bad_process_phase["coordinator_process"] = {
                **_running_process(bad_process_phase),
                "phase": "starting",
            }
            cases.append(bad_process_phase)

            bad_process_argv = copy.deepcopy(base)
            process_argv = list(
                cast(list[str], _running_process(bad_process_argv)["argv"])
            )
            process_argv[1] = "-I"
            bad_process_argv["coordinator_process"] = {
                **_running_process(bad_process_argv),
                "argv": process_argv,
            }
            cases.append(bad_process_argv)

            bad_process_pid = copy.deepcopy(base)
            bad_process_pid["coordinator_process"] = {
                **_running_process(bad_process_pid),
                "pid": True,
            }
            cases.append(bad_process_pid)

            bad_process_exit = copy.deepcopy(base)
            bad_process_exit["coordinator_process"] = {
                **_running_process(bad_process_exit),
                "phase": "exited",
                "exit_code": True,
            }
            cases.append(bad_process_exit)

            for damaged in cases:
                with (
                    self.subTest(damaged=damaged),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(Path(str(base["state_path"])), damaged)

    def test_existing_orca_agent_and_native_program_gates_remain_strict(self) -> None:
        with tempfile.TemporaryDirectory():
            agent = {
                "version": 4,
                "runtime": "orca",
                "graph": {
                    "nodes": [{"node_id": "main", "kind": "main"}],
                    "edges": [],
                    "coordination": {
                        "mode": "agent",
                        "entry_nodes": ["main"],
                        "dispatch_mode": "serial",
                        "max_active": 1,
                    },
                    "routes": [],
                },
                "main_terminal": "main",
            }
            with self.assertRaises(RuntimeValidationError):
                controller_key({**agent, "coordinator_terminal": "alias"})


if __name__ == "__main__":
    unittest.main()
