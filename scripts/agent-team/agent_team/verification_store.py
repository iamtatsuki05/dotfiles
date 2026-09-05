"""Package-private #82 verification authority, capture, and digest seams.

Context and owner objects are process-local admission capabilities.  Approval
snapshots and record mappings are canonical wire values, never authority, and
must be validated against Store provenance before local objects are issued.
"""

from __future__ import annotations

import hashlib
import re
import struct
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from threading import RLock
from typing import Final, NoReturn, Self, SupportsIndex, cast
from weakref import ReferenceType, WeakKeyDictionary, ref

from . import _review_workflow_store as _review_workflow
from . import path_resource_policy as _path_resource
from . import policy_verification_handoff as _handoff
from . import review_policy as _review
from . import task_policy as _task
from . import task_verification_ledger as _ledger
from . import verification_gate as _gate
from . import workflow_store as _workflow

ApprovalBindingSnapshotV1 = _ledger.ApprovalBindingSnapshotV1

_RECORD_DIGEST_DOMAIN: Final = b"agent-team/verification-record/v1\0"
_MIN_INT64: Final = -(2**63)
_MAX_INT64: Final = 2**63 - 1
_BARE_DIGEST: Final = re.compile(r"[0-9a-f]{64}\Z")
_WRAPPED_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONTEXT_OWNER_DOMAIN: Final = b"agent-team/verification-context-owner/v1\0"
_CONTEXT_REVISION_DOMAIN: Final = b"agent-team/verification-context-revision/v1\0"
_RECEIPT_REF_DOMAIN: Final = b"agent-team/verification-receipt-ref/v1\0"
_BOUNDARY_EXCEPTION: Final[type[Exception]] = Exception
_ERROR_ISSUER: Final = object()

VERIFICATION_ACTOR: Final = "verification-store-adapter-v1"
_REQUEST_WRAPPER_DOMAIN: Final = b"agent-team/workflow-verification-request/v1\0"
_EVIDENCE_WRAPPER_DOMAINS: Final = {
    "prepare": b"agent-team/workflow-verification-prepare-evidence/v1\0",
    "receipt": b"agent-team/workflow-verification-receipt-evidence/v1\0",
    "terminal": b"agent-team/workflow-verification-terminal-evidence/v1\0",
    "unknown": b"agent-team/workflow-verification-unknown-evidence/v1\0",
}
_UNKNOWN_CODES: Final = frozenset(
    {
        "effect-response-loss",
        "runner-response-loss",
        "runner-response-invalid",
        "cleanup-unknown",
        "snapshot-drift",
        "receipt-response-loss",
        "receipt-commit-unknown",
        "effect-fence-unknown",
    }
)


class VerificationStage(str, Enum):
    """Closed workflow-visible stages written by the #82 Store adapter."""

    PREPARE = "prepare"
    RECEIPT = "receipt"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


class VerificationStoreError(ValueError):
    """A bounded verification Store input is not admissible."""

    retry_cleanup: Callable[[], None] | None
    _verification_cleanup_capability: bool
    _verification_reason_code: str | None
    _verification_reason_issuer: object | None
    _origin: object | None

    def __init__(self, message: str) -> None:
        self.retry_cleanup = None
        self._verification_cleanup_capability = False
        self._verification_reason_code = None
        self._verification_reason_issuer = None
        self._origin = None
        super().__init__(message)


_INTERNAL_CONTEXT_ERRORS: Final = WeakKeyDictionary[VerificationStoreError, object]()
_INTERNAL_CONTEXT_ERRORS_LOCK: Final = RLock()


