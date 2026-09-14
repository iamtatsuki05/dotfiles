from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Literal

from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.runtime import RuntimeValidationError, validate_state_object
from agent_team.scoped_acp import native_profile
from agent_team.task_execution import task_digest
from agent_team.task_spec import TaskSpec


def _graph(
    *,
    mode: Literal["agent", "program"] = "agent",
    dispatch_mode: Literal["serial", "parallel"] = "serial",
) -> GraphSpec:
    return GraphSpec(
        nodes=(
            NodeRef("main", Role.MAIN),
            NodeRef("worker-a", Role.WORKER),
            NodeRef("reviewer-a", Role.REVIEWER),
        ),
        edges=(
            GraphEdge("main", "worker-a", "delegates-to"),
            GraphEdge("worker-a", "reviewer-a", "reviewed-by"),
        ),
        coordination=Coordination(mode, ("main",), dispatch_mode, 1),
        routes=(),
    )


def _role_spec(kind: str) -> dict[str, object]:
    if kind == "main":
        return {
            "kind": kind,
            "provider": "claude",
            "transport": "direct",
            "model": "fable",
            "effort": "high",
            "permission": "orchestrator",
            "instructions": kind,
            "execution": "tui_direct",
        }
    profile = native_profile("claude", kind)
    return {
        "kind": kind,
        **profile,
        "model": "fable",
        "effort": "high",
        "instructions": kind,
    }


def _assignment(
    root: Path, *, role: str = "worker-a", kind: str = "worker"
) -> dict[str, object]:
    profile = native_profile("claude", kind)
    return {
        "role": role,
        "role_kind": kind,
        "task_id": "remote-task-1",
        "dispatch_id": "remote-dispatch-1",
        "terminal_handle": "remote-terminal-1",
        "completion_observed": False,
        "launcher_owned_terminal": True,
        "execution": "background",
        "adapter_id": profile["adapter_id"],
        "launch_nonce": "nonce1234",
        "prompt_path": str(root / f"prompt-{role}.md"),
        "provider_private_root": str(root / f"private-{role}"),
        "snapshot_root": str(root / f"snapshot-{role}"),
        "adapter_snapshot": {
            "adapter_id": profile["adapter_id"],
            "revision": "adapter-revision",
            "executable": "/usr/bin/claude",
            "version": "claude-test",
            "identity": {
                "device": 1,
                "inode": 2,
                "size": 3,
                "mtime_ns": 4,
                "sha256": "a" * 64,
            },
        },
    }


def _result(
    *, delivery_id: str | None = None, cleanup_confirmed: bool = False
) -> dict[str, object]:
    result: dict[str, object] = {
        "role": "worker-a",
        "role_kind": "worker",
        "run_id": "run-1",
        "task_id": "remote-task-1",
        "dispatch_id": "remote-dispatch-1",
        "terminal_handle": "remote-terminal-1",
        "launch_nonce": "nonce1234",
        "outcome": "failed",
        "body": "trusted runner failure",
        "cleanup_confirmed": cleanup_confirmed,
        "notification_expected": cleanup_confirmed,
    }
    if delivery_id is not None:
        result["delivery_id"] = delivery_id
    return result


def _task() -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": "task-1",
            "objective": "objective",
            "acceptance_criteria": ["result is saved"],
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


def _state(root: Path, *, graph: GraphSpec | None = None) -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    graph = graph or _graph()
    return {
        "version": 4,
        "runtime": "orca",
        "team_id": "orca-named",
        "workspace": str(workspace),
        "config_path": str(root / "config.toml"),
        "state_path": str(root / "state.json"),
        "launcher_path": str(root / "agent-team"),
        "worktree_id": "repo::workspace",
        "orca_socket": str(root / "orca.sock"),
        "run_id": "run-1",
        "main_terminal": "main-terminal",
        "graph": graph.as_dict(),
        "task_specs": [],
        "max_review_rounds": 2,
        "tasks": {},
        "role_specs": {
            "main": _role_spec("main"),
            "worker-a": _role_spec("worker"),
            "reviewer-a": _role_spec("reviewer"),
        },
        "roles": {},
    }


