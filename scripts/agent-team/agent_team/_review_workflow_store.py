"""Package-private typed boundary for schema-4 review checkpoint commits.

The module owns no SQLite connection and invokes no external effect.  It turns
one owner-bound review-policy update into a complete Store plan while keeping
checkpoint construction, SQL, clocks, and transaction ownership in
``store.py``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Final, NoReturn, Protocol, SupportsIndex, cast
from weakref import WeakKeyDictionary

from . import policy_verification_handoff as _handoff
from . import review_policy as _review
from . import task_policy as _task
from . import task_verification_ledger as _task_ledger
from . import workflow_store as _workflow

REVIEW_POLICY_ACTOR: Final = "review-policy-producer-v1"
_AUTHORITY_DIGEST_DOMAIN: Final = b"agent-team/workflow-review-authority/v1\0"
_REQUEST_DIGEST_DOMAIN: Final = b"agent-team/workflow-review-request/v1\0"
_REVIEW_REFERENCE: Final = re.compile(r"review-authority-([0-9a-f]{64})\Z")
_STORE_PORT_EXCEPTION: Final[type[Exception]] = Exception


class ReviewCheckpointError(ValueError):
    """A bounded typed review producer input is not admissible."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _error(code: str, message: str) -> ReviewCheckpointError:
    return ReviewCheckpointError(code, message)


def _framed_digest(domain: bytes, parts: tuple[str, ...]) -> str:
    if type(domain) is not bytes or not domain.endswith(b"\0"):
        raise _error("digest-domain", "review digest domain is invalid")
    digest = hashlib.sha256()
    digest.update(domain)
    digest.update(len(parts).to_bytes(4, "big"))
    for part in parts:
        if type(part) is not str:
            raise _error("digest-part", "review digest part is invalid")
        try:
            encoded = part.encode("utf-8")
        except UnicodeEncodeError:
            raise _error("digest-part", "review digest part is invalid") from None
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return "sha256:" + digest.hexdigest()


class ReviewPolicyEdge(str, Enum):
    WORKER_COMPLETION = "worker_completion"
    REVIEW_REQUEST = "review_request"
    APPROVED_DECISION = "approved_decision"


@dataclass(frozen=True, slots=True)
class StoredTaskPolicyState:
    root_key: str
    state: _task.TaskPolicyStateV4
    state_bytes: bytes
    state_digest: str
    run_id: str
    updated_ns: int

    def __post_init__(self) -> None:
        _workflow._require_identifier(self.root_key, "task row root_key")
        if type(self.state) is not _task.TaskPolicyStateV4:
            raise _error("task-state", "stored task state type is invalid")
        if type(self.state_bytes) is not bytes:
            raise _error("task-state", "stored task bytes are invalid")
        decoded = _task_ledger.decode_task_state(self.state_bytes)
        if decoded != self.state:
            raise _error("task-state", "stored task bytes differ from state")
        expected_digest = str(_task_ledger.task_state_digest(self.state_bytes))
        if type(self.state_digest) is not str or self.state_digest != expected_digest:
            raise _error("task-digest", "stored task digest differs")
        _workflow._require_identifier(self.run_id, "task row run_id")
        _workflow._require_int(self.updated_ns, "task row updated_ns")


@dataclass(frozen=True, slots=True)
class ReviewPolicyEventObservation:
    workflow_event_id: int
    workflow_event_schema_version: int
    root_key: str
    operation_id: str | None
    workflow_sequence: int
    task_sequence_before: int
    task_sequence_after: int
    from_state: str
    to_state: str
    kind: str
    actor: str
    clock_ns: int
    request_digest: str
    receipt_id: str | None
    checkpoint_bytes: bytes
    checkpoint_digest: str
    evidence_ref: str
    event_digest: str

    def __post_init__(self) -> None:
        _workflow._require_int(
            self.workflow_event_id,
            "review event id",
            minimum=1,
        )
        if (
            type(self.workflow_event_schema_version) is not int
            or self.workflow_event_schema_version
            != _workflow.WORKFLOW_EVENT_SCHEMA_VERSION
        ):
            raise _error("event-version", "review event version is invalid")
        for value, name in (
            (self.root_key, "review event root_key"),
            (self.from_state, "review event from_state"),
            (self.to_state, "review event to_state"),
            (self.kind, "review event kind"),
            (self.actor, "review event actor"),
            (self.request_digest, "review event request_digest"),
            (self.checkpoint_digest, "review event checkpoint_digest"),
            (self.evidence_ref, "review event evidence_ref"),
            (self.event_digest, "review event event_digest"),
        ):
            if type(value) is not str:
                raise _error("event-scalar", f"{name} type is invalid")
        _workflow._require_identifier(self.root_key, "review event root_key")
        if self.operation_id is not None or self.receipt_id is not None:
            raise _error("event-effect", "review event must be null-operation")
        _workflow._require_int(
            self.workflow_sequence,
            "review event workflow_sequence",
            minimum=1,
        )
        _workflow._require_int(
            self.task_sequence_before,
            "review event task_sequence_before",
        )
        _workflow._require_int(
            self.task_sequence_after,
            "review event task_sequence_after",
        )
        if self.task_sequence_after != self.task_sequence_before + 1:
            raise _error("event-sequence", "review event task sequence differs")
        _workflow.CheckpointState(self.from_state)
        _workflow.CheckpointState(self.to_state)
        if self.kind != _workflow.TransitionKind.POLICY.value:
            raise _error("event-kind", "review event kind is invalid")
        if self.actor != REVIEW_POLICY_ACTOR:
            raise _error("event-actor", "review event actor is invalid")
        _workflow._require_int(self.clock_ns, "review event clock_ns")
        for value, name in (
            (self.request_digest, "review event request_digest"),
            (self.checkpoint_digest, "review event checkpoint_digest"),
            (self.evidence_ref, "review event evidence_ref"),
            (self.event_digest, "review event event_digest"),
        ):
            _workflow._require_digest(value, name)
        if type(self.checkpoint_bytes) is not bytes or not self.checkpoint_bytes:
            raise _error(
                "event-checkpoint", "review event checkpoint bytes are invalid"
            )


