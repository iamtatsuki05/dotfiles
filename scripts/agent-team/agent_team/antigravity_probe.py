"""Static Antigravity provenance and historical safety evidence.

This module deliberately does not start ``agy`` or any other provider
process.  The live ``-p/--print`` matrix remains a separate, approval-gated
follow-up after a static identity and policy have been reviewed.
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

from .adapters import FileIdentity
from .probe_receipts import CURRENT_SCHEMA_VERSION

ANTIGRAVITY_EXECUTABLE: Final = Path("/opt/homebrew/bin/agy")
ANTIGRAVITY_VERSION: Final = "1.1.22"
ANTIGRAVITY_SHA256: Final = (
    "7b1317779085913d338bde0e9b39b72323d9083a879525f944fd469c8ecca906"
)
ANTIGRAVITY_SIGNING_IDENTITY: Final = (
    "Developer ID Application: Google LLC (EQHXZ8M8AV)"
)
ANTIGRAVITY_TEAM_ID: Final = "EQHXZ8M8AV"
PROBE_REVISION: Final = "antigravity-static-probe-20260830-v2"
RAW_POLICY_ID: Final = "antigravity-raw-workspace-readonly-v1"
SNAPSHOT_POLICY_ID: Final = "antigravity-snapshot-seatbelt-readonly-v1"

ProbeProfile = Literal["raw-workspace", "snapshot"]
RoleToken = Literal["planner", "reviewer"]
SnapshotStatus = Literal["blocked", "not-run"]
SnapshotReason = Literal["outer-sandbox-unverified", "provider-not-run"]


class AntigravityProbeError(ValueError):
    """Raised when static Antigravity evidence is not pinned or safe."""


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
class DeviceIdentity:
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


EXPECTED_DEVICE_IDENTITY: Final = DeviceIdentity(
    "Darwin", "arm64", "25.5.0", "26.5.2", "MacBookPro18,4"
)


@dataclass(frozen=True, slots=True)
class CodeSignature:
    """The verified leaf signer and Team ID, without raw ``codesign`` output."""

    identifier: str
    team_id: str

    def __post_init__(self) -> None:
        _text(self.identifier, "signature.identifier")
        _text(self.team_id, "signature.team_id")


EXPECTED_SIGNATURE: Final = CodeSignature(
    ANTIGRAVITY_SIGNING_IDENTITY, ANTIGRAVITY_TEAM_ID
)


def _validate_pin_values(
    *,
    executable_path: str,
    version: str,
    sha256: str,
    signature: CodeSignature,
    device_identity: DeviceIdentity,
) -> None:
    if (
        executable_path != str(ANTIGRAVITY_EXECUTABLE)
        or version != ANTIGRAVITY_VERSION
        or sha256 != ANTIGRAVITY_SHA256
        or signature != EXPECTED_SIGNATURE
        or device_identity != EXPECTED_DEVICE_IDENTITY
    ):
        _fail("agy static provenance is not pinned")


def _validate_file_identity(identity: FileIdentity) -> None:
    if not isinstance(identity, FileIdentity) or identity.sha256 != ANTIGRAVITY_SHA256:
        _fail("agy same-fd file identity is not pinned")
    for value in (identity.device, identity.inode, identity.size, identity.mtime_ns):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail("agy same-fd file identity is invalid")


@dataclass(frozen=True, slots=True)
class StaticProvenance:
    executable_path: str
    version: str
    sha256: str
    signature: CodeSignature
    device_identity: DeviceIdentity
    file_identity: FileIdentity

    def __post_init__(self) -> None:
        _validate_pin_values(
            executable_path=self.executable_path,
            version=self.version,
            sha256=self.sha256,
            signature=self.signature,
            device_identity=self.device_identity,
        )
        _validate_file_identity(self.file_identity)


def validate_static_provenance(provenance: StaticProvenance) -> StaticProvenance:
    """Return only a fully pinned static provenance object."""

    if not isinstance(provenance, StaticProvenance):
        _fail("agy static provenance has an invalid type")
    _validate_pin_values(
        executable_path=provenance.executable_path,
        version=provenance.version,
        sha256=provenance.sha256,
        signature=provenance.signature,
        device_identity=provenance.device_identity,
    )
    _validate_file_identity(provenance.file_identity)
    return provenance


def inspect_file_same_fd(path: Path) -> FileIdentity:
    """Hash one opened regular executable and reject path/descriptor drift."""

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
        return FileIdentity(
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


def parse_leaf_signature(metadata: str) -> CodeSignature:
    """Parse static signature metadata and select the first Authority (the leaf)."""

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
        or authorities[0] != ANTIGRAVITY_SIGNING_IDENTITY
        or not team_ids
        or any(team_id != ANTIGRAVITY_TEAM_ID for team_id in team_ids)
    ):
        _fail("agy leaf signature is not pinned")
    return CodeSignature(authorities[0], team_ids[0])


def inspect_static_binary(
    *,
    path: Path = ANTIGRAVITY_EXECUTABLE,
    version: str,
    signature_metadata: str,
    device_identity: DeviceIdentity,
) -> StaticProvenance:
    """Inspect the pinned file and supplied static facts without running a provider."""

    if path != ANTIGRAVITY_EXECUTABLE:
        _fail("agy executable path is not the pinned path")
    identity = inspect_file_same_fd(path)
    signature = parse_leaf_signature(signature_metadata)
    return StaticProvenance(
        str(path), version, identity.sha256, signature, device_identity, identity
    )


_ROLE_TOKENS: Final = ("planner", "reviewer")
_PROFILE_POLICIES: Final = {
    "raw-workspace": RAW_POLICY_ID,
    "snapshot": SNAPSHOT_POLICY_ID,
}


@dataclass(frozen=True, slots=True)
class RoleManifest:
    schema_version: int
    harness_id: str
    revision: str
    profile: ProbeProfile
    role: RoleToken
    permission_profile: str
    transport: str
    route: str
    sandbox_policy_id: str
    provenance: StaticProvenance

    def __post_init__(self) -> None:
        _validate_role_manifest(self)


def _validate_role_manifest(manifest: RoleManifest) -> RoleManifest:
    if not isinstance(manifest, RoleManifest):
        _fail("agy role manifest has an invalid type")
    if (
        manifest.schema_version != CURRENT_SCHEMA_VERSION
        or manifest.harness_id != "antigravity"
        or manifest.revision != PROBE_REVISION
        or manifest.profile not in _PROFILE_POLICIES
        or manifest.role not in _ROLE_TOKENS
        or manifest.permission_profile != "read-only"
        or manifest.transport != "print"
        or manifest.route != "--print"
        or manifest.sandbox_policy_id != _PROFILE_POLICIES[manifest.profile]
    ):
        _fail("agy role manifest is not a fixed read-only print profile")
    validate_static_provenance(manifest.provenance)
    return manifest


def _build_role_manifest(
    profile: ProbeProfile, role: RoleToken, provenance: StaticProvenance
) -> RoleManifest:
    if profile not in _PROFILE_POLICIES or role not in _ROLE_TOKENS:
        _fail("agy role or profile is not fixed")
    return RoleManifest(
        CURRENT_SCHEMA_VERSION,
        "antigravity",
        PROBE_REVISION,
        profile,
        role,
        "read-only",
        "print",
        "--print",
        _PROFILE_POLICIES[profile],
        validate_static_provenance(provenance),
    )


def build_raw_role_manifest(
    role: RoleToken, provenance: StaticProvenance
) -> RoleManifest:
    return _build_role_manifest("raw-workspace", role, provenance)


def build_snapshot_role_manifest(
    role: RoleToken, provenance: StaticProvenance
) -> RoleManifest:
    return _build_role_manifest("snapshot", role, provenance)


def _provenance_payload(provenance: StaticProvenance) -> dict[str, object]:
    validate_static_provenance(provenance)
    device = provenance.device_identity
    return {
        "path": str(ANTIGRAVITY_EXECUTABLE),
        "version": ANTIGRAVITY_VERSION,
        "sha256": ANTIGRAVITY_SHA256,
        "signature": {
            "identifier": ANTIGRAVITY_SIGNING_IDENTITY,
            "team_id": ANTIGRAVITY_TEAM_ID,
        },
        "device": {
            "os": device.os_name,
            "architecture": device.architecture,
            "kernel_release": device.kernel_release,
            "os_version": device.os_version,
            "model_identifier": device.model_identifier,
        },
    }


def _dump(payload: dict[str, object]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def serialize_role_manifest(manifest: RoleManifest) -> str:
    """Serialize a redacted fixed manifest, never the generic receipt contract."""

    checked = _validate_role_manifest(manifest)
    return _dump(
        {
            "artifact": "antigravity-role-manifest",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "harness_id": "antigravity",
            "revision": PROBE_REVISION,
            "profile": checked.profile,
            "role": checked.role,
            "permission_profile": "read-only",
            "transport": "print",
            "route": "--print",
            "sandbox_policy_id": _PROFILE_POLICIES[checked.profile],
            "provenance": _provenance_payload(checked.provenance),
        }
    )


@dataclass(frozen=True, slots=True)
class HistoricalOutsideReadEvidence:
    tool: Literal["filesystem"] = "filesystem"
    operation: Literal["read"] = "read"
    target: Literal["outside"] = "outside"
    result: Literal["allowed"] = "allowed"

    def __post_init__(self) -> None:
        if (self.tool, self.operation, self.target, self.result) != (
            "filesystem",
            "read",
            "outside",
            "allowed",
        ):
            _fail("historical outside-read evidence is not fixed")


KNOWN_RAW_OUTSIDE_READ_EVIDENCE: Final = HistoricalOutsideReadEvidence()


def _validate_timestamp(value: str) -> str:
    _text(value, "historical observed_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AntigravityProbeError("historical observed_at is invalid") from exc
    if parsed.tzinfo is None:
        _fail("historical observed_at must include a timezone")
    return value


@dataclass(frozen=True, slots=True)
class HistoricalOutsideReadReceipt:
    observed_at: str
    source_sha256: str
    provenance: StaticProvenance
    evidence: HistoricalOutsideReadEvidence = KNOWN_RAW_OUTSIDE_READ_EVIDENCE
    status: Literal["rejected"] = "rejected"
    profile: Literal["raw-workspace"] = "raw-workspace"
    reason: Literal["outside-read"] = "outside-read"
    historical_unverified: Literal[True] = True

    def __post_init__(self) -> None:
        _validate_historical_receipt(self)


def _validate_historical_receipt(
    receipt: HistoricalOutsideReadReceipt,
) -> HistoricalOutsideReadReceipt:
    if not isinstance(receipt, HistoricalOutsideReadReceipt):
        _fail("historical receipt has an invalid type")
    if (
        receipt.status != "rejected"
        or receipt.profile != "raw-workspace"
        or receipt.reason != "outside-read"
        or receipt.historical_unverified is not True
        or receipt.evidence != KNOWN_RAW_OUTSIDE_READ_EVIDENCE
    ):
        _fail("historical outside-read receipt must remain rejected")
    _validate_timestamp(receipt.observed_at)
    _sha256(receipt.source_sha256, "historical source_sha256")
    validate_static_provenance(receipt.provenance)
    return receipt


def build_raw_historical_receipt(
    *, observed_at: str, source_sha256: str, provenance: StaticProvenance
) -> HistoricalOutsideReadReceipt:
    return HistoricalOutsideReadReceipt(observed_at, source_sha256, provenance)


def serialize_raw_historical_receipt(
    receipt: HistoricalOutsideReadReceipt,
) -> str:
    """Serialize only redacted historical evidence and current static provenance."""

    checked = _validate_historical_receipt(receipt)
    source_sha256 = _sha256(checked.source_sha256, "historical source_sha256")
    return _dump(
        {
            "artifact": "antigravity-historical-outside-read",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "profile": "raw-workspace",
            "status": "rejected",
            "reason": "outside-read",
            "historical_unverified": True,
            "observed_at": checked.observed_at,
            "source_sha256": source_sha256,
            "evidence": {
                "tool": "filesystem",
                "operation": "read",
                "target": "outside",
                "result": "allowed",
            },
            "provenance": _provenance_payload(checked.provenance),
        }
    )


@dataclass(frozen=True, slots=True)
class SnapshotReceipt:
    status: SnapshotStatus
    reason: SnapshotReason
    provenance: StaticProvenance
    profile: Literal["snapshot"] = "snapshot"
    sandbox_policy_id: str = SNAPSHOT_POLICY_ID

    def __post_init__(self) -> None:
        _validate_snapshot_receipt(self)


def _validate_snapshot_receipt(receipt: SnapshotReceipt) -> SnapshotReceipt:
    if not isinstance(receipt, SnapshotReceipt):
        _fail("snapshot receipt has an invalid type")
    if (
        receipt.status not in {"blocked", "not-run"}
        or receipt.reason not in {"outer-sandbox-unverified", "provider-not-run"}
        or receipt.profile != "snapshot"
        or receipt.sandbox_policy_id != SNAPSHOT_POLICY_ID
    ):
        _fail("snapshot receipt must remain blocked or not-run")
    validate_static_provenance(receipt.provenance)
    return receipt


def build_snapshot_receipt(
    provenance: StaticProvenance,
    *,
    status: SnapshotStatus = "not-run",
    reason: SnapshotReason = "provider-not-run",
) -> SnapshotReceipt:
    return SnapshotReceipt(status, reason, validate_static_provenance(provenance))


def build_snapshot_blocked_receipt(
    provenance: StaticProvenance,
) -> SnapshotReceipt:
    return build_snapshot_receipt(
        provenance, status="blocked", reason="outer-sandbox-unverified"
    )


def build_snapshot_not_run_receipt(
    provenance: StaticProvenance,
) -> SnapshotReceipt:
    return build_snapshot_receipt(
        provenance, status="not-run", reason="provider-not-run"
    )


def serialize_snapshot_receipt(receipt: SnapshotReceipt) -> str:
    """Serialize a non-candidate snapshot gate with redacted static provenance."""

    checked = _validate_snapshot_receipt(receipt)
    return _dump(
        {
            "artifact": "antigravity-snapshot-gate",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "profile": "snapshot",
            "status": checked.status,
            "reason": checked.reason,
            "sandbox_policy_id": SNAPSHOT_POLICY_ID,
            "provenance": _provenance_payload(checked.provenance),
        }
    )
