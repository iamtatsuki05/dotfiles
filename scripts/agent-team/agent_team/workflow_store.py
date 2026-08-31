"""Pure typed values and codecs for the durable workflow Store boundary.

This module deliberately owns no SQLite connection, filesystem descriptor,
provider result, policy transition, or external effect.  ``store.py`` is the
authority that issues handles/receipts and commits these values.  The values
here provide the strict, versioned wire boundary shared by that authority and
its callers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Final,
    NewType,
    NoReturn,
    Protocol,
    SupportsIndex,
    TypeAlias,
    TypeVar,
    cast,
)

STORE_SCHEMA: Final = 3
CHECKPOINT_VERSION: Final = 4
SEED_VERSION: Final = 1
WORKFLOW_EVENT_SCHEMA_VERSION: Final = 1

MAX_CHECKPOINT_BYTES: Final = 1_048_576
MAX_IDENTIFIER_BYTES: Final = 128
MAX_PATH_BYTES: Final = 4096
MAX_COLLECTION_ITEMS: Final = 256
MAX_SEQUENCE: Final = 2**63 - 1
MAX_DIGEST_LENGTH: Final = 71

_OPAQUE_IDENTIFIER_RE = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._:/-]{{0,{MAX_IDENTIFIER_BYTES - 1}}}\Z"
)
_SECRET_LIKE_PATTERNS: Final = (
    re.compile(
        r"(?:api[_-]?key|secret|token|password|passwd|authorization|bearer|"
        r"cookie|credential|private[_-]?key)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:^|[-_])(sk|pk|ghp|gho|github_pat|xox[baprs])[-_]?", re.IGNORECASE),
)

CHECKPOINT_DIGEST_DOMAIN: Final = b"agent-team/workflow-checkpoint/v4\0"
SEED_DIGEST_DOMAIN: Final = b"agent-team/workflow-seed/v1\0"
REQUEST_DIGEST_DOMAIN: Final = b"agent-team/workflow-request/v1\0"
DELIVERY_DIGEST_DOMAIN: Final = b"agent-team/workflow-delivery/v1\0"
WAIT_TIMEOUT_DIGEST_DOMAIN: Final = b"agent-team/workflow-wait-timeout/v1\0"
EVENT_BODY_DIGEST_DOMAIN: Final = b"agent-team/workflow-event-body/v1\0"
CONFIG_DIGEST_DOMAIN: Final = b"agent-team/workflow-config/v1\0"
INTENT_DIGEST_DOMAIN: Final = b"agent-team/workflow-intent/v1\0"
RECEIPT_DIGEST_DOMAIN: Final = b"agent-team/workflow-receipt/v1\0"
WORKFLOW_EVENT_DIGEST_DOMAIN: Final = b"agent-team/workflow-event/v1\0"
ASSIGNMENT_DIGEST_DOMAIN: Final = b"agent-team/workflow-assignment/v1\0"

CHECKPOINT_FIELDS: Final = (
    "checkpoint_version",
    "store_schema",
    "task_policy_version",
    "root",
    "run",
    "workflow_sequence",
    "task_sequence",
    "execution_mode",
    "workflow_state",
    "task_policy",
    "active_assignment",
    "pending_delivery",
    "replied_message_ids",
    "read_observed",
    "released",
    "review_authority",
    "verification_authority",
    "last_operation",
    "checkpoint_digest",
    "updated_ns",
)
SEED_FIELDS: Final = (
    "seed_version",
    "checkpoint_version",
    "store_schema",
    "root",
    "workflow_sequence",
    "operation_id",
    "operation_status",
    "workflow_state",
    "seed_digest",
    "updated_ns",
)
ROOT_FIELDS: Final = (
    "root_key",
    "team_id",
    "workspace_path",
    "workspace_device",
    "workspace_inode",
    "config_device",
    "config_inode",
    "config_digest",
    "state_root_device",
    "state_root_inode",
    "config_path",
    "state_root",
)
RUN_FIELDS: Final = ("run_id", "main_terminal_id", "consumer_generation")
POLICY_FIELDS: Final = (
    "version",
    "team_id",
    "workspace",
    "task_id",
    "sequence",
    "state_digest",
)
COMPLETION_FIELDS: Final = (
    "run_id",
    "task_id",
    "dispatch_id",
    "sender_terminal_id",
)
ASSIGNMENT_FIELDS: Final = (
    "role",
    "worker_node",
    "task_id",
    "attempt",
    "dispatch_id",
    "terminal_id",
    "launch_mode",
    "completion_identity",
)
PROJECTION_FIELDS: Final = (
    "kind",
    "message_id",
    "completion_identity",
    "outcome",
    "body_digest",
)
DELIVERY_FIELDS: Final = (
    "delivery_id",
    "consumer_generation",
    "ordered_message_ids",
    "ordered_event_projection",
    "delivery_digest",
    "ack_operation_id",
    "ack_status",
)
AUTHORITY_FIELDS: Final = ("reference", "digest")
LAST_OPERATION_FIELDS: Final = (
    "operation_id",
    "effect_key",
    "action",
    "request_digest",
    "expected_workflow_sequence",
    "expected_task_sequence",
    "status",
    "receipt_id",
    "receipt_digest",
)


WorkflowRootKey = NewType("WorkflowRootKey", str)
WorkflowOperationId = NewType("WorkflowOperationId", str)
EffectKey = NewType("EffectKey", str)


class WorkflowStoreError(Exception):
    """Base for stable errors at the workflow Store boundary."""


class CheckpointSchemaError(WorkflowStoreError, ValueError):
    """A checkpoint or nested value is not the exact v4 wire contract."""


class SeedSchemaError(CheckpointSchemaError):
    """A pre-start seed is not the exact v1 seed contract."""


class StateConflict(WorkflowStoreError):
    """A workflow or task CAS precondition is stale."""


class OperationIdentityConflict(WorkflowStoreError):
    """An operation/receipt/handle identity does not match its authority."""


class RecoveryRequired(WorkflowStoreError):
    """An uncertain or incomplete effect requires explicit recovery."""


class AssignmentRole(str, Enum):
    MAIN = "main"
    PLANNER = "planner"
    WORKER = "worker"
    REVIEWER = "reviewer"


class LaunchMode(str, Enum):
    SUPERVISED_DIRECT = "supervised_direct"
    BARE_BACKGROUND = "bare_background"


class EventProjectionKind(str, Enum):
    WORKER_DONE = "WORKER_DONE"
    QUESTION = "QUESTION"
    ESCALATION = "ESCALATION"


class EventOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class CheckpointState(str, Enum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    WAITING = "WAITING"
    QUESTION = "QUESTION"
    WORKER_DONE = "WORKER_DONE"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"
    AWAITING_ACK = "AWAITING_ACK"
    REVIEW_PENDING = "REVIEW_PENDING"
    VERIFYING = "VERIFYING"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    STOPPED = "STOPPED"


class SeedState(str, Enum):
    STARTING = "STARTING"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class ExecutionMode(str, Enum):
    SERIAL = "serial"


class OperationAction(str, Enum):
    START = "start"
    PROMPT = "prompt"
    WAIT = "wait"
    REPLY = "reply"
    READ = "read"
    RELEASE = "release"
    ACK = "ack"
    STOP = "stop"


class OperationStatus(str, Enum):
    INTENT = "INTENT"
    UNKNOWN_EFFECT = "UNKNOWN_EFFECT"
    COMMITTED = "COMMITTED"


class AckStatus(str, Enum):
    PENDING = "PENDING"
    ACK_INTENT = "ACK_INTENT"


class RecoveryCode(str, Enum):
    UNKNOWN_EFFECT = "UNKNOWN_EFFECT"
    RESPONSE_LOST = "RESPONSE_LOST"
    COMMIT_UNKNOWN = "COMMIT_UNKNOWN"
    RECEIPT_MISMATCH = "RECEIPT_MISMATCH"
    STORE_COMMIT_UNKNOWN = "STORE_COMMIT_UNKNOWN"


class TransitionKind(str, Enum):
    POLICY = "policy_transition"
    VERIFICATION = "verification_transition"


# Names used by the logical contract and the existing runtime vocabulary.
WorkflowState = CheckpointState
EventKind = EventProjectionKind
Outcome = EventOutcome


def _schema_error(message: str, *, seed: bool = False) -> CheckpointSchemaError:
    error_type = SeedSchemaError if seed else CheckpointSchemaError
    return error_type(message)


def _require_exact_type(value: object, expected: type[object], name: str) -> None:
    if type(value) is not expected:
        raise ValueError(f"{name} has an invalid type")


def _require_int(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SEQUENCE,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} is out of range")
    return value


def _require_text(
    value: object,
    name: str,
    *,
    maximum_bytes: int = MAX_IDENTIFIER_BYTES,
) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} is invalid")
    for character in value:
        codepoint = ord(character)
        if (
            codepoint < 0x20
            or 0x7F <= codepoint <= 0x9F
            or 0xD800 <= codepoint <= 0xDFFF
            or codepoint in (0x2028, 0x2029)
        ):
            raise ValueError(f"{name} contains an unsafe character")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} is not valid UTF-8") from exc
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{name} is too long")
    return value


def _require_optional_text(
    value: object,
    name: str,
    *,
    maximum_bytes: int = MAX_IDENTIFIER_BYTES,
) -> str | None:
    if value is None:
        return None
    return _require_text(value, name, maximum_bytes=maximum_bytes)


def _require_identifier(value: object, name: str) -> str:
    value = _require_text(value, name)
    if _OPAQUE_IDENTIFIER_RE.fullmatch(value) is None or any(
        pattern.search(value) for pattern in _SECRET_LIKE_PATTERNS
    ):
        raise ValueError(f"{name} is not an opaque identifier")
    return value


def _require_optional_identifier(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _require_identifier(value, name)


def _require_optional_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, name)


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be bool")
    return value


_EnumT = TypeVar("_EnumT", bound=Enum)


def _enum_value(value: object, enum_type: type[_EnumT], name: str) -> _EnumT:
    if type(value) is not str:
        raise ValueError(f"{name} is invalid")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValueError(f"{name} is invalid") from exc


def _require_digest(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != MAX_DIGEST_LENGTH
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _require_path(value: object, name: str) -> str:
    value = _require_text(value, name, maximum_bytes=MAX_PATH_BYTES)
    if value.startswith("//") or not value.startswith("/"):
        raise ValueError(f"{name} is not canonical")
    if value != "/" and value.endswith("/"):
        raise ValueError(f"{name} is not canonical")
    if posixpath.normpath(value) != value:
        raise ValueError(f"{name} is not canonical")
    return value


def _require_enum(value: object, enum_type: type[Enum], name: str) -> Enum:
    if type(value) is not enum_type:
        raise ValueError(f"{name} is invalid")
    return value


def _require_tuple(value: object, name: str) -> tuple[object, ...]:
    if type(value) is not tuple or len(value) > MAX_COLLECTION_ITEMS:
        raise ValueError(f"{name} is invalid")
    return value


def _require_id_tuple(value: object, name: str) -> tuple[str, ...]:
    values = _require_tuple(value, name)
    return tuple(_require_identifier(item, f"{name} item") for item in values)


def _require_assignment_fields(
    task_id: object,
    dispatch_id: object,
    attempt: object,
    terminal_id: object,
) -> None:
    values = (task_id, dispatch_id, attempt, terminal_id)
    if all(value is None for value in values):
        return
    if any(value is None for value in values):
        raise ValueError("assignment identity must be complete")
    _require_identifier(task_id, "task_id")
    _require_identifier(dispatch_id, "dispatch_id")
    _require_int(attempt, "attempt", minimum=1)
    _require_identifier(terminal_id, "terminal_id")


def _require_message_pair(delivery_id: object, message_id: object) -> None:
    if message_id is not None and delivery_id is None:
        raise ValueError("message_id requires delivery_id")


@dataclass(frozen=True, slots=True)
class PathIdentity:
    """A trusted filesystem observation supplied by the Store owner."""

    path: str
    device: int
    inode: int

    def __post_init__(self) -> None:
        _require_path(self.path, "path")
        _require_int(self.device, "device")
        _require_int(self.inode, "inode", minimum=1)


@dataclass(frozen=True, slots=True)
class RootIdentity:
    """The root/path identity persisted in a workflow checkpoint."""

    root_key: str
    team_id: str
    workspace: PathIdentity
    config_path: str
    config_device: int
    config_inode: int
    config_digest: str
    state_root: PathIdentity

    def __post_init__(self) -> None:
        _require_identifier(self.root_key, "root_key")
        _require_identifier(self.team_id, "team_id")
        if type(self.workspace) is not PathIdentity:
            raise TypeError("workspace identity is invalid")
        _require_path(self.config_path, "config_path")
        _require_int(self.config_device, "config_device")
        _require_int(self.config_inode, "config_inode", minimum=1)
        _require_digest(self.config_digest, "config_digest")
        if type(self.state_root) is not PathIdentity:
            raise TypeError("state root identity is invalid")

    @property
    def workspace_path(self) -> str:
        return self.workspace.path

    @property
    def workspace_device(self) -> int:
        return self.workspace.device

    @property
    def workspace_inode(self) -> int:
        return self.workspace.inode

    @property
    def state_root_path(self) -> str:
        return self.state_root.path

    @property
    def state_root_device(self) -> int:
        return self.state_root.device

    @property
    def state_root_inode(self) -> int:
        return self.state_root.inode


@dataclass(frozen=True, slots=True)
class RunIdentity:
    run_id: str
    main_terminal_id: str
    consumer_generation: int

    def __post_init__(self) -> None:
        _require_identifier(self.run_id, "run_id")
        _require_identifier(self.main_terminal_id, "main_terminal_id")
        _require_int(self.consumer_generation, "consumer_generation")


@dataclass(frozen=True, slots=True)
class TaskPolicyReference:
    version: int
    team_id: str
    workspace: str
    task_id: str
    sequence: int
    state_digest: str

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 4:
            raise ValueError("task policy version is invalid")
        _require_identifier(self.team_id, "task policy team_id")
        _require_path(self.workspace, "task policy workspace")
        _require_identifier(self.task_id, "task policy task_id")
        _require_int(self.sequence, "task policy sequence")
        _require_digest(self.state_digest, "task policy state_digest")


@dataclass(frozen=True, slots=True)
class CompletionIdentity:
    run_id: str
    task_id: str
    dispatch_id: str
    sender_terminal_id: str

    def __post_init__(self) -> None:
        _require_identifier(self.run_id, "completion run_id")
        _require_identifier(self.task_id, "completion task_id")
        _require_identifier(self.dispatch_id, "completion dispatch_id")
        _require_identifier(self.sender_terminal_id, "completion sender_terminal_id")


@dataclass(frozen=True, slots=True)
class ActiveAssignment:
    role: AssignmentRole
    worker_node: str
    task_id: str
    attempt: int
    dispatch_id: str
    terminal_id: str
    launch_mode: LaunchMode
    completion_identity: CompletionIdentity

    def __post_init__(self) -> None:
        _require_enum(self.role, AssignmentRole, "role")
        _require_identifier(self.worker_node, "worker_node")
        _require_identifier(self.task_id, "assignment task_id")
        _require_int(self.attempt, "attempt", minimum=1)
        _require_identifier(self.dispatch_id, "assignment dispatch_id")
        _require_identifier(self.terminal_id, "assignment terminal_id")
        _require_enum(self.launch_mode, LaunchMode, "launch_mode")
        if type(self.completion_identity) is not CompletionIdentity:
            raise TypeError("completion identity is invalid")
        if (
            self.task_id != self.completion_identity.task_id
            or self.dispatch_id != self.completion_identity.dispatch_id
            or self.terminal_id != self.completion_identity.sender_terminal_id
        ):
            raise ValueError("assignment and completion identities differ")


@dataclass(frozen=True, slots=True)
class EventProjection:
    kind: EventProjectionKind
    message_id: str | None
    completion_identity: CompletionIdentity
    outcome: EventOutcome | None
    body_digest: str

    def __post_init__(self) -> None:
        _require_enum(self.kind, EventProjectionKind, "event kind")
        _require_optional_identifier(self.message_id, "message_id")
        if type(self.completion_identity) is not CompletionIdentity:
            raise TypeError("event completion identity is invalid")
        if self.kind is EventProjectionKind.QUESTION:
            if self.message_id is None or self.outcome is not None:
                raise ValueError("question projection has invalid fields")
        elif self.kind is EventProjectionKind.WORKER_DONE:
            if self.message_id is not None or type(self.outcome) is not EventOutcome:
                raise ValueError("worker completion projection has invalid fields")
        elif self.message_id is not None or self.outcome is not None:
            raise ValueError("escalation projection has invalid fields")
        if self.outcome is not None:
            _require_enum(self.outcome, EventOutcome, "event outcome")
        _require_digest(self.body_digest, "event body_digest")


@dataclass(frozen=True, slots=True)
class PendingDelivery:
    delivery_id: str
    consumer_generation: int
    ordered_message_ids: tuple[str, ...]
    ordered_event_projection: tuple[EventProjection, ...]
    delivery_digest: str
    ack_operation_id: str | None
    ack_status: AckStatus

    def __post_init__(self) -> None:
        _require_identifier(self.delivery_id, "delivery_id")
        _require_int(self.consumer_generation, "delivery consumer_generation")
        message_ids = _require_id_tuple(self.ordered_message_ids, "ordered_message_ids")
        if len(set(message_ids)) != len(message_ids):
            raise ValueError("ordered_message_ids contains duplicates")
        projections = _require_tuple(
            self.ordered_event_projection, "ordered_event_projection"
        )
        if not projections or any(
            type(item) is not EventProjection for item in projections
        ):
            raise ValueError("event projection is invalid")
        typed_projections: tuple[EventProjection, ...] = tuple(
            cast(EventProjection, item) for item in projections
        )
        if len({item.kind for item in typed_projections}) != 1:
            raise ValueError("delivery event kinds must not be mixed")
        kind = typed_projections[0].kind
        if kind is EventProjectionKind.QUESTION and not message_ids:
            raise ValueError("question delivery requires messages")
        if kind is not EventProjectionKind.QUESTION and len(typed_projections) != 1:
            raise ValueError("terminal delivery has one projection")
        projected_ids = tuple(
            item.message_id for item in typed_projections if item.message_id is not None
        )
        if projected_ids != message_ids:
            raise ValueError("delivery message order does not match projection")
        _require_digest(self.delivery_digest, "delivery_digest")
        _require_optional_identifier(self.ack_operation_id, "ack_operation_id")
        _require_enum(self.ack_status, AckStatus, "ack_status")
        if self.ack_status is AckStatus.PENDING and self.ack_operation_id is not None:
            raise ValueError("pending delivery cannot have an ack operation")
        if self.ack_status is AckStatus.ACK_INTENT and self.ack_operation_id is None:
            raise ValueError("ack intent requires an operation")
        if self.delivery_digest != delivery_content_digest(
            delivery_id=self.delivery_id,
            consumer_generation=self.consumer_generation,
            ordered_message_ids=self.ordered_message_ids,
            ordered_event_projection=typed_projections,
        ):
            raise ValueError("delivery digest differs from its ordered projection")


@dataclass(frozen=True, slots=True)
class AuthorityReference:
    reference: str
    digest: str

    def __post_init__(self) -> None:
        _require_identifier(self.reference, "authority reference")
        _require_digest(self.digest, "authority digest")


@dataclass(frozen=True, slots=True)
class LastOperation:
    operation_id: str
    effect_key: str
    action: OperationAction
    request_digest: str
    expected_workflow_sequence: int
    expected_task_sequence: int | None
    status: OperationStatus
    receipt_id: str | None
    receipt_digest: str | None

    def __post_init__(self) -> None:
        _require_identifier(self.operation_id, "last operation_id")
        _require_identifier(self.effect_key, "last effect_key")
        _require_enum(self.action, OperationAction, "last action")
        _require_digest(self.request_digest, "last request_digest")
        _require_int(self.expected_workflow_sequence, "expected workflow sequence")
        if self.expected_task_sequence is not None:
            _require_int(self.expected_task_sequence, "expected task sequence")
        _require_enum(self.status, OperationStatus, "last status")
        _require_optional_identifier(self.receipt_id, "last receipt_id")
        if self.receipt_digest is not None:
            _require_digest(self.receipt_digest, "last receipt_digest")
        if self.status is OperationStatus.COMMITTED:
            if self.receipt_id is None or self.receipt_digest is None:
                raise ValueError("committed operation requires a receipt")
        elif self.receipt_id is not None or self.receipt_digest is not None:
            raise ValueError("uncommitted operation cannot have a receipt")


def _validate_checkpoint_parts(
    *,
    root: object,
    run: object,
    workflow_sequence: object,
    task_sequence: object,
    execution_mode: object,
    workflow_state: object,
    task_policy: object,
    active_assignment: object,
    pending_delivery: object,
    replied_message_ids: object,
    read_observed: object,
    released: object,
    review_authority: object,
    verification_authority: object,
    last_operation: object,
) -> None:
    if type(root) is not RootIdentity:
        raise TypeError("root identity is invalid")
    if type(run) is not RunIdentity:
        raise TypeError("run identity is invalid")
    _require_int(workflow_sequence, "workflow_sequence", minimum=2)
    if task_sequence is not None:
        _require_int(task_sequence, "task_sequence")
    _require_enum(execution_mode, ExecutionMode, "execution_mode")
    _require_enum(workflow_state, CheckpointState, "workflow_state")
    if task_policy is not None and type(task_policy) is not TaskPolicyReference:
        raise TypeError("task policy reference is invalid")
    if task_policy is None and task_sequence is not None:
        raise ValueError("task sequence requires a task policy reference")
    if task_policy is not None and task_sequence is None:
        raise ValueError("task policy reference requires a task sequence")
    if task_policy is not None and (
        task_policy.team_id != root.team_id
        or task_policy.workspace != root.workspace_path
        or task_policy.sequence != task_sequence
    ):
        raise ValueError("task policy root or sequence identity differs")
    if (
        type(active_assignment) is not ActiveAssignment
        and active_assignment is not None
    ):
        raise TypeError("active assignment is invalid")
    if type(pending_delivery) is not PendingDelivery and pending_delivery is not None:
        raise TypeError("pending delivery is invalid")
    if pending_delivery is not None and active_assignment is None:
        raise ValueError("pending delivery requires an assignment")
    if active_assignment is not None:
        if active_assignment.completion_identity.run_id != run.run_id:
            raise ValueError("assignment run identity differs")
        if task_policy is None:
            raise ValueError("assignment requires a task policy reference")
        if task_policy.task_id != active_assignment.task_id:
            raise ValueError("task policy task identity differs")
    if pending_delivery is not None:
        if pending_delivery.consumer_generation != run.consumer_generation:
            raise ValueError("delivery generation differs")
        for projection in pending_delivery.ordered_event_projection:
            if active_assignment is None:
                raise ValueError("delivery has no assignment")
            if projection.completion_identity != active_assignment.completion_identity:
                raise ValueError("delivery completion identity differs")
    replies = _require_id_tuple(replied_message_ids, "replied_message_ids")
    if len(set(replies)) != len(replies):
        raise ValueError("replied_message_ids contains duplicates")
    if pending_delivery is None and replies:
        raise ValueError("replies require a pending delivery")
    if pending_delivery is not None:
        question_ids = pending_delivery.ordered_message_ids
        for message_id in replies:
            if message_id not in question_ids:
                raise ValueError("replied message is not in the pending delivery")
    if type(read_observed) is not bool or type(released) is not bool:
        raise ValueError("read/release markers must be bool")
    if released and not read_observed:
        raise ValueError("released checkpoint must have observed read")
    if (
        review_authority is not None
        and type(review_authority) is not AuthorityReference
    ):
        raise TypeError("review authority is invalid")
    if (
        verification_authority is not None
        and type(verification_authority) is not AuthorityReference
    ):
        raise TypeError("verification authority is invalid")
    if last_operation is not None and type(last_operation) is not LastOperation:
        raise TypeError("last operation is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowCheckpointDraft:
    """Strict reducer input; Store metadata and digest are not caller fields."""

    root: RootIdentity
    run: RunIdentity
    workflow_sequence: int
    task_sequence: int | None
    execution_mode: ExecutionMode
    workflow_state: CheckpointState
    task_policy: TaskPolicyReference | None
    active_assignment: ActiveAssignment | None
    pending_delivery: PendingDelivery | None
    replied_message_ids: tuple[str, ...]
    read_observed: bool
    released: bool
    review_authority: AuthorityReference | None
    verification_authority: AuthorityReference | None
    last_operation: LastOperation | None

    def __post_init__(self) -> None:
        _validate_checkpoint_parts(
            root=self.root,
            run=self.run,
            workflow_sequence=self.workflow_sequence,
            task_sequence=self.task_sequence,
            execution_mode=self.execution_mode,
            workflow_state=self.workflow_state,
            task_policy=self.task_policy,
            active_assignment=self.active_assignment,
            pending_delivery=self.pending_delivery,
            replied_message_ids=self.replied_message_ids,
            read_observed=self.read_observed,
            released=self.released,
            review_authority=self.review_authority,
            verification_authority=self.verification_authority,
            last_operation=self.last_operation,
        )

    @property
    def task_policy_version(self) -> int | None:
        return None if self.task_policy is None else self.task_policy.version


@dataclass(frozen=True, slots=True, init=False, repr=False)
class WorkflowCheckpointV4:
    """Store-issued immutable checkpoint observation.

    The constructor is intentionally disabled.  ``store.py`` uses the small
    private ``_issue_checkpoint`` primitive after its own per-Store registry
    and transaction checks have succeeded.
    """

    checkpoint_version: int
    store_schema: int
    task_policy_version: int | None
    root: RootIdentity
    run: RunIdentity
    workflow_sequence: int
    task_sequence: int | None
    execution_mode: ExecutionMode
    workflow_state: CheckpointState
    task_policy: TaskPolicyReference | None
    active_assignment: ActiveAssignment | None
    pending_delivery: PendingDelivery | None
    replied_message_ids: tuple[str, ...]
    read_observed: bool
    released: bool
    review_authority: AuthorityReference | None
    verification_authority: AuthorityReference | None
    last_operation: LastOperation | None
    checkpoint_digest: str
    updated_ns: int
    _provenance: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("WorkflowCheckpointV4 is Store-issued")

    def __post_init__(self) -> None:
        if (
            type(self.checkpoint_version) is not int
            or self.checkpoint_version != CHECKPOINT_VERSION
        ):
            raise ValueError("checkpoint version is invalid")
        if type(self.store_schema) is not int or self.store_schema != STORE_SCHEMA:
            raise ValueError("store schema is invalid")
        _validate_checkpoint_parts(
            root=self.root,
            run=self.run,
            workflow_sequence=self.workflow_sequence,
            task_sequence=self.task_sequence,
            execution_mode=self.execution_mode,
            workflow_state=self.workflow_state,
            task_policy=self.task_policy,
            active_assignment=self.active_assignment,
            pending_delivery=self.pending_delivery,
            replied_message_ids=self.replied_message_ids,
            read_observed=self.read_observed,
            released=self.released,
            review_authority=self.review_authority,
            verification_authority=self.verification_authority,
            last_operation=self.last_operation,
        )
        expected_policy_version = (
            None if self.task_policy is None else self.task_policy.version
        )
        if self.task_policy_version != expected_policy_version:
            raise ValueError("task policy version projection differs")
        _require_digest(self.checkpoint_digest, "checkpoint_digest")
        _require_int(self.updated_ns, "updated_ns")

    def __repr__(self) -> str:
        return "<WorkflowCheckpointV4 immutable observation>"


def _validate_checkpoint_observation(
    value: object, *, issuer: object | None = None
) -> None:
    if type(value) is not WorkflowCheckpointV4:
        raise CheckpointSchemaError("checkpoint observation type is invalid")
    try:
        provenance = object.__getattribute__(value, "_provenance")
        value.__post_init__()
    except (AttributeError, TypeError, ValueError) as exc:
        raise CheckpointSchemaError("checkpoint observation is invalid") from exc
    if provenance is None or (issuer is not None and provenance is not issuer):
        raise CheckpointSchemaError("checkpoint observation issuer is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowRootSeed:
    """The strict pre-start row used before a backend supplies Run identity."""

    root: RootIdentity
    workflow_sequence: int = 0
    operation_id: str | None = None
    operation_status: OperationStatus | None = None
    updated_ns: int = 0
    seed_version: int = field(init=False, default=SEED_VERSION)
    checkpoint_version: int = field(init=False, default=CHECKPOINT_VERSION)
    store_schema: int = field(init=False, default=STORE_SCHEMA)

    def __post_init__(self) -> None:
        if type(self.root) is not RootIdentity:
            raise TypeError("seed root identity is invalid")
        if type(self.seed_version) is not int or self.seed_version != SEED_VERSION:
            raise ValueError("seed version is invalid")
        if (
            type(self.checkpoint_version) is not int
            or self.checkpoint_version != CHECKPOINT_VERSION
        ):
            raise ValueError("seed checkpoint version is invalid")
        if type(self.store_schema) is not int or self.store_schema != STORE_SCHEMA:
            raise ValueError("seed store schema is invalid")
        _require_int(self.workflow_sequence, "seed workflow_sequence")
        _require_optional_identifier(self.operation_id, "seed operation_id")
        _require_int(self.updated_ns, "seed updated_ns")
        if self.operation_status is not None:
            _require_enum(
                self.operation_status, OperationStatus, "seed operation_status"
            )
        if self.workflow_sequence == 0:
            if self.operation_id is not None or self.operation_status is not None:
                raise ValueError("empty seed cannot have an operation")
        elif self.workflow_sequence == 1:
            if (
                self.operation_id is None
                or self.operation_status is not OperationStatus.INTENT
            ):
                raise ValueError("start intent seed is invalid")
        elif self.workflow_sequence == 2:
            if (
                self.operation_id is None
                or self.operation_status is not OperationStatus.UNKNOWN_EFFECT
            ):
                raise ValueError("unknown seed is invalid")
        else:
            raise ValueError("seed sequence is invalid")

    @property
    def workflow_state(self) -> SeedState:
        if self.workflow_sequence == 2:
            return SeedState.RECOVERY_REQUIRED
        return SeedState.STARTING

    @property
    def seed_digest(self) -> str:
        return _domain_digest(
            SEED_DIGEST_DOMAIN, _canonical_json(_seed_body_mapping(self))
        )


@dataclass(frozen=True, slots=True)
class OperationIntent:
    """Stable caller-selected operation intent; no raw request body is stored."""

    operation_id: str
    effect_key: str
    root_key: str
    root: RootIdentity | None
    action: OperationAction
    request_digest: str
    expected_workflow_sequence: int
    expected_task_sequence: int | None
    run_id: str | None
    main_terminal_id: str | None
    task_id: str | None
    dispatch_id: str | None
    attempt: int | None
    terminal_id: str | None
    delivery_id: str | None
    message_id: str | None
    consumer_generation: int
    owner: str
    lease_epoch: int
    fencing_token: int
    actor: str
    evidence_ref: str | None
    next_task_sequence: int | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.operation_id, "operation_id")
        _require_identifier(self.effect_key, "effect_key")
        _require_identifier(self.root_key, "root_key")
        if self.root is not None:
            if type(self.root) is not RootIdentity:
                raise TypeError("operation root identity is invalid")
            if self.root.root_key != self.root_key:
                raise ValueError("operation root identity differs")
        _require_enum(self.action, OperationAction, "action")
        _require_digest(self.request_digest, "request_digest")
        _require_int(self.expected_workflow_sequence, "expected workflow sequence")
        if self.expected_task_sequence is not None:
            _require_int(self.expected_task_sequence, "expected task sequence")
        _require_optional_identifier(self.run_id, "run_id")
        _require_optional_identifier(self.main_terminal_id, "main_terminal_id")
        if (self.run_id is None) != (self.main_terminal_id is None):
            raise ValueError("run and main terminal identity must be paired")
        _require_assignment_fields(
            self.task_id, self.dispatch_id, self.attempt, self.terminal_id
        )
        _require_optional_identifier(self.delivery_id, "delivery_id")
        _require_optional_identifier(self.message_id, "message_id")
        _require_message_pair(self.delivery_id, self.message_id)
        _require_int(self.consumer_generation, "consumer_generation")
        _require_identifier(self.owner, "owner")
        _require_int(self.lease_epoch, "lease_epoch")
        _require_int(self.fencing_token, "fencing_token")
        _require_identifier(self.actor, "actor")
        if self.evidence_ref is not None:
            _require_digest(self.evidence_ref, "evidence_ref")
        if self.action is OperationAction.PROMPT:
            if self.expected_task_sequence is None:
                if self.next_task_sequence != 1:
                    raise ValueError("initial prompt requires task sequence one")
                _require_int(self.next_task_sequence, "next task sequence")
            elif self.next_task_sequence is not None:
                raise ValueError("existing-task prompt cannot advance task sequence")
        elif self.next_task_sequence is not None:
            raise ValueError("only prompt may advance the task sequence")
        if self.action is OperationAction.START:
            if (
                self.expected_workflow_sequence != 0
                or self.expected_task_sequence is not None
            ):
                raise ValueError("start intent sequence is invalid")
            if self.root is None:
                raise ValueError("start intent requires a root identity")
            if self.run_id is not None or any(
                value is not None
                for value in (
                    self.main_terminal_id,
                    self.task_id,
                    self.dispatch_id,
                    self.attempt,
                    self.terminal_id,
                    self.delivery_id,
                    self.message_id,
                )
            ):
                raise ValueError(
                    "start intent cannot contain run or assignment identity"
                )
        elif self.run_id is None or self.main_terminal_id is None:
            raise ValueError("non-start intent requires a run and main terminal")
        elif self.root is not None:
            raise ValueError("non-start intent must use the persisted root identity")


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class OperationHandle:
    """Store-issued capability for one intent transaction."""

    root_key: str
    operation_id: str
    intent_sequence: int
    owner: str
    lease_epoch: int
    fencing_token: int
    _issuer: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("OperationHandle is Store-issued")

    def __repr__(self) -> str:
        return "<OperationHandle opaque>"

    def __copy__(self) -> OperationHandle:
        raise TypeError("OperationHandle cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> OperationHandle:
        del memo
        raise TypeError("OperationHandle cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("OperationHandle cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("OperationHandle cannot be pickled")


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class DurableReceipt:
    """Trusted adapter-issued receipt; its constructor is intentionally closed."""

    receipt_id: str
    operation_id: str
    effect_key: str
    receipt_schema_version: int
    action: OperationAction
    request_digest: str
    root_key: str
    run_id: str
    main_terminal_id: str
    task_id: str | None
    dispatch_id: str | None
    attempt: int | None
    terminal_id: str | None
    delivery_id: str | None
    message_id: str | None
    consumer_generation: int
    owner: str
    lease_epoch: int
    fencing_token: int
    effect_ref: str
    result_kind: str
    result_digest: str
    evidence_ref: str
    issued_ns: int
    _issuer: object = field(repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("DurableReceipt is adapter-issued")

    def __repr__(self) -> str:
        return "<DurableReceipt opaque>"

    def __copy__(self) -> DurableReceipt:
        raise TypeError("DurableReceipt cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> DurableReceipt:
        del memo
        raise TypeError("DurableReceipt cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("DurableReceipt cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("DurableReceipt cannot be pickled")


def _issue_operation_handle(
    *,
    issuer: object,
    root_key: str,
    operation_id: str,
    intent_sequence: int,
    owner: str,
    lease_epoch: int,
    fencing_token: int,
) -> OperationHandle:
    handle = object.__new__(OperationHandle)
    for name, value in (
        ("root_key", root_key),
        ("operation_id", operation_id),
        ("intent_sequence", intent_sequence),
        ("owner", owner),
        ("lease_epoch", lease_epoch),
        ("fencing_token", fencing_token),
        ("_issuer", issuer),
    ):
        object.__setattr__(handle, name, value)
    _validate_operation_handle(handle, issuer=issuer)
    return handle


def _validate_operation_handle(value: object, *, issuer: object | None = None) -> None:
    if type(value) is not OperationHandle:
        raise OperationIdentityConflict("operation handle type is invalid")
    try:
        fields = (
            object.__getattribute__(value, "root_key"),
            object.__getattribute__(value, "operation_id"),
            object.__getattribute__(value, "intent_sequence"),
            object.__getattribute__(value, "owner"),
            object.__getattribute__(value, "lease_epoch"),
            object.__getattribute__(value, "fencing_token"),
            object.__getattribute__(value, "_issuer"),
        )
    except AttributeError as exc:
        raise OperationIdentityConflict("operation handle is unissued") from exc
    try:
        _require_identifier(fields[0], "handle root_key")
        _require_identifier(fields[1], "handle operation_id")
        _require_int(fields[2], "handle intent_sequence", minimum=1)
        _require_identifier(fields[3], "handle owner")
        _require_int(fields[4], "handle lease_epoch")
        _require_int(fields[5], "handle fencing_token")
    except (TypeError, ValueError) as exc:
        raise OperationIdentityConflict("operation handle values are invalid") from exc
    if fields[6] is None or (issuer is not None and fields[6] is not issuer):
        raise OperationIdentityConflict("operation handle issuer is invalid")


def _validate_receipt_fields(value: DurableReceipt) -> None:
    fields = (
        (value.receipt_id, "receipt_id"),
        (value.operation_id, "operation_id"),
        (value.effect_key, "effect_key"),
        (value.root_key, "root_key"),
        (value.run_id, "run_id"),
        (value.main_terminal_id, "main_terminal_id"),
        (value.effect_ref, "effect_ref"),
        (value.result_kind, "result_kind"),
        (value.owner, "owner"),
    )
    for field_value, name in fields:
        _require_identifier(field_value, name)
    if (
        type(value.receipt_schema_version) is not int
        or value.receipt_schema_version != 1
    ):
        raise ValueError("receipt schema version is invalid")
    _require_enum(value.action, OperationAction, "receipt action")
    _require_digest(value.request_digest, "receipt request_digest")
    _require_assignment_fields(
        value.task_id, value.dispatch_id, value.attempt, value.terminal_id
    )
    _require_optional_identifier(value.delivery_id, "receipt delivery_id")
    _require_optional_identifier(value.message_id, "receipt message_id")
    _require_message_pair(value.delivery_id, value.message_id)
    _require_int(value.consumer_generation, "receipt consumer_generation")
    _require_int(value.lease_epoch, "receipt lease_epoch")
    _require_int(value.fencing_token, "receipt fencing_token")
    _require_digest(value.result_digest, "receipt result_digest")
    _require_digest(value.evidence_ref, "receipt evidence_ref")
    _require_int(value.issued_ns, "receipt issued_ns")


def _issue_durable_receipt(
    *,
    issuer: object,
    receipt_id: str,
    operation_id: str,
    effect_key: str,
    action: OperationAction,
    request_digest: str,
    root_key: str,
    run_id: str,
    main_terminal_id: str,
    task_id: str | None,
    dispatch_id: str | None,
    attempt: int | None,
    terminal_id: str | None,
    delivery_id: str | None,
    message_id: str | None,
    consumer_generation: int,
    owner: str,
    lease_epoch: int,
    fencing_token: int,
    effect_ref: str,
    result_kind: str,
    result_digest: str,
    evidence_ref: str,
    issued_ns: int,
) -> DurableReceipt:
    receipt = object.__new__(DurableReceipt)
    for name, value in (
        ("receipt_id", receipt_id),
        ("operation_id", operation_id),
        ("effect_key", effect_key),
        ("receipt_schema_version", 1),
        ("action", action),
        ("request_digest", request_digest),
        ("root_key", root_key),
        ("run_id", run_id),
        ("main_terminal_id", main_terminal_id),
        ("task_id", task_id),
        ("dispatch_id", dispatch_id),
        ("attempt", attempt),
        ("terminal_id", terminal_id),
        ("delivery_id", delivery_id),
        ("message_id", message_id),
        ("consumer_generation", consumer_generation),
        ("owner", owner),
        ("lease_epoch", lease_epoch),
        ("fencing_token", fencing_token),
        ("effect_ref", effect_ref),
        ("result_kind", result_kind),
        ("result_digest", result_digest),
        ("evidence_ref", evidence_ref),
        ("issued_ns", issued_ns),
        ("_issuer", issuer),
    ):
        object.__setattr__(receipt, name, value)
    _validate_durable_receipt(receipt, issuer=issuer)
    return receipt


def _validate_durable_receipt(value: object, *, issuer: object | None = None) -> None:
    if type(value) is not DurableReceipt:
        raise OperationIdentityConflict("durable receipt type is invalid")
    try:
        receipt_issuer = object.__getattribute__(value, "_issuer")
        _validate_receipt_fields(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise OperationIdentityConflict(
            "durable receipt is unissued or invalid"
        ) from exc
    if receipt_issuer is None or (issuer is not None and receipt_issuer is not issuer):
        raise OperationIdentityConflict("durable receipt issuer is invalid")


@dataclass(frozen=True, slots=True)
class OperationBegin:
    operation: OperationHandle

    def __post_init__(self) -> None:
        _validate_operation_handle(self.operation)


@dataclass(frozen=True, slots=True)
class StoredReplay:
    operation_id: str
    receipt: DurableReceipt
    checkpoint: WorkflowCheckpointV4

    def __post_init__(self) -> None:
        _require_identifier(self.operation_id, "stored replay operation_id")
        if type(self.receipt) is not DurableReceipt:
            raise TypeError("stored replay receipt is invalid")
        if type(self.checkpoint) is not WorkflowCheckpointV4:
            raise TypeError("stored replay checkpoint is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowCommit:
    checkpoint: WorkflowCheckpointV4
    receipt: DurableReceipt

    def __post_init__(self) -> None:
        if type(self.checkpoint) is not WorkflowCheckpointV4:
            raise TypeError("workflow commit checkpoint is invalid")
        if type(self.receipt) is not DurableReceipt:
            raise TypeError("workflow commit receipt is invalid")


WorkflowCheckpointObservation: TypeAlias = WorkflowCheckpointV4 | WorkflowRootSeed


@dataclass(frozen=True, slots=True)
class UnknownCommit:
    operation_id: str
    status: OperationStatus
    checkpoint: WorkflowCheckpointObservation
    reason: RecoveryCode
    event_digest: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.operation_id, "unknown operation_id")
        if self.status is not OperationStatus.UNKNOWN_EFFECT:
            raise ValueError("unknown commit status is invalid")
        if type(self.checkpoint) not in (WorkflowCheckpointV4, WorkflowRootSeed):
            raise TypeError("unknown checkpoint observation is invalid")
        _require_enum(self.reason, RecoveryCode, "recovery code")
        if self.event_digest is not None:
            _require_digest(self.event_digest, "unknown event_digest")


@dataclass(frozen=True, slots=True)
class OperationLookup:
    operation_id: str
    effect_key: str
    action: OperationAction
    request_digest: str
    status: OperationStatus
    receipt_id: str | None
    receipt_digest: str | None
    checkpoint_digest: str
    event_digest: str

    def __post_init__(self) -> None:
        _require_identifier(self.operation_id, "lookup operation_id")
        _require_identifier(self.effect_key, "lookup effect_key")
        _require_enum(self.action, OperationAction, "lookup action")
        _require_digest(self.request_digest, "lookup request_digest")
        _require_enum(self.status, OperationStatus, "lookup status")
        _require_optional_identifier(self.receipt_id, "lookup receipt_id")
        if self.receipt_digest is not None:
            _require_digest(self.receipt_digest, "lookup receipt_digest")
        _require_digest(self.checkpoint_digest, "lookup checkpoint_digest")
        _require_digest(self.event_digest, "lookup event_digest")
        if self.status is OperationStatus.COMMITTED:
            if self.receipt_id is None or self.receipt_digest is None:
                raise ValueError("committed lookup requires receipt")
        elif self.receipt_id is not None or self.receipt_digest is not None:
            raise ValueError("uncommitted lookup cannot have receipt")


@dataclass(frozen=True, slots=True)
class PolicyOrVerificationTransition:
    kind: TransitionKind
    root_key: str
    authority: AuthorityReference
    expected_workflow_sequence: int
    expected_task_sequence: int | None
    next_task_sequence: int | None
    actor: str
    request_digest: str

    def __post_init__(self) -> None:
        _require_enum(self.kind, TransitionKind, "transition kind")
        _require_identifier(self.root_key, "transition root_key")
        if type(self.authority) is not AuthorityReference:
            raise TypeError("transition authority is invalid")
        _require_int(self.expected_workflow_sequence, "transition workflow sequence")
        if self.next_task_sequence is not None:
            _require_int(self.next_task_sequence, "transition next task sequence")
        if self.expected_task_sequence is None:
            if self.next_task_sequence not in (None, 1):
                raise ValueError("initial task sequence must be one")
        else:
            _require_int(self.expected_task_sequence, "transition task sequence")
            if self.next_task_sequence not in (
                self.expected_task_sequence,
                self.expected_task_sequence + 1,
            ):
                raise ValueError("transition task sequence must advance by one")
        _require_identifier(self.actor, "transition actor")
        _require_digest(self.request_digest, "transition request_digest")


class WorkflowStorePort(Protocol):
    def load_checkpoint(
        self, key: WorkflowRootKey
    ) -> WorkflowCheckpointObservation | None: ...

    def begin_operation(
        self,
        intent: OperationIntent,
        *,
        expected_workflow_sequence: int,
        expected_task_sequence: int | None,
    ) -> OperationBegin | StoredReplay: ...

    def commit_effect(
        self,
        operation: OperationHandle,
        receipt: DurableReceipt,
        next_checkpoint: WorkflowCheckpointDraft,
    ) -> WorkflowCommit | StoredReplay: ...

    def commit_transition(
        self,
        transition: PolicyOrVerificationTransition,
        next_checkpoint: WorkflowCheckpointDraft,
        *,
        expected_workflow_sequence: int,
        expected_task_sequence: int | None,
    ) -> WorkflowCheckpointV4: ...

    def lookup_operation(
        self, operation_id: WorkflowOperationId
    ) -> OperationLookup: ...

    def mark_unknown(
        self, operation: OperationHandle, *, reason: RecoveryCode
    ) -> UnknownCommit: ...


def _domain_digest(domain: bytes, body: bytes) -> str:
    if type(domain) is not bytes or not domain.endswith(b"\0"):
        raise ValueError("digest domain is invalid")
    if type(body) is not bytes:
        raise TypeError("digest body is invalid")
    return "sha256:" + hashlib.sha256(domain + body).hexdigest()


def digest_bounded_body(
    body: bytes, *, domain: bytes = EVENT_BODY_DIGEST_DOMAIN
) -> str:
    """Hash already-bounded bytes without retaining their contents."""

    if type(body) is not bytes or len(body) > MAX_CHECKPOINT_BYTES:
        raise ValueError("body is too large")
    return _domain_digest(domain, body)


def config_content_digest(raw: bytes) -> str:
    """Bind one bounded config byte snapshot without parsing or retaining it."""

    return digest_bounded_body(raw, domain=CONFIG_DIGEST_DOMAIN)


def wait_timeout_digest() -> str:
    """Return the fixed digest for a WAIT result with no Delivery."""

    return _domain_digest(
        WAIT_TIMEOUT_DIGEST_DOMAIN,
        _canonical_json({"result": "timeout"}),
    )


def assignment_digest(assignment: ActiveAssignment) -> str:
    """Bind the identity-only assignment projection returned by PROMPT."""

    if type(assignment) is not ActiveAssignment:
        raise TypeError("assignment is invalid")
    assignment.__post_init__()
    return _domain_digest(
        ASSIGNMENT_DIGEST_DOMAIN,
        _canonical_json(_assignment_mapping(assignment)),
    )


def delivery_content_digest(
    *,
    delivery_id: str,
    consumer_generation: int,
    ordered_message_ids: tuple[str, ...],
    ordered_event_projection: tuple[EventProjection, ...],
) -> str:
    """Bind immutable Delivery identity, order, and event projections."""

    delivery_id = _require_identifier(delivery_id, "delivery_id")
    consumer_generation = _require_int(
        consumer_generation,
        "delivery consumer_generation",
    )
    message_ids = _require_id_tuple(ordered_message_ids, "ordered_message_ids")
    projections = _require_tuple(
        ordered_event_projection,
        "ordered_event_projection",
    )
    if not projections or any(
        type(item) is not EventProjection for item in projections
    ):
        raise ValueError("event projection is invalid")
    typed_projections = tuple(cast(EventProjection, item) for item in projections)
    for projection in typed_projections:
        projection.__post_init__()
    if len({item.kind for item in typed_projections}) != 1:
        raise ValueError("delivery event kinds must not be mixed")
    kind = typed_projections[0].kind
    if kind is EventProjectionKind.QUESTION and not message_ids:
        raise ValueError("question delivery requires messages")
    if kind is not EventProjectionKind.QUESTION and len(typed_projections) != 1:
        raise ValueError("terminal delivery has one projection")
    projected_ids = tuple(
        item.message_id for item in typed_projections if item.message_id is not None
    )
    if projected_ids != message_ids:
        raise ValueError("delivery message order does not match projection")
    mapping = {
        "delivery_id": delivery_id,
        "consumer_generation": consumer_generation,
        "ordered_message_ids": list(message_ids),
        "ordered_event_projection": [
            _projection_mapping(item) for item in typed_projections
        ],
    }
    return _domain_digest(DELIVERY_DIGEST_DOMAIN, _canonical_json(mapping))


def operation_intent_digest(intent: OperationIntent, *, intent_sequence: int) -> str:
    """Bind every durable intent field, including its Store-issued sequence."""

    if type(intent) is not OperationIntent:
        raise TypeError("operation intent is invalid")
    intent.__post_init__()
    intent_sequence = _require_int(intent_sequence, "intent_sequence", minimum=1)
    mapping: dict[str, object] = {
        "operation_id": intent.operation_id,
        "effect_key": intent.effect_key,
        "root_key": intent.root_key,
        "root": None if intent.root is None else _root_mapping(intent.root),
        "action": intent.action.value,
        "request_digest": intent.request_digest,
        "expected_workflow_sequence": intent.expected_workflow_sequence,
        "expected_task_sequence": intent.expected_task_sequence,
        "intent_sequence": intent_sequence,
        "next_task_sequence": intent.next_task_sequence,
        "run_id": intent.run_id,
        "main_terminal_id": intent.main_terminal_id,
        "task_id": intent.task_id,
        "dispatch_id": intent.dispatch_id,
        "attempt": intent.attempt,
        "terminal_id": intent.terminal_id,
        "delivery_id": intent.delivery_id,
        "message_id": intent.message_id,
        "consumer_generation": intent.consumer_generation,
        "owner": intent.owner,
        "lease_epoch": intent.lease_epoch,
        "fencing_token": intent.fencing_token,
        "actor": intent.actor,
        "evidence_ref": intent.evidence_ref,
    }
    return _domain_digest(INTENT_DIGEST_DOMAIN, _canonical_json(mapping))


def durable_receipt_digest(receipt: DurableReceipt) -> str:
    """Bind one adapter-issued receipt without its process-local issuer token."""

    _validate_durable_receipt(receipt)
    mapping: dict[str, object] = {
        "receipt_id": receipt.receipt_id,
        "operation_id": receipt.operation_id,
        "effect_key": receipt.effect_key,
        "receipt_schema_version": receipt.receipt_schema_version,
        "action": receipt.action.value,
        "request_digest": receipt.request_digest,
        "root_key": receipt.root_key,
        "run_id": receipt.run_id,
        "main_terminal_id": receipt.main_terminal_id,
        "task_id": receipt.task_id,
        "dispatch_id": receipt.dispatch_id,
        "attempt": receipt.attempt,
        "terminal_id": receipt.terminal_id,
        "delivery_id": receipt.delivery_id,
        "message_id": receipt.message_id,
        "consumer_generation": receipt.consumer_generation,
        "owner": receipt.owner,
        "lease_epoch": receipt.lease_epoch,
        "fencing_token": receipt.fencing_token,
        "effect_ref": receipt.effect_ref,
        "result_kind": receipt.result_kind,
        "result_digest": receipt.result_digest,
        "evidence_ref": receipt.evidence_ref,
        "issued_ns": receipt.issued_ns,
    }
    return _domain_digest(RECEIPT_DIGEST_DOMAIN, _canonical_json(mapping))


def _canonical_json(mapping: Mapping[str, object]) -> bytes:
    try:
        encoded = (
            json.dumps(
                mapping,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise CheckpointSchemaError("canonical JSON encoding failed") from exc
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise CheckpointSchemaError("checkpoint is too large")
    return encoded


def _json_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object field")
        result[key] = value
    return result


def _parse_int(value: str) -> int:
    digits = value.removeprefix("-")
    if len(digits) > 19:
        raise ValueError("integer is too large")
    parsed = int(value)
    if not 0 <= parsed <= MAX_SEQUENCE:
        raise ValueError("integer is out of range")
    return parsed


def _parse_float(value: str) -> object:
    del value
    raise ValueError("floating point values are not supported")


def _parse_constant(value: str) -> object:
    del value
    raise ValueError("JSON constants are not supported")


def _strict_json(raw: bytes, *, seed: bool = False) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_CHECKPOINT_BYTES:
        raise _schema_error("wire bytes are invalid", seed=seed)
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(
            text,
            object_pairs_hook=_json_object_pairs,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_parse_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _schema_error("wire JSON is invalid", seed=seed) from exc
    if type(parsed) is not dict:
        raise _schema_error("wire value must be one JSON object", seed=seed)
    try:
        canonical = _canonical_json(parsed)
    except CheckpointSchemaError as exc:
        if seed:
            raise SeedSchemaError("seed JSON is not canonical") from exc
        raise
    if canonical != raw:
        raise _schema_error("wire bytes are not canonical", seed=seed)
    return parsed


def _expect_object(
    value: object, fields: Sequence[str], name: str
) -> dict[str, object]:
    if type(value) is not dict or tuple(value) != tuple(fields):
        raise CheckpointSchemaError(f"{name} fields are not canonical")
    return cast(dict[str, object], value)


def _root_mapping(root: RootIdentity) -> dict[str, object]:
    if type(root) is not RootIdentity:
        raise TypeError("root must be a RootIdentity")
    return {
        "root_key": root.root_key,
        "team_id": root.team_id,
        "workspace_path": root.workspace_path,
        "workspace_device": root.workspace_device,
        "workspace_inode": root.workspace_inode,
        "config_device": root.config_device,
        "config_inode": root.config_inode,
        "config_digest": root.config_digest,
        "state_root_device": root.state_root_device,
        "state_root_inode": root.state_root_inode,
        "config_path": root.config_path,
        "state_root": root.state_root_path,
    }


def _decode_root(value: object) -> RootIdentity:
    mapping = _expect_object(value, ROOT_FIELDS, "root")
    try:
        workspace = PathIdentity(
            path=_require_text(
                mapping["workspace_path"],
                "workspace_path",
                maximum_bytes=MAX_PATH_BYTES,
            ),
            device=_require_int(mapping["workspace_device"], "workspace_device"),
            inode=_require_int(
                mapping["workspace_inode"], "workspace_inode", minimum=1
            ),
        )
        state_root = PathIdentity(
            path=_require_text(
                mapping["state_root"], "state_root", maximum_bytes=MAX_PATH_BYTES
            ),
            device=_require_int(mapping["state_root_device"], "state_root_device"),
            inode=_require_int(
                mapping["state_root_inode"], "state_root_inode", minimum=1
            ),
        )
        return RootIdentity(
            root_key=_require_text(mapping["root_key"], "root_key"),
            team_id=_require_text(mapping["team_id"], "team_id"),
            workspace=workspace,
            config_path=_require_text(
                mapping["config_path"], "config_path", maximum_bytes=MAX_PATH_BYTES
            ),
            config_device=_require_int(mapping["config_device"], "config_device"),
            config_inode=_require_int(
                mapping["config_inode"], "config_inode", minimum=1
            ),
            config_digest=_require_digest(mapping["config_digest"], "config_digest"),
            state_root=state_root,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointSchemaError("root values are invalid") from exc


def _run_mapping(run: RunIdentity) -> dict[str, object]:
    if type(run) is not RunIdentity:
        raise TypeError("run must be a RunIdentity")
    return {
        "run_id": run.run_id,
        "main_terminal_id": run.main_terminal_id,
        "consumer_generation": run.consumer_generation,
    }


def _decode_run(value: object) -> RunIdentity:
    mapping = _expect_object(value, RUN_FIELDS, "run")
    try:
        return RunIdentity(
            run_id=_require_text(mapping["run_id"], "run_id"),
            main_terminal_id=_require_text(
                mapping["main_terminal_id"], "main_terminal_id"
            ),
            consumer_generation=_require_int(
                mapping["consumer_generation"], "consumer_generation"
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointSchemaError("run values are invalid") from exc


def _policy_mapping(policy: TaskPolicyReference) -> dict[str, object]:
    if type(policy) is not TaskPolicyReference:
        raise TypeError("policy must be a TaskPolicyReference")
    return {
        "version": policy.version,
        "team_id": policy.team_id,
        "workspace": policy.workspace,
        "task_id": policy.task_id,
        "sequence": policy.sequence,
        "state_digest": policy.state_digest,
    }


def _decode_policy(value: object) -> TaskPolicyReference:
    mapping = _expect_object(value, POLICY_FIELDS, "task_policy")
    try:
        return TaskPolicyReference(
            version=_require_int(mapping["version"], "task policy version"),
            team_id=_require_text(mapping["team_id"], "task policy team_id"),
            workspace=_require_text(
                mapping["workspace"],
                "task policy workspace",
                maximum_bytes=MAX_PATH_BYTES,
            ),
            task_id=_require_text(mapping["task_id"], "task policy task_id"),
            sequence=_require_int(mapping["sequence"], "task policy sequence"),
            state_digest=_require_digest(
                mapping["state_digest"], "task policy state_digest"
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointSchemaError("task policy values are invalid") from exc


def _completion_mapping(identity: CompletionIdentity) -> dict[str, object]:
    if type(identity) is not CompletionIdentity:
        raise TypeError("completion identity is invalid")
    return {
        "run_id": identity.run_id,
        "task_id": identity.task_id,
        "dispatch_id": identity.dispatch_id,
        "sender_terminal_id": identity.sender_terminal_id,
    }


def _decode_completion(value: object) -> CompletionIdentity:
    mapping = _expect_object(value, COMPLETION_FIELDS, "completion_identity")
    try:
        return CompletionIdentity(
            run_id=_require_text(mapping["run_id"], "completion run_id"),
            task_id=_require_text(mapping["task_id"], "completion task_id"),
            dispatch_id=_require_text(mapping["dispatch_id"], "completion dispatch_id"),
            sender_terminal_id=_require_text(
                mapping["sender_terminal_id"], "completion sender_terminal_id"
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointSchemaError("completion identity values are invalid") from exc


def _assignment_mapping(assignment: ActiveAssignment) -> dict[str, object]:
    if type(assignment) is not ActiveAssignment:
        raise TypeError("assignment is invalid")
    return {
        "role": assignment.role.value,
        "worker_node": assignment.worker_node,
        "task_id": assignment.task_id,
        "attempt": assignment.attempt,
        "dispatch_id": assignment.dispatch_id,
        "terminal_id": assignment.terminal_id,
        "launch_mode": assignment.launch_mode.value,
        "completion_identity": _completion_mapping(assignment.completion_identity),
    }


def _decode_assignment(value: object) -> ActiveAssignment:
    mapping = _expect_object(value, ASSIGNMENT_FIELDS, "active_assignment")
    try:
        return ActiveAssignment(
            role=_enum_value(mapping["role"], AssignmentRole, "role"),
            worker_node=_require_text(mapping["worker_node"], "worker_node"),
            task_id=_require_text(mapping["task_id"], "assignment task_id"),
            attempt=_require_int(mapping["attempt"], "attempt", minimum=1),
            dispatch_id=_require_text(mapping["dispatch_id"], "assignment dispatch_id"),
            terminal_id=_require_text(mapping["terminal_id"], "assignment terminal_id"),
            launch_mode=_enum_value(mapping["launch_mode"], LaunchMode, "launch_mode"),
            completion_identity=_decode_completion(mapping["completion_identity"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointSchemaError("assignment values are invalid") from exc


def _projection_mapping(projection: EventProjection) -> dict[str, object]:
    if type(projection) is not EventProjection:
        raise TypeError("projection is invalid")
    return {
        "kind": projection.kind.value,
        "message_id": projection.message_id,
        "completion_identity": _completion_mapping(projection.completion_identity),
        "outcome": None if projection.outcome is None else projection.outcome.value,
        "body_digest": projection.body_digest,
    }


def _decode_projection(value: object) -> EventProjection:
    mapping = _expect_object(value, PROJECTION_FIELDS, "event_projection")
    try:
        outcome = (
            None
            if mapping["outcome"] is None
            else _enum_value(mapping["outcome"], EventOutcome, "event outcome")
        )
        return EventProjection(
            kind=_enum_value(mapping["kind"], EventProjectionKind, "event kind"),
            message_id=_require_optional_text(mapping["message_id"], "message_id"),
            completion_identity=_decode_completion(mapping["completion_identity"]),
            outcome=outcome,
            body_digest=_require_digest(mapping["body_digest"], "body_digest"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointSchemaError("event projection values are invalid") from exc


def _delivery_mapping(delivery: PendingDelivery) -> dict[str, object]:
    if type(delivery) is not PendingDelivery:
        raise TypeError("delivery is invalid")
    return {
        "delivery_id": delivery.delivery_id,
        "consumer_generation": delivery.consumer_generation,
        "ordered_message_ids": list(delivery.ordered_message_ids),
        "ordered_event_projection": [
            _projection_mapping(item) for item in delivery.ordered_event_projection
        ],
        "delivery_digest": delivery.delivery_digest,
        "ack_operation_id": delivery.ack_operation_id,
        "ack_status": delivery.ack_status.value,
    }


def _decode_delivery(value: object) -> PendingDelivery:
    mapping = _expect_object(value, DELIVERY_FIELDS, "pending_delivery")
    try:
        message_ids = mapping["ordered_message_ids"]
        projections = mapping["ordered_event_projection"]
        if type(message_ids) is not list or type(projections) is not list:
            raise ValueError("delivery arrays are invalid")
        message_values = cast(list[object], message_ids)
        projection_values = cast(list[object], projections)
        return PendingDelivery(
            delivery_id=_require_text(mapping["delivery_id"], "delivery_id"),
            consumer_generation=_require_int(
                mapping["consumer_generation"], "delivery consumer_generation"
            ),
            ordered_message_ids=tuple(
                _require_text(item, "ordered_message_ids item")
                for item in message_values
            ),
            ordered_event_projection=tuple(
                _decode_projection(item) for item in projection_values
            ),
            delivery_digest=_require_digest(
                mapping["delivery_digest"], "delivery_digest"
            ),
            ack_operation_id=_require_optional_text(
                mapping["ack_operation_id"], "ack_operation_id"
            ),
            ack_status=_enum_value(mapping["ack_status"], AckStatus, "ack_status"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointSchemaError("delivery values are invalid") from exc


def _authority_mapping(authority: AuthorityReference) -> dict[str, object]:
    if type(authority) is not AuthorityReference:
        raise TypeError("authority is invalid")
    return {"reference": authority.reference, "digest": authority.digest}


def _decode_authority(value: object) -> AuthorityReference:
    mapping = _expect_object(value, AUTHORITY_FIELDS, "authority")
    try:
        return AuthorityReference(
            reference=_require_text(mapping["reference"], "authority reference"),
            digest=_require_digest(mapping["digest"], "authority digest"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointSchemaError("authority values are invalid") from exc


def _last_operation_mapping(operation: LastOperation) -> dict[str, object]:
    if type(operation) is not LastOperation:
        raise TypeError("last operation is invalid")
    return {
        "operation_id": operation.operation_id,
        "effect_key": operation.effect_key,
        "action": operation.action.value,
        "request_digest": operation.request_digest,
        "expected_workflow_sequence": operation.expected_workflow_sequence,
        "expected_task_sequence": operation.expected_task_sequence,
        "status": operation.status.value,
        "receipt_id": operation.receipt_id,
        "receipt_digest": operation.receipt_digest,
    }


def _decode_last_operation(value: object) -> LastOperation:
    mapping = _expect_object(value, LAST_OPERATION_FIELDS, "last_operation")
    try:
        return LastOperation(
            operation_id=_require_text(mapping["operation_id"], "last operation_id"),
            effect_key=_require_text(mapping["effect_key"], "last effect_key"),
            action=_enum_value(mapping["action"], OperationAction, "last action"),
            request_digest=_require_digest(
                mapping["request_digest"], "last request_digest"
            ),
            expected_workflow_sequence=_require_int(
                mapping["expected_workflow_sequence"], "expected workflow sequence"
            ),
            expected_task_sequence=_require_optional_int(
                mapping["expected_task_sequence"], "expected task sequence"
            ),
            status=_enum_value(mapping["status"], OperationStatus, "last status"),
            receipt_id=_require_optional_text(mapping["receipt_id"], "last receipt_id"),
            receipt_digest=(
                None
                if mapping["receipt_digest"] is None
                else _require_digest(mapping["receipt_digest"], "last receipt_digest")
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointSchemaError("last operation values are invalid") from exc


def _checkpoint_body_mapping(value: WorkflowCheckpointV4) -> dict[str, object]:
    return {
        "checkpoint_version": value.checkpoint_version,
        "store_schema": value.store_schema,
        "task_policy_version": value.task_policy_version,
        "root": _root_mapping(value.root),
        "run": _run_mapping(value.run),
        "workflow_sequence": value.workflow_sequence,
        "task_sequence": value.task_sequence,
        "execution_mode": value.execution_mode.value,
        "workflow_state": value.workflow_state.value,
        "task_policy": None
        if value.task_policy is None
        else _policy_mapping(value.task_policy),
        "active_assignment": None
        if value.active_assignment is None
        else _assignment_mapping(value.active_assignment),
        "pending_delivery": None
        if value.pending_delivery is None
        else _delivery_mapping(value.pending_delivery),
        "replied_message_ids": list(value.replied_message_ids),
        "read_observed": value.read_observed,
        "released": value.released,
        "review_authority": None
        if value.review_authority is None
        else _authority_mapping(value.review_authority),
        "verification_authority": None
        if value.verification_authority is None
        else _authority_mapping(value.verification_authority),
        "last_operation": None
        if value.last_operation is None
        else _last_operation_mapping(value.last_operation),
        "updated_ns": value.updated_ns,
    }


def checkpoint_mapping(value: WorkflowCheckpointV4) -> dict[str, object]:
    """Return a detached canonical mapping, including its digest field."""

    _validate_checkpoint_observation(value)
    body = _checkpoint_body_mapping(value)
    body_without_digest = dict(body)
    # Insert the digest at its fixed wire position rather than appending it.
    result: dict[str, object] = {}
    for name in CHECKPOINT_FIELDS:
        if name == "checkpoint_digest":
            result[name] = value.checkpoint_digest
        else:
            result[name] = body_without_digest[name]
    return result


def checkpoint_scalar_projection(value: WorkflowCheckpointV4) -> dict[str, object]:
    """Derive SQL scalar columns from the typed checkpoint, never vice versa."""

    _validate_checkpoint_observation(value)
    last = value.last_operation
    return {
        "root_key": value.root.root_key,
        "team_id": value.root.team_id,
        "workspace_path": value.root.workspace_path,
        "workspace_device": value.root.workspace_device,
        "workspace_inode": value.root.workspace_inode,
        "config_path": value.root.config_path,
        "config_device": value.root.config_device,
        "config_inode": value.root.config_inode,
        "config_digest": value.root.config_digest,
        "state_root": value.root.state_root_path,
        "state_root_device": value.root.state_root_device,
        "state_root_inode": value.root.state_root_inode,
        "run_id": value.run.run_id,
        "main_terminal_id": value.run.main_terminal_id,
        "checkpoint_version": value.checkpoint_version,
        "store_schema": value.store_schema,
        "task_policy_version": value.task_policy_version,
        "workflow_sequence": value.workflow_sequence,
        "task_sequence": value.task_sequence,
        "execution_mode": value.execution_mode.value,
        "workflow_state": value.workflow_state.value,
        "consumer_generation": value.run.consumer_generation,
        "read_observed": int(value.read_observed),
        "released": int(value.released),
        "checkpoint_digest": value.checkpoint_digest,
        "last_operation_id": None if last is None else last.operation_id,
        "last_operation_status": None if last is None else last.status.value,
        "last_operation_receipt_id": None if last is None else last.receipt_id,
        "updated_ns": value.updated_ns,
    }


def checkpoint_to_draft(value: WorkflowCheckpointV4) -> WorkflowCheckpointDraft:
    """Return the reducer fields from one validated Store observation."""

    _validate_checkpoint_observation(value)
    return WorkflowCheckpointDraft(
        root=value.root,
        run=value.run,
        workflow_sequence=value.workflow_sequence,
        task_sequence=value.task_sequence,
        execution_mode=value.execution_mode,
        workflow_state=value.workflow_state,
        task_policy=value.task_policy,
        active_assignment=value.active_assignment,
        pending_delivery=value.pending_delivery,
        replied_message_ids=value.replied_message_ids,
        read_observed=value.read_observed,
        released=value.released,
        review_authority=value.review_authority,
        verification_authority=value.verification_authority,
        last_operation=value.last_operation,
    )


def _seed_body_mapping(seed: WorkflowRootSeed) -> dict[str, object]:
    return {
        "seed_version": seed.seed_version,
        "checkpoint_version": seed.checkpoint_version,
        "store_schema": seed.store_schema,
        "root": _root_mapping(seed.root),
        "workflow_sequence": seed.workflow_sequence,
        "operation_id": seed.operation_id,
        "operation_status": None
        if seed.operation_status is None
        else seed.operation_status.value,
        "workflow_state": seed.workflow_state.value,
        "updated_ns": seed.updated_ns,
    }


def encode_seed(seed: WorkflowRootSeed) -> bytes:
    if type(seed) is not WorkflowRootSeed:
        raise TypeError("seed must be a WorkflowRootSeed")
    seed.__post_init__()
    body = _seed_body_mapping(seed)
    result: dict[str, object] = {}
    for name in SEED_FIELDS:
        if name == "seed_digest":
            result[name] = seed.seed_digest
        else:
            result[name] = body[name]
    encoded = _canonical_json(result)
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise SeedSchemaError("seed is too large")
    return encoded


def seed_scalar_projection(seed: WorkflowRootSeed) -> dict[str, object]:
    """Derive every checkpoint-row scalar for a strict pre-start seed."""

    if type(seed) is not WorkflowRootSeed:
        raise TypeError("seed must be a WorkflowRootSeed")
    seed.__post_init__()
    encoded = encode_seed(seed)
    return {
        "root_key": seed.root.root_key,
        "team_id": seed.root.team_id,
        "workspace_path": seed.root.workspace_path,
        "workspace_device": seed.root.workspace_device,
        "workspace_inode": seed.root.workspace_inode,
        "config_path": seed.root.config_path,
        "config_device": seed.root.config_device,
        "config_inode": seed.root.config_inode,
        "config_digest": seed.root.config_digest,
        "state_root": seed.root.state_root_path,
        "state_root_device": seed.root.state_root_device,
        "state_root_inode": seed.root.state_root_inode,
        "run_id": None,
        "main_terminal_id": None,
        "checkpoint_version": seed.checkpoint_version,
        "store_schema": seed.store_schema,
        "task_policy_version": None,
        "workflow_sequence": seed.workflow_sequence,
        "task_sequence": None,
        "execution_mode": ExecutionMode.SERIAL.value,
        "workflow_state": seed.workflow_state.value,
        "consumer_generation": 0,
        "read_observed": 0,
        "released": 0,
        "checkpoint_bytes": encoded,
        "checkpoint_digest": seed.seed_digest,
        "last_operation_id": seed.operation_id,
        "last_operation_status": (
            None if seed.operation_status is None else seed.operation_status.value
        ),
        "last_operation_receipt_id": None,
        "updated_ns": seed.updated_ns,
    }


def decode_seed(raw: bytes) -> WorkflowRootSeed:
    parsed = _strict_json(raw, seed=True)
    if tuple(parsed) != SEED_FIELDS:
        raise SeedSchemaError("seed fields are not canonical")
    try:
        if parsed["seed_version"] != SEED_VERSION:
            raise ValueError("seed version")
        if parsed["checkpoint_version"] != CHECKPOINT_VERSION:
            raise ValueError("checkpoint version")
        if parsed["store_schema"] != STORE_SCHEMA:
            raise ValueError("store schema")
        seed = WorkflowRootSeed(
            root=_decode_root(parsed["root"]),
            workflow_sequence=_require_int(
                parsed["workflow_sequence"], "seed workflow_sequence"
            ),
            operation_id=_require_optional_text(
                parsed["operation_id"], "seed operation_id"
            ),
            operation_status=(
                None
                if parsed["operation_status"] is None
                else _enum_value(
                    parsed["operation_status"], OperationStatus, "seed operation_status"
                )
            ),
            updated_ns=_require_int(parsed["updated_ns"], "seed updated_ns"),
        )
        if parsed["workflow_state"] != seed.workflow_state.value:
            raise ValueError("seed state")
        provided_digest = _require_digest(parsed["seed_digest"], "seed_digest")
        expected_digest = _domain_digest(
            SEED_DIGEST_DOMAIN, _canonical_json(_seed_body_mapping(seed))
        )
        if not hmac.compare_digest(provided_digest, expected_digest):
            raise ValueError("seed digest")
        return seed
    except (KeyError, TypeError, ValueError, CheckpointSchemaError) as exc:
        raise SeedSchemaError("seed values are invalid") from exc


def encode_checkpoint(value: WorkflowCheckpointV4) -> bytes:
    _validate_checkpoint_observation(value)
    body = _checkpoint_body_mapping(value)
    expected_digest = _domain_digest(CHECKPOINT_DIGEST_DOMAIN, _canonical_json(body))
    if not hmac.compare_digest(value.checkpoint_digest, expected_digest):
        raise CheckpointSchemaError("checkpoint digest is invalid")
    result: dict[str, object] = {}
    for name in CHECKPOINT_FIELDS:
        if name == "checkpoint_digest":
            result[name] = value.checkpoint_digest
        else:
            result[name] = body[name]
    return _canonical_json(result)


def _decode_checkpoint_mapping(parsed: dict[str, object]) -> WorkflowCheckpointV4:
    if tuple(parsed) != CHECKPOINT_FIELDS:
        raise CheckpointSchemaError("checkpoint fields are not canonical")
    try:
        if parsed["checkpoint_version"] != CHECKPOINT_VERSION:
            raise ValueError("checkpoint version")
        if parsed["store_schema"] != STORE_SCHEMA:
            raise ValueError("store schema")
        policy = (
            None
            if parsed["task_policy"] is None
            else _decode_policy(parsed["task_policy"])
        )
        replied_values = parsed["replied_message_ids"]
        if type(replied_values) is not list:
            raise ValueError("replied message list")
        draft = WorkflowCheckpointDraft(
            root=_decode_root(parsed["root"]),
            run=_decode_run(parsed["run"]),
            workflow_sequence=_require_int(
                parsed["workflow_sequence"], "workflow_sequence"
            ),
            task_sequence=_require_optional_int(
                parsed["task_sequence"], "task_sequence"
            ),
            execution_mode=_enum_value(
                parsed["execution_mode"], ExecutionMode, "execution_mode"
            ),
            workflow_state=_enum_value(
                parsed["workflow_state"], CheckpointState, "workflow_state"
            ),
            task_policy=policy,
            active_assignment=(
                None
                if parsed["active_assignment"] is None
                else _decode_assignment(parsed["active_assignment"])
            ),
            pending_delivery=(
                None
                if parsed["pending_delivery"] is None
                else _decode_delivery(parsed["pending_delivery"])
            ),
            replied_message_ids=tuple(
                _require_text(item, "replied_message_ids item")
                for item in cast(list[object], replied_values)
            ),
            read_observed=_require_bool(parsed["read_observed"], "read_observed"),
            released=_require_bool(parsed["released"], "released"),
            review_authority=(
                None
                if parsed["review_authority"] is None
                else _decode_authority(parsed["review_authority"])
            ),
            verification_authority=(
                None
                if parsed["verification_authority"] is None
                else _decode_authority(parsed["verification_authority"])
            ),
            last_operation=(
                None
                if parsed["last_operation"] is None
                else _decode_last_operation(parsed["last_operation"])
            ),
        )
        if parsed["task_policy_version"] != draft.task_policy_version:
            raise ValueError("task policy version projection")
        updated_ns = _require_int(parsed["updated_ns"], "updated_ns")
        provided_digest = _require_digest(
            parsed["checkpoint_digest"], "checkpoint_digest"
        )
        body = dict(parsed)
        del body["checkpoint_digest"]
        expected_digest = _domain_digest(
            CHECKPOINT_DIGEST_DOMAIN, _canonical_json(body)
        )
        if not hmac.compare_digest(provided_digest, expected_digest):
            raise ValueError("checkpoint digest")
        return _issue_checkpoint(draft, updated_ns=updated_ns, issuer=_DECODE_ISSUER)
    except (KeyError, TypeError, ValueError, CheckpointSchemaError) as exc:
        raise CheckpointSchemaError("checkpoint values are invalid") from exc


def decode_checkpoint(raw: bytes) -> WorkflowCheckpointV4:
    parsed = _strict_json(raw)
    checkpoint = _decode_checkpoint_mapping(parsed)
    if encode_checkpoint(checkpoint) != raw:
        raise CheckpointSchemaError("checkpoint bytes are not canonical")
    return checkpoint


def compute_checkpoint_digest(value: bytes | WorkflowCheckpointV4) -> str:
    """Compute the v4 self-excluding digest from bytes or an observation."""

    if type(value) is WorkflowCheckpointV4:
        _validate_checkpoint_observation(value)
        return _domain_digest(
            CHECKPOINT_DIGEST_DOMAIN,
            _canonical_json(_checkpoint_body_mapping(value)),
        )
    if type(value) is not bytes:
        raise TypeError("checkpoint digest input is invalid")
    parsed = _strict_json(value)
    if tuple(parsed) != CHECKPOINT_FIELDS:
        raise CheckpointSchemaError("checkpoint fields are not canonical")
    body = dict(parsed)
    del body["checkpoint_digest"]
    return _domain_digest(CHECKPOINT_DIGEST_DOMAIN, _canonical_json(body))


def compute_seed_digest(value: bytes | WorkflowRootSeed) -> str:
    if type(value) is WorkflowRootSeed:
        return value.seed_digest
    if type(value) is not bytes:
        raise TypeError("seed digest input is invalid")
    parsed = _strict_json(value, seed=True)
    if tuple(parsed) != SEED_FIELDS:
        raise SeedSchemaError("seed fields are not canonical")
    body = dict(parsed)
    del body["seed_digest"]
    return _domain_digest(SEED_DIGEST_DOMAIN, _canonical_json(body))


def _issue_checkpoint(
    draft: WorkflowCheckpointDraft,
    *,
    updated_ns: int,
    issuer: object,
) -> WorkflowCheckpointV4:
    if type(draft) is not WorkflowCheckpointDraft:
        raise TypeError("checkpoint draft is invalid")
    _require_int(updated_ns, "updated_ns")
    checkpoint = object.__new__(WorkflowCheckpointV4)
    values = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "store_schema": STORE_SCHEMA,
        "task_policy_version": draft.task_policy_version,
        "root": draft.root,
        "run": draft.run,
        "workflow_sequence": draft.workflow_sequence,
        "task_sequence": draft.task_sequence,
        "execution_mode": draft.execution_mode,
        "workflow_state": draft.workflow_state,
        "task_policy": draft.task_policy,
        "active_assignment": draft.active_assignment,
        "pending_delivery": draft.pending_delivery,
        "replied_message_ids": draft.replied_message_ids,
        "read_observed": draft.read_observed,
        "released": draft.released,
        "review_authority": draft.review_authority,
        "verification_authority": draft.verification_authority,
        "last_operation": draft.last_operation,
        "checkpoint_digest": "sha256:" + "0" * 64,
        "updated_ns": updated_ns,
        "_provenance": issuer,
    }
    for name, value in values.items():
        object.__setattr__(checkpoint, name, value)
    body = _checkpoint_body_mapping(checkpoint)
    object.__setattr__(
        checkpoint,
        "checkpoint_digest",
        _domain_digest(CHECKPOINT_DIGEST_DOMAIN, _canonical_json(body)),
    )
    checkpoint.__post_init__()
    return checkpoint


_DECODE_ISSUER = object()


__all__ = [
    "ASSIGNMENT_DIGEST_DOMAIN",
    "CHECKPOINT_DIGEST_DOMAIN",
    "CHECKPOINT_FIELDS",
    "CHECKPOINT_VERSION",
    "CONFIG_DIGEST_DOMAIN",
    "DELIVERY_DIGEST_DOMAIN",
    "EVENT_BODY_DIGEST_DOMAIN",
    "INTENT_DIGEST_DOMAIN",
    "MAX_CHECKPOINT_BYTES",
    "MAX_COLLECTION_ITEMS",
    "MAX_IDENTIFIER_BYTES",
    "MAX_PATH_BYTES",
    "MAX_SEQUENCE",
    "RECEIPT_DIGEST_DOMAIN",
    "REQUEST_DIGEST_DOMAIN",
    "SEED_DIGEST_DOMAIN",
    "SEED_FIELDS",
    "SEED_VERSION",
    "STORE_SCHEMA",
    "WAIT_TIMEOUT_DIGEST_DOMAIN",
    "WORKFLOW_EVENT_DIGEST_DOMAIN",
    "WORKFLOW_EVENT_SCHEMA_VERSION",
    "AckStatus",
    "ActiveAssignment",
    "AssignmentRole",
    "AuthorityReference",
    "CheckpointSchemaError",
    "CheckpointState",
    "CompletionIdentity",
    "DurableReceipt",
    "EffectKey",
    "EventKind",
    "EventOutcome",
    "EventProjection",
    "EventProjectionKind",
    "ExecutionMode",
    "LastOperation",
    "LaunchMode",
    "OperationAction",
    "OperationBegin",
    "OperationHandle",
    "OperationIdentityConflict",
    "OperationIntent",
    "OperationLookup",
    "OperationStatus",
    "Outcome",
    "PathIdentity",
    "PendingDelivery",
    "PolicyOrVerificationTransition",
    "RecoveryCode",
    "RecoveryRequired",
    "RootIdentity",
    "RunIdentity",
    "SeedSchemaError",
    "StateConflict",
    "StoredReplay",
    "TaskPolicyReference",
    "TransitionKind",
    "UnknownCommit",
    "WorkflowCheckpointDraft",
    "WorkflowCheckpointObservation",
    "WorkflowCheckpointV4",
    "WorkflowOperationId",
    "WorkflowRootKey",
    "WorkflowRootSeed",
    "WorkflowState",
    "WorkflowStoreError",
    "WorkflowStorePort",
    "assignment_digest",
    "checkpoint_mapping",
    "checkpoint_scalar_projection",
    "checkpoint_to_draft",
    "compute_checkpoint_digest",
    "compute_seed_digest",
    "config_content_digest",
    "decode_checkpoint",
    "decode_seed",
    "delivery_content_digest",
    "digest_bounded_body",
    "durable_receipt_digest",
    "encode_checkpoint",
    "encode_seed",
    "operation_intent_digest",
    "seed_scalar_projection",
    "wait_timeout_digest",
]
