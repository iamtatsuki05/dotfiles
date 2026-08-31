from __future__ import annotations

import copy
import errno
import fcntl
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
from typing import ClassVar, cast
from unittest import mock

from agent_team import recovery, wal
from agent_team.doctor import ReadOnlyDoctor, StateFilesystem, StateFilesystemError
from agent_team.lease import RecoveryFloor
from agent_team.recovery import RecoveryCoordinator, RestoreIdentity, RestoreLedger
from agent_team.store import (
    CoordinationStore,
    StoreBusyError,
    StoreUnavailableError,
    _CleanupCapability,
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
    DatabaseCandidate,
    DatabaseCopyTarget,
    DatabaseReplacementResult,
    QuiescenceOwner,
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

    def test_hold_quiescence_allows_only_declared_restore_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence(
                allowed_root_names=("candidate.db", "manifest.json")
            )
            try:
                (root / "candidate.db").write_bytes(b"candidate")
                (root / "candidate.db").chmod(0o600)
                session.assert_identity()
                (root / "unexpected").write_bytes(b"unknown")
                (root / "unexpected").chmod(0o600)
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    session.assert_identity()
            finally:
                session.close()

            for value in (
                ["candidate.db"],
                ("candidate.db", "candidate.db"),
                ("*",),
                ("coordination.sqlite3",),
                ("recovery.tombstones",),
                ("candidate/db",),
            ):
                with (
                    self.subTest(value=value),
                    self.assertRaises((TypeError, ValueError)),
                ):
                    WalSidecarController(root).hold_quiescence(
                        allowed_root_names=value  # type: ignore[arg-type]
                    )

    def test_restore_ledger_prepare_creates_internal_logs_under_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence()
            owner = session.issue_owner()
            restore = RestoreLedger(root)
            try:
                handle = restore.prepare(
                    backup_digest="sha256:" + "a" * 64,
                    previous_primary_digest="sha256:" + "b" * 64,
                    candidate_digest="sha256:" + "c" * 64,
                    identities=(
                        RestoreIdentity(
                            operation_id="operation-a",
                            effect_key="effect-a",
                        ),
                    ),
                    actor="operator",
                    audit_ref="audit/restore/1",
                    previous_recovery_epoch=0,
                    previous_fencing_token_hwm=0,
                    previous_last_clock_ns=0,
                    floor_lower_bound=RecoveryFloor(
                        recovery_epoch=1,
                        fencing_token_floor=1,
                    ),
                    owner=owner,
                )
                self.assertEqual("RESTORE_PREPARED", handle.phase)
                for name in ("recovery.ledger", "recovery.tombstones"):
                    metadata = (root / name).stat()
                    self.assertTrue(stat.S_ISREG(metadata.st_mode))
                    self.assertEqual(0o600, stat.S_IMODE(metadata.st_mode))
                    self.assertEqual(1, metadata.st_nlink)
                checkpoint = session.checkpoint(CheckpointRequest("PASSIVE"))
                self.assertEqual((0, 0, 0), checkpoint.values)
            finally:
                session.close()

    def test_acquisition_validates_existing_internal_and_declared_entries(self) -> None:
        cases = ("recovery.ledger", "candidate.db")
        for name in cases:
            for kind in ("symlink", "fifo", "directory", "hardlink", "mode"):
                with (
                    self.subTest(name=name, kind=kind),
                    tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary,
                ):
                    root = _make_root(temporary)
                    with CoordinationStore(root):
                        pass
                    target = root.parent / f"{name.replace('.', '-')}-target"
                    target.write_bytes(b"target")
                    target.chmod(0o600)
                    entry = root / name
                    if kind == "symlink":
                        entry.symlink_to(target)
                    elif kind == "fifo":
                        os.mkfifo(entry, 0o600)
                    elif kind == "directory":
                        entry.mkdir(mode=0o700)
                    elif kind == "hardlink":
                        os.link(root / "coordination.sqlite3", entry)
                    else:
                        entry.write_bytes(b"unsafe-mode")
                        entry.chmod(0o644)
                    allowed = (name,) if name == "candidate.db" else ()
                    with self.assertRaises(WalSidecarUnsafeError):
                        WalSidecarController(root).hold_quiescence(
                            allowed_root_names=allowed
                        )

    def test_quiescence_owner_borrow_is_opaque_and_session_bound(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence()
            owner = session.issue_owner()
            try:
                with owner._borrow_root(root) as root_fd:
                    self.assertEqual(root_fd, session._resources.root_fd)
                    session.assert_identity()
                with self.assertRaises(TypeError):
                    copy.copy(owner)
                with self.assertRaises(TypeError):
                    copy.deepcopy(owner)
                with self.assertRaises(TypeError):
                    pickle.dumps(owner)
                forged = object.__new__(QuiescenceOwner)
                object.__setattr__(forged, "_session", session)
                object.__setattr__(forged, "_token", owner._token)
                with (
                    self.assertRaises(WalSidecarClosedError),
                    forged._borrow_root(root),
                ):
                    pass
                with (
                    self.assertRaises(WalSidecarUnsafeError),
                    owner._borrow_root(root.parent),
                ):
                    pass
            finally:
                session.close()
            self.assertEqual({}, session._controller._active_owners)
            with self.assertRaises(WalSidecarClosedError), owner._borrow_root(root):
                pass

    def test_owner_cleanup_retry_drains_recovery_fd_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-owner-retry-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = WalSidecarController(root)
            session = controller.hold_quiescence()
            owner = session.issue_owner()
            fd = os.open(root / "recovery-pending", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
                owner._retain_failed_fd(
                    fd,
                    identity,
                    "recovery file recovery.ledger durability",
                )
                owner._retry_cleanup()
                owner._retry_cleanup()
                owner._retry_cleanup()
                with self.assertRaises(OSError):
                    os.fstat(fd)
                self.assertFalse(controller._active_sessions)
                self.assertFalse(controller._active_owners)
                session.close()
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
                session.close()
                controller.close()

    def test_owner_cleanup_retry_rejects_forged_or_already_closed_owner(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-owner-invalid-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = WalSidecarController(root)
            session = controller.hold_quiescence()
            owner = session.issue_owner()
            forged = object.__new__(QuiescenceOwner)
            object.__setattr__(forged, "_session", session)
            object.__setattr__(forged, "_token", owner._token)
            try:
                with self.assertRaises(WalSidecarClosedError):
                    forged._retry_cleanup()
            finally:
                session.close()

            closed_session = controller.hold_quiescence()
            closed_owner = closed_session.issue_owner()
            closed_session.close()
            with self.assertRaises(WalSidecarClosedError):
                closed_owner._retry_cleanup()
            controller.close()

    def test_uninitialized_quiescence_owner_is_typed_closed(self) -> None:
        owner = object.__new__(QuiescenceOwner)
        with self.assertRaises(WalSidecarClosedError):
            repr(owner)
        with self.assertRaises(WalSidecarClosedError):
            owner.assert_identity()
        with self.assertRaises(WalSidecarClosedError), owner._borrow_root(Path("/tmp")):
            pass

    def test_copy_observation_can_be_reverified_without_exposing_resources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence()
            try:
                copied = session.copy_database_to(
                    CheckpointRequest("TRUNCATE"),
                    DatabaseCopyTarget(name="snapshot"),
                )
                candidate = DatabaseCandidate(
                    name=copied.target.name,
                    identity=copied.target_identity,
                    size=copied.size,
                    digest=copied.digest,
                )
                verified = session.verify_candidate(candidate)
                self.assertEqual(candidate, verified)
                self.assertFalse(hasattr(verified, "root_fd"))
                self.assertFalse(hasattr(verified, "connection"))
            finally:
                session.close()

    def test_verify_candidate_rejects_tamper_and_sidecar_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            source = root / "candidate.db"
            source.write_bytes(b"candidate-bytes")
            source.chmod(0o600)
            candidate = DatabaseCandidate(
                name=source.name,
                identity=(source.stat().st_dev, source.stat().st_ino),
                size=source.stat().st_size,
                digest="sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            session = WalSidecarController(root).hold_quiescence(
                allowed_root_names=(source.name,)
            )
            try:
                self.assertEqual(candidate, session.verify_candidate(candidate))
                source.write_bytes(b"tampered-bytes")
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    session.verify_candidate(candidate)
                sidecar = root / f"{source.name}-wal"
                sidecar.write_bytes(b"keep-sidecar")
                sidecar.chmod(0o600)
                with self.assertRaises(WalSidecarUnsafeError):
                    session.verify_candidate(candidate)
                self.assertEqual(b"keep-sidecar", sidecar.read_bytes())
            finally:
                session.close()

    def test_verify_candidate_rejects_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            target = root.parent / "candidate-target"
            target.write_bytes(b"outside")
            target.chmod(0o600)
            candidate_path = root / "candidate.db"
            candidate = DatabaseCandidate(
                name=candidate_path.name,
                identity=(target.stat().st_dev, target.stat().st_ino),
                size=target.stat().st_size,
                digest="sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
            )
            session = WalSidecarController(root).hold_quiescence(
                allowed_root_names=(candidate_path.name,)
            )
            try:
                candidate_path.symlink_to(target)
                with self.assertRaises(WalSidecarUnsafeError):
                    session.verify_candidate(candidate)
            finally:
                session.close()
            self.assertTrue(candidate_path.is_symlink())
            self.assertEqual(b"outside", target.read_bytes())

    def test_replace_uses_root_relative_rename_and_rebinds_same_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "op-replace",
                    effect_key="effect/replace",
                    actor="main",
                    clock_ns=1,
                )
            session = WalSidecarController(root).hold_quiescence()
            try:
                copied = session.copy_database_to(
                    CheckpointRequest("TRUNCATE"),
                    DatabaseCopyTarget(name="candidate.db"),
                )
                candidate = DatabaseCandidate(
                    name=copied.target.name,
                    identity=copied.target_identity,
                    size=copied.size,
                    digest=copied.digest,
                )
                with mock.patch(
                    "agent_team.wal.os.replace", wraps=os.replace
                ) as replace:
                    replaced = session.replace_database(candidate)
                self.assertIs(type(replaced), DatabaseReplacementResult)
                self.assertEqual(candidate, replaced.candidate)
                self.assertFalse((root / candidate.name).exists())
                self.assertEqual(
                    candidate.identity,
                    (
                        (root / "coordination.sqlite3").stat().st_dev,
                        (root / "coordination.sqlite3").stat().st_ino,
                    ),
                )
                self.assertEqual(candidate.identity[1], replaced.primary_identity[1])
                self.assertEqual(1, replace.call_count)
                _, kwargs = replace.call_args
                self.assertEqual(session._resources.root_fd, kwargs["src_dir_fd"])
                self.assertEqual(session._resources.root_fd, kwargs["dst_dir_fd"])
                session.assert_identity()
                checkpoint = session.checkpoint(CheckpointRequest("PASSIVE"))
                self.assertEqual((0, 0, 0), checkpoint.values)
                with self.assertRaises(StoreBusyError):
                    WalSidecarController(root, busy_timeout_ms=20).checkpoint(
                        CheckpointRequest("PASSIVE")
                    )
            finally:
                session.close()
            with CoordinationStore(root):
                pass

    def test_replace_rejects_candidate_or_primary_swap_before_rename(self) -> None:
        for swap_kind in ("candidate", "primary"):
            with (
                self.subTest(swap_kind=swap_kind),
                tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary,
            ):
                root = _make_root(temporary)
                with CoordinationStore(root):
                    pass
                alternate = root / "alternate.db"
                alternate.write_bytes(b"alternate")
                alternate.chmod(0o600)

                selected_swap_kind = swap_kind

                class SwapController(WalSidecarController):
                    swapped = False

                    def _fault(
                        self,
                        point: str,
                        *,
                        _swap_kind: str = selected_swap_kind,
                        _alternate: Path = alternate,
                    ) -> None:
                        if point == "before_replace" and not self.swapped:
                            self.swapped = True
                            scratch = self.state_root / "swap.scratch"
                            if _swap_kind == "candidate":
                                source = self.state_root / "candidate.db"
                            else:
                                source = self.state_root / "coordination.sqlite3"
                            source.rename(scratch)
                            _alternate.rename(source)
                            scratch.rename(_alternate)

                session = SwapController(root).hold_quiescence()
                try:
                    copied = session.copy_database_to(
                        CheckpointRequest("TRUNCATE"),
                        DatabaseCopyTarget(name="candidate.db"),
                    )
                    candidate = DatabaseCandidate(
                        name=copied.target.name,
                        identity=copied.target_identity,
                        size=copied.size,
                        digest=copied.digest,
                    )
                    with (
                        mock.patch(
                            "agent_team.wal.os.replace", wraps=os.replace
                        ) as replace,
                        self.assertRaises(WalSidecarRecoveryRequiredError),
                    ):
                        session.replace_database(candidate)
                    self.assertEqual(0, replace.call_count)
                    self.assertTrue((root / "alternate.db").exists())
                    self.assertTrue((root / "candidate.db").exists())
                finally:
                    session.close()

    def test_replace_orders_rename_fsync_rebind_and_final_assert(self) -> None:
        events: list[str] = []

        class TraceController(WalSidecarController):
            def _fault(self, point: str) -> None:
                if point in {
                    "before_replace",
                    "after_replace",
                    "before_replace_fsync",
                    "after_replace_fsync",
                    "before_database_rebind",
                    "after_database_rebind",
                    "before_replace_result",
                    "after_replace_result",
                }:
                    events.append(point)

            def _rebind_database(self, resources: object) -> None:
                events.append("rebind")
                super()._rebind_database(resources)  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = TraceController(root).hold_quiescence()
            try:
                copied = session.copy_database_to(
                    CheckpointRequest("TRUNCATE"),
                    DatabaseCopyTarget(name="candidate.db"),
                )
                candidate = DatabaseCandidate(
                    name=copied.target.name,
                    identity=copied.target_identity,
                    size=copied.size,
                    digest=copied.digest,
                )
                with mock.patch("agent_team.wal.os.fsync", wraps=os.fsync):
                    session.replace_database(candidate)
            finally:
                session.close()
        self.assertLess(events.index("before_replace"), events.index("after_replace"))
        self.assertLess(
            events.index("after_replace"), events.index("before_replace_fsync")
        )
        self.assertLess(
            events.index("after_replace_fsync"), events.index("before_database_rebind")
        )
        self.assertLess(events.index("after_replace_fsync"), events.index("rebind"))
        self.assertLess(events.index("rebind"), events.index("before_database_rebind"))
        self.assertLess(
            events.index("before_database_rebind"),
            events.index("after_database_rebind"),
        )
        self.assertLess(
            events.index("after_database_rebind"), events.index("before_replace_result")
        )

    def test_rebind_old_fd_close_failure_keeps_new_descriptor_retryable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / "coordination.sqlite3"
            replacement = root / "replacement.sqlite3"
            replacement.write_bytes(database.read_bytes())
            replacement.chmod(0o600)
            session = WalSidecarController(root, busy_timeout_ms=20).hold_quiescence(
                allowed_root_names=(replacement.name,)
            )
            old_fd = session._resources.database_fd
            old_identity = session._resources.database_identity
            original_close = os.close
            failed = True

            def close(fd: int) -> None:
                nonlocal failed
                if fd == old_fd and failed:
                    failed = False
                    raise OSError("injected old descriptor close failure")
                original_close(fd)

            try:
                os.replace(replacement, database)
                with (
                    mock.patch("agent_team.wal.os.close", side_effect=close),
                    self.assertRaises(WalSidecarRecoveryRequiredError),
                ):
                    session._rebind_database()
                self.assertNotEqual(old_identity, session._resources.database_identity)
                session.assert_identity()
            finally:
                session.close()
            with self.assertRaises(OSError):
                os.fstat(old_fd)

    def test_rebind_persistent_old_fd_failure_retains_orphan_until_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / "coordination.sqlite3"
            replacement = root / "replacement.sqlite3"
            replacement.write_bytes(database.read_bytes())
            replacement.chmod(0o600)
            session = WalSidecarController(root, busy_timeout_ms=20).hold_quiescence(
                allowed_root_names=(replacement.name,)
            )
            old_fd = session._resources.database_fd
            try:
                os.replace(replacement, database)

                original_close = os.close

                def close(fd: int) -> None:
                    if fd == old_fd:
                        raise OSError("persistent old descriptor close failure")
                    original_close(fd)

                with (
                    mock.patch("agent_team.wal.os.close", side_effect=close),
                    self.assertRaises(WalSidecarRecoveryRequiredError),
                ):
                    session._rebind_database()
                self.assertTrue(
                    any(fd == old_fd for fd, _, _ in session._resources._orphan_fds)
                )
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    session.assert_identity()
                session.close()
            finally:
                session.close()
            with self.assertRaises(OSError):
                os.fstat(old_fd)

    def test_replace_candidate_fd_close_uncertainty_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence()
            try:
                copied = session.copy_database_to(
                    CheckpointRequest("TRUNCATE"),
                    DatabaseCopyTarget(name="candidate.db"),
                )
                candidate = DatabaseCandidate(
                    name=copied.target.name,
                    identity=copied.target_identity,
                    size=copied.size,
                    digest=copied.digest,
                )
                original_close = wal._close_temporary_fd
                candidate_closes = 0

                def close(
                    fd: int,
                    expected_identity: tuple[int, int] | None,
                    label: str,
                ) -> None:
                    nonlocal candidate_closes
                    if label == "database candidate":
                        candidate_closes += 1
                        if candidate_closes == 2:
                            raise OSError("injected candidate close failure")
                    original_close(fd, expected_identity, label)

                with (
                    mock.patch("agent_team.wal._close_temporary_fd", side_effect=close),
                    self.assertRaises(WalSidecarRecoveryRequiredError),
                ):
                    session.replace_database(candidate)
                self.assertEqual(
                    candidate.identity,
                    (
                        (root / "coordination.sqlite3").stat().st_dev,
                        (root / "coordination.sqlite3").stat().st_ino,
                    ),
                )
                self.assertTrue(session._resources._orphan_fds)
            finally:
                session.close()

    def test_replace_rechecks_primary_digest_before_result(self) -> None:
        class TamperController(WalSidecarController):
            tampered = False

            def _fault(self, point: str) -> None:
                if point == "before_replace_result" and not self.tampered:
                    self.tampered = True
                    database = self.state_root / "coordination.sqlite3"
                    with database.open("ab") as stream:
                        stream.write(b"tampered")

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = TamperController(root).hold_quiescence()
            try:
                copied = session.copy_database_to(
                    CheckpointRequest("TRUNCATE"),
                    DatabaseCopyTarget(name="candidate.db"),
                )
                candidate = DatabaseCandidate(
                    name=copied.target.name,
                    identity=copied.target_identity,
                    size=copied.size,
                    digest=copied.digest,
                )
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    session.replace_database(candidate)
            finally:
                session.close()

    def test_replace_rejects_sidecar_that_appears_before_rename(self) -> None:
        class SidecarController(WalSidecarController):
            def _fault(self, point: str) -> None:
                if point == "before_replace":
                    sidecar = self.state_root / WAL_BASENAME
                    sidecar.write_bytes(b"late-sidecar")
                    sidecar.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = SidecarController(root).hold_quiescence()
            try:
                copied = session.copy_database_to(
                    CheckpointRequest("TRUNCATE"),
                    DatabaseCopyTarget(name="candidate.db"),
                )
                candidate = DatabaseCandidate(
                    name=copied.target.name,
                    identity=copied.target_identity,
                    size=copied.size,
                    digest=copied.digest,
                )
                with (
                    mock.patch("agent_team.wal.os.replace") as replace,
                    self.assertRaises(WalSidecarRecoveryRequiredError),
                ):
                    session.replace_database(candidate)
                replace.assert_not_called()
                self.assertEqual(b"late-sidecar", (root / WAL_BASENAME).read_bytes())
            finally:
                session.close()

    def test_replace_rejects_existing_primary_sidecars_without_deleting(self) -> None:
        for name in (WAL_BASENAME, SHM_BASENAME, JOURNAL_BASENAME):
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary,
            ):
                root = _make_root(temporary)
                with CoordinationStore(root):
                    pass
                session = WalSidecarController(root).hold_quiescence()
                try:
                    copied = session.copy_database_to(
                        CheckpointRequest("TRUNCATE"),
                        DatabaseCopyTarget(name="candidate.db"),
                    )
                    candidate = DatabaseCandidate(
                        name=copied.target.name,
                        identity=copied.target_identity,
                        size=copied.size,
                        digest=copied.digest,
                    )
                    sidecar = root / name
                    sidecar.write_bytes(b"keep-sidecar")
                    sidecar.chmod(0o600)
                    with self.assertRaises(WalSidecarUnsafeError):
                        session.replace_database(candidate)
                    self.assertEqual(b"keep-sidecar", sidecar.read_bytes())
                    self.assertTrue((root / "candidate.db").exists())
                finally:
                    session.close()

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

    def test_session_retry_after_close_failure_releases_owner_registry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-wal-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence()
            owner = session.issue_owner()
            marker_fd = session._resources.marker_fd
            original_close = os.close
            failed = True

            def close(fd: int) -> None:
                nonlocal failed
                if fd == marker_fd and failed:
                    failed = False
                    raise OSError("injected marker close failure")
                original_close(fd)

            with (
                mock.patch("agent_team.wal.os.close", side_effect=close),
                self.assertRaises(WalSidecarRecoveryRequiredError),
            ):
                session.close()
            self.assertIn(owner._token, session._controller._active_owners)
            session.close()
            self.assertEqual({}, session._controller._active_owners)

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

    def test_checkpoint_body_error_keeps_connection_cleanup_retryable(self) -> None:
        class BodyError(Exception):
            pass

        class FailingController(WalSidecarController):
            __slots__ = ("failed",)

            def __init__(self, state_root: Path) -> None:
                super().__init__(state_root, busy_timeout_ms=20)
                self.failed = False

            def _fault(self, point: str) -> None:
                if point == "after_checkpoint" and not self.failed:
                    self.failed = True
                    raise BodyError("checkpoint body failed")

        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-checkpoint-owner-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = FailingController(root).hold_quiescence()
            original_close = wal._close_temporary_connection

            def fail_connection_close(
                connection: sqlite3.Connection, label: str
            ) -> None:
                if label == "SQLite connection":
                    raise OSError("persistent checkpoint connection close failure")
                original_close(connection, label)

            try:
                with (
                    mock.patch(
                        "agent_team.wal._close_temporary_connection",
                        side_effect=fail_connection_close,
                    ),
                    self.assertRaises(BodyError) as raised,
                ):
                    session.checkpoint(CheckpointRequest("PASSIVE"))
                self.assertTrue(session._resources._orphan_connections)
                retry = getattr(raised.exception, "retry_cleanup", None)
                if not callable(retry):
                    self.fail("checkpoint body error has no cleanup retry")
            finally:
                retry = locals().get("retry")
                if callable(retry):
                    retry()
                    retry()
                    self.assertFalse(session._resources._orphan_connections)
                session.close()

    def test_root_open_wrapper_adopts_lower_store_cleanup_owner(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-root-owner-"
        ) as temporary:
            root = _make_root(temporary)
            calls: list[str] = []
            lower = StoreUnavailableError("lower root traversal failure")
            lower._attach_cleanup_capability(
                _CleanupCapability(lambda: calls.append("lower"))
            )
            with (
                mock.patch(
                    "agent_team.wal._store._open_state_root",
                    side_effect=lower,
                ),
                self.assertRaises(WalSidecarUnsafeError) as raised,
            ):
                WalSidecarController(root).hold_quiescence()
            retry = getattr(raised.exception, "retry_cleanup", None)
            if not callable(retry):
                self.fail("root wrapper has no cleanup retry")
            retry()
            retry()
            self.assertEqual(["lower"], calls)

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

    def test_failed_acquisition_retains_locked_fds_until_controller_retry(self) -> None:
        class FailingController(WalSidecarController):
            marker_fd: int | None = None
            gate_fd: int | None = None

            def _assert_resources(self, resources: object) -> None:
                self.marker_fd = resources.marker_fd  # type: ignore[attr-defined]
                self.gate_fd = resources.gate_fd  # type: ignore[attr-defined]
                raise WalSidecarRecoveryRequiredError("injected acquisition failure")

        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-acquire-retry-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = FailingController(root, busy_timeout_ms=20)
            original_close = os.close
            original_flock = fcntl.flock

            def fail_close(fd: int) -> None:
                if fd in {controller.marker_fd, controller.gate_fd}:
                    raise OSError("persistent close failure")
                original_close(fd)

            def fail_unlock(fd: int, operation: int) -> None:
                if (
                    fd in {controller.marker_fd, controller.gate_fd}
                    and operation == fcntl.LOCK_UN
                ):
                    raise OSError("persistent unlock failure")
                original_flock(fd, operation)

            with (
                mock.patch("agent_team.wal.os.close", side_effect=fail_close),
                mock.patch("agent_team.wal.fcntl.flock", side_effect=fail_unlock),
                self.assertRaises(WalSidecarRecoveryRequiredError) as raised,
            ):
                controller.hold_quiescence()
            pending_fds = getattr(controller, "_pending_fds", ())
            self.assertTrue(pending_fds)
            self.assertEqual(
                {controller.marker_fd, controller.gate_fd},
                {entry.fd for entry in pending_fds},
            )
            retry = getattr(raised.exception, "retry_cleanup", None)
            if not callable(retry):
                self.fail("acquisition error has no cleanup retry")
            retry()
            retry()
            self.assertFalse(controller._pending_fds)

            with WalSidecarController(root)._resources() as resources:
                self.assertIsNotNone(resources.marker_fd)

    def test_retain_failed_fd_binds_identity_before_storage_and_never_closes_reused_fd(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-fd-identity-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence(
                allowed_root_names=("owner-append",)
            )
            fd: int | None = None
            replacement_fd: int | None = None
            try:
                fd = os.open(root / "retained", os.O_CREAT | os.O_RDWR, 0o600)
                os.write(fd, b"retained")
                original_identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
                wal._retain_failed_fd(session._resources, fd, None, "retained fd")
                self.assertEqual(
                    original_identity, session._resources._orphan_fds[0][1]
                )

                os.close(fd)
                fd = None
                replacement_fd = os.open(
                    root / "replacement", os.O_CREAT | os.O_RDWR, 0o600
                )
                self.assertEqual(session._resources._orphan_fds[0][0], replacement_fd)
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    session.close()
                os.write(replacement_fd, b"still-open")
            finally:
                if replacement_fd is not None:
                    os.close(replacement_fd)
                if fd is not None:
                    os.close(fd)
                session.close()

    def test_session_context_preserves_body_error_when_cleanup_is_uncertain(
        self,
    ) -> None:
        class BodyError(Exception):
            pass

        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-body-cleanup-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root, busy_timeout_ms=20).hold_quiescence()
            marker_fd = session._resources.marker_fd
            original_close = os.close
            original_flock = fcntl.flock

            def fail_close(fd: int) -> None:
                if fd == marker_fd:
                    raise OSError("persistent marker close failure")
                original_close(fd)

            def fail_unlock(fd: int, operation: int) -> None:
                if fd == marker_fd and operation == fcntl.LOCK_UN:
                    raise OSError("persistent marker unlock failure")
                original_flock(fd, operation)

            try:
                with (
                    mock.patch("agent_team.wal.os.close", side_effect=fail_close),
                    mock.patch("agent_team.wal.fcntl.flock", side_effect=fail_unlock),
                    self.assertRaises(
                        (BodyError, WalSidecarRecoveryRequiredError)
                    ) as raised,
                    session,
                ):
                    raise BodyError("body failed")
                self.assertIsInstance(raised.exception, BodyError)
                self.assertIsInstance(
                    raised.exception.__cause__, WalSidecarRecoveryRequiredError
                )
                session.close()
            finally:
                session.close()

    def test_public_checkpoint_retains_resources_and_drains_before_new_io(self) -> None:
        class BodyError(Exception):
            pass

        class FaultController(WalSidecarController):
            __slots__ = ("fail_body", "last_marker_fd")

            def __init__(self, state_root: Path, *, busy_timeout_ms: int) -> None:
                super().__init__(state_root, busy_timeout_ms=busy_timeout_ms)
                self.fail_body = True
                self.last_marker_fd: int | None = None

            def _open_resources(
                self,
                *,
                allowed_root_names: frozenset[str] = frozenset(),
            ) -> wal._Resources:
                resources = super()._open_resources(
                    allowed_root_names=allowed_root_names,
                )
                self.last_marker_fd = resources.marker_fd
                return resources

            def _fault(self, point: str) -> None:
                if point == "after_result" and self.fail_body:
                    self.fail_body = False
                    raise BodyError("checkpoint body failed")

        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-public-retry-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = FaultController(root, busy_timeout_ms=20)
            original_close = os.close
            original_flock = fcntl.flock

            def fail_close(fd: int) -> None:
                if (
                    controller.last_marker_fd is not None
                    and fd == controller.last_marker_fd
                ):
                    raise OSError("persistent marker close failure")
                original_close(fd)

            def fail_unlock(fd: int, operation: int) -> None:
                if (
                    controller.last_marker_fd is not None
                    and fd == controller.last_marker_fd
                    and operation == fcntl.LOCK_UN
                ):
                    raise OSError("persistent marker unlock failure")
                original_flock(fd, operation)

            try:
                with (
                    mock.patch("agent_team.wal.os.close", side_effect=fail_close),
                    mock.patch("agent_team.wal.fcntl.flock", side_effect=fail_unlock),
                ):
                    with self.assertRaises(
                        (BodyError, WalSidecarRecoveryRequiredError)
                    ) as raised:
                        controller.checkpoint(CheckpointRequest("PASSIVE"))
                    self.assertIsInstance(raised.exception, BodyError)
                    self.assertEqual(
                        1,
                        len(getattr(controller, "_pending_resources", ())),
                    )
                    with self.assertRaises(WalSidecarRecoveryRequiredError):
                        controller.checkpoint(CheckpointRequest("PASSIVE"))
                    self.assertEqual(
                        1,
                        len(getattr(controller, "_pending_resources", ())),
                    )
            finally:
                close_controller = getattr(controller, "close", None)
                if callable(close_controller):
                    close_controller()

    def test_owner_io_fails_closed_after_one_retained_fd_instead_of_growing_registry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-owner-ready-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence(
                allowed_root_names=("owner-append",)
            )
            owner = session.issue_owner()
            fd = os.open(root / "owner-append", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                wal._retain_failed_fd(session._resources, fd, None, "owner append")
                self.assertEqual(1, len(session._resources._orphan_fds))
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    owner.assert_identity()
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    owner._retain_failed_fd(fd, None, "owner append retry")
                self.assertEqual(1, len(session._resources._orphan_fds))
            finally:
                session.close()

    def test_controller_does_not_issue_session_while_old_cleanup_is_pending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-session-ready-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = WalSidecarController(root, busy_timeout_ms=20)
            session = controller.hold_quiescence()
            marker_fd = session._resources.marker_fd
            original_close = os.close

            def fail_close(fd: int) -> None:
                if fd == marker_fd:
                    raise OSError("persistent marker close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.wal.os.close", side_effect=fail_close),
                    self.assertRaises(WalSidecarRecoveryRequiredError),
                ):
                    session.close()
                self.assertEqual(1, len(controller._active_sessions))
                with self.assertRaises(WalSidecarRecoveryRequiredError) as raised:
                    controller.hold_quiescence()
                retry = getattr(raised.exception, "retry_cleanup", None)
                if not callable(retry):
                    self.fail("pending-session error has no cleanup retry")
                retry()
                retry()
                self.assertFalse(controller._active_sessions)
            finally:
                session.close()
                controller.close()

    def test_temporary_fd_reuse_is_rejected_before_close(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-helper-reuse-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            fd = os.open(root / "original", os.O_CREAT | os.O_RDWR, 0o600)
            original_identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            replacement_fd: int | None = None
            try:
                os.close(fd)
                replacement_fd = os.open(
                    root / "replacement", os.O_CREAT | os.O_RDWR, 0o600
                )
                self.assertEqual(fd, replacement_fd)
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    wal._close_temporary_fd(
                        replacement_fd,
                        original_identity,
                        "reused temporary fd",
                    )
                os.write(replacement_fd, b"still-open")
            finally:
                if replacement_fd is not None:
                    try:
                        os.close(replacement_fd)
                    except OSError:
                        pass

    def test_temporary_fd_binds_none_identity_before_first_close(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-helper-bind-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            fd = os.open(root / "temporary", os.O_CREAT | os.O_RDWR, 0o600)
            original_fstat = os.fstat
            original_close = os.close
            events: list[str] = []

            def fstat(value: int) -> os.stat_result:
                if value == fd:
                    events.append("fstat")
                return original_fstat(value)

            def close(value: int) -> None:
                if value == fd:
                    events.append("close")
                original_close(value)

            try:
                with (
                    mock.patch("agent_team.wal.os.fstat", side_effect=fstat),
                    mock.patch("agent_team.wal.os.close", side_effect=close),
                ):
                    wal._close_temporary_fd(fd, None, "bound temporary fd")
                self.assertEqual(["fstat", "close"], events)
            finally:
                try:
                    original_close(fd)
                except OSError:
                    pass

    def test_actual_close_then_error_does_not_retry_into_reused_fd(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-close-then-error-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            fd = os.open(root / "original", os.O_CREAT | os.O_RDWR, 0o600)
            expected_identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            original_close = os.close
            failed = False

            def close(value: int) -> None:
                nonlocal failed
                if value == fd and not failed:
                    failed = True
                    original_close(value)
                    raise OSError("close result was lost")
                original_close(value)

            replacement_fd: int | None = None
            try:
                with (
                    mock.patch("agent_team.wal.os.close", side_effect=close),
                    self.assertRaises(WalSidecarRecoveryRequiredError),
                ):
                    wal._close_temporary_fd(
                        fd, expected_identity, "close-then-error fd"
                    )
                replacement_fd = os.open(
                    root / "replacement", os.O_CREAT | os.O_RDWR, 0o600
                )
                self.assertEqual(fd, replacement_fd)
                os.write(replacement_fd, b"replacement remains open")
            finally:
                if replacement_fd is not None:
                    try:
                        os.close(replacement_fd)
                    except OSError:
                        pass

    def test_held_fd_reuse_is_rejected_without_unlock_or_close(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-held-reuse-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root, busy_timeout_ms=20).hold_quiescence()
            marker_fd = session._resources.marker_fd
            marker_identity = session._resources.marker_identity
            replacement_fd: int | None = None
            try:
                os.close(marker_fd)
                replacement_fd = os.open(
                    root / "replacement-marker", os.O_CREAT | os.O_RDWR, 0o600
                )
                self.assertEqual(marker_fd, replacement_fd)
                with self.assertRaises(WalSidecarRecoveryRequiredError):
                    session.close()
                self.assertEqual(marker_identity, session._resources.marker_identity)
                os.write(replacement_fd, b"still-open")
            finally:
                if replacement_fd is not None:
                    try:
                        os.close(replacement_fd)
                    except OSError:
                        pass
                session.close()

    def test_unlock_hook_reuse_does_not_close_foreign_held_fd(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-unlock-reuse-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root, busy_timeout_ms=20).hold_quiescence()
            marker_fd = session._resources.marker_fd
            marker_identity = session._resources.marker_identity
            original_close = os.close
            original_flock = fcntl.flock
            replacement_fd: int | None = None
            swapped = False

            def flock(fd: int, operation: int) -> None:
                nonlocal replacement_fd, swapped
                if fd == marker_fd and operation == fcntl.LOCK_UN and not swapped:
                    swapped = True
                    replacement_fd = os.open(
                        root / "foreign-marker",
                        os.O_CREAT | os.O_RDWR,
                        0o600,
                    )
                    os.dup2(replacement_fd, fd)
                    original_close(replacement_fd)
                    replacement_fd = fd
                    return
                original_flock(fd, operation)

            try:
                with (
                    mock.patch("agent_team.wal.fcntl.flock", side_effect=flock),
                    self.assertRaises(WalSidecarRecoveryRequiredError),
                ):
                    session.close()
                self.assertTrue(swapped)
                self.assertEqual(marker_identity, session._resources.marker_identity)
                self.assertIsNotNone(replacement_fd)
                os.fstat(marker_fd)
            finally:
                session.close()
                if replacement_fd is not None:
                    try:
                        original_close(replacement_fd)
                    except OSError:
                        pass

    def test_pending_unlock_reuse_does_not_close_foreign_fd(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-pending-unlock-reuse-"
        ) as temporary:
            root = _make_root(temporary)
            controller = WalSidecarController(root)
            fd = os.open(root / "original", os.O_CREAT | os.O_RDWR, 0o600)
            expected_identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            controller._pending_fds.append(
                wal._PendingFD(
                    fd,
                    expected_identity,
                    "pending marker",
                    True,
                    True,
                )
            )
            original_close = os.close
            original_flock = fcntl.flock
            swapped = False

            def flock(value: int, operation: int) -> None:
                nonlocal swapped
                if value == fd and operation == fcntl.LOCK_UN and not swapped:
                    swapped = True
                    replacement = os.open(
                        root / "foreign",
                        os.O_CREAT | os.O_RDWR,
                        0o600,
                    )
                    os.dup2(replacement, fd)
                    original_close(replacement)
                    return
                original_flock(value, operation)

            try:
                with (
                    mock.patch("agent_team.wal.fcntl.flock", side_effect=flock),
                    self.assertRaises(WalSidecarRecoveryRequiredError) as raised,
                ):
                    controller.close()
                self.assertTrue(swapped)
                raised.exception.retry_cleanup()
                raised.exception.retry_cleanup()
                os.fstat(fd)
                self.assertFalse(controller._pending_fds)
            finally:
                try:
                    original_close(fd)
                except OSError:
                    pass

    def test_recovery_pair_cleanup_handoff_retains_both_fds(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-recovery-pair-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            for name in (
                recovery.RECOVERY_LEDGER_BASENAME,
                recovery.RECOVERY_TOMBSTONES_BASENAME,
            ):
                path = root / name
                path.write_bytes(b"pair")
                path.chmod(0o600)
            session = WalSidecarController(root, busy_timeout_ms=20).hold_quiescence()
            owner = session.issue_owner()
            pair_fds: set[int] = set()
            original_close = os.close
            original_flock = fcntl.flock

            def flock(fd: int, operation: int) -> None:
                if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                    pair_fds.add(fd)
                original_flock(fd, operation)

            def close(fd: int) -> None:
                if fd in pair_fds:
                    raise OSError("persistent recovery pair close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.recovery.fcntl.flock", side_effect=flock),
                    mock.patch("agent_team.recovery.os.close", side_effect=close),
                    self.assertRaises(recovery.RecoveryLedgerError),
                    recovery._locked_restore_files(
                        session._resources.root_fd,
                        owner._retain_failed_fd,
                        None,
                    ),
                ):
                    pass
                self.assertEqual(2, len(session._resources._orphan_fds))
                retained_fds = tuple(fd for fd, _, _ in session._resources._orphan_fds)
                session.close()
                for fd in retained_fds:
                    with self.assertRaises(OSError):
                        os.fstat(fd)
            finally:
                session.close()

    def test_status_unknown_after_temporary_close_failure_is_retained(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-status-unknown-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence(
                allowed_root_names=("temporary",)
            )
            resources = session._resources
            fd = os.open(root / "temporary", os.O_CREAT | os.O_RDWR, 0o600)
            expected_identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            original_fstat = os.fstat
            original_close = os.close
            fstat_calls = 0

            def fstat(value: int) -> os.stat_result:
                nonlocal fstat_calls
                if value == fd:
                    fstat_calls += 1
                    if fstat_calls > 1:
                        raise OSError("descriptor status unavailable")
                return original_fstat(value)

            def close(value: int) -> None:
                if value == fd:
                    raise OSError("close status unavailable")
                original_close(value)

            try:
                with (
                    mock.patch("agent_team.wal.os.fstat", side_effect=fstat),
                    mock.patch("agent_team.wal.os.close", side_effect=close),
                    self.assertRaises(WalSidecarRecoveryRequiredError),
                ):
                    wal._close_fd_into_resources(
                        resources,
                        fd,
                        expected_identity,
                        "status-unknown temporary fd",
                    )
                self.assertEqual(
                    [(fd, expected_identity, "status-unknown temporary fd")],
                    resources._orphan_fds,
                )
            finally:
                session.close()

    def test_close_only_retry_does_not_repeat_successful_unlock(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-unlock-count-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root, busy_timeout_ms=20).hold_quiescence()
            marker_fd = session._resources.marker_fd
            original_close = os.close
            original_flock = fcntl.flock
            failed = True
            unlock_calls = 0

            def close(fd: int) -> None:
                nonlocal failed
                if fd == marker_fd and failed:
                    failed = False
                    raise OSError("one-shot marker close failure")
                original_close(fd)

            def flock(fd: int, operation: int) -> None:
                nonlocal unlock_calls
                if fd == marker_fd and operation == fcntl.LOCK_UN:
                    unlock_calls += 1
                original_flock(fd, operation)

            try:
                with (
                    mock.patch("agent_team.wal.os.close", side_effect=close),
                    mock.patch("agent_team.wal.fcntl.flock", side_effect=flock),
                ):
                    with self.assertRaises(WalSidecarRecoveryRequiredError):
                        session.close()
                    session.close()
                self.assertEqual(1, unlock_calls)
            finally:
                session.close()

    def test_rebind_identity_error_remains_primary_when_new_fd_close_is_uncertain(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-rebind-primary-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            database = root / "coordination.sqlite3"
            alternate = root / "alternate.sqlite3"
            alternate.write_bytes(database.read_bytes())
            alternate.chmod(0o600)
            session = WalSidecarController(root, busy_timeout_ms=20).hold_quiescence()
            root_fd = session._resources.root_fd
            original_open = os.open
            original_stat = os.stat
            original_replace = os.replace
            original_close = os.close
            new_fds: list[int] = []
            database_stat_calls = 0

            def open_file(*args: object, **kwargs: object) -> int:
                fd = original_open(*args, **kwargs)  # type: ignore[arg-type]
                if (
                    args
                    and args[0] == "coordination.sqlite3"
                    and kwargs.get("dir_fd") == root_fd
                ):
                    new_fds.append(fd)
                return fd

            def stat_file(
                path: object, *args: object, **kwargs: object
            ) -> os.stat_result:
                nonlocal database_stat_calls
                result = original_stat(path, *args, **kwargs)  # type: ignore[arg-type]
                if path == "coordination.sqlite3" and kwargs.get("dir_fd") == root_fd:
                    database_stat_calls += 1
                    if database_stat_calls == 2:
                        original_replace(database, root / "original.sqlite3")
                        original_replace(alternate, database)
                        result = original_stat(path, *args, **kwargs)  # type: ignore[arg-type]
                return result

            def close_file(fd: int) -> None:
                if new_fds and fd == new_fds[0]:
                    raise OSError("persistent new descriptor close failure")
                original_close(fd)

            try:
                with (
                    mock.patch("agent_team.wal.os.open", side_effect=open_file),
                    mock.patch("agent_team.wal.os.stat", side_effect=stat_file),
                    mock.patch("agent_team.wal.os.close", side_effect=close_file),
                    self.assertRaises(WalSidecarRecoveryRequiredError) as raised,
                ):
                    session._rebind_database()
                self.assertIn("changed while rebinding", str(raised.exception))
                self.assertIsInstance(
                    raised.exception.__cause__, WalSidecarRecoveryRequiredError
                )
                self.assertTrue(new_fds)
                self.assertTrue(
                    any(fd == new_fds[0] for fd, _, _ in session._resources._orphan_fds)
                )
                retry = getattr(raised.exception, "retry_cleanup", None)
                if not callable(retry):
                    self.fail("rebind error has no cleanup retry")
                retry()
                retry()
                self.assertFalse(session._resources._orphan_fds)
            finally:
                session.close()

    def test_lock_timeout_computes_remaining_once_and_never_sleeps_negative(
        self,
    ) -> None:
        busy = OSError(errno.EAGAIN, "lock busy")
        sleep_calls: list[float] = []

        def sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            if seconds < 0:
                raise ValueError("sleep length must be non-negative")

        with (
            mock.patch("agent_team.wal.fcntl.flock", side_effect=busy),
            mock.patch(
                "agent_team.wal.time.monotonic_ns",
                side_effect=(0, 0, 2_000_000),
            ),
            mock.patch("agent_team.wal.time.sleep", side_effect=sleep),
            self.assertRaises((ValueError, WalSidecarBusyError)) as raised,
        ):
            wal._lock_nonblocking(
                99,
                exclusive=True,
                timeout_ms=1,
                label="timeout boundary",
            )
        self.assertIsInstance(raised.exception, WalSidecarBusyError)
        self.assertEqual([0.001], sleep_calls)

    def test_verify_candidate_body_error_remains_primary_when_fd_close_fails(
        self,
    ) -> None:
        class BodyError(Exception):
            pass

        class FaultController(WalSidecarController):
            __slots__ = ("failed",)

            def __init__(self, state_root: Path) -> None:
                super().__init__(state_root, busy_timeout_ms=20)
                self.failed = False

            def _fault(self, point: str) -> None:
                if point == "after_candidate_verify" and not self.failed:
                    self.failed = True
                    raise BodyError("candidate verification body failed")

        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-verify-primary-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            candidate_path = root / "candidate.db"
            candidate_path.write_bytes(b"candidate")
            candidate_path.chmod(0o600)
            candidate = DatabaseCandidate(
                name=candidate_path.name,
                identity=(candidate_path.stat().st_dev, candidate_path.stat().st_ino),
                size=candidate_path.stat().st_size,
                digest="sha256:"
                + hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
            )
            session = FaultController(root).hold_quiescence(
                allowed_root_names=(candidate_path.name,)
            )
            original_close = wal._close_temporary_fd

            def fail_candidate_close(
                fd: int,
                expected_identity: tuple[int, int] | None,
                label: str,
            ) -> None:
                if label == "database candidate":
                    raise OSError("persistent candidate close failure")
                original_close(fd, expected_identity, label)

            try:
                with (
                    mock.patch(
                        "agent_team.wal._close_temporary_fd",
                        side_effect=fail_candidate_close,
                    ),
                    self.assertRaises(
                        (BodyError, WalSidecarRecoveryRequiredError)
                    ) as raised,
                ):
                    session.verify_candidate(candidate)
                self.assertIsInstance(raised.exception, BodyError)
                self.assertIsInstance(
                    raised.exception.__cause__, WalSidecarRecoveryRequiredError
                )
                self.assertTrue(session._resources._orphan_fds)
                retry = getattr(raised.exception, "retry_cleanup", None)
                if not callable(retry):
                    self.fail("verify error has no cleanup retry")
                retry()
                retry()
                self.assertFalse(session._resources._orphan_fds)
            finally:
                session.close()

    def test_replace_result_body_error_remains_primary_when_fd_close_fails(
        self,
    ) -> None:
        class BodyError(Exception):
            pass

        class FaultController(WalSidecarController):
            __slots__ = ("failed",)

            def __init__(self, state_root: Path) -> None:
                super().__init__(state_root, busy_timeout_ms=20)
                self.failed = False

            def _fault(self, point: str) -> None:
                if point == "after_replace_result" and not self.failed:
                    self.failed = True
                    raise BodyError("replace result body failed")

        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-replace-primary-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = FaultController(root)
            session = controller.hold_quiescence(allowed_root_names=("candidate.db",))
            original_close = wal._close_temporary_fd
            candidate_close_calls = 0
            try:
                copied = session.copy_database_to(
                    CheckpointRequest("TRUNCATE"),
                    DatabaseCopyTarget(name="candidate.db"),
                )
                candidate = DatabaseCandidate(
                    name=copied.target.name,
                    identity=copied.target_identity,
                    size=copied.size,
                    digest=copied.digest,
                )

                def fail_candidate_close(
                    fd: int,
                    expected_identity: tuple[int, int] | None,
                    label: str,
                ) -> None:
                    nonlocal candidate_close_calls
                    if label == "database candidate":
                        candidate_close_calls += 1
                    if label == "database candidate" and candidate_close_calls == 2:
                        raise OSError("persistent candidate close failure")
                    original_close(fd, expected_identity, label)

                with (
                    mock.patch(
                        "agent_team.wal._close_temporary_fd",
                        side_effect=fail_candidate_close,
                    ),
                    self.assertRaises(
                        (BodyError, WalSidecarRecoveryRequiredError)
                    ) as raised,
                ):
                    session.replace_database(candidate)
                self.assertIsInstance(raised.exception, BodyError)
                self.assertIsInstance(
                    raised.exception.__cause__, WalSidecarRecoveryRequiredError
                )
                self.assertTrue(session._resources._orphan_fds)
                retry = getattr(raised.exception, "retry_cleanup", None)
                if not callable(retry):
                    self.fail("replace error has no cleanup retry")
                retry()
                retry()
                self.assertFalse(session._resources._orphan_fds)
            finally:
                session.close()

    def test_session_cleanup_error_attaches_retry_owner_for_attrless_body(self) -> None:
        class AttrlessBodyError(Exception):
            __slots__ = ()

            def __getattribute__(self, name: str) -> object:
                if name == "__dict__":
                    raise AttributeError(name)
                return super().__getattribute__(name)

        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-session-owner-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root, busy_timeout_ms=20).hold_quiescence()
            marker_fd = session._resources.marker_fd
            original_close = os.close
            original_flock = fcntl.flock

            def fail_close(fd: int) -> None:
                if fd == marker_fd:
                    raise OSError("persistent marker close failure")
                original_close(fd)

            def fail_unlock(fd: int, operation: int) -> None:
                if fd == marker_fd and operation == fcntl.LOCK_UN:
                    raise OSError("persistent marker unlock failure")
                original_flock(fd, operation)

            try:
                with (
                    mock.patch("agent_team.wal.os.close", side_effect=fail_close),
                    mock.patch("agent_team.wal.fcntl.flock", side_effect=fail_unlock),
                    self.assertRaises(
                        (AttrlessBodyError, WalSidecarRecoveryRequiredError)
                    ) as raised,
                    session,
                ):
                    raise AttrlessBodyError("attrless body failed")
                self.assertIsInstance(raised.exception, WalSidecarRecoveryRequiredError)
                self.assertIsInstance(raised.exception.__cause__, AttrlessBodyError)
                retry = getattr(raised.exception, "retry_cleanup", None)
                self.assertTrue(callable(retry))
            finally:
                retry = locals().get("retry")
                if callable(retry):
                    retry()
                    retry()
                    self.assertFalse(session._controller._active_sessions)
                session.close()

    def test_resources_body_error_attaches_exact_controller_retry_owner(self) -> None:
        class BodyError(Exception):
            pass

        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-resource-owner-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = WalSidecarController(root, busy_timeout_ms=20)
            marker_fd: int | None = None
            original_close = os.close
            original_flock = fcntl.flock

            def fail_close(fd: int) -> None:
                if marker_fd is not None and fd == marker_fd:
                    raise OSError("persistent marker close failure")
                original_close(fd)

            def fail_unlock(fd: int, operation: int) -> None:
                if (
                    marker_fd is not None
                    and fd == marker_fd
                    and operation == fcntl.LOCK_UN
                ):
                    raise OSError("persistent marker unlock failure")
                original_flock(fd, operation)

            try:
                with (
                    mock.patch("agent_team.wal.os.close", side_effect=fail_close),
                    mock.patch("agent_team.wal.fcntl.flock", side_effect=fail_unlock),
                    self.assertRaises(BodyError) as raised,
                    controller._resources() as resources,
                ):
                    marker_fd = resources.marker_fd
                    raise BodyError("resource body failed")
                retry = getattr(raised.exception, "retry_cleanup", None)
                self.assertTrue(callable(retry))
            finally:
                retry = locals().get("retry")
                if callable(retry):
                    retry()
                    retry()
                    self.assertFalse(controller._pending_resources)
                controller.close()

    def test_pending_fd_overflow_keeps_current_fd_on_composite_retry_owner(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-fd-overflow-"
        ) as temporary:
            root = _make_root(temporary)
            controller = WalSidecarController(root)
            fds: list[int] = []
            current_fd: int | None = None
            for index in range(wal._MAX_PENDING_FDS):
                fd = os.open(root / f"pending-{index}", os.O_CREAT | os.O_RDWR, 0o600)
                fds.append(fd)
                metadata = os.fstat(fd)
                controller._pending_fds.append(
                    wal._PendingFD(
                        fd,
                        (metadata.st_dev, metadata.st_ino),
                        f"pending-{index}",
                        False,
                        False,
                    )
                )
            current_fd = os.open(root / "current", os.O_CREAT | os.O_RDWR, 0o600)
            fds.append(current_fd)
            current_metadata = os.fstat(current_fd)
            try:
                with self.assertRaises(WalSidecarRecoveryRequiredError) as raised:
                    controller._retain_pending_fd(
                        current_fd,
                        (current_metadata.st_dev, current_metadata.st_ino),
                        "current fd",
                        unlock=False,
                        locked=False,
                    )
                retry = getattr(raised.exception, "retry_cleanup", None)
                if not callable(retry):
                    self.fail("overflow error has no cleanup retry")
                retry()
                retry()
                self.assertFalse(controller._pending_fds)
                with self.assertRaises(OSError):
                    os.fstat(current_fd)
            finally:
                for fd in fds:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_connection_overflow_keeps_current_connection_on_composite_retry_owner(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-connection-overflow-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            session = WalSidecarController(root).hold_quiescence()
            resources = session._resources
            connections = [
                sqlite3.connect(":memory:") for _ in range(wal._MAX_RESOURCE_ORPHANS)
            ]
            resources._orphan_connections.extend(connections)
            current = sqlite3.connect(":memory:")
            try:
                with self.assertRaises(WalSidecarRecoveryRequiredError) as raised:
                    resources._retain_connection(current)
                retry = getattr(raised.exception, "retry_cleanup", None)
                if not callable(retry):
                    self.fail("connection overflow error has no cleanup retry")
                retry()
                retry()
                self.assertFalse(resources._orphan_connections)
            finally:
                current.close()
                session.close()

    def test_arbitrary_status_probe_retains_known_pending_fd_for_retry(self) -> None:
        class AttrlessProbe(BaseException):
            __slots__ = ()

            def __getattribute__(self, name: str) -> object:
                if name == "__dict__":
                    raise AttributeError(name)
                return super().__getattribute__(name)

        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-status-probe-"
        ) as temporary:
            root = _make_root(temporary)
            controller = WalSidecarController(root)
            fd = os.open(root / "pending", os.O_CREAT | os.O_RDWR, 0o600)
            metadata = os.fstat(fd)
            original_fstat = os.fstat

            def fstat(value: int) -> os.stat_result:
                if value == fd:
                    raise AttrlessProbe("status probe failed")
                return original_fstat(value)

            try:
                with (
                    mock.patch("agent_team.wal.os.fstat", side_effect=fstat),
                    self.assertRaises(WalSidecarRecoveryRequiredError) as raised,
                ):
                    controller._retain_pending_fd(
                        fd,
                        (metadata.st_dev, metadata.st_ino),
                        "arbitrary status fd",
                        unlock=False,
                        locked=False,
                    )
                self.assertEqual(1, len(controller._pending_fds))
                retry = getattr(raised.exception, "retry_cleanup", None)
                if not callable(retry):
                    self.fail("status probe error has no cleanup retry")
                retry()
                retry()
                self.assertFalse(controller._pending_fds)
                with self.assertRaises(OSError):
                    os.fstat(fd)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_open_fd_status_overflow_keeps_current_fd_on_error_owner(self) -> None:
        class AttrlessProbe(BaseException):
            __slots__ = ()

        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-open-fd-overflow-"
        ) as temporary:
            root = _make_root(temporary)
            controller = WalSidecarController(root)
            fds: list[int] = []
            for index in range(wal._MAX_PENDING_FDS):
                fd = os.open(root / f"pending-{index}", os.O_CREAT | os.O_RDWR, 0o600)
                fds.append(fd)
                metadata = os.fstat(fd)
                controller._pending_fds.append(
                    wal._PendingFD(
                        fd,
                        (metadata.st_dev, metadata.st_ino),
                        f"pending-{index}",
                        False,
                        False,
                    )
                )
            current_fd = os.open(root / "current", os.O_CREAT | os.O_RDWR, 0o600)
            fds.append(current_fd)
            current_metadata = os.fstat(current_fd)
            original_fstat = os.fstat
            probe_calls = 0

            def fstat(value: int) -> os.stat_result:
                nonlocal probe_calls
                if value == current_fd and probe_calls < 2:
                    probe_calls += 1
                    raise AttrlessProbe()
                return original_fstat(value)

            try:
                with (
                    mock.patch("agent_team.wal.os.fstat", side_effect=fstat),
                    self.assertRaises(WalSidecarRecoveryRequiredError) as raised,
                ):
                    controller._cleanup_open_fd(
                        current_fd,
                        (current_metadata.st_dev, current_metadata.st_ino),
                        "current open fd",
                        unlock=False,
                    )
                retry = getattr(raised.exception, "retry_cleanup", None)
                if not callable(retry):
                    self.fail("status-overflow error has no cleanup retry")
                retry()
                retry()
                self.assertFalse(controller._pending_fds)
                with self.assertRaises(OSError):
                    os.fstat(current_fd)
            finally:
                for fd in fds:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_pending_resource_overflow_keeps_current_resources_on_error_owner(
        self,
    ) -> None:
        class Placeholder:
            def close(self) -> None:
                return

        with tempfile.TemporaryDirectory(
            prefix="agent-team-wal-resource-overflow-"
        ) as temporary:
            root = _make_root(temporary)
            with CoordinationStore(root):
                pass
            controller = WalSidecarController(root)
            try:
                with controller._resources() as resources:
                    controller._pending_resources.extend(
                        cast(wal._Resources, Placeholder())
                        for _ in range(wal._MAX_PENDING_RESOURCES)
                    )
                    with self.assertRaises(WalSidecarRecoveryRequiredError) as raised:
                        controller._retain_pending_resources(resources)
                    retry = getattr(raised.exception, "retry_cleanup", None)
                    if not callable(retry):
                        self.fail("resource-overflow error has no cleanup retry")
                    retry()
                    retry()
                    self.assertFalse(controller._pending_resources)
                    with self.assertRaises(OSError):
                        os.fstat(resources.marker_fd)
            finally:
                controller.close()

    def test_existing_cleanup_owner_is_composed_and_all_members_are_attempted(
        self,
    ) -> None:
        calls: list[str] = []
        previous_failures = 1

        def previous() -> None:
            nonlocal previous_failures
            calls.append("previous")
            if previous_failures:
                previous_failures -= 1
                raise OSError("previous cleanup failed")

        def current() -> None:
            calls.append("current")

        error = WalSidecarRecoveryRequiredError("existing cleanup")
        error._attach_cleanup_capability(_CleanupCapability(previous))
        attached = wal._attach_cleanup_owner(error, current, "composite cleanup")
        with self.assertRaises(OSError) as raised:
            attached.retry_cleanup()  # type: ignore[attr-defined]
        self.assertEqual(1, calls.count("previous"))
        self.assertEqual(1, calls.count("current"))
        self.assertEqual("previous cleanup failed", str(raised.exception))
        self.assertTrue(callable(getattr(raised.exception, "retry_cleanup", None)))
        self.assertIsNotNone(getattr(raised.exception, "_cleanup_capability", None))

        raised.exception.retry_cleanup()  # type: ignore[attr-defined]
        self.assertEqual(2, calls.count("previous"))
        self.assertEqual(1, calls.count("current"))
        raised.exception.retry_cleanup()  # type: ignore[attr-defined]
        self.assertEqual(2, calls.count("previous"))
        self.assertEqual(1, calls.count("current"))


if __name__ == "__main__":
    unittest.main()
