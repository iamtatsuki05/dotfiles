from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_team.native_controller import ControllerKeys, controller_keys
from agent_team.runtime import (
    RuntimeValidationError,
    validate_native_controller_process,
    validate_state_object,
)


def _program_graph() -> dict[str, object]:
    return {
        "nodes": [{"node_id": "worker", "kind": "worker"}],
        "edges": [],
        "coordination": {
            "mode": "program",
            "entry_nodes": ["worker"],
            "dispatch_mode": "serial",
            "max_active": 1,
        },
        "routes": [],
    }


def _agent_graph() -> dict[str, object]:
    return {
        "nodes": [{"node_id": "main", "kind": "main"}],
        "edges": [],
        "coordination": {
            "mode": "agent",
            "entry_nodes": ["main"],
            "dispatch_mode": "serial",
            "max_active": 1,
        },
        "routes": [],
    }


def _spec(kind: str) -> dict[str, object]:
    return {
        "kind": kind,
        "provider": "claude",
        "transport": "direct" if kind == "main" else "acp",
        "model": "claude-test",
        "effort": "medium",
        "permission": "orchestrator" if kind == "main" else "workspace-write",
        "instructions": kind,
        "execution": "tui_direct" if kind == "main" else "background",
        **({} if kind == "main" else {"adapter_id": "claude-acp-scoped-0.70.0"}),
    }


def _state(root: Path, *, program: bool) -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    if program:
        graph = _program_graph()
        role_specs = {"worker": _spec("worker")}
        controller = {"coordinator_terminal": "controller-terminal"}
        native = {
            "phase": "running",
            "run_nonce": "nonce1234",
            "coordinator_argv": ["/usr/bin/true"],
        }
    else:
        graph = _agent_graph()
        role_specs = {"main": _spec("main")}
        controller = {"main_terminal": "main-terminal"}
        native = {
            "phase": "running",
            "run_nonce": "nonce1234",
            "main_argv": ["/usr/bin/true"],
        }
    return {
        "version": 4,
        "runtime": "zellij",
        "team_id": "team",
        "workspace": str(workspace),
        "config_path": str(root / "config.toml"),
        "state_path": str(root / "state.json"),
        "launcher_path": str(root / "agent-team"),
        "run_id": "run-1",
        **controller,
        "task_specs": [],
        "graph": graph,
        "role_specs": role_specs,
        "roles": {},
        "native": native,
    }


class ProgramNativeStateTest(unittest.TestCase):
    def test_controller_keys_are_mode_specific_and_frozen(self) -> None:
        agent = controller_keys({"version": 3})
        self.assertEqual(
            agent,
            ControllerKeys("main_terminal", "main_argv", "main_process", "agent_pid"),
        )
        program = controller_keys({"version": 4, "graph": _program_graph()})
        self.assertEqual(
            program,
            ControllerKeys(
                "coordinator_terminal",
                "coordinator_argv",
                "coordinator_process",
                "coordinator_pid",
            ),
        )
        self.assertEqual(
            controller_keys({"version": 4, "graph": _agent_graph()}),
            agent,
        )

    def test_controller_process_receipt_uses_the_selected_pid_key(self) -> None:
        validate_native_controller_process(
            {
                "supervisor_pid": 101,
                "coordinator_pid": 102,
                "process_group_id": 102,
                "launch_nonce": "a" * 32,
                "phase": "running",
            },
            pid_key="coordinator_pid",
        )
        with self.assertRaises(RuntimeValidationError):
            validate_native_controller_process(
                {
                    "supervisor_pid": 101,
                    "agent_pid": 102,
                    "process_group_id": 102,
                    "launch_nonce": "a" * 32,
                    "phase": "running",
                },
                pid_key="coordinator_pid",
            )

    def test_named_state_requires_mode_specific_lifecycle_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = _state(root, program=True)
            validate_state_object(Path(program["state_path"]), program)

            for mutation in (
                {"main_terminal": "fake"},
                {"native": {**program["native"], "main_argv": ["/usr/bin/false"]}},
                {
                    "native": {
                        key: value
                        for key, value in program["native"].items()
                        if key != "coordinator_argv"
                    }
                },
            ):
                with self.subTest(mutation=mutation):
                    changed = {**program, **mutation}
                    with self.assertRaises(RuntimeValidationError):
                        validate_state_object(Path(program["state_path"]), changed)

            agent = _state(root, program=False)
            validate_state_object(Path(agent["state_path"]), agent)
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(
                    Path(agent["state_path"]),
                    {**agent, "coordinator_terminal": "fake"},
                )
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(
                    Path(agent["state_path"]),
                    {
                        **agent,
                        "native": {
                            **agent["native"],
                            "coordinator_argv": ["/usr/bin/false"],
                        },
                    },
                )


if __name__ == "__main__":
    unittest.main()