@dataclass(frozen=True, slots=True)
class ReviewCheckpointObservation:
    checkpoint: _workflow.WorkflowCheckpointV4
    task: StoredTaskPolicyState
    events: tuple[ReviewPolicyEventObservation, ...]
    predecessor_checkpoint_bytes: bytes
    verification_operation_count: int
    verification_receipt_count: int

    def __post_init__(self) -> None:
        if type(self.checkpoint) is not _workflow.WorkflowCheckpointV4:
            raise _error("checkpoint", "review checkpoint type is invalid")
        if type(self.task) is not StoredTaskPolicyState:
            raise _error("task-state", "review task observation is invalid")
        if type(self.events) is not tuple or not 1 <= len(self.events) <= 3:
            raise _error("event-count", "review event suffix is invalid")
        if any(type(item) is not ReviewPolicyEventObservation for item in self.events):
            raise _error("event-type", "review event observation type is invalid")
        if (
            type(self.predecessor_checkpoint_bytes) is not bytes
            or not self.predecessor_checkpoint_bytes
        ):
            raise _error(
                "event-predecessor",
                "review event predecessor checkpoint is invalid",
            )
        if (
            type(self.verification_operation_count) is not int
            or type(self.verification_receipt_count) is not int
            or self.verification_operation_count != 0
            or self.verification_receipt_count != 0
        ):
            raise _error("verification-row", "verification rows are not empty")


