"""Pinned Cursor Agent profiles and provider-free receipt assembly.

The live boundary is the injected runner.  This module never resolves
``cursor-agent`` through PATH, writes provider state, or persists prompts,
provider output, environment values, or local paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol, cast

from agent_team.adapters import (
    SAFE_ENV_KEYS,
    AdapterSnapshot,
    FileIdentity,
    ProcessResult,
)
from agent_team.probe_receipts import (
    BLOCKER_CODES,
    CURRENT_SCHEMA_VERSION,
    ExecutableIdentity,
    Manifest,
    PhaseReceipt,
    ProfileIdentity,
    Receipt,
    ReceiptValidationError,
    judge_profile,
    required_phases_for_profile,
)

CURSOR_PINNED_VERSION: Final = "2026.05.09-0afadcc"
CURSOR_PROBE_REVISION: Final = "cursor-probe-20260830"
MAX_PROMPT_BYTES: Final = 400_000
MAX_TIMEOUT_SECONDS: Final = 900.0
VERSION_TIMEOUT_SECONDS: Final = 15.0
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLATFORM = re.compile(r"[a-zA-Z0-9._-]{1,64}\Z")
_REASON_CODE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_PROFILES: Final[dict[str, tuple[str, str, str]]] = {
    "direct-plan": ("cursor", "argv", "cursor-advertised-plan-v1"),
    "acp": ("cursor", "stdin", "cursor-acp-no-policy-v1"),
}
_BYPASS_FLAGS: Final = frozenset(
    {"--force", "--yolo", "--trust", "--approve-mcps", "--plugin-dir"}
)

CursorProfile = Literal["direct-plan", "acp"]
CursorTransport = Literal["argv", "stdin"]
CursorStatus = Literal["candidate", "rejected", "blocked", "not-run"]


class CursorProbeError(RuntimeError):
    """Base class for fail-closed Cursor probe errors."""


class CursorIdentityError(CursorProbeError):
    """The pinned executable or bundle no longer matches its identity."""


class CursorExecutionError(CursorProbeError):
    """A bounded Cursor invocation failed or timed out."""


def _validate_file_identity(identity: FileIdentity) -> None:
    if not isinstance(identity, FileIdentity):
        raise CursorIdentityError("Cursor file identity is invalid")
    for value, name in (
        (identity.device, "device"),
        (identity.inode, "inode"),
        (identity.size, "size"),
        (identity.mtime_ns, "mtime_ns"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise CursorIdentityError(f"Cursor file identity {name} is invalid")
    if _SHA256.fullmatch(identity.sha256) is None:
        raise CursorIdentityError("Cursor file identity sha256 is invalid")


@dataclass(frozen=True, slots=True)
class _ArtifactPin:
    path: Path = field(repr=False)
    identity: FileIdentity
    canonical_path: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise CursorIdentityError("Cursor pin path must be absolute")
        _validate_file_identity(self.identity)
        object.__setattr__(self, "canonical_path", self.path.resolve(strict=False))


@dataclass(frozen=True, slots=True)
class CursorExecutablePin:
    """Caller-supplied pin; no PATH or alternate-version fallback is allowed."""

    path: Path = field(repr=False)
    version: str
    identity: FileIdentity
    bundle_path: Path | None = field(default=None, repr=False)
    bundle_identity: FileIdentity | None = None
    _canonical_path: Path = field(init=False, repr=False)
    _bundle: _ArtifactPin | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        executable = _ArtifactPin(self.path, self.identity)
        if not isinstance(self.version, str) or not self.version.strip():
            raise CursorIdentityError("Cursor pin version must be non-empty")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in self.version):
            raise CursorIdentityError("Cursor pin version contains control text")
        if (self.bundle_path is None) != (self.bundle_identity is None):
            raise CursorIdentityError(
                "Cursor bundle_path and bundle_identity must be supplied together"
            )
        bundle = None
        if self.bundle_path is not None and self.bundle_identity is not None:
            bundle = _ArtifactPin(self.bundle_path, self.bundle_identity)
        object.__setattr__(self, "_canonical_path", executable.canonical_path)
        object.__setattr__(self, "_bundle", bundle)


@dataclass(frozen=True, slots=True)
class CursorInvocation:
    profile: CursorProfile
    executable: Path = field(repr=False)
    argv: tuple[str, ...] = field(repr=False)
    input_text: str | None = field(repr=False)
    prompt_transport: CursorTransport
    timeout_seconds: float
    identity: FileIdentity | None = None
    bundle_path: Path | None = field(default=None, repr=False)
    bundle_identity: FileIdentity | None = None
    _prompt_arg_index: int | None = field(default=None, repr=False)

    @property
    def argv_sha256(self) -> str:
        payload = json.dumps(
            self.argv,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def redacted_argv(self, workspace: Path) -> tuple[str, ...]:
        """Return argv with prompt and filesystem paths replaced by markers."""

        result: list[str] = []
        for index, token in enumerate(self.argv):
            if index == self._prompt_arg_index:
                result.append("<prompt>")
            elif token == str(self.executable):
                result.append("<pinned-cursor-agent>")
            elif token == str(workspace):
                result.append("<workspace>")
            else:
                result.append(token)
        return tuple(result)


@dataclass(frozen=True, slots=True)
class CursorDecision:
    harness_id: str
    permission_profile: str
    profile_id: CursorProfile
    status: CursorStatus
    reason_codes: tuple[str, ...]
    receipt: Receipt | None


class CursorCommandRunner(Protocol):
    """Runner seam; :class:`agent_team.adapters.ProcessRunner` implements it."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_text: str | None = None,
        timeout_seconds: float = 900.0,
    ) -> ProcessResult: ...


