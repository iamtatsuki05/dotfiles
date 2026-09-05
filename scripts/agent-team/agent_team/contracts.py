"""Typed runtime and backend contracts.

The values in this module intentionally do not describe an Orca command or JSON
response.  A backend binds its own identifiers to the opaque references before
returning a receipt to the workflow layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, TypeAlias


class Role(str, Enum):
    MAIN = "main"
    PLANNER = "planner"
    WORKER = "worker"
    REVIEWER = "reviewer"


class LaunchMode(str, Enum):
    SUPERVISED_DIRECT = "supervised_direct"
    BARE_BACKGROUND = "bare_background"


class EventKind(str, Enum):
    WORKER_DONE = "worker_done"
    QUESTION = "question"
    ESCALATION = "escalation"


class Outcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkflowState(str, Enum):
    IDLE = "idle"
    ACTIVE = "active"
    WAITING = "waiting"
    COMPLETED = "completed"
    QUESTION = "question"
    ESCALATED = "escalated"
    READ = "read"
    AWAITING_ACK = "awaiting_ack"
    STOPPED = "stopped"


class ErrorCode(str, Enum):
    INVALID_REQUEST = "InvalidRequest"
    TEAM_NOT_RUNNING = "TeamNotRunning"
    TEAM_ALREADY_RUNNING = "TeamAlreadyRunning"
    BUSY = "Busy"
    ORDER_VIOLATION = "OrderViolation"
    IDENTITY_MISMATCH = "IdentityMismatch"
    COMPLETION_NOT_OBSERVED = "CompletionNotObserved"
    MESSAGE_OR_DELIVERY_UNKNOWN = "MessageOrDeliveryUnknown"
    BACKEND_PROTOCOL_FAILURE = "BackendProtocolFailure"


class RuntimeFailure(Exception):
    """A stable, redacted failure returned by the runtime contract."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True, repr=False)
class _OpaqueRef:
    """An identity whose interpretation belongs to the backend adapter."""

    _value: str

    def __post_init__(self) -> None:
        if not isinstance(self._value, str) or not self._value:
            msg = "opaque reference token must be a non-empty string"
            raise ValueError(msg)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(opaque)"


@dataclass(frozen=True, slots=True, repr=False)
class RunRef(_OpaqueRef):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class TaskRef(_OpaqueRef):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class DispatchRef(_OpaqueRef):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class TerminalRef(_OpaqueRef):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class MessageRef(_OpaqueRef):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class DeliveryRef(_OpaqueRef):
    pass


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """The already validated launch snapshot for one role."""

    provider: str
    transport: str
    model: str
    effort: str
    permission: str
    instructions: str
    execution: str
    adapter_id: str | None = None
    acp_executables: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class StartSpec:
    team_id: str
    workspace: Path
    config_path: Path
    state_path: Path
    role_specs: Mapping[Role, RoleSpec]
    attach: bool = False


@dataclass(frozen=True, slots=True)
class CompletionIdentity:
    run_id: RunRef
    task_id: TaskRef
    dispatch_id: DispatchRef
    sender_terminal_id: TerminalRef


@dataclass(frozen=True, slots=True)
class Assignment:
    role: Role
    launch_mode: LaunchMode
    task_id: TaskRef
    dispatch_id: DispatchRef
    terminal_id: TerminalRef
    completion_identity: CompletionIdentity


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    """A backend-normalized event; message text is always untrusted data."""

    kind: EventKind
    delivery_id: DeliveryRef
    message_id: MessageRef | None = None
    identity: CompletionIdentity | None = None
    outcome: Outcome | None = None
    body: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EventKind):
            raise TypeError("event kind must be an EventKind")
        if not isinstance(self.delivery_id, DeliveryRef):
            raise TypeError("event delivery must be a DeliveryRef")
        if not isinstance(self.body, str):
            raise TypeError("event body must be a string")
        if self.kind is EventKind.WORKER_DONE:
            if not isinstance(self.identity, CompletionIdentity) or not isinstance(
                self.outcome, Outcome
            ):
                raise ValueError("worker_done requires identity and outcome")
            if self.message_id is not None:
                raise ValueError("worker_done cannot contain a message")
        elif self.kind is EventKind.QUESTION:
            if not isinstance(self.message_id, MessageRef) or not isinstance(
                self.identity, CompletionIdentity
            ):
                raise ValueError("question requires message and source identity")
            if self.outcome is not None:
                raise ValueError("question cannot contain an outcome")
        elif self.kind is EventKind.ESCALATION:
            if not isinstance(self.identity, CompletionIdentity):
                raise ValueError("escalation requires source identity")
            if self.message_id is not None or self.outcome is not None:
                raise ValueError("escalation cannot contain message or outcome")

    @classmethod
    def worker_done(
        cls,
        *,
        identity: CompletionIdentity,
        outcome: Outcome,
        body: str,
        delivery_id: DeliveryRef,
    ) -> NormalizedEvent:
        return cls(
            EventKind.WORKER_DONE,
            delivery_id,
            identity=identity,
            outcome=outcome,
            body=body,
        )

    @classmethod
    def question(
        cls,
        *,
        identity: CompletionIdentity,
        message_id: MessageRef,
        delivery_id: DeliveryRef,
        body: str,
    ) -> NormalizedEvent:
        return cls(
            EventKind.QUESTION,
            delivery_id,
            message_id=message_id,
            identity=identity,
            body=body,
        )

    @classmethod
    def escalation(
        cls,
        *,
        identity: CompletionIdentity,
        delivery_id: DeliveryRef,
        body: str,
    ) -> NormalizedEvent:
        return cls(EventKind.ESCALATION, delivery_id, identity=identity, body=body)


