"""Read-only identity and Docker preflight for OpenClaw sandbox cells.

The current repository has no audited Docker image or endpoint pin.  This
module therefore stops at a blocked/not-run receipt and has no container,
cleanup, image-pull, or provider-turn API.  A command runner is injected so
the preflight can be tested without starting Docker or a provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Literal, NoReturn, Protocol

from .adapters import ExecutionError, ProcessResult
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
OPENCLAW_DEFAULT_NETWORK: Final = "none"
OPENCLAW_MAX_PROVIDER_TURNS: Final = 1
OPENCLAW_QUOTA_BUDGET: Final = 1
OPENCLAW_VERSION_TIMEOUT_SECONDS: Final = 15.0
DOCKER_PREFLIGHT_TIMEOUT_SECONDS: Final = 15.0
DOCKER_IMAGE_INSPECT_TIMEOUT_SECONDS: Final = 15.0
OPENCLAW_VERSION_PROBE_ARGV: Final = ("openclaw", "--version")
REDACTED_EXECUTABLE_PATH: Final = "/redacted/openclaw/2026.7.1/openclaw.mjs"
REDACTED_WORKSPACE_PATH: Final = "/redacted/openclaw/probe-workspace"

# No full repository@digest or endpoint has been audited yet.  Keeping these
# pins absent makes every unreviewed caller-supplied value fail closed.
AUDITED_OPENCLAW_IMAGE_PIN: Final[str | None] = None
AUDITED_DOCKER_CONTEXT: Final[str | None] = None
AUDITED_DOCKER_ENDPOINT_SHA256: Final[str | None] = None

SAFE_ENVIRONMENT_NAMES: Final = frozenset(
    {"HOME", "LANG", "LC_ALL", "PATH", "SHELL", "TERM", "TMPDIR", "USER"}
)
_RESERVED_NAMES: Final = frozenset(
    {
        ".agent",
        ".claude",
        ".codex",
        ".config",
        ".env",
        ".env.local",
        ".env.production",
        ".github",
        ".git",
        ".netrc",
        ".openclaw",
        ".npmrc",
        ".ssh",
        "auth.json",
        "credential",
        "credentials",
        "credentials.json",
        "private_key",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_RESERVED_SUFFIXES: Final = (".key", ".pem", ".p12", ".pfx", ".secret", ".token")
_DISPOSABLE_PARENT_PREFIX: Final = "openclaw-probe-"

CellId = Literal["direct-sandbox-off", "docker-read-only", "docker-workspace-write"]
ReceiptProfile = Literal["read-only", "workspace-write"]
DockerStatus = Literal["ready", "blocked"]

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTEXT = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_IMAGE = re.compile(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}\Z")
_SESSION = re.compile(r"agent:issue9-openclaw:[a-zA-Z0-9._-]{8,64}\Z")
_ENVIRONMENT = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SENSITIVE_NAME = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|bearer|cookie|password|secret|credential)"
)


class OpenClawProbeError(RuntimeError):
    """Raised when an OpenClaw identity or safety policy is unverified."""


class OpenClawProbeStatus(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    NOT_RUN = "not-run"


class CommandRunner(Protocol):
    """A no-shell command runner used for injected, bounded probes."""

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProcessResult: ...


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


def _version_banner(value: object) -> str:
    if not isinstance(value, str):
        _fail("OpenClaw version output must be text")
    banner = value.strip()
    expected = f"OpenClaw {OPENCLAW_VERSION} ({OPENCLAW_BUILD})"
    if banner != expected:
        _fail("executable did not report the exact OpenClaw 2026.7.1 identity")
    return banner


def resolve_openclaw_identity(
    executable: Path, observed_version: str | None = None
) -> OpenClawIdentity:
    """Inspect one canonical executable before any version process is started."""

    attestation = _read_executable_attestation(executable)
    if attestation.sha256 != OPENCLAW_EXECUTABLE_SHA256:
        _fail("OpenClaw executable SHA-256 identity drifted")
    if observed_version is not None:
        _version_banner(observed_version)
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
    """Explicit policy inputs for one future disposable Docker cell."""

    context: str
    image: str
    disposable_parent: Path
    mount_source: Path
    session_key: str
    mount_target: str = "/agent"
    mount_mode: Literal["ro", "rw"] = "ro"
    network: str = OPENCLAW_DEFAULT_NETWORK
    read_only_root: bool = True
    cap_drop: tuple[str, ...] = ("ALL",)
    privileged: bool = False
    docker_socket: bool = False
    credential_mounts: tuple[str, ...] = ()
    environment_allowlist: tuple[str, ...] = ("HOME", "PATH", "TMPDIR")
    provider: str = "openclaw"
    transport: str = "acp"
    model: str = "fixed"
    max_provider_turns: int = OPENCLAW_MAX_PROVIDER_TURNS
    quota_budget: int = OPENCLAW_QUOTA_BUDGET


def _current_home() -> Path:
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (ImportError, KeyError, OSError):
        return Path.home().resolve()


def _canonical_directory(path: Path, field_name: str) -> tuple[Path, os.stat_result]:
    if not isinstance(path, Path) or not path.is_absolute():
        _fail(f"{field_name} must be an absolute path")
    try:
        canonical = path.resolve(strict=True)
        value = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise OpenClawProbeError(f"{field_name} could not be inspected") from exc
    if path != canonical or stat.S_ISLNK(value.st_mode):
        _fail(f"{field_name} must be an absolute canonical path")
    if not stat.S_ISDIR(value.st_mode):
        _fail(f"{field_name} must be a directory")
    if value.st_uid != os.getuid():
        _fail(f"{field_name} must be owned by the current user")
    if stat.S_IMODE(value.st_mode) != 0o700:
        _fail(f"{field_name} must have mode 0700")
    return canonical, value


def _assert_no_symlink_components(path: Path, stop: Path) -> None:
    cursor = path
    while True:
        try:
            if stat.S_ISLNK(os.lstat(cursor).st_mode):
                _fail("disposable mount has a symlink parent")
        except OSError as exc:
            raise OpenClawProbeError(
                "disposable mount parent could not be inspected"
            ) from exc
        if cursor == stop:
            return
        if cursor == Path("/"):
            _fail("disposable mount is outside its declared parent")
        cursor = cursor.parent


def _assert_clean_mount_tree(root: Path) -> None:
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as scanner:
                entries = tuple(scanner)
        except OSError as exc:
            raise OpenClawProbeError("disposable mount could not be scanned") from exc
        for entry in entries:
            entry_name = entry.name.lower()
            if (
                entry_name in _RESERVED_NAMES
                or entry_name.startswith((".env.", "credential", "secret", "token"))
                or entry_name.endswith(_RESERVED_SUFFIXES)
            ):
                _fail("disposable mount contains a reserved or secret-like name")
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise OpenClawProbeError(
                    "disposable mount entry could not be inspected"
                ) from exc
            if stat.S_ISLNK(entry_stat.st_mode):
                _fail("disposable mount contains a symlink")
            if stat.S_ISDIR(entry_stat.st_mode):
                pending.append(Path(entry.path))
            elif not stat.S_ISREG(entry_stat.st_mode):
                _fail("disposable mount contains a special file")


def validate_docker_sandbox_config(config: DockerSandboxConfig) -> None:
    """Validate policy and disposable mount metadata without creating a mount."""

    if not isinstance(config, DockerSandboxConfig):
        _fail("Docker sandbox config has an invalid type")
    if (
        not isinstance(config.context, str)
        or _CONTEXT.fullmatch(config.context) is None
    ):
        _fail("Docker context is invalid")
    if not isinstance(config.image, str) or _IMAGE.fullmatch(config.image) is None:
        _fail("Docker image must include an immutable sha256 digest")
    parent, _ = _canonical_directory(config.disposable_parent, "disposable parent")
    source, source_stat = _canonical_directory(config.mount_source, "mount source")
    if source == parent:
        _fail("mount source must be a child of the disposable parent")
    if not parent.name.startswith(_DISPOSABLE_PARENT_PREFIX):
        _fail("disposable parent must have an explicit probe prefix")
    try:
        source.relative_to(parent)
    except ValueError:
        _fail("mount source must be inside the disposable parent")
    _assert_no_symlink_components(source, parent)
    if source_stat.st_uid != os.getuid() or stat.S_IMODE(source_stat.st_mode) != 0o700:
        _fail("mount source must be owned by the current user with mode 0700")
    if parent == _current_home():
        _fail("disposable parent must not be the user's home directory")
    _assert_clean_mount_tree(source)
    if not isinstance(config.mount_mode, str) or config.mount_mode not in {"ro", "rw"}:
        _fail("Docker mount mode is invalid")
    expected_target = "/agent" if config.mount_mode == "ro" else "/workspace"
    if (
        not isinstance(config.mount_target, str)
        or config.mount_target != expected_target
    ):
        _fail(f"Docker mount target must be {expected_target} for {config.mount_mode}")
    if (
        not isinstance(config.network, str)
        or config.network != OPENCLAW_DEFAULT_NETWORK
    ):
        _fail("Docker network must be none (network=none)")
    if config.read_only_root is not True:
        _fail("Docker root filesystem must be read-only")
    if config.cap_drop != ("ALL",):
        _fail("Docker must drop all capabilities")
    if not isinstance(config.privileged, bool):
        _fail("Docker privileged flag must be boolean")
    if config.privileged:
        _fail("privileged Docker containers are forbidden")
    if not isinstance(config.docker_socket, bool):
        _fail("Docker socket flag must be boolean")
    if config.docker_socket:
        _fail("Docker socket mounts are forbidden")
    if not isinstance(config.credential_mounts, tuple) or config.credential_mounts:
        _fail("credential mounts are forbidden")
    if not isinstance(config.environment_allowlist, tuple):
        _fail("environment allowlist must be a tuple")
    if any(not isinstance(name, str) for name in config.environment_allowlist):
        _fail("environment allowlist must contain strings")
    if len(set(config.environment_allowlist)) != len(config.environment_allowlist):
        _fail("environment allowlist contains duplicate names")
    for name in config.environment_allowlist:
        if (
            not isinstance(name, str)
            or _ENVIRONMENT.fullmatch(name) is None
            or name not in SAFE_ENVIRONMENT_NAMES
            or _SENSITIVE_NAME.search(name) is not None
        ):
            _fail("environment allowlist contains an unsafe name")
    if (
        not isinstance(config.session_key, str)
        or _SESSION.fullmatch(config.session_key) is None
    ):
        _fail("session key must be an explicit issue9 OpenClaw key")
    if not isinstance(config.provider, str) or config.provider != "openclaw":
        _fail("provider fallback is forbidden")
    if not isinstance(config.transport, str) or config.transport != "acp":
        _fail("OpenClaw Docker transport must be acp")
    if (
        not isinstance(config.model, str)
        or not config.model
        or _SENSITIVE_NAME.search(config.model) is not None
    ):
        _fail("model is invalid or contains a sensitive value marker")
    if (
        not isinstance(config.max_provider_turns, int)
        or isinstance(config.max_provider_turns, bool)
        or config.max_provider_turns != OPENCLAW_MAX_PROVIDER_TURNS
    ):
        _fail("OpenClaw provider turn count must be exactly one")
    if (
        not isinstance(config.quota_budget, int)
        or isinstance(config.quota_budget, bool)
        or config.quota_budget != OPENCLAW_QUOTA_BUDGET
    ):
        _fail("OpenClaw quota budget must be exactly one turn")


@dataclass(frozen=True, slots=True)
class DockerPreflight:
    status: DockerStatus
    context: str | None = None
    client_version: str | None = None
    server_version: str | None = None
    image_ref: str | None = None
    reason: str | None = None


def _docker_command(config: DockerSandboxConfig, *args: str) -> tuple[str, ...]:
    return ("docker", "--context", config.context, *args)


def _safe_run(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    timeout_seconds: float,
) -> tuple[ProcessResult | None, str | None]:
    try:
        result = runner.run(tuple(argv), timeout_seconds=timeout_seconds)
    except (ExecutionError, OSError, TimeoutError):
        return None, "failed"
    if not isinstance(result, ProcessResult):
        return None, "failed"
    if result.timed_out:
        return result, "timeout"
    if result.returncode != 0:
        return result, "failed"
    return result, None


def _blocked(reason: str, context: str | None = None) -> DockerPreflight:
    return DockerPreflight("blocked", context=context, reason=reason)


def _parse_versions(stdout: str) -> tuple[str, str] | None:
    value = stdout.strip()
    if not value or "\n" in value or "\r" in value:
        return None
    if "\t" in value:
        client, server = value.split("\t", 1)
        return (
            (client.strip(), server.strip())
            if client.strip() and server.strip()
            else None
        )
    try:
        payload: object = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    client_value = payload.get("Client")
    server_value = payload.get("Server")
    if not isinstance(client_value, Mapping) or not isinstance(server_value, Mapping):
        return None
    client = client_value.get("Version")
    server = server_value.get("Version")
    if not isinstance(client, str) or not isinstance(server, str):
        return None
    return (client, server) if client and server else None


def _parse_endpoint_digest(stdout: str) -> str | None:
    try:
        value: object = json.loads(stdout.strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, str)
        or not value
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_image_ref(stdout: str, expected: str) -> bool:
    try:
        payload: object = json.loads(stdout.strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, list):
        return False
    return any(isinstance(item, str) and item.strip() == expected for item in payload)


def docker_preflight(
    config: DockerSandboxConfig, runner: CommandRunner
) -> DockerPreflight:
    """Inspect only an audited context, daemon, and local immutable image."""

    validate_docker_sandbox_config(config)
    if (
        AUDITED_OPENCLAW_IMAGE_PIN is None
        or config.image != AUDITED_OPENCLAW_IMAGE_PIN
        or _IMAGE.fullmatch(config.image) is None
    ):
        return _blocked("blocked-image", config.context)
    if (
        AUDITED_DOCKER_CONTEXT is None
        or config.context != AUDITED_DOCKER_CONTEXT
        or _CONTEXT.fullmatch(config.context) is None
    ):
        return _blocked("blocked-context", config.context)
    if (
        AUDITED_DOCKER_ENDPOINT_SHA256 is None
        or _SHA256.fullmatch(AUDITED_DOCKER_ENDPOINT_SHA256) is None
    ):
        return _blocked("blocked-context-endpoint", config.context)

    context_result, context_error = _safe_run(
        runner,
        _docker_command(config, "context", "show"),
        timeout_seconds=DOCKER_PREFLIGHT_TIMEOUT_SECONDS,
    )
    if context_error == "timeout":
        return _blocked("docker-context-timeout", config.context)
    if context_error is not None or context_result is None:
        return _blocked("docker-context-unavailable", config.context)
    if context_result.stdout.strip() != config.context:
        return _blocked("docker-context-mismatch", config.context)

    endpoint_result, endpoint_error = _safe_run(
        runner,
        _docker_command(
            config,
            "context",
            "inspect",
            config.context,
            "--format",
            "{{json .Endpoints.docker.Host}}",
        ),
        timeout_seconds=DOCKER_PREFLIGHT_TIMEOUT_SECONDS,
    )
    if endpoint_error == "timeout":
        return _blocked("docker-context-endpoint-timeout", config.context)
    if endpoint_error is not None or endpoint_result is None:
        return _blocked("docker-context-endpoint-unavailable", config.context)
    endpoint_digest = _parse_endpoint_digest(endpoint_result.stdout)
    if endpoint_digest != AUDITED_DOCKER_ENDPOINT_SHA256:
        return _blocked("blocked-context-endpoint", config.context)

    version_result, version_error = _safe_run(
        runner,
        _docker_command(
            config,
            "version",
            "--format",
            "{{.Client.Version}}\\t{{.Server.Version}}",
        ),
        timeout_seconds=DOCKER_PREFLIGHT_TIMEOUT_SECONDS,
    )
    if version_error == "timeout":
        return _blocked("docker-daemon-timeout", config.context)
    if version_error is not None or version_result is None:
        return _blocked("docker-daemon-unavailable", config.context)
    versions = _parse_versions(version_result.stdout)
    if versions is None:
        return _blocked("docker-daemon-unavailable", config.context)
    client_version, server_version = versions

    image_result, image_error = _safe_run(
        runner,
        _docker_command(
            config,
            "image",
            "inspect",
            config.image,
            "--format",
            "{{json .RepoDigests}}",
        ),
        timeout_seconds=DOCKER_IMAGE_INSPECT_TIMEOUT_SECONDS,
    )
    if image_error == "timeout":
        return _blocked("docker-image-timeout", config.context)
    if image_error is not None or image_result is None:
        return _blocked("docker-image-unavailable", config.context)
    if not _parse_image_ref(image_result.stdout, config.image):
        return _blocked("blocked-image", config.context)
    return DockerPreflight(
        "ready",
        context=config.context,
        client_version=client_version,
        server_version=server_version,
        image_ref=config.image,
    )


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
            hashlib.sha256(
                json.dumps(
                    list(OPENCLAW_VERSION_PROBE_ARGV),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
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
    """Run identity and Docker read-only checks; never run a container."""

    def __init__(
        self,
        executable: Path,
        config: DockerSandboxConfig,
        runner: CommandRunner,
    ) -> None:
        self.executable = executable
        self.config = config
        self.runner = runner

    def _version_output(self, identity: OpenClawIdentity) -> str:
        result, error = _safe_run(
            self.runner,
            (str(identity.path), "--version"),
            timeout_seconds=OPENCLAW_VERSION_TIMEOUT_SECONDS,
        )
        if error == "timeout":
            _fail("OpenClaw version preflight timed out")
        if error is not None or result is None:
            _fail("OpenClaw version preflight failed")
        return result.stdout

    def preflight(self) -> OpenClawPreflightReport:
        """Verify file identity before invoking its canonical path for version."""

        validate_docker_sandbox_config(self.config)
        identity = resolve_openclaw_identity(self.executable)
        observed_version = self._version_output(identity)
        _version_banner(observed_version)
        identity = resolve_openclaw_identity(identity.path, observed_version)
        docker = docker_preflight(self.config, self.runner)
        if docker.status == "blocked":
            receipts = (
                build_blocked_receipt(identity, "read-only", "docker"),
                build_blocked_receipt(identity, "workspace-write", "docker"),
            )
            read_status = write_status = OpenClawProbeStatus.BLOCKED
            read_reason = write_reason = docker.reason or "docker-preflight-blocked"
            status = OpenClawProbeStatus.BLOCKED
        else:
            receipts = (
                build_not_run_receipt(identity, "read-only"),
                build_not_run_receipt(identity, "workspace-write"),
            )
            read_status = write_status = OpenClawProbeStatus.NOT_RUN
            read_reason = write_reason = "live-matrix-not-run"
            status = OpenClawProbeStatus.READY
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
