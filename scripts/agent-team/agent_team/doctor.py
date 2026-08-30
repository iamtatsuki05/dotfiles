"""Read-only inspection of the private agent-team coordination state.

This module deliberately has no normal :class:`CoordinationStore` opener.  It
uses an already-existing directory, lifetime gate, and regular files only;
all observations are taken through no-follow descriptors and are checked
again before a report is returned.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Self

from . import store as _store

FileType = Literal[
    "directory",
    "regular",
    "symlink",
    "fifo",
    "socket",
    "block_device",
    "char_device",
    "unknown",
]
ObservedState = Literal[
    "MISSING_ROOT",
    "EMPTY_ROOT",
    "MISSING",
    "WRITER_ACTIVE",
    "WAL_PENDING",
    "RESTORE_INCOMPLETE",
    "UNSAFE_SIDECAR",
    "SCHEMA_INVALID",
    "UNREADABLE",
    "NOT_FOUND",
    "INTENT_ONLY",
    "UNKNOWN_EFFECT",
    "RECEIPTED",
    "COMPLETED",
    "CLEANED",
]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]
SafeAction = Literal[
    "NONE",
    "CLAIM",
    "QUERY_PROVIDER_THEN_RESOLVE",
    "VERIFY_RECEIPT_THEN_COMPLETE",
    "CHECKPOINT_AFTER_QUIESCE",
    "INSPECT_SCHEMA",
    "OPERATOR_REVIEW",
]
Mutation = Literal[
    "claim",
    "heartbeat",
    "reclaim",
    "reserve_fence",
    "execute_effect",
    "record_receipt",
    "complete",
    "recover",
    "force_recover",
    "resolve_unknown",
    "rebind_receipt",
    "checkpoint",
    "cleanup",
    "restore",
]
LedgerPhase = Literal[
    "RESTORE_PREPARED",
    "RESTORE_REPLACED",
    "RESTORE_COMMITTED",
    "RESTORE_ABORTED",
]

DOCTOR_PROTOCOL_VERSION: Final[int] = 1
RECOVERY_LEDGER_VERSION: Final[int] = 1
WRITER_MARKER_BASENAME: Final[str] = _store.WRITER_MARKER_FILENAME
MAX_LEDGER_BYTES: Final[int] = 4 * 1024 * 1024
MAX_DATABASE_BYTES: Final[int] = 256 * 1024 * 1024
_CLEANUP_EXCEPTION: Final[type[BaseException]] = BaseException
_MAX_INT = 2**63 - 1
_HEX = frozenset("0123456789abcdef")
_FILE_TYPES: Final[tuple[FileType, ...]] = (
    "directory",
    "regular",
    "symlink",
    "fifo",
    "socket",
    "block_device",
    "char_device",
    "unknown",
)
_OBSERVED_STATES: Final[tuple[ObservedState, ...]] = (
    "MISSING_ROOT",
    "EMPTY_ROOT",
    "MISSING",
    "WRITER_ACTIVE",
    "WAL_PENDING",
    "RESTORE_INCOMPLETE",
    "UNSAFE_SIDECAR",
    "SCHEMA_INVALID",
    "UNREADABLE",
    "NOT_FOUND",
    "INTENT_ONLY",
    "UNKNOWN_EFFECT",
    "RECEIPTED",
    "COMPLETED",
    "CLEANED",
)
_CONFIDENCES: Final[tuple[Confidence, ...]] = ("HIGH", "MEDIUM", "LOW")
_SAFE_ACTIONS: Final[tuple[SafeAction, ...]] = (
    "NONE",
    "CLAIM",
    "QUERY_PROVIDER_THEN_RESOLVE",
    "VERIFY_RECEIPT_THEN_COMPLETE",
    "CHECKPOINT_AFTER_QUIESCE",
    "INSPECT_SCHEMA",
    "OPERATOR_REVIEW",
)
_MUTATIONS: Final[tuple[Mutation, ...]] = (
    "claim",
    "heartbeat",
    "reclaim",
    "reserve_fence",
    "execute_effect",
    "record_receipt",
    "complete",
    "recover",
    "force_recover",
    "resolve_unknown",
    "rebind_receipt",
    "checkpoint",
    "cleanup",
    "restore",
)
_LEDGER_PHASES: Final[tuple[LedgerPhase, ...]] = (
    "RESTORE_PREPARED",
    "RESTORE_REPLACED",
    "RESTORE_COMMITTED",
    "RESTORE_ABORTED",
)
_SIDECAR_SUFFIXES: Final[tuple[str, ...]] = ("-wal", "-shm", "-journal")
_PENDING_LEDGER_PHASES: Final[frozenset[LedgerPhase]] = frozenset(
    {"RESTORE_PREPARED", "RESTORE_REPLACED"}
)
_TERMINAL_LEDGER_PHASES: Final[frozenset[LedgerPhase]] = frozenset(
    {"RESTORE_COMMITTED", "RESTORE_ABORTED"}
)
_LEDGER_TRANSITIONS: Final[dict[LedgerPhase, frozenset[LedgerPhase]]] = {
    "RESTORE_PREPARED": frozenset({"RESTORE_REPLACED", "RESTORE_ABORTED"}),
    "RESTORE_REPLACED": frozenset({"RESTORE_COMMITTED", "RESTORE_ABORTED"}),
}


class DoctorError(RuntimeError):
    """Base class for read-only doctor failures."""


class StateFilesystemError(DoctorError):
    """The state root or a required existing file cannot be observed safely."""


class UnsafeFilesystemError(StateFilesystemError):
    """A known state file has an unsafe type, owner, mode, or link count."""


class UnstableSnapshotError(StateFilesystemError):
    """A path and its descriptor did not retain one identity/metadata view."""


class WriterActiveError(StateFilesystemError):
    """The existing lifetime gate is held exclusively by another process."""


class LedgerReadError(DoctorError):
    """An existing recovery ledger is incomplete or malformed."""


def _require_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_INT:
        msg = f"{name} must be a supported integer"
        raise ValueError(msg)
    return value


def _require_basename(value: object, name: str) -> str:
    if type(value) is not str or not value or value in {".", ".."}:
        msg = f"{name} must be one basename"
        raise ValueError(msg)
    if "\x00" in value or "/" in value or "\\" in value:
        msg = f"{name} must be one basename"
        raise ValueError(msg)
    try:
        encoded = value.encode()
    except UnicodeEncodeError as exc:
        msg = f"{name} must be one basename"
        raise ValueError(msg) from exc
    if len(encoded) > 255:
        msg = f"{name} is too long"
        raise ValueError(msg)
    return value


def _require_choice(value: object, name: str, choices: tuple[str, ...]) -> str:
    if type(value) is not str or value not in choices:
        msg = f"{name} is unsupported"
        raise ValueError(msg)
    return value


def _require_identity(value: object, name: str) -> tuple[int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or any(type(item) is not int or item < 0 for item in value)
    ):
        msg = f"{name} is invalid"
        raise ValueError(msg)
    return value


def _require_digest(value: object, name: str = "digest") -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(char not in _HEX for char in value[7:])
    ):
        msg = f"{name} must be a sha256 digest"
        raise ValueError(msg)
    return value


def _coerce_root(value: object) -> Path:
    if not isinstance(value, (Path, str, os.PathLike)):
        msg = "state_root is invalid"
        raise TypeError(msg)
    try:
        root = Path(value).expanduser().absolute()
    except (TypeError, ValueError, RuntimeError) as exc:
        msg = "state_root is invalid"
        raise ValueError(msg) from exc
    if any(part in {".", ".."} for part in root.parts):
        msg = "state_root must not contain traversal components"
        raise ValueError(msg)
    if root == Path(root.root):
        msg = "state_root must be a private directory"
        raise ValueError(msg)
    return root


def _validate_owner(value: object) -> str:
    try:
        return _store._require_opaque_identifier(value, "owner")
    except (TypeError, ValueError) as exc:
        msg = "owner is invalid"
        raise ValueError(msg) from exc


@dataclass(frozen=True, slots=True)
class FilesystemEntry:
    """An immutable relative-name inventory item with no path disclosure."""

    name: str
    file_type: FileType
    uid: int
    mode: int
    nlink: int
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    digest: str | None

    def __post_init__(self) -> None:
        _require_basename(self.name, "name")
        _require_choice(self.file_type, "file_type", _FILE_TYPES)
        _require_int(self.uid, "uid")
        _require_int(self.mode, "mode")
        _require_int(self.nlink, "nlink", minimum=1)
        _require_int(self.device, "device")
        _require_int(self.inode, "inode", minimum=1)
        _require_int(self.size, "size")
        _require_int(self.mtime_ns, "mtime_ns")
        _require_int(self.ctime_ns, "ctime_ns")
        if self.file_type == "regular" and self.digest is not None:
            _require_digest(self.digest)
        elif self.digest is not None:
            msg = "special file must not contain a digest"
            raise ValueError(msg)

    @property
    def identity(self) -> tuple[int, int]:
        """Return the device/inode identity without exposing a path."""

        return (self.device, self.inode)


@dataclass(frozen=True, slots=True)
class FilesetInventory:
    """Complete root-direct-entry observation used for before/after checks."""

    root_identity: tuple[int, int] | None
    lifetime_gate_identity: tuple[int, int] | None
    marker_identity: tuple[int, int] | None
    ledger_identity: tuple[int, int] | None
    entries: tuple[FilesystemEntry, ...]

    def __post_init__(self) -> None:
        _require_identity(self.root_identity, "root_identity")
        _require_identity(self.lifetime_gate_identity, "lifetime_gate_identity")
        _require_identity(self.marker_identity, "marker_identity")
        _require_identity(self.ledger_identity, "ledger_identity")
        if not isinstance(self.entries, tuple):
            msg = "entries must be a tuple"
            raise TypeError(msg)
        if any(not isinstance(entry, FilesystemEntry) for entry in self.entries):
            msg = "entries must contain FilesystemEntry values"
            raise ValueError(msg)
        sorted_entries = tuple(sorted(self.entries, key=lambda entry: entry.name))
        object.__setattr__(self, "entries", sorted_entries)
        names = tuple(entry.name for entry in sorted_entries)
        if len(names) != len(set(names)):
            msg = "entries must be unique and sorted"
            raise ValueError(msg)

    def entry(self, name: str) -> FilesystemEntry | None:
        """Find one relative root entry by exact basename."""

        _require_basename(name, "name")
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    @property
    def primary_identity(self) -> tuple[int, int] | None:
        entry = self.entry(_store.DATABASE_FILENAME)
        return None if entry is None else entry.identity


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """Validated latest recovery-ledger record."""

    version: int
    sequence: int
    phase: LedgerPhase
    restore_generation: int
    recovery_epoch: int
    fencing_token_floor: int
    backup_digest: str
    actor: str
    audit_ref: str

    def __post_init__(self) -> None:
        _require_int(self.version, "version", minimum=1)
        if self.version != RECOVERY_LEDGER_VERSION:
            msg = "recovery ledger version is unsupported"
            raise ValueError(msg)
        _require_int(self.sequence, "sequence", minimum=1)
        _require_choice(self.phase, "phase", _LEDGER_PHASES)
        _require_int(self.restore_generation, "restore_generation")
        _require_int(self.recovery_epoch, "recovery_epoch")
        _require_int(self.fencing_token_floor, "fencing_token_floor")
        _require_digest(self.backup_digest, "backup_digest")
        _validate_owner(self.actor)
        _validate_owner(self.audit_ref)


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Stable operator-facing diagnosis with no raw path or provider payload."""

    observed_state: ObservedState
    confidence: Confidence
    owner: str | None
    safe_action: SafeAction
    forbidden_mutations: tuple[Mutation, ...]

    def __post_init__(self) -> None:
        _require_choice(self.observed_state, "observed_state", _OBSERVED_STATES)
        _require_choice(self.confidence, "confidence", _CONFIDENCES)
        if self.owner is not None:
            _validate_owner(self.owner)
        _require_choice(self.safe_action, "safe_action", _SAFE_ACTIONS)
        if not isinstance(self.forbidden_mutations, tuple):
            msg = "forbidden_mutations must be a tuple"
            raise TypeError(msg)
        if any(item not in _MUTATIONS for item in self.forbidden_mutations):
            msg = "forbidden_mutations contains an unsupported mutation"
            raise ValueError(msg)
        if len(self.forbidden_mutations) != len(set(self.forbidden_mutations)):
            msg = "forbidden_mutations must not contain duplicates"
            raise ValueError(msg)
        if tuple(item for item in _MUTATIONS if item in self.forbidden_mutations) != (
            self.forbidden_mutations
        ):
            msg = "forbidden_mutations must use canonical order"
            raise ValueError(msg)


