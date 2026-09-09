"""Ownership checked driver for one private detached Zellij session.

Zellij's JSON pane listing does not expose operating-system PIDs.  The
driver therefore joins the listing with exact process argv/parent/cwd
observations before publishing a receipt.  Pane output and ``done``-like
strings are intentionally outside this module's lifecycle contract.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Literal

from .native_terminal import TerminalPresence
from .process_identity import python_process_argv, read_process_argv

ZELLIJ_CONTRACT_VERSION: Final = "contract_version_1"
BUILTIN_PLUGIN_URL: Final = "zellij:link"
_COMMAND_TIMEOUT_SECONDS: Final = 10.0
_STARTUP_TIMEOUT_SECONDS: Final = 10.0
_POLL_SECONDS: Final = 0.05
_MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
_MAX_SOCKET_BYTES: Final = 103
_IDENTIFIER_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_ROOT_RE: Final = re.compile(r"at-[A-Za-z0-9][A-Za-z0-9_.-]{0,47}\Z")
_UUID_RE: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_SESSION_LINE_RE: Final = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]{0,79}) "
    r"\[Created [0-9]+(?:s|m|h|d) ago\](?: \(EXITED\))?\s*\Z"
)
_ENV_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PATH_IDENTITY_FIELDS: Final = frozenset({"device", "inode", "mode", "uid"})
_ROOT_ENTRIES: Final = frozenset(
    {
        "cache",
        "config-dir",
        "config.kdl",
        "data-dir",
        "home",
        "runtime",
        "s",
        "state",
        "tmp",
    }
)
PaneKind = Literal["terminal", "plugin"]


class ZellijError(RuntimeError):
    """Base class for deterministic Zellij-driver failures."""


class ZellijUnavailableError(ZellijError):
    """The selected Zellij executable is absent or cannot be executed."""


class ZellijValidationError(ZellijError):
    """A driver input, receipt, or Zellij response is invalid."""


class ZellijOwnershipError(ZellijError):
    """An operation would address a resource not proven to be ours."""


class CloseEvidence(str, Enum):
    """Evidence returned by :meth:`ZellijDriver.close`."""

    SERVER_TERMINATED = "server-terminated"
    SESSION_TERMINATED = "session-terminated"
    OWNERSHIP_UNPROVEN = "ownership-unproven"
    TERMINATION_UNPROVEN = "termination-unproven"


@dataclass(frozen=True)
class _PathIdentity:
    device: int
    inode: int
    mode: int
    uid: int


@dataclass(frozen=True)
class ZellijPaneMetadata:
    """The identity-bearing subset of one ``list-panes --all --json`` row."""

    pane_kind: PaneKind
    pane_id: int
    tab_id: int
    tab_name: str
    title: str
    is_plugin: bool
    is_suppressed: bool
    plugin_url: str | None
    exited: bool
    is_held: bool
    exit_status: int | None
    terminal_command: str | None
    pane_cwd: Path | None

    def as_dict(self) -> dict[str, object]:
        return {
            "pane_kind": self.pane_kind,
            "pane_id": self.pane_id,
            "tab_id": self.tab_id,
            "tab_name": self.tab_name,
            "title": self.title,
            "is_plugin": self.is_plugin,
            "is_suppressed": self.is_suppressed,
            "plugin_url": self.plugin_url,
            "exited": self.exited,
            "is_held": self.is_held,
            "exit_status": self.exit_status,
            "terminal_command": self.terminal_command,
            "pane_cwd": None if self.pane_cwd is None else str(self.pane_cwd),
        }


@dataclass(frozen=True)
class ZellijReceipt:
    """Immutable identity for one driver-created Zellij terminal pane."""

    executable: Path
    private_root: Path
    socket_path: Path
    config_path: Path
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    tmp_dir: Path
    run_nonce: str
    session_name: str
    tab_id: int
    pane_id: int
    tab_kind: str
    pane_kind: PaneKind
    pane_pid: int
    pane_pgid: int
    server_pid: int
    server_pgid: int
    server_ppid: int
    supervisor_ppid: int
    server_argv: tuple[str, ...]
    supervisor_argv: tuple[str, ...]
    cwd: Path
    pane_command_argv: tuple[str, ...]
    initial_pane: Mapping[str, object]
    initial_plugins: tuple[Mapping[str, object], ...]
    socket_identity: _PathIdentity
    config_identity: _PathIdentity
    private_root_identity: _PathIdentity
    owned_paths: tuple[tuple[str, _PathIdentity], ...]

    @property
    def supervisor_pid(self) -> int:
        """The terminal pane process is the native Main supervisor."""

        return self.pane_pid

    @property
    def supervisor_pgid(self) -> int:
        return self.pane_pgid

    @property
    def server_process_group_id(self) -> int:
        """Return the captured server PGID without treating it as a PID."""

        return self.server_pgid

    def as_dict(self) -> dict[str, object]:
        values: dict[str, object] = {
            "executable": self.executable,
            "private_root": self.private_root,
            "socket_path": self.socket_path,
            "config_path": self.config_path,
            "config_dir": self.config_dir,
            "data_dir": self.data_dir,
            "cache_dir": self.cache_dir,
            "tmp_dir": self.tmp_dir,
            "run_nonce": self.run_nonce,
            "session_name": self.session_name,
            "tab_id": self.tab_id,
            "pane_id": self.pane_id,
            "tab_kind": self.tab_kind,
            "pane_kind": self.pane_kind,
            "pane_pid": self.pane_pid,
            "pane_pgid": self.pane_pgid,
            "server_pid": self.server_pid,
            "server_pgid": self.server_pgid,
            "server_ppid": self.server_ppid,
            "supervisor_ppid": self.supervisor_ppid,
            "server_argv": list(self.server_argv),
            "supervisor_argv": list(self.supervisor_argv),
            "cwd": self.cwd,
            "pane_command_argv": list(self.pane_command_argv),
            "initial_pane": _validate_initial_pane(self.initial_pane),
            "initial_plugins": [
                dict(item) for item in _validate_initial_plugins(self.initial_plugins)
            ],
            "socket_identity": _path_identity_as_dict(
                self.socket_identity, "socket_identity"
            ),
            "config_identity": _path_identity_as_dict(
                self.config_identity, "config_identity"
            ),
            "private_root_identity": _path_identity_as_dict(
                self.private_root_identity, "private_root_identity"
            ),
            "owned_paths": [
                {
                    "relative": relative,
                    "identity": _path_identity_as_dict(identity, "owned path"),
                }
                for relative, identity in self.owned_paths
            ],
        }
        for name, value in values.items():
            if name.endswith("_identity") or name in {
                "initial_pane",
                "initial_plugins",
            }:
                continue
            if name in {
                "pane_id",
                "tab_id",
                "pane_pid",
                "pane_pgid",
                "server_pid",
                "server_pgid",
                "server_ppid",
                "supervisor_ppid",
            }:
                _nonnegative_pid(value, f"receipt {name}")
            elif name in {"run_nonce", "session_name", "tab_kind"}:
                _identifier(value, f"receipt {name}")
            elif name == "pane_kind":
                _pane_kind(value, "receipt pane_kind")
            elif name in {"server_argv", "supervisor_argv", "pane_command_argv"}:
                _argv(value, f"receipt {name}")
            elif name in {
                "executable",
                "private_root",
                "socket_path",
                "config_path",
                "config_dir",
                "data_dir",
                "cache_dir",
                "tmp_dir",
                "cwd",
            }:
                _receipt_path_object(value, f"receipt {name}")
        _validate_initial_pane(self.initial_pane)
        _validate_initial_plugins(self.initial_plugins)
        _validate_receipt_paths(self)
        return {
            name: str(value) if isinstance(value, Path) else value
            for name, value in values.items()
        }

    @classmethod
    def from_dict(cls, data: object) -> ZellijReceipt:
        if not isinstance(data, Mapping):
            raise ZellijValidationError("Zellij receipt must be an object")
        expected = {
            "executable",
            "private_root",
            "socket_path",
            "config_path",
            "config_dir",
            "data_dir",
            "cache_dir",
            "tmp_dir",
            "run_nonce",
            "session_name",
            "tab_id",
            "pane_id",
            "tab_kind",
            "pane_kind",
            "pane_pid",
            "pane_pgid",
            "server_pid",
            "server_pgid",
            "server_ppid",
            "supervisor_ppid",
            "server_argv",
            "supervisor_argv",
            "cwd",
            "pane_command_argv",
            "initial_pane",
            "initial_plugins",
            "socket_identity",
            "config_identity",
            "private_root_identity",
            "owned_paths",
        }
        if set(data) != expected:
            raise ZellijValidationError(
                "Zellij receipt has unsupported or missing fields"
            )
        result = cls(
            executable=_receipt_path(data["executable"], "executable"),
            private_root=_private_root_path(data["private_root"]),
            socket_path=_receipt_path(data["socket_path"], "socket_path"),
            config_path=_receipt_path(data["config_path"], "config_path"),
            config_dir=_receipt_path(data["config_dir"], "config_dir"),
            data_dir=_receipt_path(data["data_dir"], "data_dir"),
            cache_dir=_receipt_path(data["cache_dir"], "cache_dir"),
            tmp_dir=_receipt_path(data["tmp_dir"], "tmp_dir"),
            run_nonce=_identifier(data["run_nonce"], "receipt run nonce"),
            session_name=_identifier(data["session_name"], "receipt session name"),
            tab_id=_nonnegative_pid(data["tab_id"], "receipt tab ID"),
            pane_id=_nonnegative_pid(data["pane_id"], "receipt pane ID"),
            tab_kind=_identifier(data["tab_kind"], "receipt tab kind"),
            pane_kind=_pane_kind(data["pane_kind"], "receipt pane kind"),
            pane_pid=_pid(data["pane_pid"], "receipt pane PID"),
            pane_pgid=_pid(data["pane_pgid"], "receipt pane PGID"),
            server_pid=_pid(data["server_pid"], "receipt server PID"),
            server_pgid=_pid(data["server_pgid"], "receipt server PGID"),
            server_ppid=_pid(data["server_ppid"], "receipt server PPID"),
            supervisor_ppid=_pid(data["supervisor_ppid"], "receipt supervisor PPID"),
            server_argv=_argv(data["server_argv"], "receipt server argv"),
            supervisor_argv=_argv(data["supervisor_argv"], "receipt supervisor argv"),
            cwd=_receipt_path(data["cwd"], "receipt cwd"),
            pane_command_argv=_argv(
                data["pane_command_argv"], "receipt pane command argv"
            ),
            initial_pane=_validate_initial_pane(data["initial_pane"]),
            initial_plugins=_validate_initial_plugins(data["initial_plugins"]),
            socket_identity=_path_identity_from_dict(
                data["socket_identity"], "socket_identity"
            ),
            config_identity=_path_identity_from_dict(
                data["config_identity"], "config_identity"
            ),
            private_root_identity=_path_identity_from_dict(
                data["private_root_identity"], "private_root_identity"
            ),
            owned_paths=_owned_paths(data["owned_paths"]),
        )
        _validate_receipt_paths(result)
        return result


@dataclass(frozen=True)
class ZellijInspection:
    """A point-in-time pane observation with explicit presence confidence."""

    presence: TerminalPresence
    running: bool | None
    exit_status: int | None
    identity_verified: bool
    pane_present: bool
    session_present: bool | None
    pane_pid: int | None
    server_pid: int | None
    observed_nonce: str | None
    reason: str | None = None
    pane_pgid: int | None = None


@dataclass(frozen=True)
class ZellijCloseResult:
    """Evidence about an owned Zellij session close operation."""

    evidence: CloseEvidence
    session_terminated: bool
    server_terminated: bool
    socket_removed: bool
    ownership_verified: bool
    descendants_stopped: bool = False
    exit_status: int | None = None
    reason: str | None = None


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ZellijValidationError(f"{context} must be a non-empty string")
    if "\x00" in value:
        raise ZellijValidationError(f"{context} must not contain NUL")
    return value


def _identifier(value: object, context: str) -> str:
    text = _text(value, context)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise ZellijValidationError(f"{context} contains unsupported characters")
    return text


def _pane_kind(value: object, context: str) -> PaneKind:
    if value not in {"terminal", "plugin"}:
        raise ZellijValidationError(f"{context} is invalid")
    return value


def _receipt_path(value: object, context: str) -> Path:
    text = _text(value, context)
    path = Path(text)
    if not path.is_absolute():
        raise ZellijValidationError(f"{context} must be absolute")
    return path


def _receipt_path_object(value: object, context: str) -> Path:
    if not isinstance(value, Path):
        raise ZellijValidationError(f"{context} must be a Path")
    return _receipt_path(str(value), context)


def _private_root_path(value: object) -> Path:
    if isinstance(value, Path):
        value = str(value)
    path = _receipt_path(value, "private_root")
    if path.parent != Path("/tmp") or _ROOT_RE.fullmatch(path.name) is None:
        raise ZellijValidationError("private_root must be a short /tmp/at-* path")
    if len(os.fsencode(str(path))) > 80:
        raise ZellijValidationError("private_root is too long")
    return path


def _pid(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ZellijValidationError(f"{context} must be a positive integer")
    return value


def _nonnegative_pid(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ZellijValidationError(f"{context} must be a non-negative integer")
    return value


def _mode(value: object, context: str) -> int:
    result = _nonnegative_pid(value, context)
    if result > 0o7777:
        raise ZellijValidationError(f"{context} is invalid")
    return result


def _argv(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not value:
        raise ZellijValidationError(f"{context} must be a non-empty argv")
    return tuple(_text(item, f"{context}[{index}]") for index, item in enumerate(value))


def _path_identity_from_dict(value: object, context: str) -> _PathIdentity:
    if not isinstance(value, Mapping) or set(value) != _PATH_IDENTITY_FIELDS:
        raise ZellijValidationError(f"{context} is invalid")
    fields: list[int] = []
    for name in ("device", "inode", "mode", "uid"):
        raw = value[name]
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise ZellijValidationError(f"{context}.{name} is invalid")
        fields.append(raw)
    if fields[2] > 0o7777:
        raise ZellijValidationError(f"{context}.mode is invalid")
    return _PathIdentity(*fields)


def _path_identity_as_dict(value: object, context: str) -> dict[str, int]:
    if not isinstance(value, _PathIdentity):
        raise ZellijValidationError(f"{context} is invalid")
    return {
        "device": _nonnegative_pid(value.device, f"{context}.device"),
        "inode": _nonnegative_pid(value.inode, f"{context}.inode"),
        "mode": _mode(value.mode, f"{context}.mode"),
        "uid": _nonnegative_pid(value.uid, f"{context}.uid"),
    }


def _validate_initial_pane(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ZellijValidationError("receipt initial_pane must be an object")
    expected = {
        "pane_kind",
        "pane_id",
        "tab_id",
        "tab_name",
        "title",
        "is_plugin",
        "is_suppressed",
        "plugin_url",
        "exited",
        "is_held",
        "exit_status",
        "terminal_command",
        "pane_cwd",
    }
    if set(value) != expected:
        raise ZellijValidationError("receipt initial_pane has unsupported fields")
    pane_kind = _pane_kind(value["pane_kind"], "initial_pane pane_kind")
    pane_id = _nonnegative_pid(value["pane_id"], "initial_pane pane_id")
    tab_id = _nonnegative_pid(value["tab_id"], "initial_pane tab_id")
    tab_name = _text(value["tab_name"], "initial_pane tab_name")
    title = _text(value["title"], "initial_pane title")
    if not isinstance(value["is_plugin"], bool):
        raise ZellijValidationError("initial_pane is_plugin is invalid")
    if not isinstance(value["is_suppressed"], bool):
        raise ZellijValidationError("initial_pane is_suppressed is invalid")
    plugin_url = value["plugin_url"]
    if plugin_url is not None:
        plugin_url = _text(plugin_url, "initial_pane plugin_url")
    for name in ("exited", "is_held"):
        if not isinstance(value[name], bool):
            raise ZellijValidationError(f"initial_pane {name} is invalid")
    exit_status = value["exit_status"]
    if exit_status is not None:
        exit_status = _nonnegative_pid(exit_status, "initial_pane exit_status")
    terminal_command = value["terminal_command"]
    if terminal_command is not None:
        terminal_command = _text(terminal_command, "initial_pane terminal_command")
    pane_cwd = value["pane_cwd"]
    if pane_cwd is not None:
        pane_cwd = str(_receipt_path(pane_cwd, "initial_pane pane_cwd"))
    return {
        "pane_kind": pane_kind,
        "pane_id": pane_id,
        "tab_id": tab_id,
        "tab_name": tab_name,
        "title": title,
        "is_plugin": value["is_plugin"],
        "is_suppressed": value["is_suppressed"],
        "plugin_url": plugin_url,
        "exited": value["exited"],
        "is_held": value["is_held"],
        "exit_status": exit_status,
        "terminal_command": terminal_command,
        "pane_cwd": pane_cwd,
    }


def _validate_initial_plugins(
    value: object,
) -> tuple[dict[str, object], ...]:
    if not isinstance(value, (tuple, list)) or len(value) != 1:
        raise ZellijValidationError(
            "receipt initial_plugins must contain exactly one built-in plugin"
        )
    result: list[dict[str, object]] = []
    for index, item in enumerate(value):
        plugin = _validate_initial_pane(item)
        if (
            plugin["pane_kind"] != "plugin"
            or plugin["is_plugin"] is not True
            or plugin["is_suppressed"] is not True
            or plugin["plugin_url"] != BUILTIN_PLUGIN_URL
        ):
            raise ZellijValidationError(
                f"receipt initial_plugins[{index}] is not the built-in link plugin"
            )
        result.append(plugin)
    return tuple(result)


def _owned_paths(value: object) -> tuple[tuple[str, _PathIdentity], ...]:
    if not isinstance(value, list):
        raise ZellijValidationError("receipt owned_paths must be a list")
    result: list[tuple[str, _PathIdentity]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {"relative", "identity"}:
            raise ZellijValidationError(f"receipt owned_paths[{index}] is invalid")
        relative = _text(item["relative"], f"owned_paths[{index}].relative")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ZellijValidationError("receipt owned path escapes private root")
        if relative in seen:
            raise ZellijValidationError("receipt owned_paths contains a duplicate")
        seen.add(relative)
        result.append(
            (
                relative,
                _path_identity_from_dict(item["identity"], "owned path identity"),
            )
        )
    return tuple(result)


def _validate_receipt_paths(receipt: ZellijReceipt) -> None:
    root = receipt.private_root
    expected = {
        "config_path": root / "config.kdl",
        "config_dir": root / "config-dir",
        "data_dir": root / "data-dir",
        "cache_dir": root / "cache",
        "tmp_dir": root / "tmp",
        "socket_path": root / "s" / ZELLIJ_CONTRACT_VERSION / receipt.session_name,
    }
    for name, expected_path in expected.items():
        if getattr(receipt, name) != expected_path:
            raise ZellijValidationError(f"receipt {name} does not match private root")
    if receipt.server_argv != (
        str(receipt.executable),
        "--server",
        str(receipt.socket_path),
    ):
        raise ZellijValidationError("receipt server argv does not match socket")
    if receipt.tab_kind != "tab" or receipt.pane_kind != "terminal":
        raise ZellijValidationError("receipt pane/tab kind is unsupported")
    required_owned = {
        "config.kdl",
        "config-dir",
        "data-dir",
        "cache",
        "tmp",
        "home",
        "runtime",
        "state",
        "s",
        f"s/{ZELLIJ_CONTRACT_VERSION}",
        f"s/{ZELLIJ_CONTRACT_VERSION}/{receipt.session_name}",
    }
    owned = {relative for relative, _identity in receipt.owned_paths}
    if not required_owned.issubset(owned) or any(
        not _owned_relative_allowed(relative, receipt.session_name)
        or (
            relative not in required_owned
            and not _managed_generated_relative(relative, receipt.session_name)
        )
        for relative in owned
    ):
        raise ZellijValidationError(
            "receipt owned_paths contain an unregistered private path"
        )
    initial = _validate_initial_pane(receipt.initial_pane)
    if initial["pane_kind"] != "terminal" or initial["pane_id"] != receipt.pane_id:
        raise ZellijValidationError("receipt initial pane identity is inconsistent")
    if initial["tab_id"] != receipt.tab_id:
        raise ZellijValidationError("receipt initial tab identity is inconsistent")
    plugins = _validate_initial_plugins(receipt.initial_plugins)
    if any(plugin["tab_id"] != receipt.tab_id for plugin in plugins):
        raise ZellijValidationError(
            "receipt built-in plugin tab identity is inconsistent"
        )


def _owned_relative_allowed(relative: str, session_name: str) -> bool:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return False
    fixed = {
        "config.kdl",
        "config-dir",
        "data-dir",
        "cache",
        "tmp",
        "home",
        "runtime",
        "state",
        "s",
        f"s/{ZELLIJ_CONTRACT_VERSION}",
        f"s/{ZELLIJ_CONTRACT_VERSION}/{session_name}",
    }
    if relative in fixed:
        return True
    return path.parts[0] in {
        "config-dir",
        "data-dir",
        "cache",
        "tmp",
        "home",
        "runtime",
        "state",
    }


def _managed_generated_relative(relative: str, session_name: str) -> bool:
    """Allow only Zellij 0.44's known generated cache/log paths."""

    parts = Path(relative).parts
    if relative in {"home/Library", "home/Library/Caches"}:
        return True
    cache_prefix = (
        "home",
        "Library",
        "Caches",
        "org.Zellij-Contributors.Zellij",
    )
    if parts[: len(cache_prefix)] != cache_prefix:
        return (
            (
                len(parts) == 2
                and parts[0] == "tmp"
                and re.fullmatch(r"zellij-[0-9]+", parts[1]) is not None
            )
            or (
                len(parts) == 3
                and parts[0] == "tmp"
                and re.fullmatch(r"zellij-[0-9]+", parts[1]) is not None
                and parts[2] == "zellij-log"
            )
            or (
                len(parts) == 4
                and parts[0] == "tmp"
                and re.fullmatch(r"zellij-[0-9]+", parts[1]) is not None
                and parts[2:] == ("zellij-log", "zellij.log")
            )
        )
    suffix = parts[len(cache_prefix) :]
    if suffix in {
        (),
        ("zellij:link",),
        ("zellij:link", "plugin_cache"),
        ("contract_version_1",),
        ("contract_version_1", "session_info"),
        ("contract_version_1", "session_info", session_name),
    }:
        return True
    if len(suffix) == 1 and _UUID_RE.fullmatch(suffix[0]) is not None:
        return True
    return (
        (
            len(suffix) == 2
            and _UUID_RE.fullmatch(suffix[0]) is not None
            and suffix[1] == "zellij:link"
        )
        or (
            len(suffix) == 3
            and _UUID_RE.fullmatch(suffix[0]) is not None
            and suffix[1] == "zellij:link"
            and re.fullmatch(r"[0-9]+-[0-9]+", suffix[2]) is not None
        )
        or (
            suffix
            == (
                "contract_version_1",
                "session_info",
                session_name,
                "session-metadata.kdl",
            )
        )
    )


