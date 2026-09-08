from __future__ import annotations

import copy
import importlib
import io
import unittest
from contextlib import redirect_stdout

import test_program_wave as waves

from agent_team import contracts as c
from agent_team import task_execution as tasks
from agent_team.task_spec import TaskSpec


class CanonicalBackend:
    def __init__(self):
        self.state, self.catalog = waves.state_fixture()
        self.calls = []
        self.snapshots = 0

    def program_snapshot(self):
        self.snapshots += 1
        if self.snapshots > 100:
            raise AssertionError("program did not make bounded progress")
        tasks.validate_saved_tasks(self.state)
        return copy.deepcopy(self.state)

    def program_transition(self, action):
        self.calls.append(action)
        tasks.transition_program_wave(
            self.state, action, revision="a" * 64 if action == "seal_wave" else None
        )

    def request(self, request):
        self.calls.append(type(request).__name__)
        if isinstance(request, c.TaskDispatch):
            record, _ = tasks.prepare_dispatch(
                self.state,
                request,
                revision=self.state["program_wave"]["revision"]
                if request.role.kind is c.Role.REVIEWER
                else None,
            )
            dispatch = f"dispatch-{len(self.calls)}"
            record["dispatch_id"] = dispatch
            self.state["roles"] = {
                request.role.node_id: {
                    "role": request.role.node_id,
                    "role_kind": request.role.kind.value,
                    "task_id": request.task.task_id,
                    "dispatch_id": dispatch,
                }
            }
            return None
        if isinstance(request, c.RoleWait):
            assignment = self.state["roles"][request.role.node_id]
            task = next(
                task for task in self.catalog if task.task_id == assignment["task_id"]
            )
            record = self.state["tasks"][task.task_id]
            result = waves.routing._result(
                dispatch_id=assignment["dispatch_id"], target=request.role
            )
            if request.role.kind is c.Role.REVIEWER:
                result["task_evidence"] = waves.routing._review(
                    task, stage="implementation", revision=record["revision"]
                )
            self.state.update(
                native_result=result,
                pending_delivery_id=assignment["dispatch_id"],
                pending_delivery_kind="worker_done",
                pending_delivery_stage="observed",
            )
            delivery = c.DeliveryRef(assignment["dispatch_id"])
            event = c.NormalizedEvent.worker_done(
                identity=c.CompletionIdentity(
                    c.RunRef("run"),
                    c.TaskRef(task.task_id),
                    c.DispatchRef(assignment["dispatch_id"]),
                    c.TerminalRef("terminal"),
                ),
                outcome=c.Outcome.SUCCEEDED,
                body="provider result",
                delivery_id=delivery,
            )
            return c.WaitReceipt(delivery, (event,))
        if isinstance(request, c.RoleRead):
            assert self.state["pending_delivery_stage"] == "observed"
            self.state["pending_delivery_stage"] = "read"
            return c.ReadReceipt("provider result")
        if isinstance(request, c.RoleRelease):
            assert self.state["pending_delivery_stage"] == "read"
            self.state["roles"] = {}
            self.state["pending_delivery_stage"] = "released"
            return c.ReleaseReceipt("released")
        if isinstance(request, c.DeliveryAck):
            assert self.state["pending_delivery_stage"] == "released"
            tasks.acknowledge_task(self.state, self.state.pop("native_result"))
            for key in (
                "pending_delivery_id",
                "pending_delivery_kind",
                "pending_delivery_stage",
            ):
                self.state.pop(key)
            return c.AckReceipt(True)
        if isinstance(request, c.TaskVerify):
            record = self.state["tasks"][request.task_id]
            waves.verification_result(self.state, TaskSpec.from_dict(record["spec"]))
            return c.TaskStatusReceipt(request.task_id, "completed", record)
        raise AssertionError(f"unexpected request {request}")


class NativeProgramTest(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module("agent_team.native_program")
        except ModuleNotFoundError:
            self.fail("native program coordinator entrypoint is not implemented")

    def test_program_drives_canonical_tasks_through_all_waves_without_main(self):
        module = self.module()
        backend = CanonicalBackend()
        with redirect_stdout(io.StringIO()):
            result = module.drive(backend)
        self.assertEqual(result, 0)
        self.assertTrue(
            all(
                record["status"] == "completed"
                for record in backend.state["tasks"].values()
            )
        )
        self.assertEqual(backend.state["roles"], {})
        self.assertNotIn("pending_delivery_id", backend.state)
        self.assertEqual(backend.calls.count("TaskDispatch"), 6)
        self.assertEqual(backend.calls.count("TaskVerify"), 3)
        self.assertEqual(backend.calls.count("next_wave"), 1)
        for index, call in enumerate(backend.calls):
            if call == "RoleWait":
                self.assertEqual(
                    backend.calls[index : index + 4],
                    ["RoleWait", "RoleRead", "RoleRelease", "DeliveryAck"],
                )

    def test_failed_read_keeps_delivery_and_prevents_release_or_ack(self):
        module = self.module()

        class FailedRead(CanonicalBackend):
            def request(self, request):
                if isinstance(request, c.RoleRead):
                    raise c.RuntimeFailure(
                        c.ErrorCode.BACKEND_PROTOCOL_FAILURE, "read failed"
                    )
                return super().request(request)

        backend = FailedRead()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(module.drive(backend), 1)
        self.assertTrue(backend.state["roles"])
        self.assertEqual(backend.state["pending_delivery_stage"], "observed")
        self.assertNotIn("RoleRelease", backend.calls)
        self.assertNotIn("DeliveryAck", backend.calls)
