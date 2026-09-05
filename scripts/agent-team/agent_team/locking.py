"""Private per-team lifecycle reservation shared by runtime backends."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import ModuleType

from .contracts import ErrorCode, RuntimeFailure

_fcntl: ModuleType | None
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None


class _LifecycleReservation:
    """Hold a stable per-team lock outside the removable state root."""

    def __init__(self, state_path: Path, *, create_parent: bool = False) -> None:
        self._state_path = state_path.expanduser().absolute()
        self._state_root = self._state_path.parent
        self._lock_dir = self._state_root.parent / ".agent-team-locks"
        self._lock_path = self._lock_dir / f"{self._state_root.name}.lock"
        self._create_parent = create_parent
        self._fd: int | None = None

    def acquire(self) -> None:
        if os.name == "nt" or _fcntl is None:
            raise RuntimeFailure(
                ErrorCode.INVALID_REQUEST,
                "agent-team lifecycle reservation requires a POSIX runtime",
            )
        state_root = self._state_root
        lock_dir = self._lock_dir
        try:
            if self._create_parent:
                state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            state_root_stat = state_root.lstat()
            if not lock_dir.exists():
                lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_dir_stat = lock_dir.lstat()
        except OSError as exc:
            if not self._create_parent and not state_root.exists():
                raise RuntimeFailure(
                    ErrorCode.TEAM_NOT_RUNNING,
                    "agent-team is not running",
                ) from exc
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team lifecycle reservation is unavailable",
            ) from exc
        if (
            not stat.S_ISDIR(state_root_stat.st_mode)
            or stat.S_ISLNK(state_root_stat.st_mode)
            or state_root_stat.st_uid != os.getuid()
            or stat.S_IMODE(state_root_stat.st_mode) != 0o700
            or not stat.S_ISDIR(lock_dir_stat.st_mode)
            or stat.S_ISLNK(lock_dir_stat.st_mode)
            or lock_dir_stat.st_uid != os.getuid()
            or stat.S_IMODE(lock_dir_stat.st_mode) != 0o700
        ):
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team lifecycle reservation directory is not private",
            )
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self._lock_path, flags, 0o600)
            lock_stat = os.fstat(fd)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.getuid()
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
            ):
                os.close(fd)
                raise RuntimeFailure(
                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                    "agent-team lifecycle reservation is not private",
                )
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(fd)
                raise RuntimeFailure(
                    ErrorCode.TEAM_ALREADY_RUNNING,
                    "agent-team lifecycle is already in progress",
                ) from exc
            except OSError:
                os.close(fd)
                raise
            self._fd = fd
        except RuntimeFailure:
            raise
        except OSError as exc:
            raise RuntimeFailure(
                ErrorCode.BACKEND_PROTOCOL_FAILURE,
                "agent-team lifecycle reservation could not be acquired",
            ) from exc

    def release(self, *, remove_file: bool = False) -> None:
        del remove_file
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)
