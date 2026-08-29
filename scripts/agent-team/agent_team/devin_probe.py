"""Devin-specific, fail-closed probe helpers.

Provider output remains in memory. Persisted artifacts use the shared receipt
contract and contain only redacted identity labels and structured evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, NoReturn, cast

from .adapters import (
    ExecutionError,
    FileIdentity,
    ProcessResult,
    ProcessRunner,
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

DEVIN_VERSION: Final = "3000.6.7"
DEVIN_BUILD: Final = "260a97c8"
DEVIN_VERSION_OUTPUT: Final = f"devin {DEVIN_VERSION} ({DEVIN_BUILD})"
DEVIN_SHA256: Final = "82a95b4c7c4bfd73a3ad8ac31207d7ba815ceab2c2fc4a432d002adf7fe43590"
DEVIN_CDHASH: Final = "30bb4bb91719ca3457ff3af32ad7b0614d3ff379"
DEVIN_TEAM_IDENTIFIER: Final = "83Z2LHX6XW"
PROBE_REVISION: Final = "devin-probe-20260830-v1"

PROFILE_DIRECT_READ_ONLY: Final = "direct-read-only"
PROFILE_DIRECT_WORKSPACE_WRITE: Final = "direct-workspace-write"
PROFILE_ACP_REVIEW: Final = "acp-review"
PROFILE_ACP_SUMMARIZER: Final = "acp-summarizer"

_PROFILES: Final[dict[str, tuple[str, str | None, str]]] = {
    PROFILE_DIRECT_READ_ONLY: (
        "read-only",
        "auto",
        "devin-direct-auto-sandbox-readonly-v1",
    ),
    PROFILE_DIRECT_WORKSPACE_WRITE: (
        "workspace-write",
        "accept-edits",
        "devin-direct-accept-edits-sandbox-v1",
    ),
    PROFILE_ACP_REVIEW: ("read-only", None, "devin-acp-review-nonsandbox-v1"),
    PROFILE_ACP_SUMMARIZER: ("read-only", None, "devin-acp-summarizer-nonsandbox-v1"),
}
_RUNNING = frozenset({"pending", "running", "started", "in_progress"})
_ALLOWED = frozenset({"allowed", "allow", "completed", "success", "succeeded", "ok"})
_DENIED = frozenset(
    {"denied", "deny", "error", "failed", "failure", "forbidden", "rejected"}
)
_TOOL_TYPES = frozenset(
    {"tool", "tool_call", "tool_use", "tool_result", "tool_execute"}
)
_FINAL_TYPES = frozenset({"final", "final_result", "assistant_final"})
_FAILURE_TYPES = frozenset({"error", "failure", "provider_error", "provider_failure"})
_SAFE_ENV_KEYS: Final[frozenset[str]] = frozenset({"PATH", "SHELL", "LANG", "TERM"})
_DENIED_TOOLS: Final[tuple[str, ...]] = (
    "network",
    "process",
    "shell",
    "subagent",
    "mcp",
    "lsp",
    "hook",
    "plugin",
)
_TOOL_SHAPES: Final[dict[str, tuple[str, str]]] = {
    name: shape
    for names, shape in (
        (
            ["read", "list", "filesystem.read", "file.read", "fs.read"],
            ("filesystem", "read"),
        ),
        (
            [
                "write",
                "edit",
                "filesystem.write",
                "filesystem.edit",
                "file.write",
                "fs.write",
            ],
            ("filesystem", "write"),
        ),
        (
            ["network", "network.connect", "web", "webfetch", "fetch"],
            ("network", "connect"),
        ),
        (
            ["process", "process.spawn", "shell", "command", "exec", "bash"],
            ("process", "spawn"),
        ),
    )
    for name in names
}
_PROBE_WORKSPACE = Path("/probe/workspace")
_PROBE_EXECUTABLE = Path("/probe/devin")
_MAX_PROMPT_BYTES = 400_000
_RECEIPT_ENVIRONMENT = (
    "HOME",
    "LANG",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
_ProfileSpec = tuple[
    Literal["read-only", "workspace-write"], bool, str | None, str | None, str
]


class DevinProbeError(RuntimeError):
    """Raised when a Devin probe cannot be established safely."""


# FileIdentity already carries device/inode/size/mtime/hash. The alias keeps
# the provider-specific API explicit without duplicating the shared structure.
DevinFileIdentity = FileIdentity


@dataclass(frozen=True, slots=True)
class DevinExecutable:
    path: Path
    version: str
    build: str
    file_identity: DevinFileIdentity
    cdhash: str = DEVIN_CDHASH
    team_identifier: str = DEVIN_TEAM_IDENTIFIER

    def __post_init__(self) -> None:
        if not self.path.is_absolute() or (self.version, self.build) != (
            DEVIN_VERSION,
            DEVIN_BUILD,
        ):
            raise DevinProbeError("Devin executable path/version is not pinned")
        if (
            self.file_identity.sha256,
            self.cdhash,
            self.team_identifier,
        ) != (DEVIN_SHA256, DEVIN_CDHASH, DEVIN_TEAM_IDENTIFIER):
            raise DevinProbeError("Devin executable identity is not pinned")


def profile_spec(
    profile: str,
) -> _ProfileSpec:
    try:
        permission, permission_mode, policy = _PROFILES[profile]
    except KeyError as exc:
        raise DevinProbeError(f"unknown Devin probe profile: {profile}") from exc
    sandboxed = profile.startswith("direct-")
    acp_type = None if sandboxed else profile.removeprefix("acp-")
    return cast(
        _ProfileSpec, (permission, sandboxed, permission_mode, acp_type, policy)
    )


@dataclass(frozen=True, slots=True)
class DevinProbeTargets:
    """Runtime-only target paths; none are written to a receipt."""

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
class ParsedDevinEvents:
    observations: tuple[ToolEvidence, ...]
    final_text_seen: bool
    provider_failed: bool
    cleanup: CleanupInventory | None

    @property
    def evidence(self) -> tuple[ToolEvidence, ...]:
        return self.observations

    @property
    def tool_event_count(self) -> int:
        return len(self.observations)


@dataclass(frozen=True, slots=True)
class DevinProbeRun:
    receipt: Receipt
    judgment: Judgment
    returncode: int | None
    timed_out: bool
    tool_event_count: int


def _fail(message: str) -> NoReturn:
    raise DevinProbeError(message)


def _absolute(path: Path, field: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        _fail(f"{field} must be an absolute path")
    return path


def _sha256_file(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise DevinProbeError("could not hash Devin executable") from exc


def _stat_identity(path: Path, digest: str) -> DevinFileIdentity:
    try:
        value = path.lstat()
    except OSError as exc:
        raise DevinProbeError("Devin executable is unavailable") from exc
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISREG(value.st_mode)
        or not value.st_mode & 0o111
    ):
        _fail("Devin executable must be executable")
    return FileIdentity(
        value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, digest
    )


def _parse_version(output: str) -> tuple[str, str]:
    if output.strip() != DEVIN_VERSION_OUTPUT:
        _fail("Devin version output is not the exact pinned release")
    return DEVIN_VERSION, DEVIN_BUILD


def capture_executable_identity(path: Path, *, version_output: str) -> DevinExecutable:
    """Capture the pinned binary identity without starting a provider turn."""

    path = _absolute(path, "Devin executable")
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode):
        _fail("Devin executable must not be a symlink")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise DevinProbeError("Devin executable is unavailable") from exc
    version, build = _parse_version(version_output)
    digest = _sha256_file(path)
    if digest != DEVIN_SHA256:
        _fail("Devin executable does not have the pinned SHA-256")
    identity = _stat_identity(path, digest)
    if identity != _stat_identity(path, digest):
        _fail("Devin executable changed during identity capture")
    return DevinExecutable(path, version, build, identity)


def verify_executable_identity(
    executable: DevinExecutable, *, version_output: str
) -> None:
    current = capture_executable_identity(
        executable.path, version_output=version_output
    )
    if current != executable:
        _fail("Devin executable identity changed after preflight")


def _argv_digest(argv: Sequence[str]) -> str:
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        _fail("Devin probe argv must contain non-empty strings")
    return hashlib.sha256(
        json.dumps(list(argv), ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def build_probe_argv(
    profile: str,
    executable: Path,
    config_path: Path,
    prompt_path: Path,
    *,
    model: str | None = None,
) -> tuple[str, ...]:
    _, sandboxed, permission_mode, acp_agent_type, _ = profile_spec(profile)
    executable, config_path, prompt_path = (
        _absolute(executable, "Devin executable"),
        _absolute(config_path, "Devin config"),
        _absolute(prompt_path, "Devin prompt"),
    )
    if model is not None:
        _fail("Devin model override is unsupported; use the current account default")
    if sandboxed:
        return (
            str(executable),
            "--permission-mode",
            cast(str, permission_mode),
            "--sandbox",
            "--respect-workspace-trust",
            "false",
            "--config",
            str(config_path),
            "--print",
            "--prompt-file",
            str(prompt_path),
        )
    return (str(executable), "acp", "--agent-type", cast(str, acp_agent_type))


def build_probe_manifest(
    *,
    profile: str,
    executable: DevinExecutable,
    argv: Sequence[str],
    environment_allowlist: Sequence[str],
) -> Manifest:
    permission_profile, sandboxed, permission_mode, acp_agent_type, policy = (
        profile_spec(profile)
    )
    if executable.file_identity.sha256 != DEVIN_SHA256:
        _fail("Devin executable hash is not pinned")
    if str(executable.path) not in argv:
        _fail("Devin probe argv does not use the preflighted executable")
    if sandboxed:
        if "--sandbox" not in argv or permission_mode not in argv or "acp" in argv:
            _fail("Devin direct argv does not include the pinned sandbox profile")
    elif tuple(argv[1:4]) != ("acp", "--agent-type", cast(str, acp_agent_type)):
        _fail("Devin ACP argv does not use the pinned non-sandbox profile")
    environment = tuple(environment_allowlist)
    if len(set(environment)) != len(environment) or any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name)
        for name in environment
    ):
        _fail("Devin environment allowlist is invalid")
    return Manifest(
        ProfileIdentity(
            CURRENT_SCHEMA_VERSION,
            "devin",
            permission_profile,
            platform.system().lower(),
            platform.machine().lower(),
            PROBE_REVISION,
            ExecutableIdentity(
                str(_PROBE_EXECUTABLE), DEVIN_VERSION_OUTPUT, DEVIN_SHA256
            ),
            _argv_digest(argv),
            "file" if sandboxed else "stdin",
            str(_PROBE_WORKSPACE),
            environment,
            policy,
        ),
        required_phases_for_profile(permission_profile),
    )


def build_safe_environment(
    *, home: Path, private_root: Path, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Use the shared closed environment helper with a Devin-safe source map."""

    home, private_root = (
        _absolute(home, "Devin probe HOME"),
        _absolute(private_root, "Devin private root"),
    )
    source_values = os.environ if source is None else source
    safe_source = {
        key: value
        for key, value in source_values.items()
        if key in _SAFE_ENV_KEYS or key.startswith("LC_")
    }
    # The shared helper has no provider-neutral selector yet. Passing the
    # already-filtered map means its OpenCode branch contributes no provider key.
    return safe_environment(
        "opencode", home=home, private_root=private_root, source=safe_source
    )


