from __future__ import annotations

import json
import unittest
from collections.abc import Iterable
from dataclasses import FrozenInstanceError
from typing import cast, get_type_hints

from agent_team.task_policy import (
    MAX_VALIDATION_ERRORS,
    STATE_POLICY_VERSION,
    ClaimRef,
    ExpectedSequenceUpdate,
    GitObjectId,
    ResourceClaim,
    StateConflictError,
    TaskId,
    TaskKind,
    TaskLane,
    TaskPhase,
    TaskPolicyStateV4,
    TaskPolicyValidationError,
    TaskSpec,
    TreeDigest,
    ValidationResult,
    VerificationProfileRef,
    WorkspaceIdentity,
    apply_expected_sequence_update,
    canonical_task_json,
    canonical_task_state_json,
    parse_task_spec,
    parse_task_specs,
    parse_task_state,
    task_dependency_order,
    task_spec_to_dict,
    task_state_to_dict,
    validate_task_specs,
)
from agent_team.topology import NodeId, TeamId


def spec(
    task_id: str = "build",
    *,
    dependencies: tuple[str, ...] = (),
    escalation_node: str | None = "main",
    kind: TaskKind = TaskKind.IMPLEMENTATION,
    lane: TaskLane = TaskLane.NORMAL,
) -> TaskSpec:
    return TaskSpec(
        task_id=TaskId(task_id),
        title=f"Title {task_id}",
        context="A bounded context.",
        goal="Produce the requested result.",
        acceptance=("The result is checked.",),
        allowed_paths=("scripts/agent-team",),
        do_not_modify=(".git",),
        dependencies=tuple(TaskId(item) for item in dependencies),
        verification=VerificationProfileRef("python-tests"),
        escalation_node=(None if escalation_node is None else NodeId(escalation_node)),
        kind=kind,
        lane=lane,
        resource_claims=(
            ResourceClaim(
                "workspace",
            ),
        ),
    )


def valid_task_mapping(task_id: str = "build") -> dict[str, object]:
    return {
        "task_id": task_id,
        "title": f"Title {task_id}",
        "context": "A bounded context.",
        "goal": "Produce the requested result.",
        "acceptance": ["The result is checked."],
        "allowed_paths": ["scripts/agent-team"],
        "do_not_modify": [".git"],
        "dependencies": [],
        "verification": "python-tests",
        "escalation_node": "main",
        "kind": "implementation",
        "lane": "normal",
        "resource_claims": ["workspace"],
    }


