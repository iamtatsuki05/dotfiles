"""Grok-specific static identity gates and blocked probe receipts.

The current slice is intentionally provider-free.  It verifies the pinned
Grok installation using filesystem metadata, hashes, package provenance, and
signature metadata, then records blocked/not-run profiles without exposing a
live command or reading credential contents.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Literal, NoReturn

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

GROK_VERSION: Final = "1.0.13"
GROK_COMMIT: Final = "5e9a58528b76"
GROK_CHANNEL: Final = "alpha"
GROK_TARGET_NAME: Final = "grok-1.0.13"
GROK_WRAPPER_NAME: Final = "grok"
GROK_PACKAGE_NAME: Final = "@xai-official/grok"
GROK_INSTALL_RELATIVE: Final = (
    ".local/share/mise/installs/npm-xai-official-grok/1.0.13"
)
GROK_BINARY_SHA256: Final = (
    "8669e0fdadceec25b8c159c355f427ffbd82583525d774b6ab1522197ea83b80"
)
GROK_WRAPPER_SHA256: Final = (
    "13a2405556fe9e86108731a801771db1b9a742ef11e934ab2cb886f1089aeef0"
)
GROK_PACKAGE_SHA256: Final = (
    "5c1a4a21cb52065961ba51cc74a4fc45984ccdf72d38863f4c7dca2214b924ca"
)
GROK_TEAM_ID: Final = "5Y6N3AJ54S"
GROK_CDHASH: Final = "ce62b26141f33105a604c3f66c98bdcaee9dd00b"
PROBE_REVISION: Final = "grok-probe-20260830-v2"
GROK_RECEIPT_SCHEMA_VERSION: Final = 1
REDACTED_EXECUTABLE_PATH: Final = "/__agent_team_probe__/grok/canonical-target"
REDACTED_DIRECT_CWD: Final = "/__agent_team_probe__/grok/direct"
REDACTED_NATIVE_CWD: Final = "/__agent_team_probe__/grok/native-stdio"
_SAFE_PATH: Final = "/usr/bin:/bin:/usr/sbin:/sbin"
_ENVIRONMENT_ALLOWLIST: Final[tuple[str, ...]] = (
    "GROK_DISABLE_AUTOUPDATER",
    "GROK_HOME",
    "GROK_SUBAGENTS",
    "HOME",
    "PATH",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
GrokProfile = Literal["direct", "native-stdio"]
AuthMarkerStatus = Literal["absent", "present-unverified"]


class GrokProbeError(RuntimeError):
    """Raised when a Grok identity or receipt cannot be proven safely."""


def _fail(message: str) -> NoReturn:
    raise GrokProbeError(message)


def _absolute(path: Path | str, field: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        _fail(f"{field} must be absolute")
    return candidate


def _validate_sha256(value: object, field: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _fail(f"{field} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class GrokProvenance:
    """Pinned release provenance; test fixtures must opt into their own pin."""

    binary_sha256: str = GROK_BINARY_SHA256
    wrapper_sha256: str = GROK_WRAPPER_SHA256
    package_sha256: str = GROK_PACKAGE_SHA256
    team_id: str = GROK_TEAM_ID
    cdhash: str = GROK_CDHASH
    version: str = GROK_VERSION
    commit: str = GROK_COMMIT
    channel: str = GROK_CHANNEL
    target_name: str = GROK_TARGET_NAME

    def __post_init__(self) -> None:
        for value, field in (
            (self.binary_sha256, "binary_sha256"),
            (self.wrapper_sha256, "wrapper_sha256"),
            (self.package_sha256, "package_sha256"),
        ):
            _validate_sha256(value, field)
        if (
            not isinstance(self.team_id, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.team_id) is None
        ):
            _fail("team_id is invalid")
        if (
            not isinstance(self.cdhash, str)
            or re.fullmatch(r"[0-9a-f]{40}", self.cdhash) is None
        ):
            _fail("cdhash must be 40 lowercase hexadecimal characters")
        if self.version != GROK_VERSION or self.commit != GROK_COMMIT:
            _fail("Grok provenance is not the pinned release")
        if self.channel != GROK_CHANNEL or self.target_name != GROK_TARGET_NAME:
            _fail("Grok provenance has an unexpected channel or target")


PINNED_GROK_PROVENANCE: Final = GrokProvenance()


@dataclass(frozen=True, slots=True)
class GrokSignature:
    team_id: str
    cdhash: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.team_id) is None:
            _fail("signature team_id is invalid")
        if re.fullmatch(r"[0-9a-f]{40}", self.cdhash) is None:
            _fail("signature cdhash is invalid")


def parse_grok_version(banner: str) -> tuple[str, str]:
    """Validate a version banner without starting a provider turn."""

    if not isinstance(banner, str) or len(banner) > 1024:
        _fail("Grok version banner is invalid")
    match = re.fullmatch(
        r"grok (?P<version>[0-9]+\.[0-9]+\.[0-9]+) "
        r"\((?P<commit>[0-9a-f]{12})\) \[alpha\]",
        banner.strip(),
    )
    if match is None:
        _fail("Grok version banner is not the expected alpha format")
    version, commit = match.group("version"), match.group("commit")
    if (version, commit) != (GROK_VERSION, GROK_COMMIT):
        _fail("Grok version or commit is not the pinned release")
    return version, commit


def _regular_file_identity(path: Path, field: str) -> tuple[str, int, int]:
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise GrokProbeError(f"{field} is unavailable") from exc
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        _fail(f"{field} must be a regular non-symlink file")
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise GrokProbeError(f"{field} cannot be hashed") from exc
    return digest, file_stat.st_dev, file_stat.st_ino


def _default_path_lookup() -> Path | None:
    """Resolve the command name only; the result is never executed."""

    value = shutil.which("grok")
    return None if value is None else Path(value)


def _codesign_signature(path: Path) -> GrokSignature:
    """Read signature metadata with the fixed system codesign utility."""

    if platform.system().lower() != "darwin":
        _fail("Grok signature metadata is unavailable on this platform")
    codesign = Path("/usr/bin/codesign")
    try:
        result = subprocess.run(
            (str(codesign), "-dv", "--verbose=4", str(path)),
            cwd="/",
            env={"PATH": _SAFE_PATH},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            shell=False,
            timeout=15.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GrokProbeError("Grok signature probe failed") from exc
    if result.returncode != 0:
        _fail("Grok signature probe failed")
    try:
        output = (result.stdout + result.stderr).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GrokProbeError("Grok signature metadata was not UTF-8") from exc
    team_match = re.search(r"\bTeamIdentifier=(\S+)", output)
    cdhash_match = re.search(r"\bCDHash=(\S+)", output)
    if team_match is None or cdhash_match is None:
        _fail("Grok signature metadata is incomplete")
    return GrokSignature(team_match.group(1), cdhash_match.group(1).lower())


SignatureProbe = Callable[[Path], GrokSignature]
PathLookup = Callable[[], Path | str | None]


@dataclass(frozen=True, slots=True)
class GrokBinaryIdentity:
    """All static identity needed to correlate a pinned Grok installation."""

    canonical_link: Path
    canonical_target: Path
    path_entry: Path
    wrapper_path: Path
    package_path: Path
    version: str
    commit: str
    sha256: str
    device: int
    inode: int
    symlink_device: int
    symlink_inode: int
    wrapper_sha256: str
    wrapper_device: int
    wrapper_inode: int
    path_entry_device: int
    path_entry_inode: int
    package_sha256: str
    package_device: int
    package_inode: int
    team_id: str
    cdhash: str

    def __post_init__(self) -> None:
        for path_value, field in (
            (self.canonical_link, "canonical_link"),
            (self.canonical_target, "canonical_target"),
            (self.path_entry, "path_entry"),
            (self.wrapper_path, "wrapper_path"),
            (self.package_path, "package_path"),
        ):
            _absolute(path_value, field)
        if self.canonical_target.name != GROK_TARGET_NAME:
            _fail("canonical target is not the pinned Grok executable")
        if self.version != GROK_VERSION or self.commit != GROK_COMMIT:
            _fail("Grok binary identity is not the pinned release")
        for digest_value, field in (
            (self.sha256, "sha256"),
            (self.wrapper_sha256, "wrapper_sha256"),
            (self.package_sha256, "package_sha256"),
        ):
            _validate_sha256(digest_value, field)
        if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.team_id) is None:
            _fail("team_id is invalid")
        if re.fullmatch(r"[0-9a-f]{40}", self.cdhash) is None:
            _fail("cdhash is invalid")
        for numeric_value, field in (
            (self.device, "device"),
            (self.inode, "inode"),
            (self.symlink_device, "symlink_device"),
            (self.symlink_inode, "symlink_inode"),
            (self.wrapper_device, "wrapper_device"),
            (self.wrapper_inode, "wrapper_inode"),
            (self.path_entry_device, "path_entry_device"),
            (self.path_entry_inode, "path_entry_inode"),
            (self.package_device, "package_device"),
            (self.package_inode, "package_inode"),
        ):
            if (
                not isinstance(numeric_value, int)
                or isinstance(numeric_value, bool)
                or numeric_value < 0
            ):
                _fail(f"{field} must be a non-negative integer")


class GrokBinaryResolver:
    """Verify one fixed home layout and one exact mise package installation."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        canonical_link: Path | None = None,
        expected_path_entry: Path | None = None,
        path_lookup: PathLookup | None = None,
        provenance: GrokProvenance = PINNED_GROK_PROVENANCE,
        signature_probe: SignatureProbe | None = None,
    ) -> None:
        self.home = _absolute(Path.home() if home is None else home, "home")
        expected_link = self.home / ".grok" / "bin" / GROK_WRAPPER_NAME
        if (
            canonical_link is not None
            and _absolute(canonical_link, "canonical_link") != expected_link
        ):
            _fail("canonical link is not the fixed ~/.grok/bin/grok path")
        self.canonical_link = expected_link
        fixed_install = self.home / GROK_INSTALL_RELATIVE
        fixed_path_entry = fixed_install / "bin" / GROK_WRAPPER_NAME
        if expected_path_entry is not None and _absolute(
            expected_path_entry, "expected_path_entry"
        ) != fixed_path_entry:
            _fail("expected PATH entry is not the fixed mise installation")
        self.expected_path_entry = fixed_path_entry
        self.path_lookup = path_lookup or _default_path_lookup
        self.provenance = provenance
        self.signature_probe = signature_probe or _codesign_signature

    def current_path_entry(self) -> Path:
        value = self.path_lookup()
        if value is None:
            _fail("PATH does not resolve the Grok command")
        entry = _absolute(value, "PATH Grok entry")
        if entry != self.expected_path_entry:
            _fail("PATH Grok entry changed from the pinned wrapper")
        return entry

    def resolve(self, *, path_entry: Path | None = None) -> GrokBinaryIdentity:
        link = self.canonical_link
        try:
            link_stat = link.lstat()
        except OSError as exc:
            raise GrokProbeError("canonical Grok symlink is unavailable") from exc
        if not stat.S_ISLNK(link_stat.st_mode):
            _fail("canonical Grok path is not a symlink")
        try:
            link_target_name = Path(os.readlink(link)).name
            target = link.resolve(strict=True)
        except OSError as exc:
            raise GrokProbeError("canonical Grok symlink cannot be resolved") from exc
        expected_target = link.parent / self.provenance.target_name
        if (
            link_target_name != self.provenance.target_name
            or target != expected_target.resolve(strict=True)
        ):
            _fail("canonical target is not grok-1.0.13")
        target_sha, target_device, target_inode = _regular_file_identity(
            target, "canonical target"
        )
        if target_sha != self.provenance.binary_sha256:
            _fail("canonical binary hash does not match pinned provenance")
        version, commit = self.provenance.version, self.provenance.commit

        entry = (
            self.current_path_entry()
            if path_entry is None
            else _absolute(path_entry, "PATH Grok entry")
        )
        if entry != self.expected_path_entry:
            _fail("PATH Grok entry changed from the pinned wrapper")
        try:
            entry_stat = entry.lstat()
            entry_resolved = entry.resolve(strict=True)
        except OSError as exc:
            raise GrokProbeError("PATH Grok entry is unavailable") from exc
        if not stat.S_ISLNK(entry_stat.st_mode):
            _fail("PATH Grok entry must be the pinned symlink wrapper")
        if entry.name != GROK_WRAPPER_NAME or entry.parent.name != "bin":
            _fail("PATH Grok entry has an unexpected layout")
        install_root = entry.parent.parent
        if (
            install_root.name != self.provenance.version
            or install_root.parent.name != "npm-xai-official-grok"
        ):
            _fail("PATH Grok entry is not the pinned mise installation")
        package_root = install_root / "lib" / "node_modules" / GROK_PACKAGE_NAME
        wrapper = package_root / "bin" / GROK_WRAPPER_NAME
        package = package_root / "package.json"
        if entry_resolved != wrapper.resolve(strict=True):
            _fail("PATH Grok entry does not point to the pinned wrapper")
        wrapper_sha, wrapper_device, wrapper_inode = _regular_file_identity(
            wrapper, "Grok wrapper"
        )
        if wrapper_sha != self.provenance.wrapper_sha256:
            _fail("Grok wrapper hash does not match pinned provenance")
        package_sha, package_device, package_inode = _regular_file_identity(
            package, "Grok package metadata"
        )
        if package_sha != self.provenance.package_sha256:
            _fail("Grok package metadata hash does not match pinned provenance")
        signature = self.signature_probe(target)
        if not isinstance(signature, GrokSignature):
            _fail("Grok signature probe returned an invalid result")
        if (signature.team_id, signature.cdhash) != (
            self.provenance.team_id,
            self.provenance.cdhash,
        ):
            _fail("Grok signature metadata does not match pinned provenance")
        return GrokBinaryIdentity(
            canonical_link=link,
            canonical_target=target,
            path_entry=entry,
            wrapper_path=wrapper,
            package_path=package,
            version=version,
            commit=commit,
            sha256=target_sha,
            device=target_device,
            inode=target_inode,
            symlink_device=link_stat.st_dev,
            symlink_inode=link_stat.st_ino,
            wrapper_sha256=wrapper_sha,
            wrapper_device=wrapper_device,
            wrapper_inode=wrapper_inode,
            path_entry_device=entry_stat.st_dev,
            path_entry_inode=entry_stat.st_ino,
            package_sha256=package_sha,
            package_device=package_device,
            package_inode=package_inode,
            team_id=signature.team_id,
            cdhash=signature.cdhash,
        )


