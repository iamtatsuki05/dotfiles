from __future__ import annotations

import copy
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from agent_team import orca_acp
from agent_team.contracts import ErrorCode, NodeRef, Role, RuntimeFailure
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec
from agent_team.task_spec import TaskSpec, VerificationSpec


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="edit-one",
        objective="Edit the declared file.",
        acceptance_criteria=("The declared check passes.",),
        allowed_paths=("src/answer.py",),
        forbidden_paths=("src/protected.txt",),
        dependencies=(),
        verification=(VerificationSpec("check", ("python", "-V"), 10),),
        evidence_requirements=("command output",),
        consultation_conditions=(),
    )


def _graph() -> GraphSpec:
    return GraphSpec(
        nodes=(
            (main := NodeRef("lead", Role.MAIN)),
            (worker := NodeRef("worker-a", Role.WORKER)),
            (NodeRef("review-a", Role.REVIEWER)),
        ),
        edges=(
            GraphEdge(main.node_id, worker.node_id, "delegates-to"),
            GraphEdge(main.node_id, "review-a", "delegates-to"),
            GraphEdge(worker.node_id, "review-a", "reviewed-by"),
        ),
        coordination=Coordination("agent", (main.node_id,), "serial", 1),
        routes=(),
    )


class OrcaScopedPublicationTest(unittest.TestCase):
    def make_state(
        self,
        root: Path,
        *,
        role: str = "worker-a",
        role_kind: str = "worker",
        task: TaskSpec | None = None,
    ) -> tuple[dict[str, object], TaskSpec]:
        task = _task() if task is None else task
        workspace = root / "workspace"
        workspace.mkdir()
        state_path = root / "state" / "state.json"
        state_path.parent.mkdir(mode=0o700)
        assignment = {
            "role": role,
            "role_kind": role_kind,
            "task_id": "orca-task-1",
            "dispatch_id": "orca-dispatch-1",
            "terminal_handle": "orca-terminal-1",
            "launch_nonce": "nonce-1234",
            "launcher_owned_terminal": True,
            "execution": "background",
            "task_spec": task.as_dict(),
            "task_stage": "implementation",
            "task_revision": "revision-1",
        }
        state: dict[str, object] = {
            "version": 4,
            "runtime": "orca",
            "team_id": "orca-team",
            "workspace": str(workspace),
            "config_path": str(root / "config.toml"),
            "state_path": str(state_path),
            "launcher_path": str(root / "agent-team"),
            "worktree_id": "repo::workspace",
            "orca_socket": str(root / "orca.sock"),
            "run_id": "orca-run-1",
            "main_terminal": "orca-main-1",
            "graph": _graph().as_dict(),
            "max_review_rounds": 2,
            "task_specs": [task.as_dict()],
            "tasks": {},
            "role_specs": {
                "lead": {
                    "kind": "main",
                    "provider": "claude",
                    "transport": "direct",
                    "model": "fable",
                    "effort": "high",
                    "permission": "orchestrator",
                    "instructions": "main",
                    "execution": "tui_direct",
                },
                "worker-a": {
                    "kind": "worker",
                    "provider": "claude",
                    "transport": "acp",
                    "model": "fable",
                    "effort": "high",
                    "permission": "workspace-write",
                    "instructions": "worker",
                    "execution": "background",
                    "adapter_id": "claude-acp-scoped-0.70.0",
                },
                "review-a": {
                    "kind": "reviewer",
                    "provider": "claude",
                    "transport": "acp",
                    "model": "fable",
                    "effort": "high",
                    "permission": "read-only",
                    "instructions": "reviewer",
                    "execution": "background",
                    "adapter_id": "claude-acp-0.70.0",
                },
            },
            "roles": {role: assignment},
        }
        return state, task

    def publish(
        self,
        state: dict[str, object],
        *,
        outcome: str = "succeeded",
        cleanup_confirmed: bool = True,
        task_evidence: Mapping[str, object] | None = None,
        sender: mock.Mock | None = None,
        role: str = "worker-a",
        role_kind: str = "worker",
    ) -> tuple[str, dict[str, object], mock.Mock]:
        current = copy.deepcopy(state)
        saved: dict[str, object] = {}
        sender_mock = mock.Mock() if sender is None else sender

        def read(_path: Path) -> dict[str, object]:
            return copy.deepcopy(current)

        def write(
            _path: Path,
            value: dict[str, object],
            *,
            require_existing: bool,
            reservation_held: bool,
        ) -> None:
            self.assertTrue(require_existing)
            self.assertTrue(reservation_held)
            current.clear()
            current.update(copy.deepcopy(value))
            saved.clear()
            saved.update(copy.deepcopy(value))

        with (
            mock.patch.object(orca_acp, "read_state", side_effect=read),
            mock.patch.object(orca_acp, "write_state", side_effect=write),
            mock.patch.object(
                orca_acp, "validate_task_assignment", return_value=_task()
            ),
            mock.patch.object(orca_acp, "_send_worker_done", sender_mock),
        ):
            result = orca_acp.publish_completion(
                Path(str(state["state_path"])),
                role=role,
                role_kind=role_kind,
                run_id="orca-run-1",
                task_id="orca-task-1",
                dispatch_id="orca-dispatch-1",
                terminal_handle="orca-terminal-1",
                launch_nonce="nonce-1234",
                outcome=outcome,
                body="typed client result",
                cleanup_confirmed=cleanup_confirmed,
                task_evidence=task_evidence,
            )
        return result, saved, sender_mock

    def test_saved_orca_result_then_sends_one_remote_worker_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, task = self.make_state(Path(directory))
            result, saved, sender = self.publish(state)

        self.assertEqual(result, "succeeded")
        self.assertNotIn("native_result", saved)
        self.assertIn("orca_result", saved)
        result_value = saved["orca_result"]
        self.assertIsInstance(result_value, dict)
        assert isinstance(result_value, dict)
        self.assertEqual(result_value["role_kind"], "worker")
        self.assertEqual(result_value["task_id"], "orca-task-1")
        self.assertEqual(result_value["logical_task_id"], task.task_id)
        self.assertNotIn("delivery_id", result_value)
        sender.assert_called_once()
        self.assertEqual(sender.call_args.kwargs["outcome"], "succeeded")
        self.assertEqual(sender.call_args.kwargs["body"], "typed client result")

    def test_failed_typed_result_stays_failed_after_cleanup_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, _task_value = self.make_state(Path(directory))
            result, saved, sender = self.publish(
                state, outcome="failed", cleanup_confirmed=True
            )

        self.assertEqual(result, "failed")
        result_value = saved["orca_result"]
        self.assertIsInstance(result_value, dict)
        assert isinstance(result_value, dict)
        self.assertEqual(result_value["outcome"], "failed")
        sender.assert_called_once()
        self.assertEqual(sender.call_args.kwargs["outcome"], "failed")

    def test_selected_codex_scoped_profile_uses_the_same_publication_contract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, _task_value = self.make_state(Path(directory))
            role_specs = state["role_specs"]
            assert isinstance(role_specs, dict)
            role_spec = role_specs["worker-a"]
            assert isinstance(role_spec, dict)
            role_spec["provider"] = "codex"
            role_spec["adapter_id"] = "codex-acp-scoped-1.10.0"
            result, saved, sender = self.publish(state)

        self.assertEqual(result, "succeeded")
        self.assertIn("orca_result", saved)
        sender.assert_called_once()

    def test_unconfirmed_cleanup_saves_failed_result_without_remote_send(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, _task_value = self.make_state(Path(directory))
            result, saved, sender = self.publish(
                state, cleanup_confirmed=False, outcome="succeeded"
            )

        self.assertEqual(result, "failed")
        result_value = saved["orca_result"]
        self.assertIsInstance(result_value, dict)
        assert isinstance(result_value, dict)
        self.assertEqual(result_value["outcome"], "failed")
        self.assertFalse(result_value["cleanup_confirmed"])
        sender.assert_not_called()

    def test_duplicate_completion_is_rejected_before_state_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, _task_value = self.make_state(Path(directory))
            state["orca_result"] = {"existing": True}
            with (
                mock.patch.object(orca_acp, "read_state", return_value=state),
                mock.patch.object(orca_acp, "write_state") as write,
                mock.patch.object(orca_acp, "_send_worker_done") as sender,
                self.assertRaises(RuntimeFailure) as raised,
            ):
                orca_acp.publish_completion(
                    Path(str(state["state_path"])),
                    role="worker-a",
                    role_kind="worker",
                    run_id="orca-run-1",
                    task_id="orca-task-1",
                    dispatch_id="orca-dispatch-1",
                    terminal_handle="orca-terminal-1",
                    launch_nonce="nonce-1234",
                    outcome="succeeded",
                    body="typed client result",
                    cleanup_confirmed=True,
                )

        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        write.assert_not_called()
        sender.assert_not_called()

    def test_stale_run_is_rejected_before_state_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, _task_value = self.make_state(Path(directory))
            result, _saved, sender = self.publish_with_state_override(
                state, run_id="other-run"
            )

        self.assertEqual(result.code, ErrorCode.IDENTITY_MISMATCH)
        sender.assert_not_called()

    def test_tampered_reviewer_evidence_is_rejected_before_state_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, _task_value = self.make_state(
                Path(directory), role="review-a", role_kind="reviewer"
            )
            roles = state["roles"]
            assert isinstance(roles, dict)
            assignment = roles["review-a"]
            assert isinstance(assignment, dict)
            assignment["task_stage"] = "implementation"
            assignment["task_revision"] = "revision-1"
            evidence = {
                "task_id": "different-task",
                "stage": "implementation",
                "revision": "revision-1",
                "decision": "approve",
                "findings": [],
            }
            with self.assertRaises(RuntimeFailure) as raised:
                self.publish(
                    state,
                    role="review-a",
                    role_kind="reviewer",
                    task_evidence=evidence,
                )

        self.assertIn("review", str(raised.exception))

    def test_unknown_remote_send_keeps_result_and_rejects_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, _task_value = self.make_state(Path(directory))
            current = copy.deepcopy(state)
            saved: dict[str, object] = {}
            sender = mock.Mock(side_effect=RuntimeError("remote effect unknown"))

            def read(_path: Path) -> dict[str, object]:
                return copy.deepcopy(current)

            def write(
                _path: Path,
                value: dict[str, object],
                *,
                require_existing: bool,
                reservation_held: bool,
            ) -> None:
                del require_existing, reservation_held
                current.clear()
                current.update(copy.deepcopy(value))
                saved.clear()
                saved.update(copy.deepcopy(value))

            with (
                mock.patch.object(orca_acp, "read_state", side_effect=read),
                mock.patch.object(orca_acp, "write_state", side_effect=write),
                mock.patch.object(
                    orca_acp, "validate_task_assignment", return_value=_task()
                ),
                mock.patch.object(orca_acp, "_send_worker_done", sender),
                self.assertRaises(RuntimeError),
            ):
                orca_acp.publish_completion(
                    Path(str(state["state_path"])),
                    role="worker-a",
                    role_kind="worker",
                    run_id="orca-run-1",
                    task_id="orca-task-1",
                    dispatch_id="orca-dispatch-1",
                    terminal_handle="orca-terminal-1",
                    launch_nonce="nonce-1234",
                    outcome="succeeded",
                    body="typed client result",
                    cleanup_confirmed=True,
                )

            with (
                mock.patch.object(orca_acp, "read_state", side_effect=read),
                mock.patch.object(orca_acp, "write_state") as retry_write,
                mock.patch.object(orca_acp, "_send_worker_done") as retry_sender,
                self.assertRaises(RuntimeFailure),
            ):
                orca_acp.publish_completion(
                    Path(str(state["state_path"])),
                    role="worker-a",
                    role_kind="worker",
                    run_id="orca-run-1",
                    task_id="orca-task-1",
                    dispatch_id="orca-dispatch-1",
                    terminal_handle="orca-terminal-1",
                    launch_nonce="nonce-1234",
                    outcome="succeeded",
                    body="typed client result",
                    cleanup_confirmed=True,
                )

        self.assertIn("orca_result", saved)
        self.assertEqual(sender.call_count, 1)
        retry_write.assert_not_called()
        retry_sender.assert_not_called()

    def publish_with_state_override(
        self, state: dict[str, object], *, run_id: str
    ) -> tuple[RuntimeFailure, dict[str, object], mock.Mock]:
        current = copy.deepcopy(state)
        current["run_id"] = run_id
        sender = mock.Mock()
        with (
            mock.patch.object(orca_acp, "read_state", return_value=current),
            mock.patch.object(orca_acp, "_send_worker_done", sender),
            self.assertRaises(RuntimeFailure) as raised,
        ):
            orca_acp.publish_completion(
                Path(str(state["state_path"])),
                role="worker-a",
                role_kind="worker",
                run_id="orca-run-1",
                task_id="orca-task-1",
                dispatch_id="orca-dispatch-1",
                terminal_handle="orca-terminal-1",
                launch_nonce="nonce-1234",
                outcome="succeeded",
                body="typed client result",
                cleanup_confirmed=True,
            )
        return raised.exception, {}, sender


if __name__ == "__main__":
    unittest.main()
