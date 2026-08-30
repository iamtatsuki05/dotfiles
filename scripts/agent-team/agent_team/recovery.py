"""Explicit, fail-closed recovery operations for the local coordination store.

Recovery is deliberately separate from the normal lease/provider API.  This
module never executes a provider effect and never infers an absent effect from
local state.  The SQLite mutation authority remains the private typed
``_RecoveryStoreTx`` seam in :mod:`agent_team.store`; the JSONL ledger is an
independent owner-only append log reserved for restore phases consumed by
``agent_team.doctor``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol, cast

from . import doctor as _doctor
from . import store as _store
from .doctor import DoctorReport, LedgerPhase, LedgerSnapshot, StateFilesystem
from .lease import (
    _RECEIPT_SENTINEL,
    LeaseConflictError,
    ProviderBlockedError,
    ProviderCapabilities,
    ProviderEffect,
    ProviderFenceProof,
    ProviderPort,
    ProviderProofError,
    ProviderReceiptError,
    ProviderStatus,
    RecoveryFloor,
    RecoverySnapshot,
    VerifiedProviderReceipt,
    _verified_receipt_from_status,
    require_provider_capabilities,
)

RECOVERY_LEDGER_VERSION: Final[int] = 1
RECOVERY_LEDGER_BASENAME: Final[str] = "recovery.ledger"
WRITER_MARKER_BASENAME: Final[str] = "writer.marker"
MAX_LEDGER_BYTES: Final[int] = _doctor.MAX_LEDGER_BYTES

RecoveryAction = Literal[
    "recover",
    "force_recover",
    "resolve_unknown",
    "rebind_receipt",
]
ForceReasonCode = Literal[
    "force_recover",
    "manual_intervention",
    "operator_override",
    "recovery_required",
    "restore_recovery",
]

FORCE_REASON_CODES: Final[tuple[ForceReasonCode, ...]] = (
    "force_recover",
    "manual_intervention",
    "operator_override",
    "recovery_required",
    "restore_recovery",
)
_RECOVERY_ACTIONS: Final[tuple[RecoveryAction, ...]] = (
    "recover",
    "force_recover",
    "resolve_unknown",
    "rebind_receipt",
)
_TERMINAL_PHASES: Final[frozenset[LedgerPhase]] = frozenset(
    {"RESTORE_COMMITTED", "RESTORE_ABORTED"}
)
_LEDGER_TRANSITIONS: Final[dict[LedgerPhase, frozenset[LedgerPhase]]] = {
    "RESTORE_PREPARED": frozenset({"RESTORE_REPLACED", "RESTORE_ABORTED"}),
    "RESTORE_REPLACED": frozenset({"RESTORE_COMMITTED", "RESTORE_ABORTED"}),
}


@dataclass(frozen=True, slots=True)
class RecoveryLayout:
    """Immutable layout identity shared by every coordinator entry point."""

    marker_name: str
    ledger_name: str = RECOVERY_LEDGER_BASENAME

    def __post_init__(self) -> None:
        if type(self.marker_name) is not str:
            raise TypeError("marker_name is invalid")
        _doctor._require_basename(self.marker_name, "marker_name")
        if type(self.ledger_name) is not str:
            raise TypeError("ledger_name is invalid")
        if self.ledger_name != RECOVERY_LEDGER_BASENAME:
            raise ValueError("ledger_name is not canonical")
        if self.marker_name == self.ledger_name:
            raise ValueError("marker_name and ledger basename must differ")


class RecoveryError(RuntimeError):
    """Base class for explicit recovery failures."""


class RecoveryRequiredError(RecoveryError):
    """Recovery evidence or an external ledger is unsafe or incomplete."""


class RecoveryConflictError(RecoveryError, _store.StoreError):
    """A recovery caller supplied stale or conflicting state identity."""


class RecoveryCommitUnknownError(RecoveryError, _store.StoreCommitUnknownError):
    """The store committed, but post-commit identity could not be verified."""


class RecoveryAuthorizationError(RecoveryRequiredError):
    """A force-recovery request lacks trusted, exact operator authorization."""


class RecoveryLedgerError(RecoveryRequiredError):
    """The append-only recovery ledger cannot be safely read or extended."""


def _identifier(value: object, name: str) -> str:
    try:
        return _store._require_opaque_identifier(value, name)
    except (TypeError, ValueError) as exc:
        msg = f"{name} is invalid"
        raise ValueError(msg) from exc


def _timestamp(value: object, name: str = "timestamp") -> int:
    try:
        return _store._require_sqlite_integer(value, name)
    except (TypeError, ValueError) as exc:
        msg = f"{name} is invalid"
        raise ValueError(msg) from exc


def _digest(value: object, name: str = "backup_digest") -> str:
    if type(value) is not str:
        raise ValueError(f"{name} is invalid")
    try:
        return _doctor._require_digest(value, name)
    except (TypeError, ValueError) as exc:
        msg = f"{name} is invalid"
        raise ValueError(msg) from exc


def _evidence(value: object | None) -> str | None:
    if value is None:
        return None
    try:
        return _store._require_evidence_ref(value)
    except (TypeError, ValueError) as exc:
        msg = "evidence_ref is invalid"
        raise ValueError(msg) from exc


def _force_reason(value: object) -> ForceReasonCode:
    if type(value) is not str or value not in FORCE_REASON_CODES:
        raise ValueError("reason_code is unsupported")
    return value


@dataclass(frozen=True, slots=True, init=False)
class RecoveryAuthorization:
    """Opaque authorization issued by a trusted :class:`RecoveryAuthorizer`.

    The constructor is intentionally unavailable to callers.  A coordinator
    accepts only a value issued by the authorizer protocol and rechecks every
    requested identity against that value.
    """

    operation_id: str
    operator_id: str
    reason_code: ForceReasonCode
    audit_ref: str
    _provenance: object = field(default=None, repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("RecoveryAuthorization instances are authorizer-issued")

    @property
    def is_verified(self) -> bool:
        return getattr(self, "_provenance", None) is _AUTHORIZATION_SENTINEL


_AUTHORIZATION_SENTINEL = object()


def _issue_recovery_authorization(
    *,
    operation_id: str,
    operator_id: str,
    reason_code: str,
    audit_ref: str,
) -> RecoveryAuthorization:
    operation_id = _identifier(operation_id, "operation_id")
    operator_id = _identifier(operator_id, "operator_id")
    audit_ref = _identifier(audit_ref, "audit_ref")
    reason_code = _force_reason(reason_code)
    instance = object.__new__(RecoveryAuthorization)
    for field_name, value in {
        "operation_id": operation_id,
        "operator_id": operator_id,
        "reason_code": reason_code,
        "audit_ref": audit_ref,
    }.items():
        object.__setattr__(instance, field_name, value)
    object.__setattr__(instance, "_provenance", _AUTHORIZATION_SENTINEL)
    return instance


def _authorization_values(authorization: object) -> tuple[str, str, str, str]:
    if type(authorization) is not RecoveryAuthorization:
        raise RecoveryAuthorizationError(
            "recovery authorization has an unsupported type"
        )
    try:
        provenance = object.__getattribute__(authorization, "_provenance")
        values = tuple(
            object.__getattribute__(authorization, name)
            for name in ("operation_id", "operator_id", "reason_code", "audit_ref")
        )
    except AttributeError as exc:
        raise RecoveryAuthorizationError(
            "recovery authorization fields are unavailable"
        ) from exc
    if provenance is not _AUTHORIZATION_SENTINEL:
        raise RecoveryAuthorizationError("recovery authorization is unissued")
    if any(type(value) is not str for value in values):
        raise RecoveryAuthorizationError(
            "recovery authorization fields must be exact strings"
        )
    try:
        operation_id = _identifier(values[0], "operation_id")
        operator_id = _identifier(values[1], "operator_id")
        reason_code = _force_reason(values[2])
        audit_ref = _identifier(values[3], "audit_ref")
    except (TypeError, ValueError) as exc:
        raise RecoveryAuthorizationError(
            "recovery authorization fields are invalid"
        ) from exc
    return operation_id, operator_id, reason_code, audit_ref


class RecoveryAuthorizer(Protocol):
    """Trusted composition-root protocol for force recovery."""

    def authorize(
        self,
        *,
        operation_id: str,
        operator_id: str,
        reason_code: str,
        audit_ref: str,
    ) -> RecoveryAuthorization: ...


@dataclass(frozen=True, slots=True)
class RecoveryLedgerRecord:
    """One strict JSONL recovery-ledger record."""

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
        if type(self.version) is not int or self.version != RECOVERY_LEDGER_VERSION:
            raise ValueError("recovery ledger version is unsupported")
        _store._require_sqlite_integer(self.sequence, "sequence", minimum=1)
        if type(self.phase) is not str or self.phase not in _doctor._LEDGER_PHASES:
            raise ValueError("recovery ledger phase is unsupported")
        for value, name in (
            (self.restore_generation, "restore_generation"),
            (self.recovery_epoch, "recovery_epoch"),
            (self.fencing_token_floor, "fencing_token_floor"),
        ):
            _timestamp(value, name)
        _digest(self.backup_digest)
        _identifier(self.actor, "actor")
        _identifier(self.audit_ref, "audit_ref")


_LEDGER_RECORD_FIELDS: Final[tuple[str, ...]] = (
    "version",
    "sequence",
    "phase",
    "restore_generation",
    "recovery_epoch",
    "fencing_token_floor",
    "backup_digest",
    "actor",
    "audit_ref",
)


def _record_values(record: object) -> tuple[object, ...]:
    if type(record) is not RecoveryLedgerRecord:
        raise TypeError("record must be an exact RecoveryLedgerRecord")
    try:
        return tuple(
            object.__getattribute__(record, name) for name in _LEDGER_RECORD_FIELDS
        )
    except (AttributeError, TypeError) as exc:
        raise ValueError("recovery ledger record fields are unavailable") from exc


def _canonical_record(record: object) -> RecoveryLedgerRecord:
    values = _record_values(record)
    try:
        return RecoveryLedgerRecord(
            version=cast(int, values[0]),
            sequence=cast(int, values[1]),
            phase=cast(LedgerPhase, values[2]),
            restore_generation=cast(int, values[3]),
            recovery_epoch=cast(int, values[4]),
            fencing_token_floor=cast(int, values[5]),
            backup_digest=cast(str, values[6]),
            actor=cast(str, values[7]),
            audit_ref=cast(str, values[8]),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecoveryLedgerError("recovery ledger record is invalid") from exc


def _record_mapping(record: RecoveryLedgerRecord) -> dict[str, object]:
    values = _record_values(record)
    return dict(zip(_LEDGER_RECORD_FIELDS, values, strict=True))


def _encode_record(record: RecoveryLedgerRecord) -> bytes:
    return (
        json.dumps(
            _record_mapping(record),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _record_from_snapshot(snapshot: LedgerSnapshot) -> RecoveryLedgerRecord:
    return RecoveryLedgerRecord(
        version=snapshot.version,
        sequence=snapshot.sequence,
        phase=snapshot.phase,
        restore_generation=snapshot.restore_generation,
        recovery_epoch=snapshot.recovery_epoch,
        fencing_token_floor=snapshot.fencing_token_floor,
        backup_digest=snapshot.backup_digest,
        actor=snapshot.actor,
        audit_ref=snapshot.audit_ref,
    )


def _same_record(left: RecoveryLedgerRecord, right: RecoveryLedgerRecord) -> bool:
    return _record_values(left) == _record_values(right)


def _same_snapshot(
    left: LedgerSnapshot | None,
    right: LedgerSnapshot | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return (
        left.version,
        left.sequence,
        left.phase,
        left.restore_generation,
        left.recovery_epoch,
        left.fencing_token_floor,
        left.backup_digest,
        left.actor,
        left.audit_ref,
    ) == (
        right.version,
        right.sequence,
        right.phase,
        right.restore_generation,
        right.recovery_epoch,
        right.fencing_token_floor,
        right.backup_digest,
        right.actor,
        right.audit_ref,
    )


_RECEIPT_FIELDS: Final[tuple[str, ...]] = (
    "operation_id",
    "effect_key",
    "provider_id",
    "owner",
    "attempt",
    "lease_epoch",
    "fencing_token",
    "provider_effect_id",
    "provider_status",
    "proof_version",
    "proof_ref",
)


def _receipt_values(receipt: object) -> tuple[object, ...]:
    if type(receipt) is not VerifiedProviderReceipt:
        raise ProviderReceiptError("verified receipt must have its exact runtime type")
    try:
        provenance = object.__getattribute__(receipt, "_provenance")
        values = tuple(
            object.__getattribute__(receipt, name) for name in _RECEIPT_FIELDS
        )
    except AttributeError as exc:
        raise ProviderReceiptError("verified receipt fields are unavailable") from exc
    if provenance is not _RECEIPT_SENTINEL:
        raise ProviderReceiptError("verified receipt provenance is unverified")
    if any(type(values[index]) is not str for index in (0, 1, 2, 3, 7, 8, 10)) or any(
        type(values[index]) is not int for index in (4, 5, 6, 9)
    ):
        raise ProviderReceiptError("verified receipt fields must be exact scalars")
    if values[8] != "COMPLETED":
        raise ProviderReceiptError("verified receipt status is invalid")
    try:
        ProviderFenceProof(
            operation_id=cast(str, values[0]),
            effect_key=cast(str, values[1]),
            provider_id=cast(str, values[2]),
            owner=cast(str, values[3]),
            attempt=cast(int, values[4]),
            lease_epoch=cast(int, values[5]),
            fencing_token=cast(int, values[6]),
            proof_version=cast(int, values[9]),
            proof_ref=cast(str, values[10]),
        )
    except (TypeError, ValueError, ProviderProofError) as exc:
        raise ProviderReceiptError("verified receipt identity is invalid") from exc
    for value, name in (
        (values[0], "operation_id"),
        (values[1], "effect_key"),
        (values[2], "provider_id"),
        (values[3], "owner"),
        (values[7], "provider_effect_id"),
        (values[10], "proof_ref"),
    ):
        _identifier(value, name)
    return values


@dataclass(frozen=True, slots=True, init=False)
class RecoveryLedgerInitialization:
    """Opaque authority for creating the first ledger record."""

    operator_id: str
    audit_ref: str
    request_digest: str
    _provenance: object = field(default=None, repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("RecoveryLedgerInitialization instances are issued values")


_LEDGER_INITIALIZATION_SENTINEL = object()


def _issue_recovery_ledger_initialization(
    *,
    operator_id: str,
    audit_ref: str,
    request_digest: str,
) -> RecoveryLedgerInitialization:
    operator_id = _identifier(operator_id, "operator_id")
    audit_ref = _identifier(audit_ref, "audit_ref")
    request_digest = _digest(request_digest, "request_digest")
    instance = object.__new__(RecoveryLedgerInitialization)
    for name, value in {
        "operator_id": operator_id,
        "audit_ref": audit_ref,
        "request_digest": request_digest,
    }.items():
        object.__setattr__(instance, name, value)
    object.__setattr__(instance, "_provenance", _LEDGER_INITIALIZATION_SENTINEL)
    return instance


def _validate_initialization(
    authority: object,
    record: RecoveryLedgerRecord,
) -> None:
    if type(authority) is not RecoveryLedgerInitialization:
        raise RecoveryLedgerError("ledger initialization authority is invalid")
    try:
        issued = object.__getattribute__(authority, "_provenance")
        operator_id = object.__getattribute__(authority, "operator_id")
        audit_ref = object.__getattribute__(authority, "audit_ref")
        request_digest = object.__getattribute__(authority, "request_digest")
    except AttributeError as exc:
        raise RecoveryLedgerError("ledger initialization authority is invalid") from exc
    if issued is not _LEDGER_INITIALIZATION_SENTINEL:
        raise RecoveryLedgerError("ledger initialization authority is unissued")
    _identifier(operator_id, "operator_id")
    _identifier(audit_ref, "audit_ref")
    _digest(request_digest, "request_digest")
    if operator_id != record.actor or audit_ref != record.audit_ref:
        raise RecoveryLedgerError("ledger initialization identity mismatches record")
    if record.sequence != 1 or record.phase != "RESTORE_PREPARED":
        raise RecoveryLedgerError(
            "ledger initialization must start sequence one prepared"
        )


def _metadata_for_entry(metadata: os.stat_result) -> tuple[int, ...]:
    return _doctor._metadata_signature(metadata)


def _entry_metadata_matches(
    metadata: os.stat_result,
    entry: _doctor.FilesystemEntry,
) -> bool:
    return _metadata_for_entry(metadata) == (
        stat.S_IFREG,
        entry.uid,
        entry.mode,
        entry.nlink,
        entry.device,
        entry.inode,
        entry.size,
        entry.mtime_ns,
        entry.ctime_ns,
    )


class RecoveryLedgerWriter:
    """Write the canonical owner-only append-only recovery ledger."""

    def __init__(
        self,
        state_root: Path,
        *,
        marker_name: str = WRITER_MARKER_BASENAME,
        busy_timeout_ms: int = _store.DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if not isinstance(state_root, (Path, str, os.PathLike)):
            raise TypeError("state_root is invalid")
        self.state_root = _doctor._coerce_root(state_root)
        if type(marker_name) is not str:
            raise TypeError("marker_name is invalid")
        self.marker_name = _doctor._require_basename(marker_name, "marker_name")
        if self.marker_name == RECOVERY_LEDGER_BASENAME:
            raise ValueError("marker_name and ledger basename must differ")
        if (
            type(busy_timeout_ms) is not int
            or not 0 <= busy_timeout_ms <= _store.MAX_BUSY_TIMEOUT_MS
        ):
            raise ValueError("busy_timeout_ms must be between 0 and 30000")
        self.busy_timeout_ms = busy_timeout_ms
        self.ledger_name = RECOVERY_LEDGER_BASENAME

    def read(self) -> RecoveryLedgerRecord | None:
        filesystem = self._open_filesystem()
        try:
            try:
                snapshot = _doctor.RecoveryLedgerReader().read(filesystem)
            except _doctor.DoctorError as exc:
                raise RecoveryLedgerError(str(exc)) from exc
            return None if snapshot is None else _record_from_snapshot(snapshot)
        finally:
            filesystem.close()

    def initialize(
        self,
        record: RecoveryLedgerRecord,
        authority: RecoveryLedgerInitialization,
    ) -> RecoveryLedgerRecord:
        canonical = _canonical_record(record)
        _validate_initialization(authority, canonical)
        return self._append_impl(canonical, allow_create=True)

    def append(self, record: RecoveryLedgerRecord) -> RecoveryLedgerRecord:
        canonical = _canonical_record(record)
        return self._append_impl(canonical, allow_create=False)

    def _append_impl(
        self,
        record: RecoveryLedgerRecord,
        *,
        allow_create: bool,
    ) -> RecoveryLedgerRecord:
        if type(self.ledger_name) is not str:
            raise RecoveryLedgerError("recovery ledger basename is not canonical")
        if self.ledger_name != RECOVERY_LEDGER_BASENAME:
            raise RecoveryLedgerError("recovery ledger basename is not canonical")
        filesystem = self._open_filesystem()
        fd: int | None = None
        locked = False
        try:
            try:
                before = filesystem.inventory()
                latest = _doctor.RecoveryLedgerReader().read(filesystem)
            except _doctor.DoctorError as exc:
                raise RecoveryLedgerError(str(exc)) from exc
            root_fd = filesystem._root_fd
            root_identity = before.root_identity
            if root_fd is None or root_identity is None:
                raise RecoveryLedgerError("state root descriptor is unavailable")
            ledger_entry = before.entry(self.ledger_name)
            creating = ledger_entry is None
            if creating and not allow_create:
                raise RecoveryLedgerError("recovery ledger is missing")
            if not creating and allow_create:
                raise RecoveryLedgerError("recovery ledger is already initialized")
            original = b""
            if ledger_entry is not None:
                original, opened = self._read_ledger(root_fd)
                if not _entry_metadata_matches(opened, ledger_entry):
                    raise RecoveryLedgerError("recovery ledger changed before append")
            self._validate_append(record, latest)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if nofollow == 0:
                raise RecoveryLedgerError("secure no-follow append is unavailable")
            nonblock = getattr(os, "O_NONBLOCK", 0)
            if nonblock == 0:
                raise RecoveryLedgerError("non-blocking append is unavailable")
            flags |= nofollow | nonblock
            if creating:
                flags |= os.O_CREAT | os.O_EXCL
            self._fault("before_ledger_open")
            try:
                fd = os.open(self.ledger_name, flags, 0o600, dir_fd=root_fd)
            except OSError as exc:
                raise RecoveryLedgerError("recovery ledger cannot be opened") from exc
            metadata = os.fstat(fd)
            self._validate_ledger_metadata(metadata)
            if ledger_entry is not None and not _entry_metadata_matches(
                metadata,
                ledger_entry,
            ):
                raise RecoveryLedgerError("recovery ledger changed while opening")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RecoveryLedgerError("recovery ledger is busy") from exc
            locked = True
            self._fault("after_ledger_lock")
            self._assert_root_identity(filesystem, root_identity)
            current, current_metadata = self._read_ledger(root_fd)
            if creating:
                if current != original or _metadata_for_entry(
                    current_metadata
                ) != _metadata_for_entry(metadata):
                    raise RecoveryLedgerError("recovery ledger changed before append")
            elif (
                ledger_entry is None
                or current != original
                or not _entry_metadata_matches(
                    current_metadata,
                    ledger_entry,
                )
            ):
                raise RecoveryLedgerError("recovery ledger changed before append")
            if creating and current:
                raise RecoveryLedgerError("new recovery ledger is not empty")
            locked_latest = self._latest_from_bytes(current, allow_empty=creating)
            if not _same_snapshot(latest, locked_latest):
                raise RecoveryLedgerError("recovery ledger changed before append")
            self._validate_append(record, locked_latest)
            encoded = _encode_record(record)
            if len(encoded) > MAX_LEDGER_BYTES or len(current) > MAX_LEDGER_BYTES - len(
                encoded
            ):
                raise RecoveryLedgerError("recovery ledger record is too large")
            self._fault("before_ledger_write")
            offset = 0
            while offset < len(encoded):
                try:
                    written = os.write(fd, encoded[offset:])
                except OSError as exc:
                    raise RecoveryLedgerError("recovery ledger append failed") from exc
                if written <= 0:
                    raise RecoveryLedgerError("recovery ledger append was incomplete")
                offset += written
            self._fault("after_ledger_write")
            try:
                os.fsync(fd)
                os.fsync(root_fd)
            except OSError as exc:
                raise RecoveryLedgerError(
                    "recovery ledger durability is unknown"
                ) from exc
            self._fault("before_final_check")
            self._assert_root_identity(filesystem, root_identity)
            path_metadata = os.stat(
                self.ledger_name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            final_metadata = os.fstat(fd)
            self._validate_ledger_metadata(final_metadata)
            if (final_metadata.st_dev, final_metadata.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ) or (path_metadata.st_dev, path_metadata.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise RecoveryLedgerError(
                    "recovery ledger identity changed after append"
                )
            final_bytes, _ = self._read_ledger(root_fd)
            expected_bytes = current + encoded
            if final_bytes != expected_bytes:
                raise RecoveryLedgerError("recovery ledger bytes changed after append")
            final_latest = self._latest_from_bytes(final_bytes, allow_empty=False)
            if final_latest is None:
                raise RecoveryLedgerError("recovery ledger readback is empty")
            readback = _record_from_snapshot(final_latest)
            if not _same_record(record, readback):
                raise RecoveryLedgerError("recovery ledger readback mismatches record")
            return record
        except _doctor.DoctorError as exc:
            raise RecoveryLedgerError(str(exc)) from exc
        finally:
            if fd is not None:
                if locked:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                try:
                    os.close(fd)
                except OSError:
                    pass
            filesystem.close()

    def _open_filesystem(self) -> StateFilesystem:
        if type(self.ledger_name) is not str:
            raise RecoveryLedgerError("recovery ledger basename is not canonical")
        if self.ledger_name != RECOVERY_LEDGER_BASENAME:
            raise RecoveryLedgerError("recovery ledger basename is not canonical")
        try:
            return StateFilesystem.open_existing(
                self.state_root,
                marker_name=self.marker_name,
                ledger_name=self.ledger_name,
                busy_timeout_ms=self.busy_timeout_ms,
            )
        except (_doctor.DoctorError, OSError, ValueError) as exc:
            raise RecoveryLedgerError(
                "recovery ledger filesystem is unavailable"
            ) from exc

    def _read_ledger(
        self,
        root_fd: int,
    ) -> tuple[bytes, os.stat_result]:
        flags = _store._open_flags(directory=False, writable=False)
        nonblock = getattr(os, "O_NONBLOCK", 0)
        if nonblock == 0:
            raise RecoveryLedgerError("non-blocking ledger read is unavailable")
        flags |= nonblock
        try:
            read_fd = os.open(self.ledger_name, flags, dir_fd=root_fd)
        except OSError as exc:
            raise RecoveryLedgerError("recovery ledger cannot be read") from exc
        try:
            before = os.fstat(read_fd)
            self._validate_ledger_metadata(before)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(read_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_LEDGER_BYTES:
                    raise RecoveryLedgerError("recovery ledger is too large")
            after = os.fstat(read_fd)
            path_metadata = os.stat(
                self.ledger_name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if _metadata_for_entry(before) != _metadata_for_entry(
                after
            ) or _metadata_for_entry(before) != _metadata_for_entry(path_metadata):
                raise RecoveryLedgerError("recovery ledger changed while reading")
            return b"".join(chunks), before
        except OSError as exc:
            raise RecoveryLedgerError("recovery ledger cannot be read") from exc
        finally:
            os.close(read_fd)

    @staticmethod
    def _validate_ledger_metadata(metadata: os.stat_result) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RecoveryLedgerError("recovery ledger is unsafe")

    @staticmethod
    def _assert_root_identity(
        filesystem: StateFilesystem,
        expected: tuple[int, int],
    ) -> None:
        root_fd = filesystem._root_fd
        if root_fd is None:
            raise RecoveryLedgerError("state root descriptor is unavailable")
        try:
            fd_metadata = os.fstat(root_fd)
            path_metadata = os.stat(filesystem.state_root, follow_symlinks=False)
        except (OSError, ValueError) as exc:
            raise RecoveryLedgerError("state root identity is unavailable") from exc
        if (
            not stat.S_ISDIR(fd_metadata.st_mode)
            or (fd_metadata.st_dev, fd_metadata.st_ino) != expected
            or (path_metadata.st_dev, path_metadata.st_ino) != expected
            or path_metadata.st_uid != os.getuid()
            or stat.S_IMODE(path_metadata.st_mode) != 0o700
        ):
            raise RecoveryLedgerError("state root identity changed")

    @staticmethod
    def _latest_from_bytes(
        raw: bytes,
        *,
        allow_empty: bool,
    ) -> LedgerSnapshot | None:
        if not raw and allow_empty:
            return None
        records = _doctor._ledger_records(raw)
        snapshots = [_doctor._ledger_snapshot(record) for record in records]
        previous: LedgerSnapshot | None = None
        for snapshot in snapshots:
            if previous is None and snapshot.sequence != 1:
                raise _doctor.LedgerReadError(
                    "recovery ledger sequence does not start at one"
                )
            if previous is None and snapshot.phase != "RESTORE_PREPARED":
                raise _doctor.LedgerReadError(
                    "recovery ledger generation does not start prepared"
                )
            if previous is not None:
                if snapshot.sequence != previous.sequence + 1:
                    raise _doctor.LedgerReadError(
                        "recovery ledger sequence is not monotonic"
                    )
                if previous.phase in _TERMINAL_PHASES:
                    if (
                        snapshot.phase != "RESTORE_PREPARED"
                        or snapshot.restore_generation <= previous.restore_generation
                    ):
                        raise _doctor.LedgerReadError(
                            "recovery ledger terminal transition is invalid"
                        )
                else:
                    if snapshot.restore_generation != previous.restore_generation:
                        raise _doctor.LedgerReadError(
                            "recovery ledger generation changed before terminal phase"
                        )
                    if snapshot.phase not in _LEDGER_TRANSITIONS[previous.phase]:
                        raise _doctor.LedgerReadError(
                            "recovery ledger phase transition is invalid"
                        )
                if snapshot.recovery_epoch < previous.recovery_epoch:
                    raise _doctor.LedgerReadError(
                        "recovery ledger epoch moved backwards"
                    )
                if snapshot.fencing_token_floor < previous.fencing_token_floor:
                    raise _doctor.LedgerReadError(
                        "recovery ledger floor moved backwards"
                    )
                if (
                    snapshot.restore_generation == previous.restore_generation
                    and snapshot.backup_digest != previous.backup_digest
                ):
                    raise _doctor.LedgerReadError(
                        "recovery ledger digest changed in one generation"
                    )
            previous = snapshot
        return previous

    def _fault(self, point: str) -> None:
        """Deterministic process-test seam; production implementation is a no-op."""

    @staticmethod
    def _validate_append(
        record: RecoveryLedgerRecord,
        latest: LedgerSnapshot | None,
    ) -> None:
        if latest is None:
            if record.sequence != 1 or record.phase != "RESTORE_PREPARED":
                raise RecoveryLedgerError(
                    "recovery ledger must start with RESTORE_PREPARED sequence one"
                )
            return
        if record.sequence != latest.sequence + 1:
            raise RecoveryLedgerError("recovery ledger sequence is not monotonic")
        if latest.phase in _TERMINAL_PHASES:
            if (
                record.phase != "RESTORE_PREPARED"
                or record.restore_generation <= latest.restore_generation
            ):
                raise RecoveryLedgerError("recovery ledger generation is invalid")
        else:
            if record.restore_generation != latest.restore_generation:
                raise RecoveryLedgerError("recovery ledger generation is invalid")
            if record.phase not in _LEDGER_TRANSITIONS[latest.phase]:
                raise RecoveryLedgerError("recovery ledger phase transition is invalid")
        if record.recovery_epoch < latest.recovery_epoch:
            raise RecoveryLedgerError("recovery ledger epoch moved backwards")
        if record.fencing_token_floor < latest.fencing_token_floor:
            raise RecoveryLedgerError("recovery ledger floor moved backwards")
        if (
            record.restore_generation == latest.restore_generation
            and record.backup_digest != latest.backup_digest
        ):
            raise RecoveryLedgerError(
                "recovery ledger digest changed in one generation"
            )


class RecoveryLedger(RecoveryLedgerWriter):
    """Canonical public name for the recovery-ledger writer."""


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Immutable result returned only after a verified recovery transaction."""

    action: RecoveryAction
    operation_id: str
    from_status: str
    snapshot: RecoverySnapshot
    floor: RecoveryFloor | None = None
    audit_ref: str | None = None

    def __post_init__(self) -> None:
        if type(self.action) is not str or self.action not in _RECOVERY_ACTIONS:
            raise ValueError("recovery action is unsupported")
        _identifier(self.operation_id, "operation_id")
        if self.snapshot.operation_id != self.operation_id:
            raise ValueError("recovery result operation identity mismatches")
        _store._require_status(self.from_status, "from_status")
        if self.audit_ref is not None:
            _identifier(self.audit_ref, "audit_ref")

    @property
    def status(self) -> str:
        """Return the durable status after the explicit operation."""

        return self.snapshot.status

    @property
    def receipt(self) -> VerifiedProviderReceipt | None:
        """Return a verified receipt when the resulting state has one."""

        return self.snapshot.verified_receipt_identity


