from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import cast

from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.native_controller import ControllerKeys, controller_keys
from agent_team.native_delivery import container
from agent_team.runtime import (
    NAMED_STATE_VERSION,
    PARALLEL_STATE_VERSION,
    RuntimeValidationError,
    resolve_state_role,
    validate_state_object,
)
from agent_team.task_execution import task_digest
from agent_team.task_spec import TaskSpec


def _parallel_graph(*, max_active: int = 1) -> GraphSpec:
    return GraphSpec(
        nodes=(
            NodeRef("worker-a", Role.WORKER),
            NodeRef("worker-b", Role.WORKER),
        ),
        edges=(),
        coordination=Coordination(
            "program", ("worker-a", "worker-b"), "parallel", max_active
        ),
        routes=(),
    )


def _spec(kind: str = "worker") -> dict[str, object]:
    permission = "workspace-write" if kind == "worker" else "read-only"
    adapter_id = "claude-acp-scoped-0.70.0" if kind == "worker" else "claude-acp-0.70.0"
    return {
        "kind": kind,
        "provider": "claude",
        "transport": "acp",
        "model": "fable",
        "effort": "high",
        "permission": permission,
        "instructions": kind,
        "execution": "background",
        "adapter_id": adapter_id,
    }


def _assignment(root: Path, node_id: str) -> dict[str, object]:
    return {
        "role": node_id,
        "role_kind": "worker",
        "task_id": f"provider-{node_id}",
        "dispatch_id": f"dispatch-{node_id}",
        "terminal_handle": f"terminal-{node_id}",
        "completion_observed": False,
        "launcher_owned_runner": True,
        "execution": "background",
        "adapter_id": "claude-acp-scoped-0.70.0",
        "launch_nonce": f"nonce{node_id.replace('-', '')}",
        "prompt_path": str(root / f"prompt-{node_id}.md"),
        "provider_private_root": str(root / f"private-{node_id}"),
        "snapshot_root": str(root / f"snapshot-{node_id}"),
        "adapter_snapshot": {
            "adapter_id": "claude-acp-scoped-0.70.0",
            "revision": "sdk-1",
            "executable": "/bin/claude",
            "version": "claude 1",
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
    assignment: dict[str, object], *, delivery_id: str | None = None
) -> dict[str, object]:
    return {
        "role": assignment["role"],
        "role_kind": assignment["role_kind"],
        "run_id": "run-parallel-1",
        "task_id": assignment["task_id"],
        "dispatch_id": assignment["dispatch_id"],
        "terminal_handle": assignment["terminal_handle"],
        "launch_nonce": assignment["launch_nonce"],
        "outcome": "succeeded",
        "body": "completed",
        "cleanup_confirmed": True,
        "delivery_id": delivery_id or f"result-{assignment['role']}",
    }


def _question(assignment: dict[str, object], *, delivery_id: str) -> dict[str, object]:
    node_id = cast(str, assignment["role"])
    return {
        "role": node_id,
        "role_kind": "worker",
        "run_id": "run-parallel-1",
        "task_id": assignment["task_id"],
        "dispatch_id": assignment["dispatch_id"],
        "terminal_handle": assignment["terminal_handle"],
        "launch_nonce": assignment["launch_nonce"],
        "phase": "observed",
        "request": {
            "kind": "question",
            "session_id": f"session-{node_id}",
            "tool_call_id": f"tool-{node_id}",
            "questions": [
                {"field": "question_0_custom", "body": f"Approve {node_id}?"}
            ],
        },
        "delivery_id": delivery_id,
        "message_ids": [f"message-{node_id}"],
        "answers": {},
        "error": None,
    }


def _state(root: Path, *, max_active: int = 2) -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    graph = _parallel_graph(max_active=max_active)
    first = _assignment(root, "worker-a")
    second = _assignment(root, "worker-b")
    return {
        "version": PARALLEL_STATE_VERSION,
        "runtime": "zellij",
        "team_id": "parallel-team",
        "workspace": str(workspace),
        "config_path": str(root / "config.toml"),
        "state_path": str(root / "state.json"),
        "launcher_path": str(root / "agent-team"),
        "run_id": "run-parallel-1",
        "coordinator_terminal": "coordinator-terminal",
        "native": {
            "phase": "running",
            "run_nonce": "coordinatornonce",
            "coordinator_argv": ["/usr/bin/true"],
        },
        "task_specs": [],
        "graph": graph.as_dict(),
        "role_specs": {"worker-a": _spec(), "worker-b": _spec()},
        "roles": {"worker-a": first, "worker-b": second},
    }


def _task_spec() -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": "task-a",
            "objective": "Implement task A",
            "acceptance_criteria": ["the change is complete"],
            "allowed_paths": ["src/a"],
            "forbidden_paths": [],
            "dependencies": [],
            "verification": [
                {"name": "check", "argv": ["/usr/bin/true"], "timeout_seconds": 30}
            ],
            "evidence_requirements": ["result body"],
            "consultation_conditions": [],
        }
    )