class TaskPolicyValidationTest(unittest.TestCase):
    def test_task_spec_is_frozen_and_keeps_explicit_typed_values(self) -> None:
        value = spec()

        with self.assertRaises(FrozenInstanceError):
            value.title = "changed"  # type: ignore[misc]

        self.assertIs(value.kind, TaskKind.IMPLEMENTATION)
        self.assertIs(value.lane, TaskLane.NORMAL)
        self.assertEqual(value.dependencies, ())
        self.assertEqual(value.resource_claims, (ResourceClaim("workspace"),))

    def test_task_mapping_requires_all_fields_and_rejects_unknown_fields(self) -> None:
        missing = valid_task_mapping()
        del missing["goal"]
        with self.assertRaisesRegex(TaskPolicyValidationError, "missing goal"):
            parse_task_spec(missing)

        unknown = valid_task_mapping()
        unknown["argv"] = ["python"]
        with self.assertRaisesRegex(TaskPolicyValidationError, "unsupported fields"):
            parse_task_spec(unknown)

    def test_mapping_parsing_does_not_infer_lane_or_execution_fields(self) -> None:
        task = parse_task_spec(valid_task_mapping())

        self.assertEqual(task.lane, TaskLane.NORMAL)
        self.assertEqual(task.kind, TaskKind.IMPLEMENTATION)
        with self.assertRaises(TaskPolicyValidationError):
            parse_task_spec({**valid_task_mapping(), "lane": "default"})

    def test_task_array_requires_an_explicit_non_empty_list(self) -> None:
        self.assertEqual(parse_task_specs([valid_task_mapping()])[0].task_id, "build")
        with self.assertRaisesRegex(TaskPolicyValidationError, "must not be empty"):
            parse_task_specs([])

        malformed: dict[str | int, object] = {}
        for key, value in valid_task_mapping().items():
            malformed[key] = value
        malformed[1] = "not a field"
        with self.assertRaisesRegex(TaskPolicyValidationError, "unsupported fields"):
            parse_task_spec(cast(dict[str, object], malformed))

    def test_task_graph_validates_known_references_and_returns_stable_issues(
        self,
    ) -> None:
        tasks = (
            spec("deploy", dependencies=("build",)),
            spec("build"),
        )
        result = validate_task_specs(
            TeamId("build-team"),
            tasks,
            known_team_ids=(TeamId("build-team"),),
            known_node_ids=(NodeId("main"),),
            known_verification_profiles=(VerificationProfileRef("python-tests"),),
        )

        self.assertIsInstance(result, ValidationResult)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(
            task_dependency_order(tasks), (TaskId("build"), TaskId("deploy"))
        )

        reordered = tuple(reversed(tasks))
        self.assertEqual(
            validate_task_specs(
                TeamId("build-team"),
                reordered,
                known_team_ids=(TeamId("build-team"),),
                known_node_ids=(NodeId("main"),),
                known_verification_profiles=(VerificationProfileRef("python-tests"),),
            ),
            result,
        )

    def test_task_graph_rejects_duplicate_self_cyclic_and_unknown_dependencies(
        self,
    ) -> None:
        tasks = (
            spec("A", dependencies=("A", "missing")),
            spec("a"),
            spec("cycle-b", dependencies=("cycle-a",)),
            spec("cycle-a", dependencies=("cycle-b",)),
            spec("bad", dependencies=("bad", "missing")),
        )
        result = validate_task_specs(
            TeamId("build-team"),
            tasks,
            known_team_ids=(TeamId("build-team"),),
            known_node_ids=(NodeId("main"),),
            known_verification_profiles=(VerificationProfileRef("python-tests"),),
        )
        codes = {issue.code for issue in result.errors}

        self.assertTrue(
            {"duplicate-task-id", "self-dependency", "unknown-dependency", "cycle"}
            <= codes
        )

        with self.assertRaisesRegex(TaskPolicyValidationError, "cycle"):
            task_dependency_order(
                (spec("a", dependencies=("b",)), spec("b", dependencies=("a",)))
            )

    def test_task_graph_rejects_unknown_team_node_and_verification_profile(
        self,
    ) -> None:
        task = spec(escalation_node="reviewer")
        result = validate_task_specs(
            TeamId("unknown-team"),
            (task,),
            known_team_ids=(TeamId("build-team"),),
            known_node_ids=(NodeId("main"),),
            known_verification_profiles=(VerificationProfileRef("python-tests"),),
        )
        codes = {issue.code for issue in result.errors}

        self.assertEqual(codes, {"unknown-team", "unknown-node"})

        unknown_profile = parse_task_spec(
            {**valid_task_mapping(), "verification": "missing-profile"}
        )
        result = validate_task_specs(
            TeamId("build-team"),
            (unknown_profile,),
            known_team_ids=(TeamId("build-team"),),
            known_node_ids=(NodeId("main"),),
            known_verification_profiles=(VerificationProfileRef("python-tests"),),
        )
        self.assertEqual({issue.code for issue in result.errors}, {"unknown-profile"})

    def test_task_graph_rejects_duplicate_resource_claims(self) -> None:
        duplicate_resources = TaskSpec(
            task_id=TaskId("build"),
            title="Title build",
            context="A bounded context.",
            goal="Produce the requested result.",
            acceptance=("The result is checked.",),
            allowed_paths=("scripts/agent-team",),
            do_not_modify=(".git",),
            dependencies=(),
            verification=VerificationProfileRef("python-tests"),
            escalation_node=NodeId("main"),
            kind=TaskKind.IMPLEMENTATION,
            lane=TaskLane.NORMAL,
            resource_claims=(ResourceClaim("workspace"), ResourceClaim("WORKSPACE")),
        )
        result = validate_task_specs(
            TeamId("build-team"),
            (duplicate_resources,),
            known_team_ids=(TeamId("build-team"),),
            known_node_ids=(NodeId("main"),),
            known_verification_profiles=(VerificationProfileRef("python-tests"),),
        )
        self.assertEqual(
            {issue.code for issue in result.errors}, {"duplicate-resource-claim"}
        )

    def test_task_canonical_json_is_input_order_independent_and_unicode_safe(
        self,
    ) -> None:
        first = spec("z-task", dependencies=("a-task", "b-task"))
        first_reordered_dependencies = spec("z-task", dependencies=("b-task", "a-task"))
        second = spec("a-task")
        third = spec("b-task")
        reordered = (second, first)
        expected = canonical_task_json(
            TeamId("build-team"),
            (first, second, third),
            known_team_ids=(TeamId("build-team"),),
            known_node_ids=(NodeId("main"),),
            known_verification_profiles=(VerificationProfileRef("python-tests"),),
        )
        actual = canonical_task_json(
            TeamId("build-team"),
            reordered + (third,),
            known_team_ids=(TeamId("build-team"),),
            known_node_ids=(NodeId("main"),),
            known_verification_profiles=(VerificationProfileRef("python-tests"),),
        )
        dependency_reordered = canonical_task_json(
            TeamId("build-team"),
            (first_reordered_dependencies, second, third),
            known_team_ids=(TeamId("build-team"),),
            known_node_ids=(NodeId("main"),),
            known_verification_profiles=(VerificationProfileRef("python-tests"),),
        )

        self.assertEqual(actual, expected)
        self.assertEqual(dependency_reordered, expected)
        self.assertIn('"team_id": "build-team"', actual)
        self.assertEqual(actual, actual.encode("utf-8").decode("utf-8"))
        self.assertEqual(
            json.loads(actual)["dependency_order"], ["a-task", "b-task", "z-task"]
        )

    def test_mixed_non_task_spec_is_invalid_and_canonicalization_does_not_leak_attribute_error(
        self,
    ) -> None:
        tasks = (spec("build"), None)
        result = validate_task_specs(
            TeamId("build-team"),
            tasks,  # type: ignore[arg-type]
            known_team_ids=(TeamId("build-team"),),
            known_node_ids=(NodeId("main"),),
            known_verification_profiles=(VerificationProfileRef("python-tests"),),
        )

        self.assertFalse(result.valid)
        self.assertIn("invalid-task", {issue.code for issue in result.errors})
        with self.assertRaisesRegex(TaskPolicyValidationError, "invalid-task"):
            canonical_task_json(
                TeamId("build-team"),
                tasks,  # type: ignore[arg-type]
                known_team_ids=(TeamId("build-team"),),
                known_node_ids=(NodeId("main"),),
                known_verification_profiles=(VerificationProfileRef("python-tests"),),
            )

    def test_exact_duplicate_group_is_order_independent_and_never_last_wins(
        self,
    ) -> None:
        with_missing_dependency = spec("build", dependencies=("missing",))
        plain = spec("build")

        first = validate_task_specs(
            TeamId("build-team"),
            (with_missing_dependency, plain),
            known_team_ids=(TeamId("build-team"),),
            known_node_ids=(NodeId("main"),),
            known_verification_profiles=(VerificationProfileRef("python-tests"),),
        )
        second = validate_task_specs(
            TeamId("build-team"),
            (plain, with_missing_dependency),
            known_team_ids=(TeamId("build-team"),),
            known_node_ids=(NodeId("main"),),
            known_verification_profiles=(VerificationProfileRef("python-tests"),),
        )

        self.assertEqual(first, second)
        self.assertEqual(
            tuple(issue.code for issue in first.errors), ("duplicate-task-id",)
        )
        for tasks in (
            (with_missing_dependency, plain),
            (plain, with_missing_dependency),
        ):
            with self.assertRaisesRegex(TaskPolicyValidationError, "duplicate-task-id"):
                canonical_task_json(
                    TeamId("build-team"),
                    tasks,
                    known_team_ids=(TeamId("build-team"),),
                    known_node_ids=(NodeId("main"),),
                    known_verification_profiles=(
                        VerificationProfileRef("python-tests"),
                    ),
                )

    def test_invalid_text_enum_and_bounds_fail_fast(self) -> None:
        for field in ("title", "context", "goal"):
            with self.subTest(field=field):
                invalid = spec()
                object.__setattr__(invalid, field, "bad\x1btext")
                with self.assertRaises(TaskPolicyValidationError):
                    TaskSpec(**task_spec_to_dict(invalid))  # type: ignore[arg-type]

        invalid_mapping: dict[str, object] = valid_task_mapping()
        invalid_mapping["kind"] = "unknown"
        with self.assertRaisesRegex(TaskPolicyValidationError, "kind") as caught:
            parse_task_spec(invalid_mapping)
        self.assertEqual(caught.exception.code, "unknown-kind")

        invalid_mapping = valid_task_mapping()
        invalid_mapping["acceptance"] = []
        with self.assertRaisesRegex(TaskPolicyValidationError, "acceptance"):
            parse_task_spec(invalid_mapping)

        invalid_mapping = valid_task_mapping()
        invalid_mapping["title"] = "x" * 4097
        with self.assertRaisesRegex(TaskPolicyValidationError, "maximum"):
            parse_task_spec(invalid_mapping)

    def test_secret_like_text_is_rejected_without_echoing_the_value(self) -> None:
        for field in ("title", "context", "goal"):
            with self.subTest(field=field):
                invalid_mapping = valid_task_mapping()
                invalid_mapping[field] = "OPENAI_API_KEY=super-secret-value"
                with self.assertRaises(TaskPolicyValidationError) as caught:
                    parse_task_spec(invalid_mapping)
                self.assertEqual(caught.exception.code, "secret-like-value")
                self.assertNotIn("super-secret-value", str(caught.exception))

        with self.assertRaises(TaskPolicyValidationError) as caught:
            TaskPolicyStateV4(
                version=4,
                team_id=TeamId("build-team"),
                workspace=WorkspaceIdentity("/workspace/build"),
                sequence=0,
                task_id=TaskId("build"),
                attempt_id=None,
                dispatch_id=None,
                worker_node=None,
                reviewer_node=None,
                review_round=0,
                target_head=None,
                target_tree_digest=None,
                claim_ref=ClaimRef("token=super-secret-value"),
                receipt_ref=None,
                phase=TaskPhase.PENDING,
            )
        self.assertEqual(caught.exception.code, "secret-like-value")
        self.assertNotIn("super-secret-value", str(caught.exception))

    def test_target_head_and_tree_digest_require_canonical_lowercase_hex(self) -> None:
        state_test = TaskPolicyStateTest()
        for head, tree in (
            ("x" * 40, "a" * 64),
            ("A" * 40, "a" * 64),
            ("a" * 40, "x" * 64),
        ):
            with (
                self.subTest(head=head[:4], tree=tree[:4]),
                self.assertRaises(TaskPolicyValidationError),
            ):
                state_test.state(target_head=head, target_tree_digest=tree)

        valid = state_test.state(
            target_head="a" * 40,
            target_tree_digest="b" * 64,
        )
        self.assertEqual(valid.target_head, GitObjectId("a" * 40))
        self.assertEqual(valid.target_tree_digest, TreeDigest("b" * 64))

    def test_nominal_identity_annotations_do_not_reintroduce_plain_str_union(
        self,
    ) -> None:
        task_hints = get_type_hints(TaskSpec)
        state_hints = get_type_hints(TaskPolicyStateV4)
        self.assertNotIn("str", str(task_hints["verification"]))
        self.assertNotIn("str", str(state_hints["team_id"]))
        self.assertNotIn("str", str(state_hints["workspace"]))
        self.assertNotIn("str", str(state_hints["claim_ref"]))

    def test_invalid_team_and_registries_are_stable_validation_results(self) -> None:
        invalid_teams: tuple[object, ...] = ([], {})
        for invalid_team in invalid_teams:
            with self.subTest(team=repr(invalid_team)):
                result = validate_task_specs(
                    cast(TeamId, invalid_team),
                    (spec(),),
                    known_team_ids=(TeamId("build-team"),),
                    known_node_ids=(NodeId("main"),),
                    known_verification_profiles=(
                        VerificationProfileRef("python-tests"),
                    ),
                )
                self.assertFalse(result.valid)
                self.assertIn("invalid-type", {issue.code for issue in result.errors})

        for name, invalid_registry in (("team", None), ("team", 1)):
            with self.subTest(name=name, registry=repr(invalid_registry)):
                result = validate_task_specs(
                    TeamId("build-team"),
                    (spec(),),
                    known_team_ids=cast(Iterable[TeamId], invalid_registry),
                    known_node_ids=(NodeId("main"),),
                    known_verification_profiles=(
                        VerificationProfileRef("python-tests"),
                    ),
                )
                self.assertFalse(result.valid)
                self.assertIn("invalid-type", {issue.code for issue in result.errors})

        for name, invalid_registry in (("node", None), ("node", 1)):
            with self.subTest(name=name, registry=repr(invalid_registry)):
                result = validate_task_specs(
                    TeamId("build-team"),
                    (spec(),),
                    known_team_ids=(TeamId("build-team"),),
                    known_node_ids=cast(Iterable[NodeId], invalid_registry),
                    known_verification_profiles=(
                        VerificationProfileRef("python-tests"),
                    ),
                )
                self.assertFalse(result.valid)
                self.assertIn("invalid-type", {issue.code for issue in result.errors})

        for name, invalid_registry in (("profile", None), ("profile", 1)):
            with self.subTest(name=name, registry=repr(invalid_registry)):
                result = validate_task_specs(
                    TeamId("build-team"),
                    (spec(),),
                    known_team_ids=(TeamId("build-team"),),
                    known_node_ids=(NodeId("main"),),
                    known_verification_profiles=cast(
                        Iterable[VerificationProfileRef], invalid_registry
                    ),
                )
                self.assertFalse(result.valid)
                self.assertIn("invalid-type", {issue.code for issue in result.errors})

    def test_validation_diagnostics_are_bounded_before_exception_serialization(
        self,
    ) -> None:
        tasks = tuple(spec(f"task-{index}") for index in range(256))
        result = validate_task_specs(
            TeamId("build-team"),
            tasks,
            known_team_ids=(TeamId("build-team"),),
            known_node_ids=(NodeId("main"),),
            known_verification_profiles=(),
        )

        self.assertFalse(result.valid)
        self.assertLessEqual(len(result.errors), MAX_VALIDATION_ERRORS)
        self.assertIn("diagnostic-limit", {issue.code for issue in result.errors})


