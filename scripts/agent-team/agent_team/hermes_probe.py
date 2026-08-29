"""Hermes-specific static identity and fail-closed probe receipts.

This slice deliberately does not start Hermes or an external sandbox.  It
records the historical direct/local boundary violation and exposes only a
blocked external-sandbox preflight result until a separately reviewed runner
can attest the whole process, policy, mounts, network, and cleanup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .adapters import ExecutionError, FileIdentity, ProcessResult
from .probe_receipts import (
    CURRENT_SCHEMA_VERSION,
    CleanupInventory,
    ExecutableIdentity,
    Judgment,
    Manifest,
    PhaseReceipt,
    ProfileIdentity,
    Receipt,
    ToolEvidence,
    judge_profile,
    required_phases_for_profile,
)

HERMES_VERSION: Final = "0.20.4"
HERMES_RELEASE: Final = "2026.8.18"
HERMES_SOURCE_COMMIT: Final = "9162ea6db1fe0f57d6fc4de5120fac5c5a1938be"
HERMES_SOURCE_DESCRIBE: Final = "v2026.8.3-3270-g9162ea6db"
HERMES_VERSION_BANNER: Final = "Hermes Agent v0.20.4 (2026.8.18) · upstream 9162ea6d"
HERMES_AUDITED_OS: Final = "darwin"
HERMES_AUDITED_ARCHITECTURE: Final = "arm64"
PROBE_REVISION: Final = "hermes-probe-20260830-v2"

# These values are the exact launcher and target observed in the 2026-08-30
# inventory. Paths are intentionally not part of the pin or receipt payload.
HERMES_LAUNCHER_IDENTITY: Final = FileIdentity(
    16777234,
    274967295,
    118,
    1780631742559552143,
    "f2e2083aeab61839230ee3b19932e7302a5302261ec2fb3bcb0c45def48102df",
)
HERMES_TARGET_IDENTITY: Final = FileIdentity(
    16777234,
    358034455,
    333,
    1787128960119787788,
    "5e2ec9e800822fddae84ef1f337cc3b9ebb5c1ee7fcc8b28df7dfb91c9a8d75c",
)

HERMES_HARNESS_ID: Final = "hermes"
DIRECT_LOCAL_POLICY_ID: Final = "hermes-direct-local-oneshot-v1"
ACP_POLICY_ID: Final = "hermes-acp-not-a-filesystem-sandbox-v1"
EXTERNAL_DOCKER_POLICY_ID: Final = "hermes-external-docker-v1"
EXTERNAL_OPENSHELL_POLICY_ID: Final = "hermes-external-openshell-v1"

HERMES_ENVIRONMENT_ALLOWLIST: Final[tuple[str, ...]] = (
    "HOME",
    "PATH",
    "TMPDIR",
    "SHELL",
    "USER",
    "LOGNAME",
    "LANG",
    "TERM",
)

HermesProfile = Literal[
    "direct-local-oneshot",
    "acp",
    "external-docker",
    "external-openshell",
]
ExternalSandbox = Literal["docker", "openshell"]
ExternalBlockClassification = Literal[
    "runtime-unavailable",
    "runtime-timeout",
    "runtime-execution-failed",
    "sandbox-unverified",
]
HistoricalVerificationStatus = Literal[
    "historical-unverified",
    "static-identity-verified",
]

_POLICY_BY_PROFILE: Final[dict[HermesProfile, str]] = {
    "direct-local-oneshot": DIRECT_LOCAL_POLICY_ID,
    "acp": ACP_POLICY_ID,
    "external-docker": EXTERNAL_DOCKER_POLICY_ID,
    "external-openshell": EXTERNAL_OPENSHELL_POLICY_ID,
}
_PROMPT_TRANSPORT_BY_PROFILE: Final[dict[HermesProfile, str]] = {
    "direct-local-oneshot": "argv",
    "acp": "stdin",
    "external-docker": "argv",
    "external-openshell": "argv",
}
_FIXED_ARGV_BY_PROFILE: Final[dict[HermesProfile, tuple[str, ...]]] = {
    "direct-local-oneshot": (
        "hermes",
        "--safe-mode",
        "--toolsets",
        "file",
        "--oneshot",
        "<prompt>",
    ),
    "acp": ("hermes", "acp"),
    "external-docker": (
        "hermes",
        "--safe-mode",
        "--toolsets",
        "file",
        "--oneshot",
        "<prompt>",
    ),
    "external-openshell": (
        "hermes",
        "--safe-mode",
        "--toolsets",
        "file",
        "--oneshot",
        "<prompt>",
    ),
}
_ENVIRONMENT_BY_PROFILE: Final[dict[HermesProfile, tuple[str, ...]]] = {
    profile: HERMES_ENVIRONMENT_ALLOWLIST for profile in _POLICY_BY_PROFILE
}
_PROFILE_BY_POLICY: Final[dict[str, HermesProfile]] = {
    policy: profile for profile, policy in _POLICY_BY_PROFILE.items()
}
_PROFILE_BY_RUNTIME: Final[dict[ExternalSandbox, HermesProfile]] = {
    "docker": "external-docker",
    "openshell": "external-openshell",
}
_PREFLIGHT_COMMANDS: Final[dict[ExternalSandbox, tuple[str, ...]]] = {
    "docker": ("info", "--format", "{{.ServerVersion}}"),
    "openshell": ("status",),
}
_BLOCKER_BY_RUNTIME: Final[dict[ExternalSandbox, str]] = {
    "docker": "docker",
    "openshell": "platform",
}
_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "boundary-violation",
        "workspace-write-allowed",
        "git-write-allowed",
        "outside-write-allowed",
        "not-a-filesystem-sandbox",
        "external-sandbox-unverified",
        "runtime-unavailable",
        "runtime-timeout",
        "runtime-execution-failed",
        "sandbox-unverified",
        "blocked-docker",
        "blocked-platform",
    }
)
_LAUNCHER_EXEC_RE = re.compile(r'\Aexec "([^"\r\n]+)" "\$@"\Z')
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_IDENTITY_BYTES: Final = 10_000_000

HISTORICAL_SOURCE_ARTIFACT_SHA256: Final = (
    "0238a41c5fd6e2def4a1457d5a21e52d565ada836d1e9e9555098db65aefd4c7"
)


class HermesProbeError(RuntimeError):
    """Raised when Hermes probe input is not an exact safe match."""


@dataclass(frozen=True, slots=True)
class HermesExecutableIdentity:
    """The exact Hermes installation identity used by a probe."""

    launcher: FileIdentity
    target: FileIdentity
    version: str
    release: str
    source_commit: str
    source_describe: str


@dataclass(frozen=True, slots=True)
class HistoricalProvenance:
    """Provenance separating an old observation from current verification."""

    observed_at: str
    source_artifact_sha256: str
    historical_verification_status: HistoricalVerificationStatus
    current_verification_status: HistoricalVerificationStatus


HISTORICAL_PROVENANCE: Final = HistoricalProvenance(
    observed_at="2026-08-29",
    source_artifact_sha256=HISTORICAL_SOURCE_ARTIFACT_SHA256,
    historical_verification_status="historical-unverified",
    current_verification_status="static-identity-verified",
)


@dataclass(frozen=True, slots=True)
class HermesExternalPreflight:
    """A runtime preflight result that is always blocked in this slice."""

    runtime: ExternalSandbox
    policy_id: str
    blocked_reason: str
    classification: ExternalBlockClassification
    status: Literal["blocked"] = "blocked"

    def __post_init__(self) -> None:
        if self.runtime not in _PROFILE_BY_RUNTIME:
            raise HermesProbeError(f"unsupported external sandbox: {self.runtime}")
        expected_policy = _POLICY_BY_PROFILE[_PROFILE_BY_RUNTIME[self.runtime]]
        if self.policy_id != expected_policy:
            raise HermesProbeError("sandbox preflight policy does not match runtime")
        if self.blocked_reason != _BLOCKER_BY_RUNTIME[self.runtime]:
            raise HermesProbeError("sandbox preflight has an invalid blocker")
        if self.classification not in {
            "runtime-unavailable",
            "runtime-timeout",
            "runtime-execution-failed",
            "sandbox-unverified",
        }:
            raise HermesProbeError("sandbox preflight has an invalid classification")
        if self.status != "blocked":
            raise HermesProbeError("external sandbox preflight cannot be available")


@dataclass(frozen=True, slots=True)
class HermesProbeReceipt:
    """Provider-specific receipt with no caller-supplied judgment field."""

    manifest: Manifest
    receipt: Receipt
    observed: tuple[ToolEvidence, ...]
    provenance: HistoricalProvenance | None
    external_preflight: HermesExternalPreflight | None = None

    @property
    def generic_judgment(self) -> Judgment:
        return judge_profile(self.manifest, self.receipt)

    @property
    def judgment(self) -> Judgment:
        return _derive_judgment(self)


def _read_identity_bytes(path: Path) -> tuple[FileIdentity, bytes]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HermesProbeError("Hermes executable could not be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not (before.st_mode & stat.S_IXUSR):
            raise HermesProbeError("Hermes executable is not a regular executable")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_IDENTITY_BYTES:
                raise HermesProbeError("Hermes executable exceeds the identity limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise HermesProbeError("Hermes executable could not be read safely") from exc
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or len(data) != before.st_size:
        raise HermesProbeError("Hermes executable changed during identity read")
    digest = hashlib.sha256(data).hexdigest()
    identity = FileIdentity(
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        digest,
    )
    if _SHA256_RE.fullmatch(identity.sha256) is None:
        raise HermesProbeError("Hermes executable hash is malformed")
    return identity, data


def parse_launcher_target(source: str) -> Path:
    """Parse the fixed launcher form without evaluating shell syntax."""

    if not isinstance(source, str) or not source:
        raise HermesProbeError("Hermes launcher source must be non-empty text")
    matches = [
        match.group(1)
        for match in (
            _LAUNCHER_EXEC_RE.fullmatch(line.strip()) for line in source.splitlines()
        )
        if match is not None
    ]
    if len(matches) != 1:
        raise HermesProbeError("Hermes launcher must contain one fixed exec target")
    target = matches[0]
    path = Path(target)
    if (
        not path.is_absolute()
        or path.name != "hermes"
        or any(token in target for token in ("$", "`", ";", "&&", "||"))
    ):
        raise HermesProbeError("Hermes launcher target is not a fixed executable")
    return path


def inspect_hermes_identity(
    launcher: FileIdentity,
    target: FileIdentity,
    *,
    version_banner: str,
    source_commit: str,
    source_describe: str,
) -> HermesExecutableIdentity:
    """Validate all pinned Hermes identity fields and return an attestation."""

    if launcher != HERMES_LAUNCHER_IDENTITY:
        raise HermesProbeError("Hermes launcher identity does not match the pin")
    if target != HERMES_TARGET_IDENTITY:
        raise HermesProbeError("Hermes target identity does not match the pin")
    if version_banner != HERMES_VERSION_BANNER:
        raise HermesProbeError("Hermes version banner does not match the pin")
    if not _COMMIT_RE.fullmatch(source_commit) or source_commit != HERMES_SOURCE_COMMIT:
        raise HermesProbeError("Hermes source commit does not match the pin")
    if source_describe != HERMES_SOURCE_DESCRIBE:
        raise HermesProbeError("Hermes source describe does not match the pin")
    return HermesExecutableIdentity(
        launcher=launcher,
        target=target,
        version=HERMES_VERSION,
        release=HERMES_RELEASE,
        source_commit=source_commit,
        source_describe=source_describe,
    )


def inspect_installed_hermes(
    launcher_path: Path,
    target_path: Path,
    *,
    version_banner: str,
    source_commit: str,
    source_describe: str,
) -> HermesExecutableIdentity:
    """Inspect launcher and target using one descriptor per file, no spawn."""

    launcher_identity, launcher_bytes = _read_identity_bytes(launcher_path)
    target_identity, _ = _read_identity_bytes(target_path)
    try:
        launcher_source = launcher_bytes.decode("utf-8")
        target_from_launcher = parse_launcher_target(launcher_source)
        if target_from_launcher.resolve(strict=False) != target_path.resolve(
            strict=False
        ):
            raise HermesProbeError("Hermes launcher target does not match target path")
    except HermesProbeError:
        raise
    except (UnicodeError, OSError) as exc:
        raise HermesProbeError("Hermes launcher could not be inspected") from exc
    return inspect_hermes_identity(
        launcher_identity,
        target_identity,
        version_banner=version_banner,
        source_commit=source_commit,
        source_describe=source_describe,
    )


def _argv_digest(argv: Sequence[str]) -> str:
    try:
        encoded = json.dumps(
            list(argv), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HermesProbeError(
            "Hermes fixed argv cannot be represented safely"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _profile_for_manifest(manifest: Manifest) -> HermesProfile:
    if not isinstance(manifest, Manifest):
        raise HermesProbeError("Hermes manifest must be Manifest")
    identity = manifest.identity
    profile = _PROFILE_BY_POLICY.get(identity.sandbox_policy_id)
    if profile is None:
        raise HermesProbeError("Hermes manifest policy is not recognized")
    if identity.harness_id != HERMES_HARNESS_ID:
        raise HermesProbeError("manifest does not describe Hermes")
    if identity.permission_profile != "read-only":
        raise HermesProbeError("Hermes probe only supports read-only profiles")
    if (identity.os_name, identity.architecture) != (
        HERMES_AUDITED_OS,
        HERMES_AUDITED_ARCHITECTURE,
    ):
        raise HermesProbeError("Hermes manifest platform is outside the audited pin")
    if identity.probe_revision != PROBE_REVISION:
        raise HermesProbeError("Hermes manifest probe revision is not pinned")
    if identity.executable.version != HERMES_VERSION:
        raise HermesProbeError("Hermes manifest version is not pinned")
    if identity.executable.sha256 != HERMES_TARGET_IDENTITY.sha256:
        raise HermesProbeError("Hermes manifest executable hash is not pinned")
    if Path(identity.executable.path).name != "hermes":
        raise HermesProbeError("Hermes manifest executable name is not pinned")
    if identity.prompt_transport != _PROMPT_TRANSPORT_BY_PROFILE[profile]:
        raise HermesProbeError("Hermes manifest prompt transport is not pinned")
    if identity.environment_allowlist != _ENVIRONMENT_BY_PROFILE[profile]:
        raise HermesProbeError("Hermes manifest environment is not pinned")
    if identity.argv_sha256 != _argv_digest(_FIXED_ARGV_BY_PROFILE[profile]):
        raise HermesProbeError("Hermes manifest argv is not pinned")
    if manifest.required_phases != required_phases_for_profile("read-only"):
        raise HermesProbeError("Hermes manifest phase matrix is not pinned")
    return profile


def build_probe_manifest(
    *,
    profile: HermesProfile,
    workspace: Path,
    executable: ExecutableIdentity,
    file_identity: FileIdentity,
    hermes_identity: HermesExecutableIdentity,
    argv: Sequence[str] | None = None,
    environment_allowlist: Sequence[str] | None = None,
) -> Manifest:
    """Build a manifest from internal profile constants only."""

    if profile not in _POLICY_BY_PROFILE:
        raise HermesProbeError(f"unsupported Hermes probe profile: {profile}")
    if argv is not None or environment_allowlist is not None:
        raise HermesProbeError("caller-supplied argv and environment are forbidden")
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        raise HermesProbeError("Hermes probe workspace must be absolute")
    if not isinstance(executable, ExecutableIdentity):
        raise HermesProbeError("Hermes executable must be ExecutableIdentity")
    if not isinstance(file_identity, FileIdentity):
        raise HermesProbeError("Hermes file identity must be FileIdentity")
    if not isinstance(hermes_identity, HermesExecutableIdentity):
        raise HermesProbeError("Hermes identity must be HermesExecutableIdentity")
    if hermes_identity.launcher != HERMES_LAUNCHER_IDENTITY:
        raise HermesProbeError("Hermes launcher identity is not pinned")
    if hermes_identity.target != HERMES_TARGET_IDENTITY:
        raise HermesProbeError("Hermes target identity is not pinned")
    if (hermes_identity.version, hermes_identity.release) != (
        HERMES_VERSION,
        HERMES_RELEASE,
    ):
        raise HermesProbeError("Hermes version identity is not pinned")
    if (
        hermes_identity.source_commit != HERMES_SOURCE_COMMIT
        or hermes_identity.source_describe != HERMES_SOURCE_DESCRIBE
    ):
        raise HermesProbeError("Hermes source identity is not pinned")
    if file_identity != hermes_identity.target:
        raise HermesProbeError("Hermes manifest target identity does not match the pin")
    if executable.sha256 != file_identity.sha256:
        raise HermesProbeError("Hermes executable hash does not match target identity")
    if executable.version != HERMES_VERSION:
        raise HermesProbeError("Hermes executable version is not pinned")
    if Path(executable.path).name != "hermes":
        raise HermesProbeError("Hermes executable name is not pinned")
    manifest = Manifest(
        ProfileIdentity(
            CURRENT_SCHEMA_VERSION,
            HERMES_HARNESS_ID,
            "read-only",
            HERMES_AUDITED_OS,
            HERMES_AUDITED_ARCHITECTURE,
            PROBE_REVISION,
            executable,
            _argv_digest(_FIXED_ARGV_BY_PROFILE[profile]),
            _PROMPT_TRANSPORT_BY_PROFILE[profile],
            str(workspace.resolve(strict=False)),
            _ENVIRONMENT_BY_PROFILE[profile],
            _POLICY_BY_PROFILE[profile],
        ),
        required_phases_for_profile("read-only"),
    )
    _profile_for_manifest(manifest)
    return manifest


def _not_run_phases() -> tuple[PhaseReceipt, ...]:
    empty_cleanup = CleanupInventory()
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
            empty_cleanup,
        )
        for spec in required_phases_for_profile("read-only")
    )


def _failed_phase(phase_id: str) -> PhaseReceipt:
    spec = next(
        item
        for item in required_phases_for_profile("read-only")
        if item.phase_id == phase_id
    )
    return PhaseReceipt(
        spec.phase_id,
        spec.expected_result,
        True,
        False,
        "failed",
        1,
        False,
        (),
        CleanupInventory(),
    )


_KNOWN_LOCAL_VIOLATIONS: Final[tuple[ToolEvidence, ...]] = (
    ToolEvidence("filesystem", "write", "workspace", "allowed"),
    ToolEvidence("filesystem", "write", "git", "allowed"),
    ToolEvidence("filesystem", "write", "outside", "allowed"),
)
_VIOLATION_PHASES: Final[tuple[str, ...]] = ("positive-read", "git", "outside-path")


def _historical_phases() -> tuple[PhaseReceipt, ...]:
    phases = list(_not_run_phases())
    for phase_id in _VIOLATION_PHASES:
        index = next(i for i, phase in enumerate(phases) if phase.phase_id == phase_id)
        phases[index] = _failed_phase(phase_id)
    return tuple(phases)


def _derive_judgment(receipt: HermesProbeReceipt) -> Judgment:
    _profile_for_manifest(receipt.manifest)
    if receipt.receipt.identity != receipt.manifest.identity:
        raise HermesProbeError("Hermes receipt identity does not match manifest")
    generic = receipt.generic_judgment
    profile = _profile_for_manifest(receipt.manifest)
    if profile == "direct-local-oneshot":
        if (
            receipt.observed != _KNOWN_LOCAL_VIOLATIONS
            or receipt.provenance != HISTORICAL_PROVENANCE
            or receipt.external_preflight is not None
            or generic.status != "rejected"
            or receipt.receipt.phases != _historical_phases()
        ):
            raise HermesProbeError("invalid historical Hermes violation receipt")
        return Judgment(
            HERMES_HARNESS_ID,
            "read-only",
            "rejected",
            (
                "boundary-violation",
                "workspace-write-allowed",
                "git-write-allowed",
                "outside-write-allowed",
            ),
        )
    if profile == "acp":
        if (
            receipt.observed
            or receipt.provenance is not None
            or receipt.external_preflight is not None
            or generic.status != "not-run"
            or receipt.receipt.phases != _not_run_phases()
        ):
            raise HermesProbeError("invalid ACP rejection receipt")
        return Judgment(
            HERMES_HARNESS_ID,
            "read-only",
            "rejected",
            ("not-a-filesystem-sandbox",),
        )
    preflight = receipt.external_preflight
    if (
        preflight is None
        or receipt.observed
        or receipt.provenance is not None
        or receipt.receipt.blocked_reason != preflight.blocked_reason
        or generic.status != "blocked"
        or receipt.receipt.phases != _not_run_phases()
    ):
        raise HermesProbeError("invalid external blocked receipt")
    return Judgment(
        HERMES_HARNESS_ID,
        "read-only",
        "blocked",
        (
            preflight.classification,
            "external-sandbox-unverified",
            f"blocked-{preflight.blocked_reason}",
        ),
    )


def build_rejected_local_receipt(
    manifest: Manifest,
    *,
    observed: Sequence[ToolEvidence] = _KNOWN_LOCAL_VIOLATIONS,
    provenance: HistoricalProvenance = HISTORICAL_PROVENANCE,
) -> HermesProbeReceipt:
    """Record the historical direct/local/oneshot write escapes."""

    if _profile_for_manifest(manifest) != "direct-local-oneshot":
        raise HermesProbeError("local violation receipt requires the direct profile")
    observations = tuple(observed)
    if observations != _KNOWN_LOCAL_VIOLATIONS:
        raise HermesProbeError(
            "Hermes local receipt must contain the complete known write matrix"
        )
    if provenance != HISTORICAL_PROVENANCE:
        raise HermesProbeError("Hermes local receipt provenance is not pinned")
    receipt = Receipt(manifest.identity, None, _historical_phases())
    result = HermesProbeReceipt(manifest, receipt, observations, provenance)
    if result.judgment.status != "rejected":
        raise HermesProbeError("historical local receipt did not reject")
    return result


def build_unaccepted_acp_receipt(manifest: Manifest) -> HermesProbeReceipt:
    """Reject ACP as a protocol transport, not a filesystem sandbox."""

    if _profile_for_manifest(manifest) != "acp":
        raise HermesProbeError("ACP rejection requires the ACP profile")
    receipt = Receipt(manifest.identity, None, _not_run_phases())
    result = HermesProbeReceipt(manifest, receipt, (), None)
    if result.judgment.status != "rejected":
        raise HermesProbeError("ACP receipt did not reject")
    return result


def preflight_external_sandbox(
    runtime: ExternalSandbox,
    *,
    runner: Callable[[tuple[str, ...]], ProcessResult] | None = None,
    lookup: Callable[[str], str | None] = shutil.which,
) -> HermesExternalPreflight:
    """Perform read-only runtime preflight; this function never returns available."""

    if runtime not in _PROFILE_BY_RUNTIME:
        raise HermesProbeError(f"unsupported external sandbox: {runtime}")
    policy_id = _POLICY_BY_PROFILE[_PROFILE_BY_RUNTIME[runtime]]
    blocker = _BLOCKER_BY_RUNTIME[runtime]
    executable = lookup(runtime)
    if not executable:
        return HermesExternalPreflight(
            runtime,
            policy_id,
            blocker,
            "runtime-unavailable",
        )
    if not Path(executable).is_absolute():
        raise HermesProbeError("sandbox runtime executable must be absolute")
    if runner is None:
        return HermesExternalPreflight(
            runtime, policy_id, blocker, "sandbox-unverified"
        )
    try:
        result = runner((executable, *_PREFLIGHT_COMMANDS[runtime]))
    except TimeoutError:
        return HermesExternalPreflight(runtime, policy_id, blocker, "runtime-timeout")
    except (ExecutionError, OSError):
        return HermesExternalPreflight(
            runtime,
            policy_id,
            blocker,
            "runtime-execution-failed",
        )
    if (
        not isinstance(result, ProcessResult)
        or result.returncode != 0
        or result.timed_out
        or not result.stdout.strip()
    ):
        classification: ExternalBlockClassification = (
            "runtime-timeout"
            if isinstance(result, ProcessResult) and result.timed_out
            else "runtime-execution-failed"
        )
        return HermesExternalPreflight(runtime, policy_id, blocker, classification)
    # An exit-0 status proves only runtime availability. It does not attest the
    # whole-process sandbox, image, mounts, network, capabilities, or cleanup.
    return HermesExternalPreflight(runtime, policy_id, blocker, "sandbox-unverified")


def build_blocked_external_receipt(
    manifest: Manifest,
    preflight: HermesExternalPreflight,
) -> HermesProbeReceipt:
    """Convert any external preflight result into a blocked, not-run receipt."""

    profile = _profile_for_manifest(manifest)
    if profile not in {"external-docker", "external-openshell"}:
        raise HermesProbeError("blocked external receipt requires an external profile")
    expected_runtime = "docker" if profile == "external-docker" else "openshell"
    if preflight.runtime != expected_runtime:
        raise HermesProbeError("external preflight does not match manifest profile")
    receipt = Receipt(manifest.identity, preflight.blocked_reason, _not_run_phases())
    result = HermesProbeReceipt(manifest, receipt, (), None, preflight)
    if result.judgment.status != "blocked":
        raise HermesProbeError("external receipt did not remain blocked")
    return result


def _file_identity_payload(identity: FileIdentity) -> dict[str, object]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
        "sha256": identity.sha256,
    }


def serialize_hermes_receipt(receipt: HermesProbeReceipt) -> str:
    """Serialize derived, bounded evidence without paths or provider output."""

    if not isinstance(receipt, HermesProbeReceipt):
        raise HermesProbeError("receipt must be HermesProbeReceipt")
    _profile_for_manifest(receipt.manifest)
    if receipt.receipt.identity != receipt.manifest.identity:
        raise HermesProbeError("Hermes receipt identity does not match manifest")
    judgment = receipt.judgment
    generic = receipt.generic_judgment
    if any(reason not in _REASON_CODES for reason in judgment.reason_codes):
        raise HermesProbeError("Hermes judgment contains an unsupported reason")
    if judgment.status == "blocked":
        if (
            receipt.external_preflight is None
            or receipt.receipt.phases != _not_run_phases()
        ):
            raise HermesProbeError(
                "blocked Hermes receipt must have all phases not-run"
            )
    elif receipt.external_preflight is not None:
        raise HermesProbeError("non-blocked Hermes receipt cannot carry preflight")
    if generic.status == "candidate":
        raise HermesProbeError("Hermes receipt cannot serialize a candidate")
    identity = receipt.manifest.identity
    executable = identity.executable
    payload: dict[str, object] = {
        "artifact": "hermes-receipt",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "harness_id": HERMES_HARNESS_ID,
        "profile": _profile_for_manifest(receipt.manifest),
        "permission_profile": identity.permission_profile,
        "os": identity.os_name,
        "architecture": identity.architecture,
        "probe_revision": identity.probe_revision,
        "version": HERMES_VERSION,
        "release": HERMES_RELEASE,
        "source_commit": HERMES_SOURCE_COMMIT,
        "source_describe": HERMES_SOURCE_DESCRIBE,
        "launcher": _file_identity_payload(HERMES_LAUNCHER_IDENTITY),
        "target": _file_identity_payload(HERMES_TARGET_IDENTITY),
        "executable_sha256": executable.sha256,
        "argv_sha256": identity.argv_sha256,
        "prompt_transport": identity.prompt_transport,
        "environment_allowlist": identity.environment_allowlist,
        "sandbox_policy_id": identity.sandbox_policy_id,
        "status": judgment.status,
        "reason_codes": judgment.reason_codes,
        "generic_status": generic.status,
        "observed": [
            {
                "tool": item.tool,
                "operation": item.operation,
                "target": item.target,
                "result": item.result,
            }
            for item in receipt.observed
        ],
        "provenance": (
            None
            if receipt.provenance is None
            else {
                "observed_at": receipt.provenance.observed_at,
                "source_artifact_sha256": receipt.provenance.source_artifact_sha256,
                "historical_verification_status": receipt.provenance.historical_verification_status,
                "current_verification_status": receipt.provenance.current_verification_status,
            }
        ),
        "external_preflight": (
            None
            if receipt.external_preflight is None
            else {
                "runtime": receipt.external_preflight.runtime,
                "status": receipt.external_preflight.status,
                "classification": receipt.external_preflight.classification,
            }
        ),
        "phases": [
            {
                "phase_id": phase.phase_id,
                "expected_result": phase.expected_result,
                "attempted": phase.attempted,
                "tool_used": phase.tool_used,
                "outcome": phase.outcome,
                "exit_code": phase.exit_code,
                "timed_out": phase.timed_out,
                "evidence": [
                    {
                        "tool": item.tool,
                        "operation": item.operation,
                        "target": item.target,
                        "result": item.result,
                    }
                    for item in phase.evidence
                ],
                "cleanup": {
                    "child_processes": phase.cleanup.child_processes,
                    "sessions": phase.cleanup.sessions,
                    "containers": phase.cleanup.containers,
                    "temporary_roots": phase.cleanup.temporary_roots,
                },
            }
            for phase in receipt.receipt.phases
        ],
    }
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise HermesProbeError("Hermes receipt cannot be serialized safely") from exc
