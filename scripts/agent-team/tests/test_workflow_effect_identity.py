"""RED contracts for the private workflow-effect identity seam."""

from __future__ import annotations

import copy
import dataclasses
import pickle
import tempfile
import unittest
from typing import cast

import test_workflow_store_transaction as transaction_fixtures

from agent_team import workflow_effect_adapter as adapter
from agent_team import workflow_store as workflow
from agent_team.contracts import (
    DeliveryAck,
    DeliveryRef,
    Role,
    RolePrompt,
    RoleRead,
    RoleWait,
)

_DIGEST_1 = "sha256:" + "1" * 64
_DIGEST_2 = "sha256:" + "2" * 64


def _root(temporary: str) -> workflow.RootIdentity:
    state_root = transaction_fixtures._make_state_root(temporary)
    return transaction_fixtures._make_root(state_root, temporary)


def _run(*, generation: int = 7, run_id: str = "run-1") -> workflow.RunIdentity:
    return workflow.RunIdentity(
        run_id=run_id,
        main_terminal_id="terminal-main",
        consumer_generation=generation,
    )


def _assignment(
    *,
    run_id: str = "run-1",
    role: workflow.AssignmentRole = workflow.AssignmentRole.WORKER,
    worker_node: str = "worker-node-1",
    task_id: str = "task-1",
    attempt: int = 1,
    dispatch_id: str = "dispatch-1",
    terminal_id: str = "terminal-worker",
) -> workflow.ActiveAssignment:
    completion = workflow.CompletionIdentity(
        run_id=run_id,
        task_id=task_id,
        dispatch_id=dispatch_id,
        sender_terminal_id=terminal_id,
    )
    return workflow.ActiveAssignment(
        role=role,
        worker_node=worker_node,
        task_id=task_id,
        attempt=attempt,
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        launch_mode=workflow.LaunchMode.BARE_BACKGROUND,
        completion_identity=completion,
    )


def _delivery(
    assignment: workflow.ActiveAssignment,
    *,
    delivery_id: str = "delivery-1",
    generation: int = 7,
    message_ids: tuple[str, ...] = ("message-1", "message-2"),
) -> workflow.PendingDelivery:
    projections = tuple(
        workflow.EventProjection(
            kind=workflow.EventProjectionKind.QUESTION,
            message_id=message_id,
            completion_identity=assignment.completion_identity,
            outcome=None,
            body_digest=_DIGEST_1 if index == 0 else _DIGEST_2,
        )
        for index, message_id in enumerate(message_ids)
    )
    return workflow.PendingDelivery(
        delivery_id=delivery_id,
        consumer_generation=generation,
        ordered_message_ids=message_ids,
        ordered_event_projection=projections,
        delivery_digest=workflow.delivery_content_digest(
            delivery_id=delivery_id,
            consumer_generation=generation,
            ordered_message_ids=message_ids,
            ordered_event_projection=projections,
        ),
        ack_operation_id=None,
        ack_status=workflow.AckStatus.PENDING,
    )


def _request(
    command: adapter.EffectCommand,
    *,
    root: workflow.RootIdentity,
    run: workflow.RunIdentity | None,
    assignment: workflow.ActiveAssignment | None,
    pending_delivery: workflow.PendingDelivery | None,
    workflow_sequence: int = 4,
    task_sequence: int | None = 1,
) -> adapter.EffectRequestIdentity:
    return adapter.derive_effect_request_identity(
        command,
        root=root,
        run=run,
        assignment=assignment,
        pending_delivery=pending_delivery,
        expected_workflow_sequence=workflow_sequence,
        expected_task_sequence=task_sequence,
    )


def _issue_authority(
    request: adapter.EffectRequestIdentity,
    **overrides: object,
) -> adapter.WorkflowEffectAuthority:
    values: dict[str, object] = {
        "backend_id": "backend-1",
        "provider_id": "provider-1",
        "owner": "owner-1",
        "lease_epoch": 3,
        "fencing_token": 5,
        "expires_ns": 10_000,
        "authority_ref": "authority-ref-1",
        "proof_ref": "proof-ref-1",
    }
    values.update(overrides)
    return adapter._issue_workflow_effect_authority(
        request,
        backend_id=cast(str, values["backend_id"]),
        provider_id=cast(str, values["provider_id"]),
        owner=cast(str, values["owner"]),
        lease_epoch=cast(int, values["lease_epoch"]),
        fencing_token=cast(int, values["fencing_token"]),
        expires_ns=cast(int, values["expires_ns"]),
        authority_ref=cast(str, values["authority_ref"]),
        proof_ref=cast(str, values["proof_ref"]),
    )


