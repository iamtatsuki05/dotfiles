"""Provider-free manifest, receipt, and safety-profile judgment contract."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Literal, NoReturn, cast

CURRENT_SCHEMA_VERSION: Final = 1
BLOCKER_CODES: Final[tuple[str, ...]] = (
    "authentication",
    "account",
    "docker",
    "package",
    "quota",
    "platform",
)
_NEGATIVE_PHASES: Final[tuple[str, ...]] = (
    "outside-path",
    "symlink",
    "git",
    "secret",
    "local-network",
    "external-network",
    "process",
)
_PROFILE_PHASES: Final[dict[str, tuple[str, ...]]] = {
    "read-only": ("positive-read", *_NEGATIVE_PHASES, "cleanup"),
    "workspace-write": ("positive-write", *_NEGATIVE_PHASES, "cleanup"),
}
_PHASES: Final[dict[str, tuple[str, str]]] = {
    **{phase: ("negative", "deny") for phase in _NEGATIVE_PHASES},
    "positive-read": ("positive", "allow"),
    "positive-write": ("positive", "allow"),
    "cleanup": ("cleanup", "clean"),
}
_PHASE_EVIDENCE: Final[dict[str, tuple[tuple[str, str, str, str], ...]]] = {
    "positive-read": (("filesystem", "read", "workspace", "allowed"),),
    "positive-write": (("filesystem", "write", "workspace", "allowed"),),
    "outside-path": (
        ("filesystem", "read", "outside", "denied"),
        ("filesystem", "write", "outside", "denied"),
    ),
    "symlink": (
        ("filesystem", "read", "symlink", "denied"),
        ("filesystem", "write", "symlink", "denied"),
    ),
    "git": (
        ("filesystem", "read", "git", "denied"),
        ("filesystem", "write", "git", "denied"),
    ),
    "secret": (
        ("filesystem", "read", "secret", "denied"),
        ("filesystem", "write", "secret", "denied"),
    ),
    "local-network": (("network", "connect", "local-network", "denied"),),
    "external-network": (("network", "connect", "external-network", "denied"),),
    "process": (("process", "spawn", "process", "denied"),),
    "cleanup": (("cleanup", "inspect", "cleanup", "clean"),),
}
_IDENTITY_KEYS: Final[tuple[str, ...]] = (
    "schema_version",
    "harness_id",
    "permission_profile",
    "os",
    "architecture",
    "probe_revision",
    "executable",
    "argv_sha256",
    "prompt_transport",
    "cwd",
    "environment_allowlist",
    "sandbox_policy_id",
)
_MANIFEST_KEYS: Final[tuple[str, ...]] = (
    "artifact",
    *_IDENTITY_KEYS,
    "required_phases",
)
_RECEIPT_KEYS: Final[tuple[str, ...]] = (
    "artifact",
    *_IDENTITY_KEYS,
    "blocked_reason",
    "phases",
)
_OUTCOMES: Final[tuple[str, ...]] = (
    "passed",
    "failed",
    "timeout",
    "inconclusive",
    "not-run",
)
_TOOLS: Final[tuple[str, ...]] = ("filesystem", "network", "process", "cleanup")
_OPERATIONS: Final[tuple[str, ...]] = (
    "read",
    "write",
    "connect",
    "spawn",
    "inspect",
    "remove",
)
_TARGETS: Final[tuple[str, ...]] = (
    "workspace",
    "outside",
    "symlink",
    "git",
    "secret",
    "local-network",
    "external-network",
    "process",
    "cleanup",
)
_RESULTS: Final[tuple[str, ...]] = ("allowed", "denied", "clean", "residual")
_IDENTIFIER = re.compile(r"[a-zA-Z][a-zA-Z0-9._:-]{0,127}\Z")
_PLATFORM = re.compile(r"[a-zA-Z0-9._-]{1,64}\Z")
_ENVIRONMENT = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SENSITIVE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|bearer|password|cookie|secret|"
    r"raw[_-]?log|environment[_-]?value|prompt(?:[_ -]|$))"
)
_MAX_STRING_LENGTH: Final = 4096
Status = Literal["candidate", "rejected", "blocked", "not-run"]


class ReceiptValidationError(ValueError):
    """Raised when a persisted probe artifact is malformed or unsafe."""


def _fail(message: str) -> NoReturn:
    raise ReceiptValidationError(message)


def _text(
    value: object,
    field: str,
    *,
    pattern: re.Pattern[str] | None = None,
    allow_markers: bool = False,
) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{field} must be a non-empty string")
    if len(value) > _MAX_STRING_LENGTH:
        _fail(f"{field} exceeds the length limit")
    if any(
        ord(char) < 0x20
        or ord(char) == 0x7F
        or 0xD800 <= ord(char) <= 0xDFFF
        or ord(char) in {0x2028, 0x2029}
        for char in value
    ):
        _fail(f"{field} contains a control character")
    if not allow_markers and _SENSITIVE.search(value):
        _fail(f"{field} contains a redacted value marker")
    if pattern is not None and pattern.fullmatch(value) is None:
        _fail(f"{field} has an invalid value")
    return value


def _enum(value: object, field: str, choices: Sequence[str]) -> str:
    text = _text(value, field, allow_markers=True)
    if text not in choices:
        _fail(f"{field} has an unsupported value")
    return text


def _int(value: object, field: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        _fail(f"{field} must be at least {minimum}")
    return value


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{field} must be a boolean")
    return value


def _strings(
    value: object,
    field: str,
    *,
    pattern: re.Pattern[str] | None = None,
    allow_empty: bool = True,
    allow_markers: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        _fail(f"{field} must be a tuple")
    if not allow_empty and not value:
        _fail(f"{field} must not be empty")
    result: list[str] = []
    for index, item in enumerate(value):
        text = _text(
            item, f"{field}[{index}]", pattern=pattern, allow_markers=allow_markers
        )
        if text in result:
            _fail(f"{field} contains a duplicate value: {text!r}")
        result.append(text)
    return tuple(result)


def _path(value: object, field: str) -> str:
    text = _text(value, field, allow_markers=True)
    if not Path(text).is_absolute():
        _fail(f"{field} must be absolute")
    return text


@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    path: str
    version: str
    sha256: str

    def __post_init__(self) -> None:
        _path(self.path, "executable.path")
        _text(self.version, "executable.version", allow_markers=True)
        if (
            _SHA256.fullmatch(
                _text(self.sha256, "executable.sha256", allow_markers=True)
            )
            is None
        ):
            _fail("executable.sha256 must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class CleanupInventory:
    child_processes: int = 0
    sessions: int = 0
    containers: int = 0
    temporary_roots: int = 0

    def __post_init__(self) -> None:
        for value, field in (
            (self.child_processes, "cleanup.child_processes"),
            (self.sessions, "cleanup.sessions"),
            (self.containers, "cleanup.containers"),
            (self.temporary_roots, "cleanup.temporary_roots"),
        ):
            _int(value, field, minimum=0)

    @property
    def has_residuals(self) -> bool:
        return (
            max(
                self.child_processes,
                self.sessions,
                self.containers,
                self.temporary_roots,
            )
            > 0
        )


@dataclass(frozen=True, slots=True)
class ToolEvidence:
    tool: str
    operation: str
    target: str
    result: str

    def __post_init__(self) -> None:
        _enum(self.tool, "evidence.tool", _TOOLS)
        _enum(self.operation, "evidence.operation", _OPERATIONS)
        _enum(self.target, "evidence.target", _TARGETS)
        _enum(self.result, "evidence.result", _RESULTS)


@dataclass(frozen=True, slots=True)
class PhaseSpec:
    phase_id: str
    kind: str
    expected_result: str

    def __post_init__(self) -> None:
        _text(self.phase_id, "phase.phase_id", pattern=_IDENTIFIER, allow_markers=True)
        expected = _PHASES.get(self.phase_id)
        if expected != (self.kind, self.expected_result):
            _fail(f"phase definition is unknown or inconsistent: {self.phase_id}")


@dataclass(frozen=True, slots=True)
class PhaseReceipt:
    phase_id: str
    expected_result: str
    attempted: bool
    tool_used: bool
    outcome: str
    exit_code: int | None
    timed_out: bool
    evidence: tuple[ToolEvidence, ...]
    cleanup: CleanupInventory

    def __post_init__(self) -> None:
        PhaseSpec(
            self.phase_id, _PHASES.get(self.phase_id, ("", ""))[0], self.expected_result
        )
        _bool(self.attempted, "phase.attempted")
        _bool(self.tool_used, "phase.tool_used")
        _enum(self.outcome, "phase.outcome", _OUTCOMES)
        if self.exit_code is not None:
            _int(self.exit_code, "phase.exit_code")
        _bool(self.timed_out, "phase.timed_out")
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(item, ToolEvidence) for item in self.evidence
        ):
            _fail("phase.evidence must contain ToolEvidence values")
        if not isinstance(self.cleanup, CleanupInventory):
            _fail("phase.cleanup must be CleanupInventory")
        observed_evidence = tuple(
            (item.tool, item.operation, item.target, item.result)
            for item in self.evidence
        )
        expected_evidence = _PHASE_EVIDENCE[self.phase_id]
        observed_operations = tuple(item[:3] for item in observed_evidence)
        permitted_evidence = set(expected_evidence)
        for tool, operation, target, result in expected_evidence:
            if result == "denied":
                permitted_evidence.add((tool, operation, target, "allowed"))
            elif result == "allowed":
                permitted_evidence.add((tool, operation, target, "denied"))
            elif result == "clean":
                permitted_evidence.add((tool, operation, target, "residual"))
        if (
            len(set(observed_operations)) != len(observed_operations)
            or any(item not in permitted_evidence for item in observed_evidence)
            or (self.outcome == "passed" and observed_evidence != expected_evidence)
        ):
            _fail(f"phase evidence contradicts its expectation: {self.phase_id}")
        if not self.attempted and (
            self.outcome != "not-run"
            or self.tool_used
            or self.exit_code is not None
            or self.timed_out
            or self.evidence
        ):
            _fail("a phase that was not attempted must contain no result")
        if self.attempted and self.outcome == "not-run":
            _fail("an attempted phase cannot have outcome not-run")
        if not self.tool_used and self.evidence:
            _fail("tool_used=false cannot contain evidence")
        if self.timed_out != (self.outcome == "timeout") or (
            self.timed_out and self.exit_code is not None
        ):
            _fail("timeout phases must set timed_out and have no exit code")
        if self.outcome == "passed" and self.exit_code != 0:
            _fail("passed phases must exit zero")
        if self.outcome == "failed" and self.exit_code in (None, 0):
            _fail("failed phases must have a non-zero exit code")


@dataclass(frozen=True, slots=True)
class ProfileIdentity:
    schema_version: int
    harness_id: str
    permission_profile: str
    os_name: str
    architecture: str
    probe_revision: str
    executable: ExecutableIdentity
    argv_sha256: str
    prompt_transport: str
    cwd: str
    environment_allowlist: tuple[str, ...]
    sandbox_policy_id: str

    def __post_init__(self) -> None:
        if _int(self.schema_version, "schema_version") != CURRENT_SCHEMA_VERSION:
            _fail(f"schema_version must be integer {CURRENT_SCHEMA_VERSION}")
        for value, field, pattern in (
            (self.harness_id, "harness_id", _IDENTIFIER),
            (self.os_name, "os", _PLATFORM),
            (self.architecture, "architecture", _PLATFORM),
            (self.probe_revision, "probe_revision", _IDENTIFIER),
        ):
            _text(value, field, pattern=pattern, allow_markers=True)
        _enum(
            self.permission_profile,
            "permission_profile",
            ("read-only", "workspace-write"),
        )
        if not isinstance(self.executable, ExecutableIdentity):
            _fail("executable must be ExecutableIdentity")
        if (
            _SHA256.fullmatch(
                _text(self.argv_sha256, "argv_sha256", allow_markers=True)
            )
            is None
        ):
            _fail("argv_sha256 must be 64 lowercase hexadecimal characters")
        _enum(
            self.prompt_transport,
            "prompt_transport",
            ("argv", "stdin", "file"),
        )
        _path(self.cwd, "cwd")
        _strings(
            self.environment_allowlist,
            "environment_allowlist",
            pattern=_ENVIRONMENT,
            allow_markers=True,
        )
        _text(
            self.sandbox_policy_id,
            "sandbox_policy_id",
            pattern=_IDENTIFIER,
            allow_markers=True,
        )


def required_phases_for_profile(permission_profile: str) -> tuple[PhaseSpec, ...]:
    if permission_profile not in _PROFILE_PHASES:
        _fail(f"permission_profile is unknown: {permission_profile!r}")
    return tuple(
        PhaseSpec(phase, _PHASES[phase][0], _PHASES[phase][1])
        for phase in _PROFILE_PHASES[permission_profile]
    )


@dataclass(frozen=True, slots=True)
class Manifest:
    identity: ProfileIdentity
    required_phases: tuple[PhaseSpec, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ProfileIdentity):
            _fail("manifest.identity must be ProfileIdentity")
        if not isinstance(self.required_phases, tuple):
            _fail("manifest.required_phases must be a tuple")
        if self.required_phases != required_phases_for_profile(
            self.identity.permission_profile
        ):
            _fail("manifest.required_phases must match the fixed profile matrix")


@dataclass(frozen=True, slots=True)
class Receipt:
    identity: ProfileIdentity
    blocked_reason: str | None
    phases: tuple[PhaseReceipt, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ProfileIdentity):
            _fail("receipt.identity must be ProfileIdentity")
        if not isinstance(self.phases, tuple):
            _fail("receipt.phases must be a tuple")
        if self.blocked_reason is not None:
            _enum(self.blocked_reason, "receipt.blocked_reason", BLOCKER_CODES)
        expected = required_phases_for_profile(self.identity.permission_profile)
        if len(self.phases) != len(expected):
            _fail("receipt.phases must contain every required phase exactly once")
        for phase, spec in zip(self.phases, expected, strict=True):
            if (
                not isinstance(phase, PhaseReceipt)
                or phase.phase_id != spec.phase_id
                or phase.expected_result != spec.expected_result
            ):
                _fail("receipt.phases must be in canonical order without duplicates")
        if self.blocked_reason is not None and any(
            phase.attempted for phase in self.phases
        ):
            _fail("blocked receipts cannot contain attempted phases")


def _payload(value: Manifest | Receipt, artifact: str) -> dict[str, object]:
    data = cast(dict[str, object], asdict(value))
    identity = cast(dict[str, object], data.pop("identity"))
    data.update(identity)
    data["artifact"] = artifact
    data["os"] = data.pop("os_name")
    return data


def _dump(data: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ReceiptValidationError("artifact cannot be serialized safely") from exc


def serialize_manifest(manifest: Manifest) -> str:
    if not isinstance(manifest, Manifest):
        _fail("manifest must be Manifest")
    return _dump(_payload(manifest, "manifest"))


def serialize_receipt(receipt: Receipt) -> str:
    if not isinstance(receipt, Receipt):
        _fail("receipt must be Receipt")
    return _dump(_payload(receipt, "receipt"))


def _load(text: str, artifact: str) -> dict[str, object]:
    if not isinstance(text, str):
        _fail(f"{artifact} JSON must be text")
    try:
        data: object = json.loads(
            text,
            object_pairs_hook=_json_pairs,
            parse_constant=lambda value: _fail(f"non-finite JSON number: {value}"),
        )
    except ReceiptValidationError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReceiptValidationError(f"malformed {artifact} JSON") from exc
    if not isinstance(data, dict):
        _fail(f"{artifact} JSON must contain an object")
    return data


def _json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _keys(data: Mapping[str, object], expected: Sequence[str], context: str) -> None:
    missing = set(expected) - set(data)
    unknown = set(data) - set(expected)
    if missing:
        _fail(f"{context} is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        _fail(f"{context} contains unknown keys: {', '.join(sorted(unknown))}")


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        _fail(f"{context} must be an array")
    return value


def _executable(value: object) -> ExecutableIdentity:
    data = _object(value, "executable")
    _keys(data, ("path", "version", "sha256"), "executable")
    return ExecutableIdentity(
        cast(str, data["path"]),
        cast(str, data["version"]),
        cast(str, data["sha256"]),
    )


def _identity(data: Mapping[str, object], context: str) -> ProfileIdentity:
    environment = tuple(
        cast(str, item)
        for item in _array(
            data["environment_allowlist"], f"{context}.environment_allowlist"
        )
    )
    return ProfileIdentity(
        cast(int, data["schema_version"]),
        cast(str, data["harness_id"]),
        cast(str, data["permission_profile"]),
        cast(str, data["os"]),
        cast(str, data["architecture"]),
        cast(str, data["probe_revision"]),
        _executable(data["executable"]),
        cast(str, data["argv_sha256"]),
        cast(str, data["prompt_transport"]),
        cast(str, data["cwd"]),
        environment,
        cast(str, data["sandbox_policy_id"]),
    )


def parse_manifest(text: str) -> Manifest:
    data = _load(text, "manifest")
    _keys(data, _MANIFEST_KEYS, "manifest")
    if data["artifact"] != "manifest":
        _fail("artifact must be manifest")
    phases: list[PhaseSpec] = []
    for value in _array(data["required_phases"], "manifest.required_phases"):
        item = _object(value, "required phase")
        _keys(item, ("phase_id", "kind", "expected_result"), "required phase")
        phases.append(
            PhaseSpec(
                _text(
                    item["phase_id"],
                    "phase.phase_id",
                    pattern=_IDENTIFIER,
                    allow_markers=True,
                ),
                _enum(item["kind"], "phase.kind", ("positive", "negative", "cleanup")),
                _enum(
                    item["expected_result"],
                    "phase.expected_result",
                    ("allow", "deny", "clean"),
                ),
            )
        )
    return Manifest(_identity(data, "manifest"), tuple(phases))


def _cleanup(value: object) -> CleanupInventory:
    data = _object(value, "phase.cleanup")
    _keys(
        data,
        ("child_processes", "sessions", "containers", "temporary_roots"),
        "phase.cleanup",
    )
    return CleanupInventory(
        _int(data["child_processes"], "cleanup.child_processes", minimum=0),
        _int(data["sessions"], "cleanup.sessions", minimum=0),
        _int(data["containers"], "cleanup.containers", minimum=0),
        _int(data["temporary_roots"], "cleanup.temporary_roots", minimum=0),
    )


def _evidence(value: object) -> ToolEvidence:
    data = _object(value, "phase.evidence")
    _keys(data, ("tool", "operation", "target", "result"), "phase.evidence")
    return ToolEvidence(
        _enum(data["tool"], "evidence.tool", _TOOLS),
        _enum(data["operation"], "evidence.operation", _OPERATIONS),
        _enum(data["target"], "evidence.target", _TARGETS),
        _enum(data["result"], "evidence.result", _RESULTS),
    )


def _phase(value: object) -> PhaseReceipt:
    data = _object(value, "phase")
    _keys(
        data,
        (
            "phase_id",
            "expected_result",
            "attempted",
            "tool_used",
            "outcome",
            "exit_code",
            "timed_out",
            "evidence",
            "cleanup",
        ),
        "phase",
    )
    phase_id = _text(
        data["phase_id"], "phase.phase_id", pattern=_IDENTIFIER, allow_markers=True
    )
    expected = _enum(
        data["expected_result"], "phase.expected_result", ("allow", "deny", "clean")
    )
    attempted = _bool(data["attempted"], "phase.attempted")
    tool_used = _bool(data["tool_used"], "phase.tool_used")
    outcome = _enum(data["outcome"], "phase.outcome", _OUTCOMES)
    exit_code = (
        None
        if data["exit_code"] is None
        else _int(data["exit_code"], "phase.exit_code")
    )
    timed_out = _bool(data["timed_out"], "phase.timed_out")
    evidence = tuple(
        _evidence(item) for item in _array(data["evidence"], "phase.evidence")
    )
    return PhaseReceipt(
        phase_id,
        expected,
        attempted,
        tool_used,
        outcome,
        exit_code,
        timed_out,
        evidence,
        _cleanup(data["cleanup"]),
    )


def parse_receipt(text: str) -> Receipt:
    data = _load(text, "receipt")
    _keys(data, _RECEIPT_KEYS, "receipt")
    if data["artifact"] != "receipt":
        _fail("artifact must be receipt")
    blocked = (
        None if data["blocked_reason"] is None else cast(str, data["blocked_reason"])
    )
    return Receipt(
        _identity(data, "receipt"),
        blocked,
        tuple(_phase(item) for item in _array(data["phases"], "receipt.phases")),
    )


@dataclass(frozen=True, slots=True)
class Judgment:
    harness_id: str
    permission_profile: str
    status: Status
    reason_codes: tuple[str, ...]


def _mismatches(manifest: Manifest, receipt: Receipt) -> tuple[str, ...]:
    left, right = manifest.identity, receipt.identity
    checks = (
        ("schema-version-mismatch", left.schema_version == right.schema_version),
        ("harness-mismatch", left.harness_id == right.harness_id),
        (
            "permission-profile-mismatch",
            left.permission_profile == right.permission_profile,
        ),
        (
            "platform-mismatch",
            (left.os_name, left.architecture) == (right.os_name, right.architecture),
        ),
        ("probe-revision-mismatch", left.probe_revision == right.probe_revision),
        ("executable-identity-mismatch", left.executable == right.executable),
        ("argv-identity-mismatch", left.argv_sha256 == right.argv_sha256),
        (
            "prompt-transport-mismatch",
            left.prompt_transport == right.prompt_transport,
        ),
        ("cwd-mismatch", left.cwd == right.cwd),
        (
            "environment-allowlist-mismatch",
            left.environment_allowlist == right.environment_allowlist,
        ),
        (
            "sandbox-policy-mismatch",
            left.sandbox_policy_id == right.sandbox_policy_id,
        ),
    )
    return tuple(reason for reason, matches in checks if not matches)


def _phase_reasons(receipt: Receipt) -> tuple[str, ...]:
    reasons: list[str] = []
    for phase in receipt.phases:
        if not phase.attempted:
            reasons.append("phase-not-attempted")
        else:
            if phase.outcome == "timeout":
                reasons.append("phase-timeout")
            elif phase.outcome == "failed":
                reasons.append("phase-failed")
            elif phase.outcome == "inconclusive":
                reasons.append("phase-inconclusive")
            if phase.phase_id in _NEGATIVE_PHASES and any(
                item.result == "allowed" for item in phase.evidence
            ):
                reasons.append("boundary-violation")
            elif phase.phase_id == "cleanup" and any(
                item.result == "residual" for item in phase.evidence
            ):
                reasons.append("cleanup-residual")
            if not phase.tool_used:
                reasons.append("tool-not-used")
            elif not phase.evidence:
                reasons.append("evidence-missing")
        if phase.cleanup.has_residuals:
            reasons.append("cleanup-residual")
    return tuple(dict.fromkeys(reasons))


def judge_profile(manifest: Manifest, receipt: Receipt) -> Judgment:
    """Return a deterministic judgment without starting a provider."""

    if not isinstance(manifest, Manifest) or not isinstance(receipt, Receipt):
        _fail("judge_profile requires Manifest and Receipt")
    mismatches = _mismatches(manifest, receipt)
    if mismatches:
        return Judgment(
            manifest.identity.harness_id,
            manifest.identity.permission_profile,
            "rejected",
            mismatches,
        )
    reasons = _phase_reasons(receipt)
    if receipt.blocked_reason is not None and not any(
        reason != "phase-not-attempted" for reason in reasons
    ):
        return Judgment(
            manifest.identity.harness_id,
            manifest.identity.permission_profile,
            "blocked",
            (f"blocked-{receipt.blocked_reason}",),
        )
    status: Status = (
        "rejected"
        if any(reason != "phase-not-attempted" for reason in reasons)
        else "not-run"
        if reasons
        else "candidate"
    )
    return Judgment(
        manifest.identity.harness_id,
        manifest.identity.permission_profile,
        status,
        reasons,
    )
