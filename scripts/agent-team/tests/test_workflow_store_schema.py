from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_team import workflow_store as workflow
from agent_team.store import (
    CoordinationStore,
    StoreIntegrityError,
    StoreMigrationRequiredError,
    StoreSchemaError,
)

WORKFLOW_TABLES = {
    "workflow_checkpoints",
    "workflow_operations",
    "workflow_receipts",
    "workflow_events",
}
PROVIDER_TABLES = {
    "store_meta",
    "operations",
    "operation_attempts",
    "effect_receipts",
    "transition_events",
}
EXPECTED_TABLES = PROVIDER_TABLES | WORKFLOW_TABLES

EXPECTED_COLUMNS: dict[str, tuple[tuple[str, str, int, int], ...]] = {
    "workflow_checkpoints": (
        ("root_key", "TEXT", 1, 1),
        ("team_id", "TEXT", 1, 0),
        ("workspace_path", "TEXT", 1, 0),
        ("workspace_device", "INTEGER", 1, 0),
        ("workspace_inode", "INTEGER", 1, 0),
        ("config_path", "TEXT", 1, 0),
        ("config_device", "INTEGER", 1, 0),
        ("config_inode", "INTEGER", 1, 0),
        ("config_digest", "TEXT", 1, 0),
        ("state_root", "TEXT", 1, 0),
        ("state_root_device", "INTEGER", 1, 0),
        ("state_root_inode", "INTEGER", 1, 0),
        ("run_id", "TEXT", 0, 0),
        ("main_terminal_id", "TEXT", 0, 0),
        ("checkpoint_version", "INTEGER", 1, 0),
        ("store_schema", "INTEGER", 1, 0),
        ("task_policy_version", "INTEGER", 0, 0),
        ("workflow_sequence", "INTEGER", 1, 0),
        ("task_sequence", "INTEGER", 0, 0),
        ("execution_mode", "TEXT", 1, 0),
        ("workflow_state", "TEXT", 1, 0),
        ("consumer_generation", "INTEGER", 1, 0),
        ("read_observed", "INTEGER", 1, 0),
        ("released", "INTEGER", 1, 0),
        ("checkpoint_bytes", "BLOB", 1, 0),
        ("checkpoint_digest", "TEXT", 1, 0),
        ("last_operation_id", "TEXT", 0, 0),
        ("last_operation_status", "TEXT", 0, 0),
        ("last_operation_receipt_id", "TEXT", 0, 0),
        ("updated_ns", "INTEGER", 1, 0),
    ),
    "workflow_operations": (
        ("operation_id", "TEXT", 1, 1),
        ("effect_key", "TEXT", 1, 0),
        ("root_key", "TEXT", 1, 0),
        ("action", "TEXT", 1, 0),
        ("request_digest", "TEXT", 1, 0),
        ("expected_workflow_sequence", "INTEGER", 1, 0),
        ("expected_task_sequence", "INTEGER", 0, 0),
        ("intent_sequence", "INTEGER", 1, 0),
        ("next_task_sequence", "INTEGER", 0, 0),
        ("run_id", "TEXT", 0, 0),
        ("main_terminal_id", "TEXT", 0, 0),
        ("task_id", "TEXT", 0, 0),
        ("dispatch_id", "TEXT", 0, 0),
        ("attempt", "INTEGER", 0, 0),
        ("terminal_id", "TEXT", 0, 0),
        ("delivery_id", "TEXT", 0, 0),
        ("message_id", "TEXT", 0, 0),
        ("consumer_generation", "INTEGER", 1, 0),
        ("owner", "TEXT", 1, 0),
        ("lease_epoch", "INTEGER", 1, 0),
        ("fencing_token", "INTEGER", 1, 0),
        ("status", "TEXT", 1, 0),
        ("receipt_id", "TEXT", 0, 0),
        ("created_ns", "INTEGER", 1, 0),
        ("updated_ns", "INTEGER", 1, 0),
        ("intent_digest", "TEXT", 1, 0),
        ("receipt_digest", "TEXT", 0, 0),
        ("evidence_ref", "TEXT", 0, 0),
    ),
    "workflow_receipts": (
        ("receipt_id", "TEXT", 1, 1),
        ("operation_id", "TEXT", 1, 0),
        ("effect_key", "TEXT", 1, 0),
        ("receipt_schema_version", "INTEGER", 1, 0),
        ("action", "TEXT", 1, 0),
        ("request_digest", "TEXT", 1, 0),
        ("effect_ref", "TEXT", 1, 0),
        ("result_kind", "TEXT", 1, 0),
        ("result_digest", "TEXT", 1, 0),
        ("evidence_ref", "TEXT", 1, 0),
        ("issued_ns", "INTEGER", 1, 0),
        ("run_id", "TEXT", 1, 0),
        ("main_terminal_id", "TEXT", 1, 0),
        ("task_id", "TEXT", 0, 0),
        ("dispatch_id", "TEXT", 0, 0),
        ("attempt", "INTEGER", 0, 0),
        ("terminal_id", "TEXT", 0, 0),
        ("delivery_id", "TEXT", 0, 0),
        ("message_id", "TEXT", 0, 0),
        ("consumer_generation", "INTEGER", 1, 0),
        ("owner", "TEXT", 1, 0),
        ("lease_epoch", "INTEGER", 1, 0),
        ("fencing_token", "INTEGER", 1, 0),
    ),
    "workflow_events": (
        ("workflow_event_id", "INTEGER", 0, 1),
        ("workflow_event_schema_version", "INTEGER", 1, 0),
        ("root_key", "TEXT", 1, 0),
        ("operation_id", "TEXT", 0, 0),
        ("workflow_sequence", "INTEGER", 1, 0),
        ("task_sequence_before", "INTEGER", 0, 0),
        ("task_sequence_after", "INTEGER", 0, 0),
        ("from_state", "TEXT", 0, 0),
        ("to_state", "TEXT", 1, 0),
        ("kind", "TEXT", 1, 0),
        ("actor", "TEXT", 1, 0),
        ("clock_ns", "INTEGER", 1, 0),
        ("request_digest", "TEXT", 1, 0),
        ("receipt_id", "TEXT", 0, 0),
        ("checkpoint_bytes", "BLOB", 1, 0),
        ("checkpoint_digest", "TEXT", 1, 0),
        ("evidence_ref", "TEXT", 0, 0),
        ("event_digest", "TEXT", 1, 0),
    ),
}

