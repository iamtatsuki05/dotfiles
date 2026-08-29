from __future__ import annotations

import fcntl
import multiprocessing
import os
import signal
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from threading import BrokenBarrierError
from typing import cast

from agent_team.store import (
    CoordinationStore,
    DuplicateOperationError,
    OperationSnapshot,
    StoreBusyError,
    StoreClosedError,
    StoreCommitUnknownError,
    StoreError,
    StoreIntegrityError,
    StoreSchemaError,
    StoreUnavailableError,
    TransitionEvent,
)


def _database(state_root: Path) -> Path:
    return state_root / "coordination.sqlite3"


def _row_counts(database: Path) -> tuple[int, int]:
    if not database.exists():
        return 0, 0
    connection = sqlite3.connect(str(database))
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "operations" not in tables or "transition_events" not in tables:
            return 0, 0
        return (
            int(connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]),
            int(
                connection.execute("SELECT COUNT(*) FROM transition_events").fetchone()[
                    0
                ]
            ),
        )
    finally:
        connection.close()


def _make_state_root(temporary: str, name: str = "state") -> Path:
    state_root = Path(os.path.realpath(temporary)) / name
    state_root.mkdir()
    state_root.chmod(0o700)
    return state_root


def _concurrent_writer_worker(
    state_root: str,
    prefix: str,
    barrier: multiprocessing.synchronize.Barrier,
    result_queue: multiprocessing.queues.Queue[tuple[str, str]],
) -> None:
    try:
        with CoordinationStore(Path(state_root)) as store:
            result_queue.put(("ready", prefix))
            for index in range(32):
                barrier.wait(timeout=10)
                store.create_intent(
                    f"{prefix}-{index:02d}",
                    effect_key=f"effect/{prefix}/{index:02d}",
                    actor=prefix,
                    reason_code="intent_created",
                )
                barrier.wait(timeout=10)
        result_queue.put(("ok", prefix))
    except (BrokenBarrierError, OSError, StoreError, ValueError) as error:
        result_queue.put(
            (
                "error",
                (
                    f"{prefix}: {type(error).__name__}: {error!r}; "
                    f"cause={error.__cause__!r}"
                ),
            )
        )


def _same_operation_worker(
    state_root: str,
    owner: str,
    barrier: multiprocessing.synchronize.Barrier,
    result_queue: multiprocessing.queues.Queue[tuple[str, str]],
) -> None:
    try:
        with CoordinationStore(Path(state_root)) as store:
            result_queue.put(("ready", owner))
            barrier.wait(timeout=10)
            store.create_intent(
                "same-operation",
                effect_key="effect/same-operation",
                actor=owner,
                reason_code="intent_created",
            )
        result_queue.put(("created", owner))
    except DuplicateOperationError:
        result_queue.put(("duplicate", owner))
    except (BrokenBarrierError, OSError, StoreError, ValueError) as error:
        result_queue.put(
            (
                "error",
                (
                    f"{owner}: {type(error).__name__}: {error!r}; "
                    f"cause={error.__cause__!r}"
                ),
            )
        )


class _FaultStore(CoordinationStore):
    def __init__(self, state_root: Path, target: str) -> None:
        self._target = target
        super().__init__(state_root)

    def _fault(self, point: str) -> None:
        if point == self._target:
            os.kill(os.getpid(), signal.SIGKILL)


def _kill_at_fault_point(state_root: str, target: str) -> None:
    try:
        with _FaultStore(Path(state_root), target) as store:
            store.create_intent(
                "crash-operation",
                effect_key="effect/crash-operation",
                actor="fault-worker",
                reason_code="intent_created",
            )
    except (OSError, StoreError):
        os._exit(70)


def _startup_lock_worker(
    state_root: str,
    result_queue: multiprocessing.queues.Queue[str],
) -> None:
    try:
        with CoordinationStore(Path(state_root), busy_timeout_ms=20):
            result_queue.put("opened")
    except StoreBusyError:
        result_queue.put("busy")
    except StoreError:
        result_queue.put("error")


def _fifo_open_worker(
    state_root: str,
    result_queue: multiprocessing.queues.Queue[str],
) -> None:
    try:
        with CoordinationStore(Path(state_root)):
            result_queue.put("opened")
    except StoreUnavailableError:
        result_queue.put("unavailable")
    except StoreError:
        result_queue.put("error")


