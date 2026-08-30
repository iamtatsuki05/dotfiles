from __future__ import annotations

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
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Literal, cast
from unittest import mock

from test_lease_provider import FakeClock, FakeProvider

from agent_team.doctor import (
    DoctorReport,
    FilesetInventory,
    FilesystemEntry,
    ReadOnlyDoctor,
    RecoveryLedgerReader,
    StateFilesystem,
    StateFilesystemError,
    UnstableSnapshotError,
)
from agent_team.store import WRITER_MARKER_CLEAN_CONTENT, CoordinationStore

MARKER_NAME = "writer.marker"
LEDGER_NAME = "recovery.ledger"


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


class DoctorValueTest(unittest.TestCase):
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
            ) -> tuple[int, os.stat_result]:
                fd, metadata = super()._open_regular(name)
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
            self.assertEqual("QUERY_PROVIDER_THEN_RESOLVE", pending.safe_action)
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
                    "audit_ref": f"audit/{sequence}",
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