def _owned_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        value = path.lstat()
    except OSError as exc:
        raise DevinProbeError("Devin private root is unavailable") from exc
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o700
    ):
        _fail("Devin private root must be an owned mode-0700 directory")


def _write_private(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(data)
        path.chmod(0o600)
    except OSError as exc:
        try:
            path.unlink()
        except OSError:
            pass
        raise DevinProbeError("could not write temporary Devin probe data") from exc


def write_isolated_config(
    private_root: Path,
    workspace: Path,
    *,
    profile: str = PROFILE_DIRECT_READ_ONLY,
) -> Path:
    permission_profile, _, _, _, _ = profile_spec(profile)
    private_root, workspace = (
        _absolute(private_root, "Devin private root"),
        _absolute(workspace, "Devin workspace"),
    )
    _owned_directory(private_root)
    path = private_root / "devin-probe-config.json"
    payload = {
        "version": 1,
        "auto_update": False,
        "respect_workspace_trust": False,
        "hooks": {},
        "mcpServers": {},
        "plugins": [],
        "workspace": str(workspace),
        "permissions": {
            "allow": ["read"]
            if permission_profile == "read-only"
            else ["read", "write"],
            "ask": [],
            "deny": _DENIED_TOOLS,
        },
    }
    _write_private(
        path, json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    )
    return path


def _write_prompt(private_root: Path, prompt: str) -> Path:
    if not prompt.strip() or len(prompt.encode()) > _MAX_PROMPT_BYTES:
        _fail("Devin probe prompt is empty or too large")
    path = private_root / "devin-probe-prompt.md"
    _write_private(path, prompt.encode())
    return path


def _target(value: str, targets: DevinProbeTargets) -> str:
    label_values = {
        "workspace": targets.workspace,
        "normal": targets.normal,
        "outside": targets.outside,
        "symlink": targets.symlink,
        "git": targets.git,
        "secret": targets.secret,
        "process": targets.process_marker,
    }
    actual = value.strip().strip("\"'`").removeprefix("file://").rstrip("/")
    normalized = actual.lower()
    if normalized in {"process-marker", "process_probe"}:
        return "process"
    if normalized in label_values:
        return "workspace" if normalized == "normal" else normalized
    for label, path in label_values.items():
        if actual in {str(path), str(path.resolve(strict=False))}:
            return "workspace" if label == "normal" else label
    if actual in {targets.local_url.rstrip("/"), "local-network"}:
        return "local-network"
    if actual in {targets.external_url.rstrip("/"), "external-network"}:
        return "external-network"
    _fail("Devin tool event target is outside the declared probe matrix")


def _tool_shape(event: Mapping[str, object]) -> tuple[str, str]:
    raw = event.get("tool", event.get("name"))
    operation = event.get("operation")
    tool = raw.lower() if isinstance(raw, str) else ""
    op = operation.lower() if isinstance(operation, str) else ""
    combined = tool if "." in tool or not op else f"{tool}.{op}"
    try:
        return _TOOL_SHAPES[combined]
    except KeyError:
        _fail(f"unsupported Devin tool: {tool or '<missing>'}")


def _event_target(event: Mapping[str, object]) -> str:
    for key in ("target", "path", "file", "filePath", "url", "uri", "command"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    nested = event.get("input")
    if isinstance(nested, dict):
        return _event_target(cast(Mapping[str, object], nested))
    _fail("Devin tool event has no structured target")


def _event_status(event: Mapping[str, object]) -> str:
    value = event.get("status", event.get("result"))
    if isinstance(value, str) and value:
        return value.lower()
    if isinstance(value, dict) and isinstance(value.get("status"), str):
        return cast(str, value["status"]).lower()
    _fail("Devin tool event has no structured status")


def _cleanup(event: Mapping[str, object]) -> CleanupInventory:
    value = event.get("inventory")
    source = value if isinstance(value, dict) else event
    numbers: list[int] = []
    for name in ("child_processes", "sessions", "containers", "temporary_roots"):
        number = source.get(name, 0)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            _fail(f"Devin cleanup field {name} is invalid")
        numbers.append(number)
    return CleanupInventory(*numbers)


def parse_devin_events(raw: str, targets: DevinProbeTargets) -> ParsedDevinEvents:
    if not isinstance(raw, str):
        _fail("Devin output must be text")
    observations: list[ToolEvidence] = []
    calls: dict[str, ToolEvidence] = {}
    final_seen = provider_failed = False
    cleanup: CleanupInventory | None = None
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value: object = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise DevinProbeError(
                f"Devin output line {line_number} is not JSON"
            ) from exc
        if not isinstance(value, dict):
            _fail(f"Devin output line {line_number} must be an object")
        event = cast(Mapping[str, object], value)
        event_type = event.get("type")
        kind = event_type.lower() if isinstance(event_type, str) else ""
        if kind in _FINAL_TYPES:
            final_seen |= isinstance(event.get("text"), str) and bool(
                cast(str, event["text"]).strip()
            )
            continue
        if kind in _FAILURE_TYPES:
            provider_failed = True
            continue
        if kind == "cleanup":
            status = event.get("status")
            if not isinstance(status, str) or status.lower() not in {
                "clean",
                "completed",
                "success",
                "residual",
                "dirty",
            }:
                _fail("Devin cleanup status is missing or unsupported")
            observed = _cleanup(event)
            if status.lower() in {"residual", "dirty"} and not observed.has_residuals:
                _fail("Devin cleanup marked residual without residual inventory")
            if cleanup is not None and cleanup != observed:
                _fail("Devin cleanup events are contradictory")
            cleanup = observed
            continue
        if kind not in _TOOL_TYPES:
            continue
        status = _event_status(event)
        if status in _RUNNING:
            continue
        tool, operation = _tool_shape(event)
        result = (
            "allowed" if status in _ALLOWED else "denied" if status in _DENIED else None
        )
        if result is None:
            _fail(f"unsupported Devin tool status: {status}")
        target = _target(_event_target(event), targets)
        if tool == "network" and target not in {"local-network", "external-network"}:
            _fail("Devin network event target is not declared")
        if tool == "process" and target != "process":
            _fail("Devin process event target is not declared")
        observation = ToolEvidence(tool, operation, target, result)
        call_id = event.get("id", event.get("call_id"))
        if not isinstance(call_id, str) or not call_id:
            _fail("Devin tool event has no stable call id")
        previous = calls.get(call_id)
        if previous is not None:
            if previous != observation:
                _fail("one Devin tool call produced contradictory terminal results")
            continue
        calls[call_id] = observation
        observations.append(observation)
    return ParsedDevinEvents(tuple(observations), final_seen, provider_failed, cleanup)


def _expected(phase_id: str) -> tuple[ToolEvidence, ...]:
    try:
        return tuple(ToolEvidence(*item) for item in _PHASE_EVIDENCE[phase_id])
    except KeyError as exc:
        raise DevinProbeError(f"unknown Devin probe phase: {phase_id}") from exc


def _not_run(phase_id: str, expected_result: str) -> PhaseReceipt:
    return PhaseReceipt(
        phase_id,
        expected_result,
        False,
        False,
        "not-run",
        None,
        False,
        (),
        CleanupInventory(),
    )


def attest_phase(
    phase_id: str,
    evidence: Iterable[ToolEvidence],
    *,
    exit_code: int | None,
    timed_out: bool = False,
    cancelled: bool = False,
    cleanup: CleanupInventory | None = None,
    strict: bool = True,
    provider_failed: bool = False,
) -> PhaseReceipt:
    expected = _expected(phase_id)
    observed = tuple(evidence)
    if any(not isinstance(item, ToolEvidence) for item in observed) or len(
        set(observed)
    ) != len(observed):
        raise ReceiptValidationError(f"Devin phase evidence is invalid: {phase_id}")
    if strict and observed != expected:
        raise ReceiptValidationError(f"Devin phase evidence is incomplete: {phase_id}")
    if timed_out != (exit_code is None):
        raise ReceiptValidationError("Devin timeout and exit code disagree")
    inventory = cleanup or CleanupInventory()
    if cancelled and not timed_out and exit_code == 0:
        exit_code = 130
    if phase_id == "cleanup" and inventory.has_residuals:
        observed = ()
    outcome = (
        "timeout"
        if timed_out
        else "failed"
        if provider_failed or exit_code not in {None, 0}
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
    parsed: ParsedDevinEvents,
    *,
    exit_code: int | None,
    timed_out: bool = False,
    cancelled: bool = False,
    attempted: bool = True,
    permission_profile: Literal["read-only", "workspace-write"] = "read-only",
) -> tuple[PhaseReceipt, ...]:
    specs = required_phases_for_profile(permission_profile)
    if not attempted:
        return tuple(_not_run(s.phase_id, s.expected_result) for s in specs)
    if exit_code is None and not timed_out:
        exit_code = 1
    expected_evidence = tuple(
        item for spec in specs for item in _expected(spec.phase_id)
    )
    unexpected = any(item not in expected_evidence for item in parsed.evidence)
    failed = parsed.provider_failed or not parsed.final_text_seen or unexpected
    if failed and not timed_out and exit_code == 0:
        exit_code = 1
    result: list[PhaseReceipt] = []
    for spec in specs:
        if spec.phase_id == "cleanup":
            evidence = (
                _expected("cleanup")
                if parsed.cleanup is not None and not parsed.cleanup.has_residuals
                else ()
            )
            result.append(
                attest_phase(
                    spec.phase_id,
                    evidence,
                    exit_code=0,
                    cleanup=parsed.cleanup,
                    strict=False,
                )
            )
            continue
        expected = _expected(spec.phase_id)
        result.append(
            attest_phase(
                spec.phase_id,
                tuple(item for item in parsed.evidence if item in expected),
                exit_code=exit_code,
                timed_out=timed_out,
                cancelled=cancelled,
                strict=False,
                provider_failed=failed,
            )
        )
    return tuple(result)


def blocked_receipt(
    manifest: Manifest, reason: str = "account"
) -> tuple[Receipt, Judgment]:
    if reason not in BLOCKER_CODES:
        raise ReceiptValidationError(f"unsupported Devin blocked reason: {reason}")
    receipt = Receipt(
        manifest.identity,
        reason,
        tuple(
            _not_run(s.phase_id, s.expected_result) for s in manifest.required_phases
        ),
    )
    return receipt, judge_profile(manifest, receipt)


def run_live_probe(
    *,
    profile: str,
    executable: DevinExecutable,
    workspace: Path,
    private_root: Path,
    prompt: str,
    version_output: str,
    targets: DevinProbeTargets,
    environment_source: Mapping[str, str] | None = None,
    timeout_seconds: float = 900.0,
    model: str | None = None,
    runner: ProcessRunner | None = None,
) -> DevinProbeRun:
    """Run one bounded isolated turn and return only a redacted summary."""

    workspace, private_root = (
        _absolute(workspace, "Devin workspace"),
        _absolute(private_root, "Devin private root"),
    )
    permission_profile, sandboxed, _, _, _ = profile_spec(profile)
    if not sandboxed:
        _fail("native ACP requires an explicit protocol client; no raw stdin fallback")
    verify_executable_identity(executable, version_output=version_output)
    _owned_directory(private_root)
    config: Path | None = None
    prompt_path: Path | None = None
    try:
        config = write_isolated_config(private_root, workspace, profile=profile)
        prompt_path = _write_prompt(private_root, prompt)
        argv = build_probe_argv(
            profile,
            executable.path,
            config or private_root / "unused",
            prompt_path or private_root / "unused",
            model=model,
        )
        manifest = build_probe_manifest(
            profile=profile,
            executable=executable,
            argv=argv,
            environment_allowlist=_RECEIPT_ENVIRONMENT,
        )
        process_runner = runner or ProcessRunner()
        try:
            raw_result = process_runner.run(
                argv,
                cwd=workspace,
                env=build_safe_environment(
                    home=private_root / "home",
                    private_root=private_root,
                    source=environment_source,
                ),
                input_text=None,
                timeout_seconds=timeout_seconds,
            )
        except ExecutionError as exc:
            raw_result = ProcessResult(1, "", "", "timed out" in str(exc).lower())
        result = raw_result
        parsed = parse_devin_events(result.stdout, targets)
        phases = attest_profile(
            parsed,
            exit_code=None if result.timed_out else result.returncode,
            timed_out=result.timed_out,
            permission_profile=permission_profile,
        )
        receipt = Receipt(manifest.identity, None, phases)
        return DevinProbeRun(
            receipt,
            judge_profile(manifest, receipt),
            result.returncode,
            result.timed_out,
            parsed.tool_event_count,
        )
    finally:
        for path in (prompt_path, config):
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise DevinProbeError(
                        "could not remove temporary Devin probe file"
                    ) from exc
