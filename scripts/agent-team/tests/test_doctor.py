from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import multiprocessing
import os
import queue
import sqlite3
import stat
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Literal, cast
from unittest import mock

from test_lease_provider import FakeClock, FakeProvider

import agent_team.doctor as doctor_module
from agent_team.backup import BackupArtifact, SQLiteBackup
from agent_team.doctor import (
    CleanupOwner,
    CleanupOwnerError,
    CleanupUncertaintyError,
    DoctorReport,
    FilesetInventory,
    FilesystemEntry,
    LedgerReadError,
    ReadOnlyDoctor,
    RecoveryLedgerReader,
    StateFilesystem,
    StateFilesystemError,
    UnsafeFilesystemError,
    UnstableSnapshotError,
)
from agent_team.doctor import doctor as run_doctor
from agent_team.lease import RecoveryFloor
from agent_team.recovery import (
    RECOVERY_TOMBSTONES_BASENAME,
    RecoveryLedgerRecord,
    RecoveryTombstoneRecord,
    RestoreIdentity,
    RestoreLedger,
    _encode_record,
    _encode_tombstone,
)
from agent_team.restore import BackupRestore, _candidate_basename
from agent_team.store import WRITER_MARKER_CLEAN_CONTENT, CoordinationStore
from agent_team.wal import WalSidecarController

MARKER_NAME = "writer.marker"
LEDGER_NAME = "recovery.ledger"
TOMBSTONE_NAME = RECOVERY_TOMBSTONES_BASENAME


def _make_root(temporary: str, name: str = "state") -> Path:
    root = Path(os.path.realpath(temporary)) / name
    root.mkdir(mode=0o700)
    return root


def _root_listing(root: Path) -> tuple[tuple[str, int, int, int, int, int, int], ...]:
    result: list[tuple[str, int, int, int, int, int, int]] = []
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        metadata = entry.lstat()
        result.append(
            (
                entry.name,
                stat.S_IFMT(metadata.st_mode),
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_size,
                metadata.st_mtime_ns,
            )
        )
    return tuple(result)


class _FailingSQLiteConnection(sqlite3.Connection):
    persistent_failure = True
    actual_close_then_error = False
    close_calls = 0

    def close(self) -> None:
        type(self).close_calls += 1
        if type(self).persistent_failure:
            raise OSError("simulated persistent SQLite connection close")
        if type(self).actual_close_then_error and type(self).close_calls == 1:
            super().close()
            raise OSError("simulated post-close SQLite connection error")
        super().close()


class _AttrlessBody(BaseException):
    __slots__ = ()

    def __getattribute__(self, name: str) -> object:
        if name == "__dict__":
            raise AttributeError(name)
        return super().__getattribute__(name)


def _failing_sqlite_connect(
    original_connect: Callable[..., sqlite3.Connection],
    connections: list[sqlite3.Connection],
) -> Callable[..., sqlite3.Connection]:
    return _sqlite_connect_with_factory(
        original_connect,
        connections,
        _FailingSQLiteConnection,
    )


def _sqlite_connect_with_factory(
    original_connect: Callable[..., sqlite3.Connection],
    connections: list[sqlite3.Connection],
    factory: type[sqlite3.Connection],
) -> Callable[..., sqlite3.Connection]:
    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = factory
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    return connect


