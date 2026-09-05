from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest import mock

import agent_team.verification_gate as gate
from agent_team import backup, task_verification_ledger, workflow_store
from agent_team import store as store_module
from agent_team.store import (
    _WORKFLOW_EVENTS_NO_UPDATE_TRIGGER_SQL,
    CoordinationStore,
    StoreIntegrityError,
    StoreMigrationRequiredError,
    StoreSchemaError,
    StoreUnavailableError,
)
from agent_team.task_policy import (
    AttemptId,
    ClaimRef,
    DispatchId,
    GitObjectId,
    ReceiptRef,
    TaskId,
    TaskPhase,
    TaskPolicyStateV4,
    TreeDigest,
    WorkspaceIdentity,
)
from agent_team.topology import NodeId, TeamId

PROVIDER_TABLES = frozenset(
    {
        "store_meta",
        "operations",
        "operation_attempts",
        "effect_receipts",
        "transition_events",
    }
)
WORKFLOW_TABLES = frozenset(
    {
        "workflow_checkpoints",
        "workflow_operations",
        "workflow_receipts",
        "workflow_events",
    }
)
VERIFICATION_TABLES = frozenset(
    {
        "task_policy_states",
        "verification_operations",
        "verification_receipts",
    }
)
EXPECTED_TABLES = PROVIDER_TABLES | WORKFLOW_TABLES | VERIFICATION_TABLES
WORKFLOW_ACTIONS = {
    "start",
    "prompt",
    "wait",
    "reply",
    "read",
    "release",
    "ack",
    "stop",
}
VERIFICATION_STATUSES = {
    "PREPARED",
    "EFFECT_PREPARED",
    "RECEIPTED",
    "TERMINAL",
    "UNKNOWN_EFFECT",
}
TASK_STATE_FIELDS = (
    "version",
    "team_id",
    "workspace",
    "sequence",
    "task_id",
    "attempt_id",
    "dispatch_id",
    "worker_node",
    "reviewer_node",
    "review_round",
    "target_head",
    "target_tree_digest",
    "claim_ref",
    "receipt_ref",
    "phase",
)
STAGE_POINTERS = {
    "prepare": ("prepare_event_id", "prepare_event_digest"),
    "receipt": ("receipt_event_id", "receipt_event_digest"),
    "terminal": ("terminal_event_id", "terminal_event_digest"),
    "unknown": ("unknown_event_id", "unknown_event_digest"),
}
EXPECTED_VERIFICATION_COLUMNS = {
    "task_policy_states": (
        ("root_key", "TEXT", 1, 1),
        ("task_id", "TEXT", 1, 2),
        ("version", "INTEGER", 1, 0),
        ("state_codec_version", "INTEGER", 1, 0),
        ("team_id", "TEXT", 1, 0),
        ("workspace", "TEXT", 1, 0),
        ("sequence", "INTEGER", 1, 0),
        ("attempt_id", "TEXT", 0, 0),
        ("dispatch_id", "TEXT", 0, 0),
        ("worker_node", "TEXT", 0, 0),
        ("reviewer_node", "TEXT", 0, 0),
        ("review_round", "INTEGER", 1, 0),
        ("target_head", "TEXT", 0, 0),
        ("target_tree_digest", "TEXT", 0, 0),
        ("claim_ref", "TEXT", 0, 0),
        ("receipt_ref", "TEXT", 0, 0),
        ("phase", "TEXT", 1, 0),
        ("state_bytes", "BLOB", 1, 0),
        ("state_digest", "TEXT", 1, 0),
        ("run_id", "TEXT", 1, 0),
        ("updated_ns", "INTEGER", 1, 0),
    ),
    "verification_operations": (
        ("root_key", "TEXT", 1, 1),
        ("verification_ref", "TEXT", 1, 2),
        ("record_version", "INTEGER", 1, 0),
        ("approval_binding_version", "INTEGER", 1, 0),
        ("approval_binding_bytes", "BLOB", 1, 0),
        ("approval_binding_digest", "TEXT", 1, 0),
        ("request_schema_version", "INTEGER", 1, 0),
        ("approval_ref", "TEXT", 1, 0),
        ("approval_digest", "TEXT", 1, 0),
        ("review_ref", "TEXT", 1, 0),
        ("review_digest", "TEXT", 1, 0),
        ("completion_ref", "TEXT", 1, 0),
        ("completion_digest", "TEXT", 1, 0),
        ("request_bytes", "BLOB", 1, 0),
        ("request_digest", "TEXT", 1, 0),
        ("record_digest", "TEXT", 1, 0),
        ("run_id", "TEXT", 1, 0),
        ("main_terminal_id", "TEXT", 1, 0),
        ("task_id", "TEXT", 1, 0),
        ("dispatch_id", "TEXT", 1, 0),
        ("attempt_id", "TEXT", 1, 0),
        ("worker_node", "TEXT", 1, 0),
        ("reviewer_node", "TEXT", 1, 0),
        ("worker_terminal_id", "TEXT", 1, 0),
        ("reviewer_terminal_id", "TEXT", 1, 0),
        ("team_id", "TEXT", 1, 0),
        ("workspace", "TEXT", 1, 0),
        ("review_round", "INTEGER", 1, 0),
        ("task_sequence_before", "INTEGER", 1, 0),
        ("task_sequence_after", "INTEGER", 1, 0),
        ("task_digest_before", "TEXT", 1, 0),
        ("task_digest_after", "TEXT", 1, 0),
        ("workflow_sequence_before", "INTEGER", 1, 0),
        ("workflow_sequence_after", "INTEGER", 1, 0),
        ("workflow_digest_before", "TEXT", 1, 0),
        ("workflow_digest_after", "TEXT", 1, 0),
        ("status", "TEXT", 1, 0),
        ("effect_owner", "TEXT", 0, 0),
        ("effect_attempt", "INTEGER", 0, 0),
        ("effect_epoch", "INTEGER", 0, 0),
        ("effect_fence", "INTEGER", 0, 0),
        ("effect_nonce", "TEXT", 0, 0),
        ("receipt_ref", "TEXT", 0, 0),
        ("receipt_digest", "TEXT", 0, 0),
        ("terminal_phase", "TEXT", 0, 0),
        ("terminal_receipt_ref", "TEXT", 0, 0),
        ("terminal_receipt_digest", "TEXT", 0, 0),
        ("unknown_code", "TEXT", 0, 0),
        ("unknown_evidence_digest", "TEXT", 0, 0),
        ("prepare_event_id", "INTEGER", 0, 0),
        ("prepare_event_digest", "TEXT", 0, 0),
        ("receipt_event_id", "INTEGER", 0, 0),
        ("receipt_event_digest", "TEXT", 0, 0),
        ("terminal_event_id", "INTEGER", 0, 0),
        ("terminal_event_digest", "TEXT", 0, 0),
        ("unknown_event_id", "INTEGER", 0, 0),
        ("unknown_event_digest", "TEXT", 0, 0),
        ("created_ns", "INTEGER", 1, 0),
        ("updated_ns", "INTEGER", 1, 0),
    ),
    "verification_receipts": (
        ("root_key", "TEXT", 1, 1),
        ("receipt_ref", "TEXT", 1, 2),
        ("verification_ref", "TEXT", 1, 0),
        ("receipt_schema_version", "INTEGER", 1, 0),
        ("receipt_bytes", "BLOB", 1, 0),
        ("receipt_digest", "TEXT", 1, 0),
        ("stored_ns", "INTEGER", 1, 0),
    ),
}
EXPECTED_VERIFICATION_INDEXES = {
    "task_policy_states": {(1, "pk", ("root_key", "task_id"))},
    "verification_operations": {
        (1, "pk", ("root_key", "verification_ref")),
        (0, "c", ("root_key", "status", "updated_ns", "verification_ref")),
        (1, "c", ("root_key", "approval_ref")),
    },
    "verification_receipts": {
        (1, "pk", ("root_key", "receipt_ref")),
        (1, "u", ("root_key", "verification_ref")),
    },
}
EXPECTED_VERIFICATION_FOREIGN_KEYS = {
    "task_policy_states": {
        ("workflow_checkpoints", "root_key", "root_key", "RESTRICT", "RESTRICT"),
    },
    "verification_operations": {
        ("workflow_checkpoints", "root_key", "root_key", "RESTRICT", "RESTRICT"),
        ("task_policy_states", "root_key", "root_key", "RESTRICT", "RESTRICT"),
        ("task_policy_states", "task_id", "task_id", "RESTRICT", "RESTRICT"),
        ("verification_receipts", "root_key", "root_key", "RESTRICT", "RESTRICT"),
        (
            "verification_receipts",
            "receipt_ref",
            "receipt_ref",
            "RESTRICT",
            "RESTRICT",
        ),
        (
            "workflow_events",
            "prepare_event_id",
            "workflow_event_id",
            "RESTRICT",
            "RESTRICT",
        ),
        (
            "workflow_events",
            "receipt_event_id",
            "workflow_event_id",
            "RESTRICT",
            "RESTRICT",
        ),
        (
            "workflow_events",
            "terminal_event_id",
            "workflow_event_id",
            "RESTRICT",
            "RESTRICT",
        ),
        (
            "workflow_events",
            "unknown_event_id",
            "workflow_event_id",
            "RESTRICT",
            "RESTRICT",
        ),
    },
    "verification_receipts": {
        ("verification_operations", "root_key", "root_key", "RESTRICT", "RESTRICT"),
        (
            "verification_operations",
            "verification_ref",
            "verification_ref",
            "RESTRICT",
            "RESTRICT",
        ),
    },
}
VERIFICATION_STATUS_GROUP_FIELDS = {
    "effect": (
        "effect_owner",
        "effect_attempt",
        "effect_epoch",
        "effect_fence",
        "effect_nonce",
    ),
    "receipt": ("receipt_ref", "receipt_digest"),
    "terminal": (
        "terminal_phase",
        "terminal_receipt_ref",
        "terminal_receipt_digest",
    ),
    "unknown": ("unknown_code", "unknown_evidence_digest"),
    "prepare_pointer": ("prepare_event_id", "prepare_event_digest"),
    "receipt_pointer": ("receipt_event_id", "receipt_event_digest"),
    "terminal_pointer": ("terminal_event_id", "terminal_event_digest"),
    "unknown_pointer": ("unknown_event_id", "unknown_event_digest"),
}
VERIFICATION_STATUS_REQUIRED_GROUPS = {
    "PREPARED": frozenset({"prepare_pointer"}),
    "EFFECT_PREPARED": frozenset({"prepare_pointer", "effect"}),
    "RECEIPTED": frozenset({"prepare_pointer", "receipt_pointer", "effect", "receipt"}),
    "TERMINAL": frozenset(
        {
            "prepare_pointer",
            "receipt_pointer",
            "terminal_pointer",
            "effect",
            "receipt",
            "terminal",
        }
    ),
    "UNKNOWN_EFFECT": frozenset(
        {"prepare_pointer", "unknown_pointer", "effect", "unknown"}
    ),
}
FORBIDDEN_BODY_COLUMNS = (
    "argv",
    "environment_value",
    "stdout",
    "stderr",
    "prompt",
    "task_body",
    "reviewer_body",
    "agent_body",
    "credential",
    "secret",
    "exception",
    "pid",
)
LEGACY_METADATA_KEYS = frozenset(
    {"store_schema", "recovery_epoch", "fencing_token_floor", "last_clock_ns"}
)
SQLiteRowFactory = (
    type[sqlite3.Row] | Callable[[sqlite3.Cursor, tuple[Any, ...]], object] | None
)

# This is an independent oracle for the clean v3 image at the Issue #80
# implementation boundary.  It is deliberately a digest of a compact
# structural manifest, not an embedded SQLite binary and not a value read
# from production _V3_* constants.
FROZEN_V3_MANIFEST_DIGEST = (
    "sha256:6fd4c60a792f9d6def9f6478cf9a1ee816f5553315091de48e376aa30fb8db87"
)
V3_MANIFEST_CODEC = {
    "checkpoint_version": 4,
    "seed_version": 1,
    "workflow_event_schema_version": 1,
    "provider_event_schema_version": 2,
}
V3_HIGH_WATER_COLUMNS = (
    ("operations", "created_ns"),
    ("operations", "updated_ns"),
    ("operation_attempts", "lease_heartbeat_ns"),
    ("operation_attempts", "lease_expires_ns"),
    ("operation_attempts", "effect_started_ns"),
    ("operation_attempts", "fence_started_ns"),
    ("effect_receipts", "received_ns"),
    ("transition_events", "clock_ns"),
    ("workflow_checkpoints", "updated_ns"),
    ("workflow_operations", "created_ns"),
    ("workflow_operations", "updated_ns"),
    ("workflow_receipts", "issued_ns"),
    ("workflow_events", "clock_ns"),
)
V3_TABLE_ORDER = (
    "effect_receipts",
    "operation_attempts",
    "operations",
    "store_meta",
    "transition_events",
    "workflow_checkpoints",
    "workflow_events",
    "workflow_operations",
    "workflow_receipts",
)


def _state_root(parent: str, name: str = "state") -> Path:
    root = Path(os.path.realpath(parent)) / name
    root.mkdir()
    root.chmod(0o700)
    return root


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.strip().split())


def _schema_objects(connection: sqlite3.Connection) -> list[list[str | None]]:
    return [
        [str(row[0]), str(row[1]), _normalize_sql(str(row[2])) if row[2] else None]
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_autoindex_%' ORDER BY type, name"
        )
    ]


def _columns(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[str, str, int, int], ...]:
    return tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    )


def _indexes(
    connection: sqlite3.Connection, table: str
) -> list[tuple[int, str, tuple[str, ...]]]:
    indexes: list[tuple[int, str, tuple[str, ...]]] = []
    for row in connection.execute(f'PRAGMA index_list("{table}")'):
        name = str(row[1])
        columns = [
            str(index_row[2])
            for index_row in connection.execute(f'PRAGMA index_info("{name}")')
        ]
        indexes.append((int(row[2]), str(row[3]), tuple(columns)))
    indexes.sort(key=lambda item: (item[2], item[1], item[0]))
    return indexes


def _foreign_keys(connection: sqlite3.Connection, table: str) -> list[list[str]]:
    rows = [
        [
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]).upper(),
            str(row[6]).upper(),
        ]
        for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
    ]
    rows.sort()
    return rows


def _v3_manifest(connection: sqlite3.Connection) -> dict[str, object]:
    tables: list[list[object]] = []
    for table in V3_TABLE_ORDER:
        tables.append(
            [
                table,
                [list(column) for column in _columns(connection, table)],
                _indexes(connection, table),
                _foreign_keys(connection, table),
            ]
        )
    metadata = [
        [str(row[0]), int(row[1])]
        for row in connection.execute("SELECT key, value FROM store_meta ORDER BY key")
    ]
    row_counts = [
        [
            table,
            int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]),
        ]
        for table in V3_TABLE_ORDER
    ]
    high_water = {
        f"{table}.{column}": int(
            connection.execute(
                f'SELECT COALESCE(MAX("{column}"), 0) FROM "{table}"'
            ).fetchone()[0]
        )
        for table, column in V3_HIGH_WATER_COLUMNS
    }
    return {
        "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        "objects": _schema_objects(connection),
        "tables": tables,
        "metadata": metadata,
        "row_counts": row_counts,
        "highwater": high_water,
        "codec": V3_MANIFEST_CODEC,
    }