class TaskPolicyStateTest(unittest.TestCase):
    def state(
        self,
        *,
        phase: TaskPhase = TaskPhase.PENDING,
        sequence: int = 0,
        target_head: str | None = None,
        target_tree_digest: str | None = None,
    ) -> TaskPolicyStateV4:
        return TaskPolicyStateV4(
            version=STATE_POLICY_VERSION,
            team_id=TeamId("build-team"),
            workspace=WorkspaceIdentity("/workspace/build"),
            sequence=sequence,
            task_id=TaskId("build"),
            attempt_id=None,
            dispatch_id=None,
            worker_node=None,
            reviewer_node=None,
            review_round=0,
            target_head=(None if target_head is None else GitObjectId(target_head)),
            target_tree_digest=(
                None if target_tree_digest is None else TreeDigest(target_tree_digest)
            ),
            claim_ref=None,
            receipt_ref=None,
            phase=phase,
        )

    def test_state_v4_is_frozen_and_canonical_round_trips_without_defaults(
        self,
    ) -> None:
        state = self.state()
        with self.assertRaises(FrozenInstanceError):
            state.sequence = 1  # type: ignore[misc]

        encoded = canonical_task_state_json(state)
        decoded = parse_task_state(json.loads(encoded))
        self.assertEqual(decoded, state)
        self.assertEqual(task_state_to_dict(decoded), task_state_to_dict(state))
        self.assertEqual(json.loads(encoded)["version"], 4)

    def test_state_v3_and_unknown_versions_fail_at_the_explicit_boundary(self) -> None:
        for version in (3, 5, "4", True):
            with (
                self.subTest(version=repr(version)),
                self.assertRaisesRegex(TaskPolicyValidationError, "version"),
            ):
                parse_task_state(
                    {**task_state_to_dict(self.state()), "version": version}
                )

    def test_state_rejects_unknown_fields_missing_fields_and_unsafe_identity(
        self,
    ) -> None:
        payload = task_state_to_dict(self.state())
        with self.assertRaisesRegex(TaskPolicyValidationError, "unsupported fields"):
            parse_task_state({**payload, "status": "pending"})

        missing = dict(payload)
        del missing["phase"]
        with self.assertRaisesRegex(TaskPolicyValidationError, "missing phase"):
            parse_task_state(missing)

        for workspace in (
            "/workspace/../other",
            "/workspace//other",
            "//workspace/other",
            "/workspace/./other",
            "/workspace/other/",
        ):
            with (
                self.subTest(workspace=workspace),
                self.assertRaises(TaskPolicyValidationError),
            ):
                TaskPolicyStateV4(
                    version=4,
                    team_id=TeamId("build-team"),
                    workspace=WorkspaceIdentity(workspace),
                    sequence=0,
                    task_id=TaskId("build"),
                    attempt_id=None,
                    dispatch_id=None,
                    worker_node=None,
                    reviewer_node=None,
                    review_round=0,
                    target_head=None,
                    target_tree_digest=None,
                    claim_ref=None,
                    receipt_ref=None,
                    phase=TaskPhase.PENDING,
                )

    def test_state_phase_is_closed_and_completed_has_no_public_transition_factory(
        self,
    ) -> None:
        self.assertEqual(
            {phase.value for phase in TaskPhase},
            {
                "pending",
                "assigned",
                "worker_done",
                "review_pending",
                "approved",
                "changes_requested",
                "verifying",
                "completed",
                "failed",
                "ask_user",
                "verification_failed",
            },
        )
        self.assertFalse(hasattr(self.state(), "complete"))
        completed_payload = {
            **task_state_to_dict(self.state()),
            "phase": "completed",
        }
        observation = parse_task_state(completed_payload)
        self.assertIs(observation.phase, TaskPhase.COMPLETED)

    def test_expected_sequence_update_is_typed_and_stale_update_conflicts(self) -> None:
        current = self.state(sequence=4)
        next_state = self.state(sequence=5, phase=TaskPhase.ASSIGNED)
        update = ExpectedSequenceUpdate(expected_sequence=4, state=next_state)

        self.assertEqual(apply_expected_sequence_update(current, update), next_state)
        with self.assertRaisesRegex(TaskPolicyValidationError, "stale sequence"):
            apply_expected_sequence_update(
                current,
                ExpectedSequenceUpdate(expected_sequence=3, state=next_state),
            )

        with self.assertRaisesRegex(TaskPolicyValidationError, "sequence"):
            apply_expected_sequence_update(
                current,
                ExpectedSequenceUpdate(
                    expected_sequence=4, state=self.state(sequence=7)
                ),
            )

    def test_fake_typed_port_does_not_overwrite_on_stale_sequence(self) -> None:
        class FakeStatePort:
            def __init__(self, state: TaskPolicyStateV4) -> None:
                self.state = state

            def update(self, update: ExpectedSequenceUpdate) -> TaskPolicyStateV4:
                self.state = apply_expected_sequence_update(self.state, update)
                return self.state

        port = FakeStatePort(self.state(sequence=2))
        intended = self.state(sequence=3, phase=TaskPhase.ASSIGNED)
        port.update(ExpectedSequenceUpdate(expected_sequence=2, state=intended))
        before_stale = port.state

        with self.assertRaisesRegex(StateConflictError, "stale sequence"):
            port.update(
                ExpectedSequenceUpdate(
                    expected_sequence=2,
                    state=self.state(sequence=3, phase=TaskPhase.WORKER_DONE),
                )
            )
        self.assertEqual(port.state, before_stale)

    def test_expected_sequence_update_cannot_change_state_identity(self) -> None:
        current = self.state(sequence=1)
        different = TaskPolicyStateV4(
            version=4,
            team_id=TeamId("build-team"),
            workspace=WorkspaceIdentity("/workspace/build"),
            sequence=2,
            task_id=TaskId("other"),
            attempt_id=None,
            dispatch_id=None,
            worker_node=None,
            reviewer_node=None,
            review_round=0,
            target_head=None,
            target_tree_digest=None,
            claim_ref=None,
            receipt_ref=None,
            phase=TaskPhase.PENDING,
        )
        with self.assertRaisesRegex(TaskPolicyValidationError, "identity"):
            apply_expected_sequence_update(
                current,
                ExpectedSequenceUpdate(expected_sequence=1, state=different),
            )


if __name__ == "__main__":
    unittest.main()
