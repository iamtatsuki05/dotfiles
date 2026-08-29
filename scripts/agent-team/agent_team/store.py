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
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, Self
from urllib.parse import quote

STORE_SCHEMA: Final[int] = 1
EVENT_SCHEMA_VERSION: Final[int] = 1
DATABASE_FILENAME: Final[str] = "coordination.sqlite3"
DEFAULT_BUSY_TIMEOUT_MS: Final[int] = 5_000
MAX_BUSY_TIMEOUT_MS: Final[int] = 30_000
SQLITE_INTEGER_MAX: Final[int] = 2**63 - 1
MAX_IDENTIFIER_LENGTH: Final[int] = 128

_VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {"INTENT", "CLAIMED", "RECEIPTED", "COMPLETED", "CLEANED", "UNKNOWN"}
)
_VALID_REASON_CODES: Final[frozenset[str]] = frozenset({"intent_created"})
_VALID_EVENT_KINDS: Final[frozenset[str]] = frozenset({"intent"})
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
            'INTENT', 'CLAIMED', 'RECEIPTED', 'COMPLETED', 'CLEANED', 'UNKNOWN'
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
    )
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
        AND attempt BETWEEN 0 AND 9223372036854775807
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
        AND length(provider_status) BETWEEN 1 AND 128
    ),
    owner TEXT NOT NULL CHECK(
        typeof(owner) = 'text' AND length(owner) BETWEEN 1 AND 128
    ),
    fencing_token INTEGER NOT NULL CHECK(
        typeof(fencing_token) = 'integer'
        AND fencing_token BETWEEN 0 AND 9223372036854775807
    ),
    lease_epoch INTEGER NOT NULL CHECK(
        typeof(lease_epoch) = 'integer'
        AND lease_epoch BETWEEN 0 AND 9223372036854775807
    ),
    received_ns INTEGER NOT NULL CHECK(
        typeof(received_ns) = 'integer'
        AND received_ns BETWEEN 0 AND 9223372036854775807
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
        typeof(event_schema_version) = 'integer' AND event_schema_version = 1
    ),
    operation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK(
        typeof(attempt) = 'integer'
        AND attempt BETWEEN 0 AND 9223372036854775807
    ),
    from_status TEXT CHECK(
        from_status IS NULL OR (
            typeof(from_status) = 'text' AND from_status IN (
                'INTENT', 'CLAIMED', 'RECEIPTED', 'COMPLETED', 'CLEANED', 'UNKNOWN'
            )
        )
    ),
    to_status TEXT NOT NULL CHECK(
        typeof(to_status) = 'text' AND to_status IN (
            'INTENT', 'CLAIMED', 'RECEIPTED', 'COMPLETED', 'CLEANED', 'UNKNOWN'
        )
    ),
    kind TEXT NOT NULL CHECK(typeof(kind) = 'text' AND kind = 'intent'),
    actor TEXT NOT NULL CHECK(
        typeof(actor) = 'text' AND length(actor) BETWEEN 1 AND 128
    ),
    clock_ns INTEGER NOT NULL CHECK(
        typeof(clock_ns) = 'integer'
        AND clock_ns BETWEEN 0 AND 9223372036854775807
    ),
    reason_code TEXT NOT NULL CHECK(
        typeof(reason_code) = 'text' AND reason_code = 'intent_created'
    ),
    evidence_ref TEXT CHECK(
        evidence_ref IS NULL OR (
            typeof(evidence_ref) = 'text'
            AND length(evidence_ref) = 71
            AND substr(evidence_ref, 1, 7) = 'sha256:'
            AND substr(evidence_ref, 8) NOT GLOB '*[^0-9a-f]*'
        )
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
}
_EXPECTED_TABLES: Final[frozenset[str]] = frozenset(_TABLE_DEFINITIONS)
_EXPECTED_TRIGGERS: Final[frozenset[str]] = frozenset(_TRIGGER_DEFINITIONS)
_EXPECTED_META_KEYS: Final[frozenset[str]] = frozenset(
    {"store_schema", "recovery_epoch"}
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
        ("current_attempt", "INTEGER", 1, 0),
        ("recovery_epoch", "INTEGER", 1, 0),
        ("created_ns", "INTEGER", 1, 0),
        ("updated_ns", "INTEGER", 1, 0),
    ),
    "operation_attempts": (
        ("operation_id", "TEXT", 1, 1),
        ("attempt", "INTEGER", 1, 2),
        ("owner", "TEXT", 0, 0),
        ("lease_epoch", "INTEGER", 1, 0),
        ("fencing_token", "INTEGER", 1, 0),
        ("lease_heartbeat_ns", "INTEGER", 0, 0),
        ("lease_expires_ns", "INTEGER", 0, 0),
    ),
    "effect_receipts": (
        ("operation_id", "TEXT", 1, 1),
        ("attempt", "INTEGER", 1, 2),
        ("effect_key", "TEXT", 1, 0),
        ("provider_effect_id", "TEXT", 1, 0),
        ("provider_status", "TEXT", 1, 0),
        ("owner", "TEXT", 1, 0),
        ("fencing_token", "INTEGER", 1, 0),
        ("lease_epoch", "INTEGER", 1, 0),
        ("received_ns", "INTEGER", 1, 0),
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

    def __post_init__(self) -> None:
        _require_opaque_identifier(self.operation_id, "operation_id")
        _require_opaque_identifier(self.effect_key, "effect_key")
        _require_status(self.status)
        _require_sqlite_integer(self.attempt, "attempt")
        _require_sqlite_integer(self.recovery_epoch, "recovery_epoch")
        _require_sqlite_integer(self.created_ns, "created_ns")
        _require_sqlite_integer(self.updated_ns, "updated_ns")


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


class CoordinationStore:
    """Private SQLite store for durable intent and journal state.

    ``state_root`` must already exist as an owner-only mode ``0700`` directory.
    The store creates or opens only ``coordination.sqlite3`` below that root;
    the database and SQLite sidecars must be owner-only mode ``0600`` files.
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
        self._clock = clock or time.time_ns
        self._connection: sqlite3.Connection | None = None
        self._state_root_fd: int | None = None
        self._state_root_identity: tuple[int, int] | None = None
        self._database_fd: int | None = None
        self._database_identity: tuple[int, int] | None = None
        self._startup_lock_held = False
        self._sidecars_before_open: frozenset[str] = frozenset()
        self._schema_empty = False
        try:
            self._state_root_fd = _open_state_root(self.state_root)
            self._state_root_identity = _identity(os.fstat(self._state_root_fd))
            self._database_fd = self._open_database_file()
            self._acquire_startup_lock()
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
            self._configure_pragmas()
            self._enforce_sidecar_modes()
            if self._schema_empty:
                self._initialize_schema()
            self._validate_schema()
            self._assert_database_identity()
            self._release_startup_lock()
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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        database_fd = self._database_fd
        self._database_fd = None
        root_fd = self._state_root_fd
        self._state_root_fd = None
        self._state_root_identity = None
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
        actor: str,
        reason_code: str = "intent_created",
        evidence_ref: str | None = None,
        clock_ns: int | None = None,
    ) -> OperationSnapshot:
        """Atomically persist an operation intent and its first event."""

        operation_id = _require_opaque_identifier(operation_id, "operation_id")
        effect_key = _require_opaque_identifier(effect_key, "effect_key")
        actor = _require_opaque_identifier(actor, "actor")
        reason_code = _require_reason_code(reason_code)
        if evidence_ref is not None:
            evidence_ref = _require_evidence_ref(evidence_ref)
        timestamp = self._timestamp(clock_ns)
        try:
            with self._write_transaction() as connection:
                self._fault("before_intent_insert")
                connection.execute(
                    """
                    INSERT INTO operations(
                        operation_id, effect_key, status, current_attempt,
                        recovery_epoch, created_ns, updated_ns
                    ) VALUES (?, ?, 'INTENT', 0, 0, ?, ?)
                    """,
                    (operation_id, effect_key, timestamp, timestamp),
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
                           recovery_epoch, created_ns, updated_ns
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

    def operation(self, operation_id: str) -> OperationSnapshot | None:
        """Return one immutable operation observation, or ``None`` if absent."""

        operation_id = _require_opaque_identifier(operation_id, "operation_id")
        try:
            connection = self._require_connection()
            row = connection.execute(
                """
                SELECT operation_id, effect_key, status, current_attempt,
                       recovery_epoch, created_ns, updated_ns
                FROM operations
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is None:
                return None
            return self._operation_from_row(row)
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
            return tuple(self._event_from_row(row) for row in rows)
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

    def _require_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise StoreClosedError("coordination store is closed")
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
                    ('store_schema', ?), ('recovery_epoch', 0)
                """,
                (STORE_SCHEMA,),
            )
            connection.execute(f"PRAGMA user_version = {STORE_SCHEMA}")

    def _validate_schema(self) -> None:
        connection = self._require_connection()
        objects = self._schema_objects()
        expected_objects = {
            key: _normalize_sql(sql) for key, sql in _EXPECTED_OBJECT_SQL.items()
        }
        if objects != expected_objects:
            raise StoreSchemaError("SQLite store objects do not match schema")
        metadata = {
            str(row["key"]): row["value"]
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
        ):
            raise StoreSchemaError("SQLite store metadata is invalid")
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != STORE_SCHEMA:
            raise StoreSchemaError("SQLite user_version does not match store schema")
        for table, expected_columns in _EXPECTED_COLUMNS.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual_columns = tuple(
                (
                    str(row["name"]),
                    str(row["type"]).upper(),
                    int(row["notnull"]),
                    int(row["pk"]),
                )
                for row in rows
            )
            if actual_columns != expected_columns:
                raise StoreSchemaError("SQLite store columns do not match schema")
        for table, expected_indexes in _EXPECTED_INDEX_CONTRACT.items():
            actual_indexes = self._index_contract(table)
            if actual_indexes != tuple(sorted(expected_indexes)):
                raise StoreSchemaError("SQLite store indexes do not match schema")
        for table, expected_foreign_keys in _EXPECTED_FOREIGN_KEYS.items():
            actual_foreign_keys = self._foreign_key_contract(table)
            if actual_foreign_keys != tuple(sorted(expected_foreign_keys)):
                raise StoreSchemaError("SQLite store foreign keys do not match schema")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise StoreIntegrityError("SQLite integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise StoreIntegrityError("SQLite foreign_key_check failed")

    def _schema_objects(self) -> dict[tuple[str, str], str]:
        connection = self._require_connection()
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
            sql = row["sql"]
            if not isinstance(sql, str):
                raise StoreSchemaError("SQLite store object SQL is invalid")
            objects[(str(row["type"]), str(row["name"]))] = _normalize_sql(sql)
        return objects

    def _index_contract(
        self, table: str
    ) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
        connection = self._require_connection()
        contracts: list[tuple[int, str, tuple[str, ...]]] = []
        for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
            index_name = str(row["name"])
            columns = tuple(
                str(column["name"])
                for column in connection.execute(
                    f"PRAGMA index_info({index_name})"
                ).fetchall()
            )
            contracts.append((int(row["unique"]), str(row["origin"]), columns))
        return tuple(sorted(contracts))

    def _foreign_key_contract(
        self, table: str
    ) -> tuple[tuple[str, str, str, str, str], ...]:
        connection = self._require_connection()
        contracts = [
            (
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]).upper(),
                str(row[6]).upper(),
            )
            for row in connection.execute(
                f"PRAGMA foreign_key_list({table})"
            ).fetchall()
        ]
        return tuple(sorted(contracts))

    def _assert_transaction_identity(self) -> None:
        self._assert_state_root()
        self._assert_database_identity()
        self._assert_connection_identity()
        self._existing_sidecar_names()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._require_connection()
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
            connection.rollback()
            raise

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
