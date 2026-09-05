"""Pure TaskSpec and task-policy state version 4 contracts.

This module contains policy data and validation only.  It does not open a
store, inspect a workspace, start a provider, or perform a workflow
transition.  A :class:`TaskPolicyStateV4` is an observation/data value; a
later store implementation must re-check its identity and sequence before
accepting an :class:`ExpectedSequenceUpdate`.
"""

from __future__ import annotations

import heapq
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, NewType, Protocol, cast

from .topology import NodeId, TeamId

TaskId = NewType("TaskId", str)
AttemptId = NewType("AttemptId", str)
DispatchId = NewType("DispatchId", str)
ClaimRef = NewType("ClaimRef", str)
ReceiptRef = NewType("ReceiptRef", str)
WorkspaceIdentity = NewType("WorkspaceIdentity", str)
VerificationProfileRef = NewType("VerificationProfileRef", str)
GitObjectId = NewType("GitObjectId", str)
TreeDigest = NewType("TreeDigest", str)

_CURRENT_STATE_POLICY_VERSION: Final = 4
# Public compatibility name for callers that inspect the policy version.  All
# runtime validation uses the private snapshot above so rebinding this name
# cannot change the current-state contract.
STATE_POLICY_VERSION: Final = _CURRENT_STATE_POLICY_VERSION
MAX_TASKS: Final = 256
MAX_TASK_ID_CHARS: Final = 64
MAX_TASK_TEXT_CHARS: Final = 4096
MAX_ACCEPTANCE_ITEMS: Final = 64
MAX_ACCEPTANCE_TEXT_CHARS: Final = 2048
MAX_PATH_ITEMS: Final = 128
MAX_PATH_CHARS: Final = 512
MAX_DEPENDENCIES: Final = 256
MAX_RESOURCE_CLAIMS: Final = 128
MAX_RESOURCE_CHARS: Final = 256
MAX_STATE_REFERENCE_CHARS: Final = 256
MAX_WORKSPACE_CHARS: Final = 4096
MAX_SEQUENCE: Final = 2**63 - 1
MAX_VALIDATION_ERRORS: Final = 64
MAX_VALIDATION_MESSAGE_CHARS: Final = 512
MAX_VALIDATION_TOTAL_CHARS: Final = 16_384
MAX_KNOWN_REFERENCES: Final = 256

_UNSAFE_RANGES: Final = ((0x00, 0x1F), (0x7F, 0x9F), (0xD800, 0xDFFF))
_SECRET_LIKE: Final = re.compile(
    r"(?i)(?:api[_-]?key|secret(?:[_-]?access)?|token|password|authorization|bearer)\s*[:=]\s*\S+"
    r"|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
    r"|\b(?:sk|rk|ghp|github_pat|xox[baprs])-[A-Za-z0-9_-]{12,}\b"
)
_GIT_OBJECT_ID: Final = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_TREE_DIGEST: Final = re.compile(r"[0-9a-f]{64}\Z")
_TASK_FIELDS: Final = frozenset(
    {
        "task_id",
        "title",
        "context",
        "goal",
        "acceptance",
        "allowed_paths",
        "do_not_modify",
        "dependencies",
        "verification",
        "escalation_node",
        "kind",
        "lane",
        "resource_claims",
    }
)
_STATE_FIELDS: Final = frozenset(
    {
        "version",
        "team_id",
        "workspace",
        "sequence",
        "task_id",
        "attempt_id",
        "dispatch_id",
        "worker_node",
        "reviewer_node",
        "review_round",
        "target_head",
        "target_tree_digest",
        "claim_ref",
        "receipt_ref",
        "phase",
    }
)


class TaskKind(str, Enum):
    """Closed set of task meanings understood by the policy layer."""

    IMPLEMENTATION = "implementation"
    SMALL_CHANGE = "small-change"
    RESEARCH = "research"


class TaskLane(str, Enum):
    """Closed set of explicitly selected routing lanes."""

    NORMAL = "normal"
    EXPRESS = "express"
    RESEARCH = "research"


class TaskPhase(str, Enum):
    """The version-4 policy phases; transition legality is a later concern."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    WORKER_DONE = "worker_done"
    REVIEW_PENDING = "review_pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    ASK_USER = "ask_user"
    VERIFICATION_FAILED = "verification_failed"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One deterministic validation finding."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Immutable result for a task collection validation."""

    valid: bool
    errors: tuple[ValidationIssue, ...]