def _metadata_signature(metadata: os.stat_result) -> tuple[int, ...]:
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


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _file_type(mode: int) -> FileType:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode):
        return "block_device"
    if stat.S_ISCHR(mode):
        return "char_device"
    return "unknown"


def _unsafe_regular(metadata: os.stat_result) -> bool:
    return (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    )


def _read_only_open_flags(*, directory: bool) -> int:
    try:
        flags = _store._open_flags(directory=directory, writable=False)
    except _store.StoreError as exc:
        raise StateFilesystemError("read-only open flags are unavailable") from exc
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if nonblock == 0:
        raise StateFilesystemError("non-blocking read-only open is unavailable")
    return flags | nonblock


def _close_temporary_fd(
    fd: int,
    expected_identity: tuple[int, int] | None,
    label: str,
) -> None:
    """Close a temporary descriptor with an identity-checked retry."""

    try:
        os.close(fd)
    except _CLEANUP_EXCEPTION:
        try:
            metadata = os.fstat(fd)
        except OSError as status_error:
            if status_error.errno == errno.EBADF:
                raise StateFilesystemError(
                    f"{label} close status is unknown"
                ) from status_error
            raise StateFilesystemError(
                f"{label} close status is unknown"
            ) from status_error
        if (
            expected_identity is not None
            and (
                metadata.st_dev,
                metadata.st_ino,
            )
            != expected_identity
        ):
            raise StateFilesystemError(f"{label} descriptor was reused")
        try:
            os.close(fd)
        except _CLEANUP_EXCEPTION as retry_error:
            raise StateFilesystemError(f"{label} cannot be closed") from retry_error
        raise StateFilesystemError(f"{label} close failed before retry")


