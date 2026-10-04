from __future__ import annotations

import copy
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from unittest import mock

from agent_team import task_verification
from agent_team.contracts import ErrorCode, RuntimeFailure
from agent_team.task_spec import TaskSpec, VerificationSpec


class ApprovedTaskVerificationTest(unittest.TestCase):
    revision = "a" * 64
    workspace = Path("/private/approved-task-workspace")

    def _task(self, *commands: VerificationSpec) -> TaskSpec:
        selected = commands or (
            VerificationSpec("fixed", (sys.executable, "-c", "print('ok')"), 5),
        )
        return TaskSpec(
            task_id="verify-task",
            objective="Verify one approved task.",
            acceptance_criteria=("The revision stays unchanged.",),
            allowed_paths=("tracked.txt",),
            forbidden_paths=(),
            dependencies=(),
            verification=selected,
            evidence_requirements=("command evidence",),
            consultation_conditions=(),
        )

    def _state(
        self,
        task: TaskSpec,
        *,
        status: str = "implementation_approved",
        graph: dict[str, object] | None = None,
    ) -> dict[str, object]:
        record = {
            "task_id": task.task_id,
            "spec": task.as_dict(),
            "status": status,
            "stage": "implementation",
            "revision": self.revision,
            "task_evidence": {
                "task_id": task.task_id,
                "stage": "implementation",
                "revision": self.revision,
                "decision": "approve",
                "findings": [],
            },
        }
        state: dict[str, object] = {
            "workspace": str(self.workspace),
            "tasks": {task.task_id: record},
            "roles": {},
        }
        if graph is not None:
            state["graph"] = graph
        return state

    def _evidence(
        self, task: TaskSpec, *, passed: bool, cleanup_confirmed: bool = True
    ) -> dict[str, object]:
        return {
            "revision": self.revision,
            "passed": passed,
            "commands": [
                {
                    "name": command.name,
                    "argv": list(command.argv),
                    "timeout_seconds": command.timeout_seconds,
                    "returncode": 0 if passed else 1,
                    "stdout_sha256": "0" * 64,
                    "stderr_sha256": "0" * 64,
                    "error": None if passed else "failed",
                }
                for command in task.verification
            ],
            "error": None if passed else "failed",
            "cleanup_confirmed": cleanup_confirmed,
        }

    def _record(self, state: dict[str, object], task: TaskSpec) -> dict[str, object]:
        tasks = state["tasks"]
        assert isinstance(tasks, dict)
        record = tasks[task.task_id]
        assert isinstance(record, dict)
        return record

    def _verification(self, record: Mapping[str, object]) -> Mapping[str, object]:
        verification = record["verification"]
        assert isinstance(verification, Mapping)
        return verification

    def test_approved_revision_runs_fixed_argv_and_completes(self) -> None:
        task = self._task()
        state = self._state(task)
        saved: list[dict[str, object]] = []
        evidence = self._evidence(task, passed=True)

        def save() -> None:
            saved.append(copy.deepcopy(state))

        def verify(
            selected_task: TaskSpec, workspace: Path, revision: str
        ) -> dict[str, object]:
            self.assertEqual(selected_task.verification[0].argv[0], sys.executable)
            self.assertEqual(workspace, self.workspace)
            self.assertEqual(revision, self.revision)
            self.assertEqual(self._record(state, task)["status"], "verifying")
            return evidence

        with (
            mock.patch.object(
                task_verification, "snapshot_revision", return_value=self.revision
            ),
            mock.patch.object(
                task_verification, "run_verification", side_effect=verify
            ),
        ):
            result = task_verification.verify_approved_task(
                state, task.task_id, save=save
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.record["verification"], evidence)
        self.assertEqual(self._record(state, task)["status"], "completed")
        self.assertEqual(
            [self._record(snapshot, task)["status"] for snapshot in saved],
            ["verifying", "completed"],
        )

    def test_revision_drift_is_rejected_before_any_effect(self) -> None:
        task = self._task()
        state = self._state(task)
        save = mock.Mock()
        run = mock.Mock()

        with (
            mock.patch.object(
                task_verification, "snapshot_revision", return_value="b" * 64
            ),
            mock.patch.object(task_verification, "run_verification", run),
            self.assertRaises(RuntimeFailure) as raised,
        ):
            task_verification.verify_approved_task(state, task.task_id, save=save)

        self.assertIs(raised.exception.code, ErrorCode.IDENTITY_MISMATCH)
        run.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self._record(state, task)["status"], "implementation_approved")

    def test_failed_verification_keeps_all_declared_command_evidence(self) -> None:
        task = self._task(
            VerificationSpec("first", (sys.executable, "-c", "exit(1)"), 5),
            VerificationSpec("second", (sys.executable, "-c", "print('ran')"), 5),
        )
        state = self._state(task)
        evidence = self._evidence(task, passed=False)
        save = mock.Mock()

        with (
            mock.patch.object(
                task_verification, "snapshot_revision", return_value=self.revision
            ),
            mock.patch.object(
                task_verification, "run_verification", return_value=evidence
            ),
        ):
            result = task_verification.verify_approved_task(
                state, task.task_id, save=save
            )

        self.assertEqual(result.status, "verification_failed")
        self.assertEqual(
            len(cast(list[object], self._verification(result.record)["commands"])), 2
        )
        self.assertEqual(self._record(state, task)["status"], "verification_failed")
        self.assertEqual(save.call_count, 2)

    def test_unconfirmed_cleanup_retains_verifying_and_blocks_other_task(self) -> None:
        task = self._task()
        state = self._state(task)
        evidence = self._evidence(task, passed=False, cleanup_confirmed=False)
        save = mock.Mock()

        with (
            mock.patch.object(
                task_verification, "snapshot_revision", return_value=self.revision
            ),
            mock.patch.object(
                task_verification, "run_verification", return_value=evidence
            ),
        ):
            result = task_verification.verify_approved_task(
                state, task.task_id, save=save
            )

        self.assertEqual(result.status, "verifying")
        self.assertIs(self._verification(result.record)["cleanup_confirmed"], False)
        with self.assertRaisesRegex(RuntimeFailure, "verification cleanup"):
            task_verification.verify_approved_task(state, task.task_id, save=save)
        self.assertEqual(save.call_count, 2)

    def test_signal_cleanup_exception_saves_failed_state_then_reraises(self) -> None:
        task = self._task()
        state = self._state(task)
        saved: list[dict[str, object]] = []

        def save() -> None:
            saved.append(copy.deepcopy(state))

        with (
            mock.patch.object(
                task_verification, "snapshot_revision", return_value=self.revision
            ),
            mock.patch.object(
                task_verification,
                "run_verification",
                side_effect=task_verification.VerificationCancelled("interrupted"),
            ),
            self.assertRaises(task_verification.VerificationCancelled),
        ):
            task_verification.verify_approved_task(state, task.task_id, save=save)

        self.assertEqual(self._record(state, task)["status"], "verification_failed")
        self.assertIs(
            self._verification(self._record(state, task))["cleanup_confirmed"], True
        )
        self.assertEqual(self._record(saved[-1], task)["status"], "verification_failed")

    def test_saved_program_wave_and_agent_batch_guards_use_existing_helpers(
        self,
    ) -> None:
        task = self._task()
        graph: dict[str, object] = {
            "nodes": [],
            "edges": [],
            "coordination": {
                "mode": "program",
                "entry_nodes": ["writer"],
                "dispatch_mode": "serial",
                "max_active": 1,
            },
            "routes": [],
        }
        state = self._state(task, graph=graph)
        wave = {
            "phase": "verification",
            "task_ids": [task.task_id],
            "revision": self.revision,
        }
        save = mock.Mock()
        evidence = self._evidence(task, passed=True)

        with (
            mock.patch.object(
                task_verification, "snapshot_revision", return_value=self.revision
            ),
            mock.patch.object(
                task_verification, "program_wave", return_value=wave
            ) as program_wave,
            mock.patch.object(
                task_verification, "run_verification", return_value=evidence
            ),
        ):
            task_verification.verify_approved_task(state, task.task_id, save=save)
        program_wave.assert_called_once_with(state)

        state = self._state(task)
        save.reset_mock()
        with (
            mock.patch.object(
                task_verification, "snapshot_revision", return_value=self.revision
            ),
            mock.patch.object(
                task_verification, "is_agent_parallel", return_value=True
            ),
            mock.patch.object(
                task_verification, "prepare_agent_batch_verification"
            ) as prepare,
            mock.patch.object(
                task_verification, "run_verification", return_value=evidence
            ),
        ):
            task_verification.verify_approved_task(state, task.task_id, save=save)
        prepare.assert_called_once_with(state, task.task_id, self.revision)


if __name__ == "__main__":
    unittest.main()
