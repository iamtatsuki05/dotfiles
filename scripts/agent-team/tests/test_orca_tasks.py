from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_named_orca_backend as fixture_support
from test_named_orca_state import _assignment

from agent_team import backend as backend_module
from agent_team import cli, orca_acp
from agent_team import orca_tasks as runtime
from agent_team.contracts import (
    DeliveryAck,
    DeliveryRef,
    ErrorCode,
    NodeRef,
    Role,
    RoleRead,
    RoleRelease,
    RoleWait,
    RuntimeFailure,
    TaskDispatch,
    TaskGet,
)
from agent_team.locking import _LifecycleReservation
from agent_team.orca import TerminalCloseVerdict
from agent_team.runtime import create_prompt_file, read_state, write_state
from agent_team.runtime_mcp import RuntimeMcpSession
from agent_team.task_execution import prepare_dispatch


class OrcaTasksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture_support.NamedOrcaBackendTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        state = self.fixture.start()
        self.backend = self.fixture.backend
        self.path = self.fixture.spec.state_path
        self.role = NodeRef("worker-a", Role.WORKER)
        self.task = self.fixture.spec.task_specs[0]
        record, prompt = prepare_dispatch(
            state, TaskDispatch(self.role, self.task, "実装してください")
        )
        assignment = _assignment(self.fixture.root)
        assignment.update(
            {
                "task_id": "task_worker",
                "dispatch_id": "dispatch_1",
                "terminal_handle": "term_planner",
                "task_spec": self.task.as_dict(),
                "task_stage": record["stage"],
                "task_revision": record["revision"],
            }
        )
        record["dispatch_id"] = assignment["dispatch_id"]
        assignment["prompt_path"] = str(
            create_prompt_file(
                self.path.parent, self.role, str(assignment["launch_nonce"]), prompt
            )
        )
        for field, prefix in (
            ("provider_private_root", "agent-team-provider-"),
            ("snapshot_root", "agent-team-snapshot-"),
        ):
            temporary = tempfile.TemporaryDirectory(prefix=prefix)
            self.addCleanup(temporary.cleanup)
            assignment[field] = str(Path(temporary.name).resolve())
        state["roles"] = {self.role.node_id: assignment}
        self.assignment = assignment
        write_state(self.path, state, require_existing=True)
        self.state = state
        self.fixture.client.terminal_result = {
            "terminal": {
                "handle": "term_planner",
                "worktreeId": "repo::project",
                "title": "team-project-worker-a",
                "worktreePath": str(self.fixture.spec.workspace),
            }
        }
        self.fixture.client.close_result = TerminalCloseVerdict(
            "term_planner", "tab", True, "exited"
        )
        self.remote_calls: list[str] = []
        patch = mock.patch.object(runtime.remote, "run_orca", side_effect=self.remote)
        patch.start()
        self.addCleanup(patch.stop)

    def publish(self, *, outcome: str = "succeeded", cleanup: bool = True) -> None:
        with mock.patch.object(orca_acp, "_send_worker_done"):
            orca_acp.publish_completion(
                self.path,
                role=self.role.node_id,
                role_kind="worker",
                run_id="run_1",
                task_id="task_worker",
                dispatch_id="dispatch_1",
                terminal_handle="term_planner",
                launch_nonce=str(self.assignment["launch_nonce"]),
                outcome=outcome,
                body="trusted full client output",
                cleanup_confirmed=cleanup,
            )

    def remote(self, state, args, **_kwargs):
        reservation = _LifecycleReservation(self.path)
        if "--ack" in args:
            with self.assertRaises(RuntimeFailure):
                reservation.acquire()
        else:
            reservation.acquire()
            reservation.release()
        operation = args[1]
        self.remote_calls.append(operation)
        if operation == "check" and "--wait" in args:
            if "orca_result" not in read_state(self.path):
                self.publish()
            return {
                "deliveryId": "delivery_1",
                "messages": [
                    {
                        "id": "message_1",
                        "run_id": "run_1",
                        "type": "worker_done",
                        "from_handle": "term_planner",
                        "body": "trusted full client output",
                        "payload": {
                            "taskId": "task_worker",
                            "dispatchId": "dispatch_1",
                            "outcome": "succeeded",
                        },
                    }
                ],
            }
        if operation == "worker-read":
            return {
                "dispatchId": "dispatch_1",
                "source": "terminal",
                "sourceIdentity": "terminal_incarnation",
                "terminal": {
                    "handle": "term_planner",
                    "tail": ["untrusted screen output"],
                },
            }
        if operation == "worker-release":
            return {
                "dispatchId": "dispatch_1",
                "state": "retained",
                "reason": "no_owned_resource",
                "processAction": "none",
                "archive": None,
            }
        if operation == "check" and "--ack" in args:
            self.assertEqual(args[args.index("--ack") + 1], "delivery_1")
            current = read_state(self.path)
            self.assertEqual(current["pending_delivery_stage"], "released")
            self.assertEqual(current["tasks"][self.task.task_id]["status"], "running")
            return {"acknowledged": "delivery_1"}
        raise AssertionError(f"unexpected remote operation: {args!r}")

    def observe(self):
        return self.backend.request(RoleWait(self.role, 1000))

    def test_mcp_reads_saved_task_without_resolving_configuration_or_dependencies(self):
        before = list(self.fixture.client.calls)
        with (
            mock.patch.object(
                backend_module, "OrcaClient", return_value=self.fixture.client
            ),
            mock.patch.object(
                cli,
                "_start_prerequisites",
                side_effect=AssertionError("dependency probe"),
            ),
            mock.patch.object(
                cli, "load_config", side_effect=AssertionError("configuration read")
            ),
        ):
            session = RuntimeMcpSession(self.path, read_state(self.path))
            result = session.execute("task_get", {"task_id": self.task.task_id})
        self.assertEqual(result["task_id"], self.task.task_id)
        self.assertEqual(result["status"], "running")
        self.assertEqual(self.fixture.client.calls, before)

    def test_worker_completion_uses_trusted_body_and_remote_identity_in_exact_order(
        self,
    ):
        receipt = self.observe()
        self.assertEqual(receipt.delivery_id, DeliveryRef("delivery_1"))
        self.assertEqual(receipt.events[0].identity.task_id._value, "task_worker")
        state = read_state(self.path)
        self.assertEqual(state["orca_result"]["delivery_id"], "delivery_1")
        for early in (RoleRelease(self.role), DeliveryAck(DeliveryRef("delivery_1"))):
            with self.assertRaises(RuntimeFailure):
                self.backend.request(early)
        self.assertEqual(
            self.backend.request(RoleRead(self.role, 1)).output,
            "trusted full client output",
        )
        self.assertEqual(self.backend.request(RoleRelease(self.role)).state, "released")
        state = read_state(self.path)
        self.assertEqual(state["roles"], {})
        self.assertEqual(state["orca_release"]["phase"], "released")
        self.assertEqual(
            state["orca_release"]["terminal_close"]["handle"], "term_planner"
        )
        self.assertTrue(
            self.backend.request(DeliveryAck(DeliveryRef("delivery_1"))).acknowledged
        )
        state = read_state(self.path)
        self.assertEqual(
            state["tasks"][self.task.task_id]["status"],
            "awaiting_implementation_review",
        )
        self.assertNotIn("orca_result", state)
        self.assertNotIn("orca_release", state)
        self.assertNotIn("pending_delivery_id", state)
        self.assertEqual(
            self.remote_calls, ["check", "worker-read", "worker-release", "check"]
        )
        for field in ("prompt_path", "provider_private_root", "snapshot_root"):
            self.assertFalse(Path(self.assignment[field]).exists())

    def test_malformed_completion_retains_delivery_and_trusted_result(self):
        self.publish()
        original = self.remote

        def mismatch(state, args, **kwargs):
            response = original(state, args, **kwargs)
            response["messages"][0]["body"] = "different provider claim"
            return response

        with (
            mock.patch.object(runtime.remote, "run_orca", side_effect=mismatch),
            self.assertRaises(RuntimeFailure),
        ):
            self.observe()
        state = read_state(self.path)
        self.assertEqual(state["pending_delivery_stage"], "invalid")
        self.assertEqual(state["orca_result"]["body"], "trusted full client output")
        self.assertNotIn("delivery_id", state["orca_result"])
        self.assertFalse(state["roles"][self.role.node_id]["completion_observed"])

    def test_changed_context_terminal_identity_is_rejected_before_wait(self):
        original = self.fixture.client.worker_show

        def changed(**kwargs):
            response = original(**kwargs)
            response["observation"]["exactWorker"] = False
            return response

        self.fixture.client.worker_show = changed
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeFailure):
            self.observe()
        self.assertEqual(self.remote_calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_wait_rechecks_saved_run_after_remote_call(self):
        original = self.remote

        def replace_run(state, args, **kwargs):
            response = original(state, args, **kwargs)
            changed = read_state(self.path)
            changed["run_id"] = "different_run"
            changed.pop("orca_result")
            write_state(self.path, changed, require_existing=True)
            return response

        with (
            mock.patch.object(runtime.remote, "run_orca", side_effect=replace_run),
            self.assertRaises(RuntimeFailure) as failure,
        ):
            self.observe()
        self.assertEqual(failure.exception.code, ErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(read_state(self.path)["run_id"], "different_run")
        self.assertNotIn("pending_delivery_id", read_state(self.path))

    def test_read_failure_preserves_observed_state(self):
        self.observe()
        before = self.path.read_bytes()
        with (
            mock.patch.object(
                runtime.remote, "run_orca", return_value={"dispatchId": "other"}
            ),
            self.assertRaises(RuntimeError),
        ):
            self.backend.request(RoleRead(self.role, 5))
        self.assertEqual(self.path.read_bytes(), before)

    def test_close_unknown_retains_ownership_and_blocks_release_retry(self):
        self.observe()
        self.backend.request(RoleRead(self.role, 5))
        self.fixture.client.close_error = RuntimeError("close response lost")
        with self.assertRaises(RuntimeError):
            self.backend.request(RoleRelease(self.role))
        state = read_state(self.path)
        self.assertEqual(state["orca_release"]["phase"], "closing")
        self.assertEqual(state["pending_delivery_stage"], "read")
        before = list(self.fixture.client.calls)
        with self.assertRaises(RuntimeFailure):
            self.backend.request(RoleRelease(self.role))
        self.assertEqual(self.fixture.client.calls, before)
        self.assertTrue(Path(self.assignment["prompt_path"]).exists())

    def test_ack_failure_keeps_task_unconsumed_and_released_evidence(self):
        self.observe()
        self.backend.request(RoleRead(self.role, 5))
        self.backend.request(RoleRelease(self.role))
        before = read_state(self.path)
        with (
            mock.patch.object(
                runtime.remote, "run_orca", return_value={"acknowledged": "other"}
            ),
            self.assertRaises(RuntimeFailure),
        ):
            self.backend.request(DeliveryAck(DeliveryRef("delivery_1")))
        after = read_state(self.path)
        intent = after.pop("pending_orca_effect")
        self.assertEqual(intent["operation"], "ack")
        self.assertEqual(intent["delivery_id"], "delivery_1")
        self.assertEqual(after, before)
        record = self.backend.request(TaskGet(self.task.task_id))
        self.assertEqual(record.status, "running")
        self.assertEqual(record.record["dispatch_id"], "dispatch_1")
