"""Static OpenCode identity and blocked/not-run receipt contract.

This slice never starts OpenCode, sends a prompt, parses provider output, or
exposes a live runner.  The authenticated permission matrix belongs to a later
controlled Issue #22 follow-up.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, NoReturn

from .adapters import FileIdentity
from .probe_receipts import (
    CURRENT_SCHEMA_VERSION,
    CleanupInventory,
    ExecutableIdentity,
    Manifest,
    PhaseReceipt,
    ProfileIdentity,
    Receipt,
    judge_profile,
    required_phases_for_profile,
    serialize_manifest,
)

OPENCODE_CANONICAL_RELATIVE_PATH: Final = (
    ".local/share/mise/installs/opencode/1.18.25/opencode"
)
OPENCODE_CANONICAL_PATH: Final = Path.home() / OPENCODE_CANONICAL_RELATIVE_PATH
OPENCODE_VERSION: Final = "1.18.25"
OPENCODE_SHA256: Final = (
    "88eed7b0c2431162422cb0625aa68a55239970446951e4c9aad6a4f1fbc232b9"
)
OPENCODE_IDENTIFIER: Final = "opencode"
PROBE_REVISION: Final = "opencode-static-probe-20260830-v3"

PROFILE_RAW: Final = "raw-workspace-read-only"
PROFILE_SNAPSHOT: Final = "snapshot-read-only"
PROFILES: Final[tuple[str, ...]] = (PROFILE_RAW, PROFILE_SNAPSHOT)
_POLICIES: Final[dict[str, str]] = {
    PROFILE_RAW: "opencode-raw-workspace-readonly-static-v3",
    PROFILE_SNAPSHOT: "opencode-snapshot-readonly-static-v3",
}
_ROLE_TOKENS: Final[dict[str, tuple[str, ...]]] = {
    PROFILE_RAW: (
        "role=reviewer",
        "provider=opencode",
        "transport=direct",
        "permission=read-only",
        "profile=raw-workspace",
        "pure=true",
        "format=json",
        "model=opencode-go/kimi-k2.6",
        "variant=low",
    ),
    PROFILE_SNAPSHOT: (
        "role=reviewer",
        "provider=opencode",
        "transport=direct",
        "permission=read-only",
        "profile=snapshot",
        "pure=true",
        "format=json",
        "model=opencode-go/kimi-k2.6",
        "variant=low",
    ),
}
_ENVIRONMENT_NAMES: Final[tuple[str, ...]] = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "OPENCODE_API_KEY",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
_PROBE_PATH_LABEL: Final = "/probe/opencode"
_CWD_LABELS: Final[dict[str, str]] = {
    PROFILE_RAW: "/probe/opencode/raw-workspace",
    PROFILE_SNAPSHOT: "/probe/opencode/snapshot",
}
_BLOCKED_REASON: Final = "authentication"
HISTORICAL_SOURCE_DIGEST: Final = (
    "0238a41c5fd6e2def4a1457d5a21e52d565ada836d1e9e9555098db65aefd4c7"
)
AUTH_SOURCE_DIGEST: Final = (
    "8fc9336fb6cac498366d951c3a986c7bdf16efdd72e2beb13f39630c6fbcb225"
)


class OpenCodeProbeError(RuntimeError):
    """Raised when static OpenCode evidence is unavailable or forged."""


@dataclass(frozen=True, slots=True)
class OpenCodeExecutablePin:
    path: Path
    version: str
    file_identity: FileIdentity

    def __post_init__(self) -> None:
        if not self.path.is_absolute() or self.path.name != OPENCODE_IDENTIFIER:
            _fail("OpenCode executable path is not an absolute pinned binary")
        if self.version != OPENCODE_VERSION:
            _fail("OpenCode executable version is not pinned")
        if self.file_identity.sha256 != OPENCODE_SHA256:
            _fail("OpenCode executable hash is not pinned")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (
                self.file_identity.device,
                self.file_identity.inode,
                self.file_identity.size,
                self.file_identity.mtime_ns,
            )
        ):
            _fail("OpenCode executable file identity is invalid")


@dataclass(frozen=True, slots=True)
class HistoricalSymlinkProvenance:
    observed_at: str
    source_digest: str
    verification_status: Literal["verified", "unverified"]
    executable_version: str
    executable_sha256: str
    policy_id: str

    def __post_init__(self) -> None:
        if (
            not self.observed_at
            or self.source_digest != HISTORICAL_SOURCE_DIGEST
            or self.verification_status != "unverified"
            or self.executable_version != OPENCODE_VERSION
            or self.executable_sha256 != OPENCODE_SHA256
            or self.policy_id != _POLICIES[PROFILE_RAW]
        ):
            _fail("historical symlink provenance does not match the pinned record")


@dataclass(frozen=True, slots=True)
class BlockedObservation:
    source: Literal["historical", "current"]
    code: str
    observed_at: str
    source_digest: str
    verification_status: Literal["verified", "unverified"]


@dataclass(frozen=True, slots=True)
class BlockedState:
    reason: str
    observations: tuple[BlockedObservation, ...]

    def __post_init__(self) -> None:
        if (
            self.reason != _BLOCKED_REASON
            or self.observations != _blocked_observations()
        ):
            _fail("blocked provenance does not match the current static record")


@dataclass(frozen=True, slots=True)
class OpenCodeProfileRecord:
    profile: str
    role_token_digest: str
    manifest_digest: str
    argv_sha256: str
    policy_id: str
    permission_profile: str
    prompt_transport: str
    environment_allowlist: tuple[str, ...]
    cwd_label: str
    blocked_reason: str
    phase_ids: tuple[str, ...]
    phase_outcomes: tuple[str, ...]
    status: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpenCodeStaticProbe:
    pin: OpenCodeExecutablePin
    profiles: tuple[OpenCodeProfileRecord, ...]
    blocked: BlockedState
    historical: HistoricalSymlinkProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.pin, OpenCodeExecutablePin):
            _fail("static probe pin has an invalid type")
        expected_profiles = tuple(
            _profile_record(self.pin, profile) for profile in PROFILES
        )
        if self.profiles != expected_profiles:
            _fail("static probe profiles, receipts, or judgments were forged")
        if self.blocked != BlockedState(_BLOCKED_REASON, _blocked_observations()):
            _fail("static probe blocked provenance was forged")
        if self.historical != _historical_provenance():
            _fail("static probe historical provenance was forged")


def _fail(message: str) -> NoReturn:
    raise OpenCodeProbeError(message)


def _capture_file_identity(path: Path) -> FileIdentity:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OpenCodeProbeError(
            "OpenCode executable cannot be opened read-only"
        ) from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or not before.st_mode & 0o111
            or before.st_uid != os.getuid()
        ):
            _fail("OpenCode executable is not an owned executable regular file")
        digest = hashlib.sha256()
        os.lseek(fd, 0, os.SEEK_SET)
        while chunk := os.read(fd, 1_048_576):
            digest.update(chunk)
        after = os.fstat(fd)
        before_key = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_mode,
            before.st_uid,
        )
        after_key = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_mode,
            after.st_uid,
        )
        if before_key != after_key:
            _fail("OpenCode executable changed during static identity capture")
        return FileIdentity(
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            digest.hexdigest(),
        )
    finally:
        os.close(fd)


def _canonical_path() -> Path:
    raw = OPENCODE_CANONICAL_PATH
    try:
        if stat.S_ISLNK(raw.lstat().st_mode):
            _fail("OpenCode canonical executable must not be a symlink")
        return raw.resolve(strict=True)
    except OSError as exc:
        raise OpenCodeProbeError(
            "OpenCode canonical executable is unavailable"
        ) from exc


def static_preflight() -> OpenCodeExecutablePin:
    """Read only the exact pinned path; never invoke OpenCode or another provider."""

    path = _canonical_path()
    first = _capture_file_identity(path)
    if first.sha256 != OPENCODE_SHA256:
        _fail("OpenCode executable does not have the pinned SHA-256")
    second = _capture_file_identity(path)
    if first != second:
        _fail("OpenCode executable changed between static identity captures")
    return OpenCodeExecutablePin(path, OPENCODE_VERSION, first)


def _token_digest(profile: str) -> str:
    try:
        tokens = _ROLE_TOKENS[profile]
    except KeyError as exc:
        raise OpenCodeProbeError("unknown OpenCode static profile") from exc
    encoded = json.dumps(list(tokens), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _profile_manifest(pin: OpenCodeExecutablePin, profile: str) -> Manifest:
    if profile not in PROFILES:
        _fail("unknown OpenCode static profile")
    identity = ProfileIdentity(
        CURRENT_SCHEMA_VERSION,
        OPENCODE_IDENTIFIER,
        "read-only",
        platform.system().lower(),
        platform.machine().lower(),
        PROBE_REVISION,
        ExecutableIdentity(_PROBE_PATH_LABEL, pin.version, pin.file_identity.sha256),
        _token_digest(profile),
        "argv",
        _CWD_LABELS[profile],
        _ENVIRONMENT_NAMES,
        _POLICIES[profile],
    )
    return Manifest(identity, required_phases_for_profile("read-only"))


def _not_run_receipt(manifest: Manifest) -> Receipt:
    phases = tuple(
        PhaseReceipt(
            phase.phase_id,
            phase.expected_result,
            False,
            False,
            "not-run",
            None,
            False,
            (),
            CleanupInventory(),
        )
        for phase in manifest.required_phases
    )
    return Receipt(manifest.identity, _BLOCKED_REASON, phases)


def _profile_record(pin: OpenCodeExecutablePin, profile: str) -> OpenCodeProfileRecord:
    manifest = _profile_manifest(pin, profile)
    receipt = _not_run_receipt(manifest)
    judgment = judge_profile(manifest, receipt)
    return OpenCodeProfileRecord(
        profile,
        _token_digest(profile),
        hashlib.sha256(serialize_manifest(manifest).encode("utf-8")).hexdigest(),
        manifest.identity.argv_sha256,
        manifest.identity.sandbox_policy_id,
        manifest.identity.permission_profile,
        manifest.identity.prompt_transport,
        manifest.identity.environment_allowlist,
        manifest.identity.cwd,
        receipt.blocked_reason or "",
        tuple(phase.phase_id for phase in receipt.phases),
        tuple(phase.outcome for phase in receipt.phases),
        judgment.status,
        judgment.reason_codes,
    )


def _historical_provenance() -> HistoricalSymlinkProvenance:
    return HistoricalSymlinkProvenance(
        "2026-08-29",
        HISTORICAL_SOURCE_DIGEST,
        "unverified",
        OPENCODE_VERSION,
        OPENCODE_SHA256,
        _POLICIES[PROFILE_RAW],
    )


def _blocked_observations() -> tuple[BlockedObservation, ...]:
    return (
        BlockedObservation(
            "historical",
            "raw-symlink-escape",
            "2026-08-29",
            HISTORICAL_SOURCE_DIGEST,
            "unverified",
        ),
        BlockedObservation(
            "current",
            "auth-list-zero-credentials",
            "2026-08-30",
            AUTH_SOURCE_DIGEST,
            "verified",
        ),
    )


def build_static_probe() -> OpenCodeStaticProbe:
    """Build fixed raw/snapshot records without a provider turn."""

    pin = static_preflight()
    return OpenCodeStaticProbe(
        pin,
        tuple(_profile_record(pin, profile) for profile in PROFILES),
        BlockedState(_BLOCKED_REASON, _blocked_observations()),
        _historical_provenance(),
    )


def validate_static_probe(probe: OpenCodeStaticProbe) -> None:
    """Re-read the pinned file and reject forged profile/provenance records."""

    if not isinstance(probe, OpenCodeStaticProbe):
        _fail("static probe has an invalid type")
    observed = static_preflight()
    if probe.pin != observed:
        _fail("OpenCode executable pin does not match static preflight")
    expected = OpenCodeStaticProbe(
        observed,
        tuple(_profile_record(observed, profile) for profile in PROFILES),
        BlockedState(_BLOCKED_REASON, _blocked_observations()),
        _historical_provenance(),
    )
    if probe != expected:
        _fail("static probe does not match recomputed provenance")


def _record_payload(record: OpenCodeProfileRecord) -> dict[str, object]:
    return {
        "profile": record.profile,
        "role_token_digest": record.role_token_digest,
        "manifest_digest": record.manifest_digest,
        "argv_sha256": record.argv_sha256,
        "policy_id": record.policy_id,
        "permission_profile": record.permission_profile,
        "prompt_transport": record.prompt_transport,
        "environment_allowlist": list(record.environment_allowlist),
        "cwd": record.cwd_label,
        "blocked_reason": record.blocked_reason,
        "phase_ids": list(record.phase_ids),
        "phase_outcomes": list(record.phase_outcomes),
        "status": record.status,
        "reason_codes": list(record.reason_codes),
    }


def serialize_static_probe(probe: OpenCodeStaticProbe) -> str:
    """Serialize redacted static evidence after a fresh provider-free validation."""

    validate_static_probe(probe)
    payload: dict[str, object] = {
        "artifact": "opencode-static-probe",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "probe_revision": PROBE_REVISION,
        "pin": {
            "path": _PROBE_PATH_LABEL,
            "version": probe.pin.version,
            "sha256": probe.pin.file_identity.sha256,
            "device": probe.pin.file_identity.device,
            "inode": probe.pin.file_identity.inode,
            "size": probe.pin.file_identity.size,
            "mtime_ns": probe.pin.file_identity.mtime_ns,
        },
        "profiles": [_record_payload(record) for record in probe.profiles],
        "blocked": {
            "reason": probe.blocked.reason,
            "provenance": [
                {
                    "source": item.source,
                    "code": item.code,
                    "observed_at": item.observed_at,
                    "source_digest": item.source_digest,
                    "verification_status": item.verification_status,
                }
                for item in probe.blocked.observations
            ],
        },
        "historical_symlink": {
            "verdict": "rejected",
            "observed_at": probe.historical.observed_at,
            "source_digest": probe.historical.source_digest,
            "verification_status": probe.historical.verification_status,
            "executable_version": probe.historical.executable_version,
            "executable_sha256": probe.historical.executable_sha256,
            "policy_id": probe.historical.policy_id,
        },
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
