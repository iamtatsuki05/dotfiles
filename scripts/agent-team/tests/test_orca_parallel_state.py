from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import cast

from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.runtime import (
    PARALLEL_STATE_VERSION,
    RuntimeValidationError,
    resolve_state_role,
    validate_state_object,
)
from agent_team.scoped_acp import native_profile
from agent_team.task_execution import task_digest
from agent_team.task_spec import TaskSpec


def _task(task_id: str) -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": task_id,
            "objective": f"Complete {task_id}.",
            "acceptance_criteria": ["the result is complete"],
            "allowed_paths": [f"src/{task_id}"],
            "forbidden_paths": [],
            "dependencies": [],
            "verification": [
                {"name": "check", "argv": ["/usr/bin/true"], "timeout_seconds": 1}
            ],
            "evidence_requirements": ["result body"],
            "consultation_conditions": [],
        }
    )


def _role_spec(kind: str) -> dict[str, object]:
    if kind == "main":
        return {
            "kind": "main",
            "provider": "claude",
            "transport": "direct",
            "model": "fable",
            "effort": "high",
            "permission": "orchestrator",
            "instructions": "main",
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
    root: Path, node_id: str, task: TaskSpec, dispatch_id: str
) -> dict[str, object]:
    profile = native_profile("claude", "worker")
    return {
        "role": node_id,
        "role_kind": "worker",
        "task_id": f"provider-{node_id}",
        "dispatch_id": dispatch_id,
        "terminal_handle": f"terminal-{node_id}",
        "completion_observed": False,
        "launcher_owned_terminal": True,
        "execution": "background",
        "adapter_id": profile["adapter_id"],
        "launch_nonce": f"nonce{node_id.replace('-', '')}",
        "prompt_path": str(root / f"prompt-{node_id}.md"),
        "provider_private_root": str(root / f"private-{node_id}"),
        "snapshot_root": str(root / f"snapshot-{node_id}"),
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
        "task_spec": task.as_dict(),
        "task_stage": "implementation",
        "task_revision": None,
    }


def _result(
    assignment: dict[str, object], *, delivery_id: str, cleanup_confirmed: bool = True
) -> dict[str, object]:
    return {
        "role": assignment["role"],
        "role_kind": assignment["role_kind"],
        "run_id": "run-orca-parallel",
        "task_id": assignment["task_id"],
        "dispatch_id": assignment["dispatch_id"],
        "terminal_handle": assignment["terminal_handle"],
        "launch_nonce": assignment["launch_nonce"],
        "outcome": "succeeded",
        "body": "completed",
        "cleanup_confirmed": cleanup_confirmed,
        "notification_expected": cleanup_confirmed,
        "delivery_id": delivery_id,
        "logical_task_id": cast(dict[str, object], assignment["task_spec"])["task_id"],
    }


def _batch_member(
    assignment: dict[str, object], *, message_id: str, kind: str = "worker_done"
) -> dict[str, object]:
    return {
        "role": assignment["role"],
        "role_kind": assignment["role_kind"],
        "message_id": message_id,
        "kind": kind,
        "task_id": assignment["task_id"],
        "dispatch_id": assignment["dispatch_id"],
        "terminal_handle": assignment["terminal_handle"],
        "launch_nonce": assignment["launch_nonce"],
    }