def validate_grok_identity(
    expected: GrokBinaryIdentity, resolver: GrokBinaryResolver
) -> None:
    """Recheck the static identity; no live spawn API exists in this slice."""

    current_entry = resolver.current_path_entry()
    if current_entry != expected.path_entry:
        _fail("Grok PATH entry changed after preflight")
    current = resolver.resolve(path_entry=expected.path_entry)
    if current != expected:
        _fail("Grok executable identity changed after preflight")


def _profile(profile: str) -> GrokProfile:
    if profile == "direct":
        return "direct"
    if profile == "native-stdio":
        return "native-stdio"
    _fail(f"unsupported Grok profile: {profile}")


_DIRECT_ROLE_TOKENS: Final[tuple[str, ...]] = (
    "grok",
    "profile=direct",
    "transport=file",
    "permission=plan",
    "subagents=disabled",
    "mcp=empty",
    "hooks=isolated",
    "plugins=isolated",
    "web-search=disabled",
)
_NATIVE_ROLE_TOKENS: Final[tuple[str, ...]] = (
    "grok",
    "profile=native-stdio",
    "transport=stdin",
    "permission=unverified",
    "subagents=disabled",
    "mcp=empty",
    "hooks=isolated",
    "plugins=isolated",
    "web-search=disabled",
)
_PROFILE_POLICIES: Final[dict[GrokProfile, str]] = {
    "direct": "grok-direct-probe-v2",
    "native-stdio": "grok-native-stdio-unverified-v2",
}


