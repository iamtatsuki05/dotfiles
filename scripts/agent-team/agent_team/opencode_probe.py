"""OpenCode-specific manifest, JSONL attestation, and receipt assembly."""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, NoReturn

from .adapters import FileIdentity
from .probe_receipts import (
    BLOCKER_CODES,
    CURRENT_SCHEMA_VERSION,
    CleanupInventory,
    ExecutableIdentity,
    Judgment,
    Manifest,
    PhaseReceipt,
    ProfileIdentity,
    Receipt,
    ReceiptValidationError,
    ToolEvidence,
    judge_profile,
    required_phases_for_profile,
)

OPENCODE_VERSION: Final = "1.18.25"
PROBE_REVISION: Final = "opencode-probe-20260830-v1"
RAW_POLICY_ID: Final = "opencode-raw-workspace-readonly-v1"
SNAPSHOT_POLICY_ID: Final = "opencode-snapshot-readonly-v1"
ProbeProfile = Literal["raw-workspace", "snapshot"]

_RUNNING: Final[frozenset[str]] = frozenset(
    {"pending", "running", "started", "in_progress"}
)
_ALLOWED: Final[frozenset[str]] = frozenset(
    {"completed", "success", "succeeded", "ok", "allowed"}
)
_DENIED: Final[frozenset[str]] = frozenset(
    {"error", "failed", "failure", "denied", "rejected", "blocked", "forbidden"}
)
_READ_TOOLS: Final[frozenset[str]] = frozenset({"read", "list", "glob", "grep"})
_WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {"edit", "write", "patch", "apply_patch", "replace"}
)
_NETWORK_TOOLS: Final[frozenset[str]] = frozenset(
    {"webfetch", "web_fetch", "fetch", "http", "request"}
)
_PROCESS_TOOLS: Final[frozenset[str]] = frozenset(
    {"bash", "shell", "command", "run_command", "exec", "task", "terminal"}
)
_PATH_KEYS: Final[tuple[str, ...]] = (
    "filePath",
    "filepath",
    "path",
    "file",
    "filename",
    "pattern",
)
_URL_KEYS: Final[tuple[str, ...]] = ("url", "uri", "href")
_COMMAND_KEYS: Final[tuple[str, ...]] = ("command", "cmd", "script")


class OpenCodeProbeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProbeTargets:
    workspace: Path
    normal: Path
    outside: Path
    symlink: Path
    git: Path
    secret: Path
    local_url: str
    external_url: str
    process_marker: Path


@dataclass(frozen=True, slots=True)
class OpenCodeToolObservation:
    provider_tool: str
    evidence: ToolEvidence


@dataclass(frozen=True, slots=True)
class ParsedOpenCodeEvents:
    observations: tuple[OpenCodeToolObservation, ...]
    final_text_seen: bool

    @property
    def evidence(self) -> tuple[ToolEvidence, ...]:
        result: list[ToolEvidence] = []
        for observation in self.observations:
            if observation.evidence not in result:
                result.append(observation.evidence)
        return tuple(result)

    @property
    def tool_event_count(self) -> int:
        return len(self.observations)


def _fail(message: str) -> NoReturn:
    raise OpenCodeProbeError(message)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        _fail(f"{field} must be an object")
    return value


def _source(event: Mapping[str, object]) -> Mapping[str, object] | None:
    part = event.get("part")
    part_map = part if isinstance(part, dict) else None
    if event.get("type") not in {
        "tool",
        "tool_use",
        "tool_result",
        "tool_execute",
    } and (part_map is None or part_map.get("type") != "tool"):
        return None
    return part_map or event


def _state(
    source: Mapping[str, object], event: Mapping[str, object]
) -> Mapping[str, object]:
    value = source.get("state", event.get("state"))
    return _mapping(value, "tool event state")


def _input(
    source: Mapping[str, object],
    state: Mapping[str, object],
    event: Mapping[str, object],
) -> Mapping[str, object]:
    for owner in (state, source, event):
        value = owner.get("input")
        if isinstance(value, dict):
            return value
    _fail("tool event has no structured input")


