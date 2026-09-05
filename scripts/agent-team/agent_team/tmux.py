"""A small, ownership-checked driver for private tmux sessions.

This module only owns terminal process placement and observation.  Team
coordination, task state, and completion policy stay in the caller.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final


class TmuxError(RuntimeError):
    """Base class for deterministic tmux-driver failures."""


class TmuxUnavailableError(TmuxError):
    """The selected tmux executable is absent or cannot be executed."""


class TmuxValidationError(TmuxError):
    """The driver input or a tmux response is invalid."""


class TmuxOwnershipError(TmuxError):
    """An operation would address a resource not proven to be ours."""


class CloseEvidence(str, Enum):
    """Evidence returned by :meth:`TmuxDriver.close`."""

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
class TmuxReceipt:
    """Immutable identity for one driver-created tmux pane."""

    executable: Path
    socket_path: Path
    config_path: Path
    run_nonce: str
    session_name: str
    session_id: str
    window_id: str
    pane_id: str
    pane_pid: int
    server_pid: int
    socket_identity: _PathIdentity
    config_identity: _PathIdentity


@dataclass(frozen=True)
class TmuxInspection:
    """A point-in-time pane observation and its ownership confidence.

    ``running`` is ``None`` when the pane cannot be trusted or observed.
    ``exit_status`` is taken from tmux's ``pane_dead_status`` metadata and is
    never inferred from pane output.
    """

    running: bool | None
    exit_status: int | None
    identity_verified: bool
    pane_present: bool
    session_present: bool | None
    pane_pid: int | None
    server_pid: int | None
    observed_nonce: str | None
    reason: str | None = None


@dataclass(frozen=True)
class TmuxCloseResult:
    """Evidence about the owned tmux session close operation.

    A successful session/server close does not prove that arbitrary
    descendants of the pane process stopped, so ``descendants_stopped`` is
    deliberately always false for this driver.
    """

    evidence: CloseEvidence
    session_terminated: bool
    server_terminated: bool
    socket_removed: bool
    descendants_stopped: bool
    exit_status: int | None
    ownership_verified: bool
    reason: str | None = None


_IDENTIFIER_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_PANE_ID_RE: Final = re.compile(r"%[0-9]+\Z")
_WINDOW_ID_RE: Final = re.compile(r"@[0-9]+\Z")
_SESSION_ID_RE: Final = re.compile(r"\$[0-9]+\Z")
_MAX_SOCKET_BYTES: Final = 96
_CREATE_FORMAT: Final = "#{session_id}|#{window_id}|#{pane_id}|#{pane_pid}|#{pid}"
_INSPECT_FORMAT: Final = (
    "#{session_id}|#{session_name}|#{window_id}|#{pane_id}|#{pane_pid}|#{pid}|"
    "#{pane_dead}|#{pane_dead_status}|#{@agent_team_run_nonce}"
)
_PANES_FORMAT: Final = "#{session_id}|#{window_id}|#{pane_id}|#{pane_pid}"
_CONTROL_CONFIG: Final = (
    "set-window-option -g remain-on-exit on\nset-option -g exit-empty on\n"
)
_COMMAND_TIMEOUT_SECONDS: Final = 10.0
_SERVER_EXIT_TIMEOUT_SECONDS: Final = 2.0
_SERVER_EXIT_POLL_SECONDS: Final = 0.05


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise TmuxValidationError(f"{context} must be a non-empty string")
    if "\x00" in value:
        raise TmuxValidationError(f"{context} must not contain NUL")
    return value


def _identifier(value: object, context: str) -> str:
    text = _text(value, context)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise TmuxValidationError(f"{context} contains unsupported characters")
    return text


def _validate_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(argv, tuple) or not argv:
        raise TmuxValidationError("tmux argv must be a non-empty tuple")
    validated: list[str] = []
    for index, item in enumerate(argv):
        validated.append(_text(item, f"argv[{index}]"))
    return tuple(validated)


def _validate_env(env: Mapping[str, str]) -> tuple[str, ...]:
    if not isinstance(env, Mapping):
        raise TmuxValidationError("tmux environment must be a mapping")
    assignments: list[str] = []
    for name in sorted(env):
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\Z", name) is None
        ):
            raise TmuxValidationError(f"environment name is invalid: {name!r}")
        value = env[name]
        if not isinstance(value, str) or "\x00" in value:
            raise TmuxValidationError(f"environment value is invalid: {name}")
        assignments.append(f"{name}={value}")
    return tuple(assignments)


def _tmux_escape_argument(value: str) -> str:
    """Escape a trailing tmux command separator after exec argv parsing.

    The command-line parser keeps ordinary metacharacters in each direct
    argument, but consumes a semicolon that is an individual or trailing
    token.  One backslash protects that final semicolon and is removed by
    tmux before the child process receives the argument.
    """

    if value.endswith(";"):
        return f"{value[:-1]}\\;"
    return value


def _parse_pid(value: str, context: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise TmuxValidationError(f"tmux returned an invalid {context}") from exc
    if result < 1:
        raise TmuxValidationError(f"tmux returned an invalid {context}")
    return result


def _parse_create_output(stdout: str) -> tuple[str, str, str, int, int]:
    lines = stdout.splitlines()
    if len(lines) != 1:
        raise TmuxValidationError("tmux new-session returned an invalid pane receipt")
    fields = lines[0].split("|")
    if len(fields) != 5:
        raise TmuxValidationError("tmux new-session returned an invalid pane receipt")
    session_id, window_id, pane_id, pane_pid, server_pid = fields
    if _SESSION_ID_RE.fullmatch(session_id) is None:
        raise TmuxValidationError("tmux returned an invalid session ID")
    if _WINDOW_ID_RE.fullmatch(window_id) is None:
        raise TmuxValidationError("tmux returned an invalid window ID")
    if _PANE_ID_RE.fullmatch(pane_id) is None:
        raise TmuxValidationError("tmux returned an invalid pane ID")
    return (
        session_id,
        window_id,
        pane_id,
        _parse_pid(pane_pid, "pane PID"),
        _parse_pid(server_pid, "server PID"),
    )


def _safe_path_identity(path: Path, *, require_socket: bool = False) -> _PathIdentity:
    try:
        info = path.lstat()
    except OSError as exc:
        raise TmuxValidationError(f"tmux control path is unavailable: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise TmuxValidationError(f"tmux control path must not be a symlink: {path}")
    if require_socket and not stat.S_ISSOCK(info.st_mode):
        raise TmuxValidationError(f"tmux control path is not a socket: {path}")
    if info.st_uid != os.geteuid():
        raise TmuxOwnershipError(f"tmux control path is not owned by this user: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise TmuxOwnershipError(f"tmux control path is not private: {path}")
    return _PathIdentity(
        device=info.st_dev,
        inode=info.st_ino,
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
    )


def _same_path_identity(path: Path, identity: _PathIdentity, *, socket: bool) -> bool:
    try:
        current = _safe_path_identity(path, require_socket=socket)
    except (OSError, TmuxError):
        return False
    return (
        current.device == identity.device
        and current.inode == identity.inode
        and current.uid == identity.uid
    )


def _private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise TmuxValidationError(
            f"tmux socket directory is unavailable: {path}"
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise TmuxValidationError(f"tmux socket parent is not a directory: {path}")
    if info.st_uid != os.geteuid():
        raise TmuxOwnershipError(
            f"tmux socket directory is not owned by this user: {path}"
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise TmuxOwnershipError(f"tmux socket directory is not private: {path}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


class TmuxDriver:
    """Create and manage one private, nonce-tagged tmux session."""

    def __init__(
        self,
        executable: str | Path,
        socket_path: str | Path,
        run_nonce: str,
        session_name: str,
    ) -> None:
        self._executable = self._resolve_executable(executable)
        raw_socket = Path(socket_path)
        if not raw_socket.is_absolute():
            raise TmuxValidationError("tmux socket path must be absolute")
        if len(os.fsencode(str(raw_socket))) > _MAX_SOCKET_BYTES:
            raise TmuxValidationError("tmux socket path is too long")
        self._socket_path = raw_socket
        _private_directory(raw_socket.parent)
        if raw_socket.exists() or raw_socket.is_symlink():
            raise TmuxOwnershipError(
                f"tmux socket path already exists; refusing to take it over: {raw_socket}"
            )
        self._run_nonce = _identifier(run_nonce, "run nonce")
        self._session_name = _identifier(session_name, "session name")
        self._receipt: TmuxReceipt | None = None
        self._config_path: Path | None = None
        self._config_identity: _PathIdentity | None = None

    @staticmethod
    def _resolve_executable(value: str | Path) -> Path:
        raw = _text(os.fspath(value), "tmux executable")
        candidate = Path(raw)
        resolved = candidate if candidate.is_absolute() or "/" in raw else None
        if resolved is None:
            found = shutil.which(raw)
            if found is None:
                raise TmuxUnavailableError(
                    f"selected tmux executable is unavailable: {raw}"
                )
            resolved = Path(found)
        try:
            resolved = resolved.expanduser().resolve(strict=True)
        except OSError as exc:
            raise TmuxUnavailableError(
                f"selected tmux executable is unavailable: {raw}"
            ) from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise TmuxUnavailableError(
                f"selected tmux executable is not executable: {resolved}"
            )
        return resolved

    @property
    def executable(self) -> Path:
        return self._executable

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def run_nonce(self) -> str:
        return self._run_nonce

    @property
    def session_name(self) -> str:
        return self._session_name

    def _command_argv(
        self, args: Sequence[str], *, config_path: Path | None = None
    ) -> list[str]:
        return [
            str(self._executable),
            "-S",
            str(self._socket_path),
            "-f",
            str(config_path or "/dev/null"),
            *args,
        ]

    def _run(
        self, args: Sequence[str], *, config_path: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                self._command_argv(args, config_path=config_path),
                capture_output=True,
                text=True,
                check=False,
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise TmuxUnavailableError(
                f"selected tmux executable disappeared: {self._executable}"
            ) from exc
        except PermissionError as exc:
            raise TmuxUnavailableError(
                f"selected tmux executable cannot be executed: {self._executable}"
            ) from exc
        except OSError as exc:
            raise TmuxUnavailableError(
                f"selected tmux executable failed to start: {self._executable}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TmuxError("tmux command timed out") from exc

    @staticmethod
    def _require_success(
        result: subprocess.CompletedProcess[str], context: str
    ) -> None:
        if result.returncode == 0:
            return
        detail = result.stderr.strip()
        if detail:
            raise TmuxError(f"{context} failed: {detail}")
        raise TmuxError(f"{context} failed with exit status {result.returncode}")

    def _new_config(self) -> tuple[Path, _PathIdentity]:
        try:
            fd, raw_path = tempfile.mkstemp(
                prefix=f".{self._session_name}.",
                suffix=".tmux.conf",
                dir=self._socket_path.parent,
            )
        except OSError as exc:
            raise TmuxError("could not create the private tmux config") from exc
        path = Path(raw_path)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(_CONTROL_CONFIG)
            identity = _safe_path_identity(path)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            self._unlink_if_owned(path, None)
            raise
        return path, identity

    def _unlink_if_owned(
        self, path: Path, identity: _PathIdentity | None, *, socket: bool = False
    ) -> bool:
        try:
            _private_directory(path.parent)
        except TmuxError:
            return False
        if identity is not None and not _same_path_identity(
            path, identity, socket=socket
        ):
            return False
        try:
            current = _safe_path_identity(path, require_socket=socket)
        except TmuxError:
            return False
        if identity is not None and (
            current.device != identity.device or current.inode != identity.inode
        ):
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True

    def _receipt_is_for_driver(self, receipt: TmuxReceipt) -> bool:
        if not isinstance(receipt, TmuxReceipt):
            return False
        return (
            receipt.executable == self._executable
            and receipt.socket_path == self._socket_path
            and receipt.run_nonce == self._run_nonce
            and receipt.session_name == self._session_name
            and receipt.config_path == self._config_path
            and self._config_identity == receipt.config_identity
        )

    def _unknown(
        self,
        reason: str,
        *,
        pane_present: bool = False,
        session_present: bool | None = None,
        pane_pid: int | None = None,
        server_pid: int | None = None,
        observed_nonce: str | None = None,
    ) -> TmuxInspection:
        return TmuxInspection(
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

    def _cleanup_partial_create(self, receipt: TmuxReceipt) -> None:
        """Reclaim a just-created pane only while its pre-tag identity matches."""

        try:
            _private_directory(self._socket_path.parent)
            if not _same_path_identity(
                self._socket_path, receipt.socket_identity, socket=True
            ):
                return
            observed = self._run(
                (
                    "display-message",
                    "-p",
                    "-t",
                    receipt.pane_id,
                    _INSPECT_FORMAT,
                ),
                config_path=receipt.config_path,
            )
            fields = observed.stdout.strip().split("|")
            if len(fields) != 9:
                return
            (
                session_id,
                session_name,
                window_id,
                pane_id,
                pane_pid,
                server_pid,
                _dead,
                _status,
                nonce,
            ) = fields
            if (
                session_id != receipt.session_id
                or session_name != receipt.session_name
                or window_id != receipt.window_id
                or pane_id != receipt.pane_id
                or pane_pid != str(receipt.pane_pid)
                or server_pid != str(receipt.server_pid)
                or nonce not in {"", receipt.run_nonce}
            ):
                return
            killed = self._run(
                ("kill-session", "-t", f"={receipt.session_name}"),
                config_path=receipt.config_path,
            )
            if (
                killed.returncode == 0
                and self._session_absent(receipt)
                and self._wait_server_terminated(receipt.server_pid)
            ):
                self._unlink_if_owned(
                    receipt.socket_path, receipt.socket_identity, socket=True
                )
                self._unlink_if_owned(receipt.config_path, receipt.config_identity)
        except (OSError, TmuxError):
            return

    def create(
        self,
        argv: tuple[str, ...],
        cwd: str | Path,
        env: Mapping[str, str],
        title: str,
    ) -> TmuxReceipt:
        """Create one detached pane and return its immutable owned receipt.

        The command is passed to tmux as direct argv after ``--``.  The
        ``env -i`` prefix makes the supplied environment explicit without
        re-parsing shell metacharacters or tmux command separators.
        """

        if self._receipt is not None:
            raise TmuxValidationError("this tmux driver already created a pane")
        command_argv = _validate_argv(argv)
        assignments = _validate_env(env)
        title = _text(title, "tmux window title")
        if len(title) > 256:
            raise TmuxValidationError("tmux window title is too long")
        working_directory = Path(cwd)
        if not working_directory.is_absolute():
            raise TmuxValidationError("tmux working directory must be absolute")
        if not working_directory.is_dir():
            raise TmuxValidationError(
                f"tmux working directory is not a directory: {working_directory}"
            )
        _private_directory(self._socket_path.parent)
        if self._socket_path.exists() or self._socket_path.is_symlink():
            raise TmuxOwnershipError(
                f"tmux socket path already exists; refusing to take it over: "
                f"{self._socket_path}"
            )

        config_path, config_identity = self._new_config()
        self._config_path = config_path
        self._config_identity = config_identity
        command = (
            "new-session",
            "-d",
            "-s",
            _tmux_escape_argument(self._session_name),
            "-n",
            _tmux_escape_argument(title),
            "-c",
            _tmux_escape_argument(str(working_directory)),
            "-P",
            "-F",
            _CREATE_FORMAT,
            "--",
            "env",
            "-i",
            *(_tmux_escape_argument(item) for item in assignments),
            *(_tmux_escape_argument(item) for item in command_argv),
        )
        provisional: TmuxReceipt | None = None
        try:
            created = self._run(command, config_path=config_path)
            self._require_success(created, "tmux new-session")
            session_id, window_id, pane_id, pane_pid, server_pid = _parse_create_output(
                created.stdout
            )
            socket_identity = _safe_path_identity(
                self._socket_path, require_socket=True
            )
            provisional = TmuxReceipt(
                executable=self._executable,
                socket_path=self._socket_path,
                config_path=config_path,
                run_nonce=self._run_nonce,
                session_name=self._session_name,
                session_id=session_id,
                window_id=window_id,
                pane_id=pane_id,
                pane_pid=pane_pid,
                server_pid=server_pid,
                socket_identity=socket_identity,
                config_identity=config_identity,
            )
            set_nonce = self._run(
                (
                    "set-option",
                    "-t",
                    session_id,
                    "@agent_team_run_nonce",
                    self._run_nonce,
                ),
                config_path=config_path,
            )
            self._require_success(set_nonce, "tmux ownership tag")
            receipt = provisional
            self._receipt = receipt
            inspected = self.inspect(receipt)
            if not inspected.identity_verified:
                self._receipt = None
                raise TmuxOwnershipError(
                    inspected.reason or "tmux pane ownership could not be verified"
                )
            return receipt
        except BaseException:
            self._receipt = None
            if provisional is not None:
                self._cleanup_partial_create(provisional)
            self._unlink_if_owned(config_path, config_identity)
            raise

    def inspect(self, receipt: TmuxReceipt) -> TmuxInspection:
        """Observe pane metadata without reading its screen output."""

        if not self._receipt_is_for_driver(receipt):
            return self._unknown("tmux receipt is not owned by this driver")
        try:
            _private_directory(self._socket_path.parent)
        except TmuxError:
            return self._unknown("tmux socket directory ownership is unknown")
        if not _same_path_identity(
            self._socket_path, receipt.socket_identity, socket=True
        ):
            return self._unknown("tmux socket identity changed")
        if not _same_path_identity(
            receipt.config_path, receipt.config_identity, socket=False
        ):
            return self._unknown("tmux config identity changed")
        result = self._run(
            (
                "display-message",
                "-p",
                "-t",
                receipt.pane_id,
                _INSPECT_FORMAT,
            ),
            config_path=receipt.config_path,
        )
        if result.returncode != 0:
            return self._unknown("tmux pane is missing")
        lines = result.stdout.splitlines()
        if len(lines) != 1:
            return self._unknown("tmux returned an invalid pane observation")
        fields = lines[0].split("|")
        if len(fields) != 9:
            return self._unknown("tmux returned an invalid pane observation")
        (
            session_id,
            session_name,
            window_id,
            pane_id,
            pane_pid_text,
            server_pid_text,
            dead_text,
            status_text,
            nonce,
        ) = fields
        try:
            pane_pid = _parse_pid(pane_pid_text, "pane PID")
            server_pid = _parse_pid(server_pid_text, "server PID")
        except TmuxValidationError:
            return self._unknown("tmux returned an invalid pane identity")
        identity_verified = (
            session_id == receipt.session_id
            and session_name == receipt.session_name
            and window_id == receipt.window_id
            and pane_id == receipt.pane_id
            and pane_pid == receipt.pane_pid
            and server_pid == receipt.server_pid
            and nonce == receipt.run_nonce
        )
        if not identity_verified:
            return self._unknown(
                "tmux pane identity changed",
                pane_present=True,
                pane_pid=pane_pid,
                server_pid=server_pid,
                observed_nonce=nonce or None,
            )
        if dead_text not in {"0", "1"}:
            return self._unknown(
                "tmux returned an invalid pane state",
                pane_present=True,
                pane_pid=pane_pid,
                server_pid=server_pid,
                observed_nonce=nonce,
            )
        exit_status: int | None = None
        if status_text not in {"", "-1"}:
            try:
                exit_status = int(status_text)
            except ValueError:
                return self._unknown(
                    "tmux returned an invalid pane exit status",
                    pane_present=True,
                    pane_pid=pane_pid,
                    server_pid=server_pid,
                    observed_nonce=nonce,
                )
            if exit_status < 0:
                return self._unknown(
                    "tmux returned an invalid pane exit status",
                    pane_present=True,
                    pane_pid=pane_pid,
                    server_pid=server_pid,
                    observed_nonce=nonce,
                )
        panes = self._run(
            (
                "list-panes",
                "-t",
                f"={receipt.session_name}",
                "-F",
                _PANES_FORMAT,
            ),
            config_path=receipt.config_path,
        )
        if panes.returncode != 0:
            return self._unknown(
                "tmux session panes could not be verified",
                pane_present=True,
                pane_pid=pane_pid,
                server_pid=server_pid,
                observed_nonce=nonce,
            )
        pane_rows = panes.stdout.splitlines()
        if len(pane_rows) != 1 or pane_rows[0].split("|") != [
            receipt.session_id,
            receipt.window_id,
            receipt.pane_id,
            str(receipt.pane_pid),
        ]:
            return self._unknown(
                "tmux session contains an unowned pane",
                pane_present=True,
                pane_pid=pane_pid,
                server_pid=server_pid,
                observed_nonce=nonce,
            )
        return TmuxInspection(
            running=dead_text == "0",
            exit_status=exit_status,
            identity_verified=True,
            pane_present=True,
            session_present=True,
            pane_pid=pane_pid,
            server_pid=server_pid,
            observed_nonce=nonce,
        )

    def attach_argv(self, receipt: TmuxReceipt) -> tuple[str, ...]:
        """Return a detached, exact-session attach command after revalidation."""

        if not self._receipt_is_for_driver(receipt):
            raise TmuxOwnershipError(
                "cannot attach to a receipt owned by another driver"
            )
        inspected = self.inspect(receipt)
        if not inspected.identity_verified:
            raise TmuxOwnershipError(
                inspected.reason or "cannot attach without verified pane ownership"
            )
        return (
            str(self._executable),
            "-S",
            str(self._socket_path),
            "-f",
            str(receipt.config_path),
            "attach-session",
            "-t",
            f"={receipt.session_name}",
        )

    def _session_absent(self, receipt: TmuxReceipt) -> bool:
        try:
            _private_directory(self._socket_path.parent)
        except TmuxError:
            return False
        if not _same_path_identity(
            self._socket_path, receipt.socket_identity, socket=True
        ):
            return False
        result = self._run(
            ("has-session", "-t", f"={receipt.session_name}"),
            config_path=receipt.config_path,
        )
        if result.returncode == 0:
            return False
        detail = f"{result.stdout}\n{result.stderr}".lower()
        if any(
            marker in detail
            for marker in ("no server", "can't find session", "no such session")
        ):
            return True
        return not _pid_alive(receipt.server_pid)

    def _wait_server_terminated(self, server_pid: int) -> bool:
        deadline = time.monotonic() + _SERVER_EXIT_TIMEOUT_SECONDS
        while _pid_alive(server_pid):
            if time.monotonic() >= deadline:
                return False
            time.sleep(_SERVER_EXIT_POLL_SECONDS)
        return True

    def close(self, receipt: TmuxReceipt) -> TmuxCloseResult:
        """Close only the verified owned session and return termination evidence."""

        if not self._receipt_is_for_driver(receipt):
            return TmuxCloseResult(
                evidence=CloseEvidence.OWNERSHIP_UNPROVEN,
                session_terminated=False,
                server_terminated=False,
                socket_removed=False,
                descendants_stopped=False,
                exit_status=None,
                ownership_verified=False,
                reason="tmux receipt is not owned by this driver",
            )
        inspected = self.inspect(receipt)
        if not inspected.identity_verified:
            return TmuxCloseResult(
                evidence=CloseEvidence.OWNERSHIP_UNPROVEN,
                session_terminated=False,
                server_terminated=False,
                socket_removed=False,
                descendants_stopped=False,
                exit_status=None,
                ownership_verified=False,
                reason=inspected.reason,
            )
        inspected = self.inspect(receipt)
        if not inspected.identity_verified:
            return TmuxCloseResult(
                evidence=CloseEvidence.OWNERSHIP_UNPROVEN,
                session_terminated=False,
                server_terminated=False,
                socket_removed=False,
                descendants_stopped=False,
                exit_status=None,
                ownership_verified=False,
                reason=inspected.reason,
            )
        try:
            killed = self._run(
                ("kill-session", "-t", f"={receipt.session_name}"),
                config_path=receipt.config_path,
            )
            if killed.returncode != 0 or not self._session_absent(receipt):
                reason = killed.stderr.strip() or "tmux session termination is unproven"
                return TmuxCloseResult(
                    evidence=CloseEvidence.TERMINATION_UNPROVEN,
                    session_terminated=False,
                    server_terminated=False,
                    socket_removed=False,
                    descendants_stopped=False,
                    exit_status=inspected.exit_status,
                    ownership_verified=True,
                    reason=reason,
                )
        except TmuxError as exc:
            return TmuxCloseResult(
                evidence=CloseEvidence.TERMINATION_UNPROVEN,
                session_terminated=False,
                server_terminated=False,
                socket_removed=False,
                descendants_stopped=False,
                exit_status=inspected.exit_status,
                ownership_verified=True,
                reason=str(exc),
            )
        server_terminated = self._wait_server_terminated(receipt.server_pid)
        socket_removed = False
        if server_terminated:
            socket_removed = self._unlink_if_owned(
                receipt.socket_path, receipt.socket_identity, socket=True
            )
            self._unlink_if_owned(receipt.config_path, receipt.config_identity)
        evidence = (
            CloseEvidence.SERVER_TERMINATED
            if server_terminated
            else CloseEvidence.SESSION_TERMINATED
        )
        return TmuxCloseResult(
            evidence=evidence,
            session_terminated=True,
            server_terminated=server_terminated,
            socket_removed=socket_removed,
            descendants_stopped=False,
            exit_status=inspected.exit_status,
            ownership_verified=True,
            reason=None if server_terminated else "tmux server remains running",
        )


__all__ = (
    "CloseEvidence",
    "TmuxCloseResult",
    "TmuxDriver",
    "TmuxError",
    "TmuxInspection",
    "TmuxOwnershipError",
    "TmuxReceipt",
    "TmuxUnavailableError",
    "TmuxValidationError",
)
