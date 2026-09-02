"""Pure serial Worker-to-Reviewer review policy.

The module owns the policy seam for a normal-lane or admitted express-lane
write task.  It accepts
validated topology and task-policy values, validates typed lifecycle events,
and returns immutable state-update/effect intents.  It does not inspect a
terminal, process, prompt, workspace, backend, or persistence format.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from threading import RLock
from typing import Final, NewType, NoReturn, Protocol, SupportsIndex, TypeAlias
from weakref import WeakKeyDictionary

from .task_policy import (
    STATE_POLICY_VERSION,
    AttemptId,
    DispatchId,
    ExpectedSequenceUpdate,
    GitObjectId,
    TaskId,
    TaskLane,
    TaskPhase,
    TaskPolicyStateV4,
    TaskSpec,
    TreeDigest,
    WorkspaceIdentity,
)
from .topology import (
    AgentNode,
    EdgeKind,
    NodeId,
    TeamDefinition,
    TeamId,
)

RunId = NewType("RunId", str)
TerminalId = NewType("TerminalId", str)
CompletionId = NewType("CompletionId", str)
DecisionRef = NewType("DecisionRef", str)
PolicyFingerprint = NewType("PolicyFingerprint", str)

MAX_POLICY_IDENTIFIER_CHARS: Final = 256
MAX_POLICY_TEXT_CHARS: Final = 4096
MAX_REVIEW_ROUNDS: Final = 2**63 - 1
_GIT_OBJECT_ID: Final = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_TREE_DIGEST: Final = re.compile(r"[0-9a-f]{64}\Z")
_POLICY_FINGERPRINT: Final = re.compile(r"[0-9a-f]{64}\Z")
_REVIEW_AUTHORITY_REFERENCE: Final = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._:-]{{0,{MAX_POLICY_IDENTIFIER_CHARS - 1}}}\Z"
)
_REVIEW_LANES: Final = frozenset((TaskLane.NORMAL, TaskLane.EXPRESS))
_REVIEW_AUTHORITY_ISSUER: Final[object] = object()


class ReviewPolicyError(ValueError):
    """Raised when a typed policy input or event is not admissible."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ReviewDecisionKind(str, Enum):
    """Closed set of decisions a typed Reviewer may submit."""

    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    ASK_USER = "ask_user"
    FAILED = "failed"


