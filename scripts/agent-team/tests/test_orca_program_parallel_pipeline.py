from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import unittest
from collections.abc import Mapping
from dataclasses import replace
from typing import cast
from unittest import mock

import test_orca_task_pipeline as pipeline_fixture

from agent_team import (
    mcp_server,
    orca_acp,
    orca_dispatch,
    orca_program,
    orca_tasks,
    program_driver,
    workspace_revision,
)
from agent_team.contracts import NodeRef, Role
from agent_team.named_graph import Coordination, GraphEdge, GraphSpec, TaskRoute
from agent_team.runtime import read_state, write_state
from agent_team.task_execution import new_program_wave


class OrcaProgramParallelPipelineTest(unittest.TestCase):
    revision = "a" * 64

    def setUp(self) -> None:
        # Reuse the serial pipeline fixture's dependency/preflight seams.  The
        # saved state below is then replaced with the program/parallel shape.
        self.fixture = pipeline_fixture.OrcaTaskPipelineIntegrationTest(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.path
        self.backend = self.fixture.fixture.backend
        self.remote_calls: list[tuple[str, ...]] = []
        self.wait_batches: list[tuple[str, tuple[str, ...]]] = []
        self.read_dispatches: list[str] = []
        self.release_dispatches: list[str] = []
        self.acknowledged: list[str] = []
        self._task_counter = 0
        self._dispatch_counter = 0
        self._prepare_program_parallel_state()

        patches = self.fixture.patches
        patches.enter_context(mock.patch.object(orca_tasks.OrcaTasks, "verify_remote"))
        patches.enter_context(
            mock.patch.object(mcp_server, "run_orca", side_effect=self._run_orca)
        )
        patches.enter_context(
            mock.patch.object(orca_dispatch, "run_orca", side_effect=self._run_orca)
        )
        patches.enter_context(
            mock.patch.object(
                orca_program,
                "read_process_argv",
                return_value=tuple(self.coordinator_argv),
            )
        )
        patches.enter_context(
            mock.patch.object(
                workspace_revision,
                "snapshot_revision",
                return_value=self.revision,
            )
        )

    def _prepare_program_parallel_state(self) -> None:
        state = read_state(self.path)
        first_task = replace(self.fixture.task, allowed_paths=("src/task-a",))
        second_task = replace(
            first_task,
            task_id="task-b",
            objective="Complete task-b.",
            allowed_paths=("src/task-b",),
        )
        nodes = (
            NodeRef("worker-a", Role.WORKER),
            NodeRef("worker-b", Role.WORKER),
            NodeRef("reviewer-a", Role.REVIEWER),
            NodeRef("reviewer-b", Role.REVIEWER),
        )
        graph = GraphSpec(
            nodes=nodes,
            edges=(
                GraphEdge("worker-a", "reviewer-a", "reviewed-by"),
                GraphEdge("worker-b", "reviewer-b", "reviewed-by"),
            ),
            coordination=Coordination(
                "program", ("worker-a", "worker-b"), "parallel", 2
            ),
            routes=(
                TaskRoute("task-1", None, None, "worker-a", "reviewer-a"),
                TaskRoute("task-b", None, None, "worker-b", "reviewer-b"),
            ),
        )
        role_specs = cast(dict[str, object], state["role_specs"])
        worker_spec = copy.deepcopy(role_specs["worker-a"])
        reviewer_spec = copy.deepcopy(role_specs["reviewer-a"])
        role_specs = {
            "worker-a": worker_spec,
            "worker-b": copy.deepcopy(worker_spec),
            "reviewer-a": reviewer_spec,
            "reviewer-b": copy.deepcopy(reviewer_spec),
        }
        coordinator_terminal = cast(str, state.pop("main_terminal"))
        state.update(
            {
                "version": 5,
                "graph": graph.as_dict(),
                "task_specs": [first_task.as_dict(), second_task.as_dict()],
                "role_specs": role_specs,
                "roles": {},
                "tasks": {},
                "coordinator_terminal": coordinator_terminal,
            }
        )
        state.pop("agent_batch", None)
        nonce = "nonceprogram1234"
        self.coordinator_argv = orca_program.launch_argv(
            self.path, str(state["run_id"]), nonce
        )
        state["coordinator_argv"] = self.coordinator_argv
        state["coordinator_process"] = {
            "pid": os.getpid(),
            "process_group_id": os.getpgrp(),
            "launch_nonce": nonce,
            "argv": self.coordinator_argv,
            "phase": "running",
            "exit_code": None,
            "cli_cleanup_confirmed": None,
        }
        state["program_wave"] = new_program_wave(state)
        write_state(self.path, state, require_existing=True)
        self.backend._state = read_state(self.path)
        self.tasks = (first_task, second_task)

    @staticmethod
    def _arg(args: list[str], name: str) -> str:
        return args[args.index(name) + 1]

    def _run_orca(
        self,
        state: dict[str, object],
        args: list[str],
        **_kwargs: object,
    ) -> dict[str, object]:
        self.remote_calls.append(tuple(args))
        operation = tuple(args[:2])
        if operation == ("orchestration", "task-create"):
            self._task_counter += 1
            return {"task": {"id": f"remote-task-{self._task_counter}"}}
        if operation == ("terminal", "create"):
            title = self._arg(args, "--title")
            return {
                "terminal": {
                    "handle": f"terminal-{title.rsplit('-', 1)[-1]}",
                    "worktreeId": state["worktree_id"],
                    "title": title,
                }
            }
        if operation == ("orchestration", "dispatch"):
            self._dispatch_counter += 1
            task_id = self._arg(args, "--task")
            terminal = self._arg(args, "--to")
            return {
                "injected": False,
                "dispatch": {
                    "id": f"remote-dispatch-{self._dispatch_counter}",
                    "task_id": task_id,
                    "assignee_handle": terminal,
                    "run_id": state["run_id"],
                },
            }
        if operation == ("terminal", "send"):
            return {}
        if operation == ("orchestration", "worker-read"):
            dispatch_id = self._arg(args, "--dispatch")
            self.read_dispatches.append(dispatch_id)
            assignment = self._assignment_for_dispatch(
                read_state(self.path), dispatch_id
            )
            return {
                "dispatchId": dispatch_id,
                "source": "terminal",
                "sourceIdentity": assignment["terminal_handle"],
                "terminal": {
                    "handle": assignment["terminal_handle"],
                    "tail": ["trusted worker output"],
                },
            }
        if operation == ("orchestration", "worker-release"):
            dispatch_id = self._arg(args, "--dispatch")
            self.release_dispatches.append(dispatch_id)
            return {
                "dispatchId": dispatch_id,
                "state": "retained",
                "reason": "no_owned_resource",
                "processAction": "none",
                "archive": None,
            }
        if operation == ("orchestration", "check") and "--wait" in args:
            return self._publish_batch()
        if operation == ("orchestration", "check") and "--ack" in args:
            delivery_id = self._arg(args, "--ack")
            saved = read_state(self.path)
            batch = saved.get("orca_delivery_batch")
            if not isinstance(batch, Mapping) or batch.get("phase") != "acknowledging":
                raise AssertionError(
                    "whole batch ACK was not persisted before remote ACK"
                )
            self.acknowledged.append(delivery_id)
            return {"acknowledged": delivery_id}
        raise AssertionError(f"unexpected fake Orca command: {args!r}")

    @staticmethod
    def _assignment_for_dispatch(
        state: Mapping[str, object], dispatch_id: str
    ) -> dict[str, object]:
        roles = state.get("roles")
        if not isinstance(roles, Mapping):
            raise TypeError("saved roles are invalid")
        for assignment in roles.values():
            if (
                isinstance(assignment, dict)
                and assignment.get("dispatch_id") == dispatch_id
            ):
                return assignment
        raise AssertionError(f"unknown dispatch: {dispatch_id}")

    def _publish_batch(self) -> dict[str, object]:
        saved = read_state(self.path)
        roles = saved.get("roles")
        if not isinstance(roles, Mapping):
            raise TypeError("saved roles are invalid")
        active = [
            assignment
            for assignment in roles.values()
            if isinstance(assignment, dict) and "pending_delivery_id" not in assignment
        ]
        if not active:
            raise AssertionError("wait did not have an active assignment")
        active.sort(key=lambda assignment: str(assignment["role"]))
        delivery_id = f"delivery-{len(self.wait_batches) + 1}"
        messages: list[dict[str, object]] = []
        roles_seen: list[str] = []
        for assignment in active:
            role = str(assignment["role"])
            roles_seen.append(role)
            evidence: dict[str, object] | None = None
            if assignment["role_kind"] == Role.REVIEWER.value:
                task_spec = cast(dict[str, object], assignment["task_spec"])
                task_revision = assignment.get("task_revision")
                if task_revision != self.revision:
                    raise AssertionError("reviewer did not receive the sealed revision")
                evidence = {
                    "task_id": task_spec["task_id"],
                    "stage": "implementation",
                    "revision": task_revision,
                    "decision": "approve",
                    "findings": [],
                }
            body = (
                json.dumps(evidence, ensure_ascii=False, sort_keys=True)
                if evidence is not None
                else f"{role} completed"
            )
            with mock.patch.object(orca_acp, "_send_worker_done"):
                outcome = orca_acp.publish_completion(
                    self.path,
                    role=str(assignment["role"]),
                    role_kind=str(assignment["role_kind"]),
                    run_id=str(saved["run_id"]),
                    task_id=str(assignment["task_id"]),
                    dispatch_id=str(assignment["dispatch_id"]),
                    terminal_handle=str(assignment["terminal_handle"]),
                    launch_nonce=str(assignment["launch_nonce"]),
                    outcome="succeeded",
                    body=body,
                    cleanup_confirmed=True,
                    task_evidence=evidence,
                )
            if outcome != "succeeded":
                raise AssertionError(f"completion was not published: {outcome}")
            message = {
                "id": f"message-{role}",
                "run_id": saved["run_id"],
                "type": "worker_done",
                "from_handle": assignment["terminal_handle"],
                "body": body,
                "payload": {
                    "taskId": assignment["task_id"],
                    "dispatchId": assignment["dispatch_id"],
                    "outcome": "succeeded",
                },
            }
            messages.append(message)
        self.wait_batches.append((delivery_id, tuple(roles_seen)))
        return {"deliveryId": delivery_id, "messages": messages}

    def test_program_driver_drains_parallel_orca_batches_and_verifies_same_revision(
        self,
    ) -> None:
        snapshots = 0
        original_snapshot = self.backend.program_snapshot

        def snapshot() -> dict[str, object]:
            nonlocal snapshots
            snapshots += 1
            self.assertLess(
                snapshots, 120, "program driver did not make bounded progress"
            )
            return cast(dict[str, object], original_snapshot())

        output = io.StringIO()
        with (
            mock.patch.object(self.backend, "program_snapshot", side_effect=snapshot),
            contextlib.redirect_stdout(output),
        ):
            code = program_driver.drive(self.backend)

        self.assertEqual(code, 0, output.getvalue())
        saved = read_state(self.path)
        wave = cast(dict[str, object], saved["program_wave"])
        self.assertEqual(wave["phase"], "verification")
        self.assertEqual(wave["revision"], self.revision)
        tasks = cast(dict[str, object], saved["tasks"])
        self.assertEqual(
            {
                cast(dict[str, object], tasks[task.task_id])["status"]
                for task in self.tasks
            },
            {"completed"},
        )
        for task in self.tasks:
            record = cast(dict[str, object], tasks[task.task_id])
            verification = cast(dict[str, object], record["verification"])
            commands = cast(list[dict[str, object]], verification["commands"])
            self.assertEqual(verification["revision"], self.revision)
            self.assertEqual(commands[0]["argv"], ["/usr/bin/true"])
            self.assertEqual(commands[0]["returncode"], 0)
        self.assertEqual(saved["roles"], {})
        self.assertNotIn("orca_delivery_batch", saved)
        self.assertEqual(
            [roles for _delivery, roles in self.wait_batches],
            [
                ("worker-a", "worker-b"),
                ("reviewer-a", "reviewer-b"),
            ],
        )
        self.assertEqual(len(self.acknowledged), 2)
        self.assertEqual(len(self.read_dispatches), 4)
        self.assertEqual(len(self.release_dispatches), 4)
        self.assertEqual(
            len([call for call in self.remote_calls if "--ack" in call]), 2
        )
        self.assertNotIn("main_terminal", saved)
        self.assertIn("coordinator_terminal", saved)
        self.assertEqual(saved["coordinator_argv"], self.coordinator_argv)
        process = cast(dict[str, object], saved["coordinator_process"])
        self.assertEqual(process["argv"], self.coordinator_argv)


if __name__ == "__main__":
    unittest.main()