@dataclass(frozen=True, slots=True)
class StartResult:
    team_id: str
    run_id: RunRef
    main_terminal_id: TerminalRef
    state_path: Path


@dataclass(frozen=True, slots=True)
class StopResult:
    team_id: str
    run_id: RunRef


@dataclass(frozen=True, slots=True)
class WaitReceipt:
    delivery_id: DeliveryRef | None
    events: tuple[NormalizedEvent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.events, tuple) or any(
            not isinstance(event, NormalizedEvent) for event in self.events
        ):
            raise ValueError("wait events must be normalized event values")
        if self.events and self.delivery_id is None:
            raise ValueError("wait events require a Delivery")
        if not self.events and self.delivery_id is not None:
            raise ValueError("timeout wait cannot contain a Delivery")


@dataclass(frozen=True, slots=True)
class ReadReceipt:
    output: str


@dataclass(frozen=True, slots=True)
class ReleaseReceipt:
    state: str


@dataclass(frozen=True, slots=True)
class AckReceipt:
    acknowledged: bool


@dataclass(frozen=True, slots=True)
class ReplyReceipt:
    replied: bool


@dataclass(frozen=True, slots=True)
class StatusReceipt:
    status: str
    team_id: str
    run_id: RunRef


@dataclass(frozen=True, slots=True)
class RoleStatusReceipt:
    role: Role
    status: str


@dataclass(frozen=True, slots=True)
class AttachReceipt:
    role: Role
    terminal_id: TerminalRef
    run_id: RunRef


@dataclass(frozen=True, slots=True)
class Status:
    pass


@dataclass(frozen=True, slots=True)
class Attach:
    role: Role


@dataclass(frozen=True, slots=True)
class RoleGet:
    role: Role


@dataclass(frozen=True, slots=True)
class RolePrompt:
    role: Role
    text: str


@dataclass(frozen=True, slots=True)
class RoleWait:
    role: Role
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class RoleRead:
    role: Role
    lines: int


@dataclass(frozen=True, slots=True)
class RoleRelease:
    role: Role


@dataclass(frozen=True, slots=True)
class MessageReply:
    message_id: MessageRef
    body: str


@dataclass(frozen=True, slots=True)
class DeliveryAck:
    delivery_id: DeliveryRef


RuntimeRequest: TypeAlias = (
    Status
    | Attach
    | RoleGet
    | RolePrompt
    | RoleWait
    | RoleRead
    | RoleRelease
    | MessageReply
    | DeliveryAck
)
BackendRequest: TypeAlias = RuntimeRequest
BackendResult: TypeAlias = (
    Assignment
    | WaitReceipt
    | ReadReceipt
    | ReleaseReceipt
    | AckReceipt
    | ReplyReceipt
    | StatusReceipt
    | RoleStatusReceipt
    | AttachReceipt
)
RuntimeResult: TypeAlias = BackendResult


class TeamRuntime(Protocol):
    def start(self, spec: StartSpec) -> StartResult: ...

    def request(self, request: RuntimeRequest) -> RuntimeResult: ...

    def stop(self) -> StopResult: ...


class BackendPort(Protocol):
    def start(self, spec: StartSpec) -> StartResult: ...

    def request(self, request: BackendRequest) -> BackendResult: ...

    def stop(self) -> StopResult: ...
