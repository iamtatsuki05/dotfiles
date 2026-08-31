"""Verified SQLite backup artifacts for the local coordination store.

This module owns only the backup artifact format and its two-file publication
protocol.  Quiescence, SQLite backup, schema validation, and recovery-floor
observation remain owned by the WAL controller and the coordination store.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, NoReturn, Self

from . import doctor as _doctor
from . import store as _store
from .lease import StoreImageObservation
from .wal import (
    _CLEANUP_EXCEPTION,
    CheckpointRequest,
    DatabaseCopyResult,
    DatabaseCopyTarget,
    QuiescenceSession,
    WalSidecarController,
)

BACKUP_MANIFEST_VERSION: Final[int] = 1
BACKUP_MANIFEST_FIELDS: Final[tuple[str, ...]] = (
    "version",
    "database_basename",
    "store_schema",
    "event_schema_version",
    "sqlite_user_version",
    "integrity_check",
    "database_size",
    "database_digest",
    "captured_recovery_epoch",
    "captured_fencing_token_floor",
)
MAX_MANIFEST_BYTES: Final[int] = 64 * 1024
MAX_BACKUP_BYTES: Final[int] = _doctor.MAX_DATABASE_BYTES
_MAX_TEMP_ATTEMPTS: Final[int] = 8
_TEMP_NONCE_BYTES: Final[int] = 16
_MANIFEST_SUFFIX: Final[str] = ".manifest"
_RESTORE_CANDIDATE_PREFIX: Final[str] = ".coordination.sqlite3.restore-"
_OrphanFD = tuple[int, tuple[int, int] | None, str]
_MAX_ORPHAN_CONTROLLERS: Final[int] = 4
_MAX_ORPHAN_FDS: Final[int] = 16
_MAX_ORPHAN_SESSIONS: Final[int] = 4
_SIDECAR_SUFFIXES: Final[tuple[str, ...]] = ("-wal", "-shm", "-journal")
_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {
        _store.DATABASE_FILENAME,
        *(f"{_store.DATABASE_FILENAME}{suffix}" for suffix in _SIDECAR_SUFFIXES),
        _store.WRITER_MARKER_FILENAME,
        _store.LIFETIME_GATE_FILENAME,
        "recovery.ledger",
        "recovery.tombstones",
    }
)


class BackupError(_store.StoreError):
    """Base class for backup artifact failures."""


class BackupIncompleteError(BackupError):
    """A pair is partial, mixed, or otherwise not a complete artifact."""


class BackupIntegrityError(BackupError):
    """Manifest, database bytes, or Store-owned image evidence disagrees."""


class BackupFilesystemError(BackupError):
    """A path, descriptor, file type, or filesystem identity is unsafe."""


class BackupDurabilityUnknownError(BackupError):
    """A required fsync did not return a durable success."""


class BackupIntegrationError(BackupError):
    """A required Store-owned image observation seam is unavailable."""


def _require_int(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= _store.SQLITE_INTEGER_MAX:
        raise ValueError(f"{name} is invalid")
    return value


def _require_identity(value: object, name: str) -> tuple[int, int]:
    if (
        type(value) is not tuple
        or len(value) != 2
        or any(type(item) is not int or item < 0 for item in value)
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _require_digest(value: object, name: str = "digest") -> str:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _require_basename(value: object, name: str) -> str:
    try:
        return _doctor._require_basename(value, name)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is invalid") from exc


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """The canonical ten-field description of one backup database image."""

    version: int
    database_basename: str
    store_schema: int
    event_schema_version: int
    sqlite_user_version: int
    integrity_check: Literal["ok"]
    database_size: int
    database_digest: str
    captured_recovery_epoch: int
    captured_fencing_token_floor: int

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != BACKUP_MANIFEST_VERSION:
            raise ValueError("manifest version is unsupported")
        _require_basename(self.database_basename, "database_basename")
        if (
            self.database_basename in _RESERVED_NAMES
            or self.database_basename.startswith(_RESTORE_CANDIDATE_PREFIX)
        ):
            raise ValueError("manifest database basename is reserved")
        derived_manifest = f"{self.database_basename}{_MANIFEST_SUFFIX}"
        _require_basename(derived_manifest, "manifest_basename")
        if any(
            len(derived.encode("utf-8")) > 255
            for derived in (
                derived_manifest,
                *(f"{self.database_basename}{suffix}" for suffix in _SIDECAR_SUFFIXES),
            )
        ):
            raise ValueError("manifest derived basename is too long")
        if (
            type(self.store_schema) is not int
            or self.store_schema != _store.STORE_SCHEMA
        ):
            raise ValueError("manifest store schema is unsupported")
        if (
            type(self.event_schema_version) is not int
            or self.event_schema_version != _store.EVENT_SCHEMA_VERSION
        ):
            raise ValueError("manifest event schema is unsupported")
        if (
            type(self.sqlite_user_version) is not int
            or self.sqlite_user_version != _store.STORE_SCHEMA
        ):
            raise ValueError("manifest SQLite user version is unsupported")
        if type(self.integrity_check) is not str or self.integrity_check != "ok":
            raise ValueError("manifest integrity result is unsupported")
        if (
            type(self.database_size) is not int
            or not 0 <= self.database_size <= MAX_BACKUP_BYTES
        ):
            raise ValueError("manifest database size is invalid")
        _require_digest(self.database_digest, "database_digest")
        _require_int(self.captured_recovery_epoch, "captured_recovery_epoch")
        _require_int(
            self.captured_fencing_token_floor,
            "captured_fencing_token_floor",
        )


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    """A verified observation of one final database/manifest pair."""

    database_basename: str
    manifest_basename: str
    manifest: BackupManifest
    database_identity: tuple[int, int]
    manifest_identity: tuple[int, int]
    workflow_row_counts: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        database_basename = _require_basename(
            self.database_basename,
            "database_basename",
        )
        manifest_basename = _require_basename(
            self.manifest_basename,
            "manifest_basename",
        )
        if manifest_basename != f"{database_basename}{_MANIFEST_SUFFIX}":
            raise ValueError("manifest basename does not match database basename")
        if type(self.manifest) is not BackupManifest:
            raise TypeError("manifest is invalid")
        if self.manifest.database_basename != database_basename:
            raise ValueError("manifest database basename does not match artifact")
        _require_identity(self.database_identity, "database_identity")
        _require_identity(self.manifest_identity, "manifest_identity")
        if (
            type(self.workflow_row_counts) is not tuple
            or len(self.workflow_row_counts) != 4
            or any(
                type(value) is not int or not 0 <= value <= _store.SQLITE_INTEGER_MAX
                for value in self.workflow_row_counts
            )
        ):
            raise ValueError("artifact workflow row counts are invalid")

    @property
    def workflow_rows_present(self) -> bool:
        return any(self.workflow_row_counts)


def _manifest_values(manifest: BackupManifest) -> dict[str, object]:
    if type(manifest) is not BackupManifest:
        raise TypeError("manifest must be a BackupManifest")
    try:
        normalized = BackupManifest(
            version=manifest.version,
            database_basename=manifest.database_basename,
            store_schema=manifest.store_schema,
            event_schema_version=manifest.event_schema_version,
            sqlite_user_version=manifest.sqlite_user_version,
            integrity_check=manifest.integrity_check,
            database_size=manifest.database_size,
            database_digest=manifest.database_digest,
            captured_recovery_epoch=manifest.captured_recovery_epoch,
            captured_fencing_token_floor=manifest.captured_fencing_token_floor,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise BackupIntegrityError("manifest values are invalid") from exc
    return {name: getattr(normalized, name) for name in BACKUP_MANIFEST_FIELDS}


def _encode_manifest(manifest: BackupManifest) -> bytes:
    """Encode one manifest in the fixed canonical JSON representation."""

    mapping = _manifest_values(manifest)
    try:
        encoded = (
            json.dumps(
                mapping,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise BackupIntegrityError("manifest cannot be encoded") from exc
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise BackupIntegrityError("manifest is too large")
    return encoded


def _json_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("manifest contains a duplicate field")
        result[key] = value
    return result


def _decode_manifest(raw: bytes) -> BackupManifest:
    """Decode and strictly validate canonical manifest bytes."""

    if type(raw) is not bytes or not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise BackupIntegrityError("manifest bytes are invalid")
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text, object_pairs_hook=_json_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BackupIntegrityError("manifest JSON is invalid") from exc
    if type(parsed) is not dict:
        raise BackupIntegrityError("manifest must be one JSON object")
    if tuple(parsed) != BACKUP_MANIFEST_FIELDS:
        raise BackupIntegrityError("manifest fields are not canonical")
    try:
        manifest = BackupManifest(**parsed)
    except (TypeError, ValueError, KeyError) as exc:
        raise BackupIntegrityError("manifest values are invalid") from exc
    if _encode_manifest(manifest) != raw:
        raise BackupIntegrityError("manifest bytes are not canonical")
    return manifest


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _security_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        stat.S_IFMT(metadata.st_mode),
        metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_dev,
        metadata.st_ino,
    )


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


def _backup_error(message: str, cause: BaseException) -> NoReturn:
    raise BackupFilesystemError(message) from cause


def _remember_orphan_fd(
    registry: list[_OrphanFD],
    fd: int,
    expected_identity: tuple[int, int] | None,
    label: str,
    *,
    cleanup_callback: Callable[[], None] | None = None,
) -> None:
    try:
        metadata = os.fstat(fd)
    except _CLEANUP_EXCEPTION as status_error:
        if isinstance(status_error, OSError) and status_error.errno == errno.EBADF:
            return
        actual_identity = expected_identity
    else:
        actual_identity = _identity(metadata)
        if expected_identity is not None and actual_identity != expected_identity:
            raise BackupDurabilityUnknownError(f"{label} descriptor was reused")
    for existing_fd, existing_identity, _ in registry:
        if existing_fd != fd:
            continue
        if existing_identity == actual_identity:
            return
        error = BackupDurabilityUnknownError(f"{label} descriptor was reused")
        if cleanup_callback is not None:
            _store._attach_cleanup_capability(
                error,
                _store._CleanupCapability(cleanup_callback),
            )
        raise error
    if len(registry) >= _MAX_ORPHAN_FDS:
        error = BackupDurabilityUnknownError("backup descriptor retry registry is full")
        if cleanup_callback is not None:
            _store._attach_cleanup_capability(
                error,
                _store._CleanupCapability(cleanup_callback),
            )
        raise error
    registry.append((fd, actual_identity, label))


def _close_fd_checked(
    fd: int,
    expected_identity: tuple[int, int] | None,
    label: str,
    *,
    orphan_registry: list[_OrphanFD] | None = None,
    cleanup_callback: Callable[[], None] | None = None,
) -> None:
    try:
        _doctor._close_temporary_fd(fd, expected_identity, label)
    except _CLEANUP_EXCEPTION as exc:
        if orphan_registry is not None:
            overflow_callback: Callable[[], None] | None = None
            if cleanup_callback is not None:

                def retry_current_fd() -> None:
                    try:
                        metadata = os.fstat(fd)
                    except _CLEANUP_EXCEPTION as status_error:
                        if (
                            isinstance(status_error, OSError)
                            and status_error.errno == errno.EBADF
                        ):
                            cleanup_callback()
                            return
                        raise BackupDurabilityUnknownError(
                            f"{label} descriptor status is unknown"
                        ) from status_error
                    if expected_identity is None:
                        raise BackupDurabilityUnknownError(
                            f"{label} descriptor identity is unavailable"
                        )
                    if _identity(metadata) != expected_identity:
                        raise BackupDurabilityUnknownError(
                            f"{label} descriptor was reused"
                        )
                    _close_fd_checked(fd, expected_identity, label)
                    cleanup_callback()

                overflow_callback = retry_current_fd
            _remember_orphan_fd(
                orphan_registry,
                fd,
                expected_identity,
                label,
                cleanup_callback=overflow_callback,
            )
        raise BackupDurabilityUnknownError(f"{label} close status is unknown") from exc


def _close_fds(
    entries: tuple[tuple[int | None, tuple[int, int] | None, str], ...],
    *,
    orphan_registry: list[_OrphanFD] | None = None,
    cleanup_callback: Callable[[], None] | None = None,
) -> None:
    first_error: BackupDurabilityUnknownError | None = None
    for fd, expected_identity, label in entries:
        if fd is None:
            continue
        try:
            _close_fd_checked(
                fd,
                expected_identity,
                label,
                orphan_registry=orphan_registry,
                cleanup_callback=cleanup_callback,
            )
        except BackupDurabilityUnknownError as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


@dataclass(frozen=True, slots=True)
class _PairPrecondition:
    database_signature: tuple[int, ...] | None
    manifest_signature: tuple[int, ...] | None
    database_digest: str | None = None
    manifest_digest: str | None = None


class SQLiteBackup:
    """Create and inspect one current backup pair in an existing state root."""

    __slots__ = (
        "_busy_timeout_ms",
        "_orphan_controllers",
        "_orphan_fds",
        "_orphan_sessions",
        "_state_root",
    )

    def __init__(
        self,
        state_root: Path,
        *,
        busy_timeout_ms: int = _store.DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if (
            type(busy_timeout_ms) is not int
            or not 1 <= busy_timeout_ms <= _store.MAX_BUSY_TIMEOUT_MS
        ):
            raise ValueError(
                f"busy_timeout_ms must be between 1 and {_store.MAX_BUSY_TIMEOUT_MS}"
            )
        try:
            self._state_root = _doctor._coerce_root(
                _store._coerce_state_root(state_root)
            )
        except (_store.StoreError, TypeError, ValueError) as exc:
            raise ValueError("state_root is invalid") from exc
        self._busy_timeout_ms = busy_timeout_ms
        self._orphan_controllers: list[WalSidecarController] = []
        self._orphan_fds: list[_OrphanFD] = []
        self._orphan_sessions: list[QuiescenceSession] = []

    @property
    def state_root(self) -> Path:
        return self._state_root

    @property
    def busy_timeout_ms(self) -> int:
        return self._busy_timeout_ms

    def _fault(self, point: str) -> None:
        """Deterministic fault-injection seam; production implementation is a no-op."""

        del point

    def _next_nonce(self) -> str:
        return secrets.token_hex(_TEMP_NONCE_BYTES)

    def _new_controller(self) -> WalSidecarController:
        return WalSidecarController(
            self.state_root,
            busy_timeout_ms=self.busy_timeout_ms,
        )

    def _attach_cleanup_capability(
        self,
        error: BaseException,
        callback: Callable[[], None] | None = None,
    ) -> BaseException:
        retry = self.close if callback is None else callback
        capability = _store._CleanupCapability(retry)
        attached_error: BaseException | None = None
        try:
            attached_error = _store._attach_cleanup_capability(error, capability)
        except _CLEANUP_EXCEPTION:
            pass
        if attached_error is not None and attached_error is not error:
            try:
                returned_capability = _store._extract_cleanup_capability(attached_error)
            except _CLEANUP_EXCEPTION:
                returned_capability = None
            if returned_capability is not None:
                capability = returned_capability
        try:
            attached = callable(getattr(error, "retry_cleanup", None))
        except _CLEANUP_EXCEPTION:
            attached = False
        if attached:
            return error
        wrapper = BackupDurabilityUnknownError("backup cleanup status is unknown")
        wrapper.__cause__ = error
        _store._attach_cleanup_capability(wrapper, capability)
        return wrapper

    def _adopt_cleanup_capability(
        self,
        error: BaseException,
        owner_error: BaseException,
    ) -> BaseException:
        if error is owner_error:
            return error
        try:
            capability = _store._extract_cleanup_capability(owner_error)
        except _CLEANUP_EXCEPTION:
            capability = None
        if capability is None:
            return error
        return self._attach_cleanup_capability(error, capability.retry_cleanup)

    def _retain_controller(self, controller: WalSidecarController) -> None:
        if any(existing is controller for existing in self._orphan_controllers):
            return
        if len(self._orphan_controllers) >= _MAX_ORPHAN_CONTROLLERS:
            error = BackupDurabilityUnknownError(
                "backup controller retry registry is full"
            )
            retry = lambda: controller.close()
            wrapped = self._attach_cleanup_capability(error, retry)
            if wrapped is not error:
                raise wrapped from error
            raise error
        self._orphan_controllers.append(controller)

    def _retry_orphan_controllers(self) -> None:
        remaining: list[WalSidecarController] = []
        first_error: BaseException | None = None
        for controller in self._orphan_controllers:
            try:
                controller.close()
            except _CLEANUP_EXCEPTION as error:
                remaining.append(controller)
                if first_error is None:
                    first_error = error
        self._orphan_controllers = remaining
        if first_error is not None:
            raise first_error

    def _hold_quiescence(self, controller: WalSidecarController) -> QuiescenceSession:
        try:
            return controller.hold_quiescence()
        except _CLEANUP_EXCEPTION as acquisition_error:
            try:
                controller.close()
            except _CLEANUP_EXCEPTION as cleanup_error:
                try:
                    self._retain_controller(controller)
                except _CLEANUP_EXCEPTION as retention_error:
                    primary = self._adopt_cleanup_capability(
                        acquisition_error,
                        retention_error,
                    )
                    wrapped = self._attach_cleanup_capability(primary)
                    if wrapped is not primary:
                        raise wrapped from acquisition_error
                    raise primary from cleanup_error
                primary = self._attach_cleanup_capability(acquisition_error)
                if primary is not acquisition_error:
                    raise primary from acquisition_error
                raise acquisition_error from cleanup_error
            raise

    def _retry_orphan_fds(self) -> None:
        remaining: list[_OrphanFD] = []
        first_error: BackupDurabilityUnknownError | None = None
        for fd, expected_identity, label in self._orphan_fds:
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
                    first_error = BackupDurabilityUnknownError(
                        f"{label} descriptor status is unknown"
                    )
                continue
            if expected_identity is None:
                remaining.append((fd, None, label))
                if first_error is None:
                    first_error = BackupDurabilityUnknownError(
                        f"{label} descriptor identity is unavailable"
                    )
                continue
            actual_identity = _identity(metadata)
            if expected_identity is not None and actual_identity != expected_identity:
                if first_error is None:
                    first_error = BackupDurabilityUnknownError(
                        f"{label} descriptor was reused"
                    )
                continue
            close_identity = expected_identity or actual_identity
            try:
                _close_fd_checked(fd, close_identity, label)
            except BackupDurabilityUnknownError as error:
                try:
                    retry_metadata = os.fstat(fd)
                except _CLEANUP_EXCEPTION as status_error:
                    if (
                        isinstance(status_error, OSError)
                        and status_error.errno == errno.EBADF
                    ):
                        continue
                    remaining.append((fd, close_identity, label))
                    if first_error is None:
                        first_error = BackupDurabilityUnknownError(
                            f"{label} descriptor status is unknown"
                        )
                    continue
                if _identity(retry_metadata) != close_identity:
                    if first_error is None:
                        first_error = BackupDurabilityUnknownError(
                            f"{label} descriptor was reused"
                        )
                    continue
                remaining.append((fd, close_identity, label))
                if first_error is None:
                    first_error = error
        self._orphan_fds = remaining
        if first_error is not None:
            raise first_error

    def _retain_session(self, session: QuiescenceSession) -> None:
        if any(existing is session for existing in self._orphan_sessions):
            return
        if len(self._orphan_sessions) >= _MAX_ORPHAN_SESSIONS:
            error = BackupDurabilityUnknownError(
                "backup session retry registry is full"
            )
            retry = lambda: session.close()
            wrapped = self._attach_cleanup_capability(error, retry)
            if wrapped is not error:
                raise wrapped from error
            raise error
        self._orphan_sessions.append(session)

    def _retry_orphan_sessions(self) -> None:
        remaining: list[QuiescenceSession] = []
        first_error: BaseException | None = None
        for session in self._orphan_sessions:
            try:
                session.close()
            except _CLEANUP_EXCEPTION as error:
                remaining.append(session)
                if first_error is None:
                    first_error = error
        self._orphan_sessions = remaining
        if first_error is not None:
            raise first_error

    def _retry_retained_resources(self) -> None:
        first_error: BaseException | None = None
        for retry in (
            self._retry_orphan_controllers,
            self._retry_orphan_sessions,
            self._retry_orphan_fds,
        ):
            try:
                retry()
            except _CLEANUP_EXCEPTION as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            self._attach_cleanup_capability(first_error)
            raise first_error

    @contextmanager
    def _session_lifecycle(
        self,
        session: QuiescenceSession,
    ) -> Iterator[QuiescenceSession]:
        body_error: BaseException | None = None
        try:
            yield session
        except _CLEANUP_EXCEPTION as error:
            body_error = error
            raise
        finally:
            try:
                session.close()
            except _CLEANUP_EXCEPTION as cleanup_error:
                try:
                    self._retain_session(session)
                except _CLEANUP_EXCEPTION as retention_error:
                    primary = body_error if body_error is not None else cleanup_error
                    primary = self._adopt_cleanup_capability(
                        primary,
                        retention_error,
                    )
                    wrapped = self._attach_cleanup_capability(primary)
                    if body_error is None:
                        if wrapped is not primary:
                            raise wrapped from cleanup_error
                        raise primary
                    if wrapped is not primary:
                        raise wrapped from body_error
                    if primary is not body_error:
                        raise primary from body_error
                    raise primary
                primary_error = body_error if body_error is not None else cleanup_error
                wrapped = self._attach_cleanup_capability(primary_error)
                if body_error is None:
                    if wrapped is not primary_error:
                        raise wrapped from cleanup_error
                    raise
                if wrapped is not primary_error:
                    raise wrapped from primary_error

    def close(self) -> None:
        """Retry resources retained after an uncertain close."""

        self._retry_retained_resources()

    def __enter__(self) -> Self:
        self._retry_retained_resources()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        del exc_type, traceback
        try:
            self.close()
        except _CLEANUP_EXCEPTION as cleanup_error:
            if isinstance(exc_value, BaseException):
                wrapped = self._attach_cleanup_capability(exc_value)
                if wrapped is not exc_value:
                    raise wrapped from exc_value
                return
            wrapped = self._attach_cleanup_capability(cleanup_error)
            if wrapped is not cleanup_error:
                raise wrapped from cleanup_error
            raise

    def _validate_destination_names(self, destination_name: str) -> tuple[str, str]:
        name = _require_basename(destination_name, "destination_name")
        if any(character in name for character in "*?[]"):
            raise ValueError("destination name contains a wildcard")
        manifest_name = _require_basename(
            f"{name}{_MANIFEST_SUFFIX}",
            "manifest_basename",
        )
        derived_names = (
            manifest_name,
            *(f"{name}{suffix}" for suffix in _SIDECAR_SUFFIXES),
        )
        if any(
            item in _RESERVED_NAMES or item.startswith(_RESTORE_CANDIDATE_PREFIX)
            for item in (name, *derived_names)
        ):
            raise ValueError("destination name is reserved")
        if any(len(item.encode("utf-8")) > 255 for item in (name, *derived_names)):
            raise ValueError("destination or derived name is too long")
        return name, manifest_name

    def _open_root(self) -> tuple[int, tuple[int, int], tuple[int, ...]]:
        root_fd: int | None = None
        root_expected_identity: tuple[int, int] | None = None
        try:
            root_fd = _store._open_state_root(self.state_root)
        except (_store.StoreError, OSError) as exc:
            open_error = BackupFilesystemError("state root cannot be opened")
            try:
                lower_capability = _store._extract_cleanup_capability(exc)
            except _CLEANUP_EXCEPTION:
                lower_capability = None
            if lower_capability is not None:
                wrapped = self._attach_cleanup_capability(
                    open_error,
                    lower_capability.retry_cleanup,
                )
                if wrapped is not open_error:
                    raise wrapped from exc
            raise open_error from exc
        try:
            metadata = os.fstat(root_fd)
            root_expected_identity = _identity(metadata)
            path_metadata = os.stat(self.state_root, follow_symlinks=False)
            _store._validate_directory_fd(root_fd, state_root=True)
            if root_expected_identity != _identity(path_metadata):
                raise BackupFilesystemError("state root changed while opening")
            signature = _security_signature(metadata)
            if signature != _security_signature(path_metadata):
                raise BackupFilesystemError("state root metadata changed while opening")
            return root_fd, root_expected_identity, signature
        except _CLEANUP_EXCEPTION as exc:
            if root_fd is not None:
                try:
                    _close_fd_checked(
                        root_fd,
                        root_expected_identity,
                        "backup root",
                        orphan_registry=self._orphan_fds,
                        cleanup_callback=self.close,
                    )
                except BackupDurabilityUnknownError as close_error:
                    if isinstance(exc, BackupError):
                        primary = self._adopt_cleanup_capability(exc, close_error)
                        wrapped = self._attach_cleanup_capability(primary)
                        if wrapped is not primary:
                            raise wrapped from exc
                        if primary is not exc:
                            raise primary from exc
                        raise primary
                    cleanup_primary: BaseException = BackupFilesystemError(
                        "state root cannot be inspected"
                    )
                    cleanup_primary = self._adopt_cleanup_capability(
                        cleanup_primary,
                        close_error,
                    )
                    wrapped = self._attach_cleanup_capability(cleanup_primary)
                    if wrapped is not cleanup_primary:
                        raise wrapped from exc
                    raise cleanup_primary from exc
            if isinstance(exc, BackupError):
                raise
            _backup_error("state root cannot be inspected", exc)

    def _assert_root(
        self,
        root_fd: int,
        expected_identity: tuple[int, int],
        expected_signature: tuple[int, ...],
    ) -> None:
        try:
            metadata = os.fstat(root_fd)
            path_metadata = os.stat(self.state_root, follow_symlinks=False)
            _store._validate_directory_fd(root_fd, state_root=True)
        except _CLEANUP_EXCEPTION as exc:
            _backup_error("state root cannot be revalidated", exc)
        if (
            _identity(metadata) != expected_identity
            or _identity(path_metadata) != expected_identity
            or _security_signature(metadata) != expected_signature
            or _security_signature(path_metadata) != expected_signature
        ):
            raise BackupFilesystemError("state root changed while publishing")

    @staticmethod
    def _lstat(
        root_fd: int,
        name: str,
        *,
        label: str,
    ) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            _backup_error(f"{label} cannot be inspected", exc)

    @staticmethod
    def _validate_regular(
        metadata: os.stat_result,
        *,
        label: str,
        max_size: int | None = None,
    ) -> None:
        if _doctor._unsafe_regular(metadata):
            raise BackupFilesystemError(f"{label} is unsafe")
        if max_size is not None and metadata.st_size > max_size:
            raise BackupIntegrityError(f"{label} is too large")

    def _open_existing_regular(
        self,
        root_fd: int,
        name: str,
        *,
        label: str,
        max_size: int | None = None,
    ) -> tuple[int, os.stat_result]:
        before = self._lstat(root_fd, name, label=label)
        if before is None:
            raise BackupIncompleteError(f"{label} is missing")
        self._validate_regular(before, label=label, max_size=max_size)
        try:
            fd = os.open(
                name,
                _doctor._read_only_open_flags(directory=False),
                dir_fd=root_fd,
            )
        except (_doctor.StateFilesystemError, _store.StoreError, OSError) as exc:
            _backup_error(f"{label} cannot be opened", exc)
        opened_identity: tuple[int, int] | None = _identity(before)
        try:
            opened = os.fstat(fd)
            opened_identity = _identity(opened)
            after = self._lstat(root_fd, name, label=label)
            if (
                after is None
                or _metadata_signature(opened) != _metadata_signature(before)
                or _metadata_signature(opened) != _metadata_signature(after)
            ):
                raise BackupFilesystemError(f"{label} changed while opening")
            self._validate_regular(opened, label=label, max_size=max_size)
            return fd, opened
        except _CLEANUP_EXCEPTION as original_error:
            try:
                _close_fd_checked(
                    fd,
                    opened_identity,
                    label,
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                )
            except BackupDurabilityUnknownError as close_error:
                primary = self._adopt_cleanup_capability(
                    original_error,
                    close_error,
                )
                wrapped = self._attach_cleanup_capability(primary)
                if wrapped is not primary:
                    raise wrapped from original_error
                if primary is not original_error:
                    raise primary from original_error
                raise primary
            raise

    def _create_manifest_temp(
        self,
        root_fd: int,
        name: str,
    ) -> tuple[int, os.stat_result]:
        if self._lstat(root_fd, name, label="manifest temp") is not None:
            raise BackupFilesystemError("manifest temp already exists")
        nonblock = getattr(os, "O_NONBLOCK", 0)
        if nonblock == 0:
            raise BackupFilesystemError("non-blocking manifest open is unavailable")
        try:
            fd = os.open(
                name,
                _store._open_flags(directory=False, writable=True)
                | os.O_CREAT
                | os.O_EXCL
                | nonblock,
                0o600,
                dir_fd=root_fd,
            )
        except FileExistsError as exc:
            _backup_error("manifest temp appeared during creation", exc)
        except (_store.StoreError, OSError) as exc:
            _backup_error("manifest temp cannot be created", exc)
        temporary_identity: tuple[int, int] | None = None
        try:
            metadata = os.fstat(fd)
            temporary_identity = _identity(metadata)
            path_metadata = self._lstat(root_fd, name, label="manifest temp")
            if path_metadata is None:
                raise BackupFilesystemError("manifest temp disappeared while creating")
            self._validate_regular(
                metadata, label="manifest temp", max_size=MAX_MANIFEST_BYTES
            )
            if _identity(metadata) != _identity(path_metadata) or metadata.st_size != 0:
                raise BackupFilesystemError("manifest temp changed while creating")
            return fd, metadata
        except _CLEANUP_EXCEPTION as original_error:
            try:
                _close_fd_checked(
                    fd,
                    temporary_identity,
                    "manifest temp",
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                )
            except BackupDurabilityUnknownError as close_error:
                primary = self._adopt_cleanup_capability(
                    original_error,
                    close_error,
                )
                wrapped = self._attach_cleanup_capability(primary)
                if wrapped is not primary:
                    raise wrapped from original_error
                if primary is not original_error:
                    raise primary from original_error
                raise primary
            raise

    @staticmethod
    def _write_all(fd: int, content: bytes, *, label: str) -> None:
        offset = 0
        try:
            while offset < len(content):
                written = os.pwrite(fd, content[offset:], offset)
                if written <= 0:
                    raise OSError(f"{label} write was incomplete")
                offset += written
        except OSError as exc:
            _backup_error(f"{label} cannot be written", exc)

    @staticmethod
    def _read_bounded_fd(
        fd: int, *, label: str, limit: int
    ) -> tuple[bytes, os.stat_result]:
        try:
            before = os.fstat(fd)
        except _CLEANUP_EXCEPTION as exc:
            _backup_error(f"{label} descriptor cannot be inspected", exc)
        if before.st_size > limit:
            raise BackupIntegrityError(f"{label} is too large")
        chunks: list[bytes] = []
        offset = 0
        while offset < before.st_size:
            try:
                chunk = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
            except OSError as exc:
                _backup_error(f"{label} cannot be read", exc)
            if not chunk:
                raise BackupFilesystemError(f"{label} ended while reading")
            chunks.append(chunk)
            offset += len(chunk)
        try:
            after = os.fstat(fd)
        except _CLEANUP_EXCEPTION as exc:
            _backup_error(f"{label} changed while reading", exc)
        if _metadata_signature(before) != _metadata_signature(after):
            raise BackupFilesystemError(f"{label} changed while reading")
        return b"".join(chunks), before

    @staticmethod
    def _store_image(fd: int) -> StoreImageObservation:
        inspector = getattr(_store, "_inspect_image_fd", None)
        if not callable(inspector):
            inspector = getattr(_store.CoordinationStore, "_inspect_image_fd", None)
        if not callable(inspector):
            raise BackupIntegrationError("Store image observation seam is unavailable")
        try:
            observation = inspector(fd)
        except _store.StoreSchemaError as exc:
            raise BackupIntegrityError("SQLite backup image schema is invalid") from exc
        except _store.StoreIntegrityError as exc:
            raise BackupIntegrityError("SQLite backup image is invalid") from exc
        except _store.StoreError as exc:
            raise BackupFilesystemError(
                "SQLite backup image cannot be inspected"
            ) from exc
        if type(observation) is not StoreImageObservation:
            raise BackupIntegrationError("Store image observation has the wrong type")
        return observation

    def _assert_fd_path(
        self,
        root_fd: int,
        fd: int,
        name: str,
        *,
        label: str,
        expected_identity: tuple[int, int],
        expected_signature: tuple[int, ...],
        allow_missing: bool = False,
    ) -> os.stat_result | None:
        try:
            fd_metadata = os.fstat(fd)
            path_metadata = self._lstat(root_fd, name, label=label)
        except _CLEANUP_EXCEPTION as exc:
            if isinstance(exc, BackupError):
                raise
            _backup_error(f"{label} identity cannot be checked", exc)
        if path_metadata is None:
            if allow_missing:
                return None
            raise BackupFilesystemError(f"{label} disappeared")
        self._validate_regular(fd_metadata, label=label, max_size=MAX_BACKUP_BYTES)
        self._validate_regular(path_metadata, label=label, max_size=MAX_BACKUP_BYTES)
        if (
            _identity(fd_metadata) != expected_identity
            or _identity(path_metadata) != expected_identity
            or _security_signature(fd_metadata)
            != (
                expected_signature[0],
                expected_signature[1],
                expected_signature[2],
                expected_signature[4],
                expected_signature[5],
            )
            or _security_signature(path_metadata)
            != (
                expected_signature[0],
                expected_signature[1],
                expected_signature[2],
                expected_signature[4],
                expected_signature[5],
            )
        ):
            raise BackupFilesystemError(f"{label} identity changed")
        return path_metadata

    def _check_sidecars(self, root_fd: int, name: str) -> None:
        for suffix in _SIDECAR_SUFFIXES:
            sidecar_name = f"{name}{suffix}"
            if self._lstat(root_fd, sidecar_name, label=sidecar_name) is not None:
                raise BackupFilesystemError(f"backup sidecar exists: {sidecar_name}")

    def _inspect_pair(
        self,
        root_fd: int,
        database_name: str,
    ) -> tuple[BackupArtifact, _PairPrecondition]:
        _, manifest_name = self._validate_destination_names(database_name)
        self._check_sidecars(root_fd, database_name)
        database_fd, database_metadata = self._open_existing_regular(
            root_fd,
            database_name,
            label="backup database",
            max_size=MAX_BACKUP_BYTES,
        )
        manifest_fd: int | None = None
        manifest_identity: tuple[int, int] | None = None
        body_error: BaseException | None = None
        try:
            observation = self._store_image(database_fd)
            database_after = self._assert_fd_path(
                root_fd,
                database_fd,
                database_name,
                label="backup database",
                expected_identity=_identity(database_metadata),
                expected_signature=_metadata_signature(database_metadata),
            )
            if database_after is None:
                raise BackupFilesystemError("backup database disappeared")
            manifest_fd, manifest_metadata = self._open_existing_regular(
                root_fd,
                manifest_name,
                label="backup manifest",
                max_size=MAX_MANIFEST_BYTES,
            )
            manifest_identity = _identity(manifest_metadata)
            raw_manifest, _ = self._read_bounded_fd(
                manifest_fd,
                label="backup manifest",
                limit=MAX_MANIFEST_BYTES,
            )
            manifest = _decode_manifest(raw_manifest)
            if manifest.database_basename != database_name:
                raise BackupIntegrityError(
                    "manifest database basename does not match path"
                )
            if (
                manifest.database_size != observation.size
                or manifest.database_digest != observation.digest
                or manifest.captured_recovery_epoch != observation.floor.recovery_epoch
                or manifest.captured_fencing_token_floor
                != observation.floor.fencing_token_floor
            ):
                raise BackupIntegrityError("manifest does not match SQLite image")
            if observation.database_identity != _identity(database_metadata):
                raise BackupFilesystemError(
                    "Store image identity does not match database"
                )
            manifest_after = self._assert_fd_path(
                root_fd,
                manifest_fd,
                manifest_name,
                label="backup manifest",
                expected_identity=_identity(manifest_metadata),
                expected_signature=_metadata_signature(manifest_metadata),
            )
            if manifest_after is None:
                raise BackupFilesystemError("backup manifest disappeared")
            manifest_signature = _metadata_signature(manifest_metadata)
            if _metadata_signature(manifest_after) != manifest_signature:
                raise BackupFilesystemError(
                    "backup manifest metadata changed while inspecting pair"
                )
            manifest_readback, manifest_readback_metadata = self._read_bounded_fd(
                manifest_fd,
                label="backup manifest",
                limit=MAX_MANIFEST_BYTES,
            )
            if (
                manifest_readback != raw_manifest
                or _metadata_signature(manifest_readback_metadata) != manifest_signature
                or "sha256:" + hashlib.sha256(manifest_readback).hexdigest()
                != "sha256:" + hashlib.sha256(raw_manifest).hexdigest()
            ):
                raise BackupIntegrityError(
                    "backup manifest changed while inspecting pair"
                )
            if _decode_manifest(manifest_readback) != manifest:
                raise BackupIntegrityError(
                    "backup manifest readback differs while inspecting pair"
                )
            manifest_final = self._assert_fd_path(
                root_fd,
                manifest_fd,
                manifest_name,
                label="backup manifest",
                expected_identity=_identity(manifest_metadata),
                expected_signature=_metadata_signature(manifest_metadata),
            )
            if manifest_final is None or _metadata_signature(manifest_final) != (
                manifest_signature
            ):
                raise BackupFilesystemError(
                    "backup manifest changed after final readback"
                )
            database_after_read = self._lstat(
                root_fd,
                database_name,
                label="backup database",
            )
            if database_after_read is None or _metadata_signature(
                database_after_read
            ) != _metadata_signature(database_metadata):
                raise BackupFilesystemError(
                    "backup database changed while inspecting pair"
                )
            self._check_sidecars(root_fd, database_name)
            artifact = BackupArtifact(
                database_basename=database_name,
                manifest_basename=manifest_name,
                manifest=manifest,
                database_identity=_identity(database_metadata),
                manifest_identity=_identity(manifest_metadata),
                workflow_row_counts=observation.workflow_row_counts,
            )
            return artifact, _PairPrecondition(
                database_signature=_metadata_signature(database_metadata),
                manifest_signature=_metadata_signature(manifest_metadata),
                manifest_digest="sha256:" + hashlib.sha256(raw_manifest).hexdigest(),
            )
        except _CLEANUP_EXCEPTION as error:
            body_error = error
            raise
        finally:
            try:
                _close_fds(
                    (
                        (manifest_fd, manifest_identity, "backup manifest"),
                        (database_fd, _identity(database_metadata), "backup database"),
                    ),
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                )
            except _CLEANUP_EXCEPTION as cleanup_error:
                primary_error = body_error if body_error is not None else cleanup_error
                if body_error is not None:
                    primary_error = self._adopt_cleanup_capability(
                        primary_error,
                        cleanup_error,
                    )
                wrapped = self._attach_cleanup_capability(primary_error)
                if body_error is None:
                    if wrapped is not primary_error:
                        raise wrapped from cleanup_error
                    raise
                if wrapped is not primary_error:
                    raise wrapped from body_error
                if primary_error is not body_error:
                    raise primary_error from body_error
                raise primary_error

    def _preflight_destination(
        self,
        root_fd: int,
        database_name: str,
    ) -> _PairPrecondition:
        _, manifest_name = self._validate_destination_names(database_name)
        self._check_sidecars(root_fd, database_name)
        database_metadata = self._lstat(root_fd, database_name, label="backup database")
        manifest_metadata = self._lstat(root_fd, manifest_name, label="backup manifest")
        if database_metadata is None and manifest_metadata is None:
            return _PairPrecondition(None, None)
        if database_metadata is None or manifest_metadata is None:
            raise BackupIncompleteError(
                "backup database and manifest must appear together"
            )
        artifact, pair_precondition = self._inspect_pair(root_fd, database_name)
        self._validate_regular(
            database_metadata,
            label="backup database",
            max_size=MAX_BACKUP_BYTES,
        )
        self._validate_regular(
            manifest_metadata,
            label="backup manifest",
            max_size=MAX_MANIFEST_BYTES,
        )
        return _PairPrecondition(
            database_signature=_metadata_signature(database_metadata),
            manifest_signature=_metadata_signature(manifest_metadata),
            database_digest=artifact.manifest.database_digest,
            manifest_digest=pair_precondition.manifest_digest,
        )

    def _assert_manifest_destination_precondition(
        self,
        root_fd: int,
        manifest_name: str,
        expected: _PairPrecondition,
    ) -> None:
        manifest_metadata = self._lstat(
            root_fd,
            manifest_name,
            label="backup manifest",
        )
        if expected.manifest_signature is None:
            if manifest_metadata is not None:
                raise BackupIncompleteError(
                    "backup manifest appeared after database publish"
                )
            return
        if manifest_metadata is None:
            raise BackupIncompleteError(
                "backup manifest disappeared after database publish"
            )
        if _metadata_signature(manifest_metadata) != expected.manifest_signature:
            raise BackupFilesystemError(
                "backup manifest changed after database publish"
            )
        manifest_fd, opened_metadata = self._open_existing_regular(
            root_fd,
            manifest_name,
            label="backup manifest",
            max_size=MAX_MANIFEST_BYTES,
        )
        body_error: BaseException | None = None
        try:
            raw_manifest, _ = self._read_bounded_fd(
                manifest_fd,
                label="backup manifest",
                limit=MAX_MANIFEST_BYTES,
            )
            manifest_digest = "sha256:" + hashlib.sha256(raw_manifest).hexdigest()
            if manifest_digest != expected.manifest_digest:
                raise BackupFilesystemError(
                    "backup manifest content changed after database publish"
                )
            manifest_after = self._assert_fd_path(
                root_fd,
                manifest_fd,
                manifest_name,
                label="backup manifest",
                expected_identity=_identity(opened_metadata),
                expected_signature=_metadata_signature(opened_metadata),
            )
            if manifest_after is None or _metadata_signature(manifest_after) != (
                _metadata_signature(opened_metadata)
            ):
                raise BackupFilesystemError(
                    "backup manifest changed after database publish"
                )
        except _CLEANUP_EXCEPTION as error:
            body_error = error
            raise
        finally:
            try:
                _close_fds(
                    (
                        (
                            manifest_fd,
                            _identity(opened_metadata),
                            "backup manifest precondition",
                        ),
                    ),
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                )
            except _CLEANUP_EXCEPTION as cleanup_error:
                primary_error = body_error if body_error is not None else cleanup_error
                if body_error is not None:
                    primary_error = self._adopt_cleanup_capability(
                        primary_error,
                        cleanup_error,
                    )
                wrapped = self._attach_cleanup_capability(primary_error)
                if body_error is None:
                    if wrapped is not primary_error:
                        raise wrapped from cleanup_error
                    raise
                if wrapped is not primary_error:
                    raise wrapped from body_error
                if primary_error is not body_error:
                    raise primary_error from body_error
                raise primary_error

    def _assert_destination_precondition(
        self,
        root_fd: int,
        database_name: str,
        expected: _PairPrecondition,
    ) -> None:
        _, manifest_name = self._validate_destination_names(database_name)
        database_metadata = self._lstat(root_fd, database_name, label="backup database")
        manifest_metadata = self._lstat(root_fd, manifest_name, label="backup manifest")
        if expected.database_signature is None or expected.manifest_signature is None:
            if database_metadata is not None or manifest_metadata is not None:
                raise BackupFilesystemError(
                    "backup destination appeared during publish"
                )
            return
        if database_metadata is None or manifest_metadata is None:
            raise BackupFilesystemError("backup destination changed during publish")
        if (
            _metadata_signature(database_metadata) != expected.database_signature
            or _metadata_signature(manifest_metadata) != expected.manifest_signature
        ):
            raise BackupFilesystemError("backup destination changed during publish")
        artifact, _ = self._inspect_pair(root_fd, database_name)
        if artifact.manifest.database_digest != expected.database_digest:
            raise BackupFilesystemError(
                "backup destination content changed during publish"
            )

    def _choose_temp_names(self, root_fd: int) -> tuple[str, str]:
        for _ in range(_MAX_TEMP_ATTEMPTS):
            nonce = self._next_nonce()
            if type(nonce) is not str:
                raise BackupFilesystemError("backup temp nonce is invalid")
            db_name = f".{nonce}.db.tmp"
            manifest_name = f".{nonce}.manifest.tmp"
            try:
                _require_basename(db_name, "database temp")
                _require_basename(manifest_name, "manifest temp")
            except ValueError as exc:
                raise BackupFilesystemError("backup temp nonce is invalid") from exc
            if (
                db_name in _RESERVED_NAMES
                or manifest_name in _RESERVED_NAMES
                or self._lstat(root_fd, db_name, label="database temp") is not None
                or self._lstat(root_fd, manifest_name, label="manifest temp")
                is not None
            ):
                continue
            return db_name, manifest_name
        raise BackupFilesystemError("unique backup temp names are unavailable")

    def _fsync(self, fd: int, label: str) -> None:
        try:
            os.fsync(fd)
        except OSError as exc:
            raise BackupDurabilityUnknownError(
                f"{label} durability is unknown"
            ) from exc

    def create(self, destination_name: str) -> BackupArtifact:
        """Create or replace one verified DB/manifest pair."""

        database_name, _ = self._validate_destination_names(destination_name)
        self._retry_retained_resources()
        root_fd, root_identity, root_signature = self._open_root()
        database_fd: int | None = None
        manifest_fd: int | None = None
        db_temp_name: str | None = None
        manifest_temp_name: str | None = None
        database_temp_identity: tuple[int, int] | None = None
        manifest_temp_identity: tuple[int, int] | None = None
        database_metadata: os.stat_result | None = None
        manifest_metadata: os.stat_result | None = None
        db_replaced = False
        manifest_replaced = False
        body_error: BaseException | None = None
        error: BackupError
        try:
            precondition = self._preflight_destination(root_fd, database_name)
            self._assert_root(root_fd, root_identity, root_signature)
            db_temp_name, manifest_temp_name = self._choose_temp_names(root_fd)
            controller = self._new_controller()
            session = self._hold_quiescence(controller)
            with self._session_lifecycle(session) as retained_session:
                copy_result = retained_session.copy_database_to(
                    CheckpointRequest("TRUNCATE"),
                    DatabaseCopyTarget(db_temp_name),
                )
            if type(copy_result) is not DatabaseCopyResult:
                raise BackupIntegrationError("WAL copy returned an unexpected result")
            if copy_result.target.name != db_temp_name:
                raise BackupIntegrityError(
                    "WAL copy target does not match planned temp"
                )
            if (
                copy_result.checkpoint.request.mode != "TRUNCATE"
                or not copy_result.checkpoint.safe
            ):
                raise BackupIntegrityError("WAL copy checkpoint is not safe TRUNCATE")
            database_temp_identity = copy_result.target_identity
            self._assert_root(root_fd, root_identity, root_signature)
            database_fd, database_metadata = self._open_existing_regular(
                root_fd,
                db_temp_name,
                label="database temp",
                max_size=MAX_BACKUP_BYTES,
            )
            database_temp_identity = _identity(database_metadata)
            self._check_sidecars(root_fd, db_temp_name)
            observation = self._store_image(database_fd)
            self._assert_fd_path(
                root_fd,
                database_fd,
                db_temp_name,
                label="database temp",
                expected_identity=_identity(database_metadata),
                expected_signature=_metadata_signature(database_metadata),
            )
            if (
                observation.database_identity != _identity(database_metadata)
                or copy_result.target_identity != _identity(database_metadata)
                or observation.size != database_metadata.st_size
                or observation.size != copy_result.size
                or observation.digest != copy_result.digest
            ):
                raise BackupIntegrityError(
                    "WAL copy observation does not match Store image"
                )
            manifest = BackupManifest(
                version=BACKUP_MANIFEST_VERSION,
                database_basename=database_name,
                store_schema=_store.STORE_SCHEMA,
                event_schema_version=_store.EVENT_SCHEMA_VERSION,
                sqlite_user_version=_store.STORE_SCHEMA,
                integrity_check="ok",
                database_size=observation.size,
                database_digest=observation.digest,
                captured_recovery_epoch=observation.floor.recovery_epoch,
                captured_fencing_token_floor=observation.floor.fencing_token_floor,
            )
            manifest_bytes = _encode_manifest(manifest)
            self._fault("before_manifest_temp_create")
            manifest_fd, manifest_metadata = self._create_manifest_temp(
                root_fd,
                manifest_temp_name,
            )
            manifest_temp_identity = _identity(manifest_metadata)
            self._fault("after_manifest_temp_create")
            self._fault("before_manifest_write")
            self._write_all(manifest_fd, manifest_bytes, label="manifest temp")
            self._fault("after_manifest_write")
            self._fault("before_manifest_fsync")
            self._fsync(manifest_fd, "manifest temp")
            self._fault("after_manifest_fsync")
            readback, _ = self._read_bounded_fd(
                manifest_fd,
                label="manifest temp",
                limit=MAX_MANIFEST_BYTES,
            )
            if _decode_manifest(readback) != manifest:
                raise BackupIntegrityError("manifest temp readback differs")
            self._fault("before_publish_recheck")
            self._fault("before_db_replace")
            self._assert_root(root_fd, root_identity, root_signature)
            self._assert_fd_path(
                root_fd,
                database_fd,
                db_temp_name,
                label="database temp",
                expected_identity=_identity(database_metadata),
                expected_signature=_metadata_signature(database_metadata),
            )
            self._check_sidecars(root_fd, db_temp_name)
            if self._store_image(database_fd) != observation:
                raise BackupIntegrityError("database temp changed before publish")
            self._assert_fd_path(
                root_fd,
                manifest_fd,
                manifest_temp_name,
                label="manifest temp",
                expected_identity=_identity(manifest_metadata),
                expected_signature=_metadata_signature(manifest_metadata),
            )
            manifest_readback, _ = self._read_bounded_fd(
                manifest_fd,
                label="manifest temp",
                limit=MAX_MANIFEST_BYTES,
            )
            if _decode_manifest(manifest_readback) != manifest:
                raise BackupIntegrityError("manifest temp changed before publish")
            self._assert_fd_path(
                root_fd,
                database_fd,
                db_temp_name,
                label="database temp",
                expected_identity=_identity(database_metadata),
                expected_signature=_metadata_signature(database_metadata),
            )
            if self._store_image(database_fd) != observation:
                raise BackupIntegrityError("database temp changed before replace")
            self._check_sidecars(root_fd, db_temp_name)
            self._assert_fd_path(
                root_fd,
                manifest_fd,
                manifest_temp_name,
                label="manifest temp",
                expected_identity=_identity(manifest_metadata),
                expected_signature=_metadata_signature(manifest_metadata),
            )
            self._assert_destination_precondition(root_fd, database_name, precondition)
            self._check_sidecars(root_fd, database_name)
            self._assert_root(root_fd, root_identity, root_signature)
            db_replaced = True
            os.replace(
                db_temp_name,
                database_name,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            self._fault("after_db_replace")
            self._assert_manifest_destination_precondition(
                root_fd,
                f"{database_name}{_MANIFEST_SUFFIX}",
                precondition,
            )
            self._fault("before_manifest_replace")
            self._assert_fd_path(
                root_fd,
                database_fd,
                database_name,
                label="published database",
                expected_identity=_identity(database_metadata),
                expected_signature=_metadata_signature(database_metadata),
            )
            if self._store_image(database_fd) != observation:
                raise BackupIntegrityError(
                    "published database changed before manifest publish"
                )
            self._check_sidecars(root_fd, database_name)
            self._assert_fd_path(
                root_fd,
                manifest_fd,
                manifest_temp_name,
                label="manifest temp",
                expected_identity=_identity(manifest_metadata),
                expected_signature=_metadata_signature(manifest_metadata),
            )
            manifest_readback, _ = self._read_bounded_fd(
                manifest_fd,
                label="manifest temp",
                limit=MAX_MANIFEST_BYTES,
            )
            if _decode_manifest(manifest_readback) != manifest:
                raise BackupIntegrityError("manifest temp changed before replace")
            self._assert_fd_path(
                root_fd,
                database_fd,
                database_name,
                label="published database",
                expected_identity=_identity(database_metadata),
                expected_signature=_metadata_signature(database_metadata),
            )
            if self._store_image(database_fd) != observation:
                raise BackupIntegrityError(
                    "published database changed before manifest replace"
                )
            self._check_sidecars(root_fd, database_name)
            self._assert_fd_path(
                root_fd,
                manifest_fd,
                manifest_temp_name,
                label="manifest temp",
                expected_identity=_identity(manifest_metadata),
                expected_signature=_metadata_signature(manifest_metadata),
            )
            self._check_sidecars(root_fd, database_name)
            self._assert_root(root_fd, root_identity, root_signature)
            self._assert_manifest_destination_precondition(
                root_fd,
                f"{database_name}{_MANIFEST_SUFFIX}",
                precondition,
            )
            self._assert_root(root_fd, root_identity, root_signature)
            manifest_replaced = True
            os.replace(
                manifest_temp_name,
                f"{database_name}{_MANIFEST_SUFFIX}",
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            self._fault("after_manifest_replace")
            self._assert_fd_path(
                root_fd,
                manifest_fd,
                f"{database_name}{_MANIFEST_SUFFIX}",
                label="published manifest",
                expected_identity=_identity(manifest_metadata),
                expected_signature=_metadata_signature(manifest_metadata),
            )
            self._fault("before_directory_fsync")
            self._fsync(root_fd, "backup root directory")
            self._fault("after_directory_fsync")
            self._fault("before_final_inspect")
            result, final_precondition = self._inspect_pair(root_fd, database_name)
            if (
                result.database_basename != database_name
                or result.manifest_basename != f"{database_name}{_MANIFEST_SUFFIX}"
                or result.manifest != manifest
                or result.database_identity != _identity(database_metadata)
                or result.manifest_identity != _identity(manifest_metadata)
                or final_precondition.manifest_digest
                != "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
                or result.manifest.database_size != observation.size
                or result.manifest.database_digest != observation.digest
                or result.manifest.captured_recovery_epoch
                != observation.floor.recovery_epoch
                or result.manifest.captured_fencing_token_floor
                != observation.floor.fencing_token_floor
                or result.workflow_row_counts != observation.workflow_row_counts
            ):
                raise BackupIntegrityError(
                    "final backup inspect does not match its publication"
                )
            self._fault("after_final_inspect")
            return result
        except FileNotFoundError as exc:
            if db_replaced or manifest_replaced:
                error = BackupDurabilityUnknownError(
                    "backup publication state is unknown"
                )
            else:
                error = BackupFilesystemError("backup publication file is missing")
            body_error = error
            self._attach_cleanup_capability(error)
            raise error from exc
        except OSError as exc:
            if db_replaced or manifest_replaced:
                error = BackupDurabilityUnknownError(
                    "backup publication state is unknown"
                )
            else:
                error = BackupFilesystemError("backup publication failed")
            body_error = error
            self._attach_cleanup_capability(error)
            raise error from exc
        except _CLEANUP_EXCEPTION as exc:
            body_error = exc
            self._attach_cleanup_capability(exc)
            raise
        finally:
            try:
                _close_fds(
                    (
                        (manifest_fd, manifest_temp_identity, "manifest temp"),
                        (database_fd, database_temp_identity, "database temp"),
                        (root_fd, root_identity, "backup root"),
                    ),
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                )
            except _CLEANUP_EXCEPTION as cleanup_error:
                primary_error = body_error if body_error is not None else cleanup_error
                if body_error is not None:
                    primary_error = self._adopt_cleanup_capability(
                        primary_error,
                        cleanup_error,
                    )
                wrapped = self._attach_cleanup_capability(primary_error)
                if body_error is None:
                    if wrapped is not primary_error:
                        raise wrapped from cleanup_error
                    raise
                if wrapped is not primary_error:
                    raise wrapped from body_error
                if primary_error is not body_error:
                    raise primary_error from body_error
                raise primary_error

    def inspect(self, destination_name: str) -> BackupArtifact:
        """Read one complete pair without creating or mutating state."""

        database_name, _ = self._validate_destination_names(destination_name)
        self._retry_retained_resources()
        root_fd, root_identity, root_signature = self._open_root()
        body_error: BaseException | None = None
        try:
            self._assert_root(root_fd, root_identity, root_signature)
            result, _ = self._inspect_pair(root_fd, database_name)
            self._assert_root(root_fd, root_identity, root_signature)
            return result
        except _CLEANUP_EXCEPTION as error:
            body_error = error
            raise
        finally:
            try:
                _close_fds(
                    ((root_fd, root_identity, "backup root"),),
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                )
            except _CLEANUP_EXCEPTION as cleanup_error:
                primary_error = body_error if body_error is not None else cleanup_error
                if body_error is not None:
                    primary_error = self._adopt_cleanup_capability(
                        primary_error,
                        cleanup_error,
                    )
                wrapped = self._attach_cleanup_capability(primary_error)
                if body_error is None:
                    if wrapped is not primary_error:
                        raise wrapped from cleanup_error
                    raise
                if wrapped is not primary_error:
                    raise wrapped from body_error
                if primary_error is not body_error:
                    raise primary_error from body_error
                raise primary_error


__all__ = [
    "BACKUP_MANIFEST_FIELDS",
    "BACKUP_MANIFEST_VERSION",
    "MAX_BACKUP_BYTES",
    "MAX_MANIFEST_BYTES",
    "BackupArtifact",
    "BackupDurabilityUnknownError",
    "BackupError",
    "BackupFilesystemError",
    "BackupIncompleteError",
    "BackupIntegrationError",
    "BackupIntegrityError",
    "BackupManifest",
    "SQLiteBackup",
]