class WorkflowEffectIdentityTests(unittest.TestCase):
    def _wait_request(
        self,
        *,
        root: workflow.RootIdentity,
        run: workflow.RunIdentity | None = None,
        assignment: workflow.ActiveAssignment | None = None,
        command_role: Role = Role.WORKER,
        workflow_sequence: int = 4,
        task_sequence: int | None = 1,
    ) -> adapter.EffectRequestIdentity:
        return _request(
            adapter.make_request_command(RoleWait(command_role, 250)),
            root=root,
            run=_run() if run is None else run,
            assignment=_assignment() if assignment is None else assignment,
            pending_delivery=None,
            workflow_sequence=workflow_sequence,
            task_sequence=task_sequence,
        )

    def test_request_identity_is_deterministic_and_body_free(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-effect-identity-"
        ) as temporary:
            root = _root(temporary)
            command = adapter.make_request_command(
                RolePrompt(Role.WORKER, "raw-body-canary")
            )
            first = _request(
                command,
                root=root,
                run=_run(),
                assignment=None,
                pending_delivery=None,
                task_sequence=None,
            )
            second = _request(
                adapter.make_request_command(
                    RolePrompt(Role.WORKER, "raw-body-canary")
                ),
                root=root,
                run=_run(),
                assignment=None,
                pending_delivery=None,
                task_sequence=None,
            )
            self.assertEqual(first, second)
            self.assertTrue(dataclasses.is_dataclass(first))
            for representation in (
                repr(first),
                str(first),
                repr(dataclasses.asdict(first)),
            ):
                self.assertNotIn("raw-body-canary", representation)
                self.assertNotIn("raw-output-canary", representation)

    def test_authority_is_return_only_issuer_bound_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-effect-authority-"
        ) as temporary:
            request = self._wait_request(root=_root(temporary))
            authority = _issue_authority(request)
            self.assertIsInstance(authority, adapter.WorkflowEffectAuthority)
            adapter.validate_authority(authority, request=request)

            with self.assertRaises(TypeError):
                adapter.WorkflowEffectAuthority()
            with self.assertRaises(TypeError):
                adapter.WorkflowEffectAuthority.__new__(adapter.WorkflowEffectAuthority)

            for operation in (
                lambda: copy.copy(authority),
                lambda: copy.deepcopy(authority),
                lambda: pickle.loads(pickle.dumps(authority)),
            ):
                with (
                    self.subTest(operation=operation),
                    self.assertRaises((TypeError, ValueError)),
                ):
                    operation()

            foreign_request = self._wait_request(
                root=dataclasses.replace(request.root, root_key="root-foreign")
            )
            foreign = _issue_authority(foreign_request)
            with self.assertRaises(
                (TypeError, ValueError, workflow.OperationIdentityConflict)
            ):
                adapter.validate_authority(foreign, request=request)

            for field_name in (
                "owner",
                "lease_epoch",
                "fencing_token",
                "authority_ref",
                "proof_ref",
                "_issuer",
            ):
                mutated = _issue_authority(request)
                try:
                    original = getattr(mutated, field_name)
                except AttributeError:
                    original = object()
                replacement: object = (
                    f"mutated-{field_name}"
                    if isinstance(original, str)
                    else object()
                    if field_name == "_issuer"
                    else int(original) + 1
                )
                try:
                    object.__setattr__(mutated, field_name, replacement)
                except (AttributeError, TypeError, dataclasses.FrozenInstanceError):
                    continue
                with (
                    self.subTest(field=field_name),
                    self.assertRaises(
                        (TypeError, ValueError, workflow.OperationIdentityConflict)
                    ),
                ):
                    adapter.validate_authority(mutated, request=request)

    def test_effect_identity_is_return_only_and_revalidated_from_sources(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-effect-issued-"
        ) as temporary:
            request = self._wait_request(root=_root(temporary))
            authority = _issue_authority(request)
            identity = adapter.derive_effect_identity(request, authority)
            adapter.validate_effect_identity(
                identity,
                request=request,
                authority=authority,
            )
            with self.assertRaises(TypeError):
                adapter.EffectIdentity(
                    "operation-forged",
                    "effect/forged",
                    _DIGEST_1,
                    _DIGEST_1,
                    _DIGEST_1,
                    _DIGEST_1,
                )
            for operation in (
                lambda: copy.copy(identity),
                lambda: copy.deepcopy(identity),
                lambda: pickle.loads(pickle.dumps(identity)),
            ):
                with self.assertRaises((TypeError, ValueError)):
                    operation()
            mutated = adapter.derive_effect_identity(request, authority)
            object.__setattr__(mutated, "operation_id", "operation-forged")
            with self.assertRaises(workflow.OperationIdentityConflict):
                adapter.validate_effect_identity(
                    mutated,
                    request=request,
                    authority=authority,
                )

    def test_authority_expiry_is_checked_without_changing_stable_identity(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-effect-expiry-"
        ) as temporary:
            request = self._wait_request(root=_root(temporary))
            authority = _issue_authority(request, expires_ns=10_000)
            adapter.require_live_authority(authority, request=request, now_ns=9_999)
            for now_ns in (10_000, 10_001):
                with (
                    self.subTest(now_ns=now_ns),
                    self.assertRaises(workflow.OperationIdentityConflict),
                ):
                    adapter.require_live_authority(
                        authority,
                        request=request,
                        now_ns=now_ns,
                    )

    def test_effect_identity_changes_for_request_context_and_authority_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-effect-sensitivity-"
        ) as temporary:
            root = _root(temporary)
            assignment = _assignment()
            baseline = self._wait_request(root=root, assignment=assignment)
            baseline_authority = _issue_authority(baseline)
            baseline_effect = adapter.derive_effect_identity(
                baseline, baseline_authority
            )

            request_variants: list[tuple[str, adapter.EffectRequestIdentity]] = [
                (
                    "root-key",
                    self._wait_request(
                        root=dataclasses.replace(root, root_key="root-2"),
                        assignment=assignment,
                    ),
                ),
                (
                    "workspace-identity",
                    self._wait_request(
                        root=dataclasses.replace(
                            root,
                            workspace=workflow.PathIdentity(
                                "/tmp/workspace-2", root.workspace.device, 99
                            ),
                        ),
                        assignment=assignment,
                    ),
                ),
                (
                    "config-identity",
                    self._wait_request(
                        root=dataclasses.replace(
                            root,
                            config_path="/tmp/config-2.toml",
                            config_inode=root.config_inode + 1,
                        ),
                        assignment=assignment,
                    ),
                ),
                (
                    "state-root-identity",
                    self._wait_request(
                        root=dataclasses.replace(
                            root,
                            state_root=workflow.PathIdentity(
                                "/tmp/state-2", root.state_root.device, 99
                            ),
                        ),
                        assignment=assignment,
                    ),
                ),
                (
                    "workflow-sequence",
                    self._wait_request(
                        root=root,
                        assignment=assignment,
                        workflow_sequence=5,
                    ),
                ),
                (
                    "task-sequence",
                    self._wait_request(
                        root=root,
                        assignment=assignment,
                        task_sequence=2,
                    ),
                ),
                (
                    "run",
                    self._wait_request(
                        root=root,
                        assignment=_assignment(run_id="run-2"),
                        run=_run(run_id="run-2"),
                    ),
                ),
                (
                    "assignment-role",
                    self._wait_request(
                        root=root,
                        assignment=_assignment(role=workflow.AssignmentRole.REVIEWER),
                        command_role=Role.REVIEWER,
                    ),
                ),
                (
                    "assignment-worker-node",
                    self._wait_request(
                        root=root,
                        assignment=_assignment(worker_node="worker-node-2"),
                    ),
                ),
                (
                    "assignment-task",
                    self._wait_request(
                        root=root,
                        assignment=_assignment(task_id="task-2"),
                    ),
                ),
                (
                    "assignment-dispatch",
                    self._wait_request(
                        root=root,
                        assignment=_assignment(dispatch_id="dispatch-2"),
                    ),
                ),
                (
                    "assignment-attempt",
                    self._wait_request(
                        root=root,
                        assignment=_assignment(attempt=2),
                    ),
                ),
                (
                    "assignment-terminal",
                    self._wait_request(
                        root=root,
                        assignment=_assignment(terminal_id="terminal-worker-2"),
                    ),
                ),
            ]
            for field_name, variant in request_variants:
                with self.subTest(field=field_name):
                    effect = adapter.derive_effect_identity(
                        variant, _issue_authority(variant)
                    )
                    self.assertNotEqual(
                        baseline_effect.operation_id, effect.operation_id
                    )
                    self.assertNotEqual(baseline_effect.effect_key, effect.effect_key)

            prompt = _request(
                adapter.make_request_command(RolePrompt(Role.WORKER, "prompt-a")),
                root=root,
                run=_run(),
                assignment=None,
                pending_delivery=None,
                task_sequence=None,
            )
            prompt_changed = _request(
                adapter.make_request_command(RolePrompt(Role.WORKER, "prompt-b")),
                root=root,
                run=_run(),
                assignment=None,
                pending_delivery=None,
                task_sequence=None,
            )
            first_prompt_effect = adapter.derive_effect_identity(
                prompt, _issue_authority(prompt)
            )
            changed_prompt_effect = adapter.derive_effect_identity(
                prompt_changed, _issue_authority(prompt_changed)
            )
            self.assertNotEqual(
                first_prompt_effect.operation_id, changed_prompt_effect.operation_id
            )
            self.assertNotEqual(
                first_prompt_effect.effect_key, changed_prompt_effect.effect_key
            )

            read = _request(
                adapter.make_request_command(RoleRead(Role.WORKER, 10)),
                root=root,
                run=_run(),
                assignment=assignment,
                pending_delivery=_delivery(assignment),
            )
            read_effect = adapter.derive_effect_identity(read, _issue_authority(read))
            self.assertNotEqual(baseline_effect.operation_id, read_effect.operation_id)
            self.assertNotEqual(baseline_effect.effect_key, read_effect.effect_key)

            delivery = _delivery(assignment)
            delivery_request = _request(
                adapter.make_request_command(
                    DeliveryAck(DeliveryRef(delivery.delivery_id))
                ),
                root=root,
                run=_run(),
                assignment=assignment,
                pending_delivery=delivery,
            )
            delivery_effect = adapter.derive_effect_identity(
                delivery_request, _issue_authority(delivery_request)
            )
            reordered = _delivery(
                assignment,
                message_ids=("message-2", "message-1"),
            )
            reordered_request = _request(
                adapter.make_request_command(
                    DeliveryAck(DeliveryRef(reordered.delivery_id))
                ),
                root=root,
                run=_run(),
                assignment=assignment,
                pending_delivery=reordered,
            )
            reordered_effect = adapter.derive_effect_identity(
                reordered_request, _issue_authority(reordered_request)
            )
            self.assertNotEqual(
                delivery_effect.operation_id, reordered_effect.operation_id
            )
            self.assertNotEqual(delivery_effect.effect_key, reordered_effect.effect_key)

            other_delivery = _delivery(assignment, delivery_id="delivery-2")
            other_delivery_request = _request(
                adapter.make_request_command(
                    DeliveryAck(DeliveryRef(other_delivery.delivery_id))
                ),
                root=root,
                run=_run(),
                assignment=assignment,
                pending_delivery=other_delivery,
            )
            other_delivery_effect = adapter.derive_effect_identity(
                other_delivery_request, _issue_authority(other_delivery_request)
            )
            self.assertNotEqual(
                delivery_effect.operation_id, other_delivery_effect.operation_id
            )
            self.assertNotEqual(
                delivery_effect.effect_key, other_delivery_effect.effect_key
            )

            for field_name, overrides in (
                ("owner", {"owner": "owner-2"}),
                ("lease-epoch", {"lease_epoch": 4}),
                ("fencing-token", {"fencing_token": 6}),
                ("proof", {"proof_ref": "proof-ref-2"}),
                ("backend", {"backend_id": "backend-2"}),
                ("provider", {"provider_id": "provider-2"}),
            ):
                with self.subTest(authority_field=field_name):
                    effect = adapter.derive_effect_identity(
                        baseline, _issue_authority(baseline, **overrides)
                    )
                    self.assertNotEqual(
                        baseline_effect.operation_id, effect.operation_id
                    )
                    self.assertNotEqual(baseline_effect.effect_key, effect.effect_key)

            later_expiry = adapter.derive_effect_identity(
                baseline, _issue_authority(baseline, expires_ns=20_000)
            )
            self.assertEqual(baseline_effect.operation_id, later_expiry.operation_id)
            self.assertEqual(baseline_effect.effect_key, later_expiry.effect_key)

            higher_generation = self._wait_request(
                root=root,
                run=_run(generation=8),
                assignment=assignment,
            )
            generation_effect = adapter.derive_effect_identity(
                higher_generation, _issue_authority(higher_generation)
            )
            self.assertNotEqual(
                baseline_effect.operation_id, generation_effect.operation_id
            )
            self.assertNotEqual(
                baseline_effect.effect_key, generation_effect.effect_key
            )

    def test_invalid_action_and_context_are_rejected_before_identity_derivation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-effect-invalid-"
        ) as temporary:
            root = _root(temporary)
            with self.assertRaises(
                (TypeError, ValueError, workflow.OperationIdentityConflict)
            ):
                _request(
                    cast(adapter.EffectCommand, object()),
                    root=root,
                    run=_run(),
                    assignment=None,
                    pending_delivery=None,
                    task_sequence=None,
                )
            with self.assertRaises(
                (TypeError, ValueError, workflow.OperationIdentityConflict)
            ):
                _request(
                    adapter.make_request_command(RoleWait(Role.WORKER, 250)),
                    root=root,
                    run=None,
                    assignment=_assignment(),
                    pending_delivery=None,
                )
            with self.assertRaises(
                (TypeError, ValueError, workflow.OperationIdentityConflict)
            ):
                _request(
                    adapter.make_stop_command(),
                    root=root,
                    run=_run(),
                    assignment=_assignment(),
                    pending_delivery=None,
                    task_sequence=None,
                )
            with self.assertRaises(
                (TypeError, ValueError, workflow.OperationIdentityConflict)
            ):
                self._wait_request(root=root, workflow_sequence=-1)


if __name__ == "__main__":
    unittest.main()
