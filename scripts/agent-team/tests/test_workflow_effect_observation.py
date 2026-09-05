"""RED contracts for the private backend-effect observation seam."""

from __future__ import annotations

import copy
import dataclasses
import pickle
import tempfile
import unittest
from pathlib import Path
from typing import cast

import test_workflow_store_transaction as transaction_fixtures

from agent_team import workflow_effect_adapter as adapter
from agent_team import workflow_store as workflow
from agent_team.contracts import (
    AckReceipt,
    Assignment,
    CompletionIdentity,
    DeliveryAck,
    DeliveryRef,
    DispatchRef,
    LaunchMode,
    MessageRef,
    MessageReply,
    NormalizedEvent,
    Outcome,
    ReadReceipt,
    ReleaseReceipt,
    ReplyReceipt,
    Role,
    RolePrompt,
    RoleRead,
    RoleRelease,
    RoleWait,
    RunRef,
    StartResult,
    StartSpec,
    StopResult,
    TaskRef,
    TerminalRef,
    WaitReceipt,
)

_DIGEST_1 = "sha256:" + "1" * 64
_DIGEST_2 = "sha256:" + "2" * 64
_RAW_BODY = "raw-body-canary"
_RAW_OUTPUT = "raw-output-canary\n二行目"


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
    task_id: str = "task-1",
    attempt: int = 1,
) -> workflow.ActiveAssignment:
    completion = workflow.CompletionIdentity(
        run_id=run_id,
        task_id=task_id,
        dispatch_id="dispatch-1",
        sender_terminal_id="terminal-worker",
    )
    return workflow.ActiveAssignment(
        role=workflow.AssignmentRole.WORKER,
        worker_node="worker-node-1",
        task_id=task_id,
        attempt=attempt,
        dispatch_id="dispatch-1",
        terminal_id="terminal-worker",
        launch_mode=workflow.LaunchMode.BARE_BACKGROUND,
        completion_identity=completion,
    )


def _event_body(kind: workflow.EventProjectionKind) -> str:
    return (
        _RAW_BODY
        if kind is workflow.EventProjectionKind.QUESTION
        else "worker-done-body"
    )


def _delivery(
    assignment: workflow.ActiveAssignment,
    *,
    kind: workflow.EventProjectionKind = workflow.EventProjectionKind.QUESTION,
    delivery_id: str = "delivery-1",
    generation: int = 7,
) -> workflow.PendingDelivery:
    projection = workflow.EventProjection(
        kind=kind,
        message_id=(
            "message-1" if kind is workflow.EventProjectionKind.QUESTION else None
        ),
        completion_identity=assignment.completion_identity,
        outcome=(
            None
            if kind is not workflow.EventProjectionKind.WORKER_DONE
            else workflow.EventOutcome.SUCCEEDED
        ),
        body_digest=workflow.digest_bounded_body(_event_body(kind).encode("utf-8")),
    )
    message_ids = ("message-1",) if projection.message_id is not None else ()
    return workflow.PendingDelivery(
        delivery_id=delivery_id,
        consumer_generation=generation,
        ordered_message_ids=message_ids,
        ordered_event_projection=(projection,),
        delivery_digest=workflow.delivery_content_digest(
            delivery_id=delivery_id,
            consumer_generation=generation,
            ordered_message_ids=message_ids,
            ordered_event_projection=(projection,),
        ),
        ack_operation_id=None,
        ack_status=workflow.AckStatus.PENDING,
    )


def _start_spec(root: workflow.RootIdentity) -> StartSpec:
    return StartSpec(
        team_id=root.team_id,
        workspace=Path(root.workspace.path),
        config_path=Path(root.config_path),
        state_path=Path(root.state_root.path),
        role_specs={},
    )