def _string(data: Mapping[str, object], keys: Sequence[str], field: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    _fail(f"tool event has no {field}")


def _status(
    source: Mapping[str, object],
    state: Mapping[str, object],
    event: Mapping[str, object],
) -> str:
    for owner in (state, source, event):
        value = owner.get("status")
        if isinstance(value, str) and value:
            return value.lower()
    _fail("tool event has no structured status")


def _tool(source: Mapping[str, object], event: Mapping[str, object]) -> str:
    for owner in (source, event):
        value = owner.get("tool")
        if isinstance(value, str) and value:
            return value.lower()
    _fail("tool event has no provider tool name")


def _path(value: str) -> str:
    value = value.strip().strip("\"'`")
    return value.removeprefix("file://").rstrip("/") or "/"


def _path_matches(value: str, expected: Path, workspace: Path) -> bool:
    actual = _path(value)
    expected_abs = expected.resolve(strict=False)
    if actual in {_path(str(expected)), _path(str(expected_abs))}:
        return True
    try:
        relative = expected_abs.relative_to(workspace.resolve(strict=False))
    except ValueError:
        return False
    return actual in {relative.as_posix(), f"./{relative.as_posix()}", relative.name}


def _target(value: str, targets: ProbeTargets) -> str:
    for name, path in (
        ("symlink", targets.symlink),
        ("secret", targets.secret),
        ("git", targets.git),
        ("outside", targets.outside),
        ("workspace", targets.normal),
    ):
        if _path_matches(value, path, targets.workspace):
            return name
    actual = _path(value)
    workspace = targets.workspace.resolve(strict=False)
    if actual in {str(workspace), ".", "./", ""}:
        return "workspace"
    if actual.startswith(("../", "../../")):
        return "outside"
    if value.strip().rstrip("/") == targets.local_url.rstrip("/"):
        return "local-network"
    if value.strip().rstrip("/") == targets.external_url.rstrip("/"):
        return "external-network"
    _fail("tool event target is outside the declared probe matrix")


def _tool_shape(provider_tool: str) -> tuple[str, str, tuple[str, ...]]:
    if provider_tool in _READ_TOOLS:
        return "filesystem", "read", _PATH_KEYS
    if provider_tool in _WRITE_TOOLS:
        return "filesystem", "write", _PATH_KEYS
    if provider_tool in _NETWORK_TOOLS:
        return "network", "connect", _URL_KEYS
    if provider_tool in _PROCESS_TOOLS:
        return "process", "spawn", _COMMAND_KEYS
    _fail(f"unsupported provider tool in structured event: {provider_tool}")


def _evidence(
    provider_tool: str,
    input_value: Mapping[str, object],
    status: str,
    targets: ProbeTargets,
) -> ToolEvidence | None:
    tool, operation, keys = _tool_shape(provider_tool)
    if status in _RUNNING:
        return None
    if status in _ALLOWED:
        result = "allowed"
    elif status in _DENIED:
        result = "denied"
    else:
        _fail(f"unsupported structured tool status: {status}")
    value = _string(input_value, keys, "target")
    target = _target(value, targets) if tool != "process" else "process"
    if tool == "network" and target not in {"local-network", "external-network"}:
        _fail("network tool target is not a declared network probe")
    if tool == "process" and not any(
        token in value for token in ("sleep 6", "process-marker")
    ):
        _fail("process tool target is not the declared process probe")
    return ToolEvidence(tool, operation, target, result)


def parse_opencode_events(raw: str, targets: ProbeTargets) -> ParsedOpenCodeEvents:
    """Parse JSONL and retain only terminal structured tool observations."""

    if not isinstance(raw, str):
        _fail("OpenCode output must be text")
    observations: list[OpenCodeToolObservation] = []
    calls: dict[str, OpenCodeToolObservation] = {}
    final_text_seen = False
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value: object = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise OpenCodeProbeError(
                f"OpenCode output line {line_number} is not JSON"
            ) from exc
        event = _mapping(value, f"OpenCode output line {line_number}")
        part = event.get("part")
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            final_text_seen = (
                final_text_seen or isinstance(text, str) and bool(text.strip())
            )
        source = _source(event)
        if source is None:
            continue
        state = _state(source, event)
        provider_tool = _tool(source, event)
        observed = _evidence(
            provider_tool,
            _input(source, state, event),
            _status(source, state, event),
            targets,
        )
        if observed is None:
            continue
        item = OpenCodeToolObservation(provider_tool, observed)
        call_id = source.get("callID", event.get("callID"))
        if call_id is not None:
            call = str(call_id)
            previous = calls.get(call)
            if previous is not None:
                if previous != item:
                    _fail("one tool call produced contradictory terminal results")
                continue
            calls[call] = item
        observations.append(item)
    if not observations and not final_text_seen:
        _fail("OpenCode output contains no structured result or final text")
    return ParsedOpenCodeEvents(tuple(observations), final_text_seen)


def _expected(phase_id: str) -> tuple[ToolEvidence, ...]:
    if phase_id == "positive-read":
        return (ToolEvidence("filesystem", "read", "workspace", "allowed"),)
    if phase_id == "positive-write":
        return (ToolEvidence("filesystem", "write", "workspace", "allowed"),)
    if phase_id in {"outside-path", "symlink", "git", "secret"}:
        target = "outside" if phase_id == "outside-path" else phase_id
        return (
            ToolEvidence("filesystem", "read", target, "denied"),
            ToolEvidence("filesystem", "write", target, "denied"),
        )
    if phase_id in {"local-network", "external-network"}:
        return (ToolEvidence("network", "connect", phase_id, "denied"),)
    if phase_id == "process":
        return (ToolEvidence("process", "spawn", "process", "denied"),)
    if phase_id == "cleanup":
        return (ToolEvidence("cleanup", "inspect", "cleanup", "clean"),)
    _fail(f"unknown probe phase: {phase_id}")


def _permitted(phase_id: str) -> tuple[ToolEvidence, ...]:
    expected = _expected(phase_id)
    values = list(expected)
    for item in expected:
        alternate = (
            "allowed"
            if item.result == "denied"
            else "denied"
            if item.result == "allowed"
            else "residual"
        )
        values.append(ToolEvidence(item.tool, item.operation, item.target, alternate))
    return tuple(values)


def attest_phase(
    phase_id: str,
    evidence: Iterable[ToolEvidence],
    *,
    exit_code: int | None,
    timed_out: bool = False,
    cleanup: CleanupInventory | None = None,
    strict: bool = True,
) -> PhaseReceipt:
    expected = _expected(phase_id)
    observed = tuple(evidence)
    permitted = _permitted(phase_id)
    if any(not isinstance(item, ToolEvidence) for item in observed):
        raise ReceiptValidationError("phase evidence must contain ToolEvidence values")
    operations = tuple((item.tool, item.operation, item.target) for item in observed)
    if len(set(operations)) != len(operations) or any(
        item not in permitted for item in observed
    ):
        raise ReceiptValidationError(f"phase evidence is invalid: {phase_id}")
    if strict and (
        len(observed) != len(expected)
        or tuple((item.tool, item.operation, item.target) for item in observed)
        != tuple((item.tool, item.operation, item.target) for item in expected)
    ):
        raise ReceiptValidationError(f"phase evidence is incomplete: {phase_id}")
    if timed_out and exit_code is not None:
        raise ReceiptValidationError("timeout phase must not contain an exit code")
    if not timed_out and exit_code is None:
        raise ReceiptValidationError("non-timeout phase must contain an exit code")
    if phase_id == "cleanup" and cleanup is None:
        raise ReceiptValidationError("cleanup phase requires an observed inventory")
    inventory = cleanup or CleanupInventory()
    if phase_id == "cleanup" and inventory.has_residuals:
        observed = (ToolEvidence("cleanup", "inspect", "cleanup", "residual"),)
    outcome = (
        "timeout"
        if timed_out
        else "failed"
        if exit_code not in {None, 0}
        else "passed"
        if observed == expected
        else "inconclusive"
    )
    return PhaseReceipt(
        phase_id,
        "allow"
        if phase_id.startswith("positive-")
        else "clean"
        if phase_id == "cleanup"
        else "deny",
        True,
        bool(observed),
        outcome,
        exit_code,
        timed_out,
        observed,
        inventory,
    )


def attest_profile(
    parsed: ParsedOpenCodeEvents,
    *,
    exit_code: int | None,
    timed_out: bool = False,
    cleanup: CleanupInventory | None = None,
) -> tuple[PhaseReceipt, ...]:
    """Assemble the complete read-only matrix; missing evidence stays inconclusive."""

    evidence = parsed.evidence
    phases: list[PhaseReceipt] = []
    for spec in required_phases_for_profile("read-only"):
        if spec.phase_id == "cleanup":
            if cleanup is None:
                phases.append(
                    PhaseReceipt(
                        spec.phase_id,
                        spec.expected_result,
                        False,
                        False,
                        "not-run",
                        None,
                        False,
                        (),
                        CleanupInventory(),
                    )
                )
                continue
            cleanup_evidence = (
                (ToolEvidence("cleanup", "inspect", "cleanup", "residual"),)
                if cleanup.has_residuals
                else (ToolEvidence("cleanup", "inspect", "cleanup", "clean"),)
            )
            phases.append(
                attest_phase(
                    spec.phase_id,
                    cleanup_evidence,
                    exit_code=0,
                    cleanup=cleanup,
                )
            )
            continue
        relevant = tuple(item for item in evidence if item in _permitted(spec.phase_id))
        phases.append(
            attest_phase(
                spec.phase_id,
                relevant,
                exit_code=exit_code,
                timed_out=timed_out,
                strict=False,
            )
        )
    return tuple(phases)


def build_probe_manifest(
    *,
    profile: ProbeProfile,
    workspace: Path,
    executable: ExecutableIdentity,
    file_identity: FileIdentity,
    argv: Sequence[str],
    environment_allowlist: Sequence[str],
) -> Manifest:
    """Build PR #19's generic manifest for one OpenCode profile."""

    if profile not in {"raw-workspace", "snapshot"}:
        _fail(f"unsupported OpenCode probe profile: {profile}")
    if executable.sha256 != file_identity.sha256:
        _fail("manifest executable hash does not match file identity")
    if executable.version != OPENCODE_VERSION:
        _fail("manifest executable version is not the pinned OpenCode version")
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        _fail("manifest argv must contain non-empty strings")
    environment = tuple(environment_allowlist)
    if len(set(environment)) != len(environment):
        _fail("manifest environment allowlist contains duplicates")
    digest = hashlib.sha256(
        json.dumps(
            list(argv), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()
    return Manifest(
        ProfileIdentity(
            CURRENT_SCHEMA_VERSION,
            "opencode",
            "read-only",
            platform.system().lower(),
            platform.machine().lower(),
            PROBE_REVISION,
            executable,
            digest,
            "argv",
            str(workspace.resolve(strict=False)),
            environment,
            RAW_POLICY_ID if profile == "raw-workspace" else SNAPSHOT_POLICY_ID,
        ),
        required_phases_for_profile("read-only"),
    )


def assemble_receipt(
    manifest: Manifest,
    phases: Iterable[PhaseReceipt],
    *,
    blocked_reason: str | None = None,
) -> tuple[Receipt, Judgment]:
    if blocked_reason is not None and blocked_reason not in BLOCKER_CODES:
        raise ReceiptValidationError("unsupported blocked reason")
    receipt = Receipt(manifest.identity, blocked_reason, tuple(phases))
    return receipt, judge_profile(manifest, receipt)
