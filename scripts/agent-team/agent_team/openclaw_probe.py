"""Fail-closed preflight and future live-run seam for OpenClaw Docker cells.

This module deliberately does not start Docker, pull an image, or contact a
provider during import or object construction.  A caller must inject a
command runner and explicitly authorize a live run after the read-only
preflight has established the exact executable, daemon, and image identity.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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
DOCKER_LIVE_TIMEOUT_SECONDS: Final = 900.0
REDACTED_EXECUTABLE_PATH: Final = "/redacted/openclaw/2026.7.1/bin/openclaw"
REDACTED_WORKSPACE_PATH: Final = "/redacted/openclaw/probe-workspace"
SAFE_ENVIRONMENT_NAMES: Final = frozenset(
    {"HOME", "LANG", "LC_ALL", "PATH", "SHELL", "TERM", "TMPDIR", "USER"}
)

CellId = Literal["direct-sandbox-off", "docker-read-only", "docker-workspace-write"]
DockerStatus = Literal["ready", "blocked"]
LiveStatus = Literal["passed", "failed", "timeout", "blocked", "rejected"]

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTEXT = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_IMAGE = re.compile(r"[A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}\Z")
_SESSION = re.compile(r"agent:issue9-openclaw:[a-zA-Z0-9._-]{8,64}\Z")
_PREFIX = re.compile(r"[a-z][a-z0-9_.-]{0,39}-\Z")
_ENVIRONMENT = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SENSITIVE_NAME = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|bearer|cookie|password|secret|credential)"
)


class OpenClawProbeError(RuntimeError):
    """Raised when an OpenClaw identity or safety policy cannot be verified."""


class OpenClawProbeStatus(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    NOT_RUN = "not-run"


class CommandRunner(Protocol):
    """Small injectable seam; implementations must not invoke a shell."""

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProcessResult: ...


def _fail(message: str) -> NoReturn:
    raise OpenClawProbeError(message)


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{field} must be a non-empty string")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        _fail(f"{field} contains a control character")
    return value


@dataclass(frozen=True, slots=True)
class OpenClawIdentity:
    """Verified runtime identity kept in memory; receipt paths are redacted."""

    path: Path
    version: str
    sha256: str
    build: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            _fail("OpenClaw executable path must be absolute")
        if self.version != OPENCLAW_VERSION:
            _fail("identity is not the exact OpenClaw 2026.7.1 version")
        if self.build != OPENCLAW_BUILD:
            _fail("identity is not the exact OpenClaw 2026.7.1 build")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            _fail("OpenClaw executable SHA-256 is invalid")

    @property
    def version_banner(self) -> str:
        return f"OpenClaw {self.version} ({self.build})"

    def as_receipt_identity(self) -> ExecutableIdentity:
        """Return a public identity without retaining the user's executable path."""

        return ExecutableIdentity(
            REDACTED_EXECUTABLE_PATH,
            self.version_banner,
            self.sha256,
        )


def _version_banner(value: str) -> str:
    if not isinstance(value, str):
        _fail("OpenClaw version output must be text")
    banner = value.strip()
    expected = f"OpenClaw {OPENCLAW_VERSION} ({OPENCLAW_BUILD})"
    if banner != expected:
        _fail("executable did not report the exact OpenClaw 2026.7.1 identity")
    return banner


def resolve_openclaw_identity(
    executable: Path, observed_version: str
) -> OpenClawIdentity:
    """Verify the canonical executable bytes and exact version banner."""

    if not isinstance(executable, Path):
        _fail("OpenClaw executable must be a Path")
    try:
        canonical = executable.resolve(strict=True)
        payload = canonical.read_bytes()
    except OSError as exc:
        raise OpenClawProbeError("OpenClaw executable could not be inspected") from exc
    if (
        not canonical.is_file()
        or executable.name != "openclaw"
        or canonical.name not in {"openclaw", "openclaw.mjs"}
    ):
        _fail("OpenClaw executable is not the canonical openclaw file")
    _version_banner(observed_version)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != OPENCLAW_EXECUTABLE_SHA256:
        _fail("OpenClaw executable SHA-256 identity drifted")
    return OpenClawIdentity(canonical, OPENCLAW_VERSION, digest, OPENCLAW_BUILD)


@dataclass(frozen=True, slots=True)
class DockerSandboxConfig:
    """Immutable, explicit Docker policy for one disposable OpenClaw cell."""

    context: str
    image: str
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
    container_name_prefix: str = "agent-team-openclaw-"
    provider: str = "openclaw"
    transport: str = "acp"
    model: str = "fixed"
    max_provider_turns: int = OPENCLAW_MAX_PROVIDER_TURNS
    quota_budget: int = OPENCLAW_QUOTA_BUDGET
    auto_pull: bool = False


