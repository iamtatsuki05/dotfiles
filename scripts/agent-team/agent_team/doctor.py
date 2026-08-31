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
    "MIGRATION_REQUIRED",
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
_MAX_ORPHAN_FDS: Final[int] = 8
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
    "MIGRATION_REQUIRED",
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
    "RESTORE_REPLACED": frozenset({"RESTORE_COMMITTED"}),
}


class CleanupOwner:
    """Opaque owner for retrying one bounded cleanup lifecycle."""

    __slots__ = ("_members", "_retry")

    def __init__(
        self,
        retry: Callable[[], None],
        *,
        _members: tuple[CleanupOwner, ...] | None = None,
    ) -> None:
        self._retry = retry
        self._members = _members if _members is not None else (self,)

    def retry_cleanup(self) -> None:
        """Retry the cleanup held by this owner."""

        self._retry()

    def close(self) -> None:
        """Idempotent alias for retrying the owned cleanup."""

        self.retry_cleanup()


def _combine_cleanup_owners(*owners: CleanupOwner) -> CleanupOwner:
    members: list[CleanupOwner] = []
    for owner in owners:
        for member in owner._members:
            if all(member is not existing for existing in members):
                members.append(member)

    pending = list(members)
    combined: CleanupOwner | None = None

    def retry_all() -> None:
        first_error: BaseException | None = None
        remaining: list[CleanupOwner] = []
        for member in tuple(pending):
            try:
                member.retry_cleanup()
            except _CLEANUP_EXCEPTION as error:
                remaining.append(member)
                if first_error is None:
                    first_error = error
        pending[:] = remaining
        assert combined is not None
        combined._members = tuple(pending)
        if first_error is not None:
            raise first_error

    combined = CleanupOwner(retry_all, _members=tuple(pending))
    return combined


