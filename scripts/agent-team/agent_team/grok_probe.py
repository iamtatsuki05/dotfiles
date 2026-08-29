"""Grok-specific offline identity gates and provider-free probe receipts.

This module deliberately stops before a provider turn.  It fixes the native
Grok 1.0.13 executable, keeps direct and native stdio profiles separate, and
builds a blocked receipt when authentication is unavailable or unverified.
No credential contents, prompt text, process output, or user paths are put in
the persisted receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
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
GROK_STALE_TARGET_NAMES: Final = frozenset({"grok-1.0.5"})
PROBE_REVISION: Final = "grok-probe-20260830-v1"
GROK_RECEIPT_SCHEMA_VERSION: Final = 1
REDACTED_EXECUTABLE_PATH: Final = "/__agent_team_probe__/grok/grok-1.0.13"
REDACTED_DIRECT_CWD: Final = "/__agent_team_probe__/grok/direct"
REDACTED_NATIVE_CWD: Final = "/__agent_team_probe__/grok/native-stdio"
REDACTED_PROMPT_PATH: Final = "/__agent_team_probe__/grok/prompt-file"
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
    """Raised when a Grok probe cannot prove its fixed safety boundary."""


VersionProbe = Callable[[Path], str]
PathLookup = Callable[[], Path | str | None]


def _fail(message: str) -> NoReturn:
    raise GrokProbeError(message)


def _absolute(path: Path | str, field: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        _fail(f"{field} must be absolute")
    return candidate


def _digest(path: Path, field: str) -> tuple[str, int, int]:
    try:
        resolved = path.resolve(strict=True)
        file_stat = resolved.stat()
        if not stat.S_ISREG(file_stat.st_mode) or not os.access(resolved, os.X_OK):
            _fail(f"{field} is not an executable regular file")
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise GrokProbeError(f"{field} is unavailable") from exc
    return digest, file_stat.st_dev, file_stat.st_ino


def _version_probe(executable: Path) -> str:
    """Read only the exact executable's version banner, never a prompt."""

    node = shutil.which("node")
    version_path = _SAFE_PATH
    if node is not None:
        version_path = str(Path(node).parent) + os.pathsep + _SAFE_PATH
    try:
        result = subprocess.run(
            (str(executable), "--version"),
            cwd=executable.parent,
            env={"PATH": version_path},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            shell=False,
            timeout=15.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GrokProbeError("Grok version probe failed") from exc
    if result.returncode != 0:
        _fail("Grok version probe failed")
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise GrokProbeError("Grok version probe was not UTF-8") from exc


_VERSION_PATTERN = re.compile(
    r"\Agrok (?P<version>[0-9]+\.[0-9]+\.[0-9]+) "
    r"\((?P<commit>[0-9a-f]{12})\) \[alpha\]\Z"
)


def parse_grok_version(banner: str) -> tuple[str, str]:
    """Validate and return the exact current Grok version and commit."""

    if not isinstance(banner, str) or len(banner) > 1024:
        _fail("Grok version banner is invalid")
    match = _VERSION_PATTERN.fullmatch(banner.strip())
    if match is None:
        _fail("Grok version banner is not the expected alpha format")
    version, commit = match.group("version"), match.group("commit")
    if (version, commit) != (GROK_VERSION, GROK_COMMIT):
        _fail("Grok version or commit is not the pinned release")
    return version, commit


@dataclass(frozen=True, slots=True)
class GrokBinaryIdentity:
    """The identity fixed for both preflight and a future provider launch."""

    canonical_link: Path
    canonical_target: Path
    path_entry: Path
    version: str
    commit: str
    sha256: str
    device: int
    inode: int
    symlink_device: int
    symlink_inode: int
    path_entry_sha256: str
    path_entry_device: int
    path_entry_inode: int

    def __post_init__(self) -> None:
        for path_value, field in (
            (self.canonical_link, "canonical_link"),
            (self.canonical_target, "canonical_target"),
            (self.path_entry, "path_entry"),
        ):
            _absolute(path_value, field)
        if self.canonical_target.name != GROK_TARGET_NAME:
            _fail("canonical target is not the pinned Grok executable")
        if self.version != GROK_VERSION or self.commit != GROK_COMMIT:
            _fail("Grok binary identity is not the pinned release")
        for digest_value, field in (
            (self.sha256, "sha256"),
            (self.path_entry_sha256, "path_entry_sha256"),
        ):
            if (
                not isinstance(digest_value, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest_value) is None
            ):
                _fail(f"{field} must be a lowercase SHA-256 digest")
        for numeric_value, field in (
            (self.device, "device"),
            (self.inode, "inode"),
            (self.symlink_device, "symlink_device"),
            (self.symlink_inode, "symlink_inode"),
            (self.path_entry_device, "path_entry_device"),
            (self.path_entry_inode, "path_entry_inode"),
        ):
            if (
                not isinstance(numeric_value, int)
                or isinstance(numeric_value, bool)
                or numeric_value < 0
            ):
                _fail(f"{field} must be a non-negative integer")


def _default_path_lookup() -> Path | None:
    resolved = shutil.which("grok")
    return None if resolved is None else Path(resolved)


class GrokBinaryResolver:
    """Resolve one canonical symlink and reject every alternate executable."""

    def __init__(
        self,
        *,
        canonical_link: Path,
        path_lookup: PathLookup | None = None,
        version_probe: VersionProbe | None = None,
    ) -> None:
        self.canonical_link = _absolute(canonical_link, "canonical_link")
        self.path_lookup = path_lookup or _default_path_lookup
        self.version_probe = version_probe or _version_probe

    def current_path_entry(self) -> Path:
        value = self.path_lookup()
        if value is None:
            _fail("PATH does not resolve the Grok command")
        return _absolute(value, "PATH Grok entry")

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
        if link_target_name != GROK_TARGET_NAME or target.name != GROK_TARGET_NAME:
            _fail("canonical target is not grok-1.0.13")
        target_sha, target_device, target_inode = _digest(target, "canonical target")
        version, commit = parse_grok_version(self.version_probe(target))

        entry = (
            self.current_path_entry()
            if path_entry is None
            else _absolute(path_entry, "PATH Grok entry")
        )
        try:
            entry_resolved = entry.resolve(strict=True)
        except OSError as exc:
            raise GrokProbeError("PATH Grok entry is unavailable") from exc
        if entry_resolved.name in GROK_STALE_TARGET_NAMES:
            _fail("PATH resolves a stale Grok executable")
        path_version, path_commit = parse_grok_version(
            self.version_probe(entry_resolved)
        )
        if (path_version, path_commit) != (version, commit):
            _fail("PATH Grok entry does not match the canonical executable")
        path_sha, path_device, path_inode = _digest(entry_resolved, "PATH Grok entry")
        return GrokBinaryIdentity(
            canonical_link=link,
            canonical_target=target,
            path_entry=entry,
            version=version,
            commit=commit,
            sha256=target_sha,
            device=target_device,
            inode=target_inode,
            symlink_device=link_stat.st_dev,
            symlink_inode=link_stat.st_ino,
            path_entry_sha256=path_sha,
            path_entry_device=path_device,
            path_entry_inode=path_inode,
        )


def validate_grok_identity(
    expected: GrokBinaryIdentity, resolver: GrokBinaryResolver
) -> None:
    """Recheck PATH, symlink, version, hash, and device identity immediately."""

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


def build_profile_command(
    profile: GrokProfile,
    executable: Path,
    *,
    prompt_file: Path | None = None,
) -> tuple[str, ...]:
    """Build a fixed command template; this function never starts a process."""

    profile = _profile(profile)
    executable = _absolute(executable, "Grok executable")
    if profile == "native-stdio":
        if prompt_file is not None:
            _fail("native stdio does not accept a direct prompt file")
        return (str(executable), "agent", "--no-leader", "stdio")
    if prompt_file is None:
        _fail("direct Grok profile requires an external prompt file")
    prompt_file = _absolute(prompt_file, "prompt file")
    return (
        str(executable),
        "--single",
        "--output-format",
        "json",
        "--permission-mode",
        "plan",
        "--no-subagents",
        "--tools",
        "read_file,grep,list_dir",
        "--disallowed-tools",
        "run_terminal_cmd,search_replace,Agent",
        "--deny",
        "MCPTool(*)",
        "--deny",
        "WebFetch(*)",
        "--disable-web-search",
        "--prompt-file",
        str(prompt_file),
    )


def _argv_digest(argv: Sequence[str]) -> str:
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        _fail("Grok argv must contain non-empty strings")
    encoded = json.dumps(
        list(argv), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_profile_manifest(
    profile: GrokProfile, identity: GrokBinaryIdentity
) -> Manifest:
    """Create a receipt manifest using stable labels instead of user paths."""

    profile = _profile(profile)
    argv = build_profile_command(
        profile,
        Path(REDACTED_EXECUTABLE_PATH),
        prompt_file=Path(REDACTED_PROMPT_PATH) if profile == "direct" else None,
    )
    executable = ExecutableIdentity(
        REDACTED_EXECUTABLE_PATH,
        f"grok {identity.version} ({identity.commit}) [{GROK_CHANNEL}]",
        identity.sha256,
    )
    cwd = REDACTED_DIRECT_CWD if profile == "direct" else REDACTED_NATIVE_CWD
    transport = "file" if profile == "direct" else "stdin"
    policy = f"grok-{profile}-isolated-readonly-v1"
    return Manifest(
        ProfileIdentity(
            CURRENT_SCHEMA_VERSION,
            "grok",
            "read-only",
            platform.system().lower(),
            platform.machine().lower(),
            PROBE_REVISION,
            executable,
            _argv_digest(argv),
            transport,
            cwd,
            _ENVIRONMENT_ALLOWLIST,
            policy,
        ),
        required_phases_for_profile("read-only"),
    )


def build_isolated_environment(
    private_root: Path, *, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return a closed environment with fresh config roots and no credentials."""

    private_root = _absolute(private_root, "private_root")
    _ = source
    return {
        "PATH": _SAFE_PATH,
        "HOME": str(private_root / "home"),
        "GROK_HOME": str(private_root / "grok-home"),
        "XDG_CONFIG_HOME": str(private_root / "xdg_config_home"),
        "XDG_DATA_HOME": str(private_root / "xdg_data_home"),
        "XDG_STATE_HOME": str(private_root / "xdg_state_home"),
        "XDG_CACHE_HOME": str(private_root / "xdg_cache_home"),
        "TMPDIR": str(private_root / "tmp"),
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
    """Resolve identity and assemble blocked/not-run receipts without a turn."""

    profile = _profile(profile)
    private_root = _absolute(private_root, "private_root")
    build_isolated_environment(private_root, source=source_environment)
    identity = resolver.resolve()
    manifest = build_profile_manifest(profile, identity)
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


@dataclass(frozen=True, slots=True)
class GrokBoundedCommand:
    profile: GrokProfile
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    timeout_seconds: float
    identity: GrokBinaryIdentity


def prepare_bounded_command(
    profile: GrokProfile,
    identity: GrokBinaryIdentity,
    *,
    resolver: GrokBinaryResolver,
    private_root: Path,
    prompt_file: Path | None = None,
    timeout_seconds: float = 900.0,
) -> GrokBoundedCommand:
    """Prepare, but do not execute, a future bounded Grok invocation."""

    profile = _profile(profile)
    if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds):
        _fail("Grok timeout must be finite")
    if timeout_seconds <= 0:
        _fail("Grok timeout must be positive")
    private_root = _absolute(private_root, "private_root")
    validate_grok_identity(identity, resolver)
    argv = build_profile_command(
        profile,
        identity.canonical_target,
        prompt_file=prompt_file,
    )
    return GrokBoundedCommand(
        profile,
        argv,
        build_isolated_environment(private_root),
        timeout_seconds,
        identity,
    )


def serialize_grok_receipt(result: GrokPreflightResult) -> str:
    """Serialize only redacted provider metadata and the generic receipt."""

    if not isinstance(result, GrokPreflightResult):
        _fail("result must be GrokPreflightResult")
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
            "path_entry_role": "PATH-entry",
            "version": identity.version,
            "commit": identity.commit,
            "sha256": identity.sha256,
            "device": identity.device,
            "inode": identity.inode,
            "symlink_device": identity.symlink_device,
            "symlink_inode": identity.symlink_inode,
            "path_entry_sha256": identity.path_entry_sha256,
            "path_entry_device": identity.path_entry_device,
            "path_entry_inode": identity.path_entry_inode,
        },
        "manifest": json.loads(serialize_manifest(result.manifest)),
        "receipt": json.loads(serialize_receipt(result.receipt)),
        "judgment": asdict(result.judgment),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