class DoctorValueTest(unittest.TestCase):
    def test_ledger_reader_publishes_commit_only_after_final_identity_check(
        self,
    ) -> None:
        class FailingFilesystem(StateFilesystem):
            fail_final_identity = False

            def assert_identity(self, inventory: FilesetInventory) -> None:
                if self.fail_final_identity:
                    raise UnstableSnapshotError("injected final identity failure")
                super().assert_identity(inventory)

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            _write_restore_pair(root, terminal=True)
            filesystem = cast(
                FailingFilesystem,
                FailingFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ),
            )
            reader = RecoveryLedgerReader()
            filesystem.fail_final_identity = True
            try:
                with self.assertRaises(UnstableSnapshotError):
                    reader.read(filesystem)
                self.assertIsNone(reader.latest_committed)
                filesystem.fail_final_identity = False
                snapshot = reader.read(filesystem)
                self.assertIsNotNone(snapshot)
                self.assertIsNotNone(reader.latest_committed)
                assert reader.latest_committed is not None
                self.assertEqual("RESTORE_COMMITTED", reader.latest_committed.phase)
            finally:
                filesystem.close()

    def test_partial_open_generic_base_exception_keeps_cleanup_owner(self) -> None:
        class GenericFailure(BaseException):
            pass

        class FailingFilesystem(StateFilesystem):
            def _fault(self, point: str) -> None:
                if point == "after_marker_lstat":
                    raise GenericFailure("generic partial-open failure")

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_close = os.close
            original_flock = fcntl.flock
            gate_fd: int | None = None

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                nonlocal gate_fd
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == ".coordination-lifetime.lock":
                    gate_fd = fd
                return fd

            def fail_gate_close(fd: int) -> None:
                if fd == gate_fd:
                    raise OSError("persistent generic partial close failure")
                original_close(fd)

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if fd == gate_fd and operation & fcntl.LOCK_UN:
                    raise OSError("persistent generic partial unlock failure")
                original_flock(fd, operation)

            with (
                mock.patch(
                    "agent_team.doctor.os.open",
                    side_effect=capture_open,
                ),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_gate_close,
                ),
                mock.patch(
                    "agent_team.doctor.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                self.assertRaises(GenericFailure) as raised,
            ):
                FailingFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertIsNotNone(gate_fd)
            cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            cleanup_owner.retry_cleanup()

    def test_partial_open_attrless_exception_uses_typed_cleanup_wrapper(self) -> None:
        class AttrlessFailure(BaseException):
            __slots__ = ()

            def __getattribute__(self, name: str) -> object:
                if name == "__dict__":
                    raise AttributeError(name)
                return super().__getattribute__(name)

        class FailingFilesystem(StateFilesystem):
            def _fault(self, point: str) -> None:
                if point == "after_marker_lstat":
                    raise AttrlessFailure("attrless partial-open failure")

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_close = os.close
            original_flock = fcntl.flock
            gate_fd: int | None = None

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                nonlocal gate_fd
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == ".coordination-lifetime.lock":
                    gate_fd = fd
                return fd

            def fail_gate_close(fd: int) -> None:
                if fd == gate_fd:
                    raise OSError("persistent attrless partial close failure")
                original_close(fd)

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if fd == gate_fd and operation & fcntl.LOCK_UN:
                    raise OSError("persistent attrless partial unlock failure")
                original_flock(fd, operation)

            with (
                mock.patch(
                    "agent_team.doctor.os.open",
                    side_effect=capture_open,
                ),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_gate_close,
                ),
                mock.patch(
                    "agent_team.doctor.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                self.assertRaises(CleanupOwnerError) as raised,
            ):
                FailingFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertIsInstance(raised.exception.__cause__, AttrlessFailure)
            cleanup_owner = raised.exception.cleanup_owner
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            cleanup_owner.retry_cleanup()

    def test_filesystem_context_preserves_body_error_when_close_fails(self) -> None:
        class BodyError(RuntimeError):
            pass

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            gate_fd = filesystem._gate_fd
            self.assertIsNotNone(gate_fd)
            assert gate_fd is not None
            original_close = os.close
            original_flock = fcntl.flock

            def fail_gate_close(fd: int) -> None:
                if fd == gate_fd:
                    raise OSError("persistent context close failure")
                original_close(fd)

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if fd == gate_fd and operation & fcntl.LOCK_UN:
                    raise OSError("persistent context unlock failure")
                original_flock(fd, operation)

            with (
                mock.patch("agent_team.doctor.os.close", side_effect=fail_gate_close),
                mock.patch(
                    "agent_team.doctor.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                self.assertRaises(BodyError) as raised,
                filesystem,
            ):
                raise BodyError("body must remain primary")
            cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            cleanup_owner.retry_cleanup()

    def test_digest_preserves_body_error_when_temporary_close_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            metadata = filesystem._lstat(_database_name())
            self.assertIsNotNone(metadata)
            assert metadata is not None
            original_open = os.open
            original_close = os.close
            original_read = os.read
            opened: list[int] = []

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == _database_name():
                    opened.append(fd)
                return fd

            def fail_read(fd: int, size: int) -> bytes:
                if fd in opened:
                    raise OSError("digest body failure")
                return original_read(fd, size)

            def fail_close(fd: int) -> None:
                if fd in opened:
                    raise OSError("persistent digest close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                    mock.patch("agent_team.doctor.os.read", side_effect=fail_read),
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_close,
                    ),
                    self.assertRaises(StateFilesystemError) as raised,
                ):
                    filesystem._digest_regular(_database_name(), metadata)
                self.assertEqual("state entry cannot be read", str(raised.exception))
                cleanup_owner = raised.exception.cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                cleanup_owner.retry_cleanup()
            finally:
                filesystem.close()

    def test_ledger_reader_preserves_body_error_when_temporary_close_fails(
        self,
    ) -> None:
        class CaptureFilesystem(StateFilesystem):
            ledger_fd: int | None = None

            def open_existing_regular(self, name: str) -> int:
                fd = super().open_existing_regular(name)
                if name == LEDGER_NAME:
                    self.ledger_fd = fd
                return fd

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            ledger = root / LEDGER_NAME
            digest = "sha256:" + "a" * 64
            ledger.write_text(
                '{"version":1,"sequence":1,"phase":"RESTORE_PREPARED",'
                f'"restore_generation":1,"recovery_epoch":1,"fencing_token_floor":1,"backup_digest":"{digest}",'
                '"actor":"operator","audit_ref":"audit/1"}\n',
                encoding="utf-8",
            )
            ledger.chmod(0o600)
            filesystem = cast(
                CaptureFilesystem,
                CaptureFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ),
            )
            original_read = os.read
            original_close = os.close

            def fail_read(fd: int, size: int) -> bytes:
                if fd == filesystem.ledger_fd:
                    raise OSError("ledger body failure")
                return original_read(fd, size)

            def fail_close(fd: int) -> None:
                if fd == filesystem.ledger_fd:
                    raise OSError("persistent ledger close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.doctor.os.read", side_effect=fail_read),
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_close,
                    ),
                    self.assertRaises(LedgerReadError) as raised,
                ):
                    RecoveryLedgerReader().read(filesystem)
                cleanup_owner = raised.exception.cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                cleanup_owner.retry_cleanup()
            finally:
                filesystem.close()

    def test_internal_doctor_preserves_unhandled_body_when_finally_close_fails(
        self,
    ) -> None:
        class BodyError(RuntimeError):
            pass

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_close = os.close
            original_flock = fcntl.flock
            gate_fds: list[int] = []

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == ".coordination-lifetime.lock":
                    gate_fds.append(fd)
                return fd

            def fail_gate_close(fd: int) -> None:
                if fd in gate_fds:
                    raise OSError("persistent internal final close failure")
                original_close(fd)

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if fd in gate_fds and operation & fcntl.LOCK_UN:
                    raise OSError("persistent internal final unlock failure")
                original_flock(fd, operation)

            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_gate_close,
                ),
                mock.patch(
                    "agent_team.doctor.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                mock.patch.object(
                    StateFilesystem,
                    "inventory",
                    side_effect=BodyError("body must remain primary"),
                ),
                self.assertRaises(BodyError) as raised,
            ):
                ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-identity-mismatch")
            cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            cleanup_owner.retry_cleanup()

    def test_marker_handoff_respects_orphan_registry_capacity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_open = os.open
            original_close = os.close
            marker_fd = filesystem._marker_fd
            self.assertIsNotNone(marker_fd)
            assert marker_fd is not None
            marker_fds: list[int] = [marker_fd]

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == MARKER_NAME:
                    marker_fds.append(fd)
                return fd

            def fail_marker_close(fd: int) -> None:
                if fd in marker_fds:
                    raise OSError("persistent marker handoff close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_marker_close,
                    ),
                    mock.patch("agent_team.doctor._MAX_ORPHAN_FDS", 0),
                    self.assertRaises(StateFilesystemError),
                ):
                    filesystem.try_marker_exclusive()
                self.assertEqual([], filesystem._orphan_fds)
            finally:
                filesystem.close()

    def test_marker_handoff_eagain_retains_new_fd_close_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_open = os.open
            original_close = os.close
            original_flock = fcntl.flock
            old_marker_fd = filesystem._marker_fd
            self.assertIsNotNone(old_marker_fd)
            assert old_marker_fd is not None
            marker_fds: list[int] = [old_marker_fd]
            new_marker_fd: int | None = None

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                nonlocal new_marker_fd
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == MARKER_NAME:
                    marker_fds.append(fd)
                    new_marker_fd = fd
                return fd

            def fail_new_close(fd: int) -> None:
                if new_marker_fd is not None and fd == new_marker_fd:
                    raise OSError("persistent EAGAIN handoff close failure")
                original_close(fd)

            def reject_new_lock(fd: int, operation: int) -> None:
                if (
                    new_marker_fd is not None
                    and fd == new_marker_fd
                    and operation & fcntl.LOCK_EX
                ):
                    raise BlockingIOError(errno.EAGAIN, "marker is busy")
                original_flock(fd, operation)

            try:
                with (
                    mock.patch(
                        "agent_team.doctor.os.open",
                        side_effect=capture_open,
                    ),
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_new_close,
                    ),
                    mock.patch(
                        "agent_team.doctor.fcntl.flock",
                        side_effect=reject_new_lock,
                    ),
                ):
                    with self.assertRaises(StateFilesystemError):
                        filesystem.try_marker_exclusive()
                    self.assertTrue(new_marker_fd is not None)
                    self.assertEqual(1, len(filesystem._orphan_fds))
                    with self.assertRaises(StateFilesystemError):
                        filesystem.inventory()
                filesystem.inventory()
                self.assertEqual([], filesystem._orphan_fds)
            finally:
                for fd in marker_fds:
                    try:
                        original_close(fd)
                    except OSError:
                        pass
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_marker_handoff_pending_state_blocks_until_shared_lock_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            marker_fd = filesystem._marker_fd
            self.assertIsNotNone(marker_fd)
            assert marker_fd is not None
            original_close = os.close
            original_flock = fcntl.flock
            failed_old_close = False
            reject_reacquire = False

            def fail_old_close_once(fd: int) -> None:
                nonlocal failed_old_close
                if fd == marker_fd and not failed_old_close:
                    failed_old_close = True
                    raise OSError("one-shot old marker close failure")
                original_close(fd)

            def reject_shared_reacquire(fd: int, operation: int) -> None:
                if reject_reacquire and fd == marker_fd and operation & fcntl.LOCK_SH:
                    raise BlockingIOError(errno.EAGAIN, "marker reacquire is busy")
                original_flock(fd, operation)

            try:
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_old_close_once,
                    ),
                    self.assertRaises(StateFilesystemError),
                ):
                    filesystem.try_marker_exclusive()
                self.assertFalse(filesystem._marker_shared)
                self.assertFalse(filesystem._marker_exclusive)
                self.assertEqual([], filesystem._orphan_fds)
                reject_reacquire = True
                with (
                    mock.patch(
                        "agent_team.doctor.fcntl.flock",
                        side_effect=reject_shared_reacquire,
                    ),
                    self.assertRaises(StateFilesystemError),
                ):
                    filesystem.inventory()
                reject_reacquire = False
                filesystem.inventory()
                self.assertTrue(filesystem._marker_shared)
            finally:
                filesystem.close()

    def test_marker_post_lock_state_failure_retains_exclusive_fd_for_retry(
        self,
    ) -> None:
        from agent_team import store as store_module

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            old_marker_fd = filesystem._marker_fd
            self.assertIsNotNone(old_marker_fd)
            assert old_marker_fd is not None
            original_open = os.open
            original_close = os.close
            original_read_state = store_module._read_writer_marker_state
            new_marker_fds: list[int] = []

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == MARKER_NAME:
                    new_marker_fds.append(fd)
                return fd

            def fail_state_read(fd: int) -> str:
                if fd in new_marker_fds:
                    raise StateFilesystemError("post-lock marker state failure")
                return original_read_state(fd)

            def fail_new_close(fd: int) -> None:
                if fd in new_marker_fds:
                    raise OSError("persistent post-lock marker close failure")
                original_close(fd)

            try:
                with (
                    mock.patch(
                        "agent_team.doctor.os.open",
                        side_effect=capture_open,
                    ),
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_new_close,
                    ),
                    mock.patch.object(
                        store_module,
                        "_read_writer_marker_state",
                        side_effect=fail_state_read,
                    ),
                    self.assertRaises(StateFilesystemError) as raised,
                ):
                    filesystem.try_marker_exclusive()
                self.assertTrue(new_marker_fds)
                self.assertEqual(1, len(filesystem._orphan_fds))
                cleanup_owner = raised.exception.cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_new_close,
                    ),
                    self.assertRaises(StateFilesystemError),
                ):
                    cleanup_owner.retry_cleanup()
                cleanup_owner.retry_cleanup()
                with self.assertRaises(OSError):
                    os.fstat(new_marker_fds[0])
                probe_fd = os.open(root / MARKER_NAME, os.O_RDONLY)
                try:
                    fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(probe_fd, fcntl.LOCK_UN)
                finally:
                    original_close(probe_fd)
            finally:
                for fd in new_marker_fds:
                    try:
                        original_close(fd)
                    except OSError:
                        pass
                filesystem.close()

    def test_orphan_registry_deduplicates_and_enforces_capacity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            fd = os.open(root / _database_name(), os.O_RDONLY)
            metadata = os.fstat(fd)
            identity = (metadata.st_dev, metadata.st_ino)
            filesystem._orphan_fds.append((fd, identity, "existing"))
            replacement_fd = os.open(root / _database_name(), os.O_RDONLY)
            replacement_metadata = os.fstat(replacement_fd)
            try:
                filesystem._remember_orphan_fd(fd, identity, "duplicate")
                self.assertEqual(1, len(filesystem._orphan_fds))
                with (
                    mock.patch("agent_team.doctor._MAX_ORPHAN_FDS", 1),
                    self.assertRaises(StateFilesystemError),
                ):
                    filesystem._remember_orphan_fd(
                        replacement_fd,
                        (replacement_metadata.st_dev, replacement_metadata.st_ino),
                        "capacity",
                    )
                self.assertEqual(1, len(filesystem._orphan_fds))
            finally:
                original_close = os.close
                original_close(fd)
                original_close(replacement_fd)
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_partial_open_failure_keeps_primary_and_cleanup_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            marker = root / MARKER_NAME
            marker.write_bytes(b"malformed marker")
            marker.chmod(0o600)
            original_open = os.open
            original_close = os.close
            original_flock = fcntl.flock
            gate_fd: int | None = None

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                nonlocal gate_fd
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == ".coordination-lifetime.lock":
                    gate_fd = fd
                return fd

            def fail_gate_close(fd: int) -> None:
                if fd == gate_fd:
                    raise OSError("persistent partial-open gate close failure")
                original_close(fd)

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if fd == gate_fd and operation & fcntl.LOCK_UN:
                    raise OSError("persistent partial-open gate unlock failure")
                original_flock(fd, operation)

            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_gate_close,
                ),
                mock.patch(
                    "agent_team.doctor.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                self.assertRaises(UnsafeFilesystemError) as raised,
            ):
                StateFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertIsNotNone(gate_fd)
            error = raised.exception
            cleanup_owner = getattr(error, "cleanup_owner", None)
            self.assertIsNotNone(cleanup_owner)
            retry_cleanup = getattr(cleanup_owner, "retry_cleanup", None)
            self.assertTrue(callable(retry_cleanup))
            assert callable(retry_cleanup)
            with (
                mock.patch("agent_team.doctor.os.close", side_effect=fail_gate_close),
                mock.patch(
                    "agent_team.doctor.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                self.assertRaises(StateFilesystemError),
            ):
                retry_cleanup()
            retry_cleanup()

    def test_partial_gate_stat_failure_keeps_fd_reachable_for_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_stat = os.stat
            original_close = os.close
            gate_fds: list[int] = []
            gate_stat_calls = 0
            close_snapshots: list[tuple[int, int | None, tuple[int, int] | None]] = []

            class ObservingFilesystem(StateFilesystem):
                def _close_owned_temporary_fd(
                    self,
                    fd: int,
                    expected_identity: tuple[int, int] | None,
                    label: str,
                ) -> None:
                    if label == "lifetime gate":
                        close_snapshots.append((fd, self._gate_fd, self._gate_identity))
                    super()._close_owned_temporary_fd(fd, expected_identity, label)

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == ".coordination-lifetime.lock":
                    gate_fds.append(fd)
                return fd

            def fail_gate_stat(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> os.stat_result:
                nonlocal gate_stat_calls
                if path == ".coordination-lifetime.lock":
                    gate_stat_calls += 1
                    if gate_stat_calls >= 2:
                        raise OSError("gate validation failure")
                return original_stat(path, *args, **kwargs)  # type: ignore[arg-type]

            def fail_gate_close(fd: int) -> None:
                if fd in gate_fds:
                    raise OSError("persistent gate close failure")
                original_close(fd)

            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch("agent_team.doctor.os.stat", side_effect=fail_gate_stat),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_gate_close,
                ),
                self.assertRaises(OSError) as raised,
            ):
                ObservingFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertEqual(1, len(gate_fds))
            self.assertEqual(1, len(close_snapshots))
            self.assertEqual(gate_fds[0], close_snapshots[0][1])
            cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            with (
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_gate_close,
                ),
                self.assertRaises(StateFilesystemError),
            ):
                cleanup_owner.retry_cleanup()
            self.assertTrue(gate_fds)
            # The open-existing cleanup owner must retain the local gate fd even
            # though it was never published as a held gate on the instance.
            cleanup_owner.retry_cleanup()
            with self.assertRaises(OSError):
                os.fstat(gate_fds[0])

    def test_partial_marker_content_failure_keeps_locked_fd_for_retry(self) -> None:
        from agent_team import store as store_module

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_close = os.close
            original_flock = fcntl.flock
            marker_fds: list[int] = []
            close_snapshots: list[
                tuple[int, int | None, tuple[int, int] | None, bool]
            ] = []

            class ObservingFilesystem(StateFilesystem):
                def _close_owned_temporary_fd(
                    self,
                    fd: int,
                    expected_identity: tuple[int, int] | None,
                    label: str,
                ) -> None:
                    if label == "writer marker":
                        close_snapshots.append(
                            (
                                fd,
                                self._marker_fd,
                                self._marker_identity,
                                self._marker_shared,
                            )
                        )
                    super()._close_owned_temporary_fd(fd, expected_identity, label)

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == MARKER_NAME:
                    marker_fds.append(fd)
                return fd

            def fail_marker_close(fd: int) -> None:
                if fd in marker_fds:
                    raise OSError("persistent marker close failure")
                original_close(fd)

            def fail_marker_unlock(fd: int, operation: int) -> None:
                if fd in marker_fds and operation & fcntl.LOCK_UN:
                    raise OSError("persistent marker unlock failure")
                original_flock(fd, operation)

            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch(
                    "agent_team.doctor._store._read_writer_marker_state",
                    side_effect=store_module.StoreError("marker body failure"),
                ),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_marker_close,
                ),
                mock.patch(
                    "agent_team.doctor.fcntl.flock",
                    side_effect=fail_marker_unlock,
                ),
                self.assertRaises(UnsafeFilesystemError) as raised,
            ):
                ObservingFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertEqual(1, len(marker_fds))
            self.assertEqual(1, len(close_snapshots))
            self.assertEqual(marker_fds[0], close_snapshots[0][1])
            self.assertTrue(close_snapshots[0][3])
            cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            with (
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_marker_close,
                ),
                self.assertRaises(StateFilesystemError),
            ):
                cleanup_owner.retry_cleanup()
            self.assertTrue(marker_fds)
            cleanup_owner.retry_cleanup()
            with self.assertRaises(OSError):
                os.fstat(marker_fds[0])

    def test_partial_gate_attrless_fstat_keeps_fd_owner_for_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_fstat = os.fstat
            original_close = os.close
            gate_fds: list[int] = []

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == ".coordination-lifetime.lock":
                    gate_fds.append(fd)
                return fd

            def fail_gate_fstat(fd: int) -> os.stat_result:
                if fd in gate_fds:
                    raise _AttrlessBody("gate fstat failure")
                return original_fstat(fd)

            def fail_gate_close(fd: int) -> None:
                if fd in gate_fds:
                    raise OSError("gate close after attrless fstat")
                original_close(fd)

            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch("agent_team.doctor.os.fstat", side_effect=fail_gate_fstat),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_gate_close,
                ),
                self.assertRaises(BaseException) as raised,
            ):
                StateFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertIsInstance(raised.exception, CleanupOwnerError)
            self.assertIsInstance(raised.exception.__cause__, _AttrlessBody)
            cleanup_owner = cast(CleanupOwnerError, raised.exception).cleanup_owner
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            cleanup_owner.retry_cleanup()
            self.assertTrue(gate_fds)
            with self.assertRaises(OSError):
                os.fstat(gate_fds[0])

    def test_open_state_root_adopts_lower_store_cleanup_owner(self) -> None:
        from agent_team import store as store_module

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            retries: list[bool] = []
            lower_owner = store_module._CleanupCapability(lambda: retries.append(True))
            lower_error = store_module.StoreError("lower root open failure")
            lower_error._attach_cleanup_capability(lower_owner)
            with (
                mock.patch(
                    "agent_team.doctor._store._open_state_root",
                    side_effect=lower_error,
                ),
                self.assertRaises(StateFilesystemError) as raised,
            ):
                StateFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            cleanup_owner = raised.exception.cleanup_owner
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            cleanup_owner.retry_cleanup()
            self.assertEqual([True], retries)

    def test_partial_marker_attrless_fstat_keeps_fd_owner_for_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_fstat = os.fstat
            original_close = os.close
            marker_fds: list[int] = []

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == MARKER_NAME:
                    marker_fds.append(fd)
                return fd

            def fail_marker_fstat(fd: int) -> os.stat_result:
                if fd in marker_fds:
                    raise _AttrlessBody("marker fstat failure")
                return original_fstat(fd)

            def fail_marker_close(fd: int) -> None:
                if fd in marker_fds:
                    raise OSError("marker close after attrless fstat")
                original_close(fd)

            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch(
                    "agent_team.doctor.os.fstat",
                    side_effect=fail_marker_fstat,
                ),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_marker_close,
                ),
                self.assertRaises(BaseException) as raised,
            ):
                StateFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertIsInstance(raised.exception, CleanupOwnerError)
            self.assertIsInstance(raised.exception.__cause__, _AttrlessBody)
            cleanup_owner = cast(CleanupOwnerError, raised.exception).cleanup_owner
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            cleanup_owner.retry_cleanup()
            self.assertTrue(marker_fds)
            with self.assertRaises(OSError):
                os.fstat(marker_fds[0])

    def test_temporary_fd_attrless_status_probe_is_typed_and_owned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            fd = os.open(root / _database_name(), os.O_RDONLY)
            metadata = os.fstat(fd)
            identity = (metadata.st_dev, metadata.st_ino)
            original_fstat = os.fstat
            close_calls = 0

            def fail_fstat(target: int) -> os.stat_result:
                if target == fd:
                    raise _AttrlessBody("temporary status probe failure")
                return original_fstat(target)

            original_close = os.close

            def count_close(target: int) -> None:
                nonlocal close_calls
                if target == fd:
                    close_calls += 1
                original_close(target)

            try:
                with (
                    mock.patch("agent_team.doctor.os.fstat", side_effect=fail_fstat),
                    mock.patch("agent_team.doctor.os.close", side_effect=count_close),
                    self.assertRaises(CleanupUncertaintyError) as raised,
                ):
                    filesystem._close_owned_temporary_fd(
                        fd,
                        identity,
                        "temporary status probe",
                    )
                self.assertEqual(0, close_calls)
                cleanup_owner = raised.exception.cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                with mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=count_close,
                ):
                    cleanup_owner.retry_cleanup()
                self.assertEqual(1, close_calls)
                self.assertEqual([], filesystem._orphan_fds)
            finally:
                filesystem._orphan_fds.clear()
                try:
                    original_close(fd)
                except OSError:
                    pass
                filesystem.close()

    def test_marker_status_attrless_fstat_is_typed_and_owned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            marker_fd = filesystem._marker_fd
            self.assertIsNotNone(marker_fd)
            assert marker_fd is not None
            original_fstat = os.fstat

            def fail_marker_fstat(fd: int) -> os.stat_result:
                if fd == marker_fd:
                    raise _AttrlessBody("marker status probe failure")
                return original_fstat(fd)

            try:
                with (
                    mock.patch(
                        "agent_team.doctor.os.fstat",
                        side_effect=fail_marker_fstat,
                    ),
                    self.assertRaises(BaseException) as raised,
                ):
                    filesystem.inventory()
                self.assertIsInstance(raised.exception, CleanupUncertaintyError)
                cleanup_owner = cast(
                    CleanupUncertaintyError,
                    raised.exception,
                ).cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                cleanup_owner.retry_cleanup()
            finally:
                filesystem.close()

    def test_temporary_fd_identity_reuse_is_never_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            fd = os.open(root / _database_name(), os.O_RDONLY)
            original_close = os.close
            replacement_source: int | None = None
            try:
                original_close(fd)
                replacement_source = os.open(root / MARKER_NAME, os.O_RDONLY)
                if replacement_source != fd:
                    os.dup2(replacement_source, fd)
                    original_close(replacement_source)
                replacement_source = None
                with self.assertRaises(StateFilesystemError):
                    filesystem._close_owned_temporary_fd(
                        fd,
                        (
                            os.stat(root / _database_name()).st_dev,
                            os.stat(root / _database_name()).st_ino,
                        ),
                        "temporary reused",
                    )
                self.assertEqual([], filesystem._orphan_fds)
                filesystem.inventory()
                filesystem.close()
                os.fstat(fd)
            finally:
                if replacement_source is not None:
                    original_close(replacement_source)
                try:
                    original_close(fd)
                except OSError:
                    pass
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_root_status_attrless_fstat_is_typed_and_owned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            root_fd = filesystem._root_fd
            self.assertIsNotNone(root_fd)
            assert root_fd is not None
            original_fstat = os.fstat

            def fail_root_fstat(fd: int) -> os.stat_result:
                if fd == root_fd:
                    raise _AttrlessBody("root status probe failure")
                return original_fstat(fd)

            try:
                with (
                    mock.patch(
                        "agent_team.doctor.os.fstat",
                        side_effect=fail_root_fstat,
                    ),
                    self.assertRaises(BaseException) as raised,
                ):
                    filesystem.inventory()
                self.assertIsInstance(raised.exception, CleanupUncertaintyError)
                cleanup_owner = cast(
                    CleanupUncertaintyError,
                    raised.exception,
                ).cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                cleanup_owner.retry_cleanup()
            finally:
                filesystem.close()

    def test_doctor_database_retain_overflow_keeps_current_fd_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_connect = sqlite3.connect
            original_open = os.open
            original_fstat = os.fstat
            original_close = os.close
            connections: list[sqlite3.Connection] = []
            database_fds: list[int] = []
            filler_fds: list[int] = []

            class OverflowConnection(sqlite3.Connection):
                persistent_failure = True
                inject_callback: Callable[[], None] | None = None

                def close(self) -> None:
                    callback = type(self).inject_callback
                    if callback is not None:
                        type(self).inject_callback = None
                        callback()
                    if type(self).persistent_failure:
                        raise OSError("persistent overflow connection close")
                    super().close()

            class OverflowFilesystem(StateFilesystem):
                def open_existing_regular(self, name: str) -> int:
                    fd = super().open_existing_regular(name)
                    if name == _database_name():
                        database_fds.append(fd)
                    return fd

            filesystem = OverflowFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )

            def fill_registry() -> None:
                for index in range(8):
                    filler_fd = original_open(
                        root / _database_name(),
                        os.O_RDONLY,
                    )
                    filler_metadata = original_fstat(filler_fd)
                    filler_fds.append(filler_fd)
                    filesystem._orphan_fds.append(
                        (
                            filler_fd,
                            (filler_metadata.st_dev, filler_metadata.st_ino),
                            f"overflow filler {index}",
                        )
                    )

            OverflowConnection.inject_callback = fill_registry

            def fail_database_close(fd: int) -> None:
                if database_fds and fd == database_fds[-1]:
                    raise OSError("database retain registry overflow")
                original_close(fd)

            OverflowConnection.persistent_failure = True
            try:
                with (
                    mock.patch(
                        "agent_team.doctor.sqlite3.connect",
                        side_effect=_sqlite_connect_with_factory(
                            original_connect,
                            connections,
                            OverflowConnection,
                        ),
                    ),
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_database_close,
                    ),
                    self.assertRaises(CleanupUncertaintyError) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                self.assertEqual(8, len(filesystem._orphan_fds))
                cleanup_owner = raised.exception.cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                self.assertEqual(3, len(cleanup_owner._members))
                OverflowConnection.persistent_failure = False
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_database_close,
                    ),
                    self.assertRaises(CleanupUncertaintyError),
                ):
                    cleanup_owner.retry_cleanup()
                self.assertEqual([], filesystem._orphan_fds)
                self.assertTrue(database_fds)
                os.fstat(database_fds[-1])
                cleanup_owner.retry_cleanup()
                with self.assertRaises(OSError):
                    os.fstat(database_fds[-1])
            finally:
                OverflowConnection.persistent_failure = False
                OverflowConnection.inject_callback = None
                for filler_fd in filler_fds:
                    try:
                        original_close(filler_fd)
                    except OSError:
                        pass
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_recovery_ledger_retain_overflow_keeps_current_fd_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            _write_restore_pair(root, terminal=True)
            original_open = os.open
            original_fstat = os.fstat
            original_close = os.close
            ledger_fds: list[int] = []
            filler_fds: list[int] = []

            class OverflowFilesystem(StateFilesystem):
                injected = False

                def open_existing_regular(self, name: str) -> int:
                    fd = super().open_existing_regular(name)
                    if name == LEDGER_NAME:
                        ledger_fds.append(fd)
                        if not self.injected:
                            self.injected = True
                            for index in range(8):
                                filler_fd = original_open(
                                    root / _database_name(),
                                    os.O_RDONLY,
                                )
                                filler_metadata = original_fstat(filler_fd)
                                filler_fds.append(filler_fd)
                                self._orphan_fds.append(
                                    (
                                        filler_fd,
                                        (
                                            filler_metadata.st_dev,
                                            filler_metadata.st_ino,
                                        ),
                                        f"ledger overflow filler {index}",
                                    )
                                )
                    return fd

            filesystem = OverflowFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )

            def fail_ledger_close(fd: int) -> None:
                if ledger_fds and fd == ledger_fds[-1]:
                    raise OSError("ledger retain registry overflow")
                original_close(fd)

            try:
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_ledger_close,
                    ),
                    self.assertRaises(CleanupUncertaintyError) as raised,
                ):
                    RecoveryLedgerReader().read(filesystem, ledger_name=LEDGER_NAME)
                self.assertEqual(8, len(filesystem._orphan_fds))
                cleanup_owner = raised.exception.cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                self.assertEqual(2, len(cleanup_owner._members))
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_ledger_close,
                    ),
                    self.assertRaises(CleanupUncertaintyError),
                ):
                    cleanup_owner.retry_cleanup()
                self.assertEqual([], filesystem._orphan_fds)
                self.assertTrue(ledger_fds)
                os.fstat(ledger_fds[-1])
                cleanup_owner.retry_cleanup()
                with self.assertRaises(OSError):
                    os.fstat(ledger_fds[-1])
            finally:
                for filler_fd in filler_fds:
                    try:
                        original_close(filler_fd)
                    except OSError:
                        pass
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_marker_tamper_during_handoff_invalidates_filesystem_readiness(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            before = filesystem.inventory()
            marker_fd = filesystem._marker_fd
            self.assertIsNotNone(marker_fd)
            assert marker_fd is not None
            original_flock = fcntl.flock
            tampered = False

            def unlock_then_tamper(fd: int, operation: int) -> None:
                nonlocal tampered
                original_flock(fd, operation)
                if fd == marker_fd and operation & fcntl.LOCK_UN and not tampered:
                    tampered = True
                    (root / MARKER_NAME).write_bytes(b'{"version":1,"state":"TAMP?"}\n')

            try:
                with (
                    mock.patch(
                        "agent_team.doctor.fcntl.flock",
                        side_effect=unlock_then_tamper,
                    ),
                    self.assertRaises(UnstableSnapshotError),
                ):
                    filesystem.try_marker_exclusive()
                self.assertTrue(tampered)
                with self.assertRaises(StateFilesystemError):
                    filesystem.inventory()
                with self.assertRaises(StateFilesystemError):
                    filesystem.assert_identity(before)
            finally:
                filesystem.close()

    def test_marker_identity_mismatch_drops_orphan_and_never_closes_reuse(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            marker_fd = filesystem._marker_fd
            marker_identity = filesystem._marker_identity
            self.assertIsNotNone(marker_fd)
            self.assertIsNotNone(marker_identity)
            assert marker_fd is not None
            assert marker_identity is not None
            original_close = os.close
            replacement_fd: int | None = None
            filesystem._orphan_fds.append(
                (marker_fd, marker_identity, "marker identity mismatch")
            )
            original_close(marker_fd)
            fillers: list[int] = []
            try:
                while True:
                    candidate_fd = os.open(root / _database_name(), os.O_RDONLY)
                    if candidate_fd == marker_fd:
                        replacement_fd = candidate_fd
                        break
                    fillers.append(candidate_fd)
                with self.assertRaises(StateFilesystemError):
                    filesystem.inventory()
                self.assertTrue(filesystem._marker_invalidated)
                self.assertIsNone(filesystem._marker_fd)
                self.assertFalse(
                    any(fd == marker_fd for fd, _, _ in filesystem._orphan_fds)
                )
                with self.assertRaises(StateFilesystemError):
                    filesystem.inventory()
                filesystem.close()
                assert replacement_fd is not None
                os.fstat(replacement_fd)
            finally:
                if replacement_fd is not None:
                    original_close(replacement_fd)
                for filler_fd in fillers:
                    original_close(filler_fd)
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_root_fstat_failure_retain_reused_fd_as_unresolved_owner(self) -> None:
        from agent_team import store as store_module

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            root_fd = store_module._open_state_root(root)
            original_close = os.close
            original_open = os.open
            original_fstat = os.fstat
            fstat_failed = False
            replacement_fd: int | None = None
            instances: list[StateFilesystem] = []

            class ObservingFilesystem(StateFilesystem):
                def __init__(
                    self,
                    state_root: Path,
                    *,
                    marker_name: str,
                    ledger_name: str,
                    busy_timeout_ms: int = 0,
                ) -> None:
                    super().__init__(
                        state_root,
                        marker_name=marker_name,
                        ledger_name=ledger_name,
                        busy_timeout_ms=busy_timeout_ms,
                    )
                    instances.append(self)

            def fail_root_fstat(fd: int) -> os.stat_result:
                nonlocal fstat_failed, replacement_fd
                if fd == root_fd and not fstat_failed:
                    fstat_failed = True
                    original_close(fd)
                    replacement_source = original_open(
                        root / _database_name(),
                        os.O_RDONLY,
                    )
                    if replacement_source != fd:
                        os.dup2(replacement_source, fd)
                        original_close(replacement_source)
                    replacement_fd = fd
                    raise OSError("initial root fstat failure")
                return original_fstat(fd)

            try:
                with (
                    mock.patch(
                        "agent_team.doctor._store._open_state_root",
                        return_value=root_fd,
                    ),
                    mock.patch(
                        "agent_team.doctor.os.fstat",
                        side_effect=fail_root_fstat,
                    ),
                    self.assertRaises(OSError) as raised,
                ):
                    ObservingFilesystem.open_existing(
                        root,
                        marker_name=MARKER_NAME,
                        ledger_name=LEDGER_NAME,
                    )
                self.assertTrue(fstat_failed)
                self.assertIsNotNone(replacement_fd)
                self.assertEqual(1, len(instances))
                filesystem = instances[0]
                self.assertEqual(1, len(filesystem._orphan_fds))
                self.assertIsNone(filesystem._root_fd)
                cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                with self.assertRaises(StateFilesystemError):
                    cleanup_owner.retry_cleanup()
                self.assertTrue(filesystem._filesystem_invalidated)
                self.assertEqual(1, len(filesystem._orphan_fds))
                assert replacement_fd is not None
                os.fstat(replacement_fd)
            finally:
                if replacement_fd is not None:
                    original_close(replacement_fd)
                if instances:
                    instances[0]._orphan_fds.clear()
                    instances[0].close()

    def test_parent_fstat_failure_retain_reused_fd_as_unresolved_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_close = os.close
            original_fstat = os.fstat
            parent_fds: list[int] = []
            fstat_failed = False
            replacement_fd: int | None = None
            instances: list[StateFilesystem] = []

            class ObservingFilesystem(StateFilesystem):
                def __init__(
                    self,
                    state_root: Path,
                    *,
                    marker_name: str,
                    ledger_name: str,
                    busy_timeout_ms: int = 0,
                ) -> None:
                    super().__init__(
                        state_root,
                        marker_name=marker_name,
                        ledger_name=ledger_name,
                        busy_timeout_ms=busy_timeout_ms,
                    )
                    instances.append(self)

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == "..":
                    parent_fds.append(fd)
                return fd

            def fail_parent_fstat(fd: int) -> os.stat_result:
                nonlocal fstat_failed, replacement_fd
                if parent_fds and fd == parent_fds[0] and not fstat_failed:
                    fstat_failed = True
                    original_close(fd)
                    replacement_source = original_open(
                        root / _database_name(), os.O_RDONLY
                    )
                    if replacement_source != fd:
                        os.dup2(replacement_source, fd)
                        original_close(replacement_source)
                    replacement_fd = fd
                    raise OSError("initial parent fstat failure")
                return original_fstat(fd)

            try:
                with (
                    mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                    mock.patch(
                        "agent_team.doctor.os.fstat",
                        side_effect=fail_parent_fstat,
                    ),
                    self.assertRaises(StateFilesystemError) as raised,
                ):
                    ObservingFilesystem.open_existing(
                        root,
                        marker_name=MARKER_NAME,
                        ledger_name=LEDGER_NAME,
                    )
                self.assertTrue(fstat_failed)
                self.assertIsNotNone(replacement_fd)
                self.assertEqual(1, len(instances))
                filesystem = instances[0]
                self.assertEqual(1, len(filesystem._orphan_fds))
                self.assertIsNone(filesystem._parent_fd)
                cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                with self.assertRaises(StateFilesystemError):
                    cleanup_owner.retry_cleanup()
                self.assertTrue(filesystem._filesystem_invalidated)
                self.assertEqual(1, len(filesystem._orphan_fds))
                assert replacement_fd is not None
                os.fstat(replacement_fd)
            finally:
                if replacement_fd is not None:
                    original_close(replacement_fd)
                if instances:
                    instances[0]._orphan_fds.clear()
                    instances[0].close()

    def test_deserialize_generic_base_exception_keeps_connection_owner(self) -> None:
        class DeserializeFailure(BaseException):
            pass

        class FailingConnection(sqlite3.Connection):
            close_failure = True

            def deserialize(
                self,
                data: object,
                /,
                *,
                name: str = "main",
            ) -> None:
                del data, name
                raise DeserializeFailure("generic deserialize failure")

            def close(self) -> None:
                if type(self).close_failure:
                    raise OSError("persistent deserialize connection close")
                super().close()

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_connect = sqlite3.connect
            connections: list[sqlite3.Connection] = []
            try:
                with (
                    mock.patch(
                        "agent_team.doctor.sqlite3.connect",
                        side_effect=_sqlite_connect_with_factory(
                            original_connect,
                            connections,
                            FailingConnection,
                        ),
                    ),
                    self.assertRaises(DeserializeFailure) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                self.assertEqual(1, len(connections))
                cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                FailingConnection.close_failure = False
                cleanup_owner.retry_cleanup()
            finally:
                filesystem.close()

    def test_deserialize_attrless_base_exception_uses_typed_cleanup_wrapper(
        self,
    ) -> None:
        class FailingConnection(sqlite3.Connection):
            close_failure = True

            def deserialize(self, data: object, *, name: str = "main") -> None:
                del data, name
                raise _AttrlessBody("attrless deserialize failure")

            def close(self) -> None:
                if type(self).close_failure:
                    raise OSError("persistent attrless deserialize close")
                super().close()

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_connect = sqlite3.connect
            connections: list[sqlite3.Connection] = []
            try:
                with (
                    mock.patch(
                        "agent_team.doctor.sqlite3.connect",
                        side_effect=_sqlite_connect_with_factory(
                            original_connect,
                            connections,
                            FailingConnection,
                        ),
                    ),
                    self.assertRaises(BaseException) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                self.assertIsInstance(raised.exception, CleanupOwnerError)
                self.assertIsInstance(raised.exception.__cause__, _AttrlessBody)
                self.assertEqual(
                    "attrless deserialize failure",
                    str(raised.exception.__cause__),
                )
                cleanup_owner = cast(CleanupOwnerError, raised.exception).cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                FailingConnection.close_failure = False
                cleanup_owner.retry_cleanup()
            finally:
                filesystem.close()

    def test_deserialize_attrless_one_shot_close_preserves_body_cause(self) -> None:
        class OneShotConnection(sqlite3.Connection):
            close_calls = 0

            def deserialize(self, data: object, *, name: str = "main") -> None:
                del data, name
                raise _AttrlessBody("one-shot deserialize body")

            def close(self) -> None:
                type(self).close_calls += 1
                if type(self).close_calls == 1:
                    raise OSError("one-shot deserialize close")
                super().close()

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_connect = sqlite3.connect
            connections: list[sqlite3.Connection] = []
            OneShotConnection.close_calls = 0
            try:
                with (
                    mock.patch(
                        "agent_team.doctor.sqlite3.connect",
                        side_effect=_sqlite_connect_with_factory(
                            original_connect,
                            connections,
                            OneShotConnection,
                        ),
                    ),
                    self.assertRaises(BaseException) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                self.assertIsInstance(raised.exception, CleanupOwnerError)
                self.assertIsInstance(raised.exception.__cause__, _AttrlessBody)
                self.assertEqual(
                    "one-shot deserialize body",
                    str(raised.exception.__cause__),
                )
                cleanup_owner = cast(CleanupOwnerError, raised.exception).cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                self.assertEqual(2, OneShotConnection.close_calls)
                cleanup_owner.retry_cleanup()
                self.assertEqual(3, OneShotConnection.close_calls)
            finally:
                filesystem.close()

    def test_state_filesystem_attrless_body_with_marker_cleanup_is_wrapped(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_close = os.close
            held_fds = [
                fd
                for fd in (
                    filesystem._marker_fd,
                    filesystem._gate_fd,
                    filesystem._root_fd,
                    filesystem._parent_fd,
                )
                if fd is not None
            ]

            def fail_held_close(fd: int) -> None:
                if fd in held_fds:
                    raise OSError("persistent context close failure")
                original_close(fd)

            try:
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_held_close,
                    ),
                    self.assertRaises(BaseException) as raised,
                    filesystem,
                ):
                    raise _AttrlessBody("attrless context body")
                self.assertIsInstance(raised.exception, CleanupOwnerError)
                self.assertIsInstance(raised.exception.__cause__, _AttrlessBody)
                self.assertEqual(
                    "attrless context body", str(raised.exception.__cause__)
                )
                cleanup_owner = cast(CleanupOwnerError, raised.exception).cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                cleanup_owner.retry_cleanup()
            finally:
                filesystem.close()

    def test_state_filesystem_attrless_body_with_temporary_fd_cleanup_is_wrapped(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            metadata = filesystem._lstat(_database_name())
            self.assertIsNotNone(metadata)
            assert metadata is not None
            original_open = os.open
            original_read = os.read
            original_close = os.close
            opened: list[int] = []

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == _database_name():
                    opened.append(fd)
                return fd

            def fail_read(fd: int, size: int) -> bytes:
                if fd in opened:
                    raise _AttrlessBody("attrless digest body")
                return original_read(fd, size)

            def fail_close(fd: int) -> None:
                if fd in opened:
                    raise OSError("persistent digest close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                    mock.patch("agent_team.doctor.os.read", side_effect=fail_read),
                    mock.patch("agent_team.doctor.os.close", side_effect=fail_close),
                    self.assertRaises(BaseException) as raised,
                ):
                    filesystem._digest_regular(_database_name(), metadata)
                self.assertIsInstance(raised.exception, CleanupOwnerError)
                cleanup_owner = cast(CleanupOwnerError, raised.exception).cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                cleanup_owner.retry_cleanup()
            finally:
                filesystem.close()

    def test_read_only_doctor_attrless_body_with_connection_cleanup_is_wrapped(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_connect = sqlite3.connect
            connections: list[sqlite3.Connection] = []
            _FailingSQLiteConnection.persistent_failure = True
            _FailingSQLiteConnection.actual_close_then_error = False
            _FailingSQLiteConnection.close_calls = 0
            try:
                with (
                    mock.patch(
                        "agent_team.doctor.sqlite3.connect",
                        side_effect=_failing_sqlite_connect(
                            original_connect,
                            connections,
                        ),
                    ),
                    mock.patch(
                        "agent_team.doctor._store._validate_existing_schema",
                        side_effect=_AttrlessBody("attrless schema body"),
                    ),
                    self.assertRaises(BaseException) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                self.assertIsInstance(raised.exception, CleanupOwnerError)
                self.assertIsInstance(raised.exception.__cause__, _AttrlessBody)
                self.assertEqual(
                    "attrless schema body", str(raised.exception.__cause__)
                )
                cleanup_owner = cast(CleanupOwnerError, raised.exception).cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                self.assertEqual(2, _FailingSQLiteConnection.close_calls)
                _FailingSQLiteConnection.persistent_failure = False
                cleanup_owner.retry_cleanup()
                self.assertEqual(3, _FailingSQLiteConnection.close_calls)
            finally:
                filesystem.close()

    def test_attrless_body_with_one_shot_connection_close_keeps_exact_owner(
        self,
    ) -> None:
        class OneShotConnection(sqlite3.Connection):
            close_calls = 0

            def close(self) -> None:
                type(self).close_calls += 1
                if type(self).close_calls == 1:
                    raise OSError("one-shot connection close failure")
                super().close()

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_connect = sqlite3.connect
            connections: list[sqlite3.Connection] = []
            OneShotConnection.close_calls = 0
            try:
                with (
                    mock.patch(
                        "agent_team.doctor.sqlite3.connect",
                        side_effect=_sqlite_connect_with_factory(
                            original_connect,
                            connections,
                            OneShotConnection,
                        ),
                    ),
                    mock.patch(
                        "agent_team.doctor._store._validate_existing_schema",
                        side_effect=_AttrlessBody("one-shot body"),
                    ),
                    self.assertRaises(BaseException) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                self.assertIsInstance(raised.exception, CleanupOwnerError)
                self.assertIsInstance(raised.exception.__cause__, _AttrlessBody)
                self.assertEqual("one-shot body", str(raised.exception.__cause__))
                cleanup_owner = cast(CleanupOwnerError, raised.exception).cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                self.assertEqual(2, OneShotConnection.close_calls)
                cleanup_owner.retry_cleanup()
                self.assertEqual(3, OneShotConnection.close_calls)
            finally:
                filesystem.close()

    def test_convenience_doctor_attrless_body_with_database_fd_cleanup_is_wrapped(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_close = os.close
            database_fds: list[int] = []
            cleanup_armed = False

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == _database_name():
                    database_fds.append(fd)
                return fd

            def fail_database_close(fd: int) -> None:
                if cleanup_armed and database_fds and fd == database_fds[-1]:
                    raise OSError("persistent convenience database fd close")
                original_close(fd)

            def fail_schema(connection: sqlite3.Connection) -> None:
                del connection
                nonlocal cleanup_armed
                cleanup_armed = True
                raise _AttrlessBody("attrless convenience body")

            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_database_close,
                ),
                mock.patch(
                    "agent_team.doctor._store._validate_existing_schema",
                    side_effect=fail_schema,
                ),
                self.assertRaises(BaseException) as raised,
            ):
                run_doctor(
                    root,
                    "op-identity-mismatch",
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertIsInstance(raised.exception, CleanupOwnerError)
            cleanup_owner = cast(CleanupOwnerError, raised.exception).cleanup_owner
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            cleanup_owner.retry_cleanup()

    def test_state_filesystem_held_fd_drops_actual_close_then_error_without_reuse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            marker_fd = filesystem._marker_fd
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
                    mock.patch("agent_team.doctor.os.close", side_effect=close),
                    self.assertRaises(StateFilesystemError),
                ):
                    filesystem.close()
                fillers: list[int] = []
                while True:
                    candidate_fd = os.open(root / _database_name(), os.O_RDONLY)
                    if candidate_fd == marker_fd:
                        replacement_fd = candidate_fd
                        break
                    fillers.append(candidate_fd)
                try:
                    filesystem.close()
                    os.fstat(replacement_fd)
                finally:
                    original_close(replacement_fd)
                    for filler_fd in fillers:
                        original_close(filler_fd)
            finally:
                try:
                    filesystem.close()
                except StateFilesystemError:
                    pass

    def test_state_filesystem_persistent_orphan_close_is_typed_and_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            fd = os.open(root / _database_name(), os.O_RDONLY)
            metadata = os.fstat(fd)
            filesystem._orphan_fds.append(
                (fd, (metadata.st_dev, metadata.st_ino), "persistent orphan")
            )
            original_close = os.close

            def fail_orphan_close(target: int) -> None:
                if target == fd:
                    raise OSError("simulated persistent orphan close")
                original_close(target)

            try:
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_orphan_close,
                    ),
                    self.assertRaises(StateFilesystemError),
                ):
                    filesystem.close()
                self.assertEqual(1, len(filesystem._orphan_fds))
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_orphan_close,
                    ),
                    self.assertRaises(StateFilesystemError),
                ):
                    filesystem.close()
                self.assertEqual(1, len(filesystem._orphan_fds))
            finally:
                original_close(fd)
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_pending_orphan_blocks_new_inventory_io_and_stays_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            fd = os.open(root / _database_name(), os.O_RDONLY)
            metadata = os.fstat(fd)
            filesystem._orphan_fds.append(
                (fd, (metadata.st_dev, metadata.st_ino), "pending inventory")
            )
            original_close = os.close
            original_open = os.open
            open_calls = 0

            def fail_orphan_close(target: int) -> None:
                if target == fd:
                    raise OSError("simulated pending inventory close")
                original_close(target)

            def count_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                nonlocal open_calls
                open_calls += 1
                return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

            try:
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_orphan_close,
                    ),
                    mock.patch(
                        "agent_team.doctor.os.open",
                        side_effect=count_open,
                    ),
                ):
                    for _ in range(3):
                        with self.assertRaises(StateFilesystemError):
                            filesystem.inventory()
                self.assertEqual(0, open_calls)
                self.assertEqual(1, len(filesystem._orphan_fds))
            finally:
                original_close(fd)
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_temporary_fd_status_unknown_is_retained_for_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            fd = os.open(root / _database_name(), os.O_RDONLY)
            identity = (
                os.fstat(fd).st_dev,
                os.fstat(fd).st_ino,
            )
            original_close = os.close
            original_fstat = os.fstat
            fstat_calls = 0

            def fail_close(target: int) -> None:
                if target == fd:
                    raise OSError("simulated status-unknown close")
                original_close(target)

            def fail_fstat(target: int) -> os.stat_result:
                nonlocal fstat_calls
                if target == fd:
                    fstat_calls += 1
                    if fstat_calls >= 2:
                        raise OSError("simulated status-unknown fstat")
                return original_fstat(target)

            try:
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_close,
                    ),
                    mock.patch(
                        "agent_team.doctor.os.fstat",
                        side_effect=fail_fstat,
                    ),
                    self.assertRaises(StateFilesystemError),
                ):
                    filesystem._close_owned_temporary_fd(
                        fd,
                        identity,
                        "status-unknown temporary",
                    )
                self.assertEqual(1, len(filesystem._orphan_fds))
                filesystem._retry_orphan_fds()
                with self.assertRaises(OSError):
                    os.fstat(fd)
            finally:
                filesystem._orphan_fds.clear()
                try:
                    original_close(fd)
                except OSError:
                    pass
                filesystem.close()

    def test_identity_unknown_orphan_is_retained_without_fd_close(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            fd = os.open(root / _database_name(), os.O_RDONLY)
            original_close = os.close
            close_calls = 0

            def count_close(target: int) -> None:
                nonlocal close_calls
                if target == fd:
                    close_calls += 1
                original_close(target)

            try:
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=count_close,
                    ),
                    self.assertRaises(StateFilesystemError),
                ):
                    filesystem._close_owned_temporary_fd(
                        fd,
                        None,
                        "identity-unknown",
                    )
                self.assertEqual(0, close_calls)
                self.assertEqual(1, len(filesystem._orphan_fds))
                self.assertIsNone(filesystem._orphan_fds[0][1])
            finally:
                filesystem._orphan_fds.clear()
                original_close(fd)
                filesystem.close()

    def test_doctor_database_fd_is_handed_to_filesystem_registry_on_close_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            opened: list[int] = []
            original_open_existing = filesystem.open_existing_regular
            original_close = os.close

            def open_existing(name: str) -> int:
                fd = original_open_existing(name)
                if name == _database_name():
                    opened.append(fd)
                return fd

            def fail_database_close(fd: int) -> None:
                if fd in opened:
                    raise OSError("simulated persistent doctor database close")
                original_close(fd)

            try:
                with (
                    mock.patch.object(
                        filesystem,
                        "open_existing_regular",
                        side_effect=open_existing,
                    ),
                    mock.patch(
                        "agent_team.doctor.os.close", side_effect=fail_database_close
                    ),
                    self.assertRaises(CleanupUncertaintyError) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                self.assertEqual(1, len(filesystem._orphan_fds))
                self.assertTrue(opened)
                os.fstat(opened[0])
                cleanup_owner = raised.exception.cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                cleanup_owner.retry_cleanup()
                self.assertEqual([], filesystem._orphan_fds)
            finally:
                try:
                    original_close(opened[0])
                except OSError:
                    pass
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_doctor_database_connection_close_failure_is_typed_and_owned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_connect = sqlite3.connect
            connections: list[sqlite3.Connection] = []
            _FailingSQLiteConnection.persistent_failure = True
            _FailingSQLiteConnection.actual_close_then_error = False
            _FailingSQLiteConnection.close_calls = 0
            try:
                with (
                    mock.patch(
                        "agent_team.doctor.sqlite3.connect",
                        side_effect=_failing_sqlite_connect(
                            original_connect,
                            connections,
                        ),
                    ),
                    self.assertRaises(CleanupUncertaintyError) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                self.assertEqual(1, len(connections))
                cleanup_owner = raised.exception.cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                with self.assertRaises(OSError):
                    cleanup_owner.retry_cleanup()
                self.assertEqual(1, len(connections))
                _FailingSQLiteConnection.persistent_failure = False
                cleanup_owner.retry_cleanup()
                self.assertEqual(1, len(connections))
                report = ReadOnlyDoctor(filesystem=filesystem).inspect(
                    root, "op-identity-mismatch"
                )
                self.assertEqual("UNKNOWN_EFFECT", report.observed_state)
            finally:
                filesystem.close()

    def test_external_doctor_combines_connection_and_database_fd_cleanup_owners(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_connect = sqlite3.connect
            original_deserialize = doctor_module._deserialize_database_from_fd
            original_open_existing = filesystem.open_existing_regular
            original_close = os.close
            connections: list[sqlite3.Connection] = []
            database_fds: list[int] = []
            cleanup_armed = False
            doctor_database_fd: int | None = None

            def capture_open_existing(name: str) -> int:
                fd = original_open_existing(name)
                if name == _database_name():
                    database_fds.append(fd)
                return fd

            def arm_deserialize(fd: int) -> sqlite3.Connection:
                nonlocal cleanup_armed, doctor_database_fd
                cleanup_armed = True
                doctor_database_fd = fd
                return original_deserialize(fd)

            def fail_database_close(fd: int) -> None:
                if cleanup_armed and fd == doctor_database_fd:
                    raise OSError("persistent simultaneous database fd close")
                original_close(fd)

            _FailingSQLiteConnection.persistent_failure = True
            _FailingSQLiteConnection.actual_close_then_error = False
            _FailingSQLiteConnection.close_calls = 0
            try:
                with (
                    mock.patch.object(
                        filesystem,
                        "open_existing_regular",
                        side_effect=capture_open_existing,
                    ),
                    mock.patch(
                        "agent_team.doctor._deserialize_database_from_fd",
                        side_effect=arm_deserialize,
                    ),
                    mock.patch(
                        "agent_team.doctor.sqlite3.connect",
                        side_effect=_failing_sqlite_connect(
                            original_connect,
                            connections,
                        ),
                    ),
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_database_close,
                    ),
                    self.assertRaises(CleanupUncertaintyError) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                self.assertEqual(1, len(connections))
                self.assertEqual(1, len(filesystem._orphan_fds))
                cleanup_owner = raised.exception.cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                self.assertEqual(2, len(cleanup_owner._members))
                _FailingSQLiteConnection.persistent_failure = False
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_database_close,
                    ),
                    self.assertRaises(StateFilesystemError),
                ):
                    cleanup_owner.retry_cleanup()
                self.assertEqual(1, len(filesystem._orphan_fds))
                cleanup_owner.retry_cleanup()
                self.assertEqual([], filesystem._orphan_fds)
                cleanup_owner.retry_cleanup()
            finally:
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_external_doctor_initial_cleanup_uncertainty_is_not_a_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            fd = os.open(root / _database_name(), os.O_RDONLY)
            metadata = os.fstat(fd)
            filesystem._orphan_fds.append(
                (fd, (metadata.st_dev, metadata.st_ino), "initial readiness")
            )
            original_close = os.close

            def fail_close(target: int) -> None:
                if target == fd:
                    raise OSError("persistent initial readiness close")
                original_close(target)

            try:
                with (
                    mock.patch("agent_team.doctor.os.close", side_effect=fail_close),
                    self.assertRaises(StateFilesystemError) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                cleanup_owner = raised.exception.cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                cleanup_owner.retry_cleanup()
                self.assertEqual([], filesystem._orphan_fds)
            finally:
                filesystem._orphan_fds.clear()
                try:
                    original_close(fd)
                except OSError:
                    pass
                filesystem.close()

    def test_open_regular_registry_overflow_composes_current_fd_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_fstat = os.fstat
            original_close = os.close
            opened: list[int] = []

            class OverflowFilesystem(StateFilesystem):
                lstat_calls = 0
                injected = False

                def _fault(self, point: str) -> None:
                    if point != "after_db_lstat" or self.injected:
                        return
                    self.injected = True
                    for index in range(8):
                        filler_fd = original_open(
                            root / _database_name(),
                            os.O_RDONLY,
                        )
                        filler_metadata = original_fstat(filler_fd)
                        self._orphan_fds.append(
                            (
                                filler_fd,
                                (filler_metadata.st_dev, filler_metadata.st_ino),
                                f"open regular filler {index}",
                            )
                        )

                def _lstat(self, name: str) -> os.stat_result | None:
                    result = super()._lstat(name)
                    if name == _database_name():
                        self.lstat_calls += 1
                        if self.lstat_calls == 2:
                            raise _AttrlessBody("open regular body failure")
                    return result

            filesystem = OverflowFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == _database_name():
                    opened.append(fd)
                return fd

            def fail_current_close(fd: int) -> None:
                if opened and fd == opened[-1]:
                    raise OSError("persistent open regular current close")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_current_close,
                    ),
                    self.assertRaises(BaseException) as raised,
                ):
                    filesystem._open_regular(_database_name())
                self.assertIsInstance(raised.exception, CleanupOwnerError)
                cleanup_owner = cast(CleanupOwnerError, raised.exception).cleanup_owner
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                self.assertEqual(2, len(cleanup_owner._members))
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_current_close,
                    ),
                    self.assertRaises(StateFilesystemError),
                ):
                    cleanup_owner.retry_cleanup()
                self.assertEqual([], filesystem._orphan_fds)
                cleanup_owner.retry_cleanup()
                self.assertTrue(opened)
                with self.assertRaises(OSError):
                    os.fstat(opened[-1])
            finally:
                for fd in opened:
                    try:
                        original_close(fd)
                    except OSError:
                        pass
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_combined_cleanup_owner_removes_successful_members_and_deduplicates(
        self,
    ) -> None:
        calls: list[str] = []
        first = CleanupOwner(lambda: calls.append("first"))
        second_attempts = 0

        def retry_second() -> None:
            nonlocal second_attempts
            second_attempts += 1
            calls.append("second")
            if second_attempts == 1:
                raise OSError("one-shot second cleanup")

        second = CleanupOwner(retry_second)
        combined = doctor_module._combine_cleanup_owners(first, second, first)
        self.assertEqual(2, len(combined._members))
        with self.assertRaises(OSError):
            combined.retry_cleanup()
        self.assertEqual(["first", "second"], calls)
        self.assertEqual(1, len(combined._members))
        combined.retry_cleanup()
        self.assertEqual(["first", "second", "second"], calls)
        self.assertEqual(0, len(combined._members))
        combined.retry_cleanup()
        self.assertEqual(["first", "second", "second"], calls)

    def test_close_rechecks_identity_after_marker_and_gate_unlock(self) -> None:
        for target in ("marker", "gate"):
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _create_pending_claim_root(temporary)
                filesystem = StateFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
                if target == "marker":
                    target_fd = filesystem._marker_fd
                else:
                    target_fd = filesystem._gate_fd
                self.assertIsNotNone(target_fd)
                assert target_fd is not None
                original_flock = fcntl.flock
                original_open = os.open
                original_close = os.close
                replacement_fd: int | None = None

                def unlock_and_reuse(
                    fd: int,
                    operation: int,
                    _original_flock: Callable[[int, int], object] = original_flock,
                    _target_fd: int = target_fd,
                    _original_close: Callable[[int], None] = original_close,
                    _original_open: Callable[..., int] = original_open,
                    _root: Path = root,
                ) -> None:
                    nonlocal replacement_fd
                    _original_flock(fd, operation)
                    if fd == _target_fd and operation & fcntl.LOCK_UN:
                        _original_close(fd)
                        source_fd = _original_open(
                            _root / _database_name(), os.O_RDONLY
                        )
                        if source_fd != fd:
                            os.dup2(source_fd, fd)
                            _original_close(source_fd)
                        replacement_fd = fd

                try:
                    with mock.patch(
                        "agent_team.doctor.fcntl.flock",
                        side_effect=unlock_and_reuse,
                    ):
                        try:
                            filesystem.close()
                        except StateFilesystemError:
                            pass
                    self.assertTrue(filesystem._filesystem_invalidated)
                    self.assertIsNone(
                        filesystem._marker_fd
                        if target == "marker"
                        else filesystem._gate_fd
                    )
                    assert replacement_fd is not None
                    os.fstat(replacement_fd)
                finally:
                    if replacement_fd is not None:
                        original_close(replacement_fd)
                    filesystem._orphan_fds.clear()
                    filesystem.close()

    def test_convenience_doctor_database_connection_close_failure_is_owned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_connect = sqlite3.connect
            connections: list[sqlite3.Connection] = []
            _FailingSQLiteConnection.persistent_failure = True
            _FailingSQLiteConnection.actual_close_then_error = False
            _FailingSQLiteConnection.close_calls = 0
            with (
                mock.patch(
                    "agent_team.doctor.sqlite3.connect",
                    side_effect=_failing_sqlite_connect(
                        original_connect,
                        connections,
                    ),
                ),
                self.assertRaises(CleanupUncertaintyError) as raised,
            ):
                run_doctor(
                    root,
                    "op-identity-mismatch",
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertEqual(1, len(connections))
            cleanup_owner = raised.exception.cleanup_owner
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            _FailingSQLiteConnection.persistent_failure = False
            cleanup_owner.retry_cleanup()
            self.assertEqual(1, len(connections))
            report = run_doctor(
                root,
                "op-identity-mismatch",
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            self.assertEqual("UNKNOWN_EFFECT", report.observed_state)

    def test_convenience_doctor_combines_connection_and_database_fd_cleanup_owners(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_connect = sqlite3.connect
            original_deserialize = doctor_module._deserialize_database_from_fd
            original_open = os.open
            original_close = os.close
            connections: list[sqlite3.Connection] = []
            database_fds: list[int] = []
            cleanup_armed = False
            doctor_database_fd: int | None = None

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == _database_name():
                    database_fds.append(fd)
                return fd

            def arm_deserialize(fd: int) -> sqlite3.Connection:
                nonlocal cleanup_armed, doctor_database_fd
                cleanup_armed = True
                doctor_database_fd = fd
                return original_deserialize(fd)

            def fail_database_close(fd: int) -> None:
                if cleanup_armed and fd == doctor_database_fd:
                    raise OSError("persistent convenience database fd close")
                original_close(fd)

            _FailingSQLiteConnection.persistent_failure = True
            _FailingSQLiteConnection.actual_close_then_error = False
            _FailingSQLiteConnection.close_calls = 0
            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch(
                    "agent_team.doctor._deserialize_database_from_fd",
                    side_effect=arm_deserialize,
                ),
                mock.patch(
                    "agent_team.doctor.sqlite3.connect",
                    side_effect=_failing_sqlite_connect(
                        original_connect,
                        connections,
                    ),
                ),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_database_close,
                ),
                self.assertRaises(CleanupUncertaintyError) as raised,
            ):
                run_doctor(
                    root,
                    "op-identity-mismatch",
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertEqual(1, len(connections))
            cleanup_owner = raised.exception.cleanup_owner
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            self.assertEqual(2, len(cleanup_owner._members))
            _FailingSQLiteConnection.persistent_failure = False
            with (
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_database_close,
                ),
                self.assertRaises(StateFilesystemError),
            ):
                cleanup_owner.retry_cleanup()
            cleanup_owner.retry_cleanup()
            cleanup_owner.retry_cleanup()
            self.assertTrue(database_fds)
            with self.assertRaises(OSError):
                os.fstat(database_fds[-1])

    def test_convenience_doctor_combined_cleanup_keeps_schema_body_primary(
        self,
    ) -> None:
        from agent_team import store as store_module

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_connect = sqlite3.connect
            original_deserialize = doctor_module._deserialize_database_from_fd
            original_open = os.open
            original_close = os.close
            connections: list[sqlite3.Connection] = []
            database_fds: list[int] = []
            cleanup_armed = False
            doctor_database_fd: int | None = None

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == _database_name():
                    database_fds.append(fd)
                return fd

            def arm_deserialize(fd: int) -> sqlite3.Connection:
                nonlocal cleanup_armed, doctor_database_fd
                cleanup_armed = True
                doctor_database_fd = fd
                return original_deserialize(fd)

            def fail_database_close(fd: int) -> None:
                if cleanup_armed and fd == doctor_database_fd:
                    raise OSError("persistent convenience body database fd close")
                original_close(fd)

            _FailingSQLiteConnection.persistent_failure = True
            _FailingSQLiteConnection.actual_close_then_error = False
            _FailingSQLiteConnection.close_calls = 0

            def fail_schema(connection: sqlite3.Connection) -> None:
                del connection
                raise store_module.StoreSchemaError("convenience schema body")

            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch(
                    "agent_team.doctor._deserialize_database_from_fd",
                    side_effect=arm_deserialize,
                ),
                mock.patch(
                    "agent_team.doctor.sqlite3.connect",
                    side_effect=_failing_sqlite_connect(
                        original_connect,
                        connections,
                    ),
                ),
                mock.patch(
                    "agent_team.doctor._store._validate_existing_schema",
                    side_effect=fail_schema,
                ),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_database_close,
                ),
                self.assertRaises(store_module.StoreSchemaError) as raised,
            ):
                run_doctor(
                    root,
                    "op-identity-mismatch",
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertEqual("convenience schema body", str(raised.exception))
            cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            self.assertEqual(2, len(cleanup_owner._members))
            _FailingSQLiteConnection.persistent_failure = False
            with (
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_database_close,
                ),
                self.assertRaises(StateFilesystemError),
            ):
                cleanup_owner.retry_cleanup()
            cleanup_owner.retry_cleanup()
            self.assertTrue(database_fds)
            with self.assertRaises(OSError):
                os.fstat(database_fds[-1])

    def test_doctor_schema_body_and_connection_cleanup_keep_body_primary(
        self,
    ) -> None:
        from agent_team import store as store_module

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_connect = sqlite3.connect
            connections: list[sqlite3.Connection] = []
            _FailingSQLiteConnection.persistent_failure = True
            _FailingSQLiteConnection.actual_close_then_error = False
            _FailingSQLiteConnection.close_calls = 0
            try:
                with (
                    mock.patch(
                        "agent_team.doctor.sqlite3.connect",
                        side_effect=_failing_sqlite_connect(
                            original_connect,
                            connections,
                        ),
                    ),
                    mock.patch(
                        "agent_team.doctor._store._validate_existing_schema",
                        side_effect=store_module.StoreSchemaError(
                            "schema body failure"
                        ),
                    ),
                    self.assertRaises(store_module.StoreSchemaError) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                self.assertEqual(1, len(connections))
                cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                _FailingSQLiteConnection.persistent_failure = False
                cleanup_owner.retry_cleanup()
                report = ReadOnlyDoctor(filesystem=filesystem).inspect(
                    root, "op-identity-mismatch"
                )
                self.assertEqual("UNKNOWN_EFFECT", report.observed_state)
            finally:
                filesystem.close()

    def test_external_doctor_combines_existing_body_connection_and_fd_owners(
        self,
    ) -> None:
        from agent_team import store as store_module

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_connect = sqlite3.connect
            original_deserialize = doctor_module._deserialize_database_from_fd
            original_open_existing = filesystem.open_existing_regular
            original_close = os.close
            connections: list[sqlite3.Connection] = []
            database_fds: list[int] = []
            cleanup_armed = False
            body_retries: list[bool] = []
            body_owner = CleanupOwner(lambda: body_retries.append(True))
            schema_error = store_module.StoreSchemaError("schema body failure")
            vars(schema_error)["cleanup_owner"] = body_owner

            def capture_open_existing(name: str) -> int:
                fd = original_open_existing(name)
                if name == _database_name():
                    database_fds.append(fd)
                return fd

            def arm_deserialize(fd: int) -> sqlite3.Connection:
                nonlocal cleanup_armed
                cleanup_armed = True
                return original_deserialize(fd)

            def fail_database_close(fd: int) -> None:
                if cleanup_armed and database_fds and fd == database_fds[-1]:
                    raise OSError("persistent simultaneous body fd close")
                original_close(fd)

            _FailingSQLiteConnection.persistent_failure = True
            _FailingSQLiteConnection.actual_close_then_error = False
            _FailingSQLiteConnection.close_calls = 0
            try:
                with (
                    mock.patch.object(
                        filesystem,
                        "open_existing_regular",
                        side_effect=capture_open_existing,
                    ),
                    mock.patch(
                        "agent_team.doctor._deserialize_database_from_fd",
                        side_effect=arm_deserialize,
                    ),
                    mock.patch(
                        "agent_team.doctor.sqlite3.connect",
                        side_effect=_failing_sqlite_connect(
                            original_connect,
                            connections,
                        ),
                    ),
                    mock.patch(
                        "agent_team.doctor._store._validate_existing_schema",
                        side_effect=schema_error,
                    ),
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_database_close,
                    ),
                    self.assertRaises(store_module.StoreSchemaError) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                self.assertEqual("schema body failure", str(raised.exception))
                self.assertEqual(1, len(filesystem._orphan_fds))
                cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                self.assertEqual(3, len(cleanup_owner._members))
                self.assertEqual(2, _FailingSQLiteConnection.close_calls)
                _FailingSQLiteConnection.persistent_failure = False
                with (
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_database_close,
                    ),
                    self.assertRaises(StateFilesystemError),
                ):
                    cleanup_owner.retry_cleanup()
                self.assertEqual(1, len(filesystem._orphan_fds))
                cleanup_owner.retry_cleanup()
                self.assertEqual([], filesystem._orphan_fds)
                self.assertTrue(body_retries)
            finally:
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_doctor_schema_body_and_database_fd_cleanup_keep_body_primary(
        self,
    ) -> None:
        from agent_team import store as store_module

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_open_existing = filesystem.open_existing_regular
            original_close = os.close
            opened: list[int] = []

            def capture_open_existing(name: str) -> int:
                fd = original_open_existing(name)
                if name == _database_name():
                    opened.append(fd)
                return fd

            def fail_database_close(fd: int) -> None:
                if fd in opened:
                    raise OSError("persistent doctor database fd close")
                original_close(fd)

            try:
                with (
                    mock.patch.object(
                        filesystem,
                        "open_existing_regular",
                        side_effect=capture_open_existing,
                    ),
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=fail_database_close,
                    ),
                    mock.patch(
                        "agent_team.doctor._store._validate_existing_schema",
                        side_effect=store_module.StoreSchemaError(
                            "schema body failure"
                        ),
                    ),
                    self.assertRaises(store_module.StoreSchemaError) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
                self.assertIsNotNone(cleanup_owner)
                assert cleanup_owner is not None
                self.assertEqual(1, len(filesystem._orphan_fds))
                cleanup_owner.retry_cleanup()
                self.assertEqual([], filesystem._orphan_fds)
            finally:
                filesystem._orphan_fds.clear()
                filesystem.close()

    def test_actual_close_then_error_on_connection_is_typed_without_owner(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_connect = sqlite3.connect
            connections: list[sqlite3.Connection] = []
            _FailingSQLiteConnection.persistent_failure = False
            _FailingSQLiteConnection.actual_close_then_error = True
            _FailingSQLiteConnection.close_calls = 0
            try:
                with (
                    mock.patch(
                        "agent_team.doctor.sqlite3.connect",
                        side_effect=_failing_sqlite_connect(
                            original_connect,
                            connections,
                        ),
                    ),
                    self.assertRaises(CleanupUncertaintyError) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root, "op-identity-mismatch"
                    )
                self.assertEqual(1, len(connections))
                self.assertIsNone(raised.exception.cleanup_owner)
                raised.exception.retry_cleanup()
            finally:
                filesystem.close()

    def test_actual_close_then_error_on_connection_survives_convenience_wrapper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_connect = sqlite3.connect
            connections: list[sqlite3.Connection] = []
            _FailingSQLiteConnection.persistent_failure = False
            _FailingSQLiteConnection.actual_close_then_error = True
            _FailingSQLiteConnection.close_calls = 0
            with (
                mock.patch(
                    "agent_team.doctor.sqlite3.connect",
                    side_effect=_failing_sqlite_connect(
                        original_connect,
                        connections,
                    ),
                ),
                self.assertRaises(CleanupUncertaintyError) as raised,
            ):
                run_doctor(
                    root,
                    "op-identity-mismatch",
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertEqual(1, len(connections))
            self.assertIsNone(raised.exception.cleanup_owner)
            raised.exception.retry_cleanup()

    def test_public_layout_names_reject_evil_str_subclasses_before_stat(self) -> None:
        class EvilName(str):
            def __contains__(self, value: object) -> bool:
                del value
                return False

            def encode(self, *args: object, **kwargs: object) -> bytes:
                del args, kwargs
                return b"safe-name"

            def __eq__(self, other: object) -> bool:
                del other
                return False

            def __ne__(self, other: object) -> bool:
                del other
                return False

            def __hash__(self) -> int:
                return hash("safe-name")

        evil = EvilName("../victim")
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            victim = root.parent / "victim"
            victim.write_bytes(WRITER_MARKER_CLEAN_CONTENT)
            victim.chmod(0o600)
            with self.assertRaises((TypeError, ValueError)):
                StateFilesystem.open_existing(
                    root,
                    marker_name=evil,
                    ledger_name=LEDGER_NAME,
                )
            with self.assertRaises((TypeError, ValueError)):
                StateFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=evil,
                )
            self.assertEqual(WRITER_MARKER_CLEAN_CONTENT, victim.read_bytes())

    def test_digest_temporary_fd_close_failure_is_typed_and_not_success(self) -> None:
        class CaptureFilesystem(StateFilesystem):
            digest_fd: int | None = None

            def _open_regular(
                self,
                name: str,
                *,
                ensure_ready: bool = True,
            ) -> tuple[int, os.stat_result]:
                fd, metadata = super()._open_regular(
                    name,
                    ensure_ready=ensure_ready,
                )
                if name == _database_name():
                    self.digest_fd = fd
                return fd, metadata

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = cast(
                CaptureFilesystem,
                CaptureFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ),
            )
            original_close = os.close
            failed = True

            def close(fd: int) -> None:
                nonlocal failed
                if fd == filesystem.digest_fd and failed:
                    failed = False
                    raise OSError("injected digest close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.doctor.os.close", side_effect=close),
                    self.assertRaises(StateFilesystemError),
                ):
                    filesystem.inventory()
                digest_fd = filesystem.digest_fd
                self.assertIsNotNone(digest_fd)
                assert digest_fd is not None
                with self.assertRaises(OSError):
                    os.fstat(digest_fd)
            finally:
                filesystem.close()

    def test_ledger_temporary_fd_close_failure_is_typed_and_not_success(self) -> None:
        class CaptureFilesystem(StateFilesystem):
            ledger_fd: int | None = None

            def open_existing_regular(self, name: str) -> int:
                fd = super().open_existing_regular(name)
                if name == LEDGER_NAME:
                    self.ledger_fd = fd
                return fd

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            digest = "sha256:" + "a" * 64
            ledger = root / LEDGER_NAME
            ledger.write_text(
                '{"version":1,"sequence":1,"phase":"RESTORE_PREPARED",'
                f'"restore_generation":1,"recovery_epoch":1,"fencing_token_floor":1,"backup_digest":"{digest}",'
                '"actor":"operator","audit_ref":"audit/1"}\n',
                encoding="utf-8",
            )
            ledger.chmod(0o600)
            filesystem = cast(
                CaptureFilesystem,
                CaptureFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ),
            )
            original_close = os.close
            failed = True

            def close(fd: int) -> None:
                nonlocal failed
                if fd == filesystem.ledger_fd and failed:
                    failed = False
                    raise OSError("injected ledger close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.doctor.os.close", side_effect=close),
                    self.assertRaises(StateFilesystemError),
                ):
                    RecoveryLedgerReader().read(filesystem)
                ledger_fd = filesystem.ledger_fd
                self.assertIsNotNone(ledger_fd)
                assert ledger_fd is not None
                with self.assertRaises(OSError):
                    os.fstat(ledger_fd)
            finally:
                filesystem.close()

    def test_open_regular_exception_close_failure_is_typed_and_retriable(self) -> None:
        class FailingFilesystem(StateFilesystem):
            lstat_calls = 0

            def _lstat(self, name: str) -> os.stat_result | None:
                if name == _database_name():
                    type(self).lstat_calls += 1
                    if type(self).lstat_calls == 2:
                        raise StateFilesystemError("injected post-open stat failure")
                return super()._lstat(name)

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = FailingFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_open = os.open
            original_close = os.close
            opened_fd: int | None = None
            failed = True

            def open_file(path: object, *args: object, **kwargs: object) -> int:
                nonlocal opened_fd
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == _database_name():
                    opened_fd = fd
                return fd

            def close(fd: int) -> None:
                nonlocal failed
                if fd == opened_fd and failed:
                    failed = False
                    raise OSError("injected open helper close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.doctor.os.open", side_effect=open_file),
                    mock.patch("agent_team.doctor.os.close", side_effect=close),
                    self.assertRaises(StateFilesystemError),
                ):
                    filesystem.open_existing_regular(_database_name())
                self.assertIsNotNone(opened_fd)
                with self.assertRaises(OSError):
                    os.fstat(opened_fd)  # type: ignore[arg-type]
            finally:
                filesystem.close()

    def test_reports_and_inventory_are_immutable_and_sorted(self) -> None:
        report = DoctorReport(
            observed_state="EMPTY_ROOT",
            confidence="HIGH",
            owner=None,
            safe_action="OPERATOR_REVIEW",
            forbidden_mutations=("claim", "restore"),
        )
        with self.assertRaises(FrozenInstanceError):
            report.confidence = "LOW"  # type: ignore[misc]
        first = FilesystemEntry(
            name="a",
            file_type="regular",
            uid=os.getuid(),
            mode=0o600,
            nlink=1,
            device=1,
            inode=2,
            size=1,
            mtime_ns=3,
            ctime_ns=4,
            digest="sha256:" + hashlib.sha256(b"x").hexdigest(),
        )
        second = FilesystemEntry(
            name="b",
            file_type="directory",
            uid=os.getuid(),
            mode=0o700,
            nlink=1,
            device=1,
            inode=3,
            size=0,
            mtime_ns=3,
            ctime_ns=4,
            digest=None,
        )
        inventory = FilesetInventory(
            root_identity=(1, 2),
            lifetime_gate_identity=None,
            marker_identity=None,
            ledger_identity=None,
            entries=(second, first),
        )
        self.assertEqual(("a", "b"), tuple(entry.name for entry in inventory.entries))
        with self.assertRaises(FrozenInstanceError):
            inventory.entries = ()  # type: ignore[misc]