def _stable_issues(issues: Iterable[ValidationIssue]) -> tuple[ValidationIssue, ...]:
    """Deduplicate and bound diagnostics before exposing them to a caller."""

    unique = sorted(set(issues), key=lambda issue: (issue.code, issue.message))
    bounded: list[ValidationIssue] = []
    total = 0
    truncated = False
    marker_size = (
        len("diagnostic-limit") + len("validation diagnostics were truncated") + 2
    )
    for issue in unique:
        message = issue.message
        if len(message) > MAX_VALIDATION_MESSAGE_CHARS:
            message = message[: MAX_VALIDATION_MESSAGE_CHARS - len("...")] + "..."
        candidate = ValidationIssue(issue.code, message)
        size = len(candidate.code) + len(candidate.message) + 2
        if (
            len(bounded) >= MAX_VALIDATION_ERRORS - 1
            or total + size + marker_size > MAX_VALIDATION_TOTAL_CHARS
        ):
            truncated = True
            break
        bounded.append(candidate)
        total += size
    if truncated:
        bounded.append(
            ValidationIssue("diagnostic-limit", "validation diagnostics were truncated")
        )
    return tuple(bounded)


class TaskPolicyValidationError(ValueError):
    """Raised when an untrusted task or state value violates the contract."""

    def __init__(
        self,
        issues: tuple[ValidationIssue, ...] | str,
        *,
        code: str = "invalid-task-policy",
    ) -> None:
        resolved: tuple[ValidationIssue, ...]
        if isinstance(issues, str):
            resolved = _stable_issues((ValidationIssue(code, issues),))
        else:
            resolved = _stable_issues(issues)
        self.issues = resolved
        self.code = resolved[0].code if resolved else code
        message = "; ".join(f"{item.code}: {item.message}" for item in resolved)
        super().__init__(message or "task policy is invalid")


class StateConflictError(TaskPolicyValidationError):
    """Raised when a typed update was prepared from a stale sequence."""


class TaskPolicyStatePort(Protocol):
    """Minimal future store seam; implementations own persistence and CAS."""

    def update(self, update: ExpectedSequenceUpdate) -> TaskPolicyStateV4: ...


def _issue(code: str, message: str) -> TaskPolicyValidationError:
    return TaskPolicyValidationError((ValidationIssue(code, message),))