EXPECTED_WORKFLOW_INDEXES = {
    "workflow_checkpoints": {
        (1, "pk", ("root_key",)),
        (1, "u", ("root_key", "run_id")),
        (1, "u", ("root_key", "run_id", "main_terminal_id")),
    },
    "workflow_operations": {
        (1, "pk", ("operation_id",)),
        (1, "u", ("effect_key",)),
        (1, "u", ("operation_id", "effect_key")),
        (0, "c", ("root_key", "status", "updated_ns", "operation_id")),
    },
    "workflow_receipts": {
        (1, "pk", ("receipt_id",)),
        (1, "u", ("operation_id",)),
    },
    "workflow_events": {
        (1, "u", ("root_key", "workflow_sequence")),
        (0, "c", ("operation_id", "workflow_event_id")),
    },
}

EXPECTED_WORKFLOW_FOREIGN_KEYS = {
    "workflow_checkpoints": {
        (
            "workflow_operations",
            "last_operation_id",
            "operation_id",
            "RESTRICT",
            "RESTRICT",
        ),
        (
            "workflow_receipts",
            "last_operation_receipt_id",
            "receipt_id",
            "RESTRICT",
            "RESTRICT",
        ),
    },
    "workflow_operations": {
        ("workflow_checkpoints", "root_key", "root_key", "RESTRICT", "RESTRICT"),
        ("workflow_checkpoints", "run_id", "run_id", "RESTRICT", "RESTRICT"),
        ("workflow_checkpoints", "root_key", "root_key", "RESTRICT", "RESTRICT"),
        ("workflow_checkpoints", "run_id", "run_id", "RESTRICT", "RESTRICT"),
        (
            "workflow_checkpoints",
            "main_terminal_id",
            "main_terminal_id",
            "RESTRICT",
            "RESTRICT",
        ),
        ("workflow_receipts", "receipt_id", "receipt_id", "RESTRICT", "RESTRICT"),
    },
    "workflow_receipts": {
        ("workflow_operations", "operation_id", "operation_id", "RESTRICT", "RESTRICT"),
        ("workflow_operations", "effect_key", "effect_key", "RESTRICT", "RESTRICT"),
    },
    "workflow_events": {
        ("workflow_checkpoints", "root_key", "root_key", "RESTRICT", "RESTRICT"),
        ("workflow_operations", "operation_id", "operation_id", "RESTRICT", "RESTRICT"),
        ("workflow_receipts", "receipt_id", "receipt_id", "RESTRICT", "RESTRICT"),
    },
}

