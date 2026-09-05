"""Workflow policy for the three-entry runtime contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NoReturn

from .contracts import (
    AckReceipt,
    Assignment,
    Attach,
    AttachReceipt,
    BackendPort,
    DeliveryAck,
    DeliveryRef,
    ErrorCode,
    EventKind,
    MessageRef,
    MessageReply,
    NormalizedEvent,
    Outcome,
    ReadReceipt,
    ReleaseReceipt,
    ReplyReceipt,
    Role,
    RoleGet,
    RolePrompt,
    RoleRead,
    RoleRelease,
    RoleStatusReceipt,
    RoleWait,
    RuntimeFailure,
    RuntimeRequest,
    RuntimeResult,
    StartResult,
    StartSpec,
    Status,
    StatusReceipt,
    StopResult,
    TeamRuntime,
    WaitReceipt,
    WorkflowState,
)


@dataclass
class _WorkflowSession:
    state: WorkflowState = WorkflowState.IDLE
    start: StartResult | None = None
    assignment: Assignment | None = None
    completion: NormalizedEvent | None = None
    pending_delivery: DeliveryRef | None = None
    pending_events: tuple[NormalizedEvent, ...] = ()
    replied_messages: set[MessageRef] = field(default_factory=set)
    released: bool = False
    read_observed: bool = False


class WorkflowEngine(TeamRuntime):
    """Own lifecycle ordering while delegating external effects to a port.

    The engine deliberately stores only process-local observations.  Durable
    v3 state remains the backend's responsibility, so no new state fields are
    invented by this policy layer.
    """

    def __init__(self, backend: BackendPort) -> None:
        self._backend = backend
        self._session = _WorkflowSession()

    @property
    def state(self) -> WorkflowState:
        return self._session.state

    @property
    def completed_successfully(self) -> bool:
        completion = self._session.completion
        return completion is not None and completion.outcome is Outcome.SUCCEEDED

    def start(self, spec: StartSpec) -> StartResult:
        if self._session.start is not None:
            raise RuntimeFailure(
                ErrorCode.TEAM_ALREADY_RUNNING,
                "agent-team runtime has already been started",
            )
        if not spec.team_id or not spec.workspace or not spec.config_path:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "start specification is incomplete",
            )
        result = self._backend.start(spec)
        if not isinstance(result, StartResult):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "backend returned an invalid start receipt",
            )
        if result.team_id != spec.team_id or result.state_path != spec.state_path:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "start receipt does not match the requested team identity",
            )
        self._session.start = result
        self._session.state = WorkflowState.IDLE
        return result

    def request(self, request: RuntimeRequest) -> RuntimeResult:
        if self._session.start is None:
            raise RuntimeFailure(
                ErrorCode.TEAM_NOT_RUNNING,
                "agent-team runtime has not been started",
            )
        if self._session.state is WorkflowState.STOPPED:
            raise RuntimeFailure(
                ErrorCode.TEAM_NOT_RUNNING,
                "agent-team runtime has been stopped",
            )
        if isinstance(request, Status):
            result = self._backend.request(request)
            if not isinstance(result, StatusReceipt):
                self._protocol_failure("backend returned an invalid status receipt")
            start = self._require_start()
            if result.team_id != start.team_id or result.run_id != start.run_id:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "status receipt does not match the started team",
                )
            return result
        if isinstance(request, Attach):
            return self._attach(request)
        if isinstance(request, RolePrompt):
            return self._prompt(request)
        if isinstance(request, RoleWait):
            return self._wait(request)
        if isinstance(request, RoleRead):
            return self._read(request)
        if isinstance(request, RoleRelease):
            return self._release(request)
        if isinstance(request, DeliveryAck):
            return self._ack(request)
        if isinstance(request, MessageReply):
            return self._reply(request)
        if isinstance(request, RoleGet):
            return self._role_get(request)
        raise RuntimeFailure(ErrorCode.INVALID_REQUEST, "unsupported runtime request")

    def stop(self) -> StopResult:
        if self._session.start is None:
            raise RuntimeFailure(
                ErrorCode.TEAM_NOT_RUNNING,
                "agent-team runtime has not been started",
            )
        if self._session.state is WorkflowState.STOPPED:
            raise RuntimeFailure(
                ErrorCode.TEAM_NOT_RUNNING,
                "agent-team runtime has already been stopped",
            )
        result = self._backend.stop()
        if not isinstance(result, StopResult):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "backend returned an invalid stop receipt",
            )
        start = self._require_start()
        if result.team_id != start.team_id or result.run_id != start.run_id:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "stop receipt does not match the started team",
            )
        self._session.state = WorkflowState.STOPPED
        self._session.assignment = None
        self._session.pending_delivery = None
        self._session.pending_events = ()
        return result

    def _attach(self, request: Attach) -> AttachReceipt:
        result = self._backend.request(request)
        if not isinstance(result, AttachReceipt):
            self._protocol_failure("backend returned an invalid attach receipt")
        if (
            result.role is not request.role
            or result.run_id != self._require_start().run_id
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "attach receipt does not match the requested role and Run",
            )
        return result

    def _prompt(self, request: RolePrompt) -> Assignment:
        if not request.text.strip():
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "role prompt must be a non-empty string",
            )
        if (
            self._session.assignment is not None
            or self._session.pending_delivery is not None
        ):
            raise RuntimeFailure(
                ErrorCode.BUSY,
                "a role or Delivery is already active",
            )
        result = self._backend.request(request)
        if not isinstance(result, Assignment) or result.role is not request.role:
            self._protocol_failure("backend returned an invalid role assignment")
        identity = result.completion_identity
        if (
            identity.run_id != self._require_start().run_id
            or result.task_id != identity.task_id
            or result.dispatch_id != identity.dispatch_id
            or result.terminal_id != identity.sender_terminal_id
        ):
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "role assignment identity is inconsistent with the started team",
            )
        self._session.assignment = result
        self._session.completion = None
        self._session.pending_events = ()
        self._session.replied_messages.clear()
        self._session.released = False
        self._session.read_observed = False
        self._session.state = WorkflowState.ACTIVE
        return result

    def _wait(self, request: RoleWait) -> WaitReceipt:
        assignment = self._require_assignment(request.role)
        if self._session.pending_delivery is not None:
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION,
                "acknowledge the pending Delivery before waiting again",
            )
        if request.timeout_ms < 1:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "wait timeout must be positive",
            )
        result = self._backend.request(request)
        if not isinstance(result, WaitReceipt):
            self._protocol_failure("backend returned an invalid wait receipt")
        if result.delivery_id is None:
            if result.events:
                self._protocol_failure("events require a Delivery receipt")
            self._session.state = WorkflowState.WAITING
            return result
        for event in result.events:
            if event.delivery_id != result.delivery_id:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "event does not belong to the observed Delivery",
                )
        next_state, completion = self._classify_events(assignment, result.events)
        self._session.pending_delivery = result.delivery_id
        self._session.pending_events = result.events
        self._session.replied_messages.clear()
        self._session.completion = completion
        self._session.state = next_state
        return result

    def _classify_events(
        self, assignment: Assignment, events: tuple[NormalizedEvent, ...]
    ) -> tuple[WorkflowState, NormalizedEvent | None]:
        completions = tuple(
            event for event in events if event.kind is EventKind.WORKER_DONE
        )
        questions = tuple(event for event in events if event.kind is EventKind.QUESTION)
        escalations = tuple(
            event for event in events if event.kind is EventKind.ESCALATION
        )
        kinds_present = sum(
            bool(group) for group in (completions, questions, escalations)
        )
        if kinds_present > 1:
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION,
                "a Delivery cannot mix completion, question, and escalation events",
            )
        if len(completions) > 1 or len(escalations) > 1:
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION,
                "a Delivery contains duplicate terminal events",
            )
        if questions:
            if any(event.message_id is None for event in questions):
                self._protocol_failure("question event is missing its message")
            message_ids = tuple(event.message_id for event in questions)
            if len(set(message_ids)) != len(message_ids):
                raise RuntimeFailure(
                    ErrorCode.ORDER_VIOLATION,
                    "a Delivery contains duplicate question messages",
                )
            if any(
                event.identity != assignment.completion_identity for event in questions
            ):
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "question source identity does not match its assignment",
                )
            return WorkflowState.QUESTION, None
        if escalations:
            if escalations[0].identity != assignment.completion_identity:
                raise RuntimeFailure(
                    ErrorCode.IDENTITY_MISMATCH,
                    "escalation source identity does not match its assignment",
                )
            return WorkflowState.ESCALATED, None
        if not completions:
            return WorkflowState.WAITING, None

        event = completions[0]
        if event.identity != assignment.completion_identity:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "worker completion identity does not match its assignment",
            )
        if event.outcome not in (Outcome.SUCCEEDED, Outcome.FAILED):
            self._protocol_failure("worker completion has an invalid outcome")
        if self._session.completion is not None:
            raise RuntimeFailure(
                ErrorCode.IDENTITY_MISMATCH,
                "duplicate worker completion was observed",
            )
        return WorkflowState.COMPLETED, event

    def _read(self, request: RoleRead) -> ReadReceipt:
        self._require_completion(request.role)
        if self._session.read_observed:
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION,
                "worker output has already been read",
            )
        if request.lines < 1:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "read lines must be positive",
            )
        result = self._backend.request(request)
        if not isinstance(result, ReadReceipt):
            self._protocol_failure("backend returned an invalid read receipt")
        self._session.read_observed = True
        self._session.state = WorkflowState.READ
        return result

    def _release(self, request: RoleRelease) -> ReleaseReceipt:
        self._require_completion(request.role)
        if not self._session.read_observed:
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION,
                "worker output must be read before release",
            )
        if self._session.released:
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION,
                "worker assignment has already been released",
            )
        result = self._backend.request(request)
        if not isinstance(result, ReleaseReceipt):
            self._protocol_failure("backend returned an invalid release receipt")
        if result.state not in {"retained", "released", "already_released"}:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "backend returned an unknown release state",
            )
        self._session.released = True
        self._session.assignment = None
        self._session.state = WorkflowState.AWAITING_ACK
        return result

    def _ack(self, request: DeliveryAck) -> AckReceipt:
        pending = self._session.pending_delivery
        if pending is None or pending != request.delivery_id:
            raise RuntimeFailure(
                ErrorCode.MESSAGE_OR_DELIVERY_UNKNOWN,
                "Delivery does not match the pending observation",
            )
        events = self._session.pending_events
        if any(event.kind is EventKind.ESCALATION for event in events):
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION,
                "escalation must remain pending for user review",
            )
        if any(event.kind is EventKind.QUESTION for event in events):
            questions = {
                event.message_id
                for event in events
                if event.kind is EventKind.QUESTION and event.message_id is not None
            }
            if not questions.issubset(self._session.replied_messages):
                raise RuntimeFailure(
                    ErrorCode.ORDER_VIOLATION,
                    "reply to every question before acknowledging its Delivery",
                )
        if (
            any(event.kind is EventKind.WORKER_DONE for event in events)
            and not self._session.released
        ):
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION,
                "release the completed role before acknowledging its Delivery",
            )
        result = self._backend.request(request)
        if not isinstance(result, AckReceipt) or not result.acknowledged:
            self._protocol_failure("backend did not acknowledge the Delivery")
        has_worker_done = any(event.kind is EventKind.WORKER_DONE for event in events)
        self._session.pending_delivery = None
        self._session.pending_events = ()
        self._session.replied_messages.clear()
        if has_worker_done:
            self._session.assignment = None
            self._session.completion = None
            self._session.released = False
            self._session.read_observed = False
            self._session.state = WorkflowState.IDLE
        else:
            # A question Delivery is only consumed after its reply.  The
            # assignment remains active so the caller can wait for the next
            # worker event without starting a second Dispatch.
            self._session.state = WorkflowState.WAITING
        return result

    def _reply(self, request: MessageReply) -> RuntimeResult:
        events = self._session.pending_events
        messages = {
            event.message_id
            for event in events
            if event.kind is EventKind.QUESTION and event.message_id is not None
        }
        if self._session.pending_delivery is None or request.message_id not in messages:
            raise RuntimeFailure(
                ErrorCode.MESSAGE_OR_DELIVERY_UNKNOWN,
                "message does not match a pending question",
            )
        if not request.body.strip():
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "reply body must be a non-empty string",
            )
        if request.message_id in self._session.replied_messages:
            raise RuntimeFailure(
                ErrorCode.ORDER_VIOLATION,
                "question message has already been replied to",
            )
        result = self._backend.request(request)
        if not isinstance(result, ReplyReceipt) or not result.replied:
            self._protocol_failure("backend did not acknowledge the reply")
        self._session.replied_messages.add(request.message_id)
        return result

    def _role_get(self, request: RoleGet) -> RuntimeResult:
        self._require_assignment(request.role)
        result = self._backend.request(request)
        if not isinstance(result, RoleStatusReceipt) or result.role is not request.role:
            self._protocol_failure("backend returned an invalid role status receipt")
        return result

    def _require_start(self) -> StartResult:
        start = self._session.start
        if start is None:
            raise RuntimeFailure(
                ErrorCode.TEAM_NOT_RUNNING,
                "agent-team runtime has not been started",
            )
        return start

    def _require_assignment(self, role: Role) -> Assignment:
        assignment = self._session.assignment
        if assignment is None or assignment.role is not role:
            raise RuntimeFailure(
                ErrorCode.TEAM_NOT_RUNNING,
                "role has no active assignment",
            )
        return assignment

    def _require_completion(self, role: Role) -> None:
        self._require_assignment(role)
        if self._session.completion is None:
            raise RuntimeFailure(
                ErrorCode.COMPLETION_NOT_OBSERVED,
                "matching worker_done has not been observed",
            )

    @staticmethod
    def _protocol_failure(message: str) -> NoReturn:
        raise RuntimeFailure(ErrorCode.BACKEND_PROTOCOL_FAILURE, message)
