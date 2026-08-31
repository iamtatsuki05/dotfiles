"""Candidate-first restoration of one verified coordination-store image.

This module is intentionally an orchestration layer.  SQLite policy belongs to
``RestoreStoreAuthority``, path replacement belongs to the WAL controller, and
the durable restore protocol belongs to ``RestoreLedger``.  No provider or
normal ``CoordinationStore`` is opened here.
"""

from __future__ import annotations

import errno
import hashlib
import os
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Self, cast

from . import recovery as _recovery
from . import store as _store
from .backup import BackupArtifact, BackupError, SQLiteBackup
from .lease import (
    RecoveryFloor,
    RestoreApplyResult,
    RestoreCandidateEvidence,
    RestoreIdentity,
    RestoreReplacedEvidence,
    StoreImageObservation,
)
from .recovery import (
    NormalOpenRecoveryState,
    RestoreHandle,
    RestoreLedger,
    RestoreTombstoneOrphan,
)
from .wal import (
    _CLEANUP_EXCEPTION,
    CheckpointRequest,
    DatabaseCandidate,
    QuiescenceOwner,
    QuiescenceSession,
    WalSidecarController,
    WalSidecarError,
    _close_temporary_fd,
)

RestorePhase = Literal[
    "RESTORE_PREPARED",
    "RESTORE_REPLACED",
    "RESTORE_COMMITTED",
    "RESTORE_ABORTED",
]

_CANDIDATE_PREFIX: Final[str] = ".coordination.sqlite3.restore-"
_PRIMARY_NAME: Final[str] = _store.DATABASE_FILENAME
_MAX_DIGEST_LENGTH: Final[int] = 71
_OrphanFD = tuple[int, tuple[int, int] | None, str]
_MAX_ORPHAN_CONTROLLERS: Final[int] = 4
_MAX_ORPHAN_FDS: Final[int] = 16
_MAX_ORPHAN_SESSIONS: Final[int] = 4


class RestoreError(_store.StoreError):
    """Base class for orchestration-level restore failures."""


class RestorePendingError(RestoreError):
    """A new restore was requested while another generation is pending."""


class RestoreReviewRequiredError(RestoreError):
    """The durable filesystem state cannot be classified safely."""


class RestoreFilesystemError(RestoreError):
    """A restore-owned descriptor or candidate filesystem operation failed."""


class RestoreDurabilityError(RestoreError):
    """A candidate/root fsync did not return durable success."""


def _with_cleanup_owner(
    error: BaseException,
    callback: Callable[[], None],
) -> BaseException:
    capability = _store._CleanupCapability(callback)
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
    wrapper = RestoreFilesystemError("restore cleanup status is unknown")
    wrapper.__cause__ = error
    _store._attach_cleanup_capability(wrapper, capability)
    return wrapper


