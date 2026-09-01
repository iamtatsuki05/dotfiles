"""Pure path/resource admission and explicit task-lane routing.

This module consumes trusted, point-in-time observations supplied by a caller.
It never walks a filesystem, resolves a path, acquires a lock, persists a
reservation, starts a provider, or changes task/review state.  Unknown or
inconsistent observations are rejected rather than repaired or routed to
another lane.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Final, NewType, NoReturn, Protocol, SupportsIndex, cast
from weakref import WeakKeyDictionary

from .review_policy import (
    DependencyState,
    ReviewPair,
    SerialReviewPolicy,
    WorkerAssignment,
)
from .task_policy import (
    MAX_PATH_CHARS,
    MAX_WORKSPACE_CHARS,
    ResourceClaim,
    TaskId,
    TaskKind,
    TaskLane,
    TaskPhase,
    TaskSpec,
    WorkspaceIdentity,
)
from .topology import (
    AgentNode,
    Edge,
    EdgeKind,
    NodeId,
    Permission,
    ProfileRef,
    TeamDefinition,
    TeamId,
)

MAX_RESERVATION_ID_CHARS: Final = 256
MAX_RESOURCE_KEY_CHARS: Final = 256
_UNSAFE_RANGES: Final = ((0x00, 0x1F), (0x7F, 0x9F), (0xD800, 0xDFFF))
_UNSUPPORTED_GLOB: Final = frozenset("*?[]{}")
_WINDOWS_ABSOLUTE: Final = re.compile(r"^[A-Za-z]:")
_SHA256_DIGEST: Final = re.compile(r"[0-9a-f]{64}\Z")
_OPAQUE_IDENTIFIER: Final = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._:/-]{{0,{MAX_RESERVATION_ID_CHARS - 1}}}\Z"
)
_DIAGNOSTIC_PRIORITY: Final = {
    "invalid-type": 0,
    "invalid-task": 1,
    "empty-value": 2,
    "unsafe-text": 3,
    "absolute-path": 4,
    "path-traversal": 5,
    "unsupported-glob": 6,
    "unknown-path-kind": 7,
    "unknown-path-access": 8,
    "unknown-entry-kind": 9,
    "unknown-resource-mode": 10,
    "unknown-resource-key": 11,
    "duplicate-resource-claim": 12,
    "duplicate-resource-key": 13,
    "missing-resource-claim": 14,
    "extra-resource-claim": 15,
}


class PathResourcePolicyError(ValueError):
    """Raised when a typed path/resource/routing input is malformed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _error(code: str, message: str) -> PathResourcePolicyError:
    return PathResourcePolicyError(code, message)


def _text(
    value: object,
    context: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        raise _error("invalid-type", f"{context} must be a string")
    if not allow_empty and (not value or not value.strip()):
        raise _error("empty-value", f"{context} must not be empty")
    if len(value) > maximum:
        raise _error("value-too-long", f"{context} exceeds its character limit")
    if value != value.strip() or any(
        any(start <= ord(character) <= end for start, end in _UNSAFE_RANGES)
        or ord(character) in {0x2028, 0x2029}
        for character in value
    ):
        raise _error("unsafe-text", f"{context} contains unsafe text")
    return value


def _relative_path(value: object, context: str, *, allow_root: bool = False) -> str:
    candidate = _text(value, context, maximum=MAX_PATH_CHARS)
    if allow_root and candidate == ".":
        return "."
    if not candidate:
        raise _error("invalid-path", f"{context} must not be empty")
    if (
        candidate.startswith(("/", "\\"))
        or _WINDOWS_ABSOLUTE.match(candidate) is not None
    ):
        raise _error("absolute-path", f"{context} must be workspace-relative")
    if "\\" in candidate:
        raise _error("invalid-path", f"{context} must use POSIX separators")
    if candidate.endswith("/"):
        raise _error("invalid-path", f"{context} must not have a trailing slash")
    if any(character in _UNSUPPORTED_GLOB for character in candidate):
        raise _error("unsupported-glob", f"{context} must not contain a glob")
    components = candidate.split("/")
    if any(not component or component in {".", ".."} for component in components):
        raise _error("path-traversal", f"{context} contains an invalid component")
    return candidate


def _absolute_path(
    value: object, context: str, *, maximum: int = MAX_PATH_CHARS
) -> str:
    candidate = _text(value, context, maximum=maximum)
    if not candidate.startswith("/") or candidate.startswith("//"):
        raise _error("noncanonical-path", f"{context} must be absolute")
    normalized = posixpath.normpath(candidate)
    if normalized != candidate:
        raise _error("noncanonical-path", f"{context} must be normalized")
    return candidate


def _lexical_workspace(value: object, context: str) -> str:
    candidate = _text(value, context, maximum=MAX_WORKSPACE_CHARS)
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or posixpath.normpath(candidate) != candidate
    ):
        raise _error(
            "noncanonical-workspace", f"{context} must be absolute and normalized"
        )
    if candidate != "/" and candidate.endswith("/"):
        raise _error(
            "noncanonical-workspace", f"{context} must not have a trailing slash"
        )
    return candidate


def _nonnegative_int(value: object, context: str) -> int:
    if type(value) is not int or value < 0:
        raise _error("invalid-identity", f"{context} must be a non-negative integer")
    return value


def _optional_nonnegative_int(value: object, context: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, context)