def _state(root: Path, *, max_active: int = 2) -> dict[str, object]:
    root.joinpath("workspace").mkdir()
    tasks = (_task("task-a"), _task("task-b"))
    nodes = (
        NodeRef("main", Role.MAIN),
        NodeRef("worker-a", Role.WORKER),
        NodeRef("worker-b", Role.WORKER),
        NodeRef("reviewer-a", Role.REVIEWER),
        NodeRef("reviewer-b", Role.REVIEWER),
    )
    graph = GraphSpec(
        nodes=nodes,
        edges=(
            GraphEdge("main", "worker-a", "delegates-to"),
            GraphEdge("main", "worker-b", "delegates-to"),
            GraphEdge("worker-a", "reviewer-a", "reviewed-by"),
            GraphEdge("worker-b", "reviewer-b", "reviewed-by"),
        ),
        coordination=Coordination("agent", ("main",), "parallel", max_active),
        routes=(
            TaskRoute("task-a", None, None, "worker-a", "reviewer-a"),
            TaskRoute("task-b", None, None, "worker-b", "reviewer-b"),
        ),
    )
    dispatches = {"task-a": "dispatch-a", "task-b": "dispatch-b"}
    roles = {"worker-a": _assignment(root, "worker-a", tasks[0], dispatches["task-a"])}
    roles["worker-b"] = _assignment(root, "worker-b", tasks[1], dispatches["task-b"])
    records = {
        task.task_id: {
            "spec": task.as_dict(),
            "digest": task_digest(task),
            "dispatch_id": dispatches[task.task_id],
            "status": "running",
            "stage": "implementation",
            "revision": None,
            "review_rounds": {"plan": 0, "implementation": 0},
            "role": node_id,
            "role_kind": "worker",
            "writer_role": node_id,
            "writer_kind": "worker",
        }
        for task, node_id in zip(tasks, ("worker-a", "worker-b"), strict=True)
    }
    return {
        "version": PARALLEL_STATE_VERSION,
        "runtime": "orca",
        "team_id": "orca-parallel",
        "workspace": str(root / "workspace"),
        "config_path": str(root / "config.toml"),
        "state_path": str(root / "state.json"),
        "launcher_path": str(root / "agent-team"),
        "worktree_id": "repo::workspace",
        "orca_socket": str(root / "orca.sock"),
        "run_id": "run-orca-parallel",
        "main_terminal": "main-terminal",
        "task_specs": [task.as_dict() for task in tasks],
        "max_review_rounds": 2,
        "tasks": records,
        "agent_batch": {
            "task_ids": [task.task_id for task in tasks],
            "phase": "writers",
            "revision": None,
        },
        "graph": graph.as_dict(),
        "role_specs": {node.node_id: _role_spec(node.kind.value) for node in nodes},
        "roles": roles,
    }


def _state_path(state: dict[str, object]) -> Path:
    value = state.get("state_path")
    if not isinstance(value, str):
        raise TypeError("state_path fixture is invalid")
    return Path(value)


def _role_map(state: dict[str, object]) -> dict[str, dict[str, object]]:
    value = state.get("roles")
    if not isinstance(value, dict):
        raise TypeError("roles fixture is invalid")
    return cast(dict[str, dict[str, object]], value)


def _graph_map(state: dict[str, object]) -> dict[str, object]:
    value = state.get("graph")
    if not isinstance(value, dict):
        raise TypeError("graph fixture is invalid")
    return value


