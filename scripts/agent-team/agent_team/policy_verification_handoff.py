"""Owner-issued policy/verification handoff contract.

This module binds #49 review authority and #50 completion admission before
issuing the opaque approval consumed by #51.  It owns no persistence and no
runner: one injected private registry must save and read every contract record
and provide the existing six-method verification state port.

Issue #74 supplies the owner contract and deterministic fixtures.  Issue #81
uses the read-only process-local update/ref binding for review persistence;
Issue #82 owns approval capture, Store hydration, and unknown-effect mutation
on the schema-4 foundation from Issue #80.  Neither child hydrates these
private record classes or issuer sentinels as wire values.
"""

from __future__ import annotations

import hashlib
import hmac
import posixpath
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from threading import RLock
from typing import Final, NoReturn, Protocol, SupportsIndex
from weakref import WeakKeyDictionary

from . import verification_gate as _gate
from .path_resource_policy import (
    CompletionAdmissionRef,
    DispatchMode,
    LaneProfileBinding,
    LaneRoutingDecision,
    PathClaimPolicy,
    PathMutation,
    PathObservation,
    ReservationStatus,
    ResourceClaimPolicy,
    ResourceKey,
    ResourceReservationAuthority,
    ResourceReservationPort,
    ResourceReservationRequest,
    ResourceReservationResult,
    _canonical_lane_profile_projection,
    _canonical_task_projection,
    _issue_completion_admission_ref,
    _validate_completion_admission_ref,
    adapt_resource_claims,
    route_task,
)
from .review_policy import (
    PolicyProjectionKind,
    ReviewAuthorityRef,
    ReviewDecisionKind,
    ReviewPolicyUpdate,
    SerialReviewPolicy,
    _issue_review_authority_ref,
    _validate_review_authority_ref,
    policy_authority_projection,
    validate_policy_update,
)
from .task_policy import TaskLane, TaskPhase, TaskSpec

MAX_IDENTIFIER_CHARS: Final = 256
MAX_PATH_CHARS: Final = 4096
_SHA256: Final = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_REFERENCE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_HANDOFF_EXCEPTIONS: Final = (
    AttributeError,
    LookupError,
    OSError,
    OverflowError,
    RuntimeError,
    TypeError,
    ValueError,
)

_REVIEW_RECORD_ISSUER: Final = object()
_COMPLETION_RECORD_ISSUER: Final = object()
_APPROVAL_RECORD_ISSUER: Final = object()


class PolicyVerificationHandoffError(ValueError):
    """A bounded owner/ref/composition contract rejection."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code if type(code) is str and code else "handoff-invalid"
        super().__init__(f"{self.code}: {message}")


class PolicyVerificationRecoveryRequired(PolicyVerificationHandoffError):
    """An authority Store response cannot be classified by exact readback."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = _safe_code(reason_code)
        super().__init__("recovery-required", self.reason_code)


def _error(code: str, message: str) -> PolicyVerificationHandoffError:
    return PolicyVerificationHandoffError(code, message)


def _safe_code(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_IDENTIFIER_CHARS
        or _SAFE_REFERENCE.fullmatch(value) is None
    ):
        return "handoff-unknown"
    return value


def _safe_text(
    value: object, context: str, *, maximum: int = MAX_IDENTIFIER_CHARS
) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _error("invalid-value", f"{context} is invalid")
    if len(value) > maximum or any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        or ord(character) in {0x2028, 0x2029}
        for character in value
    ):
        raise _error("invalid-value", f"{context} is invalid")
    return value


def _reference(value: object, context: str) -> str:
    candidate = _safe_text(value, context)
    if _SAFE_REFERENCE.fullmatch(candidate) is None:
        raise _error("invalid-reference", f"{context} is invalid")
    return candidate


def _bounded_identity(value: object, context: str) -> str:
    """Keep #50's bounded opaque text grammar separate from ref grammar."""

    return _safe_text(value, context, maximum=MAX_IDENTIFIER_CHARS)


def _digest(value: object, context: str) -> str:
    candidate = _safe_text(value, context, maximum=64)
    if _SHA256.fullmatch(candidate) is None:
        raise _error("invalid-digest", f"{context} is invalid")
    return candidate


def _canonical_path(value: object, context: str) -> str:
    candidate = _safe_text(value, context, maximum=MAX_PATH_CHARS)
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or posixpath.normpath(candidate) != candidate
    ):
        raise _error("invalid-path", f"{context} is invalid")
    return candidate


def _optional(value: object, context: str) -> str:
    if value is None:
        return ""
    return _safe_text(value, context, maximum=MAX_PATH_CHARS)


def _framed_digest(parts: Iterable[str]) -> str:
    values = tuple(parts)
    digest = hashlib.sha256()
    digest.update(len(values).to_bytes(8, "big"))
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _same_text(left: object, right: object) -> bool:
    if type(left) is not str or type(right) is not str:
        return False
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except UnicodeEncodeError:
        return False


