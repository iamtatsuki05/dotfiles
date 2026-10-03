from __future__ import annotations

import hashlib
import subprocess
import sys
import unittest
from dataclasses import replace
from unittest import mock

import test_native_backend as support

from agent_team import native_backend as native
from agent_team.contracts import (
    DeliveryAck,
    NodeRef,
    Role,
    RoleRead,
    RoleRelease,
    RoleWait,
    RuntimeFailure,
    TaskDispatch,
    TaskVerify,
)
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.task_spec import VerificationSpec


class NativePlanOnlyTest(unittest.TestCase):
    def setUp(self):
        self.fixture = support.NativeBackendTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        subprocess.run(
            ["git", "init", "--quiet", str(self.fixture.workspace)], check=True
        )
        self.source = self.fixture.workspace / "facts.txt"
        self.source.write_text("confirmed")
        self.task = replace(
            self.fixture.task_spec(),
            allowed_paths=("facts.txt",),
            verification=(
                VerificationSpec(
                    "check-facts",
                    (
                        sys.executable,
                        "-c",
                        "from pathlib import Path; assert Path('facts.txt').read_text() == 'confirmed'; print('verified')",
                    ),
                    5,
                ),
            ),
        )
        self.planner = NodeRef("planner", Role.PLANNER)
        self.reviewer = NodeRef("reviewer", Role.REVIEWER)
        self.graph = GraphSpec(
            (NodeRef("main", Role.MAIN), self.planner, self.reviewer),
            (
                GraphEdge("main", "planner", "delegates-to"),
                GraphEdge("planner", "reviewer", "reviewed-by"),
            ),
            Coordination("agent", ("main",), "serial", 1),
            (TaskRoute(self.task.task_id, "planner", "reviewer", None, None),),
        )

    def dispatch(self, backend, role):
        revision = native.snapshot_revision(self.fixture.workspace)
        processes = []

        def spawn(argv, **kwargs):
            process = support.FakePopen(argv, **kwargs)
            processes.append(process)
            return process

        try:
            with (
                mock.patch.object(native, "snapshot_revision", return_value=revision),
                mock.patch.object(native.subprocess, "Popen", side_effect=spawn),
                mock.patch.object(native.os, "getpgid", return_value=77001),
            ):
                backend.request(TaskDispatch(role, self.task, "作成・確認してください"))
        finally:
            for process in processes:
                process.wait()
        return native.runtime_read_state(self.fixture.state_path)["roles"][role.node_id]

    def finish(self, backend, role, body, evidence=None):
        state = native.runtime_read_state(self.fixture.state_path)
        assignment = state["roles"][role.node_id]
        with mock.patch.object(native, "_assert_publisher"):
            native.publish_completion(
                self.fixture.state_path,
                role=role.node_id,
                run_id=state["run_id"],
                **{
                    key: assignment[key]
                    for key in (
                        "task_id",
                        "dispatch_id",
                        "terminal_handle",
                        "launch_nonce",
                    )
                },
                outcome="succeeded",
                body=body,
                cleanup_confirmed=True,
                task_evidence=evidence,
            )
        delivery = backend.request(RoleWait(role, 1000))
        backend.request(RoleRead(role, 2000))
        backend.request(RoleRelease(role))
        backend.request(DeliveryAck(delivery.delivery_id))

    def approve(self, backend):
        self.dispatch(backend, self.planner)
        self.finish(backend, self.planner, "facts.txtの内容を確認しました。")
        review = self.dispatch(backend, self.reviewer)
        verdict = {
            "task_id": self.task.task_id,
            "stage": "plan",
            "revision": review["task_revision"],
            "decision": "approve",
            "findings": [],
        }
        self.finish(backend, self.reviewer, "承認", verdict)
        return review

    def test_plan_digest_and_code_revision_are_bound_to_real_fixed_verification(self):
        with self.fixture.planner_backend(
            reviewer=True, task_specs=(self.task,), graph=self.graph
        ) as backend:
            self.dispatch(backend, self.planner)
            body = "facts.txtの内容を確認しました。"
            self.finish(backend, self.planner, body)
            with self.assertRaises(RuntimeFailure):
                backend.request(TaskVerify(self.task.task_id))
            review = self.dispatch(backend, self.reviewer)
            code_revision = native.snapshot_revision(self.fixture.workspace)
            self.assertEqual(review["task_workspace_revision"], code_revision)
            self.assertEqual(
                review["task_revision"], hashlib.sha256(body.encode()).hexdigest()
            )
            self.assertNotEqual(review["task_revision"], code_revision)
            verdict = {
                "task_id": self.task.task_id,
                "stage": "plan",
                "revision": review["task_revision"],
                "decision": "approve",
                "findings": [],
            }
            self.source.write_text("drift")
            before = self.fixture.state_path.read_bytes()
            with self.assertRaisesRegex(RuntimeFailure, "workspace revision changed"):
                self.finish(backend, self.reviewer, "承認", verdict)
            self.assertEqual(self.fixture.state_path.read_bytes(), before)
            self.source.write_text("confirmed")
            self.finish(backend, self.reviewer, "承認", verdict)
            self.source.write_text("later drift")
            before = self.fixture.state_path.read_bytes()
            with self.assertRaisesRegex(RuntimeFailure, "workspace revision changed"):
                backend.request(TaskVerify(self.task.task_id))
            self.assertEqual(self.fixture.state_path.read_bytes(), before)
            self.source.write_text("confirmed")
            receipt = backend.request(TaskVerify(self.task.task_id))
            self.assertEqual(receipt.status, "completed")
            self.assertEqual(receipt.record["revision"], review["task_revision"])
            self.assertEqual(receipt.record["verification"]["revision"], code_revision)
            command = receipt.record["verification"]["commands"][0]
            self.assertEqual(command["argv"], list(self.task.verification[0].argv))
            self.assertEqual(command["returncode"], 0)
            self.assertTrue(receipt.record["verification"]["cleanup_confirmed"])

    def test_failed_fixed_verification_routes_back_to_exact_planner_with_limit(self):
        self.task = replace(
            self.task,
            verification=(
                VerificationSpec(
                    "fails", (sys.executable, "-c", "raise SystemExit(1)"), 5
                ),
            ),
        )
        with self.fixture.planner_backend(
            reviewer=True, task_specs=(self.task,), graph=self.graph
        ) as backend:
            self.approve(backend)
            receipt = backend.request(TaskVerify(self.task.task_id))
            self.assertEqual(receipt.status, "verification_failed")
            self.assertTrue(receipt.record["verification"]["cleanup_confirmed"])
            self.assertEqual(
                receipt.record["verification"]["commands"][0]["returncode"], 1
            )
            self.approve(backend)
            self.assertEqual(
                backend.request(TaskVerify(self.task.task_id)).status,
                "verification_failed",
            )
            before = self.fixture.state_path.read_bytes()
            with self.assertRaisesRegex(RuntimeFailure, "maximum review rounds"):
                self.dispatch(backend, self.planner)
            self.assertEqual(self.fixture.state_path.read_bytes(), before)
