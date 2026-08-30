from __future__ import annotations

import copy
import gc
import hashlib
import os
import pickle
import signal
import sqlite3
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from agent_team.doctor import ReadOnlyDoctor, StateFilesystem, StateFilesystemError
from agent_team.recovery import RecoveryCoordinator
from agent_team.store import (
    CoordinationStore,
    StoreBusyError,
    StoreUnavailableError,
)
from agent_team.wal import (
    CHECKPOINT_MODES,
    JOURNAL_BASENAME,
    MARKER_CLEAN_CONTENT,
    MARKER_PREPARED_CONTENT,
    SHM_BASENAME,
    WAL_BASENAME,
    WRITER_MARKER_BASENAME,
    CheckpointRequest,
    CheckpointResult,
    DatabaseCopyTarget,
    QuiescenceSession,
    WalSidecarBusyError,
    WalSidecarClosedError,
    WalSidecarController,
    WalSidecarRecoveryRequiredError,
    WalSidecarUnsafeError,
)

_TEST_EXCEPTION: type[BaseException] = BaseException


def _make_root(temporary: str, name: str = "state") -> Path:
    root = Path(os.path.realpath(temporary)) / name
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _write_structurally_valid_wal(root: Path) -> Path:
    """Create a shape-valid but checksum-unknown WAL fixture."""

    header = bytearray(32)
    struct.pack_into(">I", header, 0, 0x377F0682)
    struct.pack_into(">I", header, 4, 3_007_000)
    struct.pack_into(">I", header, 8, 4096)
    struct.pack_into(">I", header, 12, 0)
    struct.pack_into(">I", header, 16, 1)
    struct.pack_into(">I", header, 20, 2)
    struct.pack_into(">I", header, 24, 3)
    struct.pack_into(">I", header, 28, 4)
    frame = bytearray(24 + 4096)
    struct.pack_into(">I", frame, 0, 1)
    struct.pack_into(">I", frame, 4, 1)
    struct.pack_into(">I", frame, 8, 1)
    struct.pack_into(">I", frame, 12, 2)
    struct.pack_into(">I", frame, 16, 3)
    struct.pack_into(">I", frame, 20, 4)
    wal = root / WAL_BASENAME
    wal.write_bytes(bytes(header) + bytes(frame))
    wal.chmod(0o600)
    return wal