class DoctorFilesystemTest(unittest.TestCase):
    def test_restore_preflight_read_fd_is_handed_to_filesystem_retry_registry(
        self,
    ) -> None:
        for target_name in (LEDGER_NAME, TOMBSTONE_NAME):
            with (
                self.subTest(target_name=target_name),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _make_root(temporary)
                _write_restore_pair(root, terminal=True)
                filesystem = StateFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
                original_open = os.open
                original_close = os.close
                opened: list[int] = []
                failed = [False]
                armed = [False]

                def capture_open(
                    *args: object,
                    _original_open: Callable[..., int] = original_open,
                    _opened: list[int] = opened,
                    _armed: list[bool] = armed,
                    _target_name: str = target_name,
                    **kwargs: object,
                ) -> int:
                    fd = _original_open(*args, **kwargs)
                    if _armed[0] and args and args[0] == _target_name:
                        _opened.append(fd)
                    return fd

                def fail_close(
                    fd: int,
                    _original_close: Callable[[int], None] = original_close,
                    _opened: list[int] = opened,
                    _armed: list[bool] = armed,
                    _failed: list[bool] = failed,
                ) -> None:
                    if _armed[0] and fd in _opened and not _failed[0]:
                        _failed[0] = True
                        raise OSError("simulated doctor preflight close uncertainty")
                    _original_close(fd)

                try:
                    from agent_team import recovery

                    original_preflight = recovery._normal_open_preflight

                    def arm_preflight(
                        *args: object,
                        _original_preflight: Callable[..., object] = original_preflight,
                        _armed: list[bool] = armed,
                        **kwargs: object,
                    ) -> object:
                        _armed[0] = True
                        return _original_preflight(*args, **kwargs)

                    with (
                        mock.patch.object(
                            recovery,
                            "_normal_open_preflight",
                            side_effect=arm_preflight,
                        ),
                        mock.patch(
                            "agent_team.recovery.os.open",
                            side_effect=capture_open,
                        ),
                        mock.patch(
                            "agent_team.recovery.os.close",
                            side_effect=fail_close,
                        ),
                        self.assertRaises(CleanupUncertaintyError) as raised,
                    ):
                        ReadOnlyDoctor(
                            filesystem=filesystem,
                            marker_name=MARKER_NAME,
                            ledger_name=LEDGER_NAME,
                        ).inspect(root, "op-restore")
                    self.assertEqual(1, len(filesystem._orphan_fds))
                    cleanup_owner = raised.exception.cleanup_owner
                    self.assertIsNotNone(cleanup_owner)
                    assert cleanup_owner is not None
                    cleanup_owner.retry_cleanup()
                    self.assertEqual([], filesystem._orphan_fds)
                    second_report = ReadOnlyDoctor(
                        filesystem=filesystem,
                        marker_name=MARKER_NAME,
                        ledger_name=LEDGER_NAME,
                    ).inspect(root, "op-restore")
                    self.assertEqual("MISSING", second_report.observed_state)
                    self.assertEqual([], filesystem._orphan_fds)
                finally:
                    filesystem.close()
                self.assertEqual([], filesystem._orphan_fds)
                with self.assertRaises(OSError):
                    os.fstat(opened[0])

    def test_missing_root_is_reported_without_creating_anything(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = Path(temporary) / "missing"
            doctor = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            report = doctor.inspect(root, "op-missing")
            self.assertEqual("MISSING_ROOT", report.observed_state)
            self.assertEqual(
                (
                    "claim",
                    "heartbeat",
                    "reclaim",
                    "reserve_fence",
                    "execute_effect",
                    "record_receipt",
                    "complete",
                    "recover",
                    "force_recover",
                    "resolve_unknown",
                    "rebind_receipt",
                    "checkpoint",
                    "cleanup",
                    "restore",
                ),
                report.forbidden_mutations,
            )
            self.assertFalse(root.exists())
            self.assertFalse((root.parent / ".coordination-lifetime.lock").exists())

    def test_empty_root_is_reported_without_creating_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            before = _root_listing(root)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-empty")
            self.assertEqual("EMPTY_ROOT", report.observed_state)
            self.assertEqual(before, _root_listing(root))

    def test_existing_store_status_is_read_without_mutating_fileset(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-intent",
                    effect_key="effect/op-intent",
                    actor="main",
                    clock_ns=1,
                )
            before_root = _root_listing(root)
            before_gate = (root.parent / ".coordination-lifetime.lock").stat()
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-intent")
            self.assertEqual("INTENT_ONLY", report.observed_state)
            self.assertIsNone(report.owner)
            self.assertEqual(before_root, _root_listing(root))
            after_gate = (root.parent / ".coordination-lifetime.lock").stat()
            self.assertEqual(
                (before_gate.st_dev, before_gate.st_ino),
                (after_gate.st_dev, after_gate.st_ino),
            )

    def test_missing_lifetime_gate_never_gets_created_for_existing_store(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-no-gate",
                    effect_key="effect/op-no-gate",
                    actor="main",
                    clock_ns=1,
                )
            gate = root.parent / ".coordination-lifetime.lock"
            gate.unlink()
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-no-gate")
            self.assertEqual("UNREADABLE", report.observed_state)
            self.assertEqual("LOW", report.confidence)
            self.assertEqual("OPERATOR_REVIEW", report.safe_action)
            self.assertFalse(gate.exists())

    def test_state_filesystem_inventory_keeps_unknown_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            unknown = root / "unrelated.txt"
            unknown.write_bytes(b"keep")
            unknown.chmod(0o600)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            try:
                inventory = filesystem.inventory()
            finally:
                filesystem.close()
            self.assertEqual(
                ("unrelated.txt",), tuple(item.name for item in inventory.entries)
            )

    def test_layout_names_must_be_explicit_single_basename_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            with self.assertRaises(ValueError):
                StateFilesystem.open_existing(
                    root,
                    marker_name="writer/marker",
                    ledger_name=LEDGER_NAME,
                )
            with self.assertRaises(ValueError):
                ReadOnlyDoctor(marker_name=MARKER_NAME).inspect(
                    root,
                    "op-layout",
                )

    def test_unsafe_known_fifo_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            os.mkfifo(root / f"{_database_name()}-wal", mode=0o600)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-fifo")
            self.assertEqual("UNSAFE_SIDECAR", report.observed_state)

    def test_unsafe_unknown_fifo_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            os.mkfifo(root / "unrelated.pipe", mode=0o600)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-fifo")
            self.assertEqual("UNSAFE_SIDECAR", report.observed_state)

    def test_unsafe_unknown_hardlink_is_rejected_without_opening_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            source = root / "source.txt"
            source.write_bytes(b"keep")
            source.chmod(0o600)
            os.link(source, root / "unrelated-hardlink.txt")
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-hardlink")
            self.assertEqual("UNSAFE_SIDECAR", report.observed_state)

    def test_nonzero_wal_is_reported_before_immutable_database_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            database = root / _database_name()
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE marker(value TEXT)")
                connection.execute("INSERT INTO marker(value) VALUES ('pending')")
                connection.commit()
                wal = root / f"{database.name}-wal"
                self.assertTrue(wal.exists())
                for sidecar in root.iterdir():
                    sidecar.chmod(0o600)
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-wal")
                self.assertEqual("WAL_PENDING", report.observed_state)
            finally:
                connection.close()

    def test_schema_mismatch_is_classified_without_store_constructor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            database = root / _database_name()
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE not_coordination(value TEXT)")
                connection.commit()
            finally:
                connection.close()
            database.chmod(0o600)
            gate = root.parent / ".coordination-lifetime.lock"
            gate.write_bytes(b"")
            gate.chmod(0o600)
            with mock.patch.object(
                CoordinationStore,
                "__init__",
                side_effect=AssertionError("doctor must not construct a store"),
            ):
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-schema")
            self.assertEqual("SCHEMA_INVALID", report.observed_state)

    def test_pending_ledger_blocks_missing_primary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            digest = "sha256:" + "a" * 64
            ledger = root / LEDGER_NAME
            ledger.write_text(
                '{"version":1,"sequence":1,"phase":"RESTORE_PREPARED",'
                f'"restore_generation":1,"recovery_epoch":1,"fencing_token_floor":1,"backup_digest":"{digest}",'
                '"actor":"operator","audit_ref":"audit/1"}\n',
                encoding="utf-8",
            )
            ledger.chmod(0o600)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-restore")
            self.assertEqual("RESTORE_INCOMPLETE", report.observed_state)

    def test_tombstone_only_restore_pair_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            _write_restore_pair(root, include_ledger=False)
            before = _root_listing(root)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-restore")
            self.assertEqual("RESTORE_INCOMPLETE", report.observed_state)
            self.assertEqual("HIGH", report.confidence)
            self.assertEqual("OPERATOR_REVIEW", report.safe_action)
            self.assertEqual(before, _root_listing(root))

    def test_malformed_tombstone_restore_pair_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            tombstone = root / TOMBSTONE_NAME
            tombstone.write_bytes(b'{"version":1,"sequence":\n')
            tombstone.chmod(0o600)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-restore")
            self.assertEqual("RESTORE_INCOMPLETE", report.observed_state)
            self.assertEqual("HIGH", report.confidence)
            self.assertEqual("OPERATOR_REVIEW", report.safe_action)

    def test_unavailable_restore_preflight_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            _write_restore_pair(root, include_ledger=False)
            from agent_team import recovery

            with mock.patch.object(
                recovery,
                "_normal_open_preflight",
                side_effect=RuntimeError("preflight unavailable"),
            ):
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-restore")
            self.assertEqual("RESTORE_INCOMPLETE", report.observed_state)
            self.assertEqual("HIGH", report.confidence)
            self.assertEqual("OPERATOR_REVIEW", report.safe_action)

    def test_prepared_restore_pair_is_incomplete_and_uses_recovery_preflight(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            _write_restore_pair(root)
            from agent_team import recovery

            with mock.patch.object(
                recovery,
                "_normal_open_preflight",
                wraps=recovery._normal_open_preflight,
            ) as preflight:
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-restore")
            self.assertEqual("RESTORE_INCOMPLETE", report.observed_state)
            self.assertEqual(1, preflight.call_count)

    def test_committed_restore_pair_proceeds_without_writers_or_provider(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            _write_restore_pair(root, terminal=True)
            before = _root_listing(root)
            from agent_team import recovery

            with (
                mock.patch.object(
                    recovery,
                    "_normal_open_preflight",
                    wraps=recovery._normal_open_preflight,
                ) as preflight,
                mock.patch.object(
                    CoordinationStore,
                    "__init__",
                    side_effect=AssertionError("doctor must not construct a store"),
                ),
                mock.patch(
                    "agent_team.doctor.sqlite3.connect",
                    side_effect=AssertionError("doctor must not open SQLite"),
                ),
            ):
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-restore")
            self.assertEqual("MISSING", report.observed_state)
            self.assertEqual(1, preflight.call_count)
            self.assertEqual(before, _root_listing(root))

    def test_restore_preflight_fileset_race_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)

            class TombstoneAppearsFilesystem(StateFilesystem):
                created = False

                def _fault(self, point: str) -> None:
                    if point == "before_final_inventory" and not self.created:
                        self.created = True
                        tombstone = self.state_root / TOMBSTONE_NAME
                        tombstone.write_bytes(b"race")
                        tombstone.chmod(0o600)

            filesystem = TombstoneAppearsFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            from agent_team import recovery

            try:
                with mock.patch.object(
                    recovery,
                    "_normal_open_preflight",
                    wraps=recovery._normal_open_preflight,
                ) as preflight:
                    report = ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root,
                        "op-restore",
                    )
            finally:
                filesystem.close()
            self.assertEqual("UNREADABLE", report.observed_state)
            self.assertEqual(1, preflight.call_count)

    def test_malformed_ledger_is_unreadable_and_does_not_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            ledger = root / LEDGER_NAME
            ledger.write_bytes(b'{"version":1,"sequence":')
            ledger.chmod(0o600)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-restore")
            self.assertEqual("UNREADABLE", report.observed_state)

    def test_ledger_sequence_gap_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            digest = "sha256:" + "b" * 64
            records = (
                (
                    '{"version":1,"sequence":1,"phase":"RESTORE_PREPARED",'
                    f'"restore_generation":1,"recovery_epoch":1,"fencing_token_floor":1,"backup_digest":"{digest}",'
                    '"actor":"operator","audit_ref":"audit/1"}'
                ),
                (
                    '{"version":1,"sequence":3,"phase":"RESTORE_COMMITTED",'
                    f'"restore_generation":1,"recovery_epoch":1,"fencing_token_floor":1,"backup_digest":"{digest}",'
                    '"actor":"operator","audit_ref":"audit/1"}'
                ),
            )
            ledger = root / LEDGER_NAME
            ledger.write_text("\n".join(records) + "\n", encoding="utf-8")
            ledger.chmod(0o600)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-restore")
            self.assertEqual("UNREADABLE", report.observed_state)

    def test_marker_and_ledger_identities_are_exposed_without_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            marker = root / MARKER_NAME
            ledger = root / LEDGER_NAME
            marker.write_bytes(WRITER_MARKER_CLEAN_CONTENT)
            ledger.write_bytes(b"ledger")
            marker.chmod(0o600)
            ledger.chmod(0o600)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            try:
                inventory = filesystem.inventory()
            finally:
                filesystem.close()
            self.assertIsNotNone(inventory.marker_identity)
            self.assertIsNotNone(inventory.ledger_identity)
            self.assertFalse(hasattr(inventory, "state_root"))

    def test_primary_mode_mismatch_is_not_opened(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            database = root / _database_name()
            database.write_bytes(b"not sqlite")
            database.chmod(0o644)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-unsafe")
            self.assertEqual("UNSAFE_SIDECAR", report.observed_state)

    def test_status_mapping_preserves_unknown_effect_and_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-status",
                    effect_key="effect/op-status",
                    actor="main",
                    clock_ns=1,
                )
                claim = store.claim(
                    "op-status",
                    owner="owner-a",
                    provider_id="provider/test",
                    lease_ttl_ns=10,
                    now_ns=1,
                )
            pending = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-status")
            self.assertEqual("UNKNOWN_EFFECT", pending.observed_state)
            self.assertEqual("owner-a", pending.owner)
            self.assertEqual("OPERATOR_REVIEW", pending.safe_action)
            self.assertEqual("LOW", pending.confidence)
            self.assertEqual(claim.attempt, 1)

    def test_receipted_and_completed_statuses_keep_safe_action_typed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            clock = FakeClock(100)
            with CoordinationStore(root, clock=clock) as store:
                store.create_intent(
                    "op-receipt",
                    effect_key="effect/op-receipt",
                    actor="main",
                    clock_ns=100,
                )
                claim = store.claim(
                    "op-receipt",
                    owner="owner-a",
                    provider_id="provider/test",
                    lease_ttl_ns=10,
                    now_ns=100,
                )
                provider = FakeProvider()
                claim = store.reserve_fence(claim, provider)
                store.execute_effect(claim, provider, now_ns=102)
            marker = root / MARKER_NAME
            marker.write_bytes(WRITER_MARKER_CLEAN_CONTENT)
            marker.chmod(0o600)
            receipt_report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-receipt")
            self.assertEqual("RECEIPTED", receipt_report.observed_state)
            self.assertEqual("VERIFY_RECEIPT_THEN_COMPLETE", receipt_report.safe_action)
            with CoordinationStore(root) as store:
                snapshot = store._recovery_snapshot("op-receipt")
                receipt = snapshot.verified_receipt_identity
                assert receipt is not None
                store.complete(receipt, now_ns=103)
            completed_report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-receipt")
            self.assertEqual("COMPLETED", completed_report.observed_state)
            self.assertEqual("NONE", completed_report.safe_action)

    def test_restore_preserved_statuses_are_readable_after_epoch_invalidation(
        self,
    ) -> None:
        expected_states = {
            "FENCE_PENDING": "UNKNOWN_EFFECT",
            "FENCE_RESERVATION_STARTED": "UNKNOWN_EFFECT",
            "CLAIMED": "UNKNOWN_EFFECT",
            "EFFECT_PREPARED": "UNKNOWN_EFFECT",
            "UNKNOWN_EFFECT": "UNKNOWN_EFFECT",
            "UNKNOWN": "UNKNOWN_EFFECT",
            "COMPLETED": "COMPLETED",
        }
        for status, expected_state in expected_states.items():
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _create_restore_preserved_root(temporary, status)
                before = _root_listing(root)
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-restored")
                self.assertEqual(expected_state, report.observed_state)
                self.assertEqual("owner-a", report.owner)
                self.assertEqual(before, _root_listing(root))
                self.assertEqual(
                    "NONE" if status == "COMPLETED" else "OPERATOR_REVIEW",
                    report.safe_action,
                )

    def test_committed_generation_remains_observable_after_later_abort(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_restore_preserved_root(temporary, "FENCE_PENDING")
            with CoordinationStore(root, clock=FakeClock(250)) as store:
                store.create_intent(
                    "op-before-later-abort",
                    effect_key="effect/op-before-later-abort",
                    provider_id="provider/test",
                    actor="main",
                    clock_ns=250,
                )
                store.claim(
                    "op-before-later-abort",
                    owner="owner-before-later-abort",
                    provider_id="provider/test",
                    lease_ttl_ns=100,
                    now_ns=250,
                )
            second_artifact = SQLiteBackup(root, busy_timeout_ms=100).create("second")
            _leave_restore_prepared(
                root,
                second_artifact,
                audit_ref="audit/restore-generation-two",
                clock=FakeClock(300),
            )
            _abort_restore_generation(root, second_artifact)
            before = _root_listing(root)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-restored")
            self.assertEqual("UNKNOWN_EFFECT", report.observed_state)
            self.assertEqual("owner-a", report.owner)
            self.assertEqual(before, _root_listing(root))

    def test_committed_generation_remains_observable_after_unrelated_token_advance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_restore_preserved_root(temporary, "FENCE_PENDING")
            with CoordinationStore(root, clock=FakeClock(400)) as store:
                store.create_intent(
                    "op-after-restore",
                    effect_key="effect/op-after-restore",
                    provider_id="provider/test",
                    actor="main",
                    clock_ns=400,
                )
                store.claim(
                    "op-after-restore",
                    owner="owner-after-restore",
                    provider_id="provider/test",
                    lease_ttl_ns=100,
                    now_ns=400,
                )
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-restored")
            self.assertEqual("UNKNOWN_EFFECT", report.observed_state)
            self.assertEqual("owner-a", report.owner)

    def test_post_restore_attempt_or_receipt_token_above_floor_is_unreadable(
        self,
    ) -> None:
        mutations = (
            (
                "UPDATE operation_attempts SET fencing_token = "
                "(SELECT value + 1 FROM store_meta WHERE key = 'fencing_token_floor') "
                "WHERE operation_id = 'op-restored'"
            ),
            (
                "UPDATE effect_receipts SET fencing_token = "
                "(SELECT value + 1 FROM store_meta WHERE key = 'fencing_token_floor') "
                "WHERE operation_id = 'op-restored'"
            ),
        )
        for statement in mutations:
            with (
                self.subTest(statement=statement),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _create_restore_preserved_root(temporary, "COMPLETED")
                _mutate_and_checkpoint(root, statement)
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-restored")
                self.assertEqual("UNREADABLE", report.observed_state)

    def test_custom_ledger_never_authorizes_restore_epoch_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_restore_preserved_root(temporary, "FENCE_PENDING")
            records = [
                cast(dict[str, object], json.loads(line))
                for line in (root / LEDGER_NAME).read_bytes().splitlines()
            ]
            for record in records:
                record["fencing_token_floor"] = (
                    cast(int, record["fencing_token_floor"]) + 1
                )
            custom = root / "custom.ledger"
            custom.write_bytes(
                b"".join(
                    json.dumps(
                        record, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    + b"\n"
                    for record in records
                )
            )
            custom.chmod(0o600)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name="custom.ledger",
            ).inspect(root, "op-restored")
            self.assertEqual("UNKNOWN_EFFECT", report.observed_state)

    def test_custom_ledger_generation_gap_after_abort_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_restore_preserved_root(temporary, "FENCE_PENDING")
            canonical_records = [
                cast(dict[str, object], json.loads(line))
                for line in (root / LEDGER_NAME).read_bytes().splitlines()
            ]
            first = dict(canonical_records[0])
            aborted = dict(first)
            aborted["sequence"] = 2
            aborted["phase"] = "RESTORE_ABORTED"
            gap_prepared = dict(aborted)
            gap_prepared["sequence"] = 3
            gap_prepared["phase"] = "RESTORE_PREPARED"
            gap_prepared["restore_generation"] = 3
            gap_replaced = dict(gap_prepared)
            gap_replaced["sequence"] = 4
            gap_replaced["phase"] = "RESTORE_REPLACED"
            gap_committed = dict(gap_replaced)
            gap_committed["sequence"] = 5
            gap_committed["phase"] = "RESTORE_COMMITTED"
            custom = root / "custom.ledger"
            custom.write_bytes(
                b"".join(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                    for record in (
                        first,
                        aborted,
                        gap_prepared,
                        gap_replaced,
                        gap_committed,
                    )
                )
            )
            custom.chmod(0o600)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name="custom.ledger",
            ).inspect(root, "op-restored")
            self.assertEqual("UNREADABLE", report.observed_state)

    def test_internal_inspect_cleanup_failure_keeps_owner_for_next_inspect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_close = os.close
            original_flock = fcntl.flock
            gate_fds: list[int] = []
            database_fds: list[int] = []

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == ".coordination-lifetime.lock":
                    gate_fds.append(fd)
                elif path == _database_name():
                    database_fds.append(fd)
                return fd

            def fail_owned_close(fd: int) -> None:
                if fd in gate_fds or fd in database_fds:
                    raise OSError("persistent internal doctor close failure")
                original_close(fd)

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if fd in gate_fds and operation & fcntl.LOCK_UN:
                    raise OSError("persistent internal doctor gate unlock failure")
                original_flock(fd, operation)

            doctor = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_owned_close,
                ),
                mock.patch(
                    "agent_team.doctor.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                self.assertRaises(StateFilesystemError) as raised,
            ):
                doctor.inspect(root, "op-identity-mismatch")
            self.assertTrue(gate_fds)
            self.assertTrue(database_fds)
            cleanup_owner = getattr(raised.exception, "cleanup_owner", None)
            self.assertIsNotNone(cleanup_owner)
            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_owned_close,
                ),
                mock.patch(
                    "agent_team.doctor.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                self.assertRaises(StateFilesystemError),
            ):
                doctor.inspect(root, "op-identity-mismatch")
            assert cleanup_owner is not None
            cleanup_owner.retry_cleanup()
            report = doctor.inspect(root, "op-identity-mismatch")
            self.assertEqual("UNKNOWN_EFFECT", report.observed_state)

    def test_convenience_doctor_keeps_cleanup_owner_after_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_close = os.close
            original_flock = fcntl.flock
            gate_fds: list[int] = []

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == ".coordination-lifetime.lock":
                    gate_fds.append(fd)
                return fd

            def fail_gate_close(fd: int) -> None:
                if fd in gate_fds:
                    raise OSError("persistent convenience doctor close failure")
                original_close(fd)

            def fail_gate_unlock(fd: int, operation: int) -> None:
                if fd in gate_fds and operation & fcntl.LOCK_UN:
                    raise OSError("persistent convenience doctor unlock failure")
                original_flock(fd, operation)

            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=fail_gate_close,
                ),
                mock.patch(
                    "agent_team.doctor.fcntl.flock",
                    side_effect=fail_gate_unlock,
                ),
                self.assertRaises(StateFilesystemError) as raised,
            ):
                run_doctor(
                    root,
                    "op-identity-mismatch",
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertTrue(gate_fds)
            cleanup_owner = raised.exception.cleanup_owner
            self.assertIsNotNone(cleanup_owner)
            assert cleanup_owner is not None
            cleanup_owner.retry_cleanup()
            report = run_doctor(
                root,
                "op-identity-mismatch",
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            self.assertEqual("UNKNOWN_EFFECT", report.observed_state)

    def test_actual_close_then_error_on_database_is_typed_directly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            original_open = os.open
            original_close = os.close
            opened: list[int] = []
            failed = False

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == _database_name():
                    opened.append(fd)
                return fd

            def close_once(fd: int) -> None:
                nonlocal failed
                if fd in opened and not failed:
                    failed = True
                    original_close(fd)
                    raise OSError("actual database close then error")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                    mock.patch(
                        "agent_team.doctor.os.close",
                        side_effect=close_once,
                    ),
                    self.assertRaises(StateFilesystemError) as raised,
                ):
                    ReadOnlyDoctor(filesystem=filesystem).inspect(
                        root,
                        "op-identity-mismatch",
                    )
                self.assertIsInstance(raised.exception, CleanupUncertaintyError)
                self.assertTrue(opened)
                self.assertIsNone(raised.exception.cleanup_owner)
                raised.exception.retry_cleanup()
            finally:
                filesystem.close()

    def test_actual_close_then_error_on_database_survives_convenience_wrapper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            original_open = os.open
            original_close = os.close
            opened: list[int] = []
            failed = False

            def capture_open(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> int:
                fd = original_open(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == _database_name():
                    opened.append(fd)
                return fd

            def close_once(fd: int) -> None:
                nonlocal failed
                if fd in opened and not failed:
                    failed = True
                    original_close(fd)
                    raise OSError("actual database close then error")
                original_close(fd)

            with (
                mock.patch("agent_team.doctor.os.open", side_effect=capture_open),
                mock.patch(
                    "agent_team.doctor.os.close",
                    side_effect=close_once,
                ),
                self.assertRaises(StateFilesystemError) as raised,
            ):
                run_doctor(
                    root,
                    "op-identity-mismatch",
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertIsInstance(raised.exception, CleanupUncertaintyError)
            self.assertTrue(opened)
            self.assertIsNone(raised.exception.cleanup_owner)
            raised.exception.retry_cleanup()

    def test_actual_close_then_error_on_preflight_files_is_typed_directly(self) -> None:
        for target_name in (LEDGER_NAME, TOMBSTONE_NAME):
            with (
                self.subTest(target_name=target_name),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _make_root(temporary)
                _write_restore_pair(root, terminal=True)
                filesystem = StateFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
                original_open = os.open
                original_close = os.close
                opened: list[int] = []
                failed = False

                def capture_open(
                    path: object,
                    *args: object,
                    _original_open: Callable[..., int] = original_open,
                    _opened: list[int] = opened,
                    _target_name: str = target_name,
                    **kwargs: object,
                ) -> int:
                    fd = _original_open(path, *args, **kwargs)
                    if path == _target_name:
                        _opened.append(fd)
                    return fd

                def close_once(
                    fd: int,
                    _original_close: Callable[[int], None] = original_close,
                    _opened: list[int] = opened,
                ) -> None:
                    nonlocal failed
                    if fd in _opened and not failed:
                        failed = True
                        _original_close(fd)
                        raise OSError("actual preflight close then error")
                    _original_close(fd)

                try:
                    with (
                        mock.patch(
                            "agent_team.recovery.os.open",
                            side_effect=capture_open,
                        ),
                        mock.patch(
                            "agent_team.recovery.os.close",
                            side_effect=close_once,
                        ),
                        self.assertRaises(StateFilesystemError) as raised,
                    ):
                        ReadOnlyDoctor(filesystem=filesystem).inspect(
                            root,
                            "op-restore",
                        )
                    self.assertIsInstance(raised.exception, CleanupUncertaintyError)
                    self.assertTrue(opened)
                    self.assertIsNone(raised.exception.cleanup_owner)
                    raised.exception.retry_cleanup()
                finally:
                    filesystem.close()

    def test_actual_close_then_error_on_preflight_files_survives_convenience_wrapper(
        self,
    ) -> None:
        for target_name in (LEDGER_NAME, TOMBSTONE_NAME):
            with (
                self.subTest(target_name=target_name),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _make_root(temporary)
                _write_restore_pair(root, terminal=True)
                original_open = os.open
                original_close = os.close
                opened: list[int] = []
                failed = False

                def capture_open(
                    path: object,
                    *args: object,
                    _original_open: Callable[..., int] = original_open,
                    _opened: list[int] = opened,
                    _target_name: str = target_name,
                    **kwargs: object,
                ) -> int:
                    fd = _original_open(path, *args, **kwargs)
                    if path == _target_name:
                        _opened.append(fd)
                    return fd

                def close_once(
                    fd: int,
                    _original_close: Callable[[int], None] = original_close,
                    _opened: list[int] = opened,
                ) -> None:
                    nonlocal failed
                    if fd in _opened and not failed:
                        failed = True
                        _original_close(fd)
                        raise OSError("actual preflight close then error")
                    _original_close(fd)

                with (
                    mock.patch(
                        "agent_team.recovery.os.open",
                        side_effect=capture_open,
                    ),
                    mock.patch(
                        "agent_team.recovery.os.close",
                        side_effect=close_once,
                    ),
                    self.assertRaises(StateFilesystemError) as raised,
                ):
                    run_doctor(
                        root,
                        "op-restore",
                        marker_name=MARKER_NAME,
                        ledger_name=LEDGER_NAME,
                    )
                self.assertIsInstance(raised.exception, CleanupUncertaintyError)
                self.assertTrue(opened)
                self.assertIsNone(raised.exception.cleanup_owner)
                raised.exception.retry_cleanup()

    def test_custom_ledger_without_canonical_pair_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            _write_restore_pair(root, terminal=True)
            (root / LEDGER_NAME).rename(root / "custom.ledger")
            (root / TOMBSTONE_NAME).unlink()
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name="custom.ledger",
            ).inspect(root, "op-restore")
            self.assertEqual("RESTORE_INCOMPLETE", report.observed_state)

    def test_custom_ledger_cannot_hide_malformed_canonical_ledger(self) -> None:
        for custom_present in (False, True):
            with (
                self.subTest(custom_present=custom_present),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _create_restore_preserved_root(temporary, "COMPLETED")
                canonical = root / LEDGER_NAME
                canonical_bytes = canonical.read_bytes()
                if custom_present:
                    custom = root / "custom.ledger"
                    custom.write_bytes(canonical_bytes)
                    custom.chmod(0o600)
                canonical.write_bytes(b"malformed canonical ledger\n")
                canonical.chmod(0o600)
                (root / TOMBSTONE_NAME).unlink()
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name="custom.ledger",
                ).inspect(root, "op-restored")
                self.assertEqual("UNREADABLE", report.observed_state)

    def test_aborted_only_history_does_not_authorize_epoch_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root, artifact = _create_restore_artifact_root(temporary, "FENCE_PENDING")
            _leave_restore_prepared(
                root,
                artifact,
                audit_ref="audit/restore-aborted-only",
                clock=FakeClock(200),
            )
            _abort_restore_generation(root, artifact)
            _mutate_and_checkpoint(
                root,
                "UPDATE operations SET recovery_epoch = recovery_epoch + 1 "
                "WHERE operation_id = 'op-restored'",
            )
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-restored")
            self.assertEqual("UNREADABLE", report.observed_state)

    def test_existing_marker_lock_is_reported_as_writer_active(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            marker = root / MARKER_NAME
            marker.write_bytes(WRITER_MARKER_CLEAN_CONTENT)
            marker.chmod(0o600)
            marker_fd = os.open(marker, os.O_RDWR)
            try:
                fcntl.flock(marker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-writer")
            finally:
                fcntl.flock(marker_fd, fcntl.LOCK_UN)
                os.close(marker_fd)
            self.assertEqual("WRITER_ACTIVE", report.observed_state)

    def test_database_inode_swap_after_lstat_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-race",
                    effect_key="effect/op-race",
                    actor="main",
                    clock_ns=1,
                )

            class SwapFilesystem(StateFilesystem):
                swapped = False

                def _fault(self, point: str) -> None:
                    if point != "after_db_lstat" or self.swapped:
                        return
                    self.swapped = True
                    database = self.state_root / _database_name()
                    moved = self.state_root / "coordination.sqlite3-old"
                    database.rename(moved)
                    replacement = self.state_root / _database_name()
                    replacement.write_bytes(b"replacement")
                    replacement.chmod(0o600)

            filesystem = SwapFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            try:
                report = ReadOnlyDoctor(filesystem=filesystem).inspect(
                    root,
                    "op-race",
                )
            finally:
                filesystem.close()
            self.assertEqual("UNREADABLE", report.observed_state)
            self.assertTrue((root / "coordination.sqlite3-old").exists())

    def test_root_inode_swap_after_lstat_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            parent = Path(os.path.realpath(temporary))
            root = parent / "state"
            root.mkdir(mode=0o700)

            class SwapFilesystem(StateFilesystem):
                swapped = False

                def _fault(self, point: str) -> None:
                    if point != "after_root_lstat" or self.swapped:
                        return
                    self.swapped = True
                    moved = self.state_root.with_name("state-old")
                    self.state_root.rename(moved)
                    self.state_root.mkdir(mode=0o700)

            with self.assertRaises(UnstableSnapshotError):
                SwapFilesystem.open_existing(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                )
            self.assertTrue((parent / "state-old").is_dir())
            self.assertTrue(root.is_dir())

    def test_open_flags_are_read_only_and_no_create(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            calls: list[int] = []
            original_open = os.open

            def checked_open(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                calls.append(flags)
                self.assertFalse(flags & os.O_CREAT)
                self.assertFalse(flags & os.O_RDWR)
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch("os.open", side_effect=checked_open):
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-flags")
            self.assertEqual("EMPTY_ROOT", report.observed_state)
            self.assertTrue(calls)

    def test_receipt_or_lease_marker_corruption_is_unreadable(self) -> None:
        mutations = (
            (
                "UPDATE operation_attempts SET fence_proof_version = NULL, "
                "fence_proof_ref = NULL WHERE operation_id = "
                "'op-receipt-corrupt'"
            ),
            (
                "UPDATE operation_attempts SET fence_started_ns = NULL "
                "WHERE operation_id = 'op-receipt-corrupt'"
            ),
            (
                "UPDATE operation_attempts SET effect_started_ns = NULL "
                "WHERE operation_id = 'op-receipt-corrupt'"
            ),
            (
                "UPDATE effect_receipts SET proof_ref = 'forged-proof' "
                "WHERE operation_id = 'op-receipt-corrupt'"
            ),
        )
        for statement in mutations:
            with (
                self.subTest(statement=statement),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _create_receipted_root(temporary)
                _mutate_and_checkpoint(root, statement)
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-receipt-corrupt")
                self.assertEqual("UNREADABLE", report.observed_state)
                self.assertEqual("LOW", report.confidence)
                self.assertEqual("OPERATOR_REVIEW", report.safe_action)

    def test_ledger_wire_format_is_strict_jsonl(self) -> None:
        digest = "sha256:" + "c" * 64
        record = {
            "version": 1,
            "sequence": 1,
            "phase": "RESTORE_PREPARED",
            "restore_generation": 1,
            "recovery_epoch": 1,
            "fencing_token_floor": 1,
            "backup_digest": digest,
            "actor": "operator",
            "audit_ref": "audit/1",
        }
        encodings = (
            ("array", json.dumps([record], separators=(",", ":")) + "\n"),
            ("missing-final-newline", json.dumps(record, separators=(",", ":"))),
            (
                "blank-line",
                json.dumps(record, separators=(",", ":")) + "\n\n",
            ),
            (
                "leading-whitespace-line",
                " " + json.dumps(record, separators=(",", ":")) + "\n",
            ),
        )
        for name, payload in encodings:
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _make_root(temporary)
                ledger = root / LEDGER_NAME
                ledger.write_text(payload, encoding="utf-8")
                ledger.chmod(0o600)
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-ledger")
                self.assertEqual("UNREADABLE", report.observed_state)

    def test_ledger_phase_state_machine_allows_next_generation_only(self) -> None:
        digest = "sha256:" + "d" * 64

        def record(sequence: int, phase: str, generation: int) -> str:
            return json.dumps(
                {
                    "version": 1,
                    "sequence": sequence,
                    "phase": phase,
                    "restore_generation": generation,
                    "recovery_epoch": generation,
                    "fencing_token_floor": generation,
                    "backup_digest": digest,
                    "actor": "operator",
                    "audit_ref": f"audit/{generation}",
                },
                separators=(",", ":"),
            )

        invalid_ledgers = (
            ("terminal-first", (record(1, "RESTORE_COMMITTED", 1),)),
            (
                "same-generation-after-terminal",
                (
                    record(1, "RESTORE_PREPARED", 1),
                    record(2, "RESTORE_COMMITTED", 1),
                    record(3, "RESTORE_PREPARED", 1),
                ),
            ),
            (
                "phase-regression",
                (
                    record(1, "RESTORE_PREPARED", 1),
                    record(2, "RESTORE_REPLACED", 1),
                    record(3, "RESTORE_PREPARED", 1),
                ),
            ),
        )
        for name, records in invalid_ledgers:
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _make_root(temporary)
                ledger = root / LEDGER_NAME
                ledger.write_text("\n".join(records) + "\n", encoding="utf-8")
                ledger.chmod(0o600)
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-ledger")
                self.assertEqual("UNREADABLE", report.observed_state)

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            ledger = root / LEDGER_NAME
            ledger.write_text(
                "\n".join(
                    (
                        record(1, "RESTORE_PREPARED", 1),
                        record(2, "RESTORE_REPLACED", 1),
                        record(3, "RESTORE_COMMITTED", 1),
                        record(4, "RESTORE_PREPARED", 2),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            ledger.chmod(0o600)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-ledger")
            self.assertEqual("RESTORE_INCOMPLETE", report.observed_state)

    def test_lstat_to_fifo_swap_for_db_and_gate_is_bounded(self) -> None:
        for kind in ("database", "gate"):
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _create_receipted_root(temporary)
                context = multiprocessing.get_context("spawn")
                result_queue = context.Queue()
                process = context.Process(
                    target=_fifo_swap_worker,
                    args=(str(root), kind, result_queue),
                )
                process.start()
                process.join(timeout=1.0)
                was_alive = process.is_alive()
                if was_alive:
                    process.terminate()
                    process.join(timeout=3.0)
                self.assertFalse(was_alive, f"{kind} FIFO open blocked")
                try:
                    result = result_queue.get(timeout=1.0)
                except queue.Empty:
                    result = ("missing", "no report")
                self.assertIn(
                    result,
                    {
                        ("report", "UNREADABLE"),
                        ("error", "UnstableSnapshotError"),
                    },
                )

    def test_root_metadata_mutation_after_open_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_receipted_root(temporary)
            filesystem = _RootMetadataMutationFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            try:
                report = ReadOnlyDoctor(filesystem=filesystem).inspect(
                    root,
                    "op-receipt-corrupt",
                )
            finally:
                filesystem.close()
            self.assertEqual("UNREADABLE", report.observed_state)

    def test_known_state_basenames_require_regular_files(self) -> None:
        names = (
            _database_name(),
            f"{_database_name()}-wal",
            f"{_database_name()}-shm",
            f"{_database_name()}-journal",
            MARKER_NAME,
            LEDGER_NAME,
        )
        for name in names:
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _make_root(temporary)
                (root / name).mkdir(mode=0o700)
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-known-directory")
                self.assertEqual("UNSAFE_SIDECAR", report.observed_state)

    def test_db_path_swap_before_sqlite_open_is_bounded_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_receipted_root(temporary)
            context = multiprocessing.get_context("spawn")
            result_queue = context.Queue()
            process = context.Process(
                target=_fifo_swap_worker,
                args=(str(root), "database-before-open", result_queue),
            )
            process.start()
            process.join(timeout=1.0)
            was_alive = process.is_alive()
            if was_alive:
                process.terminate()
                process.join(timeout=3.0)
            self.assertFalse(was_alive, "SQLite pathname reopen blocked on FIFO")
            try:
                result = result_queue.get(timeout=1.0)
            except queue.Empty:
                result = ("missing", "no report")
            self.assertEqual(("report", "UNREADABLE"), result)

    def test_database_inspection_uses_bounded_memory_without_pathname_reopen(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_receipted_root(temporary)
            original_connect = sqlite3.connect
            databases: list[object] = []

            def track_database(
                database: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *,
                uri: bool = False,
                timeout: float = 5.0,
                isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"]
                | None = "DEFERRED",
            ) -> sqlite3.Connection:
                databases.append(database)
                return original_connect(
                    database,
                    uri=uri,
                    timeout=timeout,
                    isolation_level=isolation_level,
                )

            with mock.patch(
                "agent_team.doctor.sqlite3.connect",
                side_effect=track_database,
            ):
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-receipt-corrupt")
            self.assertEqual("RECEIPTED", report.observed_state)
            self.assertEqual([":memory:"], databases)

    def test_database_header_versions_fail_closed_without_normalization(self) -> None:
        for pair in ((3, 3), (255, 255), (2, 1)):
            with (
                self.subTest(pair=pair),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _create_pending_claim_root(temporary)
                database = root / _database_name()
                fd = os.open(database, os.O_RDWR)
                try:
                    os.pwrite(fd, bytes(pair), 18)
                finally:
                    os.close(fd)
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-identity-mismatch")
                self.assertEqual("UNREADABLE", report.observed_state)

    def test_prebound_intent_provider_is_observed_without_provider_disclosure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-prebound",
                    effect_key="effect/op-prebound",
                    provider_id="provider/prebound",
                    actor="main",
                    clock_ns=1,
                )
            marker = root / MARKER_NAME
            marker.write_bytes(WRITER_MARKER_CLEAN_CONTENT)
            marker.chmod(0o600)
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-prebound")
            self.assertEqual("INTENT_ONLY", report.observed_state)
            self.assertIsNone(report.owner)
            self.assertNotIn("provider/prebound", repr(report))

    def test_ledger_absence_is_rechecked_before_reader_returns_none(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)

            class LedgerAppearsFilesystem(StateFilesystem):
                created = False

                def _fault(self, point: str) -> None:
                    if point == "after_ledger_absence" and not self.created:
                        self.created = True
                        ledger = self.state_root / LEDGER_NAME
                        ledger.write_text(
                            '{"version":1,"sequence":1,"phase":"RESTORE_PREPARED",'
                            '"restore_generation":1,"recovery_epoch":1,"fencing_token_floor":1,'
                            '"backup_digest":"sha256:' + "a" * 64 + '",'
                            '"actor":"operator","audit_ref":"audit/1"}\n',
                            encoding="utf-8",
                        )
                        ledger.chmod(0o600)

            filesystem = LedgerAppearsFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            try:
                with self.assertRaises(UnstableSnapshotError):
                    RecoveryLedgerReader().read(filesystem)
            finally:
                filesystem.close()

    def test_doctor_maps_ledger_absence_race_to_unreadable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)

            class LedgerAppearsFilesystem(StateFilesystem):
                created = False

                def _fault(self, point: str) -> None:
                    if point == "after_ledger_absence" and not self.created:
                        self.created = True
                        ledger = self.state_root / LEDGER_NAME
                        ledger.write_text(
                            '{"version":1,"sequence":1,"phase":"RESTORE_PREPARED",'
                            '"restore_generation":1,"recovery_epoch":1,"fencing_token_floor":1,'
                            '"backup_digest":"sha256:' + "b" * 64 + '",'
                            '"actor":"operator","audit_ref":"audit/1"}\n',
                            encoding="utf-8",
                        )
                        ledger.chmod(0o600)

            filesystem = LedgerAppearsFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            try:
                report = ReadOnlyDoctor(filesystem=filesystem).inspect(
                    root,
                    "op-race",
                )
            finally:
                filesystem.close()
            self.assertEqual("UNREADABLE", report.observed_state)

    def test_close_releases_every_fd_after_custom_base_exception(self) -> None:
        class CleanupFailure(BaseException):
            pass

        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            self.assertTrue(filesystem.try_marker_exclusive())
            fds = tuple(
                fd
                for fd in (
                    filesystem._marker_fd,
                    filesystem._gate_fd,
                    filesystem._root_fd,
                    filesystem._parent_fd,
                )
                if fd is not None
            )
            original_flock = fcntl.flock

            def fail_unlock(fd: int, operation: int) -> None:
                if operation & fcntl.LOCK_UN:
                    raise CleanupFailure
                original_flock(fd, operation)

            with (
                mock.patch("agent_team.doctor.fcntl.flock", side_effect=fail_unlock),
                self.assertRaises(CleanupFailure),
            ):
                filesystem.close()
            for fd in fds:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_close_releases_every_fd_after_base_exception(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_pending_claim_root(temporary)
            filesystem = StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            )
            self.assertTrue(filesystem.try_marker_exclusive())
            fds = tuple(
                fd
                for fd in (
                    filesystem._marker_fd,
                    filesystem._gate_fd,
                    filesystem._root_fd,
                    filesystem._parent_fd,
                )
                if fd is not None
            )
            original_flock = fcntl.flock

            def interrupt_unlock(fd: int, operation: int) -> None:
                if operation & fcntl.LOCK_UN:
                    raise KeyboardInterrupt
                original_flock(fd, operation)

            with (
                mock.patch(
                    "agent_team.doctor.fcntl.flock", side_effect=interrupt_unlock
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                filesystem.close()
            self.assertTrue(filesystem._closed)
            self.assertIsNone(filesystem._marker_fd)
            self.assertIsNone(filesystem._gate_fd)
            self.assertIsNone(filesystem._root_fd)
            self.assertIsNone(filesystem._parent_fd)
            for fd in fds:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_missing_current_attempt_is_not_reported_as_not_found(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-missing-attempt",
                    effect_key="effect/op-missing-attempt",
                    actor="main",
                    clock_ns=1,
                )
            _mutate_and_checkpoint(
                root,
                "UPDATE operations SET current_attempt = 2 "
                "WHERE operation_id = 'op-missing-attempt'",
            )
            report = ReadOnlyDoctor(
                marker_name=MARKER_NAME,
                ledger_name=LEDGER_NAME,
            ).inspect(root, "op-missing-attempt")
            self.assertEqual("UNREADABLE", report.observed_state)

    def test_operation_and_attempt_identity_mismatch_is_unreadable(self) -> None:
        mutations = (
            (
                "UPDATE operation_attempts SET provider_id = 'provider/other' "
                "WHERE operation_id = 'op-identity-mismatch'"
            ),
            (
                "UPDATE operations SET recovery_epoch = recovery_epoch + 1 "
                "WHERE operation_id = 'op-identity-mismatch'"
            ),
            (
                "UPDATE operations SET status = 'INTENT' "
                "WHERE operation_id = 'op-identity-mismatch'"
            ),
        )
        for statement in mutations:
            with (
                self.subTest(statement=statement),
                tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary,
            ):
                root = _create_pending_claim_root(temporary)
                _mutate_and_checkpoint(root, statement)
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-identity-mismatch")
                self.assertEqual("UNREADABLE", report.observed_state)

    def test_doctor_receipt_reader_does_not_issue_authority_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-doctor-") as temporary:
            root = _create_receipted_root(temporary)
            with mock.patch.object(
                CoordinationStore,
                "_receipt_from_row",
                side_effect=AssertionError("doctor must not issue receipt"),
            ):
                report = ReadOnlyDoctor(
                    marker_name=MARKER_NAME,
                    ledger_name=LEDGER_NAME,
                ).inspect(root, "op-receipt-corrupt")
            self.assertEqual("RECEIPTED", report.observed_state)


def _database_name() -> str:
    return "coordination.sqlite3"


def _create_restore_artifact_root(
    temporary: str,
    status: str,
) -> tuple[Path, BackupArtifact]:
    root = _make_root(temporary)
    clock = FakeClock(100)
    provider = FakeProvider()
    with CoordinationStore(root, clock=clock) as store:
        store.create_intent(
            "op-restored",
            effect_key="effect/op-restored",
            provider_id="provider/test",
            actor="main",
            clock_ns=100,
        )
        claim = store.claim(
            "op-restored",
            owner="owner-a",
            provider_id="provider/test",
            lease_ttl_ns=1_000,
            now_ns=100,
        )
        if status in {
            "FENCE_RESERVATION_STARTED",
            "CLAIMED",
            "EFFECT_PREPARED",
            "UNKNOWN_EFFECT",
            "UNKNOWN",
            "COMPLETED",
        }:
            if status == "FENCE_RESERVATION_STARTED":
                store._begin_fence_reservation(claim, now_ns=101)
            else:
                claim = store.reserve_fence(claim, provider)
        if status == "EFFECT_PREPARED":
            store._begin_effect(claim, now_ns=102)
        elif status == "UNKNOWN_EFFECT":
            effect = store._begin_effect(claim, now_ns=102)
            store._mark_unknown_effect(effect, now_ns=103)
        elif status == "COMPLETED":
            receipt = store.execute_effect(claim, provider, now_ns=102)
            store.complete(receipt, now_ns=103)
        elif status == "UNKNOWN":
            effect = store._begin_effect(claim, now_ns=102)
            store._mark_unknown_effect(effect, now_ns=103)
            connection = store._connection
            assert connection is not None
            connection.execute(
                "UPDATE operations SET status = 'UNKNOWN' "
                "WHERE operation_id = 'op-restored'"
            )
    artifact = SQLiteBackup(root, busy_timeout_ms=100).create("snapshot")
    return root, artifact


def _create_restore_preserved_root(temporary: str, status: str) -> Path:
    root, artifact = _create_restore_artifact_root(temporary, status)
    result = BackupRestore(
        root,
        busy_timeout_ms=100,
        clock=FakeClock(200),
    ).restore(
        artifact,
        actor="operator",
        audit_ref=f"audit/restore-preserved/{status.lower()}",
    )
    if result.phase != "RESTORE_COMMITTED":
        raise AssertionError("restore-preserved fixture did not commit")
    return root


def _leave_restore_prepared(
    root: Path,
    artifact: BackupArtifact,
    *,
    audit_ref: str,
    clock: FakeClock,
) -> None:
    def fail_before_replace(point: str) -> None:
        if point == "before_replace_call":
            raise RuntimeError("leave restore prepared")

    try:
        BackupRestore(
            root,
            busy_timeout_ms=100,
            clock=clock,
            fault=fail_before_replace,
        ).restore(
            artifact,
            actor="operator",
            audit_ref=audit_ref,
        )
    except RuntimeError:
        return
    raise AssertionError("restore fixture did not stop before replacement")


def _abort_restore_generation(root: Path, artifact: BackupArtifact) -> None:
    candidate_name = _candidate_basename(artifact)
    session = WalSidecarController(root, busy_timeout_ms=100).hold_quiescence(
        allowed_root_names=(
            artifact.database_basename,
            artifact.manifest_basename,
            candidate_name,
        )
    )
    try:
        owner = session.issue_owner()
        ledger = RestoreLedger(root, busy_timeout_ms=100)
        handle = ledger.read(owner)
        if handle is None:
            raise AssertionError("restore fixture has no prepared handle")
        aborted = ledger.mark_aborted(
            handle,
            RecoveryFloor(handle.recovery_epoch, handle.fencing_token_floor),
            owner,
        )
        if aborted.phase != "RESTORE_ABORTED":
            raise AssertionError("restore fixture did not abort")
    finally:
        session.close()


def _write_restore_pair(
    root: Path,
    *,
    include_ledger: bool = True,
    include_tombstone: bool = True,
    terminal: bool = False,
) -> None:
    backup_digest = "sha256:" + "a" * 64
    prepared_ledger = RecoveryLedgerRecord(
        version=1,
        sequence=1,
        phase="RESTORE_PREPARED",
        restore_generation=1,
        recovery_epoch=1,
        fencing_token_floor=1,
        backup_digest=backup_digest,
        actor="operator",
        audit_ref="audit/restore/doctor",
    )
    prepared_tombstone = RecoveryTombstoneRecord(
        version=1,
        sequence=1,
        phase="PREPARED",
        restore_generation=1,
        backup_digest=backup_digest,
        previous_primary_digest="sha256:" + "b" * 64,
        candidate_digest="sha256:" + "c" * 64,
        previous_recovery_epoch=0,
        previous_fencing_token_hwm=0,
        previous_last_clock_ns=0,
        identities=(RestoreIdentity("op-restore", "effect/op-restore"),),
        actor="operator",
        audit_ref="audit/restore/doctor",
    )
    if include_ledger:
        ledger_records = [prepared_ledger]
        if terminal:
            ledger_records.append(
                RecoveryLedgerRecord(
                    version=1,
                    sequence=2,
                    phase="RESTORE_REPLACED",
                    restore_generation=1,
                    recovery_epoch=1,
                    fencing_token_floor=1,
                    backup_digest=backup_digest,
                    actor="operator",
                    audit_ref="audit/restore/doctor",
                )
            )
            ledger_records.append(
                RecoveryLedgerRecord(
                    version=1,
                    sequence=3,
                    phase="RESTORE_COMMITTED",
                    restore_generation=1,
                    recovery_epoch=1,
                    fencing_token_floor=1,
                    backup_digest=backup_digest,
                    actor="operator",
                    audit_ref="audit/restore/doctor",
                )
            )
        (root / LEDGER_NAME).write_bytes(
            b"".join(_encode_record(record) for record in ledger_records)
        )
        (root / LEDGER_NAME).chmod(0o600)
    if include_tombstone:
        tombstone_records = [prepared_tombstone]
        if terminal:
            tombstone_records.append(
                RecoveryTombstoneRecord(
                    version=1,
                    sequence=2,
                    phase="COMMITTED",
                    restore_generation=1,
                    backup_digest=backup_digest,
                    previous_primary_digest="sha256:" + "b" * 64,
                    candidate_digest="sha256:" + "c" * 64,
                    previous_recovery_epoch=0,
                    previous_fencing_token_hwm=0,
                    previous_last_clock_ns=0,
                    identities=prepared_tombstone.identities,
                    actor="operator",
                    audit_ref="audit/restore/doctor",
                )
            )
        (root / TOMBSTONE_NAME).write_bytes(
            b"".join(_encode_tombstone(record) for record in tombstone_records)
        )
        (root / TOMBSTONE_NAME).chmod(0o600)


def _create_receipted_root(temporary: str) -> Path:
    root = _make_root(temporary)
    clock = FakeClock(100)
    with CoordinationStore(root, clock=clock) as store:
        store.create_intent(
            "op-receipt-corrupt",
            effect_key="effect/op-receipt-corrupt",
            provider_id="provider/test",
            actor="main",
            clock_ns=100,
        )
        claim = store.claim(
            "op-receipt-corrupt",
            owner="owner-a",
            provider_id="provider/test",
            lease_ttl_ns=10,
            now_ns=100,
        )
        provider = FakeProvider()
        claim = store.reserve_fence(claim, provider)
        store.execute_effect(claim, provider, now_ns=102)
    marker = root / MARKER_NAME
    marker.write_bytes(WRITER_MARKER_CLEAN_CONTENT)
    marker.chmod(0o600)
    return root


def _create_pending_claim_root(temporary: str) -> Path:
    root = _make_root(temporary)
    clock = FakeClock(100)
    with CoordinationStore(root, clock=clock) as store:
        store.create_intent(
            "op-identity-mismatch",
            effect_key="effect/op-identity-mismatch",
            provider_id="provider/test",
            actor="main",
            clock_ns=100,
        )
        store.claim(
            "op-identity-mismatch",
            owner="owner-a",
            provider_id="provider/test",
            lease_ttl_ns=10,
            now_ns=100,
        )
    marker = root / MARKER_NAME
    marker.write_bytes(WRITER_MARKER_CLEAN_CONTENT)
    marker.chmod(0o600)
    return root


def _mutate_and_checkpoint(root: Path, statement: str) -> None:
    database = root / _database_name()
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(statement)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


class _SwapAfterLstatFilesystem(StateFilesystem):
    swap_target: str | None = None
    swap_kind: str | None = None
    swapped = False

    def _fault(self, point: str) -> None:
        if self.swapped or point != self.swap_target or self.swap_kind is None:
            return
        self.swapped = True
        if self.swap_kind in {"database", "database-before-open"}:
            target = self.state_root / _database_name()
            target.rename(self.state_root / "coordination.sqlite3-old")
            os.mkfifo(target, mode=0o600)
        elif self.swap_kind == "gate":
            target = self.state_root.parent / ".coordination-lifetime.lock"
            target.rename(self.state_root.parent / ".coordination-lifetime-old")
            os.mkfifo(target, mode=0o600)


def _fifo_swap_worker(
    root_value: str,
    kind: str,
    result_queue: multiprocessing.queues.Queue[tuple[str, str]],
) -> None:
    if kind == "database-before-open":
        _SwapAfterLstatFilesystem.swap_target = "before_db_open"
    else:
        _SwapAfterLstatFilesystem.swap_target = (
            "after_db_lstat" if kind == "database" else "after_gate_lstat"
        )
    _SwapAfterLstatFilesystem.swap_kind = kind
    try:
        filesystem = _SwapAfterLstatFilesystem.open_existing(
            Path(root_value),
            marker_name=MARKER_NAME,
            ledger_name=LEDGER_NAME,
        )
        try:
            report = ReadOnlyDoctor(filesystem=filesystem).inspect(
                Path(root_value),
                "op-fifo-race",
            )
        finally:
            filesystem.close()
        result_queue.put(("report", report.observed_state))
    except (OSError, StateFilesystemError, ValueError) as error:
        result_queue.put(("error", type(error).__name__))


class _RootMetadataMutationFilesystem(StateFilesystem):
    mutated = False

    def _fault(self, point: str) -> None:
        if point == "before_final_inventory" and not self.mutated:
            self.mutated = True
            self.state_root.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