class _ConstructorSwapStore(CoordinationStore):
    def _acquire_startup_lock(self) -> None:
        moved_root = self.state_root.with_name("state-old")
        self.state_root.rename(moved_root)
        self.state_root.mkdir()
        self.state_root.chmod(0o700)
        super()._acquire_startup_lock()


class _IdentitySwapStore(CoordinationStore):
    def __init__(self, state_root: Path, target: str, identity: str) -> None:
        self._target = target
        self._identity = identity
        self._armed = False
        super().__init__(state_root)
        self._armed = True

    def _fault(self, point: str) -> None:
        if not self._armed or point != self._target:
            return
        if self._identity == "root":
            moved_root = self.state_root.with_name("identity-swap-old")
            self.state_root.rename(moved_root)
            self.state_root.mkdir()
            self.state_root.chmod(0o700)
            return
        database = _database(self.state_root)
        moved_database = database.with_name("coordination.sqlite3-old")
        database.rename(moved_database)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{database}{suffix}")
            if sidecar.exists():
                sidecar.rename(Path(f"{moved_database}{suffix}"))
        database.touch()
        database.chmod(0o600)


def _deny_insert(
    action_code: int,
    arg1: str | None,
    arg2: str | None,
    database_name: str | None,
    trigger_name: str | None,
) -> int:
    del arg1, arg2, database_name, trigger_name
    if action_code == sqlite3.SQLITE_INSERT:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class CoordinationStoreTest(unittest.TestCase):
    def test_intent_and_first_event_commit_as_one_transaction(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                operation = store.create_intent(
                    "op-1",
                    effect_key="effect/op-1",
                    actor="main",
                    reason_code="intent_created",
                    evidence_ref="sha256:" + "a" * 64,
                    clock_ns=101,
                )
                events = store.events("op-1")

            self.assertEqual(
                (
                    "op-1",
                    "effect/op-1",
                    "INTENT",
                    0,
                    0,
                    101,
                    101,
                ),
                (
                    operation.operation_id,
                    operation.effect_key,
                    operation.status,
                    operation.attempt,
                    operation.recovery_epoch,
                    operation.created_ns,
                    operation.updated_ns,
                ),
            )
            self.assertEqual(1, len(events))
            self.assertEqual(
                (
                    1,
                    "op-1",
                    1,
                    0,
                    None,
                    "INTENT",
                    "intent",
                    "main",
                    101,
                    "intent_created",
                    "sha256:" + "a" * 64,
                ),
                (
                    events[0].sequence,
                    events[0].operation_id,
                    events[0].event_schema_version,
                    events[0].attempt,
                    events[0].from_status,
                    events[0].to_status,
                    events[0].kind,
                    events[0].actor,
                    events[0].clock_ns,
                    events[0].reason_code,
                    events[0].evidence_ref,
                ),
            )

    def test_reason_code_and_opaque_refs_reject_secret_like_or_free_form_values(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            secret = "OPENAI_API_KEY=do-not-store"
            with CoordinationStore(state_root) as store:
                with self.assertRaises(ValueError) as reason_error:
                    store.create_intent(
                        "op-1",
                        effect_key="effect/op-1",
                        actor="main",
                        reason_code="prompt text",
                    )
                with self.assertRaises(ValueError) as identifier_error:
                    store.create_intent(
                        secret,
                        effect_key="effect/op-1",
                        actor="main",
                    )
                with self.assertRaises(ValueError) as evidence_error:
                    store.create_intent(
                        "op-2",
                        effect_key="effect/op-2",
                        actor="main",
                        evidence_ref="evidence/provider-token-value",
                    )
                self.assertNotIn(secret, str(identifier_error.exception))
                self.assertNotIn("prompt text", str(reason_error.exception))
                self.assertNotIn("provider-token-value", str(evidence_error.exception))

    def test_bad_input_and_integer_overflow_use_stable_errors_without_echo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                with self.assertRaises(ValueError) as type_error:
                    store.create_intent(
                        cast(str, None),
                        effect_key="effect/op-1",
                        actor="main",
                    )
                with self.assertRaises(ValueError) as overflow_error:
                    store.create_intent(
                        "op-1",
                        effect_key="effect/op-1",
                        actor="main",
                        clock_ns=2**63,
                    )
                self.assertNotIn("None", str(type_error.exception))
                self.assertNotIn("9223372036854775808", str(overflow_error.exception))
                self.assertIsNone(store.operation("op-1"))

    def test_read_failures_are_normalized_to_store_errors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                store.create_intent(
                    "bad-row", effect_key="effect/bad-row", actor="main"
                )
                raw = sqlite3.connect(str(_database(state_root)), isolation_level=None)
                try:
                    raw.execute("PRAGMA ignore_check_constraints = ON")
                    raw.execute(
                        "UPDATE operations SET current_attempt = 'not-int' "
                        "WHERE operation_id = 'bad-row'"
                    )
                    with self.assertRaises(StoreIntegrityError):
                        store.operation("bad-row")
                    raw.execute("PRAGMA foreign_keys = OFF")
                    raw.execute("DROP TABLE operations")
                finally:
                    raw.close()
                with self.assertRaises(StoreUnavailableError):
                    store.operation("missing")

    def test_write_database_errors_are_normalized_and_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                connection = store._connection
                assert connection is not None
                connection.set_authorizer(_deny_insert)
                with self.assertRaises(StoreUnavailableError):
                    store.create_intent(
                        "denied",
                        effect_key="effect/denied",
                        actor="main",
                    )
                connection.set_authorizer(None)
                self.assertIsNone(store.operation("denied"))
                self.assertEqual((), store.events())

    def test_public_snapshots_and_events_validate_observation_values(self) -> None:
        valid_operation = OperationSnapshot(
            operation_id="op-1",
            effect_key="effect/op-1",
            status="INTENT",
            attempt=0,
            recovery_epoch=0,
            created_ns=1,
            updated_ns=1,
        )
        self.assertEqual("INTENT", valid_operation.status)
        with self.assertRaises(ValueError):
            OperationSnapshot(
                operation_id="op-1",
                effect_key="effect/op-1",
                status="COMPLETED",
                attempt=-1,
                recovery_epoch=0,
                created_ns=1,
                updated_ns=1,
            )
        with self.assertRaises(ValueError):
            TransitionEvent(
                sequence=1,
                event_schema_version=1,
                operation_id="op-1",
                attempt=0,
                from_status=None,
                to_status="NOT_A_STATUS",
                kind="intent",
                actor="main",
                clock_ns=1,
                reason_code="intent_created",
                evidence_ref=None,
            )
        with self.assertRaises(ValueError):
            TransitionEvent(
                sequence=2**63,
                event_schema_version=1,
                operation_id="op-1",
                attempt=0,
                from_status=None,
                to_status="INTENT",
                kind="intent",
                actor="main",
                clock_ns=1,
                reason_code="intent_created",
                evidence_ref=None,
            )

    def test_duplicate_operation_and_effect_key_fail_without_second_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                store.create_intent("op-1", effect_key="effect/op-1", actor="main")
                with self.assertRaises(DuplicateOperationError):
                    store.create_intent(
                        "op-1",
                        effect_key="effect/op-2",
                        actor="main",
                    )
                with self.assertRaises(DuplicateOperationError):
                    store.create_intent(
                        "op-2",
                        effect_key="effect/op-1",
                        actor="main",
                    )
                self.assertEqual(1, len(store.events()))

    def test_fk_unique_constraints_and_append_only_journal_are_database_guards(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                store.create_intent("op-1", effect_key="effect/op-1", actor="main")
                self.assertNotIn("connection", dir(store))

            connection = sqlite3.connect(
                str(_database(state_root)), isolation_level=None
            )
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO effect_receipts(
                            operation_id, attempt, effect_key, provider_effect_id,
                            provider_status, owner, fencing_token, lease_epoch,
                            received_ns
                        ) VALUES ('missing', 0, 'effect/missing', 'provider/missing',
                                  'COMPLETED', 'owner', 1, 0, 1)
                        """
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO transition_events(
                            event_id, event_schema_version, operation_id, attempt,
                            from_status, to_status, kind, actor, clock_ns,
                            reason_code, evidence_ref
                        ) VALUES (99, 1, 'missing', 0, NULL, 'INTENT', 'intent',
                                  'actor', 1, 'intent_created', NULL)
                        """
                    )

                connection.execute(
                    """
                    INSERT INTO effect_receipts(
                        operation_id, attempt, effect_key, provider_effect_id,
                        provider_status, owner, fencing_token, lease_epoch,
                        received_ns
                    ) VALUES ('op-1', 0, 'effect/op-1', 'provider/op-1',
                              'COMPLETED', 'owner', 1, 0, 1)
                    """
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO effect_receipts(
                            operation_id, attempt, effect_key, provider_effect_id,
                            provider_status, owner, fencing_token, lease_epoch,
                            received_ns
                        ) VALUES ('op-1', 0, 'effect/op-1', 'provider/op-1',
                                  'COMPLETED', 'owner', 1, 0, 2)
                        """
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE transition_events SET reason_code = 'changed' WHERE event_id = 1"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM transition_events WHERE event_id = 1"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO transition_events(
                            event_id, event_schema_version, operation_id, attempt,
                            from_status, to_status, kind, actor, clock_ns,
                            reason_code, evidence_ref
                        ) VALUES (1, 1, 'op-1', 0, NULL, 'INTENT', 'duplicate',
                                  'actor', 2, 'intent_created', NULL)
                        """
                    )
            finally:
                connection.close()

    def test_effective_pragmas_and_private_file_modes_are_verified_on_open(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root, busy_timeout_ms=137) as store:
                connection = store._connection
                assert connection is not None
                store.create_intent("op-1", effect_key="effect/op-1", actor="main")
                self.assertEqual(
                    1, connection.execute("PRAGMA foreign_keys").fetchone()[0]
                )
                self.assertEqual(
                    "wal", connection.execute("PRAGMA journal_mode").fetchone()[0]
                )
                self.assertEqual(
                    2, connection.execute("PRAGMA synchronous").fetchone()[0]
                )
                self.assertEqual(
                    137, connection.execute("PRAGMA busy_timeout").fetchone()[0]
                )

                database_stat = _database(state_root).stat()
                self.assertEqual(0o600, stat.S_IMODE(database_stat.st_mode))
                self.assertEqual(os.getuid(), database_stat.st_uid)
                self.assertEqual(1, database_stat.st_nlink)
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{_database(state_root)}{suffix}")
                    if sidecar.exists():
                        sidecar_stat = sidecar.lstat()
                        self.assertEqual(0o600, stat.S_IMODE(sidecar_stat.st_mode))
                        self.assertEqual(os.getuid(), sidecar_stat.st_uid)
                        self.assertEqual(1, sidecar_stat.st_nlink)

    def test_schema_mismatch_state_json_and_bad_file_security_fail_fast(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            root = Path(os.path.realpath(temporary))
            future_root = root / "future-state"
            future_root.mkdir()
            future_root.chmod(0o700)
            future_database = _database(future_root)
            raw = sqlite3.connect(str(future_database))
            try:
                raw.execute(
                    "CREATE TABLE store_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
                )
                raw.execute(
                    "INSERT INTO store_meta(key, value) VALUES ('store_schema', 2)"
                )
                raw.commit()
            finally:
                raw.close()
            future_database.chmod(0o600)
            future_bytes = future_database.read_bytes()
            with self.assertRaises(StoreSchemaError):
                CoordinationStore(future_root)
            self.assertEqual(future_bytes, future_database.read_bytes())

            empty_future_root = root / "empty-future-state"
            empty_future_root.mkdir()
            empty_future_root.chmod(0o700)
            empty_future_database = _database(empty_future_root)
            raw = sqlite3.connect(str(empty_future_database))
            try:
                raw.execute("PRAGMA user_version = 2")
                raw.commit()
            finally:
                raw.close()
            empty_future_database.chmod(0o600)
            with self.assertRaises(StoreSchemaError):
                CoordinationStore(empty_future_root)

            missing_column_root = root / "missing-column-state"
            missing_column_root.mkdir()
            missing_column_root.chmod(0o700)
            with CoordinationStore(missing_column_root):
                pass
            raw = sqlite3.connect(
                str(_database(missing_column_root)), isolation_level=None
            )
            try:
                raw.execute("ALTER TABLE operations DROP COLUMN updated_ns")
            finally:
                raw.close()
            with self.assertRaises(StoreSchemaError):
                CoordinationStore(missing_column_root)

            state_json = root / "state.json"
            original = b'{"version":3,"state":"untouched"}'
            state_json.write_bytes(original)
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(state_json)
            self.assertEqual(original, state_json.read_bytes())

            empty_state_json = root / "empty-state.json"
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(empty_state_json)
            self.assertFalse(empty_state_json.exists())

            zero_state_json = root / "zero-state.json"
            zero_state_json.write_bytes(b"")
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(zero_state_json)
            self.assertEqual(b"", zero_state_json.read_bytes())

            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(root / "missing-parent")

    def test_schema_validator_rejects_extra_objects_and_trigger_body_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            root = Path(os.path.realpath(temporary))
            for variant in ("table", "view", "index", "trigger", "literal"):
                state_root = root / variant
                state_root.mkdir()
                state_root.chmod(0o700)
                with CoordinationStore(state_root):
                    pass
                connection = sqlite3.connect(str(_database(state_root)))
                try:
                    if variant == "table":
                        connection.execute("CREATE TABLE unexpected_table(value TEXT)")
                    elif variant == "view":
                        connection.execute("CREATE VIEW unexpected_view AS SELECT 1")
                    elif variant == "index":
                        connection.execute(
                            "CREATE INDEX unexpected_index ON operations(status)"
                        )
                    elif variant == "trigger":
                        connection.execute("DROP TRIGGER transition_events_no_update")
                        connection.execute(
                            """
                            CREATE TRIGGER transition_events_no_update
                            BEFORE UPDATE ON transition_events
                            BEGIN
                                SELECT NULL;
                            END
                            """
                        )
                    else:
                        connection.execute("PRAGMA writable_schema = ON")
                        connection.execute(
                            """
                            UPDATE sqlite_master
                            SET sql = replace(sql, ?, ?)
                            WHERE type = 'table' AND name = 'operations'
                            """,
                            ("'INTENT'", "'intent'"),
                        )
                        connection.execute("PRAGMA writable_schema = OFF")
                        schema_version = connection.execute(
                            "PRAGMA schema_version"
                        ).fetchone()[0]
                        connection.execute(
                            f"PRAGMA schema_version = {schema_version + 1}"
                        )
                    connection.commit()
                finally:
                    connection.close()
                with self.assertRaises(StoreSchemaError):
                    CoordinationStore(state_root)

    def test_foreign_key_check_rejects_orphaned_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                store.create_intent("op-1", effect_key="effect/op-1", actor="main")
            connection = sqlite3.connect(str(_database(state_root)))
            try:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("DELETE FROM operations WHERE operation_id = 'op-1'")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(state_root)

    def test_state_root_and_database_ownership_modes_links_and_ancestors_are_checked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            root = Path(os.path.realpath(temporary))
            wide_root = root / "wide-state"
            wide_root.mkdir()
            wide_root.chmod(0o750)
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(wide_root)

            wide_database_root = _make_state_root(temporary, "wide-database-state")
            with CoordinationStore(wide_database_root):
                pass
            _database(wide_database_root).chmod(0o640)
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(wide_database_root)

            hardlink_root = _make_state_root(temporary, "hardlink-state")
            with CoordinationStore(hardlink_root):
                pass
            hardlink = root / "database-hardlink"
            os.link(_database(hardlink_root), hardlink)
            try:
                with self.assertRaises(StoreUnavailableError):
                    CoordinationStore(hardlink_root)
            finally:
                hardlink.unlink()

            sidecar_root = _make_state_root(temporary, "sidecar-state")
            with CoordinationStore(sidecar_root):
                pass
            sidecar = Path(f"{_database(sidecar_root)}-wal")
            sidecar.write_bytes(b"stale")
            sidecar.chmod(0o644)
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(sidecar_root)
            sidecar.unlink()

            target = root / "real-parent"
            target.mkdir()
            target.chmod(0o700)
            intermediate = root / "symlink-parent"
            intermediate.symlink_to(target, target_is_directory=True)
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(intermediate / "state")

            symlink_root_target = root / "real-state"
            symlink_root_target.mkdir()
            symlink_root_target.chmod(0o700)
            symlink_root = root / "symlink-state"
            symlink_root.symlink_to(symlink_root_target, target_is_directory=True)
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(symlink_root)

            wide_parent = root / "wide-parent"
            wide_parent.mkdir()
            wide_parent.chmod(0o777)
            wide_child = wide_parent / "state"
            wide_child.mkdir()
            wide_child.chmod(0o700)
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(wide_child)

            constructor_swap_root = _make_state_root(temporary, "constructor-swap")
            with self.assertRaises(StoreUnavailableError):
                _ConstructorSwapStore(constructor_swap_root)
            self.assertFalse(_database(constructor_swap_root).exists())

            after_open_root = _make_state_root(temporary, "after-open-swap")
            store = CoordinationStore(after_open_root)
            moved_root = after_open_root.with_name("after-open-old")
            after_open_root.rename(moved_root)
            after_open_root.mkdir()
            after_open_root.chmod(0o700)
            try:
                with self.assertRaises(StoreUnavailableError):
                    store.create_intent(
                        "split-operation",
                        effect_key="effect/split-operation",
                        actor="main",
                    )
                self.assertFalse(_database(after_open_root).exists())
            finally:
                store.close()

            lock_root = _make_state_root(temporary, "startup-lock")
            with CoordinationStore(lock_root):
                pass
            root_fd = os.open(lock_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(root_fd, fcntl.LOCK_EX)
                context = multiprocessing.get_context("spawn")
                result_queue = context.Queue()
                process = context.Process(
                    target=_startup_lock_worker,
                    args=(str(lock_root), result_queue),
                )
                process.start()
                self.assertEqual("busy", result_queue.get(timeout=5))
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail("startup lock contender did not exit")
            finally:
                fcntl.flock(root_fd, fcntl.LOCK_UN)
                os.close(root_fd)

            fifo_root = _make_state_root(temporary, "fifo-sidecar")
            with CoordinationStore(fifo_root):
                pass
            fifo_sidecar = Path(f"{_database(fifo_root)}-wal")
            os.mkfifo(fifo_sidecar, 0o600)
            try:
                context = multiprocessing.get_context("spawn")
                result_queue = context.Queue()
                process = context.Process(
                    target=_fifo_open_worker,
                    args=(str(fifo_root), result_queue),
                )
                process.start()
                self.assertEqual("unavailable", result_queue.get(timeout=5))
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail("FIFO sidecar probe did not exit")
            finally:
                fifo_sidecar.unlink()

    def test_identity_swap_after_begin_or_before_commit_rolls_back_everywhere(
        self,
    ) -> None:
        for point in ("after_begin", "before_commit"):
            for identity in ("root", "database"):
                with (
                    self.subTest(point=point, identity=identity),
                    tempfile.TemporaryDirectory(
                        prefix="agent-team-store-swap-"
                    ) as temporary,
                ):
                    state_root = _make_state_root(temporary)
                    with (
                        self.assertRaises(StoreUnavailableError),
                        _IdentitySwapStore(state_root, point, identity) as store,
                    ):
                        store.create_intent(
                            "swap-operation",
                            effect_key="effect/swap-operation",
                            actor="main",
                        )
                    if identity == "root":
                        old_root = state_root.with_name("identity-swap-old")
                        self.assertEqual((0, 0), _row_counts(_database(old_root)))
                        self.assertFalse(_database(state_root).exists())
                    else:
                        old_database = state_root / "coordination.sqlite3-old"
                        self.assertEqual((0, 0), _row_counts(old_database))
                        self.assertEqual((0, 0), _row_counts(_database(state_root)))

    def test_identity_swap_after_commit_never_returns_success(self) -> None:
        for identity in ("root", "database"):
            with (
                self.subTest(identity=identity),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-store-swap-after-"
                ) as temporary,
            ):
                state_root = _make_state_root(temporary)
                with (
                    self.assertRaises(StoreCommitUnknownError),
                    _IdentitySwapStore(state_root, "after_commit", identity) as store,
                ):
                    store.create_intent(
                        "swap-operation",
                        effect_key="effect/swap-operation",
                        actor="main",
                    )
                if identity == "root":
                    old_root = state_root.with_name("identity-swap-old")
                    self.assertEqual((1, 1), _row_counts(_database(old_root)))
                    self.assertFalse(_database(state_root).exists())
                else:
                    old_database = state_root / "coordination.sqlite3-old"
                    self.assertEqual((1, 1), _row_counts(old_database))
                    self.assertEqual((0, 0), _row_counts(_database(state_root)))

    def test_fault_points_leave_no_half_committed_intent_or_event(self) -> None:
        fault_points = (
            "before_begin",
            "after_begin",
            "before_intent_insert",
            "before_attempt_insert",
            "before_event_insert",
            "before_commit",
            "after_commit",
        )
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            context = multiprocessing.get_context("spawn")
            for point in fault_points:
                process = context.Process(
                    target=_kill_at_fault_point,
                    args=(str(state_root), point),
                )
                process.start()
                process.join(timeout=15)
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail(f"fault worker did not exit at {point}")
                self.assertEqual(-signal.SIGKILL, process.exitcode, point)
                with CoordinationStore(state_root) as store:
                    if point == "after_commit":
                        self.assertIsNotNone(store.operation("crash-operation"))
                        self.assertEqual(1, len(store.events()))
                    else:
                        self.assertIsNone(store.operation("crash-operation"))
                        self.assertEqual((), store.events())

    def test_two_process_writers_serialize_without_lost_updates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_concurrent_writer_worker,
                    args=(str(state_root), prefix, barrier, result_queue),
                )
                for prefix in ("writer-a", "writer-b")
            ]
            for process in processes:
                process.start()
            readiness = [result_queue.get(timeout=15) for _ in processes]
            self.assertCountEqual(
                [("ready", "writer-a"), ("ready", "writer-b")],
                readiness,
            )
            for process in processes:
                process.join(timeout=30)
            for process in processes:
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail("concurrent writer did not exit")
            results = [result_queue.get(timeout=15) for _ in processes]
            self.assertCountEqual(
                [("ok", "writer-a"), ("ok", "writer-b")],
                results,
                results,
            )
            with CoordinationStore(state_root) as store:
                operations = [
                    store.operation(f"{prefix}-{index:02d}")
                    for prefix in ("writer-a", "writer-b")
                    for index in range(32)
                ]
                self.assertTrue(all(operation is not None for operation in operations))
                events = store.events()
                self.assertEqual(64, len(events))
                self.assertEqual(
                    list(range(1, 65)), [event.sequence for event in events]
                )

    def test_same_operation_concurrent_intent_has_one_winner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_same_operation_worker,
                    args=(str(state_root), owner, barrier, result_queue),
                )
                for owner in ("owner-a", "owner-b")
            ]
            for process in processes:
                process.start()
            readiness = [result_queue.get(timeout=15) for _ in processes]
            self.assertCountEqual(
                [("ready", "owner-a"), ("ready", "owner-b")],
                readiness,
            )
            for process in processes:
                process.join(timeout=30)
            for process in processes:
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail("same-operation writer did not exit")
            results = [result_queue.get(timeout=15) for _ in processes]
            self.assertEqual(1, sum(result[0] == "created" for result in results))
            self.assertEqual(
                1,
                sum(result[0] == "duplicate" for result in results),
                results,
            )
            self.assertFalse(any(result[0] == "error" for result in results))
            with CoordinationStore(state_root) as store:
                self.assertIsNotNone(store.operation("same-operation"))
                self.assertEqual(1, len(store.events()))

    def test_lock_holder_returns_explicit_busy_error_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root, busy_timeout_ms=100) as store:
                holder = sqlite3.connect(
                    str(_database(state_root)), isolation_level=None
                )
                try:
                    holder.execute("BEGIN IMMEDIATE")
                    with self.assertRaises(StoreBusyError):
                        store.create_intent(
                            "contender",
                            effect_key="effect/contender",
                            actor="contender",
                        )
                finally:
                    holder.rollback()
                    holder.close()
                self.assertIsNone(store.operation("contender"))
                self.assertEqual((), store.events())
            self.assertEqual(
                ["coordination.sqlite3"],
                sorted(path.name for path in state_root.iterdir()),
            )
            with self.assertRaises(StoreClosedError):
                store.operation("contender")


if __name__ == "__main__":
    unittest.main()