class OrcaParallelStateTest(unittest.TestCase):
    def test_two_assignments_use_exact_main_graph_and_parallel_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = _state(Path(directory))
            validate_state_object(_state_path(state), state)
            self.assertEqual(
                resolve_state_role(state, "worker-a"),
                NodeRef("worker-a", Role.WORKER),
            )
            self.assertEqual(
                resolve_state_role(state, "worker-b"),
                NodeRef("worker-b", Role.WORKER),
            )
            self.assertEqual(
                resolve_state_role(state, "main"), NodeRef("main", Role.MAIN)
            )
            main_assignment = copy.deepcopy(state)
            _role_map(main_assignment)["main"] = {
                "role": "main",
                "role_kind": "main",
            }
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(main_assignment), main_assignment)

            over_cap = copy.deepcopy(state)
            coordination = cast(dict[str, object], _graph_map(over_cap)["coordination"])
            coordination["max_active"] = 1
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(over_cap), over_cap)

    def test_result_delivery_release_is_assignment_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            assignment = _role_map(state)["worker-a"]
            result = _result(assignment, delivery_id="delivery-a")
            assignment.update(
                {
                    "completion_observed": True,
                    "orca_result": result,
                    "pending_delivery_id": "delivery-a",
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "observed",
                    "pending_question_ids": [],
                    "replied_question_ids": [],
                }
            )
            state["orca_delivery_batch"] = {
                "delivery_id": "delivery-a",
                "phase": "observed",
                "members": [_batch_member(assignment, message_id="message-a")],
                "message_count": 1,
                "messages_sha256": "a" * 64,
                "error": None,
            }
            validate_state_object(_state_path(state), state)

            assignment.update(
                {
                    "pending_delivery_stage": "released",
                    "orca_release": {
                        "phase": "released",
                        "identity": {
                            field: result[field]
                            for field in (
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
                            "handle": "terminal-worker-a",
                            "close_mode": "tab",
                            "pty_killed": True,
                            "pty_stop_verdict": "exited",
                        },
                    },
                }
            )
            validate_state_object(_state_path(state), state)

    def test_root_orca_delivery_and_native_assignment_metadata_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = _state(root)
            for field in (
                "orca_result",
                "orca_release",
                "pending_orca_effect",
                "pending_delivery_id",
                "pending_delivery_kind",
                "pending_delivery_stage",
                "pending_question_ids",
                "replied_question_ids",
                "orca_question",
            ):
                damaged = copy.deepcopy(base)
                damaged[field] = {}
                with (
                    self.subTest(field=field),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(_state_path(damaged), damaged)

            damaged = copy.deepcopy(base)
            _role_map(damaged)["worker-a"]["runner_pid"] = 123
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(damaged), damaged)

            for field in ("orca_stop_requested", "pending_role_start"):
                damaged = copy.deepcopy(base)
                _role_map(damaged)["worker-a"][field] = {}
                with (
                    self.subTest(field=field),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(_state_path(damaged), damaged)

            unpaired_question = copy.deepcopy(base)
            _role_map(unpaired_question)["worker-a"].update(
                {
                    "pending_delivery_id": "question-delivery",
                    "pending_delivery_kind": "question",
                    "pending_delivery_stage": "observed",
                    "pending_question_ids": ["message-a"],
                    "replied_question_ids": [],
                }
            )
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(unpaired_question), unpaired_question)

    def test_result_identity_and_delivery_ids_cannot_cross_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = _state(root)
            first = _role_map(base)["worker-a"]
            second = _role_map(base)["worker-b"]
            first_result = _result(first, delivery_id="same-delivery")
            second_result = _result(second, delivery_id="same-delivery")
            for assignment, result in ((first, first_result), (second, second_result)):
                assignment.update(
                    {
                        "completion_observed": True,
                        "orca_result": result,
                        "pending_delivery_id": result["delivery_id"],
                        "pending_delivery_kind": "worker_done",
                        "pending_delivery_stage": "observed",
                        "pending_question_ids": [],
                        "replied_question_ids": [],
                    }
                )
            state_batch = {
                "delivery_id": "same-delivery",
                "phase": "observed",
                "members": [
                    _batch_member(first, message_id="message-a"),
                    _batch_member(second, message_id="message-b"),
                ],
                "message_count": 2,
                "messages_sha256": "a" * 64,
                "error": None,
            }
            base["orca_delivery_batch"] = state_batch
            validate_state_object(_state_path(base), base)

            duplicate = copy.deepcopy(base)
            duplicate_result = cast(
                dict[str, object], _role_map(duplicate)["worker-b"]["orca_result"]
            )
            duplicate_result["delivery_id"] = "other-delivery"
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(duplicate), duplicate)

            foreign = copy.deepcopy(base)
            foreign_result = cast(
                dict[str, object], _role_map(foreign)["worker-a"]["orca_result"]
            )
            foreign_result["role"] = "worker-b"
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(foreign), foreign)

    def test_invalid_batch_retains_unpublished_result_without_partial_delivery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid_root = root / "invalid"
            invalid_root.mkdir()
            invalid = _state(invalid_root)
            invalid["orca_delivery_batch"] = {
                "delivery_id": "invalid-delivery",
                "phase": "invalid",
                "members": [],
                "message_count": 2,
                "messages_sha256": "a" * 64,
                "error": "unknown message owner",
            }
            validate_state_object(_state_path(invalid), invalid)

            retained_root = root / "retained"
            retained_root.mkdir()
            retained = _state(retained_root)
            retained_result = _result(
                _role_map(retained)["worker-a"], delivery_id="unused-delivery"
            )
            retained_result.pop("delivery_id")
            _role_map(retained)["worker-a"]["orca_result"] = retained_result
            retained["orca_delivery_batch"] = copy.deepcopy(
                invalid["orca_delivery_batch"]
            )
            validate_state_object(_state_path(retained), retained)

            partial_root = root / "partial"
            partial_root.mkdir()
            partial = _state(partial_root)
            _role_map(partial)["worker-a"].update(
                {
                    "pending_delivery_id": "invalid-delivery",
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "invalid",
                    "pending_question_ids": [],
                    "replied_question_ids": [],
                }
            )
            partial["orca_delivery_batch"] = copy.deepcopy(
                invalid["orca_delivery_batch"]
            )
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(partial), partial)

            incomplete_root = root / "incomplete"
            incomplete_root.mkdir()
            incomplete = _state(incomplete_root)
            _role_map(incomplete)["worker-a"]["completion_observed"] = True
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(_state_path(incomplete), incomplete)


if __name__ == "__main__":
    unittest.main()
