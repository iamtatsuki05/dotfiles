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
from agent_team.scoped_acp import native_profile
from agent_team.task_spec import TaskSpec, VerificationSpec


def _task() -> TaskSpec:
    return TaskSpec(
        "edit-one",
        "Edit the declared file.",
        ("The check passes.",),
        ("src/answer.py",),
        ("src/protected.txt",),
        (),
        (VerificationSpec("check", ("python", "-V"), 10),),
        ("command output",),
        (),
    )


def _graph() -> GraphSpec:
    return GraphSpec(
        nodes=(
            NodeRef("lead", Role.MAIN),
            NodeRef("worker-a", Role.WORKER),
            NodeRef("review-a", Role.REVIEWER),
        ),
        edges=(
            GraphEdge("lead", "worker-a", "delegates-to"),
            GraphEdge("lead", "review-a", "delegates-to"),
            GraphEdge("worker-a", "review-a", "reviewed-by"),
        ),
        coordination=Coordination("agent", ("lead",), "serial", 1),
        routes=(),
    )


class OrcaPublicationLifecycleTest(unittest.TestCase):
    def state(
        self,
        root: Path,
        *,
        role: str,
        kind: str,
        task: bool,
    ) -> dict[str, object]:
        task_value = _task()
        state_path = root / "state" / "state.json"
        state_path.parent.mkdir(mode=0o700)
        (root / "workspace").mkdir()
        assignment: dict[str, object] = {
            "role": role,
            "role_kind": kind,
            "task_id": "orca-task-1",
            "dispatch_id": "orca-dispatch-1",
            "terminal_handle": "orca-terminal-1",
            "launch_nonce": "nonce-1234",
            "launcher_owned_terminal": True,
            "completion_observed": False,
            "execution": "background",
        }
        if task:
            assignment.update(
                task_spec=task_value.as_dict(),
                task_stage="implementation",
                task_revision="revision-1",
            )
        role_specs: dict[str, dict[str, object]] = {
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
                **native_profile("claude", "worker"),
                "model": "fable",
                "instructions": "worker",
            },
            "review-a": {
                "kind": "reviewer",
                **native_profile("claude", "reviewer"),
                "model": "fable",
                "instructions": "reviewer",
            },
        }
        return {
            "version": 4,
            "runtime": "orca",
            "team_id": "orca-team",
            "workspace": str(root / "workspace"),
            "config_path": str(root / "config.toml"),
            "state_path": str(state_path),
            "launcher_path": str(root / "agent-team"),
            "worktree_id": "repo::workspace",
            "orca_socket": str(root / "orca.sock"),
            "run_id": "orca-run-1",
            "main_terminal": "orca-main-1",
            "graph": _graph().as_dict(),
            "max_review_rounds": 2,
            "task_specs": [task_value.as_dict()],
            "tasks": {},
            "role_specs": role_specs,
            "roles": {role: assignment},
        }

    def publish(
        self,
        state: dict[str, object],
        *,
        outcome: str = "succeeded",
        cleanup_confirmed: bool = True,
        evidence: Mapping[str, object] | None = None,
    ) -> tuple[str, dict[str, object], mock.Mock, list[str]]:
        current = copy.deepcopy(state)
        saved: dict[str, object] = {}
        events: list[str] = []
        sender = mock.Mock()
        path = Path(str(state["state_path"]))
        roles = state["roles"]
        assert isinstance(roles, dict)
        role = next(iter(roles))
        assignment = roles[role]
        assert isinstance(assignment, dict)

        def read(_path: Path) -> dict[str, object]:
            return copy.deepcopy(current)

        def write(
            _path: Path,
            value: dict[str, object],
            *,
            require_existing: bool,
            reservation_held: bool,
        ) -> None:
            self.assertTrue(require_existing and reservation_held)
            events.append("save")
            current.clear()
            current.update(copy.deepcopy(value))
            saved.clear()
            saved.update(copy.deepcopy(value))

        def send(
            _state: dict[str, object],
            _assignment: dict[str, object],
            *,
            outcome: str,
            body: str,
        ) -> None:
            del outcome, body
            probe = orca_acp._LifecycleReservation(path, create_parent=False)
            probe.acquire()
            probe.release()
            events.append("send")
            sender(_state, _assignment)

        with (
            mock.patch.object(orca_acp, "read_state", side_effect=read),
            mock.patch.object(orca_acp, "write_state", side_effect=write),
            mock.patch.object(
                orca_acp,
                "validate_task_assignment",
                side_effect=lambda _state, value: (
                    _task() if "task_spec" in value else None
                ),
            ),
            mock.patch.object(orca_acp, "_send_worker_done", side_effect=send),
        ):
            result = orca_acp.publish_completion(
                path,
                role=role,
                role_kind=str(assignment["role_kind"]),
                run_id="orca-run-1",
                task_id="orca-task-1",
                dispatch_id="orca-dispatch-1",
                terminal_handle="orca-terminal-1",
                launch_nonce="nonce-1234",
                outcome=outcome,
                body="typed result",
                cleanup_confirmed=cleanup_confirmed,
                task_evidence=evidence,
            )
        return result, saved, sender, events

    def test_taskless_readonly_reviewer_is_sent_without_task_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, saved, sender, events = self.publish(
                self.state(
                    Path(directory), role="review-a", kind="reviewer", task=False
                )
            )
        self.assertEqual(result, "succeeded")
        result_value = saved["orca_result"]
        assert isinstance(result_value, dict)
        self.assertNotIn("logical_task_id", result_value)
        self.assertNotIn("task_evidence", result_value)
        sender.assert_called_once()
        self.assertEqual(events, ["save", "send"])

    def test_failed_typed_result_stays_failed_after_cleanup_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, saved, sender, _events = self.publish(
                self.state(Path(directory), role="worker-a", kind="worker", task=True),
                outcome="failed",
            )
        self.assertEqual(result, "failed")
        result_value = saved["orca_result"]
        assert isinstance(result_value, dict)
        self.assertEqual(result_value["outcome"], "failed")
        sender.assert_called_once()

    def test_selected_codex_scoped_profile_uses_shared_native_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(
                Path(directory), role="worker-a", kind="worker", task=True
            )
            role_specs = state["role_specs"]
            assert isinstance(role_specs, dict)
            worker_spec = role_specs["worker-a"]
            assert isinstance(worker_spec, dict)
            worker_spec.update(native_profile("codex", "worker"))
            result, saved, sender, _events = self.publish(state)
        self.assertEqual(result, "succeeded")
        self.assertIn("orca_result", saved)
        sender.assert_called_once()

    def test_worker_without_task_spec_is_rejected_before_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(
                Path(directory), role="worker-a", kind="worker", task=False
            )
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
                    body="typed result",
                    cleanup_confirmed=True,
                )
        self.assertEqual(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        write.assert_not_called()
        sender.assert_not_called()

    def test_cleanup_unconfirmed_reviewer_drops_evidence_and_send(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(
                Path(directory), role="review-a", kind="reviewer", task=True
            )
            evidence = {
                "task_id": "edit-one",
                "stage": "implementation",
                "revision": "revision-1",
                "decision": "approve",
                "findings": [],
            }
            result, saved, sender, _events = self.publish(
                state, cleanup_confirmed=False, evidence=evidence
            )
        self.assertEqual(result, "failed")
        result_value = saved["orca_result"]
        assert isinstance(result_value, dict)
        self.assertEqual(result_value["outcome"], "failed")
        self.assertNotIn("task_evidence", result_value)
        sender.assert_not_called()

    def test_stop_and_question_pending_force_failed_without_send(self) -> None:
        for field in ("orca_stop_requested", "orca_question"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                state = self.state(
                    Path(directory), role="worker-a", kind="worker", task=True
                )
                if field == "orca_stop_requested":
                    state[field] = True
                else:
                    roles = state["roles"]
                    assert isinstance(roles, dict)
                    assignment = roles["worker-a"]
                    assert isinstance(assignment, dict)
                    assignment[field] = {"phase": "asking"}
                result, saved, sender, _events = self.publish(state)
            self.assertEqual(result, "failed")
            result_value = saved["orca_result"]
            assert isinstance(result_value, dict)
            self.assertEqual(result_value["outcome"], "failed")
            sender.assert_not_called()

    def test_recorded_question_outbox_is_removed_in_completion_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(
                Path(directory), role="worker-a", kind="worker", task=True
            )
            roles = state["roles"]
            assert isinstance(roles, dict)
            assignment = roles["worker-a"]
            assert isinstance(assignment, dict)
            assignment["acp_session_id"] = "session-1"
            assignment["orca_question"] = {
                "session_id": "session-1",
                "phase": "recorded",
            }
            result, saved, sender, _events = self.publish(state)

        self.assertEqual(result, "succeeded")
        saved_roles = saved["roles"]
        assert isinstance(saved_roles, dict)
        saved_assignment = saved_roles["worker-a"]
        assert isinstance(saved_assignment, dict)
        self.assertNotIn("orca_question", saved_assignment)
        self.assertEqual(saved_assignment["acp_session_id"], "session-1")
        sender.assert_called_once()

    def test_received_question_outbox_is_retained_with_failed_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(
                Path(directory), role="worker-a", kind="worker", task=True
            )
            roles = state["roles"]
            assert isinstance(roles, dict)
            assignment = roles["worker-a"]
            assert isinstance(assignment, dict)
            assignment["acp_session_id"] = "session-1"
            assignment["orca_question"] = {
                "session_id": "session-1",
                "phase": "received",
            }
            result, saved, sender, _events = self.publish(state)

        self.assertEqual(result, "failed")
        saved_roles = saved["roles"]
        assert isinstance(saved_roles, dict)
        saved_assignment = saved_roles["worker-a"]
        assert isinstance(saved_assignment, dict)
        self.assertEqual(saved_assignment["orca_question"]["phase"], "received")
        self.assertEqual(saved_assignment["acp_session_id"], "session-1")
        sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
