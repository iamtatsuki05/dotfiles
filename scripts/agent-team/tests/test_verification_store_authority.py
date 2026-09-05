"""RED tests for the Issue #82 Store/context authority seam.

The positive cases use a real #81 current pair from
``verification_store_fixtures``.  These tests intentionally stop at the
authority/capture boundary; verification lifecycle writes belong to the
follow-up transaction tests.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import pickle
import re
import types
import unittest
from collections.abc import Iterable
from typing import Any, cast
from unittest import mock

from verification_store_fixtures import (
    OWNER_ID,
    actual_review_checkpoint_fixture,
    issue_foreign_handoff_completion_ref,
)

import agent_team.policy_verification_handoff as handoff_module
from agent_team import path_resource_policy
from agent_team import workflow_store as workflow
from agent_team.store import CoordinationStore
from agent_team.task_policy import TaskPhase

_AUTHORITY_ERRORS = (
    AssertionError,
    KeyError,
    LookupError,
    RuntimeError,
    TypeError,
    ValueError,
)
_SHA256_DOMAIN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SNAPSHOT_FIELDS = frozenset(
    {
        "version",
        "review_ref",
        "review_digest",
        "completion_ref",
        "completion_digest",
        "approval_ref",
        "approval_digest",
        "approved_review",
        "task_state_bytes",
        "task_state_digest",
        "binding_digest",
        "root_key",
        "run_id",
        "main_terminal_id",
        "consumer_generation",
        "workflow_sequence",
        "workflow_checkpoint_digest",
        "task_sequence",
        "effect_owner",
    }
)
_BODY_CANARIES = (
    "handoff-authority-canary",
    "A bounded context.",
    "Implement the requested change.",
    "The change is verified.",
    "scripts/agent-team",
    "src/file.txt",
    "cache",
)


def _verification_store_api() -> Any:
    from agent_team import verification_store

    return verification_store


def _read_context(test: unittest.TestCase, fixture: Any) -> tuple[Any, ...]:
    """Read the four-part Store seam with its exact, non-alias contract."""

    method = getattr(fixture.store, "read_verification_context", None)
    test.assertTrue(
        callable(method),
        "missing API: CoordinationStore.read_verification_context",
    )
    if not callable(method):
        raise TypeError("unreachable: read_verification_context is missing")
    result = method(
        fixture.root.root_key,
        fixture.owner_id,
        fixture.final_review_binding,
    )
    test.assertIs(type(result), tuple)
    test.assertEqual(
        4,
        len(result),
        "context read must be (VerificationContextSeed, "
        "VerificationEffectOwner, revision, revision_digest)",
    )
    context, owner, revision, revision_digest = result
    context_type = getattr(_verification_store_api(), "VerificationContextSeed", None)
    owner_type = getattr(_verification_store_api(), "VerificationEffectOwner", None)
    test.assertTrue(inspect.isclass(context_type), "VerificationContextSeed is missing")
    test.assertTrue(inspect.isclass(owner_type), "VerificationEffectOwner is missing")
    if not inspect.isclass(context_type) or not inspect.isclass(owner_type):
        raise AssertionError("verification context authority types are missing")
    test.assertIs(type(context), context_type)
    test.assertIs(type(owner), owner_type)
    test.assertIs(type(revision), int)
    test.assertRegex(revision_digest, _SHA256_DOMAIN)
    return cast(tuple[Any, ...], result)


def _context_field(test: unittest.TestCase, context: Any, name: str) -> Any:
    value = getattr(context, name, None)
    test.assertIsNotNone(value, f"VerificationContextSeed.{name} is missing")
    return value


def _forge_context(context: Any, **changes: object) -> Any:
    """Forge a structurally similar value; Store provenance must reject it."""

    if not dataclasses.is_dataclass(context):
        raise AssertionError("VerificationContextSeed must be a dataclass value")
    forged = object.__new__(type(context))
    for field in dataclasses.fields(context):
        object.__setattr__(
            forged,
            field.name,
            changes.get(field.name, object.__getattribute__(context, field.name)),
        )
    return forged


def _primitive_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, bytes):
        try:
            yield value.decode("utf-8")
        except UnicodeDecodeError:
            return
        return
    if dataclasses.is_dataclass(value):
        for field in dataclasses.fields(value):
            yield from _primitive_strings(getattr(value, field.name))
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            yield from _primitive_strings(key)
            yield from _primitive_strings(nested)
        return
    if isinstance(value, (tuple, list, set, frozenset)):
        for nested in value:
            yield from _primitive_strings(nested)


def _database_projection(store: CoordinationStore) -> tuple[object, ...]:
    """Read every #82-relevant durable row without changing the image."""

    queries = (
        ("store_meta", "SELECT * FROM store_meta ORDER BY key"),
        (
            "workflow_checkpoints",
            "SELECT * FROM workflow_checkpoints ORDER BY root_key",
        ),
        (
            "workflow_events",
            "SELECT * FROM workflow_events ORDER BY workflow_event_id",
        ),
        (
            "task_policy_states",
            "SELECT * FROM task_policy_states ORDER BY root_key, task_id",
        ),
        (
            "verification_operations",
            "SELECT * FROM verification_operations ORDER BY root_key, verification_ref",
        ),
        (
            "verification_receipts",
            "SELECT * FROM verification_receipts ORDER BY root_key, receipt_ref",
        ),
    )
    with store._workflow_read_snapshot() as connection:
        return tuple(
            (name, tuple(tuple(row) for row in connection.execute(sql).fetchall()))
            for name, sql in queries
        )