def _tuple_value(value: object, context: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise _error("invalid-type", f"{context} must be an immutable tuple")
    return value


def _stable_failure(errors: list[PathResourcePolicyError]) -> None:
    if not errors:
        raise _error("invalid-input", "input is not admissible")
    selected = min(
        errors,
        key=lambda error: (
            _DIAGNOSTIC_PRIORITY.get(error.code, 100),
            error.code,
        ),
    )
    raise _error(selected.code, "input is not admissible")


class PathKind(str, Enum):
    """Whether a path claim denotes one entry or a directory subtree."""

    EXACT = "exact"
    DIRECTORY = "directory"


class PathAccess(str, Enum):
    """Access level granted by a path claim."""

    READ = "read"
    WRITE = "write"


class PathOperation(str, Enum):
    """Mutation/observation operation being admitted."""

    READ = "read"
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    RENAME = "rename"


class PathEntryKind(str, Enum):
    """Entry type reported by the trusted snapshot producer."""

    MISSING = "missing"
    REGULAR = "regular"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class WorkspaceObservation:
    """Trusted workspace identity and case semantics observation."""

    workspace: WorkspaceIdentity
    canonical_path: str
    device: int
    inode: int
    case_sensitive: bool

    def __post_init__(self) -> None:
        _lexical_workspace(self.workspace, "workspace.workspace")
        _absolute_path(
            self.canonical_path,
            "workspace.canonical_path",
            maximum=MAX_WORKSPACE_CHARS,
        )
        _nonnegative_int(self.device, "workspace.device")
        _nonnegative_int(self.inode, "workspace.inode")
        if not isinstance(self.case_sensitive, bool):
            raise _error(
                "invalid-case-semantics", "workspace.case_sensitive must be boolean"
            )


def _revalidate_workspace(value: object, context: str) -> WorkspaceObservation:
    if type(value) is not WorkspaceObservation:
        raise _error("invalid-workspace", f"{context} must be WorkspaceObservation")
    try:
        _lexical_workspace(value.workspace, f"{context}.workspace")
        _absolute_path(
            value.canonical_path,
            f"{context}.canonical_path",
            maximum=MAX_WORKSPACE_CHARS,
        )
        _nonnegative_int(value.device, f"{context}.device")
        _nonnegative_int(value.inode, f"{context}.inode")
        if not isinstance(value.case_sensitive, bool):
            raise _error(
                "invalid-case-semantics", f"{context}.case_sensitive must be boolean"
            )
    except AttributeError as exc:
        raise _error("invalid-workspace", f"{context} is malformed") from exc
    return value


@dataclass(frozen=True, slots=True)
class PathClaim:
    """One explicit relative path claim."""

    relative_path: str
    kind: PathKind
    access: PathAccess

    def __post_init__(self) -> None:
        _relative_path(self.relative_path, "path_claim.relative_path", allow_root=True)
        if not isinstance(self.kind, PathKind):
            raise _error("unknown-path-kind", "path_claim.kind must be PathKind")
        if not isinstance(self.access, PathAccess):
            raise _error("unknown-path-access", "path_claim.access must be PathAccess")


def _validate_path_claim(value: object, context: str) -> PathClaim:
    if type(value) is not PathClaim:
        raise _error("invalid-type", f"{context} must be PathClaim")
    try:
        _relative_path(
            value.relative_path,
            f"{context}.relative_path",
            allow_root=True,
        )
        if not isinstance(value.kind, PathKind):
            raise _error("unknown-path-kind", f"{context}.kind must be PathKind")
        if not isinstance(value.access, PathAccess):
            raise _error("unknown-path-access", f"{context}.access must be PathAccess")
    except AttributeError as exc:
        raise _error("invalid-type", f"{context} is malformed") from exc
    return value


def _validated_declared_paths(value: object, context: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise _error("invalid-type", f"{context} must be an immutable tuple")
    valid: list[str] = []
    errors: list[PathResourcePolicyError] = []
    for index, path in enumerate(value):
        try:
            valid.append(_relative_path(path, f"{context}[{index}]", allow_root=True))
        except PathResourcePolicyError as error:
            errors.append(error)
    if errors:
        _stable_failure(errors)
    return tuple(valid)


@dataclass(frozen=True, slots=True)
class PathObservation:
    """One immutable trusted snapshot observation; no filesystem access occurs here."""

    relative_path: str
    canonical_path: str
    entry_kind: PathEntryKind
    device: int | None
    inode: int | None
    nlink: int | None
    parent_device: int | None
    parent_inode: int | None
    ancestor_symlink: bool

    def __post_init__(self) -> None:
        _relative_path(
            self.relative_path, "path_observation.relative_path", allow_root=True
        )
        _absolute_path(
            self.canonical_path,
            "path_observation.canonical_path",
            maximum=MAX_WORKSPACE_CHARS,
        )
        if not isinstance(self.entry_kind, PathEntryKind):
            raise _error("unknown-entry-kind", "path_observation.entry_kind is unknown")
        _optional_nonnegative_int(self.device, "path_observation.device")
        _optional_nonnegative_int(self.inode, "path_observation.inode")
        _optional_nonnegative_int(self.nlink, "path_observation.nlink")
        _optional_nonnegative_int(self.parent_device, "path_observation.parent_device")
        _optional_nonnegative_int(self.parent_inode, "path_observation.parent_inode")
        if not isinstance(self.ancestor_symlink, bool):
            raise _error(
                "invalid-observation",
                "path_observation.ancestor_symlink must be boolean",
            )


def _validate_path_observation(value: object, context: str) -> PathObservation:
    if type(value) is not PathObservation:
        raise _error("invalid-type", f"{context} must be PathObservation")
    try:
        _relative_path(
            value.relative_path,
            f"{context}.relative_path",
            allow_root=True,
        )
        _absolute_path(
            value.canonical_path,
            f"{context}.canonical_path",
            maximum=MAX_WORKSPACE_CHARS,
        )
        if not isinstance(value.entry_kind, PathEntryKind):
            raise _error("unknown-entry-kind", f"{context}.entry_kind is unknown")
        _optional_nonnegative_int(value.device, f"{context}.device")
        _optional_nonnegative_int(value.inode, f"{context}.inode")
        _optional_nonnegative_int(value.nlink, f"{context}.nlink")
        _optional_nonnegative_int(value.parent_device, f"{context}.parent_device")
        _optional_nonnegative_int(value.parent_inode, f"{context}.parent_inode")
        if not isinstance(value.ancestor_symlink, bool):
            raise _error(
                "invalid-observation", f"{context}.ancestor_symlink must be boolean"
            )
    except AttributeError as exc:
        raise _error("invalid-type", f"{context} is malformed") from exc
    return value


@dataclass(frozen=True, slots=True)
class PathMutation:
    """Explicit path operation.  Destination is required only for rename."""

    operation: PathOperation
    source: str
    destination: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, PathOperation):
            raise _error("unknown-path-operation", "path_mutation.operation is unknown")
        _relative_path(self.source, "path_mutation.source")
        if self.operation is PathOperation.RENAME:
            if self.destination is None:
                raise _error("missing-destination", "rename requires a destination")
            _relative_path(self.destination, "path_mutation.destination")
        elif self.destination is not None:
            raise _error(
                "unexpected-destination",
                "only rename may specify a destination",
            )


def _validate_path_mutation(
    value: object, context: str = "path_mutation"
) -> PathMutation:
    if type(value) is not PathMutation:
        raise _error("invalid-mutation", f"{context} must be PathMutation")
    try:
        if not isinstance(value.operation, PathOperation):
            raise _error("unknown-path-operation", f"{context}.operation is unknown")
        _relative_path(value.source, f"{context}.source")
        if value.operation is PathOperation.RENAME:
            if value.destination is None:
                raise _error("missing-destination", "rename requires a destination")
            _relative_path(value.destination, f"{context}.destination")
        elif value.destination is not None:
            raise _error(
                "unexpected-destination", "only rename may specify a destination"
            )
    except AttributeError as exc:
        raise _error("invalid-mutation", f"{context} is malformed") from exc
    return value


@dataclass(frozen=True, slots=True)
class PathAdmission:
    """Pure result of path admission."""

    candidate: bool
    reason_code: str | None
    canonical_paths: tuple[str, ...] = ()

    @property
    def admitted(self) -> bool:
        """Alias useful to callers that describe admission rather than routing."""

        return self.candidate


def _claim_sort_key(claim: PathClaim) -> tuple[str, str, str]:
    return (
        claim.relative_path.casefold(),
        claim.relative_path,
        claim.kind.value + claim.access.value,
    )


def _root_aware_key(value: str, workspace: WorkspaceObservation) -> str:
    return value if workspace.case_sensitive else value.casefold()


def _claim_matches(
    claim: PathClaim, relative_path: str, workspace: WorkspaceObservation
) -> bool:
    claim_value = _root_aware_key(claim.relative_path, workspace)
    path_value = _root_aware_key(relative_path, workspace)
    if claim.kind is PathKind.EXACT:
        return claim_value == path_value
    if claim_value == ".":
        return True
    return path_value == claim_value or path_value.startswith(claim_value + "/")


def _claims_intersect(
    left: PathClaim, right: PathClaim, workspace: WorkspaceObservation
) -> bool:
    return _claim_matches(left, right.relative_path, workspace) or _claim_matches(
        right, left.relative_path, workspace
    )


def _path_parent(relative_path: str) -> str:
    if "/" not in relative_path:
        return "."
    return relative_path.rsplit("/", 1)[0]


def _canonical_relative(
    workspace: WorkspaceObservation, observation: PathObservation
) -> str:
    root = workspace.canonical_path
    candidate = observation.canonical_path
    root_key = _root_aware_key(root, workspace)
    candidate_key = _root_aware_key(candidate, workspace)
    if candidate_key == root_key:
        return "."
    prefix = root if root.endswith("/") else root + "/"
    prefix_key = _root_aware_key(prefix, workspace)
    if not candidate_key.startswith(prefix_key):
        raise _error(
            "outside-workspace", "observation canonical path is outside workspace"
        )
    relative = candidate[len(prefix) :]
    _relative_path(relative, "path_observation.canonical_relative_path")
    return relative


def _validate_workspace_observation(
    workspace: WorkspaceObservation, observation: PathObservation
) -> None:
    _validate_path_observation(observation, "path_observation")
    relative = _relative_path(
        observation.relative_path, "path_observation.relative_path", allow_root=True
    )
    canonical_relative = _canonical_relative(workspace, observation)
    if workspace.case_sensitive:
        if relative != canonical_relative:
            raise _error(
                "unknown-path-observation",
                "observation relative and canonical paths disagree",
            )
    elif relative.casefold() != canonical_relative.casefold():
        raise _error(
            "unknown-path-observation",
            "observation relative and canonical paths disagree",
        )

    is_root = relative == "."
    if is_root:
        if observation.entry_kind is not PathEntryKind.DIRECTORY:
            raise _error(
                "unknown-path-observation", "workspace root is not a directory"
            )
        if observation.nlink is None or observation.nlink < 1:
            raise _error(
                "unknown-path-observation",
                "workspace root identity is incomplete",
            )
        if (
            observation.device != workspace.device
            or observation.inode != workspace.inode
            or observation.parent_device is not None
            or observation.parent_inode is not None
        ):
            raise _error(
                "unknown-path-observation", "workspace root identity is incomplete"
            )
    else:
        if observation.parent_device is None or observation.parent_inode is None:
            raise _error("unknown-path-observation", "path parent identity is missing")
        if observation.parent_device != workspace.device:
            raise _error("device-mismatch", "path parent is on another device")
        if observation.entry_kind is PathEntryKind.MISSING:
            if (
                observation.device is not None
                or observation.inode is not None
                or observation.nlink is not None
            ):
                raise _error(
                    "unknown-path-observation", "missing entry has an identity"
                )
        elif (
            observation.device is None
            or observation.inode is None
            or observation.nlink is None
        ):
            raise _error(
                "unknown-path-observation", "existing entry identity is incomplete"
            )
        elif observation.device != workspace.device:
            raise _error("device-mismatch", "path is on another device")
        elif observation.nlink < 1:
            raise _error("unknown-path-observation", "existing entry nlink is invalid")

    if observation.entry_kind is PathEntryKind.SYMLINK or observation.ancestor_symlink:
        raise _error("symlink-path", "path or an ancestor is a symlink")
    if observation.entry_kind is PathEntryKind.OTHER:
        raise _error("special-path", "path is not a regular file or directory")
    if (
        observation.entry_kind is PathEntryKind.REGULAR
        and observation.nlink is not None
        and observation.nlink > 1
    ):
        raise _error("hardlink-path", "regular file has multiple links")


@dataclass(frozen=True, slots=True)
class PathClaimPolicy:
    """Explicit allow/deny path policy over one observed workspace."""

    workspace: WorkspaceObservation
    allowed: tuple[PathClaim, ...]
    denied: tuple[PathClaim, ...]
    reserved_roots: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, WorkspaceObservation):
            raise _error(
                "invalid-workspace", "path policy requires WorkspaceObservation"
            )
        _revalidate_workspace(self.workspace, "path_policy.workspace")
        allowed_raw = _tuple_value(self.allowed, "path_policy.allowed")
        denied_raw = _tuple_value(self.denied, "path_policy.denied")
        reserved_raw = _tuple_value(self.reserved_roots, "path_policy.reserved_roots")
        allowed = cast(tuple[PathClaim, ...], allowed_raw)
        denied = cast(tuple[PathClaim, ...], denied_raw)
        reserved = cast(tuple[str, ...], reserved_raw)
        if not allowed:
            raise _error("empty-allowed-paths", "path_policy.allowed must not be empty")
        claim_errors: list[PathResourcePolicyError] = []
        for value, context in (
            (allowed, "path_policy.allowed"),
            (denied, "path_policy.denied"),
        ):
            for index, item in enumerate(value):
                try:
                    _validate_path_claim(item, f"{context}[{index}]")
                except PathResourcePolicyError as error:
                    claim_errors.append(error)
        if claim_errors:
            _stable_failure(claim_errors)
        seen: set[str] = set()
        for item in (*allowed, *denied):
            path_key = _root_aware_key(item.relative_path, self.workspace)
            if path_key in seen:
                raise _error(
                    "case-collision"
                    if not self.workspace.case_sensitive
                    else "duplicate-path-claim",
                    "path claim is duplicated",
                )
            seen.add(path_key)
        for index, left in enumerate(allowed):
            for right in allowed[index + 1 :]:
                if _claims_intersect(left, right, self.workspace):
                    if (
                        not self.workspace.case_sensitive
                        and left.relative_path.casefold()
                        != right.relative_path.casefold()
                    ):
                        raise _error("case-collision", "allowed claims collide by case")
                    raise _error("path-overlap", "allowed path claims overlap")
        reserved_errors: list[PathResourcePolicyError] = []
        for reserved_item in reserved:
            try:
                _relative_path(
                    reserved_item, "path_policy.reserved_roots", allow_root=True
                )
            except PathResourcePolicyError as error:
                reserved_errors.append(error)
        if reserved_errors:
            _stable_failure(reserved_errors)
        reserved_keys = {
            _root_aware_key(reserved_item, self.workspace) for reserved_item in reserved
        }
        if len(reserved_keys) != len(reserved):
            raise _error("case-collision", "reserved roots collide by case")
        object.__setattr__(self, "allowed", tuple(sorted(allowed, key=_claim_sort_key)))
        object.__setattr__(self, "denied", tuple(sorted(denied, key=_claim_sort_key)))
        object.__setattr__(self, "reserved_roots", tuple(sorted(reserved)))

    @classmethod
    def from_task_spec(
        cls,
        task: TaskSpec,
        *,
        workspace: WorkspaceObservation,
        allowed: tuple[PathClaim, ...],
        denied: tuple[PathClaim, ...],
        reserved_roots: tuple[str, ...],
    ) -> PathClaimPolicy:
        """Adapt explicit TaskSpec path strings without inferring claim metadata."""

        if type(task) is not TaskSpec:
            raise _error("invalid-task", "task must be a TaskSpec")
        _revalidate_workspace(workspace, "path_policy.workspace")
        if type(allowed) is not tuple or type(denied) is not tuple:
            raise _error("invalid-type", "path claims must be immutable tuples")
        task = _revalidate_task_spec(task)
        allowed_paths = _validated_declared_paths(
            task.allowed_paths, "task.allowed_paths"
        )
        denied_paths = _validated_declared_paths(
            task.do_not_modify, "task.do_not_modify"
        )
        _check_one_to_one_paths(allowed_paths, allowed, "allowed", workspace)
        _check_one_to_one_paths(denied_paths, denied, "denied", workspace)
        return cls(workspace, allowed, denied, reserved_roots)

    def admit(
        self,
        mutation: PathMutation,
        observations: tuple[PathObservation, ...],
    ) -> PathAdmission:
        """Return a candidate only when every explicit path condition is proven."""

        _revalidate_path_policy(self)
        _validate_path_mutation(mutation)
        if type(observations) is not tuple:
            raise _error("invalid-type", "path observations must be an immutable tuple")
        touched = _touched_paths(mutation)
        for path in touched:
            _relative_path(path, "path_mutation.path", allow_root=True)

        for path in touched:
            if any(
                _claim_matches(claim, path, self.workspace) for claim in self.denied
            ):
                return _path_rejection("denied-path")
        for path in touched:
            if any(
                _claim_matches(
                    PathClaim(root, PathKind.DIRECTORY, PathAccess.WRITE),
                    path,
                    self.workspace,
                )
                for root in self.reserved_roots
            ):
                return _path_rejection("reserved-path")

        required_access = (
            PathAccess.READ
            if mutation.operation is PathOperation.READ
            else PathAccess.WRITE
        )
        paths_requiring_allow = touched
        if mutation.operation not in {PathOperation.DELETE, PathOperation.RENAME}:
            paths_requiring_allow = (mutation.source,)
        for path in paths_requiring_allow:
            if not any(
                _claim_matches(claim, path, self.workspace)
                and (
                    required_access is PathAccess.READ
                    or claim.access is PathAccess.WRITE
                )
                for claim in self.allowed
            ):
                return _path_rejection("path-outside-allowed")

        try:
            index = _observation_index(self.workspace, observations)
            for observation in sorted(
                observations,
                key=lambda item: _observation_key(item.relative_path, self.workspace),
            ):
                _validate_workspace_observation(self.workspace, observation)
            _validate_physical_identities(self.workspace, index)
            required = _required_observation_paths(mutation)
            tuple(index[_observation_key(path, self.workspace)] for path in required)
            for path in required:
                if path == ".":
                    continue
                current = index[_observation_key(path, self.workspace)]
                parent_path = _path_parent(path)
                if parent_path == ".":
                    parent_device: int | None = self.workspace.device
                    parent_inode: int | None = self.workspace.inode
                else:
                    parent = index.get(_observation_key(parent_path, self.workspace))
                    if parent is None:
                        raise _error(
                            "unknown-path-observation",
                            "path parent observation is missing",
                        )
                    if parent.entry_kind is not PathEntryKind.DIRECTORY:
                        raise _error(
                            "unknown-path-observation",
                            "path parent is not an existing directory",
                        )
                    parent_device = parent.device
                    parent_inode = parent.inode
                if (
                    current.parent_device != parent_device
                    or current.parent_inode != parent_inode
                ):
                    raise _error(
                        "unknown-path-observation",
                        "path parent identity does not match its observation",
                    )
        except KeyError:
            return _path_rejection("unknown-path-observation")
        except PathResourcePolicyError as exc:
            return _path_rejection(exc.code)
        except (AttributeError, TypeError):
            return _path_rejection("unknown-path-observation")

        target = index[_observation_key(mutation.source, self.workspace)]
        if (
            mutation.operation is PathOperation.READ
            and target.entry_kind is PathEntryKind.MISSING
        ):
            return _path_rejection("read-missing-path")
        if (
            mutation.operation is PathOperation.CREATE
            and target.entry_kind is not PathEntryKind.MISSING
        ):
            return _path_rejection("create-existing-path")
        if (
            mutation.operation is PathOperation.MODIFY
            and target.entry_kind is not PathEntryKind.REGULAR
        ):
            return _path_rejection("modify-nonregular-path")
        if mutation.operation is PathOperation.DELETE and target.entry_kind not in {
            PathEntryKind.REGULAR,
            PathEntryKind.DIRECTORY,
        }:
            return _path_rejection("delete-missing-path")
        if mutation.operation is PathOperation.RENAME:
            if mutation.destination is None:
                raise _error("missing-destination", "rename requires a destination")
            if _observation_key(mutation.source, self.workspace) == _observation_key(
                mutation.destination, self.workspace
            ):
                return _path_rejection("rename-same-path")
            if target.entry_kind not in {
                PathEntryKind.REGULAR,
                PathEntryKind.DIRECTORY,
            }:
                return _path_rejection("rename-missing-source")
            destination = index[_observation_key(mutation.destination, self.workspace)]
            if destination.entry_kind not in {
                PathEntryKind.MISSING,
                PathEntryKind.REGULAR,
                PathEntryKind.DIRECTORY,
            }:
                return _path_rejection("rename-invalid-destination")

        canonical_paths = tuple(
            sorted(
                {
                    index[_observation_key(path, self.workspace)].canonical_path
                    for path in required
                }
            )
        )
        return PathAdmission(True, None, canonical_paths)