EXPECTED_WORKFLOW_TRIGGERS = {
    "workflow_receipts_require_committed",
    "workflow_receipts_no_update",
    "workflow_receipts_no_delete",
    "workflow_receipts_no_replace",
    "workflow_events_receipt_matches_operation",
    "workflow_events_no_update",
    "workflow_events_no_delete",
    "workflow_events_no_replace",
}
EXPECTED_PROVIDER_INDEXES = {
    "operations_status_idx",
    "transition_events_operation_idx",
    "transition_events_attempt_idx",
}
EXPECTED_PROVIDER_TRIGGERS = {
    "transition_events_no_update",
    "transition_events_no_delete",
    "transition_events_no_replace",
}

FROZEN_SCHEMA_SQL_DIGESTS = {
    (
        "table",
        "workflow_checkpoints",
    ): "sha256:9a86645151486002e0b572bb84b822e71d01f4f4f8e7172cb6a1f4a93164bdb8",
    (
        "table",
        "workflow_operations",
    ): "sha256:6a10cb56605760f0f9c8a99f24bc7c6e207072a957b8ca2a8ff96c763a3e82ce",
    (
        "table",
        "workflow_receipts",
    ): "sha256:8c38f2e2ab09d1f1c3c39798f10c0a4f15598298c954e3156da0d360bca1d6d5",
    (
        "table",
        "workflow_events",
    ): "sha256:f7af791e9a1533f1fd0ddf0fd254ee8edb4e916b306954739ee3d8fe4f9343f6",
    (
        "table",
        "transition_events",
    ): "sha256:bd5991dbd2e78fa74350259fc29c4eca1c99dc0eb05cac13a3df76871c697dcb",
    (
        "trigger",
        "workflow_receipts_require_committed",
    ): "sha256:c4f39e81fb8c3ee626f316b5f3994fcaf0000e9476cf997f2e3020fc898c3b82",
    (
        "trigger",
        "workflow_receipts_no_update",
    ): "sha256:f856bb3b66978907bf763e36adc436de9d823f5ad9f6f735365bb8d1d909c7e1",
    (
        "trigger",
        "workflow_receipts_no_delete",
    ): "sha256:b770c085b6a843c56d62142b91b0f389891aa6842e37b5d3e0027bedad7b622a",
    (
        "trigger",
        "workflow_receipts_no_replace",
    ): "sha256:6d2ff2dc82da31fdbbccf1aab18e5ae18dc280eda51a53ce7e2644adffbc7c13",
    (
        "trigger",
        "workflow_events_receipt_matches_operation",
    ): "sha256:46a4a75a8335c6f9c0ee9322b9f4b79e2ec9515a8618bdae3fad151e64821dce",
    (
        "trigger",
        "workflow_events_no_update",
    ): "sha256:5f62b7d803f797250fcc32a94f6e5b9b9329a15f02d68a17f5636aef3a150d2b",
    (
        "trigger",
        "workflow_events_no_delete",
    ): "sha256:b03b333ce1ccd6b0e4e268f2b2b3c577c97bad029157a779aa0a8461c4207321",
    (
        "trigger",
        "workflow_events_no_replace",
    ): "sha256:ce75c2c416b7f42f28842cd84be6916e1f989f387c3d44387e66c65d8da97c70",
}