def _owner_call_projection(owner_store: Any) -> tuple[int, ...]:
    return (
        owner_store.review_read_calls,
        owner_store.completion_read_calls,
        owner_store.review_save_calls,
        owner_store.completion_save_calls,
        owner_store.save_calls,
        owner_store.read_calls,
    )


class VerificationStoreAuthorityRedTests(unittest.TestCase):
    def test_fixture_is_an_actual_reopened_review_pending_approved_pair(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            self.assertGreaterEqual(fixture.current.workflow_sequence, 2)
            self.assertIs(
                fixture.current.workflow_state,
                workflow.CheckpointState.REVIEW_PENDING,
            )
            self.assertIs(
                fixture.observation.task.state.phase,
                TaskPhase.APPROVED,
            )
            self.assertEqual(
                fixture.current.task_sequence, fixture.observation.task.state.sequence
            )
            self.assertEqual(3, len(fixture.observation.events))
            self.assertEqual(
                fixture.predecessor.workflow_sequence + 3,
                fixture.current.workflow_sequence,
            )
            self.assertEqual(1, len(fixture.reservation_port.calls))
            self.assertIsNotNone(fixture.final_review_binding)

    def test_store_context_api_and_return_only_owner_type_are_present(self) -> None:
        api = _verification_store_api()
        context_type = getattr(api, "VerificationContextSeed", None)
        owner_type = getattr(api, "VerificationEffectOwner", None)
        self.assertTrue(
            inspect.isclass(context_type), "VerificationContextSeed is missing"
        )
        self.assertTrue(
            inspect.isclass(owner_type), "VerificationEffectOwner is missing"
        )
        self.assertTrue(
            callable(getattr(CoordinationStore, "read_verification_context", None)),
            "CoordinationStore.read_verification_context is missing",
        )
        self.assertTrue(
            callable(getattr(api, "capture_approval_binding", None)),
            "capture_approval_binding is missing",
        )

    def test_store_issues_full_context_and_effect_owner_at_one_revision(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            context_read = _read_context(self, fixture)
            context, owner, revision, revision_digest = context_read
            current = fixture.current
            task = fixture.observation.task.state
            approved_event = fixture.chain.approved.event

            self.assertEqual(
                fixture.root.root_key, _context_field(self, context, "root_key")
            )
            self.assertEqual(
                current.run.run_id, _context_field(self, context, "run_id")
            )
            self.assertEqual(
                current.run.main_terminal_id,
                _context_field(self, context, "main_terminal_id"),
            )
            self.assertEqual(
                approved_event.worker_terminal_id,
                _context_field(self, context, "worker_terminal_id"),
            )
            self.assertEqual(
                approved_event.reviewer_terminal_id,
                _context_field(self, context, "reviewer_terminal_id"),
            )
            self.assertEqual(
                current.run.consumer_generation,
                _context_field(self, context, "consumer_generation"),
            )
            self.assertEqual(
                current.workflow_sequence,
                _context_field(self, context, "workflow_sequence"),
            )
            self.assertEqual(
                current.checkpoint_digest,
                _context_field(self, context, "workflow_checkpoint_digest"),
            )
            self.assertEqual(task, _context_field(self, context, "task_state"))
            self.assertEqual(
                fixture.observation.task.state_digest,
                _context_field(self, context, "task_state_digest"),
            )
            self.assertEqual(
                task.sequence, _context_field(self, context, "task_sequence")
            )
            effect_owner = _context_field(self, context, "effect_owner")
            self.assertIs(type(effect_owner), str)
            self.assertTrue(effect_owner)
            self.assertEqual(OWNER_ID, getattr(owner, "owner_id", None))
            self.assertEqual(type(revision), int)
            self.assertRegex(revision_digest, _SHA256_DOMAIN)
            self.assertIsNotNone(current.review_authority)
            if current.review_authority is None:
                raise AssertionError("current review authority is missing")
            self.assertEqual(
                fixture.review_refs[-1].reference,
                current.review_authority.reference,
            )
            self.assertEqual(
                current.review_authority.digest,
                fixture.observation.events[-1].evidence_ref,
            )

            repeated = _read_context(self, fixture)
            self.assertEqual(context, repeated[0])
            self.assertEqual(revision, repeated[2])
            self.assertEqual(revision_digest, repeated[3])

    def test_revision_is_owner_independent_and_issued_inside_one_snapshot(
        self,
    ) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            api = _verification_store_api()
            original_issue = api._issue_verification_context
            transaction_states: list[bool] = []

            def observed_issue(*args: object, **kwargs: object) -> object:
                connection = fixture.store._connection
                transaction_states.append(
                    connection is not None and connection.in_transaction
                )
                return original_issue(*args, **kwargs)

            with mock.patch.object(
                api,
                "_issue_verification_context",
                autospec=True,
                side_effect=observed_issue,
            ) as issue:
                first = fixture.store.read_verification_context(
                    fixture.root.root_key,
                    "verification-owner-a",
                    fixture.final_review_binding,
                )
            second = fixture.store.read_verification_context(
                fixture.root.root_key,
                "verification-owner-b",
                fixture.final_review_binding,
            )

            self.assertEqual(1, issue.call_count)
            self.assertEqual([True], transaction_states)
            self.assertEqual(first[2], second[2])
            self.assertEqual(first[3], second[3])
            self.assertNotEqual(first[0].effect_owner, second[0].effect_owner)

    def test_full_owner_and_context_clones_mutation_and_wrong_revision_are_rejected(
        self,
    ) -> None:
        api = _verification_store_api()
        with actual_review_checkpoint_fixture() as fixture:
            context, owner, revision, revision_digest = _read_context(self, fixture)
            mutated = _read_context(self, fixture)
            nested = _read_context(self, fixture)
            before_database = _database_projection(fixture.store)
            before_owner_calls = _owner_call_projection(fixture.owner_store)

            owner_clone = object.__new__(type(owner))
            for field in dataclasses.fields(owner):
                object.__setattr__(owner_clone, field.name, getattr(owner, field.name))
            context_clone = _forge_context(context)
            invalid_reads = (
                (context, owner_clone, revision, revision_digest),
                (context_clone, owner, revision, revision_digest),
                (context, owner, revision + 1, revision_digest),
                (context, owner, revision, "sha256:" + "f" * 64),
            )
            for invalid in invalid_reads:
                with (
                    self.subTest(invalid=invalid),
                    self.assertRaises(_AUTHORITY_ERRORS),
                ):
                    api.capture_approval_binding(
                        fixture.store,
                        fixture.handoff,
                        invalid,
                        fixture.review_refs[-1],
                        fixture.completion_ref,
                    )

            object.__setattr__(mutated[1], "owner_id", "mutated-owner")
            with self.assertRaises(_AUTHORITY_ERRORS):
                api.capture_approval_binding(
                    fixture.store,
                    fixture.handoff,
                    mutated,
                    fixture.review_refs[-1],
                    fixture.completion_ref,
                )

            object.__setattr__(
                nested[0].task_state,
                "sequence",
                nested[0].task_state.sequence + 1,
            )
            with self.assertRaises(_AUTHORITY_ERRORS):
                api.capture_approval_binding(
                    fixture.store,
                    fixture.handoff,
                    nested,
                    fixture.review_refs[-1],
                    fixture.completion_ref,
                )

            self.assertEqual(before_database, _database_projection(fixture.store))
            self.assertEqual(
                before_owner_calls,
                _owner_call_projection(fixture.owner_store),
            )

    def test_effect_owner_is_return_only_and_not_copyable_or_forgeable(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            _context, owner, _revision, _revision_digest = _read_context(self, fixture)
            for operation in (copy.copy, copy.deepcopy, pickle.dumps):
                with self.assertRaises((TypeError, pickle.PicklingError)):
                    operation(owner)

            forged = object.__new__(type(owner))
            with self.assertRaises(_AUTHORITY_ERRORS):
                _verification_store_api().capture_approval_binding(
                    fixture.store,
                    fixture.handoff,
                    (
                        _context,
                        forged,
                        _revision,
                        _revision_digest,
                    ),
                    fixture.review_refs[-1],
                    fixture.completion_ref,
                )

    def test_context_read_rejects_bad_root_owner_binding_and_old_revision(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            store = fixture.store
            read_context = _read_context(self, fixture)
            method = getattr(store, "read_verification_context", None)
            self.assertTrue(
                callable(method),
                "missing API: CoordinationStore.read_verification_context",
            )
            if not callable(method):
                raise TypeError("unreachable: read_verification_context is missing")
            for root_key, owner_id, binding in (
                ("foreign-root", fixture.owner_id, fixture.final_review_binding),
                (fixture.root.root_key, "", fixture.final_review_binding),
                (fixture.root.root_key, fixture.owner_id, object()),
            ):
                with (
                    self.subTest(root_key=root_key, owner_id=owner_id),
                    self.assertRaises(_AUTHORITY_ERRORS),
                ):
                    method(root_key, owner_id, binding)

            old_store = fixture.store
            old_store.close()
            new_store = CoordinationStore(fixture.state_root)
            try:
                with self.assertRaises(_AUTHORITY_ERRORS):
                    _verification_store_api().capture_approval_binding(
                        new_store,
                        fixture.handoff,
                        read_context,
                        fixture.review_refs[-1],
                        fixture.completion_ref,
                    )
            finally:
                new_store.close()

    def test_nonfinal_binding_and_foreign_handoff_completion_are_rejected(
        self,
    ) -> None:
        api = _verification_store_api()
        with actual_review_checkpoint_fixture() as fixture:
            method = fixture.store.read_verification_context
            nonfinal = fixture.handoff._bind_review_authority(
                fixture.chain.review_pending,
                fixture.chain.policy,
                fixture.review_refs[1],
            )
            before_database = _database_projection(fixture.store)
            with self.assertRaises(_AUTHORITY_ERRORS):
                method(fixture.root.root_key, fixture.owner_id, nonfinal)
            self.assertEqual(before_database, _database_projection(fixture.store))

            context_read = _read_context(self, fixture)
            _foreign_handoff, foreign_completion, foreign_reservation = (
                issue_foreign_handoff_completion_ref(fixture)
            )
            reservation_calls = len(foreign_reservation.calls)
            approval_saves = fixture.owner_store.save_calls
            with (
                mock.patch.object(
                    type(fixture.handoff),
                    "compose",
                    autospec=True,
                ) as compose,
                self.assertRaises(_AUTHORITY_ERRORS),
            ):
                api.capture_approval_binding(
                    fixture.store,
                    fixture.handoff,
                    context_read,
                    fixture.review_refs[-1],
                    foreign_completion,
                )
            self.assertEqual(0, compose.call_count)
            self.assertEqual(reservation_calls, len(foreign_reservation.calls))
            self.assertEqual(approval_saves, fixture.owner_store.save_calls)
            self.assertEqual(before_database, _database_projection(fixture.store))

    def test_staged_admission_revalidates_bound_value_and_store_generation(
        self,
    ) -> None:
        api = _verification_store_api()
        with actual_review_checkpoint_fixture() as fixture:
            snapshot, staged = api.capture_approval_binding(
                fixture.store,
                fixture.handoff,
                _read_context(self, fixture),
                fixture.review_refs[-1],
                fixture.completion_ref,
            )
            state = api._STAGED_ADMISSIONS[staged]
            approved = state.bound.approved
            original_digest = approved.authority_digest
            object.__setattr__(approved, "authority_digest", "0" * 64)
            with self.assertRaises(_AUTHORITY_ERRORS):
                staged.resolve(snapshot.approval_ref)
            self.assertFalse(state.consumed)
            object.__setattr__(approved, "authority_digest", original_digest)
            bound = staged.resolve(snapshot.approval_ref)
            self.assertIs(bound, state.bound)
            with self.assertRaises(_AUTHORITY_ERRORS):
                staged.resolve(snapshot.approval_ref)

            closed_snapshot, closed_staged = api.capture_approval_binding(
                fixture.store,
                fixture.handoff,
                _read_context(self, fixture),
                fixture.review_refs[-1],
                fixture.completion_ref,
            )
            fixture.store.close()
            with self.assertRaises(_AUTHORITY_ERRORS):
                closed_staged.resolve(closed_snapshot.approval_ref)

    def test_returned_snapshot_mutation_cannot_replace_capture_baseline(self) -> None:
        api = _verification_store_api()
        with actual_review_checkpoint_fixture() as fixture:
            snapshot, staged = api.capture_approval_binding(
                fixture.store,
                fixture.handoff,
                _read_context(self, fixture),
                fixture.review_refs[-1],
                fixture.completion_ref,
            )
            original_values = {
                "consumer_generation": snapshot.consumer_generation,
                "main_terminal_id": snapshot.main_terminal_id,
                "review_ref": snapshot.review_ref,
                "review_digest": snapshot.review_digest,
                "effect_owner": snapshot.effect_owner,
                "binding_digest": snapshot.binding_digest,
            }
            mutations = (
                {"consumer_generation": snapshot.consumer_generation + 1},
                {"main_terminal_id": "forged-main-terminal"},
                {
                    "review_ref": "forged-review",
                    "review_digest": "f" * 64,
                },
                {"effect_owner": "forged-effect-owner"},
            )
            profiles = mock.Mock()
            profiles.resolve = mock.Mock()
            before_database = _database_projection(fixture.store)
            for changes in mutations:
                with self.subTest(changes=changes):
                    for name, value in changes.items():
                        object.__setattr__(snapshot, name, value)
                    object.__setattr__(
                        snapshot,
                        "binding_digest",
                        api._ledger._snapshot_digest(
                            api._ledger._snapshot_payload_for_digest(snapshot)
                        ),
                    )
                    with self.assertRaises(_AUTHORITY_ERRORS):
                        api.StoreVerificationAdapter.from_capture(
                            fixture.store,
                            snapshot,
                            staged,
                            profiles,
                        )
                    for name, value in original_values.items():
                        object.__setattr__(snapshot, name, value)
                    self.assertEqual(
                        before_database,
                        _database_projection(fixture.store),
                    )

    def test_live_capture_calls_actual_owner_once_and_does_not_route_or_write_store(
        self,
    ) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            api = _verification_store_api()
            capture = getattr(api, "capture_approval_binding", None)
            self.assertTrue(callable(capture), "capture_approval_binding is missing")
            if not callable(capture):
                raise TypeError("unreachable: capture_approval_binding is missing")
            context_read = _read_context(self, fixture)
            before_checkpoint = fixture.store.load_checkpoint(
                workflow.WorkflowRootKey(fixture.root.root_key)
            )
            self.assertEqual(before_checkpoint, fixture.current)
            before_database = _database_projection(fixture.store)
            reservation_calls = len(fixture.reservation_port.calls)
            original_compose = handoff_module.PolicyVerificationHandoff.compose
            original_resolve = handoff_module.PolicyVerificationHandoff.resolve
            with (
                mock.patch.object(
                    handoff_module.PolicyVerificationHandoff,
                    "compose",
                    autospec=True,
                    side_effect=original_compose,
                ) as compose,
                mock.patch.object(
                    handoff_module.PolicyVerificationHandoff,
                    "resolve",
                    autospec=True,
                    side_effect=original_resolve,
                ) as resolve,
                mock.patch.object(
                    path_resource_policy,
                    "route_task",
                    wraps=path_resource_policy.route_task,
                ) as route,
            ):
                trace = mock.Mock()
                trace.attach_mock(compose, "compose")
                trace.attach_mock(resolve, "resolve")
                captured = capture(
                    fixture.store,
                    fixture.handoff,
                    context_read,
                    fixture.review_refs[-1],
                    fixture.completion_ref,
                )

            self.assertEqual(1, compose.call_count)
            self.assertEqual(1, resolve.call_count)
            self.assertIs(compose.call_args.args[1], fixture.review_refs[-1])
            self.assertIs(compose.call_args.args[2], fixture.completion_ref)
            approval_ref = resolve.call_args.args[1]
            self.assertEqual(
                [
                    mock.call.compose(
                        fixture.handoff,
                        fixture.review_refs[-1],
                        fixture.completion_ref,
                    ),
                    mock.call.resolve(fixture.handoff, approval_ref),
                ],
                trace.mock_calls,
            )
            self.assertEqual(0, route.call_count)
            self.assertEqual(reservation_calls, len(fixture.reservation_port.calls))
            self.assertIs(type(captured), tuple)
            self.assertEqual(2, len(captured))
            snapshot, staged = captured
            snapshot_type = getattr(api, "ApprovalBindingSnapshotV1", None)
            staged_type = getattr(api, "StagedStoreApprovalAdmission", None)
            self.assertTrue(
                inspect.isclass(snapshot_type), "ApprovalBindingSnapshotV1 is missing"
            )
            self.assertTrue(
                inspect.isclass(staged_type),
                "StagedStoreApprovalAdmission is missing",
            )
            if inspect.isclass(snapshot_type):
                self.assertIs(type(snapshot), snapshot_type)
            if inspect.isclass(staged_type):
                self.assertIs(type(staged), staged_type)
            context = context_read[0]
            self.assertEqual(fixture.review_refs[-1].reference, snapshot.review_ref)
            self.assertEqual(fixture.review_refs[-1].digest, snapshot.review_digest)
            self.assertEqual(fixture.completion_ref.reference, snapshot.completion_ref)
            self.assertEqual(fixture.completion_ref.digest, snapshot.completion_digest)
            self.assertEqual(approval_ref, snapshot.approval_ref)
            self.assertEqual(context.root_key, snapshot.root_key)
            self.assertEqual(context.run_id, snapshot.run_id)
            self.assertEqual(context.main_terminal_id, snapshot.main_terminal_id)
            self.assertEqual(context.consumer_generation, snapshot.consumer_generation)
            self.assertEqual(context.workflow_sequence, snapshot.workflow_sequence)
            self.assertEqual(
                context.workflow_checkpoint_digest,
                snapshot.workflow_checkpoint_digest,
            )
            self.assertEqual(context.task_sequence, snapshot.task_sequence)
            self.assertEqual(context.task_state_digest, snapshot.task_state_digest)
            self.assertEqual(context.effect_owner, snapshot.effect_owner)
            approved = dict(snapshot.approved_review)
            self.assertEqual(approved["authority_digest"], snapshot.approval_digest)
            self.assertEqual(context.worker_terminal_id, approved["worker_terminal_id"])
            self.assertEqual(
                context.reviewer_terminal_id,
                approved["reviewer_terminal_id"],
            )
            self.assertEqual(context.task_state.team_id, approved["team_id"])
            self.assertEqual(context.task_state.workspace, approved["workspace"])
            self.assertEqual(context.task_state.task_id, approved["task_id"])
            self.assertEqual(
                _SNAPSHOT_FIELDS,
                frozenset(field.name for field in dataclasses.fields(snapshot)),
            )
            self.assertFalse(hasattr(snapshot, "bound"))
            self.assertFalse(hasattr(snapshot, "_issuer"))
            self.assertFalse(hasattr(snapshot, "registry"))
            for value in _primitive_strings(snapshot):
                for canary in _BODY_CANARIES:
                    self.assertNotIn(canary, value)
            self.assertEqual(before_database, _database_projection(fixture.store))
            self.assertEqual(
                fixture.current,
                fixture.store.load_checkpoint(
                    workflow.WorkflowRootKey(fixture.root.root_key)
                ),
            )

    def test_context_mismatch_stops_capture_before_owner_calls_or_store_writes(
        self,
    ) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            api = _verification_store_api()
            capture = getattr(api, "capture_approval_binding", None)
            self.assertTrue(callable(capture), "capture_approval_binding is missing")
            if not callable(capture):
                raise TypeError("unreachable: capture_approval_binding is missing")
            context_read = _read_context(self, fixture)
            context, owner, revision, revision_digest = context_read
            before_database = _database_projection(fixture.store)
            before_owner_calls = _owner_call_projection(fixture.owner_store)
            mutations: tuple[tuple[str, object], ...] = (
                ("root_key", "foreign-root"),
                ("run_id", "foreign-run"),
                ("main_terminal_id", "foreign-main-terminal"),
                ("worker_terminal_id", "foreign-worker-terminal"),
                ("reviewer_terminal_id", "foreign-reviewer-terminal"),
                (
                    "consumer_generation",
                    _context_field(self, context, "consumer_generation") + 1,
                ),
                (
                    "workflow_sequence",
                    _context_field(self, context, "workflow_sequence") + 1,
                ),
                ("workflow_checkpoint_digest", "sha256:" + "d" * 64),
                ("task_state_digest", "sha256:" + "e" * 64),
                ("task_sequence", _context_field(self, context, "task_sequence") + 1),
                ("effect_owner", "foreign-effect-owner"),
            )
            for field, value in mutations:
                with self.subTest(field=field):
                    forged = _forge_context(context, **{field: value})
                    original_compose = handoff_module.PolicyVerificationHandoff.compose
                    original_resolve = handoff_module.PolicyVerificationHandoff.resolve
                    write_transaction = fixture.store._write_transaction
                    with (
                        mock.patch.object(
                            handoff_module.PolicyVerificationHandoff,
                            "compose",
                            autospec=True,
                            side_effect=original_compose,
                        ) as compose,
                        mock.patch.object(
                            handoff_module.PolicyVerificationHandoff,
                            "resolve",
                            autospec=True,
                            side_effect=original_resolve,
                        ) as resolve,
                        mock.patch.object(
                            fixture.store,
                            "_write_transaction",
                            wraps=write_transaction,
                        ) as write,
                        self.assertRaises(_AUTHORITY_ERRORS),
                    ):
                        capture(
                            fixture.store,
                            fixture.handoff,
                            (forged, owner, revision, revision_digest),
                            fixture.review_refs[-1],
                            fixture.completion_ref,
                        )
                    self.assertEqual(0, compose.call_count)
                    self.assertEqual(0, resolve.call_count)
                    self.assertEqual(0, write.call_count)
                    self.assertEqual(
                        before_owner_calls,
                        _owner_call_projection(fixture.owner_store),
                    )
                    self.assertEqual(
                        before_database,
                        _database_projection(fixture.store),
                    )

    def test_foreign_context_and_subclass_owner_cannot_capture(self) -> None:
        with actual_review_checkpoint_fixture() as fixture:
            api = _verification_store_api()
            capture = getattr(api, "capture_approval_binding", None)
            self.assertTrue(callable(capture), "capture_approval_binding is missing")
            if not callable(capture):
                raise TypeError("unreachable: capture_approval_binding is missing")
            context_read = _read_context(self, fixture)
            foreign_owner: object | None = None
            try:
                owner_type = type(context_read[1])
                foreign_owner_type = types.new_class(
                    "ForeignOwner",
                    (owner_type,),
                )
                foreign_owner = object.__new__(foreign_owner_type)
            except (TypeError, ValueError):
                pass
            if foreign_owner is not None:
                with self.assertRaises(_AUTHORITY_ERRORS):
                    capture(
                        fixture.store,
                        fixture.handoff,
                        (
                            context_read[0],
                            foreign_owner,
                            context_read[2],
                            context_read[3],
                        ),
                        fixture.review_refs[-1],
                        fixture.completion_ref,
                    )

            with actual_review_checkpoint_fixture() as foreign:
                foreign_context_read = _read_context(self, foreign)
                with self.assertRaises(_AUTHORITY_ERRORS):
                    capture(
                        fixture.store,
                        fixture.handoff,
                        foreign_context_read,
                        fixture.review_refs[-1],
                        fixture.completion_ref,
                    )


if __name__ == "__main__":
    unittest.main()
