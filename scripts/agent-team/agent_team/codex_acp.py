"""Native scoped Codex ACP lifecycle boundary.

This module deliberately owns only the files needed to connect the selected
Codex ACP profile to the native lifecycle.  It does not resolve another
provider, refresh authentication, or execute a prompt.  The app-server
inspection is performed through :class:`~agent_team.adapters.ProcessRunner`
using the already selected launcher and a closed private environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

from .adapters import ExecutionError, ProcessRunner
from .codex_preflight import (
    assert_no_system_configuration,
    inspect_file_auth,
    project_configuration_snapshot,
)
from .native_acp_dependencies import (
    CodexAcpExecutables,
    NativeAcpDependencyError,
)
from .runtime import RuntimeValidationError
from .scoped_acp import CODEX_SCOPED_ADAPTER_ID, checked_digest, policy_payload
from .task_spec import TaskSpec

ADAPTER_ID = CODEX_SCOPED_ADAPTER_ID

RUNTIME_FILENAMES: tuple[str, ...] = (
    "codex_scoped_launch.mjs",
    "codex_scoped_inspect.mjs",
    "codex_scoped_transport.mjs",
    "codex_scoped_bridge.mjs",
    "scoped_file_tools.mjs",
    "scoped_policy.mjs",
    "scoped_acp_client.mjs",
)
_MODULE_ROOT = Path(__file__).resolve().parent
_LAUNCHER = _MODULE_ROOT / "codex_scoped_launch.mjs"
_RUNTIME_FILES: Mapping[str, Path] = {
    name: _MODULE_ROOT / name for name in RUNTIME_FILENAMES
}

_MANIFEST_FIELDS = frozenset(
    {
        "version",
        "codex",
        "codex_sha256",
        "policy_sha256",
        "config_sha256",
        "auth_path",
        "auth_sha256",
        "auth_expires_at",
        "model",
        "effort",
        "instructions",
        "config_snapshot",
    }
)
_SNAPSHOT_FIELDS = frozenset(
    {
        "runtime_sha256",
        "auth_path",
        "auth_sha256",
        "auth_expires_at",
        "project_configs",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONFIG_CONTENT = 'cli_auth_credentials_store="file"\n'
_INSPECTION_TIMEOUT_SECONDS = 30.0
_PROXY_NAME = "codex-proxy"
_LAUNCH_NAME = "codex-launch.json"
_POLICY_NAME = "write-policy.json"
_EXPECTED_ROOT_ENTRIES = frozenset(
    {"home", "codex-home", "tmp", _POLICY_NAME, _LAUNCH_NAME, _PROXY_NAME}
)


def _fail(message: str) -> NoReturn:
    raise RuntimeValidationError(message)


def _text(value: object, label: str, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"Codex ACP {label} is invalid")
    if "\x00" in value:
        _fail(f"Codex ACP {label} is invalid")
    if any(
        ord(character) < 0x20 and (not multiline or ord(character) not in {9, 10, 13})
        for character in value
    ):
        _fail(f"Codex ACP {label} is invalid")
    return value


def _canonical(path: Path, label: str, *, directory: bool = False) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        _fail(f"Codex ACP {label} path is invalid")
    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except (OSError, RuntimeError, ValueError):
        _fail(f"Codex ACP {label} is unavailable")
    if resolved != path or (directory and not stat.S_ISDIR(info.st_mode)):
        _fail(f"Codex ACP {label} path is not canonical")
    if not directory and not stat.S_ISREG(info.st_mode):
        _fail(f"Codex ACP {label} is not a regular file")
    if stat.S_ISLNK(info.st_mode):
        _fail(f"Codex ACP {label} must not be a symlink")
    return path


def _digest(path: Path, *, private: bool = False, executable: bool = False) -> str:
    try:
        value = checked_digest(path, private=private)
        if executable:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
                _fail("Codex ACP executable binding is invalid")
        return value
    except RuntimeValidationError:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail("Codex ACP artifact cannot be fingerprinted")


def _runtime_digests() -> dict[str, str]:
    if set(_RUNTIME_FILES) != set(RUNTIME_FILENAMES):
        _fail("Codex ACP runtime file binding is invalid")
    result: dict[str, str] = {}
    for name in RUNTIME_FILENAMES:
        path = _RUNTIME_FILES.get(name)
        if not isinstance(path, Path):
            _fail("Codex ACP runtime file binding is invalid")
        _canonical(path, f"runtime {name}")
        result[name] = _digest(path)
    return result


def _snapshot_shape(value: Mapping[str, object]) -> None:
    if set(value) != _SNAPSHOT_FIELDS:
        _fail("Codex ACP provider snapshot fields are invalid")


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(f"Codex ACP {label} is invalid")
    return value


def _project_configs(value: object) -> dict[str, str | None]:
    if not isinstance(value, Mapping):
        _fail("Codex ACP project configuration snapshot is invalid")
    result: dict[str, str | None] = {}
    for raw_path, raw_digest in value.items():
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            _fail("Codex ACP project configuration snapshot is invalid")
        if raw_digest is None:
            result[raw_path] = None
            continue
        _sha(raw_digest, "project configuration digest")
        result[raw_path] = raw_digest
    return result


def _snapshot_auth(value: Mapping[str, object]) -> tuple[Path, str, int]:
    raw_path = value.get("auth_path")
    if not isinstance(raw_path, str):
        _fail("Codex ACP authentication binding is missing")
    path = _canonical(Path(raw_path), "authentication source")
    digest = _sha(value.get("auth_sha256"), "authentication digest")
    expiry = value.get("auth_expires_at")
    if not isinstance(expiry, int) or isinstance(expiry, bool) or expiry <= 0:
        _fail("Codex ACP authentication expiry is invalid")
    return path, digest, expiry


def snapshot(workspace: Path, auth_path: Path) -> dict[str, object]:
    """Capture all immutable bindings used by a selected Codex ACP profile."""

    assert_no_system_configuration()
    workspace = _canonical(workspace, "workspace", directory=True)
    auth_path = _canonical(auth_path, "authentication source")
    binding = inspect_file_auth(auth_path)
    return {
        "runtime_sha256": _runtime_digests(),
        "auth_path": str(auth_path),
        "auth_sha256": binding.sha256,
        "auth_expires_at": binding.expires_at,
        "project_configs": project_configuration_snapshot(workspace),
    }


def verify_snapshot(value: Mapping[str, object], workspace: Path) -> None:
    """Fail closed if any preflight binding changed since team selection."""

    if not isinstance(value, Mapping):
        _fail("Codex ACP provider snapshot is invalid")
    _snapshot_shape(value)
    workspace = _canonical(workspace, "workspace", directory=True)
    assert_no_system_configuration()
    runtime = value.get("runtime_sha256")
    if not isinstance(runtime, Mapping) or dict(runtime) != {
        name: runtime.get(name) for name in RUNTIME_FILENAMES
    }:
        _fail("Codex ACP runtime binding is invalid")
    if dict(runtime) != _runtime_digests():
        _fail("Codex ACP runtime binding changed")
    auth_path, auth_digest, expiry = _snapshot_auth(value)
    binding = inspect_file_auth(auth_path)
    if (
        binding.path != auth_path
        or binding.sha256 != auth_digest
        or binding.expires_at != expiry
    ):
        _fail("Codex ACP authentication binding changed")
    expected_configs = project_configuration_snapshot(workspace)
    if _project_configs(value.get("project_configs")) != expected_configs:
        _fail("Codex ACP project configuration changed")


def _private_root(path: Path, *, empty: bool) -> Path:
    root = _canonical(path, "private root", directory=True)
    try:
        info = root.lstat()
        entries = tuple(root.iterdir())
    except OSError:
        _fail("Codex ACP private root is unavailable")
    if (
        info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or (empty and entries)
    ):
        _fail("Codex ACP private root is not a new 0700 directory")
    return root


def _mkdir_private(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
        info = path.lstat()
    except OSError:
        _fail("Codex ACP private directory could not be created")
    if (
        info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or not stat.S_ISDIR(info.st_mode)
    ):
        _fail("Codex ACP private directory is unsafe")


def _write_new(path: Path, contents: bytes, mode: int) -> None:
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
        )
        with os.fdopen(fd, "wb") as target:
            target.write(contents)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(path, mode)
    except (OSError, ValueError):
        _fail("Codex ACP private artifact could not be written")


def _replace_private(path: Path, contents: bytes, mode: int) -> None:
    pending = path.with_name(f".{path.name}.pending")
    try:
        _write_new(pending, contents, mode)
        os.replace(pending, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except RuntimeValidationError:
        try:
            pending.unlink()
        except OSError:
            pass
        raise
    except OSError:
        try:
            pending.unlink()
        except OSError:
            pass
        _fail("Codex ACP private artifact could not be replaced")


def _write_json(path: Path, value: Mapping[str, object], mode: int) -> None:
    try:
        contents = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail("Codex ACP metadata is not serializable")
    _write_new(path, contents, mode)


def _append_protected(protected: list[str], path: Path, label: str) -> None:
    canonical = _canonical(path, label, directory=path.is_dir())
    text = str(canonical)
    if text not in protected:
        protected.append(text)


def _policy(
    private_root: Path,
    workspace: Path,
    state_path: Path,
    task: TaskSpec | None,
    permission: str,
    executables: CodexAcpExecutables,
    provider_snapshot: Mapping[str, object],
) -> dict[str, object]:
    try:
        value = policy_payload(
            private_root,
            workspace,
            state_path,
            task,
            executables.agent,
            permission=permission,
        )
    except (RuntimeValidationError, OSError, ValueError):
        _fail("Codex ACP write policy could not be built")
    protected = value.get("protected_paths")
    if not isinstance(protected, list) or any(
        not isinstance(item, str) for item in protected
    ):
        _fail("Codex ACP write policy is invalid")
    for path in (
        executables.node,
        executables.agent,
        executables.sdk,
        executables.codex,
    ):
        _append_protected(protected, path, "selected executable")
    for path in (
        executables.agent.parent.parent,
        executables.sdk.parent.parent,
        executables.codex.parent,
    ):
        _append_protected(protected, path, "selected dependency root")
    auth_path, _digest_value, _expiry = _snapshot_auth(provider_snapshot)
    _append_protected(protected, auth_path, "authentication source")
    configs = _project_configs(provider_snapshot.get("project_configs"))
    for raw_path in configs:
        candidate = Path(raw_path)
        if candidate.exists():
            _append_protected(protected, candidate, "project configuration")
        elif candidate.parent.is_dir() and candidate.parent.name == ".codex":
            _append_protected(protected, candidate.parent, "project configuration")
    return value


def _manifest(
    executables: CodexAcpExecutables,
    policy_digest: str,
    config_digest: str,
    provider_snapshot: Mapping[str, object],
    model: str,
    effort: str,
    instructions: str,
    config_snapshot: str | None,
) -> dict[str, object]:
    auth_path, auth_digest, expiry = _snapshot_auth(provider_snapshot)
    return {
        "version": 1,
        "codex": str(executables.codex),
        "codex_sha256": executables.codex_sha256,
        "policy_sha256": policy_digest,
        "config_sha256": config_digest,
        "auth_path": str(auth_path),
        "auth_sha256": auth_digest,
        "auth_expires_at": expiry,
        "model": _text(model, "model"),
        "effort": _text(effort, "effort"),
        "instructions": _text(instructions, "instructions", multiline=True),
        "config_snapshot": config_snapshot,
    }


def _manifest_digest(path: Path) -> str:
    return _digest(path, private=True)


def _inspection_environment(private_root: Path) -> dict[str, str]:
    home = private_root / "home"
    return {
        "HOME": str(home),
        "CODEX_HOME": str(private_root / "codex-home"),
        "TMPDIR": str(private_root / "tmp"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "PATH": "/usr/bin:/bin",
    }


def _inspection_snapshot(stdout: object) -> str:
    if not isinstance(stdout, str):
        _fail("Codex ACP configuration inspection returned an invalid result")
    stdout = stdout.removesuffix("\n")
    if not stdout or "\n" in stdout:
        _fail("Codex ACP configuration inspection returned an invalid result")
    try:
        value = json.loads(stdout)
    except (ValueError, UnicodeError):
        _fail("Codex ACP configuration inspection returned an invalid result")
    if not isinstance(value, dict) or set(value) != {"configSnapshot"}:
        _fail("Codex ACP configuration inspection returned an invalid result")
    return _sha(value.get("configSnapshot"), "configuration snapshot")


def _inspect_config(
    private_root: Path,
    workspace: Path,
    manifest_path: Path,
    manifest_digest: str,
    executables: CodexAcpExecutables,
) -> str:
    argv = [
        str(executables.node),
        str(_LAUNCHER),
        "--manifest",
        str(manifest_path),
        "--sha256",
        manifest_digest,
        "inspect-config",
    ]
    try:
        result = ProcessRunner().run(
            argv,
            cwd=private_root,
            env=_inspection_environment(private_root),
            timeout_seconds=_INSPECTION_TIMEOUT_SECONDS,
        )
    except ExecutionError as exc:
        # ProcessRunner owns exact-child cleanup; retain only that status and
        # discard provider stderr or exception text from the public boundary.
        raise ExecutionError(
            "Codex configuration inspection failed",
            cleanup_confirmed=exc.cleanup_confirmed,
        ) from None
    except (OSError, RuntimeError, ValueError):
        raise ExecutionError(
            "Codex configuration inspection could not be started",
            cleanup_confirmed=False,
        ) from None
    if result.returncode != 0:
        raise ExecutionError(
            "Codex configuration inspection failed", cleanup_confirmed=True
        )
    try:
        return _inspection_snapshot(result.stdout)
    except RuntimeValidationError:
        raise ExecutionError(
            "Codex configuration inspection returned an invalid result",
            cleanup_confirmed=True,
        ) from None


def _proxy_contents(node: Path, manifest: Path, digest: str) -> bytes:
    command = shlex.join(
        [str(node), str(_LAUNCHER), "--manifest", str(manifest), "--sha256", digest]
    )
    return f'#!/bin/sh\nexec {command} "$@"\n'.encode()


def agent_command(executables: CodexAcpExecutables) -> str:
    try:
        executables.verify()
    except NativeAcpDependencyError:
        _fail("Codex ACP executable binding changed")
    return shlex.join([str(executables.node), str(executables.agent)])


def environment(private_root: Path, executables: CodexAcpExecutables) -> dict[str, str]:
    root = _private_root(private_root, empty=False)
    try:
        executables.verify()
    except NativeAcpDependencyError:
        _fail("Codex ACP executable binding changed")
    proxy = root / _PROXY_NAME
    _checked_proxy(proxy)
    value = _inspection_environment(root)
    value["CODEX_PATH"] = str(proxy)
    return value


def prepare_assignment(
    private_root: Path,
    workspace: Path,
    state_path: Path,
    task: TaskSpec | None,
    permission: str,
    executables: CodexAcpExecutables,
    model: str,
    effort: str,
    instructions: str,
    provider_snapshot: Mapping[str, object],
) -> dict[str, object]:
    """Prepare an assignment; the native caller owns root and failure cleanup."""

    # These checks intentionally precede TaskSpec serialization, filesystem
    # creation, and ProcessRunner invocation.
    try:
        executables.verify()
    except NativeAcpDependencyError:
        _fail("Codex ACP executable binding changed")
    verify_snapshot(provider_snapshot, workspace)
    root = _private_root(private_root, empty=True)
    workspace = _canonical(workspace, "workspace", directory=True)
    state_path = (
        _canonical(state_path.parent, "state directory", directory=True)
        / state_path.name
    )
    if not isinstance(task, (TaskSpec, type(None))):
        _fail("Codex ACP task specification is invalid")
    _text(model, "model")
    _text(effort, "effort")
    _text(instructions, "instructions", multiline=True)
    policy_value = _policy(
        root, workspace, state_path, task, permission, executables, provider_snapshot
    )

    home = root / "home"
    codex_home = root / "codex-home"
    temporary = root / "tmp"
    for directory in (home, codex_home, temporary):
        _mkdir_private(directory)
    config_path = codex_home / "config.toml"
    _write_new(config_path, _CONFIG_CONTENT.encode("utf-8"), 0o600)
    auth_path, _auth_digest, _expiry = _snapshot_auth(provider_snapshot)
    auth_link = codex_home / "auth.json"
    try:
        os.symlink(str(auth_path), auth_link)
    except OSError:
        _fail("Codex ACP authentication link could not be created")

    policy_path = root / _POLICY_NAME
    _write_json(policy_path, policy_value, 0o600)
    policy_digest = _digest(policy_path, private=True)
    config_digest = _digest(config_path, private=True)
    launch_path = root / _LAUNCH_NAME
    initial_manifest = _manifest(
        executables,
        policy_digest,
        config_digest,
        provider_snapshot,
        model,
        effort,
        instructions,
        None,
    )
    _write_json(launch_path, initial_manifest, 0o600)
    initial_launch_digest = _manifest_digest(launch_path)
    config_snapshot = _inspect_config(
        root, workspace, launch_path, initial_launch_digest, executables
    )

    # Recheck every team binding after the child inspection and before the
    # digest-bound manifest or proxy becomes usable by the ACP client.
    executables.verify()
    verify_snapshot(provider_snapshot, workspace)
    bound_manifest = _manifest(
        executables,
        policy_digest,
        config_digest,
        provider_snapshot,
        model,
        effort,
        instructions,
        config_snapshot,
    )
    _replace_private(
        launch_path,
        json.dumps(
            bound_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"),
        0o600,
    )
    launch_digest = _manifest_digest(launch_path)
    proxy_path = root / _PROXY_NAME
    _write_new(
        proxy_path, _proxy_contents(executables.node, launch_path, launch_digest), 0o700
    )
    proxy_digest = _checked_proxy(proxy_path)
    return {
        "write_policy_path": str(policy_path),
        "write_policy_sha256": policy_digest,
        "codex_launch_path": str(launch_path),
        "codex_launch_sha256": launch_digest,
        "codex_proxy_path": str(proxy_path),
        "codex_proxy_sha256": proxy_digest,
        "codex_config_snapshot": config_snapshot,
    }


def _checked_proxy(path: Path) -> str:
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_nlink != 1
        ):
            _fail("Codex ACP proxy is unsafe")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                _fail("Codex ACP proxy changed while opening")
            with os.fdopen(fd, "rb", closefd=False) as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            current = os.fstat(fd)
            latest = path.lstat()
            if (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            ) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) or (
                latest.st_dev,
                latest.st_ino,
                latest.st_size,
                latest.st_mtime_ns,
            ) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
                _fail("Codex ACP proxy changed while reading")
            return digest
        finally:
            os.close(fd)
    except RuntimeValidationError:
        raise
    except OSError:
        _fail("Codex ACP proxy is unavailable")


def _assignment_path(
    assignment: Mapping[str, object], root: Path, key: str, name: str
) -> Path:
    raw = assignment.get(key)
    expected = root / name
    if not isinstance(raw, str) or raw != str(expected):
        _fail(f"Codex ACP assignment {key} is invalid")
    return expected


def _manifest_value(path: Path) -> dict[str, object]:
    try:
        contents = path.read_bytes()
        value = json.loads(contents.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        _fail("Codex ACP launch manifest is invalid")
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        _fail("Codex ACP launch manifest fields are invalid")
    return value


def _task_from_assignment(assignment: Mapping[str, object]) -> TaskSpec | None:
    raw = assignment.get("task_spec")
    if raw is None:
        return None
    try:
        return TaskSpec.from_dict(raw)
    except (TypeError, ValueError):
        _fail("Codex ACP assignment TaskSpec is invalid")


def validate_assignment(
    state: Mapping[str, object],
    assignment: Mapping[str, object],
    spec: Mapping[str, object],
) -> None:
    """Revalidate private artifacts and immutable provider bindings before spawn.

    The caller validates the native role profile and saved TaskSpec/dispatch
    identity through the common lifecycle; this boundary binds their artifacts.
    """

    if (
        not isinstance(state, Mapping)
        or not isinstance(assignment, Mapping)
        or not isinstance(spec, Mapping)
    ):
        _fail("Codex ACP assignment boundary is invalid")
    if assignment.get("adapter_id") != ADAPTER_ID:
        _fail("Codex ACP assignment adapter does not match")
    raw_workspace = state.get("workspace")
    raw_state_path = state.get("state_path")
    if not isinstance(raw_workspace, str) or not isinstance(raw_state_path, str):
        _fail("Codex ACP state binding is missing")
    workspace = _canonical(Path(raw_workspace), "workspace", directory=True)
    state_path = Path(raw_state_path)
    if not state_path.is_absolute():
        _fail("Codex ACP state path is invalid")
    provider_snapshot = spec.get("provider_snapshot")
    if not isinstance(provider_snapshot, Mapping):
        _fail("Codex ACP provider snapshot binding is missing")
    verify_snapshot(provider_snapshot, workspace)
    try:
        executables = CodexAcpExecutables.from_dict(spec.get("acp_executables"))
        executables.verify()
    except (NativeAcpDependencyError, TypeError, ValueError):
        _fail("Codex ACP executable binding changed")
    raw_root = assignment.get("provider_private_root")
    if not isinstance(raw_root, str):
        _fail("Codex ACP private root binding is missing")
    root = _private_root(Path(raw_root), empty=False)
    if raw_root != str(root):
        _fail("Codex ACP private root path is not canonical")
    try:
        if frozenset(path.name for path in root.iterdir()) != _EXPECTED_ROOT_ENTRIES:
            _fail("Codex ACP private root contains unexpected artifacts")
    except OSError:
        _fail("Codex ACP private root is unavailable")
    policy_path = _assignment_path(assignment, root, "write_policy_path", _POLICY_NAME)
    launch_path = _assignment_path(assignment, root, "codex_launch_path", _LAUNCH_NAME)
    proxy_path = _assignment_path(assignment, root, "codex_proxy_path", _PROXY_NAME)
    config_path = root / "codex-home" / "config.toml"
    auth_link = root / "codex-home" / "auth.json"
    for directory in (root / "home", root / "codex-home", root / "tmp"):
        _canonical(directory, "private directory", directory=True)
        info = directory.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            _fail("Codex ACP private directory is unsafe")
    _digest(policy_path, private=True)
    _digest(config_path, private=True)
    launch_digest = _manifest_digest(launch_path)
    proxy_digest = _checked_proxy(proxy_path)
    if assignment.get("write_policy_sha256") != _digest(policy_path, private=True):
        _fail("Codex ACP write policy changed")
    if assignment.get("codex_launch_sha256") != launch_digest:
        _fail("Codex ACP launch manifest changed")
    if assignment.get("codex_proxy_sha256") != proxy_digest:
        _fail("Codex ACP proxy changed")
    manifest = _manifest_value(launch_path)
    permission = spec.get("permission")
    if not isinstance(permission, str):
        _fail("Codex ACP permission is missing")
    task = _task_from_assignment(assignment)
    expected_policy = _policy(
        root,
        workspace,
        state_path,
        task,
        permission,
        executables,
        provider_snapshot,
    )
    try:
        actual_policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        _fail("Codex ACP write policy is invalid")
    if actual_policy != expected_policy:
        _fail("Codex ACP write policy does not match the saved TaskSpec")
    config_bytes = config_path.read_bytes()
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    if config_bytes != _CONFIG_CONTENT.encode("utf-8"):
        _fail("Codex ACP private configuration changed")
    auth_path, auth_digest, expiry = _snapshot_auth(provider_snapshot)
    if not auth_link.is_symlink() or os.readlink(auth_link) != str(auth_path):
        _fail("Codex ACP authentication link changed")
    expected_manifest = _manifest(
        executables,
        _digest(policy_path, private=True),
        config_digest,
        provider_snapshot,
        _text(spec.get("model"), "model"),
        _text(spec.get("effort"), "effort"),
        _text(spec.get("instructions"), "instructions", multiline=True),
        _sha(assignment.get("codex_config_snapshot"), "configuration snapshot"),
    )
    if manifest != expected_manifest:
        _fail("Codex ACP launch manifest does not match the saved assignment")
    expected_proxy = _proxy_contents(executables.node, launch_path, launch_digest)
    if proxy_path.read_bytes() != expected_proxy:
        _fail("Codex ACP proxy content changed")
    if assignment.get("agent_command") != agent_command(executables):
        _fail("Codex ACP agent command changed")
    if assignment.get("codex_config_snapshot") != manifest["config_snapshot"]:
        _fail("Codex ACP configuration snapshot changed")
    if (
        manifest["auth_path"] != str(auth_path)
        or manifest["auth_sha256"] != auth_digest
        or manifest["auth_expires_at"] != expiry
    ):
        _fail("Codex ACP authentication manifest binding changed")