def safe_environment(
    source: Mapping[str, str], *, home: Path | None = None
) -> dict[str, str]:
    """Use the shared allowlist and never return provider credential names."""

    result = {
        key: value
        for key, value in source.items()
        if (key in SAFE_ENV_KEYS or key.startswith("LC_")) and isinstance(value, str)
    }
    if home is not None:
        if not home.is_absolute():
            raise CursorProbeError("isolated HOME must be absolute")
        result["HOME"] = str(home)
    return result


class CursorProbe:
    """Build one fixed Cursor read-only profile and run it through a runner."""

    def __init__(
        self,
        pin: CursorExecutablePin,
        *,
        profile: CursorProfile,
        workspace: Path,
        environment: Mapping[str, str] | None = None,
        probe_revision: str = CURSOR_PROBE_REVISION,
    ) -> None:
        if not isinstance(profile, str) or profile not in _PROFILES:
            raise CursorProbeError(f"unsupported Cursor profile: {profile!r}")
        if not isinstance(workspace, Path) or not workspace.is_absolute():
            raise CursorProbeError("Cursor workspace must be absolute")
        if not isinstance(pin, CursorExecutablePin):
            raise CursorIdentityError("Cursor executable pin is invalid")
        if not isinstance(probe_revision, str) or not _PLATFORM.fullmatch(
            probe_revision
        ):
            raise CursorProbeError("Cursor probe revision is invalid")
        self.pin = pin
        self.profile: CursorProfile = profile
        self.workspace = workspace
        self.environment = safe_environment(
            os.environ if environment is None else environment
        )
        self.probe_revision = probe_revision

    @property
    def adapter_id(self) -> str:
        return f"cursor-{self.profile}-readonly"

    def build_invocation(
        self, prompt: str, *, timeout_seconds: float = 90.0
    ) -> CursorInvocation:
        _validate_prompt(prompt)
        _validate_timeout(timeout_seconds)
        self._resolve_current_path()
        self._resolve_workspace()
        if self.profile == "direct-plan":
            argv = (
                str(self.pin.path),
                "--print",
                "--mode",
                "plan",
                "--sandbox",
                "enabled",
                "--workspace",
                str(self.workspace),
                "--output-format",
                "text",
                prompt,
            )
            return CursorInvocation(
                self.profile,
                self.pin.path,
                argv,
                None,
                "argv",
                timeout_seconds,
                self.pin.identity,
                self.pin.bundle_path,
                self.pin.bundle_identity,
                len(argv) - 1,
            )
        return CursorInvocation(
            self.profile,
            self.pin.path,
            (str(self.pin.path), "acp"),
            prompt,
            "stdin",
            timeout_seconds,
            self.pin.identity,
            self.pin.bundle_path,
            self.pin.bundle_identity,
        )

    def manifest(self, invocation: CursorInvocation) -> Manifest:
        self._validate_invocation(invocation)
        harness_id, transport, policy_id = _PROFILES[self.profile]
        os_name = platform.system().lower()
        architecture = platform.machine().lower()
        if not _PLATFORM.fullmatch(os_name) or not _PLATFORM.fullmatch(architecture):
            raise CursorProbeError("Cursor platform identity is invalid")
        identity = ProfileIdentity(
            CURRENT_SCHEMA_VERSION,
            harness_id,
            "read-only",
            os_name,
            architecture,
            self.probe_revision,
            ExecutableIdentity(
                str(invocation.executable), self.pin.version, self.pin.identity.sha256
            ),
            invocation.argv_sha256,
            transport,
            str(self.workspace),
            tuple(sorted(self.environment)),
            policy_id,
        )
        return Manifest(identity, required_phases_for_profile("read-only"))

    def preflight(self, runner: CursorCommandRunner) -> AdapterSnapshot:
        """Verify the exact file and version without starting a model turn."""

        path, identity = self._verify_identity()
        result = runner.run(
            (str(self.pin.path), "--version"),
            cwd=self._resolve_workspace(),
            env=safe_environment(self.environment),
            input_text=None,
            timeout_seconds=VERSION_TIMEOUT_SECONDS,
        )
        version = _require_version_result(result, self.pin.version)
        return AdapterSnapshot(
            self.adapter_id, self.probe_revision, path, version, identity
        )

    def execute(
        self,
        invocation: CursorInvocation,
        runner: CursorCommandRunner,
    ) -> ProcessResult:
        """Revalidate identity/version immediately before the provider command."""

        self._validate_invocation(invocation)
        self._verify_identity()
        version_result = runner.run(
            (str(self.pin.path), "--version"),
            cwd=self._resolve_workspace(),
            env=safe_environment(self.environment),
            input_text=None,
            timeout_seconds=VERSION_TIMEOUT_SECONDS,
        )
        _require_version_result(version_result, self.pin.version)
        self._verify_identity()
        result = runner.run(
            invocation.argv,
            cwd=self._resolve_workspace(),
            env=safe_environment(self.environment),
            input_text=invocation.input_text,
            timeout_seconds=invocation.timeout_seconds,
        )
        if result.timed_out:
            raise CursorExecutionError("Cursor provider command timed out")
        if result.returncode != 0:
            raise CursorExecutionError(
                f"Cursor provider command failed with exit {result.returncode}"
            )
        if not result.stdout.strip():
            raise CursorExecutionError("Cursor provider returned empty output")
        return result

    def run(
        self, prompt: str, runner: CursorCommandRunner, *, timeout_seconds: float = 90.0
    ) -> ProcessResult:
        invocation = self.build_invocation(prompt, timeout_seconds=timeout_seconds)
        self.preflight(runner)
        return self.execute(invocation, runner)

    def _resolve_workspace(self) -> Path:
        try:
            workspace_stat = self.workspace.lstat()
            resolved = self.workspace.resolve(strict=True)
        except OSError as exc:
            raise CursorProbeError("Cursor workspace is unavailable") from exc
        if stat.S_ISLNK(workspace_stat.st_mode) or not resolved.is_dir():
            raise CursorProbeError("Cursor workspace must be a real directory")
        return resolved

    def _resolve_current_path(self) -> Path:
        try:
            resolved = self.pin.path.resolve(strict=True)
        except OSError as exc:
            raise CursorIdentityError(
                "pinned Cursor executable is unavailable"
            ) from exc
        if resolved != self.pin._canonical_path:
            raise CursorIdentityError("pinned Cursor executable path changed")
        return resolved

    def _verify_identity(self) -> tuple[Path, FileIdentity]:
        path = self._resolve_current_path()
        observed = _capture_identity(path)
        if observed != self.pin.identity:
            raise CursorIdentityError("pinned Cursor executable identity changed")
        if self.pin._bundle is not None:
            bundle = self.pin._bundle
            try:
                bundle_path = bundle.path.resolve(strict=True)
            except OSError as exc:
                raise CursorIdentityError(
                    "pinned Cursor bundle is unavailable"
                ) from exc
            if bundle_path != bundle.canonical_path:
                raise CursorIdentityError("pinned Cursor bundle path changed")
            if (
                _capture_identity(bundle_path, require_executable=False)
                != bundle.identity
            ):
                raise CursorIdentityError("pinned Cursor bundle identity changed")
        return path, observed

    def _validate_invocation(self, invocation: CursorInvocation) -> None:
        if not isinstance(invocation, CursorInvocation):
            raise CursorProbeError("Cursor invocation is invalid")
        if invocation.profile != self.profile:
            raise CursorProbeError("Cursor invocation profile does not match probe")
        if invocation.executable != self.pin.path:
            raise CursorIdentityError("Cursor invocation executable is not pinned")
        if (
            invocation.bundle_path != self.pin.bundle_path
            or invocation.bundle_identity != self.pin.bundle_identity
        ):
            raise CursorIdentityError("Cursor invocation bundle is not pinned")
        if not invocation.argv or any(
            not isinstance(item, str) or not item for item in invocation.argv
        ):
            raise CursorProbeError("Cursor invocation argv is invalid")
        if invocation.argv[0] != str(invocation.executable):
            raise CursorProbeError("Cursor invocation executable argv is inconsistent")
        if invocation.identity != self.pin.identity:
            raise CursorIdentityError("Cursor invocation identity is not pinned")
        _validate_timeout(invocation.timeout_seconds)
        if self.profile == "direct-plan":
            expected_prefix = (
                str(self.pin.path),
                "--print",
                "--mode",
                "plan",
                "--sandbox",
                "enabled",
                "--workspace",
                str(self.workspace),
                "--output-format",
                "text",
            )
            if (
                invocation.prompt_transport != "argv"
                or invocation.input_text is not None
                or invocation._prompt_arg_index != len(invocation.argv) - 1
                or tuple(invocation.argv[:-1]) != expected_prefix
            ):
                raise CursorProbeError("Cursor direct invocation is not fixed")
            _validate_prompt(invocation.argv[-1])
            if any(flag in invocation.argv[:-1] for flag in _BYPASS_FLAGS):
                raise CursorProbeError(
                    "Cursor direct invocation contains a bypass flag"
                )
            return
        if (
            invocation.prompt_transport != "stdin"
            or invocation.argv != (str(self.pin.path), "acp")
            or invocation.input_text is None
        ):
            raise CursorProbeError("Cursor ACP invocation is not fixed")
        _validate_prompt(invocation.input_text)


