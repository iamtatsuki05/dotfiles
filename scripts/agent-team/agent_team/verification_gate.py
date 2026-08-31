"""Pure, durable fixed-argv verification completion gate.

The public seam is deliberately small: a composition root constructs a gate
with trusted ports, then calls ``start(approval_ref)`` and ``resume(handle)``.
Approval/routing provenance, prepared state, effect fencing, and normalized
receipts are owned by those ports.  This module never executes a shell,
touches a filesystem, or persists a value itself.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Literal, NewType, Protocol

from .task_policy import (
    ClaimRef,
    GitObjectId,
    ReceiptRef,
    TaskLane,
    TaskPhase,
    TreeDigest,
    VerificationProfileRef,
    WorkspaceIdentity,
)

ApprovalRef = NewType("ApprovalRef", str)
VerificationRef = NewType("VerificationRef", str)
VerificationId = NewType("VerificationId", str)
EffectNonce = NewType("EffectNonce", str)
EnvName = NewType("EnvName", str)
ArgvDigest = NewType("ArgvDigest", str)
OutputDigest = NewType("OutputDigest", str)
ReceiptDigest = NewType("ReceiptDigest", str)
ResultSchemaId = NewType("ResultSchemaId", str)
VerificationProfileBindingDigest = NewType("VerificationProfileBindingDigest", str)

MAX_IDENTIFIER_CHARS: Final = 256
MAX_PROFILE_TEXT_CHARS: Final = 4096
MAX_ARGV_ITEMS: Final = 128
MAX_ARGV_ELEMENT_CHARS: Final = 4096
MAX_ENV_ITEMS: Final = 32
MAX_ENV_VALUE_CHARS: Final = 256
MAX_TIMEOUT_MS: Final = 86_400_000
MAX_OUTPUT_LIMIT_BYTES: Final = 64 * 1024 * 1024
MAX_OUTPUT_BYTES: Final = MAX_OUTPUT_LIMIT_BYTES
_SHA256: Final = re.compile(r"[0-9a-f]{64}\Z")
_ENV_NAME: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SECRET_VALUE: Final = re.compile(
    r"(?i)(?:token|secret|password|private[_-]?key|authorization|bearer)\s*[:=]\s*\S+"
    r"|\b(?:sk|rk|ghp|github_pat|xox[baprs])-[A-Za-z0-9_-]{12,}\b"
)
_CWD_POLICY: Final = "canonical-workspace"
_WORKSPACE_PLACEHOLDER: Final = "{workspace}"
_SAFE_ENV_NAMES: Final = frozenset(
    {"CI", "LANG", "LC_ALL", "LC_CTYPE", "NO_COLOR", "TERM", "TZ"}
)

_APPROVAL_ISSUER: Final = object()
_BOUND_ISSUER: Final = object()
_REQUEST_ISSUER: Final = object()
_PREPARE_ISSUER: Final = object()
_EFFECT_ISSUER: Final = object()
_RECEIPT_ISSUER: Final = object()
_RECORD_ISSUER: Final = object()
_EVIDENCE_ISSUER: Final = object()
_HANDLE_ISSUER: Final = object()
_TERMINAL_ISSUER: Final = object()


class VerificationGateError(ValueError):
    """Raised when a verification contract is not admissible."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code if type(code) is str and code else "invalid-verification-gate"
        super().__init__(f"{self.code}: {message}")


class RecoveryRequired(VerificationGateError):
    """Raised when an external effect cannot be classified safely."""

    def __init__(self, reason_code: str) -> None:
        _safe_text(reason_code, "recovery.reason_code", MAX_IDENTIFIER_CHARS)
        self.reason_code = reason_code
        super().__init__("recovery-required", reason_code)


class VerificationOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    SCHEMA_INVALID = "schema_invalid"
    RUNNER_UNAVAILABLE = "runner_unavailable"
    UNKNOWN_EFFECT = "unknown_effect"


class CleanupStatus(str, Enum):
    NOT_STARTED = "not_started"
    REAPED = "reaped"
    UNKNOWN = "unknown"


class PreparationStatus(str, Enum):
    PREPARED = "prepared"
    EXISTING = "existing"
    UNKNOWN = "unknown"


class EffectBeginStatus(str, Enum):
    RUN_ONCE = "run_once"
    RECEIPTED = "receipted"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


class DurableRecordStatus(str, Enum):
    PREPARED = "prepared"
    RECEIPTED = "receipted"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


def _error(code: str, message: str) -> VerificationGateError:
    return VerificationGateError(code, message)


def _safe_text(value: object, context: str, maximum: int) -> str:
    if type(value) is not str:
        raise _error("invalid-type", f"{context} must be an exact string")
    if not value or not value.strip():
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


def _identifier(value: object, context: str) -> str:
    return _safe_text(value, context, MAX_IDENTIFIER_CHARS)


def _digest(value: object, context: str) -> str:
    candidate = _safe_text(value, context, 64)
    if _SHA256.fullmatch(candidate) is None:
        raise _error("invalid-digest", f"{context} must be a lowercase SHA-256 digest")
    return candidate


def _git_object_id(value: object, context: str) -> None:
    candidate = _safe_text(value, context, 64)
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", candidate) is None:
        raise _error(
            "invalid-target-head", f"{context} must be a lowercase Git object ID"
        )


def _tree_digest(value: object, context: str) -> None:
    if _SHA256.fullmatch(_safe_text(value, context, 64)) is None:
        raise _error(
            "invalid-tree-digest", f"{context} must be a lowercase SHA-256 digest"
        )


def _canonical_path(value: object, context: str) -> str:
    candidate = _safe_text(value, context, MAX_PROFILE_TEXT_CHARS)
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        raise _error(
            "noncanonical-path", f"{context} must be an absolute canonical path"
        )
    if posixpath.normpath(candidate) != candidate:
        raise _error(
            "noncanonical-path", f"{context} must be an absolute canonical path"
        )
    return candidate


def _positive(value: object, context: str, maximum: int) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise _error("invalid-range", f"{context} must be a positive bounded integer")
    return value