def _safe_path_identity(
    path: Path,
    *,
    require_socket: bool = False,
    require_private: bool = True,
) -> _PathIdentity:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ZellijValidationError(f"Zellij path is unavailable: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ZellijValidationError(f"Zellij path must not be a symlink: {path}")
    if require_socket:
        if not stat.S_ISSOCK(info.st_mode):
            raise ZellijValidationError(f"Zellij path is not a socket: {path}")
    elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ZellijValidationError(f"Zellij path is a special file: {path}")
    if info.st_uid != os.geteuid():
        raise ZellijOwnershipError(f"Zellij path is not owned by this user: {path}")
    if require_private and stat.S_IMODE(info.st_mode) & 0o077:
        raise ZellijOwnershipError(f"Zellij path is not private: {path}")
    return _PathIdentity(
        device=info.st_dev,
        inode=info.st_ino,
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
    )


def _same_path_identity(
    path: Path,
    identity: _PathIdentity,
    *,
    require_socket: bool = False,
    require_private: bool = True,
) -> bool:
    try:
        return (
            _safe_path_identity(
                path,
                require_socket=require_socket,
                require_private=require_private,
            )
            == identity
        )
    except ZellijError:
        return False


def _pid_state(pid: int) -> bool | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return None
    if sys.platform.startswith("linux"):
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            state = text[text.rfind(")") + 2]
        except (OSError, UnicodeError, IndexError):
            return None
        return state != "Z"
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                check=False,
                timeout=2.0,
                env={"PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        state = result.stdout.strip()
        if not state:
            return None
        return not state.startswith("Z")
    return True


def _read_process_ppid(pid: int) -> int | None:
    if sys.platform.startswith("linux"):
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = text[text.rfind(")") + 2 :].split()
            value = int(fields[1])
            return value if value > 0 else None
        except (OSError, UnicodeError, ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "ppid="],
                capture_output=True,
                text=True,
                check=False,
                timeout=2.0,
                env={"PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        try:
            value = int(result.stdout.strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    return None


def _read_process_cwd(pid: int) -> Path | None:
    if sys.platform.startswith("linux"):
        try:
            return Path(os.readlink(f"/proc/{pid}/cwd"))
        except (OSError, ValueError):
            return None
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2.0,
                env={"PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in result.stdout.splitlines():
            if line.startswith("n") and len(line) > 1:
                return Path(line[1:])
    return None


def _process_ids() -> tuple[int, ...]:
    if sys.platform.startswith("linux"):
        result: list[int] = []
        try:
            entries = os.listdir("/proc")
        except OSError:
            return ()
        for entry in entries:
            if entry.isdigit() and int(entry) > 0:
                result.append(int(entry))
        return tuple(result)
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
            env={"PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    values: list[int] = []
    for line in result.stdout.splitlines():
        try:
            value = int(line.strip())
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return tuple(values)


def _child_process_ids(parent_pid: int) -> tuple[int, ...]:
    """Read one process table snapshot before doing exact argv probes."""

    if sys.platform.startswith("linux"):
        result: list[int] = []
        for pid in _process_ids():
            if _read_process_ppid(pid) == parent_pid:
                result.append(pid)
        return tuple(result)
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
            env={"PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    children: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, ppid = (int(value) for value in fields)
        except ValueError:
            continue
        if pid > 0 and ppid == parent_pid:
            children.append(pid)
    return tuple(children)


def _same_cwd(observed: Path | None, expected: Path) -> bool:
    if observed is None:
        return False
    try:
        return observed.resolve(strict=False) == expected.resolve(strict=False)
    except OSError:
        return observed == expected


def _kdl_quote(value: str) -> str:
    if "\x00" in value:
        raise ZellijValidationError("KDL value must not contain NUL")
    return json.dumps(value, ensure_ascii=False)


def _parse_pane(value: object) -> ZellijPaneMetadata:
    if not isinstance(value, Mapping):
        raise ZellijValidationError("Zellij pane row must be an object")
    required = {
        "id",
        "is_plugin",
        "is_suppressed",
        "title",
        "exited",
        "exit_status",
        "is_held",
        "terminal_command",
        "plugin_url",
        "tab_id",
        "tab_name",
    }
    if not required.issubset(value):
        raise ZellijValidationError("Zellij pane row is missing identity fields")
    pane_id = _nonnegative_pid(value["id"], "Zellij pane ID")
    tab_id = _nonnegative_pid(value["tab_id"], "Zellij tab ID")
    if not isinstance(value["is_plugin"], bool):
        raise ZellijValidationError("Zellij pane plugin flag is invalid")
    is_plugin = value["is_plugin"]
    is_suppressed = value["is_suppressed"]
    if not isinstance(is_suppressed, bool):
        raise ZellijValidationError("Zellij pane suppression flag is invalid")
    title = _text(value["title"], "Zellij pane title")
    tab_name = _text(value["tab_name"], "Zellij tab name")
    exited = value["exited"]
    is_held = value["is_held"]
    if not isinstance(exited, bool) or not isinstance(is_held, bool):
        raise ZellijValidationError("Zellij pane state is invalid")
    exit_status = value["exit_status"]
    if exit_status is not None:
        exit_status = _nonnegative_pid(exit_status, "Zellij pane exit status")
    terminal_command = value["terminal_command"]
    if terminal_command is not None:
        terminal_command = _text(terminal_command, "Zellij terminal command")
    plugin_url = value["plugin_url"]
    if plugin_url is not None:
        plugin_url = _text(plugin_url, "Zellij plugin URL")
    cwd_value = value.get("pane_cwd")
    pane_cwd: Path | None
    if cwd_value is None:
        pane_cwd = None
    else:
        pane_cwd = _receipt_path(cwd_value, "Zellij pane cwd")
    if is_plugin:
        if plugin_url != BUILTIN_PLUGIN_URL or not is_suppressed:
            raise ZellijValidationError(
                "Zellij session contains an unknown or unsuppressed plugin"
            )
        kind: PaneKind = "plugin"
    else:
        if plugin_url is not None or is_suppressed:
            raise ZellijValidationError("Zellij terminal pane has plugin metadata")
        kind = "terminal"
    return ZellijPaneMetadata(
        pane_kind=kind,
        pane_id=pane_id,
        tab_id=tab_id,
        tab_name=tab_name,
        title=title,
        is_plugin=is_plugin,
        is_suppressed=is_suppressed,
        plugin_url=plugin_url,
        exited=exited,
        is_held=is_held,
        exit_status=exit_status,
        terminal_command=terminal_command,
        pane_cwd=pane_cwd,
    )


def _parse_panes(
    value: object,
) -> tuple[ZellijPaneMetadata, tuple[ZellijPaneMetadata, ...]]:
    if not isinstance(value, list):
        raise ZellijValidationError("Zellij pane response must be a list")
    terminals: list[ZellijPaneMetadata] = []
    plugins: list[ZellijPaneMetadata] = []
    for row in value:
        pane = _parse_pane(row)
        (plugins if pane.is_plugin else terminals).append(pane)
    if len(terminals) != 1:
        raise ZellijValidationError("Zellij session must contain one terminal pane")
    if len(plugins) != 1:
        raise ZellijValidationError(
            "Zellij session must contain exactly one built-in link plugin"
        )
    return terminals[0], tuple(plugins)


def _parse_session_listing(value: str) -> set[str] | None:
    names: set[str] = set()
    for line in value.splitlines():
        if not line.strip():
            continue
        match = _SESSION_LINE_RE.fullmatch(line)
        if match is None:
            return None
        names.add(match.group("name"))
    return names if names else None


class ZellijDriver:
    """Create and manage one private, nonce-tagged Zellij session."""

    def __init__(
        self,
        executable: str | Path,
        socket_root: Path,
        run_nonce: str,
        session_name: str,
    ) -> None:
        self._executable = self._resolve_executable(executable)
        self._private_root = _private_root_path(socket_root)
        if not self._private_root.exists() or self._private_root.is_symlink():
            raise ZellijValidationError("Zellij private root must already exist")
        self._private_root_identity = _safe_path_identity(self._private_root)
        self._run_nonce = _identifier(run_nonce, "run nonce")
        self._session_name = _identifier(session_name, "session name")
        self._config_path = self._private_root / "config.kdl"
        self._config_dir = self._private_root / "config-dir"
        self._data_dir = self._private_root / "data-dir"
        self._cache_dir = self._private_root / "cache"
        self._tmp_dir = self._private_root / "tmp"
        self._home_dir = self._private_root / "home"
        self._runtime_dir = self._private_root / "runtime"
        self._state_dir = self._private_root / "state"
        self._socket_path = (
            self._private_root / "s" / ZELLIJ_CONTRACT_VERSION / self._session_name
        )
        if len(os.fsencode(str(self._socket_path))) > _MAX_SOCKET_BYTES:
            raise ZellijValidationError("Zellij socket path is too long")
        self._env_executable = self._resolve_env_executable()
        self._receipt: ZellijReceipt | None = None
        self._config_identity: _PathIdentity | None = None

    @classmethod
    def from_receipt(cls, receipt: ZellijReceipt) -> ZellijDriver:
        if not isinstance(receipt, ZellijReceipt):
            raise ZellijValidationError("Zellij receipt has an invalid type")
        restored = ZellijReceipt.from_dict(receipt.as_dict())
        driver = object.__new__(cls)
        driver._executable = cls._resolve_executable(restored.executable)
        if driver._executable != restored.executable:
            raise ZellijOwnershipError("Zellij executable path is not canonical")
        driver._private_root = _private_root_path(restored.private_root)
        driver._run_nonce = restored.run_nonce
        driver._session_name = restored.session_name
        driver._config_path = restored.config_path
        driver._config_dir = restored.config_dir
        driver._data_dir = restored.data_dir
        driver._cache_dir = restored.cache_dir
        driver._tmp_dir = restored.tmp_dir
        driver._home_dir = driver._private_root / "home"
        driver._runtime_dir = driver._private_root / "runtime"
        driver._state_dir = driver._private_root / "state"
        driver._socket_path = restored.socket_path
        driver._env_executable = driver._resolve_env_executable()
        driver._private_root_identity = restored.private_root_identity
        driver._config_identity = restored.config_identity
        driver._receipt = restored
        if not _same_path_identity(
            driver._private_root, restored.private_root_identity
        ):
            raise ZellijOwnershipError("Zellij private-root identity changed")
        inspected = driver.inspect(restored)
        if not inspected.identity_verified:
            raise ZellijOwnershipError(
                inspected.reason or "Zellij pane ownership could not be verified"
            )
        return driver

    @staticmethod
    def _resolve_executable(value: str | Path) -> Path:
        raw = _text(os.fspath(value), "Zellij executable")
        candidate = Path(raw)
        if not candidate.is_absolute() and "/" not in raw:
            selected = shutil.which(raw)
            if selected is None:
                raise ZellijUnavailableError(
                    f"selected Zellij executable is unavailable: {raw}"
                )
            candidate = Path(selected)
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ZellijUnavailableError(
                f"selected Zellij executable is unavailable: {raw}"
            ) from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ZellijUnavailableError(
                f"selected Zellij executable is not executable: {resolved}"
            )
        return resolved

    @staticmethod
    def _resolve_env_executable() -> Path:
        selected = shutil.which("env")
        if selected is None:
            raise ZellijUnavailableError("the env utility is unavailable")
        try:
            resolved = Path(selected).resolve(strict=True)
        except OSError as exc:
            raise ZellijUnavailableError("the env utility is unavailable") from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ZellijUnavailableError("the env utility is not executable")
        return resolved

    @property
    def executable(self) -> Path:
        return self._executable

    @property
    def private_root(self) -> Path:
        return self._private_root

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def run_nonce(self) -> str:
        return self._run_nonce

    @property
    def session_name(self) -> str:
        return self._session_name

    def _preflight(self, cwd: str | Path) -> Path:
        if not _same_path_identity(self._private_root, self._private_root_identity):
            raise ZellijOwnershipError("Zellij private-root identity changed")
        try:
            entries = tuple(self._private_root.iterdir())
        except OSError as exc:
            raise ZellijValidationError(
                "Zellij private root cannot be inspected"
            ) from exc
        if entries:
            raise ZellijOwnershipError("Zellij private root contains unknown entries")
        for path in (self._config_path, self._socket_path):
            if path.exists() or path.is_symlink():
                raise ZellijOwnershipError(f"Zellij path already exists: {path}")
        working_directory = Path(cwd)
        if not working_directory.is_absolute() or not working_directory.is_dir():
            raise ZellijValidationError("Zellij working directory is invalid")
        return working_directory

    def _prepare_private_tree(self) -> None:
        paths = (
            self._home_dir,
            self._config_dir,
            self._cache_dir,
            self._data_dir,
            self._runtime_dir,
            self._state_dir,
            self._tmp_dir,
            self._private_root / "s",
            self._private_root / "s" / ZELLIJ_CONTRACT_VERSION,
        )
        for path in paths:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise ZellijOwnershipError(
                    f"Zellij private directory already exists: {path}"
                ) from exc
            _safe_path_identity(path)
        encoded = (
            b"show_release_notes false\n"
            b"show_startup_tips false\n"
            b"session_serialization false\n"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd: int | None = None
        try:
            fd = os.open(self._config_path, flags, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self._config_identity = _safe_path_identity(self._config_path)
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            raise ZellijError("Zellij private config could not be created") from exc

    def _private_env(self) -> dict[str, str]:
        return {
            "HOME": str(self._home_dir),
            "PATH": f"{self._executable.parent}:/usr/bin:/bin",
            "SHELL": "/bin/sh",
            "TERM": "xterm-256color",
            "LANG": "C",
            "TMPDIR": str(self._tmp_dir),
            "XDG_CONFIG_HOME": str(self._config_dir),
            "XDG_CACHE_HOME": str(self._cache_dir),
            "XDG_DATA_HOME": str(self._data_dir),
            "XDG_STATE_HOME": str(self._state_dir),
            "XDG_RUNTIME_DIR": str(self._runtime_dir),
            "ZELLIJ_CONFIG_FILE": str(self._config_path),
            "ZELLIJ_CONFIG_DIR": str(self._config_dir),
            "ZELLIJ_SOCKET_DIR": str(self._private_root / "s"),
        }

    @staticmethod
    def _validate_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(argv, tuple) or not argv:
            raise ZellijValidationError("Zellij argv must be a non-empty tuple")
        return _argv(argv, "argv")

    @staticmethod
    def _validate_env(env: Mapping[str, str]) -> tuple[str, ...]:
        if not isinstance(env, Mapping):
            raise ZellijValidationError("Zellij environment must be a mapping")
        values: list[tuple[str, str]] = []
        for raw_name, raw_value in env.items():
            name = _text(raw_name, "environment name")
            if _ENV_NAME_RE.fullmatch(name) is None:
                raise ZellijValidationError(f"environment name is invalid: {name}")
            if not isinstance(raw_value, str) or "\x00" in raw_value:
                raise ZellijValidationError(f"environment value is invalid: {name}")
            values.append((name, raw_value))
        return tuple(f"{name}={value}" for name, value in sorted(values))

    def _layout_string(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        title: str,
    ) -> str:
        assignments = self._validate_env(env)
        pane_args = ("-i", *assignments, *argv)
        args = (
            "        args " + " ".join(_kdl_quote(value) for value in pane_args) + ";"
        )
        return (
            "layout {\n"
            f"    pane command={_kdl_quote(str(self._env_executable))} "
            f"name={_kdl_quote(title)} cwd={_kdl_quote(str(cwd))} "
            "close_on_exit=false {\n"
            f"{args}\n"
            "    }\n"
            "}\n"
        )

    def _base_argv(self) -> list[str]:
        return [
            str(self._executable),
            "--config",
            str(self._config_path),
            "--config-dir",
            str(self._config_dir),
            "--data-dir",
            str(self._data_dir),
        ]

    def _private_env_argv(self) -> tuple[str, ...]:
        return tuple(
            [str(self._env_executable), "-i"]
            + [f"{name}={value}" for name, value in sorted(self._private_env().items())]
        )

    def _query_argv(self) -> tuple[str, ...]:
        return tuple(
            self._base_argv()
            + [
                "--session",
                self._session_name,
                "action",
                "list-panes",
                "--all",
                "--json",
            ]
        )

    def _kill_argv(self) -> tuple[str, ...]:
        return tuple(self._base_argv() + ["kill-session", self._session_name])

    def _list_sessions_argv(self) -> tuple[str, ...]:
        return tuple(self._base_argv() + ["list-sessions", "--no-formatting"])

    def _run(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=str(self._private_root),
                env=self._private_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
            stdout, stderr = process.communicate(timeout=_COMMAND_TIMEOUT_SECONDS)
        except FileNotFoundError as exc:
            raise ZellijUnavailableError(
                "selected Zellij executable disappeared"
            ) from exc
        except PermissionError as exc:
            raise ZellijUnavailableError(
                "selected Zellij executable cannot be executed"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise ZellijError("Zellij command timed out") from exc
        except OSError as exc:
            raise ZellijUnavailableError(
                "selected Zellij executable failed to start"
            ) from exc
        stdout_text = self._decode_response(stdout)
        stderr_text = self._decode_response(stderr)
        return subprocess.CompletedProcess(
            list(argv), process.returncode, stdout_text, stderr_text
        )

    @staticmethod
    def _decode_response(value: bytes | str) -> str:
        if isinstance(value, bytes):
            if len(value) > _MAX_RESPONSE_BYTES:
                raise ZellijError("Zellij response is too large")
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ZellijError("Zellij response is not UTF-8") from exc
        if not isinstance(value, str):
            raise ZellijError("Zellij response has invalid type")
        if len(value.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise ZellijError("Zellij response is too large")
        return value

    @staticmethod
    def _require_success(
        result: subprocess.CompletedProcess[str], context: str
    ) -> None:
        if result.returncode == 0:
            return
        detail = result.stderr.strip()
        if detail:
            raise ZellijError(f"{context} failed: {detail}")
        raise ZellijError(f"{context} failed with exit status {result.returncode}")

    def _list_panes(self) -> tuple[ZellijPaneMetadata, tuple[ZellijPaneMetadata, ...]]:
        result = self._run(self._query_argv())
        self._require_success(result, "Zellij list-panes")
        try:
            value = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ZellijError("Zellij list-panes returned invalid JSON") from exc
        return _parse_panes(value)

    def _find_server(self, server_argv: tuple[str, ...]) -> int | None:
        matches = [
            pid for pid in _process_ids() if read_process_argv(pid) == server_argv
        ]
        if len(matches) > 1:
            raise ZellijOwnershipError(
                "multiple Zellij servers match the private socket"
            )
        return matches[0] if matches else None

    def _find_supervisor(
        self,
        server_pid: int,
        supervisor_argv: tuple[str, ...],
        cwd: Path,
    ) -> tuple[int, int] | None:
        matches: list[tuple[int, int]] = []
        for pid in _child_process_ids(server_pid):
            if read_process_argv(pid) != supervisor_argv:
                continue
            if not _same_cwd(_read_process_cwd(pid), cwd):
                continue
            try:
                pgid = os.getpgid(pid)
            except OSError:
                continue
            matches.append((pid, pgid))
        if len(matches) > 1:
            raise ZellijOwnershipError("multiple supervisors match the Zellij pane")
        return matches[0] if matches else None

    def _wait_for_startup(
        self,
        supervisor_argv: tuple[str, ...],
        cwd: Path,
    ) -> tuple[
        int, int, int, int, Path, ZellijPaneMetadata, tuple[ZellijPaneMetadata, ...]
    ]:
        server_argv = (
            str(self._executable),
            "--server",
            str(self._socket_path),
        )
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            server_pid = self._find_server(server_argv)
            if server_pid is None:
                time.sleep(_POLL_SECONDS)
                continue
            server_ppid = _read_process_ppid(server_pid)
            server_cwd = _read_process_cwd(server_pid)
            if server_ppid is None or not _same_cwd(server_cwd, self._private_root):
                time.sleep(_POLL_SECONDS)
                continue
            try:
                server_pgid = os.getpgid(server_pid)
            except OSError:
                time.sleep(_POLL_SECONDS)
                continue
            if not self._socket_path.exists():
                time.sleep(_POLL_SECONDS)
                continue
            try:
                terminal, plugins = self._list_panes()
            except ZellijError:
                # A session can be visible in process listings a little before
                # its first client has finished applying the layout.
                time.sleep(_POLL_SECONDS)
                continue
            supervisor = self._find_supervisor(server_pid, supervisor_argv, cwd)
            if supervisor is None:
                time.sleep(_POLL_SECONDS)
                continue
            return (
                server_pid,
                server_pgid,
                server_ppid,
                supervisor[0],
                server_cwd or self._private_root,
                terminal,
                plugins,
            )
        raise ZellijError("Zellij session did not become ready")

    def _capture_owned_paths(self) -> tuple[tuple[str, _PathIdentity], ...]:
        previous: dict[str, _PathIdentity] | None = None
        for _attempt in range(5):
            inventory = self._current_inventory(include_socket=True)
            if inventory is None:
                raise ZellijOwnershipError(
                    "Zellij private inventory could not be captured"
                )
            if inventory == previous:
                return tuple(sorted(inventory.items()))
            previous = inventory
            time.sleep(_POLL_SECONDS * 2)
        return tuple(sorted(previous.items())) if previous is not None else ()

    def _current_inventory(
        self, *, include_socket: bool
    ) -> dict[str, _PathIdentity] | None:
        try:
            names = {item.name for item in self._private_root.iterdir()}
        except OSError:
            return None
        if not names.issubset(_ROOT_ENTRIES):
            return None
        result: dict[str, _PathIdentity] = {}

        def add(path: Path, *, socket: bool = False) -> bool:
            relative = path.relative_to(self._private_root).as_posix()
            try:
                result[relative] = _safe_path_identity(
                    path, require_socket=socket, require_private=False
                )
            except ZellijError:
                return False
            return True

        def walk(path: Path) -> bool:
            if not add(path):
                return False
            try:
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    entries = tuple(path.iterdir())
                elif stat.S_ISREG(info.st_mode):
                    entries = ()
                else:
                    return False
            except OSError:
                return False
            for entry in entries:
                if not walk(entry):
                    return False
            return True

        if not add(self._config_path):
            return None
        for directory in (
            self._config_dir,
            self._data_dir,
            self._cache_dir,
            self._tmp_dir,
            self._home_dir,
            self._runtime_dir,
            self._state_dir,
        ):
            if not walk(directory):
                return None
        socket_dir = self._private_root / "s"
        contract_dir = socket_dir / ZELLIJ_CONTRACT_VERSION
        if not add(socket_dir) or not add(contract_dir):
            return None
        try:
            socket_entries = tuple(contract_dir.iterdir())
        except OSError:
            return None
        if include_socket:
            if len(socket_entries) != 1 or socket_entries[0] != self._socket_path:
                return None
            if not add(self._socket_path, socket=True):
                return None
        elif socket_entries:
            return None
        return result

    def _receipt_is_for_driver(self, receipt: ZellijReceipt) -> bool:
        if not isinstance(receipt, ZellijReceipt):
            return False
        try:
            _validate_receipt_paths(receipt)
        except ZellijError:
            return False
        return (
            receipt.executable == self._executable
            and receipt.private_root == self._private_root
            and receipt.socket_path == self._socket_path
            and receipt.config_path == self._config_path
            and receipt.config_dir == self._config_dir
            and receipt.data_dir == self._data_dir
            and receipt.cache_dir == self._cache_dir
            and receipt.tmp_dir == self._tmp_dir
            and receipt.run_nonce == self._run_nonce
            and receipt.session_name == self._session_name
        )

    def _unknown(
        self,
        reason: str,
        *,
        pane_present: bool = False,
        session_present: bool | None = None,
        pane_pid: int | None = None,
        server_pid: int | None = None,
    ) -> ZellijInspection:
        return ZellijInspection(
            presence="unknown",
            running=None,
            exit_status=None,
            identity_verified=False,
            pane_present=pane_present,
            session_present=session_present,
            pane_pid=pane_pid,
            server_pid=server_pid,
            observed_nonce=None,
            reason=reason,
        )

    def _server_verified(self, receipt: ZellijReceipt) -> ZellijInspection | None:
        if not self._receipt_is_for_driver(receipt):
            return self._unknown("Zellij receipt is not owned by this driver")
        if not _same_path_identity(self._private_root, receipt.private_root_identity):
            return self._unknown("Zellij private-root identity changed")
        if not _same_path_identity(receipt.config_path, receipt.config_identity):
            return self._unknown("Zellij config identity changed")
        if not _same_path_identity(
            receipt.socket_path, receipt.socket_identity, require_socket=True
        ):
            return self._unknown("Zellij socket identity changed")
        alive = _pid_state(receipt.server_pid)
        if alive is not True:
            return self._unknown(
                "Zellij server liveness is unproven",
                session_present=alive is False,
                server_pid=receipt.server_pid,
            )
        if read_process_argv(receipt.server_pid) != receipt.server_argv:
            return self._unknown(
                "Zellij server argv identity changed",
                session_present=True,
                server_pid=receipt.server_pid,
            )
        if _read_process_ppid(receipt.server_pid) != receipt.server_ppid:
            return self._unknown(
                "Zellij server PPID changed",
                session_present=True,
                server_pid=receipt.server_pid,
            )
        try:
            if os.getpgid(receipt.server_pid) != receipt.server_pgid:
                return self._unknown(
                    "Zellij server PGID changed",
                    session_present=True,
                    server_pid=receipt.server_pid,
                )
        except OSError:
            return self._unknown(
                "Zellij server PGID is unavailable",
                session_present=True,
                server_pid=receipt.server_pid,
            )
        if not _same_cwd(_read_process_cwd(receipt.server_pid), self._private_root):
            return self._unknown(
                "Zellij server cwd changed",
                session_present=True,
                server_pid=receipt.server_pid,
            )
        return None

    def inspect(self, receipt: ZellijReceipt) -> ZellijInspection:
        """Observe JSON pane metadata without interpreting screen output."""

        server_failure = self._server_verified(receipt)
        if server_failure is not None:
            return server_failure
        try:
            terminal, plugins = self._list_panes()
        except (ZellijError, OSError, TypeError, ValueError) as exc:
            return self._unknown(
                f"Zellij pane observation is unproven: {type(exc).__name__}",
                session_present=True,
                server_pid=receipt.server_pid,
            )
        initial = _validate_initial_pane(receipt.initial_pane)
        initial_plugins = _validate_initial_plugins(receipt.initial_plugins)
        if len(plugins) != len(initial_plugins):
            return self._unknown(
                "Zellij built-in plugin count changed",
                pane_present=True,
                session_present=True,
                pane_pid=receipt.pane_pid,
                server_pid=receipt.server_pid,
            )
        for observed_plugin, saved_plugin in zip(plugins, initial_plugins):
            if (
                observed_plugin.pane_id != saved_plugin["pane_id"]
                or observed_plugin.tab_id != saved_plugin["tab_id"]
                or observed_plugin.tab_name != saved_plugin["tab_name"]
                or observed_plugin.title != saved_plugin["title"]
                or observed_plugin.plugin_url != saved_plugin["plugin_url"]
                or observed_plugin.is_suppressed != saved_plugin["is_suppressed"]
            ):
                return self._unknown(
                    "Zellij built-in plugin identity changed",
                    pane_present=True,
                    session_present=True,
                    pane_pid=receipt.pane_pid,
                    server_pid=receipt.server_pid,
                )
        if (
            terminal.pane_kind != "terminal"
            or terminal.pane_id != receipt.pane_id
            or terminal.tab_id != receipt.tab_id
            or terminal.tab_name != initial["tab_name"]
            or terminal.title != initial["title"]
            or terminal.terminal_command != initial["terminal_command"]
            or not _pane_cwd_matches(terminal.pane_cwd, initial, receipt.cwd)
        ):
            return self._unknown(
                "Zellij pane/tab identity changed",
                pane_present=True,
                session_present=True,
                pane_pid=receipt.pane_pid,
                server_pid=receipt.server_pid,
            )
        supervisor_state = _pid_state(receipt.pane_pid)
        if terminal.exited or terminal.is_held:
            if not terminal.is_held:
                return self._unknown(
                    "Zellij terminal exited without a held pane",
                    pane_present=True,
                    session_present=True,
                    pane_pid=receipt.pane_pid,
                    server_pid=receipt.server_pid,
                )
            if supervisor_state is not False:
                return self._unknown(
                    "Zellij held pane supervisor PID is not proven dead",
                    pane_present=True,
                    session_present=True,
                    pane_pid=receipt.pane_pid,
                    server_pid=receipt.server_pid,
                )
            if read_process_argv(receipt.pane_pid) is not None:
                return self._unknown(
                    "Zellij held pane supervisor PID was reused",
                    pane_present=True,
                    session_present=True,
                    pane_pid=receipt.pane_pid,
                    server_pid=receipt.server_pid,
                )
            return ZellijInspection(
                presence="present",
                running=False,
                exit_status=terminal.exit_status,
                identity_verified=True,
                pane_present=True,
                session_present=True,
                pane_pid=receipt.pane_pid,
                server_pid=receipt.server_pid,
                observed_nonce=receipt.run_nonce,
                pane_pgid=receipt.pane_pgid,
            )
        if supervisor_state is not True:
            return self._unknown(
                "Zellij supervisor liveness is unproven",
                pane_present=True,
                session_present=True,
                pane_pid=receipt.pane_pid,
                server_pid=receipt.server_pid,
            )
        if read_process_argv(receipt.pane_pid) != receipt.supervisor_argv:
            return self._unknown(
                "Zellij supervisor argv identity changed",
                pane_present=True,
                session_present=True,
                pane_pid=receipt.pane_pid,
                server_pid=receipt.server_pid,
            )
        if _read_process_ppid(receipt.pane_pid) != receipt.supervisor_ppid:
            return self._unknown(
                "Zellij supervisor PPID changed",
                pane_present=True,
                session_present=True,
                pane_pid=receipt.pane_pid,
                server_pid=receipt.server_pid,
            )
        try:
            if os.getpgid(receipt.pane_pid) != receipt.pane_pgid:
                return self._unknown(
                    "Zellij supervisor PGID changed",
                    pane_present=True,
                    session_present=True,
                    pane_pid=receipt.pane_pid,
                    server_pid=receipt.server_pid,
                )
        except OSError:
            return self._unknown(
                "Zellij supervisor PGID is unavailable",
                pane_present=True,
                session_present=True,
                pane_pid=receipt.pane_pid,
                server_pid=receipt.server_pid,
            )
        if not _same_cwd(_read_process_cwd(receipt.pane_pid), receipt.cwd):
            return self._unknown(
                "Zellij supervisor cwd is unproven",
                pane_present=True,
                session_present=True,
                pane_pid=receipt.pane_pid,
                server_pid=receipt.server_pid,
            )
        return ZellijInspection(
            presence="present",
            running=True,
            exit_status=terminal.exit_status,
            identity_verified=True,
            pane_present=True,
            session_present=True,
            pane_pid=receipt.pane_pid,
            server_pid=receipt.server_pid,
            observed_nonce=receipt.run_nonce,
            pane_pgid=receipt.pane_pgid,
        )

    def create(
        self,
        argv: tuple[str, ...],
        cwd: str | Path,
        env: Mapping[str, str],
        title: str,
    ) -> ZellijReceipt:
        if self._receipt is not None:
            raise ZellijValidationError("this Zellij driver already created a pane")
        command_argv = self._validate_argv(argv)
        environment = self._validate_env(env)
        title = _text(title, "Zellij pane title")
        if len(title) > 256:
            raise ZellijValidationError("Zellij pane title is too long")
        working_directory = self._preflight(cwd)
        supervisor_argv = (
            python_process_argv(command_argv)
            if command_argv[0] == sys.executable
            else command_argv
        )
        self._prepare_private_tree()
        layout = self._layout_string(command_argv, working_directory, env, title)
        command = tuple(
            self._base_argv()
            + [
                "--layout-string",
                layout,
                "attach",
                "--create-background",
                self._session_name,
            ]
        )
        try:
            started = self._run(command)
            self._require_success(started, "Zellij attach --create-background")
            (
                server_pid,
                server_pgid,
                server_ppid,
                pane_pid,
                _server_cwd,
                terminal,
                _plugins,
            ) = self._wait_for_startup(supervisor_argv, working_directory)
            pane_pgid = os.getpgid(pane_pid)
            socket_identity = _safe_path_identity(
                self._socket_path, require_socket=True
            )
            config_identity = _safe_path_identity(self._config_path)
            initial_pane = terminal.as_dict()
            receipt = ZellijReceipt(
                executable=self._executable,
                private_root=self._private_root,
                socket_path=self._socket_path,
                config_path=self._config_path,
                config_dir=self._config_dir,
                data_dir=self._data_dir,
                cache_dir=self._cache_dir,
                tmp_dir=self._tmp_dir,
                run_nonce=self._run_nonce,
                session_name=self._session_name,
                tab_id=terminal.tab_id,
                pane_id=terminal.pane_id,
                tab_kind="tab",
                pane_kind="terminal",
                pane_pid=pane_pid,
                pane_pgid=pane_pgid,
                server_pid=server_pid,
                server_pgid=server_pgid,
                server_ppid=server_ppid,
                supervisor_ppid=server_pid,
                server_argv=(
                    str(self._executable),
                    "--server",
                    str(self._socket_path),
                ),
                supervisor_argv=supervisor_argv,
                cwd=working_directory,
                pane_command_argv=(
                    str(self._env_executable),
                    "-i",
                    *environment,
                    *command_argv,
                ),
                initial_pane=initial_pane,
                initial_plugins=tuple(plugin.as_dict() for plugin in _plugins),
                socket_identity=socket_identity,
                config_identity=config_identity,
                private_root_identity=self._private_root_identity,
                owned_paths=self._capture_owned_paths(),
            )
            self._receipt = receipt
            inspected = self.inspect(receipt)
            if not inspected.identity_verified:
                raise ZellijOwnershipError(
                    inspected.reason or "Zellij pane ownership could not be verified"
                )
            return receipt
        except BaseException:
            self._receipt = None
            self._cleanup_partial_session()
            raise

    def _cleanup_partial_session(self) -> None:
        server_argv = (
            str(self._executable),
            "--server",
            str(self._socket_path),
        )
        server_pid = self._find_server(server_argv)
        if server_pid is None:
            return
        if not _same_path_identity(self._private_root, self._private_root_identity):
            return
        if not self._socket_path.exists() or self._socket_path.is_symlink():
            return
        try:
            socket_identity = _safe_path_identity(
                self._socket_path, require_socket=True
            )
            config_identity = self._config_identity
            if config_identity is None or not _same_path_identity(
                self._config_path, config_identity
            ):
                return
        except ZellijError:
            return
        try:
            result = self._run(self._kill_argv())
        except ZellijError:
            return
        if result.returncode != 0:
            return
        deadline = time.monotonic() + _COMMAND_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _pid_state(server_pid) is False and not self._socket_path.exists():
                if _same_path_identity(
                    self._socket_path,
                    socket_identity,
                    require_socket=True,
                ):
                    return
                self._cleanup_unchecked_tree(config_identity)
                return
            time.sleep(_POLL_SECONDS)

    def _cleanup_unchecked_tree(self, config_identity: _PathIdentity) -> bool:
        # Partial creation has no receipt, so only remove the fixed private
        # config path when its owner/type is still exactly safe.  The Core
        # layer retains the root for an unknown partial effect.
        if not _same_path_identity(self._config_path, config_identity):
            return False
        try:
            self._config_path.unlink()
        except OSError:
            return False
        return True

    def _known_paths_owned(
        self, receipt: ZellijReceipt, *, require_socket: bool = True
    ) -> bool:
        if not _same_path_identity(self._private_root, receipt.private_root_identity):
            return False
        current = self._current_inventory(include_socket=require_socket)
        if current is None:
            return False
        expected = dict(receipt.owned_paths)
        if not require_socket:
            expected.pop(f"s/{ZELLIJ_CONTRACT_VERSION}/{receipt.session_name}", None)
        for relative, identity in current.items():
            if relative in expected:
                if expected[relative] != identity:
                    return False
            elif not _managed_generated_relative(relative, receipt.session_name):
                return False
        for relative in expected:
            if relative in current:
                continue
            if _managed_generated_relative(relative, receipt.session_name):
                continue
            return False
        return True

    @staticmethod
    def _owned_path_kind(
        path: Path, identity: _PathIdentity
    ) -> Literal["directory", "file"] | None:
        try:
            info = path.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
            return None
        if stat.S_ISDIR(info.st_mode):
            kind: Literal["directory", "file"] = "directory"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        else:
            return None
        observed = _PathIdentity(
            device=info.st_dev,
            inode=info.st_ino,
            mode=stat.S_IMODE(info.st_mode),
            uid=info.st_uid,
        )
        return kind if observed == identity else None

    def _cleanup_known_paths(self, receipt: ZellijReceipt) -> bool:
        if not self._known_paths_owned(receipt, require_socket=False):
            return False
        if not _same_path_identity(
            self._private_root, receipt.private_root_identity, require_private=False
        ):
            return False
        current = self._current_inventory(include_socket=False)
        if current is None:
            return False
        expected = {
            relative: identity
            for relative, identity in receipt.owned_paths
            if relative in current
        }
        expected.pop(f"s/{ZELLIJ_CONTRACT_VERSION}/{receipt.session_name}", None)
        expected.update(
            {
                relative: identity
                for relative, identity in current.items()
                if relative not in expected
            }
        )
        for relative, identity in sorted(
            expected.items(), key=lambda item: len(Path(item[0]).parts), reverse=True
        ):
            path = self._private_root / relative
            kind = self._owned_path_kind(path, identity)
            if kind is None:
                return False
            try:
                if kind == "directory":
                    path.rmdir()
                else:
                    path.unlink()
            except OSError:
                return False
        try:
            return not any(self._private_root.iterdir())
        except OSError:
            return False

    def _session_absent(self) -> bool | None:
        try:
            result = self._run(self._list_sessions_argv())
        except ZellijError:
            return None
        if result.returncode != 0:
            if result.stderr.strip() == "No active zellij sessions found.":
                return True
            return None
        names = _parse_session_listing(result.stdout)
        if names is None:
            return None
        return self._session_name not in names

    def _wait_terminated(self, receipt: ZellijReceipt) -> tuple[bool, bool, bool]:
        deadline = time.monotonic() + _COMMAND_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            server_terminated = _pid_state(receipt.server_pid) is False
            socket_removed = (
                not self._socket_path.exists() and not self._socket_path.is_symlink()
            )
            session_absent = self._session_absent()
            if server_terminated and socket_removed and session_absent:
                return True, True, True
            time.sleep(_POLL_SECONDS)
        server_terminated = _pid_state(receipt.server_pid) is False
        socket_removed = (
            not self._socket_path.exists() and not self._socket_path.is_symlink()
        )
        session_absent = self._session_absent()
        return (
            server_terminated and socket_removed and session_absent is True,
            server_terminated,
            socket_removed,
        )

    def attach_argv(self, receipt: ZellijReceipt) -> tuple[str, ...]:
        """Return an explicit session-scoped attach command.

        The one-shot ``env -i`` prefix carries the private socket environment
        because Zellij has no command-line socket-path option.  This keeps an
        attach invocation from falling back to a user's default session.
        """

        if not self._receipt_is_for_driver(receipt):
            raise ZellijOwnershipError("cannot attach to another Zellij receipt")
        inspected = self.inspect(receipt)
        if not inspected.identity_verified:
            raise ZellijOwnershipError(
                inspected.reason or "cannot attach without verified pane ownership"
            )
        return self._private_env_argv() + tuple(
            self._base_argv() + ["attach", self._session_name]
        )

    def close(self, receipt: ZellijReceipt) -> ZellijCloseResult:
        """Kill only the verified exact session and retain uncertain resources."""

        if not self._receipt_is_for_driver(receipt):
            return ZellijCloseResult(
                evidence=CloseEvidence.OWNERSHIP_UNPROVEN,
                session_terminated=False,
                server_terminated=False,
                socket_removed=False,
                ownership_verified=False,
                reason="Zellij receipt is not owned by this driver",
            )
        inspected = self.inspect(receipt)
        if not inspected.identity_verified:
            return ZellijCloseResult(
                evidence=CloseEvidence.OWNERSHIP_UNPROVEN,
                session_terminated=False,
                server_terminated=False,
                socket_removed=False,
                ownership_verified=False,
                reason=inspected.reason,
                exit_status=inspected.exit_status,
            )
        if not self._known_paths_owned(receipt):
            return ZellijCloseResult(
                evidence=CloseEvidence.OWNERSHIP_UNPROVEN,
                session_terminated=False,
                server_terminated=False,
                socket_removed=False,
                ownership_verified=False,
                reason="Zellij private resource identity is unproven",
                exit_status=inspected.exit_status,
            )
        try:
            killed = self._run(self._kill_argv())
        except ZellijError as exc:
            return ZellijCloseResult(
                evidence=CloseEvidence.TERMINATION_UNPROVEN,
                session_terminated=False,
                server_terminated=False,
                socket_removed=False,
                ownership_verified=True,
                reason=str(exc),
                exit_status=inspected.exit_status,
            )
        if killed.returncode != 0:
            reason = killed.stderr.strip() or "Zellij kill-session failed"
            return ZellijCloseResult(
                evidence=CloseEvidence.TERMINATION_UNPROVEN,
                session_terminated=False,
                server_terminated=False,
                socket_removed=False,
                ownership_verified=True,
                reason=reason,
                exit_status=inspected.exit_status,
            )
        session_terminated, server_terminated, socket_removed = self._wait_terminated(
            receipt
        )
        if not session_terminated:
            return ZellijCloseResult(
                evidence=CloseEvidence.TERMINATION_UNPROVEN,
                session_terminated=False,
                server_terminated=server_terminated,
                socket_removed=socket_removed,
                ownership_verified=True,
                reason="Zellij session/server/socket termination is unproven",
                exit_status=inspected.exit_status,
            )
        cleaned = self._cleanup_known_paths(receipt)
        return ZellijCloseResult(
            evidence=CloseEvidence.SERVER_TERMINATED,
            session_terminated=True,
            server_terminated=True,
            socket_removed=socket_removed,
            ownership_verified=True,
            reason=None if cleaned else "Zellij private-resource cleanup is unproven",
            exit_status=inspected.exit_status,
        )


def _pane_cwd_matches(
    observed: Path | None, initial: Mapping[str, object], expected: Path
) -> bool:
    if observed is None:
        # Zellij omits pane_cwd after a held command exits.  The saved
        # terminal command, pane/tab identity, and dead exact child PID are
        # the held-pane evidence in that state.
        return True
    return _same_cwd(observed, expected)


__all__ = (
    "BUILTIN_PLUGIN_URL",
    "ZELLIJ_CONTRACT_VERSION",
    "CloseEvidence",
    "ZellijCloseResult",
    "ZellijDriver",
    "ZellijError",
    "ZellijInspection",
    "ZellijOwnershipError",
    "ZellijPaneMetadata",
    "ZellijReceipt",
    "ZellijUnavailableError",
    "ZellijValidationError",
)