class DoctorError(RuntimeError):
    """Base class for read-only doctor failures."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self._cleanup_capability: CleanupOwner | None = None

    @property
    def cleanup_owner(self) -> CleanupOwner | None:
        """Return the opaque owner for any incomplete cleanup."""

        return self._cleanup_capability

    def retry_cleanup(self) -> None:
        """Retry cleanup owned by this error, if any."""

        capability = self._cleanup_capability
        if capability is None:
            return
        try:
            capability.retry_cleanup()
        except BaseException as error:
            wrapped_error = _cleanup_owner_or_wrap(error, capability)
            if wrapped_error is not error:
                raise wrapped_error from error
            raise
        self._cleanup_capability = None

    def _set_cleanup_owner(self, owner: CleanupOwner) -> None:
        if self._cleanup_capability is None:
            self._cleanup_capability = owner

    def _replace_cleanup_owner(self, owner: CleanupOwner) -> None:
        self._cleanup_capability = owner

    def _attach_cleanup_capability(self, capability: CleanupOwner) -> None:
        self._set_cleanup_owner(capability)


class StateFilesystemError(DoctorError):
    """The state root or a required existing file cannot be observed safely."""


class CleanupUncertaintyError(StateFilesystemError):
    """A cleanup operation returned an error after its final status became uncertain."""

    def __init__(self, message: str, *, cleanup_complete: bool = False) -> None:
        super().__init__(message)
        self.cleanup_complete = cleanup_complete


class CleanupOwnerError(StateFilesystemError):
    """A primary exception cannot carry the required cleanup owner."""

    def __init__(self, primary_error: BaseException) -> None:
        super().__init__("state filesystem cleanup owner is unavailable")
        self.primary_error = primary_error


class UnsafeFilesystemError(StateFilesystemError):
    """A known state file has an unsafe type, owner, mode, or link count."""


class UnstableSnapshotError(StateFilesystemError):
    """A path and its descriptor did not retain one identity/metadata view."""


class WriterActiveError(StateFilesystemError):
    """The existing lifetime gate is held exclusively by another process."""


class LedgerReadError(DoctorError):
    """An existing recovery ledger is incomplete or malformed."""


class _RestorePairReadError(DoctorError):
    """The canonical recovery ledger/tombstone pair cannot be validated."""

    def __init__(self, message: str, *, tombstone_present: bool) -> None:
        super().__init__(message)
        self.tombstone_present = tombstone_present


def _attach_cleanup_owner(error: BaseException, owner: CleanupOwner) -> bool:
    existing_owner = getattr(error, "cleanup_owner", None)
    if existing_owner is None:
        try:
            foreign_owner = vars(error).get("_cleanup_capability")
        except (AttributeError, TypeError):
            foreign_owner = None
        if isinstance(foreign_owner, CleanupOwner):
            existing_owner = foreign_owner
        elif foreign_owner is not None:
            retry = getattr(foreign_owner, "retry_cleanup", None)
            if callable(retry):
                existing_owner = CleanupOwner(retry)
    if existing_owner is owner:
        return True
    if isinstance(existing_owner, CleanupOwner):
        owner = _combine_cleanup_owners(existing_owner, owner)
    if isinstance(error, DoctorError):
        if existing_owner is None:
            error._set_cleanup_owner(owner)
        else:
            error._replace_cleanup_owner(owner)
        return True
    try:
        error_attributes = vars(error)
        error_attributes["_cleanup_capability"] = owner
        error_attributes["cleanup_owner"] = owner
        error_attributes["retry_cleanup"] = owner.retry_cleanup
    except (AttributeError, TypeError):
        return False
    return True


def _mark_cleanup_uncertainty(
    error: BaseException,
    cleanup_error: CleanupUncertaintyError,
) -> bool:
    try:
        vars(error)["_cleanup_uncertainty"] = cleanup_error
    except (AttributeError, TypeError):
        return False
    return True


def _has_cleanup_uncertainty(error: BaseException) -> bool:
    return (
        isinstance(error, CleanupOwnerError)
        or getattr(
            error,
            "_cleanup_uncertainty",
            None,
        )
        is not None
    )


def _cleanup_owner_or_wrap(
    error: BaseException,
    owner: CleanupOwner,
    *,
    cleanup_error: CleanupUncertaintyError | None = None,
) -> BaseException:
    if _attach_cleanup_owner(error, owner):
        if cleanup_error is not None:
            _mark_cleanup_uncertainty(error, cleanup_error)
        return error
    wrapped_error = CleanupOwnerError(error)
    wrapped_error._set_cleanup_owner(owner)
    if cleanup_error is not None:
        _mark_cleanup_uncertainty(wrapped_error, cleanup_error)
    return wrapped_error


def _foreign_cleanup_owner(error: BaseException) -> CleanupOwner | None:
    owner = getattr(error, "cleanup_owner", None)
    if isinstance(owner, CleanupOwner):
        return owner
    try:
        foreign_owner = vars(error).get("_cleanup_capability")
    except (AttributeError, TypeError):
        return None
    if isinstance(foreign_owner, CleanupOwner):
        return foreign_owner
    retry = getattr(foreign_owner, "retry_cleanup", None)
    if callable(retry):
        return CleanupOwner(retry)
    return None


def _find_cleanup_owner(error: BaseException) -> CleanupOwner | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        owner = getattr(current, "cleanup_owner", None)
        if isinstance(owner, CleanupOwner):
            return owner
        try:
            foreign_owner = vars(current).get("_cleanup_capability")
        except (AttributeError, TypeError):
            foreign_owner = None
        if isinstance(foreign_owner, CleanupOwner):
            return foreign_owner
        next_error = current.__cause__
        if next_error is None and not current.__suppress_context__:
            next_error = current.__context__
        current = next_error
    return None


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

    if expected_identity is None:
        raise StateFilesystemError(f"{label} descriptor identity is unavailable")
    try:
        metadata = os.fstat(fd)
    except _CLEANUP_EXCEPTION as status_error:
        if isinstance(status_error, OSError) and status_error.errno == errno.EBADF:
            raise CleanupUncertaintyError(
                f"{label} close status is unknown",
                cleanup_complete=True,
            ) from status_error
        raise CleanupUncertaintyError(
            f"{label} descriptor status is unknown"
        ) from status_error
    if _metadata_identity(metadata) != expected_identity:
        raise StateFilesystemError(f"{label} descriptor was reused")
    try:
        os.close(fd)
    except _CLEANUP_EXCEPTION as first_error:
        try:
            metadata = os.fstat(fd)
        except _CLEANUP_EXCEPTION as status_error:
            if isinstance(status_error, OSError) and status_error.errno == errno.EBADF:
                raise CleanupUncertaintyError(
                    f"{label} close status is unknown",
                    cleanup_complete=True,
                ) from first_error
            raise CleanupUncertaintyError(
                f"{label} close status is unknown"
            ) from status_error
        if _metadata_identity(metadata) != expected_identity:
            raise StateFilesystemError(f"{label} descriptor was reused")
        try:
            os.close(fd)
        except _CLEANUP_EXCEPTION as retry_error:
            raise CleanupUncertaintyError(f"{label} cannot be closed") from retry_error
        raise CleanupUncertaintyError(
            f"{label} close failed before retry",
            cleanup_complete=True,
        )


def _close_temporary_connection(
    connection: sqlite3.Connection,
    label: str,
) -> None:
    """Close a temporary SQLite connection, retrying an idempotent failure."""

    try:
        connection.close()
    except _CLEANUP_EXCEPTION as first_error:
        try:
            connection.close()
        except _CLEANUP_EXCEPTION as retry_error:
            raise CleanupUncertaintyError(f"{label} cannot be closed") from retry_error
        raise CleanupUncertaintyError(
            f"{label} close failed before retry",
            cleanup_complete=True,
        ) from first_error


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
    except BaseException as exc:
        primary_error: BaseException
        if isinstance(exc, (AttributeError, sqlite3.DatabaseError)):
            primary_error = _store.StoreIntegrityError(
                "SQLite in-memory database deserialization failed"
            )
        else:
            primary_error = exc
        cleanup_error: CleanupUncertaintyError | None = None
        if connection is not None:
            try:
                _close_temporary_connection(
                    connection,
                    "SQLite in-memory database",
                )
            except CleanupUncertaintyError as error:
                cleanup_error = error
        if cleanup_error is not None:
            if not cleanup_error.cleanup_complete:
                assert connection is not None
                owner = CleanupOwner(connection.close)
                cleanup_error._set_cleanup_owner(owner)
                wrapped_error = _cleanup_owner_or_wrap(
                    primary_error,
                    owner,
                    cleanup_error=cleanup_error,
                )
                if wrapped_error is not primary_error:
                    raise wrapped_error from primary_error
            elif not _mark_cleanup_uncertainty(primary_error, cleanup_error):
                assert connection is not None
                owner = CleanupOwner(connection.close)
                wrapped_error = CleanupOwnerError(primary_error)
                wrapped_error._set_cleanup_owner(owner)
                _mark_cleanup_uncertainty(wrapped_error, cleanup_error)
                raise wrapped_error from primary_error
        if primary_error is exc:
            raise
        raise primary_error from exc
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
        self._parent_identity: tuple[int, int] | None = None
        self._gate_fd: int | None = None
        self._gate_identity: tuple[int, int] | None = None
        self._marker_fd: int | None = None
        self._marker_identity: tuple[int, int] | None = None
        self._marker_signature: tuple[int, ...] | None = None
        self._marker_state: str | None = None
        self._gate_shared = False
        self._marker_shared = False
        self._marker_exclusive = False
        self._marker_handoff_pending = False
        self._marker_invalidated = False
        self._filesystem_invalidated = False
        self._orphan_fds: list[tuple[int, tuple[int, int] | None, str]] = []
        self._cleanup_owner: CleanupOwner | None = None
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
                root_error = StateFilesystemError("state root cannot be opened")
                owner = _foreign_cleanup_owner(exc)
                if owner is not None:
                    root_error._set_cleanup_owner(owner)
                raise root_error from exc
            instance._root_fd = root_fd
            instance._root_identity = _metadata_identity(root_before)
            instance._root_signature = _metadata_signature(root_before)
            metadata = os.fstat(root_fd)
            if _metadata_signature(metadata) != _metadata_signature(root_before):
                raise UnstableSnapshotError("state root changed while opening")
            instance._root_identity = _metadata_identity(metadata)
            instance._root_signature = _metadata_signature(metadata)
            instance._open_existing_gate()
            instance._open_existing_marker()
            instance._assert_root_identity()
            return instance
        except BaseException as primary_error:
            try:
                instance.close()
            except _CLEANUP_EXCEPTION as cleanup_error:
                owner = instance._cleanup_owner_handle()
                wrapped_error = _cleanup_owner_or_wrap(
                    primary_error,
                    owner,
                    cleanup_error=(
                        cleanup_error
                        if isinstance(cleanup_error, CleanupUncertaintyError)
                        else None
                    ),
                )
                if wrapped_error is not primary_error:
                    raise wrapped_error from primary_error
            raise

    def _fault(self, point: str) -> None:
        """Deterministic process-test seam; production implementation is a no-op."""

    def _assert_open(self) -> int:
        if self._closed or self._root_fd is None:
            raise StateFilesystemError("state filesystem is closed")
        return self._root_fd

    def _ensure_ready_for_io(self) -> int:
        root_fd = self._assert_open()
        if self._filesystem_invalidated:
            raise StateFilesystemError("state filesystem was invalidated")
        self._retry_orphan_fds()
        self._retry_marker_handoff()
        return root_fd

    def _retry_marker_handoff(self) -> None:
        if not self._marker_handoff_pending:
            return
        if self._marker_invalidated:
            raise StateFilesystemError("writer marker was invalidated")
        marker_fd = self._marker_fd
        expected_identity = self._marker_identity
        expected_signature = self._marker_signature
        expected_state = self._marker_state
        if marker_fd is None or expected_identity is None or expected_signature is None:
            if marker_fd is not None or expected_identity is None:
                raise StateFilesystemError("writer marker handoff is incomplete")
            try:
                self._open_existing_marker()
            except _CLEANUP_EXCEPTION as error:
                if isinstance(error, (UnsafeFilesystemError, UnstableSnapshotError)):
                    self._marker_invalidated = True
                    self._filesystem_invalidated = True
                raise StateFilesystemError(
                    "writer marker handoff cannot reopen marker"
                ) from error
            marker_fd = self._marker_fd
            if (
                marker_fd is None
                or self._marker_identity != expected_identity
                or self._marker_signature != expected_signature
                or self._marker_state != expected_state
            ):
                self._marker_invalidated = True
                self._filesystem_invalidated = True
                self._marker_handoff_pending = True
                raise UnstableSnapshotError(
                    "writer marker changed while reopening handoff"
                )
            self._marker_shared = True
            self._marker_exclusive = False
            self._marker_handoff_pending = False
            self._assert_marker_identity()
            return
        try:
            metadata = os.fstat(marker_fd)
        except OSError as exc:
            raise StateFilesystemError(
                "writer marker handoff descriptor is unavailable"
            ) from exc
        except _CLEANUP_EXCEPTION as exc:
            status_unknown = CleanupUncertaintyError(
                "writer marker handoff status is unknown"
            )
            status_unknown._set_cleanup_owner(self._cleanup_owner_handle())
            raise status_unknown from exc
        if (
            _metadata_identity(metadata) != expected_identity
            or _metadata_signature(metadata) != expected_signature
        ):
            self._drop_descriptor_number(marker_fd)
            raise StateFilesystemError("writer marker handoff descriptor was reused")
        try:
            fcntl.flock(marker_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            raise StateFilesystemError(
                "writer marker handoff cannot reacquire shared lock"
            ) from exc
        self._marker_shared = True
        self._marker_exclusive = False
        self._marker_handoff_pending = False
        try:
            self._assert_marker_identity()
        except BaseException:
            self._marker_invalidated = True
            self._filesystem_invalidated = True
            self._marker_shared = False
            self._marker_handoff_pending = True
            raise

    def _cleanup_owner_handle(self) -> CleanupOwner:
        owner = self._cleanup_owner
        if owner is None:
            owner = CleanupOwner(self.close)
            self._cleanup_owner = owner
        return owner

    def _temporary_fd_cleanup_owner(
        self,
        fd: int,
        expected_identity: tuple[int, int] | None,
        label: str,
    ) -> CleanupOwner:
        def retry() -> None:
            _close_temporary_fd(fd, expected_identity, label)
            self._orphan_fds = [item for item in self._orphan_fds if item[0] != fd]

        return CleanupOwner(retry)

    def _drop_descriptor_number(self, fd: int) -> None:
        """Forget a descriptor number after identity proof no longer holds."""

        self._orphan_fds = [item for item in self._orphan_fds if item[0] != fd]
        if self._marker_fd == fd:
            self._marker_fd = None
            self._marker_identity = None
            self._marker_signature = None
            self._marker_state = None
            self._marker_shared = False
            self._marker_exclusive = False
            self._marker_handoff_pending = False
            self._marker_invalidated = True
        if self._gate_fd == fd:
            self._gate_fd = None
            self._gate_identity = None
            self._gate_shared = False
        if self._root_fd == fd:
            self._root_fd = None
            self._root_identity = None
            self._root_signature = None
        if self._parent_fd == fd:
            self._parent_fd = None
            self._parent_identity = None
        self._filesystem_invalidated = True

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
        except _CLEANUP_EXCEPTION as exc:
            error = CleanupUncertaintyError("state root status is unknown")
            error._set_cleanup_owner(self._cleanup_owner_handle())
            raise error from exc
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
            parent_before = os.stat(
                "..",
                dir_fd=root_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise StateFilesystemError("state parent cannot be inspected") from exc
        try:
            parent_fd = os.open("..", directory_flags, dir_fd=root_fd)
        except OSError as exc:
            raise StateFilesystemError("state parent cannot be opened") from exc
        self._parent_fd = parent_fd
        self._parent_identity = _metadata_identity(parent_before)
        try:
            _store._validate_directory_fd(parent_fd, state_root=False)
        except _store.StoreError as exc:
            raise StateFilesystemError("state parent is unsafe") from exc
        try:
            parent_metadata = os.fstat(parent_fd)
        except OSError as exc:
            raise StateFilesystemError("state parent cannot be inspected") from exc
        except _CLEANUP_EXCEPTION as exc:
            error = CleanupUncertaintyError("state parent status is unknown")
            error._set_cleanup_owner(self._cleanup_owner_handle())
            raise error from exc
        if _metadata_identity(parent_metadata) != self._parent_identity:
            raise UnstableSnapshotError("state parent changed while opening")
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
            self._gate_fd = gate_fd
            self._gate_identity = _metadata_identity(before)
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
            self._gate_shared = True
        except BaseException as primary_error:
            cleanup_error: BaseException | None = None
            try:
                self._close_owned_temporary_fd(
                    gate_fd,
                    _metadata_identity(before),
                    "lifetime gate",
                )
            except _CLEANUP_EXCEPTION as error:
                cleanup_error = error
            if cleanup_error is not None:
                owner = self._cleanup_owner_handle()
                wrapped_error = _cleanup_owner_or_wrap(
                    primary_error,
                    owner,
                    cleanup_error=(
                        cleanup_error
                        if isinstance(cleanup_error, CleanupUncertaintyError)
                        else None
                    ),
                )
                if wrapped_error is not primary_error:
                    raise wrapped_error from primary_error
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
            self._marker_fd = marker_fd
            self._marker_identity = _metadata_identity(marker)
            self._marker_signature = _metadata_signature(marker)
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
            self._marker_shared = True
            try:
                marker_state = _store._read_writer_marker_state(marker_fd)
            except _store.StoreError as exc:
                raise UnsafeFilesystemError("writer marker content is invalid") from exc
            self._marker_state = marker_state
        except BaseException as primary_error:
            cleanup_error: BaseException | None = None
            try:
                self._close_owned_temporary_fd(
                    marker_fd,
                    _metadata_identity(marker),
                    "writer marker",
                )
            except _CLEANUP_EXCEPTION as error:
                cleanup_error = error
            if cleanup_error is not None:
                owner = self._cleanup_owner_handle()
                wrapped_error = _cleanup_owner_or_wrap(
                    primary_error,
                    owner,
                    cleanup_error=(
                        cleanup_error
                        if isinstance(cleanup_error, CleanupUncertaintyError)
                        else None
                    ),
                )
                if wrapped_error is not primary_error:
                    raise wrapped_error from primary_error
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
        if self._marker_handoff_pending:
            raise StateFilesystemError("writer marker handoff is pending")
        self._retry_orphan_fds()
        marker_fd = self._marker_fd
        expected_identity = self._marker_identity
        expected_signature = self._marker_signature
        if marker_fd is None:
            if expected_identity is not None or self._marker_state is not None:
                self._marker_invalidated = True
                self._filesystem_invalidated = True
                raise UnstableSnapshotError("writer marker descriptor is unavailable")
            return
        if expected_identity is None or expected_signature is None:
            self._marker_invalidated = True
            self._filesystem_invalidated = True
            raise UnstableSnapshotError("writer marker identity is unavailable")
        try:
            fd_metadata = os.fstat(marker_fd)
        except OSError as exc:
            if exc.errno == errno.EBADF:
                self._drop_descriptor_number(marker_fd)
            else:
                self._marker_invalidated = True
                self._filesystem_invalidated = True
            raise UnstableSnapshotError(
                "writer marker descriptor is unavailable"
            ) from exc
        except _CLEANUP_EXCEPTION as exc:
            self._marker_invalidated = True
            self._filesystem_invalidated = True
            error = CleanupUncertaintyError("writer marker status is unknown")
            error._set_cleanup_owner(self._cleanup_owner_handle())
            raise error from exc
        path_metadata = self._lstat(self.marker_name)
        if path_metadata is None:
            self._marker_invalidated = True
            self._filesystem_invalidated = True
            raise UnstableSnapshotError("writer marker disappeared while observed")
        if (
            (fd_metadata.st_dev, fd_metadata.st_ino) != expected_identity
            or (path_metadata.st_dev, path_metadata.st_ino) != expected_identity
            or _metadata_signature(fd_metadata) != expected_signature
            or _metadata_signature(path_metadata) != expected_signature
        ):
            self._drop_descriptor_number(marker_fd)
            raise UnstableSnapshotError("writer marker changed while observed")
        try:
            state = _store._read_writer_marker_state(marker_fd)
        except _store.StoreError as exc:
            self._marker_invalidated = True
            self._filesystem_invalidated = True
            raise UnstableSnapshotError("writer marker content changed") from exc
        if state != self._marker_state:
            self._marker_invalidated = True
            self._filesystem_invalidated = True
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
            except _CLEANUP_EXCEPTION as status_error:
                if (
                    isinstance(status_error, OSError)
                    and status_error.errno == errno.EBADF
                ):
                    raise close_error
                try:
                    self._remember_orphan_fd(fd, expected_identity, label)
                except StateFilesystemError as registry_error:
                    cleanup_error = CleanupUncertaintyError(
                        f"{label} descriptor cannot be retained"
                    )
                    cleanup_error._set_cleanup_owner(
                        self._temporary_fd_cleanup_owner(
                            fd,
                            expected_identity,
                            label,
                        )
                    )
                    raise cleanup_error from registry_error
                cleanup_error = CleanupUncertaintyError(
                    f"{label} descriptor status is unknown"
                )
                wrapped_error = _cleanup_owner_or_wrap(
                    cleanup_error,
                    self._cleanup_owner_handle(),
                )
                if wrapped_error is not cleanup_error:
                    raise wrapped_error from status_error
                raise cleanup_error from status_error
            if (
                expected_identity is not None
                and _metadata_identity(metadata) != expected_identity
            ):
                if "marker" in label:
                    self._drop_descriptor_number(fd)
                raise StateFilesystemError(f"{label} descriptor was reused")
            try:
                self._remember_orphan_fd(
                    fd,
                    expected_identity,
                    label,
                )
            except StateFilesystemError as registry_error:
                cleanup_error = CleanupUncertaintyError(
                    f"{label} descriptor cannot be retained"
                )
                cleanup_error._set_cleanup_owner(
                    self._temporary_fd_cleanup_owner(
                        fd,
                        expected_identity,
                        label,
                    )
                )
                raise cleanup_error from registry_error
            if isinstance(close_error, CleanupUncertaintyError):
                wrapped_error = _cleanup_owner_or_wrap(
                    close_error,
                    self._cleanup_owner_handle(),
                )
                if wrapped_error is not close_error:
                    raise wrapped_error from close_error
                raise
            raise CleanupUncertaintyError(
                f"{label} close status is unknown"
            ) from close_error

    def _remember_orphan_fd(
        self,
        fd: int,
        expected_identity: tuple[int, int] | None,
        label: str,
    ) -> None:
        if any(existing_fd == fd for existing_fd, _, _ in self._orphan_fds):
            return
        if len(self._orphan_fds) >= _MAX_ORPHAN_FDS:
            error = CleanupUncertaintyError(
                "state filesystem descriptor retry registry is full"
            )
            error._set_cleanup_owner(
                self._temporary_fd_cleanup_owner(fd, expected_identity, label)
            )
            raise error
        self._orphan_fds.append((fd, expected_identity, label))

    def _retain_failed_fd(
        self,
        fd: int,
        expected_identity: tuple[int, int] | None,
        label: str,
    ) -> None:
        """Retain one close-uncertain preflight descriptor for safe retry."""

        try:
            metadata = os.fstat(fd)
        except _CLEANUP_EXCEPTION as exc:
            if isinstance(exc, OSError) and exc.errno == errno.EBADF:
                return
            try:
                self._remember_orphan_fd(fd, expected_identity, label)
            except StateFilesystemError as registry_error:
                cleanup_error = CleanupUncertaintyError(
                    f"{label} descriptor cannot be retained"
                )
                cleanup_error._set_cleanup_owner(
                    self._temporary_fd_cleanup_owner(
                        fd,
                        expected_identity,
                        label,
                    )
                )
                raise cleanup_error from registry_error
            cleanup_error = CleanupUncertaintyError(
                f"{label} descriptor status is unknown"
            )
            cleanup_error._set_cleanup_owner(
                self._temporary_fd_cleanup_owner(fd, expected_identity, label)
            )
            raise cleanup_error from exc
        actual_identity = _metadata_identity(metadata)
        if expected_identity is not None and actual_identity != expected_identity:
            raise StateFilesystemError(f"{label} descriptor was reused")
        try:
            self._remember_orphan_fd(fd, expected_identity, label)
        except StateFilesystemError as registry_error:
            cleanup_error = CleanupUncertaintyError(
                f"{label} descriptor cannot be retained"
            )
            cleanup_error._set_cleanup_owner(
                self._temporary_fd_cleanup_owner(fd, expected_identity, label)
            )
            raise cleanup_error from registry_error

    def _retry_orphan_fds(self) -> None:
        """Drain retained temporary descriptors before starting another read."""

        remaining: list[tuple[int, tuple[int, int] | None, str]] = []
        first_error: BaseException | None = None
        for fd, expected_identity, label in self._orphan_fds:
            marker_orphan = "marker" in label
            if label.startswith("unresolved "):
                remaining.append((fd, expected_identity, label))
                self._filesystem_invalidated = True
                if first_error is None:
                    first_error = StateFilesystemError(
                        f"{label} descriptor identity is unavailable"
                    )
                continue
            try:
                metadata = os.fstat(fd)
            except _CLEANUP_EXCEPTION as status_error:
                if (
                    isinstance(status_error, OSError)
                    and status_error.errno == errno.EBADF
                ):
                    continue
                remaining.append((fd, expected_identity, label))
                if first_error is None:
                    first_error = CleanupUncertaintyError(
                        f"{label} descriptor status is unknown"
                    )
                    first_error.__cause__ = status_error
                continue
            if (
                expected_identity is None
                or _metadata_identity(metadata) != expected_identity
            ):
                if marker_orphan:
                    self._marker_invalidated = True
                    self._drop_descriptor_number(fd)
                else:
                    remaining.append((fd, expected_identity, label))
                    self._filesystem_invalidated = True
                if first_error is None:
                    first_error = StateFilesystemError(
                        f"{label} descriptor identity is unavailable"
                        if expected_identity is None
                        else f"{label} descriptor was reused"
                    )
                continue
            try:
                os.close(fd)
            except _CLEANUP_EXCEPTION as close_error:
                try:
                    retry_metadata = os.fstat(fd)
                except _CLEANUP_EXCEPTION as status_error:
                    if (
                        isinstance(status_error, OSError)
                        and status_error.errno == errno.EBADF
                    ):
                        continue
                    remaining.append((fd, expected_identity, label))
                    if first_error is None:
                        first_error = CleanupUncertaintyError(
                            f"{label} descriptor status is unknown"
                        )
                    continue
                if _metadata_identity(retry_metadata) != expected_identity:
                    if marker_orphan:
                        self._marker_invalidated = True
                        self._drop_descriptor_number(fd)
                    else:
                        remaining.append((fd, expected_identity, label))
                        self._filesystem_invalidated = True
                    if first_error is None:
                        first_error = StateFilesystemError(
                            f"{label} descriptor was reused"
                        )
                    continue
                remaining.append((fd, expected_identity, label))
                if first_error is None:
                    first_error = StateFilesystemError(f"{label} cannot be closed")
                del close_error
        self._orphan_fds = remaining
        if first_error is not None:
            if remaining:
                wrapped_error = _cleanup_owner_or_wrap(
                    first_error,
                    self._cleanup_owner_handle(),
                )
                if wrapped_error is not first_error:
                    raise wrapped_error from first_error
            raise first_error

    def _open_regular(
        self,
        name: str,
        *,
        ensure_ready: bool = True,
    ) -> tuple[int, os.stat_result]:
        root_fd = self._ensure_ready_for_io() if ensure_ready else self._assert_open()
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
        except BaseException as body_error:
            try:
                self._close_owned_temporary_fd(
                    fd,
                    _metadata_identity(before),
                    "state entry",
                )
            except _CLEANUP_EXCEPTION as cleanup_error:
                owner = self._cleanup_owner_handle()
                current_owner = _foreign_cleanup_owner(cleanup_error)
                if current_owner is not None:
                    owner = _combine_cleanup_owners(owner, current_owner)
                wrapped_error = _cleanup_owner_or_wrap(
                    body_error,
                    owner,
                    cleanup_error=(
                        cleanup_error
                        if isinstance(cleanup_error, CleanupUncertaintyError)
                        else None
                    ),
                )
                if wrapped_error is not body_error:
                    raise wrapped_error from body_error
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
            body_error = sys.exc_info()[1]
            try:
                self._close_owned_temporary_fd(
                    fd,
                    _metadata_identity(opened),
                    "state entry digest",
                )
            except _CLEANUP_EXCEPTION as cleanup_error:
                if body_error is None:
                    if isinstance(cleanup_error, CleanupUncertaintyError) and not (
                        self._orphan_fds
                    ):
                        raise
                    wrapped_error = _cleanup_owner_or_wrap(
                        cleanup_error,
                        self._cleanup_owner_handle(),
                    )
                    if wrapped_error is not cleanup_error:
                        raise wrapped_error from cleanup_error
                    raise
                wrapped_error = _cleanup_owner_or_wrap(
                    body_error,
                    self._cleanup_owner_handle(),
                    cleanup_error=(
                        cleanup_error
                        if isinstance(cleanup_error, CleanupUncertaintyError)
                        else None
                    ),
                )
                if wrapped_error is not body_error:
                    raise wrapped_error from body_error
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

        if self._marker_invalidated:
            raise StateFilesystemError("writer marker was invalidated")
        return self._marker_state

    def inventory(self) -> FilesetInventory:
        """Return all root-direct entries, including unrelated names."""

        root_fd = self._ensure_ready_for_io()
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
        gate_path_metadata: os.stat_result | None = None
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
            if current_gate_identity is None:
                raise UnstableSnapshotError("lifetime gate changed while observing")
            try:
                gate_fd_metadata = os.fstat(self._gate_fd)
            except OSError as exc:
                raise UnstableSnapshotError(
                    "lifetime gate descriptor is unavailable"
                ) from exc
            except _CLEANUP_EXCEPTION as exc:
                error = CleanupUncertaintyError("lifetime gate status is unknown")
                error._set_cleanup_owner(self._cleanup_owner_handle())
                raise error from exc
            assert gate_path_metadata is not None
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
        self._ensure_ready_for_io()
        self._assert_root_identity()
        self._fault("before_final_inventory")
        current = self.inventory()
        if current != inventory:
            raise UnstableSnapshotError("state fileset changed while observed")

    def _restore_marker_handoff(
        self,
        marker_fd: int,
        expected_identity: tuple[int, int] | None,
        expected_signature: tuple[int, ...] | None,
        expected_state: str | None,
    ) -> None:
        self._marker_fd = marker_fd
        self._marker_identity = expected_identity
        self._marker_signature = expected_signature
        self._marker_state = expected_state
        self._marker_shared = False
        self._marker_exclusive = False
        self._marker_handoff_pending = True

    def _cleanup_marker_handoff_fd(
        self,
        fd: int,
        expected_identity: tuple[int, int] | None,
        label: str,
    ) -> BaseException | None:
        cleanup_error: BaseException | None = None
        try:
            metadata = os.fstat(fd)
        except _CLEANUP_EXCEPTION as error:
            if isinstance(error, OSError) and error.errno == errno.EBADF:
                return None
            cleanup_error = CleanupUncertaintyError(
                f"{label} descriptor status is unknown"
            )
            cleanup_error.__cause__ = error
            cleanup_error._set_cleanup_owner(
                self._temporary_fd_cleanup_owner(fd, expected_identity, label)
            )
            return cleanup_error
        if (
            expected_identity is None
            or _metadata_identity(metadata) != expected_identity
        ):
            self._drop_descriptor_number(fd)
            return StateFilesystemError(f"{label} descriptor was reused")
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except _CLEANUP_EXCEPTION as error:
            cleanup_error = error
        try:
            _close_temporary_fd(fd, expected_identity, label)
        except _CLEANUP_EXCEPTION as error:
            if cleanup_error is None:
                cleanup_error = error
            try:
                metadata = os.fstat(fd)
            except _CLEANUP_EXCEPTION as status_error:
                if not (
                    isinstance(status_error, OSError)
                    and status_error.errno == errno.EBADF
                ):
                    try:
                        self._remember_orphan_fd(fd, expected_identity, label)
                    except _CLEANUP_EXCEPTION as remember_error:
                        if cleanup_error is None:
                            cleanup_error = remember_error
            else:
                if (
                    expected_identity is None
                    or _metadata_identity(metadata) == expected_identity
                ):
                    try:
                        self._remember_orphan_fd(fd, expected_identity, label)
                    except _CLEANUP_EXCEPTION as remember_error:
                        if cleanup_error is None:
                            cleanup_error = remember_error
        return cleanup_error

    def try_marker_exclusive(self) -> bool:
        """Try a non-blocking exclusive lock on an existing stable marker."""

        self._ensure_ready_for_io()
        if self._marker_fd is not None and self._marker_exclusive:
            self._assert_marker_identity()
            return True
        if self._marker_fd is not None and self._marker_shared:
            if len(self._orphan_fds) >= _MAX_ORPHAN_FDS:
                raise StateFilesystemError(
                    "state filesystem descriptor retry registry is full"
                )
            marker_fd = self._marker_fd
            expected_identity = self._marker_identity
            expected_signature = self._marker_signature
            expected_state = self._marker_state
            self._assert_marker_identity()
            self._marker_shared = False
            self._marker_handoff_pending = True
            try:
                fcntl.flock(marker_fd, fcntl.LOCK_UN)
            except OSError as exc:
                self._marker_shared = True
                self._marker_handoff_pending = False
                raise StateFilesystemError("writer marker cannot be released") from exc
            marker_path = self._lstat(self.marker_name)
            if (
                marker_path is None
                or expected_identity is None
                or expected_signature is None
                or (marker_path.st_dev, marker_path.st_ino) != expected_identity
                or _metadata_signature(marker_path) != expected_signature
            ):
                self._marker_invalidated = True
                self._filesystem_invalidated = True
                _close_temporary_fd(
                    marker_fd,
                    expected_identity,
                    "writer marker",
                )
                self._marker_fd = None
                self._marker_identity = None
                self._marker_signature = None
                self._marker_state = None
                self._marker_handoff_pending = False
                raise UnstableSnapshotError("writer marker changed before reacquire")
            fd: int | None = None
            retained_old_descriptor = False
            try:
                fd, metadata = self._open_regular(
                    self.marker_name,
                    ensure_ready=False,
                )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                        handoff_cleanup_error = self._cleanup_marker_handoff_fd(
                            fd,
                            expected_identity,
                            "writer marker EAGAIN handoff",
                        )
                        if handoff_cleanup_error is not None:
                            self._restore_marker_handoff(
                                marker_fd,
                                expected_identity,
                                expected_signature,
                                expected_state,
                            )
                            fd = None
                            marker_fd = -1
                            retained_old_descriptor = True
                            primary_error = StateFilesystemError(
                                "writer marker is busy"
                            )
                            handoff_owner = _foreign_cleanup_owner(
                                handoff_cleanup_error
                            )
                            owner = self._cleanup_owner_handle()
                            if handoff_owner is not None:
                                owner = _combine_cleanup_owners(owner, handoff_owner)
                            _cleanup_owner_or_wrap(
                                primary_error,
                                owner,
                            )
                            raise primary_error from exc
                        fd = None
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
                            self._marker_handoff_pending = False
                            raise UnstableSnapshotError(
                                "writer marker cannot be reacquired"
                            ) from reacquire_exc
                        self._marker_shared = True
                        self._marker_exclusive = False
                        self._marker_handoff_pending = False
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
                    self._marker_invalidated = True
                    self._filesystem_invalidated = True
                    raise UnstableSnapshotError(
                        "writer marker changed while reacquiring"
                    )
                try:
                    state = _store._read_writer_marker_state(fd)
                except _store.StoreError as exc:
                    self._marker_invalidated = True
                    self._filesystem_invalidated = True
                    raise UnstableSnapshotError(
                        "writer marker content changed while reacquiring"
                    ) from exc
                if state != expected_state:
                    self._marker_invalidated = True
                    self._filesystem_invalidated = True
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
                    handoff_cleanup_error = self._cleanup_marker_handoff_fd(
                        fd,
                        expected_identity,
                        "writer marker handoff",
                    )
                    self._restore_marker_handoff(
                        marker_fd,
                        expected_identity,
                        expected_signature,
                        expected_state,
                    )
                    fd = None
                    marker_fd = -1
                    retained_old_descriptor = True
                    primary_error = StateFilesystemError(
                        "writer marker handoff cannot close old descriptor"
                    )
                    if handoff_cleanup_error is not None:
                        handoff_owner = _foreign_cleanup_owner(handoff_cleanup_error)
                        owner = self._cleanup_owner_handle()
                        if handoff_owner is not None:
                            owner = _combine_cleanup_owners(owner, handoff_owner)
                        _cleanup_owner_or_wrap(
                            primary_error,
                            owner,
                            cleanup_error=(
                                handoff_cleanup_error
                                if isinstance(
                                    handoff_cleanup_error,
                                    CleanupUncertaintyError,
                                )
                                else None
                            ),
                        )
                    raise primary_error from exc
                marker_fd = -1
                self._marker_fd = fd
                self._marker_identity = expected_identity
                self._marker_signature = expected_signature
                self._marker_state = state
                self._marker_shared = False
                self._marker_exclusive = True
                self._marker_handoff_pending = False
                self._assert_marker_identity()
                return True
            except BaseException as primary_error:
                if isinstance(primary_error, UnstableSnapshotError):
                    self._marker_invalidated = True
                    self._filesystem_invalidated = True
                if retained_old_descriptor:
                    raise
                cleanup_error: BaseException | None = None
                if fd is not None:
                    cleanup_error = self._cleanup_marker_handoff_fd(
                        fd,
                        expected_identity,
                        "writer marker post-lock cleanup",
                    )
                if marker_fd != -1:
                    self._restore_marker_handoff(
                        marker_fd,
                        expected_identity,
                        expected_signature,
                        expected_state,
                    )
                    marker_fd = -1
                elif fd is not None and self._marker_fd == fd:
                    self._marker_fd = None
                    self._marker_shared = False
                    self._marker_exclusive = False
                    self._marker_handoff_pending = True
                if cleanup_error is not None:
                    cleanup_owner = _foreign_cleanup_owner(cleanup_error)
                    owner = self._cleanup_owner_handle()
                    if cleanup_owner is not None:
                        owner = _combine_cleanup_owners(owner, cleanup_owner)
                    wrapped_error = _cleanup_owner_or_wrap(
                        primary_error,
                        owner,
                        cleanup_error=(
                            cleanup_error
                            if isinstance(cleanup_error, CleanupUncertaintyError)
                            else None
                        ),
                    )
                    if wrapped_error is not primary_error:
                        raise wrapped_error from primary_error
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

        def close_held_fd(
            attr_name: str,
            identity_attr_name: str,
            fd: int | None,
            *,
            unlock: bool,
            lock_state_attr_names: tuple[str, ...] = (),
        ) -> None:
            if fd is None:
                return

            def clear() -> None:
                setattr(self, attr_name, None)
                setattr(self, identity_attr_name, None)
                for lock_state_attr_name in lock_state_attr_names:
                    setattr(self, lock_state_attr_name, False)
                if attr_name == "_marker_fd":
                    self._marker_handoff_pending = False

            def retain_unresolved(
                expected_identity: tuple[int, int] | None,
            ) -> None:
                try:
                    self._remember_orphan_fd(
                        fd,
                        expected_identity,
                        f"unresolved {attr_name}",
                    )
                except _CLEANUP_EXCEPTION as error:
                    remember(error)
                    return
                clear()
                self._filesystem_invalidated = True

            expected_identity = getattr(self, identity_attr_name)
            try:
                metadata = os.fstat(fd)
            except _CLEANUP_EXCEPTION as error:
                if isinstance(error, OSError) and error.errno == errno.EBADF:
                    clear()
                else:
                    self._filesystem_invalidated = True
                    status_error = CleanupUncertaintyError(
                        f"{attr_name} descriptor status is unknown"
                    )
                    status_error.__cause__ = error
                    status_error._set_cleanup_owner(self._cleanup_owner_handle())
                    remember(status_error)
                return
            actual_identity = _metadata_identity(metadata)
            if expected_identity is None:
                if attr_name == "_marker_fd":
                    self._drop_descriptor_number(fd)
                else:
                    retain_unresolved(None)
                remember(
                    StateFilesystemError(
                        f"{attr_name} descriptor identity is unavailable"
                    )
                )
                return
            if actual_identity != expected_identity:
                if attr_name == "_marker_fd":
                    self._drop_descriptor_number(fd)
                else:
                    retain_unresolved(expected_identity)
                remember(StateFilesystemError(f"{attr_name} descriptor was reused"))
                return
            close_error: BaseException | None = None
            unlock_attempted = unlock and any(
                bool(getattr(self, lock_state_attr_name))
                for lock_state_attr_name in lock_state_attr_names
            )
            if unlock_attempted:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except _CLEANUP_EXCEPTION as error:
                    close_error = error
                else:
                    for lock_state_attr_name in lock_state_attr_names:
                        setattr(self, lock_state_attr_name, False)
                try:
                    after_unlock = os.fstat(fd)
                except _CLEANUP_EXCEPTION as status_error:
                    if (
                        isinstance(status_error, OSError)
                        and status_error.errno == errno.EBADF
                    ):
                        clear()
                        if close_error is not None:
                            remember(close_error)
                        return
                    self._filesystem_invalidated = True
                    close_status = CleanupUncertaintyError(
                        f"{attr_name} status is unknown after unlock"
                    )
                    close_status.__cause__ = status_error
                    close_status._set_cleanup_owner(self._cleanup_owner_handle())
                    remember(close_status)
                    return
                if _metadata_identity(after_unlock) != expected_identity:
                    self._drop_descriptor_number(fd)
                    remember(StateFilesystemError(f"{attr_name} descriptor was reused"))
                    return
            try:
                os.close(fd)
            except _CLEANUP_EXCEPTION as error:
                close_error = close_error or error
                try:
                    after = os.fstat(fd)
                except _CLEANUP_EXCEPTION as status_error:
                    if (
                        isinstance(status_error, OSError)
                        and status_error.errno == errno.EBADF
                    ):
                        clear()
                    else:
                        self._filesystem_invalidated = True
                        close_status = CleanupUncertaintyError(
                            f"{attr_name} close status is unknown"
                        )
                        close_status.__cause__ = status_error
                        close_status._set_cleanup_owner(self._cleanup_owner_handle())
                        remember(close_status)
                else:
                    if _metadata_identity(after) != expected_identity:
                        if attr_name == "_marker_fd":
                            self._drop_descriptor_number(fd)
                        else:
                            retain_unresolved(expected_identity)
                        remember(
                            StateFilesystemError(f"{attr_name} descriptor was reused")
                        )
            else:
                clear()
            if close_error is not None:
                remember(close_error)

        close_held_fd(
            "_marker_fd",
            "_marker_identity",
            self._marker_fd,
            unlock=True,
            lock_state_attr_names=("_marker_shared", "_marker_exclusive"),
        )
        close_held_fd(
            "_gate_fd",
            "_gate_identity",
            self._gate_fd,
            unlock=True,
            lock_state_attr_names=("_gate_shared",),
        )
        close_held_fd(
            "_root_fd",
            "_root_identity",
            self._root_fd,
            unlock=False,
        )
        close_held_fd(
            "_parent_fd",
            "_parent_identity",
            self._parent_fd,
            unlock=False,
        )
        remaining_orphans: list[tuple[int, tuple[int, int] | None, str]] = []
        for orphan_fd, expected_identity, label in self._orphan_fds:
            marker_orphan = "marker" in label
            if label.startswith("unresolved "):
                remaining_orphans.append((orphan_fd, expected_identity, label))
                remember(
                    StateFilesystemError(f"{label} descriptor identity is unavailable")
                )
                continue
            try:
                metadata = os.fstat(orphan_fd)
            except _CLEANUP_EXCEPTION as status_error:
                if (
                    isinstance(status_error, OSError)
                    and status_error.errno == errno.EBADF
                ):
                    continue
                close_status: DoctorError = CleanupUncertaintyError(
                    f"{label} descriptor status is unknown"
                )
                close_status._set_cleanup_owner(self._cleanup_owner_handle())
                remember(close_status)
                remaining_orphans.append((orphan_fd, expected_identity, label))
                continue
            if (
                expected_identity is None
                or _metadata_identity(metadata) != expected_identity
            ):
                if marker_orphan:
                    self._marker_invalidated = True
                    self._drop_descriptor_number(orphan_fd)
                else:
                    remaining_orphans.append((orphan_fd, expected_identity, label))
                    self._filesystem_invalidated = True
                remember(
                    StateFilesystemError(
                        f"{label} descriptor identity is unavailable"
                        if expected_identity is None
                        else f"{label} descriptor was reused"
                    )
                )
                continue
            try:
                os.close(orphan_fd)
            except _CLEANUP_EXCEPTION as first_close_error:
                try:
                    retry_metadata = os.fstat(orphan_fd)
                except _CLEANUP_EXCEPTION as status_error:
                    if (
                        isinstance(status_error, OSError)
                        and status_error.errno == errno.EBADF
                    ):
                        close_status = StateFilesystemError(
                            f"{label} close status is unknown"
                        )
                        close_status.__cause__ = first_close_error
                        remember(close_status)
                    else:
                        close_status = CleanupUncertaintyError(
                            f"{label} close status is unknown"
                        )
                        close_status._set_cleanup_owner(self._cleanup_owner_handle())
                        remember(close_status)
                        remaining_orphans.append((orphan_fd, expected_identity, label))
                    continue
                if _metadata_identity(retry_metadata) != expected_identity:
                    remember(UnstableSnapshotError(f"{label} descriptor was reused"))
                    if marker_orphan:
                        self._marker_invalidated = True
                        self._drop_descriptor_number(orphan_fd)
                    else:
                        self._filesystem_invalidated = True
                        remaining_orphans.append((orphan_fd, expected_identity, label))
                    continue
                try:
                    os.close(orphan_fd)
                except _CLEANUP_EXCEPTION as retry_error:
                    try:
                        final_metadata = os.fstat(orphan_fd)
                    except _CLEANUP_EXCEPTION as status_error:
                        if (
                            isinstance(status_error, OSError)
                            and status_error.errno == errno.EBADF
                        ):
                            close_status = StateFilesystemError(
                                f"{label} close status is unknown"
                            )
                            close_status.__cause__ = retry_error
                            remember(close_status)
                        else:
                            close_status = CleanupUncertaintyError(
                                f"{label} close status is unknown"
                            )
                            close_status._set_cleanup_owner(
                                self._cleanup_owner_handle()
                            )
                            remember(close_status)
                            remaining_orphans.append(
                                (orphan_fd, expected_identity, label)
                            )
                    else:
                        if _metadata_identity(final_metadata) != expected_identity:
                            if marker_orphan:
                                self._marker_invalidated = True
                                self._drop_descriptor_number(orphan_fd)
                            else:
                                self._filesystem_invalidated = True
                                remaining_orphans.append(
                                    (orphan_fd, expected_identity, label)
                                )
                            remember(
                                StateFilesystemError(f"{label} descriptor was reused")
                            )
                        else:
                            remaining_orphans.append(
                                (orphan_fd, expected_identity, label)
                            )
                            remember(retry_error)
        self._orphan_fds = remaining_orphans
        if first_error is not None:
            if isinstance(first_error, CleanupUncertaintyError):
                raise first_error
            if isinstance(first_error, Exception):
                raise StateFilesystemError(
                    "state filesystem close failed"
                ) from first_error
            raise first_error

    def __enter__(self) -> Self:
        self._ensure_ready_for_io()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, traceback
        if not isinstance(exc_value, BaseException):
            try:
                self.close()
            except _CLEANUP_EXCEPTION as cleanup_error:
                wrapped_error = _cleanup_owner_or_wrap(
                    cleanup_error,
                    self._cleanup_owner_handle(),
                )
                if wrapped_error is not cleanup_error:
                    raise wrapped_error from cleanup_error
                raise
            return
        try:
            self.close()
        except _CLEANUP_EXCEPTION as cleanup_error:
            wrapped_error = _cleanup_owner_or_wrap(
                exc_value,
                self._cleanup_owner_handle(),
                cleanup_error=(
                    cleanup_error
                    if isinstance(cleanup_error, CleanupUncertaintyError)
                    else None
                ),
            )
            if wrapped_error is not exc_value:
                raise wrapped_error from exc_value


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

    def __init__(self) -> None:
        self._latest_committed: LedgerSnapshot | None = None

    @property
    def latest_committed(self) -> LedgerSnapshot | None:
        """Return the latest committed generation from the last successful read."""

        return self._latest_committed

    def read(
        self,
        filesystem: StateFilesystem,
        *,
        ledger_name: str | None = None,
    ) -> LedgerSnapshot | None:
        if not isinstance(filesystem, StateFilesystem):
            msg = "filesystem is invalid"
            raise TypeError(msg)
        if ledger_name is None:
            ledger_name = filesystem.ledger_name
        ledger_name = _require_basename(ledger_name, "ledger_name")
        self._latest_committed = None
        before = filesystem.inventory()
        entry = before.entry(ledger_name)
        if entry is None:
            filesystem._fault("after_ledger_absence")
            filesystem.assert_identity(before)
            return None
        if entry.file_type != "regular" or entry.digest is None:
            raise LedgerReadError("recovery ledger is not a safe regular file")
        fd = filesystem.open_existing_regular(ledger_name)
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
            body_error = sys.exc_info()[1]
            try:
                filesystem._close_owned_temporary_fd(
                    fd,
                    entry.identity,
                    "recovery ledger",
                )
            except _CLEANUP_EXCEPTION as cleanup_error:
                cleanup_owners: list[CleanupOwner] = []
                current_owner = getattr(cleanup_error, "cleanup_owner", None)
                if isinstance(current_owner, CleanupOwner):
                    cleanup_owners.append(current_owner)
                elif not (
                    isinstance(cleanup_error, CleanupUncertaintyError)
                    and cleanup_error.cleanup_complete
                    and not filesystem._orphan_fds
                ):
                    cleanup_owners.append(
                        filesystem._temporary_fd_cleanup_owner(
                            fd,
                            entry.identity,
                            "recovery ledger",
                        )
                    )
                if filesystem._orphan_fds:
                    cleanup_owners.append(filesystem._cleanup_owner_handle())
                if not cleanup_owners:
                    cleanup_owners.append(filesystem._cleanup_owner_handle())
                owner = (
                    cleanup_owners[0]
                    if len(cleanup_owners) == 1
                    else _combine_cleanup_owners(*cleanup_owners)
                )
                if body_error is not None:
                    wrapped_error = _cleanup_owner_or_wrap(
                        body_error,
                        owner,
                        cleanup_error=(
                            cleanup_error
                            if isinstance(cleanup_error, CleanupUncertaintyError)
                            else None
                        ),
                    )
                    if wrapped_error is not body_error:
                        raise wrapped_error from body_error
                else:
                    if isinstance(cleanup_error, CleanupUncertaintyError) and not (
                        filesystem._orphan_fds
                    ):
                        raise
                    wrapped_error = _cleanup_owner_or_wrap(cleanup_error, owner)
                    if wrapped_error is not cleanup_error:
                        raise wrapped_error from cleanup_error
                    raise
        raw = b"".join(chunks)
        records = _ledger_records(raw)
        snapshots = [_ledger_snapshot(record) for record in records]
        previous: LedgerSnapshot | None = None
        latest_committed: LedgerSnapshot | None = None
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
                        or snapshot.restore_generation
                        != previous.restore_generation + 1
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
                if snapshot.restore_generation == previous.restore_generation and (
                    snapshot.backup_digest != previous.backup_digest
                    or snapshot.actor != previous.actor
                    or snapshot.audit_ref != previous.audit_ref
                ):
                    raise LedgerReadError(
                        "recovery ledger digest changed in one generation"
                    )
            if snapshot.phase == "RESTORE_COMMITTED":
                latest_committed = snapshot
            previous = snapshot
        filesystem.assert_identity(before)
        self._latest_committed = latest_committed
        return previous


def _read_existing_operation_for_observation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    committed_generation: LedgerSnapshot | None,
) -> _store.ExistingOperationObservation | None:
    """Read one operation with validated restore-invalidated lease evidence."""

    try:
        row, receipt_row = _store._existing_operation_rows(connection, operation_id)
        if row is None:
            return None
        recovery_epoch_row = connection.execute(
            "SELECT value FROM store_meta WHERE key = 'recovery_epoch'"
        ).fetchone()
        fencing_token_floor_row = connection.execute(
            "SELECT value FROM store_meta WHERE key = 'fencing_token_floor'"
        ).fetchone()
        if recovery_epoch_row is None or fencing_token_floor_row is None:
            raise _store.StoreIntegrityError(
                "SQLite store recovery floor metadata is unavailable"
            )
        store_recovery_epoch = _store._require_sqlite_integer(
            recovery_epoch_row["value"],
            "recovery_epoch",
        )
        store_fencing_token_floor = _store._require_sqlite_integer(
            fencing_token_floor_row["value"],
            "fencing_token_floor",
        )
        # A committed restore ledger and matching store floor prove this mismatch is intentional.
        allow_mismatch = (
            committed_generation is not None
            and store_recovery_epoch == committed_generation.recovery_epoch
            and store_fencing_token_floor >= committed_generation.fencing_token_floor
            and row["recovery_epoch"] == store_recovery_epoch
            and row["recovery_epoch"] > row["lease_epoch"]
            and row["status"]
            in {
                "FENCE_PENDING",
                "FENCE_RESERVATION_STARTED",
                "CLAIMED",
                "EFFECT_PREPARED",
                "UNKNOWN_EFFECT",
                "UNKNOWN",
                "COMPLETED",
            }
        )
        return _store._validate_existing_operation_rows(
            row,
            receipt_row,
            allow_recovery_epoch_mismatch=allow_mismatch,
        )
    except _store.StoreError:
        raise
    except sqlite3.DatabaseError as exc:
        raise _store.StoreIntegrityError("SQLite operation observation failed") from exc
    except (TypeError, ValueError, OverflowError) as exc:
        raise _store.StoreIntegrityError(
            "SQLite operation observation is invalid"
        ) from exc


def _is_recovery_cleanup_uncertainty(error: BaseException) -> bool:
    """Recognize the recovery reader's close-status marker without fd access."""

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if str(current) == "recovery read close status is unknown":
            return True
        next_error = current.__cause__
        if next_error is None and not current.__suppress_context__:
            next_error = current.__context__
        current = next_error
    return False


