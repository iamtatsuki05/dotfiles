"""Private SQLite coordination state for the agent-team runtime.

The public seam exposes immutable observations only.  SQLite connections,
queries, rows, and mutation authority stay inside this module so later
coordination phases can extend the implementation without widening callers.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, Self, cast
from urllib.parse import quote

from .lease import (
    Claim,
    ClockRollbackError,
    LeaseConflictError,
    LeaseError,
    ProviderEffect,
    ProviderFenceProof,
    ProviderPort,
    ProviderProofError,
    ProviderReceiptError,
    ProviderStatus,
    RecoveryFloor,
    RecoveryFloorReservation,
    RecoveryRebaseMode,
    RecoverySnapshot,
    RestoreApplyResult,
    RestoreCandidateEvidence,
    RestoreIdentity,
    RestoreReplacedEvidence,
    StoreImageObservation,
    VerifiedProviderReceipt,
    _issue_floor_reservation,
    _issue_provider_effect,
    _issue_restore_apply_result,
    _restore_binding_digest,
    _restore_expected_fencing_tokens,
    _verified_receipt_from_status,
    require_provider_capabilities,
)

STORE_SCHEMA: Final[int] = 2
EVENT_SCHEMA_VERSION: Final[int] = 2
DATABASE_FILENAME: Final[str] = "coordination.sqlite3"
WRITER_MARKER_FILENAME: Final[str] = "writer.marker"
WRITER_MARKER_VERSION: Final[int] = 1
WRITER_MARKER_CLEAN_STATE: Final[str] = "CLEAN"
WRITER_MARKER_PREPARED_STATE: Final[str] = "CLEANUP_PREPARED"
WRITER_MARKER_CLEAN_CONTENT: Final[bytes] = b'{"version":1,"state":"CLEAN"}\n'
WRITER_MARKER_PREPARED_CONTENT: Final[bytes] = (
    b'{"version":1,"state":"CLEANUP_PREPARED"}\n'
)
LIFETIME_GATE_FILENAME: Final[str] = ".coordination-lifetime.lock"
DEFAULT_BUSY_TIMEOUT_MS: Final[int] = 5_000
MAX_BUSY_TIMEOUT_MS: Final[int] = 30_000
SQLITE_INTEGER_MAX: Final[int] = 2**63 - 1
MAX_IDENTIFIER_LENGTH: Final[int] = 128
MAX_IMAGE_BYTES: Final[int] = 256 * 1024 * 1024
_CLEANUP_EXCEPTION: Final[type[BaseException]] = BaseException
_OrphanFD = tuple[int, tuple[int, int] | None, str]
_MAX_ORPHAN_FDS: Final[int] = 8
_WRITER_MARKER_CONTENTS: Final[dict[str, bytes]] = {
    WRITER_MARKER_CLEAN_STATE: WRITER_MARKER_CLEAN_CONTENT,
    WRITER_MARKER_PREPARED_STATE: WRITER_MARKER_PREPARED_CONTENT,
}

_VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "INTENT",
        "FENCE_PENDING",
        "FENCE_RESERVATION_STARTED",
        "CLAIMED",
        "EFFECT_PREPARED",
        "UNKNOWN_EFFECT",
        "UNKNOWN",
        "RECEIPTED",
        "COMPLETED",
        "CLEANED",
        "RESTORE_INCOMPLETE",
    }
)
_VALID_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "intent_created",
        "lease_claimed",
        "lease_heartbeat",
        "lease_reclaimed",
        "fence_activated",
        "fence_reservation_started",
        "effect_prepared",
        "effect_unknown",
        "receipt_recorded",
        "operation_completed",
        "recover",
        "force_recover",
        "resolve_unknown",
        "rebind_receipt",
        "cleaned",
        "checkpoint",
        "cleanup",
        "restore",
    }
)
_VALID_EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "intent",
        "claim",
        "heartbeat",
        "reclaim",
        "fence",
        "effect",
        "unknown_effect",
        "receipt",
        "complete",
        "recover",
        "force_recover",
        "resolve_unknown",
        "rebind_receipt",
        "cleaned",
        "checkpoint",
        "cleanup",
        "restore",
    }
)
_VALID_REBASE_MODES: Final[frozenset[str]] = frozenset(
    {"INTENT", "RECEIPTED", "COMPLETED"}
)
_EVENT_REASON_PAIRS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("intent", "intent_created"),
        ("claim", "lease_claimed"),
        ("heartbeat", "lease_heartbeat"),
        ("reclaim", "lease_reclaimed"),
        ("fence", "fence_activated"),
        ("fence", "fence_reservation_started"),
        ("effect", "effect_prepared"),
        ("unknown_effect", "effect_unknown"),
        ("receipt", "receipt_recorded"),
        ("complete", "operation_completed"),
        ("recover", "recover"),
        ("force_recover", "force_recover"),
        ("resolve_unknown", "resolve_unknown"),
        ("rebind_receipt", "rebind_receipt"),
        ("cleaned", "cleaned"),
        ("checkpoint", "checkpoint"),
        ("cleanup", "cleanup"),
        ("restore", "restore"),
    }
)
_OPAQUE_IDENTIFIER_RE = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._:/-]{{0,{MAX_IDENTIFIER_LENGTH - 1}}}\Z"
)
_EVIDENCE_REF_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SECRET_LIKE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(?:api[_-]?key|secret|token|password|passwd|authorization|bearer|"
        r"cookie|credential|private[_-]?key)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:^|[-_])(sk|pk|ghp|gho|github_pat|xox[baprs])[-_]?", re.IGNORECASE),
)

_STORE_META_SQL = """
CREATE TABLE store_meta (
    key TEXT NOT NULL PRIMARY KEY,
    value INTEGER NOT NULL CHECK(
        typeof(value) = 'integer' AND value BETWEEN 0 AND 9223372036854775807
    )
)
"""
_OPERATIONS_SQL = """
CREATE TABLE operations (
    operation_id TEXT NOT NULL PRIMARY KEY CHECK(
        typeof(operation_id) = 'text' AND length(operation_id) BETWEEN 1 AND 128
    ),
    effect_key TEXT NOT NULL UNIQUE CHECK(
        typeof(effect_key) = 'text' AND length(effect_key) BETWEEN 1 AND 128
    ),
    status TEXT NOT NULL CHECK(
        typeof(status) = 'text' AND status IN (
            'INTENT', 'FENCE_PENDING', 'FENCE_RESERVATION_STARTED', 'CLAIMED',
            'EFFECT_PREPARED',
            'UNKNOWN_EFFECT', 'UNKNOWN', 'RECEIPTED', 'COMPLETED', 'CLEANED',
            'RESTORE_INCOMPLETE'
        )
    ),
    provider_id TEXT CHECK(
        provider_id IS NULL OR (
            typeof(provider_id) = 'text' AND length(provider_id) BETWEEN 1 AND 128
        )
    ),
    current_attempt INTEGER NOT NULL CHECK(
        typeof(current_attempt) = 'integer'
        AND current_attempt BETWEEN 0 AND 9223372036854775807
    ),
    recovery_epoch INTEGER NOT NULL CHECK(
        typeof(recovery_epoch) = 'integer'
        AND recovery_epoch BETWEEN 0 AND 9223372036854775807
    ),
    created_ns INTEGER NOT NULL CHECK(
        typeof(created_ns) = 'integer'
        AND created_ns BETWEEN 0 AND 9223372036854775807
    ),
    updated_ns INTEGER NOT NULL CHECK(
        typeof(updated_ns) = 'integer'
        AND updated_ns BETWEEN 0 AND 9223372036854775807
    ),
    CHECK(status = 'INTENT' OR (provider_id IS NOT NULL AND current_attempt >= 1))
)
"""
_OPERATION_ATTEMPTS_SQL = """
CREATE TABLE operation_attempts (
    operation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK(
        typeof(attempt) = 'integer'
        AND attempt BETWEEN 0 AND 9223372036854775807
    ),
    owner TEXT CHECK(
        owner IS NULL OR (
            typeof(owner) = 'text' AND length(owner) BETWEEN 1 AND 128
        )
    ),
    provider_id TEXT CHECK(
        provider_id IS NULL OR (
            typeof(provider_id) = 'text' AND length(provider_id) BETWEEN 1 AND 128
        )
    ),
    lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK(
        typeof(lease_epoch) = 'integer'
        AND lease_epoch BETWEEN 0 AND 9223372036854775807
    ),
    fencing_token INTEGER NOT NULL DEFAULT 0 CHECK(
        typeof(fencing_token) = 'integer'
        AND fencing_token BETWEEN 0 AND 9223372036854775807
    ),
    lease_heartbeat_ns INTEGER CHECK(
        lease_heartbeat_ns IS NULL OR (
            typeof(lease_heartbeat_ns) = 'integer'
            AND lease_heartbeat_ns BETWEEN 0 AND 9223372036854775807
        )
    ),
    lease_expires_ns INTEGER CHECK(
        lease_expires_ns IS NULL OR (
            typeof(lease_expires_ns) = 'integer'
            AND lease_expires_ns BETWEEN 0 AND 9223372036854775807
        )
    ),
    fence_proof_version INTEGER CHECK(
        fence_proof_version IS NULL OR (
            typeof(fence_proof_version) = 'integer'
            AND fence_proof_version BETWEEN 1 AND 9223372036854775807
        )
    ),
    fence_proof_ref TEXT CHECK(
        fence_proof_ref IS NULL OR (
            typeof(fence_proof_ref) = 'text'
            AND length(fence_proof_ref) BETWEEN 1 AND 128
        )
    ),
    effect_started_ns INTEGER CHECK(
        effect_started_ns IS NULL OR (
            typeof(effect_started_ns) = 'integer'
            AND effect_started_ns BETWEEN 0 AND 9223372036854775807
        )
    ),
    fence_started_ns INTEGER CHECK(
        fence_started_ns IS NULL OR (
            typeof(fence_started_ns) = 'integer'
            AND fence_started_ns BETWEEN 0 AND 9223372036854775807
        )
    ),
    CHECK(
        (attempt = 0 AND owner IS NULL AND provider_id IS NULL
         AND lease_epoch = 0 AND fencing_token = 0 AND lease_heartbeat_ns IS NULL
         AND lease_expires_ns IS NULL AND fence_proof_version IS NULL
         AND fence_proof_ref IS NULL AND effect_started_ns IS NULL
         AND fence_started_ns IS NULL)
        OR (attempt >= 1 AND owner IS NOT NULL AND provider_id IS NOT NULL
            AND fencing_token >= 1 AND lease_heartbeat_ns IS NOT NULL
            AND lease_expires_ns IS NOT NULL
            AND lease_expires_ns > lease_heartbeat_ns)
    ),
    CHECK((fence_proof_version IS NULL) = (fence_proof_ref IS NULL)),
    PRIMARY KEY(operation_id, attempt),
    FOREIGN KEY(operation_id) REFERENCES operations(operation_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""
_EFFECT_RECEIPTS_SQL = """
CREATE TABLE effect_receipts (
    operation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK(
        typeof(attempt) = 'integer'
        AND attempt BETWEEN 1 AND 9223372036854775807
    ),
    effect_key TEXT NOT NULL CHECK(
        typeof(effect_key) = 'text' AND length(effect_key) BETWEEN 1 AND 128
    ),
    provider_effect_id TEXT NOT NULL CHECK(
        typeof(provider_effect_id) = 'text'
        AND length(provider_effect_id) BETWEEN 1 AND 128
    ),
    provider_status TEXT NOT NULL CHECK(
        typeof(provider_status) = 'text'
        AND provider_status = 'COMPLETED'
    ),
    provider_id TEXT NOT NULL CHECK(
        typeof(provider_id) = 'text' AND length(provider_id) BETWEEN 1 AND 128
    ),
    owner TEXT NOT NULL CHECK(
        typeof(owner) = 'text' AND length(owner) BETWEEN 1 AND 128
    ),
    fencing_token INTEGER NOT NULL CHECK(
        typeof(fencing_token) = 'integer'
        AND fencing_token BETWEEN 1 AND 9223372036854775807
    ),
    lease_epoch INTEGER NOT NULL CHECK(
        typeof(lease_epoch) = 'integer'
        AND lease_epoch BETWEEN 0 AND 9223372036854775807
    ),
    received_ns INTEGER NOT NULL CHECK(
        typeof(received_ns) = 'integer'
        AND received_ns BETWEEN 0 AND 9223372036854775807
    ),
    proof_version INTEGER NOT NULL CHECK(
        typeof(proof_version) = 'integer'
        AND proof_version BETWEEN 1 AND 9223372036854775807
    ),
    proof_ref TEXT NOT NULL CHECK(
        typeof(proof_ref) = 'text' AND length(proof_ref) BETWEEN 1 AND 128
    ),
    PRIMARY KEY(operation_id, attempt),
    UNIQUE(effect_key, provider_effect_id),
    FOREIGN KEY(operation_id, attempt)
        REFERENCES operation_attempts(operation_id, attempt)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""
_TRANSITION_EVENTS_SQL = """
CREATE TABLE transition_events (
    event_id INTEGER PRIMARY KEY CHECK(
        typeof(event_id) = 'integer'
        AND event_id BETWEEN 1 AND 9223372036854775807
    ),
    event_schema_version INTEGER NOT NULL CHECK(
        typeof(event_schema_version) = 'integer' AND event_schema_version = 2
    ),
    operation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK(
        typeof(attempt) = 'integer'
        AND attempt BETWEEN 0 AND 9223372036854775807
    ),
    from_status TEXT CHECK(
        from_status IS NULL OR (
            typeof(from_status) = 'text' AND from_status IN (
                'INTENT', 'FENCE_PENDING', 'FENCE_RESERVATION_STARTED', 'CLAIMED',
                'EFFECT_PREPARED',
                'UNKNOWN_EFFECT', 'UNKNOWN', 'RECEIPTED', 'COMPLETED', 'CLEANED',
                'RESTORE_INCOMPLETE'
            )
        )
    ),
    to_status TEXT NOT NULL CHECK(
        typeof(to_status) = 'text' AND to_status IN (
            'INTENT', 'FENCE_PENDING', 'FENCE_RESERVATION_STARTED', 'CLAIMED',
            'EFFECT_PREPARED',
            'UNKNOWN_EFFECT', 'UNKNOWN', 'RECEIPTED', 'COMPLETED', 'CLEANED',
            'RESTORE_INCOMPLETE'
        )
    ),
    kind TEXT NOT NULL CHECK(
        typeof(kind) = 'text' AND kind IN (
            'intent', 'claim', 'heartbeat', 'reclaim', 'fence', 'effect',
            'unknown_effect', 'receipt', 'complete', 'recover', 'force_recover',
            'resolve_unknown', 'rebind_receipt',
            'cleaned', 'checkpoint', 'cleanup', 'restore'
        )
    ),
    actor TEXT NOT NULL CHECK(
        typeof(actor) = 'text' AND length(actor) BETWEEN 1 AND 128
    ),
    clock_ns INTEGER NOT NULL CHECK(
        typeof(clock_ns) = 'integer'
        AND clock_ns BETWEEN 0 AND 9223372036854775807
    ),
    reason_code TEXT NOT NULL CHECK(
        typeof(reason_code) = 'text' AND reason_code IN (
            'intent_created', 'lease_claimed', 'lease_heartbeat',
            'lease_reclaimed', 'fence_activated', 'fence_reservation_started',
            'effect_prepared',
            'effect_unknown', 'receipt_recorded', 'operation_completed',
            'recover', 'force_recover', 'resolve_unknown', 'rebind_receipt',
            'cleaned', 'checkpoint', 'cleanup', 'restore'
        )
    ),
    evidence_ref TEXT CHECK(
        evidence_ref IS NULL OR (
            typeof(evidence_ref) = 'text'
            AND length(evidence_ref) = 71
            AND substr(evidence_ref, 1, 7) = 'sha256:'
            AND substr(evidence_ref, 8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    CHECK(
        (kind = 'intent' AND reason_code = 'intent_created')
        OR (kind = 'claim' AND reason_code = 'lease_claimed')
        OR (kind = 'heartbeat' AND reason_code = 'lease_heartbeat')
        OR (kind = 'reclaim' AND reason_code = 'lease_reclaimed')
        OR (kind = 'fence' AND reason_code = 'fence_activated')
        OR (kind = 'fence' AND reason_code = 'fence_reservation_started')
        OR (kind = 'effect' AND reason_code = 'effect_prepared')
        OR (kind = 'unknown_effect' AND reason_code = 'effect_unknown')
        OR (kind = 'receipt' AND reason_code = 'receipt_recorded')
        OR (kind = 'complete' AND reason_code = 'operation_completed')
        OR (kind = 'recover' AND reason_code = 'recover')
        OR (kind = 'force_recover' AND reason_code = 'force_recover')
        OR (kind = 'resolve_unknown' AND reason_code = 'resolve_unknown')
        OR (kind = 'rebind_receipt' AND reason_code = 'rebind_receipt')
        OR (kind = 'cleaned' AND reason_code = 'cleaned')
        OR (kind = 'checkpoint' AND reason_code = 'checkpoint')
        OR (kind = 'cleanup' AND reason_code = 'cleanup')
        OR (kind = 'restore' AND reason_code = 'restore')
    ),
    FOREIGN KEY(operation_id, attempt)
        REFERENCES operation_attempts(operation_id, attempt)
        ON UPDATE RESTRICT ON DELETE RESTRICT
)
"""
_OPERATIONS_STATUS_INDEX_SQL = (
    "CREATE INDEX operations_status_idx ON operations(status)"
)
_EVENTS_OPERATION_INDEX_SQL = (
    "CREATE INDEX transition_events_operation_idx "
    "ON transition_events(operation_id, event_id)"
)
_EVENTS_ATTEMPT_INDEX_SQL = (
    "CREATE INDEX transition_events_attempt_idx "
    "ON transition_events(operation_id, attempt, event_id)"
)
_EVENTS_NO_UPDATE_TRIGGER_SQL = """
CREATE TRIGGER transition_events_no_update
BEFORE UPDATE ON transition_events
BEGIN
    SELECT RAISE(ABORT, 'transition_events is append-only');
END
"""
_EVENTS_NO_DELETE_TRIGGER_SQL = """
CREATE TRIGGER transition_events_no_delete
BEFORE DELETE ON transition_events
BEGIN
    SELECT RAISE(ABORT, 'transition_events is append-only');
END
"""
_EVENTS_NO_REPLACE_TRIGGER_SQL = """
CREATE TRIGGER transition_events_no_replace
BEFORE INSERT ON transition_events
WHEN EXISTS(
    SELECT 1 FROM transition_events WHERE event_id = NEW.event_id
)
BEGIN
    SELECT RAISE(ABORT, 'transition_events is append-only');
END
"""

_TABLE_DEFINITIONS: Final[dict[str, str]] = {
    "store_meta": _STORE_META_SQL,
    "operations": _OPERATIONS_SQL,
    "operation_attempts": _OPERATION_ATTEMPTS_SQL,
    "effect_receipts": _EFFECT_RECEIPTS_SQL,
    "transition_events": _TRANSITION_EVENTS_SQL,
}
_INDEX_DEFINITIONS: Final[dict[str, str]] = {
    "operations_status_idx": _OPERATIONS_STATUS_INDEX_SQL,
    "transition_events_operation_idx": _EVENTS_OPERATION_INDEX_SQL,
    "transition_events_attempt_idx": _EVENTS_ATTEMPT_INDEX_SQL,
}
_TRIGGER_DEFINITIONS: Final[dict[str, str]] = {
    "transition_events_no_update": _EVENTS_NO_UPDATE_TRIGGER_SQL,
    "transition_events_no_delete": _EVENTS_NO_DELETE_TRIGGER_SQL,
    "transition_events_no_replace": _EVENTS_NO_REPLACE_TRIGGER_SQL,
}
_EXPECTED_META_KEYS: Final[frozenset[str]] = frozenset(
    {"store_schema", "recovery_epoch", "fencing_token_floor", "last_clock_ns"}
)
_EXPECTED_OBJECT_SQL: Final[dict[tuple[str, str], str]] = {
    **{("table", name): sql for name, sql in _TABLE_DEFINITIONS.items()},
    **{("index", name): sql for name, sql in _INDEX_DEFINITIONS.items()},
    **{("trigger", name): sql for name, sql in _TRIGGER_DEFINITIONS.items()},
}
_EXPECTED_COLUMNS: Final[dict[str, tuple[tuple[str, str, int, int], ...]]] = {
    "store_meta": (
        ("key", "TEXT", 1, 1),
        ("value", "INTEGER", 1, 0),
    ),
    "operations": (
        ("operation_id", "TEXT", 1, 1),
        ("effect_key", "TEXT", 1, 0),
        ("status", "TEXT", 1, 0),
        ("provider_id", "TEXT", 0, 0),
        ("current_attempt", "INTEGER", 1, 0),
        ("recovery_epoch", "INTEGER", 1, 0),
        ("created_ns", "INTEGER", 1, 0),
        ("updated_ns", "INTEGER", 1, 0),
    ),
    "operation_attempts": (
        ("operation_id", "TEXT", 1, 1),
        ("attempt", "INTEGER", 1, 2),
        ("owner", "TEXT", 0, 0),
        ("provider_id", "TEXT", 0, 0),
        ("lease_epoch", "INTEGER", 1, 0),
        ("fencing_token", "INTEGER", 1, 0),
        ("lease_heartbeat_ns", "INTEGER", 0, 0),
        ("lease_expires_ns", "INTEGER", 0, 0),
        ("fence_proof_version", "INTEGER", 0, 0),
        ("fence_proof_ref", "TEXT", 0, 0),
        ("effect_started_ns", "INTEGER", 0, 0),
        ("fence_started_ns", "INTEGER", 0, 0),
    ),
    "effect_receipts": (
        ("operation_id", "TEXT", 1, 1),
        ("attempt", "INTEGER", 1, 2),
        ("effect_key", "TEXT", 1, 0),
        ("provider_effect_id", "TEXT", 1, 0),
        ("provider_status", "TEXT", 1, 0),
        ("provider_id", "TEXT", 1, 0),
        ("owner", "TEXT", 1, 0),
        ("fencing_token", "INTEGER", 1, 0),
        ("lease_epoch", "INTEGER", 1, 0),
        ("received_ns", "INTEGER", 1, 0),
        ("proof_version", "INTEGER", 1, 0),
        ("proof_ref", "TEXT", 1, 0),
    ),
    "transition_events": (
        ("event_id", "INTEGER", 0, 1),
        ("event_schema_version", "INTEGER", 1, 0),
        ("operation_id", "TEXT", 1, 0),
        ("attempt", "INTEGER", 1, 0),
        ("from_status", "TEXT", 0, 0),
        ("to_status", "TEXT", 1, 0),
        ("kind", "TEXT", 1, 0),
        ("actor", "TEXT", 1, 0),
        ("clock_ns", "INTEGER", 1, 0),
        ("reason_code", "TEXT", 1, 0),
        ("evidence_ref", "TEXT", 0, 0),
    ),
}
_EXPECTED_INDEX_CONTRACT: Final[
    dict[str, tuple[tuple[int, str, tuple[str, ...]], ...]]
] = {
    "store_meta": ((1, "pk", ("key",)),),
    "operations": (
        (1, "pk", ("operation_id",)),
        (1, "u", ("effect_key",)),
        (0, "c", ("status",)),
    ),
    "operation_attempts": ((1, "pk", ("operation_id", "attempt")),),
    "effect_receipts": (
        (1, "pk", ("operation_id", "attempt")),
        (1, "u", ("effect_key", "provider_effect_id")),
    ),
    "transition_events": (
        (0, "c", ("operation_id", "event_id")),
        (0, "c", ("operation_id", "attempt", "event_id")),
    ),
}
_EXPECTED_FOREIGN_KEYS: Final[dict[str, tuple[tuple[str, str, str, str, str], ...]]] = {
    "store_meta": (),
    "operations": (),
    "operation_attempts": (
        ("operations", "operation_id", "operation_id", "RESTRICT", "RESTRICT"),
    ),
    "effect_receipts": (
        (
            "operation_attempts",
            "operation_id",
            "operation_id",
            "RESTRICT",
            "RESTRICT",
        ),
        ("operation_attempts", "attempt", "attempt", "RESTRICT", "RESTRICT"),
    ),
    "transition_events": (
        (
            "operation_attempts",
            "operation_id",
            "operation_id",
            "RESTRICT",
            "RESTRICT",
        ),
        ("operation_attempts", "attempt", "attempt", "RESTRICT", "RESTRICT"),
    ),
}


class _CleanupCapability:
    """Opaque, bounded owner for deferred cleanup retries."""

    __slots__ = ("_members",)

    def __init__(self, retry: Callable[[], None]) -> None:
        self._members: list[Callable[[], None]] = [retry]

    @classmethod
    def _from_members(cls, members: Iterable[Callable[[], None]]) -> _CleanupCapability:
        capability = cls.__new__(cls)
        capability._members = list(members)
        return capability

    @staticmethod
    def _same_member(
        first: Callable[[], None],
        second: Callable[[], None],
    ) -> bool:
        if first is second:
            return True
        try:
            return bool(first == second)
        except _CLEANUP_EXCEPTION:
            return False

    @classmethod
    def compose(cls, *capabilities: _CleanupCapability) -> _CleanupCapability:
        members: list[Callable[[], None]] = []
        for capability in capabilities:
            for member in capability._members:
                if not any(cls._same_member(member, existing) for existing in members):
                    members.append(member)
        return cls._from_members(members)

    def retry_cleanup(self) -> None:
        remaining: list[Callable[[], None]] = []
        first_error: BaseException | None = None
        for member in self._members:
            try:
                member()
            except _CLEANUP_EXCEPTION as exc:
                remaining.append(member)
                if first_error is None:
                    first_error = exc
        self._members = remaining
        if first_error is not None:
            raise first_error


class _FDRecoveryOwner:
    """Keep one uncertain descriptor for identity-safe retry."""

    __slots__ = ("_expected_identity", "_fd", "_label", "_resolve_identity")

    def __init__(
        self,
        fd: int,
        expected_identity: tuple[int, int] | None,
        label: str,
        resolve_identity: Callable[[int, tuple[int, int]], tuple[int, int] | None]
        | None = None,
    ) -> None:
        self._fd: int | None = fd
        self._expected_identity = expected_identity
        self._label = label
        self._resolve_identity = resolve_identity

    def _drop(self) -> None:
        self._fd = None
        self._expected_identity = None

    def retry_cleanup(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            metadata = os.fstat(fd)
        except _CLEANUP_EXCEPTION as exc:
            if isinstance(exc, OSError) and exc.errno == errno.EBADF:
                self._drop()
                return
            raise StoreUnavailableError(
                f"{self._label} descriptor status is unknown"
            ) from exc
        expected_identity = self._expected_identity
        if expected_identity is None and self._resolve_identity is not None:
            expected_identity = self._resolve_identity(fd, _identity(metadata))
            if expected_identity is not None:
                self._expected_identity = expected_identity
        if expected_identity is None:
            raise StoreUnavailableError(
                f"{self._label} descriptor identity is unavailable"
            )
        if _identity(metadata) != expected_identity:
            self._drop()
            raise StoreUnavailableError(f"{self._label} descriptor was reused")
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
                    self._drop()
                    return
                raise StoreUnavailableError(
                    f"{self._label} descriptor close status is unknown"
                ) from exc
            if _identity(retry_metadata) != expected_identity:
                self._drop()
                raise StoreUnavailableError(
                    f"{self._label} descriptor was reused"
                ) from exc
            raise StoreUnavailableError(
                f"{self._label} descriptor cannot be closed"
            ) from exc
        self._drop()


class _FDRecoveryGroup:
    """Drain a bounded set of descriptor owners while preserving first error."""

    __slots__ = ("_owners",)

    def __init__(self, owners: Iterable[_FDRecoveryOwner]) -> None:
        self._owners = tuple(owners)

    def retry_cleanup(self) -> None:
        first_error: BaseException | None = None
        for owner in self._owners:
            try:
                owner.retry_cleanup()
            except _CLEANUP_EXCEPTION as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


class _ConnectionCleanupOwner:
    """Keep one temporary SQLite connection reachable until close is certain."""

    __slots__ = ("_connection", "_label")

    def __init__(self, connection: sqlite3.Connection, label: str) -> None:
        self._connection: sqlite3.Connection | None = connection
        self._label = label

    def retry_cleanup(self) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            connection.close()
        except sqlite3.ProgrammingError:
            self._connection = None
            return
        except _CLEANUP_EXCEPTION as exc:
            raise StoreUnavailableError(
                f"{self._label} connection cannot be closed"
            ) from exc
        self._connection = None


def _attempt_fd_cleanup(
    fd: int,
    expected_identity: tuple[int, int] | None,
    label: str,
) -> tuple[BaseException | None, _FDRecoveryOwner | None]:
    owner = _FDRecoveryOwner(fd, expected_identity, label)
    try:
        owner.retry_cleanup()
    except _CLEANUP_EXCEPTION as exc:
        if owner._fd is None:
            return exc, None
        return exc, owner
    return None, None


class StoreError(RuntimeError):
    """Base class for explicit coordination-store failures."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self._cleanup_capability: _CleanupCapability | None = None

    def _attach_cleanup_capability(self, capability: _CleanupCapability) -> None:
        existing = self._cleanup_capability
        self._cleanup_capability = (
            capability
            if existing is None
            else _CleanupCapability.compose(capability, existing)
        )

    def retry_cleanup(self) -> None:
        """Retry an opaque, deferred resource cleanup, if one is pending."""

        capability = self._cleanup_capability
        if capability is None:
            return
        try:
            capability.retry_cleanup()
        except BaseException as exc:
            replacement = _attach_cleanup_capability(exc, capability)
            if replacement is not exc:
                raise replacement from exc
            raise
        self._cleanup_capability = None


class StoreClosedError(StoreError):
    """The store was used after its connection was closed."""


class StoreSchemaError(StoreError):
    """The database is not exactly the supported store schema."""


class StoreIntegrityError(StoreError):
    """SQLite reported a corrupted or inconsistent database."""


class StoreUnavailableError(StoreError):
    """The requested private state root or SQLite database is unavailable."""


class StoreBusyError(StoreError):
    """The bounded SQLite busy timeout expired without a write lock."""


class StoreCommitUnknownError(StoreError):
    """The transaction committed but its result lost identity certainty."""


class DuplicateOperationError(StoreError):
    """An operation or effect identity already exists."""


def _attach_cleanup_capability(
    error: BaseException,
    capability: _CleanupCapability,
) -> BaseException:
    if isinstance(error, StoreError):
        error._attach_cleanup_capability(capability)
        return error
    # Most Python exceptions permit private attributes.  Keeping the original
    # body exception primary is preferable to replacing it with cleanup state.
    try:
        cleanup_attribute = "_cleanup_capability"
        retry_attribute = "retry_cleanup"
        attributes = vars(error)
        existing = attributes.get(cleanup_attribute)
        if isinstance(existing, _CleanupCapability):
            capability = _CleanupCapability.compose(capability, existing)
        else:
            retry = attributes.get(retry_attribute)
            if retry is None:
                retry = getattr(error, retry_attribute, None)
            if callable(retry):
                capability = _CleanupCapability.compose(
                    capability,
                    _CleanupCapability(cast(Callable[[], None], retry)),
                )
        setattr(error, cleanup_attribute, capability)
        setattr(error, retry_attribute, capability.retry_cleanup)
    except _CLEANUP_EXCEPTION:
        wrapped = StoreUnavailableError("coordination store cleanup failed")
        wrapped.__cause__ = error
        wrapped._attach_cleanup_capability(capability)
        return wrapped
    return error


def _extract_cleanup_capability(
    error: BaseException,
) -> _CleanupCapability | None:
    try:
        if isinstance(error, StoreError):
            return error._cleanup_capability
        attributes = vars(error)
        capability = attributes.get("_cleanup_capability")
        if isinstance(capability, _CleanupCapability):
            return capability
        retry = attributes.get("retry_cleanup")
        if retry is None:
            retry = getattr(error, "retry_cleanup", None)
        if callable(retry):
            return _CleanupCapability(cast(Callable[[], None], retry))
    except _CLEANUP_EXCEPTION:
        return None
    return None


def _adopt_cleanup_capability(
    wrapper: BaseException,
    source: BaseException,
) -> BaseException:
    capability = _extract_cleanup_capability(source)
    if capability is None:
        return wrapper
    return _attach_cleanup_capability(wrapper, capability)


def _raise_with_cleanup_capability(
    error: BaseException,
    capability: _CleanupCapability,
) -> NoReturn:
    attached = _attach_cleanup_capability(error, capability)
    if attached is not error:
        raise attached from error
    raise error


def _store_error_from_exception(
    error: BaseException,
    message: str,
) -> StoreError:
    if isinstance(error, StoreError):
        return error
    wrapped = StoreUnavailableError(message)
    wrapped.__cause__ = error
    return wrapped


@contextmanager
def _temporary_sqlite_connection(label: str) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(
        ":memory:",
        uri=False,
        timeout=0,
        isolation_level=None,
    )
    owner = _ConnectionCleanupOwner(connection, label)
    body_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    try:
        yield connection
    except _CLEANUP_EXCEPTION as exc:
        body_error = exc
    finally:
        try:
            owner.retry_cleanup()
        except _CLEANUP_EXCEPTION as exc:
            cleanup_error = exc
    capability = _CleanupCapability(owner.retry_cleanup)
    if body_error is not None:
        if cleanup_error is not None:
            _raise_with_cleanup_capability(body_error, capability)
        attached = _attach_cleanup_capability(body_error, capability)
        if attached is not body_error:
            raise attached from body_error
        raise body_error
    if cleanup_error is not None:
        _raise_with_cleanup_capability(cleanup_error, capability)


def _normalize_sql(sql: str) -> str:
    return sql.strip()


def _noop_restore_fault(point: str) -> None:
    del point


def _require_sqlite_integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > SQLITE_INTEGER_MAX:
        raise ValueError(f"{name} must be a supported integer")
    return value


def _require_opaque_identifier(value: object, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be an opaque identifier")
    if not _OPAQUE_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{name} must be an opaque identifier")
    lowered = value.lower()
    if any(pattern.search(lowered) for pattern in _SECRET_LIKE_PATTERNS):
        raise ValueError(f"{name} must be an opaque identifier")
    return value


def _require_reason_code(value: object) -> str:
    if type(value) is not str or value not in _VALID_REASON_CODES:
        raise ValueError("reason_code is unsupported")
    return value


def _require_writer_marker_state(value: object) -> str:
    if type(value) is not str or value not in _WRITER_MARKER_CONTENTS:
        raise StoreUnavailableError("writer marker state is invalid")
    return value


def _read_writer_marker_state(fd: int) -> str:
    try:
        metadata = os.fstat(fd)
        _validate_private_file(metadata, sidecar=True)
        content = os.pread(
            fd, max(len(item) for item in _WRITER_MARKER_CONTENTS.values()) + 1, 0
        )
    except OSError as exc:
        raise StoreUnavailableError("writer marker content cannot be read") from exc
    for state, expected in _WRITER_MARKER_CONTENTS.items():
        if content == expected:
            return state
    raise StoreUnavailableError("writer marker content is invalid")


def _write_writer_marker_state(fd: int, state: str) -> None:
    state = _require_writer_marker_state(state)
    content = _WRITER_MARKER_CONTENTS[state]
    try:
        os.ftruncate(fd, 0)
        offset = 0
        while offset < len(content):
            written = os.pwrite(fd, content[offset:], offset)
            if written <= 0:
                raise OSError("writer marker write was incomplete")
            offset += written
    except OSError as exc:
        raise StoreUnavailableError("writer marker content cannot be written") from exc


def _require_evidence_ref(value: object) -> str:
    if type(value) is not str or not _EVIDENCE_REF_RE.fullmatch(value):
        raise ValueError("evidence_ref must be a sha256 digest reference")
    return value


def _require_status(value: object, name: str = "status") -> str:
    if type(value) is not str or value not in _VALID_STATUSES:
        raise ValueError(f"{name} is unsupported")
    return value


def _require_rebase_mode(value: object) -> RecoveryRebaseMode:
    if type(value) is not str or value not in _VALID_REBASE_MODES:
        raise ValueError("rebase mode is unsupported")
    return cast(RecoveryRebaseMode, value)


def _require_optional_status(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _require_status(value, name)


def _raise_sqlite_write_error(error: sqlite3.DatabaseError) -> NoReturn:
    message = str(error).lower()
    mapped: StoreError
    if isinstance(error, sqlite3.OperationalError):
        if "locked" in message or "busy" in message:
            mapped = StoreBusyError("SQLite busy timeout expired while writing")
        elif any(
            marker in message
            for marker in ("malformed", "not a database", "corrupt", "integrity")
        ):
            mapped = StoreIntegrityError("SQLite write found an integrity failure")
        else:
            mapped = StoreUnavailableError("SQLite write failed")
    elif any(
        marker in message
        for marker in ("not authorized", "readonly", "read-only", "disk i/o")
    ):
        mapped = StoreUnavailableError("SQLite write failed")
    elif isinstance(error, sqlite3.IntegrityError):
        mapped = StoreIntegrityError("SQLite write violated a store constraint")
    else:
        mapped = StoreIntegrityError("SQLite write failed")
    _adopt_cleanup_capability(mapped, error)
    raise mapped from error


def _raise_recovery_read_error(
    error: sqlite3.DatabaseError | TypeError | ValueError | OverflowError,
    *,
    kind: str,
) -> NoReturn:
    if isinstance(error, sqlite3.DatabaseError):
        raise StoreIntegrityError(f"SQLite {kind} query failed") from error
    raise StoreIntegrityError(f"SQLite {kind} data is invalid") from error


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    """Caller-facing observation of durable operation state."""

    operation_id: str
    effect_key: str
    status: str
    attempt: int
    recovery_epoch: int
    created_ns: int
    updated_ns: int
    provider_id: str | None = None

    def __post_init__(self) -> None:
        _require_opaque_identifier(self.operation_id, "operation_id")
        _require_opaque_identifier(self.effect_key, "effect_key")
        _require_status(self.status)
        _require_sqlite_integer(self.attempt, "attempt")
        _require_sqlite_integer(self.recovery_epoch, "recovery_epoch")
        _require_sqlite_integer(self.created_ns, "created_ns")
        _require_sqlite_integer(self.updated_ns, "updated_ns")
        if self.provider_id is not None:
            _require_opaque_identifier(self.provider_id, "provider_id")


@dataclass(frozen=True, slots=True)
class TransitionEvent:
    """Caller-facing immutable observation of one journal entry."""

    sequence: int
    event_schema_version: int
    operation_id: str
    attempt: int
    from_status: str | None
    to_status: str
    kind: str
    actor: str
    clock_ns: int
    reason_code: str
    evidence_ref: str | None

    def __post_init__(self) -> None:
        _require_sqlite_integer(self.sequence, "sequence", minimum=1)
        _require_sqlite_integer(
            self.event_schema_version,
            "event_schema_version",
            minimum=1,
        )
        if self.event_schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError("event_schema_version is unsupported")
        _require_opaque_identifier(self.operation_id, "operation_id")
        _require_sqlite_integer(self.attempt, "attempt")
        _require_optional_status(self.from_status, "from_status")
        _require_status(self.to_status, "to_status")
        if type(self.kind) is not str or self.kind not in _VALID_EVENT_KINDS:
            raise ValueError("kind is unsupported")
        _require_opaque_identifier(self.actor, "actor")
        _require_sqlite_integer(self.clock_ns, "clock_ns")
        _require_reason_code(self.reason_code)
        if (self.kind, self.reason_code) not in _EVENT_REASON_PAIRS:
            raise ValueError("event kind and reason_code are inconsistent")
        if self.evidence_ref is not None:
            _require_evidence_ref(self.evidence_ref)


def _coerce_state_root(value: object) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise StoreError("state_root is invalid")
    try:
        state_root = Path(value).expanduser().absolute()
    except (TypeError, ValueError, RuntimeError) as exc:
        raise ValueError("state_root is invalid") from exc
    if state_root.name == "state.json" or state_root.suffix.lower() == ".json":
        raise StoreUnavailableError("state_root must not be a JSON state path")
    if not state_root.parts or state_root == Path(state_root.root):
        raise ValueError("state_root must be a private directory")
    return state_root


def _current_uid() -> int:
    try:
        return os.getuid()
    except AttributeError as exc:
        raise StoreUnavailableError("private state ownership is unsupported") from exc


def _open_flags(*, directory: bool, writable: bool) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0:
        raise StoreUnavailableError("secure no-follow open is unavailable")
    flags = os.O_CLOEXEC | nofollow
    flags |= os.O_RDONLY if not writable else os.O_RDWR
    if directory:
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        if directory_flag == 0:
            raise StoreUnavailableError("secure directory open is unavailable")
        flags |= directory_flag
    return flags


def _validate_directory_fd(fd: int, *, state_root: bool) -> None:
    try:
        metadata = os.fstat(fd)
    except OSError as exc:
        raise StoreUnavailableError(
            "private state directory cannot be inspected"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise StoreUnavailableError("private state path is not a directory")
    mode = stat.S_IMODE(metadata.st_mode)
    if state_root:
        if metadata.st_uid != _current_uid() or mode != 0o700:
            raise StoreUnavailableError(
                "private state root ownership or mode is unsafe"
            )
    elif mode & 0o022 and not mode & stat.S_ISVTX:
        raise StoreUnavailableError(
            "private state ancestor is writable by another user"
        )


def _open_state_root(state_root: Path) -> int:
    directory_flags = _open_flags(directory=True, writable=False)
    root_fd: int | None = None
    root_identity: tuple[int, int] | None = None
    current_fd: int | None = None
    current_identity: tuple[int, int] | None = None
    result_fd: int | None = None
    result_identity: tuple[int, int] | None = None
    body_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    owners: list[_FDRecoveryOwner] = []

    def remember_cleanup(
        error: BaseException | None,
        owner: _FDRecoveryOwner | None,
    ) -> None:
        nonlocal cleanup_error
        if error is not None:
            if cleanup_error is None:
                cleanup_error = error
            if owner is not None:
                owners.append(owner)

    def close_current(label: str) -> None:
        nonlocal current_fd, current_identity, root_fd, root_identity
        fd = current_fd
        identity = current_identity
        if fd == root_fd:
            identity = root_identity
        current_fd = None
        current_identity = None
        if fd is None:
            return
        if fd == root_fd:
            root_fd = None
            root_identity = None
        error, owner = _attempt_fd_cleanup(fd, identity, label)
        remember_cleanup(error, owner)

    def close_root(label: str) -> None:
        nonlocal root_fd, root_identity
        fd = root_fd
        identity = root_identity
        root_fd = None
        root_identity = None
        if fd is None:
            return
        error, owner = _attempt_fd_cleanup(fd, identity, label)
        remember_cleanup(error, owner)

    def store_error(error: BaseException, message: str) -> BaseException:
        if isinstance(error, StoreError):
            return error
        wrapped = StoreUnavailableError(message)
        wrapped.__cause__ = error
        return wrapped

    try:
        try:
            root_identity = _identity(os.stat(os.sep, follow_symlinks=False))
        except OSError as exc:
            body_error = store_error(
                exc,
                "private state root path cannot be inspected",
            )
        except _CLEANUP_EXCEPTION as exc:
            body_error = store_error(
                exc,
                "private state root path status is unknown",
            )
        root_fd = os.open(os.sep, directory_flags)
        current_fd = root_fd
        if body_error is None:
            try:
                actual_root_identity = _identity(os.fstat(root_fd))
            except OSError as exc:
                body_error = store_error(
                    exc,
                    "private state root descriptor cannot be inspected",
                )
            except _CLEANUP_EXCEPTION as exc:
                body_error = store_error(
                    exc,
                    "private state root descriptor status is unknown",
                )
            else:
                if actual_root_identity != root_identity:
                    body_error = StoreUnavailableError(
                        "private state root changed while opening"
                    )
                root_identity = actual_root_identity
        components = state_root.parts[1:]
        if body_error is None:
            for index, component in enumerate(components):
                next_fd: int | None = None
                next_identity: tuple[int, int] | None = None
                try:
                    try:
                        next_identity = _identity(
                            os.stat(
                                component,
                                dir_fd=current_fd,
                                follow_symlinks=False,
                            )
                        )
                    except FileNotFoundError as exc:
                        raise store_error(
                            exc,
                            "private state path cannot be inspected",
                        )
                    except OSError as exc:
                        raise store_error(
                            exc,
                            "private state path cannot be inspected",
                        )
                    next_fd = os.open(component, directory_flags, dir_fd=current_fd)
                    try:
                        actual_next_identity = _identity(os.fstat(next_fd))
                    except OSError as exc:
                        raise store_error(
                            exc,
                            "private state traversal descriptor cannot be inspected",
                        )
                    except _CLEANUP_EXCEPTION as exc:
                        raise store_error(
                            exc,
                            "private state traversal descriptor status is unknown",
                        )
                    if actual_next_identity != next_identity:
                        raise StoreUnavailableError(
                            "private state path changed while opening"
                        )
                    next_identity = actual_next_identity
                    _validate_directory_fd(
                        next_fd,
                        state_root=index == len(components) - 1,
                    )
                except _CLEANUP_EXCEPTION as exc:
                    body_error = store_error(
                        exc,
                        "private state traversal failed",
                    )
                    if next_fd is not None:
                        error, owner = _attempt_fd_cleanup(
                            next_fd,
                            next_identity,
                            "state root traversal next descriptor",
                        )
                        remember_cleanup(error, owner)
                    break
                if current_fd is not None and current_fd != root_fd:
                    close_current("state root traversal current descriptor")
                current_fd = next_fd
                current_identity = next_identity
            if body_error is None and current_fd == root_fd:
                body_error = StoreUnavailableError("private state root is invalid")
            elif body_error is None:
                result_fd = current_fd
                result_identity = current_identity
                current_fd = None
                current_identity = None
                close_root("state root traversal root descriptor")
                if cleanup_error is not None and result_fd is not None:
                    error, owner = _attempt_fd_cleanup(
                        result_fd,
                        result_identity,
                        "state root traversal result descriptor",
                    )
                    remember_cleanup(error, owner)
                    result_fd = None
    except StoreError as exc:
        body_error = exc
    except OSError as exc:
        body_error = StoreUnavailableError(
            "private state root cannot be securely opened"
        )
        body_error.__cause__ = exc
    except _CLEANUP_EXCEPTION as exc:
        body_error = store_error(
            exc,
            "private state root status is unknown",
        )
    finally:
        if result_fd is None:
            close_current("state root traversal current descriptor")
            close_root("state root traversal root descriptor")

    if body_error is not None:
        if cleanup_error is not None or owners:
            capability = _CleanupCapability(_FDRecoveryGroup(owners).retry_cleanup)
            attached = _attach_cleanup_capability(body_error, capability)
            if attached is not body_error:
                raise attached from body_error
        raise body_error
    if cleanup_error is not None or owners:
        capability = _CleanupCapability(_FDRecoveryGroup(owners).retry_cleanup)
        assert cleanup_error is not None
        attached = _attach_cleanup_capability(cleanup_error, capability)
        if attached is not cleanup_error:
            raise attached from cleanup_error
        raise cleanup_error
    if result_fd is None:
        raise StoreUnavailableError("private state root cannot be securely opened")
    return result_fd


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


class _LifetimeGateCleanupOwner:
    """Keep one gate descriptor available until its cleanup is certain."""

    __slots__ = ("_expected_identity", "_fd", "_label", "_locked")

    def __init__(
        self,
        fd: int,
        expected_identity: tuple[int, int],
        *,
        locked: bool,
        label: str = "coordination lifetime gate",
    ) -> None:
        self._fd: int | None = fd
        self._expected_identity: tuple[int, int] | None = expected_identity
        self._locked = locked
        self._label = label

    def mark_locked(self) -> None:
        self._locked = True

    def bind_identity(self, identity: tuple[int, int]) -> None:
        self._expected_identity = identity

    def _drop(self) -> None:
        self._fd = None
        self._expected_identity = None
        self._locked = False

    def _check_identity(self, fd: int) -> bool:
        try:
            metadata = os.fstat(fd)
        except _CLEANUP_EXCEPTION as exc:
            if isinstance(exc, OSError) and exc.errno == errno.EBADF:
                self._drop()
                return False
            raise StoreUnavailableError(
                f"{self._label} descriptor status is unknown"
            ) from exc
        expected_identity = self._expected_identity
        if expected_identity is None:
            raise StoreUnavailableError(
                f"{self._label} descriptor identity is unavailable"
            )
        if _identity(metadata) != expected_identity:
            self._drop()
            raise StoreUnavailableError(f"{self._label} descriptor was reused")
        return True

    def retry_cleanup(self) -> None:
        """Release and close the gate once, retaining uncertain state on error."""

        fd = self._fd
        if fd is None or not self._check_identity(fd):
            return

        first_error: BaseException | None = None
        if self._locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except _CLEANUP_EXCEPTION as exc:
                first_error = exc
            try:
                metadata = os.fstat(fd)
            except _CLEANUP_EXCEPTION as status_error:
                if (
                    isinstance(status_error, OSError)
                    and status_error.errno == errno.EBADF
                ):
                    self._drop()
                    if first_error is None:
                        return
                elif first_error is None:
                    first_error = StoreUnavailableError(
                        f"{self._label} descriptor status is unknown"
                    )
                raise StoreUnavailableError(
                    f"{self._label} cleanup failed"
                ) from first_error
            expected_identity = self._expected_identity
            if expected_identity is None:
                self._drop()
                error = StoreUnavailableError(
                    f"{self._label} descriptor identity is unavailable"
                )
                if first_error is None:
                    first_error = error
                raise StoreUnavailableError(
                    f"{self._label} cleanup failed"
                ) from first_error
            if _identity(metadata) != expected_identity:
                self._drop()
                error = StoreUnavailableError(f"{self._label} descriptor was reused")
                if first_error is None:
                    first_error = error
                raise StoreUnavailableError(
                    f"{self._label} cleanup failed"
                ) from first_error
            if first_error is None:
                self._locked = False

        try:
            os.close(fd)
        except _CLEANUP_EXCEPTION as exc:
            if first_error is None:
                first_error = exc
            try:
                metadata = os.fstat(fd)
            except _CLEANUP_EXCEPTION as status_error:
                if (
                    isinstance(status_error, OSError)
                    and status_error.errno == errno.EBADF
                ):
                    self._drop()
                elif first_error is None:
                    first_error = StoreUnavailableError(
                        f"{self._label} descriptor status is unknown"
                    )
            else:
                expected_identity = self._expected_identity
                if expected_identity is None:
                    first_error = StoreUnavailableError(
                        f"{self._label} descriptor identity is unavailable"
                    )
                elif _identity(metadata) != expected_identity:
                    self._drop()
                    if first_error is None:
                        first_error = StoreUnavailableError(
                            f"{self._label} descriptor was reused"
                        )
        else:
            self._drop()

        if first_error is not None:
            raise StoreUnavailableError(
                f"{self._label} cleanup failed"
            ) from first_error


def _validate_private_file(
    metadata: os.stat_result,
    *,
    sidecar: bool,
    require_mode: bool = True,
) -> None:
    expected_mode = 0o600
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != _current_uid()
        or metadata.st_nlink != 1
        or (require_mode and stat.S_IMODE(metadata.st_mode) != expected_mode)
    ):
        label = "sidecar" if sidecar else "database"
        raise StoreUnavailableError(f"private SQLite {label} file is unsafe")


def _fresh_cleanup_error(label: str, message: str, cause: BaseException) -> StoreError:
    error = StoreUnavailableError(f"fresh {label} cleanup {message}")
    error.__cause__ = cause
    return error


def _unlink_identity_tracked(
    path: str | Path,
    expected_identity: tuple[int, int] | None,
    *,
    dir_fd: int | None,
    sidecar: bool,
    label: str,
) -> tuple[bool, bool, BaseException | None]:
    """Unlink one fresh path only while its observed identity still matches."""

    if expected_identity is None:
        return True, False, None
    try:
        metadata = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True, False, None
    except _CLEANUP_EXCEPTION as exc:
        return False, False, _fresh_cleanup_error(label, "status is unknown", exc)
    if _identity(metadata) != expected_identity:
        return True, False, None
    try:
        _validate_private_file(metadata, sidecar=sidecar)
    except _CLEANUP_EXCEPTION as exc:
        return False, False, _fresh_cleanup_error(label, "path is unsafe", exc)
    try:
        os.unlink(path, dir_fd=dir_fd)
    except _CLEANUP_EXCEPTION as exc:
        try:
            after = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True, True, _fresh_cleanup_error(label, "status is unknown", exc)
        except _CLEANUP_EXCEPTION as status_error:
            return (
                False,
                False,
                _fresh_cleanup_error(
                    label,
                    "status is unknown",
                    status_error,
                ),
            )
        if _identity(after) != expected_identity:
            return True, True, _fresh_cleanup_error(label, "status is unknown", exc)
        return False, False, _fresh_cleanup_error(label, "failed", exc)
    return True, True, None


def _image_stat_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_image_fd(
    fd: int,
    *,
    label: str,
    allow_empty: bool = False,
) -> tuple[os.stat_result, bytes]:
    if type(fd) is not int or fd < 0:
        raise StoreUnavailableError(f"{label} descriptor is invalid")
    try:
        before = os.fstat(fd)
    except OSError as exc:
        raise StoreUnavailableError(f"{label} descriptor cannot be inspected") from exc
    _validate_private_file(before, sidecar=False)
    if not allow_empty and before.st_size == 0:
        raise StoreIntegrityError(f"{label} image is empty")
    if before.st_size > MAX_IMAGE_BYTES:
        raise StoreIntegrityError(f"{label} image is too large")
    try:
        image = os.pread(fd, before.st_size, 0)
        after = os.fstat(fd)
    except OSError as exc:
        raise StoreUnavailableError(f"{label} image cannot be read") from exc
    _validate_private_file(after, sidecar=False)
    if len(image) != before.st_size or _image_stat_signature(
        before
    ) != _image_stat_signature(after):
        raise StoreUnavailableError(f"{label} image changed while reading")
    if not allow_empty:
        _validate_sqlite_image_header(image, label=label)
    return before, image


def _validate_sqlite_image_header(image: bytes, *, label: str) -> None:
    if len(image) < 100 or image[:16] != b"SQLite format 3\x00":
        raise StoreIntegrityError(f"{label} has an invalid SQLite header")
    page_size = int.from_bytes(image[16:18], "big")
    if page_size == 1:
        page_size = 65_536
    elif page_size < 512 or page_size > 32_768 or page_size & (page_size - 1):
        raise StoreIntegrityError(f"{label} has an invalid SQLite page size")
    if image[18:20] not in (b"\x01\x01", b"\x02\x02"):
        raise StoreIntegrityError(f"{label} has an invalid SQLite format pair")
    if image[20] != 0 or image[21:24] != b"\x40\x20\x20":
        raise StoreIntegrityError(f"{label} has noncanonical SQLite payload bytes")
    if int.from_bytes(image[44:48], "big") != 4:
        raise StoreIntegrityError(f"{label} has an unsupported SQLite schema format")
    if int.from_bytes(image[56:60], "big") != 1:
        raise StoreIntegrityError(f"{label} has a noncanonical SQLite encoding")
    if image[72:92] != b"\x00" * 20:
        raise StoreIntegrityError(f"{label} has noncanonical SQLite reserved bytes")
    page_count = int.from_bytes(image[28:32], "big")
    if page_count < 1 or page_size * page_count != len(image):
        raise StoreIntegrityError(f"{label} has an invalid SQLite page count")


def _memory_image(image: bytes) -> bytes:
    """Make a WAL image loadable by SQLite's in-memory deserializer."""

    _validate_sqlite_image_header(image, label="SQLite image")
    if image[18:20] != b"\x02\x02":
        return image
    normalized = bytearray(image)
    normalized[18] = 1
    normalized[19] = 1
    return bytes(normalized)


def _wal_image(image: bytes) -> bytes:
    """Emit the serialized candidate with SQLite's WAL header marker."""

    if len(image) < 20:
        return image
    wal_image = bytearray(image)
    wal_image[18] = 2
    wal_image[19] = 2
    return bytes(wal_image)


def _write_image_fd(fd: int, image: bytes, *, label: str) -> os.stat_result:
    if type(fd) is not int or fd < 0:
        raise StoreUnavailableError(f"{label} descriptor is invalid")
    write_started = False
    try:
        before = os.fstat(fd)
    except OSError as exc:
        raise StoreUnavailableError(f"{label} descriptor cannot be inspected") from exc
    _validate_private_file(before, sidecar=False)
    if before.st_size != 0:
        raise LeaseConflictError(f"{label} target is not empty")
    try:
        write_started = True
        os.ftruncate(fd, 0)
        offset = 0
        while offset < len(image):
            written = os.pwrite(fd, image[offset:], offset)
            if written <= 0:
                raise OSError("short SQLite image write")
            offset += written
        os.fsync(fd)
        after = os.fstat(fd)
    except OSError as exc:
        if write_started:
            raise StoreCommitUnknownError(
                f"{label} durability or identity is unknown"
            ) from exc
        raise StoreUnavailableError(f"{label} image cannot be written") from exc
    try:
        _validate_private_file(after, sidecar=False)
    except StoreError as exc:
        raise StoreCommitUnknownError(
            f"{label} durability or identity is unknown"
        ) from exc
    if (
        _identity(before) != _identity(after)
        or after.st_size != len(image)
        or _image_stat_signature(before)[:5] != _image_stat_signature(after)[:5]
    ):
        raise StoreCommitUnknownError(f"{label} target changed while writing")
    return after


def _restore_identity_from_snapshot(snapshot: RecoverySnapshot) -> RestoreIdentity:
    return RestoreIdentity(
        operation_id=snapshot.operation_id,
        effect_key=snapshot.effect_key,
    )


def _canonical_restore_identities(
    identities: object,
    *,
    label: str,
) -> tuple[RestoreIdentity, ...]:
    if type(identities) is not tuple or any(
        type(identity) is not RestoreIdentity for identity in identities
    ):
        raise StoreIntegrityError(f"{label} are invalid")
    keys = tuple(
        (identity.operation_id, identity.effect_key) for identity in identities
    )
    if keys != tuple(sorted(set(keys))):
        raise StoreIntegrityError(f"{label} are not canonical")
    return identities


def _normal_open_state_values(
    value: object,
) -> tuple[tuple[RestoreIdentity, ...], object | None]:
    """Validate Recovery's issuer-only normal-open value without coercion."""

    try:
        from . import recovery as _recovery

        state_type = getattr(_recovery, "NormalOpenRecoveryState", None)
        validator = getattr(_recovery, "_validate_normal_open_recovery_state", None)
        if state_type is None or validator is None or type(value) is not state_type:
            raise StoreUnavailableError(
                "recovery preflight returned an unsupported state"
            )
        validator(value)
        identities = object.__getattribute__(value, "active_committed_tombstones")
        latest = object.__getattribute__(value, "latest_committed_handle")
    except StoreError:
        raise
    except BaseException as exc:
        raise StoreUnavailableError("recovery preflight state is invalid") from exc
    active = _canonical_restore_identities(
        identities,
        label="recovery active committed tombstones",
    )
    if active and latest is None:
        raise StoreUnavailableError(
            "recovery active tombstones have no committed handle"
        )
    return active, latest


def _normal_open_state_keys(value: object) -> tuple[tuple[str, str], ...]:
    identities, _ = _normal_open_state_values(value)
    return tuple(
        (identity.operation_id, identity.effect_key) for identity in identities
    )


def _restore_active_identities(value: object) -> tuple[RestoreIdentity, ...]:
    """Accept only the typed Recovery state or one canonical identity tuple."""

    try:
        identities, _ = _normal_open_state_values(value)
    except StoreUnavailableError:
        if type(value) is not tuple:
            raise StoreIntegrityError("restore active tombstones are invalid")
        identities = _canonical_restore_identities(
            value,
            label="restore active tombstones",
        )
    return identities


def _restore_history_binding_ref(value: object) -> str | None:
    """Derive the stable binding for Recovery's latest committed state."""

    active_tombstones, latest = _normal_open_state_values(value)
    if latest is None:
        return None
    try:
        current_tombstones = object.__getattribute__(latest, "identities")
        restore_generation = object.__getattribute__(latest, "restore_generation")
        actor = object.__getattribute__(latest, "actor")
        audit_ref = object.__getattribute__(latest, "audit_ref")
        source_digest = object.__getattribute__(latest, "backup_digest")
        previous_primary_digest = object.__getattribute__(
            latest,
            "previous_primary_digest",
        )
        previous_recovery_epoch = object.__getattribute__(
            latest,
            "previous_recovery_epoch",
        )
        previous_fencing_token_hwm = object.__getattribute__(
            latest,
            "previous_fencing_token_hwm",
        )
        previous_last_clock_ns = object.__getattribute__(
            latest,
            "previous_last_clock_ns",
        )
        final_floor = RecoveryFloor(
            object.__getattribute__(latest, "recovery_epoch"),
            object.__getattribute__(latest, "fencing_token_floor"),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise StoreIntegrityError("recovery history handle is invalid") from exc
    current_tombstones = _canonical_restore_identities(
        current_tombstones,
        label="recovery current tombstones",
    )
    audit_evidence_ref = (
        "sha256:" + hashlib.sha256(audit_ref.encode("utf-8")).hexdigest()
    )
    try:
        return _restore_binding_digest(
            restore_generation=restore_generation,
            actor=actor,
            audit_evidence_ref=audit_evidence_ref,
            source_digest=source_digest,
            previous_primary_digest=previous_primary_digest,
            previous_recovery_epoch=previous_recovery_epoch,
            previous_fencing_token_hwm=previous_fencing_token_hwm,
            previous_last_clock_ns=previous_last_clock_ns,
            final_floor=final_floor,
            current_tombstones=current_tombstones,
            active_tombstones=active_tombstones,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise StoreIntegrityError("recovery history binding is invalid") from exc


def _schema_objects_for_connection(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    """Read the exact schema object contract from an existing connection."""

    objects: dict[tuple[str, str], str] = {}
    rows = connection.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE type IN ('table', 'index', 'trigger', 'view')
          AND name NOT LIKE 'sqlite_autoindex_%'
        """
    ).fetchall()
    for row in rows:
        sql = row[2]
        if not isinstance(sql, str):
            raise StoreSchemaError("SQLite store object SQL is invalid")
        objects[(str(row[0]), str(row[1]))] = _normalize_sql(sql)
    return objects


def _index_contract_for_connection(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
    contracts: list[tuple[int, str, tuple[str, ...]]] = []
    for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
        index_name = str(row[1])
        columns = tuple(
            str(column[2])
            for column in connection.execute(
                f"PRAGMA index_info({index_name})"
            ).fetchall()
        )
        contracts.append((int(row[2]), str(row[3]), columns))
    return tuple(sorted(contracts))


def _foreign_key_contract_for_connection(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, str, str, str, str], ...]:
    contracts = [
        (
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]).upper(),
            str(row[6]).upper(),
        )
        for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    ]
    return tuple(sorted(contracts))


def _validate_existing_schema(connection: sqlite3.Connection) -> None:
    """Validate an existing SQLite database without configuring or writing it.

    This is the read-only counterpart of ``CoordinationStore._validate_schema``.
    The schema constants and exact contracts remain owned by this module; a
    doctor can therefore inspect an immutable connection without constructing a
    store or duplicating DDL/version definitions.
    """

    try:
        objects = _schema_objects_for_connection(connection)
        expected_objects = {
            key: _normalize_sql(sql) for key, sql in _EXPECTED_OBJECT_SQL.items()
        }
        user_version_row = connection.execute("PRAGMA user_version").fetchone()
        if user_version_row is None:
            raise StoreSchemaError("SQLite user_version is unavailable")
        user_version = _require_sqlite_integer(user_version_row[0], "user_version")
        if not objects:
            if user_version != 0:
                raise StoreSchemaError(
                    "empty SQLite database has an unsupported version"
                )
            raise StoreSchemaError("SQLite database has no coordination schema")
        if objects != expected_objects:
            raise StoreSchemaError("SQLite store objects do not match schema")
        metadata = {
            str(row[0]): row[1]
            for row in connection.execute(
                "SELECT key, value FROM store_meta"
            ).fetchall()
        }
        if frozenset(metadata) != _EXPECTED_META_KEYS:
            raise StoreSchemaError("SQLite store metadata keys do not match schema")
        if (
            type(metadata["store_schema"]) is not int
            or metadata["store_schema"] != STORE_SCHEMA
            or type(metadata["recovery_epoch"]) is not int
            or not 0 <= metadata["recovery_epoch"] <= SQLITE_INTEGER_MAX
            or type(metadata["fencing_token_floor"]) is not int
            or not 0 <= metadata["fencing_token_floor"] <= SQLITE_INTEGER_MAX
            or type(metadata["last_clock_ns"]) is not int
            or not 0 <= metadata["last_clock_ns"] <= SQLITE_INTEGER_MAX
        ):
            raise StoreSchemaError("SQLite store metadata is invalid")
        if user_version != STORE_SCHEMA:
            raise StoreSchemaError("SQLite user_version does not match store schema")
        for table, expected_columns in _EXPECTED_COLUMNS.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual_columns = tuple(
                (
                    str(row[1]),
                    str(row[2]).upper(),
                    int(row[3]),
                    int(row[5]),
                )
                for row in rows
            )
            if actual_columns != expected_columns:
                raise StoreSchemaError("SQLite store columns do not match schema")
        for table, expected_indexes in _EXPECTED_INDEX_CONTRACT.items():
            actual_indexes = _index_contract_for_connection(connection, table)
            if actual_indexes != tuple(sorted(expected_indexes)):
                raise StoreSchemaError("SQLite store indexes do not match schema")
        for table, expected_foreign_keys in _EXPECTED_FOREIGN_KEYS.items():
            actual_foreign_keys = _foreign_key_contract_for_connection(
                connection,
                table,
            )
            if actual_foreign_keys != tuple(sorted(expected_foreign_keys)):
                raise StoreSchemaError("SQLite store foreign keys do not match schema")
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity_row is None or str(integrity_row[0]).lower() != "ok":
            raise StoreIntegrityError("SQLite integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise StoreIntegrityError("SQLite foreign_key_check failed")
        for row in connection.execute(
            """
            SELECT event_id, event_schema_version, operation_id, attempt,
                   from_status, to_status, kind, actor, clock_ns,
                   reason_code, evidence_ref
            FROM transition_events
            ORDER BY event_id
            """
        ).fetchall():
            try:
                TransitionEvent(
                    sequence=row[0],
                    event_schema_version=row[1],
                    operation_id=row[2],
                    attempt=row[3],
                    from_status=row[4],
                    to_status=row[5],
                    kind=row[6],
                    actor=row[7],
                    clock_ns=row[8],
                    reason_code=row[9],
                    evidence_ref=row[10],
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise StoreIntegrityError("SQLite transition event is invalid") from exc
    except (StoreError, sqlite3.DatabaseError):
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise StoreSchemaError("SQLite store schema data is invalid") from exc


@dataclass(frozen=True, slots=True)
class ExistingOperationObservation:
    """Minimal validated operation view shared with read-only recovery code."""

    operation_id: str
    status: str
    owner: str | None
    attempt: int

    def __post_init__(self) -> None:
        _require_opaque_identifier(self.operation_id, "operation_id")
        _require_status(self.status)
        if self.owner is not None:
            _require_opaque_identifier(self.owner, "owner")
        _require_sqlite_integer(self.attempt, "attempt")


def _read_existing_operation(
    connection: sqlite3.Connection,
    operation_id: str,
) -> ExistingOperationObservation | None:
    """Read one operation through pure recovery-row validation."""

    try:
        row, receipt_row = _existing_operation_rows(connection, operation_id)
        if row is None:
            return None
        return _validate_existing_operation_rows(row, receipt_row)
    except StoreError:
        raise
    except sqlite3.DatabaseError as exc:
        raise StoreIntegrityError("SQLite operation observation failed") from exc
    except (TypeError, ValueError, OverflowError) as exc:
        raise StoreIntegrityError("SQLite operation observation is invalid") from exc


def _existing_operation_rows(
    connection: sqlite3.Connection,
    operation_id: str,
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    """Fetch operation/attempt/receipt rows and distinguish missing attempts."""

    operation_id = _require_opaque_identifier(operation_id, "operation_id")
    row = CoordinationStore._fetch_attempt(connection, operation_id)
    if row is None:
        operation_row = connection.execute(
            "SELECT operation_id FROM operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if operation_row is not None:
            raise StoreIntegrityError("SQLite operation current attempt is unavailable")
        return None, None
    receipt_row = connection.execute(
        """
        SELECT operation_id, attempt, effect_key, provider_effect_id,
               provider_status, provider_id, owner, fencing_token,
               lease_epoch, received_ns, proof_version, proof_ref
        FROM effect_receipts
        WHERE operation_id = ? AND attempt = ?
        """,
        (operation_id, row["attempt"]),
    ).fetchone()
    return row, receipt_row


def _validate_existing_receipt_row(
    row: sqlite3.Row,
    receipt_row: sqlite3.Row,
) -> None:
    """Validate persisted receipt/proof identity without issuing authority values."""

    operation_id = row["operation_id"]
    effect_key = row["effect_key"]
    attempt = row["attempt"]
    owner = row["owner"]
    provider_id = row["attempt_provider_id"]
    lease_epoch = row["lease_epoch"]
    fencing_token = row["fencing_token"]
    proof_version = row["fence_proof_version"]
    proof_ref = row["fence_proof_ref"]
    if (
        receipt_row["operation_id"] != operation_id
        or receipt_row["attempt"] != attempt
        or receipt_row["effect_key"] != effect_key
        or receipt_row["provider_id"] != provider_id
        or receipt_row["owner"] != owner
        or receipt_row["lease_epoch"] != lease_epoch
        or receipt_row["fencing_token"] != fencing_token
        or receipt_row["proof_version"] != proof_version
        or receipt_row["proof_ref"] != proof_ref
    ):
        raise StoreIntegrityError("SQLite provider receipt identity is inconsistent")
    _require_opaque_identifier(receipt_row["operation_id"], "operation_id")
    _require_sqlite_integer(receipt_row["attempt"], "receipt attempt", minimum=1)
    _require_opaque_identifier(receipt_row["effect_key"], "effect_key")
    _require_opaque_identifier(receipt_row["provider_effect_id"], "provider_effect_id")
    if receipt_row["provider_status"] != "COMPLETED":
        raise StoreIntegrityError("SQLite receipt status is invalid")
    _require_opaque_identifier(receipt_row["provider_id"], "provider_id")
    _require_opaque_identifier(receipt_row["owner"], "owner")
    _require_sqlite_integer(receipt_row["fencing_token"], "fencing_token", minimum=1)
    _require_sqlite_integer(receipt_row["lease_epoch"], "lease_epoch")
    _require_sqlite_integer(receipt_row["received_ns"], "received_ns")
    _require_sqlite_integer(receipt_row["proof_version"], "proof_version", minimum=1)
    _require_opaque_identifier(receipt_row["proof_ref"], "proof_ref")


def _validate_existing_operation_rows(
    row: sqlite3.Row,
    receipt_row: sqlite3.Row | None,
    *,
    allow_recovery_epoch_mismatch: bool = False,
) -> ExistingOperationObservation:
    """Validate one operation projection using #31's typed value contracts."""

    try:
        operation_id = _require_opaque_identifier(row["operation_id"], "operation_id")
        _require_opaque_identifier(row["effect_key"], "effect_key")
        status = _require_status(row["status"])
        current_attempt = _require_sqlite_integer(
            row["current_attempt"],
            "current_attempt",
        )
        attempt = _require_sqlite_integer(row["attempt"], "attempt")
        if attempt != current_attempt:
            raise StoreIntegrityError(
                "SQLite operation current attempt is inconsistent"
            )
        operation_provider_id = row["operation_provider_id"]
        attempt_provider_id = row["attempt_provider_id"]
        if operation_provider_id is not None:
            _require_opaque_identifier(operation_provider_id, "provider_id")
        if attempt_provider_id is not None:
            _require_opaque_identifier(attempt_provider_id, "provider_id")
        owner = row["owner"]
        if owner is not None:
            _require_opaque_identifier(owner, "owner")
        recovery_epoch = _require_sqlite_integer(
            row["recovery_epoch"],
            "recovery_epoch",
        )
        lease_epoch = _require_sqlite_integer(row["lease_epoch"], "lease_epoch")
        fencing_token = _require_sqlite_integer(
            row["fencing_token"],
            "fencing_token",
        )
        lease_heartbeat_ns = row["lease_heartbeat_ns"]
        lease_expires_ns = row["lease_expires_ns"]
        fence_proof_version = row["fence_proof_version"]
        fence_proof_ref = row["fence_proof_ref"]
        effect_started_ns = row["effect_started_ns"]
        fence_started_ns = row["fence_started_ns"]
        for value, field_name in (
            (lease_heartbeat_ns, "lease_heartbeat_ns"),
            (lease_expires_ns, "lease_expires_ns"),
            (effect_started_ns, "effect_started_ns"),
            (fence_started_ns, "fence_started_ns"),
        ):
            if value is not None:
                _require_sqlite_integer(value, field_name)
        if (fence_proof_version is None) != (fence_proof_ref is None):
            raise StoreIntegrityError("SQLite fence proof is incomplete")
        if fence_proof_version is not None:
            _require_sqlite_integer(
                fence_proof_version, "fence_proof_version", minimum=1
            )
            _require_opaque_identifier(fence_proof_ref, "fence_proof_ref")
        if current_attempt == 0:
            if (
                status != "INTENT"
                or attempt_provider_id is not None
                or owner is not None
                or fencing_token != 0
                or lease_heartbeat_ns is not None
                or lease_expires_ns is not None
                or fence_proof_version is not None
                or fence_proof_ref is not None
                or effect_started_ns is not None
                or fence_started_ns is not None
            ):
                raise StoreIntegrityError("SQLite intent lease identity is invalid")
        else:
            if (
                status == "INTENT"
                or operation_provider_id is None
                or attempt_provider_id is None
                or operation_provider_id != attempt_provider_id
                or owner is None
                or (recovery_epoch != lease_epoch and not allow_recovery_epoch_mismatch)
                or fencing_token < 1
                or lease_heartbeat_ns is None
                or lease_expires_ns is None
                or lease_expires_ns <= lease_heartbeat_ns
            ):
                raise StoreIntegrityError("SQLite operation lease identity is invalid")
        snapshot = RecoverySnapshot(
            operation_id=operation_id,
            effect_key=row["effect_key"],
            provider_id=operation_provider_id,
            status=status,
            updated_ns=row["updated_ns"],
            current_attempt=attempt,
            recovery_epoch=recovery_epoch,
            owner=owner,
            lease_heartbeat_ns=lease_heartbeat_ns,
            lease_expires_ns=lease_expires_ns,
            lease_epoch=lease_epoch,
            fencing_token=fencing_token,
            fence_proof_version=fence_proof_version,
            fence_proof_ref=fence_proof_ref,
            effect_started_ns=effect_started_ns,
            fence_started_ns=fence_started_ns,
        )
        _validate_recovery_marker_fields(snapshot)
        if status in {"RECEIPTED", "COMPLETED"}:
            if receipt_row is None:
                raise StoreIntegrityError("SQLite receipted operation has no receipt")
            _validate_existing_receipt_row(row, receipt_row)
        elif receipt_row is not None:
            raise StoreIntegrityError("SQLite non-receipted operation has a receipt")
        return ExistingOperationObservation(
            operation_id=operation_id,
            status=status,
            owner=owner,
            attempt=attempt,
        )
    except StoreError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise StoreIntegrityError("SQLite operation observation is invalid") from exc


def _validate_recovery_marker_fields(snapshot: RecoverySnapshot) -> None:
    """Apply the #31 marker invariants to one validated recovery snapshot."""

    if snapshot.status == "FENCE_PENDING":
        if (
            snapshot.fence_started_ns is not None
            or snapshot.fence_proof_version is not None
            or snapshot.effect_started_ns is not None
        ):
            raise StoreIntegrityError("SQLite pending lease marker is invalid")
    elif snapshot.status == "FENCE_RESERVATION_STARTED":
        if (
            snapshot.fence_started_ns is None
            or snapshot.fence_proof_version is not None
            or snapshot.effect_started_ns is not None
        ):
            raise StoreIntegrityError("SQLite fence reservation marker is invalid")
    elif snapshot.status == "CLAIMED":
        if (
            snapshot.fence_started_ns is None
            or snapshot.fence_proof_version is None
            or snapshot.effect_started_ns is not None
        ):
            raise StoreIntegrityError("SQLite activated lease marker is invalid")
    elif snapshot.status in {"EFFECT_PREPARED", "RECEIPTED", "COMPLETED"} and (
        snapshot.fence_started_ns is None
        or snapshot.fence_proof_version is None
        or snapshot.effect_started_ns is None
    ):
        raise StoreIntegrityError("SQLite prepared effect marker is incomplete")


def _read_existing_recovery_snapshot(
    connection: sqlite3.Connection,
    operation_id: str,
) -> RecoverySnapshot | None:
    """Read through the canonical typed recovery/receipt validation path."""

    operation_id = _require_opaque_identifier(operation_id, "operation_id")
    try:
        row, receipt_row = _existing_operation_rows(connection, operation_id)
        if row is None:
            return None
        _validate_existing_operation_rows(
            row,
            receipt_row,
            allow_recovery_epoch_mismatch=True,
        )
        receipt: VerifiedProviderReceipt | None = None
        if receipt_row is not None:
            receipt = CoordinationStore._receipt_from_row(row, receipt_row)
        status = row["status"]
        if status in {"RECEIPTED", "COMPLETED"} and receipt is None:
            raise StoreIntegrityError("SQLite receipted operation has no receipt")
        snapshot = RecoverySnapshot(
            operation_id=row["operation_id"],
            effect_key=row["effect_key"],
            provider_id=row["operation_provider_id"],
            status=status,
            updated_ns=row["updated_ns"],
            current_attempt=row["attempt"],
            recovery_epoch=row["recovery_epoch"],
            owner=row["owner"],
            lease_heartbeat_ns=row["lease_heartbeat_ns"],
            lease_expires_ns=row["lease_expires_ns"],
            lease_epoch=row["lease_epoch"],
            fencing_token=row["fencing_token"],
            fence_proof_version=row["fence_proof_version"],
            fence_proof_ref=row["fence_proof_ref"],
            effect_started_ns=row["effect_started_ns"],
            fence_started_ns=row["fence_started_ns"],
            verified_receipt_identity=receipt,
        )
        _validate_recovery_marker_fields(snapshot)
        return snapshot
    except StoreError:
        raise
    except sqlite3.DatabaseError as exc:
        raise StoreIntegrityError("SQLite recovery snapshot query failed") from exc
    except (TypeError, ValueError, OverflowError) as exc:
        raise StoreIntegrityError("SQLite recovery snapshot data is invalid") from exc


def _validate_image_high_water(
    connection: sqlite3.Connection,
    *,
    floor: RecoveryFloor,
    last_clock_ns: int,
) -> int:
    """Validate timestamp and fencing floors across every durable row."""

    maximum_clock = last_clock_ns
    timestamp_columns = (
        ("operations", "created_ns", "created_ns"),
        ("operations", "updated_ns", "updated_ns"),
        ("operation_attempts", "lease_heartbeat_ns", "lease_heartbeat_ns"),
        ("operation_attempts", "effect_started_ns", "effect_started_ns"),
        ("operation_attempts", "fence_started_ns", "fence_started_ns"),
        ("effect_receipts", "received_ns", "received_ns"),
        ("transition_events", "clock_ns", "clock_ns"),
    )
    for table, column, name in timestamp_columns:
        for row in connection.execute(
            f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
        ).fetchall():
            value = _require_sqlite_integer(row[0], name)
            maximum_clock = max(maximum_clock, value)
    for row in connection.execute(
        "SELECT created_ns, updated_ns FROM operations"
    ).fetchall():
        created_ns = _require_sqlite_integer(row[0], "created_ns")
        updated_ns = _require_sqlite_integer(row[1], "updated_ns")
        if created_ns > updated_ns:
            raise StoreIntegrityError("SQLite operation clock order is invalid")

    recovery_epoch = floor.recovery_epoch
    maximum_epoch = 0
    for table, column, name in (
        ("operations", "recovery_epoch", "recovery_epoch"),
        ("operation_attempts", "lease_epoch", "lease_epoch"),
        ("effect_receipts", "lease_epoch", "receipt lease_epoch"),
    ):
        for row in connection.execute(f"SELECT {column} FROM {table}").fetchall():
            maximum_epoch = max(maximum_epoch, _require_sqlite_integer(row[0], name))
    if maximum_epoch > recovery_epoch:
        raise StoreIntegrityError("SQLite recovery epoch exceeds global floor")

    maximum_token = 0
    for table, column, name in (
        ("operation_attempts", "fencing_token", "fencing_token"),
        ("effect_receipts", "fencing_token", "receipt fencing_token"),
    ):
        for row in connection.execute(f"SELECT {column} FROM {table}").fetchall():
            maximum_token = max(
                maximum_token,
                _require_sqlite_integer(row[0], name),
            )
    if maximum_token > floor.fencing_token_floor:
        raise StoreIntegrityError("SQLite fencing token exceeds global floor")
    if maximum_clock > last_clock_ns:
        raise StoreIntegrityError("SQLite durable clock is below row high-water")
    return maximum_token


def _validate_existing_image_high_water(
    connection: sqlite3.Connection,
) -> RecoveryFloor:
    """Validate all durable row high-water values and return the typed floor."""

    try:
        metadata = {
            str(row["key"]): row["value"]
            for row in connection.execute(
                """
                SELECT key, value FROM store_meta
                WHERE key IN ('recovery_epoch', 'fencing_token_floor', 'last_clock_ns')
                """
            ).fetchall()
        }
        if frozenset(metadata) != {
            "recovery_epoch",
            "fencing_token_floor",
            "last_clock_ns",
        }:
            raise StoreIntegrityError("SQLite store high-water metadata is incomplete")
        floor = RecoveryFloor(
            _require_sqlite_integer(metadata["recovery_epoch"], "recovery_epoch"),
            _require_sqlite_integer(
                metadata["fencing_token_floor"],
                "fencing_token_floor",
            ),
        )
        last_clock_ns = _require_sqlite_integer(
            metadata["last_clock_ns"],
            "last_clock_ns",
        )
        _validate_image_high_water(
            connection,
            floor=floor,
            last_clock_ns=last_clock_ns,
        )
        return floor
    except StoreError:
        raise
    except sqlite3.DatabaseError as exc:
        raise StoreIntegrityError("SQLite store high-water query failed") from exc
    except (TypeError, ValueError, OverflowError) as exc:
        raise StoreIntegrityError(
            "SQLite store high-water metadata is invalid"
        ) from exc


class CoordinationStore:
    """Private SQLite store for durable intent and journal state.

    ``state_root`` must already exist as an owner-only mode ``0700`` directory.
    The store creates or opens only ``coordination.sqlite3`` below that root;
    the database and SQLite sidecars must be owner-only mode ``0600`` files.
    Opening is an existing-state validation operation: an interrupted
    fence/effect marker remains a recovery-required status.  Recovery/doctor
    callers must use the typed recovery seam rather than implicit opener
    mutation.
    """

    def __init__(
        self,
        state_root: Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if (
            type(busy_timeout_ms) is not int
            or not 1 <= busy_timeout_ms <= MAX_BUSY_TIMEOUT_MS
        ):
            raise ValueError(
                f"busy_timeout_ms must be between 1 and {MAX_BUSY_TIMEOUT_MS}"
            )
        self.state_root = _coerce_state_root(state_root)
        self.busy_timeout_ms = busy_timeout_ms
        self._clock_injected = clock is not None
        self._clock = clock or time.time_ns
        self._connection: sqlite3.Connection | None = None
        self._connection_cleanup_pending = False
        self._orphan_fds: list[_OrphanFD] = []
        self._detached_fd_owners: list[_FDRecoveryOwner] = []
        self._state_root_fd: int | None = None
        self._state_root_identity: tuple[int, int] | None = None
        self._lifetime_gate_fd: int | None = None
        self._lifetime_gate_identity: tuple[int, int] | None = None
        self._fresh_gate_created_identity: tuple[int, int] | None = None
        self._fresh_gate_fd_identity: tuple[int, int] | None = None
        self._lifetime_gate_required = False
        self._lifetime_gate_shared = False
        self._lifetime_gate_persistent = False
        self._lifetime_gate_cleanup_pending = False
        self._lifetime_gate_condition = threading.Condition()
        self._lifetime_gate_shared_users = 0
        self._lifetime_gate_exclusive_owner: int | None = None
        self._marker_probe_owner: int | None = None
        self._lifetime_gate_local = threading.local()
        self._database_fd: int | None = None
        self._database_identity: tuple[int, int] | None = None
        self._fresh_database_created_identity: tuple[int, int] | None = None
        self._fresh_database_fd_identity: tuple[int, int] | None = None
        self._marker_fd: int | None = None
        self._marker_identity: tuple[int, int] | None = None
        self._fresh_marker_created_identity: tuple[int, int] | None = None
        self._fresh_marker_fd_identity: tuple[int, int] | None = None
        self._initial_marker_identity: tuple[int, int] | None = None
        self._marker_shared = False
        self._marker_probe_failed = False
        self._startup_lock_held = False
        self._sidecars_before_open: frozenset[str] = frozenset()
        self._fresh_sidecar_created_identities: dict[str, tuple[int, int]] = {}
        self._fresh_cleanup_pending = False
        self._fresh_cleanup_sync_pending: set[str] = set()
        self._fresh_cleanup_gate_parent_fd: int | None = None
        self._fresh_cleanup_gate_parent_identity: tuple[int, int] | None = None
        self._schema_empty = False
        self._marker_creation_allowed = False
        self._fresh_bootstrap = False
        self._normal_open_state: object | None = None
        self._committed_tombstones: tuple[tuple[str, str], ...] = ()
        self._last_clock_ns = 0
        fresh_bootstrap_started = False
        try:
            self._state_root_fd = _open_state_root(self.state_root)
            self._state_root_identity = _identity(os.fstat(self._state_root_fd))
            self._acquire_startup_lock()
            first_state = self._run_normal_open_preflight()
            self._normal_open_state = first_state
            self._committed_tombstones = _normal_open_state_keys(first_state)
            initial_database = self._initial_entry(DATABASE_FILENAME)
            initial_marker = self._initial_entry(WRITER_MARKER_FILENAME)
            if initial_marker is not None:
                self._validate_initial_writer_marker(initial_marker)
                self._initial_marker_identity = _identity(initial_marker)
                self._fault("after_initial_marker_validation")
            if (initial_database is None) != (initial_marker is None):
                raise StoreUnavailableError(
                    "coordination database and writer marker must be created together"
                )
            if (
                initial_database is None
                and initial_marker is None
                and self._initial_root_inventory()
            ):
                raise StoreUnavailableError(
                    "fresh coordination state root must be entirely empty"
                )
            if initial_database is not None:
                _validate_private_file(initial_database, sidecar=False)
                if initial_database.st_size < 100:
                    raise StoreUnavailableError(
                        "existing coordination database is empty or truncated"
                    )
                self._reject_nonempty_rollback_journal()
            self._fresh_bootstrap = initial_database is None and initial_marker is None
            fresh_bootstrap_started = self._fresh_bootstrap
            self._lifetime_gate_fd = self._open_lifetime_gate(create=True)
            self._lifetime_gate_required = True
            self._acquire_lifetime_gate(exclusive=False)
            self._lifetime_gate_persistent = True
            second_state = self._run_normal_open_preflight()
            if second_state != self._normal_open_state:
                raise StoreUnavailableError("recovery preflight changed while opening")
            self._normal_open_state = second_state
            if not self._fresh_bootstrap:
                self._open_writer_marker()
            elif self._initial_root_inventory():
                raise StoreUnavailableError(
                    "fresh coordination state root changed before database creation"
                )
            self._database_fd = self._open_database_file(create=self._fresh_bootstrap)
            self._assert_state_root()
            self._sidecars_before_open = self._existing_sidecar_names()
            self._reject_nonempty_rollback_journal()
            database_uri = self._database_uri()
            self._connection = sqlite3.connect(
                database_uri,
                uri=True,
                timeout=busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            self._connection.row_factory = sqlite3.Row
            self._assert_state_root()
            self._assert_database_identity()
            self._assert_connection_identity()
            self._preflight_schema()
            if not self._fresh_bootstrap and self._schema_empty:
                raise StoreUnavailableError(
                    "an established coordination database is empty"
                )
            if not self._fresh_bootstrap:
                self._verify_normal_open_history(self._normal_open_state)
            if not self._fresh_bootstrap and initial_marker is None:
                raise StoreUnavailableError(
                    "writer marker is missing from an initialized store"
                )
            self._marker_creation_allowed = self._fresh_bootstrap and self._schema_empty
            self._assert_state_root()
            self._assert_database_identity()
            self._assert_connection_identity()
            self._configure_pragmas()
            self._track_fresh_sidecars()
            self._enforce_sidecar_modes()
            if self._schema_empty:
                self._initialize_schema()
            self._track_fresh_sidecars()
            self._validate_schema()
            self._load_store_high_water()
            self._validate_prepared_markers()
            if self._fresh_bootstrap:
                self._open_writer_marker()
            self._assert_database_identity()
            fresh_bootstrap_started = False
            self._release_startup_lock()
            self._release_lifetime_gate()
            self._lifetime_gate_persistent = False
        except StoreError as exc:
            cleanup = self._cleanup_failed_initialization(
                fresh_bootstrap_started=fresh_bootstrap_started,
            )
            if cleanup is not None:
                _attach_cleanup_capability(exc, cleanup)
            raise
        except sqlite3.OperationalError as exc:
            cleanup = self._cleanup_failed_initialization(
                fresh_bootstrap_started=fresh_bootstrap_started,
            )
            error: StoreError
            if "locked" in str(exc).lower():
                error = StoreBusyError("SQLite busy timeout expired while opening")
            else:
                error = StoreUnavailableError("SQLite database could not be opened")
            if cleanup is not None:
                _attach_cleanup_capability(error, cleanup)
            raise error from exc
        except sqlite3.DatabaseError as exc:
            cleanup = self._cleanup_failed_initialization(
                fresh_bootstrap_started=fresh_bootstrap_started,
            )
            error = StoreSchemaError("SQLite database is not a valid store")
            if cleanup is not None:
                _attach_cleanup_capability(error, cleanup)
            raise error from exc
        except OSError as exc:
            cleanup = self._cleanup_failed_initialization(
                fresh_bootstrap_started=fresh_bootstrap_started,
            )
            error = StoreUnavailableError("private SQLite state cannot be opened")
            if cleanup is not None:
                _attach_cleanup_capability(error, cleanup)
            raise error from exc
        except BaseException as exc:
            cleanup = self._cleanup_failed_initialization(
                fresh_bootstrap_started=fresh_bootstrap_started,
            )
            if cleanup is not None:
                attached = _attach_cleanup_capability(exc, cleanup)
                if attached is not exc:
                    raise attached from exc
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, traceback
        try:
            self.close()
        except _CLEANUP_EXCEPTION:
            if isinstance(exc_value, BaseException):
                attached = _attach_cleanup_capability(
                    exc_value,
                    _CleanupCapability(self.close),
                )
                if attached is not exc_value:
                    raise attached from exc_value
                return
            raise

    def _cleanup_failed_initialization(
        self,
        *,
        fresh_bootstrap_started: bool,
    ) -> _CleanupCapability | None:
        """Finish constructor cleanup without replacing its primary failure."""

        cleanup_error: BaseException | None = None
        if fresh_bootstrap_started:
            self._fresh_cleanup_pending = True
            try:
                self._cleanup_fresh_bootstrap()
            except _CLEANUP_EXCEPTION as exc:
                cleanup_error = exc
                return _CleanupCapability(self._retry_failed_initialization_cleanup)
            if self._fresh_cleanup_pending:
                return _CleanupCapability(self._retry_failed_initialization_cleanup)
        try:
            self.close()
        except _CLEANUP_EXCEPTION as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if cleanup_error is not None:
            if fresh_bootstrap_started:
                return _CleanupCapability(self._retry_failed_initialization_cleanup)
            return _CleanupCapability(self.close)
        return None

    def _retry_failed_initialization_cleanup(self) -> None:
        cleanup_error: BaseException | None = None
        if self._fresh_cleanup_pending:
            try:
                self._retry_detached_fds()
                self._retry_orphan_fds()
                self._cleanup_fresh_bootstrap()
            except _CLEANUP_EXCEPTION as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error
        self.close()

    def _raise_gate_cleanup_failure(
        self,
        body_error: BaseException | None,
        cleanup_error: BaseException,
    ) -> NoReturn:
        capability = _CleanupCapability(self.close)
        if body_error is not None:
            _raise_with_cleanup_capability(body_error, capability)
        if isinstance(cleanup_error, StoreError):
            _raise_with_cleanup_capability(cleanup_error, capability)
        error = StoreUnavailableError("coordination lifetime gate cleanup failed")
        _attach_cleanup_capability(error, capability)
        raise error from cleanup_error

    def _run_normal_open_preflight(self) -> object:
        """Call Recovery's existing-only guard and preserve its typed state."""

        self._retry_orphan_fds()
        root_fd = self._state_root_fd
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        try:
            from . import recovery as _recovery

            preflight = getattr(_recovery, "_normal_open_preflight", None)
            if preflight is None:
                raise StoreUnavailableError("recovery preflight is unavailable")
            result = preflight(root_fd, retain_fd=self._retain_failed_fd)
            _normal_open_state_values(result)
            return result
        except StoreError:
            raise
        except Exception as exc:
            raise StoreUnavailableError(
                "recovery preflight cannot authorize normal open"
            ) from exc

    def _verify_normal_open_history(self, state: object | None) -> None:
        if state is None:
            raise StoreUnavailableError("recovery preflight state is missing")
        database_fd = self._database_fd
        if database_fd is None:
            raise StoreClosedError("coordination store is closed")
        RestoreStoreAuthority().verify_history_binding(database_fd, state)

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
            if any(existing_fd == fd for existing_fd, _, _ in self._orphan_fds):
                raise StoreUnavailableError(
                    f"{label} descriptor status is unknown"
                ) from exc
            if len(self._orphan_fds) >= _MAX_ORPHAN_FDS:
                raise self._dedicated_constructor_fd_error(
                    fd,
                    expected_identity,
                    label,
                    exc,
                )
            self._orphan_fds.append((fd, expected_identity, label))
            raise StoreUnavailableError(
                f"{label} descriptor status is unknown"
            ) from exc
        actual_identity = _identity(metadata)
        if expected_identity is not None and actual_identity != expected_identity:
            if any(existing_fd == fd for existing_fd, _, _ in self._orphan_fds):
                raise StoreUnavailableError(f"{label} descriptor was reused")
            if len(self._orphan_fds) >= _MAX_ORPHAN_FDS:
                raise self._dedicated_constructor_fd_error(
                    fd,
                    expected_identity,
                    label,
                    StoreUnavailableError(f"{label} descriptor was reused"),
                )
            self._orphan_fds.append((fd, expected_identity, label))
            raise StoreUnavailableError(f"{label} descriptor was reused")
        if any(existing_fd == fd for existing_fd, _, _ in self._orphan_fds):
            return
        if len(self._orphan_fds) >= _MAX_ORPHAN_FDS:
            raise self._dedicated_constructor_fd_error(
                fd,
                expected_identity,
                label,
                StoreUnavailableError("preflight descriptor retry registry is full"),
            )
        retained_identity = expected_identity
        self._orphan_fds.append((fd, retained_identity, label))

    def _resolve_fresh_fd_identity(
        self,
        fd: int,
        observed_identity: tuple[int, int],
        label: str,
    ) -> tuple[int, int] | None:
        del fd
        if not self._fresh_bootstrap:
            return None
        path: str | Path | None = None
        path_dir_fd: int | None = None
        if label == "lifetime gate open":
            path = self.state_root.parent / LIFETIME_GATE_FILENAME
        elif label == "writer marker open":
            path = WRITER_MARKER_FILENAME
            path_dir_fd = self._state_root_fd
        elif label == "database open":
            path = DATABASE_FILENAME
            path_dir_fd = self._state_root_fd
        elif label == "SQLite sidecar open":
            for suffix in ("-wal", "-shm", "-journal"):
                candidate = f"{DATABASE_FILENAME}{suffix}"
                candidate_path: str | Path = candidate
                candidate_dir_fd = self._state_root_fd
                if candidate_dir_fd is None:
                    candidate_path = self.state_root / candidate
                try:
                    metadata = os.stat(
                        candidate_path,
                        dir_fd=candidate_dir_fd,
                        follow_symlinks=False,
                    )
                except _CLEANUP_EXCEPTION:
                    continue
                if _identity(metadata) == observed_identity:
                    self._fresh_sidecar_created_identities.setdefault(
                        candidate,
                        observed_identity,
                    )
                    return observed_identity
            return None
        if path is None:
            return None
        if path_dir_fd is None and path in {
            WRITER_MARKER_FILENAME,
            DATABASE_FILENAME,
        }:
            path = self.state_root / path
        try:
            metadata = os.stat(
                path,
                dir_fd=path_dir_fd,
                follow_symlinks=False,
            )
        except _CLEANUP_EXCEPTION:
            return None
        if _identity(metadata) != observed_identity:
            return None
        if label == "lifetime gate open":
            self._fresh_gate_fd_identity = observed_identity
        elif label == "writer marker open":
            self._fresh_marker_fd_identity = observed_identity
        elif label == "database open":
            self._fresh_database_fd_identity = observed_identity
        return observed_identity

    def _dedicated_constructor_fd_error(
        self,
        fd: int,
        expected_identity: tuple[int, int] | None,
        label: str,
        cause: BaseException,
    ) -> StoreError:
        error = StoreUnavailableError(f"{label} descriptor retry registry is full")
        error.__cause__ = cause
        owner = next(
            (existing for existing in self._detached_fd_owners if existing._fd == fd),
            None,
        )
        if owner is None:
            resolve_identity = None
            if expected_identity is None:
                resolve_identity = lambda current_fd, observed: (
                    self._resolve_fresh_fd_identity(current_fd, observed, label)
                )
            owner = _FDRecoveryOwner(
                fd,
                expected_identity,
                label,
                resolve_identity=resolve_identity,
            )
            if self._fresh_bootstrap:
                self._detached_fd_owners.append(owner)
        error._attach_cleanup_capability(_CleanupCapability(owner.retry_cleanup))
        return error

    def _handoff_constructor_fd(
        self,
        fd: int,
        expected_identity: tuple[int, int] | None,
        label: str,
        *,
        unlock: bool = False,
    ) -> BaseException | None:
        """Close a temporary constructor fd or retain it for identity-safe retry."""

        error: StoreError
        try:
            metadata = os.fstat(fd)
        except _CLEANUP_EXCEPTION as status_error:
            if isinstance(status_error, OSError) and status_error.errno == errno.EBADF:
                return None
            if not any(existing_fd == fd for existing_fd, _, _ in self._orphan_fds):
                if len(self._orphan_fds) >= _MAX_ORPHAN_FDS:
                    return self._dedicated_constructor_fd_error(
                        fd,
                        expected_identity,
                        label,
                        status_error,
                    )
                self._orphan_fds.append((fd, expected_identity, label))
            error = StoreUnavailableError(f"{label} descriptor status is unknown")
            error.__cause__ = status_error
            _attach_cleanup_capability(error, _CleanupCapability(self.close))
            return error
        if expected_identity is not None and _identity(metadata) != expected_identity:
            if not any(existing_fd == fd for existing_fd, _, _ in self._orphan_fds):
                if len(self._orphan_fds) >= _MAX_ORPHAN_FDS:
                    return self._dedicated_constructor_fd_error(
                        fd,
                        expected_identity,
                        label,
                        StoreUnavailableError(
                            f"{label} descriptor retry registry is full"
                        ),
                    )
                self._orphan_fds.append((fd, expected_identity, label))
            error = StoreUnavailableError(f"{label} descriptor was reused")
            _attach_cleanup_capability(error, _CleanupCapability(self.close))
            return error
        if expected_identity is None:
            expected_identity = _identity(metadata)
        if self._fresh_bootstrap:
            if label == "lifetime gate open":
                self._fresh_gate_fd_identity = expected_identity
            elif label == "writer marker open":
                self._fresh_marker_fd_identity = expected_identity
            elif label == "database open":
                self._fresh_database_fd_identity = expected_identity
        first_error: StoreError | None = None
        if unlock:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except _CLEANUP_EXCEPTION as unlock_error:
                first_error = StoreUnavailableError(f"{label} descriptor unlock failed")
                first_error.__cause__ = unlock_error
            try:
                metadata = os.fstat(fd)
            except _CLEANUP_EXCEPTION as status_error:
                if (
                    isinstance(status_error, OSError)
                    and status_error.errno == errno.EBADF
                ):
                    if first_error is None:
                        return None
                    _attach_cleanup_capability(
                        first_error,
                        _CleanupCapability(self.close),
                    )
                    return first_error
                try:
                    self._retain_failed_fd(fd, expected_identity, label)
                except _CLEANUP_EXCEPTION as handoff_error:
                    error = (
                        handoff_error
                        if isinstance(handoff_error, StoreError)
                        else StoreUnavailableError(
                            f"{label} descriptor status is unknown"
                        )
                    )
                else:
                    error = StoreUnavailableError(
                        f"{label} descriptor status is unknown"
                    )
                error.__cause__ = status_error
                if first_error is None:
                    first_error = error
                _attach_cleanup_capability(
                    first_error,
                    _CleanupCapability(self.close),
                )
                return first_error
            if _identity(metadata) != expected_identity:
                error = StoreUnavailableError(f"{label} descriptor was reused")
                if first_error is None:
                    first_error = error
                _attach_cleanup_capability(
                    first_error,
                    _CleanupCapability(self.close),
                )
                return first_error
        try:
            os.close(fd)
        except _CLEANUP_EXCEPTION as close_error:
            retained_before = len(self._orphan_fds)
            try:
                self._retain_failed_fd(fd, expected_identity, label)
            except _CLEANUP_EXCEPTION as handoff_error:
                if len(self._orphan_fds) == retained_before:
                    if len(self._orphan_fds) >= _MAX_ORPHAN_FDS:
                        if not (
                            isinstance(handoff_error, StoreError)
                            and _extract_cleanup_capability(handoff_error) is not None
                        ):
                            return self._dedicated_constructor_fd_error(
                                fd,
                                expected_identity,
                                label,
                                close_error,
                            )
                    else:
                        self._orphan_fds.append((fd, expected_identity, label))
                error = (
                    handoff_error
                    if isinstance(handoff_error, StoreError)
                    else StoreUnavailableError(
                        f"{label} descriptor cleanup is unavailable"
                    )
                )
            else:
                error = StoreUnavailableError(
                    f"{label} descriptor close status is unknown"
                )
            if first_error is None:
                first_error = error
            else:
                first_error.__cause__ = close_error
            _attach_cleanup_capability(error, _CleanupCapability(self.close))
            if first_error is error:
                return error
            _attach_cleanup_capability(first_error, _CleanupCapability(self.close))
            return first_error
        if first_error is not None:
            _attach_cleanup_capability(first_error, _CleanupCapability(self.close))
            return first_error
        return None

    def _retry_orphan_fds(self) -> None:
        """Drain retained preflight descriptors after an identity check."""

        remaining: list[_OrphanFD] = []
        first_error: BaseException | None = None
        for fd, expected_identity, label in self._orphan_fds:
            try:
                metadata = os.fstat(fd)
            except _CLEANUP_EXCEPTION as exc:
                if isinstance(exc, OSError) and exc.errno == errno.EBADF:
                    continue
                remaining.append((fd, expected_identity, label))
                if first_error is None:
                    first_error = StoreUnavailableError(
                        f"{label} descriptor status is unknown"
                    )
                continue
            if expected_identity is None:
                expected_identity = self._resolve_fresh_fd_identity(
                    fd,
                    _identity(metadata),
                    label,
                )
                if expected_identity is None:
                    remaining.append((fd, expected_identity, label))
                    if first_error is None:
                        first_error = StoreUnavailableError(
                            f"{label} descriptor identity is unavailable"
                        )
                    continue
            if _identity(metadata) != expected_identity:
                if first_error is None:
                    first_error = StoreUnavailableError(
                        f"{label} descriptor was reused"
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
                        first_error = StoreUnavailableError(
                            f"{label} descriptor status is unknown"
                        )
                    continue
                if _identity(retry_metadata) != expected_identity:
                    if first_error is None:
                        first_error = StoreUnavailableError(
                            f"{label} descriptor was reused"
                        )
                    continue
                remaining.append((fd, expected_identity, label))
                if first_error is None:
                    first_error = StoreUnavailableError(f"{label} cannot be closed")
                del close_error
        self._orphan_fds = remaining
        if first_error is not None:
            raise first_error

    def _retry_detached_fds(self) -> None:
        remaining: list[_FDRecoveryOwner] = []
        first_error: BaseException | None = None
        for owner in self._detached_fd_owners:
            try:
                owner.retry_cleanup()
            except _CLEANUP_EXCEPTION as exc:
                if owner._fd is not None:
                    remaining.append(owner)
                if first_error is None:
                    first_error = exc
            if owner._fd is not None and owner not in remaining:
                remaining.append(owner)
        self._detached_fd_owners = remaining
        if first_error is not None:
            raise first_error

    def _ensure_fresh_gate_parent_fd(self, gate_path: Path) -> None:
        if self._fresh_cleanup_gate_parent_fd is not None:
            return
        parent = gate_path.parent
        try:
            path_metadata = os.stat(parent, follow_symlinks=False)
            fd = os.open(parent, _open_flags(directory=True, writable=False))
            self._fresh_cleanup_gate_parent_fd = fd
            self._fresh_cleanup_gate_parent_identity = _identity(path_metadata)
            metadata = os.fstat(fd)
            self._fresh_cleanup_gate_parent_identity = _identity(metadata)
            _validate_directory_fd(fd, state_root=False)
            if _identity(path_metadata) != _identity(metadata):
                raise StoreUnavailableError(
                    "fresh gate parent changed while opening cleanup owner"
                )
        except StoreError:
            raise
        except _CLEANUP_EXCEPTION as exc:
            raise StoreUnavailableError(
                "fresh gate parent cannot be opened for cleanup"
            ) from exc

    def _sync_fresh_state_root(
        self, root_fd: int, expected_root: tuple[int, int]
    ) -> None:
        try:
            metadata = os.fstat(root_fd)
        except _CLEANUP_EXCEPTION as exc:
            raise StoreUnavailableError(
                "fresh state root cleanup status is unknown"
            ) from exc
        if _identity(metadata) != expected_root:
            raise StoreUnavailableError("fresh state root changed during cleanup")
        try:
            os.fsync(root_fd)
        except _CLEANUP_EXCEPTION as exc:
            raise StoreUnavailableError(
                "fresh state root cleanup durability is unknown"
            ) from exc

    def _sync_fresh_gate_parent(self, gate_path: Path) -> None:
        self._ensure_fresh_gate_parent_fd(gate_path)
        fd = self._fresh_cleanup_gate_parent_fd
        expected = self._fresh_cleanup_gate_parent_identity
        if fd is None or expected is None:
            raise StoreUnavailableError(
                "fresh gate parent cleanup owner is unavailable"
            )
        try:
            metadata = os.fstat(fd)
        except _CLEANUP_EXCEPTION as exc:
            if isinstance(exc, OSError) and exc.errno == errno.EBADF:
                self._fresh_cleanup_gate_parent_fd = None
                self._fresh_cleanup_gate_parent_identity = None
            raise StoreUnavailableError(
                "fresh gate parent cleanup status is unknown"
            ) from exc
        if _identity(metadata) != expected:
            self._fresh_cleanup_gate_parent_fd = None
            self._fresh_cleanup_gate_parent_identity = None
            raise StoreUnavailableError("fresh gate parent descriptor was reused")
        _validate_directory_fd(fd, state_root=False)
        try:
            os.fsync(fd)
        except _CLEANUP_EXCEPTION as exc:
            raise StoreUnavailableError(
                "fresh gate parent cleanup durability is unknown"
            ) from exc
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
                    self._fresh_cleanup_gate_parent_fd = None
                    self._fresh_cleanup_gate_parent_identity = None
                raise StoreUnavailableError(
                    "fresh gate parent cleanup close status is unknown"
                ) from exc
            if _identity(retry_metadata) != expected:
                self._fresh_cleanup_gate_parent_fd = None
                self._fresh_cleanup_gate_parent_identity = None
                raise StoreUnavailableError(
                    "fresh gate parent descriptor was reused"
                ) from exc
            raise StoreUnavailableError("fresh gate parent cannot be closed") from exc
        self._fresh_cleanup_gate_parent_fd = None
        self._fresh_cleanup_gate_parent_identity = None

    def _cleanup_fresh_bootstrap(self) -> None:
        """Remove identity-tracked fresh files and retain uncertain cleanup state."""

        connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except _CLEANUP_EXCEPTION as exc:
                raise StoreUnavailableError(
                    "fresh coordination connection cannot be closed"
                ) from exc
            self._connection = None
        root_fd = self._state_root_fd
        expected_root = self._state_root_identity
        if root_fd is None or expected_root is None:
            raise StoreUnavailableError("fresh state root cleanup owner is unavailable")
        try:
            root_metadata = os.fstat(root_fd)
        except OSError as exc:
            raise StoreUnavailableError(
                "fresh state root cleanup status is unknown"
            ) from exc
        if _identity(root_metadata) != expected_root:
            raise StoreUnavailableError("fresh state root changed during cleanup")

        first_error: BaseException | None = None

        def remember(error: BaseException | None) -> None:
            nonlocal first_error
            if error is not None and first_error is None:
                first_error = error

        def unlink_root_file(
            filename: str,
            expected: tuple[int, int] | None,
            *,
            sidecar: bool,
            set_missing: Callable[[], None],
        ) -> None:
            resolved, sync_needed, error = _unlink_identity_tracked(
                filename,
                expected,
                dir_fd=root_fd,
                sidecar=sidecar,
                label=filename,
            )
            if resolved:
                set_missing()
            if sync_needed:
                self._fresh_cleanup_sync_pending.add("state_root")
            remember(error)

        database_expected = (
            self._fresh_database_created_identity
            if self._fresh_database_created_identity is not None
            else self._fresh_database_fd_identity
        )

        def clear_database_tracker() -> None:
            self._fresh_database_created_identity = None
            self._fresh_database_fd_identity = None

        unlink_root_file(
            DATABASE_FILENAME,
            database_expected,
            sidecar=False,
            set_missing=clear_database_tracker,
        )
        marker_expected = (
            self._fresh_marker_created_identity
            if self._fresh_marker_created_identity is not None
            else self._fresh_marker_fd_identity
        )

        def clear_marker_tracker() -> None:
            self._fresh_marker_created_identity = None
            self._fresh_marker_fd_identity = None

        unlink_root_file(
            WRITER_MARKER_FILENAME,
            marker_expected,
            sidecar=True,
            set_missing=clear_marker_tracker,
        )
        for filename, expected in tuple(self._fresh_sidecar_created_identities.items()):
            resolved, sync_needed, error = _unlink_identity_tracked(
                filename,
                expected,
                dir_fd=root_fd,
                sidecar=True,
                label=filename,
            )
            if resolved:
                del self._fresh_sidecar_created_identities[filename]
            if sync_needed:
                self._fresh_cleanup_sync_pending.add("state_root")
            remember(error)

        gate_path = self.state_root.parent / LIFETIME_GATE_FILENAME
        gate_expected = (
            self._fresh_gate_created_identity
            if self._fresh_gate_created_identity is not None
            else self._fresh_gate_fd_identity
        )
        resolved, sync_needed, error = _unlink_identity_tracked(
            gate_path,
            gate_expected,
            dir_fd=None,
            sidecar=True,
            label=LIFETIME_GATE_FILENAME,
        )
        if resolved:
            self._fresh_gate_created_identity = None
            self._fresh_gate_fd_identity = None
        if sync_needed:
            self._fresh_cleanup_sync_pending.add("gate_parent")
        remember(error)

        if "state_root" in self._fresh_cleanup_sync_pending:
            try:
                self._sync_fresh_state_root(root_fd, expected_root)
            except _CLEANUP_EXCEPTION as exc:
                remember(exc)
            else:
                self._fresh_cleanup_sync_pending.discard("state_root")
        if "gate_parent" in self._fresh_cleanup_sync_pending:
            try:
                self._sync_fresh_gate_parent(gate_path)
            except _CLEANUP_EXCEPTION as exc:
                remember(exc)
            else:
                self._fresh_cleanup_sync_pending.discard("gate_parent")

        if first_error is not None:
            raise first_error
        self._fresh_cleanup_pending = bool(
            self._fresh_cleanup_sync_pending
            or self._fresh_cleanup_gate_parent_fd is not None
            or self._orphan_fds
            or self._detached_fd_owners
            or self._fresh_database_created_identity is not None
            or self._fresh_database_fd_identity is not None
            or self._fresh_marker_created_identity is not None
            or self._fresh_marker_fd_identity is not None
            or self._fresh_gate_created_identity is not None
            or self._fresh_gate_fd_identity is not None
            or self._fresh_sidecar_created_identities
        )

    def _initial_entry(self, filename: str) -> os.stat_result | None:
        root_fd = self._state_root_fd
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        try:
            return os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreUnavailableError(
                f"private SQLite entry {filename} cannot be inspected"
            ) from exc

    def _initial_root_inventory(self) -> frozenset[str]:
        root_fd = self._state_root_fd
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        try:
            names = frozenset(os.listdir(root_fd))
            for name in names:
                os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            return names
        except FileNotFoundError as exc:
            raise StoreUnavailableError(
                "state root entry disappeared during bootstrap inventory"
            ) from exc
        except OSError as exc:
            raise StoreUnavailableError(
                "state root cannot be inventoried before bootstrap"
            ) from exc

    def _validate_initial_writer_marker(self, before: os.stat_result) -> None:
        root_fd = self._state_root_fd
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        _validate_private_file(before, sidecar=True)
        marker_fd: int | None = None
        marker_identity = _identity(before)
        body_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            marker_fd = os.open(
                WRITER_MARKER_FILENAME,
                _open_flags(directory=False, writable=False)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root_fd,
            )
            metadata = os.fstat(marker_fd)
            after = os.stat(
                WRITER_MARKER_FILENAME,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            marker_identity = _identity(metadata)
            if (
                marker_identity != _identity(before)
                or _identity(after) != marker_identity
            ):
                raise StoreUnavailableError("writer marker changed during bootstrap")
            state = _read_writer_marker_state(marker_fd)
            if state != WRITER_MARKER_CLEAN_STATE:
                raise StoreUnavailableError("writer marker is not clean")
        except StoreError as exc:
            body_error = exc
        except OSError as exc:
            body_error = StoreUnavailableError("writer marker cannot be inspected")
            body_error.__cause__ = exc
        except _CLEANUP_EXCEPTION as exc:
            body_error = _store_error_from_exception(
                exc,
                "writer marker cannot be inspected",
            )
        finally:
            if marker_fd is not None:
                cleanup_error = self._handoff_constructor_fd(
                    marker_fd,
                    marker_identity,
                    "initial writer marker",
                )
        if body_error is not None:
            if cleanup_error is not None:
                capability = _CleanupCapability(self.close)
                cleanup_capability = _extract_cleanup_capability(cleanup_error)
                if cleanup_capability is not None:
                    capability = _CleanupCapability.compose(
                        cleanup_capability,
                        capability,
                    )
                _raise_with_cleanup_capability(
                    body_error,
                    capability,
                )
            raise body_error
        if cleanup_error is not None:
            raise cleanup_error

    def _open_lifetime_gate(self, *, create: bool) -> int:
        """Open the stable, never-unlinked gate beside the state root."""

        gate_path = self.state_root.parent / LIFETIME_GATE_FILENAME
        gate_fd: int | None = None
        gate_identity: tuple[int, int] | None = None
        body_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            try:
                before = os.stat(gate_path, follow_symlinks=False)
            except FileNotFoundError:
                before = None
            if before is None and not create:
                raise StoreUnavailableError("coordination lifetime gate is missing")
            if before is not None:
                gate_identity = _identity(before)
                _validate_private_file(before, sidecar=True)
            flags = _open_flags(directory=False, writable=True)
            created = before is None and create
            if created:
                flags |= os.O_CREAT
                flags |= os.O_EXCL
            try:
                gate_fd = os.open(
                    gate_path,
                    flags,
                    0o600,
                )
            except FileExistsError:
                if not created:
                    raise
                created = False
                gate_fd = os.open(
                    gate_path,
                    flags & ~os.O_EXCL,
                    0o600,
                )
            try:
                metadata = os.fstat(gate_fd)
                gate_identity = _identity(metadata)
                if created:
                    self._fresh_gate_fd_identity = gate_identity
                _validate_private_file(metadata, sidecar=True)
                after = os.stat(gate_path, follow_symlinks=False)
                if before is not None and _identity(before) != gate_identity:
                    raise StoreUnavailableError(
                        "coordination lifetime gate changed while opening"
                    )
                if _identity(after) != gate_identity:
                    raise StoreUnavailableError(
                        "coordination lifetime gate changed while opening"
                    )
                self._lifetime_gate_identity = gate_identity
                if created:
                    self._fresh_gate_created_identity = gate_identity
                    self._fresh_gate_fd_identity = None
                result_fd = gate_fd
                gate_fd = None
                return result_fd
            except StoreError as exc:
                body_error = exc
            except OSError as exc:
                body_error = StoreUnavailableError(
                    "coordination lifetime gate cannot be opened"
                )
                body_error.__cause__ = exc
            except _CLEANUP_EXCEPTION as exc:
                body_error = _store_error_from_exception(
                    exc,
                    "coordination lifetime gate cannot be opened",
                )
        except StoreError as exc:
            body_error = exc
        except OSError as exc:
            body_error = StoreUnavailableError(
                "coordination lifetime gate cannot be opened"
            )
            body_error.__cause__ = exc
        except _CLEANUP_EXCEPTION as exc:
            body_error = _store_error_from_exception(
                exc,
                "coordination lifetime gate cannot be opened",
            )
        finally:
            if gate_fd is not None:
                cleanup_error = self._handoff_constructor_fd(
                    gate_fd,
                    gate_identity,
                    "lifetime gate open",
                )
        if body_error is not None:
            if cleanup_error is not None:
                capability = _CleanupCapability(self.close)
                cleanup_capability = _extract_cleanup_capability(cleanup_error)
                if cleanup_capability is not None:
                    capability = _CleanupCapability.compose(
                        cleanup_capability,
                        capability,
                    )
                _raise_with_cleanup_capability(
                    body_error,
                    capability,
                )
            raise body_error
        if cleanup_error is not None:
            raise cleanup_error
        raise StoreUnavailableError("coordination lifetime gate cannot be opened")

    def _open_writer_marker(self) -> None:
        """Create once, then hold the canonical writer marker shared."""

        root_fd = self._state_root_fd
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        filename = WRITER_MARKER_FILENAME
        marker_fd: int | None = None
        marker_identity: tuple[int, int] | None = None
        body_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            try:
                before = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                before = None
            created = before is None
            if before is not None:
                _validate_private_file(before, sidecar=True)
                if self._fresh_bootstrap:
                    raise StoreUnavailableError(
                        "writer marker appeared during bootstrap"
                    )
            if before is None:
                if not self._marker_creation_allowed:
                    raise StoreUnavailableError(
                        "writer marker is missing from an initialized store"
                    )
                self._fault("before_marker_create")
                try:
                    marker_fd = os.open(
                        filename,
                        _open_flags(directory=False, writable=True)
                        | getattr(os, "O_NONBLOCK", 0)
                        | os.O_CREAT
                        | os.O_EXCL,
                        0o600,
                        dir_fd=root_fd,
                    )
                except FileExistsError as exc:
                    raise StoreUnavailableError(
                        "writer marker appeared while creating"
                    ) from exc
                created_metadata = os.fstat(marker_fd)
                marker_identity = _identity(created_metadata)
                self._fresh_marker_fd_identity = marker_identity
                _validate_private_file(created_metadata, sidecar=True)
                os.fchmod(marker_fd, 0o600)
                _write_writer_marker_state(marker_fd, WRITER_MARKER_CLEAN_STATE)
                self._fault("after_marker_create")
                try:
                    self._fault("before_marker_fsync")
                    os.fsync(marker_fd)
                    os.fsync(root_fd)
                    self._fault("after_marker_fsync")
                except OSError as exc:
                    raise StoreUnavailableError(
                        "writer marker directory cannot be synchronized"
                    ) from exc
            else:
                marker_fd = os.open(
                    filename,
                    _open_flags(directory=False, writable=True)
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=root_fd,
                )
            metadata = os.fstat(marker_fd)
            marker_identity = _identity(metadata)
            if created:
                self._fresh_marker_created_identity = marker_identity
            _validate_private_file(metadata, sidecar=True)
            after = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
            if before is not None and _identity(before) != marker_identity:
                raise StoreUnavailableError("writer marker changed while opening")
            if (
                self._initial_marker_identity is not None
                and marker_identity != self._initial_marker_identity
            ):
                raise StoreUnavailableError("writer marker changed while opening")
            if _identity(after) != marker_identity:
                raise StoreUnavailableError("writer marker changed while opening")
            if created:
                self._fresh_marker_created_identity = marker_identity
                self._fresh_marker_fd_identity = None
            self._fault("before_marker_lock")
            self._lock_lifetime_gate_fd(
                marker_fd,
                exclusive=False,
                busy_timeout_ms=self.busy_timeout_ms,
            )
            if _read_writer_marker_state(marker_fd) != WRITER_MARKER_CLEAN_STATE:
                raise StoreUnavailableError("writer marker is not clean")
            self._marker_fd = marker_fd
            self._marker_identity = marker_identity
            self._marker_shared = True
            self._marker_creation_allowed = False
            self._fresh_bootstrap = False
            marker_fd = None
            self._fault("after_marker_lock")
        except StoreError as exc:
            body_error = exc
        except OSError as exc:
            body_error = StoreUnavailableError("writer marker cannot be opened")
            body_error.__cause__ = exc
        except _CLEANUP_EXCEPTION as exc:
            body_error = _store_error_from_exception(
                exc,
                "writer marker cannot be opened",
            )
        finally:
            if marker_fd is not None:
                close_error = self._handoff_constructor_fd(
                    marker_fd,
                    marker_identity,
                    "writer marker open",
                    unlock=True,
                )
                if cleanup_error is None:
                    cleanup_error = close_error
        if body_error is not None:
            if cleanup_error is not None:
                capability = _CleanupCapability(self.close)
                cleanup_capability = _extract_cleanup_capability(cleanup_error)
                if cleanup_capability is not None:
                    capability = _CleanupCapability.compose(
                        cleanup_capability,
                        capability,
                    )
                _raise_with_cleanup_capability(
                    body_error,
                    capability,
                )
            raise body_error
        if cleanup_error is not None:
            raise cleanup_error

    def _assert_marker_identity(self) -> None:
        if self._marker_probe_failed:
            raise StoreUnavailableError("writer marker lock cannot be recovered")
        root_fd = self._state_root_fd
        marker_fd = self._marker_fd
        expected = self._marker_identity
        if root_fd is None or marker_fd is None or expected is None:
            raise StoreClosedError("coordination store is closed")
        try:
            fd_metadata = os.fstat(marker_fd)
            path_metadata = os.stat(
                WRITER_MARKER_FILENAME,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise StoreUnavailableError("writer marker is unavailable") from exc
        _validate_private_file(fd_metadata, sidecar=True)
        _validate_private_file(path_metadata, sidecar=True)
        if _identity(fd_metadata) != expected or _identity(path_metadata) != expected:
            raise StoreUnavailableError("writer marker changed while open")
        if _read_writer_marker_state(marker_fd) != WRITER_MARKER_CLEAN_STATE:
            raise StoreUnavailableError("writer marker records an incomplete cleanup")

    def _assert_lifetime_gate(self) -> None:
        gate_fd = self._lifetime_gate_fd
        expected = self._lifetime_gate_identity
        if gate_fd is None or expected is None:
            if not self._lifetime_gate_required:
                return
            raise StoreClosedError("coordination store is closed")
        gate_path = self.state_root.parent / LIFETIME_GATE_FILENAME
        try:
            fd_metadata = os.fstat(gate_fd)
            path_metadata = os.stat(gate_path, follow_symlinks=False)
        except OSError as exc:
            raise StoreUnavailableError(
                "coordination lifetime gate is unavailable"
            ) from exc
        _validate_private_file(fd_metadata, sidecar=True)
        _validate_private_file(path_metadata, sidecar=True)
        if _identity(fd_metadata) != expected or _identity(path_metadata) != expected:
            raise StoreUnavailableError("coordination lifetime gate changed while open")

    def _acquire_lifetime_gate(self, *, exclusive: bool) -> None:
        gate_fd = self._lifetime_gate_fd
        if gate_fd is None:
            raise StoreClosedError("coordination store is closed")
        if not exclusive and self._lifetime_gate_shared:
            return
        self._assert_lifetime_gate()
        had_shared = self._lifetime_gate_shared
        if exclusive and had_shared:
            try:
                fcntl.flock(gate_fd, fcntl.LOCK_UN)
            except OSError as exc:
                raise StoreUnavailableError(
                    "coordination lifetime gate cannot be upgraded"
                ) from exc
            self._lifetime_gate_shared = False
        try:
            self._lock_lifetime_gate_fd(
                gate_fd,
                exclusive=exclusive,
                busy_timeout_ms=self.busy_timeout_ms,
            )
        except BaseException:
            if had_shared:
                self._lock_lifetime_gate_fd(
                    gate_fd,
                    exclusive=False,
                    busy_timeout_ms=self.busy_timeout_ms,
                )
                self._lifetime_gate_shared = True
            raise
        self._lifetime_gate_shared = not exclusive

    @staticmethod
    def _lock_lifetime_gate_fd(
        gate_fd: int,
        *,
        exclusive: bool,
        busy_timeout_ms: int,
    ) -> None:
        deadline_ns = time.monotonic_ns() + busy_timeout_ms * 1_000_000
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(gate_fd, operation | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise StoreUnavailableError(
                        "coordination lifetime gate is unavailable"
                    ) from exc
                remaining_ns = deadline_ns - time.monotonic_ns()
                if remaining_ns <= 0:
                    raise StoreBusyError(
                        "coordination lifetime gate busy timeout expired"
                    ) from exc
                time.sleep(min(0.005, remaining_ns / 1_000_000_000))
            else:
                return

    def _downgrade_lifetime_gate(self) -> None:
        gate_fd = self._lifetime_gate_fd
        if gate_fd is None:
            raise StoreClosedError("coordination store is closed")
        try:
            fcntl.flock(gate_fd, fcntl.LOCK_SH)
        except OSError as exc:
            raise StoreUnavailableError(
                "coordination lifetime gate cannot be downgraded"
            ) from exc
        self._lifetime_gate_shared = True

    def _release_lifetime_gate(self) -> None:
        gate_fd = self._lifetime_gate_fd
        if gate_fd is None:
            raise StoreClosedError("coordination store is closed")
        if not self._lifetime_gate_shared:
            try:
                fcntl.flock(gate_fd, fcntl.LOCK_UN)
            except OSError as exc:
                raise StoreUnavailableError(
                    "coordination lifetime gate cannot be released"
                ) from exc
            return
        try:
            fcntl.flock(gate_fd, fcntl.LOCK_UN)
        except OSError as exc:
            raise StoreUnavailableError(
                "coordination lifetime gate cannot be released"
            ) from exc
        self._lifetime_gate_shared = False

    @contextmanager
    def _shared_lifetime_gate(self) -> Iterator[None]:
        """Guard one operation while cooperating replacement is excluded."""

        if self._lifetime_gate_cleanup_pending:
            raise StoreUnavailableError("coordination lifetime gate cleanup is pending")
        depth = getattr(self._lifetime_gate_local, "shared_depth", 0)
        if type(depth) is not int or depth < 0:
            raise StoreIntegrityError("coordination lifetime gate depth is invalid")
        if depth:
            self._assert_lifetime_gate()
            self._lifetime_gate_local.shared_depth = depth + 1
            try:
                yield
            finally:
                self._lifetime_gate_local.shared_depth = depth
            return
        owner = threading.get_ident()
        deadline_ns = time.monotonic_ns() + self.busy_timeout_ms * 1_000_000
        with self._lifetime_gate_condition:
            while (
                self._lifetime_gate_exclusive_owner is not None
                and self._lifetime_gate_exclusive_owner != owner
            ) or (
                self._marker_probe_owner is not None
                and self._marker_probe_owner != owner
            ):
                remaining_ns = deadline_ns - time.monotonic_ns()
                if remaining_ns <= 0:
                    raise StoreBusyError(
                        "coordination lifetime gate busy timeout expired"
                    )
                self._lifetime_gate_condition.wait(remaining_ns / 1_000_000_000)
            had_shared = self._lifetime_gate_shared
            if (
                self._lifetime_gate_shared_users == 0
                and self._lifetime_gate_exclusive_owner != owner
                and not had_shared
            ):
                self._acquire_lifetime_gate(exclusive=False)
            elif not had_shared and self._lifetime_gate_exclusive_owner != owner:
                raise StoreIntegrityError("coordination lifetime gate is not held")
            self._lifetime_gate_shared_users += 1
            self._lifetime_gate_local.shared_depth = 1
        body_error: BaseException | None = None
        try:
            self._assert_lifetime_gate()
            yield
            self._assert_lifetime_gate()
        except _CLEANUP_EXCEPTION as exc:
            body_error = exc

        cleanup_error: BaseException | None = None
        try:
            with self._lifetime_gate_condition:
                self._lifetime_gate_local.shared_depth = 0
                self._lifetime_gate_shared_users -= 1
                if self._lifetime_gate_shared_users < 0:
                    self._lifetime_gate_shared_users = 0
                    raise StoreIntegrityError(
                        "coordination lifetime gate depth is invalid"
                    )
                if (
                    self._lifetime_gate_shared_users == 0
                    and self._lifetime_gate_exclusive_owner is None
                    and not self._lifetime_gate_persistent
                ):
                    try:
                        self._release_lifetime_gate()
                    except _CLEANUP_EXCEPTION as exc:
                        cleanup_error = exc
                        self._lifetime_gate_cleanup_pending = True
        except _CLEANUP_EXCEPTION as exc:
            cleanup_error = exc
            self._lifetime_gate_cleanup_pending = True
        finally:
            with self._lifetime_gate_condition:
                self._lifetime_gate_condition.notify_all()

        if cleanup_error is not None:
            self._raise_gate_cleanup_failure(body_error, cleanup_error)
        if body_error is not None:
            raise body_error

    @contextmanager
    def _exclusive_lifetime_gate(self) -> Iterator[None]:
        """Reserve the gate for cooperating restore/replacement operations."""

        if self._lifetime_gate_cleanup_pending:
            raise StoreUnavailableError("coordination lifetime gate cleanup is pending")
        if self._marker_probe_failed:
            raise StoreUnavailableError("writer marker lock cannot be recovered")
        if getattr(self._lifetime_gate_local, "shared_depth", 0):
            raise StoreBusyError(
                "coordination lifetime gate is already shared by this operation"
            )
        owner = threading.get_ident()
        deadline_ns = time.monotonic_ns() + self.busy_timeout_ms * 1_000_000
        with self._lifetime_gate_condition:
            while self._lifetime_gate_shared_users or (
                self._lifetime_gate_exclusive_owner is not None
                and self._lifetime_gate_exclusive_owner != owner
            ):
                remaining_ns = deadline_ns - time.monotonic_ns()
                if remaining_ns <= 0:
                    raise StoreBusyError(
                        "coordination lifetime gate busy timeout expired"
                    )
                self._lifetime_gate_condition.wait(remaining_ns / 1_000_000_000)
            had_shared = self._lifetime_gate_shared
            self._lifetime_gate_exclusive_owner = owner
            try:
                self._acquire_lifetime_gate(exclusive=True)
            except BaseException:
                self._lifetime_gate_exclusive_owner = None
                self._lifetime_gate_condition.notify_all()
                raise
        body_error: BaseException | None = None
        try:
            self._assert_lifetime_gate()
            yield
            self._assert_lifetime_gate()
        except _CLEANUP_EXCEPTION as exc:
            body_error = exc

        cleanup_error: BaseException | None = None
        try:
            try:
                if had_shared:
                    self._downgrade_lifetime_gate()
                else:
                    self._release_lifetime_gate()
            except _CLEANUP_EXCEPTION as exc:
                cleanup_error = exc
                self._lifetime_gate_cleanup_pending = True
            if cleanup_error is None:
                with self._lifetime_gate_condition:
                    self._lifetime_gate_exclusive_owner = None
        except _CLEANUP_EXCEPTION as exc:
            cleanup_error = exc
            self._lifetime_gate_cleanup_pending = True
        finally:
            with self._lifetime_gate_condition:
                if cleanup_error is not None:
                    self._lifetime_gate_cleanup_pending = True
                self._lifetime_gate_condition.notify_all()

        if cleanup_error is not None:
            self._raise_gate_cleanup_failure(body_error, cleanup_error)
        if body_error is not None:
            raise body_error

    @contextmanager
    def _marker_exclusive_probe(self) -> Iterator[None]:
        """Temporarily release this store's marker for a doctor probe.

        The store keeps its lifetime shared guard while its own marker lock is
        released.  Local store operations are paused, while a peer process that
        still holds the shared marker makes the doctor's normal exclusive probe
        report ``WRITER_ACTIVE``.  The marker is reacquired before the lifetime
        guard is released.
        """

        if getattr(self._lifetime_gate_local, "shared_depth", 0):
            raise StoreBusyError(
                "writer marker probe cannot run inside a store operation"
            )
        owner = threading.get_ident()
        with self._shared_lifetime_gate():
            deadline_ns = time.monotonic_ns() + self.busy_timeout_ms * 1_000_000
            with self._lifetime_gate_condition:
                while self._marker_probe_owner not in {None, owner}:
                    remaining_ns = deadline_ns - time.monotonic_ns()
                    if remaining_ns <= 0:
                        raise StoreBusyError("writer marker probe is busy")
                    self._lifetime_gate_condition.wait(remaining_ns / 1_000_000_000)
                self._marker_probe_owner = owner
                while self._lifetime_gate_shared_users > 1:
                    remaining_ns = deadline_ns - time.monotonic_ns()
                    if remaining_ns <= 0:
                        self._marker_probe_owner = None
                        self._lifetime_gate_condition.notify_all()
                        raise StoreBusyError("writer marker probe has active users")
                    self._lifetime_gate_condition.wait(remaining_ns / 1_000_000_000)
            marker_fd = self._marker_fd
            if marker_fd is None or not self._marker_shared:
                with self._lifetime_gate_condition:
                    self._marker_probe_owner = None
                    self._lifetime_gate_condition.notify_all()
                raise StoreUnavailableError("writer marker is not held")
            try:
                self._assert_marker_identity()
            except BaseException:
                with self._lifetime_gate_condition:
                    self._marker_probe_owner = None
                    self._lifetime_gate_condition.notify_all()
                raise
            try:
                fcntl.flock(marker_fd, fcntl.LOCK_UN)
                self._marker_shared = False
                yield
            finally:
                try:
                    self._lock_lifetime_gate_fd(
                        marker_fd,
                        exclusive=False,
                        busy_timeout_ms=self.busy_timeout_ms,
                    )
                    self._marker_shared = True
                    self._assert_marker_identity()
                except BaseException:
                    self._marker_probe_failed = True
                    if self._marker_shared:
                        try:
                            fcntl.flock(marker_fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
                    self._marker_shared = False
                    raise
                finally:
                    with self._lifetime_gate_condition:
                        self._marker_probe_owner = None
                        self._lifetime_gate_condition.notify_all()

    @classmethod
    @contextmanager
    def _exclusive_lifetime_gate_for_root(
        cls,
        state_root: Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> Iterator[None]:
        """Lock an existing gate for restore without opening the SQLite DB."""

        if (
            type(busy_timeout_ms) is not int
            or not 1 <= busy_timeout_ms <= MAX_BUSY_TIMEOUT_MS
        ):
            raise ValueError(
                f"busy_timeout_ms must be between 1 and {MAX_BUSY_TIMEOUT_MS}"
            )
        root = _coerce_state_root(state_root)
        gate_path = root.parent / LIFETIME_GATE_FILENAME
        gate_fd: int | None = None
        owner: _LifetimeGateCleanupOwner | None = None
        body_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            path_metadata = os.stat(gate_path, follow_symlinks=False)
            _validate_private_file(path_metadata, sidecar=True)
            expected_identity = _identity(path_metadata)
            gate_fd = os.open(
                gate_path,
                _open_flags(directory=False, writable=True),
            )
            owner = _LifetimeGateCleanupOwner(
                gate_fd,
                expected_identity,
                locked=False,
            )
            metadata = os.fstat(gate_fd)
            _validate_private_file(metadata, sidecar=True)
            identity = _identity(metadata)
            owner.bind_identity(identity)
            if identity != expected_identity:
                raise StoreUnavailableError(
                    "coordination lifetime gate changed while opening"
                )
            cls._lock_lifetime_gate_fd(
                gate_fd,
                exclusive=True,
                busy_timeout_ms=busy_timeout_ms,
            )
            owner.mark_locked()
            path_metadata = os.stat(gate_path, follow_symlinks=False)
            _validate_private_file(path_metadata, sidecar=True)
            if _identity(path_metadata) != identity:
                raise StoreUnavailableError(
                    "coordination lifetime gate changed while locking"
                )
            try:
                yield
                path_metadata = os.stat(gate_path, follow_symlinks=False)
                _validate_private_file(path_metadata, sidecar=True)
                if _identity(path_metadata) != identity:
                    raise StoreUnavailableError(
                        "coordination lifetime gate changed while held"
                    )
            except _CLEANUP_EXCEPTION as exc:
                body_error = exc
        except StoreError as exc:
            body_error = exc
        except OSError as exc:
            body_error = StoreUnavailableError(
                "coordination lifetime gate cannot be locked"
            )
            body_error.__cause__ = exc
        except _CLEANUP_EXCEPTION as exc:
            body_error = exc
        finally:
            if owner is not None:
                try:
                    owner.retry_cleanup()
                except _CLEANUP_EXCEPTION as exc:
                    cleanup_error = exc

        if body_error is not None:
            if owner is not None:
                attached = _attach_cleanup_capability(
                    body_error,
                    _CleanupCapability(owner.retry_cleanup),
                )
                if attached is not body_error:
                    raise attached from body_error
            raise body_error
        if cleanup_error is not None and owner is not None:
            _attach_cleanup_capability(
                cleanup_error,
                _CleanupCapability(owner.retry_cleanup),
            )
            raise cleanup_error

    def close(self) -> None:
        if self._fresh_cleanup_pending and self._state_root_fd is not None:
            raise StoreUnavailableError("fresh coordination cleanup is pending")
        first_error: BaseException | None = None

        def attempt(action: Callable[[], None]) -> None:
            nonlocal first_error
            try:
                action()
            except _CLEANUP_EXCEPTION as exc:
                if first_error is None:
                    first_error = exc

        attempt(self._retry_detached_fds)
        attempt(self._retry_orphan_fds)

        connection = self._connection
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
                    self._connection = None
                    self._connection_cleanup_pending = False
            else:
                self._connection = None
                self._connection_cleanup_pending = False

        def close_fd(
            attr_name: str,
            identity_attr_name: str,
            fd: int | None,
            *,
            unlock: bool,
            lock_state_attr_name: str | None = None,
        ) -> None:
            nonlocal first_error
            if fd is None:
                return

            def clear() -> None:
                setattr(self, attr_name, None)
                setattr(self, identity_attr_name, None)
                if lock_state_attr_name is not None:
                    setattr(self, lock_state_attr_name, False)
                if attr_name == "_lifetime_gate_fd":
                    self._lifetime_gate_required = False
                    self._lifetime_gate_cleanup_pending = False
                    with self._lifetime_gate_condition:
                        self._lifetime_gate_exclusive_owner = None
                        self._lifetime_gate_condition.notify_all()

            def mark_pending() -> None:
                if attr_name == "_lifetime_gate_fd":
                    self._lifetime_gate_cleanup_pending = True

            try:
                metadata = os.fstat(fd)
            except _CLEANUP_EXCEPTION as exc:
                if isinstance(exc, OSError) and exc.errno == errno.EBADF:
                    clear()
                else:
                    if first_error is None:
                        first_error = exc
                    mark_pending()
                return
            expected_identity = getattr(self, identity_attr_name)
            if expected_identity is None:
                path: str | Path | None = None
                path_dir_fd: int | None = None
                if attr_name == "_state_root_fd":
                    path = self.state_root
                elif attr_name == "_lifetime_gate_fd":
                    path = self.state_root.parent / LIFETIME_GATE_FILENAME
                elif attr_name == "_database_fd":
                    path = DATABASE_FILENAME
                    path_dir_fd = self._state_root_fd
                elif attr_name == "_marker_fd":
                    path = WRITER_MARKER_FILENAME
                    path_dir_fd = self._state_root_fd
                if path is not None:
                    try:
                        path_metadata = os.stat(
                            path,
                            dir_fd=path_dir_fd,
                            follow_symlinks=False,
                        )
                    except _CLEANUP_EXCEPTION as exc:
                        if first_error is None:
                            first_error = StoreUnavailableError(
                                f"{attr_name} descriptor identity is unavailable"
                            )
                            first_error.__cause__ = exc
                        mark_pending()
                        return
                    if _identity(path_metadata) == _identity(metadata):
                        expected_identity = _identity(metadata)
                        setattr(self, identity_attr_name, expected_identity)
                    else:
                        if first_error is None:
                            first_error = StoreUnavailableError(
                                f"{attr_name} descriptor identity is unavailable"
                            )
                        mark_pending()
                        return
                if expected_identity is None:
                    if first_error is None:
                        first_error = StoreUnavailableError(
                            f"{attr_name} descriptor identity is unavailable"
                        )
                    mark_pending()
                    return
            if _identity(metadata) != expected_identity:
                clear()
                if first_error is None:
                    first_error = StoreUnavailableError(
                        f"{attr_name} descriptor was reused"
                    )
                return
            unlock_error: BaseException | None = None
            if unlock:
                lock_state = (
                    bool(getattr(self, lock_state_attr_name))
                    if lock_state_attr_name is not None
                    else False
                )
                if lock_state:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except _CLEANUP_EXCEPTION as exc:
                        unlock_error = exc
                    try:
                        unlocked_metadata = os.fstat(fd)
                    except _CLEANUP_EXCEPTION as status_error:
                        clear_if_gone = (
                            isinstance(status_error, OSError)
                            and status_error.errno == errno.EBADF
                        )
                        if clear_if_gone:
                            clear()
                            if unlock_error is None:
                                return
                            if first_error is None:
                                first_error = unlock_error
                            return
                        if first_error is None:
                            first_error = status_error
                        mark_pending()
                        return
                    if _identity(unlocked_metadata) != expected_identity:
                        clear()
                        if first_error is None:
                            first_error = StoreUnavailableError(
                                f"{attr_name} descriptor was reused"
                            )
                        return
                    if unlock_error is None:
                        setattr(self, cast(str, lock_state_attr_name), False)
            close_error: BaseException | None = None
            try:
                os.close(fd)
            except _CLEANUP_EXCEPTION as exc:
                close_error = exc
                try:
                    retry_metadata = os.fstat(fd)
                except _CLEANUP_EXCEPTION as status_error:
                    if (
                        isinstance(status_error, OSError)
                        and status_error.errno == errno.EBADF
                    ):
                        clear()
                    else:
                        if first_error is None:
                            first_error = status_error
                        mark_pending()
                else:
                    if _identity(retry_metadata) != expected_identity:
                        clear()
                        if first_error is None:
                            first_error = StoreUnavailableError(
                                f"{attr_name} descriptor was reused"
                            )
            else:
                clear()
            if unlock_error is not None and first_error is None:
                first_error = unlock_error
                mark_pending()
            if close_error is not None and first_error is None:
                first_error = close_error
                mark_pending()

        close_fd(
            "_marker_fd",
            "_marker_identity",
            self._marker_fd,
            unlock=True,
            lock_state_attr_name="_marker_shared",
        )
        close_fd(
            "_lifetime_gate_fd",
            "_lifetime_gate_identity",
            self._lifetime_gate_fd,
            unlock=True,
            lock_state_attr_name="_lifetime_gate_shared",
        )
        close_fd(
            "_database_fd",
            "_database_identity",
            self._database_fd,
            unlock=False,
        )
        close_fd(
            "_state_root_fd",
            "_state_root_identity",
            self._state_root_fd,
            unlock=True,
            lock_state_attr_name="_startup_lock_held",
        )
        if self._lifetime_gate_fd is None and not self._lifetime_gate_cleanup_pending:
            self._lifetime_gate_required = False
            self._lifetime_gate_cleanup_pending = False
        if self._lifetime_gate_cleanup_pending and first_error is None:
            first_error = StoreUnavailableError(
                "coordination lifetime gate cleanup is pending"
            )
        if first_error is not None:
            error = StoreUnavailableError("coordination store close failed")
            _attach_cleanup_capability(error, _CleanupCapability(self.close))
            raise error from first_error

    def _acquire_startup_lock(self) -> None:
        root_fd = self._state_root_fd
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        deadline_ns = time.monotonic_ns() + self.busy_timeout_ms * 1_000_000
        while True:
            try:
                fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise StoreUnavailableError(
                        "private SQLite startup lock is unavailable"
                    ) from exc
                remaining_ns = deadline_ns - time.monotonic_ns()
                if remaining_ns <= 0:
                    raise StoreBusyError(
                        "SQLite busy timeout expired while acquiring startup lock"
                    ) from exc
                time.sleep(min(0.005, remaining_ns / 1_000_000_000))
            else:
                self._startup_lock_held = True
                return

    def _release_startup_lock(self) -> None:
        root_fd = self._state_root_fd
        if not self._startup_lock_held or root_fd is None:
            return
        try:
            fcntl.flock(root_fd, fcntl.LOCK_UN)
        except OSError as exc:
            raise StoreUnavailableError(
                "private SQLite startup lock cannot be released"
            ) from exc
        self._startup_lock_held = False

    def create_intent(
        self,
        operation_id: str,
        *,
        effect_key: str,
        provider_id: str | None = None,
        actor: str,
        reason_code: str = "intent_created",
        evidence_ref: str | None = None,
        clock_ns: int | None = None,
    ) -> OperationSnapshot:
        """Recheck recovery history while the shared lifetime gate is held."""

        operation_id = _require_opaque_identifier(operation_id, "operation_id")
        effect_key = _require_opaque_identifier(effect_key, "effect_key")
        if provider_id is not None:
            provider_id = _require_opaque_identifier(provider_id, "provider_id")
        actor = _require_opaque_identifier(actor, "actor")
        reason_code = _require_reason_code(reason_code)
        if reason_code != "intent_created":
            raise ValueError("reason_code is unsupported for intent")
        if evidence_ref is not None:
            evidence_ref = _require_evidence_ref(evidence_ref)
        with self._shared_lifetime_gate():
            state = self._run_normal_open_preflight()
            self._normal_open_state = state
            self._committed_tombstones = _normal_open_state_keys(state)
            self._verify_normal_open_history(state)
            if any(
                operation_id == tombstone_operation_id
                or effect_key == tombstone_effect_key
                for tombstone_operation_id, tombstone_effect_key in self._committed_tombstones
            ):
                raise DuplicateOperationError(
                    "operation or effect identity is tombstoned"
                )
            return self._create_intent_locked(
                operation_id,
                effect_key=effect_key,
                provider_id=provider_id,
                actor=actor,
                reason_code=reason_code,
                evidence_ref=evidence_ref,
                clock_ns=clock_ns,
            )

    def _create_intent_locked(
        self,
        operation_id: str,
        *,
        effect_key: str,
        provider_id: str | None = None,
        actor: str,
        reason_code: str = "intent_created",
        evidence_ref: str | None = None,
        clock_ns: int | None = None,
    ) -> OperationSnapshot:
        """Atomically persist an operation intent and its first event."""

        operation_id = _require_opaque_identifier(operation_id, "operation_id")
        effect_key = _require_opaque_identifier(effect_key, "effect_key")
        if provider_id is not None:
            provider_id = _require_opaque_identifier(provider_id, "provider_id")
        actor = _require_opaque_identifier(actor, "actor")
        reason_code = _require_reason_code(reason_code)
        if reason_code != "intent_created":
            raise ValueError("reason_code is unsupported for intent")
        if evidence_ref is not None:
            evidence_ref = _require_evidence_ref(evidence_ref)
        timestamp = self._timestamp(clock_ns)
        try:
            with self._write_transaction() as connection:
                timestamp = self._record_clock(
                    connection,
                    timestamp,
                    strict=clock_ns is not None or self._clock_injected,
                )
                recovery_epoch = self._metadata_integer(
                    connection,
                    "recovery_epoch",
                    "recovery_epoch",
                )
                self._fault("before_intent_insert")
                connection.execute(
                    """
                    INSERT INTO operations(
                        operation_id, effect_key, status, current_attempt,
                        provider_id, recovery_epoch, created_ns, updated_ns
                    ) VALUES (?, ?, 'INTENT', 0, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        effect_key,
                        provider_id,
                        recovery_epoch,
                        timestamp,
                        timestamp,
                    ),
                )
                self._fault("before_attempt_insert")
                connection.execute(
                    """
                    INSERT INTO operation_attempts(
                        operation_id, attempt, lease_epoch, fencing_token
                    ) VALUES (?, 0, 0, 0)
                    """,
                    (operation_id,),
                )
                self._fault("before_event_insert")
                connection.execute(
                    """
                    INSERT INTO transition_events(
                        event_schema_version, operation_id, attempt, from_status,
                        to_status, kind, actor, clock_ns, reason_code, evidence_ref
                    ) VALUES (?, ?, 0, NULL, 'INTENT', 'intent', ?, ?, ?, ?)
                    """,
                    (
                        EVENT_SCHEMA_VERSION,
                        operation_id,
                        actor,
                        timestamp,
                        reason_code,
                        evidence_ref,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT operation_id, effect_key, status, current_attempt,
                           provider_id, recovery_epoch, created_ns, updated_ns
                    FROM operations
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise StoreIntegrityError("SQLite intent snapshot is unavailable")
                snapshot = self._operation_from_row(row)
        except DuplicateOperationError:
            raise
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc):
                error = DuplicateOperationError(
                    "operation or effect identity already exists"
                )
                _adopt_cleanup_capability(error, exc)
                raise error from exc
            _raise_sqlite_write_error(exc)
        except sqlite3.DatabaseError as exc:
            _raise_sqlite_write_error(exc)
        except (OverflowError, TypeError) as exc:
            mapped_error = StoreError("SQLite intent transaction failed")
            _adopt_cleanup_capability(mapped_error, exc)
            raise mapped_error from exc
        return snapshot

    def claim(
        self,
        operation_id: str,
        *,
        owner: str,
        provider_id: str,
        lease_ttl_ns: int | None = None,
        effect_key: str | None = None,
        now_ns: int | None = None,
    ) -> Claim:
        """Reserve the first lease and durably enter ``FENCE_PENDING``."""

        operation_id = _require_opaque_identifier(operation_id, "operation_id")
        owner = _require_opaque_identifier(owner, "owner")
        provider_id = _require_opaque_identifier(provider_id, "provider_id")
        if effect_key is not None:
            effect_key = _require_opaque_identifier(effect_key, "effect_key")
        ttl = self._lease_ttl(lease_ttl_ns)
        timestamp = self._timestamp(now_ns)
        with self._lease_write_transaction() as connection:
            timestamp = self._record_clock(
                connection,
                timestamp,
                strict=now_ns is not None or self._clock_injected,
            )
            expires = self._lease_expiry(timestamp, ttl)
            row = connection.execute(
                """
                    SELECT operation_id, effect_key, provider_id, status,
                           current_attempt, recovery_epoch
                    FROM operations WHERE operation_id = ?
                    """,
                (operation_id,),
            ).fetchone()
            if row is None:
                raise LeaseConflictError("operation is unavailable for claim")
            self._require_current_epoch(connection, row["recovery_epoch"])
            if effect_key is not None and row["effect_key"] != effect_key:
                raise LeaseConflictError("operation effect identity mismatches")
            if row["status"] != "INTENT" or row["current_attempt"] != 0:
                raise LeaseConflictError("operation is not claimable")
            operation_provider = row["provider_id"]
            if operation_provider is not None and operation_provider != provider_id:
                raise LeaseConflictError("operation provider identity mismatches")
            attempt_row = connection.execute(
                "SELECT MAX(attempt) AS maximum FROM operation_attempts "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            maximum_attempt = (
                0
                if attempt_row is None or attempt_row["maximum"] is None
                else _require_sqlite_integer(attempt_row["maximum"], "attempt")
            )
            if maximum_attempt >= SQLITE_INTEGER_MAX:
                raise ValueError("attempt exceeds supported integer")
            next_attempt = maximum_attempt + 1
            fencing_token = self._next_value(connection)
            connection.execute(
                """
                UPDATE operations
                SET provider_id = ?, status = 'FENCE_PENDING',
                        current_attempt = ?, updated_ns = ?
                WHERE operation_id = ? AND status = 'INTENT'
                      AND current_attempt = 0
                """,
                (provider_id, next_attempt, timestamp, operation_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("operation claim was lost")
            connection.execute(
                """
                    INSERT INTO operation_attempts(
                        operation_id, attempt, owner, provider_id,
                        lease_epoch, fencing_token, lease_heartbeat_ns,
                        lease_expires_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    operation_id,
                    next_attempt,
                    owner,
                    provider_id,
                    row["recovery_epoch"],
                    fencing_token,
                    timestamp,
                    expires,
                ),
            )
            self._append_event(
                connection,
                operation_id=operation_id,
                attempt=next_attempt,
                from_status="INTENT",
                to_status="FENCE_PENDING",
                kind="claim",
                actor=owner,
                timestamp=timestamp,
                reason_code="lease_claimed",
            )
            claim_row = self._fetch_attempt(connection, operation_id)
            if claim_row is None:
                raise StoreIntegrityError("SQLite claim is unavailable")
            return self._claim_from_row(claim_row)

    def heartbeat(
        self,
        claim: Claim,
        *,
        lease_ttl_ns: int | None = None,
        now_ns: int | None = None,
    ) -> Claim:
        """Extend only a live, fully activated claim with exact identity."""

        if not isinstance(claim, Claim):
            raise LeaseConflictError("lease claim has an unsupported type")
        if claim.phase != "CLAIMED":
            raise LeaseConflictError("only an activated claim can heartbeat")
        ttl = self._lease_ttl(lease_ttl_ns)
        timestamp = self._timestamp(now_ns)
        with self._lease_write_transaction() as connection:
            timestamp = self._record_clock(
                connection,
                timestamp,
                strict=now_ns is not None or self._clock_injected,
            )
            expires = self._lease_expiry(timestamp, ttl)
            row = self._fetch_attempt(connection, claim.operation_id)
            if row is None:
                raise LeaseConflictError("lease claim is unavailable")
            self._require_uninvalidated_lease(row)
            self._require_current_epoch(connection, row["recovery_epoch"])
            self._check_claim_identity(
                row,
                claim,
                expected_status="CLAIMED",
            )
            current_expires = row["lease_expires_ns"]
            if type(current_expires) is not int or timestamp >= current_expires:
                raise LeaseConflictError("lease has expired")
            connection.execute(
                """
                    UPDATE operation_attempts
                    SET lease_heartbeat_ns = ?, lease_expires_ns = ?
                    WHERE operation_id = ? AND attempt = ?
                      AND owner = ? AND provider_id = ?
                      AND lease_epoch = ? AND fencing_token = ?
                      AND lease_expires_ns > ?
                    """,
                (
                    timestamp,
                    expires,
                    claim.operation_id,
                    claim.attempt,
                    claim.owner,
                    claim.provider_id,
                    claim.lease_epoch,
                    claim.fencing_token,
                    timestamp,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("lease heartbeat was lost")
            connection.execute(
                "UPDATE operations SET updated_ns = ? WHERE operation_id = ?",
                (timestamp, claim.operation_id),
            )
            self._append_event(
                connection,
                operation_id=claim.operation_id,
                attempt=claim.attempt,
                from_status="CLAIMED",
                to_status="CLAIMED",
                kind="heartbeat",
                actor=claim.owner,
                timestamp=timestamp,
                reason_code="lease_heartbeat",
            )
            refreshed = self._fetch_attempt(connection, claim.operation_id)
            if refreshed is None:
                raise StoreIntegrityError("SQLite heartbeat is unavailable")
            return self._claim_from_row(refreshed)

    def reclaim(
        self,
        claim: Claim | str,
        *,
        owner: str | None = None,
        provider_id: str | None = None,
        effect_key: str | None = None,
        lease_ttl_ns: int | None = None,
        now_ns: int | None = None,
    ) -> Claim:
        """Replace an expired claim with a higher attempt and fence token."""

        if owner is None:
            raise ValueError("owner is required for reclaim")
        replacement_owner = _require_opaque_identifier(owner, "owner")
        if provider_id is not None:
            provider_id = _require_opaque_identifier(provider_id, "provider_id")
        if effect_key is not None:
            effect_key = _require_opaque_identifier(effect_key, "effect_key")
        ttl = self._lease_ttl(lease_ttl_ns)
        timestamp = self._timestamp(now_ns)
        if isinstance(claim, Claim):
            old_claim = claim
            old_status = old_claim.phase
            operation_id = old_claim.operation_id
            if provider_id is None:
                provider_id = old_claim.provider_id
            if effect_key is not None and effect_key != old_claim.effect_key:
                raise LeaseConflictError("operation effect identity mismatches")
        elif type(claim) is str:
            old_claim = None
            old_status = None
            operation_id = _require_opaque_identifier(claim, "operation_id")
            if provider_id is None:
                raise ValueError("provider_id is required for reclaim")
            if effect_key is None:
                raise ValueError("effect_key is required for reclaim")
        else:
            raise LeaseConflictError("lease claim has an unsupported type")
        with self._lease_write_transaction() as connection:
            timestamp = self._record_clock(
                connection,
                timestamp,
                strict=now_ns is not None or self._clock_injected,
            )
            row = self._fetch_attempt(connection, operation_id)
            if row is None:
                raise LeaseConflictError("lease claim is unavailable")
            self._require_uninvalidated_lease(row)
            self._require_current_epoch(connection, row["recovery_epoch"])
            if old_claim is not None:
                if old_status is None:
                    raise StoreIntegrityError("reclaim status is unavailable")
                self._check_claim_identity(
                    row,
                    old_claim,
                    expected_status=old_status,
                )
                old_status = row["status"]
            elif row["status"] not in {"FENCE_PENDING", "CLAIMED"}:
                raise LeaseConflictError("operation is not reclaimable")
            if old_status is None:
                old_status = row["status"]
            if effect_key is not None and row["effect_key"] != effect_key:
                raise LeaseConflictError("operation effect identity mismatches")
            current_provider = row["attempt_provider_id"]
            if current_provider is None or current_provider != provider_id:
                raise LeaseConflictError("operation provider identity mismatches")
            current_expires = row["lease_expires_ns"]
            if type(current_expires) is not int:
                raise StoreIntegrityError("SQLite lease expiry is invalid")
            if timestamp < current_expires:
                raise LeaseConflictError("lease has not expired")
            current_attempt = _require_sqlite_integer(
                row["current_attempt"], "attempt", minimum=1
            )
            if current_attempt >= SQLITE_INTEGER_MAX:
                raise ValueError("attempt exceeds supported integer")
            next_attempt = current_attempt + 1
            fencing_token = self._next_value(connection)
            expires = self._lease_expiry(timestamp, ttl)
            connection.execute(
                """
                    UPDATE operations
                    SET status = 'FENCE_PENDING', current_attempt = ?,
                        updated_ns = ?
                    WHERE operation_id = ? AND status = ?
                      AND current_attempt = ?
                    """,
                (next_attempt, timestamp, operation_id, old_status, current_attempt),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("lease reclaim was lost")
            connection.execute(
                """
                    INSERT INTO operation_attempts(
                        operation_id, attempt, owner, provider_id,
                        lease_epoch, fencing_token, lease_heartbeat_ns,
                        lease_expires_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    operation_id,
                    next_attempt,
                    replacement_owner,
                    provider_id,
                    row["recovery_epoch"],
                    fencing_token,
                    timestamp,
                    expires,
                ),
            )
            self._append_event(
                connection,
                operation_id=operation_id,
                attempt=next_attempt,
                from_status=old_status,
                to_status="FENCE_PENDING",
                kind="reclaim",
                actor=replacement_owner,
                timestamp=timestamp,
                reason_code="lease_reclaimed",
            )
            replacement = self._fetch_attempt(connection, operation_id)
            if replacement is None:
                raise StoreIntegrityError("SQLite reclaimed lease is unavailable")
            return self._claim_from_row(replacement)

    @staticmethod
    def _provider_effect_for_claim(claim: Claim) -> ProviderEffect:
        try:
            return _issue_provider_effect(
                operation_id=claim.operation_id,
                effect_key=claim.effect_key,
                provider_id=claim.provider_id,
                owner=claim.owner,
                attempt=claim.attempt,
                lease_epoch=claim.lease_epoch,
                fencing_token=claim.fencing_token,
            )
        except (TypeError, ValueError, ProviderProofError, ProviderReceiptError) as exc:
            raise ProviderProofError("provider effect identity is invalid") from exc

    @staticmethod
    def _provider_effect_with_proof(
        claim: Claim, proof: ProviderFenceProof
    ) -> ProviderEffect:
        if not isinstance(proof, ProviderFenceProof):
            raise ProviderProofError("provider fence proof has an unsupported type")
        try:
            return _issue_provider_effect(
                operation_id=claim.operation_id,
                effect_key=claim.effect_key,
                provider_id=claim.provider_id,
                owner=claim.owner,
                attempt=claim.attempt,
                lease_epoch=claim.lease_epoch,
                fencing_token=claim.fencing_token,
                fence_proof=proof,
            )
        except (TypeError, ValueError, ProviderReceiptError) as exc:
            raise ProviderProofError(
                "provider fence proof does not match claim"
            ) from exc

    def _begin_fence_reservation(
        self,
        claim: Claim,
        *,
        now_ns: int | None = None,
    ) -> Claim:
        """Durably mark a reservation before entering untrusted provider code."""

        if not isinstance(claim, Claim) or claim.phase != "FENCE_PENDING":
            raise LeaseConflictError("only a pending claim can start a fence")
        timestamp = self._timestamp(now_ns)
        with self._lease_write_transaction() as connection:
            timestamp = self._record_clock(
                connection,
                timestamp,
                strict=now_ns is not None or self._clock_injected,
            )
            row = self._fetch_attempt(connection, claim.operation_id)
            if row is None:
                raise LeaseConflictError("lease claim is unavailable")
            self._require_uninvalidated_lease(row)
            self._require_current_epoch(connection, row["recovery_epoch"])
            self._check_claim_identity(
                row,
                claim,
                expected_status="FENCE_PENDING",
            )
            current_expires = row["lease_expires_ns"]
            if type(current_expires) is not int:
                raise StoreIntegrityError("SQLite lease expiry is invalid")
            if timestamp >= current_expires:
                raise LeaseConflictError("pending lease has expired")
            connection.execute(
                """
                UPDATE operation_attempts
                SET fence_started_ns = ?
                WHERE operation_id = ? AND attempt = ?
                  AND owner = ? AND provider_id = ?
                  AND lease_epoch = ? AND fencing_token = ?
                  AND lease_expires_ns > ? AND fence_started_ns IS NULL
                """,
                (
                    timestamp,
                    claim.operation_id,
                    claim.attempt,
                    claim.owner,
                    claim.provider_id,
                    claim.lease_epoch,
                    claim.fencing_token,
                    timestamp,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("fence reservation start was lost")
            connection.execute(
                """
                UPDATE operations
                SET status = 'FENCE_RESERVATION_STARTED', updated_ns = ?
                WHERE operation_id = ? AND status = 'FENCE_PENDING'
                  AND current_attempt = ?
                """,
                (timestamp, claim.operation_id, claim.attempt),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("fence reservation start was lost")
            self._append_event(
                connection,
                operation_id=claim.operation_id,
                attempt=claim.attempt,
                from_status="FENCE_PENDING",
                to_status="FENCE_RESERVATION_STARTED",
                kind="fence",
                actor=claim.owner,
                timestamp=timestamp,
                reason_code="fence_reservation_started",
            )
        return claim

    def reserve_fence(self, claim: Claim, provider: ProviderPort) -> Claim:
        """Reserve a provider fence outside SQLite, then durably activate it."""

        if not isinstance(claim, Claim) or claim.phase != "FENCE_PENDING":
            raise LeaseConflictError("only a pending claim can reserve a fence")
        require_provider_capabilities(provider)
        with self._shared_lifetime_gate():
            started = self._begin_fence_reservation(claim)
            effect = self._provider_effect_for_claim(started)
            try:
                proof = provider.reserve_fence(effect)
            except Exception as exc:
                try:
                    self._mark_unknown_effect(
                        started,
                        now_ns=max(started.lease_heartbeat_ns, self._last_clock_ns),
                    )
                except (LeaseError, StoreError):
                    pass
                raise ProviderProofError(
                    "provider fence reservation outcome is unknown"
                ) from exc
            try:
                return self._activate_fence(started, proof)
            except Exception:
                try:
                    self._mark_unknown_effect(
                        started,
                        now_ns=max(started.lease_heartbeat_ns, self._last_clock_ns),
                    )
                except (LeaseError, StoreError):
                    pass
                raise

    def _activate_fence(
        self,
        claim: Claim,
        proof: ProviderFenceProof,
        *,
        now_ns: int | None = None,
    ) -> Claim:
        """Persist a provider reservation proof and enter ``CLAIMED``."""

        if not isinstance(claim, Claim) or claim.phase != "FENCE_PENDING":
            raise LeaseConflictError("only a pending claim can activate a fence")
        self._provider_effect_with_proof(claim, proof)
        timestamp = self._timestamp(now_ns)
        with self._lease_write_transaction() as connection:
            timestamp = self._record_clock(
                connection,
                timestamp,
                strict=now_ns is not None or self._clock_injected,
            )
            row = self._fetch_attempt(connection, claim.operation_id)
            if row is None:
                raise LeaseConflictError("lease claim is unavailable")
            self._require_uninvalidated_lease(row)
            self._require_current_epoch(connection, row["recovery_epoch"])
            self._check_claim_identity(
                row,
                claim,
                expected_status="FENCE_RESERVATION_STARTED",
            )
            current_expires = row["lease_expires_ns"]
            if type(current_expires) is not int:
                raise StoreIntegrityError("SQLite lease expiry is invalid")
            if timestamp >= current_expires:
                raise LeaseConflictError("pending lease has expired")
            connection.execute(
                """
                    UPDATE operation_attempts
                    SET fence_proof_version = ?, fence_proof_ref = ?
                    WHERE operation_id = ? AND attempt = ?
                      AND owner = ? AND provider_id = ?
                      AND lease_epoch = ? AND fencing_token = ?
                    """,
                (
                    proof.proof_version,
                    proof.proof_ref,
                    claim.operation_id,
                    claim.attempt,
                    claim.owner,
                    claim.provider_id,
                    claim.lease_epoch,
                    claim.fencing_token,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("fence activation was lost")
            connection.execute(
                """
                    UPDATE operations SET status = 'CLAIMED', updated_ns = ?
                    WHERE operation_id = ? AND status = 'FENCE_RESERVATION_STARTED'
                      AND current_attempt = ?
                    """,
                (timestamp, claim.operation_id, claim.attempt),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("fence activation was lost")
            self._append_event(
                connection,
                operation_id=claim.operation_id,
                attempt=claim.attempt,
                from_status="FENCE_RESERVATION_STARTED",
                to_status="CLAIMED",
                kind="fence",
                actor=claim.owner,
                timestamp=timestamp,
                reason_code="fence_activated",
            )
            activated = self._fetch_attempt(connection, claim.operation_id)
            if activated is None:
                raise StoreIntegrityError("SQLite activated lease is unavailable")
            return self._claim_from_row(activated)

    def _mark_unknown_effect(
        self,
        claim_or_effect: Claim | ProviderEffect,
        *,
        now_ns: int | None = None,
    ) -> OperationSnapshot:
        """Durably stop an ambiguous reservation/effect; never retry it."""

        if isinstance(claim_or_effect, Claim):
            operation_id = claim_or_effect.operation_id
            attempt = claim_or_effect.attempt
            identity_claim = claim_or_effect
            identity_effect = None
            expected_statuses = {
                "FENCE_PENDING",
                "FENCE_RESERVATION_STARTED",
                "CLAIMED",
            }
        elif isinstance(claim_or_effect, ProviderEffect):
            operation_id = claim_or_effect.operation_id
            attempt = claim_or_effect.attempt
            identity_claim = None
            identity_effect = claim_or_effect
            expected_statuses = {"EFFECT_PREPARED"}
        else:
            raise LeaseConflictError("lease identity has an unsupported type")
        timestamp = self._timestamp(now_ns)
        with self._lease_write_transaction() as connection:
            timestamp = self._record_clock(
                connection,
                timestamp,
                # Ambiguity must be durably fenced even if the provider call
                # moved an injected clock backwards while it was in flight.
                strict=False,
            )
            row = self._fetch_attempt(connection, operation_id, attempt)
            if row is None:
                raise LeaseConflictError("lease identity is unavailable")
            self._require_uninvalidated_lease(row)
            self._require_current_epoch(connection, row["recovery_epoch"])
            if row["status"] not in expected_statuses:
                raise LeaseConflictError("effect outcome cannot be changed")
            if identity_claim is not None:
                self._check_claim_identity(
                    row,
                    identity_claim,
                    expected_status=row["status"],
                )
            if identity_effect is not None:
                self._check_effect_identity(
                    row,
                    identity_effect,
                    expected_status="EFFECT_PREPARED",
                )
            connection.execute(
                """
                    UPDATE operations SET status = 'UNKNOWN_EFFECT', updated_ns = ?
                    WHERE operation_id = ? AND current_attempt = ?
                      AND status = ?
                    """,
                (timestamp, operation_id, attempt, row["status"]),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("unknown-effect transition was lost")
            actor = row["owner"]
            if actor is None:
                raise StoreIntegrityError("SQLite lease owner is incomplete")
            self._append_event(
                connection,
                operation_id=operation_id,
                attempt=attempt,
                from_status=row["status"],
                to_status="UNKNOWN_EFFECT",
                kind="unknown_effect",
                actor=actor,
                timestamp=timestamp,
                reason_code="effect_unknown",
            )
            return self._operation_snapshot_tx(connection, operation_id)

    def _begin_effect(
        self,
        claim: Claim,
        *,
        now_ns: int | None = None,
    ) -> ProviderEffect:
        """Commit an effect-prepared marker before any provider call."""

        if not isinstance(claim, Claim) or claim.phase != "CLAIMED":
            raise LeaseConflictError("only an activated claim can begin an effect")
        if claim.fence_proof is None:
            raise ProviderProofError("activated claim has no fence proof")
        timestamp = self._timestamp(now_ns)
        with self._lease_write_transaction() as connection:
            timestamp = self._record_clock(
                connection,
                timestamp,
                strict=now_ns is not None or self._clock_injected,
            )
            row = self._fetch_attempt(connection, claim.operation_id)
            if row is None:
                raise LeaseConflictError("lease claim is unavailable")
            self._require_uninvalidated_lease(row)
            self._require_current_epoch(connection, row["recovery_epoch"])
            self._check_claim_identity(
                row,
                claim,
                expected_status="CLAIMED",
            )
            current_expires = row["lease_expires_ns"]
            if type(current_expires) is not int:
                raise StoreIntegrityError("SQLite lease expiry is invalid")
            if timestamp >= current_expires:
                raise LeaseConflictError("lease has expired")
            proof = ProviderFenceProof(
                operation_id=row["operation_id"],
                effect_key=row["effect_key"],
                provider_id=row["attempt_provider_id"],
                owner=row["owner"],
                attempt=row["attempt"],
                lease_epoch=row["lease_epoch"],
                fencing_token=row["fencing_token"],
                proof_version=row["fence_proof_version"],
                proof_ref=row["fence_proof_ref"],
            )
            if proof != claim.fence_proof:
                raise ProviderProofError("stored fence proof does not match claim")
            connection.execute(
                """
                    UPDATE operation_attempts SET effect_started_ns = ?
                    WHERE operation_id = ? AND attempt = ?
                      AND owner = ? AND provider_id = ?
                      AND lease_epoch = ? AND fencing_token = ?
                      AND fence_proof_version = ? AND fence_proof_ref = ?
                    """,
                (
                    timestamp,
                    claim.operation_id,
                    claim.attempt,
                    claim.owner,
                    claim.provider_id,
                    claim.lease_epoch,
                    claim.fencing_token,
                    proof.proof_version,
                    proof.proof_ref,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("effect preparation was lost")
            connection.execute(
                """
                    UPDATE operations SET status = 'EFFECT_PREPARED', updated_ns = ?
                    WHERE operation_id = ? AND status = 'CLAIMED'
                      AND current_attempt = ?
                    """,
                (timestamp, claim.operation_id, claim.attempt),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("effect preparation was lost")
            self._append_event(
                connection,
                operation_id=claim.operation_id,
                attempt=claim.attempt,
                from_status="CLAIMED",
                to_status="EFFECT_PREPARED",
                kind="effect",
                actor=claim.owner,
                timestamp=timestamp,
                reason_code="effect_prepared",
            )
            return _issue_provider_effect(
                operation_id=claim.operation_id,
                effect_key=claim.effect_key,
                provider_id=claim.provider_id,
                owner=claim.owner,
                attempt=claim.attempt,
                lease_epoch=claim.lease_epoch,
                fencing_token=claim.fencing_token,
                fence_proof=proof,
            )

    def execute_effect(
        self,
        claim: Claim,
        provider: ProviderPort,
        *,
        now_ns: int | None = None,
    ) -> VerifiedProviderReceipt:
        """Execute one prepared effect outside SQLite and record its receipt."""

        require_provider_capabilities(provider)
        with self._shared_lifetime_gate():
            effect = self._begin_effect(claim, now_ns=now_ns)
            try:
                status = provider.execute(effect)
                if not isinstance(status, ProviderStatus):
                    raise ProviderReceiptError("provider returned an unverified status")
                proof = effect.fence_proof
                if proof is None:
                    raise ProviderProofError("prepared effect has no fence proof")
                receipt = _verified_receipt_from_status(effect, proof, status)
            except Exception as exc:
                try:
                    self._mark_unknown_effect(effect, now_ns=now_ns)
                except (LeaseError, StoreError):
                    pass
                if isinstance(exc, ProviderReceiptError):
                    raise
                raise ProviderReceiptError(
                    "provider effect outcome is unknown"
                ) from exc
            try:
                return self._record_receipt(effect, receipt, now_ns=now_ns)
            except Exception:
                # A receipt that cannot be durably committed leaves the external
                # outcome ambiguous; the operation must stop rather than retry.
                try:
                    self._mark_unknown_effect(effect, now_ns=now_ns)
                except (LeaseError, StoreError):
                    pass
                raise

    @staticmethod
    def _validate_receipt(
        effect: ProviderEffect,
        receipt: VerifiedProviderReceipt,
    ) -> None:
        if not isinstance(receipt, VerifiedProviderReceipt):
            raise ProviderReceiptError("provider receipt has an unsupported type")
        if not receipt.is_verified:
            raise ProviderReceiptError("provider receipt provenance is unverified")
        if effect.fence_proof is None:
            raise ProviderReceiptError("provider effect has no fence proof")
        for field in (
            "operation_id",
            "effect_key",
            "provider_id",
            "owner",
            "attempt",
            "lease_epoch",
            "fencing_token",
        ):
            if getattr(receipt, field) != getattr(effect, field):
                raise ProviderReceiptError("provider receipt identity mismatches")
        if receipt.provider_status != "COMPLETED":
            raise ProviderReceiptError("provider receipt status is not completed")
        if receipt.proof_version != effect.fence_proof.proof_version:
            raise ProviderReceiptError("provider receipt proof version mismatches")
        if receipt.proof_ref != effect.fence_proof.proof_ref:
            raise ProviderReceiptError("provider receipt proof reference mismatches")

    def _record_receipt(
        self,
        effect: ProviderEffect,
        receipt: VerifiedProviderReceipt,
        *,
        now_ns: int | None = None,
    ) -> VerifiedProviderReceipt:
        """Persist only a verified, identity-matching provider receipt."""

        self._validate_receipt(effect, receipt)
        timestamp = self._timestamp(now_ns)
        with self._lease_write_transaction() as connection:
            timestamp = self._record_clock(
                connection,
                timestamp,
                strict=now_ns is not None or self._clock_injected,
            )
            row = self._fetch_attempt(
                connection,
                effect.operation_id,
                effect.attempt,
            )
            if row is None:
                raise LeaseConflictError("provider effect is unavailable")
            self._require_uninvalidated_lease(row)
            self._require_current_epoch(connection, row["recovery_epoch"])
            self._check_effect_identity(
                row,
                effect,
                expected_status="EFFECT_PREPARED",
            )
            proof_version = row["fence_proof_version"]
            proof_ref = row["fence_proof_ref"]
            if proof_version != receipt.proof_version or proof_ref != receipt.proof_ref:
                raise ProviderReceiptError("stored fence proof mismatches receipt")
            connection.execute(
                """
                    INSERT INTO effect_receipts(
                        operation_id, attempt, effect_key, provider_effect_id,
                        provider_status, provider_id, owner, fencing_token,
                        lease_epoch, received_ns, proof_version, proof_ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    receipt.operation_id,
                    receipt.attempt,
                    receipt.effect_key,
                    receipt.provider_effect_id,
                    receipt.provider_status,
                    receipt.provider_id,
                    receipt.owner,
                    receipt.fencing_token,
                    receipt.lease_epoch,
                    timestamp,
                    receipt.proof_version,
                    receipt.proof_ref,
                ),
            )
            connection.execute(
                """
                    UPDATE operations SET status = 'RECEIPTED', updated_ns = ?
                    WHERE operation_id = ? AND status = 'EFFECT_PREPARED'
                      AND current_attempt = ?
                    """,
                (timestamp, effect.operation_id, effect.attempt),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("receipt transition was lost")
            self._append_event(
                connection,
                operation_id=effect.operation_id,
                attempt=effect.attempt,
                from_status="EFFECT_PREPARED",
                to_status="RECEIPTED",
                kind="receipt",
                actor=effect.owner,
                timestamp=timestamp,
                reason_code="receipt_recorded",
            )
            return receipt

    @staticmethod
    def _effect_from_receipt(receipt: VerifiedProviderReceipt) -> ProviderEffect:
        try:
            proof = ProviderFenceProof(
                operation_id=receipt.operation_id,
                effect_key=receipt.effect_key,
                provider_id=receipt.provider_id,
                owner=receipt.owner,
                attempt=receipt.attempt,
                lease_epoch=receipt.lease_epoch,
                fencing_token=receipt.fencing_token,
                proof_version=receipt.proof_version,
                proof_ref=receipt.proof_ref,
            )
            return _issue_provider_effect(
                operation_id=receipt.operation_id,
                effect_key=receipt.effect_key,
                provider_id=receipt.provider_id,
                owner=receipt.owner,
                attempt=receipt.attempt,
                lease_epoch=receipt.lease_epoch,
                fencing_token=receipt.fencing_token,
                fence_proof=proof,
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            ProviderProofError,
            ProviderReceiptError,
        ) as exc:
            raise ProviderReceiptError("provider receipt identity is invalid") from exc

    def complete(
        self,
        receipt: VerifiedProviderReceipt,
        *,
        now_ns: int | None = None,
    ) -> OperationSnapshot:
        """Complete only the exact operation attempt that owns the receipt."""

        if not isinstance(receipt, VerifiedProviderReceipt):
            raise ProviderReceiptError("provider receipt has an unsupported type")
        if not receipt.is_verified:
            raise ProviderReceiptError("provider receipt provenance is unverified")
        effect = self._effect_from_receipt(receipt)
        self._validate_receipt(effect, receipt)
        timestamp = self._timestamp(now_ns)
        with self._lease_write_transaction() as connection:
            timestamp = self._record_clock(
                connection,
                timestamp,
                strict=now_ns is not None or self._clock_injected,
            )
            row = self._fetch_attempt(
                connection,
                effect.operation_id,
                effect.attempt,
            )
            if row is None:
                raise LeaseConflictError("provider receipt is unavailable")
            self._require_uninvalidated_lease(row)
            self._require_current_epoch(connection, row["recovery_epoch"])
            self._check_effect_identity(
                row,
                effect,
                expected_status="RECEIPTED",
            )
            stored = connection.execute(
                """
                    SELECT effect_key, provider_effect_id, provider_status,
                           provider_id, owner, fencing_token, lease_epoch,
                           proof_version, proof_ref
                    FROM effect_receipts
                    WHERE operation_id = ? AND attempt = ?
                    """,
                (effect.operation_id, effect.attempt),
            ).fetchone()
            if stored is None:
                raise StoreIntegrityError("SQLite provider receipt is unavailable")
            stored_values = (
                stored["effect_key"],
                stored["provider_effect_id"],
                stored["provider_status"],
                stored["provider_id"],
                stored["owner"],
                stored["fencing_token"],
                stored["lease_epoch"],
                stored["proof_version"],
                stored["proof_ref"],
            )
            receipt_values = (
                receipt.effect_key,
                receipt.provider_effect_id,
                receipt.provider_status,
                receipt.provider_id,
                receipt.owner,
                receipt.fencing_token,
                receipt.lease_epoch,
                receipt.proof_version,
                receipt.proof_ref,
            )
            if stored_values != receipt_values:
                raise ProviderReceiptError("stored provider receipt mismatches")
            connection.execute(
                """
                    UPDATE operations SET status = 'COMPLETED', updated_ns = ?
                    WHERE operation_id = ? AND status = 'RECEIPTED'
                      AND current_attempt = ?
                    """,
                (timestamp, effect.operation_id, effect.attempt),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("completion transition was lost")
            self._append_event(
                connection,
                operation_id=effect.operation_id,
                attempt=effect.attempt,
                from_status="RECEIPTED",
                to_status="COMPLETED",
                kind="complete",
                actor=effect.owner,
                timestamp=timestamp,
                reason_code="operation_completed",
            )
            return self._operation_snapshot_tx(connection, effect.operation_id)

    def operation(self, operation_id: str) -> OperationSnapshot | None:
        """Return one immutable operation observation, or ``None`` if absent."""

        operation_id = _require_opaque_identifier(operation_id, "operation_id")
        try:
            with self._shared_lifetime_gate():
                connection = self._require_connection()
                row = connection.execute(
                    """
                    SELECT operation_id, effect_key, status, current_attempt,
                           provider_id, recovery_epoch, created_ns, updated_ns
                    FROM operations
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if row is None:
                    self._assert_transaction_identity()
                    return None
                result = self._operation_from_row(row)
                self._assert_transaction_identity()
                return result
        except StoreError:
            raise
        except sqlite3.OperationalError as exc:
            self._raise_read_error(exc)
        except sqlite3.DatabaseError as exc:
            raise StoreIntegrityError("SQLite operation read failed") from exc
        except (TypeError, ValueError, OverflowError) as exc:
            raise StoreIntegrityError("SQLite operation data is invalid") from exc
        raise StoreIntegrityError("SQLite operation read failed")

    def events(self, operation_id: str | None = None) -> tuple[TransitionEvent, ...]:
        """Return immutable journal observations in durable sequence order."""

        if operation_id is not None:
            operation_id = _require_opaque_identifier(operation_id, "operation_id")
        try:
            with self._shared_lifetime_gate():
                connection = self._require_connection()
                if operation_id is None:
                    rows = connection.execute(
                        """
                        SELECT event_id, event_schema_version, operation_id, attempt,
                               from_status, to_status, kind, actor, clock_ns,
                               reason_code, evidence_ref
                        FROM transition_events
                        ORDER BY event_id
                        """
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT event_id, event_schema_version, operation_id, attempt,
                               from_status, to_status, kind, actor, clock_ns,
                               reason_code, evidence_ref
                        FROM transition_events
                        WHERE operation_id = ?
                        ORDER BY event_id
                        """,
                        (operation_id,),
                    ).fetchall()
                result = tuple(self._event_from_row(row) for row in rows)
                self._assert_transaction_identity()
                return result
        except StoreError:
            raise
        except sqlite3.OperationalError as exc:
            self._raise_read_error(exc)
        except sqlite3.DatabaseError as exc:
            raise StoreIntegrityError("SQLite journal read failed") from exc
        except (TypeError, ValueError, OverflowError) as exc:
            raise StoreIntegrityError("SQLite journal data is invalid") from exc
        raise StoreIntegrityError("SQLite journal read failed")

    def _timestamp(self, value: int | None) -> int:
        timestamp = self._clock() if value is None else value
        return _require_sqlite_integer(timestamp, "clock_ns")

    @staticmethod
    def _lease_ttl(
        lease_ttl_ns: int | None,
    ) -> int:
        if lease_ttl_ns is None:
            raise ValueError("lease_ttl_ns is required")
        return _require_sqlite_integer(lease_ttl_ns, "lease_ttl_ns", minimum=1)

    @staticmethod
    def _lease_expiry(timestamp: int, ttl: int) -> int:
        if timestamp > SQLITE_INTEGER_MAX - ttl:
            raise ValueError("lease expiry exceeds supported integer")
        return timestamp + ttl

    def _load_store_high_water(self) -> None:
        connection = self._require_connection()
        metadata = {
            str(row["key"]): row["value"]
            for row in connection.execute(
                """
                SELECT key, value FROM store_meta
                WHERE key IN ('recovery_epoch', 'fencing_token_floor', 'last_clock_ns')
                """
            ).fetchall()
        }
        if frozenset(metadata) != {
            "recovery_epoch",
            "fencing_token_floor",
            "last_clock_ns",
        } or any(type(value) is not int for value in metadata.values()):
            raise StoreSchemaError("SQLite clock metadata is invalid")
        _require_sqlite_integer(metadata["recovery_epoch"], "recovery_epoch")
        _require_sqlite_integer(
            metadata["fencing_token_floor"],
            "fencing_token_floor",
        )
        self._last_clock_ns = _require_sqlite_integer(
            metadata["last_clock_ns"], "last_clock_ns"
        )

    def _record_clock(
        self,
        connection: sqlite3.Connection,
        timestamp: int,
        *,
        strict: bool = False,
    ) -> int:
        timestamp = _require_sqlite_integer(timestamp, "clock_ns")
        row = connection.execute(
            "SELECT value FROM store_meta WHERE key = 'last_clock_ns'"
        ).fetchone()
        if row is None or type(row["value"]) is not int:
            raise StoreIntegrityError("SQLite clock metadata is invalid")
        durable_clock = _require_sqlite_integer(row["value"], "last_clock_ns")
        if strict and timestamp < durable_clock:
            raise ClockRollbackError("clock moved behind the durable timestamp")
        timestamp = max(timestamp, durable_clock)
        connection.execute(
            "UPDATE store_meta SET value = ? WHERE key = 'last_clock_ns'",
            (timestamp,),
        )
        self._last_clock_ns = timestamp
        return timestamp

    @staticmethod
    def _metadata_integer(
        connection: sqlite3.Connection,
        key: str,
        name: str,
    ) -> int:
        row = connection.execute(
            "SELECT value FROM store_meta WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            raise StoreIntegrityError("SQLite store metadata is incomplete")
        return _require_sqlite_integer(row["value"], name)

    def _require_current_epoch(
        self,
        connection: sqlite3.Connection,
        operation_epoch: object,
    ) -> int:
        current = self._metadata_integer(
            connection,
            "recovery_epoch",
            "recovery_epoch",
        )
        if operation_epoch != current:
            raise LeaseConflictError("lease recovery epoch is stale")
        return current

    @staticmethod
    def _require_uninvalidated_lease(row: sqlite3.Row) -> None:
        """Reject old lease authority after restore epoch invalidation."""

        attempt = _require_sqlite_integer(row["attempt"], "attempt")
        if attempt == 0:
            return
        recovery_epoch = _require_sqlite_integer(
            row["recovery_epoch"],
            "recovery_epoch",
        )
        lease_epoch = _require_sqlite_integer(row["lease_epoch"], "lease_epoch")
        if recovery_epoch != lease_epoch:
            raise LeaseConflictError("operation lease was invalidated by restore")

    @staticmethod
    def _fetch_attempt(
        connection: sqlite3.Connection,
        operation_id: str,
        attempt: int | None = None,
    ) -> sqlite3.Row | None:
        if attempt is None:
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                SELECT o.operation_id, o.effect_key,
                       o.provider_id AS operation_provider_id,
                       o.status, o.current_attempt, o.recovery_epoch,
                       o.created_ns, o.updated_ns,
                       a.attempt, a.owner, a.provider_id AS attempt_provider_id,
                       a.lease_epoch, a.fencing_token,
                       a.lease_heartbeat_ns, a.lease_expires_ns,
                       a.fence_proof_version, a.fence_proof_ref,
                       a.effect_started_ns, a.fence_started_ns
                FROM operations AS o
                JOIN operation_attempts AS a
                  ON a.operation_id = o.operation_id
                 AND a.attempt = o.current_attempt
                WHERE o.operation_id = ?
                """,
                    (operation_id,),
                ).fetchone(),
            )
        return cast(
            sqlite3.Row | None,
            connection.execute(
                """
            SELECT o.operation_id, o.effect_key,
                   o.provider_id AS operation_provider_id,
                   o.status, o.current_attempt, o.recovery_epoch,
                   o.created_ns, o.updated_ns,
                   a.attempt, a.owner, a.provider_id AS attempt_provider_id,
                   a.lease_epoch, a.fencing_token,
                   a.lease_heartbeat_ns, a.lease_expires_ns,
                   a.fence_proof_version, a.fence_proof_ref,
                   a.effect_started_ns, a.fence_started_ns
            FROM operations AS o
            JOIN operation_attempts AS a
              ON a.operation_id = o.operation_id
            WHERE o.operation_id = ? AND a.attempt = ?
            """,
                (operation_id, attempt),
            ).fetchone(),
        )

    @staticmethod
    def _claim_from_row(row: sqlite3.Row) -> Claim:
        try:
            provider_id = row["attempt_provider_id"]
            if provider_id is None or row["owner"] is None:
                raise StoreIntegrityError("SQLite lease identity is incomplete")
            if row["operation_provider_id"] != provider_id:
                raise StoreIntegrityError(
                    "SQLite lease provider identity is inconsistent"
                )
            proof_version = row["fence_proof_version"]
            proof_ref = row["fence_proof_ref"]
            if (proof_version is None) != (proof_ref is None):
                raise StoreIntegrityError("SQLite fence proof is incomplete")
            proof: ProviderFenceProof | None = None
            if proof_version is not None and proof_ref is not None:
                proof = ProviderFenceProof(
                    operation_id=row["operation_id"],
                    effect_key=row["effect_key"],
                    provider_id=provider_id,
                    owner=row["owner"],
                    attempt=row["attempt"],
                    lease_epoch=row["lease_epoch"],
                    fencing_token=row["fencing_token"],
                    proof_version=proof_version,
                    proof_ref=proof_ref,
                )
            phase = row["status"]
            if phase not in {"FENCE_PENDING", "CLAIMED"}:
                raise StoreIntegrityError("SQLite lease phase is invalid")
            if phase == "FENCE_PENDING" and row["fence_started_ns"] is not None:
                raise StoreIntegrityError(
                    "SQLite pending lease contains a fence reservation marker"
                )
            if phase == "CLAIMED" and row["fence_started_ns"] is None:
                raise StoreIntegrityError(
                    "SQLite activated lease has no fence reservation marker"
                )
            return Claim(
                operation_id=row["operation_id"],
                effect_key=row["effect_key"],
                provider_id=provider_id,
                owner=row["owner"],
                attempt=row["attempt"],
                lease_epoch=row["lease_epoch"],
                fencing_token=row["fencing_token"],
                lease_heartbeat_ns=row["lease_heartbeat_ns"],
                lease_expires_ns=row["lease_expires_ns"],
                phase=phase,
                fence_proof=proof,
            )
        except StoreError:
            raise
        except (TypeError, ValueError, OverflowError) as exc:
            raise StoreIntegrityError("SQLite lease data is invalid") from exc

    @staticmethod
    def _check_claim_identity(
        row: sqlite3.Row,
        claim: Claim,
        *,
        expected_status: str,
    ) -> None:
        if not isinstance(claim, Claim):
            raise LeaseConflictError("lease claim has an unsupported type")
        expected = (
            ("operation_id", row["operation_id"], claim.operation_id),
            ("effect_key", row["effect_key"], claim.effect_key),
            ("operation_provider_id", row["operation_provider_id"], claim.provider_id),
            ("provider_id", row["attempt_provider_id"], claim.provider_id),
            ("owner", row["owner"], claim.owner),
            ("attempt", row["attempt"], claim.attempt),
            ("recovery_epoch", row["recovery_epoch"], claim.recovery_epoch),
            ("lease_epoch", row["lease_epoch"], claim.lease_epoch),
            ("fencing_token", row["fencing_token"], claim.fencing_token),
            ("status", row["status"], expected_status),
        )
        if any(actual != wanted for _, actual, wanted in expected):
            raise LeaseConflictError("lease identity is stale or mismatched")
        stored_proof = (
            row["fence_proof_version"],
            row["fence_proof_ref"],
        )
        claim_proof = (
            (None, None)
            if claim.fence_proof is None
            else (claim.fence_proof.proof_version, claim.fence_proof.proof_ref)
        )
        if stored_proof != claim_proof:
            raise LeaseConflictError("lease fence proof is stale or mismatched")
        if expected_status == "FENCE_PENDING" and row["fence_started_ns"] is not None:
            raise LeaseConflictError("lease reservation has already started")
        if expected_status == "CLAIMED" and row["fence_started_ns"] is None:
            raise StoreIntegrityError("SQLite activated lease has no fence marker")

    @staticmethod
    def _check_effect_identity(
        row: sqlite3.Row,
        effect: ProviderEffect,
        *,
        expected_status: str,
    ) -> None:
        if (
            not isinstance(effect, ProviderEffect)
            or not effect.is_issued
            or effect.fence_proof is None
        ):
            raise LeaseConflictError("provider effect identity is incomplete")
        expected = (
            ("operation_id", row["operation_id"], effect.operation_id),
            ("effect_key", row["effect_key"], effect.effect_key),
            ("operation_provider_id", row["operation_provider_id"], effect.provider_id),
            ("provider_id", row["attempt_provider_id"], effect.provider_id),
            ("owner", row["owner"], effect.owner),
            ("attempt", row["attempt"], effect.attempt),
            ("recovery_epoch", row["recovery_epoch"], effect.lease_epoch),
            ("lease_epoch", row["lease_epoch"], effect.lease_epoch),
            ("fencing_token", row["fencing_token"], effect.fencing_token),
            ("status", row["status"], expected_status),
        )
        if any(actual != wanted for _, actual, wanted in expected):
            raise LeaseConflictError("provider effect identity is stale or mismatched")
        if (
            row["fence_proof_version"],
            row["fence_proof_ref"],
        ) != (
            effect.fence_proof.proof_version,
            effect.fence_proof.proof_ref,
        ):
            raise LeaseConflictError("provider effect proof is stale or mismatched")
        if row["fence_started_ns"] is None:
            raise StoreIntegrityError("SQLite effect has no fence reservation marker")

    @staticmethod
    def _next_value(connection: sqlite3.Connection) -> int:
        floor = CoordinationStore._metadata_integer(
            connection,
            "fencing_token_floor",
            "fencing_token_floor",
        )
        row = connection.execute(
            "SELECT MAX(fencing_token) AS maximum FROM operation_attempts"
        ).fetchone()
        maximum = 0 if row is None or row["maximum"] is None else row["maximum"]
        maximum = max(
            floor,
            _require_sqlite_integer(maximum, "fencing_token"),
        )
        if maximum >= SQLITE_INTEGER_MAX:
            raise ValueError("fencing_token exceeds supported integer")
        token = maximum + 1
        connection.execute(
            "UPDATE store_meta SET value = ? WHERE key = 'fencing_token_floor'",
            (token,),
        )
        return token

    def _reserve_floor(self) -> RecoveryFloorReservation:
        """Issue an opaque, non-mutating reservation for a new global floor."""

        try:
            with self._shared_lifetime_gate():
                connection = self._require_connection()
                epoch = self._metadata_integer(
                    connection,
                    "recovery_epoch",
                    "recovery_epoch",
                )
                floor = self._metadata_integer(
                    connection,
                    "fencing_token_floor",
                    "fencing_token_floor",
                )
                row = connection.execute(
                    "SELECT MAX(fencing_token) AS maximum FROM operation_attempts"
                ).fetchone()
                maximum = 0 if row is None or row["maximum"] is None else row["maximum"]
                maximum = max(
                    floor,
                    _require_sqlite_integer(maximum, "fencing_token"),
                )
                if epoch >= SQLITE_INTEGER_MAX or maximum >= SQLITE_INTEGER_MAX:
                    raise ValueError("recovery floor exceeds supported integer")
                reservation = _issue_floor_reservation(epoch + 1, maximum + 1)
                self._assert_transaction_identity()
                return reservation
        except StoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError) as exc:
            _raise_recovery_read_error(exc, kind="recovery floor")

    @staticmethod
    def _inspect_image_bytes(
        metadata: os.stat_result,
        image: bytes,
        *,
        label: str,
    ) -> StoreImageObservation:
        """Validate one immutable SQLite image and return only typed evidence."""

        if not isinstance(image, bytes):
            raise StoreIntegrityError(f"{label} image is invalid")
        digest = "sha256:" + hashlib.sha256(image).hexdigest()
        try:
            with _temporary_sqlite_connection(f"SQLite {label} image") as connection:
                connection.row_factory = sqlite3.Row
                connection.deserialize(_memory_image(image))
                _validate_existing_schema(connection)

                def metadata_integer(key: str, name: str) -> int:
                    row = connection.execute(
                        "SELECT value FROM store_meta WHERE key = ?",
                        (key,),
                    ).fetchone()
                    if row is None:
                        raise StoreIntegrityError(
                            "SQLite restore metadata is incomplete"
                        )
                    return _require_sqlite_integer(row["value"], name)

                floor = RecoveryFloor(
                    metadata_integer("recovery_epoch", "recovery_epoch"),
                    metadata_integer("fencing_token_floor", "fencing_token_floor"),
                )
                last_clock_ns = metadata_integer("last_clock_ns", "last_clock_ns")
                maximum = _validate_image_high_water(
                    connection,
                    floor=floor,
                    last_clock_ns=last_clock_ns,
                )
                operations: list[RecoverySnapshot] = []
                for row in connection.execute(
                    "SELECT operation_id FROM operations ORDER BY operation_id"
                ).fetchall():
                    snapshot = _read_existing_recovery_snapshot(
                        connection,
                        row["operation_id"],
                    )
                    if snapshot is None:
                        raise StoreIntegrityError(
                            "SQLite restore operation snapshot is unavailable"
                        )
                    operations.append(snapshot)
                operation_values = tuple(operations)
                identities = tuple(
                    _restore_identity_from_snapshot(snapshot)
                    for snapshot in operation_values
                )
                return StoreImageObservation(
                    database_identity=_identity(metadata),
                    size=metadata.st_size,
                    digest=digest,
                    floor=floor,
                    max_fencing_token=maximum,
                    last_clock_ns=last_clock_ns,
                    operations=operation_values,
                    identities=identities,
                )
        except StoreError:
            raise
        except sqlite3.DatabaseError as exc:
            error = StoreIntegrityError(f"SQLite {label} image is invalid")
            _adopt_cleanup_capability(error, exc)
            raise error from exc
        except (TypeError, ValueError, OverflowError) as exc:
            error = StoreIntegrityError(f"SQLite {label} image data is invalid")
            _adopt_cleanup_capability(error, exc)
            raise error from exc

    @staticmethod
    def _inspect_image_fd(fd: int) -> StoreImageObservation:
        """Inspect a validated database fd without returning fd/connection state."""

        metadata, image = _read_image_fd(fd, label="SQLite restore image")
        return CoordinationStore._inspect_image_bytes(
            metadata,
            image,
            label="restore",
        )

    @staticmethod
    def _verify_history_binding(
        primary_fd: int,
        state: object,
    ) -> None:
        """Verify the stable committed-restore anchor without provider access."""

        active_tombstones, latest = _normal_open_state_values(state)
        binding_ref = _restore_history_binding_ref(state)
        if not active_tombstones:
            return
        if latest is None or binding_ref is None:
            raise StoreIntegrityError("restore history binding is unavailable")
        metadata, image = _read_image_fd(
            primary_fd,
            label="SQLite restore primary",
        )
        observation = CoordinationStore._inspect_image_bytes(
            metadata,
            image,
            label="restore primary",
        )
        try:
            final_epoch = object.__getattribute__(latest, "recovery_epoch")
            final_token_floor = object.__getattribute__(
                latest,
                "fencing_token_floor",
            )
            expected_actor = object.__getattribute__(latest, "actor")
        except AttributeError as exc:
            raise StoreIntegrityError("recovery history handle is invalid") from exc
        if observation.floor.recovery_epoch < final_epoch:
            raise StoreIntegrityError("primary recovery epoch regressed")
        if observation.floor.fencing_token_floor < final_token_floor:
            raise StoreIntegrityError("primary fencing-token floor regressed")
        events = CoordinationStore._read_image_events(
            image,
            label="restore primary",
        )
        if not any(
            event.kind == "restore"
            and event.actor == expected_actor
            and event.reason_code == "restore"
            and event.evidence_ref == binding_ref
            for event in events
        ):
            raise StoreIntegrityError("restore history binding anchor is missing")

    def _read_restore_floor(self, fd: int) -> RecoveryFloor:
        """Return only the validated floor observation for one image fd."""

        return self._inspect_image_fd(fd).floor

    def _read_floor(self, fd: int) -> RecoveryFloor:
        """Package-private alias for the candidate floor observation seam."""

        return self._read_restore_floor(fd)

    def _read_restore_identities(self, fd: int) -> tuple[RestoreIdentity, ...]:
        """Return all validated operation identities for one image fd."""

        return self._inspect_image_fd(fd).identities

    def _read_identities(self, fd: int) -> tuple[RestoreIdentity, ...]:
        """Package-private alias for the candidate identity observation seam."""

        return self._read_restore_identities(fd)

    @staticmethod
    def _assert_restore_observation(
        expected: StoreImageObservation,
        actual: StoreImageObservation,
        *,
        label: str,
    ) -> None:
        if type(expected) is not StoreImageObservation or expected != actual:
            raise LeaseConflictError(f"restore {label} image observation is stale")

    @staticmethod
    def _restore_floor_bounds(
        source: StoreImageObservation,
        destination: StoreImageObservation,
        ledger_floor_lower_bound: RecoveryFloor,
    ) -> tuple[int, int]:
        if type(source) is not StoreImageObservation:
            raise StoreIntegrityError("restore source observation is invalid")
        if type(destination) is not StoreImageObservation:
            raise StoreIntegrityError("restore destination observation is invalid")
        if type(ledger_floor_lower_bound) is not RecoveryFloor:
            raise StoreIntegrityError("restore ledger floor is invalid")
        epoch = max(
            source.floor.recovery_epoch,
            destination.floor.recovery_epoch,
            ledger_floor_lower_bound.recovery_epoch,
        )
        fencing_token = max(
            source.floor.fencing_token_floor,
            source.max_fencing_token,
            destination.floor.fencing_token_floor,
            destination.max_fencing_token,
            ledger_floor_lower_bound.fencing_token_floor,
        )
        if epoch >= SQLITE_INTEGER_MAX or fencing_token >= SQLITE_INTEGER_MAX:
            raise StoreIntegrityError("restore floor exceeds supported integer")
        return epoch + 1, fencing_token + 1

    @staticmethod
    def _restore_binding_ref(
        *,
        restore_generation: int,
        actor: str,
        audit_evidence_ref: str,
        source_digest: str,
        previous_primary_digest: str,
        previous_recovery_epoch: int,
        previous_fencing_token_hwm: int,
        previous_last_clock_ns: int,
        final_floor: RecoveryFloor,
        current_tombstones: tuple[RestoreIdentity, ...],
        active_tombstones: tuple[RestoreIdentity, ...],
    ) -> str:
        try:
            return _restore_binding_digest(
                restore_generation=restore_generation,
                actor=actor,
                audit_evidence_ref=audit_evidence_ref,
                source_digest=source_digest,
                previous_primary_digest=previous_primary_digest,
                previous_recovery_epoch=previous_recovery_epoch,
                previous_fencing_token_hwm=previous_fencing_token_hwm,
                previous_last_clock_ns=previous_last_clock_ns,
                final_floor=final_floor,
                current_tombstones=current_tombstones,
                active_tombstones=active_tombstones,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise StoreIntegrityError("restore binding evidence is invalid") from exc

    @staticmethod
    def _reserve_restore_floor(
        source: StoreImageObservation,
        destination: StoreImageObservation,
        ledger_floor_lower_bound: RecoveryFloor,
    ) -> RecoveryFloorReservation:
        """Issue a Store-owned floor above source, destination, and ledger HWM."""

        epoch, fencing_token = CoordinationStore._restore_floor_bounds(
            source,
            destination,
            ledger_floor_lower_bound,
        )
        return _issue_floor_reservation(epoch, fencing_token)

    @staticmethod
    def _record_restore_clock(
        connection: sqlite3.Connection,
        timestamp: int,
    ) -> int:
        timestamp = _require_sqlite_integer(timestamp, "clock_ns")
        row = connection.execute(
            "SELECT value FROM store_meta WHERE key = 'last_clock_ns'"
        ).fetchone()
        if row is None:
            raise StoreIntegrityError("SQLite restore clock metadata is incomplete")
        durable = _require_sqlite_integer(row["value"], "last_clock_ns")
        if timestamp < durable:
            raise ClockRollbackError("restore timestamp moved behind durable clock")
        connection.execute(
            "UPDATE store_meta SET value = ? WHERE key = 'last_clock_ns'",
            (timestamp,),
        )
        return timestamp

    @staticmethod
    def _restore_operation_tx(
        connection: sqlite3.Connection,
        snapshot: RecoverySnapshot,
        *,
        recovery_epoch: int,
        timestamp: int,
        actor: str,
        evidence_ref: str | None,
    ) -> int:
        """Apply one status-preserving restore transform inside candidate SQL."""

        if snapshot.status == "RESTORE_INCOMPLETE":
            raise StoreIntegrityError("restore source contains incomplete state")
        if snapshot.status == "CLEANED":
            return 0
        if snapshot.status == "RECEIPTED":
            receipt = snapshot.verified_receipt_identity
            if receipt is None:
                raise StoreIntegrityError("SQLite restore receipt is unavailable")
            new_token = CoordinationStore._next_value(connection)
            connection.execute(
                """
                UPDATE operation_attempts
                SET lease_epoch = ?, fencing_token = ?
                WHERE operation_id = ? AND attempt = ?
                  AND owner = ? AND provider_id = ?
                  AND lease_epoch = ? AND fencing_token = ?
                """,
                (
                    recovery_epoch,
                    new_token,
                    snapshot.operation_id,
                    snapshot.current_attempt,
                    snapshot.owner,
                    snapshot.provider_id,
                    snapshot.lease_epoch,
                    snapshot.fencing_token,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("restore receipt lease rebind was lost")
            connection.execute(
                """
                UPDATE effect_receipts
                SET lease_epoch = ?, fencing_token = ?
                WHERE operation_id = ? AND attempt = ?
                  AND effect_key = ? AND provider_effect_id = ?
                  AND provider_id = ? AND owner = ?
                  AND lease_epoch = ? AND fencing_token = ?
                  AND proof_version = ? AND proof_ref = ?
                """,
                (
                    recovery_epoch,
                    new_token,
                    receipt.operation_id,
                    receipt.attempt,
                    receipt.effect_key,
                    receipt.provider_effect_id,
                    receipt.provider_id,
                    receipt.owner,
                    receipt.lease_epoch,
                    receipt.fencing_token,
                    receipt.proof_version,
                    receipt.proof_ref,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("restore receipt rebind was lost")
        connection.execute(
            """
            UPDATE operations
            SET recovery_epoch = ?, updated_ns = ?
            WHERE operation_id = ? AND current_attempt = ?
              AND status = ? AND recovery_epoch = ? AND updated_ns = ?
            """,
            (
                recovery_epoch,
                timestamp,
                snapshot.operation_id,
                snapshot.current_attempt,
                snapshot.status,
                snapshot.recovery_epoch,
                snapshot.updated_ns,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise LeaseConflictError("restore operation update was lost")
        CoordinationStore._append_event(
            connection,
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=snapshot.status,
            to_status=snapshot.status,
            kind="restore",
            actor=actor,
            timestamp=timestamp,
            reason_code="restore",
            evidence_ref=evidence_ref,
        )
        return 1

    @staticmethod
    def _apply_restore_candidate(
        source_fd: int,
        destination_fd: int,
        target_fd: int,
        *,
        source_observation: StoreImageObservation | None = None,
        destination_observation: StoreImageObservation | None = None,
        ledger_floor_lower_bound: RecoveryFloor,
        reservation: RecoveryFloorReservation | None = None,
        previous_active_tombstones: object,
        restore_generation: int,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
        fault: Callable[[str], None] | None = None,
    ) -> RestoreApplyResult:
        """Apply a validated source image to an empty caller-owned candidate fd."""

        fault_callback = _noop_restore_fault if fault is None else fault
        if not callable(fault_callback):
            raise StoreIntegrityError("restore fault hook is invalid")
        actor = _require_opaque_identifier(actor, "actor")
        if evidence_ref is not None:
            evidence_ref = _require_evidence_ref(evidence_ref)
        timestamp = _require_sqlite_integer(timestamp, "clock_ns")
        if type(ledger_floor_lower_bound) is not RecoveryFloor:
            raise StoreIntegrityError("restore ledger floor is invalid")
        if type(restore_generation) is not int or restore_generation < 1:
            raise StoreIntegrityError("restore generation is invalid")
        previous_active = _restore_active_identities(previous_active_tombstones)
        previous_active_keys = {
            (identity.operation_id, identity.effect_key) for identity in previous_active
        }
        source_metadata, source_image = _read_image_fd(
            source_fd,
            label="SQLite restore source",
        )
        destination_metadata, destination_image = _read_image_fd(
            destination_fd,
            label="SQLite restore destination",
        )
        if _identity(source_metadata) == _identity(destination_metadata):
            raise LeaseConflictError("restore source and destination images alias")
        actual_source = CoordinationStore._inspect_image_bytes(
            source_metadata,
            source_image,
            label="restore source",
        )
        actual_destination = CoordinationStore._inspect_image_bytes(
            destination_metadata,
            destination_image,
            label="restore destination",
        )
        if source_observation is not None:
            CoordinationStore._assert_restore_observation(
                source_observation,
                actual_source,
                label="source",
            )
        else:
            source_observation = actual_source
        if destination_observation is not None:
            CoordinationStore._assert_restore_observation(
                destination_observation,
                actual_destination,
                label="destination",
            )
        else:
            destination_observation = actual_destination
        if timestamp < max(
            source_observation.last_clock_ns,
            destination_observation.last_clock_ns,
        ):
            raise ClockRollbackError("restore timestamp moved behind durable clock")
        target_metadata, target_image = _read_image_fd(
            target_fd,
            label="SQLite restore candidate",
            allow_empty=True,
        )
        if target_metadata.st_size != 0 or target_image:
            raise LeaseConflictError("restore candidate target is not empty")
        if reservation is None:
            reservation = CoordinationStore._reserve_restore_floor(
                source_observation,
                destination_observation,
                ledger_floor_lower_bound,
            )
        elif (
            type(reservation) is not RecoveryFloorReservation
            or not reservation.is_issued
        ):
            raise LeaseConflictError("restore floor reservation is invalid")
        expected_epoch, expected_token_floor = CoordinationStore._restore_floor_bounds(
            source_observation,
            destination_observation,
            ledger_floor_lower_bound,
        )
        if (
            reservation.recovery_epoch != expected_epoch
            or reservation.fencing_token_floor != expected_token_floor
        ):
            raise LeaseConflictError("restore floor reservation is stale")
        source_tombstone_keys = {
            (identity.operation_id, identity.effect_key)
            for identity in source_observation.identities
        }
        if source_tombstone_keys & previous_active_keys:
            raise StoreIntegrityError(
                "restore source contains a previously tombstoned identity"
            )
        tombstones = tuple(
            identity
            for identity in destination_observation.identities
            if (identity.operation_id, identity.effect_key) not in source_tombstone_keys
        )
        active_tombstones = tuple(
            sorted(
                {
                    *previous_active,
                    *tombstones,
                },
                key=lambda identity: (identity.operation_id, identity.effect_key),
            )
        )
        receipted_count = sum(
            operation.status == "RECEIPTED"
            for operation in source_observation.operations
        )
        if (
            not any(
                operation.status != "CLEANED"
                for operation in source_observation.operations
            )
            and active_tombstones
        ):
            raise StoreIntegrityError(
                "restore tombstones have no operation event anchor"
            )
        if reservation.fencing_token_floor > (SQLITE_INTEGER_MAX - receipted_count):
            raise StoreIntegrityError("restore final fencing floor exceeds range")
        expected_final_floor = RecoveryFloor(
            reservation.recovery_epoch,
            reservation.fencing_token_floor + receipted_count,
        )
        binding_ref = CoordinationStore._restore_binding_ref(
            restore_generation=restore_generation,
            actor=actor,
            audit_evidence_ref=evidence_ref
            if evidence_ref is not None
            else "sha256:" + "0" * 64,
            source_digest=source_observation.digest,
            previous_primary_digest=destination_observation.digest,
            previous_recovery_epoch=destination_observation.floor.recovery_epoch,
            previous_fencing_token_hwm=max(
                destination_observation.floor.fencing_token_floor,
                destination_observation.max_fencing_token,
            ),
            previous_last_clock_ns=destination_observation.last_clock_ns,
            final_floor=expected_final_floor,
            current_tombstones=tombstones,
            active_tombstones=active_tombstones,
        )

        candidate: sqlite3.Connection | None = None
        candidate_owner: _ConnectionCleanupOwner | None = None
        body_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            candidate = sqlite3.connect(
                ":memory:",
                uri=False,
                timeout=0,
                isolation_level=None,
            )
            candidate_owner = _ConnectionCleanupOwner(
                candidate,
                "SQLite restore candidate",
            )
            assert candidate_owner is not None
            candidate.row_factory = sqlite3.Row
            candidate.deserialize(_memory_image(source_image))
            _validate_existing_schema(candidate)
            fault_callback("before_restore_begin")
            candidate.execute("BEGIN IMMEDIATE")
            commit_started = False
            try:
                fault_callback("after_restore_begin")
                CoordinationStore._record_restore_clock(candidate, timestamp)
                current_epoch = CoordinationStore._metadata_integer(
                    candidate,
                    "recovery_epoch",
                    "recovery_epoch",
                )
                current_floor = CoordinationStore._metadata_integer(
                    candidate,
                    "fencing_token_floor",
                    "fencing_token_floor",
                )
                maximum_row = candidate.execute(
                    "SELECT MAX(fencing_token) AS maximum FROM operation_attempts"
                ).fetchone()
                maximum = (
                    0
                    if maximum_row is None or maximum_row["maximum"] is None
                    else _require_sqlite_integer(
                        maximum_row["maximum"],
                        "fencing_token",
                    )
                )
                if (
                    reservation.recovery_epoch <= current_epoch
                    or reservation.fencing_token_floor <= max(current_floor, maximum)
                ):
                    raise LeaseConflictError("restore floor reservation is stale")
                candidate.execute(
                    "UPDATE store_meta SET value = ? "
                    "WHERE key = 'recovery_epoch' AND value = ?",
                    (reservation.recovery_epoch, current_epoch),
                )
                if candidate.execute("SELECT changes()").fetchone()[0] != 1:
                    raise LeaseConflictError("restore recovery epoch advance was lost")
                candidate.execute(
                    "UPDATE store_meta SET value = ? "
                    "WHERE key = 'fencing_token_floor' AND value = ?",
                    (reservation.fencing_token_floor, current_floor),
                )
                if candidate.execute("SELECT changes()").fetchone()[0] != 1:
                    raise LeaseConflictError("restore fencing floor advance was lost")
                operation_ids = tuple(
                    row["operation_id"]
                    for row in candidate.execute(
                        "SELECT operation_id FROM operations ORDER BY operation_id"
                    ).fetchall()
                )
                event_count = 0
                for operation_id in operation_ids:
                    fault_callback("before_restore_operation")
                    snapshot = _read_existing_recovery_snapshot(
                        candidate,
                        operation_id,
                    )
                    if snapshot is None:
                        raise StoreIntegrityError(
                            "SQLite restore operation snapshot is unavailable"
                        )
                    event_count += CoordinationStore._restore_operation_tx(
                        candidate,
                        snapshot,
                        recovery_epoch=reservation.recovery_epoch,
                        timestamp=timestamp,
                        actor=actor,
                        evidence_ref=binding_ref,
                    )
                fault_callback("before_restore_commit")
                commit_started = True
                candidate.commit()
                event_count = int(
                    candidate.execute(
                        "SELECT COUNT(*) FROM transition_events WHERE kind = 'restore'"
                    ).fetchone()[0]
                )
                fault_callback("after_restore_commit")
            except _CLEANUP_EXCEPTION as phase_error:
                if commit_started:
                    error = StoreCommitUnknownError(
                        "SQLite restore candidate commit status is unknown"
                    )
                    _attach_cleanup_capability(
                        error,
                        _CleanupCapability(candidate_owner.retry_cleanup),
                    )
                    raise error from phase_error
                rollback_error: BaseException | None = None
                try:
                    if candidate.in_transaction:
                        candidate.rollback()
                except _CLEANUP_EXCEPTION as exc:
                    rollback_error = exc
                if rollback_error is not None:
                    _raise_with_cleanup_capability(
                        phase_error,
                        _CleanupCapability(candidate_owner.retry_cleanup),
                    )
                raise
            try:
                final_image = _wal_image(candidate.serialize())
            except _CLEANUP_EXCEPTION as serialization_error:
                error = StoreCommitUnknownError(
                    "SQLite restore candidate image status is unknown"
                )
                _attach_cleanup_capability(
                    error,
                    _CleanupCapability(candidate_owner.retry_cleanup),
                )
                raise error from serialization_error
            if not isinstance(final_image, bytes):
                error = StoreCommitUnknownError(
                    "SQLite restore candidate serialization is unknown"
                )
                _attach_cleanup_capability(
                    error,
                    _CleanupCapability(candidate_owner.retry_cleanup),
                )
                raise error
        except StoreError as exc:
            body_error = exc
        except sqlite3.DatabaseError as exc:
            mapped_error = StoreIntegrityError(
                "SQLite restore candidate transaction failed"
            )
            _adopt_cleanup_capability(mapped_error, exc)
            body_error = mapped_error
        except (TypeError, ValueError, OverflowError) as exc:
            mapped_error = StoreIntegrityError("SQLite restore candidate is invalid")
            _adopt_cleanup_capability(mapped_error, exc)
            body_error = mapped_error
        except _CLEANUP_EXCEPTION as exc:
            body_error = exc
        finally:
            if candidate_owner is not None:
                try:
                    candidate_owner.retry_cleanup()
                except _CLEANUP_EXCEPTION as exc:
                    cleanup_error = exc

        if body_error is not None:
            if cleanup_error is not None:
                assert candidate_owner is not None
                _raise_with_cleanup_capability(
                    body_error,
                    _CleanupCapability(candidate_owner.retry_cleanup),
                )
            if candidate_owner is None:
                raise body_error
            attached = _attach_cleanup_capability(
                body_error,
                _CleanupCapability(candidate_owner.retry_cleanup),
            )
            if attached is not body_error:
                raise attached from body_error
            raise body_error
        if cleanup_error is not None:
            assert candidate_owner is not None
            _raise_with_cleanup_capability(
                cleanup_error,
                _CleanupCapability(candidate_owner.retry_cleanup),
            )

        current_source_metadata, current_source_image = _read_image_fd(
            source_fd,
            label="SQLite restore source",
        )
        current_destination_metadata, current_destination_image = _read_image_fd(
            destination_fd,
            label="SQLite restore destination",
        )
        CoordinationStore._assert_restore_observation(
            source_observation,
            CoordinationStore._inspect_image_bytes(
                current_source_metadata,
                current_source_image,
                label="restore source",
            ),
            label="source",
        )
        CoordinationStore._assert_restore_observation(
            destination_observation,
            CoordinationStore._inspect_image_bytes(
                current_destination_metadata,
                current_destination_image,
                label="restore destination",
            ),
            label="destination",
        )
        fault_callback("before_restore_candidate_write")
        _write_image_fd(target_fd, final_image, label="SQLite restore candidate")
        fault_callback("after_restore_candidate_write")
        try:
            final_metadata, final_readback = _read_image_fd(
                target_fd,
                label="SQLite restore candidate",
            )
        except StoreCommitUnknownError:
            raise
        except StoreError as exc:
            raise StoreCommitUnknownError(
                "SQLite restore candidate readback is unknown"
            ) from exc
        if final_readback != final_image:
            raise StoreCommitUnknownError(
                "SQLite restore candidate readback does not match committed image"
            )
        final_observation = CoordinationStore._inspect_image_bytes(
            final_metadata,
            final_readback,
            label="restore candidate",
        )
        CoordinationStore._verify_restore_projection(
            source_observation,
            final_observation,
            CoordinationStore._read_image_events(
                current_source_image,
                label="restore source",
            ),
            CoordinationStore._read_image_events(
                final_readback,
                label="restore candidate",
            ),
            final_floor=expected_final_floor,
            actor=actor,
            evidence_ref=binding_ref,
            expected_event_count=event_count,
            active_tombstones=active_tombstones,
            pre_rebind_fencing_token_floor=reservation.fencing_token_floor,
        )
        source_keys = {
            (identity.operation_id, identity.effect_key)
            for identity in source_observation.identities
        }
        tombstones = tuple(
            identity
            for identity in destination_observation.identities
            if (identity.operation_id, identity.effect_key) not in source_keys
        )
        return _issue_restore_apply_result(
            observation=final_observation,
            tombstones=tombstones,
            active_tombstones=active_tombstones,
            restore_event_count=event_count,
        )

    @staticmethod
    def _verify_candidate_applied(
        candidate_fd: int,
        expected: RestoreApplyResult,
    ) -> RestoreApplyResult:
        """Verify candidate bytes only; this operation never reapplies SQL."""

        if (
            type(expected) is not RestoreApplyResult
            or not expected.is_issued
            or not expected.applied
        ):
            raise LeaseConflictError("restore candidate expectation is invalid")
        actual_observation = CoordinationStore._inspect_image_fd(candidate_fd)
        expected_observation = expected.observation
        if actual_observation != expected_observation:
            raise StoreIntegrityError(
                "restore candidate does not match expected result"
            )
        connection: sqlite3.Connection | None = None
        connection_owner: _ConnectionCleanupOwner | None = None
        body_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            _, image = _read_image_fd(candidate_fd, label="SQLite restore candidate")
            connection = sqlite3.connect(
                ":memory:",
                uri=False,
                timeout=0,
                isolation_level=None,
            )
            connection_owner = _ConnectionCleanupOwner(
                connection,
                "SQLite restore candidate verification",
            )
            connection.row_factory = sqlite3.Row
            connection.deserialize(_memory_image(image))
            restore_event_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM transition_events WHERE kind = 'restore'"
                ).fetchone()[0]
            )
        except StoreError as exc:
            body_error = exc
        except sqlite3.DatabaseError as exc:
            error = StoreIntegrityError("SQLite restore candidate verification failed")
            _adopt_cleanup_capability(error, exc)
            body_error = error
        except (TypeError, ValueError, OverflowError) as exc:
            error = StoreIntegrityError(
                "SQLite restore candidate verification is invalid"
            )
            _adopt_cleanup_capability(error, exc)
            body_error = error
        except _CLEANUP_EXCEPTION as exc:
            body_error = exc
        finally:
            if connection_owner is not None:
                try:
                    connection_owner.retry_cleanup()
                except _CLEANUP_EXCEPTION as exc:
                    cleanup_error = exc
        if body_error is not None:
            if cleanup_error is not None:
                assert connection_owner is not None
                _raise_with_cleanup_capability(
                    body_error,
                    _CleanupCapability(connection_owner.retry_cleanup),
                )
            if connection_owner is None:
                raise body_error
            attached = _attach_cleanup_capability(
                body_error,
                _CleanupCapability(connection_owner.retry_cleanup),
            )
            if attached is not body_error:
                raise attached from body_error
            raise body_error
        if cleanup_error is not None:
            assert connection_owner is not None
            _raise_with_cleanup_capability(
                cleanup_error,
                _CleanupCapability(connection_owner.retry_cleanup),
            )
        if restore_event_count != expected.restore_event_count:
            raise StoreIntegrityError("restore candidate event evidence mismatches")
        return _issue_restore_apply_result(
            observation=actual_observation,
            tombstones=expected.tombstones,
            active_tombstones=expected.active_tombstones,
            restore_event_count=restore_event_count,
        )

    @staticmethod
    def _read_image_events(
        image: bytes,
        *,
        label: str,
    ) -> tuple[TransitionEvent, ...]:
        """Read only typed journal observations from an immutable image."""

        connection: sqlite3.Connection | None = None
        connection_owner: _ConnectionCleanupOwner | None = None
        body_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        events: tuple[TransitionEvent, ...] | None = None
        try:
            connection = sqlite3.connect(
                ":memory:",
                uri=False,
                timeout=0,
                isolation_level=None,
            )
            connection_owner = _ConnectionCleanupOwner(
                connection,
                f"SQLite {label} events",
            )
            connection.row_factory = sqlite3.Row
            connection.deserialize(_memory_image(image))
            _validate_existing_schema(connection)
            rows = connection.execute(
                """
                SELECT event_id, event_schema_version, operation_id, attempt,
                       from_status, to_status, kind, actor, clock_ns,
                       reason_code, evidence_ref
                FROM transition_events
                ORDER BY event_id
                """
            ).fetchall()
            events = tuple(CoordinationStore._event_from_row(row) for row in rows)
        except StoreError as exc:
            body_error = exc
        except sqlite3.DatabaseError as exc:
            error = StoreIntegrityError(f"SQLite {label} events are invalid")
            _adopt_cleanup_capability(error, exc)
            body_error = error
        except (TypeError, ValueError, OverflowError) as exc:
            error = StoreIntegrityError(f"SQLite {label} event data is invalid")
            _adopt_cleanup_capability(error, exc)
            body_error = error
        except _CLEANUP_EXCEPTION as exc:
            body_error = exc
        finally:
            if connection_owner is not None:
                try:
                    connection_owner.retry_cleanup()
                except _CLEANUP_EXCEPTION as exc:
                    cleanup_error = exc
        if body_error is not None:
            if cleanup_error is not None:
                assert connection_owner is not None
                _raise_with_cleanup_capability(
                    body_error,
                    _CleanupCapability(connection_owner.retry_cleanup),
                )
            if connection_owner is None:
                raise body_error
            attached = _attach_cleanup_capability(
                body_error,
                _CleanupCapability(connection_owner.retry_cleanup),
            )
            if attached is not body_error:
                raise attached from body_error
            raise body_error
        if cleanup_error is not None:
            assert connection_owner is not None
            _raise_with_cleanup_capability(
                cleanup_error,
                _CleanupCapability(connection_owner.retry_cleanup),
            )
        assert events is not None
        return events

    @staticmethod
    def _verify_restore_projection(
        source_observation: StoreImageObservation,
        candidate_observation: StoreImageObservation,
        source_events: tuple[TransitionEvent, ...],
        candidate_events: tuple[TransitionEvent, ...],
        *,
        final_floor: RecoveryFloor,
        actor: str,
        evidence_ref: str | None,
        expected_event_count: int | None,
        active_tombstones: tuple[RestoreIdentity, ...] | None = None,
        pre_rebind_fencing_token_floor: int | None = None,
    ) -> int:
        """Verify status, lease, receipt, and journal transforms independently."""

        if any(
            snapshot.status == "RESTORE_INCOMPLETE"
            for snapshot in source_observation.operations
        ):
            raise StoreIntegrityError("restore source contains incomplete state")
        if final_floor.recovery_epoch <= source_observation.floor.recovery_epoch:
            raise StoreIntegrityError("restore candidate epoch is not above source")
        if candidate_observation.max_fencing_token > final_floor.fencing_token_floor:
            raise StoreIntegrityError("restore candidate token exceeds evidence floor")
        expected_tokens: dict[tuple[str, str], int | None] = {}
        if pre_rebind_fencing_token_floor is not None:
            expected_plan = _restore_expected_fencing_tokens(
                source_observation.operations,
                pre_rebind_fencing_token_floor=pre_rebind_fencing_token_floor,
            )
            expected_tokens = {
                (operation_id, effect_key): expected_token
                for operation_id, effect_key, expected_token in expected_plan
            }
            receipted_count = sum(
                operation.status == "RECEIPTED"
                for operation in source_observation.operations
            )
            if final_floor.fencing_token_floor != (
                pre_rebind_fencing_token_floor + receipted_count
            ):
                raise StoreIntegrityError("restore final fencing floor is not exact")
        source_keys = tuple(
            (identity.operation_id, identity.effect_key)
            for identity in source_observation.identities
        )
        candidate_keys = tuple(
            (identity.operation_id, identity.effect_key)
            for identity in candidate_observation.identities
        )
        if candidate_keys != source_keys:
            raise StoreIntegrityError("restore candidate operation identities changed")
        candidate_by_key = {
            (identity.operation_id, identity.effect_key): snapshot
            for identity, snapshot in zip(
                candidate_observation.identities,
                candidate_observation.operations,
                strict=True,
            )
        }
        for source in source_observation.operations:
            candidate = candidate_by_key[(source.operation_id, source.effect_key)]
            if source.status != candidate.status:
                raise StoreIntegrityError("restore candidate status changed")
            if source.status == "CLEANED":
                if candidate != source:
                    raise StoreIntegrityError("restore CLEANED tombstone was modified")
                continue
            if candidate.recovery_epoch != final_floor.recovery_epoch:
                raise StoreIntegrityError("restore candidate epoch is not rebased")
            for field_name in (
                "operation_id",
                "effect_key",
                "provider_id",
                "status",
                "current_attempt",
                "owner",
                "lease_heartbeat_ns",
                "lease_expires_ns",
                "fence_proof_version",
                "fence_proof_ref",
                "effect_started_ns",
                "fence_started_ns",
            ):
                if getattr(candidate, field_name) != getattr(source, field_name):
                    raise StoreIntegrityError(
                        "restore candidate operation identity changed"
                    )
            if source.status == "RECEIPTED":
                source_receipt = source.verified_receipt_identity
                candidate_receipt = candidate.verified_receipt_identity
                if source_receipt is None or candidate_receipt is None:
                    raise StoreIntegrityError("restore receipt rebind is incomplete")
                if candidate.lease_epoch != final_floor.recovery_epoch:
                    raise StoreIntegrityError("restore receipt epoch is not rebased")
                expected_token = expected_tokens.get(
                    (source.operation_id, source.effect_key)
                )
                if (
                    expected_token is not None
                    and candidate.fencing_token != expected_token
                ):
                    raise StoreIntegrityError("restore receipt token is not exact")
                if candidate.fencing_token <= source.fencing_token:
                    raise StoreIntegrityError("restore receipt token was not rebound")
                for field_name in (
                    "operation_id",
                    "effect_key",
                    "provider_id",
                    "owner",
                    "attempt",
                    "provider_effect_id",
                    "provider_status",
                    "proof_version",
                    "proof_ref",
                ):
                    if getattr(candidate_receipt, field_name) != getattr(
                        source_receipt,
                        field_name,
                    ):
                        raise StoreIntegrityError("restore receipt identity changed")
                if (
                    candidate_receipt.lease_epoch != candidate.lease_epoch
                    or candidate_receipt.fencing_token != candidate.fencing_token
                ):
                    raise StoreIntegrityError("restore receipt lease is inconsistent")
            elif (
                candidate.lease_epoch != source.lease_epoch
                or candidate.fencing_token != source.fencing_token
                or candidate.verified_receipt_identity
                != source.verified_receipt_identity
            ):
                raise StoreIntegrityError("restore candidate lease identity changed")

        if candidate_events[: len(source_events)] != source_events:
            raise StoreIntegrityError("restore candidate journal prefix changed")
        rebased_operations = tuple(
            snapshot
            for snapshot in source_observation.operations
            if snapshot.status != "CLEANED"
        )
        restore_events = candidate_events[len(source_events) :]
        if len(restore_events) != len(rebased_operations):
            raise StoreIntegrityError("restore candidate event count is incomplete")
        if not restore_events and active_tombstones:
            raise StoreIntegrityError(
                "restore tombstones have no operation event anchor"
            )
        source_event_sequence = source_events[-1].sequence if source_events else 0
        for offset, (snapshot, event) in enumerate(
            zip(rebased_operations, restore_events, strict=True),
            start=1,
        ):
            if (
                event.sequence != source_event_sequence + offset
                or event.operation_id != snapshot.operation_id
                or event.attempt != snapshot.current_attempt
                or event.from_status != snapshot.status
                or event.to_status != snapshot.status
                or event.kind != "restore"
                or event.actor != actor
                or event.reason_code != "restore"
                or event.evidence_ref != evidence_ref
            ):
                raise StoreIntegrityError("restore candidate event evidence is invalid")
            candidate_snapshot = candidate_by_key[
                (
                    snapshot.operation_id,
                    snapshot.effect_key,
                )
            ]
            if event.clock_ns != candidate_snapshot.updated_ns:
                raise StoreIntegrityError("restore candidate event clock is invalid")
        restore_event_count = sum(event.kind == "restore" for event in candidate_events)
        if (
            expected_event_count is not None
            and restore_event_count != expected_event_count
        ):
            raise StoreIntegrityError(
                "restore candidate event count mismatches evidence"
            )
        if candidate_observation.last_clock_ns < source_observation.last_clock_ns:
            raise ClockRollbackError("restore candidate clock moved backwards")
        if restore_events and candidate_observation.last_clock_ns < max(
            event.clock_ns for event in restore_events
        ):
            raise ClockRollbackError("restore candidate clock does not cover events")
        return restore_event_count

    @staticmethod
    def _verify_candidate_from_evidence(
        source_fd: int,
        destination_fd: int,
        candidate_fd: int,
        evidence: RestoreCandidateEvidence,
    ) -> RestoreApplyResult:
        """Verify a candidate from durable evidence without a prior result."""

        if type(evidence) is not RestoreCandidateEvidence:
            raise StoreIntegrityError(
                "restore candidate evidence has an unsupported type"
            )
        try:
            evidence = RestoreCandidateEvidence(
                restore_generation=evidence.restore_generation,
                source_digest=evidence.source_digest,
                previous_primary_digest=evidence.previous_primary_digest,
                candidate_digest=evidence.candidate_digest,
                final_floor=evidence.final_floor,
                tombstones=evidence.tombstones,
                active_tombstones=evidence.active_tombstones,
                actor=evidence.actor,
                evidence_ref=evidence.evidence_ref,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise StoreIntegrityError("restore candidate evidence is invalid") from exc
        source_metadata, source_image = _read_image_fd(
            source_fd,
            label="SQLite restore source",
        )
        destination_metadata, destination_image = _read_image_fd(
            destination_fd,
            label="SQLite restore destination",
        )
        candidate_metadata, candidate_image = _read_image_fd(
            candidate_fd,
            label="SQLite restore candidate",
        )
        image_ids = {
            _identity(source_metadata),
            _identity(destination_metadata),
            _identity(candidate_metadata),
        }
        if len(image_ids) != 3:
            raise LeaseConflictError("restore verification images alias")
        source_observation = CoordinationStore._inspect_image_bytes(
            source_metadata,
            source_image,
            label="restore source",
        )
        destination_observation = CoordinationStore._inspect_image_bytes(
            destination_metadata,
            destination_image,
            label="restore destination",
        )
        if source_observation.digest != evidence.source_digest:
            raise StoreIntegrityError("restore source evidence mismatches image")
        if destination_observation.digest != evidence.previous_primary_digest:
            raise StoreIntegrityError("restore destination evidence mismatches image")
        candidate_observation = CoordinationStore._inspect_image_bytes(
            candidate_metadata,
            candidate_image,
            label="restore candidate",
        )
        if candidate_observation.digest != evidence.candidate_digest:
            raise StoreIntegrityError("restore candidate digest mismatches evidence")
        if candidate_observation.floor != evidence.final_floor:
            raise StoreIntegrityError("restore candidate floor mismatches evidence")
        if evidence.final_floor.recovery_epoch <= max(
            source_observation.floor.recovery_epoch,
            destination_observation.floor.recovery_epoch,
        ):
            raise StoreIntegrityError("restore candidate epoch is not above inputs")
        if evidence.final_floor.fencing_token_floor <= max(
            source_observation.floor.fencing_token_floor,
            source_observation.max_fencing_token,
            destination_observation.floor.fencing_token_floor,
            destination_observation.max_fencing_token,
        ):
            raise StoreIntegrityError(
                "restore candidate token floor is not above inputs"
            )
        if candidate_observation.last_clock_ns < max(
            source_observation.last_clock_ns,
            destination_observation.last_clock_ns,
        ):
            raise ClockRollbackError("restore candidate clock is behind destination")
        if (
            candidate_observation.max_fencing_token
            > evidence.final_floor.fencing_token_floor
        ):
            raise StoreIntegrityError("restore candidate token exceeds evidence floor")
        source_keys = tuple(
            (identity.operation_id, identity.effect_key)
            for identity in source_observation.identities
        )
        candidate_keys = tuple(
            (identity.operation_id, identity.effect_key)
            for identity in candidate_observation.identities
        )
        if candidate_keys != source_keys:
            raise StoreIntegrityError("restore candidate operation identities changed")
        if any(
            (identity.operation_id, identity.effect_key)
            in {
                (active.operation_id, active.effect_key)
                for active in evidence.active_tombstones
            }
            for identity in source_observation.identities
        ):
            raise StoreIntegrityError(
                "restore source contains a previously tombstoned identity"
            )
        destination_tombstones = tuple(
            identity
            for identity in destination_observation.identities
            if (identity.operation_id, identity.effect_key) not in set(source_keys)
        )
        if destination_tombstones != evidence.tombstones:
            raise StoreIntegrityError(
                "restore tombstone evidence mismatches destination"
            )
        receipted_count = sum(
            operation.status == "RECEIPTED"
            for operation in source_observation.operations
        )
        if evidence.final_floor.fencing_token_floor < receipted_count:
            raise StoreIntegrityError("restore candidate token floor is invalid")
        pre_rebind_fencing_token_floor = (
            evidence.final_floor.fencing_token_floor - receipted_count
        )
        input_hwm = max(
            source_observation.floor.fencing_token_floor,
            source_observation.max_fencing_token,
            destination_observation.floor.fencing_token_floor,
            destination_observation.max_fencing_token,
        )
        if pre_rebind_fencing_token_floor <= input_hwm:
            raise StoreIntegrityError(
                "restore candidate token reservation is not exact"
            )
        binding_ref = CoordinationStore._restore_binding_ref(
            restore_generation=evidence.restore_generation,
            actor=evidence.actor,
            audit_evidence_ref=evidence.evidence_ref,
            source_digest=evidence.source_digest,
            previous_primary_digest=evidence.previous_primary_digest,
            previous_recovery_epoch=destination_observation.floor.recovery_epoch,
            previous_fencing_token_hwm=max(
                destination_observation.floor.fencing_token_floor,
                destination_observation.max_fencing_token,
            ),
            previous_last_clock_ns=destination_observation.last_clock_ns,
            final_floor=evidence.final_floor,
            current_tombstones=evidence.tombstones,
            active_tombstones=evidence.active_tombstones,
        )
        source_events = CoordinationStore._read_image_events(
            source_image,
            label="restore source",
        )
        candidate_events = CoordinationStore._read_image_events(
            candidate_image,
            label="restore candidate",
        )
        restore_event_count = CoordinationStore._verify_restore_projection(
            source_observation,
            candidate_observation,
            source_events,
            candidate_events,
            final_floor=evidence.final_floor,
            actor=evidence.actor,
            evidence_ref=binding_ref,
            expected_event_count=None,
            active_tombstones=evidence.active_tombstones,
            pre_rebind_fencing_token_floor=pre_rebind_fencing_token_floor,
        )
        return _issue_restore_apply_result(
            observation=candidate_observation,
            tombstones=evidence.tombstones,
            active_tombstones=evidence.active_tombstones,
            restore_event_count=restore_event_count,
        )

    @staticmethod
    def _verify_replaced_evidence(
        source_fd: int,
        primary_fd: int,
        evidence: RestoreReplacedEvidence,
    ) -> RestoreApplyResult:
        """Verify the replaced primary without an old-destination descriptor."""

        if type(evidence) is not RestoreReplacedEvidence:
            raise StoreIntegrityError(
                "restore replaced evidence has an unsupported type"
            )
        try:
            evidence = RestoreReplacedEvidence(
                restore_generation=evidence.restore_generation,
                source_digest=evidence.source_digest,
                candidate_digest=evidence.candidate_digest,
                previous_primary_digest=evidence.previous_primary_digest,
                previous_recovery_epoch=evidence.previous_recovery_epoch,
                previous_fencing_token_hwm=evidence.previous_fencing_token_hwm,
                previous_last_clock_ns=evidence.previous_last_clock_ns,
                final_floor=evidence.final_floor,
                tombstones=evidence.tombstones,
                active_tombstones=evidence.active_tombstones,
                actor=evidence.actor,
                evidence_ref=evidence.evidence_ref,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise StoreIntegrityError("restore replaced evidence is invalid") from exc
        source_metadata, source_image = _read_image_fd(
            source_fd,
            label="SQLite restore source",
        )
        primary_metadata, primary_image = _read_image_fd(
            primary_fd,
            label="SQLite restore primary",
        )
        if _identity(source_metadata) == _identity(primary_metadata):
            raise LeaseConflictError("restore source and primary images alias")
        source_observation = CoordinationStore._inspect_image_bytes(
            source_metadata,
            source_image,
            label="restore source",
        )
        primary_observation = CoordinationStore._inspect_image_bytes(
            primary_metadata,
            primary_image,
            label="restore primary",
        )
        if source_observation.digest != evidence.source_digest:
            raise StoreIntegrityError("restore source evidence mismatches image")
        if primary_observation.digest != evidence.candidate_digest:
            raise StoreIntegrityError("restore primary digest mismatches evidence")
        if primary_observation.floor != evidence.final_floor:
            raise StoreIntegrityError("restore primary floor mismatches evidence")
        if evidence.final_floor.recovery_epoch <= max(
            source_observation.floor.recovery_epoch,
            evidence.previous_recovery_epoch,
        ):
            raise StoreIntegrityError(
                "restore primary epoch is not above prior destination"
            )
        if evidence.final_floor.fencing_token_floor <= max(
            source_observation.floor.fencing_token_floor,
            source_observation.max_fencing_token,
            evidence.previous_fencing_token_hwm,
        ):
            raise StoreIntegrityError(
                "restore primary token floor is not above prior destination"
            )
        receipted_count = sum(
            operation.status == "RECEIPTED"
            for operation in source_observation.operations
        )
        if evidence.final_floor.fencing_token_floor < receipted_count:
            raise StoreIntegrityError("restore primary token floor is invalid")
        pre_rebind_fencing_token_floor = (
            evidence.final_floor.fencing_token_floor - receipted_count
        )
        if pre_rebind_fencing_token_floor <= max(
            source_observation.floor.fencing_token_floor,
            source_observation.max_fencing_token,
            evidence.previous_fencing_token_hwm,
        ):
            raise StoreIntegrityError("restore primary token reservation is not exact")
        if primary_observation.last_clock_ns < max(
            source_observation.last_clock_ns,
            evidence.previous_last_clock_ns,
        ):
            raise ClockRollbackError(
                "restore primary clock is behind prior destination"
            )
        source_keys = {
            (identity.operation_id, identity.effect_key)
            for identity in source_observation.identities
        }
        if any(
            (identity.operation_id, identity.effect_key) in source_keys
            for identity in evidence.tombstones
        ):
            raise StoreIntegrityError("restore tombstone identity is not displaced")
        if any(
            (identity.operation_id, identity.effect_key) in source_keys
            for identity in evidence.active_tombstones
        ):
            raise StoreIntegrityError(
                "restore active tombstone identity is not displaced"
            )
        binding_ref = CoordinationStore._restore_binding_ref(
            restore_generation=evidence.restore_generation,
            actor=evidence.actor,
            audit_evidence_ref=evidence.evidence_ref,
            source_digest=evidence.source_digest,
            previous_primary_digest=evidence.previous_primary_digest,
            previous_recovery_epoch=evidence.previous_recovery_epoch,
            previous_fencing_token_hwm=evidence.previous_fencing_token_hwm,
            previous_last_clock_ns=evidence.previous_last_clock_ns,
            final_floor=evidence.final_floor,
            current_tombstones=evidence.tombstones,
            active_tombstones=evidence.active_tombstones,
        )
        restore_event_count = CoordinationStore._verify_restore_projection(
            source_observation,
            primary_observation,
            CoordinationStore._read_image_events(
                source_image,
                label="restore source",
            ),
            CoordinationStore._read_image_events(
                primary_image,
                label="restore primary",
            ),
            final_floor=evidence.final_floor,
            actor=evidence.actor,
            evidence_ref=binding_ref,
            expected_event_count=None,
            active_tombstones=evidence.active_tombstones,
            pre_rebind_fencing_token_floor=pre_rebind_fencing_token_floor,
        )
        return _issue_restore_apply_result(
            observation=primary_observation,
            tombstones=evidence.tombstones,
            active_tombstones=evidence.active_tombstones,
            restore_event_count=restore_event_count,
        )

    @staticmethod
    def _verify_candidate(
        candidate_fd: int,
        expected: RestoreApplyResult,
    ) -> RestoreApplyResult:
        """Package-private canonical name for the verify-only candidate seam."""

        return CoordinationStore._verify_candidate_applied(candidate_fd, expected)

    @staticmethod
    def _apply_candidate(
        source_fd: int,
        destination_fd: int,
        target_fd: int,
        *,
        source_observation: StoreImageObservation | None = None,
        destination_observation: StoreImageObservation | None = None,
        ledger_floor_lower_bound: RecoveryFloor,
        reservation: RecoveryFloorReservation | None = None,
        previous_active_tombstones: object,
        restore_generation: int,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RestoreApplyResult:
        """Package-private canonical name for the candidate apply seam."""

        return CoordinationStore._apply_restore_candidate(
            source_fd,
            destination_fd,
            target_fd,
            source_observation=source_observation,
            destination_observation=destination_observation,
            ledger_floor_lower_bound=ledger_floor_lower_bound,
            reservation=reservation,
            previous_active_tombstones=previous_active_tombstones,
            restore_generation=restore_generation,
            actor=actor,
            timestamp=timestamp,
            evidence_ref=evidence_ref,
        )

    def _advance_floor(
        self,
        reservation: RecoveryFloorReservation,
        *,
        now_ns: int | None = None,
    ) -> RecoveryFloor:
        """Atomically commit a store-issued recovery epoch/token floor."""

        timestamp = self._timestamp(now_ns)
        with self._lease_write_transaction() as connection:
            self._record_clock(
                connection,
                timestamp,
                strict=now_ns is not None or self._clock_injected,
            )
            return self._advance_floor_tx(connection, reservation)

    def _advance_floor_tx(
        self,
        connection: sqlite3.Connection,
        reservation: RecoveryFloorReservation,
    ) -> RecoveryFloor:
        """Apply a store-issued floor while an existing transaction is open."""

        if (
            not isinstance(reservation, RecoveryFloorReservation)
            or not reservation.is_issued
        ):
            raise LeaseConflictError("recovery floor reservation is invalid")
        current_epoch = self._metadata_integer(
            connection,
            "recovery_epoch",
            "recovery_epoch",
        )
        current_floor = self._metadata_integer(
            connection,
            "fencing_token_floor",
            "fencing_token_floor",
        )
        row = connection.execute(
            "SELECT MAX(fencing_token) AS maximum FROM operation_attempts"
        ).fetchone()
        maximum = 0 if row is None or row["maximum"] is None else row["maximum"]
        maximum = max(
            current_floor,
            _require_sqlite_integer(maximum, "fencing_token"),
        )
        if (
            reservation.recovery_epoch <= current_epoch
            or reservation.fencing_token_floor <= maximum
        ):
            raise LeaseConflictError("recovery floor reservation is stale")
        connection.execute(
            """
            UPDATE store_meta SET value = ?
            WHERE key = 'recovery_epoch' AND value = ?
            """,
            (reservation.recovery_epoch, current_epoch),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise LeaseConflictError("recovery epoch advance was lost")
        connection.execute(
            """
            UPDATE store_meta SET value = ?
            WHERE key = 'fencing_token_floor' AND value = ?
            """,
            (reservation.fencing_token_floor, current_floor),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise LeaseConflictError("fencing floor advance was lost")
        return RecoveryFloor(
            reservation.recovery_epoch,
            reservation.fencing_token_floor,
        )

    def _rebase_operation_tx(
        self,
        connection: sqlite3.Connection,
        snapshot: RecoverySnapshot,
        *,
        floor: RecoveryFloor,
        mode: RecoveryRebaseMode,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        """Rebase one known operation after a floor advance in this transaction."""

        mode = _require_rebase_mode(mode)
        if snapshot.status != mode:
            raise LeaseConflictError("rebase mode does not match operation status")
        current_epoch = self._metadata_integer(
            connection,
            "recovery_epoch",
            "recovery_epoch",
        )
        current_floor = self._metadata_integer(
            connection,
            "fencing_token_floor",
            "fencing_token_floor",
        )
        if (
            current_epoch != floor.recovery_epoch
            or current_floor < floor.fencing_token_floor
            or snapshot.recovery_epoch >= floor.recovery_epoch
        ):
            raise LeaseConflictError("rebase floor is stale or unavailable")
        row = self._assert_recovery_snapshot_identity(
            connection,
            snapshot,
            require_current_epoch=False,
        )
        self._validate_event_values(
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=row["status"],
            to_status=row["status"],
            kind="restore",
            actor=actor,
            timestamp=timestamp,
            reason_code="restore",
            evidence_ref=evidence_ref,
        )
        new_token: int | None = None
        if mode == "RECEIPTED":
            receipt = snapshot.verified_receipt_identity
            if receipt is None:
                raise StoreIntegrityError("SQLite receipted operation has no receipt")
            new_token = self._next_value(connection)
            connection.execute(
                """
                UPDATE operation_attempts
                SET lease_epoch = ?, fencing_token = ?
                WHERE operation_id = ? AND attempt = ?
                  AND owner = ? AND provider_id = ?
                  AND lease_epoch = ? AND fencing_token = ?
                """,
                (
                    floor.recovery_epoch,
                    new_token,
                    snapshot.operation_id,
                    snapshot.current_attempt,
                    snapshot.owner,
                    snapshot.provider_id,
                    snapshot.lease_epoch,
                    snapshot.fencing_token,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("receipt rebind was lost")
            connection.execute(
                """
                UPDATE effect_receipts
                SET lease_epoch = ?, fencing_token = ?
                WHERE operation_id = ? AND attempt = ?
                  AND effect_key = ? AND provider_effect_id = ?
                  AND provider_id = ? AND owner = ?
                  AND lease_epoch = ? AND fencing_token = ?
                  AND proof_version = ? AND proof_ref = ?
                """,
                (
                    floor.recovery_epoch,
                    new_token,
                    receipt.operation_id,
                    receipt.attempt,
                    receipt.effect_key,
                    receipt.provider_effect_id,
                    receipt.provider_id,
                    receipt.owner,
                    receipt.lease_epoch,
                    receipt.fencing_token,
                    receipt.proof_version,
                    receipt.proof_ref,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("receipt rebind was lost")
        connection.execute(
            """
            UPDATE operations
            SET recovery_epoch = ?, updated_ns = ?
            WHERE operation_id = ? AND current_attempt = ?
              AND status = ? AND recovery_epoch = ? AND updated_ns = ?
            """,
            (
                floor.recovery_epoch,
                timestamp,
                snapshot.operation_id,
                snapshot.current_attempt,
                snapshot.status,
                snapshot.recovery_epoch,
                snapshot.updated_ns,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise LeaseConflictError("operation rebase was lost")
        self._append_event(
            connection,
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=row["status"],
            to_status=row["status"],
            kind="restore",
            actor=actor,
            timestamp=timestamp,
            reason_code="restore",
            evidence_ref=evidence_ref,
        )
        return self._recovery_snapshot_tx(connection, snapshot.operation_id)

    @staticmethod
    def _validate_event_values(
        *,
        operation_id: str,
        attempt: int,
        from_status: str | None,
        to_status: str,
        kind: str,
        actor: str,
        timestamp: int,
        reason_code: str,
        evidence_ref: str | None = None,
    ) -> None:
        """Validate the complete event before any related state mutation."""

        _require_opaque_identifier(operation_id, "operation_id")
        _require_sqlite_integer(attempt, "attempt")
        _require_optional_status(from_status, "from_status")
        _require_status(to_status, "to_status")
        if type(kind) is not str or kind not in _VALID_EVENT_KINDS:
            raise ValueError("kind is unsupported")
        _require_opaque_identifier(actor, "actor")
        _require_sqlite_integer(timestamp, "clock_ns")
        _require_reason_code(reason_code)
        if (kind, reason_code) not in _EVENT_REASON_PAIRS:
            raise ValueError("event kind and reason_code are inconsistent")
        if evidence_ref is not None:
            _require_evidence_ref(evidence_ref)

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        attempt: int,
        from_status: str | None,
        to_status: str,
        kind: str,
        actor: str,
        timestamp: int,
        reason_code: str,
        evidence_ref: str | None = None,
    ) -> None:
        """Append one finite-vocabulary event inside the caller's transaction."""

        CoordinationStore._validate_event_values(
            operation_id=operation_id,
            attempt=attempt,
            from_status=from_status,
            to_status=to_status,
            kind=kind,
            actor=actor,
            timestamp=timestamp,
            reason_code=reason_code,
            evidence_ref=evidence_ref,
        )
        connection.execute(
            """
            INSERT INTO transition_events(
                event_schema_version, operation_id, attempt, from_status,
                to_status, kind, actor, clock_ns, reason_code, evidence_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                EVENT_SCHEMA_VERSION,
                operation_id,
                attempt,
                from_status,
                to_status,
                kind,
                actor,
                timestamp,
                reason_code,
                evidence_ref,
            ),
        )

    @staticmethod
    def _operation_snapshot_tx(
        connection: sqlite3.Connection, operation_id: str
    ) -> OperationSnapshot:
        row = connection.execute(
            """
            SELECT operation_id, effect_key, status, current_attempt,
                   provider_id, recovery_epoch, created_ns, updated_ns
            FROM operations WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            raise StoreIntegrityError("SQLite operation snapshot is unavailable")
        return CoordinationStore._operation_from_row(row)

    @staticmethod
    def _receipt_from_row(
        row: sqlite3.Row,
        receipt_row: sqlite3.Row,
    ) -> VerifiedProviderReceipt:
        try:
            provider_id = row["attempt_provider_id"]
            owner = row["owner"]
            if provider_id is None or owner is None:
                raise StoreIntegrityError("SQLite receipt lease identity is incomplete")
            proof_version = row["fence_proof_version"]
            proof_ref = row["fence_proof_ref"]
            if proof_version is None or proof_ref is None:
                raise StoreIntegrityError("SQLite receipt fence proof is incomplete")
            proof = ProviderFenceProof(
                operation_id=row["operation_id"],
                effect_key=row["effect_key"],
                provider_id=provider_id,
                owner=owner,
                attempt=row["attempt"],
                lease_epoch=row["lease_epoch"],
                fencing_token=row["fencing_token"],
                proof_version=proof_version,
                proof_ref=proof_ref,
            )
            effect = _issue_provider_effect(
                operation_id=row["operation_id"],
                effect_key=row["effect_key"],
                provider_id=provider_id,
                owner=owner,
                attempt=row["attempt"],
                lease_epoch=row["lease_epoch"],
                fencing_token=row["fencing_token"],
                fence_proof=proof,
            )
            status = ProviderStatus(
                operation_id=row["operation_id"],
                effect_key=receipt_row["effect_key"],
                provider_id=receipt_row["provider_id"],
                owner=receipt_row["owner"],
                attempt=receipt_row["attempt"],
                lease_epoch=receipt_row["lease_epoch"],
                fencing_token=receipt_row["fencing_token"],
                provider_effect_id=receipt_row["provider_effect_id"],
                status=receipt_row["provider_status"],
                consistency="STRONG",
                proof_version=receipt_row["proof_version"],
                proof_ref=receipt_row["proof_ref"],
            )
            return _verified_receipt_from_status(effect, proof, status)
        except StoreError:
            raise
        except (TypeError, ValueError, ProviderProofError, ProviderReceiptError) as exc:
            raise StoreIntegrityError("SQLite provider receipt is invalid") from exc

    @staticmethod
    def _effect_from_attempt_row(row: sqlite3.Row) -> ProviderEffect:
        try:
            provider_id = row["attempt_provider_id"]
            owner = row["owner"]
            proof_version = row["fence_proof_version"]
            proof_ref = row["fence_proof_ref"]
            if provider_id is None or owner is None:
                raise StoreIntegrityError("SQLite effect identity is incomplete")
            if proof_version is None or proof_ref is None:
                raise StoreIntegrityError("SQLite effect fence proof is incomplete")
            proof = ProviderFenceProof(
                operation_id=row["operation_id"],
                effect_key=row["effect_key"],
                provider_id=provider_id,
                owner=owner,
                attempt=row["attempt"],
                lease_epoch=row["lease_epoch"],
                fencing_token=row["fencing_token"],
                proof_version=proof_version,
                proof_ref=proof_ref,
            )
            return _issue_provider_effect(
                operation_id=row["operation_id"],
                effect_key=row["effect_key"],
                provider_id=provider_id,
                owner=owner,
                attempt=row["attempt"],
                lease_epoch=row["lease_epoch"],
                fencing_token=row["fencing_token"],
                fence_proof=proof,
            )
        except StoreError:
            raise
        except (TypeError, ValueError, ProviderProofError, ProviderReceiptError) as exc:
            raise StoreIntegrityError("SQLite provider effect is invalid") from exc

    def _recovery_snapshot_tx(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> RecoverySnapshot:
        snapshot = _read_existing_recovery_snapshot(connection, operation_id)
        if snapshot is None:
            raise LeaseConflictError("operation is unavailable")
        return snapshot

    def _rehydrate_claim_tx(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> Claim | None:
        operation_id = _require_opaque_identifier(operation_id, "operation_id")
        try:
            row = self._fetch_attempt(connection, operation_id)
            if row is None or row["status"] not in {"FENCE_PENDING", "CLAIMED"}:
                return None
            return self._claim_from_row(row)
        except StoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError) as exc:
            _raise_recovery_read_error(exc, kind="claim")

    def _rehydrate_effect_tx(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> ProviderEffect | None:
        operation_id = _require_opaque_identifier(operation_id, "operation_id")
        try:
            row = self._fetch_attempt(connection, operation_id)
            if row is None or row["status"] != "EFFECT_PREPARED":
                return None
            return self._effect_from_attempt_row(row)
        except StoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError) as exc:
            _raise_recovery_read_error(exc, kind="effect")

    def _rehydrate_recovery_effect_tx(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> ProviderEffect | None:
        """Rehydrate identity-only effect evidence for unknown recovery."""

        operation_id = _require_opaque_identifier(operation_id, "operation_id")
        try:
            row = self._fetch_attempt(connection, operation_id)
            if row is None or row["status"] not in {
                "UNKNOWN_EFFECT",
                "UNKNOWN",
            }:
                return None
            if row["fence_proof_version"] is None or row["fence_proof_ref"] is None:
                return None
            return self._effect_from_attempt_row(row)
        except StoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError) as exc:
            _raise_recovery_read_error(exc, kind="recovery effect")

    def _rehydrate_receipt_tx(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> VerifiedProviderReceipt | None:
        snapshot = self._recovery_snapshot_tx(connection, operation_id)
        return snapshot.verified_receipt_identity

    def _assert_recovery_snapshot_identity(
        self,
        connection: sqlite3.Connection,
        snapshot: RecoverySnapshot,
        *,
        require_current_epoch: bool = True,
    ) -> sqlite3.Row:
        row = self._fetch_attempt(
            connection,
            snapshot.operation_id,
            snapshot.current_attempt,
        )
        if row is None:
            raise LeaseConflictError("recovery operation is unavailable")
        if require_current_epoch:
            self._require_current_epoch(connection, row["recovery_epoch"])
        if (
            snapshot.current_attempt >= 1
            and row["operation_provider_id"] != row["attempt_provider_id"]
        ):
            raise StoreIntegrityError(
                "SQLite recovery provider identity is inconsistent"
            )
        expected_provider = (
            row["operation_provider_id"]
            if snapshot.current_attempt == 0
            else row["attempt_provider_id"]
        )
        expected = (
            row["operation_id"],
            row["effect_key"],
            expected_provider,
            row["owner"],
            row["attempt"],
            row["recovery_epoch"],
            row["lease_epoch"],
            row["fencing_token"],
            row["status"],
            row["updated_ns"],
            row["lease_heartbeat_ns"],
            row["lease_expires_ns"],
            row["fence_proof_version"],
            row["fence_proof_ref"],
            row["effect_started_ns"],
            row["fence_started_ns"],
        )
        actual = (
            snapshot.operation_id,
            snapshot.effect_key,
            snapshot.provider_id,
            snapshot.owner,
            snapshot.current_attempt,
            snapshot.recovery_epoch,
            snapshot.lease_epoch,
            snapshot.fencing_token,
            snapshot.status,
            snapshot.updated_ns,
            snapshot.lease_heartbeat_ns,
            snapshot.lease_expires_ns,
            snapshot.fence_proof_version,
            snapshot.fence_proof_ref,
            snapshot.effect_started_ns,
            snapshot.fence_started_ns,
        )
        if expected != actual:
            raise LeaseConflictError("recovery snapshot identity is stale")
        receipt_row = connection.execute(
            """
            SELECT operation_id, attempt, effect_key, provider_effect_id,
                   provider_status, provider_id, owner, fencing_token,
                   lease_epoch, received_ns, proof_version, proof_ref
            FROM effect_receipts
            WHERE operation_id = ? AND attempt = ?
            """,
            (snapshot.operation_id, snapshot.current_attempt),
        ).fetchone()
        if receipt_row is None:
            if snapshot.verified_receipt_identity is not None:
                raise LeaseConflictError("recovery receipt identity is stale")
        else:
            if snapshot.verified_receipt_identity is None:
                raise LeaseConflictError("recovery receipt identity is stale")
            stored = self._receipt_from_row(row, receipt_row)
            if stored != snapshot.verified_receipt_identity:
                raise LeaseConflictError("recovery receipt identity is stale")
        return row

    def _mark_recovery_unknown_tx(
        self,
        connection: sqlite3.Connection,
        snapshot: RecoverySnapshot,
        *,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        row = self._assert_recovery_snapshot_identity(connection, snapshot)
        if row["status"] not in {
            "FENCE_RESERVATION_STARTED",
            "EFFECT_PREPARED",
        }:
            raise LeaseConflictError("operation is not a prepared recovery marker")
        updated_ns = _require_sqlite_integer(timestamp, "clock_ns")
        self._validate_event_values(
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=row["status"],
            to_status="UNKNOWN_EFFECT",
            kind="unknown_effect",
            actor=actor,
            timestamp=updated_ns,
            reason_code="effect_unknown",
            evidence_ref=evidence_ref,
        )
        connection.execute(
            """
            UPDATE operations SET status = ?, updated_ns = ?
            WHERE operation_id = ? AND current_attempt = ? AND status = ?
              AND recovery_epoch = ? AND updated_ns = ?
            """,
            (
                "UNKNOWN_EFFECT",
                updated_ns,
                snapshot.operation_id,
                snapshot.current_attempt,
                snapshot.status,
                snapshot.recovery_epoch,
                snapshot.updated_ns,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise LeaseConflictError("recovery transition was lost")
        self._append_event(
            connection,
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=row["status"],
            to_status="UNKNOWN_EFFECT",
            kind="unknown_effect",
            actor=actor,
            timestamp=updated_ns,
            reason_code="effect_unknown",
            evidence_ref=evidence_ref,
        )
        return self._recovery_snapshot_tx(connection, snapshot.operation_id)

    def _recover_expired_tx(
        self,
        connection: sqlite3.Connection,
        snapshot: RecoverySnapshot,
        *,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        """Move one expired lease to the fail-closed unknown state.

        The caller owns the clock and event validation.  This helper only
        performs the identity-checked row/event transition while the private
        recovery transaction is open.
        """

        row = self._assert_recovery_snapshot_identity(connection, snapshot)
        status = row["status"]
        if status not in {"FENCE_PENDING", "CLAIMED"}:
            raise LeaseConflictError("operation is not an expiring lease")
        expiry = row["lease_expires_ns"]
        if type(expiry) is not int:
            raise StoreIntegrityError("SQLite lease expiry is invalid")
        if timestamp < expiry:
            raise LeaseConflictError("lease has not expired")
        timestamp = _require_sqlite_integer(timestamp, "clock_ns")
        self._fault("before_recovery_transition")
        if status == "FENCE_PENDING" and row["fence_started_ns"] is None:
            current_attempt = _require_sqlite_integer(
                row["current_attempt"],
                "attempt",
                minimum=1,
            )
            attempt_row = connection.execute(
                "SELECT MAX(attempt) AS maximum FROM operation_attempts "
                "WHERE operation_id = ?",
                (snapshot.operation_id,),
            ).fetchone()
            maximum_attempt = (
                current_attempt
                if attempt_row is None or attempt_row["maximum"] is None
                else _require_sqlite_integer(attempt_row["maximum"], "attempt")
            )
            if maximum_attempt >= SQLITE_INTEGER_MAX:
                raise ValueError("attempt exceeds supported integer")
            next_attempt = maximum_attempt + 1
            heartbeat = row["lease_heartbeat_ns"]
            if type(heartbeat) is not int or expiry <= heartbeat:
                raise StoreIntegrityError("SQLite lease duration is invalid")
            lease_ttl = expiry - heartbeat
            next_expiry = self._lease_expiry(timestamp, lease_ttl)
            provider_id = row["attempt_provider_id"]
            owner = row["owner"]
            if provider_id is None or owner is None:
                raise StoreIntegrityError("SQLite pending lease identity is incomplete")
            fencing_token = self._next_value(connection)
            connection.execute(
                """
                UPDATE operations
                SET status = 'FENCE_PENDING', current_attempt = ?, updated_ns = ?
                WHERE operation_id = ? AND current_attempt = ?
                  AND status = ? AND recovery_epoch = ? AND updated_ns = ?
                """,
                (
                    next_attempt,
                    timestamp,
                    snapshot.operation_id,
                    snapshot.current_attempt,
                    status,
                    snapshot.recovery_epoch,
                    snapshot.updated_ns,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("expired pending recovery was lost")
            connection.execute(
                """
                INSERT INTO operation_attempts(
                    operation_id, attempt, owner, provider_id,
                    lease_epoch, fencing_token, lease_heartbeat_ns,
                    lease_expires_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.operation_id,
                    next_attempt,
                    owner,
                    provider_id,
                    row["recovery_epoch"],
                    fencing_token,
                    timestamp,
                    next_expiry,
                ),
            )
            self._fault("after_recovery_row")
            self._fault("before_recovery_event")
            self._append_event(
                connection,
                operation_id=snapshot.operation_id,
                attempt=next_attempt,
                from_status=status,
                to_status="FENCE_PENDING",
                kind="recover",
                actor=actor,
                timestamp=timestamp,
                reason_code="recover",
                evidence_ref=evidence_ref,
            )
            self._fault("after_recovery_event")
            return self._recovery_snapshot_tx(connection, snapshot.operation_id)
        connection.execute(
            """
            UPDATE operations
            SET status = 'UNKNOWN_EFFECT', updated_ns = ?
            WHERE operation_id = ? AND current_attempt = ? AND status = ?
              AND recovery_epoch = ? AND updated_ns = ?
            """,
            (
                timestamp,
                snapshot.operation_id,
                snapshot.current_attempt,
                status,
                snapshot.recovery_epoch,
                snapshot.updated_ns,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise LeaseConflictError("expired recovery transition was lost")
        self._fault("after_recovery_row")
        self._fault("before_recovery_event")
        self._append_event(
            connection,
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=status,
            to_status="UNKNOWN_EFFECT",
            kind="recover",
            actor=actor,
            timestamp=timestamp,
            reason_code="recover",
            evidence_ref=evidence_ref,
        )
        self._fault("after_recovery_event")
        return self._recovery_snapshot_tx(connection, snapshot.operation_id)

    def _force_recover_tx(
        self,
        connection: sqlite3.Connection,
        snapshot: RecoverySnapshot,
        *,
        floor: RecoveryFloor,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        """Fence one operation under an already advanced recovery floor."""

        if snapshot.status == "CLEANED":
            raise LeaseConflictError("cleaned operation is immutable")
        row = self._assert_recovery_snapshot_identity(
            connection,
            snapshot,
            require_current_epoch=False,
        )
        current_epoch = self._metadata_integer(
            connection,
            "recovery_epoch",
            "recovery_epoch",
        )
        if current_epoch != floor.recovery_epoch:
            raise LeaseConflictError("force recovery floor is stale")
        self._fault("before_recovery_transition")
        status = row["status"]
        if status in {"INTENT", "RECEIPTED", "COMPLETED"}:
            rebased = self._rebase_operation_tx(
                connection,
                snapshot,
                floor=floor,
                mode=cast(RecoveryRebaseMode, status),
                actor=actor,
                timestamp=timestamp,
                evidence_ref=evidence_ref,
            )
            self._fault("after_recovery_row")
            self._fault("before_recovery_event")
            self._append_event(
                connection,
                operation_id=rebased.operation_id,
                attempt=rebased.current_attempt,
                from_status=rebased.status,
                to_status=rebased.status,
                kind="force_recover",
                actor=actor,
                timestamp=timestamp,
                reason_code="force_recover",
                evidence_ref=evidence_ref,
            )
            self._fault("after_recovery_event")
            return rebased
        to_status = (
            "UNKNOWN_EFFECT"
            if status
            in {
                "FENCE_PENDING",
                "FENCE_RESERVATION_STARTED",
                "CLAIMED",
                "EFFECT_PREPARED",
            }
            else status
        )
        connection.execute(
            """
            UPDATE operations
            SET status = ?, recovery_epoch = ?, updated_ns = ?
            WHERE operation_id = ? AND current_attempt = ? AND status = ?
              AND recovery_epoch = ? AND updated_ns = ?
            """,
            (
                to_status,
                floor.recovery_epoch,
                timestamp,
                snapshot.operation_id,
                snapshot.current_attempt,
                status,
                snapshot.recovery_epoch,
                snapshot.updated_ns,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise LeaseConflictError("force recovery transition was lost")
        self._fault("after_recovery_row")
        self._fault("before_recovery_event")
        self._append_event(
            connection,
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=status,
            to_status=to_status,
            kind="force_recover",
            actor=actor,
            timestamp=timestamp,
            reason_code="force_recover",
            evidence_ref=evidence_ref,
        )
        self._fault("after_recovery_event")
        return self._recovery_snapshot_tx(connection, snapshot.operation_id)

    def _resolve_unknown_absent_tx(
        self,
        connection: sqlite3.Connection,
        snapshot: RecoverySnapshot,
        *,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        """Return a proven-absent operation to its original intent state."""

        row = self._assert_recovery_snapshot_identity(connection, snapshot)
        if row["status"] not in {"UNKNOWN_EFFECT", "UNKNOWN"}:
            raise LeaseConflictError("operation is not unknown")
        self._fault("before_recovery_transition")
        connection.execute(
            """
            UPDATE operations
            SET status = 'INTENT', current_attempt = 0, updated_ns = ?
            WHERE operation_id = ? AND current_attempt = ? AND status = ?
              AND recovery_epoch = ? AND updated_ns = ?
            """,
            (
                timestamp,
                snapshot.operation_id,
                snapshot.current_attempt,
                row["status"],
                snapshot.recovery_epoch,
                snapshot.updated_ns,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise LeaseConflictError("unknown resolution transition was lost")
        self._fault("after_recovery_row")
        self._fault("before_recovery_event")
        self._append_event(
            connection,
            operation_id=snapshot.operation_id,
            attempt=0,
            from_status=row["status"],
            to_status="INTENT",
            kind="resolve_unknown",
            actor=actor,
            timestamp=timestamp,
            reason_code="resolve_unknown",
            evidence_ref=evidence_ref,
        )
        self._fault("after_recovery_event")
        return self._recovery_snapshot_tx(connection, snapshot.operation_id)

    def _resolve_unknown_completed_tx(
        self,
        connection: sqlite3.Connection,
        snapshot: RecoverySnapshot,
        receipt: VerifiedProviderReceipt,
        *,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        """Persist a verified completed provider status without executing it."""

        row = self._assert_recovery_snapshot_identity(connection, snapshot)
        if row["status"] not in {"UNKNOWN_EFFECT", "UNKNOWN"}:
            raise LeaseConflictError("operation is not unknown")
        effect = self._effect_from_attempt_row(row)
        self._validate_receipt(effect, receipt)
        self._fault("before_recovery_transition")
        connection.execute(
            """
            INSERT INTO effect_receipts(
                operation_id, attempt, effect_key, provider_effect_id,
                provider_status, provider_id, owner, fencing_token,
                lease_epoch, received_ns, proof_version, proof_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.operation_id,
                receipt.attempt,
                receipt.effect_key,
                receipt.provider_effect_id,
                receipt.provider_status,
                receipt.provider_id,
                receipt.owner,
                receipt.fencing_token,
                receipt.lease_epoch,
                timestamp,
                receipt.proof_version,
                receipt.proof_ref,
            ),
        )
        connection.execute(
            """
            UPDATE operations
            SET status = 'RECEIPTED', updated_ns = ?
            WHERE operation_id = ? AND current_attempt = ? AND status = ?
              AND recovery_epoch = ? AND updated_ns = ?
            """,
            (
                timestamp,
                snapshot.operation_id,
                snapshot.current_attempt,
                row["status"],
                snapshot.recovery_epoch,
                snapshot.updated_ns,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise LeaseConflictError("unknown receipt transition was lost")
        self._fault("after_recovery_row")
        self._fault("before_recovery_event")
        self._append_event(
            connection,
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=row["status"],
            to_status="RECEIPTED",
            kind="resolve_unknown",
            actor=actor,
            timestamp=timestamp,
            reason_code="resolve_unknown",
            evidence_ref=evidence_ref,
        )
        self._fault("after_recovery_event")
        return self._recovery_snapshot_tx(connection, snapshot.operation_id)

    def _append_recovery_event_tx(
        self,
        connection: sqlite3.Connection,
        snapshot: RecoverySnapshot,
        *,
        kind: str,
        reason_code: str,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        row = self._assert_recovery_snapshot_identity(connection, snapshot)
        self._validate_event_values(
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=row["status"],
            to_status=row["status"],
            kind=kind,
            actor=actor,
            timestamp=timestamp,
            reason_code=reason_code,
            evidence_ref=evidence_ref,
        )
        self._append_event(
            connection,
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=row["status"],
            to_status=row["status"],
            kind=kind,
            actor=actor,
            timestamp=timestamp,
            reason_code=reason_code,
            evidence_ref=evidence_ref,
        )
        return snapshot

    def _validate_prepared_markers(self) -> None:
        """Validate recovery-required rows without changing their status."""

        connection = self._require_connection()
        try:
            rows = connection.execute(
                """
                SELECT o.operation_id, o.status
                FROM operations AS o
                JOIN operation_attempts AS a
                  ON a.operation_id = o.operation_id
                 AND a.attempt = o.current_attempt
                WHERE o.status IN ('EFFECT_PREPARED', 'FENCE_RESERVATION_STARTED')
                ORDER BY o.operation_id
                """
            ).fetchall()
            for row in rows:
                snapshot = self._recovery_snapshot_tx(
                    connection,
                    row["operation_id"],
                )
                if snapshot.status != row["status"]:
                    raise StoreIntegrityError("SQLite prepared state changed")
        except StoreError:
            raise
        except LeaseError as exc:
            raise StoreIntegrityError("SQLite prepared state is invalid") from exc
        except sqlite3.DatabaseError as exc:
            raise StoreIntegrityError("SQLite prepared state cannot be read") from exc
        except (TypeError, ValueError, OverflowError) as exc:
            raise StoreIntegrityError("SQLite prepared state is invalid") from exc

    def _require_connection(self) -> sqlite3.Connection:
        self._retry_orphan_fds()
        if self._lifetime_gate_cleanup_pending:
            raise StoreUnavailableError("coordination lifetime gate cleanup is pending")
        if self._connection_cleanup_pending:
            raise StoreUnavailableError("SQLite connection cleanup is pending")
        connection = self._connection
        if connection is None:
            raise StoreClosedError("coordination store is closed")
        self._assert_lifetime_gate()
        self._assert_state_root()
        self._assert_database_identity()
        self._assert_connection_identity()
        if self._marker_fd is not None:
            self._assert_marker_identity()
        self._existing_sidecar_names()
        return connection

    def _assert_state_root(self) -> None:
        root_fd = self._state_root_fd
        expected = self._state_root_identity
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        if expected is None:
            raise StoreClosedError("coordination store is closed")
        try:
            fd_metadata = os.fstat(root_fd)
            path_metadata = os.stat(self.state_root, follow_symlinks=False)
        except OSError as exc:
            raise StoreUnavailableError(
                "private SQLite state root is unavailable"
            ) from exc
        _validate_directory_fd(root_fd, state_root=True)
        if not stat.S_ISDIR(path_metadata.st_mode):
            raise StoreUnavailableError("private SQLite state root changed while open")
        if _identity(fd_metadata) != expected or _identity(path_metadata) != expected:
            raise StoreUnavailableError("private SQLite state root changed while open")

    def _database_uri(self) -> str:
        root_fd = self._state_root_fd
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        self._assert_state_root()
        if sys.platform == "darwin":
            # macOS devfs exposes /dev/fd/N for the descriptor itself, but
            # sqlite3 cannot resolve a child pathname below that pseudo-entry.
            # F_GETPATH derives the stable path from the held descriptor; the
            # root identity checks and mode=rw prevent a replacement from being
            # created or accepted between this lookup and SQLite open.
            try:
                raw_path = fcntl.fcntl(root_fd, fcntl.F_GETPATH, b"\0" * 1024)
            except OSError as exc:
                raise StoreUnavailableError(
                    "private SQLite directory anchor is unavailable"
                ) from exc
            if not isinstance(raw_path, bytes):
                raise StoreUnavailableError(
                    "private SQLite directory anchor is unavailable"
                )
            if b"\0" not in raw_path:
                raise StoreUnavailableError(
                    "private SQLite directory anchor is invalid"
                )
            root_path_bytes = raw_path.split(b"\0", 1)[0]
            try:
                anchored_root = Path(root_path_bytes.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise StoreUnavailableError(
                    "private SQLite directory anchor is invalid"
                ) from exc
            database_path = anchored_root / DATABASE_FILENAME
        else:
            proc_root = Path(f"/proc/self/fd/{root_fd}")
            if not proc_root.exists():
                raise StoreUnavailableError(
                    "private SQLite directory anchor is unavailable"
                )
            database_path = proc_root / DATABASE_FILENAME
        return f"file:{quote(str(database_path), safe='/')}?mode=rw"

    def _open_database_file(self, *, create: bool) -> int:
        root_fd = self._state_root_fd
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        filename = DATABASE_FILENAME
        try:
            try:
                before = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                before = None
            if before is not None:
                _validate_private_file(before, sidecar=False)
            if not create and before is None:
                raise StoreUnavailableError("coordination database disappeared")
            created = create and before is None
            flags = _open_flags(directory=False, writable=True)
            if create:
                flags |= os.O_CREAT | os.O_EXCL
            database_fd = os.open(
                filename,
                flags,
                0o600,
                dir_fd=root_fd,
            )
            observed_identity = _identity(before) if before is not None else None
            try:
                metadata = os.fstat(database_fd)
                observed_identity = _identity(metadata)
                if created:
                    self._fresh_database_fd_identity = observed_identity
                _validate_private_file(metadata, sidecar=False)
                after = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
                if before is not None and _identity(before) != observed_identity:
                    raise StoreUnavailableError(
                        "private SQLite database changed while opening"
                    )
                if _identity(after) != observed_identity:
                    raise StoreUnavailableError(
                        "private SQLite database changed while opening"
                    )
                if not create:
                    if metadata.st_size < 100:
                        raise StoreUnavailableError(
                            "existing coordination database is empty or truncated"
                        )
                    try:
                        header = os.pread(database_fd, 100, 0)
                    except OSError as exc:
                        raise StoreUnavailableError(
                            "existing coordination database header cannot be read"
                        ) from exc
                    if len(header) < 100 or header[:16] != b"SQLite format 3\x00":
                        raise StoreUnavailableError(
                            "existing coordination database is truncated"
                        )
                self._database_identity = observed_identity
                if created:
                    self._fresh_database_created_identity = observed_identity
                    self._fresh_database_fd_identity = None
                return database_fd
            except FileExistsError as exc:
                raise StoreUnavailableError(
                    "coordination database appeared during bootstrap"
                ) from exc
            except _CLEANUP_EXCEPTION as exc:
                handoff_error: BaseException | None = None
                try:
                    self._retain_failed_fd(
                        database_fd,
                        observed_identity,
                        "database open",
                    )
                except _CLEANUP_EXCEPTION as retain_error:
                    handoff_error = retain_error
                capability = _CleanupCapability(self.close)
                if handoff_error is not None:
                    lower = _extract_cleanup_capability(handoff_error)
                    if lower is not None:
                        capability = _CleanupCapability.compose(lower, capability)
                _raise_with_cleanup_capability(exc, capability)
        except StoreError:
            raise
        except OSError as exc:
            raise StoreUnavailableError(
                "private SQLite database cannot be opened"
            ) from exc

    def _track_fresh_sidecars(self) -> None:
        if not self._fresh_bootstrap:
            return
        root_fd = self._state_root_fd
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        for suffix in ("-wal", "-shm", "-journal"):
            filename = f"{DATABASE_FILENAME}{suffix}"
            if (
                filename in self._sidecars_before_open
                or filename in self._fresh_sidecar_created_identities
            ):
                continue
            try:
                metadata = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            _validate_private_file(metadata, sidecar=True)
            self._fresh_sidecar_created_identities[filename] = _identity(metadata)

    def _existing_sidecar_names(self) -> frozenset[str]:
        root_fd = self._state_root_fd
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        names: set[str] = set()
        for suffix in ("-wal", "-shm", "-journal"):
            filename = f"{DATABASE_FILENAME}{suffix}"
            try:
                before = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StoreUnavailableError(
                    "SQLite sidecar cannot be inspected"
                ) from exc
            _validate_private_file(before, sidecar=True)
            names.add(filename)
        return frozenset(names)

    def _reject_nonempty_rollback_journal(self) -> None:
        """Refuse to let SQLite consume a pending hot rollback journal."""

        root_fd = self._state_root_fd
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        filename = f"{DATABASE_FILENAME}-journal"
        try:
            metadata = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StoreUnavailableError(
                "SQLite rollback journal cannot be inspected"
            ) from exc
        _validate_private_file(metadata, sidecar=True)
        if metadata.st_size > 0:
            raise StoreUnavailableError(
                "SQLite rollback journal is pending before open"
            )

    def _enforce_sidecar_modes(self) -> None:
        root_fd = self._state_root_fd
        if root_fd is None:
            raise StoreClosedError("coordination store is closed")
        for suffix in ("-wal", "-shm", "-journal"):
            filename = f"{DATABASE_FILENAME}{suffix}"
            try:
                before = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StoreUnavailableError(
                    "SQLite sidecar cannot be inspected"
                ) from exc
            is_new = filename not in self._sidecars_before_open
            _validate_private_file(before, sidecar=True, require_mode=not is_new)
            if not is_new:
                continue
            sidecar_fd = None
            sidecar_identity = _identity(before)
            if is_new:
                self._fresh_sidecar_created_identities.setdefault(
                    filename,
                    sidecar_identity,
                )
            body_error: BaseException | None = None
            cleanup_error: BaseException | None = None
            try:
                sidecar_fd = os.open(
                    filename,
                    _open_flags(directory=False, writable=True),
                    dir_fd=root_fd,
                )
                metadata = os.fstat(sidecar_fd)
                sidecar_identity = _identity(metadata)
                _validate_private_file(metadata, sidecar=True, require_mode=False)
                if is_new:
                    os.fchmod(sidecar_fd, 0o600)
                    metadata = os.fstat(sidecar_fd)
                    sidecar_identity = _identity(metadata)
                    _validate_private_file(metadata, sidecar=True)
                after = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
                if (
                    _identity(before) != sidecar_identity
                    or _identity(after) != sidecar_identity
                ):
                    raise StoreUnavailableError("SQLite sidecar changed while opening")
                if is_new:
                    self._sidecars_before_open = frozenset(
                        {*self._sidecars_before_open, filename}
                    )
            except StoreError as exc:
                body_error = exc
            except OSError as exc:
                body_error = StoreUnavailableError(
                    "private SQLite sidecar cannot be secured"
                )
                body_error.__cause__ = exc
            except _CLEANUP_EXCEPTION as exc:
                body_error = _store_error_from_exception(
                    exc,
                    "private SQLite sidecar cannot be secured",
                )
            finally:
                if sidecar_fd is not None:
                    cleanup_error = self._handoff_constructor_fd(
                        sidecar_fd,
                        sidecar_identity,
                        "SQLite sidecar open",
                    )
            if body_error is not None:
                if cleanup_error is not None:
                    capability = _CleanupCapability(self.close)
                    cleanup_capability = _extract_cleanup_capability(cleanup_error)
                    if cleanup_capability is not None:
                        capability = _CleanupCapability.compose(
                            cleanup_capability,
                            capability,
                        )
                    _raise_with_cleanup_capability(
                        body_error,
                        capability,
                    )
                raise body_error
            if cleanup_error is not None:
                raise cleanup_error

    def _assert_database_identity(self) -> None:
        root_fd = self._state_root_fd
        database_fd = self._database_fd
        expected = self._database_identity
        if root_fd is None or database_fd is None or expected is None:
            raise StoreClosedError("coordination store is closed")
        try:
            fd_metadata = os.fstat(database_fd)
            path_metadata = os.stat(
                DATABASE_FILENAME,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise StoreUnavailableError(
                "private SQLite database is unavailable"
            ) from exc
        _validate_private_file(fd_metadata, sidecar=False)
        _validate_private_file(path_metadata, sidecar=False)
        if _identity(fd_metadata) != expected or _identity(path_metadata) != expected:
            raise StoreUnavailableError("private SQLite database changed while open")

    def _assert_connection_identity(self) -> None:
        connection = self._connection
        database_fd = self._database_fd
        expected = self._database_identity
        if connection is None or database_fd is None or expected is None:
            raise StoreClosedError("coordination store is closed")
        try:
            main_path = next(
                str(row[2])
                for row in connection.execute("PRAGMA database_list").fetchall()
                if row[1] == "main"
            )
            path_metadata = os.stat(main_path, follow_symlinks=False)
            fd_metadata = os.fstat(database_fd)
        except (OSError, sqlite3.DatabaseError, StopIteration) as exc:
            raise StoreUnavailableError(
                "SQLite connection identity is unavailable"
            ) from exc
        _validate_private_file(fd_metadata, sidecar=False)
        _validate_private_file(path_metadata, sidecar=False)
        if _identity(fd_metadata) != expected or _identity(path_metadata) != expected:
            raise StoreUnavailableError(
                "SQLite connection is not anchored to the private database"
            )

    def _configure_pragmas(self) -> None:
        connection = self._require_connection()
        connection.execute("PRAGMA foreign_keys = ON")
        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        if foreign_keys != 1:
            raise StoreSchemaError("SQLite foreign_keys pragma is not effective")
        journal_mode = str(
            connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        ).lower()
        if journal_mode != "wal":
            raise StoreUnavailableError("SQLite WAL journal mode is unavailable")
        connection.execute("PRAGMA synchronous = FULL")
        synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        if synchronous != 2:
            raise StoreUnavailableError("SQLite synchronous=FULL is not effective")
        connection.execute("PRAGMA recursive_triggers = ON")
        recursive_triggers = int(
            connection.execute("PRAGMA recursive_triggers").fetchone()[0]
        )
        if recursive_triggers != 1:
            raise StoreUnavailableError(
                "SQLite recursive_triggers pragma is not effective"
            )
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
        if busy_timeout != self.busy_timeout_ms:
            raise StoreUnavailableError("SQLite busy timeout is not effective")

    def _preflight_schema(self) -> None:
        connection = self._require_connection()
        objects = self._schema_objects()
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if not objects:
            if user_version != 0:
                raise StoreSchemaError(
                    "empty SQLite database has an unsupported version"
                )
            self._schema_empty = True
            return
        self._schema_empty = False
        self._validate_schema()

    def _initialize_schema(self) -> None:
        with self._write_transaction() as connection:
            for statement in _TABLE_DEFINITIONS.values():
                connection.execute(statement)
            for statement in _INDEX_DEFINITIONS.values():
                connection.execute(statement)
            for statement in _TRIGGER_DEFINITIONS.values():
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO store_meta(key, value) VALUES
                    ('store_schema', ?), ('recovery_epoch', 0),
                    ('fencing_token_floor', 0), ('last_clock_ns', 0)
                """,
                (STORE_SCHEMA,),
            )
            connection.execute(f"PRAGMA user_version = {STORE_SCHEMA}")

    def _validate_schema(self) -> None:
        _validate_existing_schema(self._require_connection())

    def _schema_objects(self) -> dict[tuple[str, str], str]:
        return _schema_objects_for_connection(self._require_connection())

    def _index_contract(
        self, table: str
    ) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
        return _index_contract_for_connection(self._require_connection(), table)

    def _foreign_key_contract(
        self, table: str
    ) -> tuple[tuple[str, str, str, str, str], ...]:
        return _foreign_key_contract_for_connection(self._require_connection(), table)

    def _assert_transaction_identity(self) -> None:
        self._assert_lifetime_gate()
        self._assert_state_root()
        self._assert_database_identity()
        self._assert_connection_identity()
        if self._marker_fd is not None:
            self._assert_marker_identity()
        self._existing_sidecar_names()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._require_connection()
        with self._shared_lifetime_gate():
            self._fault("before_begin")
            connection.execute("BEGIN IMMEDIATE")

            def rollback_body(body_error: BaseException) -> None:
                rollback_error: BaseException | None = None
                if self._connection is connection:
                    try:
                        connection.rollback()
                    except _CLEANUP_EXCEPTION as exc:
                        rollback_error = exc
                        self._connection_cleanup_pending = True
                if rollback_error is not None:
                    capability = _CleanupCapability(self.close)
                    _raise_with_cleanup_capability(body_error, capability)
                raise body_error

            def commit_unknown(cause: BaseException) -> NoReturn:
                self._connection_cleanup_pending = True
                error = StoreCommitUnknownError(
                    "SQLite transaction commit status is unknown"
                )
                _attach_cleanup_capability(error, _CleanupCapability(self.close))
                raise error from cause

            try:
                self._fault("after_begin")
                self._assert_transaction_identity()
                yield connection
            except _CLEANUP_EXCEPTION as body_error:
                rollback_body(body_error)

            try:
                self._fault("before_commit")
                self._assert_transaction_identity()
            except _CLEANUP_EXCEPTION as body_error:
                rollback_body(body_error)

            try:
                connection.commit()
            except _CLEANUP_EXCEPTION as commit_error:
                commit_unknown(commit_error)

            try:
                self._fault("after_commit")
                self._assert_transaction_identity()
            except _CLEANUP_EXCEPTION as postcommit_error:
                commit_unknown(postcommit_error)

    @contextmanager
    def _lease_write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Apply C1 transaction/error mapping to each C2 state transition."""

        try:
            with self._write_transaction() as connection:
                yield connection
        except (LeaseError, StoreError):
            raise
        except sqlite3.IntegrityError as exc:
            _raise_sqlite_write_error(exc)
        except sqlite3.DatabaseError as exc:
            _raise_sqlite_write_error(exc)

    @contextmanager
    def _recovery_transaction(self) -> Iterator[_RecoveryStoreTx]:
        """Yield a typed recovery seam without exposing SQL or rows."""

        with self._lease_write_transaction() as connection:
            transaction = _RecoveryStoreTx(self, connection)
            try:
                yield transaction
            finally:
                transaction._close()

    def _recovery_snapshot(self, operation_id: str) -> RecoverySnapshot:
        with self._shared_lifetime_gate():
            result = self._recovery_snapshot_tx(
                self._require_connection(),
                operation_id,
            )
            self._assert_transaction_identity()
            return result

    def _current_recovery_epoch(self) -> int:
        """Read the current global recovery epoch without opening a write path."""

        try:
            with self._shared_lifetime_gate():
                connection = self._require_connection()
                epoch = self._metadata_integer(
                    connection,
                    "recovery_epoch",
                    "recovery_epoch",
                )
                self._assert_transaction_identity()
                return epoch
        except StoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError) as exc:
            _raise_recovery_read_error(exc, kind="recovery epoch")

    def _rehydrate_claim(self, operation_id: str) -> Claim | None:
        operation_id = _require_opaque_identifier(operation_id, "operation_id")
        try:
            with self._shared_lifetime_gate():
                result = self._rehydrate_claim_tx(
                    self._require_connection(),
                    operation_id,
                )
                self._assert_transaction_identity()
                return result
        except StoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError) as exc:
            _raise_recovery_read_error(exc, kind="claim")

    def _rehydrate_effect(self, operation_id: str) -> ProviderEffect | None:
        operation_id = _require_opaque_identifier(operation_id, "operation_id")
        try:
            with self._shared_lifetime_gate():
                result = self._rehydrate_effect_tx(
                    self._require_connection(),
                    operation_id,
                )
                self._assert_transaction_identity()
                return result
        except StoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError) as exc:
            _raise_recovery_read_error(exc, kind="effect")

    def _rehydrate_receipt(self, operation_id: str) -> VerifiedProviderReceipt | None:
        with self._shared_lifetime_gate():
            result = self._rehydrate_receipt_tx(
                self._require_connection(),
                operation_id,
            )
            self._assert_transaction_identity()
            return result

    def _fault(self, point: str) -> None:
        """Private no-op seam used only by deterministic process tests."""

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> OperationSnapshot:
        try:
            return OperationSnapshot(
                operation_id=row["operation_id"],
                effect_key=row["effect_key"],
                status=row["status"],
                attempt=row["current_attempt"],
                recovery_epoch=row["recovery_epoch"],
                created_ns=row["created_ns"],
                updated_ns=row["updated_ns"],
                provider_id=row["provider_id"],
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise StoreIntegrityError("SQLite operation data is invalid") from exc

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> TransitionEvent:
        try:
            return TransitionEvent(
                sequence=row["event_id"],
                event_schema_version=row["event_schema_version"],
                operation_id=row["operation_id"],
                attempt=row["attempt"],
                from_status=row["from_status"],
                to_status=row["to_status"],
                kind=row["kind"],
                actor=row["actor"],
                clock_ns=row["clock_ns"],
                reason_code=row["reason_code"],
                evidence_ref=row["evidence_ref"],
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise StoreIntegrityError("SQLite journal data is invalid") from exc

    @staticmethod
    def _raise_read_error(error: sqlite3.OperationalError) -> None:
        if "locked" in str(error).lower():
            raise StoreBusyError("SQLite busy timeout expired while reading") from error
        raise StoreUnavailableError("SQLite read failed") from error


class RestoreStoreAuthority:
    """Filesystem-free authority for candidate restore operations.

    Restore owns the quiescent lifetime gate while it works with source,
    destination, and candidate descriptors.  It therefore cannot keep a
    normal ``CoordinationStore`` open for this work.  This authority carries
    only the trusted fault hook; all image validation and SQLite mutation are
    pure class seams on ``CoordinationStore`` and return typed observations.
    """

    __slots__ = ("__fault",)

    def __init__(
        self,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        if fault is not None and not callable(fault):
            raise TypeError("restore fault hook must be callable")
        self.__fault = _noop_restore_fault if fault is None else fault

    def _require_initialized(self) -> Callable[[str], None]:
        try:
            fault = self.__fault
        except AttributeError as exc:
            raise StoreClosedError("restore authority is uninitialized") from exc
        if not callable(fault):
            raise StoreClosedError("restore authority is invalid")
        return fault

    def inspect_image(self, fd: int) -> StoreImageObservation:
        self._require_initialized()
        return CoordinationStore._inspect_image_fd(fd)

    def read_floor(self, fd: int) -> RecoveryFloor:
        return self.inspect_image(fd).floor

    def read_identities(self, fd: int) -> tuple[RestoreIdentity, ...]:
        return self.inspect_image(fd).identities

    def reserve_restore_floor(
        self,
        source: StoreImageObservation,
        destination: StoreImageObservation,
        ledger_floor_lower_bound: RecoveryFloor,
    ) -> RecoveryFloorReservation:
        self._require_initialized()
        return CoordinationStore._reserve_restore_floor(
            source,
            destination,
            ledger_floor_lower_bound,
        )

    def verify_history_binding(self, primary_fd: int, state: object) -> None:
        """Verify the committed restore anchor without opening a Store or provider."""

        self._require_initialized()
        CoordinationStore._verify_history_binding(primary_fd, state)

    def apply_candidate(
        self,
        source_fd: int,
        destination_fd: int,
        target_fd: int,
        *,
        source_observation: StoreImageObservation | None = None,
        destination_observation: StoreImageObservation | None = None,
        ledger_floor_lower_bound: RecoveryFloor,
        reservation: RecoveryFloorReservation | None = None,
        previous_active_tombstones: object,
        restore_generation: int,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RestoreApplyResult:
        fault = self._require_initialized()
        return CoordinationStore._apply_restore_candidate(
            source_fd,
            destination_fd,
            target_fd,
            source_observation=source_observation,
            destination_observation=destination_observation,
            ledger_floor_lower_bound=ledger_floor_lower_bound,
            reservation=reservation,
            previous_active_tombstones=previous_active_tombstones,
            restore_generation=restore_generation,
            actor=actor,
            timestamp=timestamp,
            evidence_ref=evidence_ref,
            fault=fault,
        )

    def verify_candidate(
        self,
        candidate_fd: int,
        expected: RestoreApplyResult,
    ) -> RestoreApplyResult:
        self._require_initialized()
        return CoordinationStore._verify_candidate_applied(candidate_fd, expected)

    def verify_candidate_evidence(
        self,
        source_fd: int,
        destination_fd: int,
        candidate_fd: int,
        evidence: RestoreCandidateEvidence,
    ) -> RestoreApplyResult:
        """Verify a resumed candidate using durable evidence only."""

        self._require_initialized()
        return CoordinationStore._verify_candidate_from_evidence(
            source_fd,
            destination_fd,
            candidate_fd,
            evidence,
        )

    def verify_replaced_evidence(
        self,
        source_fd: int,
        primary_fd: int,
        evidence: RestoreReplacedEvidence,
    ) -> RestoreApplyResult:
        """Verify a replaced primary using durable evidence only.

        The old destination and candidate pathname may be gone at resume time;
        restore event count is reconstructed from source and primary images.
        """

        self._require_initialized()
        return CoordinationStore._verify_replaced_evidence(
            source_fd,
            primary_fd,
            evidence,
        )

    # Package-private names are retained as explicit aliases for the restore
    # coordinator; they still bind to this authority, never to a Store.
    inspect_restore = inspect_image
    _inspect_image_fd = inspect_image
    _read_restore_floor = read_floor
    _read_floor = read_floor
    _read_restore_identities = read_identities
    _read_identities = read_identities
    _reserve_restore_floor = reserve_restore_floor
    _verify_history_binding = verify_history_binding
    _apply_restore_candidate = apply_candidate
    _apply_candidate = apply_candidate
    _verify_candidate_applied = verify_candidate
    _verify_candidate = verify_candidate
    _verify_candidate_from_evidence = verify_candidate_evidence
    verify_from_durable_evidence = verify_candidate_evidence
    _verify_replaced_evidence = verify_replaced_evidence


class _RecoveryStoreTx:
    """Private typed transaction facade for C3; SQL/rows stay store-internal."""

    __slots__ = (
        "__active_token",
        "__advanced_floor",
        "__closed",
        "__connection",
        "__store",
    )

    def __init__(
        self,
        store: CoordinationStore,
        connection: sqlite3.Connection,
    ) -> None:
        self.__store = store
        self.__connection = connection
        self.__active_token: object | None = object()
        self.__advanced_floor: RecoveryFloor | None = None
        self.__closed = False

    def _assert_active(self) -> None:
        if self.__closed or self.__active_token is None:
            raise StoreClosedError("recovery transaction is closed")
        if self.__store._connection is not self.__connection:
            self._close()
            raise StoreClosedError("coordination store is closed")
        try:
            in_transaction = self.__connection.in_transaction
        except sqlite3.ProgrammingError as exc:
            self._close()
            raise StoreClosedError("recovery transaction is closed") from exc
        if not in_transaction:
            self.__closed = True
            self.__active_token = None
            raise StoreClosedError("recovery transaction is no longer active")
        self.__store._assert_transaction_identity()

    def _close(self) -> None:
        self.__closed = True
        self.__active_token = None

    def advance_floor(
        self,
        reservation: RecoveryFloorReservation,
        *,
        timestamp: int,
    ) -> RecoveryFloor:
        """Apply a store-issued recovery floor in this transaction."""

        self._assert_active()
        if self.__advanced_floor is not None:
            raise LeaseConflictError("recovery floor is already advanced")
        if (
            not isinstance(reservation, RecoveryFloorReservation)
            or not reservation.is_issued
        ):
            raise LeaseConflictError("recovery floor reservation is invalid")
        self.__store._record_clock(
            self.__connection,
            timestamp,
            strict=True,
        )
        floor = self.__store._advance_floor_tx(
            self.__connection,
            reservation,
        )
        self.__advanced_floor = floor
        return floor

    def rebase(
        self,
        snapshot: RecoverySnapshot,
        *,
        mode: RecoveryRebaseMode,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        """Rebase a finite operation class after ``advance_floor``."""

        self._assert_active()
        floor = self.__advanced_floor
        if floor is None:
            raise LeaseConflictError("recovery floor must be advanced first")
        mode = _require_rebase_mode(mode)
        if snapshot.status != mode:
            raise LeaseConflictError("rebase mode does not match operation status")
        self.__store._validate_event_values(
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=snapshot.status,
            to_status=snapshot.status,
            kind="restore",
            actor=actor,
            timestamp=timestamp,
            reason_code="restore",
            evidence_ref=evidence_ref,
        )
        effective = self.__store._record_clock(
            self.__connection,
            timestamp,
            strict=True,
        )
        return self.__store._rebase_operation_tx(
            self.__connection,
            snapshot,
            floor=floor,
            mode=mode,
            actor=actor,
            timestamp=effective,
            evidence_ref=evidence_ref,
        )

    def snapshot(self, operation_id: str) -> RecoverySnapshot:
        self._assert_active()
        result = self.__store._recovery_snapshot_tx(
            self.__connection,
            operation_id,
        )
        self.__store._assert_transaction_identity()
        return result

    def claim(self, operation_id: str) -> Claim | None:
        self._assert_active()
        result = self.__store._rehydrate_claim_tx(
            self.__connection,
            operation_id,
        )
        self.__store._assert_transaction_identity()
        return result

    def effect(self, operation_id: str) -> ProviderEffect | None:
        self._assert_active()
        result = self.__store._rehydrate_effect_tx(
            self.__connection,
            operation_id,
        )
        self.__store._assert_transaction_identity()
        return result

    def recovery_effect(self, operation_id: str) -> ProviderEffect | None:
        """Return identity-only effect evidence for an unknown operation."""

        self._assert_active()
        result = self.__store._rehydrate_recovery_effect_tx(
            self.__connection,
            operation_id,
        )
        self.__store._assert_transaction_identity()
        return result

    def recover_expired(
        self,
        snapshot: RecoverySnapshot,
        *,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        """Fail closed one lease after its exact expiry boundary."""

        self._assert_active()
        if snapshot.status not in {"FENCE_PENDING", "CLAIMED"}:
            raise LeaseConflictError("operation is not an expiring lease")
        self.__store._validate_event_values(
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=snapshot.status,
            to_status=(
                "FENCE_PENDING"
                if snapshot.status == "FENCE_PENDING"
                else "UNKNOWN_EFFECT"
            ),
            kind="recover",
            actor=actor,
            timestamp=timestamp,
            reason_code="recover",
            evidence_ref=evidence_ref,
        )
        effective = self.__store._record_clock(
            self.__connection,
            timestamp,
            strict=True,
        )
        return self.__store._recover_expired_tx(
            self.__connection,
            snapshot,
            actor=actor,
            timestamp=effective,
            evidence_ref=evidence_ref,
        )

    def force_recover(
        self,
        snapshot: RecoverySnapshot,
        *,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        """Fence one operation after a floor reservation was committed."""

        self._assert_active()
        floor = self.__advanced_floor
        if floor is None:
            raise LeaseConflictError("recovery floor must be advanced first")
        if snapshot.status == "CLEANED":
            raise LeaseConflictError("cleaned operation is immutable")
        self.__store._validate_event_values(
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=snapshot.status,
            to_status=(
                "UNKNOWN_EFFECT"
                if snapshot.status
                in {
                    "FENCE_PENDING",
                    "FENCE_RESERVATION_STARTED",
                    "CLAIMED",
                    "EFFECT_PREPARED",
                }
                else snapshot.status
            ),
            kind="force_recover",
            actor=actor,
            timestamp=timestamp,
            reason_code="force_recover",
            evidence_ref=evidence_ref,
        )
        effective = self.__store._record_clock(
            self.__connection,
            timestamp,
            strict=True,
        )
        return self.__store._force_recover_tx(
            self.__connection,
            snapshot,
            floor=floor,
            actor=actor,
            timestamp=effective,
            evidence_ref=evidence_ref,
        )

    def resolve_unknown_absent(
        self,
        snapshot: RecoverySnapshot,
        *,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        """Return only a provider-proven absent operation to INTENT."""

        self._assert_active()
        if snapshot.status not in {"UNKNOWN_EFFECT", "UNKNOWN"}:
            raise LeaseConflictError("operation is not unknown")
        self.__store._validate_event_values(
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=snapshot.status,
            to_status="INTENT",
            kind="resolve_unknown",
            actor=actor,
            timestamp=timestamp,
            reason_code="resolve_unknown",
            evidence_ref=evidence_ref,
        )
        effective = self.__store._record_clock(
            self.__connection,
            timestamp,
            strict=True,
        )
        return self.__store._resolve_unknown_absent_tx(
            self.__connection,
            snapshot,
            actor=actor,
            timestamp=effective,
            evidence_ref=evidence_ref,
        )

    def resolve_unknown_completed(
        self,
        snapshot: RecoverySnapshot,
        receipt: VerifiedProviderReceipt,
        *,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        """Persist a provider-proven completed result without execution."""

        self._assert_active()
        if snapshot.status not in {"UNKNOWN_EFFECT", "UNKNOWN"}:
            raise LeaseConflictError("operation is not unknown")
        self.__store._validate_event_values(
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=snapshot.status,
            to_status="RECEIPTED",
            kind="resolve_unknown",
            actor=actor,
            timestamp=timestamp,
            reason_code="resolve_unknown",
            evidence_ref=evidence_ref,
        )
        effective = self.__store._record_clock(
            self.__connection,
            timestamp,
            strict=True,
        )
        return self.__store._resolve_unknown_completed_tx(
            self.__connection,
            snapshot,
            receipt,
            actor=actor,
            timestamp=effective,
            evidence_ref=evidence_ref,
        )

    def receipt(self, operation_id: str) -> VerifiedProviderReceipt | None:
        self._assert_active()
        result = self.__store._rehydrate_receipt_tx(
            self.__connection,
            operation_id,
        )
        self.__store._assert_transaction_identity()
        return result

    def mark_prepared_unknown(
        self,
        snapshot: RecoverySnapshot,
        *,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        """Fail-closed a prepared marker without a generic downgrade path."""

        self._assert_active()
        if snapshot.status not in {
            "FENCE_RESERVATION_STARTED",
            "EFFECT_PREPARED",
        }:
            raise LeaseConflictError("operation is not a prepared recovery marker")
        self.__store._validate_event_values(
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=snapshot.status,
            to_status="UNKNOWN_EFFECT",
            kind="unknown_effect",
            actor=actor,
            timestamp=timestamp,
            reason_code="effect_unknown",
            evidence_ref=evidence_ref,
        )
        effective = self.__store._record_clock(
            self.__connection,
            timestamp,
            strict=True,
        )
        return self.__store._mark_recovery_unknown_tx(
            self.__connection,
            snapshot,
            actor=actor,
            timestamp=effective,
            evidence_ref=evidence_ref,
        )

    def append_event(
        self,
        snapshot: RecoverySnapshot,
        *,
        kind: str,
        reason_code: str,
        actor: str,
        timestamp: int,
        evidence_ref: str | None = None,
    ) -> RecoverySnapshot:
        self._assert_active()
        self.__store._validate_event_values(
            operation_id=snapshot.operation_id,
            attempt=snapshot.current_attempt,
            from_status=snapshot.status,
            to_status=snapshot.status,
            kind=kind,
            actor=actor,
            timestamp=timestamp,
            reason_code=reason_code,
            evidence_ref=evidence_ref,
        )
        effective = self.__store._record_clock(
            self.__connection,
            timestamp,
            strict=True,
        )
        return self.__store._append_recovery_event_tx(
            self.__connection,
            snapshot,
            kind=kind,
            reason_code=reason_code,
            actor=actor,
            timestamp=effective,
            evidence_ref=evidence_ref,
        )
