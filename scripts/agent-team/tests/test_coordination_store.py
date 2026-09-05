from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import signal
import sqlite3
import stat
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from threading import BrokenBarrierError
from typing import cast
from unittest import mock

from agent_team import recovery
from agent_team.lease import RestoreIdentity
from agent_team.store import (
    _MAX_ORPHAN_FDS,
    EVENT_SCHEMA_VERSION,
    LIFETIME_GATE_FILENAME,
    WRITER_MARKER_CLEAN_CONTENT,
    CoordinationStore,
    DuplicateOperationError,
    OperationSnapshot,
    RestoreStoreAuthority,
    StoreBusyError,
    StoreClosedError,
    StoreCommitUnknownError,
    StoreError,
    StoreIntegrityError,
    StoreSchemaError,
    StoreUnavailableError,
    TransitionEvent,
    _CleanupCapability,
    _open_state_root,
    _restore_history_binding_ref,
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


def _write_recovery_history(
    root: Path,
    ledger_records: tuple[recovery.RecoveryLedgerRecord, ...],
    tombstone_records: tuple[recovery.RecoveryTombstoneRecord, ...],
) -> None:
    ledger_path = root / recovery.RECOVERY_LEDGER_BASENAME
    tombstone_path = root / recovery.RECOVERY_TOMBSTONES_BASENAME
    ledger_path.write_bytes(
        b"".join(recovery._encode_record(record) for record in ledger_records)
    )
    tombstone_path.write_bytes(
        b"".join(recovery._encode_tombstone(record) for record in tombstone_records)
    )
    ledger_path.chmod(0o600)
    tombstone_path.chmod(0o600)


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


class _ReleaseStartupFailureStore(CoordinationStore):
    def _release_startup_lock(self) -> None:
        raise StoreUnavailableError("injected startup-lock release failure")


class _ReleaseLifetimeFailureStore(CoordinationStore):
    def _release_lifetime_gate(self) -> None:
        raise StoreUnavailableError("injected lifetime-gate release failure")


class _ForeignDatabaseStore(CoordinationStore):
    def _open_database_file(self, *, create: bool) -> int:
        if create:
            database = self.state_root / "coordination.sqlite3"
            database.write_bytes(b"foreign-database")
            database.chmod(0o600)
        return super()._open_database_file(create=create)


class _ForeignMarkerStore(CoordinationStore):
    def _open_writer_marker(self) -> None:
        marker = self.state_root / "writer.marker"
        marker.write_bytes(b"foreign-marker")
        marker.chmod(0o600)
        super()._open_writer_marker()


class _ForeignSidecarStore(CoordinationStore):
    def _existing_sidecar_names(self) -> frozenset[str]:
        sidecar = self.state_root / "coordination.sqlite3-wal"
        sidecar.write_bytes(b"foreign-sidecar")
        sidecar.chmod(0o600)
        raise StoreUnavailableError("injected sidecar race")


class _ForeignGateStore(CoordinationStore):
    def _open_lifetime_gate(self, *, create: bool) -> int:
        gate = self.state_root.parent / LIFETIME_GATE_FILENAME
        gate.write_bytes(b"foreign-gate")
        gate.chmod(0o600)
        gate_fd = super()._open_lifetime_gate(create=create)
        os.close(gate_fd)
        raise StoreUnavailableError("injected gate race")


class _FreshSidecarFailureStore(CoordinationStore):
    def _fault(self, point: str) -> None:
        if point == "after_marker_lock":
            raise StoreUnavailableError("injected post-marker sidecar failure")


class _BodyFailureStore(CoordinationStore):
    def _preflight_schema(self) -> None:
        raise StoreSchemaError("injected constructor body failure")


class _BeforeCommitFailureStore(CoordinationStore):
    def _fault(self, point: str) -> None:
        if point == "before_commit":
            raise RuntimeError("transaction body is primary")


class _CommitResponseLostConnection(sqlite3.Connection):
    def commit(self) -> None:
        super().commit()
        raise OSError("commit response was lost")


class _RollbackFailureConnection(sqlite3.Connection):
    def rollback(self) -> None:
        raise OSError("rollback failed")


class _CloseFailureConnection(sqlite3.Connection):
    fail_close = True
    close_calls = 0

    def close(self) -> None:
        type(self).close_calls += 1
        if type(self).fail_close:
            raise OSError("temporary connection close failed")
        super().close()


class _FreshCleanupFailureStore(CoordinationStore):
    def _preflight_schema(self) -> None:
        raise StoreSchemaError("injected fresh cleanup body failure")


class _FreshCleanupReplacementStore(CoordinationStore):
    def _preflight_schema(self) -> None:
        database = self.state_root / "coordination.sqlite3"
        database.unlink()
        database.write_bytes(b"foreign-database")
        database.chmod(0o600)
        raise StoreSchemaError("injected fresh cleanup replacement")


class _AttrlessBodyError(BaseException):
    __slots__ = ()

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("exception does not allow attributes")


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


def _fresh_open_worker(
    state_root: str,
    barrier: multiprocessing.synchronize.Barrier,
    result_queue: multiprocessing.queues.Queue[str],
) -> None:
    try:
        barrier.wait(timeout=10)
        with CoordinationStore(Path(state_root), busy_timeout_ms=300):
            result_queue.put("opened")
    except (BrokenBarrierError, OSError, StoreError, ValueError) as error:
        result_queue.put(f"{type(error).__name__}: {error}; cause={error.__cause__!r}")


def _hold_lifetime_gate(
    state_root: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    gate = Path(state_root).parent / LIFETIME_GATE_FILENAME
    gate_fd = os.open(str(gate), os.O_RDWR)
    try:
        fcntl.flock(gate_fd, fcntl.LOCK_SH)
        ready.set()
        release.wait(timeout=10)
    finally:
        fcntl.flock(gate_fd, fcntl.LOCK_UN)
        os.close(gate_fd)


def _hold_lifetime_gate_exclusive(
    state_root: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    gate = Path(state_root).parent / LIFETIME_GATE_FILENAME
    gate_fd = os.open(str(gate), os.O_RDWR)
    try:
        fcntl.flock(gate_fd, fcntl.LOCK_EX)
        ready.set()
        release.wait(timeout=10)
    finally:
        fcntl.flock(gate_fd, fcntl.LOCK_UN)
        os.close(gate_fd)


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
    def test_state_root_traversal_close_uncertainty_retries_without_foreign_close(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            root = Path(os.path.realpath(temporary)) / "nested" / "state"
            root.mkdir(parents=True)
            root.chmod(0o700)
            original_fstat = os.fstat
            original_close = os.close
            fault_enabled = True
            target_fd: int | None = None

            def fail_one_traversal_close(fd: int) -> None:
                nonlocal target_fd
                try:
                    metadata = original_fstat(fd)
                except OSError:
                    metadata = None
                if (
                    target_fd is None
                    and metadata is not None
                    and stat.S_ISDIR(metadata.st_mode)
                ):
                    target_fd = fd
                if fault_enabled and fd == target_fd:
                    raise OSError("injected traversal close failure")
                original_close(fd)

            with (
                mock.patch(
                    "agent_team.store.os.close",
                    side_effect=fail_one_traversal_close,
                ),
                self.assertRaises(StoreUnavailableError) as raised,
            ):
                _open_state_root(root)
            self.assertIsNotNone(target_fd)
            assert target_fd is not None
            original_fstat(target_fd)
            replacement = root / "replacement"
            replacement.write_bytes(b"replacement")
            replacement.chmod(0o600)
            original_close(target_fd)
            fillers: list[int] = []
            replacement_fd: int | None = None
            try:
                while replacement_fd is None:
                    candidate_fd = os.open(replacement, os.O_RDONLY)
                    if candidate_fd == target_fd:
                        replacement_fd = candidate_fd
                    else:
                        fillers.append(candidate_fd)
                self.assertEqual(target_fd, replacement_fd)
                with self.assertRaises(StoreUnavailableError):
                    raised.exception.retry_cleanup()
                original_fstat(replacement_fd)
                raised.exception.retry_cleanup()
                original_fstat(replacement_fd)
            finally:
                if replacement_fd is not None:
                    original_close(replacement_fd)
                for filler_fd in fillers:
                    original_close(filler_fd)

    def test_state_root_traversal_generic_fstat_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            root = Path(os.path.realpath(temporary)) / "nested" / "state"
            root.mkdir(parents=True)
            root.chmod(0o700)
            original_fstat = os.fstat
            fault_enabled = True
            fstat_calls = 0
            target_fd: int | None = None

            def fail_generic_fstat(fd: int) -> os.stat_result:
                nonlocal fstat_calls, target_fd
                fstat_calls += 1
                if fstat_calls == 2:
                    target_fd = fd
                if fault_enabled and fd == target_fd:
                    raise _AttrlessBodyError("injected traversal fstat failure")
                return original_fstat(fd)

            with (
                mock.patch("agent_team.store.os.fstat", side_effect=fail_generic_fstat),
                self.assertRaises(StoreUnavailableError) as raised,
            ):
                _open_state_root(root)
            self.assertIsNotNone(target_fd)
            assert target_fd is not None
            with (
                mock.patch("agent_team.store.os.fstat", side_effect=fail_generic_fstat),
                self.assertRaises(StoreUnavailableError),
            ):
                raised.exception.retry_cleanup()
            fault_enabled = False
            raised.exception.retry_cleanup()
            with self.assertRaises(OSError):
                original_fstat(target_fd)

    def test_initial_state_root_fstat_failure_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            root = Path(os.path.realpath(temporary)) / "nested" / "state"
            root.mkdir(parents=True)
            root.chmod(0o700)
            original_open = os.open
            original_fstat = os.fstat
            root_fd: int | None = None
            fault_enabled = True

            def open_root(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal root_fd
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
                if path == os.sep and dir_fd is None:
                    root_fd = fd
                return fd

            def fail_root_fstat(fd: int) -> os.stat_result:
                if fault_enabled and fd == root_fd:
                    raise _AttrlessBodyError("injected initial root fstat failure")
                return original_fstat(fd)

            with (
                mock.patch("agent_team.store.os.open", side_effect=open_root),
                mock.patch("agent_team.store.os.fstat", side_effect=fail_root_fstat),
                self.assertRaises(StoreUnavailableError) as raised,
            ):
                _open_state_root(root)
            self.assertIsNotNone(root_fd)
            assert root_fd is not None
            fault_enabled = False
            raised.exception.retry_cleanup()
            with self.assertRaises(OSError):
                original_fstat(root_fd)

    def test_constructor_initial_root_fstat_failure_retains_unbound_fd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            original_open_state_root = _open_state_root
            original_fstat = os.fstat
            root_fd: int | None = None
            fail_enabled = True

            def open_root(path: Path) -> int:
                nonlocal root_fd
                root_fd = original_open_state_root(path)
                return root_fd

            def fail_root_fstat(fd: int) -> os.stat_result:
                if fail_enabled and root_fd == fd:
                    raise _AttrlessBodyError("injected constructor root fstat failure")
                return original_fstat(fd)

            with (
                mock.patch("agent_team.store._open_state_root", side_effect=open_root),
                mock.patch("agent_team.store.os.fstat", side_effect=fail_root_fstat),
                self.assertRaises(StoreUnavailableError) as raised,
            ):
                CoordinationStore(state_root)
            self.assertIsNotNone(root_fd)
            assert root_fd is not None
            fail_enabled = False
            raised.exception.retry_cleanup()
            with self.assertRaises(OSError):
                original_fstat(root_fd)

    def test_attrless_traversal_body_is_wrapped_with_retry_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            root = Path(os.path.realpath(temporary)) / "nested" / "state"
            root.mkdir(parents=True)
            root.chmod(0o700)
            original_close = os.close
            target_fd: int | None = None
            fault_enabled = True

            def fail_traversal_close(fd: int) -> None:
                nonlocal target_fd
                if target_fd is None:
                    target_fd = fd
                if fault_enabled and fd == target_fd:
                    raise OSError("injected traversal cleanup failure")
                original_close(fd)

            def fail_validation(fd: int, *, state_root: bool) -> None:
                del fd, state_root
                raise _AttrlessBodyError("attrless traversal body")

            with (
                mock.patch(
                    "agent_team.store._validate_directory_fd",
                    side_effect=fail_validation,
                ),
                mock.patch(
                    "agent_team.store.os.close",
                    side_effect=fail_traversal_close,
                ),
                self.assertRaises(StoreUnavailableError) as raised,
            ):
                _open_state_root(root)
            self.assertIsInstance(raised.exception.__cause__, _AttrlessBodyError)
            fault_enabled = False
            raised.exception.retry_cleanup()

    def test_constructor_fd_handoff_uses_error_owner_when_registry_is_full(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            store = CoordinationStore(state_root)
            original_close = os.close
            target_fd = os.open(state_root / "writer.marker", os.O_RDONLY)
            target_identity = (
                os.fstat(target_fd).st_dev,
                os.fstat(target_fd).st_ino,
            )
            retained: list[int] = []
            try:
                for index in range(_MAX_ORPHAN_FDS):
                    path = state_root / f"registry-{index}"
                    path.write_bytes(b"registry")
                    path.chmod(0o600)
                    fd = os.open(path, os.O_RDONLY)
                    metadata = os.fstat(fd)
                    retained.append(fd)
                    store._orphan_fds.append(
                        (fd, (metadata.st_dev, metadata.st_ino), f"registry-{index}")
                    )

                def fail_target_close(fd: int) -> None:
                    if fd == target_fd:
                        raise OSError("injected full-registry close failure")
                    original_close(fd)

                with mock.patch(
                    "agent_team.store.os.close",
                    side_effect=fail_target_close,
                ):
                    error = store._handoff_constructor_fd(
                        target_fd,
                        target_identity,
                        "full-registry current",
                    )
                    self.assertIsInstance(error, StoreError)
                    assert error is not None
                    error = cast(StoreError, error)
                    with self.assertRaises(StoreUnavailableError):
                        error.retry_cleanup()
                error.retry_cleanup()
                with self.assertRaises(OSError):
                    os.fstat(target_fd)
                self.assertEqual(0, len(store._orphan_fds))
            finally:
                try:
                    store.close()
                except StoreError:
                    pass
                for fd in retained:
                    try:
                        original_close(fd)
                    except OSError:
                        pass

    def test_fresh_cleanup_unlink_failure_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            database = _database(state_root)
            gate = state_root.parent / LIFETIME_GATE_FILENAME

            def fail_unlink(*args: object, **kwargs: object) -> None:
                del args, kwargs
                raise OSError("injected persistent fresh unlink failure")

            with (
                mock.patch("agent_team.store.os.unlink", side_effect=fail_unlink),
                self.assertRaises(StoreSchemaError) as raised,
            ):
                _FreshCleanupFailureStore(state_root)
            self.assertTrue(database.exists())
            self.assertTrue(gate.exists())
            error = raised.exception
            self.assertTrue(callable(error.retry_cleanup))

            with (
                mock.patch("agent_team.store.os.unlink", side_effect=fail_unlink),
                self.assertRaises(StoreUnavailableError),
            ):
                error.retry_cleanup()
            self.assertTrue(database.exists())
            self.assertTrue(gate.exists())

            error.retry_cleanup()
            self.assertFalse(database.exists())
            self.assertFalse(gate.exists())
            with CoordinationStore(state_root):
                pass

    def test_fresh_cleanup_fsync_failure_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            database = _database(state_root)
            gate = state_root.parent / LIFETIME_GATE_FILENAME

            def fail_fsync(fd: int) -> None:
                del fd
                raise OSError("injected persistent fresh fsync failure")

            with (
                mock.patch("agent_team.store.os.fsync", side_effect=fail_fsync),
                self.assertRaises(StoreSchemaError) as raised,
            ):
                _FreshCleanupFailureStore(state_root)
            self.assertFalse(database.exists())
            self.assertFalse(gate.exists())
            error = raised.exception
            self.assertTrue(callable(error.retry_cleanup))
            with (
                mock.patch(
                    "agent_team.store.os.fsync",
                    side_effect=fail_fsync,
                ),
                self.assertRaises(StoreUnavailableError),
            ):
                error.retry_cleanup()

            error.retry_cleanup()
            with CoordinationStore(state_root):
                pass

    def test_fresh_gate_parent_fstat_failure_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            gate_parent = state_root.parent
            original_open = os.open
            original_fstat = os.fstat
            parent_fd: int | None = None
            failed = False

            def open_directory(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal parent_fd
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
                raw_path = os.fspath(path)
                if isinstance(raw_path, bytes):
                    raw_path = os.fsdecode(raw_path)
                if dir_fd is None and Path(raw_path) == gate_parent:
                    parent_fd = fd
                return fd

            def fail_parent_fstat(fd: int) -> os.stat_result:
                nonlocal failed
                if parent_fd is not None and fd == parent_fd and not failed:
                    failed = True
                    raise OSError("injected fresh gate parent fstat failure")
                return original_fstat(fd)

            with (
                mock.patch("agent_team.store.os.open", side_effect=open_directory),
                mock.patch("agent_team.store.os.fstat", side_effect=fail_parent_fstat),
                self.assertRaises(StoreSchemaError) as raised,
            ):
                _FreshCleanupFailureStore(state_root)
            self.assertIsNotNone(parent_fd)
            assert parent_fd is not None
            raised.exception.retry_cleanup()
            with self.assertRaises(OSError):
                original_fstat(parent_fd)

    def test_fresh_cleanup_preserves_replaced_database_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            database = _database(state_root)
            with self.assertRaises(StoreSchemaError):
                _FreshCleanupReplacementStore(state_root)
            self.assertEqual(b"foreign-database", database.read_bytes())
            self.assertFalse((state_root.parent / LIFETIME_GATE_FILENAME).exists())

    def test_fresh_gate_post_create_stat_failure_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            original_open = os.open
            original_stat = os.stat
            gate_fd: int | None = None
            failed = False

            def open_gate(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal gate_fd
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
                raw_path = os.fspath(path)
                if isinstance(raw_path, bytes):
                    raw_path = os.fsdecode(raw_path)
                if dir_fd is None and Path(raw_path) == gate and flags & os.O_CREAT:
                    gate_fd = fd
                return fd

            def fail_gate_stat(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *,
                dir_fd: int | None = None,
                follow_symlinks: bool = True,
            ) -> os.stat_result:
                nonlocal failed
                raw_path = os.fspath(path)
                if isinstance(raw_path, bytes):
                    raw_path = os.fsdecode(raw_path)
                if (
                    gate_fd is not None
                    and not failed
                    and dir_fd is None
                    and Path(raw_path) == gate
                ):
                    failed = True
                    raise OSError("injected fresh gate post-create stat failure")
                return original_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            with (
                mock.patch("agent_team.store.os.open", side_effect=open_gate),
                mock.patch("agent_team.store.os.stat", side_effect=fail_gate_stat),
                self.assertRaises(StoreUnavailableError),
            ):
                _FreshCleanupFailureStore(state_root)
            self.assertIsNotNone(gate_fd)
            assert gate_fd is not None
            with self.assertRaises(OSError):
                original_stat(gate_fd)
            self.assertFalse(gate.exists())

    def test_fresh_database_post_create_stat_failure_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            database = _database(state_root)
            original_open = os.open
            original_stat = os.stat
            database_fd: int | None = None
            failed = False

            def open_database(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal database_fd
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
                raw_path = os.fspath(path)
                if dir_fd is not None and raw_path == "coordination.sqlite3":
                    database_fd = fd
                return fd

            def fail_database_stat(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *,
                dir_fd: int | None = None,
                follow_symlinks: bool = True,
            ) -> os.stat_result:
                nonlocal failed
                if (
                    database_fd is not None
                    and not failed
                    and dir_fd is not None
                    and os.fspath(path) == "coordination.sqlite3"
                ):
                    failed = True
                    raise OSError("injected fresh database post-create stat failure")
                return original_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            with (
                mock.patch("agent_team.store.os.open", side_effect=open_database),
                mock.patch(
                    "agent_team.store.os.stat",
                    side_effect=fail_database_stat,
                ),
                self.assertRaises(StoreUnavailableError),
            ):
                CoordinationStore(state_root)
            self.assertIsNotNone(database_fd)
            assert database_fd is not None
            self.assertFalse(database.exists())

    def test_fresh_marker_post_create_stat_failure_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            marker = state_root / "writer.marker"
            original_open = os.open
            original_stat = os.stat
            marker_fd: int | None = None
            failed = False

            def open_marker(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal marker_fd
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
                if dir_fd is not None and os.fspath(path) == "writer.marker":
                    marker_fd = fd
                return fd

            def fail_marker_stat(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *,
                dir_fd: int | None = None,
                follow_symlinks: bool = True,
            ) -> os.stat_result:
                nonlocal failed
                if (
                    marker_fd is not None
                    and not failed
                    and dir_fd is not None
                    and os.fspath(path) == "writer.marker"
                ):
                    failed = True
                    raise OSError("injected fresh marker post-create stat failure")
                return original_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            with (
                mock.patch("agent_team.store.os.open", side_effect=open_marker),
                mock.patch("agent_team.store.os.stat", side_effect=fail_marker_stat),
                self.assertRaises(StoreUnavailableError),
            ):
                CoordinationStore(state_root)
            self.assertIsNotNone(marker_fd)
            assert marker_fd is not None
            with self.assertRaises(OSError):
                original_stat(marker_fd)
            self.assertFalse(marker.exists())

    def test_fresh_database_fstat_failure_retains_fd_until_retry_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            database = _database(state_root)
            original_open = os.open
            original_fstat = os.fstat
            database_fd: int | None = None
            fail_enabled = True

            def open_database(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal database_fd
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
                if dir_fd is not None and os.fspath(path) == "coordination.sqlite3":
                    database_fd = fd
                return fd

            def fail_database_fstat(fd: int) -> os.stat_result:
                if fail_enabled and database_fd == fd:
                    raise _AttrlessBodyError("injected fresh database fstat failure")
                return original_fstat(fd)

            with (
                mock.patch("agent_team.store.os.open", side_effect=open_database),
                mock.patch(
                    "agent_team.store.os.fstat", side_effect=fail_database_fstat
                ),
                self.assertRaises(StoreUnavailableError) as raised,
            ):
                CoordinationStore(state_root)
            self.assertIsNotNone(database_fd)
            assert database_fd is not None
            self.assertTrue(database.exists())
            with (
                mock.patch(
                    "agent_team.store.os.fstat",
                    side_effect=fail_database_fstat,
                ),
                self.assertRaises(StoreUnavailableError),
            ):
                raised.exception.retry_cleanup()
            self.assertTrue(database.exists())
            fail_enabled = False
            raised.exception.retry_cleanup()
            with self.assertRaises(OSError):
                original_fstat(database_fd)
            self.assertFalse(database.exists())

    def test_fresh_gate_fstat_failure_retains_fd_until_retry_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            original_open = os.open
            original_fstat = os.fstat
            gate_fd: int | None = None
            fail_enabled = True

            def open_gate(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal gate_fd
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
                raw_path = os.fspath(path)
                if isinstance(raw_path, bytes):
                    raw_path = os.fsdecode(raw_path)
                if dir_fd is None and Path(raw_path) == gate and flags & os.O_CREAT:
                    gate_fd = fd
                return fd

            def fail_gate_fstat(fd: int) -> os.stat_result:
                if fail_enabled and gate_fd == fd:
                    raise _AttrlessBodyError("injected fresh gate fstat failure")
                return original_fstat(fd)

            with (
                mock.patch("agent_team.store.os.open", side_effect=open_gate),
                mock.patch("agent_team.store.os.fstat", side_effect=fail_gate_fstat),
                self.assertRaises(StoreUnavailableError) as raised,
            ):
                CoordinationStore(state_root)
            self.assertIsNotNone(gate_fd)
            assert gate_fd is not None
            self.assertTrue(gate.exists())
            fail_enabled = False
            raised.exception.retry_cleanup()
            with self.assertRaises(OSError):
                original_fstat(gate_fd)
            self.assertFalse(gate.exists())

    def test_fresh_marker_fstat_failure_retains_fd_until_retry_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            marker = state_root / "writer.marker"
            original_open = os.open
            original_fstat = os.fstat
            marker_fd: int | None = None
            fail_enabled = True

            def open_marker(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal marker_fd
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
                if dir_fd is not None and os.fspath(path) == "writer.marker":
                    marker_fd = fd
                return fd

            def fail_marker_fstat(fd: int) -> os.stat_result:
                if fail_enabled and marker_fd == fd:
                    raise _AttrlessBodyError("injected fresh marker fstat failure")
                return original_fstat(fd)

            with (
                mock.patch("agent_team.store.os.open", side_effect=open_marker),
                mock.patch(
                    "agent_team.store.os.fstat",
                    side_effect=fail_marker_fstat,
                ),
                self.assertRaises(StoreUnavailableError) as raised,
            ):
                CoordinationStore(state_root)
            self.assertIsNotNone(marker_fd)
            assert marker_fd is not None
            self.assertTrue(marker.exists())
            with (
                mock.patch(
                    "agent_team.store.os.fstat",
                    side_effect=fail_marker_fstat,
                ),
                self.assertRaises(StoreUnavailableError),
            ):
                raised.exception.retry_cleanup()
            fail_enabled = False
            raised.exception.retry_cleanup()
            with self.assertRaises(OSError):
                original_fstat(marker_fd)
            self.assertFalse(marker.exists())

    def test_direct_database_open_failure_retains_close_uncertain_fd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            store = CoordinationStore(state_root)
            root_fd = store._state_root_fd
            self.assertIsNotNone(root_fd)
            assert root_fd is not None
            original_open = os.open
            original_pread = os.pread
            opened_fd: int | None = None

            def open_file(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal opened_fd
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
                if path == "coordination.sqlite3" and dir_fd == root_fd:
                    opened_fd = fd
                return fd

            def fail_pread(fd: int, size: int, offset: int) -> bytes:
                if fd == opened_fd:
                    raise OSError("injected database read failure")
                return original_pread(fd, size, offset)

            try:
                with (
                    mock.patch("agent_team.store.os.open", side_effect=open_file),
                    mock.patch("agent_team.store.os.pread", side_effect=fail_pread),
                    self.assertRaises(StoreUnavailableError) as raised,
                ):
                    store._open_database_file(create=False)
                self.assertIsNotNone(opened_fd)
                self.assertEqual(1, len(store._orphan_fds))
                raised.exception.retry_cleanup()
                assert opened_fd is not None
                with self.assertRaises(OSError):
                    os.fstat(opened_fd)
            finally:
                store.close()

    def test_database_open_overflow_keeps_current_fd_in_detached_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            store = CoordinationStore(state_root)
            root_fd = store._state_root_fd
            self.assertIsNotNone(root_fd)
            assert root_fd is not None
            original_open = os.open
            original_fstat = os.fstat
            original_pread = os.pread
            original_close = os.close
            target_fd: int | None = None
            retained: list[int] = []

            def open_file(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal target_fd
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
                if path == "coordination.sqlite3" and dir_fd == root_fd:
                    target_fd = fd
                return fd

            def fail_pread(fd: int, size: int, offset: int) -> bytes:
                if fd == target_fd:
                    raise OSError("injected database read failure")
                return original_pread(fd, size, offset)

            for index in range(_MAX_ORPHAN_FDS):
                path = state_root / f"overflow-{index}"
                path.write_bytes(b"overflow")
                path.chmod(0o600)
                fd = original_open(path, os.O_RDONLY)
                retained.append(fd)
                metadata = original_fstat(fd)
                store._orphan_fds.append(
                    (fd, (metadata.st_dev, metadata.st_ino), f"overflow-{index}")
                )

            try:

                def fail_target_close(fd: int) -> None:
                    if fd == target_fd:
                        raise OSError("injected overflow close failure")
                    original_close(fd)

                with (
                    mock.patch("agent_team.store.os.open", side_effect=open_file),
                    mock.patch("agent_team.store.os.pread", side_effect=fail_pread),
                    mock.patch(
                        "agent_team.store.os.close",
                        side_effect=fail_target_close,
                    ),
                    self.assertRaises(StoreUnavailableError) as raised,
                ):
                    store._open_database_file(create=False)
                self.assertIsNotNone(target_fd)
                with (
                    mock.patch(
                        "agent_team.store.os.close",
                        side_effect=fail_target_close,
                    ),
                    self.assertRaises(StoreUnavailableError),
                ):
                    raised.exception.retry_cleanup()
                self.assertEqual(0, len(store._orphan_fds))
                raised.exception.retry_cleanup()
            finally:
                try:
                    store.close()
                except StoreError:
                    pass
                for fd in retained:
                    try:
                        original_close(fd)
                    except OSError:
                        pass

    def test_initial_marker_inspection_handoffs_close_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            store = CoordinationStore(state_root)
            marker = state_root / "writer.marker"
            marker_identity = (marker.stat().st_dev, marker.stat().st_ino)
            original_close = os.close

            def fail_close(fd: int) -> None:
                try:
                    metadata = os.fstat(fd)
                except OSError:
                    metadata = None
                if (
                    metadata is not None
                    and (metadata.st_dev, metadata.st_ino) == marker_identity
                ):
                    raise OSError("injected initial marker close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.store.os.close", side_effect=fail_close),
                    self.assertRaises(StoreUnavailableError) as raised,
                ):
                    store._validate_initial_writer_marker(marker.stat())
                self.assertEqual(1, len(store._orphan_fds))
                self.assertTrue(callable(raised.exception.retry_cleanup))
                raised.exception.retry_cleanup()
                self.assertEqual([], store._orphan_fds)
            finally:
                store.close()

    def test_lifetime_gate_open_handoffs_close_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            store = CoordinationStore(state_root)
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            gate_identity = (gate.stat().st_dev, gate.stat().st_ino)
            original_stat = os.stat
            original_close = os.close
            stat_calls = 0

            def fail_after_open(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *,
                dir_fd: int | None = None,
                follow_symlinks: bool = True,
            ) -> os.stat_result:
                nonlocal stat_calls
                stat_calls += 1
                if stat_calls == 2:
                    raise OSError("injected gate validation failure")
                return original_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            def fail_close(fd: int) -> None:
                try:
                    metadata = os.fstat(fd)
                except OSError:
                    metadata = None
                if (
                    metadata is not None
                    and (metadata.st_dev, metadata.st_ino) == gate_identity
                ):
                    raise OSError("injected gate close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.store.os.stat", side_effect=fail_after_open),
                    mock.patch("agent_team.store.os.close", side_effect=fail_close),
                    self.assertRaises(StoreUnavailableError) as raised,
                ):
                    store._open_lifetime_gate(create=False)
                self.assertEqual(1, len(store._orphan_fds))
                raised.exception.retry_cleanup()
                self.assertEqual([], store._orphan_fds)
            finally:
                store.close()

    def test_sidecar_mode_enforcement_handoffs_close_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            store = CoordinationStore(state_root)
            sidecar = state_root / "coordination.sqlite3-wal"
            sidecar.write_bytes(b"sidecar")
            sidecar.chmod(0o600)
            sidecar_identity = (sidecar.stat().st_dev, sidecar.stat().st_ino)
            original_close = os.close
            store._sidecars_before_open = frozenset()

            def fail_close(fd: int) -> None:
                try:
                    metadata = os.fstat(fd)
                except OSError:
                    metadata = None
                if (
                    metadata is not None
                    and (metadata.st_dev, metadata.st_ino) == sidecar_identity
                ):
                    raise OSError("injected sidecar close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.store.os.close", side_effect=fail_close),
                    self.assertRaises(StoreUnavailableError) as raised,
                ):
                    store._enforce_sidecar_modes()
                self.assertEqual(1, len(store._orphan_fds))
                raised.exception.retry_cleanup()
                self.assertEqual([], store._orphan_fds)
            finally:
                store.close()
                sidecar.unlink(missing_ok=True)

    def test_shared_gate_body_error_wins_and_retry_closes_store(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            gate_identity = (gate.stat().st_dev, gate.stat().st_ino)
            original_flock = fcntl.flock
            original_close = os.close

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if operation == fcntl.LOCK_UN:
                    try:
                        metadata = os.fstat(fd)
                    except OSError:
                        metadata = None
                    if (
                        metadata is not None
                        and (metadata.st_dev, metadata.st_ino) == gate_identity
                    ):
                        raise OSError("injected persistent gate unlock failure")
                original_flock(fd, operation)

            def fail_gate_close(fd: int) -> None:
                try:
                    metadata = os.fstat(fd)
                except OSError:
                    metadata = None
                if (
                    metadata is not None
                    and (metadata.st_dev, metadata.st_ino) == gate_identity
                ):
                    raise OSError("injected persistent gate close failure")
                original_close(fd)

            store = CoordinationStore(state_root)
            try:
                with (
                    mock.patch(
                        "agent_team.store.fcntl.flock",
                        side_effect=fail_gate_unlock,
                    ),
                    mock.patch(
                        "agent_team.store.os.close",
                        side_effect=fail_gate_close,
                    ),
                    self.assertRaises(RuntimeError) as raised,
                    store,
                    store._shared_lifetime_gate(),
                ):
                    raise RuntimeError("shared body is primary")

                error = raised.exception
                self.assertIs(type(error), RuntimeError)
                self.assertEqual("shared body is primary", str(error))
                retry_cleanup = cast(Callable[[], None], vars(error)["retry_cleanup"])
                self.assertTrue(callable(retry_cleanup))
                retry_cleanup()
                retry_cleanup()
                with CoordinationStore(state_root) as next_store:
                    next_store.create_intent(
                        "after-shared-cleanup",
                        effect_key="effect/after-shared-cleanup",
                        actor="main",
                    )
            finally:
                try:
                    store.close()
                except StoreError:
                    pass

    def test_exclusive_gate_body_error_wins_and_retry_closes_store(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            gate_identity = (gate.stat().st_dev, gate.stat().st_ino)
            original_flock = fcntl.flock
            original_close = os.close

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if operation == fcntl.LOCK_UN:
                    try:
                        metadata = os.fstat(fd)
                    except OSError:
                        metadata = None
                    if (
                        metadata is not None
                        and (metadata.st_dev, metadata.st_ino) == gate_identity
                    ):
                        raise OSError("injected persistent gate unlock failure")
                original_flock(fd, operation)

            def fail_gate_close(fd: int) -> None:
                try:
                    metadata = os.fstat(fd)
                except OSError:
                    metadata = None
                if (
                    metadata is not None
                    and (metadata.st_dev, metadata.st_ino) == gate_identity
                ):
                    raise OSError("injected persistent gate close failure")
                original_close(fd)

            store = CoordinationStore(state_root)
            try:
                with (
                    mock.patch(
                        "agent_team.store.fcntl.flock",
                        side_effect=fail_gate_unlock,
                    ),
                    mock.patch(
                        "agent_team.store.os.close",
                        side_effect=fail_gate_close,
                    ),
                    self.assertRaises(RuntimeError) as raised,
                    store._exclusive_lifetime_gate(),
                ):
                    raise RuntimeError("exclusive body is primary")

                error = raised.exception
                self.assertIs(type(error), RuntimeError)
                self.assertEqual("exclusive body is primary", str(error))
                retry_cleanup = cast(Callable[[], None], vars(error)["retry_cleanup"])
                self.assertTrue(callable(retry_cleanup))
                retry_cleanup()
                retry_cleanup()
                with CoordinationStore(state_root) as next_store:
                    next_store.create_intent(
                        "after-exclusive-cleanup",
                        effect_key="effect/after-exclusive-cleanup",
                        actor="main",
                    )
            finally:
                store.close()

    def test_exit_success_body_cleanup_failure_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            gate_identity = (gate.stat().st_dev, gate.stat().st_ino)
            original_flock = fcntl.flock
            original_close = os.close

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if operation == fcntl.LOCK_UN:
                    try:
                        metadata = os.fstat(fd)
                    except OSError:
                        metadata = None
                    if (
                        metadata is not None
                        and (metadata.st_dev, metadata.st_ino) == gate_identity
                    ):
                        raise OSError("injected persistent gate unlock failure")
                original_flock(fd, operation)

            def fail_gate_close(fd: int) -> None:
                try:
                    metadata = os.fstat(fd)
                except OSError:
                    metadata = None
                if (
                    metadata is not None
                    and (metadata.st_dev, metadata.st_ino) == gate_identity
                ):
                    raise OSError("injected persistent gate close failure")
                original_close(fd)

            store = CoordinationStore(state_root)
            try:
                with (
                    mock.patch(
                        "agent_team.store.fcntl.flock",
                        side_effect=fail_gate_unlock,
                    ),
                    mock.patch(
                        "agent_team.store.os.close",
                        side_effect=fail_gate_close,
                    ),
                    self.assertRaises(StoreUnavailableError) as raised,
                    store,
                ):
                    pass

                error = raised.exception
                self.assertTrue(callable(error.retry_cleanup))
                error.retry_cleanup()
                with CoordinationStore(state_root):
                    pass
            finally:
                try:
                    store.close()
                except StoreError:
                    pass

    def test_exit_body_error_never_closes_reused_gate_fd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            store = CoordinationStore(state_root)
            gate_fd = store._lifetime_gate_fd
            self.assertIsNotNone(gate_fd)
            assert gate_fd is not None
            gate_identity = (os.fstat(gate_fd).st_dev, os.fstat(gate_fd).st_ino)
            replacement = state_root / "replacement"
            replacement.write_bytes(b"replacement")
            replacement.chmod(0o600)
            original_close = os.close
            original_open = os.open
            replacement_fd: int | None = None

            def close(fd: int) -> None:
                nonlocal replacement_fd
                try:
                    metadata = os.fstat(fd)
                except OSError:
                    metadata = None
                if (
                    replacement_fd is None
                    and metadata is not None
                    and (metadata.st_dev, metadata.st_ino) == gate_identity
                ):
                    original_close(fd)
                    replacement_fd = original_open(replacement, os.O_RDONLY)
                    raise OSError("injected actual-close-then-error")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.store.os.close", side_effect=close),
                    self.assertRaises(RuntimeError) as raised,
                    store,
                ):
                    raise RuntimeError("exit body is primary")
                self.assertEqual([], store._orphan_fds)
                self.assertIsNotNone(replacement_fd)
                assert replacement_fd is not None
                os.fstat(replacement_fd)
                self.assertEqual("exit body is primary", str(raised.exception))
                cast(
                    Callable[[], None],
                    vars(raised.exception)["retry_cleanup"],
                )()
            finally:
                if replacement_fd is not None:
                    original_close(replacement_fd)
                store.close()

    def test_root_gate_cleanup_failure_is_retryable_and_does_not_return_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            gate_identity = (gate.stat().st_dev, gate.stat().st_ino)
            original_flock = fcntl.flock
            original_close = os.close

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if operation == fcntl.LOCK_UN:
                    try:
                        metadata = os.fstat(fd)
                    except OSError:
                        metadata = None
                    if (
                        metadata is not None
                        and (
                            metadata.st_dev,
                            metadata.st_ino,
                        )
                        == gate_identity
                    ):
                        raise OSError("injected persistent gate unlock failure")
                original_flock(fd, operation)

            def fail_gate_close(fd: int) -> None:
                try:
                    metadata = os.fstat(fd)
                except OSError:
                    metadata = None
                if (
                    metadata is not None
                    and (
                        metadata.st_dev,
                        metadata.st_ino,
                    )
                    == gate_identity
                ):
                    raise OSError("injected persistent gate close failure")
                original_close(fd)

            with (
                mock.patch(
                    "agent_team.store.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                mock.patch(
                    "agent_team.store.os.close",
                    side_effect=fail_gate_close,
                ),
                self.assertRaises(StoreUnavailableError) as raised,
                CoordinationStore._exclusive_lifetime_gate_for_root(state_root),
            ):
                pass
            error = raised.exception
            self.assertTrue(callable(error.retry_cleanup))

            with (
                mock.patch(
                    "agent_team.store.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                mock.patch(
                    "agent_team.store.os.close",
                    side_effect=fail_gate_close,
                ),
                self.assertRaises(StoreUnavailableError) as retry_raised,
            ):
                error.retry_cleanup()
            self.assertTrue(callable(retry_raised.exception.retry_cleanup))

            with (
                mock.patch(
                    "agent_team.store.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                mock.patch(
                    "agent_team.store.os.close",
                    side_effect=fail_gate_close,
                ),
                self.assertRaises(StoreBusyError),
                CoordinationStore._exclusive_lifetime_gate_for_root(
                    state_root,
                    busy_timeout_ms=20,
                ),
            ):
                pass

            error.retry_cleanup()
            error.retry_cleanup()
            with CoordinationStore(state_root):
                pass

    def test_root_gate_cleanup_failure_preserves_store_body_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            gate_identity = (gate.stat().st_dev, gate.stat().st_ino)
            original_flock = fcntl.flock
            original_close = os.close

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if operation == fcntl.LOCK_UN:
                    try:
                        metadata = os.fstat(fd)
                    except OSError:
                        metadata = None
                    if (
                        metadata is not None
                        and (
                            metadata.st_dev,
                            metadata.st_ino,
                        )
                        == gate_identity
                    ):
                        raise OSError("injected persistent gate unlock failure")
                original_flock(fd, operation)

            def fail_gate_close(fd: int) -> None:
                try:
                    metadata = os.fstat(fd)
                except OSError:
                    metadata = None
                if (
                    metadata is not None
                    and (
                        metadata.st_dev,
                        metadata.st_ino,
                    )
                    == gate_identity
                ):
                    raise OSError("injected persistent gate close failure")
                original_close(fd)

            body_error = StoreSchemaError("body failure is primary")
            with (
                mock.patch(
                    "agent_team.store.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                mock.patch(
                    "agent_team.store.os.close",
                    side_effect=fail_gate_close,
                ),
                self.assertRaises(StoreSchemaError) as raised,
                CoordinationStore._exclusive_lifetime_gate_for_root(state_root),
            ):
                raise body_error
            self.assertIs(raised.exception, body_error)
            self.assertEqual("body failure is primary", str(raised.exception))
            self.assertTrue(callable(raised.exception.retry_cleanup))

            raised.exception.retry_cleanup()
            with CoordinationStore(state_root):
                pass

    def test_root_gate_actual_close_then_error_never_closes_reused_fd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            gate_identity = (gate.stat().st_dev, gate.stat().st_ino)
            original_close = os.close
            closed_once = False
            closed_fd: int | None = None

            def close(fd: int) -> None:
                nonlocal closed_fd, closed_once
                try:
                    metadata = os.fstat(fd)
                except OSError:
                    metadata = None
                if (
                    not closed_once
                    and metadata is not None
                    and (metadata.st_dev, metadata.st_ino) == gate_identity
                ):
                    closed_once = True
                    closed_fd = fd
                    original_close(fd)
                    raise OSError("injected actual-close-then-error")
                original_close(fd)

            with (
                mock.patch("agent_team.store.os.close", side_effect=close),
                self.assertRaises(StoreUnavailableError) as raised,
                CoordinationStore._exclusive_lifetime_gate_for_root(state_root),
            ):
                pass

            fillers: list[int] = []
            replacement_fd: int | None = None
            try:
                self.assertIsNotNone(closed_fd)
                while replacement_fd is None:
                    candidate_fd = os.open(gate, os.O_RDONLY)
                    if candidate_fd == closed_fd:
                        replacement_fd = candidate_fd
                    else:
                        fillers.append(candidate_fd)
                raised.exception.retry_cleanup()
                os.fstat(replacement_fd)
            finally:
                if replacement_fd is not None:
                    original_close(replacement_fd)
                for filler_fd in fillers:
                    original_close(filler_fd)

    def test_constructor_body_error_keeps_retryable_partial_store(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            gate_identity = (gate.stat().st_dev, gate.stat().st_ino)
            original_flock = fcntl.flock
            original_close = os.close

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if operation == fcntl.LOCK_UN:
                    try:
                        metadata = os.fstat(fd)
                    except OSError:
                        metadata = None
                    if (
                        metadata is not None
                        and (
                            metadata.st_dev,
                            metadata.st_ino,
                        )
                        == gate_identity
                    ):
                        raise OSError("injected persistent gate unlock failure")
                original_flock(fd, operation)

            def fail_gate_close(fd: int) -> None:
                try:
                    metadata = os.fstat(fd)
                except OSError:
                    metadata = None
                if (
                    metadata is not None
                    and (
                        metadata.st_dev,
                        metadata.st_ino,
                    )
                    == gate_identity
                ):
                    raise OSError("injected persistent gate close failure")
                original_close(fd)

            with (
                mock.patch(
                    "agent_team.store.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                mock.patch(
                    "agent_team.store.os.close",
                    side_effect=fail_gate_close,
                ),
                self.assertRaises(StoreSchemaError) as raised,
            ):
                _BodyFailureStore(state_root)
            error = raised.exception
            self.assertEqual("injected constructor body failure", str(error))
            self.assertTrue(callable(error.retry_cleanup))

            with (
                mock.patch(
                    "agent_team.store.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                mock.patch(
                    "agent_team.store.os.close",
                    side_effect=fail_gate_close,
                ),
                self.assertRaises(StoreBusyError),
                CoordinationStore._exclusive_lifetime_gate_for_root(
                    state_root,
                    busy_timeout_ms=20,
                ),
            ):
                pass

            error.retry_cleanup()
            error.retry_cleanup()
            with CoordinationStore(state_root):
                pass

    def test_close_drops_actual_close_then_error_without_closing_reused_fd(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            store = CoordinationStore(state_root)
            marker_fd = store._marker_fd
            self.assertIsNotNone(marker_fd)
            assert marker_fd is not None
            original_close = os.close
            failed = False

            def close(fd: int) -> None:
                nonlocal failed
                if fd == marker_fd and not failed:
                    failed = True
                    original_close(fd)
                    raise OSError("simulated actual-close-then-error")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.store.os.close", side_effect=close),
                    self.assertRaises(StoreUnavailableError),
                ):
                    store.close()
                fillers: list[int] = []
                while True:
                    candidate_fd = os.open(
                        state_root / "coordination.sqlite3", os.O_RDONLY
                    )
                    if candidate_fd == marker_fd:
                        replacement_fd = candidate_fd
                        break
                    fillers.append(candidate_fd)
                try:
                    store.close()
                    os.fstat(replacement_fd)
                finally:
                    original_close(replacement_fd)
                    for filler_fd in fillers:
                        original_close(filler_fd)
            finally:
                try:
                    store.close()
                except StoreUnavailableError:
                    pass

    def test_close_gate_unlock_reuse_never_closes_foreign_fd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            store = CoordinationStore(state_root)
            gate_fd = store._lifetime_gate_fd
            self.assertIsNotNone(gate_fd)
            assert gate_fd is not None
            store._acquire_lifetime_gate(exclusive=False)
            gate_identity = (os.fstat(gate_fd).st_dev, os.fstat(gate_fd).st_ino)
            foreign_path = state_root / "foreign-gate"
            foreign_path.write_bytes(b"foreign")
            foreign_path.chmod(0o600)
            original_flock = fcntl.flock
            original_close = os.close
            original_open = os.open
            replacement_fd: int | None = None
            filler_fds: list[int] = []

            def unlock(fd: int, operation: int) -> None:
                nonlocal replacement_fd
                if operation == fcntl.LOCK_UN and fd == gate_fd:
                    metadata = os.fstat(fd)
                    self.assertEqual(gate_identity, (metadata.st_dev, metadata.st_ino))
                    original_close(fd)
                    while replacement_fd != fd:
                        candidate_fd = original_open(foreign_path, os.O_RDONLY)
                        if candidate_fd == fd:
                            replacement_fd = candidate_fd
                        else:
                            filler_fds.append(candidate_fd)
                    return
                original_flock(fd, operation)

            try:
                with (
                    mock.patch("agent_team.store.fcntl.flock", side_effect=unlock),
                    self.assertRaises(StoreUnavailableError),
                ):
                    store.close()
                self.assertIsNotNone(replacement_fd)
                assert replacement_fd is not None
                os.fstat(replacement_fd)
            finally:
                if replacement_fd is not None:
                    original_close(replacement_fd)
                for filler_fd in filler_fds:
                    original_close(filler_fd)
                try:
                    store.close()
                except StoreError:
                    pass

    def test_close_marker_unlock_reuse_never_closes_foreign_fd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            store = CoordinationStore(state_root)
            marker_fd = store._marker_fd
            self.assertIsNotNone(marker_fd)
            assert marker_fd is not None
            marker_identity = (os.fstat(marker_fd).st_dev, os.fstat(marker_fd).st_ino)
            foreign_path = state_root / "foreign-marker"
            foreign_path.write_bytes(b"foreign")
            foreign_path.chmod(0o600)
            original_flock = fcntl.flock
            original_close = os.close
            original_open = os.open
            replacement_fd: int | None = None
            filler_fds: list[int] = []

            def unlock(fd: int, operation: int) -> None:
                nonlocal replacement_fd
                if operation == fcntl.LOCK_UN and fd == marker_fd:
                    metadata = os.fstat(fd)
                    self.assertEqual(
                        marker_identity,
                        (metadata.st_dev, metadata.st_ino),
                    )
                    original_close(fd)
                    while replacement_fd != fd:
                        candidate_fd = original_open(foreign_path, os.O_RDONLY)
                        if candidate_fd == fd:
                            replacement_fd = candidate_fd
                        else:
                            filler_fds.append(candidate_fd)
                    return
                original_flock(fd, operation)

            try:
                with (
                    mock.patch("agent_team.store.fcntl.flock", side_effect=unlock),
                    self.assertRaises(StoreUnavailableError),
                ):
                    store.close()
                self.assertIsNotNone(replacement_fd)
                assert replacement_fd is not None
                os.fstat(replacement_fd)
            finally:
                if replacement_fd is not None:
                    original_close(replacement_fd)
                for filler_fd in filler_fds:
                    original_close(filler_fd)
                try:
                    store.close()
                except StoreError:
                    pass

    def test_constructor_fd_handoff_checks_identity_after_unlock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            store = CoordinationStore(state_root)
            marker = state_root / "writer.marker"
            fd = os.open(marker, os.O_RDONLY)
            identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            foreign_path = state_root / "foreign-handoff"
            foreign_path.write_bytes(b"foreign")
            foreign_path.chmod(0o600)
            original_flock = fcntl.flock
            original_close = os.close
            original_open = os.open
            replacement_fd: int | None = None
            filler_fds: list[int] = []

            def unlock(candidate_fd: int, operation: int) -> None:
                nonlocal replacement_fd
                if operation == fcntl.LOCK_UN and candidate_fd == fd:
                    original_close(candidate_fd)
                    while replacement_fd != candidate_fd:
                        opened_fd = original_open(foreign_path, os.O_RDONLY)
                        if opened_fd == candidate_fd:
                            replacement_fd = opened_fd
                        else:
                            filler_fds.append(opened_fd)
                    return
                original_flock(candidate_fd, operation)

            try:
                with mock.patch(
                    "agent_team.store.fcntl.flock",
                    side_effect=unlock,
                ):
                    error = store._handoff_constructor_fd(
                        fd,
                        identity,
                        "marker handoff",
                        unlock=True,
                    )
                self.assertIsInstance(error, StoreUnavailableError)
                self.assertIsNotNone(replacement_fd)
                assert replacement_fd is not None
                os.fstat(replacement_fd)
            finally:
                if replacement_fd is not None:
                    original_close(replacement_fd)
                for filler_fd in filler_fds:
                    original_close(filler_fd)
                try:
                    store.close()
                except StoreError:
                    pass

    def test_root_gate_unlock_reuse_never_closes_foreign_fd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            foreign_path = state_root / "foreign-root-gate"
            foreign_path.write_bytes(b"foreign")
            foreign_path.chmod(0o600)
            original_flock = fcntl.flock
            original_close = os.close
            original_open = os.open
            replacement_fd: int | None = None
            filler_fds: list[int] = []
            tracked_fd: int | None = None

            def unlock(fd: int, operation: int) -> None:
                nonlocal replacement_fd, tracked_fd
                if operation == fcntl.LOCK_UN:
                    if tracked_fd is None:
                        tracked_fd = fd
                    if fd == tracked_fd:
                        original_close(fd)
                        while replacement_fd != fd:
                            candidate_fd = original_open(foreign_path, os.O_RDONLY)
                            if candidate_fd == fd:
                                replacement_fd = candidate_fd
                            else:
                                filler_fds.append(candidate_fd)
                        return
                original_flock(fd, operation)

            try:
                with (
                    mock.patch("agent_team.store.fcntl.flock", side_effect=unlock),
                    self.assertRaises(StoreUnavailableError),
                    CoordinationStore._exclusive_lifetime_gate_for_root(state_root),
                ):
                    pass
                self.assertIsNotNone(replacement_fd)
                assert replacement_fd is not None
                os.fstat(replacement_fd)
            finally:
                if replacement_fd is not None:
                    original_close(replacement_fd)
                for filler_fd in filler_fds:
                    original_close(filler_fd)

    def test_retained_preflight_fd_is_drained_before_next_preflight(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                fd = os.open(state_root / "writer.marker", os.O_RDONLY)
                identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
                store._retain_failed_fd(fd, identity, "preflight test")
                self.assertEqual(1, len(store._orphan_fds))
                store._run_normal_open_preflight()
                self.assertEqual([], store._orphan_fds)
                with self.assertRaises(OSError):
                    os.fstat(fd)

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
                    EVENT_SCHEMA_VERSION,
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

    def test_commit_response_loss_is_unknown_and_releases_on_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            original_connect = sqlite3.connect

            def connect_with_commit_loss(
                *args: object, **kwargs: object
            ) -> sqlite3.Connection:
                kwargs["factory"] = _CommitResponseLostConnection
                connect = cast(Callable[..., sqlite3.Connection], original_connect)
                return connect(*args, **kwargs)

            store: CoordinationStore | None = None
            with mock.patch(
                "agent_team.store.sqlite3.connect",
                side_effect=connect_with_commit_loss,
            ):
                store = CoordinationStore(state_root)
                try:
                    with self.assertRaises(StoreCommitUnknownError) as raised:
                        store.create_intent(
                            "commit-unknown",
                            effect_key="effect/commit-unknown",
                            actor="main",
                        )
                    self.assertIsInstance(raised.exception.__cause__, OSError)
                    self.assertTrue(store._connection_cleanup_pending)
                    with self.assertRaises(StoreUnavailableError):
                        store.operation("commit-unknown")
                    raised.exception.retry_cleanup()
                    self.assertIsNone(store._connection)
                finally:
                    if store is not None:
                        store.close()
            with CoordinationStore(state_root) as next_store:
                self.assertIsNotNone(next_store.operation("commit-unknown"))

    def test_rollback_failure_preserves_body_and_releases_on_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            original_connect = sqlite3.connect

            def connect_with_rollback_failure(
                *args: object,
                **kwargs: object,
            ) -> sqlite3.Connection:
                kwargs["factory"] = _RollbackFailureConnection
                connect = cast(Callable[..., sqlite3.Connection], original_connect)
                return connect(*args, **kwargs)

            store: CoordinationStore | None = None
            with mock.patch(
                "agent_team.store.sqlite3.connect",
                side_effect=connect_with_rollback_failure,
            ):
                store = _BeforeCommitFailureStore(state_root)
                try:
                    with self.assertRaises(RuntimeError) as raised:
                        store.create_intent(
                            "rollback-unknown",
                            effect_key="effect/rollback-unknown",
                            actor="main",
                        )
                    self.assertEqual(
                        "transaction body is primary",
                        str(raised.exception),
                    )
                    self.assertTrue(store._connection_cleanup_pending)
                    self.assertTrue(callable(vars(raised.exception)["retry_cleanup"]))
                    with self.assertRaises(StoreUnavailableError):
                        store.operation("rollback-unknown")
                    cast(Callable[[], None], vars(raised.exception)["retry_cleanup"])()
                    self.assertIsNone(store._connection)
                finally:
                    if store is not None:
                        store.close()
            with CoordinationStore(state_root) as next_store:
                self.assertIsNone(next_store.operation("rollback-unknown"))

    def test_restore_image_connection_close_failure_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            database = _database(state_root)
            metadata = database.stat()
            image = database.read_bytes()
            original_connect = sqlite3.connect
            _CloseFailureConnection.fail_close = True
            _CloseFailureConnection.close_calls = 0

            def connect_with_close_failure(
                *args: object,
                **kwargs: object,
            ) -> sqlite3.Connection:
                kwargs["factory"] = _CloseFailureConnection
                connect = cast(Callable[..., sqlite3.Connection], original_connect)
                return connect(*args, **kwargs)

            try:
                with mock.patch(
                    "agent_team.store.sqlite3.connect",
                    side_effect=connect_with_close_failure,
                ):
                    with self.assertRaises(StoreUnavailableError) as raised:
                        CoordinationStore._inspect_image_bytes(
                            metadata,
                            image,
                            label="restore",
                        )
                    error = raised.exception
                    self.assertTrue(callable(error.retry_cleanup))
                    with self.assertRaises(StoreUnavailableError):
                        error.retry_cleanup()
                    self.assertGreaterEqual(_CloseFailureConnection.close_calls, 2)
                    _CloseFailureConnection.fail_close = False
                    error.retry_cleanup()
                    error.retry_cleanup()
            finally:
                _CloseFailureConnection.fail_close = False

    def test_restore_image_body_error_stays_primary_when_close_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            database = _database(state_root)
            metadata = database.stat()
            image = database.read_bytes()
            original_connect = sqlite3.connect
            _CloseFailureConnection.fail_close = True

            def connect_with_close_failure(
                *args: object,
                **kwargs: object,
            ) -> sqlite3.Connection:
                kwargs["factory"] = _CloseFailureConnection
                connect = cast(Callable[..., sqlite3.Connection], original_connect)
                return connect(*args, **kwargs)

            def fail_schema(connection: sqlite3.Connection) -> None:
                del connection
                raise RuntimeError("restore image body is primary")

            try:
                with (
                    mock.patch(
                        "agent_team.store.sqlite3.connect",
                        side_effect=connect_with_close_failure,
                    ),
                    mock.patch(
                        "agent_team.store._validate_existing_schema",
                        side_effect=fail_schema,
                    ),
                    self.assertRaises(RuntimeError) as raised,
                ):
                    CoordinationStore._inspect_image_bytes(
                        metadata,
                        image,
                        label="restore",
                    )
                self.assertEqual(
                    "restore image body is primary",
                    str(raised.exception),
                )
                self.assertTrue(callable(vars(raised.exception)["retry_cleanup"]))
                with self.assertRaises(StoreUnavailableError):
                    cast(Callable[[], None], vars(raised.exception)["retry_cleanup"])()
                _CloseFailureConnection.fail_close = False
                cast(Callable[[], None], vars(raised.exception)["retry_cleanup"])()
            finally:
                _CloseFailureConnection.fail_close = False

    def test_restore_event_connection_close_failure_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                store.create_intent(
                    "restore-events",
                    effect_key="effect/restore-events",
                    actor="main",
                )
            image = _database(state_root).read_bytes()
            original_connect = sqlite3.connect
            _CloseFailureConnection.fail_close = True

            def connect_with_close_failure(
                *args: object,
                **kwargs: object,
            ) -> sqlite3.Connection:
                kwargs["factory"] = _CloseFailureConnection
                connect = cast(Callable[..., sqlite3.Connection], original_connect)
                return connect(*args, **kwargs)

            try:
                with (
                    mock.patch(
                        "agent_team.store.sqlite3.connect",
                        side_effect=connect_with_close_failure,
                    ),
                    self.assertRaises(StoreUnavailableError) as raised,
                ):
                    CoordinationStore._read_image_events(image, label="restore")
                self.assertTrue(callable(raised.exception.retry_cleanup))
                with self.assertRaises(StoreUnavailableError):
                    raised.exception.retry_cleanup()
                _CloseFailureConnection.fail_close = False
                raised.exception.retry_cleanup()
            finally:
                _CloseFailureConnection.fail_close = False

    def test_cleanup_capability_retries_all_members_and_is_idempotent(self) -> None:
        calls: list[str] = []
        fail_first = True

        def first() -> None:
            nonlocal fail_first
            calls.append("first")
            if fail_first:
                raise OSError("first cleanup failed")

        def second() -> None:
            calls.append("second")

        error = StoreUnavailableError("cleanup owner")
        error._attach_cleanup_capability(
            _CleanupCapability.compose(
                _CleanupCapability(first),
                _CleanupCapability(second),
            )
        )
        with self.assertRaises(OSError):
            error.retry_cleanup()
        self.assertEqual(["first", "second"], calls)
        fail_first = False
        error.retry_cleanup()
        self.assertEqual(["first", "second", "first"], calls)
        error.retry_cleanup()
        self.assertEqual(["first", "second", "first"], calls)

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
                event_schema_version=EVENT_SCHEMA_VERSION,
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
                event_schema_version=EVENT_SCHEMA_VERSION,
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
                connection.execute(
                    """
                    INSERT INTO operation_attempts(
                        operation_id, attempt, owner, provider_id,
                        lease_epoch, fencing_token, lease_heartbeat_ns,
                        lease_expires_ns
                    ) VALUES ('op-1', 1, 'owner', 'provider/op-1', 0, 1, 1, 2)
                    """
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO effect_receipts(
                            operation_id, attempt, effect_key, provider_effect_id,
                            provider_status, provider_id, owner, fencing_token,
                            lease_epoch, received_ns, proof_version, proof_ref
                        ) VALUES ('missing', 1, 'effect/missing', 'provider/missing',
                                  'COMPLETED', 'provider/missing', 'owner', 1, 0, 1,
                                  1, 'proof/missing')
                        """
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO transition_events(
                            event_id, event_schema_version, operation_id, attempt,
                            from_status, to_status, kind, actor, clock_ns,
                            reason_code, evidence_ref
                        ) VALUES (99, 2, 'missing', 0, NULL, 'INTENT', 'intent',
                                  'actor', 1, 'intent_created', NULL)
                        """
                    )

                connection.execute(
                    """
                    INSERT INTO effect_receipts(
                        operation_id, attempt, effect_key, provider_effect_id,
                        provider_status, provider_id, owner, fencing_token,
                        lease_epoch, received_ns, proof_version, proof_ref
                    ) VALUES ('op-1', 1, 'effect/op-1', 'provider/op-1',
                              'COMPLETED', 'provider/op-1', 'owner', 1, 0, 1,
                              1, 'proof/op-1')
                    """
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO effect_receipts(
                            operation_id, attempt, effect_key, provider_effect_id,
                            provider_status, provider_id, owner, fencing_token,
                            lease_epoch, received_ns, proof_version, proof_ref
                        ) VALUES ('op-1', 1, 'effect/op-1', 'provider/op-1',
                                  'COMPLETED', 'provider/op-1', 'owner', 1, 0, 2,
                                  1, 'proof/op-1')
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
                        INSERT OR REPLACE INTO transition_events(
                            event_id, event_schema_version, operation_id, attempt,
                            from_status, to_status, kind, actor, clock_ns,
                            reason_code, evidence_ref
                        ) VALUES (1, 2, 'op-1', 0, NULL, 'INTENT', 'intent',
                                  'actor', 2, 'intent_created', NULL)
                        """
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO transition_events(
                            event_id, event_schema_version, operation_id, attempt,
                            from_status, to_status, kind, actor, clock_ns,
                            reason_code, evidence_ref
                        ) VALUES (1, 2, 'op-1', 0, NULL, 'INTENT', 'duplicate',
                                  'actor', 2, 'intent_created', NULL)
                        """
                    )
            finally:
                connection.close()

    def test_receipt_sql_constraints_reject_unsupported_identity_rows(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                store.create_intent("op-1", effect_key="effect/op-1", actor="main")

            connection = sqlite3.connect(
                str(_database(state_root)), isolation_level=None
            )
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO effect_receipts(
                            operation_id, attempt, effect_key, provider_effect_id,
                            provider_status, provider_id, owner, fencing_token,
                            lease_epoch, received_ns, proof_version, proof_ref
                        ) VALUES ('op-1', 0, 'effect/op-1', 'provider/op-1',
                                  'GARBAGE', 'provider/op-1', 'owner', 0, 0, 1,
                                  1, 'proof/op-1')
                        """
                    )
            finally:
                connection.close()

    def test_private_event_append_rejects_free_form_actor_and_public_reason(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                store.create_intent("op-1", effect_key="effect/op-1", actor="main")
                with (
                    self.assertRaises(ValueError),
                    store._write_transaction() as connection,
                ):
                    store._append_event(
                        connection,
                        operation_id="op-1",
                        attempt=0,
                        from_status=None,
                        to_status="INTENT",
                        kind="intent",
                        actor="prompt text",
                        timestamp=2,
                        reason_code="intent_created",
                    )
                with self.assertRaises(ValueError):
                    store.create_intent(
                        "op-2",
                        effect_key="effect/op-2",
                        actor="main",
                        reason_code="recover",
                    )

    def test_lifetime_gate_is_external_and_never_unlinked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            with CoordinationStore(state_root, busy_timeout_ms=20) as store:
                self.assertTrue(gate.is_file())
                self.assertNotEqual(state_root, gate.parent)
                gate_identity = (gate.stat().st_dev, gate.stat().st_ino)
                context = multiprocessing.get_context("spawn")
                ready = context.Event()
                release = context.Event()
                process = context.Process(
                    target=_hold_lifetime_gate,
                    args=(str(state_root), ready, release),
                )
                process.start()
                try:
                    self.assertTrue(ready.wait(timeout=10))
                    with (
                        self.assertRaises(StoreBusyError),
                        store._exclusive_lifetime_gate(),
                    ):
                        pass
                finally:
                    release.set()
                    process.join(timeout=10)
                    if process.is_alive():
                        process.kill()
                        process.join()
                self.assertEqual(0, process.exitcode)
            self.assertTrue(gate.is_file())
            self.assertEqual(
                gate_identity,
                (gate.stat().st_dev, gate.stat().st_ino),
            )
            with CoordinationStore._exclusive_lifetime_gate_for_root(
                state_root,
                busy_timeout_ms=20,
            ):
                pass

    def test_reserve_floor_holds_shared_gate_for_entire_metadata_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root, busy_timeout_ms=20) as store:
                context = multiprocessing.get_context("spawn")
                ready = context.Event()
                release = context.Event()
                process = context.Process(
                    target=_hold_lifetime_gate_exclusive,
                    args=(str(state_root), ready, release),
                )
                process.start()
                try:
                    self.assertTrue(ready.wait(timeout=10))
                    with self.assertRaises(StoreBusyError):
                        store._reserve_floor()
                finally:
                    release.set()
                    process.join(timeout=10)
                    if process.is_alive():
                        process.kill()
                        process.join()
                self.assertEqual(0, process.exitcode)

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
            invalid_root = root / "invalid-without-gate"
            invalid_root.mkdir()
            invalid_root.chmod(0o700)
            invalid_database = _database(invalid_root)
            raw = sqlite3.connect(str(invalid_database))
            try:
                raw.execute("CREATE TABLE invalid_schema(value TEXT NOT NULL)")
                raw.commit()
            finally:
                raw.close()
            invalid_database.chmod(0o600)
            (invalid_root / "writer.marker").write_bytes(WRITER_MARKER_CLEAN_CONTENT)
            (invalid_root / "writer.marker").chmod(0o600)
            invalid_files_before = tuple(
                sorted(path.name for path in invalid_root.iterdir())
            )
            invalid_bytes_before = invalid_database.read_bytes()
            with self.assertRaises(StoreSchemaError):
                CoordinationStore(invalid_root)
            self.assertFalse((root / LIFETIME_GATE_FILENAME).exists())
            self.assertEqual(
                invalid_files_before,
                tuple(sorted(path.name for path in invalid_root.iterdir())),
            )
            self.assertEqual(invalid_bytes_before, invalid_database.read_bytes())

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
            (future_root / "writer.marker").write_bytes(WRITER_MARKER_CLEAN_CONTENT)
            (future_root / "writer.marker").chmod(0o600)
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
            (empty_future_root / "writer.marker").write_bytes(
                WRITER_MARKER_CLEAN_CONTENT
            )
            (empty_future_root / "writer.marker").chmod(0o600)
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

    def test_normal_open_preflight_loads_committed_tombstones_before_store_creation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            calls: list[int] = []

            def preflight(
                root_fd: int,
                **kwargs: object,
            ) -> object:
                calls.append(root_fd)
                del kwargs
                from agent_team import recovery
                from agent_team.lease import RestoreIdentity

                digest = "sha256:" + "a" * 64
                ledger = recovery.RecoveryLedgerRecord(
                    version=recovery.RECOVERY_LEDGER_VERSION,
                    sequence=1,
                    phase="RESTORE_COMMITTED",
                    restore_generation=1,
                    recovery_epoch=2,
                    fencing_token_floor=2,
                    backup_digest=digest,
                    actor="operator",
                    audit_ref="audit/restore",
                )
                tombstone = recovery.RecoveryTombstoneRecord(
                    version=recovery.TOMBSTONE_LOG_VERSION,
                    sequence=1,
                    phase="COMMITTED",
                    restore_generation=1,
                    backup_digest=digest,
                    previous_primary_digest=digest,
                    candidate_digest=digest,
                    previous_recovery_epoch=1,
                    previous_fencing_token_hwm=1,
                    previous_last_clock_ns=1,
                    identities=(
                        RestoreIdentity(
                            "tombstoned-operation",
                            "effect/tombstoned",
                        ),
                    ),
                    actor="operator",
                    audit_ref="audit/restore",
                )
                handle = recovery._issue_restore_handle(ledger, tombstone)
                return recovery._issue_normal_open_recovery_state(
                    frozenset({("tombstoned-operation", "effect/tombstoned")}),
                    handle,
                )

            from agent_team import recovery

            with (
                mock.patch.object(
                    recovery,
                    "_normal_open_preflight",
                    side_effect=preflight,
                    create=True,
                ),
                mock.patch.object(
                    RestoreStoreAuthority,
                    "verify_history_binding",
                    return_value=None,
                ),
                CoordinationStore(state_root) as store,
            ):
                self.assertGreaterEqual(len(calls), 2)
                with self.assertRaises(DuplicateOperationError):
                    store.create_intent(
                        "tombstoned-operation",
                        effect_key="effect/new",
                        actor="main",
                    )
                with self.assertRaises(DuplicateOperationError):
                    store.create_intent(
                        "new-operation",
                        effect_key="effect/tombstoned",
                        actor="main",
                    )

    def test_restore_authority_history_binding_accepts_empty_recovery_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-history-binding-"
        ) as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                database_fd = os.open(_database(state_root), os.O_RDONLY)
                try:
                    from agent_team import recovery

                    state = recovery._issue_normal_open_recovery_state(
                        frozenset(),
                        None,
                    )
                    RestoreStoreAuthority().verify_history_binding(
                        database_fd,
                        state,
                    )
                finally:
                    os.close(database_fd)

    def test_history_binding_rejects_log_tamper_before_create_and_reopen(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-history-binding-tamper-"
        ) as temporary:
            state_root = _make_state_root(temporary)
            identity = RestoreIdentity("anchor-op", "effect/anchor")
            digest = "sha256:" + "a" * 64
            with CoordinationStore(state_root) as store:
                store.create_intent(
                    identity.operation_id,
                    effect_key=identity.effect_key,
                    actor="main",
                    clock_ns=100,
                )
                reservation = store._reserve_floor()
                store._advance_floor(reservation, now_ns=101)
                ledger_records = (
                    recovery.RecoveryLedgerRecord(
                        version=recovery.RECOVERY_LEDGER_VERSION,
                        sequence=1,
                        phase="RESTORE_PREPARED",
                        restore_generation=1,
                        recovery_epoch=1,
                        fencing_token_floor=1,
                        backup_digest=digest,
                        actor="operator",
                        audit_ref="audit/restore",
                    ),
                    recovery.RecoveryLedgerRecord(
                        version=recovery.RECOVERY_LEDGER_VERSION,
                        sequence=2,
                        phase="RESTORE_REPLACED",
                        restore_generation=1,
                        recovery_epoch=1,
                        fencing_token_floor=1,
                        backup_digest=digest,
                        actor="operator",
                        audit_ref="audit/restore",
                    ),
                    recovery.RecoveryLedgerRecord(
                        version=recovery.RECOVERY_LEDGER_VERSION,
                        sequence=3,
                        phase="RESTORE_COMMITTED",
                        restore_generation=1,
                        recovery_epoch=1,
                        fencing_token_floor=1,
                        backup_digest=digest,
                        actor="operator",
                        audit_ref="audit/restore",
                    ),
                )
                tombstone_records = (
                    recovery.RecoveryTombstoneRecord(
                        version=recovery.TOMBSTONE_LOG_VERSION,
                        sequence=1,
                        phase="PREPARED",
                        restore_generation=1,
                        backup_digest=digest,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(identity,),
                        actor="operator",
                        audit_ref="audit/restore",
                    ),
                    recovery.RecoveryTombstoneRecord(
                        version=recovery.TOMBSTONE_LOG_VERSION,
                        sequence=2,
                        phase="COMMITTED",
                        restore_generation=1,
                        backup_digest=digest,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(identity,),
                        actor="operator",
                        audit_ref="audit/restore",
                    ),
                )
                handle = recovery._issue_restore_handle(
                    ledger_records[-1],
                    tombstone_records[-1],
                )
                state = recovery._issue_normal_open_recovery_state(
                    frozenset({(identity.operation_id, identity.effect_key)}),
                    handle,
                )
                binding_ref = _restore_history_binding_ref(state)
                self.assertIsNotNone(binding_ref)
                with store._write_transaction() as connection:
                    CoordinationStore._append_event(
                        connection,
                        operation_id=identity.operation_id,
                        attempt=0,
                        from_status=None,
                        to_status="INTENT",
                        kind="restore",
                        actor="operator",
                        timestamp=101,
                        reason_code="restore",
                        evidence_ref=binding_ref,
                    )
            _write_recovery_history(state_root, ledger_records, tombstone_records)
            with CoordinationStore(state_root) as store:
                store.create_intent(
                    "legit-op",
                    effect_key="effect/legit",
                    actor="main",
                    clock_ns=102,
                )
            with CoordinationStore(state_root) as store:
                tombstone_path = state_root / recovery.RECOVERY_TOMBSTONES_BASENAME
                changed_lines: list[bytes] = []
                for raw_line in tombstone_path.read_bytes().splitlines():
                    item = cast(dict[str, object], json.loads(raw_line))
                    item["identities"] = [
                        {
                            "operation_id": "forged-op",
                            "effect_key": "effect/forged",
                        }
                    ]
                    changed_lines.append(
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                tombstone_path.write_bytes(b"\n".join(changed_lines) + b"\n")
                before = _row_counts(_database(state_root))
                with self.assertRaises(StoreIntegrityError):
                    store.create_intent(
                        "new-op",
                        effect_key="effect/new",
                        actor="main",
                        clock_ns=102,
                    )
                self.assertEqual(before, _row_counts(_database(state_root)))
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(state_root)

    def test_history_binding_includes_prior_union_when_current_batch_is_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-history-binding-cumulative-"
        ) as temporary:
            state_root = _make_state_root(temporary)
            first_identity = RestoreIdentity("op-a", "effect/a")
            digest_one = "sha256:" + "a" * 64
            digest_two = "sha256:" + "b" * 64
            with CoordinationStore(state_root) as store:
                store.create_intent(
                    "anchor-g2",
                    effect_key="effect/anchor-g2",
                    actor="main",
                    clock_ns=100,
                )
                first_floor = store._reserve_floor()
                store._advance_floor(first_floor, now_ns=101)
                second_floor = store._reserve_floor()
                store._advance_floor(second_floor, now_ns=102)
                ledger_records = (
                    recovery.RecoveryLedgerRecord(
                        version=recovery.RECOVERY_LEDGER_VERSION,
                        sequence=1,
                        phase="RESTORE_PREPARED",
                        restore_generation=1,
                        recovery_epoch=1,
                        fencing_token_floor=1,
                        backup_digest=digest_one,
                        actor="operator",
                        audit_ref="audit/restore/one",
                    ),
                    recovery.RecoveryLedgerRecord(
                        version=recovery.RECOVERY_LEDGER_VERSION,
                        sequence=2,
                        phase="RESTORE_REPLACED",
                        restore_generation=1,
                        recovery_epoch=1,
                        fencing_token_floor=1,
                        backup_digest=digest_one,
                        actor="operator",
                        audit_ref="audit/restore/one",
                    ),
                    recovery.RecoveryLedgerRecord(
                        version=recovery.RECOVERY_LEDGER_VERSION,
                        sequence=3,
                        phase="RESTORE_COMMITTED",
                        restore_generation=1,
                        recovery_epoch=1,
                        fencing_token_floor=1,
                        backup_digest=digest_one,
                        actor="operator",
                        audit_ref="audit/restore/one",
                    ),
                    recovery.RecoveryLedgerRecord(
                        version=recovery.RECOVERY_LEDGER_VERSION,
                        sequence=4,
                        phase="RESTORE_PREPARED",
                        restore_generation=2,
                        recovery_epoch=2,
                        fencing_token_floor=2,
                        backup_digest=digest_two,
                        actor="operator",
                        audit_ref="audit/restore/two",
                    ),
                    recovery.RecoveryLedgerRecord(
                        version=recovery.RECOVERY_LEDGER_VERSION,
                        sequence=5,
                        phase="RESTORE_REPLACED",
                        restore_generation=2,
                        recovery_epoch=2,
                        fencing_token_floor=2,
                        backup_digest=digest_two,
                        actor="operator",
                        audit_ref="audit/restore/two",
                    ),
                    recovery.RecoveryLedgerRecord(
                        version=recovery.RECOVERY_LEDGER_VERSION,
                        sequence=6,
                        phase="RESTORE_COMMITTED",
                        restore_generation=2,
                        recovery_epoch=2,
                        fencing_token_floor=2,
                        backup_digest=digest_two,
                        actor="operator",
                        audit_ref="audit/restore/two",
                    ),
                )
                tombstone_records = (
                    recovery.RecoveryTombstoneRecord(
                        version=recovery.TOMBSTONE_LOG_VERSION,
                        sequence=1,
                        phase="PREPARED",
                        restore_generation=1,
                        backup_digest=digest_one,
                        previous_primary_digest="sha256:" + "c" * 64,
                        candidate_digest="sha256:" + "d" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(first_identity,),
                        actor="operator",
                        audit_ref="audit/restore/one",
                    ),
                    recovery.RecoveryTombstoneRecord(
                        version=recovery.TOMBSTONE_LOG_VERSION,
                        sequence=2,
                        phase="COMMITTED",
                        restore_generation=1,
                        backup_digest=digest_one,
                        previous_primary_digest="sha256:" + "c" * 64,
                        candidate_digest="sha256:" + "d" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(first_identity,),
                        actor="operator",
                        audit_ref="audit/restore/one",
                    ),
                    recovery.RecoveryTombstoneRecord(
                        version=recovery.TOMBSTONE_LOG_VERSION,
                        sequence=3,
                        phase="PREPARED",
                        restore_generation=2,
                        backup_digest=digest_two,
                        previous_primary_digest="sha256:" + "e" * 64,
                        candidate_digest="sha256:" + "f" * 64,
                        previous_recovery_epoch=1,
                        previous_fencing_token_hwm=1,
                        previous_last_clock_ns=101,
                        identities=(),
                        actor="operator",
                        audit_ref="audit/restore/two",
                    ),
                    recovery.RecoveryTombstoneRecord(
                        version=recovery.TOMBSTONE_LOG_VERSION,
                        sequence=4,
                        phase="COMMITTED",
                        restore_generation=2,
                        backup_digest=digest_two,
                        previous_primary_digest="sha256:" + "e" * 64,
                        candidate_digest="sha256:" + "f" * 64,
                        previous_recovery_epoch=1,
                        previous_fencing_token_hwm=1,
                        previous_last_clock_ns=101,
                        identities=(),
                        actor="operator",
                        audit_ref="audit/restore/two",
                    ),
                )
                handle = recovery._issue_restore_handle(
                    ledger_records[-1],
                    tombstone_records[-1],
                )
                state = recovery._issue_normal_open_recovery_state(
                    frozenset(
                        {(first_identity.operation_id, first_identity.effect_key)}
                    ),
                    handle,
                )
                binding_ref = _restore_history_binding_ref(state)
                self.assertIsNotNone(binding_ref)
                with store._write_transaction() as connection:
                    CoordinationStore._append_event(
                        connection,
                        operation_id="anchor-g2",
                        attempt=0,
                        from_status=None,
                        to_status="INTENT",
                        kind="restore",
                        actor="operator",
                        timestamp=102,
                        reason_code="restore",
                        evidence_ref=binding_ref,
                    )
            _write_recovery_history(state_root, ledger_records, tombstone_records)
            with CoordinationStore(state_root):
                pass
            tombstone_path = state_root / recovery.RECOVERY_TOMBSTONES_BASENAME
            changed_lines: list[bytes] = []
            for raw_line in tombstone_path.read_bytes().splitlines():
                item = cast(dict[str, object], json.loads(raw_line))
                if item["restore_generation"] == 1:
                    item["identities"] = [
                        {
                            "operation_id": "forged-op",
                            "effect_key": "effect/forged",
                        }
                    ]
                changed_lines.append(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            tombstone_path.write_bytes(b"\n".join(changed_lines) + b"\n")
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(state_root)

    def test_normal_open_preflight_failure_happens_before_sqlite_or_gate_creation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            gate = state_root.parent / LIFETIME_GATE_FILENAME

            def preflight(root_fd: int, **kwargs: object) -> None:
                del root_fd, kwargs
                raise StoreUnavailableError("pending recovery ledger")

            from agent_team import recovery

            with (
                mock.patch.object(
                    recovery,
                    "_normal_open_preflight",
                    side_effect=preflight,
                    create=True,
                ),
                mock.patch(
                    "agent_team.store.sqlite3.connect",
                    side_effect=AssertionError("SQLite must not open"),
                ),
                self.assertRaises(StoreUnavailableError),
            ):
                CoordinationStore(state_root)
            self.assertFalse(gate.exists())
            self.assertEqual((), tuple(state_root.iterdir()))

    def test_missing_normal_open_preflight_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            from agent_team import recovery

            with (
                mock.patch.object(
                    recovery,
                    "_normal_open_preflight",
                    None,
                    create=True,
                ),
                self.assertRaises(StoreUnavailableError),
            ):
                CoordinationStore(state_root)
            self.assertEqual((), tuple(state_root.iterdir()))
            self.assertFalse((state_root.parent / LIFETIME_GATE_FILENAME).exists())

    def test_normal_open_preflight_rejects_legacy_iterable_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)

            def preflight(root_fd: int, **kwargs: object) -> frozenset[object]:
                del root_fd, kwargs
                return frozenset()

            with (
                mock.patch.object(
                    recovery,
                    "_normal_open_preflight",
                    side_effect=preflight,
                ),
                self.assertRaises(StoreUnavailableError),
            ):
                CoordinationStore(state_root)
            self.assertEqual((), tuple(state_root.iterdir()))

    def test_fresh_bootstrap_connect_failure_restores_empty_fileset(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            with (
                mock.patch(
                    "agent_team.store.sqlite3.connect",
                    side_effect=OSError("injected connect failure"),
                ),
                self.assertRaises(StoreUnavailableError),
            ):
                CoordinationStore(state_root)
            self.assertEqual((), tuple(state_root.iterdir()))
            self.assertFalse(gate.exists())

    def test_fresh_bootstrap_release_failures_preserve_durable_state(self) -> None:
        for store_type in (
            _ReleaseStartupFailureStore,
            _ReleaseLifetimeFailureStore,
        ):
            with (
                self.subTest(store_type=store_type.__name__),
                tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary,
            ):
                state_root = _make_state_root(temporary)
                gate = state_root.parent / LIFETIME_GATE_FILENAME
                with self.assertRaises(StoreUnavailableError):
                    store_type(state_root)
                self.assertTrue(_database(state_root).exists())
                self.assertTrue((state_root / "writer.marker").exists())
                self.assertTrue(gate.exists())
                with CoordinationStore(state_root):
                    pass

    def test_fresh_cleanup_preserves_unowned_known_files_and_sidecars(self) -> None:
        for store_type, filename, expected in (
            (_ForeignDatabaseStore, "coordination.sqlite3", b"foreign-database"),
            (_ForeignMarkerStore, "writer.marker", b"foreign-marker"),
            (_ForeignSidecarStore, "coordination.sqlite3-wal", b"foreign-sidecar"),
            (_ForeignGateStore, LIFETIME_GATE_FILENAME, b"foreign-gate"),
        ):
            with (
                self.subTest(store_type=store_type.__name__),
                tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary,
            ):
                state_root = _make_state_root(temporary)
                path = state_root / filename
                if filename == LIFETIME_GATE_FILENAME:
                    path = state_root.parent / filename
                with self.assertRaises(StoreUnavailableError):
                    store_type(state_root)
                self.assertTrue(path.exists())
                self.assertEqual(expected, path.read_bytes())
                self.assertEqual(1, path.stat().st_nlink)

    def test_fresh_cleanup_removes_opener_created_sidecars_for_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with self.assertRaises(StoreUnavailableError):
                _FreshSidecarFailureStore(state_root)
            self.assertEqual((), tuple(state_root.iterdir()))
            self.assertFalse((state_root.parent / LIFETIME_GATE_FILENAME).exists())

    def test_pending_recovery_ledger_blocks_before_store_file_creation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root):
                pass
            gate = state_root.parent / LIFETIME_GATE_FILENAME
            gate.unlink()
            from agent_team import recovery

            digest = "sha256:" + "a" * 64
            record = recovery.RecoveryLedgerRecord(
                version=recovery.RECOVERY_LEDGER_VERSION,
                sequence=1,
                phase="RESTORE_PREPARED",
                restore_generation=1,
                recovery_epoch=1,
                fencing_token_floor=1,
                backup_digest=digest,
                actor="operator",
                audit_ref="audit/restore",
            )
            authority = recovery._issue_recovery_ledger_initialization(
                operator_id="operator",
                audit_ref="audit/restore",
                request_digest=digest,
            )
            recovery.RecoveryLedgerWriter(state_root).initialize(record, authority)
            sqlite_connect = mock.patch(
                "agent_team.store.sqlite3.connect",
                side_effect=AssertionError("SQLite must not open"),
            )
            with sqlite_connect, self.assertRaises(StoreUnavailableError):
                CoordinationStore(state_root)
            self.assertFalse(gate.exists())
            self.assertTrue((state_root / recovery.RECOVERY_LEDGER_BASENAME).exists())

    def test_fresh_bootstrap_rechecks_root_before_database_creation(self) -> None:
        class LateEntryStore(CoordinationStore):
            inventory_calls = 0

            def _initial_root_inventory(self) -> frozenset[str]:
                names = super()._initial_root_inventory()
                type(self).inventory_calls += 1
                if type(self).inventory_calls == 1:
                    late = self.state_root / "late-entry"
                    late.write_bytes(b"preserve")
                    late.chmod(0o600)
                return names

        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with self.assertRaises(StoreUnavailableError):
                LateEntryStore(state_root)
            self.assertEqual(
                ["late-entry"],
                sorted(path.name for path in state_root.iterdir()),
            )
            self.assertFalse(_database(state_root).exists())
            self.assertFalse((state_root / "writer.marker").exists())

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

    def test_c2_lease_commit_identity_loss_is_reported_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                store.create_intent(
                    "lease-operation",
                    effect_key="effect/lease-operation",
                    provider_id="provider/test",
                    actor="main",
                )
            with (
                self.assertRaises(StoreCommitUnknownError),
                _IdentitySwapStore(state_root, "after_commit", "database") as store,
            ):
                store.claim(
                    "lease-operation",
                    owner="owner-a",
                    provider_id="provider/test",
                    lease_ttl_ns=20,
                )
            old_database = state_root / "coordination.sqlite3-old"
            self.assertEqual((1, 2), _row_counts(old_database))

    def test_c2_recovery_commit_identity_loss_closes_facade(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            with CoordinationStore(state_root) as store:
                store.create_intent(
                    "recovery-operation",
                    effect_key="effect/recovery-operation",
                    actor="main",
                )
            transaction = None
            with (
                self.assertRaises(StoreCommitUnknownError),
                _IdentitySwapStore(state_root, "after_commit", "database") as store,
                store._recovery_transaction() as active,
            ):
                transaction = active
                snapshot = active.snapshot("recovery-operation")
                active.append_event(
                    snapshot,
                    kind="recover",
                    reason_code="recover",
                    actor="operator",
                    timestamp=snapshot.updated_ns + 1,
                )
            assert transaction is not None
            with self.assertRaises(StoreClosedError):
                transaction.snapshot("recovery-operation")

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

    def test_fresh_database_opens_concurrently_without_gate_deadlock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-store-") as temporary:
            state_root = _make_state_root(temporary)
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            results = context.Queue()
            processes = [
                context.Process(
                    target=_fresh_open_worker,
                    args=(str(state_root), barrier, results),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=15)
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail("fresh database opener did not exit")
            self.assertEqual(
                ["opened", "opened"], sorted(results.get() for _ in processes)
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
                ["coordination.sqlite3", "writer.marker"],
                sorted(path.name for path in state_root.iterdir()),
            )
            with self.assertRaises(StoreClosedError):
                store.operation("contender")


if __name__ == "__main__":
    unittest.main()
