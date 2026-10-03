from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Literal

from agent_team import runtime
from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import (
    Coordination,
    GraphEdge,
    GraphSpec,
    TaskRoute,
)
from agent_team.scoped_acp import native_profile
from agent_team.task_spec import TaskSpec


def _graph(
    *,
    mode: Literal["agent", "program"] = "agent",
    dispatch_mode: Literal["serial", "parallel"] = "serial",
    routes: tuple[TaskRoute, ...] = (),
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
        routes=routes,
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
    return {
        "kind": kind,
        **native_profile("claude", kind),
        "model": "fable",
        "effort": "high",
        "instructions": kind,
    }


def _assignment(
    root: Path,
    *,
    role: str = "worker-a",
    kind: str = "worker",
    task_spec: dict[str, object] | None = None,
) -> dict[str, object]:
    profile = native_profile("claude", kind)
    assignment: dict[str, object] = {
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
    if task_spec is not None:
        assignment["task_spec"] = task_spec
        assignment["task_stage"] = "implementation"
        assignment["task_revision"] = None
    return assignment


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


def _result(
    *,
    delivery_id: str | None = None,
    role: str = "worker-a",
    role_kind: str = "worker",
    outcome: str = "failed",
    cleanup_confirmed: bool = False,
    logical_task_id: str | None = None,
    task_evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "role": role,
        "role_kind": role_kind,
        "run_id": "run-1",
        "task_id": "remote-task-1",
        "dispatch_id": "remote-dispatch-1",
        "terminal_handle": "remote-terminal-1",
        "launch_nonce": "nonce1234",
        "outcome": outcome,
        "body": "trusted runner output",
        "cleanup_confirmed": cleanup_confirmed,
        "notification_expected": cleanup_confirmed,
    }
    if delivery_id is not None:
        result["delivery_id"] = delivery_id
    if logical_task_id is not None:
        result["logical_task_id"] = logical_task_id
    if task_evidence is not None:
        result["task_evidence"] = task_evidence
    return result


def _release(
    *,
    phase: str,
    delivery_id: str = "delivery-1",
    terminal_close: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "phase": phase,
        "identity": {
            "role": "worker-a",
            "role_kind": "worker",
            "run_id": "run-1",
            "task_id": "remote-task-1",
            "dispatch_id": "remote-dispatch-1",
            "terminal_handle": "remote-terminal-1",
            "launch_nonce": "nonce1234",
            "delivery_id": delivery_id,
        },
        "terminal_close": terminal_close,
    }


def _close_receipt(*, handle: str = "remote-terminal-1") -> dict[str, object]:
    return {
        "handle": handle,
        "close_mode": None,
        "pty_killed": True,
        "pty_stop_verdict": "terminated",
    }


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


def _assert_valid(state: dict[str, object]) -> None:
    try:
        runtime.validate_state_object(Path(str(state["state_path"])), state)
    except runtime.RuntimeValidationError as exc:
        raise AssertionError(f"expected valid state: {exc}") from exc


class OrcaStateLifecycleTest(unittest.TestCase):
    def test_orca_requires_task_records_container(self):
        with tempfile.TemporaryDirectory() as directory:
            state = _state(Path(directory))
            state.pop("tasks")
            with self.assertRaises(runtime.RuntimeValidationError):
                runtime.validate_state_object(Path(str(state["state_path"])), state)

    def test_result_without_active_assignment_requires_released_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            state = _state(Path(directory))
            state["orca_result"] = _result(cleanup_confirmed=True)
            with self.assertRaises(runtime.RuntimeValidationError):
                runtime.validate_state_object(Path(str(state["state_path"])), state)

    def test_pre_delivery_result_and_observed_delivery_id_are_both_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            state["roles"] = {"worker-a": _assignment(root)}
            state["orca_result"] = _result()
            _assert_valid(state)

            observed = copy.deepcopy(state)
            observed["roles"]["worker-a"]["completion_observed"] = True
            observed["orca_result"] = _result(
                delivery_id="delivery-1", cleanup_confirmed=True
            )
            observed.update(
                {
                    "pending_delivery_id": "delivery-1",
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "observed",
                    "pending_question_ids": [],
                    "replied_question_ids": [],
                }
            )
            _assert_valid(observed)
            for stage in ("observed", "read"):
                missing_id = copy.deepcopy(observed)
                missing_id["pending_delivery_stage"] = stage
                missing_id["orca_result"].pop("delivery_id")
                with (
                    self.subTest(stage=stage),
                    self.assertRaises(runtime.RuntimeValidationError),
                ):
                    runtime.validate_state_object(
                        Path(str(missing_id["state_path"])), missing_id
                    )

    def test_release_retains_all_identity_and_accepts_non_tab_close_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            assignment = _assignment(root)
            assignment["completion_observed"] = True
            state["roles"] = {"worker-a": assignment}
            state["orca_result"] = _result(
                delivery_id="delivery-1", cleanup_confirmed=True
            )
            state.update(
                {
                    "pending_delivery_id": "delivery-1",
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "read",
                    "pending_question_ids": [],
                    "replied_question_ids": [],
                    "orca_release": _release(
                        phase="closed", terminal_close=_close_receipt()
                    ),
                }
            )
            _assert_valid(state)

            released = copy.deepcopy(state)
            released["roles"] = {}
            released["pending_delivery_stage"] = "released"
            released["orca_release"] = _release(
                phase="released", terminal_close=_close_receipt()
            )
            _assert_valid(released)

            mismatched = copy.deepcopy(released)
            mismatched["orca_release"]["identity"]["terminal_handle"] = "other"
            with self.assertRaises(runtime.RuntimeValidationError):
                runtime.validate_state_object(
                    Path(str(mismatched["state_path"])), mismatched
                )

    def test_release_closing_is_durable_but_bad_close_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            assignment = _assignment(root)
            assignment["completion_observed"] = True
            state["roles"] = {"worker-a": assignment}
            state["orca_result"] = _result(
                delivery_id="delivery-1", cleanup_confirmed=True
            )
            state.update(
                {
                    "pending_delivery_id": "delivery-1",
                    "pending_delivery_kind": "worker_done",
                    "pending_delivery_stage": "read",
                    "pending_question_ids": [],
                    "replied_question_ids": [],
                    "orca_release": _release(phase="closing"),
                }
            )
            _assert_valid(state)

            bad = copy.deepcopy(state)
            bad["orca_release"] = _release(
                phase="closed",
                terminal_close={
                    **_close_receipt(),
                    "pty_stop_verdict": "unverifiable",
                },
            )
            with self.assertRaises(runtime.RuntimeValidationError):
                runtime.validate_state_object(Path(str(bad["state_path"])), bad)

    def test_rejects_native_process_fields_but_allows_scoped_policy_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            assignment = _assignment(root)
            assignment.update(
                {
                    "write_policy_path": str(root / "write-policy.json"),
                    "write_policy_sha256": "b" * 64,
                    "question_socket": str(root / "q.sock"),
                    "acp_session_id": "session-1",
                }
            )
            state["roles"] = {"worker-a": assignment}
            _assert_valid(state)
            for field in (
                "runner_pid",
                "runner_process_group_id",
                "runner_argv",
                "runner_startup_argv",
                "question_receipts",
                "native_result",
                "native_question",
                "launcher_owned_runner",
            ):
                damaged = copy.deepcopy(state)
                damaged["roles"]["worker-a"][field] = True
                with (
                    self.subTest(field=field),
                    self.assertRaises(runtime.RuntimeValidationError),
                ):
                    runtime.validate_state_object(
                        Path(str(damaged["state_path"])), damaged
                    )

    def test_taskless_role_prompt_cannot_publish_task_or_evidence_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            state["roles"] = {
                "reviewer-a": _assignment(root, role="reviewer-a", kind="reviewer")
            }
            state["orca_result"] = _result(
                role="reviewer-a",
                role_kind="reviewer",
                logical_task_id="task-unknown",
            )
            with self.assertRaises(runtime.RuntimeValidationError):
                runtime.validate_state_object(Path(str(state["state_path"])), state)

            evidence = copy.deepcopy(state)
            evidence["orca_result"] = _result(
                role="reviewer-a",
                role_kind="reviewer",
                task_evidence={"task_id": "task-unknown"},
            )
            with self.assertRaises(runtime.RuntimeValidationError):
                runtime.validate_state_object(
                    Path(str(evidence["state_path"])), evidence
                )

    def test_failed_result_with_failed_question_outbox_can_remain_unnotified(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = _state(root)
            assignment = _assignment(root)
            assignment["acp_session_id"] = "session-1"
            assignment["orca_question"] = {
                "session_id": "session-1",
                "tool_call_id": "tool-1",
                "request": {
                    "kind": "question",
                    "session_id": "session-1",
                    "tool_call_id": "tool-1",
                    "questions": [
                        {"field": "question_0_custom", "body": "Choose yes."}
                    ],
                },
                "message_id": "message-1",
                "answer_message_id": None,
                "thread_id": None,
                "phase": "failed",
                "answers": None,
                "answer_sha256": None,
                "error": "provider stopped",
            }
            state["roles"] = {"worker-a": assignment}
            state["orca_result"] = _result(outcome="failed")
            state.update(
                {
                    "pending_delivery_id": "delivery-1",
                    "pending_delivery_kind": "question",
                    "pending_delivery_stage": "observed",
                    "pending_question_ids": ["message-1"],
                    "replied_question_ids": [],
                }
            )
            _assert_valid(state)

            unresolved = copy.deepcopy(state)
            unresolved["pending_delivery_stage"] = "invalid"
            unresolved["orca_result"] = _result(outcome="succeeded")
            with self.assertRaises(runtime.RuntimeValidationError):
                runtime.validate_state_object(
                    Path(str(unresolved["state_path"])), unresolved
                )

    def test_stop_requested_must_be_true(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in (False, "true", 1):
                state = _state(root)
                state["orca_stop_requested"] = value
                with (
                    self.subTest(value=value),
                    self.assertRaises(runtime.RuntimeValidationError),
                ):
                    runtime.validate_state_object(Path(str(state["state_path"])), state)


if __name__ == "__main__":
    unittest.main()
