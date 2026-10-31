"""Static Devin identity and blocked/not-run receipt contract.

This slice does not start Devin, send a prompt, create a workspace, or parse
provider output.  Controlled live matrix work belongs to a later slice.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, NoReturn, cast

from .adapters import FileIdentity
from .probe_receipts import (
    CURRENT_SCHEMA_VERSION,
    CleanupInventory,
    ExecutableIdentity,
    Judgment,
    Manifest,
    PhaseReceipt,
    ProfileIdentity,
    Receipt,
    judge_profile,
    required_phases_for_profile,
    serialize_manifest,
    serialize_receipt,
)

DEVIN_CANONICAL_RELATIVE_PATH: Final = (
    ".local/share/mise/installs/http-devin/3000.6.7/bin/devin"
)
DEVIN_CANONICAL_PATH: Final = Path.home() / DEVIN_CANONICAL_RELATIVE_PATH
DEVIN_VERSION: Final = "3000.6.7"
DEVIN_BUILD: Final = "260a97c8"
DEVIN_VERSION_OUTPUT: Final = f"devin {DEVIN_VERSION} ({DEVIN_BUILD})"
DEVIN_SHA256: Final = "82a95b4c7c4bfd73a3ad8ac31207d7ba815ceab2c2fc4a432d002adf7fe43590"
DEVIN_CDHASH: Final = "30bb4bb91719ca3457ff3af32ad7b0614d3ff379"
DEVIN_TEAM_IDENTIFIER: Final = "83Z2LHX6XW"
DEVIN_IDENTIFIER: Final = "devin"
PROBE_REVISION: Final = "devin-static-probe-20260830-v2"

PROFILE_DIRECT: Final = "direct-auto-sandbox-read-only"
PROFILE_ACP: Final = "native-acp-review-no-sandbox"
PROFILES: Final[tuple[str, ...]] = (PROFILE_DIRECT, PROFILE_ACP)
_POLICIES: Final[dict[str, str]] = {
    PROFILE_DIRECT: "devin-direct-auto-sandbox-readonly-v2",
    PROFILE_ACP: "devin-native-acp-review-nonsandbox-v2",
}
_ARGV_TOKENS: Final[dict[str, tuple[str, ...]]] = {
    PROFILE_DIRECT: (
        "role=probe",
        "devin",
        "--permission-mode",
        "auto",
        "--sandbox",
        "--transport=stdin",
    ),
    PROFILE_ACP: (
        "role=probe",
        "devin",
        "acp",
        "--agent-type",
        "review",
        "--sandbox=absent",
        "--transport=stdin",
    ),
}
_ENVIRONMENT_NAMES: Final[tuple[str, ...]] = (
    "HOME",
    "LANG",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
)
_PROBE_PATH_LABEL = "/probe/devin"
_PROBE_CWD_LABEL = "/probe/workspace"
_BLOCKED_REASON: Final = "account"
_BLOCKED_OBSERVATIONS: Final[tuple[tuple[str, str], ...]] = (
    ("historical", "free-tier-tool-turn-not-established"),
    ("current", "tool-turn-not-run-without-tier-change"),
)


class DevinProbeError(RuntimeError):
    """Raised when static Devin evidence is unavailable or forged."""


@dataclass(frozen=True, slots=True)
class CodesignMetadata:
    identifier: str
    cdhash: str
    team_identifier: str

    def __post_init__(self) -> None:
        if (self.identifier, self.cdhash, self.team_identifier) != (
            DEVIN_IDENTIFIER,
            DEVIN_CDHASH,
            DEVIN_TEAM_IDENTIFIER,
        ):
            _fail("codesign metadata does not match the pinned Devin identity")


@dataclass(frozen=True, slots=True)
class DevinExecutablePin:
    path: Path
    version: str
    build: str
    file_identity: FileIdentity
    codesign: CodesignMetadata

    def __post_init__(self) -> None:
        if not isinstance(self.file_identity, FileIdentity) or not isinstance(
            self.codesign, CodesignMetadata
        ):
            _fail("Devin executable identity has an invalid type")
        if not self.path.is_absolute():
            _fail("Devin executable path must be absolute")
        if (self.version, self.build) != (DEVIN_VERSION, DEVIN_BUILD):
            _fail("Devin executable version/build is not pinned")
        if self.file_identity.sha256 != DEVIN_SHA256:
            _fail("Devin executable hash is not pinned")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (
                self.file_identity.device,
                self.file_identity.inode,
                self.file_identity.size,
                self.file_identity.mtime_ns,
            )
        ):
            _fail("Devin executable file identity is invalid")


@dataclass(frozen=True, slots=True)
class DevinProfileRecord:
    profile: str
    manifest: Manifest
    receipt: Receipt
    judgment: Judgment


@dataclass(frozen=True, slots=True)
class BlockedObservation:
    source: Literal["historical", "current"]
    code: str


@dataclass(frozen=True, slots=True)
class BlockedState:
    reason: str
    observations: tuple[BlockedObservation, ...]


@dataclass(frozen=True, slots=True)
class DevinStaticProbe:
    pin: DevinExecutablePin
    profiles: tuple[DevinProfileRecord, ...]
    blocked: BlockedState


def _fail(message: str) -> NoReturn:
    raise DevinProbeError(message)


def _canonical_path() -> Path:
    raw = DEVIN_CANONICAL_PATH
    try:
        if stat.S_ISLNK(raw.lstat().st_mode):
            _fail("Devin canonical executable must not be a symlink")
        return raw.resolve(strict=True)
    except OSError as exc:
        raise DevinProbeError("Devin canonical executable is unavailable") from exc


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1_048_576)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _capture_file_identity(path: Path) -> FileIdentity:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DevinProbeError("Devin executable cannot be opened read-only") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or not before.st_mode & 0o111
            or before.st_uid != os.getuid()
        ):
            _fail("Devin executable is not an owned executable regular file")
        digest = _hash_fd(fd)
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
            _fail("Devin executable changed during read-only identity capture")
        return FileIdentity(
            device=before.st_dev,
            inode=before.st_ino,
            size=before.st_size,
            mtime_ns=before.st_mtime_ns,
            sha256=digest,
        )
    finally:
        os.close(fd)


def _read_codesign_metadata(path: Path) -> CodesignMetadata:
    try:
        result = subprocess.run(
            ("codesign", "--display", "--verbose=4", str(path)),
            cwd=Path("/"),
            env={"PATH": "/usr/bin:/bin"},
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DevinProbeError("read-only codesign metadata is unavailable") from exc
    if result.returncode != 0:
        _fail("read-only codesign metadata failed")
    values: dict[str, str] = {}
    for line in f"{result.stdout}\n{result.stderr}".splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"Identifier", "CDHash", "TeamIdentifier"}:
            if key in values and values[key] != value:
                _fail("codesign metadata contains contradictory values")
            values[key] = value
    try:
        metadata = CodesignMetadata(
            values["Identifier"], values["CDHash"], values["TeamIdentifier"]
        )
    except KeyError as exc:
        raise DevinProbeError("codesign metadata is incomplete") from exc
    if (
        metadata.identifier != DEVIN_IDENTIFIER
        or metadata.cdhash != DEVIN_CDHASH
        or metadata.team_identifier != DEVIN_TEAM_IDENTIFIER
    ):
        _fail("codesign metadata does not match the pinned Devin identity")
    return metadata


def static_preflight() -> DevinExecutablePin:
    """Read only the fixed executable and its fixed codesign metadata."""

    path = _canonical_path()
    first = _capture_file_identity(path)
    metadata = _read_codesign_metadata(path)
    second = _capture_file_identity(path)
    if first != second:
        _fail("Devin executable changed around codesign metadata capture")
    if first.sha256 != DEVIN_SHA256:
        _fail("Devin executable does not have the pinned SHA-256")
    return DevinExecutablePin(
        path=path,
        version=DEVIN_VERSION,
        build=DEVIN_BUILD,
        file_identity=first,
        codesign=metadata,
    )


def _argv_digest(tokens: Sequence[str]) -> str:
    if tuple(tokens) not in _ARGV_TOKENS.values():
        _fail("Devin argv tokens are not a fixed profile")
    encoded = json.dumps(list(tokens), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _profile_manifest(pin: DevinExecutablePin, profile: str) -> Manifest:
    if profile not in PROFILES:
        _fail("unknown Devin static profile")
    permission = "read-only"
    return Manifest(
        ProfileIdentity(
            CURRENT_SCHEMA_VERSION,
            DEVIN_IDENTIFIER,
            permission,
            platform.system().lower(),
            platform.machine().lower(),
            PROBE_REVISION,
            ExecutableIdentity(_PROBE_PATH_LABEL, DEVIN_VERSION_OUTPUT, DEVIN_SHA256),
            _argv_digest(_ARGV_TOKENS[profile]),
            "stdin",
            _PROBE_CWD_LABEL,
            _ENVIRONMENT_NAMES,
            _POLICIES[profile],
        ),
        required_phases_for_profile(permission),
    )


def _not_run_receipt(manifest: Manifest) -> Receipt:
    phases = tuple(
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
        for spec in manifest.required_phases
    )
    return Receipt(manifest.identity, _BLOCKED_REASON, phases)


def _blocked_state() -> BlockedState:
    return BlockedState(
        _BLOCKED_REASON,
        tuple(
            BlockedObservation(cast(Literal["historical", "current"], source), code)
            for source, code in _BLOCKED_OBSERVATIONS
        ),
    )


def _profile_record(pin: DevinExecutablePin, profile: str) -> DevinProfileRecord:
    manifest = _profile_manifest(pin, profile)
    receipt = _not_run_receipt(manifest)
    return DevinProfileRecord(
        profile, manifest, receipt, judge_profile(manifest, receipt)
    )


def _validate_pin(pin: DevinExecutablePin, observed: DevinExecutablePin) -> None:
    if pin != observed:
        _fail("Devin executable pin does not match the read-only preflight")


def _validate_static_probe(
    probe: DevinStaticProbe, observed: DevinExecutablePin
) -> None:
    if not isinstance(probe, DevinStaticProbe):
        _fail("static probe has an invalid type")
    _validate_pin(probe.pin, observed)
    expected_profiles = tuple(
        _profile_record(observed, profile) for profile in PROFILES
    )
    if probe.profiles != expected_profiles:
        _fail("static probe profiles, receipts, or judgments were forged")
    if probe.blocked != _blocked_state():
        _fail("static probe blocked provenance was forged")


def build_static_probe() -> DevinStaticProbe:
    """Build two fixed profiles, both blocked before any provider turn."""

    pin = static_preflight()
    probe = DevinStaticProbe(
        pin,
        tuple(_profile_record(pin, profile) for profile in PROFILES),
        _blocked_state(),
    )
    _validate_static_probe(probe, pin)
    return probe


def validate_static_probe(probe: DevinStaticProbe) -> None:
    """Re-read the fixed pin and reject fake identities or candidate phases."""

    _validate_static_probe(probe, static_preflight())


def _judgment_payload(judgment: Judgment) -> dict[str, object]:
    return {
        "harness_id": judgment.harness_id,
        "permission_profile": judgment.permission_profile,
        "status": judgment.status,
        "reason_codes": list(judgment.reason_codes),
    }


def _pin_payload(pin: DevinExecutablePin) -> dict[str, object]:
    return {
        "path": _PROBE_PATH_LABEL,
        "version": pin.version,
        "build": pin.build,
        "sha256": pin.file_identity.sha256,
        "device": pin.file_identity.device,
        "inode": pin.file_identity.inode,
        "size": pin.file_identity.size,
        "mtime_ns": pin.file_identity.mtime_ns,
        "codesign": {
            "identifier": pin.codesign.identifier,
            "cdhash": pin.codesign.cdhash,
            "team_identifier": pin.codesign.team_identifier,
        },
    }


def serialize_static_probe(probe: DevinStaticProbe) -> str:
    """Serialize only a freshly validated, all-not-run static probe."""

    observed = static_preflight()
    _validate_static_probe(probe, observed)
    payload: dict[str, object] = {
        "artifact": "devin-static-probe",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "probe_revision": PROBE_REVISION,
        "pin": _pin_payload(probe.pin),
        "profiles": [
            {
                "profile": record.profile,
                "manifest": json.loads(serialize_manifest(record.manifest)),
                "receipt": json.loads(serialize_receipt(record.receipt)),
                "judgment": _judgment_payload(record.judgment),
            }
            for record in probe.profiles
        ],
        "blocked": {
            "reason": probe.blocked.reason,
            "provenance": [
                {"source": item.source, "code": item.code}
                for item in probe.blocked.observations
            ],
        },
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
