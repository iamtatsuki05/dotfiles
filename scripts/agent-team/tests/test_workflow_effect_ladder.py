"""Deterministic real-Store ladder for all durable effect action tags."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from unittest import mock

import test_workflow_store_transaction as transaction_fixtures
from test_workflow_effect_execution import _Authority, _start_spec, _StoreSpy

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
    StopResult,
    TaskRef,
    TerminalRef,
    WaitReceipt,
)
from agent_team.store import CoordinationStore

_QUESTION_BODY = "question-body-canary"
_WORKER_BODY = "worker-body-canary"
_READ_OUTPUT = "read-output-canary\n"


def _assignment() -> workflow.ActiveAssignment:
    completion = workflow.CompletionIdentity(
        run_id="run-1",
        task_id="task-1",
        dispatch_id="dispatch-1",
        sender_terminal_id="terminal-worker",
    )
    return workflow.ActiveAssignment(
        role=workflow.AssignmentRole.WORKER,
        worker_node="worker-node-1",
        task_id="task-1",
        attempt=1,
        dispatch_id="dispatch-1",
        terminal_id="terminal-worker",
        launch_mode=workflow.LaunchMode.BARE_BACKGROUND,
        completion_identity=completion,
    )


def _public_completion(
    assignment: workflow.ActiveAssignment,
) -> CompletionIdentity:
    return CompletionIdentity(
        run_id=RunRef(assignment.completion_identity.run_id),
        task_id=TaskRef(assignment.task_id),
        dispatch_id=DispatchRef(assignment.dispatch_id),
        sender_terminal_id=TerminalRef(assignment.terminal_id),
    )


def _public_assignment(assignment: workflow.ActiveAssignment) -> Assignment:
    return Assignment(
        role=Role.WORKER,
        launch_mode=LaunchMode.BARE_BACKGROUND,
        task_id=TaskRef(assignment.task_id),
        dispatch_id=DispatchRef(assignment.dispatch_id),
        terminal_id=TerminalRef(assignment.terminal_id),
        completion_identity=_public_completion(assignment),
    )


def _delivery(
    assignment: workflow.ActiveAssignment,
    *,
    delivery_id: str,
    question: bool,
) -> tuple[workflow.PendingDelivery, WaitReceipt]:
    body = _QUESTION_BODY if question else _WORKER_BODY
    projection = workflow.EventProjection(
        kind=(
            workflow.EventProjectionKind.QUESTION
            if question
            else workflow.EventProjectionKind.WORKER_DONE
        ),
        message_id="message-1" if question else None,
        completion_identity=assignment.completion_identity,
        outcome=None if question else workflow.EventOutcome.SUCCEEDED,
        body_digest=workflow.digest_bounded_body(body.encode("utf-8")),
    )
    message_ids = ("message-1",) if question else ()
    pending = workflow.PendingDelivery(
        delivery_id=delivery_id,
        consumer_generation=7,
        ordered_message_ids=message_ids,
        ordered_event_projection=(projection,),
        delivery_digest=workflow.delivery_content_digest(
            delivery_id=delivery_id,
            consumer_generation=7,
            ordered_message_ids=message_ids,
            ordered_event_projection=(projection,),
        ),
        ack_operation_id=None,
        ack_status=workflow.AckStatus.PENDING,
    )
    event = (
        NormalizedEvent.question(
            identity=_public_completion(assignment),
            message_id=MessageRef("message-1"),
            delivery_id=DeliveryRef(delivery_id),
            body=body,
        )
        if question
        else NormalizedEvent.worker_done(
            identity=_public_completion(assignment),
            outcome=Outcome.SUCCEEDED,
            delivery_id=DeliveryRef(delivery_id),
            body=body,
        )
    )
    return pending, WaitReceipt(DeliveryRef(delivery_id), (event,))


class _LadderBackend:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.execute_actions: list[workflow.OperationAction] = []
        self.wait_count = 0
        self.observations: dict[str, adapter.BackendEffectObservation] = {}

    def durability_capabilities(self) -> adapter.DurableEffectCapabilities:
        self.calls.append("backend.capabilities")
        return adapter.DurableEffectCapabilities(
            version=1,
            effect_key_idempotency=True,
            pure_effect_lookup=True,
            attempt_fence_enforcement=True,
            consumer_generation=True,
            exact_delivery_lookup=True,
            exact_read_lookup=True,
            composite_stop=True,
        )

    def execute(
        self, effect: adapter.BackendEffectRequest
    ) -> adapter.BackendEffectObservation:
        action = effect.command.action
        self.calls.append(f"backend.execute:{action.value}")
        self.execute_actions.append(action)
        run = workflow.RunIdentity("run-1", "terminal-main", 7)
        assignment: workflow.ActiveAssignment | None = None
        delivery: workflow.PendingDelivery | None = None
        composite: adapter.CompositeStopObservation | None = None
        if action is workflow.OperationAction.START:
            public: adapter.EffectPublicResult = StartResult(
                effect.request.root.team_id,
                RunRef(run.run_id),
                TerminalRef(run.main_terminal_id),
                _start_spec(effect.request.root).state_path,
            )
        elif action is workflow.OperationAction.PROMPT:
            assignment = _assignment()
            public = _public_assignment(assignment)
        elif action is workflow.OperationAction.WAIT:
            assignment = effect.request.assignment
            assert assignment is not None
            self.wait_count += 1
            delivery, public = _delivery(
                assignment,
                delivery_id=f"delivery-{self.wait_count}",
                question=self.wait_count == 1,
            )
        elif action is workflow.OperationAction.REPLY:
            assignment = effect.request.assignment
            delivery = effect.request.pending_delivery
            public = ReplyReceipt(True)
        elif action is workflow.OperationAction.READ:
            assignment = effect.request.assignment
            delivery = effect.request.pending_delivery
            public = ReadReceipt(_READ_OUTPUT)
        elif action is workflow.OperationAction.RELEASE:
            assignment = effect.request.assignment
            delivery = effect.request.pending_delivery
            public = ReleaseReceipt("released")
        elif action is workflow.OperationAction.ACK:
            assignment = effect.request.assignment
            delivery = effect.request.pending_delivery
            public = AckReceipt(True)
        else:
            public = StopResult(effect.request.root.team_id, RunRef(run.run_id))
            composite = adapter.make_composite_stop_observation(
                (
                    adapter.CompositeStopStage(
                        stage_id="stage-provider",
                        resource_ref="resource-provider",
                        effect_ref="effect-provider-stop",
                        status="COMPLETED",
                        evidence_digest="sha256:" + "1" * 64,
                    ),
                ),
                composite_ref="composite-stop-1",
            )
        observation = adapter._issue_backend_effect_observation(
            effect.request,
            effect.identity,
            effect.authority,
            run=run,
            assignment=assignment,
            delivery=delivery,
            public_result=public,
            effect_ref=f"provider-effect-{action.value}-{len(self.execute_actions)}",
            provider_proof_ref=f"provider-proof-{action.value}-{len(self.execute_actions)}",
            composite_stop=composite,
        )
        self.observations[observation.effect_ref] = observation
        return observation

    def lookup(
        self, snapshot: workflow.WorkflowEffectSnapshot
    ) -> adapter.BackendEffectObservation:
        self.calls.append("backend.lookup")
        try:
            return self.observations[snapshot.receipt.effect_ref]
        except KeyError as exc:
            raise workflow.RecoveryRequired(
                "backend effect lookup is unavailable"
            ) from exc


class _LadderProjector:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    @staticmethod
    def _last(
        request: adapter.EffectRequestIdentity,
        receipt: workflow.DurableReceipt,
    ) -> workflow.LastOperation:
        return workflow.LastOperation(
            operation_id=receipt.operation_id,
            effect_key=receipt.effect_key,
            action=receipt.action,
            request_digest=receipt.request_digest,
            expected_workflow_sequence=request.expected_workflow_sequence,
            expected_task_sequence=request.expected_task_sequence,
            status=workflow.OperationStatus.COMMITTED,
            receipt_id=receipt.receipt_id,
            receipt_digest=workflow.durable_receipt_digest(receipt),
        )

    def project(
        self,
        current: workflow.WorkflowCheckpointObservation | None,
        request: adapter.EffectRequestIdentity,
        observation: adapter.BackendEffectObservation,
        receipt: workflow.DurableReceipt,
    ) -> adapter.EffectProjection:
        action = request.command.action
        self.calls.append(f"projector.project:{action.value}")
        if action is workflow.OperationAction.START:
            run = workflow.RunIdentity(
                observation.run_id,
                observation.main_terminal_id,
                observation.consumer_generation,
            )
            draft = workflow.WorkflowCheckpointDraft(
                root=request.root,
                run=run,
                workflow_sequence=2,
                task_sequence=None,
                execution_mode=workflow.ExecutionMode.SERIAL,
                workflow_state=workflow.CheckpointState.IDLE,
                task_policy=None,
                active_assignment=None,
                pending_delivery=None,
                replied_message_ids=(),
                read_observed=False,
                released=False,
                review_authority=None,
                verification_authority=None,
                last_operation=self._last(request, receipt),
            )
            return adapter.EffectProjection(draft, observation.public_result)
        assert isinstance(current, workflow.WorkflowCheckpointV4)
        assignment = current.active_assignment
        pending = current.pending_delivery
        replies = current.replied_message_ids
        read_observed = current.read_observed
        released = current.released
        state = current.workflow_state
        task_sequence = current.task_sequence
        task_policy = current.task_policy
        if action is workflow.OperationAction.PROMPT:
            assignment = observation.assignment
            assert assignment is not None
            task_sequence = 1
            task_policy = workflow.TaskPolicyReference(
                version=4,
                team_id=current.root.team_id,
                workspace=current.root.workspace_path,
                task_id=assignment.task_id,
                sequence=1,
                state_digest=request.command.parameter_digest,
            )
            state = workflow.CheckpointState.ACTIVE
        elif action is workflow.OperationAction.WAIT:
            pending = observation.delivery
            assert pending is not None
            kind = pending.ordered_event_projection[0].kind
            state = (
                workflow.CheckpointState.QUESTION
                if kind is workflow.EventProjectionKind.QUESTION
                else workflow.CheckpointState.WORKER_DONE
            )
            replies = ()
            read_observed = False
            released = False
        elif action is workflow.OperationAction.REPLY:
            assert request.command.message_id is not None
            replies = (*replies, request.command.message_id)
        elif action is workflow.OperationAction.READ:
            read_observed = True
        elif action is workflow.OperationAction.RELEASE:
            read_observed = True
            released = True
            state = workflow.CheckpointState.AWAITING_ACK
        elif action is workflow.OperationAction.ACK:
            assert pending is not None
            kind = pending.ordered_event_projection[0].kind
            pending = None
            replies = ()
            read_observed = False
            released = False
            if kind is workflow.EventProjectionKind.QUESTION:
                state = workflow.CheckpointState.WAITING
            else:
                state = workflow.CheckpointState.IDLE
                assignment = None
        else:
            assignment = None
            pending = None
            replies = ()
            read_observed = False
            released = False
            state = workflow.CheckpointState.STOPPED
        draft = workflow.WorkflowCheckpointDraft(
            root=current.root,
            run=current.run,
            workflow_sequence=request.expected_workflow_sequence + 2,
            task_sequence=task_sequence,
            execution_mode=current.execution_mode,
            workflow_state=state,
            task_policy=task_policy,
            active_assignment=assignment,
            pending_delivery=pending,
            replied_message_ids=replies,
            read_observed=read_observed,
            released=released,
            review_authority=current.review_authority,
            verification_authority=current.verification_authority,
            last_operation=self._last(request, receipt),
        )
        return adapter.EffectProjection(draft, observation.public_result)


class WorkflowEffectLadderTests(unittest.TestCase):
    def test_all_action_tags_commit_through_real_store(self) -> None:
        with tempfile.TemporaryDirectory(prefix="effect-ladder-") as temporary:
            state_root = transaction_fixtures._make_state_root(temporary)
            root = transaction_fixtures._make_root(state_root, temporary)
            calls: list[str] = []
            backend = _LadderBackend(calls)
            projector = _LadderProjector(calls)
            with CoordinationStore(state_root, clock=lambda: 100) as store:
                runtime = adapter.WorkflowEffectAdapter(
                    _StoreSpy(store, calls),
                    backend,
                    _Authority(calls, expires_ns=10_000),
                    projector,
                    clock=lambda: 100,
                )
                spec = _start_spec(root)
                results = [
                    runtime.execute(
                        adapter.make_start_command(spec), root=root, payload=spec
                    ),
                    runtime.execute(
                        adapter.make_request_command(
                            RolePrompt(Role.WORKER, "prompt-body-canary")
                        ),
                        root=root,
                        payload="prompt-body-canary",
                    ),
                    runtime.execute(
                        adapter.make_request_command(RoleWait(Role.WORKER, 1)),
                        root=root,
                    ),
                    runtime.execute(
                        adapter.make_request_command(
                            MessageReply(MessageRef("message-1"), "reply-body-canary")
                        ),
                        root=root,
                        payload="reply-body-canary",
                    ),
                    runtime.execute(
                        adapter.make_request_command(
                            DeliveryAck(DeliveryRef("delivery-1"))
                        ),
                        root=root,
                    ),
                    runtime.execute(
                        adapter.make_request_command(RoleWait(Role.WORKER, 1)),
                        root=root,
                    ),
                    runtime.execute(
                        adapter.make_request_command(RoleRead(Role.WORKER, 8)),
                        root=root,
                    ),
                    runtime.execute(
                        adapter.make_request_command(RoleRelease(Role.WORKER)),
                        root=root,
                    ),
                    runtime.execute(
                        adapter.make_request_command(
                            DeliveryAck(DeliveryRef("delivery-2"))
                        ),
                        root=root,
                    ),
                    runtime.execute(adapter.make_stop_command(), root=root),
                ]
                self.assertTrue(
                    all(isinstance(result, adapter.AppliedEffect) for result in results)
                )
                self.assertEqual(
                    [
                        workflow.OperationAction.START,
                        workflow.OperationAction.PROMPT,
                        workflow.OperationAction.WAIT,
                        workflow.OperationAction.REPLY,
                        workflow.OperationAction.ACK,
                        workflow.OperationAction.WAIT,
                        workflow.OperationAction.READ,
                        workflow.OperationAction.RELEASE,
                        workflow.OperationAction.ACK,
                        workflow.OperationAction.STOP,
                    ],
                    backend.execute_actions,
                )
                self.assertEqual(10, len({result.operation_id for result in results}))
                final = store.load_checkpoint(workflow.WorkflowRootKey(root.root_key))
                self.assertIsInstance(final, workflow.WorkflowCheckpointV4)
                assert isinstance(final, workflow.WorkflowCheckpointV4)
                self.assertIs(workflow.CheckpointState.STOPPED, final.workflow_state)
                self.assertIsNone(final.active_assignment)
                self.assertIsNone(final.pending_delivery)
                self.assertEqual(0, calls.count("backend.lookup"))
                stopped = results[-1]
                assert isinstance(stopped, adapter.AppliedEffect)
                replayed_stop = runtime.replay(
                    workflow.WorkflowOperationId(stopped.operation_id)
                )
                self.assertIsInstance(replayed_stop, adapter.ReplayedEffect)
                self.assertIsInstance(replayed_stop.public_result, StopResult)
                self.assertEqual(1, calls.count("backend.lookup"))
                release = results[7]
                assert isinstance(release, adapter.AppliedEffect)
                no_pure_lookup = dataclasses.replace(
                    backend.durability_capabilities(),
                    effect_key_idempotency=True,
                    pure_effect_lookup=False,
                )
                with mock.patch.object(
                    backend,
                    "durability_capabilities",
                    return_value=no_pure_lookup,
                ):
                    release_replay = adapter.WorkflowEffectAdapter(
                        _StoreSpy(store, calls),
                        backend,
                        _Authority(calls, expires_ns=10_000),
                        projector,
                        clock=lambda: 100,
                    )
                lookup_count = calls.count("backend.lookup")
                with self.assertRaises(adapter.DurabilityUnsupported):
                    release_replay.replay(
                        workflow.WorkflowOperationId(release.operation_id)
                    )
                self.assertEqual(lookup_count, calls.count("backend.lookup"))


if __name__ == "__main__":
    unittest.main()