def _canonical_profile_tokens(profile: GrokProfile) -> tuple[str, ...]:
    return _DIRECT_ROLE_TOKENS if profile == "direct" else _NATIVE_ROLE_TOKENS


def _profile_digest(profile: GrokProfile) -> str:
    payload = {
        "profile": profile,
        "policy": _PROFILE_POLICIES[profile],
        "argv_role_tokens": _canonical_profile_tokens(profile),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_profile_manifest(
    profile: GrokProfile, identity: GrokBinaryIdentity
) -> Manifest:
    """Build a profile manifest without storing a path or returning a command."""

    profile = _profile(profile)
    executable = ExecutableIdentity(
        REDACTED_EXECUTABLE_PATH,
        f"grok {identity.version} ({identity.commit}) [{GROK_CHANNEL}]",
        identity.sha256,
    )
    cwd = REDACTED_DIRECT_CWD if profile == "direct" else REDACTED_NATIVE_CWD
    transport = "file" if profile == "direct" else "stdin"
    return Manifest(
        ProfileIdentity(
            CURRENT_SCHEMA_VERSION,
            "grok",
            "read-only",
            platform.system().lower(),
            platform.machine().lower(),
            PROBE_REVISION,
            executable,
            _profile_digest(profile),
            transport,
            cwd,
            _ENVIRONMENT_ALLOWLIST,
            _PROFILE_POLICIES[profile],
        ),
        required_phases_for_profile("read-only"),
    )


def _validate_private_root(private_root: Path) -> Path:
    private_root = _absolute(private_root, "private_root")
    try:
        root_stat = private_root.lstat()
    except OSError as exc:
        raise GrokProbeError("private_root must already be a fresh directory") from exc
    if stat.S_ISLNK(root_stat.st_mode):
        _fail("private_root must not be a symlink")
    if not stat.S_ISDIR(root_stat.st_mode):
        _fail("private_root must be a directory")
    if hasattr(os, "getuid") and root_stat.st_uid != os.getuid():
        _fail("private_root must be owned by the current user")
    if stat.S_IMODE(root_stat.st_mode) & 0o077:
        _fail("private_root must be owner-only")
    try:
        if next(private_root.iterdir(), None) is not None:
            _fail("private_root must be empty")
    except OSError as exc:
        raise GrokProbeError("private_root contents cannot be inspected") from exc
    return private_root


def build_isolated_environment(
    private_root: Path, *, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return fresh roots and no inherited credentials or ambient controls."""

    root = _validate_private_root(private_root)
    _ = source
    return {
        "PATH": _SAFE_PATH,
        "HOME": str(root / "home"),
        "GROK_HOME": str(root / "grok-home"),
        "XDG_CONFIG_HOME": str(root / "xdg_config_home"),
        "XDG_DATA_HOME": str(root / "xdg_data_home"),
        "XDG_STATE_HOME": str(root / "xdg_state_home"),
        "XDG_CACHE_HOME": str(root / "xdg_cache_home"),
        "TMPDIR": str(root / "tmp"),
        "GROK_SUBAGENTS": "0",
        "GROK_DISABLE_AUTOUPDATER": "1",
    }


def _not_run_phases(manifest: Manifest) -> tuple[PhaseReceipt, ...]:
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
        for spec in manifest.required_phases
    )


@dataclass(frozen=True, slots=True)
class GrokPreflightResult:
    profile: GrokProfile
    identity: GrokBinaryIdentity
    manifest: Manifest
    receipt: Receipt
    judgment: Judgment
    auth_status: Literal["blocked"]
    auth_marker_status: AuthMarkerStatus
    matrix_status: Literal["not-run"]
    acpx_status: Literal["not-run"]


def offline_preflight(
    profile: GrokProfile,
    *,
    resolver: GrokBinaryResolver,
    private_root: Path,
    auth_path: Path | None = None,
    source_environment: Mapping[str, str] | None = None,
) -> GrokPreflightResult:
    """Assemble a blocked receipt after static identity checks, never a turn."""

    profile = _profile(profile)
    build_isolated_environment(private_root, source=source_environment)
    marker_path = (
        Path.home() / ".grok" / "auth.json"
        if auth_path is None
        else _absolute(auth_path, "auth_path")
    )
    environment = os.environ if source_environment is None else source_environment
    marker_present = os.path.lexists(marker_path) or "XAI_API_KEY" in environment
    marker_status: AuthMarkerStatus = (
        "present-unverified" if marker_present else "absent"
    )
    identity = resolver.resolve()
    manifest = build_profile_manifest(profile, identity)
    receipt = Receipt(
        manifest.identity,
        "authentication",
        _not_run_phases(manifest),
    )
    return GrokPreflightResult(
        profile,
        identity,
        manifest,
        receipt,
        judge_profile(manifest, receipt),
        "blocked",
        marker_status,
        "not-run",
        "not-run",
    )


def _validate_serializable_result(result: GrokPreflightResult) -> Judgment:
    if not isinstance(result, GrokPreflightResult):
        _fail("result must be GrokPreflightResult")
    if not isinstance(result.identity, GrokBinaryIdentity):
        _fail("result identity is invalid")
    if not isinstance(result.manifest, Manifest):
        _fail("result manifest is invalid")
    if not isinstance(result.receipt, Receipt):
        _fail("result receipt is invalid")
    if not isinstance(result.judgment, Judgment):
        _fail("result judgment is invalid")
    if result.auth_marker_status not in {"absent", "present-unverified"}:
        _fail("auth marker status is invalid")
    expected_manifest = build_profile_manifest(result.profile, result.identity)
    if result.manifest != expected_manifest:
        _fail("manifest does not correlate with profile and identity")
    if result.receipt.identity != result.manifest.identity:
        _fail("receipt identity does not correlate with manifest")
    computed = judge_profile(result.manifest, result.receipt)
    if result.judgment != computed:
        _fail("judgment does not correlate with manifest and receipt")
    if result.auth_status != "blocked" or result.matrix_status != "not-run":
        _fail("Grok live status is not blocked/not-run")
    if result.acpx_status != "not-run":
        _fail("acpx status must remain not-run")
    if computed.status != "blocked" or computed.reason_codes != (
        "blocked-authentication",
    ):
        _fail("Grok receipt must remain authentication-blocked")
    for phase in result.receipt.phases:
        if (
            phase.attempted
            or phase.tool_used
            or phase.outcome != "not-run"
            or phase.exit_code is not None
            or phase.timed_out
            or phase.evidence
            or phase.cleanup.has_residuals
        ):
            _fail("blocked Grok receipt must contain only not-run phases")
    return computed


def serialize_grok_receipt(result: GrokPreflightResult) -> str:
    """Serialize a correlated, redacted blocked receipt."""

    judgment = _validate_serializable_result(result)
    identity = result.identity
    payload: dict[str, object] = {
        "artifact": "grok-probe-receipt",
        "schema_version": GROK_RECEIPT_SCHEMA_VERSION,
        "profile": result.profile,
        "auth_status": result.auth_status,
        "auth_marker_status": result.auth_marker_status,
        "matrix_status": result.matrix_status,
        "acpx_status": result.acpx_status,
        "binary": {
            "canonical_target_name": GROK_TARGET_NAME,
            "path_role": "canonical-target",
            "symlink_role": "canonical-link",
            "path_entry_role": "PATH-entry-wrapper",
            "package_role": "pinned-npm-package-metadata",
            "version": identity.version,
            "commit": identity.commit,
            "sha256": identity.sha256,
            "device": identity.device,
            "inode": identity.inode,
            "symlink_device": identity.symlink_device,
            "symlink_inode": identity.symlink_inode,
            "wrapper_sha256": identity.wrapper_sha256,
            "wrapper_device": identity.wrapper_device,
            "wrapper_inode": identity.wrapper_inode,
            "path_entry_device": identity.path_entry_device,
            "path_entry_inode": identity.path_entry_inode,
            "package_sha256": identity.package_sha256,
            "package_device": identity.package_device,
            "package_inode": identity.package_inode,
            "team_id": identity.team_id,
            "cdhash": identity.cdhash,
        },
        "manifest": json.loads(serialize_manifest(result.manifest)),
        "receipt": json.loads(serialize_receipt(result.receipt)),
        "judgment": asdict(judgment),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    for path in (
        identity.canonical_link,
        identity.canonical_target,
        identity.path_entry,
        identity.wrapper_path,
        identity.package_path,
    ):
        if str(path) in serialized:
            _fail("receipt contains a user path")
    return serialized