def validate_docker_sandbox_config(config: DockerSandboxConfig) -> None:
    """Reject mutable images, broad mounts, and privileged or credentialed cells."""

    if not isinstance(config, DockerSandboxConfig):
        _fail("Docker sandbox config has an invalid type")
    if (
        not isinstance(config.context, str)
        or _CONTEXT.fullmatch(config.context) is None
    ):
        _fail("Docker context is invalid")
    if not isinstance(config.image, str) or _IMAGE.fullmatch(config.image) is None:
        _fail("Docker image must include an immutable sha256 digest")
    if (
        not isinstance(config.mount_source, Path)
        or not config.mount_source.is_absolute()
    ):
        _fail("Docker mount source must be an absolute path")
    source_text = str(config.mount_source)
    if any(char in source_text for char in ",\x00\r\n"):
        _fail("Docker mount source contains an unsafe character")
    if not config.mount_source.exists() or not config.mount_source.is_dir():
        _fail("Docker mount source must be an existing directory")
    if config.mount_source.is_symlink():
        _fail("Docker mount source must not be a symlink")
    try:
        canonical_source = config.mount_source.resolve(strict=True)
    except OSError as exc:
        raise OpenClawProbeError("Docker mount source could not be inspected") from exc
    home = Path.home().resolve()
    if canonical_source == home or home in canonical_source.parents:
        _fail("Docker mount source must not be the user's home directory")
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
    if not isinstance(config.credential_mounts, tuple):
        _fail("credential mounts must be a tuple")
    if any(not isinstance(item, str) or not item for item in config.credential_mounts):
        _fail("credential mounts must contain non-empty strings")
    if config.credential_mounts:
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
    if (
        not isinstance(config.container_name_prefix, str)
        or _PREFIX.fullmatch(config.container_name_prefix) is None
    ):
        _fail("container name prefix is invalid")
    if not isinstance(config.provider, str) or config.provider != "openclaw":
        _fail("provider fallback is forbidden")
    if not isinstance(config.transport, str) or config.transport != "acp":
        _fail("OpenClaw Docker transport must be acp")
    model = _require_text(config.model, "model")
    if _SENSITIVE_NAME.search(model) is not None:
        _fail("model contains a sensitive value marker")
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
    if not isinstance(config.auto_pull, bool):
        _fail("automatic pull flag must be boolean")
    if config.auto_pull:
        _fail("automatic image pull is forbidden")


@dataclass(frozen=True, slots=True)
class DockerPreflight:
    status: DockerStatus
    context: str | None = None
    client_version: str | None = None
    server_version: str | None = None
    image_digest: str | None = None
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
    if not value or "\n" in value:
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
    client_version = client_value.get("Version")
    server_version = server_value.get("Version")
    if not isinstance(client_version, str) or not isinstance(server_version, str):
        return None
    return (
        (client_version, server_version) if client_version and server_version else None
    )


def _expected_image_digest(image: str) -> str:
    return image.rsplit("@", 1)[1]


def _parse_image_digest(stdout: str, expected: str) -> bool:
    value = stdout.strip()
    try:
        payload: object = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = value
    values: list[str] = []
    if isinstance(payload, list):
        values = [item.strip() for item in payload if isinstance(item, str)]
    elif isinstance(payload, str):
        values = [payload]
    return any(item.endswith("@" + expected) or item == expected for item in values)


def docker_preflight(
    config: DockerSandboxConfig, runner: CommandRunner
) -> DockerPreflight:
    """Perform only context, daemon, and local immutable-image inspections."""

    validate_docker_sandbox_config(config)
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
    expected_digest = _expected_image_digest(config.image)
    if not _parse_image_digest(image_result.stdout, expected_digest):
        return _blocked("docker-image-digest-mismatch", config.context)
    return DockerPreflight(
        "ready",
        context=config.context,
        client_version=client_version,
        server_version=server_version,
        image_digest=expected_digest,
    )


@dataclass(frozen=True, slots=True)
class ReceiptBundle:
    receipt: Receipt
    judgment: Judgment