def _nonnegative(value: object, context: str, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or value < 0 or value > maximum:
        raise _error(
            "invalid-range", f"{context} must be a bounded non-negative integer"
        )
    return value


def _framed_digest(parts: Iterable[str]) -> str:
    values = tuple(parts)
    digest = hashlib.sha256()
    digest.update(len(values).to_bytes(8, "big"))
    for part in values:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _argv_digest(argv: tuple[str, ...]) -> ArgvDigest:
    if type(argv) is not tuple or not argv:
        raise _error("invalid-type", "argv must be a non-empty immutable tuple")
    if len(argv) > MAX_ARGV_ITEMS:
        raise _error("too-many-items", "argv exceeds its item limit")
    values = tuple(
        _safe_text(item, f"argv[{index}]", MAX_ARGV_ELEMENT_CHARS)
        for index, item in enumerate(argv)
    )
    return ArgvDigest(_framed_digest(values))


def _same_text(left: object, right: object) -> bool:
    return type(left) is str and type(right) is str and left == right


def _same_tuple(left: object, right: object) -> bool:
    if type(left) is not tuple or type(right) is not tuple or len(left) != len(right):
        return False
    for left_item, right_item in zip(left, right):
        if type(left_item) is not type(right_item):
            return False
        if type(left_item) is str:
            if not _same_text(left_item, right_item):
                return False
        elif type(left_item) is int or type(left_item) is bool:
            if left_item != right_item:
                return False
        elif left_item is None:
            if right_item is not None:
                return False
        elif isinstance(left_item, Enum):
            if left_item.value != right_item.value:
                return False
        elif not _same_scalar(left_item, right_item):
            return False
    return True


def _same_scalar(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is str or type(left) is int or type(left) is bool:
        return left == right
    if left is None:
        return right is None
    if isinstance(left, tuple):
        return _same_tuple(left, right)
    if type(left) is VerificationProfileIdentity:
        return _same_profile_identity(left, right)
    if type(left) is VerificationExecutableIdentity:
        return _same_executable(left, right)
    if type(left) is ResultSchema:
        return _same_result_schema(left, right)
    if type(left) is VerificationSnapshot:
        return _same_snapshot(left, right)
    if type(left) is ApprovedReview:
        return _same_approved(left, right)
    if type(left) is VerificationEffectLease:
        return _same_effect(left, right)
    return (
        isinstance(left, Enum) and isinstance(right, Enum) and left.value == right.value
    )


@dataclass(frozen=True, slots=True)
class VerificationProfileIdentity:
    harness_id: str
    permission: str
    operating_system: str
    architecture: str
    probe_revision: str
    sandbox_policy_id: str

    def __post_init__(self) -> None:
        if type(self) is not VerificationProfileIdentity:
            raise _error(
                "invalid-type", "verification profile identity type is not exact"
            )
        for name in (
            "harness_id",
            "permission",
            "operating_system",
            "architecture",
            "probe_revision",
            "sandbox_policy_id",
        ):
            _safe_text(
                getattr(self, name), f"profile_identity.{name}", MAX_IDENTIFIER_CHARS
            )
        if self.permission != "read-only":
            raise _error(
                "profile-permission",
                "verification profile permission must be read-only",
            )


@dataclass(frozen=True, slots=True)
class VerificationExecutableIdentity:
    path: str
    version: str
    sha256: str

    def __post_init__(self) -> None:
        if type(self) is not VerificationExecutableIdentity:
            raise _error(
                "invalid-type", "verification executable identity type is not exact"
            )
        _canonical_path(self.path, "executable.path")
        _safe_text(self.version, "executable.version", MAX_IDENTIFIER_CHARS)
        _digest(self.sha256, "executable.sha256")


@dataclass(frozen=True, slots=True)
class ResultSchema:
    schema_id: ResultSchemaId
    version: int
    digest: ReceiptDigest

    def __post_init__(self) -> None:
        if type(self) is not ResultSchema:
            raise _error("invalid-type", "result schema type is not exact")
        _identifier(self.schema_id, "result_schema.schema_id")
        _positive(self.version, "result_schema.version", 2**31 - 1)
        _digest(self.digest, "result_schema.digest")


def _same_profile_identity(left: object, right: object) -> bool:
    if (
        type(left) is not VerificationProfileIdentity
        or type(right) is not VerificationProfileIdentity
    ):
        return False
    return all(
        _same_text(getattr(left, name), getattr(right, name))
        for name in (
            "harness_id",
            "permission",
            "operating_system",
            "architecture",
            "probe_revision",
            "sandbox_policy_id",
        )
    )


def _same_executable(left: object, right: object) -> bool:
    if (
        type(left) is not VerificationExecutableIdentity
        or type(right) is not VerificationExecutableIdentity
    ):
        return False
    return all(
        _same_text(getattr(left, name), getattr(right, name))
        for name in ("path", "version", "sha256")
    )


def _same_result_schema(left: object, right: object) -> bool:
    if type(left) is not ResultSchema or type(right) is not ResultSchema:
        return False
    return (
        _same_text(left.schema_id, right.schema_id)
        and type(left.version) is int
        and type(right.version) is int
        and left.version == right.version
        and _same_text(left.digest, right.digest)
    )


def _same_snapshot(left: object, right: object) -> bool:
    if (
        type(left) is not VerificationSnapshot
        or type(right) is not VerificationSnapshot
    ):
        return False
    return all(
        _same_scalar(getattr(left, name), getattr(right, name))
        for name in (
            "workspace",
            "canonical_path",
            "device",
            "inode",
            "claim_ref",
            "target_head",
            "allowed_tree_digest",
        )
    )


@dataclass(frozen=True, slots=True)
class VerificationProfile:
    ref: VerificationProfileRef
    profile_identity: VerificationProfileIdentity
    executable: VerificationExecutableIdentity
    argv_template: tuple[str, ...]
    argv_template_digest: ArgvDigest
    cwd_policy: Literal["canonical-workspace"]
    environment_allowlist: tuple[EnvName, ...]
    environment_values: tuple[str, ...]
    timeout_ms: int
    output_limit_bytes: int
    result_schema: ResultSchema
    profile_binding_digest: VerificationProfileBindingDigest = field(init=False)

    def __post_init__(self) -> None:
        if type(self) is not VerificationProfile:
            raise _error("invalid-type", "verification profile type is not exact")
        _identifier(self.ref, "profile.ref")
        if type(self.profile_identity) is not VerificationProfileIdentity:
            raise _error("invalid-type", "profile identity is invalid")
        VerificationProfileIdentity.__post_init__(self.profile_identity)
        if type(self.executable) is not VerificationExecutableIdentity:
            raise _error("invalid-type", "profile executable is invalid")
        VerificationExecutableIdentity.__post_init__(self.executable)
        if type(self.argv_template) is not tuple or not self.argv_template:
            raise _error(
                "invalid-type", "profile argv template must be a non-empty tuple"
            )
        values = tuple(
            _safe_text(item, f"profile.argv_template[{index}]", MAX_ARGV_ELEMENT_CHARS)
            for index, item in enumerate(self.argv_template)
        )
        if values[0] != self.executable.path:
            raise _error(
                "executable-mismatch", "profile argv must begin with executable"
            )
        if values.count(_WORKSPACE_PLACEHOLDER) > 1:
            raise _error(
                "duplicate-placeholder", "workspace placeholder may occur once"
            )
        for item in values:
            if item != _WORKSPACE_PLACEHOLDER and ("{" in item or "}" in item):
                raise _error(
                    "unknown-placeholder", "profile argv has an undeclared placeholder"
                )
        if not _same_text(_argv_digest(values), self.argv_template_digest):
            raise _error("argv-digest", "profile argv template digest does not match")
        if type(self.cwd_policy) is not str or self.cwd_policy != _CWD_POLICY:
            raise _error("cwd-policy", "profile cwd policy must be canonical-workspace")
        names = _validate_environment_names(
            self.environment_allowlist, "profile.environment_allowlist"
        )
        values_env = _validate_environment_values(
            self.environment_values, "profile.environment_values"
        )
        if len(names) != len(values_env):
            raise _error(
                "environment-mismatch",
                "profile environment names and values differ in length",
            )
        _positive(self.timeout_ms, "profile.timeout_ms", MAX_TIMEOUT_MS)
        _positive(
            self.output_limit_bytes,
            "profile.output_limit_bytes",
            MAX_OUTPUT_LIMIT_BYTES,
        )
        if type(self.result_schema) is not ResultSchema:
            raise _error("invalid-type", "profile result schema is invalid")
        ResultSchema.__post_init__(self.result_schema)
        object.__setattr__(
            self,
            "profile_binding_digest",
            VerificationProfileBindingDigest(
                _framed_digest(
                    _profile_binding_parts(
                        self.ref,
                        self.profile_identity,
                        self.executable,
                        values,
                        self.argv_template_digest,
                        self.cwd_policy,
                        names,
                        values_env,
                        self.timeout_ms,
                        self.output_limit_bytes,
                        self.result_schema,
                    )
                )
            ),
        )


def _validate_environment_names(value: object, context: str) -> tuple[EnvName, ...]:
    if type(value) is not tuple:
        raise _error("invalid-type", f"{context} must be an immutable tuple")
    if len(value) > MAX_ENV_ITEMS:
        raise _error("too-many-items", f"{context} exceeds its item limit")
    names: list[EnvName] = []
    for index, item in enumerate(value):
        name = _safe_text(item, f"{context}[{index}]", 64)
        if _ENV_NAME.fullmatch(name) is None or name not in _SAFE_ENV_NAMES:
            raise _error(
                "environment-not-allowlisted", f"{context}[{index}] is not safe"
            )
        names.append(EnvName(name))
    result = tuple(names)
    if result != tuple(sorted(result)) or len(set(result)) != len(result):
        raise _error("noncanonical-order", f"{context} must be sorted and unique")
    return result


def _validate_environment_values(value: object, context: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise _error("invalid-type", f"{context} must be an immutable tuple")
    if len(value) > MAX_ENV_ITEMS:
        raise _error("too-many-items", f"{context} exceeds its item limit")
    values = tuple(
        _safe_text(item, f"{context}[{index}]", MAX_ENV_VALUE_CHARS)
        for index, item in enumerate(value)
    )
    if any(_SECRET_VALUE.search(item) is not None for item in values):
        raise _error(
            "secret-environment-value", f"{context} contains secret-like material"
        )
    return values


def _profile_binding_parts(
    ref: VerificationProfileRef,
    identity: VerificationProfileIdentity,
    executable: VerificationExecutableIdentity,
    argv_template: tuple[str, ...],
    argv_template_digest: ArgvDigest,
    cwd_policy: str,
    environment_allowlist: tuple[EnvName, ...],
    environment_values: tuple[str, ...],
    timeout_ms: int,
    output_limit_bytes: int,
    result_schema: ResultSchema,
) -> tuple[str, ...]:
    return (
        "verification-profile-binding-v2",
        str(ref),
        identity.harness_id,
        identity.permission,
        identity.operating_system,
        identity.architecture,
        identity.probe_revision,
        identity.sandbox_policy_id,
        executable.path,
        executable.version,
        executable.sha256,
        str(argv_template_digest),
        *argv_template,
        cwd_policy,
        *(
            part
            for pair in zip(environment_allowlist, environment_values)
            for part in pair
        ),
        str(timeout_ms),
        str(output_limit_bytes),
        str(result_schema.schema_id),
        str(result_schema.version),
        str(result_schema.digest),
    )


@dataclass(frozen=True, slots=True)
class VerificationSnapshot:
    workspace: WorkspaceIdentity
    canonical_path: str
    device: int
    inode: int
    claim_ref: ClaimRef
    target_head: GitObjectId
    allowed_tree_digest: TreeDigest

    def __post_init__(self) -> None:
        if type(self) is not VerificationSnapshot:
            raise _error("invalid-type", "snapshot type is not exact")
        workspace = _canonical_path(self.workspace, "snapshot.workspace")
        canonical_path = _canonical_path(self.canonical_path, "snapshot.canonical_path")
        if workspace != canonical_path:
            raise _error("workspace-mismatch", "snapshot workspace and path differ")
        _nonnegative(self.device, "snapshot.device")
        _nonnegative(self.inode, "snapshot.inode")
        _identifier(self.claim_ref, "snapshot.claim_ref")
        _git_object_id(self.target_head, "snapshot.target_head")
        _tree_digest(self.allowed_tree_digest, "snapshot.allowed_tree_digest")


@dataclass(frozen=True, slots=True, init=False)
class ApprovedReview:
    run_id: str
    team_id: str
    workspace: str
    task_id: str
    dispatch_id: str
    attempt_id: str
    worker_node: str
    reviewer_node: str
    worker_terminal_id: str
    reviewer_terminal_id: str
    review_round: int
    target_head: str
    target_tree_digest: str
    claim_ref: str
    policy_fingerprint: str
    routing_lane: TaskLane
    approval_ref: str
    approval_sequence: int
    profile_ref: str
    verification_id: str
    routing_digest: str
    reservation_digest: str | None
    authority_digest: str
    _issuer: object = field(init=False, repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("ApprovedReview is return-only")


def _approved_parts(value: ApprovedReview) -> tuple[str, ...]:
    return (
        "verification-approved-v2",
        value.run_id,
        value.team_id,
        value.workspace,
        value.task_id,
        value.dispatch_id,
        value.attempt_id,
        value.worker_node,
        value.reviewer_node,
        value.worker_terminal_id,
        value.reviewer_terminal_id,
        str(value.review_round),
        value.target_head,
        value.target_tree_digest,
        value.claim_ref,
        value.policy_fingerprint,
        value.routing_lane.value,
        value.approval_ref,
        str(value.approval_sequence),
        value.profile_ref,
        value.verification_id,
        value.routing_digest,
        "" if value.reservation_digest is None else value.reservation_digest,
    )


def _validate_approved(value: ApprovedReview) -> None:
    if type(value) is not ApprovedReview:
        raise _error("approval-invalid", "approval type is not exact")
    if getattr(value, "_issuer", None) is not _APPROVAL_ISSUER:
        raise _error("approval-invalid", "approval was not issued by admission")
    for name in (
        "run_id",
        "team_id",
        "workspace",
        "task_id",
        "dispatch_id",
        "attempt_id",
        "worker_node",
        "reviewer_node",
        "worker_terminal_id",
        "reviewer_terminal_id",
        "claim_ref",
        "approval_ref",
        "profile_ref",
        "verification_id",
    ):
        _identifier(getattr(value, name), f"approved.{name}")
    _canonical_path(value.workspace, "approved.workspace")
    _positive(value.review_round, "approved.review_round", 2**63 - 1)
    _nonnegative(value.approval_sequence, "approved.approval_sequence")
    _git_object_id(value.target_head, "approved.target_head")
    _tree_digest(value.target_tree_digest, "approved.target_tree_digest")
    _digest(value.policy_fingerprint, "approved.policy_fingerprint")
    _digest(value.routing_digest, "approved.routing_digest")
    if value.reservation_digest is not None:
        _digest(value.reservation_digest, "approved.reservation_digest")
    if type(value.routing_lane) is not TaskLane or value.routing_lane not in {
        TaskLane.NORMAL,
        TaskLane.EXPRESS,
    }:
        raise _error("lane-not-eligible", "approved lane is not normal or express")
    if _same_text(value.worker_terminal_id, value.reviewer_terminal_id):
        raise _error(
            "independent-terminal", "Worker and Reviewer terminals must differ"
        )
    _digest(value.authority_digest, "approved.authority_digest")
    expected = ReceiptDigest(_framed_digest(_approved_parts(value)))
    if not _same_text(value.authority_digest, expected):
        raise _error("approval-invalid", "approval authority digest does not match")


def _same_approved(left: object, right: object) -> bool:
    if type(left) is not ApprovedReview or type(right) is not ApprovedReview:
        return False
    names = tuple(
        name for name in ApprovedReview.__dataclass_fields__ if not name.startswith("_")
    )
    return all(
        _same_scalar(getattr(left, name), getattr(right, name)) for name in names
    )


def _make_approved(**values: object) -> ApprovedReview:
    result = object.__new__(ApprovedReview)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    object.__setattr__(result, "_issuer", _APPROVAL_ISSUER)
    object.__setattr__(result, "authority_digest", ReceiptDigest("0" * 64))
    object.__setattr__(
        result,
        "authority_digest",
        ReceiptDigest(_framed_digest(_approved_parts(result))),
    )
    _validate_approved(result)
    return result


@dataclass(frozen=True, slots=True, init=False)
class _BoundApproval:
    approval_ref: ApprovalRef
    approved: ApprovedReview
    _issuer: object = field(init=False, repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("bound approval is return-only")


def _make_bound_approval(
    approval_ref: ApprovalRef, approved: ApprovedReview
) -> _BoundApproval:
    _validate_approved(approved)
    result = object.__new__(_BoundApproval)
    object.__setattr__(result, "approval_ref", approval_ref)
    object.__setattr__(result, "approved", approved)
    object.__setattr__(result, "_issuer", _BOUND_ISSUER)
    _validate_bound_approval(result)
    return result


def _validate_bound_approval(value: _BoundApproval) -> None:
    if type(value) is not _BoundApproval:
        raise _error("approval-invalid", "bound approval type is not exact")
    if getattr(value, "_issuer", None) is not _BOUND_ISSUER:
        raise _error("approval-invalid", "bound approval was not issued by admission")
    _identifier(value.approval_ref, "bound.approval_ref")
    _validate_approved(value.approved)
    if not _same_text(value.approval_ref, value.approved.approval_ref):
        raise _error("approval-invalid", "bound approval reference differs")


class ApprovalAdmissionPort(Protocol):
    """Trusted adapter from an opaque ref to validated #49/#50 authority."""

    def resolve(self, approval_ref: ApprovalRef) -> _BoundApproval:
        """Resolve and validate one private bound approval."""


@dataclass(frozen=True, slots=True, init=False)
class VerificationRequest:
    approval_ref: ApprovalRef
    approval: ApprovedReview
    profile_ref: VerificationProfileRef
    profile_identity: VerificationProfileIdentity
    profile_binding_digest: VerificationProfileBindingDigest
    executable: VerificationExecutableIdentity
    argv: tuple[str, ...]
    argv_digest: ArgvDigest
    cwd: WorkspaceIdentity
    environment_names: tuple[EnvName, ...]
    environment_values: tuple[str, ...]
    timeout_ms: int
    output_limit_bytes: int
    result_schema: ResultSchema
    before_snapshot: VerificationSnapshot
    routing_digest: ReceiptDigest
    reservation_digest: ReceiptDigest | None
    verification_id: VerificationId
    request_digest: ReceiptDigest
    _issuer: object = field(init=False, repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("VerificationRequest is return-only")


def _request_parts(value: VerificationRequest) -> tuple[str, ...]:
    approval = value.approval
    return (
        "verification-request-v2",
        str(value.approval_ref),
        str(value.verification_id),
        approval.run_id,
        approval.team_id,
        approval.workspace,
        approval.task_id,
        approval.dispatch_id,
        approval.attempt_id,
        approval.worker_node,
        approval.reviewer_node,
        approval.worker_terminal_id,
        approval.reviewer_terminal_id,
        str(approval.review_round),
        approval.target_head,
        approval.target_tree_digest,
        approval.claim_ref,
        approval.policy_fingerprint,
        approval.routing_lane.value,
        approval.approval_ref,
        str(approval.approval_sequence),
        approval.profile_ref,
        str(value.routing_digest),
        "" if value.reservation_digest is None else str(value.reservation_digest),
        str(value.profile_ref),
        value.profile_identity.harness_id,
        value.profile_identity.permission,
        value.profile_identity.operating_system,
        value.profile_identity.architecture,
        value.profile_identity.probe_revision,
        value.profile_identity.sandbox_policy_id,
        str(value.profile_binding_digest),
        value.executable.path,
        value.executable.version,
        value.executable.sha256,
        str(value.argv_digest),
        *value.argv,
        value.cwd,
        *(
            part
            for pair in zip(value.environment_names, value.environment_values)
            for part in pair
        ),
        str(value.timeout_ms),
        str(value.output_limit_bytes),
        str(value.result_schema.schema_id),
        str(value.result_schema.version),
        str(value.result_schema.digest),
        *_snapshot_parts(value.before_snapshot),
    )


def _snapshot_parts(value: VerificationSnapshot) -> tuple[str, ...]:
    return (
        str(value.workspace),
        value.canonical_path,
        str(value.device),
        str(value.inode),
        str(value.claim_ref),
        str(value.target_head),
        str(value.allowed_tree_digest),
    )


def _same_request(left: object, right: object) -> bool:
    if type(left) is not VerificationRequest or type(right) is not VerificationRequest:
        return False
    names = tuple(
        name
        for name in VerificationRequest.__dataclass_fields__
        if not name.startswith("_")
    )
    return all(
        _same_scalar(getattr(left, name), getattr(right, name)) for name in names
    )


def _validate_request(value: VerificationRequest, *, verify_digest: bool) -> None:
    if type(value) is not VerificationRequest:
        raise _error("request-invalid", "request type is not exact")
    if getattr(value, "_issuer", None) is not _REQUEST_ISSUER:
        raise _error("request-invalid", "request was not issued by the gate")
    try:
        _identifier(value.approval_ref, "request.approval_ref")
        _validate_approved(value.approval)
        if not _same_text(value.approval_ref, value.approval.approval_ref):
            raise _error("approval-invalid", "request approval ref differs")
        _identifier(value.profile_ref, "request.profile_ref")
        if not _same_text(value.profile_ref, value.approval.profile_ref):
            raise _error("profile-mismatch", "request profile ref differs")
        if type(value.profile_identity) is not VerificationProfileIdentity:
            raise _error("invalid-type", "request profile identity is invalid")
        VerificationProfileIdentity.__post_init__(value.profile_identity)
        _digest(value.profile_binding_digest, "request.profile_binding_digest")
        if type(value.executable) is not VerificationExecutableIdentity:
            raise _error("invalid-type", "request executable is invalid")
        VerificationExecutableIdentity.__post_init__(value.executable)
        if type(value.argv) is not tuple or not value.argv:
            raise _error("invalid-type", "request argv is invalid")
        argv = tuple(
            _safe_text(item, f"request.argv[{index}]", MAX_ARGV_ELEMENT_CHARS)
            for index, item in enumerate(value.argv)
        )
        if any("{" in item or "}" in item for item in argv):
            raise _error(
                "unresolved-placeholder", "request argv contains a placeholder"
            )
        if argv[0] != value.executable.path:
            raise _error(
                "executable-mismatch", "request argv does not start with executable"
            )
        _digest(value.argv_digest, "request.argv_digest")
        if not _same_text(value.argv_digest, _argv_digest(argv)):
            raise _error("argv-digest", "request argv digest does not match")
        _canonical_path(value.cwd, "request.cwd")
        if not _same_text(value.cwd, value.approval.workspace):
            raise _error("workspace-mismatch", "request cwd differs from approval")
        names = _validate_environment_names(
            value.environment_names, "request.environment_names"
        )
        values = _validate_environment_values(
            value.environment_values, "request.environment_values"
        )
        if len(names) != len(values):
            raise _error(
                "environment-mismatch", "request environment names and values differ"
            )
        _positive(value.timeout_ms, "request.timeout_ms", MAX_TIMEOUT_MS)
        _positive(
            value.output_limit_bytes,
            "request.output_limit_bytes",
            MAX_OUTPUT_LIMIT_BYTES,
        )
        if type(value.result_schema) is not ResultSchema:
            raise _error("invalid-type", "request result schema is invalid")
        ResultSchema.__post_init__(value.result_schema)
        if type(value.before_snapshot) is not VerificationSnapshot:
            raise _error("invalid-type", "request before snapshot is invalid")
        VerificationSnapshot.__post_init__(value.before_snapshot)
        _validate_snapshot_approval(value.before_snapshot, value.approval)
        _digest(value.routing_digest, "request.routing_digest")
        if value.reservation_digest is not None:
            _digest(value.reservation_digest, "request.reservation_digest")
        _identifier(value.verification_id, "request.verification_id")
        if verify_digest:
            _digest(value.request_digest, "request.request_digest")
            if not _same_text(value.request_digest, _compute_request_digest(value)):
                raise _error(
                    "request-digest", "request digest does not bind its fields"
                )
    except AttributeError as exc:
        raise _error("request-invalid", "request is malformed") from exc


def _compute_request_digest(value: VerificationRequest) -> ReceiptDigest:
    if type(value) is not VerificationRequest:
        raise _error("request-invalid", "request must be issued before digesting")
    _validate_request(value, verify_digest=False)
    return ReceiptDigest(_framed_digest(_request_parts(value)))


def _make_request(**values: object) -> VerificationRequest:
    result = object.__new__(VerificationRequest)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    object.__setattr__(result, "_issuer", _REQUEST_ISSUER)
    object.__setattr__(result, "request_digest", ReceiptDigest("0" * 64))
    _validate_request(result, verify_digest=False)
    object.__setattr__(result, "request_digest", _compute_request_digest(result))
    _validate_request(result, verify_digest=True)
    return result


def _validate_snapshot_approval(
    snapshot: VerificationSnapshot, approval: ApprovedReview
) -> None:
    if not _same_text(snapshot.workspace, approval.workspace):
        raise _error("workspace-mismatch", "snapshot workspace differs from approval")
    if not _same_text(snapshot.claim_ref, approval.claim_ref):
        raise _error("claim-mismatch", "snapshot claim differs from approval")
    if not _same_text(snapshot.target_head, approval.target_head):
        raise _error("target-identity", "snapshot HEAD differs from approval")
    if not _same_text(snapshot.allowed_tree_digest, approval.target_tree_digest):
        raise _error("target-identity", "snapshot tree differs from approval")


@dataclass(frozen=True, slots=True)
class VerificationRunResult:
    verification_ref: VerificationRef
    request_digest: ReceiptDigest
    profile_ref: VerificationProfileRef
    profile_identity: VerificationProfileIdentity
    profile_binding_digest: VerificationProfileBindingDigest
    executable_before: VerificationExecutableIdentity
    executable_after: VerificationExecutableIdentity | None
    effect_nonce: EffectNonce
    lease_epoch: int
    fencing_token: int
    argv_digest: ArgvDigest
    cwd: WorkspaceIdentity
    environment_names: tuple[EnvName, ...]
    result_schema: ResultSchema
    outcome: VerificationOutcome
    exit_code: int | None
    stdout_sha256: OutputDigest | None
    stderr_sha256: OutputDigest | None
    stdout_bytes: int
    stderr_bytes: int
    cleanup: CleanupStatus

    def __post_init__(self) -> None:
        if type(self) is not VerificationRunResult:
            raise _error("invalid-type", "runner result type is not exact")
        _identifier(self.verification_ref, "result.verification_ref")
        _digest(self.request_digest, "result.request_digest")
        _identifier(self.profile_ref, "result.profile_ref")
        if type(self.profile_identity) is not VerificationProfileIdentity:
            raise _error("invalid-type", "runner profile identity is invalid")
        VerificationProfileIdentity.__post_init__(self.profile_identity)
        _digest(self.profile_binding_digest, "result.profile_binding_digest")
        if type(self.executable_before) is not VerificationExecutableIdentity:
            raise _error("invalid-type", "runner executable_before is invalid")
        VerificationExecutableIdentity.__post_init__(self.executable_before)
        if self.executable_after is not None:
            if type(self.executable_after) is not VerificationExecutableIdentity:
                raise _error("invalid-type", "runner executable_after is invalid")
            VerificationExecutableIdentity.__post_init__(self.executable_after)
        _identifier(self.effect_nonce, "result.effect_nonce")
        _nonnegative(self.lease_epoch, "result.lease_epoch")
        _positive(self.fencing_token, "result.fencing_token", 2**63 - 1)
        _digest(self.argv_digest, "result.argv_digest")
        _canonical_path(self.cwd, "result.cwd")
        _validate_environment_names(self.environment_names, "result.environment_names")
        if type(self.result_schema) is not ResultSchema:
            raise _error("invalid-type", "runner result schema is invalid")
        ResultSchema.__post_init__(self.result_schema)
        if type(self.outcome) is not VerificationOutcome:
            raise _error("invalid-type", "runner outcome is invalid")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise _error("invalid-type", "runner exit code is invalid")
        for name, digest in (
            ("stdout_sha256", self.stdout_sha256),
            ("stderr_sha256", self.stderr_sha256),
        ):
            if digest is not None:
                _digest(digest, f"result.{name}")
        _nonnegative(self.stdout_bytes, "result.stdout_bytes", MAX_OUTPUT_BYTES)
        _nonnegative(self.stderr_bytes, "result.stderr_bytes", MAX_OUTPUT_BYTES)
        if self.stdout_bytes and self.stdout_sha256 is None:
            raise _error("result-contract", "stdout bytes require a digest")
        if self.stderr_bytes and self.stderr_sha256 is None:
            raise _error("result-contract", "stderr bytes require a digest")
        if type(self.cleanup) is not CleanupStatus:
            raise _error("invalid-type", "runner cleanup is invalid")
        if self.outcome is VerificationOutcome.PASSED and self.exit_code != 0:
            raise _error("result-contract", "passed result requires exit code zero")
        if self.outcome is VerificationOutcome.FAILED and (
            self.exit_code is None or self.exit_code == 0
        ):
            raise _error("result-contract", "failed result requires non-zero exit code")


@dataclass(frozen=True, slots=True, init=False)
class VerificationPrepareResult:
    verification_ref: VerificationRef
    approval_ref: ApprovalRef
    request_digest: ReceiptDigest
    approval_sequence: int
    status: PreparationStatus
    request: VerificationRequest
    _issuer: object = field(init=False, repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("VerificationPrepareResult is return-only")


@dataclass(frozen=True, slots=True, init=False)
class VerificationEffectLease:
    verification_ref: VerificationRef
    request_digest: ReceiptDigest
    effect_nonce: EffectNonce
    lease_epoch: int
    fencing_token: int
    status: EffectBeginStatus
    _issuer: object = field(init=False, repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("VerificationEffectLease is return-only")


def _same_effect(left: object, right: object) -> bool:
    if (
        type(left) is not VerificationEffectLease
        or type(right) is not VerificationEffectLease
    ):
        return False
    return all(
        _same_scalar(getattr(left, name), getattr(right, name))
        for name in (
            "verification_ref",
            "request_digest",
            "effect_nonce",
            "lease_epoch",
            "fencing_token",
            "status",
        )
    )


def _validate_effect(value: VerificationEffectLease) -> None:
    if type(value) is not VerificationEffectLease:
        raise _error("effect-invalid", "effect lease type is not exact")
    if getattr(value, "_issuer", None) is not _EFFECT_ISSUER:
        raise _error("effect-invalid", "effect lease was not issued by state authority")
    _identifier(value.verification_ref, "effect.verification_ref")
    _digest(value.request_digest, "effect.request_digest")
    _identifier(value.effect_nonce, "effect.effect_nonce")
    _nonnegative(value.lease_epoch, "effect.lease_epoch")
    _positive(value.fencing_token, "effect.fencing_token", 2**63 - 1)
    if type(value.status) is not EffectBeginStatus:
        raise _error("effect-invalid", "effect status is invalid")


@dataclass(frozen=True, slots=True, init=False)
class VerificationReceipt:
    receipt_ref: ReceiptRef
    receipt_digest: ReceiptDigest
    verification_ref: VerificationRef
    approval_ref: ApprovalRef
    request_digest: ReceiptDigest
    approval: ApprovedReview
    routing_digest: ReceiptDigest
    reservation_digest: ReceiptDigest | None
    profile_ref: VerificationProfileRef
    profile_identity: VerificationProfileIdentity
    profile_binding_digest: VerificationProfileBindingDigest
    executable_before: VerificationExecutableIdentity
    executable_after: VerificationExecutableIdentity | None
    effect_nonce: EffectNonce
    lease_epoch: int
    fencing_token: int
    argv_digest: ArgvDigest
    cwd: WorkspaceIdentity
    environment_names: tuple[EnvName, ...]
    timeout_ms: int
    output_limit_bytes: int
    result_schema: ResultSchema
    before_snapshot: VerificationSnapshot
    after_snapshot: VerificationSnapshot
    outcome: VerificationOutcome
    exit_code: int | None
    stdout_sha256: OutputDigest | None
    stderr_sha256: OutputDigest | None
    stdout_bytes: int
    stderr_bytes: int
    cleanup: CleanupStatus
    _issuer: object = field(init=False, repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("VerificationReceipt is return-only")


def _receipt_parts(value: VerificationReceipt) -> tuple[str, ...]:
    approval = value.approval
    return (
        "verification-receipt-v3",
        str(value.receipt_ref),
        str(value.verification_ref),
        str(value.approval_ref),
        str(value.request_digest),
        approval.run_id,
        approval.team_id,
        approval.workspace,
        approval.task_id,
        approval.dispatch_id,
        approval.attempt_id,
        approval.worker_node,
        approval.reviewer_node,
        approval.worker_terminal_id,
        approval.reviewer_terminal_id,
        str(approval.review_round),
        approval.target_head,
        approval.target_tree_digest,
        approval.claim_ref,
        approval.policy_fingerprint,
        approval.routing_lane.value,
        approval.approval_ref,
        str(approval.approval_sequence),
        approval.profile_ref,
        approval.verification_id,
        str(value.routing_digest),
        "" if value.reservation_digest is None else str(value.reservation_digest),
        str(value.profile_ref),
        value.profile_identity.harness_id,
        value.profile_identity.permission,
        value.profile_identity.operating_system,
        value.profile_identity.architecture,
        value.profile_identity.probe_revision,
        value.profile_identity.sandbox_policy_id,
        str(value.profile_binding_digest),
        value.executable_before.path,
        value.executable_before.version,
        value.executable_before.sha256,
        "" if value.executable_after is None else value.executable_after.path,
        "" if value.executable_after is None else value.executable_after.version,
        "" if value.executable_after is None else value.executable_after.sha256,
        str(value.effect_nonce),
        str(value.lease_epoch),
        str(value.fencing_token),
        str(value.argv_digest),
        value.cwd,
        *value.environment_names,
        str(value.timeout_ms),
        str(value.output_limit_bytes),
        str(value.result_schema.schema_id),
        str(value.result_schema.version),
        str(value.result_schema.digest),
        *_snapshot_parts(value.before_snapshot),
        *_snapshot_parts(value.after_snapshot),
        value.outcome.value,
        "" if value.exit_code is None else str(value.exit_code),
        "" if value.stdout_sha256 is None else str(value.stdout_sha256),
        "" if value.stderr_sha256 is None else str(value.stderr_sha256),
        str(value.stdout_bytes),
        str(value.stderr_bytes),
        value.cleanup.value,
    )


def _validate_receipt(value: VerificationReceipt, *, verify_digest: bool) -> None:
    if type(value) is not VerificationReceipt:
        raise _error("receipt-invalid", "receipt type is not exact")
    if getattr(value, "_issuer", None) is not _RECEIPT_ISSUER:
        raise _error("receipt-invalid", "receipt was not issued by state authority")
    try:
        _identifier(value.receipt_ref, "receipt.receipt_ref")
        _digest(value.receipt_digest, "receipt.receipt_digest")
        _identifier(value.verification_ref, "receipt.verification_ref")
        _identifier(value.approval_ref, "receipt.approval_ref")
        _digest(value.request_digest, "receipt.request_digest")
        _validate_approved(value.approval)
        _digest(value.routing_digest, "receipt.routing_digest")
        if value.reservation_digest is not None:
            _digest(value.reservation_digest, "receipt.reservation_digest")
        _identifier(value.profile_ref, "receipt.profile_ref")
        if type(value.profile_identity) is not VerificationProfileIdentity:
            raise _error("invalid-type", "receipt profile identity is invalid")
        VerificationProfileIdentity.__post_init__(value.profile_identity)
        _digest(value.profile_binding_digest, "receipt.profile_binding_digest")
        if type(value.executable_before) is not VerificationExecutableIdentity:
            raise _error("invalid-type", "receipt executable_before is invalid")
        VerificationExecutableIdentity.__post_init__(value.executable_before)
        if value.executable_after is not None:
            if type(value.executable_after) is not VerificationExecutableIdentity:
                raise _error("invalid-type", "receipt executable_after is invalid")
            VerificationExecutableIdentity.__post_init__(value.executable_after)
        _identifier(value.effect_nonce, "receipt.effect_nonce")
        _nonnegative(value.lease_epoch, "receipt.lease_epoch")
        _positive(value.fencing_token, "receipt.fencing_token", 2**63 - 1)
        _digest(value.argv_digest, "receipt.argv_digest")
        _canonical_path(value.cwd, "receipt.cwd")
        _validate_environment_names(
            value.environment_names, "receipt.environment_names"
        )
        _positive(value.timeout_ms, "receipt.timeout_ms", MAX_TIMEOUT_MS)
        _positive(
            value.output_limit_bytes,
            "receipt.output_limit_bytes",
            MAX_OUTPUT_LIMIT_BYTES,
        )
        if type(value.result_schema) is not ResultSchema:
            raise _error("invalid-type", "receipt result schema is invalid")
        ResultSchema.__post_init__(value.result_schema)
        for name, snapshot in (
            ("before_snapshot", value.before_snapshot),
            ("after_snapshot", value.after_snapshot),
        ):
            if type(snapshot) is not VerificationSnapshot:
                raise _error("invalid-type", f"receipt.{name} is invalid")
            VerificationSnapshot.__post_init__(snapshot)
        if type(value.outcome) is not VerificationOutcome:
            raise _error("invalid-type", "receipt outcome is invalid")
        if value.exit_code is not None and type(value.exit_code) is not int:
            raise _error("invalid-type", "receipt exit code is invalid")
        for name, digest in (
            ("stdout_sha256", value.stdout_sha256),
            ("stderr_sha256", value.stderr_sha256),
        ):
            if digest is not None:
                _digest(digest, f"receipt.{name}")
        _nonnegative(value.stdout_bytes, "receipt.stdout_bytes", MAX_OUTPUT_BYTES)
        _nonnegative(value.stderr_bytes, "receipt.stderr_bytes", MAX_OUTPUT_BYTES)
        if value.stdout_bytes and value.stdout_sha256 is None:
            raise _error("receipt-contract", "stdout bytes require a digest")
        if value.stderr_bytes and value.stderr_sha256 is None:
            raise _error("receipt-contract", "stderr bytes require a digest")
        if type(value.cleanup) is not CleanupStatus:
            raise _error("invalid-type", "receipt cleanup is invalid")
        if verify_digest and not _same_text(
            value.receipt_digest, _compute_receipt_digest(value)
        ):
            raise _error("receipt-digest", "receipt digest does not bind its fields")
    except AttributeError as exc:
        raise _error("receipt-invalid", "receipt is malformed") from exc


def _compute_receipt_digest(value: VerificationReceipt) -> ReceiptDigest:
    if type(value) is not VerificationReceipt:
        raise _error("receipt-invalid", "receipt must be issued before digesting")
    _validate_receipt(value, verify_digest=False)
    return ReceiptDigest(_framed_digest(_receipt_parts(value)))


def _make_receipt(
    *,
    receipt_ref: ReceiptRef,
    request: VerificationRequest,
    result: VerificationRunResult,
    effect: VerificationEffectLease,
    after_snapshot: VerificationSnapshot,
) -> VerificationReceipt:
    result_value = object.__new__(VerificationReceipt)
    values: dict[str, object] = {
        "receipt_ref": receipt_ref,
        "receipt_digest": ReceiptDigest("0" * 64),
        "verification_ref": result.verification_ref,
        "approval_ref": request.approval_ref,
        "request_digest": request.request_digest,
        "approval": request.approval,
        "routing_digest": request.routing_digest,
        "reservation_digest": request.reservation_digest,
        "profile_ref": request.profile_ref,
        "profile_identity": request.profile_identity,
        "profile_binding_digest": request.profile_binding_digest,
        "executable_before": result.executable_before,
        "executable_after": result.executable_after,
        "effect_nonce": effect.effect_nonce,
        "lease_epoch": effect.lease_epoch,
        "fencing_token": effect.fencing_token,
        "argv_digest": request.argv_digest,
        "cwd": request.cwd,
        "environment_names": request.environment_names,
        "timeout_ms": request.timeout_ms,
        "output_limit_bytes": request.output_limit_bytes,
        "result_schema": request.result_schema,
        "before_snapshot": request.before_snapshot,
        "after_snapshot": after_snapshot,
        "outcome": result.outcome,
        "exit_code": result.exit_code,
        "stdout_sha256": result.stdout_sha256,
        "stderr_sha256": result.stderr_sha256,
        "stdout_bytes": result.stdout_bytes,
        "stderr_bytes": result.stderr_bytes,
        "cleanup": result.cleanup,
        "_issuer": _RECEIPT_ISSUER,
    }
    for name, value in values.items():
        object.__setattr__(result_value, name, value)
    _validate_receipt(result_value, verify_digest=False)
    object.__setattr__(
        result_value, "receipt_digest", _compute_receipt_digest(result_value)
    )
    _validate_receipt(result_value, verify_digest=True)
    return result_value


@dataclass(frozen=True, slots=True, init=False)
class VerificationDurableRecord:
    verification_ref: VerificationRef
    approval_ref: ApprovalRef
    request: VerificationRequest
    status: DurableRecordStatus
    effect: VerificationEffectLease | None
    receipt: VerificationReceipt | None
    receipt_ref: ReceiptRef | None
    receipt_digest: ReceiptDigest | None
    _issuer: object = field(init=False, repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("VerificationDurableRecord is return-only")


def _validate_record(value: VerificationDurableRecord) -> None:
    if type(value) is not VerificationDurableRecord:
        raise _error("state-invalid", "durable record type is not exact")
    if getattr(value, "_issuer", None) is not _RECORD_ISSUER:
        raise _error(
            "state-invalid", "durable record was not issued by state authority"
        )
    _identifier(value.verification_ref, "record.verification_ref")
    _identifier(value.approval_ref, "record.approval_ref")
    _validate_request(value.request, verify_digest=True)
    if not _same_text(value.verification_ref, value.request.verification_id):
        raise _error(
            "identity-mismatch", "durable verification ref differs from request"
        )
    if not _same_text(value.approval_ref, value.request.approval_ref):
        raise _error("identity-mismatch", "durable approval ref differs from request")
    if type(value.status) is not DurableRecordStatus:
        raise _error("state-invalid", "durable status is invalid")
    if value.effect is not None:
        _validate_effect(value.effect)
        if not _same_text(value.effect.verification_ref, value.verification_ref):
            raise _error("identity-mismatch", "durable effect ref differs")
        if not _same_text(value.effect.request_digest, value.request.request_digest):
            raise _error("identity-mismatch", "durable effect request differs")
    if value.receipt is not None:
        _validate_receipt(value.receipt, verify_digest=True)
        if not _same_text(value.receipt.verification_ref, value.verification_ref):
            raise _error("identity-mismatch", "durable receipt ref differs")
        if not _same_text(value.receipt.request_digest, value.request.request_digest):
            raise _error("identity-mismatch", "durable receipt request differs")
        if not _same_text(value.receipt_ref, value.receipt.receipt_ref):
            raise _error("identity-mismatch", "durable receipt ref binding differs")
        if not _same_text(value.receipt_digest, value.receipt.receipt_digest):
            raise _error("identity-mismatch", "durable receipt digest binding differs")
    elif value.receipt_ref is not None or value.receipt_digest is not None:
        raise _error("state-invalid", "durable receipt binding has no receipt")
    if value.status is DurableRecordStatus.PREPARED and value.receipt is not None:
        raise _error("state-invalid", "prepared record cannot carry receipt")
    if (
        value.status in {DurableRecordStatus.RECEIPTED, DurableRecordStatus.TERMINAL}
        and value.receipt is None
    ):
        raise _error("state-invalid", "receipted/terminal record requires receipt")
    if value.status is DurableRecordStatus.RECEIPTED and (
        value.effect is None
        or value.effect.status
        not in {EffectBeginStatus.RECEIPTED, EffectBeginStatus.TERMINAL}
    ):
        raise _error("state-invalid", "receipted record requires receipted effect")
    if value.status is DurableRecordStatus.TERMINAL and (
        value.effect is None or value.effect.status is not EffectBeginStatus.TERMINAL
    ):
        raise _error("state-invalid", "terminal record requires terminal effect")


class VerificationStatePort(Protocol):
    """Mandatory durable #31/#33 state/effect authority."""

    def prepare_once(self, request: VerificationRequest) -> VerificationPrepareResult:
        """CAS approved -> verifying and return a store-issued ref."""

    def begin_effect_once(
        self, verification_ref: VerificationRef, request_digest: ReceiptDigest
    ) -> VerificationEffectLease:
        """Fence duplicate/concurrent effects before runner invocation."""

    def read(self, verification_ref: VerificationRef) -> VerificationDurableRecord:
        """Read the durable record after restart."""

    def status(self, verification_ref: VerificationRef) -> DurableRecordStatus:
        """Return the durable phase/status for cross-checking read."""

    def record_receipt_once(
        self,
        verification_ref: VerificationRef,
        effect: VerificationEffectLease,
        result: VerificationRunResult,
        before: VerificationSnapshot,
        after: VerificationSnapshot,
    ) -> VerificationReceipt:
        """Record one normalized receipt with effect fencing."""

    def apply_terminal_once(
        self,
        verification_ref: VerificationRef,
        receipt_ref: ReceiptRef,
        receipt_digest: ReceiptDigest,
    ) -> VerificationTerminalResult:
        """CAS receipt evidence to terminal task state exactly once."""


@dataclass(frozen=True, slots=True, init=False)
class VerificationTerminalResult:
    verification_ref: VerificationRef
    receipt_ref: ReceiptRef
    receipt_digest: ReceiptDigest
    phase: TaskPhase
    _issuer: object = field(init=False, repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("VerificationTerminalResult is return-only")


def _validate_terminal(value: VerificationTerminalResult) -> None:
    if type(value) is not VerificationTerminalResult:
        raise _error("terminal-invalid", "terminal result type is not exact")
    if getattr(value, "_issuer", None) is not _TERMINAL_ISSUER:
        raise _error(
            "terminal-invalid", "terminal result was not issued by state authority"
        )
    _identifier(value.verification_ref, "terminal.verification_ref")
    _identifier(value.receipt_ref, "terminal.receipt_ref")
    _digest(value.receipt_digest, "terminal.receipt_digest")
    if type(value.phase) is not TaskPhase or value.phase not in {
        TaskPhase.COMPLETED,
        TaskPhase.VERIFICATION_FAILED,
    }:
        raise _error("terminal-invalid", "terminal phase is invalid")


@dataclass(frozen=True, slots=True, init=False)
class VerificationEvidence:
    receipt: VerificationReceipt
    _issuer: object = field(init=False, repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("VerificationEvidence is return-only")


@dataclass(frozen=True, slots=True, init=False)
class VerificationHandle:
    verification_ref: VerificationRef
    approval_ref: ApprovalRef
    request_digest: ReceiptDigest
    _issuer: object = field(init=False, repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("VerificationHandle is return-only")

    @property
    def verification_id(self) -> VerificationRef:
        _validate_handle(self)
        return self.verification_ref


def _validate_handle(value: VerificationHandle) -> None:
    if type(value) is not VerificationHandle:
        raise _error("handle-invalid", "handle type is not exact")
    if getattr(value, "_issuer", None) is not _HANDLE_ISSUER:
        raise _error("handle-invalid", "handle was not issued by verification gate")
    _identifier(value.verification_ref, "handle.verification_ref")
    _identifier(value.approval_ref, "handle.approval_ref")
    _digest(value.request_digest, "handle.request_digest")


def _make_handle(
    verification_ref: VerificationRef,
    approval_ref: ApprovalRef,
    request_digest: ReceiptDigest,
) -> VerificationHandle:
    result = object.__new__(VerificationHandle)
    for name, value in {
        "verification_ref": verification_ref,
        "approval_ref": approval_ref,
        "request_digest": request_digest,
        "_issuer": _HANDLE_ISSUER,
    }.items():
        object.__setattr__(result, name, value)
    _validate_handle(result)
    return result


def _make_evidence(receipt: VerificationReceipt) -> VerificationEvidence:
    _validate_receipt(receipt, verify_digest=True)
    result = object.__new__(VerificationEvidence)
    object.__setattr__(result, "receipt", receipt)
    object.__setattr__(result, "_issuer", _EVIDENCE_ISSUER)
    return result


def _make_prepare(
    verification_ref: VerificationRef,
    approval_ref: ApprovalRef,
    request: VerificationRequest,
    status: PreparationStatus,
) -> VerificationPrepareResult:
    result = object.__new__(VerificationPrepareResult)
    for name, value in {
        "verification_ref": verification_ref,
        "approval_ref": approval_ref,
        "request_digest": request.request_digest,
        "approval_sequence": request.approval.approval_sequence,
        "status": status,
        "request": request,
        "_issuer": _PREPARE_ISSUER,
    }.items():
        object.__setattr__(result, name, value)
    return result


def _make_effect(
    verification_ref: VerificationRef,
    request_digest: ReceiptDigest,
    effect_nonce: EffectNonce,
    lease_epoch: int,
    fencing_token: int,
    status: EffectBeginStatus,
) -> VerificationEffectLease:
    result = object.__new__(VerificationEffectLease)
    for name, value in {
        "verification_ref": verification_ref,
        "request_digest": request_digest,
        "effect_nonce": effect_nonce,
        "lease_epoch": lease_epoch,
        "fencing_token": fencing_token,
        "status": status,
        "_issuer": _EFFECT_ISSUER,
    }.items():
        object.__setattr__(result, name, value)
    _validate_effect(result)
    return result


def _make_terminal(
    verification_ref: VerificationRef,
    receipt_ref: ReceiptRef,
    receipt_digest: ReceiptDigest,
    phase: TaskPhase,
) -> VerificationTerminalResult:
    result = object.__new__(VerificationTerminalResult)
    for name, value in {
        "verification_ref": verification_ref,
        "receipt_ref": receipt_ref,
        "receipt_digest": receipt_digest,
        "phase": phase,
        "_issuer": _TERMINAL_ISSUER,
    }.items():
        object.__setattr__(result, name, value)
    _validate_terminal(result)
    return result


def _make_record(
    verification_ref: VerificationRef,
    approval_ref: ApprovalRef,
    request: VerificationRequest,
    status: DurableRecordStatus,
    effect: VerificationEffectLease | None,
    receipt: VerificationReceipt | None,
) -> VerificationDurableRecord:
    result = object.__new__(VerificationDurableRecord)
    for name, value in {
        "verification_ref": verification_ref,
        "approval_ref": approval_ref,
        "request": request,
        "status": status,
        "effect": effect,
        "receipt": receipt,
        "receipt_ref": None if receipt is None else receipt.receipt_ref,
        "receipt_digest": None if receipt is None else receipt.receipt_digest,
        "_issuer": _RECORD_ISSUER,
    }.items():
        object.__setattr__(result, name, value)
    _validate_record(result)
    return result


def _validate_result_for_request(
    result: VerificationRunResult,
    request: VerificationRequest,
    effect: VerificationEffectLease,
) -> None:
    if type(result) is not VerificationRunResult:
        raise RecoveryRequired("runner-response-loss")
    try:
        VerificationRunResult.__post_init__(result)
    except Exception as exc:
        raise RecoveryRequired("runner-response-invalid") from exc
    _validate_effect(effect)
    if not _same_text(result.verification_ref, request.verification_id):
        raise _error("identity-mismatch", "runner verification ref differs")
    if not _same_text(result.request_digest, request.request_digest):
        raise _error("request-digest", "runner request digest differs")
    if not _same_text(result.profile_ref, request.profile_ref):
        raise _error("profile-mismatch", "runner profile ref differs")
    if not _same_profile_identity(result.profile_identity, request.profile_identity):
        raise _error("profile-mismatch", "runner profile identity differs")
    if not _same_text(result.profile_binding_digest, request.profile_binding_digest):
        raise _error("profile-binding-digest", "runner profile binding differs")
    if not _same_executable(result.executable_before, request.executable):
        raise _error("executable-mismatch", "runner executable_before differs")
    if not _same_text(result.effect_nonce, effect.effect_nonce):
        raise _error("effect-mismatch", "runner effect nonce differs")
    if (
        result.lease_epoch != effect.lease_epoch
        or result.fencing_token != effect.fencing_token
    ):
        raise _error("effect-mismatch", "runner effect fencing differs")
    if not _same_text(result.argv_digest, request.argv_digest):
        raise _error("argv-digest", "runner argv digest differs")
    if not _same_text(result.cwd, request.cwd):
        raise _error("workspace-mismatch", "runner cwd differs")
    if not _same_tuple(result.environment_names, request.environment_names):
        raise _error("environment-mismatch", "runner environment names differ")
    if not _same_result_schema(result.result_schema, request.result_schema):
        raise _error("schema-mismatch", "runner result schema differs")
    if result.outcome is VerificationOutcome.UNKNOWN_EFFECT:
        raise RecoveryRequired("unknown-effect")
    if result.outcome is VerificationOutcome.RUNNER_UNAVAILABLE:
        if (
            result.executable_after is not None
            or result.cleanup is not CleanupStatus.NOT_STARTED
        ):
            raise RecoveryRequired("runner-unavailable-contract")
        if (
            result.exit_code is not None
            or result.stdout_bytes
            or result.stderr_bytes
            or result.stdout_sha256 is not None
            or result.stderr_sha256 is not None
        ):
            raise _error(
                "result-contract", "runner unavailable must prove no process/output"
            )
        return
    if result.executable_after is None or not _same_executable(
        result.executable_after, request.executable
    ):
        raise RecoveryRequired("executable-identity-after-run-unavailable")
    if (
        result.outcome
        in {
            VerificationOutcome.PASSED,
            VerificationOutcome.FAILED,
            VerificationOutcome.TIMEOUT,
            VerificationOutcome.OUTPUT_LIMIT,
            VerificationOutcome.SCHEMA_INVALID,
        }
        and result.cleanup is not CleanupStatus.REAPED
    ):
        raise RecoveryRequired("cleanup-unknown")
    if result.outcome is VerificationOutcome.PASSED:
        if result.exit_code != 0:
            raise _error("result-contract", "passed result requires exit code zero")
        if result.stdout_bytes + result.stderr_bytes > request.output_limit_bytes:
            raise _error("output-limit", "passed result exceeds output limit")
    elif result.outcome is VerificationOutcome.FAILED and (
        result.exit_code is None or result.exit_code == 0
    ):
        raise _error("result-contract", "failed result requires non-zero exit code")


def _validate_receipt_for_result(
    receipt: VerificationReceipt,
    request: VerificationRequest,
    result: VerificationRunResult,
    effect: VerificationEffectLease,
    after: VerificationSnapshot,
) -> None:
    if type(receipt) is not VerificationReceipt:
        raise RecoveryRequired("receipt-response-loss")
    try:
        _validate_receipt(receipt, verify_digest=True)
    except RecoveryRequired:
        raise
    except Exception as exc:
        raise RecoveryRequired("receipt-response-invalid") from exc
    if not _same_snapshot(after, request.before_snapshot):
        raise RecoveryRequired("snapshot-drift")
    _validate_snapshot_approval(after, request.approval)
    fields: tuple[tuple[object, object], ...] = (
        (receipt.verification_ref, result.verification_ref),
        (receipt.approval_ref, request.approval_ref),
        (receipt.request_digest, request.request_digest),
        (receipt.profile_ref, request.profile_ref),
        (receipt.profile_identity, request.profile_identity),
        (receipt.profile_binding_digest, request.profile_binding_digest),
        (receipt.executable_before, result.executable_before),
        (receipt.executable_after, result.executable_after),
        (receipt.effect_nonce, effect.effect_nonce),
        (receipt.lease_epoch, effect.lease_epoch),
        (receipt.fencing_token, effect.fencing_token),
        (receipt.routing_digest, request.routing_digest),
        (receipt.reservation_digest, request.reservation_digest),
        (receipt.argv_digest, request.argv_digest),
        (receipt.cwd, request.cwd),
        (receipt.environment_names, request.environment_names),
        (receipt.timeout_ms, request.timeout_ms),
        (receipt.output_limit_bytes, request.output_limit_bytes),
        (receipt.result_schema, request.result_schema),
        (receipt.before_snapshot, request.before_snapshot),
        (receipt.after_snapshot, after),
        (receipt.outcome, result.outcome),
        (receipt.exit_code, result.exit_code),
        (receipt.stdout_sha256, result.stdout_sha256),
        (receipt.stderr_sha256, result.stderr_sha256),
        (receipt.stdout_bytes, result.stdout_bytes),
        (receipt.stderr_bytes, result.stderr_bytes),
        (receipt.cleanup, result.cleanup),
    )
    if any(not _same_scalar(observed, expected) for observed, expected in fields):
        raise _error("receipt-mismatch", "receipt does not match request/result/effect")
    if not _same_approved(receipt.approval, request.approval):
        raise _error("receipt-mismatch", "receipt approval differs")


def _capture_snapshot(
    snapshot_port: WorkspaceSnapshotPort,
    approval: ApprovedReview,
) -> VerificationSnapshot:
    try:
        snapshot = snapshot_port.capture(
            WorkspaceIdentity(approval.workspace), ClaimRef(approval.claim_ref)
        )
    except Exception as exc:
        raise RecoveryRequired("snapshot-response-loss") from exc
    if type(snapshot) is not VerificationSnapshot:
        raise RecoveryRequired("snapshot-response-invalid")
    try:
        VerificationSnapshot.__post_init__(snapshot)
    except Exception as exc:
        raise RecoveryRequired("snapshot-response-invalid") from exc
    return snapshot


def _resolve_bound(
    admission: ApprovalAdmissionPort,
    approval_ref: ApprovalRef,
) -> _BoundApproval:
    if not callable(getattr(admission, "resolve", None)):
        raise RecoveryRequired("approval-response-loss")
    try:
        bound = admission.resolve(approval_ref)
        if type(bound) is not _BoundApproval:
            raise _error(
                "approval-response-invalid",
                "admission returned non-exact bound approval",
            )
        _validate_bound_approval(bound)
    except RecoveryRequired:
        raise
    except Exception as exc:
        raise RecoveryRequired("approval-response-invalid") from exc
    if not _same_text(bound.approval_ref, approval_ref):
        raise RecoveryRequired("approval-ref-mismatch")
    return bound


def _resolve_profile(
    resolver: VerificationProfileResolver,
    profile_ref: VerificationProfileRef,
) -> VerificationProfile:
    if not callable(getattr(resolver, "resolve", None)):
        raise RecoveryRequired("profile-response-loss")
    try:
        profile = resolver.resolve(profile_ref)
        if type(profile) is not VerificationProfile:
            raise _error(
                "profile-response-invalid", "resolver returned non-exact profile"
            )
        VerificationProfile.__post_init__(profile)
    except RecoveryRequired:
        raise
    except Exception as exc:
        raise RecoveryRequired("profile-response-invalid") from exc
    if not _same_text(profile.ref, profile_ref):
        raise RecoveryRequired("profile-drift")
    return profile


def _build_request(
    bound: _BoundApproval,
    profile: VerificationProfile,
    before: VerificationSnapshot,
) -> VerificationRequest:
    _validate_bound_approval(bound)
    VerificationProfile.__post_init__(profile)
    VerificationSnapshot.__post_init__(before)
    _validate_snapshot_approval(before, bound.approved)
    argv = tuple(
        before.canonical_path if item == _WORKSPACE_PLACEHOLDER else item
        for item in profile.argv_template
    )
    return _make_request(
        approval_ref=bound.approval_ref,
        approval=bound.approved,
        profile_ref=profile.ref,
        profile_identity=profile.profile_identity,
        profile_binding_digest=profile.profile_binding_digest,
        executable=profile.executable,
        argv=argv,
        argv_digest=_argv_digest(argv),
        cwd=WorkspaceIdentity(before.canonical_path),
        environment_names=profile.environment_allowlist,
        environment_values=profile.environment_values,
        timeout_ms=profile.timeout_ms,
        output_limit_bytes=profile.output_limit_bytes,
        result_schema=profile.result_schema,
        before_snapshot=before,
        routing_digest=bound.approved.routing_digest,
        reservation_digest=bound.approved.reservation_digest,
        verification_id=VerificationId(bound.approved.verification_id),
    )


def _state_methods_present(state_port: VerificationStatePort) -> None:
    for name in (
        "prepare_once",
        "begin_effect_once",
        "read",
        "status",
        "record_receipt_once",
        "apply_terminal_once",
    ):
        if not callable(getattr(state_port, name, None)):
            raise _error("state-port-invalid", f"state port lacks {name}")


class VerificationGate:
    """Public start/resume seam backed by mandatory trusted ports."""

    __slots__ = (
        "_admission",
        "_profiles",
        "_runner",
        "_snapshots",
        "_state",
    )

    def __init__(
        self,
        admission: ApprovalAdmissionPort,
        profiles: VerificationProfileResolver,
        snapshots: WorkspaceSnapshotPort,
        runner: VerificationRunnerPort,
        state: VerificationStatePort,
    ) -> None:
        self._admission = admission
        self._profiles = profiles
        self._snapshots = snapshots
        self._runner = runner
        self._state = state
        _state_methods_present(state)
        for port, method in (
            (admission, "resolve"),
            (profiles, "resolve"),
            (snapshots, "capture"),
            (runner, "run"),
        ):
            if not callable(getattr(port, method, None)):
                raise _error("port-invalid", f"port lacks {method}")

    def start(self, approval_ref: ApprovalRef) -> VerificationHandle:
        """Resolve opaque approval, prepare durable state, and return a handle."""

        if type(approval_ref) is not str:
            raise _error("invalid-type", "approval_ref must be an exact string")
        bound = _resolve_bound(self._admission, approval_ref)
        profile = _resolve_profile(
            self._profiles, VerificationProfileRef(bound.approved.profile_ref)
        )
        before = _capture_snapshot(self._snapshots, bound.approved)
        request = _build_request(bound, profile, before)
        try:
            prepared = self._state.prepare_once(request)
        except Exception as exc:
            raise RecoveryRequired("prepare-response-loss") from exc
        if type(prepared) is not VerificationPrepareResult:
            raise RecoveryRequired("prepare-response-invalid")
        if getattr(prepared, "_issuer", None) is not _PREPARE_ISSUER:
            raise RecoveryRequired("prepare-response-invalid")
        try:
            _validate_request(prepared.request, verify_digest=True)
            _identifier(prepared.verification_ref, "prepare.verification_ref")
            _identifier(prepared.approval_ref, "prepare.approval_ref")
            _digest(prepared.request_digest, "prepare.request_digest")
            _positive(
                prepared.approval_sequence, "prepare.approval_sequence", 2**63 - 1
            )
            if type(prepared.status) is not PreparationStatus:
                raise _error("prepare-response-invalid", "prepare status is invalid")
        except Exception as exc:
            raise RecoveryRequired("prepare-response-invalid") from exc
        if prepared.status is PreparationStatus.UNKNOWN:
            raise RecoveryRequired("prepare-unknown")
        if not _same_text(prepared.approval_ref, approval_ref):
            raise RecoveryRequired("prepare-approval-mismatch")
        if not _same_text(prepared.request_digest, request.request_digest):
            raise RecoveryRequired("prepare-request-mismatch")
        if not _same_text(prepared.request.approval_ref, approval_ref):
            raise RecoveryRequired("prepare-approval-mismatch")
        if not _same_text(prepared.verification_ref, request.verification_id):
            raise RecoveryRequired("prepare-verification-mismatch")
        if prepared.approval_sequence != bound.approved.approval_sequence:
            raise RecoveryRequired("prepare-sequence-mismatch")
        if not _same_request(prepared.request, request):
            raise RecoveryRequired("prepare-request-mismatch")
        return _make_handle(
            VerificationRef(prepared.verification_ref),
            ApprovalRef(prepared.approval_ref),
            ReceiptDigest(prepared.request_digest),
        )

    def resume(self, handle: VerificationHandle) -> VerificationTerminalResult:
        """Reconstruct durable state and perform/replay one fenced effect."""

        _validate_handle(handle)
        try:
            record = self._state.read(handle.verification_ref)
            observed_status = self._state.status(handle.verification_ref)
        except Exception as exc:
            raise RecoveryRequired("state-read-response-loss") from exc
        if type(record) is not VerificationDurableRecord:
            raise RecoveryRequired("state-read-response-invalid")
        try:
            _validate_record(record)
        except Exception as exc:
            raise RecoveryRequired("state-read-response-invalid") from exc
        if (
            type(observed_status) is not DurableRecordStatus
            or observed_status is not record.status
        ):
            raise RecoveryRequired("state-status-mismatch")
        if not _same_text(record.verification_ref, handle.verification_ref):
            raise RecoveryRequired("handle-state-mismatch")
        if not _same_text(record.approval_ref, handle.approval_ref):
            raise RecoveryRequired("handle-state-mismatch")
        if not _same_text(record.request.request_digest, handle.request_digest):
            raise RecoveryRequired("handle-state-mismatch")
        bound = _resolve_bound(self._admission, record.approval_ref)
        if not _same_approved(bound.approved, record.request.approval):
            raise RecoveryRequired("approval-drift")
        if record.status is DurableRecordStatus.TERMINAL:
            return self._replay_record(record, bound)
        if record.status is DurableRecordStatus.RECEIPTED:
            if record.effect is None or record.receipt is None:
                raise RecoveryRequired("state-record-invalid")
            return self._apply_receipted(record, bound, record.effect, record.receipt)
        if record.status is not DurableRecordStatus.PREPARED:
            raise RecoveryRequired("state-unknown")
        try:
            effect = self._state.begin_effect_once(
                record.verification_ref, record.request.request_digest
            )
        except Exception as exc:
            raise RecoveryRequired("effect-response-loss") from exc
        if type(effect) is not VerificationEffectLease:
            raise RecoveryRequired("effect-response-invalid")
        try:
            _validate_effect(effect)
        except Exception as exc:
            raise RecoveryRequired("effect-response-invalid") from exc
        if not _same_text(effect.verification_ref, record.verification_ref):
            raise RecoveryRequired("effect-identity-mismatch")
        if not _same_text(effect.request_digest, record.request.request_digest):
            raise RecoveryRequired("effect-identity-mismatch")
        if effect.status is EffectBeginStatus.UNKNOWN:
            raise RecoveryRequired("unknown-effect")
        if effect.status is EffectBeginStatus.TERMINAL:
            refreshed = self._read_again(record.verification_ref)
            if refreshed.status is DurableRecordStatus.TERMINAL:
                return self._replay_record(refreshed, bound)
            if refreshed.status is DurableRecordStatus.RECEIPTED:
                if refreshed.receipt is None or refreshed.effect is None:
                    raise RecoveryRequired("receipt-missing")
                return self._apply_receipted(
                    refreshed, bound, refreshed.effect, refreshed.receipt
                )
            raise RecoveryRequired("terminal-status-mismatch")
        if effect.status is EffectBeginStatus.RECEIPTED:
            refreshed = self._read_again(record.verification_ref)
            if refreshed.receipt is None or refreshed.effect is None:
                raise RecoveryRequired("receipt-missing")
            return self._apply_receipted(
                refreshed, bound, refreshed.effect, refreshed.receipt
            )
        if effect.status is not EffectBeginStatus.RUN_ONCE:
            raise RecoveryRequired("effect-unknown")
        return self._run_once(record, bound, effect)

    def _read_again(
        self, verification_ref: VerificationRef
    ) -> VerificationDurableRecord:
        try:
            record = self._state.read(verification_ref)
        except Exception as exc:
            raise RecoveryRequired("state-read-response-loss") from exc
        if type(record) is not VerificationDurableRecord:
            raise RecoveryRequired("state-read-response-invalid")
        try:
            _validate_record(record)
        except Exception as exc:
            raise RecoveryRequired("state-read-response-invalid") from exc
        return record

    def _run_once(
        self,
        record: VerificationDurableRecord,
        bound: _BoundApproval,
        effect: VerificationEffectLease,
    ) -> VerificationTerminalResult:
        fresh_bound = _resolve_bound(self._admission, record.approval_ref)
        if not _same_approved(fresh_bound.approved, bound.approved):
            raise RecoveryRequired("approval-drift-before-run")
        bound = fresh_bound
        profile = _resolve_profile(
            self._profiles, VerificationProfileRef(bound.approved.profile_ref)
        )
        before = _capture_snapshot(self._snapshots, bound.approved)
        if not _same_snapshot(before, record.request.before_snapshot):
            raise RecoveryRequired("snapshot-drift-before-run")
        canonical = _build_request(bound, profile, before)
        if not _same_request(canonical, record.request):
            raise RecoveryRequired("request-profile-drift")
        profile = _resolve_profile(
            self._profiles, VerificationProfileRef(bound.approved.profile_ref)
        )
        canonical = _build_request(bound, profile, before)
        if not _same_request(canonical, record.request):
            raise RecoveryRequired("profile-drift-before-run")
        try:
            result = self._runner.run(canonical, effect)
        except Exception as exc:
            raise RecoveryRequired("runner-response-loss") from exc
        try:
            _validate_result_for_request(result, canonical, effect)
        except RecoveryRequired:
            raise
        except Exception as exc:
            raise RecoveryRequired("runner-response-invalid") from exc
        after = _capture_snapshot(self._snapshots, bound.approved)
        try:
            _validate_snapshot_approval(after, bound.approved)
        except Exception as exc:
            raise RecoveryRequired("snapshot-drift") from exc
        if not _same_snapshot(after, canonical.before_snapshot):
            raise RecoveryRequired("snapshot-drift")
        try:
            receipt = self._state.record_receipt_once(
                record.verification_ref,
                effect,
                result,
                canonical.before_snapshot,
                after,
            )
        except Exception as exc:
            raise RecoveryRequired("receipt-response-loss") from exc
        if type(receipt) is not VerificationReceipt:
            raise RecoveryRequired("receipt-response-invalid")
        try:
            _validate_receipt_for_result(receipt, canonical, result, effect, after)
        except RecoveryRequired:
            raise
        except Exception as exc:
            raise RecoveryRequired("receipt-response-invalid") from exc
        profile_after = _resolve_profile(
            self._profiles, VerificationProfileRef(bound.approved.profile_ref)
        )
        if not _same_request(
            _build_request(bound, profile_after, canonical.before_snapshot), canonical
        ):
            raise RecoveryRequired("profile-drift-after-receipt")
        current_after = _capture_snapshot(self._snapshots, bound.approved)
        if not _same_snapshot(current_after, receipt.after_snapshot):
            raise RecoveryRequired("snapshot-drift-after-receipt")
        if not _same_snapshot(current_after, canonical.before_snapshot):
            raise RecoveryRequired("snapshot-drift-after-receipt")
        return self._apply_terminal(record.verification_ref, receipt)

    def _apply_receipted(
        self,
        record: VerificationDurableRecord,
        bound: _BoundApproval,
        effect: VerificationEffectLease,
        receipt: VerificationReceipt,
    ) -> VerificationTerminalResult:
        fresh_bound = _resolve_bound(self._admission, record.approval_ref)
        if not _same_approved(fresh_bound.approved, bound.approved):
            raise RecoveryRequired("approval-drift-before-terminal")
        bound = fresh_bound
        profile = _resolve_profile(
            self._profiles, VerificationProfileRef(bound.approved.profile_ref)
        )
        request = _build_request(bound, profile, record.request.before_snapshot)
        if not _same_request(request, record.request):
            raise RecoveryRequired("request-profile-drift")
        current = _capture_snapshot(self._snapshots, bound.approved)
        try:
            _validate_receipt_for_result(
                receipt,
                request,
                _result_from_receipt(receipt),
                effect,
                current,
            )
        except RecoveryRequired:
            raise
        except Exception as exc:
            raise RecoveryRequired("receipt-replay-invalid") from exc
        profile_after = _resolve_profile(
            self._profiles, VerificationProfileRef(bound.approved.profile_ref)
        )
        if not _same_request(
            _build_request(bound, profile_after, record.request.before_snapshot),
            request,
        ):
            raise RecoveryRequired("profile-drift-after-receipt")
        return self._apply_terminal(record.verification_ref, receipt)

    def _replay_record(
        self,
        record: VerificationDurableRecord,
        bound: _BoundApproval,
    ) -> VerificationTerminalResult:
        if record.effect is None or record.receipt is None:
            raise RecoveryRequired("terminal-receipt-missing")
        fresh_bound = _resolve_bound(self._admission, record.approval_ref)
        if not _same_approved(fresh_bound.approved, bound.approved):
            raise RecoveryRequired("approval-drift-before-replay")
        bound = fresh_bound
        profile = _resolve_profile(
            self._profiles, VerificationProfileRef(bound.approved.profile_ref)
        )
        request = _build_request(bound, profile, record.request.before_snapshot)
        if not _same_request(request, record.request):
            raise RecoveryRequired("request-profile-drift")
        current = _capture_snapshot(self._snapshots, bound.approved)
        try:
            result = _result_from_receipt(record.receipt)
            _validate_receipt_for_result(
                record.receipt, request, result, record.effect, current
            )
        except RecoveryRequired:
            raise
        except Exception as exc:
            raise RecoveryRequired("terminal-replay-invalid") from exc
        return _make_terminal(
            record.verification_ref,
            record.receipt.receipt_ref,
            record.receipt.receipt_digest,
            TaskPhase.COMPLETED
            if record.receipt.outcome is VerificationOutcome.PASSED
            else TaskPhase.VERIFICATION_FAILED,
        )

    def _apply_terminal(
        self,
        verification_ref: VerificationRef,
        receipt: VerificationReceipt,
    ) -> VerificationTerminalResult:
        try:
            terminal = self._state.apply_terminal_once(
                verification_ref, receipt.receipt_ref, receipt.receipt_digest
            )
        except Exception as exc:
            raise RecoveryRequired("terminal-response-loss") from exc
        if type(terminal) is not VerificationTerminalResult:
            raise RecoveryRequired("terminal-response-invalid")
        try:
            _validate_terminal(terminal)
        except Exception as exc:
            raise RecoveryRequired("terminal-response-invalid") from exc
        if not _same_text(terminal.verification_ref, verification_ref):
            raise RecoveryRequired("terminal-identity-mismatch")
        if not _same_text(terminal.receipt_ref, receipt.receipt_ref):
            raise RecoveryRequired("terminal-receipt-mismatch")
        if not _same_text(terminal.receipt_digest, receipt.receipt_digest):
            raise RecoveryRequired("terminal-receipt-mismatch")
        expected_phase = (
            TaskPhase.COMPLETED
            if receipt.outcome is VerificationOutcome.PASSED
            else TaskPhase.VERIFICATION_FAILED
        )
        if terminal.phase is not expected_phase:
            raise RecoveryRequired("terminal-phase-mismatch")
        return terminal


def _result_from_receipt(receipt: VerificationReceipt) -> VerificationRunResult:
    return VerificationRunResult(
        verification_ref=receipt.verification_ref,
        request_digest=receipt.request_digest,
        profile_ref=receipt.profile_ref,
        profile_identity=receipt.profile_identity,
        profile_binding_digest=receipt.profile_binding_digest,
        executable_before=receipt.executable_before,
        executable_after=receipt.executable_after,
        effect_nonce=receipt.effect_nonce,
        lease_epoch=receipt.lease_epoch,
        fencing_token=receipt.fencing_token,
        argv_digest=receipt.argv_digest,
        cwd=receipt.cwd,
        environment_names=receipt.environment_names,
        result_schema=receipt.result_schema,
        outcome=receipt.outcome,
        exit_code=receipt.exit_code,
        stdout_sha256=receipt.stdout_sha256,
        stderr_sha256=receipt.stderr_sha256,
        stdout_bytes=receipt.stdout_bytes,
        stderr_bytes=receipt.stderr_bytes,
        cleanup=receipt.cleanup,
    )


class VerificationProfileResolver(Protocol):
    def resolve(self, ref: VerificationProfileRef) -> VerificationProfile:
        """Resolve one exact named profile from a trusted registry adapter."""


class WorkspaceSnapshotPort(Protocol):
    def capture(
        self, workspace: WorkspaceIdentity, claim_ref: ClaimRef
    ) -> VerificationSnapshot:
        """Return one complete immutable snapshot."""


class VerificationRunnerPort(Protocol):
    def run(
        self, request: VerificationRequest, effect: VerificationEffectLease
    ) -> VerificationRunResult:
        """Run only the opaque fixed request under the supplied effect fence."""


__all__ = [
    "MAX_ARGV_ITEMS",
    "MAX_ENV_ITEMS",
    "ApprovalAdmissionPort",
    "ApprovalRef",
    "ArgvDigest",
    "CleanupStatus",
    "DurableRecordStatus",
    "EffectBeginStatus",
    "EffectNonce",
    "EnvName",
    "OutputDigest",
    "PreparationStatus",
    "ReceiptDigest",
    "RecoveryRequired",
    "ResultSchema",
    "ResultSchemaId",
    "VerificationExecutableIdentity",
    "VerificationGate",
    "VerificationGateError",
    "VerificationHandle",
    "VerificationId",
    "VerificationOutcome",
    "VerificationProfile",
    "VerificationProfileBindingDigest",
    "VerificationProfileIdentity",
    "VerificationProfileResolver",
    "VerificationRef",
    "VerificationRunResult",
    "VerificationRunnerPort",
    "VerificationSnapshot",
    "VerificationStatePort",
    "VerificationTerminalResult",
    "WorkspaceSnapshotPort",
]