def _manifest_digest(connection: sqlite3.Connection) -> str:
    encoded = json.dumps(
        _v3_manifest(connection),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _root_snapshot(
    root: Path,
) -> tuple[tuple[str, bool, int | None, bytes | None], ...]:
    paths = (
        root / store_module.DATABASE_FILENAME,
        root / store_module.WRITER_MARKER_FILENAME,
        root.parent / store_module.LIFETIME_GATE_FILENAME,
    )
    snapshot: list[tuple[str, bool, int | None, bytes | None]] = []
    for path in paths:
        try:
            metadata = path.stat()
        except FileNotFoundError:
            snapshot.append((str(path), False, None, None))
        else:
            snapshot.append((str(path), True, metadata.st_mode, path.read_bytes()))
    return tuple(snapshot)


def _foundation_snapshot(
    root: Path,
) -> tuple[tuple[str, bool, int | None, bytes | None], ...]:
    """Capture the fileset that a pre-gate classifier must leave unchanged."""

    paths = (
        root / store_module.DATABASE_FILENAME,
        root / f"{store_module.DATABASE_FILENAME}-wal",
        root / f"{store_module.DATABASE_FILENAME}-shm",
        root / store_module.WRITER_MARKER_FILENAME,
        root.parent / store_module.LIFETIME_GATE_FILENAME,
    )
    snapshot: list[tuple[str, bool, int | None, bytes | None]] = []
    for path in paths:
        try:
            metadata = path.stat()
        except FileNotFoundError:
            snapshot.append((str(path), False, None, None))
        else:
            snapshot.append((str(path), True, metadata.st_mode, path.read_bytes()))
    return tuple(snapshot)


def _root_inventory(root: Path) -> tuple[tuple[str, int, bytes], ...]:
    """Capture every regular entry in a test state root by name and bytes."""

    entries: list[tuple[str, int, bytes]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        metadata = path.stat(follow_symlinks=False)
        if not path.is_file():
            raise AssertionError(f"test state root entry is not a regular file: {path}")
        entries.append((path.name, metadata.st_mode, path.read_bytes()))
    return tuple(entries)


class _WorkflowFetchallGuardCursor:
    """Fail if workflow validation materializes a whole table result."""

    def __init__(self, cursor: Any, sql: str) -> None:
        self._cursor = cursor
        self._sql = " ".join(sql.split()).lower()

    def fetchall(self) -> Any:
        if self._sql.startswith("select") and " from workflow_" in self._sql:
            raise AssertionError(
                "workflow validation must stream rows instead of fetchall"
            )
        return self._cursor.fetchall()

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def __iter__(self) -> Any:
        return iter(self._cursor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _WorkflowFetchallGuardConnection:
    """Delegate a real SQLite connection while guarding workflow fetches."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @property
    def row_factory(self) -> SQLiteRowFactory:
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value: SQLiteRowFactory) -> None:
        self._connection.row_factory = value

    def execute(self, sql: str, parameters: Any = ()) -> _WorkflowFetchallGuardCursor:
        return _WorkflowFetchallGuardCursor(
            self._connection.execute(sql, parameters),
            sql,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _assert_injection_preserved_lifecycle_files(
    testcase: unittest.TestCase,
    before: tuple[tuple[str, bool, int | None, bytes | None], ...],
    after: tuple[tuple[str, bool, int | None, bytes | None], ...],
) -> None:
    """Keep writer-marker and lifetime-gate bytes outside the SQL fixture."""

    testcase.assertEqual(
        before[3:],
        after[3:],
        "non-empty-row fixture changed the writer marker or lifetime gate",
    )


def _mapping_from_module(
    testcase: unittest.TestCase,
    module: Any,
    names: tuple[str, ...],
    label: str,
) -> Mapping[object, object]:
    for name in names:
        value = getattr(module, name, None)
        if isinstance(value, Mapping) and value:
            return value
    testcase.fail(f"{label} legacy schema object mapping is missing")
    raise AssertionError("unreachable")


def _legacy_object_sql(
    testcase: unittest.TestCase, schema_version: int
) -> dict[tuple[str, str], str]:
    object_names: tuple[str, ...]
    table_names: tuple[str, ...]
    index_names: tuple[str, ...]
    trigger_names: tuple[str, ...]
    if schema_version == 2:
        object_names = ("_V2_EXPECTED_OBJECT_SQL", "_V2_OBJECT_SQL")
        table_names = ("_V2_TABLE_DEFINITIONS",)
        index_names = ("_V2_INDEX_DEFINITIONS",)
        trigger_names = ("_V2_TRIGGER_DEFINITIONS",)
    else:
        object_names = ("_V3_EXPECTED_OBJECT_SQL", "_V3_OBJECT_SQL")
        table_names = ("_V3_TABLE_DEFINITIONS",)
        index_names = ("_V3_INDEX_DEFINITIONS",)
        trigger_names = ("_V3_TRIGGER_DEFINITIONS",)
        if store_module.STORE_SCHEMA == 3:
            object_names += ("_EXPECTED_OBJECT_SQL",)
            table_names += ("_TABLE_DEFINITIONS",)
            index_names += ("_INDEX_DEFINITIONS",)
            trigger_names += ("_TRIGGER_DEFINITIONS",)

    objects: dict[tuple[str, str], str] = {}
    for schema_object_name in object_names:
        value = getattr(store_module, schema_object_name, None)
        if isinstance(value, Mapping) and value:
            for key, schema_sql in value.items():
                if (
                    isinstance(key, tuple)
                    and len(key) == 2
                    and all(isinstance(item, str) for item in key)
                    and isinstance(schema_sql, str)
                ):
                    objects[(key[0], key[1])] = schema_sql
            if objects:
                return objects

    for kind, names in (
        ("table", table_names),
        ("index", index_names),
        ("trigger", trigger_names),
    ):
        value = _mapping_from_module(
            testcase, store_module, names, f"v{schema_version}"
        )
        for object_name, schema_sql in value.items():
            if isinstance(object_name, str) and isinstance(schema_sql, str):
                objects[(kind, object_name)] = schema_sql
    if not objects:
        testcase.fail(f"v{schema_version} legacy schema object mapping is empty")
    return objects


def _make_legacy_state(
    testcase: unittest.TestCase,
    parent: str,
    schema_version: int,
    name: str,
) -> Path:
    root = _state_root(parent, name)
    objects = _legacy_object_sql(testcase, schema_version)
    database = root / store_module.DATABASE_FILENAME
    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        for kind in ("table", "index", "trigger"):
            for (object_kind, _), sql in sorted(objects.items()):
                if object_kind == kind:
                    connection.execute(sql)
        connection.executemany(
            "INSERT INTO store_meta(key, value) VALUES (?, ?)",
            (
                ("store_schema", schema_version),
                ("recovery_epoch", 0),
                ("fencing_token_floor", 0),
                ("last_clock_ns", 0),
            ),
        )
        connection.execute(f"PRAGMA user_version = {schema_version}")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    database.chmod(0o600)
    marker = root / store_module.WRITER_MARKER_FILENAME
    marker.write_bytes(store_module.WRITER_MARKER_CLEAN_CONTENT)
    marker.chmod(0o600)
    return root


def _legacy_json(value: Mapping[str, object]) -> bytes:
    """Encode the historical v3 fixture without using the current codec."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _legacy_digest(domain: bytes, value: bytes) -> str:
    return "sha256:" + hashlib.sha256(domain + value).hexdigest()


def _legacy_mapping_digest(domain: bytes, value: Mapping[str, object]) -> str:
    return _legacy_digest(domain, _legacy_json(value))


def _insert_legacy_row(
    connection: sqlite3.Connection,
    table: str,
    values: Mapping[str, object],
) -> None:
    columns = tuple(values)
    connection.execute(
        f'INSERT INTO "{table}" ({", ".join(columns)}) '
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )


def _make_nonempty_v2_state(
    testcase: unittest.TestCase,
    parent: str,
    name: str,
) -> Path:
    """Create a frozen v2 provider image with one transition event."""

    root = _make_legacy_state(testcase, parent, 2, name)
    connection = sqlite3.connect(
        str(root / store_module.DATABASE_FILENAME),
        isolation_level=None,
    )
    try:
        connection.execute(
            "UPDATE store_meta SET value = 10 WHERE key = 'last_clock_ns'"
        )
        _insert_legacy_row(
            connection,
            "operations",
            {
                "operation_id": "operation-v2",
                "effect_key": "effect/v2",
                "status": "INTENT",
                "provider_id": None,
                "current_attempt": 0,
                "recovery_epoch": 0,
                "created_ns": 10,
                "updated_ns": 10,
            },
        )
        _insert_legacy_row(
            connection,
            "operation_attempts",
            {
                "operation_id": "operation-v2",
                "attempt": 0,
                "owner": None,
                "provider_id": None,
                "lease_epoch": 0,
                "fencing_token": 0,
                "lease_heartbeat_ns": None,
                "lease_expires_ns": None,
                "fence_proof_version": None,
                "fence_proof_ref": None,
                "effect_started_ns": None,
                "fence_started_ns": None,
            },
        )
        _insert_legacy_row(
            connection,
            "transition_events",
            {
                "event_id": 1,
                "event_schema_version": 2,
                "operation_id": "operation-v2",
                "attempt": 0,
                "from_status": None,
                "to_status": "INTENT",
                "kind": "intent",
                "actor": "actor-v2",
                "clock_ns": 10,
                "reason_code": "intent_created",
                "evidence_ref": None,
            },
        )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    return root


def _make_nonempty_v3_state(
    testcase: unittest.TestCase,
    parent: str,
    name: str,
) -> Path:
    """Create a valid start/commit image from the frozen v3 wire contract."""

    root = _make_legacy_state(testcase, parent, 3, name)
    workspace = root.parent / f"{name}-workspace"
    workspace.mkdir()
    workspace.chmod(0o700)
    config = root.parent / f"{name}-config.toml"
    config_bytes = b"team = 'team-1'\n"
    config.write_bytes(config_bytes)
    config.chmod(0o600)
    workspace_stat = workspace.stat()
    config_stat = config.stat()
    root_stat = root.stat()
    root_mapping: dict[str, object] = {
        "root_key": "root-1",
        "team_id": "team-1",
        "workspace_path": str(workspace),
        "workspace_device": int(workspace_stat.st_dev),
        "workspace_inode": int(workspace_stat.st_ino),
        "config_device": int(config_stat.st_dev),
        "config_inode": int(config_stat.st_ino),
        "config_digest": _legacy_digest(
            b"agent-team/workflow-config/v1\0", config_bytes
        ),
        "state_root_device": int(root_stat.st_dev),
        "state_root_inode": int(root_stat.st_ino),
        "config_path": str(config),
        "state_root": str(root),
    }
    request_digest = _legacy_digest(
        b"agent-team/workflow-request/v1\0", b"start-request"
    )
    evidence_ref = "sha256:" + "1" * 64
    intent_mapping: dict[str, object] = {
        "operation_id": "operation-start",
        "effect_key": "effect/start",
        "root_key": "root-1",
        "root": root_mapping,
        "action": "start",
        "request_digest": request_digest,
        "expected_workflow_sequence": 0,
        "expected_task_sequence": None,
        "intent_sequence": 1,
        "next_task_sequence": None,
        "run_id": None,
        "main_terminal_id": None,
        "task_id": None,
        "dispatch_id": None,
        "attempt": None,
        "terminal_id": None,
        "delivery_id": None,
        "message_id": None,
        "consumer_generation": 0,
        "owner": "owner-1",
        "lease_epoch": 0,
        "fencing_token": 0,
        "actor": "actor-1",
        "evidence_ref": evidence_ref,
    }
    intent_digest = _legacy_mapping_digest(
        b"agent-team/workflow-intent/v1\0", intent_mapping
    )
    seed_body: dict[str, object] = {
        "seed_version": 1,
        "checkpoint_version": 4,
        "store_schema": 3,
        "root": root_mapping,
        "workflow_sequence": 1,
        "operation_id": "operation-start",
        "operation_status": "INTENT",
        "workflow_state": "STARTING",
        "updated_ns": 10,
    }
    seed_digest = _legacy_mapping_digest(b"agent-team/workflow-seed/v1\0", seed_body)
    seed_with_digest = dict(seed_body)
    seed_with_digest["seed_digest"] = seed_digest
    seed_fields = (
        "seed_version",
        "checkpoint_version",
        "store_schema",
        "root",
        "workflow_sequence",
        "operation_id",
        "operation_status",
        "workflow_state",
        "seed_digest",
        "updated_ns",
    )
    seed_bytes = _legacy_json({field: seed_with_digest[field] for field in seed_fields})
    receipt_mapping: dict[str, object] = {
        "receipt_id": "receipt-start",
        "operation_id": "operation-start",
        "effect_key": "effect/start",
        "receipt_schema_version": 1,
        "action": "start",
        "request_digest": request_digest,
        "root_key": "root-1",
        "run_id": "run-1",
        "main_terminal_id": "terminal-main",
        "task_id": None,
        "dispatch_id": None,
        "attempt": None,
        "terminal_id": None,
        "delivery_id": None,
        "message_id": None,
        "consumer_generation": 0,
        "owner": "owner-1",
        "lease_epoch": 0,
        "fencing_token": 0,
        "effect_ref": "backend/receipt-start",
        "result_kind": "lifecycle",
        "result_digest": "sha256:" + "3" * 64,
        "evidence_ref": "sha256:" + "4" * 64,
        "issued_ns": 50,
    }
    receipt_digest = _legacy_mapping_digest(
        b"agent-team/workflow-receipt/v1\0", receipt_mapping
    )
    last_operation: dict[str, object] = {
        "operation_id": "operation-start",
        "effect_key": "effect/start",
        "action": "start",
        "request_digest": request_digest,
        "expected_workflow_sequence": 0,
        "expected_task_sequence": None,
        "status": "COMMITTED",
        "receipt_id": "receipt-start",
        "receipt_digest": receipt_digest,
    }
    checkpoint_body: dict[str, object] = {
        "checkpoint_version": 4,
        "store_schema": 3,
        "task_policy_version": None,
        "root": root_mapping,
        "run": {
            "run_id": "run-1",
            "main_terminal_id": "terminal-main",
            "consumer_generation": 0,
        },
        "workflow_sequence": 2,
        "task_sequence": None,
        "execution_mode": "serial",
        "workflow_state": "IDLE",
        "task_policy": None,
        "active_assignment": None,
        "pending_delivery": None,
        "replied_message_ids": [],
        "read_observed": False,
        "released": False,
        "review_authority": None,
        "verification_authority": None,
        "last_operation": last_operation,
        "updated_ns": 20,
    }
    checkpoint_digest = _legacy_mapping_digest(
        b"agent-team/workflow-checkpoint/v4\0", checkpoint_body
    )
    checkpoint_with_digest = dict(checkpoint_body)
    checkpoint_with_digest["checkpoint_digest"] = checkpoint_digest
    checkpoint_fields = (
        "checkpoint_version",
        "store_schema",
        "task_policy_version",
        "root",
        "run",
        "workflow_sequence",
        "task_sequence",
        "execution_mode",
        "workflow_state",
        "task_policy",
        "active_assignment",
        "pending_delivery",
        "replied_message_ids",
        "read_observed",
        "released",
        "review_authority",
        "verification_authority",
        "last_operation",
        "checkpoint_digest",
        "updated_ns",
    )
    checkpoint_bytes = _legacy_json(
        {field: checkpoint_with_digest[field] for field in checkpoint_fields}
    )
    operation: dict[str, object] = {
        "operation_id": "operation-start",
        "effect_key": "effect/start",
        "root_key": "root-1",
        "action": "start",
        "request_digest": request_digest,
        "expected_workflow_sequence": 0,
        "expected_task_sequence": None,
        "intent_sequence": 1,
        "next_task_sequence": None,
        "run_id": "run-1",
        "main_terminal_id": "terminal-main",
        "task_id": None,
        "dispatch_id": None,
        "attempt": None,
        "terminal_id": None,
        "delivery_id": None,
        "message_id": None,
        "consumer_generation": 0,
        "owner": "owner-1",
        "lease_epoch": 0,
        "fencing_token": 0,
        "status": "COMMITTED",
        "receipt_id": "receipt-start",
        "created_ns": 10,
        "updated_ns": 20,
        "intent_digest": intent_digest,
        "receipt_digest": receipt_digest,
        "evidence_ref": evidence_ref,
    }

    def event_digest(
        event_id: int,
        sequence: int,
        from_state: str,
        to_state: str,
        receipt_id: str | None,
        checkpoint: bytes,
        checkpoint_digest_value: str,
        clock_ns: int,
    ) -> str:
        return _legacy_mapping_digest(
            b"agent-team/workflow-event/v1\0",
            {
                "workflow_event_id": event_id,
                "workflow_event_schema_version": 1,
                "root_key": "root-1",
                "operation_id": "operation-start",
                "workflow_sequence": sequence,
                "task_sequence_before": None,
                "task_sequence_after": None,
                "from_state": from_state,
                "to_state": to_state,
                "kind": "start",
                "actor": "actor-1",
                "clock_ns": clock_ns,
                "request_digest": request_digest,
                "receipt_id": receipt_id,
                "checkpoint_bytes": checkpoint.decode("utf-8"),
                "checkpoint_digest": checkpoint_digest_value,
                "evidence_ref": evidence_ref,
            },
        )

    events = (
        {
            "workflow_event_id": 1,
            "workflow_event_schema_version": 1,
            "root_key": "root-1",
            "operation_id": "operation-start",
            "workflow_sequence": 1,
            "task_sequence_before": None,
            "task_sequence_after": None,
            "from_state": "STARTING",
            "to_state": "STARTING",
            "kind": "start",
            "actor": "actor-1",
            "clock_ns": 10,
            "request_digest": request_digest,
            "receipt_id": None,
            "checkpoint_bytes": seed_bytes,
            "checkpoint_digest": seed_digest,
            "evidence_ref": evidence_ref,
            "event_digest": event_digest(
                1,
                1,
                "STARTING",
                "STARTING",
                None,
                seed_bytes,
                seed_digest,
                10,
            ),
        },
        {
            "workflow_event_id": 2,
            "workflow_event_schema_version": 1,
            "root_key": "root-1",
            "operation_id": "operation-start",
            "workflow_sequence": 2,
            "task_sequence_before": None,
            "task_sequence_after": None,
            "from_state": "STARTING",
            "to_state": "IDLE",
            "kind": "start",
            "actor": "actor-1",
            "clock_ns": 20,
            "request_digest": request_digest,
            "receipt_id": "receipt-start",
            "checkpoint_bytes": checkpoint_bytes,
            "checkpoint_digest": checkpoint_digest,
            "evidence_ref": evidence_ref,
            "event_digest": event_digest(
                2,
                2,
                "STARTING",
                "IDLE",
                "receipt-start",
                checkpoint_bytes,
                checkpoint_digest,
                20,
            ),
        },
    )
    connection = sqlite3.connect(
        str(root / store_module.DATABASE_FILENAME),
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "UPDATE store_meta SET value = 50 WHERE key = 'last_clock_ns'"
        )
        _insert_legacy_row(
            connection,
            "workflow_checkpoints",
            {
                "root_key": "root-1",
                "team_id": "team-1",
                "workspace_path": root_mapping["workspace_path"],
                "workspace_device": root_mapping["workspace_device"],
                "workspace_inode": root_mapping["workspace_inode"],
                "config_path": root_mapping["config_path"],
                "config_device": root_mapping["config_device"],
                "config_inode": root_mapping["config_inode"],
                "config_digest": root_mapping["config_digest"],
                "state_root": root_mapping["state_root"],
                "state_root_device": root_mapping["state_root_device"],
                "state_root_inode": root_mapping["state_root_inode"],
                "run_id": "run-1",
                "main_terminal_id": "terminal-main",
                "checkpoint_version": 4,
                "store_schema": 3,
                "task_policy_version": None,
                "workflow_sequence": 2,
                "task_sequence": None,
                "execution_mode": "serial",
                "workflow_state": "IDLE",
                "consumer_generation": 0,
                "read_observed": 0,
                "released": 0,
                "checkpoint_bytes": checkpoint_bytes,
                "checkpoint_digest": checkpoint_digest,
                "last_operation_id": "operation-start",
                "last_operation_status": "COMMITTED",
                "last_operation_receipt_id": "receipt-start",
                "updated_ns": 20,
            },
        )
        _insert_legacy_row(connection, "workflow_operations", operation)
        _insert_legacy_row(
            connection,
            "workflow_receipts",
            {
                column: value
                for column, value in receipt_mapping.items()
                if column != "root_key"
            },
        )
        for event in events:
            _insert_legacy_row(connection, "workflow_events", event)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    return root


def _mutate_legacy_marker(root: Path, variant: str) -> None:
    database = root / store_module.DATABASE_FILENAME
    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        if variant == "marker-only":
            connection.execute("PRAGMA user_version = 4")
        elif variant == "mixed":
            connection.execute(
                "UPDATE store_meta SET value = 4 WHERE key = 'store_schema'"
            )
        elif variant == "malformed":
            connection.execute("DROP INDEX workflow_events_operation_idx")
        elif variant == "missing-table":
            connection.execute("DROP TABLE workflow_events")
        elif variant == "missing-index":
            connection.execute("DROP INDEX workflow_events_operation_idx")
        elif variant == "missing-trigger":
            connection.execute("DROP TRIGGER workflow_events_no_update")
        elif variant == "future":
            connection.execute(
                "UPDATE store_meta SET value = 99 WHERE key = 'store_schema'"
            )
            connection.execute("PRAGMA user_version = 99")
        elif variant == "extra-object":
            connection.execute("CREATE TABLE unexpected_schema_object (value TEXT)")
        else:
            raise AssertionError(f"unknown legacy image variant: {variant}")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def _sqlite_sql(connection: sqlite3.Connection, kind: str, name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        (kind, name),
    ).fetchone()
    return "" if row is None or row[0] is None else str(row[0])


def _column_names(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(column[0] for column in _columns(connection, table))


def _assert_composite_index(
    testcase: unittest.TestCase,
    connection: sqlite3.Connection,
    table: str,
    expected: tuple[str, ...],
    *,
    unique: bool,
    origin: str | None = None,
) -> None:
    observed = {
        (bool(item[0]), str(item[1]), tuple(str(column) for column in item[2]))
        for item in _indexes(connection, table)
    }
    matches = {item for item in observed if item[2] == expected and item[0] == unique}
    if origin is not None:
        matches = {item for item in matches if item[1] == origin}
    testcase.assertTrue(
        matches,
        f"{table} lacks {'unique ' if unique else ''}index {expected!r}",
    )


def _assert_foreign_key(
    testcase: unittest.TestCase,
    connection: sqlite3.Connection,
    table: str,
    target: str,
    pairs: tuple[tuple[str, str], ...],
) -> None:
    grouped: dict[int, list[tuple[str, str]]] = {}
    actions: dict[int, tuple[str, str, str]] = {}
    for row in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
        identifier = int(row[0])
        grouped.setdefault(identifier, []).append((str(row[3]), str(row[4])))
        actions[identifier] = (str(row[2]), str(row[5]).upper(), str(row[6]).upper())
    for identifier, actual_pairs in grouped.items():
        actual_target, on_update, on_delete = actions[identifier]
        if actual_target == target and tuple(sorted(actual_pairs)) == tuple(
            sorted(pairs)
        ):
            testcase.assertEqual("RESTRICT", on_update, f"{table} FK update action")
            testcase.assertEqual("RESTRICT", on_delete, f"{table} FK delete action")
            return
    testcase.fail(f"{table} lacks FK to {target} for {pairs!r}")


_SCHEMA4_DIGEST_1 = "sha256:" + "1" * 64
_SCHEMA4_DIGEST_2 = "sha256:" + "2" * 64
_SCHEMA4_DIGEST_3 = "sha256:" + "3" * 64
_SCHEMA4_DIGEST_4 = "sha256:" + "4" * 64
_FOUNDATION_NOT_READY_MESSAGE = (
    "SQLite schema-4 verification ledger validation is unavailable"
)


def _schema4_root(parent: str, state_root: Path) -> workflow_store.RootIdentity:
    """Create one real filesystem-backed workflow root identity."""

    workspace = Path(parent) / "schema4-workspace"
    workspace.mkdir()
    workspace.chmod(0o700)
    config = workspace / "config.toml"
    config_bytes = b"[team]\nid = 'team-1'\n"
    config.write_bytes(config_bytes)
    config.chmod(0o600)
    workspace_stat = workspace.stat()
    config_stat = config.stat()
    state_stat = state_root.stat()
    return workflow_store.RootIdentity(
        root_key="root-1",
        team_id="team-1",
        workspace=workflow_store.PathIdentity(
            path=str(workspace),
            device=int(workspace_stat.st_dev),
            inode=int(workspace_stat.st_ino),
        ),
        config_path=str(config),
        config_device=int(config_stat.st_dev),
        config_inode=int(config_stat.st_ino),
        config_digest=workflow_store.config_content_digest(config_bytes),
        state_root=workflow_store.PathIdentity(
            path=str(state_root),
            device=int(state_stat.st_dev),
            inode=int(state_stat.st_ino),
        ),
    )


def _start_schema4_workflow(
    state_root: Path,
    root: workflow_store.RootIdentity,
    *,
    operation_id: str,
    receipt_id: str,
) -> None:
    """Build workflow rows through the typed Store facade."""

    intent = workflow_store.OperationIntent(
        operation_id=operation_id,
        effect_key=f"effect/{operation_id}",
        root_key=root.root_key,
        root=root,
        action=workflow_store.OperationAction.START,
        request_digest=workflow_store.digest_bounded_body(
            f"request/{operation_id}".encode(),
            domain=workflow_store.REQUEST_DIGEST_DOMAIN,
        ),
        expected_workflow_sequence=0,
        expected_task_sequence=None,
        run_id=None,
        main_terminal_id=None,
        task_id=None,
        dispatch_id=None,
        attempt=None,
        terminal_id=None,
        delivery_id=None,
        message_id=None,
        consumer_generation=0,
        owner=f"owner-{root.root_key}",
        lease_epoch=0,
        fencing_token=0,
        actor=f"actor-{root.root_key}",
        evidence_ref=_SCHEMA4_DIGEST_1,
    )
    with CoordinationStore(state_root) as store:
        begun = store.begin_operation(
            intent,
            expected_workflow_sequence=0,
            expected_task_sequence=None,
        )
        if type(begun) is not workflow_store.OperationBegin:
            raise AssertionError("typed workflow fixture did not issue an operation")
        receipt = store._issue_workflow_receipt(
            operation=begun.operation,
            receipt_id=receipt_id,
            run_id="run-1",
            main_terminal_id="terminal-main",
            consumer_generation=0,
            task_id=None,
            dispatch_id=None,
            attempt=None,
            terminal_id=None,
            delivery_id=None,
            message_id=None,
            effect_ref=f"workflow/{receipt_id}",
            result_kind="started",
            result_digest=_SCHEMA4_DIGEST_2,
            evidence_ref=_SCHEMA4_DIGEST_3,
            issued_ns=20,
        )
        draft = workflow_store.WorkflowCheckpointDraft(
            root=root,
            run=workflow_store.RunIdentity("run-1", "terminal-main", 0),
            workflow_sequence=2,
            task_sequence=None,
            execution_mode=workflow_store.ExecutionMode.SERIAL,
            workflow_state=workflow_store.CheckpointState.IDLE,
            task_policy=None,
            active_assignment=None,
            pending_delivery=None,
            replied_message_ids=(),
            read_observed=False,
            released=False,
            review_authority=None,
            verification_authority=None,
            last_operation=workflow_store.LastOperation(
                operation_id=receipt.operation_id,
                effect_key=receipt.effect_key,
                action=receipt.action,
                request_digest=receipt.request_digest,
                expected_workflow_sequence=0,
                expected_task_sequence=None,
                status=workflow_store.OperationStatus.COMMITTED,
                receipt_id=receipt.receipt_id,
                receipt_digest=workflow_store.durable_receipt_digest(receipt),
            ),
        )
        committed = store.commit_effect(begun.operation, receipt, draft)
        if type(committed) is not workflow_store.WorkflowCommit:
            raise AssertionError("typed workflow fixture did not commit a start")


def _schema4_workflow_image(
    parent: str, *, two_roots: bool = False
) -> tuple[Path, workflow_store.RootIdentity, workflow_store.RootIdentity | None]:
    """Create valid workflow rows; later SQL writes are corruption injection only."""

    state_root = _state_root(parent)
    root = _schema4_root(parent, state_root)
    _start_schema4_workflow(
        state_root,
        root,
        operation_id="workflow-start-1",
        receipt_id="workflow-receipt-1",
    )
    second_root: workflow_store.RootIdentity | None = None
    if two_roots:
        second_root = replace(root, root_key="root-2")
        _start_schema4_workflow(
            state_root,
            second_root,
            operation_id="workflow-start-2",
            receipt_id="workflow-receipt-2",
        )
    return state_root, root, second_root


def _typed_verification_payloads(
    root: workflow_store.RootIdentity,
) -> tuple[
    task_verification_ledger.ApprovalBindingSnapshotV1,
    TaskPolicyStateV4,
    bytes,
    str,
    Any,
    Any,
    bytes,
    str,
    bytes,
    gate.VerificationEffectLease,
    gate.VerificationRunResult,
]:
    """Use #51 and the Issue #80 codecs for a canonical typed fixture."""

    gate_fixture = importlib.import_module("tests.test_verification_gate")
    base_approved = gate_fixture.approved_review()
    approved_values = {
        name: getattr(base_approved, name)
        for name in gate.ApprovedReview.__dataclass_fields__
        if not name.startswith("_") and name != "authority_digest"
    }
    approved_values["workspace"] = root.workspace_path
    approved = gate._make_approved(**approved_values)
    bound = gate._make_bound_approval(gate.ApprovalRef(approved.approval_ref), approved)
    profile = gate_fixture.profile()
    before_snapshot = gate_fixture.snapshot(approved)
    request = gate._build_request(bound, profile, before_snapshot)
    effect = gate._make_effect(
        gate.VerificationRef(request.verification_id),
        request.request_digest,
        gate.EffectNonce("effect-nonce"),
        1,
        1,
        gate.EffectBeginStatus.RUN_ONCE,
    )
    result = gate_fixture.FakeRunner().run(request, effect)
    receipt = gate._make_receipt(
        receipt_ref=ReceiptRef("receipt-1"),
        request=request,
        result=result,
        effect=effect,
        after_snapshot=before_snapshot,
    )
    task_state = TaskPolicyStateV4(
        version=4,
        team_id=TeamId(root.team_id),
        workspace=WorkspaceIdentity(approved.workspace),
        sequence=int(approved.approval_sequence),
        task_id=TaskId(str(approved.task_id)),
        attempt_id=AttemptId(str(approved.attempt_id)),
        dispatch_id=DispatchId(str(approved.dispatch_id)),
        worker_node=NodeId(str(approved.worker_node)),
        reviewer_node=NodeId(str(approved.reviewer_node)),
        review_round=int(approved.review_round),
        target_head=GitObjectId(str(approved.target_head)),
        target_tree_digest=TreeDigest(str(approved.target_tree_digest)),
        claim_ref=ClaimRef(str(approved.claim_ref)),
        receipt_ref=None,
        phase=TaskPhase.APPROVED,
    )
    task_bytes = task_verification_ledger.encode_task_state(task_state)
    task_digest = task_verification_ledger.task_state_digest(task_bytes)
    projection = task_verification_ledger._projection_from_approved(approved)
    snapshot_payload = task_verification_ledger._snapshot_payload(
        version=1,
        review_ref="review-schema4",
        review_digest="a" * 64,
        completion_ref="completion-schema4",
        completion_digest="b" * 64,
        approval_ref=str(approved.approval_ref),
        approval_digest=str(approved.authority_digest),
        approved_review=projection,
        task_state_bytes=task_bytes,
        task_state_digest=task_digest,
        root_key=root.root_key,
        run_id=str(approved.run_id),
        main_terminal_id="terminal-main",
        consumer_generation=0,
        workflow_sequence=2,
        workflow_checkpoint_digest=_SCHEMA4_DIGEST_4,
        task_sequence=task_state.sequence,
        effect_owner="effect-owner",
    )
    snapshot = task_verification_ledger.ApprovalBindingSnapshotV1(
        version=1,
        review_ref="review-schema4",
        review_digest="a" * 64,
        completion_ref="completion-schema4",
        completion_digest="b" * 64,
        approval_ref=str(approved.approval_ref),
        approval_digest=str(approved.authority_digest),
        approved_review=projection,
        task_state_bytes=task_bytes,
        task_state_digest=task_digest,
        binding_digest=task_verification_ledger._snapshot_digest(snapshot_payload),
        root_key=root.root_key,
        run_id=str(approved.run_id),
        main_terminal_id="terminal-main",
        consumer_generation=0,
        workflow_sequence=2,
        workflow_checkpoint_digest=_SCHEMA4_DIGEST_4,
        task_sequence=task_state.sequence,
        effect_owner="effect-owner",
    )
    request_projection = (
        task_verification_ledger.verification_request_projection_from_request(request)
    )
    request_bytes = task_verification_ledger.encode_verification_request_projection(
        request_projection
    )
    receipt_projection = (
        task_verification_ledger.verification_receipt_projection_from_receipt(receipt)
    )
    receipt_bytes = task_verification_ledger.encode_verification_receipt_projection(
        receipt_projection
    )
    return (
        snapshot,
        task_state,
        task_bytes,
        task_digest,
        request,
        receipt,
        request_bytes,
        str(request.request_digest),
        receipt_bytes,
        effect,
        result,
    )


def _insert_nonempty_ledger_row(
    connection: sqlite3.Connection,
    table: str,
    values: Mapping[str, object],
) -> None:
    """Inject one non-empty ledger row with parameterized SQL only."""

    columns = tuple(values)
    connection.execute(
        f'INSERT INTO "{table}" ({", ".join(columns)}) '
        f"VALUES ({', '.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )


def _task_row_values(
    root: workflow_store.RootIdentity,
    task_state: TaskPolicyStateV4,
    task_bytes: bytes,
    task_digest: str,
    *,
    changes: Mapping[str, object] | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "root_key": root.root_key,
        "task_id": str(task_state.task_id),
        "version": 4,
        "state_codec_version": 1,
        "team_id": str(task_state.team_id),
        "workspace": str(task_state.workspace),
        "sequence": task_state.sequence,
        "attempt_id": str(task_state.attempt_id),
        "dispatch_id": str(task_state.dispatch_id),
        "worker_node": str(task_state.worker_node),
        "reviewer_node": str(task_state.reviewer_node),
        "review_round": task_state.review_round,
        "target_head": str(task_state.target_head),
        "target_tree_digest": str(task_state.target_tree_digest),
        "claim_ref": str(task_state.claim_ref),
        "receipt_ref": None,
        "phase": task_state.phase.value,
        "state_bytes": task_bytes,
        "state_digest": task_digest,
        "run_id": "run-1",
        "updated_ns": 20,
    }
    if changes is not None:
        values.update(changes)
    return values


def _operation_row_values(
    connection: sqlite3.Connection,
    root: workflow_store.RootIdentity,
    task_state: TaskPolicyStateV4,
    task_digest: str,
    snapshot: task_verification_ledger.ApprovalBindingSnapshotV1,
    request: Any,
    request_bytes: bytes,
    request_digest: str,
    *,
    changes: Mapping[str, object] | None = None,
    status: str = "PREPARED",
    root_key: str | None = None,
    verification_ref: str = "verification-1",
) -> dict[str, object]:
    events = connection.execute(
        "SELECT workflow_event_id, event_digest FROM workflow_events "
        "WHERE root_key = ? ORDER BY workflow_event_id",
        (root.root_key,),
    ).fetchall()
    if len(events) < 2:
        raise AssertionError("typed workflow fixture has no event pointers")
    checkpoint = connection.execute(
        "SELECT workflow_sequence, checkpoint_digest FROM workflow_checkpoints "
        "WHERE root_key = ?",
        (root.root_key,),
    ).fetchone()
    if checkpoint is None:
        raise AssertionError("typed workflow fixture has no checkpoint")
    values: dict[str, object] = {
        "root_key": root.root_key if root_key is None else root_key,
        "verification_ref": verification_ref,
        "record_version": 1,
        "approval_binding_version": 1,
        "approval_binding_bytes": task_verification_ledger.encode_approval_binding_snapshot(
            snapshot
        ),
        "approval_binding_digest": task_verification_ledger.approval_binding_snapshot_digest(
            snapshot
        ),
        "request_schema_version": 1,
        "approval_ref": str(request.approval_ref),
        "approval_digest": str(request.approval.authority_digest),
        "review_ref": snapshot.review_ref,
        "review_digest": snapshot.review_digest,
        "completion_ref": snapshot.completion_ref,
        "completion_digest": snapshot.completion_digest,
        "request_bytes": request_bytes,
        "request_digest": request_digest,
        "record_digest": _SCHEMA4_DIGEST_1,
        "run_id": "run-1",
        "main_terminal_id": "terminal-main",
        "task_id": str(task_state.task_id),
        "dispatch_id": str(task_state.dispatch_id),
        "attempt_id": str(task_state.attempt_id),
        "worker_node": str(task_state.worker_node),
        "reviewer_node": str(task_state.reviewer_node),
        "worker_terminal_id": "terminal-worker",
        "reviewer_terminal_id": "terminal-reviewer",
        "team_id": root.team_id,
        "workspace": root.workspace_path,
        "review_round": task_state.review_round,
        "task_sequence_before": task_state.sequence,
        "task_sequence_after": task_state.sequence + 1,
        "task_digest_before": task_digest,
        "task_digest_after": _SCHEMA4_DIGEST_2,
        "workflow_sequence_before": int(checkpoint[0]),
        "workflow_sequence_after": int(checkpoint[0]) + 1,
        "workflow_digest_before": str(checkpoint[1]),
        "workflow_digest_after": _SCHEMA4_DIGEST_3,
        "status": status,
        "effect_owner": None,
        "effect_attempt": None,
        "effect_epoch": None,
        "effect_fence": None,
        "effect_nonce": None,
        "receipt_ref": None,
        "receipt_digest": None,
        "terminal_phase": None,
        "terminal_receipt_ref": None,
        "terminal_receipt_digest": None,
        "unknown_code": None,
        "unknown_evidence_digest": None,
        "prepare_event_id": int(events[0][0]),
        "prepare_event_digest": str(events[0][1]),
        "receipt_event_id": None,
        "receipt_event_digest": None,
        "terminal_event_id": None,
        "terminal_event_digest": None,
        "unknown_event_id": None,
        "unknown_event_digest": None,
        "created_ns": 20,
        "updated_ns": 20,
    }
    if changes is not None:
        values.update(changes)
    return values


def _receipt_row_values(
    receipt: Any,
    receipt_bytes: bytes,
    *,
    root_key: str,
    verification_ref: str = "verification-1",
    changes: Mapping[str, object] | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "root_key": root_key,
        "receipt_ref": str(receipt.receipt_ref),
        "verification_ref": verification_ref,
        "receipt_schema_version": 1,
        "receipt_bytes": receipt_bytes,
        "receipt_digest": str(receipt.receipt_digest),
        "stored_ns": 30,
    }
    if changes is not None:
        values.update(changes)
    return values


def _status_group_values(
    group: str,
    events: list[tuple[Any, ...]],
    receipt: Any,
) -> dict[str, object]:
    if len(events) < 2:
        raise AssertionError("typed workflow fixture has no event pointers")
    event_id = int(events[1][0])
    event_digest = str(events[1][1])
    if group == "effect":
        return {
            "effect_owner": "effect-owner",
            "effect_attempt": 1,
            "effect_epoch": 1,
            "effect_fence": 1,
            "effect_nonce": "effect-nonce",
        }
    if group == "receipt":
        return {
            "receipt_ref": str(receipt.receipt_ref),
            "receipt_digest": str(receipt.receipt_digest),
        }
    if group == "terminal":
        return {
            "terminal_phase": "completed",
            "terminal_receipt_ref": str(receipt.receipt_ref),
            "terminal_receipt_digest": str(receipt.receipt_digest),
        }
    if group == "unknown":
        return {
            "unknown_code": "runner-response-loss",
            "unknown_evidence_digest": _SCHEMA4_DIGEST_4,
        }
    if group.endswith("_pointer"):
        prefix = group.removesuffix("_pointer")
        return {
            f"{prefix}_event_id": event_id,
            f"{prefix}_event_digest": event_digest,
        }
    raise AssertionError(f"unknown verification status group: {group}")


def _valid_status_changes(
    status: str,
    events: list[tuple[Any, ...]],
    receipt: Any,
) -> dict[str, object]:
    changes: dict[str, object] = {}
    for group in VERIFICATION_STATUS_REQUIRED_GROUPS[status]:
        changes.update(_status_group_values(group, events, receipt))
    return changes


def _mutate_current_v4_image(root: Path, variant: str) -> None:
    database = root / store_module.DATABASE_FILENAME
    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        if variant == "missing-table":
            connection.execute("DROP TABLE verification_receipts")
        elif variant == "missing-index":
            connection.execute("DROP INDEX verification_operations_root_status_idx")
        elif variant == "missing-trigger":
            connection.execute("DROP TRIGGER verification_receipts_no_update")
        elif variant == "extra-object":
            connection.execute("CREATE TABLE unexpected_schema_object (value TEXT)")
        elif variant == "mixed-marker":
            connection.execute(
                "UPDATE store_meta SET value = 3 WHERE key = 'store_schema'"
            )
        elif variant == "future-marker":
            connection.execute(
                "UPDATE store_meta SET value = 99 WHERE key = 'store_schema'"
            )
            connection.execute("PRAGMA user_version = 99")
        else:
            raise AssertionError(f"unknown current schema-4 image variant: {variant}")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


class TaskVerificationSchemaTests(unittest.TestCase):
    def test_schema4_constants_and_exact_twelve_table_image(self) -> None:
        self.assertEqual(4, store_module.STORE_SCHEMA)
        self.assertEqual(4, workflow_store.STORE_SCHEMA)
        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root) as store:
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                self.assertEqual(
                    4, connection.execute("PRAGMA user_version").fetchone()[0]
                )
                self.assertEqual(
                    4,
                    connection.execute(
                        "SELECT value FROM store_meta WHERE key = 'store_schema'"
                    ).fetchone()[0],
                )
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                self.assertEqual(EXPECTED_TABLES, tables)
                self.assertEqual(
                    12,
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    ).fetchone()[0],
                )

    def test_schema4_versions_and_no_verify_workflow_action(self) -> None:
        self.assertEqual(2, store_module.EVENT_SCHEMA_VERSION)
        self.assertEqual(1, store_module.WORKFLOW_EVENT_SCHEMA_VERSION)
        self.assertEqual(1, workflow_store.WORKFLOW_EVENT_SCHEMA_VERSION)
        self.assertEqual(1, backup.BACKUP_MANIFEST_VERSION)
        self.assertEqual(
            (
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
            ),
            backup.BACKUP_MANIFEST_FIELDS,
        )
        self.assertEqual(8, len(WORKFLOW_ACTIONS))
        operation_action = getattr(workflow_store, "OperationAction", None)
        self.assertIsNotNone(operation_action)
        assert operation_action is not None
        self.assertEqual(WORKFLOW_ACTIONS, {item.value for item in operation_action})
        self.assertFalse(hasattr(operation_action, "VERIFY"))
        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root) as store:
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                operation_sql = _sqlite_sql(connection, "table", "workflow_operations")
                event_sql = _sqlite_sql(connection, "table", "workflow_events")
                self.assertNotIn("'VERIFY'", operation_sql)
                self.assertIn("'mark_unknown'", event_sql)
                self.assertIn("'verification_transition'", event_sql)
                self.assertIn("operation_id IS NULL AND kind IN", event_sql)
                self.assertEqual(
                    "", _sqlite_sql(connection, "table", "verification_events")
                )

    def test_new_tables_have_exact_columns_indexes_and_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root) as store:
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                for table, expected_columns in EXPECTED_VERIFICATION_COLUMNS.items():
                    self.assertEqual(
                        expected_columns,
                        _columns(connection, table),
                        table,
                    )
                    self.assertFalse(
                        any(
                            any(
                                forbidden in column[0].lower()
                                for forbidden in FORBIDDEN_BODY_COLUMNS
                            )
                            for column in expected_columns
                        ),
                        f"{table} expected columns include a raw body field",
                    )
                for table, expected_indexes in EXPECTED_VERIFICATION_INDEXES.items():
                    self.assertEqual(
                        expected_indexes,
                        {
                            (unique, origin, columns)
                            for unique, origin, columns in _indexes(connection, table)
                        },
                        table,
                    )
                for (
                    table,
                    expected_foreign_keys,
                ) in EXPECTED_VERIFICATION_FOREIGN_KEYS.items():
                    self.assertEqual(
                        expected_foreign_keys,
                        {tuple(row) for row in _foreign_keys(connection, table)},
                        table,
                    )

    def test_verification_operations_have_safe_columns_and_literal_status_matrix(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root) as store:
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                operation_columns = _column_names(connection, "verification_operations")
                for forbidden in FORBIDDEN_BODY_COLUMNS:
                    self.assertFalse(
                        any(forbidden in name.lower() for name in operation_columns),
                        f"raw body column leaked into verification_operations: {forbidden}",
                    )
                operation_sql = _sqlite_sql(
                    connection, "table", "verification_operations"
                )
                self.assertTrue(operation_sql)
                for status in VERIFICATION_STATUSES:
                    self.assertIn(f"'{status}'", operation_sql)
                self.assertIn("PREPARED", operation_sql)
                self.assertIn("EFFECT_PREPARED", operation_sql)
                self.assertIn("RECEIPTED", operation_sql)
                self.assertIn("TERMINAL", operation_sql)
                self.assertIn("UNKNOWN_EFFECT", operation_sql)
                for pointer, digest in STAGE_POINTERS.values():
                    self.assertIn(pointer, operation_sql)
                    self.assertIn(digest, operation_sql)
                    self.assertIn("IS NULL", operation_sql)
                    self.assertIn("IS NOT NULL", operation_sql)
                self.assertEqual(
                    5, operation_sql.count("DEFERRABLE INITIALLY DEFERRED")
                )
                self.assertRegex(
                    operation_sql,
                    r"(?s)FOREIGN KEY\(root_key, receipt_ref\).*?"
                    r"REFERENCES verification_receipts\(root_key, receipt_ref\)"
                    r".*?DEFERRABLE INITIALLY DEFERRED",
                )
                for pointer, _ in STAGE_POINTERS.values():
                    self.assertRegex(
                        operation_sql,
                        rf"(?s)FOREIGN KEY\({pointer}\).*?"
                        r"REFERENCES workflow_events\(workflow_event_id\)"
                        r".*?DEFERRABLE INITIALLY DEFERRED",
                    )
                self.assertIn("CHECK", operation_sql)

    def test_new_scalar_columns_have_bounded_sqlite_constraints(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root) as store:
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                task_sql = _sqlite_sql(connection, "table", "task_policy_states")
                self.assertIn("length(task_id) BETWEEN 1 AND 64", task_sql)
                self.assertIn("length(target_head) IN (40, 64)", task_sql)
                self.assertIn("target_head NOT GLOB", task_sql)
                self.assertIn("length(target_tree_digest) = 64", task_sql)
                self.assertIn("state_codec_version = 1", task_sql)
                self.assertIn("state_codec_version", task_sql)
                operation_sql = _sqlite_sql(
                    connection, "table", "verification_operations"
                )
                for field in (
                    "record_version",
                    "approval_binding_version",
                    "request_schema_version",
                ):
                    self.assertIn(f"typeof({field}) = 'integer'", operation_sql)
                    self.assertIn(f"{field} = 1", operation_sql)
                self.assertIn(
                    "length(approval_binding_bytes) BETWEEN 1 AND 1048576",
                    operation_sql,
                )
                for field in ("approval_binding_digest", "record_digest"):
                    self.assertIn(f"length({field}) = 71", operation_sql)
                    self.assertIn(f"substr({field}, 1, 7) = 'sha256:'", operation_sql)
                    self.assertIn(f"substr({field}, 8) NOT GLOB", operation_sql)
                for field in (
                    "task_sequence_before",
                    "task_sequence_after",
                    "workflow_sequence_before",
                    "workflow_sequence_after",
                    "created_ns",
                    "updated_ns",
                ):
                    self.assertIn(f"typeof({field}) = 'integer'", operation_sql)
                    self.assertIn(
                        f"{field} BETWEEN 0 AND 9223372036854775807",
                        operation_sql,
                    )
                self.assertIn(
                    "terminal_phase IN ('completed', 'verification_failed')",
                    operation_sql,
                )

    def test_verification_ddl_rejects_future_record_version_and_partial_status_groups(
        self,
    ) -> None:
        cases = (
            ("record-version", "PREPARED", {"record_version": 2}),
            (
                "prepared-pointer",
                "PREPARED",
                {"prepare_event_id": None, "prepare_event_digest": None},
            ),
            (
                "effect-prepared-effect",
                "EFFECT_PREPARED",
                {"effect_owner": "effect-owner"},
            ),
            (
                "receipted-effect",
                "RECEIPTED",
                {"receipt_ref": "receipt-1", "receipt_digest": "a" * 64},
            ),
            (
                "terminal-pointer",
                "TERMINAL",
                {
                    "effect_owner": "effect-owner",
                    "effect_attempt": 1,
                    "effect_epoch": 1,
                    "effect_fence": 1,
                    "effect_nonce": "effect-nonce",
                    "receipt_ref": "receipt-1",
                    "receipt_digest": "a" * 64,
                    "terminal_phase": "completed",
                    "terminal_receipt_ref": "receipt-1",
                    "terminal_receipt_digest": "a" * 64,
                },
            ),
            (
                "unknown-pointer",
                "UNKNOWN_EFFECT",
                {
                    "effect_owner": "effect-owner",
                    "effect_attempt": 1,
                    "effect_epoch": 1,
                    "effect_fence": 1,
                    "effect_nonce": "effect-nonce",
                    "unknown_code": "runner-response-loss",
                    "unknown_evidence_digest": _SCHEMA4_DIGEST_4,
                },
            ),
        )
        for label, status, changes in cases:
            with (
                self.subTest(case=label),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-task-verification-ddl-"
                ) as temporary,
            ):
                state_root, root, _ = _schema4_workflow_image(temporary)
                (
                    snapshot,
                    task_state,
                    task_bytes,
                    task_digest,
                    request,
                    _receipt,
                    request_bytes,
                    request_digest,
                    _receipt_bytes,
                    _effect,
                    _result,
                ) = _typed_verification_payloads(root)
                with closing(
                    sqlite3.connect(
                        str(state_root / store_module.DATABASE_FILENAME),
                        isolation_level=None,
                    )
                ) as connection:
                    _insert_nonempty_ledger_row(
                        connection,
                        "task_policy_states",
                        _task_row_values(root, task_state, task_bytes, task_digest),
                    )
                    operation = _operation_row_values(
                        connection,
                        root,
                        task_state,
                        task_digest,
                        snapshot,
                        request,
                        request_bytes,
                        request_digest,
                        status=status,
                        changes=changes,
                    )
                    with self.assertRaises(sqlite3.IntegrityError):
                        _insert_nonempty_ledger_row(
                            connection,
                            "verification_operations",
                            operation,
                        )
                    self.assertEqual(
                        0,
                        connection.execute(
                            "SELECT COUNT(*) FROM verification_operations"
                        ).fetchone()[0],
                    )

    def test_verification_ddl_accepts_each_status_null_group(self) -> None:
        effect_fields = {
            "effect_owner": "effect-owner",
            "effect_attempt": 1,
            "effect_epoch": 1,
            "effect_fence": 1,
            "effect_nonce": "effect-nonce",
        }
        for status, changes, needs_receipt in (
            ("PREPARED", {}, False),
            ("EFFECT_PREPARED", effect_fields, False),
            (
                "RECEIPTED",
                {
                    **effect_fields,
                    "receipt_ref": "receipt-1",
                    "receipt_digest": "a" * 64,
                    "receipt_event_id": 2,
                    "receipt_event_digest": _SCHEMA4_DIGEST_2,
                },
                True,
            ),
            (
                "TERMINAL",
                {
                    **effect_fields,
                    "receipt_ref": "receipt-1",
                    "receipt_digest": "a" * 64,
                    "receipt_event_id": 2,
                    "receipt_event_digest": _SCHEMA4_DIGEST_2,
                    "terminal_phase": "completed",
                    "terminal_receipt_ref": "receipt-1",
                    "terminal_receipt_digest": "a" * 64,
                    "terminal_event_id": 2,
                    "terminal_event_digest": _SCHEMA4_DIGEST_2,
                },
                True,
            ),
            (
                "UNKNOWN_EFFECT",
                {
                    **effect_fields,
                    "unknown_code": "runner-response-loss",
                    "unknown_evidence_digest": _SCHEMA4_DIGEST_4,
                    "unknown_event_id": 2,
                    "unknown_event_digest": _SCHEMA4_DIGEST_2,
                },
                False,
            ),
        ):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-task-verification-valid-status-"
                ) as temporary,
            ):
                state_root, root, _ = _schema4_workflow_image(temporary)
                (
                    snapshot,
                    task_state,
                    task_bytes,
                    task_digest,
                    request,
                    receipt,
                    request_bytes,
                    request_digest,
                    receipt_bytes,
                    _effect,
                    _result,
                ) = _typed_verification_payloads(root)
                with closing(
                    sqlite3.connect(
                        str(state_root / store_module.DATABASE_FILENAME),
                        isolation_level=None,
                    )
                ) as connection:
                    connection.execute("PRAGMA foreign_keys = ON")
                    _insert_nonempty_ledger_row(
                        connection,
                        "task_policy_states",
                        _task_row_values(root, task_state, task_bytes, task_digest),
                    )
                    events = connection.execute(
                        "SELECT workflow_event_id, event_digest FROM workflow_events "
                        "WHERE root_key = ? ORDER BY workflow_event_id",
                        (root.root_key,),
                    ).fetchall()
                    self.assertGreaterEqual(len(events), 2)
                    operation_changes = dict(changes)
                    if status in {"RECEIPTED", "TERMINAL"}:
                        operation_changes["receipt_event_id"] = int(events[1][0])
                        operation_changes["receipt_event_digest"] = str(events[1][1])
                    if status == "TERMINAL":
                        operation_changes["terminal_event_id"] = int(events[1][0])
                        operation_changes["terminal_event_digest"] = str(events[1][1])
                    if status == "UNKNOWN_EFFECT":
                        operation_changes["unknown_event_id"] = int(events[1][0])
                        operation_changes["unknown_event_digest"] = str(events[1][1])
                    operation = _operation_row_values(
                        connection,
                        root,
                        task_state,
                        task_digest,
                        snapshot,
                        request,
                        request_bytes,
                        request_digest,
                        status=status,
                        changes=operation_changes,
                    )
                    connection.execute("BEGIN")
                    _insert_nonempty_ledger_row(
                        connection,
                        "verification_operations",
                        operation,
                    )
                    if needs_receipt:
                        _insert_nonempty_ledger_row(
                            connection,
                            "verification_receipts",
                            _receipt_row_values(
                                receipt,
                                receipt_bytes,
                                root_key=root.root_key,
                            ),
                        )
                    connection.execute("COMMIT")
                    self.assertEqual(
                        1,
                        connection.execute(
                            "SELECT COUNT(*) FROM verification_operations"
                        ).fetchone()[0],
                    )

    def test_verification_ddl_rejects_all_forbidden_and_missing_required_groups(
        self,
    ) -> None:
        all_groups = tuple(VERIFICATION_STATUS_GROUP_FIELDS)
        for status, required_groups in VERIFICATION_STATUS_REQUIRED_GROUPS.items():
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-task-verification-status-matrix-"
                ) as temporary,
            ):
                state_root, root, _ = _schema4_workflow_image(temporary)
                (
                    snapshot,
                    task_state,
                    task_bytes,
                    task_digest,
                    request,
                    receipt,
                    request_bytes,
                    request_digest,
                    receipt_bytes,
                    _effect,
                    _result,
                ) = _typed_verification_payloads(root)
                database = state_root / store_module.DATABASE_FILENAME
                with closing(
                    sqlite3.connect(str(database), isolation_level=None)
                ) as connection:
                    connection.execute("PRAGMA foreign_keys = ON")
                    _insert_nonempty_ledger_row(
                        connection,
                        "task_policy_states",
                        _task_row_values(root, task_state, task_bytes, task_digest),
                    )
                    events = connection.execute(
                        "SELECT workflow_event_id, event_digest FROM workflow_events "
                        "WHERE root_key = ? ORDER BY workflow_event_id",
                        (root.root_key,),
                    ).fetchall()
                    valid_changes = _valid_status_changes(status, events, receipt)
                    invalid_cases: list[tuple[str, dict[str, object]]] = [
                        (
                            "record-version",
                            {**valid_changes, "record_version": 2},
                        )
                    ]
                    for group in all_groups:
                        if group not in required_groups:
                            invalid_cases.append(
                                (
                                    f"{group}-forbidden",
                                    {
                                        **valid_changes,
                                        **_status_group_values(
                                            group,
                                            events,
                                            receipt,
                                        ),
                                    },
                                )
                            )
                    for group in required_groups:
                        invalid_cases.append(
                            (
                                f"{group}-required",
                                {
                                    **valid_changes,
                                    VERIFICATION_STATUS_GROUP_FIELDS[group][0]: None,
                                },
                            )
                        )
                    for index, (label, changes) in enumerate(invalid_cases):
                        with self.subTest(status=status, case=label):
                            operation = _operation_row_values(
                                connection,
                                root,
                                task_state,
                                task_digest,
                                snapshot,
                                request,
                                request_bytes,
                                request_digest,
                                status=status,
                                verification_ref=(
                                    f"verification-{status.lower()}-{index}"
                                ),
                                changes={
                                    **changes,
                                    "approval_ref": (
                                        f"approval-{status.lower()}-{index}"
                                    ),
                                },
                            )
                            accepted = False
                            try:
                                connection.execute("BEGIN")
                                _insert_nonempty_ledger_row(
                                    connection,
                                    "verification_operations",
                                    operation,
                                )
                                if operation["receipt_ref"] is not None:
                                    _insert_nonempty_ledger_row(
                                        connection,
                                        "verification_receipts",
                                        _receipt_row_values(
                                            receipt,
                                            receipt_bytes,
                                            root_key=root.root_key,
                                            verification_ref=str(
                                                operation["verification_ref"]
                                            ),
                                            changes={
                                                "receipt_ref": operation["receipt_ref"],
                                                "receipt_digest": operation[
                                                    "receipt_digest"
                                                ],
                                            },
                                        ),
                                    )
                                connection.execute("COMMIT")
                                accepted = True
                            except sqlite3.IntegrityError:
                                connection.rollback()
                            if accepted:
                                self.fail(
                                    f"DDL accepted invalid status group: {status}/{label}"
                                )
                            connection.rollback()
                            self.assertEqual(
                                0,
                                connection.execute(
                                    "SELECT COUNT(*) FROM verification_operations"
                                ).fetchone()[0],
                            )

    def test_verification_receipt_is_body_free_and_guarded_by_immutable_triggers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root) as store:
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                receipt_columns = _column_names(connection, "verification_receipts")
                self.assertTrue(
                    {"root_key", "receipt_ref", "verification_ref"}
                    <= set(receipt_columns),
                )
                self.assertTrue(
                    any(
                        name in receipt_columns
                        for name in ("receipt_bytes", "canonical_receipt_bytes")
                    ),
                    "canonical safe receipt bytes are missing",
                )
                self.assertIn("receipt_digest", receipt_columns)
                self.assertIn("receipt_schema_version", receipt_columns)
                self.assertTrue(
                    any(
                        name in receipt_columns
                        for name in ("issued_ns", "created_ns", "stored_ns")
                    ),
                    "Store receipt timestamp is missing",
                )
                for forbidden in FORBIDDEN_BODY_COLUMNS + ("environment",):
                    if forbidden == "environment":
                        forbidden_names = {"environment_value"}
                    else:
                        forbidden_names = {forbidden}
                    self.assertFalse(
                        any(
                            any(item in name.lower() for item in forbidden_names)
                            for name in receipt_columns
                        ),
                        f"raw receipt body column leaked: {forbidden}",
                    )
                trigger_names = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    )
                }
                expected_triggers = {
                    "verification_receipts_no_update",
                    "verification_receipts_no_delete",
                    "verification_receipts_no_replace",
                }
                self.assertTrue(expected_triggers <= trigger_names)
                for name in expected_triggers:
                    trigger_sql = _sqlite_sql(connection, "trigger", name).upper()
                    self.assertIn("VERIFICATION_RECEIPTS", trigger_sql)
                    self.assertIn("RAISE", trigger_sql)
                    self.assertIn("ABORT", trigger_sql)

    def test_verification_receipt_triggers_reject_mutation_and_conflicting_replace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-receipt-trigger-"
        ) as temporary:
            state_root, root, _ = _schema4_workflow_image(temporary)
            (
                snapshot,
                task_state,
                task_bytes,
                task_digest,
                request,
                receipt,
                request_bytes,
                request_digest,
                receipt_bytes,
                _effect,
                _result,
            ) = _typed_verification_payloads(root)
            database = state_root / store_module.DATABASE_FILENAME
            with closing(
                sqlite3.connect(str(database), isolation_level=None)
            ) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                _insert_nonempty_ledger_row(
                    connection,
                    "task_policy_states",
                    _task_row_values(root, task_state, task_bytes, task_digest),
                )
                events = connection.execute(
                    "SELECT workflow_event_id, event_digest FROM workflow_events "
                    "WHERE root_key = ? ORDER BY workflow_event_id",
                    (root.root_key,),
                ).fetchall()
                self.assertGreaterEqual(len(events), 2)
                operation = _operation_row_values(
                    connection,
                    root,
                    task_state,
                    task_digest,
                    snapshot,
                    request,
                    request_bytes,
                    request_digest,
                    status="RECEIPTED",
                    changes={
                        "effect_owner": "effect-owner",
                        "effect_attempt": 1,
                        "effect_epoch": 1,
                        "effect_fence": 1,
                        "effect_nonce": "effect-nonce",
                        "receipt_ref": str(receipt.receipt_ref),
                        "receipt_digest": str(receipt.receipt_digest),
                        "receipt_event_id": int(events[1][0]),
                        "receipt_event_digest": str(events[1][1]),
                    },
                )
                receipt_row = _receipt_row_values(
                    receipt,
                    receipt_bytes,
                    root_key=root.root_key,
                )
                connection.execute("BEGIN")
                _insert_nonempty_ledger_row(
                    connection,
                    "verification_operations",
                    operation,
                )
                _insert_nonempty_ledger_row(
                    connection,
                    "verification_receipts",
                    receipt_row,
                )
                connection.execute("COMMIT")
                original_row = connection.execute(
                    "SELECT receipt_bytes, receipt_digest, stored_ns "
                    "FROM verification_receipts WHERE root_key = ? AND receipt_ref = ?",
                    (root.root_key, str(receipt.receipt_ref)),
                ).fetchone()
                self.assertIsNotNone(original_row)
                assert original_row is not None

                for statement, parameters in (
                    (
                        (
                            "UPDATE verification_receipts SET stored_ns = ? "
                            "WHERE root_key = ? AND receipt_ref = ?"
                        ),
                        (31, root.root_key, str(receipt.receipt_ref)),
                    ),
                    (
                        (
                            "DELETE FROM verification_receipts "
                            "WHERE root_key = ? AND receipt_ref = ?"
                        ),
                        (root.root_key, str(receipt.receipt_ref)),
                    ),
                ):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(statement, parameters)
                    connection.rollback()

                replacement = dict(receipt_row)
                replacement["receipt_bytes"] = receipt_bytes + b" "
                with self.assertRaises(sqlite3.IntegrityError):
                    _insert_nonempty_ledger_row(
                        connection,
                        "verification_receipts",
                        replacement,
                    )
                connection.rollback()
                columns = tuple(replacement)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        f"INSERT OR REPLACE INTO verification_receipts "
                        f"({', '.join(columns)}) "
                        f"VALUES ({', '.join('?' for _ in columns)})",
                        tuple(replacement[column] for column in columns),
                    )
                connection.rollback()
                self.assertEqual(
                    original_row,
                    connection.execute(
                        "SELECT receipt_bytes, receipt_digest, stored_ns "
                        "FROM verification_receipts WHERE root_key = ? AND receipt_ref = ?",
                        (root.root_key, str(receipt.receipt_ref)),
                    ).fetchone(),
                )

    def test_frozen_v3_manifest_is_independent_of_v3_production_constants(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-"
        ) as temporary:
            root = _make_legacy_state(self, temporary, 3, "v3-oracle")
            connection = sqlite3.connect(str(root / store_module.DATABASE_FILENAME))
            try:
                self.assertEqual(
                    FROZEN_V3_MANIFEST_DIGEST, _manifest_digest(connection)
                )
                self.assertEqual(
                    LEGACY_METADATA_KEYS,
                    {
                        str(row[0])
                        for row in connection.execute("SELECT key FROM store_meta")
                    },
                )
            finally:
                connection.close()

    def test_exact_v2_and_v3_images_require_migration_without_root_gate_or_db_mutation(
        self,
    ) -> None:
        for schema_version in (2, 3):
            with (
                self.subTest(schema_version=schema_version),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-task-verification-"
                ) as temporary,
            ):
                root = _make_legacy_state(
                    self, temporary, schema_version, f"v{schema_version}"
                )
                before = _root_snapshot(root)
                self.assertTrue(
                    callable(getattr(CoordinationStore, "_open_lifetime_gate", None)),
                    "CoordinationStore lifetime-gate seam is missing",
                )
                with (
                    mock.patch.object(
                        CoordinationStore,
                        "_open_lifetime_gate",
                        side_effect=AssertionError(
                            "legacy classifier created a lifetime gate"
                        ),
                    ),
                    self.assertRaises(StoreMigrationRequiredError) as raised,
                ):
                    CoordinationStore(root)
                error = raised.exception
                source = getattr(error, "source", getattr(error, "source_schema", None))
                target = getattr(error, "target", getattr(error, "target_schema", None))
                self.assertEqual(schema_version, source)
                self.assertEqual(4, target)
                self.assertEqual(before, _root_snapshot(root))

    def test_store_schema_rebinding_does_not_change_classification_or_v4_marker(
        self,
    ) -> None:
        """The public schema marker cannot alter frozen v2/v3 or v4 behavior."""

        for schema_version in (2, 3):
            with (
                self.subTest(schema_version=schema_version),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-task-verification-"
                ) as temporary,
            ):
                root = _make_legacy_state(
                    self, temporary, schema_version, f"rebound-v{schema_version}"
                )
                before = _root_snapshot(root)
                with (
                    mock.patch.object(store_module, "STORE_SCHEMA", 99),
                    self.assertRaises(StoreMigrationRequiredError) as raised,
                ):
                    CoordinationStore(root)
                error = raised.exception
                source = getattr(error, "source", getattr(error, "source_schema", None))
                target = getattr(error, "target", getattr(error, "target_schema", None))
                self.assertEqual(schema_version, source)
                self.assertEqual(4, target)
                self.assertEqual(before, _root_snapshot(root))

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            before = _root_snapshot(root)
            with (
                mock.patch.object(store_module, "STORE_SCHEMA", 3),
                CoordinationStore(root) as store,
            ):
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                self.assertEqual(
                    4, connection.execute("PRAGMA user_version").fetchone()[0]
                )
                self.assertEqual(
                    4,
                    connection.execute(
                        "SELECT value FROM store_meta WHERE key = 'store_schema'"
                    ).fetchone()[0],
                )
            self.assertEqual(before, _root_snapshot(root))

    def test_nonempty_v2_event_stays_migration_required_under_event_rebinding(
        self,
    ) -> None:
        """A v2 provider event cannot make migration classification schema-dependent."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-"
        ) as temporary:
            root = _make_nonempty_v2_state(self, temporary, "v2-nonempty")
            before = _root_snapshot(root)
            with (
                mock.patch.object(store_module, "STORE_SCHEMA", 99),
                mock.patch.object(store_module, "EVENT_SCHEMA_VERSION", 99),
                self.assertRaises(StoreMigrationRequiredError) as raised,
            ):
                CoordinationStore(root)
            error = raised.exception
            source = getattr(error, "source", getattr(error, "source_schema", None))
            target = getattr(error, "target", getattr(error, "target_schema", None))
            self.assertEqual(2, source)
            self.assertEqual(4, target)
            self.assertEqual(before, _root_snapshot(root))

    def test_v3_migration_classification_uses_frozen_workflow_codec(self) -> None:
        """Target-v4 codec globals must not change the v3 migration result."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-"
        ) as temporary:
            root = _make_legacy_state(self, temporary, 3, "v3-frozen-codec")
            before = _root_snapshot(root)
            with (
                mock.patch.object(workflow_store, "CHECKPOINT_VERSION", 99),
                mock.patch.object(workflow_store, "SEED_VERSION", 99),
                mock.patch.object(workflow_store, "CHECKPOINT_FIELDS", ("future",)),
                mock.patch.object(workflow_store, "SEED_FIELDS", ("future",)),
                mock.patch.object(
                    workflow_store,
                    "CHECKPOINT_DIGEST_DOMAIN",
                    b"future-checkpoint-domain\0",
                ),
                mock.patch.object(
                    workflow_store,
                    "SEED_DIGEST_DOMAIN",
                    b"future-seed-domain\0",
                ),
                mock.patch.object(workflow_store, "WORKFLOW_EVENT_SCHEMA_VERSION", 99),
                self.assertRaises(StoreMigrationRequiredError) as raised,
            ):
                CoordinationStore(root)
            error = raised.exception
            source = getattr(error, "source", getattr(error, "source_schema", None))
            target = getattr(error, "target", getattr(error, "target_schema", None))
            self.assertEqual(3, source)
            self.assertEqual(4, target)
            self.assertEqual(before, _root_snapshot(root))

    def test_nonempty_v3_image_requires_migration_without_global_rebinding_effect(
        self,
    ) -> None:
        """A historical start/commit image stays migration-required under rebinding."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-"
        ) as temporary:
            root = _make_nonempty_v3_state(self, temporary, "v3-nonempty")
            before = _root_snapshot(root)
            with (
                mock.patch.object(workflow_store, "STORE_SCHEMA", 3),
                mock.patch.object(workflow_store, "CHECKPOINT_VERSION", 99),
                mock.patch.object(workflow_store, "SEED_VERSION", 99),
                mock.patch.object(workflow_store, "CHECKPOINT_FIELDS", ("future",)),
                mock.patch.object(workflow_store, "SEED_FIELDS", ("future",)),
                mock.patch.object(
                    workflow_store,
                    "CHECKPOINT_DIGEST_DOMAIN",
                    b"future-checkpoint-domain\0",
                ),
                mock.patch.object(
                    workflow_store,
                    "SEED_DIGEST_DOMAIN",
                    b"future-seed-domain\0",
                ),
                mock.patch.object(workflow_store, "WORKFLOW_EVENT_SCHEMA_VERSION", 99),
                mock.patch.object(store_module, "EVENT_SCHEMA_VERSION", 99),
                mock.patch.object(store_module, "WORKFLOW_EVENT_SCHEMA_VERSION", 99),
                self.assertRaises(StoreMigrationRequiredError) as raised,
            ):
                CoordinationStore(root)
            error = raised.exception
            source = getattr(error, "source", getattr(error, "source_schema", None))
            target = getattr(error, "target", getattr(error, "target_schema", None))
            self.assertEqual(3, source)
            self.assertEqual(4, target)
            self.assertEqual(before, _root_snapshot(root))

    def test_marker_only_mixed_malformed_extra_and_future_images_are_generic_and_read_only(
        self,
    ) -> None:
        variants = (
            "marker-only",
            "mixed",
            "malformed",
            "missing-table",
            "missing-index",
            "missing-trigger",
            "extra-object",
            "future",
        )
        for variant in variants:
            with (
                self.subTest(variant=variant),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-task-verification-"
                ) as temporary,
            ):
                root = _make_legacy_state(self, temporary, 3, variant)
                _mutate_legacy_marker(root, variant)
                before = _foundation_snapshot(root)
                self.assertTrue(
                    callable(getattr(CoordinationStore, "_open_lifetime_gate", None)),
                    "CoordinationStore lifetime-gate seam is missing",
                )
                with (
                    mock.patch.object(
                        CoordinationStore,
                        "_open_lifetime_gate",
                        side_effect=AssertionError(
                            "invalid established image created a lifetime gate"
                        ),
                    ),
                    self.assertRaises(StoreSchemaError) as raised,
                ):
                    CoordinationStore(root)
                self.assertNotIsInstance(raised.exception, StoreMigrationRequiredError)
                self.assertEqual(before, _foundation_snapshot(root))

    def test_current_v4_missing_extra_mixed_and_future_images_are_read_only(
        self,
    ) -> None:
        variants = (
            "missing-table",
            "missing-index",
            "missing-trigger",
            "extra-object",
            "mixed-marker",
            "future-marker",
        )
        for variant in variants:
            with (
                self.subTest(variant=variant),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-task-verification-current-v4-"
                ) as temporary,
            ):
                root = _state_root(temporary)
                with CoordinationStore(root):
                    pass
                _mutate_current_v4_image(root, variant)
                before = _foundation_snapshot(root)
                before_inventory = _root_inventory(root)
                with (
                    mock.patch.object(
                        CoordinationStore,
                        "_open_lifetime_gate",
                        side_effect=AssertionError(
                            "invalid current image created a lifetime gate"
                        ),
                    ),
                    self.assertRaises(
                        (StoreSchemaError, StoreIntegrityError)
                    ) as raised,
                ):
                    CoordinationStore(root)
                self.assertNotIsInstance(raised.exception, StoreMigrationRequiredError)
                self.assertEqual(before, _foundation_snapshot(root))
                self.assertEqual(before_inventory, _root_inventory(root))

    def test_established_classifier_reads_pending_wal_before_opening_lifetime_gate(
        self,
    ) -> None:
        """A WAL-only schema mismatch must fail before any root/gate mutation."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-wal-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / store_module.DATABASE_FILENAME
            raw = sqlite3.connect(str(database), isolation_level=None)
            try:
                # Keep both marker writes in the WAL.  The main database header
                # remains schema 4, while the effective SQLite snapshot is a
                # mixed v4-object/v3-marker image.
                raw.execute("PRAGMA user_version = 3")
                raw.execute(
                    "UPDATE store_meta SET value = 3 WHERE key = 'store_schema'"
                )
                before = tuple(
                    (path.name, path.stat().st_mode, path.read_bytes())
                    for path in sorted(root.iterdir(), key=lambda item: item.name)
                )
                with (
                    mock.patch.object(
                        CoordinationStore,
                        "_open_lifetime_gate",
                        side_effect=AssertionError(
                            "pending-WAL image created a lifetime gate"
                        ),
                    ),
                    self.assertRaises(StoreSchemaError) as raised,
                ):
                    CoordinationStore(root)
                self.assertNotIsInstance(raised.exception, StoreMigrationRequiredError)
                self.assertEqual(
                    before,
                    tuple(
                        (path.name, path.stat().st_mode, path.read_bytes())
                        for path in sorted(root.iterdir(), key=lambda item: item.name)
                    ),
                )
            finally:
                raw.close()

    def test_classifier_rejects_established_rollback_mode_without_source_mutation(
        self,
    ) -> None:
        """Schema-4 rollback-mode images fail before source SQLite reopen."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-rollback-mode-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / store_module.DATABASE_FILENAME
            raw = sqlite3.connect(str(database), isolation_level=None)
            try:
                self.assertEqual(
                    "delete",
                    str(
                        raw.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
                    ).lower(),
                )
            finally:
                raw.close()
            header = database.read_bytes()[:100]
            self.assertEqual(b"\x01\x01", header[18:20])
            before = _foundation_snapshot(root)
            source_connect_calls: list[object] = []

            original_connect = cast(
                Callable[..., sqlite3.Connection],
                sqlite3.connect,
            )

            def reject_source_connect(*args: object, **kwargs: object) -> object:
                uri = str(args[0]) if args else ""
                if "mode=rw" in uri:
                    source_connect_calls.append((args, kwargs))
                    raise AssertionError("rollback image reached source SQLite open")
                return original_connect(*args, **kwargs)

            with (
                mock.patch.object(
                    sqlite3,
                    "connect",
                    side_effect=reject_source_connect,
                ),
                self.assertRaises(StoreIntegrityError),
            ):
                CoordinationStore(root, busy_timeout_ms=100)
            self.assertEqual([], source_connect_calls)
            self.assertEqual(before, _foundation_snapshot(root))

    def test_established_classifier_rejects_stat_open_fifo_race_without_blocking(
        self,
    ) -> None:
        """A regular file replaced by a FIFO cannot block the classifier."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-fifo-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / store_module.DATABASE_FILENAME
            original_database = database.with_name("coordination.sqlite3.original")
            before = database.read_bytes()
            armed = threading.Event()
            swapped = threading.Event()
            observed_flags: list[int] = []
            database_stat_count = 0
            root_fd: list[int | None] = [None]

            class _ClassifierRaceStore(CoordinationStore):
                def _classify_established_image_before_gate(self) -> None:
                    root_fd[0] = self._state_root_fd
                    armed.set()
                    super()._classify_established_image_before_gate()

            original_stat = cast(Callable[..., os.stat_result], os.stat)
            original_open = cast(Callable[..., int], os.open)

            def racing_stat(
                path: str | os.PathLike[str],
                *args: object,
                **kwargs: object,
            ) -> os.stat_result:
                nonlocal database_stat_count
                result = original_stat(path, *args, **kwargs)
                if (
                    armed.is_set()
                    and path == store_module.DATABASE_FILENAME
                    and kwargs.get("dir_fd") == root_fd[0]
                ):
                    database_stat_count += 1
                if (
                    armed.is_set()
                    and not swapped.is_set()
                    and database_stat_count >= 2
                    and path == store_module.DATABASE_FILENAME
                    and kwargs.get("dir_fd") == root_fd[0]
                ):
                    database.rename(original_database)
                    os.mkfifo(database)
                    database.chmod(0o600)
                    swapped.set()
                return result

            def racing_open(
                path: str | os.PathLike[str],
                flags: int,
                *args: object,
                **kwargs: object,
            ) -> int:
                if (
                    path == store_module.DATABASE_FILENAME
                    and kwargs.get("dir_fd") == root_fd[0]
                ):
                    self.assertTrue(
                        swapped.wait(1),
                        "classifier did not observe the deterministic FIFO swap",
                    )
                    observed_flags.append(flags)
                    if not flags & getattr(os, "O_NONBLOCK", 0):
                        raise AssertionError(
                            "classifier opened a raced FIFO without O_NONBLOCK"
                        )
                return original_open(path, flags, *args, **kwargs)

            try:
                with (
                    mock.patch.object(
                        os,
                        "stat",
                        side_effect=racing_stat,
                    ),
                    mock.patch.object(
                        os,
                        "open",
                        side_effect=racing_open,
                    ),
                    self.assertRaises(StoreUnavailableError),
                ):
                    _ClassifierRaceStore(root, busy_timeout_ms=100)
                self.assertTrue(observed_flags)
                self.assertTrue(observed_flags[0] & getattr(os, "O_NONBLOCK", 0))
            finally:
                if swapped.is_set():
                    database.unlink()
                    original_database.rename(database)
            self.assertEqual(before, database.read_bytes())

    def test_established_classifier_copies_source_with_bounded_chunks(self) -> None:
        """Classifier source reads never allocate the complete image at once."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-chunks-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            armed = threading.Event()
            read_sizes: list[int] = []

            class _ChunkProbeStore(CoordinationStore):
                def _classify_established_image_before_gate(self) -> None:
                    armed.set()
                    try:
                        super()._classify_established_image_before_gate()
                    finally:
                        armed.clear()

            original_pread = cast(Callable[..., bytes], os.pread)

            def tracking_pread(fd: int, size: int, offset: int) -> bytes:
                if armed.is_set():
                    read_sizes.append(size)
                return original_pread(fd, size, offset)

            with (
                mock.patch.object(
                    store_module,
                    "_CLASSIFIER_COPY_CHUNK_BYTES",
                    64,
                ),
                mock.patch.object(
                    os,
                    "pread",
                    side_effect=tracking_pread,
                ),
                _ChunkProbeStore(root),
            ):
                pass
            self.assertGreater(len(read_sizes), 1)
            copy_read_sizes = [size for size in read_sizes if size != 100]
            self.assertTrue(copy_read_sizes)
            self.assertLessEqual(max(copy_read_sizes), 64)

    def test_established_classifier_enforces_aggregate_snapshot_size_limit(
        self,
    ) -> None:
        """DB and all sidecars share one explicit classifier size budget."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-size-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            before = _root_snapshot(root)
            database_size = (root / store_module.DATABASE_FILENAME).stat().st_size
            with (
                mock.patch.object(
                    store_module,
                    "_MAX_CLASSIFIER_SNAPSHOT_BYTES",
                    database_size - 1,
                ),
                self.assertRaises(StoreIntegrityError),
            ):
                CoordinationStore(root)
            self.assertEqual(before, _root_snapshot(root))

    def test_classifier_copy_memory_error_is_typed_and_does_not_retry(self) -> None:
        """Source allocation failure is a bounded Store error, not a raw leak."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-memory-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            armed = threading.Event()

            class _MemoryFaultStore(CoordinationStore):
                def _classify_established_image_before_gate(self) -> None:
                    armed.set()
                    super()._classify_established_image_before_gate()

            original_pread = cast(Callable[..., bytes], os.pread)

            def failing_pread(fd: int, size: int, offset: int) -> bytes:
                if armed.is_set():
                    raise MemoryError("injected classifier allocation failure")
                return original_pread(fd, size, offset)

            with (
                mock.patch.object(
                    os,
                    "pread",
                    side_effect=failing_pread,
                ),
                self.assertRaises(StoreUnavailableError) as raised,
            ):
                _MemoryFaultStore(root, busy_timeout_ms=100)
            self.assertIsInstance(raised.exception.__cause__, MemoryError)

    def test_classifier_rejects_same_inode_in_place_valid_image_rewrite(self) -> None:
        """A same-inode rewrite is never accepted as a second valid image."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-in-place-"
        ) as temporary:
            source_root = _state_root(temporary, "source")
            replacement_root = _state_root(temporary, "replacement")
            with CoordinationStore(source_root):
                pass
            with CoordinationStore(replacement_root):
                pass
            database = source_root / store_module.DATABASE_FILENAME
            replacement_database = replacement_root / store_module.DATABASE_FILENAME
            original_bytes = database.read_bytes()
            replacement_bytes = replacement_database.read_bytes()
            self.assertEqual(len(original_bytes), len(replacement_bytes))
            gate = source_root.parent / store_module.LIFETIME_GATE_FILENAME
            gate_before = (gate.stat().st_mode, gate.read_bytes())
            armed = threading.Event()
            rewritten = threading.Event()
            root_fd: list[int | None] = [None]
            database_stat_count = 0
            original_stat = cast(Callable[..., os.stat_result], os.stat)
            original_open = cast(Callable[..., int], os.open)
            original_close = cast(Callable[..., None], os.close)

            class _InPlaceRewriteStore(CoordinationStore):
                def _classify_established_image_before_gate(self) -> None:
                    root_fd[0] = self._state_root_fd
                    armed.set()
                    super()._classify_established_image_before_gate()

            def rewrite_database() -> None:
                descriptor = original_open(database, os.O_WRONLY)
                try:
                    offset = 0
                    while offset < len(replacement_bytes):
                        written = os.pwrite(
                            descriptor,
                            replacement_bytes[offset:],
                            offset,
                        )
                        self.assertGreater(written, 0)
                        offset += written
                finally:
                    original_close(descriptor)
                rewritten.set()

            def rewriting_stat(
                path: str | os.PathLike[str],
                *args: object,
                **kwargs: object,
            ) -> os.stat_result:
                nonlocal database_stat_count
                result = original_stat(path, *args, **kwargs)
                if (
                    armed.is_set()
                    and path == store_module.DATABASE_FILENAME
                    and kwargs.get("dir_fd") == root_fd[0]
                ):
                    database_stat_count += 1
                    if database_stat_count == 2:
                        rewrite_database()
                return result

            try:
                with (
                    mock.patch.object(
                        os,
                        "stat",
                        side_effect=rewriting_stat,
                    ),
                    self.assertRaises(StoreUnavailableError),
                ):
                    _InPlaceRewriteStore(source_root, busy_timeout_ms=100)
                self.assertTrue(rewritten.is_set())
            finally:
                descriptor = original_open(database, os.O_WRONLY)
                try:
                    offset = 0
                    while offset < len(original_bytes):
                        written = os.pwrite(descriptor, original_bytes[offset:], offset)
                        self.assertGreater(written, 0)
                        offset += written
                finally:
                    original_close(descriptor)
            self.assertEqual(gate_before, (gate.stat().st_mode, gate.read_bytes()))

    def test_classifier_retries_one_snapshot_race_with_same_active_peer_marker(
        self,
    ) -> None:
        """Only a live cooperating marker holder may authorize one retry."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-peer-marker-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root) as peer:
                del peer

                class _PeerRaceStore(CoordinationStore):
                    snapshot_calls = 0

                    def _classify_established_image_snapshot_once(self) -> None:
                        type(self).snapshot_calls += 1
                        if type(self).snapshot_calls == 1:
                            raise store_module._ClassifierSnapshotRace(
                                "injected source snapshot race"
                            )
                        super()._classify_established_image_snapshot_once()

                with _PeerRaceStore(root, busy_timeout_ms=100):
                    pass
                self.assertEqual(2, _PeerRaceStore.snapshot_calls)

    def test_classifier_retries_sidecar_disappearance_with_active_peer_marker(
        self,
    ) -> None:
        """A disappearing sidecar is retryable only with a live peer marker."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-sidecar-race-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            sidecar = root / f"{store_module.DATABASE_FILENAME}-wal"
            sidecar.touch(mode=0o600)
            armed = threading.Event()
            removed = threading.Event()
            root_fd: list[int | None] = [None]

            class _PeerSidecarRaceStore(CoordinationStore):
                def _classify_established_image_before_gate(self) -> None:
                    root_fd[0] = self._state_root_fd
                    armed.set()
                    super()._classify_established_image_before_gate()

            original_open = cast(Callable[..., int], os.open)

            def remove_sidecar_before_open(
                path: str | os.PathLike[str],
                flags: int,
                *args: object,
                **kwargs: object,
            ) -> int:
                if (
                    armed.is_set()
                    and not removed.is_set()
                    and path == f"{store_module.DATABASE_FILENAME}-wal"
                    and kwargs.get("dir_fd") == root_fd[0]
                ):
                    sidecar.unlink()
                    removed.set()
                return original_open(path, flags, *args, **kwargs)

            with CoordinationStore(root) as peer:
                del peer
                with mock.patch.object(
                    os,
                    "open",
                    side_effect=remove_sidecar_before_open,
                ):
                    try:
                        with _PeerSidecarRaceStore(root, busy_timeout_ms=100):
                            pass
                    except StoreUnavailableError as exc:
                        self.fail(f"active peer sidecar race was not retried: {exc}")
            self.assertTrue(removed.is_set())

    def test_classifier_rejects_bogus_nonzero_wal_and_shm_without_root_mutation(
        self,
    ) -> None:
        """Nonzero sidecars must be shape-validated before normal open."""

        for suffix, payload in (
            ("-wal", b"bogus-wal-sidecar"),
            ("-shm", b"bogus-shm-sidecar"),
        ):
            with (
                self.subTest(suffix=suffix),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-task-verification-bogus-sidecar-"
                ) as temporary,
            ):
                root = _state_root(temporary)
                with CoordinationStore(root):
                    pass
                sidecar = root / f"{store_module.DATABASE_FILENAME}{suffix}"
                sidecar.write_bytes(payload)
                sidecar.chmod(0o600)
                before = tuple(
                    (path.name, path.stat().st_mode, path.read_bytes())
                    for path in sorted(root.iterdir(), key=lambda item: item.name)
                )
                gate = root.parent / store_module.LIFETIME_GATE_FILENAME
                gate_before = (gate.stat().st_mode, gate.read_bytes())
                with self.assertRaises(StoreUnavailableError):
                    CoordinationStore(root, busy_timeout_ms=100)
                self.assertEqual(
                    before,
                    tuple(
                        (path.name, path.stat().st_mode, path.read_bytes())
                        for path in sorted(root.iterdir(), key=lambda item: item.name)
                    ),
                )
                self.assertEqual(gate_before, (gate.stat().st_mode, gate.read_bytes()))

    def test_classifier_enforces_wal_header_and_frame_alignment_and_shm_size(
        self,
    ) -> None:
        """Nonzero WAL/SHM shapes must fail before source-side cleanup."""

        wal_header = bytearray(32)
        wal_header[0:4] = (0x377F0682).to_bytes(4, "big")
        wal_header[8:12] = (512).to_bytes(4, "big")
        malformed = (
            ("-wal", bytes(wal_header)[:-1]),
            ("-wal", bytes(wal_header) + b"frame"),
            ("-wal", b"\x00" * 32),
            ("-shm", b"x" * (32 * 1024 + 1)),
        )
        for suffix, payload in malformed:
            with (
                self.subTest(suffix=suffix, size=len(payload)),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-task-verification-sidecar-shape-"
                ) as temporary,
            ):
                root = _state_root(temporary)
                with CoordinationStore(root):
                    pass
                sidecar = root / f"{store_module.DATABASE_FILENAME}{suffix}"
                sidecar.write_bytes(payload)
                sidecar.chmod(0o600)
                before = tuple(
                    (path.name, path.stat().st_mode, path.read_bytes())
                    for path in sorted(root.iterdir(), key=lambda item: item.name)
                )
                with self.assertRaises(StoreUnavailableError):
                    CoordinationStore(root, busy_timeout_ms=100)
                self.assertEqual(
                    before,
                    tuple(
                        (path.name, path.stat().st_mode, path.read_bytes())
                        for path in sorted(root.iterdir(), key=lambda item: item.name)
                    ),
                )

    def test_classifier_rejects_wal_page_size_mismatch_before_sqlite_open(self) -> None:
        """Known-incompatible main/WAL page sizes fail before temp SQLite open."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-wal-binding-page-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / store_module.DATABASE_FILENAME
            main_header = database.read_bytes()[:100]
            main_page_size = int.from_bytes(main_header[16:18], "big")
            if main_page_size == 1:
                main_page_size = 65_536
            wal_page_size = 8_192 if main_page_size != 8_192 else 4_096
            wal_header = bytearray(32)
            wal_header[0:4] = (0x377F0682).to_bytes(4, "big")
            wal_header[4:8] = (3_007_000).to_bytes(4, "big")
            wal_header[8:12] = wal_page_size.to_bytes(4, "big")
            sidecar = root / f"{store_module.DATABASE_FILENAME}-wal"
            sidecar.write_bytes(wal_header)
            sidecar.chmod(0o600)
            before = tuple(
                (path.name, path.stat().st_mode, path.read_bytes())
                for path in sorted(root.iterdir(), key=lambda item: item.name)
            )
            gate = root.parent / store_module.LIFETIME_GATE_FILENAME
            gate_before = (gate.stat().st_mode, gate.read_bytes())
            connect_calls: list[object] = []

            def unexpected_connect(*args: object, **kwargs: object) -> object:
                connect_calls.append((args, kwargs))
                raise AssertionError("WAL binding opened SQLite too early")

            with (
                mock.patch.object(
                    sqlite3,
                    "connect",
                    side_effect=unexpected_connect,
                ),
                self.assertRaises(StoreIntegrityError),
            ):
                CoordinationStore(root, busy_timeout_ms=100)
            self.assertEqual([], connect_calls)
            self.assertEqual(
                before,
                tuple(
                    (path.name, path.stat().st_mode, path.read_bytes())
                    for path in sorted(root.iterdir(), key=lambda item: item.name)
                ),
            )
            self.assertEqual(gate_before, (gate.stat().st_mode, gate.read_bytes()))

    def test_classifier_rejects_non_wal_main_header_with_nonzero_wal(self) -> None:
        """A non-WAL main header cannot be paired with a nonzero WAL."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-wal-binding-mode-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / store_module.DATABASE_FILENAME
            original = database.read_bytes()
            modified = bytearray(original)
            modified[18:20] = b"\x01\x01"
            database.write_bytes(modified)
            wal_header = bytearray(32)
            wal_header[0:4] = (0x377F0682).to_bytes(4, "big")
            wal_header[4:8] = (3_007_000).to_bytes(4, "big")
            wal_header[8:12] = int.from_bytes(original[16:18], "big").to_bytes(
                4,
                "big",
            )
            sidecar = root / f"{store_module.DATABASE_FILENAME}-wal"
            sidecar.write_bytes(wal_header)
            sidecar.chmod(0o600)
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(root, busy_timeout_ms=100)

    def test_classifier_rejects_unsupported_wal_format_version(self) -> None:
        """Unknown WAL format versions fail closed before SQLite open."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-wal-binding-version-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / store_module.DATABASE_FILENAME
            main_header = database.read_bytes()[:100]
            wal_header = bytearray(32)
            wal_header[0:4] = (0x377F0682).to_bytes(4, "big")
            wal_header[4:8] = (3_007_001).to_bytes(4, "big")
            wal_header[8:12] = int.from_bytes(main_header[16:18], "big").to_bytes(
                4,
                "big",
            )
            sidecar = root / f"{store_module.DATABASE_FILENAME}-wal"
            sidecar.write_bytes(wal_header)
            sidecar.chmod(0o600)
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(root, busy_timeout_ms=100)

    def test_wal_binding_normalizes_65536_page_size_and_ignores_salt(self) -> None:
        """WAL binding compares normalized page size, not salt/checksum fields."""

        validator = getattr(store_module, "_validate_sqlite_wal_binding", None)
        self.assertTrue(callable(validator), "WAL binding helper is missing")
        assert callable(validator)
        main_header = bytearray(100)
        main_header[:16] = b"SQLite format 3\x00"
        main_header[16:18] = b"\x00\x01"
        main_header[18:20] = b"\x02\x02"
        wal_header = bytearray(32)
        wal_header[0:4] = (0x377F0682).to_bytes(4, "big")
        wal_header[4:8] = (3_007_000).to_bytes(4, "big")
        wal_header[8:12] = (65_536).to_bytes(4, "big")
        wal_header[16:24] = b"salt-one"
        validator(bytes(main_header), bytes(wal_header), 32)
        wal_header[16:24] = b"salt-two"
        validator(bytes(main_header), bytes(wal_header), 32)
        validator(bytes(main_header), b"not-a-wal-header", 0)
        rollback_header = bytearray(main_header)
        rollback_header[18:20] = b"\x01\x01"
        with self.assertRaises(ValueError):
            validator(
                bytes(rollback_header),
                b"",
                0,
                require_main_wal_mode=True,
            )

    def test_classifier_source_close_uncertainty_is_retryable_and_identity_safe(
        self,
    ) -> None:
        """A source close fault is retained by the constructor cleanup owner."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-source-close-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            armed = threading.Event()
            root_fd: list[int | None] = [None]
            source_fds: set[int] = set()
            failures = [2]

            class _SourceCloseFaultStore(CoordinationStore):
                def _classify_established_image_before_gate(self) -> None:
                    root_fd[0] = self._state_root_fd
                    armed.set()
                    super()._classify_established_image_before_gate()

            original_open = cast(Callable[..., int], os.open)
            original_close = cast(Callable[..., None], os.close)

            def tracking_open(
                path: str | os.PathLike[str],
                flags: int,
                *args: object,
                **kwargs: object,
            ) -> int:
                descriptor = original_open(path, flags, *args, **kwargs)
                if (
                    armed.is_set()
                    and path == store_module.DATABASE_FILENAME
                    and kwargs.get("dir_fd") == root_fd[0]
                ):
                    source_fds.add(descriptor)
                return descriptor

            def failing_close(fd: int) -> None:
                if fd in source_fds and failures[0] > 0:
                    failures[0] -= 1
                    raise OSError("injected classifier source close fault")
                original_close(fd)

            with (
                mock.patch.object(
                    os,
                    "open",
                    side_effect=tracking_open,
                ),
                mock.patch.object(
                    os,
                    "close",
                    side_effect=failing_close,
                ),
                self.assertRaises(StoreUnavailableError) as raised,
            ):
                _SourceCloseFaultStore(root, busy_timeout_ms=100)
            self.assertTrue(source_fds)
            raised.exception.retry_cleanup()
            for descriptor in source_fds:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_classifier_connection_close_uncertainty_preserves_body_and_retry(
        self,
    ) -> None:
        """Temporary SQLite close faults expose a retryable cleanup owner."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-connection-close-"
        ) as temporary:
            root = _state_root(temporary)
            with CoordinationStore(root):
                pass
            original_connect = cast(Callable[..., sqlite3.Connection], sqlite3.connect)
            proxies: list[object] = []

            class _ConnectionProxy:
                def __init__(self, connection: sqlite3.Connection) -> None:
                    self._connection = connection
                    self.close_calls = 0
                    self.failures = 1

                @property
                def row_factory(self) -> SQLiteRowFactory:
                    return self._connection.row_factory

                @row_factory.setter
                def row_factory(self, value: SQLiteRowFactory) -> None:
                    self._connection.row_factory = value

                def __getattr__(self, name: str) -> object:
                    return getattr(self._connection, name)

                def close(self) -> None:
                    self.close_calls += 1
                    if self.failures:
                        self.failures -= 1
                        raise OSError("injected classifier connection close fault")
                    self._connection.close()

            def connect_with_proxy(*args: object, **kwargs: object) -> _ConnectionProxy:
                proxy = _ConnectionProxy(original_connect(*args, **kwargs))
                proxies.append(proxy)
                return proxy

            with (
                mock.patch.object(
                    sqlite3,
                    "connect",
                    side_effect=connect_with_proxy,
                ),
                mock.patch.object(
                    store_module,
                    "_validate_existing_schema",
                    side_effect=StoreSchemaError("classifier body error"),
                ),
                self.assertRaises(StoreSchemaError) as raised,
            ):
                CoordinationStore(root, busy_timeout_ms=100)
            self.assertEqual("classifier body error", str(raised.exception))
            self.assertEqual(1, len(proxies))
            proxy = cast(_ConnectionProxy, proxies[0])
            raised.exception.retry_cleanup()
            self.assertEqual(2, proxy.close_calls)

    def _assert_foundation_gate_rejects_nonempty_ledger(
        self,
        state_root: Path,
        before_open: tuple[tuple[str, bool, int | None, bytes | None], ...],
    ) -> None:
        with (
            self.assertRaises(StoreIntegrityError) as raised,
            CoordinationStore(state_root),
        ):
            pass
        self.assertIs(type(raised.exception), StoreIntegrityError)
        self.assertEqual(_FOUNDATION_NOT_READY_MESSAGE, str(raised.exception))
        self.assertEqual(before_open, _foundation_snapshot(state_root))

    def test_nonempty_ledger_fails_closed_for_image_and_backup_inspection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-nonempty-inspect-"
        ) as temporary:
            state_root, root, _ = _schema4_workflow_image(temporary)
            (
                _snapshot,
                task_state,
                task_bytes,
                task_digest,
                _request,
                _receipt,
                _request_bytes,
                _request_digest,
                _receipt_bytes,
                _effect,
                _result,
            ) = _typed_verification_payloads(root)
            artifact = backup.SQLiteBackup(
                state_root,
                busy_timeout_ms=100,
            ).create("nonempty-source")
            for database in (
                state_root / store_module.DATABASE_FILENAME,
                state_root / artifact.database_basename,
            ):
                with closing(
                    sqlite3.connect(str(database), isolation_level=None)
                ) as connection:
                    _insert_nonempty_ledger_row(
                        connection,
                        "task_policy_states",
                        _task_row_values(root, task_state, task_bytes, task_digest),
                    )
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            before = _root_inventory(state_root)
            primary = state_root / store_module.DATABASE_FILENAME
            primary_fd = os.open(
                primary,
                store_module._open_flags(directory=False, writable=False),
            )
            try:
                with self.assertRaises(StoreIntegrityError):
                    store_module.RestoreStoreAuthority().inspect_image(primary_fd)
            finally:
                os.close(primary_fd)
            self.assertEqual(before, _root_inventory(state_root))

            with self.assertRaises(backup.BackupIntegrityError):
                backup.SQLiteBackup(state_root, busy_timeout_ms=100).inspect(
                    artifact.database_basename
                )
            self.assertEqual(before, _root_inventory(state_root))

            with self.assertRaises((backup.BackupError, store_module.StoreError)):
                backup.SQLiteBackup(state_root, busy_timeout_ms=100).create(
                    "nonempty-created"
                )
            self.assertEqual(before, _root_inventory(state_root))

    def test_nonempty_ledger_gate_precedes_workflow_and_high_water_validation(
        self,
    ) -> None:
        """The foundation reject must run before any unbounded row scan."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-ledger-order-"
        ) as temporary:
            state_root, root, _ = _schema4_workflow_image(temporary)
            (
                _snapshot,
                task_state,
                task_bytes,
                task_digest,
                _request,
                _receipt,
                _request_bytes,
                _request_digest,
                _receipt_bytes,
                _effect,
                _result,
            ) = _typed_verification_payloads(root)
            artifact = backup.SQLiteBackup(
                state_root,
                busy_timeout_ms=100,
            ).create("ledger-order")
            for database in (
                state_root / store_module.DATABASE_FILENAME,
                state_root / artifact.database_basename,
            ):
                with closing(
                    sqlite3.connect(str(database), isolation_level=None)
                ) as connection:
                    _insert_nonempty_ledger_row(
                        connection,
                        "task_policy_states",
                        _task_row_values(root, task_state, task_bytes, task_digest),
                    )
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            before = _root_inventory(state_root)
            with (
                mock.patch.object(
                    store_module,
                    "_validate_workflow_rows_for_connection",
                    side_effect=StoreIntegrityError(
                        "workflow validation ran before the foundation gate"
                    ),
                ) as workflow_validation,
                mock.patch.object(
                    store_module,
                    "_validate_image_high_water",
                    side_effect=StoreIntegrityError(
                        "high-water validation ran before the foundation gate"
                    ),
                ) as high_water_validation,
                self.assertRaises(StoreIntegrityError) as raised,
            ):
                CoordinationStore(state_root, busy_timeout_ms=100)
            self.assertIn("SQLite task policy", str(raised.exception))
            self.assertFalse(workflow_validation.called)
            self.assertFalse(high_water_validation.called)
            self.assertEqual(before, _root_inventory(state_root))

            primary_fd = os.open(
                state_root / store_module.DATABASE_FILENAME,
                store_module._open_flags(directory=False, writable=False),
            )
            try:
                with (
                    mock.patch.object(
                        store_module,
                        "_validate_workflow_rows_for_connection",
                        side_effect=StoreIntegrityError(
                            "workflow validation ran before the foundation gate"
                        ),
                    ) as workflow_validation,
                    mock.patch.object(
                        store_module,
                        "_validate_image_high_water",
                        side_effect=StoreIntegrityError(
                            "high-water validation ran before the foundation gate"
                        ),
                    ) as high_water_validation,
                    self.assertRaises(StoreIntegrityError) as raised,
                ):
                    store_module.RestoreStoreAuthority().inspect_image(primary_fd)
                self.assertEqual(_FOUNDATION_NOT_READY_MESSAGE, str(raised.exception))
                self.assertFalse(workflow_validation.called)
                self.assertFalse(high_water_validation.called)
            finally:
                os.close(primary_fd)
            self.assertEqual(before, _root_inventory(state_root))

            with (
                mock.patch.object(
                    store_module,
                    "_validate_workflow_rows_for_connection",
                    side_effect=StoreIntegrityError(
                        "workflow validation ran before the foundation gate"
                    ),
                ) as workflow_validation,
                mock.patch.object(
                    store_module,
                    "_validate_image_high_water",
                    side_effect=StoreIntegrityError(
                        "high-water validation ran before the foundation gate"
                    ),
                ) as high_water_validation,
                self.assertRaises(backup.BackupIntegrityError) as backup_raised,
            ):
                backup.SQLiteBackup(state_root, busy_timeout_ms=100).inspect(
                    artifact.database_basename
                )
            cause = backup_raised.exception.__cause__
            self.assertIs(type(cause), StoreIntegrityError)
            assert isinstance(cause, StoreIntegrityError)
            self.assertEqual(_FOUNDATION_NOT_READY_MESSAGE, str(cause))
            self.assertFalse(workflow_validation.called)
            self.assertFalse(high_water_validation.called)
            self.assertEqual(before, _root_inventory(state_root))

    def test_workflow_validation_streams_current_and_exact_v3_rows(self) -> None:
        """Current and migration-only workflow rows must not use fetchall()."""

        cases = (
            (4, (2, 2, 2, 4), "current"),
            (3, (1, 1, 1, 2), "v3"),
        )
        for schema_version, expected_counts, label in cases:
            with (
                self.subTest(schema_version=schema_version),
                tempfile.TemporaryDirectory(
                    prefix=f"agent-team-task-verification-stream-{label}-"
                ) as temporary,
            ):
                if schema_version == 4:
                    state_root, _root, _second_root = _schema4_workflow_image(
                        temporary,
                        two_roots=True,
                    )
                else:
                    state_root = _make_nonempty_v3_state(
                        self,
                        temporary,
                        "v3-stream",
                    )
                connection = sqlite3.connect(
                    str(state_root / store_module.DATABASE_FILENAME),
                    isolation_level=None,
                )
                guarded = _WorkflowFetchallGuardConnection(connection)
                try:
                    counts = store_module._validate_workflow_rows_for_connection(
                        cast(Any, guarded),
                        store_schema=schema_version,
                    )
                finally:
                    guarded.close()
                self.assertIs(type(counts), tuple)
                self.assertEqual(expected_counts, counts)

    def test_streaming_preserves_root_chain_error_precedence(self) -> None:
        """A root-chain fault must be diagnosed before operation semantics."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-stream-order-"
        ) as temporary:
            state_root, root, _ = _schema4_workflow_image(temporary)
            with closing(
                sqlite3.connect(
                    str(state_root / store_module.DATABASE_FILENAME),
                    isolation_level=None,
                )
            ) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT * FROM workflow_events WHERE root_key = ? "
                    "ORDER BY workflow_sequence DESC LIMIT 1",
                    (root.root_key,),
                ).fetchone()
                self.assertIsNotNone(row)
                assert row is not None
                changed = dict(row)
                changed["task_sequence_before"] = 1
                changed["event_digest"] = (
                    store_module.CoordinationStore._workflow_event_digest(changed)
                )
                connection.execute("DROP TRIGGER workflow_events_no_update")
                try:
                    connection.execute(
                        "UPDATE workflow_events "
                        "SET task_sequence_before = ?, event_digest = ? "
                        "WHERE workflow_event_id = ?",
                        (
                            changed["task_sequence_before"],
                            changed["event_digest"],
                            changed["workflow_event_id"],
                        ),
                    )
                finally:
                    connection.execute(_WORKFLOW_EVENTS_NO_UPDATE_TRIGGER_SQL)

            with self.assertRaises(StoreIntegrityError) as raised:
                CoordinationStore(state_root, busy_timeout_ms=100)
            self.assertEqual(
                "SQLite workflow task sequence chain differs",
                str(raised.exception),
            )

    def test_foundation_gate_rejects_nonempty_ledger_with_cross_root_event_pointer(
        self,
    ) -> None:
        """A non-empty verification ledger is outside the foundation open contract."""

        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-cross-root-"
        ) as temporary:
            state_root, root, second_root = _schema4_workflow_image(
                temporary, two_roots=True
            )
            self.assertIsNotNone(second_root)
            assert second_root is not None
            (
                snapshot,
                task_state,
                task_bytes,
                task_digest,
                request,
                _receipt,
                request_bytes,
                request_digest,
                _receipt_bytes,
                _effect,
                _result,
            ) = _typed_verification_payloads(root)
            before_injection = _foundation_snapshot(state_root)
            with closing(
                sqlite3.connect(
                    str(state_root / store_module.DATABASE_FILENAME),
                    isolation_level=None,
                )
            ) as connection:
                _insert_nonempty_ledger_row(
                    connection,
                    "task_policy_states",
                    _task_row_values(root, task_state, task_bytes, task_digest),
                )
                foreign_event = connection.execute(
                    "SELECT workflow_event_id, event_digest FROM workflow_events "
                    "WHERE root_key = ? ORDER BY workflow_event_id LIMIT 1",
                    (second_root.root_key,),
                ).fetchone()
                self.assertIsNotNone(foreign_event)
                assert foreign_event is not None
                operation = _operation_row_values(
                    connection,
                    root,
                    task_state,
                    task_digest,
                    snapshot,
                    request,
                    request_bytes,
                    request_digest,
                    changes={
                        "prepare_event_id": int(foreign_event[0]),
                        "prepare_event_digest": str(foreign_event[1]),
                    },
                )
                _insert_nonempty_ledger_row(
                    connection, "verification_operations", operation
                )
            after_injection = _foundation_snapshot(state_root)
            _assert_injection_preserved_lifecycle_files(
                self, before_injection, after_injection
            )
            self._assert_foundation_gate_rejects_nonempty_ledger(
                state_root, after_injection
            )

    def test_task_row_validator_rejects_noncanonical_or_unbound_state(
        self,
    ) -> None:
        """Normal open accepts only a canonical task row bound to its checkpoint."""

        cases = ("bytes", "digest", "scalar")
        for case in cases:
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-task-verification-task-state-"
                ) as temporary,
            ):
                state_root, root, _ = _schema4_workflow_image(temporary)
                (
                    _snapshot,
                    task_state,
                    task_bytes,
                    task_digest,
                    _request,
                    _receipt,
                    _request_bytes,
                    _request_digest,
                    _receipt_bytes,
                    _effect,
                    _result,
                ) = _typed_verification_payloads(root)
                other_state = replace(
                    task_state,
                    sequence=task_state.sequence + 1,
                    phase=TaskPhase.VERIFYING,
                )
                other_bytes = task_verification_ledger.encode_task_state(other_state)
                other_digest = str(
                    task_verification_ledger.task_state_digest(other_bytes)
                )
                changes: dict[str, object]
                if case == "bytes":
                    changes = {"state_bytes": other_bytes}
                elif case == "digest":
                    changes = {"state_digest": other_digest}
                else:
                    changes = {"phase": TaskPhase.VERIFYING.value}
                before_injection = _foundation_snapshot(state_root)
                with closing(
                    sqlite3.connect(
                        str(state_root / store_module.DATABASE_FILENAME),
                        isolation_level=None,
                    )
                ) as connection:
                    _insert_nonempty_ledger_row(
                        connection,
                        "task_policy_states",
                        _task_row_values(
                            root,
                            task_state,
                            task_bytes,
                            task_digest,
                            changes=changes,
                        ),
                    )
                after_injection = _foundation_snapshot(state_root)
                _assert_injection_preserved_lifecycle_files(
                    self, before_injection, after_injection
                )
                with self.assertRaises(StoreIntegrityError) as raised:
                    CoordinationStore(state_root, busy_timeout_ms=100)
                self.assertIn("SQLite task policy", str(raised.exception))
                self.assertEqual(after_injection, _foundation_snapshot(state_root))

    def test_foundation_gate_rejects_nonempty_operation_row(
        self,
    ) -> None:
        """A non-empty operation projection is rejected until a Store writer exists."""

        cases = (
            "approval-bytes",
            "approval-digest",
            "request-bytes",
            "request-digest",
            "record-digest",
            "pointer-digest",
            "pointer-event",
        )
        for case in cases:
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-task-verification-operation-"
                ) as temporary,
            ):
                state_root, root, _ = _schema4_workflow_image(temporary)
                (
                    snapshot,
                    task_state,
                    task_bytes,
                    task_digest,
                    request,
                    _receipt,
                    request_bytes,
                    request_digest,
                    _receipt_bytes,
                    _effect,
                    _result,
                ) = _typed_verification_payloads(root)
                before_injection = _foundation_snapshot(state_root)
                with closing(
                    sqlite3.connect(
                        str(state_root / store_module.DATABASE_FILENAME),
                        isolation_level=None,
                    )
                ) as connection:
                    _insert_nonempty_ledger_row(
                        connection,
                        "task_policy_states",
                        _task_row_values(root, task_state, task_bytes, task_digest),
                    )
                    events = connection.execute(
                        "SELECT workflow_event_id, event_digest FROM workflow_events "
                        "WHERE root_key = ? ORDER BY workflow_event_id",
                        (root.root_key,),
                    ).fetchall()
                    self.assertGreaterEqual(len(events), 2)
                    operation_changes: dict[str, object]
                    if case == "approval-bytes":
                        operation_changes = {
                            "approval_binding_bytes": b"corrupt-approval-bytes"
                        }
                    elif case == "approval-digest":
                        operation_changes = {
                            "approval_binding_digest": _SCHEMA4_DIGEST_2
                        }
                    elif case == "request-bytes":
                        operation_changes = {"request_bytes": request_bytes + b" "}
                    elif case == "request-digest":
                        operation_changes = {"request_digest": "f" * 64}
                    elif case == "record-digest":
                        operation_changes = {"record_digest": _SCHEMA4_DIGEST_3}
                    elif case == "pointer-digest":
                        operation_changes = {"prepare_event_digest": _SCHEMA4_DIGEST_3}
                    else:
                        operation_changes = {
                            "prepare_event_id": int(events[1][0]),
                            "prepare_event_digest": str(events[1][1]),
                        }
                    _insert_nonempty_ledger_row(
                        connection,
                        "verification_operations",
                        _operation_row_values(
                            connection,
                            root,
                            task_state,
                            task_digest,
                            snapshot,
                            request,
                            request_bytes,
                            request_digest,
                            changes=operation_changes,
                        ),
                    )
                after_injection = _foundation_snapshot(state_root)
                _assert_injection_preserved_lifecycle_files(
                    self, before_injection, after_injection
                )
                self._assert_foundation_gate_rejects_nonempty_ledger(
                    state_root, after_injection
                )

    def test_foundation_gate_rejects_nonempty_receipt_row(
        self,
    ) -> None:
        """A non-empty receipt projection is rejected until a Store writer exists."""

        cases = ("projection", "digest", "root", "verification")
        for case in cases:
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-task-verification-receipt-"
                ) as temporary,
            ):
                state_root, root, second_root = _schema4_workflow_image(
                    temporary, two_roots=case == "root"
                )
                (
                    snapshot,
                    task_state,
                    task_bytes,
                    task_digest,
                    request,
                    receipt,
                    request_bytes,
                    request_digest,
                    receipt_bytes,
                    effect,
                    result,
                ) = _typed_verification_payloads(root)
                changed_result = replace(
                    result,
                    outcome=gate.VerificationOutcome.FAILED,
                    exit_code=17,
                )
                changed_receipt = gate._make_receipt(
                    receipt_ref=ReceiptRef(str(receipt.receipt_ref)),
                    request=request,
                    result=changed_result,
                    effect=effect,
                    after_snapshot=request.before_snapshot,
                )
                changed_receipt_projection = task_verification_ledger.verification_receipt_projection_from_receipt(
                    changed_receipt
                )
                changed_receipt_bytes = (
                    task_verification_ledger.encode_verification_receipt_projection(
                        changed_receipt_projection
                    )
                )
                row_root = root
                operation_changes: dict[str, object] = {
                    "status": "RECEIPTED",
                    "effect_owner": "effect-owner",
                    "effect_attempt": 1,
                    "effect_epoch": 1,
                    "effect_fence": 1,
                    "effect_nonce": "effect-nonce",
                    "receipt_ref": str(receipt.receipt_ref),
                    "receipt_digest": str(receipt.receipt_digest),
                }
                receipt_changes: dict[str, object] = {}
                verification_ref = "verification-1"
                if case == "projection":
                    receipt_changes["receipt_bytes"] = changed_receipt_bytes
                elif case == "digest":
                    receipt_changes["receipt_digest"] = "f" * 64
                elif case == "root":
                    self.assertIsNotNone(second_root)
                    assert second_root is not None
                    row_root = second_root
                    operation_changes["root_key"] = second_root.root_key
                else:
                    verification_ref = "verification-foreign"
                    operation_changes["verification_ref"] = verification_ref
                    receipt_changes["verification_ref"] = verification_ref
                before_injection = _foundation_snapshot(state_root)
                with closing(
                    sqlite3.connect(
                        str(state_root / store_module.DATABASE_FILENAME),
                        isolation_level=None,
                    )
                ) as connection:
                    _insert_nonempty_ledger_row(
                        connection,
                        "task_policy_states",
                        _task_row_values(
                            row_root,
                            task_state,
                            task_bytes,
                            task_digest,
                            changes={"root_key": row_root.root_key},
                        ),
                    )
                    operation = _operation_row_values(
                        connection,
                        root,
                        task_state,
                        task_digest,
                        snapshot,
                        request,
                        request_bytes,
                        request_digest,
                        status="RECEIPTED",
                        root_key=row_root.root_key,
                        verification_ref=verification_ref,
                        changes=operation_changes,
                    )
                    event_rows = connection.execute(
                        "SELECT workflow_event_id, event_digest FROM workflow_events "
                        "WHERE root_key = ? ORDER BY workflow_event_id",
                        (row_root.root_key,),
                    ).fetchall()
                    self.assertGreaterEqual(len(event_rows), 2)
                    operation["receipt_event_id"] = int(event_rows[1][0])
                    operation["receipt_event_digest"] = str(event_rows[1][1])
                    _insert_nonempty_ledger_row(
                        connection, "verification_operations", operation
                    )
                    connection.execute("BEGIN")
                    try:
                        _insert_nonempty_ledger_row(
                            connection,
                            "verification_receipts",
                            _receipt_row_values(
                                receipt,
                                receipt_bytes,
                                root_key=row_root.root_key,
                                verification_ref=verification_ref,
                                changes=receipt_changes,
                            ),
                        )
                        connection.execute("COMMIT")
                    except BaseException:
                        connection.rollback()
                        raise
                after_injection = _foundation_snapshot(state_root)
                _assert_injection_preserved_lifecycle_files(
                    self, before_injection, after_injection
                )
                self._assert_foundation_gate_rejects_nonempty_ledger(
                    state_root, after_injection
                )

    def test_unknown_evidence_digest_uses_sha256_wrapper_grammar(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-task-verification-unknown-"
        ) as temporary:
            state_root, root, _ = _schema4_workflow_image(temporary)
            (
                snapshot,
                task_state,
                task_bytes,
                task_digest,
                request,
                _receipt,
                request_bytes,
                request_digest,
                _receipt_bytes,
                _effect,
                _result,
            ) = _typed_verification_payloads(root)
            try:
                with closing(
                    sqlite3.connect(
                        str(state_root / store_module.DATABASE_FILENAME),
                        isolation_level=None,
                    )
                ) as connection:
                    _insert_nonempty_ledger_row(
                        connection,
                        "task_policy_states",
                        _task_row_values(root, task_state, task_bytes, task_digest),
                    )
                    events = connection.execute(
                        "SELECT workflow_event_id, event_digest FROM workflow_events "
                        "WHERE root_key = ? ORDER BY workflow_event_id",
                        (root.root_key,),
                    ).fetchall()
                    operation = _operation_row_values(
                        connection,
                        root,
                        task_state,
                        task_digest,
                        snapshot,
                        request,
                        request_bytes,
                        request_digest,
                        status="UNKNOWN_EFFECT",
                        changes={
                            "effect_owner": "effect-owner",
                            "effect_attempt": 1,
                            "effect_epoch": 1,
                            "effect_fence": 1,
                            "effect_nonce": "effect-nonce",
                            "unknown_code": "runner-response-loss",
                            "unknown_evidence_digest": _SCHEMA4_DIGEST_4,
                            "unknown_event_id": int(events[1][0]),
                            "unknown_event_digest": str(events[1][1]),
                        },
                    )
                    _insert_nonempty_ledger_row(
                        connection, "verification_operations", operation
                    )
            except sqlite3.IntegrityError as error:
                self.fail(
                    "contract sha256: unknown_evidence_digest was rejected by DDL: "
                    f"{error}"
                )


if __name__ == "__main__":
    unittest.main()