def _revalidate_path_policy(policy: PathClaimPolicy) -> PathClaimPolicy:
    if type(policy) is not PathClaimPolicy:
        raise _error("invalid-path-policy", "path policy type is not exact")
    try:
        workspace = policy.workspace
        allowed = policy.allowed
        denied = policy.denied
        reserved = policy.reserved_roots
    except AttributeError as exc:
        raise _error("invalid-path-policy", "path policy is malformed") from exc
    _revalidate_workspace(workspace, "path_policy.workspace")
    if type(allowed) is not tuple or not allowed:
        raise _error("empty-allowed-paths", "path_policy.allowed must not be empty")
    if type(denied) is not tuple or type(reserved) is not tuple:
        raise _error("invalid-type", "path policy values must be immutable tuples")
    claim_errors: list[PathResourcePolicyError] = []
    for index, claim in enumerate(allowed):
        try:
            _validate_path_claim(claim, f"path_policy.allowed[{index}]")
        except PathResourcePolicyError as error:
            claim_errors.append(error)
    for index, claim in enumerate(denied):
        try:
            _validate_path_claim(claim, f"path_policy.denied[{index}]")
        except PathResourcePolicyError as error:
            claim_errors.append(error)
    if claim_errors:
        _stable_failure(claim_errors)
    seen: set[str] = set()
    for claim in (*allowed, *denied):
        key = _root_aware_key(claim.relative_path, workspace)
        if key in seen:
            raise _error(
                "case-collision"
                if not workspace.case_sensitive
                else "duplicate-path-claim",
                "path claim is duplicated",
            )
        seen.add(key)
    for index, left in enumerate(allowed):
        for right in allowed[index + 1 :]:
            if _claims_intersect(left, right, workspace):
                raise _error("path-overlap", "allowed path claims overlap")
    reserved_errors: list[PathResourcePolicyError] = []
    for root in reserved:
        try:
            _relative_path(root, "path_policy.reserved_roots", allow_root=True)
        except PathResourcePolicyError as error:
            reserved_errors.append(error)
    if reserved_errors:
        _stable_failure(reserved_errors)
    reserved_keys = {_root_aware_key(root, workspace) for root in reserved}
    if len(reserved_keys) != len(reserved):
        raise _error("case-collision", "reserved roots collide by case")
    return policy