def evaluate_profile(
    manifest: Manifest,
    phases: Sequence[PhaseReceipt],
    *,
    blocked_reason: str | None = None,
) -> CursorDecision:
    """Judge the common receipt contract without starting Cursor."""

    profile = _profile_from_manifest(manifest)
    if profile is None:
        return CursorDecision(
            "cursor",
            "read-only",
            "direct-plan",
            "rejected",
            ("profile-identity-mismatch",),
            None,
        )
    if blocked_reason is not None and blocked_reason not in BLOCKER_CODES:
        return _decision(manifest, profile, "rejected", ("invalid-blocker",), None)
    expected_ids = tuple(
        phase.phase_id for phase in required_phases_for_profile("read-only")
    )
    observations = tuple(phases)
    if tuple(getattr(phase, "phase_id", "") for phase in observations) != expected_ids:
        return _decision(manifest, profile, "rejected", ("phase-set-mismatch",), None)
    if any(not isinstance(phase, PhaseReceipt) for phase in observations):
        return _decision(manifest, profile, "rejected", ("phase-malformed",), None)
    try:
        receipt = Receipt(manifest.identity, blocked_reason, observations)
        judgment = judge_profile(manifest, receipt)
    except (ReceiptValidationError, TypeError, ValueError):
        return _decision(manifest, profile, "rejected", ("receipt-invalid",), None)
    return _decision(
        manifest,
        profile,
        judgment.status,
        judgment.reason_codes,
        receipt,
    )