class NamedOrcaStateTest(unittest.TestCase):
    @staticmethod
    def _assert_valid(state: dict[str, object]) -> None:
        try:
            validate_state_object(Path(str(state["state_path"])), state)
        except RuntimeValidationError as exc:
            raise AssertionError(f"expected valid state: {exc}") from exc

    def test_accepts_named_orca_agent_serial_state_without_native_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            self._assert_valid(state)

    def test_rejects_orca_program_and_mismatched_state_graph_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                {
                    **_state(root),
                    "version": 5,
                    "graph": _graph(dispatch_mode="serial").as_dict(),
                },
                {**_state(root), "graph": _graph(mode="program").as_dict()},
                {**_state(root), "graph": _graph(dispatch_mode="parallel").as_dict()},
            )
            for state in cases:
                with (
                    self.subTest(version=state["version"], graph=state["graph"]),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(Path(str(state["state_path"])), state)

    def test_rejects_orca_metadata_mixed_with_native_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = _state(root)
            for field, value in (
                ("native", {}),
                ("native_result", {}),
                ("native_question", {}),
                ("coordinator_terminal", "coordinator-terminal"),
                ("main_argv", ["/usr/bin/true"]),
            ):
                with self.subTest(field=field):
                    damaged = {**base, field: value}
                    with self.assertRaises(RuntimeValidationError):
                        validate_state_object(Path(str(damaged["state_path"])), damaged)

    def test_requires_orca_metadata_and_one_launcher_owned_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = _state(root)
            for field in ("worktree_id", "orca_socket", "main_terminal"):
                damaged = {key: value for key, value in base.items() if key != field}
                with (
                    self.subTest(field=field),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(Path(str(base["state_path"])), damaged)

            with self.assertRaises(RuntimeValidationError):
                validate_state_object(
                    Path(str(base["state_path"])),
                    {
                        **base,
                        "roles": {
                            "worker-a": _assignment(root),
                            "reviewer-a": _assignment(
                                root, role="reviewer-a", kind="reviewer"
                            ),
                        },
                    },
                )

            assignment = _assignment(root)
            assignment["launcher_owned_terminal"] = False
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(
                    Path(str(base["state_path"])),
                    {**base, "roles": {"worker-a": assignment}},
                )

    def test_accepts_unobserved_orca_result_without_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = _assignment(root)
            state = _state(root)
            state["roles"] = {"worker-a": assignment}
            state["orca_result"] = _result()
            self._assert_valid(state)

    def test_binds_observed_worker_done_and_rejects_question_result_mix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = _assignment(root)
            assignment["completion_observed"] = True
            state = _state(root)
            state["roles"] = {"worker-a": assignment}
            state.update(
                {
                    "orca_result": _result(
                        delivery_id="delivery-1", cleanup_confirmed=True
                    ),
                    "pending_delivery_id": "delivery-1",
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "observed",
                    "pending_question_ids": [],
                    "replied_question_ids": [],
                }
            )
            self._assert_valid(state)

            question = copy.deepcopy(state)
            question.update(
                {
                    "pending_delivery_kind": "question",
                    "pending_delivery_stage": "observed",
                    "pending_question_ids": ["message-1"],
                    "replied_question_ids": [],
                }
            )
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(Path(str(question["state_path"])), question)

    def test_released_result_uses_saved_task_record_after_role_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            graph = GraphSpec(
                nodes=(
                    NodeRef("main", Role.MAIN),
                    NodeRef("worker-a", Role.WORKER),
                    NodeRef("reviewer-a", Role.REVIEWER),
                ),
                edges=(
                    GraphEdge("main", "worker-a", "delegates-to"),
                    GraphEdge("worker-a", "reviewer-a", "reviewed-by"),
                ),
                coordination=Coordination("agent", ("main",), "serial", 1),
                routes=(TaskRoute("task-1", None, None, "worker-a", "reviewer-a"),),
            )
            state["graph"] = graph.as_dict()
            task = _task()
            state["task_specs"] = [task.as_dict()]
            state["roles"] = {}
            state["orca_result"] = {
                **_result(delivery_id="delivery-1", cleanup_confirmed=True),
                "logical_task_id": "task-1",
            }
            state.update(
                {
                    "pending_delivery_id": "delivery-1",
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "released",
                    "pending_question_ids": [],
                    "replied_question_ids": [],
                }
            )
            state["tasks"] = {
                "task-1": {
                    "spec": task.as_dict(),
                    "digest": task_digest(task),
                    "dispatch_id": "remote-dispatch-1",
                    "status": "running",
                    "stage": "implementation",
                    "revision": None,
                    "review_rounds": {"plan": 0, "implementation": 0},
                    "role": "worker-a",
                    "role_kind": "worker",
                    "writer_role": "worker-a",
                    "writer_kind": "worker",
                }
            }
            state["orca_release"] = {
                "phase": "released",
                "identity": {
                    key: state["orca_result"][key]
                    for key in (
                        "role",
                        "role_kind",
                        "run_id",
                        "task_id",
                        "dispatch_id",
                        "terminal_handle",
                        "launch_nonce",
                        "delivery_id",
                    )
                },
                "terminal_close": {
                    "handle": "remote-terminal-1",
                    "close_mode": "tab",
                    "pty_killed": True,
                    "pty_stop_verdict": "exited",
                },
            }
            self._assert_valid(state)


if __name__ == "__main__":
    unittest.main()