def _revalidate_task_spec(task: TaskSpec) -> TaskSpec:
    """Re-run TaskSpec validation so forged nested values cannot widen scope."""

    if type(task) is not TaskSpec:
        raise _error("invalid-task", "task must be a TaskSpec")
    try:
        string_fields: tuple[tuple[object, str], ...] = (
            (task.task_id, "task.task_id"),
            (task.title, "task.title"),
            (task.context, "task.context"),
            (task.goal, "task.goal"),
            (task.verification, "task.verification"),
        )
        if task.escalation_node is not None:
            string_fields += ((task.escalation_node, "task.escalation_node"),)
        for value, context in string_fields:
            if type(value) is not str:
                raise _error("invalid-task", f"{context} must use an exact string")
        acceptance = _tuple_value(task.acceptance, "task.acceptance")
        allowed_paths = _tuple_value(task.allowed_paths, "task.allowed_paths")
        do_not_modify = _tuple_value(task.do_not_modify, "task.do_not_modify")
        dependencies = _tuple_value(task.dependencies, "task.dependencies")
        resource_claims = _tuple_value(task.resource_claims, "task.resource_claims")
        for value, context in (
            (acceptance, "task.acceptance"),
            (allowed_paths, "task.allowed_paths"),
            (do_not_modify, "task.do_not_modify"),
            (dependencies, "task.dependencies"),
        ):
            for index, item in enumerate(value):
                if type(item) is not str:
                    raise _error(
                        "invalid-task", f"{context}[{index}] must use an exact string"
                    )
        for index, claim in enumerate(resource_claims):
            _validate_resource_claim(claim, f"task.resource_claims[{index}]")
        if type(task.kind) is not TaskKind or type(task.lane) is not TaskLane:
            raise _error("invalid-task", "task kind and lane must use exact enums")
        validated = TaskSpec(
            task_id=task.task_id,
            title=task.title,
            context=task.context,
            goal=task.goal,
            acceptance=task.acceptance,
            allowed_paths=task.allowed_paths,
            do_not_modify=task.do_not_modify,
            dependencies=task.dependencies,
            verification=task.verification,
            escalation_node=task.escalation_node,
            kind=task.kind,
            lane=task.lane,
            resource_claims=task.resource_claims,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise _error("invalid-task", "task is malformed") from exc
    return validated


def _canonical_task_projection(task: TaskSpec) -> tuple[object, ...]:
    value = _revalidate_task_spec(task)
    return (
        value.task_id,
        value.title,
        value.context,
        value.goal,
        value.acceptance,
        value.allowed_paths,
        value.do_not_modify,
        value.dependencies,
        value.verification,
        value.escalation_node,
        value.kind.value,
        value.lane.value,
        tuple(claim.name for claim in value.resource_claims),
    )


def _revalidate_review_pair(value: object, context: str) -> ReviewPair:
    if type(value) is not ReviewPair:
        raise _error("invalid-type", f"{context} must be ReviewPair")
    try:
        _text(
            value.worker_node,
            f"{context}.worker_node",
            maximum=MAX_RESERVATION_ID_CHARS,
        )
        _text(
            value.reviewer_node,
            f"{context}.reviewer_node",
            maximum=MAX_RESERVATION_ID_CHARS,
        )
        if value.worker_node == value.reviewer_node:
            raise _error("self-review", f"{context} cannot contain the same node twice")
    except AttributeError as exc:
        raise _error("invalid-type", f"{context} is malformed") from exc
    return value


def _canonical_team_definition(
    value: object, context: str
) -> tuple[
    str,
    tuple[tuple[str, str, str, str, str, bool], ...],
    tuple[tuple[str, str, str], ...],
]:
    """Validate and normalize topology fields without invoking dataclass equality."""

    if type(value) is not TeamDefinition:
        raise _error("invalid-profile", f"{context} must be an exact TeamDefinition")
    try:
        team_id = value.team_id
        nodes = value.nodes
        edges = value.edges
    except AttributeError as exc:
        raise _error("invalid-profile", f"{context} is malformed") from exc
    _text(team_id, f"{context}.team_id", maximum=MAX_RESERVATION_ID_CHARS)
    if type(nodes) is not tuple or type(edges) is not tuple:
        raise _error(
            "invalid-profile", f"{context} nodes and edges must be immutable tuples"
        )

    node_records: list[tuple[str, str, str, str, str, bool]] = []
    node_ids: set[str] = set()
    node_errors: list[PathResourcePolicyError] = []
    for index, node in enumerate(nodes):
        try:
            if type(node) is not AgentNode:
                raise _error(
                    "invalid-profile", f"{context}.nodes[{index}] has an invalid type"
                )
            node_id = _text(
                node.node_id,
                f"{context}.nodes[{index}].node_id",
                maximum=MAX_RESERVATION_ID_CHARS,
            )
            label = _text(
                node.label,
                f"{context}.nodes[{index}].label",
                maximum=MAX_RESERVATION_ID_CHARS,
            )
            if type(node.profile) is not ProfileRef:
                raise _error(
                    "invalid-profile",
                    f"{context}.nodes[{index}].profile has an invalid type",
                )
            provider = _text(
                node.profile.provider,
                f"{context}.nodes[{index}].profile.provider",
                maximum=MAX_RESERVATION_ID_CHARS,
            )
            transport = _text(
                node.profile.transport,
                f"{context}.nodes[{index}].profile.transport",
                maximum=MAX_RESERVATION_ID_CHARS,
            )
            permission = node.profile.permission
            if type(permission) is not str or permission not in {
                "orchestrator",
                "read-only",
                "workspace-write",
            }:
                raise _error(
                    "invalid-permission",
                    f"{context}.nodes[{index}].profile.permission is invalid",
                )
            if type(node.is_main) is not bool:
                raise _error(
                    "invalid-profile",
                    f"{context}.nodes[{index}].is_main must be boolean",
                )
            folded = node_id.casefold()
            if folded in node_ids:
                raise _error(
                    "invalid-profile", f"{context} contains duplicate node IDs"
                )
            node_ids.add(folded)
            node_records.append(
                (node_id, label, provider, transport, permission, node.is_main)
            )
        except AttributeError:
            node_errors.append(
                _error("invalid-profile", f"{context}.nodes[{index}] is malformed")
            )
        except PathResourcePolicyError as error:
            node_errors.append(error)
    if node_errors:
        _stable_failure(node_errors)

    edge_records: list[tuple[str, str, str]] = []
    edge_signatures: set[tuple[str, str, str]] = set()
    edge_errors: list[PathResourcePolicyError] = []
    for index, edge in enumerate(edges):
        try:
            if type(edge) is not Edge:
                raise _error(
                    "invalid-profile", f"{context}.edges[{index}] has an invalid type"
                )
            source = _text(
                edge.source,
                f"{context}.edges[{index}].source",
                maximum=MAX_RESERVATION_ID_CHARS,
            )
            target = _text(
                edge.target,
                f"{context}.edges[{index}].target",
                maximum=MAX_RESERVATION_ID_CHARS,
            )
            if type(edge.kind) is not EdgeKind:
                raise _error(
                    "invalid-profile",
                    f"{context}.edges[{index}].kind has an invalid type",
                )
            signature = source, target, edge.kind.value
            if signature in edge_signatures:
                raise _error("invalid-profile", f"{context} contains duplicate edges")
            edge_signatures.add(signature)
            edge_records.append(signature)
        except AttributeError:
            edge_errors.append(
                _error("invalid-profile", f"{context}.edges[{index}] is malformed")
            )
        except PathResourcePolicyError as error:
            edge_errors.append(error)
    if edge_errors:
        _stable_failure(edge_errors)
    return (
        team_id,
        tuple(sorted(node_records)),
        tuple(sorted(edge_records)),
    )


def _canonical_team_value(value: object, context: str) -> TeamDefinition:
    """Rebuild a topology value from its validated primitive projection."""

    team_id, node_records, edge_records = _canonical_team_definition(value, context)
    nodes = tuple(
        AgentNode(
            NodeId(node_id),
            label,
            ProfileRef(provider, transport, cast(Permission, permission)),
            is_main=is_main,
        )
        for node_id, label, provider, transport, permission, is_main in node_records
    )
    edges = tuple(
        Edge(NodeId(source), NodeId(target), EdgeKind(kind))
        for source, target, kind in edge_records
    )
    return TeamDefinition(TeamId(team_id), nodes, edges)


def _canonical_dependency_projection(
    value: object, context: str
) -> tuple[tuple[str, str], ...]:
    if type(value) is not tuple:
        raise _error("review-policy-mismatch", f"{context} must be an immutable tuple")
    records: list[tuple[str, str]] = []
    errors: list[PathResourcePolicyError] = []
    for index, dependency in enumerate(value):
        try:
            if type(dependency) is not DependencyState:
                raise _error(
                    "review-policy-mismatch",
                    f"{context}[{index}] has an invalid type",
                )
            task_id = _text(
                dependency.task_id,
                f"{context}[{index}].task_id",
                maximum=MAX_RESERVATION_ID_CHARS,
            )
            if type(dependency.phase) is not TaskPhase:
                raise _error(
                    "review-policy-mismatch",
                    f"{context}[{index}].phase has an invalid type",
                )
            records.append((task_id, dependency.phase.value))
        except AttributeError:
            errors.append(
                _error("review-policy-mismatch", f"{context}[{index}] is malformed")
            )
        except PathResourcePolicyError as error:
            errors.append(error)
    if errors:
        _stable_failure(errors)
    return tuple(sorted(records))


def _canonical_dependency_values(
    value: object, context: str
) -> tuple[DependencyState, ...]:
    """Rebuild dependency observations from validated primitive fields."""

    projection = _canonical_dependency_projection(value, context)
    return tuple(
        DependencyState(TaskId(task_id), TaskPhase(phase))
        for task_id, phase in projection
    )


def _revalidate_worker_assignment(value: object, context: str) -> WorkerAssignment:
    """Validate nested assignment identity without trusting subclass fields."""

    if type(value) is not WorkerAssignment:
        raise _error(
            "review-policy-mismatch",
            f"{context} must be an exact WorkerAssignment",
        )
    try:
        text_values: tuple[tuple[object, str], ...] = (
            (value.run_id, f"{context}.run_id"),
            (value.task_id, f"{context}.task_id"),
            (value.dispatch_id, f"{context}.dispatch_id"),
            (value.attempt_id, f"{context}.attempt_id"),
            (value.worker_node, f"{context}.worker_node"),
            (value.reviewer_node, f"{context}.reviewer_node"),
            (value.worker_terminal_id, f"{context}.worker_terminal_id"),
            (value.reviewer_terminal_id, f"{context}.reviewer_terminal_id"),
        )
        for item, item_context in text_values:
            _text(item, item_context, maximum=MAX_RESERVATION_ID_CHARS)
        if type(value.review_round) is not int or value.review_round < 1:
            raise _error("review-policy-mismatch", f"{context}.review_round is invalid")
        for item, item_context in (
            (value.target_head, f"{context}.target_head"),
            (value.target_tree_digest, f"{context}.target_tree_digest"),
        ):
            if item is not None:
                _text(item, item_context, maximum=MAX_RESERVATION_ID_CHARS)
        return WorkerAssignment(
            run_id=value.run_id,
            task_id=value.task_id,
            dispatch_id=value.dispatch_id,
            attempt_id=value.attempt_id,
            worker_node=value.worker_node,
            reviewer_node=value.reviewer_node,
            worker_terminal_id=value.worker_terminal_id,
            reviewer_terminal_id=value.reviewer_terminal_id,
            review_round=value.review_round,
            target_head=value.target_head,
            target_tree_digest=value.target_tree_digest,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise _error("review-policy-mismatch", f"{context} is malformed") from exc


def _canonical_serial_policy_projection(
    policy: SerialReviewPolicy,
) -> tuple[object, ...]:
    if type(policy) is not SerialReviewPolicy:
        raise _error("review-policy-mismatch", "serial review policy type is not exact")
    try:
        policy_task = policy.task
        policy_team = policy.team_definition
        policy_pair = policy.pair
        policy_worker = policy.worker_node
        policy_rounds = policy.max_review_rounds
        policy_dependencies = policy.dependency_states
        policy_active_assignments = policy.active_assignments
        policy_fingerprint = policy.fingerprint
    except AttributeError as exc:
        raise _error(
            "review-policy-mismatch", "serial review policy is malformed"
        ) from exc
    task_projection = _canonical_task_projection(policy_task)
    team_projection = _canonical_team_definition(
        policy_team, "review_policy.team_definition"
    )
    pair = _revalidate_review_pair(policy_pair, "review_policy.pair")
    worker = _text(
        policy_worker,
        "review_policy.worker_node",
        maximum=MAX_RESERVATION_ID_CHARS,
    )
    if type(policy_rounds) is not int or policy_rounds < 1:
        raise _error("review-policy-mismatch", "review policy rounds are invalid")
    dependencies = _canonical_dependency_projection(
        policy_dependencies, "review_policy.dependency_states"
    )
    if type(policy_active_assignments) is not tuple:
        raise _error(
            "review-policy-mismatch",
            "review_policy.active_assignments must be an immutable tuple",
        )
    active_errors: list[PathResourcePolicyError] = []
    for index, item in enumerate(policy_active_assignments):
        try:
            _revalidate_worker_assignment(
                item, f"review_policy.active_assignments[{index}]"
            )
        except PathResourcePolicyError as error:
            active_errors.append(error)
    if active_errors:
        _stable_failure(active_errors)
    fingerprint = _text(
        policy_fingerprint,
        "review_policy.fingerprint",
        maximum=MAX_RESERVATION_ID_CHARS,
    )
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise _error("review-policy-mismatch", "review policy fingerprint is invalid")
    return (
        task_projection,
        team_projection,
        (pair.worker_node, pair.reviewer_node),
        worker,
        policy_rounds,
        dependencies,
        fingerprint,
    )


def _revalidate_lane_profile_binding(
    profile: LaneProfileBinding,
) -> LaneProfileBinding:
    if type(profile) is not LaneProfileBinding:
        raise _error("invalid-profile", "profile must be LaneProfileBinding")
    try:
        team_definition = profile.team_definition
        worker_node = profile.worker_node
        reviewer_pair = profile.reviewer_pair
        serial_review_policy = profile.serial_review_policy
    except AttributeError as exc:
        raise _error("invalid-profile", "profile is malformed") from exc
    if type(team_definition) is not TeamDefinition:
        raise _error("invalid-profile", "profile.team_definition is invalid")
    _canonical_team_definition(team_definition, "profile.team_definition")
    _text(worker_node, "profile.worker_node", maximum=MAX_RESERVATION_ID_CHARS)
    if reviewer_pair is not None:
        _revalidate_review_pair(reviewer_pair, "profile.reviewer_pair")
    if (
        serial_review_policy is not None
        and type(serial_review_policy) is not SerialReviewPolicy
    ):
        raise _error("invalid-type", "profile.serial_review_policy is invalid")
    return profile


def _canonical_lane_profile_projection(
    value: LaneProfileBinding,
) -> tuple[object, ...]:
    """Return the immutable #50 profile input read by one routing decision."""

    profile = _revalidate_lane_profile_binding(value)
    team_projection = _canonical_team_definition(
        profile.team_definition, "profile.team_definition"
    )
    pair = profile.reviewer_pair
    pair_projection: tuple[str, str] | None
    if pair is None:
        pair_projection = None
    else:
        validated_pair = _revalidate_review_pair(pair, "profile.reviewer_pair")
        pair_projection = (
            str(validated_pair.worker_node),
            str(validated_pair.reviewer_node),
        )
    policy = profile.serial_review_policy
    policy_projection: tuple[object, ...] | None
    if policy is None:
        policy_projection = None
    else:
        canonical_policy = _canonical_serial_policy_projection(policy)
        active_assignments = tuple(
            (
                str(assignment.run_id),
                str(assignment.task_id),
                str(assignment.dispatch_id),
                str(assignment.attempt_id),
                str(assignment.worker_node),
                str(assignment.reviewer_node),
                str(assignment.worker_terminal_id),
                str(assignment.reviewer_terminal_id),
                str(assignment.review_round),
                "" if assignment.target_head is None else str(assignment.target_head),
                ""
                if assignment.target_tree_digest is None
                else str(assignment.target_tree_digest),
            )
            for assignment in policy.active_assignments
        )
        policy_projection = (*canonical_policy, active_assignments)
    return (
        team_projection,
        str(profile.worker_node),
        pair_projection,
        policy_projection,
    )


def _check_one_to_one_paths(
    task_paths: tuple[str, ...],
    claims: tuple[PathClaim, ...],
    context: str,
    workspace: WorkspaceObservation,
) -> None:
    _revalidate_workspace(workspace, "path_policy.workspace")
    if type(task_paths) is not tuple or type(claims) is not tuple:
        raise _error("invalid-type", f"{context} paths and claims must be immutable")
    validated_task_paths = _validated_declared_paths(
        task_paths, f"task.{context}.paths"
    )
    task_keys = tuple(_root_aware_key(path, workspace) for path in validated_task_paths)
    if len(set(task_keys)) != len(task_keys):
        code = "duplicate-path-claim" if workspace.case_sensitive else "case-collision"
        raise _error(code, f"task {context} paths are duplicated")
    claim_paths: list[str] = []
    claim_errors: list[PathResourcePolicyError] = []
    for index, item in enumerate(claims):
        try:
            _validate_path_claim(item, f"{context}[{index}]")
        except PathResourcePolicyError as error:
            claim_errors.append(error)
        else:
            claim_paths.append(_root_aware_key(item.relative_path, workspace))
    if claim_errors:
        _stable_failure(claim_errors)
    if len(set(claim_paths)) != len(claim_paths):
        raise _error("duplicate-path-claim", f"{context} claims are duplicated")
    missing = set(task_keys) - set(claim_paths)
    extra = set(claim_paths) - set(task_keys)
    if missing:
        raise _error("missing-claim", f"{context} path claim is missing")
    if extra:
        raise _error("extra-claim", f"{context} path claim is extra")


def _observation_key(path: str, workspace: WorkspaceObservation) -> str:
    normalized = "." if path in {"", "."} else path
    return _root_aware_key(normalized, workspace)


def _observation_index(
    workspace: WorkspaceObservation,
    observations: tuple[PathObservation, ...],
) -> dict[str, PathObservation]:
    result: dict[str, PathObservation] = {}
    raw_paths: dict[str, str] = {}
    valid: list[PathObservation] = []
    errors: list[PathResourcePolicyError] = []
    for index, item in enumerate(observations):
        try:
            _validate_path_observation(item, f"path_observations[{index}]")
        except PathResourcePolicyError as error:
            errors.append(error)
        else:
            valid.append(item)
    if errors:
        _stable_failure(errors)
    for item in sorted(valid, key=lambda value: value.relative_path):
        key = _observation_key(item.relative_path, workspace)
        previous = raw_paths.get(key)
        if previous is not None and previous != item.relative_path:
            raise _error("case-collision", "observations collide by case")
        if key in result:
            raise _error("duplicate-path-observation", "path observation is duplicated")
        raw_paths[key] = item.relative_path
        result[key] = item
    return result


def _validate_physical_identities(
    workspace: WorkspaceObservation,
    observations: dict[str, PathObservation],
) -> None:
    identities: dict[tuple[int, int], str] = {}
    root = observations.get(_observation_key(".", workspace))
    if root is not None:
        identities[(workspace.device, workspace.inode)] = "."
    for path in sorted(observations):
        if path == _observation_key(".", workspace):
            continue
        observation = observations[path]
        if observation.device is None or observation.inode is None:
            continue
        identity = observation.device, observation.inode
        previous = identities.get(identity)
        if previous is not None and previous != path:
            raise _error(
                "hardlink-path",
                "different observed paths share a physical identity",
            )
        identities[identity] = path


def _touched_paths(mutation: PathMutation) -> tuple[str, ...]:
    if mutation.operation in {
        PathOperation.READ,
        PathOperation.CREATE,
        PathOperation.MODIFY,
    }:
        return (mutation.source,)
    if mutation.operation is PathOperation.DELETE:
        return (mutation.source, _path_parent(mutation.source))
    if mutation.destination is None:
        raise _error("missing-destination", "rename requires a destination")
    return (
        mutation.source,
        mutation.destination,
        _path_parent(mutation.source),
        _path_parent(mutation.destination),
    )


def _required_observation_paths(mutation: PathMutation) -> tuple[str, ...]:
    # A trusted snapshot must prove the complete chain from the workspace root
    # to every operation target.  This keeps a target's parent metadata from
    # being used as a substitute for an unobserved ancestor.
    targets: tuple[str, ...] = (mutation.source,)
    if mutation.operation is PathOperation.RENAME:
        if mutation.destination is None:
            raise _error("missing-destination", "rename requires a destination")
        targets = (mutation.source, mutation.destination)
    paths: list[str] = ["."]
    for target in targets:
        components = target.split("/")
        for index in range(1, len(components) + 1):
            path = "/".join(components[:index])
            if path not in paths:
                paths.append(path)
    return tuple(paths)


def _path_rejection(reason_code: str) -> PathAdmission:
    return PathAdmission(False, reason_code, ())


ResourceKey = NewType("ResourceKey", str)
ReservationDigest = NewType("ReservationDigest", str)

_COMPLETION_ADMISSION_REF_ISSUER: Final[object] = object()


@dataclass(
    frozen=True,
    slots=True,
    weakref_slot=True,
    init=False,
    repr=False,
    eq=False,
)
class CompletionAdmissionRef:
    """#50-issued completion admission reference.

    The reference is intentionally return-only. Its issuer marker and
    object-identity binding guard the in-process handoff boundary; persistence
    and record readback remain the authority across process boundaries.
    """

    reference: str
    digest: str
    _issuer: object = field(init=False, repr=False, compare=False)

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
        del cls, args, kwargs
        raise TypeError("CompletionAdmissionRef is return-only")

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("CompletionAdmissionRef is return-only")

    def __repr__(self) -> str:
        return "<CompletionAdmissionRef opaque>"

    def __copy__(self) -> NoReturn:
        raise TypeError("CompletionAdmissionRef cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("CompletionAdmissionRef cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("CompletionAdmissionRef cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex, /) -> NoReturn:
        del protocol
        raise TypeError("CompletionAdmissionRef cannot be pickled")


_COMPLETION_ADMISSION_BINDINGS: WeakKeyDictionary[
    CompletionAdmissionRef, tuple[str, str]
] = WeakKeyDictionary()
_COMPLETION_ADMISSION_BINDINGS_LOCK: Final = RLock()


def _completion_admission_reference(value: object, context: str) -> str:
    candidate = _text(
        value,
        context,
        maximum=MAX_RESERVATION_ID_CHARS,
    )
    if _OPAQUE_IDENTIFIER.fullmatch(candidate) is None:
        raise _error("invalid-reference", f"{context} is not a safe identifier")
    return candidate


def _completion_admission_digest(value: object, context: str) -> str:
    candidate = _text(value, context, maximum=64)
    if _SHA256_DIGEST.fullmatch(candidate) is None:
        raise _error("invalid-digest", f"{context} must be a lowercase SHA-256 digest")
    return candidate


def _issue_completion_admission_ref(
    reference: str, digest: str
) -> CompletionAdmissionRef:
    """Issue one validated in-process completion admission reference."""

    validated_reference = _completion_admission_reference(
        reference,
        "completion_admission_ref.reference",
    )
    validated_digest = _completion_admission_digest(
        digest,
        "completion_admission_ref.digest",
    )
    result = object.__new__(CompletionAdmissionRef)
    object.__setattr__(result, "reference", validated_reference)
    object.__setattr__(result, "digest", validated_digest)
    object.__setattr__(
        result,
        "_issuer",
        _COMPLETION_ADMISSION_REF_ISSUER,
    )
    with _COMPLETION_ADMISSION_BINDINGS_LOCK:
        _COMPLETION_ADMISSION_BINDINGS[result] = (
            validated_reference,
            validated_digest,
        )
    _validate_completion_admission_ref(result)
    return result


def _validate_completion_admission_ref(value: object) -> None:
    """Validate a #50-issued completion admission reference."""

    if type(value) is not CompletionAdmissionRef:
        raise _error(
            "invalid-completion-admission-ref",
            "completion admission reference type is not exact",
        )
    try:
        reference = object.__getattribute__(value, "reference")
        digest = object.__getattribute__(value, "digest")
        issuer = object.__getattribute__(value, "_issuer")
    except AttributeError as exc:
        raise _error(
            "invalid-completion-admission-ref",
            "completion admission reference is malformed",
        ) from exc
    _completion_admission_reference(reference, "completion_admission_ref.reference")
    _completion_admission_digest(digest, "completion_admission_ref.digest")
    if issuer is not _COMPLETION_ADMISSION_REF_ISSUER:
        raise _error(
            "invalid-completion-admission-ref",
            "completion admission reference issuer is invalid",
        )
    with _COMPLETION_ADMISSION_BINDINGS_LOCK:
        binding = _COMPLETION_ADMISSION_BINDINGS.get(value)
    if binding != (reference, digest):
        raise _error(
            "invalid-completion-admission-ref",
            "completion admission reference binding is invalid",
        )


class ResourceMode(str, Enum):
    """Explicit sharing mode for one logical resource key."""

    SHARED = "shared"
    EXCLUSIVE = "exclusive"


@dataclass(frozen=True, slots=True)
class ResourceClaimPolicy:
    """A TaskSpec claim paired with explicit canonical key and mode."""

    claim: ResourceClaim
    key: ResourceKey
    mode: ResourceMode

    def __post_init__(self) -> None:
        if type(self.claim) is not ResourceClaim:
            raise _error("invalid-type", "resource policy claim must be ResourceClaim")
        _text(self.key, "resource_policy.key", maximum=MAX_RESOURCE_KEY_CHARS)
        if not isinstance(self.mode, ResourceMode):
            raise _error("unknown-resource-mode", "resource_policy.mode is unknown")


def _validate_resource_claim(value: object, context: str) -> ResourceClaim:
    if type(value) is not ResourceClaim:
        raise _error("invalid-type", f"{context} must be ResourceClaim")
    try:
        _text(value.name, f"{context}.name", maximum=MAX_RESOURCE_KEY_CHARS)
    except AttributeError as exc:
        raise _error("invalid-type", f"{context} is malformed") from exc
    return value


def _validate_resource_claim_policy(value: object, context: str) -> ResourceClaimPolicy:
    if type(value) is not ResourceClaimPolicy:
        raise _error("invalid-type", f"{context} must be ResourceClaimPolicy")
    try:
        _validate_resource_claim(value.claim, f"{context}.claim")
        _text(value.key, f"{context}.key", maximum=MAX_RESOURCE_KEY_CHARS)
        if not isinstance(value.mode, ResourceMode):
            raise _error("unknown-resource-mode", f"{context}.mode is unknown")
    except AttributeError as exc:
        raise _error("invalid-type", f"{context} is malformed") from exc
    return value


def _resource_sort_key(value: ResourceClaimPolicy) -> tuple[str, str, str]:
    _validate_resource_claim_policy(value, "resource_claim")
    return (value.key, value.claim.name, value.mode.value)


def adapt_resource_claims(
    task: TaskSpec,
    bindings: tuple[ResourceClaimPolicy, ...],
    *,
    known_keys: frozenset[ResourceKey],
) -> tuple[ResourceClaimPolicy, ...]:
    """Bind every TaskSpec ResourceClaim to one known key and explicit mode."""

    if type(task) is not TaskSpec:
        raise _error("invalid-task", "task must be a TaskSpec")
    task = _revalidate_task_spec(task)
    if type(bindings) is not tuple or type(known_keys) is not frozenset:
        raise _error(
            "invalid-type", "resource bindings and known keys must be immutable"
        )
    known_values: set[str] = set()
    known_errors: list[PathResourcePolicyError] = []
    for key_item in known_keys:
        try:
            known_value = _text(
                key_item,
                "known_resource_key",
                maximum=MAX_RESOURCE_KEY_CHARS,
            )
        except PathResourcePolicyError as error:
            known_errors.append(error)
        else:
            known_values.add(known_value)
    if known_errors:
        _stable_failure(known_errors)
    try:
        task_resource_claims = task.resource_claims
    except AttributeError as exc:
        raise _error("invalid-task", "task resource claims are missing") from exc
    if type(task_resource_claims) is not tuple:
        raise _error("invalid-type", "task.resource_claims must be immutable")
    task_names: list[str] = []
    task_errors: list[PathResourcePolicyError] = []
    for index, claim in enumerate(task_resource_claims):
        try:
            task_names.append(
                _validate_resource_claim(claim, f"task.resource_claims[{index}]").name
            )
        except PathResourcePolicyError as error:
            task_errors.append(error)
    if task_errors:
        _stable_failure(task_errors)
    if len({name.casefold() for name in task_names}) != len(task_names):
        task_errors.append(
            _error("duplicate-resource-claim", "task resource claims are duplicated")
        )
    seen_names: set[str] = set()
    seen_keys: set[str] = set()
    by_name: dict[str, ResourceClaimPolicy] = {}
    binding_errors: list[PathResourcePolicyError] = []
    valid_bindings: list[ResourceClaimPolicy] = []
    for index, item in enumerate(bindings):
        try:
            _validate_resource_claim_policy(item, f"resource_bindings[{index}]")
        except PathResourcePolicyError as error:
            binding_errors.append(error)
        else:
            valid_bindings.append(item)
    if binding_errors:
        _stable_failure(binding_errors + task_errors)
    for item in valid_bindings:
        name = item.claim.name
        folded = name.casefold()
        if folded in seen_names:
            task_errors.append(
                _error("duplicate-resource-claim", "resource binding is duplicated")
            )
        seen_names.add(folded)
        if item.key not in known_values:
            task_errors.append(
                _error("unknown-resource-key", "resource key is not registered")
            )
        if item.key in seen_keys:
            task_errors.append(
                _error("duplicate-resource-key", "resource key is bound more than once")
            )
        seen_keys.add(item.key)
        by_name[name] = item
    if task_errors:
        _stable_failure(task_errors)
    expected = set(task_names)
    actual = set(by_name)
    binding_shape_errors: list[PathResourcePolicyError] = []
    if expected - actual:
        binding_shape_errors.append(
            _error("missing-resource-claim", "resource binding is missing")
        )
    if actual - expected:
        binding_shape_errors.append(
            _error("extra-resource-claim", "resource binding is extra")
        )
    if binding_shape_errors:
        _stable_failure(binding_shape_errors)
    return tuple(sorted(by_name.values(), key=_resource_sort_key))


def resource_claims_conflict(
    left: tuple[ResourceClaimPolicy, ...],
    right: tuple[ResourceClaimPolicy, ...],
) -> bool:
    """Return whether the explicit claims cannot share a reservation."""

    if type(left) is not tuple or type(right) is not tuple:
        raise _error("invalid-type", "resource claims must be immutable tuples")
    validation_errors: list[PathResourcePolicyError] = []
    for index, claim in enumerate((*left, *right)):
        try:
            _validate_resource_claim_policy(claim, f"resource_claims[{index}]")
        except PathResourcePolicyError as error:
            validation_errors.append(error)
    if validation_errors:
        _stable_failure(validation_errors)
    for left_claim in left:
        for right_claim in right:
            if left_claim.key == right_claim.key and not (
                left_claim.mode is ResourceMode.SHARED
                and right_claim.mode is ResourceMode.SHARED
            ):
                return True
    return False


@dataclass(frozen=True, slots=True)
class ResourceReservationAuthority:
    """Opaque owner/lease/fencing identity supplied by the reservation authority."""

    owner_id: str
    lease_epoch: int
    fencing_token: int

    def __post_init__(self) -> None:
        if type(self) is not ResourceReservationAuthority:
            raise _error("invalid-authority", "reservation authority type is not exact")
        _text(
            self.owner_id,
            "reservation_authority.owner_id",
            maximum=MAX_RESERVATION_ID_CHARS,
        )
        _nonnegative_int(self.lease_epoch, "reservation_authority.lease_epoch")
        if type(self.fencing_token) is not int or self.fencing_token <= 0:
            raise _error(
                "invalid-authority",
                "reservation_authority.fencing_token must be positive",
            )


def _validate_reservation_authority(
    value: object, context: str, *, allow_none: bool = False
) -> ResourceReservationAuthority | None:
    if value is None and allow_none:
        return None
    if type(value) is not ResourceReservationAuthority:
        raise _error("invalid-authority", f"{context} is invalid")
    try:
        _text(value.owner_id, f"{context}.owner_id", maximum=MAX_RESERVATION_ID_CHARS)
        _nonnegative_int(value.lease_epoch, f"{context}.lease_epoch")
        if type(value.fencing_token) is not int or value.fencing_token <= 0:
            raise _error(
                "invalid-authority",
                f"{context}.fencing_token must be positive",
            )
    except (AttributeError, PathResourcePolicyError) as exc:
        raise _error("invalid-authority", f"{context} is malformed") from exc
    return value


def _same_text(left: object, right: object) -> bool:
    return type(left) is str and type(right) is str and str.__eq__(left, right)


def _same_int(left: object, right: object) -> bool:
    return type(left) is int and type(right) is int and int.__eq__(left, right)


def _same_canonical_value(left: object, right: object) -> bool:
    """Compare only canonical primitive values, never an untrusted object."""

    if left is None or right is None:
        return left is None and right is None
    if type(left) is str and type(right) is str:
        return str.__eq__(left, right)
    if type(left) is bool and type(right) is bool:
        return bool.__eq__(left, right)
    if type(left) is int and type(right) is int:
        return int.__eq__(left, right)
    if type(left) is tuple and type(right) is tuple:
        if len(left) != len(right):
            return False
        return all(
            _same_canonical_value(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return False


def _same_authority(
    left: ResourceReservationAuthority | None,
    right: ResourceReservationAuthority | None,
) -> bool:
    if (
        type(left) is not ResourceReservationAuthority
        or type(right) is not ResourceReservationAuthority
    ):
        return left is None and right is None
    return (
        _same_text(left.owner_id, right.owner_id)
        and _same_int(left.lease_epoch, right.lease_epoch)
        and _same_int(left.fencing_token, right.fencing_token)
    )


def _same_keys(left: tuple[ResourceKey, ...], right: tuple[ResourceKey, ...]) -> bool:
    if type(left) is not tuple or type(right) is not tuple or len(left) != len(right):
        return False
    return all(
        _same_text(left_item, right_item)
        for left_item, right_item in zip(left, right, strict=True)
    )


def _reservation_digest(
    task_id: TaskId,
    reservation_id: str,
    claims: tuple[ResourceClaimPolicy, ...],
    authority: ResourceReservationAuthority | None,
) -> ReservationDigest:
    parts: list[str] = [
        "reservation-request-v1",
        str(task_id),
        reservation_id,
    ]
    if authority is None:
        parts.extend(("authority", "none"))
    else:
        parts.extend(
            (
                "authority",
                authority.owner_id,
                str(authority.lease_epoch),
                str(authority.fencing_token),
            )
        )
    for claim in sorted(claims, key=_resource_sort_key):
        parts.extend(("claim", claim.claim.name, str(claim.key), claim.mode.value))
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return ReservationDigest(digest.hexdigest())


def _validate_reservation_digest(value: object, context: str) -> ReservationDigest:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise _error("reservation-identity", f"{context} must be a SHA-256 digest")
    return ReservationDigest(value)


class ReservationStatus(str, Enum):
    """Typed reservation result status."""

    RESERVED = "reserved"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class ResourceReservationRequest:
    """Minimal consumer request; ownership and persistence remain downstream."""

    task_id: TaskId
    claims: tuple[ResourceClaimPolicy, ...]
    reservation_id: str
    authority: ResourceReservationAuthority | None
    request_digest: ReservationDigest = field(init=False)

    def __post_init__(self) -> None:
        if type(self) is not ResourceReservationRequest:
            raise _error(
                "invalid-reservation-request", "reservation request type is not exact"
            )
        _text(
            self.task_id,
            "reservation_request.task_id",
            maximum=MAX_RESERVATION_ID_CHARS,
        )
        if type(self.claims) is not tuple:
            raise _error("invalid-type", "reservation_request.claims must be immutable")
        claim_errors: list[PathResourcePolicyError] = []
        for index, item in enumerate(self.claims):
            try:
                _validate_resource_claim_policy(
                    item, f"reservation_request.claims[{index}]"
                )
            except PathResourcePolicyError as error:
                claim_errors.append(error)
        if claim_errors:
            _stable_failure(claim_errors)
        if len({item.key for item in self.claims}) != len(self.claims):
            raise _error("duplicate-resource-key", "reservation request repeats a key")
        object.__setattr__(
            self,
            "claims",
            tuple(sorted(self.claims, key=_resource_sort_key)),
        )
        _text(
            self.reservation_id,
            "reservation_request.reservation_id",
            maximum=MAX_RESERVATION_ID_CHARS,
        )
        _validate_reservation_authority(
            self.authority, "reservation_request.authority", allow_none=True
        )
        if self.claims and self.authority is None:
            raise _error(
                "missing-authority", "resource claims require reservation authority"
            )
        if not self.claims and self.authority is not None:
            raise _error(
                "unexpected-authority",
                "a claim-free request must not carry reservation authority",
            )
        object.__setattr__(
            self,
            "request_digest",
            _reservation_digest(
                self.task_id,
                self.reservation_id,
                self.claims,
                self.authority,
            ),
        )


@dataclass(frozen=True, slots=True)
class ResourceReservationResult:
    """Typed port result; reservation identity must be echoed exactly."""

    status: ReservationStatus
    reservation_id: str
    claim_keys: tuple[ResourceKey, ...]
    authority: ResourceReservationAuthority | None
    task_id: TaskId | None
    request_digest: ReservationDigest | None

    def __post_init__(self) -> None:
        if type(self) is not ResourceReservationResult:
            raise _error(
                "invalid-reservation-result", "reservation result type is not exact"
            )
        if not isinstance(self.status, ReservationStatus):
            raise _error("unknown-reservation-status", "reservation status is unknown")
        _text(
            self.reservation_id,
            "reservation_result.reservation_id",
            maximum=MAX_RESERVATION_ID_CHARS,
        )
        if type(self.claim_keys) is not tuple:
            raise _error(
                "invalid-type", "reservation_result.claim_keys must be immutable"
            )
        key_errors: list[PathResourcePolicyError] = []
        for key in self.claim_keys:
            try:
                _text(
                    key,
                    "reservation_result.claim_key",
                    maximum=MAX_RESOURCE_KEY_CHARS,
                )
            except PathResourcePolicyError as error:
                key_errors.append(error)
        if key_errors:
            _stable_failure(key_errors)
        if len(set(self.claim_keys)) != len(self.claim_keys):
            raise _error("duplicate-resource-key", "reservation result repeats a key")
        object.__setattr__(self, "claim_keys", tuple(sorted(self.claim_keys)))
        _validate_reservation_authority(
            self.authority, "reservation_result.authority", allow_none=True
        )
        if self.task_id is not None:
            _text(
                self.task_id,
                "reservation_result.task_id",
                maximum=MAX_RESERVATION_ID_CHARS,
            )
        if self.request_digest is not None:
            _validate_reservation_digest(
                self.request_digest, "reservation_result.request_digest"
            )


def _revalidate_reservation_result(
    value: object,
) -> ResourceReservationResult:
    if type(value) is not ResourceReservationResult:
        raise _error("invalid-reservation-result", "reservation result is invalid")
    try:
        status = value.status
        reservation_id = value.reservation_id
        claim_keys = value.claim_keys
        authority = value.authority
        task_id = value.task_id
        request_digest = value.request_digest
    except AttributeError as exc:
        raise _error(
            "invalid-reservation-result", "reservation result is malformed"
        ) from exc
    if not isinstance(status, ReservationStatus):
        raise _error("unknown-reservation-status", "reservation status is unknown")
    _text(
        reservation_id,
        "reservation_result.reservation_id",
        maximum=MAX_RESERVATION_ID_CHARS,
    )
    if type(claim_keys) is not tuple:
        raise _error("invalid-type", "reservation_result.claim_keys must be immutable")
    key_errors: list[PathResourcePolicyError] = []
    for key in claim_keys:
        try:
            _text(key, "reservation_result.claim_key", maximum=MAX_RESOURCE_KEY_CHARS)
        except PathResourcePolicyError as error:
            key_errors.append(error)
    if key_errors:
        _stable_failure(key_errors)
    if len(set(claim_keys)) != len(claim_keys):
        raise _error("duplicate-resource-key", "reservation result repeats a key")
    _validate_reservation_authority(
        authority, "reservation_result.authority", allow_none=True
    )
    if task_id is not None:
        _text(task_id, "reservation_result.task_id", maximum=MAX_RESERVATION_ID_CHARS)
    if request_digest is not None:
        _validate_reservation_digest(
            request_digest, "reservation_result.request_digest"
        )
    return value


class ResourceReservationPort(Protocol):
    """Atomic downstream reservation seam; no implementation is provided here."""

    def reserve(self, request: ResourceReservationRequest) -> ResourceReservationResult:
        """Reserve all request claims or return an explicit non-reserved status."""


class DispatchMode(str, Enum):
    """Downstream execution mode selected by the pure lane policy."""

    SERIAL = "serial"
    READ_ONLY = "read-only"


@dataclass(frozen=True, slots=True)
class LaneProfileBinding:
    """Validated topology/profile facts supplied by the composition root."""

    team_definition: TeamDefinition
    worker_node: NodeId
    reviewer_pair: ReviewPair | None
    serial_review_policy: SerialReviewPolicy | None = None

    def __post_init__(self) -> None:
        if type(self.team_definition) is not TeamDefinition:
            raise _error("invalid-profile", "profile.team_definition is invalid")
        _canonical_team_definition(self.team_definition, "profile.team_definition")
        _text(self.worker_node, "profile.worker_node", maximum=MAX_RESERVATION_ID_CHARS)
        if (
            self.reviewer_pair is not None
            and type(self.reviewer_pair) is not ReviewPair
        ):
            raise _error("invalid-type", "profile.reviewer_pair must be ReviewPair")
        if (
            self.serial_review_policy is not None
            and type(self.serial_review_policy) is not SerialReviewPolicy
        ):
            raise _error("invalid-type", "profile.serial_review_policy is invalid")


def _profile_nodes(
    profile: LaneProfileBinding,
) -> tuple[AgentNode, AgentNode | None] | None:
    """Resolve profile nodes without revalidating topology or consulting a registry."""

    try:
        definition = profile.team_definition
        _canonical_team_definition(definition, "profile.team_definition")
        nodes = definition.nodes
        pair = profile.reviewer_pair
    except (AttributeError, PathResourcePolicyError):
        return None
    if type(nodes) is not tuple:
        return None
    by_id: dict[str, AgentNode] = {}
    folded: set[str] = set()
    for node in nodes:
        if type(node) is not AgentNode:
            return None
        if type(node.node_id) is not str or not node.node_id:
            return None
        folded_id = node.node_id.casefold()
        if folded_id in folded or node.node_id in by_id:
            return None
        try:
            permission = node.profile.permission
        except AttributeError:
            return None
        if type(permission) is not str or permission not in {
            "orchestrator",
            "read-only",
            "workspace-write",
        }:
            return None
        folded.add(folded_id)
        by_id[node.node_id] = node
    if type(profile.worker_node) is not str:
        return None
    worker = by_id.get(profile.worker_node)
    if worker is None:
        return None
    if pair is None:
        return worker, None
    if type(pair.reviewer_node) is not str:
        return None
    reviewer = by_id.get(pair.reviewer_node)
    if reviewer is None:
        return None
    return worker, reviewer


@dataclass(frozen=True, slots=True)
class LaneRoutingDecision:
    """Pure lane decision and downstream handoff observations."""

    lane: TaskLane
    candidate: bool
    dispatch_mode: DispatchMode | None
    serial_review_required: bool
    completion_gate_required: bool
    permits_workspace_write: bool
    parallel_candidate: bool
    reservation: ResourceReservationResult | None
    reason_code: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.lane, TaskLane):
            raise _error("unknown-lane", "routing decision lane is unknown")
        if not isinstance(self.candidate, bool) or not isinstance(
            self.parallel_candidate, bool
        ):
            raise _error("invalid-decision", "routing decision booleans are invalid")
        if self.parallel_candidate:
            raise _error("parallel-disabled", "parallel candidate is not supported")


def _profile_matches_serial_policy(
    task: TaskSpec, profile: LaneProfileBinding
) -> str | None:
    try:
        policy = profile.serial_review_policy
    except AttributeError:
        return "review-policy-mismatch"
    if policy is None:
        return "review-policy-missing"
    if type(policy) is not SerialReviewPolicy:
        return "review-policy-mismatch"
    try:
        policy_projection = _canonical_serial_policy_projection(policy)
        policy_task = _revalidate_task_spec(policy.task)
        policy_team_definition = _canonical_team_value(
            policy.team_definition, "review_policy.team_definition"
        )
        policy_pair = _revalidate_review_pair(policy.pair, "review_policy.pair")
        policy_worker_node = _text(
            policy.worker_node,
            "review_policy.worker_node",
            maximum=MAX_RESERVATION_ID_CHARS,
        )
        policy_max_review_rounds = policy.max_review_rounds
        if type(policy_max_review_rounds) is not int or policy_max_review_rounds < 1:
            raise _error("review-policy-mismatch", "review policy rounds are invalid")
        policy_dependency_states = _canonical_dependency_values(
            policy.dependency_states, "review_policy.dependency_states"
        )
        if type(policy.active_assignments) is not tuple:
            raise _error(
                "review-policy-mismatch",
                "review_policy.active_assignments must be an immutable tuple",
            )
        policy_active_assignments = tuple(
            _revalidate_worker_assignment(
                item, f"review_policy.active_assignments[{index}]"
            )
            for index, item in enumerate(policy.active_assignments)
        )
        task_projection = _canonical_task_projection(task)
    except (AttributeError, PathResourcePolicyError):
        return "review-policy-mismatch"
    resolved_nodes = _profile_nodes(profile)
    try:
        profile_pair = profile.reviewer_pair
        profile_team_definition = profile.team_definition
        profile_worker_node = _text(
            profile.worker_node,
            "profile.worker_node",
            maximum=MAX_RESERVATION_ID_CHARS,
        )
    except AttributeError:
        return "review-policy-mismatch"
    if resolved_nodes is None or profile_pair is None:
        return "review-profile"
    worker, reviewer = resolved_nodes
    if reviewer is None or not _same_text(worker.profile.permission, "workspace-write"):
        return "review-profile"
    if not _same_text(reviewer.profile.permission, "read-only"):
        return "review-profile"
    try:
        profile_pair = _revalidate_review_pair(profile_pair, "profile.reviewer_pair")
        profile_pair_projection = (
            profile_pair.worker_node,
            profile_pair.reviewer_node,
        )
        profile_team_identity = _canonical_team_definition(
            profile_team_definition, "profile.team_definition"
        )
    except PathResourcePolicyError:
        return "review-profile"
    if (
        not _same_canonical_value(policy_projection[0], task_projection)
        or not _same_canonical_value(policy_projection[1], profile_team_identity)
        or not _same_text(policy_projection[3], profile_worker_node)
        or not _same_canonical_value(policy_projection[2], profile_pair_projection)
        or not _same_text(policy_pair.worker_node, profile_worker_node)
        or not _same_text(policy_pair.reviewer_node, profile_pair.reviewer_node)
    ):
        return "review-policy-mismatch"
    try:
        expected = SerialReviewPolicy(
            task=policy_task,
            team_definition=policy_team_definition,
            worker_node=NodeId(policy_worker_node),
            max_review_rounds=policy_max_review_rounds,
            dependency_states=policy_dependency_states,
            active_assignments=policy_active_assignments,
        )
        expected_projection = _canonical_serial_policy_projection(expected)
    except (
        AttributeError,
        LookupError,
        PathResourcePolicyError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return "review-policy-mismatch"
    if (
        not _same_canonical_value(policy_projection[0], expected_projection[0])
        or not _same_canonical_value(policy_projection[1], expected_projection[1])
        or not _same_canonical_value(policy_projection[2], expected_projection[2])
        or not _same_canonical_value(policy_projection[3], expected_projection[3])
        or not _same_canonical_value(policy_projection[4], expected_projection[4])
        or not _same_canonical_value(policy_projection[5], expected_projection[5])
        or not _same_canonical_value(policy_projection[6], expected_projection[6])
    ):
        return "review-policy-mismatch"
    return None


def _validate_route_claims(
    task: TaskSpec,
    claims: tuple[ResourceClaimPolicy, ...],
    known_keys: frozenset[ResourceKey],
) -> tuple[tuple[ResourceClaimPolicy, ...] | None, str | None]:
    try:
        adapted = adapt_resource_claims(task, claims, known_keys=known_keys)
    except PathResourcePolicyError as exc:
        return None, exc.code
    return adapted, None


def _path_policy_matches_task(task: TaskSpec, policy: PathClaimPolicy) -> str | None:
    try:
        _revalidate_path_policy(policy)
        allowed_paths = _validated_declared_paths(
            task.allowed_paths, "task.allowed_paths"
        )
        denied_paths = _validated_declared_paths(
            task.do_not_modify, "task.do_not_modify"
        )
        _check_one_to_one_paths(
            allowed_paths, policy.allowed, "allowed", policy.workspace
        )
        _check_one_to_one_paths(denied_paths, policy.denied, "denied", policy.workspace)
    except PathResourcePolicyError as exc:
        return exc.code
    return None


def _reservation_decision(
    task: TaskSpec,
    claims: tuple[ResourceClaimPolicy, ...],
    reservation_port: ResourceReservationPort,
    reservation_id: str,
    authority: ResourceReservationAuthority | None,
) -> tuple[ResourceReservationResult | None, str | None]:
    if not claims:
        if authority is not None:
            return None, "unexpected-authority"
        return None, None
    if authority is None:
        return None, "missing-authority"
    request_claims = tuple(sorted(claims, key=_resource_sort_key))
    request = ResourceReservationRequest(
        task.task_id, request_claims, reservation_id, authority
    )
    try:
        result = reservation_port.reserve(request)
    except (AttributeError, LookupError, OSError, RuntimeError, TypeError, ValueError):
        # A missing/failed downstream authority is unknown, never a fallback.
        return None, "reservation-unknown"
    try:
        _revalidate_reservation_result(result)
    except PathResourcePolicyError as exc:
        return None, exc.code
    if not _same_authority(result.authority, request.authority):
        return result, "reservation-authority-mismatch"
    if not _same_text(result.task_id, request.task_id) or not _same_text(
        result.request_digest, request.request_digest
    ):
        return result, "reservation-identity"
    if result.status is not ReservationStatus.RESERVED:
        return result, f"reservation-{result.status.value}"
    expected_keys = tuple(item.key for item in request_claims)
    if not _same_text(result.reservation_id, request.reservation_id) or not _same_keys(
        result.claim_keys, expected_keys
    ):
        return result, "reservation-identity"
    return result, None


def _decision(
    task: TaskSpec,
    *,
    candidate: bool,
    dispatch_mode: DispatchMode | None,
    serial_review_required: bool,
    completion_gate_required: bool,
    permits_workspace_write: bool,
    reservation: ResourceReservationResult | None,
    reason_code: str | None,
) -> LaneRoutingDecision:
    return LaneRoutingDecision(
        lane=task.lane,
        candidate=candidate,
        dispatch_mode=dispatch_mode,
        serial_review_required=serial_review_required,
        completion_gate_required=completion_gate_required,
        permits_workspace_write=permits_workspace_write,
        parallel_candidate=False,
        reservation=reservation,
        reason_code=reason_code,
    )


def route_task(
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
) -> LaneRoutingDecision:
    """Admit one task to its explicit lane without fallback or side effects."""

    if type(task) is not TaskSpec:
        raise _error("invalid-task", "task must be a TaskSpec")
    try:
        task_lane = task.lane
        task_kind = task.kind
    except AttributeError as exc:
        raise _error("invalid-task", "task is malformed") from exc
    if not isinstance(task_lane, TaskLane):
        raise _error("unknown-lane", "task lane is unknown")
    if not isinstance(task_kind, TaskKind):
        raise _error("unknown-kind", "task kind is unknown")
    task = _revalidate_task_spec(task)
    if type(path_policy) is not PathClaimPolicy:
        raise _error("invalid-path-policy", "path_policy must be PathClaimPolicy")
    _revalidate_lane_profile_binding(profile)
    # The port is deliberately structural: its implementation belongs to a
    # later store/lease layer and need only expose the typed reserve method.
    if not callable(getattr(reservation_port, "reserve", None)):
        raise _error("invalid-reservation-port", "reservation_port lacks reserve")
    _text(reservation_id, "reservation_id", maximum=MAX_RESERVATION_ID_CHARS)

    path_policy_reason = _path_policy_matches_task(task, path_policy)
    if path_policy_reason is not None:
        return _decision(
            task,
            candidate=False,
            dispatch_mode=None,
            serial_review_required=False,
            completion_gate_required=False,
            permits_workspace_write=False,
            reservation=None,
            reason_code=path_policy_reason,
        )

    path_admission = path_policy.admit(path_mutation, path_observations)
    if not path_admission.candidate:
        return _decision(
            task,
            candidate=False,
            dispatch_mode=None,
            serial_review_required=False,
            completion_gate_required=False,
            permits_workspace_write=False,
            reservation=None,
            reason_code=path_admission.reason_code,
        )

    adapted_claims, claim_reason = _validate_route_claims(
        task, resource_claims, known_keys
    )
    if claim_reason is not None:
        return _decision(
            task,
            candidate=False,
            dispatch_mode=None,
            serial_review_required=False,
            completion_gate_required=False,
            permits_workspace_write=False,
            reservation=None,
            reason_code=claim_reason,
        )
    if adapted_claims is None:
        raise _error("invalid-resource-claims", "resource claims could not be adapted")
    resource_claims = adapted_claims

    resolved_profile = _profile_nodes(profile)
    if resolved_profile is None:
        return _decision(
            task,
            candidate=False,
            dispatch_mode=None,
            serial_review_required=False,
            completion_gate_required=False,
            permits_workspace_write=False,
            reservation=None,
            reason_code="profile-topology",
        )
    worker_node, _ = resolved_profile

    if task.lane is TaskLane.RESEARCH:
        if task.kind is not TaskKind.RESEARCH:
            return _decision(
                task,
                candidate=False,
                dispatch_mode=None,
                serial_review_required=False,
                completion_gate_required=False,
                permits_workspace_write=False,
                reservation=None,
                reason_code="research-kind",
            )
        if (
            not _same_text(worker_node.profile.permission, "read-only")
            or profile.reviewer_pair is not None
            or profile.serial_review_policy is not None
        ):
            return _decision(
                task,
                candidate=False,
                dispatch_mode=None,
                serial_review_required=False,
                completion_gate_required=False,
                permits_workspace_write=False,
                reservation=None,
                reason_code="research-profile",
            )
        if resource_claims or any(
            claim.access is PathAccess.WRITE for claim in path_policy.allowed
        ):
            return _decision(
                task,
                candidate=False,
                dispatch_mode=None,
                serial_review_required=False,
                completion_gate_required=False,
                permits_workspace_write=False,
                reservation=None,
                reason_code="research-write",
            )
        if path_mutation.operation is not PathOperation.READ:
            return _decision(
                task,
                candidate=False,
                dispatch_mode=None,
                serial_review_required=False,
                completion_gate_required=False,
                permits_workspace_write=False,
                reservation=None,
                reason_code="research-write",
            )
        return _decision(
            task,
            candidate=True,
            dispatch_mode=DispatchMode.READ_ONLY,
            serial_review_required=False,
            completion_gate_required=False,
            permits_workspace_write=False,
            reservation=None,
            reason_code=None,
        )

    if task.lane not in {TaskLane.NORMAL, TaskLane.EXPRESS}:
        raise _error("unknown-lane", "task lane is not supported")
    if task.kind is TaskKind.RESEARCH:
        return _decision(
            task,
            candidate=False,
            dispatch_mode=None,
            serial_review_required=False,
            completion_gate_required=False,
            permits_workspace_write=False,
            reservation=None,
            reason_code="lane-kind-mismatch",
        )
    if not _same_text(worker_node.profile.permission, "workspace-write"):
        return _decision(
            task,
            candidate=False,
            dispatch_mode=None,
            serial_review_required=False,
            completion_gate_required=False,
            permits_workspace_write=False,
            reservation=None,
            reason_code="worker-profile",
        )
    review_reason = _profile_matches_serial_policy(task, profile)
    if review_reason is not None:
        return _decision(
            task,
            candidate=False,
            dispatch_mode=None,
            serial_review_required=False,
            completion_gate_required=False,
            permits_workspace_write=False,
            reservation=None,
            reason_code=review_reason,
        )
    if path_mutation.operation is PathOperation.READ:
        return _decision(
            task,
            candidate=False,
            dispatch_mode=None,
            serial_review_required=False,
            completion_gate_required=False,
            permits_workspace_write=False,
            reservation=None,
            reason_code="write-operation-required",
        )

    if task.lane is TaskLane.EXPRESS:
        if task.kind is not TaskKind.SMALL_CHANGE:
            return _decision(
                task,
                candidate=False,
                dispatch_mode=None,
                serial_review_required=False,
                completion_gate_required=False,
                permits_workspace_write=False,
                reservation=None,
                reason_code="express-kind",
            )
        if task.dependencies:
            return _decision(
                task,
                candidate=False,
                dispatch_mode=None,
                serial_review_required=False,
                completion_gate_required=False,
                permits_workspace_write=False,
                reservation=None,
                reason_code="express-dependencies",
            )
        if (
            len(path_policy.allowed) != 1
            or path_policy.allowed[0].kind is not PathKind.EXACT
            or path_policy.allowed[0].access is not PathAccess.WRITE
            or path_mutation.operation is not PathOperation.MODIFY
        ):
            return _decision(
                task,
                candidate=False,
                dispatch_mode=None,
                serial_review_required=False,
                completion_gate_required=False,
                permits_workspace_write=False,
                reservation=None,
                reason_code="express-claim",
            )
        if any(claim.mode is ResourceMode.EXCLUSIVE for claim in resource_claims):
            return _decision(
                task,
                candidate=False,
                dispatch_mode=None,
                serial_review_required=False,
                completion_gate_required=False,
                permits_workspace_write=False,
                reservation=None,
                reason_code="express-exclusive-resource",
            )

    reservation, reservation_reason = _reservation_decision(
        task,
        resource_claims,
        reservation_port,
        reservation_id,
        reservation_authority,
    )
    if reservation_reason is not None:
        return _decision(
            task,
            candidate=False,
            dispatch_mode=None,
            serial_review_required=False,
            completion_gate_required=False,
            permits_workspace_write=False,
            reservation=reservation,
            reason_code=reservation_reason,
        )
    return _decision(
        task,
        candidate=True,
        dispatch_mode=DispatchMode.SERIAL,
        serial_review_required=True,
        completion_gate_required=True,
        permits_workspace_write=True,
        reservation=reservation,
        reason_code=None,
    )


def path_claims_overlap(left: PathClaimPolicy, right: PathClaimPolicy) -> bool:
    """Return whether two policies' allowed claims intersect."""

    if type(left) is not PathClaimPolicy or type(right) is not PathClaimPolicy:
        raise _error("invalid-path-policy", "both values must be PathClaimPolicy")
    _revalidate_path_policy(left)
    _revalidate_path_policy(right)
    if (
        left.workspace.device != right.workspace.device
        or left.workspace.inode != right.workspace.inode
    ):
        return False
    if left.workspace.case_sensitive != right.workspace.case_sensitive:
        return True
    if _root_aware_key(
        left.workspace.canonical_path, left.workspace
    ) != _root_aware_key(right.workspace.canonical_path, left.workspace):
        return False
    return any(
        _claims_intersect(claim_left, claim_right, left.workspace)
        for claim_left in left.allowed
        for claim_right in right.allowed
    )


__all__ = [
    "CompletionAdmissionRef",
    "DispatchMode",
    "LaneProfileBinding",
    "LaneRoutingDecision",
    "PathAccess",
    "PathAdmission",
    "PathClaim",
    "PathClaimPolicy",
    "PathEntryKind",
    "PathKind",
    "PathMutation",
    "PathObservation",
    "PathOperation",
    "PathResourcePolicyError",
    "ReservationDigest",
    "ReservationStatus",
    "ResourceClaimPolicy",
    "ResourceKey",
    "ResourceMode",
    "ResourceReservationAuthority",
    "ResourceReservationPort",
    "ResourceReservationRequest",
    "ResourceReservationResult",
    "WorkspaceObservation",
    "adapt_resource_claims",
    "path_claims_overlap",
    "resource_claims_conflict",
    "route_task",
]
