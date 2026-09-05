"""Private durable workflow-effect composition seam."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, Protocol, SupportsIndex, TypeAlias, cast

from . import workflow_store as _workflow
from .contracts import (
    AckReceipt,
    Assignment,
    CompletionIdentity,
    DeliveryAck,
    DeliveryRef,
    DispatchRef,
    EventKind,
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
    RoleSpec,
    RoleWait,
    RunRef,
    RuntimeRequest,
    StartResult,
    StartSpec,
    StopResult,
    TaskRef,
    TerminalRef,
    WaitReceipt,
)
from .contracts import LaunchMode as PublicLaunchMode

_CAPABILITY_VERSION: Final[int] = 1
_COMMAND_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.command.v1\0"
_START_PARAMETER_DOMAIN: Final[bytes] = (
    b"agent-team.workflow-effect.start-parameter.v1\0"
)
_PROMPT_BODY_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.prompt-body.v1\0"
_REPLY_BODY_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.reply-body.v1\0"
_PARAMETER_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.parameter.v1\0"
_START_PATH_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.start-path.v1\0"
_START_ROLE_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.start-role.v1\0"
_REQUEST_IDENTITY_DOMAIN: Final[bytes] = (
    b"agent-team.workflow-effect.request-identity.v1\0"
)
_AUTHORITY_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.authority.v1\0"
_AUTHORITY_BINDING_DOMAIN: Final[bytes] = (
    b"agent-team.workflow-effect.authority-binding.v1\0"
)
_OPERATION_ID_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.operation-id.v1\0"
_EFFECT_KEY_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.effect-key.v1\0"
_EVIDENCE_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.evidence.v1\0"
_OBSERVATION_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.observation.v1\0"
_OBSERVATION_EVIDENCE_DOMAIN: Final[bytes] = (
    b"agent-team.workflow-effect.observation-evidence.v1\0"
)
_START_RESULT_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.started.v1\0"
_REPLY_RESULT_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.reply.v1\0"
_READ_RESULT_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.read.v1\0"
_RELEASE_RESULT_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.release.v1\0"
_ACK_RESULT_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.ack.v1\0"
_STOP_STAGE_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.stop-stage.v1\0"
_STOP_RESULT_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.stop-result.v1\0"
_RECEIPT_ID_DOMAIN: Final[bytes] = b"agent-team.workflow-effect.receipt-id.v1\0"
_AUTHORITY_ISSUER: Final[object] = object()
_IDENTITY_ISSUER: Final[object] = object()
_OBSERVATION_ISSUER: Final[object] = object()


class EffectAdapterError(Exception):
    """Base class for private durable effect composition failures."""


class DurabilityUnsupported(EffectAdapterError):
    """The injected backend cannot satisfy the durable effect contract."""


@dataclass(frozen=True, slots=True)
class DurableEffectCapabilities:
    """Exact capability declaration consumed only by the private adapter."""

    version: int
    effect_key_idempotency: bool
    pure_effect_lookup: bool
    attempt_fence_enforcement: bool
    consumer_generation: bool
    exact_delivery_lookup: bool
    exact_read_lookup: bool
    composite_stop: bool


def _validated_capability_values(
    capabilities: object,
) -> tuple[bool, bool, bool, bool, bool, bool, bool]:
    if type(capabilities) is not DurableEffectCapabilities:
        raise DurabilityUnsupported("durable backend capabilities are invalid")
    if type(capabilities.version) is not int or capabilities.version != 1:
        raise DurabilityUnsupported("durable backend capability version is unsupported")
    values = tuple(
        getattr(capabilities, name)
        for name in (
            "effect_key_idempotency",
            "pure_effect_lookup",
            "attempt_fence_enforcement",
            "consumer_generation",
            "exact_delivery_lookup",
            "exact_read_lookup",
            "composite_stop",
        )
    )
    if any(type(value) is not bool for value in values):
        raise DurabilityUnsupported("durable backend capabilities are invalid")
    typed = tuple(bool(value) for value in values)
    if not (typed[0] or typed[1]) or not typed[2] or not typed[3]:
        raise DurabilityUnsupported("durable backend capabilities are incomplete")
    return typed  # type: ignore[return-value]


def require_durable_capabilities(backend: object) -> DurableEffectCapabilities:
    """Fail closed before invoking any backend effect or fallback surface."""

    try:
        accessor = getattr(backend, "durability_capabilities", None)
    except Exception:  # noqa: BLE001 - capability properties are untrusted
        raise DurabilityUnsupported(
            "durable backend capabilities are unavailable"
        ) from None
    if not callable(accessor):
        raise DurabilityUnsupported("durable backend capabilities are unavailable")
    try:
        capabilities = accessor()
    except Exception:  # noqa: BLE001 - capability calls are untrusted
        raise DurabilityUnsupported(
            "durable backend capabilities are unavailable"
        ) from None
    try:
        execute = getattr(backend, "execute", None)
        lookup = getattr(backend, "lookup", None)
    except Exception:  # noqa: BLE001 - effect port properties are untrusted
        raise DurabilityUnsupported(
            "durable backend effect port is incomplete"
        ) from None
    if not callable(execute) or not callable(lookup):
        raise DurabilityUnsupported("durable backend effect port is incomplete")
    _validated_capability_values(capabilities)
    return cast(DurableEffectCapabilities, capabilities)


def require_durable_action(
    capabilities: DurableEffectCapabilities,
    action: _workflow.OperationAction,
) -> None:
    """Check fixed action-specific proof requirements before an effect."""

    values = _validated_capability_values(capabilities)
    if type(action) is not _workflow.OperationAction:
        raise DurabilityUnsupported("durable workflow action is invalid")
    required_index = {
        _workflow.OperationAction.WAIT: 4,
        _workflow.OperationAction.READ: 5,
        _workflow.OperationAction.STOP: 6,
    }.get(action)
    missing_stop_lookup = action is _workflow.OperationAction.STOP and not values[1]
    missing_action_capability = (
        required_index is not None and not values[required_index]
    )
    if missing_stop_lookup or missing_action_capability:
        raise DurabilityUnsupported(
            "durable backend lacks the requested action capability"
        )


@dataclass(frozen=True, slots=True)
class EffectCommand:
    """Body-free, deterministic identity input for one external effect."""

    action: _workflow.OperationAction
    parameter_digest: str
    command_digest: str
    role: Role | None = None
    timeout_ms: int | None = None
    lines: int | None = None
    message_id: str | None = None
    delivery_id: str | None = None
    team_id: str | None = None

    def __post_init__(self) -> None:
        _validate_effect_command(self)


@dataclass(frozen=True, slots=True)
class EffectRequestIdentity:
    """Authority-independent identity for one requested workflow effect."""

    command: EffectCommand
    root: _workflow.RootIdentity
    run: _workflow.RunIdentity | None
    assignment: _workflow.ActiveAssignment | None
    pending_delivery: _workflow.PendingDelivery | None
    expected_workflow_sequence: int
    expected_task_sequence: int | None
    request_digest: str
    binding_digest: str

    def __post_init__(self) -> None:
        _validate_effect_request_identity(self)


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class WorkflowEffectAuthority:
    """Return-only authority snapshot issued by the trusted composition root."""

    request_binding_digest: str
    backend_id: str
    provider_id: str
    owner: str
    lease_epoch: int
    fencing_token: int
    expires_ns: int
    authority_ref: str
    proof_ref: str
    authority_digest: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("WorkflowEffectAuthority is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("WorkflowEffectAuthority is return-only")

    def __repr__(self) -> str:
        return "<WorkflowEffectAuthority opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("WorkflowEffectAuthority cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("WorkflowEffectAuthority cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("WorkflowEffectAuthority cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("WorkflowEffectAuthority cannot be pickled")


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class EffectIdentity:
    """Final stable Store identity after binding trusted authority metadata."""

    operation_id: str
    effect_key: str
    request_digest: str
    evidence_ref: str
    request_binding_digest: str
    authority_binding_digest: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("EffectIdentity is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("EffectIdentity is return-only")

    def __repr__(self) -> str:
        return "<EffectIdentity opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("EffectIdentity cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("EffectIdentity cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("EffectIdentity cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("EffectIdentity cannot be pickled")


@dataclass(frozen=True, slots=True)
class CompositeStopStage:
    """One ordered stage in a backend-proven composite stop observation."""

    stage_id: str
    resource_ref: str
    effect_ref: str
    status: str
    evidence_digest: str

    def __post_init__(self) -> None:
        try:
            for value, name in (
                (self.stage_id, "stop stage_id"),
                (self.resource_ref, "stop resource_ref"),
                (self.effect_ref, "stop effect_ref"),
            ):
                _workflow._require_identifier(value, name)
            if type(self.status) is not str or self.status not in {
                "COMPLETED",
                "FAILED",
                "UNKNOWN",
            }:
                raise ValueError("stop stage status is invalid")
            _workflow._require_digest(
                self.evidence_digest,
                "stop evidence_digest",
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("composite stop stage is invalid") from exc


@dataclass(frozen=True, slots=True)
class CompositeStopObservation:
    """Successful ordered composite stop proof without raw cleanup output."""

    stages: tuple[CompositeStopStage, ...]
    composite_ref: str
    composite_digest: str

    def __post_init__(self) -> None:
        _validate_composite_stop(self)


EffectPublicResult: TypeAlias = (
    StartResult
    | Assignment
    | WaitReceipt
    | ReplyReceipt
    | ReadReceipt
    | ReleaseReceipt
    | AckReceipt
    | StopResult
)


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class BackendEffectObservation:
    """Transient, issuer-bound backend result validated before Store receipt issue."""

    operation_id: str
    effect_key: str
    action: _workflow.OperationAction
    request_digest: str
    root_key: str
    backend_id: str
    provider_id: str
    run_id: str
    main_terminal_id: str
    assignment: _workflow.ActiveAssignment | None
    delivery: _workflow.PendingDelivery | None
    message_id: str | None
    consumer_generation: int
    owner: str
    lease_epoch: int
    fencing_token: int
    effect_ref: str
    provider_proof_ref: str
    result_kind: str
    result_digest: str
    evidence_ref: str
    public_result: EffectPublicResult
    composite_stop: CompositeStopObservation | None
    observation_digest: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("BackendEffectObservation is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("BackendEffectObservation is return-only")

    def __repr__(self) -> str:
        return "<BackendEffectObservation opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("BackendEffectObservation cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("BackendEffectObservation cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("BackendEffectObservation cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("BackendEffectObservation cannot be pickled")


EffectPayload: TypeAlias = StartSpec | str | None


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class BackendEffectRequest:
    """Process-local backend handoff; raw payload is never durable or printable."""

    command: EffectCommand
    request: EffectRequestIdentity
    identity: EffectIdentity
    authority: WorkflowEffectAuthority
    operation: _workflow.OperationHandle
    payload: EffectPayload

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("BackendEffectRequest is adapter-issued")

    def __repr__(self) -> str:
        return "<BackendEffectRequest opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("BackendEffectRequest cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("BackendEffectRequest cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("BackendEffectRequest cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("BackendEffectRequest cannot be pickled")


@dataclass(frozen=True, slots=True)
class EffectProjection:
    next_checkpoint: _workflow.WorkflowCheckpointDraft
    public_result: EffectPublicResult

    def __post_init__(self) -> None:
        if type(self.next_checkpoint) is not _workflow.WorkflowCheckpointDraft:
            raise TypeError("effect projection checkpoint is invalid")
        if not _is_effect_public_result(self.public_result):
            raise TypeError("effect projection public result is invalid")


@dataclass(frozen=True, slots=True)
class AppliedEffect:
    operation_id: str
    receipt: _workflow.DurableReceipt
    checkpoint: _workflow.WorkflowCheckpointV4
    public_result: EffectPublicResult

    def __post_init__(self) -> None:
        _workflow._require_identifier(self.operation_id, "applied operation_id")
        if type(self.receipt) is not _workflow.DurableReceipt:
            raise TypeError("applied effect receipt is invalid")
        if type(self.checkpoint) is not _workflow.WorkflowCheckpointV4:
            raise TypeError("applied effect checkpoint is invalid")
        if not _is_effect_public_result(self.public_result):
            raise TypeError("applied effect public result is invalid")


@dataclass(frozen=True, slots=True)
class ReplayedEffect:
    operation_id: str
    snapshot: _workflow.WorkflowEffectSnapshot
    public_result: EffectPublicResult

    def __post_init__(self) -> None:
        _workflow._require_identifier(self.operation_id, "replayed operation_id")
        if type(self.snapshot) is not _workflow.WorkflowEffectSnapshot:
            raise TypeError("replayed effect snapshot is invalid")
        if not _is_effect_public_result(self.public_result):
            raise TypeError("replayed effect public result is invalid")


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class DurableDeliveryLookup:
    """Immutable WAIT origin plus its transient, digest-verified public result."""

    snapshot: _workflow.WorkflowEffectSnapshot
    result: WaitReceipt

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("DurableDeliveryLookup is adapter-issued")

    def __repr__(self) -> str:
        return "<DurableDeliveryLookup opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("DurableDeliveryLookup cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("DurableDeliveryLookup cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("DurableDeliveryLookup cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("DurableDeliveryLookup cannot be pickled")


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class DurableReadLookup:
    """Immutable READ effect snapshot plus exact transient output."""

    snapshot: _workflow.WorkflowEffectSnapshot
    result: ReadReceipt

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("DurableReadLookup is adapter-issued")

    def __repr__(self) -> str:
        return "<DurableReadLookup opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("DurableReadLookup cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("DurableReadLookup cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("DurableReadLookup cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("DurableReadLookup cannot be pickled")


class EffectRecoveryRequired(_workflow.RecoveryRequired):
    """The adapter stopped after an ambiguous effect without retrying it."""


class WorkflowEffectAuthorityPort(Protocol):
    def authorize(self, request: EffectRequestIdentity) -> WorkflowEffectAuthority: ...

    def validate(
        self,
        authority: WorkflowEffectAuthority,
        request: EffectRequestIdentity,
    ) -> None: ...

    def validate_observation(
        self,
        authority: WorkflowEffectAuthority,
        request: EffectRequestIdentity,
        observation: BackendEffectObservation,
    ) -> None: ...

    def validate_lookup(
        self,
        snapshot: _workflow.WorkflowEffectSnapshot,
        observation: BackendEffectObservation,
    ) -> None: ...


class DurableEffectBackendPort(Protocol):
    def durability_capabilities(self) -> DurableEffectCapabilities: ...

    def execute(self, effect: BackendEffectRequest) -> BackendEffectObservation: ...

    def lookup(
        self, snapshot: _workflow.WorkflowEffectSnapshot
    ) -> BackendEffectObservation: ...


class EffectProjectionPort(Protocol):
    def project(
        self,
        current: _workflow.WorkflowCheckpointObservation | None,
        request: EffectRequestIdentity,
        observation: BackendEffectObservation,
        receipt: _workflow.DurableReceipt,
    ) -> EffectProjection: ...


class WorkflowEffectStorePort(Protocol):
    def load_checkpoint(
        self, key: _workflow.WorkflowRootKey
    ) -> _workflow.WorkflowCheckpointObservation | None: ...

    def begin_operation(
        self,
        intent: _workflow.OperationIntent,
        *,
        expected_workflow_sequence: int,
        expected_task_sequence: int | None,
    ) -> _workflow.OperationBegin | _workflow.StoredReplay: ...

    def _issue_workflow_receipt(
        self,
        *,
        operation: _workflow.OperationHandle,
        receipt_id: str,
        run_id: str,
        main_terminal_id: str,
        consumer_generation: int,
        task_id: str | None,
        dispatch_id: str | None,
        attempt: int | None,
        terminal_id: str | None,
        delivery_id: str | None,
        message_id: str | None,
        effect_ref: str,
        result_kind: str,
        result_digest: str,
        evidence_ref: str,
        issued_ns: int,
    ) -> _workflow.DurableReceipt: ...

    def commit_effect(
        self,
        operation: _workflow.OperationHandle,
        receipt: _workflow.DurableReceipt,
        next_checkpoint: _workflow.WorkflowCheckpointDraft,
    ) -> _workflow.WorkflowCommit | _workflow.StoredReplay: ...

    def mark_unknown(
        self,
        operation: _workflow.OperationHandle,
        *,
        reason: _workflow.RecoveryCode,
    ) -> _workflow.UnknownCommit: ...

    def _lookup_workflow_effect(
        self,
        operation_id: _workflow.WorkflowOperationId,
    ) -> _workflow.WorkflowEffectSnapshot: ...

    def _lookup_workflow_delivery_effect(
        self,
        root_key: _workflow.WorkflowRootKey,
        delivery_id: str,
        consumer_generation: int,
    ) -> _workflow.WorkflowEffectSnapshot: ...


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("effect command value is not canonical") from exc


def _domain_digest(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json(value)).hexdigest()


def _bounded_body(value: object, name: str) -> bytes:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be non-blank text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} is not valid UTF-8") from exc
    if len(encoded) > _workflow.MAX_CHECKPOINT_BYTES:
        raise ValueError(f"{name} exceeds the durable effect limit")
    return encoded


def _body_digest(domain: bytes, body: bytes) -> str:
    return "sha256:" + hashlib.sha256(domain + body).hexdigest()


def _safe_text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise ValueError(f"{name} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} is not valid UTF-8") from exc
    if len(encoded) > _workflow.MAX_CHECKPOINT_BYTES:
        raise ValueError(f"{name} exceeds the durable effect limit")
    return value


def _opaque_ref(value: object, expected: type[object], name: str) -> str:
    if type(value) is not expected:
        raise TypeError(f"{name} has an invalid type")
    try:
        raw = object.__getattribute__(value, "_value")
        return _workflow._require_identifier(raw, name)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} is invalid") from exc


def _positive(value: object, name: str) -> int:
    try:
        return _workflow._require_int(value, name, minimum=1)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc


def _role(value: object) -> Role:
    if type(value) is not Role:
        raise TypeError("effect role has an invalid type")
    return value


def _command_mapping(command: EffectCommand) -> dict[str, object]:
    return {
        "action": command.action.value,
        "parameter_digest": command.parameter_digest,
        "role": None if command.role is None else command.role.value,
        "timeout_ms": command.timeout_ms,
        "lines": command.lines,
        "message_id": command.message_id,
        "delivery_id": command.delivery_id,
        "team_id": command.team_id,
    }


def _validate_effect_command(command: object) -> None:
    if type(command) is not EffectCommand:
        raise TypeError("effect command type is invalid")
    if type(command.action) is not _workflow.OperationAction:
        raise ValueError("effect command action is invalid")
    try:
        _workflow._require_digest(command.parameter_digest, "parameter_digest")
        _workflow._require_digest(command.command_digest, "command_digest")
    except (TypeError, ValueError) as exc:
        raise ValueError("effect command digest is invalid") from exc
    if command.role is not None:
        _role(command.role)
    if command.timeout_ms is not None:
        _positive(command.timeout_ms, "timeout_ms")
    if command.lines is not None:
        _positive(command.lines, "lines")
    for value, name in (
        (command.message_id, "message_id"),
        (command.delivery_id, "delivery_id"),
        (command.team_id, "team_id"),
    ):
        if value is not None:
            try:
                _workflow._require_identifier(value, name)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"effect command {name} is invalid") from exc
    expected_presence = {
        _workflow.OperationAction.START: (False, False, False, False, False, True),
        _workflow.OperationAction.PROMPT: (True, False, False, False, False, False),
        _workflow.OperationAction.WAIT: (True, True, False, False, False, False),
        _workflow.OperationAction.REPLY: (False, False, False, True, False, False),
        _workflow.OperationAction.READ: (True, False, True, False, False, False),
        _workflow.OperationAction.RELEASE: (True, False, False, False, False, False),
        _workflow.OperationAction.ACK: (False, False, False, False, True, False),
        _workflow.OperationAction.STOP: (False, False, False, False, False, False),
    }[command.action]
    actual_presence = tuple(
        value is not None
        for value in (
            command.role,
            command.timeout_ms,
            command.lines,
            command.message_id,
            command.delivery_id,
            command.team_id,
        )
    )
    if actual_presence != expected_presence:
        raise ValueError("effect command fields do not match its action")
    if command.command_digest != _domain_digest(
        _COMMAND_DOMAIN,
        _command_mapping(command),
    ):
        raise ValueError("effect command binding digest differs")


def _make_effect_command(
    *,
    action: _workflow.OperationAction,
    parameter_digest: str,
    role: Role | None = None,
    timeout_ms: int | None = None,
    lines: int | None = None,
    message_id: str | None = None,
    delivery_id: str | None = None,
    team_id: str | None = None,
) -> EffectCommand:
    provisional = object.__new__(EffectCommand)
    values: dict[str, object] = {
        "action": action,
        "parameter_digest": parameter_digest,
        "command_digest": "sha256:" + "0" * 64,
        "role": role,
        "timeout_ms": timeout_ms,
        "lines": lines,
        "message_id": message_id,
        "delivery_id": delivery_id,
        "team_id": team_id,
    }
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(
        provisional,
        "command_digest",
        _domain_digest(_COMMAND_DOMAIN, _command_mapping(provisional)),
    )
    provisional.__post_init__()
    return provisional


def _canonical_path_digest(value: object, name: str) -> str:
    if not isinstance(value, Path):
        raise TypeError(f"{name} has an invalid type")
    try:
        path = _workflow._require_path(str(value), name)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not canonical") from exc
    return _domain_digest(_START_PATH_DOMAIN, {"name": name, "path": path})


def _role_spec_digest(role: object, spec: object) -> tuple[str, str]:
    typed_role = _role(role)
    if type(spec) is not RoleSpec:
        raise TypeError("start role specification has an invalid type")
    values = {
        "role": typed_role.value,
        "provider": _safe_text(spec.provider, "role provider"),
        "transport": _safe_text(spec.transport, "role transport"),
        "model": _safe_text(spec.model, "role model"),
        "effort": _safe_text(spec.effort, "role effort"),
        "permission": _safe_text(spec.permission, "role permission"),
        "instructions_digest": _body_digest(
            _START_ROLE_DOMAIN,
            _safe_text(
                spec.instructions,
                "role instructions",
                allow_empty=True,
            ).encode("utf-8"),
        ),
        "execution": _safe_text(spec.execution, "role execution"),
        "adapter_id": (
            None
            if spec.adapter_id is None
            else _safe_text(spec.adapter_id, "role adapter_id")
        ),
    }
    return typed_role.value, _domain_digest(_START_ROLE_DOMAIN, values)


def make_start_command(spec: StartSpec) -> EffectCommand:
    if type(spec) is not StartSpec:
        raise TypeError("start specification has an invalid type")
    try:
        team_id = _workflow._require_identifier(spec.team_id, "team_id")
    except (TypeError, ValueError) as exc:
        raise ValueError("start team identity is invalid") from exc
    if not isinstance(spec.role_specs, Mapping):
        raise TypeError("start role specifications are invalid")
    role_digests = tuple(
        sorted(
            (
                _role_spec_digest(role, role_spec)
                for role, role_spec in spec.role_specs.items()
            ),
            key=lambda item: item[0],
        )
    )
    if type(spec.attach) is not bool:
        raise TypeError("start attach flag is invalid")
    if spec.attach:
        raise DurabilityUnsupported(
            "durable START attach requires an unsupported composite capability"
        )
    parameter_digest = _domain_digest(
        _START_PARAMETER_DOMAIN,
        {
            "team_id": team_id,
            "workspace_digest": _canonical_path_digest(spec.workspace, "workspace"),
            "config_path_digest": _canonical_path_digest(
                spec.config_path, "config_path"
            ),
            "state_path_digest": _canonical_path_digest(spec.state_path, "state_path"),
            "role_digests": role_digests,
            "attach": spec.attach,
        },
    )
    return _make_effect_command(
        action=_workflow.OperationAction.START,
        parameter_digest=parameter_digest,
        team_id=team_id,
    )


def make_request_command(request: RuntimeRequest) -> EffectCommand:
    request_type = type(request)
    if request_type is RolePrompt:
        prompt = cast(RolePrompt, request)
        role = _role(prompt.role)
        body_digest = _body_digest(
            _PROMPT_BODY_DOMAIN,
            _bounded_body(prompt.text, "prompt body"),
        )
        return _make_effect_command(
            action=_workflow.OperationAction.PROMPT,
            parameter_digest=_domain_digest(
                _PARAMETER_DOMAIN,
                {"action": "prompt", "role": role.value, "body": body_digest},
            ),
            role=role,
        )
    if request_type is RoleWait:
        wait = cast(RoleWait, request)
        role = _role(wait.role)
        timeout_ms = _positive(wait.timeout_ms, "timeout_ms")
        return _make_effect_command(
            action=_workflow.OperationAction.WAIT,
            parameter_digest=_domain_digest(
                _PARAMETER_DOMAIN,
                {"action": "wait", "role": role.value, "timeout_ms": timeout_ms},
            ),
            role=role,
            timeout_ms=timeout_ms,
        )
    if request_type is RoleRead:
        read = cast(RoleRead, request)
        role = _role(read.role)
        lines = _positive(read.lines, "lines")
        return _make_effect_command(
            action=_workflow.OperationAction.READ,
            parameter_digest=_domain_digest(
                _PARAMETER_DOMAIN,
                {"action": "read", "role": role.value, "lines": lines},
            ),
            role=role,
            lines=lines,
        )
    if request_type is RoleRelease:
        release = cast(RoleRelease, request)
        role = _role(release.role)
        return _make_effect_command(
            action=_workflow.OperationAction.RELEASE,
            parameter_digest=_domain_digest(
                _PARAMETER_DOMAIN,
                {"action": "release", "role": role.value},
            ),
            role=role,
        )
    if request_type is MessageReply:
        reply = cast(MessageReply, request)
        message_id = _opaque_ref(reply.message_id, MessageRef, "message_id")
        body_digest = _body_digest(
            _REPLY_BODY_DOMAIN,
            _bounded_body(reply.body, "reply body"),
        )
        return _make_effect_command(
            action=_workflow.OperationAction.REPLY,
            parameter_digest=_domain_digest(
                _PARAMETER_DOMAIN,
                {
                    "action": "reply",
                    "message_id": message_id,
                    "body": body_digest,
                },
            ),
            message_id=message_id,
        )
    if request_type is DeliveryAck:
        acknowledgement = cast(DeliveryAck, request)
        delivery_id = _opaque_ref(
            acknowledgement.delivery_id,
            DeliveryRef,
            "delivery_id",
        )
        return _make_effect_command(
            action=_workflow.OperationAction.ACK,
            parameter_digest=_domain_digest(
                _PARAMETER_DOMAIN,
                {"action": "ack", "delivery_id": delivery_id},
            ),
            delivery_id=delivery_id,
        )
    raise ValueError("request is not a durable workflow effect")


def make_stop_command() -> EffectCommand:
    return _make_effect_command(
        action=_workflow.OperationAction.STOP,
        parameter_digest=_domain_digest(_PARAMETER_DOMAIN, {"action": "stop"}),
    )


def _root_mapping(root: _workflow.RootIdentity) -> dict[str, object]:
    return {
        "root_key": root.root_key,
        "team_id": root.team_id,
        "workspace": {
            "path": root.workspace.path,
            "device": root.workspace.device,
            "inode": root.workspace.inode,
        },
        "config_path": root.config_path,
        "config_device": root.config_device,
        "config_inode": root.config_inode,
        "config_digest": root.config_digest,
        "state_root": {
            "path": root.state_root.path,
            "device": root.state_root.device,
            "inode": root.state_root.inode,
        },
    }


def _run_mapping(run: _workflow.RunIdentity | None) -> dict[str, object] | None:
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "main_terminal_id": run.main_terminal_id,
        "consumer_generation": run.consumer_generation,
    }


def _completion_mapping(
    completion: _workflow.CompletionIdentity,
) -> dict[str, object]:
    return {
        "run_id": completion.run_id,
        "task_id": completion.task_id,
        "dispatch_id": completion.dispatch_id,
        "sender_terminal_id": completion.sender_terminal_id,
    }


def _assignment_mapping(
    assignment: _workflow.ActiveAssignment | None,
) -> dict[str, object] | None:
    if assignment is None:
        return None
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


def _event_projection_mapping(
    projection: _workflow.EventProjection,
) -> dict[str, object]:
    return {
        "kind": projection.kind.value,
        "message_id": projection.message_id,
        "completion_identity": _completion_mapping(projection.completion_identity),
        "outcome": None if projection.outcome is None else projection.outcome.value,
        "body_digest": projection.body_digest,
    }


def _delivery_mapping(
    delivery: _workflow.PendingDelivery | None,
) -> dict[str, object] | None:
    if delivery is None:
        return None
    return {
        "delivery_id": delivery.delivery_id,
        "consumer_generation": delivery.consumer_generation,
        "ordered_message_ids": delivery.ordered_message_ids,
        "ordered_event_projection": tuple(
            _event_projection_mapping(item)
            for item in delivery.ordered_event_projection
        ),
        "delivery_digest": delivery.delivery_digest,
        "ack_operation_id": delivery.ack_operation_id,
        "ack_status": delivery.ack_status.value,
    }


def _request_identity_mapping(
    request: EffectRequestIdentity,
) -> dict[str, object]:
    return {
        "command_digest": request.command.command_digest,
        "request_digest": request.request_digest,
        "action": request.command.action.value,
        "root": _root_mapping(request.root),
        "run": _run_mapping(request.run),
        "assignment": _assignment_mapping(request.assignment),
        "pending_delivery": _delivery_mapping(request.pending_delivery),
        "expected_workflow_sequence": request.expected_workflow_sequence,
        "expected_task_sequence": request.expected_task_sequence,
    }


def _validate_effect_request_identity(request: object) -> None:
    if type(request) is not EffectRequestIdentity:
        raise TypeError("effect request identity type is invalid")
    _validate_effect_command(request.command)
    if type(request.root) is not _workflow.RootIdentity:
        raise TypeError("effect request root identity is invalid")
    try:
        request.root.__post_init__()
        _workflow._require_int(
            request.expected_workflow_sequence,
            "expected_workflow_sequence",
        )
        if request.expected_task_sequence is not None:
            _workflow._require_int(
                request.expected_task_sequence,
                "expected_task_sequence",
            )
        _workflow._require_digest(request.request_digest, "request_digest")
        _workflow._require_digest(request.binding_digest, "binding_digest")
    except (TypeError, ValueError) as exc:
        raise ValueError("effect request identity is invalid") from exc
    if request.request_digest != request.command.parameter_digest:
        raise _workflow.OperationIdentityConflict(
            "effect request digest differs from its command"
        )
    run = request.run
    assignment = request.assignment
    delivery = request.pending_delivery
    if run is not None and type(run) is not _workflow.RunIdentity:
        raise TypeError("effect request run identity is invalid")
    if assignment is not None and type(assignment) is not _workflow.ActiveAssignment:
        raise TypeError("effect request assignment identity is invalid")
    if delivery is not None and type(delivery) is not _workflow.PendingDelivery:
        raise TypeError("effect request Delivery identity is invalid")
    if run is not None:
        run.__post_init__()
    if assignment is not None:
        assignment.__post_init__()
    if delivery is not None:
        delivery.__post_init__()
    if assignment is not None and (
        run is None or assignment.completion_identity.run_id != run.run_id
    ):
        raise _workflow.OperationIdentityConflict(
            "effect request assignment run identity differs"
        )
    if delivery is not None:
        if run is None or delivery.consumer_generation != run.consumer_generation:
            raise _workflow.OperationIdentityConflict(
                "effect request Delivery generation differs"
            )
        if assignment is None or any(
            projection.completion_identity != assignment.completion_identity
            for projection in delivery.ordered_event_projection
        ):
            raise _workflow.OperationIdentityConflict(
                "effect request Delivery assignment differs"
            )
    action = request.command.action
    if action is _workflow.OperationAction.START:
        if (
            request.command.team_id != request.root.team_id
            or request.expected_workflow_sequence != 0
            or request.expected_task_sequence is not None
            or run is not None
            or assignment is not None
            or delivery is not None
        ):
            raise _workflow.OperationIdentityConflict(
                "effect start request context differs"
            )
    elif action is _workflow.OperationAction.PROMPT:
        if run is None or assignment is not None or delivery is not None:
            raise _workflow.OperationIdentityConflict(
                "effect prompt request context differs"
            )
    elif action is _workflow.OperationAction.WAIT:
        if run is None or assignment is None or delivery is not None:
            raise _workflow.OperationIdentityConflict(
                "effect wait request context differs"
            )
    elif action is _workflow.OperationAction.REPLY:
        if (
            run is None
            or assignment is None
            or delivery is None
            or request.command.message_id not in delivery.ordered_message_ids
        ):
            raise _workflow.OperationIdentityConflict(
                "effect reply request context differs"
            )
    elif action in {
        _workflow.OperationAction.READ,
        _workflow.OperationAction.RELEASE,
    }:
        if run is None or assignment is None or delivery is None:
            raise _workflow.OperationIdentityConflict(
                "effect terminal request context differs"
            )
    elif action is _workflow.OperationAction.ACK:
        if (
            run is None
            or assignment is None
            or delivery is None
            or request.command.delivery_id != delivery.delivery_id
        ):
            raise _workflow.OperationIdentityConflict(
                "effect acknowledgement request context differs"
            )
    elif action is _workflow.OperationAction.STOP:
        if run is None or assignment is not None or delivery is not None:
            raise _workflow.OperationIdentityConflict(
                "effect stop request context differs"
            )
    else:
        raise ValueError("effect request action is unsupported")
    if (
        request.command.role is not None
        and assignment is not None
        and request.command.role.value != assignment.role.value
    ):
        raise _workflow.OperationIdentityConflict(
            "effect request role differs from its assignment"
        )
    if request.binding_digest != _domain_digest(
        _REQUEST_IDENTITY_DOMAIN,
        _request_identity_mapping(request),
    ):
        raise _workflow.OperationIdentityConflict(
            "effect request identity digest differs"
        )


def derive_effect_request_identity(
    command: EffectCommand,
    *,
    root: _workflow.RootIdentity,
    run: _workflow.RunIdentity | None,
    assignment: _workflow.ActiveAssignment | None,
    pending_delivery: _workflow.PendingDelivery | None,
    expected_workflow_sequence: int,
    expected_task_sequence: int | None,
) -> EffectRequestIdentity:
    _validate_effect_command(command)
    provisional = object.__new__(EffectRequestIdentity)
    values: dict[str, object] = {
        "command": command,
        "root": root,
        "run": run,
        "assignment": assignment,
        "pending_delivery": pending_delivery,
        "expected_workflow_sequence": expected_workflow_sequence,
        "expected_task_sequence": expected_task_sequence,
        "request_digest": command.parameter_digest,
        "binding_digest": "sha256:" + "0" * 64,
    }
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(
        provisional,
        "binding_digest",
        _domain_digest(
            _REQUEST_IDENTITY_DOMAIN,
            _request_identity_mapping(provisional),
        ),
    )
    provisional.__post_init__()
    return provisional


def _authority_mapping(authority: WorkflowEffectAuthority) -> dict[str, object]:
    return {
        "request_binding_digest": authority.request_binding_digest,
        "backend_id": authority.backend_id,
        "provider_id": authority.provider_id,
        "owner": authority.owner,
        "lease_epoch": authority.lease_epoch,
        "fencing_token": authority.fencing_token,
        "expires_ns": authority.expires_ns,
        "authority_ref": authority.authority_ref,
        "proof_ref": authority.proof_ref,
    }


def _authority_binding_mapping(
    authority: WorkflowEffectAuthority,
) -> dict[str, object]:
    values = _authority_mapping(authority)
    values.pop("expires_ns")
    return values


def validate_authority(
    value: object,
    *,
    request: EffectRequestIdentity | None = None,
) -> None:
    if type(value) is not WorkflowEffectAuthority:
        raise _workflow.OperationIdentityConflict(
            "workflow effect authority type is invalid"
        )
    authority = value
    try:
        issuer = object.__getattribute__(authority, "_issuer")
        _workflow._require_digest(
            authority.request_binding_digest,
            "authority request_binding_digest",
        )
        for field_name in (
            "backend_id",
            "provider_id",
            "owner",
            "authority_ref",
            "proof_ref",
        ):
            _workflow._require_identifier(
                getattr(authority, field_name),
                f"authority {field_name}",
            )
        _workflow._require_int(authority.lease_epoch, "authority lease_epoch")
        _workflow._require_int(authority.fencing_token, "authority fencing_token")
        _workflow._require_int(authority.expires_ns, "authority expires_ns")
        _workflow._require_digest(
            authority.authority_digest,
            "authority_digest",
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise _workflow.OperationIdentityConflict(
            "workflow effect authority is invalid"
        ) from exc
    if issuer is not _AUTHORITY_ISSUER:
        raise _workflow.OperationIdentityConflict(
            "workflow effect authority issuer is invalid"
        )
    if authority.authority_digest != _domain_digest(
        _AUTHORITY_DOMAIN,
        _authority_mapping(authority),
    ):
        raise _workflow.OperationIdentityConflict(
            "workflow effect authority digest differs"
        )
    if request is not None:
        _validate_effect_request_identity(request)
        if authority.request_binding_digest != request.binding_digest:
            raise _workflow.OperationIdentityConflict(
                "workflow effect authority request differs"
            )


def require_live_authority(
    authority: WorkflowEffectAuthority,
    *,
    request: EffectRequestIdentity,
    now_ns: int,
) -> None:
    """Validate one captured authority snapshot against a trusted clock."""

    validate_authority(authority, request=request)
    try:
        now = _workflow._require_int(now_ns, "authority clock")
    except (TypeError, ValueError) as exc:
        raise _workflow.OperationIdentityConflict(
            "workflow effect authority clock is invalid"
        ) from exc
    if now >= authority.expires_ns:
        raise _workflow.OperationIdentityConflict(
            "workflow effect authority is expired"
        )


def _issue_workflow_effect_authority(
    request: EffectRequestIdentity,
    *,
    backend_id: str,
    provider_id: str,
    owner: str,
    lease_epoch: int,
    fencing_token: int,
    expires_ns: int,
    authority_ref: str,
    proof_ref: str,
) -> WorkflowEffectAuthority:
    _validate_effect_request_identity(request)
    authority = object.__new__(WorkflowEffectAuthority)
    values: dict[str, object] = {
        "request_binding_digest": request.binding_digest,
        "backend_id": backend_id,
        "provider_id": provider_id,
        "owner": owner,
        "lease_epoch": lease_epoch,
        "fencing_token": fencing_token,
        "expires_ns": expires_ns,
        "authority_ref": authority_ref,
        "proof_ref": proof_ref,
        "authority_digest": "sha256:" + "0" * 64,
        "_issuer": _AUTHORITY_ISSUER,
    }
    for name, value in values.items():
        object.__setattr__(authority, name, value)
    object.__setattr__(
        authority,
        "authority_digest",
        _domain_digest(_AUTHORITY_DOMAIN, _authority_mapping(authority)),
    )
    validate_authority(authority, request=request)
    return authority


def _derived_effect_identity_values(
    request: EffectRequestIdentity,
    authority: WorkflowEffectAuthority,
) -> dict[str, str]:
    _validate_effect_request_identity(request)
    validate_authority(authority, request=request)
    authority_binding_digest = _domain_digest(
        _AUTHORITY_BINDING_DOMAIN,
        _authority_binding_mapping(authority),
    )
    mapping = {
        "request_binding_digest": request.binding_digest,
        "authority_binding_digest": authority_binding_digest,
    }
    operation_hex = hashlib.sha256(
        _OPERATION_ID_DOMAIN + _canonical_json(mapping)
    ).hexdigest()
    effect_hex = hashlib.sha256(
        _EFFECT_KEY_DOMAIN + _canonical_json(mapping)
    ).hexdigest()
    return {
        "operation_id": f"workflow-op-{operation_hex}",
        "effect_key": f"workflow-effect/{effect_hex}",
        "request_digest": request.request_digest,
        "evidence_ref": _domain_digest(_EVIDENCE_DOMAIN, mapping),
        "request_binding_digest": request.binding_digest,
        "authority_binding_digest": authority_binding_digest,
    }


def _effect_identity_values(identity: EffectIdentity) -> dict[str, str]:
    return {
        "operation_id": identity.operation_id,
        "effect_key": identity.effect_key,
        "request_digest": identity.request_digest,
        "evidence_ref": identity.evidence_ref,
        "request_binding_digest": identity.request_binding_digest,
        "authority_binding_digest": identity.authority_binding_digest,
    }


def validate_effect_identity(
    value: object,
    *,
    request: EffectRequestIdentity,
    authority: WorkflowEffectAuthority,
) -> None:
    if type(value) is not EffectIdentity:
        raise _workflow.OperationIdentityConflict("effect identity type is invalid")
    identity = value
    try:
        issuer = object.__getattribute__(identity, "_issuer")
        _workflow._require_identifier(identity.operation_id, "operation_id")
        _workflow._require_identifier(identity.effect_key, "effect_key")
        for field_name in (
            "request_digest",
            "evidence_ref",
            "request_binding_digest",
            "authority_binding_digest",
        ):
            _workflow._require_digest(
                getattr(identity, field_name),
                field_name,
            )
    except (AttributeError, TypeError, ValueError) as exc:
        raise _workflow.OperationIdentityConflict("effect identity is invalid") from exc
    if issuer is not _IDENTITY_ISSUER:
        raise _workflow.OperationIdentityConflict("effect identity issuer is invalid")
    if _effect_identity_values(identity) != _derived_effect_identity_values(
        request,
        authority,
    ):
        raise _workflow.OperationIdentityConflict(
            "effect identity canonical derivation differs"
        )


def _same_effect_identity(left: object, right: object) -> bool:
    if type(left) is not EffectIdentity or type(right) is not EffectIdentity:
        return False
    try:
        return _effect_identity_values(left) == _effect_identity_values(right)
    except (AttributeError, TypeError, ValueError):
        return False


def derive_effect_identity(
    request: EffectRequestIdentity,
    authority: WorkflowEffectAuthority,
) -> EffectIdentity:
    values = _derived_effect_identity_values(request, authority)
    identity = object.__new__(EffectIdentity)
    for name, field_value in values.items():
        object.__setattr__(identity, name, field_value)
    object.__setattr__(identity, "_issuer", _IDENTITY_ISSUER)
    validate_effect_identity(identity, request=request, authority=authority)
    return identity


def _stop_stage_mapping(stage: CompositeStopStage) -> dict[str, object]:
    return {
        "stage_id": stage.stage_id,
        "resource_ref": stage.resource_ref,
        "effect_ref": stage.effect_ref,
        "status": stage.status,
        "evidence_digest": stage.evidence_digest,
    }


def _validate_composite_stop(value: object) -> None:
    if type(value) is not CompositeStopObservation:
        raise TypeError("composite stop observation type is invalid")
    if (
        type(value.stages) is not tuple
        or not value.stages
        or len(value.stages) > _workflow.MAX_COLLECTION_ITEMS
        or any(type(stage) is not CompositeStopStage for stage in value.stages)
    ):
        raise ValueError("composite stop stages are invalid")
    for stage in value.stages:
        stage.__post_init__()
    if len({stage.stage_id for stage in value.stages}) != len(value.stages):
        raise ValueError("composite stop stage identity is duplicated")
    if any(stage.status != "COMPLETED" for stage in value.stages):
        raise ValueError("composite stop is not fully completed")
    try:
        _workflow._require_identifier(value.composite_ref, "composite_ref")
        _workflow._require_digest(value.composite_digest, "composite_digest")
    except (TypeError, ValueError) as exc:
        raise ValueError("composite stop identity is invalid") from exc
    expected = _domain_digest(
        _STOP_RESULT_DOMAIN,
        {
            "composite_ref": value.composite_ref,
            "stages": tuple(_stop_stage_mapping(stage) for stage in value.stages),
        },
    )
    if value.composite_digest != expected:
        raise ValueError("composite stop digest differs")


def make_composite_stop_observation(
    stages: tuple[CompositeStopStage, ...],
    *,
    composite_ref: str,
) -> CompositeStopObservation:
    if type(stages) is not tuple:
        raise TypeError("composite stop stages must be a tuple")
    value = CompositeStopObservation(
        stages=stages,
        composite_ref=composite_ref,
        composite_digest=_domain_digest(
            _STOP_RESULT_DOMAIN,
            {
                "composite_ref": composite_ref,
                "stages": tuple(_stop_stage_mapping(stage) for stage in stages),
            },
        ),
    )
    return value


def _public_completion_matches(
    public: object,
    private: _workflow.CompletionIdentity,
) -> bool:
    if type(public) is not CompletionIdentity:
        return False
    try:
        return (
            _opaque_ref(public.run_id, RunRef, "result run_id") == private.run_id
            and _opaque_ref(public.task_id, TaskRef, "result task_id")
            == private.task_id
            and _opaque_ref(public.dispatch_id, DispatchRef, "result dispatch_id")
            == private.dispatch_id
            and _opaque_ref(
                public.sender_terminal_id,
                TerminalRef,
                "result sender_terminal_id",
            )
            == private.sender_terminal_id
        )
    except (TypeError, ValueError):
        return False


def _public_assignment_matches(
    public: object,
    private: _workflow.ActiveAssignment,
) -> bool:
    if type(public) is not Assignment:
        return False
    try:
        return (
            type(public.role) is Role
            and public.role.value == private.role.value
            and type(public.launch_mode) is PublicLaunchMode
            and public.launch_mode.value == private.launch_mode.value
            and _opaque_ref(public.task_id, TaskRef, "result task_id")
            == private.task_id
            and _opaque_ref(public.dispatch_id, DispatchRef, "result dispatch_id")
            == private.dispatch_id
            and _opaque_ref(public.terminal_id, TerminalRef, "result terminal_id")
            == private.terminal_id
            and _public_completion_matches(
                public.completion_identity,
                private.completion_identity,
            )
        )
    except (TypeError, ValueError):
        return False


def _public_event_matches(
    public: object,
    projection: _workflow.EventProjection,
    delivery_id: str,
) -> bool:
    if type(public) is not NormalizedEvent:
        return False
    kind = {
        _workflow.EventProjectionKind.QUESTION: EventKind.QUESTION,
        _workflow.EventProjectionKind.WORKER_DONE: EventKind.WORKER_DONE,
        _workflow.EventProjectionKind.ESCALATION: EventKind.ESCALATION,
    }[projection.kind]
    outcome = (
        None
        if projection.outcome is None
        else {
            _workflow.EventOutcome.SUCCEEDED: Outcome.SUCCEEDED,
            _workflow.EventOutcome.FAILED: Outcome.FAILED,
        }[projection.outcome]
    )
    try:
        body = _safe_text(public.body, "event body", allow_empty=True).encode("utf-8")
        message_id = (
            None
            if public.message_id is None
            else _opaque_ref(public.message_id, MessageRef, "event message_id")
        )
        return (
            type(public.kind) is EventKind
            and public.kind is kind
            and _opaque_ref(public.delivery_id, DeliveryRef, "event delivery_id")
            == delivery_id
            and message_id == projection.message_id
            and public.identity is not None
            and _public_completion_matches(
                public.identity,
                projection.completion_identity,
            )
            and public.outcome is outcome
            and _workflow.digest_bounded_body(body) == projection.body_digest
        )
    except (TypeError, ValueError):
        return False


def _wait_result_matches(
    public: object,
    delivery: _workflow.PendingDelivery | None,
) -> bool:
    if type(public) is not WaitReceipt:
        return False
    if delivery is None:
        return public.delivery_id is None and public.events == ()
    try:
        return (
            public.delivery_id is not None
            and _opaque_ref(
                public.delivery_id,
                DeliveryRef,
                "wait delivery_id",
            )
            == delivery.delivery_id
            and type(public.events) is tuple
            and len(public.events) == len(delivery.ordered_event_projection)
            and all(
                _public_event_matches(event, projection, delivery.delivery_id)
                for event, projection in zip(
                    public.events,
                    delivery.ordered_event_projection,
                    strict=True,
                )
            )
        )
    except (TypeError, ValueError):
        return False


def _release_result_digest(
    state: str,
    assignment: _workflow.ActiveAssignment,
    delivery: _workflow.PendingDelivery,
) -> str:
    return _domain_digest(
        _RELEASE_RESULT_DOMAIN,
        {
            "state": state,
            "assignment_digest": _workflow.assignment_digest(assignment),
            "delivery_digest": delivery.delivery_digest,
        },
    )


def _observation_result(
    *,
    request: EffectRequestIdentity,
    run: _workflow.RunIdentity,
    assignment: _workflow.ActiveAssignment | None,
    delivery: _workflow.PendingDelivery | None,
    public_result: object,
    composite_stop: CompositeStopObservation | None,
) -> tuple[str, str]:
    action = request.command.action
    if action is _workflow.OperationAction.START:
        if (
            type(public_result) is not StartResult
            or public_result.team_id != request.root.team_id
            or _opaque_ref(public_result.run_id, RunRef, "start run_id") != run.run_id
            or _opaque_ref(
                public_result.main_terminal_id,
                TerminalRef,
                "start main_terminal_id",
            )
            != run.main_terminal_id
            or not isinstance(public_result.state_path, Path)
            or str(public_result.state_path) != request.root.state_root_path
            or assignment is not None
            or delivery is not None
            or composite_stop is not None
        ):
            raise _workflow.OperationIdentityConflict("start result projection differs")
        return "started", _domain_digest(
            _START_RESULT_DOMAIN,
            {
                "team_id": request.root.team_id,
                "run": _run_mapping(run),
                "state_root": _root_mapping(request.root)["state_root"],
            },
        )
    if action is _workflow.OperationAction.PROMPT:
        if (
            assignment is None
            or delivery is not None
            or composite_stop is not None
            or not _public_assignment_matches(public_result, assignment)
            or request.command.role is None
            or request.command.role.value != assignment.role.value
        ):
            raise _workflow.OperationIdentityConflict(
                "prompt result projection differs"
            )
        return "assignment", _workflow.assignment_digest(assignment)
    if action is _workflow.OperationAction.WAIT:
        if composite_stop is not None or not _wait_result_matches(
            public_result,
            delivery,
        ):
            raise _workflow.OperationIdentityConflict("wait result projection differs")
        if delivery is None:
            return "timeout", _workflow.wait_timeout_digest()
        return "delivery", delivery.delivery_digest
    if action is _workflow.OperationAction.REPLY:
        if (
            assignment is None
            or delivery is None
            or type(public_result) is not ReplyReceipt
            or type(public_result.replied) is not bool
            or not public_result.replied
            or composite_stop is not None
        ):
            raise _workflow.OperationIdentityConflict("reply result projection differs")
        return "reply", _domain_digest(
            _REPLY_RESULT_DOMAIN,
            {
                "delivery_id": delivery.delivery_id,
                "message_id": request.command.message_id,
                "consumer_generation": delivery.consumer_generation,
                "replied": True,
            },
        )
    if action is _workflow.OperationAction.READ:
        if (
            assignment is None
            or delivery is None
            or type(public_result) is not ReadReceipt
            or type(public_result.output) is not str
            or composite_stop is not None
        ):
            raise _workflow.OperationIdentityConflict("read result projection differs")
        output = _safe_text(
            public_result.output,
            "read output",
            allow_empty=True,
        ).encode("utf-8")
        return "read_output", _body_digest(_READ_RESULT_DOMAIN, output)
    if action is _workflow.OperationAction.RELEASE:
        if (
            assignment is None
            or delivery is None
            or type(public_result) is not ReleaseReceipt
            or type(public_result.state) is not str
            or public_result.state not in {"retained", "released", "already_released"}
            or composite_stop is not None
        ):
            raise _workflow.OperationIdentityConflict(
                "release result projection differs"
            )
        return "release", _release_result_digest(
            public_result.state,
            assignment,
            delivery,
        )
    if action is _workflow.OperationAction.ACK:
        if (
            assignment is None
            or delivery is None
            or type(public_result) is not AckReceipt
            or type(public_result.acknowledged) is not bool
            or not public_result.acknowledged
            or composite_stop is not None
        ):
            raise _workflow.OperationIdentityConflict(
                "acknowledgement result projection differs"
            )
        return "ack", _domain_digest(
            _ACK_RESULT_DOMAIN,
            {
                "delivery_id": delivery.delivery_id,
                "consumer_generation": delivery.consumer_generation,
                "acknowledged": True,
            },
        )
    if action is _workflow.OperationAction.STOP:
        if (
            assignment is not None
            or delivery is not None
            or type(public_result) is not StopResult
            or public_result.team_id != request.root.team_id
            or _opaque_ref(public_result.run_id, RunRef, "stop run_id") != run.run_id
            or type(composite_stop) is not CompositeStopObservation
        ):
            raise _workflow.OperationIdentityConflict("stop result projection differs")
        _validate_composite_stop(composite_stop)
        return "stopped_composite", composite_stop.composite_digest
    raise ValueError("observation action is unsupported")


def _observation_mapping(
    observation: BackendEffectObservation,
) -> dict[str, object]:
    return {
        "operation_id": observation.operation_id,
        "effect_key": observation.effect_key,
        "action": observation.action.value,
        "request_digest": observation.request_digest,
        "root_key": observation.root_key,
        "backend_id": observation.backend_id,
        "provider_id": observation.provider_id,
        "run_id": observation.run_id,
        "main_terminal_id": observation.main_terminal_id,
        "assignment": _assignment_mapping(observation.assignment),
        "delivery": _delivery_mapping(observation.delivery),
        "message_id": observation.message_id,
        "consumer_generation": observation.consumer_generation,
        "owner": observation.owner,
        "lease_epoch": observation.lease_epoch,
        "fencing_token": observation.fencing_token,
        "effect_ref": observation.effect_ref,
        "provider_proof_ref": observation.provider_proof_ref,
        "result_kind": observation.result_kind,
        "result_digest": observation.result_digest,
        "evidence_ref": observation.evidence_ref,
        "composite_stop_digest": (
            None
            if observation.composite_stop is None
            else observation.composite_stop.composite_digest
        ),
    }


def _expected_observation_evidence(
    *,
    identity: EffectIdentity,
    authority: WorkflowEffectAuthority,
    effect_ref: str,
    provider_proof_ref: str,
    result_digest: str,
) -> str:
    return _domain_digest(
        _OBSERVATION_EVIDENCE_DOMAIN,
        {
            "operation_id": identity.operation_id,
            "effect_key": identity.effect_key,
            "authority_binding_digest": identity.authority_binding_digest,
            "backend_id": authority.backend_id,
            "provider_id": authority.provider_id,
            "effect_ref": effect_ref,
            "provider_proof_ref": provider_proof_ref,
            "result_digest": result_digest,
        },
    )


def validate_observation(
    value: object,
    *,
    request: EffectRequestIdentity,
    identity: EffectIdentity,
    authority: WorkflowEffectAuthority,
) -> None:
    _validate_effect_request_identity(request)
    validate_effect_identity(identity, request=request, authority=authority)
    if type(value) is not BackendEffectObservation:
        raise _workflow.OperationIdentityConflict(
            "backend effect observation type is invalid"
        )
    observation = value
    try:
        issuer = object.__getattribute__(observation, "_issuer")
        for field_value, name in (
            (observation.operation_id, "observation operation_id"),
            (observation.effect_key, "observation effect_key"),
            (observation.root_key, "observation root_key"),
            (observation.backend_id, "observation backend_id"),
            (observation.provider_id, "observation provider_id"),
            (observation.run_id, "observation run_id"),
            (observation.main_terminal_id, "observation main_terminal_id"),
            (observation.owner, "observation owner"),
            (observation.effect_ref, "observation effect_ref"),
            (observation.provider_proof_ref, "observation provider_proof_ref"),
            (observation.result_kind, "observation result_kind"),
        ):
            _workflow._require_identifier(field_value, name)
        if observation.message_id is not None:
            _workflow._require_identifier(
                observation.message_id,
                "observation message_id",
            )
        _workflow._require_digest(
            observation.request_digest,
            "observation request_digest",
        )
        _workflow._require_int(
            observation.consumer_generation,
            "observation consumer_generation",
        )
        _workflow._require_int(observation.lease_epoch, "observation lease_epoch")
        _workflow._require_int(
            observation.fencing_token,
            "observation fencing_token",
        )
        _workflow._require_digest(
            observation.result_digest,
            "observation result_digest",
        )
        _workflow._require_digest(
            observation.evidence_ref,
            "observation evidence_ref",
        )
        _workflow._require_digest(
            observation.observation_digest,
            "observation_digest",
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise _workflow.OperationIdentityConflict(
            "backend effect observation is invalid"
        ) from exc
    if issuer is not _OBSERVATION_ISSUER:
        raise _workflow.OperationIdentityConflict(
            "backend effect observation issuer is invalid"
        )
    if type(observation.action) is not _workflow.OperationAction:
        raise _workflow.OperationIdentityConflict(
            "backend effect observation action is invalid"
        )
    if observation.assignment is not None:
        if type(observation.assignment) is not _workflow.ActiveAssignment:
            raise _workflow.OperationIdentityConflict(
                "backend effect assignment is invalid"
            )
        observation.assignment.__post_init__()
    if observation.delivery is not None:
        if type(observation.delivery) is not _workflow.PendingDelivery:
            raise _workflow.OperationIdentityConflict(
                "backend effect Delivery is invalid"
            )
        observation.delivery.__post_init__()
    run = _workflow.RunIdentity(
        observation.run_id,
        observation.main_terminal_id,
        observation.consumer_generation,
    )
    common_expected = (
        identity.operation_id,
        identity.effect_key,
        request.command.action,
        request.request_digest,
        request.root.root_key,
        authority.backend_id,
        authority.provider_id,
        authority.owner,
        authority.lease_epoch,
        authority.fencing_token,
    )
    common_actual = (
        observation.operation_id,
        observation.effect_key,
        observation.action,
        observation.request_digest,
        observation.root_key,
        observation.backend_id,
        observation.provider_id,
        observation.owner,
        observation.lease_epoch,
        observation.fencing_token,
    )
    if common_actual != common_expected:
        raise _workflow.OperationIdentityConflict(
            "backend effect observation identity differs"
        )
    action = request.command.action
    if action is _workflow.OperationAction.START:
        if request.run is not None:
            raise _workflow.OperationIdentityConflict(
                "start request unexpectedly has a run"
            )
    elif run != request.run:
        raise _workflow.OperationIdentityConflict("backend effect run identity differs")
    if action is _workflow.OperationAction.PROMPT:
        if observation.assignment is None:
            raise _workflow.OperationIdentityConflict(
                "prompt observation lacks an assignment"
            )
    elif (
        action
        in {
            _workflow.OperationAction.WAIT,
            _workflow.OperationAction.REPLY,
            _workflow.OperationAction.READ,
            _workflow.OperationAction.RELEASE,
            _workflow.OperationAction.ACK,
        }
        and observation.assignment != request.assignment
    ):
        raise _workflow.OperationIdentityConflict(
            "backend effect assignment identity differs"
        )
    elif (
        action
        in {
            _workflow.OperationAction.START,
            _workflow.OperationAction.STOP,
        }
        and observation.assignment is not None
    ):
        raise _workflow.OperationIdentityConflict(
            "backend effect assignment is unexpected"
        )
    if action is _workflow.OperationAction.WAIT:
        if observation.message_id is not None:
            raise _workflow.OperationIdentityConflict(
                "wait observation cannot select one message"
            )
    elif (
        action
        in {
            _workflow.OperationAction.REPLY,
            _workflow.OperationAction.READ,
            _workflow.OperationAction.RELEASE,
            _workflow.OperationAction.ACK,
        }
        and observation.delivery != request.pending_delivery
    ):
        raise _workflow.OperationIdentityConflict(
            "backend effect Delivery identity differs"
        )
    elif (
        action
        in {
            _workflow.OperationAction.START,
            _workflow.OperationAction.PROMPT,
            _workflow.OperationAction.STOP,
        }
        and observation.delivery is not None
    ):
        raise _workflow.OperationIdentityConflict(
            "backend effect Delivery is unexpected"
        )
    expected_message = (
        request.command.message_id
        if action is _workflow.OperationAction.REPLY
        else None
    )
    if observation.message_id != expected_message:
        raise _workflow.OperationIdentityConflict(
            "backend effect message identity differs"
        )
    expected_kind, expected_digest = _observation_result(
        request=request,
        run=run,
        assignment=observation.assignment,
        delivery=observation.delivery,
        public_result=observation.public_result,
        composite_stop=observation.composite_stop,
    )
    if (
        observation.result_kind != expected_kind
        or observation.result_digest != expected_digest
    ):
        raise _workflow.OperationIdentityConflict(
            "backend effect result identity differs"
        )
    expected_evidence = _expected_observation_evidence(
        identity=identity,
        authority=authority,
        effect_ref=observation.effect_ref,
        provider_proof_ref=observation.provider_proof_ref,
        result_digest=observation.result_digest,
    )
    if observation.evidence_ref != expected_evidence:
        raise _workflow.OperationIdentityConflict(
            "backend effect evidence identity differs"
        )
    if observation.observation_digest != _domain_digest(
        _OBSERVATION_DOMAIN,
        _observation_mapping(observation),
    ):
        raise _workflow.OperationIdentityConflict(
            "backend effect observation digest differs"
        )


def _issue_backend_effect_observation(
    request: EffectRequestIdentity,
    identity: EffectIdentity,
    authority: WorkflowEffectAuthority,
    *,
    run: _workflow.RunIdentity,
    assignment: _workflow.ActiveAssignment | None,
    delivery: _workflow.PendingDelivery | None,
    public_result: EffectPublicResult,
    effect_ref: str,
    provider_proof_ref: str,
    composite_stop: CompositeStopObservation | None = None,
) -> BackendEffectObservation:
    _validate_effect_request_identity(request)
    if type(run) is not _workflow.RunIdentity:
        raise TypeError("backend observation run identity is invalid")
    run.__post_init__()
    validate_authority(authority, request=request)
    validate_effect_identity(identity, request=request, authority=authority)
    result_kind, result_digest = _observation_result(
        request=request,
        run=run,
        assignment=assignment,
        delivery=delivery,
        public_result=public_result,
        composite_stop=composite_stop,
    )
    observation = object.__new__(BackendEffectObservation)
    values: dict[str, object] = {
        "operation_id": identity.operation_id,
        "effect_key": identity.effect_key,
        "action": request.command.action,
        "request_digest": request.request_digest,
        "root_key": request.root.root_key,
        "backend_id": authority.backend_id,
        "provider_id": authority.provider_id,
        "run_id": run.run_id,
        "main_terminal_id": run.main_terminal_id,
        "assignment": assignment,
        "delivery": delivery,
        "message_id": (
            request.command.message_id
            if request.command.action is _workflow.OperationAction.REPLY
            else None
        ),
        "consumer_generation": run.consumer_generation,
        "owner": authority.owner,
        "lease_epoch": authority.lease_epoch,
        "fencing_token": authority.fencing_token,
        "effect_ref": effect_ref,
        "provider_proof_ref": provider_proof_ref,
        "result_kind": result_kind,
        "result_digest": result_digest,
        "evidence_ref": _expected_observation_evidence(
            identity=identity,
            authority=authority,
            effect_ref=effect_ref,
            provider_proof_ref=provider_proof_ref,
            result_digest=result_digest,
        ),
        "public_result": public_result,
        "composite_stop": composite_stop,
        "observation_digest": "sha256:" + "0" * 64,
        "_issuer": _OBSERVATION_ISSUER,
    }
    for name, field_value in values.items():
        object.__setattr__(observation, name, field_value)
    object.__setattr__(
        observation,
        "observation_digest",
        _domain_digest(_OBSERVATION_DOMAIN, _observation_mapping(observation)),
    )
    validate_observation(
        observation,
        request=request,
        identity=identity,
        authority=authority,
    )
    return observation


def _is_effect_public_result(value: object) -> bool:
    return type(value) in {
        StartResult,
        Assignment,
        WaitReceipt,
        ReplyReceipt,
        ReadReceipt,
        ReleaseReceipt,
        AckReceipt,
        StopResult,
    }


def _same_public_result(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _issue_backend_effect_request(
    *,
    command: EffectCommand,
    request: EffectRequestIdentity,
    identity: EffectIdentity,
    authority: WorkflowEffectAuthority,
    operation: _workflow.OperationHandle,
    payload: EffectPayload,
) -> BackendEffectRequest:
    _validate_effect_command(command)
    _validate_effect_request_identity(request)
    validate_effect_identity(identity, request=request, authority=authority)
    if type(operation) is not _workflow.OperationHandle:
        raise TypeError("backend effect operation handle is invalid")
    effect = object.__new__(BackendEffectRequest)
    for name, value in (
        ("command", command),
        ("request", request),
        ("identity", identity),
        ("authority", authority),
        ("operation", operation),
        ("payload", payload),
    ):
        object.__setattr__(effect, name, value)
    return effect


def _public_assignment(assignment: _workflow.ActiveAssignment) -> Assignment:
    return Assignment(
        role=Role(assignment.role.value),
        launch_mode=PublicLaunchMode(assignment.launch_mode.value),
        task_id=TaskRef(assignment.task_id),
        dispatch_id=DispatchRef(assignment.dispatch_id),
        terminal_id=TerminalRef(assignment.terminal_id),
        completion_identity=CompletionIdentity(
            run_id=RunRef(assignment.completion_identity.run_id),
            task_id=TaskRef(assignment.completion_identity.task_id),
            dispatch_id=DispatchRef(assignment.completion_identity.dispatch_id),
            sender_terminal_id=TerminalRef(
                assignment.completion_identity.sender_terminal_id
            ),
        ),
    )


def _operation_intent(
    request: EffectRequestIdentity,
    identity: EffectIdentity,
    authority: WorkflowEffectAuthority,
) -> _workflow.OperationIntent:
    validate_effect_identity(identity, request=request, authority=authority)
    assignment = request.assignment
    delivery = request.pending_delivery
    action = request.command.action
    return _workflow.OperationIntent(
        operation_id=identity.operation_id,
        effect_key=identity.effect_key,
        root_key=request.root.root_key,
        root=request.root if action is _workflow.OperationAction.START else None,
        action=action,
        request_digest=identity.request_digest,
        expected_workflow_sequence=request.expected_workflow_sequence,
        expected_task_sequence=request.expected_task_sequence,
        run_id=None if request.run is None else request.run.run_id,
        main_terminal_id=(
            None if request.run is None else request.run.main_terminal_id
        ),
        task_id=None if assignment is None else assignment.task_id,
        dispatch_id=None if assignment is None else assignment.dispatch_id,
        attempt=None if assignment is None else assignment.attempt,
        terminal_id=None if assignment is None else assignment.terminal_id,
        delivery_id=(
            delivery.delivery_id
            if action
            in {
                _workflow.OperationAction.REPLY,
                _workflow.OperationAction.READ,
                _workflow.OperationAction.RELEASE,
                _workflow.OperationAction.ACK,
            }
            and delivery is not None
            else None
        ),
        message_id=(
            request.command.message_id
            if action is _workflow.OperationAction.REPLY
            else None
        ),
        consumer_generation=(
            0 if request.run is None else request.run.consumer_generation
        ),
        owner=authority.owner,
        lease_epoch=authority.lease_epoch,
        fencing_token=authority.fencing_token,
        actor=authority.owner,
        evidence_ref=identity.evidence_ref,
        next_task_sequence=(
            1
            if action is _workflow.OperationAction.PROMPT
            and request.expected_task_sequence is None
            else None
        ),
    )


def _receipt_id(
    identity: EffectIdentity,
    observation: BackendEffectObservation,
) -> str:
    digest = hashlib.sha256(
        _RECEIPT_ID_DOMAIN
        + _canonical_json(
            {
                "operation_id": identity.operation_id,
                "effect_key": identity.effect_key,
                "effect_ref": observation.effect_ref,
                "result_digest": observation.result_digest,
                "evidence_ref": observation.evidence_ref,
            }
        )
    ).hexdigest()
    return f"workflow-receipt-{digest}"


def _validate_stored_replay_response(
    replay: _workflow.StoredReplay,
    request: EffectRequestIdentity,
    identity: EffectIdentity,
) -> None:
    if type(replay) is not _workflow.StoredReplay:
        raise _workflow.OperationIdentityConflict(
            "workflow stored replay type is invalid"
        )
    replay.__post_init__()
    receipt = replay.receipt
    checkpoint = replay.checkpoint
    if (
        replay.operation_id != identity.operation_id
        or receipt.operation_id != identity.operation_id
        or receipt.effect_key != identity.effect_key
        or receipt.action is not request.command.action
        or receipt.request_digest != identity.request_digest
        or receipt.root_key != request.root.root_key
        or checkpoint.root != request.root
        or checkpoint.run.run_id != receipt.run_id
        or checkpoint.run.main_terminal_id != receipt.main_terminal_id
        or checkpoint.run.consumer_generation != receipt.consumer_generation
    ):
        raise _workflow.OperationIdentityConflict(
            "workflow stored replay identity differs"
        )
    last = checkpoint.last_operation
    if last is None:
        raise _workflow.OperationIdentityConflict(
            "workflow stored replay checkpoint marker is missing"
        )
    if last.operation_id != replay.operation_id and (
        last.status is not _workflow.OperationStatus.COMMITTED
        or checkpoint.workflow_sequence <= request.expected_workflow_sequence + 2
    ):
        raise _workflow.OperationIdentityConflict(
            "workflow stored replay later checkpoint marker differs"
        )
    if last.operation_id == replay.operation_id and (
        last.effect_key != receipt.effect_key
        or last.action is not receipt.action
        or last.request_digest != receipt.request_digest
        or last.status is not _workflow.OperationStatus.COMMITTED
        or last.receipt_id != receipt.receipt_id
        or last.receipt_digest != _workflow.durable_receipt_digest(receipt)
    ):
        raise _workflow.OperationIdentityConflict(
            "workflow stored replay checkpoint marker differs"
        )


def _normalize_payload(
    command: EffectCommand,
    payload: object,
) -> EffectPayload:
    action = command.action
    if action is _workflow.OperationAction.START:
        if type(payload) is not StartSpec or make_start_command(payload) != command:
            raise _workflow.OperationIdentityConflict(
                "start payload differs from its command"
            )
        return payload
    if action is _workflow.OperationAction.PROMPT:
        if type(payload) is not str or command.role is None:
            raise _workflow.OperationIdentityConflict("prompt payload is unavailable")
        if make_request_command(RolePrompt(command.role, payload)) != command:
            raise _workflow.OperationIdentityConflict(
                "prompt payload differs from its command"
            )
        return payload
    if action is _workflow.OperationAction.REPLY:
        if type(payload) is not str or command.message_id is None:
            raise _workflow.OperationIdentityConflict("reply payload is unavailable")
        if (
            make_request_command(MessageReply(MessageRef(command.message_id), payload))
            != command
        ):
            raise _workflow.OperationIdentityConflict(
                "reply payload differs from its command"
            )
        return payload
    if payload is not None:
        raise _workflow.OperationIdentityConflict(
            "effect command has an unexpected payload"
        )
    return None


def _validate_lookup_observation(
    observation: object,
    snapshot: _workflow.WorkflowEffectSnapshot,
) -> BackendEffectObservation:
    if type(snapshot) is not _workflow.WorkflowEffectSnapshot:
        raise TypeError("workflow effect snapshot is invalid")
    snapshot.__post_init__()
    if type(observation) is not BackendEffectObservation:
        raise EffectRecoveryRequired("backend effect lookup is invalid")
    try:
        issuer = object.__getattribute__(observation, "_issuer")
        if issuer is not _OBSERVATION_ISSUER:
            raise ValueError("observation issuer differs")
        if observation.observation_digest != _domain_digest(
            _OBSERVATION_DOMAIN,
            _observation_mapping(observation),
        ):
            raise ValueError("observation digest differs")
    except (AttributeError, TypeError, ValueError) as exc:
        raise EffectRecoveryRequired("backend effect lookup is invalid") from exc
    receipt = snapshot.receipt
    checkpoint = snapshot.checkpoint
    common_actual = (
        observation.operation_id,
        observation.effect_key,
        observation.action,
        observation.request_digest,
        observation.root_key,
        observation.run_id,
        observation.main_terminal_id,
        observation.consumer_generation,
        observation.owner,
        observation.lease_epoch,
        observation.fencing_token,
        observation.effect_ref,
        observation.message_id,
        observation.result_kind,
        observation.result_digest,
        observation.evidence_ref,
    )
    common_expected = (
        receipt.operation_id,
        receipt.effect_key,
        receipt.action,
        receipt.request_digest,
        receipt.root_key,
        receipt.run_id,
        receipt.main_terminal_id,
        receipt.consumer_generation,
        receipt.owner,
        receipt.lease_epoch,
        receipt.fencing_token,
        receipt.effect_ref,
        receipt.message_id,
        receipt.result_kind,
        receipt.result_digest,
        receipt.evidence_ref,
    )
    if common_actual != common_expected:
        raise EffectRecoveryRequired("backend effect lookup identity differs")
    if (
        checkpoint.run.run_id != observation.run_id
        or checkpoint.run.main_terminal_id != observation.main_terminal_id
        or checkpoint.run.consumer_generation != observation.consumer_generation
    ):
        raise EffectRecoveryRequired("backend effect lookup run differs")
    action = receipt.action
    if (
        action
        in {
            _workflow.OperationAction.PROMPT,
            _workflow.OperationAction.WAIT,
            _workflow.OperationAction.REPLY,
            _workflow.OperationAction.READ,
            _workflow.OperationAction.RELEASE,
        }
        and checkpoint.active_assignment != observation.assignment
    ):
        raise EffectRecoveryRequired("backend effect lookup assignment differs")
    if (
        action
        in {
            _workflow.OperationAction.WAIT,
            _workflow.OperationAction.REPLY,
            _workflow.OperationAction.READ,
            _workflow.OperationAction.RELEASE,
        }
        and checkpoint.pending_delivery != observation.delivery
    ):
        raise EffectRecoveryRequired("backend effect lookup Delivery differs")
    if action is _workflow.OperationAction.WAIT:
        if not _wait_result_matches(
            observation.public_result,
            checkpoint.pending_delivery,
        ):
            raise EffectRecoveryRequired("backend WAIT lookup result differs")
        expected = (
            ("timeout", _workflow.wait_timeout_digest())
            if checkpoint.pending_delivery is None
            else ("delivery", checkpoint.pending_delivery.delivery_digest)
        )
    elif action is _workflow.OperationAction.READ:
        if type(observation.public_result) is not ReadReceipt:
            raise EffectRecoveryRequired("backend READ lookup result differs")
        try:
            output = _safe_text(
                observation.public_result.output,
                "read output",
                allow_empty=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise EffectRecoveryRequired("backend READ lookup result differs") from exc
        expected = ("read_output", _body_digest(_READ_RESULT_DOMAIN, output))
    elif action is _workflow.OperationAction.RELEASE:
        if (
            type(observation.public_result) is not ReleaseReceipt
            or checkpoint.active_assignment is None
            or checkpoint.pending_delivery is None
        ):
            raise EffectRecoveryRequired("backend RELEASE lookup result differs")
        expected = (
            "release",
            _release_result_digest(
                observation.public_result.state,
                checkpoint.active_assignment,
                checkpoint.pending_delivery,
            ),
        )
    elif action is _workflow.OperationAction.STOP:
        if (
            type(observation.public_result) is not StopResult
            or observation.public_result.team_id != checkpoint.root.team_id
            or _opaque_ref(
                observation.public_result.run_id,
                RunRef,
                "stop lookup run_id",
            )
            != checkpoint.run.run_id
            or type(observation.composite_stop) is not CompositeStopObservation
        ):
            raise EffectRecoveryRequired("backend STOP lookup result differs")
        try:
            _validate_composite_stop(observation.composite_stop)
        except (TypeError, ValueError) as exc:
            raise EffectRecoveryRequired("backend STOP lookup result differs") from exc
        expected = (
            "stopped_composite",
            observation.composite_stop.composite_digest,
        )
    else:
        expected = (observation.result_kind, observation.result_digest)
    if (observation.result_kind, observation.result_digest) != expected:
        raise EffectRecoveryRequired("backend effect lookup result digest differs")
    return observation


def _issue_durable_delivery_lookup(
    snapshot: _workflow.WorkflowEffectSnapshot,
    result: WaitReceipt,
) -> DurableDeliveryLookup:
    if (
        type(snapshot) is not _workflow.WorkflowEffectSnapshot
        or snapshot.receipt.action is not _workflow.OperationAction.WAIT
        or type(result) is not WaitReceipt
        or not _wait_result_matches(result, snapshot.checkpoint.pending_delivery)
    ):
        raise EffectRecoveryRequired("durable Delivery lookup is invalid")
    expected = (
        ("timeout", _workflow.wait_timeout_digest())
        if snapshot.checkpoint.pending_delivery is None
        else ("delivery", snapshot.checkpoint.pending_delivery.delivery_digest)
    )
    if (snapshot.receipt.result_kind, snapshot.receipt.result_digest) != expected:
        raise EffectRecoveryRequired("durable Delivery lookup digest differs")
    value = object.__new__(DurableDeliveryLookup)
    object.__setattr__(value, "snapshot", snapshot)
    object.__setattr__(value, "result", result)
    return value


def _issue_durable_read_lookup(
    snapshot: _workflow.WorkflowEffectSnapshot,
    result: ReadReceipt,
) -> DurableReadLookup:
    if (
        type(snapshot) is not _workflow.WorkflowEffectSnapshot
        or snapshot.receipt.action is not _workflow.OperationAction.READ
        or type(result) is not ReadReceipt
    ):
        raise EffectRecoveryRequired("durable READ lookup is invalid")
    try:
        output = _safe_text(result.output, "read output", allow_empty=True).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise EffectRecoveryRequired("durable READ lookup is invalid") from exc
    if (
        snapshot.receipt.result_kind != "read_output"
        or snapshot.receipt.result_digest != _body_digest(_READ_RESULT_DOMAIN, output)
    ):
        raise EffectRecoveryRequired("durable READ lookup digest differs")
    value = object.__new__(DurableReadLookup)
    object.__setattr__(value, "snapshot", snapshot)
    object.__setattr__(value, "result", result)
    return value


class WorkflowEffectAdapter:
    """Execute one durable effect through injected private authority ports."""

    def __init__(
        self,
        store: WorkflowEffectStorePort,
        backend: DurableEffectBackendPort,
        authority: WorkflowEffectAuthorityPort,
        projector: EffectProjectionPort,
        *,
        clock: Callable[[], int],
    ) -> None:
        self._capabilities = require_durable_capabilities(backend)
        required = (
            (store, "load_checkpoint"),
            (store, "begin_operation"),
            (store, "_issue_workflow_receipt"),
            (store, "commit_effect"),
            (store, "mark_unknown"),
            (store, "_lookup_workflow_effect"),
            (store, "_lookup_workflow_delivery_effect"),
            (authority, "authorize"),
            (authority, "validate"),
            (authority, "validate_observation"),
            (authority, "validate_lookup"),
            (projector, "project"),
        )
        try:
            incomplete = any(
                not callable(getattr(port, name, None)) for port, name in required
            )
        except Exception as exc:
            raise DurabilityUnsupported(
                "durable effect composition port is incomplete"
            ) from exc
        if incomplete or not callable(clock):
            raise DurabilityUnsupported("durable effect composition port is incomplete")
        self._store = store
        self._backend = backend
        self._authority_port = authority
        self._projector = projector
        self._clock = clock

    def _now(self) -> int:
        try:
            return _workflow._require_int(self._clock(), "effect clock")
        except (TypeError, ValueError) as exc:
            raise _workflow.OperationIdentityConflict(
                "durable effect clock is invalid"
            ) from exc

    @staticmethod
    def _request_for_current(
        command: EffectCommand,
        root: _workflow.RootIdentity,
        current: _workflow.WorkflowCheckpointObservation | None,
    ) -> EffectRequestIdentity:
        if type(root) is not _workflow.RootIdentity:
            raise TypeError("effect root identity is invalid")
        root.__post_init__()
        if current is None:
            if command.action is not _workflow.OperationAction.START:
                raise _workflow.StateConflict("workflow root is not started")
            return derive_effect_request_identity(
                command,
                root=root,
                run=None,
                assignment=None,
                pending_delivery=None,
                expected_workflow_sequence=0,
                expected_task_sequence=None,
            )
        if type(current) is not _workflow.WorkflowCheckpointV4:
            raise _workflow.RecoveryRequired("workflow root requires explicit recovery")
        if command.action is _workflow.OperationAction.START:
            raise _workflow.StateConflict("workflow root is already started")
        if current.root != root:
            raise _workflow.OperationIdentityConflict("workflow root identity differs")
        if (
            current.last_operation is not None
            and current.last_operation.status is not _workflow.OperationStatus.COMMITTED
        ):
            raise _workflow.RecoveryRequired(
                "workflow root has an unresolved operation"
            )
        return derive_effect_request_identity(
            command,
            root=root,
            run=current.run,
            assignment=current.active_assignment,
            pending_delivery=current.pending_delivery,
            expected_workflow_sequence=current.workflow_sequence,
            expected_task_sequence=current.task_sequence,
        )

    def _validate_delivery_origin(self, request: EffectRequestIdentity) -> None:
        if request.command.action not in {
            _workflow.OperationAction.REPLY,
            _workflow.OperationAction.READ,
            _workflow.OperationAction.RELEASE,
            _workflow.OperationAction.ACK,
        }:
            return
        delivery = request.pending_delivery
        run = request.run
        if delivery is None or run is None:
            raise _workflow.OperationIdentityConflict(
                "workflow Delivery identity is missing"
            )
        origin = self._store._lookup_workflow_delivery_effect(
            _workflow.WorkflowRootKey(request.root.root_key),
            delivery.delivery_id,
            run.consumer_generation,
        )
        if (
            origin.checkpoint.active_assignment != request.assignment
            or origin.checkpoint.pending_delivery != delivery
        ):
            raise _workflow.OperationIdentityConflict(
                "workflow Delivery origin assignment differs"
            )

    def _validate_authority(
        self,
        authority: WorkflowEffectAuthority,
        request: EffectRequestIdentity,
    ) -> None:
        try:
            self._authority_port.validate(authority, request)
        except Exception:  # noqa: BLE001 - authority errors may contain provider data
            raise _workflow.OperationIdentityConflict(
                "workflow effect authority validation failed"
            ) from None
        require_live_authority(authority, request=request, now_ns=self._now())

    def _mark_unknown(
        self,
        operation: _workflow.OperationHandle,
        *,
        reason: _workflow.RecoveryCode,
        cause: BaseException,
    ) -> NoReturn:
        del cause
        try:
            self._store.mark_unknown(operation, reason=reason)
        except Exception:  # noqa: BLE001 - recovery write failure must not expose raw effect data
            raise EffectRecoveryRequired(
                "workflow effect requires explicit recovery"
            ) from None
        raise EffectRecoveryRequired(
            "workflow effect requires explicit recovery"
        ) from None

    def _lookup_backend_observation(
        self,
        snapshot: _workflow.WorkflowEffectSnapshot,
    ) -> BackendEffectObservation:
        try:
            observation = self._backend.lookup(snapshot)
            self._authority_port.validate_lookup(snapshot, observation)
            return _validate_lookup_observation(observation, snapshot)
        except Exception:  # noqa: BLE001 - lookup failures are redacted recovery outcomes
            raise EffectRecoveryRequired(
                "backend effect lookup is unavailable"
            ) from None

    def _replay(
        self,
        operation_id: str,
    ) -> ReplayedEffect:
        snapshot = self._store._lookup_workflow_effect(
            _workflow.WorkflowOperationId(operation_id)
        )
        receipt = snapshot.receipt
        checkpoint = snapshot.checkpoint
        if receipt.action is _workflow.OperationAction.START:
            result: EffectPublicResult = StartResult(
                checkpoint.root.team_id,
                RunRef(checkpoint.run.run_id),
                TerminalRef(checkpoint.run.main_terminal_id),
                Path(checkpoint.root.state_root_path),
            )
        elif receipt.action is _workflow.OperationAction.PROMPT:
            if checkpoint.active_assignment is None:
                raise EffectRecoveryRequired("workflow replay is incomplete")
            result = _public_assignment(checkpoint.active_assignment)
        elif (
            receipt.action is _workflow.OperationAction.WAIT
            and receipt.result_kind == "timeout"
        ):
            result = WaitReceipt(None, ())
        elif receipt.action is _workflow.OperationAction.REPLY:
            result = ReplyReceipt(True)
        elif receipt.action is _workflow.OperationAction.ACK:
            result = AckReceipt(True)
        else:
            if (
                receipt.action is _workflow.OperationAction.RELEASE
                and not self._capabilities.pure_effect_lookup
            ):
                raise DurabilityUnsupported(
                    "durable RELEASE replay requires pure effect lookup"
                )
            require_durable_action(self._capabilities, receipt.action)
            observation = self._lookup_backend_observation(snapshot)
            result = observation.public_result
        return ReplayedEffect(operation_id, snapshot, result)

    def replay(
        self,
        operation_id: _workflow.WorkflowOperationId,
    ) -> ReplayedEffect:
        try:
            operation = _workflow._require_identifier(
                operation_id,
                "replay operation_id",
            )
        except (TypeError, ValueError) as exc:
            raise _workflow.OperationIdentityConflict(
                "replay operation identity is invalid"
            ) from exc
        return self._replay(operation)

    def lookup_delivery(
        self,
        *,
        root_key: _workflow.WorkflowRootKey,
        delivery_id: str,
        consumer_generation: int,
    ) -> DurableDeliveryLookup:
        require_durable_action(
            self._capabilities,
            _workflow.OperationAction.WAIT,
        )
        snapshot = self._store._lookup_workflow_delivery_effect(
            root_key,
            delivery_id,
            consumer_generation,
        )
        observation = self._lookup_backend_observation(snapshot)
        if type(observation.public_result) is not WaitReceipt:
            raise EffectRecoveryRequired("backend Delivery lookup result differs")
        return _issue_durable_delivery_lookup(snapshot, observation.public_result)

    def lookup_read(
        self,
        operation_id: _workflow.WorkflowOperationId,
    ) -> DurableReadLookup:
        require_durable_action(
            self._capabilities,
            _workflow.OperationAction.READ,
        )
        snapshot = self._store._lookup_workflow_effect(operation_id)
        if snapshot.receipt.action is not _workflow.OperationAction.READ:
            raise EffectRecoveryRequired("workflow effect is not a READ operation")
        observation = self._lookup_backend_observation(snapshot)
        if type(observation.public_result) is not ReadReceipt:
            raise EffectRecoveryRequired("backend READ lookup result differs")
        return _issue_durable_read_lookup(snapshot, observation.public_result)

    def execute(
        self,
        command: EffectCommand,
        *,
        root: _workflow.RootIdentity,
        payload: object = None,
    ) -> AppliedEffect | ReplayedEffect:
        _validate_effect_command(command)
        require_durable_action(self._capabilities, command.action)
        normalized_payload = _normalize_payload(command, payload)
        current = self._store.load_checkpoint(_workflow.WorkflowRootKey(root.root_key))
        request = self._request_for_current(command, root, current)
        self._validate_delivery_origin(request)
        try:
            authority = self._authority_port.authorize(request)
        except Exception:  # noqa: BLE001 - authority errors may contain provider data
            raise _workflow.OperationIdentityConflict(
                "workflow effect authority is unavailable"
            ) from None
        if type(authority) is not WorkflowEffectAuthority:
            raise _workflow.OperationIdentityConflict(
                "workflow effect authority type is invalid"
            )
        self._validate_authority(authority, request)
        identity = derive_effect_identity(request, authority)
        intent = _operation_intent(request, identity, authority)
        begun = self._store.begin_operation(
            intent,
            expected_workflow_sequence=request.expected_workflow_sequence,
            expected_task_sequence=request.expected_task_sequence,
        )
        if type(begun) is _workflow.StoredReplay:
            _validate_stored_replay_response(begun, request, identity)
            return self._replay(identity.operation_id)
        if type(begun) is not _workflow.OperationBegin:
            raise _workflow.OperationIdentityConflict(
                "workflow operation begin result is invalid"
            )
        operation = begun.operation
        try:
            self._validate_authority(authority, request)
        except Exception as exc:  # noqa: BLE001 - invalidated authority leaves intent unresolved
            self._mark_unknown(
                operation,
                reason=_workflow.RecoveryCode.UNKNOWN_EFFECT,
                cause=exc,
            )
        effect = _issue_backend_effect_request(
            command=command,
            request=request,
            identity=identity,
            authority=authority,
            operation=operation,
            payload=normalized_payload,
        )
        try:
            observation = self._backend.execute(effect)
        except Exception as exc:  # noqa: BLE001 - backend errors cannot prove no effect
            self._mark_unknown(
                operation,
                reason=_workflow.RecoveryCode.RESPONSE_LOST,
                cause=exc,
            )
        try:
            self._authority_port.validate_observation(
                authority,
                request,
                observation,
            )
            self._validate_authority(authority, request)
            validate_observation(
                observation,
                request=request,
                identity=identity,
                authority=authority,
            )
            observation_snapshot = _canonical_json(_observation_mapping(observation))
            assignment = observation.assignment
            receipt = self._store._issue_workflow_receipt(
                operation=operation,
                receipt_id=_receipt_id(identity, observation),
                run_id=observation.run_id,
                main_terminal_id=observation.main_terminal_id,
                consumer_generation=observation.consumer_generation,
                task_id=None if assignment is None else assignment.task_id,
                dispatch_id=None if assignment is None else assignment.dispatch_id,
                attempt=None if assignment is None else assignment.attempt,
                terminal_id=None if assignment is None else assignment.terminal_id,
                delivery_id=(
                    None
                    if observation.delivery is None
                    else observation.delivery.delivery_id
                ),
                message_id=observation.message_id,
                effect_ref=observation.effect_ref,
                result_kind=observation.result_kind,
                result_digest=observation.result_digest,
                evidence_ref=observation.evidence_ref,
                issued_ns=self._now(),
            )
            receipt_digest = _workflow.durable_receipt_digest(receipt)
            self._validate_authority(authority, request)
            projection = self._projector.project(
                current,
                request,
                observation,
                receipt,
            )
            if type(projection) is not EffectProjection:
                raise TypeError("effect projector result is invalid")
            projection.__post_init__()
            self._validate_authority(authority, request)
            if (
                _workflow.durable_receipt_digest(receipt) != receipt_digest
                or observation_snapshot
                != _canonical_json(_observation_mapping(observation))
                or not _same_public_result(
                    projection.public_result, observation.public_result
                )
            ):
                raise _workflow.OperationIdentityConflict(
                    "effect projection identity differs"
                )
            validate_observation(
                observation,
                request=request,
                identity=identity,
                authority=authority,
            )
        except Exception as exc:  # noqa: BLE001 - post-effect port errors are ambiguous
            self._mark_unknown(
                operation,
                reason=_workflow.RecoveryCode.RECEIPT_MISMATCH,
                cause=exc,
            )
        try:
            committed = self._store.commit_effect(
                operation,
                receipt,
                projection.next_checkpoint,
            )
        except _workflow.RecoveryRequired:
            raise
        except Exception as exc:
            raise EffectRecoveryRequired(
                "workflow effect commit requires explicit recovery"
            ) from exc
        if type(committed) is _workflow.StoredReplay:
            _validate_stored_replay_response(committed, request, identity)
            return self._replay(identity.operation_id)
        if type(committed) is not _workflow.WorkflowCommit:
            raise EffectRecoveryRequired("workflow effect commit result is ambiguous")
        return AppliedEffect(
            operation_id=identity.operation_id,
            receipt=committed.receipt,
            checkpoint=committed.checkpoint,
            public_result=projection.public_result,
        )