class WorkerCompletionKind(str, Enum):
    """Typed Worker outcome; terminal state is never inferred from liveness."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    QUESTION = "question"
    ESCALATION = "escalation"


def _error(code: str, message: str) -> ReviewPolicyError:
    return ReviewPolicyError(code, message)


def _text(
    value: object,
    context: str,
    *,
    maximum: int = MAX_POLICY_IDENTIFIER_CHARS,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise _error("invalid-type", f"{context} must be a string")
    if not allow_empty and (not value or not value.strip()):
        raise _error("empty-value", f"{context} must not be empty")
    if len(value) > maximum:
        raise _error("value-too-long", f"{context} exceeds its character limit")
    if value != value.strip() or any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        or ord(character) in {0x2028, 0x2029}
        for character in value
    ):
        raise _error("unsafe-text", f"{context} contains unsafe text")
    return value


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class ReviewAuthorityRef:
    """Opaque, owner-issued reference for a review-policy authority.

    The issuer marker and object-identity binding are in-process misuse guards,
    not a cryptographic provenance claim. Durable authority remains the exact
    owner record.
    """

    reference: str
    digest: str
    _issuer: object = field(init=False, repr=False, compare=False)

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("ReviewAuthorityRef is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("ReviewAuthorityRef is return-only")

    def __repr__(self) -> str:
        return "<ReviewAuthorityRef opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("ReviewAuthorityRef cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("ReviewAuthorityRef cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("ReviewAuthorityRef cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("ReviewAuthorityRef cannot be pickled")


_REVIEW_AUTHORITY_BINDINGS: WeakKeyDictionary[ReviewAuthorityRef, tuple[str, str]] = (
    WeakKeyDictionary()
)
_REVIEW_AUTHORITY_BINDINGS_LOCK: Final = RLock()


def _validate_review_authority_ref(value: object) -> None:
    """Validate an exact, locally issued review authority reference."""

    if type(value) is not ReviewAuthorityRef:
        raise _error("invalid-authority-ref", "review authority ref type is not exact")
    try:
        reference = object.__getattribute__(value, "reference")
        digest = object.__getattribute__(value, "digest")
        issuer = object.__getattribute__(value, "_issuer")
    except AttributeError as exc:
        raise _error(
            "invalid-authority-ref",
            "review authority ref is malformed",
        ) from exc
    if type(reference) is not str or type(digest) is not str:
        raise _error(
            "invalid-authority-ref",
            "review authority ref scalars are invalid",
        )
    if issuer is not _REVIEW_AUTHORITY_ISSUER:
        raise _error(
            "invalid-authority-ref",
            "review authority ref was not issued by review policy",
        )
    with _REVIEW_AUTHORITY_BINDINGS_LOCK:
        binding = _REVIEW_AUTHORITY_BINDINGS.get(value)
    if binding != (reference, digest):
        raise _error(
            "invalid-authority-ref",
            "review authority ref binding is invalid",
        )
    reference_value = _text(reference, "review_authority.reference")
    if _REVIEW_AUTHORITY_REFERENCE.fullmatch(reference_value) is None:
        raise _error(
            "invalid-reference",
            "review_authority.reference must be a bounded safe identifier",
        )
    digest_value = _text(digest, "review_authority.digest")
    if _POLICY_FINGERPRINT.fullmatch(digest_value) is None:
        raise _error(
            "authority-digest",
            "review_authority.digest must be a lowercase SHA-256 digest",
        )


def _issue_review_authority_ref(reference: str, digest: str) -> ReviewAuthorityRef:
    """Issue one validated opaque reference from the review-policy owner."""

    if type(reference) is not str or type(digest) is not str:
        raise _error(
            "invalid-authority-ref",
            "review authority ref scalars are invalid",
        )
    reference_value = _text(reference, "review_authority.reference")
    if _REVIEW_AUTHORITY_REFERENCE.fullmatch(reference_value) is None:
        raise _error(
            "invalid-reference",
            "review_authority.reference must be a bounded safe identifier",
        )
    digest_value = _text(digest, "review_authority.digest")
    if _POLICY_FINGERPRINT.fullmatch(digest_value) is None:
        raise _error(
            "authority-digest",
            "review_authority.digest must be a lowercase SHA-256 digest",
        )
    result = object.__new__(ReviewAuthorityRef)
    object.__setattr__(result, "reference", reference_value)
    object.__setattr__(result, "digest", digest_value)
    object.__setattr__(result, "_issuer", _REVIEW_AUTHORITY_ISSUER)
    with _REVIEW_AUTHORITY_BINDINGS_LOCK:
        _REVIEW_AUTHORITY_BINDINGS[result] = (reference_value, digest_value)
    _validate_review_authority_ref(result)
    return result


def _sequence(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _error("invalid-sequence", f"{context} must be an integer")
    if not 0 <= value <= MAX_REVIEW_ROUNDS:
        raise _error("invalid-sequence", f"{context} is outside its supported range")
    return value


def _positive_round(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _error("max-review-rounds", f"{context} must be a positive integer")
    if value > MAX_REVIEW_ROUNDS:
        raise _error("max-review-rounds", f"{context} exceeds its supported range")
    return value


def _target(
    head: object,
    tree_digest: object,
    *,
    required: bool,
    context: str,
) -> tuple[GitObjectId | None, TreeDigest | None]:
    if (head is None) != (tree_digest is None):
        raise _error(
            "target-identity",
            f"{context}.target_head and target_tree_digest must be paired",
        )
    if head is None:
        if required:
            raise _error("target-identity", f"{context} requires target identity")
        return None, None
    head_value = _text(head, f"{context}.target_head")
    tree_value = _text(tree_digest, f"{context}.target_tree_digest")
    if _GIT_OBJECT_ID.fullmatch(head_value) is None:
        raise _error(
            "target-identity",
            f"{context}.target_head must be a lowercase Git object ID",
        )
    if _TREE_DIGEST.fullmatch(tree_value) is None:
        raise _error(
            "target-identity",
            f"{context}.target_tree_digest must be a lowercase SHA-256 digest",
        )
    return GitObjectId(head_value), TreeDigest(tree_value)


def _optional_text(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _text(value, context, maximum=MAX_POLICY_TEXT_CHARS)


def _fingerprint(value: object, context: str) -> PolicyFingerprint:
    candidate = _text(value, context)
    if _POLICY_FINGERPRINT.fullmatch(candidate) is None:
        raise _error("policy-fingerprint", f"{context} must be a SHA-256 digest")
    return PolicyFingerprint(candidate)


def _digest_parts(parts: tuple[str, ...]) -> PolicyFingerprint:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return PolicyFingerprint(digest.hexdigest())


@dataclass(frozen=True, slots=True)
class ReviewPair:
    """The one fixed Worker -> Reviewer pair for a normal task."""

    worker_node: NodeId
    reviewer_node: NodeId

    def __post_init__(self) -> None:
        _text(self.worker_node, "pair.worker_node")
        _text(self.reviewer_node, "pair.reviewer_node")
        if self.worker_node == self.reviewer_node:
            raise _error("self-review", "Worker and Reviewer must be different nodes")


def resolve_worker_reviewer_pair(
    definition: TeamDefinition, worker_node: NodeId
) -> ReviewPair:
    """Resolve exactly one reviewed-by edge from a validated topology.

    The caller is responsible for running :func:`topology.validate_team` with
    its verified profile resolver first.  This function repeats only the
    pair-specific checks needed at this seam, so an unknown, self, or
    ambiguous pair cannot become an assignment by accident.
    """

    if not isinstance(definition, TeamDefinition):
        raise _error("invalid-topology", "definition must be a TeamDefinition")
    worker_value = _text(worker_node, "worker_node")
    nodes_by_id: dict[str, AgentNode] = {}
    folded_ids: dict[str, list[str]] = {}
    for node in definition.nodes:
        node_value = _text(node.node_id, "topology.node_id")
        folded_ids.setdefault(node_value.casefold(), []).append(node_value)
        nodes_by_id[node_value] = node
    ambiguous_ids = tuple(
        sorted(values[0] for values in folded_ids.values() if len(values) > 1)
    )
    if ambiguous_ids:
        raise _error("ambiguous-pair", "topology contains ambiguous node identity")
    if worker_value not in nodes_by_id:
        raise _error("unknown-node", "Worker node is not in the topology")

    candidates: list[NodeId] = []
    for edge in definition.edges:
        if edge.kind is not EdgeKind.REVIEWED_BY:
            continue
        source = _text(edge.source, "topology.edge.source")
        target = _text(edge.target, "topology.edge.target")
        if source != worker_value:
            continue
        if target == worker_value:
            raise _error("self-review", "Worker cannot review itself")
        if target not in nodes_by_id:
            raise _error("unknown-node", "Reviewer node is not in the topology")
        candidates.append(NodeId(target))
    if len(candidates) != 1:
        raise _error("ambiguous-pair", "Worker must have exactly one Reviewer")

    worker = nodes_by_id[worker_value]
    reviewer = nodes_by_id[str(candidates[0])]
    if worker.profile.permission != "workspace-write":
        raise _error(
            "worker-permission", "normal Worker must have workspace-write permission"
        )
    if reviewer.profile.permission != "read-only":
        raise _error("reviewer-permission", "Reviewer must have read-only permission")
    return ReviewPair(NodeId(worker_value), candidates[0])


@dataclass(frozen=True, slots=True)
class AssignmentIdentity:
    """Identity that binds one active Worker assignment."""

    run_id: RunId
    task_id: TaskId
    dispatch_id: DispatchId
    attempt_id: AttemptId
    worker_node: NodeId
    reviewer_node: NodeId
    worker_terminal_id: TerminalId
    reviewer_terminal_id: TerminalId
    review_round: int

    def __post_init__(self) -> None:
        _text(self.run_id, "assignment.run_id")
        _text(self.task_id, "assignment.task_id")
        _text(self.dispatch_id, "assignment.dispatch_id")
        _text(self.attempt_id, "assignment.attempt_id")
        _text(self.worker_node, "assignment.worker_node")
        _text(self.reviewer_node, "assignment.reviewer_node")
        _text(self.worker_terminal_id, "assignment.worker_terminal_id")
        _text(self.reviewer_terminal_id, "assignment.reviewer_terminal_id")
        _positive_round(self.review_round, "assignment.review_round")
        if self.worker_terminal_id == self.reviewer_terminal_id:
            raise _error(
                "independent-terminal",
                "Worker and Reviewer must use different terminals",
            )


@dataclass(frozen=True, slots=True)
class WorkerAssignment:
    """Explicit Worker and Reviewer assignment for one review round."""

    run_id: RunId
    task_id: TaskId
    dispatch_id: DispatchId
    attempt_id: AttemptId
    worker_node: NodeId
    reviewer_node: NodeId
    worker_terminal_id: TerminalId
    reviewer_terminal_id: TerminalId
    review_round: int
    target_head: GitObjectId | None = None
    target_tree_digest: TreeDigest | None = None

    def __post_init__(self) -> None:
        identity = AssignmentIdentity(
            self.run_id,
            self.task_id,
            self.dispatch_id,
            self.attempt_id,
            self.worker_node,
            self.reviewer_node,
            self.worker_terminal_id,
            self.reviewer_terminal_id,
            self.review_round,
        )
        object.__setattr__(self, "run_id", identity.run_id)
        object.__setattr__(self, "task_id", identity.task_id)
        object.__setattr__(self, "dispatch_id", identity.dispatch_id)
        object.__setattr__(self, "attempt_id", identity.attempt_id)
        object.__setattr__(self, "worker_node", identity.worker_node)
        object.__setattr__(self, "reviewer_node", identity.reviewer_node)
        object.__setattr__(self, "worker_terminal_id", identity.worker_terminal_id)
        object.__setattr__(self, "reviewer_terminal_id", identity.reviewer_terminal_id)
        object.__setattr__(self, "review_round", identity.review_round)
        head, tree = _target(
            self.target_head,
            self.target_tree_digest,
            required=False,
            context="assignment",
        )
        object.__setattr__(self, "target_head", head)
        object.__setattr__(self, "target_tree_digest", tree)

    @property
    def identity(self) -> AssignmentIdentity:
        return AssignmentIdentity(
            self.run_id,
            self.task_id,
            self.dispatch_id,
            self.attempt_id,
            self.worker_node,
            self.reviewer_node,
            self.worker_terminal_id,
            self.reviewer_terminal_id,
            self.review_round,
        )


@dataclass(frozen=True, slots=True)
class CompletionIdentity:
    """Full typed identity of a Worker completion event."""

    run_id: RunId
    task_id: TaskId
    dispatch_id: DispatchId
    attempt_id: AttemptId
    worker_node: NodeId
    reviewer_node: NodeId
    worker_terminal_id: TerminalId
    review_round: int
    completion_id: CompletionId

    def __post_init__(self) -> None:
        _text(self.run_id, "completion.run_id")
        _text(self.task_id, "completion.task_id")
        _text(self.dispatch_id, "completion.dispatch_id")
        _text(self.attempt_id, "completion.attempt_id")
        _text(self.worker_node, "completion.worker_node")
        _text(self.reviewer_node, "completion.reviewer_node")
        _text(self.worker_terminal_id, "completion.worker_terminal_id")
        _positive_round(self.review_round, "completion.review_round")
        _text(self.completion_id, "completion.completion_id")


@dataclass(frozen=True, slots=True)
class WorkerCompletion:
    """Typed Worker completion; text is explanatory and never authoritative."""

    expected_sequence: int
    run_id: RunId
    task_id: TaskId
    dispatch_id: DispatchId
    attempt_id: AttemptId
    worker_node: NodeId
    reviewer_node: NodeId
    worker_terminal_id: TerminalId
    review_round: int
    completion_id: CompletionId
    target_head: GitObjectId | None
    target_tree_digest: TreeDigest | None
    kind: WorkerCompletionKind
    explanation: str | None = None

    def __post_init__(self) -> None:
        _sequence(self.expected_sequence, "completion.expected_sequence")
        identity = CompletionIdentity(
            self.run_id,
            self.task_id,
            self.dispatch_id,
            self.attempt_id,
            self.worker_node,
            self.reviewer_node,
            self.worker_terminal_id,
            self.review_round,
            self.completion_id,
        )
        for name in (
            "run_id",
            "task_id",
            "dispatch_id",
            "attempt_id",
            "worker_node",
            "reviewer_node",
            "worker_terminal_id",
            "review_round",
            "completion_id",
        ):
            object.__setattr__(self, name, getattr(identity, name))
        head, tree = _target(
            self.target_head,
            self.target_tree_digest,
            required=False,
            context="completion",
        )
        object.__setattr__(self, "target_head", head)
        object.__setattr__(self, "target_tree_digest", tree)
        if not isinstance(self.kind, WorkerCompletionKind):
            raise _error("completion-kind", "completion.kind is not supported")
        object.__setattr__(
            self,
            "explanation",
            _optional_text(self.explanation, "completion.explanation"),
        )

    @property
    def identity(self) -> CompletionIdentity:
        return CompletionIdentity(
            self.run_id,
            self.task_id,
            self.dispatch_id,
            self.attempt_id,
            self.worker_node,
            self.reviewer_node,
            self.worker_terminal_id,
            self.review_round,
            self.completion_id,
        )


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """Typed handoff from an accepted Worker completion to the Reviewer."""

    expected_sequence: int
    completion: WorkerCompletion

    def __post_init__(self) -> None:
        _sequence(self.expected_sequence, "review_request.expected_sequence")
        if not isinstance(self.completion, WorkerCompletion):
            raise _error("invalid-event", "review_request.completion must be typed")


@dataclass(frozen=True, slots=True)
class ReviewDecisionIdentity:
    """Full identity of a Reviewer decision."""

    run_id: RunId
    task_id: TaskId
    dispatch_id: DispatchId
    attempt_id: AttemptId
    worker_node: NodeId
    reviewer_node: NodeId
    worker_terminal_id: TerminalId
    reviewer_terminal_id: TerminalId
    review_round: int
    completion_id: CompletionId
    completion_expected_sequence: int
    target_head: GitObjectId
    target_tree_digest: TreeDigest
    decision_ref: DecisionRef

    def __post_init__(self) -> None:
        _text(self.run_id, "decision.run_id")
        _text(self.task_id, "decision.task_id")
        _text(self.dispatch_id, "decision.dispatch_id")
        _text(self.attempt_id, "decision.attempt_id")
        _text(self.worker_node, "decision.worker_node")
        _text(self.reviewer_node, "decision.reviewer_node")
        _text(self.worker_terminal_id, "decision.worker_terminal_id")
        _text(self.reviewer_terminal_id, "decision.reviewer_terminal_id")
        _positive_round(self.review_round, "decision.review_round")
        _text(self.completion_id, "decision.completion_id")
        _sequence(
            self.completion_expected_sequence,
            "decision.completion_expected_sequence",
        )
        _text(self.target_head, "decision.target_head")
        _text(self.target_tree_digest, "decision.target_tree_digest")
        _text(self.decision_ref, "decision.decision_ref")


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """Typed Reviewer decision; explanation text cannot approve a task."""

    expected_sequence: int
    run_id: RunId
    task_id: TaskId
    dispatch_id: DispatchId
    attempt_id: AttemptId
    worker_node: NodeId
    reviewer_node: NodeId
    worker_terminal_id: TerminalId
    reviewer_terminal_id: TerminalId
    review_round: int
    completion_id: CompletionId
    completion_expected_sequence: int
    target_head: GitObjectId
    target_tree_digest: TreeDigest
    decision_ref: DecisionRef
    kind: ReviewDecisionKind
    explanation: str | None = None

    def __post_init__(self) -> None:
        _sequence(self.expected_sequence, "decision.expected_sequence")
        identity = ReviewDecisionIdentity(
            self.run_id,
            self.task_id,
            self.dispatch_id,
            self.attempt_id,
            self.worker_node,
            self.reviewer_node,
            self.worker_terminal_id,
            self.reviewer_terminal_id,
            self.review_round,
            self.completion_id,
            self.completion_expected_sequence,
            self.target_head,
            self.target_tree_digest,
            self.decision_ref,
        )
        for name in (
            "run_id",
            "task_id",
            "dispatch_id",
            "attempt_id",
            "worker_node",
            "reviewer_node",
            "worker_terminal_id",
            "reviewer_terminal_id",
            "review_round",
            "completion_id",
            "completion_expected_sequence",
            "target_head",
            "target_tree_digest",
            "decision_ref",
        ):
            object.__setattr__(self, name, getattr(identity, name))
        _target(
            self.target_head,
            self.target_tree_digest,
            required=True,
            context="decision",
        )
        if self.completion_expected_sequence >= self.expected_sequence:
            raise _error(
                "invalid-sequence",
                "decision completion sequence must precede decision sequence",
            )
        if self.completion_expected_sequence < 1:
            raise _error(
                "invalid-sequence",
                "decision completion sequence must follow assignment",
            )
        if not isinstance(self.kind, ReviewDecisionKind):
            raise _error("decision-kind", "decision.kind is not supported")
        object.__setattr__(
            self,
            "explanation",
            _optional_text(self.explanation, "decision.explanation"),
        )

    @property
    def identity(self) -> ReviewDecisionIdentity:
        return ReviewDecisionIdentity(
            self.run_id,
            self.task_id,
            self.dispatch_id,
            self.attempt_id,
            self.worker_node,
            self.reviewer_node,
            self.worker_terminal_id,
            self.reviewer_terminal_id,
            self.review_round,
            self.completion_id,
            self.completion_expected_sequence,
            self.target_head,
            self.target_tree_digest,
            self.decision_ref,
        )


@dataclass(frozen=True, slots=True)
class AssignmentCommand:
    """Explicit initial or retry assignment command."""

    expected_sequence: int
    assignment: WorkerAssignment

    def __post_init__(self) -> None:
        _sequence(self.expected_sequence, "assignment_command.expected_sequence")
        if not isinstance(self.assignment, WorkerAssignment):
            raise _error("invalid-event", "assignment_command.assignment must be typed")


@dataclass(frozen=True, slots=True)
class ReviewerAssignment:
    """Typed effect intent for a Reviewer dispatch; no process or argv."""

    assignment: WorkerAssignment
    completion: WorkerCompletion
    policy_fingerprint: PolicyFingerprint

    def __post_init__(self) -> None:
        if not isinstance(self.assignment, WorkerAssignment):
            raise _error(
                "invalid-effect", "Reviewer assignment must contain an assignment"
            )
        if not isinstance(self.completion, WorkerCompletion):
            raise _error(
                "invalid-effect", "Reviewer assignment must contain a completion"
            )
        fingerprint = _fingerprint(
            self.policy_fingerprint, "reviewer_assignment.policy_fingerprint"
        )
        object.__setattr__(self, "policy_fingerprint", fingerprint)
        if self.completion.kind is not WorkerCompletionKind.SUCCEEDED:
            raise _error(
                "completion-kind",
                "Reviewer assignment requires a successful completion",
            )
        if not _identity_matches_assignment(self.assignment, self.completion):
            raise _error(
                "identity-mismatch", "Reviewer assignment identities do not match"
            )
        if (
            self.assignment.target_head is None
            or self.assignment.target_tree_digest is None
        ):
            raise _error(
                "target-identity",
                "Reviewer assignment requires assignment target identity",
            )
        if (
            self.completion.target_head is None
            or self.completion.target_tree_digest is None
        ):
            raise _error(
                "target-identity", "Reviewer assignment requires target identity"
            )
        if not _target_matches(
            self.assignment.target_head,
            self.assignment.target_tree_digest,
            self.completion.target_head,
            self.completion.target_tree_digest,
        ):
            raise _error("target-identity", "Reviewer assignment targets do not match")

    @property
    def run_id(self) -> RunId:
        return self.assignment.run_id

    @property
    def task_id(self) -> TaskId:
        return self.assignment.task_id

    @property
    def dispatch_id(self) -> DispatchId:
        return self.assignment.dispatch_id

    @property
    def attempt_id(self) -> AttemptId:
        return self.assignment.attempt_id

    @property
    def worker_node(self) -> NodeId:
        return self.assignment.worker_node

    @property
    def reviewer_node(self) -> NodeId:
        return self.assignment.reviewer_node

    @property
    def review_round(self) -> int:
        return self.assignment.review_round

    @property
    def target_head(self) -> GitObjectId:
        assert self.completion.target_head is not None
        return self.completion.target_head

    @property
    def target_tree_digest(self) -> TreeDigest:
        assert self.completion.target_tree_digest is not None
        return self.completion.target_tree_digest


ReviewPolicyEvent: TypeAlias = (
    AssignmentCommand | WorkerCompletion | ReviewRequest | ReviewDecision
)


@dataclass(frozen=True, slots=True)
class DependencyState:
    """Explicit observation used to decide whether one dependency is ready."""

    task_id: TaskId
    phase: TaskPhase

    def __post_init__(self) -> None:
        _text(self.task_id, "dependency.task_id")
        if not isinstance(self.phase, TaskPhase):
            raise _error("dependency-phase", "dependency.phase must be a TaskPhase")


def _policy_fingerprint(
    task: TaskSpec,
    team_id: object,
    pair: ReviewPair,
    max_review_rounds: int,
    dependency_states: tuple[DependencyState, ...],
) -> PolicyFingerprint:
    dependencies = sorted(
        ((str(item.task_id), item.phase.value) for item in dependency_states),
        key=lambda item: (item[0].casefold(), item[0], item[1]),
    )
    task_dependencies = tuple(
        sorted(
            (str(item) for item in task.dependencies),
            key=lambda item: (item.casefold(), item),
        )
    )
    resources = tuple(
        sorted(
            (claim.name for claim in task.resource_claims),
            key=lambda item: (item.casefold(), item),
        )
    )
    parts: list[str] = ["policy-fingerprint-v1"]

    def add_scalar(label: str, value: str) -> None:
        parts.extend((label, value))

    def add_sequence(label: str, values: tuple[str, ...]) -> None:
        parts.extend((label, str(len(values))))
        parts.extend(values)

    add_scalar("team_id", str(team_id))
    add_scalar("task_id", str(task.task_id))
    add_scalar("title", task.title)
    add_scalar("context", task.context)
    add_scalar("goal", task.goal)
    add_sequence("acceptance", task.acceptance)
    add_sequence("allowed_paths", task.allowed_paths)
    add_sequence("do_not_modify", task.do_not_modify)
    add_sequence("dependencies", task_dependencies)
    add_scalar("verification", str(task.verification))
    add_scalar(
        "escalation_node",
        "" if task.escalation_node is None else str(task.escalation_node),
    )
    add_scalar("kind", task.kind.value)
    add_scalar("lane", task.lane.value)
    add_sequence("resource_claims", resources)
    add_scalar("worker_node", str(pair.worker_node))
    add_scalar("reviewer_node", str(pair.reviewer_node))
    add_scalar("max_review_rounds", str(max_review_rounds))
    add_sequence(
        "dependency_states",
        tuple(f"{task_id}:{phase}" for task_id, phase in dependencies),
    )
    return _digest_parts(tuple(parts))


@dataclass(frozen=True, slots=True)
class SerialReviewPolicy:
    """Static, validated inputs for one normal or admitted express task."""

    task: TaskSpec
    team_definition: TeamDefinition
    worker_node: NodeId
    max_review_rounds: int
    dependency_states: tuple[DependencyState, ...] = ()
    active_assignments: tuple[WorkerAssignment, ...] = ()
    pair: ReviewPair = field(init=False)
    fingerprint: PolicyFingerprint = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.task, TaskSpec):
            raise _error("invalid-task", "policy.task must be a TaskSpec")
        if not isinstance(self.team_definition, TeamDefinition):
            raise _error(
                "invalid-topology", "policy.team_definition must be a TeamDefinition"
            )
        if self.task.lane not in _REVIEW_LANES:
            raise _error(
                "review-lane",
                "serial review policy accepts only normal or express lanes",
            )
        _positive_round(self.max_review_rounds, "max_review_rounds")
        pair = resolve_worker_reviewer_pair(self.team_definition, self.worker_node)
        object.__setattr__(self, "pair", pair)
        dependency_ids = [str(item).casefold() for item in self.task.dependencies]
        if len(dependency_ids) != len(set(dependency_ids)):
            raise _error("duplicate-dependency", "task dependencies must be unique")
        if not isinstance(self.dependency_states, tuple):
            raise _error("invalid-type", "dependency_states must be an immutable tuple")
        for dependency in self.dependency_states:
            if not isinstance(dependency, DependencyState):
                raise _error(
                    "invalid-type",
                    "dependency_states must contain DependencyState values",
                )
        if not isinstance(self.active_assignments, tuple):
            raise _error(
                "invalid-type", "active_assignments must be an immutable tuple"
            )
        if len(self.active_assignments) > 1:
            raise _error(
                "active-assignment",
                "normal and express lanes allow at most one active write assignment",
            )
        if any(
            not isinstance(item, WorkerAssignment) for item in self.active_assignments
        ):
            raise _error(
                "invalid-type",
                "active_assignments must contain WorkerAssignment values",
            )
        object.__setattr__(
            self,
            "fingerprint",
            _policy_fingerprint(
                self.task,
                self.team_definition.team_id,
                pair,
                self.max_review_rounds,
                self.dependency_states,
            ),
        )


def _validate_policy_instance(policy: SerialReviewPolicy) -> None:
    if not isinstance(policy.task, TaskSpec):
        raise _error("invalid-policy", "policy.task must be a TaskSpec")
    if policy.task.lane not in _REVIEW_LANES:
        raise _error(
            "review-lane",
            "serial review policy accepts only normal or express lanes",
        )
    _positive_round(policy.max_review_rounds, "max_review_rounds")
    resolved_pair = resolve_worker_reviewer_pair(
        policy.team_definition, policy.worker_node
    )
    if resolved_pair != policy.pair:
        raise _error("policy-fingerprint", "policy pair does not match topology")
    expected = _policy_fingerprint(
        policy.task,
        policy.team_definition.team_id,
        resolved_pair,
        policy.max_review_rounds,
        policy.dependency_states,
    )
    if policy.fingerprint != expected:
        raise _error(
            "policy-fingerprint", "policy fingerprint does not match policy inputs"
        )
    if len(policy.active_assignments) > 1:
        raise _error(
            "active-assignment",
            "normal and express lanes allow at most one active write assignment",
        )


def validate_reviewer_assignment(
    value: ReviewerAssignment,
    policy: SerialReviewPolicy,
    expected_state: ReviewPolicyState,
) -> ReviewerAssignment:
    """Validate an effect intent against the exact policy that produced it."""

    if not isinstance(value, ReviewerAssignment):
        raise _error("invalid-effect", "value must be a ReviewerAssignment")
    if not isinstance(policy, SerialReviewPolicy):
        raise _error("invalid-policy", "policy must be a SerialReviewPolicy")
    if not isinstance(expected_state, ReviewPolicyState):
        raise _error("invalid-state", "expected_state must be a ReviewPolicyState")
    _validate_policy_instance(policy)
    if value.policy_fingerprint != policy.fingerprint:
        raise _error(
            "policy-fingerprint", "Reviewer effect policy differs from current policy"
        )
    _check_dependencies(policy)
    _validate_policy_state(expected_state)
    _validate_current_identity(expected_state, policy)
    if expected_state.task_state.phase is not TaskPhase.WORKER_DONE:
        raise _error("phase", "Reviewer effect requires the current worker_done state")
    if expected_state.assignment is None or expected_state.completion is None:
        raise _error(
            "identity-mismatch", "Reviewer effect has no current completion authority"
        )
    if value.assignment.identity != expected_state.assignment.identity:
        raise _error(
            "identity-mismatch", "Reviewer effect differs from current assignment"
        )
    if not _completion_authority_matches(value.completion, expected_state.completion):
        raise _error(
            "identity-mismatch", "Reviewer effect differs from current completion"
        )
    if value.review_round > policy.max_review_rounds:
        raise _error("review-limit", "Reviewer effect exceeds max_review_rounds")
    if expected_state.task_state.review_round > policy.max_review_rounds:
        raise _error("review-limit", "current state exceeds max_review_rounds")
    if value.task_id != policy.task.task_id:
        raise _error("identity-mismatch", "Reviewer effect Task differs from policy")
    if (
        value.worker_node != policy.pair.worker_node
        or value.reviewer_node != policy.pair.reviewer_node
    ):
        raise _error("identity-mismatch", "Reviewer effect pair differs from policy")
    return value


@dataclass(frozen=True, slots=True)
class ReviewPolicyState:
    """Immutable policy state that wraps the canonical v4 task observation."""

    run_id: RunId
    task_state: TaskPolicyStateV4
    assignment: WorkerAssignment | None = None
    completion: WorkerCompletion | None = None
    decision: ReviewDecision | None = None
    last_event: ReviewPolicyEvent | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _text(self.run_id, "policy_state.run_id")
        if not isinstance(self.task_state, TaskPolicyStateV4):
            raise _error(
                "invalid-state", "policy_state.task_state must be TaskPolicyStateV4"
            )
        for value, context, expected in (
            (self.assignment, "policy_state.assignment", WorkerAssignment),
            (self.completion, "policy_state.completion", WorkerCompletion),
            (self.decision, "policy_state.decision", ReviewDecision),
        ):
            if value is not None and not isinstance(value, expected):
                raise _error("invalid-state", f"{context} has an invalid type")
        if self.last_event is not None and not isinstance(
            self.last_event,
            (AssignmentCommand, WorkerCompletion, ReviewRequest, ReviewDecision),
        ):
            raise _error("invalid-state", "policy_state.last_event has an invalid type")
        if self.reason_code is not None:
            _text(self.reason_code, "policy_state.reason_code")
        _validate_policy_state(self)


def _validate_policy_state(value: ReviewPolicyState) -> None:
    """Validate causal observations before a reducer can consume them."""

    state = value.task_state
    phase = state.phase
    if phase in (
        TaskPhase.VERIFYING,
        TaskPhase.COMPLETED,
        TaskPhase.VERIFICATION_FAILED,
    ):
        raise _error(
            "phase",
            "serial review policy does not accept verification or completion state",
        )

    if phase is TaskPhase.PENDING:
        if (
            value.assignment is not None
            or value.completion is not None
            or value.decision is not None
        ):
            raise _error(
                "phase",
                "pending state cannot contain assignment, completion, or decision",
            )
        if (
            state.attempt_id is not None
            or state.dispatch_id is not None
            or state.worker_node is not None
            or state.reviewer_node is not None
            or state.review_round != 0
            or state.target_head is not None
            or state.target_tree_digest is not None
        ):
            raise _error(
                "identity-mismatch", "pending state must have no active review identity"
            )
        if value.reason_code is not None:
            raise _error("phase", "pending state cannot contain a reason")
        if value.last_event is not None:
            raise _error("phase", "pending state cannot have a last event")
        return

    assignment = value.assignment
    if assignment is None:
        raise _error("phase", f"{phase.value} state requires an assignment")
    if state.sequence < 1:
        raise _error(
            "invalid-sequence", "active policy state sequence must follow assignment"
        )
    if value.run_id != assignment.run_id or state.task_id != assignment.task_id:
        raise _error("identity-mismatch", "policy state and assignment identity differ")
    if (
        state.attempt_id != assignment.attempt_id
        or state.dispatch_id != assignment.dispatch_id
        or state.worker_node != assignment.worker_node
        or state.reviewer_node != assignment.reviewer_node
        or state.review_round != assignment.review_round
    ):
        raise _error("identity-mismatch", "v4 state assignment identity differs")
    if assignment.target_head is not None and not _target_matches(
        state.target_head,
        state.target_tree_digest,
        assignment.target_head,
        assignment.target_tree_digest,
    ):
        raise _error("target-identity", "assignment and v4 target identity differ")

    completion = value.completion
    decision = value.decision
    if phase is TaskPhase.ASSIGNED:
        if completion is not None or decision is not None:
            raise _error(
                "phase", "assigned state cannot contain completion or decision"
            )
        if not _target_matches(
            state.target_head,
            state.target_tree_digest,
            assignment.target_head,
            assignment.target_tree_digest,
        ):
            raise _error(
                "target-identity", "assigned state target differs from assignment"
            )
        if value.reason_code is not None:
            raise _error("phase", "assigned state cannot contain a reason")
        if not isinstance(value.last_event, AssignmentCommand):
            raise _error("phase", "assigned state requires an AssignmentCommand origin")
        if value.last_event.expected_sequence != state.sequence - 1:
            raise _error(
                "invalid-sequence", "assignment origin must immediately precede state"
            )
        if value.last_event.assignment != assignment:
            raise _error(
                "identity-mismatch", "assignment origin differs from state assignment"
            )
        return

    if completion is None:
        raise _error("phase", f"{phase.value} state requires a Worker completion")
    if not _identity_matches_assignment(assignment, completion):
        raise _error("identity-mismatch", "completion identity differs from assignment")
    if completion.expected_sequence < 1:
        raise _error("invalid-sequence", "completion sequence must follow assignment")
    if not _target_matches(
        state.target_head,
        state.target_tree_digest,
        completion.target_head,
        completion.target_tree_digest,
    ):
        raise _error("target-identity", "completion target differs from v4 state")
    if completion.kind is WorkerCompletionKind.SUCCEEDED and (
        completion.target_head is None or completion.target_tree_digest is None
    ):
        raise _error(
            "target-identity", "successful completion requires target identity"
        )
    if assignment.target_head is not None and not _target_matches(
        completion.target_head,
        completion.target_tree_digest,
        assignment.target_head,
        assignment.target_tree_digest,
    ):
        raise _error("target-identity", "completion target differs from assignment")

    if phase in (TaskPhase.WORKER_DONE, TaskPhase.REVIEW_PENDING):
        if (
            completion.kind is not WorkerCompletionKind.SUCCEEDED
            or decision is not None
        ):
            raise _error(
                "phase", f"{phase.value} requires a successful completion only"
            )
        if value.reason_code is not None:
            raise _error("phase", f"{phase.value} cannot contain a reason")
        if phase is TaskPhase.WORKER_DONE:
            if not isinstance(value.last_event, WorkerCompletion):
                raise _error(
                    "phase", "worker_done state requires a WorkerCompletion origin"
                )
            if value.last_event.expected_sequence != state.sequence - 1:
                raise _error(
                    "invalid-sequence",
                    "completion origin must immediately precede worker_done",
                )
            if not _completion_authority_matches(completion, value.last_event):
                raise _error(
                    "identity-mismatch",
                    "completion origin differs from state completion",
                )
            if completion.expected_sequence != state.sequence - 1:
                raise _error(
                    "invalid-sequence",
                    "completion sequence does not immediately precede worker_done",
                )
            if completion.expected_sequence != value.last_event.expected_sequence:
                raise _error(
                    "invalid-sequence", "completion sequence differs from its origin"
                )
        else:
            if not isinstance(value.last_event, ReviewRequest):
                raise _error(
                    "phase", "review_pending state requires a ReviewRequest origin"
                )
            if value.last_event.expected_sequence != state.sequence - 1:
                raise _error(
                    "invalid-sequence",
                    "review request origin must immediately precede review_pending",
                )
            if not _completion_authority_matches(
                completion, value.last_event.completion
            ):
                raise _error(
                    "identity-mismatch",
                    "review request origin differs from state completion",
                )
            if completion.expected_sequence != state.sequence - 2:
                raise _error(
                    "invalid-sequence",
                    "completion sequence does not immediately precede review request",
                )
        return

    if decision is not None and completion.kind is not WorkerCompletionKind.SUCCEEDED:
        raise _error(
            "phase", f"{phase.value} Reviewer decision requires a successful completion"
        )

    if phase is TaskPhase.APPROVED:
        if decision is None or decision.kind is not ReviewDecisionKind.APPROVED:
            raise _error(
                "phase", "approved state requires an APPROVED Reviewer decision"
            )
        if value.reason_code is not None:
            raise _error("phase", "approved state cannot contain a reason")
    elif phase is TaskPhase.CHANGES_REQUESTED:
        if (
            decision is None
            or decision.kind is not ReviewDecisionKind.CHANGES_REQUESTED
        ):
            raise _error(
                "phase",
                "changes_requested state requires a CHANGES_REQUESTED Reviewer decision",
            )
        if value.reason_code is not None:
            raise _error("phase", "changes_requested state cannot contain a reason")
    elif phase is TaskPhase.ASK_USER:
        valid_worker_origin = (
            decision is None
            and completion.kind
            in (WorkerCompletionKind.QUESTION, WorkerCompletionKind.ESCALATION)
            and value.reason_code is None
        )
        valid_reviewer_origin = (
            decision is not None
            and decision.kind is ReviewDecisionKind.ASK_USER
            and value.reason_code is None
        )
        valid_limit_origin = (
            decision is not None
            and decision.kind is ReviewDecisionKind.CHANGES_REQUESTED
            and value.reason_code == "review-limit"
        )
        if not (valid_worker_origin or valid_reviewer_origin or valid_limit_origin):
            raise _error("phase", "ask_user state has no permitted typed origin")
        if valid_worker_origin:
            if not isinstance(value.last_event, WorkerCompletion):
                raise _error(
                    "phase", "ask_user state requires a WorkerCompletion origin"
                )
            if value.last_event.expected_sequence != state.sequence - 1:
                raise _error(
                    "invalid-sequence",
                    "Worker origin must immediately precede ask_user",
                )
            if not _completion_authority_matches(completion, value.last_event):
                raise _error(
                    "identity-mismatch",
                    "Worker origin differs from ask_user completion",
                )
            if completion.expected_sequence != value.last_event.expected_sequence:
                raise _error(
                    "invalid-sequence",
                    "completion sequence differs from ask_user origin",
                )
            if completion.expected_sequence != state.sequence - 1:
                raise _error(
                    "invalid-sequence",
                    "completion sequence does not immediately precede ask_user",
                )
            return
    elif phase is TaskPhase.FAILED:
        valid_worker_origin = (
            decision is None
            and completion.kind
            in (WorkerCompletionKind.FAILED, WorkerCompletionKind.TIMEOUT)
            and value.reason_code is None
        )
        valid_reviewer_origin = (
            decision is not None
            and decision.kind is ReviewDecisionKind.FAILED
            and value.reason_code is None
        )
        if not (valid_worker_origin or valid_reviewer_origin):
            raise _error("phase", "failed state has no permitted typed origin")
        if valid_worker_origin:
            if not isinstance(value.last_event, WorkerCompletion):
                raise _error("phase", "failed state requires a WorkerCompletion origin")
            if value.last_event.expected_sequence != state.sequence - 1:
                raise _error(
                    "invalid-sequence", "Worker origin must immediately precede failed"
                )
            if not _completion_authority_matches(completion, value.last_event):
                raise _error(
                    "identity-mismatch", "Worker origin differs from failed completion"
                )
            if completion.expected_sequence != value.last_event.expected_sequence:
                raise _error(
                    "invalid-sequence", "completion sequence differs from failed origin"
                )
            if completion.expected_sequence != state.sequence - 1:
                raise _error(
                    "invalid-sequence",
                    "completion sequence does not immediately precede failed",
                )
            return
    else:
        raise _error(
            "phase", f"serial review policy cannot consume {phase.value} state"
        )

    if decision is None:
        raise _error("phase", f"{phase.value} state requires a Reviewer decision")
    if not _identity_matches_assignment(assignment, decision):
        raise _error("identity-mismatch", "decision identity differs from assignment")
    if decision.completion_id != completion.completion_id:
        raise _error("identity-mismatch", "decision completion identity differs")
    if decision.completion_expected_sequence != completion.expected_sequence:
        raise _error(
            "invalid-sequence", "decision completion sequence differs from completion"
        )
    if completion.expected_sequence != state.sequence - 3:
        raise _error(
            "invalid-sequence",
            "completion sequence does not immediately precede Reviewer decision",
        )
    if decision.expected_sequence >= state.sequence:
        raise _error(
            "invalid-sequence", "decision sequence must precede state sequence"
        )
    if decision.expected_sequence < 1:
        raise _error("invalid-sequence", "decision sequence must follow assignment")
    if not _target_matches(
        decision.target_head,
        decision.target_tree_digest,
        completion.target_head,
        completion.target_tree_digest,
    ):
        raise _error("target-identity", "decision target differs from completion")
    if not isinstance(value.last_event, ReviewDecision):
        raise _error("phase", f"{phase.value} state requires a ReviewDecision origin")
    if value.last_event.expected_sequence != state.sequence - 1:
        raise _error(
            "invalid-sequence", "decision origin must immediately precede state"
        )
    if decision is None or not _decision_authority_matches(decision, value.last_event):
        raise _error("identity-mismatch", "decision origin differs from state decision")
    if value.reason_code is not None and value.reason_code != "review-limit":
        raise _error("reason-code", "unsupported policy reason")


class PolicyProjectionKind(str, Enum):
    """Typed origin kind retained by the durable handoff projection."""

    ASSIGNMENT = "assignment"
    WORKER_COMPLETION = "worker_completion"
    REVIEW_REQUEST = "review_request"
    REVIEW_DECISION = "review_decision"


@dataclass(frozen=True, slots=True, init=False)
class PolicyAuthorityProjection:
    """Bounded policy authority fields for durable handoff.

    This projection intentionally has no explanation, prompt, or provider
    output field.  It is issued only by the policy-bound projection factory;
    a handoff adapter must never accept a caller-constructed projection.
    """

    policy_fingerprint: PolicyFingerprint
    team_id: TeamId
    workspace: WorkspaceIdentity
    run_id: RunId
    task_id: TaskId
    dispatch_id: DispatchId
    attempt_id: AttemptId
    worker_node: NodeId
    reviewer_node: NodeId
    worker_terminal_id: TerminalId
    reviewer_terminal_id: TerminalId
    review_round: int
    sequence: int
    phase: TaskPhase
    event_kind: PolicyProjectionKind
    worker_completion_kind: WorkerCompletionKind | None
    review_decision_kind: ReviewDecisionKind | None
    completion_id: CompletionId | None
    decision_ref: DecisionRef | None
    target_head: GitObjectId | None
    target_tree_digest: TreeDigest | None
    reason_code: str | None

    def __init__(self) -> None:
        raise TypeError(
            "PolicyAuthorityProjection is return-only; use the policy-bound factory"
        )

    def _validate_shape(self) -> None:
        _fingerprint(self.policy_fingerprint, "projection.policy_fingerprint")
        _text(self.team_id, "projection.team_id")
        _text(self.workspace, "projection.workspace", maximum=MAX_POLICY_TEXT_CHARS)
        _text(self.run_id, "projection.run_id")
        _text(self.task_id, "projection.task_id")
        _text(self.dispatch_id, "projection.dispatch_id")
        _text(self.attempt_id, "projection.attempt_id")
        _text(self.worker_node, "projection.worker_node")
        _text(self.reviewer_node, "projection.reviewer_node")
        _text(self.worker_terminal_id, "projection.worker_terminal_id")
        _text(self.reviewer_terminal_id, "projection.reviewer_terminal_id")
        if self.worker_terminal_id == self.reviewer_terminal_id:
            raise _error(
                "independent-terminal",
                "projection Worker and Reviewer terminals must differ",
            )
        _positive_round(self.review_round, "projection.review_round")
        _sequence(self.sequence, "projection.sequence")
        if not isinstance(self.phase, TaskPhase):
            raise _error("phase", "projection.phase must be a TaskPhase")
        if not isinstance(self.event_kind, PolicyProjectionKind):
            raise _error("projection-kind", "projection.event_kind is not supported")
        for outcome, context in (
            (self.worker_completion_kind, "projection.worker_completion_kind"),
            (self.review_decision_kind, "projection.review_decision_kind"),
        ):
            if outcome is not None and not isinstance(
                outcome, (WorkerCompletionKind, ReviewDecisionKind)
            ):
                raise _error("projection-kind", f"{context} is not typed")
        if self.completion_id is not None:
            _text(self.completion_id, "projection.completion_id")
        if self.decision_ref is not None:
            _text(self.decision_ref, "projection.decision_ref")
        _target(
            self.target_head,
            self.target_tree_digest,
            required=False,
            context="projection",
        )
        if self.reason_code is not None and self.reason_code != "review-limit":
            raise _error("reason-code", "projection reason is not supported")
        if self.event_kind is PolicyProjectionKind.ASSIGNMENT:
            if (
                self.phase is not TaskPhase.ASSIGNED
                or self.worker_completion_kind is not None
                or self.review_decision_kind is not None
                or self.completion_id is not None
                or self.decision_ref is not None
                or self.reason_code is not None
            ):
                raise _error(
                    "projection-kind",
                    "assignment projection observations are inconsistent",
                )
        elif self.event_kind is PolicyProjectionKind.WORKER_COMPLETION:
            if (
                self.worker_completion_kind is None
                or self.review_decision_kind is not None
                or self.completion_id is None
                or self.decision_ref is not None
                or self.reason_code is not None
            ):
                raise _error(
                    "projection-kind",
                    "completion projection observations are inconsistent",
                )
            expected_phase = {
                WorkerCompletionKind.SUCCEEDED: TaskPhase.WORKER_DONE,
                WorkerCompletionKind.FAILED: TaskPhase.FAILED,
                WorkerCompletionKind.TIMEOUT: TaskPhase.FAILED,
                WorkerCompletionKind.QUESTION: TaskPhase.ASK_USER,
                WorkerCompletionKind.ESCALATION: TaskPhase.ASK_USER,
            }[self.worker_completion_kind]
            if self.phase is not expected_phase:
                raise _error(
                    "projection-kind", "completion projection phase is inconsistent"
                )
        elif self.event_kind is PolicyProjectionKind.REVIEW_REQUEST:
            if (
                self.phase is not TaskPhase.REVIEW_PENDING
                or self.worker_completion_kind is not WorkerCompletionKind.SUCCEEDED
                or self.review_decision_kind is not None
                or self.completion_id is None
                or self.decision_ref is not None
                or self.reason_code is not None
            ):
                raise _error(
                    "projection-kind",
                    "review request projection observations are inconsistent",
                )
        elif self.event_kind is PolicyProjectionKind.REVIEW_DECISION:
            if (
                self.review_decision_kind is None
                or self.worker_completion_kind is not None
                or self.completion_id is None
                or self.decision_ref is None
            ):
                raise _error(
                    "projection-kind",
                    "decision projection observations are inconsistent",
                )
            if self.review_decision_kind is ReviewDecisionKind.APPROVED:
                expected_phase = TaskPhase.APPROVED
            elif self.review_decision_kind is ReviewDecisionKind.CHANGES_REQUESTED:
                expected_phase = (
                    TaskPhase.ASK_USER
                    if self.reason_code == "review-limit"
                    else TaskPhase.CHANGES_REQUESTED
                )
            elif self.review_decision_kind is ReviewDecisionKind.ASK_USER:
                expected_phase = TaskPhase.ASK_USER
            else:
                expected_phase = TaskPhase.FAILED
            if self.phase is not expected_phase:
                raise _error(
                    "projection-kind", "decision projection phase is inconsistent"
                )
            if self.reason_code is not None and (
                self.review_decision_kind is not ReviewDecisionKind.CHANGES_REQUESTED
                or self.phase is not TaskPhase.ASK_USER
            ):
                raise _error("reason-code", "projection reason is inconsistent")


@dataclass(frozen=True, slots=True)
class ReviewPolicyUpdate:
    """Pure update intent consumed later by a typed policy/store port."""

    expected_sequence: int
    previous_state: ReviewPolicyState
    next_state: ReviewPolicyState
    event: ReviewPolicyEvent
    policy_fingerprint: PolicyFingerprint | None = None
    effects: tuple[ReviewerAssignment, ...] = ()
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _sequence(self.expected_sequence, "update.expected_sequence")
        if not isinstance(self.previous_state, ReviewPolicyState):
            raise _error("invalid-update", "update.previous_state is invalid")
        if not isinstance(self.next_state, ReviewPolicyState):
            raise _error("invalid-update", "update.next_state is invalid")
        fingerprint = _fingerprint(self.policy_fingerprint, "update.policy_fingerprint")
        object.__setattr__(self, "policy_fingerprint", fingerprint)
        if self.expected_sequence != self.previous_state.task_state.sequence:
            raise _error("invalid-update", "update expected sequence is not current")
        if self.next_state.task_state.sequence != self.expected_sequence + 1:
            raise _error("invalid-update", "update sequence must increment by one")
        if self.next_state.reason_code != self.reason_code:
            raise _error("invalid-update", "update reason does not match next state")
        if self.next_state.task_state.phase in (
            TaskPhase.VERIFYING,
            TaskPhase.COMPLETED,
        ):
            raise _error(
                "phase",
                "serial review policy cannot issue verifying or completed",
            )
        if not isinstance(self.effects, tuple) or any(
            not isinstance(item, ReviewerAssignment) for item in self.effects
        ):
            raise _error("invalid-update", "update.effects must be typed")
        if self.reason_code is not None:
            _text(self.reason_code, "update.reason_code")
        if not isinstance(
            self.event,
            (AssignmentCommand, WorkerCompletion, ReviewRequest, ReviewDecision),
        ):
            raise _error(
                "invalid-event", "update.event must be a supported typed event"
            )
        _validate_policy_update(self)

    def task_update(self, policy: SerialReviewPolicy) -> ExpectedSequenceUpdate:
        """Adapt the pure transition to the existing v4 state store seam."""

        validate_policy_update(self, policy)
        return ExpectedSequenceUpdate(
            expected_sequence=self.expected_sequence,
            state=self.next_state.task_state,
        )


class ReviewPolicyStorePort(Protocol):
    """Future persistence adapter; implementation owns CAS and storage."""

    def update(
        self, update: ReviewPolicyUpdate, policy: SerialReviewPolicy
    ) -> ReviewPolicyState: ...


class ReviewPolicyEffectPort(Protocol):
    """Future effect adapter for a typed Reviewer assignment intent."""

    def assign_reviewer(
        self,
        assignment: ReviewerAssignment,
        policy: SerialReviewPolicy,
        expected_state: ReviewPolicyState,
    ) -> None: ...


class ReviewPolicyHandoffPort(Protocol):
    """Future handoff seam that issues the projection from a bound update.

    An implementation receives the typed update and its actual policy, calls
    :func:`policy_authority_projection`, and persists that canonical return
    value.  It never accepts a raw ``PolicyAuthorityProjection`` from a
    caller.
    """

    def save_authority(
        self, update: ReviewPolicyUpdate, policy: SerialReviewPolicy
    ) -> ReviewAuthorityRef: ...


def initial_review_policy_state(
    run_id: RunId, task_state: TaskPolicyStateV4
) -> ReviewPolicyState:
    """Create an explicitly pending policy observation for one Run and task."""

    if not isinstance(task_state, TaskPolicyStateV4):
        raise _error("invalid-state", "task_state must be TaskPolicyStateV4")
    if task_state.phase is not TaskPhase.PENDING:
        raise _error("phase", "initial policy state must be pending")
    if (
        task_state.attempt_id is not None
        or task_state.dispatch_id is not None
        or task_state.worker_node is not None
        or task_state.reviewer_node is not None
        or task_state.review_round != 0
        or task_state.target_head is not None
        or task_state.target_tree_digest is not None
    ):
        raise _error(
            "identity-mismatch", "initial pending state must have no active assignment"
        )
    return ReviewPolicyState(RunId(_text(run_id, "run_id")), task_state)


def _check_dependencies(policy: SerialReviewPolicy) -> None:
    expected = {str(item) for item in policy.task.dependencies}
    seen: set[str] = set()
    for dependency in policy.dependency_states:
        value = str(dependency.task_id)
        if value not in expected:
            raise _error(
                "unknown-dependency", "dependency state is not declared by the task"
            )
        folded = value.casefold()
        if folded in seen:
            raise _error("duplicate-dependency", "dependency state is duplicated")
        seen.add(folded)
        if dependency.phase not in (TaskPhase.APPROVED, TaskPhase.COMPLETED):
            raise _error("dependency-unmet", "all task dependencies must be approved")
    if seen != {value.casefold() for value in expected}:
        raise _error(
            "dependency-unmet",
            "every declared dependency needs an explicit approved state",
        )


def _identity_matches_assignment(
    assignment: WorkerAssignment,
    value: WorkerCompletion | ReviewDecision,
) -> bool:
    return (
        value.run_id == assignment.run_id
        and value.task_id == assignment.task_id
        and value.dispatch_id == assignment.dispatch_id
        and value.attempt_id == assignment.attempt_id
        and value.worker_node == assignment.worker_node
        and value.reviewer_node == assignment.reviewer_node
        and value.review_round == assignment.review_round
        and value.worker_terminal_id == assignment.worker_terminal_id
        and (
            not isinstance(value, ReviewDecision)
            or value.reviewer_terminal_id == assignment.reviewer_terminal_id
        )
    )


def _completion_authority_matches(
    expected: WorkerCompletion, observed: WorkerCompletion
) -> bool:
    """Compare only completion identity, outcome, and target authority."""

    return (
        expected.identity == observed.identity
        and expected.expected_sequence == observed.expected_sequence
        and expected.kind is observed.kind
        and expected.target_head == observed.target_head
        and expected.target_tree_digest == observed.target_tree_digest
    )


def _decision_authority_matches(
    expected: ReviewDecision, observed: ReviewDecision
) -> bool:
    """Compare decision identity/kind while ignoring explanatory text."""

    return (
        expected.identity == observed.identity
        and expected.kind is observed.kind
        and expected.expected_sequence == observed.expected_sequence
    )


def _target_matches(
    head: GitObjectId | None,
    tree_digest: TreeDigest | None,
    expected_head: GitObjectId | None,
    expected_tree: TreeDigest | None,
) -> bool:
    return head == expected_head and tree_digest == expected_tree


def _next_state(
    current: ReviewPolicyState,
    *,
    phase: TaskPhase,
    assignment: WorkerAssignment | None,
    completion: WorkerCompletion | None,
    decision: ReviewDecision | None,
    target_head: GitObjectId | None,
    target_tree_digest: TreeDigest | None,
    last_event: ReviewPolicyEvent,
    reason_code: str | None = None,
) -> ReviewPolicyState:
    task_state = current.task_state
    if assignment is None:
        attempt_id: AttemptId | None = None
        dispatch_id: DispatchId | None = None
        worker_node: NodeId | None = None
        reviewer_node: NodeId | None = None
        review_round = task_state.review_round
    else:
        attempt_id = assignment.attempt_id
        dispatch_id = assignment.dispatch_id
        worker_node = assignment.worker_node
        reviewer_node = assignment.reviewer_node
        review_round = assignment.review_round
    next_task_state = replace(
        task_state,
        sequence=task_state.sequence + 1,
        attempt_id=attempt_id,
        dispatch_id=dispatch_id,
        worker_node=worker_node,
        reviewer_node=reviewer_node,
        review_round=review_round,
        target_head=target_head,
        target_tree_digest=target_tree_digest,
        phase=phase,
    )
    return ReviewPolicyState(
        run_id=current.run_id,
        task_state=next_task_state,
        assignment=assignment,
        completion=completion,
        decision=decision,
        last_event=last_event,
        reason_code=reason_code,
    )


def _make_update(
    current: ReviewPolicyState,
    event: ReviewPolicyEvent,
    next_state: ReviewPolicyState,
    *,
    effects: tuple[ReviewerAssignment, ...] = (),
    reason_code: str | None = None,
    policy_fingerprint: PolicyFingerprint,
) -> ReviewPolicyUpdate:
    return ReviewPolicyUpdate(
        expected_sequence=current.task_state.sequence,
        previous_state=current,
        next_state=next_state,
        event=event,
        policy_fingerprint=policy_fingerprint,
        effects=effects,
        reason_code=reason_code,
    )


def _event_authority_matches(
    expected: ReviewPolicyEvent, observed: ReviewPolicyEvent
) -> bool:
    if type(expected) is not type(observed):
        return False
    if isinstance(expected, AssignmentCommand) and isinstance(
        observed, AssignmentCommand
    ):
        return (
            expected.expected_sequence == observed.expected_sequence
            and expected.assignment == observed.assignment
        )
    if isinstance(expected, WorkerCompletion) and isinstance(
        observed, WorkerCompletion
    ):
        return (
            expected.expected_sequence == observed.expected_sequence
            and _completion_authority_matches(expected, observed)
        )
    if isinstance(expected, ReviewRequest) and isinstance(observed, ReviewRequest):
        return (
            expected.expected_sequence == observed.expected_sequence
            and _completion_authority_matches(expected.completion, observed.completion)
        )
    if isinstance(expected, ReviewDecision) and isinstance(observed, ReviewDecision):
        return _decision_authority_matches(expected, observed)
    return False


def _allowed_phase_edge(
    event: ReviewPolicyEvent, previous: TaskPhase, next_phase: TaskPhase
) -> bool:
    if isinstance(event, AssignmentCommand):
        return (
            previous in (TaskPhase.PENDING, TaskPhase.CHANGES_REQUESTED)
            and next_phase is TaskPhase.ASSIGNED
        )
    if isinstance(event, WorkerCompletion):
        expected = {
            WorkerCompletionKind.SUCCEEDED: TaskPhase.WORKER_DONE,
            WorkerCompletionKind.FAILED: TaskPhase.FAILED,
            WorkerCompletionKind.TIMEOUT: TaskPhase.FAILED,
            WorkerCompletionKind.QUESTION: TaskPhase.ASK_USER,
            WorkerCompletionKind.ESCALATION: TaskPhase.ASK_USER,
        }[event.kind]
        return previous is TaskPhase.ASSIGNED and next_phase is expected
    if isinstance(event, ReviewRequest):
        return (
            previous is TaskPhase.WORKER_DONE and next_phase is TaskPhase.REVIEW_PENDING
        )
    expected_phases: tuple[TaskPhase, ...] = {
        ReviewDecisionKind.APPROVED: (TaskPhase.APPROVED,),
        ReviewDecisionKind.CHANGES_REQUESTED: (
            TaskPhase.CHANGES_REQUESTED,
            TaskPhase.ASK_USER,
        ),
        ReviewDecisionKind.ASK_USER: (TaskPhase.ASK_USER,),
        ReviewDecisionKind.FAILED: (TaskPhase.FAILED,),
    }[event.kind]
    return previous is TaskPhase.REVIEW_PENDING and next_phase in expected_phases


def _state_authority_matches(
    expected: ReviewPolicyState, observed: ReviewPolicyState
) -> bool:
    if (
        expected.run_id != observed.run_id
        or expected.task_state != observed.task_state
        or expected.assignment != observed.assignment
        or expected.reason_code != observed.reason_code
    ):
        return False
    if (expected.completion is None) != (observed.completion is None):
        return False
    if (
        expected.completion is not None
        and observed.completion is not None
        and not _completion_authority_matches(expected.completion, observed.completion)
    ):
        return False
    if (expected.decision is None) != (observed.decision is None):
        return False
    if (
        expected.decision is not None
        and observed.decision is not None
        and not _decision_authority_matches(expected.decision, observed.decision)
    ):
        return False
    if (expected.last_event is None) != (observed.last_event is None):
        return False
    return (
        expected.last_event is None
        or observed.last_event is None
        or _event_authority_matches(expected.last_event, observed.last_event)
    )


def _effect_authority_matches(
    expected: ReviewerAssignment, observed: ReviewerAssignment
) -> bool:
    return (
        expected.policy_fingerprint == observed.policy_fingerprint
        and expected.assignment.identity == observed.assignment.identity
        and _target_matches(
            expected.assignment.target_head,
            expected.assignment.target_tree_digest,
            observed.assignment.target_head,
            observed.assignment.target_tree_digest,
        )
        and _completion_authority_matches(expected.completion, observed.completion)
    )


def _validate_policy_update(
    value: ReviewPolicyUpdate, policy: SerialReviewPolicy | None = None
) -> None:
    """Reject public update values that were not produced by this reducer."""

    previous = value.previous_state
    next_state = value.next_state
    event = value.event
    if not isinstance(value.policy_fingerprint, str):
        raise _error("policy-fingerprint", "update.policy_fingerprint is required")
    _validate_policy_state(previous)
    _validate_policy_state(next_state)
    if policy is not None:
        if not isinstance(policy, SerialReviewPolicy):
            raise _error("invalid-policy", "policy must be a SerialReviewPolicy")
        _validate_policy_instance(policy)
        _check_dependencies(policy)
        if isinstance(event, AssignmentCommand) and policy.active_assignments:
            raise _error(
                "active-assignment",
                "normal and express lanes already have an active write assignment",
            )
        if value.policy_fingerprint != policy.fingerprint:
            raise _error(
                "policy-fingerprint", "update policy differs from current policy"
            )
        _validate_current_identity(previous, policy)
        _validate_current_identity(next_state, policy)
        if (
            previous.task_state.review_round > policy.max_review_rounds
            or next_state.task_state.review_round > policy.max_review_rounds
        ):
            raise _error(
                "review-limit", "update contains a state beyond max_review_rounds"
            )
    if not _allowed_phase_edge(
        event,
        previous.task_state.phase,
        next_state.task_state.phase,
    ):
        raise _error("phase", "update event and phase edge are inconsistent")
    if event.expected_sequence != value.expected_sequence:
        raise _error(
            "stale-sequence", "update event sequence differs from expected sequence"
        )
    if (
        previous.run_id != next_state.run_id
        or previous.task_state.team_id != next_state.task_state.team_id
        or previous.task_state.workspace != next_state.task_state.workspace
        or previous.task_state.task_id != next_state.task_state.task_id
    ):
        raise _error("identity-mismatch", "update changes policy state identity")
    if (
        previous.task_state.claim_ref != next_state.task_state.claim_ref
        or previous.task_state.receipt_ref != next_state.task_state.receipt_ref
    ):
        raise _error(
            "identity-mismatch",
            "review policy update cannot change claim or receipt identity",
        )
    if next_state.last_event is None or not _event_authority_matches(
        event, next_state.last_event
    ):
        raise _error("identity-mismatch", "update event differs from next state origin")
    if isinstance(event, AssignmentCommand):
        if policy is not None:
            _check_dependencies(policy)
            if (
                event.assignment.run_id != previous.run_id
                or event.assignment.task_id != policy.task.task_id
                or event.assignment.worker_node != policy.pair.worker_node
                or event.assignment.reviewer_node != policy.pair.reviewer_node
            ):
                raise _error(
                    "identity-mismatch", "assignment event does not match policy"
                )
        if next_state.assignment != event.assignment:
            raise _error(
                "identity-mismatch", "assignment event differs from next observation"
            )
        if next_state.completion is not None or next_state.decision is not None:
            raise _error("phase", "assignment update contains later observations")
        if value.effects:
            raise _error("invalid-effect", "assignment update cannot carry effects")
        if value.reason_code is not None:
            raise _error("phase", "assignment update cannot carry a reason")
        if policy is not None:
            canonical = reduce_policy(previous, event, policy)
            if not _state_authority_matches(canonical.next_state, next_state):
                raise _error(
                    "invalid-update", "assignment update differs from reducer result"
                )
        return
    if isinstance(event, WorkerCompletion):
        if previous.assignment is None or not _identity_matches_assignment(
            previous.assignment, event
        ):
            raise _error(
                "identity-mismatch", "completion event differs from previous assignment"
            )
        if next_state.assignment != previous.assignment:
            raise _error(
                "identity-mismatch", "completion update changes assignment identity"
            )
        if next_state.completion is None or not _completion_authority_matches(
            event, next_state.completion
        ):
            raise _error(
                "identity-mismatch", "completion event differs from next observation"
            )
        if (
            next_state.decision is not None
            or value.effects
            or value.reason_code is not None
        ):
            raise _error("phase", "completion update contains an invalid observation")
        if policy is not None:
            canonical = reduce_policy(previous, event, policy)
            if not _state_authority_matches(canonical.next_state, next_state):
                raise _error(
                    "invalid-update", "completion update differs from reducer result"
                )
        return
    if isinstance(event, ReviewRequest):
        if previous.assignment is None or previous.completion is None:
            raise _error(
                "identity-mismatch", "review request has no previous completion"
            )
        if not _completion_authority_matches(previous.completion, event.completion):
            raise _error(
                "identity-mismatch",
                "review request completion differs from previous completion",
            )
        if next_state.assignment != previous.assignment:
            raise _error(
                "identity-mismatch", "review request changes assignment identity"
            )
        if next_state.completion is None or not _completion_authority_matches(
            event.completion, next_state.completion
        ):
            raise _error(
                "identity-mismatch", "review request differs from next observation"
            )
        if next_state.decision is not None or value.reason_code is not None:
            raise _error("phase", "review request carries an invalid observation")
        if len(value.effects) != 1:
            raise _error(
                "invalid-effect", "review request must carry one ReviewerAssignment"
            )
        effect = value.effects[0]
        if effect.assignment.identity != previous.assignment.identity:
            raise _error(
                "identity-mismatch", "Reviewer effect assignment identity differs"
            )
        if not _target_matches(
            effect.assignment.target_head,
            effect.assignment.target_tree_digest,
            event.completion.target_head,
            event.completion.target_tree_digest,
        ):
            raise _error(
                "target-identity", "Reviewer effect target differs from completion"
            )
        if not _completion_authority_matches(effect.completion, event.completion):
            raise _error(
                "identity-mismatch", "Reviewer effect completion differs from event"
            )
        if policy is not None:
            validate_reviewer_assignment(effect, policy, previous)
            canonical = reduce_policy(previous, event, policy)
            if not _state_authority_matches(canonical.next_state, next_state):
                raise _error(
                    "invalid-update", "review request differs from reducer result"
                )
            if len(canonical.effects) != len(value.effects) or not all(
                _effect_authority_matches(expected_effect, observed_effect)
                for expected_effect, observed_effect in zip(
                    canonical.effects, value.effects
                )
            ):
                raise _error(
                    "invalid-effect", "Reviewer effects differ from reducer result"
                )
        return
    if previous.assignment is None or previous.completion is None:
        raise _error("identity-mismatch", "decision has no previous review observation")
    if next_state.assignment != previous.assignment:
        raise _error("identity-mismatch", "decision changes assignment identity")
    if next_state.completion is None or not _completion_authority_matches(
        previous.completion, next_state.completion
    ):
        raise _error("identity-mismatch", "decision changes completion observation")
    if next_state.decision is None or not _decision_authority_matches(
        event, next_state.decision
    ):
        raise _error("identity-mismatch", "decision differs from next observation")
    if value.effects:
        raise _error("invalid-effect", "decision update cannot carry effects")
    if event.kind is ReviewDecisionKind.CHANGES_REQUESTED:
        if next_state.task_state.phase is TaskPhase.ASK_USER:
            if value.reason_code != "review-limit":
                raise _error(
                    "review-limit", "limit outcome requires review-limit reason"
                )
        elif value.reason_code is not None:
            raise _error("phase", "changes_requested update cannot carry a reason")
    elif value.reason_code is not None:
        raise _error("phase", "decision update cannot carry a reason")
    if policy is not None:
        canonical = reduce_policy(previous, event, policy)
        if not _state_authority_matches(canonical.next_state, next_state):
            raise _error(
                "invalid-update", "decision update differs from reducer result"
            )


def validate_policy_update(
    value: ReviewPolicyUpdate, policy: SerialReviewPolicy
) -> ReviewPolicyUpdate:
    """Revalidate an update before a future store adapter accepts it."""

    if not isinstance(value, ReviewPolicyUpdate):
        raise _error("invalid-update", "value must be a ReviewPolicyUpdate")
    if not isinstance(policy, SerialReviewPolicy):
        raise _error("invalid-policy", "policy must be a SerialReviewPolicy")
    _validate_policy_update(value, policy)
    return value


def policy_authority_projection(
    update: ReviewPolicyUpdate, policy: SerialReviewPolicy
) -> PolicyAuthorityProjection:
    """Project an accepted update into bounded durable authority fields."""

    validate_policy_update(update, policy)
    next_state = update.next_state
    assignment = next_state.assignment
    if assignment is None:
        raise _error("invalid-update", "accepted update has no assignment")
    event = update.event
    completion_kind: WorkerCompletionKind | None = None
    decision_kind: ReviewDecisionKind | None = None
    completion_id: CompletionId | None = None
    decision_ref: DecisionRef | None = None
    target_head: GitObjectId | None = next_state.task_state.target_head
    target_tree: TreeDigest | None = next_state.task_state.target_tree_digest
    if isinstance(event, AssignmentCommand):
        event_kind = PolicyProjectionKind.ASSIGNMENT
    elif isinstance(event, WorkerCompletion):
        event_kind = PolicyProjectionKind.WORKER_COMPLETION
        completion_kind = event.kind
        completion_id = event.completion_id
    elif isinstance(event, ReviewRequest):
        event_kind = PolicyProjectionKind.REVIEW_REQUEST
        completion_kind = event.completion.kind
        completion_id = event.completion.completion_id
    else:
        event_kind = PolicyProjectionKind.REVIEW_DECISION
        decision_kind = event.kind
        completion_id = event.completion_id
        decision_ref = event.decision_ref
    projection = object.__new__(PolicyAuthorityProjection)
    object.__setattr__(projection, "policy_fingerprint", policy.fingerprint)
    object.__setattr__(projection, "team_id", next_state.task_state.team_id)
    object.__setattr__(projection, "workspace", next_state.task_state.workspace)
    object.__setattr__(projection, "run_id", next_state.run_id)
    object.__setattr__(projection, "task_id", next_state.task_state.task_id)
    object.__setattr__(projection, "dispatch_id", assignment.dispatch_id)
    object.__setattr__(projection, "attempt_id", assignment.attempt_id)
    object.__setattr__(projection, "worker_node", assignment.worker_node)
    object.__setattr__(projection, "reviewer_node", assignment.reviewer_node)
    object.__setattr__(projection, "worker_terminal_id", assignment.worker_terminal_id)
    object.__setattr__(
        projection, "reviewer_terminal_id", assignment.reviewer_terminal_id
    )
    object.__setattr__(projection, "review_round", assignment.review_round)
    object.__setattr__(projection, "sequence", next_state.task_state.sequence)
    object.__setattr__(projection, "phase", next_state.task_state.phase)
    object.__setattr__(projection, "event_kind", event_kind)
    object.__setattr__(projection, "worker_completion_kind", completion_kind)
    object.__setattr__(projection, "review_decision_kind", decision_kind)
    object.__setattr__(projection, "completion_id", completion_id)
    object.__setattr__(projection, "decision_ref", decision_ref)
    object.__setattr__(projection, "target_head", target_head)
    object.__setattr__(projection, "target_tree_digest", target_tree)
    object.__setattr__(projection, "reason_code", update.reason_code)
    projection._validate_shape()
    return projection


def _projection_authority_matches(
    expected: PolicyAuthorityProjection, observed: PolicyAuthorityProjection
) -> bool:
    """Compare every durable projection field without trusting dataclass equality."""

    if type(expected) is not type(observed):
        return False
    missing = object()
    return all(
        getattr(expected, item.name, missing) == getattr(observed, item.name, missing)
        for item in fields(expected)
    )


def validate_policy_authority_projection(
    value: PolicyAuthorityProjection,
    update: ReviewPolicyUpdate,
    policy: SerialReviewPolicy,
) -> PolicyAuthorityProjection:
    """Validate a returned projection against its update and actual policy.

    This is a verification seam for a future adapter, not a raw-save API.  The
    canonical projection is recomputed from the policy-bound update and every
    field must match it exactly.
    """

    if not isinstance(value, PolicyAuthorityProjection):
        raise _error("invalid-projection", "value must be a PolicyAuthorityProjection")
    if not isinstance(update, ReviewPolicyUpdate):
        raise _error("invalid-update", "update must be a ReviewPolicyUpdate")
    if not isinstance(policy, SerialReviewPolicy):
        raise _error("invalid-policy", "policy must be a SerialReviewPolicy")
    canonical = policy_authority_projection(update, policy)
    if canonical.phase is TaskPhase.APPROVED and (
        canonical.target_head is None or canonical.target_tree_digest is None
    ):
        raise _error(
            "target-identity", "approved authority projection requires target identity"
        )
    if not _projection_authority_matches(canonical, value):
        raise _error(
            "invalid-projection",
            "projection does not match the policy-bound canonical authority",
        )
    return value


def _reject_phase(current: ReviewPolicyState, expected: TaskPhase) -> None:
    if current.task_state.phase is not expected:
        raise _error(
            "phase",
            f"event requires phase {expected.value}; current phase is {current.task_state.phase.value}",
        )


def _validate_current_identity(
    current: ReviewPolicyState, policy: SerialReviewPolicy
) -> None:
    state = current.task_state
    if state.version != STATE_POLICY_VERSION:
        raise _error("state-version", "policy state must use task policy version 4")
    if state.team_id != policy.team_definition.team_id:
        raise _error(
            "identity-mismatch", "policy state team identity does not match topology"
        )
    if state.task_id != policy.task.task_id:
        raise _error(
            "identity-mismatch", "policy state task identity does not match policy"
        )
    assignment = current.assignment
    if assignment is not None:
        if assignment.run_id != current.run_id or assignment.task_id != state.task_id:
            raise _error(
                "identity-mismatch", "assignment identity does not match policy state"
            )
        if (
            assignment.worker_node != policy.pair.worker_node
            or assignment.reviewer_node != policy.pair.reviewer_node
        ):
            raise _error(
                "identity-mismatch", "assignment pair does not match policy topology"
            )
        if (
            state.attempt_id != assignment.attempt_id
            or state.dispatch_id != assignment.dispatch_id
            or state.worker_node != assignment.worker_node
            or state.reviewer_node != assignment.reviewer_node
            or state.review_round != assignment.review_round
        ):
            raise _error(
                "identity-mismatch", "assignment identity does not match v4 state"
            )
        if assignment.target_head is not None and not _target_matches(
            state.target_head,
            state.target_tree_digest,
            assignment.target_head,
            assignment.target_tree_digest,
        ):
            raise _error("target-identity", "assignment target does not match v4 state")
        if (
            current.completion is None
            and assignment.target_head is None
            and (state.target_head is not None or state.target_tree_digest is not None)
        ):
            raise _error(
                "target-identity",
                "uncommitted target identity is not valid for assignment",
            )
    if current.completion is not None:
        if assignment is None or not _identity_matches_assignment(
            assignment, current.completion
        ):
            raise _error(
                "identity-mismatch", "completion identity does not match assignment"
            )
        if not _target_matches(
            state.target_head,
            state.target_tree_digest,
            current.completion.target_head,
            current.completion.target_tree_digest,
        ):
            raise _error("target-identity", "completion target does not match v4 state")
    if current.decision is not None:
        if assignment is None or not _identity_matches_assignment(
            assignment, current.decision
        ):
            raise _error(
                "identity-mismatch", "decision identity does not match assignment"
            )
        if (
            current.completion is None
            or current.decision.completion_id != current.completion.completion_id
        ):
            raise _error(
                "identity-mismatch",
                "decision completion identity does not match completion",
            )
        if current.completion is None or not _target_matches(
            current.decision.target_head,
            current.decision.target_tree_digest,
            current.completion.target_head,
            current.completion.target_tree_digest,
        ):
            raise _error("target-identity", "decision target does not match completion")


def reduce_policy(
    current: ReviewPolicyState,
    event: ReviewPolicyEvent,
    policy: SerialReviewPolicy,
) -> ReviewPolicyUpdate:
    """Reduce one explicit event into one typed state/effect intent.

    The function is pure: stale sequence, identity, dependency, and phase
    errors are raised before any update or effect intent is returned.  It has
    no branch that emits ``verifying`` or ``completed``.
    """

    if not isinstance(current, ReviewPolicyState):
        raise _error("invalid-state", "current must be ReviewPolicyState")
    if not isinstance(policy, SerialReviewPolicy):
        raise _error("invalid-policy", "policy must be SerialReviewPolicy")
    if not isinstance(
        event, (AssignmentCommand, WorkerCompletion, ReviewRequest, ReviewDecision)
    ):
        raise _error("invalid-event", "event is not a supported typed policy event")
    _validate_policy_state(current)
    _validate_current_identity(current, policy)
    if current.task_state.review_round > policy.max_review_rounds:
        raise _error("review-limit", "current state exceeds max_review_rounds")
    if event.expected_sequence != current.task_state.sequence:
        raise _error(
            "stale-sequence", "event expected sequence does not match current state"
        )

    state = current.task_state
    if isinstance(event, AssignmentCommand):
        if state.phase not in (TaskPhase.PENDING, TaskPhase.CHANGES_REQUESTED):
            raise _error(
                "phase", "assignment is only legal from pending or changes_requested"
            )
        _check_dependencies(policy)
        new_assignment = event.assignment
        if new_assignment.run_id != current.run_id:
            raise _error(
                "identity-mismatch", "assignment Run identity does not match state"
            )
        if new_assignment.task_id != policy.task.task_id:
            raise _error(
                "identity-mismatch", "assignment Task identity does not match policy"
            )
        if new_assignment.worker_node != policy.pair.worker_node:
            raise _error(
                "identity-mismatch", "assignment Worker identity does not match pair"
            )
        if new_assignment.reviewer_node != policy.pair.reviewer_node:
            raise _error(
                "reviewer-mismatch", "assignment Reviewer identity does not match pair"
            )
        if new_assignment.review_round > policy.max_review_rounds:
            raise _error("review-limit", "assignment exceeds max_review_rounds")
        if policy.active_assignments:
            raise _error(
                "active-assignment",
                "normal and express lanes already have an active write assignment",
            )
        if state.phase is TaskPhase.PENDING:
            if new_assignment.review_round != 1:
                raise _error(
                    "review-round", "initial assignment must start review round 1"
                )
        else:
            if state.attempt_id is None or state.dispatch_id is None:
                raise _error(
                    "identity-mismatch",
                    "changes_requested state has no prior attempt identity",
                )
            if (
                new_assignment.attempt_id == state.attempt_id
                or new_assignment.dispatch_id == state.dispatch_id
            ):
                raise _error(
                    "new-attempt-required",
                    "retry requires new attempt and dispatch identities",
                )
            if new_assignment.review_round != state.review_round + 1:
                raise _error("review-round", "retry must increment review round by one")
            if new_assignment.review_round > policy.max_review_rounds:
                raise _error("review-limit", "retry would exceed max_review_rounds")
        return _make_update(
            current,
            event,
            _next_state(
                current,
                phase=TaskPhase.ASSIGNED,
                assignment=new_assignment,
                completion=None,
                decision=None,
                target_head=new_assignment.target_head,
                target_tree_digest=new_assignment.target_tree_digest,
                last_event=event,
            ),
            policy_fingerprint=policy.fingerprint,
        )

    if isinstance(event, WorkerCompletion):
        _reject_phase(current, TaskPhase.ASSIGNED)
        active_assignment = current.assignment
        if active_assignment is None:
            raise _error(
                "identity-mismatch", "assigned state has no assignment identity"
            )
        if not _identity_matches_assignment(active_assignment, event):
            raise _error(
                "identity-mismatch",
                "Worker completion identity does not match assignment",
            )
        if active_assignment.target_head is not None and not _target_matches(
            event.target_head,
            event.target_tree_digest,
            active_assignment.target_head,
            active_assignment.target_tree_digest,
        ):
            raise _error(
                "target-identity", "completion target does not match assignment target"
            )
        if event.kind is WorkerCompletionKind.SUCCEEDED:
            if event.target_head is None or event.target_tree_digest is None:
                raise _error(
                    "target-identity", "successful completion requires target identity"
                )
            phase = TaskPhase.WORKER_DONE
        elif event.kind in (
            WorkerCompletionKind.QUESTION,
            WorkerCompletionKind.ESCALATION,
        ):
            phase = TaskPhase.ASK_USER
        else:
            phase = TaskPhase.FAILED
        return _make_update(
            current,
            event,
            _next_state(
                current,
                phase=phase,
                assignment=active_assignment,
                completion=event,
                decision=None,
                target_head=event.target_head,
                target_tree_digest=event.target_tree_digest,
                last_event=event,
            ),
            policy_fingerprint=policy.fingerprint,
        )

    if isinstance(event, ReviewRequest):
        _reject_phase(current, TaskPhase.WORKER_DONE)
        if current.assignment is None or current.completion is None:
            raise _error(
                "identity-mismatch", "worker_done state has no completion identity"
            )
        if not _completion_authority_matches(current.completion, event.completion):
            raise _error(
                "identity-mismatch",
                "review request completion does not match worker completion",
            )
        if (
            event.completion.target_head is None
            or event.completion.target_tree_digest is None
        ):
            raise _error("target-identity", "review request requires target identity")
        reviewer_assignment = ReviewerAssignment(
            replace(
                current.assignment,
                target_head=event.completion.target_head,
                target_tree_digest=event.completion.target_tree_digest,
            ),
            event.completion,
            policy_fingerprint=policy.fingerprint,
        )
        return _make_update(
            current,
            event,
            _next_state(
                current,
                phase=TaskPhase.REVIEW_PENDING,
                assignment=current.assignment,
                completion=current.completion,
                decision=None,
                target_head=event.completion.target_head,
                target_tree_digest=event.completion.target_tree_digest,
                last_event=event,
            ),
            effects=(reviewer_assignment,),
            policy_fingerprint=policy.fingerprint,
        )

    _reject_phase(current, TaskPhase.REVIEW_PENDING)
    if current.assignment is None or current.completion is None:
        raise _error(
            "identity-mismatch",
            "review_pending state has no assignment/completion identity",
        )
    if not _identity_matches_assignment(current.assignment, event):
        if event.reviewer_node != current.assignment.reviewer_node:
            raise _error(
                "reviewer-mismatch", "decision Reviewer identity does not match pair"
            )
        raise _error(
            "identity-mismatch", "Reviewer decision identity does not match assignment"
        )
    if event.completion_id != current.completion.completion_id:
        raise _error(
            "identity-mismatch",
            "decision completion identity does not match Worker completion",
        )
    if event.reviewer_node != policy.pair.reviewer_node:
        raise _error(
            "reviewer-mismatch", "decision Reviewer identity does not match pair"
        )
    if not _target_matches(
        event.target_head,
        event.target_tree_digest,
        current.completion.target_head,
        current.completion.target_tree_digest,
    ):
        raise _error(
            "target-identity", "decision target does not match Worker completion"
        )
    if event.kind is ReviewDecisionKind.APPROVED:
        phase = TaskPhase.APPROVED
        reason_code = None
    elif event.kind is ReviewDecisionKind.CHANGES_REQUESTED:
        if state.review_round >= policy.max_review_rounds:
            phase = TaskPhase.ASK_USER
            reason_code = "review-limit"
        else:
            phase = TaskPhase.CHANGES_REQUESTED
            reason_code = None
    elif event.kind is ReviewDecisionKind.ASK_USER:
        phase = TaskPhase.ASK_USER
        reason_code = None
    else:
        phase = TaskPhase.FAILED
        reason_code = None
    return _make_update(
        current,
        event,
        _next_state(
            current,
            phase=phase,
            assignment=current.assignment,
            completion=current.completion,
            decision=event,
            target_head=event.target_head,
            target_tree_digest=event.target_tree_digest,
            last_event=event,
            reason_code=reason_code,
        ),
        reason_code=reason_code,
        policy_fingerprint=policy.fingerprint,
    )
