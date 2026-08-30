"""Stable writer-marker and exact SQLite sidecar control.

The controller is deliberately smaller than the coordination store.  It owns
only the writer marker lifecycle and the local SQLite checkpoint/sidecar
cleanup protocol; it does not perform recovery, backup/restore, provider
operations, or terminal-resource cleanup.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, NoReturn, Self, SupportsIndex
from urllib.parse import quote

from . import store as _store

WRITER_MARKER_BASENAME: Final[str] = _store.WRITER_MARKER_FILENAME
MARKER_CLEAN_CONTENT: Final[bytes] = _store.WRITER_MARKER_CLEAN_CONTENT
MARKER_PREPARED_CONTENT: Final[bytes] = _store.WRITER_MARKER_PREPARED_CONTENT
WAL_BASENAME: Final[str] = f"{_store.DATABASE_FILENAME}-wal"
SHM_BASENAME: Final[str] = f"{_store.DATABASE_FILENAME}-shm"
JOURNAL_BASENAME: Final[str] = f"{_store.DATABASE_FILENAME}-journal"
SIDECAR_BASENAMES: Final[tuple[str, ...]] = (
    WAL_BASENAME,
    SHM_BASENAME,
    JOURNAL_BASENAME,
)
_ROOT_MUTABLE_NAMES: Final[frozenset[str]] = frozenset(
    {WAL_BASENAME, SHM_BASENAME, JOURNAL_BASENAME}
)
_ROOT_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {
        _store.DATABASE_FILENAME,
        WRITER_MARKER_BASENAME,
        "recovery.ledger",
        *_ROOT_MUTABLE_NAMES,
    }
)
_CLEANUP_EXCEPTION: Final[type[BaseException]] = BaseException

CheckpointMode = Literal["PASSIVE", "FULL", "RESTART", "TRUNCATE"]
CHECKPOINT_MODES: Final[tuple[CheckpointMode, ...]] = (
    "PASSIVE",
    "FULL",
    "RESTART",
    "TRUNCATE",
)
MAX_COPY_BYTES: Final[int] = 256 * 1024 * 1024
_WAL_MAGICS: Final[frozenset[int]] = frozenset({0x377F0682, 0x377F0683})
_WAL_HEADER_BYTES: Final[int] = 32
_WAL_FRAME_HEADER_BYTES: Final[int] = 24
_SHM_PAGE_BYTES: Final[int] = 32 * 1024


CleanupOutcome = Literal["CLEANED", "BLOCKED", "RECOVERY_REQUIRED"]
_CLEANUP_OUTCOMES: Final[tuple[CleanupOutcome, ...]] = (
    "CLEANED",
    "BLOCKED",
    "RECOVERY_REQUIRED",
)
CleanupReason = Literal[
    "CHECKPOINT_BUSY",
    "CHECKPOINT_INCOMPLETE",
    "WAL_PENDING",
    "JOURNAL_PENDING",
    "READER_ACTIVE",
    "DURABILITY_UNKNOWN",
    "IDENTITY_CHANGED",
]
_CLEANUP_REASONS: Final[tuple[CleanupReason, ...]] = (
    "CHECKPOINT_BUSY",
    "CHECKPOINT_INCOMPLETE",
    "WAL_PENDING",
    "JOURNAL_PENDING",
    "READER_ACTIVE",
    "DURABILITY_UNKNOWN",
    "IDENTITY_CHANGED",
)


class WalSidecarError(_store.StoreError):
    """Base class for explicit marker/checkpoint/sidecar failures."""


class WalSidecarClosedError(WalSidecarError):
    """A quiescence session was used after its guards were released."""


class WalSidecarBusyError(_store.StoreBusyError, WalSidecarError):
    """A cooperating writer/reader still holds an exclusive/shared guard."""


class WalSidecarUnsafeError(_store.StoreUnavailableError, WalSidecarError):
    """The state root, marker, database, or exact sidecar is unsafe."""


class WalSidecarRecoveryRequiredError(WalSidecarError):
    """Identity or directory durability became uncertain during cleanup."""


class _WalPendingError(WalSidecarRecoveryRequiredError):
    """A non-zero WAL was present before a cleanup-owned SQLite phase."""


@dataclass(frozen=True, slots=True)
class CheckpointRequest:
    """One exact SQLite checkpoint mode; no implicit mode fallback exists."""

    mode: CheckpointMode

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode not in CHECKPOINT_MODES:
            raise ValueError("checkpoint mode is unsupported")


@dataclass(frozen=True, slots=True)
class DatabaseCopyTarget:
    """Root-relative basename for a controller-created copy target."""

    name: str

    def __post_init__(self) -> None:
        if type(self.name) is not str:
            raise ValueError("database copy target must be one non-reserved basename")
        try:
            name_bytes = self.name.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "database copy target must be one non-reserved basename"
            ) from exc
        if (
            not self.name
            or self.name in {".", ".."}
            or "/" in self.name
            or "\\" in self.name
            or "\x00" in self.name
            or len(name_bytes) > 255
            or self.name in _ROOT_RESERVED_NAMES
        ):
            raise ValueError("database copy target must be one non-reserved basename")


def _canonical_checkpoint_request(value: object) -> CheckpointRequest:
    if type(value) is not CheckpointRequest:
        raise TypeError("request must be a CheckpointRequest")
    try:
        mode = value.mode
    except AttributeError as exc:
        raise ValueError("checkpoint mode is unsupported") from exc
    return CheckpointRequest(mode)


def _canonical_copy_target(value: object) -> DatabaseCopyTarget:
    if type(value) is not DatabaseCopyTarget:
        raise TypeError("target must be a DatabaseCopyTarget")
    try:
        name = value.name
    except AttributeError as exc:
        raise ValueError(
            "database copy target must be one non-reserved basename"
        ) from exc
    return DatabaseCopyTarget(name)


@dataclass(frozen=True, slots=True)
class DatabaseCopyResult:
    """Result of one SQLite backup API invocation with a verified image digest."""

    checkpoint: CheckpointResult
    target: DatabaseCopyTarget
    source_identity: tuple[int, int]
    target_identity: tuple[int, int]
    size: int
    digest: str

    def __post_init__(self) -> None:
        if type(self.target) is not DatabaseCopyTarget:
            raise TypeError("target is invalid")
        if type(self.checkpoint) is not CheckpointResult:
            raise TypeError("checkpoint is invalid")
        for name, value in (
            ("source_identity", self.source_identity),
            ("target_identity", self.target_identity),
        ):
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or any(type(item) is not int or item < 0 for item in value)
            ):
                raise ValueError(f"{name} is invalid")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("size is invalid")
        if type(self.digest) is not str or not self.digest.startswith("sha256:"):
            raise ValueError("digest is invalid")
        digest_hex = self.digest.removeprefix("sha256:")
        if len(digest_hex) != 64 or any(
            char not in "0123456789abcdef" for char in digest_hex
        ):
            raise ValueError("digest is invalid")
        try:
            bytes.fromhex(digest_hex)
        except ValueError as exc:
            raise ValueError("digest is invalid") from exc


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    """The exact three integers returned by ``wal_checkpoint``."""

    request: CheckpointRequest
    busy: int
    log: int
    checkpointed: int

    def __post_init__(self) -> None:
        if type(self.request) is not CheckpointRequest:
            raise TypeError("request is invalid")
        for name, value in (
            ("busy", self.busy),
            ("log", self.log),
            ("checkpointed", self.checkpointed),
        ):
            if type(value) is not int or value < 0 or value > 2**63 - 1:
                raise ValueError(f"{name} is invalid")

    def _validated_values(self) -> tuple[int, int, int]:
        try:
            request = self.request
            busy = self.busy
            log = self.log
            checkpointed = self.checkpointed
        except AttributeError as exc:
            raise WalSidecarClosedError("checkpoint result is uninitialized") from exc
        if type(request) is not CheckpointRequest:
            raise TypeError("checkpoint result request is invalid")
        values = (busy, log, checkpointed)
        if any(type(value) is not int for value in values):
            raise TypeError("checkpoint result values are invalid")
        return values

    @property
    def safe(self) -> bool:
        """Whether SQLite reported no busy reader and no remaining frames."""

        busy, log, checkpointed = self._validated_values()
        return busy == 0 and log == checkpointed

    @property
    def values(self) -> tuple[int, int, int]:
        """Return SQLite's checkpoint triple in its canonical order."""

        return self._validated_values()

    def __iter__(self) -> Iterator[int]:
        return iter(self.values)


@dataclass(frozen=True, slots=True)
class SidecarCleanupResult:
    """Typed cleanup observation with exact names that were removed."""

    outcome: CleanupOutcome
    request: CheckpointRequest
    checkpoint: CheckpointResult | None
    removed: tuple[str, ...]
    reason: CleanupReason | None = None

    def __post_init__(self) -> None:
        if type(self.outcome) is not str or self.outcome not in _CLEANUP_OUTCOMES:
            raise TypeError("outcome is invalid")
        if type(self.request) is not CheckpointRequest:
            raise TypeError("request is invalid")
        if self.checkpoint is not None:
            if type(self.checkpoint) is not CheckpointResult:
                raise TypeError("checkpoint is invalid")
            if self.checkpoint.request != self.request:
                raise ValueError("checkpoint request does not match cleanup request")
        if not isinstance(self.removed, tuple):
            raise TypeError("removed must be a tuple")
        if any(name not in SIDECAR_BASENAMES for name in self.removed):
            raise ValueError("removed contains an unknown sidecar")
        if len(self.removed) != len(set(self.removed)):
            raise ValueError("removed contains a duplicate sidecar")
        if tuple(name for name in SIDECAR_BASENAMES if name in self.removed) != (
            self.removed
        ):
            raise ValueError("removed is not in canonical order")
        if self.outcome == "CLEANED" and self.reason is not None:
            raise ValueError("cleaned result cannot have a reason")
        if self.outcome != "CLEANED" and self.reason is None:
            raise ValueError("blocked result requires a reason")
        if self.outcome == "CLEANED" and self.checkpoint is None:
            raise ValueError("cleaned result requires a checkpoint")
        if self.checkpoint is None and self.reason not in {
            "JOURNAL_PENDING",
            "WAL_PENDING",
            "READER_ACTIVE",
        }:
            raise ValueError("only a pending journal may precede checkpoint")
        if self.reason is not None and (
            type(self.reason) is not str or self.reason not in _CLEANUP_REASONS
        ):
            raise ValueError("cleanup reason is unsupported")


