from __future__ import annotations

import json
import unittest
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import cast
from unittest import mock

import test_named_orca_backend as fixture_support

from agent_team import backend as backend_module
from agent_team import (
    cli,
    mcp_server,
    native_acp_dependencies,
    orca_acp,
    orca_dispatch,
    orca_tasks,
    role_snapshot,
    scoped_acp,
    task_verification,
)
from agent_team.adapters import ProcessResult
from agent_team.contracts import ErrorCode, RuntimeFailure
from agent_team.orca import TerminalCloseVerdict
from agent_team.runtime import read_state


class _FakeNativeExecutables:
    agent = Path("/private/claude-agent")
    node = Path("/private/claude-node")
    sdk = Path("/private/claude-sdk")
    library = Path("/private/claude-library")

    def verify(self) -> None:
        return None

    def as_dict(self) -> dict[str, str]:
        return {"node": "selected-node"}


class _RecordingVerificationRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> ProcessResult:
        del env
        self.calls.append((tuple(argv), cwd, timeout_seconds))
        return ProcessResult(0, "verification ok\n", "")


class OrcaTaskPipelineIntegrationTest(unittest.TestCase):
    revision = "a" * 64

    def setUp(self) -> None:
        self.fixture = fixture_support.NamedOrcaBackendTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.start()
        self.path = self.fixture.spec.state_path
        self.task = self.fixture.spec.task_specs[0]
        self.dispatch_counter = 0
        self.delivery_counter = 0
        self.delivery_id = ""
        self.orca_calls: list[tuple[str, ...]] = []

        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(
            mock.patch.object(
                backend_module, "OrcaClient", return_value=self.fixture.client
            )
        )
        self.patches.enter_context(
            mock.patch.object(
                role_snapshot, "preflight_scoped_role", side_effect=self._preflight
            )
        )
        self.patches.enter_context(
            mock.patch.object(
                native_acp_dependencies.NativeAcpExecutables,
                "from_dict",
                return_value=_FakeNativeExecutables(),
            )
        )
        self.patches.enter_context(
            mock.patch.object(
                native_acp_dependencies,
                "adapter_snapshot",
                side_effect=self._adapter_snapshot,
            )
        )
        self.patches.enter_context(
            mock.patch.object(orca_dispatch, "checked_digest", return_value="a" * 64)
        )
        self.patches.enter_context(
            mock.patch.object(
                scoped_acp,
                "create_write_policy",
                side_effect=self._create_write_policy,
            )
        )
        self.patches.enter_context(
            mock.patch.object(cli, "acp_agent_command", return_value="claude-agent")
        )
        self.patches.enter_context(
            mock.patch.object(cli, "acp_runner_command", return_value="runner")
        )
        self.patches.enter_context(
            mock.patch.object(
                orca_tasks, "snapshot_revision", return_value=self.revision
            )
        )
        self.patches.enter_context(
            mock.patch.object(
                task_verification, "snapshot_revision", return_value=self.revision
            )
        )
        fake_orca = mock.patch.object(
            mcp_server, "run_orca", side_effect=self._run_orca
        )
        self.patches.enter_context(fake_orca)
        # orca_dispatch keeps a module-local alias for the same MCP boundary.
        self.patches.enter_context(
            mock.patch.object(orca_dispatch, "run_orca", side_effect=self._run_orca)
        )

        from agent_team.runtime_mcp import RuntimeMcpSession

        self.session = RuntimeMcpSession(self.path, read_state(self.path))

    @staticmethod
    def _preflight(
        _normalized: dict[str, object],
        _role: object,
        _workspace: Path,
        **_kwargs: object,
    ) -> None:
        return None

    @staticmethod
    def _adapter_snapshot(
        _executables: native_acp_dependencies.NativeAcpExecutables,
    ) -> dict[str, object]:
        return {
            "adapter_id": "claude-acp-scoped-0.70.0",
            "revision": "fixture-revision",
            "executable": "/private/claude-sdk",
            "version": "claude-fixture",
            "identity": {
                "device": 1,
                "inode": 2,
                "size": 3,
                "mtime_ns": 4,
                "sha256": "a" * 64,
            },
        }

    @staticmethod
    def _create_write_policy(
        private_root: Path,
        _workspace: Path,
        _state_path: Path,
        _task: object,
        _agent_entry: Path,
        *,
        permission: str,
    ) -> tuple[Path, str]:
        del permission
        policy = private_root / "write-policy.json"
        policy.write_text("{}", encoding="utf-8")
        return policy, "p" * 64

    def _run_orca(
        self,
        state: dict[str, object],
        args: list[str],
        **_kwargs: object,
    ) -> dict[str, object]:
        self.orca_calls.append(tuple(args))
        operation = tuple(args[:2])
        if operation == ("orchestration", "task-create"):
            return {"task": {"id": "task_worker"}}
        if operation == ("terminal", "create"):
            return {"terminal": {"handle": "term_planner"}}
        if operation == ("orchestration", "dispatch"):
            dispatch_id = f"dispatch-{self.dispatch_counter}"
            self.dispatch_counter += 1
            return {
                "injected": False,
                "dispatch": {
                    "id": dispatch_id,
                    "task_id": "task_worker",
                    "assignee_handle": "term_planner",
                    "run_id": "run_1",
                },
            }
        if operation == ("terminal", "send"):
            return {}
        if operation == ("orchestration", "check") and "--wait" in args:
            roles = state.get("roles")
            if not isinstance(roles, dict) or len(roles) != 1:
                raise AssertionError("wait requires one active named assignment")
            assignment = cast(dict[str, object], next(iter(roles.values())))
            result = state.get("orca_result")
            if not isinstance(result, Mapping):
                raise AssertionError("wait requires a published completion")
            return {
                "deliveryId": self.delivery_id,
                "messages": [
                    {
                        "id": f"message-{self.delivery_id}",
                        "run_id": "run_1",
                        "type": "worker_done",
                        "from_handle": assignment["terminal_handle"],
                        "body": result["body"],
                        "payload": {
                            "taskId": assignment["task_id"],
                            "dispatchId": assignment["dispatch_id"],
                            "outcome": result["outcome"],
                        },
                    }
                ],
            }
        if operation == ("orchestration", "worker-read"):
            dispatch = args[args.index("--dispatch") + 1]
            return {
                "dispatchId": dispatch,
                "source": "terminal",
                "sourceIdentity": "terminal-incarnation",
                "terminal": {
                    "handle": "term_planner",
                    "tail": ["trusted terminal tail"],
                },
            }
        if operation == ("orchestration", "worker-release"):
            dispatch = args[args.index("--dispatch") + 1]
            return {
                "dispatchId": dispatch,
                "state": "retained",
                "reason": "no_owned_resource",
                "processAction": "none",
                "archive": None,
            }
        if operation == ("orchestration", "check") and "--ack" in args:
            acknowledged = args[args.index("--ack") + 1]
            return {"acknowledged": acknowledged}
        raise AssertionError(f"unexpected fake Orca command: {args!r}")

    def _set_terminal_identity(self, role: str) -> None:
        self.fixture.client.terminal_result = {
            "terminal": {
                "handle": "term_planner",
                "worktreeId": "repo::project",
                "title": f"team-project-{role}",
                "worktreePath": str(self.fixture.spec.workspace),
            }
        }
        self.fixture.client.close_result = TerminalCloseVerdict(
            "term_planner", "tab", True, "exited"
        )

    def _dispatch(self, role: str, message: str) -> dict[str, object]:
        result = self.session.execute(
            "task_dispatch",
            {"role": role, "task": self.task.as_dict(), "message": message},
        )
        return cast(dict[str, object], result)

    def _publish(
        self,
        role: str,
        *,
        body: str,
        evidence: Mapping[str, object] | None = None,
        outcome: str = "succeeded",
    ) -> None:
        state = read_state(self.path)
        roles = cast(dict[str, object], state["roles"])
        assignment = cast(dict[str, object], roles[role])
        self.delivery_counter += 1
        self.delivery_id = f"delivery-{self.delivery_counter}"
        with mock.patch.object(orca_acp, "_send_worker_done"):
            orca_acp.publish_completion(
                self.path,
                role=role,
                role_kind=str(assignment["role_kind"]),
                run_id=str(state["run_id"]),
                task_id=str(assignment["task_id"]),
                dispatch_id=str(assignment["dispatch_id"]),
                terminal_handle=str(assignment["terminal_handle"]),
                launch_nonce=str(assignment["launch_nonce"]),
                outcome=outcome,
                body=body,
                cleanup_confirmed=True,
                task_evidence=evidence,
            )

    def _consume(self, role: str) -> dict[str, object]:
        self._set_terminal_identity(role)
        waited = self.session.execute("role_wait", {"role": role, "timeout_ms": 1_000})
        self.assertEqual(waited["delivery_id"], self.delivery_id)
        read = self.session.execute("role_read", {"role": role, "lines": 10})
        current = read_state(self.path)
        result = cast(dict[str, object], current["orca_result"])
        self.assertEqual(read["output"], result["body"])
        released = self.session.execute("role_release", {"role": role})
        self.assertEqual(released["state"], "released")
        acknowledged = self.session.execute(
            "delivery_ack", {"delivery_id": self.delivery_id}
        )
        self.assertEqual(acknowledged, {"acknowledged": True})
        return cast(dict[str, object], read_state(self.path))

    def _worker_to_reviewer(self) -> tuple[str, str, str]:
        self._dispatch("worker-a", "implement the declared task")
        worker_state = read_state(self.path)
        worker_roles = cast(dict[str, object], worker_state["roles"])
        worker_assignment = cast(dict[str, object], worker_roles["worker-a"])
        worker_dispatch = str(worker_assignment["dispatch_id"])
        self._publish("worker-a", body="worker completion")
        after_worker = self._consume("worker-a")
        worker_tasks = cast(dict[str, object], after_worker["tasks"])
        worker_record = cast(dict[str, object], worker_tasks[self.task.task_id])
        self.assertEqual(
            worker_record["status"],
            "awaiting_implementation_review",
        )

        self._dispatch("reviewer-a", "review the implementation")
        reviewer_state = read_state(self.path)
        reviewer_roles = cast(dict[str, object], reviewer_state["roles"])
        reviewer_assignment = cast(dict[str, object], reviewer_roles["reviewer-a"])
        reviewer_dispatch = str(reviewer_assignment["dispatch_id"])
        reviewer_tasks = cast(dict[str, object], reviewer_state["tasks"])
        record = cast(dict[str, object], reviewer_tasks[self.task.task_id])
        self.assertEqual(record["review_source_dispatch_id"], worker_dispatch)
        self.assertEqual(record["dispatch_id"], reviewer_dispatch)
        self.assertEqual(record["revision"], self.revision)
        self.assertEqual(reviewer_assignment["task_revision"], self.revision)
        return worker_dispatch, reviewer_dispatch, self.revision

    def _review_evidence(
        self, *, decision: str, revision: str, findings: list[str]
    ) -> dict[str, object]:
        return {
            "task_id": self.task.task_id,
            "stage": "implementation",
            "revision": revision,
            "decision": decision,
            "findings": findings,
        }

    def test_public_runtime_pipeline_reviewer_approval_verifies_same_revision_argv(
        self,
    ) -> None:
        worker_dispatch, reviewer_dispatch, revision = self._worker_to_reviewer()
        evidence = self._review_evidence(
            decision="approve", revision=revision, findings=[]
        )
        self._publish("reviewer-a", body=json.dumps(evidence), evidence=evidence)
        after_review = self._consume("reviewer-a")
        approved_tasks = cast(dict[str, object], after_review["tasks"])
        approved = cast(dict[str, object], approved_tasks[self.task.task_id])
        self.assertEqual(approved["status"], "implementation_approved")
        self.assertEqual(approved["review_source_dispatch_id"], worker_dispatch)
        self.assertEqual(approved["dispatch_id"], reviewer_dispatch)

        runner = _RecordingVerificationRunner()
        with mock.patch.object(task_verification, "ProcessRunner", return_value=runner):
            verified = self.session.execute(
                "task_verify", {"task_id": self.task.task_id}
            )

        self.assertEqual(verified["status"], "completed")
        record = cast(dict[str, object], verified["record"])
        self.assertEqual(record["status"], "completed")
        verification = cast(dict[str, object], record["verification"])
        self.assertEqual(verification["revision"], revision)
        self.assertTrue(verification["passed"])
        self.assertEqual(
            runner.calls,
            [(tuple(self.task.verification[0].argv), self.fixture.spec.workspace, 1)],
        )

    def test_public_runtime_pipeline_runs_real_fixed_argv(self) -> None:
        _worker_dispatch, _reviewer_dispatch, revision = self._worker_to_reviewer()
        evidence = self._review_evidence(
            decision="approve", revision=revision, findings=[]
        )
        self._publish("reviewer-a", body=json.dumps(evidence), evidence=evidence)
        self._consume("reviewer-a")

        verified = self.session.execute("task_verify", {"task_id": self.task.task_id})

        self.assertEqual(verified["status"], "completed")
        record = cast(dict[str, object], verified["record"])
        verification = cast(dict[str, object], record["verification"])
        self.assertEqual(verification["revision"], revision)
        self.assertTrue(verification["passed"])
        commands = cast(list[dict[str, object]], verification["commands"])
        self.assertEqual(commands[0]["argv"], ["/usr/bin/true"])
        self.assertEqual(commands[0]["returncode"], 0)

    def test_public_runtime_pipeline_answers_orca_consultation_through_cli(
        self,
    ) -> None:
        _worker_dispatch, _reviewer_dispatch, revision = self._worker_to_reviewer()
        evidence = self._review_evidence(
            decision="consult", revision=revision, findings=["user decision required"]
        )
        self._publish("reviewer-a", body=json.dumps(evidence), evidence=evidence)
        after_review = self._consume("reviewer-a")
        tasks = cast(dict[str, object], after_review["tasks"])
        record = cast(dict[str, object], tasks[self.task.task_id])
        self.assertEqual(record["status"], "consultation_required")

        task_result = self.session.execute("task_get", {"task_id": self.task.task_id})
        task_record = cast(dict[str, object], task_result["record"])
        consultation = cast(dict[str, object], task_record["consultation"])
        consultation_id = str(consultation["consultation_id"])
        plan = cli._management_plan_from_state(read_state(self.path))
        answered = cli.manage_team(
            "answer",
            plan,
            None,
            consultation_id=consultation_id,
            body="continue within the declared TaskSpec",
        )

        self.assertEqual(
            answered,
            {
                "status": "answered",
                "consultation_id": consultation_id,
                "task_id": self.task.task_id,
            },
        )
        saved = read_state(self.path)
        saved_tasks = cast(dict[str, object], saved["tasks"])
        answered_record = cast(dict[str, object], saved_tasks[self.task.task_id])
        self.assertEqual(answered_record["status"], "consultation_required")
        self.assertIn("consultation_answer", answered_record)

    def test_public_runtime_pipeline_rejects_stale_review_then_records_changes(
        self,
    ) -> None:
        worker_dispatch, reviewer_dispatch, revision = self._worker_to_reviewer()
        before = self.path.read_bytes()
        stale = self._review_evidence(
            decision="approve", revision="b" * 64, findings=[]
        )
        with self.assertRaises(RuntimeFailure) as raised:
            self._publish("reviewer-a", body=json.dumps(stale), evidence=stale)
        self.assertIs(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(self.path.read_bytes(), before)

        changes = self._review_evidence(
            decision="request_changes",
            revision=revision,
            findings=["implementation needs a correction"],
        )
        self._publish("reviewer-a", body=json.dumps(changes), evidence=changes)
        after_review = self._consume("reviewer-a")
        changed_tasks = cast(dict[str, object], after_review["tasks"])
        record = cast(dict[str, object], changed_tasks[self.task.task_id])
        self.assertEqual(record["status"], "implementation_changes_requested")
        self.assertEqual(record["review_source_dispatch_id"], worker_dispatch)
        self.assertEqual(record["dispatch_id"], reviewer_dispatch)


if __name__ == "__main__":
    unittest.main()