def _safe_text(value: object, context: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise _issue("invalid-type", f"{context} must be a string")
    if not value or not value.strip():
        raise _issue("empty-value", f"{context} must not be empty")
    if len(value) > maximum:
        raise _issue(
            "value-too-long",
            f"{context} exceeds maximum of {maximum} characters",
        )
    if _SECRET_LIKE.search(value) is not None:
        raise _issue(
            "secret-like-value",
            f"{context} contains secret-like material",
        )
    if value != value.strip() or any(
        any(start <= ord(character) <= end for start, end in _UNSAFE_RANGES)
        or ord(character) in {0x2028, 0x2029}
        for character in value
    ):
        raise _issue(
            "unsafe-text",
            f"{context} contains unsafe whitespace or control characters",
        )
    return value


def _safe_identifier(value: object, context: str) -> str:
    return _safe_text(value, context, MAX_TASK_ID_CHARS)


def _git_object_id(value: object, context: str) -> GitObjectId:
    candidate = _safe_text(value, context, MAX_STATE_REFERENCE_CHARS)
    if _GIT_OBJECT_ID.fullmatch(candidate) is None:
        raise _issue(
            "invalid-target-head",
            f"{context} must be a lowercase 40- or 64-character Git object ID",
        )
    return GitObjectId(candidate)


def _tree_digest(value: object, context: str) -> TreeDigest:
    candidate = _safe_text(value, context, MAX_STATE_REFERENCE_CHARS)
    if _TREE_DIGEST.fullmatch(candidate) is None:
        raise _issue(
            "invalid-tree-digest",
            f"{context} must be a lowercase 64-character SHA-256 digest",
        )
    return TreeDigest(candidate)


def _tuple_of_strings(
    value: object,
    context: str,
    *,
    maximum_items: int,
    maximum_chars: int,
    non_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise _issue("invalid-type", f"{context} must be an immutable tuple")
    if non_empty and not value:
        raise _issue("empty-value", f"{context} must not be empty")
    if len(value) > maximum_items:
        raise _issue(
            "too-many-items",
            f"{context} exceeds maximum of {maximum_items} items",
        )
    return tuple(
        _safe_text(item, f"{context}[{index}]", maximum_chars)
        for index, item in enumerate(value)
    )


def _optional_ref(value: object, context: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _safe_text(value, context, maximum)


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    """A named logical resource claim; ownership is outside this module."""

    name: str

    def __post_init__(self) -> None:
        _safe_text(self.name, "resource_claim.name", MAX_RESOURCE_CHARS)


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """Immutable, backend-neutral description of one task.

    Every field is explicit.  In particular, ``lane`` and ``verification``
    are never inferred from prose, and this value contains no executable
    argv, permission, provider, or backend identity.
    """

    task_id: TaskId
    title: str
    context: str
    goal: str
    acceptance: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    do_not_modify: tuple[str, ...]
    dependencies: tuple[TaskId, ...]
    verification: VerificationProfileRef
    escalation_node: NodeId | None
    kind: TaskKind
    lane: TaskLane
    resource_claims: tuple[ResourceClaim, ...]

    def __post_init__(self) -> None:
        _safe_identifier(self.task_id, "task.task_id")
        _safe_text(self.title, "task.title", MAX_TASK_TEXT_CHARS)
        _safe_text(self.context, "task.context", MAX_TASK_TEXT_CHARS)
        _safe_text(self.goal, "task.goal", MAX_TASK_TEXT_CHARS)
        _tuple_of_strings(
            self.acceptance,
            "task.acceptance",
            maximum_items=MAX_ACCEPTANCE_ITEMS,
            maximum_chars=MAX_ACCEPTANCE_TEXT_CHARS,
            non_empty=True,
        )
        _tuple_of_strings(
            self.allowed_paths,
            "task.allowed_paths",
            maximum_items=MAX_PATH_ITEMS,
            maximum_chars=MAX_PATH_CHARS,
            non_empty=True,
        )
        _tuple_of_strings(
            self.do_not_modify,
            "task.do_not_modify",
            maximum_items=MAX_PATH_ITEMS,
            maximum_chars=MAX_PATH_CHARS,
            non_empty=False,
        )
        if not isinstance(self.dependencies, tuple):
            raise _issue("invalid-type", "task.dependencies must be an immutable tuple")
        if len(self.dependencies) > MAX_DEPENDENCIES:
            raise _issue(
                "too-many-items",
                f"task.dependencies exceeds maximum of {MAX_DEPENDENCIES} items",
            )
        for index, dependency in enumerate(self.dependencies):
            _safe_identifier(dependency, f"task.dependencies[{index}]")
        _safe_text(self.verification, "task.verification", MAX_TASK_ID_CHARS)
        if self.escalation_node is not None:
            _safe_identifier(self.escalation_node, "task.escalation_node")
        if not isinstance(self.kind, TaskKind):
            raise _issue("unknown-kind", "task.kind must be a known TaskKind")
        if not isinstance(self.lane, TaskLane):
            raise _issue("unknown-lane", "task.lane must be a known TaskLane")
        if not isinstance(self.resource_claims, tuple):
            raise _issue(
                "invalid-type", "task.resource_claims must be an immutable tuple"
            )
        if len(self.resource_claims) > MAX_RESOURCE_CLAIMS:
            raise _issue(
                "too-many-items",
                f"task.resource_claims exceeds maximum of {MAX_RESOURCE_CLAIMS} items",
            )
        if any(not isinstance(claim, ResourceClaim) for claim in self.resource_claims):
            raise _issue(
                "invalid-type", "task.resource_claims must contain ResourceClaim values"
            )


def _parse_string_array(
    value: object,
    context: str,
    *,
    maximum_items: int,
    maximum_chars: int,
    non_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _issue("invalid-type", f"{context} must be an array")
    if non_empty and not value:
        raise _issue("empty-value", f"{context} must not be empty")
    if len(value) > maximum_items:
        raise _issue(
            "too-many-items",
            f"{context} exceeds maximum of {maximum_items} items",
        )
    return tuple(
        _safe_text(item, f"{context}[{index}]", maximum_chars)
        for index, item in enumerate(value)
    )


def _parse_enum(enum_type: type[Enum], value: object, context: str, code: str) -> Enum:
    if not isinstance(value, str):
        raise _issue(code, f"{context} must be a known enum value")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise _issue(code, f"{context} is not supported") from exc


def _require_mapping_fields(
    value: Mapping[str, object], allowed: frozenset[str], context: str
) -> None:
    unknown = sorted(
        (key for key in value if not isinstance(key, str) or key not in allowed),
        key=lambda key: repr(key),
    )
    if unknown:
        raise _issue(
            "unsupported-fields",
            f"{context} has unsupported fields",
        )
    missing = sorted(allowed - {key for key in value if isinstance(key, str)})
    if missing:
        raise _issue(
            "missing-field",
            f"{context} is missing {', '.join(missing)}",
        )


def parse_task_spec(value: Mapping[str, object], *, context: str = "task") -> TaskSpec:
    """Parse one closed task mapping without inferring any field."""

    if not isinstance(value, Mapping):
        raise _issue("invalid-type", f"{context} must be a table")
    _require_mapping_fields(value, _TASK_FIELDS, context)
    acceptance = _parse_string_array(
        value["acceptance"],
        f"{context}.acceptance",
        maximum_items=MAX_ACCEPTANCE_ITEMS,
        maximum_chars=MAX_ACCEPTANCE_TEXT_CHARS,
        non_empty=True,
    )
    allowed_paths = _parse_string_array(
        value["allowed_paths"],
        f"{context}.allowed_paths",
        maximum_items=MAX_PATH_ITEMS,
        maximum_chars=MAX_PATH_CHARS,
        non_empty=True,
    )
    do_not_modify = _parse_string_array(
        value["do_not_modify"],
        f"{context}.do_not_modify",
        maximum_items=MAX_PATH_ITEMS,
        maximum_chars=MAX_PATH_CHARS,
        non_empty=False,
    )
    dependencies = _parse_string_array(
        value["dependencies"],
        f"{context}.dependencies",
        maximum_items=MAX_DEPENDENCIES,
        maximum_chars=MAX_TASK_ID_CHARS,
        non_empty=False,
    )
    resources = _parse_string_array(
        value["resource_claims"],
        f"{context}.resource_claims",
        maximum_items=MAX_RESOURCE_CLAIMS,
        maximum_chars=MAX_RESOURCE_CHARS,
        non_empty=False,
    )
    escalation = value["escalation_node"]
    if escalation is not None:
        escalation = _safe_identifier(escalation, f"{context}.escalation_node")
    verification = _safe_identifier(value["verification"], f"{context}.verification")
    return TaskSpec(
        task_id=TaskId(_safe_identifier(value["task_id"], f"{context}.task_id")),
        title=_safe_text(value["title"], f"{context}.title", MAX_TASK_TEXT_CHARS),
        context=_safe_text(value["context"], f"{context}.context", MAX_TASK_TEXT_CHARS),
        goal=_safe_text(value["goal"], f"{context}.goal", MAX_TASK_TEXT_CHARS),
        acceptance=acceptance,
        allowed_paths=allowed_paths,
        do_not_modify=do_not_modify,
        dependencies=tuple(TaskId(item) for item in dependencies),
        verification=VerificationProfileRef(verification),
        escalation_node=None if escalation is None else NodeId(escalation),
        kind=_parse_enum(TaskKind, value["kind"], f"{context}.kind", "unknown-kind"),  # type: ignore[arg-type]
        lane=_parse_enum(TaskLane, value["lane"], f"{context}.lane", "unknown-lane"),  # type: ignore[arg-type]
        resource_claims=tuple(ResourceClaim(item) for item in resources),
    )


def parse_task_specs(value: object, *, context: str = "tasks") -> tuple[TaskSpec, ...]:
    """Parse an explicit non-empty task array without supplying defaults."""

    if not isinstance(value, list):
        raise _issue("invalid-type", f"{context} must be an array")
    if not value:
        raise _issue("empty-value", f"{context} must not be empty")
    if len(value) > MAX_TASKS:
        raise _issue(
            "too-many-items", f"{context} exceeds maximum of {MAX_TASKS} items"
        )
    parsed: list[TaskSpec] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise _issue("invalid-type", f"{context}[{index}] must be a table")
        parsed.append(parse_task_spec(item, context=f"{context}[{index}]"))
    return tuple(parsed)


def _known_strings(
    values: object, context: str
) -> tuple[frozenset[str], tuple[ValidationIssue, ...]]:
    if values is None or isinstance(values, (str, bytes)):
        return frozenset(), (
            ValidationIssue("invalid-type", f"{context} must be iterable"),
        )
    try:
        iterator = iter(cast(Iterable[object], values))
    except TypeError:
        return frozenset(), (
            ValidationIssue("invalid-type", f"{context} must be iterable"),
        )
    result: set[str] = set()
    errors: list[ValidationIssue] = []
    for index in range(MAX_KNOWN_REFERENCES + 1):
        try:
            value = next(iterator)
        except StopIteration:
            break
        except (RuntimeError, TypeError):
            errors.append(ValidationIssue("invalid-type", f"{context} is not readable"))
            break
        if index == MAX_KNOWN_REFERENCES:
            errors.append(
                ValidationIssue(
                    "too-many-items",
                    f"{context} exceeds maximum of {MAX_KNOWN_REFERENCES}",
                )
            )
            break
        try:
            result.add(_safe_text(value, context, MAX_RESOURCE_CHARS))
        except TaskPolicyValidationError:
            errors.append(
                ValidationIssue("invalid-type", f"{context} contains an invalid value")
            )
    return frozenset(result), _stable_issues(errors)


def _task_key(task_id: str) -> tuple[str, str]:
    return task_id.casefold(), task_id


def _task_id_errors(tasks: Sequence[TaskSpec]) -> list[ValidationIssue]:
    by_folded: dict[str, list[str]] = {}
    for task in tasks:
        if not isinstance(task, TaskSpec):
            continue
        by_folded.setdefault(str(task.task_id).casefold(), []).append(str(task.task_id))
    errors: list[ValidationIssue] = []
    for values in by_folded.values():
        if len(values) > 1:
            errors.append(
                ValidationIssue(
                    "duplicate-task-id",
                    "task IDs are duplicated: " + ", ".join(sorted(values)),
                )
            )
    return errors


def _has_dependency_cycle(tasks: Sequence[TaskSpec]) -> bool:
    """Detect cycles while ignoring edges already reported as malformed."""

    ids = {str(task.task_id) for task in tasks}
    indegree = {task_id: 0 for task_id in ids}
    successors: dict[str, set[str]] = {task_id: set() for task_id in ids}
    for task in tasks:
        task_id = str(task.task_id)
        seen: set[str] = set()
        for dependency in task.dependencies:
            dependency_id = str(dependency)
            if (
                dependency_id.casefold() == task_id.casefold()
                or dependency_id.casefold() in seen
                or dependency_id not in ids
            ):
                continue
            seen.add(dependency_id.casefold())
            successors[dependency_id].add(task_id)
            indegree[task_id] += 1
    ready = [task_id for task_id, count in indegree.items() if count == 0]
    processed = 0
    while ready:
        task_id = ready.pop()
        processed += 1
        for successor in successors[task_id]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
    return processed != len(ids)


def validate_task_specs(
    team_id: TeamId,
    tasks: Sequence[TaskSpec],
    *,
    known_team_ids: Iterable[TeamId],
    known_node_ids: Iterable[NodeId],
    known_verification_profiles: Iterable[VerificationProfileRef],
) -> ValidationResult:
    """Validate a task set against explicit team/node/profile registries.

    No registry lookup, default lane, case-folded reference, or task-text
    inference is performed.  All returned issues are sorted by code/message.
    """

    errors: list[ValidationIssue] = []
    try:
        team_value: str | None = _safe_text(team_id, "team_id", MAX_TASK_ID_CHARS)
    except TaskPolicyValidationError as exc:
        errors.extend(exc.issues)
        team_value = None
    team_set, team_registry_errors = _known_strings(known_team_ids, "known_team_ids")
    node_set, node_registry_errors = _known_strings(known_node_ids, "known_node_ids")
    profile_set, profile_registry_errors = _known_strings(
        known_verification_profiles, "known_verification_profiles"
    )
    errors.extend(team_registry_errors)
    errors.extend(node_registry_errors)
    errors.extend(profile_registry_errors)
    if team_value is not None and team_value not in team_set:
        errors.append(ValidationIssue("unknown-team", "task team is not registered"))
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
        errors.append(ValidationIssue("invalid-type", "tasks must be a sequence"))
        return ValidationResult(False, _stable_issues(errors))
    if not tasks:
        errors.append(ValidationIssue("empty-tasks", "task set must not be empty"))
        return ValidationResult(False, _stable_issues(errors))
    if len(tasks) > MAX_TASKS:
        errors.append(
            ValidationIssue(
                "too-many-tasks", f"task set exceeds maximum of {MAX_TASKS}"
            )
        )
        return ValidationResult(False, _stable_issues(errors))
    invalid_indexes = tuple(
        index for index, task in enumerate(tasks) if not isinstance(task, TaskSpec)
    )
    if invalid_indexes:
        errors.extend(
            ValidationIssue("invalid-task", f"task at index {index} is not a TaskSpec")
            for index in invalid_indexes
        )
        return ValidationResult(False, _stable_issues(errors))
    task_groups: dict[str, list[TaskSpec]] = {}
    for task in tasks:
        task_groups.setdefault(str(task.task_id).casefold(), []).append(task)
    errors.extend(_task_id_errors(tasks))
    by_id: dict[str, TaskSpec] = {
        str(group[0].task_id): group[0]
        for group in task_groups.values()
        if len(group) == 1
    }
    for task_id in sorted(by_id, key=_task_key):
        task = by_id[task_id]
        try:
            if str(task.verification) not in profile_set:
                errors.append(
                    ValidationIssue(
                        "unknown-profile",
                        f"verification profile is not registered for task {task_id}",
                    )
                )
            if (
                task.escalation_node is not None
                and str(task.escalation_node) not in node_set
            ):
                errors.append(
                    ValidationIssue(
                        "unknown-node",
                        f"escalation node is not registered for task {task_id}",
                    )
                )
            seen_dependencies: set[str] = set()
            for dependency in task.dependencies:
                dependency_value = str(dependency)
                folded = dependency_value.casefold()
                if folded in seen_dependencies:
                    errors.append(
                        ValidationIssue(
                            "duplicate-dependency",
                            f"dependency is duplicated for task {task_id}",
                        )
                    )
                seen_dependencies.add(folded)
                if folded == task_id.casefold():
                    errors.append(
                        ValidationIssue(
                            "self-dependency",
                            f"task cannot depend on itself: {task_id}",
                        )
                    )
                elif dependency_value not in by_id:
                    errors.append(
                        ValidationIssue(
                            "unknown-dependency",
                            f"dependency is not registered for task {task_id}",
                        )
                    )
            seen_resources: set[str] = set()
            for claim in task.resource_claims:
                claim_name = claim.name.casefold()
                if claim_name in seen_resources:
                    errors.append(
                        ValidationIssue(
                            "duplicate-resource-claim",
                            f"resource claim is duplicated for task {task_id}",
                        )
                    )
                seen_resources.add(claim_name)
        except (AttributeError, TypeError):
            errors.append(
                ValidationIssue("invalid-task", f"task value is invalid: {task_id}")
            )
    # Keep independent cycle diagnostics even when another task ID is
    # ambiguous.  A duplicate must not hide a disjoint invalid subgraph.
    unambiguous_tasks = tuple(by_id.values())
    if unambiguous_tasks and _has_dependency_cycle(unambiguous_tasks):
        errors.append(ValidationIssue("cycle", "task dependency cycle is not allowed"))
    unique = _stable_issues(errors)
    return ValidationResult(not unique, unique)


def task_dependency_order(tasks: Sequence[TaskSpec]) -> tuple[TaskId, ...]:
    """Return a deterministic Kahn order, or reject an invalid dependency DAG."""

    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
        raise _issue("invalid-type", "tasks must be a sequence")
    by_id: dict[str, TaskSpec] = {}
    folded: dict[str, str] = {}
    for task in tasks:
        if not isinstance(task, TaskSpec):
            raise _issue("invalid-task", "task set contains a non-TaskSpec value")
        task_id = str(task.task_id)
        folded_id = task_id.casefold()
        if folded_id in folded:
            raise _issue("duplicate-task-id", "task ID is duplicated")
        folded[folded_id] = task_id
        by_id[task_id] = task
    if not by_id:
        raise _issue("empty-tasks", "task set must not be empty")
    indegree = {task_id: 0 for task_id in by_id}
    successors: dict[str, set[str]] = {task_id: set() for task_id in by_id}
    for task_id, task in by_id.items():
        seen: set[str] = set()
        for dependency in task.dependencies:
            dependency_id = str(dependency)
            folded_dependency = dependency_id.casefold()
            if folded_dependency in seen:
                raise _issue("duplicate-dependency", "dependency is duplicated")
            seen.add(folded_dependency)
            if folded_dependency == task_id.casefold():
                raise _issue("self-dependency", "task cannot depend on itself")
            if dependency_id not in by_id:
                raise _issue("unknown-dependency", "dependency is not registered")
            successors[dependency_id].add(task_id)
            indegree[task_id] += 1
    ready = [
        (_task_key(task_id), task_id)
        for task_id, count in indegree.items()
        if count == 0
    ]
    heapq.heapify(ready)
    order: list[TaskId] = []
    while ready:
        _sort_key, task_id = heapq.heappop(ready)
        order.append(TaskId(task_id))
        for successor in sorted(successors[task_id], key=_task_key):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(ready, (_task_key(successor), successor))
    if len(order) != len(by_id):
        raise _issue("cycle", "task dependency cycle is not allowed")
    return tuple(order)


def task_spec_to_dict(task: TaskSpec) -> dict[str, object]:
    """Return a JSON-safe representation of one TaskSpec."""

    if not isinstance(task, TaskSpec):
        raise _issue("invalid-task", "value is not a TaskSpec")
    return {
        "acceptance": list(task.acceptance),
        "allowed_paths": list(task.allowed_paths),
        "context": task.context,
        "dependencies": [
            str(item) for item in sorted(task.dependencies, key=_task_key)
        ],
        "do_not_modify": list(task.do_not_modify),
        "escalation_node": (
            None if task.escalation_node is None else str(task.escalation_node)
        ),
        "goal": task.goal,
        "kind": task.kind.value,
        "lane": task.lane.value,
        "resource_claims": [
            claim.name
            for claim in sorted(
                task.resource_claims,
                key=lambda claim: (claim.name.casefold(), claim.name),
            )
        ],
        "task_id": str(task.task_id),
        "title": task.title,
        "verification": str(task.verification),
    }


def canonical_task_json(
    team_id: TeamId,
    tasks: Sequence[TaskSpec],
    *,
    known_team_ids: Iterable[TeamId],
    known_node_ids: Iterable[NodeId],
    known_verification_profiles: Iterable[VerificationProfileRef],
) -> str:
    """Serialize validated task policy data with stable ordering and UTF-8."""

    result = validate_task_specs(
        team_id,
        tasks,
        known_team_ids=known_team_ids,
        known_node_ids=known_node_ids,
        known_verification_profiles=known_verification_profiles,
    )
    if not result.valid:
        raise TaskPolicyValidationError(result.errors)
    by_id = {str(task.task_id): task for task in tasks}
    order = task_dependency_order(tasks)
    payload = {
        "dependency_order": [str(item) for item in order],
        "tasks": [
            task_spec_to_dict(by_id[str(item)]) for item in sorted(by_id, key=_task_key)
        ],
        "team_id": team_id,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _validate_sequence(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _issue("invalid-sequence", f"{context} must be an integer")
    if not 0 <= value <= MAX_SEQUENCE:
        raise _issue("invalid-sequence", f"{context} is outside the supported range")
    return value


def _workspace(value: object) -> str:
    workspace = _safe_text(value, "state.workspace", MAX_WORKSPACE_CHARS)
    path = Path(workspace)
    if (
        not path.is_absolute()
        or str(path) != workspace
        or workspace.startswith("//")
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise _issue(
            "noncanonical-workspace",
            "state.workspace must be an absolute canonical path",
        )
    if workspace != "/" and workspace.endswith("/"):
        raise _issue(
            "noncanonical-workspace",
            "state.workspace must not have a trailing separator",
        )
    return workspace


@dataclass(frozen=True, slots=True)
class TaskPolicyStateV4:
    """Immutable state observation for one task-policy record.

    This class has no completion or transition constructor.  A state read
    from a future store is data only; the store must establish mutation
    authority separately and apply the expected sequence check.
    """

    version: int
    team_id: TeamId
    workspace: WorkspaceIdentity
    sequence: int
    task_id: TaskId
    attempt_id: AttemptId | None
    dispatch_id: DispatchId | None
    worker_node: NodeId | None
    reviewer_node: NodeId | None
    review_round: int
    target_head: GitObjectId | None
    target_tree_digest: TreeDigest | None
    claim_ref: ClaimRef | None
    receipt_ref: ReceiptRef | None
    phase: TaskPhase

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version != _CURRENT_STATE_POLICY_VERSION
        ):
            raise _issue("state-version-mismatch", "state version must be integer 4")
        _safe_identifier(self.team_id, "state.team_id")
        _workspace(self.workspace)
        _validate_sequence(self.sequence, "state.sequence")
        _safe_identifier(self.task_id, "state.task_id")
        attempt = _optional_ref(
            self.attempt_id, "state.attempt_id", MAX_STATE_REFERENCE_CHARS
        )
        dispatch = _optional_ref(
            self.dispatch_id, "state.dispatch_id", MAX_STATE_REFERENCE_CHARS
        )
        if (attempt is None) != (dispatch is None):
            raise _issue(
                "identity-correlation",
                "state.attempt_id and state.dispatch_id must be paired",
            )
        if self.worker_node is not None:
            _safe_identifier(self.worker_node, "state.worker_node")
        if self.reviewer_node is not None:
            _safe_identifier(self.reviewer_node, "state.reviewer_node")
        _validate_sequence(self.review_round, "state.review_round")
        if (self.target_head is None) != (self.target_tree_digest is None):
            raise _issue(
                "identity-correlation",
                "state.target_head and state.target_tree_digest must be paired",
            )
        if self.target_head is not None:
            _git_object_id(self.target_head, "state.target_head")
            if self.target_tree_digest is None:
                raise _issue(
                    "identity-correlation",
                    "state.target_head and state.target_tree_digest must be paired",
                )
            _tree_digest(self.target_tree_digest, "state.target_tree_digest")
        _optional_ref(self.claim_ref, "state.claim_ref", MAX_STATE_REFERENCE_CHARS)
        _optional_ref(self.receipt_ref, "state.receipt_ref", MAX_STATE_REFERENCE_CHARS)
        if not isinstance(self.phase, TaskPhase):
            raise _issue("unknown-phase", "state.phase must be a known TaskPhase")


def task_state_to_dict(state: TaskPolicyStateV4) -> dict[str, object]:
    """Return all state fields, including explicit null optional values."""

    if not isinstance(state, TaskPolicyStateV4):
        raise _issue("invalid-state", "value is not a TaskPolicyStateV4")
    return {
        "attempt_id": None if state.attempt_id is None else str(state.attempt_id),
        "claim_ref": None if state.claim_ref is None else str(state.claim_ref),
        "dispatch_id": None if state.dispatch_id is None else str(state.dispatch_id),
        "phase": state.phase.value,
        "receipt_ref": None if state.receipt_ref is None else str(state.receipt_ref),
        "review_round": state.review_round,
        "reviewer_node": (
            None if state.reviewer_node is None else str(state.reviewer_node)
        ),
        "sequence": state.sequence,
        "target_head": state.target_head,
        "target_tree_digest": state.target_tree_digest,
        "task_id": str(state.task_id),
        "team_id": str(state.team_id),
        "version": state.version,
        "worker_node": None if state.worker_node is None else str(state.worker_node),
        "workspace": str(state.workspace),
    }


def canonical_task_state_json(state: TaskPolicyStateV4) -> str:
    """Serialize one v4 state observation deterministically."""

    return (
        json.dumps(
            task_state_to_dict(state), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    )


def _optional_mapping_string(
    value: object, context: str, maximum: int = MAX_STATE_REFERENCE_CHARS
) -> str | None:
    if value is None:
        return None
    return _safe_text(value, context, maximum)


def parse_task_state(value: Mapping[str, object]) -> TaskPolicyStateV4:
    """Parse exactly one state-v4 envelope; v3 is never upgraded."""

    if not isinstance(value, Mapping):
        raise _issue("invalid-type", "state must be a table")
    if value.get("version") != _CURRENT_STATE_POLICY_VERSION:
        raise _issue("state-version-mismatch", "state version must be integer 4")
    _require_mapping_fields(value, _STATE_FIELDS, "state")
    phase = _parse_enum(TaskPhase, value["phase"], "state.phase", "unknown-phase")
    attempt = _optional_mapping_string(value["attempt_id"], "state.attempt_id")
    dispatch = _optional_mapping_string(value["dispatch_id"], "state.dispatch_id")
    worker = _optional_mapping_string(value["worker_node"], "state.worker_node")
    reviewer = _optional_mapping_string(value["reviewer_node"], "state.reviewer_node")
    target_head = (
        None
        if value["target_head"] is None
        else _git_object_id(value["target_head"], "state.target_head")
    )
    target_tree = (
        None
        if value["target_tree_digest"] is None
        else _tree_digest(value["target_tree_digest"], "state.target_tree_digest")
    )
    claim = _optional_mapping_string(value["claim_ref"], "state.claim_ref")
    receipt = _optional_mapping_string(value["receipt_ref"], "state.receipt_ref")
    return TaskPolicyStateV4(
        version=_validate_sequence(value["version"], "state.version"),
        team_id=TeamId(_safe_identifier(value["team_id"], "state.team_id")),
        workspace=WorkspaceIdentity(_workspace(value["workspace"])),
        sequence=_validate_sequence(value["sequence"], "state.sequence"),
        task_id=TaskId(_safe_identifier(value["task_id"], "state.task_id")),
        attempt_id=None if attempt is None else AttemptId(attempt),
        dispatch_id=None if dispatch is None else DispatchId(dispatch),
        worker_node=None
        if worker is None
        else NodeId(_safe_identifier(worker, "state.worker_node")),
        reviewer_node=None
        if reviewer is None
        else NodeId(_safe_identifier(reviewer, "state.reviewer_node")),
        review_round=_validate_sequence(value["review_round"], "state.review_round"),
        target_head=target_head,
        target_tree_digest=target_tree,
        claim_ref=None if claim is None else ClaimRef(claim),
        receipt_ref=None if receipt is None else ReceiptRef(receipt),
        phase=phase,  # type: ignore[arg-type]
    )


@dataclass(frozen=True, slots=True)
class ExpectedSequenceUpdate:
    """Typed compare-and-swap intent for a future state port."""

    expected_sequence: int
    state: TaskPolicyStateV4

    def __post_init__(self) -> None:
        _validate_sequence(self.expected_sequence, "update.expected_sequence")
        if not isinstance(self.state, TaskPolicyStateV4):
            raise _issue("invalid-state", "update.state must be a TaskPolicyStateV4")


def apply_expected_sequence_update(
    current: TaskPolicyStateV4, update: ExpectedSequenceUpdate
) -> TaskPolicyStateV4:
    """Validate a typed CAS intent without persisting or mutating anything."""

    if not isinstance(current, TaskPolicyStateV4):
        raise _issue("invalid-state", "current must be a TaskPolicyStateV4")
    if not isinstance(update, ExpectedSequenceUpdate):
        raise _issue("invalid-update", "update must be an ExpectedSequenceUpdate")
    if update.expected_sequence != current.sequence:
        raise StateConflictError(
            "stale sequence does not match current state",
            code="stale-sequence",
        )
    if update.state.sequence != current.sequence + 1:
        raise _issue(
            "invalid-sequence",
            "updated state sequence must increment by exactly one",
        )
    current_identity = (
        current.version,
        current.team_id,
        current.workspace,
        current.task_id,
    )
    update_identity = (
        update.state.version,
        update.state.team_id,
        update.state.workspace,
        update.state.task_id,
    )
    if update_identity != current_identity:
        raise _issue(
            "identity-mismatch",
            "updated state cannot change team, workspace, or task identity",
        )
    return update.state