class WalSidecarControllerTest(unittest.TestCase):
    def test_store_creates_one_stable_marker_and_holds_shared_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                marker = root / WRITER_MARKER_BASENAME
                self.assertTrue(marker.is_file())
                metadata = marker.stat()
                self.assertEqual(0o600, stat.S_IMODE(metadata.st_mode))
                self.assertEqual(os.getuid(), metadata.st_uid)
                self.assertEqual(1, metadata.st_nlink)
                with self.assertRaises(StoreBusyError):
                    WalSidecarController(root, busy_timeout_ms=20).cleanup(
                        CheckpointRequest("TRUNCATE")
                    )
                self.assertEqual(metadata.st_ino, marker.stat().st_ino)
            self.assertEqual(metadata.st_ino, marker.stat().st_ino)

    def test_store_marker_creation_sigkill_barriers_are_fail_closed(self) -> None:
        barriers = (
            "before_marker_create",
            "after_marker_create",
            "before_marker_fsync",
            "after_marker_fsync",
            "before_marker_lock",
            "after_marker_lock",
        )

        class KillStore(CoordinationStore):
            def __init__(self, state_root: Path, barrier: str) -> None:
                self.barrier = barrier
                super().__init__(state_root)

            def _fault(self, point: str) -> None:
                if point == self.barrier:
                    os.kill(os.getpid(), signal.SIGKILL)

        for barrier in barriers:
            with (
                self.subTest(barrier=barrier),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-wal-marker-kill-"
                ) as temporary,
            ):
                root = _make_root(temporary)
                child = os.fork()
                if child == 0:
                    try:
                        KillStore(root, barrier)
                    except _TEST_EXCEPTION:
                        os._exit(2)
                    os._exit(3)
                _, wait_status = os.waitpid(child, 0)
                self.assertEqual(
                    -signal.SIGKILL,
                    os.waitstatus_to_exitcode(wait_status),
                )
                marker = root / WRITER_MARKER_BASENAME
                if barrier == "before_marker_create":
                    self.assertFalse(marker.exists())
                    with self.assertRaises(StoreUnavailableError):
                        CoordinationStore(root)
                else:
                    self.assertEqual(MARKER_CLEAN_CONTENT, marker.read_bytes())
                    marker_identity = (marker.stat().st_dev, marker.stat().st_ino)
                    with CoordinationStore(root):
                        pass
                    self.assertEqual(
                        marker_identity,
                        (marker.stat().st_dev, marker.stat().st_ino),
                    )

    def test_each_checkpoint_mode_is_bounded_by_an_open_writer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                controller = WalSidecarController(root, busy_timeout_ms=20)
                for mode in CHECKPOINT_MODES:
                    with self.subTest(mode=mode), self.assertRaises(StoreBusyError):
                        controller.checkpoint(CheckpointRequest(mode))

    def test_hold_quiescence_keeps_guards_until_session_close(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = WalSidecarController(root, busy_timeout_ms=20)
            session = controller.hold_quiescence()
            try:
                self.assertNotIn(str(root), repr(session))
                session.assert_identity()
                checkpoint = session.checkpoint(CheckpointRequest("PASSIVE"))
                self.assertEqual("PASSIVE", checkpoint.request.mode)
                with self.assertRaises(StoreBusyError):
                    controller.checkpoint(CheckpointRequest("TRUNCATE"))
            finally:
                session.close()
            controller.checkpoint(CheckpointRequest("TRUNCATE"))

    def test_quiescence_session_cannot_be_copied_or_pickled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = WalSidecarController(root, busy_timeout_ms=20)
            session = controller.hold_quiescence()
            try:
                with self.assertRaises(TypeError):
                    copy.copy(session)
                with self.assertRaises(TypeError):
                    copy.deepcopy(session)
                with self.assertRaises(TypeError):
                    pickle.dumps(session)
                with self.assertRaises(StoreBusyError):
                    controller.checkpoint(CheckpointRequest("PASSIVE"))
            finally:
                session.close()
            WalSidecarController(root).checkpoint(CheckpointRequest("PASSIVE"))

    def test_uninitialized_quiescence_session_is_typed_closed(self) -> None:
        session = object.__new__(QuiescenceSession)
        with self.assertRaises(WalSidecarClosedError):
            repr(session)
        with self.assertRaises(WalSidecarClosedError):
            session.assert_identity()
        with self.assertRaises(WalSidecarClosedError):
            session.checkpoint(CheckpointRequest("PASSIVE"))
        with self.assertRaises(WalSidecarClosedError):
            session.cleanup(CheckpointRequest("PASSIVE"))
        with self.assertRaises(WalSidecarClosedError):
            session.close()

    def test_uninitialized_public_checkpoint_values_and_controller_are_typed(
        self,
    ) -> None:
        result = object.__new__(CheckpointResult)
        with self.assertRaises(WalSidecarClosedError):
            _ = result.safe
        with self.assertRaises(WalSidecarClosedError):
            _ = result.values
        with self.assertRaises(WalSidecarClosedError):
            list(result)
        controller = object.__new__(WalSidecarController)
        with self.assertRaises(WalSidecarClosedError):
            _ = controller.state_root
        with self.assertRaises(WalSidecarClosedError):
            _ = controller.busy_timeout_ms
        with self.assertRaises(WalSidecarClosedError):
            _ = controller.marker_name
        with self.assertRaises(WalSidecarClosedError):
            controller.hold_quiescence()
        with self.assertRaises(WalSidecarClosedError):
            controller.checkpoint(CheckpointRequest("PASSIVE"))
        with self.assertRaises(WalSidecarClosedError):
            controller.cleanup(CheckpointRequest("PASSIVE"))

    def test_forged_quiescence_session_cannot_close_issued_resources(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = WalSidecarController(root, busy_timeout_ms=20)
            session = controller.hold_quiescence()
            forged = object.__new__(QuiescenceSession)
            object.__setattr__(forged, "_controller", session._controller)
            object.__setattr__(forged, "_resources", session._resources)
            object.__setattr__(forged, "_token", session._token)
            object.__setattr__(forged, "_closed", False)
            try:
                with self.assertRaises(WalSidecarClosedError):
                    forged.close()
                session.assert_identity()
                with self.assertRaises(StoreBusyError):
                    controller.checkpoint(CheckpointRequest("PASSIVE"))
            finally:
                session.close()
            WalSidecarController(root).checkpoint(CheckpointRequest("PASSIVE"))

    def test_closed_sessions_leave_no_controller_capability_registry_entries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = WalSidecarController(root, busy_timeout_ms=20)
            for _ in range(20):
                session = controller.hold_quiescence()
                session.close()
            gc.collect()
            self.assertEqual({}, controller._active_sessions)

    def test_quiescence_session_cleanup_uses_same_typed_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root, busy_timeout_ms=20).hold_quiescence()
            try:
                result = session.cleanup(CheckpointRequest("TRUNCATE"))
                self.assertEqual("CLEANED", result.outcome)
            finally:
                session.close()

    def test_quiescence_can_rebind_replaced_database_without_releasing_guards(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / "coordination.sqlite3"
            session = WalSidecarController(root, busy_timeout_ms=20).hold_quiescence()
            try:
                replacement = root / "coordination.sqlite3-replacement"
                replacement.write_bytes(database.read_bytes())
                replacement.chmod(0o600)
                os.replace(replacement, database)
                session._rebind_database()
                session.assert_identity()
                with self.assertRaises(StoreBusyError):
                    WalSidecarController(root, busy_timeout_ms=20).checkpoint(
                        CheckpointRequest("PASSIVE")
                    )
            finally:
                session.close()
            with self.assertRaises(WalSidecarClosedError):
                session.assert_identity()

    def test_invalid_schema_does_not_create_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            database = root / "coordination.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE unrelated(value TEXT)")
                connection.commit()
            finally:
                connection.close()
            database.chmod(0o600)
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(root)
            self.assertFalse((root / WRITER_MARKER_BASENAME).exists())

    def test_cleanup_removes_only_exact_empty_sqlite_sidecars(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-cleanup",
                    effect_key="effect/op-cleanup",
                    actor="main",
                    clock_ns=1,
                )
            target_names = (
                "coordination.sqlite3-wal",
                "coordination.sqlite3-shm",
                "coordination.sqlite3-journal",
            )
            for name in target_names:
                target = root / name
                target.write_bytes(b"")
                target.chmod(0o600)
            unknown = root / "provider.receipt"
            unknown.write_bytes(b"keep")
            unknown.chmod(0o600)

            result = WalSidecarController(root).cleanup(CheckpointRequest("TRUNCATE"))

            self.assertEqual("CLEANED", result.outcome)
            self.assertEqual(target_names, result.removed)
            self.assertTrue(all(not (root / name).exists() for name in target_names))
            self.assertEqual(b"keep", unknown.read_bytes())
            self.assertTrue((root / WRITER_MARKER_BASENAME).exists())
            self.assertEqual(
                MARKER_CLEAN_CONTENT, (root / WRITER_MARKER_BASENAME).read_bytes()
            )

    def test_nonempty_rollback_journal_blocks_before_sqlite_open(self) -> None:
        class NoOpenController(WalSidecarController):
            def _open_connection(
                self,
                resources: object,
                *,
                reject_nonzero_wal: bool = False,
            ) -> sqlite3.Connection:
                del resources
                del reject_nonzero_wal
                raise AssertionError("SQLite must not open with a pending journal")

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            journal = root / JOURNAL_BASENAME
            journal.write_bytes(b"unapplied-rollback")
            journal.chmod(0o600)
            result = NoOpenController(root).cleanup(CheckpointRequest("TRUNCATE"))
            self.assertEqual("BLOCKED", result.outcome)
            self.assertEqual("JOURNAL_PENDING", result.reason)
            self.assertIsNone(result.checkpoint)
            self.assertEqual(b"unapplied-rollback", journal.read_bytes())

            with self.assertRaises(WalSidecarUnsafeError):
                NoOpenController(root).checkpoint(CheckpointRequest("TRUNCATE"))

    def test_invalid_nonzero_wal_and_shm_are_rejected_before_sqlite_open(self) -> None:
        for name in (WAL_BASENAME, SHM_BASENAME):
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary,
            ):
                root = _make_root(temporary)
                with CoordinationStore(root):
                    pass
                sidecar = root / name
                sidecar.write_bytes(b"junk")
                sidecar.chmod(0o600)
                with self.assertRaises(WalSidecarUnsafeError):
                    WalSidecarController(root).checkpoint(CheckpointRequest("PASSIVE"))
                self.assertEqual(b"junk", sidecar.read_bytes())

    def test_journal_reappearing_before_delete_leaves_prepared_recovery(self) -> None:
        class ReappearController(WalSidecarController):
            def _fault(self, point: str) -> None:
                if point == "before_journal_delete":
                    journal = self.state_root / JOURNAL_BASENAME
                    journal.write_bytes(b"late-rollback")
                    journal.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            journal = root / JOURNAL_BASENAME
            with self.assertRaises(WalSidecarRecoveryRequiredError):
                ReappearController(root).cleanup(CheckpointRequest("TRUNCATE"))
            self.assertEqual(
                MARKER_PREPARED_CONTENT,
                (root / WRITER_MARKER_BASENAME).read_bytes(),
            )
            self.assertEqual(b"late-rollback", journal.read_bytes())
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(root)

    def test_cleanup_rejects_any_preexisting_nonzero_wal_before_sqlite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            wal = _write_structurally_valid_wal(root)
            original = wal.read_bytes()
            result = WalSidecarController(root).cleanup(CheckpointRequest("TRUNCATE"))
            self.assertEqual("BLOCKED", result.outcome)
            self.assertEqual("WAL_PENDING", result.reason)
            self.assertIsNone(result.checkpoint)
            self.assertEqual(original, wal.read_bytes())

    def test_source_copy_rejects_any_preexisting_nonzero_wal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            wal = _write_structurally_valid_wal(root)
            original = wal.read_bytes()
            session = WalSidecarController(root).hold_quiescence()
            try:
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    session.copy_database_to(
                        CheckpointRequest("TRUNCATE"),
                        DatabaseCopyTarget(name="snapshot"),
                    )
            finally:
                session.close()
            self.assertEqual(original, wal.read_bytes())
            self.assertFalse((root / "snapshot").exists())

    def test_store_rejects_nonempty_rollback_journal_before_sqlite_connect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            journal = root / JOURNAL_BASENAME
            journal.write_bytes(b"pending-rollback")
            journal.chmod(0o600)
            original = journal.read_bytes()
            with (
                mock.patch(
                    "agent_team.store.sqlite3.connect",
                    side_effect=AssertionError("SQLite must not open"),
                ),
                self.assertRaises(StoreUnavailableError),
            ):
                CoordinationStore(root)
            self.assertEqual(original, journal.read_bytes())

    def test_sidecar_reappearing_before_sqlite_close_reverts_marker(self) -> None:
        class ReappearController(WalSidecarController):
            def _fault(self, point: str) -> None:
                if point == "before_sqlite_close":
                    wal = self.state_root / WAL_BASENAME
                    wal.write_bytes(b"late-wal")
                    wal.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            with self.assertRaises(WalSidecarRecoveryRequiredError):
                ReappearController(root).cleanup(CheckpointRequest("TRUNCATE"))
            self.assertEqual(
                MARKER_PREPARED_CONTENT,
                (root / WRITER_MARKER_BASENAME).read_bytes(),
            )
            self.assertEqual(b"late-wal", (root / WAL_BASENAME).read_bytes())

    def test_cleanup_never_calls_python_unlink_for_sqlite_sidecars(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            journal = root / JOURNAL_BASENAME
            journal.write_bytes(b"")
            journal.chmod(0o600)
            with mock.patch(
                "agent_team.wal.os.unlink",
                side_effect=AssertionError("Python unlink is forbidden"),
            ) as unlink:
                result = WalSidecarController(root).cleanup(
                    CheckpointRequest("TRUNCATE")
                )
            self.assertEqual("CLEANED", result.outcome)
            unlink.assert_not_called()
            self.assertFalse(journal.exists())

    def test_marker_identity_survives_cleanup_and_next_store_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            marker = root / WRITER_MARKER_BASENAME
            marker_identity = (marker.stat().st_dev, marker.stat().st_ino)
            result = WalSidecarController(root).cleanup(CheckpointRequest("TRUNCATE"))
            self.assertEqual("CLEANED", result.outcome)
            self.assertEqual(
                marker_identity,
                (marker.stat().st_dev, marker.stat().st_ino),
            )
            with CoordinationStore(root):
                self.assertEqual(
                    marker_identity,
                    (marker.stat().st_dev, marker.stat().st_ino),
                )

    def test_unsafe_known_sidecar_is_rejected_without_opening_or_unlinking(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            target = root / "coordination.sqlite3-wal"
            target.symlink_to(root / "coordination.sqlite3")
            with self.assertRaises(WalSidecarUnsafeError):
                WalSidecarController(root).cleanup(CheckpointRequest("FULL"))
            self.assertTrue(target.is_symlink())

    def test_checkpoint_accepts_each_exact_mode_and_records_three_integers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass

            for mode in CHECKPOINT_MODES:
                with self.subTest(mode=mode):
                    result = WalSidecarController(root).checkpoint(
                        CheckpointRequest(mode)
                    )
                    self.assertIs(type(result), CheckpointResult)
                    self.assertEqual(mode, result.request.mode)
                    self.assertIs(type(result.busy), int)
                    self.assertIs(type(result.log), int)
                    self.assertIs(type(result.checkpointed), int)

    def test_checkpoint_request_and_controller_require_exact_typed_input(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            with self.assertRaises(ValueError):
                CheckpointRequest("UNKNOWN")  # type: ignore[arg-type]
            with self.assertRaises(TypeError):
                WalSidecarController(root).checkpoint("TRUNCATE")  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                WalSidecarController(root, marker_name="alternate.marker")
            with self.assertRaises(ValueError):
                DatabaseCopyTarget(name="\ud800")
            with self.assertRaises(ValueError):
                DatabaseCopyTarget(name=123)  # type: ignore[arg-type]
            forged_request = object.__new__(CheckpointRequest)
            object.__setattr__(forged_request, "mode", "PASSIVE; DROP TABLE operations")
            with self.assertRaises(ValueError):
                WalSidecarController(root).checkpoint(forged_request)
            controller = WalSidecarController(root)
            with self.assertRaises(AttributeError):
                controller.marker_name = "alternate.marker"  # type: ignore[misc]

    def test_missing_marker_is_rejected_without_creation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            database = root / "coordination.sqlite3"
            with CoordinationStore(root):
                pass
            marker = root / WRITER_MARKER_BASENAME
            marker.unlink()
            with self.assertRaises(WalSidecarUnsafeError):
                WalSidecarController(root).checkpoint(CheckpointRequest("PASSIVE"))
            self.assertFalse(marker.exists())
            self.assertTrue(database.exists())

    def test_unsafe_marker_is_rejected_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            marker = root / WRITER_MARKER_BASENAME
            marker.chmod(0o644)
            with self.assertRaises(WalSidecarUnsafeError):
                WalSidecarController(root).checkpoint(CheckpointRequest("FULL"))
            self.assertEqual(0o644, stat.S_IMODE(marker.stat().st_mode))

    def test_existing_reader_shared_marker_lock_blocks_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=WRITER_MARKER_BASENAME,
                ledger_name="recovery.ledger",
            )
            try:
                with self.assertRaises(StoreBusyError):
                    WalSidecarController(root, busy_timeout_ms=20).cleanup(
                        CheckpointRequest("TRUNCATE")
                    )
            finally:
                filesystem.close()

    def test_store_waits_for_lifetime_gate_before_sqlite_connect(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root, busy_timeout_ms=20).hold_quiescence()
            try:
                with (
                    mock.patch(
                        "agent_team.store.sqlite3.connect",
                        side_effect=AssertionError("SQLite connect was reached"),
                    ),
                    self.assertRaises(StoreBusyError),
                ):
                    CoordinationStore(root, busy_timeout_ms=20)
            finally:
                session.close()

    def test_store_rejects_marker_swap_after_initial_observation(self) -> None:
        class SwapStore(CoordinationStore):
            swapped = False

            def _fault(self, point: str) -> None:
                if point == "after_initial_marker_validation" and not self.swapped:
                    self.swapped = True
                    marker = self.state_root / WRITER_MARKER_BASENAME
                    old = self.state_root / "writer.marker.old"
                    replacement = self.state_root / WRITER_MARKER_BASENAME
                    marker.rename(old)
                    replacement.write_bytes(MARKER_CLEAN_CONTENT)
                    replacement.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            marker = root / WRITER_MARKER_BASENAME
            original_identity = (marker.stat().st_dev, marker.stat().st_ino)
            with self.assertRaises(StoreUnavailableError):
                SwapStore(root, busy_timeout_ms=20)
            old = root / "writer.marker.old"
            self.assertEqual(original_identity, (old.stat().st_dev, old.stat().st_ino))
            self.assertNotEqual(
                original_identity, (marker.stat().st_dev, marker.stat().st_ino)
            )

    def test_cleanup_fsyncs_root_directory_before_returning_cleaned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            with mock.patch("agent_team.wal.os.fsync", wraps=os.fsync) as fsync:
                result = WalSidecarController(root).cleanup(
                    CheckpointRequest("TRUNCATE")
                )
            self.assertEqual("CLEANED", result.outcome)
            self.assertGreaterEqual(fsync.call_count, 1)

    def test_active_zero_frame_reader_blocks_shm_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / "coordination.sqlite3"
            reader = sqlite3.connect(database, isolation_level=None)
            try:
                reader.execute("PRAGMA journal_mode=WAL")
                reader.execute("BEGIN")
                reader.execute("SELECT name FROM sqlite_master").fetchall()
                shm = root / "coordination.sqlite3-shm"
                self.assertTrue(shm.exists())
                for mode in CHECKPOINT_MODES:
                    with self.subTest(mode=mode):
                        result = WalSidecarController(
                            root,
                            busy_timeout_ms=20,
                        ).cleanup(CheckpointRequest(mode))
                        self.assertEqual("BLOCKED", result.outcome)
                        self.assertTrue(shm.exists())
            finally:
                reader.rollback()
                reader.close()

    def test_cleanup_uses_sqlite_owned_mode_transition_in_exact_order(self) -> None:
        class TraceController(WalSidecarController):
            statements: ClassVar[list[str]] = []

            @staticmethod
            def _execute_text_pragma(
                connection: sqlite3.Connection,
                statement: str,
                expected: str,
                label: str,
                busy_values: frozenset[str] = frozenset(),
            ) -> str:
                TraceController.statements.append(statement)
                return WalSidecarController._execute_text_pragma(
                    connection,
                    statement,
                    expected,
                    label,
                    busy_values,
                )

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = TraceController(root)
            result = controller.cleanup(CheckpointRequest("TRUNCATE"))
            self.assertEqual("CLEANED", result.outcome)
            self.assertEqual(
                [
                    "PRAGMA journal_mode=DELETE",
                    "PRAGMA locking_mode=EXCLUSIVE",
                    "PRAGMA journal_mode=WAL",
                ],
                controller.statements,
            )

    def test_raw_writer_after_checkpoint_blocks_delete_transition(self) -> None:
        class RawWriterController(WalSidecarController):
            raw_writer: sqlite3.Connection | None = None

            def _fault(self, point: str) -> None:
                if point == "after_checkpoint":
                    self.raw_writer = sqlite3.connect(
                        self.state_root / "coordination.sqlite3",
                        isolation_level=None,
                        timeout=0,
                    )
                    self.raw_writer.execute("BEGIN IMMEDIATE")

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            for mode in CHECKPOINT_MODES:
                with self.subTest(mode=mode):
                    controller = RawWriterController(root)
                    try:
                        result = controller.cleanup(CheckpointRequest(mode))
                        self.assertEqual("BLOCKED", result.outcome)
                        self.assertEqual("READER_ACTIVE", result.reason)
                        self.assertEqual(
                            MARKER_CLEAN_CONTENT,
                            (root / WRITER_MARKER_BASENAME).read_bytes(),
                        )
                    finally:
                        if controller.raw_writer is not None:
                            controller.raw_writer.rollback()
                            controller.raw_writer.close()

    def test_delete_mode_returning_wal_is_treated_as_busy(self) -> None:
        class WalReturnController(WalSidecarController):
            @staticmethod
            def _execute_text_pragma(
                connection: sqlite3.Connection,
                statement: str,
                expected: str,
                label: str,
                busy_values: frozenset[str] = frozenset(),
            ) -> str:
                if statement == "PRAGMA journal_mode=DELETE":
                    return "wal"
                return WalSidecarController._execute_text_pragma(
                    connection,
                    statement,
                    expected,
                    label,
                    busy_values,
                )

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            result = WalReturnController(root).cleanup(CheckpointRequest("TRUNCATE"))
            self.assertEqual("BLOCKED", result.outcome)
            self.assertEqual("READER_ACTIVE", result.reason)
            self.assertEqual(
                MARKER_CLEAN_CONTENT,
                (root / WRITER_MARKER_BASENAME).read_bytes(),
            )

    def test_wal_reentry_returning_delete_leaves_prepared_marker(self) -> None:
        class DeleteReturnController(WalSidecarController):
            @staticmethod
            def _execute_text_pragma(
                connection: sqlite3.Connection,
                statement: str,
                expected: str,
                label: str,
                busy_values: frozenset[str] = frozenset(),
            ) -> str:
                if statement == "PRAGMA journal_mode=WAL":
                    return "delete"
                return WalSidecarController._execute_text_pragma(
                    connection,
                    statement,
                    expected,
                    label,
                    busy_values,
                )

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            with self.assertRaises(WalSidecarRecoveryRequiredError):
                DeleteReturnController(root).cleanup(CheckpointRequest("TRUNCATE"))
            self.assertEqual(
                MARKER_PREPARED_CONTENT,
                (root / WRITER_MARKER_BASENAME).read_bytes(),
            )

    def test_delete_then_wal_reentry_failure_leaves_prepared_marker(self) -> None:
        class ReentryController(WalSidecarController):
            raw_writer: sqlite3.Connection | None = None

            def _fault(self, point: str) -> None:
                if point == "after_journal_delete":
                    self.raw_writer = sqlite3.connect(
                        self.state_root / "coordination.sqlite3",
                        isolation_level=None,
                        timeout=0,
                    )
                    self.raw_writer.execute("BEGIN IMMEDIATE")

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = ReentryController(root)
            try:
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    controller.cleanup(CheckpointRequest("TRUNCATE"))
                self.assertEqual(
                    MARKER_PREPARED_CONTENT,
                    (root / WRITER_MARKER_BASENAME).read_bytes(),
                )
            finally:
                if controller.raw_writer is not None:
                    controller.raw_writer.rollback()
                    controller.raw_writer.close()

    def test_new_sidecar_before_marker_clean_prevents_cleaned_success(self) -> None:
        class AddController(WalSidecarController):
            def _fault(self, point: str) -> None:
                if point == "before_marker_clean_inventory":
                    target = self.state_root / "coordination.sqlite3-wal"
                    target.write_bytes(b"unexpected")
                    target.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            with self.assertRaises(WalSidecarRecoveryRequiredError):
                AddController(root).cleanup(CheckpointRequest("TRUNCATE"))
            self.assertTrue((root / "coordination.sqlite3-wal").exists())
            self.assertEqual(
                MARKER_PREPARED_CONTENT,
                (root / WRITER_MARKER_BASENAME).read_bytes(),
            )

    def test_new_sidecar_after_close_is_post_linearization_activity(self) -> None:
        class AddController(WalSidecarController):
            def _fault(self, point: str) -> None:
                if point == "before_result":
                    target = self.state_root / "coordination.sqlite3-wal"
                    target.write_bytes(b"new-activity")
                    target.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            result = AddController(root).cleanup(CheckpointRequest("TRUNCATE"))
            self.assertEqual("CLEANED", result.outcome)
            self.assertTrue((root / "coordination.sqlite3-wal").exists())

    def test_resource_transfer_failure_releases_all_exclusive_resources(self) -> None:
        class FailingController(WalSidecarController):
            failed = False

            def _assert_resources(self, resources: object) -> None:
                if not self.failed:
                    self.failed = True
                    raise WalSidecarRecoveryRequiredError("injected identity failure")
                super()._assert_resources(resources)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            with self.assertRaises(WalSidecarRecoveryRequiredError):
                FailingController(root, busy_timeout_ms=20).checkpoint(
                    CheckpointRequest("PASSIVE")
                )
            WalSidecarController(root, busy_timeout_ms=20).checkpoint(
                CheckpointRequest("PASSIVE")
            )

    def test_existing_initialized_database_missing_marker_fails_without_recreate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            marker = root / WRITER_MARKER_BASENAME
            marker.unlink()
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(root)
            self.assertFalse(marker.exists())

    def test_marker_with_missing_database_fails_without_recreating_database(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-missing-db",
                    effect_key="effect/missing-db",
                    actor="main",
                    clock_ns=1,
                )
            database = root / "coordination.sqlite3"
            marker = root / WRITER_MARKER_BASENAME
            marker_identity = (marker.stat().st_dev, marker.stat().st_ino)
            database.unlink()
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(root)
            self.assertFalse(database.exists())
            self.assertEqual(
                marker_identity,
                (marker.stat().st_dev, marker.stat().st_ino),
            )

    def test_fresh_bootstrap_requires_an_entirely_empty_root(self) -> None:
        cases = {
            "coordination.sqlite3-wal": b"wal-evidence",
            "coordination.sqlite3-shm": b"shm-evidence",
            "coordination.sqlite3-journal": b"journal-evidence",
            "provider.receipt": b"provider-evidence",
        }
        for name, content in cases.items():
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary,
            ):
                root = _make_root(temporary)
                entry = root / name
                entry.write_bytes(content)
                entry.chmod(0o600)
                before = entry.lstat()
                with self.assertRaises(StoreUnavailableError):
                    CoordinationStore(root)
                after = entry.lstat()
                self.assertEqual(content, entry.read_bytes())
                self.assertEqual(
                    (before.st_dev, before.st_ino, before.st_size),
                    (after.st_dev, after.st_ino, after.st_size),
                )
                self.assertEqual(
                    (name,),
                    tuple(sorted(path.name for path in root.iterdir())),
                )
        for name in ("coordination.sqlite3-wal", "coordination.sqlite3-shm"):
            with (
                self.subTest(name=f"fifo:{name}"),
                tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary,
            ):
                root = _make_root(temporary)
                fifo = root / name
                os.mkfifo(fifo, 0o600)
                before = fifo.lstat()
                with self.assertRaises(StoreUnavailableError):
                    CoordinationStore(root)
                after = fifo.lstat()
                self.assertEqual(
                    (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode)),
                    (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode)),
                )
                self.assertFalse((root / "coordination.sqlite3").exists())
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            target = root.parent / "bootstrap-symlink-target"
            target.write_bytes(b"keep")
            link = root / "provider.receipt"
            link.symlink_to(target)
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(root)
            self.assertTrue(link.is_symlink())
            self.assertEqual(b"keep", target.read_bytes())
            self.assertFalse((root / "coordination.sqlite3").exists())

    def test_marker_with_truncated_database_fails_without_reinitializing_schema(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-truncated-db",
                    effect_key="effect/truncated-db",
                    actor="main",
                    clock_ns=1,
                )
            database = root / "coordination.sqlite3"
            marker = root / WRITER_MARKER_BASENAME
            marker_identity = (marker.stat().st_dev, marker.stat().st_ino)
            database.write_bytes(b"")
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(root)
            self.assertEqual(0, database.stat().st_size)
            self.assertEqual(
                marker_identity,
                (marker.stat().st_dev, marker.stat().st_ino),
            )

    def test_nonclean_marker_with_empty_database_fails_before_schema_write(
        self,
    ) -> None:
        for marker_content in (MARKER_PREPARED_CONTENT, b"malformed-marker\n"):
            with (
                self.subTest(marker_content=marker_content),
                tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary,
            ):
                root = _make_root(temporary)
                database = root / "coordination.sqlite3"
                sqlite3.connect(database).close()
                database.chmod(0o600)
                marker = root / WRITER_MARKER_BASENAME
                marker.write_bytes(marker_content)
                marker.chmod(0o600)
                with self.assertRaises(StoreUnavailableError):
                    CoordinationStore(root)
                self.assertEqual(0, database.stat().st_size)
                self.assertEqual(marker_content, marker.read_bytes())

    def test_cleanup_marker_state_is_durable_and_doctor_requires_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            marker = root / WRITER_MARKER_BASENAME
            self.assertEqual(MARKER_CLEAN_CONTENT, marker.read_bytes())
            with (
                mock.patch(
                    "agent_team.wal.os.fsync", side_effect=OSError("fsync unavailable")
                ),
                self.assertRaises(WalSidecarRecoveryRequiredError),
            ):
                WalSidecarController(root).cleanup(CheckpointRequest("TRUNCATE"))
            self.assertEqual(MARKER_PREPARED_CONTENT, marker.read_bytes())
            report = ReadOnlyDoctor(
                marker_name=WRITER_MARKER_BASENAME,
                ledger_name="recovery.ledger",
            ).inspect(root, "cleanup-marker")
            self.assertEqual("UNREADABLE", report.observed_state)
            with self.assertRaises(StoreUnavailableError):
                CoordinationStore(root)

    def test_peer_store_is_not_hidden_by_self_preflight(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            store_a = CoordinationStore(root)
            store_b = CoordinationStore(root)
            try:
                operation = store_a.create_intent(
                    "op-peer",
                    effect_key="effect/op-peer",
                    actor="main",
                    clock_ns=1,
                )
                report = RecoveryCoordinator(store_b).startup_preflight(
                    operation.operation_id
                )
                self.assertEqual("WRITER_ACTIVE", report.observed_state)
            finally:
                store_b.close()
                store_a.close()

    def test_failed_marker_reacquire_disables_store_operations(self) -> None:
        class ReacquireFailStore(CoordinationStore):
            calls = 0
            fail_after = 0

            @staticmethod
            def _lock_lifetime_gate_fd(
                gate_fd: int,
                *,
                exclusive: bool,
                busy_timeout_ms: int,
            ) -> None:
                ReacquireFailStore.calls += 1
                if (
                    ReacquireFailStore.fail_after
                    and ReacquireFailStore.calls >= ReacquireFailStore.fail_after
                    and not exclusive
                ):
                    raise StoreUnavailableError("injected marker reacquire failure")
                CoordinationStore._lock_lifetime_gate_fd(
                    gate_fd,
                    exclusive=exclusive,
                    busy_timeout_ms=busy_timeout_ms,
                )

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            store = ReacquireFailStore(root, busy_timeout_ms=20)
            ReacquireFailStore.fail_after = ReacquireFailStore.calls + 2
            try:
                with (
                    self.assertRaises(StoreUnavailableError),
                    store._marker_exclusive_probe(),
                ):
                    pass
                ReacquireFailStore.fail_after = 0
                with self.assertRaises(StoreUnavailableError):
                    store.operation("missing-operation")
            finally:
                store.close()

    def test_session_close_retries_one_shot_marker_fd_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence()
            marker_fd = session._resources.marker_fd
            original_close = os.close
            failed = True

            def close(fd: int) -> None:
                nonlocal failed
                if fd == marker_fd and failed:
                    failed = False
                    raise OSError("injected marker close failure")
                original_close(fd)

            try:
                with mock.patch("agent_team.wal.os.close", side_effect=close):
                    with self.assertRaises(WalSidecarRecoveryRequiredError):
                        session.close()
                    session.close()
            finally:
                session.close()
            with self.assertRaises(OSError):
                os.fstat(marker_fd)

    def test_store_close_retries_one_shot_marker_fd_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            store = CoordinationStore(root)
            marker_fd = store._marker_fd
            self.assertIsNotNone(marker_fd)
            original_close = os.close
            failed = True

            def close(fd: int) -> None:
                nonlocal failed
                if fd == marker_fd and failed:
                    failed = False
                    raise OSError("injected store marker close failure")
                original_close(fd)

            try:
                with mock.patch("agent_team.store.os.close", side_effect=close):
                    with self.assertRaises(StoreUnavailableError):
                        store.close()
                    store.close()
            finally:
                store.close()
            with self.assertRaises(OSError):
                os.fstat(marker_fd)  # type: ignore[arg-type]

    def test_store_close_retries_one_shot_connection_failure(self) -> None:
        class FakeConnection:
            failed = True

            def close(self) -> None:
                if self.failed:
                    self.failed = False
                    raise OSError("injected store connection close failure")

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            store = CoordinationStore(root)
            initial_connection = store._connection
            assert initial_connection is not None
            initial_connection.close()
            connection = FakeConnection()
            store._connection = connection  # type: ignore[assignment]
            try:
                store.close()
                self.assertIsNone(store._connection)
            finally:
                store.close()

    def test_store_close_retains_persistent_connection_failure_for_retry(self) -> None:
        class PersistentConnection:
            fail = True

            def close(self) -> None:
                if self.fail:
                    raise OSError("injected persistent connection close failure")

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            store = CoordinationStore(root)
            initial_connection = store._connection
            assert initial_connection is not None
            initial_connection.close()
            connection = PersistentConnection()
            store._connection = connection  # type: ignore[assignment]
            try:
                with self.assertRaises(StoreUnavailableError):
                    store.close()
                self.assertIs(connection, store._connection)
                connection.fail = False
                store.close()
                self.assertIsNone(store._connection)
            finally:
                store.close()

    def test_doctor_close_retries_one_shot_marker_fd_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=WRITER_MARKER_BASENAME,
                ledger_name="recovery.ledger",
            )
            marker_fd = filesystem._marker_fd
            self.assertIsNotNone(marker_fd)
            original_close = os.close
            failed = True

            def close(fd: int) -> None:
                nonlocal failed
                if fd == marker_fd and failed:
                    failed = False
                    raise OSError("injected doctor marker close failure")
                original_close(fd)

            try:
                with mock.patch("agent_team.doctor.os.close", side_effect=close):
                    with self.assertRaises(StateFilesystemError):
                        filesystem.close()
                    filesystem.close()
            finally:
                filesystem.close()
            with self.assertRaises(OSError):
                os.fstat(marker_fd)  # type: ignore[arg-type]

    def test_doctor_marker_handoff_reports_old_fd_close_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=WRITER_MARKER_BASENAME,
                ledger_name="recovery.ledger",
            )
            old_marker_fd = filesystem._marker_fd
            self.assertIsNotNone(old_marker_fd)
            original_close = os.close
            failed = True

            def close(fd: int) -> None:
                nonlocal failed
                if fd == old_marker_fd and failed:
                    failed = False
                    raise OSError("injected marker handoff close failure")
                original_close(fd)

            try:
                with mock.patch("agent_team.doctor.os.close", side_effect=close):
                    with self.assertRaises(StateFilesystemError):
                        filesystem.try_marker_exclusive()
                    self.assertEqual(old_marker_fd, filesystem._marker_fd)
            finally:
                filesystem.close()
            with self.assertRaises(OSError):
                os.fstat(old_marker_fd)  # type: ignore[arg-type]

    def test_open_resources_final_assertion_closes_every_descriptor(self) -> None:
        class FailingController(WalSidecarController):
            marker_fd: int | None = None

            def _assert_resources(self, resources: object) -> None:
                self.marker_fd = resources.marker_fd  # type: ignore[attr-defined]
                raise WalSidecarRecoveryRequiredError(
                    "injected final resource assertion failure"
                )

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = FailingController(root)
            original_close = os.close
            failed = True

            def close(fd: int) -> None:
                nonlocal failed
                if controller.marker_fd == fd and failed:
                    failed = False
                    raise OSError("injected open-resource close failure")
                original_close(fd)

            with (
                mock.patch("agent_team.wal.os.close", side_effect=close),
                self.assertRaises(WalSidecarRecoveryRequiredError),
            ):
                controller.hold_quiescence()
            self.assertIsNotNone(controller.marker_fd)
            with self.assertRaises(OSError):
                os.fstat(controller.marker_fd)  # type: ignore[arg-type]
            WalSidecarController(root).checkpoint(CheckpointRequest("PASSIVE"))

    def test_source_copy_closes_target_fd_after_one_shot_close_failure(self) -> None:
        class CaptureController(WalSidecarController):
            target_fd: int | None = None

            def _create_copy_target(
                self,
                resources: object,
                target: DatabaseCopyTarget,
            ) -> tuple[int, os.stat_result]:
                fd, metadata = super()._create_copy_target(resources, target)  # type: ignore[arg-type]
                self.target_fd = fd
                return fd, metadata

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = CaptureController(root)
            session = controller.hold_quiescence()
            original_close = os.close
            failed = True

            def close(fd: int) -> None:
                nonlocal failed
                if fd == controller.target_fd and failed:
                    failed = False
                    raise OSError("injected target close failure")
                original_close(fd)

            target = root / "snapshot"
            try:
                with (
                    mock.patch("agent_team.wal.os.close", side_effect=close),
                    self.assertRaises(WalSidecarRecoveryRequiredError),
                ):
                    session.copy_database_to(
                        CheckpointRequest("TRUNCATE"),
                        DatabaseCopyTarget(name=target.name),
                    )
            finally:
                session.close()
            self.assertIsNotNone(controller.target_fd)
            with self.assertRaises(OSError):
                os.fstat(controller.target_fd)  # type: ignore[arg-type]

    def test_source_copy_rechecks_target_bytes_after_before_result(self) -> None:
        class TamperController(WalSidecarController):
            target_fd: int | None = None

            def _create_copy_target(
                self,
                resources: object,
                target: DatabaseCopyTarget,
            ) -> tuple[int, os.stat_result]:
                fd, metadata = super()._create_copy_target(resources, target)  # type: ignore[arg-type]
                self.target_fd = fd
                return fd, metadata

            def _fault(self, point: str) -> None:
                if point == "before_result":
                    assert self.target_fd is not None
                    os.pwrite(self.target_fd, b"tampered", 0)

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = TamperController(root)
            session = controller.hold_quiescence()
            try:
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    session.copy_database_to(
                        CheckpointRequest("TRUNCATE"),
                        DatabaseCopyTarget(name="snapshot"),
                    )
            finally:
                session.close()

    def test_source_copy_retains_persistent_target_fd_for_session_retry(self) -> None:
        class CaptureController(WalSidecarController):
            target_fd: int | None = None

            def _create_copy_target(
                self,
                resources: object,
                target: DatabaseCopyTarget,
            ) -> tuple[int, os.stat_result]:
                fd, metadata = super()._create_copy_target(resources, target)  # type: ignore[arg-type]
                self.target_fd = fd
                return fd, metadata

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = CaptureController(root)
            session = controller.hold_quiescence()
            original_close = os.close
            failed = True

            def close(fd: int) -> None:
                if fd == controller.target_fd and failed:
                    raise OSError("injected persistent target close failure")
                original_close(fd)

            try:
                with mock.patch("agent_team.wal.os.close", side_effect=close):
                    with self.assertRaises(WalSidecarRecoveryRequiredError):
                        session.copy_database_to(
                            CheckpointRequest("TRUNCATE"),
                            DatabaseCopyTarget(name="snapshot"),
                        )
                    self.assertTrue(session._resources._orphan_fds)
                    failed = False
                    session.close()
            finally:
                session.close()
            self.assertIsNotNone(controller.target_fd)
            with self.assertRaises(OSError):
                os.fstat(controller.target_fd)  # type: ignore[arg-type]

    def test_root_nlink_is_part_of_quiescence_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence()
            try:
                (root / "unexpected-directory").mkdir(mode=0o700)
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    session.assert_identity()
            finally:
                session.close()

    def test_root_mode_change_is_not_learned_by_refresh(self) -> None:
        class ModeController(WalSidecarController):
            def _fault(self, point: str) -> None:
                if point == "after_sqlite_connect":
                    self.state_root.chmod(0o755)

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            with self.assertRaises(WalSidecarRecoveryRequiredError):
                ModeController(root).checkpoint(CheckpointRequest("PASSIVE"))
            self.assertEqual(0o755, stat.S_IMODE(root.stat().st_mode))

    def test_quiescence_source_copy_uses_typed_target_without_raw_source_access(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-copy",
                    effect_key="effect/op-copy",
                    actor="main",
                    clock_ns=1,
                )
            target = root / "coordination.sqlite3-copy"
            session = WalSidecarController(root).hold_quiescence()
            try:
                result = session.copy_database_to(
                    CheckpointRequest("TRUNCATE"),
                    DatabaseCopyTarget(name=target.name),
                )
                self.assertTrue(result.checkpoint.safe)
                self.assertEqual(
                    result.target_identity, (target.stat().st_dev, target.stat().st_ino)
                )
                self.assertEqual(
                    "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
                    result.digest,
                )
                self.assertFalse(
                    any(
                        (root / f"{target.name}{suffix}").exists()
                        for suffix in ("-wal", "-shm", "-journal")
                    )
                )
            finally:
                session.close()
            copied = sqlite3.connect(target)
            try:
                self.assertEqual(
                    1,
                    copied.execute(
                        "SELECT COUNT(*) FROM operations WHERE operation_id = 'op-copy'"
                    ).fetchone()[0],
                )
            finally:
                copied.close()

    def test_source_copy_rejects_any_existing_target_without_mutating_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            target = root / "provider.receipt"
            target.write_bytes(b"")
            target.chmod(0o600)
            original = target.read_bytes()
            session = WalSidecarController(root).hold_quiescence()
            try:
                with self.assertRaises(WalSidecarUnsafeError):
                    session.copy_database_to(
                        CheckpointRequest("TRUNCATE"),
                        DatabaseCopyTarget(name=target.name),
                    )
            finally:
                session.close()
            self.assertEqual(original, target.read_bytes())

    def test_source_copy_target_fd_survives_path_swap(self) -> None:
        class SwapTargetController(WalSidecarController):
            def _fault(self, point: str) -> None:
                if point == "before_copy_target_write":
                    target = self.state_root / "snapshot"
                    alternate = self.state_root / "snapshot.alternate"
                    hidden = self.state_root / "snapshot.hidden"
                    target.rename(hidden)
                    alternate.rename(target)
                elif point == "after_copy_target_write":
                    target = self.state_root / "snapshot"
                    alternate = self.state_root / "snapshot.alternate"
                    hidden = self.state_root / "snapshot.hidden"
                    target.rename(alternate)
                    hidden.rename(target)

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-target-swap",
                    effect_key="effect/target-swap",
                    actor="main",
                    clock_ns=1,
                )
            alternate = root / "snapshot.alternate"
            alternate.write_bytes(b"")
            alternate.chmod(0o600)
            target = root / "snapshot"
            session = SwapTargetController(root).hold_quiescence()
            try:
                result = session.copy_database_to(
                    CheckpointRequest("TRUNCATE"),
                    DatabaseCopyTarget(name=target.name),
                )
                self.assertEqual(result.size, target.stat().st_size)
            finally:
                session.close()
            copied = sqlite3.connect(target)
            try:
                self.assertEqual(
                    1,
                    copied.execute(
                        "SELECT COUNT(*) FROM operations "
                        "WHERE operation_id = 'op-target-swap'"
                    ).fetchone()[0],
                )
            finally:
                copied.close()
            self.assertEqual(b"", alternate.read_bytes())

    def test_source_copy_rejects_reserved_and_forged_target_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            ledger = root / "recovery.ledger"
            ledger.write_bytes(b"ledger")
            ledger.chmod(0o600)
            victim = root.parent / "victim-copy-target"
            victim.write_bytes(b"keep")
            victim.chmod(0o600)
            forged = object.__new__(DatabaseCopyTarget)
            object.__setattr__(forged, "name", "../victim-copy-target")
            session = WalSidecarController(root).hold_quiescence()
            try:
                with self.assertRaises(ValueError):
                    session.copy_database_to(CheckpointRequest("TRUNCATE"), forged)
                with self.assertRaises(ValueError):
                    DatabaseCopyTarget(name=ledger.name)
            finally:
                session.close()
            self.assertEqual(b"keep", victim.read_bytes())
            self.assertEqual(b"ledger", ledger.read_bytes())

    def test_source_copy_blocks_zero_frame_external_reader_without_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / "coordination.sqlite3"
            reader = sqlite3.connect(database, isolation_level=None)
            try:
                reader.execute("PRAGMA journal_mode=WAL")
                reader.execute("BEGIN")
                reader.execute("SELECT name FROM sqlite_master").fetchall()
                target = root / "snapshot"
                session = WalSidecarController(root).hold_quiescence()
                try:
                    with self.assertRaises(WalSidecarBusyError):
                        session.copy_database_to(
                            CheckpointRequest("TRUNCATE"),
                            DatabaseCopyTarget(name=target.name),
                        )
                finally:
                    session.close()
                self.assertFalse(target.exists())
                self.assertTrue((root / SHM_BASENAME).exists())
            finally:
                reader.rollback()
                reader.close()

    def test_source_copy_rejects_path_swap_to_alternate_database(self) -> None:
        if os.uname().sysname != "Darwin":
            self.skipTest("macOS F_GETPATH path-anchor regression")

        class SwapController(WalSidecarController):
            swapped = False

            def _fault(self, point: str) -> None:
                if point == "before_sqlite_connect" and not self.swapped:
                    self.swapped = True
                    database = self.state_root / "coordination.sqlite3"
                    saved = self.state_root / "coordination.sqlite3.saved"
                    alternate = self.state_root / "alternate.sqlite3"
                    database.rename(saved)
                    alternate.rename(database)
                elif point == "after_sqlite_connect" and self.swapped:
                    database = self.state_root / "coordination.sqlite3"
                    saved = self.state_root / "coordination.sqlite3.saved"
                    alternate = self.state_root / "alternate.sqlite3"
                    database.rename(alternate)
                    saved.rename(database)
                elif point == "after_source_copy" and self.swapped:
                    alternate_connection = sqlite3.connect(
                        self.state_root / "alternate.sqlite3",
                        isolation_level=None,
                    )
                    try:
                        alternate_connection.execute(
                            "UPDATE store_meta SET value = 999 "
                            "WHERE key = 'last_clock_ns'"
                        )
                    finally:
                        alternate_connection.close()

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-source-swap",
                    effect_key="effect/source-swap",
                    actor="main",
                    clock_ns=1,
                )
            database = root / "coordination.sqlite3"
            alternate = root / "alternate.sqlite3"
            alternate.write_bytes(database.read_bytes())
            alternate.chmod(0o600)
            target = root / "snapshot"
            session = SwapController(root).hold_quiescence()
            try:
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    session.copy_database_to(
                        CheckpointRequest("TRUNCATE"),
                        DatabaseCopyTarget(name=target.name),
                    )
            finally:
                session.close()
            self.assertFalse(target.exists())
            original_connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    1,
                    original_connection.execute(
                        "SELECT value FROM store_meta WHERE key = 'last_clock_ns'"
                    ).fetchone()[0],
                )
            finally:
                original_connection.close()

    def test_busy_checkpoint_rejects_path_swap_to_alternate_database(self) -> None:
        if os.uname().sysname != "Darwin":
            self.skipTest("macOS F_GETPATH path-anchor regression")

        class SwapController(WalSidecarController):
            raw_writer: sqlite3.Connection | None = None
            swapped = False

            def _fault(self, point: str) -> None:
                if point == "before_sqlite_connect" and not self.swapped:
                    self.swapped = True
                    database = self.state_root / "coordination.sqlite3"
                    saved = self.state_root / "coordination.sqlite3.saved"
                    alternate = self.state_root / "alternate.sqlite3"
                    database.rename(saved)
                    alternate.rename(database)
                elif point == "after_sqlite_connect" and self.swapped:
                    database = self.state_root / "coordination.sqlite3"
                    saved = self.state_root / "coordination.sqlite3.saved"
                    alternate = self.state_root / "alternate.sqlite3"
                    self.raw_writer = sqlite3.connect(
                        database,
                        isolation_level=None,
                        timeout=0,
                    )
                    self.raw_writer.execute("BEGIN IMMEDIATE")
                    database.rename(alternate)
                    saved.rename(database)

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-busy-source-swap",
                    effect_key="effect/busy-source-swap",
                    actor="main",
                    clock_ns=1,
                )
            database = root / "coordination.sqlite3"
            alternate = root / "alternate.sqlite3"
            alternate.write_bytes(database.read_bytes())
            alternate.chmod(0o600)
            alternate_connection = sqlite3.connect(alternate, isolation_level=None)
            try:
                alternate_connection.execute(
                    "UPDATE store_meta SET value = 999 WHERE key = 'last_clock_ns'"
                )
            finally:
                alternate_connection.close()
            controller = SwapController(root, busy_timeout_ms=20)
            try:
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    controller.checkpoint(CheckpointRequest("TRUNCATE"))
            finally:
                if controller.raw_writer is not None:
                    controller.raw_writer.rollback()
                    controller.raw_writer.close()

    def test_busy_checkpoint_identical_alternate_inode_is_not_published(self) -> None:
        if os.uname().sysname != "Darwin":
            self.skipTest("macOS F_GETPATH path-anchor regression")

        class SwapController(WalSidecarController):
            raw_writer: sqlite3.Connection | None = None
            swapped = False

            def _fault(self, point: str) -> None:
                if point == "before_sqlite_connect" and not self.swapped:
                    self.swapped = True
                    database = self.state_root / "coordination.sqlite3"
                    saved = self.state_root / "coordination.sqlite3.saved"
                    alternate = self.state_root / "alternate.sqlite3"
                    database.rename(saved)
                    alternate.rename(database)
                elif point == "after_sqlite_connect" and self.swapped:
                    database = self.state_root / "coordination.sqlite3"
                    saved = self.state_root / "coordination.sqlite3.saved"
                    alternate = self.state_root / "alternate.sqlite3"
                    self.raw_writer = sqlite3.connect(
                        database,
                        isolation_level=None,
                        timeout=0,
                    )
                    self.raw_writer.execute("BEGIN IMMEDIATE")
                    database.rename(alternate)
                    saved.rename(database)

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / "coordination.sqlite3"
            alternate = root / "alternate.sqlite3"
            alternate.write_bytes(database.read_bytes())
            alternate.chmod(0o600)
            controller = SwapController(root, busy_timeout_ms=20)
            try:
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    controller.checkpoint(CheckpointRequest("TRUNCATE"))
            finally:
                if controller.raw_writer is not None:
                    controller.raw_writer.rollback()
                    controller.raw_writer.close()

    def test_checkpoint_with_preexisting_canonical_wal_preserves_busy_tuple(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / "coordination.sqlite3"
            writer = sqlite3.connect(database, isolation_level=None)
            reader = sqlite3.connect(database, isolation_level=None)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute(
                    "UPDATE store_meta SET value = value + 1 "
                    "WHERE key = 'last_clock_ns'"
                )
                reader.execute("BEGIN")
                reader.execute("SELECT name FROM sqlite_master").fetchall()
                writer.execute(
                    "UPDATE store_meta SET value = value + 1 "
                    "WHERE key = 'last_clock_ns'"
                )
                result = WalSidecarController(root, busy_timeout_ms=20).checkpoint(
                    CheckpointRequest("TRUNCATE")
                )
                self.assertGreater(result.busy, 0)
                self.assertIs(type(result), CheckpointResult)
            finally:
                reader.rollback()
                reader.close()
                writer.close()

    def test_doctor_rejects_marker_path_swap_after_reader_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                operation = store.create_intent(
                    "op-marker-swap",
                    effect_key="effect/marker-swap",
                    actor="main",
                    clock_ns=1,
                )
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=WRITER_MARKER_BASENAME,
                ledger_name="recovery.ledger",
            )
            try:
                marker = root / WRITER_MARKER_BASENAME
                replacement = root / "writer.marker.old"
                marker.rename(replacement)
                marker.write_bytes(MARKER_CLEAN_CONTENT)
                marker.chmod(0o600)
                report = ReadOnlyDoctor(filesystem=filesystem).inspect(
                    root,
                    operation.operation_id,
                )
                self.assertEqual("UNREADABLE", report.observed_state)
                self.assertEqual("OPERATOR_REVIEW", report.safe_action)
            finally:
                filesystem.close()

    def test_doctor_rejects_marker_state_swap_after_reader_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                operation = store.create_intent(
                    "op-marker-state",
                    effect_key="effect/marker-state",
                    actor="main",
                    clock_ns=1,
                )
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=WRITER_MARKER_BASENAME,
                ledger_name="recovery.ledger",
            )
            try:
                marker = root / WRITER_MARKER_BASENAME
                marker.write_bytes(MARKER_PREPARED_CONTENT)
                report = ReadOnlyDoctor(filesystem=filesystem).inspect(
                    root,
                    operation.operation_id,
                )
                self.assertEqual("UNREADABLE", report.observed_state)
                self.assertEqual("OPERATOR_REVIEW", report.safe_action)
            finally:
                filesystem.close()

    def test_source_copy_closes_checkpoint_connection_before_target_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            target = root / "existing-copy-target"
            target.write_bytes(b"")
            target.chmod(0o600)
            session = WalSidecarController(root).hold_quiescence()
            try:
                with self.assertRaises(WalSidecarUnsafeError):
                    session.copy_database_to(
                        CheckpointRequest("PASSIVE"),
                        DatabaseCopyTarget(name=target.name),
                    )
                self.assertIsNone(session._resources.connection)
            finally:
                session.close()

    def test_session_checkpoint_closes_connection_on_checkpoint_failure(self) -> None:
        class FailingController(WalSidecarController):
            def _fault(self, point: str) -> None:
                if point == "before_checkpoint":
                    raise WalSidecarRecoveryRequiredError("injected checkpoint failure")

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = FailingController(root).hold_quiescence()
            try:
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    session.checkpoint(CheckpointRequest("PASSIVE"))
                self.assertIsNone(session._resources.connection)
            finally:
                session.close()

    def test_checkpoint_then_cleanup_can_share_one_quiescence_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence()
            try:
                session.checkpoint(CheckpointRequest("PASSIVE"))
                result = session.cleanup(CheckpointRequest("TRUNCATE"))
                self.assertEqual("CLEANED", result.outcome)
            finally:
                session.close()

    def test_passive_checkpoint_does_not_remove_nonzero_wal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / "coordination.sqlite3"
            writer = sqlite3.connect(database, isolation_level=None)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute(
                    "UPDATE store_meta SET value = value + 1 "
                    "WHERE key = 'last_clock_ns'"
                )
                wal = root / "coordination.sqlite3-wal"
                self.assertGreater(wal.stat().st_size, 0)
                result = WalSidecarController(root, busy_timeout_ms=20).cleanup(
                    CheckpointRequest("PASSIVE")
                )
                self.assertEqual("BLOCKED", result.outcome)
                self.assertEqual("WAL_PENDING", result.reason)
                self.assertTrue(wal.exists())
            finally:
                writer.close()

    def test_fsync_fault_is_recovery_required_before_marker_clean(self) -> None:
        class FailingController(WalSidecarController):
            def _fault(self, point: str) -> None:
                if point == "before_fsync":
                    raise WalSidecarRecoveryRequiredError("injected fsync uncertainty")

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            with self.assertRaises(WalSidecarRecoveryRequiredError):
                FailingController(root).cleanup(CheckpointRequest("TRUNCATE"))
            self.assertEqual(
                MARKER_PREPARED_CONTENT,
                (root / WRITER_MARKER_BASENAME).read_bytes(),
            )

    def test_directory_fsync_error_is_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            with (
                mock.patch(
                    "agent_team.wal.os.fsync",
                    side_effect=OSError("fsync unavailable"),
                ),
                self.assertRaises(WalSidecarRecoveryRequiredError),
            ):
                WalSidecarController(root).cleanup(CheckpointRequest("TRUNCATE"))
            self.assertEqual(
                MARKER_PREPARED_CONTENT,
                (root / WRITER_MARKER_BASENAME).read_bytes(),
            )

    def test_identity_is_checked_again_before_result_is_returned(self) -> None:
        class SwapController(WalSidecarController):
            def _fault(self, point: str) -> None:
                if point == "before_result":
                    target = self.state_root / WRITER_MARKER_BASENAME
                    replacement = self.state_root / "writer.marker-old"
                    target.rename(replacement)
                    target.write_bytes(b"replacement")
                    target.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            with self.assertRaises(WalSidecarRecoveryRequiredError):
                SwapController(root).checkpoint(CheckpointRequest("PASSIVE"))
            self.assertTrue((root / "writer.marker-old").exists())

    def test_active_sqlite_reader_reports_busy_without_unlinking_wal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / "coordination.sqlite3"
            writer = sqlite3.connect(database, isolation_level=None)
            reader = sqlite3.connect(database, isolation_level=None)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute(
                    "UPDATE store_meta SET value = value + 1 "
                    "WHERE key = 'last_clock_ns'"
                )
                reader.execute("BEGIN")
                self.assertEqual(
                    [(1,)],
                    reader.execute(
                        "SELECT value FROM store_meta WHERE key = 'last_clock_ns'"
                    ).fetchall(),
                )
                writer.execute(
                    "UPDATE store_meta SET value = value + 1 "
                    "WHERE key = 'last_clock_ns'"
                )
                wal = root / "coordination.sqlite3-wal"
                self.assertGreater(wal.stat().st_size, 0)

                result = WalSidecarController(root).cleanup(
                    CheckpointRequest("TRUNCATE")
                )

                self.assertEqual("BLOCKED", result.outcome)
                self.assertIsNone(result.checkpoint)
                self.assertEqual("WAL_PENDING", result.reason)
                self.assertTrue(wal.exists())
            finally:
                reader.rollback()
                reader.close()
                writer.close()

    def test_sigkill_barriers_preserve_marker_identity_and_reopenability(self) -> None:
        barriers = (
            "before_lifetime_lock",
            "after_lifetime_lock",
            "before_marker_lock",
            "after_marker_lock",
            "before_checkpoint",
            "after_checkpoint",
            "before_marker_prepare",
            "after_marker_prepare",
            "before_marker_prepare_fsync",
            "after_marker_prepare_fsync",
            "before_marker_fsync",
            "after_marker_fsync",
            "before_journal_delete",
            "after_journal_delete",
            "before_exclusive_lock",
            "after_exclusive_lock",
            "before_wal_reentry",
            "after_wal_reentry",
            "before_fsync",
            "after_fsync",
            "before_marker_clean",
            "before_marker_clean_fsync",
            "after_marker_clean_fsync",
            "after_marker_clean",
            "before_sqlite_close",
            "after_sqlite_close",
            "before_result",
            "after_result",
        )

        class KillController(WalSidecarController):
            def __init__(self, state_root: Path, barrier: str) -> None:
                self.barrier = barrier
                super().__init__(state_root, busy_timeout_ms=20)

            def _fault(self, point: str) -> None:
                if point == self.barrier:
                    os.kill(os.getpid(), signal.SIGKILL)

        for barrier in barriers:
            with (
                self.subTest(barrier=barrier),
                tempfile.TemporaryDirectory(prefix="agent-team-wal-kill-") as temporary,
            ):
                root = _make_root(temporary)
                with CoordinationStore(root):
                    pass
                marker = root / WRITER_MARKER_BASENAME
                identity = (marker.stat().st_dev, marker.stat().st_ino)
                child = os.fork()
                if child == 0:
                    try:
                        KillController(root, barrier).cleanup(
                            CheckpointRequest("TRUNCATE")
                        )
                    except _TEST_EXCEPTION:
                        os._exit(2)
                    os._exit(3)
                _, wait_status = os.waitpid(child, 0)
                self.assertEqual(
                    -signal.SIGKILL, os.waitstatus_to_exitcode(wait_status)
                )
                self.assertEqual(identity, (marker.stat().st_dev, marker.stat().st_ino))
                if barrier in {
                    "after_marker_prepare",
                    "before_marker_prepare_fsync",
                    "after_marker_prepare_fsync",
                    "before_marker_fsync",
                    "after_marker_fsync",
                    "before_journal_delete",
                    "after_journal_delete",
                    "before_exclusive_lock",
                    "after_exclusive_lock",
                    "before_wal_reentry",
                    "after_wal_reentry",
                    "before_fsync",
                    "after_fsync",
                    "before_marker_clean",
                }:
                    self.assertEqual(MARKER_PREPARED_CONTENT, marker.read_bytes())
                    report = ReadOnlyDoctor(
                        marker_name=WRITER_MARKER_BASENAME,
                        ledger_name="recovery.ledger",
                    ).inspect(root, "sigkill-marker")
                    self.assertEqual("UNREADABLE", report.observed_state)
                elif barrier in {
                    "before_marker_clean_fsync",
                    "after_marker_clean_fsync",
                    "before_sqlite_close",
                    "after_sqlite_close",
                    "before_result",
                    "after_result",
                }:
                    self.assertEqual(MARKER_CLEAN_CONTENT, marker.read_bytes())
                    with CoordinationStore(root):
                        pass
                else:
                    with CoordinationStore(root):
                        pass


if __name__ == "__main__":
    unittest.main()
