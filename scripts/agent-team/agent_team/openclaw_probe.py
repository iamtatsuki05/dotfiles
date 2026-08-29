"""Read-only identity and Docker preflight for OpenClaw sandbox cells.

The current repository has no audited Docker image or endpoint pin.  This
module therefore stops at a blocked/not-run receipt and has no container,
cleanup, image-pull, provider-turn, or command-runner API.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Literal, NoReturn

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
    judge_profile,
    required_phases_for_profile,
    serialize_manifest,
    serialize_receipt,
)

OPENCLAW_VERSION: Final = "2026.7.1"
OPENCLAW_BUILD: Final = "2d2ddc4"
OPENCLAW_EXECUTABLE_SHA256: Final = (
    "f643b005d6db233a0b45204e8d8e943256874ccc6897b8a6e0cf42a9b376a188"
)
OPENCLAW_PROBE_REVISION: Final = "openclaw-docker-probe-20260830-v1"
OPENCLAW_SANDBOX_POLICY_ID: Final = "openclaw-docker-sandbox-v1"
REDACTED_EXECUTABLE_PATH: Final = "/redacted/openclaw/2026.7.1/openclaw.mjs"
REDACTED_WORKSPACE_PATH: Final = "/redacted/openclaw/probe-workspace"
OPENCLAW_STATIC_ARGV_DIGEST: Final = hashlib.sha256(b"[]").hexdigest()

# No full repository@digest or endpoint has been audited yet.  Keeping these
# pins absent makes every unreviewed caller-supplied value fail closed.
AUDITED_OPENCLAW_IMAGE_PIN: Final[str | None] = None
AUDITED_DOCKER_CONTEXT: Final[str | None] = None
AUDITED_DOCKER_ENDPOINT_SHA256: Final[str | None] = None

CellId = Literal["direct-sandbox-off", "docker-read-only", "docker-workspace-write"]
ReceiptProfile = Literal["read-only", "workspace-write"]
DockerStatus = Literal["blocked"]

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTEXT = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_IMAGE = re.compile(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}\Z")


class OpenClawProbeError(RuntimeError):
    """Raised when an OpenClaw identity or safety policy is unverified."""


class OpenClawProbeStatus(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    NOT_RUN = "not-run"


def _fail(message: str) -> NoReturn:
    raise OpenClawProbeError(message)


@dataclass(frozen=True, slots=True)
class FileAttestation:
    """Identity of the regular file read through one open file descriptor."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.device, "device"),
            (self.inode, "inode"),
            (self.size, "size"),
            (self.mtime_ns, "mtime_ns"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                _fail(f"file attestation {field_name} is invalid")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            _fail("file attestation SHA-256 is invalid")

    @classmethod
    def from_stat(cls, value: os.stat_result, sha256: str) -> FileAttestation:
        return cls(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, sha256)


def _path_signature(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _canonical_regular_executable(executable: Path) -> Path:
    if not isinstance(executable, Path) or not executable.is_absolute():
        _fail("OpenClaw executable must be an absolute canonical path")
    try:
        canonical = executable.resolve(strict=True)
        value = os.stat(executable, follow_symlinks=False)
    except OSError as exc:
        raise OpenClawProbeError("OpenClaw executable could not be inspected") from exc
    if executable != canonical or stat.S_ISLNK(value.st_mode):
        _fail("OpenClaw executable must be an absolute canonical path")
    if canonical.name not in {"openclaw", "openclaw.mjs"}:
        _fail("OpenClaw executable has an unexpected canonical name")
    if not stat.S_ISREG(value.st_mode):
        _fail("OpenClaw executable must be a regular file")
    if not value.st_mode & 0o111:
        _fail("OpenClaw executable must be executable")
    return canonical


def _read_executable_attestation(executable: Path) -> FileAttestation:
    canonical = _canonical_regular_executable(executable)
    try:
        before = os.stat(canonical, follow_symlinks=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(canonical, flags)
    except OSError as exc:
        raise OpenClawProbeError("OpenClaw executable could not be opened") from exc
    descriptor_to_close = descriptor
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor_to_close = -1
            opened = os.fstat(stream.fileno())
            if _path_signature(before) != _path_signature(opened):
                _fail("OpenClaw executable changed before hashing")
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after_fd = os.fstat(stream.fileno())
            if _path_signature(opened) != _path_signature(after_fd):
                _fail("OpenClaw executable changed while hashing")
    except OSError as exc:
        raise OpenClawProbeError("OpenClaw executable could not be hashed") from exc
    finally:
        if descriptor_to_close != -1:
            os.close(descriptor_to_close)
    try:
        after_path = os.stat(canonical, follow_symlinks=False)
    except OSError as exc:
        raise OpenClawProbeError(
            "OpenClaw executable disappeared after hashing"
        ) from exc
    if _path_signature(before) != _path_signature(after_path):
        _fail("OpenClaw executable changed after hashing")
    return FileAttestation.from_stat(after_path, digest.hexdigest())


@dataclass(frozen=True, slots=True)
class OpenClawIdentity:
    """Pinned identity plus a file attestation kept in memory only."""

    path: Path
    version: str
    sha256: str
    build: str
    attestation: FileAttestation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            _fail("OpenClaw executable path must be absolute")
        if self.version != OPENCLAW_VERSION:
            _fail("identity is not the exact OpenClaw 2026.7.1 version")
        if self.build != OPENCLAW_BUILD:
            _fail("identity is not the exact OpenClaw 2026.7.1 build")
        if (
            not isinstance(self.sha256, str)
            or self.sha256 != OPENCLAW_EXECUTABLE_SHA256
            or _SHA256.fullmatch(self.sha256) is None
        ):
            _fail("OpenClaw executable SHA-256 is not the pinned digest")
        if self.attestation is not None:
            if not isinstance(self.attestation, FileAttestation):
                _fail("OpenClaw file attestation has an invalid type")
            if self.attestation.sha256 != self.sha256:
                _fail("OpenClaw file attestation does not match the pinned digest")

    @property
    def version_banner(self) -> str:
        return f"OpenClaw {self.version} ({self.build})"

    def as_receipt_identity(self) -> ExecutableIdentity:
        return ExecutableIdentity(
            REDACTED_EXECUTABLE_PATH,
            self.version_banner,
            self.sha256,
        )


def resolve_openclaw_identity(executable: Path) -> OpenClawIdentity:
    """Inspect one canonical executable without starting a runtime."""

    attestation = _read_executable_attestation(executable)
    if attestation.sha256 != OPENCLAW_EXECUTABLE_SHA256:
        _fail("OpenClaw executable SHA-256 identity drifted")
    return OpenClawIdentity(
        executable,
        OPENCLAW_VERSION,
        attestation.sha256,
        OPENCLAW_BUILD,
        attestation,
    )


def _verify_identity_attestation(identity: OpenClawIdentity) -> None:
    if not isinstance(identity, OpenClawIdentity) or identity.attestation is None:
        raise ReceiptValidationError("OpenClaw identity has no file attestation")
    if identity.sha256 != OPENCLAW_EXECUTABLE_SHA256:
        raise ReceiptValidationError("OpenClaw identity is not pinned")
    try:
        current = _read_executable_attestation(identity.path)
    except OpenClawProbeError as exc:
        raise ReceiptValidationError(
            "OpenClaw identity attestation is invalid"
        ) from exc
    if current != identity.attestation or current.sha256 != identity.sha256:
        raise ReceiptValidationError("OpenClaw identity attestation changed")


@dataclass(frozen=True, slots=True)
class DockerSandboxConfig:
    """Static Docker identity inputs; no mount or execution settings."""

    context: str
    image: str


@dataclass(frozen=True, slots=True)
class DockerPreflight:
    status: DockerStatus
    context: str | None = None
    image_ref: str | None = None
    reason: str | None = None


def _blocked(reason: str, context: str | None = None) -> DockerPreflight:
    return DockerPreflight("blocked", context=context, reason=reason)


def docker_preflight(config: DockerSandboxConfig) -> DockerPreflight:
    """Return a blocked decision until a later slice supplies audited pins."""

    if not isinstance(config, DockerSandboxConfig):
        _fail("Docker preflight config has an invalid type")
    if (
        not isinstance(config.context, str)
        or _CONTEXT.fullmatch(config.context) is None
    ):
        return _blocked("blocked-context", config.context)
    if not isinstance(config.image, str) or _IMAGE.fullmatch(config.image) is None:
        return _blocked("blocked-image", config.context)
    if AUDITED_OPENCLAW_IMAGE_PIN is None or config.image != AUDITED_OPENCLAW_IMAGE_PIN:
        return _blocked("blocked-image", config.context)
    if AUDITED_DOCKER_CONTEXT is None or config.context != AUDITED_DOCKER_CONTEXT:
        return _blocked("blocked-context", config.context)
    if (
        AUDITED_DOCKER_ENDPOINT_SHA256 is None
        or _SHA256.fullmatch(AUDITED_DOCKER_ENDPOINT_SHA256) is None
    ):
        return _blocked("blocked-context-endpoint", config.context)
    return _blocked("docker-runtime-probe-not-implemented", config.context)


@dataclass(frozen=True, slots=True)
class ReceiptBundle:
    """Receipt plus its in-memory attested identity and expected profile."""

    receipt: Receipt
    identity: OpenClawIdentity
    profile: ReceiptProfile

    @property
    def judgment(self) -> Judgment:
        return _validate_receipt_bundle(self)


def _receipt_manifest(identity: OpenClawIdentity, profile: ReceiptProfile) -> Manifest:
    if not isinstance(profile, str) or profile not in {"read-only", "workspace-write"}:
        raise ReceiptValidationError("OpenClaw receipt profile is invalid")
    _verify_identity_attestation(identity)
    return Manifest(
        ProfileIdentity(
            CURRENT_SCHEMA_VERSION,
            "openclaw",
            profile,
            platform.system().lower(),
            platform.machine().lower(),
            OPENCLAW_PROBE_REVISION,
            identity.as_receipt_identity(),
            OPENCLAW_STATIC_ARGV_DIGEST,
            "argv",
            REDACTED_WORKSPACE_PATH,
            ("HOME", "PATH", "TMPDIR"),
            f"{OPENCLAW_SANDBOX_POLICY_ID}-{profile}",
        ),
        required_phases_for_profile(profile),
    )


def build_probe_manifest(
    identity: OpenClawIdentity,
    profile: ReceiptProfile,
) -> Manifest:
    """Build a profile manifest only from a freshly revalidated identity."""

    if not isinstance(identity, OpenClawIdentity):
        raise ReceiptValidationError("OpenClaw manifest identity has an invalid type")
    if not isinstance(profile, str) or profile not in {"read-only", "workspace-write"}:
        raise ReceiptValidationError("OpenClaw manifest profile is invalid")
    return _receipt_manifest(identity, profile)


def serialize_openclaw_manifest(
    identity: OpenClawIdentity, profile: ReceiptProfile
) -> str:
    """Serialize a manifest only after revalidating its file attestation."""

    return serialize_manifest(build_probe_manifest(identity, profile))


def _not_run_phases(profile: ReceiptProfile) -> tuple[PhaseReceipt, ...]:
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
        for spec in required_phases_for_profile(profile)
    )


def _validate_receipt_bundle(bundle: ReceiptBundle) -> Judgment:
    if not isinstance(bundle, ReceiptBundle):
        raise ReceiptValidationError("OpenClaw receipt bundle has an invalid type")
    if not isinstance(bundle.receipt, Receipt):
        raise ReceiptValidationError("OpenClaw receipt has an invalid type")
    if not isinstance(bundle.profile, str) or bundle.profile not in {
        "read-only",
        "workspace-write",
    }:
        raise ReceiptValidationError("OpenClaw receipt profile is invalid")
    manifest = build_probe_manifest(bundle.identity, bundle.profile)
    if bundle.receipt.identity != manifest.identity:
        raise ReceiptValidationError(
            "receipt identity or profile does not match manifest"
        )
    if bundle.receipt.identity.permission_profile != bundle.profile:
        raise ReceiptValidationError("receipt profile does not match bundle profile")
    if any(phase.attempted for phase in bundle.receipt.phases):
        raise ReceiptValidationError(
            "preflight-only OpenClaw receipts cannot contain phase provenance"
        )
    judgment = judge_profile(manifest, bundle.receipt)
    if bundle.receipt.blocked_reason is not None and (
        judgment.status != "blocked"
        or any(
            phase.attempted
            or phase.outcome != "not-run"
            or phase.tool_used
            or phase.evidence
            or phase.cleanup.has_residuals
            for phase in bundle.receipt.phases
        )
    ):
        raise ReceiptValidationError(
            "blocked OpenClaw receipt must contain only not-run phases"
        )
    return judgment


def build_blocked_receipt(
    identity: OpenClawIdentity,
    profile: ReceiptProfile,
    blocked_reason: str = "docker",
) -> ReceiptBundle:
    """Build a redacted, all-not-run receipt for a blocked prerequisite."""

    if not isinstance(blocked_reason, str) or blocked_reason not in BLOCKER_CODES:
        raise ReceiptValidationError("unsupported blocked reason")
    manifest = build_probe_manifest(identity, profile)
    receipt = Receipt(manifest.identity, blocked_reason, _not_run_phases(profile))
    bundle = ReceiptBundle(receipt, identity, profile)
    if bundle.judgment.status != "blocked":
        raise ReceiptValidationError("blocked OpenClaw receipt was not blocked")
    return bundle


def build_not_run_receipt(
    identity: OpenClawIdentity, profile: ReceiptProfile
) -> ReceiptBundle:
    """Build a receipt for prerequisites ready but the safety matrix unrun."""

    manifest = build_probe_manifest(identity, profile)
    receipt = Receipt(manifest.identity, None, _not_run_phases(profile))
    bundle = ReceiptBundle(receipt, identity, profile)
    if bundle.judgment.status != "not-run":
        raise ReceiptValidationError("OpenClaw receipt was unexpectedly run")
    return bundle


def serialize_openclaw_receipt(bundle: ReceiptBundle) -> str:
    """Revalidate identity/profile and recompute judgment before serialization."""

    if not isinstance(bundle, ReceiptBundle):
        raise ReceiptValidationError("OpenClaw receipt bundle has an invalid type")
    judgment = bundle.judgment
    if bundle.receipt.blocked_reason is not None and judgment.status != "blocked":
        raise ReceiptValidationError("blocked receipt judgment changed")
    return serialize_receipt(bundle.receipt)


@dataclass(frozen=True, slots=True)
class OpenClawCell:
    cell_id: CellId
    status: OpenClawProbeStatus
    reason: str
    receipt: ReceiptBundle | None = None


def direct_sandbox_off_cell() -> OpenClawCell:
    """Represent direct/sandbox-off as a non-candidate cell."""

    return OpenClawCell(
        "direct-sandbox-off",
        OpenClawProbeStatus.NOT_RUN,
        "sandbox-off-is-not-a-safe-profile",
    )


@dataclass(frozen=True, slots=True)
class OpenClawPreflightReport:
    status: OpenClawProbeStatus
    identity: OpenClawIdentity
    docker: DockerPreflight
    cells: tuple[OpenClawCell, ...]
    receipts: tuple[ReceiptBundle, ReceiptBundle]

    @property
    def receipt(self) -> ReceiptBundle:
        return self.receipts[0]


class OpenClawProbe:
    """Build static identity and blocked/not-run Docker cell reports."""

    def __init__(
        self,
        executable: Path,
        config: DockerSandboxConfig,
    ) -> None:
        self.executable = executable
        self.config = config

    def preflight(self) -> OpenClawPreflightReport:
        """Verify static file identity and return blocked Docker cells."""

        identity = resolve_openclaw_identity(self.executable)
        docker = docker_preflight(self.config)
        receipts = (
            build_blocked_receipt(identity, "read-only", "docker"),
            build_blocked_receipt(identity, "workspace-write", "docker"),
        )
        read_status = write_status = OpenClawProbeStatus.BLOCKED
        read_reason = write_reason = docker.reason or "docker-preflight-blocked"
        status = OpenClawProbeStatus.BLOCKED
        return OpenClawPreflightReport(
            status,
            identity,
            docker,
            (
                direct_sandbox_off_cell(),
                OpenClawCell("docker-read-only", read_status, read_reason, receipts[0]),
                OpenClawCell(
                    "docker-workspace-write", write_status, write_reason, receipts[1]
                ),
            ),
            receipts,
        )
