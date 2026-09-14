from __future__ import annotations

import hashlib
import json
import unittest
from copy import deepcopy
from typing import cast

from agent_team.contracts import ErrorCode, Role, RuntimeFailure, TaskDispatch
from agent_team.task_execution import (
    acknowledge_task,
    parse_review,
    prepare_dispatch,
    task_digest,
    validate_saved_tasks,
)
from agent_team.task_spec import TaskSpec, VerificationSpec


def task(task_id: str = "task-one", *, dependencies: tuple[str, ...] = ()) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective="Complete the requested change.",
        acceptance_criteria=("The acceptance criteria are met.",),
        allowed_paths=("agent_team/",),
        forbidden_paths=("upstreams.json",),
        dependencies=dependencies,
        verification=(VerificationSpec("unit", ("python3", "-m", "unittest"), 60),),
        evidence_requirements=("Report the command and result.",),
        consultation_conditions=("Ask before exceeding the declared scope.",),
    )


def two_command_task() -> TaskSpec:
    encoded = task().as_dict()
    encoded["verification"] = [
        {
            "name": "first",
            "argv": ["python3", "-c", "print('first')"],
            "timeout_seconds": 60,
        },
        {
            "name": "second",
            "argv": ["python3", "-c", "print('second')"],
            "timeout_seconds": 60,
        },
    ]
    return TaskSpec.from_dict(encoded)


def state(
    *, max_review_rounds: object = 2, task_catalog: tuple[TaskSpec, ...] | None = None
) -> dict[str, object]:
    catalog = task_catalog or (
        task(),
        task("new-task"),
        task("successor", dependencies=("task-one",)),
    )
    return {
        "max_review_rounds": max_review_rounds,
        "tasks": {},
        "task_specs": [item.as_dict() for item in catalog],
    }


def result(
    *,
    dispatch_id: str,
    role: Role,
    outcome: str = "succeeded",
    body: str = "provider result",
    task_evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "dispatch_id": dispatch_id,
        "role": role.value,
        "outcome": outcome,
        "body": body,
    }
    if task_evidence is not None:
        value["task_evidence"] = task_evidence
    return value


def saved_record(saved: dict[str, object]) -> dict[str, object]:
    tasks = cast(dict[str, object], saved["tasks"])
    return cast(dict[str, object], tasks["task-one"])


def implementation_approval(task_value: TaskSpec, revision: str) -> dict[str, object]:
    return {
        "task_id": task_value.task_id,
        "stage": "implementation",
        "revision": revision,
        "decision": "approve",
        "findings": [],
    }


def verification_record(
    task_value: TaskSpec,
    revision: str,
    *,
    passed: bool = True,
    cleanup_confirmed: bool = True,
) -> dict[str, object]:
    commands = [
        {
            "name": spec.name,
            "argv": list(spec.argv),
            "timeout_seconds": spec.timeout_seconds,
            "returncode": 0 if passed else 1,
            "stdout_sha256": "a" * 64,
            "stderr_sha256": "b" * 64,
            "error": None
            if passed
            else "verification command exited with return code 1",
        }
        for spec in task_value.verification
    ]
    return {
        "revision": revision,
        "passed": passed,
        "commands": commands,
        "error": None if passed else "verification command exited with return code 1",
        "cleanup_confirmed": cleanup_confirmed,
    }


def completed_state() -> dict[str, object]:
    saved = state()
    current = task()
    record, _prompt = prepare_dispatch(
        saved, TaskDispatch(Role.WORKER, current, "implement")
    )
    record["dispatch_id"] = "worker-dispatch"
    acknowledge_task(
        saved,
        result=result(
            dispatch_id="worker-dispatch", role=Role.WORKER, body="implementation"
        ),
    )
    record = saved_record(saved)
    revision = "tree-revision"
    verdict = implementation_approval(current, revision)
    record.update(
        {
            "dispatch_id": "verification-dispatch",
            "status": "completed",
            "role": Role.REVIEWER.value,
            "stage": "implementation",
            "revision": revision,
            "task_evidence": verdict,
            "review_result": {
                "dispatch_id": "review-dispatch",
                "role": Role.REVIEWER.value,
                "outcome": "succeeded",
                "body": "approved",
                "task_evidence": verdict,
            },
            "review_rounds": {"plan": 0, "implementation": 1},
            "verification": verification_record(current, revision),
        }
    )
    return saved


