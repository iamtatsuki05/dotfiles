"""Pure, immutable task specification values for the public task boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn, cast

_TASK_FIELDS = frozenset(
    {
        "task_id",
        "objective",
        "acceptance_criteria",
        "allowed_paths",
        "forbidden_paths",
        "dependencies",
        "verification",
        "evidence_requirements",
        "consultation_conditions",
    }
)
_VERIFICATION_FIELDS = frozenset({"name", "argv", "timeout_seconds"})
_SLUG = re.compile(r"[a-z](?:[a-z0-9-]*[a-z0-9])?\Z")
_GLOB_CHARACTERS = frozenset("*?[]{}")


class TaskSpecValidationError(ValueError):
    """Raised when a task specification is malformed or unsafe."""


def _fail(field: str, message: str) -> NoReturn:
    raise TaskSpecValidationError(f"{field} {message}")


def _text(value: object, field: str, *, allow_whitespace_only: bool = False) -> str:
    if not isinstance(value, str) or not value:
        _fail(field, "must be a non-empty string")
    if not allow_whitespace_only and not value.strip():
        _fail(field, "must not be whitespace-only")
    if "\x00" in value:
        _fail(field, "must not contain NUL")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _fail(field, "must not contain lone surrogate code points")
    return value


def _slug(value: object, field: str) -> str:
    text = _text(value, field)
    if _SLUG.fullmatch(text) is None:
        _fail(field, "must be a lowercase slug")
    return text


def _direct_strings(
    value: object,
    field: str,
    *,
    allow_empty: bool,
    allow_whitespace_only: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        _fail(field, "must be an immutable tuple")
    if not allow_empty and not value:
        _fail(field, "must not be empty")
    return tuple(
        _text(
            item,
            f"{field}[{index}]",
            allow_whitespace_only=allow_whitespace_only,
        )
        for index, item in enumerate(value)
    )


def _mapping(value: object, field: str, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _fail(field, "must be an object")
    items = tuple(value.items())
    keys = tuple(key for key, _item in items)
    if any(not isinstance(key, str) for key in keys):
        _fail(field, "keys must be strings")
    string_keys = cast(tuple[str, ...], keys)
    if len(set(string_keys)) != len(string_keys):
        _fail(field, "contains duplicate keys")
    unknown = set(string_keys) - fields
    missing = fields - set(string_keys)
    if unknown:
        _fail(field, f"contains unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        _fail(field, f"is missing keys: {', '.join(sorted(missing))}")
    return {key: item for key, item in items}


def _input_strings(
    value: object,
    field: str,
    *,
    allow_empty: bool,
    allow_whitespace_only: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        _fail(field, "must be an array")
    if not allow_empty and not value:
        _fail(field, "must not be empty")
    return tuple(
        _text(
            item,
            f"{field}[{index}]",
            allow_whitespace_only=allow_whitespace_only,
        )
        for index, item in enumerate(value)
    )


def _path(value: object, field: str) -> str:
    text = _text(value, field)
    if "\\" in text or text.startswith(("/", "~")):
        _fail(field, "must be a workspace-relative POSIX path")
    if any(character in _GLOB_CHARACTERS for character in text):
        _fail(field, "must not contain glob characters")
    directory = text.endswith("/")
    body = text[:-1] if directory else text
    parts = body.split("/")
    if not body or any(part in {"", ".", ".."} for part in parts):
        _fail(field, "must not escape or ambiguously identify the workspace")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        _fail(field, "must not contain control characters")
    return text


def _direct_paths(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        _fail(field, "must be an immutable tuple")
    return tuple(_path(item, f"{field}[{index}]") for index, item in enumerate(value))


def _reject_duplicates(values: tuple[str, ...], field: str) -> None:
    if len(set(values)) != len(values):
        _fail(field, "must not contain duplicate values")


@dataclass(frozen=True, slots=True)
class VerificationSpec:
    """One named fixed-argv verification command."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: int

    def __post_init__(self) -> None:
        _slug(self.name, "verification.name")
        argv = _direct_strings(
            self.argv,
            "verification.argv",
            allow_empty=False,
            allow_whitespace_only=True,
        )
        if any(
            any(ord(character) < 0x20 or ord(character) == 0x7F for character in item)
            for item in argv
        ):
            _fail("verification.argv", "must not contain control characters")
        if (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= 900
        ):
            _fail("verification.timeout_seconds", "must be an integer from 1 to 900")

    @classmethod
    def from_dict(cls, value: object) -> VerificationSpec:
        raw = _mapping(value, "verification", _VERIFICATION_FIELDS)
        argv = _input_strings(
            raw["argv"],
            "verification.argv",
            allow_empty=False,
            allow_whitespace_only=True,
        )
        timeout_seconds = raw["timeout_seconds"]
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            _fail("verification.timeout_seconds", "must be an integer")
        return cls(
            name=_slug(raw["name"], "verification.name"),
            argv=argv,
            timeout_seconds=timeout_seconds,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """Immutable policy data carried by one public task dispatch."""

    task_id: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    dependencies: tuple[str, ...]
    verification: tuple[VerificationSpec, ...]
    evidence_requirements: tuple[str, ...]
    consultation_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        _slug(self.task_id, "task_id")
        _text(self.objective, "objective")
        _direct_strings(
            self.acceptance_criteria, "acceptance_criteria", allow_empty=False
        )
        allowed_paths = _direct_paths(self.allowed_paths, "allowed_paths")
        forbidden_paths = _direct_paths(self.forbidden_paths, "forbidden_paths")
        _reject_duplicates(allowed_paths, "allowed_paths")
        _reject_duplicates(forbidden_paths, "forbidden_paths")
        dependencies = _direct_strings(
            self.dependencies, "dependencies", allow_empty=True
        )
        _reject_duplicates(dependencies, "dependencies")
        for index, dependency in enumerate(dependencies):
            _slug(dependency, f"dependencies[{index}]")
        if self.task_id in dependencies:
            _fail("dependencies", "must not contain task_id itself")
        if not isinstance(self.verification, tuple) or not self.verification:
            _fail("verification", "must be a non-empty immutable tuple")
        if any(not isinstance(item, VerificationSpec) for item in self.verification):
            _fail("verification", "must contain only VerificationSpec values")
        verification_names = tuple(item.name for item in self.verification)
        _reject_duplicates(verification_names, "verification")
        _direct_strings(
            self.evidence_requirements,
            "evidence_requirements",
            allow_empty=False,
        )
        _direct_strings(
            self.consultation_conditions,
            "consultation_conditions",
            allow_empty=True,
        )

    @classmethod
    def from_dict(cls, value: object) -> TaskSpec:
        raw = _mapping(value, "task", _TASK_FIELDS)
        verification_value = raw["verification"]
        if not isinstance(verification_value, (list, tuple)):
            _fail("verification", "must be an array")
        verification = tuple(
            VerificationSpec.from_dict(item) for item in verification_value
        )
        return cls(
            task_id=_slug(raw["task_id"], "task_id"),
            objective=_text(raw["objective"], "objective"),
            acceptance_criteria=_input_strings(
                raw["acceptance_criteria"],
                "acceptance_criteria",
                allow_empty=False,
            ),
            allowed_paths=_input_paths(raw["allowed_paths"], "allowed_paths"),
            forbidden_paths=_input_paths(raw["forbidden_paths"], "forbidden_paths"),
            dependencies=_input_slugs(raw["dependencies"], "dependencies"),
            verification=verification,
            evidence_requirements=_input_strings(
                raw["evidence_requirements"],
                "evidence_requirements",
                allow_empty=False,
            ),
            consultation_conditions=_input_strings(
                raw["consultation_conditions"],
                "consultation_conditions",
                allow_empty=True,
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "allowed_paths": list(self.allowed_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "dependencies": list(self.dependencies),
            "verification": [item.as_dict() for item in self.verification],
            "evidence_requirements": list(self.evidence_requirements),
            "consultation_conditions": list(self.consultation_conditions),
        }


def parse_task_specs(value: object) -> tuple[TaskSpec, ...]:
    """Parse one explicit task catalog and validate its dependency graph."""

    if not isinstance(value, (list, tuple)):
        _fail("tasks", "must be an array of serialized task objects")
    parsed: list[TaskSpec] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _fail(f"tasks[{index}]", "must be a serialized task object")
        parsed.append(TaskSpec.from_dict(item))

    by_id: dict[str, TaskSpec] = {}
    for item in parsed:
        if item.task_id in by_id:
            _fail("tasks", f"contains duplicate task_id: {item.task_id}")
        by_id[item.task_id] = item
    for item in parsed:
        for dependency in item.dependencies:
            if dependency not in by_id:
                _fail(
                    f"tasks.{item.task_id}.dependencies",
                    f"references undeclared task: {dependency}",
                )

    indegree = {task_id: 0 for task_id in by_id}
    successors: dict[str, list[str]] = {task_id: [] for task_id in by_id}
    for item in parsed:
        for dependency in item.dependencies:
            indegree[item.task_id] += 1
            successors[dependency].append(item.task_id)
    ready = [item.task_id for item in parsed if indegree[item.task_id] == 0]
    visited = 0
    while ready:
        current = ready.pop(0)
        visited += 1
        for successor in successors[current]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
    if visited != len(parsed):
        _fail("tasks.dependencies", "contains a dependency cycle")
    return tuple(parsed)


def _input_paths(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        _fail(field, "must be an array")
    return tuple(_path(item, f"{field}[{index}]") for index, item in enumerate(value))


def _input_slugs(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        _fail(field, "must be an array")
    return tuple(_slug(item, f"{field}[{index}]") for index, item in enumerate(value))


def task_schema() -> dict[str, object]:
    """Return the JSON schema exposed by the public MCP task boundary."""

    text_schema = {"type": "string", "minLength": 1, "pattern": r".*\S.*"}
    text_array = {"type": "array", "items": text_schema}
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "pattern": r"^[a-z](?:[a-z0-9-]*[a-z0-9])?$"},
            "objective": text_schema,
            "acceptance_criteria": {**text_array, "minItems": 1},
            "allowed_paths": {**text_array},
            "forbidden_paths": {**text_array},
            "dependencies": string_array,
            "verification": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "pattern": r"^[a-z](?:[a-z0-9-]*[a-z0-9])?$",
                        },
                        "argv": {**string_array, "minItems": 1},
                        "timeout_seconds": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 900,
                        },
                    },
                    "required": ["name", "argv", "timeout_seconds"],
                    "additionalProperties": False,
                },
            },
            "evidence_requirements": {**text_array, "minItems": 1},
            "consultation_conditions": text_array,
        },
        "required": sorted(_TASK_FIELDS),
        "additionalProperties": False,
    }
