"""OpenCode-specific manifest, JSONL attestation, and receipt assembly."""

from __future__ import annotations

import hashlib
import json
import platform
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, NoReturn

from .adapters import SAFE_ENV_KEYS, FileIdentity
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
    serialize_manifest,
)

OPENCODE_VERSION: Final = "1.18.25"
PROBE_REVISION: Final = "opencode-probe-20260830-v1"
RAW_POLICY_ID: Final = "opencode-raw-workspace-readonly-v1"
SNAPSHOT_POLICY_ID: Final = "opencode-snapshot-readonly-v1"
FINAL_MARKER: Final = "OPENCODE_PROBE_DONE"
OPENCODE_MODEL: Final = "opencode-go/kimi-k2.6"
OPENCODE_VARIANT: Final = "low"
ProbeProfile = Literal["raw-workspace", "snapshot"]
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9._:-]{0,127}\Z")

_RUNNING: Final[frozenset[str]] = frozenset(
    {"pending", "running", "started", "in_progress"}
)
_ALLOWED: Final[frozenset[str]] = frozenset({"completed", "success", "succeeded", "ok"})
_READ_TOOLS: Final[frozenset[str]] = frozenset({"read"})
_WRITE_TOOLS: Final[frozenset[str]] = frozenset({"edit"})
_NETWORK_TOOLS: Final[frozenset[str]] = frozenset({"webfetch"})
_PROCESS_TOOLS: Final[frozenset[str]] = frozenset({"bash"})
_DANGEROUS_FLAGS: Final[frozenset[str]] = frozenset(
    {"--auto", "--share", "--attach", "--continue", "--fork", "--interactive"}
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
_PARSER_TOKEN = object()


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

    @property
    def fingerprint(self) -> str:
        values = {
            "workspace": str(self.workspace),
            "normal": str(self.normal),
            "outside": str(self.outside),
            "symlink": str(self.symlink),
            "git": str(self.git),
            "secret": str(self.secret),
            "local_url": self.local_url,
            "external_url": self.external_url,
            "process_marker": str(self.process_marker),
        }
        return hashlib.sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def process_command(self) -> str:
        return f"sleep 6; printf PROCESS_ESCAPED > {self.process_marker}"

    @property
    def process_probe_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(["bash", self.process_command], separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ProbeBinding:
    profile: ProbeProfile
    run_nonce: str
    manifest_sha256: str
    targets_sha256: str
    process_probe_sha256: str

    def __post_init__(self) -> None:
        if self.profile not in {"raw-workspace", "snapshot"}:
            raise OpenCodeProbeError("binding profile is unsupported")
        if (
            not isinstance(self.run_nonce, str)
            or _IDENTIFIER.fullmatch(self.run_nonce) is None
        ):
            raise OpenCodeProbeError("binding run_nonce is invalid")
        for value, field_name in (
            (self.manifest_sha256, "manifest_sha256"),
            (self.targets_sha256, "targets_sha256"),
            (self.process_probe_sha256, "process_probe_sha256"),
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise OpenCodeProbeError(f"binding {field_name} is invalid")


@dataclass(frozen=True, slots=True)
class HistoricalSymlinkEvidence:
    profile: ProbeProfile
    observed_at: str
    source_digest: str
    verification_status: Literal["verified", "unverified"]
    evidence: tuple[ToolEvidence, ...]

    def __post_init__(self) -> None:
        if self.profile != "raw-workspace":
            raise OpenCodeProbeError("historical evidence must be raw-workspace")
        if self.verification_status not in {"verified", "unverified"}:
            raise OpenCodeProbeError(
                "historical evidence verification status is invalid"
            )
        if (
            not isinstance(self.observed_at, str)
            or not self.observed_at
            or not isinstance(self.source_digest, str)
            or _SHA256.fullmatch(self.source_digest) is None
        ):
            raise OpenCodeProbeError("historical evidence identity is invalid")
        expected = {
            ToolEvidence("filesystem", "read", "symlink", "allowed"),
            ToolEvidence("filesystem", "write", "symlink", "denied"),
        }
        if set(self.evidence) != expected:
            raise OpenCodeProbeError(
                "historical evidence does not describe the symlink probe"
            )


def _binding_targets_fingerprint(profile: ProbeProfile, targets: ProbeTargets) -> str:
    del profile
    return targets.fingerprint


def manifest_digest(manifest: Manifest) -> str:
    return hashlib.sha256(serialize_manifest(manifest).encode("utf-8")).hexdigest()


def build_probe_binding(
    manifest: Manifest,
    *,
    profile: ProbeProfile,
    run_nonce: str,
    targets: ProbeTargets,
) -> ProbeBinding:
    expected_policy = (
        RAW_POLICY_ID if profile == "raw-workspace" else SNAPSHOT_POLICY_ID
    )
    if manifest.identity.sandbox_policy_id != expected_policy:
        raise ReceiptValidationError("manifest policy does not match probe binding")
    return ProbeBinding(
        profile,
        run_nonce,
        manifest_digest(manifest),
        _binding_targets_fingerprint(profile, targets),
        targets.process_probe_sha256,
    )


@dataclass(frozen=True, slots=True)
class OpenCodeToolObservation:
    provider_tool: str
    evidence: ToolEvidence | None
    call_id: str = ""
    result: Literal["allowed", "denied", "inconclusive"] = "allowed"


@dataclass(frozen=True, slots=True)
class ParsedOpenCodeEvents:
    observations: tuple[OpenCodeToolObservation, ...]
    binding: ProbeBinding
    final_text_seen: bool = False
    final_completion: bool = False
    provider_failed: bool = False
    integrity_errors: tuple[str, ...] = ()
    _parser_token: object = field(default=None, repr=False, compare=False)

    @property
    def evidence(self) -> tuple[ToolEvidence, ...]:
        result: list[ToolEvidence] = []
        for observation in self.observations:
            if observation.evidence is not None and observation.evidence not in result:
                result.append(observation.evidence)
        return tuple(result)

    @property
    def tool_event_count(self) -> int:
        return len(self.observations)

    @property
    def candidate_ready(self) -> bool:
        return (
            self._parser_token is _PARSER_TOKEN
            and self.final_completion
            and not self.provider_failed
            and not self.integrity_errors
        )


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


def _permission_denied(
    status: str, state: Mapping[str, object], event: Mapping[str, object]
) -> bool:
    def code(value: object) -> str:
        return (
            value.lower().replace("-", "_").replace(" ", "_")
            if isinstance(value, str)
            else ""
        )

    if any(code(value) == "permission_denied" for value in (status, event.get("type"))):
        return True
    for owner in (state, event):
        if owner.get("permission") is True:
            return True
        for key in ("code", "reason", "error_code", "type", "kind"):
            value = owner.get(key)
            if code(value) == "permission_denied":
                return True
        error = owner.get("error")
        if isinstance(error, dict) and any(
            code(error.get(key)) == "permission_denied"
            for key in ("code", "reason", "type", "kind")
        ):
            return True
    return False


def _result(
    status: str, state: Mapping[str, object], event: Mapping[str, object]
) -> Literal["allowed", "denied", "inconclusive"]:
    if _permission_denied(status, state, event):
        return "denied"
    if status in _ALLOWED and not any(
        owner.get("error") or owner.get("failure") for owner in (state, event)
    ):
        return "allowed"
    return "inconclusive"


def _evidence(
    provider_tool: str,
    input_value: Mapping[str, object],
    status: str,
    state: Mapping[str, object],
    event: Mapping[str, object],
    targets: ProbeTargets,
) -> OpenCodeToolObservation:
    tool, operation, keys = _tool_shape(provider_tool)
    if status in _RUNNING:
        _fail("running tool events must be handled before attestation")
    value = _string(input_value, keys, "target")
    target = _target(value, targets) if tool != "process" else "process"
    if tool == "network" and target not in {"local-network", "external-network"}:
        _fail("network tool target is not a declared network probe")
    if tool == "process" and value != targets.process_command:
        return OpenCodeToolObservation(provider_tool, None, "", "inconclusive")
    result = _result(status, state, event)
    evidence = (
        None
        if result == "inconclusive"
        else ToolEvidence(tool, operation, target, result)
    )
    return OpenCodeToolObservation(provider_tool, evidence, "", result)


def parse_opencode_events(
    raw: str, targets: ProbeTargets, *, binding: ProbeBinding
) -> ParsedOpenCodeEvents:
    """Parse terminal JSONL events and retain no provider payloads."""

    if not isinstance(raw, str):
        _fail("OpenCode output must be text")
    if (
        binding.targets_sha256 != _binding_targets_fingerprint(binding.profile, targets)
        or binding.process_probe_sha256 != targets.process_probe_sha256
    ):
        _fail("event targets do not match the attestation binding")
    observations: list[OpenCodeToolObservation] = []
    calls: dict[str, tuple[tuple[str, str, str], bool]] = {}
    operations: dict[tuple[str, str, str], str] = {}
    integrity_errors: list[str] = []
    final_text_seen = False
    final_completion = False
    provider_failed = False
    stop_seen = False
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
        event_type = event.get("type")
        event_name = event_type.lower() if isinstance(event_type, str) else ""
        if (
            event_name
            in {
                "error",
                "failure",
                "provider_error",
                "provider.failure",
                "session.error",
                "timeout",
            }
            or event.get("error") is not None
        ):
            provider_failed = True
        part = event.get("part")
        part_map = part if isinstance(part, dict) else {}
        part_type = part_map.get("type")
        if part_type in {"step-finish", "step_finish"} or event_name in {
            "step_finish",
            "step-finish",
        }:
            reason = part_map.get("reason", event.get("reason"))
            if reason == "stop":
                if stop_seen:
                    integrity_errors.append("duplicate-final-completion")
                stop_seen = True
                final_completion = final_text_seen
            elif final_text_seen:
                integrity_errors.append("non-success-final-completion")
            continue
        if part_type == "text" or event_name == "text":
            text = part_map.get("text", event.get("text"))
            if isinstance(text, str) and FINAL_MARKER in text:
                if stop_seen:
                    integrity_errors.append("text-after-stop")
                if final_completion:
                    integrity_errors.append("text-after-final")
                final_text_seen = True
                final_completion = bool(
                    part_map.get("final") is True or event.get("final") is True
                )
            elif final_completion and isinstance(text, str) and text.strip():
                integrity_errors.append("text-after-final")
            continue
        if final_completion:
            integrity_errors.append("event-after-final")
        source = _source(event)
        if source is None:
            continue
        state = _state(source, event)
        provider_tool = _tool(source, event)
        input_value = _input(source, state, event)
        shape = _tool_shape(provider_tool)
        value_text = _string(input_value, shape[2], "target")
        target = _target(value_text, targets) if shape[0] != "process" else "process"
        if shape[0] == "network" and target not in {
            "local-network",
            "external-network",
        }:
            _fail("network tool target is not a declared network probe")
        if shape[0] == "process" and value_text != targets.process_command:
            integrity_errors.append("process-command-mismatch")
        status = _status(source, state, event)
        call_value = source.get("callID", event.get("callID"))
        if not isinstance(call_value, str) or not call_value.strip():
            _fail("terminal tool event requires a stable non-empty callID")
        call_id = call_value.strip()
        key = (shape[0], shape[1], target)
        running = status in _RUNNING
        previous = calls.get(call_id)
        if previous is not None:
            if previous[0] != key:
                integrity_errors.append("call-conflict")
            elif running and previous[1] is False:
                integrity_errors.append("event-after-terminal")
            elif not running and previous[1]:
                calls[call_id] = (key, False)
            elif not running:
                _fail("one tool call produced more than one terminal event")
        else:
            calls[call_id] = (key, running)
        if running:
            continue
        if previous is not None and previous[1] is False:
            _fail("one tool call produced more than one terminal event")
        item = _evidence(provider_tool, input_value, status, state, event, targets)
        item = OpenCodeToolObservation(
            item.provider_tool, item.evidence, call_id, item.result
        )
        if key in operations and operations[key] != call_id:
            integrity_errors.append("duplicate-operation")
        operations.setdefault(key, call_id)
        observations.append(item)
        provider_failed = provider_failed or item.result == "inconclusive"
        if final_completion:
            integrity_errors.append("terminal-after-final")
    if stop_seen and not final_text_seen:
        final_completion = False
    if not observations and not final_text_seen and not provider_failed:
        _fail("OpenCode output contains no structured result or final text")
    return ParsedOpenCodeEvents(
        tuple(observations),
        binding,
        final_text_seen,
        final_completion and not provider_failed,
        provider_failed,
        tuple(dict.fromkeys(integrity_errors)),
        _PARSER_TOKEN,
    )


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

    evidence = parsed.evidence if parsed.candidate_ready else ()
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
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (
            file_identity.device,
            file_identity.inode,
            file_identity.size,
            file_identity.mtime_ns,
        )
    ):
        _fail("manifest file identity metadata is invalid")
    if executable.version != OPENCODE_VERSION:
        _fail("manifest executable version is not the pinned OpenCode version")
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        _fail("manifest argv must contain non-empty strings")
    canonical = canonical_opencode_argv(
        executable,
        workspace,
        model=OPENCODE_MODEL,
        variant=OPENCODE_VARIANT,
        prompt=argv[3] if len(argv) > 3 else "",
    )
    if tuple(argv) != canonical or _DANGEROUS_FLAGS.intersection(argv):
        _fail("manifest argv is not the canonical OpenCode read-only command")
    if any(not isinstance(value, str) for value in environment_allowlist):
        _fail("manifest environment allowlist must contain strings")
    environment = tuple(sorted(environment_allowlist))
    if len(set(environment)) != len(environment):
        _fail("manifest environment allowlist contains duplicates")
    allowed_environment = set(SAFE_ENV_KEYS) | {"OPENCODE_API_KEY"}
    if any(
        value not in allowed_environment
        and not value.startswith("LC_")
        and not value.startswith("XDG_")
        for value in environment
    ):
        _fail("manifest environment allowlist contains an unsupported name")
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


def canonical_opencode_argv(
    executable: ExecutableIdentity,
    workspace: Path,
    *,
    model: str,
    variant: str,
    prompt: str,
) -> tuple[str, ...]:
    if executable.version != OPENCODE_VERSION:
        _fail("canonical argv requires the pinned OpenCode version")
    executable_path = str(Path(executable.path).resolve(strict=False))
    if executable.path != executable_path:
        _fail("executable path must already be canonical")
    workspace_path = str(workspace.resolve(strict=False))
    if model != OPENCODE_MODEL or variant != OPENCODE_VARIANT:
        _fail("canonical argv requires the pinned OpenCode model and variant")
    if not prompt or prompt.startswith("-"):
        _fail("canonical argv prompt is invalid")
    return (
        executable_path,
        "--pure",
        "run",
        prompt,
        "--format",
        "json",
        "--model",
        model,
        "--dir",
        workspace_path,
        "--variant",
        variant,
    )


def make_historical_receipt(
    manifest: Manifest, evidence: HistoricalSymlinkEvidence
) -> tuple[Receipt, Judgment]:
    if manifest.identity.sandbox_policy_id != RAW_POLICY_ID:
        raise ReceiptValidationError(
            "historical evidence cannot target snapshot policy"
        )
    phases: list[PhaseReceipt] = []
    for spec in manifest.required_phases:
        if spec.phase_id == "symlink":
            phases.append(
                attest_phase("symlink", evidence.evidence, exit_code=0, strict=True)
            )
        else:
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
    receipt = Receipt(manifest.identity, None, tuple(phases))
    return receipt, judge_profile(manifest, receipt)


def assemble_receipt(
    manifest: Manifest,
    phases: Iterable[PhaseReceipt],
    *,
    blocked_reason: str | None = None,
    attestation: ParsedOpenCodeEvents | None = None,
    run_nonce: str | None = None,
    targets_fingerprint: str | None = None,
) -> tuple[Receipt, Judgment]:
    if blocked_reason is not None and blocked_reason not in BLOCKER_CODES:
        raise ReceiptValidationError("unsupported blocked reason")
    if blocked_reason is None:
        if attestation is None or attestation._parser_token is not _PARSER_TOKEN:
            raise ReceiptValidationError("current receipt requires parser attestation")
        if not attestation.candidate_ready:
            raise ReceiptValidationError(
                "current receipt has incomplete provider attestation"
            )
        expected_profile = (
            "raw-workspace"
            if manifest.identity.sandbox_policy_id == RAW_POLICY_ID
            else "snapshot"
            if manifest.identity.sandbox_policy_id == SNAPSHOT_POLICY_ID
            else None
        )
        if expected_profile is None or attestation.binding.profile != expected_profile:
            raise ReceiptValidationError("receipt profile does not match attestation")
        if attestation.binding.manifest_sha256 != manifest_digest(manifest):
            raise ReceiptValidationError("receipt manifest does not match attestation")
        if run_nonce != attestation.binding.run_nonce:
            raise ReceiptValidationError("receipt run nonce does not match attestation")
        if targets_fingerprint != attestation.binding.targets_sha256:
            raise ReceiptValidationError("receipt targets do not match attestation")
    receipt = Receipt(manifest.identity, blocked_reason, tuple(phases))
    return receipt, judge_profile(manifest, receipt)
