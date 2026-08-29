"""Static Cursor Agent pin inspection and blocked/not-run receipts.

This slice deliberately has no provider runner or prompt API.  It verifies the
known mise installation without executing Cursor, then emits a redacted
receipt whose live safety matrix remains ``not-run``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from agent_team.adapters import SAFE_ENV_KEYS, FileIdentity
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

_SHA256 = "[0-9a-f]{64}"
_SAFE_ENVIRONMENT = tuple(
    sorted(SAFE_ENV_KEYS & {"HOME", "LANG", "LC_ALL", "PATH", "TERM"})
)
_PROFILE_SPECS: Final[dict[str, tuple[str, str]]] = {
    "direct-plan": ("argv", "cursor-advertised-plan-v1"),
    "acp": ("stdin", "cursor-acp-no-policy-v1"),
}


class CursorStaticPreflightError(RuntimeError):
    """The trusted installation or fresh private root is unavailable or changed."""


@dataclass(frozen=True, slots=True)
class CursorInstallationPin:
    """Inventory-derived immutable path, version, and content pin."""

    version: str
    executable_relative_path: str
    canonical_relative_path: str
    bundle_relative_path: str
    node_relative_path: str
    wrapper_sha256: str
    bundle_sha256: str
    node_version: str

    def __post_init__(self) -> None:
        for value in (
            self.executable_relative_path,
            self.canonical_relative_path,
            self.bundle_relative_path,
            self.node_relative_path,
        ):
            if not value or Path(value).is_absolute() or ".." in Path(value).parts:
                raise CursorStaticPreflightError("Cursor pin path must be relative")
        for value in (self.wrapper_sha256, self.bundle_sha256):
            if not isinstance(value, str) or re.fullmatch(_SHA256, value) is None:
                raise CursorStaticPreflightError("Cursor pin hash is invalid")
        if not self.version or not self.node_version:
            raise CursorStaticPreflightError("Cursor pin version is incomplete")


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
    node_version="v24.5.0",
)


@dataclass(frozen=True, slots=True)
class CursorStaticPreflight:
    pin: CursorInstallationPin
    home: Path = field(repr=False)
    executable_path: Path = field(repr=False)
    canonical_path: Path = field(repr=False)
    bundle_path: Path = field(repr=False)
    node_path: Path = field(repr=False)
    wrapper_identity: FileIdentity
    bundle_identity: FileIdentity
    node_identity: FileIdentity


@dataclass(frozen=True, slots=True)
class CursorStaticReport:
    profile: CursorProfile
    workspace: Path = field(repr=False)
    private_root: Path = field(repr=False)
    preflight: CursorStaticPreflight = field(repr=False)
    manifest: Manifest = field(repr=False)
    receipt: Receipt = field(repr=False)
    judgment: Judgment = field(repr=False)


def static_preflight(*, private_root: Path) -> CursorStaticPreflight:
    """Inspect only the inventory-pinned files; never start Cursor or Node."""

    _validate_private_root(private_root)
    pin = CURSOR_INSTALLATION_PIN
    home = Path.home()
    executable = home / pin.executable_relative_path
    canonical = home / pin.canonical_relative_path
    bundle = home / pin.bundle_relative_path
    node = home / pin.node_relative_path
    wrapper = _same_object_identity(executable, canonical, require_executable=True)
    if wrapper.sha256 != pin.wrapper_sha256:
        raise CursorStaticPreflightError("Cursor wrapper hash does not match pin")
    bundle_identity = _capture_identity(bundle, require_executable=False)
    if bundle_identity.sha256 != pin.bundle_sha256:
        raise CursorStaticPreflightError("Cursor bundle hash does not match pin")
    node_identity = _capture_identity(node, require_executable=True)
    return CursorStaticPreflight(
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


def static_probe(
    profile: CursorProfile,
    *,
    workspace: Path,
    private_root: Path,
) -> CursorStaticReport:
    """Create a static report; no caller-supplied executable, phases, or status."""

    if profile not in _PROFILE_SPECS:
        raise CursorStaticPreflightError(f"unsupported Cursor profile: {profile!r}")
    _validate_workspace(workspace)
    preflight = static_preflight(private_root=private_root)
    manifest = _build_manifest(profile, workspace, preflight)
    receipt = _not_run_receipt(manifest)
    judgment = judge_profile(manifest, receipt)
    if judgment.status != "not-run":
        raise CursorStaticPreflightError("static report unexpectedly passed live gate")
    return CursorStaticReport(
        profile,
        workspace,
        private_root,
        preflight,
        manifest,
        receipt,
        judgment,
    )


def serialize_static_report(report: CursorStaticReport) -> str:
    """Recompute the static report and serialize only redacted metadata."""

    if not isinstance(report, CursorStaticReport):
        raise CursorStaticPreflightError("static report is invalid")
    _validate_workspace(report.workspace)
    preflight = static_preflight(private_root=report.private_root)
    manifest = _build_manifest(report.profile, report.workspace, preflight)
    receipt = _not_run_receipt(manifest)
    judgment = judge_profile(manifest, receipt)
    if judgment.status != "not-run":
        raise CursorStaticPreflightError("static report unexpectedly passed live gate")
    pin = preflight.pin
    return json.dumps(
        {
            "artifact": "cursor-static-probe",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "profile": {
                "id": report.profile,
                "harness_id": manifest.identity.harness_id,
                "permission_profile": manifest.identity.permission_profile,
                "prompt_transport": manifest.identity.prompt_transport,
                "sandbox_policy_id": manifest.identity.sandbox_policy_id,
                "status": judgment.status,
                "reason_codes": list(judgment.reason_codes),
            },
            "manifest": {
                "probe_revision": manifest.identity.probe_revision,
                "argv_sha256": manifest.identity.argv_sha256,
                "environment_allowlist": list(_SAFE_ENVIRONMENT),
                "workspace": "<workspace>",
            },
            "receipt": {
                "status": judgment.status,
                "phases": [
                    {"phase_id": phase.phase_id, "outcome": phase.outcome}
                    for phase in receipt.phases
                ],
            },
            "installation": {
                "version": pin.version,
                "wrapper_sha256": pin.wrapper_sha256,
                "bundle_sha256": pin.bundle_sha256,
                "node_version": pin.node_version,
                "wrapper_path_sha256": _path_digest(preflight.executable_path),
                "canonical_path_sha256": _path_digest(preflight.canonical_path),
                "bundle_path_sha256": _path_digest(preflight.bundle_path),
                "node_path_sha256": _path_digest(preflight.node_path),
                "wrapper_identity": _identity_dict(preflight.wrapper_identity),
                "bundle_identity": _identity_dict(preflight.bundle_identity),
                "node_identity": _identity_dict(preflight.node_identity),
            },
            "private_root": "<private-root>",
            "auth_prerequisite": "authenticated-at-inventory",
            "live_matrix": "not-run",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _build_manifest(
    profile: CursorProfile,
    workspace: Path,
    preflight: CursorStaticPreflight,
) -> Manifest:
    transport, policy_id = _PROFILE_SPECS[profile]
    argv = (
        (
            str(preflight.executable_path),
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
        if profile == "direct-plan"
        else (str(preflight.executable_path), "acp")
    )
    argv_digest = hashlib.sha256(
        json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    identity = ProfileIdentity(
        CURRENT_SCHEMA_VERSION,
        "cursor",
        "read-only",
        platform.system().lower(),
        platform.machine().lower(),
        "cursor-static-preflight-20260830",
        ExecutableIdentity(
            str(preflight.executable_path),
            preflight.pin.version,
            preflight.pin.wrapper_sha256,
        ),
        argv_digest,
        transport,
        str(workspace),
        _SAFE_ENVIRONMENT,
        policy_id,
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
    return Receipt(manifest.identity, None, phases)


def _validate_workspace(workspace: Path) -> None:
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        raise CursorStaticPreflightError("workspace must be absolute")
    try:
        info = workspace.lstat()
    except OSError as exc:
        raise CursorStaticPreflightError("workspace is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CursorStaticPreflightError("workspace must be a real directory")


def _validate_private_root(private_root: Path) -> None:
    if not isinstance(private_root, Path) or not private_root.is_absolute():
        raise CursorStaticPreflightError("private root must be absolute")
    try:
        info = private_root.lstat()
        entries = next(private_root.iterdir(), None)
    except OSError as exc:
        raise CursorStaticPreflightError("private root cannot be inspected") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or entries is not None
    ):
        raise CursorStaticPreflightError(
            "private root is not a fresh owner-only directory"
        )


def _same_object_identity(
    logical: Path, canonical: Path, *, require_executable: bool
) -> FileIdentity:
    try:
        if logical.is_symlink() and logical.resolve(strict=True) != canonical.resolve(
            strict=True
        ):
            raise CursorStaticPreflightError("Cursor executable path target changed")
        logical_identity = _capture_identity(
            canonical if logical.is_symlink() else logical,
            require_executable=require_executable,
        )
        canonical_identity = _capture_identity(
            canonical, require_executable=require_executable
        )
    except OSError as exc:
        raise CursorStaticPreflightError(
            "Cursor executable path cannot be inspected"
        ) from exc
    if logical_identity != canonical_identity:
        raise CursorStaticPreflightError("Cursor wrapper and canonical object differ")
    return logical_identity


def _capture_identity(path: Path, *, require_executable: bool) -> FileIdentity:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or (
            require_executable and not os.access(path, os.X_OK)
        ):
            raise CursorStaticPreflightError("Cursor installation object is invalid")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 65_536):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
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


def _identity_dict(identity: FileIdentity) -> dict[str, int | str]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
        "sha256": identity.sha256,
    }


def _path_digest(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()