def redacted_record(
    manifest: Manifest,
    invocation: CursorInvocation,
    *,
    status: CursorStatus,
    reason_codes: Sequence[str] = (),
) -> dict[str, object]:
    """Return a persistence-safe summary; raw prompt/output/path never appear."""

    profile = _profile_from_manifest(manifest)
    if profile is None or invocation.profile != profile:
        raise CursorProbeError("manifest and invocation profile do not match")
    if status not in {"candidate", "rejected", "blocked", "not-run"}:
        raise CursorProbeError("Cursor status is invalid")
    safe_reasons = _safe_reason_codes(reason_codes)
    if invocation.identity is None:
        raise CursorProbeError("Cursor invocation identity is missing")
    identity = manifest.identity
    file_identity = invocation.identity
    record: dict[str, object] = {
        "artifact": "cursor-probe",
        "harness_id": identity.harness_id,
        "profile_id": profile,
        "permission_profile": identity.permission_profile,
        "os": identity.os_name,
        "architecture": identity.architecture,
        "probe_revision": identity.probe_revision,
        "executable": {
            "version": identity.executable.version,
            "sha256": identity.executable.sha256,
            "device": file_identity.device,
            "inode": file_identity.inode,
            "size": file_identity.size,
            "mtime_ns": file_identity.mtime_ns,
        },
        "executable_path_sha256": hashlib.sha256(
            identity.executable.path.encode("utf-8")
        ).hexdigest(),
        "argv_sha256": identity.argv_sha256,
        "redacted_argv": list(invocation.redacted_argv(Path(identity.cwd))),
        "prompt_transport": identity.prompt_transport,
        "cwd": "<workspace>",
        "environment_allowlist": list(identity.environment_allowlist),
        "sandbox_policy_id": identity.sandbox_policy_id,
        "status": status,
        "reason_codes": list(safe_reasons),
    }
    if invocation.bundle_path is not None and invocation.bundle_identity is not None:
        bundle = invocation.bundle_identity
        record["bundle"] = {
            "sha256": bundle.sha256,
            "device": bundle.device,
            "inode": bundle.inode,
            "size": bundle.size,
            "mtime_ns": bundle.mtime_ns,
            "path_sha256": hashlib.sha256(
                str(invocation.bundle_path).encode("utf-8")
            ).hexdigest(),
        }
    return record