def _request(
    command: adapter.EffectCommand,
    *,
    root: workflow.RootIdentity,
    run: workflow.RunIdentity | None,
    assignment: workflow.ActiveAssignment | None,
    delivery: workflow.PendingDelivery | None,
    workflow_sequence: int = 4,
    task_sequence: int | None = 1,
) -> adapter.EffectRequestIdentity:
    return adapter.derive_effect_request_identity(
        command,
        root=root,
        run=run,
        assignment=assignment,
        pending_delivery=delivery,
        expected_workflow_sequence=workflow_sequence,
        expected_task_sequence=task_sequence,
    )


def _authority(
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
        "proof_ref": "authority-proof-1",
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


def _public_event(
    assignment: workflow.ActiveAssignment,
    delivery: workflow.PendingDelivery,
) -> NormalizedEvent:
    identity = CompletionIdentity(
        run_id=RunRef(assignment.completion_identity.run_id),
        task_id=TaskRef(assignment.task_id),
        dispatch_id=DispatchRef(assignment.dispatch_id),
        sender_terminal_id=TerminalRef(assignment.terminal_id),
    )
    delivery_ref = DeliveryRef(delivery.delivery_id)
    if (
        delivery.ordered_event_projection[0].kind
        is workflow.EventProjectionKind.QUESTION
    ):
        return NormalizedEvent.question(
            identity=identity,
            message_id=MessageRef("message-1"),
            delivery_id=delivery_ref,
            body=_event_body(delivery.ordered_event_projection[0].kind),
        )
    return NormalizedEvent.worker_done(
        identity=identity,
        outcome=Outcome.SUCCEEDED,
        body=_event_body(delivery.ordered_event_projection[0].kind),
        delivery_id=delivery_ref,
    )


def _public_assignment(assignment: workflow.ActiveAssignment) -> Assignment:
    return Assignment(
        role=Role.WORKER,
        launch_mode=LaunchMode.BARE_BACKGROUND,
        task_id=TaskRef(assignment.task_id),
        dispatch_id=DispatchRef(assignment.dispatch_id),
        terminal_id=TerminalRef(assignment.terminal_id),
        completion_identity=CompletionIdentity(
            run_id=RunRef(assignment.completion_identity.run_id),
            task_id=TaskRef(assignment.task_id),
            dispatch_id=DispatchRef(assignment.dispatch_id),
            sender_terminal_id=TerminalRef(assignment.terminal_id),
        ),
    )


def _set_frozen_stage_status(stage: adapter.CompositeStopStage) -> None:
    """Attempt the forbidden mutation through the runtime attribute protocol."""
    stage.__setattr__("status", "FAILED")


class WorkflowEffectObservationTests(unittest.TestCase):
    def _case(
        self,
        action: workflow.OperationAction,
        *,
        root: workflow.RootIdentity,
    ) -> tuple[
        adapter.EffectRequestIdentity,
        workflow.RunIdentity,
        workflow.ActiveAssignment | None,
        workflow.PendingDelivery | None,
        adapter.EffectPublicResult,
        adapter.WorkflowEffectAuthority,
        adapter.EffectIdentity,
    ]:
        run = _run()
        initial_assignment = _assignment()
        assignment: workflow.ActiveAssignment | None = initial_assignment
        delivery: workflow.PendingDelivery | None = None
        if action is workflow.OperationAction.START:
            spec = _start_spec(root)
            command = adapter.make_start_command(spec)
            request = _request(
                command,
                root=root,
                run=None,
                assignment=None,
                delivery=None,
                workflow_sequence=0,
                task_sequence=None,
            )
            public_result: adapter.EffectPublicResult = StartResult(
                "team-1", RunRef("run-1"), TerminalRef("terminal-main"), spec.state_path
            )
            assignment = None
        elif action is workflow.OperationAction.PROMPT:
            command = adapter.make_request_command(RolePrompt(Role.WORKER, _RAW_BODY))
            request = _request(
                command,
                root=root,
                run=run,
                assignment=None,
                delivery=None,
                task_sequence=None,
            )
            public_result = _public_assignment(initial_assignment)
            assignment = _assignment()
        elif action is workflow.OperationAction.WAIT:
            command = adapter.make_request_command(RoleWait(Role.WORKER, 250))
            request = _request(
                command, root=root, run=run, assignment=assignment, delivery=None
            )
            public_result = WaitReceipt(None, ())
        elif action is workflow.OperationAction.REPLY:
            delivery = _delivery(initial_assignment)
            command = adapter.make_request_command(
                MessageReply(MessageRef("message-1"), _RAW_BODY)
            )
            request = _request(
                command, root=root, run=run, assignment=assignment, delivery=delivery
            )
            public_result = ReplyReceipt(True)
        elif action is workflow.OperationAction.READ:
            delivery = _delivery(
                initial_assignment, kind=workflow.EventProjectionKind.WORKER_DONE
            )
            command = adapter.make_request_command(RoleRead(Role.WORKER, 8))
            request = _request(
                command, root=root, run=run, assignment=assignment, delivery=delivery
            )
            public_result = ReadReceipt(_RAW_OUTPUT)
        elif action is workflow.OperationAction.RELEASE:
            delivery = _delivery(
                initial_assignment, kind=workflow.EventProjectionKind.WORKER_DONE
            )
            command = adapter.make_request_command(RoleRelease(Role.WORKER))
            request = _request(
                command, root=root, run=run, assignment=assignment, delivery=delivery
            )
            public_result = ReleaseReceipt("released")
        elif action is workflow.OperationAction.ACK:
            delivery = _delivery(initial_assignment)
            command = adapter.make_request_command(
                DeliveryAck(DeliveryRef("delivery-1"))
            )
            request = _request(
                command, root=root, run=run, assignment=assignment, delivery=delivery
            )
            public_result = AckReceipt(True)
        else:
            command = adapter.make_stop_command()
            request = _request(
                command, root=root, run=run, assignment=None, delivery=None
            )
            public_result = StopResult("team-1", RunRef("run-1"))
            assignment = None
        authority = _authority(request)
        identity = adapter.derive_effect_identity(request, authority)
        return request, run, assignment, delivery, public_result, authority, identity

    def test_observation_is_return_only_issuer_bound_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="effect-observation-") as temporary:
            root = _root(temporary)
            (
                request,
                run,
                assignment,
                delivery,
                public_result,
                authority,
                identity,
            ) = self._case(workflow.OperationAction.READ, root=root)
            observation = adapter._issue_backend_effect_observation(
                request,
                identity,
                authority,
                run=run,
                assignment=assignment,
                delivery=delivery,
                public_result=public_result,
                effect_ref="provider-effect-1",
                provider_proof_ref="provider-proof-1",
            )
            self.assertIsInstance(observation, adapter.BackendEffectObservation)
            adapter.validate_observation(
                observation, request=request, identity=identity, authority=authority
            )
            self.assertTrue(dataclasses.is_dataclass(observation))
            self.assertFalse(hasattr(observation, "__dict__"))
            self.assertNotIn(_RAW_OUTPUT, repr(observation))
            for operation in (
                lambda: copy.copy(observation),
                lambda: copy.deepcopy(observation),
                lambda: pickle.loads(pickle.dumps(observation)),
            ):
                with self.assertRaises((TypeError, ValueError)):
                    operation()
            with self.assertRaises(TypeError):
                adapter.BackendEffectObservation()
            with self.assertRaises(TypeError):
                adapter.BackendEffectObservation.__new__(
                    adapter.BackendEffectObservation
                )

            forged = object.__new__(adapter.BackendEffectObservation)
            for field in dataclasses.fields(observation):
                object.__setattr__(forged, field.name, getattr(observation, field.name))
            object.__setattr__(forged, "_issuer", object())
            with self.assertRaises(
                (TypeError, ValueError, workflow.OperationIdentityConflict)
            ):
                adapter.validate_observation(
                    forged, request=request, identity=identity, authority=authority
                )

    def test_observation_binds_all_identity_fields_and_rejects_low_level_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="effect-observation-binding-"
        ) as temporary:
            root = _root(temporary)
            (
                request,
                run,
                assignment,
                delivery,
                public_result,
                authority,
                identity,
            ) = self._case(workflow.OperationAction.REPLY, root=root)
            assert assignment is not None
            observation = adapter._issue_backend_effect_observation(
                request,
                identity,
                authority,
                run=run,
                assignment=assignment,
                delivery=delivery,
                public_result=public_result,
                effect_ref="provider-effect-1",
                provider_proof_ref="provider-proof-1",
            )
            fields = {
                "operation_id": "operation-foreign",
                "effect_key": "effect/foreign",
                "action": workflow.OperationAction.READ,
                "request_digest": _DIGEST_2,
                "root_key": "root-foreign",
                "run_id": "run-foreign",
                "main_terminal_id": "terminal-foreign",
                "backend_id": "backend-foreign",
                "provider_id": "provider-foreign",
                "consumer_generation": 8,
                "owner": "owner-foreign",
                "lease_epoch": 4,
                "fencing_token": 6,
                "effect_ref": "provider-effect-foreign",
                "provider_proof_ref": "provider-proof-foreign",
                "result_kind": "read_output",
                "result_digest": _DIGEST_2,
                "evidence_ref": _DIGEST_2,
                "assignment": dataclasses.replace(assignment, attempt=2),
                "delivery": _delivery(assignment, delivery_id="delivery-foreign"),
                "message_id": "message-2",
            }
            for field_name, replacement in fields.items():
                mutated = object.__new__(adapter.BackendEffectObservation)
                for field in dataclasses.fields(observation):
                    object.__setattr__(
                        mutated, field.name, getattr(observation, field.name)
                    )
                object.__setattr__(mutated, field_name, replacement)
                with (
                    self.subTest(field=field_name),
                    self.assertRaises(
                        (TypeError, ValueError, workflow.OperationIdentityConflict)
                    ),
                ):
                    adapter.validate_observation(
                        mutated, request=request, identity=identity, authority=authority
                    )

    def test_action_result_matrix_has_fixed_kind_and_digest_semantics(self) -> None:
        cases = (
            (workflow.OperationAction.START, "started"),
            (workflow.OperationAction.PROMPT, "assignment"),
            (workflow.OperationAction.WAIT, "timeout"),
            (workflow.OperationAction.REPLY, "reply"),
            (workflow.OperationAction.READ, "read_output"),
            (workflow.OperationAction.RELEASE, "release"),
            (workflow.OperationAction.ACK, "ack"),
            (workflow.OperationAction.STOP, "stopped_composite"),
        )
        with tempfile.TemporaryDirectory(
            prefix="effect-observation-matrix-"
        ) as temporary:
            root = _root(temporary)
            for action, result_kind in cases:
                with self.subTest(action=action.value):
                    (
                        request,
                        run,
                        assignment,
                        delivery,
                        public_result,
                        authority,
                        identity,
                    ) = self._case(action, root=root)
                    composite = None
                    if action is workflow.OperationAction.STOP:
                        stage = adapter.CompositeStopStage(
                            stage_id="stage-1",
                            resource_ref="resource-1",
                            effect_ref="stage-effect-1",
                            status="COMPLETED",
                            evidence_digest=_DIGEST_1,
                        )
                        composite = adapter.make_composite_stop_observation(
                            (stage,), composite_ref="composite-stop-1"
                        )
                    observation = adapter._issue_backend_effect_observation(
                        request,
                        identity,
                        authority,
                        run=run,
                        assignment=assignment,
                        delivery=delivery,
                        public_result=public_result,
                        effect_ref="provider-effect-1",
                        provider_proof_ref="provider-proof-1",
                        composite_stop=composite,
                    )
                    self.assertEqual(result_kind, observation.result_kind)
                    self.assertTrue(observation.result_digest.startswith("sha256:"))
                    self.assertEqual(identity.operation_id, observation.operation_id)
                    self.assertEqual(identity.effect_key, observation.effect_key)
                    self.assertEqual(request.request_digest, observation.request_digest)
                    self.assertEqual(root.root_key, observation.root_key)
                    self.assertEqual(authority.backend_id, observation.backend_id)
                    self.assertEqual(authority.provider_id, observation.provider_id)
                    self.assertEqual(authority.owner, observation.owner)
                    self.assertEqual(authority.fencing_token, observation.fencing_token)
                    self.assertIs(public_result, observation.public_result)

    def test_wait_delivery_batch_and_read_output_remain_transient_and_exact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="effect-observation-transient-"
        ) as temporary:
            root = _root(temporary)
            run = _run()
            assignment = _assignment()
            delivery = _delivery(assignment)
            request = _request(
                adapter.make_request_command(RoleWait(Role.WORKER, 250)),
                root=root,
                run=run,
                assignment=assignment,
                delivery=None,
            )
            authority = _authority(request)
            identity = adapter.derive_effect_identity(request, authority)
            wait_result = WaitReceipt(
                DeliveryRef(delivery.delivery_id),
                (_public_event(assignment, delivery),),
            )
            observation = adapter._issue_backend_effect_observation(
                request,
                identity,
                authority,
                run=run,
                assignment=assignment,
                delivery=delivery,
                public_result=wait_result,
                effect_ref="provider-effect-wait",
                provider_proof_ref="provider-proof-wait",
            )
            self.assertIsNone(observation.message_id)
            assert observation.delivery is not None
            self.assertEqual(
                delivery.ordered_message_ids, observation.delivery.ordered_message_ids
            )
            self.assertEqual(delivery.delivery_digest, observation.result_digest)
            self.assertNotIn(_RAW_BODY, repr(observation))
            self.assertEqual(
                _public_event(assignment, delivery).body, wait_result.events[0].body
            )

    def test_read_output_accepts_one_mib_utf8_and_rejects_one_byte_over(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="effect-observation-read-bound-"
        ) as temporary:
            root = _root(temporary)
            request, run, assignment, delivery, _, authority, identity = self._case(
                workflow.OperationAction.READ, root=root
            )
            accepted = "a" * workflow.MAX_CHECKPOINT_BYTES
            accepted_observation = adapter._issue_backend_effect_observation(
                request,
                identity,
                authority,
                run=run,
                assignment=assignment,
                delivery=delivery,
                public_result=ReadReceipt(accepted),
                effect_ref="provider-effect-read-bound",
                provider_proof_ref="provider-proof-read-bound",
            )
            self.assertEqual("read_output", accepted_observation.result_kind)
            with self.assertRaises(ValueError):
                adapter._issue_backend_effect_observation(
                    request,
                    identity,
                    authority,
                    run=run,
                    assignment=assignment,
                    delivery=delivery,
                    public_result=ReadReceipt(accepted + "a"),
                    effect_ref="provider-effect-read-bound",
                    provider_proof_ref="provider-proof-read-bound",
                )

    def test_composite_stop_requires_ordered_completed_stages(self) -> None:
        stage_a = adapter.CompositeStopStage(
            stage_id="stage-a",
            resource_ref="resource-a",
            effect_ref="effect-a",
            status="COMPLETED",
            evidence_digest=_DIGEST_1,
        )
        stage_b = adapter.CompositeStopStage(
            stage_id="stage-b",
            resource_ref="resource-b",
            effect_ref="effect-b",
            status="COMPLETED",
            evidence_digest=_DIGEST_2,
        )
        composite = adapter.make_composite_stop_observation(
            (stage_a, stage_b), composite_ref="composite-stop-1"
        )
        self.assertEqual((stage_a, stage_b), composite.stages)
        self.assertEqual("composite-stop-1", composite.composite_ref)
        self.assertTrue(composite.composite_digest.startswith("sha256:"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            _set_frozen_stage_status(stage_a)
        failed_stage = dataclasses.replace(stage_a, status="FAILED")
        for invalid in ((), (failed_stage,)):
            with self.assertRaises((TypeError, ValueError)):
                adapter.make_composite_stop_observation(
                    invalid, composite_ref="composite-stop-1"
                )


if __name__ == "__main__":
    unittest.main()
