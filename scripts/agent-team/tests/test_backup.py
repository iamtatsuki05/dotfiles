from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import signal
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest import mock

from agent_team import backup as backup_module
from agent_team import doctor as doctor_module
from agent_team import store as store_module
from agent_team import wal as wal_module
from agent_team.backup import (
    BACKUP_MANIFEST_FIELDS,
    BACKUP_MANIFEST_VERSION,
    BackupArtifact,
    BackupDurabilityUnknownError,
    BackupFilesystemError,
    BackupIncompleteError,
    BackupIntegrationError,
    BackupIntegrityError,
    BackupManifest,
    SQLiteBackup,
    _decode_manifest,
    _encode_manifest,
)
from agent_team.lease import RecoveryFloor
from agent_team.store import CoordinationStore
from agent_team.wal import (
    CheckpointRequest,
    DatabaseCopyResult,
    DatabaseCopyTarget,
    QuiescenceSession,
    WalSidecarController,
)


def _make_root(temporary: str) -> Path:
    root = Path(os.path.realpath(temporary)) / "state"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    with CoordinationStore(root):
        pass
    return root


class DeterministicBackup(SQLiteBackup):
    nonce = "0123456789abcdef0123456789abcdef"

    def __init__(self, state_root: Path) -> None:
        super().__init__(state_root, busy_timeout_ms=100)
        self.fault_point: str | None = None
        self.fault_events: list[str] = []

    def _next_nonce(self) -> str:
        return self.nonce

    def _fault(self, point: str) -> None:
        self.fault_events.append(point)
        if point == self.fault_point:
            raise RuntimeError(f"injected backup fault: {point}")


class KillAtBackup(DeterministicBackup):
    def _fault(self, point: str) -> None:
        if point == self.fault_point:
            os.kill(os.getpid(), signal.SIGKILL)


class RecordingController(WalSidecarController):
    def __init__(self, state_root: Path) -> None:
        super().__init__(state_root, busy_timeout_ms=100)
        self.hold_count = 0
        self.copy_count = 0

    def hold_quiescence(
        self,
        *,
        allowed_root_names: tuple[str, ...] = (),
    ) -> QuiescenceSession:
        self.hold_count += 1
        return super().hold_quiescence(allowed_root_names=allowed_root_names)

    def _copy_database_to(
        self,
        resources: object,
        request: CheckpointRequest,
        target: DatabaseCopyTarget,
    ) -> DatabaseCopyResult:
        self.copy_count += 1
        return super()._copy_database_to(resources, request, target)  # type: ignore[arg-type]


class RecordingBackup(DeterministicBackup):
    controller: RecordingController | None = None

    def _new_controller(self) -> WalSidecarController:
        self.controller = RecordingController(self.state_root)
        return self.controller


class SourceKillController(WalSidecarController):
    def __init__(self, state_root: Path, fault_point: str) -> None:
        super().__init__(state_root, busy_timeout_ms=100)
        self.fault_point = fault_point

    def _fault(self, point: str) -> None:
        if point == self.fault_point:
            os.kill(os.getpid(), signal.SIGKILL)


class SourceFaultBackup(DeterministicBackup):
    source_fault_point: str | None = None

    def _new_controller(self) -> WalSidecarController:
        if self.source_fault_point is None:
            return super()._new_controller()
        return SourceKillController(self.state_root, self.source_fault_point)


class MarkerLockFaultController(WalSidecarController):
    def _fault(self, point: str) -> None:
        if point == "after_marker_lock":
            raise BackupFilesystemError("injected marker-lock acquisition fault")


class MarkerLockFaultBackup(DeterministicBackup):
    def _new_controller(self) -> WalSidecarController:
        return MarkerLockFaultController(self.state_root, busy_timeout_ms=100)


class AttrlessBody(BaseException):
    pass


class CaptureDescriptorsBackup(DeterministicBackup):
    root_fd: int | None = None
    existing_database_fd: int | None = None
    held_fds: dict[str, int]

    def __init__(self, state_root: Path) -> None:
        super().__init__(state_root)
        self.held_fds = {}

    def _open_root(self) -> tuple[int, tuple[int, int], tuple[int, ...]]:
        result = super()._open_root()
        self.root_fd = result[0]
        self.held_fds["backup root"] = result[0]
        return result

    def _open_existing_regular(
        self,
        root_fd: int,
        name: str,
        *,
        label: str,
        max_size: int | None = None,
    ) -> tuple[int, os.stat_result]:
        result = super()._open_existing_regular(
            root_fd,
            name,
            label=label,
            max_size=max_size,
        )
        self.held_fds[label] = result[0]
        if label == "backup database":
            self.existing_database_fd = result[0]
        return result


def _rewrite_manifest_digest(root: Path, database_name: str) -> None:
    database = root / database_name
    manifest_path = root / f"{database_name}.manifest"
    parsed = _decode_manifest(manifest_path.read_bytes())
    digest = "sha256:" + hashlib.sha256(database.read_bytes()).hexdigest()
    manifest_path.write_bytes(_encode_manifest(replace(parsed, database_digest=digest)))
    manifest_path.chmod(0o600)


def _valid_manifest(
    *,
    name: str = "snapshot",
    size: int = 1,
    digest: str = "sha256:" + "a" * 64,
    epoch: int = 0,
    floor: int = 0,
) -> BackupManifest:
    return BackupManifest(
        version=BACKUP_MANIFEST_VERSION,
        database_basename=name,
        store_schema=2,
        event_schema_version=2,
        sqlite_user_version=2,
        integrity_check="ok",
        database_size=size,
        database_digest=digest,
        captured_recovery_epoch=epoch,
        captured_fencing_token_floor=floor,
    )


