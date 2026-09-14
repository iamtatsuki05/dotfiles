"""Static Cursor Agent identity inspection with a permanently not-run gate."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from agent_team.adapters import FileIdentity
from agent_team.probe_receipts import (
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
)

CursorProfile = Literal["direct-plan", "acp"]
AuthStatus = Literal["historical-unverified"]
CursorTransport = Literal["argv", "stdin"]

_SHA256 = r"[0-9a-f]{64}"
_STATIC_PROFILE_SPECS: Final[dict[str, tuple[CursorTransport, str]]] = {
    "direct-plan": ("argv", "cursor-advertised-plan-v1"),
    "acp": ("stdin", "cursor-acp-no-policy-v1"),
}
_SAFE_ENVIRONMENT = ("HOME", "LANG", "LC_ALL", "PATH", "TERM")
_AUTH_OBSERVED_AT = "2026-08-29T18:51:06Z"
_AUTH_SOURCE_DESCRIPTOR = f"cursor-agent status authenticated {_AUTH_OBSERVED_AT}"
_AUTH_SOURCE_SHA256 = hashlib.sha256(_AUTH_SOURCE_DESCRIPTOR.encode()).hexdigest()


class CursorStaticPreflightError(RuntimeError):
    """The inventory-pinned installation is unavailable or changed."""


@dataclass(frozen=True, slots=True)
class CursorInstallationPin:
    """Immutable installation provenance recorded by the Cursor inventory."""

    version: str
    executable_relative_path: str
    canonical_relative_path: str
    bundle_relative_path: str
    node_relative_path: str
    wrapper_sha256: str
    bundle_sha256: str
    node_sha256: str
    node_version: str

    def __post_init__(self) -> None:
        paths = (
            self.executable_relative_path,
            self.canonical_relative_path,
            self.bundle_relative_path,
            self.node_relative_path,
        )
        if any(
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            for path in paths
        ):
            raise CursorStaticPreflightError("Cursor pin path must be relative")
        if any(
            not isinstance(value, str) or re.fullmatch(_SHA256, value) is None
            for value in (self.wrapper_sha256, self.bundle_sha256, self.node_sha256)
        ):
            raise CursorStaticPreflightError("Cursor pin hash is invalid")
        if not isinstance(self.version, str) or not self.version:
            raise CursorStaticPreflightError("Cursor pin version is incomplete")
        if not isinstance(self.node_version, str) or not self.node_version:
            raise CursorStaticPreflightError("Cursor Node version is incomplete")


CURSOR_INSTALLATION_PIN: Final = CursorInstallationPin(
    version="2026.05.09-0afadcc",
    executable_relative_path=(
        ".local/share/mise/installs/http-cursor-agent/2026.05.09-0afadcc/cursor-agent"
    ),
    canonical_relative_path=(
        ".local/share/mise/http-tarballs/954dc62a6a840b808e6661c95dc56d8b6f0ea7673c3253688f80bcf235236f29/cursor-agent"
    ),
    bundle_relative_path=(
        ".local/share/mise/http-tarballs/954dc62a6a840b808e6661c95dc56d8b6f0ea7673c3253688f80bcf235236f29/index.js"
    ),
    node_relative_path=(
        ".local/share/mise/http-tarballs/954dc62a6a840b808e6661c95dc56d8b6f0ea7673c3253688f80bcf235236f29/node"
    ),
    wrapper_sha256="b7babf47d8b1eee28ac27a74affa02a559bb38103a6e71fbb1f120805d51fedf",
    bundle_sha256="cbe95bd372a165cebd83658a293d844cae2dfddc7e1e0aef59e704d16d960257",
    node_sha256="336b5b3ebc5deb86df842102b20b6e4761605b7a667823e68dda7761b91a161b",
    node_version="v24.5.0",
)


@dataclass(frozen=True, slots=True)
class CursorIdentitySummary:
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CursorAuthProvenance:
    status: AuthStatus
    observed_at: str
    source_sha256: str

    def __post_init__(self) -> None:
        if (
            self.status != "historical-unverified"
            or self.observed_at != _AUTH_OBSERVED_AT
            or self.source_sha256 != _AUTH_SOURCE_SHA256
        ):
            raise CursorStaticPreflightError(
                "Cursor auth provenance is not the historical inventory record"
            )


@dataclass(frozen=True, slots=True)
class CursorStaticPreflight:
    """Redacted installation identity; it contains no filesystem paths."""

    version: str
    node_version: str
    wrapper_sha256: str
    bundle_sha256: str
    node_sha256: str
    wrapper_path_sha256: str
    canonical_path_sha256: str
    bundle_path_sha256: str
    node_path_sha256: str
    wrapper_identity: CursorIdentitySummary
    bundle_identity: CursorIdentitySummary
    node_identity: CursorIdentitySummary
    auth: CursorAuthProvenance


@dataclass(frozen=True, slots=True)
class CursorStaticReport:
    """Redacted DTO for static evidence; it cannot represent a live candidate."""

    profile: CursorProfile
    harness_id: Literal["cursor"]
    permission_profile: Literal["read-only"]
    prompt_transport: Literal["argv", "stdin"]
    sandbox_policy_id: str
    probe_revision: str
    status: Literal["not-run"]
    reason_codes: tuple[str, ...]
    required_phases: tuple[str, ...]
    phase_outcomes: tuple[str, ...]
    installation: CursorStaticPreflight
    auth: CursorAuthProvenance

    def __post_init__(self) -> None:
        if self.profile not in _STATIC_PROFILE_SPECS:
            raise CursorStaticPreflightError("Cursor static profile is unknown")
        transport, policy = _STATIC_PROFILE_SPECS[self.profile]
        expected = tuple(
            phase.phase_id for phase in required_phases_for_profile("read-only")
        )
        if (
            self.harness_id != "cursor"
            or self.permission_profile != "read-only"
            or self.prompt_transport != transport
            or self.sandbox_policy_id != policy
            or self.status != "not-run"
            or self.reason_codes != ("phase-not-attempted",)
            or self.required_phases != expected
            or self.phase_outcomes != ("not-run",) * len(expected)
            or self.auth.status != "historical-unverified"
        ):
            raise CursorStaticPreflightError(
                "Cursor static report is not a not-run DTO"
            )


@dataclass(frozen=True, slots=True)
class _InstallationAttestation:
    pin: CursorInstallationPin
    home: Path
    executable: Path
    canonical: Path
    bundle: Path
    node: Path
    wrapper_identity: FileIdentity
    bundle_identity: FileIdentity
    node_identity: FileIdentity


@dataclass(frozen=True, slots=True)
class _InternalReport:
    profile: CursorProfile
    installation: _InstallationAttestation
    manifest: Manifest
    receipt: Receipt
    judgment: Judgment


def static_preflight() -> CursorStaticPreflight:
    """Inspect the fixed installation without executing Cursor or Node."""

    return _redacted_preflight(_inspect_installation())


def static_probe(profile: CursorProfile) -> CursorStaticReport:
    """Return static, historical-auth, permanently not-run evidence."""

    if not isinstance(profile, str) or profile not in _STATIC_PROFILE_SPECS:
        raise CursorStaticPreflightError(f"unsupported Cursor profile: {profile!r}")
    return _redacted_report(_build_internal_report(profile, _inspect_installation()))


def serialize_static_report(report: CursorStaticReport) -> str:
    """Recompute and serialize only the redacted DTO fields."""

    if not isinstance(report, CursorStaticReport):
        raise CursorStaticPreflightError("static report is invalid")
    internal = _build_internal_report(report.profile, _inspect_installation())
    return _serialize_report(_redacted_report(internal))


def _inspect_installation() -> _InstallationAttestation:
    pin = CURSOR_INSTALLATION_PIN
    home = Path.home()
    executable = home / pin.executable_relative_path
    canonical = home / pin.canonical_relative_path
    bundle = home / pin.bundle_relative_path
    node = home / pin.node_relative_path
    wrapper = _same_object_identity(executable, canonical)
    if wrapper.sha256 != pin.wrapper_sha256:
        raise CursorStaticPreflightError("Cursor wrapper hash does not match pin")
    bundle_identity = _capture_identity(bundle, require_executable=False)
    if bundle_identity.sha256 != pin.bundle_sha256:
        raise CursorStaticPreflightError("Cursor bundle hash does not match pin")
    node_identity = _capture_identity(node, require_executable=True)
    if node_identity.sha256 != pin.node_sha256:
        raise CursorStaticPreflightError("Cursor Node hash does not match pin")
    return _InstallationAttestation(
        pin,
        home,
        executable,
        canonical,
        bundle,
        node,
        wrapper,
        bundle_identity,
        node_identity,
    )


def _build_internal_report(
    profile: CursorProfile, installation: _InstallationAttestation
) -> _InternalReport:
    transport, policy_id = _STATIC_PROFILE_SPECS[profile]
    sentinel = "/cursor-static/pinned-cursor-agent"
    if profile == "direct-plan":
        argv: tuple[str, ...] = (
            sentinel,
            "--print",
            "--mode",
            "plan",
            "--sandbox",
            "enabled",
            "--workspace",
            "<workspace>",
            "--output-format",
            "text",
            "<prompt>",
        )
    else:
        argv = (sentinel, "acp")
    argv_sha256 = hashlib.sha256(
        json.dumps(argv, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = Manifest(
        ProfileIdentity(
            CURRENT_SCHEMA_VERSION,
            "cursor",
            "read-only",
            platform.system().lower(),
            platform.machine().lower(),
            "cursor-static-preflight-20260830",
            ExecutableIdentity(
                sentinel,
                installation.pin.version,
                installation.pin.wrapper_sha256,
            ),
            argv_sha256,
            transport,
            "/cursor-static/workspace",
            _SAFE_ENVIRONMENT,
            policy_id,
        ),
        required_phases_for_profile("read-only"),
    )
    receipt = Receipt(
        manifest.identity,
        None,
        tuple(
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
        ),
    )
    judgment = judge_profile(manifest, receipt)
    if judgment.status != "not-run":
        raise CursorStaticPreflightError("static report crossed the live gate")
    return _InternalReport(profile, installation, manifest, receipt, judgment)


def _redacted_preflight(
    installation: _InstallationAttestation,
) -> CursorStaticPreflight:
    pin = installation.pin
    return CursorStaticPreflight(
        pin.version,
        pin.node_version,
        pin.wrapper_sha256,
        pin.bundle_sha256,
        pin.node_sha256,
        _path_digest(installation.executable),
        _path_digest(installation.canonical),
        _path_digest(installation.bundle),
        _path_digest(installation.node),
        _identity_summary(installation.wrapper_identity),
        _identity_summary(installation.bundle_identity),
        _identity_summary(installation.node_identity),
        _auth_provenance(),
    )


def _redacted_report(internal: _InternalReport) -> CursorStaticReport:
    transport, policy_id = _STATIC_PROFILE_SPECS[internal.profile]
    phase_ids = tuple(phase.phase_id for phase in internal.receipt.phases)
    phase_outcomes = ("not-run",) * len(phase_ids)
    return CursorStaticReport(
        internal.profile,
        "cursor",
        "read-only",
        transport,
        policy_id,
        internal.manifest.identity.probe_revision,
        "not-run",
        internal.judgment.reason_codes,
        phase_ids,
        phase_outcomes,
        _redacted_preflight(internal.installation),
        _auth_provenance(),
    )


def _serialize_report(report: CursorStaticReport) -> str:
    installation = report.installation
    return json.dumps(
        {
            "artifact": "cursor-static-probe",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "status": report.status,
            "reason_codes": list(report.reason_codes),
            "profile": {
                "id": report.profile,
                "harness_id": report.harness_id,
                "permission_profile": report.permission_profile,
                "prompt_transport": report.prompt_transport,
                "sandbox_policy_id": report.sandbox_policy_id,
            },
            "manifest": {
                "probe_revision": report.probe_revision,
                "environment_allowlist": list(_SAFE_ENVIRONMENT),
            },
            "receipt": {
                "status": report.status,
                "phases": [
                    {"phase_id": phase, "outcome": outcome}
                    for phase, outcome in zip(
                        report.required_phases, report.phase_outcomes, strict=True
                    )
                ],
            },
            "installation": {
                "version": installation.version,
                "wrapper_sha256": installation.wrapper_sha256,
                "bundle_sha256": installation.bundle_sha256,
                "node_sha256": installation.node_sha256,
                "node_version": installation.node_version,
                "wrapper_path_sha256": installation.wrapper_path_sha256,
                "canonical_path_sha256": installation.canonical_path_sha256,
                "bundle_path_sha256": installation.bundle_path_sha256,
                "node_path_sha256": installation.node_path_sha256,
                "wrapper_identity": _summary_dict(installation.wrapper_identity),
                "bundle_identity": _summary_dict(installation.bundle_identity),
                "node_identity": _summary_dict(installation.node_identity),
            },
            "auth": {
                "status": report.auth.status,
                "observed_at": report.auth.observed_at,
                "source_sha256": report.auth.source_sha256,
            },
            "live_auth_gate": "not-run",
            "live_matrix": "not-run",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _same_object_identity(logical: Path, canonical: Path) -> FileIdentity:
    before_logical = _link_state(logical)
    before_canonical = _link_state(canonical)
    try:
        expected_target = canonical.resolve(strict=True)
        if before_logical.resolved_target not in (None, str(expected_target)):
            raise CursorStaticPreflightError(
                "Cursor executable link target is not pinned"
            )
        logical_identity = _capture_identity(
            canonical if before_logical.resolved_target else logical,
            require_executable=True,
        )
        canonical_identity = _capture_identity(canonical, require_executable=True)
    except OSError as exc:
        raise CursorStaticPreflightError(
            "Cursor executable path cannot be inspected"
        ) from exc
    after_logical = _link_state(logical)
    after_canonical = _link_state(canonical)
    if before_logical != after_logical or before_canonical != after_canonical:
        raise CursorStaticPreflightError("Cursor executable link changed while reading")
    if logical_identity != canonical_identity:
        raise CursorStaticPreflightError("Cursor wrapper and canonical object differ")
    return logical_identity


@dataclass(frozen=True, slots=True)
class _LinkState:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    target: str | None
    resolved_target: str | None


def _link_state(path: Path) -> _LinkState:
    try:
        info = path.lstat()
        target = os.readlink(path) if stat.S_ISLNK(info.st_mode) else None
        resolved = str(path.resolve(strict=True)) if target is not None else None
    except OSError as exc:
        raise CursorStaticPreflightError(
            "Cursor link state cannot be inspected"
        ) from exc
    return _LinkState(
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        target,
        resolved,
    )


def _capture_identity(path: Path, *, require_executable: bool) -> FileIdentity:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or (
            require_executable and not os.access(path, os.X_OK)
        ):
            raise CursorStaticPreflightError("Cursor installation object is invalid")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 65_536):
            digest.update(chunk)
        after = os.fstat(descriptor)
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
        ):
            raise CursorStaticPreflightError(
                "Cursor installation object changed while reading"
            )
        return FileIdentity(
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            digest.hexdigest(),
        )
    except CursorStaticPreflightError:
        raise
    except OSError as exc:
        raise CursorStaticPreflightError(
            "Cursor installation object cannot be inspected"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _identity_summary(identity: FileIdentity) -> CursorIdentitySummary:
    return CursorIdentitySummary(
        identity.device,
        identity.inode,
        identity.size,
        identity.mtime_ns,
        identity.sha256,
    )


def _summary_dict(summary: CursorIdentitySummary) -> dict[str, int | str]:
    return {
        "device": summary.device,
        "inode": summary.inode,
        "size": summary.size,
        "mtime_ns": summary.mtime_ns,
        "sha256": summary.sha256,
    }


def _path_digest(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _auth_provenance() -> CursorAuthProvenance:
    return CursorAuthProvenance(
        "historical-unverified",
        _AUTH_OBSERVED_AT,
        _AUTH_SOURCE_SHA256,
    )