def _close_temporary_connection(
    connection: sqlite3.Connection,
    label: str,
) -> None:
    """Close a temporary SQLite connection, retrying an idempotent failure."""

    try:
        connection.close()
    except _CLEANUP_EXCEPTION:
        try:
            connection.close()
        except _CLEANUP_EXCEPTION as retry_error:
            raise StateFilesystemError(f"{label} cannot be closed") from retry_error


def _deserialize_database_from_fd(fd: int) -> sqlite3.Connection:
    """Read one supported SQLite image from a validated descriptor."""

    try:
        before = os.fstat(fd)
    except OSError as exc:
        raise StateFilesystemError("database descriptor is unavailable") from exc
    if _unsafe_regular(before) or before.st_size > MAX_DATABASE_BYTES:
        raise StateFilesystemError("database descriptor is unsafe or too large")
    chunks: list[bytes] = []
    offset = 0
    while offset < before.st_size:
        try:
            chunk = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
        except OSError as exc:
            raise StateFilesystemError("database descriptor cannot be read") from exc
        if not chunk:
            raise UnstableSnapshotError("database descriptor ended while reading")
        chunks.append(chunk)
        offset += len(chunk)
    try:
        after = os.fstat(fd)
    except OSError as exc:
        raise UnstableSnapshotError(
            "database descriptor changed while reading"
        ) from exc
    if _metadata_signature(before) != _metadata_signature(after):
        raise UnstableSnapshotError("database descriptor changed while reading")
    image = bytearray(b"".join(chunks))
    if len(image) < 20 or image[:16] != b"SQLite format 3\x00":
        raise _store.StoreIntegrityError("SQLite database header is invalid")
    version_pair = (image[18], image[19])
    if version_pair == (2, 2):
        # A copied WAL-mode image cannot be opened as ``:memory:`` without a
        # shared-memory sidecar.  Reclassify only the in-memory copy as the
        # default rollback-journal mode; the source fd remains untouched.
        image[18] = 1
        image[19] = 1
    elif version_pair != (1, 1):
        raise _store.StoreIntegrityError(
            "SQLite database header version is unsupported"
        )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            ":memory:",
            uri=False,
            timeout=0,
            isolation_level=None,
        )
        connection.deserialize(bytes(image))
    except (AttributeError, sqlite3.DatabaseError) as exc:
        if connection is not None:
            _close_temporary_connection(connection, "SQLite in-memory database")
        raise _store.StoreIntegrityError(
            "SQLite in-memory database deserialization failed"
        ) from exc
    return connection