class TaskExecutionTest(unittest.TestCase):
    def test_undeclared_task_cannot_introduce_scope_or_verification_authority(
        self,
    ) -> None:
        saved = state()
        saved["task_specs"] = []
        before = deepcopy(saved)
        with self.assertRaisesRegex(RuntimeFailure, "declared.*startup"):
            prepare_dispatch(saved, TaskDispatch(Role.WORKER, task(), "implement"))
        self.assertEqual(saved, before)

    def test_declared_task_rejects_changed_scope_command_and_task_id(self) -> None:
        for field, value in (
            ("task_id", "unapproved"),
            ("allowed_paths", ["unapproved/"]),
            (
                "verification",
                [
                    {
                        "name": "unapproved",
                        "argv": ["python3", "-c", "print('unapproved')"],
                        "timeout_seconds": 5,
                    }
                ],
            ),
        ):
            with self.subTest(field=field):
                saved = state()
                changed = task().as_dict()
                changed[field] = value
                before = deepcopy(saved)
                with self.assertRaisesRegex(RuntimeFailure, "declared.*startup"):
                    prepare_dispatch(
                        saved,
                        TaskDispatch(
                            Role.WORKER, TaskSpec.from_dict(changed), "implement"
                        ),
                    )
                self.assertEqual(saved, before)

    def test_prepare_dispatch_persists_spec_digest_and_rejects_new_reviewer(
        self,
    ) -> None:
        saved = state()
        request = TaskDispatch(Role.PLANNER, task(), "Investigate the repository.")

        record, prompt = prepare_dispatch(saved, request)

        self.assertEqual(record["spec"], request.task.as_dict())
        self.assertEqual(record["digest"], task_digest(request.task))
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["role"], "planner")
        self.assertEqual(record["review_rounds"], {"plan": 0, "implementation": 0})
        self.assertIn(request.message, prompt)
        self.assertIn(
            json.dumps(request.task.as_dict(), ensure_ascii=False, sort_keys=True),
            prompt,
        )

        before = deepcopy(saved)
        with self.assertRaises(RuntimeFailure) as raised:
            prepare_dispatch(
                saved,
                TaskDispatch(Role.REVIEWER, task("new-task"), "review"),
            )
        self.assertEqual(raised.exception.code, ErrorCode.ORDER_VIOLATION)
        self.assertEqual(saved, before)

    def test_writer_success_waits_for_the_matching_review(self) -> None:
        saved = state()
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.PLANNER, task(), "plan")
        )
        prepared["dispatch_id"] = "planner-dispatch"

        acknowledge_task(
            saved,
            result=result(
                dispatch_id="planner-dispatch", role=Role.PLANNER, body="plan body"
            ),
        )

        record = saved_record(saved)
        self.assertEqual(record["status"], "awaiting_plan_review")
        self.assertEqual(record["revision"], hashlib.sha256(b"plan body").hexdigest())
        self.assertNotEqual(record["status"], "completed")
        self.assertEqual(
            cast(dict[str, object], record["writer_result"])["body"], "plan body"
        )

    def test_parse_review_is_exact_and_binds_task_stage_revision(self) -> None:
        current = task()
        output = json.dumps(
            {
                "task_id": current.task_id,
                "stage": "plan",
                "revision": "revision-1",
                "decision": "request_changes",
                "findings": ["Add the missing evidence."],
            }
        )

        parsed = parse_review(output, task=current, stage="plan", revision="revision-1")

        self.assertEqual(parsed["decision"], "request_changes")
        invalid_cases: tuple[dict[str, object], ...] = (
            {"extra": True},
            {"task_id": "other"},
            {"stage": "implementation"},
            {"revision": "old"},
            {"decision": "unknown"},
            {"findings": []},
        )
        for invalid in invalid_cases:
            with self.subTest(invalid=invalid):
                value: dict[str, object] = {
                    "task_id": current.task_id,
                    "stage": "plan",
                    "revision": "revision-1",
                    "decision": "request_changes",
                    "findings": ["finding"],
                }
                value.update(invalid)
                with self.assertRaises(RuntimeFailure):
                    parse_review(
                        json.dumps(value),
                        task=current,
                        stage="plan",
                        revision="revision-1",
                    )

    def test_review_verdict_drives_writer_only_transitions(self) -> None:
        saved = state()
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.PLANNER, task(), "plan")
        )
        prepared["dispatch_id"] = "planner-dispatch"
        acknowledge_task(
            saved,
            result=result(
                dispatch_id="planner-dispatch", role=Role.PLANNER, body="plan body"
            ),
        )
        reviewed, prompt = prepare_dispatch(
            saved, TaskDispatch(Role.REVIEWER, task(), "review")
        )
        reviewed["dispatch_id"] = "plan-review-dispatch"
        self.assertEqual(reviewed["status"], "reviewing_plan")
        self.assertIn('"decision":"approve"', prompt)
        verdict = parse_review(
            json.dumps(
                {
                    "task_id": "task-one",
                    "stage": "plan",
                    "revision": reviewed["revision"],
                    "decision": "approve",
                    "findings": [],
                },
                separators=(",", ":"),
            ),
            task=task(),
            stage="plan",
            revision=str(reviewed["revision"]),
        )
        acknowledge_task(
            saved,
            result=result(
                dispatch_id="plan-review-dispatch",
                role=Role.REVIEWER,
                task_evidence=verdict,
            ),
        )
        self.assertEqual(saved_record(saved)["status"], "plan_approved")

        with self.assertRaises(RuntimeFailure):
            prepare_dispatch(saved, TaskDispatch(Role.PLANNER, task(), "retry plan"))
        worker, worker_prompt = prepare_dispatch(
            saved, TaskDispatch(Role.WORKER, task(), "implement")
        )
        self.assertEqual(worker["status"], "running")
        self.assertEqual(worker["role"], "worker")
        self.assertIn("plan body", worker_prompt)
        self.assertIn(json.dumps(verdict, ensure_ascii=False), worker_prompt)

    def test_implementation_review_requires_revision_and_never_completes_task(
        self,
    ) -> None:
        saved = state(max_review_rounds=1)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.WORKER, task(), "implement")
        )
        prepared["dispatch_id"] = "worker-dispatch"
        acknowledge_task(
            saved,
            result=result(
                dispatch_id="worker-dispatch", role=Role.WORKER, body="implementation"
            ),
        )
        before = deepcopy(saved)
        with self.assertRaises(RuntimeFailure):
            prepare_dispatch(saved, TaskDispatch(Role.REVIEWER, task(), "review"))
        self.assertEqual(saved, before)

        review, _prompt = prepare_dispatch(
            saved,
            TaskDispatch(Role.REVIEWER, task(), "review"),
            revision="tree-revision-1",
        )
        review["dispatch_id"] = "implementation-review-dispatch"
        verdict = parse_review(
            json.dumps(
                {
                    "task_id": "task-one",
                    "stage": "implementation",
                    "revision": "tree-revision-1",
                    "decision": "approve",
                    "findings": [],
                }
            ),
            task=task(),
            stage="implementation",
            revision="tree-revision-1",
        )
        acknowledge_task(
            saved,
            result=result(
                dispatch_id="implementation-review-dispatch",
                role=Role.REVIEWER,
                task_evidence=verdict,
            ),
        )
        self.assertEqual(saved_record(saved)["status"], "implementation_approved")

    def test_review_limit_and_consultation_are_fail_closed(self) -> None:
        saved = state(max_review_rounds=1)
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.PLANNER, task(), "plan")
        )
        prepared["dispatch_id"] = "planner-dispatch"
        acknowledge_task(
            saved,
            result=result(
                dispatch_id="planner-dispatch", role=Role.PLANNER, body="plan body"
            ),
        )
        reviewed, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.REVIEWER, task(), "review")
        )
        reviewed["dispatch_id"] = "plan-review-dispatch"
        verdict = parse_review(
            json.dumps(
                {
                    "task_id": "task-one",
                    "stage": "plan",
                    "revision": reviewed["revision"],
                    "decision": "request_changes",
                    "findings": ["Change it."],
                }
            ),
            task=task(),
            stage="plan",
            revision=str(reviewed["revision"]),
        )
        acknowledge_task(
            saved,
            result=result(
                dispatch_id="plan-review-dispatch",
                role=Role.REVIEWER,
                task_evidence=verdict,
            ),
        )
        writer, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.PLANNER, task(), "revise")
        )
        writer["dispatch_id"] = "planner-retry-dispatch"
        acknowledge_task(
            saved,
            result=result(
                dispatch_id="planner-retry-dispatch", role=Role.PLANNER, body="retry"
            ),
        )
        before = deepcopy(saved)
        with self.assertRaisesRegex(RuntimeFailure, "user consultation required"):
            prepare_dispatch(saved, TaskDispatch(Role.REVIEWER, task(), "review again"))
        self.assertEqual(saved, before)

    def test_saved_task_validation_requires_max_and_identity_fields(self) -> None:
        with self.assertRaises(RuntimeFailure):
            validate_saved_tasks({"tasks": {}})
        saved = state()
        record, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.WORKER, task(), "implement")
        )
        record["dispatch_id"] = "worker-dispatch"
        validate_saved_tasks(saved)
        record["digest"] = "0" * 64
        with self.assertRaises(RuntimeFailure):
            validate_saved_tasks(saved)

    def test_saved_task_validation_rejects_incoherent_stage_and_provider_failure_is_terminal(
        self,
    ) -> None:
        saved = state()
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.PLANNER, task(), "plan")
        )
        prepared["dispatch_id"] = "planner-dispatch"
        acknowledge_task(
            saved,
            result=result(
                dispatch_id="planner-dispatch",
                role=Role.PLANNER,
                outcome="failed",
                body="provider failed",
            ),
        )
        self.assertEqual(saved_record(saved)["status"], "failed")
        with self.assertRaises(RuntimeFailure):
            prepare_dispatch(saved, TaskDispatch(Role.PLANNER, task(), "retry"))

        saved = state()
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.PLANNER, task(), "plan")
        )
        prepared["dispatch_id"] = "planner-dispatch"
        acknowledge_task(
            saved,
            result=result(
                dispatch_id="planner-dispatch", role=Role.PLANNER, body="plan body"
            ),
        )
        saved_record(saved)["role"] = "worker"
        with self.assertRaises(RuntimeFailure):
            validate_saved_tasks(saved)

    def test_completed_requires_approval_and_complete_matching_verification(
        self,
    ) -> None:
        mutations = (
            "missing-evidence",
            "stale-revision",
            "wrong-argv",
            "missing-command",
            "nonzero-command",
            "unknown-command",
            "error-command",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                saved = completed_state()
                record = saved_record(saved)
                if mutation == "missing-evidence":
                    record.pop("task_evidence")
                elif mutation == "stale-revision":
                    verification = cast(dict[str, object], record["verification"])
                    verification["revision"] = "stale"
                elif mutation == "wrong-argv":
                    verification = cast(dict[str, object], record["verification"])
                    commands = cast(list[dict[str, object]], verification["commands"])
                    commands[0]["argv"] = ["wrong"]
                elif mutation == "missing-command":
                    verification = cast(dict[str, object], record["verification"])
                    verification["commands"] = []
                else:
                    verification = cast(dict[str, object], record["verification"])
                    commands = cast(list[dict[str, object]], verification["commands"])
                    commands[0]["returncode"] = (
                        1
                        if mutation == "nonzero-command"
                        else None
                        if mutation == "unknown-command"
                        else 0
                    )
                    commands[0]["error"] = "command failed"
                with self.assertRaises(RuntimeFailure):
                    validate_saved_tasks(saved)

    def test_valid_completed_record_unlocks_dependency(self) -> None:
        saved = completed_state()

        successor = task("successor", dependencies=("task-one",))
        record, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.WORKER, successor, "continue")
        )

        self.assertEqual(record["status"], "running")
        self.assertEqual(record["role"], Role.WORKER.value)

        forged = completed_state()
        forged_record = saved_record(forged)
        forged_verification = cast(dict[str, object], forged_record["verification"])
        forged_commands = cast(list[dict[str, object]], forged_verification["commands"])
        forged_commands[0]["returncode"] = 1
        forged_commands[0]["error"] = "command failed"
        with self.assertRaises(RuntimeFailure):
            prepare_dispatch(
                forged,
                TaskDispatch(
                    Role.WORKER,
                    task("successor", dependencies=("task-one",)),
                    "continue",
                ),
            )

    def test_verification_failed_can_redispatch_worker_with_review_findings(
        self,
    ) -> None:
        saved = completed_state()
        record = saved_record(saved)
        current = task()
        findings = ["The verification command failed."]
        verdict = {
            **implementation_approval(current, "tree-revision"),
            "findings": findings,
        }
        record["task_evidence"] = verdict
        cast(dict[str, object], record["review_result"])["task_evidence"] = verdict
        record["status"] = "verification_failed"
        record["verification"] = verification_record(
            current, "tree-revision", passed=False, cleanup_confirmed=True
        )
        verification = cast(dict[str, object], record["verification"])
        verification["error"] = "verification command exited with return code 1"
        commands = cast(list[dict[str, object]], verification["commands"])
        commands[0]["error"] = "verification command exited with return code 1"
        commands[0]["returncode"] = 1

        worker, prompt = prepare_dispatch(
            saved, TaskDispatch(Role.WORKER, current, "fix and rerun")
        )

        self.assertEqual(worker["status"], "running")
        self.assertIn(findings[0], prompt)
        self.assertEqual(
            cast(dict[str, int], worker["review_rounds"])["implementation"], 1
        )

    def test_verification_failed_partial_prefix_can_redispatch_worker(self) -> None:
        current = two_command_task()
        saved = state(task_catalog=(current,))
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.WORKER, current, "implement")
        )
        prepared["dispatch_id"] = "worker-dispatch"
        acknowledge_task(
            saved,
            result=result(
                dispatch_id="worker-dispatch", role=Role.WORKER, body="implementation"
            ),
        )
        record = saved_record(saved)
        revision = "tree-revision"
        verdict = implementation_approval(current, revision)
        record.update(
            {
                "dispatch_id": "verification-dispatch",
                "status": "verification_failed",
                "role": Role.REVIEWER.value,
                "stage": "implementation",
                "revision": revision,
                "task_evidence": verdict,
                "review_result": {
                    "role": Role.REVIEWER.value,
                    "task_evidence": verdict,
                },
                "verification": {
                    "revision": revision,
                    "passed": False,
                    "commands": [
                        {
                            "name": current.verification[0].name,
                            "argv": list(current.verification[0].argv),
                            "timeout_seconds": current.verification[0].timeout_seconds,
                            "returncode": 0,
                            "stdout_sha256": "a" * 64,
                            "stderr_sha256": "b" * 64,
                            "error": None,
                        }
                    ],
                    "error": "workspace revision changed during verification",
                    "cleanup_confirmed": True,
                },
                "review_rounds": {"plan": 0, "implementation": 1},
            }
        )

        worker, prompt = prepare_dispatch(
            saved, TaskDispatch(Role.WORKER, current, "repair")
        )

        self.assertEqual(worker["status"], "running")
        self.assertEqual(worker["role"], Role.WORKER.value)
        self.assertIn(json.dumps(verdict, ensure_ascii=False), prompt)

    def test_verification_failed_empty_prefix_is_retryable_only_when_cleanup_is_known(
        self,
    ) -> None:
        current = two_command_task()
        saved = state(task_catalog=(current,))
        prepared, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.WORKER, current, "implement")
        )
        prepared["dispatch_id"] = "worker-dispatch"
        acknowledge_task(
            saved,
            result=result(
                dispatch_id="worker-dispatch", role=Role.WORKER, body="implementation"
            ),
        )
        record = saved_record(saved)
        revision = "tree-revision"
        verdict = implementation_approval(current, revision)
        record.update(
            {
                "dispatch_id": "verification-dispatch",
                "status": "verification_failed",
                "role": Role.REVIEWER.value,
                "stage": "implementation",
                "revision": revision,
                "task_evidence": verdict,
                "review_result": {"task_evidence": verdict},
                "verification": {
                    "revision": revision,
                    "passed": False,
                    "commands": [],
                    "error": "verification interrupted after cleanup",
                    "cleanup_confirmed": True,
                },
                "review_rounds": {"plan": 0, "implementation": 1},
            }
        )
        worker, _prompt = prepare_dispatch(
            saved, TaskDispatch(Role.WORKER, current, "repair")
        )
        self.assertEqual(worker["status"], "running")

        record["status"] = "verifying"
        with self.assertRaises(RuntimeFailure):
            prepare_dispatch(saved, TaskDispatch(Role.WORKER, current, "retry"))

    def test_verification_failed_rejects_forged_prefixes_and_success_incomplete(
        self,
    ) -> None:
        current = two_command_task()
        for mutation in ("wrong-argv", "too-many", "success-incomplete"):
            with self.subTest(mutation=mutation):
                saved = state(task_catalog=(current,))
                prepared, _prompt = prepare_dispatch(
                    saved, TaskDispatch(Role.WORKER, current, "implement")
                )
                prepared["dispatch_id"] = "worker-dispatch"
                acknowledge_task(
                    saved,
                    result=result(
                        dispatch_id="worker-dispatch",
                        role=Role.WORKER,
                        body="implementation",
                    ),
                )
                record = saved_record(saved)
                revision = "tree-revision"
                verdict = implementation_approval(current, revision)
                commands = [
                    {
                        "name": current.verification[0].name,
                        "argv": list(current.verification[0].argv),
                        "timeout_seconds": current.verification[0].timeout_seconds,
                        "returncode": 0,
                        "stdout_sha256": "a" * 64,
                        "stderr_sha256": "b" * 64,
                        "error": None,
                    }
                ]
                if mutation == "wrong-argv":
                    commands[0]["argv"] = ["wrong"]
                elif mutation == "too-many":
                    commands.append(dict(commands[0]))
                record.update(
                    {
                        "dispatch_id": "verification-dispatch",
                        "status": "verification_failed",
                        "role": Role.REVIEWER.value,
                        "stage": "implementation",
                        "revision": revision,
                        "task_evidence": verdict,
                        "review_result": {"task_evidence": verdict},
                        "verification": {
                            "revision": revision,
                            "passed": mutation == "success-incomplete",
                            "commands": commands,
                            "error": (
                                None
                                if mutation == "success-incomplete"
                                else "verification failed"
                            ),
                            "cleanup_confirmed": True,
                        },
                        "review_rounds": {"plan": 0, "implementation": 1},
                    }
                )
                with self.assertRaises(RuntimeFailure):
                    prepare_dispatch(saved, TaskDispatch(Role.WORKER, current, "retry"))


if __name__ == "__main__":
    unittest.main()
