"""Explicit, fail-closed recovery operations for the local coordination store.

Recovery is deliberately separate from the normal lease/provider API.  This
module never executes a provider effect and never infers an absent effect from
local state.  The SQLite mutation authority remains the private typed
``_RecoveryStoreTx`` seam in :mod:`agent_team.store`; the JSONL ledger is an
independent owner-only append log reserved for restore phases consumed by
``agent_team.doctor``.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import (
    Final,
    Literal,
    NoReturn,
    Protocol,
    Self,
    SupportsIndex,
    TypeVar,
    cast,
)

from . import doctor as _doctor
from . import store as _store
from .doctor import (
    DoctorReport,
    FilesetInventory,
    LedgerPhase,
    LedgerSnapshot,
    StateFilesystem,
)
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
    RestoreIdentity,
    VerifiedProviderReceipt,
    _verified_receipt_from_status,
    require_provider_capabilities,
)
from .wal import (
    _CLEANUP_EXCEPTION,
    QuiescenceOwner,
    QuiescenceSession,
    WalSidecarController,
)

_QUIESCENCE_OWNER_TYPE = QuiescenceOwner
_T = TypeVar("_T")
_RetainFD = Callable[[int, tuple[int, int] | None, str], None]
_MAX_ORPHAN_FDS: Final[int] = 8
_MAX_ORPHAN_CONTROLLERS: Final[int] = 8
_MAX_ORPHAN_RESOURCES: Final[int] = 8


class _CleanupCapability(Protocol):
    def retry_cleanup(self) -> None:
        """Retry one opaque cleanup operation."""


class _OwnerRetainAdapter:
    """Opaque owner bridge for descriptor handoff and session cleanup."""

    __slots__ = ("_retain", "_retry")

    def __init__(
        self,
        retain: _RetainFD,
        retry: Callable[[], None],
    ) -> None:
        self._retain = retain
        self._retry = retry

    def __call__(
        self,
        fd: int,
        expected_identity: tuple[int, int] | None,
        label: str,
    ) -> None:
        self._retain(fd, expected_identity, label)

    def retry_cleanup(self) -> None:
        self._retry()


class _RegistryCleanupCapability:
    """Opaque retry capability for one bounded recovery registry."""

    __slots__ = ("_retry",)

    def __init__(self, retry: Callable[[], None]) -> None:
        self._retry = retry

    def retry_cleanup(self) -> None:
        self._retry()


class _CompositeCleanupCapability:
    """Opaque capability that retries several retained resources."""

    __slots__ = ("_owners",)

    def __init__(self, first: _CleanupCapability, second: _CleanupCapability) -> None:
        self._owners: list[_CleanupCapability] = [first, second]

    def add(self, owner: _CleanupCapability) -> None:
        if not any(existing is owner for existing in self._owners):
            self._owners.append(owner)

    def retry_cleanup(self) -> None:
        remaining: list[_CleanupCapability] = []
        first_error: BaseException | None = None
        for owner in self._owners:
            try:
                owner.retry_cleanup()
            except _CLEANUP_EXCEPTION as exc:
                remaining.append(owner)
                if first_error is None:
                    first_error = exc
        self._owners = remaining
        if first_error is not None:
            raise first_error


RECOVERY_LEDGER_VERSION: Final[int] = 1
RECOVERY_LEDGER_BASENAME: Final[str] = "recovery.ledger"
TOMBSTONE_LOG_VERSION: Final[int] = 1
RECOVERY_TOMBSTONES_VERSION: Final[int] = TOMBSTONE_LOG_VERSION
RECOVERY_TOMBSTONES_BASENAME: Final[str] = "recovery.tombstones"
WRITER_MARKER_BASENAME: Final[str] = _store.WRITER_MARKER_FILENAME
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
    "RESTORE_REPLACED": frozenset({"RESTORE_COMMITTED"}),
}
TombstonePhase = Literal["PREPARED", "COMMITTED", "ABORTED"]
_TOMBSTONE_PHASES: Final[tuple[TombstonePhase, ...]] = (
    "PREPARED",
    "COMMITTED",
    "ABORTED",
)
# These are the only ledger/tombstone states observable within one generation.
# PREPARED+PREPARED is the normal pending state; PREPARED+ABORTED is the
# abort response-loss state. REPLACED+PREPARED is the normal replace state;
# REPLACED+COMMITTED is the commit response-loss state. Terminal states must
# agree, and every other cross-product pair is malformed.
_RESTORE_PHASE_PAIRS: Final[frozenset[tuple[LedgerPhase, TombstonePhase]]] = frozenset(
    {
        ("RESTORE_PREPARED", "PREPARED"),
        ("RESTORE_PREPARED", "ABORTED"),
        ("RESTORE_REPLACED", "PREPARED"),
        ("RESTORE_REPLACED", "COMMITTED"),
        ("RESTORE_COMMITTED", "COMMITTED"),
        ("RESTORE_ABORTED", "ABORTED"),
    }
)
_OrphanFD = tuple[int, tuple[int, int] | None, str]


def _require_restore_phase_pair(
    ledger_phase: LedgerPhase,
    tombstone_phase: TombstonePhase,
) -> None:
    if (ledger_phase, tombstone_phase) not in _RESTORE_PHASE_PAIRS:
        raise RecoveryLedgerError("restore record phases do not match")


@dataclass(frozen=True, slots=True)
class RecoveryLayout:
    """Immutable layout identity shared by every coordinator entry point."""

    marker_name: str
    ledger_name: str = RECOVERY_LEDGER_BASENAME

    def __post_init__(self) -> None:
        if type(self.marker_name) is not str:
            raise TypeError("marker_name is invalid")
        _doctor._require_basename(self.marker_name, "marker_name")
        if self.marker_name != WRITER_MARKER_BASENAME:
            raise ValueError("marker_name is not canonical")
        if type(self.ledger_name) is not str:
            raise TypeError("ledger_name is invalid")
        if self.ledger_name != RECOVERY_LEDGER_BASENAME:
            raise ValueError("ledger_name is not canonical")
        if self.marker_name in {
            self.ledger_name,
            RECOVERY_TOMBSTONES_BASENAME,
        }:
            raise ValueError("marker and recovery basenames must differ")


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

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self._cleanup_owner: _CleanupCapability | None = None

    @property
    def cleanup_owner(self) -> _CleanupCapability | None:
        """Opaque cleanup capability retained by a failed filesystem open."""

        return self._cleanup_owner

    def _set_cleanup_owner(self, owner: _CleanupCapability) -> None:
        if self._cleanup_owner is None:
            self._cleanup_owner = owner
        elif self._cleanup_owner is not owner:
            existing = self._cleanup_owner
            if isinstance(existing, _CompositeCleanupCapability):
                existing.add(owner)
            else:
                self._cleanup_owner = _CompositeCleanupCapability(existing, owner)

    def retry_cleanup(self) -> None:
        """Retry cleanup without exposing the underlying filesystem."""

        owner = self._cleanup_owner
        if owner is None:
            return
        try:
            owner.retry_cleanup()
        except _CLEANUP_EXCEPTION as retry_error:
            if isinstance(retry_error, RecoveryLedgerError):
                retry_error._cleanup_owner = owner
                raise
            if isinstance(retry_error, _doctor.DoctorError) and isinstance(
                owner, _doctor.CleanupOwner
            ):
                retry_error._cleanup_capability = owner
                raise
            wrapped = RecoveryLedgerError("recovery cleanup retry failed")
            wrapped._set_cleanup_owner(owner)
            raise wrapped from retry_error
        self._cleanup_owner = None


class RecoveryDurabilityError(RecoveryLedgerError):
    """Existing recovery bytes lack a fresh file/root durability proof."""


def _error_with_cleanup_owner(
    error: BaseException,
    owner: _CleanupCapability,
    message: str,
) -> BaseException:
    if isinstance(error, RecoveryLedgerError):
        error._set_cleanup_owner(owner)
        return error
    if isinstance(error, _doctor.DoctorError) and isinstance(
        owner, _doctor.CleanupOwner
    ):
        existing = error.cleanup_owner
        if existing is None:
            error._set_cleanup_owner(owner)
            return error
        wrapped = RecoveryLedgerError(message)
        wrapped.__cause__ = error
        wrapped._set_cleanup_owner(existing)
        wrapped._set_cleanup_owner(owner)
        return wrapped
    wrapped = RecoveryLedgerError(message)
    wrapped._set_cleanup_owner(owner)
    wrapped.__cause__ = error
    return wrapped


def _error_cleanup_owner(error: BaseException) -> _CleanupCapability | None:
    if isinstance(error, RecoveryLedgerError):
        return error.cleanup_owner
    if isinstance(error, _doctor.DoctorError):
        return error.cleanup_owner
    try:
        foreign_owner = _store._extract_cleanup_capability(error)
    except _CLEANUP_EXCEPTION:
        return None
    if foreign_owner is not None:
        return _RegistryCleanupCapability(foreign_owner.retry_cleanup)
    return None


def _compose_cleanup_owners(
    first: _CleanupCapability | None,
    second: _CleanupCapability | None,
) -> _CleanupCapability | None:
    if first is None:
        return second
    if second is None or first is second:
        return first
    if isinstance(first, _CompositeCleanupCapability):
        first.add(second)
        return first
    return _CompositeCleanupCapability(first, second)


def _raise_with_cleanup(
    body_error: BaseException | None,
    cleanup_error: BaseException,
    owner: _CleanupCapability | None,
    message: str,
) -> NoReturn:
    if body_error is None:
        if owner is not None:
            cleanup_error = _error_with_cleanup_owner(
                cleanup_error,
                owner,
                message,
            )
        raise cleanup_error
    primary = body_error
    if owner is not None:
        primary = _error_with_cleanup_owner(body_error, owner, message)
    if primary is not body_error:
        primary.add_note(f"cleanup remains pending: {cleanup_error}")
        raise primary
    raise primary from cleanup_error


def _recovery_filesystem_error(
    message: str,
    cause: BaseException,
) -> RecoveryLedgerError:
    error = RecoveryLedgerError(message)
    if isinstance(cause, _doctor.DoctorError):
        owner = cause.cleanup_owner
        if owner is not None:
            error._set_cleanup_owner(owner)
    return error


def _identifier(value: object, name: str) -> str:
    try:
        return _store._require_opaque_identifier(value, name)
    except (TypeError, ValueError) as exc:
        msg = f"{name} is invalid"
        raise ValueError(msg) from exc


def _timestamp(
    value: object,
    name: str = "timestamp",
    *,
    minimum: int = 0,
) -> int:
    try:
        return _store._require_sqlite_integer(value, name, minimum=minimum)
    except (TypeError, ValueError) as exc:
        msg = f"{name} is invalid"
        raise ValueError(msg) from exc


def _previous_destination_hwm(value: object, name: str) -> int:
    try:
        return _store._require_sqlite_integer(value, name, minimum=0)
    except (TypeError, ValueError) as exc:
        raise RecoveryLedgerError(f"{name} is invalid") from exc


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
            (self.recovery_epoch, "recovery_epoch"),
            (self.fencing_token_floor, "fencing_token_floor"),
        ):
            _timestamp(value, name)
        _timestamp(self.restore_generation, "restore_generation", minimum=1)
        _digest(self.backup_digest)
        _identifier(self.actor, "actor")
        _identifier(self.audit_ref, "audit_ref")


@dataclass(frozen=True, slots=True)
class RecoveryTombstoneRecord:
    """One canonical batch record in the companion identity tombstone log."""

    version: int
    sequence: int
    phase: TombstonePhase
    restore_generation: int
    backup_digest: str
    previous_primary_digest: str
    candidate_digest: str
    previous_recovery_epoch: int
    previous_fencing_token_hwm: int
    previous_last_clock_ns: int
    identities: tuple[RestoreIdentity, ...]
    actor: str
    audit_ref: str

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != TOMBSTONE_LOG_VERSION:
            raise ValueError("tombstone log version is unsupported")
        _store._require_sqlite_integer(self.sequence, "sequence", minimum=1)
        if type(self.phase) is not str or self.phase not in _TOMBSTONE_PHASES:
            raise ValueError("tombstone phase is unsupported")
        _store._require_sqlite_integer(
            self.restore_generation,
            "restore_generation",
            minimum=1,
        )
        _digest(self.backup_digest, "backup_digest")
        _digest(self.previous_primary_digest, "previous_primary_digest")
        _digest(self.candidate_digest, "candidate_digest")
        for value, name in (
            (self.previous_recovery_epoch, "previous_recovery_epoch"),
            (self.previous_fencing_token_hwm, "previous_fencing_token_hwm"),
            (self.previous_last_clock_ns, "previous_last_clock_ns"),
        ):
            _store._require_sqlite_integer(value, name, minimum=0)
        if type(self.identities) is not tuple:
            raise TypeError("identities must be a tuple")
        values: list[tuple[str, str]] = []
        for identity in self.identities:
            if type(identity) is not RestoreIdentity:
                raise TypeError("identities must contain exact RestoreIdentity values")
            operation_id, effect_key = _restore_identity_values(identity)
            values.append((operation_id, effect_key))
        if values != sorted(set(values)):
            raise ValueError("tombstone identities must be sorted and unique")
        _identifier(self.actor, "actor")
        _identifier(self.audit_ref, "audit_ref")


_TOMBSTONE_RECORD_FIELDS: Final[tuple[str, ...]] = (
    "version",
    "sequence",
    "phase",
    "restore_generation",
    "backup_digest",
    "previous_primary_digest",
    "candidate_digest",
    "previous_recovery_epoch",
    "previous_fencing_token_hwm",
    "previous_last_clock_ns",
    "identities",
    "actor",
    "audit_ref",
)
_TOMBSTONE_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "operation_id",
    "effect_key",
)


def _restore_identity_values(identity: object) -> tuple[str, str]:
    if type(identity) is not RestoreIdentity:
        raise TypeError("identity must be an exact RestoreIdentity")
    try:
        operation_id = object.__getattribute__(identity, "operation_id")
        effect_key = object.__getattribute__(identity, "effect_key")
    except AttributeError as exc:
        raise ValueError("restore identity fields are unavailable") from exc
    if type(operation_id) is not str or type(effect_key) is not str:
        raise ValueError("restore identity fields must be exact strings")
    return (
        _identifier(operation_id, "operation_id"),
        _identifier(effect_key, "effect_key"),
    )


def _tombstone_values(record: object) -> tuple[object, ...]:
    if type(record) is not RecoveryTombstoneRecord:
        raise TypeError("record must be an exact RecoveryTombstoneRecord")
    try:
        return tuple(
            object.__getattribute__(record, name) for name in _TOMBSTONE_RECORD_FIELDS
        )
    except AttributeError as exc:
        raise ValueError("tombstone record fields are unavailable") from exc


def _canonical_tombstone(record: object) -> RecoveryTombstoneRecord:
    values = _tombstone_values(record)
    try:
        return RecoveryTombstoneRecord(
            version=cast(int, values[0]),
            sequence=cast(int, values[1]),
            phase=cast(TombstonePhase, values[2]),
            restore_generation=cast(int, values[3]),
            backup_digest=cast(str, values[4]),
            previous_primary_digest=cast(str, values[5]),
            candidate_digest=cast(str, values[6]),
            previous_recovery_epoch=cast(int, values[7]),
            previous_fencing_token_hwm=cast(int, values[8]),
            previous_last_clock_ns=cast(int, values[9]),
            identities=cast(tuple[RestoreIdentity, ...], values[10]),
            actor=cast(str, values[11]),
            audit_ref=cast(str, values[12]),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecoveryLedgerError("tombstone record is invalid") from exc


def _tombstone_mapping(record: RecoveryTombstoneRecord) -> dict[str, object]:
    values = _tombstone_values(record)
    identities = cast(tuple[RestoreIdentity, ...], values[10])
    return {
        "version": values[0],
        "sequence": values[1],
        "phase": values[2],
        "restore_generation": values[3],
        "backup_digest": values[4],
        "previous_primary_digest": values[5],
        "candidate_digest": values[6],
        "previous_recovery_epoch": values[7],
        "previous_fencing_token_hwm": values[8],
        "previous_last_clock_ns": values[9],
        "identities": [
            {
                "operation_id": identity.operation_id,
                "effect_key": identity.effect_key,
            }
            for identity in identities
        ],
        "actor": values[11],
        "audit_ref": values[12],
    }


def _encode_tombstone(record: RecoveryTombstoneRecord) -> bytes:
    return (
        json.dumps(
            _tombstone_mapping(record),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _tombstone_from_mapping(item: object) -> RecoveryTombstoneRecord:
    if type(item) is not dict or set(item) != set(_TOMBSTONE_RECORD_FIELDS):
        raise RecoveryLedgerError("tombstone record fields are invalid")
    identities_item = item["identities"]
    if type(identities_item) is not list:
        raise RecoveryLedgerError("tombstone identities are invalid")
    identities: list[RestoreIdentity] = []
    for identity_item in identities_item:
        if type(identity_item) is not dict or set(identity_item) != set(
            _TOMBSTONE_IDENTITY_FIELDS
        ):
            raise RecoveryLedgerError("tombstone identity fields are invalid")
        try:
            identities.append(
                RestoreIdentity(
                    operation_id=cast(str, identity_item["operation_id"]),
                    effect_key=cast(str, identity_item["effect_key"]),
                )
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise RecoveryLedgerError("tombstone identity is invalid") from exc
    try:
        record = RecoveryTombstoneRecord(
            version=cast(int, item["version"]),
            sequence=cast(int, item["sequence"]),
            phase=cast(TombstonePhase, item["phase"]),
            restore_generation=cast(int, item["restore_generation"]),
            backup_digest=cast(str, item["backup_digest"]),
            previous_primary_digest=cast(str, item["previous_primary_digest"]),
            candidate_digest=cast(str, item["candidate_digest"]),
            previous_recovery_epoch=cast(int, item["previous_recovery_epoch"]),
            previous_fencing_token_hwm=cast(int, item["previous_fencing_token_hwm"]),
            previous_last_clock_ns=cast(int, item["previous_last_clock_ns"]),
            identities=tuple(identities),
            actor=cast(str, item["actor"]),
            audit_ref=cast(str, item["audit_ref"]),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecoveryLedgerError("tombstone record is invalid") from exc
    if _encode_tombstone(record) != _canonical_json_record(item):
        raise RecoveryLedgerError("tombstone record is not canonical")
    return record


def _canonical_json_record(item: dict[str, object]) -> bytes:
    return (
        json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _tombstone_records(raw: bytes) -> tuple[RecoveryTombstoneRecord, ...]:
    if not raw or len(raw) > MAX_LEDGER_BYTES:
        raise RecoveryLedgerError("tombstone log is empty or too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecoveryLedgerError("tombstone log is not UTF-8") from exc
    if "\r" in text or not text.endswith("\n"):
        raise RecoveryLedgerError("tombstone log has a non-canonical newline boundary")
    records: list[RecoveryTombstoneRecord] = []
    for line in text.split("\n")[:-1]:
        if not line or line.strip() != line:
            raise RecoveryLedgerError("tombstone log contains a blank or padded record")
        try:
            item = json.loads(line, object_pairs_hook=_doctor._json_object_pairs)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RecoveryLedgerError(
                "tombstone log contains a partial record"
            ) from exc
        records.append(_tombstone_from_mapping(item))
    if not records:
        raise RecoveryLedgerError("tombstone log has no records")
    return tuple(records)


def _same_tombstone(
    left: RecoveryTombstoneRecord | None,
    right: RecoveryTombstoneRecord | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return _tombstone_values(left) == _tombstone_values(right)


def _latest_tombstone(
    raw: bytes,
    *,
    allow_empty: bool,
) -> RecoveryTombstoneRecord | None:
    if not raw and allow_empty:
        return None
    records = _tombstone_records(raw)
    previous: RecoveryTombstoneRecord | None = None
    for record in records:
        if previous is None:
            if (
                record.sequence != 1
                or record.phase != "PREPARED"
                or record.restore_generation != 1
            ):
                raise RecoveryLedgerError(
                    "tombstone log must start prepared sequence one generation one"
                )
        else:
            if record.sequence != previous.sequence + 1:
                raise RecoveryLedgerError("tombstone sequence is not monotonic")
            if previous.phase == "PREPARED":
                if record.phase not in {"COMMITTED", "ABORTED"}:
                    raise RecoveryLedgerError("tombstone phase transition is invalid")
                if record.restore_generation != previous.restore_generation:
                    raise RecoveryLedgerError(
                        "tombstone generation changed before terminal"
                    )
            else:
                if (
                    record.phase != "PREPARED"
                    or record.restore_generation != previous.restore_generation + 1
                ):
                    raise RecoveryLedgerError(
                        "tombstone terminal transition is invalid"
                    )
            if record.restore_generation == previous.restore_generation and (
                record.backup_digest != previous.backup_digest
                or record.previous_primary_digest != previous.previous_primary_digest
                or record.candidate_digest != previous.candidate_digest
                or record.previous_recovery_epoch != previous.previous_recovery_epoch
                or record.previous_fencing_token_hwm
                != previous.previous_fencing_token_hwm
                or record.previous_last_clock_ns != previous.previous_last_clock_ns
                or _restore_identity_values_tuple(record.identities)
                != _restore_identity_values_tuple(previous.identities)
                or record.actor != previous.actor
                or record.audit_ref != previous.audit_ref
            ):
                raise RecoveryLedgerError("tombstone batch changed in one generation")
        previous = record
    return previous


def _restore_identity_values_tuple(
    identities: tuple[RestoreIdentity, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple(_restore_identity_values(identity) for identity in identities)


def _validate_tombstone_append(
    record: RecoveryTombstoneRecord,
    latest: RecoveryTombstoneRecord | None,
) -> None:
    if latest is None:
        if (
            record.sequence != 1
            or record.phase != "PREPARED"
            or record.restore_generation != 1
        ):
            raise RecoveryLedgerError(
                "tombstone log must start prepared sequence one generation one"
            )
        return
    if record.sequence != latest.sequence + 1:
        raise RecoveryLedgerError("tombstone sequence is not monotonic")
    if latest.phase == "PREPARED":
        if record.phase not in {"COMMITTED", "ABORTED"}:
            raise RecoveryLedgerError("tombstone phase transition is invalid")
        if record.restore_generation != latest.restore_generation:
            raise RecoveryLedgerError("tombstone generation changed before terminal")
    else:
        if (
            record.phase != "PREPARED"
            or record.restore_generation != latest.restore_generation + 1
        ):
            raise RecoveryLedgerError("tombstone terminal transition is invalid")
    if record.restore_generation == latest.restore_generation and (
        record.backup_digest != latest.backup_digest
        or record.previous_primary_digest != latest.previous_primary_digest
        or record.candidate_digest != latest.candidate_digest
        or record.previous_recovery_epoch != latest.previous_recovery_epoch
        or record.previous_fencing_token_hwm != latest.previous_fencing_token_hwm
        or record.previous_last_clock_ns != latest.previous_last_clock_ns
        or _restore_identity_values_tuple(record.identities)
        != _restore_identity_values_tuple(latest.identities)
        or record.actor != latest.actor
        or record.audit_ref != latest.audit_ref
    ):
        raise RecoveryLedgerError("tombstone batch changed in one generation")


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
    if request_digest != record.backup_digest:
        raise RecoveryLedgerError("ledger initialization digest mismatches record")
    if (
        record.sequence != 1
        or record.phase != "RESTORE_PREPARED"
        or record.restore_generation != 1
    ):
        raise RecoveryLedgerError(
            "ledger initialization must start sequence one prepared generation one"
        )


def _validate_tombstone_initialization(
    authority: object,
    record: RecoveryTombstoneRecord,
) -> None:
    if type(authority) is not RecoveryLedgerInitialization:
        raise RecoveryLedgerError("tombstone initialization authority is invalid")
    try:
        issued = object.__getattribute__(authority, "_provenance")
        operator_id = object.__getattribute__(authority, "operator_id")
        audit_ref = object.__getattribute__(authority, "audit_ref")
        request_digest = object.__getattribute__(authority, "request_digest")
    except AttributeError as exc:
        raise RecoveryLedgerError(
            "tombstone initialization authority is invalid"
        ) from exc
    if issued is not _LEDGER_INITIALIZATION_SENTINEL:
        raise RecoveryLedgerError("tombstone initialization authority is unissued")
    _identifier(operator_id, "operator_id")
    _identifier(audit_ref, "audit_ref")
    _digest(request_digest, "request_digest")
    if operator_id != record.actor or audit_ref != record.audit_ref:
        raise RecoveryLedgerError("tombstone initialization identity mismatches record")
    if request_digest != record.backup_digest:
        raise RecoveryLedgerError("tombstone initialization digest mismatches record")
    if (
        record.sequence != 1
        or record.phase != "PREPARED"
        or record.restore_generation != 1
    ):
        raise RecoveryLedgerError(
            "tombstone initialization must start sequence one prepared generation one"
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


def _validate_private_regular(metadata: os.stat_result, label: str) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RecoveryLedgerError(f"{label} is unsafe")


def _validate_root_descriptor(root_fd: int, state_root: Path) -> tuple[int, int]:
    if type(root_fd) is not int:
        raise RecoveryLedgerError("borrowed root descriptor is invalid")
    try:
        descriptor = os.fstat(root_fd)
        path = os.stat(state_root, follow_symlinks=False)
    except OSError as exc:
        raise RecoveryLedgerError("borrowed root identity is unavailable") from exc
    if (
        not stat.S_ISDIR(descriptor.st_mode)
        or descriptor.st_uid != os.getuid()
        or stat.S_IMODE(descriptor.st_mode) != 0o700
        or (descriptor.st_dev, descriptor.st_ino) != (path.st_dev, path.st_ino)
        or path.st_uid != os.getuid()
        or stat.S_IMODE(path.st_mode) != 0o700
    ):
        raise RecoveryLedgerError("borrowed root identity is unsafe")
    return descriptor.st_dev, descriptor.st_ino


def _remember_orphan_fd(
    registry: list[_OrphanFD] | None,
    retain_fd: _RetainFD | None,
    fd: int,
    expected_identity: tuple[int, int] | None,
    label: str,
) -> _CleanupCapability | None:
    """Retain a close-uncertain descriptor without ever closing by number."""

    actual_identity = expected_identity
    try:
        metadata = os.fstat(fd)
    except _CLEANUP_EXCEPTION as exc:
        if isinstance(exc, OSError) and exc.errno == errno.EBADF:
            return None
    else:
        actual_identity = _store._identity(metadata)
        if expected_identity is not None and actual_identity != expected_identity:
            raise RecoveryLedgerError(f"{label} descriptor was reused")

    if retain_fd is not None:
        retain_fd(fd, actual_identity, label)
        if isinstance(retain_fd, _OwnerRetainAdapter):
            return retain_fd
        return None

    pending = (fd, actual_identity, label)
    if registry is None:
        return _RegistryCleanupCapability(
            cast(
                Callable[[], None],
                lambda _pending=[pending]: _retry_orphan_fds(_pending),
            )
        )
    if any(existing_fd == fd for existing_fd, _, _ in registry):
        return _RegistryCleanupCapability(
            cast(
                Callable[[], None],
                lambda _registry=registry: _retry_orphan_fds(_registry),
            )
        )
    if len(registry) >= _MAX_ORPHAN_FDS:
        current_owner = _RegistryCleanupCapability(
            cast(
                Callable[[], None],
                lambda _pending=[pending]: _retry_orphan_fds(_pending),
            )
        )
        existing_owner = _RegistryCleanupCapability(
            cast(
                Callable[[], None],
                lambda _registry=registry: _retry_orphan_fds(_registry),
            )
        )
        combined = _compose_cleanup_owners(existing_owner, current_owner)
        assert combined is not None
        return combined
    registry.append(pending)
    return _RegistryCleanupCapability(
        cast(
            Callable[[], None],
            lambda _registry=registry: _retry_orphan_fds(_registry),
        )
    )


def _retain_failed_fd_with_fallback(
    registry: list[_OrphanFD] | None,
    retain_fd: _RetainFD | None,
    fd: int,
    expected_identity: tuple[int, int] | None,
    label: str,
) -> _CleanupCapability | None:
    """Handoff a descriptor while retaining a Recovery-owned fallback."""

    if retain_fd is None:
        return _remember_orphan_fd(
            registry,
            None,
            fd,
            expected_identity,
            label,
        )
    try:
        retain_fd(fd, expected_identity, label)
    except _CLEANUP_EXCEPTION as exc:
        fallback_registry = registry if registry is not None else []
        fallback_owner: _CleanupCapability | None = None
        fallback_error: BaseException | None = None
        try:
            fallback_owner = _remember_orphan_fd(
                fallback_registry,
                None,
                fd,
                expected_identity,
                label,
            )
        except _CLEANUP_EXCEPTION as fallback_exc:
            fallback_error = fallback_exc
            fallback_owner = _error_cleanup_owner(fallback_exc)
        owner = _compose_cleanup_owners(
            _error_cleanup_owner(exc),
            fallback_owner,
        )
        if fallback_error is not None:
            owner = _compose_cleanup_owners(
                owner,
                _error_cleanup_owner(fallback_error),
            )
        if owner is None:
            wrapped = RecoveryLedgerError("recovery descriptor handoff failed")
            wrapped.__cause__ = exc
            raise wrapped
        attached = _error_with_cleanup_owner(
            exc,
            owner,
            "recovery descriptor handoff failed",
        )
        if fallback_error is not None and attached is not exc:
            attached.add_note(f"fallback descriptor cleanup failed: {fallback_error}")
        if attached is exc:
            raise attached
        raise attached from exc
    if isinstance(retain_fd, _OwnerRetainAdapter):
        return retain_fd
    return None


def _retry_orphan_fds(registry: list[_OrphanFD]) -> None:
    """Close retained descriptors after a fresh identity check."""

    remaining: list[_OrphanFD] = []
    first_error: BaseException | None = None
    for fd, expected_identity, label in registry:
        try:
            metadata = os.fstat(fd)
        except _CLEANUP_EXCEPTION as exc:
            if isinstance(exc, OSError) and exc.errno == errno.EBADF:
                continue
            remaining.append((fd, expected_identity, label))
            if first_error is None:
                first_error = RecoveryLedgerError(
                    f"{label} descriptor status is unknown"
                )
            continue
        if expected_identity is None:
            remaining.append((fd, None, label))
            if first_error is None:
                first_error = RecoveryLedgerError(
                    f"{label} descriptor identity is unavailable"
                )
            continue
        if _store._identity(metadata) != expected_identity:
            if first_error is None:
                first_error = RecoveryLedgerError(f"{label} descriptor was reused")
            continue
        try:
            os.close(fd)
        except _CLEANUP_EXCEPTION as exc:
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
                    first_error = RecoveryLedgerError(
                        f"{label} descriptor status is unknown"
                    )
                continue
            if _store._identity(retry_metadata) != expected_identity:
                if first_error is None:
                    first_error = RecoveryLedgerError(f"{label} descriptor was reused")
                continue
            remaining.append((fd, expected_identity, label))
            if first_error is None:
                first_error = RecoveryLedgerError(f"{label} cannot be closed")
            del exc
    registry[:] = remaining
    if first_error is not None:
        cleanup_error = RecoveryLedgerError("recovery descriptor cleanup is uncertain")
        cleanup_error._set_cleanup_owner(
            _RegistryCleanupCapability(
                cast(
                    Callable[[], None],
                    lambda _registry=registry: _retry_orphan_fds(_registry),
                )
            )
        )
        raise cleanup_error from first_error


def _retry_orphan_filesystems(registry: list[StateFilesystem]) -> None:
    """Retry closing unowned filesystem resources retained after uncertainty."""

    remaining: list[StateFilesystem] = []
    first_error: BaseException | None = None
    for filesystem in registry:
        try:
            filesystem.close()
        except _CLEANUP_EXCEPTION as exc:
            remaining.append(filesystem)
            if first_error is None:
                first_error = exc
    registry[:] = remaining
    if first_error is not None:
        cleanup_error = RecoveryLedgerError(
            "recovery filesystem resources cannot be closed"
        )
        cleanup_error._set_cleanup_owner(
            _RegistryCleanupCapability(
                cast(
                    Callable[[], None],
                    lambda _registry=registry: _retry_orphan_filesystems(_registry),
                )
            )
        )
        raise cleanup_error from first_error


def _remember_orphan_controller(
    registry: list[WalSidecarController],
    controller: WalSidecarController,
) -> _CleanupCapability:
    if any(existing is controller for existing in registry):
        return _RegistryCleanupCapability(
            cast(
                Callable[[], None],
                lambda _registry=registry: _retry_orphan_controllers(_registry),
            )
        )
    if len(registry) >= _MAX_ORPHAN_CONTROLLERS:
        current_owner = _RegistryCleanupCapability(controller.close)
        existing_owner = _RegistryCleanupCapability(
            cast(
                Callable[[], None],
                lambda _registry=registry: _retry_orphan_controllers(_registry),
            )
        )
        combined = _compose_cleanup_owners(existing_owner, current_owner)
        assert combined is not None
        return combined
    registry.append(controller)
    return _RegistryCleanupCapability(
        cast(
            Callable[[], None],
            lambda _registry=registry: _retry_orphan_controllers(_registry),
        )
    )


def _retry_orphan_controllers(
    registry: list[WalSidecarController],
) -> None:
    """Retry cleanup owned by controllers that never issued a session."""

    remaining: list[WalSidecarController] = []
    first_error: BaseException | None = None
    for controller in registry:
        try:
            controller.close()
        except _CLEANUP_EXCEPTION as exc:
            remaining.append(controller)
            if first_error is None:
                first_error = exc
    registry[:] = remaining
    if first_error is not None:
        cleanup_error = RecoveryLedgerError(
            "recovery controller resources cannot be closed"
        )
        cleanup_error._set_cleanup_owner(
            _RegistryCleanupCapability(
                cast(
                    Callable[[], None],
                    lambda _registry=registry: _retry_orphan_controllers(_registry),
                )
            )
        )
        raise cleanup_error from first_error


def _remember_orphan_filesystem(
    registry: list[StateFilesystem],
    filesystem: StateFilesystem,
) -> _CleanupCapability:
    if any(existing is filesystem for existing in registry):
        return _RegistryCleanupCapability(
            cast(
                Callable[[], None],
                lambda _registry=registry: _retry_orphan_filesystems(_registry),
            )
        )
    if len(registry) >= _MAX_ORPHAN_RESOURCES:
        current_owner = _RegistryCleanupCapability(filesystem.close)
        existing_owner = _RegistryCleanupCapability(
            cast(
                Callable[[], None],
                lambda _registry=registry: _retry_orphan_filesystems(_registry),
            )
        )
        combined = _compose_cleanup_owners(existing_owner, current_owner)
        assert combined is not None
        return combined
    registry.append(filesystem)
    return _RegistryCleanupCapability(
        cast(
            Callable[[], None],
            lambda _registry=registry: _retry_orphan_filesystems(_registry),
        )
    )


def _remember_orphan_session(
    registry: list[QuiescenceSession],
    session: QuiescenceSession,
) -> _CleanupCapability:
    if any(existing is session for existing in registry):
        return _RegistryCleanupCapability(
            cast(
                Callable[[], None],
                lambda _registry=registry: _retry_orphan_sessions(_registry),
            )
        )
    if len(registry) >= _MAX_ORPHAN_RESOURCES:
        current_owner = _RegistryCleanupCapability(session.close)
        existing_owner = _RegistryCleanupCapability(
            cast(
                Callable[[], None],
                lambda _registry=registry: _retry_orphan_sessions(_registry),
            )
        )
        combined = _compose_cleanup_owners(existing_owner, current_owner)
        assert combined is not None
        return combined
    registry.append(session)
    return _RegistryCleanupCapability(
        cast(
            Callable[[], None],
            lambda _registry=registry: _retry_orphan_sessions(_registry),
        )
    )


def _owner_retain_callback(owner: object) -> _RetainFD | None:
    """Return the opaque handoff and cleanup bridge for a genuine owner."""

    if type(owner) is not _QUIESCENCE_OWNER_TYPE:
        return None
    try:
        retain_fd = object.__getattribute__(owner, "_retain_failed_fd")
    except Exception as exc:
        raise RecoveryLedgerError(
            "quiescence owner descriptor handoff is unavailable"
        ) from exc
    if not callable(retain_fd):
        raise RecoveryLedgerError("quiescence owner descriptor handoff is unavailable")
    retry_cleanup: object | None = None
    try:
        retry_cleanup = object.__getattribute__(owner, "_retry_cleanup")
    except AttributeError:
        try:
            provenance = object.__getattribute__(owner, "_provenance")()
            if type(provenance) is tuple and len(provenance) >= 2:
                session = provenance[1]
                retry_cleanup = getattr(session, "_retry_cleanup", None)
                if not callable(retry_cleanup):
                    retry_cleanup = getattr(session, "close", None)
        except _CLEANUP_EXCEPTION as exc:
            raise RecoveryLedgerError(
                "quiescence owner cleanup handoff is unavailable"
            ) from exc
    except _CLEANUP_EXCEPTION as exc:
        raise RecoveryLedgerError(
            "quiescence owner cleanup handoff is unavailable"
        ) from exc
    if not callable(retry_cleanup):
        raise RecoveryLedgerError("quiescence owner cleanup handoff is unavailable")
    return _OwnerRetainAdapter(cast(_RetainFD, retain_fd), retry_cleanup)


def _primary_is_present(state_root: Path) -> bool:
    try:
        os.stat(state_root / _store.DATABASE_FILENAME, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RecoveryLedgerError("coordination database cannot be inspected") from exc
    return True


def _lifetime_gate_is_present(state_root: Path) -> bool:
    try:
        os.stat(
            state_root.parent / _store.LIFETIME_GATE_FILENAME,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RecoveryLedgerError(
            "coordination lifetime gate cannot be inspected"
        ) from exc
    return True


def _lock_root_exclusive(root_fd: int, timeout_ms: int) -> None:
    deadline_ns = time.monotonic_ns() + timeout_ms * 1_000_000
    while True:
        try:
            fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise RecoveryLedgerError("recovery root lock is unavailable") from exc
            remaining_ns = deadline_ns - time.monotonic_ns()
            if remaining_ns <= 0:
                raise RecoveryLedgerError("recovery root lock is busy") from exc
            time.sleep(min(0.005, remaining_ns / 1_000_000_000))
        else:
            return


def _bootstrap_inventory(filesystem: StateFilesystem) -> FilesetInventory:
    root_fd = filesystem._root_fd
    if root_fd is None:
        raise RecoveryLedgerError("state root descriptor is unavailable")
    root_identity = _validate_root_descriptor(root_fd, filesystem.state_root)
    try:
        names = sorted(os.listdir(root_fd))
    except OSError as exc:
        raise RecoveryLedgerError("state root cannot be listed") from exc
    entries = []
    for name in names:
        try:
            metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError as exc:
            raise RecoveryLedgerError("state entry cannot be inspected") from exc
        entries.append(filesystem._entry(name, metadata))
    marker = next(
        (entry for entry in entries if entry.name == filesystem.marker_name),
        None,
    )
    ledger = next(
        (entry for entry in entries if entry.name == filesystem.ledger_name),
        None,
    )
    parent_fd = filesystem._parent_fd
    if parent_fd is None:
        raise RecoveryLedgerError("state parent is unavailable")
    try:
        gate_metadata = os.stat(
            _store.LIFETIME_GATE_FILENAME,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        gate_identity = None
    except OSError as exc:
        raise RecoveryLedgerError("lifetime gate cannot be inspected") from exc
    else:
        if _doctor._unsafe_regular(gate_metadata):
            raise RecoveryLedgerError("lifetime gate is unsafe")
        gate_identity = _store._identity(gate_metadata)
    return FilesetInventory(
        root_identity=root_identity,
        lifetime_gate_identity=gate_identity,
        marker_identity=None if marker is None else marker.identity,
        ledger_identity=None if ledger is None else ledger.identity,
        entries=tuple(entries),
    )


def _assert_bootstrap_transition(
    before: FilesetInventory,
    after: FilesetInventory,
    target_name: str,
    encoded_size: int,
) -> None:
    if (
        before.root_identity != after.root_identity
        or before.lifetime_gate_identity != after.lifetime_gate_identity
        or before.marker_identity != after.marker_identity
    ):
        raise RecoveryLedgerError("recovery root identity changed during append")
    before_other = tuple(entry for entry in before.entries if entry.name != target_name)
    after_other = tuple(entry for entry in after.entries if entry.name != target_name)
    if before_other != after_other:
        raise RecoveryLedgerError("recovery root entries changed during append")
    before_target = before.entry(target_name)
    after_target = after.entry(target_name)
    if after_target is None:
        raise RecoveryLedgerError("recovery file disappeared during append")
    if before_target is None:
        if after_target.size != encoded_size:
            raise RecoveryLedgerError("new recovery file size changed during append")
    elif (
        after_target.identity != before_target.identity
        or after_target.size != before_target.size + encoded_size
    ):
        raise RecoveryLedgerError("recovery file changed during append")


def _retry_orphan_sessions(registry: list[QuiescenceSession]) -> None:
    remaining: list[QuiescenceSession] = []
    first_error: BaseException | None = None
    for session in registry:
        try:
            session.close()
        except _CLEANUP_EXCEPTION as exc:
            remaining.append(session)
            if first_error is None:
                first_error = exc
    registry[:] = remaining
    if first_error is not None:
        cleanup_error = RecoveryLedgerError("recovery quiescence cannot be closed")
        cleanup_error._set_cleanup_owner(
            _RegistryCleanupCapability(
                cast(
                    Callable[[], None],
                    lambda _registry=registry: _retry_orphan_sessions(_registry),
                )
            )
        )
        raise cleanup_error from first_error


def _retry_resource_actions(actions: tuple[Callable[[], None], ...]) -> None:
    first_error: RecoveryLedgerError | None = None
    for action in actions:
        try:
            action()
        except _CLEANUP_EXCEPTION as error:
            if isinstance(error, RecoveryLedgerError):
                current = error
            else:
                current = RecoveryLedgerError("recovery cleanup failed")
                current.__cause__ = error
                owner = _error_cleanup_owner(error)
                if owner is not None:
                    current._set_cleanup_owner(owner)
            if first_error is None:
                first_error = current
            else:
                owner = current.cleanup_owner
                if owner is not None:
                    first_error._set_cleanup_owner(owner)
                if current is not first_error:
                    first_error.add_note(f"additional cleanup failure: {current}")
    if first_error is not None:
        raise first_error


def _run_unowned_quiescence(
    controller: WalSidecarController,
    state_root: Path,
    operation: Callable[[int], _T],
    orphan_sessions: list[QuiescenceSession],
    orphan_controllers: list[WalSidecarController],
) -> _T:
    try:
        session = controller.hold_quiescence()
    except _CLEANUP_EXCEPTION as acquisition_error:
        controller_owner: _CleanupCapability | None = None
        try:
            controller.close()
        except _CLEANUP_EXCEPTION:
            controller_owner = _remember_orphan_controller(
                orphan_controllers,
                controller,
            )
        if isinstance(acquisition_error, RecoveryLedgerError):
            if controller_owner is not None:
                acquisition_error._set_cleanup_owner(controller_owner)
            raise
        recovery_error = RecoveryLedgerError("recovery quiescence is unavailable")
        if controller_owner is not None:
            recovery_error._set_cleanup_owner(controller_owner)
        raise recovery_error from acquisition_error
    body_error: BaseException | None = None
    result: object = object()
    try:
        owner = session.issue_owner()
        with owner._borrow_root(state_root) as root_fd:
            result = operation(root_fd)
    except _CLEANUP_EXCEPTION as exc:
        if isinstance(exc, RecoveryLedgerError):
            body_error = exc
        else:
            body_error = RecoveryLedgerError("recovery quiescence operation failed")
            body_error.__cause__ = exc
    try:
        session.close()
    except _CLEANUP_EXCEPTION as exc:
        session_owner = _remember_orphan_session(orphan_sessions, session)
        if body_error is None:
            body_error = RecoveryLedgerError(
                "recovery quiescence close status is unknown"
            )
            body_error.__cause__ = exc
        if body_error is not None:
            body_error = _error_with_cleanup_owner(
                body_error,
                session_owner,
                "recovery quiescence cleanup failed",
            )
    if body_error is not None:
        raise body_error
    return cast(_T, result)


@contextmanager
def _borrowed_root(owner: object, state_root: Path) -> Iterator[int]:
    if type(owner) is not QuiescenceOwner:
        raise RecoveryLedgerError("trusted quiescence owner is required")
    borrow = getattr(owner, "_borrow_root", None)
    if not callable(borrow):
        raise RecoveryLedgerError("trusted quiescence owner is required")
    try:
        context = borrow(state_root)
        enter = getattr(context, "__enter__", None)
        exit_method = getattr(context, "__exit__", None)
        if not callable(enter) or not callable(exit_method):
            raise RecoveryLedgerError("quiescence owner returned no root context")
        with context as borrowed:
            root_fd = (
                borrowed
                if type(borrowed) is int
                else getattr(borrowed, "root_fd", None)
            )
            if type(root_fd) is not int and borrowed is not None:
                root_fd = getattr(borrowed, "_root_fd", None)
            if type(root_fd) is not int:
                raise RecoveryLedgerError(
                    "quiescence owner returned no root descriptor"
                )
            for subject in (owner, borrowed):
                assert_identity = getattr(subject, "assert_identity", None)
                if callable(assert_identity):
                    assert_identity()
            _validate_root_descriptor(root_fd, state_root)
            for subject in (owner, borrowed):
                for name in ("state_root", "root"):
                    candidate = getattr(subject, name, None)
                    if candidate is None:
                        continue
                    try:
                        candidate_root = _doctor._coerce_root(candidate)
                    except (TypeError, ValueError) as exc:
                        raise RecoveryLedgerError(
                            "quiescence owner root is invalid"
                        ) from exc
                    if candidate_root != state_root:
                        raise RecoveryLedgerError("quiescence owner root mismatches")
            yield root_fd
            for subject in (owner, borrowed):
                assert_identity = getattr(subject, "assert_identity", None)
                if callable(assert_identity):
                    assert_identity()
            _validate_root_descriptor(root_fd, state_root)
    except RecoveryLedgerError:
        raise
    except Exception as exc:
        raise RecoveryLedgerError("quiescence owner root is unavailable") from exc


def _read_root_file(
    root_fd: int,
    name: str,
    *,
    orphan_registry: list[_OrphanFD] | None = None,
    retain_fd: _RetainFD | None = None,
) -> tuple[bytes, os.stat_result] | tuple[None, None]:
    if type(root_fd) is not int:
        raise RecoveryLedgerError("state root descriptor is invalid")
    if type(name) is not str or name not in {
        RECOVERY_LEDGER_BASENAME,
        RECOVERY_TOMBSTONES_BASENAME,
    }:
        raise RecoveryLedgerError("recovery file basename is not canonical")
    try:
        before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise RecoveryLedgerError("recovery file cannot be inspected") from exc
    _validate_private_regular(before, name)
    try:
        flags = _store._open_flags(directory=False, writable=False)
    except _store.StoreError as exc:
        raise RecoveryLedgerError("secure recovery read is unavailable") from exc
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if nonblock == 0:
        raise RecoveryLedgerError("non-blocking recovery read is unavailable")
    read_fd: int | None = None
    opened: os.stat_result | None = None
    try:
        read_fd = os.open(name, flags | nonblock, dir_fd=root_fd)
        opened = os.fstat(read_fd)
        _validate_private_regular(opened, name)
        if _metadata_for_entry(opened) != _metadata_for_entry(before):
            raise RecoveryLedgerError("recovery file changed while opening")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(read_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_LEDGER_BYTES:
                raise RecoveryLedgerError("recovery file is too large")
        after = os.fstat(read_fd)
        path = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        _validate_private_regular(after, name)
        if _metadata_for_entry(opened) != _metadata_for_entry(
            after
        ) or _metadata_for_entry(opened) != _metadata_for_entry(path):
            raise RecoveryLedgerError("recovery file changed while reading")
        return b"".join(chunks), opened
    except RecoveryLedgerError:
        raise
    except OSError as exc:
        raise RecoveryLedgerError("recovery file cannot be read") from exc
    finally:
        body_error = sys.exc_info()[1]
        if read_fd is not None:
            try:
                os.close(read_fd)
            except _CLEANUP_EXCEPTION as exc:
                retention_error: BaseException | None = None
                retention_owner: _CleanupCapability | None = None
                try:
                    retention_owner = _retain_failed_fd_with_fallback(
                        orphan_registry,
                        retain_fd,
                        read_fd,
                        None if opened is None else _store._identity(opened),
                        f"recovery file {name} read",
                    )
                except _CLEANUP_EXCEPTION as retention_exc:
                    retention_error = retention_exc
                    retention_owner = _error_cleanup_owner(retention_exc)
                if retention_error is None:
                    close_error = RecoveryLedgerError(
                        "recovery read close status is unknown"
                    )
                    close_error.__cause__ = exc
                    retention_error = close_error
                if retention_owner is not None:
                    _raise_with_cleanup(
                        body_error,
                        retention_error,
                        retention_owner,
                        "recovery read cleanup failed",
                    )
                if isinstance(body_error, BaseException):
                    raise body_error from retention_error
                raise retention_error


def _read_fd_bytes(fd: int, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(fd, 1024 * 1024)
        except OSError as exc:
            raise RecoveryLedgerError(f"{label} cannot be read") from exc
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_LEDGER_BYTES:
            raise RecoveryLedgerError(f"{label} is too large")


def _durability_barrier_at_root(
    root_fd: int,
    state_root: Path,
    name: str,
    *,
    latest_from_bytes: Callable[[bytes, bool], object | None],
    retain_fd: _RetainFD | None,
) -> bytes | None:
    _validate_root_descriptor(root_fd, state_root)
    try:
        before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecoveryLedgerError("recovery file cannot be inspected") from exc
    _validate_private_regular(before, name)
    try:
        flags = _store._open_flags(directory=False, writable=False)
    except _store.StoreError as exc:
        raise RecoveryLedgerError("secure recovery durability is unavailable") from exc
    fd: int | None = None
    opened: os.stat_result | None = None
    try:
        try:
            fd = os.open(name, flags, dir_fd=root_fd)
        except OSError as exc:
            raise RecoveryLedgerError("recovery file cannot be opened") from exc
        opened = os.fstat(fd)
        _validate_private_regular(opened, name)
        if _metadata_for_entry(opened) != _metadata_for_entry(before):
            raise RecoveryLedgerError("recovery file changed before durability barrier")
        raw_before = _read_fd_bytes(fd, f"recovery file {name}")
        try:
            parsed = latest_from_bytes(raw_before, False)
        except _doctor.DoctorError as exc:
            raise RecoveryLedgerError("recovery file is malformed") from exc
        if parsed is None:
            raise RecoveryLedgerError("recovery file readback is empty")
        try:
            os.fsync(fd)
            os.fsync(root_fd)
        except _CLEANUP_EXCEPTION as exc:
            raise RecoveryDurabilityError(
                "recovery file durability is unknown"
            ) from exc
        os.lseek(fd, 0, os.SEEK_SET)
        raw_after = _read_fd_bytes(fd, f"recovery file {name}")
        if raw_after != raw_before:
            raise RecoveryLedgerError("recovery file changed during durability barrier")
        after = os.fstat(fd)
        path = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        _validate_private_regular(after, name)
        if _metadata_for_entry(opened) != _metadata_for_entry(
            after
        ) or _metadata_for_entry(opened) != _metadata_for_entry(path):
            raise RecoveryLedgerError("recovery file changed during durability barrier")
        try:
            parsed_after = latest_from_bytes(raw_after, False)
        except _doctor.DoctorError as exc:
            raise RecoveryLedgerError("recovery file readback is malformed") from exc
        if parsed_after is None:
            raise RecoveryLedgerError("recovery file readback is empty")
        return raw_after
    except RecoveryLedgerError:
        raise
    except _CLEANUP_EXCEPTION as exc:
        raise RecoveryDurabilityError("recovery file durability is unknown") from exc
    finally:
        body_error = sys.exc_info()[1]
        if fd is not None:
            try:
                os.close(fd)
            except _CLEANUP_EXCEPTION as close_failure:
                cleanup_failure: BaseException = close_failure
                retention_owner: _CleanupCapability | None = None
                try:
                    retention_owner = _retain_failed_fd_with_fallback(
                        None,
                        retain_fd,
                        fd,
                        None if opened is None else _store._identity(opened),
                        f"recovery file {name} durability",
                    )
                except _CLEANUP_EXCEPTION as retention_error:
                    retention_error_owner = _error_cleanup_owner(retention_error)
                    if retention_error_owner is not None:
                        retention_owner = retention_error_owner
                    cleanup_failure = retention_error
                if not isinstance(cleanup_failure, RecoveryLedgerError):
                    wrapped_close_error = RecoveryLedgerError(
                        f"recovery file {name} durability close status is unknown"
                    )
                    wrapped_close_error.__cause__ = cleanup_failure
                    cleanup_failure = wrapped_close_error
                _raise_with_cleanup(
                    body_error,
                    cleanup_failure,
                    retention_owner,
                    f"recovery file {name} durability cleanup failed",
                )


def _append_owned_file(
    *,
    root_fd: int,
    name: str,
    record: object,
    allow_create: bool,
    encode: Callable[[object], bytes],
    latest_from_bytes: Callable[[bytes, bool], object | None],
    validate_append: Callable[[object, object | None], None],
    same_record: Callable[[object, object], bool],
    fault: Callable[[str], None],
    orphan_registry: list[_OrphanFD] | None = None,
    retain_fd: _RetainFD | None = None,
) -> object:
    if type(root_fd) is not int:
        raise RecoveryLedgerError("state root descriptor is invalid")
    original, original_metadata = _read_root_file(
        root_fd,
        name,
        orphan_registry=orphan_registry,
        retain_fd=retain_fd,
    )
    creating = original is None
    if creating and not allow_create:
        raise RecoveryLedgerError("recovery file is missing")
    if not creating and allow_create:
        raise RecoveryLedgerError("recovery file is already initialized")
    if creating:
        original = b""
    else:
        try:
            _ = latest_from_bytes(cast(bytes, original), False)
        except _doctor.DoctorError as exc:
            raise RecoveryLedgerError("recovery file is malformed") from exc
    try:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        nonblock = getattr(os, "O_NONBLOCK", 0)
        if nofollow == 0 or nonblock == 0:
            raise RecoveryLedgerError(
                "secure non-blocking recovery append is unavailable"
            )
        flags |= nofollow | nonblock
        if creating:
            flags |= os.O_CREAT | os.O_EXCL
        fault("before_ledger_open")
        fd = os.open(name, flags, 0o600, dir_fd=root_fd)
    except RecoveryLedgerError:
        raise
    except OSError as exc:
        raise RecoveryLedgerError("recovery file cannot be opened") from exc
    locked = False
    result: object | None = None
    cleanup_error: BaseException | None = None
    opened_identity: tuple[int, int] | None = None
    try:
        metadata = os.fstat(fd)
        opened_identity = _store._identity(metadata)
        _validate_private_regular(metadata, name)
        if original_metadata is not None and _metadata_for_entry(
            metadata
        ) != _metadata_for_entry(original_metadata):
            raise RecoveryLedgerError("recovery file changed while opening")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RecoveryLedgerError("recovery file is busy") from exc
        locked = True
        fault("after_ledger_lock")
        current, current_metadata = _read_root_file(
            root_fd,
            name,
            orphan_registry=orphan_registry,
            retain_fd=retain_fd,
        )
        if current is None or current != original:
            raise RecoveryLedgerError("recovery file changed before append")
        if current_metadata is None:
            raise RecoveryLedgerError("recovery file metadata is unavailable")
        if original_metadata is not None:
            if _metadata_for_entry(current_metadata) != _metadata_for_entry(
                original_metadata
            ):
                raise RecoveryLedgerError("recovery file changed before append")
        elif _metadata_for_entry(current_metadata) != _metadata_for_entry(metadata):
            raise RecoveryLedgerError("new recovery file changed before append")
        try:
            locked_latest = latest_from_bytes(current, creating)
        except _doctor.DoctorError as exc:
            raise RecoveryLedgerError("recovery file is malformed") from exc
        validate_append(record, locked_latest)
        encoded = encode(record)
        if len(encoded) > MAX_LEDGER_BYTES or len(current) > MAX_LEDGER_BYTES - len(
            encoded
        ):
            raise RecoveryLedgerError("recovery file record is too large")
        fault("before_ledger_write")
        offset = 0
        while offset < len(encoded):
            try:
                written = os.write(fd, encoded[offset:])
            except OSError as exc:
                raise RecoveryLedgerError("recovery file append failed") from exc
            if written <= 0:
                raise RecoveryLedgerError("recovery file append was incomplete")
            offset += written
        fault("after_ledger_write")
        try:
            os.fsync(fd)
            os.fsync(root_fd)
        except OSError as exc:
            raise RecoveryLedgerError("recovery file durability is unknown") from exc
        fault("before_final_check")
        path_metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        final_metadata = os.fstat(fd)
        _validate_private_regular(final_metadata, name)
        if _metadata_for_entry(path_metadata) != _metadata_for_entry(
            final_metadata
        ) or (
            current_metadata is not None
            and _metadata_for_entry(path_metadata)[:6]
            != _metadata_for_entry(current_metadata)[:6]
        ):
            raise RecoveryLedgerError("recovery file identity changed after append")
        final_bytes, _ = _read_root_file(
            root_fd,
            name,
            orphan_registry=orphan_registry,
            retain_fd=retain_fd,
        )
        expected = current + encoded
        if final_bytes != expected:
            raise RecoveryLedgerError("recovery file bytes changed after append")
        try:
            final_latest = latest_from_bytes(final_bytes, False)
        except _doctor.DoctorError as exc:
            raise RecoveryLedgerError("recovery file readback is malformed") from exc
        if final_latest is None or not same_record(record, final_latest):
            raise RecoveryLedgerError("recovery file readback mismatches record")
        result = record
    finally:
        body_error = sys.exc_info()[1]
        retention_owner: _CleanupCapability | None = None
        if locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except _CLEANUP_EXCEPTION as exc:
                cleanup_error = RecoveryLedgerError(
                    "recovery file unlock status is unknown"
                )
                cleanup_error.__cause__ = exc
        after_unlock: os.stat_result | None = None
        try:
            after_unlock = os.fstat(fd)
        except _CLEANUP_EXCEPTION as exc:
            if isinstance(exc, OSError) and exc.errno == errno.EBADF:
                if cleanup_error is None:
                    cleanup_error = RecoveryLedgerError(
                        f"recovery file {name} disappeared during unlock"
                    )
                    cleanup_error.__cause__ = exc
            else:
                try:
                    retention_owner = _retain_failed_fd_with_fallback(
                        orphan_registry,
                        retain_fd,
                        fd,
                        opened_identity,
                        f"recovery file {name} unlock",
                    )
                except _CLEANUP_EXCEPTION as retention_error:
                    if cleanup_error is None:
                        cleanup_error = retention_error
                    retention_owner = _error_cleanup_owner(retention_error)
                else:
                    if cleanup_error is None:
                        cleanup_error = RecoveryLedgerError(
                            f"recovery file {name} identity is unknown after unlock"
                        )
                        cleanup_error.__cause__ = exc
            after_unlock = None
        if (
            after_unlock is not None
            and (
                opened_identity is None
                or _store._identity(after_unlock) != opened_identity
            )
            and cleanup_error is None
        ):
            cleanup_error = RecoveryLedgerError(
                f"recovery file {name} descriptor was reused after unlock"
            )
        identity_safe_after_unlock = (
            after_unlock is not None
            and opened_identity is not None
            and _store._identity(after_unlock) == opened_identity
        )
        if identity_safe_after_unlock:
            try:
                os.close(fd)
            except _CLEANUP_EXCEPTION as exc:
                try:
                    if retain_fd is None:
                        retention_owner = _retain_failed_fd_with_fallback(
                            orphan_registry,
                            None,
                            fd,
                            opened_identity,
                            f"recovery file {name} append",
                        )
                    else:
                        retention_owner = _remember_orphan_fd(
                            orphan_registry,
                            retain_fd,
                            fd,
                            opened_identity,
                            f"recovery file {name} append",
                        )
                except _CLEANUP_EXCEPTION as retention_error:
                    if cleanup_error is None:
                        cleanup_error = RecoveryLedgerError(
                            "recovery file close ownership is unknown"
                        )
                        cleanup_error.__cause__ = retention_error
                    retention_owner = _error_cleanup_owner(retention_error)
                if cleanup_error is None:
                    close_error = RecoveryLedgerError(
                        f"recovery file {name} close status is unknown"
                    )
                    close_error.__cause__ = exc
                    cleanup_error = close_error
        if cleanup_error is not None:
            owner = retention_owner
            if owner is None:
                owner = _error_cleanup_owner(cleanup_error)
            _raise_with_cleanup(
                body_error,
                cleanup_error,
                owner,
                "recovery file append cleanup failed",
            )
    if result is None:
        raise RecoveryLedgerError("recovery file append produced no result")
    return result


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
        if self.marker_name != WRITER_MARKER_BASENAME:
            raise ValueError("marker_name is not canonical")
        if self.marker_name == RECOVERY_LEDGER_BASENAME:
            raise ValueError("marker_name and ledger basename must differ")
        if (
            type(busy_timeout_ms) is not int
            or not 0 <= busy_timeout_ms <= _store.MAX_BUSY_TIMEOUT_MS
        ):
            raise ValueError("busy_timeout_ms must be between 0 and 30000")
        self.busy_timeout_ms = busy_timeout_ms
        self.ledger_name = RECOVERY_LEDGER_BASENAME
        self._orphan_fds: list[_OrphanFD] = []
        self._orphan_filesystems: list[StateFilesystem] = []
        self._orphan_sessions: list[QuiescenceSession] = []
        self._orphan_controllers: list[WalSidecarController] = []

    def _retry_resources(self) -> None:
        _retry_resource_actions(
            (
                lambda: _retry_orphan_controllers(self._orphan_controllers),
                lambda: _retry_orphan_sessions(self._orphan_sessions),
                lambda: _retry_orphan_fds(self._orphan_fds),
                lambda: _retry_orphan_filesystems(self._orphan_filesystems),
            )
        )

    def close(self) -> None:
        """Retry retained descriptors and filesystem resources."""

        self._retry_resources()

    def __enter__(self) -> Self:
        self.close()
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
                _raise_with_cleanup(
                    exc_value,
                    cleanup_error,
                    _error_cleanup_owner(cleanup_error),
                    "recovery ledger context cleanup failed",
                )
            raise

    def read(self) -> RecoveryLedgerRecord | None:
        self._retry_resources()
        filesystem = self._open_filesystem()
        try:
            root_fd = filesystem._root_fd
            if root_fd is None:
                raise RecoveryLedgerError("state root descriptor is unavailable")
            inventory = filesystem.inventory()
            if inventory.entry(self.ledger_name) is None:
                filesystem.assert_identity(inventory)
                return None
            try:
                raw, _ = self._read_ledger(root_fd)
                latest = self._latest_from_bytes(raw, allow_empty=False)
            except _doctor.DoctorError as exc:
                raise RecoveryLedgerError("recovery ledger is malformed") from exc
            if latest is None:
                raise RecoveryLedgerError("recovery ledger readback is empty")
            return _record_from_snapshot(latest)
        finally:
            body_error = sys.exc_info()[1]
            try:
                filesystem.close()
            except _CLEANUP_EXCEPTION as exc:
                filesystem_owner = _remember_orphan_filesystem(
                    self._orphan_filesystems,
                    filesystem,
                )
                close_error = RecoveryLedgerError(
                    "recovery ledger filesystem close status is unknown"
                )
                close_error.__cause__ = exc
                _raise_with_cleanup(
                    body_error,
                    close_error,
                    filesystem_owner,
                    "recovery ledger filesystem cleanup failed",
                )

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

    def read_owned(self, owner: object) -> RecoveryLedgerRecord | None:
        """Read through one caller-held quiescence root descriptor."""

        self._retry_resources()
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            raw, _ = _read_root_file(
                root_fd,
                RECOVERY_LEDGER_BASENAME,
                retain_fd=retain_fd,
            )
            if raw is None:
                return None
            try:
                latest = self._latest_from_bytes(raw, allow_empty=False)
            except _doctor.DoctorError as exc:
                raise RecoveryLedgerError("recovery ledger is malformed") from exc
            if latest is None:
                raise RecoveryLedgerError("recovery ledger readback is empty")
            return _record_from_snapshot(latest)

    def ensure_durable_owned(self, owner: object) -> RecoveryLedgerRecord | None:
        """Fsync and revalidate the existing ledger under a held owner."""

        self._retry_resources()
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            raw = _durability_barrier_at_root(
                root_fd,
                self.state_root,
                RECOVERY_LEDGER_BASENAME,
                latest_from_bytes=lambda value, allow_empty: self._latest_from_bytes(
                    value,
                    allow_empty=allow_empty,
                ),
                retain_fd=retain_fd,
            )
            if raw is None:
                return None
            try:
                latest = self._latest_from_bytes(raw, allow_empty=False)
            except _doctor.DoctorError as exc:
                raise RecoveryLedgerError("recovery ledger is malformed") from exc
            if latest is None:
                raise RecoveryLedgerError("recovery ledger readback is empty")
            return _record_from_snapshot(latest)

    def _append_owned_at_root(
        self,
        root_fd: int,
        record: RecoveryLedgerRecord,
        *,
        allow_create: bool,
        orphan_registry: list[_OrphanFD] | None = None,
        retain_fd: _RetainFD | None = None,
    ) -> RecoveryLedgerRecord:
        if orphan_registry is None:
            orphan_registry = self._orphan_fds
        result = _append_owned_file(
            root_fd=root_fd,
            name=RECOVERY_LEDGER_BASENAME,
            record=record,
            allow_create=allow_create,
            encode=lambda value: _encode_record(cast(RecoveryLedgerRecord, value)),
            latest_from_bytes=lambda raw, allow_empty: self._latest_from_bytes(
                raw,
                allow_empty=allow_empty,
            ),
            validate_append=lambda value, latest: self._validate_append(
                cast(RecoveryLedgerRecord, value),
                cast(LedgerSnapshot | None, latest),
            ),
            same_record=lambda value, latest: (
                latest is not None
                and _same_record(
                    cast(RecoveryLedgerRecord, value),
                    _record_from_snapshot(cast(LedgerSnapshot, latest)),
                )
            ),
            fault=self._fault,
            orphan_registry=orphan_registry,
            retain_fd=retain_fd,
        )
        _validate_root_descriptor(root_fd, self.state_root)
        return cast(RecoveryLedgerRecord, result)

    def initialize_owned(
        self,
        record: RecoveryLedgerRecord,
        authority: RecoveryLedgerInitialization,
        owner: object,
    ) -> RecoveryLedgerRecord:
        """Create the first ledger record under a held quiescence owner."""

        self._retry_resources()
        canonical = _canonical_record(record)
        _validate_initialization(authority, canonical)
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            return self._append_owned_at_root(
                root_fd,
                canonical,
                allow_create=True,
                retain_fd=retain_fd,
            )

    def append_owned(
        self,
        record: RecoveryLedgerRecord,
        owner: object,
    ) -> RecoveryLedgerRecord:
        """Append one record without reacquiring gate or marker locks."""

        self._retry_resources()
        canonical = _canonical_record(record)
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            return self._append_owned_at_root(
                root_fd,
                canonical,
                allow_create=False,
                retain_fd=retain_fd,
            )

    def _append_without_store(
        self,
        record: RecoveryLedgerRecord,
        *,
        allow_create: bool,
    ) -> RecoveryLedgerRecord:
        filesystem = self._open_filesystem()
        root_locked = False
        try:
            root_fd = filesystem._root_fd
            if root_fd is None:
                raise RecoveryLedgerError("state root descriptor is unavailable")
            _lock_root_exclusive(root_fd, self.busy_timeout_ms)
            root_locked = True
            _validate_root_descriptor(root_fd, self.state_root)
            try:
                before = _bootstrap_inventory(filesystem)
            except (_doctor.DoctorError, OSError, ValueError) as exc:
                raise RecoveryLedgerError(
                    "recovery root cannot be inventoried"
                ) from exc
            database_entry = before.entry(_store.DATABASE_FILENAME)
            if database_entry is not None and _lifetime_gate_is_present(
                self.state_root
            ):
                raise RecoveryLedgerError(
                    "initialized recovery root requires quiescence"
                )
            marker_fd = filesystem._marker_fd
            if marker_fd is not None and not filesystem.try_marker_exclusive():
                raise RecoveryLedgerError("writer marker is busy")
            try:
                result = self._append_owned_at_root(
                    root_fd,
                    record,
                    allow_create=allow_create,
                    orphan_registry=self._orphan_fds,
                )
            except OSError as exc:
                raise RecoveryLedgerError("recovery ledger append failed") from exc
            try:
                after = _bootstrap_inventory(filesystem)
            except (_doctor.DoctorError, OSError, ValueError) as exc:
                raise RecoveryLedgerError(
                    "recovery root cannot be inventoried"
                ) from exc
            _assert_bootstrap_transition(
                before,
                after,
                RECOVERY_LEDGER_BASENAME,
                len(_encode_record(record)),
            )
            return result
        finally:
            body_error = sys.exc_info()[1]
            cleanup_error: RecoveryLedgerError | None = None
            filesystem_owner: _CleanupCapability | None = None
            if root_locked:
                try:
                    fcntl.flock(cast(int, filesystem._root_fd), fcntl.LOCK_UN)
                except _CLEANUP_EXCEPTION as exc:
                    cleanup_error = RecoveryLedgerError(
                        "recovery root unlock status is unknown"
                    )
                    cleanup_error.__cause__ = exc
            try:
                filesystem.close()
            except _CLEANUP_EXCEPTION as exc:
                filesystem_owner = _remember_orphan_filesystem(
                    self._orphan_filesystems,
                    filesystem,
                )
                if cleanup_error is None:
                    cleanup_error = RecoveryLedgerError(
                        "recovery ledger filesystem close status is unknown"
                    )
                    cleanup_error.__cause__ = exc
            if cleanup_error is not None:
                owner = filesystem_owner
                if owner is None:
                    owner = _error_cleanup_owner(cleanup_error)
                _raise_with_cleanup(
                    body_error,
                    cleanup_error,
                    owner,
                    "recovery ledger bootstrap cleanup failed",
                )

    def _append_impl(
        self,
        record: RecoveryLedgerRecord,
        *,
        allow_create: bool,
    ) -> RecoveryLedgerRecord:
        self._retry_resources()
        if type(self.ledger_name) is not str:
            raise RecoveryLedgerError("recovery ledger basename is not canonical")
        if self.ledger_name != RECOVERY_LEDGER_BASENAME:
            raise RecoveryLedgerError("recovery ledger basename is not canonical")
        if not _primary_is_present(self.state_root) or not _lifetime_gate_is_present(
            self.state_root
        ):
            return self._append_without_store(record, allow_create=allow_create)
        controller = WalSidecarController(
            self.state_root,
            busy_timeout_ms=self.busy_timeout_ms,
        )
        return _run_unowned_quiescence(
            controller,
            self.state_root,
            lambda root_fd: self._append_owned_at_root(
                root_fd,
                record,
                allow_create=allow_create,
                orphan_registry=self._orphan_fds,
            ),
            self._orphan_sessions,
            self._orphan_controllers,
        )

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
            raise _recovery_filesystem_error(
                "recovery ledger filesystem is unavailable", exc
            ) from exc

    def _read_ledger(
        self,
        root_fd: int,
        *,
        orphan_registry: list[_OrphanFD] | None = None,
    ) -> tuple[bytes, os.stat_result]:
        flags = _store._open_flags(directory=False, writable=False)
        nonblock = getattr(os, "O_NONBLOCK", 0)
        if nonblock == 0:
            raise RecoveryLedgerError("non-blocking ledger read is unavailable")
        flags |= nonblock
        if orphan_registry is None:
            orphan_registry = self._orphan_fds
        try:
            read_fd = os.open(self.ledger_name, flags, dir_fd=root_fd)
        except OSError as exc:
            raise RecoveryLedgerError("recovery ledger cannot be read") from exc
        opened: os.stat_result | None = None
        try:
            before = os.fstat(read_fd)
            opened = before
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
            body_error = sys.exc_info()[1]
            try:
                os.close(read_fd)
            except _CLEANUP_EXCEPTION as exc:
                retention_error: BaseException | None = None
                retention_owner: _CleanupCapability | None = None
                try:
                    retention_owner = _remember_orphan_fd(
                        orphan_registry,
                        None,
                        read_fd,
                        None if opened is None else _store._identity(opened),
                        "recovery ledger read",
                    )
                except _CLEANUP_EXCEPTION as retention_exc:
                    retention_error = retention_exc
                if retention_error is None:
                    close_error = RecoveryLedgerError(
                        "recovery ledger read close status is unknown"
                    )
                    close_error.__cause__ = exc
                    retention_error = close_error
                if retention_owner is not None:
                    _raise_with_cleanup(
                        body_error,
                        retention_error,
                        retention_owner,
                        "recovery ledger read cleanup failed",
                    )
                if isinstance(body_error, BaseException):
                    raise body_error from retention_error
                raise retention_error

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
            if previous is None and (
                snapshot.sequence != 1
                or snapshot.phase != "RESTORE_PREPARED"
                or snapshot.restore_generation != 1
            ):
                raise _doctor.LedgerReadError(
                    "recovery ledger must start prepared sequence one generation one"
                )
            if previous is not None:
                if snapshot.sequence != previous.sequence + 1:
                    raise _doctor.LedgerReadError(
                        "recovery ledger sequence is not monotonic"
                    )
                if previous.phase in _TERMINAL_PHASES:
                    if (
                        snapshot.phase != "RESTORE_PREPARED"
                        or snapshot.restore_generation
                        != previous.restore_generation + 1
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
                if snapshot.restore_generation == previous.restore_generation and (
                    snapshot.backup_digest != previous.backup_digest
                    or snapshot.actor != previous.actor
                    or snapshot.audit_ref != previous.audit_ref
                ):
                    raise _doctor.LedgerReadError(
                        "recovery ledger provenance changed in one generation"
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
            if (
                record.sequence != 1
                or record.phase != "RESTORE_PREPARED"
                or record.restore_generation != 1
            ):
                raise RecoveryLedgerError(
                    "recovery ledger must start with RESTORE_PREPARED sequence one generation one"
                )
            return
        if record.sequence != latest.sequence + 1:
            raise RecoveryLedgerError("recovery ledger sequence is not monotonic")
        if latest.phase in _TERMINAL_PHASES:
            if (
                record.phase != "RESTORE_PREPARED"
                or record.restore_generation != latest.restore_generation + 1
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
        if record.restore_generation == latest.restore_generation and (
            record.backup_digest != latest.backup_digest
            or record.actor != latest.actor
            or record.audit_ref != latest.audit_ref
        ):
            raise RecoveryLedgerError(
                "recovery ledger provenance changed in one generation"
            )


class RecoveryTombstoneLog:
    """Strict append-only v1 log for identities removed by a restore."""

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
        if self.marker_name != WRITER_MARKER_BASENAME:
            raise ValueError("marker_name is not canonical")
        if (
            type(busy_timeout_ms) is not int
            or not 0 <= busy_timeout_ms <= _store.MAX_BUSY_TIMEOUT_MS
        ):
            raise ValueError("busy_timeout_ms must be between 0 and 30000")
        self.busy_timeout_ms = busy_timeout_ms
        self._orphan_fds: list[_OrphanFD] = []
        self._orphan_filesystems: list[StateFilesystem] = []
        self._orphan_sessions: list[QuiescenceSession] = []
        self._orphan_controllers: list[WalSidecarController] = []

    def _retry_resources(self) -> None:
        _retry_resource_actions(
            (
                lambda: _retry_orphan_controllers(self._orphan_controllers),
                lambda: _retry_orphan_sessions(self._orphan_sessions),
                lambda: _retry_orphan_fds(self._orphan_fds),
                lambda: _retry_orphan_filesystems(self._orphan_filesystems),
            )
        )

    def close(self) -> None:
        """Retry retained descriptors and filesystem resources."""

        self._retry_resources()

    def __enter__(self) -> Self:
        self.close()
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
                _raise_with_cleanup(
                    exc_value,
                    cleanup_error,
                    _error_cleanup_owner(cleanup_error),
                    "tombstone context cleanup failed",
                )
            raise

    def read(self) -> RecoveryTombstoneRecord | None:
        self._retry_resources()
        filesystem = self._open_filesystem()
        try:
            root_fd = filesystem._root_fd
            if root_fd is None:
                raise RecoveryLedgerError("state root descriptor is unavailable")
            _validate_root_descriptor(root_fd, self.state_root)
            raw, _ = _read_root_file(
                root_fd,
                RECOVERY_TOMBSTONES_BASENAME,
                orphan_registry=self._orphan_fds,
            )
            if raw is None:
                return None
            latest = _latest_tombstone(raw, allow_empty=False)
            if latest is None:
                raise RecoveryLedgerError("tombstone readback is empty")
            _validate_root_descriptor(root_fd, self.state_root)
            return latest
        finally:
            body_error = sys.exc_info()[1]
            try:
                filesystem.close()
            except _CLEANUP_EXCEPTION as exc:
                filesystem_owner = _remember_orphan_filesystem(
                    self._orphan_filesystems,
                    filesystem,
                )
                close_error = RecoveryLedgerError(
                    "tombstone filesystem close status is unknown"
                )
                close_error.__cause__ = exc
                _raise_with_cleanup(
                    body_error,
                    close_error,
                    filesystem_owner,
                    "tombstone filesystem cleanup failed",
                )

    def initialize(
        self,
        record: RecoveryTombstoneRecord,
        authority: RecoveryLedgerInitialization,
    ) -> RecoveryTombstoneRecord:
        canonical = _canonical_tombstone(record)
        _validate_tombstone_initialization(authority, canonical)
        return self._append_with_filesystem(canonical, allow_create=True)

    def append(self, record: RecoveryTombstoneRecord) -> RecoveryTombstoneRecord:
        canonical = _canonical_tombstone(record)
        return self._append_with_filesystem(canonical, allow_create=False)

    def read_owned(self, owner: object) -> RecoveryTombstoneRecord | None:
        self._retry_resources()
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            raw, _ = _read_root_file(
                root_fd,
                RECOVERY_TOMBSTONES_BASENAME,
                orphan_registry=self._orphan_fds,
                retain_fd=retain_fd,
            )
            if raw is None:
                return None
            latest = _latest_tombstone(raw, allow_empty=False)
            if latest is None:
                raise RecoveryLedgerError("tombstone readback is empty")
            return latest

    def ensure_durable_owned(self, owner: object) -> RecoveryTombstoneRecord | None:
        """Fsync and revalidate the existing tombstone under a held owner."""

        self._retry_resources()
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            raw = _durability_barrier_at_root(
                root_fd,
                self.state_root,
                RECOVERY_TOMBSTONES_BASENAME,
                latest_from_bytes=lambda value, allow_empty: _latest_tombstone(
                    value,
                    allow_empty=allow_empty,
                ),
                retain_fd=retain_fd,
            )
            if raw is None:
                return None
            try:
                latest = _latest_tombstone(raw, allow_empty=False)
            except _doctor.DoctorError as exc:
                raise RecoveryLedgerError("tombstone log is malformed") from exc
            if latest is None:
                raise RecoveryLedgerError("tombstone readback is empty")
            return latest

    def initialize_owned(
        self,
        record: RecoveryTombstoneRecord,
        owner: object,
    ) -> RecoveryTombstoneRecord:
        self._retry_resources()
        canonical = _canonical_tombstone(record)
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            return self._append_owned_at_root(
                root_fd,
                canonical,
                allow_create=True,
                retain_fd=retain_fd,
            )

    def append_owned(
        self,
        record: RecoveryTombstoneRecord,
        owner: object,
    ) -> RecoveryTombstoneRecord:
        self._retry_resources()
        canonical = _canonical_tombstone(record)
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            return self._append_owned_at_root(
                root_fd,
                canonical,
                allow_create=False,
                retain_fd=retain_fd,
            )

    def _append_without_store(
        self,
        record: RecoveryTombstoneRecord,
        *,
        allow_create: bool,
    ) -> RecoveryTombstoneRecord:
        filesystem = self._open_filesystem()
        root_locked = False
        try:
            root_fd = filesystem._root_fd
            if root_fd is None:
                raise RecoveryLedgerError("state root descriptor is unavailable")
            _lock_root_exclusive(root_fd, self.busy_timeout_ms)
            root_locked = True
            _validate_root_descriptor(root_fd, self.state_root)
            try:
                before = _bootstrap_inventory(filesystem)
            except (_doctor.DoctorError, OSError, ValueError) as exc:
                raise RecoveryLedgerError(
                    "recovery root cannot be inventoried"
                ) from exc
            database_entry = before.entry(_store.DATABASE_FILENAME)
            if database_entry is not None and _lifetime_gate_is_present(
                self.state_root
            ):
                raise RecoveryLedgerError(
                    "initialized recovery root requires quiescence"
                )
            marker_fd = filesystem._marker_fd
            if marker_fd is not None and not filesystem.try_marker_exclusive():
                raise RecoveryLedgerError("writer marker is busy")
            try:
                result = self._append_owned_at_root(
                    root_fd,
                    record,
                    allow_create=allow_create,
                    orphan_registry=self._orphan_fds,
                )
            except OSError as exc:
                raise RecoveryLedgerError("tombstone append failed") from exc
            try:
                after = _bootstrap_inventory(filesystem)
            except (_doctor.DoctorError, OSError, ValueError) as exc:
                raise RecoveryLedgerError(
                    "recovery root cannot be inventoried"
                ) from exc
            _assert_bootstrap_transition(
                before,
                after,
                RECOVERY_TOMBSTONES_BASENAME,
                len(_encode_tombstone(record)),
            )
            return result
        finally:
            body_error = sys.exc_info()[1]
            cleanup_error: RecoveryLedgerError | None = None
            filesystem_owner: _CleanupCapability | None = None
            if root_locked:
                try:
                    fcntl.flock(cast(int, filesystem._root_fd), fcntl.LOCK_UN)
                except _CLEANUP_EXCEPTION as exc:
                    cleanup_error = RecoveryLedgerError(
                        "recovery root unlock status is unknown"
                    )
                    cleanup_error.__cause__ = exc
            try:
                filesystem.close()
            except _CLEANUP_EXCEPTION as exc:
                filesystem_owner = _remember_orphan_filesystem(
                    self._orphan_filesystems,
                    filesystem,
                )
                if cleanup_error is None:
                    cleanup_error = RecoveryLedgerError(
                        "tombstone filesystem close status is unknown"
                    )
                    cleanup_error.__cause__ = exc
            if cleanup_error is not None:
                owner = filesystem_owner
                if owner is None:
                    owner = _error_cleanup_owner(cleanup_error)
                _raise_with_cleanup(
                    body_error,
                    cleanup_error,
                    owner,
                    "tombstone bootstrap cleanup failed",
                )

    def _append_with_filesystem(
        self,
        record: RecoveryTombstoneRecord,
        *,
        allow_create: bool,
    ) -> RecoveryTombstoneRecord:
        self._retry_resources()
        if not _primary_is_present(self.state_root) or not _lifetime_gate_is_present(
            self.state_root
        ):
            return self._append_without_store(record, allow_create=allow_create)
        controller = WalSidecarController(
            self.state_root,
            busy_timeout_ms=self.busy_timeout_ms,
        )
        return _run_unowned_quiescence(
            controller,
            self.state_root,
            lambda root_fd: self._append_owned_at_root(
                root_fd,
                record,
                allow_create=allow_create,
                orphan_registry=self._orphan_fds,
            ),
            self._orphan_sessions,
            self._orphan_controllers,
        )

    def _append_owned_at_root(
        self,
        root_fd: int,
        record: RecoveryTombstoneRecord,
        *,
        allow_create: bool,
        orphan_registry: list[_OrphanFD] | None = None,
        retain_fd: _RetainFD | None = None,
    ) -> RecoveryTombstoneRecord:
        if orphan_registry is None:
            orphan_registry = self._orphan_fds
        result = _append_owned_file(
            root_fd=root_fd,
            name=RECOVERY_TOMBSTONES_BASENAME,
            record=record,
            allow_create=allow_create,
            encode=lambda value: _encode_tombstone(
                cast(RecoveryTombstoneRecord, value)
            ),
            latest_from_bytes=lambda raw, allow_empty: _latest_tombstone(
                raw,
                allow_empty=allow_empty,
            ),
            validate_append=lambda value, latest: _validate_tombstone_append(
                cast(RecoveryTombstoneRecord, value),
                cast(RecoveryTombstoneRecord | None, latest),
            ),
            same_record=lambda value, latest: _same_tombstone(
                cast(RecoveryTombstoneRecord, value),
                cast(RecoveryTombstoneRecord, latest),
            ),
            fault=self._fault,
            orphan_registry=orphan_registry,
            retain_fd=retain_fd,
        )
        _validate_root_descriptor(root_fd, self.state_root)
        return cast(RecoveryTombstoneRecord, result)

    def _open_filesystem(self) -> StateFilesystem:
        try:
            return StateFilesystem.open_existing(
                self.state_root,
                marker_name=self.marker_name,
                ledger_name=RECOVERY_TOMBSTONES_BASENAME,
                busy_timeout_ms=self.busy_timeout_ms,
            )
        except (_doctor.DoctorError, OSError, ValueError) as exc:
            raise _recovery_filesystem_error(
                "tombstone filesystem is unavailable", exc
            ) from exc

    def _fault(self, point: str) -> None:
        """Deterministic process-test seam; production implementation is a no-op."""


class RestoreIdentityTombstoneLog(RecoveryTombstoneLog):
    """Descriptive public name for the restore identity tombstone log."""


_RESTORE_LEDGER_PHASES: Final[tuple[LedgerPhase, ...]] = (
    "RESTORE_PREPARED",
    "RESTORE_REPLACED",
    "RESTORE_COMMITTED",
    "RESTORE_ABORTED",
)
_RESTORE_HANDLE_SENTINEL = object()


@dataclass(frozen=True, slots=True, init=False)
class RestoreHandle:
    """Opaque generation identity returned by the owned restore ledger."""

    restore_generation: int
    sequence: int
    tombstone_sequence: int
    recovery_epoch: int
    fencing_token_floor: int
    phase: LedgerPhase
    tombstone_phase: TombstonePhase
    backup_digest: str
    previous_primary_digest: str
    candidate_digest: str
    previous_recovery_epoch: int
    previous_fencing_token_hwm: int
    previous_last_clock_ns: int
    identities: tuple[RestoreIdentity, ...]
    actor: str
    audit_ref: str
    _provenance: object = field(default=None, repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("RestoreHandle instances are ledger-issued")


def _validate_floor(floor: object) -> tuple[int, int]:
    if type(floor) is not RecoveryFloor:
        raise RecoveryLedgerError("restore floor must be an exact RecoveryFloor")
    try:
        recovery_epoch = object.__getattribute__(floor, "recovery_epoch")
        fencing_token_floor = object.__getattribute__(floor, "fencing_token_floor")
    except AttributeError as exc:
        raise RecoveryLedgerError("restore floor fields are unavailable") from exc
    if type(recovery_epoch) is not int or type(fencing_token_floor) is not int:
        raise RecoveryLedgerError("restore floor fields must be exact integers")
    if recovery_epoch < 0 or fencing_token_floor < 0:
        raise RecoveryLedgerError("restore floor fields are invalid")
    return recovery_epoch, fencing_token_floor


def _require_floor_at_least(
    recovery_epoch: int,
    fencing_token_floor: int,
    ledger: RecoveryLedgerRecord,
) -> None:
    if (
        recovery_epoch < ledger.recovery_epoch
        or fencing_token_floor < ledger.fencing_token_floor
    ):
        raise RecoveryLedgerError("restore floor moved backwards")


def _issue_restore_handle(
    ledger: RecoveryLedgerRecord,
    tombstone: RecoveryTombstoneRecord,
) -> RestoreHandle:
    if ledger.restore_generation != tombstone.restore_generation:
        raise RecoveryLedgerError("restore generation does not match")
    if ledger.backup_digest != tombstone.backup_digest:
        raise RecoveryLedgerError("restore backup digest does not match")
    _require_restore_phase_pair(ledger.phase, tombstone.phase)
    if ledger.actor != tombstone.actor or ledger.audit_ref != tombstone.audit_ref:
        raise RecoveryLedgerError("restore record actors do not match")
    instance = object.__new__(RestoreHandle)
    values: dict[str, object] = {
        "restore_generation": ledger.restore_generation,
        "sequence": ledger.sequence,
        "tombstone_sequence": tombstone.sequence,
        "recovery_epoch": ledger.recovery_epoch,
        "fencing_token_floor": ledger.fencing_token_floor,
        "phase": ledger.phase,
        "tombstone_phase": tombstone.phase,
        "backup_digest": tombstone.backup_digest,
        "previous_primary_digest": tombstone.previous_primary_digest,
        "candidate_digest": tombstone.candidate_digest,
        "previous_recovery_epoch": tombstone.previous_recovery_epoch,
        "previous_fencing_token_hwm": tombstone.previous_fencing_token_hwm,
        "previous_last_clock_ns": tombstone.previous_last_clock_ns,
        "identities": tombstone.identities,
        "actor": tombstone.actor,
        "audit_ref": tombstone.audit_ref,
        "_provenance": _RESTORE_HANDLE_SENTINEL,
    }
    for name, value in values.items():
        object.__setattr__(instance, name, value)
    _validate_restore_handle(instance)
    return instance


def _validate_restore_handle(handle: object) -> RestoreHandle:
    if type(handle) is not RestoreHandle:
        raise RecoveryLedgerError("restore handle has an unsupported type")
    try:
        provenance = object.__getattribute__(handle, "_provenance")
        values = tuple(
            object.__getattribute__(handle, name)
            for name in (
                "restore_generation",
                "sequence",
                "tombstone_sequence",
                "recovery_epoch",
                "fencing_token_floor",
                "phase",
                "tombstone_phase",
                "backup_digest",
                "previous_primary_digest",
                "candidate_digest",
                "previous_recovery_epoch",
                "previous_fencing_token_hwm",
                "previous_last_clock_ns",
                "identities",
                "actor",
                "audit_ref",
            )
        )
    except AttributeError as exc:
        raise RecoveryLedgerError("restore handle fields are unavailable") from exc
    if provenance is not _RESTORE_HANDLE_SENTINEL:
        raise RecoveryLedgerError("restore handle is unissued")
    if any(type(values[index]) is not int for index in (0, 1, 2, 3, 4, 10, 11, 12)):
        raise RecoveryLedgerError("restore handle counters are invalid")
    if values[0] < 1 or values[1] < 1 or values[2] < 1:
        raise RecoveryLedgerError("restore handle counters are invalid")
    if values[3] < 0 or values[4] < 0:
        raise RecoveryLedgerError("restore handle floor is invalid")
    if values[10] < 0 or values[11] < 0 or values[12] < 0:
        raise RecoveryLedgerError("restore handle previous HWM is invalid")
    if type(values[5]) is not str or values[5] not in _RESTORE_LEDGER_PHASES:
        raise RecoveryLedgerError("restore handle phase is invalid")
    if type(values[6]) is not str or values[6] not in _TOMBSTONE_PHASES:
        raise RecoveryLedgerError("restore handle tombstone phase is invalid")
    for index, name in (
        (7, "backup_digest"),
        (8, "previous_primary_digest"),
        (9, "candidate_digest"),
    ):
        _digest(values[index], name)
    if type(values[13]) is not tuple:
        raise RecoveryLedgerError("restore handle identities are invalid")
    _ = _restore_identity_values_tuple(cast(tuple[RestoreIdentity, ...], values[13]))
    _identifier(values[14], "actor")
    _identifier(values[15], "audit_ref")
    return handle


_NORMAL_OPEN_RECOVERY_STATE_SENTINEL = object()


@dataclass(frozen=True, slots=True, init=False)
class NormalOpenRecoveryState:
    """Immutable, Recovery-issued history state for normal store opening."""

    active_committed_tombstones: tuple[RestoreIdentity, ...]
    latest_committed_handle: RestoreHandle | None
    _provenance: object = field(default=None, repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("NormalOpenRecoveryState instances are recovery-issued")

    def __copy__(self) -> NoReturn:
        raise TypeError("NormalOpenRecoveryState instances cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        raise TypeError("NormalOpenRecoveryState instances cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("NormalOpenRecoveryState instances cannot be pickled")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        raise TypeError("NormalOpenRecoveryState instances cannot be pickled")

    def active_committed_identities(self) -> frozenset[tuple[str, str]]:
        _validate_normal_open_recovery_state(self)
        return frozenset(
            _restore_identity_values(identity)
            for identity in self.active_committed_tombstones
        )


def _normal_open_identity_values(
    identities: object,
) -> tuple[tuple[str, str], ...]:
    if type(identities) is not tuple:
        raise RecoveryLedgerError("normal open committed tombstones must be a tuple")
    try:
        values = tuple(_restore_identity_values(identity) for identity in identities)
    except (TypeError, ValueError) as exc:
        raise RecoveryLedgerError(
            "normal open committed tombstones are invalid"
        ) from exc
    if values != tuple(sorted(set(values))):
        raise RecoveryLedgerError("normal open committed tombstones are not canonical")
    return values


def _validate_normal_open_recovery_state(
    state: object,
) -> NormalOpenRecoveryState:
    if type(state) is not NormalOpenRecoveryState:
        raise RecoveryLedgerError("normal open recovery state has an unsupported type")
    try:
        provenance = object.__getattribute__(state, "_provenance")
        identities = object.__getattribute__(
            state,
            "active_committed_tombstones",
        )
        latest = object.__getattribute__(state, "latest_committed_handle")
    except AttributeError as exc:
        raise RecoveryLedgerError(
            "normal open recovery state fields are unavailable"
        ) from exc
    if provenance is not _NORMAL_OPEN_RECOVERY_STATE_SENTINEL:
        raise RecoveryLedgerError("normal open recovery state is unissued")
    _normal_open_identity_values(identities)
    if latest is not None:
        _validate_restore_handle(latest)
        if latest.phase != "RESTORE_COMMITTED" or latest.tombstone_phase != "COMMITTED":
            raise RecoveryLedgerError(
                "normal open latest committed handle is not committed"
            )
    return state


def _issue_normal_open_recovery_state(
    active_committed_keys: frozenset[tuple[str, str]],
    latest_committed_handle: RestoreHandle | None,
) -> NormalOpenRecoveryState:
    identities = tuple(
        RestoreIdentity(operation_id=operation_id, effect_key=effect_key)
        for operation_id, effect_key in sorted(active_committed_keys)
    )
    _normal_open_identity_values(identities)
    if latest_committed_handle is not None:
        _validate_restore_handle(latest_committed_handle)
        if (
            latest_committed_handle.phase != "RESTORE_COMMITTED"
            or latest_committed_handle.tombstone_phase != "COMMITTED"
        ):
            raise RecoveryLedgerError(
                "normal open latest committed handle is not committed"
            )
    instance = object.__new__(NormalOpenRecoveryState)
    object.__setattr__(instance, "active_committed_tombstones", identities)
    object.__setattr__(
        instance,
        "latest_committed_handle",
        latest_committed_handle,
    )
    object.__setattr__(
        instance,
        "_provenance",
        _NORMAL_OPEN_RECOVERY_STATE_SENTINEL,
    )
    return instance


def _read_restore_pair(
    root_fd: int,
    *,
    orphan_registry: list[_OrphanFD] | None = None,
    retain_fd: _RetainFD | None = None,
) -> tuple[
    tuple[RecoveryLedgerRecord, ...],
    RecoveryLedgerRecord | None,
    RecoveryTombstoneRecord | None,
    tuple[RecoveryTombstoneRecord, ...],
]:
    ledger_raw, _ = _read_root_file(
        root_fd,
        RECOVERY_LEDGER_BASENAME,
        orphan_registry=orphan_registry,
        retain_fd=retain_fd,
    )
    tombstone_raw, _ = _read_root_file(
        root_fd,
        RECOVERY_TOMBSTONES_BASENAME,
        orphan_registry=orphan_registry,
        retain_fd=retain_fd,
    )
    try:
        if ledger_raw is None:
            ledger_records: tuple[RecoveryLedgerRecord, ...] = ()
        else:
            ledger_maps = _doctor._ledger_records(ledger_raw)
            ledger_snapshots = tuple(
                _doctor._ledger_snapshot(item) for item in ledger_maps
            )
            RecoveryLedgerWriter._latest_from_bytes(
                ledger_raw,
                allow_empty=False,
            )
            ledger_records = tuple(
                _record_from_snapshot(snapshot) for snapshot in ledger_snapshots
            )
    except _doctor.DoctorError as exc:
        raise RecoveryLedgerError("recovery ledger is malformed") from exc
    ledger = ledger_records[-1] if ledger_records else None
    tombstone_records = (
        () if tombstone_raw is None else _tombstone_records(tombstone_raw)
    )
    tombstone = (
        None
        if tombstone_raw is None
        else _latest_tombstone(tombstone_raw, allow_empty=False)
    )
    return ledger_records, ledger, tombstone, tombstone_records


def _require_persisted_floor_above_previous_hwm(
    ledger: RecoveryLedgerRecord,
    tombstone: RecoveryTombstoneRecord,
) -> None:
    if (
        ledger.recovery_epoch <= tombstone.previous_recovery_epoch
        or ledger.fencing_token_floor <= tombstone.previous_fencing_token_hwm
    ):
        raise RecoveryLedgerError(
            "restore ledger floor is not above previous destination high water mark"
        )


def _validate_restore_histories(
    ledger_records: tuple[RecoveryLedgerRecord, ...],
    tombstone_records: tuple[RecoveryTombstoneRecord, ...],
) -> frozenset[tuple[str, str]]:
    if not ledger_records or not tombstone_records:
        raise RecoveryLedgerError("recovery ledger and tombstone pair is incomplete")
    ledger_generations = tuple(
        sorted({record.restore_generation for record in ledger_records})
    )
    tombstone_generations = tuple(
        sorted({record.restore_generation for record in tombstone_records})
    )
    if ledger_generations != tombstone_generations or ledger_generations[0] != 1:
        raise RecoveryLedgerError("restore generation history is incomplete")
    if any(right != left + 1 for left, right in pairwise(ledger_generations)):
        raise RecoveryLedgerError("restore generation history has a gap")
    for generation in ledger_generations:
        ledger_group = tuple(
            record
            for record in ledger_records
            if record.restore_generation == generation
        )
        tombstone_group = tuple(
            record
            for record in tombstone_records
            if record.restore_generation == generation
        )
        ledger_prepared = tuple(
            record for record in ledger_group if record.phase == "RESTORE_PREPARED"
        )
        ledger_terminal = tuple(
            record
            for record in ledger_group
            if record.phase in {"RESTORE_COMMITTED", "RESTORE_ABORTED"}
        )
        tombstone_prepared = tuple(
            record for record in tombstone_group if record.phase == "PREPARED"
        )
        tombstone_terminal = tuple(
            record
            for record in tombstone_group
            if record.phase in {"COMMITTED", "ABORTED"}
        )
        if (
            len(ledger_prepared) != 1
            or len(ledger_terminal) != 1
            or len(tombstone_prepared) != 1
            or len(tombstone_terminal) != 1
        ):
            raise RecoveryLedgerError("restore generation phase history is incomplete")
        prepared_ledger = ledger_prepared[0]
        terminal_ledger = ledger_terminal[0]
        prepared_tombstone = tombstone_prepared[0]
        terminal_tombstone = tombstone_terminal[0]
        for record in ledger_group:
            _require_persisted_floor_above_previous_hwm(
                record,
                prepared_tombstone,
            )
        try:
            _require_restore_phase_pair(
                terminal_ledger.phase,
                terminal_tombstone.phase,
            )
        except RecoveryLedgerError as exc:
            raise RecoveryLedgerError(
                "restore generation histories are inconsistent"
            ) from exc
        if (
            terminal_ledger.sequence <= prepared_ledger.sequence
            or terminal_tombstone.sequence <= prepared_tombstone.sequence
            or prepared_ledger.backup_digest != prepared_tombstone.backup_digest
            or prepared_ledger.actor != prepared_tombstone.actor
            or prepared_ledger.audit_ref != prepared_tombstone.audit_ref
            or terminal_ledger.backup_digest != terminal_tombstone.backup_digest
            or terminal_ledger.actor != terminal_tombstone.actor
            or terminal_ledger.audit_ref != terminal_tombstone.audit_ref
            or prepared_ledger.actor != terminal_ledger.actor
            or prepared_ledger.audit_ref != terminal_ledger.audit_ref
            or prepared_tombstone.actor != terminal_tombstone.actor
            or prepared_tombstone.audit_ref != terminal_tombstone.audit_ref
            or prepared_tombstone.previous_recovery_epoch
            != terminal_tombstone.previous_recovery_epoch
            or prepared_tombstone.previous_fencing_token_hwm
            != terminal_tombstone.previous_fencing_token_hwm
            or prepared_tombstone.previous_last_clock_ns
            != terminal_tombstone.previous_last_clock_ns
        ):
            raise RecoveryLedgerError("restore generation histories are inconsistent")
        if terminal_ledger.phase == "RESTORE_ABORTED" and (
            len(ledger_group) < 2 or ledger_group[-2].phase != "RESTORE_PREPARED"
        ):
            raise RecoveryLedgerError("restore abort predecessor is invalid")
        if terminal_tombstone.phase == "ABORTED" and (
            len(tombstone_group) < 2 or tombstone_group[-2].phase != "PREPARED"
        ):
            raise RecoveryLedgerError("tombstone abort predecessor is invalid")
        if any(
            record.backup_digest != prepared_ledger.backup_digest
            for record in ledger_group
        ):
            raise RecoveryLedgerError("ledger generation digest is inconsistent")
    return frozenset(
        identity
        for record in tombstone_records
        if record.phase == "COMMITTED"
        for identity in _restore_identity_values_tuple(record.identities)
    )


def _validate_completed_restore_history(
    ledger_records: tuple[RecoveryLedgerRecord, ...],
    tombstone_records: tuple[RecoveryTombstoneRecord, ...],
    through_generation: int,
) -> None:
    """Validate the terminal prefix before a separately resumable generation."""

    ledger_prefix = tuple(
        record
        for record in ledger_records
        if record.restore_generation <= through_generation
    )
    tombstone_prefix = tuple(
        record
        for record in tombstone_records
        if record.restore_generation <= through_generation
    )
    if through_generation < 1:
        if ledger_prefix or tombstone_prefix:
            raise RecoveryLedgerError("restore generation history has a gap")
        return
    if not ledger_prefix or not tombstone_prefix:
        raise RecoveryLedgerError("restore generation history is incomplete")
    ledger_generations = {record.restore_generation for record in ledger_prefix}
    tombstone_generations = {record.restore_generation for record in tombstone_prefix}
    if (
        max(ledger_generations) != through_generation
        or max(tombstone_generations) != through_generation
    ):
        raise RecoveryLedgerError("restore generation history has a gap")
    _validate_restore_histories(ledger_prefix, tombstone_prefix)


def _validate_restore_pair(
    ledger_records: tuple[RecoveryLedgerRecord, ...],
    ledger: RecoveryLedgerRecord | None,
    tombstone: RecoveryTombstoneRecord | None,
    tombstone_records: tuple[RecoveryTombstoneRecord, ...],
) -> frozenset[tuple[str, str]]:
    """Validate all completed history and the current pair before use."""

    if ledger is None and tombstone is None:
        return frozenset()
    if ledger is None or tombstone is None:
        raise RecoveryLedgerError("recovery ledger and tombstone pair is incomplete")
    if ledger.restore_generation != tombstone.restore_generation:
        raise RecoveryLedgerError("recovery ledger and tombstone generations differ")
    _require_persisted_floor_above_previous_hwm(ledger, tombstone)
    try:
        _require_restore_phase_pair(ledger.phase, tombstone.phase)
    except RecoveryLedgerError as exc:
        raise RecoveryLedgerError("current restore pair is inconsistent") from exc
    generation = ledger.restore_generation
    if ledger.phase in {"RESTORE_COMMITTED", "RESTORE_ABORTED"} and tombstone.phase in {
        "COMMITTED",
        "ABORTED",
    }:
        return _validate_restore_histories(ledger_records, tombstone_records)
    _validate_completed_restore_history(
        ledger_records,
        tombstone_records,
        generation - 1,
    )
    ledger_group = tuple(
        record for record in ledger_records if record.restore_generation == generation
    )
    tombstone_group = tuple(
        record
        for record in tombstone_records
        if record.restore_generation == generation
    )
    prepared_ledger = tuple(
        record for record in ledger_group if record.phase == "RESTORE_PREPARED"
    )
    prepared_tombstone = tuple(
        record for record in tombstone_group if record.phase == "PREPARED"
    )
    if len(prepared_ledger) != 1 or len(prepared_tombstone) != 1:
        raise RecoveryLedgerError("current restore generation is incomplete")
    ledger_prepared = prepared_ledger[0]
    tombstone_prepared = prepared_tombstone[0]
    if (
        ledger_prepared.backup_digest != tombstone_prepared.backup_digest
        or ledger_prepared.actor != tombstone_prepared.actor
        or ledger_prepared.audit_ref != tombstone_prepared.audit_ref
    ):
        raise RecoveryLedgerError("current restore pair is inconsistent")
    return frozenset(
        identity
        for record in tombstone_records
        if record.restore_generation < generation and record.phase == "COMMITTED"
        for identity in _restore_identity_values_tuple(record.identities)
    )


RestoreOrphanKind = Literal["TOMBSTONE_FIRST_INITIAL", "TOMBSTONE_FIRST_NEXT"]
_RESTORE_ORPHAN_SENTINEL = object()


@dataclass(frozen=True, slots=True, init=False)
class RestoreTombstoneOrphan:
    """Validated restore-only state where the tombstone append won first."""

    kind: RestoreOrphanKind
    tombstone: RecoveryTombstoneRecord
    active_identities: frozenset[tuple[str, str]]
    _provenance: object = field(default=None, repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("RestoreTombstoneOrphan instances are ledger-issued")


def _issue_restore_tombstone_orphan(
    kind: RestoreOrphanKind,
    tombstone: RecoveryTombstoneRecord,
    active_identities: frozenset[tuple[str, str]],
) -> RestoreTombstoneOrphan:
    instance = object.__new__(RestoreTombstoneOrphan)
    for name, value in {
        "kind": kind,
        "tombstone": tombstone,
        "active_identities": active_identities,
        "_provenance": _RESTORE_ORPHAN_SENTINEL,
    }.items():
        object.__setattr__(instance, name, value)
    return instance


def _validate_restore_tombstone_orphan(
    state: object,
) -> RestoreTombstoneOrphan:
    if type(state) is not RestoreTombstoneOrphan:
        raise RecoveryLedgerError("restore orphan has an unsupported type")
    try:
        provenance = object.__getattribute__(state, "_provenance")
        kind = object.__getattribute__(state, "kind")
        tombstone = object.__getattribute__(state, "tombstone")
        active_identities = object.__getattribute__(state, "active_identities")
    except AttributeError as exc:
        raise RecoveryLedgerError("restore orphan fields are unavailable") from exc
    if provenance is not _RESTORE_ORPHAN_SENTINEL:
        raise RecoveryLedgerError("restore orphan is unissued")
    if kind not in {"TOMBSTONE_FIRST_INITIAL", "TOMBSTONE_FIRST_NEXT"}:
        raise RecoveryLedgerError("restore orphan kind is invalid")
    canonical = _canonical_tombstone(tombstone)
    if not _same_tombstone(canonical, tombstone):
        raise RecoveryLedgerError("restore orphan tombstone is not canonical")
    if type(active_identities) is not frozenset or any(
        type(item) is not tuple
        or len(item) != 2
        or any(type(value) is not str for value in item)
        for item in active_identities
    ):
        raise RecoveryLedgerError("restore orphan identities are invalid")
    return state


def _classify_tombstone_first_orphan(
    ledger_records: tuple[RecoveryLedgerRecord, ...],
    ledger: RecoveryLedgerRecord | None,
    tombstone: RecoveryTombstoneRecord | None,
    tombstone_records: tuple[RecoveryTombstoneRecord, ...],
) -> RestoreTombstoneOrphan:
    if tombstone is None:
        raise RecoveryLedgerError("restore orphan tombstone is missing")
    if ledger is None:
        if (
            len(tombstone_records) != 1
            or tombstone.sequence != 1
            or tombstone.phase != "PREPARED"
            or tombstone.restore_generation != 1
        ):
            raise RecoveryLedgerError("orphan tombstone history is unsafe")
        return _issue_restore_tombstone_orphan(
            "TOMBSTONE_FIRST_INITIAL",
            tombstone,
            frozenset(),
        )
    if (
        ledger.phase not in _TERMINAL_PHASES
        or tombstone.phase != "PREPARED"
        or tombstone.restore_generation != ledger.restore_generation + 1
        or len(tombstone_records) < 2
    ):
        raise RecoveryLedgerError("orphan tombstone history is unsafe")
    previous_tombstone = tombstone_records[-2]
    if (
        previous_tombstone.phase not in {"COMMITTED", "ABORTED"}
        or previous_tombstone.restore_generation != ledger.restore_generation
        or tombstone.sequence != previous_tombstone.sequence + 1
    ):
        raise RecoveryLedgerError("orphan tombstone history is unsafe")
    prefix_ledger = tuple(
        record
        for record in ledger_records
        if record.restore_generation <= ledger.restore_generation
    )
    prefix_tombstones = tuple(
        record
        for record in tombstone_records
        if record.restore_generation <= ledger.restore_generation
    )
    active_identities = _validate_restore_histories(
        prefix_ledger,
        prefix_tombstones,
    )
    return _issue_restore_tombstone_orphan(
        "TOMBSTONE_FIRST_NEXT",
        tombstone,
        active_identities,
    )


@dataclass(slots=True)
class _LockedRecoveryFile:
    name: str
    fd: int
    identity: tuple[int, int] | None = None
    signature: tuple[int, ...] | None = None
    locked: bool = False
    raw: bytes | None = None


@dataclass(frozen=True, slots=True)
class _DurableRestorePairObservation:
    ledger_records: tuple[RecoveryLedgerRecord, ...]
    ledger: RecoveryLedgerRecord | None
    tombstone: RecoveryTombstoneRecord | None
    tombstone_records: tuple[RecoveryTombstoneRecord, ...]
    state: RestoreHandle | RestoreTombstoneOrphan | None


def _parse_restore_pair_bytes(
    ledger_raw: bytes | None,
    tombstone_raw: bytes | None,
) -> tuple[
    tuple[RecoveryLedgerRecord, ...],
    RecoveryLedgerRecord | None,
    RecoveryTombstoneRecord | None,
    tuple[RecoveryTombstoneRecord, ...],
]:
    if ledger_raw is None:
        ledger_records: tuple[RecoveryLedgerRecord, ...] = ()
    else:
        try:
            ledger_maps = _doctor._ledger_records(ledger_raw)
            ledger_snapshots = tuple(
                _doctor._ledger_snapshot(item) for item in ledger_maps
            )
            RecoveryLedgerWriter._latest_from_bytes(
                ledger_raw,
                allow_empty=False,
            )
            ledger_records = tuple(
                _record_from_snapshot(snapshot) for snapshot in ledger_snapshots
            )
        except _doctor.DoctorError as exc:
            raise RecoveryLedgerError("recovery ledger is malformed") from exc
        except (TypeError, ValueError) as exc:
            raise RecoveryLedgerError("recovery ledger is malformed") from exc
    ledger = ledger_records[-1] if ledger_records else None
    if tombstone_raw is None:
        tombstone_records: tuple[RecoveryTombstoneRecord, ...] = ()
    else:
        tombstone_records = _tombstone_records(tombstone_raw)
    tombstone = (
        None
        if not tombstone_records
        else _latest_tombstone(tombstone_raw or b"", allow_empty=False)
    )
    return ledger_records, ledger, tombstone, tombstone_records


def _classify_restore_pair_records(
    ledger_records: tuple[RecoveryLedgerRecord, ...],
    ledger: RecoveryLedgerRecord | None,
    tombstone: RecoveryTombstoneRecord | None,
    tombstone_records: tuple[RecoveryTombstoneRecord, ...],
) -> RestoreHandle | RestoreTombstoneOrphan | None:
    if ledger is None and tombstone is None:
        return None
    if ledger is not None and tombstone is not None:
        if ledger.restore_generation == tombstone.restore_generation:
            _validate_restore_pair(
                ledger_records,
                ledger,
                tombstone,
                tombstone_records,
            )
            return _pair_handle(ledger, tombstone)
        return _classify_tombstone_first_orphan(
            ledger_records,
            ledger,
            tombstone,
            tombstone_records,
        )
    return _classify_tombstone_first_orphan(
        ledger_records,
        ledger,
        tombstone,
        tombstone_records,
    )


def _validate_restore_pair_file_set(
    root_fd: int,
    files: list[_LockedRecoveryFile],
    *,
    stage: str,
) -> None:
    """Check that the locked existing-only recovery set is still unchanged."""

    by_name = {current.name: current for current in files}
    for name in (RECOVERY_TOMBSTONES_BASENAME, RECOVERY_LEDGER_BASENAME):
        current = by_name.get(name)
        try:
            path_metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            if current is not None:
                raise RecoveryDurabilityError(
                    f"recovery file {name} disappeared during {stage}"
                )
            continue
        except _CLEANUP_EXCEPTION as exc:
            raise RecoveryDurabilityError(
                f"recovery file {name} identity is unknown during {stage}"
            ) from exc
        if current is None:
            raise RecoveryDurabilityError(
                f"recovery file {name} appeared during {stage}"
            )
        try:
            descriptor_metadata = os.fstat(current.fd)
        except _CLEANUP_EXCEPTION as exc:
            raise RecoveryDurabilityError(
                f"recovery file {name} descriptor identity is unknown during {stage}"
            ) from exc
        if (
            current.identity is None
            or current.signature is None
            or _store._identity(descriptor_metadata) != current.identity
            or _metadata_for_entry(descriptor_metadata) != current.signature
            or _metadata_for_entry(path_metadata) != current.signature
        ):
            raise RecoveryDurabilityError(
                f"recovery file {name} identity changed during {stage}"
            )


@contextmanager
def _locked_restore_files(
    root_fd: int,
    retain_fd: _RetainFD | None,
    orphan_registry: list[_OrphanFD] | None = None,
) -> Iterator[list[_LockedRecoveryFile]]:
    files: list[_LockedRecoveryFile] = []
    try:
        for name in (RECOVERY_TOMBSTONES_BASENAME, RECOVERY_LEDGER_BASENAME):
            try:
                before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise RecoveryLedgerError("recovery file cannot be inspected") from exc
            _validate_private_regular(before, name)
            try:
                flags = _store._open_flags(directory=False, writable=False)
                fd = os.open(name, flags, dir_fd=root_fd)
            except _store.StoreError as exc:
                raise RecoveryLedgerError(
                    "secure recovery read is unavailable"
                ) from exc
            except OSError as exc:
                raise RecoveryLedgerError("recovery file cannot be opened") from exc
            current = _LockedRecoveryFile(name=name, fd=fd)
            files.append(current)
            try:
                metadata = os.fstat(fd)
            except _CLEANUP_EXCEPTION as exc:
                raise RecoveryLedgerError(
                    f"recovery file {name} descriptor is unavailable"
                ) from exc
            current.identity = _store._identity(metadata)
            current.signature = _metadata_for_entry(metadata)
            _validate_private_regular(metadata, name)
            if current.signature != _metadata_for_entry(before):
                raise RecoveryLedgerError("recovery file changed while opening")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise RecoveryLedgerError("recovery file is busy") from exc
                raise RecoveryLedgerError("recovery file cannot be locked") from exc
            current.locked = True
            try:
                after = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except _CLEANUP_EXCEPTION as exc:
                raise RecoveryLedgerError(
                    f"recovery file {name} cannot be revalidated"
                ) from exc
            if _metadata_for_entry(after) != current.signature:
                raise RecoveryLedgerError("recovery file changed while locked")
        yield files
    finally:
        body_error = sys.exc_info()[1]
        cleanup_error: BaseException | None = None
        cleanup_owner: _CleanupCapability | None = None
        for current in reversed(files):
            expected_identity = current.identity
            try:
                metadata = os.fstat(current.fd)
            except _CLEANUP_EXCEPTION as exc:
                if isinstance(exc, OSError) and exc.errno == errno.EBADF:
                    continue
                try:
                    current_owner = _retain_failed_fd_with_fallback(
                        orphan_registry,
                        retain_fd,
                        current.fd,
                        expected_identity,
                        f"recovery file {current.name} durability",
                    )
                except _CLEANUP_EXCEPTION as retention_error:
                    current_owner = _error_cleanup_owner(retention_error)
                    local_error = retention_error
                else:
                    local_error = RecoveryLedgerError(
                        f"recovery file {current.name} descriptor status is unknown"
                    )
                    local_error.__cause__ = exc
                if current_owner is not None:
                    if cleanup_owner is None:
                        cleanup_owner = current_owner
                    elif isinstance(cleanup_owner, _CompositeCleanupCapability):
                        cleanup_owner.add(current_owner)
                    else:
                        cleanup_owner = _CompositeCleanupCapability(
                            cleanup_owner,
                            current_owner,
                        )
                cleanup_error = cleanup_error or local_error
                continue
            if expected_identity is None:
                try:
                    current_owner = _retain_failed_fd_with_fallback(
                        orphan_registry,
                        retain_fd,
                        current.fd,
                        None,
                        f"recovery file {current.name} durability",
                    )
                except _CLEANUP_EXCEPTION as retention_error:
                    current_owner = _error_cleanup_owner(retention_error)
                    local_error = retention_error
                else:
                    local_error = RecoveryLedgerError(
                        f"recovery file {current.name} descriptor identity is unavailable"
                    )
                if current_owner is not None:
                    if cleanup_owner is None:
                        cleanup_owner = current_owner
                    elif isinstance(cleanup_owner, _CompositeCleanupCapability):
                        cleanup_owner.add(current_owner)
                    else:
                        cleanup_owner = _CompositeCleanupCapability(
                            cleanup_owner,
                            current_owner,
                        )
                cleanup_error = cleanup_error or local_error
                continue
            if _store._identity(metadata) != expected_identity:
                local_error = RecoveryLedgerError(
                    f"recovery file {current.name} descriptor was reused"
                )
                cleanup_error = cleanup_error or local_error
                continue
            if current.locked:
                try:
                    fcntl.flock(current.fd, fcntl.LOCK_UN)
                except _CLEANUP_EXCEPTION as exc:
                    local_error = RecoveryLedgerError(
                        f"recovery file {current.name} unlock status is unknown"
                    )
                    local_error.__cause__ = exc
                    cleanup_error = cleanup_error or local_error
                else:
                    current.locked = False
            try:
                after_unlock = os.fstat(current.fd)
            except _CLEANUP_EXCEPTION as exc:
                if isinstance(exc, OSError) and exc.errno == errno.EBADF:
                    local_error = RecoveryLedgerError(
                        f"recovery file {current.name} disappeared during unlock"
                    )
                    local_error.__cause__ = exc
                    cleanup_error = cleanup_error or local_error
                    continue
                try:
                    current_owner = _retain_failed_fd_with_fallback(
                        orphan_registry,
                        retain_fd,
                        current.fd,
                        expected_identity,
                        f"recovery file {current.name} unlock",
                    )
                except _CLEANUP_EXCEPTION as retention_error:
                    current_owner = _error_cleanup_owner(retention_error)
                    local_error = retention_error
                else:
                    local_error = RecoveryLedgerError(
                        f"recovery file {current.name} identity is unknown after unlock"
                    )
                    local_error.__cause__ = exc
                cleanup_owner = _compose_cleanup_owners(
                    cleanup_owner,
                    current_owner,
                )
                cleanup_error = cleanup_error or local_error
                continue
            if _store._identity(after_unlock) != expected_identity:
                local_error = RecoveryLedgerError(
                    f"recovery file {current.name} descriptor was reused after unlock"
                )
                cleanup_error = cleanup_error or local_error
                continue
            try:
                os.close(current.fd)
            except _CLEANUP_EXCEPTION as exc:
                try:
                    current_owner = _retain_failed_fd_with_fallback(
                        orphan_registry,
                        retain_fd,
                        current.fd,
                        expected_identity,
                        f"recovery file {current.name} durability",
                    )
                except _CLEANUP_EXCEPTION as retention_error:
                    current_owner = _error_cleanup_owner(retention_error)
                    local_error = retention_error
                else:
                    local_error = RecoveryLedgerError(
                        f"recovery file {current.name} close status is unknown"
                    )
                    local_error.__cause__ = exc
                if current_owner is not None:
                    if cleanup_owner is None:
                        cleanup_owner = current_owner
                    elif isinstance(cleanup_owner, _CompositeCleanupCapability):
                        cleanup_owner.add(current_owner)
                    else:
                        cleanup_owner = _CompositeCleanupCapability(
                            cleanup_owner,
                            current_owner,
                        )
                cleanup_error = cleanup_error or local_error
        if cleanup_error is not None:
            _raise_with_cleanup(
                body_error,
                cleanup_error,
                cleanup_owner,
                "restore durability cleanup failed",
            )


def _durable_restore_pair_at_root(
    root_fd: int,
    state_root: Path,
    retain_fd: _RetainFD | None,
    orphan_registry: list[_OrphanFD] | None = None,
) -> _DurableRestorePairObservation:
    _validate_root_descriptor(root_fd, state_root)
    with _locked_restore_files(root_fd, retain_fd, orphan_registry) as files:
        by_name = {current.name: current for current in files}
        for current in files:
            try:
                current.raw = _read_fd_bytes(
                    current.fd,
                    f"recovery file {current.name}",
                )
            except _CLEANUP_EXCEPTION as exc:
                if isinstance(exc, RecoveryLedgerError):
                    raise
                raise RecoveryLedgerError(
                    f"recovery file {current.name} cannot be read"
                ) from exc
        ledger_raw = by_name.get(RECOVERY_LEDGER_BASENAME)
        tombstone_raw = by_name.get(RECOVERY_TOMBSTONES_BASENAME)
        ledger_bytes = None if ledger_raw is None else ledger_raw.raw
        tombstone_bytes = None if tombstone_raw is None else tombstone_raw.raw
        (
            ledger_records,
            ledger,
            tombstone,
            tombstone_records,
        ) = _parse_restore_pair_bytes(ledger_bytes, tombstone_bytes)
        state = _classify_restore_pair_records(
            ledger_records,
            ledger,
            tombstone,
            tombstone_records,
        )
        _validate_restore_pair_file_set(root_fd, files, stage="pre-fsync validation")
        for current in files:
            try:
                os.fsync(current.fd)
            except _CLEANUP_EXCEPTION as exc:
                raise RecoveryDurabilityError(
                    f"recovery file {current.name} durability is unknown"
                ) from exc
        try:
            os.fsync(root_fd)
        except _CLEANUP_EXCEPTION as exc:
            raise RecoveryDurabilityError(
                "recovery root durability is unknown"
            ) from exc
        for current in files:
            try:
                os.lseek(current.fd, 0, os.SEEK_SET)
                raw_after = _read_fd_bytes(
                    current.fd,
                    f"recovery file {current.name}",
                )
            except _CLEANUP_EXCEPTION as exc:
                raise RecoveryDurabilityError(
                    f"recovery file {current.name} readback is unknown"
                ) from exc
            if raw_after != current.raw:
                raise RecoveryDurabilityError(
                    f"recovery file {current.name} changed during durability barrier"
                )
            current.raw = raw_after
        try:
            _validate_restore_pair_file_set(root_fd, files, stage="readback")
            _validate_root_descriptor(root_fd, state_root)
            (
                final_ledger_records,
                final_ledger,
                final_tombstone,
                final_tombstone_records,
            ) = _parse_restore_pair_bytes(
                None if ledger_raw is None else ledger_raw.raw,
                None if tombstone_raw is None else tombstone_raw.raw,
            )
            final_state = _classify_restore_pair_records(
                final_ledger_records,
                final_ledger,
                final_tombstone,
                final_tombstone_records,
            )
        except RecoveryDurabilityError:
            raise
        except _CLEANUP_EXCEPTION as exc:
            raise RecoveryDurabilityError(
                "recovery pair changed during durability readback"
            ) from exc
        del state
        return _DurableRestorePairObservation(
            ledger_records=final_ledger_records,
            ledger=final_ledger,
            tombstone=final_tombstone,
            tombstone_records=final_tombstone_records,
            state=final_state,
        )


def _normal_open_preflight(
    root_fd: int,
    *,
    retain_fd: _RetainFD | None = None,
) -> NormalOpenRecoveryState:
    """Read restore artifacts existing-only before normal gate/DB open."""

    if type(root_fd) is not int:
        raise RecoveryLedgerError("state root descriptor is invalid")
    try:
        metadata = os.fstat(root_fd)
    except OSError as exc:
        raise RecoveryLedgerError("state root descriptor is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RecoveryLedgerError("state root descriptor is unsafe")
    ledger_records, ledger, tombstone, tombstone_records = _read_restore_pair(
        root_fd,
        retain_fd=retain_fd,
    )
    return _normal_open_recovery_state_from_history(
        ledger_records,
        ledger,
        tombstone,
        tombstone_records,
    )


def _pair_handle(
    ledger: RecoveryLedgerRecord | None,
    tombstone: RecoveryTombstoneRecord | None,
) -> RestoreHandle:
    if ledger is None or tombstone is None:
        raise RecoveryLedgerError("restore ledger and tombstone pair is incomplete")
    return _issue_restore_handle(ledger, tombstone)


def _latest_committed_restore_handle(
    ledger_records: tuple[RecoveryLedgerRecord, ...],
    tombstone_records: tuple[RecoveryTombstoneRecord, ...],
) -> RestoreHandle | None:
    generations = sorted(
        {record.restore_generation for record in ledger_records}
        | {record.restore_generation for record in tombstone_records}
    )
    for generation in reversed(generations):
        committed_ledger = tuple(
            record
            for record in ledger_records
            if record.restore_generation == generation
            and record.phase == "RESTORE_COMMITTED"
        )
        committed_tombstone = tuple(
            record
            for record in tombstone_records
            if record.restore_generation == generation and record.phase == "COMMITTED"
        )
        if len(committed_ledger) == 1 and len(committed_tombstone) == 1:
            return _pair_handle(committed_ledger[0], committed_tombstone[0])
    return None


def _normal_open_recovery_state_from_history(
    ledger_records: tuple[RecoveryLedgerRecord, ...],
    ledger: RecoveryLedgerRecord | None,
    tombstone: RecoveryTombstoneRecord | None,
    tombstone_records: tuple[RecoveryTombstoneRecord, ...],
) -> NormalOpenRecoveryState:
    if ledger is None and tombstone is None:
        return _issue_normal_open_recovery_state(frozenset(), None)
    if ledger is None or tombstone is None:
        raise RecoveryLedgerError("recovery ledger and tombstone pair is incomplete")
    _require_restore_phase_pair(ledger.phase, tombstone.phase)
    active_committed_keys = _validate_restore_histories(
        ledger_records,
        tombstone_records,
    )
    latest_committed_handle = _latest_committed_restore_handle(
        ledger_records,
        tombstone_records,
    )
    return _issue_normal_open_recovery_state(
        active_committed_keys,
        latest_committed_handle,
    )


class RestoreLedger:
    """Owner-aware high-level ledger/tombstone handle for restore phases."""

    __slots__ = ("__ledger", "__state_root", "__tombstones")

    def __init__(
        self,
        state_root: Path,
        *,
        marker_name: str = WRITER_MARKER_BASENAME,
        busy_timeout_ms: int = _store.DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        normalized_root = _doctor._coerce_root(state_root)
        ledger = RecoveryLedgerWriter(
            normalized_root,
            marker_name=marker_name,
            busy_timeout_ms=busy_timeout_ms,
        )
        tombstones = RecoveryTombstoneLog(
            normalized_root,
            marker_name=marker_name,
            busy_timeout_ms=busy_timeout_ms,
        )
        self.__state_root = normalized_root
        self.__ledger = ledger
        self.__tombstones = tombstones

    @property
    def state_root(self) -> Path:
        return self.__state_root

    @property
    def ledger(self) -> RecoveryLedgerWriter:
        return self.__ledger

    @property
    def tombstones(self) -> RecoveryTombstoneLog:
        return self.__tombstones

    def _assert_layout(self) -> None:
        if (
            type(self.__ledger) is not RecoveryLedgerWriter
            or type(self.__tombstones) is not RecoveryTombstoneLog
        ):
            raise RecoveryLedgerError("restore ledger layout is invalid")
        if (
            self.__ledger.state_root != self.__state_root
            or self.__tombstones.state_root != self.__state_root
            or self.__ledger.ledger_name != RECOVERY_LEDGER_BASENAME
            or self.__ledger.marker_name != WRITER_MARKER_BASENAME
            or self.__tombstones.marker_name != WRITER_MARKER_BASENAME
        ):
            raise RecoveryLedgerError("restore ledger layout is invalid")

    def read(self, owner: object) -> RestoreHandle | None:
        self._assert_layout()
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            ledger_records, ledger, tombstone, tombstone_records = _read_restore_pair(
                root_fd,
                retain_fd=retain_fd,
            )
            if ledger is None and tombstone is None:
                return None
            _validate_restore_pair(
                ledger_records,
                ledger,
                tombstone,
                tombstone_records,
            )
            return _pair_handle(ledger, tombstone)

    def read_for_resume(
        self,
        owner: object,
    ) -> RestoreHandle | RestoreTombstoneOrphan | None:
        """Redurabilize and read a strict pair or append-order orphan."""

        self._assert_layout()
        self.__ledger._retry_resources()
        self.__tombstones._retry_resources()
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            observation = _durable_restore_pair_at_root(
                root_fd,
                self.state_root,
                retain_fd,
                self.__ledger._orphan_fds,
            )
            return observation.state

    def normal_open_state(self, owner: object) -> NormalOpenRecoveryState:
        """Return durable committed history for a normal store-open guard."""

        self._assert_layout()
        self.__ledger._retry_resources()
        self.__tombstones._retry_resources()
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            observation = _durable_restore_pair_at_root(
                root_fd,
                self.state_root,
                retain_fd,
                self.__ledger._orphan_fds,
            )
        return _normal_open_recovery_state_from_history(
            observation.ledger_records,
            observation.ledger,
            observation.tombstone,
            observation.tombstone_records,
        )

    def complete_tombstone_first(
        self,
        state: RestoreTombstoneOrphan,
        *,
        floor_lower_bound: RecoveryFloor,
        owner: object,
    ) -> RestoreHandle:
        """Append only the missing prepared ledger record for a classified orphan."""

        state = _validate_restore_tombstone_orphan(state)
        current = self.read_for_resume(owner)
        if type(current) is not RestoreTombstoneOrphan or (
            current.kind != state.kind
            or not _same_tombstone(current.tombstone, state.tombstone)
            or current.active_identities != state.active_identities
        ):
            raise RecoveryLedgerError("restore orphan state is stale or mismatched")
        tombstone = state.tombstone
        return self.prepare(
            backup_digest=tombstone.backup_digest,
            previous_primary_digest=tombstone.previous_primary_digest,
            candidate_digest=tombstone.candidate_digest,
            identities=tombstone.identities,
            actor=tombstone.actor,
            audit_ref=tombstone.audit_ref,
            previous_recovery_epoch=tombstone.previous_recovery_epoch,
            previous_fencing_token_hwm=tombstone.previous_fencing_token_hwm,
            previous_last_clock_ns=tombstone.previous_last_clock_ns,
            floor_lower_bound=floor_lower_bound,
            owner=owner,
        )

    def read_owned(self, owner: object) -> RestoreHandle | None:
        return self.read(owner=owner)

    def prepare(
        self,
        *,
        backup_digest: str,
        previous_primary_digest: str,
        candidate_digest: str,
        identities: tuple[RestoreIdentity, ...],
        actor: str,
        audit_ref: str,
        previous_recovery_epoch: int,
        previous_fencing_token_hwm: int,
        previous_last_clock_ns: int,
        floor_lower_bound: RecoveryFloor,
        owner: object,
    ) -> RestoreHandle:
        self._assert_layout()
        backup_digest = _digest(backup_digest, "backup_digest")
        previous_primary_digest = _digest(
            previous_primary_digest,
            "previous_primary_digest",
        )
        candidate_digest = _digest(candidate_digest, "candidate_digest")
        actor = _identifier(actor, "actor")
        audit_ref = _identifier(audit_ref, "audit_ref")
        previous_recovery_epoch = _previous_destination_hwm(
            previous_recovery_epoch,
            "previous_recovery_epoch",
        )
        previous_fencing_token_hwm = _previous_destination_hwm(
            previous_fencing_token_hwm,
            "previous_fencing_token_hwm",
        )
        previous_last_clock_ns = _previous_destination_hwm(
            previous_last_clock_ns,
            "previous_last_clock_ns",
        )
        recovery_epoch, fencing_token_floor = _validate_floor(floor_lower_bound)
        if (
            recovery_epoch <= previous_recovery_epoch
            or fencing_token_floor <= previous_fencing_token_hwm
        ):
            raise RecoveryLedgerError(
                "restore floor is not above previous destination high water mark"
            )
        if type(identities) is not tuple:
            raise RecoveryLedgerError("identities must be a tuple")
        canonical_identities = tuple(
            RestoreIdentity(
                operation_id=_restore_identity_values(identity)[0],
                effect_key=_restore_identity_values(identity)[1],
            )
            for identity in identities
        )
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            ledger_records, ledger, tombstone, tombstone_records = _read_restore_pair(
                root_fd,
                retain_fd=retain_fd,
            )
            ledger_create = False
            ledger_append = False
            tombstone_create = False
            tombstone_append = False
            if ledger is not None or tombstone is not None:
                if ledger is not None and tombstone is not None:
                    if ledger.restore_generation == tombstone.restore_generation:
                        _validate_restore_pair(
                            ledger_records,
                            ledger,
                            tombstone,
                            tombstone_records,
                        )
                    if ledger.restore_generation != tombstone.restore_generation:
                        _validate_completed_restore_history(
                            ledger_records,
                            tombstone_records,
                            ledger.restore_generation,
                        )
                        previous_tombstone = (
                            tombstone_records[-2]
                            if len(tombstone_records) >= 2
                            else None
                        )
                        if not (
                            ledger.phase in {"RESTORE_COMMITTED", "RESTORE_ABORTED"}
                            and tombstone.phase == "PREPARED"
                            and previous_tombstone is not None
                            and previous_tombstone.phase in {"COMMITTED", "ABORTED"}
                            and previous_tombstone.restore_generation
                            == ledger.restore_generation
                            and tombstone.restore_generation
                            == ledger.restore_generation + 1
                            and tombstone.sequence == previous_tombstone.sequence + 1
                            and previous_tombstone.backup_digest == ledger.backup_digest
                            and previous_tombstone.actor == ledger.actor
                            and previous_tombstone.audit_ref == ledger.audit_ref
                            and tombstone.backup_digest == backup_digest
                            and tombstone.previous_primary_digest
                            == previous_primary_digest
                            and tombstone.candidate_digest == candidate_digest
                            and tombstone.previous_recovery_epoch
                            == previous_recovery_epoch
                            and tombstone.previous_fencing_token_hwm
                            == previous_fencing_token_hwm
                            and tombstone.previous_last_clock_ns
                            == previous_last_clock_ns
                            and _restore_identity_values_tuple(tombstone.identities)
                            == _restore_identity_values_tuple(canonical_identities)
                            and tombstone.actor == actor
                            and tombstone.audit_ref == audit_ref
                        ):
                            raise RecoveryLedgerError(
                                "restore generation resume evidence mismatches"
                            )
                        generation = tombstone.restore_generation
                        ledger_sequence = ledger.sequence + 1
                        tombstone_sequence = tombstone.sequence
                        ledger_append = True
                    else:
                        if ledger.phase in {
                            "RESTORE_COMMITTED",
                            "RESTORE_ABORTED",
                        } and tombstone.phase in {"COMMITTED", "ABORTED"}:
                            _validate_restore_histories(
                                ledger_records,
                                tombstone_records,
                            )
                        else:
                            _validate_completed_restore_history(
                                ledger_records,
                                tombstone_records,
                                ledger.restore_generation - 1,
                            )
                        current = _pair_handle(ledger, tombstone)
                        if (
                            current.backup_digest == backup_digest
                            and current.previous_primary_digest
                            == previous_primary_digest
                            and current.candidate_digest == candidate_digest
                            and current.previous_recovery_epoch
                            == previous_recovery_epoch
                            and current.previous_fencing_token_hwm
                            == previous_fencing_token_hwm
                            and current.previous_last_clock_ns == previous_last_clock_ns
                            and _restore_identity_values_tuple(current.identities)
                            == _restore_identity_values_tuple(canonical_identities)
                            and current.actor == actor
                            and current.audit_ref == audit_ref
                        ):
                            return current
                        if ledger.phase not in {
                            "RESTORE_COMMITTED",
                            "RESTORE_ABORTED",
                        }:
                            raise RecoveryLedgerError(
                                "restore generation is already pending"
                            )
                        if tombstone.phase not in {"COMMITTED", "ABORTED"}:
                            raise RecoveryLedgerError(
                                "tombstone generation is already pending"
                            )
                        generation = ledger.restore_generation + 1
                        ledger_sequence = ledger.sequence + 1
                        tombstone_sequence = tombstone.sequence + 1
                        ledger_append = True
                        tombstone_append = True
                elif tombstone is not None:
                    if tombstone.phase != "PREPARED":
                        raise RecoveryLedgerError("orphan terminal tombstone is unsafe")
                    if tombstone.sequence != 1 or tombstone.restore_generation != 1:
                        raise RecoveryLedgerError("recovery ledger history is missing")
                    generation = tombstone.restore_generation
                    ledger_sequence = 1
                    tombstone_sequence = tombstone.sequence
                    ledger_create = True
                else:
                    if ledger is None or ledger.phase != "RESTORE_PREPARED":
                        raise RecoveryLedgerError("orphan recovery ledger is unsafe")
                    if ledger.sequence != 1 or ledger.restore_generation != 1:
                        raise RecoveryLedgerError("tombstone history is missing")
                    if (
                        ledger.backup_digest != backup_digest
                        or ledger.actor != actor
                        or ledger.audit_ref != audit_ref
                    ):
                        raise RecoveryLedgerError("pending ledger request mismatches")
                    generation = ledger.restore_generation
                    ledger_sequence = ledger.sequence
                    tombstone_sequence = 1
                    tombstone_create = True
                if (
                    tombstone is not None
                    and tombstone.phase == "PREPARED"
                    and (
                        tombstone.restore_generation != generation
                        or tombstone.backup_digest != backup_digest
                        or tombstone.previous_primary_digest != previous_primary_digest
                        or tombstone.candidate_digest != candidate_digest
                        or tombstone.previous_recovery_epoch != previous_recovery_epoch
                        or tombstone.previous_fencing_token_hwm
                        != previous_fencing_token_hwm
                        or tombstone.previous_last_clock_ns != previous_last_clock_ns
                        or _restore_identity_values_tuple(tombstone.identities)
                        != _restore_identity_values_tuple(canonical_identities)
                        or tombstone.actor != actor
                        or tombstone.audit_ref != audit_ref
                    )
                ):
                    raise RecoveryLedgerError("pending tombstone request mismatches")
            else:
                generation = 1
                ledger_sequence = 1
                tombstone_sequence = 1
                ledger_create = True
                tombstone_create = True
            ledger_record = RecoveryLedgerRecord(
                version=RECOVERY_LEDGER_VERSION,
                sequence=ledger_sequence,
                phase="RESTORE_PREPARED",
                restore_generation=generation,
                recovery_epoch=recovery_epoch,
                fencing_token_floor=fencing_token_floor,
                backup_digest=backup_digest,
                actor=actor,
                audit_ref=audit_ref,
            )
            tombstone_record = RecoveryTombstoneRecord(
                version=TOMBSTONE_LOG_VERSION,
                sequence=tombstone_sequence,
                phase="PREPARED",
                restore_generation=generation,
                backup_digest=backup_digest,
                previous_primary_digest=previous_primary_digest,
                candidate_digest=candidate_digest,
                previous_recovery_epoch=previous_recovery_epoch,
                previous_fencing_token_hwm=previous_fencing_token_hwm,
                previous_last_clock_ns=previous_last_clock_ns,
                identities=canonical_identities,
                actor=actor,
                audit_ref=audit_ref,
            )
            authority = _issue_recovery_ledger_initialization(
                operator_id=actor,
                audit_ref=audit_ref,
                request_digest=backup_digest,
            )
            _require_persisted_floor_above_previous_hwm(
                ledger_record,
                tombstone_record,
            )
            if ledger is not None and tombstone is None:
                _require_persisted_floor_above_previous_hwm(
                    ledger,
                    tombstone_record,
                )
            if ledger is not None and ledger_append:
                _require_floor_at_least(
                    recovery_epoch,
                    fencing_token_floor,
                    ledger,
                )
            if tombstone_create:
                self.tombstones._append_owned_at_root(
                    root_fd,
                    tombstone_record,
                    allow_create=True,
                    retain_fd=retain_fd,
                )
            elif tombstone_append:
                self.tombstones._append_owned_at_root(
                    root_fd,
                    tombstone_record,
                    allow_create=False,
                    retain_fd=retain_fd,
                )
            if ledger_create:
                _validate_initialization(authority, ledger_record)
                self.ledger._append_owned_at_root(
                    root_fd,
                    ledger_record,
                    allow_create=True,
                    retain_fd=retain_fd,
                )
            elif ledger_append:
                self.ledger._append_owned_at_root(
                    root_fd,
                    ledger_record,
                    allow_create=False,
                    retain_fd=retain_fd,
                )
            (
                final_ledger_records,
                final_ledger,
                final_tombstone,
                final_tombstone_records,
            ) = _read_restore_pair(
                root_fd,
                retain_fd=retain_fd,
            )
            _validate_restore_pair(
                final_ledger_records,
                final_ledger,
                final_tombstone,
                final_tombstone_records,
            )
            return _pair_handle(final_ledger, final_tombstone)

    def mark_replaced(
        self,
        handle: RestoreHandle,
        floor: RecoveryFloor,
        owner: object,
    ) -> RestoreHandle:
        self._assert_layout()
        handle = _validate_restore_handle(handle)
        recovery_epoch, fencing_token_floor = _validate_floor(floor)
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            ledger_records, ledger, tombstone, tombstone_records = _read_restore_pair(
                root_fd,
                retain_fd=retain_fd,
            )
            _validate_restore_pair(
                ledger_records,
                ledger,
                tombstone,
                tombstone_records,
            )
            current = _pair_handle(ledger, tombstone)
            self._require_same_restore_identity(handle, current)
            if current.phase in {
                "RESTORE_REPLACED",
                "RESTORE_COMMITTED",
                "RESTORE_ABORTED",
            }:
                return current
            if (
                current.phase != "RESTORE_PREPARED"
                or current.tombstone_phase != "PREPARED"
            ):
                raise RecoveryLedgerError("restore is not prepared")
            assert ledger is not None
            _require_floor_at_least(
                recovery_epoch,
                fencing_token_floor,
                ledger,
            )
            record = RecoveryLedgerRecord(
                version=RECOVERY_LEDGER_VERSION,
                sequence=ledger.sequence + 1,
                phase="RESTORE_REPLACED",
                restore_generation=current.restore_generation,
                recovery_epoch=recovery_epoch,
                fencing_token_floor=fencing_token_floor,
                backup_digest=current.backup_digest,
                actor=current.actor,
                audit_ref=current.audit_ref,
            )
            self.ledger._append_owned_at_root(
                root_fd,
                record,
                allow_create=False,
                retain_fd=retain_fd,
            )
            (
                final_ledger_records,
                final_ledger,
                final_tombstone,
                final_tombstone_records,
            ) = _read_restore_pair(
                root_fd,
                retain_fd=retain_fd,
            )
            _validate_restore_pair(
                final_ledger_records,
                final_ledger,
                final_tombstone,
                final_tombstone_records,
            )
            return _pair_handle(final_ledger, final_tombstone)

    def mark_committed(
        self,
        handle: RestoreHandle,
        floor: RecoveryFloor,
        owner: object,
    ) -> RestoreHandle:
        self._assert_layout()
        handle = _validate_restore_handle(handle)
        recovery_epoch, fencing_token_floor = _validate_floor(floor)
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            ledger_records, ledger, tombstone, tombstone_records = _read_restore_pair(
                root_fd,
                retain_fd=retain_fd,
            )
            _validate_restore_pair(
                ledger_records,
                ledger,
                tombstone,
                tombstone_records,
            )
            current = _pair_handle(ledger, tombstone)
            self._require_same_restore_identity(handle, current)
            if (
                current.phase == "RESTORE_COMMITTED"
                and current.tombstone_phase == "COMMITTED"
            ):
                return current
            if current.phase not in {"RESTORE_REPLACED", "RESTORE_COMMITTED"}:
                raise RecoveryLedgerError("restore is not replaced")
            if ledger is None or tombstone is None:
                raise RecoveryLedgerError("restore pair is incomplete")
            _require_floor_at_least(
                recovery_epoch,
                fencing_token_floor,
                ledger,
            )
            if tombstone.phase == "PREPARED":
                committed_tombstone = RecoveryTombstoneRecord(
                    version=TOMBSTONE_LOG_VERSION,
                    sequence=tombstone.sequence + 1,
                    phase="COMMITTED",
                    restore_generation=current.restore_generation,
                    backup_digest=current.backup_digest,
                    previous_primary_digest=current.previous_primary_digest,
                    candidate_digest=current.candidate_digest,
                    previous_recovery_epoch=current.previous_recovery_epoch,
                    previous_fencing_token_hwm=current.previous_fencing_token_hwm,
                    previous_last_clock_ns=current.previous_last_clock_ns,
                    identities=current.identities,
                    actor=current.actor,
                    audit_ref=current.audit_ref,
                )
                self.tombstones._append_owned_at_root(
                    root_fd,
                    committed_tombstone,
                    allow_create=False,
                    retain_fd=retain_fd,
                )
            ledger_records, ledger, tombstone, tombstone_records = _read_restore_pair(
                root_fd,
                retain_fd=retain_fd,
            )
            _validate_restore_pair(
                ledger_records,
                ledger,
                tombstone,
                tombstone_records,
            )
            if (
                ledger is None
                or tombstone is None
                or ledger.phase == "RESTORE_COMMITTED"
            ):
                if ledger is not None and tombstone is not None:
                    return _pair_handle(ledger, tombstone)
                raise RecoveryLedgerError("restore pair is incomplete")
            committed_ledger = RecoveryLedgerRecord(
                version=RECOVERY_LEDGER_VERSION,
                sequence=ledger.sequence + 1,
                phase="RESTORE_COMMITTED",
                restore_generation=current.restore_generation,
                recovery_epoch=recovery_epoch,
                fencing_token_floor=fencing_token_floor,
                backup_digest=current.backup_digest,
                actor=current.actor,
                audit_ref=current.audit_ref,
            )
            self.ledger._append_owned_at_root(
                root_fd,
                committed_ledger,
                allow_create=False,
                retain_fd=retain_fd,
            )
            (
                final_ledger_records,
                final_ledger,
                final_tombstone,
                final_tombstone_records,
            ) = _read_restore_pair(
                root_fd,
                retain_fd=retain_fd,
            )
            _validate_restore_pair(
                final_ledger_records,
                final_ledger,
                final_tombstone,
                final_tombstone_records,
            )
            return _pair_handle(final_ledger, final_tombstone)

    def mark_aborted(
        self,
        handle: RestoreHandle,
        floor: RecoveryFloor,
        owner: object,
    ) -> RestoreHandle:
        self._assert_layout()
        handle = _validate_restore_handle(handle)
        recovery_epoch, fencing_token_floor = _validate_floor(floor)
        retain_fd = _owner_retain_callback(owner)
        with _borrowed_root(owner, self.state_root) as root_fd:
            ledger_records, ledger, tombstone, tombstone_records = _read_restore_pair(
                root_fd,
                retain_fd=retain_fd,
            )
            _validate_restore_pair(
                ledger_records,
                ledger,
                tombstone,
                tombstone_records,
            )
            current = _pair_handle(ledger, tombstone)
            self._require_same_restore_identity(handle, current)
            if (
                current.phase == "RESTORE_ABORTED"
                and current.tombstone_phase == "ABORTED"
            ):
                return current
            if current.phase not in {"RESTORE_PREPARED", "RESTORE_ABORTED"}:
                raise RecoveryLedgerError("restore cannot be aborted")
            if ledger is None or tombstone is None:
                raise RecoveryLedgerError("restore pair is incomplete")
            _require_floor_at_least(
                recovery_epoch,
                fencing_token_floor,
                ledger,
            )
            if tombstone.phase == "PREPARED":
                aborted_tombstone = RecoveryTombstoneRecord(
                    version=TOMBSTONE_LOG_VERSION,
                    sequence=tombstone.sequence + 1,
                    phase="ABORTED",
                    restore_generation=current.restore_generation,
                    backup_digest=current.backup_digest,
                    previous_primary_digest=current.previous_primary_digest,
                    candidate_digest=current.candidate_digest,
                    previous_recovery_epoch=current.previous_recovery_epoch,
                    previous_fencing_token_hwm=current.previous_fencing_token_hwm,
                    previous_last_clock_ns=current.previous_last_clock_ns,
                    identities=current.identities,
                    actor=current.actor,
                    audit_ref=current.audit_ref,
                )
                self.tombstones._append_owned_at_root(
                    root_fd,
                    aborted_tombstone,
                    allow_create=False,
                    retain_fd=retain_fd,
                )
            ledger_records, ledger, tombstone, tombstone_records = _read_restore_pair(
                root_fd,
                retain_fd=retain_fd,
            )
            _validate_restore_pair(
                ledger_records,
                ledger,
                tombstone,
                tombstone_records,
            )
            if ledger is None or tombstone is None or ledger.phase == "RESTORE_ABORTED":
                if ledger is not None and tombstone is not None:
                    return _pair_handle(ledger, tombstone)
                raise RecoveryLedgerError("restore pair is incomplete")
            aborted_ledger = RecoveryLedgerRecord(
                version=RECOVERY_LEDGER_VERSION,
                sequence=ledger.sequence + 1,
                phase="RESTORE_ABORTED",
                restore_generation=current.restore_generation,
                recovery_epoch=recovery_epoch,
                fencing_token_floor=fencing_token_floor,
                backup_digest=current.backup_digest,
                actor=current.actor,
                audit_ref=current.audit_ref,
            )
            self.ledger._append_owned_at_root(
                root_fd,
                aborted_ledger,
                allow_create=False,
                retain_fd=retain_fd,
            )
            (
                final_ledger_records,
                final_ledger,
                final_tombstone,
                final_tombstone_records,
            ) = _read_restore_pair(
                root_fd,
                retain_fd=retain_fd,
            )
            _validate_restore_pair(
                final_ledger_records,
                final_ledger,
                final_tombstone,
                final_tombstone_records,
            )
            return _pair_handle(final_ledger, final_tombstone)

    def verify_generation(
        self,
        handle: RestoreHandle,
        owner: object,
    ) -> RestoreHandle:
        self._assert_layout()
        handle = _validate_restore_handle(handle)
        current = self.read(owner=owner)
        if current is None:
            raise RecoveryLedgerError("restore ledger is missing")
        self._require_same_handle(handle, current)
        return current

    def active_committed_identities(
        self,
        owner: object,
    ) -> frozenset[tuple[str, str]]:
        state = self.normal_open_state(owner)
        return state.active_committed_identities()

    @staticmethod
    def _require_same_handle(expected: RestoreHandle, actual: RestoreHandle) -> None:
        fields = (
            "restore_generation",
            "sequence",
            "tombstone_sequence",
            "recovery_epoch",
            "fencing_token_floor",
            "phase",
            "tombstone_phase",
            "backup_digest",
            "previous_primary_digest",
            "candidate_digest",
            "previous_recovery_epoch",
            "previous_fencing_token_hwm",
            "previous_last_clock_ns",
            "identities",
            "actor",
            "audit_ref",
        )
        expected_values = tuple(
            object.__getattribute__(expected, name) for name in fields
        )
        actual_values = tuple(object.__getattribute__(actual, name) for name in fields)
        if expected_values != actual_values:
            raise RecoveryLedgerError("restore handle is stale or mismatched")

    @staticmethod
    def _require_same_restore_identity(
        expected: RestoreHandle,
        actual: RestoreHandle,
    ) -> None:
        fields = (
            "restore_generation",
            "recovery_epoch",
            "fencing_token_floor",
            "backup_digest",
            "previous_primary_digest",
            "candidate_digest",
            "previous_recovery_epoch",
            "previous_fencing_token_hwm",
            "previous_last_clock_ns",
            "identities",
            "actor",
            "audit_ref",
        )
        expected_values = tuple(
            object.__getattribute__(expected, name) for name in fields
        )
        actual_values = tuple(object.__getattribute__(actual, name) for name in fields)
        if expected_values != actual_values:
            raise RecoveryLedgerError("restore handle is stale or mismatched")


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
        if marker_name != WRITER_MARKER_BASENAME:
            raise ValueError("marker_name is not canonical")
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
        with self.store._marker_exclusive_probe():
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
            "MIGRATION_REQUIRED",
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
    "RECOVERY_TOMBSTONES_BASENAME",
    "RECOVERY_TOMBSTONES_VERSION",
    "TOMBSTONE_LOG_VERSION",
    "WRITER_MARKER_BASENAME",
    "ForceReasonCode",
    "NormalOpenRecoveryState",
    "RecoveryAction",
    "RecoveryAuthorization",
    "RecoveryAuthorizationError",
    "RecoveryAuthorizer",
    "RecoveryCommitUnknownError",
    "RecoveryConflictError",
    "RecoveryCoordinator",
    "RecoveryDurabilityError",
    "RecoveryError",
    "RecoveryLayout",
    "RecoveryLedger",
    "RecoveryLedgerError",
    "RecoveryLedgerInitialization",
    "RecoveryLedgerRecord",
    "RecoveryLedgerWriter",
    "RecoveryRequiredError",
    "RecoveryResult",
    "RecoveryTombstoneLog",
    "RecoveryTombstoneRecord",
    "RestoreHandle",
    "RestoreIdentity",
    "RestoreIdentityTombstoneLog",
    "RestoreLedger",
    "RestoreOrphanKind",
    "RestoreTombstoneOrphan",
    "TombstonePhase",
]