def _run_restore_pair_preflight(
    filesystem: StateFilesystem,
    inventory: FilesetInventory,
) -> bool:
    """Validate the fixed recovery pair through the held root descriptor."""

    try:
        from . import recovery as _recovery
    except ImportError as exc:
        raise _RestorePairReadError(
            "recovery preflight is unavailable",
            tombstone_present=True,
        ) from exc
    tombstone_present = True

    def cleanup_error(cause: BaseException) -> CleanupUncertaintyError:
        error = CleanupUncertaintyError("recovery preflight cleanup status is unknown")
        owner = _find_cleanup_owner(cause)
        if owner is None and filesystem._orphan_fds:
            owner = CleanupOwner(filesystem._retry_orphan_fds)
        if owner is not None:
            error._set_cleanup_owner(owner)
        error.__cause__ = cause
        return error

    try:
        filesystem._retry_orphan_fds()
        tombstone_name = _recovery.RECOVERY_TOMBSTONES_BASENAME
        tombstone_present = inventory.entry(tombstone_name) is not None
        if (
            not tombstone_present
            and filesystem.ledger_name != _recovery.RECOVERY_LEDGER_BASENAME
            and inventory.entry(filesystem.ledger_name) is not None
        ):
            raise _RestorePairReadError(
                "canonical recovery ledger and tombstone pair is missing",
                tombstone_present=False,
            )
        _recovery._normal_open_preflight(
            filesystem._assert_open(),
            retain_fd=filesystem._retain_failed_fd,
        )
    except _RestorePairReadError:
        raise
    except _recovery.RecoveryError as exc:
        if (
            _is_recovery_cleanup_uncertainty(exc)
            or _find_cleanup_owner(exc) is not None
        ):
            raise cleanup_error(exc)
        raise _RestorePairReadError(
            "recovery ledger and tombstone pair is incomplete",
            tombstone_present=tombstone_present,
        ) from exc
    except StateFilesystemError as exc:
        if filesystem._orphan_fds or _find_cleanup_owner(exc) is not None:
            raise cleanup_error(exc)
        raise _RestorePairReadError(
            "recovery preflight is unavailable",
            tombstone_present=tombstone_present,
        ) from exc
    except Exception as exc:
        raise _RestorePairReadError(
            "recovery preflight is unavailable",
            tombstone_present=tombstone_present,
        ) from exc
    return tombstone_present


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
        action = "OPERATOR_REVIEW"
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
        self._owned_cleanup: CleanupOwner | None = None

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

    def _retain_filesystem(self, filesystem: StateFilesystem) -> CleanupOwner:
        owner = filesystem._cleanup_owner_handle()
        if self._owned_cleanup is None:
            self._owned_cleanup = owner
        elif self._owned_cleanup is not owner:
            self._owned_cleanup = _combine_cleanup_owners(
                self._owned_cleanup,
                owner,
            )
        return self._owned_cleanup

    def _retry_owned_cleanup(self) -> None:
        owner = self._owned_cleanup
        if owner is None:
            return
        try:
            owner.retry_cleanup()
        except _CLEANUP_EXCEPTION as error:
            wrapped_error = _cleanup_owner_or_wrap(error, owner)
            if wrapped_error is not error:
                raise wrapped_error from error
            if isinstance(error, DoctorError):
                raise
            cleanup_error = StateFilesystemError(
                "internally-owned filesystem cleanup failed"
            )
            cleanup_error._set_cleanup_owner(owner)
            raise cleanup_error from error
        self._owned_cleanup = None

    def retry_cleanup(self) -> None:
        """Retry cleanup retained by an internally-owned inspection."""

        self._retry_owned_cleanup()

    def close(self) -> None:
        """Release or retry cleanup retained by an internal inspection."""

        self._retry_owned_cleanup()

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
        self._retry_owned_cleanup()
        root = _coerce_root(state_root)
        external_filesystem = self._filesystem
        if external_filesystem is not None:
            try:
                external_filesystem._ensure_ready_for_io()
            except StateFilesystemError as error:
                if (
                    isinstance(error, CleanupUncertaintyError)
                    or error.cleanup_owner is not None
                ):
                    raise
                return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
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
            except WriterActiveError as error:
                if error.cleanup_owner is not None:
                    self._owned_cleanup = error.cleanup_owner
                    raise
                return _report("WRITER_ACTIVE", "MEDIUM", None, "OPERATOR_REVIEW")
            except UnsafeFilesystemError as error:
                if error.cleanup_owner is not None:
                    self._owned_cleanup = error.cleanup_owner
                    raise
                return _report("UNSAFE_SIDECAR", "HIGH", None, "OPERATOR_REVIEW")
            except (StateFilesystemError, OSError) as error:
                if isinstance(error, StateFilesystemError):
                    cleanup_owner = error.cleanup_owner
                    if cleanup_owner is not None:
                        self._owned_cleanup = cleanup_owner
                        raise
                return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
        else:
            if filesystem.state_root != root:
                raise ValueError("state_root does not match filesystem")
        assert filesystem is not None
        try:
            before = filesystem.inventory()
            marker_entry = before.entry(marker)
            from . import recovery as _recovery

            canonical_ledger_name = _recovery.RECOVERY_LEDGER_BASENAME
            canonical_ledger_reader = RecoveryLedgerReader()
            ledger_reader = RecoveryLedgerReader()
            try:
                _run_restore_pair_preflight(filesystem, before)
            except _RestorePairReadError as pair_error:
                if not pair_error.tombstone_present:
                    # Keep the historical bare-ledger diagnosis for malformed
                    # ledgers; a valid ledger without its companion is still
                    # an incomplete restore pair.
                    canonical_ledger_reader.read(
                        filesystem,
                        ledger_name=canonical_ledger_name,
                    )
                _assert_stable(filesystem, before)
                return _report("RESTORE_INCOMPLETE", "HIGH", None, "OPERATOR_REVIEW")
            canonical_snapshot = canonical_ledger_reader.read(
                filesystem,
                ledger_name=canonical_ledger_name,
            )
            if ledger == canonical_ledger_name:
                ledger_reader = canonical_ledger_reader
                ledger_snapshot = canonical_snapshot
            else:
                ledger_snapshot = ledger_reader.read(filesystem, ledger_name=ledger)
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
                _store._validate_existing_image_high_water(connection)
                workflow_row = connection.execute(
                    "SELECT status FROM workflow_operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                provider_row = connection.execute(
                    "SELECT 1 FROM operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if workflow_row is not None and provider_row is not None:
                    _assert_stable(filesystem, before)
                    return _report(
                        "UNREADABLE",
                        "HIGH",
                        None,
                        "OPERATOR_REVIEW",
                    )
                if workflow_row is not None and workflow_row["status"] in {
                    "INTENT",
                    "UNKNOWN_EFFECT",
                }:
                    _assert_stable(filesystem, before)
                    workflow_state: ObservedState = (
                        "INTENT_ONLY"
                        if workflow_row["status"] == "INTENT"
                        else "UNKNOWN_EFFECT"
                    )
                    return _report(
                        workflow_state,
                        "HIGH",
                        None,
                        "OPERATOR_REVIEW",
                    )
                if workflow_row is not None:
                    _assert_stable(filesystem, before)
                    return _report(
                        "UNREADABLE",
                        "HIGH",
                        None,
                        "OPERATOR_REVIEW",
                    )
                observation = _read_existing_operation_for_observation(
                    connection,
                    operation_id,
                    committed_generation=canonical_ledger_reader.latest_committed,
                )
                if observation is None:
                    _assert_stable(filesystem, before)
                    return _report("NOT_FOUND", "HIGH", None, "OPERATOR_REVIEW")
                result = _status_report(observation, marker_present=marker_present)
                _assert_stable(filesystem, before)
                return result
            finally:
                body_error = sys.exc_info()[1]
                cleanup_error: BaseException | None = None
                connection_cleanup_error: CleanupUncertaintyError | None = None
                connection_cleanup_owner: CleanupOwner | None = None
                retained_fd_owner: CleanupOwner | None = None
                if connection is not None:
                    try:
                        _close_temporary_connection(
                            connection, "SQLite doctor database"
                        )
                    except CleanupUncertaintyError as error:
                        cleanup_error = error
                        connection_cleanup_error = error
                        if not error.cleanup_complete:
                            connection_cleanup_owner = CleanupOwner(connection.close)
                            error._set_cleanup_owner(connection_cleanup_owner)
                    except _CLEANUP_EXCEPTION as error:
                        cleanup_error = error
                try:
                    _close_temporary_fd(
                        db_fd,
                        primary.identity,
                        "SQLite doctor database",
                    )
                except _CLEANUP_EXCEPTION as error:
                    if cleanup_error is None:
                        cleanup_error = error
                    try:
                        filesystem._retain_failed_fd(
                            db_fd,
                            primary.identity,
                            "SQLite doctor database",
                        )
                    except _CLEANUP_EXCEPTION as error:
                        owner = getattr(error, "cleanup_owner", None)
                        if isinstance(owner, CleanupOwner):
                            retained_fd_owner = owner
                        else:
                            retained_fd_owner = filesystem._temporary_fd_cleanup_owner(
                                db_fd,
                                primary.identity,
                                "SQLite doctor database",
                            )
                if cleanup_error is not None:
                    cleanup_owners: list[CleanupOwner] = []
                    body_owner = getattr(body_error, "cleanup_owner", None)
                    if isinstance(body_owner, CleanupOwner):
                        cleanup_owners.append(body_owner)
                    if connection_cleanup_error is not None:
                        if connection_cleanup_owner is None and (
                            body_error is not None
                            or not connection_cleanup_error.cleanup_complete
                        ):
                            assert connection is not None
                            connection_cleanup_owner = CleanupOwner(connection.close)
                        if connection_cleanup_owner is not None:
                            cleanup_owners.append(connection_cleanup_owner)
                    if filesystem._orphan_fds:
                        cleanup_owners.append(filesystem._cleanup_owner_handle())
                    if retained_fd_owner is not None:
                        cleanup_owners.append(retained_fd_owner)
                    if not cleanup_owners:
                        cleanup_owners.append(filesystem._cleanup_owner_handle())
                    owner = (
                        cleanup_owners[0]
                        if len(cleanup_owners) == 1
                        else _combine_cleanup_owners(*cleanup_owners)
                    )
                    if body_error is not None:
                        if isinstance(cleanup_error, CleanupUncertaintyError):
                            _mark_cleanup_uncertainty(body_error, cleanup_error)
                        wrapped_error = _cleanup_owner_or_wrap(
                            body_error,
                            owner,
                            cleanup_error=(
                                cleanup_error
                                if isinstance(
                                    cleanup_error,
                                    CleanupUncertaintyError,
                                )
                                else None
                            ),
                        )
                        if wrapped_error is not body_error:
                            raise wrapped_error from body_error
                    elif (
                        isinstance(cleanup_error, CleanupUncertaintyError)
                        and cleanup_error.cleanup_complete
                        and not filesystem._orphan_fds
                    ):
                        raise cleanup_error
                    else:
                        wrapped_error = _cleanup_owner_or_wrap(
                            cleanup_error,
                            owner,
                        )
                        if wrapped_error is not cleanup_error:
                            raise wrapped_error from cleanup_error
                        raise cleanup_error
        except CleanupUncertaintyError:
            raise
        except LedgerReadError as error:
            if _has_cleanup_uncertainty(error):
                raise
            try:
                _assert_stable(filesystem, before)
            except StateFilesystemError:
                return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
            return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
        except _store.StoreMigrationRequiredError as error:
            if _has_cleanup_uncertainty(error):
                raise
            try:
                _assert_stable(filesystem, before)
            except StateFilesystemError:
                return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
            return _report("MIGRATION_REQUIRED", "HIGH", None, "INSPECT_SCHEMA")
        except _store.StoreSchemaError as error:
            if _has_cleanup_uncertainty(error):
                raise
            try:
                _assert_stable(filesystem, before)
            except StateFilesystemError:
                return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
            return _report("SCHEMA_INVALID", "HIGH", None, "INSPECT_SCHEMA")
        except (_store.StoreIntegrityError, sqlite3.DatabaseError) as error:
            if _has_cleanup_uncertainty(error):
                raise
            try:
                _assert_stable(filesystem, before)
            except StateFilesystemError:
                return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
            return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
        except UnsafeFilesystemError as error:
            if _has_cleanup_uncertainty(error):
                raise
            return _report("UNSAFE_SIDECAR", "HIGH", None, "OPERATOR_REVIEW")
        except (UnstableSnapshotError, StateFilesystemError, OSError) as error:
            if _has_cleanup_uncertainty(error):
                raise
            return _report("UNREADABLE", "LOW", None, "OPERATOR_REVIEW")
        finally:
            if owns_filesystem:
                body_error = sys.exc_info()[1]
                try:
                    filesystem.close()
                except _CLEANUP_EXCEPTION as error:
                    owner = self._retain_filesystem(filesystem)
                    if body_error is not None:
                        wrapped_error = _cleanup_owner_or_wrap(
                            body_error,
                            owner,
                            cleanup_error=(
                                error
                                if isinstance(error, CleanupUncertaintyError)
                                else None
                            ),
                        )
                        if wrapped_error is not body_error:
                            raise wrapped_error from body_error
                    elif isinstance(error, DoctorError):
                        error._set_cleanup_owner(owner)
                        raise
                    else:
                        cleanup_error = StateFilesystemError(
                            "internally-owned filesystem cleanup failed"
                        )
                        cleanup_error._set_cleanup_owner(owner)
                        raise cleanup_error from error
                else:
                    self._owned_cleanup = None


def doctor(
    state_root: Path,
    operation_id: str,
    *,
    marker_name: str,
    ledger_name: str,
) -> DoctorReport:
    """Convenience wrapper around :class:`ReadOnlyDoctor`."""

    instance = ReadOnlyDoctor(
        marker_name=marker_name,
        ledger_name=ledger_name,
    )
    try:
        return instance.inspect(state_root, operation_id)
    except BaseException as error:
        cleanup_owner = instance._owned_cleanup
        if cleanup_owner is not None:
            wrapped_error = _cleanup_owner_or_wrap(error, cleanup_owner)
            if wrapped_error is not error:
                raise wrapped_error from error
        raise


__all__ = [
    "DOCTOR_PROTOCOL_VERSION",
    "RECOVERY_LEDGER_VERSION",
    "WRITER_MARKER_BASENAME",
    "CleanupOwner",
    "CleanupOwnerError",
    "CleanupUncertaintyError",
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