@dataclass(frozen=True, slots=True, init=False)
class VerificationContextSeed:
    """Store-issued, immutable current-pair observation for one capture."""

    root_key: str
    run_id: str
    main_terminal_id: str
    worker_terminal_id: str
    reviewer_terminal_id: str
    consumer_generation: int
    workflow_sequence: int
    workflow_checkpoint_digest: str
    task_state: _task.TaskPolicyStateV4
    task_state_digest: str
    task_sequence: int
    effect_owner: str

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("verification context is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("verification context is return-only")


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class VerificationEffectOwner:
    """Process-local mutation capability issued by one open Store."""

    owner_id: str

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("verification effect owner is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("verification effect owner is return-only")

    def __repr__(self) -> str:
        return "<VerificationEffectOwner opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("verification effect owner cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("verification effect owner cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("verification effect owner cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("verification effect owner cannot be pickled")


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class StagedStoreApprovalAdmission:
    """One-use admission containing a live owner result before prepare."""

    approval_ref: _gate.ApprovalRef

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("staged approval admission is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("staged approval admission is return-only")

    def __repr__(self) -> str:
        return "<StagedStoreApprovalAdmission opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("staged approval admission cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("staged approval admission cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("staged approval admission cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("staged approval admission cannot be pickled")

    def resolve(self, approval_ref: _gate.ApprovalRef) -> _gate._BoundApproval:
        """Consume the exact staged live value without another owner call."""

        with _STAGED_ADMISSIONS_LOCK:
            state = _STAGED_ADMISSIONS.get(self)
        if state is None or state.consumed:
            raise VerificationStoreError("staged approval is unavailable")
        store = state.store_ref()
        if store is None:
            raise VerificationStoreError("staged approval generation differs")
        with _store_generation_guard(store), _STAGED_ADMISSIONS_LOCK:
            state = _STAGED_ADMISSIONS.get(self)
            if state is None or state.consumed:
                raise VerificationStoreError("staged approval is unavailable")
            if type(approval_ref) is not str or approval_ref != state.approval_ref:
                raise VerificationStoreError("staged approval reference differs")
            if not _live_store_generation(
                store,
                state.store_marker,
                state.open_generation,
            ):
                raise VerificationStoreError("staged approval generation differs")
            with _VERIFICATION_EFFECT_OWNERS_LOCK:
                owner_state = _VERIFICATION_EFFECT_OWNERS.get(state.owner)
            if (
                owner_state is None
                or owner_state.store_ref() is not store
                or owner_state.store_marker is not state.store_marker
                or owner_state.open_generation is not state.open_generation
                or state.owner.owner_id != owner_state.owner_id
                or state.snapshot.effect_owner != owner_state.context_snapshot[-1]
                or state.snapshot.approval_ref != state.approval_ref
            ):
                raise VerificationStoreError("staged approval provenance differs")
            try:
                _gate._validate_bound_approval(state.bound)
                state.snapshot.__post_init__()
                returned_bytes = _ledger.encode_approval_binding_snapshot(
                    state.returned_snapshot
                )
                raw = _ledger.encode_approval_binding_snapshot(state.snapshot)
                if (
                    returned_bytes != state.snapshot_bytes
                    or raw != state.snapshot_bytes
                    or _ledger.decode_approval_binding_snapshot(raw) != state.snapshot
                    or state.bound.approval_ref != state.snapshot.approval_ref
                    or _ledger._projection_from_approved(state.bound.approved)
                    != state.snapshot.approved_review
                    or state.bound.approved.authority_digest
                    != state.snapshot.approval_digest
                ):
                    raise VerificationStoreError("staged approval snapshot differs")
            except VerificationStoreError:
                raise
            except _BOUNDARY_EXCEPTION:
                raise VerificationStoreError("staged approval is invalid") from None
            state.consumed = True
            return state.bound


@dataclass(frozen=True, slots=True)
class _VerificationEffectOwnerState:
    store_ref: ReferenceType[object]
    store_marker: object
    open_generation: object
    read_function: object
    context: VerificationContextSeed
    context_baseline: VerificationContextSeed
    context_snapshot: tuple[object, ...]
    revision: int
    revision_digest: str
    owner_id: str
    binding: object | None
    handoff: _handoff.PolicyVerificationHandoff | None
    review_ref: _review.ReviewAuthorityRef | None


@dataclass(slots=True)
class _StagedAdmissionState:
    store_ref: ReferenceType[object]
    store_marker: object
    open_generation: object
    owner: VerificationEffectOwner
    snapshot: _ledger.ApprovalBindingSnapshotV1
    returned_snapshot: _ledger.ApprovalBindingSnapshotV1
    snapshot_bytes: bytes
    approval_ref: _gate.ApprovalRef
    bound: _gate._BoundApproval
    consumed: bool = False


_VERIFICATION_EFFECT_OWNERS: Final = WeakKeyDictionary[
    VerificationEffectOwner, _VerificationEffectOwnerState
]()
_VERIFICATION_EFFECT_OWNERS_LOCK: Final = RLock()
_STAGED_ADMISSIONS: Final = WeakKeyDictionary[
    StagedStoreApprovalAdmission, _StagedAdmissionState
]()
_STAGED_ADMISSIONS_LOCK: Final = RLock()


@dataclass(frozen=True, slots=True)
class _RegisteredVerificationStore:
    read_context_function: object
    prepare_function: object
    effect_function: object
    reentry_function: object
    receipt_function: object
    lifecycle_read_function: object
    terminal_function: object
    unknown_function: object


_REGISTERED_VERIFICATION_STORES: Final = WeakKeyDictionary[
    object, _RegisteredVerificationStore
]()
_REGISTERED_VERIFICATION_STORES_LOCK: Final = RLock()
_VERIFICATION_CLEANUP_CAPABILITY_TYPE: type[object] | None = None
_VERIFICATION_CLEANUP_CAPABILITY_VALIDATOR: object | None = None
_VERIFICATION_COMMIT_UNKNOWN_ERROR_TYPE: type[BaseException] | None = None
_REGISTERED_METHOD_NAMES: Final = {
    "_verification_read_context_function": "read_context_function",
    "_verification_prepare_function": "prepare_function",
    "_verification_effect_function": "effect_function",
    "_verification_reentry_function": "reentry_function",
    "_verification_receipt_function": "receipt_function",
    "_verification_lifecycle_read_function": "lifecycle_read_function",
    "_verification_terminal_function": "terminal_function",
    "_verification_unknown_function": "unknown_function",
}


def _register_verification_store(
    store: object,
    *,
    read_context_function: object,
    prepare_function: object,
    effect_function: object,
    reentry_function: object,
    receipt_function: object,
    lifecycle_read_function: object,
    terminal_function: object,
    unknown_function: object,
    cleanup_capability_type: type[object],
    cleanup_capability_validator: object,
    commit_unknown_error_type: type[BaseException],
) -> None:
    global _VERIFICATION_CLEANUP_CAPABILITY_TYPE
    global _VERIFICATION_CLEANUP_CAPABILITY_VALIDATOR
    global _VERIFICATION_COMMIT_UNKNOWN_ERROR_TYPE
    functions = (
        read_context_function,
        prepare_function,
        effect_function,
        reentry_function,
        receipt_function,
        lifecycle_read_function,
        terminal_function,
        unknown_function,
        cleanup_capability_validator,
    )
    if any(not callable(function) for function in functions):
        raise _context_error("verification Store registration is incomplete")
    if type(cleanup_capability_type) is not type:
        raise _context_error("verification cleanup capability type differs")
    if type(commit_unknown_error_type) is not type:
        raise _context_error("verification commit-unknown type differs")
    with _REGISTERED_VERIFICATION_STORES_LOCK:
        if (
            _VERIFICATION_CLEANUP_CAPABILITY_TYPE is not None
            and _VERIFICATION_CLEANUP_CAPABILITY_TYPE is not cleanup_capability_type
        ):
            raise _context_error("verification cleanup capability type changed")
        _VERIFICATION_CLEANUP_CAPABILITY_TYPE = cleanup_capability_type
        if (
            _VERIFICATION_CLEANUP_CAPABILITY_VALIDATOR is not None
            and _VERIFICATION_CLEANUP_CAPABILITY_VALIDATOR
            is not cleanup_capability_validator
        ):
            raise _context_error("verification cleanup validator changed")
        _VERIFICATION_CLEANUP_CAPABILITY_VALIDATOR = cleanup_capability_validator
        if (
            _VERIFICATION_COMMIT_UNKNOWN_ERROR_TYPE is not None
            and _VERIFICATION_COMMIT_UNKNOWN_ERROR_TYPE is not commit_unknown_error_type
        ):
            raise _context_error("verification commit-unknown type changed")
        _VERIFICATION_COMMIT_UNKNOWN_ERROR_TYPE = commit_unknown_error_type
        _REGISTERED_VERIFICATION_STORES[store] = _RegisteredVerificationStore(
            read_context_function=read_context_function,
            prepare_function=prepare_function,
            effect_function=effect_function,
            reentry_function=reentry_function,
            receipt_function=receipt_function,
            lifecycle_read_function=lifecycle_read_function,
            terminal_function=terminal_function,
            unknown_function=unknown_function,
        )


def _invalidate_verification_store(
    store: object,
    store_marker: object,
    open_generation: object,
) -> None:
    """Invalidate only capabilities issued by one exact Store generation."""

    with _VERIFICATION_EFFECT_OWNERS_LOCK:
        expired_owners = tuple(
            owner
            for owner, state in _VERIFICATION_EFFECT_OWNERS.items()
            if state.store_ref() is store
            and state.store_marker is store_marker
            and state.open_generation is open_generation
        )
        for owner in expired_owners:
            del _VERIFICATION_EFFECT_OWNERS[owner]
    with _STAGED_ADMISSIONS_LOCK:
        expired_admissions = tuple(
            admission
            for admission, state in _STAGED_ADMISSIONS.items()
            if state.store_ref() is store
            and state.store_marker is store_marker
            and state.open_generation is open_generation
        )
        for admission in expired_admissions:
            del _STAGED_ADMISSIONS[admission]
    with _STORE_VERIFICATION_ADAPTERS_LOCK:
        expired_adapters = tuple(
            adapter
            for adapter, state in _STORE_VERIFICATION_ADAPTERS.items()
            if state.store_ref() is store
            and state.store_marker is store_marker
            and state.open_generation is open_generation
        )
        for adapter in expired_adapters:
            del _STORE_VERIFICATION_ADAPTERS[adapter]
    with _ISSUED_VERIFICATION_EFFECTS_LOCK:
        expired_effects = tuple(
            effect_key
            for effect_key, state in _ISSUED_VERIFICATION_EFFECTS.items()
            if state.store_ref() is store
            and state.store_marker is store_marker
            and state.open_generation is open_generation
        )
        for effect_key in expired_effects:
            del _ISSUED_VERIFICATION_EFFECTS[effect_key]
    with _VERIFICATION_PREPARE_REQUESTS_LOCK:
        expired_requests = tuple(
            prepare_request
            for prepare_request, state in _VERIFICATION_PREPARE_REQUESTS.items()
            if state.store_ref() is store
            and state.store_marker is store_marker
            and state.open_generation is open_generation
        )
        for prepare_request in expired_requests:
            del _VERIFICATION_PREPARE_REQUESTS[prepare_request]
    with _VERIFICATION_EFFECT_BEGIN_REQUESTS_LOCK:
        expired_effect_requests = tuple(
            effect_request
            for effect_request, state in _VERIFICATION_EFFECT_BEGIN_REQUESTS.items()
            if state.store_ref() is store
            and state.store_marker is store_marker
            and state.open_generation is open_generation
        )
        for effect_request in expired_effect_requests:
            del _VERIFICATION_EFFECT_BEGIN_REQUESTS[effect_request]
    with _REGISTERED_VERIFICATION_STORES_LOCK:
        if _REGISTERED_VERIFICATION_STORES.get(store) is not None:
            del _REGISTERED_VERIFICATION_STORES[store]
    with _VERIFICATION_RECEIPT_REQUESTS_LOCK:
        expired_receipt_requests = tuple(
            receipt_request
            for receipt_request, state in _VERIFICATION_RECEIPT_REQUESTS.items()
            if state.store_ref() is store
            and state.store_marker is store_marker
            and state.open_generation is open_generation
        )
        for receipt_request in expired_receipt_requests:
            del _VERIFICATION_RECEIPT_REQUESTS[receipt_request]
    with _VERIFICATION_TERMINAL_REQUESTS_LOCK:
        expired_terminal_requests = tuple(
            terminal_request
            for terminal_request, state in _VERIFICATION_TERMINAL_REQUESTS.items()
            if state.store_ref() is store
            and state.store_marker is store_marker
            and state.open_generation is open_generation
        )
        for terminal_request in expired_terminal_requests:
            del _VERIFICATION_TERMINAL_REQUESTS[terminal_request]
    with _VERIFICATION_UNKNOWN_REQUESTS_LOCK:
        expired_unknown_requests = tuple(
            unknown_request
            for unknown_request, state in _VERIFICATION_UNKNOWN_REQUESTS.items()
            if state.store_ref() is store
            and state.store_marker is store_marker
            and state.open_generation is open_generation
        )
        for unknown_request in expired_unknown_requests:
            del _VERIFICATION_UNKNOWN_REQUESTS[unknown_request]


def _context_error(message: str) -> VerificationStoreError:
    error = VerificationStoreError(message)
    error._origin = _ERROR_ISSUER
    with _INTERNAL_CONTEXT_ERRORS_LOCK:
        _INTERNAL_CONTEXT_ERRORS[error] = _ERROR_ISSUER
    return error


def _is_context_error(error: VerificationStoreError) -> bool:
    with _INTERNAL_CONTEXT_ERRORS_LOCK:
        return _INTERNAL_CONTEXT_ERRORS.get(error) is _ERROR_ISSUER


def _context_boundary_error(
    message: str,
    source: BaseException,
    *,
    commit_unknown_reason: str | None = None,
) -> VerificationStoreError:
    error = _context_error(message)
    if (
        commit_unknown_reason is not None
        and commit_unknown_reason in _UNKNOWN_CODES
        and _VERIFICATION_COMMIT_UNKNOWN_ERROR_TYPE is not None
        and type(source) is _VERIFICATION_COMMIT_UNKNOWN_ERROR_TYPE
    ):
        error._verification_reason_code = commit_unknown_reason
        error._verification_reason_issuer = _gate._STATE_REASON_ISSUER
    try:
        cleanup_capability = object.__getattribute__(source, "_cleanup_capability")
        retry_cleanup = object.__getattribute__(source, "retry_cleanup")
    except _BOUNDARY_EXCEPTION:
        return error
    validator = _VERIFICATION_CLEANUP_CAPABILITY_VALIDATOR
    try:
        capability_valid = (
            _VERIFICATION_CLEANUP_CAPABILITY_TYPE is not None
            and type(cleanup_capability) is _VERIFICATION_CLEANUP_CAPABILITY_TYPE
            and callable(validator)
            and validator(cleanup_capability, retry_cleanup) is True
        )
    except _BOUNDARY_EXCEPTION:
        capability_valid = False
    if capability_valid:

        def retry() -> None:
            retry_cleanup()

        error.retry_cleanup = retry
        error._verification_cleanup_capability = True
    return error


def _store_method_function(store: object, name: str) -> object:
    try:
        method = object.__getattribute__(store, name)
        owner = object.__getattribute__(method, "__self__")
        function = object.__getattribute__(method, "__func__")
    except (AttributeError, TypeError):
        raise _context_error("verification Store method binding is invalid") from None
    if owner is not store or not callable(function):
        raise _context_error("verification Store method binding is invalid")
    return function


def _registered_store_method(
    store: object,
    name: str,
    registration_name: str,
) -> object:
    function = _store_method_function(store, name)
    field_name = _REGISTERED_METHOD_NAMES.get(registration_name)
    if field_name is None:
        raise _context_error("verification Store method name is unsupported")
    with _REGISTERED_VERIFICATION_STORES_LOCK:
        registration = _REGISTERED_VERIFICATION_STORES.get(store)
    if registration is None:
        raise _context_error("verification Store registration is unavailable")
    registered = getattr(registration, field_name)
    if function is not registered or not callable(registered):
        raise _context_error("verification Store method differs")
    return function


def _context_frame(domain: bytes, values: tuple[object, ...]) -> str:
    return _event_wrapper_digest(domain, values)


def _verification_read_revision_digest(values: tuple[object, ...]) -> str:
    """Digest one Store-validated SQLite snapshot projection."""

    if type(values) is not tuple or not values:
        raise _context_error("verification revision projection is invalid")
    return _context_frame(_CONTEXT_REVISION_DOMAIN, values)


def _context_value_snapshot(context: VerificationContextSeed) -> tuple[object, ...]:
    if type(context) is not VerificationContextSeed:
        raise _context_error("verification context type differs")
    for text_value, text_name in (
        (context.root_key, "context root_key"),
        (context.run_id, "context run_id"),
        (context.main_terminal_id, "context main_terminal_id"),
        (context.worker_terminal_id, "context worker_terminal_id"),
        (context.reviewer_terminal_id, "context reviewer_terminal_id"),
        (context.effect_owner, "context effect_owner"),
    ):
        _identifier(text_value, text_name)
    for sequence_value, sequence_name in (
        (context.consumer_generation, "context consumer_generation"),
        (context.workflow_sequence, "context workflow_sequence"),
        (context.task_sequence, "context task_sequence"),
    ):
        _sequence(sequence_value, sequence_name)
    _wrapped_digest(
        context.workflow_checkpoint_digest,
        "context workflow_checkpoint_digest",
    )
    _wrapped_digest(context.task_state_digest, "context task_state_digest")
    if type(context.task_state) is not _task.TaskPolicyStateV4:
        raise _context_error("verification context task type differs")
    task_state_bytes = _ledger.encode_task_state(context.task_state)
    if (
        _ledger.task_state_digest(task_state_bytes) != context.task_state_digest
        or context.task_state.sequence != context.task_sequence
    ):
        raise _context_error("verification context task state differs")
    return (
        context.root_key,
        context.run_id,
        context.main_terminal_id,
        context.worker_terminal_id,
        context.reviewer_terminal_id,
        context.consumer_generation,
        context.workflow_sequence,
        context.workflow_checkpoint_digest,
        task_state_bytes,
        context.task_state_digest,
        context.task_sequence,
        context.effect_owner,
    )


def _context_baseline(context: VerificationContextSeed) -> VerificationContextSeed:
    task_bytes = _ledger.encode_task_state(context.task_state)
    baseline = object.__new__(VerificationContextSeed)
    for name, value in (
        ("root_key", context.root_key),
        ("run_id", context.run_id),
        ("main_terminal_id", context.main_terminal_id),
        ("worker_terminal_id", context.worker_terminal_id),
        ("reviewer_terminal_id", context.reviewer_terminal_id),
        ("consumer_generation", context.consumer_generation),
        ("workflow_sequence", context.workflow_sequence),
        ("workflow_checkpoint_digest", context.workflow_checkpoint_digest),
        ("task_state", _ledger.decode_task_state(task_bytes)),
        ("task_state_digest", context.task_state_digest),
        ("task_sequence", context.task_sequence),
        ("effect_owner", context.effect_owner),
    ):
        object.__setattr__(baseline, name, value)
    _context_value_snapshot(baseline)
    return baseline


def _live_store_generation(
    store: object,
    store_marker: object,
    open_generation: object,
) -> bool:
    try:
        return (
            object.__getattribute__(store, "_verification_store_marker") is store_marker
            and object.__getattribute__(store, "_verification_open_generation")
            is open_generation
            and object.__getattribute__(store, "_connection") is not None
        )
    except AttributeError:
        return False


@contextmanager
def _store_generation_guard(store: object) -> Iterator[None]:
    try:
        lock = object.__getattribute__(store, "_verification_generation_lock")
        acquire = object.__getattribute__(lock, "acquire")
        release = object.__getattribute__(lock, "release")
        if not callable(acquire) or not callable(release):
            raise _context_error("verification generation lock is invalid")
        acquire()
    except VerificationStoreError:
        raise
    except _BOUNDARY_EXCEPTION:
        raise _context_error("verification generation lock is unavailable") from None
    try:
        yield
    finally:
        release()


def _context_effect_owner(
    owner_id: str,
    checkpoint: _workflow.WorkflowCheckpointV4,
    task: _review_workflow.StoredTaskPolicyState,
    review_ref: _review.ReviewAuthorityRef,
) -> str:
    digest = _context_frame(
        _CONTEXT_OWNER_DOMAIN,
        (
            owner_id,
            checkpoint.root.root_key,
            checkpoint.run.run_id,
            checkpoint.workflow_sequence,
            checkpoint.checkpoint_digest,
            task.state.sequence,
            task.state_digest,
            review_ref.reference,
            review_ref.digest,
        ),
    )
    return "verification-owner-" + digest.removeprefix("sha256:")


def _snapshot_effect_owner(
    owner_id: str,
    snapshot: _ledger.ApprovalBindingSnapshotV1,
) -> str:
    digest = _context_frame(
        _CONTEXT_OWNER_DOMAIN,
        (
            owner_id,
            snapshot.root_key,
            snapshot.run_id,
            snapshot.workflow_sequence,
            snapshot.workflow_checkpoint_digest,
            snapshot.task_sequence,
            str(snapshot.task_state_digest),
            snapshot.review_ref,
            snapshot.review_digest,
        ),
    )
    return "verification-owner-" + digest.removeprefix("sha256:")


def _verification_receipt_reference(
    root_key: str,
    verification_ref: str,
    request_digest: str,
    effect_nonce: str,
    lease_epoch: int,
    fencing_token: int,
) -> str:
    digest = _context_frame(
        _RECEIPT_REF_DOMAIN,
        (
            root_key,
            verification_ref,
            request_digest,
            effect_nonce,
            lease_epoch,
            fencing_token,
        ),
    )
    return "verification-receipt-" + digest.removeprefix("sha256:")


def _validate_context_observation(
    observation: _review_workflow.ReviewCheckpointObservation,
    evidence: _handoff._ReviewAuthorityEvidence,
) -> tuple[_review.ReviewDecision, _review.ReviewAuthorityRef]:
    if type(observation) is not _review_workflow.ReviewCheckpointObservation:
        raise _context_error("verification review observation type is invalid")
    checkpoint = observation.checkpoint
    task = observation.task
    update = evidence.update
    review_ref = evidence.review_ref
    event = update.event
    assignment = checkpoint.active_assignment
    authority = checkpoint.review_authority
    last_event = observation.events[-1]
    if (
        checkpoint.workflow_state is not _workflow.CheckpointState.REVIEW_PENDING
        or checkpoint.workflow_sequence < 2
        or task.state.phase is not _task.TaskPhase.APPROVED
        or checkpoint.task_sequence != task.state.sequence
        or checkpoint.task_policy is None
        or checkpoint.task_policy.state_digest != task.state_digest
        or checkpoint.task_policy.task_id != str(task.state.task_id)
        or checkpoint.task_policy.sequence != task.state.sequence
        or observation.verification_operation_count != 0
        or observation.verification_receipt_count != 0
        or type(update) is not _review.ReviewPolicyUpdate
        or type(event) is not _review.ReviewDecision
        or event.kind is not _review.ReviewDecisionKind.APPROVED
        or update.next_state.task_state != task.state
        or update.expected_sequence + 1 != task.state.sequence
        or authority is None
        or authority.reference != review_ref.reference
        or _review_workflow._review_owner_digest_from_reference(authority.reference)
        != review_ref.digest
        or last_event.workflow_sequence != checkpoint.workflow_sequence
        or last_event.checkpoint_digest != checkpoint.checkpoint_digest
        or last_event.evidence_ref != authority.digest
        or assignment is None
    ):
        raise _context_error("verification current pair differs")
    identity_pairs = (
        (event.run_id, checkpoint.run.run_id),
        (event.run_id, task.run_id),
        (event.task_id, task.state.task_id),
        (event.dispatch_id, task.state.dispatch_id),
        (event.attempt_id, task.state.attempt_id),
        (event.worker_node, task.state.worker_node),
        (event.reviewer_node, task.state.reviewer_node),
        (event.review_round, task.state.review_round),
        (event.task_id, assignment.task_id),
        (event.dispatch_id, assignment.dispatch_id),
        (event.worker_node, assignment.worker_node),
        (event.worker_terminal_id, assignment.terminal_id),
        (task.state.team_id, checkpoint.root.team_id),
        (task.state.workspace, checkpoint.root.workspace_path),
    )
    if any(left != right for left, right in identity_pairs):
        raise _context_error("verification current identity differs")
    if (
        task.updated_ns != checkpoint.updated_ns
        or last_event.clock_ns != checkpoint.updated_ns
    ):
        raise _context_error("verification current revision differs")
    return event, review_ref


def _issue_verification_context(
    store: object,
    observation: _review_workflow.ReviewCheckpointObservation,
    owner_id: str,
    final_review_binding: object,
    *,
    revision: int,
    revision_digest: str,
    read_function: object,
    store_marker: object,
    open_generation: object,
) -> tuple[VerificationContextSeed, VerificationEffectOwner, int, str]:
    """Issue one context and capability from an already-open SQLite snapshot."""

    try:
        if (
            type(owner_id) is not str
            or not owner_id
            or owner_id.strip() != owner_id
            or len(owner_id) > 128
        ):
            raise _context_error("verification owner locator is invalid")
        if (
            _registered_store_method(
                store,
                "read_verification_context",
                "_verification_read_context_function",
            )
            is not read_function
            or store_marker is None
            or open_generation is None
            or not _live_store_generation(store, store_marker, open_generation)
            or type(revision) is not int
            or revision < 0
            or _WRAPPED_DIGEST.fullmatch(revision_digest) is None
        ):
            raise _context_error("verification Store registration differs")
        evidence = _handoff._validate_review_authority_binding(final_review_binding)
        event, review_ref = _validate_context_observation(observation, evidence)
        checkpoint = observation.checkpoint
        task = observation.task
        effect_owner = _context_effect_owner(owner_id, checkpoint, task, review_ref)
        context = object.__new__(VerificationContextSeed)
        for name, value in (
            ("root_key", checkpoint.root.root_key),
            ("run_id", checkpoint.run.run_id),
            ("main_terminal_id", checkpoint.run.main_terminal_id),
            ("worker_terminal_id", str(event.worker_terminal_id)),
            ("reviewer_terminal_id", str(event.reviewer_terminal_id)),
            ("consumer_generation", checkpoint.run.consumer_generation),
            ("workflow_sequence", checkpoint.workflow_sequence),
            ("workflow_checkpoint_digest", checkpoint.checkpoint_digest),
            ("task_state", task.state),
            ("task_state_digest", task.state_digest),
            ("task_sequence", task.state.sequence),
            ("effect_owner", effect_owner),
        ):
            object.__setattr__(context, name, value)
        context_snapshot = _context_value_snapshot(context)
        baseline = _context_baseline(context)
        owner = object.__new__(VerificationEffectOwner)
        object.__setattr__(owner, "owner_id", owner_id)
        state = _VerificationEffectOwnerState(
            store_ref=ref(store),
            store_marker=store_marker,
            open_generation=open_generation,
            read_function=read_function,
            context=context,
            context_baseline=baseline,
            context_snapshot=context_snapshot,
            revision=revision,
            revision_digest=revision_digest,
            owner_id=owner_id,
            binding=final_review_binding,
            handoff=evidence.owner,
            review_ref=review_ref,
        )
        with _VERIFICATION_EFFECT_OWNERS_LOCK:
            _VERIFICATION_EFFECT_OWNERS[owner] = state
        return context, owner, revision, revision_digest
    except VerificationStoreError:
        raise
    except _BOUNDARY_EXCEPTION:
        raise _context_error("verification context is unavailable") from None


def _issue_persisted_verification_context(
    store: object,
    snapshot: _ledger.ApprovalBindingSnapshotV1,
    request_projection: _ledger.VerificationRequestProjectionV1,
    owner_id: str,
    *,
    revision: int,
    revision_digest: str,
    read_function: object,
    store_marker: object,
    open_generation: object,
) -> tuple[
    VerificationContextSeed,
    VerificationEffectOwner,
    _ledger.ApprovalBindingSnapshotV1,
    _ledger.VerificationRequestProjectionV1,
    int,
    str,
]:
    try:
        if (
            type(owner_id) is not str
            or not owner_id
            or owner_id.strip() != owner_id
            or len(owner_id) > 128
            or _registered_store_method(
                store,
                "read_verification_reentry",
                "_verification_reentry_function",
            )
            is not read_function
            or not _live_store_generation(store, store_marker, open_generation)
            or type(revision) is not int
            or revision < 0
            or type(revision_digest) is not str
            or _WRAPPED_DIGEST.fullmatch(revision_digest) is None
        ):
            raise _context_error("persisted verification context input differs")
        snapshot.__post_init__()
        request_projection.__post_init__()
        if (
            _snapshot_effect_owner(owner_id, snapshot) != snapshot.effect_owner
            or snapshot.approved_review != request_projection.approval
        ):
            raise _context_error("persisted verification owner differs")
        approved = dict(snapshot.approved_review)
        context = object.__new__(VerificationContextSeed)
        for name, value in (
            ("root_key", snapshot.root_key),
            ("run_id", snapshot.run_id),
            ("main_terminal_id", snapshot.main_terminal_id),
            ("worker_terminal_id", approved["worker_terminal_id"]),
            ("reviewer_terminal_id", approved["reviewer_terminal_id"]),
            ("consumer_generation", snapshot.consumer_generation),
            ("workflow_sequence", snapshot.workflow_sequence),
            ("workflow_checkpoint_digest", snapshot.workflow_checkpoint_digest),
            ("task_state", _ledger.decode_task_state(snapshot.task_state_bytes)),
            ("task_state_digest", str(snapshot.task_state_digest)),
            ("task_sequence", snapshot.task_sequence),
            ("effect_owner", snapshot.effect_owner),
        ):
            object.__setattr__(context, name, value)
        context_snapshot = _context_value_snapshot(context)
        baseline = _context_baseline(context)
        owner = object.__new__(VerificationEffectOwner)
        object.__setattr__(owner, "owner_id", owner_id)
        with _VERIFICATION_EFFECT_OWNERS_LOCK:
            _VERIFICATION_EFFECT_OWNERS[owner] = _VerificationEffectOwnerState(
                store_ref=ref(store),
                store_marker=store_marker,
                open_generation=open_generation,
                read_function=read_function,
                context=context,
                context_baseline=baseline,
                context_snapshot=context_snapshot,
                revision=revision,
                revision_digest=revision_digest,
                owner_id=owner_id,
                binding=None,
                handoff=None,
                review_ref=None,
            )
        return (
            context,
            owner,
            snapshot,
            request_projection,
            revision,
            revision_digest,
        )
    except VerificationStoreError:
        raise
    except _BOUNDARY_EXCEPTION:
        raise _context_error("persisted verification context is unavailable") from None


def _validated_context_read(
    store: object,
    context_read: object,
) -> tuple[
    VerificationContextSeed,
    VerificationEffectOwner,
    int,
    str,
    _VerificationEffectOwnerState,
]:
    if type(context_read) is not tuple or len(context_read) != 4:
        raise _context_error("verification context read is invalid")
    context, owner, revision, revision_digest = context_read
    if (
        type(context) is not VerificationContextSeed
        or type(owner) is not VerificationEffectOwner
        or type(revision) is not int
        or type(revision_digest) is not str
    ):
        raise _context_error("verification context read type differs")
    with _VERIFICATION_EFFECT_OWNERS_LOCK:
        state = _VERIFICATION_EFFECT_OWNERS.get(owner)
    if (
        state is None
        or state.store_ref() is not store
        or state.context is not context
        or state.context_snapshot != _context_value_snapshot(context)
        or state.revision != revision
        or state.revision_digest != revision_digest
        or state.owner_id != owner.owner_id
        or _registered_store_method(
            store,
            "read_verification_context",
            "_verification_read_context_function",
        )
        is not state.read_function
        or not _live_store_generation(
            store,
            state.store_marker,
            state.open_generation,
        )
    ):
        raise _context_error("verification context provenance differs")
    return context, owner, revision, revision_digest, state


def _approval_snapshot(
    context: VerificationContextSeed,
    owner: VerificationEffectOwner,
    review_ref: _review.ReviewAuthorityRef,
    completion_ref: _path_resource.CompletionAdmissionRef,
    approval_ref: _gate.ApprovalRef,
    bound: _gate._BoundApproval,
) -> _ledger.ApprovalBindingSnapshotV1:
    _gate._validate_bound_approval(bound)
    approved = bound.approved
    identity_pairs = (
        (bound.approval_ref, approval_ref),
        (approved.approval_ref, approval_ref),
        (approved.run_id, context.run_id),
        (approved.worker_terminal_id, context.worker_terminal_id),
        (approved.reviewer_terminal_id, context.reviewer_terminal_id),
        (approved.approval_sequence, context.task_sequence),
        (approved.team_id, context.task_state.team_id),
        (approved.workspace, context.task_state.workspace),
        (approved.task_id, context.task_state.task_id),
        (approved.dispatch_id, context.task_state.dispatch_id),
        (approved.attempt_id, context.task_state.attempt_id),
        (approved.worker_node, context.task_state.worker_node),
        (approved.reviewer_node, context.task_state.reviewer_node),
        (approved.review_round, context.task_state.review_round),
    )
    if any(left != right for left, right in identity_pairs):
        raise _context_error("captured approval identity differs")
    task_state_bytes = _ledger.encode_task_state(context.task_state)
    task_state_digest = _ledger.task_state_digest(task_state_bytes)
    if task_state_digest != context.task_state_digest:
        raise _context_error("captured task digest differs")
    projection = _ledger._projection_from_approved(approved)
    payload = _ledger._snapshot_payload(
        version=1,
        review_ref=review_ref.reference,
        review_digest=review_ref.digest,
        completion_ref=completion_ref.reference,
        completion_digest=completion_ref.digest,
        approval_ref=str(approval_ref),
        approval_digest=str(approved.authority_digest),
        approved_review=projection,
        task_state_bytes=task_state_bytes,
        task_state_digest=task_state_digest,
        root_key=context.root_key,
        run_id=context.run_id,
        main_terminal_id=context.main_terminal_id,
        consumer_generation=context.consumer_generation,
        workflow_sequence=context.workflow_sequence,
        workflow_checkpoint_digest=context.workflow_checkpoint_digest,
        task_sequence=context.task_sequence,
        effect_owner=context.effect_owner,
    )
    snapshot = _ledger.ApprovalBindingSnapshotV1(
        version=1,
        review_ref=review_ref.reference,
        review_digest=review_ref.digest,
        completion_ref=completion_ref.reference,
        completion_digest=completion_ref.digest,
        approval_ref=str(approval_ref),
        approval_digest=str(approved.authority_digest),
        approved_review=projection,
        task_state_bytes=task_state_bytes,
        task_state_digest=task_state_digest,
        binding_digest=_ledger._snapshot_digest(payload),
        root_key=context.root_key,
        run_id=context.run_id,
        main_terminal_id=context.main_terminal_id,
        consumer_generation=context.consumer_generation,
        workflow_sequence=context.workflow_sequence,
        workflow_checkpoint_digest=context.workflow_checkpoint_digest,
        task_sequence=context.task_sequence,
        effect_owner=context.effect_owner,
    )
    raw = _ledger.encode_approval_binding_snapshot(snapshot)
    if _ledger.decode_approval_binding_snapshot(raw) != snapshot:
        raise _context_error("captured approval snapshot is not canonical")
    if owner.owner_id == "":
        raise _context_error("verification effect owner is invalid")
    return snapshot


def capture_approval_binding(
    store: object,
    handoff: _handoff.PolicyVerificationHandoff,
    context_read: object,
    review_ref: _review.ReviewAuthorityRef,
    completion_ref: _path_resource.CompletionAdmissionRef,
) -> tuple[_ledger.ApprovalBindingSnapshotV1, StagedStoreApprovalAdmission]:
    """Capture one actual owner pair without mutating the durable Store."""

    try:
        context, owner, revision, revision_digest, state = _validated_context_read(
            store, context_read
        )
        if (
            type(handoff) is not _handoff.PolicyVerificationHandoff
            or handoff is not state.handoff
            or review_ref is not state.review_ref
        ):
            raise _context_error("verification owner binding differs")
        fresh = object.__getattribute__(store, "read_verification_context")(
            context.root_key,
            state.owner_id,
            state.binding,
        )
        (
            _fresh_context,
            _fresh_owner,
            fresh_revision,
            fresh_digest,
            fresh_state,
        ) = _validated_context_read(store, fresh)
        if (
            fresh_state.context_snapshot != state.context_snapshot
            or fresh_revision != revision
            or fresh_digest != revision_digest
        ):
            raise _context_error("verification context is stale")
        _handoff.PolicyVerificationHandoff._validate_completion_owner(
            handoff,
            completion_ref,
        )
        approval_ref = handoff.compose(review_ref, completion_ref)
        bound = handoff.resolve(approval_ref)
        snapshot = _approval_snapshot(
            state.context_baseline,
            owner,
            review_ref,
            completion_ref,
            approval_ref,
            bound,
        )
        snapshot_bytes = _ledger.encode_approval_binding_snapshot(snapshot)
        snapshot_baseline = _ledger.decode_approval_binding_snapshot(snapshot_bytes)
        staged = object.__new__(StagedStoreApprovalAdmission)
        object.__setattr__(staged, "approval_ref", approval_ref)
        with _STAGED_ADMISSIONS_LOCK:
            _STAGED_ADMISSIONS[staged] = _StagedAdmissionState(
                store_ref=ref(store),
                store_marker=state.store_marker,
                open_generation=state.open_generation,
                owner=owner,
                snapshot=snapshot_baseline,
                returned_snapshot=snapshot,
                snapshot_bytes=snapshot_bytes,
                approval_ref=approval_ref,
                bound=bound,
            )
        return snapshot, staged
    except VerificationStoreError:
        raise
    except _BOUNDARY_EXCEPTION:
        raise _context_error("approval capture is unavailable") from None


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class VerificationPrepareRequest:
    """Return-only prepare command issued by one exact Store adapter."""

    request: _gate.VerificationRequest
    snapshot: _ledger.ApprovalBindingSnapshotV1
    context: VerificationContextSeed
    owner: VerificationEffectOwner
    revision: int
    revision_digest: str
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("verification prepare request is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("verification prepare request is return-only")

    def __repr__(self) -> str:
        return "<VerificationPrepareRequest opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("verification prepare request cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("verification prepare request cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("verification prepare request cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("verification prepare request cannot be pickled")


@dataclass(frozen=True, slots=True)
class _VerificationPreparePlan:
    request: _gate.VerificationRequest
    request_snapshot: tuple[str, ...]
    snapshot: _ledger.ApprovalBindingSnapshotV1
    context: VerificationContextSeed
    context_snapshot: tuple[object, ...]
    owner: VerificationEffectOwner
    revision: int
    revision_digest: str
    approval_binding_bytes: bytes
    request_projection: _ledger.VerificationRequestProjectionV1
    request_bytes: bytes
    before_task: _task.TaskPolicyStateV4
    before_task_bytes: bytes
    before_task_digest: str
    after_task: _task.TaskPolicyStateV4
    after_task_bytes: bytes
    after_task_digest: str


@dataclass(frozen=True, slots=True)
class _VerificationPrepareRequestState:
    adapter: StoreVerificationAdapter
    store_ref: ReferenceType[object]
    store_marker: object
    open_generation: object
    commit_function: object
    plan: _VerificationPreparePlan


@dataclass(slots=True)
class _StoreVerificationAdapterState:
    store_ref: ReferenceType[object]
    store_marker: object
    open_generation: object
    commit_function: object
    effect_function: object
    receipt_function: object
    read_function: object
    lifecycle_read_function: object
    terminal_function: object
    unknown_function: object
    snapshot: _ledger.ApprovalBindingSnapshotV1
    staged_admission: StagedStoreApprovalAdmission | None
    owner: VerificationEffectOwner
    profile_resolver: _gate.VerificationProfileResolver
    bound: _gate._BoundApproval | None = None
    prepared_request: _gate.VerificationRequest | None = None
    issued_effect: _gate.VerificationEffectLease | None = None
    committed_result: _gate.VerificationRunResult | None = None
    failed: bool = False


@dataclass(frozen=True, slots=True)
class _IssuedVerificationEffectState:
    effect_ref: ReferenceType[_gate.VerificationEffectLease]
    store_ref: ReferenceType[object]
    store_marker: object
    open_generation: object
    root_key: str
    verification_ref: str
    request_digest: str
    effect_owner: str
    effect_snapshot: tuple[object, ...]


_ISSUED_VERIFICATION_EFFECTS: Final[dict[int, _IssuedVerificationEffectState]] = {}
_ISSUED_VERIFICATION_EFFECTS_LOCK: Final = RLock()


def _register_issued_verification_effect(
    effect: _gate.VerificationEffectLease,
    state: _StoreVerificationAdapterState,
) -> None:
    _gate._validate_effect(effect)
    if effect.status is not _gate.EffectBeginStatus.RUN_ONCE:
        raise _context_error("verification effect is not runnable")
    effect_key = id(effect)

    def expire(effect_ref: ReferenceType[_gate.VerificationEffectLease]) -> None:
        with _ISSUED_VERIFICATION_EFFECTS_LOCK:
            current = _ISSUED_VERIFICATION_EFFECTS.get(effect_key)
            if current is not None and current.effect_ref is effect_ref:
                del _ISSUED_VERIFICATION_EFFECTS[effect_key]

    effect_ref = ref(effect, expire)
    issued = _IssuedVerificationEffectState(
        effect_ref=effect_ref,
        store_ref=state.store_ref,
        store_marker=state.store_marker,
        open_generation=state.open_generation,
        root_key=state.snapshot.root_key,
        verification_ref=str(effect.verification_ref),
        request_digest=str(effect.request_digest),
        effect_owner=state.snapshot.effect_owner,
        effect_snapshot=_effect_value_snapshot(effect),
    )
    with _ISSUED_VERIFICATION_EFFECTS_LOCK:
        existing = _ISSUED_VERIFICATION_EFFECTS.get(effect_key)
        if existing is not None and existing.effect_ref() is not effect:
            raise _context_error("verification effect identity is unavailable")
        _ISSUED_VERIFICATION_EFFECTS[effect_key] = issued


def _validate_issued_verification_effect(
    effect: _gate.VerificationEffectLease,
    state: _StoreVerificationAdapterState,
) -> None:
    _gate._validate_effect(effect)
    with _ISSUED_VERIFICATION_EFFECTS_LOCK:
        issued = _ISSUED_VERIFICATION_EFFECTS.get(id(effect))
    if (
        issued is None
        or issued.effect_ref() is not effect
        or issued.store_ref() is not state.store_ref()
        or issued.store_marker is not state.store_marker
        or issued.open_generation is not state.open_generation
        or issued.root_key != state.snapshot.root_key
        or issued.verification_ref != str(effect.verification_ref)
        or issued.request_digest != str(effect.request_digest)
        or issued.effect_owner != state.snapshot.effect_owner
        or issued.effect_snapshot != _effect_value_snapshot(effect)
    ):
        raise _context_error("verification effect provenance differs")


_ADAPTER_ISSUER: Final = object()
_PREPARE_REQUEST_ISSUER: Final = object()
_STORE_VERIFICATION_ADAPTERS: Final = WeakKeyDictionary[
    object, _StoreVerificationAdapterState
]()
_STORE_VERIFICATION_ADAPTERS_LOCK: Final = RLock()
_VERIFICATION_PREPARE_REQUESTS: Final = WeakKeyDictionary[
    VerificationPrepareRequest, _VerificationPrepareRequestState
]()
_VERIFICATION_PREPARE_REQUESTS_LOCK: Final = RLock()


def _approved_from_projection(
    projection: _ledger.ApprovedReviewProjection,
) -> _gate.ApprovedReview:
    values = dict(projection)
    lane = values["routing_lane"]
    if type(lane) is not str:
        raise _context_error("persisted approval lane differs")
    approved = _gate._make_approved(
        run_id=values["run_id"],
        team_id=values["team_id"],
        workspace=values["workspace"],
        task_id=values["task_id"],
        dispatch_id=values["dispatch_id"],
        attempt_id=values["attempt_id"],
        worker_node=values["worker_node"],
        reviewer_node=values["reviewer_node"],
        worker_terminal_id=values["worker_terminal_id"],
        reviewer_terminal_id=values["reviewer_terminal_id"],
        review_round=values["review_round"],
        target_head=values["target_head"],
        target_tree_digest=values["target_tree_digest"],
        claim_ref=values["claim_ref"],
        policy_fingerprint=values["policy_fingerprint"],
        routing_lane=_task.TaskLane(lane),
        approval_ref=values["approval_ref"],
        approval_sequence=values["approval_sequence"],
        profile_ref=values["profile_ref"],
        verification_id=values["verification_id"],
        routing_digest=values["routing_digest"],
        reservation_digest=values["reservation_digest"],
    )
    if (
        _ledger._projection_from_approved(approved) != projection
        or approved.authority_digest != values["authority_digest"]
    ):
        raise _context_error("persisted approval projection differs")
    return approved


def _hydrate_request(
    snapshot: _ledger.ApprovalBindingSnapshotV1,
    projection: _ledger.VerificationRequestProjectionV1,
    profile_resolver: _gate.VerificationProfileResolver,
) -> tuple[_gate._BoundApproval, _gate.VerificationRequest]:
    if snapshot.approved_review != projection.approval:
        raise _context_error("persisted approval/request differs")
    approved = _approved_from_projection(snapshot.approved_review)
    bound = _gate._make_bound_approval(
        _gate.ApprovalRef(snapshot.approval_ref),
        approved,
    )
    try:
        profile = profile_resolver.resolve(
            _task.VerificationProfileRef(projection.profile_ref)
        )
    except _BOUNDARY_EXCEPTION:
        raise _context_error("verification profile is unavailable") from None
    if type(profile) is not _gate.VerificationProfile:
        raise _context_error("verification profile type differs")
    try:
        profile.__post_init__()
        request = _gate._build_request(
            bound,
            profile,
            projection.before_snapshot,
        )
    except _BOUNDARY_EXCEPTION:
        raise _context_error("verification request hydration failed") from None
    if (
        _ledger.verification_request_projection_from_request(request) != projection
        or request.request_digest != projection.request_digest
    ):
        raise _context_error("persisted verification request differs")
    return bound, request


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class StoreVerificationAdapter:
    """Single package-private admission/state adapter for one Store."""

    _issuer: object

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("StoreVerificationAdapter uses an exact factory")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("StoreVerificationAdapter uses an exact factory")

    def __repr__(self) -> str:
        return "<StoreVerificationAdapter opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("StoreVerificationAdapter cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("StoreVerificationAdapter cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("StoreVerificationAdapter cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("StoreVerificationAdapter cannot be pickled")

    @classmethod
    def from_capture(
        cls,
        store: object,
        snapshot: _ledger.ApprovalBindingSnapshotV1,
        staged_admission: StagedStoreApprovalAdmission,
        profile_resolver: _gate.VerificationProfileResolver,
    ) -> Self:
        if cls is not StoreVerificationAdapter:
            raise _context_error("verification adapter type differs")
        try:
            if (
                type(snapshot) is not _ledger.ApprovalBindingSnapshotV1
                or type(staged_admission) is not StagedStoreApprovalAdmission
                or not callable(getattr(profile_resolver, "resolve", None))
            ):
                raise _context_error("verification adapter input is invalid")
            snapshot.__post_init__()
            with _STAGED_ADMISSIONS_LOCK:
                staged_state = _STAGED_ADMISSIONS.get(staged_admission)
            if (
                staged_state is None
                or staged_state.consumed
                or staged_state.store_ref() is not store
                or staged_state.returned_snapshot is not snapshot
                or _ledger.encode_approval_binding_snapshot(snapshot)
                != staged_state.snapshot_bytes
                or not _live_store_generation(
                    store,
                    staged_state.store_marker,
                    staged_state.open_generation,
                )
            ):
                raise _context_error("verification capture provenance differs")
            commit_function = _registered_store_method(
                store,
                "commit_verification_prepare",
                "_verification_prepare_function",
            )
            effect_function = _registered_store_method(
                store,
                "commit_verification_effect_begin",
                "_verification_effect_function",
            )
            receipt_function = _registered_store_method(
                store,
                "commit_verification_receipt",
                "_verification_receipt_function",
            )
            read_function = _registered_store_method(
                store,
                "read_verification_reentry",
                "_verification_reentry_function",
            )
            lifecycle_read_function = _registered_store_method(
                store,
                "read_verification_lifecycle",
                "_verification_lifecycle_read_function",
            )
            terminal_function = _registered_store_method(
                store,
                "commit_verification_terminal",
                "_verification_terminal_function",
            )
            unknown_function = _registered_store_method(
                store,
                "commit_verification_unknown",
                "_verification_unknown_function",
            )
            adapter = object.__new__(StoreVerificationAdapter)
            object.__setattr__(adapter, "_issuer", _ADAPTER_ISSUER)
            with _STORE_VERIFICATION_ADAPTERS_LOCK:
                _STORE_VERIFICATION_ADAPTERS[adapter] = _StoreVerificationAdapterState(
                    store_ref=ref(store),
                    store_marker=staged_state.store_marker,
                    open_generation=staged_state.open_generation,
                    commit_function=commit_function,
                    effect_function=effect_function,
                    receipt_function=receipt_function,
                    read_function=read_function,
                    lifecycle_read_function=lifecycle_read_function,
                    terminal_function=terminal_function,
                    unknown_function=unknown_function,
                    snapshot=staged_state.snapshot,
                    staged_admission=staged_admission,
                    owner=staged_state.owner,
                    profile_resolver=profile_resolver,
                )
            return cast(Self, adapter)
        except VerificationStoreError:
            raise
        except _BOUNDARY_EXCEPTION:
            raise _context_error("verification adapter is unavailable") from None

    @classmethod
    def from_store(
        cls,
        store: object,
        root_key: _workflow.WorkflowRootKey,
        verification_ref: _gate.VerificationRef,
        owner_id: str,
        profile_resolver: _gate.VerificationProfileResolver,
    ) -> Self:
        if cls is not StoreVerificationAdapter:
            raise _context_error("verification adapter type differs")
        try:
            if (
                type(root_key) is not str
                or type(verification_ref) is not str
                or type(owner_id) is not str
                or not callable(getattr(profile_resolver, "resolve", None))
            ):
                raise _context_error("verification reentry input is invalid")
            read_function = _registered_store_method(
                store,
                "read_verification_reentry",
                "_verification_reentry_function",
            )
            commit_function = _registered_store_method(
                store,
                "commit_verification_prepare",
                "_verification_prepare_function",
            )
            effect_function = _registered_store_method(
                store,
                "commit_verification_effect_begin",
                "_verification_effect_function",
            )
            receipt_function = _registered_store_method(
                store,
                "commit_verification_receipt",
                "_verification_receipt_function",
            )
            lifecycle_read_function = _registered_store_method(
                store,
                "read_verification_lifecycle",
                "_verification_lifecycle_read_function",
            )
            terminal_function = _registered_store_method(
                store,
                "commit_verification_terminal",
                "_verification_terminal_function",
            )
            unknown_function = _registered_store_method(
                store,
                "commit_verification_unknown",
                "_verification_unknown_function",
            )
            if not callable(read_function):
                raise _context_error("verification reentry read is unavailable")
            persisted = read_function(
                store,
                root_key,
                verification_ref,
                owner_id,
            )
            if type(persisted) is not tuple or len(persisted) != 6:
                raise _context_error("verification reentry result differs")
            (
                _context,
                owner,
                snapshot,
                request_projection,
                _revision,
                _revision_digest,
            ) = persisted
            if (
                type(owner) is not VerificationEffectOwner
                or type(snapshot) is not _ledger.ApprovalBindingSnapshotV1
                or type(request_projection)
                is not _ledger.VerificationRequestProjectionV1
            ):
                raise _context_error("verification reentry type differs")
            bound, request = _hydrate_request(
                snapshot,
                request_projection,
                profile_resolver,
            )
            with _VERIFICATION_EFFECT_OWNERS_LOCK:
                owner_state = _VERIFICATION_EFFECT_OWNERS.get(owner)
            if owner_state is None or owner_state.store_ref() is not store:
                raise _context_error("verification reentry owner differs")
            adapter = object.__new__(StoreVerificationAdapter)
            object.__setattr__(adapter, "_issuer", _ADAPTER_ISSUER)
            with _STORE_VERIFICATION_ADAPTERS_LOCK:
                _STORE_VERIFICATION_ADAPTERS[adapter] = _StoreVerificationAdapterState(
                    store_ref=ref(store),
                    store_marker=owner_state.store_marker,
                    open_generation=owner_state.open_generation,
                    commit_function=commit_function,
                    effect_function=effect_function,
                    receipt_function=receipt_function,
                    read_function=read_function,
                    lifecycle_read_function=lifecycle_read_function,
                    terminal_function=terminal_function,
                    unknown_function=unknown_function,
                    snapshot=snapshot,
                    staged_admission=None,
                    owner=owner,
                    profile_resolver=profile_resolver,
                    bound=bound,
                    prepared_request=request,
                )
            return cast(Self, adapter)
        except VerificationStoreError:
            raise
        except _BOUNDARY_EXCEPTION:
            raise _context_error(
                "durable verification adapter is unavailable"
            ) from None

    def _state(self) -> _StoreVerificationAdapterState:
        try:
            if (
                type(self) is not StoreVerificationAdapter
                or object.__getattribute__(self, "_issuer") is not _ADAPTER_ISSUER
            ):
                raise _context_error("verification adapter provenance differs")
            with _STORE_VERIFICATION_ADAPTERS_LOCK:
                state = _STORE_VERIFICATION_ADAPTERS.get(self)
            if state is None:
                raise _context_error("verification adapter is unavailable")
            store = state.store_ref()
            if store is None or not _live_store_generation(
                store,
                state.store_marker,
                state.open_generation,
            ):
                raise _context_error("verification adapter generation differs")
            with _VERIFICATION_EFFECT_OWNERS_LOCK:
                owner_state = _VERIFICATION_EFFECT_OWNERS.get(state.owner)
            if (
                owner_state is None
                or owner_state.store_ref() is not store
                or owner_state.store_marker is not state.store_marker
                or owner_state.open_generation is not state.open_generation
                or state.owner.owner_id != owner_state.owner_id
            ):
                raise _context_error("verification adapter owner differs")
            return state
        except VerificationStoreError:
            raise
        except _BOUNDARY_EXCEPTION:
            raise _context_error("verification adapter is unavailable") from None

    def resolve(self, approval_ref: _gate.ApprovalRef) -> _gate._BoundApproval:
        state = self._state()
        if state.failed:
            raise _context_error("verification adapter capture is unavailable")
        if state.bound is None:
            if state.staged_admission is None:
                raise _context_error("verification staged admission is unavailable")
            bound = state.staged_admission.resolve(approval_ref)
            if (
                bound.approval_ref != state.snapshot.approval_ref
                or _ledger._projection_from_approved(bound.approved)
                != state.snapshot.approved_review
            ):
                raise _context_error("verification adapter approval differs")
            state.bound = bound
        elif state.bound.approval_ref != approval_ref:
            raise _context_error("verification adapter approval ref differs")
        _gate._validate_bound_approval(state.bound)
        if (
            _ledger._projection_from_approved(state.bound.approved)
            != state.snapshot.approved_review
            or str(state.bound.approved.authority_digest)
            != state.snapshot.approval_digest
        ):
            raise _context_error("verification adapter approval changed")
        return state.bound

    def prepare_once(
        self,
        request: _gate.VerificationRequest,
    ) -> _gate.VerificationPrepareResult:
        state = self._state()
        if state.failed or state.bound is None:
            raise _context_error("verification adapter is not staged")
        try:
            command = _issue_verification_prepare_request(self, request)
            store = state.store_ref()
            if store is None:
                raise _context_error("verification Store is unavailable")
            commit = state.commit_function
            if not callable(commit):
                raise _context_error("verification Store commit is unavailable")
            result = commit(store, command)
            if type(result) is not _gate.VerificationPrepareResult:
                raise _context_error("verification prepare result type differs")
            if (
                object.__getattribute__(result, "_issuer") is not _gate._PREPARE_ISSUER
                or str(result.verification_ref) != str(request.verification_id)
                or result.approval_ref != request.approval_ref
                or result.request_digest != request.request_digest
                or result.approval_sequence != request.approval.approval_sequence
                or not _gate._same_request(result.request, request)
                or result.status
                not in {
                    _gate.PreparationStatus.PREPARED,
                    _gate.PreparationStatus.EXISTING,
                }
            ):
                raise _context_error("verification prepare result differs")
            record, _durable_status, _revision_digest = self._read_with_status(
                _gate.VerificationRef(request.verification_id)
            )
            if not _gate._same_request(record.request, request):
                raise _context_error("verification prepare readback differs")
            state.staged_admission = None
            state.prepared_request = record.request
            return result
        except VerificationStoreError:
            state.failed = True
            raise
        except _BOUNDARY_EXCEPTION as exc:
            state.failed = True
            raise _context_boundary_error(
                "verification prepare is unavailable",
                exc,
            ) from None

    def begin_effect_once(
        self,
        verification_ref: _gate.VerificationRef,
        request_digest: _gate.ReceiptDigest,
    ) -> _gate.VerificationEffectLease:
        state = self._state()
        try:
            request = state.prepared_request
            if request is None or state.bound is None:
                raise _context_error("verification effect adapter is not prepared")
            _gate._validate_bound_approval(state.bound)
            profile = state.profile_resolver.resolve(request.profile_ref)
            if type(profile) is not _gate.VerificationProfile:
                raise _context_error("verification effect profile differs")
            expected_request = _gate._build_request(
                state.bound,
                profile,
                request.before_snapshot,
            )
            if (
                not _gate._same_request(expected_request, request)
                or _ledger._projection_from_approved(state.bound.approved)
                != state.snapshot.approved_review
            ):
                raise _context_error("verification effect request changed")
            command = _issue_verification_effect_begin_request(
                self,
                verification_ref,
                request_digest,
            )
            store = state.store_ref()
            if store is None or not callable(state.effect_function):
                raise _context_error("verification effect Store is unavailable")
            result = state.effect_function(store, command)
            if (
                type(result) is not _gate.VerificationEffectLease
                or object.__getattribute__(result, "_issuer")
                is not _gate._EFFECT_ISSUER
                or str(result.verification_ref) != str(verification_ref)
                or str(result.request_digest) != str(request_digest)
            ):
                raise _context_error("verification effect result differs")
            _gate._validate_effect(result)
            if result.status is _gate.EffectBeginStatus.RUN_ONCE:
                state.issued_effect = result
                _register_issued_verification_effect(result, state)
            return result
        except VerificationStoreError as exc:
            if _is_context_error(exc):
                raise
            raise _context_error("verification effect is unavailable") from None
        except _BOUNDARY_EXCEPTION as exc:
            raise _context_boundary_error(
                "verification effect is unavailable",
                exc,
            ) from None

    def read(
        self,
        verification_ref: _gate.VerificationRef,
    ) -> _gate.VerificationDurableRecord:
        return self._read_with_status(verification_ref)[0]

    def status(
        self,
        verification_ref: _gate.VerificationRef,
    ) -> _gate.DurableRecordStatus:
        return self._read_with_status(verification_ref)[1]

    def record_receipt_once(
        self,
        verification_ref: _gate.VerificationRef,
        effect: _gate.VerificationEffectLease,
        result: _gate.VerificationRunResult,
        before: _gate.VerificationSnapshot,
        after: _gate.VerificationSnapshot,
    ) -> _gate.VerificationReceipt:
        state = self._state()
        try:
            command = _issue_verification_receipt_request(
                self,
                verification_ref,
                effect,
                result,
                before,
                after,
            )
            store = state.store_ref()
            if store is None or not callable(state.receipt_function):
                raise _context_error("verification receipt Store is unavailable")
            receipt = state.receipt_function(store, command)
            if (
                type(receipt) is not _gate.VerificationReceipt
                or object.__getattribute__(receipt, "_issuer")
                is not _gate._RECEIPT_ISSUER
                or str(receipt.verification_ref) != str(verification_ref)
                or str(receipt.request_digest) != str(effect.request_digest)
            ):
                raise _context_error("verification receipt result differs")
            _gate._validate_receipt(receipt, verify_digest=True)
            state.committed_result = result
            return receipt
        except VerificationStoreError:
            _gate._discard_runner_result_attestation(result)
            raise
        except _BOUNDARY_EXCEPTION as exc:
            _gate._discard_runner_result_attestation(result)
            raise _context_boundary_error(
                "verification receipt is unavailable",
                exc,
                commit_unknown_reason="receipt-commit-unknown",
            ) from None

    def apply_terminal_once(
        self,
        verification_ref: _gate.VerificationRef,
        receipt_ref: _task.ReceiptRef,
        receipt_digest: _gate.ReceiptDigest,
    ) -> _gate.VerificationTerminalResult:
        state = self._state()
        try:
            command = _issue_verification_terminal_request(
                self,
                verification_ref,
                receipt_ref,
                receipt_digest,
            )
            store = state.store_ref()
            if store is None or not callable(state.terminal_function):
                raise _context_error("verification terminal Store is unavailable")
            result = state.terminal_function(store, command)
            if (
                type(result) is not _gate.VerificationTerminalResult
                or object.__getattribute__(result, "_issuer")
                is not _gate._TERMINAL_ISSUER
                or str(result.verification_ref) != str(verification_ref)
                or str(result.receipt_ref) != str(receipt_ref)
                or str(result.receipt_digest) != str(receipt_digest)
            ):
                raise _context_error("verification terminal result differs")
            _gate._validate_terminal(result)
            return result
        except VerificationStoreError:
            raise
        except _BOUNDARY_EXCEPTION as exc:
            raise _context_boundary_error(
                "verification terminal is unavailable",
                exc,
            ) from None

    def _read_with_status(
        self,
        verification_ref: _gate.VerificationRef,
    ) -> tuple[
        _gate.VerificationDurableRecord,
        _gate.DurableRecordStatus,
        str,
    ]:
        state = self._state()
        store = state.store_ref()
        if (
            store is None
            or type(verification_ref) is not str
            or not callable(state.lifecycle_read_function)
        ):
            raise _context_error("verification durable snapshot is unavailable")
        try:
            projection = state.lifecycle_read_function(
                store,
                state.snapshot.root_key,
                str(verification_ref),
            )
            if type(projection) is not _VerificationLifecycleProjection:
                raise _context_error("verification lifecycle projection differs")
            bound, request = _hydrate_request(
                projection.snapshot,
                projection.request_projection,
                state.profile_resolver,
            )
            if (
                bound.approval_ref != state.snapshot.approval_ref
                or str(request.verification_id) != str(verification_ref)
                or projection.revision_digest == ""
                or _WRAPPED_DIGEST.fullmatch(projection.revision_digest) is None
            ):
                raise _context_error("verification lifecycle identity differs")
            state.bound = bound
            state.prepared_request = request
            effect: _gate.VerificationEffectLease | None = None
            receipt: _gate.VerificationReceipt | None = None
            if projection.status == "PREPARED":
                durable_status = _gate.DurableRecordStatus.PREPARED
            elif projection.status == "EFFECT_PREPARED":
                durable_status = _gate.DurableRecordStatus.PREPARED
                effect_status = _gate.EffectBeginStatus.RUN_ONCE
                effect = _effect_from_lifecycle_projection(
                    projection,
                    request,
                    effect_status,
                )
                state.issued_effect = effect
                _register_issued_verification_effect(effect, state)
            elif projection.status == "RECEIPTED":
                durable_status = _gate.DurableRecordStatus.RECEIPTED
                effect = _effect_from_lifecycle_projection(
                    projection,
                    request,
                    _gate.EffectBeginStatus.RECEIPTED,
                )
                if projection.receipt_projection is None:
                    raise _context_error("verification receipt projection is missing")
                receipt = _receipt_from_projection(
                    projection.receipt_projection,
                    request,
                    effect,
                )
            elif projection.status == "TERMINAL":
                durable_status = _gate.DurableRecordStatus.TERMINAL
                effect = _effect_from_lifecycle_projection(
                    projection,
                    request,
                    _gate.EffectBeginStatus.TERMINAL,
                )
                if projection.receipt_projection is None:
                    raise _context_error("verification receipt projection is missing")
                receipt = _receipt_from_projection(
                    projection.receipt_projection,
                    request,
                    effect,
                )
            elif projection.status == "UNKNOWN_EFFECT":
                durable_status = _gate.DurableRecordStatus.UNKNOWN
                effect = _effect_from_lifecycle_projection(
                    projection,
                    request,
                    _gate.EffectBeginStatus.UNKNOWN,
                )
            else:
                raise _context_error("verification lifecycle status is unsupported")
            record = _gate._make_record(
                _gate.VerificationRef(projection.request_projection.verification_id),
                _gate.ApprovalRef(projection.snapshot.approval_ref),
                request,
                durable_status,
                effect,
                receipt,
            )
            return record, durable_status, projection.revision_digest
        except VerificationStoreError:
            raise
        except _BOUNDARY_EXCEPTION:
            raise _context_error(
                "verification durable snapshot is unavailable"
            ) from None

    def _mark_unknown(
        self,
        verification_ref: _gate.VerificationRef,
        request_digest: _gate.ReceiptDigest,
        *,
        reason_code: str,
        effect: _gate.VerificationEffectLease | None = None,
        evidence_digest: str | None = None,
    ) -> object:
        state = self._state()
        try:
            command = _issue_verification_unknown_request(
                self,
                verification_ref,
                request_digest,
                reason_code=reason_code,
                effect=effect,
                evidence_digest=evidence_digest,
            )
            store = state.store_ref()
            if store is None or not callable(state.unknown_function):
                raise _context_error("verification unknown Store is unavailable")
            return state.unknown_function(store, command)
        except VerificationStoreError:
            raise
        except _BOUNDARY_EXCEPTION as exc:
            raise _context_boundary_error(
                "verification unknown is unavailable",
                exc,
            ) from None


def _issue_verification_prepare_request(
    adapter: StoreVerificationAdapter,
    request: _gate.VerificationRequest,
) -> VerificationPrepareRequest:
    state = adapter._state()
    store = state.store_ref()
    if store is None or state.bound is None:
        raise _context_error("verification prepare authority is unavailable")
    try:
        _gate._validate_request(request, verify_digest=True)
        profile = state.profile_resolver.resolve(request.profile_ref)
        if type(profile) is not _gate.VerificationProfile:
            raise _context_error("verification profile type differs")
        profile.__post_init__()
        expected_request = _gate._build_request(
            state.bound,
            profile,
            request.before_snapshot,
        )
        if not _gate._same_request(expected_request, request):
            raise _context_error("verification profile/request differs")
        with _VERIFICATION_EFFECT_OWNERS_LOCK:
            owner_state = _VERIFICATION_EFFECT_OWNERS.get(state.owner)
        if (
            owner_state is None
            or owner_state.store_ref() is not store
            or owner_state.store_marker is not state.store_marker
            or owner_state.open_generation is not state.open_generation
            or _context_value_snapshot(owner_state.context)
            != owner_state.context_snapshot
            or state.snapshot.approval_ref != request.approval_ref
            or state.snapshot.approval_digest != request.approval.authority_digest
            or state.snapshot.approved_review
            != _ledger._projection_from_approved(request.approval)
            or state.snapshot.task_sequence != request.approval.approval_sequence
        ):
            raise _context_error("verification prepare binding differs")
        context = owner_state.context_baseline
        approved = dict(state.snapshot.approved_review)
        context_pairs = (
            (state.snapshot.root_key, context.root_key),
            (state.snapshot.run_id, context.run_id),
            (state.snapshot.main_terminal_id, context.main_terminal_id),
            (state.snapshot.consumer_generation, context.consumer_generation),
            (state.snapshot.workflow_sequence, context.workflow_sequence),
            (
                state.snapshot.workflow_checkpoint_digest,
                context.workflow_checkpoint_digest,
            ),
            (state.snapshot.task_sequence, context.task_sequence),
            (state.snapshot.task_state_digest, context.task_state_digest),
            (state.snapshot.effect_owner, context.effect_owner),
            (approved["worker_terminal_id"], context.worker_terminal_id),
            (approved["reviewer_terminal_id"], context.reviewer_terminal_id),
        )
        if any(left != right for left, right in context_pairs):
            raise _context_error("verification prepare context differs")
        approval_binding_bytes = _ledger.encode_approval_binding_snapshot(
            state.snapshot
        )
        if (
            _ledger.decode_approval_binding_snapshot(approval_binding_bytes)
            != state.snapshot
            or _ledger.approval_binding_snapshot_digest(approval_binding_bytes)
            != state.snapshot.binding_digest
        ):
            raise _context_error("verification approval snapshot differs")
        request_projection = _ledger.verification_request_projection_from_request(
            request
        )
        request_bytes = _ledger.encode_verification_request_projection(
            request_projection
        )
        if (
            _ledger.decode_verification_request_projection(request_bytes)
            != request_projection
            or request_projection.request_digest != request.request_digest
        ):
            raise _context_error("verification request projection differs")
        before_task = _ledger.decode_task_state(state.snapshot.task_state_bytes)
        if (
            before_task != context.task_state
            or before_task.phase is not _task.TaskPhase.APPROVED
            or _ledger.task_state_digest(state.snapshot.task_state_bytes)
            != state.snapshot.task_state_digest
        ):
            raise _context_error("verification task preimage differs")
        after_task = replace(
            before_task,
            sequence=before_task.sequence + 1,
            receipt_ref=None,
            phase=_task.TaskPhase.VERIFYING,
        )
        after_task_bytes = _ledger.encode_task_state(after_task)
        after_task = _ledger.decode_task_state(after_task_bytes)
        after_task_digest = str(_ledger.task_state_digest(after_task_bytes))
        plan = _VerificationPreparePlan(
            request=request,
            request_snapshot=_gate._request_parts(request),
            snapshot=state.snapshot,
            context=context,
            context_snapshot=_context_value_snapshot(context),
            owner=state.owner,
            revision=owner_state.revision,
            revision_digest=owner_state.revision_digest,
            approval_binding_bytes=approval_binding_bytes,
            request_projection=request_projection,
            request_bytes=request_bytes,
            before_task=before_task,
            before_task_bytes=state.snapshot.task_state_bytes,
            before_task_digest=str(state.snapshot.task_state_digest),
            after_task=after_task,
            after_task_bytes=after_task_bytes,
            after_task_digest=after_task_digest,
        )
        command = object.__new__(VerificationPrepareRequest)
        for name, value in (
            ("request", request),
            ("snapshot", state.snapshot),
            ("context", context),
            ("owner", state.owner),
            ("revision", owner_state.revision),
            ("revision_digest", owner_state.revision_digest),
            ("_issuer", _PREPARE_REQUEST_ISSUER),
        ):
            object.__setattr__(command, name, value)
        with _VERIFICATION_PREPARE_REQUESTS_LOCK:
            _VERIFICATION_PREPARE_REQUESTS[command] = _VerificationPrepareRequestState(
                adapter=adapter,
                store_ref=ref(store),
                store_marker=state.store_marker,
                open_generation=state.open_generation,
                commit_function=state.commit_function,
                plan=plan,
            )
        return command
    except VerificationStoreError as exc:
        if _is_context_error(exc):
            raise
        raise _context_error("verification prepare request is invalid") from None
    except _BOUNDARY_EXCEPTION:
        raise _context_error("verification prepare request is invalid") from None


def _validate_verification_prepare_request(
    command: object,
    expected_store: object,
) -> _VerificationPreparePlan:
    try:
        if type(command) is not VerificationPrepareRequest:
            raise _context_error("verification prepare request type differs")
        if object.__getattribute__(command, "_issuer") is not _PREPARE_REQUEST_ISSUER:
            raise _context_error("verification prepare request issuer differs")
        with _VERIFICATION_PREPARE_REQUESTS_LOCK:
            state = _VERIFICATION_PREPARE_REQUESTS.get(command)
        if state is None:
            raise _context_error("verification prepare request is unavailable")
        adapter_state = state.adapter._state()
        if (
            state.store_ref() is not expected_store
            or adapter_state.store_ref() is not expected_store
            or state.store_marker is not adapter_state.store_marker
            or state.open_generation is not adapter_state.open_generation
            or not _live_store_generation(
                expected_store,
                state.store_marker,
                state.open_generation,
            )
            or _registered_store_method(
                expected_store,
                "commit_verification_prepare",
                "_verification_prepare_function",
            )
            is not state.commit_function
            or command.request is not state.plan.request
            or command.snapshot is not state.plan.snapshot
            or command.context is not state.plan.context
            or command.owner is not state.plan.owner
            or command.revision != state.plan.revision
            or command.revision_digest != state.plan.revision_digest
        ):
            raise _context_error("verification prepare request provenance differs")
        _gate._validate_request(command.request, verify_digest=True)
        snapshot_bytes = _ledger.encode_approval_binding_snapshot(command.snapshot)
        if (
            _gate._request_parts(command.request) != state.plan.request_snapshot
            or _ledger.verification_request_projection_from_request(command.request)
            != state.plan.request_projection
            or _ledger.encode_verification_request_projection(
                state.plan.request_projection
            )
            != state.plan.request_bytes
            or snapshot_bytes != state.plan.approval_binding_bytes
            or _ledger.decode_approval_binding_snapshot(snapshot_bytes)
            != state.plan.snapshot
            or _ledger.approval_binding_snapshot_digest(snapshot_bytes)
            != state.plan.snapshot.binding_digest
            or _context_value_snapshot(command.context) != state.plan.context_snapshot
        ):
            raise _context_error("verification prepare request changed after issue")
        return state.plan
    except VerificationStoreError:
        raise
    except _BOUNDARY_EXCEPTION:
        raise _context_error("verification prepare request is invalid") from None


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class VerificationEffectBeginRequest:
    """Return-only effect arm command for one exact prepared adapter."""

    root_key: str
    verification_ref: _gate.VerificationRef
    request_digest: _gate.ReceiptDigest
    owner: VerificationEffectOwner
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("verification effect request is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("verification effect request is return-only")

    def __repr__(self) -> str:
        return "<VerificationEffectBeginRequest opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("verification effect request cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("verification effect request cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("verification effect request cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("verification effect request cannot be pickled")


@dataclass(frozen=True, slots=True)
class _VerificationEffectBeginPlan:
    root_key: str
    verification_ref: str
    request_digest: str
    effect_owner: str
    owner: VerificationEffectOwner


@dataclass(frozen=True, slots=True)
class _VerificationEffectBeginRequestState:
    adapter: StoreVerificationAdapter
    store_ref: ReferenceType[object]
    store_marker: object
    open_generation: object
    effect_function: object
    plan: _VerificationEffectBeginPlan


_EFFECT_BEGIN_REQUEST_ISSUER: Final = object()
_VERIFICATION_EFFECT_BEGIN_REQUESTS: Final = WeakKeyDictionary[
    VerificationEffectBeginRequest, _VerificationEffectBeginRequestState
]()
_VERIFICATION_EFFECT_BEGIN_REQUESTS_LOCK: Final = RLock()


def _issue_verification_effect_begin_request(
    adapter: StoreVerificationAdapter,
    verification_ref: _gate.VerificationRef,
    request_digest: _gate.ReceiptDigest,
) -> VerificationEffectBeginRequest:
    state = adapter._state()
    request = state.prepared_request
    if (
        request is None
        or type(verification_ref) is not str
        or type(request_digest) is not str
        or str(verification_ref) != str(request.verification_id)
        or str(request_digest) != str(request.request_digest)
    ):
        raise _context_error("verification effect identity differs")
    with _VERIFICATION_EFFECT_OWNERS_LOCK:
        owner_state = _VERIFICATION_EFFECT_OWNERS.get(state.owner)
    if (
        owner_state is None
        or owner_state.store_ref() is not state.store_ref()
        or owner_state.store_marker is not state.store_marker
        or owner_state.open_generation is not state.open_generation
        or _context_value_snapshot(owner_state.context) != owner_state.context_snapshot
        or state.snapshot.effect_owner != owner_state.context_baseline.effect_owner
    ):
        raise _context_error("verification effect owner differs")
    store = state.store_ref()
    if store is None:
        raise _context_error("verification effect Store is unavailable")
    plan = _VerificationEffectBeginPlan(
        root_key=state.snapshot.root_key,
        verification_ref=str(verification_ref),
        request_digest=str(request_digest),
        effect_owner=state.snapshot.effect_owner,
        owner=state.owner,
    )
    command = object.__new__(VerificationEffectBeginRequest)
    for name, value in (
        ("root_key", plan.root_key),
        ("verification_ref", verification_ref),
        ("request_digest", request_digest),
        ("owner", state.owner),
        ("_issuer", _EFFECT_BEGIN_REQUEST_ISSUER),
    ):
        object.__setattr__(command, name, value)
    with _VERIFICATION_EFFECT_BEGIN_REQUESTS_LOCK:
        _VERIFICATION_EFFECT_BEGIN_REQUESTS[command] = (
            _VerificationEffectBeginRequestState(
                adapter=adapter,
                store_ref=ref(store),
                store_marker=state.store_marker,
                open_generation=state.open_generation,
                effect_function=state.effect_function,
                plan=plan,
            )
        )
    return command


def _validate_verification_effect_begin_request(
    command: object,
    expected_store: object,
) -> _VerificationEffectBeginPlan:
    try:
        if (
            type(command) is not VerificationEffectBeginRequest
            or object.__getattribute__(command, "_issuer")
            is not _EFFECT_BEGIN_REQUEST_ISSUER
        ):
            raise _context_error("verification effect request type differs")
        with _VERIFICATION_EFFECT_BEGIN_REQUESTS_LOCK:
            state = _VERIFICATION_EFFECT_BEGIN_REQUESTS.get(command)
        if state is None:
            raise _context_error("verification effect request is unavailable")
        adapter_state = state.adapter._state()
        plan = state.plan
        if (
            state.store_ref() is not expected_store
            or adapter_state.store_ref() is not expected_store
            or state.store_marker is not adapter_state.store_marker
            or state.open_generation is not adapter_state.open_generation
            or _registered_store_method(
                expected_store,
                "commit_verification_effect_begin",
                "_verification_effect_function",
            )
            is not state.effect_function
            or command.root_key != plan.root_key
            or str(command.verification_ref) != plan.verification_ref
            or str(command.request_digest) != plan.request_digest
            or command.owner is not plan.owner
        ):
            raise _context_error("verification effect request provenance differs")
        return plan
    except VerificationStoreError:
        raise
    except _BOUNDARY_EXCEPTION:
        raise _context_error("verification effect request is invalid") from None


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class VerificationReceiptRequest:
    """Return-only receipt command for one exact armed operation."""

    root_key: str
    verification_ref: _gate.VerificationRef
    effect: _gate.VerificationEffectLease
    result: _gate.VerificationRunResult
    before: _gate.VerificationSnapshot
    after: _gate.VerificationSnapshot
    owner: VerificationEffectOwner
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("verification receipt request is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("verification receipt request is return-only")

    def __repr__(self) -> str:
        return "<VerificationReceiptRequest opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("verification receipt request cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("verification receipt request cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("verification receipt request cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("verification receipt request cannot be pickled")


def _effect_value_snapshot(effect: _gate.VerificationEffectLease) -> tuple[object, ...]:
    _gate._validate_effect(effect)
    return (
        effect.verification_ref,
        effect.request_digest,
        effect.effect_nonce,
        effect.lease_epoch,
        effect.fencing_token,
        effect.status,
    )


def _result_value_snapshot(result: _gate.VerificationRunResult) -> tuple[object, ...]:
    result.__post_init__()
    return tuple(
        getattr(result, name)
        for name in result.__dataclass_fields__
        if not name.startswith("_")
    )


@dataclass(frozen=True, slots=True)
class _VerificationReceiptPlan:
    root_key: str
    verification_ref: str
    request: _gate.VerificationRequest
    request_snapshot: tuple[str, ...]
    effect: _gate.VerificationEffectLease
    effect_snapshot: tuple[object, ...]
    result: _gate.VerificationRunResult
    result_snapshot: tuple[object, ...]
    before: _gate.VerificationSnapshot
    before_snapshot: tuple[str, ...]
    after: _gate.VerificationSnapshot
    after_snapshot: tuple[str, ...]
    owner: VerificationEffectOwner


@dataclass(frozen=True, slots=True)
class _VerificationReceiptRequestState:
    adapter: StoreVerificationAdapter
    store_ref: ReferenceType[object]
    store_marker: object
    open_generation: object
    receipt_function: object
    plan: _VerificationReceiptPlan


_RECEIPT_REQUEST_ISSUER: Final = object()
_VERIFICATION_RECEIPT_REQUESTS: Final = WeakKeyDictionary[
    VerificationReceiptRequest, _VerificationReceiptRequestState
]()
_VERIFICATION_RECEIPT_REQUESTS_LOCK: Final = RLock()


def _issue_verification_receipt_request(
    adapter: StoreVerificationAdapter,
    verification_ref: _gate.VerificationRef,
    effect: _gate.VerificationEffectLease,
    result: _gate.VerificationRunResult,
    before: _gate.VerificationSnapshot,
    after: _gate.VerificationSnapshot,
) -> VerificationReceiptRequest:
    state = adapter._state()
    request = state.prepared_request
    store = state.store_ref()
    if request is None or store is None or type(verification_ref) is not str:
        raise _context_error("verification receipt adapter is not prepared")
    try:
        _gate._validate_request(request, verify_digest=True)
        _gate._validate_effect(effect)
        _gate._validate_result_for_request(result, request, effect)
        if (
            effect.status is not _gate.EffectBeginStatus.RUN_ONCE
            or str(verification_ref) != str(request.verification_id)
            or str(effect.verification_ref) != str(verification_ref)
            or str(effect.request_digest) != str(request.request_digest)
            or type(before) is not _gate.VerificationSnapshot
            or type(after) is not _gate.VerificationSnapshot
        ):
            raise _context_error("verification receipt identity differs")
        _validate_issued_verification_effect(effect, state)
        before.__post_init__()
        after.__post_init__()
        if not _gate._same_snapshot(
            before, request.before_snapshot
        ) or not _gate._same_snapshot(after, before):
            raise _context_error("verification receipt snapshot differs")
        _gate._validate_snapshot_approval(before, request.approval)
        _gate._validate_snapshot_approval(after, request.approval)
        if state.bound is None:
            raise _context_error("verification receipt approval is unavailable")
        profile = state.profile_resolver.resolve(request.profile_ref)
        if type(profile) is not _gate.VerificationProfile:
            raise _context_error("verification receipt profile differs")
        expected_request = _gate._build_request(state.bound, profile, before)
        if not _gate._same_request(expected_request, request):
            raise _context_error("verification receipt request differs")
        with _VERIFICATION_EFFECT_OWNERS_LOCK:
            owner_state = _VERIFICATION_EFFECT_OWNERS.get(state.owner)
        if (
            owner_state is None
            or owner_state.store_ref() is not store
            or owner_state.store_marker is not state.store_marker
            or owner_state.open_generation is not state.open_generation
        ):
            raise _context_error("verification receipt owner differs")
        if result is not state.committed_result:
            try:
                _gate._consume_runner_result_attestation(result, request, effect)
            except _gate.RecoveryRequired:
                if not callable(state.lifecycle_read_function):
                    raise _context_error(
                        "verification receipt replay is unavailable"
                    ) from None
                projection = state.lifecycle_read_function(
                    store,
                    state.snapshot.root_key,
                    str(verification_ref),
                )
                if (
                    type(projection) is not _VerificationLifecycleProjection
                    or projection.status != "RECEIPTED"
                    or projection.receipt_projection is None
                ):
                    raise _context_error(
                        "verification runner result provenance differs"
                    ) from None
        plan = _VerificationReceiptPlan(
            root_key=state.snapshot.root_key,
            verification_ref=str(verification_ref),
            request=request,
            request_snapshot=_gate._request_parts(request),
            effect=effect,
            effect_snapshot=_effect_value_snapshot(effect),
            result=result,
            result_snapshot=_result_value_snapshot(result),
            before=before,
            before_snapshot=_gate._snapshot_parts(before),
            after=after,
            after_snapshot=_gate._snapshot_parts(after),
            owner=state.owner,
        )
        command = object.__new__(VerificationReceiptRequest)
        for name, value in (
            ("root_key", plan.root_key),
            ("verification_ref", verification_ref),
            ("effect", effect),
            ("result", result),
            ("before", before),
            ("after", after),
            ("owner", state.owner),
            ("_issuer", _RECEIPT_REQUEST_ISSUER),
        ):
            object.__setattr__(command, name, value)
        with _VERIFICATION_RECEIPT_REQUESTS_LOCK:
            _VERIFICATION_RECEIPT_REQUESTS[command] = _VerificationReceiptRequestState(
                adapter=adapter,
                store_ref=ref(store),
                store_marker=state.store_marker,
                open_generation=state.open_generation,
                receipt_function=state.receipt_function,
                plan=plan,
            )
        return command
    except VerificationStoreError as exc:
        if _is_context_error(exc):
            raise
        raise _context_error("verification receipt request is invalid") from None
    except _BOUNDARY_EXCEPTION:
        raise _context_error("verification receipt request is invalid") from None


def _validate_verification_receipt_request(
    command: object,
    expected_store: object,
) -> _VerificationReceiptPlan:
    try:
        if (
            type(command) is not VerificationReceiptRequest
            or object.__getattribute__(command, "_issuer")
            is not _RECEIPT_REQUEST_ISSUER
        ):
            raise _context_error("verification receipt request type differs")
        with _VERIFICATION_RECEIPT_REQUESTS_LOCK:
            state = _VERIFICATION_RECEIPT_REQUESTS.get(command)
        if state is None:
            raise _context_error("verification receipt request is unavailable")
        adapter_state = state.adapter._state()
        plan = state.plan
        if (
            state.store_ref() is not expected_store
            or adapter_state.store_ref() is not expected_store
            or state.store_marker is not adapter_state.store_marker
            or state.open_generation is not adapter_state.open_generation
            or _registered_store_method(
                expected_store,
                "commit_verification_receipt",
                "_verification_receipt_function",
            )
            is not state.receipt_function
            or command.root_key != plan.root_key
            or str(command.verification_ref) != plan.verification_ref
            or command.effect is not plan.effect
            or command.result is not plan.result
            or command.before is not plan.before
            or command.after is not plan.after
            or command.owner is not plan.owner
        ):
            raise _context_error("verification receipt request provenance differs")
        _gate._validate_request(plan.request, verify_digest=True)
        _gate._validate_effect(command.effect)
        _gate._validate_result_for_request(
            command.result,
            plan.request,
            command.effect,
        )
        if (
            _gate._request_parts(plan.request) != plan.request_snapshot
            or _effect_value_snapshot(command.effect) != plan.effect_snapshot
            or _result_value_snapshot(command.result) != plan.result_snapshot
            or _gate._snapshot_parts(command.before) != plan.before_snapshot
            or _gate._snapshot_parts(command.after) != plan.after_snapshot
        ):
            raise _context_error("verification receipt request changed after issue")
        return plan
    except VerificationStoreError:
        raise
    except _BOUNDARY_EXCEPTION:
        raise _context_error("verification receipt request is invalid") from None


@dataclass(frozen=True, slots=True)
class _VerificationLifecycleProjection:
    snapshot: _ledger.ApprovalBindingSnapshotV1
    request_projection: _ledger.VerificationRequestProjectionV1
    status: str
    effect_owner: str | None
    effect_attempt: int | None
    effect_epoch: int | None
    effect_fence: int | None
    effect_nonce: str | None
    receipt_projection: _ledger.VerificationReceiptProjectionV1 | None
    revision_digest: str


def _effect_from_lifecycle_projection(
    projection: _VerificationLifecycleProjection,
    request: _gate.VerificationRequest,
    status: _gate.EffectBeginStatus,
) -> _gate.VerificationEffectLease:
    if (
        type(projection.effect_owner) is not str
        or projection.effect_attempt != 1
        or type(projection.effect_epoch) is not int
        or type(projection.effect_fence) is not int
        or type(projection.effect_nonce) is not str
    ):
        raise _context_error("verification effect projection is incomplete")
    return _gate._make_effect(
        _gate.VerificationRef(projection.request_projection.verification_id),
        _gate.ReceiptDigest(request.request_digest),
        _gate.EffectNonce(projection.effect_nonce),
        projection.effect_epoch,
        projection.effect_fence,
        status,
    )


def _receipt_from_projection(
    projection: _ledger.VerificationReceiptProjectionV1,
    request: _gate.VerificationRequest,
    effect: _gate.VerificationEffectLease,
) -> _gate.VerificationReceipt:
    result = _gate.VerificationRunResult(
        verification_ref=_gate.VerificationRef(projection.verification_ref),
        request_digest=_gate.ReceiptDigest(projection.request_digest),
        profile_ref=_task.VerificationProfileRef(projection.profile_ref),
        profile_identity=projection.profile_identity,
        profile_binding_digest=_gate.VerificationProfileBindingDigest(
            projection.profile_binding_digest
        ),
        executable_before=projection.executable_before,
        executable_after=projection.executable_after,
        effect_nonce=_gate.EffectNonce(projection.effect_nonce),
        lease_epoch=projection.lease_epoch,
        fencing_token=projection.fencing_token,
        argv_digest=_gate.ArgvDigest(projection.argv_digest),
        cwd=_task.WorkspaceIdentity(projection.cwd),
        environment_names=tuple(
            _gate.EnvName(name) for name in projection.environment_names
        ),
        result_schema=projection.result_schema,
        outcome=projection.outcome,
        exit_code=projection.exit_code,
        stdout_sha256=(
            None
            if projection.stdout_sha256 is None
            else _gate.OutputDigest(projection.stdout_sha256)
        ),
        stderr_sha256=(
            None
            if projection.stderr_sha256 is None
            else _gate.OutputDigest(projection.stderr_sha256)
        ),
        stdout_bytes=projection.stdout_bytes,
        stderr_bytes=projection.stderr_bytes,
        cleanup=projection.cleanup,
    )
    receipt = _gate._make_receipt(
        receipt_ref=_task.ReceiptRef(projection.receipt_ref),
        request=request,
        result=result,
        effect=effect,
        after_snapshot=projection.after_snapshot,
    )
    if (
        _ledger.verification_receipt_projection_from_receipt(receipt) != projection
        or receipt.receipt_digest != projection.receipt_digest
    ):
        raise _context_error("persisted verification receipt differs")
    return receipt


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class VerificationTerminalRequest:
    """Return-only terminal command for one exact durable receipt."""

    root_key: str
    verification_ref: _gate.VerificationRef
    receipt_ref: _task.ReceiptRef
    receipt_digest: _gate.ReceiptDigest
    owner: VerificationEffectOwner
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("verification terminal request is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("verification terminal request is return-only")

    def __copy__(self) -> NoReturn:
        raise TypeError("verification terminal request cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("verification terminal request cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("verification terminal request cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("verification terminal request cannot be pickled")


@dataclass(frozen=True, slots=True)
class _VerificationTerminalPlan:
    root_key: str
    verification_ref: str
    receipt_ref: str
    receipt_digest: str
    owner: VerificationEffectOwner


@dataclass(frozen=True, slots=True)
class _VerificationTerminalRequestState:
    adapter: StoreVerificationAdapter
    store_ref: ReferenceType[object]
    store_marker: object
    open_generation: object
    terminal_function: object
    plan: _VerificationTerminalPlan


_TERMINAL_REQUEST_ISSUER: Final = object()
_VERIFICATION_TERMINAL_REQUESTS: Final = WeakKeyDictionary[
    VerificationTerminalRequest, _VerificationTerminalRequestState
]()
_VERIFICATION_TERMINAL_REQUESTS_LOCK: Final = RLock()


def _issue_verification_terminal_request(
    adapter: StoreVerificationAdapter,
    verification_ref: _gate.VerificationRef,
    receipt_ref: _task.ReceiptRef,
    receipt_digest: _gate.ReceiptDigest,
) -> VerificationTerminalRequest:
    state = adapter._state()
    store = state.store_ref()
    if (
        store is None
        or type(verification_ref) is not str
        or type(receipt_ref) is not str
        or type(receipt_digest) is not str
        or state.prepared_request is None
        or str(verification_ref) != str(state.prepared_request.verification_id)
    ):
        raise _context_error("verification terminal identity differs")
    with _VERIFICATION_EFFECT_OWNERS_LOCK:
        owner_state = _VERIFICATION_EFFECT_OWNERS.get(state.owner)
    if owner_state is None or owner_state.store_ref() is not store:
        raise _context_error("verification terminal owner differs")
    plan = _VerificationTerminalPlan(
        root_key=state.snapshot.root_key,
        verification_ref=str(verification_ref),
        receipt_ref=str(receipt_ref),
        receipt_digest=str(receipt_digest),
        owner=state.owner,
    )
    command = object.__new__(VerificationTerminalRequest)
    for name, value in (
        ("root_key", plan.root_key),
        ("verification_ref", verification_ref),
        ("receipt_ref", receipt_ref),
        ("receipt_digest", receipt_digest),
        ("owner", state.owner),
        ("_issuer", _TERMINAL_REQUEST_ISSUER),
    ):
        object.__setattr__(command, name, value)
    with _VERIFICATION_TERMINAL_REQUESTS_LOCK:
        _VERIFICATION_TERMINAL_REQUESTS[command] = _VerificationTerminalRequestState(
            adapter=adapter,
            store_ref=ref(store),
            store_marker=state.store_marker,
            open_generation=state.open_generation,
            terminal_function=state.terminal_function,
            plan=plan,
        )
    return command


def _validate_verification_terminal_request(
    command: object,
    expected_store: object,
) -> _VerificationTerminalPlan:
    try:
        if (
            type(command) is not VerificationTerminalRequest
            or object.__getattribute__(command, "_issuer")
            is not _TERMINAL_REQUEST_ISSUER
        ):
            raise _context_error("verification terminal request type differs")
        with _VERIFICATION_TERMINAL_REQUESTS_LOCK:
            state = _VERIFICATION_TERMINAL_REQUESTS.get(command)
        if state is None:
            raise _context_error("verification terminal request is unavailable")
        adapter_state = state.adapter._state()
        plan = state.plan
        if (
            state.store_ref() is not expected_store
            or adapter_state.store_ref() is not expected_store
            or state.store_marker is not adapter_state.store_marker
            or state.open_generation is not adapter_state.open_generation
            or _registered_store_method(
                expected_store,
                "commit_verification_terminal",
                "_verification_terminal_function",
            )
            is not state.terminal_function
            or command.root_key != plan.root_key
            or str(command.verification_ref) != plan.verification_ref
            or str(command.receipt_ref) != plan.receipt_ref
            or str(command.receipt_digest) != plan.receipt_digest
            or command.owner is not plan.owner
        ):
            raise _context_error("verification terminal request provenance differs")
        return plan
    except VerificationStoreError:
        raise
    except _BOUNDARY_EXCEPTION:
        raise _context_error("verification terminal request is invalid") from None


@dataclass(
    frozen=True,
    slots=True,
    init=False,
    repr=False,
    eq=False,
    weakref_slot=True,
)
class VerificationUnknownRequest:
    """Return-only command for one exact armed recovery marker."""

    root_key: str
    verification_ref: _gate.VerificationRef
    request_digest: _gate.ReceiptDigest
    reason_code: str
    effect: _gate.VerificationEffectLease
    evidence_digest: str
    owner: VerificationEffectOwner
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("verification unknown request is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("verification unknown request is return-only")

    def __repr__(self) -> str:
        return "<VerificationUnknownRequest opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("verification unknown request cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("verification unknown request cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("verification unknown request cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("verification unknown request cannot be pickled")


@dataclass(frozen=True, slots=True)
class _VerificationUnknownPlan:
    root_key: str
    verification_ref: str
    request_digest: str
    reason_code: str
    effect: _gate.VerificationEffectLease
    effect_snapshot: tuple[object, ...]
    evidence_digest: str
    owner: VerificationEffectOwner


@dataclass(frozen=True, slots=True)
class _VerificationUnknownRequestState:
    adapter: StoreVerificationAdapter
    store_ref: ReferenceType[object]
    store_marker: object
    open_generation: object
    unknown_function: object
    plan: _VerificationUnknownPlan


_UNKNOWN_REQUEST_ISSUER: Final = object()
_VERIFICATION_UNKNOWN_REQUESTS: Final = WeakKeyDictionary[
    VerificationUnknownRequest, _VerificationUnknownRequestState
]()
_VERIFICATION_UNKNOWN_REQUESTS_LOCK: Final = RLock()


def _issue_verification_unknown_request(
    adapter: StoreVerificationAdapter,
    verification_ref: _gate.VerificationRef,
    request_digest: _gate.ReceiptDigest,
    *,
    reason_code: str,
    effect: _gate.VerificationEffectLease | None,
    evidence_digest: str | None,
) -> VerificationUnknownRequest:
    state = adapter._state()
    store = state.store_ref()
    request = state.prepared_request
    if (
        store is None
        or request is None
        or type(verification_ref) is not str
        or type(request_digest) is not str
        or type(reason_code) is not str
        or reason_code not in _UNKNOWN_CODES
        or type(evidence_digest) is not str
        or _WRAPPED_DIGEST.fullmatch(evidence_digest) is None
        or str(verification_ref) != str(request.verification_id)
        or str(request_digest) != str(request.request_digest)
    ):
        raise _context_error("verification unknown identity differs")
    selected_effect = effect
    reconstructed_effect = selected_effect is None
    if selected_effect is None:
        if not callable(state.lifecycle_read_function):
            raise _context_error("verification lifecycle read is unavailable")
        projection = state.lifecycle_read_function(
            store,
            state.snapshot.root_key,
            str(verification_ref),
        )
        if (
            type(projection) is not _VerificationLifecycleProjection
            or projection.status not in {"EFFECT_PREPARED", "UNKNOWN_EFFECT"}
            or projection.request_projection.verification_id != str(verification_ref)
            or projection.request_projection.request_digest != str(request_digest)
        ):
            raise _context_error("verification unknown effect is unavailable")
        selected_effect = _effect_from_lifecycle_projection(
            projection,
            request,
            _gate.EffectBeginStatus.RUN_ONCE,
        )
    _gate._validate_effect(selected_effect)
    if (
        selected_effect.status is not _gate.EffectBeginStatus.RUN_ONCE
        or str(selected_effect.verification_ref) != str(verification_ref)
        or str(selected_effect.request_digest) != str(request_digest)
    ):
        raise _context_error("verification unknown effect differs")
    if reconstructed_effect:
        _register_issued_verification_effect(selected_effect, state)
    else:
        _validate_issued_verification_effect(selected_effect, state)
    with _VERIFICATION_EFFECT_OWNERS_LOCK:
        owner_state = _VERIFICATION_EFFECT_OWNERS.get(state.owner)
    if (
        owner_state is None
        or owner_state.store_ref() is not store
        or owner_state.store_marker is not state.store_marker
        or owner_state.open_generation is not state.open_generation
    ):
        raise _context_error("verification unknown owner differs")
    plan = _VerificationUnknownPlan(
        root_key=state.snapshot.root_key,
        verification_ref=str(verification_ref),
        request_digest=str(request_digest),
        reason_code=reason_code,
        effect=selected_effect,
        effect_snapshot=_effect_value_snapshot(selected_effect),
        evidence_digest=evidence_digest,
        owner=state.owner,
    )
    command = object.__new__(VerificationUnknownRequest)
    for name, value in (
        ("root_key", plan.root_key),
        ("verification_ref", verification_ref),
        ("request_digest", request_digest),
        ("reason_code", reason_code),
        ("effect", selected_effect),
        ("evidence_digest", evidence_digest),
        ("owner", state.owner),
        ("_issuer", _UNKNOWN_REQUEST_ISSUER),
    ):
        object.__setattr__(command, name, value)
    with _VERIFICATION_UNKNOWN_REQUESTS_LOCK:
        _VERIFICATION_UNKNOWN_REQUESTS[command] = _VerificationUnknownRequestState(
            adapter=adapter,
            store_ref=ref(store),
            store_marker=state.store_marker,
            open_generation=state.open_generation,
            unknown_function=state.unknown_function,
            plan=plan,
        )
    return command


def _validate_verification_unknown_request(
    command: object,
    expected_store: object,
) -> _VerificationUnknownPlan:
    try:
        if (
            type(command) is not VerificationUnknownRequest
            or object.__getattribute__(command, "_issuer")
            is not _UNKNOWN_REQUEST_ISSUER
        ):
            raise _context_error("verification unknown request type differs")
        with _VERIFICATION_UNKNOWN_REQUESTS_LOCK:
            state = _VERIFICATION_UNKNOWN_REQUESTS.get(command)
        if state is None:
            raise _context_error("verification unknown request is unavailable")
        adapter_state = state.adapter._state()
        plan = state.plan
        if (
            state.store_ref() is not expected_store
            or adapter_state.store_ref() is not expected_store
            or state.store_marker is not adapter_state.store_marker
            or state.open_generation is not adapter_state.open_generation
            or _registered_store_method(
                expected_store,
                "commit_verification_unknown",
                "_verification_unknown_function",
            )
            is not state.unknown_function
            or command.root_key != plan.root_key
            or str(command.verification_ref) != plan.verification_ref
            or str(command.request_digest) != plan.request_digest
            or command.reason_code != plan.reason_code
            or command.effect is not plan.effect
            or command.evidence_digest != plan.evidence_digest
            or command.owner is not plan.owner
        ):
            raise _context_error("verification unknown request provenance differs")
        _gate._validate_effect(command.effect)
        if (
            _effect_value_snapshot(command.effect) != plan.effect_snapshot
            or plan.reason_code not in _UNKNOWN_CODES
            or _WRAPPED_DIGEST.fullmatch(plan.evidence_digest) is None
        ):
            raise _context_error("verification unknown request changed after issue")
        return plan
    except VerificationStoreError:
        raise
    except _BOUNDARY_EXCEPTION:
        raise _context_error("verification unknown request is invalid") from None


VERIFICATION_RECORD_PREIMAGE_FIELDS: Final = (
    "root_key",
    "verification_ref",
    "record_version",
    "approval_binding_version",
    "approval_binding_bytes",
    "approval_binding_digest",
    "request_schema_version",
    "approval_ref",
    "approval_digest",
    "review_ref",
    "review_digest",
    "completion_ref",
    "completion_digest",
    "request_bytes",
    "request_digest",
    "run_id",
    "main_terminal_id",
    "task_id",
    "dispatch_id",
    "attempt_id",
    "worker_node",
    "reviewer_node",
    "worker_terminal_id",
    "reviewer_terminal_id",
    "team_id",
    "workspace",
    "review_round",
    "task_sequence_before",
    "task_sequence_after",
    "task_digest_before",
    "task_digest_after",
    "workflow_sequence_before",
    "workflow_sequence_after",
    "workflow_digest_before",
    "workflow_digest_after",
    "status",
    "effect_owner",
    "effect_attempt",
    "effect_epoch",
    "effect_fence",
    "effect_nonce",
    "receipt_ref",
    "receipt_digest",
    "terminal_phase",
    "terminal_receipt_ref",
    "terminal_receipt_digest",
    "unknown_code",
    "unknown_evidence_digest",
    "prepare_event_id",
    "prepare_event_digest",
    "receipt_event_id",
    "receipt_event_digest",
    "terminal_event_id",
    "terminal_event_digest",
    "unknown_event_id",
    "unknown_event_digest",
    "created_ns",
    "updated_ns",
)

_RECORD_DIGEST_FIELD: Final = "record_digest"
_VERIFICATION_RECORD_COLUMNS: Final = (
    *VERIFICATION_RECORD_PREIMAGE_FIELDS[:15],
    _RECORD_DIGEST_FIELD,
    *VERIFICATION_RECORD_PREIMAGE_FIELDS[15:],
)


def _validate_record_mapping(mapping: Mapping[str, object]) -> None:
    """Require the exact DDL projection and its insertion order."""

    if type(mapping) is not dict:
        msg = "verification record input must be an exact dict"
        raise TypeError(msg)

    keys = tuple(mapping)
    if len(keys) != len(_VERIFICATION_RECORD_COLUMNS):
        msg = "verification record input has an invalid field set"
        raise ValueError(msg)
    for actual, expected in zip(keys, _VERIFICATION_RECORD_COLUMNS, strict=True):
        if type(actual) is not str or actual != expected:
            msg = "verification record input is not in canonical DDL order"
            raise ValueError(msg)


def _append_value(frame: bytearray, field: str, value: object) -> None:
    """Append one explicitly typed value to a canonical record frame."""

    if value is None:
        frame.extend(b"N")
        return

    value_type = type(value)
    if value_type is int:
        integer_value = cast(int, value)
        if integer_value < _MIN_INT64 or integer_value > _MAX_INT64:
            msg = f"verification record integer is outside signed int64: {field}"
            raise OverflowError(msg)
        frame.extend(b"I")
        frame.extend(struct.pack(">q", integer_value))
        return

    if value_type is str:
        text_value = cast(str, value)
        text_bytes = text_value.encode("utf-8")
        frame.extend(b"T")
        frame.extend(struct.pack(">Q", len(text_bytes)))
        frame.extend(text_bytes)
        return

    if value_type is bytes:
        binary_value = cast(bytes, value)
        frame.extend(b"B")
        frame.extend(struct.pack(">Q", len(binary_value)))
        frame.extend(binary_value)
        return

    msg = f"unsupported verification record value for {field}: {value_type!r}"
    raise TypeError(msg)


def _verification_record_frame(mapping: Mapping[str, object]) -> bytes:
    """Return the fixed, self-delimited preimage frame for a record mapping."""

    _validate_record_mapping(mapping)
    frame = bytearray(struct.pack(">I", len(VERIFICATION_RECORD_PREIMAGE_FIELDS)))
    for field in VERIFICATION_RECORD_PREIMAGE_FIELDS:
        field_bytes = field.encode("utf-8")
        frame.extend(struct.pack(">I", len(field_bytes)))
        frame.extend(field_bytes)
        _append_value(frame, field, mapping[field])
    return bytes(frame)


def _verification_record_digest(mapping: Mapping[str, object]) -> str:
    """Return the domain-separated SHA-256 digest for a record preimage."""

    digest = hashlib.sha256(
        _RECORD_DIGEST_DOMAIN + _verification_record_frame(mapping)
    ).hexdigest()
    return "sha256:" + digest


def _identifier(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact string")
    candidate = value
    if not candidate or candidate.strip() != candidate or len(candidate) > 4096:
        raise ValueError(f"{name} is invalid")
    candidate.encode("utf-8")
    return candidate


def _bare_digest(value: object, name: str) -> str:
    if type(value) is not str or _BARE_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bare SHA-256 digest")
    return value


def _wrapped_digest(value: object, name: str) -> str:
    if type(value) is not str or _WRAPPED_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a wrapped SHA-256 digest")
    return value


def _sequence(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact integer")
    candidate = value
    minimum = 1 if positive else 0
    if candidate < minimum or candidate > _MAX_INT64:
        raise ValueError(f"{name} is outside the supported range")
    return candidate


def _stage(value: object) -> VerificationStage:
    if type(value) is not VerificationStage:
        raise TypeError("verification stage must be exact")
    return value


def _event_wrapper_digest(domain: bytes, values: tuple[object, ...]) -> str:
    frame = bytearray(struct.pack(">I", len(values)))
    for value in values:
        if type(value) is str:
            encoded = value.encode("utf-8")
        elif type(value) is int:
            encoded = str(value).encode("ascii")
        else:
            raise TypeError("verification wrapper value type is invalid")
        frame.extend(struct.pack(">Q", len(encoded)))
        frame.extend(encoded)
    return "sha256:" + hashlib.sha256(domain + frame).hexdigest()


def _verification_request_wrapper(
    stage: VerificationStage,
    root_key: str,
    verification_ref: str,
    workflow_sequence_before: int,
    workflow_sequence_after: int,
    task_sequence_before: int,
    task_sequence_after: int,
    request_digest: str,
) -> str:
    """Wrap one bare request digest for a workflow verification event."""

    selected_stage = _stage(stage)
    values: tuple[object, ...] = (
        selected_stage.value,
        _identifier(root_key, "root_key"),
        _identifier(verification_ref, "verification_ref"),
        _sequence(workflow_sequence_before, "workflow_sequence_before"),
        _sequence(workflow_sequence_after, "workflow_sequence_after"),
        _sequence(task_sequence_before, "task_sequence_before"),
        _sequence(task_sequence_after, "task_sequence_after"),
        _bare_digest(request_digest, "request_digest"),
    )
    return _event_wrapper_digest(_REQUEST_WRAPPER_DOMAIN, values)


def _verification_evidence_wrapper(
    stage: VerificationStage,
    root_key: str,
    verification_ref: str,
    workflow_sequence_before: int,
    workflow_sequence_after: int,
    task_sequence_before: int,
    task_sequence_after: int,
    *source: object,
) -> str:
    """Wrap one stage-specific evidence source for checkpoint/event binding."""

    selected_stage = _stage(stage)
    common: tuple[object, ...] = (
        selected_stage.value,
        _identifier(root_key, "root_key"),
        _identifier(verification_ref, "verification_ref"),
        _sequence(workflow_sequence_before, "workflow_sequence_before"),
        _sequence(workflow_sequence_after, "workflow_sequence_after"),
        _sequence(task_sequence_before, "task_sequence_before"),
        _sequence(task_sequence_after, "task_sequence_after"),
    )
    stage_source: tuple[object, ...]
    if selected_stage is VerificationStage.PREPARE:
        if len(source) != 1:
            raise ValueError("prepare evidence source arity is invalid")
        stage_source = (_wrapped_digest(source[0], "approval_binding_digest"),)
    elif selected_stage is VerificationStage.RECEIPT:
        if len(source) != 1:
            raise ValueError("receipt evidence source arity is invalid")
        stage_source = (_bare_digest(source[0], "receipt_digest"),)
    elif selected_stage is VerificationStage.TERMINAL:
        if len(source) != 3:
            raise ValueError("terminal evidence source arity is invalid")
        phase = _identifier(source[0], "terminal_phase")
        if phase not in {"completed", "verification_failed"}:
            raise ValueError("terminal phase is invalid")
        stage_source = (
            phase,
            _identifier(source[1], "terminal_receipt_ref"),
            _bare_digest(source[2], "terminal_receipt_digest"),
        )
    else:
        if len(source) != 3:
            raise ValueError("unknown evidence source arity is invalid")
        code = _identifier(source[0], "unknown_code")
        if code not in _UNKNOWN_CODES:
            raise ValueError("unknown code is invalid")
        stage_source = (
            code,
            _wrapped_digest(source[1], "unknown_evidence_digest"),
            _sequence(source[2], "effect_fence", positive=True),
        )
    return _event_wrapper_digest(
        _EVIDENCE_WRAPPER_DOMAINS[selected_stage.value],
        (*common, *stage_source),
    )
