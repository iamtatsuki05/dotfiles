from __future__ import annotations

import hashlib
import json
import platform
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final, Literal, NamedTuple, NoReturn, cast

from . import probe_receipts
from .adapters import (
    SAFE_ENV_KEYS,
    FileIdentity,
    ProcessResult,
    ProcessRunner,
    _identity,
    _version_probe,
    safe_environment,
)
from .probe_receipts import (
    _PHASE_EVIDENCE,
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

ANTIGRAVITY_EXECUTABLE: Final = Path("/opt/homebrew/bin/agy")
ANTIGRAVITY_VERSION: Final = "1.1.22"
ANTIGRAVITY_SHA256: Final = (
    "7b1317779085913d338bde0e9b39b72323d9083a879525f944fd469c8ecca906"
)
ANTIGRAVITY_SIGNING_IDENTITY: Final = (
    "Developer ID Application: Google LLC (EQHXZ8M8AV)"
)
ANTIGRAVITY_TEAM_ID: Final = "EQHXZ8M8AV"
PROBE_REVISION: Final = "antigravity-probe-20260830-v1"
RAW_POLICY_ID: Final = "antigravity-raw-workspace-readonly-v1"
SNAPSHOT_POLICY_ID: Final = "antigravity-snapshot-seatbelt-readonly-v1"
ProbeProfile = Literal["raw-workspace", "snapshot"]


class AntigravityProbeError(ValueError):
    pass


def _fail(message: str) -> NoReturn:
    raise AntigravityProbeError(message)


def _text(value: object, field: str) -> str:
    return probe_receipts._text(value, field, allow_markers=True)


class DeviceIdentity(NamedTuple):
    os_name: str
    architecture: str
    kernel_release: str
    os_version: str
    model_identifier: str


EXPECTED_DEVICE_IDENTITY: Final = DeviceIdentity(
    "Darwin", "arm64", "25.5.0", "26.5.2", "MacBookPro18,4"
)


class CodeSignature(NamedTuple):
    identifier: str
    team_id: str
    valid: bool


EXPECTED_SIGNATURE: Final = (
    ANTIGRAVITY_SIGNING_IDENTITY,
    ANTIGRAVITY_TEAM_ID,
    True,
)


class BinaryProvenance(NamedTuple):
    executable: ExecutableIdentity
    file_identity: FileIdentity
    signature: CodeSignature
    device_identity: DeviceIdentity


def validate_binary_provenance(
    *,
    path: Path,
    version: str,
    identity: FileIdentity,
    signature: CodeSignature,
    device_identity: DeviceIdentity,
) -> BinaryProvenance:
    if not (
        isinstance(path, Path)
        and path.is_absolute()
        and path == ANTIGRAVITY_EXECUTABLE
        and isinstance(identity, FileIdentity)
        and identity.sha256 == ANTIGRAVITY_SHA256
        and version == ANTIGRAVITY_VERSION
        and isinstance(signature, CodeSignature)
        and (signature.identifier, signature.team_id, signature.valid)
        == EXPECTED_SIGNATURE
        and isinstance(device_identity, DeviceIdentity)
        and device_identity == EXPECTED_DEVICE_IDENTITY
    ):
        _fail("agy binary provenance is not pinned")
    return BinaryProvenance(
        ExecutableIdentity(str(path), version, identity.sha256),
        identity,
        signature,
        device_identity,
    )


def _run_preflight(
    runner: ProcessRunner,
    argv: Sequence[str],
    private_root: Path,
) -> ProcessResult:
    return runner.run(
        argv,
        cwd=private_root,
        env=safe_environment(
            "antigravity", home=private_root, private_root=private_root
        ),
        timeout_seconds=15,
    )


def _checked_identity(path: Path) -> FileIdentity:
    if path.is_symlink():
        _fail("pinned agy executable must not be a symlink")
    return _identity(path)


def preflight_binary(
    *,
    private_root: Path,
    executable: Path = ANTIGRAVITY_EXECUTABLE,
    runner: ProcessRunner | None = None,
) -> BinaryProvenance:
    if executable != ANTIGRAVITY_EXECUTABLE or not isinstance(private_root, Path):
        _fail("agy preflight identity is not pinned")
    if not private_root.is_absolute():
        _fail("agy preflight private root must be absolute")
    selected = runner if runner is not None else ProcessRunner()
    identity = _checked_identity(executable)
    version = _version_probe(
        executable,
        provider="antigravity",
        private_root=private_root,
        runner=selected,
    )
    if (
        re.search(rf"(?<![0-9]){re.escape(ANTIGRAVITY_VERSION)}(?![0-9])", version)
        is None
    ):
        _fail("agy version probe did not report the pinned version")
    verified = _run_preflight(
        selected,
        ("/usr/bin/codesign", "--verify", "--strict", str(executable)),
        private_root,
    )
    if verified.returncode != 0:
        _fail("agy code signature verification failed")
    metadata = _run_preflight(
        selected,
        ("/usr/bin/codesign", "-dv", "--verbose=4", str(executable)),
        private_root,
    )
    if metadata.returncode != 0:
        _fail("agy code signature metadata probe failed")
    fields = {
        key: line.partition("=")[2].strip()
        for line in metadata.stderr.splitlines()
        for key in ("Authority", "TeamIdentifier")
        if line.startswith(f"{key}=")
    }
    if (
        fields.get("Authority") != ANTIGRAVITY_SIGNING_IDENTITY
        or fields.get("TeamIdentifier") != ANTIGRAVITY_TEAM_ID
    ):
        _fail("agy code signature metadata does not match the pinned signer")
    system, architecture = platform.system(), platform.machine()
    os_version = platform.mac_ver()[0]
    if (system, architecture) != ("Darwin", "arm64") or not os_version:
        _fail("agy probe requires the audited Darwin arm64 host")
    model = _run_preflight(
        selected, ("/usr/sbin/sysctl", "-n", "hw.model"), private_root
    )
    if model.returncode != 0 or not model.stdout.strip():
        _fail("macOS model identity probe failed")
    return validate_binary_provenance(
        path=executable,
        version=ANTIGRAVITY_VERSION,
        identity=identity,
        signature=CodeSignature(fields["Authority"], fields["TeamIdentifier"], True),
        device_identity=DeviceIdentity(
            system, architecture, platform.release(), os_version, model.stdout.strip()
        ),
    )


def _executable(value: ExecutableIdentity) -> None:
    if not isinstance(value, ExecutableIdentity) or (
        value.path,
        value.version,
        value.sha256,
    ) != (str(ANTIGRAVITY_EXECUTABLE), ANTIGRAVITY_VERSION, ANTIGRAVITY_SHA256):
        _fail("agy command must use the pinned executable identity")


def build_probe_argv(
    *,
    executable: ExecutableIdentity,
    profile: ProbeProfile,
    prompt: str,
    model: str,
    effort: Literal["low", "medium", "high"] = "low",
) -> tuple[str, ...]:
    _executable(executable)
    if not isinstance(profile, str) or profile not in {"raw-workspace", "snapshot"}:
        _fail(f"unsupported Antigravity probe profile: {profile!r}")
    checked_prompt = _text(prompt, "agy prompt")
    if not checked_prompt.strip():
        _fail("agy prompt must not be empty")
    checked_model = _text(model, "agy model")
    if not isinstance(effort, str) or effort not in {"low", "medium", "high"}:
        _fail("agy effort is invalid")
    return (
        str(ANTIGRAVITY_EXECUTABLE),
        "--print",
        checked_prompt,
        "--mode",
        "plan",
        "--sandbox",
        "--output-format",
        "stream-json",
        "--input-format",
        "text",
        "--disable-slash-commands",
        "--print-timeout",
        "90s",
        "--model",
        checked_model,
        "--effort",
        effort,
    )


def build_probe_manifest(
    *,
    profile: ProbeProfile,
    workspace: Path,
    executable: ExecutableIdentity,
    file_identity: FileIdentity,
    argv: Sequence[str],
    environment_allowlist: Sequence[str],
) -> Manifest:
    if not isinstance(profile, str) or profile not in {"raw-workspace", "snapshot"}:
        _fail(f"unsupported Antigravity probe profile: {profile!r}")
    selected = cast(ProbeProfile, profile)
    if (
        not isinstance(workspace, Path)
        or not workspace.is_absolute()
        or workspace.is_symlink()
    ):
        _fail("agy probe workspace must be a non-symlink absolute path")
    _executable(executable)
    if (
        not isinstance(file_identity, FileIdentity)
        or file_identity.sha256 != ANTIGRAVITY_SHA256
    ):
        _fail("agy manifest file identity is not pinned")
    if not isinstance(argv, Sequence):
        _fail("agy argv must be a sequence")
    values = tuple(argv)
    if len(values) != 17:
        _fail("agy argv has an unsupported route or option")
    if values != build_probe_argv(
        executable=executable,
        profile=selected,
        prompt=values[2],
        model=values[14],
        effort=cast(Literal["low", "medium", "high"], values[16]),
    ):
        _fail("agy argv has an unsupported route or option")
    names = tuple(environment_allowlist)
    if (
        any(not isinstance(name, str) for name in names)
        or len(set(names)) != len(names)
        or any(
            name not in SAFE_ENV_KEYS and not name.startswith("LC_") for name in names
        )
    ):
        _fail("agy environment allowlist is not closed")
    digest = hashlib.sha256(
        json.dumps(list(values), ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return Manifest(
        ProfileIdentity(
            CURRENT_SCHEMA_VERSION,
            "antigravity",
            "read-only",
            "darwin",
            "arm64",
            PROBE_REVISION,
            executable,
            digest,
            "argv",
            str(workspace.resolve(strict=False)),
            names,
            RAW_POLICY_ID if selected == "raw-workspace" else SNAPSHOT_POLICY_ID,
        ),
        required_phases_for_profile("read-only"),
    )


class AntigravityProbeTargets(NamedTuple):
    workspace: Path
    normal: Path
    outside: Path
    symlink: Path
    git: Path
    secret: Path
    local_url: str
    external_url: str
    process_marker: Path


class ParsedAntigravityEvents(NamedTuple):
    observations: tuple[ToolEvidence, ...]


KNOWN_RAW_OUTSIDE_READ_EVIDENCE: Final = ToolEvidence(
    "filesystem", "read", "outside", "allowed"
)
KNOWN_RAW_OUTSIDE_READ = ParsedAntigravityEvents((KNOWN_RAW_OUTSIDE_READ_EVIDENCE,))


_ALLOWED: Final[frozenset[str]] = frozenset(
    {"completed", "success", "succeeded", "ok", "allowed"}
)
_DENIED: Final[frozenset[str]] = frozenset(
    {"error", "failed", "failure", "denied", "rejected", "blocked", "forbidden"}
)
_PATH_KEYS: Final[tuple[str, ...]] = ("file_path", "filePath", "path")
_URL_KEYS: Final[tuple[str, ...]] = ("url", "uri")
_COMMAND_KEYS: Final[tuple[str, ...]] = ("command", "cmd")
_TOOL_SHAPES: Final[dict[str, tuple[str, str, tuple[str, ...]]]] = {
    "read_file": ("filesystem", "read", _PATH_KEYS),
    "write_file": ("filesystem", "write", _PATH_KEYS),
    "write_to_file": ("filesystem", "write", _PATH_KEYS),
    "replace_file_content": ("filesystem", "write", _PATH_KEYS),
    "multi_replace_file_content": ("filesystem", "write", _PATH_KEYS),
    "web_fetch": ("network", "connect", _URL_KEYS),
    "run_command": ("process", "spawn", _COMMAND_KEYS),
}
_EVENT_TYPES: Final = ("tool_result", "tool_call")


def _string(value: object, field: str) -> str:
    if isinstance(value, str) and value:
        return _text(value, f"tool event {field}")
    _fail(f"tool event has no {field}")


def _input(event: Mapping[str, object]) -> Mapping[str, object]:
    value = event.get("parameters")
    if isinstance(value, dict):
        return value
    raw = event.get("tool_call_args_json")
    if isinstance(raw, str):
        try:
            decoded: object = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise AntigravityProbeError(
                "tool call arguments are not valid JSON"
            ) from exc
        if isinstance(decoded, dict):
            return decoded
    _fail("tool event has no structured input")


def _status(event: Mapping[str, object]) -> str | None:
    value = event.get("status")
    if isinstance(value, str) and value:
        return value.lower()
    error = event.get("tool_result_is_error")
    if isinstance(error, bool):
        return "error" if error else "success"
    return None


def _target(value: str, targets: AntigravityProbeTargets) -> str:
    stripped = value.strip().rstrip("/")
    if stripped == targets.local_url.rstrip("/"):
        return "local-network"
    if stripped == targets.external_url.rstrip("/"):
        return "external-network"
    cleaned = Path(value.strip().strip("\"'`").removeprefix("file://"))
    for name, path in (
        ("symlink", targets.symlink),
        ("secret", targets.secret),
        ("git", targets.git),
        ("outside", targets.outside),
        ("workspace", targets.normal),
    ):
        if cleaned in {path, path.absolute()}:
            return name
        try:
            relative = path.absolute().relative_to(targets.workspace.absolute())
        except ValueError:
            continue
        if cleaned in {relative, Path(f"./{relative}")}:
            return name
    if cleaned in {Path("."), targets.workspace}:
        return "workspace"
    _fail("tool event target is outside the declared probe matrix")


def _evidence(
    tool: str,
    input_value: Mapping[str, object],
    status: str,
    targets: AntigravityProbeTargets,
) -> ToolEvidence:
    shape = _TOOL_SHAPES.get(tool)
    if shape is None:
        _fail(f"unsupported Antigravity provider tool: {tool}")
    kind, operation, keys = shape
    result = (
        "allowed" if status in _ALLOWED else "denied" if status in _DENIED else None
    )
    if result is None:
        _fail(f"unsupported Antigravity tool status: {status}")
    value = _string(
        next((input_value[key] for key in keys if key in input_value), None),
        "target",
    )
    if kind == "process":
        if not any(
            marker in value for marker in ("sleep 6", targets.process_marker.name)
        ):
            _fail("process tool target is not the declared process probe")
        target = "process"
    else:
        target = _target(value, targets)
    if kind == "network" and target not in {"local-network", "external-network"}:
        _fail("network tool target is not the declared network probe")
    return ToolEvidence(kind, operation, target, result)


def parse_antigravity_events(
    raw: str, targets: AntigravityProbeTargets
) -> ParsedAntigravityEvents:
    if not isinstance(raw, str):
        _fail("Antigravity output must be text")
    observations: list[ToolEvidence] = []
    completed: dict[str, ToolEvidence] = {}
    pending: dict[str, tuple[str, Mapping[str, object]]] = {}
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value: object = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise AntigravityProbeError(
                f"Antigravity output line {line_number} is not JSON"
            ) from exc
        if not isinstance(value, dict):
            _fail(f"Antigravity output line {line_number} must be an object")
        event = cast(Mapping[str, object], value)
        event_type = event.get("type")
        if not isinstance(event_type, str) or event_type not in _EVENT_TYPES:
            continue
        status = _status(event)
        call_value = event.get("tool_call_id") or event.get("call_id")
        call_id = (
            _text(call_value, "tool event call ID")
            if isinstance(call_value, str) and call_value
            else None
        )
        if status is None:
            if event_type != "tool_call" or call_id is None:
                _fail("terminal tool event has no status or call ID")
            pending[call_id] = (
                _string(event.get("tool_name"), "name").lower(),
                _input(event),
            )
            continue
        previous = pending.pop(call_id, None) if call_id is not None else None
        value = event.get("tool_name")
        tool = (
            _text(value, "tool event name").lower()
            if isinstance(value, str) and value
            else previous[0]
            if previous
            else _fail("tool event has no name")
        )
        try:
            input_value = _input(event)
        except AntigravityProbeError:
            if previous is None:
                raise
            input_value = previous[1]
        item = _evidence(tool, input_value, status, targets)
        if call_id is not None:
            old = completed.get(call_id)
            if old is not None and old != item:
                _fail("one Antigravity tool call produced contradictory results")
            if old is not None:
                continue
            completed[call_id] = item
        observations.append(item)
    if pending:
        _fail("Antigravity tool call has no terminal result")
    return ParsedAntigravityEvents(tuple(dict.fromkeys(observations)))


_EXPECTED: Final = {
    phase: tuple(ToolEvidence(*item) for item in values)
    for phase, values in _PHASE_EVIDENCE.items()
}
_PERMITTED: Final = {
    phase: expected
    + tuple(
        ToolEvidence(
            item.tool,
            item.operation,
            item.target,
            {"denied": "allowed", "allowed": "denied", "clean": "residual"}[
                item.result
            ],
        )
        for item in expected
    )
    for phase, expected in _EXPECTED.items()
}


def attest_phase(
    phase_id: str,
    evidence: Iterable[ToolEvidence],
    *,
    exit_code: int | None,
    timed_out: bool = False,
    cleanup: CleanupInventory | None = None,
    strict: bool = True,
) -> PhaseReceipt:
    expected = _EXPECTED[phase_id]
    observed = tuple(evidence)
    if (
        any(not isinstance(item, ToolEvidence) for item in observed)
        or len(set(observed)) != len(observed)
        or any(item not in _PERMITTED[phase_id] for item in observed)
        or strict
        and observed != expected
    ):
        raise ReceiptValidationError(
            f"Antigravity phase evidence is invalid: {phase_id}"
        )
    if timed_out != (exit_code is None):
        raise ReceiptValidationError("Antigravity phase exit status is inconsistent")
    inventory = cleanup or CleanupInventory()
    if phase_id == "cleanup" and inventory.has_residuals:
        observed = (ToolEvidence("cleanup", "inspect", "cleanup", "residual"),)
    outcome = (
        "timeout"
        if timed_out
        else "failed"
        if exit_code
        else "passed"
        if observed == expected
        else "inconclusive"
    )
    expected_result = {"positive-read": "allow", "cleanup": "clean"}.get(
        phase_id, "deny"
    )
    return PhaseReceipt(
        phase_id,
        expected_result,
        True,
        bool(observed),
        outcome,
        exit_code,
        timed_out,
        observed,
        inventory,
    )


def attest_profile(
    parsed: ParsedAntigravityEvents,
    *,
    exit_code: int | None,
    timed_out: bool = False,
    cleanup: CleanupInventory | None = None,
    blocked_reason: str | None = None,
) -> tuple[PhaseReceipt, ...]:
    if not isinstance(parsed, ParsedAntigravityEvents):
        _fail("Antigravity profile attestation requires parsed events")
    if blocked_reason is not None:
        if blocked_reason not in BLOCKER_CODES or parsed.observations:
            _fail("blocked Antigravity profile has invalid evidence")
        return tuple(
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
            for spec in required_phases_for_profile("read-only")
        )
    return tuple(
        attest_phase(
            spec.phase_id,
            (
                (
                    (ToolEvidence("cleanup", "inspect", "cleanup", "residual"),)
                    if cleanup is not None and cleanup.has_residuals
                    else _EXPECTED["cleanup"]
                )
                if spec.phase_id == "cleanup"
                else tuple(
                    item
                    for item in parsed.observations
                    if item in _PERMITTED[spec.phase_id]
                )
            ),
            exit_code=0 if spec.phase_id == "cleanup" else exit_code,
            timed_out=False if spec.phase_id == "cleanup" else timed_out,
            cleanup=cleanup,
            strict=spec.phase_id == "cleanup",
        )
        for spec in required_phases_for_profile("read-only")
    )


def assemble_receipt(
    manifest: Manifest,
    parsed: ParsedAntigravityEvents,
    *,
    exit_code: int | None,
    timed_out: bool = False,
    cleanup: CleanupInventory | None = None,
    blocked_reason: str | None = None,
) -> tuple[Receipt, Judgment]:
    if (
        not isinstance(manifest, Manifest)
        or manifest.identity.harness_id != "antigravity"
        or manifest.identity.sandbox_policy_id
        not in {RAW_POLICY_ID, SNAPSHOT_POLICY_ID}
    ):
        _fail("Antigravity receipt manifest has an invalid profile")
    _executable(manifest.identity.executable)
    receipt = Receipt(
        manifest.identity,
        blocked_reason,
        attest_profile(
            parsed,
            exit_code=exit_code,
            timed_out=timed_out,
            cleanup=cleanup,
            blocked_reason=blocked_reason,
        ),
    )
    return receipt, judge_profile(manifest, receipt)


def execute_probe(
    *,
    provenance: BinaryProvenance,
    profile: ProbeProfile,
    workspace: Path,
    private_root: Path,
    prompt: str,
    model: str,
    effort: Literal["low", "medium", "high"] = "low",
    runner: ProcessRunner | None = None,
    timeout_seconds: float = 180.0,
) -> ProcessResult:
    if not isinstance(provenance, BinaryProvenance):
        _fail("agy probe requires validated binary provenance")
    if (
        not isinstance(workspace, Path)
        or not workspace.is_absolute()
        or workspace.is_symlink()
        or not isinstance(private_root, Path)
        or not private_root.is_absolute()
        or private_root.is_symlink()
    ):
        _fail("agy probe roots must be non-symlink absolute paths")
    if timeout_seconds <= 0:
        _fail("agy probe timeout must be positive")
    current = _checked_identity(Path(provenance.executable.path))
    if current != provenance.file_identity:
        _fail("agy executable identity changed after preflight")
    validate_binary_provenance(
        path=Path(provenance.executable.path),
        version=provenance.executable.version,
        identity=current,
        signature=provenance.signature,
        device_identity=provenance.device_identity,
    )
    selected = runner if runner is not None else ProcessRunner()
    version = _version_probe(
        Path(provenance.executable.path),
        provider="antigravity",
        private_root=private_root,
        runner=selected,
    )
    if (
        re.search(rf"(?<![0-9]){re.escape(ANTIGRAVITY_VERSION)}(?![0-9])", version)
        is None
    ):
        _fail("agy executable version changed after preflight")
    return selected.run(
        build_probe_argv(
            executable=provenance.executable,
            profile=profile,
            prompt=prompt,
            model=model,
            effort=effort,
        ),
        cwd=workspace,
        env=safe_environment(
            "antigravity", home=private_root, private_root=private_root
        ),
        timeout_seconds=timeout_seconds,
    )