def _adopt_cleanup_capability(
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
    return _with_cleanup_owner(error, capability.retry_cleanup)


def _restore_error_with_owner(
    error: BaseException,
    message: str,
) -> BaseException:
    if isinstance(error, RestoreError):
        return error
    try:
        capability = _store._extract_cleanup_capability(error)
    except _CLEANUP_EXCEPTION:
        capability = None
    if capability is None:
        return error
    wrapper = RestoreFilesystemError(message)
    wrapper.__cause__ = error
    return _with_cleanup_owner(wrapper, capability.retry_cleanup)


def _digest(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != _MAX_DIGEST_LENGTH
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _identifier(value: object, name: str) -> str:
    try:
        return _store._require_opaque_identifier(value, name)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is invalid") from exc


def _evidence_ref(audit_ref: str) -> str:
    """Derive the stable event evidence reference required by the Store seam."""

    return "sha256:" + hashlib.sha256(audit_ref.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Public result containing no path, descriptor, or per-operation token."""

    phase: RestorePhase
    restore_generation: int
    backup_digest: str
    candidate_digest: str
    floor: RecoveryFloor
    identities: tuple[RestoreIdentity, ...]
    active_tombstones: tuple[RestoreIdentity, ...]

    def __post_init__(self) -> None:
        if type(self.phase) is not str or self.phase not in {
            "RESTORE_PREPARED",
            "RESTORE_REPLACED",
            "RESTORE_COMMITTED",
            "RESTORE_ABORTED",
        }:
            raise ValueError("restore phase is unsupported")
        if type(self.restore_generation) is not int or self.restore_generation < 1:
            raise ValueError("restore generation is invalid")
        _digest(self.backup_digest, "backup_digest")
        _digest(self.candidate_digest, "candidate_digest")
        if type(self.floor) is not RecoveryFloor:
            raise TypeError("restore floor is invalid")
        if type(self.identities) is not tuple or any(
            type(identity) is not RestoreIdentity for identity in self.identities
        ):
            raise TypeError("restore identities are invalid")
        identity_keys = tuple(
            (identity.operation_id, identity.effect_key) for identity in self.identities
        )
        if identity_keys != tuple(sorted(set(identity_keys))):
            raise ValueError("restore identities are not sorted and unique")
        if type(self.active_tombstones) is not tuple or any(
            type(identity) is not RestoreIdentity for identity in self.active_tombstones
        ):
            raise TypeError("restore active tombstones are invalid")
        active_keys = tuple(
            (identity.operation_id, identity.effect_key)
            for identity in self.active_tombstones
        )
        if active_keys != tuple(sorted(set(active_keys))):
            raise ValueError("restore active tombstones are not sorted and unique")
        if self.phase != "RESTORE_ABORTED" and any(
            identity not in self.active_tombstones for identity in self.identities
        ):
            raise ValueError("restore identities are not active")


def _candidate_basename(artifact: BackupArtifact) -> str:
    if type(artifact) is not BackupArtifact:
        raise TypeError("backup artifact is invalid")
    digest = _digest(artifact.manifest.database_digest, "database_digest")
    name = _CANDIDATE_PREFIX + digest.removeprefix("sha256:")
    try:
        from .wal import DatabaseCopyTarget

        DatabaseCopyTarget(name)
    except (TypeError, ValueError) as exc:
        raise RestoreFilesystemError("restore candidate basename is invalid") from exc
    if name in {
        artifact.database_basename,
        artifact.manifest_basename,
        _PRIMARY_NAME,
        _store.WRITER_MARKER_FILENAME,
        _store.LIFETIME_GATE_FILENAME,
        "recovery.ledger",
        "recovery.tombstones",
    }:
        raise RestoreFilesystemError("restore candidate basename collides with state")
    return name


def _floor_from_handle(handle: RestoreHandle) -> RecoveryFloor:
    return RecoveryFloor(handle.recovery_epoch, handle.fencing_token_floor)


def _identities_equal(
    left: tuple[RestoreIdentity, ...], right: tuple[RestoreIdentity, ...]
) -> bool:
    return tuple(
        (identity.operation_id, identity.effect_key) for identity in left
    ) == tuple((identity.operation_id, identity.effect_key) for identity in right)


def _canonical_restore_identities(
    value: object,
    *,
    label: str,
) -> tuple[RestoreIdentity, ...]:
    if type(value) is not tuple or any(
        type(identity) is not RestoreIdentity for identity in value
    ):
        raise RestoreReviewRequiredError(f"{label} are invalid")
    keys = tuple((identity.operation_id, identity.effect_key) for identity in value)
    if keys != tuple(sorted(set(keys))):
        raise RestoreReviewRequiredError(f"{label} are not canonical")
    return value


def _canonical_active_tombstones(
    value: object,
    *,
    label: str = "restore active tombstones",
) -> tuple[RestoreIdentity, ...]:
    if type(value) is frozenset:
        try:
            value = tuple(
                RestoreIdentity(operation_id=operation_id, effect_key=effect_key)
                for operation_id, effect_key in sorted(value)
            )
        except (TypeError, ValueError) as exc:
            raise RestoreReviewRequiredError(f"{label} are invalid") from exc
    return _canonical_restore_identities(value, label=label)


def _active_tombstones_from_keys(
    value: object,
    *,
    label: str,
) -> tuple[RestoreIdentity, ...]:
    if type(value) is not frozenset:
        raise RestoreReviewRequiredError(f"{label} are invalid")
    try:
        identities = tuple(
            RestoreIdentity(operation_id=operation_id, effect_key=effect_key)
            for operation_id, effect_key in sorted(value)
        )
    except (TypeError, ValueError) as exc:
        raise RestoreReviewRequiredError(f"{label} are invalid") from exc
    return _canonical_restore_identities(identities, label=label)


def _merge_active_tombstones(
    *groups: tuple[RestoreIdentity, ...],
) -> tuple[RestoreIdentity, ...]:
    merged: set[RestoreIdentity] = set()
    for group in groups:
        merged.update(_canonical_restore_identities(group, label="restore tombstones"))
    return tuple(
        sorted(
            merged,
            key=lambda identity: (identity.operation_id, identity.effect_key),
        )
    )


def _active_tombstones_for_handle(
    previous_active_tombstones: tuple[RestoreIdentity, ...],
    handle: RestoreHandle,
) -> tuple[RestoreIdentity, ...]:
    previous = _canonical_restore_identities(
        previous_active_tombstones,
        label="restore previous active tombstones",
    )
    current = _canonical_restore_identities(
        handle.identities,
        label="restore handle tombstones",
    )
    if handle.phase in {"RESTORE_PREPARED", "RESTORE_REPLACED"} and (
        handle.tombstone_phase != "ABORTED"
    ):
        return _merge_active_tombstones(previous, current)
    return previous


def _active_tombstones_for_orphan(
    orphan: RestoreTombstoneOrphan,
) -> tuple[RestoreIdentity, ...]:
    previous = _active_tombstones_from_keys(
        orphan.active_identities,
        label="restore orphan active tombstones",
    )
    return _merge_active_tombstones(previous, orphan.tombstone.identities)


def _assert_apply_matches_handle(
    result: RestoreApplyResult,
    handle: RestoreHandle,
    active_tombstones: tuple[RestoreIdentity, ...],
) -> None:
    if (
        result.digest != handle.candidate_digest
        or result.floor != _floor_from_handle(handle)
        or not _identities_equal(result.tombstones, handle.identities)
        or not _identities_equal(result.active_tombstones, active_tombstones)
    ):
        raise RestoreReviewRequiredError("restore candidate evidence mismatches ledger")


def _assert_previous_primary_observation(
    observation: StoreImageObservation,
    *,
    expected_digest: str,
    expected_recovery_epoch: int,
    expected_fencing_token_hwm: int,
    expected_last_clock_ns: int,
) -> None:
    observed_fencing_token_hwm = max(
        observation.floor.fencing_token_floor,
        observation.max_fencing_token,
    )
    if (
        observation.digest != expected_digest
        or observation.floor.recovery_epoch != expected_recovery_epoch
        or observed_fencing_token_hwm != expected_fencing_token_hwm
        or observation.last_clock_ns != expected_last_clock_ns
    ):
        raise RestoreReviewRequiredError(
            "restore primary does not match durable previous evidence"
        )


def _assert_apply_results_match(
    expected: RestoreApplyResult,
    actual: RestoreApplyResult,
) -> None:
    expected_observation = expected.observation
    actual_observation = actual.observation
    if (
        expected.digest != actual.digest
        or expected.size != actual.size
        or expected.floor != actual.floor
        or expected.restore_event_count != actual.restore_event_count
        or not _identities_equal(expected.tombstones, actual.tombstones)
        or not _identities_equal(
            expected.active_tombstones,
            actual.active_tombstones,
        )
        or expected_observation.max_fencing_token
        != actual_observation.max_fencing_token
        or expected_observation.last_clock_ns != actual_observation.last_clock_ns
        or expected_observation.operations != actual_observation.operations
        or not _identities_equal(
            expected_observation.identities,
            actual_observation.identities,
        )
    ):
        raise RestoreReviewRequiredError("restore image evidence changed")


def _assert_source_artifact(
    artifact: BackupArtifact,
    observation: StoreImageObservation,
) -> None:
    manifest = artifact.manifest
    if (
        observation.database_identity != artifact.database_identity
        or observation.size != manifest.database_size
        or observation.digest != manifest.database_digest
        or observation.floor.recovery_epoch != manifest.captured_recovery_epoch
        or observation.floor.fencing_token_floor
        != manifest.captured_fencing_token_floor
    ):
        raise RestoreReviewRequiredError("backup source image changed")


def _assert_source_not_tombstoned(
    observation: StoreImageObservation,
    active_identities: frozenset[tuple[str, str]],
) -> None:
    active_operations = {operation_id for operation_id, _ in active_identities}
    active_effects = {effect_key for _, effect_key in active_identities}
    if any(
        identity.operation_id in active_operations
        or identity.effect_key in active_effects
        for identity in observation.identities
    ):
        raise RestoreReviewRequiredError(
            "restore source contains a committed tombstone identity"
        )


def _fsync(fd: int, label: str) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise RestoreDurabilityError(f"{label} durability is unknown") from exc


def _entry_exists(root_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RestoreFilesystemError(
            f"restore entry {name} cannot be inspected"
        ) from exc
    return True


def _assert_fd_path(
    root_fd: int,
    name: str,
    fd: int,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> os.stat_result:
    try:
        metadata = os.fstat(fd)
        path_metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        _store._validate_private_file(metadata, sidecar=False)
        _store._validate_private_file(path_metadata, sidecar=False)
    except _store.StoreError as exc:
        raise RestoreFilesystemError(f"restore entry {name} is unsafe") from exc
    except OSError as exc:
        raise RestoreFilesystemError(
            f"restore entry {name} identity cannot be inspected"
        ) from exc
    except _CLEANUP_EXCEPTION as exc:
        raise RestoreFilesystemError(
            f"restore entry {name} identity status is unknown"
        ) from exc
    identity = _store._identity(metadata)
    if identity != _store._identity(path_metadata) or (
        expected_identity is not None and identity != expected_identity
    ):
        raise RestoreFilesystemError(f"restore entry {name} identity changed")
    return metadata


def _remember_orphan_fd(
    registry: list[_OrphanFD],
    fd: int,
    expected_identity: tuple[int, int] | None,
    label: str,
    *,
    cleanup_callback: Callable[[], None] | None = None,
) -> None:
    identity_mismatch = False
    try:
        metadata = os.fstat(fd)
    except _CLEANUP_EXCEPTION as exc:
        if isinstance(exc, OSError) and exc.errno == errno.EBADF:
            return
        actual_identity = expected_identity
    else:
        actual_identity = _store._identity(metadata)
        if expected_identity is not None and actual_identity != expected_identity:
            identity_mismatch = True
            actual_identity = expected_identity
    item = (fd, actual_identity, label)
    for existing_fd, existing_identity, _ in registry:
        if existing_fd != fd:
            continue
        if existing_identity == actual_identity:
            if identity_mismatch:
                error = RestoreReviewRequiredError(f"{label} descriptor was reused")
                if cleanup_callback is not None:
                    _store._attach_cleanup_capability(
                        error,
                        _store._CleanupCapability(cleanup_callback),
                    )
                raise error
            return
        error = RestoreReviewRequiredError(f"{label} descriptor was reused")
        if cleanup_callback is not None:
            _store._attach_cleanup_capability(
                error,
                _store._CleanupCapability(cleanup_callback),
            )
        raise error
    if len(registry) >= _MAX_ORPHAN_FDS:
        overflow_error = RestoreFilesystemError(
            "restore descriptor retry registry is full"
        )
        if cleanup_callback is not None:
            _store._attach_cleanup_capability(
                overflow_error,
                _store._CleanupCapability(cleanup_callback),
            )
        raise overflow_error
    registry.append(item)
    if identity_mismatch:
        error = RestoreReviewRequiredError(f"{label} descriptor was reused")
        if cleanup_callback is not None:
            _store._attach_cleanup_capability(
                error,
                _store._CleanupCapability(cleanup_callback),
            )
        raise error


@contextmanager
def _owned_fd(
    root_fd: int,
    name: str,
    *,
    writable: bool,
    create: bool = False,
    expected_identity: tuple[int, int] | None = None,
    orphan_registry: list[_OrphanFD] | None = None,
    cleanup_callback: Callable[[], None] | None = None,
) -> Iterator[tuple[int, os.stat_result]]:
    try:
        nonblock = os.O_NONBLOCK
    except AttributeError as exc:
        raise RestoreFilesystemError(
            "non-blocking restore open is unavailable"
        ) from exc
    try:
        flags = _store._open_flags(directory=False, writable=writable) | nonblock
    except _store.StoreError as exc:
        error = RestoreFilesystemError("restore open flags are unavailable")
        adopted = _adopt_cleanup_capability(error, exc)
        if adopted is not error:
            raise adopted from exc
        raise error from exc
    except _CLEANUP_EXCEPTION as exc:
        raise RestoreFilesystemError("restore open flags status is unknown") from exc
    fd: int | None = None
    identity: tuple[int, int] | None = expected_identity
    body_error: BaseException | None = None
    try:
        try:
            if create:
                try:
                    os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError(name)
                flags |= os.O_CREAT | os.O_EXCL
                fd = os.open(name, flags, 0o600, dir_fd=root_fd)
            else:
                path_before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                _store._validate_private_file(path_before, sidecar=False)
                fd = os.open(name, flags, dir_fd=root_fd)
            path_metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            identity = _store._identity(path_metadata)
            _store._validate_private_file(path_metadata, sidecar=False)
            metadata = os.fstat(fd)
            _store._validate_private_file(metadata, sidecar=False)
            identity = _store._identity(metadata)
            if identity != _store._identity(path_metadata) or (
                expected_identity is not None and identity != expected_identity
            ):
                raise RestoreFilesystemError(
                    f"restore entry {name} changed while opening"
                )
        except RestoreError:
            raise
        except _store.StoreError as exc:
            error = RestoreFilesystemError(f"restore entry {name} is unsafe")
            adopted = _adopt_cleanup_capability(error, exc)
            if adopted is not error:
                raise adopted from exc
            raise error from exc
        except FileExistsError as exc:
            raise RestoreFilesystemError(
                f"restore entry {name} already exists"
            ) from exc
        except FileNotFoundError as exc:
            raise RestoreFilesystemError(f"restore entry {name} is missing") from exc
        except OSError as exc:
            raise RestoreFilesystemError(
                f"restore entry {name} cannot be opened"
            ) from exc
        except _CLEANUP_EXCEPTION as exc:
            raise RestoreFilesystemError(
                f"restore entry {name} status is unknown"
            ) from exc
        assert fd is not None
        try:
            yield fd, metadata
        except _CLEANUP_EXCEPTION as error:
            body_error = error
            raise
    except _CLEANUP_EXCEPTION as error:
        body_error = error
        raise
    finally:
        if fd is not None:
            try:
                _close_temporary_fd(fd, identity, f"restore entry {name}")
            except _CLEANUP_EXCEPTION as cleanup_error:
                retention_error: BaseException | None = None
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
                                raise RestoreReviewRequiredError(
                                    f"restore entry {name} descriptor status is unknown"
                                ) from status_error
                            if identity is None:
                                raise RestoreReviewRequiredError(
                                    f"restore entry {name} descriptor identity is unavailable"
                                )
                            if _store._identity(metadata) != identity:
                                raise RestoreReviewRequiredError(
                                    f"restore entry {name} descriptor was reused"
                                )
                            _close_temporary_fd(fd, identity, f"restore entry {name}")
                            cleanup_callback()

                        overflow_callback = retry_current_fd
                    try:
                        _remember_orphan_fd(
                            orphan_registry,
                            fd,
                            identity,
                            f"restore entry {name}",
                            cleanup_callback=overflow_callback,
                        )
                    except _CLEANUP_EXCEPTION as error:
                        retention_error = error
                cleanup_target = (
                    retention_error if retention_error is not None else cleanup_error
                )
                primary = body_error if body_error is not None else cleanup_target
                if body_error is None:
                    primary = _restore_error_with_owner(
                        primary,
                        "restore cleanup status is unknown",
                    )
                else:
                    primary = _adopt_cleanup_capability(primary, cleanup_error)
                if retention_error is not None and retention_error is not primary:
                    primary = _adopt_cleanup_capability(primary, retention_error)
                if cleanup_error is not primary:
                    primary = _adopt_cleanup_capability(primary, cleanup_error)
                wrapped = (
                    _with_cleanup_owner(primary, cleanup_callback)
                    if cleanup_callback is not None
                    else primary
                )
                if body_error is None:
                    if wrapped is not cleanup_target:
                        raise wrapped from cleanup_target
                    if primary is not cleanup_target:
                        raise primary from cleanup_target
                    raise primary
                if wrapped is not primary:
                    raise wrapped from body_error
                if primary is not body_error:
                    raise primary from body_error
                raise primary


@contextmanager
def _owned_fd_with_fault(
    root_fd: int,
    name: str,
    *,
    writable: bool,
    create: bool = False,
    fault: Callable[[str], None],
    before_point: str,
    after_point: str,
    expected_identity: tuple[int, int] | None = None,
    orphan_registry: list[_OrphanFD] | None = None,
    cleanup_callback: Callable[[], None] | None = None,
) -> Iterator[tuple[int, os.stat_result]]:
    fault(before_point)
    with _owned_fd(
        root_fd,
        name,
        writable=writable,
        create=create,
        expected_identity=expected_identity,
        orphan_registry=orphan_registry,
        cleanup_callback=cleanup_callback,
    ) as value:
        fault(after_point)
        _assert_fd_path(
            root_fd,
            name,
            value[0],
            expected_identity=expected_identity,
        )
        yield value


class BackupRestore:
    """Coordinate one candidate-first restore under one quiescence session."""

    __slots__ = (
        "_backup_helper",
        "_busy_timeout_ms",
        "_clock",
        "_fault_callback",
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
        clock: Callable[[], int] = time.time_ns,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        if (
            type(busy_timeout_ms) is not int
            or not 1 <= busy_timeout_ms <= _store.MAX_BUSY_TIMEOUT_MS
        ):
            raise ValueError(
                f"busy_timeout_ms must be between 1 and {_store.MAX_BUSY_TIMEOUT_MS}"
            )
        if not callable(clock):
            raise TypeError("restore clock must be callable")
        if fault is not None and not callable(fault):
            raise TypeError("restore fault hook must be callable")
        self._state_root = _store._coerce_state_root(state_root)
        self._busy_timeout_ms = busy_timeout_ms
        self._clock = clock
        self._fault_callback = fault
        self._backup_helper = SQLiteBackup(
            self._state_root,
            busy_timeout_ms=self._busy_timeout_ms,
        )
        self._orphan_controllers: list[WalSidecarController] = []
        self._orphan_fds: list[_OrphanFD] = []
        self._orphan_sessions: list[QuiescenceSession] = []

    def _fault(self, point: str) -> None:
        callback = self._fault_callback
        if callback is not None:
            callback(point)

    def _attach_cleanup_capability(
        self,
        error: BaseException,
        callback: Callable[[], None] | None = None,
    ) -> BaseException:
        retry = self.close if callback is None else callback
        return _with_cleanup_owner(error, retry)

    def _retry_orphan_fds(self) -> None:
        remaining: list[_OrphanFD] = []
        first_error: BaseException | None = None
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
                    first_error = RestoreReviewRequiredError(
                        f"{label} descriptor status is unknown"
                    )
                continue
            if expected_identity is None:
                remaining.append((fd, None, label))
                if first_error is None:
                    first_error = RestoreReviewRequiredError(
                        f"{label} descriptor identity is unavailable"
                    )
                continue
            if _store._identity(metadata) != expected_identity:
                remaining.append((fd, expected_identity, label))
                if first_error is None:
                    first_error = RestoreReviewRequiredError(
                        f"{label} descriptor was reused"
                    )
                continue
            try:
                _close_temporary_fd(fd, expected_identity, label)
            except _CLEANUP_EXCEPTION as error:
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
                        first_error = RestoreReviewRequiredError(
                            f"{label} descriptor status is unknown"
                        )
                    continue
                if _store._identity(retry_metadata) != expected_identity:
                    remaining.append((fd, expected_identity, label))
                    if first_error is None:
                        first_error = RestoreReviewRequiredError(
                            f"{label} descriptor was reused"
                        )
                    continue
                remaining.append((fd, expected_identity, label))
                if first_error is None:
                    first_error = error
        self._orphan_fds = remaining
        if first_error is not None:
            primary: BaseException = RestoreFilesystemError(
                "restore-owned descriptors cannot be closed"
            )
            primary.__cause__ = first_error
            primary = _adopt_cleanup_capability(primary, first_error)
            wrapped = self._attach_cleanup_capability(primary)
            if wrapped is not primary:
                raise wrapped from first_error
            raise primary from first_error

    def _retain_session(self, session: QuiescenceSession) -> None:
        if any(existing is session for existing in self._orphan_sessions):
            return
        if len(self._orphan_sessions) >= _MAX_ORPHAN_SESSIONS:
            error = RestoreFilesystemError("restore session retry registry is full")
            retry = lambda: session.close()
            wrapped = self._attach_cleanup_capability(error, retry)
            if wrapped is not error:
                raise wrapped from error
            raise error
        self._orphan_sessions.append(session)

    def _retain_controller(self, controller: WalSidecarController) -> None:
        if any(existing is controller for existing in self._orphan_controllers):
            return
        if len(self._orphan_controllers) >= _MAX_ORPHAN_CONTROLLERS:
            error = RestoreFilesystemError("restore controller retry registry is full")
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
            primary = (
                _restore_error_with_owner(
                    first_error,
                    "restore controller cleanup is uncertain",
                )
                if isinstance(first_error, WalSidecarError)
                else first_error
            )
            wrapped = self._attach_cleanup_capability(primary)
            if wrapped is not primary:
                raise wrapped from first_error
            if primary is not first_error:
                raise primary from first_error
            raise primary

    def _hold_quiescence(
        self,
        controller: WalSidecarController,
        *,
        allowed_root_names: tuple[str, ...],
    ) -> QuiescenceSession:
        try:
            return controller.hold_quiescence(
                allowed_root_names=allowed_root_names,
            )
        except _CLEANUP_EXCEPTION as acquisition_error:
            try:
                controller.close()
            except _CLEANUP_EXCEPTION as cleanup_error:
                try:
                    self._retain_controller(controller)
                except _CLEANUP_EXCEPTION as retention_error:
                    primary = _restore_error_with_owner(
                        acquisition_error,
                        "restore quiescence acquisition cleanup is uncertain",
                    )
                    primary = _adopt_cleanup_capability(primary, cleanup_error)
                    primary = _adopt_cleanup_capability(primary, retention_error)
                    wrapped = self._attach_cleanup_capability(primary)
                    if wrapped is not primary:
                        raise wrapped from acquisition_error
                    if primary is not acquisition_error:
                        raise primary from acquisition_error
                    raise primary from cleanup_error
                primary = _restore_error_with_owner(
                    acquisition_error,
                    "restore quiescence acquisition cleanup is uncertain",
                )
                primary = _adopt_cleanup_capability(primary, cleanup_error)
                wrapped = self._attach_cleanup_capability(primary)
                if wrapped is not primary:
                    raise wrapped from acquisition_error
                if primary is not acquisition_error:
                    raise primary from acquisition_error
                raise primary from cleanup_error
            raise

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
            primary = (
                _restore_error_with_owner(
                    first_error,
                    "restore session cleanup is uncertain",
                )
                if isinstance(first_error, WalSidecarError)
                else first_error
            )
            wrapped = self._attach_cleanup_capability(primary)
            if wrapped is not primary:
                raise wrapped from first_error
            if primary is not first_error:
                raise primary from first_error
            raise primary

    def _retry_backup_helper(self) -> None:
        try:
            self._backup_helper.close()
        except _CLEANUP_EXCEPTION as error:
            wrapped = _restore_error_with_owner(
                error,
                "restore backup helper cleanup is uncertain",
            )
            if wrapped is not error:
                raise wrapped from error
            raise

    def _retry_retained_resources(self) -> None:
        first_error: BaseException | None = None
        for retry in (
            self._retry_orphan_controllers,
            self._retry_orphan_sessions,
            self._retry_orphan_fds,
            self._retry_backup_helper,
        ):
            try:
                retry()
            except _CLEANUP_EXCEPTION as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            wrapped = self._attach_cleanup_capability(first_error)
            if wrapped is not first_error:
                raise wrapped from first_error
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
                    primary_error = (
                        body_error if body_error is not None else cleanup_error
                    )
                    if body_error is None:
                        primary_error = _restore_error_with_owner(
                            primary_error,
                            "restore session cleanup is uncertain",
                        )
                    else:
                        primary_error = _adopt_cleanup_capability(
                            primary_error,
                            cleanup_error,
                        )
                    primary_error = _adopt_cleanup_capability(
                        primary_error,
                        retention_error,
                    )
                    wrapped = self._attach_cleanup_capability(primary_error)
                    if body_error is None:
                        if wrapped is not primary_error:
                            raise wrapped from cleanup_error
                        if primary_error is not cleanup_error:
                            raise primary_error from cleanup_error
                        raise primary_error
                    if wrapped is not primary_error:
                        raise wrapped from body_error
                    if primary_error is not body_error:
                        raise primary_error from body_error
                    raise primary_error
                if body_error is None:
                    primary_error = _restore_error_with_owner(
                        cleanup_error,
                        "restore session cleanup is uncertain",
                    )
                else:
                    primary_error = _adopt_cleanup_capability(
                        body_error,
                        cleanup_error,
                    )
                wrapped = self._attach_cleanup_capability(primary_error)
                if body_error is None:
                    if wrapped is not primary_error:
                        raise wrapped from cleanup_error
                    if primary_error is not cleanup_error:
                        raise primary_error from cleanup_error
                    raise primary_error
                if wrapped is not primary_error:
                    raise wrapped from body_error
                if primary_error is not body_error:
                    raise primary_error from body_error
                raise primary_error

    def close(self) -> None:
        """Retry retained restore session and descriptor resources."""

        self._retry_retained_resources()

    def __enter__(self) -> Self:
        self._retry_retained_resources()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
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

    def _backup(self) -> SQLiteBackup:
        return self._backup_helper

    def _controller(self) -> WalSidecarController:
        return WalSidecarController(
            self._state_root,
            busy_timeout_ms=self._busy_timeout_ms,
        )

    def _ledger(self) -> RestoreLedger:
        return RestoreLedger(
            self._state_root,
            busy_timeout_ms=self._busy_timeout_ms,
        )

    def _authority(self) -> _store.RestoreStoreAuthority:
        return _store.RestoreStoreAuthority(fault=self._fault)

    def _inspect_primary_observation(
        self,
        *,
        owner: QuiescenceOwner,
        authority: _store.RestoreStoreAuthority,
    ) -> StoreImageObservation:
        with (
            owner._borrow_root(self._state_root) as root_fd,
            _owned_fd(
                root_fd,
                _PRIMARY_NAME,
                writable=False,
                orphan_registry=self._orphan_fds,
                cleanup_callback=self.close,
            ) as (primary_fd, _),
        ):
            observation = authority.inspect_image(primary_fd)
            _assert_fd_path(root_fd, _PRIMARY_NAME, primary_fd)
        return observation

    def _verify_normal_open_binding(
        self,
        *,
        owner: QuiescenceOwner,
        authority: _store.RestoreStoreAuthority,
        state: NormalOpenRecoveryState,
    ) -> StoreImageObservation:
        with (
            owner._borrow_root(self._state_root) as root_fd,
            _owned_fd(
                root_fd,
                _PRIMARY_NAME,
                writable=False,
                orphan_registry=self._orphan_fds,
                cleanup_callback=self.close,
            ) as (primary_fd, _),
        ):
            authority.verify_history_binding(primary_fd, state)
            observation = authority.inspect_image(primary_fd)
            _assert_fd_path(root_fd, _PRIMARY_NAME, primary_fd)
        return observation

    def _normal_open_state_before_generation(
        self,
        *,
        owner: QuiescenceOwner,
        restore_generation: int,
    ) -> NormalOpenRecoveryState:
        """Read the committed prefix when the current pair is still pending.

        Recovery's normal-open consumer intentionally rejects a pending current
        generation.  Resume still needs the issuer-authenticated state for the
        committed prefix when the old primary is being inspected, so derive that
        prefix from the same durable pair records while the quiescence owner is
        held.  The current generation is never used as a new history anchor.
        """

        if type(restore_generation) is not int or restore_generation < 1:
            raise RestoreReviewRequiredError("restore generation is invalid")
        retain_fd = _recovery._owner_retain_callback(owner)
        with owner._borrow_root(self._state_root) as root_fd:
            (
                ledger_records,
                _ledger_record,
                _tombstone_record,
                tombstone_records,
            ) = _recovery._read_restore_pair(
                root_fd,
                retain_fd=retain_fd,
            )
        if any(
            record.restore_generation > restore_generation for record in ledger_records
        ) or any(
            record.restore_generation > restore_generation
            for record in tombstone_records
        ):
            raise RestoreReviewRequiredError(
                "restore history contains a newer generation"
            )
        prior_ledger_records = tuple(
            record
            for record in ledger_records
            if record.restore_generation < restore_generation
        )
        prior_tombstone_records = tuple(
            record
            for record in tombstone_records
            if record.restore_generation < restore_generation
        )
        prior_ledger = prior_ledger_records[-1] if prior_ledger_records else None
        prior_tombstone = (
            prior_tombstone_records[-1] if prior_tombstone_records else None
        )
        return _recovery._normal_open_recovery_state_from_history(
            prior_ledger_records,
            prior_ledger,
            prior_tombstone,
            prior_tombstone_records,
        )

    def _inspect_artifact(
        self,
        backup: SQLiteBackup,
        artifact: BackupArtifact,
    ) -> BackupArtifact:
        self._fault("before_preflight_call")
        try:
            observed = backup.inspect(artifact.database_basename)
        except _CLEANUP_EXCEPTION as error:
            if not isinstance(error, (BackupError, WalSidecarError)):
                raise
            wrapped = _restore_error_with_owner(
                error,
                "backup artifact cleanup is uncertain",
            )
            if wrapped is not error:
                raise wrapped from error
            raise
        self._fault("after_preflight_call")
        try:
            rechecked = backup.inspect(artifact.database_basename)
        except _CLEANUP_EXCEPTION as error:
            if not isinstance(error, (BackupError, WalSidecarError)):
                raise
            wrapped = _restore_error_with_owner(
                error,
                "backup artifact cleanup is uncertain",
            )
            if wrapped is not error:
                raise wrapped from error
            raise
        if observed != artifact or rechecked != artifact or observed != rechecked:
            raise RestoreReviewRequiredError("backup artifact does not match source")
        return rechecked

    def _allowlist(
        self, artifact: BackupArtifact, candidate_name: str
    ) -> tuple[str, ...]:
        names = (
            artifact.database_basename,
            artifact.manifest_basename,
            candidate_name,
        )
        if len(set(names)) != len(names):
            raise RestoreFilesystemError("restore allowlist contains duplicate names")
        return names

    def _precheck_source_tombstones(
        self,
        *,
        artifact: BackupArtifact,
        owner: QuiescenceOwner,
        authority: _store.RestoreStoreAuthority,
        active_tombstones: tuple[RestoreIdentity, ...],
    ) -> None:
        """Reject a resurrected image before cleanup can mutate the primary."""

        with (
            owner._borrow_root(self._state_root) as root_fd,
            _owned_fd_with_fault(
                root_fd,
                artifact.database_basename,
                writable=False,
                expected_identity=artifact.database_identity,
                orphan_registry=self._orphan_fds,
                cleanup_callback=self.close,
                fault=self._fault,
                before_point="before_source_open_call",
                after_point="after_source_open_call",
            ) as (source_fd, _),
        ):
            source_observation = authority.inspect_image(source_fd)
            _assert_fd_path(
                root_fd,
                artifact.database_basename,
                source_fd,
                expected_identity=artifact.database_identity,
            )
            _assert_source_artifact(artifact, source_observation)
            if any(
                operation.status == "RESTORE_INCOMPLETE"
                for operation in source_observation.operations
            ):
                raise _store.StoreIntegrityError(
                    "restore source contains incomplete state"
                )
            active_identities = frozenset(
                (identity.operation_id, identity.effect_key)
                for identity in active_tombstones
            )
            _assert_source_not_tombstoned(
                source_observation,
                active_identities,
            )

    def _precheck_zero_event_tombstones(
        self,
        *,
        artifact: BackupArtifact,
        owner: QuiescenceOwner,
        authority: _store.RestoreStoreAuthority,
        previous_active_tombstones: tuple[RestoreIdentity, ...],
    ) -> None:
        with owner._borrow_root(self._state_root) as root_fd, ExitStack() as stack:
            source_fd, _ = stack.enter_context(
                _owned_fd(
                    root_fd,
                    artifact.database_basename,
                    writable=False,
                    expected_identity=artifact.database_identity,
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                )
            )
            destination_fd, _ = stack.enter_context(
                _owned_fd(
                    root_fd,
                    _PRIMARY_NAME,
                    writable=False,
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                )
            )
            source_observation = authority.inspect_image(source_fd)
            destination_observation = authority.inspect_image(destination_fd)
            _assert_fd_path(
                root_fd,
                artifact.database_basename,
                source_fd,
                expected_identity=artifact.database_identity,
            )
            _assert_fd_path(root_fd, _PRIMARY_NAME, destination_fd)
            _assert_source_artifact(artifact, source_observation)
            source_keys = {
                (identity.operation_id, identity.effect_key)
                for identity in source_observation.identities
            }
            tombstones = tuple(
                identity
                for identity in destination_observation.identities
                if (identity.operation_id, identity.effect_key) not in source_keys
            )
            active_tombstones = _merge_active_tombstones(
                previous_active_tombstones,
                tombstones,
            )
            if (
                not any(
                    operation.status != "CLEANED"
                    for operation in source_observation.operations
                )
                and active_tombstones
            ):
                raise _store.StoreIntegrityError(
                    "restore tombstones have no operation event anchor"
                )

    def _new_restore(
        self,
        *,
        backup: SQLiteBackup,
        artifact: BackupArtifact,
        actor: str,
        audit_ref: str,
        session: QuiescenceSession,
        owner: QuiescenceOwner,
        previous_active_tombstones: tuple[RestoreIdentity, ...],
        handle: RestoreHandle | None,
        authority: _store.RestoreStoreAuthority,
        ledger: RestoreLedger,
        candidate_name: str,
    ) -> RestoreResult:
        self._precheck_source_tombstones(
            artifact=artifact,
            owner=owner,
            authority=authority,
            active_tombstones=previous_active_tombstones,
        )
        self._precheck_zero_event_tombstones(
            artifact=artifact,
            owner=owner,
            authority=authority,
            previous_active_tombstones=previous_active_tombstones,
        )
        self._fault("before_cleanup_call")
        cleanup = session.cleanup(CheckpointRequest("TRUNCATE"))
        self._fault("after_cleanup_call")
        if cleanup.outcome != "CLEANED":
            raise RestoreError("restore requires a CLEANED TRUNCATE result")
        session.assert_identity()

        self._fault("before_store_observation_call")
        with owner._borrow_root(self._state_root) as root_fd, ExitStack() as stack:
            source_fd, _ = stack.enter_context(
                _owned_fd_with_fault(
                    root_fd,
                    artifact.database_basename,
                    writable=False,
                    expected_identity=artifact.database_identity,
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                    fault=self._fault,
                    before_point="before_source_open_call",
                    after_point="after_source_open_call",
                )
            )
            destination_fd, _ = stack.enter_context(
                _owned_fd_with_fault(
                    root_fd,
                    _PRIMARY_NAME,
                    writable=False,
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                    fault=self._fault,
                    before_point="before_destination_open_call",
                    after_point="after_destination_open_call",
                )
            )
            source_observation = authority.inspect_image(source_fd)
            destination_observation = authority.inspect_image(destination_fd)
            self._fault("after_store_observation_call")
            _assert_fd_path(
                root_fd,
                artifact.database_basename,
                source_fd,
                expected_identity=artifact.database_identity,
            )
            _assert_fd_path(root_fd, _PRIMARY_NAME, destination_fd)
            _assert_source_artifact(artifact, source_observation)
            active_identities = frozenset(
                (identity.operation_id, identity.effect_key)
                for identity in previous_active_tombstones
            )
            _assert_source_not_tombstoned(source_observation, active_identities)
            lower_bound = (
                RecoveryFloor(0, 0) if handle is None else _floor_from_handle(handle)
            )
            restore_generation = 1 if handle is None else handle.restore_generation + 1
            candidate_fd, _ = stack.enter_context(
                _owned_fd_with_fault(
                    root_fd,
                    candidate_name,
                    writable=True,
                    create=True,
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                    fault=self._fault,
                    before_point="before_candidate_create_call",
                    after_point="after_candidate_create_call",
                )
            )
            _assert_fd_path(
                root_fd,
                artifact.database_basename,
                source_fd,
                expected_identity=artifact.database_identity,
            )
            _assert_fd_path(root_fd, _PRIMARY_NAME, destination_fd)
            self._fault("before_floor_reservation_call")
            reservation = authority.reserve_restore_floor(
                source_observation,
                destination_observation,
                lower_bound,
            )
            self._fault("after_floor_reservation_call")
            _assert_fd_path(
                root_fd,
                artifact.database_basename,
                source_fd,
                expected_identity=artifact.database_identity,
            )
            _assert_fd_path(root_fd, _PRIMARY_NAME, destination_fd)
            timestamp = self._clock()
            self._fault("before_store_apply_call")
            applied = authority.apply_candidate(
                source_fd,
                destination_fd,
                candidate_fd,
                source_observation=source_observation,
                destination_observation=destination_observation,
                ledger_floor_lower_bound=lower_bound,
                reservation=reservation,
                previous_active_tombstones=previous_active_tombstones,
                restore_generation=restore_generation,
                actor=actor,
                timestamp=timestamp,
                evidence_ref=_evidence_ref(audit_ref),
            )
            self._fault("after_store_apply_call")
            _assert_fd_path(
                root_fd,
                artifact.database_basename,
                source_fd,
                expected_identity=artifact.database_identity,
            )
            _assert_fd_path(root_fd, _PRIMARY_NAME, destination_fd)
            self._fault("before_candidate_verify_call")
            verified = authority.verify_candidate(candidate_fd, applied)
            self._fault("after_candidate_verify_call")
            if verified != applied:
                raise RestoreReviewRequiredError(
                    "restore candidate verification changed result"
                )
            current_tombstones = _canonical_restore_identities(
                applied.tombstones,
                label="restore current tombstones",
            )
            active_tombstones = _merge_active_tombstones(
                previous_active_tombstones,
                current_tombstones,
            )
            if not _identities_equal(
                applied.active_tombstones, active_tombstones
            ) or not _identities_equal(verified.active_tombstones, active_tombstones):
                raise RestoreReviewRequiredError(
                    "restore active tombstones changed during candidate verification"
                )
            final_candidate_metadata = _assert_fd_path(
                root_fd,
                candidate_name,
                candidate_fd,
            )
            candidate = DatabaseCandidate(
                name=candidate_name,
                identity=_store._identity(final_candidate_metadata),
                size=final_candidate_metadata.st_size,
                digest=applied.digest,
            )
            _fsync(root_fd, "restore candidate directory")
        session.verify_candidate(candidate)
        previous_fencing_token_hwm = max(
            destination_observation.floor.fencing_token_floor,
            destination_observation.max_fencing_token,
        )
        self._fault("before_ledger_prepare_call")
        prepared = ledger.prepare(
            backup_digest=artifact.manifest.database_digest,
            previous_primary_digest=destination_observation.digest,
            candidate_digest=applied.digest,
            identities=applied.tombstones,
            actor=actor,
            audit_ref=audit_ref,
            previous_recovery_epoch=destination_observation.floor.recovery_epoch,
            previous_fencing_token_hwm=previous_fencing_token_hwm,
            previous_last_clock_ns=destination_observation.last_clock_ns,
            floor_lower_bound=applied.floor,
            owner=owner,
        )
        self._fault("after_ledger_prepare_call")
        if (
            prepared.phase != "RESTORE_PREPARED"
            or prepared.backup_digest != artifact.manifest.database_digest
            or prepared.candidate_digest != applied.digest
            or not _identities_equal(prepared.identities, applied.tombstones)
            or _floor_from_handle(prepared) != applied.floor
        ):
            raise RestoreReviewRequiredError(
                "restore prepare readback mismatches candidate"
            )
        return self._replace_and_commit(
            backup=backup,
            artifact=artifact,
            actor=actor,
            audit_ref=audit_ref,
            session=session,
            owner=owner,
            authority=authority,
            ledger=ledger,
            handle=prepared,
            candidate=candidate,
            expected=applied,
            active_tombstones=active_tombstones,
        )

    def _replaced_evidence(
        self,
        handle: RestoreHandle,
        active_tombstones: tuple[RestoreIdentity, ...],
    ) -> RestoreReplacedEvidence:
        active_tombstones = _canonical_restore_identities(
            active_tombstones,
            label="restore active tombstones",
        )
        return RestoreReplacedEvidence(
            restore_generation=handle.restore_generation,
            source_digest=handle.backup_digest,
            candidate_digest=handle.candidate_digest,
            previous_primary_digest=handle.previous_primary_digest,
            previous_recovery_epoch=handle.previous_recovery_epoch,
            previous_fencing_token_hwm=handle.previous_fencing_token_hwm,
            previous_last_clock_ns=handle.previous_last_clock_ns,
            final_floor=_floor_from_handle(handle),
            tombstones=handle.identities,
            active_tombstones=active_tombstones,
            actor=handle.actor,
            evidence_ref=_evidence_ref(handle.audit_ref),
        )

    def _candidate_evidence(
        self,
        handle: RestoreHandle,
        active_tombstones: tuple[RestoreIdentity, ...],
    ) -> RestoreCandidateEvidence:
        active_tombstones = _canonical_restore_identities(
            active_tombstones,
            label="restore active tombstones",
        )
        return RestoreCandidateEvidence(
            restore_generation=handle.restore_generation,
            source_digest=handle.backup_digest,
            previous_primary_digest=handle.previous_primary_digest,
            candidate_digest=handle.candidate_digest,
            final_floor=_floor_from_handle(handle),
            tombstones=handle.identities,
            active_tombstones=active_tombstones,
            actor=handle.actor,
            evidence_ref=_evidence_ref(handle.audit_ref),
        )

    def _orphan_candidate_evidence(
        self,
        orphan: RestoreTombstoneOrphan,
        floor: RecoveryFloor,
    ) -> RestoreCandidateEvidence:
        tombstone = orphan.tombstone
        active_tombstones = _active_tombstones_for_orphan(orphan)
        return RestoreCandidateEvidence(
            restore_generation=tombstone.restore_generation,
            source_digest=tombstone.backup_digest,
            previous_primary_digest=tombstone.previous_primary_digest,
            candidate_digest=tombstone.candidate_digest,
            final_floor=floor,
            tombstones=tombstone.identities,
            active_tombstones=active_tombstones,
            actor=tombstone.actor,
            evidence_ref=_evidence_ref(tombstone.audit_ref),
        )

    def _verify_tombstone_first_candidate(
        self,
        *,
        artifact: BackupArtifact,
        owner: QuiescenceOwner,
        authority: _store.RestoreStoreAuthority,
        orphan: RestoreTombstoneOrphan,
        candidate_name: str,
    ) -> RecoveryFloor:
        tombstone = orphan.tombstone
        active_tombstones = _active_tombstones_for_orphan(orphan)
        if not _entry_exists_in_owner(owner, self._state_root, candidate_name):
            raise RestoreReviewRequiredError("restore orphan candidate is missing")
        self._fault("before_candidate_evidence_call")
        with (
            owner._borrow_root(self._state_root) as root_fd,
            _owned_fd_with_fault(
                root_fd,
                artifact.database_basename,
                writable=False,
                expected_identity=artifact.database_identity,
                orphan_registry=self._orphan_fds,
                cleanup_callback=self.close,
                fault=self._fault,
                before_point="before_source_open_call",
                after_point="after_source_open_call",
            ) as (source_fd, _),
            _owned_fd_with_fault(
                root_fd,
                _PRIMARY_NAME,
                writable=False,
                orphan_registry=self._orphan_fds,
                cleanup_callback=self.close,
                fault=self._fault,
                before_point="before_destination_open_call",
                after_point="after_destination_open_call",
            ) as (destination_fd, _),
            _owned_fd_with_fault(
                root_fd,
                candidate_name,
                writable=False,
                orphan_registry=self._orphan_fds,
                cleanup_callback=self.close,
                fault=self._fault,
                before_point="before_candidate_open_call",
                after_point="after_candidate_open_call",
            ) as (candidate_fd, _),
        ):
            source_observation = authority.inspect_image(source_fd)
            destination_observation = authority.inspect_image(destination_fd)
            candidate_observation = authority.inspect_image(candidate_fd)
            _assert_fd_path(
                root_fd,
                artifact.database_basename,
                source_fd,
                expected_identity=artifact.database_identity,
            )
            _assert_fd_path(root_fd, _PRIMARY_NAME, destination_fd)
            _assert_fd_path(root_fd, candidate_name, candidate_fd)
            _assert_source_artifact(artifact, source_observation)
            _assert_source_not_tombstoned(
                source_observation,
                orphan.active_identities,
            )
            if destination_observation.digest != tombstone.previous_primary_digest:
                raise RestoreReviewRequiredError(
                    "restore orphan primary does not match tombstone"
                )
            if candidate_observation.digest != tombstone.candidate_digest:
                raise RestoreReviewRequiredError(
                    "restore orphan candidate does not match tombstone"
                )
            _assert_previous_primary_observation(
                destination_observation,
                expected_digest=tombstone.previous_primary_digest,
                expected_recovery_epoch=tombstone.previous_recovery_epoch,
                expected_fencing_token_hwm=tombstone.previous_fencing_token_hwm,
                expected_last_clock_ns=tombstone.previous_last_clock_ns,
            )
            evidence = self._orphan_candidate_evidence(
                orphan,
                candidate_observation.floor,
            )
            verified = authority.verify_candidate_evidence(
                source_fd,
                destination_fd,
                candidate_fd,
                evidence,
            )
            if (
                verified.digest != tombstone.candidate_digest
                or verified.size != candidate_observation.size
                or verified.floor != candidate_observation.floor
                or not _identities_equal(verified.tombstones, tombstone.identities)
                or not _identities_equal(
                    verified.active_tombstones,
                    active_tombstones,
                )
            ):
                raise RestoreReviewRequiredError(
                    "restore orphan candidate evidence mismatches tombstone"
                )
        self._fault("after_candidate_evidence_call")
        return candidate_observation.floor

    def _verify_previous_primary_before_replace(
        self,
        *,
        owner: QuiescenceOwner,
        authority: _store.RestoreStoreAuthority,
        handle: RestoreHandle,
    ) -> None:
        with (
            owner._borrow_root(self._state_root) as root_fd,
            _owned_fd_with_fault(
                root_fd,
                _PRIMARY_NAME,
                writable=False,
                orphan_registry=self._orphan_fds,
                cleanup_callback=self.close,
                fault=self._fault,
                before_point="before_primary_open_call",
                after_point="after_primary_open_call",
            ) as (primary_fd, _),
        ):
            observation = authority.inspect_image(primary_fd)
            _assert_fd_path(root_fd, _PRIMARY_NAME, primary_fd)
        _assert_previous_primary_observation(
            observation,
            expected_digest=handle.previous_primary_digest,
            expected_recovery_epoch=handle.previous_recovery_epoch,
            expected_fencing_token_hwm=handle.previous_fencing_token_hwm,
            expected_last_clock_ns=handle.previous_last_clock_ns,
        )

    def _verify_replaced(
        self,
        *,
        artifact: BackupArtifact,
        owner: QuiescenceOwner,
        authority: _store.RestoreStoreAuthority,
        handle: RestoreHandle,
        active_tombstones: tuple[RestoreIdentity, ...],
    ) -> RestoreApplyResult:
        evidence = self._replaced_evidence(handle, active_tombstones)
        self._fault("before_primary_final_inspect_call")
        with (
            owner._borrow_root(self._state_root) as root_fd,
            _owned_fd_with_fault(
                root_fd,
                artifact.database_basename,
                writable=False,
                expected_identity=artifact.database_identity,
                orphan_registry=self._orphan_fds,
                cleanup_callback=self.close,
                fault=self._fault,
                before_point="before_source_open_call",
                after_point="after_source_open_call",
            ) as (source_fd, _),
            _owned_fd_with_fault(
                root_fd,
                _PRIMARY_NAME,
                writable=False,
                orphan_registry=self._orphan_fds,
                cleanup_callback=self.close,
                fault=self._fault,
                before_point="before_primary_open_call",
                after_point="after_primary_open_call",
            ) as (primary_fd, _),
        ):
            result = authority.verify_replaced_evidence(
                source_fd,
                primary_fd,
                evidence,
            )
        if (
            result.digest != handle.candidate_digest
            or result.floor != _floor_from_handle(handle)
            or not _identities_equal(result.tombstones, handle.identities)
            or not _identities_equal(result.active_tombstones, active_tombstones)
        ):
            raise RestoreReviewRequiredError(
                "replaced primary evidence mismatches ledger"
            )
        return result

    def _assert_no_primary_sidecars(self, owner: QuiescenceOwner) -> None:
        with owner._borrow_root(self._state_root) as root_fd:
            for suffix in ("-wal", "-shm", "-journal"):
                name = f"{_PRIMARY_NAME}{suffix}"
                try:
                    os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise RestoreReviewRequiredError(
                        f"restore sidecar {name} cannot be inspected"
                    ) from exc
                raise RestoreReviewRequiredError(f"restore sidecar {name} is present")

    def _finalize_committed(
        self,
        *,
        backup: SQLiteBackup,
        artifact: BackupArtifact,
        owner: QuiescenceOwner,
        session: QuiescenceSession,
        authority: _store.RestoreStoreAuthority,
        ledger: RestoreLedger,
        handle: RestoreHandle,
        candidate_name: str,
        active_tombstones: tuple[RestoreIdentity, ...],
    ) -> RestoreResult:
        active_tombstones = _canonical_restore_identities(
            active_tombstones,
            label="restore expected final active tombstones",
        )
        # This is the last injectable hook.  It must precede every final
        # readback; no callback runs after the final filesystem observation.
        self._fault("before_result_call")
        self._fault("before_final_artifact_inspect_call")
        self._inspect_artifact(backup, artifact)
        if _entry_exists_in_owner(owner, self._state_root, candidate_name):
            raise RestoreReviewRequiredError(
                "restore candidate is present after commit"
            )
        self._assert_no_primary_sidecars(owner)
        self._fault("before_final_primary_verify_call")
        self._verify_replaced(
            artifact=artifact,
            owner=owner,
            authority=authority,
            handle=handle,
            active_tombstones=active_tombstones,
        )
        verified = ledger.verify_generation(handle, owner)
        active = _canonical_active_tombstones(
            ledger.active_committed_identities(owner),
            label="restore final active tombstones",
        )
        session.assert_identity()
        self._assert_no_primary_sidecars(owner)
        expected_identities = frozenset(
            (identity.operation_id, identity.effect_key)
            for identity in verified.identities
        )
        active_keys = {
            (identity.operation_id, identity.effect_key) for identity in active
        }
        if (
            verified.phase != "RESTORE_COMMITTED"
            or verified.tombstone_phase != "COMMITTED"
            or not expected_identities <= active_keys
            or not _identities_equal(active, active_tombstones)
        ):
            raise RestoreReviewRequiredError("restore final readback is invalid")
        return _result_from_handle(verified, active_tombstones)

    def _replace_and_commit(
        self,
        *,
        backup: SQLiteBackup,
        artifact: BackupArtifact,
        actor: str,
        audit_ref: str,
        session: QuiescenceSession,
        owner: QuiescenceOwner,
        authority: _store.RestoreStoreAuthority,
        ledger: RestoreLedger,
        handle: RestoreHandle,
        candidate: DatabaseCandidate,
        expected: RestoreApplyResult,
        active_tombstones: tuple[RestoreIdentity, ...],
    ) -> RestoreResult:
        del actor, audit_ref
        session.assert_identity()
        self._fault("before_replace_call")
        self._inspect_artifact(backup, artifact)
        self._verify_previous_primary_before_replace(
            owner=owner,
            authority=authority,
            handle=handle,
        )
        session.replace_database(candidate)
        self._fault("after_replace_call")
        if _entry_exists_in_owner(owner, self._state_root, candidate.name):
            raise RestoreReviewRequiredError("restore candidate remained after replace")
        final_result = self._verify_replaced(
            artifact=artifact,
            owner=owner,
            authority=authority,
            handle=handle,
            active_tombstones=active_tombstones,
        )
        _assert_apply_results_match(expected, final_result)
        self._fault("before_mark_replaced_call")
        self._inspect_artifact(backup, artifact)
        replaced = ledger.mark_replaced(handle, final_result.floor, owner)
        self._fault("after_mark_replaced_call")
        replaced = ledger.verify_generation(replaced, owner)
        if replaced.phase != "RESTORE_REPLACED":
            raise RestoreReviewRequiredError("restore replaced readback is invalid")
        self._fault("before_mark_committed_call")
        self._inspect_artifact(backup, artifact)
        committed = ledger.mark_committed(replaced, final_result.floor, owner)
        self._fault("after_mark_committed_call")
        self._fault("before_final_ledger_verify_call")
        result = self._finalize_committed(
            backup=backup,
            artifact=artifact,
            owner=owner,
            session=session,
            authority=authority,
            ledger=ledger,
            handle=committed,
            candidate_name=candidate.name,
            active_tombstones=active_tombstones,
        )
        return result

    def _finish_pending_resume(
        self,
        *,
        backup: SQLiteBackup,
        artifact: BackupArtifact,
        owner: QuiescenceOwner,
        session: QuiescenceSession,
        authority: _store.RestoreStoreAuthority,
        ledger: RestoreLedger,
        handle: RestoreHandle,
        candidate_name: str,
        active_tombstones: tuple[RestoreIdentity, ...],
    ) -> RestoreResult:
        primary_digest: str | None = None
        with owner._borrow_root(self._state_root) as root_fd:
            if not _entry_exists(root_fd, _PRIMARY_NAME):
                raise RestoreReviewRequiredError("restore primary is missing")
            with _owned_fd_with_fault(
                root_fd,
                _PRIMARY_NAME,
                writable=False,
                orphan_registry=self._orphan_fds,
                cleanup_callback=self.close,
                fault=self._fault,
                before_point="before_primary_open_call",
                after_point="after_primary_open_call",
            ) as (primary_fd, _):
                primary_observation = authority.inspect_image(primary_fd)
                primary_digest = primary_observation.digest
        primary_is_old = primary_digest == handle.previous_primary_digest
        primary_is_new = primary_digest == handle.candidate_digest
        if primary_is_old and primary_is_new:
            raise RestoreReviewRequiredError(
                "restore primary classification is ambiguous"
            )
        candidate_present = _entry_exists_in_owner(
            owner, self._state_root, candidate_name
        )
        if primary_is_old and candidate_present:
            self._fault("before_candidate_evidence_call")
            with (
                owner._borrow_root(self._state_root) as root_fd,
                _owned_fd_with_fault(
                    root_fd,
                    artifact.database_basename,
                    writable=False,
                    expected_identity=artifact.database_identity,
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                    fault=self._fault,
                    before_point="before_source_open_call",
                    after_point="after_source_open_call",
                ) as (source_fd, _),
                _owned_fd_with_fault(
                    root_fd,
                    _PRIMARY_NAME,
                    writable=False,
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                    fault=self._fault,
                    before_point="before_destination_open_call",
                    after_point="after_destination_open_call",
                ) as (destination_fd, _),
                _owned_fd_with_fault(
                    root_fd,
                    candidate_name,
                    writable=False,
                    orphan_registry=self._orphan_fds,
                    cleanup_callback=self.close,
                    fault=self._fault,
                    before_point="before_candidate_open_call",
                    after_point="after_candidate_open_call",
                ) as (candidate_fd, candidate_metadata),
            ):
                verified = authority.verify_candidate_evidence(
                    source_fd,
                    destination_fd,
                    candidate_fd,
                    self._candidate_evidence(handle, active_tombstones),
                )
                candidate = DatabaseCandidate(
                    name=candidate_name,
                    identity=_store._identity(candidate_metadata),
                    size=candidate_metadata.st_size,
                    digest=verified.digest,
                )
            self._fault("after_candidate_evidence_call")
            _assert_apply_matches_handle(verified, handle, active_tombstones)
            session.verify_candidate(candidate)
            return self._replace_and_commit(
                backup=backup,
                artifact=artifact,
                actor=handle.actor,
                audit_ref=handle.audit_ref,
                session=session,
                owner=owner,
                authority=authority,
                ledger=ledger,
                handle=handle,
                candidate=candidate,
                expected=verified,
                active_tombstones=active_tombstones,
            )
        if primary_is_new and not candidate_present:
            raise RestoreReviewRequiredError(
                "prepared restore replacement durability is unproven"
            )
        raise RestoreReviewRequiredError(
            "restore primary/candidate state is mixed or incomplete"
        )

    def _finish_terminal(
        self,
        *,
        backup: SQLiteBackup,
        artifact: BackupArtifact,
        owner: QuiescenceOwner,
        session: QuiescenceSession,
        authority: _store.RestoreStoreAuthority,
        ledger: RestoreLedger,
        handle: RestoreHandle,
        candidate_name: str,
        active_tombstones: tuple[RestoreIdentity, ...],
    ) -> RestoreResult:
        if handle.phase == "RESTORE_ABORTED":
            self._assert_aborted_primary(
                owner=owner,
                authority=authority,
                handle=handle,
            )
            del candidate_name
            verified = ledger.verify_generation(handle, owner)
            active = _canonical_active_tombstones(
                ledger.active_committed_identities(owner),
                label="restore aborted active tombstones",
            )
            if not _identities_equal(active, active_tombstones):
                raise RestoreReviewRequiredError(
                    "restore aborted active tombstones changed"
                )
            return _result_from_handle(
                verified,
                active,
            )
        if _entry_exists_in_owner(owner, self._state_root, candidate_name):
            raise RestoreReviewRequiredError(
                "restore candidate is present after replace"
            )
        final_before_commit = self._verify_replaced(
            artifact=artifact,
            owner=owner,
            authority=authority,
            handle=handle,
            active_tombstones=active_tombstones,
        )
        verified = ledger.verify_generation(handle, owner)
        if verified.phase == "RESTORE_REPLACED":
            self._fault("before_mark_committed_call")
            self._inspect_artifact(backup, artifact)
            verified = ledger.mark_committed(
                verified,
                final_before_commit.floor,
                owner,
            )
            self._fault("after_mark_committed_call")
        return self._finalize_committed(
            backup=backup,
            artifact=artifact,
            owner=owner,
            session=session,
            authority=authority,
            ledger=ledger,
            handle=verified,
            candidate_name=candidate_name,
            active_tombstones=active_tombstones,
        )

    def _assert_aborted_primary(
        self,
        *,
        owner: QuiescenceOwner,
        authority: _store.RestoreStoreAuthority,
        handle: RestoreHandle,
    ) -> None:
        with (
            owner._borrow_root(self._state_root) as root_fd,
            _owned_fd_with_fault(
                root_fd,
                _PRIMARY_NAME,
                writable=False,
                orphan_registry=self._orphan_fds,
                cleanup_callback=self.close,
                fault=self._fault,
                before_point="before_primary_open_call",
                after_point="after_primary_open_call",
            ) as (primary_fd, _),
        ):
            observation = authority.inspect_image(primary_fd)
        if observation.digest != handle.previous_primary_digest:
            raise RestoreReviewRequiredError("aborted restore primary is not old")

    def _run_restore_impl(
        self,
        artifact: BackupArtifact,
        *,
        actor: str,
        audit_ref: str,
        resume: bool,
    ) -> RestoreResult:
        self._retry_retained_resources()
        if type(artifact) is not BackupArtifact:
            raise TypeError("backup artifact is invalid")
        actor = _identifier(actor, "actor")
        audit_ref = _identifier(audit_ref, "audit_ref")
        backup = self._backup()
        self._inspect_artifact(backup, artifact)
        candidate_name = _candidate_basename(artifact)
        allowlist = self._allowlist(artifact, candidate_name)
        controller = self._controller()
        authority = self._authority()
        ledger = self._ledger()
        self._fault("before_quiescence_call")
        session = self._hold_quiescence(
            controller,
            allowed_root_names=allowlist,
        )
        with self._session_lifecycle(session):
            self._fault("after_quiescence_call")
            owner = session.issue_owner()
            self._inspect_artifact(backup, artifact)
            resume_state = ledger.read_for_resume(owner)
            if type(resume_state) is RestoreTombstoneOrphan:
                normal_state = self._normal_open_state_before_generation(
                    owner=owner,
                    restore_generation=resume_state.tombstone.restore_generation,
                )
            elif type(resume_state) is RestoreHandle and resume_state.phase in {
                "RESTORE_PREPARED",
                "RESTORE_REPLACED",
            }:
                normal_state = self._normal_open_state_before_generation(
                    owner=owner,
                    restore_generation=resume_state.restore_generation,
                )
            else:
                normal_state = ledger.normal_open_state(owner)
            previous_active_tombstones = _canonical_active_tombstones(
                normal_state.active_committed_tombstones,
                label="restore previous active tombstones",
            )
            handle: RestoreHandle | None = None
            if resume:
                if resume_state is None:
                    raise RestoreError("restore ledger is missing")
                if type(resume_state) is RestoreTombstoneOrphan:
                    orphan = resume_state
                    tombstone = orphan.tombstone
                    if (
                        tombstone.backup_digest != artifact.manifest.database_digest
                        or tombstone.actor != actor
                        or tombstone.audit_ref != audit_ref
                    ):
                        raise RestoreReviewRequiredError(
                            "resume request does not match restore orphan"
                        )
                    self._verify_normal_open_binding(
                        owner=owner,
                        authority=authority,
                        state=normal_state,
                    )
                    floor = self._verify_tombstone_first_candidate(
                        artifact=artifact,
                        owner=owner,
                        authority=authority,
                        orphan=orphan,
                        candidate_name=candidate_name,
                    )
                    handle = ledger.complete_tombstone_first(
                        orphan,
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                else:
                    handle = cast(RestoreHandle, resume_state)
                if handle is None:
                    raise RestoreError("restore ledger is missing")
                if (
                    handle.backup_digest != artifact.manifest.database_digest
                    or handle.actor != actor
                    or handle.audit_ref != audit_ref
                ):
                    raise RestoreReviewRequiredError(
                        "resume request does not match restore handle"
                    )
                active_tombstones = _active_tombstones_for_handle(
                    previous_active_tombstones,
                    handle,
                )
                if handle.phase in {"RESTORE_PREPARED", "RESTORE_REPLACED"}:
                    primary_observation = self._inspect_primary_observation(
                        owner=owner,
                        authority=authority,
                    )
                    primary_is_old = (
                        primary_observation.digest == handle.previous_primary_digest
                    )
                    primary_is_new = (
                        primary_observation.digest == handle.candidate_digest
                    )
                    if primary_is_old == primary_is_new:
                        raise RestoreReviewRequiredError(
                            "restore primary classification is ambiguous"
                        )
                    if primary_is_old:
                        self._verify_normal_open_binding(
                            owner=owner,
                            authority=authority,
                            state=normal_state,
                        )
                elif handle.phase in {"RESTORE_COMMITTED", "RESTORE_ABORTED"}:
                    self._verify_normal_open_binding(
                        owner=owner,
                        authority=authority,
                        state=normal_state,
                    )
                if (
                    handle.phase == "RESTORE_PREPARED"
                    and handle.tombstone_phase == "ABORTED"
                ):
                    self._assert_aborted_primary(
                        owner=owner,
                        authority=authority,
                        handle=handle,
                    )
                    aborted = ledger.mark_aborted(
                        handle,
                        floor=_floor_from_handle(handle),
                        owner=owner,
                    )
                    return self._finish_terminal(
                        backup=backup,
                        artifact=artifact,
                        owner=owner,
                        session=session,
                        authority=authority,
                        ledger=ledger,
                        handle=aborted,
                        candidate_name=candidate_name,
                        active_tombstones=previous_active_tombstones,
                    )
                if handle.phase == "RESTORE_PREPARED":
                    return self._finish_pending_resume(
                        backup=backup,
                        artifact=artifact,
                        owner=owner,
                        session=session,
                        authority=authority,
                        ledger=ledger,
                        handle=handle,
                        candidate_name=candidate_name,
                        active_tombstones=active_tombstones,
                    )
                if handle.phase in {
                    "RESTORE_REPLACED",
                    "RESTORE_COMMITTED",
                    "RESTORE_ABORTED",
                }:
                    return self._finish_terminal(
                        backup=backup,
                        artifact=artifact,
                        owner=owner,
                        session=session,
                        authority=authority,
                        ledger=ledger,
                        handle=handle,
                        candidate_name=candidate_name,
                        active_tombstones=active_tombstones,
                    )
                raise RestoreReviewRequiredError("restore phase is unsupported")
            if type(resume_state) is RestoreTombstoneOrphan:
                raise RestorePendingError(
                    "restore tombstone orphan requires explicit resume"
                )
            handle = cast(RestoreHandle | None, resume_state)
            if handle is not None and handle.phase in {
                "RESTORE_PREPARED",
                "RESTORE_REPLACED",
            }:
                raise RestorePendingError("restore generation is already pending")
            active_tombstones = (
                previous_active_tombstones
                if handle is None
                else _active_tombstones_for_handle(
                    previous_active_tombstones,
                    handle,
                )
            )
            if handle is not None and (
                handle.backup_digest == artifact.manifest.database_digest
                and handle.actor == actor
                and handle.audit_ref == audit_ref
                and handle.phase in {"RESTORE_COMMITTED", "RESTORE_ABORTED"}
            ):
                self._verify_normal_open_binding(
                    owner=owner,
                    authority=authority,
                    state=normal_state,
                )
                return self._finish_terminal(
                    backup=backup,
                    artifact=artifact,
                    owner=owner,
                    session=session,
                    authority=authority,
                    ledger=ledger,
                    handle=handle,
                    candidate_name=candidate_name,
                    active_tombstones=active_tombstones,
                )
            self._verify_normal_open_binding(
                owner=owner,
                authority=authority,
                state=normal_state,
            )
            return self._new_restore(
                backup=backup,
                artifact=artifact,
                actor=actor,
                audit_ref=audit_ref,
                session=session,
                owner=owner,
                previous_active_tombstones=previous_active_tombstones,
                handle=handle,
                authority=authority,
                ledger=ledger,
                candidate_name=candidate_name,
            )

    def _run_restore(
        self,
        artifact: BackupArtifact,
        *,
        actor: str,
        audit_ref: str,
        resume: bool,
    ) -> RestoreResult:
        try:
            return self._run_restore_impl(
                artifact,
                actor=actor,
                audit_ref=audit_ref,
                resume=resume,
            )
        except (BackupError, WalSidecarError) as error:
            wrapped = _restore_error_with_owner(
                error,
                "restore lower-layer cleanup is uncertain",
            )
            if wrapped is not error:
                raise wrapped from error
            raise

    def restore(
        self,
        artifact: BackupArtifact,
        *,
        actor: str,
        audit_ref: str,
    ) -> RestoreResult:
        """Start one new candidate-first restore generation."""

        return self._run_restore(
            artifact,
            actor=actor,
            audit_ref=audit_ref,
            resume=False,
        )

    def resume(
        self,
        artifact: BackupArtifact,
        *,
        actor: str,
        audit_ref: str,
    ) -> RestoreResult:
        """Resume only the exact generation described by the durable ledger."""

        return self._run_restore(
            artifact,
            actor=actor,
            audit_ref=audit_ref,
            resume=True,
        )


def _entry_exists_in_owner(owner: QuiescenceOwner, state_root: Path, name: str) -> bool:
    with owner._borrow_root(state_root) as root_fd:
        return _entry_exists(root_fd, name)


def _result_from_handle(
    handle: RestoreHandle,
    active_tombstones: tuple[RestoreIdentity, ...],
) -> RestoreResult:
    active_tombstones = _canonical_restore_identities(
        active_tombstones,
        label="restore result active tombstones",
    )
    return RestoreResult(
        phase=handle.phase,
        restore_generation=handle.restore_generation,
        backup_digest=handle.backup_digest,
        candidate_digest=handle.candidate_digest,
        floor=_floor_from_handle(handle),
        identities=handle.identities,
        active_tombstones=active_tombstones,
    )


__all__ = [
    "BackupRestore",
    "RestoreDurabilityError",
    "RestoreError",
    "RestoreFilesystemError",
    "RestorePendingError",
    "RestorePhase",
    "RestoreResult",
    "RestoreReviewRequiredError",
]