def _state_root(parent: str) -> Path:
    root = Path(os.path.realpath(parent)) / "state"
    root.mkdir()
    root.chmod(0o700)
    return root


def _sql_digest(sql: str) -> str:
    normalized = " ".join(sql.strip().split())
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _schema_rows(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, str, int, int], ...]:
    return tuple(
        (
            str(row[1]),
            str(row[2]).upper(),
            int(row[3]),
            int(row[5]),
        )
        for row in connection.execute(f"PRAGMA table_info({table})")
    )


def _index_rows(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[int, str, tuple[str, ...]]]:
    result: set[tuple[int, str, tuple[str, ...]]] = set()
    for row in connection.execute(f"PRAGMA index_list({table})"):
        name = str(row[1])
        columns = tuple(
            str(column[2])
            for column in connection.execute(f"PRAGMA index_info({name})")
        )
        result.add((int(row[2]), str(row[3]), columns))
    return result


def _foreign_key_rows(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, str, str, str, str]]:
    return {
        (
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]).upper(),
            str(row[6]).upper(),
        )
        for row in connection.execute(f"PRAGMA foreign_key_list({table})")
    }


def _make_valid_legacy_v2_state(
    parent: str,
    name: str = "state",
) -> tuple[Path, bytes, tuple[str, ...]]:
    state_root = Path(os.path.realpath(parent)) / name
    state_root.mkdir()
    state_root.chmod(0o700)
    with CoordinationStore(state_root):
        pass
    database = state_root / "coordination.sqlite3"
    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        for trigger in (
            "workflow_receipts_require_committed",
            "workflow_receipts_no_update",
            "workflow_receipts_no_delete",
            "workflow_receipts_no_replace",
            "workflow_events_receipt_matches_operation",
            "workflow_events_no_update",
            "workflow_events_no_delete",
            "workflow_events_no_replace",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP INDEX workflow_operations_root_status_idx")
        connection.execute("DROP INDEX workflow_events_operation_idx")
        for table in (
            "workflow_events",
            "workflow_receipts",
            "workflow_operations",
            "workflow_checkpoints",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE store_meta SET value = 2 WHERE key = 'store_schema'")
        connection.execute("PRAGMA user_version = 2")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    return (
        state_root,
        database.read_bytes(),
        tuple(sorted(path.name for path in state_root.iterdir())),
    )


class WorkflowStoreSchemaTests(unittest.TestCase):
    def test_durable_seed_zero_is_rejected_as_incomplete_state(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-seed-zero-"
        ) as temporary:
            state_root = _state_root(temporary)
            state_metadata = state_root.stat()
            root = workflow.RootIdentity(
                root_key="root-seed-zero",
                team_id="team-1",
                workspace=workflow.PathIdentity("/workspace", 1, 2),
                config_path="/config.toml",
                config_device=1,
                config_inode=3,
                config_digest="sha256:" + "1" * 64,
                state_root=workflow.PathIdentity(
                    str(state_root),
                    state_metadata.st_dev,
                    state_metadata.st_ino,
                ),
            )
            seed = workflow.WorkflowRootSeed(root=root, updated_ns=0)
            projection = workflow.seed_scalar_projection(seed)
            columns = tuple(
                column[0] for column in EXPECTED_COLUMNS["workflow_checkpoints"]
            )
            with CoordinationStore(state_root) as store:
                connection = store._connection
                assert connection is not None
                with store._write_transaction():
                    connection.execute(
                        "INSERT INTO workflow_checkpoints("
                        + ", ".join(columns)
                        + ") VALUES ("
                        + ", ".join("?" for _ in columns)
                        + ")",
                        tuple(projection[column] for column in columns),
                    )
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(state_root)

    def test_fresh_store_has_v3_provider_and_four_workflow_tables(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-schema-"
        ) as temporary:
            state_root = _state_root(temporary)
            with CoordinationStore(state_root) as store:
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                self.assertEqual(
                    3, connection.execute("PRAGMA user_version").fetchone()[0]
                )
                self.assertEqual(
                    3,
                    connection.execute(
                        "SELECT value FROM store_meta WHERE key = 'store_schema'"
                    ).fetchone()[0],
                )
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertEqual(EXPECTED_TABLES, tables)
                provider_event_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'transition_events'"
                    ).fetchone()[0]
                )
                self.assertIn("event_schema_version", provider_event_sql)
                self.assertIn("= 2", provider_event_sql)
                workflow_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'workflow_events'"
                    ).fetchone()[0]
                )
                self.assertIn("workflow_event_schema_version", workflow_sql)
                self.assertIn("= 1", workflow_sql)

                trigger_names = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    )
                }
                self.assertTrue(EXPECTED_WORKFLOW_TRIGGERS <= trigger_names)
                objects = {
                    (str(row[0]), str(row[1]))
                    for row in connection.execute(
                        "SELECT type, name FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_autoindex_%'"
                    )
                }
                self.assertEqual(
                    {
                        *(("table", name) for name in EXPECTED_TABLES),
                        *(("index", name) for name in EXPECTED_PROVIDER_INDEXES),
                        *(
                            ("index", name)
                            for name in (
                                "workflow_operations_root_status_idx",
                                "workflow_events_operation_idx",
                            )
                        ),
                        *(("trigger", name) for name in EXPECTED_PROVIDER_TRIGGERS),
                        *(("trigger", name) for name in EXPECTED_WORKFLOW_TRIGGERS),
                    },
                    objects,
                )
                self.assertFalse(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'"
                    ).fetchone()
                )

    def test_workflow_sql_and_trigger_contract_has_independent_digest_oracle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-schema-oracle-"
        ) as temporary:
            state_root = _state_root(temporary)
            with CoordinationStore(state_root) as store:
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                observed: dict[tuple[str, str], str] = {}
                for kind, name in FROZEN_SCHEMA_SQL_DIGESTS:
                    row = connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
                        (kind, name),
                    ).fetchone()
                    self.assertIsNotNone(row, (kind, name))
                    assert row is not None
                    observed[(kind, name)] = _sql_digest(str(row[0]))
                self.assertTrue(
                    all(
                        len(value) == 71 for value in FROZEN_SCHEMA_SQL_DIGESTS.values()
                    )
                )
                self.assertEqual(FROZEN_SCHEMA_SQL_DIGESTS, observed)

    def test_workflow_columns_indexes_and_foreign_keys_are_exact(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-schema-"
        ) as temporary:
            state_root = _state_root(temporary)
            with CoordinationStore(state_root) as store:
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                for table, expected_columns in EXPECTED_COLUMNS.items():
                    self.assertEqual(
                        expected_columns, _schema_rows(connection, table), table
                    )
                for table, expected_indexes in EXPECTED_WORKFLOW_INDEXES.items():
                    self.assertEqual(
                        expected_indexes, _index_rows(connection, table), table
                    )
                for (
                    table,
                    expected_foreign_keys,
                ) in EXPECTED_WORKFLOW_FOREIGN_KEYS.items():
                    self.assertEqual(
                        expected_foreign_keys,
                        _foreign_key_rows(connection, table),
                        table,
                    )
                operation_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'workflow_operations'"
                    ).fetchone()[0]
                )
                self.assertEqual(
                    2, operation_sql.count("DEFERRABLE INITIALLY DEFERRED")
                )
                checkpoint_sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'workflow_checkpoints'"
                    ).fetchone()[0]
                )
                self.assertIn(
                    "length(checkpoint_bytes) BETWEEN 1 AND 1048576",
                    checkpoint_sql,
                )
                self.assertIn(
                    "intent_sequence = expected_workflow_sequence + 1",
                    operation_sql,
                )
                self.assertIn(
                    "FOREIGN KEY(root_key, run_id, main_terminal_id)\n"
                    "        REFERENCES workflow_checkpoints(root_key, run_id, main_terminal_id)\n"
                    "        ON UPDATE RESTRICT ON DELETE RESTRICT\n"
                    "        DEFERRABLE INITIALLY DEFERRED",
                    operation_sql,
                )

    def test_exact_legacy_v2_is_migration_required_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-schema-"
        ) as temporary:
            state_root, database_before, files_before = _make_valid_legacy_v2_state(
                temporary
            )
            with self.assertRaises(StoreMigrationRequiredError):
                CoordinationStore(state_root)
            self.assertEqual(
                database_before, (state_root / "coordination.sqlite3").read_bytes()
            )
            self.assertEqual(
                files_before, tuple(sorted(path.name for path in state_root.iterdir()))
            )

    def test_v2_marker_without_the_legacy_schema_is_not_migration_required(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-schema-"
        ) as temporary:
            state_root = _state_root(temporary)
            database = state_root / "coordination.sqlite3"
            connection = sqlite3.connect(str(database))
            try:
                connection.execute(
                    "CREATE TABLE store_meta "
                    "(key TEXT NOT NULL PRIMARY KEY, value INTEGER NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO store_meta(key, value) VALUES ('store_schema', 2)"
                )
                connection.execute("PRAGMA user_version = 2")
                connection.commit()
            finally:
                connection.close()
            database.chmod(0o600)
            marker = state_root / "writer.marker"
            marker.write_bytes(b'{"version":1,"state":"CLEAN"}\n')
            marker.chmod(0o600)
            with self.assertRaises(StoreSchemaError) as raised:
                CoordinationStore(state_root)
            self.assertNotIsInstance(raised.exception, StoreMigrationRequiredError)

    def test_workflow_trigger_body_mutation_is_not_an_exact_v3_schema(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-trigger-schema-"
        ) as temporary:
            state_root = _state_root(temporary)
            with CoordinationStore(state_root):
                pass
            connection = sqlite3.connect(str(state_root / "coordination.sqlite3"))
            try:
                connection.execute("DROP TRIGGER workflow_events_no_update")
                connection.execute(
                    """
                    CREATE TRIGGER workflow_events_no_update
                    BEFORE UPDATE ON workflow_events
                    BEGIN
                        SELECT NULL;
                    END
                    """
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(StoreSchemaError):
                CoordinationStore(state_root)

    def test_malformed_mixed_and_future_markers_are_generic_schema_errors(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-schema-"
        ) as temporary:
            variants = ("malformed", "mixed", "future")
            for variant in variants:
                state_root, _, _ = _make_valid_legacy_v2_state(temporary, variant)
                database = state_root / "coordination.sqlite3"
                connection = sqlite3.connect(str(database), isolation_level=None)
                try:
                    if variant == "malformed":
                        connection.execute("DROP INDEX operations_status_idx")
                    elif variant == "mixed":
                        connection.execute("PRAGMA user_version = 3")
                    else:
                        connection.execute(
                            "UPDATE store_meta SET value = 4 WHERE key = 'store_schema'"
                        )
                        connection.execute("PRAGMA user_version = 4")
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    connection.close()
                with self.assertRaises(StoreSchemaError) as raised:
                    CoordinationStore(state_root)
                self.assertNotIsInstance(raised.exception, StoreMigrationRequiredError)

    def test_workflow_trigger_guards_reject_receipt_and_event_mutation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-schema-"
        ) as temporary:
            state_root = _state_root(temporary)
            with CoordinationStore(state_root) as store:
                connection = store._connection
                self.assertIsNotNone(connection)
                assert connection is not None
                digest = "sha256:" + "0" * 64
                connection.execute(
                    """
                    INSERT INTO workflow_checkpoints(
                        root_key, team_id, workspace_path, workspace_device,
                        workspace_inode, config_path, config_device, config_inode,
                        config_digest, state_root, state_root_device, state_root_inode,
                        run_id, main_terminal_id, checkpoint_version, store_schema,
                        task_policy_version, workflow_sequence, task_sequence,
                        execution_mode, workflow_state, consumer_generation,
                        read_observed, released, checkpoint_bytes, checkpoint_digest,
                        last_operation_id, last_operation_status,
                        last_operation_receipt_id, updated_ns
                    ) VALUES (
                        'root-1', 'team-1', '/workspace', 1, 2, '/config', 1, 3,
                        ?, '/state', 1, 4, NULL, NULL, 4, 3, NULL, 0, NULL,
                        'serial', 'STARTING', 0, 0, 0, ?, ?, NULL, NULL, NULL, 1
                    )
                    """,
                    (digest, b"seed", digest),
                )
                connection.execute(
                    """
                    INSERT INTO workflow_operations(
                        operation_id, effect_key, root_key, action, request_digest,
                        expected_workflow_sequence, expected_task_sequence,
                        intent_sequence, next_task_sequence, run_id,
                        main_terminal_id, task_id, dispatch_id, attempt, terminal_id,
                        delivery_id, message_id, consumer_generation, owner,
                        lease_epoch, fencing_token, status, receipt_id, created_ns,
                        updated_ns, intent_digest, receipt_digest, evidence_ref
                    ) VALUES (
                        'op-1', 'effect-1', 'root-1', 'start', ?, 0, NULL, 1,
                        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0,
                        'owner-1', 0, 0, 'INTENT', NULL, 1, 1, ?, NULL, NULL
                    )
                    """,
                    (digest, digest),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO workflow_receipts(
                            receipt_id, operation_id, effect_key,
                            receipt_schema_version, action, request_digest,
                            effect_ref, result_kind,
                            result_digest, evidence_ref, issued_ns, run_id,
                            main_terminal_id, task_id, dispatch_id, attempt,
                            terminal_id, delivery_id, message_id,
                            consumer_generation, owner, lease_epoch, fencing_token
                        ) VALUES (
                            'receipt-1', 'op-1', 'effect-1', 1, 'start', ?, 'ref-1',
                            'started', ?, ?, 2, 'run-1', 'main-1', NULL, NULL,
                            NULL, NULL, NULL, NULL, 0, 'owner-1', 0, 0
                        )
                        """,
                        (digest, digest, digest),
                    )
                connection.execute(
                    """
                    INSERT INTO workflow_events(
                        workflow_event_id, workflow_event_schema_version, root_key,
                        operation_id, workflow_sequence, task_sequence_before,
                        task_sequence_after, from_state, to_state, kind, actor,
                        clock_ns, request_digest, receipt_id, checkpoint_bytes,
                        checkpoint_digest, evidence_ref, event_digest
                    ) VALUES (1, 1, 'root-1', 'op-1', 1, NULL, NULL, NULL,
                               'STARTING', 'start', 'owner-1', 2, ?, NULL, ?, ?, NULL, ?)
                    """,
                    (digest, b"seed", digest, digest),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO workflow_events(
                            workflow_event_schema_version, root_key, operation_id,
                            workflow_sequence, to_state, kind, actor, clock_ns,
                            request_digest, checkpoint_bytes, checkpoint_digest,
                            event_digest
                        ) VALUES (
                            1, 'root-1', NULL, 2, 'STARTING', 'prompt',
                            'owner-1', 2, ?, ?, ?, ?
                        )
                        """,
                        (digest, b"seed", digest, digest),
                    )
                before = connection.execute(
                    "SELECT COUNT(*) FROM workflow_events"
                ).fetchone()[0]
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE workflow_events SET actor = 'owner-2' "
                        "WHERE workflow_event_id = 1"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM workflow_events WHERE workflow_event_id = 1"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT OR REPLACE INTO workflow_events "
                        "(workflow_event_id, workflow_event_schema_version, root_key, "
                        "operation_id, workflow_sequence, to_state, kind, actor, "
                        "clock_ns, request_digest, checkpoint_bytes, checkpoint_digest, "
                        "event_digest) "
                        "VALUES (1, 1, 'root-1', 'op-1', 1, 'STARTING', 'start', "
                        "'owner-1', 2, ?, ?, ?, ?) ",
                        (digest, b"seed", digest, digest),
                    )
                self.assertEqual(
                    before,
                    connection.execute(
                        "SELECT COUNT(*) FROM workflow_events"
                    ).fetchone()[0],
                )


if __name__ == "__main__":
    unittest.main()
