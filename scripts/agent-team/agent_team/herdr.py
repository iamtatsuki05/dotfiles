"""Private Herdr terminal driver for the native team controller.

The driver owns one named, headless Herdr server and one workspace root pane.
It deliberately does not interpret pane output or Herdr agent status as team
completion.  The native controller remains responsible for Main and child
process cleanup receipts.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, cast

from .native_terminal import TerminalPresence
from .process_identity import python_process_argv, read_process_argv

HERDR_VERSION: Final = "0.8.2"
HERDR_PROTOCOL: Final = 20
_COMMAND_TIMEOUT_SECONDS: Final = 10.0
_STARTUP_TIMEOUT_SECONDS: Final = 10.0
_POLL_SECONDS: Final = 0.05
_MAX_SOCKET_BYTES: Final = 96
_MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
_IDENTIFIER_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_OPAQUE_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ENV_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PATH_IDENTITY_FIELDS: Final = frozenset({"device", "inode", "mode", "uid"})
_RESERVED_ENV_NAMES: Final = frozenset(
    {
        "HERDR_ENV",
        "HERDR_SESSION",
        "HERDR_SOCKET_PATH",
        "HERDR_CLIENT_SOCKET_PATH",
        "HERDR_CONFIG_PATH",
        "HERDR_WORKSPACE_ID",
        "HERDR_TAB_ID",
        "HERDR_PANE_ID",
    }
)


class HerdrError(RuntimeError):
    """Base class for deterministic Herdr-driver failures."""


class HerdrUnavailableError(HerdrError):
    """The selected Herdr executable cannot be used."""


class HerdrValidationError(HerdrError):
    """A driver input, receipt, or Herdr response is invalid."""


class HerdrOwnershipError(HerdrError):
    """The requested resource is not proven to belong to this driver."""


class HerdrApiError(HerdrError):
    """The Herdr API returned an error or an unusable response."""


class CloseEvidence(str, Enum):
    """Evidence returned by :meth:`HerdrDriver.close`."""

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
class HerdrReceipt:
    """Immutable identity for one driver-created Herdr workspace pane."""

    executable: Path
    private_root: Path
    config_path: Path
    socket_path: Path
    client_socket_path: Path
    session_dir: Path
    run_nonce: str
    session_name: str
    workspace_id: str
    tab_id: str
    pane_id: str
    terminal_id: str
    pane_pid: int
    pane_pgid: int
    server_pid: int
    server_pgid: int
    supervisor_ppid: int
    server_argv: tuple[str, ...]
    supervisor_argv: tuple[str, ...]
    server_cwd: Path
    cwd: Path
    version: str
    protocol: int
    config_identity: _PathIdentity
    socket_identity: _PathIdentity
    client_socket_identity: _PathIdentity
    private_root_identity: _PathIdentity
    session_dir_identity: _PathIdentity
    owned_paths: tuple[tuple[str, _PathIdentity], ...]

    def as_dict(self) -> dict[str, object]:
        values: dict[str, object] = {
            "executable": self.executable,
            "private_root": self.private_root,
            "config_path": self.config_path,
            "socket_path": self.socket_path,
            "client_socket_path": self.client_socket_path,
            "session_dir": self.session_dir,
            "run_nonce": self.run_nonce,
            "session_name": self.session_name,
            "workspace_id": self.workspace_id,
            "tab_id": self.tab_id,
            "pane_id": self.pane_id,
            "terminal_id": self.terminal_id,
            "pane_pid": self.pane_pid,
            "pane_pgid": self.pane_pgid,
            "server_pid": self.server_pid,
            "server_pgid": self.server_pgid,
            "supervisor_ppid": self.supervisor_ppid,
            "server_argv": list(self.server_argv),
            "supervisor_argv": list(self.supervisor_argv),
            "server_cwd": self.server_cwd,
            "cwd": self.cwd,
            "version": self.version,
            "protocol": self.protocol,
            "config_identity": _path_identity_as_dict(
                self.config_identity, "config_identity"
            ),
            "socket_identity": _path_identity_as_dict(
                self.socket_identity, "socket_identity"
            ),
            "client_socket_identity": _path_identity_as_dict(
                self.client_socket_identity, "client_socket_identity"
            ),
            "private_root_identity": _path_identity_as_dict(
                self.private_root_identity, "private_root_identity"
            ),
            "session_dir_identity": _path_identity_as_dict(
                self.session_dir_identity, "session_dir_identity"
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
            if name.endswith("_identity"):
                continue
            if name in {
                "pane_pid",
                "pane_pgid",
                "server_pid",
                "server_pgid",
                "supervisor_ppid",
            }:
                _pid(value, f"receipt {name}")
            elif name in {"run_nonce", "session_name"}:
                _identifier(value, f"receipt {name}")
            elif name in {
                "workspace_id",
                "tab_id",
                "pane_id",
                "terminal_id",
            }:
                _opaque_id(value, f"receipt {name}")
            elif name in {"server_argv", "supervisor_argv"}:
                _argv(tuple(value), f"receipt {name}")  # type: ignore[arg-type]
            elif name in {"version"}:
                _text(value, f"receipt {name}")
            elif name in {"protocol"}:
                if value != HERDR_PROTOCOL:
                    raise HerdrValidationError("receipt protocol is unsupported")
            elif name in {
                "executable",
                "private_root",
                "config_path",
                "socket_path",
                "client_socket_path",
                "session_dir",
                "server_cwd",
                "cwd",
            }:
                _receipt_path_object(value, f"receipt {name}")
        if self.version != HERDR_VERSION:
            raise HerdrValidationError("receipt Herdr version is unsupported")
        return {
            name: str(value) if isinstance(value, Path) else value
            for name, value in values.items()
        }

    @classmethod
    def from_dict(cls, data: object) -> HerdrReceipt:
        if not isinstance(data, Mapping):
            raise HerdrValidationError("Herdr receipt must be an object")
        expected = {
            "executable",
            "private_root",
            "config_path",
            "socket_path",
            "client_socket_path",
            "session_dir",
            "run_nonce",
            "session_name",
            "workspace_id",
            "tab_id",
            "pane_id",
            "terminal_id",
            "pane_pid",
            "pane_pgid",
            "server_pid",
            "server_pgid",
            "supervisor_ppid",
            "server_argv",
            "supervisor_argv",
            "server_cwd",
            "cwd",
            "version",
            "protocol",
            "config_identity",
            "socket_identity",
            "client_socket_identity",
            "private_root_identity",
            "session_dir_identity",
            "owned_paths",
        }
        if set(data) != expected:
            raise HerdrValidationError(
                "Herdr receipt has unsupported or missing fields"
            )
        executable = _receipt_path(data["executable"], "executable")
        private_root = _private_root_path(data["private_root"])
        config_path = _receipt_path(data["config_path"], "config_path")
        socket_path = _receipt_path(data["socket_path"], "socket_path")
        client_socket_path = _receipt_path(
            data["client_socket_path"], "client_socket_path"
        )
        session_dir = _receipt_path(data["session_dir"], "session_dir")
        cwd = _receipt_path(data["cwd"], "cwd")
        run_nonce = _identifier(data["run_nonce"], "receipt run nonce")
        session_name = _identifier(data["session_name"], "receipt session name")
        workspace_id = _opaque_id(data["workspace_id"], "receipt workspace ID")
        tab_id = _opaque_id(data["tab_id"], "receipt tab ID")
        pane_id = _opaque_id(data["pane_id"], "receipt pane ID")
        terminal_id = _opaque_id(data["terminal_id"], "receipt terminal ID")
        pane_pid = _pid(data["pane_pid"], "receipt pane PID")
        pane_pgid = _pid(data["pane_pgid"], "receipt pane PGID")
        server_pid = _pid(data["server_pid"], "receipt server PID")
        server_pgid = _pid(data["server_pgid"], "receipt server PGID")
        supervisor_ppid = _pid(data["supervisor_ppid"], "receipt supervisor PPID")
        server_argv = _argv(data["server_argv"], "receipt server argv")
        supervisor_argv = _argv(data["supervisor_argv"], "receipt supervisor argv")
        server_cwd = _receipt_path(data["server_cwd"], "server cwd")
        version = _text(data["version"], "receipt version")
        if version != HERDR_VERSION:
            raise HerdrValidationError("receipt Herdr version is unsupported")
        protocol = data["protocol"]
        if protocol != HERDR_PROTOCOL or isinstance(protocol, bool):
            raise HerdrValidationError("receipt Herdr protocol is unsupported")
        owned_paths = _owned_paths(data["owned_paths"], session_name)
        result = cls(
            executable=executable,
            private_root=private_root,
            config_path=config_path,
            socket_path=socket_path,
            client_socket_path=client_socket_path,
            session_dir=session_dir,
            run_nonce=run_nonce,
            session_name=session_name,
            workspace_id=workspace_id,
            tab_id=tab_id,
            pane_id=pane_id,
            terminal_id=terminal_id,
            pane_pid=pane_pid,
            pane_pgid=pane_pgid,
            server_pid=server_pid,
            server_pgid=server_pgid,
            supervisor_ppid=supervisor_ppid,
            server_argv=server_argv,
            supervisor_argv=supervisor_argv,
            server_cwd=server_cwd,
            cwd=cwd,
            version=version,
            protocol=protocol,
            config_identity=_path_identity_from_dict(
                data["config_identity"], "config_identity"
            ),
            socket_identity=_path_identity_from_dict(
                data["socket_identity"], "socket_identity"
            ),
            client_socket_identity=_path_identity_from_dict(
                data["client_socket_identity"], "client_socket_identity"
            ),
            private_root_identity=_path_identity_from_dict(
                data["private_root_identity"], "private_root_identity"
            ),
            session_dir_identity=_path_identity_from_dict(
                data["session_dir_identity"], "session_dir_identity"
            ),
            owned_paths=owned_paths,
        )
        _validate_receipt_paths(result)
        return result


@dataclass(frozen=True)
class HerdrInspection:
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
    snapshot_empty: bool | None = None
    pane_pgid: int | None = None

    @classmethod
    def unknown(
        cls,
        reason: str,
        *,
        pane_present: bool = False,
        session_present: bool | None = None,
        pane_pid: int | None = None,
        server_pid: int | None = None,
        observed_nonce: str | None = None,
    ) -> HerdrInspection:
        return cls(
            presence="unknown",
            running=None,
            exit_status=None,
            identity_verified=False,
            pane_present=pane_present,
            session_present=session_present,
            pane_pid=pane_pid,
            server_pid=server_pid,
            observed_nonce=observed_nonce,
            reason=reason,
        )


@dataclass(frozen=True)
class HerdrCloseResult:
    evidence: CloseEvidence
    session_terminated: bool
    server_terminated: bool
    socket_removed: bool
    ownership_verified: bool
    descendants_stopped: bool = False
    reason: str | None = None


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise HerdrValidationError(f"{context} must be a non-empty string")
    if "\x00" in value:
        raise HerdrValidationError(f"{context} must not contain NUL")
    return value


def _receipt_path(value: object, context: str) -> Path:
    text = _text(value, context)
    path = Path(text)
    if not path.is_absolute():
        raise HerdrValidationError(f"{context} must be absolute")
    return path


def _receipt_path_object(value: object, context: str) -> Path:
    if not isinstance(value, Path):
        raise HerdrValidationError(f"{context} must be a Path")
    return _receipt_path(str(value), context)


def _private_root_path(value: object) -> Path:
    if isinstance(value, Path):
        value = str(value)
    path = _receipt_path(value, "private_root")
    if path.parent != Path("/tmp") or not path.name.startswith("at-"):
        raise HerdrValidationError("private_root must be a short /tmp/at-* path")
    if len(str(path)) > 80:
        raise HerdrValidationError("private_root is too long")
    return path


def _identifier(value: object, context: str) -> str:
    text = _text(value, context)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise HerdrValidationError(f"{context} contains unsupported characters")
    return text


def _opaque_id(value: object, context: str) -> str:
    text = _text(value, context)
    if _OPAQUE_ID_RE.fullmatch(text) is None:
        raise HerdrValidationError(f"{context} is not a valid opaque ID")
    return text


def _pid(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise HerdrValidationError(f"{context} must be a positive integer")
    return value


def _argv(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not value:
        raise HerdrValidationError(f"{context} must be a non-empty argv")
    return tuple(_text(item, f"{context}[{index}]") for index, item in enumerate(value))


def _path_identity_from_dict(value: object, context: str) -> _PathIdentity:
    if not isinstance(value, Mapping) or set(value) != _PATH_IDENTITY_FIELDS:
        raise HerdrValidationError(f"{context} is invalid")
    fields: list[int] = []
    for name in ("device", "inode", "mode", "uid"):
        raw = value[name]
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise HerdrValidationError(f"{context}.{name} is invalid")
        fields.append(raw)
    if fields[2] > 0o777:
        raise HerdrValidationError(f"{context}.mode is invalid")
    return _PathIdentity(*fields)


def _path_identity_as_dict(value: object, context: str) -> dict[str, int]:
    if not isinstance(value, _PathIdentity):
        raise HerdrValidationError(f"{context} is invalid")
    return {
        "device": _nonnegative(value.device, f"{context}.device"),
        "inode": _nonnegative(value.inode, f"{context}.inode"),
        "mode": _mode(value.mode, f"{context}.mode"),
        "uid": _nonnegative(value.uid, f"{context}.uid"),
    }


def _nonnegative(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HerdrValidationError(f"{context} is invalid")
    return value


def _mode(value: object, context: str) -> int:
    result = _nonnegative(value, context)
    if result > 0o777:
        raise HerdrValidationError(f"{context} is invalid")
    return result


_SESSION_MANAGED_FILES: Final = frozenset(
    {
        "session.json",
        "session-history.json",
        "herdr-server.log",
        "herdr.log",
        "herdr-client.log",
        "herdr.sock",
        "herdr-client.sock",
    }
)


def _owned_relative_allowed(relative: str, session_name: str) -> bool:
    parts = Path(relative).parts
    if not parts or parts[0] not in {"h", "c", "s", "t"}:
        return False
    if parts[0] in {"s", "t"}:
        return len(parts) == 1
    if parts[0] == "h":
        if len(parts) == 1:
            return True
        return len(parts) == 2 and (
            parts[1] in {".bash_history", ".zsh_history", ".zcompdump"}
            or parts[1].startswith(".zcompdump-")
        )
    if parts == ("c",):
        return True
    if len(parts) < 2 or parts[1] != "herdr":
        return False
    if len(parts) == 2:
        return True
    if parts[2] in {"config.toml", ".plugins.lock"}:
        return len(parts) == 3
    if parts[2] != "sessions":
        return False
    if len(parts) == 3:
        return True
    if parts[3] != session_name:
        return False
    return len(parts) == 4 or (len(parts) == 5 and parts[4] in _SESSION_MANAGED_FILES)


def _relative_mutable(relative: str) -> bool:
    parts = Path(relative).parts
    if len(parts) == 2 and parts[0] == "h":
        return True
    if relative == "c/herdr/.plugins.lock":
        return True
    return (
        len(parts) == 5
        and parts[0:3] == ("c", "herdr", "sessions")
        and parts[4] not in {"herdr.sock", "herdr-client.sock"}
    )


def _owned_paths(
    value: object, session_name: str
) -> tuple[tuple[str, _PathIdentity], ...]:
    if not isinstance(value, list):
        raise HerdrValidationError("receipt owned_paths must be a list")
    if not value:
        raise HerdrValidationError("receipt owned_paths must not be empty")
    result: list[tuple[str, _PathIdentity]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {"relative", "identity"}:
            raise HerdrValidationError(f"receipt owned_paths[{index}] is invalid")
        relative = _text(item["relative"], f"owned_paths[{index}].relative")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise HerdrValidationError("receipt owned path escapes private root")
        if not _owned_relative_allowed(relative, session_name):
            raise HerdrValidationError("receipt owned path is outside the private tree")
        if relative in seen:
            raise HerdrValidationError("receipt owned_paths contains a duplicate")
        seen.add(relative)
        result.append(
            (
                relative,
                _path_identity_from_dict(item["identity"], "owned path identity"),
            )
        )
    return tuple(result)


def _validate_receipt_paths(receipt: HerdrReceipt) -> None:
    root = receipt.private_root
    expected = {
        "config_path": root / "c/herdr/config.toml",
        "socket_path": root / "c/herdr/sessions" / receipt.session_name / "herdr.sock",
        "client_socket_path": root
        / "c/herdr/sessions"
        / receipt.session_name
        / "herdr-client.sock",
        "session_dir": root / "c/herdr/sessions" / receipt.session_name,
    }
    for name, path in expected.items():
        if getattr(receipt, name) != path:
            raise HerdrValidationError(f"receipt {name} does not match private root")
    if receipt.server_argv[0] != str(receipt.executable):
        raise HerdrValidationError("receipt server argv does not match executable")
    if receipt.server_argv != (
        str(receipt.executable),
        "--session",
        receipt.session_name,
        "server",
    ):
        raise HerdrValidationError("receipt server argv does not match session")


def _safe_path_info(
    path: Path,
    *,
    require_socket: bool = False,
    require_private: bool = True,
) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HerdrValidationError(f"Herdr path is unavailable: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise HerdrValidationError(f"Herdr path must not be a symlink: {path}")
    if require_socket and not stat.S_ISSOCK(info.st_mode):
        raise HerdrValidationError(f"Herdr path is not a socket: {path}")
    if info.st_uid != os.geteuid():
        raise HerdrOwnershipError(f"Herdr path is not owned by this user: {path}")
    if require_private and stat.S_IMODE(info.st_mode) & 0o077:
        raise HerdrOwnershipError(f"Herdr path is not private: {path}")
    return info


def _safe_path_identity(
    path: Path,
    *,
    require_socket: bool = False,
    require_private: bool = True,
) -> _PathIdentity:
    info = _safe_path_info(
        path,
        require_socket=require_socket,
        require_private=require_private,
    )
    return _PathIdentity(
        info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), info.st_uid
    )


def _same_path_identity(
    path: Path,
    identity: _PathIdentity,
    *,
    require_socket: bool = False,
    require_private: bool = True,
) -> bool:
    try:
        current = _safe_path_identity(
            path,
            require_socket=require_socket,
            require_private=require_private,
        )
    except HerdrError:
        return False
    return current == identity


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
    return True


def _group_state(pgid: int) -> bool | None:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return _group_state_from_ps(pgid)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return _group_state_from_ps(pgid)
        return None
    return True


def _group_state_from_ps(pgid: int) -> bool | None:
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pgid="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
            env={"PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        groups = {
            int(line.strip()) for line in result.stdout.splitlines() if line.strip()
        }
    except ValueError:
        return None
    return pgid in groups


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
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.startswith("n") and len(line) > 1:
                return Path(line[1:])
    return None


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
        if result.returncode != 0:
            return None
        try:
            value = int(result.stdout.strip())
        except ValueError:
            return None
        return value if value > 0 else None
    return None


def _validate_managed_file(path: Path, relative: str) -> bool:
    if relative.endswith(("herdr.sock", "herdr-client.sock")):
        return True
    if not relative.endswith(("session.json", "session-history.json")):
        return True
    try:
        data = path.read_bytes()
        if len(data) > 4 * 1024 * 1024:
            return False
        json.loads(data.decode("utf-8"), parse_constant=_json_constant)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False
    return True


def _json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _require_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HerdrValidationError(f"Herdr {context} must be an object")
    return value


def _require_response_type(
    value: Mapping[str, object], expected: str
) -> Mapping[str, object]:
    if value.get("type") != expected:
        raise HerdrValidationError(
            f"Herdr response type mismatch: expected {expected}, got {value.get('type')!r}"
        )
    return value


class HerdrDriver:
    """Create and manage one private Herdr named session."""

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
            raise HerdrValidationError("Herdr private root must already exist")
        _safe_path_identity(self._private_root)
        self._run_nonce = _identifier(run_nonce, "run nonce")
        self._session_name = _identifier(session_name, "session name")
        self._config_path = self._private_root / "c/herdr/config.toml"
        self._session_dir = self._private_root / "c/herdr/sessions" / self._session_name
        self._socket_path = self._session_dir / "herdr.sock"
        self._client_socket_path = self._session_dir / "herdr-client.sock"
        self._receipt: HerdrReceipt | None = None
        self._server_process: subprocess.Popen[bytes] | None = None

    @classmethod
    def from_receipt(cls, receipt: HerdrReceipt) -> HerdrDriver:
        if not isinstance(receipt, HerdrReceipt):
            raise HerdrValidationError("Herdr receipt has an invalid type")
        restored = HerdrReceipt.from_dict(receipt.as_dict())
        driver = object.__new__(cls)
        driver._executable = cls._resolve_executable(restored.executable)
        if driver._executable != restored.executable:
            raise HerdrOwnershipError("Herdr executable path is not canonical")
        driver._private_root = restored.private_root
        driver._run_nonce = restored.run_nonce
        driver._session_name = restored.session_name
        driver._config_path = restored.config_path
        driver._session_dir = restored.session_dir
        driver._socket_path = restored.socket_path
        driver._client_socket_path = restored.client_socket_path
        driver._receipt = restored
        driver._server_process = None
        if not _same_path_identity(
            driver._private_root, restored.private_root_identity
        ):
            raise HerdrOwnershipError("Herdr private-root identity changed")
        inspected = driver.inspect(restored)
        if inspected.presence == "unknown":
            raise HerdrOwnershipError(inspected.reason or "Herdr ownership is unknown")
        return driver

    @staticmethod
    def _resolve_executable(value: str | Path) -> Path:
        raw = _text(os.fspath(value), "Herdr executable")
        candidate = Path(raw)
        if not candidate.is_absolute() and "/" not in raw:
            selected = shutil.which(raw)
            if selected is None:
                raise HerdrUnavailableError(
                    f"selected Herdr executable is unavailable: {raw}"
                )
            candidate = Path(selected)
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except OSError as exc:
            raise HerdrUnavailableError(
                f"selected Herdr executable is unavailable: {raw}"
            ) from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise HerdrUnavailableError(
                f"selected Herdr executable is not executable: {resolved}"
            )
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

    def _private_env(self) -> dict[str, str]:
        shell = "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/sh"
        return {
            "HOME": str(self._private_root / "h"),
            "PATH": f"{self._executable.parent}:/usr/bin:/bin",
            "SHELL": shell,
            "TERM": "xterm-256color",
            "LANG": "C",
            "TMPDIR": str(self._private_root / "t"),
            "XDG_CONFIG_HOME": str(self._private_root / "c"),
            "XDG_STATE_HOME": str(self._private_root / "s"),
            "HERDR_CONFIG_PATH": str(self._config_path),
        }

    @staticmethod
    def _validate_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(argv, tuple) or not argv:
            raise HerdrValidationError("Herdr argv must be a non-empty tuple")
        return _argv(argv, "argv")

    @staticmethod
    def _validate_env(env: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(env, Mapping):
            raise HerdrValidationError("Herdr environment must be a mapping")
        result: dict[str, str] = {}
        for raw_name, raw_value in env.items():
            name = _text(raw_name, "environment name")
            if _ENV_NAME_RE.fullmatch(name) is None:
                raise HerdrValidationError(f"environment name is invalid: {name}")
            if name in _RESERVED_ENV_NAMES:
                raise HerdrValidationError(f"environment name is reserved: {name}")
            value = raw_value
            if not isinstance(value, str) or "\x00" in value:
                raise HerdrValidationError(f"environment value is invalid: {name}")
            result[name] = value
        return dict(sorted(result.items()))

    def _preflight(self, cwd: str | Path) -> Path:
        root_identity = _safe_path_identity(self._private_root)
        if root_identity != _safe_path_identity(self._private_root):
            raise HerdrOwnershipError("Herdr private-root identity changed")
        if len(os.fsencode(str(self._socket_path))) > _MAX_SOCKET_BYTES:
            raise HerdrValidationError("Herdr socket path is too long")
        if self._config_path.exists() or self._config_path.is_symlink():
            raise HerdrOwnershipError("Herdr config path already exists")
        if self._socket_path.exists() or self._socket_path.is_symlink():
            raise HerdrOwnershipError("Herdr socket path already exists")
        for item in self._private_root.iterdir():
            if item.name not in {"h", "c", "s", "t"}:
                raise HerdrOwnershipError(
                    "Herdr private root contains an unknown entry"
                )
        working_directory = Path(cwd)
        if not working_directory.is_absolute() or not working_directory.is_dir():
            raise HerdrValidationError("Herdr working directory is invalid")
        return working_directory

    def _prepare_private_tree(self) -> _PathIdentity:
        for name in ("h", "c", "s", "t"):
            path = self._private_root / name
            try:
                path.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise HerdrOwnershipError(
                    f"Herdr private directory already exists: {path}"
                ) from exc
            _safe_path_identity(path)
        for path in (
            self._private_root / "c/herdr",
            self._private_root / "c/herdr/sessions",
        ):
            try:
                path.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise HerdrOwnershipError(
                    f"Herdr private directory already exists: {path}"
                ) from exc
            _safe_path_identity(path)
        encoded = (
            b"onboarding = false\n\n"
            b"[update]\nversion_check = false\nmanifest_check = false\n\n"
            b"[session]\nresume_agents_on_restore = false\n\n"
            b'[terminal]\ndefault_shell = "/bin/sh"\nshell_mode = "non_login"\n\n'
            b"[experimental]\npane_history = false\n\n"
            b'[ui.toast]\ndelivery = "off"\n'
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
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            raise HerdrError("Herdr private config could not be created") from exc
        return _safe_path_identity(self._private_root)

    def _server_argv(self) -> tuple[str, ...]:
        return (str(self._executable), "--session", self._session_name, "server")

    def _request(self, method: str, params: Mapping[str, object]) -> dict[str, object]:
        if not _IDENTIFIER_RE.fullmatch(method.replace(".", "-")):
            raise HerdrValidationError("Herdr API method is invalid")
        request_id = f"agent-team-{uuid.uuid4().hex}"
        request = {"id": request_id, "method": method, "params": dict(params)}
        encoded = (
            json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(_COMMAND_TIMEOUT_SECONDS)
                stream.connect(str(self._socket_path))
                stream.sendall(encoded)
                data = bytearray()
                while len(data) <= _MAX_RESPONSE_BYTES:
                    chunk = stream.recv(
                        min(65_536, _MAX_RESPONSE_BYTES + 1 - len(data))
                    )
                    if not chunk:
                        break
                    data.extend(chunk)
                    if b"\n" in chunk:
                        break
        except (OSError, ValueError) as exc:
            raise HerdrApiError("Herdr API request could not be completed") from exc
        if not data:
            raise HerdrApiError("Herdr API response was empty")
        line = bytes(data).split(b"\n", 1)[0]
        if len(line) > _MAX_RESPONSE_BYTES:
            raise HerdrApiError("Herdr API response is too large")
        try:
            response = json.loads(line.decode("utf-8"), parse_constant=_json_constant)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise HerdrApiError("Herdr API response is not valid JSON") from exc
        if not isinstance(response, Mapping) or response.get("id") != request_id:
            raise HerdrApiError("Herdr API response ID does not match request")
        keys = set(response)
        if keys == {"id", "error"}:
            error = _require_mapping(response.get("error"), "API error")
            code = _text(error.get("code"), "API error code")
            message = _text(error.get("message"), "API error message")
            raise HerdrApiError(f"Herdr API {code}: {message}")
        if keys != {"id", "result"}:
            raise HerdrApiError("Herdr API response has unsupported fields")
        return dict(_require_mapping(response.get("result"), "API result"))

    def _wait_for_socket(self, process: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise HerdrError("Herdr server exited before its socket became ready")
            if self._socket_path.exists() and self._client_socket_path.exists():
                return
            time.sleep(_POLL_SECONDS)
        raise HerdrError("Herdr server socket did not become ready")

    def _workspace_created(
        self, result: Mapping[str, object], cwd: Path
    ) -> tuple[str, str, str, str]:
        _require_response_type(result, "workspace_created")
        workspace = _require_mapping(result.get("workspace"), "workspace")
        tab = _require_mapping(result.get("tab"), "tab")
        pane = _require_mapping(result.get("root_pane"), "root pane")
        workspace_id = _opaque_id(workspace.get("workspace_id"), "workspace ID")
        tab_id = _opaque_id(tab.get("tab_id"), "tab ID")
        pane_id = _opaque_id(pane.get("pane_id"), "pane ID")
        terminal_id = _opaque_id(pane.get("terminal_id"), "terminal ID")
        if tab.get("workspace_id") != workspace_id:
            raise HerdrValidationError("workspace.create returned inconsistent tab")
        if pane.get("workspace_id") != workspace_id or pane.get("tab_id") != tab_id:
            raise HerdrValidationError("workspace.create returned inconsistent pane")
        if pane.get("cwd") not in {None, str(cwd)}:
            raise HerdrValidationError("workspace.create returned an unexpected cwd")
        return workspace_id, tab_id, pane_id, terminal_id

    def _supervisor_process_info(
        self,
        pane_id: str,
        supervisor_argv: tuple[str, ...],
        cwd: Path,
    ) -> tuple[int, int]:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            result = self._request("pane.process_info", {"pane_id": pane_id})
            _require_response_type(result, "pane_process_info")
            info = _require_mapping(result.get("process_info"), "pane process info")
            shell_pid = info.get("shell_pid")
            group_id = info.get("foreground_process_group_id")
            if (
                isinstance(shell_pid, int)
                and not isinstance(shell_pid, bool)
                and shell_pid > 0
                and isinstance(group_id, int)
                and not isinstance(group_id, bool)
                and group_id > 0
            ):
                processes = info.get("foreground_processes")
                if isinstance(processes, list):
                    for process in processes:
                        if not isinstance(process, Mapping):
                            continue
                        if process.get("pid") != shell_pid:
                            continue
                        raw_argv = process.get("argv")
                        raw_cwd = process.get("cwd")
                        if (
                            isinstance(raw_argv, list)
                            and tuple(raw_argv) == supervisor_argv
                            and raw_cwd == str(cwd)
                        ):
                            return shell_pid, group_id
            time.sleep(_POLL_SECONDS)
        raise HerdrError("Herdr supervisor process did not become ready")

    def _snapshot(self, result: Mapping[str, object]) -> Mapping[str, object]:
        _require_response_type(result, "session_snapshot")
        snapshot = _require_mapping(result.get("snapshot"), "session snapshot")
        allowed = {
            "version",
            "protocol",
            "focused_workspace_id",
            "focused_tab_id",
            "focused_pane_id",
            "workspaces",
            "tabs",
            "panes",
            "layouts",
            "agents",
        }
        unknown = set(snapshot) - allowed
        if unknown:
            raise HerdrValidationError(
                "Herdr session snapshot has unsupported resource fields"
            )
        if (
            snapshot.get("version") != HERDR_VERSION
            or snapshot.get("protocol") != HERDR_PROTOCOL
        ):
            raise HerdrValidationError(
                "Herdr session snapshot version/protocol mismatch"
            )
        for name in ("workspaces", "tabs", "panes", "layouts", "agents"):
            if not isinstance(snapshot.get(name), list):
                raise HerdrValidationError(f"Herdr session snapshot is missing {name}")
        for name in (
            "focused_workspace_id",
            "focused_tab_id",
            "focused_pane_id",
        ):
            value = snapshot.get(name)
            if value is not None and not isinstance(value, str):
                raise HerdrValidationError(f"Herdr {name} is invalid")
        return snapshot

    def _validate_snapshot_resources(
        self, receipt: HerdrReceipt, snapshot: Mapping[str, object]
    ) -> tuple[tuple[list[object], ...], tuple[Mapping[str, object], ...] | None]:
        arrays = tuple(
            cast(list[object], snapshot[name])
            for name in ("workspaces", "tabs", "panes", "layouts", "agents")
        )
        focus_ids = (
            ("focused_workspace_id", receipt.workspace_id),
            ("focused_tab_id", receipt.tab_id),
            ("focused_pane_id", receipt.pane_id),
        )
        empty = all(not values for values in arrays)
        for name, expected in focus_ids:
            value = snapshot.get(name)
            if empty:
                if value is not None:
                    raise HerdrValidationError(f"Herdr empty snapshot has {name}")
            elif value is not None and value != expected:
                raise HerdrValidationError(f"Herdr {name} contradicts owned IDs")
        if empty:
            return arrays, None
        if any(len(values) != 1 for values in arrays[:4]) or arrays[4]:
            return arrays, None
        resources = tuple(
            _require_mapping(values[0], "snapshot resource") for values in arrays[:4]
        )
        workspace, tab, pane, layout = resources
        pane_cwd = pane.get("cwd")
        if pane_cwd is not None and (
            not isinstance(pane_cwd, str) or pane_cwd != str(receipt.cwd)
        ):
            raise HerdrValidationError("Herdr pane cwd is invalid")
        if (
            workspace.get("workspace_id") != receipt.workspace_id
            or tab.get("tab_id") != receipt.tab_id
            or tab.get("workspace_id") != receipt.workspace_id
            or pane.get("pane_id") != receipt.pane_id
            or pane.get("terminal_id") != receipt.terminal_id
            or pane.get("workspace_id") != receipt.workspace_id
            or pane.get("tab_id") != receipt.tab_id
            or layout.get("workspace_id") != receipt.workspace_id
            or layout.get("tab_id") != receipt.tab_id
        ):
            raise HerdrValidationError("Herdr workspace/tab/pane identity changed")
        raw_layout_panes = layout.get("panes")
        if not isinstance(raw_layout_panes, list) or len(raw_layout_panes) != 1:
            raise HerdrValidationError("Herdr layout identity is invalid")
        layout_pane = _require_mapping(raw_layout_panes[0], "layout pane")
        if layout_pane.get("pane_id") != receipt.pane_id:
            raise HerdrValidationError("Herdr layout contains an unowned pane")
        return arrays, resources

    def _capture_owned_paths(self) -> tuple[tuple[str, _PathIdentity], ...]:
        paths = (
            self._private_root / "h",
            self._private_root / "c",
            self._private_root / "c/herdr",
            self._private_root / "c/herdr/config.toml",
            self._private_root / "c/herdr/.plugins.lock",
            self._private_root / "c/herdr/sessions",
            self._session_dir,
            self._socket_path,
            self._client_socket_path,
            self._private_root / "s",
            self._private_root / "t",
        )
        result: list[tuple[str, _PathIdentity]] = []
        seen: set[Path] = set()
        for path in paths:
            try:
                identity = _safe_path_identity(
                    path,
                    require_socket=path
                    in {self._socket_path, self._client_socket_path},
                    require_private=path
                    not in {
                        self._session_dir,
                        self._private_root / "c/herdr/.plugins.lock",
                    },
                )
            except HerdrError:
                continue
            seen.add(path)
            result.append((path.relative_to(self._private_root).as_posix(), identity))
        for base in (
            self._private_root / "h",
            self._private_root / "c/herdr",
            self._private_root / "s",
            self._private_root / "t",
        ):
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if path in seen or path == self._session_dir:
                    continue
                try:
                    path.relative_to(self._session_dir)
                except ValueError:
                    pass
                else:
                    continue
                try:
                    identity = _safe_path_identity(path, require_private=False)
                except HerdrError:
                    continue
                result.append(
                    (path.relative_to(self._private_root).as_posix(), identity)
                )
                seen.add(path)
        return tuple(result)

    def _scan_private_tree(
        self, receipt: HerdrReceipt, *, allow_missing_session: bool = False
    ) -> dict[str, _PathIdentity] | None:
        try:
            _safe_path_identity(self._private_root)
        except HerdrError:
            return None
        result: dict[str, _PathIdentity] = {}
        pending = [self._private_root]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                return None
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(self._private_root).as_posix()
                if not _owned_relative_allowed(relative, self._session_name):
                    return None
                is_socket = path in {receipt.socket_path, receipt.client_socket_path}
                require_private = not (
                    relative == "c/herdr/.plugins.lock"
                    or relative.startswith(
                        ("h/", f"c/herdr/sessions/{self._session_name}/")
                    )
                    or relative == f"c/herdr/sessions/{self._session_name}"
                )
                try:
                    identity = _safe_path_identity(
                        path,
                        require_socket=is_socket,
                        require_private=require_private,
                    )
                except HerdrError:
                    return None
                if not entry.is_dir(
                    follow_symlinks=False
                ) and not _validate_managed_file(path, relative):
                    return None
                result[relative] = identity
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                except OSError:
                    return None
        session_relative = self._session_dir.relative_to(self._private_root).as_posix()
        if not allow_missing_session and session_relative not in result:
            return None
        return result

    def _current_owned_paths(
        self, receipt: HerdrReceipt
    ) -> dict[str, _PathIdentity] | None:
        return self._scan_private_tree(receipt)

    def create(
        self,
        argv: tuple[str, ...],
        cwd: str | Path,
        env: Mapping[str, str],
        title: str,
    ) -> HerdrReceipt:
        if self._receipt is not None:
            raise HerdrValidationError("this Herdr driver already created a workspace")
        command_argv = self._validate_argv(argv)
        environment = self._validate_env(env)
        title = _text(title, "Herdr workspace title")
        if len(title) > 256:
            raise HerdrValidationError("Herdr workspace title is too long")
        working_directory = self._preflight(cwd)
        root_identity = self._prepare_private_tree()
        server_argv = self._server_argv()
        try:
            process = subprocess.Popen(
                server_argv,
                cwd=str(self._private_root),
                env=self._private_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise HerdrUnavailableError("Herdr server could not be started") from exc
        server_pid = _pid(process.pid, "Herdr server PID")
        self._server_process = process
        self._wait_for_socket(process)
        server_argv = tuple(server_argv)
        if read_process_argv(server_pid) != server_argv:
            raise HerdrOwnershipError("Herdr server argv identity is unproven")
        try:
            server_pgid = _pid(os.getpgid(server_pid), "Herdr server PGID")
        except OSError as exc:
            raise HerdrOwnershipError("Herdr server PGID is unavailable") from exc
        server_cwd = _read_process_cwd(server_pid)
        if server_cwd is None:
            raise HerdrOwnershipError("Herdr server cwd identity is unproven")
        if server_cwd.resolve() != self._private_root.resolve():
            raise HerdrOwnershipError("Herdr server cwd identity changed")
        ping = _require_response_type(self._request("ping", {}), "pong")
        if (
            ping.get("version") != HERDR_VERSION
            or ping.get("protocol") != HERDR_PROTOCOL
        ):
            raise HerdrValidationError("Herdr version/protocol is unsupported")
        initial = self._snapshot(self._request("session.snapshot", {}))
        if any(
            initial[name]
            for name in ("workspaces", "tabs", "panes", "layouts", "agents")
        ):
            raise HerdrOwnershipError("private Herdr session was not empty at startup")
        subscriptions = [
            {"type": name}
            for name in (
                "workspace.created",
                "workspace.closed",
                "pane.created",
                "pane.closed",
                "pane.exited",
            )
        ]
        _require_response_type(
            self._request("events.subscribe", {"subscriptions": subscriptions}),
            "subscription_started",
        )
        created = self._workspace_created(
            self._request(
                "workspace.create",
                {
                    "cwd": str(working_directory.resolve()),
                    "focus": False,
                    "label": title,
                    "env": environment,
                },
            ),
            working_directory.resolve(),
        )
        workspace_id, tab_id, pane_id, terminal_id = created
        supervisor_kernel_argv = (
            python_process_argv(command_argv)
            if command_argv[0] == sys.executable
            else command_argv
        )
        assignments = [f"{name}={value}" for name, value in environment.items()]
        command = shlex.join(("exec", "env", "-i", *assignments, *command_argv))
        _require_response_type(
            self._request(
                "pane.send_input",
                {"pane_id": pane_id, "text": command, "keys": ["Enter"]},
            ),
            "ok",
        )
        pane_pid, pane_pgid = self._supervisor_process_info(
            pane_id, supervisor_kernel_argv, working_directory.resolve()
        )
        if read_process_argv(pane_pid) != supervisor_kernel_argv:
            raise HerdrOwnershipError("Herdr supervisor argv identity is unproven")
        supervisor_ppid = _read_process_ppid(pane_pid)
        if supervisor_ppid is None or supervisor_ppid != server_pid:
            raise HerdrOwnershipError("Herdr supervisor PPID identity is unproven")
        try:
            if os.getpgid(pane_pid) != pane_pgid:
                raise HerdrOwnershipError("Herdr supervisor PGID identity changed")
        except OSError as exc:
            raise HerdrOwnershipError("Herdr supervisor PGID is unavailable") from exc
        process_cwd = _read_process_cwd(pane_pid)
        if process_cwd is None or process_cwd.resolve() != working_directory.resolve():
            raise HerdrOwnershipError("Herdr supervisor cwd identity is unproven")
        socket_identity = _safe_path_identity(self._socket_path, require_socket=True)
        client_socket_identity = _safe_path_identity(
            self._client_socket_path, require_socket=True
        )
        config_identity = _safe_path_identity(self._config_path)
        session_identity = _safe_path_identity(self._session_dir, require_private=False)
        receipt = HerdrReceipt(
            executable=self._executable,
            private_root=self._private_root,
            config_path=self._config_path,
            socket_path=self._socket_path,
            client_socket_path=self._client_socket_path,
            session_dir=self._session_dir,
            run_nonce=self._run_nonce,
            session_name=self._session_name,
            workspace_id=workspace_id,
            tab_id=tab_id,
            pane_id=pane_id,
            terminal_id=terminal_id,
            pane_pid=pane_pid,
            pane_pgid=pane_pgid,
            server_pid=server_pid,
            server_pgid=server_pgid,
            supervisor_ppid=supervisor_ppid,
            server_argv=server_argv,
            supervisor_argv=supervisor_kernel_argv,
            server_cwd=server_cwd,
            cwd=working_directory.resolve(),
            version=HERDR_VERSION,
            protocol=HERDR_PROTOCOL,
            config_identity=config_identity,
            socket_identity=socket_identity,
            client_socket_identity=client_socket_identity,
            private_root_identity=root_identity,
            session_dir_identity=session_identity,
            owned_paths=self._capture_owned_paths(),
        )
        self._receipt = receipt
        inspected = self.inspect(receipt)
        if inspected.presence != "present":
            raise HerdrOwnershipError(
                inspected.reason or "Herdr workspace ownership could not be verified"
            )
        return receipt

    def _receipt_is_for_driver(self, receipt: HerdrReceipt) -> bool:
        if not isinstance(receipt, HerdrReceipt):
            return False
        try:
            _validate_receipt_paths(receipt)
        except HerdrError:
            return False
        return (
            receipt.executable == self._executable
            and receipt.private_root == self._private_root
            and receipt.config_path == self._config_path
            and receipt.socket_path == self._socket_path
            and receipt.client_socket_path == self._client_socket_path
            and receipt.session_dir == self._session_dir
            and receipt.run_nonce == self._run_nonce
            and receipt.session_name == self._session_name
        )

    def _server_verified(self, receipt: HerdrReceipt) -> HerdrInspection | None:
        if not self._receipt_is_for_driver(receipt):
            return HerdrInspection.unknown("Herdr receipt is not owned by this driver")
        if not _same_path_identity(self._private_root, receipt.private_root_identity):
            return HerdrInspection.unknown("Herdr private-root identity changed")
        if not _same_path_identity(receipt.config_path, receipt.config_identity):
            return HerdrInspection.unknown("Herdr config identity changed")
        if not _same_path_identity(
            receipt.socket_path, receipt.socket_identity, require_socket=True
        ) or not _same_path_identity(
            receipt.client_socket_path,
            receipt.client_socket_identity,
            require_socket=True,
        ):
            return HerdrInspection.unknown("Herdr socket identity changed")
        if not _same_path_identity(
            receipt.session_dir,
            receipt.session_dir_identity,
            require_private=False,
        ):
            return HerdrInspection.unknown("Herdr session identity changed")
        alive = _pid_state(receipt.server_pid)
        if alive is not True:
            return HerdrInspection.unknown(
                "Herdr server liveness is unproven",
                session_present=alive is False,
                server_pid=receipt.server_pid,
                observed_nonce=receipt.run_nonce,
            )
        if read_process_argv(receipt.server_pid) != receipt.server_argv:
            return HerdrInspection.unknown(
                "Herdr server argv identity changed",
                session_present=True,
                server_pid=receipt.server_pid,
                observed_nonce=receipt.run_nonce,
            )
        observed_cwd = _read_process_cwd(receipt.server_pid)
        if (
            observed_cwd is None
            or observed_cwd.resolve() != receipt.server_cwd.resolve()
        ):
            return HerdrInspection.unknown(
                "Herdr server cwd changed",
                session_present=True,
                server_pid=receipt.server_pid,
                observed_nonce=receipt.run_nonce,
            )
        try:
            observed_pgid = os.getpgid(receipt.server_pid)
        except OSError:
            return HerdrInspection.unknown(
                "Herdr server PGID is unavailable",
                session_present=True,
                server_pid=receipt.server_pid,
                observed_nonce=receipt.run_nonce,
            )
        if observed_pgid != receipt.server_pgid:
            return HerdrInspection.unknown(
                "Herdr server PGID changed",
                session_present=True,
                server_pid=receipt.server_pid,
                observed_nonce=receipt.run_nonce,
            )
        return None

    def inspect(self, receipt: HerdrReceipt) -> HerdrInspection:
        server_failure = self._server_verified(receipt)
        if server_failure is not None:
            return server_failure
        try:
            snapshot = self._snapshot(self._request("session.snapshot", {}))
        except (HerdrError, OSError, TypeError, ValueError) as exc:
            return HerdrInspection.unknown(
                f"Herdr session snapshot is unproven: {type(exc).__name__}",
                session_present=True,
                server_pid=receipt.server_pid,
                observed_nonce=receipt.run_nonce,
            )
        try:
            arrays, resources = self._validate_snapshot_resources(receipt, snapshot)
        except (HerdrError, TypeError, ValueError) as exc:
            return HerdrInspection.unknown(
                f"Herdr session snapshot schema is unproven: {type(exc).__name__}",
                session_present=True,
                server_pid=receipt.server_pid,
                observed_nonce=receipt.run_nonce,
            )
        if all(not values for values in arrays):
            pane_alive = _pid_state(receipt.pane_pid)
            if pane_alive is not False:
                return HerdrInspection.unknown(
                    "Herdr snapshot is empty but supervisor PID is not proven dead",
                    session_present=True,
                    pane_pid=receipt.pane_pid,
                    server_pid=receipt.server_pid,
                    observed_nonce=receipt.run_nonce,
                )
            return HerdrInspection(
                presence="absent",
                running=False,
                exit_status=None,
                identity_verified=True,
                pane_present=False,
                session_present=True,
                pane_pid=None,
                server_pid=receipt.server_pid,
                observed_nonce=receipt.run_nonce,
                reason="owned Herdr pane is absent",
                snapshot_empty=True,
            )
        if resources is None:
            return HerdrInspection.unknown(
                "Herdr session snapshot contains an unowned resource",
                pane_present=bool(arrays[2]),
                session_present=True,
                server_pid=receipt.server_pid,
                observed_nonce=receipt.run_nonce,
            )
        try:
            process_result = _require_response_type(
                self._request("pane.process_info", {"pane_id": receipt.pane_id}),
                "pane_process_info",
            )
            process_info = _require_mapping(
                process_result.get("process_info"), "pane process info"
            )
            if (
                process_info.get("pane_id") != receipt.pane_id
                or process_info.get("shell_pid") != receipt.pane_pid
                or process_info.get("foreground_process_group_id") != receipt.pane_pgid
            ):
                raise HerdrValidationError("Herdr pane process identity changed")
            foreground = process_info.get("foreground_processes")
            if not isinstance(foreground, list):
                raise HerdrValidationError("Herdr foreground process list is missing")
            matched = False
            for process in foreground:
                if (
                    not isinstance(process, Mapping)
                    or process.get("pid") != receipt.pane_pid
                ):
                    continue
                if tuple(process.get("argv", ())) != receipt.supervisor_argv:
                    continue
                if process.get("cwd") != str(receipt.cwd):
                    continue
                matched = True
                break
            if not matched:
                raise HerdrValidationError("Herdr supervisor process is not present")
            if read_process_argv(receipt.pane_pid) != receipt.supervisor_argv:
                raise HerdrValidationError("Herdr supervisor argv changed")
            if _read_process_ppid(receipt.pane_pid) != receipt.supervisor_ppid:
                raise HerdrValidationError("Herdr supervisor PPID changed")
            if os.getpgid(receipt.pane_pid) != receipt.pane_pgid:
                raise HerdrValidationError("Herdr supervisor PGID changed")
            process_cwd = _read_process_cwd(receipt.pane_pid)
            if process_cwd is None or process_cwd.resolve() != receipt.cwd.resolve():
                raise HerdrValidationError("Herdr supervisor cwd is unproven")
        except (HerdrError, OSError, TypeError, ValueError) as exc:
            return HerdrInspection.unknown(
                f"Herdr supervisor identity is unproven: {type(exc).__name__}",
                pane_present=True,
                session_present=True,
                pane_pid=receipt.pane_pid,
                server_pid=receipt.server_pid,
                observed_nonce=receipt.run_nonce,
            )
        return HerdrInspection(
            presence="present",
            running=True,
            exit_status=None,
            identity_verified=True,
            pane_present=True,
            session_present=True,
            pane_pid=receipt.pane_pid,
            server_pid=receipt.server_pid,
            observed_nonce=receipt.run_nonce,
            snapshot_empty=False,
            pane_pgid=receipt.pane_pgid,
        )

    def attach_argv(self, receipt: HerdrReceipt) -> tuple[str, ...]:
        if not self._receipt_is_for_driver(receipt):
            raise HerdrOwnershipError(
                "cannot attach to a receipt owned by another driver"
            )
        inspected = self.inspect(receipt)
        if inspected.presence != "present":
            raise HerdrOwnershipError(inspected.reason or "Herdr pane is not present")
        return (
            str(self._executable),
            "--session",
            self._session_name,
            "terminal",
            "attach",
            receipt.terminal_id,
        )

    def _session_command(self, action: str) -> subprocess.CompletedProcess[str]:
        if action not in {"stop", "delete"}:
            raise HerdrValidationError("unsupported Herdr session command")
        argv = (
            str(self._executable),
            "--session",
            self._session_name,
            "session",
            action,
            self._session_name,
            "--json",
        )
        try:
            return subprocess.run(
                argv,
                cwd=str(self._private_root),
                env=self._private_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HerdrError(f"Herdr session {action} command failed") from exc

    @staticmethod
    def _session_command_ok(
        result: subprocess.CompletedProcess[str], action: str, session_name: str
    ) -> bool:
        if result.returncode != 0:
            return False
        try:
            value = json.loads(result.stdout, parse_constant=_json_constant)
        except (ValueError, json.JSONDecodeError):
            return False
        if not isinstance(value, Mapping):
            return False
        session = value.get("session")
        if not isinstance(session, Mapping):
            return False
        if (
            session.get("name") != session_name
            or session.get("default") is not False
            or session.get("running") is not False
        ):
            return False
        return value.get("stopped" if action == "stop" else "deleted") is True

    def _wait_server_terminated(
        self, receipt: HerdrReceipt
    ) -> tuple[bool | None, bool | None]:
        deadline = time.monotonic() + _COMMAND_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._server_process is not None:
                try:
                    self._server_process.wait(timeout=0)
                except subprocess.TimeoutExpired:
                    pass
                except OSError:
                    pass
            server_state = _pid_state(receipt.server_pid)
            group_state = _group_state(receipt.server_pgid)
            if server_state is False and group_state is False:
                return server_state, group_state
            time.sleep(_POLL_SECONDS)
        return _pid_state(receipt.server_pid), _group_state(receipt.server_pgid)

    def _known_paths_owned(
        self, receipt: HerdrReceipt, *, allow_stopped_sockets: bool = False
    ) -> bool:
        if not _same_path_identity(self._private_root, receipt.private_root_identity):
            return False
        try:
            current_names = {path.name for path in self._private_root.iterdir()}
        except OSError:
            return False
        if not current_names.issubset({"h", "c", "s", "t"}):
            return False
        current = self._current_owned_paths(receipt)
        if current is None:
            return False
        expected = dict(receipt.owned_paths)
        if not expected:
            return False
        for relative, identity in expected.items():
            observed = current.get(relative)
            if observed is None:
                if allow_stopped_sockets and relative in {
                    receipt.socket_path.relative_to(self._private_root).as_posix(),
                    receipt.client_socket_path.relative_to(
                        self._private_root
                    ).as_posix(),
                }:
                    continue
                return False
            if not _relative_mutable(relative) and observed != identity:
                return False
        return all(
            relative in expected or _relative_mutable(relative) for relative in current
        )

    @staticmethod
    def _remove_checked_path(
        path: Path,
        expected: _PathIdentity,
        *,
        require_private: bool,
    ) -> bool:
        try:
            info = _safe_path_info(path, require_private=require_private)
            observed = _PathIdentity(
                info.st_dev,
                info.st_ino,
                stat.S_IMODE(info.st_mode),
                info.st_uid,
            )
            if observed != expected:
                return False
            if stat.S_ISDIR(info.st_mode):
                path.rmdir()
            elif stat.S_ISREG(info.st_mode):
                path.unlink()
            else:
                return False
        except (HerdrError, OSError):
            return False
        return True

    def _cleanup_known_paths(self, receipt: HerdrReceipt) -> bool:
        current = self._scan_private_tree(receipt, allow_missing_session=True)
        if current is None:
            return False
        expected = dict(receipt.owned_paths)
        if not expected:
            return False
        socket_relatives = {
            receipt.socket_path.relative_to(self._private_root).as_posix(),
            receipt.client_socket_path.relative_to(self._private_root).as_posix(),
        }
        session_relative = receipt.session_dir.relative_to(
            self._private_root
        ).as_posix()
        for relative, identity in expected.items():
            observed = current.get(relative)
            if observed is None:
                if relative in socket_relatives or relative == session_relative:
                    continue
                return False
            if not _relative_mutable(relative) and observed != identity:
                return False
        fixed = {
            "c/herdr/config.toml",
            "c/herdr/.plugins.lock",
            "c/herdr/sessions",
            "c/herdr",
            "c",
            "h",
            "s",
            "t",
        }
        for relative in sorted(
            current,
            key=lambda item: len(Path(item).parts),
            reverse=True,
        ):
            path = self._private_root / relative
            if relative in fixed:
                continue
            if path in {receipt.socket_path, receipt.client_socket_path}:
                return False
            if not self._remove_checked_path(
                path,
                current[relative],
                require_private=not _relative_mutable(relative),
            ):
                return False
        for relative in (
            "c/herdr/config.toml",
            "c/herdr/.plugins.lock",
            "c/herdr/sessions",
            "c/herdr",
            "c",
            "h",
            "s",
            "t",
        ):
            path = self._private_root / relative
            current_identity: _PathIdentity | None = current.get(relative)
            if current_identity is None:
                return False
            if not self._remove_checked_path(
                path,
                current_identity,
                require_private=not _relative_mutable(relative),
            ):
                return False
        return not any(self._private_root.iterdir())

    def close(self, receipt: HerdrReceipt) -> HerdrCloseResult:
        if not self._receipt_is_for_driver(receipt):
            return HerdrCloseResult(
                CloseEvidence.OWNERSHIP_UNPROVEN,
                False,
                False,
                False,
                False,
                reason="Herdr receipt is not owned by this driver",
            )
        inspected = self.inspect(receipt)
        if not inspected.identity_verified or inspected.presence == "unknown":
            return HerdrCloseResult(
                CloseEvidence.OWNERSHIP_UNPROVEN,
                False,
                False,
                False,
                False,
                reason=inspected.reason,
            )
        if not self._known_paths_owned(receipt):
            return HerdrCloseResult(
                CloseEvidence.OWNERSHIP_UNPROVEN,
                False,
                False,
                False,
                False,
                reason="Herdr private resource identity is unproven",
            )
        if inspected.presence == "present":
            try:
                _require_response_type(
                    self._request(
                        "workspace.close", {"workspace_id": receipt.workspace_id}
                    ),
                    "ok",
                )
            except (HerdrError, OSError, TypeError, ValueError) as exc:
                return HerdrCloseResult(
                    CloseEvidence.TERMINATION_UNPROVEN,
                    False,
                    False,
                    False,
                    True,
                    reason=f"Herdr workspace close is unproven: {type(exc).__name__}",
                )
            inspected = self.inspect(receipt)
            if inspected.presence != "absent" or not inspected.identity_verified:
                return HerdrCloseResult(
                    CloseEvidence.TERMINATION_UNPROVEN,
                    False,
                    False,
                    False,
                    True,
                    reason=inspected.reason or "Herdr workspace close is unproven",
                )
        if not self._known_paths_owned(receipt):
            return HerdrCloseResult(
                CloseEvidence.OWNERSHIP_UNPROVEN,
                False,
                False,
                False,
                True,
                reason="Herdr private resource identity changed before stop",
            )
        try:
            stopped = self._session_command("stop")
        except HerdrError as exc:
            return HerdrCloseResult(
                CloseEvidence.TERMINATION_UNPROVEN,
                False,
                False,
                False,
                True,
                reason=str(exc),
            )
        if not self._session_command_ok(stopped, "stop", self._session_name):
            return HerdrCloseResult(
                CloseEvidence.TERMINATION_UNPROVEN,
                False,
                False,
                False,
                True,
                reason="Herdr session stop response is unproven",
            )
        server_state, group_state = self._wait_server_terminated(receipt)
        if server_state is not False or group_state is not False:
            return HerdrCloseResult(
                CloseEvidence.SESSION_TERMINATED,
                True,
                False,
                False,
                True,
                reason=(
                    "Herdr server/group termination is unproven: "
                    f"server_state={server_state!r}, group_state={group_state!r}, "
                    f"server_pid={receipt.server_pid}, server_pgid={receipt.server_pgid}"
                ),
            )
        if not self._known_paths_owned(receipt, allow_stopped_sockets=True):
            return HerdrCloseResult(
                CloseEvidence.TERMINATION_UNPROVEN,
                True,
                True,
                not self._socket_path.exists()
                and not self._client_socket_path.exists(),
                True,
                reason="Herdr private resource identity changed before delete",
            )
        try:
            deleted = self._session_command("delete")
        except HerdrError as exc:
            return HerdrCloseResult(
                CloseEvidence.SESSION_TERMINATED,
                True,
                True,
                False,
                True,
                reason=str(exc),
            )
        if not self._session_command_ok(deleted, "delete", self._session_name):
            return HerdrCloseResult(
                CloseEvidence.SESSION_TERMINATED,
                True,
                True,
                False,
                True,
                reason="Herdr session delete response is unproven",
            )
        socket_removed = (
            not self._socket_path.exists() and not self._client_socket_path.exists()
        )
        session_removed = (
            not self._session_dir.exists() and not self._session_dir.is_symlink()
        )
        private_clean = self._cleanup_known_paths(receipt) if session_removed else False
        if not socket_removed or not session_removed or not private_clean:
            return HerdrCloseResult(
                CloseEvidence.SESSION_TERMINATED,
                True,
                True,
                socket_removed,
                True,
                reason="Herdr private-resource cleanup is unproven",
            )
        self._server_process = None
        return HerdrCloseResult(
            CloseEvidence.SERVER_TERMINATED,
            True,
            True,
            True,
            True,
        )


def _identity_for_relative(
    receipt: HerdrReceipt, relative: str
) -> _PathIdentity | None:
    for candidate, identity in receipt.owned_paths:
        if candidate == relative:
            return identity
    return None


def _identity(path: Path) -> _PathIdentity:
    return _safe_path_identity(path)


__all__ = (
    "HERDR_PROTOCOL",
    "HERDR_VERSION",
    "CloseEvidence",
    "HerdrApiError",
    "HerdrCloseResult",
    "HerdrDriver",
    "HerdrError",
    "HerdrInspection",
    "HerdrOwnershipError",
    "HerdrReceipt",
    "HerdrUnavailableError",
    "HerdrValidationError",
)
