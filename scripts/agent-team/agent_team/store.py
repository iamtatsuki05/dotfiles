"""Private SQLite coordination state for the agent-team runtime.

The public seam exposes immutable observations only.  SQLite connections,
queries, rows, and mutation authority stay inside this module so later
coordination phases can extend the implementation without widening callers.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
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
    VerifiedProviderReceipt,
    _issue_floor_reservation,
    _issue_provider_effect,
    _verified_receipt_from_status,
    require_provider_capabilities,
)

STORE_SCHEMA: Final[int] = 2
EVENT_SCHEMA_VERSION: Final[int] = 2
DATABASE_FILENAME: Final[str] = "coordination.sqlite3"
LIFETIME_GATE_FILENAME: Final[str] = ".coordination-lifetime.lock"
DEFAULT_BUSY_TIMEOUT_MS: Final[int] = 5_000
MAX_BUSY_TIMEOUT_MS: Final[int] = 30_000
SQLITE_INTEGER_MAX: Final[int] = 2**63 - 1
MAX_IDENTIFIER_LENGTH: Final[int] = 128

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


class StoreError(RuntimeError):
    """Base class for explicit coordination-store failures."""


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


def _normalize_sql(sql: str) -> str:
    return sql.strip()


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
    if isinstance(error, sqlite3.OperationalError):
        if "locked" in message or "busy" in message:
            raise StoreBusyError("SQLite busy timeout expired while writing") from error
        if any(
            marker in message
            for marker in ("malformed", "not a database", "corrupt", "integrity")
        ):
            raise StoreIntegrityError(
                "SQLite write found an integrity failure"
            ) from error
        raise StoreUnavailableError("SQLite write failed") from error
    if any(
        marker in message
        for marker in ("not authorized", "readonly", "read-only", "disk i/o")
    ):
        raise StoreUnavailableError("SQLite write failed") from error
    if isinstance(error, sqlite3.IntegrityError):
        raise StoreIntegrityError("SQLite write violated a store constraint") from error
    raise StoreIntegrityError("SQLite write failed") from error


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
    current_fd: int | None = None
    try:
        root_fd = os.open(os.sep, directory_flags)
        current_fd = root_fd
        components = state_root.parts[1:]
        for index, component in enumerate(components):
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
            _validate_directory_fd(
                current_fd,
                state_root=index == len(components) - 1,
            )
        if current_fd == root_fd:
            raise StoreUnavailableError("private state root is invalid")
        result_fd = current_fd
        if root_fd != result_fd:
            os.close(root_fd)
            root_fd = None
        return result_fd
    except StoreError:
        if current_fd is not None and current_fd != root_fd:
            os.close(current_fd)
        if root_fd is not None:
            os.close(root_fd)
        raise
    except OSError as exc:
        if current_fd is not None and current_fd != root_fd:
            os.close(current_fd)
        if root_fd is not None:
            os.close(root_fd)
        raise StoreUnavailableError(
            "private state root cannot be securely opened"
        ) from exc


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


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
        self._state_root_fd: int | None = None
        self._state_root_identity: tuple[int, int] | None = None
        self._lifetime_gate_fd: int | None = None
        self._lifetime_gate_identity: tuple[int, int] | None = None
        self._lifetime_gate_required = False
        self._lifetime_gate_shared = False
        self._lifetime_gate_persistent = False
        self._lifetime_gate_condition = threading.Condition()
        self._lifetime_gate_shared_users = 0
        self._lifetime_gate_exclusive_owner: int | None = None
        self._lifetime_gate_local = threading.local()
        self._database_fd: int | None = None
        self._database_identity: tuple[int, int] | None = None
        self._startup_lock_held = False
        self._sidecars_before_open: frozenset[str] = frozenset()
        self._schema_empty = False
        self._last_clock_ns = 0
        try:
            self._state_root_fd = _open_state_root(self.state_root)
            self._state_root_identity = _identity(os.fstat(self._state_root_fd))
            self._acquire_startup_lock()
            self._database_fd = self._open_database_file()
            self._assert_state_root()
            self._sidecars_before_open = self._existing_sidecar_names()
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
            self._lifetime_gate_fd = self._open_lifetime_gate()
            self._lifetime_gate_required = True
            self._acquire_lifetime_gate(exclusive=False)
            self._lifetime_gate_persistent = True
            self._assert_state_root()
            self._assert_database_identity()
            self._assert_connection_identity()
            self._configure_pragmas()
            self._enforce_sidecar_modes()
            if self._schema_empty:
                self._initialize_schema()
            self._validate_schema()
            self._load_store_high_water()
            self._validate_prepared_markers()
            self._assert_database_identity()
            self._release_startup_lock()
            self._release_lifetime_gate()
            self._lifetime_gate_persistent = False
        except StoreError:
            self.close()
            raise
        except sqlite3.OperationalError as exc:
            self.close()
            if "locked" in str(exc).lower():
                raise StoreBusyError(
                    "SQLite busy timeout expired while opening"
                ) from exc
            raise StoreUnavailableError("SQLite database could not be opened") from exc
        except sqlite3.DatabaseError as exc:
            self.close()
            raise StoreSchemaError("SQLite database is not a valid store") from exc
        except OSError as exc:
            self.close()
            raise StoreUnavailableError(
                "private SQLite state cannot be opened"
            ) from exc
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _open_lifetime_gate(self) -> int:
        """Open the stable, never-unlinked gate beside the state root."""

        gate_path = self.state_root.parent / LIFETIME_GATE_FILENAME
        try:
            try:
                before = os.stat(gate_path, follow_symlinks=False)
            except FileNotFoundError:
                before = None
            if before is not None:
                _validate_private_file(before, sidecar=True)
            gate_fd = os.open(
                gate_path,
                _open_flags(directory=False, writable=True) | os.O_CREAT,
                0o600,
            )
            try:
                metadata = os.fstat(gate_fd)
                _validate_private_file(metadata, sidecar=True)
                after = os.stat(gate_path, follow_symlinks=False)
                if before is not None and _identity(before) != _identity(metadata):
                    raise StoreUnavailableError(
                        "coordination lifetime gate changed while opening"
                    )
                if _identity(after) != _identity(metadata):
                    raise StoreUnavailableError(
                        "coordination lifetime gate changed while opening"
                    )
                self._lifetime_gate_identity = _identity(metadata)
                return gate_fd
            except BaseException:
                os.close(gate_fd)
                raise
        except StoreError:
            raise
        except OSError as exc:
            raise StoreUnavailableError(
                "coordination lifetime gate cannot be opened"
            ) from exc

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
        try:
            self._assert_lifetime_gate()
            yield
            self._assert_lifetime_gate()
        finally:
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
                    self._release_lifetime_gate()
                self._lifetime_gate_condition.notify_all()

    @contextmanager
    def _exclusive_lifetime_gate(self) -> Iterator[None]:
        """Reserve the gate for cooperating restore/replacement operations."""

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
        try:
            self._assert_lifetime_gate()
            yield
            self._assert_lifetime_gate()
        finally:
            try:
                if had_shared:
                    self._downgrade_lifetime_gate()
                else:
                    self._release_lifetime_gate()
            finally:
                with self._lifetime_gate_condition:
                    self._lifetime_gate_exclusive_owner = None
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
        try:
            gate_fd = os.open(
                gate_path,
                _open_flags(directory=False, writable=True),
            )
            metadata = os.fstat(gate_fd)
            _validate_private_file(metadata, sidecar=True)
            path_metadata = os.stat(gate_path, follow_symlinks=False)
            _validate_private_file(path_metadata, sidecar=True)
            identity = _identity(metadata)
            if _identity(path_metadata) != identity:
                raise StoreUnavailableError(
                    "coordination lifetime gate changed while opening"
                )
            cls._lock_lifetime_gate_fd(
                gate_fd,
                exclusive=True,
                busy_timeout_ms=busy_timeout_ms,
            )
            path_metadata = os.stat(gate_path, follow_symlinks=False)
            _validate_private_file(path_metadata, sidecar=True)
            if _identity(path_metadata) != identity:
                raise StoreUnavailableError(
                    "coordination lifetime gate changed while locking"
                )
            yield
            path_metadata = os.stat(gate_path, follow_symlinks=False)
            _validate_private_file(path_metadata, sidecar=True)
            if _identity(path_metadata) != identity:
                raise StoreUnavailableError(
                    "coordination lifetime gate changed while held"
                )
        except StoreError:
            raise
        except OSError as exc:
            raise StoreUnavailableError(
                "coordination lifetime gate cannot be locked"
            ) from exc
        finally:
            if gate_fd is not None:
                try:
                    fcntl.flock(gate_fd, fcntl.LOCK_UN)
                finally:
                    os.close(gate_fd)

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        database_fd = self._database_fd
        self._database_fd = None
        root_fd = self._state_root_fd
        self._state_root_fd = None
        lifetime_gate_fd = self._lifetime_gate_fd
        self._lifetime_gate_fd = None
        self._lifetime_gate_required = False
        self._state_root_identity = None
        self._lifetime_gate_identity = None
        self._database_identity = None
        if self._startup_lock_held and root_fd is not None:
            try:
                fcntl.flock(root_fd, fcntl.LOCK_UN)
            finally:
                self._startup_lock_held = False
        if connection is not None:
            connection.close()
        if database_fd is not None:
            os.close(database_fd)
        if root_fd is not None:
            os.close(root_fd)
        if lifetime_gate_fd is not None:
            try:
                fcntl.flock(lifetime_gate_fd, fcntl.LOCK_UN)
            finally:
                os.close(lifetime_gate_fd)
        self._lifetime_gate_shared = False

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
                raise DuplicateOperationError(
                    "operation or effect identity already exists"
                ) from exc
            _raise_sqlite_write_error(exc)
        except sqlite3.DatabaseError as exc:
            _raise_sqlite_write_error(exc)
        except (OverflowError, TypeError) as exc:
            raise StoreError("SQLite intent transaction failed") from exc
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
            fencing_token = self._next_value(connection)
            connection.execute(
                """
                    UPDATE operations
                    SET provider_id = ?, status = 'FENCE_PENDING',
                        current_attempt = 1, updated_ns = ?
                    WHERE operation_id = ? AND status = 'INTENT'
                      AND current_attempt = 0
                    """,
                (provider_id, timestamp, operation_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise LeaseConflictError("operation claim was lost")
            connection.execute(
                """
                    INSERT INTO operation_attempts(
                        operation_id, attempt, owner, provider_id,
                        lease_epoch, fencing_token, lease_heartbeat_ns,
                        lease_expires_ns
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    operation_id,
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
                attempt=1,
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

    def _next_value(self, connection: sqlite3.Connection) -> int:
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
        connection = self._connection
        if connection is None:
            raise StoreClosedError("coordination store is closed")
        self._assert_lifetime_gate()
        self._assert_state_root()
        self._assert_database_identity()
        self._assert_connection_identity()
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

    def _open_database_file(self) -> int:
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
            database_fd = os.open(
                filename,
                _open_flags(directory=False, writable=True) | os.O_CREAT,
                0o600,
                dir_fd=root_fd,
            )
            try:
                metadata = os.fstat(database_fd)
                _validate_private_file(metadata, sidecar=False)
                after = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
                if before is not None and _identity(before) != _identity(metadata):
                    raise StoreUnavailableError(
                        "private SQLite database changed while opening"
                    )
                if _identity(after) != _identity(metadata):
                    raise StoreUnavailableError(
                        "private SQLite database changed while opening"
                    )
                self._database_identity = _identity(metadata)
                return database_fd
            except BaseException:
                os.close(database_fd)
                raise
        except StoreError:
            raise
        except OSError as exc:
            raise StoreUnavailableError(
                "private SQLite database cannot be opened"
            ) from exc

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
            try:
                sidecar_fd = os.open(
                    filename,
                    _open_flags(directory=False, writable=True),
                    dir_fd=root_fd,
                )
                metadata = os.fstat(sidecar_fd)
                _validate_private_file(metadata, sidecar=True, require_mode=False)
                if is_new:
                    os.fchmod(sidecar_fd, 0o600)
                    metadata = os.fstat(sidecar_fd)
                    _validate_private_file(metadata, sidecar=True)
                after = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
                if _identity(before) != _identity(metadata) or _identity(
                    after
                ) != _identity(metadata):
                    raise StoreUnavailableError("SQLite sidecar changed while opening")
                if is_new:
                    self._sidecars_before_open = frozenset(
                        {*self._sidecars_before_open, filename}
                    )
            except StoreError:
                raise
            except OSError as exc:
                raise StoreUnavailableError(
                    "private SQLite sidecar cannot be secured"
                ) from exc
            finally:
                if sidecar_fd is not None:
                    os.close(sidecar_fd)

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
        self._existing_sidecar_names()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._require_connection()
        with self._shared_lifetime_gate():
            self._fault("before_begin")
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._fault("after_begin")
                self._assert_transaction_identity()
                yield connection
                self._fault("before_commit")
                self._assert_transaction_identity()
                connection.commit()
                self._fault("after_commit")
                try:
                    self._assert_transaction_identity()
                except StoreError as exc:
                    raise StoreCommitUnknownError(
                        "SQLite commit completed without a stable identity"
                    ) from exc
            except BaseException:
                if self._connection is connection:
                    try:
                        connection.rollback()
                    except sqlite3.ProgrammingError:
                        pass
                raise

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