class ReviewPolicyCommitRequest:
    """Return-only request issued by one bound producer for one Store."""

    __slots__ = ("__weakref__", "_issuer", "binding", "current")

    current: _workflow.WorkflowCheckpointV4
    binding: _handoff._ReviewAuthorityBinding
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("review policy commit request is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("review policy commit request is return-only")

    def __repr__(self) -> str:
        return "<ReviewPolicyCommitRequest opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("review policy commit request cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("review policy commit request cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("review policy commit request cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("review policy commit request cannot be pickled")


_REVIEW_REQUEST_ISSUER: Final = object()
_REVIEW_REQUEST_BINDINGS: WeakKeyDictionary[
    ReviewPolicyCommitRequest, tuple[object, object, object, object]
] = WeakKeyDictionary()
_REVIEW_REQUEST_BINDINGS_LOCK: Final = RLock()
_REVIEW_PRODUCER_BINDINGS: WeakKeyDictionary[object, tuple[object, object, object]] = (
    WeakKeyDictionary()
)
_REVIEW_PRODUCER_BINDINGS_LOCK: Final = RLock()


@dataclass(frozen=True, slots=True)
class _RegisteredReviewStore:
    commit_function: object
    read_function: object
    event_observation_function: object
    error_type: type[BaseException]
    cleanup_capability_type: type[object]
    state_root_path: str
    state_root_device: int
    state_root_inode: int
    checkpoint_issuer: object


_REGISTERED_REVIEW_STORES: WeakKeyDictionary[object, _RegisteredReviewStore] = (
    WeakKeyDictionary()
)
_REGISTERED_REVIEW_STORES_LOCK: Final = RLock()


def _review_store_method_function(store: object, name: str) -> object:
    function: object | None = None
    owner: object | None = None
    binding_failed = False
    try:
        method = object.__getattribute__(store, name)
        owner = object.__getattribute__(method, "__self__")
        function = object.__getattribute__(method, "__func__")
    except (AttributeError, TypeError):
        binding_failed = True
    if binding_failed:
        raise _error("store", "review Store method binding is invalid")
    if owner is not store or not callable(function):
        raise _error("store", "review Store method binding is invalid")
    return function


def _review_store_function(store: object, name: str) -> object:
    function: object | None = None
    binding_failed = False
    try:
        function = object.__getattribute__(store, name)
    except (AttributeError, TypeError):
        binding_failed = True
    if binding_failed or not callable(function):
        raise _error("store", "review Store function binding is invalid")
    return function


def _register_review_workflow_store(
    store: object,
    store_error_type: type[BaseException],
    cleanup_capability_type: type[object],
    *,
    commit_function: object,
    read_function: object,
    event_observation_function: object,
    state_root_path: str,
    state_root_identity: tuple[int, int],
    checkpoint_issuer: object,
) -> None:
    if (
        type(store_error_type) is not type
        or not issubclass(store_error_type, BaseException)
        or type(cleanup_capability_type) is not type
    ):
        raise TypeError("review Store error type is invalid")
    if (
        _review_store_method_function(store, "commit_review_policy")
        is not commit_function
        or _review_store_method_function(store, "load_review_checkpoint")
        is not read_function
        or _review_store_function(store, "_review_event_observation")
        is not event_observation_function
        or not callable(event_observation_function)
        or type(state_root_path) is not str
        or not state_root_path
        or type(state_root_identity) is not tuple
        or len(state_root_identity) != 2
        or any(type(value) is not int or value < 0 for value in state_root_identity)
        or checkpoint_issuer is None
    ):
        raise _error("store", "review Store registration is invalid")
    with _REGISTERED_REVIEW_STORES_LOCK:
        _REGISTERED_REVIEW_STORES[store] = _RegisteredReviewStore(
            commit_function=commit_function,
            read_function=read_function,
            event_observation_function=event_observation_function,
            error_type=store_error_type,
            cleanup_capability_type=cleanup_capability_type,
            state_root_path=state_root_path,
            state_root_device=state_root_identity[0],
            state_root_inode=state_root_identity[1],
            checkpoint_issuer=checkpoint_issuer,
        )


def _registered_review_workflow_store(
    store: object,
) -> _RegisteredReviewStore | None:
    with _REGISTERED_REVIEW_STORES_LOCK:
        return _REGISTERED_REVIEW_STORES.get(store)


def _validate_registered_review_store_surface(
    store: object,
) -> _RegisteredReviewStore | None:
    registration = _registered_review_workflow_store(store)
    if registration is None:
        return None
    if (
        _review_store_method_function(store, "commit_review_policy")
        is not registration.commit_function
        or _review_store_method_function(store, "load_review_checkpoint")
        is not registration.read_function
        or _review_store_function(store, "_review_event_observation")
        is not registration.event_observation_function
    ):
        raise _error("store", "review Store method binding differs")
    return registration


def _registered_store_error_has_cleanup(
    error: BaseException,
    registration: _RegisteredReviewStore,
) -> bool:
    if not isinstance(error, registration.error_type):
        return False
    capability: object | None = None
    retry_cleanup: object | None = None
    try:
        capability = object.__getattribute__(error, "_cleanup_capability")
        retry_cleanup = object.__getattribute__(error, "retry_cleanup")
    except _STORE_PORT_EXCEPTION:
        return False
    return type(capability) is registration.cleanup_capability_type and callable(
        retry_cleanup
    )


def _validate_review_checkpoint_producer(
    producer: object,
) -> tuple[_handoff.PolicyVerificationHandoff, ReviewWorkflowStorePort]:
    try:
        if type(producer) is not ReviewCheckpointProducer:
            raise _error("producer", "review producer type is invalid")
        handoff = object.__getattribute__(producer, "_handoff")
        store = object.__getattribute__(producer, "_store")
        handoff_store = object.__getattribute__(handoff, "_store")
        with _REVIEW_PRODUCER_BINDINGS_LOCK:
            binding = _REVIEW_PRODUCER_BINDINGS.get(producer)
        if (
            binding is None
            or binding[0] is not handoff
            or binding[1] is not store
            or binding[2] is not handoff_store
            or type(handoff) is not _handoff.PolicyVerificationHandoff
        ):
            raise _error("producer", "review producer binding differs")
        _validate_registered_review_store_surface(store)
        return handoff, cast(ReviewWorkflowStorePort, store)
    except ReviewCheckpointError:
        raise
    except (AttributeError, TypeError):
        raise _error("producer", "review producer is invalid") from None


def _issue_review_policy_commit_request(
    producer: object,
    current: _workflow.WorkflowCheckpointV4,
    binding: _handoff._ReviewAuthorityBinding,
) -> ReviewPolicyCommitRequest:
    if (
        type(producer) is not ReviewCheckpointProducer
        or type(current) is not _workflow.WorkflowCheckpointV4
        or type(binding) is not _handoff._ReviewAuthorityBinding
    ):
        raise _error("request", "review policy request input is invalid")
    handoff, store = _validate_review_checkpoint_producer(producer)
    try:
        evidence = _handoff._validate_review_authority_binding(binding)
    except _handoff.PolicyVerificationHandoffError:
        raise _error("request", "review authority binding is invalid") from None
    if evidence.owner is not handoff:
        raise _error("request", "review authority owner differs")
    result = object.__new__(ReviewPolicyCommitRequest)
    object.__setattr__(result, "current", current)
    object.__setattr__(result, "binding", binding)
    object.__setattr__(result, "_issuer", _REVIEW_REQUEST_ISSUER)
    with _REVIEW_REQUEST_BINDINGS_LOCK:
        _REVIEW_REQUEST_BINDINGS[result] = (producer, current, binding, store)
    return result


def _validate_review_policy_commit_request(
    request: object,
    expected_store: object,
) -> tuple[_workflow.WorkflowCheckpointV4, _handoff._ReviewAuthorityBinding]:
    try:
        if type(request) is not ReviewPolicyCommitRequest:
            raise _error("request", "review policy request type is invalid")
        if object.__getattribute__(request, "_issuer") is not _REVIEW_REQUEST_ISSUER:
            raise _error("request", "review policy request issuer is invalid")
        current = object.__getattribute__(request, "current")
        binding = object.__getattribute__(request, "binding")
        with _REVIEW_REQUEST_BINDINGS_LOCK:
            state = _REVIEW_REQUEST_BINDINGS.get(request)
        if state is None:
            raise _error("request", "review policy request binding is unavailable")
        producer, issued_current, issued_binding, issued_store = state
        producer_handoff, producer_store = _validate_review_checkpoint_producer(
            producer
        )
        evidence = _handoff._validate_review_authority_binding(binding)
        if (
            issued_current is not current
            or issued_binding is not binding
            or issued_store is not expected_store
            or producer_store is not expected_store
            or evidence.owner is not producer_handoff
            or type(current) is not _workflow.WorkflowCheckpointV4
            or type(binding) is not _handoff._ReviewAuthorityBinding
        ):
            raise _error("request", "review policy request binding differs")
        return current, binding
    except ReviewCheckpointError:
        raise
    except (
        AttributeError,
        KeyError,
        TypeError,
        _handoff.PolicyVerificationHandoffError,
    ):
        raise _error("request", "review policy request is invalid") from None


@dataclass(frozen=True, slots=True)
class ReviewPolicyCommitResult:
    checkpoint: _workflow.WorkflowCheckpointV4
    task: StoredTaskPolicyState
    event: ReviewPolicyEventObservation
    reviewer_assignment: _review.ReviewerAssignment | None
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.checkpoint) is not _workflow.WorkflowCheckpointV4:
            raise _error("checkpoint", "committed review checkpoint is invalid")
        if type(self.task) is not StoredTaskPolicyState:
            raise _error("task-state", "committed task observation is invalid")
        if type(self.event) is not ReviewPolicyEventObservation:
            raise _error("event", "committed review event is invalid")
        if (
            self.reviewer_assignment is not None
            and type(self.reviewer_assignment) is not _review.ReviewerAssignment
        ):
            raise _error("reviewer-intent", "reviewer assignment intent is invalid")
        if type(self.replayed) is not bool:
            raise _error("replay", "review replay marker is invalid")


class ReviewWorkflowStorePort(Protocol):
    def commit_review_policy(
        self,
        request: ReviewPolicyCommitRequest,
    ) -> ReviewPolicyCommitResult: ...

    def load_review_checkpoint(
        self,
        key: _workflow.WorkflowRootKey,
    ) -> ReviewCheckpointObservation | None: ...


@dataclass(frozen=True, slots=True)
class _ReviewPolicyPlan:
    current: _workflow.WorkflowCheckpointV4
    update: _review.ReviewPolicyUpdate
    edge: ReviewPolicyEdge
    before_state: _task.TaskPolicyStateV4
    before_state_bytes: bytes
    before_state_digest: str
    after_state: _task.TaskPolicyStateV4
    after_state_bytes: bytes
    after_state_digest: str
    next_workflow_state: _workflow.CheckpointState
    authority: _workflow.AuthorityReference
    request_digest: str
    reviewer_assignment: _review.ReviewerAssignment | None


def _exact_edge(
    update: _review.ReviewPolicyUpdate,
) -> tuple[
    ReviewPolicyEdge,
    _workflow.CheckpointState,
    _workflow.CheckpointState,
    _review.ReviewerAssignment | None,
]:
    previous_phase = update.previous_state.task_state.phase
    next_phase = update.next_state.task_state.phase
    event = update.event
    if type(event) is _review.WorkerCompletion:
        if (
            event.kind is not _review.WorkerCompletionKind.SUCCEEDED
            or previous_phase is not _task.TaskPhase.ASSIGNED
            or next_phase is not _task.TaskPhase.WORKER_DONE
            or update.effects != ()
        ):
            raise _error("edge", "worker completion edge is invalid")
        return (
            ReviewPolicyEdge.WORKER_COMPLETION,
            _workflow.CheckpointState.WORKER_DONE,
            _workflow.CheckpointState.WORKER_DONE,
            None,
        )
    if type(event) is _review.ReviewRequest:
        if (
            previous_phase is not _task.TaskPhase.WORKER_DONE
            or next_phase is not _task.TaskPhase.REVIEW_PENDING
            or type(update.effects) is not tuple
            or len(update.effects) != 1
            or type(update.effects[0]) is not _review.ReviewerAssignment
        ):
            raise _error("edge", "review request edge is invalid")
        return (
            ReviewPolicyEdge.REVIEW_REQUEST,
            _workflow.CheckpointState.WORKER_DONE,
            _workflow.CheckpointState.REVIEW_PENDING,
            update.effects[0],
        )
    if type(event) is _review.ReviewDecision:
        if (
            event.kind is not _review.ReviewDecisionKind.APPROVED
            or previous_phase is not _task.TaskPhase.REVIEW_PENDING
            or next_phase is not _task.TaskPhase.APPROVED
            or update.effects != ()
        ):
            raise _error("edge", "approved decision edge is invalid")
        return (
            ReviewPolicyEdge.APPROVED_DECISION,
            _workflow.CheckpointState.REVIEW_PENDING,
            _workflow.CheckpointState.REVIEW_PENDING,
            None,
        )
    raise _error("edge", "review policy event type is invalid")


def _review_policy_request_digest(
    edge: ReviewPolicyEdge,
    current: _workflow.WorkflowCheckpointV4,
    authority: _workflow.AuthorityReference,
) -> str:
    """Bind one review request to its exact prior checkpoint and authority."""

    if (
        type(edge) is not ReviewPolicyEdge
        or type(current) is not _workflow.WorkflowCheckpointV4
        or type(authority) is not _workflow.AuthorityReference
        or current.task_sequence is None
    ):
        raise _error("request-digest", "review request digest input is invalid")
    previous_authority = current.review_authority
    return _framed_digest(
        _REQUEST_DIGEST_DOMAIN,
        (
            edge.value,
            current.root.root_key,
            str(current.workflow_sequence),
            str(current.task_sequence),
            current.checkpoint_digest,
            "" if previous_authority is None else previous_authority.reference,
            "" if previous_authority is None else previous_authority.digest,
            authority.reference,
            authority.digest,
        ),
    )


def _review_owner_digest_from_reference(reference: str) -> str:
    if type(reference) is not str:
        raise _error("authority-reference", "review authority reference is invalid")
    match = _REVIEW_REFERENCE.fullmatch(reference)
    if match is None:
        raise _error("authority-reference", "review authority reference is invalid")
    return match.group(1)


def _review_policy_authority_reference(
    edge: ReviewPolicyEdge,
    current: _workflow.WorkflowCheckpointV4,
    *,
    review_reference: str,
    review_digest: str,
    before_task_digest: str,
    after_task_digest: str,
) -> _workflow.AuthorityReference:
    if _review_owner_digest_from_reference(review_reference) != review_digest:
        raise _error("authority-reference", "review owner digest differs")
    for value, name in (
        (before_task_digest, "before task digest"),
        (after_task_digest, "after task digest"),
    ):
        _workflow._require_digest(value, name)
    authority_digest = _framed_digest(
        _AUTHORITY_DIGEST_DOMAIN,
        (
            edge.value,
            current.root.root_key,
            str(current.workflow_sequence),
            str(current.task_sequence),
            current.checkpoint_digest,
            review_reference,
            review_digest,
            before_task_digest,
            after_task_digest,
        ),
    )
    return _workflow.AuthorityReference(
        reference=review_reference,
        digest=authority_digest,
    )


def _plan_review_policy_request(
    request: ReviewPolicyCommitRequest,
    expected_store: object,
) -> _ReviewPolicyPlan:
    current, binding = _validate_review_policy_commit_request(
        request,
        expected_store,
    )
    try:
        evidence = _handoff._validate_review_authority_binding(binding)
    except _handoff.PolicyVerificationHandoffError:
        raise _error("authority", "review authority binding is invalid") from None
    update = evidence.update
    if (
        type(update) is not _review.ReviewPolicyUpdate
        or type(update.previous_state) is not _review.ReviewPolicyState
        or type(update.next_state) is not _review.ReviewPolicyState
        or type(update.previous_state.task_state) is not _task.TaskPolicyStateV4
        or type(update.next_state.task_state) is not _task.TaskPolicyStateV4
    ):
        raise _error("update", "review policy update type is invalid")
    edge, expected_workflow_state, next_workflow_state, reviewer_assignment = (
        _exact_edge(update)
    )
    before_state = _task_ledger.decode_task_state(
        _task_ledger.encode_task_state(update.previous_state.task_state)
    )
    after_state = _task_ledger.decode_task_state(
        _task_ledger.encode_task_state(update.next_state.task_state)
    )
    before_bytes = _task_ledger.encode_task_state(before_state)
    after_bytes = _task_ledger.encode_task_state(after_state)
    before_digest = str(_task_ledger.task_state_digest(before_bytes))
    after_digest = str(_task_ledger.task_state_digest(after_bytes))
    if (
        update.expected_sequence != before_state.sequence
        or after_state.sequence != before_state.sequence + 1
        or current.workflow_state is not expected_workflow_state
        or current.task_sequence != before_state.sequence
        or update.previous_state.run_id != current.run.run_id
        or str(before_state.team_id) != current.root.team_id
        or str(before_state.workspace) != current.root.workspace_path
        or current.verification_authority is not None
    ):
        raise _error("current", "review policy current identity differs")
    current_reference = current.task_policy
    if (
        current_reference is None
        or current_reference.version != before_state.version
        or current_reference.team_id != str(before_state.team_id)
        or current_reference.workspace != str(before_state.workspace)
        or current_reference.task_id != str(before_state.task_id)
        or current_reference.sequence != before_state.sequence
        or current_reference.state_digest != before_digest
    ):
        raise _error("current-task", "review current task reference differs")
    if edge is ReviewPolicyEdge.WORKER_COMPLETION:
        if current.review_authority is not None:
            raise _error("current-authority", "initial review authority is not empty")
    elif current.review_authority is None:
        raise _error("current-authority", "prior review authority is missing")

    review_ref = evidence.review_ref
    authority = _review_policy_authority_reference(
        edge,
        current,
        review_reference=review_ref.reference,
        review_digest=review_ref.digest,
        before_task_digest=before_digest,
        after_task_digest=after_digest,
    )
    request_digest = _review_policy_request_digest(edge, current, authority)
    return _ReviewPolicyPlan(
        current=current,
        update=update,
        edge=edge,
        before_state=before_state,
        before_state_bytes=before_bytes,
        before_state_digest=before_digest,
        after_state=after_state,
        after_state_bytes=after_bytes,
        after_state_digest=after_digest,
        next_workflow_state=next_workflow_state,
        authority=authority,
        request_digest=request_digest,
        reviewer_assignment=reviewer_assignment,
    )


def _validate_review_policy_commit_result(
    result: object,
    plan: _ReviewPolicyPlan,
    registration: _RegisteredReviewStore,
) -> ReviewPolicyCommitResult:
    try:
        if type(result) is not ReviewPolicyCommitResult:
            raise _error("store-result", "review Store result type is invalid")
        result.__post_init__()
        checkpoint = result.checkpoint
        task = result.task
        event = result.event
        _workflow._validate_checkpoint_observation(
            checkpoint,
            issuer=registration.checkpoint_issuer,
        )
        checkpoint_bytes = _workflow.encode_checkpoint(checkpoint)
        task.__post_init__()
        event.__post_init__()
        task_reference = _workflow.TaskPolicyReference(
            version=plan.after_state.version,
            team_id=str(plan.after_state.team_id),
            workspace=str(plan.after_state.workspace),
            task_id=str(plan.after_state.task_id),
            sequence=plan.after_state.sequence,
            state_digest=plan.after_state_digest,
        )
        current = plan.current
        checkpoint_projection = (
            checkpoint.root,
            checkpoint.run,
            checkpoint.workflow_sequence,
            checkpoint.task_sequence,
            checkpoint.execution_mode,
            checkpoint.workflow_state,
            checkpoint.task_policy,
            checkpoint.active_assignment,
            checkpoint.pending_delivery,
            checkpoint.replied_message_ids,
            checkpoint.read_observed,
            checkpoint.released,
            checkpoint.review_authority,
            checkpoint.verification_authority,
            checkpoint.last_operation,
        )
        expected_checkpoint_projection = (
            current.root,
            current.run,
            current.workflow_sequence + 1,
            plan.after_state.sequence,
            current.execution_mode,
            plan.next_workflow_state,
            task_reference,
            current.active_assignment,
            current.pending_delivery,
            current.replied_message_ids,
            current.read_observed,
            current.released,
            plan.authority,
            current.verification_authority,
            current.last_operation,
        )
        if (
            checkpoint_projection != expected_checkpoint_projection
            or checkpoint.updated_ns < current.updated_ns
            or task.root_key != current.root.root_key
            or task.run_id != current.run.run_id
            or task.state != plan.after_state
            or task.state_bytes != plan.after_state_bytes
            or task.state_digest != plan.after_state_digest
            or task.updated_ns != checkpoint.updated_ns
            or event.root_key != current.root.root_key
            or event.workflow_sequence != checkpoint.workflow_sequence
            or event.task_sequence_before != plan.before_state.sequence
            or event.task_sequence_after != plan.after_state.sequence
            or event.from_state != current.workflow_state.value
            or event.to_state != plan.next_workflow_state.value
            or event.clock_ns != checkpoint.updated_ns
            or event.request_digest != plan.request_digest
            or event.checkpoint_bytes != checkpoint_bytes
            or event.checkpoint_digest != checkpoint.checkpoint_digest
            or event.evidence_ref != plan.authority.digest
            or event.event_digest != _review_policy_event_digest(event)
            or result.reviewer_assignment != plan.reviewer_assignment
            or result.replayed is not False
        ):
            raise _error("store-result", "review Store result differs from request")
        return result
    except ReviewCheckpointError:
        raise
    except (AttributeError, TypeError, ValueError, _workflow.WorkflowStoreError):
        raise _error("store-result", "review Store result is invalid") from None


def _review_policy_event_digest(event: ReviewPolicyEventObservation) -> str:
    checkpoint_text = event.checkpoint_bytes.decode("utf-8")
    values: dict[str, object] = {
        "workflow_event_id": event.workflow_event_id,
        "workflow_event_schema_version": event.workflow_event_schema_version,
        "root_key": event.root_key,
        "operation_id": event.operation_id,
        "workflow_sequence": event.workflow_sequence,
        "task_sequence_before": event.task_sequence_before,
        "task_sequence_after": event.task_sequence_after,
        "from_state": event.from_state,
        "to_state": event.to_state,
        "kind": event.kind,
        "actor": event.actor,
        "clock_ns": event.clock_ns,
        "request_digest": event.request_digest,
        "receipt_id": event.receipt_id,
        "checkpoint_bytes": checkpoint_text,
        "checkpoint_digest": event.checkpoint_digest,
        "evidence_ref": event.evidence_ref,
    }
    digest: str = _workflow._domain_digest(
        _workflow.WORKFLOW_EVENT_DIGEST_DOMAIN,
        _workflow._canonical_json(values),
    )
    return digest


def _validate_review_checkpoint_observation_result(
    result: object,
    root_key: _workflow.WorkflowRootKey,
    registration: _RegisteredReviewStore,
) -> ReviewCheckpointObservation | None:
    if result is None:
        return None
    try:
        if type(result) is not ReviewCheckpointObservation:
            raise _error("store-result", "review read result type is invalid")
        result.__post_init__()
        checkpoint = result.checkpoint
        task = result.task
        _workflow._validate_checkpoint_observation(
            checkpoint,
            issuer=registration.checkpoint_issuer,
        )
        checkpoint_bytes = _workflow.encode_checkpoint(checkpoint)
        predecessor = _workflow.decode_checkpoint(result.predecessor_checkpoint_bytes)
        task.__post_init__()
        for event in result.events:
            event.__post_init__()
        phase = task.state.phase
        expected_events: tuple[
            tuple[
                ReviewPolicyEdge,
                _workflow.CheckpointState,
                _workflow.CheckpointState,
            ],
            ...,
        ]
        if phase is _task.TaskPhase.WORKER_DONE:
            expected_events = (
                (
                    ReviewPolicyEdge.WORKER_COMPLETION,
                    _workflow.CheckpointState.WORKER_DONE,
                    _workflow.CheckpointState.WORKER_DONE,
                ),
            )
        elif phase is _task.TaskPhase.REVIEW_PENDING:
            expected_events = (
                (
                    ReviewPolicyEdge.WORKER_COMPLETION,
                    _workflow.CheckpointState.WORKER_DONE,
                    _workflow.CheckpointState.WORKER_DONE,
                ),
                (
                    ReviewPolicyEdge.REVIEW_REQUEST,
                    _workflow.CheckpointState.WORKER_DONE,
                    _workflow.CheckpointState.REVIEW_PENDING,
                ),
            )
        elif phase is _task.TaskPhase.APPROVED:
            expected_events = (
                (
                    ReviewPolicyEdge.WORKER_COMPLETION,
                    _workflow.CheckpointState.WORKER_DONE,
                    _workflow.CheckpointState.WORKER_DONE,
                ),
                (
                    ReviewPolicyEdge.REVIEW_REQUEST,
                    _workflow.CheckpointState.WORKER_DONE,
                    _workflow.CheckpointState.REVIEW_PENDING,
                ),
                (
                    ReviewPolicyEdge.APPROVED_DECISION,
                    _workflow.CheckpointState.REVIEW_PENDING,
                    _workflow.CheckpointState.REVIEW_PENDING,
                ),
            )
        else:
            raise _error("store-result", "review read task phase is invalid")
        if len(result.events) != len(expected_events):
            raise _error("store-result", "review read event prefix is incomplete")
        reference = checkpoint.task_policy
        state_root = checkpoint.root.state_root
        if (
            checkpoint.root.root_key != root_key
            or state_root.path != registration.state_root_path
            or state_root.device != registration.state_root_device
            or state_root.inode != registration.state_root_inode
            or reference is None
            or task.root_key != root_key
            or task.run_id != checkpoint.run.run_id
            or task.updated_ns != checkpoint.updated_ns
            or reference.version != task.state.version
            or reference.team_id != str(task.state.team_id)
            or reference.workspace != str(task.state.workspace)
            or reference.task_id != str(task.state.task_id)
            or reference.sequence != task.state.sequence
            or reference.state_digest != task.state_digest
            or checkpoint.task_sequence != task.state.sequence
            or checkpoint.review_authority is None
            or checkpoint.verification_authority is not None
        ):
            raise _error("store-result", "review read result differs from request")
        first_workflow_sequence = checkpoint.workflow_sequence - len(result.events)
        first_task_sequence = task.state.sequence - len(result.events)
        predecessor_reference = predecessor.task_policy
        if (
            predecessor.root != checkpoint.root
            or predecessor.run != checkpoint.run
            or predecessor.workflow_sequence != first_workflow_sequence
            or predecessor.task_sequence != first_task_sequence
            or predecessor.workflow_state is not _workflow.CheckpointState.WORKER_DONE
            or predecessor_reference is None
            or predecessor_reference.sequence != first_task_sequence
            or predecessor.execution_mode is not checkpoint.execution_mode
            or predecessor.active_assignment != checkpoint.active_assignment
            or predecessor.pending_delivery != checkpoint.pending_delivery
            or predecessor.replied_message_ids != checkpoint.replied_message_ids
            or predecessor.read_observed is not checkpoint.read_observed
            or predecessor.released is not checkpoint.released
            or predecessor.review_authority is not None
            or predecessor.verification_authority is not None
            or predecessor.last_operation != checkpoint.last_operation
        ):
            raise _error("store-result", "review read predecessor differs")
        decoded_events: list[_workflow.WorkflowCheckpointV4] = []
        event_ids: list[int] = []
        previous_checkpoint = predecessor
        for index, (event, expected) in enumerate(
            zip(result.events, expected_events, strict=True)
        ):
            edge, from_state, to_state = expected
            decoded = _workflow.decode_checkpoint(event.checkpoint_bytes)
            authority = decoded.review_authority
            event_ids.append(event.workflow_event_id)
            if (
                event.root_key != root_key
                or event.workflow_sequence != first_workflow_sequence + index + 1
                or event.task_sequence_before != first_task_sequence + index
                or event.task_sequence_after != first_task_sequence + index + 1
                or event.from_state != from_state.value
                or event.to_state != to_state.value
                or event.clock_ns != decoded.updated_ns
                or event.checkpoint_digest != decoded.checkpoint_digest
                or event.event_digest != _review_policy_event_digest(event)
                or decoded.root != checkpoint.root
                or decoded.run != checkpoint.run
                or decoded.workflow_sequence != event.workflow_sequence
                or decoded.task_sequence != event.task_sequence_after
                or decoded.workflow_state is not to_state
                or decoded.execution_mode is not checkpoint.execution_mode
                or decoded.active_assignment != checkpoint.active_assignment
                or decoded.pending_delivery != checkpoint.pending_delivery
                or decoded.replied_message_ids != checkpoint.replied_message_ids
                or decoded.read_observed is not checkpoint.read_observed
                or decoded.released is not checkpoint.released
                or decoded.verification_authority is not None
                or decoded.last_operation != checkpoint.last_operation
                or authority is None
                or event.evidence_ref != authority.digest
            ):
                raise _error("store-result", "review read event differs")
            if event.request_digest != _review_policy_request_digest(
                edge,
                previous_checkpoint,
                authority,
            ):
                raise _error("store-result", "review read request digest differs")
            decoded_events.append(decoded)
            previous_checkpoint = decoded
        if (
            event_ids != sorted(event_ids)
            or len(set(event_ids)) != len(event_ids)
            or decoded_events[-1] != checkpoint
            or result.events[-1].checkpoint_bytes != checkpoint_bytes
        ):
            raise _error("store-result", "review read event order differs")
        return result
    except ReviewCheckpointError:
        raise
    except (AttributeError, TypeError, ValueError, _workflow.WorkflowStoreError):
        raise _error("store-result", "review read result is invalid") from None


class ReviewCheckpointProducer:
    """Deep package-private producer for one actual review-policy edge."""

    __slots__ = ("__weakref__", "_handoff", "_store")

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise TypeError("ReviewCheckpointProducer is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("ReviewCheckpointProducer cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("ReviewCheckpointProducer cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("ReviewCheckpointProducer cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("ReviewCheckpointProducer cannot be pickled")

    def __init__(
        self,
        handoff: _handoff.PolicyVerificationHandoff,
        store: ReviewWorkflowStorePort,
    ) -> None:
        with _REVIEW_PRODUCER_BINDINGS_LOCK:
            if _REVIEW_PRODUCER_BINDINGS.get(self) is not None:
                raise _error("producer", "review producer is already initialized")
        if type(handoff) is not _handoff.PolicyVerificationHandoff:
            raise _error("handoff", "review authority owner is invalid")
        try:
            handoff_store = object.__getattribute__(handoff, "_store")
        except (AttributeError, TypeError):
            raise _error("handoff", "review authority owner is invalid") from None
        commit_method: object | None = None
        read_method: object | None = None
        introspection_failed = False
        try:
            commit_method = getattr(store, "commit_review_policy", None)
            read_method = getattr(store, "load_review_checkpoint", None)
        except _STORE_PORT_EXCEPTION:
            introspection_failed = True
        if introspection_failed:
            raise _error("store", "review workflow Store port is invalid")
        if not callable(commit_method) or not callable(read_method):
            raise _error("store", "review workflow Store port is incomplete")
        with _REVIEW_PRODUCER_BINDINGS_LOCK:
            if _REVIEW_PRODUCER_BINDINGS.get(self) is not None:
                raise _error("producer", "review producer is already initialized")
            object.__setattr__(self, "_handoff", handoff)
            object.__setattr__(self, "_store", store)
            _REVIEW_PRODUCER_BINDINGS[self] = (handoff, store, handoff_store)

    def commit(
        self,
        current: _workflow.WorkflowCheckpointV4,
        update: _review.ReviewPolicyUpdate,
        policy: _review.SerialReviewPolicy,
        review_ref: _review.ReviewAuthorityRef,
    ) -> ReviewPolicyCommitResult:
        if (
            type(current) is not _workflow.WorkflowCheckpointV4
            or type(update) is not _review.ReviewPolicyUpdate
            or type(policy) is not _review.SerialReviewPolicy
            or type(review_ref) is not _review.ReviewAuthorityRef
        ):
            raise _error("input", "review producer input type is invalid")
        handoff, store = _validate_review_checkpoint_producer(self)
        binding = handoff._bind_review_authority(update, policy, review_ref)
        request = _issue_review_policy_commit_request(self, current, binding)
        plan = _plan_review_policy_request(request, store)
        registration = _validate_registered_review_store_surface(store)
        if registration is None:
            raise _error("store", "review Store is not authoritative")
        result: object | None = None
        store_failed = False
        try:
            commit_function = cast(
                Callable[[object, ReviewPolicyCommitRequest], object],
                registration.commit_function,
            )
            result = commit_function(store, request)
        except Exception as error:
            if _registered_store_error_has_cleanup(error, registration):
                raise
            store_failed = True
        if store_failed:
            raise _error("store-failed", "review Store commit failed")
        return _validate_review_policy_commit_result(result, plan, registration)

    def read(
        self,
        root_key: _workflow.WorkflowRootKey,
    ) -> ReviewCheckpointObservation | None:
        if type(root_key) is not str:
            raise _error("root-key", "review root key type is invalid")
        _, store = _validate_review_checkpoint_producer(self)
        registration = _validate_registered_review_store_surface(store)
        if registration is None:
            raise _error("store", "review Store is not authoritative")
        result: object | None = None
        store_failed = False
        try:
            read_function = cast(
                Callable[[object, _workflow.WorkflowRootKey], object],
                registration.read_function,
            )
            result = read_function(store, root_key)
        except Exception as error:
            if _registered_store_error_has_cleanup(error, registration):
                raise
            store_failed = True
        if store_failed:
            raise _error("store-failed", "review Store read failed")
        return _validate_review_checkpoint_observation_result(
            result,
            root_key,
            registration,
        )