class RecoveryCoordinator:
    """Coordinate explicit recovery without provider execution or fallback."""

    __slots__ = ("__clock", "__layout", "__ledger", "__store")

    def __init__(
        self,
        store: _store.CoordinationStore,
        *,
        marker_name: str = WRITER_MARKER_BASENAME,
        ledger: RecoveryLedgerWriter | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(store, _store.CoordinationStore):
            raise TypeError("store is invalid")
        marker_name = _doctor._require_basename(marker_name, "marker_name")
        layout = RecoveryLayout(marker_name=marker_name)
        if ledger is not None and not isinstance(ledger, RecoveryLedgerWriter):
            raise TypeError("ledger is invalid")
        if ledger is not None and (
            ledger.state_root != store.state_root
            or ledger.marker_name != layout.marker_name
            or ledger.ledger_name != RECOVERY_LEDGER_BASENAME
        ):
            raise ValueError("ledger layout does not match store")
        coordinator_ledger = ledger or RecoveryLedgerWriter(
            store.state_root,
            marker_name=marker_name,
            busy_timeout_ms=store.busy_timeout_ms,
        )
        candidate_clock = clock or getattr(store, "_clock", time.time_ns)
        if not callable(candidate_clock):
            raise TypeError("clock is invalid")
        self.__store = store
        self.__layout = layout
        self.__ledger = coordinator_ledger
        self.__clock = candidate_clock

    @property
    def store(self) -> _store.CoordinationStore:
        return self.__store

    @property
    def layout(self) -> RecoveryLayout:
        return self.__layout

    @property
    def marker_name(self) -> str:
        return self.__layout.marker_name

    @property
    def ledger_name(self) -> str:
        return self.__layout.ledger_name

    @property
    def ledger(self) -> RecoveryLedgerWriter:
        return self.__ledger

    def _assert_layout(self) -> None:
        layout = self.__layout
        ledger = self.__ledger
        if type(layout) is not RecoveryLayout:
            raise RecoveryRequiredError("recovery layout is invalid")
        try:
            validated = RecoveryLayout(
                marker_name=layout.marker_name,
                ledger_name=layout.ledger_name,
            )
        except (TypeError, ValueError) as exc:
            raise RecoveryRequiredError("recovery layout is invalid") from exc
        if validated != layout or not isinstance(ledger, RecoveryLedgerWriter):
            raise RecoveryRequiredError("recovery layout is invalid")
        if (
            type(ledger.marker_name) is not str
            or type(ledger.ledger_name) is not str
            or ledger.ledger_name != RECOVERY_LEDGER_BASENAME
            or ledger.marker_name != layout.marker_name
            or ledger.state_root != self.__store.state_root
        ):
            raise RecoveryRequiredError("recovery layout is invalid")

    def startup_preflight(
        self,
        operation_id: str = "startup-preflight",
        *,
        state_root: Path | None = None,
    ) -> DoctorReport:
        """Inspect existing state without opening or mutating the store."""

        self._assert_layout()
        operation_id = _identifier(operation_id, "operation_id")
        root = (
            self.store.state_root
            if state_root is None
            else _doctor._coerce_root(state_root)
        )
        if root != self.store.state_root:
            raise ValueError("state_root does not match store")
        return _doctor.ReadOnlyDoctor(
            marker_name=self.marker_name,
            ledger_name=self.ledger_name,
        ).inspect(root, operation_id)

    def recover(
        self,
        operation_id: str,
        *,
        owner: str,
        provider_id: str,
        effect_key: str,
        now_ns: int | None = None,
        evidence_ref: str | None = None,
    ) -> RecoveryResult:
        """Fail closed an exact expired ``CLAIMED``/``FENCE_PENDING`` lease."""

        self._assert_layout()
        operation_id = _identifier(operation_id, "operation_id")
        owner = _identifier(owner, "owner")
        provider_id = _identifier(provider_id, "provider_id")
        effect_key = _identifier(effect_key, "effect_key")
        evidence_ref = _evidence(evidence_ref)
        timestamp = self._now(now_ns)
        snapshot = self._snapshot(operation_id)
        self._require_lease_identity(
            snapshot,
            owner=owner,
            provider_id=provider_id,
            effect_key=effect_key,
        )
        if snapshot.status not in {"FENCE_PENDING", "CLAIMED"}:
            raise RecoveryConflictError("operation is not an expiring lease")
        try:
            with self.store._recovery_transaction() as transaction:
                updated = transaction.recover_expired(
                    snapshot,
                    actor=owner,
                    timestamp=timestamp,
                    evidence_ref=evidence_ref,
                )
        except _store.StoreCommitUnknownError as exc:
            raise RecoveryCommitUnknownError(str(exc)) from exc
        except (_store.StoreError, LeaseConflictError) as exc:
            raise self._conflict_if_lease(exc) from exc
        self._fault("before_result")
        result = RecoveryResult(
            action="recover",
            operation_id=operation_id,
            from_status=snapshot.status,
            snapshot=updated,
        )
        self._fault("after_result")
        return result

    def force_recover(
        self,
        operation_id: str,
        *,
        operator_id: str,
        reason_code: str,
        audit_ref: str,
        authorizer: RecoveryAuthorizer,
        now_ns: int | None = None,
    ) -> RecoveryResult:
        """Advance the store-issued floor and explicitly fence one operation."""

        self._assert_layout()
        operation_id = _identifier(operation_id, "operation_id")
        operator_id = _identifier(operator_id, "operator_id")
        audit_ref = _identifier(audit_ref, "audit_ref")
        reason_code = _force_reason(reason_code)
        authorize = getattr(authorizer, "authorize", None)
        if not callable(authorize):
            raise RecoveryAuthorizationError("trusted recovery authorizer is required")
        try:
            authorization = authorize(
                operation_id=operation_id,
                operator_id=operator_id,
                reason_code=reason_code,
                audit_ref=audit_ref,
            )
        except RecoveryError:
            raise
        except (ProviderBlockedError, TypeError, ValueError) as exc:
            raise RecoveryAuthorizationError(
                "trusted recovery authorizer rejected the request"
            ) from exc
        authorization_values = _authorization_values(authorization)
        if authorization_values != (
            operation_id,
            operator_id,
            reason_code,
            audit_ref,
        ):
            raise RecoveryAuthorizationError("recovery authorization is invalid")
        timestamp = self._now(now_ns)
        snapshot = self._snapshot(operation_id)
        if snapshot.status in {"CLEANED", "RESTORE_INCOMPLETE"}:
            raise RecoveryConflictError("operation cannot be force recovered")
        evidence_ref = _audit_evidence(audit_ref)
        reservation = self.store._reserve_floor()
        try:
            with self.store._recovery_transaction() as transaction:
                floor = transaction.advance_floor(reservation, timestamp=timestamp)
                updated = transaction.force_recover(
                    snapshot,
                    actor=operator_id,
                    timestamp=timestamp,
                    evidence_ref=evidence_ref,
                )
        except _store.StoreCommitUnknownError as exc:
            raise RecoveryCommitUnknownError(str(exc)) from exc
        except (_store.StoreError, LeaseConflictError) as exc:
            raise self._conflict_if_lease(exc) from exc
        self._fault("before_result")
        result = RecoveryResult(
            action="force_recover",
            operation_id=operation_id,
            from_status=snapshot.status,
            snapshot=updated,
            floor=floor,
            audit_ref=audit_ref,
        )
        self._fault("after_result")
        return result

    def resolve_unknown(
        self,
        operation_id: str,
        *,
        provider: ProviderPort,
        actor: str,
        now_ns: int | None = None,
        evidence_ref: str | None = None,
    ) -> RecoveryResult:
        """Resolve unknown only from an exact strong current provider status."""

        self._assert_layout()
        operation_id = _identifier(operation_id, "operation_id")
        actor = _identifier(actor, "actor")
        evidence_ref = _evidence(evidence_ref)
        self._require_status_provider(provider)
        timestamp = self._now(now_ns)
        self._require_status_preflight(operation_id)
        snapshot, effect = self._unknown_effect(operation_id)
        if snapshot.status not in {"UNKNOWN_EFFECT", "UNKNOWN"}:
            raise RecoveryConflictError("operation is not unknown")
        if effect is None or effect.fence_proof is None:
            raise RecoveryRequiredError(
                "unknown operation has no verifiable effect identity"
            )
        self._require_current_epoch(snapshot)
        try:
            status = provider.status(effect)
        except Exception as exc:
            raise RecoveryRequiredError("provider status outcome is unknown") from exc
        self._validate_current_status(effect, status)
        if status.status not in {"ABSENT", "COMPLETED"}:
            raise RecoveryRequiredError("provider status does not resolve the effect")
        receipt: VerifiedProviderReceipt | None = None
        if status.status == "COMPLETED":
            try:
                receipt = _verified_receipt_from_status(
                    effect,
                    effect.fence_proof,
                    status,
                )
            except (
                ProviderProofError,
                ProviderReceiptError,
                TypeError,
                ValueError,
            ) as exc:
                raise RecoveryRequiredError(
                    "provider completed status is unverified"
                ) from exc
        try:
            with self.store._recovery_transaction() as transaction:
                current = transaction.snapshot(operation_id)
                self._require_snapshot_match(snapshot, current)
                if status.status == "ABSENT":
                    updated = transaction.resolve_unknown_absent(
                        current,
                        actor=actor,
                        timestamp=timestamp,
                        evidence_ref=evidence_ref,
                    )
                else:
                    assert receipt is not None
                    updated = transaction.resolve_unknown_completed(
                        current,
                        receipt,
                        actor=actor,
                        timestamp=timestamp,
                        evidence_ref=evidence_ref,
                    )
        except _store.StoreCommitUnknownError as exc:
            raise RecoveryCommitUnknownError(str(exc)) from exc
        except (_store.StoreError, LeaseConflictError) as exc:
            raise self._conflict_if_lease(exc) from exc
        self._fault("before_result")
        result = RecoveryResult(
            action="resolve_unknown",
            operation_id=operation_id,
            from_status=snapshot.status,
            snapshot=updated,
        )
        self._fault("after_result")
        return result

    def rebind_receipt(
        self,
        operation_id: str,
        *,
        receipt: VerifiedProviderReceipt,
        actor: str,
        now_ns: int | None = None,
        evidence_ref: str | None = None,
    ) -> RecoveryResult:
        """Rebind a verified receipt under a new store-issued floor."""

        self._assert_layout()
        operation_id = _identifier(operation_id, "operation_id")
        actor = _identifier(actor, "actor")
        evidence_ref = _evidence(evidence_ref)
        supplied_values = _receipt_values(receipt)
        if supplied_values[0] != operation_id:
            raise RecoveryConflictError("receipt operation identity mismatches")
        timestamp = self._now(now_ns)
        snapshot = self._snapshot(operation_id)
        if snapshot.status != "RECEIPTED":
            raise RecoveryConflictError("operation is not receipted")
        stored_receipt = snapshot.verified_receipt_identity
        if stored_receipt is None or supplied_values != _receipt_values(stored_receipt):
            raise RecoveryConflictError("receipt identity is stale or mismatched")
        reservation = self.store._reserve_floor()
        try:
            with self.store._recovery_transaction() as transaction:
                floor = transaction.advance_floor(reservation, timestamp=timestamp)
                updated = transaction.rebase(
                    snapshot,
                    mode="RECEIPTED",
                    actor=actor,
                    timestamp=timestamp,
                    evidence_ref=evidence_ref,
                )
                updated = transaction.append_event(
                    updated,
                    kind="rebind_receipt",
                    reason_code="rebind_receipt",
                    actor=actor,
                    timestamp=timestamp,
                    evidence_ref=evidence_ref,
                )
        except _store.StoreCommitUnknownError as exc:
            raise RecoveryCommitUnknownError(str(exc)) from exc
        except (_store.StoreError, LeaseConflictError) as exc:
            raise self._conflict_if_lease(exc) from exc
        self._fault("before_result")
        result = RecoveryResult(
            action="rebind_receipt",
            operation_id=operation_id,
            from_status=snapshot.status,
            snapshot=updated,
            floor=floor,
        )
        self._fault("after_result")
        return result

    def _snapshot(self, operation_id: str) -> RecoverySnapshot:
        try:
            return self.store._recovery_snapshot(operation_id)
        except _store.StoreCommitUnknownError:
            raise
        except (_store.StoreError, LeaseConflictError) as exc:
            raise self._conflict_if_lease(exc) from exc

    def _fault(self, point: str) -> None:
        """Deterministic process-test seam; production implementation is a no-op."""

    def _unknown_effect(
        self,
        operation_id: str,
    ) -> tuple[RecoverySnapshot, ProviderEffect | None]:
        try:
            with self.store._recovery_transaction() as transaction:
                snapshot = transaction.snapshot(operation_id)
                effect = transaction.recovery_effect(operation_id)
            return snapshot, effect
        except _store.StoreCommitUnknownError:
            raise
        except (_store.StoreError, LeaseConflictError) as exc:
            raise self._conflict_if_lease(exc) from exc

    def _now(self, now_ns: int | None) -> int:
        candidate = self.__clock() if now_ns is None else now_ns
        return _timestamp(candidate, "clock_ns")

    def _require_status_preflight(self, operation_id: str) -> None:
        report = self.startup_preflight(operation_id)
        if report.observed_state in {
            "MISSING_ROOT",
            "MISSING",
            "EMPTY_ROOT",
            "WRITER_ACTIVE",
            "WAL_PENDING",
            "RESTORE_INCOMPLETE",
            "UNSAFE_SIDECAR",
            "SCHEMA_INVALID",
            "UNREADABLE",
        }:
            raise RecoveryRequiredError(
                "provider status cannot be trusted for the observed state"
            )

    @staticmethod
    def _require_lease_identity(
        snapshot: RecoverySnapshot,
        *,
        owner: str,
        provider_id: str,
        effect_key: str,
    ) -> None:
        if (
            snapshot.owner != owner
            or snapshot.provider_id != provider_id
            or snapshot.effect_key != effect_key
            or snapshot.recovery_epoch != snapshot.lease_epoch
        ):
            raise RecoveryConflictError("lease identity is stale or mismatched")

    def _require_current_epoch(self, snapshot: RecoverySnapshot) -> None:
        try:
            current = self.store._current_recovery_epoch()
        except _store.StoreError as exc:
            raise RecoveryRequiredError(
                "current recovery epoch cannot be verified"
            ) from exc
        if (
            snapshot.recovery_epoch != snapshot.lease_epoch
            or snapshot.recovery_epoch != current
        ):
            raise RecoveryRequiredError("operation lease epoch is stale")

    @staticmethod
    def _require_status_provider(provider: object) -> None:
        if any(
            not callable(getattr(provider, method, None))
            for method in ("reserve_fence", "execute", "status")
        ):
            raise RecoveryRequiredError("trusted provider port is incomplete")
        capabilities = getattr(provider, "capabilities", None)
        if type(capabilities) is not ProviderCapabilities:
            raise RecoveryRequiredError(
                "provider capabilities have an unsupported type"
            )
        try:
            capability_values = tuple(
                object.__getattribute__(capabilities, name)
                for name in ("idempotency", "fencing", "strong_status")
            )
        except AttributeError as exc:
            raise RecoveryRequiredError("provider capabilities are incomplete") from exc
        if any(type(value) is not bool for value in capability_values):
            raise RecoveryRequiredError("provider capabilities are invalid")
        try:
            require_provider_capabilities(provider)
        except (ProviderBlockedError, TypeError, ValueError) as exc:
            raise RecoveryRequiredError(
                "provider lacks required effect capabilities"
            ) from exc

    @staticmethod
    def _validate_current_status(effect: ProviderEffect, status: object) -> None:
        if type(status) is not ProviderStatus:
            raise RecoveryRequiredError("provider returned an unsupported status type")
        try:
            values = tuple(
                object.__getattribute__(status, name)
                for name in (
                    "operation_id",
                    "effect_key",
                    "provider_id",
                    "owner",
                    "attempt",
                    "lease_epoch",
                    "fencing_token",
                    "provider_effect_id",
                    "status",
                    "consistency",
                    "proof_version",
                    "proof_ref",
                )
            )
            validated = ProviderStatus(
                operation_id=cast(str, values[0]),
                effect_key=cast(str, values[1]),
                provider_id=cast(str, values[2]),
                owner=cast(str, values[3]),
                attempt=cast(int, values[4]),
                lease_epoch=cast(int, values[5]),
                fencing_token=cast(int, values[6]),
                provider_effect_id=cast(str | None, values[7]),
                status=cast(Literal["ABSENT", "COMPLETED", "UNKNOWN"], values[8]),
                consistency=cast(Literal["STRONG", "UNKNOWN"], values[9]),
                proof_version=cast(int | None, values[10]),
                proof_ref=cast(str | None, values[11]),
            )
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise RecoveryRequiredError("provider status fields are invalid") from exc
        proof = effect.fence_proof
        if proof is None:
            raise RecoveryRequiredError("provider effect proof is unavailable")
        if validated.consistency != "STRONG":
            raise RecoveryRequiredError("provider status is not strongly consistent")
        expected = (
            effect.operation_id,
            effect.effect_key,
            effect.provider_id,
            effect.owner,
            effect.attempt,
            effect.lease_epoch,
            effect.fencing_token,
        )
        actual = (
            validated.operation_id,
            validated.effect_key,
            validated.provider_id,
            validated.owner,
            validated.attempt,
            validated.lease_epoch,
            validated.fencing_token,
        )
        if actual != expected:
            raise RecoveryRequiredError("provider status identity mismatches effect")
        if (validated.proof_version, validated.proof_ref) != (
            proof.proof_version,
            proof.proof_ref,
        ):
            raise RecoveryRequiredError("provider status proof mismatches effect")

    @staticmethod
    def _require_snapshot_match(
        expected: RecoverySnapshot,
        actual: RecoverySnapshot,
    ) -> None:
        if expected != actual:
            raise RecoveryConflictError("recovery snapshot is stale")

    @staticmethod
    def _conflict_if_lease(
        error: _store.StoreError | LeaseConflictError,
    ) -> _store.StoreError | LeaseConflictError:
        if isinstance(error, _store.StoreCommitUnknownError):
            return RecoveryCommitUnknownError(str(error))
        if isinstance(error, LeaseConflictError):
            return RecoveryConflictError(str(error))
        return error


def _audit_evidence(audit_ref: str) -> str:
    digest = hashlib.sha256(("audit:" + audit_ref).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


__all__ = [
    "FORCE_REASON_CODES",
    "MAX_LEDGER_BYTES",
    "RECOVERY_LEDGER_BASENAME",
    "RECOVERY_LEDGER_VERSION",
    "WRITER_MARKER_BASENAME",
    "ForceReasonCode",
    "RecoveryAction",
    "RecoveryAuthorization",
    "RecoveryAuthorizationError",
    "RecoveryAuthorizer",
    "RecoveryCommitUnknownError",
    "RecoveryConflictError",
    "RecoveryCoordinator",
    "RecoveryError",
    "RecoveryLayout",
    "RecoveryLedger",
    "RecoveryLedgerError",
    "RecoveryLedgerInitialization",
    "RecoveryLedgerRecord",
    "RecoveryLedgerWriter",
    "RecoveryRequiredError",
    "RecoveryResult",
]