@dataclass(slots=True)
class _Resources:
    root: Path
    root_fd: int
    parent_fd: int
    gate_fd: int
    marker_fd: int
    database_fd: int
    parent_identity: tuple[int, int]
    root_identity: tuple[int, int]
    gate_identity: tuple[int, int]
    marker_identity: tuple[int, int]
    database_identity: tuple[int, int]
    root_signature: tuple[int, ...]
    gate_signature: tuple[int, ...]
    marker_signature: tuple[int, ...]
    database_signature: tuple[int, ...]
    marker_state: str
    root_names: frozenset[str]
    sidecar_signatures: dict[str, tuple[int, ...]]
    preexisting_wal_or_journal: bool
    allowed_root_names: frozenset[str] = field(default_factory=frozenset)
    connection: sqlite3.Connection | None = None
    _orphan_connections: list[sqlite3.Connection] = field(
        default_factory=list, init=False, repr=False
    )
    _orphan_fds: list[tuple[int, tuple[int, int] | None, str]] = field(
        default_factory=list, init=False, repr=False
    )
    _closed_fds: set[str] = field(default_factory=set, init=False, repr=False)
    _failed_fds: set[str] = field(default_factory=set, init=False, repr=False)

    def close(self) -> None:
        first_error: BaseException | None = None

        def attempt(action: Callable[[], None]) -> None:
            nonlocal first_error
            try:
                action()
            except _CLEANUP_EXCEPTION as error:
                if first_error is None:
                    first_error = error

        connection = self.connection
        if connection is not None:
            try:
                connection.close()
            except _CLEANUP_EXCEPTION:
                try:
                    connection.close()
                except _CLEANUP_EXCEPTION as retry_error:
                    if first_error is None:
                        first_error = retry_error
            else:
                self.connection = None
            if self.connection is connection and first_error is None:
                self.connection = None

        def close_fd(
            name: str,
            fd: int,
            expected_identity: tuple[int, int],
            *,
            unlock: bool,
        ) -> None:
            if name in self._closed_fds:
                return
            if name in self._failed_fds:
                try:
                    metadata = os.fstat(fd)
                except OSError as error:
                    if error.errno == errno.EBADF:
                        self._closed_fds.add(name)
                        self._failed_fds.discard(name)
                        return
                    raise WalSidecarRecoveryRequiredError(
                        f"{name} descriptor status is unknown"
                    ) from error
                if _identity(metadata) != expected_identity:
                    self._closed_fds.add(name)
                    self._failed_fds.discard(name)
                    raise WalSidecarRecoveryRequiredError(
                        f"{name} descriptor was reused"
                    )
            close_error: BaseException | None = None
            if unlock:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except _CLEANUP_EXCEPTION as error:
                    close_error = error
            try:
                os.close(fd)
            except _CLEANUP_EXCEPTION as error:
                self._failed_fds.add(name)
                if close_error is None:
                    close_error = error
            else:
                self._closed_fds.add(name)
                self._failed_fds.discard(name)
            if close_error is not None:
                raise close_error

        remaining_connections: list[sqlite3.Connection] = []
        for orphan_connection in self._orphan_connections:
            try:
                _close_temporary_connection(
                    orphan_connection,
                    "temporary SQLite connection",
                )
            except _CLEANUP_EXCEPTION as error:
                remaining_connections.append(orphan_connection)
                if first_error is None:
                    first_error = error
        self._orphan_connections = remaining_connections

        remaining_fds: list[tuple[int, tuple[int, int] | None, str]] = []
        for orphan_fd, expected_identity, label in self._orphan_fds:
            try:
                metadata = os.fstat(orphan_fd)
            except OSError as error:
                if error.errno == errno.EBADF:
                    continue
                remaining_fds.append((orphan_fd, expected_identity, label))
                if first_error is None:
                    first_error = WalSidecarRecoveryRequiredError(
                        f"{label} descriptor status is unknown"
                    )
                continue
            if (
                expected_identity is not None
                and _identity(metadata) != expected_identity
            ):
                if first_error is None:
                    first_error = WalSidecarRecoveryRequiredError(
                        f"{label} descriptor was reused"
                    )
                continue
            try:
                os.close(orphan_fd)
            except _CLEANUP_EXCEPTION:
                try:
                    retry_metadata = os.fstat(orphan_fd)
                    if (
                        expected_identity is not None
                        and _identity(retry_metadata) != expected_identity
                    ):
                        if first_error is None:
                            first_error = WalSidecarRecoveryRequiredError(
                                f"{label} descriptor was reused"
                            )
                        continue
                    os.close(orphan_fd)
                except OSError as retry_error:
                    if retry_error.errno == errno.EBADF:
                        continue
                    remaining_fds.append((orphan_fd, expected_identity, label))
                    if first_error is None:
                        first_error = WalSidecarRecoveryRequiredError(
                            f"{label} cannot be closed"
                        )
                    continue
                except _CLEANUP_EXCEPTION:
                    remaining_fds.append((orphan_fd, expected_identity, label))
                    if first_error is None:
                        first_error = WalSidecarRecoveryRequiredError(
                            f"{label} cannot be closed"
                        )
                    continue
        self._orphan_fds = remaining_fds

        attempt(
            lambda: close_fd(
                "marker",
                self.marker_fd,
                self.marker_identity,
                unlock=True,
            )
        )
        attempt(
            lambda: close_fd(
                "gate",
                self.gate_fd,
                self.gate_identity,
                unlock=True,
            )
        )
        attempt(
            lambda: close_fd(
                "database",
                self.database_fd,
                self.database_identity,
                unlock=False,
            )
        )
        attempt(
            lambda: close_fd(
                "root",
                self.root_fd,
                self.root_identity,
                unlock=False,
            )
        )
        attempt(
            lambda: close_fd(
                "parent",
                self.parent_fd,
                self.parent_identity,
                unlock=False,
            )
        )
        if first_error is not None:
            if isinstance(first_error, WalSidecarError):
                raise first_error
            raise WalSidecarRecoveryRequiredError(
                "quiescence resources cannot be closed"
            ) from first_error


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        stat.S_IFMT(metadata.st_mode),
        metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _sidecar_recreation_is_safe(
    name: str,
    previous: tuple[int, ...],
    current: tuple[int, ...],
) -> bool:
    """Allow SQLite's zero-frame open churn, never nonzero replacement."""

    if previous[4:6] == current[4:6]:
        return True
    if previous[6] != 0 or current[6] != 0:
        return False
    return name in {WAL_BASENAME, SHM_BASENAME, JOURNAL_BASENAME}


def _security_signature(metadata: os.stat_result) -> tuple[int, ...]:
    """Return metadata that remains stable while database contents change."""

    return (
        stat.S_IFMT(metadata.st_mode),
        metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_dev,
        metadata.st_ino,
    )


def _directory_security_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        stat.S_IFMT(metadata.st_mode),
        metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_dev,
        metadata.st_ino,
    )


def _validate_regular(metadata: os.stat_result, label: str) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise WalSidecarUnsafeError(f"{label} is unsafe")


def _open_file_flags(*, writable: bool) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if nofollow == 0 or nonblock == 0:
        raise WalSidecarUnsafeError("secure non-blocking no-follow open is unavailable")
    return os.O_CLOEXEC | nofollow | nonblock | (os.O_RDWR if writable else os.O_RDONLY)


def _open_directory_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if directory == 0 or nofollow == 0:
        raise WalSidecarUnsafeError("secure directory open is unavailable")
    return os.O_CLOEXEC | nofollow | os.O_RDONLY | directory


def _lock_nonblocking(fd: int, *, exclusive: bool, timeout_ms: int, label: str) -> None:
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline_ns = time.monotonic_ns() + timeout_ms * 1_000_000
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise WalSidecarUnsafeError(f"{label} cannot be locked") from exc
            if time.monotonic_ns() >= deadline_ns:
                raise WalSidecarBusyError(f"{label} is busy") from exc
            time.sleep(min(0.005, (deadline_ns - time.monotonic_ns()) / 1e9))
        else:
            return


def _close_temporary_fd(
    fd: int,
    expected_identity: tuple[int, int] | None,
    label: str,
) -> None:
    try:
        os.close(fd)
    except _CLEANUP_EXCEPTION as first_error:
        try:
            metadata = os.fstat(fd)
        except OSError as status_error:
            if status_error.errno == errno.EBADF:
                raise WalSidecarRecoveryRequiredError(
                    f"{label} close status is unknown"
                ) from first_error
            raise WalSidecarRecoveryRequiredError(
                f"{label} descriptor status is unknown"
            ) from status_error
        if expected_identity is not None and _identity(metadata) != expected_identity:
            raise WalSidecarRecoveryRequiredError(
                f"{label} descriptor was reused"
            ) from first_error
        try:
            os.close(fd)
        except _CLEANUP_EXCEPTION as retry_error:
            raise WalSidecarRecoveryRequiredError(
                f"{label} cannot be closed"
            ) from retry_error
        raise WalSidecarRecoveryRequiredError(
            f"{label} close failed before retry"
        ) from first_error


def _retain_failed_fd(
    resources: _Resources,
    fd: int,
    expected_identity: tuple[int, int] | None,
    label: str,
) -> None:
    """Keep a still-open temporary fd reachable for a later safe close."""

    try:
        metadata = os.fstat(fd)
    except OSError as error:
        if error.errno == errno.EBADF:
            return
        raise WalSidecarRecoveryRequiredError(
            f"{label} descriptor status is unknown"
        ) from error
    if expected_identity is not None and _identity(metadata) != expected_identity:
        raise WalSidecarRecoveryRequiredError(f"{label} descriptor was reused")
    if not any(existing_fd == fd for existing_fd, _, _ in resources._orphan_fds):
        resources._orphan_fds.append((fd, expected_identity, label))


def _close_temporary_connection(
    connection: sqlite3.Connection,
    label: str,
) -> None:
    try:
        connection.close()
    except _CLEANUP_EXCEPTION:
        try:
            connection.close()
        except _CLEANUP_EXCEPTION as retry_error:
            raise WalSidecarRecoveryRequiredError(
                f"{label} cannot be closed"
            ) from retry_error


