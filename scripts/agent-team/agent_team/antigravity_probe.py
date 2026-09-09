"""Path-free Antigravity safety artifacts backed by a static same-fd check.

The public API only returns redacted serialized artifacts.  Each artifact
re-inspects the pinned executable, but never starts ``agy`` or another
provider process.  The live ``-p/--print`` matrix remains a separate gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, NoReturn

from .adapters import FileIdentity as _FileIdentity
from .probe_receipts import CURRENT_SCHEMA_VERSION as _CURRENT_SCHEMA_VERSION

__all__ = (
    "serialize_raw_historical_artifact",
    "serialize_raw_role_manifest_artifact",
    "serialize_snapshot_blocked_artifact",
    "serialize_snapshot_not_run_artifact",
    "serialize_snapshot_role_manifest_artifact",
)

_ANTIGRAVITY_EXECUTABLE: Final = Path("/opt/homebrew/bin/agy")
_ANTIGRAVITY_VERSION: Final = "1.1.22"
_ANTIGRAVITY_SHA256: Final = (
    "7b1317779085913d338bde0e9b39b72323d9083a879525f944fd469c8ecca906"
)
_ANTIGRAVITY_SIGNING_IDENTITY: Final = (
    "Developer ID Application: Google LLC (EQHXZ8M8AV)"
)
_ANTIGRAVITY_TEAM_ID: Final = "EQHXZ8M8AV"
_PROBE_REVISION: Final = "antigravity-static-probe-20260830-v3"
_RAW_POLICY_ID: Final = "antigravity-raw-workspace-readonly-v1"
_SNAPSHOT_POLICY_ID: Final = "antigravity-snapshot-seatbelt-readonly-v1"
_HISTORICAL_UNVERIFIED: Final = "historical-unverified"
_LIVE_GATE_INELIGIBLE: Final = "ineligible"

_ProbeProfile = Literal["raw-workspace", "snapshot"]
_RoleToken = Literal["planner", "reviewer"]
_SnapshotStatus = Literal["blocked", "not-run"]
_SnapshotReason = Literal["outer-sandbox-unverified", "provider-not-run"]
_Verification = Literal["historical-unverified"]


class AntigravityProbeError(ValueError):
    """Raised when a static identity or safety artifact is invalid."""


def _fail(message: str) -> NoReturn:
    raise AntigravityProbeError(message)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        _fail(f"{field} is invalid")
    if any(
        ord(char) < 0x20
        or ord(char) == 0x7F
        or 0xD800 <= ord(char) <= 0xDFFF
        or ord(char) in {0x2028, 0x2029}
        for char in value
    ):
        _fail(f"{field} contains a control character")
    return value


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        _fail(f"{field} is not a lowercase SHA-256 digest")
    return text


@dataclass(frozen=True, slots=True)
class _DeviceIdentity:
    os_name: str
    architecture: str
    kernel_release: str
    os_version: str
    model_identifier: str

    def __post_init__(self) -> None:
        for field, value in (
            ("device.os_name", self.os_name),
            ("device.architecture", self.architecture),
            ("device.kernel_release", self.kernel_release),
            ("device.os_version", self.os_version),
            ("device.model_identifier", self.model_identifier),
        ):
            _text(value, field)


_EXPECTED_DEVICE_IDENTITY: Final = _DeviceIdentity(
    "Darwin", "arm64", "25.5.0", "26.5.2", "MacBookPro18,4"
)


@dataclass(frozen=True, slots=True)
class _CodeSignature:
    identifier: str
    team_id: str

    def __post_init__(self) -> None:
        _text(self.identifier, "signature.identifier")
        _text(self.team_id, "signature.team_id")


_EXPECTED_SIGNATURE: Final = _CodeSignature(
    _ANTIGRAVITY_SIGNING_IDENTITY, _ANTIGRAVITY_TEAM_ID
)


@dataclass(frozen=True, slots=True)
class _StaticAttestation:
    file_identity: _FileIdentity
    signature: _CodeSignature
    device_identity: _DeviceIdentity
    signature_verification: _Verification = _HISTORICAL_UNVERIFIED
    device_verification: _Verification = _HISTORICAL_UNVERIFIED
    live_gate: Literal["ineligible"] = _LIVE_GATE_INELIGIBLE
    version: str = _ANTIGRAVITY_VERSION

    def __post_init__(self) -> None:
        _validate_attestation(self)


def _validate_attestation(attestation: _StaticAttestation) -> _StaticAttestation:
    if not isinstance(attestation, _StaticAttestation):
        _fail("agy static attestation has an invalid type")
    if not isinstance(attestation.file_identity, _FileIdentity):
        _fail("agy same-fd identity has an invalid type")
    if attestation.file_identity.sha256 != _ANTIGRAVITY_SHA256:
        _fail("agy same-fd identity is not pinned")
    if attestation.signature != _EXPECTED_SIGNATURE:
        _fail("agy signature is not pinned")
    if attestation.device_identity != _EXPECTED_DEVICE_IDENTITY:
        _fail("agy device identity is not pinned")
    if attestation.signature_verification != _HISTORICAL_UNVERIFIED:
        _fail("agy signature verification provenance is invalid")
    if attestation.device_verification != _HISTORICAL_UNVERIFIED:
        _fail("agy device verification provenance is invalid")
    if attestation.live_gate != _LIVE_GATE_INELIGIBLE:
        _fail("agy live gate must remain ineligible")
    if attestation.version != _ANTIGRAVITY_VERSION:
        _fail("agy version is not pinned")
    return attestation


def _inspect_file_same_fd(path: Path) -> _FileIdentity:
    """Hash one opened regular executable and reject descriptor drift."""

    if not isinstance(path, Path) or not path.is_absolute():
        _fail("agy executable path must be absolute")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        _fail("safe no-follow executable inspection is unavailable")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise AntigravityProbeError(
            "agy executable could not be opened safely"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or (before.st_mode & 0o111) == 0:
            _fail("agy executable is not a regular executable file")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if size != before.st_size or before_identity != after_identity:
            _fail("agy executable changed during same-fd inspection")
        return _FileIdentity(
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            digest.hexdigest(),
        )
    except AntigravityProbeError:
        raise
    except OSError as exc:
        raise AntigravityProbeError(
            "agy executable could not be inspected safely"
        ) from exc
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            raise AntigravityProbeError(
                "agy executable descriptor could not be closed"
            ) from exc


def _current_attestation() -> _StaticAttestation:
    """Reinspect the pinned path and attach only fixed historical facts."""

    identity = _inspect_file_same_fd(_ANTIGRAVITY_EXECUTABLE)
    return _StaticAttestation(identity, _EXPECTED_SIGNATURE, _EXPECTED_DEVICE_IDENTITY)


def _parse_leaf_signature(metadata: str) -> _CodeSignature:
    """Select the first Authority (the leaf) without returning raw metadata."""

    if not isinstance(metadata, str) or len(metadata) > 32_768 or "\x00" in metadata:
        _fail("agy signature metadata is invalid")
    authorities: list[str] = []
    team_ids: list[str] = []
    for line in metadata.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if key == "Authority" and value:
            authorities.append(value)
        elif key == "TeamIdentifier" and value:
            team_ids.append(value)
    if (
        not authorities
        or authorities[0] != _ANTIGRAVITY_SIGNING_IDENTITY
        or not team_ids
        or any(team_id != _ANTIGRAVITY_TEAM_ID for team_id in team_ids)
    ):
        _fail("agy leaf signature is not pinned")
    return _CodeSignature(authorities[0], team_ids[0])


_ROLE_TOKENS: Final = ("planner", "reviewer")
_PROFILE_POLICIES: Final = {
    "raw-workspace": _RAW_POLICY_ID,
    "snapshot": _SNAPSHOT_POLICY_ID,
}


def _provenance_payload(attestation: _StaticAttestation) -> dict[str, object]:
    _validate_attestation(attestation)
    device = attestation.device_identity
    return {
        "binary": "agy",
        "version": _ANTIGRAVITY_VERSION,
        "sha256": _ANTIGRAVITY_SHA256,
        "signature": {
            "identifier": _ANTIGRAVITY_SIGNING_IDENTITY,
            "team_id": _ANTIGRAVITY_TEAM_ID,
            "verification": _HISTORICAL_UNVERIFIED,
        },
        "device": {
            "os": device.os_name,
            "architecture": device.architecture,
            "kernel_release": device.kernel_release,
            "os_version": device.os_version,
            "model_identifier": device.model_identifier,
            "verification": _HISTORICAL_UNVERIFIED,
        },
        "live_gate": _LIVE_GATE_INELIGIBLE,
    }


def _dump(payload: dict[str, object]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _build_role_artifact(
    profile: _ProbeProfile, role: _RoleToken, attestation: _StaticAttestation
) -> dict[str, object]:
    if profile not in _PROFILE_POLICIES or role not in _ROLE_TOKENS:
        _fail("agy role or profile is not fixed")
    if not isinstance(attestation, _StaticAttestation):
        _fail("agy role artifact requires internal attestation")
    return {
        "artifact": "antigravity-role-manifest",
        "schema_version": _CURRENT_SCHEMA_VERSION,
        "harness_id": "antigravity",
        "revision": _PROBE_REVISION,
        "profile": profile,
        "role": role,
        "permission_profile": "read-only",
        "transport": "print",
        "route": "--print",
        "sandbox_policy_id": _PROFILE_POLICIES[profile],
        "provenance": _provenance_payload(attestation),
    }


def _build_historical_artifact(
    *, observed_at: str, source_sha256: str, attestation: _StaticAttestation
) -> dict[str, object]:
    if not isinstance(attestation, _StaticAttestation):
        _fail("historical artifact requires internal attestation")
    _validate_timestamp(observed_at)
    source = _sha256(source_sha256, "historical source_sha256")
    return {
        "artifact": "antigravity-historical-outside-read",
        "schema_version": _CURRENT_SCHEMA_VERSION,
        "profile": "raw-workspace",
        "status": "rejected",
        "reason": "outside-read",
        "historical_unverified": True,
        "observed_at": observed_at,
        "source_sha256": source,
        "evidence": {
            "tool": "filesystem",
            "operation": "read",
            "target": "outside",
            "result": "allowed",
        },
        "provenance": _provenance_payload(attestation),
    }


def _validate_timestamp(value: str) -> str:
    _text(value, "historical observed_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AntigravityProbeError("historical observed_at is invalid") from exc
    if parsed.tzinfo is None:
        _fail("historical observed_at must include a timezone")
    return value


_SNAPSHOT_PAIRS: Final = {
    ("blocked", "outer-sandbox-unverified"),
    ("not-run", "provider-not-run"),
}


def _build_snapshot_artifact(
    status: _SnapshotStatus, reason: _SnapshotReason, attestation: _StaticAttestation
) -> dict[str, object]:
    if (status, reason) not in _SNAPSHOT_PAIRS:
        _fail("snapshot status and reason are not a fixed pair")
    if not isinstance(attestation, _StaticAttestation):
        _fail("snapshot artifact requires internal attestation")
    return {
        "artifact": "antigravity-snapshot-gate",
        "schema_version": _CURRENT_SCHEMA_VERSION,
        "profile": "snapshot",
        "status": status,
        "reason": reason,
        "sandbox_policy_id": _SNAPSHOT_POLICY_ID,
        "provenance": _provenance_payload(attestation),
    }


def serialize_raw_historical_artifact(*, observed_at: str, source_sha256: str) -> str:
    """Return a rejected historical raw-profile artifact after current reinspection."""

    attestation = _current_attestation()
    return _dump(
        _build_historical_artifact(
            observed_at=observed_at,
            source_sha256=source_sha256,
            attestation=attestation,
        )
    )


def serialize_raw_role_manifest_artifact(role: _RoleToken) -> str:
    """Return the fixed raw-profile role manifest after current reinspection."""

    return _dump(_build_role_artifact("raw-workspace", role, _current_attestation()))


def serialize_snapshot_role_manifest_artifact(role: _RoleToken) -> str:
    """Return the fixed snapshot role manifest after current reinspection."""

    return _dump(_build_role_artifact("snapshot", role, _current_attestation()))


def serialize_snapshot_blocked_artifact() -> str:
    """Return the outer-sandbox-unverified blocked snapshot gate."""

    return _dump(
        _build_snapshot_artifact(
            "blocked", "outer-sandbox-unverified", _current_attestation()
        )
    )


def serialize_snapshot_not_run_artifact() -> str:
    """Return the provider-not-run snapshot gate."""

    return _dump(
        _build_snapshot_artifact("not-run", "provider-not-run", _current_attestation())
    )