def capture_cursor_identity(path: Path) -> FileIdentity:
    """Capture a trusted identity for an offline pin; reuse it for live runs."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise CursorIdentityError("Cursor executable path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CursorIdentityError("Cursor executable is unavailable") from exc
    return _capture_identity(resolved)


def _profile_from_manifest(manifest: Manifest) -> CursorProfile | None:
    if not isinstance(manifest, Manifest):
        return None
    identity = manifest.identity
    for profile, (harness_id, transport, policy_id) in _PROFILES.items():
        if (
            identity.harness_id == harness_id
            and identity.permission_profile == "read-only"
            and identity.prompt_transport == transport
            and identity.sandbox_policy_id == policy_id
        ):
            return cast(CursorProfile, profile)
    return None


def _decision(
    manifest: Manifest,
    profile: CursorProfile,
    status: CursorStatus,
    reasons: Sequence[str],
    receipt: Receipt | None,
) -> CursorDecision:
    return CursorDecision(
        manifest.identity.harness_id,
        manifest.identity.permission_profile,
        profile,
        status,
        tuple(reasons),
        receipt,
    )


def _safe_reason_codes(reason_codes: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for reason in reason_codes:
        if not isinstance(reason, str) or _REASON_CODE.fullmatch(reason) is None:
            raise CursorProbeError("Cursor reason code is invalid")
        result.append(reason)
    return tuple(result)


def _capture_identity(path: Path, *, require_executable: bool = True) -> FileIdentity:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(path, flags)
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode) or (
            require_executable and not os.access(path, os.X_OK)
        ):
            raise CursorIdentityError("Cursor file is not a regular executable")
        digest_builder = hashlib.sha256()
        while True:
            chunk = os.read(file_descriptor, 65_536)
            if not chunk:
                break
            digest_builder.update(chunk)
        after = os.fstat(file_descriptor)
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise CursorIdentityError("Cursor file changed while reading")
        identity = FileIdentity(
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            digest_builder.hexdigest(),
        )
    except CursorIdentityError:
        raise
    except OSError as exc:
        raise CursorIdentityError("Cursor file cannot be inspected") from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
    return identity


def _validate_prompt(prompt: str) -> None:
    if not isinstance(prompt, str) or not prompt.strip():
        raise CursorProbeError("Cursor prompt must not be empty")
    try:
        prompt_bytes = len(prompt.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise CursorProbeError("Cursor prompt contains invalid Unicode") from exc
    if prompt_bytes > MAX_PROMPT_BYTES:
        raise CursorProbeError("Cursor prompt exceeds the byte limit")


def _validate_timeout(timeout_seconds: float) -> None:
    if not isinstance(timeout_seconds, (int, float)) or isinstance(
        timeout_seconds, bool
    ):
        raise CursorExecutionError("Cursor timeout must be numeric")
    if timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise CursorExecutionError("Cursor timeout is outside the bounded range")


def _require_version_result(result: ProcessResult, expected: str) -> str:
    if result.timed_out:
        raise CursorExecutionError("Cursor version probe timed out")
    if result.returncode != 0:
        raise CursorExecutionError("Cursor version probe failed")
    if not result.stdout.strip() or not _exact_version_present(result.stdout, expected):
        raise CursorIdentityError("pinned Cursor executable version changed")
    return expected


def _exact_version_present(output: str, version: str) -> bool:
    return re.search(rf"(?<![0-9]){re.escape(version)}(?![0-9])", output) is not None