class BackupManifestTest(unittest.TestCase):
    def test_manifest_canonical_encoding_and_strict_decoding(self) -> None:
        manifest = _valid_manifest()
        raw = _encode_manifest(manifest)
        self.assertEqual(
            b'{"version":1,"database_basename":"snapshot",'
            b'"store_schema":2,"event_schema_version":2,"sqlite_user_version":2,'
            b'"integrity_check":"ok","database_size":1,'
            b'"database_digest":"sha256:' + b"a" * 64 + b'",'
            b'"captured_recovery_epoch":0,"captured_fencing_token_floor":0}\n',
            raw,
        )
        self.assertEqual(manifest, _decode_manifest(raw))
        parsed = json.loads(raw, object_pairs_hook=list)
        self.assertEqual(BACKUP_MANIFEST_FIELDS, tuple(key for key, _ in parsed))

        cases = (
            raw.replace(b'"version":1', b'"version":1,"version":1'),
            raw.replace(b"}\n", b',"unknown":1}\n'),
            raw.replace(b',"captured_fencing_token_floor":0', b""),
            raw.replace(b'"database_size":1', b'"database_size":1.0'),
            raw.replace(b'"database_size":1', b'"database_size":true'),
            raw.replace(b'"database_size":1', b'"database_size":-1'),
            raw.replace(b'"database_size":1', b'"database_size":1e100'),
            raw.replace(b'"snapshot"', b'"snap\\u0073hot"'),
            raw.replace(b"\n", b"\r\n"),
            b"\xef\xbb\xbf" + raw,
            raw[:-1] + b" \n",
        )
        for candidate in cases:
            with (
                self.subTest(candidate=candidate),
                self.assertRaises(BackupIntegrityError),
            ):
                _decode_manifest(candidate)

    def test_manifest_rejects_invalid_runtime_values(self) -> None:
        base = {
            "version": 1,
            "database_basename": "snapshot",
            "store_schema": 2,
            "event_schema_version": 2,
            "sqlite_user_version": 2,
            "integrity_check": "ok",
            "database_size": 1,
            "database_digest": "sha256:" + "a" * 64,
            "captured_recovery_epoch": 0,
            "captured_fencing_token_floor": 0,
        }
        invalid = (
            ("version", True),
            ("database_basename", "../snapshot"),
            ("database_basename", "coordination.sqlite3"),
            ("database_basename", "a" * 247),
            ("store_schema", 3),
            ("event_schema_version", 3),
            ("sqlite_user_version", 3),
            ("integrity_check", "OK"),
            ("database_size", -1),
            ("database_digest", "sha256:" + "A" * 64),
            ("captured_recovery_epoch", 2**63),
            ("captured_fencing_token_floor", -1),
        )
        for key, value in invalid:
            with self.subTest(key=key):
                candidate = dict(base)
                candidate[key] = value
                with self.assertRaises((BackupIntegrityError, ValueError)):
                    _decode_manifest(
                        json.dumps(
                            candidate,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode()
                        + b"\n"
                    )


class BackupFilesystemContractTest(unittest.TestCase):
    def test_destination_basename_and_derived_names_fail_fast(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            invalid = (
                "",
                ".",
                "..",
                "snapshot/child",
                "snapshot\\child",
                "snapshot\x00child",
                "\ud800",
                "coordination.sqlite3",
                "writer.marker",
                "recovery.ledger",
                "recovery.tombstones",
                ".coordination-lifetime.lock",
                "a" * 247,
            )
            for name in invalid:
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        backup.create(name)
                    with self.assertRaises(ValueError):
                        backup.inspect(name)
            self.assertTrue((root / "coordination.sqlite3").exists())
            self.assertTrue((root / "writer.marker").exists())

    def test_restore_candidate_namespace_is_reserved_before_any_io(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            candidate_prefix = ".coordination.sqlite3.restore-"
            invalid = (
                candidate_prefix,
                candidate_prefix + "short",
                candidate_prefix + "a" * 64,
                candidate_prefix + "a" * 65,
                candidate_prefix + "a" * 64 + ".manifest",
            )
            for name in invalid:
                with self.subTest(name=name):
                    path = root / name
                    path.write_bytes(b"foreign candidate namespace entry")
                    path.chmod(0o600)
                    before = (path.read_bytes(), path.stat().st_ino)
                    with (
                        mock.patch.object(
                            SQLiteBackup,
                            "_retry_retained_resources",
                            side_effect=AssertionError("namespace check came too late"),
                        ),
                        mock.patch.object(
                            SQLiteBackup,
                            "_open_root",
                            side_effect=AssertionError("namespace check reached root"),
                        ) as open_root,
                        mock.patch.object(
                            WalSidecarController,
                            "hold_quiescence",
                            side_effect=AssertionError(
                                "namespace check reached controller"
                            ),
                        ) as hold_quiescence,
                        self.assertRaises(ValueError),
                    ):
                        backup.create(name)
                    open_root.assert_not_called()
                    hold_quiescence.assert_not_called()
                    with (
                        mock.patch.object(
                            SQLiteBackup,
                            "_retry_retained_resources",
                            side_effect=AssertionError("namespace check came too late"),
                        ),
                        mock.patch.object(
                            SQLiteBackup,
                            "_open_root",
                            side_effect=AssertionError("namespace check reached root"),
                        ) as open_root,
                        mock.patch.object(
                            WalSidecarController,
                            "hold_quiescence",
                            side_effect=AssertionError(
                                "namespace check reached controller"
                            ),
                        ) as hold_quiescence,
                        self.assertRaises(ValueError),
                    ):
                        backup.inspect(name)
                    open_root.assert_not_called()
                    hold_quiescence.assert_not_called()
                    self.assertEqual(before, (path.read_bytes(), path.stat().st_ino))

            ordinary = backup.create(".ordinary")
            self.assertEqual(ordinary, backup.inspect(".ordinary"))

    def test_manifest_temp_is_exclusive_private_and_foreign_files_survive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.fault_point = "before_manifest_write"
            foreign = root / "provider.receipt"
            foreign.write_bytes(b"keep")
            foreign.chmod(0o600)
            with self.assertRaises(RuntimeError):
                backup.create("snapshot")
            self.assertEqual(b"keep", foreign.read_bytes())
            db_temp = root / f".{backup.nonce}.db.tmp"
            self.assertTrue(db_temp.exists())
            manifest_temp = root / f".{backup.nonce}.manifest.tmp"
            self.assertTrue(manifest_temp.exists())
            self.assertEqual(0o600, db_temp.stat().st_mode & 0o777)
            self.assertEqual(0o600, manifest_temp.stat().st_mode & 0o777)
            self.assertEqual(1, db_temp.stat().st_nlink)
            self.assertEqual(1, manifest_temp.stat().st_nlink)
            self.assertFalse((root / "snapshot").exists())
            self.assertFalse((root / "snapshot.manifest").exists())

    def test_temp_name_collision_is_fail_closed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            collision = root / f".{backup.nonce}.db.tmp"
            collision.write_bytes(b"foreign")
            collision.chmod(0o600)
            with self.assertRaises(BackupFilesystemError):
                backup.create("snapshot")
            self.assertEqual(b"foreign", collision.read_bytes())
            self.assertFalse((root / "snapshot").exists())

    def test_existing_pair_policy_does_not_repair_one_side_or_invalid_pair(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.create("snapshot")
            database = root / "snapshot"
            manifest = root / "snapshot.manifest"
            database_before = database.read_bytes()
            database_identity = (database.stat().st_dev, database.stat().st_ino)
            manifest.unlink()
            with self.assertRaises(BackupIncompleteError):
                backup.create("snapshot")
            self.assertEqual(database_before, database.read_bytes())
            self.assertEqual(
                database_identity, (database.stat().st_dev, database.stat().st_ino)
            )

            manifest.write_bytes(b"not-canonical\n")
            manifest.chmod(0o600)
            with self.assertRaises(BackupIntegrityError):
                backup.create("snapshot")
            self.assertEqual(database_before, database.read_bytes())
            self.assertEqual(
                database_identity, (database.stat().st_dev, database.stat().st_ino)
            )

    def test_inspect_is_read_only_and_rejects_backup_sidecars(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            result = backup.create("snapshot")
            database = root / "snapshot"
            manifest = root / "snapshot.manifest"
            before = (
                database.read_bytes(),
                manifest.read_bytes(),
                database.stat().st_ino,
                manifest.stat().st_ino,
                database.stat().st_mtime_ns,
                manifest.stat().st_mtime_ns,
            )
            inspected = backup.inspect("snapshot")
            self.assertIsInstance(result, BackupArtifact)
            self.assertEqual(result, inspected)
            after = (
                database.read_bytes(),
                manifest.read_bytes(),
                database.stat().st_ino,
                manifest.stat().st_ino,
                database.stat().st_mtime_ns,
                manifest.stat().st_mtime_ns,
            )
            self.assertEqual(before, after)

            sidecar = root / "snapshot-wal"
            sidecar.write_bytes(b"foreign")
            sidecar.chmod(0o600)
            with self.assertRaises(BackupFilesystemError):
                backup.inspect("snapshot")
            self.assertEqual(b"foreign", sidecar.read_bytes())

    def test_existing_unsafe_pair_entries_are_rejected_without_replacement(
        self,
    ) -> None:
        cases = ("symlink", "fifo", "hardlink", "mode")
        for case in cases:
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary,
            ):
                root = _make_root(temporary)
                backup = DeterministicBackup(root)
                backup.create("snapshot")
                database = root / "snapshot"
                manifest = root / "snapshot.manifest"
                original_database = database.read_bytes()
                original_manifest = manifest.read_bytes()
                if case == "symlink":
                    database.unlink()
                    database.symlink_to(root / "coordination.sqlite3")
                elif case == "fifo":
                    database.unlink()
                    os.mkfifo(database, mode=0o600)
                elif case == "hardlink":
                    os.link(database, root / "snapshot-copy")
                else:
                    database.chmod(0o644)
                with self.assertRaises(BackupFilesystemError):
                    backup.create("snapshot")
                if case not in {"symlink", "fifo"}:
                    self.assertEqual(original_database, database.read_bytes())
                self.assertEqual(original_manifest, manifest.read_bytes())

    def test_destination_appearance_after_preflight_is_not_overwritten(self) -> None:
        class AppearingDestinationBackup(DeterministicBackup):
            def _fault(self, point: str) -> None:
                super()._fault(point)
                if point == "before_publish_recheck":
                    destination = self.state_root / "snapshot"
                    destination.write_bytes(b"foreign")
                    destination.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = AppearingDestinationBackup(root)
            with self.assertRaises(BackupFilesystemError):
                backup.create("snapshot")
            self.assertEqual(b"foreign", (root / "snapshot").read_bytes())
            self.assertFalse((root / "snapshot.manifest").exists())

    def test_db_temp_path_swap_is_rejected_without_foreign_temp_unlink(self) -> None:
        class SwapDatabaseTempBackup(DeterministicBackup):
            def _fault(self, point: str) -> None:
                super()._fault(point)
                if point == "before_db_replace":
                    temp_path = self.state_root / f".{self.nonce}.db.tmp"
                    foreign_path = self.state_root / ".foreign-db-temp"
                    temp_path.rename(foreign_path)
                    temp_path.write_bytes(b"foreign")
                    temp_path.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = SwapDatabaseTempBackup(root)
            with self.assertRaises(BackupFilesystemError):
                backup.create("snapshot")
            self.assertTrue((root / ".foreign-db-temp").exists())
            self.assertEqual(
                b"foreign", (root / f".{backup.nonce}.db.tmp").read_bytes()
            )
            self.assertFalse((root / "snapshot").exists())
            self.assertFalse((root / "snapshot.manifest").exists())

    def test_db_temp_content_swap_is_rejected_before_rename(self) -> None:
        class MutateDatabaseTempBackup(DeterministicBackup):
            def _fault(self, point: str) -> None:
                super()._fault(point)
                if point == "before_db_replace":
                    temp_path = self.state_root / f".{self.nonce}.db.tmp"
                    fd = os.open(temp_path, os.O_RDWR)
                    try:
                        original = os.pread(fd, 1, 0)
                        os.pwrite(fd, bytes((original[0] ^ 1,)), 0)
                    finally:
                        os.close(fd)

        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = MutateDatabaseTempBackup(root)
            with self.assertRaises(BackupIntegrityError):
                backup.create("snapshot")
            self.assertFalse((root / "snapshot").exists())
            self.assertFalse((root / "snapshot.manifest").exists())

    def test_manifest_temp_path_swap_is_rejected_before_manifest_rename(self) -> None:
        class SwapManifestTempBackup(DeterministicBackup):
            def _fault(self, point: str) -> None:
                super()._fault(point)
                if point == "before_manifest_replace":
                    temp_path = self.state_root / f".{self.nonce}.manifest.tmp"
                    foreign_path = self.state_root / ".foreign-manifest-temp"
                    temp_path.rename(foreign_path)
                    temp_path.write_bytes(b"foreign")
                    temp_path.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = SwapManifestTempBackup(root)
            with self.assertRaises(BackupFilesystemError):
                backup.create("snapshot")
            self.assertTrue((root / "snapshot").exists())
            self.assertFalse((root / "snapshot.manifest").exists())
            self.assertTrue((root / ".foreign-manifest-temp").exists())
            self.assertEqual(
                b"foreign",
                (root / f".{backup.nonce}.manifest.tmp").read_bytes(),
            )

    def test_existing_pair_precondition_drift_is_not_overwritten(self) -> None:
        class DriftBackup(DeterministicBackup):
            def _fault(self, point: str) -> None:
                super()._fault(point)
                if point == "before_db_replace":
                    manifest_path = self.state_root / "snapshot.manifest"
                    manifest_path.write_bytes(manifest_path.read_bytes() + b"drift")
                    manifest_path.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            initial = DeterministicBackup(root)
            initial.create("snapshot")
            database = root / "snapshot"
            manifest = root / "snapshot.manifest"
            old_database = database.read_bytes()
            old_database_identity = (database.stat().st_dev, database.stat().st_ino)
            old_manifest = manifest.read_bytes()
            backup = DriftBackup(root)
            with self.assertRaises(BackupFilesystemError):
                backup.create("snapshot")
            self.assertEqual(old_database, database.read_bytes())
            self.assertEqual(
                old_database_identity,
                (database.stat().st_dev, database.stat().st_ino),
            )
            self.assertEqual(old_manifest + b"drift", manifest.read_bytes())

    def test_manifest_appearing_after_db_replace_is_not_overwritten(self) -> None:
        class AppearingManifestBackup(DeterministicBackup):
            def _fault(self, point: str) -> None:
                super()._fault(point)
                if point == "after_db_replace":
                    manifest_path = self.state_root / "snapshot.manifest"
                    manifest_path.write_bytes(b"foreign")
                    manifest_path.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = AppearingManifestBackup(root)
            with self.assertRaises(BackupIncompleteError):
                backup.create("snapshot")
            self.assertTrue((root / "snapshot").exists())
            self.assertEqual(b"foreign", (root / "snapshot.manifest").read_bytes())

    def test_existing_manifest_drift_after_db_replace_is_not_overwritten(self) -> None:
        class DriftAfterDatabaseReplaceBackup(DeterministicBackup):
            def _fault(self, point: str) -> None:
                super()._fault(point)
                if point == "after_db_replace":
                    manifest_path = self.state_root / "snapshot.manifest"
                    manifest_path.write_bytes(manifest_path.read_bytes() + b"drift")
                    manifest_path.chmod(0o600)

        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            initial = DeterministicBackup(root)
            initial.create("snapshot")
            manifest = root / "snapshot.manifest"
            old_manifest = manifest.read_bytes()
            backup = DriftAfterDatabaseReplaceBackup(root)
            with self.assertRaises(BackupFilesystemError):
                backup.create("snapshot")
            self.assertEqual(old_manifest + b"drift", manifest.read_bytes())


class BackupPublishTest(unittest.TestCase):
    def test_create_final_inspect_is_bound_to_own_published_pair(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            SQLiteBackup(root, busy_timeout_ms=100).create("foreign")
            backup = DeterministicBackup(root)
            original_inspect_pair = backup._inspect_pair

            def inspect_foreign(
                root_fd: int,
                database_name: str,
            ) -> tuple[BackupArtifact, object]:
                del database_name
                return original_inspect_pair(root_fd, "foreign")

            with (
                mock.patch.object(
                    backup,
                    "_inspect_pair",
                    side_effect=inspect_foreign,
                ),
                self.assertRaises(BackupIntegrityError),
            ):
                backup.create("snapshot")

    def test_wildcard_destination_names_fail_before_retained_resource_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            with mock.patch.object(
                backup,
                "_retry_retained_resources",
                side_effect=AssertionError("wildcard validation must be first"),
            ):
                for name in ("snapshot*", "snapshot?", "snapshot[", "snapshot]"):
                    with self.assertRaises(ValueError):
                        backup.create(name)
                    with self.assertRaises(ValueError):
                        backup.inspect(name)

    def test_final_manifest_metadata_drift_is_rejected(self) -> None:
        class DriftAfterManifestPathCheckBackup(DeterministicBackup):
            def _assert_fd_path(
                self,
                root_fd: int,
                fd: int,
                name: str,
                *,
                label: str,
                expected_identity: tuple[int, int],
                expected_signature: tuple[int, ...],
                allow_missing: bool = False,
            ) -> os.stat_result | None:
                result = super()._assert_fd_path(
                    root_fd,
                    fd,
                    name,
                    label=label,
                    expected_identity=expected_identity,
                    expected_signature=expected_signature,
                    allow_missing=allow_missing,
                )
                if label == "backup manifest" and name == "snapshot.manifest":
                    manifest_path = self.state_root / name
                    metadata = manifest_path.stat()
                    os.utime(
                        manifest_path,
                        ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1),
                    )
                return result

        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DriftAfterManifestPathCheckBackup(root)
            with self.assertRaises(BackupIntegrityError):
                backup.create("snapshot")

    def test_open_root_adopts_lower_store_cleanup_capability(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            cleanup_calls: list[str] = []
            lower_error = store_module.StoreUnavailableError("lower root failure")
            store_module._attach_cleanup_capability(
                lower_error,
                store_module._CleanupCapability(
                    lambda: cleanup_calls.append("lower"),
                ),
            )
            with (
                mock.patch.object(
                    store_module,
                    "_open_state_root",
                    side_effect=lower_error,
                ),
                self.assertRaises(BackupFilesystemError) as raised,
            ):
                backup._open_root()

            self.assertIs(lower_error, raised.exception.__cause__)
            raised.exception.retry_cleanup()
            self.assertEqual(["lower"], cleanup_calls)

    def test_attrless_post_open_fstat_retains_identity_safe_fd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.create("snapshot")
            root_fd = os.open(root, os.O_RDONLY)
            original_open = os.open
            original_fstat = os.fstat
            original_close = os.close
            original_attach = store_module._attach_cleanup_capability
            target_fd: int | None = None
            body_error = AttrlessBody("post-open status unavailable")

            def open_file(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
                nonlocal target_fd
                fd = original_open(path, flags, *args, **kwargs)
                if path == "snapshot":
                    target_fd = fd
                return fd

            def fstat(fd: int) -> os.stat_result:
                if target_fd is not None and fd == target_fd:
                    raise body_error
                return original_fstat(fd)

            def close(fd: int) -> None:
                if target_fd is not None and fd == target_fd:
                    raise OSError("persistent close failure")
                original_close(fd)

            def attach(
                error: BaseException,
                capability: store_module._CleanupCapability,
            ) -> None:
                if error is body_error:
                    raise TypeError("body has no writable attributes")
                original_attach(error, capability)

            try:
                with (
                    mock.patch.object(os, "open", side_effect=open_file),
                    mock.patch.object(os, "fstat", side_effect=fstat),
                    mock.patch.object(os, "close", side_effect=close),
                    mock.patch.object(
                        store_module,
                        "_attach_cleanup_capability",
                        side_effect=attach,
                    ),
                    self.assertRaises(BackupDurabilityUnknownError) as raised,
                ):
                    backup._open_existing_regular(
                        root_fd,
                        "snapshot",
                        label="backup database",
                    )
                self.assertIs(body_error, raised.exception.__cause__)
                self.assertEqual(1, len(backup._orphan_fds))
                assert target_fd is not None
                self.assertEqual(target_fd, backup._orphan_fds[0][0])
                backup.close()
                self.assertEqual([], backup._orphan_fds)
                with self.assertRaises(OSError):
                    os.fstat(target_fd)
            finally:
                try:
                    original_close(root_fd)
                except OSError:
                    pass

    def test_cleanup_chain_attempts_previous_and_current_best_effort(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            calls: list[str] = []
            previous_fails = True

            def previous() -> None:
                nonlocal previous_fails
                calls.append("previous")
                if previous_fails:
                    previous_fails = False
                    raise OSError("previous cleanup failed")

            def current() -> None:
                calls.append("current")

            error = BackupDurabilityUnknownError("cleanup chain")
            store_module._attach_cleanup_capability(
                error,
                store_module._CleanupCapability(previous),
            )
            backup._attach_cleanup_capability(error, current)

            with self.assertRaises(OSError):
                error.retry_cleanup()
            self.assertCountEqual(["previous", "current"], calls)
            self.assertEqual(1, calls.count("previous"))
            self.assertEqual(1, calls.count("current"))
            error.retry_cleanup()
            self.assertEqual(2, calls.count("previous"))
            self.assertEqual(1, calls.count("current"))

    def test_controller_overflow_keeps_current_owner_on_acquisition_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup._orphan_controllers.extend(
                cast(
                    WalSidecarController,
                    mock.Mock(),
                )
                for _ in range(backup_module._MAX_ORPHAN_CONTROLLERS)
            )
            acquisition_error = BackupFilesystemError("controller acquisition")
            controller = mock.Mock()
            controller.hold_quiescence.side_effect = acquisition_error
            controller.close.side_effect = OSError("controller close")

            with self.assertRaises(BackupFilesystemError) as raised:
                backup._hold_quiescence(cast(WalSidecarController, controller))
            self.assertIs(acquisition_error, raised.exception)
            self.assertEqual(
                backup_module._MAX_ORPHAN_CONTROLLERS,
                len(backup._orphan_controllers),
            )

            controller.close.side_effect = None
            raised.exception.retry_cleanup()
            self.assertEqual([], backup._orphan_controllers)
            self.assertGreaterEqual(controller.close.call_count, 2)

    def test_session_overflow_keeps_current_owner_on_cleanup_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup._orphan_sessions.extend(
                cast(QuiescenceSession, mock.Mock())
                for _ in range(backup_module._MAX_ORPHAN_SESSIONS)
            )
            session = mock.Mock()
            session.close.side_effect = OSError("session close")

            with (
                self.assertRaises(OSError) as raised,
                backup._session_lifecycle(cast(QuiescenceSession, session)),
            ):
                pass
            self.assertEqual(
                backup_module._MAX_ORPHAN_SESSIONS,
                len(backup._orphan_sessions),
            )

            session.close.side_effect = None
            cast(Any, raised.exception).retry_cleanup()
            self.assertEqual([], backup._orphan_sessions)
            self.assertGreaterEqual(session.close.call_count, 2)

    def test_attrless_body_with_session_overflow_keeps_body_and_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup._orphan_sessions.extend(
                cast(QuiescenceSession, mock.Mock())
                for _ in range(backup_module._MAX_ORPHAN_SESSIONS)
            )
            session = mock.Mock()
            session.close.side_effect = OSError("session close")
            body_error = AttrlessBody("attrless session body")
            original_attach = store_module._attach_cleanup_capability

            def attach(
                error: BaseException,
                capability: store_module._CleanupCapability,
            ) -> None:
                if error is body_error:
                    raise TypeError("body has no writable attributes")
                original_attach(error, capability)

            with (
                mock.patch.object(
                    store_module,
                    "_attach_cleanup_capability",
                    side_effect=attach,
                ),
                self.assertRaises(BackupDurabilityUnknownError) as raised,
                backup._session_lifecycle(cast(QuiescenceSession, session)),
            ):
                raise body_error

            self.assertIs(body_error, raised.exception.__cause__)
            session.close.side_effect = None
            raised.exception.retry_cleanup()
            self.assertEqual([], backup._orphan_sessions)

    def test_fd_overflow_keeps_current_owner_on_cleanup_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.create("snapshot")
            registry_fds: list[int] = []
            current_fd = os.open(root / "snapshot", os.O_RDONLY)
            current_identity = (
                os.fstat(current_fd).st_dev,
                os.fstat(current_fd).st_ino,
            )
            try:
                for _ in range(backup_module._MAX_ORPHAN_FDS):
                    fd = os.open(root / "snapshot", os.O_RDONLY)
                    registry_fds.append(fd)
                    backup._orphan_fds.append((fd, current_identity, "retained"))

                original_close_temporary_fd = doctor_module._close_temporary_fd
                failed = False

                def close_temporary_fd(
                    fd: int,
                    expected_identity: tuple[int, int] | None,
                    label: str,
                ) -> None:
                    nonlocal failed
                    if fd == current_fd and not failed:
                        failed = True
                        raise doctor_module.StateFilesystemError(
                            "current fd close failure"
                        )
                    original_close_temporary_fd(fd, expected_identity, label)

                with (
                    mock.patch.object(
                        doctor_module,
                        "_close_temporary_fd",
                        side_effect=close_temporary_fd,
                    ),
                    self.assertRaises(BackupDurabilityUnknownError) as raised,
                ):
                    backup_module._close_fds(
                        ((current_fd, current_identity, "current"),),
                        orphan_registry=backup._orphan_fds,
                        cleanup_callback=backup.close,
                    )

                self.assertEqual(
                    backup_module._MAX_ORPHAN_FDS,
                    len(backup._orphan_fds),
                )
                raised.exception.retry_cleanup()
                self.assertEqual([], backup._orphan_fds)
                with self.assertRaises(OSError):
                    os.fstat(current_fd)
            finally:
                for fd in registry_fds:
                    try:
                        os.close(fd)
                    except OSError as close_error:
                        if close_error.errno != errno.EBADF:
                            raise
                try:
                    os.close(current_fd)
                except OSError as close_error:
                    if close_error.errno != errno.EBADF:
                        raise

    def test_retained_resource_registries_are_bounded_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-backup-registry-"
        ) as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.create("snapshot")

            session = cast(QuiescenceSession, object())
            backup._retain_session(session)
            backup._retain_session(session)
            self.assertEqual(1, len(backup._orphan_sessions))
            backup._orphan_sessions.clear()
            for _ in range(backup_module._MAX_ORPHAN_SESSIONS):
                backup._retain_session(cast(QuiescenceSession, object()))
            session_owner = mock.Mock()
            with self.assertRaises(BackupDurabilityUnknownError) as raised:
                backup._retain_session(cast(QuiescenceSession, session_owner))
            raised.exception.retry_cleanup()
            session_owner.close.assert_called_once_with()
            backup._orphan_sessions.clear()

            controller = cast(WalSidecarController, object())
            backup._retain_controller(controller)
            backup._retain_controller(controller)
            self.assertEqual(1, len(backup._orphan_controllers))
            backup._orphan_controllers.clear()
            for _ in range(backup_module._MAX_ORPHAN_CONTROLLERS):
                backup._retain_controller(cast(WalSidecarController, object()))
            controller_owner = mock.Mock()
            with self.assertRaises(BackupDurabilityUnknownError) as raised:
                backup._retain_controller(cast(WalSidecarController, controller_owner))
            raised.exception.retry_cleanup()
            controller_owner.close.assert_called_once_with()
            backup._orphan_controllers.clear()

            fds: list[int] = []
            try:
                for _ in range(backup_module._MAX_ORPHAN_FDS):
                    fd = os.open(root / "snapshot", os.O_RDONLY)
                    fds.append(fd)
                    backup_module._remember_orphan_fd(
                        backup._orphan_fds,
                        fd,
                        None,
                        "backup registry",
                    )
                extra_fd = os.open(root / "snapshot", os.O_RDONLY)
                try:
                    with self.assertRaises(BackupDurabilityUnknownError) as raised:
                        backup_module._remember_orphan_fd(
                            backup._orphan_fds,
                            extra_fd,
                            None,
                            "backup registry",
                            cleanup_callback=lambda: os.close(extra_fd),
                        )
                    self.assertEqual(
                        backup_module._MAX_ORPHAN_FDS, len(backup._orphan_fds)
                    )
                    raised.exception.retry_cleanup()
                finally:
                    try:
                        os.close(extra_fd)
                    except OSError as error:
                        if error.errno != errno.EBADF:
                            raise
            finally:
                for fd in fds:
                    try:
                        os.close(fd)
                    except OSError as error:
                        if error.errno != errno.EBADF:
                            raise
                backup._orphan_fds.clear()

    def test_failed_quiescence_acquisition_retains_controller_for_next_operation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-backup-controller-"
        ) as temporary:
            root = _make_root(temporary)
            SQLiteBackup(root, busy_timeout_ms=100).create("existing")
            backup = MarkerLockFaultBackup(root)
            original_flock = fcntl.flock
            original_close = os.close
            marker_fd: int | None = None
            exclusive_locks = 0

            def flock(fd: int, operation: int) -> None:
                nonlocal exclusive_locks, marker_fd
                if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                    exclusive_locks += 1
                    if exclusive_locks == 2:
                        marker_fd = fd
                if operation == fcntl.LOCK_UN and fd == marker_fd:
                    raise OSError("persistent marker unlock failure")
                original_flock(fd, operation)

            def close(fd: int) -> None:
                if fd == marker_fd:
                    raise OSError("persistent marker close failure")
                original_close(fd)

            with (
                mock.patch.object(fcntl, "flock", side_effect=flock),
                mock.patch.object(os, "close", side_effect=close),
                self.assertRaises(BackupFilesystemError) as raised,
            ):
                backup.create("second")

            self.assertEqual(1, len(backup._orphan_controllers))
            retained = backup._orphan_controllers[0]
            raised.exception.retry_cleanup()
            self.assertEqual([], backup._orphan_controllers)
            result = backup.inspect("existing")
            self.assertEqual("existing", result.database_basename)
            self.assertEqual([], backup._orphan_controllers)
            self.assertEqual([], retained._pending_fds)

    def test_quiescence_close_failure_is_retained_and_retried_before_next_create(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-backup-session-"
        ) as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.create("existing")
            original_close = QuiescenceSession.close
            closed_sessions: list[QuiescenceSession] = []
            failed = False

            def fail_once(session: QuiescenceSession) -> None:
                nonlocal failed
                closed_sessions.append(session)
                if not failed:
                    failed = True
                    raise OSError("injected backup session close failure")
                original_close(session)

            with (
                mock.patch.object(QuiescenceSession, "close", new=fail_once),
                self.assertRaises(BackupFilesystemError),
            ):
                backup.create("second")

            self.assertEqual(1, len(backup._orphan_sessions))
            retained = backup._orphan_sessions[0]
            result = backup.inspect("existing")
            self.assertEqual(result, backup.inspect("existing"))
            self.assertEqual([], backup._orphan_sessions)
            self.assertIs(retained, closed_sessions[0])

    def test_persistent_quiescence_close_failure_is_bounded_and_explicitly_retryable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-backup-session-"
        ) as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.create("existing")
            close_error = OSError("persistent backup session close failure")

            with (
                mock.patch.object(
                    QuiescenceSession,
                    "close",
                    side_effect=close_error,
                ),
                self.assertRaises(BackupFilesystemError) as raised,
            ):
                backup.create("second")

            self.assertEqual(1, len(backup._orphan_sessions))
            with (
                mock.patch.object(
                    QuiescenceSession,
                    "close",
                    side_effect=close_error,
                ),
                self.assertRaises(OSError),
            ):
                backup.inspect("existing")
            self.assertEqual(1, len(backup._orphan_sessions))

            raised.exception.retry_cleanup()
            self.assertEqual([], backup._orphan_sessions)

    def test_quiescence_close_failure_does_not_replace_backup_body_error(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-backup-session-"
        ) as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            body_error = BackupIntegrityError("injected backup body failure")
            close_error = OSError("injected backup cleanup failure")

            with (
                mock.patch.object(
                    QuiescenceSession,
                    "copy_database_to",
                    side_effect=body_error,
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "close",
                    side_effect=close_error,
                ),
                self.assertRaisesRegex(
                    BackupIntegrityError, "injected backup body failure"
                ) as raised,
            ):
                backup.create("snapshot")

            self.assertEqual(1, len(backup._orphan_sessions))
            raised.exception.retry_cleanup()
            self.assertEqual([], backup._orphan_sessions)

    def test_attrless_backup_body_gets_typed_cleanup_wrapper(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-backup-session-"
        ) as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            body_error = AttrlessBody("injected attrless backup body failure")
            original_attach = store_module._attach_cleanup_capability

            def attach(
                error: BaseException,
                capability: store_module._CleanupCapability,
            ) -> None:
                if error is body_error:
                    raise TypeError("body has no writable attributes")
                original_attach(error, capability)

            with (
                mock.patch.object(
                    store_module,
                    "_attach_cleanup_capability",
                    side_effect=attach,
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "copy_database_to",
                    side_effect=body_error,
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "close",
                    side_effect=OSError("injected attrless backup cleanup failure"),
                ),
                self.assertRaises(BackupDurabilityUnknownError) as raised,
            ):
                backup.create("snapshot")

            self.assertIs(body_error, raised.exception.__cause__)
            raised.exception.retry_cleanup()

    def test_quiescence_unlock_failure_is_retained_until_next_operation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-backup-session-"
        ) as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.create("snapshot")
            original_flock = fcntl.flock
            failed = False

            def fail_unlock(fd: int, operation: int) -> None:
                nonlocal failed
                if operation == fcntl.LOCK_UN and not failed:
                    failed = True
                    raise OSError("injected backup unlock failure")
                original_flock(fd, operation)

            with (
                mock.patch.object(fcntl, "flock", side_effect=fail_unlock),
                self.assertRaises(wal_module.WalSidecarRecoveryRequiredError),
            ):
                backup.create("second")
            self.assertEqual(1, len(backup._orphan_sessions))

            result = backup.inspect("snapshot")
            self.assertEqual(result, backup.inspect("snapshot"))
            self.assertEqual([], backup._orphan_sessions)

    def test_create_uses_one_quiescent_truncate_copy_and_store_image_helper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = RecordingBackup(root)
            result = backup.create("snapshot")
            self.assertIsInstance(result, BackupArtifact)
            assert backup.controller is not None
            self.assertEqual(1, backup.controller.hold_count)
            self.assertEqual(1, backup.controller.copy_count)
            self.assertEqual(0, result.manifest.captured_recovery_epoch)
            self.assertEqual(0, result.manifest.captured_fencing_token_floor)

    def test_publish_order_is_db_manifest_directory_fsync_then_final_inspect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.create("snapshot")
            expected = (
                "before_manifest_fsync",
                "after_manifest_fsync",
                "before_publish_recheck",
                "before_db_replace",
                "after_db_replace",
                "before_manifest_replace",
                "after_manifest_replace",
                "before_directory_fsync",
                "after_directory_fsync",
                "before_final_inspect",
            )
            self.assertEqual(
                expected,
                tuple(backup.fault_events[-len(expected) - 1 : -1]),
            )

    def test_fsync_failure_is_durability_unknown_without_success_result(self) -> None:
        class FailingBackup(DeterministicBackup):
            def _fsync(self, fd: int, label: str) -> None:
                del fd
                if label == "manifest temp":
                    raise BackupDurabilityUnknownError("injected fsync failure")

        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = FailingBackup(root)
            with self.assertRaises(BackupDurabilityUnknownError):
                backup.create("snapshot")
            self.assertFalse((root / "snapshot").exists())

    def test_directory_fsync_failure_never_returns_success(self) -> None:
        class FailingDirectoryFsyncBackup(DeterministicBackup):
            def _fsync(self, fd: int, label: str) -> None:
                if label == "backup root directory":
                    raise OSError("injected directory fsync failure")
                super()._fsync(fd, label)

        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = FailingDirectoryFsyncBackup(root)
            with self.assertRaises(BackupDurabilityUnknownError):
                backup.create("snapshot")
            self.assertTrue((root / "snapshot").exists())
            self.assertTrue((root / "snapshot.manifest").exists())

    def test_root_close_failure_is_durability_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = CaptureDescriptorsBackup(root)
            original_close = os.close
            failed = False

            def close(fd: int) -> None:
                nonlocal failed
                if fd == backup.root_fd and not failed:
                    failed = True
                    raise OSError("injected root close failure")
                original_close(fd)

            with (
                mock.patch.object(os, "close", side_effect=close),
                self.assertRaises(BackupDurabilityUnknownError),
            ):
                backup.create("snapshot")
            self.assertTrue(failed)

    def test_held_database_close_failure_is_durability_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = CaptureDescriptorsBackup(root)
            backup.create("snapshot")
            backup.existing_database_fd = None
            original_close = os.close
            failed = False

            def close(fd: int) -> None:
                nonlocal failed
                if fd == backup.existing_database_fd and not failed:
                    failed = True
                    raise OSError("injected database close failure")
                original_close(fd)

            with (
                mock.patch.object(os, "close", side_effect=close),
                self.assertRaises(BackupDurabilityUnknownError),
            ):
                backup.inspect("snapshot")
            self.assertTrue(failed)

    def test_persistent_create_fd_close_is_retained_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = CaptureDescriptorsBackup(root)
            original_close = os.close

            def close(fd: int) -> None:
                if fd == backup.root_fd:
                    raise OSError("persistent create fd close failure")
                original_close(fd)

            with (
                mock.patch.object(os, "close", side_effect=close),
                self.assertRaises(BackupDurabilityUnknownError),
            ):
                backup.create("snapshot")
            with (
                mock.patch.object(os, "close", side_effect=close),
                self.assertRaises(BackupDurabilityUnknownError),
            ):
                backup.create("snapshot")

            self.assertEqual(1, len(backup._orphan_fds))
            self.assertEqual(backup.root_fd, backup._orphan_fds[0][0])
            backup.close()
            self.assertEqual([], backup._orphan_fds)

    def test_persistent_inspect_fd_close_is_retained_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = CaptureDescriptorsBackup(root)
            backup.create("snapshot")
            backup.existing_database_fd = None
            backup.held_fds.pop("backup database", None)
            original_close = os.close

            def close(fd: int) -> None:
                if fd == backup.held_fds.get("backup database"):
                    raise OSError("persistent inspect fd close failure")
                original_close(fd)

            with (
                mock.patch.object(os, "close", side_effect=close),
                self.assertRaises(BackupDurabilityUnknownError),
            ):
                backup.inspect("snapshot")
            with (
                mock.patch.object(os, "close", side_effect=close),
                self.assertRaises(BackupDurabilityUnknownError),
            ):
                backup.inspect("snapshot")
            target_fd = backup.held_fds["backup database"]

            self.assertEqual(1, len(backup._orphan_fds))
            self.assertEqual(target_fd, backup._orphan_fds[0][0])
            backup.close()
            self.assertEqual([], backup._orphan_fds)

    def test_actual_close_then_error_does_not_close_reused_backup_fd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = CaptureDescriptorsBackup(root)
            backup.create("snapshot")
            original_close_temporary_fd = doctor_module._close_temporary_fd
            closed = False

            def close_temporary_fd(
                fd: int,
                expected_identity: tuple[int, int] | None,
                label: str,
            ) -> None:
                nonlocal closed
                if fd == backup.root_fd and not closed:
                    closed = True
                    original_close_temporary_fd(fd, expected_identity, label)
                    raise doctor_module.StateFilesystemError(
                        "close returned after actual close"
                    )
                original_close_temporary_fd(fd, expected_identity, label)

            with (
                mock.patch.object(
                    doctor_module,
                    "_close_temporary_fd",
                    side_effect=close_temporary_fd,
                ),
                self.assertRaises(BackupDurabilityUnknownError),
            ):
                backup.inspect("snapshot")
            target_fd = backup.root_fd

            self.assertEqual([], backup._orphan_fds)
            replacement_path = root / "replacement"
            replacement_path.write_bytes(b"replacement")
            filler_fds: list[int] = []
            replacement_fd: int | None = None
            while replacement_fd is None:
                opened_fd = os.open(replacement_path, os.O_RDONLY)
                if opened_fd == target_fd:
                    replacement_fd = opened_fd
                else:
                    filler_fds.append(opened_fd)
            try:
                assert replacement_fd is not None
                self.assertEqual(target_fd, replacement_fd)
                backup.close()
                os.fstat(replacement_fd)
            finally:
                if replacement_fd is not None:
                    os.close(replacement_fd)
                for filler_fd in filler_fds:
                    os.close(filler_fd)

    def test_unknown_fd_identity_never_closes_a_reused_backup_fd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = CaptureDescriptorsBackup(root)
            backup.create("snapshot")
            original_close = os.close
            original_fstat = os.fstat
            target_fd = os.open(root / "snapshot", os.O_RDONLY)

            def fstat(fd: int) -> os.stat_result:
                if fd == target_fd:
                    raise OSError("fd status is unavailable")
                return original_fstat(fd)

            with mock.patch.object(os, "fstat", side_effect=fstat):
                backup_module._remember_orphan_fd(
                    backup._orphan_fds,
                    target_fd,
                    None,
                    "backup unknown",
                )
            self.assertEqual((target_fd, None), backup._orphan_fds[0][:2])
            original_close(target_fd)
            replacement_path = root / "replacement"
            replacement_path.write_bytes(b"replacement")
            filler_fds: list[int] = []
            replacement_fd: int | None = None
            while replacement_fd is None:
                opened_fd = os.open(replacement_path, os.O_RDONLY)
                if opened_fd == target_fd:
                    replacement_fd = opened_fd
                else:
                    filler_fds.append(opened_fd)
            try:
                with self.assertRaises(BackupDurabilityUnknownError):
                    backup.close()
                self.assertEqual(1, len(backup._orphan_fds))
                with self.assertRaises(BackupDurabilityUnknownError):
                    backup.inspect("snapshot")
                self.assertEqual(1, len(backup._orphan_fds))
                assert replacement_fd is not None
                os.fstat(replacement_fd)
            finally:
                if replacement_fd is not None:
                    try:
                        original_close(replacement_fd)
                    except OSError as close_error:
                        if close_error.errno != errno.EBADF:
                            raise
                for filler_fd in filler_fds:
                    original_close(filler_fd)

    def test_attrless_unknown_fd_identity_never_closes_a_reused_backup_fd(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = CaptureDescriptorsBackup(root)
            backup.create("snapshot")
            original_close = os.close
            original_fstat = os.fstat
            target_fd = os.open(root / "snapshot", os.O_RDONLY)
            status_error = AttrlessBody("fd status is unavailable")

            def fstat(fd: int) -> os.stat_result:
                if fd == target_fd:
                    raise status_error
                return original_fstat(fd)

            with mock.patch.object(os, "fstat", side_effect=fstat):
                backup_module._remember_orphan_fd(
                    backup._orphan_fds,
                    target_fd,
                    None,
                    "backup unknown",
                )
            self.assertEqual((target_fd, None), backup._orphan_fds[0][:2])
            original_close(target_fd)
            replacement_path = root / "replacement"
            replacement_path.write_bytes(b"replacement")
            filler_fds: list[int] = []
            replacement_fd: int | None = None
            while replacement_fd is None:
                opened_fd = os.open(replacement_path, os.O_RDONLY)
                if opened_fd == target_fd:
                    replacement_fd = opened_fd
                else:
                    filler_fds.append(opened_fd)
            try:
                with self.assertRaises(BackupDurabilityUnknownError):
                    backup.close()
                self.assertEqual(1, len(backup._orphan_fds))
                assert replacement_fd is not None
                os.fstat(replacement_fd)
            finally:
                if replacement_fd is not None:
                    original_close(replacement_fd)
                for filler_fd in filler_fds:
                    original_close(filler_fd)
                backup._orphan_fds.clear()

    def test_backup_body_error_stays_primary_when_fd_cleanup_is_uncertain(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = CaptureDescriptorsBackup(root)
            body_error = BackupIntegrityError(
                "injected backup body error with fd cleanup"
            )
            original_close = os.close

            def close(fd: int) -> None:
                if fd == backup.held_fds.get("database temp"):
                    raise OSError("persistent body fd close failure")
                original_close(fd)

            with (
                mock.patch.object(backup, "_store_image", side_effect=body_error),
                mock.patch.object(os, "close", side_effect=close),
                self.assertRaisesRegex(
                    BackupIntegrityError,
                    "injected backup body error with fd cleanup",
                ),
            ):
                backup.create("snapshot")

            self.assertEqual(1, len(backup._orphan_fds))
            backup.close()

    def test_db_rename_response_loss_is_durability_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            original_replace = os.replace

            def replace_after_action(
                source: str,
                destination: str,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                original_replace(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                if source == f".{backup.nonce}.db.tmp" and destination == "snapshot":
                    raise OSError("rename response was lost")

            with (
                mock.patch.object(
                    os,
                    "replace",
                    side_effect=replace_after_action,
                ),
                self.assertRaises(BackupDurabilityUnknownError),
            ):
                backup.create("snapshot")
            self.assertTrue((root / "snapshot").exists())
            self.assertFalse((root / "snapshot.manifest").exists())

    def test_mixed_pair_is_never_accepted_or_falls_back(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.create("snapshot")
            database = root / "snapshot"
            manifest = root / "snapshot.manifest"
            original_database = database.read_bytes()
            original_manifest = manifest.read_bytes()
            database.write_bytes(original_database + b"tamper")
            database.chmod(0o600)
            with self.assertRaises((BackupIntegrityError, BackupIncompleteError)):
                backup.inspect("snapshot")
            database.write_bytes(original_database)
            database.chmod(0o600)
            parsed = _decode_manifest(original_manifest)
            manifest.write_bytes(_encode_manifest(replace(parsed, database_size=999)))
            manifest.chmod(0o600)
            with self.assertRaises(BackupIntegrityError):
                backup.inspect("snapshot")
            self.assertEqual(original_database, database.read_bytes())

    def test_missing_store_image_helper_fails_closed_without_raw_sql_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.create("snapshot")
            with (
                mock.patch.object(
                    store_module.CoordinationStore,
                    "_inspect_image_fd",
                    None,
                    create=True,
                ),
                self.assertRaises(BackupIntegrationError),
            ):
                backup.inspect("snapshot")


class BackupManifestCrossCheckTest(unittest.TestCase):
    def test_real_store_image_helper_validates_a_published_pair(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            result = backup.create("snapshot")
            self.assertEqual(result, backup.inspect("snapshot"))

    def test_captured_floor_is_snapshot_value_not_locally_advanced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            with (
                mock.patch.object(
                    CoordinationStore,
                    "_reserve_floor",
                    side_effect=AssertionError("backup must not advance floor"),
                ),
            ):
                result = backup.create("snapshot")
            self.assertEqual(
                RecoveryFloor(0, 0),
                RecoveryFloor(
                    result.manifest.captured_recovery_epoch,
                    result.manifest.captured_fencing_token_floor,
                ),
            )

    def test_store_rejects_mixed_sqlite_header_through_store_helper(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.create("snapshot")
            database = root / "snapshot"
            fd = os.open(database, os.O_RDWR)
            try:
                os.pwrite(fd, b"\x02", 18)
                os.pwrite(fd, b"\x01", 19)
            finally:
                os.close(fd)
            _rewrite_manifest_digest(root, "snapshot")
            with self.assertRaises(BackupIntegrityError):
                backup.inspect("snapshot")

    def test_store_schema_error_is_mapped_to_backup_integrity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            backup.create("snapshot")
            database = root / "snapshot"
            fd = os.open(database, os.O_RDWR)
            try:
                os.pwrite(fd, b"\x00\x00\x00\x03", 60)
            finally:
                os.close(fd)
            _rewrite_manifest_digest(root, "snapshot")
            with self.assertRaises(BackupIntegrityError):
                backup.inspect("snapshot")

    def test_backup_does_not_invoke_store_constructor_floor_provider_or_ddl(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-backup-") as temporary:
            root = _make_root(temporary)
            backup = DeterministicBackup(root)
            source = Path(backup_module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("RecoveryCoordinator", source)
            self.assertNotIn("_issue_provider_effect", source)
            self.assertNotIn("CREATE TABLE", source)
            with (
                mock.patch.object(
                    CoordinationStore,
                    "__init__",
                    side_effect=AssertionError("backup must not construct Store"),
                ),
                mock.patch.object(
                    CoordinationStore,
                    "_reserve_floor",
                    side_effect=AssertionError("backup must not reserve floor"),
                ),
            ):
                result = backup.create("snapshot")
            self.assertIsInstance(result, BackupArtifact)


class BackupCrashTest(unittest.TestCase):
    def test_sigkill_source_copy_fault_matrix_never_promotes_partial_pairs(
        self,
    ) -> None:
        fault_points = (
            "before_source_copy",
            "after_source_copy",
            "before_copy_target_write",
            "after_copy_target_write",
            "before_source_copy_fsync",
            "after_source_copy_fsync",
        )
        for index, point in enumerate(fault_points, start=1):
            with (
                self.subTest(point=point),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-backup-source-kill-"
                ) as temporary,
            ):
                root = _make_root(temporary)
                source_before = (root / "coordination.sqlite3").read_bytes()
                nonce = f"{index + 100:032x}"
                child = os.fork()
                if child == 0:
                    backup = SourceFaultBackup(root)
                    backup.nonce = nonce
                    backup.source_fault_point = point
                    backup.create("snapshot")
                    os._exit(3)
                _, wait_status = os.waitpid(child, 0)
                self.assertEqual(
                    -signal.SIGKILL, os.waitstatus_to_exitcode(wait_status)
                )
                self.assertEqual(
                    source_before, (root / "coordination.sqlite3").read_bytes()
                )
                self.assertFalse((root / "snapshot").exists())
                self.assertFalse((root / "snapshot.manifest").exists())

    def test_sigkill_fault_matrix_never_promotes_partial_pairs(self) -> None:
        fault_points = (
            "before_manifest_temp_create",
            "after_manifest_temp_create",
            "before_manifest_write",
            "after_manifest_write",
            "before_manifest_fsync",
            "after_manifest_fsync",
            "before_publish_recheck",
            "before_db_replace",
            "after_db_replace",
            "before_manifest_replace",
            "after_manifest_replace",
            "before_directory_fsync",
            "after_directory_fsync",
            "before_final_inspect",
        )
        complete_points = {
            "after_manifest_replace",
            "before_directory_fsync",
            "after_directory_fsync",
            "before_final_inspect",
        }
        for index, point in enumerate(fault_points, start=1):
            with (
                self.subTest(point=point),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-backup-kill-"
                ) as temporary,
            ):
                root = _make_root(temporary)
                source_before = (root / "coordination.sqlite3").read_bytes()
                nonce = f"{index:032x}"
                child = os.fork()
                if child == 0:
                    backup = KillAtBackup(root)
                    backup.nonce = nonce
                    backup.fault_point = point
                    backup.create("snapshot")
                    os._exit(3)
                _, wait_status = os.waitpid(child, 0)
                self.assertEqual(
                    -signal.SIGKILL, os.waitstatus_to_exitcode(wait_status)
                )
                self.assertEqual(
                    source_before, (root / "coordination.sqlite3").read_bytes()
                )
                inspected_backup = DeterministicBackup(root)
                if point in complete_points:
                    self.assertIsInstance(
                        inspected_backup.inspect("snapshot"),
                        BackupArtifact,
                    )
                else:
                    with self.assertRaises(BackupIncompleteError):
                        inspected_backup.inspect("snapshot")


if __name__ == "__main__":
    unittest.main()
