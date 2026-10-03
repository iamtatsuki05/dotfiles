"""Internal scoped GitHub Copilot CLI ACP lifecycle boundary.

The pinned platform binary is itself the ACP server, so no in-process hook can
be injected.  This module owns only the fixed server argv, the closed process
environment, and the private ``COPILOT_HOME``.  It never runs Copilot, logs in,
links or copies credentials, or selects another provider; the shared Node
client decides every permission request from the frozen write policy.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

from .native_acp_dependencies import CopilotAcpExecutables, NativeAcpDependencyError
from .runtime import RuntimeValidationError
from .scoped_acp import (
    COPILOT_SCOPED_ADAPTER_ID,
    SCOPED_CLIENT,
    SCOPED_POLICY,
    checked_digest,
    policy_payload,
)
from .task_spec import TaskSpec

ADAPTER_ID = COPILOT_SCOPED_ADAPTER_ID
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_POLICY_NAME = "write-policy.json"
_HOME_NAME = "copilot-home"
_SETTINGS_NAME = "settings.json"
_TMP_NAME = "tmp"
_EXPECTED_ROOT_ENTRIES = frozenset({_POLICY_NAME, _HOME_NAME, _TMP_NAME})
_COMMON_FLAGS: tuple[str, ...] = (
    "--no-auto-update",
    "--no-custom-instructions",
    "--disable-builtin-mcps",
    "--no-remote",
    "--no-remote-export",
    "--disallow-temp-dir",
    "--no-ask-user",
)
_PERMISSION_FLAGS: Mapping[str, tuple[str, ...]] = {
    "read-only": (
        "--available-tools",
        "view,grep,glob",
        "--deny-tool",
        "shell",
        "--deny-tool",
        "write",
        "--deny-tool",
        "url",
    ),
    "workspace-write": (
        "--available-tools",
        "view,grep,glob,edit,create",
        "--deny-tool",
        "shell",
        "--deny-tool",
        "url",
    ),
}
_PASSED_ENVIRONMENT = frozenset({"USER", "LOGNAME", "LANG"})
_NORMAL_COPILOT_STATE = (".copilot", ".config/github-copilot", ".config/gh")


def _fail(message: str) -> NoReturn:
    raise RuntimeValidationError(message)


def _canonical(path: Path, label: str, *, directory: bool = False) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        _fail(f"Copilot ACP {label} path is invalid")
    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except (OSError, RuntimeError, ValueError):
        _fail(f"Copilot ACP {label} is unavailable")
    if stat.S_ISLNK(info.st_mode):
        _fail(f"Copilot ACP {label} must not be a symlink")
    if resolved != path:
        _fail(f"Copilot ACP {label} path is not canonical")
    if directory and not stat.S_ISDIR(info.st_mode):
        _fail(f"Copilot ACP {label} is not a directory")
    if not directory and not stat.S_ISREG(info.st_mode):
        _fail(f"Copilot ACP {label} is not a regular file")
    return path


def _digest(path: Path) -> str:
    try:
        return checked_digest(path, private=True)
    except RuntimeValidationError:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail("Copilot ACP private artifact cannot be fingerprinted")


def _private_root(path: Path, *, empty: bool) -> Path:
    root = _canonical(path, "private root", directory=True)
    try:
        info = root.lstat()
        entries = tuple(root.iterdir())
    except OSError:
        _fail("Copilot ACP private root is unavailable")
    if (
        info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or (empty and entries)
    ):
        _fail("Copilot ACP private root is not a new 0700 directory")
    return root


def _private_directory(path: Path) -> tuple[str, ...]:
    _canonical(path, "private directory", directory=True)
    try:
        info = path.lstat()
        entries = tuple(sorted(entry.name for entry in path.iterdir()))
    except OSError:
        _fail("Copilot ACP private directory is unavailable")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        _fail("Copilot ACP private directory is unsafe")
    return entries


def _mkdir_private(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    except OSError:
        _fail("Copilot ACP private directory could not be created")
    _private_directory(path)


def _write_new(path: Path, contents: bytes) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(contents)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(path, 0o600)
    except (OSError, ValueError):
        _fail("Copilot ACP private artifact could not be written")


def _validated_flags(
    permission: object, model: object, effort: object
) -> tuple[str, ...]:
    flags = _PERMISSION_FLAGS.get(permission) if isinstance(permission, str) else None
    if flags is None:
        _fail("Copilot ACP permission is invalid")
    # The model and effort become argv values, so these patterns also keep a
    # value from being parsed as another Copilot flag.
    if not isinstance(model, str) or model == "auto" or _MODEL.fullmatch(model) is None:
        _fail("Copilot ACP model is invalid")
    if not isinstance(effort, str) or effort not in EFFORTS:
        _fail("Copilot ACP effort is invalid")
    return flags


def validate_role_options(permission: object, model: object, effort: object) -> None:
    """Reject a role whose permission, model, or effort cannot form the argv."""

    _validated_flags(permission, model, effort)


def agent_argv(
    executables: CopilotAcpExecutables,
    *,
    permission: str,
    model: str,
    effort: str,
) -> list[str]:
    """Return the only Copilot server argv used by the scoped client."""

    if not isinstance(executables, CopilotAcpExecutables):
        _fail("Copilot ACP executable binding is invalid")
    flags = _validated_flags(permission, model, effort)
    return [
        str(executables.copilot),
        "--acp",
        "--stdio",
        "--model",
        model,
        "--effort",
        effort,
        *_COMMON_FLAGS,
        *flags,
    ]


def agent_command(
    executables: CopilotAcpExecutables,
    *,
    permission: str,
    model: str,
    effort: str,
) -> str:
    return shlex.join(
        agent_argv(executables, permission=permission, model=model, effort=effort)
    )


def environment(private_root: Path) -> dict[str, str]:
    """Return the closed environment shared by the Node client and Copilot."""

    root = _private_root(private_root, empty=False)
    home = Path.home()
    if not home.is_absolute():
        _fail("Copilot ACP home directory is invalid")
    value = {
        key: item
        for key, item in os.environ.items()
        if key in _PASSED_ENVIRONMENT or key.startswith("LC_")
    }
    value.update(
        {
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(root / _TMP_NAME),
            "COPILOT_HOME": str(root / _HOME_NAME),
        }
    )
    return value


def _append_protected(protected: list[str], path: Path) -> None:
    try:
        text = str(path.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        _fail("Copilot ACP protected path is unavailable")
    if text not in protected:
        protected.append(text)


def _policy(
    private_root: Path,
    workspace: Path,
    state_path: Path,
    task: TaskSpec | None,
    permission: str,
    executables: CopilotAcpExecutables,
) -> dict[str, object]:
    try:
        value = policy_payload(
            private_root,
            workspace,
            state_path,
            task,
            executables.loader,
            permission=permission,
        )
    except (RuntimeValidationError, OSError, ValueError):
        _fail("Copilot ACP write policy could not be built")
    protected = value.get("protected_paths")
    if not isinstance(protected, list) or any(
        not isinstance(item, str) for item in protected
    ):
        _fail("Copilot ACP write policy is invalid")
    for path in (
        executables.node,
        executables.loader,
        executables.copilot,
        executables.sdk,
        executables.loader.parent,
        executables.copilot.parent,
        executables.sdk.parent.parent,
    ):
        _append_protected(protected, path)
    home = Path.home()
    for relative in _NORMAL_COPILOT_STATE:
        source = home / relative
        if source.exists():
            _append_protected(protected, source)
    return value


def _settings(private_root: Path, workspace: Path, state_path: Path) -> bytes:
    denied = (private_root, workspace / ".git", state_path.parent)
    value = {
        "disableAllHooks": True,
        "remote": "off",
        "remoteExport": False,
        "sandbox": {
            "enabled": True,
            "allowBypass": False,
            "auth": {"git": False, "gh": False},
            "userPolicy": {
                "deniedPaths": [
                    item for path in denied for item in (str(path), str(path / "**"))
                ],
                "network": {"allowOutbound": False, "allowLocalNetwork": False},
                "seatbelt": {"keychainAccess": False},
            },
        },
    }
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def prepare_assignment(
    private_root: Path,
    workspace: Path,
    state_path: Path,
    task: TaskSpec | None,
    permission: str,
    executables: CopilotAcpExecutables,
    model: str,
    effort: str,
) -> dict[str, object]:
    """Prepare an assignment; the native caller owns root and failure cleanup."""

    # Every check precedes the first artifact in the caller-created root.
    agent_argv(executables, permission=permission, model=model, effort=effort)
    if not isinstance(task, (TaskSpec, type(None))):
        _fail("Copilot ACP task specification is invalid")
    try:
        executables.verify()
    except NativeAcpDependencyError:
        _fail("Copilot ACP executable binding changed")
    root = _private_root(private_root, empty=True)
    workspace = _canonical(workspace, "workspace", directory=True)
    state_path = (
        _canonical(state_path.parent, "state directory", directory=True)
        / state_path.name
    )
    policy_value = _policy(root, workspace, state_path, task, permission, executables)
    try:
        policy_bytes = json.dumps(
            policy_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail("Copilot ACP write policy is not serializable")

    home = root / _HOME_NAME
    for directory in (home, root / _TMP_NAME):
        _mkdir_private(directory)
    _write_new(home / _SETTINGS_NAME, _settings(root, workspace, state_path))
    policy_path = root / _POLICY_NAME
    _write_new(policy_path, policy_bytes)
    return {
        "write_policy_path": str(policy_path),
        "write_policy_sha256": _digest(policy_path),
    }


def _task_from_assignment(assignment: Mapping[str, object]) -> TaskSpec | None:
    raw = assignment.get("task_spec")
    if raw is None:
        return None
    try:
        return TaskSpec.from_dict(raw)
    except (TypeError, ValueError):
        _fail("Copilot ACP assignment TaskSpec is invalid")


def validate_assignment(
    state: Mapping[str, object],
    assignment: Mapping[str, object],
    spec: Mapping[str, object],
) -> None:
    """Revalidate private artifacts and immutable bindings before the client spawn.

    The caller validates the native role profile and the saved TaskSpec and
    dispatch identity through the common lifecycle.
    """

    if (
        not isinstance(state, Mapping)
        or not isinstance(assignment, Mapping)
        or not isinstance(spec, Mapping)
    ):
        _fail("Copilot ACP assignment boundary is invalid")
    if assignment.get("adapter_id") != ADAPTER_ID or spec.get("adapter_id") != (
        ADAPTER_ID
    ):
        _fail("Copilot ACP assignment adapter does not match")
    if "question_socket" in assignment:
        _fail("Copilot ACP assignment must not carry a question socket")
    raw_workspace = state.get("workspace")
    raw_state_path = state.get("state_path")
    if not isinstance(raw_workspace, str) or not isinstance(raw_state_path, str):
        _fail("Copilot ACP state binding is missing")
    workspace = _canonical(Path(raw_workspace), "workspace", directory=True)
    saved_state_path = Path(raw_state_path)
    if not saved_state_path.is_absolute():
        _fail("Copilot ACP state path is invalid")
    state_path = (
        _canonical(saved_state_path.parent, "state directory", directory=True)
        / saved_state_path.name
    )
    if checked_digest(SCOPED_CLIENT) != spec.get("scoped_client_sha256"):
        _fail("Copilot ACP client changed since team start")
    if checked_digest(SCOPED_POLICY) != spec.get("scoped_policy_sha256"):
        _fail("Copilot ACP shared policy changed since team start")
    try:
        executables = CopilotAcpExecutables.from_dict(spec.get("acp_executables"))
        executables.verify()
    except (NativeAcpDependencyError, TypeError, ValueError):
        _fail("Copilot ACP executable binding changed")
    permission = spec.get("permission")
    model = spec.get("model")
    effort = spec.get("effort")
    if not all(isinstance(value, str) for value in (permission, model, effort)):
        _fail("Copilot ACP role spec is incomplete")
    expected_command = agent_command(
        executables,
        permission=str(permission),
        model=str(model),
        effort=str(effort),
    )

    raw_root = assignment.get("provider_private_root")
    if not isinstance(raw_root, str):
        _fail("Copilot ACP private root binding is missing")
    root = _private_root(Path(raw_root), empty=False)
    if raw_root != str(root):
        _fail("Copilot ACP private root path is not canonical")
    try:
        entries = frozenset(path.name for path in root.iterdir())
    except OSError:
        _fail("Copilot ACP private root is unavailable")
    if entries != _EXPECTED_ROOT_ENTRIES:
        _fail("Copilot ACP private root contains unexpected artifacts")
    policy_path = root / _POLICY_NAME
    if assignment.get("write_policy_path") != str(policy_path):
        _fail("Copilot ACP assignment write_policy_path is invalid")
    if _private_directory(root / _HOME_NAME) != (_SETTINGS_NAME,):
        _fail("Copilot ACP private home contains unexpected artifacts")
    if _private_directory(root / _TMP_NAME):
        _fail("Copilot ACP private temporary directory is not empty")

    settings_path = root / _HOME_NAME / _SETTINGS_NAME
    _digest(settings_path)
    try:
        settings = settings_path.read_bytes()
    except OSError:
        _fail("Copilot ACP private settings are unavailable")
    if settings != _settings(root, workspace, state_path):
        _fail("Copilot ACP private settings changed")
    if assignment.get("write_policy_sha256") != _digest(policy_path):
        _fail("Copilot ACP write policy changed")
    expected_policy = _policy(
        root,
        workspace,
        state_path,
        _task_from_assignment(assignment),
        str(permission),
        executables,
    )
    try:
        actual_policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        _fail("Copilot ACP write policy is invalid")
    if actual_policy != expected_policy:
        _fail("Copilot ACP write policy does not match the saved TaskSpec")
    if assignment.get("agent_command") != expected_command:
        _fail("Copilot ACP agent command changed")