def _run_cleanup_actions(actions: tuple[Callable[[], None], ...], label: str) -> None:
    first_error: BaseException | None = None
    for action in actions:
        try:
            action()
        except _CLEANUP_EXCEPTION as error:
            if first_error is None:
                first_error = error
    if first_error is None:
        return
    if isinstance(first_error, WalSidecarError):
        raise first_error
    raise WalSidecarRecoveryRequiredError(f"{label} cleanup failed") from first_error


@dataclass(frozen=True, slots=True, init=False)
class QuiescenceSession:
    """Opaque exclusive guard shared by checkpoint/replace workflows.

    The constructor is intentionally private.  The controller is the sole
    issuer, so callers can retain quiescence without receiving a raw file
    descriptor or pathname lock.
    """

    _controller: WalSidecarController
    _resources: _Resources
    _token: object
    _closed: bool

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("QuiescenceSession instances are controller-issued")

    @classmethod
    def _issue(
        cls,
        controller: WalSidecarController,
        resources: _Resources,
    ) -> QuiescenceSession:
        token = object()
        instance = object.__new__(cls)
        object.__setattr__(instance, "_controller", controller)
        object.__setattr__(instance, "_resources", resources)
        object.__setattr__(instance, "_token", token)
        object.__setattr__(instance, "_closed", False)
        controller._active_sessions[token] = instance
        return instance

    def _provenance(self) -> tuple[WalSidecarController, _Resources, object, bool]:
        try:
            controller = self._controller
            resources = self._resources
            token = self._token
            closed = self._closed
        except AttributeError as exc:
            raise WalSidecarClosedError("quiescence session is uninitialized") from exc
        try:
            owner = controller._active_sessions.get(token)
        except (AttributeError, TypeError) as exc:
            raise WalSidecarClosedError(
                "quiescence session provenance is invalid"
            ) from exc
        if owner is not self and (not closed or owner is not None):
            raise WalSidecarClosedError("quiescence session provenance is invalid")
        return controller, resources, token, closed

    def _assert_active(self) -> None:
        _, _, _, closed = self._provenance()
        if closed:
            raise WalSidecarClosedError("quiescence session is closed")

    def __copy__(self) -> NoReturn:
        raise TypeError("QuiescenceSession instances cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("QuiescenceSession instances cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("QuiescenceSession instances cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        raise TypeError("QuiescenceSession instances cannot be pickled")

    def __repr__(self) -> str:
        _, _, _, closed = self._provenance()
        return f"QuiescenceSession(active={not closed!r})"

    def assert_identity(self) -> None:
        """Revalidate the held root, gate, marker, and database identities."""

        self._assert_active()
        self._controller._assert_resources(self._resources)

    def checkpoint(self, request: CheckpointRequest) -> CheckpointResult:
        """Run one checkpoint while retaining the exclusive guards."""

        request = _canonical_checkpoint_request(request)
        self._assert_active()
        return self._controller._checkpoint_for_session(self._resources, request)

    def _rebind_database(self) -> None:
        """Refresh the held database descriptor after an authorized replace."""

        self._assert_active()
        self._controller._rebind_database(self._resources)

    def copy_database_to(
        self,
        request: CheckpointRequest,
        target: DatabaseCopyTarget,
    ) -> DatabaseCopyResult:
        """Copy the held database through one controller-owned backup call."""

        request = _canonical_checkpoint_request(request)
        target = _canonical_copy_target(target)
        self._assert_active()
        return self._controller._copy_database_to(self._resources, request, target)

    def cleanup(self, request: CheckpointRequest) -> SidecarCleanupResult:
        """Clean exact sidecars while retaining the exclusive guards."""

        request = _canonical_checkpoint_request(request)
        self._assert_active()
        return self._controller._cleanup_locked(self._resources, request)

    def close(self) -> None:
        """Release exclusive guards; the stable marker itself remains."""

        controller, resources, _, closed = self._provenance()
        if closed:
            if controller._active_sessions.get(self._token) is self:
                resources.close()
                controller._active_sessions.pop(self._token, None)
            return
        object.__setattr__(self, "_closed", True)
        resources.close()
        controller._active_sessions.pop(self._token, None)

    def __enter__(self) -> Self:
        self._assert_active()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        self.close()


class WalSidecarController:
    """Checkpoint SQLite and remove only its exact, safe sidecars."""

    __slots__ = ("_active_sessions", "_busy_timeout_ms", "_state_root")

    def __init__(
        self,
        state_root: Path,
        *,
        busy_timeout_ms: int = _store.DEFAULT_BUSY_TIMEOUT_MS,
        marker_name: str = WRITER_MARKER_BASENAME,
    ) -> None:
        if (
            type(busy_timeout_ms) is not int
            or not 0 <= busy_timeout_ms <= _store.MAX_BUSY_TIMEOUT_MS
        ):
            raise ValueError(
                f"busy_timeout_ms must be between 0 and {_store.MAX_BUSY_TIMEOUT_MS}"
            )
        if type(marker_name) is not str or marker_name != WRITER_MARKER_BASENAME:
            raise ValueError("marker_name is not canonical")
        self._state_root = _store._coerce_state_root(state_root)
        self._busy_timeout_ms = busy_timeout_ms
        self._active_sessions: dict[object, QuiescenceSession] = {}

    @property
    def state_root(self) -> Path:
        self._assert_initialized()
        return self._state_root

    @property
    def busy_timeout_ms(self) -> int:
        self._assert_initialized()
        return self._busy_timeout_ms

    @property
    def marker_name(self) -> str:
        self._assert_initialized()
        return WRITER_MARKER_BASENAME

    def __enter__(self) -> Self:
        self._assert_initialized()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback

    def _fault(self, point: str) -> None:
        """Deterministic fault-injection seam; production implementation is a no-op."""

        del point

    def _assert_initialized(self) -> None:
        try:
            _ = (self._state_root, self._busy_timeout_ms, self._active_sessions)
        except AttributeError as exc:
            raise WalSidecarClosedError(
                "WAL sidecar controller is uninitialized"
            ) from exc

    @contextmanager
    def _resources(self) -> Iterator[_Resources]:
        resources = self._open_resources()
        try:
            yield resources
        finally:
            resources.close()

    def hold_quiescence(self) -> QuiescenceSession:
        """Acquire an opaque exclusive lifetime/marker session.

        The returned session owns the guards until ``close`` or context-manager
        exit.  It is the only supported way for a caller such as backup/restore
        to span multiple local phases without reimplementing lock handling.
        """

        self._assert_initialized()
        resources = self._open_resources()
        try:
            return QuiescenceSession._issue(
                self,
                resources,
            )
        except _CLEANUP_EXCEPTION:
            resources.close()
            raise

    def _open_resources(self) -> _Resources:
        root_fd: int | None = None
        parent_fd: int | None = None
        gate_fd: int | None = None
        marker_fd: int | None = None
        database_fd: int | None = None
        root_identity: tuple[int, int] | None = None
        gate_identity: tuple[int, int] | None = None
        marker_identity: tuple[int, int] | None = None
        database_identity: tuple[int, int] | None = None
        try:
            try:
                root_fd = _store._open_state_root(self.state_root)
                root_metadata = os.fstat(root_fd)
                root_identity = _identity(root_metadata)
                path_metadata = os.stat(self.state_root, follow_symlinks=False)
                if _identity(path_metadata) != root_identity:
                    raise WalSidecarRecoveryRequiredError(
                        "state root changed while opening"
                    )
                self._fault("after_root_lstat")
            except WalSidecarError:
                raise
            except _store.StoreError as exc:
                raise WalSidecarUnsafeError("state root cannot be opened") from exc
            parent_fd = os.open("..", _open_directory_flags(), dir_fd=root_fd)
            _store._validate_directory_fd(parent_fd, state_root=False)

            gate_name = _store.LIFETIME_GATE_FILENAME
            try:
                gate_before = os.stat(
                    gate_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                raise WalSidecarUnsafeError(
                    "coordination lifetime gate is missing"
                ) from exc
            _validate_regular(gate_before, "coordination lifetime gate")
            self._fault("after_gate_lstat")
            gate_fd = os.open(
                gate_name,
                _open_file_flags(writable=True),
                dir_fd=parent_fd,
            )
            gate_metadata = os.fstat(gate_fd)
            gate_identity = _identity(gate_metadata)
            _validate_regular(gate_metadata, "coordination lifetime gate")
            if _identity(gate_metadata) != _identity(gate_before):
                raise WalSidecarRecoveryRequiredError(
                    "lifetime gate changed while opening"
                )
            self._fault("before_lifetime_lock")
            _lock_nonblocking(
                gate_fd,
                exclusive=True,
                timeout_ms=self.busy_timeout_ms,
                label="coordination lifetime gate",
            )
            self._fault("after_lifetime_lock")
            gate_after = os.stat(gate_name, dir_fd=parent_fd, follow_symlinks=False)
            if _identity(gate_after) != _identity(gate_metadata):
                raise WalSidecarRecoveryRequiredError(
                    "lifetime gate changed while locked"
                )

            marker_before = os.stat(
                self.marker_name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            _validate_regular(marker_before, "writer marker")
            self._fault("after_marker_lstat")
            marker_fd = os.open(
                self.marker_name,
                _open_file_flags(writable=True),
                dir_fd=root_fd,
            )
            marker_metadata = os.fstat(marker_fd)
            marker_identity = _identity(marker_metadata)
            _validate_regular(marker_metadata, "writer marker")
            if _identity(marker_metadata) != _identity(marker_before):
                raise WalSidecarRecoveryRequiredError(
                    "writer marker changed while opening"
                )
            self._fault("before_marker_lock")
            _lock_nonblocking(
                marker_fd,
                exclusive=True,
                timeout_ms=self.busy_timeout_ms,
                label="writer marker",
            )
            self._fault("after_marker_lock")
            marker_after = os.stat(
                self.marker_name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if _identity(marker_after) != _identity(marker_metadata):
                raise WalSidecarRecoveryRequiredError(
                    "writer marker changed while locked"
                )
            marker_state = _store._read_writer_marker_state(marker_fd)
            if marker_state != _store.WRITER_MARKER_CLEAN_STATE:
                raise WalSidecarRecoveryRequiredError(
                    "writer marker records an incomplete cleanup"
                )

            database_before = os.stat(
                _store.DATABASE_FILENAME,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            _validate_regular(database_before, "coordination database")
            self._fault("after_db_lstat")
            database_fd = os.open(
                _store.DATABASE_FILENAME,
                _open_file_flags(writable=True),
                dir_fd=root_fd,
            )
            database_metadata = os.fstat(database_fd)
            database_identity = _identity(database_metadata)
            _validate_regular(database_metadata, "coordination database")
            database_after = os.stat(
                _store.DATABASE_FILENAME,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if _identity(database_before) != _identity(database_metadata) or _identity(
                database_after
            ) != _identity(database_metadata):
                raise WalSidecarRecoveryRequiredError(
                    "coordination database changed while opening"
                )
            sidecar_signatures = self._sidecar_signatures(root_fd)
            root_names = frozenset(os.listdir(root_fd))
            root_metadata = os.fstat(root_fd)
            root_path_metadata = os.stat(self.state_root, follow_symlinks=False)
            if _identity(root_metadata) != _identity(root_path_metadata):
                raise WalSidecarRecoveryRequiredError(
                    "state root changed while opening"
                )
            resources = _Resources(
                root=self.state_root,
                root_fd=root_fd,
                parent_fd=parent_fd,
                gate_fd=gate_fd,
                marker_fd=marker_fd,
                database_fd=database_fd,
                parent_identity=_identity(os.fstat(parent_fd)),
                root_identity=root_identity,
                gate_identity=_identity(gate_metadata),
                marker_identity=_identity(marker_metadata),
                database_identity=_identity(database_metadata),
                root_signature=_directory_security_signature(root_metadata),
                gate_signature=_security_signature(gate_metadata),
                marker_signature=_security_signature(marker_metadata),
                database_signature=_security_signature(database_metadata),
                marker_state=marker_state,
                root_names=root_names,
                sidecar_signatures=sidecar_signatures,
                preexisting_wal_or_journal=bool(
                    {WAL_BASENAME, JOURNAL_BASENAME}.intersection(sidecar_signatures)
                ),
            )
            self._assert_resources(resources)
            root_fd = parent_fd = gate_fd = marker_fd = database_fd = None
            return resources
        except (WalSidecarError, _store.StoreError):
            raise
        except FileNotFoundError as exc:
            raise WalSidecarUnsafeError(
                "required coordination file is missing"
            ) from exc
        except OSError as exc:
            raise WalSidecarUnsafeError("coordination files cannot be opened") from exc
        finally:
            cleanup_actions: list[Callable[[], None]] = []
            for fd, identity, label, unlock in (
                (marker_fd, marker_identity, "writer marker", True),
                (gate_fd, gate_identity, "coordination lifetime gate", True),
                (database_fd, database_identity, "coordination database", False),
                (root_fd, root_identity, "state root", False),
                (parent_fd, None, "state parent", False),
            ):
                if fd is None:
                    continue
                fd_value = fd
                identity_value = identity
                label_value = label
                unlock_value = unlock

                def close_open_fd(
                    fd: int = fd_value,
                    identity: tuple[int, int] | None = identity_value,
                    label: str = label_value,
                    unlock: bool = unlock_value,
                ) -> None:
                    first_error: BaseException | None = None
                    if unlock:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        except _CLEANUP_EXCEPTION as error:
                            first_error = error
                    try:
                        _close_temporary_fd(fd, identity, label)
                    except _CLEANUP_EXCEPTION as error:
                        if first_error is None:
                            first_error = error
                    if first_error is not None:
                        raise first_error

                cleanup_actions.append(close_open_fd)
            _run_cleanup_actions(
                tuple(cleanup_actions), "opening coordination resources"
            )

    def _sidecar_signatures(self, root_fd: int) -> dict[str, tuple[int, ...]]:
        signatures: dict[str, tuple[int, ...]] = {}
        for name in SIDECAR_BASENAMES:
            try:
                metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise WalSidecarUnsafeError(f"{name} cannot be inspected") from exc
            _validate_regular(metadata, name)
            self._fault("after_sidecar_lstat")
            signatures[name] = _signature(metadata)
        return signatures

    def _copy_target_sidecar_signatures(
        self,
        root_fd: int,
        target: DatabaseCopyTarget,
    ) -> dict[str, tuple[int, ...]]:
        signatures: dict[str, tuple[int, ...]] = {}
        for suffix in ("-wal", "-shm", "-journal"):
            name = f"{target.name}{suffix}"
            try:
                metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise WalSidecarRecoveryRequiredError(
                    f"{name} cannot be inspected"
                ) from exc
            _validate_regular(metadata, name)
            signatures[name] = _signature(metadata)
        return signatures

    def _journal_is_nonempty(self, resources: _Resources) -> bool:
        signatures = self._sidecar_signatures(resources.root_fd)
        journal = signatures.get(JOURNAL_BASENAME)
        return journal is not None and journal[6] > 0

    def _preflight_sidecars(
        self,
        resources: _Resources,
        signatures: dict[str, tuple[int, ...]] | None = None,
        *,
        reader_hint: bool | None = None,
    ) -> dict[str, tuple[int, ...]]:
        current = (
            self._sidecar_signatures(resources.root_fd)
            if signatures is None
            else signatures
        )
        journal = current.get(JOURNAL_BASENAME)
        if journal is not None and journal[6] > 0:
            raise WalSidecarBusyError("SQLite rollback journal is pending")
        wal = current.get(WAL_BASENAME)
        if wal is not None and wal[6] > 0:
            if wal[6] < _WAL_HEADER_BYTES:
                raise WalSidecarUnsafeError("SQLite WAL header is incomplete")
            wal_fd: int | None = None
            try:
                wal_fd = os.open(
                    WAL_BASENAME,
                    _open_file_flags(writable=False),
                    dir_fd=resources.root_fd,
                )
                wal_metadata = os.fstat(wal_fd)
                wal_path = os.stat(
                    WAL_BASENAME,
                    dir_fd=resources.root_fd,
                    follow_symlinks=False,
                )
                if _signature(wal_metadata) != wal or _signature(wal_path) != wal:
                    raise WalSidecarRecoveryRequiredError(
                        "SQLite WAL changed during preflight"
                    )
                header = os.pread(wal_fd, _WAL_HEADER_BYTES, 0)
            except OSError as exc:
                raise WalSidecarRecoveryRequiredError(
                    "SQLite WAL cannot be read during preflight"
                ) from exc
            finally:
                if wal_fd is not None:
                    _close_temporary_fd(
                        wal_fd,
                        (wal[4], wal[5]),
                        WAL_BASENAME,
                    )
            if len(header) != _WAL_HEADER_BYTES:
                raise WalSidecarUnsafeError("SQLite WAL header is incomplete")
            magic = int.from_bytes(header[0:4], "big")
            page_size = int.from_bytes(header[8:12], "big")
            frame_size = page_size + _WAL_FRAME_HEADER_BYTES
            if (
                magic not in _WAL_MAGICS
                or page_size < 512
                or page_size > 65_536
                or page_size & (page_size - 1)
                or (wal[6] - _WAL_HEADER_BYTES) % frame_size != 0
            ):
                raise WalSidecarUnsafeError("SQLite WAL structure is invalid")
        shm = current.get(SHM_BASENAME)
        if shm is not None and shm[6] > 0:
            if shm[6] < _SHM_PAGE_BYTES or shm[6] % _SHM_PAGE_BYTES != 0:
                raise WalSidecarUnsafeError("SQLite SHM structure is invalid")
            if (wal is None or wal[6] == 0) and (
                SHM_BASENAME in resources.sidecar_signatures
                if reader_hint is None
                else reader_hint
            ):
                raise WalSidecarBusyError("SQLite SHM indicates an active reader")
        return current

    def _refresh_sidecars_after_connection_close(self, resources: _Resources) -> None:
        resources.sidecar_signatures = self._sidecar_signatures(resources.root_fd)
        self._refresh_root_observation(resources)

    @staticmethod
    def _journal_pending_result(request: CheckpointRequest) -> SidecarCleanupResult:
        return SidecarCleanupResult(
            outcome="BLOCKED",
            request=request,
            checkpoint=None,
            removed=(),
            reason="JOURNAL_PENDING",
        )

    def _create_copy_target(
        self,
        resources: _Resources,
        target: DatabaseCopyTarget,
    ) -> tuple[int, os.stat_result]:
        if self._copy_target_sidecar_signatures(resources.root_fd, target):
            raise WalSidecarUnsafeError("database copy target sidecar already exists")
        try:
            os.stat(target.name, dir_fd=resources.root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise WalSidecarUnsafeError(
                "database copy target cannot be inspected"
            ) from exc
        else:
            raise WalSidecarUnsafeError("database copy target already exists")
        try:
            target_fd = os.open(
                target.name,
                _open_file_flags(writable=True) | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=resources.root_fd,
            )
        except FileExistsError as exc:
            raise WalSidecarUnsafeError(
                "database copy target appeared during creation"
            ) from exc
        except OSError as exc:
            raise WalSidecarUnsafeError(
                "database copy target cannot be created"
            ) from exc
        target_identity: tuple[int, int] | None = None
        try:
            metadata = os.fstat(target_fd)
            path_metadata = os.stat(
                target.name,
                dir_fd=resources.root_fd,
                follow_symlinks=False,
            )
            _validate_regular(metadata, "database copy target")
            target_identity = _identity(metadata)
            if _identity(path_metadata) != _identity(metadata) or metadata.st_size != 0:
                raise WalSidecarRecoveryRequiredError(
                    "database copy target changed while creating"
                )
            resources.allowed_root_names = frozenset(
                {*resources.allowed_root_names, target.name}
            )
            self._refresh_root_observation(resources)
            return target_fd, metadata
        except BaseException as original_error:
            try:
                _close_temporary_fd(
                    target_fd,
                    target_identity,
                    "database copy target",
                )
            except _CLEANUP_EXCEPTION as close_error:
                raise WalSidecarRecoveryRequiredError(
                    "database copy target cannot be closed"
                ) from close_error
            del original_error
            raise

    def _refresh_root_observation(self, resources: _Resources) -> None:
        try:
            current_names = frozenset(os.listdir(resources.root_fd))
            mutable_names = _ROOT_MUTABLE_NAMES | resources.allowed_root_names
            previous_fixed = resources.root_names - mutable_names
            current_fixed = current_names - mutable_names
            if current_fixed != previous_fixed:
                raise WalSidecarRecoveryRequiredError(
                    "root entries changed outside SQLite sidecars"
                )
            for name in current_names & mutable_names:
                try:
                    metadata = os.stat(
                        name,
                        dir_fd=resources.root_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                _validate_regular(metadata, name)
            root_metadata = os.fstat(resources.root_fd)
            root_path = os.stat(resources.root, follow_symlinks=False)
        except OSError as exc:
            raise WalSidecarRecoveryRequiredError(
                "state root cannot be revalidated"
            ) from exc
        current_signature = _directory_security_signature(root_metadata)
        if current_signature != _directory_security_signature(root_path):
            raise WalSidecarRecoveryRequiredError("state root metadata changed")
        previous_signature = resources.root_signature
        if current_signature[0:3] + current_signature[4:] != (
            previous_signature[0:3] + previous_signature[4:]
        ):
            raise WalSidecarRecoveryRequiredError("state root security changed")
        if (
            current_signature[3] != previous_signature[3]
            and not (current_names ^ resources.root_names) <= mutable_names
        ):
            raise WalSidecarRecoveryRequiredError("state root link count changed")
        resources.root_names = current_names
        resources.root_signature = current_signature

    def _assert_resources(self, resources: _Resources) -> None:
        try:
            current_names = frozenset(os.listdir(resources.root_fd))
            root_metadata = os.fstat(resources.root_fd)
            root_path = os.stat(resources.root, follow_symlinks=False)
            parent_metadata = os.fstat(resources.parent_fd)
            gate_metadata = os.fstat(resources.gate_fd)
            gate_path = os.stat(
                _store.LIFETIME_GATE_FILENAME,
                dir_fd=resources.parent_fd,
                follow_symlinks=False,
            )
            marker_metadata = os.fstat(resources.marker_fd)
            marker_path = os.stat(
                self.marker_name,
                dir_fd=resources.root_fd,
                follow_symlinks=False,
            )
            database_metadata = os.fstat(resources.database_fd)
            database_path = os.stat(
                _store.DATABASE_FILENAME,
                dir_fd=resources.root_fd,
                follow_symlinks=False,
            )
            connection = resources.connection
            main_path = (
                None
                if connection is None
                else next(
                    str(row[2])
                    for row in connection.execute("PRAGMA database_list").fetchall()
                    if row[1] == "main"
                )
            )
            connection_path = (
                None if main_path is None else os.stat(main_path, follow_symlinks=False)
            )
        except OSError as exc:
            raise WalSidecarRecoveryRequiredError(
                "coordination identity is unavailable"
            ) from exc
        except (sqlite3.DatabaseError, StopIteration) as exc:
            raise WalSidecarRecoveryRequiredError(
                "SQLite connection identity is unavailable"
            ) from exc
        mutable_names = _ROOT_MUTABLE_NAMES | resources.allowed_root_names
        if current_names - mutable_names != resources.root_names - mutable_names:
            raise WalSidecarRecoveryRequiredError(
                "root entries changed outside SQLite sidecars"
            )
        checks = (
            (
                _directory_security_signature(root_metadata),
                _directory_security_signature(root_path),
                resources.root_signature,
            ),
            (
                _security_signature(gate_metadata),
                _security_signature(gate_path),
                resources.gate_signature,
            ),
            (
                _security_signature(marker_metadata),
                _security_signature(marker_path),
                resources.marker_signature,
            ),
            (
                _security_signature(database_metadata),
                _security_signature(database_path),
                resources.database_signature,
            ),
        )
        if any(
            fd_identity != path_identity or fd_identity != expected
            for fd_identity, path_identity, expected in checks
        ):
            raise WalSidecarRecoveryRequiredError("coordination identity changed")
        if connection is not None and (
            connection_path is None
            or _identity(connection_path) != resources.database_identity
        ):
            raise WalSidecarRecoveryRequiredError("SQLite connection identity changed")
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or _identity(parent_metadata) != resources.parent_identity
        ):
            raise WalSidecarRecoveryRequiredError("state parent identity changed")
        _validate_regular(marker_metadata, "writer marker")
        _validate_regular(database_metadata, "coordination database")
        try:
            marker_state = _store._read_writer_marker_state(resources.marker_fd)
        except _store.StoreError as exc:
            raise WalSidecarRecoveryRequiredError(
                "writer marker content is invalid"
            ) from exc
        if marker_state != resources.marker_state:
            raise WalSidecarRecoveryRequiredError("writer marker state changed")

    def _open_connection(
        self,
        resources: _Resources,
        *,
        reject_nonzero_wal: bool = False,
    ) -> sqlite3.Connection:
        self._assert_resources(resources)
        database_path = self._path_from_fd(resources.database_fd)
        connection: sqlite3.Connection | None = None
        try:
            self._fault("before_sqlite_connect")
            current_sidecars = self._sidecar_signatures(resources.root_fd)
            if reject_nonzero_wal:
                wal = current_sidecars.get(WAL_BASENAME)
                if wal is not None and wal[6] > 0:
                    raise _WalPendingError(
                        "SQLite WAL is already pending before cleanup connect"
                    )
            self._preflight_sidecars(resources, current_sidecars)
            connection = sqlite3.connect(
                f"file:{quote(database_path, safe='/')}?mode=rw",
                uri=True,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
            )
            self._fault("after_sqlite_connect")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            if journal_mode != "wal":
                _close_temporary_connection(connection, "SQLite checkpoint connection")
                connection = None
                raise WalSidecarUnsafeError("SQLite journal mode is not WAL")
            synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
            if type(synchronous) is not int or synchronous != 2:
                _close_temporary_connection(connection, "SQLite checkpoint connection")
                connection = None
                raise WalSidecarUnsafeError("SQLite synchronous mode is not FULL")
            _store._validate_existing_schema(connection)
            if not {
                WAL_BASENAME,
                JOURNAL_BASENAME,
            }.intersection(resources.sidecar_signatures):
                self._serialize_bound_source(resources, connection)
            current_sidecars = self._sidecar_signatures(resources.root_fd)
            for name, previous in resources.sidecar_signatures.items():
                current = current_sidecars.get(name)
                if current is None:
                    _close_temporary_connection(
                        connection,
                        "SQLite checkpoint connection",
                    )
                    connection = None
                    raise WalSidecarRecoveryRequiredError(
                        f"{name} disappeared while opening SQLite"
                    )
                if not _sidecar_recreation_is_safe(name, previous, current):
                    _close_temporary_connection(
                        connection,
                        "SQLite checkpoint connection",
                    )
                    connection = None
                    raise WalSidecarRecoveryRequiredError(
                        f"{name} changed while opening SQLite"
                    )
            resources.sidecar_signatures = current_sidecars
            self._refresh_root_observation(resources)
            resources.connection = connection
            self._assert_resources(resources)
            return connection
        except _store.StoreSchemaError as exc:
            raise WalSidecarUnsafeError(
                "SQLite database schema is unsupported"
            ) from exc
        except WalSidecarError:
            raise
        except _store.StoreError as exc:
            raise WalSidecarUnsafeError("SQLite database state is unsupported") from exc
        except (sqlite3.DatabaseError, OSError) as exc:
            raise WalSidecarUnsafeError(
                "SQLite database cannot be opened safely"
            ) from exc
        finally:
            if connection is not None and resources.connection is not connection:
                _close_temporary_connection(connection, "SQLite checkpoint connection")

    @staticmethod
    def _path_from_fd(fd: int) -> str:
        if os.uname().sysname == "Darwin":
            try:
                raw_path = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
            except OSError as exc:
                raise WalSidecarRecoveryRequiredError(
                    "descriptor path anchor is unavailable"
                ) from exc
            if not isinstance(raw_path, bytes) or b"\0" not in raw_path:
                raise WalSidecarRecoveryRequiredError(
                    "descriptor path anchor is invalid"
                )
            try:
                return raw_path.split(b"\0", 1)[0].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WalSidecarRecoveryRequiredError(
                    "descriptor path anchor is invalid"
                ) from exc
        return f"/proc/self/fd/{fd}"

    def _checkpoint_locked(
        self,
        resources: _Resources,
        request: CheckpointRequest,
        *,
        reject_nonzero_wal: bool = False,
    ) -> CheckpointResult:
        request = _canonical_checkpoint_request(request)
        if self._journal_is_nonempty(resources):
            raise WalSidecarUnsafeError(
                "SQLite rollback journal is pending before checkpoint"
            )
        if reject_nonzero_wal:
            current_sidecars = self._sidecar_signatures(resources.root_fd)
            wal = current_sidecars.get(WAL_BASENAME)
            if wal is not None and wal[6] > 0:
                raise _WalPendingError(
                    "SQLite WAL is already pending before checkpoint"
                )
        connection = resources.connection
        if connection is None:
            connection = self._open_connection(
                resources,
                reject_nonzero_wal=reject_nonzero_wal,
            )
        try:
            self._fault("before_checkpoint")
            try:
                row = connection.execute(
                    f"PRAGMA wal_checkpoint({request.mode})"
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise WalSidecarUnsafeError("SQLite checkpoint failed") from exc
            self._fault("after_checkpoint")
            if row is None or len(row) != 3:
                raise WalSidecarRecoveryRequiredError(
                    "SQLite checkpoint result is invalid"
                )
            try:
                result = CheckpointResult(
                    request=request,
                    busy=row[0],
                    log=row[1],
                    checkpointed=row[2],
                )
            except (TypeError, ValueError) as exc:
                raise WalSidecarRecoveryRequiredError(
                    "SQLite checkpoint result is invalid"
                ) from exc
            current_sidecars = self._sidecar_signatures(resources.root_fd)
            if reject_nonzero_wal:
                wal = current_sidecars.get(WAL_BASENAME)
                if wal is not None and wal[6] > 0:
                    raise _WalPendingError(
                        "SQLite WAL appeared before cleanup transition"
                    )
            if result.safe:
                self._serialize_bound_source(resources, connection)
            elif not resources.preexisting_wal_or_journal:
                raise WalSidecarRecoveryRequiredError(
                    "SQLite checkpoint source binding is not provable"
                )
            elif not reject_nonzero_wal:
                # An explicit public checkpoint may observe a valid existing
                # WAL.  Cleanup and source-copy callers reject that state
                # before opening SQLite because they cannot consume it safely.
                self._assert_resources(resources)
            else:
                raise WalSidecarRecoveryRequiredError(
                    "SQLite checkpoint source binding is not provable"
                )
            return result
        except BaseException:
            if resources.connection is connection:
                try:
                    self._close_resource_connection(resources)
                except WalSidecarError as close_error:
                    raise WalSidecarRecoveryRequiredError(
                        "SQLite checkpoint connection cannot be closed"
                    ) from close_error
            else:
                try:
                    _close_temporary_connection(
                        connection,
                        "SQLite checkpoint connection",
                    )
                except WalSidecarError as close_error:
                    raise WalSidecarRecoveryRequiredError(
                        "SQLite checkpoint connection cannot be closed"
                    ) from close_error
            raise

    @staticmethod
    def _execute_text_pragma(
        connection: sqlite3.Connection,
        statement: str,
        expected: str,
        label: str,
        busy_values: frozenset[str] = frozenset(),
    ) -> str:
        try:
            row = connection.execute(statement).fetchone()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise WalSidecarBusyError(f"SQLite {label} is busy") from exc
            raise WalSidecarUnsafeError(f"SQLite {label} failed") from exc
        except sqlite3.DatabaseError as exc:
            raise WalSidecarUnsafeError(f"SQLite {label} failed") from exc
        if row is None or len(row) != 1 or type(row[0]) is not str:
            raise WalSidecarRecoveryRequiredError(f"SQLite {label} result is invalid")
        value = row[0].lower()
        if value in busy_values:
            raise WalSidecarBusyError(f"SQLite {label} is busy")
        if value != expected:
            raise WalSidecarRecoveryRequiredError(
                f"SQLite {label} returned an unexpected mode"
            )
        return value

    def _sync_marker_state(
        self,
        resources: _Resources,
        state: str,
        *,
        require_no_sidecars: bool = False,
    ) -> None:
        if type(state) is not str:
            raise TypeError("marker state is invalid")
        if state not in {
            _store.WRITER_MARKER_CLEAN_STATE,
            _store.WRITER_MARKER_PREPARED_STATE,
        }:
            raise ValueError("marker state is unsupported")
        if state == _store.WRITER_MARKER_PREPARED_STATE:
            self._fault("before_marker_prepare")
        else:
            self._fault("before_marker_clean")
            if require_no_sidecars and self._sidecar_signatures(resources.root_fd):
                raise WalSidecarRecoveryRequiredError(
                    "exact SQLite sidecar inventory changed before marker clean"
                )
        self._assert_resources(resources)
        try:
            _store._write_writer_marker_state(resources.marker_fd, state)
        except _store.StoreError as exc:
            raise WalSidecarRecoveryRequiredError(
                "writer marker state cannot be written"
            ) from exc
        if state == _store.WRITER_MARKER_PREPARED_STATE:
            self._fault("after_marker_prepare")
        try:
            if state == _store.WRITER_MARKER_PREPARED_STATE:
                self._fault("before_marker_prepare_fsync")
            else:
                self._fault("before_marker_clean_fsync")
            self._fault("before_marker_fsync")
            os.fsync(resources.marker_fd)
            os.fsync(resources.root_fd)
            if state == _store.WRITER_MARKER_PREPARED_STATE:
                self._fault("after_marker_prepare_fsync")
            else:
                self._fault("after_marker_clean_fsync")
            self._fault("after_marker_fsync")
        except OSError as exc:
            if state == _store.WRITER_MARKER_CLEAN_STATE:
                try:
                    _store._write_writer_marker_state(
                        resources.marker_fd,
                        _store.WRITER_MARKER_PREPARED_STATE,
                    )
                except _store.StoreError:
                    pass
            raise WalSidecarRecoveryRequiredError(
                "writer marker durability is unknown"
            ) from exc
        resources.marker_state = state
        if state == _store.WRITER_MARKER_CLEAN_STATE:
            self._fault("after_marker_clean")
        self._assert_resources(resources)

    def _checkpoint_for_session(
        self,
        resources: _Resources,
        request: CheckpointRequest,
    ) -> CheckpointResult:
        request = _canonical_checkpoint_request(request)
        result = self._checkpoint_locked(resources, request)
        connection = resources.connection
        if connection is not None:
            try:
                _close_temporary_connection(
                    connection,
                    "SQLite checkpoint connection",
                )
            except WalSidecarError as exc:
                raise WalSidecarRecoveryRequiredError(
                    "SQLite checkpoint connection cannot be closed"
                ) from exc
            resources.connection = None
        current_sidecars = self._sidecar_signatures(resources.root_fd)
        for name, previous in resources.sidecar_signatures.items():
            current = current_sidecars.get(name)
            if current is not None and not _sidecar_recreation_is_safe(
                name, previous, current
            ):
                raise WalSidecarRecoveryRequiredError(
                    f"{name} changed after checkpoint"
                )
        resources.sidecar_signatures = current_sidecars
        self._refresh_root_observation(resources)
        self._fault("before_result")
        self._assert_resources(resources)
        self._fault("after_result")
        self._assert_resources(resources)
        return result

    def _rebind_database(self, resources: _Resources) -> None:
        """Refresh a database descriptor while exclusive guards stay held."""

        self._close_resource_connection(resources)
        old_fd = resources.database_fd
        self._fault("before_database_rebind")
        try:
            before = os.stat(
                _store.DATABASE_FILENAME,
                dir_fd=resources.root_fd,
                follow_symlinks=False,
            )
            _validate_regular(before, "coordination database")
            new_fd = os.open(
                _store.DATABASE_FILENAME,
                _open_file_flags(writable=True),
                dir_fd=resources.root_fd,
            )
            try:
                metadata = os.fstat(new_fd)
                after = os.stat(
                    _store.DATABASE_FILENAME,
                    dir_fd=resources.root_fd,
                    follow_symlinks=False,
                )
                _validate_regular(metadata, "coordination database")
                if _identity(before) != _identity(metadata) or _identity(
                    after
                ) != _identity(metadata):
                    raise WalSidecarRecoveryRequiredError(
                        "coordination database changed while rebinding"
                    )
                database_identity = _identity(metadata)
                database_signature = _security_signature(metadata)
                sidecar_signatures = self._sidecar_signatures(resources.root_fd)
                _close_temporary_fd(
                    old_fd,
                    resources.database_identity,
                    "old coordination database descriptor",
                )
                resources.database_fd = new_fd
                resources.database_identity = database_identity
                resources.database_signature = database_signature
                resources.sidecar_signatures = sidecar_signatures
                new_fd = -1
            finally:
                if new_fd != -1:
                    _close_temporary_fd(
                        new_fd,
                        None,
                        "rebound coordination database",
                    )
        except WalSidecarError:
            raise
        except OSError as exc:
            raise WalSidecarRecoveryRequiredError(
                "coordination database cannot be rebound"
            ) from exc
        self._open_connection(resources)
        self._close_resource_connection(resources)
        self._refresh_root_observation(resources)
        self._assert_resources(resources)
        self._fault("after_database_rebind")
        self._assert_resources(resources)

    def _read_stable_database_bytes(self, resources: _Resources) -> bytes:
        try:
            before = os.fstat(resources.database_fd)
            path_before = os.stat(
                _store.DATABASE_FILENAME,
                dir_fd=resources.root_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise WalSidecarRecoveryRequiredError(
                "coordination database cannot be read from its held descriptor"
            ) from exc
        _validate_regular(before, "coordination database")
        if _identity(path_before) != _identity(before):
            raise WalSidecarRecoveryRequiredError(
                "coordination database changed before serialization"
            )
        if before.st_size > MAX_COPY_BYTES:
            raise WalSidecarUnsafeError("coordination database is too large")
        chunks: list[bytes] = []
        offset = 0
        while offset < before.st_size:
            try:
                chunk = os.pread(
                    resources.database_fd,
                    min(1024 * 1024, before.st_size - offset),
                    offset,
                )
            except OSError as exc:
                raise WalSidecarRecoveryRequiredError(
                    "coordination database cannot be read"
                ) from exc
            if not chunk:
                raise WalSidecarRecoveryRequiredError(
                    "coordination database ended while reading"
                )
            chunks.append(chunk)
            offset += len(chunk)
        try:
            after = os.fstat(resources.database_fd)
            path_after = os.stat(
                _store.DATABASE_FILENAME,
                dir_fd=resources.root_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise WalSidecarRecoveryRequiredError(
                "coordination database changed while reading"
            ) from exc
        if _signature(before) != _signature(after) or _identity(
            path_after
        ) != _identity(after):
            raise WalSidecarRecoveryRequiredError(
                "coordination database changed while reading"
            )
        return b"".join(chunks)

    @staticmethod
    def _write_bytes_to_fd(fd: int, content: bytes, label: str) -> None:
        if len(content) > MAX_COPY_BYTES:
            raise WalSidecarUnsafeError(f"{label} is too large")
        try:
            os.ftruncate(fd, 0)
            offset = 0
            while offset < len(content):
                written = os.pwrite(fd, content[offset:], offset)
                if written <= 0:
                    raise OSError(f"{label} write was incomplete")
                offset += written
        except OSError as exc:
            raise WalSidecarRecoveryRequiredError(f"{label} cannot be written") from exc

    def _read_stable_copy_target_bytes(
        self,
        resources: _Resources,
        target: DatabaseCopyTarget,
        target_fd: int,
        expected_identity: tuple[int, int],
        expected_size: int,
    ) -> bytes:
        """Read a newly-created target through its held descriptor and path."""

        try:
            before = os.fstat(target_fd)
            path_before = os.stat(
                target.name,
                dir_fd=resources.root_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise WalSidecarRecoveryRequiredError(
                "database copy target cannot be revalidated"
            ) from exc
        _validate_regular(before, "database copy target")
        _validate_regular(path_before, "database copy target")
        if (
            _identity(before) != expected_identity
            or _identity(path_before) != expected_identity
            or before.st_size != expected_size
        ):
            raise WalSidecarRecoveryRequiredError(
                "database copy target identity or size changed"
            )
        chunks: list[bytes] = []
        offset = 0
        while offset < expected_size:
            try:
                chunk = os.pread(
                    target_fd,
                    min(1024 * 1024, expected_size - offset),
                    offset,
                )
            except OSError as exc:
                raise WalSidecarRecoveryRequiredError(
                    "database copy target cannot be read"
                ) from exc
            if not chunk:
                raise WalSidecarRecoveryRequiredError(
                    "database copy target ended while reading"
                )
            chunks.append(chunk)
            offset += len(chunk)
        try:
            after = os.fstat(target_fd)
            path_after = os.stat(
                target.name,
                dir_fd=resources.root_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise WalSidecarRecoveryRequiredError(
                "database copy target changed while reading"
            ) from exc
        _validate_regular(after, "database copy target")
        _validate_regular(path_after, "database copy target")
        if (
            _signature(before) != _signature(after)
            or _identity(after) != expected_identity
            or _identity(path_after) != expected_identity
            or after.st_size != expected_size
        ):
            raise WalSidecarRecoveryRequiredError(
                "database copy target changed while reading"
            )
        return b"".join(chunks)

    def _serialize_bound_source(
        self,
        resources: _Resources,
        connection: sqlite3.Connection,
    ) -> bytes:
        try:
            serialized = connection.serialize()
        except (AttributeError, sqlite3.DatabaseError) as exc:
            raise WalSidecarRecoveryRequiredError(
                "SQLite source cannot be serialized"
            ) from exc
        if not isinstance(serialized, bytes):
            raise WalSidecarRecoveryRequiredError(
                "SQLite source serialization is invalid"
            )
        held = self._read_stable_database_bytes(resources)
        if serialized != held:
            raise WalSidecarRecoveryRequiredError(
                "SQLite source is not bound to the held database"
            )
        return serialized

    @staticmethod
    def _close_resource_connection(resources: _Resources) -> None:
        connection = resources.connection
        if connection is None:
            return
        try:
            connection.close()
        except _CLEANUP_EXCEPTION:
            try:
                connection.close()
            except _CLEANUP_EXCEPTION as retry_error:
                raise WalSidecarRecoveryRequiredError(
                    "SQLite connection cannot be closed"
                ) from retry_error
        resources.connection = None

    def _copy_database_to(
        self,
        resources: _Resources,
        request: CheckpointRequest,
        target: DatabaseCopyTarget,
    ) -> DatabaseCopyResult:
        """Copy the source while locks stay held, without exposing its handle."""

        request = _canonical_checkpoint_request(request)
        target = _canonical_copy_target(target)
        checkpoint = self._checkpoint_locked(
            resources,
            request,
            reject_nonzero_wal=True,
        )
        if not checkpoint.safe:
            self._close_resource_connection(resources)
            self._refresh_sidecars_after_connection_close(resources)
            raise WalSidecarRecoveryRequiredError(
                "database source copy checkpoint is not safe"
            )
        source_connection = resources.connection
        if source_connection is None:
            raise WalSidecarRecoveryRequiredError(
                "database source connection is unavailable"
            )
        copy_image: bytes | None = None
        target_fd: int | None = None
        target_connection: sqlite3.Connection | None = None
        try:
            self._assert_resources(resources)
            self._serialize_bound_source(resources, source_connection)
            target_connection = sqlite3.connect(
                ":memory:",
                uri=False,
                timeout=0,
                isolation_level=None,
            )
            self._fault("before_source_copy")
            source_connection.backup(target_connection)
            self._fault("after_source_copy")
            try:
                backup_image = target_connection.serialize()
            except (AttributeError, sqlite3.DatabaseError) as exc:
                raise WalSidecarRecoveryRequiredError(
                    "SQLite backup serialization is unavailable"
                ) from exc
            if (
                not isinstance(backup_image, bytes)
                or len(backup_image) > MAX_COPY_BYTES
            ):
                raise WalSidecarRecoveryRequiredError(
                    "SQLite backup serialization is invalid"
                )
            try:
                copied = source_connection.serialize()
            except (AttributeError, sqlite3.DatabaseError) as exc:
                raise WalSidecarRecoveryRequiredError(
                    "SQLite source changed during backup"
                ) from exc
            held_after = self._read_stable_database_bytes(resources)
            if not isinstance(copied, bytes) or copied != held_after:
                raise WalSidecarRecoveryRequiredError(
                    "SQLite source changed during backup"
                )
            if len(backup_image) != len(held_after) or (
                backup_image[:40] != held_after[:40]
                or backup_image[44:] != held_after[44:]
            ):
                raise WalSidecarRecoveryRequiredError(
                    "SQLite backup serialization mismatches its source"
                )
            copy_image = held_after
            _close_temporary_connection(
                target_connection,
                "SQLite backup connection",
            )
            target_connection = None
            self._close_resource_connection(resources)
            source_connection = None
            source_sidecars = self._sidecar_signatures(resources.root_fd)
            resources.sidecar_signatures = source_sidecars
            if SHM_BASENAME in source_sidecars:
                raise WalSidecarBusyError("database source has an active reader")
            if WAL_BASENAME in source_sidecars or JOURNAL_BASENAME in source_sidecars:
                raise WalSidecarRecoveryRequiredError(
                    "database source sidecars remain after checkpoint"
                )
            self._refresh_root_observation(resources)
            self._assert_resources(resources)
            target_fd, target_metadata = self._create_copy_target(resources, target)
            self._fault("before_copy_target_write")
            self._write_bytes_to_fd(target_fd, copy_image, "database copy target")
            self._fault("after_copy_target_write")
            target_identity = _identity(target_metadata)
            target_image = self._read_stable_copy_target_bytes(
                resources,
                target,
                target_fd,
                target_identity,
                len(copy_image),
            )
            if target_image != copy_image:
                raise WalSidecarRecoveryRequiredError(
                    "database copy target contents changed after serialization"
                )
            if self._copy_target_sidecar_signatures(resources.root_fd, target):
                raise WalSidecarRecoveryRequiredError(
                    "database copy target sidecars were created"
                )
            self._fault("before_source_copy_fsync")
            os.fsync(target_fd)
            os.fsync(resources.root_fd)
            self._fault("after_source_copy_fsync")
            target_image = self._read_stable_copy_target_bytes(
                resources,
                target,
                target_fd,
                target_identity,
                len(copy_image),
            )
            if target_image != copy_image or self._copy_target_sidecar_signatures(
                resources.root_fd, target
            ):
                raise WalSidecarRecoveryRequiredError(
                    "database copy target changed before result"
                )
            self._fault("before_result")
            final_image = self._read_stable_copy_target_bytes(
                resources,
                target,
                target_fd,
                target_identity,
                len(copy_image),
            )
            if final_image != copy_image:
                raise WalSidecarRecoveryRequiredError(
                    "database copy target contents changed before result"
                )
            if self._copy_target_sidecar_signatures(resources.root_fd, target):
                raise WalSidecarRecoveryRequiredError(
                    "database copy target sidecars were created before result"
                )
            digest = "sha256:" + hashlib.sha256(final_image).hexdigest()
            final_target = os.fstat(target_fd)
            result = DatabaseCopyResult(
                checkpoint=checkpoint,
                target=target,
                source_identity=resources.database_identity,
                target_identity=_identity(final_target),
                size=final_target.st_size,
                digest=digest,
            )
            self._assert_resources(resources)
            return result
        except (WalSidecarError, _store.StoreError):
            raise
        except (OSError, sqlite3.DatabaseError, StopIteration) as exc:
            raise WalSidecarRecoveryRequiredError(
                "database source copy is incomplete"
            ) from exc
        finally:
            cleanup_actions: list[Callable[[], None]] = []
            if target_connection is not None:
                backup_connection = target_connection

                def close_backup_connection() -> None:
                    try:
                        _close_temporary_connection(
                            backup_connection,
                            "SQLite backup connection",
                        )
                    except _CLEANUP_EXCEPTION:
                        if backup_connection not in resources._orphan_connections:
                            resources._orphan_connections.append(backup_connection)
                        raise

                cleanup_actions.append(close_backup_connection)
            if source_connection is not None:

                def close_source_connection() -> None:
                    _close_temporary_connection(
                        source_connection,
                        "SQLite source connection",
                    )
                    if resources.connection is source_connection:
                        resources.connection = None

                cleanup_actions.append(close_source_connection)
            if target_fd is not None:
                copy_fd = target_fd
                copy_identity = _identity(target_metadata)

                def close_copy_fd() -> None:
                    try:
                        _close_temporary_fd(
                            copy_fd,
                            copy_identity,
                            "database copy target",
                        )
                    except _CLEANUP_EXCEPTION:
                        _retain_failed_fd(
                            resources,
                            copy_fd,
                            copy_identity,
                            "database copy target",
                        )
                        raise

                cleanup_actions.append(close_copy_fd)
            _run_cleanup_actions(
                tuple(cleanup_actions),
                "database source copy resource",
            )

    def checkpoint(self, request: CheckpointRequest) -> CheckpointResult:
        """Run one exact checkpoint request under exclusive guards."""

        self._assert_initialized()
        request = _canonical_checkpoint_request(request)
        with self._resources() as resources:
            result = self._checkpoint_locked(resources, request)
            self._fault("before_result")
            self._assert_resources(resources)
            self._fault("after_result")
            self._assert_resources(resources)
            return result

    def _finish_cleanup_locked(
        self,
        resources: _Resources,
        request: CheckpointRequest,
        checkpoint: CheckpointResult,
        before_transition: frozenset[str],
    ) -> SidecarCleanupResult:
        post_transition = self._sidecar_signatures(resources.root_fd)
        resources.sidecar_signatures = post_transition
        self._refresh_root_observation(resources)
        if post_transition:
            raise WalSidecarRecoveryRequiredError(
                "SQLite left exact sidecars after WAL reentry"
            )
        self._fault("before_fsync")
        try:
            os.fsync(resources.root_fd)
        except OSError as exc:
            raise WalSidecarRecoveryRequiredError(
                "sidecar directory durability is unknown"
            ) from exc
        self._fault("after_fsync")
        self._assert_resources(resources)
        if self._sidecar_signatures(resources.root_fd):
            raise WalSidecarRecoveryRequiredError(
                "exact SQLite sidecar inventory is not empty"
            )
        self._fault("before_marker_clean_inventory")
        if self._sidecar_signatures(resources.root_fd):
            raise WalSidecarRecoveryRequiredError(
                "exact SQLite sidecar inventory changed before marker clean"
            )
        self._sync_marker_state(
            resources,
            _store.WRITER_MARKER_CLEAN_STATE,
            require_no_sidecars=True,
        )
        try:
            self._fault("before_sqlite_close")
            if self._preflight_sidecars(resources):
                raise WalSidecarRecoveryRequiredError(
                    "exact SQLite sidecar reappeared before close"
                )
        except _CLEANUP_EXCEPTION as close_error:
            try:
                resources.sidecar_signatures = self._sidecar_signatures(
                    resources.root_fd
                )
                self._refresh_root_observation(resources)
                self._sync_marker_state(
                    resources,
                    _store.WRITER_MARKER_PREPARED_STATE,
                )
            except WalSidecarError as prepare_error:
                raise WalSidecarRecoveryRequiredError(
                    "cleanup marker cannot be reverted to prepared"
                ) from prepare_error
            raise WalSidecarRecoveryRequiredError(
                "cleanup sidecar appeared before SQLite close"
            ) from close_error
        self._close_resource_connection(resources)
        self._fault("after_sqlite_close")
        resources.sidecar_signatures = self._sidecar_signatures(resources.root_fd)
        self._refresh_root_observation(resources)
        self._assert_resources(resources)
        removed = tuple(name for name in SIDECAR_BASENAMES if name in before_transition)
        self._fault("before_result")
        resources.sidecar_signatures = self._sidecar_signatures(resources.root_fd)
        self._refresh_root_observation(resources)
        self._assert_resources(resources)
        result = SidecarCleanupResult(
            outcome="CLEANED",
            request=request,
            checkpoint=checkpoint,
            removed=removed,
        )
        self._fault("after_result")
        resources.sidecar_signatures = self._sidecar_signatures(resources.root_fd)
        self._refresh_root_observation(resources)
        self._assert_resources(resources)
        return result

    def _cleanup_locked(
        self,
        resources: _Resources,
        request: CheckpointRequest,
    ) -> SidecarCleanupResult:
        """Run cleanup with an already-held exclusive quiescence session."""

        request = _canonical_checkpoint_request(request)
        initial_sidecars = self._sidecar_signatures(resources.root_fd)
        journal = initial_sidecars.get(JOURNAL_BASENAME)
        if journal is not None and journal[6] > 0:
            return self._journal_pending_result(request)
        wal = initial_sidecars.get(WAL_BASENAME)
        if wal is not None and wal[6] > 0:
            return SidecarCleanupResult(
                outcome="BLOCKED",
                request=request,
                checkpoint=None,
                removed=(),
                reason="WAL_PENDING",
            )
        try:
            checkpoint = self._checkpoint_locked(
                resources,
                request,
                reject_nonzero_wal=True,
            )
        except _WalPendingError:
            self._refresh_sidecars_after_connection_close(resources)
            return SidecarCleanupResult(
                outcome="BLOCKED",
                request=request,
                checkpoint=None,
                removed=(),
                reason="WAL_PENDING",
            )
        except WalSidecarBusyError:
            self._refresh_sidecars_after_connection_close(resources)
            return SidecarCleanupResult(
                outcome="BLOCKED",
                request=request,
                checkpoint=None,
                removed=(),
                reason="READER_ACTIVE",
            )
        if checkpoint.busy != 0:
            self._close_resource_connection(resources)
            self._refresh_sidecars_after_connection_close(resources)
            return SidecarCleanupResult(
                outcome="BLOCKED",
                request=request,
                checkpoint=checkpoint,
                removed=(),
                reason="CHECKPOINT_BUSY",
            )
        if checkpoint.log != checkpoint.checkpointed:
            self._close_resource_connection(resources)
            self._refresh_sidecars_after_connection_close(resources)
            return SidecarCleanupResult(
                outcome="BLOCKED",
                request=request,
                checkpoint=checkpoint,
                removed=(),
                reason="CHECKPOINT_INCOMPLETE",
            )
        checkpoint_sidecars = self._sidecar_signatures(resources.root_fd)
        wal = checkpoint_sidecars.get(WAL_BASENAME)
        if wal is not None and wal[6] > 0:
            self._close_resource_connection(resources)
            resources.sidecar_signatures = checkpoint_sidecars
            self._refresh_root_observation(resources)
            return SidecarCleanupResult(
                outcome="BLOCKED",
                request=request,
                checkpoint=None,
                removed=(),
                reason="WAL_PENDING",
            )
        if (
            JOURNAL_BASENAME in checkpoint_sidecars
            and checkpoint_sidecars[JOURNAL_BASENAME][6] > 0
        ):
            self._close_resource_connection(resources)
            resources.sidecar_signatures = checkpoint_sidecars
            self._refresh_root_observation(resources)
            return SidecarCleanupResult(
                outcome="BLOCKED",
                request=request,
                checkpoint=checkpoint,
                removed=(),
                reason="JOURNAL_PENDING",
            )
        resources.sidecar_signatures = checkpoint_sidecars
        self._refresh_root_observation(resources)
        self._assert_resources(resources)
        connection = resources.connection
        if connection is None:
            raise WalSidecarRecoveryRequiredError(
                "SQLite cleanup connection is unavailable"
            )
        before_transition = frozenset(checkpoint_sidecars)
        try:
            self._sync_marker_state(resources, _store.WRITER_MARKER_PREPARED_STATE)
        except _CLEANUP_EXCEPTION:
            try:
                self._close_resource_connection(resources)
            except WalSidecarError as close_error:
                raise WalSidecarRecoveryRequiredError(
                    "SQLite cleanup connection cannot be closed"
                ) from close_error
            raise
        try:
            self._fault("before_journal_delete")
            transition_sidecars = self._sidecar_signatures(resources.root_fd)
            transition_journal = transition_sidecars.get(JOURNAL_BASENAME)
            transition_wal = transition_sidecars.get(WAL_BASENAME)
            if (transition_journal is not None and transition_journal[6] > 0) or (
                transition_wal is not None and transition_wal[6] > 0
            ):
                raise WalSidecarRecoveryRequiredError(
                    "SQLite sidecar appeared before DELETE transition"
                )
            self._preflight_sidecars(
                resources,
                transition_sidecars,
                reader_hint=False,
            )
            delete_mode = self._execute_text_pragma(
                connection,
                "PRAGMA journal_mode=DELETE",
                "delete",
                "journal_mode=DELETE",
                frozenset({"wal"}),
            )
            if delete_mode == "wal":
                raise WalSidecarBusyError("SQLite journal_mode=DELETE is busy")
            if delete_mode != "delete":
                raise WalSidecarRecoveryRequiredError(
                    "SQLite journal_mode=DELETE returned an unexpected mode"
                )
            self._fault("after_journal_delete")
        except WalSidecarBusyError:
            self._close_resource_connection(resources)
            resources.sidecar_signatures = self._sidecar_signatures(resources.root_fd)
            self._refresh_root_observation(resources)
            self._sync_marker_state(resources, _store.WRITER_MARKER_CLEAN_STATE)
            journal = resources.sidecar_signatures.get(JOURNAL_BASENAME)
            reason: CleanupReason = (
                "JOURNAL_PENDING"
                if journal is not None and journal[6] > 0
                else "READER_ACTIVE"
            )
            return SidecarCleanupResult(
                outcome="BLOCKED",
                request=request,
                checkpoint=checkpoint,
                removed=(),
                reason=reason,
            )
        try:
            self._fault("before_exclusive_lock")
            exclusive_mode = self._execute_text_pragma(
                connection,
                "PRAGMA locking_mode=EXCLUSIVE",
                "exclusive",
                "locking_mode=EXCLUSIVE",
            )
            if exclusive_mode != "exclusive":
                raise WalSidecarRecoveryRequiredError(
                    "SQLite locking_mode=EXCLUSIVE returned an unexpected mode"
                )
            self._fault("after_exclusive_lock")
            self._fault("before_wal_reentry")
            wal_mode = self._execute_text_pragma(
                connection,
                "PRAGMA journal_mode=WAL",
                "wal",
                "journal_mode=WAL",
            )
            if wal_mode != "wal":
                raise WalSidecarRecoveryRequiredError(
                    "SQLite journal_mode=WAL returned an unexpected mode"
                )
            self._fault("after_wal_reentry")
        except _CLEANUP_EXCEPTION as transition_error:
            try:
                self._close_resource_connection(resources)
            except WalSidecarError as close_error:
                raise WalSidecarRecoveryRequiredError(
                    "SQLite cleanup connection cannot be closed"
                ) from close_error
            if isinstance(transition_error, WalSidecarBusyError):
                raise WalSidecarRecoveryRequiredError(
                    "SQLite cleanup mode transition is incomplete"
                ) from transition_error
            raise
        try:
            return self._finish_cleanup_locked(
                resources,
                request,
                checkpoint,
                before_transition,
            )
        except _CLEANUP_EXCEPTION:
            if resources.connection is not None:
                try:
                    self._close_resource_connection(resources)
                except WalSidecarError as close_error:
                    raise WalSidecarRecoveryRequiredError(
                        "SQLite cleanup connection cannot be closed"
                    ) from close_error
            raise

    def cleanup(self, request: CheckpointRequest) -> SidecarCleanupResult:
        """Checkpoint, then remove only safe exact SQLite sidecars."""

        self._assert_initialized()
        request = _canonical_checkpoint_request(request)
        with self._resources() as resources:
            return self._cleanup_locked(resources, request)


__all__ = [
    "CHECKPOINT_MODES",
    "JOURNAL_BASENAME",
    "MARKER_CLEAN_CONTENT",
    "MARKER_PREPARED_CONTENT",
    "SHM_BASENAME",
    "SIDECAR_BASENAMES",
    "WAL_BASENAME",
    "WRITER_MARKER_BASENAME",
    "CheckpointMode",
    "CheckpointRequest",
    "CheckpointResult",
    "CleanupOutcome",
    "CleanupReason",
    "DatabaseCopyResult",
    "DatabaseCopyTarget",
    "QuiescenceSession",
    "SidecarCleanupResult",
    "WalSidecarBusyError",
    "WalSidecarClosedError",
    "WalSidecarController",
    "WalSidecarError",
    "WalSidecarRecoveryRequiredError",
    "WalSidecarUnsafeError",
]