class _OpaqueRecord:
    __slots__ = ()

    def __copy__(self) -> NoReturn:
        raise TypeError(f"{type(self).__name__} cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError(f"{type(self).__name__} cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError(f"{type(self).__name__} cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError(f"{type(self).__name__} cannot be pickled")


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class _ReviewAuthorityRecord(_OpaqueRecord):
    reference: str
    digest: str
    policy_fingerprint: str
    team_id: str
    workspace: str
    run_id: str
    task_id: str
    dispatch_id: str
    attempt_id: str
    worker_node: str
    reviewer_node: str
    worker_terminal_id: str
    reviewer_terminal_id: str
    review_round: int
    sequence: int
    phase: str
    event_kind: str
    completion_kind: str | None
    decision_kind: str | None
    completion_id: str | None
    decision_ref: str | None
    target_head: str | None
    target_tree_digest: str | None
    reason_code: str | None
    claim_ref: str | None
    profile_ref: str
    lane: str
    _issuer: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("review authority record is return-only")

    def __repr__(self) -> str:
        return "<_ReviewAuthorityRecord opaque>"


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False, weakref_slot=True)
class _ReviewAuthorityBinding(_OpaqueRecord):
    """Process-local proof that one actual policy update owns one ref.

    The update and policy are retained as the exact objects supplied by the
    policy owner.  The object is intentionally not a wire value: its weak-key
    registry entry carries immutable snapshots used to reject ``object``
    mutation, forged instances, and ref substitution before a consumer can
    use the update.
    """

    update: ReviewPolicyUpdate
    policy: SerialReviewPolicy
    review_ref: ReviewAuthorityRef
    _issuer: object = field(repr=False, compare=False)

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("review authority binding is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("review authority binding is return-only")

    def __repr__(self) -> str:
        return "<_ReviewAuthorityBinding opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("review authority binding cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("review authority binding cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("review authority binding cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("review authority binding cannot be pickled")


@dataclass(frozen=True, slots=True)
class _ReviewAuthorityEvidence:
    """Validated, typed owner evidence returned to a producer consumer."""

    owner: PolicyVerificationHandoff
    update: ReviewPolicyUpdate
    policy: SerialReviewPolicy
    review_ref: ReviewAuthorityRef

    @property
    def actual_update(self) -> ReviewPolicyUpdate:
        return self.update

    @property
    def ref(self) -> ReviewAuthorityRef:
        return self.review_ref


@dataclass(frozen=True, slots=True)
class _ReviewAuthorityBindingState:
    """Registry-only state for one process-local binding object."""

    owner: PolicyVerificationHandoff
    store: object
    update: ReviewPolicyUpdate
    policy: SerialReviewPolicy
    review_ref: ReviewAuthorityRef
    update_snapshot: tuple[object, ...]
    policy_snapshot: tuple[object, ...]
    record_snapshot: tuple[object, ...]


_REVIEW_AUTHORITY_BINDING_ISSUER: Final = object()
_REVIEW_AUTHORITY_BINDINGS: WeakKeyDictionary[
    _ReviewAuthorityBinding, _ReviewAuthorityBindingState
] = WeakKeyDictionary()
_REVIEW_AUTHORITY_BINDINGS_LOCK: Final = RLock()
_HANDOFF_REVIEW_REF_OWNERS: WeakKeyDictionary[
    ReviewAuthorityRef, tuple[object, object]
] = WeakKeyDictionary()
_HANDOFF_REVIEW_REF_OWNERS_LOCK: Final = RLock()
_HANDOFF_COMPLETION_REF_OWNERS: WeakKeyDictionary[
    CompletionAdmissionRef, tuple[object, object, str, str]
] = WeakKeyDictionary()
_HANDOFF_COMPLETION_REF_OWNERS_LOCK: Final = RLock()


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class _CompletionAdmissionRecord(_OpaqueRecord):
    reference: str
    digest: str
    team_id: str
    task_id: str
    workspace: str
    profile_ref: str
    policy_fingerprint: str
    worker_node: str
    reviewer_node: str
    lane: str
    path_claim_digest: str
    resource_claim_digest: str
    routing_digest: str
    reservation_digest: str | None
    reservation_id: str | None
    reservation_claim_keys: tuple[str, ...]
    reservation_owner: str | None
    reservation_lease_epoch: int | None
    reservation_fencing_token: int | None
    candidate: bool
    dispatch_mode: str
    serial_review_required: bool
    completion_gate_required: bool
    permits_workspace_write: bool
    parallel_candidate: bool
    _issuer: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("completion admission record is return-only")

    def __repr__(self) -> str:
        return "<_CompletionAdmissionRecord opaque>"


@dataclass(frozen=True, slots=True)
class _ReservationRequestEvidence:
    task_id: str
    reservation_id: str
    claim_keys: tuple[str, ...]
    owner: str
    lease_epoch: int
    fencing_token: int
    request_digest: str


@dataclass(frozen=True, slots=True)
class _ReservationResultEvidence:
    status: str
    reservation_id: str
    claim_keys: tuple[str, ...]
    owner: str | None
    lease_epoch: int | None
    fencing_token: int | None
    task_id: str | None
    request_digest: str | None


@dataclass(frozen=True, slots=True)
class _CompletionInputSnapshot:
    task_projection: tuple[object, ...]
    profile_projection: tuple[object, ...]
    team_id: str
    task_id: str
    workspace: str
    profile_ref: str
    policy_fingerprint: str | None
    worker_node: str
    reviewer_node: str | None
    lane: str
    path_claim_digest: str
    resource_claim_digest: str
    expected_reservation: _ReservationRequestEvidence | None


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class _ApprovalRecord(_OpaqueRecord):
    approval_ref: str
    digest: str
    review_ref: ReviewAuthorityRef
    completion_ref: CompletionAdmissionRef
    review_digest: str
    completion_digest: str
    bound: _gate._BoundApproval
    _issuer: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("approval record is return-only")

    def __repr__(self) -> str:
        return "<_ApprovalRecord opaque>"


class _PolicyVerificationRegistryPort(Protocol):
    """Process-local contract fixture plus the same injected #51 state port."""

    def save_review_authority(
        self, record: _ReviewAuthorityRecord
    ) -> _ReviewAuthorityRecord: ...

    def read_review_authority(self, reference: str) -> _ReviewAuthorityRecord: ...

    def save_completion_admission(
        self, record: _CompletionAdmissionRecord
    ) -> _CompletionAdmissionRecord: ...

    def read_completion_admission(
        self, reference: str
    ) -> _CompletionAdmissionRecord: ...

    def save_approval(self, record: _ApprovalRecord) -> _ApprovalRecord: ...

    def read_approval(self, reference: str) -> _ApprovalRecord: ...

    def state_port(self) -> _gate.VerificationStatePort: ...


def _review_parts(value: _ReviewAuthorityRecord) -> tuple[str, ...]:
    return (
        "review-authority-v1",
        value.policy_fingerprint,
        value.team_id,
        value.workspace,
        value.run_id,
        value.task_id,
        value.dispatch_id,
        value.attempt_id,
        value.worker_node,
        value.reviewer_node,
        value.worker_terminal_id,
        value.reviewer_terminal_id,
        str(value.review_round),
        str(value.sequence),
        value.phase,
        value.event_kind,
        _optional(value.completion_kind, "review completion kind"),
        _optional(value.decision_kind, "review decision kind"),
        _optional(value.completion_id, "review completion ID"),
        _optional(value.decision_ref, "review decision ref"),
        _optional(value.target_head, "review target head"),
        _optional(value.target_tree_digest, "review target tree"),
        _optional(value.reason_code, "review reason"),
        _optional(value.claim_ref, "review claim ref"),
        value.profile_ref,
        value.lane,
    )


def _validate_review_record(value: object) -> _ReviewAuthorityRecord:
    if type(value) is not _ReviewAuthorityRecord:
        raise _error("review-record-invalid", "review authority record type is invalid")
    try:
        if object.__getattribute__(value, "_issuer") is not _REVIEW_RECORD_ISSUER:
            raise _error(
                "review-record-invalid", "review authority record issuer is invalid"
            )
        _reference(value.reference, "review reference")
        _digest(value.digest, "review digest")
        _digest(value.policy_fingerprint, "review policy fingerprint")
        for name in (
            "team_id",
            "run_id",
            "task_id",
            "dispatch_id",
            "attempt_id",
            "worker_node",
            "reviewer_node",
            "worker_terminal_id",
            "reviewer_terminal_id",
            "profile_ref",
        ):
            _bounded_identity(getattr(value, name), f"review {name}")
        _canonical_path(value.workspace, "review workspace")
        if type(value.review_round) is not int or value.review_round < 1:
            raise _error("review-record-invalid", "review round is invalid")
        if type(value.sequence) is not int or value.sequence < 1:
            raise _error("review-record-invalid", "review sequence is invalid")
        if value.phase not in {item.value for item in TaskPhase}:
            raise _error("review-record-invalid", "review phase is invalid")
        if value.event_kind not in {item.value for item in PolicyProjectionKind}:
            raise _error("review-record-invalid", "review event kind is invalid")
        if value.decision_kind is not None and value.decision_kind not in {
            item.value for item in ReviewDecisionKind
        }:
            raise _error("review-record-invalid", "review decision kind is invalid")
        if value.lane not in {item.value for item in TaskLane}:
            raise _error("review-record-invalid", "review lane is invalid")
        expected = _framed_digest(_review_parts(value))
        if not _same_text(value.digest, expected):
            raise _error("review-record-invalid", "review digest differs")
        if not _same_text(value.reference, f"review-authority-{value.digest}"):
            raise _error("review-record-invalid", "review reference differs")
    except AttributeError as exc:
        raise _error(
            "review-record-invalid", "review authority record is malformed"
        ) from exc
    return value


def _make_review_record(
    update: ReviewPolicyUpdate, policy: SerialReviewPolicy
) -> _ReviewAuthorityRecord:
    if type(update) is not ReviewPolicyUpdate or type(policy) is not SerialReviewPolicy:
        raise _error("review-input-invalid", "review update or policy type is invalid")
    try:
        validate_policy_update(update, policy)
        projection = policy_authority_projection(update, policy)
        state = update.next_state.task_state
        record = object.__new__(_ReviewAuthorityRecord)
        values: dict[str, object] = {
            "reference": "review-authority-placeholder",
            "digest": "0" * 64,
            "policy_fingerprint": str(projection.policy_fingerprint),
            "team_id": str(projection.team_id),
            "workspace": str(projection.workspace),
            "run_id": str(projection.run_id),
            "task_id": str(projection.task_id),
            "dispatch_id": str(projection.dispatch_id),
            "attempt_id": str(projection.attempt_id),
            "worker_node": str(projection.worker_node),
            "reviewer_node": str(projection.reviewer_node),
            "worker_terminal_id": str(projection.worker_terminal_id),
            "reviewer_terminal_id": str(projection.reviewer_terminal_id),
            "review_round": projection.review_round,
            "sequence": projection.sequence,
            "phase": projection.phase.value,
            "event_kind": projection.event_kind.value,
            "completion_kind": (
                None
                if projection.worker_completion_kind is None
                else projection.worker_completion_kind.value
            ),
            "decision_kind": (
                None
                if projection.review_decision_kind is None
                else projection.review_decision_kind.value
            ),
            "completion_id": (
                None
                if projection.completion_id is None
                else str(projection.completion_id)
            ),
            "decision_ref": (
                None
                if projection.decision_ref is None
                else str(projection.decision_ref)
            ),
            "target_head": (
                None if projection.target_head is None else str(projection.target_head)
            ),
            "target_tree_digest": (
                None
                if projection.target_tree_digest is None
                else str(projection.target_tree_digest)
            ),
            "reason_code": projection.reason_code,
            "claim_ref": None if state.claim_ref is None else str(state.claim_ref),
            "profile_ref": str(policy.task.verification),
            "lane": policy.task.lane.value,
            "_issuer": _REVIEW_RECORD_ISSUER,
        }
        for name, item in values.items():
            object.__setattr__(record, name, item)
        digest = _framed_digest(_review_parts(record))
        object.__setattr__(record, "digest", digest)
        object.__setattr__(record, "reference", f"review-authority-{digest}")
        return _validate_review_record(record)
    except PolicyVerificationHandoffError:
        raise
    except _HANDOFF_EXCEPTIONS:
        raise _error(
            "review-input-invalid", "review update is not admissible"
        ) from None


def _binding_failure() -> PolicyVerificationHandoffError:
    """Return one constant error for every malformed binding path."""

    return _error(
        "review-authority-binding-invalid", "review authority binding is invalid"
    )


def _binding_value_snapshot(
    value: object, _active: set[int] | None = None
) -> tuple[object, ...]:
    """Capture the complete typed update/policy graph without invoking repr.

    The policy objects are frozen dataclasses, but callers can still mutate
    them through ``object.__setattr__``.  A structural snapshot therefore
    complements the object-identity registry: identity catches substitution,
    while this snapshot catches mutation of any nested causal value.
    """

    active = set() if _active is None else _active
    if value is None:
        return ("none",)
    if type(value) is bool:
        return ("bool", value)
    if type(value) is int:
        return ("int", value)
    if type(value) is str:
        return ("str", value)
    if type(value) is bytes:
        return ("bytes", value)
    if isinstance(value, Enum):
        return (
            "enum",
            type(value),
            _binding_value_snapshot(value.value, active),
        )
    if type(value) is tuple:
        identity = id(value)
        if identity in active:
            raise _binding_failure()
        active.add(identity)
        try:
            return (
                "tuple",
                tuple(_binding_value_snapshot(item, active) for item in value),
            )
        finally:
            active.remove(identity)
    if type(value) is list:
        identity = id(value)
        if identity in active:
            raise _binding_failure()
        active.add(identity)
        try:
            return (
                "list",
                tuple(_binding_value_snapshot(item, active) for item in value),
            )
        finally:
            active.remove(identity)
    if is_dataclass(value) and not isinstance(value, type):
        identity = id(value)
        if identity in active:
            raise _binding_failure()
        active.add(identity)
        try:
            return (
                "dataclass",
                type(value),
                tuple(
                    (
                        item.name,
                        _binding_value_snapshot(
                            object.__getattribute__(value, item.name), active
                        ),
                    )
                    for item in fields(value)
                ),
            )
        finally:
            active.remove(identity)
    raise _binding_failure()


def _review_record_snapshot(value: _ReviewAuthorityRecord) -> tuple[object, ...]:
    """Snapshot a validated owner record without retaining its issuer object."""

    return (
        value.reference,
        value.digest,
        *_review_parts(value),
    )


def _path_claim_parts(
    path_policy: PathClaimPolicy,
    mutation: PathMutation,
    observations: tuple[PathObservation, ...],
) -> tuple[str, ...]:
    workspace = path_policy.workspace
    parts: list[str] = [
        "completion-path-claim-v1",
        str(workspace.workspace),
        workspace.canonical_path,
        str(workspace.device),
        str(workspace.inode),
        "true" if workspace.case_sensitive else "false",
    ]
    for kind, claims in (("allow", path_policy.allowed), ("deny", path_policy.denied)):
        for claim in claims:
            parts.extend(
                (kind, claim.relative_path, claim.kind.value, claim.access.value)
            )
    for root in path_policy.reserved_roots:
        parts.extend(("reserved", root))
    parts.extend(
        (
            "mutation",
            mutation.operation.value,
            mutation.source,
            "" if mutation.destination is None else mutation.destination,
        )
    )
    for observation in sorted(
        observations,
        key=lambda item: (item.relative_path.casefold(), item.relative_path),
    ):
        parts.extend(
            (
                "observation",
                observation.relative_path,
                observation.canonical_path,
                observation.entry_kind.value,
                "" if observation.device is None else str(observation.device),
                "" if observation.inode is None else str(observation.inode),
                "" if observation.nlink is None else str(observation.nlink),
                ""
                if observation.parent_device is None
                else str(observation.parent_device),
                ""
                if observation.parent_inode is None
                else str(observation.parent_inode),
                "true" if observation.ancestor_symlink else "false",
            )
        )
    return tuple(parts)


def _resource_claim_parts(claims: tuple[ResourceClaimPolicy, ...]) -> tuple[str, ...]:
    parts: list[str] = ["completion-resource-claim-v1"]
    for claim in claims:
        parts.extend((claim.claim.name, str(claim.key), claim.mode.value))
    return tuple(parts)


def _reservation_request_evidence(
    value: object,
) -> _ReservationRequestEvidence:
    if type(value) is not ResourceReservationRequest:
        raise _error("reservation-invalid", "reservation request type is invalid")
    try:
        ResourceReservationRequest.__post_init__(value)
        authority = value.authority
        if type(authority) is not ResourceReservationAuthority:
            raise _error("reservation-invalid", "reservation authority is missing")
        return _ReservationRequestEvidence(
            task_id=str(value.task_id),
            reservation_id=value.reservation_id,
            claim_keys=tuple(str(item.key) for item in value.claims),
            owner=authority.owner_id,
            lease_epoch=authority.lease_epoch,
            fencing_token=authority.fencing_token,
            request_digest=str(value.request_digest),
        )
    except PolicyVerificationHandoffError:
        raise
    except _HANDOFF_EXCEPTIONS:
        raise _error("reservation-invalid", "reservation request is invalid") from None


def _reservation_result_evidence(value: object) -> _ReservationResultEvidence:
    if type(value) is not ResourceReservationResult:
        raise _error("reservation-invalid", "reservation result type is invalid")
    try:
        ResourceReservationResult.__post_init__(value)
        authority = value.authority
        return _ReservationResultEvidence(
            status=value.status.value,
            reservation_id=value.reservation_id,
            claim_keys=tuple(str(item) for item in value.claim_keys),
            owner=None if authority is None else authority.owner_id,
            lease_epoch=None if authority is None else authority.lease_epoch,
            fencing_token=None if authority is None else authority.fencing_token,
            task_id=None if value.task_id is None else str(value.task_id),
            request_digest=(
                None if value.request_digest is None else str(value.request_digest)
            ),
        )
    except PolicyVerificationHandoffError:
        raise
    except _HANDOFF_EXCEPTIONS:
        raise _error("reservation-invalid", "reservation result is invalid") from None


class _CapturedReservationPort:
    """Capture the one owner call so the handoff can bind its exact result."""

    __slots__ = ("_port", "attempts", "request", "result")

    def __init__(self, port: ResourceReservationPort) -> None:
        self._port = port
        self.attempts = 0
        self.request: _ReservationRequestEvidence | None = None
        self.result: _ReservationResultEvidence | None = None

    def reserve(self, request: ResourceReservationRequest) -> ResourceReservationResult:
        self.attempts += 1
        if self.attempts != 1:
            raise RuntimeError("reservation port may be called once")
        before = _reservation_request_evidence(request)
        try:
            result = self._port.reserve(request)
        except Exception:  # noqa: BLE001 - injected port text must not escape
            raise RuntimeError("reservation port failed") from None
        after = _reservation_request_evidence(request)
        if before != after:
            raise RuntimeError("reservation request changed during the owner call")
        self.request = before
        self.result = _reservation_result_evidence(result)
        return result


def _completion_input_snapshot(
    *,
    task: TaskSpec,
    path_policy: PathClaimPolicy,
    path_mutation: PathMutation,
    path_observations: tuple[PathObservation, ...],
    resource_claims: tuple[ResourceClaimPolicy, ...],
    known_keys: frozenset[ResourceKey],
    profile: LaneProfileBinding,
    reservation_id: str,
    reservation_authority: ResourceReservationAuthority | None,
) -> _CompletionInputSnapshot:
    try:
        if (
            type(task) is not TaskSpec
            or type(path_policy) is not PathClaimPolicy
            or type(path_mutation) is not PathMutation
            or type(path_observations) is not tuple
            or type(resource_claims) is not tuple
            or type(known_keys) is not frozenset
            or type(profile) is not LaneProfileBinding
        ):
            raise _error("completion-input-invalid", "completion input type is invalid")
        task_projection = _canonical_task_projection(task)
        PathClaimPolicy.__post_init__(path_policy)
        PathMutation.__post_init__(path_mutation)
        for observation in path_observations:
            if type(observation) is not PathObservation:
                raise _error(
                    "completion-input-invalid", "path observation type is invalid"
                )
            PathObservation.__post_init__(observation)
        LaneProfileBinding.__post_init__(profile)
        adapted = adapt_resource_claims(task, resource_claims, known_keys=known_keys)
        policy = profile.serial_review_policy
        pair = profile.reviewer_pair
        if policy is not None and type(policy) is not SerialReviewPolicy:
            raise _error("completion-input-invalid", "serial review policy is invalid")
        profile_projection = _canonical_lane_profile_projection(profile)
        path_digest = _framed_digest(
            _path_claim_parts(path_policy, path_mutation, path_observations)
        )
        resource_digest = _framed_digest(_resource_claim_parts(adapted))
        expected_reservation: _ReservationRequestEvidence | None
        if adapted:
            request = ResourceReservationRequest(
                task.task_id,
                adapted,
                reservation_id,
                reservation_authority,
            )
            expected_reservation = _reservation_request_evidence(request)
        else:
            _bounded_identity(reservation_id, "reservation ID")
            if reservation_authority is not None:
                raise _error(
                    "completion-input-invalid",
                    "claim-free input has reservation authority",
                )
            expected_reservation = None
        return _CompletionInputSnapshot(
            task_projection=task_projection,
            profile_projection=profile_projection,
            team_id=str(profile.team_definition.team_id),
            task_id=str(task.task_id),
            workspace=str(path_policy.workspace.workspace),
            profile_ref=str(task.verification),
            policy_fingerprint=None if policy is None else str(policy.fingerprint),
            worker_node=str(profile.worker_node),
            reviewer_node=None if pair is None else str(pair.reviewer_node),
            lane=task.lane.value,
            path_claim_digest=path_digest,
            resource_claim_digest=resource_digest,
            expected_reservation=expected_reservation,
        )
    except PolicyVerificationHandoffError:
        raise
    except _HANDOFF_EXCEPTIONS:
        raise _error(
            "completion-input-invalid", "completion input is not admissible"
        ) from None


def _validate_captured_reservation(
    *,
    decision: LaneRoutingDecision,
    snapshot: _CompletionInputSnapshot,
    capture: _CapturedReservationPort,
) -> _ReservationResultEvidence | None:
    expected = snapshot.expected_reservation
    if expected is None:
        if capture.attempts != 0 or decision.reservation is not None:
            raise _error("reservation-invalid", "claim-free route used a reservation")
        return None
    if capture.attempts != 1 or capture.request != expected or capture.result is None:
        raise _error("reservation-invalid", "reservation call identity differs")
    decision_result = _reservation_result_evidence(decision.reservation)
    if decision_result != capture.result:
        raise _error("reservation-invalid", "route reservation result differs")
    observed = capture.result
    if (
        observed.status != ReservationStatus.RESERVED.value
        or observed.reservation_id != expected.reservation_id
        or observed.claim_keys != expected.claim_keys
        or observed.owner != expected.owner
        or observed.lease_epoch != expected.lease_epoch
        or observed.fencing_token != expected.fencing_token
        or observed.task_id != expected.task_id
        or observed.request_digest != expected.request_digest
    ):
        raise _error("reservation-invalid", "reservation identity differs")
    return observed


def _completion_parts(value: _CompletionAdmissionRecord) -> tuple[str, ...]:
    return (
        "completion-admission-v1",
        value.team_id,
        value.task_id,
        value.workspace,
        value.profile_ref,
        value.policy_fingerprint,
        value.worker_node,
        value.reviewer_node,
        value.lane,
        value.path_claim_digest,
        value.resource_claim_digest,
        value.routing_digest,
        "" if value.reservation_digest is None else value.reservation_digest,
        "" if value.reservation_id is None else value.reservation_id,
        str(len(value.reservation_claim_keys)),
        *value.reservation_claim_keys,
        "" if value.reservation_owner is None else value.reservation_owner,
        ""
        if value.reservation_lease_epoch is None
        else str(value.reservation_lease_epoch),
        ""
        if value.reservation_fencing_token is None
        else str(value.reservation_fencing_token),
        "true" if value.candidate else "false",
        value.dispatch_mode,
        "true" if value.serial_review_required else "false",
        "true" if value.completion_gate_required else "false",
        "true" if value.permits_workspace_write else "false",
        "true" if value.parallel_candidate else "false",
    )


def _validate_completion_record(value: object) -> _CompletionAdmissionRecord:
    if type(value) is not _CompletionAdmissionRecord:
        raise _error(
            "completion-record-invalid", "completion admission record type is invalid"
        )
    try:
        if object.__getattribute__(value, "_issuer") is not _COMPLETION_RECORD_ISSUER:
            raise _error(
                "completion-record-invalid",
                "completion admission record issuer is invalid",
            )
        _reference(value.reference, "completion reference")
        for name in (
            "digest",
            "policy_fingerprint",
            "path_claim_digest",
            "resource_claim_digest",
            "routing_digest",
        ):
            _digest(getattr(value, name), f"completion {name}")
        if value.reservation_digest is not None:
            _digest(value.reservation_digest, "completion reservation digest")
        if type(value.reservation_claim_keys) is not tuple or any(
            type(item) is not str for item in value.reservation_claim_keys
        ):
            raise _error(
                "completion-record-invalid", "reservation claim keys are invalid"
            )
        for item in value.reservation_claim_keys:
            _bounded_identity(item, "reservation claim key")
        if tuple(sorted(value.reservation_claim_keys)) != value.reservation_claim_keys:
            raise _error(
                "completion-record-invalid", "reservation claim keys are not canonical"
            )
        if len(set(value.reservation_claim_keys)) != len(value.reservation_claim_keys):
            raise _error(
                "completion-record-invalid", "reservation claim keys are duplicated"
            )
        if value.reservation_digest is None:
            if (
                value.reservation_id is not None
                or value.reservation_claim_keys
                or value.reservation_owner is not None
                or value.reservation_lease_epoch is not None
                or value.reservation_fencing_token is not None
            ):
                raise _error(
                    "completion-record-invalid",
                    "claim-free reservation identity is not empty",
                )
        else:
            _bounded_identity(value.reservation_id, "reservation ID")
            _bounded_identity(value.reservation_owner, "reservation owner")
            if (
                not value.reservation_claim_keys
                or type(value.reservation_lease_epoch) is not int
                or value.reservation_lease_epoch < 0
                or type(value.reservation_fencing_token) is not int
                or value.reservation_fencing_token <= 0
            ):
                raise _error(
                    "completion-record-invalid", "reservation identity is invalid"
                )
        for name in (
            "team_id",
            "task_id",
            "profile_ref",
            "worker_node",
            "reviewer_node",
        ):
            _bounded_identity(getattr(value, name), f"completion {name}")
        _canonical_path(value.workspace, "completion workspace")
        if value.lane not in {TaskLane.NORMAL.value, TaskLane.EXPRESS.value}:
            raise _error("completion-record-invalid", "completion lane is invalid")
        if (
            type(value.candidate) is not bool
            or not value.candidate
            or value.dispatch_mode != DispatchMode.SERIAL.value
            or type(value.serial_review_required) is not bool
            or not value.serial_review_required
            or type(value.completion_gate_required) is not bool
            or not value.completion_gate_required
            or type(value.permits_workspace_write) is not bool
            or not value.permits_workspace_write
            or type(value.parallel_candidate) is not bool
            or value.parallel_candidate
        ):
            raise _error(
                "completion-record-invalid", "completion postcondition is invalid"
            )
        expected = _framed_digest(_completion_parts(value))
        if not _same_text(value.digest, expected):
            raise _error("completion-record-invalid", "completion digest differs")
        if not _same_text(value.reference, f"completion-admission-{value.digest}"):
            raise _error("completion-record-invalid", "completion reference differs")
    except AttributeError as exc:
        raise _error(
            "completion-record-invalid", "completion admission record is malformed"
        ) from exc
    return value


def _completion_postcondition(
    decision: object, *, expected_lane: str
) -> LaneRoutingDecision:
    if type(decision) is not LaneRoutingDecision:
        raise _error("completion-not-eligible", "route result type is invalid")
    if (
        not decision.candidate
        or decision.lane not in {TaskLane.NORMAL, TaskLane.EXPRESS}
        or decision.lane.value != expected_lane
        or decision.dispatch_mode is not DispatchMode.SERIAL
        or decision.serial_review_required is not True
        or decision.completion_gate_required is not True
        or decision.permits_workspace_write is not True
        or decision.parallel_candidate is not False
        or decision.reason_code is not None
    ):
        code = _safe_code(decision.reason_code or "completion-not-eligible")
        raise _error(code, "route is not eligible for completion")
    return decision


def _make_completion_record(
    *,
    snapshot: _CompletionInputSnapshot,
    decision: LaneRoutingDecision,
    reservation: _ReservationResultEvidence | None,
) -> _CompletionAdmissionRecord:
    decision = _completion_postcondition(decision, expected_lane=snapshot.lane)
    try:
        policy_fingerprint = snapshot.policy_fingerprint
        reviewer_node = snapshot.reviewer_node
        if policy_fingerprint is None or reviewer_node is None:
            raise _error("completion-not-eligible", "serial review profile is missing")
        reservation_digest = None if reservation is None else reservation.request_digest
        if reservation_digest is not None:
            _digest(reservation_digest, "reservation digest")
        routing_digest = _framed_digest(
            (
                "completion-routing-v1",
                snapshot.team_id,
                snapshot.task_id,
                snapshot.workspace,
                snapshot.profile_ref,
                policy_fingerprint,
                snapshot.worker_node,
                reviewer_node,
                snapshot.lane,
                snapshot.path_claim_digest,
                snapshot.resource_claim_digest,
                "" if reservation_digest is None else reservation_digest,
                "" if reservation is None else reservation.reservation_id,
                "" if reservation is None else reservation.owner or "",
                ""
                if reservation is None or reservation.lease_epoch is None
                else str(reservation.lease_epoch),
                ""
                if reservation is None or reservation.fencing_token is None
                else str(reservation.fencing_token),
                DispatchMode.SERIAL.value,
                "true" if decision.serial_review_required else "false",
                "true" if decision.completion_gate_required else "false",
                "true" if decision.permits_workspace_write else "false",
            )
        )
        record = object.__new__(_CompletionAdmissionRecord)
        values: dict[str, object] = {
            "reference": "completion-admission-placeholder",
            "digest": "0" * 64,
            "team_id": snapshot.team_id,
            "task_id": snapshot.task_id,
            "workspace": snapshot.workspace,
            "profile_ref": snapshot.profile_ref,
            "policy_fingerprint": policy_fingerprint,
            "worker_node": snapshot.worker_node,
            "reviewer_node": reviewer_node,
            "lane": decision.lane.value,
            "path_claim_digest": snapshot.path_claim_digest,
            "resource_claim_digest": snapshot.resource_claim_digest,
            "routing_digest": routing_digest,
            "reservation_digest": reservation_digest,
            "reservation_id": (
                None if reservation is None else reservation.reservation_id
            ),
            "reservation_claim_keys": (
                () if reservation is None else reservation.claim_keys
            ),
            "reservation_owner": None if reservation is None else reservation.owner,
            "reservation_lease_epoch": (
                None if reservation is None else reservation.lease_epoch
            ),
            "reservation_fencing_token": (
                None if reservation is None else reservation.fencing_token
            ),
            "candidate": decision.candidate,
            "dispatch_mode": DispatchMode.SERIAL.value,
            "serial_review_required": decision.serial_review_required,
            "completion_gate_required": decision.completion_gate_required,
            "permits_workspace_write": decision.permits_workspace_write,
            "parallel_candidate": decision.parallel_candidate,
            "_issuer": _COMPLETION_RECORD_ISSUER,
        }
        for name, item in values.items():
            object.__setattr__(record, name, item)
        digest = _framed_digest(_completion_parts(record))
        object.__setattr__(record, "digest", digest)
        object.__setattr__(record, "reference", f"completion-admission-{digest}")
        return _validate_completion_record(record)
    except PolicyVerificationHandoffError:
        raise
    except _HANDOFF_EXCEPTIONS:
        raise _error(
            "completion-input-invalid", "completion input is not admissible"
        ) from None


def _approval_parts(value: _ApprovalRecord) -> tuple[str, ...]:
    return (
        "policy-verification-approval-record-v1",
        value.approval_ref,
        value.review_ref.reference,
        value.review_digest,
        value.completion_ref.reference,
        value.completion_digest,
        value.bound.approved.authority_digest,
    )


def _validate_approval_record(value: object) -> _ApprovalRecord:
    if type(value) is not _ApprovalRecord:
        raise _error("approval-record-invalid", "approval record type is invalid")
    try:
        if object.__getattribute__(value, "_issuer") is not _APPROVAL_RECORD_ISSUER:
            raise _error("approval-record-invalid", "approval record issuer is invalid")
        _reference(value.approval_ref, "approval ref")
        _digest(value.digest, "approval record digest")
        _validate_review_authority_ref(value.review_ref)
        _validate_completion_admission_ref(value.completion_ref)
        _digest(value.review_digest, "approval review digest")
        _digest(value.completion_digest, "approval completion digest")
        if not _same_text(value.review_ref.digest, value.review_digest):
            raise _error("approval-record-invalid", "approval review digest differs")
        if not _same_text(value.completion_ref.digest, value.completion_digest):
            raise _error(
                "approval-record-invalid", "approval completion digest differs"
            )
        _gate._validate_bound_approval(value.bound)
        if not _same_text(value.approval_ref, value.bound.approval_ref):
            raise _error("approval-record-invalid", "approval ref differs")
        expected = _framed_digest(_approval_parts(value))
        if not _same_text(value.digest, expected):
            raise _error("approval-record-invalid", "approval record digest differs")
    except AttributeError as exc:
        raise _error("approval-record-invalid", "approval record is malformed") from exc
    return value


def _same_review_record(left: object, right: object) -> bool:
    try:
        first = _validate_review_record(left)
        second = _validate_review_record(right)
    except Exception:  # noqa: BLE001 - malformed Store records compare unequal
        return False
    return _review_parts(first) == _review_parts(second) and _same_text(
        first.digest, second.digest
    )


def _same_completion_record(left: object, right: object) -> bool:
    try:
        first = _validate_completion_record(left)
        second = _validate_completion_record(right)
    except Exception:  # noqa: BLE001 - malformed Store records compare unequal
        return False
    return _completion_parts(first) == _completion_parts(second) and _same_text(
        first.digest, second.digest
    )


def _same_approval_record(left: object, right: object) -> bool:
    try:
        first = _validate_approval_record(left)
        second = _validate_approval_record(right)
    except Exception:  # noqa: BLE001 - malformed Store records compare unequal
        return False
    return _approval_parts(first) == _approval_parts(second) and _same_text(
        first.digest, second.digest
    )


def _required_store_methods(store: object) -> None:
    for name in (
        "save_review_authority",
        "read_review_authority",
        "save_completion_admission",
        "read_completion_admission",
        "save_approval",
        "read_approval",
        "state_port",
    ):
        if not callable(getattr(store, name, None)):
            raise _error(
                "store-port-invalid", "policy verification Store port is incomplete"
            )


class PolicyVerificationHandoff:
    """Deep owner-ref issuer, composer, and #51 approval resolver."""

    __slots__ = ("_store",)

    def __init__(self, store: _PolicyVerificationRegistryPort) -> None:
        _required_store_methods(store)
        self._store = store

    def _persist_exact(
        self,
        *,
        record: object,
        save_name: str,
        read_name: str,
        same: Callable[[object, object], bool],
        conflict_code: str,
        recovery_code: str,
    ) -> object:
        save = getattr(self._store, save_name)
        read = getattr(self._store, read_name)
        save_failed = False
        try:
            save(record)
        except Exception:  # noqa: BLE001 - injected Store text must not escape
            save_failed = True
        try:
            observed = read(
                getattr(record, "reference", getattr(record, "approval_ref", None))
            )
        except Exception:  # noqa: BLE001 - readback uncertainty is recovery
            raise PolicyVerificationRecoveryRequired(recovery_code) from None
        if not same(record, observed):
            if save_failed:
                raise PolicyVerificationRecoveryRequired(recovery_code) from None
            raise _error(conflict_code, "authority readback differs")
        return observed

    def save_authority(
        self, update: ReviewPolicyUpdate, policy: SerialReviewPolicy
    ) -> ReviewAuthorityRef:
        record = _make_review_record(update, policy)
        self._persist_exact(
            record=record,
            save_name="save_review_authority",
            read_name="read_review_authority",
            same=_same_review_record,
            conflict_code="review-authority-conflict",
            recovery_code="review-authority-response-loss",
        )
        result = _issue_review_authority_ref(record.reference, record.digest)
        with _HANDOFF_REVIEW_REF_OWNERS_LOCK:
            _HANDOFF_REVIEW_REF_OWNERS[result] = (self, self._store)
        return result

    def issue_completion_admission(
        self,
        task: TaskSpec,
        *,
        path_policy: PathClaimPolicy,
        path_mutation: PathMutation,
        path_observations: tuple[PathObservation, ...],
        resource_claims: tuple[ResourceClaimPolicy, ...],
        known_keys: frozenset[ResourceKey],
        profile: LaneProfileBinding,
        reservation_port: ResourceReservationPort,
        reservation_id: str,
        reservation_authority: ResourceReservationAuthority | None,
    ) -> CompletionAdmissionRef:
        before = _completion_input_snapshot(
            task=task,
            path_policy=path_policy,
            path_mutation=path_mutation,
            path_observations=path_observations,
            resource_claims=resource_claims,
            known_keys=known_keys,
            profile=profile,
            reservation_id=reservation_id,
            reservation_authority=reservation_authority,
        )
        capture: _CapturedReservationPort | None
        route_port: ResourceReservationPort
        if callable(getattr(reservation_port, "reserve", None)):
            capture = _CapturedReservationPort(reservation_port)
            route_port = capture
        else:
            capture = None
            route_port = reservation_port
        try:
            decision = route_task(
                task,
                path_policy=path_policy,
                path_mutation=path_mutation,
                path_observations=path_observations,
                resource_claims=resource_claims,
                known_keys=known_keys,
                profile=profile,
                reservation_port=route_port,
                reservation_id=reservation_id,
                reservation_authority=reservation_authority,
            )
        except Exception:  # noqa: BLE001 - route/port text must not escape
            raise _error(
                "completion-route-invalid", "completion route is not admissible"
            ) from None
        after = _completion_input_snapshot(
            task=task,
            path_policy=path_policy,
            path_mutation=path_mutation,
            path_observations=path_observations,
            resource_claims=resource_claims,
            known_keys=known_keys,
            profile=profile,
            reservation_id=reservation_id,
            reservation_authority=reservation_authority,
        )
        if after != before:
            raise _error("completion-input-drift", "completion input changed")
        decision = _completion_postcondition(decision, expected_lane=before.lane)
        if capture is None:
            raise _error("completion-route-invalid", "reservation port is invalid")
        reservation = _validate_captured_reservation(
            decision=decision,
            snapshot=before,
            capture=capture,
        )
        record = _make_completion_record(
            snapshot=before,
            decision=decision,
            reservation=reservation,
        )
        self._persist_exact(
            record=record,
            save_name="save_completion_admission",
            read_name="read_completion_admission",
            same=_same_completion_record,
            conflict_code="completion-admission-conflict",
            recovery_code="completion-admission-response-loss",
        )
        result = _issue_completion_admission_ref(record.reference, record.digest)
        with _HANDOFF_COMPLETION_REF_OWNERS_LOCK:
            _HANDOFF_COMPLETION_REF_OWNERS[result] = (
                self,
                self._store,
                result.reference,
                result.digest,
            )
        return result

    def _bind_review_authority(
        self,
        update: ReviewPolicyUpdate,
        policy: SerialReviewPolicy,
        review_ref: ReviewAuthorityRef,
    ) -> _ReviewAuthorityBinding:
        """Bind one actual policy update to its owner-issued review ref.

        This is a read-only consumer seam for the schema-4 review producer.
        It deliberately does not issue a ref or invoke any route/composition
        operation.  The owner record is read once during binding and again by
        :func:`_validate_review_authority_binding` before consumption.
        """

        try:
            if (
                type(update) is not ReviewPolicyUpdate
                or type(policy) is not SerialReviewPolicy
                or type(review_ref) is not ReviewAuthorityRef
            ):
                raise _binding_failure()
            validate_policy_update(update, policy)
            expected = _make_review_record(update, policy)
            observed = self._read_review(review_ref)
            if not _same_review_record(expected, observed):
                raise _binding_failure()
            expected_snapshot = _review_record_snapshot(expected)
            observed_snapshot = _review_record_snapshot(observed)
            if expected_snapshot != observed_snapshot:
                raise _binding_failure()
            binding = object.__new__(_ReviewAuthorityBinding)
            for name, value in (
                ("update", update),
                ("policy", policy),
                ("review_ref", review_ref),
                ("_issuer", _REVIEW_AUTHORITY_BINDING_ISSUER),
            ):
                object.__setattr__(binding, name, value)
            state = _ReviewAuthorityBindingState(
                owner=self,
                store=self._store,
                update=update,
                policy=policy,
                review_ref=review_ref,
                update_snapshot=_binding_value_snapshot(update),
                policy_snapshot=_binding_value_snapshot(policy),
                record_snapshot=observed_snapshot,
            )
            with _REVIEW_AUTHORITY_BINDINGS_LOCK:
                _REVIEW_AUTHORITY_BINDINGS[binding] = state
            return binding
        except PolicyVerificationHandoffError:
            raise _binding_failure() from None
        except Exception:  # noqa: BLE001 - owner/input text must not escape
            raise _binding_failure() from None

    def _read_review(self, ref: ReviewAuthorityRef) -> _ReviewAuthorityRecord:
        try:
            _validate_review_authority_ref(ref)
            with _HANDOFF_REVIEW_REF_OWNERS_LOCK:
                owner = _HANDOFF_REVIEW_REF_OWNERS.get(ref)
            if owner is None or owner[0] is not self or owner[1] is not self._store:
                raise _error(
                    "review-authority-invalid",
                    "review authority owner differs",
                )
            value = self._store.read_review_authority(ref.reference)
            record = _validate_review_record(value)
        except Exception:  # noqa: BLE001 - injected Store text must not escape
            raise _error(
                "review-authority-invalid", "review authority is unavailable"
            ) from None
        if not _same_text(ref.digest, record.digest):
            raise _error("review-authority-invalid", "review authority digest differs")
        return record

    def _validate_completion_owner(self, ref: CompletionAdmissionRef) -> None:
        try:
            _validate_completion_admission_ref(ref)
            with _HANDOFF_COMPLETION_REF_OWNERS_LOCK:
                owner = _HANDOFF_COMPLETION_REF_OWNERS.get(ref)
            if (
                owner is None
                or owner[0] is not self
                or owner[1] is not self._store
                or owner[2] != ref.reference
                or owner[3] != ref.digest
            ):
                raise _error(
                    "completion-admission-invalid",
                    "completion admission owner differs",
                )
        except Exception:  # noqa: BLE001 - injected ref text must not escape
            raise _error(
                "completion-admission-invalid", "completion admission is unavailable"
            ) from None

    def _read_completion(
        self, ref: CompletionAdmissionRef
    ) -> _CompletionAdmissionRecord:
        try:
            self._validate_completion_owner(ref)
            value = self._store.read_completion_admission(ref.reference)
            record = _validate_completion_record(value)
        except Exception:  # noqa: BLE001 - injected Store text must not escape
            raise _error(
                "completion-admission-invalid", "completion admission is unavailable"
            ) from None
        if not _same_text(ref.digest, record.digest):
            raise _error(
                "completion-admission-invalid", "completion admission digest differs"
            )
        return record

    @staticmethod
    def _validate_overlap(
        review: _ReviewAuthorityRecord, completion: _CompletionAdmissionRecord
    ) -> None:
        overlap = (
            (review.team_id, completion.team_id),
            (review.task_id, completion.task_id),
            (review.workspace, completion.workspace),
            (review.profile_ref, completion.profile_ref),
            (review.policy_fingerprint, completion.policy_fingerprint),
            (review.worker_node, completion.worker_node),
            (review.reviewer_node, completion.reviewer_node),
            (review.lane, completion.lane),
        )
        if any(not _same_text(left, right) for left, right in overlap):
            raise _error("owner-binding-mismatch", "owner authority overlap differs")
        if (
            review.phase != TaskPhase.APPROVED.value
            or review.event_kind != PolicyProjectionKind.REVIEW_DECISION.value
            or review.decision_kind != ReviewDecisionKind.APPROVED.value
            or review.target_head is None
            or review.target_tree_digest is None
            or review.claim_ref is None
        ):
            raise _error("review-not-approved", "review authority is not approved")

    @staticmethod
    def _approval_seed(
        review: _ReviewAuthorityRecord, completion: _CompletionAdmissionRecord
    ) -> str:
        return _framed_digest(
            (
                "policy-verification-approval-v1",
                review.reference,
                review.digest,
                completion.reference,
                completion.digest,
                review.run_id,
                review.task_id,
                review.dispatch_id,
                review.attempt_id,
                review.sequence.__str__(),
            )
        )

    @staticmethod
    def _approved_matches_owners(
        approved: _gate.ApprovedReview,
        review: _ReviewAuthorityRecord,
        completion: _CompletionAdmissionRecord,
    ) -> bool:
        text_fields = (
            (approved.run_id, review.run_id),
            (approved.team_id, review.team_id),
            (approved.workspace, review.workspace),
            (approved.task_id, review.task_id),
            (approved.dispatch_id, review.dispatch_id),
            (approved.attempt_id, review.attempt_id),
            (approved.worker_node, review.worker_node),
            (approved.reviewer_node, review.reviewer_node),
            (approved.worker_terminal_id, review.worker_terminal_id),
            (approved.reviewer_terminal_id, review.reviewer_terminal_id),
            (approved.target_head, review.target_head),
            (approved.target_tree_digest, review.target_tree_digest),
            (approved.claim_ref, review.claim_ref),
            (approved.policy_fingerprint, review.policy_fingerprint),
            (approved.profile_ref, review.profile_ref),
            (approved.routing_digest, completion.routing_digest),
        )
        if any(not _same_text(left, right) for left, right in text_fields):
            return False
        if approved.review_round != review.review_round:
            return False
        if approved.approval_sequence != review.sequence:
            return False
        if approved.routing_lane.value != review.lane:
            return False
        if completion.reservation_digest is None:
            return approved.reservation_digest is None
        return _same_text(approved.reservation_digest, completion.reservation_digest)

    def compose(
        self,
        review_ref: ReviewAuthorityRef,
        completion_ref: CompletionAdmissionRef,
    ) -> _gate.ApprovalRef:
        review = self._read_review(review_ref)
        completion = self._read_completion(completion_ref)
        self._validate_overlap(review, completion)
        seed = self._approval_seed(review, completion)
        approval_ref = _gate.ApprovalRef(f"approval-{seed}")
        verification_id = _gate.VerificationId(
            f"verification-{_framed_digest(('verification-id-v1', seed))}"
        )
        approved = _gate._make_approved(
            run_id=review.run_id,
            team_id=review.team_id,
            workspace=review.workspace,
            task_id=review.task_id,
            dispatch_id=review.dispatch_id,
            attempt_id=review.attempt_id,
            worker_node=review.worker_node,
            reviewer_node=review.reviewer_node,
            worker_terminal_id=review.worker_terminal_id,
            reviewer_terminal_id=review.reviewer_terminal_id,
            review_round=review.review_round,
            target_head=review.target_head,
            target_tree_digest=review.target_tree_digest,
            claim_ref=review.claim_ref,
            policy_fingerprint=review.policy_fingerprint,
            routing_lane=TaskLane(review.lane),
            approval_ref=approval_ref,
            approval_sequence=review.sequence,
            profile_ref=review.profile_ref,
            verification_id=verification_id,
            routing_digest=_gate.ReceiptDigest(completion.routing_digest),
            reservation_digest=(
                None
                if completion.reservation_digest is None
                else _gate.ReceiptDigest(completion.reservation_digest)
            ),
        )
        bound = _gate._make_bound_approval(approval_ref, approved)
        record = object.__new__(_ApprovalRecord)
        values: dict[str, object] = {
            "approval_ref": str(approval_ref),
            "digest": "0" * 64,
            "review_ref": review_ref,
            "completion_ref": completion_ref,
            "review_digest": review.digest,
            "completion_digest": completion.digest,
            "bound": bound,
            "_issuer": _APPROVAL_RECORD_ISSUER,
        }
        for name, item in values.items():
            object.__setattr__(record, name, item)
        object.__setattr__(record, "digest", _framed_digest(_approval_parts(record)))
        _validate_approval_record(record)
        self._persist_exact(
            record=record,
            save_name="save_approval",
            read_name="read_approval",
            same=_same_approval_record,
            conflict_code="approval-conflict",
            recovery_code="approval-response-loss",
        )
        return approval_ref

    def resolve(self, approval_ref: _gate.ApprovalRef) -> _gate._BoundApproval:
        if type(approval_ref) is not str:
            raise _error("approval-invalid", "approval ref type is invalid")
        try:
            stored = self._store.read_approval(approval_ref)
            record = _validate_approval_record(stored)
            review = self._read_review(record.review_ref)
            completion = self._read_completion(record.completion_ref)
            self._validate_overlap(review, completion)
        except PolicyVerificationHandoffError:
            raise
        except Exception:  # noqa: BLE001 - injected Store text must not escape
            raise _error("approval-invalid", "approval is unavailable") from None
        if (
            not _same_text(record.review_digest, review.digest)
            or not _same_text(record.completion_digest, completion.digest)
            or not _same_text(record.approval_ref, approval_ref)
            or not self._approved_matches_owners(
                record.bound.approved, review, completion
            )
        ):
            raise _error("approval-invalid", "approval owner binding differs")
        expected_seed = self._approval_seed(review, completion)
        if not _same_text(record.approval_ref, f"approval-{expected_seed}"):
            raise _error("approval-invalid", "approval identity differs")
        expected_verification_id = "verification-" + _framed_digest(
            ("verification-id-v1", expected_seed)
        )
        if not _same_text(
            record.bound.approved.verification_id, expected_verification_id
        ):
            raise _error("approval-invalid", "verification identity differs")
        return record.bound

    def state_port(self) -> _gate.VerificationStatePort:
        try:
            state = self._store.state_port()
        except Exception:  # noqa: BLE001 - injected state-port text must not escape
            raise _error(
                "state-port-invalid", "verification state port is unavailable"
            ) from None
        for name in (
            "prepare_once",
            "begin_effect_once",
            "read",
            "status",
            "record_receipt_once",
            "apply_terminal_once",
        ):
            if not callable(getattr(state, name, None)):
                raise _error(
                    "state-port-invalid", "verification state port is incomplete"
                )
        return state


def _validate_review_authority_binding(
    binding: object,
) -> _ReviewAuthorityEvidence:
    """Revalidate a process-local binding against current owner readback.

    A binding is useful only while its exact update, policy, ref, owner Store,
    and owner record remain the values captured at issuance.  Every check is
    repeated here so a consumer cannot treat the return-only wrapper as a
    durable authority or bypass the owner's current readback.
    """

    try:
        if type(binding) is not _ReviewAuthorityBinding:
            raise _binding_failure()
        if object.__getattribute__(binding, "_issuer") is not (
            _REVIEW_AUTHORITY_BINDING_ISSUER
        ):
            raise _binding_failure()
        update = object.__getattribute__(binding, "update")
        policy = object.__getattribute__(binding, "policy")
        review_ref = object.__getattribute__(binding, "review_ref")
        with _REVIEW_AUTHORITY_BINDINGS_LOCK:
            state = _REVIEW_AUTHORITY_BINDINGS.get(binding)
        if state is None:
            raise _binding_failure()
        if (
            state.update is not update
            or state.policy is not policy
            or state.review_ref is not review_ref
        ):
            raise _binding_failure()
        if (
            type(update) is not ReviewPolicyUpdate
            or type(policy) is not SerialReviewPolicy
            or type(review_ref) is not ReviewAuthorityRef
        ):
            raise _binding_failure()
        if _binding_value_snapshot(update) != state.update_snapshot:
            raise _binding_failure()
        if _binding_value_snapshot(policy) != state.policy_snapshot:
            raise _binding_failure()
        _validate_review_authority_ref(review_ref)
        if object.__getattribute__(state.owner, "_store") is not state.store:
            raise _binding_failure()
        expected = _make_review_record(update, policy)
        observed = PolicyVerificationHandoff._read_review(state.owner, review_ref)
        if not _same_review_record(expected, observed):
            raise _binding_failure()
        if _review_record_snapshot(observed) != state.record_snapshot:
            raise _binding_failure()
        return _ReviewAuthorityEvidence(state.owner, update, policy, review_ref)
    except Exception:  # noqa: BLE001 - binding/owner text must not escape
        raise _binding_failure() from None


__all__ = [
    "PolicyVerificationHandoff",
    "PolicyVerificationHandoffError",
    "PolicyVerificationRecoveryRequired",
]