def _state_with_task_assignment(root: Path) -> dict[str, object]:
    state = _state(root, max_active=2)
    task = _task_spec()
    graph = GraphSpec(
        nodes=(
            NodeRef("worker-a", Role.WORKER),
            NodeRef("reviewer-a", Role.REVIEWER),
        ),
        edges=(GraphEdge("worker-a", "reviewer-a", "reviewed-by"),),
        coordination=Coordination("program", ("worker-a",), "parallel", 2),
        routes=(TaskRoute("task-a", None, None, "worker-a", "reviewer-a"),),
    )
    assignment = cast(dict[str, object], state["roles"]["worker-a"])
    assignment["task_spec"] = task.as_dict()
    assignment["task_stage"] = "implementation"
    assignment["task_revision"] = None
    state["graph"] = graph.as_dict()
    state["role_specs"] = {
        "worker-a": _spec("worker"),
        "reviewer-a": _spec("reviewer"),
    }
    state["roles"] = {"worker-a": assignment}
    state["task_specs"] = [task.as_dict()]
    state["max_review_rounds"] = 2
    state["tasks"] = {
        "task-a": {
            "spec": task.as_dict(),
            "digest": task_digest(task),
            "dispatch_id": assignment["dispatch_id"],
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
    state["program_wave"] = {
        "task_ids": ["task-a"],
        "phase": "writers",
        "revision": None,
    }
    return state


def _v4_released_state(root: Path) -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    graph = GraphSpec(
        nodes=(NodeRef("worker-a", Role.WORKER),),
        edges=(),
        coordination=Coordination("program", ("worker-a",), "serial", 1),
        routes=(),
    )
    result = {
        "role": "worker-a",
        "role_kind": "worker",
        "run_id": "run-v4-release",
        "task_id": "provider-worker-a",
        "dispatch_id": "dispatch-worker-a",
        "terminal_handle": "terminal-worker-a",
        "launch_nonce": "nonceworkera",
        "outcome": "succeeded",
        "body": "completed",
        "cleanup_confirmed": True,
        "delivery_id": "delivery-v4-release",
    }
    return {
        "version": NAMED_STATE_VERSION,
        "runtime": "zellij",
        "team_id": "v4-release-team",
        "workspace": str(workspace),
        "config_path": str(root / "config.toml"),
        "state_path": str(root / "state.json"),
        "launcher_path": str(root / "agent-team"),
        "run_id": "run-v4-release",
        "coordinator_terminal": "coordinator-terminal",
        "native": {
            "phase": "running",
            "run_nonce": "coordinatornonce",
            "coordinator_argv": ["/usr/bin/true"],
        },
        "task_specs": [],
        "graph": graph.as_dict(),
        "role_specs": {"worker-a": _spec()},
        "roles": {},
        "native_result": result,
        "pending_delivery_id": result["delivery_id"],
        "pending_delivery_kind": "worker_done",
        "pending_delivery_stage": "released",
    }


class ParallelRuntimeStateTest(unittest.TestCase):
    def test_state5_roundtrips_two_assignments_with_explicit_cap_two(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = _state(Path(directory), max_active=2)

            self.assertEqual(NAMED_STATE_VERSION, 4)
            self.assertEqual(PARALLEL_STATE_VERSION, 5)
            validate_state_object(Path(state["state_path"]), state)
            self.assertEqual(
                controller_keys(state),
                ControllerKeys(
                    "coordinator_terminal",
                    "coordinator_argv",
                    "coordinator_process",
                    "coordinator_pid",
                ),
            )
            self.assertEqual(
                resolve_state_role(state, "worker-a"),
                NodeRef("worker-a", Role.WORKER),
            )
            self.assertEqual(
                resolve_state_role(state, "worker-b"),
                NodeRef("worker-b", Role.WORKER),
            )
            roles = cast(dict[str, object], state["roles"])
            self.assertIs(container(state, "worker-a"), roles["worker-a"])
            self.assertIs(container(state, "worker-b"), roles["worker-b"])

    def test_state5_result_is_owned_by_the_containing_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            roles = cast(dict[str, dict[str, object]], state["roles"])
            roles["worker-a"]["native_result"] = _result(roles["worker-a"])
            validate_state_object(Path(state["state_path"]), state)

            foreign = copy.deepcopy(state)
            foreign_roles = cast(dict[str, dict[str, object]], foreign["roles"])
            foreign_result = cast(
                dict[str, object], foreign_roles["worker-a"]["native_result"]
            )
            foreign_result["role"] = "worker-b"
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(Path(foreign["state_path"]), foreign)

            wrong_kind = copy.deepcopy(state)
            wrong_roles = cast(dict[str, dict[str, object]], wrong_kind["roles"])
            wrong_roles["worker-a"]["native_result"] = {
                **_result(wrong_roles["worker-a"]),
                "role_kind": "reviewer",
            }
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(Path(wrong_kind["state_path"]), wrong_kind)

    def test_state5_task_result_requires_the_canonical_task_spec_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state_with_task_assignment(root)
            assignment = cast(dict[str, object], state["roles"]["worker-a"])
            result = _result(assignment)
            result["logical_task_id"] = "task-a"
            assignment.update(
                {
                    "native_result": result,
                    "pending_delivery_id": result["delivery_id"],
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "observed",
                    "completion_observed": True,
                }
            )
            validate_state_object(Path(state["state_path"]), state)

            for logical_task_id in (None, "task-b"):
                damaged = copy.deepcopy(state)
                damaged_assignment = cast(
                    dict[str, object], damaged["roles"]["worker-a"]
                )
                damaged_result = cast(
                    dict[str, object], damaged_assignment["native_result"]
                )
                if logical_task_id is None:
                    damaged_result.pop("logical_task_id")
                else:
                    damaged_result["logical_task_id"] = logical_task_id
                with (
                    self.subTest(logical_task_id=logical_task_id),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(Path(damaged["state_path"]), damaged)

    def test_state5_assignment_cap_counts_released_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = _state(root, max_active=1)
            valid_roles = cast(dict[str, object], valid["roles"])
            valid_roles.pop("worker-b")
            validate_state_object(Path(valid["state_path"]), valid)

            over_cap = _state(root, max_active=1)
            over_roles = cast(dict[str, dict[str, object]], over_cap["roles"])
            released_assignment = over_roles["worker-a"]
            released_result = _result(released_assignment)
            released_assignment.update(
                {
                    "native_result": released_result,
                    "pending_delivery_id": released_result["delivery_id"],
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "released",
                    "completion_observed": True,
                }
            )
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(Path(over_cap["state_path"]), over_cap)

    def test_state5_rejects_cross_assignment_resource_path_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = _state(root)
            base_roles = cast(dict[str, dict[str, object]], base["roles"])
            private_root = cast(str, base_roles["worker-a"]["provider_private_root"])
            base_roles["worker-a"]["question_socket"] = str(
                Path(private_root) / "q.sock"
            )
            base_roles["worker-a"]["write_policy_path"] = str(
                Path(private_root) / "write-policy.json"
            )
            validate_state_object(Path(base["state_path"]), base)

            root_alias = root / "resource-alias"
            root_alias.symlink_to(Path(private_root).parent, target_is_directory=True)
            for replacement in (
                private_root,
                str(Path(private_root) / "nested"),
                str(root_alias / Path(private_root).name),
            ):
                damaged = copy.deepcopy(base)
                damaged_roles = cast(dict[str, dict[str, object]], damaged["roles"])
                damaged_roles["worker-b"]["snapshot_root"] = replacement
                with (
                    self.subTest(replacement=replacement),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(Path(damaged["state_path"]), damaged)

    def test_state5_rejects_duplicate_assignment_claims_but_allows_prelaunch_pids(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            roles = cast(dict[str, dict[str, object]], state["roles"])
            roles["worker-a"]["runner_pid"] = 501
            roles["worker-a"]["runner_process_group_id"] = None
            validate_state_object(Path(state["state_path"]), state)

            for field in (
                "task_id",
                "dispatch_id",
                "terminal_handle",
                "launch_nonce",
                "prompt_path",
                "provider_private_root",
                "snapshot_root",
                "runner_pid",
            ):
                damaged = copy.deepcopy(state)
                damaged_roles = cast(dict[str, dict[str, object]], damaged["roles"])
                damaged_roles["worker-b"][field] = damaged_roles["worker-a"][field]
                with (
                    self.subTest(field=field),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(Path(damaged["state_path"]), damaged)

    def test_v4_released_root_result_survives_role_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = _v4_released_state(Path(directory))
            validate_state_object(Path(state["state_path"]), state)

    def test_state5_rejects_pending_without_result_or_completion_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = "worker-a"
            cases: tuple[dict[str, object], ...] = (
                {
                    "pending_delivery_id": "delivery-without-result",
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "observed",
                },
                {"pending_delivery_stage": "read"},
                {"pending_question_ids": ["message-worker-a"]},
                {"completion_observed": True},
            )
            for mutation in cases:
                state = _state(root)
                assignment = cast(dict[str, object], state["roles"][worker])
                assignment.update(mutation)
                with (
                    self.subTest(mutation=mutation),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(Path(state["state_path"]), state)

            null_result = _state(root)
            null_assignment = cast(dict[str, object], null_result["roles"][worker])
            null_assignment["native_result"] = None
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(Path(null_result["state_path"]), null_result)

            missing_pending = _state(root)
            missing_assignment = cast(
                dict[str, object], missing_pending["roles"][worker]
            )
            missing_result = _result(missing_assignment)
            missing_assignment.update(
                {"native_result": missing_result, "completion_observed": True}
            )
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(
                    Path(missing_pending["state_path"]), missing_pending
                )

    def test_state5_rejects_old_version_root_delivery_and_missing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = _state(root)
            for field in (
                "native_result",
                "native_question",
                "pending_delivery_id",
                "pending_delivery_kind",
                "pending_delivery_stage",
            ):
                with self.subTest(field=field):
                    damaged = copy.deepcopy(base)
                    damaged[field] = None
                    with self.assertRaises(RuntimeValidationError):
                        validate_state_object(Path(damaged["state_path"]), damaged)

            damaged = copy.deepcopy(base)
            roles = cast(dict[str, dict[str, object]], damaged["roles"])
            roles["worker-a"]["native_result"] = {
                key: value
                for key, value in _result(roles["worker-a"]).items()
                if key != "delivery_id"
            }
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(Path(damaged["state_path"]), damaged)

            for invalid in (
                {"version": 5},
                {"version": 5, "graph": _parallel_graph().as_dict()},
                {
                    "version": 5,
                    "graph": _parallel_graph().as_dict(),
                    "role_specs": {},
                },
            ):
                with (
                    self.subTest(invalid=invalid),
                    self.assertRaises(RuntimeValidationError),
                ):
                    resolve_state_role(invalid, "worker-a")

    def test_state5_rejects_cross_node_question_and_result_delivery_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            roles = cast(dict[str, dict[str, object]], state["roles"])
            roles["worker-a"]["native_result"] = _result(
                roles["worker-a"], delivery_id="same-delivery"
            )
            question = _question(roles["worker-b"], delivery_id="same-delivery")
            roles["worker-b"].update(
                {
                    "native_question": question,
                    "pending_delivery_id": question["delivery_id"],
                    "pending_delivery_kind": "question",
                    "pending_delivery_stage": "observed",
                    "pending_question_ids": question["message_ids"],
                    "replied_question_ids": [],
                }
            )
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(Path(state["state_path"]), state)

    def test_state5_released_result_requires_cleanup_and_observed_completion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stage in ("observed", "read"):
                state = _state(root)
                assignment = cast(dict[str, object], state["roles"]["worker-a"])
                result = _result(assignment)
                assignment.update(
                    {
                        "native_result": result,
                        "pending_delivery_id": result["delivery_id"],
                        "pending_delivery_kind": "worker_done",
                        "pending_delivery_stage": stage,
                        "completion_observed": True,
                    }
                )
                validate_state_object(Path(state["state_path"]), state)
                assignment["completion_observed"] = False
                with (
                    self.subTest(stage=stage, completion_observed=False),
                    self.assertRaises(RuntimeValidationError),
                ):
                    validate_state_object(Path(state["state_path"]), state)

            released = _state(root)
            assignment = cast(dict[str, object], released["roles"]["worker-a"])
            result = _result(assignment)
            assignment.update(
                {
                    "native_result": result,
                    "pending_delivery_id": result["delivery_id"],
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "released",
                    "completion_observed": True,
                }
            )
            validate_state_object(Path(released["state_path"]), released)

            bad = copy.deepcopy(released)
            bad_assignment = cast(dict[str, object], bad["roles"]["worker-a"])
            bad_result = cast(dict[str, object], bad_assignment["native_result"])
            bad_result["cleanup_confirmed"] = False
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(Path(bad["state_path"]), bad)

            bad = copy.deepcopy(released)
            bad_assignment = cast(dict[str, object], bad["roles"]["worker-a"])
            bad_assignment["completion_observed"] = False
            with self.assertRaises(RuntimeValidationError):
                validate_state_object(Path(bad["state_path"]), bad)


if __name__ == "__main__":
    unittest.main()