class StateFilesystem:
    """Held no-follow root/gate descriptors and immutable fileset reader."""

    def __init__(
        self,
        state_root: Path,
        *,
        marker_name: str,
        ledger_name: str,
        busy_timeout_ms: int,
    ) -> None:
        self.state_root = state_root
        self.marker_name = _require_basename(marker_name, "marker_name")
        self.ledger_name = _require_basename(ledger_name, "ledger_name")
        self.busy_timeout_ms = busy_timeout_ms
        self._root_fd: int | None = None
        self._root_identity: tuple[int, int] | None = None
        self._root_signature: tuple[int, ...] | None = None
        self._parent_fd: int | None = None
        self._gate_fd: int | None = None
        self._gate_identity: tuple[int, int] | None = None
        self._marker_fd: int | None = None
        self._marker_identity: tuple[int, int] | None = None
        self._marker_signature: tuple[int, ...] | None = None
        self._marker_state: str | None = None
        self._marker_shared = False
        self._marker_exclusive = False
        self._orphan_fds: list[tuple[int, tuple[int, int] | None, str]] = []
        self._closed = False

    @classmethod
    def open_existing(
        cls,
        state_root: Path,
        *,
        marker_name: str,
        ledger_name: str,
        busy_timeout_ms: int = 0,
    ) -> StateFilesystem:
        if type(busy_timeout_ms) is not int or not 0 <= busy_timeout_ms <= 30_000:
            msg = "busy_timeout_ms must be between 0 and 30000"
            raise ValueError(msg)
        if marker_name == ledger_name:
            msg = "marker_name and ledger_name must differ"
            raise ValueError(msg)
        marker_name = _require_basename(marker_name, "marker_name")
        if marker_name != _store.WRITER_MARKER_FILENAME:
            raise ValueError("marker_name is not canonical")
        ledger_name = _require_basename(ledger_name, "ledger_name")
        root = _coerce_root(state_root)
        instance = cls(
            root,
            marker_name=marker_name,
            ledger_name=ledger_name,
            busy_timeout_ms=busy_timeout_ms,
        )
        try:
            try:
                root_before = os.stat(root, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise StateFilesystemError("state root is missing") from exc
            except OSError as exc:
                raise StateFilesystemError("state root cannot be inspected") from exc
            if (
                not stat.S_ISDIR(root_before.st_mode)
                or root_before.st_uid != os.getuid()
                or stat.S_IMODE(root_before.st_mode) != 0o700
            ):
                raise UnsafeFilesystemError("state root is unsafe")
            instance._fault("after_root_lstat")
            try:
                root_fd = _store._open_state_root(root)
            except _store.StoreError as exc:
                raise StateFilesystemError("state root cannot be opened") from exc
            instance._root_fd = root_fd
            metadata = os.fstat(root_fd)
            if _metadata_signature(metadata) != _metadata_signature(root_before):
                raise UnstableSnapshotError("state root changed while opening")
            instance._root_identity = (metadata.st_dev, metadata.st_ino)
            instance._root_signature = _metadata_signature(metadata)
            instance._open_existing_gate()
            instance._open_existing_marker()
            instance._assert_root_identity()
            return instance
        except BaseException:
            instance.close()
            raise

    def _fault(self, point: str) -> None:
        """Deterministic process-test seam; production implementation is a no-op."""

    def _assert_open(self) -> int:
        if self._closed or self._root_fd is None:
            raise StateFilesystemError("state filesystem is closed")
        return self._root_fd

    def _assert_root_identity(self) -> None:
        root_fd = self._assert_open()
        expected = self._root_identity
        expected_signature = self._root_signature
        if expected is None or expected_signature is None:
            raise StateFilesystemError("state root identity is unavailable")
        try:
            fd_metadata = os.fstat(root_fd)
            path_metadata = os.stat(self.state_root, follow_symlinks=False)
        except OSError as exc:
            raise UnstableSnapshotError("state root identity is unavailable") from exc
        if (
            _metadata_signature(fd_metadata) != expected_signature
            or _metadata_signature(path_metadata) != expected_signature
            or (fd_metadata.st_dev, fd_metadata.st_ino) != expected
        ):
            raise UnstableSnapshotError("state root changed while observed")

    def _open_existing_gate(self) -> None:
        root_fd = self._assert_open()
        directory_flags = _read_only_open_flags(directory=True)
        try:
            parent_fd = os.open("..", directory_flags, dir_fd=root_fd)
        except OSError as exc:
            raise StateFilesystemError("state parent cannot be opened") from exc
        self._parent_fd = parent_fd
        try:
            _store._validate_directory_fd(parent_fd, state_root=False)
        except _store.StoreError as exc:
            raise StateFilesystemError("state parent is unsafe") from exc
        gate_name = _store.LIFETIME_GATE_FILENAME
        flags = _read_only_open_flags(directory=False)
        try:
            before = os.stat(gate_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            self._gate_identity = None
            return
        except OSError as exc:
            raise StateFilesystemError("lifetime gate cannot be inspected") from exc
        if _unsafe_regular(before):
            raise UnsafeFilesystemError("lifetime gate is unsafe")
        self._fault("after_gate_lstat")
        try:
            gate_fd = os.open(gate_name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise StateFilesystemError("lifetime gate cannot be opened") from exc
        try:
            metadata = os.fstat(gate_fd)
            after = os.stat(gate_name, dir_fd=parent_fd, follow_symlinks=False)
            if _metadata_signature(metadata) != _metadata_signature(
                before
            ) or _metadata_signature(metadata) != _metadata_signature(after):
                raise UnstableSnapshotError("lifetime gate changed while opening")
            try:
                fcntl.flock(gate_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise WriterActiveError(
                        "lifetime gate is held exclusively"
                    ) from exc
                raise StateFilesystemError("lifetime gate cannot be locked") from exc
            self._gate_fd = gate_fd
            self._gate_identity = (metadata.st_dev, metadata.st_ino)
        except BaseException:
            self._close_owned_temporary_fd(
                gate_fd,
                _metadata_identity(before),
                "lifetime gate",
            )
            raise

    def _open_existing_marker(self) -> None:
        """Hold a shared marker lock for a reader without creating it."""

        root_fd = self._assert_open()
        marker = self._lstat(self.marker_name)
        if marker is None:
            return
        if _unsafe_regular(marker):
            raise UnsafeFilesystemError("writer marker is unsafe")
        self._fault("after_marker_lstat")
        try:
            marker_fd = os.open(
                self.marker_name,
                _read_only_open_flags(directory=False),
                dir_fd=root_fd,
            )
        except OSError as exc:
            raise StateFilesystemError("writer marker cannot be opened") from exc
        try:
            metadata = os.fstat(marker_fd)
            after = self._lstat(self.marker_name)
            if (
                after is None
                or _metadata_signature(metadata) != _metadata_signature(marker)
                or _metadata_signature(metadata) != _metadata_signature(after)
            ):
                raise UnstableSnapshotError("writer marker changed while opening")
            try:
                fcntl.flock(marker_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise WriterActiveError(
                        "writer marker is held exclusively"
                    ) from exc
                raise StateFilesystemError("writer marker cannot be locked") from exc
            try:
                marker_state = _store._read_writer_marker_state(marker_fd)
            except _store.StoreError as exc:
                raise UnsafeFilesystemError("writer marker content is invalid") from exc
            self._marker_fd = marker_fd
            self._marker_identity = (metadata.st_dev, metadata.st_ino)
            self._marker_signature = _metadata_signature(metadata)
            self._marker_state = marker_state
            self._marker_shared = True
        except BaseException:
            self._close_owned_temporary_fd(
                marker_fd,
                _metadata_identity(marker),
                "writer marker",
            )
            raise

    def _lstat(self, name: str) -> os.stat_result | None:
        root_fd = self._assert_open()
        try:
            return os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StateFilesystemError("state entry cannot be inspected") from exc

    def _assert_marker_identity(self) -> None:
        marker_fd = self._marker_fd
        expected_identity = self._marker_identity
        expected_signature = self._marker_signature
        if marker_fd is None:
            if expected_identity is not None or self._marker_state is not None:
                raise UnstableSnapshotError("writer marker descriptor is unavailable")
            return
        if expected_identity is None or expected_signature is None:
            raise UnstableSnapshotError("writer marker identity is unavailable")
        try:
            fd_metadata = os.fstat(marker_fd)
        except OSError as exc:
            raise UnstableSnapshotError(
                "writer marker descriptor is unavailable"
            ) from exc
        path_metadata = self._lstat(self.marker_name)
        if path_metadata is None:
            raise UnstableSnapshotError("writer marker disappeared while observed")
        if (
            (fd_metadata.st_dev, fd_metadata.st_ino) != expected_identity
            or (path_metadata.st_dev, path_metadata.st_ino) != expected_identity
            or _metadata_signature(fd_metadata) != expected_signature
            or _metadata_signature(path_metadata) != expected_signature
        ):
            raise UnstableSnapshotError("writer marker changed while observed")
        try:
            state = _store._read_writer_marker_state(marker_fd)
        except _store.StoreError as exc:
            raise UnstableSnapshotError("writer marker content changed") from exc
        if state != self._marker_state:
            raise UnstableSnapshotError("writer marker state changed while observed")

    def _close_owned_temporary_fd(
        self,
        fd: int,
        expected_identity: tuple[int, int] | None,
        label: str,
    ) -> None:
        try:
            _close_temporary_fd(fd, expected_identity, label)
        except StateFilesystemError as close_error:
            try:
                metadata = os.fstat(fd)
            except OSError as status_error:
                if status_error.errno == errno.EBADF:
                    raise close_error
                raise StateFilesystemError(
                    f"{label} descriptor status is unknown"
                ) from status_error
            if (
                expected_identity is not None
                and (
                    metadata.st_dev,
                    metadata.st_ino,
                )
                != expected_identity
            ):
                raise StateFilesystemError(f"{label} descriptor was reused")
            if not any(existing_fd == fd for existing_fd, _, _ in self._orphan_fds):
                self._orphan_fds.append((fd, expected_identity, label))
            raise

    def _open_regular(self, name: str) -> tuple[int, os.stat_result]:
        root_fd = self._assert_open()
        _require_basename(name, "name")
        before = self._lstat(name)
        if before is None:
            raise StateFilesystemError("state entry is missing")
        if _unsafe_regular(before):
            raise UnsafeFilesystemError("state entry is unsafe")
        if name == _store.DATABASE_FILENAME:
            self._fault("after_db_lstat")
        elif name in {
            f"{_store.DATABASE_FILENAME}{suffix}" for suffix in _SIDECAR_SUFFIXES
        }:
            self._fault("after_sidecar_lstat")
        flags = _read_only_open_flags(directory=False)
        try:
            fd = os.open(name, flags, dir_fd=root_fd)
        except OSError as exc:
            raise StateFilesystemError("state entry cannot be opened") from exc
        try:
            metadata = os.fstat(fd)
            after = self._lstat(name)
            if after is None or _metadata_signature(metadata) != _metadata_signature(
                after
            ):
                raise UnstableSnapshotError("state entry changed while opening")
            if _metadata_signature(metadata) != _metadata_signature(before):
                raise UnstableSnapshotError("state entry changed while opening")
            return fd, metadata
        except BaseException:
            self._close_owned_temporary_fd(
                fd,
                _metadata_identity(before),
                "state entry",
            )
            raise

    def open_existing_regular(self, name: str) -> int:
        """Open one existing owner-only regular file read-only and no-follow."""

        fd, _ = self._open_regular(name)
        return fd

    def _digest_regular(self, name: str, metadata: os.stat_result) -> str:
        fd, opened = self._open_regular(name)
        digest = hashlib.sha256()
        try:
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after_fd = os.fstat(fd)
            after_path = self._lstat(name)
            if (
                after_path is None
                or _metadata_signature(opened) != _metadata_signature(after_fd)
                or _metadata_signature(opened) != _metadata_signature(after_path)
                or _metadata_signature(metadata) != _metadata_signature(after_path)
            ):
                raise UnstableSnapshotError("state entry changed while reading")
        except OSError as exc:
            raise StateFilesystemError("state entry cannot be read") from exc
        finally:
            self._close_owned_temporary_fd(
                fd,
                _metadata_identity(opened),
                "state entry digest",
            )
        return f"sha256:{digest.hexdigest()}"

    def _entry(self, name: str, metadata: os.stat_result) -> FilesystemEntry:
        kind = _file_type(metadata.st_mode)
        if kind != "regular" and name in self._known_state_names:
            raise UnsafeFilesystemError("known state entry has an unsafe type")
        digest: str | None = None
        if kind == "regular":
            if _unsafe_regular(metadata):
                raise UnsafeFilesystemError("state file is unsafe")
            digest = self._digest_regular(name, metadata)
        elif kind != "directory":
            raise UnsafeFilesystemError("state entry has an unsafe type")
        elif metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise UnsafeFilesystemError("state directory is unsafe")
        return FilesystemEntry(
            name=name,
            file_type=kind,
            uid=metadata.st_uid,
            mode=stat.S_IMODE(metadata.st_mode),
            nlink=metadata.st_nlink,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
            digest=digest,
        )

    @property
    def _known_state_names(self) -> frozenset[str]:
        return frozenset(
            {
                _store.DATABASE_FILENAME,
                *(
                    f"{_store.DATABASE_FILENAME}{suffix}"
                    for suffix in _SIDECAR_SUFFIXES
                ),
                self.marker_name,
                self.ledger_name,
            }
        )

    @property
    def marker_state(self) -> str | None:
        """Return the validated stable-marker lifecycle state, if present."""

        return self._marker_state

    def inventory(self) -> FilesetInventory:
        """Return all root-direct entries, including unrelated names."""

        root_fd = self._assert_open()
        self._assert_root_identity()
        self._assert_marker_identity()
        names: list[str]
        try:
            names = sorted(os.listdir(root_fd))
        except OSError as exc:
            raise StateFilesystemError("state root cannot be listed") from exc
        entries: list[FilesystemEntry] = []
        for name in names:
            metadata = self._lstat(name)
            if metadata is None:
                raise UnstableSnapshotError("state entry disappeared while listing")
            entries.append(self._entry(name, metadata))
        marker = next(
            (entry for entry in entries if entry.name == self.marker_name), None
        )
        ledger = next(
            (entry for entry in entries if entry.name == self.ledger_name), None
        )
        self._assert_root_identity()
        gate_identity = self._gate_identity
        parent_fd = self._parent_fd
        if parent_fd is None:
            raise StateFilesystemError("state parent is unavailable")
        try:
            gate_path_metadata = os.stat(
                _store.LIFETIME_GATE_FILENAME,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            current_gate_identity = None
        except OSError as exc:
            raise UnstableSnapshotError("lifetime gate cannot be observed") from exc
        else:
            if _unsafe_regular(gate_path_metadata):
                raise UnsafeFilesystemError("lifetime gate is unsafe")
            current_gate_identity = (
                gate_path_metadata.st_dev,
                gate_path_metadata.st_ino,
            )
        if current_gate_identity != gate_identity:
            raise UnstableSnapshotError("lifetime gate changed while observing")
        if self._gate_fd is not None:
            gate_fd_metadata = os.fstat(self._gate_fd)
            if _metadata_signature(gate_fd_metadata) != _metadata_signature(
                gate_path_metadata
            ):
                raise UnstableSnapshotError("lifetime gate changed while observing")
        self._assert_marker_identity()
        return FilesetInventory(
            root_identity=self._root_identity,
            lifetime_gate_identity=gate_identity,
            marker_identity=None if marker is None else marker.identity,
            ledger_identity=None if ledger is None else ledger.identity,
            entries=tuple(entries),
        )

    def assert_identity(self, inventory: FilesetInventory) -> None:
        """Fail closed unless the complete root-direct fileset is unchanged."""

        if not isinstance(inventory, FilesetInventory):
            msg = "inventory is invalid"
            raise TypeError(msg)
        self._assert_root_identity()
        self._fault("before_final_inventory")
        current = self.inventory()
        if current != inventory:
            raise UnstableSnapshotError("state fileset changed while observed")

    def try_marker_exclusive(self) -> bool:
        """Try a non-blocking exclusive lock on an existing stable marker."""

        if self._marker_fd is not None and self._marker_exclusive:
            self._assert_marker_identity()
            return True
        if self._marker_fd is not None and self._marker_shared:
            marker_fd = self._marker_fd
            expected_identity = self._marker_identity
            expected_signature = self._marker_signature
            expected_state = self._marker_state
            self._assert_marker_identity()
            self._marker_shared = False
            try:
                fcntl.flock(marker_fd, fcntl.LOCK_UN)
            except OSError as exc:
                self._marker_shared = True
                raise StateFilesystemError("writer marker cannot be released") from exc
            marker_path = self._lstat(self.marker_name)
            if (
                marker_path is None
                or expected_identity is None
                or expected_signature is None
                or (marker_path.st_dev, marker_path.st_ino) != expected_identity
                or _metadata_signature(marker_path) != expected_signature
            ):
                _close_temporary_fd(
                    marker_fd,
                    expected_identity,
                    "writer marker",
                )
                self._marker_fd = None
                self._marker_identity = None
                self._marker_signature = None
                self._marker_state = None
                raise UnstableSnapshotError("writer marker changed before reacquire")
            fd: int | None = None
            retained_old_descriptor = False
            try:
                fd, metadata = self._open_regular(self.marker_name)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                        _close_temporary_fd(fd, expected_identity, "writer marker")
                        self._marker_fd = marker_fd
                        try:
                            fcntl.flock(marker_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        except OSError as reacquire_exc:
                            _close_temporary_fd(
                                marker_fd,
                                expected_identity,
                                "writer marker",
                            )
                            self._marker_fd = None
                            self._marker_identity = None
                            self._marker_signature = None
                            self._marker_state = None
                            raise UnstableSnapshotError(
                                "writer marker cannot be reacquired"
                            ) from reacquire_exc
                        self._marker_shared = True
                        self._marker_exclusive = False
                        self._marker_state = expected_state
                        self._assert_marker_identity()
                        return False
                    raise StateFilesystemError(
                        "writer marker cannot be locked"
                    ) from exc
                if (
                    expected_identity is None
                    or expected_signature is None
                    or (metadata.st_dev, metadata.st_ino) != expected_identity
                    or _metadata_signature(metadata) != expected_signature
                ):
                    raise UnstableSnapshotError(
                        "writer marker changed while reacquiring"
                    )
                try:
                    state = _store._read_writer_marker_state(fd)
                except _store.StoreError as exc:
                    raise UnstableSnapshotError(
                        "writer marker content changed while reacquiring"
                    ) from exc
                if state != expected_state:
                    raise UnstableSnapshotError(
                        "writer marker state changed while reacquiring"
                    )
                try:
                    old_metadata = os.fstat(marker_fd)
                except OSError as status_error:
                    raise StateFilesystemError(
                        "writer marker old descriptor is unavailable"
                    ) from status_error
                if (
                    expected_identity is None
                    or _metadata_identity(old_metadata) != expected_identity
                ):
                    raise StateFilesystemError(
                        "writer marker old descriptor was reused"
                    )
                try:
                    os.close(marker_fd)
                except _CLEANUP_EXCEPTION as exc:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except _CLEANUP_EXCEPTION:
                        pass
                    try:
                        _close_temporary_fd(
                            fd,
                            expected_identity,
                            "writer marker handoff",
                        )
                    except StateFilesystemError:
                        try:
                            metadata = os.fstat(fd)
                        except OSError as status_error:
                            if status_error.errno != errno.EBADF:
                                self._orphan_fds.append(
                                    (
                                        fd,
                                        expected_identity,
                                        "writer marker handoff",
                                    )
                                )
                        else:
                            if (
                                expected_identity is None
                                or _metadata_identity(metadata) == expected_identity
                            ):
                                self._orphan_fds.append(
                                    (
                                        fd,
                                        expected_identity,
                                        "writer marker handoff",
                                    )
                                )
                    self._marker_fd = marker_fd
                    self._marker_identity = expected_identity
                    self._marker_signature = expected_signature
                    self._marker_state = expected_state
                    self._marker_shared = False
                    self._marker_exclusive = False
                    fd = None
                    marker_fd = -1
                    retained_old_descriptor = True
                    raise StateFilesystemError(
                        "writer marker handoff cannot close old descriptor"
                    ) from exc
                marker_fd = -1
                self._marker_fd = fd
                self._marker_identity = expected_identity
                self._marker_signature = expected_signature
                self._marker_state = state
                self._marker_shared = False
                self._marker_exclusive = True
                self._assert_marker_identity()
                return True
            except BaseException:
                if retained_old_descriptor:
                    raise
                if fd is not None:
                    _close_temporary_fd(fd, expected_identity, "writer marker")
                if marker_fd != -1:
                    _close_temporary_fd(
                        marker_fd,
                        expected_identity,
                        "writer marker",
                    )
                self._marker_fd = None
                self._marker_identity = None
                self._marker_signature = None
                self._marker_state = None
                self._marker_shared = False
                self._marker_exclusive = False
                raise
        marker = self._lstat(self.marker_name)
        if marker is None:
            return False
        return False

    def close(self) -> None:
        if (
            self._closed
            and all(
                fd is None
                for fd in (
                    self._marker_fd,
                    self._gate_fd,
                    self._root_fd,
                    self._parent_fd,
                )
            )
            and not self._orphan_fds
        ):
            return
        self._closed = True
        first_error: BaseException | None = None

        def remember(error: BaseException) -> None:
            nonlocal first_error
            if first_error is None:
                first_error = error

        def remember_current_exception() -> None:
            error = sys.exc_info()[1]
            if error is None:
                error = RuntimeError("descriptor cleanup failed")
            remember(error)

        def attempt_cleanup(action: Callable[[], None]) -> None:
            try:
                action()
            except _CLEANUP_EXCEPTION:
                remember_current_exception()

        marker_fd = self._marker_fd
        if marker_fd is not None:
            if self._marker_shared or self._marker_exclusive:
                try:
                    fcntl.flock(marker_fd, fcntl.LOCK_UN)
                except _CLEANUP_EXCEPTION:
                    remember_current_exception()
                else:
                    self._marker_shared = False
                    self._marker_exclusive = False
            try:
                os.close(marker_fd)
            except _CLEANUP_EXCEPTION:
                remember_current_exception()
            else:
                self._marker_fd = None
                self._marker_identity = None
                self._marker_signature = None
                self._marker_state = None
                self._marker_shared = False
                self._marker_exclusive = False

        gate_fd = self._gate_fd
        if gate_fd is not None:
            attempt_cleanup(lambda: fcntl.flock(gate_fd, fcntl.LOCK_UN))
            try:
                os.close(gate_fd)
            except _CLEANUP_EXCEPTION:
                remember_current_exception()
            else:
                self._gate_fd = None

        root_fd = self._root_fd
        if root_fd is not None:
            try:
                os.close(root_fd)
            except _CLEANUP_EXCEPTION:
                remember_current_exception()
            else:
                self._root_fd = None
                self._root_identity = None
                self._root_signature = None

        parent_fd = self._parent_fd
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except _CLEANUP_EXCEPTION:
                remember_current_exception()
            else:
                self._parent_fd = None
        remaining_orphans: list[tuple[int, tuple[int, int] | None, str]] = []
        for orphan_fd, expected_identity, label in self._orphan_fds:
            try:
                os.close(orphan_fd)
            except _CLEANUP_EXCEPTION as first_error:
                try:
                    metadata = os.fstat(orphan_fd)
                except OSError as status_error:
                    if status_error.errno == errno.EBADF:
                        continue
                    remember(status_error)
                    remaining_orphans.append((orphan_fd, expected_identity, label))
                    continue
                if (
                    expected_identity is not None
                    and (
                        metadata.st_dev,
                        metadata.st_ino,
                    )
                    != expected_identity
                ):
                    remember(UnstableSnapshotError(f"{label} descriptor was reused"))
                    continue
                try:
                    os.close(orphan_fd)
                except _CLEANUP_EXCEPTION:
                    remember(first_error)
                    remaining_orphans.append((orphan_fd, expected_identity, label))
        self._orphan_fds = remaining_orphans
        if first_error is not None:
            if isinstance(first_error, Exception):
                raise StateFilesystemError(
                    "state filesystem close failed"
                ) from first_error
            raise first_error

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


def _json_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = "recovery ledger contains a duplicate field"
            raise ValueError(msg)
        result[key] = value
    return result


def _ledger_records(raw: bytes) -> list[dict[str, object]]:
    if not raw or len(raw) > MAX_LEDGER_BYTES:
        raise LedgerReadError("recovery ledger is empty or too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerReadError("recovery ledger is not UTF-8") from exc
    if "\r" in text or not text.endswith("\n"):
        raise LedgerReadError("recovery ledger has a non-canonical newline boundary")
    lines = text.split("\n")[:-1]
    if any(not line or line.strip() != line for line in lines):
        raise LedgerReadError("recovery ledger contains a blank or padded record")
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            item = json.loads(line, object_pairs_hook=_json_object_pairs)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise LedgerReadError("recovery ledger contains a partial record") from exc
        if not isinstance(item, dict):
            raise LedgerReadError("recovery ledger record is not an object")
        records.append(item)
    if not records:
        raise LedgerReadError("recovery ledger has no records")
    return records


def _ledger_snapshot(record: dict[str, object]) -> LedgerSnapshot:
    fields = {
        "version",
        "sequence",
        "phase",
        "restore_generation",
        "recovery_epoch",
        "fencing_token_floor",
        "backup_digest",
        "actor",
        "audit_ref",
    }
    if set(record) != fields:
        raise LedgerReadError("recovery ledger record fields are invalid")
    try:
        return LedgerSnapshot(
            version=record["version"],  # type: ignore[arg-type]
            sequence=record["sequence"],  # type: ignore[arg-type]
            phase=record["phase"],  # type: ignore[arg-type]
            restore_generation=record["restore_generation"],  # type: ignore[arg-type]
            recovery_epoch=record["recovery_epoch"],  # type: ignore[arg-type]
            fencing_token_floor=record["fencing_token_floor"],  # type: ignore[arg-type]
            backup_digest=record["backup_digest"],  # type: ignore[arg-type]
            actor=record["actor"],  # type: ignore[arg-type]
            audit_ref=record["audit_ref"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise LedgerReadError("recovery ledger record is invalid") from exc


class RecoveryLedgerReader:
    """Read an existing canonical JSON ledger without opening it for writing."""

    def read(self, filesystem: StateFilesystem) -> LedgerSnapshot | None:
        if not isinstance(filesystem, StateFilesystem):
            msg = "filesystem is invalid"
            raise TypeError(msg)
        before = filesystem.inventory()
        entry = before.entry(filesystem.ledger_name)
        if entry is None:
            filesystem._fault("after_ledger_absence")
            filesystem.assert_identity(before)
            return None
        if entry.file_type != "regular" or entry.digest is None:
            raise LedgerReadError("recovery ledger is not a safe regular file")
        fd = filesystem.open_existing_regular(filesystem.ledger_name)
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_LEDGER_BYTES:
                    raise LedgerReadError("recovery ledger is too large")
                chunks.append(chunk)
        except OSError as exc:
            raise LedgerReadError("recovery ledger cannot be read") from exc
        finally:
            filesystem._close_owned_temporary_fd(
                fd,
                entry.identity,
                "recovery ledger",
            )
        raw = b"".join(chunks)
        records = _ledger_records(raw)
        snapshots = [_ledger_snapshot(record) for record in records]
        previous: LedgerSnapshot | None = None
        for snapshot in snapshots:
            if previous is None and snapshot.sequence != 1:
                raise LedgerReadError("recovery ledger sequence does not start at one")
            if previous is None and snapshot.phase != "RESTORE_PREPARED":
                raise LedgerReadError(
                    "recovery ledger generation does not start prepared"
                )
            if previous is not None:
                if snapshot.sequence != previous.sequence + 1:
                    raise LedgerReadError("recovery ledger sequence is not monotonic")
                if previous.phase in _TERMINAL_LEDGER_PHASES:
                    if (
                        snapshot.phase != "RESTORE_PREPARED"
                        or snapshot.restore_generation <= previous.restore_generation
                    ):
                        raise LedgerReadError(
                            "recovery ledger terminal transition is invalid"
                        )
                else:
                    if snapshot.restore_generation != previous.restore_generation:
                        raise LedgerReadError(
                            "recovery ledger generation changed before terminal phase"
                        )
                    if snapshot.phase not in _LEDGER_TRANSITIONS[previous.phase]:
                        raise LedgerReadError(
                            "recovery ledger phase transition is invalid"
                        )
                if snapshot.recovery_epoch < previous.recovery_epoch:
                    raise LedgerReadError("recovery ledger epoch moved backwards")
                if snapshot.fencing_token_floor < previous.fencing_token_floor:
                    raise LedgerReadError("recovery ledger floor moved backwards")
                if (
                    snapshot.restore_generation == previous.restore_generation
                    and snapshot.backup_digest != previous.backup_digest
                ):
                    raise LedgerReadError(
                        "recovery ledger digest changed in one generation"
                    )
            previous = snapshot
        filesystem.assert_identity(before)
        return previous


def _all_forbidden() -> tuple[Mutation, ...]:
    return _MUTATIONS


def _assert_stable(
    filesystem: StateFilesystem,
    inventory: FilesetInventory,
) -> None:
    try:
        filesystem.assert_identity(inventory)
    except (StateFilesystemError, OSError) as exc:
        raise UnstableSnapshotError("state fileset changed while observed") from exc


def _report(
    state: ObservedState,
    confidence: Confidence,
    owner: str | None,
    action: SafeAction,
    forbidden: tuple[Mutation, ...] | None = None,
) -> DoctorReport:
    return DoctorReport(
        observed_state=state,
        confidence=confidence,
        owner=owner,
        safe_action=action,
        forbidden_mutations=_all_forbidden() if forbidden is None else forbidden,
    )


def _status_report(
    observation: _store.ExistingOperationObservation,
    *,
    marker_present: bool,
) -> DoctorReport:
    status = observation.status
    owner = observation.owner
    if status == "INTENT":
        state: ObservedState = "INTENT_ONLY"
        action: SafeAction = "CLAIM"
        forbidden: tuple[Mutation, ...] = tuple(
            item for item in _MUTATIONS if item != "claim"
        )
        confidence: Confidence = "HIGH"
    elif status in {
        "FENCE_PENDING",
        "FENCE_RESERVATION_STARTED",
        "CLAIMED",
        "EFFECT_PREPARED",
        "UNKNOWN_EFFECT",
        "UNKNOWN",
    }:
        state = "UNKNOWN_EFFECT"
        action = "QUERY_PROVIDER_THEN_RESOLVE"
        forbidden = _all_forbidden()
        confidence = "LOW"
    elif status == "RECEIPTED":
        state = "RECEIPTED"
        action = "VERIFY_RECEIPT_THEN_COMPLETE"
        forbidden = tuple(item for item in _MUTATIONS if item != "complete")
        confidence = "HIGH"
    elif status == "COMPLETED":
        state = "COMPLETED"
        action = "NONE"
        forbidden = _all_forbidden()
        confidence = "HIGH"
    elif status == "CLEANED":
        state = "CLEANED"
        action = "NONE"
        forbidden = _all_forbidden()
        confidence = "HIGH"
    elif status == "RESTORE_INCOMPLETE":
        state = "RESTORE_INCOMPLETE"
        action = "OPERATOR_REVIEW"
        forbidden = _all_forbidden()
        confidence = "HIGH"
    else:
        raise ValueError("unsupported operation status")
    if not marker_present:
        confidence = "MEDIUM" if confidence == "HIGH" else "LOW"
        action = "OPERATOR_REVIEW"
        forbidden = _all_forbidden()
    return _report(state, confidence, owner, action, forbidden)


class ReadOnlyDoctor:
    """Inspect one operation while preserving the complete fileset."""

    def __init__(
        self,
        *,
        filesystem: StateFilesystem | None = None,
        marker_name: str | None = None,
        ledger_name: str | None = None,
    ) -> None:
        if filesystem is not None and not isinstance(filesystem, StateFilesystem):
            msg = "filesystem is invalid"
            raise TypeError(msg)
        if marker_name is not None:
            marker_name = _require_basename(marker_name, "marker_name")
            if marker_name != _store.WRITER_MARKER_FILENAME:
                raise ValueError("marker_name is not canonical")
        if ledger_name is not None:
            ledger_name = _require_basename(ledger_name, "ledger_name")
        if (
            marker_name is not None
            and ledger_name is not None
            and marker_name == ledger_name
        ):
            msg = "marker_name and ledger_name must differ"
            raise ValueError(msg)
        if filesystem is not None:
            if marker_name is not None and marker_name != filesystem.marker_name:
                raise ValueError("marker_name does not match filesystem")
            if ledger_name is not None and ledger_name != filesystem.ledger_name:
                raise ValueError("ledger_name does not match filesystem")
        self._filesystem = filesystem
        self._marker_name = marker_name
        self._ledger_name = ledger_name

    def _layout(
        self, marker_name: str | None, ledger_name: str | None
    ) -> tuple[str, str]:
        if self._filesystem is not None:
            if marker_name is not None and marker_name != self._filesystem.marker_name:
                raise ValueError("marker_name does not match filesystem")
            if ledger_name is not None and ledger_name != self._filesystem.ledger_name:
                raise ValueError("ledger_name does not match filesystem")
            if marker_name is None:
                marker_name = self._filesystem.marker_name
            if ledger_name is None:
                ledger_name = self._filesystem.ledger_name
        marker = self._marker_name if marker_name is None else marker_name
        ledger = self._ledger_name if ledger_name is None else ledger_name
        if marker is None or ledger is None:
            msg = "marker_name and ledger_name are required layout inputs"
            raise ValueError(msg)
        marker = _require_basename(marker, "marker_name")
        if marker != _store.WRITER_MARKER_FILENAME:
            raise ValueError("marker_name is not canonical")
        ledger = _require_basename(ledger, "ledger_name")
        if marker == ledger:
            msg = "marker_name and ledger_name must differ"
            raise ValueError(msg)
        return marker, ledger

    @staticmethod
    def _missing_root_report() -> DoctorReport:
        return _report("MISSING_ROOT", "HIGH", None, "OPERATOR_REVIEW")

    def inspect(
        self,
        state_root: Path,
        operation_id: str,
        *,
        marker_name: str | None = None,
        ledger_name: str | None = None,
    ) -> DoctorReport:
        try:
            _store._require_opaque_identifier(operation_id, "operation_id")
        except (TypeError, ValueError) as exc:
            msg = "operation_id is invalid"
            raise ValueError(msg) from exc
        marker, ledger = self._layout(marker_name, ledger_name)
        root = _coerce_root(state_root)
        try:
            root_metadata = os.stat(root, follow_symlinks=False)
        except FileNotFoundError:
            return self._missing_root_report()
        except OSError:
            return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
        if not stat.S_ISDIR(root_metadata.st_mode):
            return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
        filesystem = self._filesystem
        owns_filesystem = filesystem is None
        if filesystem is None:
            try:
                filesystem = StateFilesystem.open_existing(
                    root,
                    marker_name=marker,
                    ledger_name=ledger,
                )
            except WriterActiveError:
                return _report("WRITER_ACTIVE", "MEDIUM", None, "OPERATOR_REVIEW")
            except UnsafeFilesystemError:
                return _report("UNSAFE_SIDECAR", "HIGH", None, "OPERATOR_REVIEW")
            except (StateFilesystemError, OSError):
                return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
        else:
            if filesystem.state_root != root:
                raise ValueError("state_root does not match filesystem")
        assert filesystem is not None
        try:
            before = filesystem.inventory()
            marker_entry = before.entry(marker)
            ledger_reader = RecoveryLedgerReader()
            ledger_snapshot = ledger_reader.read(filesystem)
            if (
                ledger_snapshot is not None
                and ledger_snapshot.phase in _PENDING_LEDGER_PHASES
            ):
                _assert_stable(filesystem, before)
                return _report("RESTORE_INCOMPLETE", "HIGH", None, "OPERATOR_REVIEW")
            marker_present = marker_entry is not None
            if (
                marker_present
                and filesystem.marker_state == _store.WRITER_MARKER_PREPARED_STATE
            ):
                _assert_stable(filesystem, before)
                return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
            if marker_present and not filesystem.try_marker_exclusive():
                _assert_stable(filesystem, before)
                return _report("WRITER_ACTIVE", "MEDIUM", None, "OPERATOR_REVIEW")
            wal = before.entry(f"{_store.DATABASE_FILENAME}-wal")
            journal = before.entry(f"{_store.DATABASE_FILENAME}-journal")
            if journal is not None and journal.size > 0:
                _assert_stable(filesystem, before)
                return _report("UNSAFE_SIDECAR", "HIGH", None, "OPERATOR_REVIEW")
            if wal is not None and wal.size > 0:
                _assert_stable(filesystem, before)
                return _report(
                    "WAL_PENDING", "MEDIUM", None, "CHECKPOINT_AFTER_QUIESCE"
                )
            primary = before.entry(_store.DATABASE_FILENAME)
            if primary is None:
                state: ObservedState = "EMPTY_ROOT" if not before.entries else "MISSING"
                _assert_stable(filesystem, before)
                return _report(state, "HIGH", None, "OPERATOR_REVIEW")
            if primary.file_type != "regular" or primary.digest is None:
                _assert_stable(filesystem, before)
                return _report("UNSAFE_SIDECAR", "HIGH", None, "OPERATOR_REVIEW")
            if before.lifetime_gate_identity is None:
                _assert_stable(filesystem, before)
                return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
            db_fd = filesystem.open_existing_regular(_store.DATABASE_FILENAME)
            connection: sqlite3.Connection | None = None
            try:
                filesystem._fault("before_db_open")
                connection = _deserialize_database_from_fd(db_fd)
                connection.row_factory = sqlite3.Row
                _store._validate_existing_schema(connection)
                observation = _store._read_existing_operation(connection, operation_id)
                if observation is None:
                    _assert_stable(filesystem, before)
                    return _report("NOT_FOUND", "HIGH", None, "OPERATOR_REVIEW")
                result = _status_report(observation, marker_present=marker_present)
                _assert_stable(filesystem, before)
                return result
            finally:
                cleanup_error: BaseException | None = None
                if connection is not None:
                    try:
                        _close_temporary_connection(
                            connection, "SQLite doctor database"
                        )
                    except _CLEANUP_EXCEPTION as error:
                        cleanup_error = error
                try:
                    _close_temporary_fd(db_fd, None, "SQLite doctor database")
                except _CLEANUP_EXCEPTION as error:
                    if cleanup_error is None:
                        cleanup_error = error
                if cleanup_error is not None:
                    raise cleanup_error
        except LedgerReadError:
            try:
                _assert_stable(filesystem, before)
            except StateFilesystemError:
                return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
            return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
        except _store.StoreSchemaError:
            try:
                _assert_stable(filesystem, before)
            except StateFilesystemError:
                return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
            return _report("SCHEMA_INVALID", "HIGH", None, "INSPECT_SCHEMA")
        except (_store.StoreIntegrityError, sqlite3.DatabaseError):
            try:
                _assert_stable(filesystem, before)
            except StateFilesystemError:
                return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
            return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
        except UnsafeFilesystemError:
            return _report("UNSAFE_SIDECAR", "HIGH", None, "OPERATOR_REVIEW")
        except (UnstableSnapshotError, StateFilesystemError, OSError):
            return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
        finally:
            if owns_filesystem:
                filesystem.close()


def doctor(
    state_root: Path,
    operation_id: str,
    *,
    marker_name: str,
    ledger_name: str,
) -> DoctorReport:
    """Convenience wrapper around :class:`ReadOnlyDoctor`."""

    return ReadOnlyDoctor(
        marker_name=marker_name,
        ledger_name=ledger_name,
    ).inspect(state_root, operation_id)


__all__ = [
    "DOCTOR_PROTOCOL_VERSION",
    "RECOVERY_LEDGER_VERSION",
    "WRITER_MARKER_BASENAME",
    "Confidence",
    "DoctorError",
    "DoctorReport",
    "FileType",
    "FilesetInventory",
    "FilesystemEntry",
    "LedgerPhase",
    "LedgerReadError",
    "LedgerSnapshot",
    "Mutation",
    "ObservedState",
    "ReadOnlyDoctor",
    "RecoveryLedgerReader",
    "SafeAction",
    "StateFilesystem",
    "StateFilesystemError",
    "UnsafeFilesystemError",
    "UnstableSnapshotError",
    "WriterActiveError",
    "doctor",
]