def _fixed_argv_digest() -> str:
    argv = ("openclaw", "acp", "--sandbox", "docker")
    payload = json.dumps(list(argv), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _receipt_manifest(identity: OpenClawIdentity, profile: str) -> Manifest:
    if profile not in {"read-only", "workspace-write"}:
        raise ReceiptValidationError("unsupported OpenClaw receipt profile")
    return Manifest(
        ProfileIdentity(
            CURRENT_SCHEMA_VERSION,
            "openclaw",
            profile,
            platform.system().lower(),
            platform.machine().lower(),
            OPENCLAW_PROBE_REVISION,
            identity.as_receipt_identity(),
            _fixed_argv_digest(),
            "argv",
            REDACTED_WORKSPACE_PATH,
            ("HOME", "PATH", "TMPDIR"),
            f"{OPENCLAW_SANDBOX_POLICY_ID}-{profile}",
        ),
        required_phases_for_profile(profile),
    )


def build_probe_manifest(
    identity: OpenClawIdentity,
    profile: Literal["read-only", "workspace-write"],
) -> Manifest:
    """Build the fixed, redacted manifest for one Docker permission cell."""

    if not isinstance(identity, OpenClawIdentity):
        raise ReceiptValidationError("OpenClaw manifest identity has an invalid type")
    return _receipt_manifest(identity, profile)


def build_blocked_receipt(
    identity: OpenClawIdentity,
    profile: Literal["read-only", "workspace-write"],
    blocked_reason: str,
) -> ReceiptBundle:
    """Build a redacted receipt with every cell explicitly unattempted."""

    if not isinstance(identity, OpenClawIdentity):
        raise ReceiptValidationError("OpenClaw receipt identity has an invalid type")
    if blocked_reason not in BLOCKER_CODES:
        raise ReceiptValidationError("unsupported blocked reason")
    manifest = _receipt_manifest(identity, profile)
    phases = tuple(
        # A blocked prerequisite is recorded before any provider/tool attempt.
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
    receipt = Receipt(manifest.identity, blocked_reason, phases)
    return ReceiptBundle(receipt, judge_profile(manifest, receipt))


def build_not_run_receipt(
    identity: OpenClawIdentity,
    profile: Literal["read-only", "workspace-write"],
) -> ReceiptBundle:
    """Build a receipt for a verified preflight whose live matrix is not run."""

    if not isinstance(identity, OpenClawIdentity):
        raise ReceiptValidationError("OpenClaw receipt identity has an invalid type")
    manifest = _receipt_manifest(identity, profile)
    receipt = Receipt(
        manifest.identity,
        None,
        tuple(
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
        ),
    )
    return ReceiptBundle(receipt, judge_profile(manifest, receipt))


@dataclass(frozen=True, slots=True)
class OpenClawCell:
    cell_id: CellId
    status: OpenClawProbeStatus
    reason: str
    receipt: ReceiptBundle | None = None


def direct_sandbox_off_cell() -> OpenClawCell:
    """Represent direct/sandbox-off as a non-candidate cell, never as safe."""

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
        """Compatibility convenience for the read-only Docker cell."""

        return self.receipts[0]


@dataclass(frozen=True, slots=True)
class LiveAuthorization:
    allow_container_execution: bool = False
    allow_provider_turn: bool = False
    session_key: str = ""


@dataclass(frozen=True, slots=True)
class LiveRunResult:
    status: LiveStatus
    reason: str | None = None
    timed_out: bool = False
    cleanup: CleanupInventory = field(default_factory=CleanupInventory)
    container_name: str | None = None


class OpenClawProbe:
    """Coordinate exact identity, Docker preflight, and an opt-in live seam."""

    def __init__(
        self,
        executable: Path,
        config: DockerSandboxConfig,
        runner: CommandRunner,
    ) -> None:
        self.executable = executable
        self.config = config
        self.runner = runner

    def _version_output(self) -> str:
        result, error = _safe_run(
            self.runner,
            (str(self.executable), "--version"),
            timeout_seconds=OPENCLAW_VERSION_TIMEOUT_SECONDS,
        )
        if error == "timeout":
            _fail("OpenClaw version preflight timed out")
        if error is not None or result is None:
            _fail("OpenClaw version preflight failed")
        return result.stdout

    def preflight(self) -> OpenClawPreflightReport:
        """Run only identity and Docker read-only probes; no container is started."""

        validate_docker_sandbox_config(self.config)
        identity = resolve_openclaw_identity(
            self.executable,
            _version_banner(self._version_output()),
        )
        docker = docker_preflight(self.config, self.runner)
        if docker.status == "blocked":
            receipts = (
                build_blocked_receipt(identity, "read-only", "docker"),
                build_blocked_receipt(identity, "workspace-write", "docker"),
            )
            status = OpenClawProbeStatus.BLOCKED
            read_status = write_status = OpenClawProbeStatus.BLOCKED
            read_reason = write_reason = docker.reason or "docker-preflight-blocked"
        else:
            receipts = (
                build_not_run_receipt(identity, "read-only"),
                build_not_run_receipt(identity, "workspace-write"),
            )
            status = OpenClawProbeStatus.READY
            read_status = write_status = OpenClawProbeStatus.NOT_RUN
            read_reason = write_reason = "live-matrix-not-run"
        cells = (
            direct_sandbox_off_cell(),
            OpenClawCell("docker-read-only", read_status, read_reason, receipts[0]),
            OpenClawCell(
                "docker-workspace-write", write_status, write_reason, receipts[1]
            ),
        )
        return OpenClawPreflightReport(status, identity, docker, cells, receipts)

    def _container_name(self) -> str:
        digest = hashlib.sha256(self.config.session_key.encode("utf-8")).hexdigest()
        return f"{self.config.container_name_prefix}{digest[:16]}"

    def _create_argv(self, container_name: str) -> tuple[str, ...]:
        mount_source = self.config.mount_source.resolve(strict=True)
        mount = (
            f"type=bind,src={mount_source},dst={self.config.mount_target},"
            f"{'readonly' if self.config.mount_mode == 'ro' else 'rw'}"
        )
        label = (
            "agent-team.openclaw.session-sha256="
            + hashlib.sha256(self.config.session_key.encode("utf-8")).hexdigest()
        )
        return _docker_command(
            self.config,
            "create",
            "--name",
            container_name,
            "--label",
            label,
            "--network",
            OPENCLAW_DEFAULT_NETWORK,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--pull",
            "never",
            "--mount",
            mount,
            self.config.image,
            "openclaw",
            "acp",
        )

    def _cleanup(self, container_name: str) -> CleanupInventory:
        remove, remove_error = _safe_run(
            self.runner,
            _docker_command(self.config, "rm", "--force", container_name),
            timeout_seconds=DOCKER_PREFLIGHT_TIMEOUT_SECONDS,
        )
        inspect, inspect_error = _safe_run(
            self.runner,
            _docker_command(self.config, "inspect", container_name),
            timeout_seconds=DOCKER_PREFLIGHT_TIMEOUT_SECONDS,
        )
        if remove_error is not None or remove is None:
            return CleanupInventory(containers=1)
        if inspect is None or inspect_error == "timeout":
            return CleanupInventory(containers=1)
        if inspect_error is None and inspect.returncode == 0:
            return CleanupInventory(containers=1)
        return CleanupInventory()

    def run_live(self, authorization: LiveAuthorization) -> LiveRunResult:
        """Run one provider session only after explicit authorization and preflight."""

        validate_docker_sandbox_config(self.config)
        if (
            not isinstance(authorization, LiveAuthorization)
            or authorization.allow_container_execution is not True
            or authorization.allow_provider_turn is not True
            or authorization.session_key != self.config.session_key
        ):
            return LiveRunResult("blocked", "explicit-live-authorization-required")
        report = self.preflight()
        if report.docker.status != "ready":
            return LiveRunResult("blocked", report.docker.reason)
        try:
            resolve_openclaw_identity(
                self.executable,
                report.identity.version_banner,
            )
        except OpenClawProbeError:
            return LiveRunResult("rejected", "openclaw-identity-drift")
        container_name = self._container_name()
        create, create_error = _safe_run(
            self.runner,
            self._create_argv(container_name),
            timeout_seconds=DOCKER_LIVE_TIMEOUT_SECONDS,
        )
        result: LiveRunResult
        try:
            if create_error == "timeout":
                result = LiveRunResult(
                    "timeout",
                    "Docker container create timed out",
                    True,
                    container_name=container_name,
                )
            elif create_error is not None or create is None:
                result = LiveRunResult(
                    "failed",
                    "Docker container create failed",
                    container_name=container_name,
                )
            else:
                start, start_error = _safe_run(
                    self.runner,
                    _docker_command(self.config, "start", "--attach", container_name),
                    timeout_seconds=DOCKER_LIVE_TIMEOUT_SECONDS,
                )
                if start_error == "timeout":
                    result = LiveRunResult(
                        "timeout",
                        "OpenClaw container timed out",
                        True,
                        container_name=container_name,
                    )
                elif start_error is not None or start is None:
                    result = LiveRunResult(
                        "failed",
                        "OpenClaw container failed",
                        container_name=container_name,
                    )
                else:
                    result = LiveRunResult("passed", container_name=container_name)
        finally:
            cleanup = self._cleanup(container_name)
            if cleanup.has_residuals:
                result = LiveRunResult(
                    "rejected",
                    "container-cleanup-residual",
                    cleanup=cleanup,
                    container_name=container_name,
                )
            else:
                result = replace(result, cleanup=cleanup)
        return result
