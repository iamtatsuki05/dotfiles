from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import multiprocessing
import os
import signal
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from agent_team import doctor as doctor_module
from agent_team import lease as lease_module
from agent_team import recovery as recovery_module
from agent_team import restore as restore_module
from agent_team import store as store_module
from agent_team import wal as wal_module
from agent_team.backup import (
    BackupArtifact,
    BackupIncompleteError,
    BackupManifest,
    SQLiteBackup,
    _encode_manifest,
)
from agent_team.lease import (
    LeaseConflictError,
    ProviderCapabilities,
    ProviderFenceProof,
    ProviderStatus,
    RecoveryFloor,
    RestoreApplyResult,
    RestoreIdentity,
    StoreImageObservation,
)
from agent_team.recovery import RestoreLedger
from agent_team.restore import (
    BackupRestore,
    RestoreError,
    RestoreFilesystemError,
    RestorePendingError,
    RestoreResult,
    RestoreReviewRequiredError,
    _candidate_basename,
    _evidence_ref,
)
from agent_team.store import (
    CoordinationStore,
    DuplicateOperationError,
    RestoreStoreAuthority,
    StoreIntegrityError,
    StoreUnavailableError,
)
from agent_team.wal import (
    CheckpointRequest,
    QuiescenceSession,
    SidecarCleanupResult,
    WalSidecarController,
    WalSidecarUnsafeError,
)


class FakeClock:
    def __init__(self, now_ns: int = 200) -> None:
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns


class FaultAt:
    def __init__(self, target: str) -> None:
        self.target = target
        self.events: list[str] = []

    def __call__(self, point: str) -> None:
        self.events.append(point)
        if point == self.target:
            raise RuntimeError(f"injected restore fault: {point}")


class AttrlessBody(BaseException):
    pass


class MarkerLockFaultController(WalSidecarController):
    def _fault(self, point: str) -> None:
        if point == "after_marker_lock":
            raise RestoreError("injected marker-lock acquisition fault")


class MarkerLockFaultRestore(BackupRestore):
    def __init__(self, state_root: Path) -> None:
        super().__init__(state_root, busy_timeout_ms=100, clock=FakeClock(200))
        self.fail_acquisition = True

    def _controller(self) -> WalSidecarController:
        if self.fail_acquisition:
            self.fail_acquisition = False
            return MarkerLockFaultController(self._state_root, busy_timeout_ms=100)
        return super()._controller()


def _make_root(temporary: str) -> Path:
    root = Path(os.path.realpath(temporary)) / "state"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    with CoordinationStore(root, busy_timeout_ms=100):
        pass
    return root


def _artifact_and_newer_destination(temporary: str) -> tuple[Path, BackupArtifact]:
    root = _make_root(temporary)
    with CoordinationStore(root, busy_timeout_ms=100, clock=FakeClock(50)) as store:
        store.create_intent(
            "source-operation",
            effect_key="effect/source-operation",
            provider_id="provider/test",
            actor="main",
            clock_ns=50,
        )
    artifact = SQLiteBackup(root, busy_timeout_ms=100).create("snapshot")
    with CoordinationStore(root, busy_timeout_ms=100, clock=FakeClock(100)) as store:
        store.create_intent(
            "destination-only",
            effect_key="effect/destination-only",
            provider_id="provider/test",
            actor="main",
            clock_ns=100,
        )
    return root, artifact


def _swap_with_byte_identical_inode(path: Path) -> None:
    replacement = path.with_name(f".{path.name}.replacement")
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o600)
    os.replace(replacement, path)


def _rewrite_tombstone_field(path: Path, field: str, value: int) -> None:
    lines = path.read_bytes().splitlines(keepends=True)
    item = json.loads(lines[-1])
    item[field] = value
    lines[-1] = (
        json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    path.write_bytes(b"".join(lines))


def _rewrite_tombstone_identities(
    path: Path,
    identities: tuple[tuple[str, str], ...],
    *,
    generation: int | None = None,
) -> None:
    lines = path.read_bytes().splitlines(keepends=True)
    for index, line in enumerate(lines):
        item = json.loads(line)
        if generation is not None and item["restore_generation"] != generation:
            continue
        item["identities"] = [
            {"operation_id": operation_id, "effect_key": effect_key}
            for operation_id, effect_key in identities
        ]
        lines[index] = (
            json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
    path.write_bytes(b"".join(lines))


def _open_fifo_with_owned_fd(root: str) -> None:
    root_path = Path(root)
    root_fd = os.open(
        root_path,
        store_module._open_flags(directory=True, writable=False),
    )
    try:
        with restore_module._owned_fd(root_fd, "restore-fifo", writable=False):
            pass
    except restore_module.RestoreError:
        return
    finally:
        os.close(root_fd)


def _latest_ledger_phase(root: Path) -> str | None:
    record = RestoreLedger(root, busy_timeout_ms=100).ledger.read()
    return None if record is None else record.phase


def _read_only_sqlite_snapshot(path: Path) -> dict[str, Any]:
    """Read the durable projection through a SQLite read-only connection."""

    # ``immutable`` prevents SQLite from creating WAL/SHM sidecars while this
    # read-only acceptance snapshot is open.
    uri = f"file:{path}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return {
            "file_metadata": _sqlite_file_metadata(path),
            "raw_header": path.read_bytes()[:100],
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "store_meta": tuple(
                connection.execute(
                    "SELECT key, value FROM store_meta ORDER BY key"
                ).fetchall()
            ),
            "operations": tuple(
                connection.execute(
                    "SELECT * FROM operations ORDER BY operation_id"
                ).fetchall()
            ),
            "operation_attempts": tuple(
                connection.execute(
                    "SELECT * FROM operation_attempts ORDER BY operation_id, attempt"
                ).fetchall()
            ),
            "effect_receipts": tuple(
                connection.execute(
                    "SELECT * FROM effect_receipts ORDER BY operation_id, attempt"
                ).fetchall()
            ),
            "transition_events": tuple(
                connection.execute(
                    "SELECT * FROM transition_events ORDER BY event_id"
                ).fetchall()
            ),
        }
    finally:
        connection.close()


def _sqlite_file_metadata(path: Path) -> tuple[int, int, int, int]:
    metadata = path.stat(follow_symlinks=False)
    return (
        metadata.st_size,
        metadata.st_mode & 0o777,
        metadata.st_nlink,
        metadata.st_ino,
    )


def _read_restore_pair(
    root: Path,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    records: list[tuple[dict[str, Any], ...]] = []
    for basename in (
        recovery_module.RECOVERY_LEDGER_BASENAME,
        recovery_module.RECOVERY_TOMBSTONES_BASENAME,
    ):
        path = root / basename
        lines = path.read_bytes().splitlines()
        parsed: list[dict[str, Any]] = []
        for line in lines:
            item = json.loads(line)
            self_encoded = json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if line != self_encoded:
                raise AssertionError(f"{basename} contains non-canonical JSON")
            parsed.append(item)
        records.append(tuple(parsed))
    return records[0], records[1]


def _read_store_observation(path: Path) -> StoreImageObservation:
    fd = os.open(path, os.O_RDONLY)
    try:
        return RestoreStoreAuthority().inspect_image(fd)
    finally:
        os.close(fd)


def _restore_identity_values(
    identities: tuple[RestoreIdentity, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (identity.operation_id, identity.effect_key) for identity in identities
    )


def _restore_binding_from_records(
    *,
    ledger: dict[str, Any],
    tombstone: dict[str, Any],
    final_floor: RecoveryFloor,
    active_tombstones: tuple[RestoreIdentity, ...],
) -> str:
    current_tombstones = tuple(
        (
            str(identity["operation_id"]),
            str(identity["effect_key"]),
        )
        for identity in tombstone["identities"]
    )
    active_values = _restore_identity_values(active_tombstones)
    preimage = {
        "domain": "restore-history-binding",
        "version": 1,
        "restore_generation": ledger["restore_generation"],
        "actor": ledger["actor"],
        "audit_evidence_ref": _evidence_ref(ledger["audit_ref"]),
        "source_digest": ledger["backup_digest"],
        "previous_primary_digest": tombstone["previous_primary_digest"],
        "previous_recovery_epoch": tombstone["previous_recovery_epoch"],
        "previous_fencing_token_hwm": tombstone["previous_fencing_token_hwm"],
        "previous_last_clock_ns": tombstone["previous_last_clock_ns"],
        "final_recovery_epoch": final_floor.recovery_epoch,
        "final_fencing_token_floor": final_floor.fencing_token_floor,
        "current_tombstones": current_tombstones,
        "active_tombstones": active_values,
    }
    encoded = json.dumps(
        preimage,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _assert_pair_record_fields(
    testcase: unittest.TestCase,
    *,
    root: Path,
    artifact: BackupArtifact,
    phase_pair: tuple[str, str],
    actor: str,
    audit_ref: str,
    final_floor: RecoveryFloor,
    active_tombstones: tuple[RestoreIdentity, ...],
) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger_records, tombstone_records = _read_restore_pair(root)
    testcase.assertTrue(ledger_records)
    testcase.assertTrue(tombstone_records)
    ledger = ledger_records[-1]
    tombstone = tombstone_records[-1]
    testcase.assertEqual(
        {
            "version",
            "sequence",
            "phase",
            "restore_generation",
            "recovery_epoch",
            "fencing_token_floor",
            "backup_digest",
            "actor",
            "audit_ref",
        },
        set(ledger),
    )
    testcase.assertEqual(
        {
            "version",
            "sequence",
            "phase",
            "restore_generation",
            "backup_digest",
            "previous_primary_digest",
            "candidate_digest",
            "previous_recovery_epoch",
            "previous_fencing_token_hwm",
            "previous_last_clock_ns",
            "identities",
            "actor",
            "audit_ref",
        },
        set(tombstone),
    )
    testcase.assertEqual(1, ledger["version"])
    testcase.assertEqual(1, tombstone["version"])
    testcase.assertEqual(phase_pair[0], ledger["phase"])
    testcase.assertEqual(phase_pair[1], tombstone["phase"])
    testcase.assertEqual(ledger["restore_generation"], tombstone["restore_generation"])
    testcase.assertEqual(ledger["recovery_epoch"], final_floor.recovery_epoch)
    testcase.assertEqual(ledger["fencing_token_floor"], final_floor.fencing_token_floor)
    testcase.assertEqual(artifact.manifest.database_digest, ledger["backup_digest"])
    testcase.assertEqual(ledger["backup_digest"], tombstone["backup_digest"])
    testcase.assertEqual(actor, ledger["actor"])
    testcase.assertEqual(actor, tombstone["actor"])
    testcase.assertEqual(audit_ref, ledger["audit_ref"])
    testcase.assertEqual(audit_ref, tombstone["audit_ref"])
    testcase.assertEqual(
        _restore_identity_values(active_tombstones),
        tuple(
            (item["operation_id"], item["effect_key"])
            for item in tombstone["identities"]
        ),
    )
    for field in (
        "previous_recovery_epoch",
        "previous_fencing_token_hwm",
        "previous_last_clock_ns",
    ):
        testcase.assertIsInstance(tombstone[field], int)
        testcase.assertGreaterEqual(tombstone[field], 0)
    return ledger, tombstone


def _assert_committed_restore_evidence(
    testcase: unittest.TestCase,
    *,
    root: Path,
    artifact: BackupArtifact,
    result: RestoreResult,
    actor: str,
    audit_ref: str,
    expected_restore_events: int,
) -> None:
    primary_path = root / store_module.DATABASE_FILENAME
    primary = _read_store_observation(primary_path)
    ledger, tombstone = _assert_pair_record_fields(
        testcase,
        root=root,
        artifact=artifact,
        phase_pair=("RESTORE_COMMITTED", "COMMITTED"),
        actor=actor,
        audit_ref=audit_ref,
        final_floor=primary.floor,
        active_tombstones=result.active_tombstones,
    )
    testcase.assertEqual("RESTORE_COMMITTED", result.phase)
    testcase.assertEqual(artifact.manifest.database_digest, result.backup_digest)
    testcase.assertEqual(primary.digest, result.candidate_digest)
    testcase.assertEqual(primary.floor, result.floor)
    testcase.assertEqual(
        _restore_identity_values(result.identities),
        tuple(
            (item["operation_id"], item["effect_key"])
            for item in tombstone["identities"]
        ),
    )
    testcase.assertEqual(result.restore_generation, ledger["restore_generation"])
    testcase.assertEqual(result.floor.recovery_epoch, ledger["recovery_epoch"])
    testcase.assertEqual(
        result.floor.fencing_token_floor,
        ledger["fencing_token_floor"],
    )
    testcase.assertEqual(result.candidate_digest, tombstone["candidate_digest"])
    testcase.assertEqual(
        result.active_tombstones,
        tuple(
            RestoreIdentity(
                operation_id=item["operation_id"],
                effect_key=item["effect_key"],
            )
            for item in tombstone["identities"]
        ),
    )
    events = _read_only_sqlite_snapshot(primary_path)["transition_events"]
    restore_events = tuple(event for event in events if event[6] == "restore")
    testcase.assertEqual(expected_restore_events, len(restore_events))
    expected_binding = _restore_binding_from_records(
        ledger=ledger,
        tombstone=tombstone,
        final_floor=result.floor,
        active_tombstones=result.active_tombstones,
    )
    for event in restore_events:
        testcase.assertEqual(actor, event[7])
        testcase.assertEqual("restore", event[9])
        testcase.assertEqual(expected_binding, event[10])


def _assert_pending_restore_evidence(
    testcase: unittest.TestCase,
    *,
    root: Path,
    artifact: BackupArtifact,
    phase_pair: tuple[str, str],
    actor: str,
    audit_ref: str,
    candidate_present: bool,
    primary_is_new: bool,
) -> None:
    candidate_path = root / _candidate_basename(artifact)
    primary_path = root / store_module.DATABASE_FILENAME
    primary = _read_store_observation(primary_path)
    candidate = _read_store_observation(candidate_path) if candidate_present else None
    if candidate_present:
        testcase.assertTrue(candidate_path.exists())
    else:
        testcase.assertFalse(candidate_path.exists())
    final_floor = (
        candidate.floor
        if candidate is not None
        else RecoveryFloor(
            int(
                json.loads(
                    (root / recovery_module.RECOVERY_LEDGER_BASENAME)
                    .read_bytes()
                    .splitlines()[-1]
                )["recovery_epoch"]
            ),
            int(
                json.loads(
                    (root / recovery_module.RECOVERY_LEDGER_BASENAME)
                    .read_bytes()
                    .splitlines()[-1]
                )["fencing_token_floor"]
            ),
        )
    )
    active = tuple(
        RestoreIdentity(
            operation_id=item["operation_id"],
            effect_key=item["effect_key"],
        )
        for item in _read_restore_pair(root)[1][-1]["identities"]
    )
    ledger, tombstone = _assert_pair_record_fields(
        testcase,
        root=root,
        artifact=artifact,
        phase_pair=phase_pair,
        actor=actor,
        audit_ref=audit_ref,
        final_floor=final_floor,
        active_tombstones=active,
    )
    expected_primary_digest = (
        tombstone["candidate_digest"]
        if primary_is_new
        else tombstone["previous_primary_digest"]
    )
    testcase.assertEqual(expected_primary_digest, primary.digest)
    if candidate is not None:
        testcase.assertEqual(tombstone["candidate_digest"], candidate.digest)
        testcase.assertEqual(ledger["recovery_epoch"], candidate.floor.recovery_epoch)
        testcase.assertEqual(
            ledger["fencing_token_floor"], candidate.floor.fencing_token_floor
        )
        expected_binding = _restore_binding_from_records(
            ledger=ledger,
            tombstone=tombstone,
            final_floor=candidate.floor,
            active_tombstones=active,
        )
        events = _read_only_sqlite_snapshot(candidate_path)["transition_events"]
        restore_events = tuple(event for event in events if event[6] == "restore")
        testcase.assertEqual(1, len(restore_events))
        testcase.assertEqual(expected_binding, restore_events[-1][10])


def _assert_captured_candidate_result(
    testcase: unittest.TestCase,
    *,
    root: Path,
    artifact: BackupArtifact,
    captured: RestoreApplyResult,
    actor: str,
    audit_ref: str,
    source_observation: StoreImageObservation,
    destination_observation: StoreImageObservation,
    primary_before_snapshot: dict[str, Any],
) -> None:
    candidate_path = root / _candidate_basename(artifact)
    root_fd = os.open(
        root,
        store_module._open_flags(directory=True, writable=False),
    )
    try:
        candidate_metadata = os.stat(
            candidate_path.name,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        expected_identity = store_module._identity(candidate_metadata)
        with restore_module._owned_fd(
            root_fd,
            candidate_path.name,
            writable=False,
            expected_identity=expected_identity,
        ) as (candidate_fd, _):
            verified = RestoreStoreAuthority().verify_candidate(candidate_fd, captured)
    finally:
        os.close(root_fd)

    testcase.assertEqual(captured, verified)
    testcase.assertEqual(captured.digest, verified.digest)
    testcase.assertEqual(captured.size, verified.size)
    testcase.assertEqual(captured.floor, verified.floor)
    testcase.assertEqual(captured.tombstones, verified.tombstones)
    testcase.assertEqual(captured.active_tombstones, verified.active_tombstones)
    testcase.assertEqual(
        captured.observation.identities,
        source_observation.identities,
    )
    testcase.assertEqual(captured.size, _sqlite_file_metadata(candidate_path)[0])

    candidate_snapshot = _read_only_sqlite_snapshot(candidate_path)
    candidate_meta = dict(candidate_snapshot["store_meta"])
    testcase.assertEqual(3, candidate_snapshot["user_version"])
    testcase.assertEqual(
        captured.floor.recovery_epoch,
        candidate_meta["recovery_epoch"],
    )
    testcase.assertEqual(
        captured.floor.fencing_token_floor,
        candidate_meta["fencing_token_floor"],
    )
    testcase.assertEqual(
        captured.observation.last_clock_ns, candidate_meta["last_clock_ns"]
    )
    source_snapshot = _read_only_sqlite_snapshot(root / artifact.database_basename)
    source_operation_rows = {row[0]: row for row in source_snapshot["operations"]}
    operation_rows = candidate_snapshot["operations"]
    testcase.assertEqual(len(captured.observation.operations), len(operation_rows))
    for operation, row in zip(
        captured.observation.operations,
        operation_rows,
        strict=True,
    ):
        testcase.assertEqual(
            (
                operation.operation_id,
                operation.effect_key,
                operation.status,
                operation.provider_id,
                operation.current_attempt,
                operation.recovery_epoch,
                source_operation_rows[operation.operation_id][6],
                operation.updated_ns,
            ),
            row,
        )
    attempt_rows = {
        (row[0], row[1]): row for row in candidate_snapshot["operation_attempts"]
    }
    for operation in captured.observation.operations:
        row = attempt_rows[(operation.operation_id, operation.current_attempt)]
        testcase.assertEqual(
            (
                operation.operation_id,
                operation.current_attempt,
                operation.owner,
                None if operation.current_attempt == 0 else operation.provider_id,
                operation.lease_epoch,
                operation.fencing_token,
                operation.lease_heartbeat_ns,
                operation.lease_expires_ns,
                operation.fence_proof_version,
                operation.fence_proof_ref,
                operation.effect_started_ns,
                operation.fence_started_ns,
            ),
            row,
        )
    receipt_rows = {
        (row[0], row[1]): row for row in candidate_snapshot["effect_receipts"]
    }
    for operation in captured.observation.operations:
        receipt = operation.verified_receipt_identity
        row = receipt_rows.get((operation.operation_id, operation.current_attempt))
        if receipt is None:
            testcase.assertIsNone(row)
            continue
        testcase.assertIsNotNone(row)
        assert row is not None
        testcase.assertEqual(
            (
                receipt.operation_id,
                receipt.attempt,
                receipt.effect_key,
                receipt.provider_effect_id,
                receipt.provider_status,
                receipt.provider_id,
                receipt.owner,
                receipt.fencing_token,
                receipt.lease_epoch,
            ),
            row[:9],
        )
        testcase.assertIsInstance(row[9], int)
        testcase.assertEqual((receipt.proof_version, receipt.proof_ref), row[10:])

    primary_path = root / store_module.DATABASE_FILENAME
    primary_after_snapshot = _read_only_sqlite_snapshot(primary_path)
    for key in (
        "user_version",
        "store_meta",
        "operations",
        "operation_attempts",
        "effect_receipts",
        "transition_events",
    ):
        testcase.assertEqual(primary_before_snapshot[key], primary_after_snapshot[key])
    testcase.assertEqual(
        source_observation.digest,
        artifact.manifest.database_digest,
    )
    testcase.assertNotEqual(
        source_observation.database_identity,
        destination_observation.database_identity,
    )

    ledger = {
        "restore_generation": 1,
        "actor": actor,
        "audit_ref": audit_ref,
        "backup_digest": artifact.manifest.database_digest,
    }
    tombstone = {
        "identities": [
            {
                "operation_id": identity.operation_id,
                "effect_key": identity.effect_key,
            }
            for identity in captured.tombstones
        ],
        "previous_primary_digest": destination_observation.digest,
        "previous_recovery_epoch": destination_observation.floor.recovery_epoch,
        "previous_fencing_token_hwm": max(
            destination_observation.floor.fencing_token_floor,
            destination_observation.max_fencing_token,
        ),
        "previous_last_clock_ns": destination_observation.last_clock_ns,
    }
    expected_binding = _restore_binding_from_records(
        ledger=ledger,
        tombstone=tombstone,
        final_floor=captured.floor,
        active_tombstones=captured.active_tombstones,
    )
    restore_events = tuple(
        event
        for event in candidate_snapshot["transition_events"]
        if event[6] == "restore"
    )
    testcase.assertEqual(captured.restore_event_count, len(restore_events))
    for event in restore_events:
        operation = next(
            operation
            for operation in captured.observation.operations
            if operation.operation_id == event[2]
        )
        testcase.assertEqual(operation.current_attempt, event[3])
        testcase.assertEqual(operation.status, event[4])
        testcase.assertEqual(operation.status, event[5])
        testcase.assertEqual(actor, event[7])
        testcase.assertEqual(operation.updated_ns, event[8])
        testcase.assertEqual("restore", event[9])
        testcase.assertEqual(expected_binding, event[10])


class _RestoreAcceptanceProvider:
    """Provider double used only to construct a receipted source image."""

    capabilities = ProviderCapabilities(
        idempotency=True,
        fencing=True,
        strong_status=True,
    )

    def __init__(self) -> None:
        self.reserve_calls = 0
        self.execute_calls = 0
        self.status_calls = 0

    def reserve_fence(self, effect: object) -> ProviderFenceProof:
        self.reserve_calls += 1
        del effect
        raise AssertionError("restore acceptance fixture does not reserve a fence")

    def execute(self, effect: object) -> ProviderStatus:
        self.execute_calls += 1
        provider_effect = cast(Any, effect)
        proof = provider_effect.fence_proof
        assert proof is not None
        return ProviderStatus(
            operation_id=provider_effect.operation_id,
            effect_key=provider_effect.effect_key,
            provider_id=provider_effect.provider_id,
            owner=provider_effect.owner,
            attempt=provider_effect.attempt,
            lease_epoch=provider_effect.lease_epoch,
            fencing_token=provider_effect.fencing_token,
            provider_effect_id="provider/effect-source-operation",
            status="COMPLETED",
            consistency="STRONG",
            proof_version=proof.proof_version,
            proof_ref=proof.proof_ref,
        )

    def status(self, effect: object) -> ProviderStatus:
        self.status_calls += 1
        del effect
        raise AssertionError("restore acceptance fixture must not query provider")


def _set_operation_status(root: Path, status: str) -> None:
    connection = sqlite3.connect(str(root / "coordination.sqlite3"))
    try:
        connection.execute(
            "UPDATE operations SET status = ? WHERE operation_id = ?",
            (status, "source-operation"),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def _status_source_artifact(
    temporary: str,
    status: str,
) -> tuple[Path, BackupArtifact, dict[str, Any]]:
    """Build one valid source image for the high-level status matrix."""

    root = _make_root(temporary)
    with CoordinationStore(root, busy_timeout_ms=100, clock=FakeClock(10)) as store:
        store.create_intent(
            "source-operation",
            effect_key="effect/source-operation",
            provider_id="provider/test",
            actor="main",
            clock_ns=10,
        )
        if status == "INTENT":
            pass
        else:
            claim = store.claim(
                "source-operation",
                owner="owner/source-operation",
                provider_id="provider/test",
                lease_ttl_ns=100,
                now_ns=20,
            )
            if status in {"FENCE_PENDING", "RESTORE_INCOMPLETE"}:
                pass
            else:
                started = store._begin_fence_reservation(claim, now_ns=21)
                if status == "FENCE_RESERVATION_STARTED":
                    pass
                else:
                    proof = ProviderFenceProof(
                        operation_id=claim.operation_id,
                        effect_key=claim.effect_key,
                        provider_id=claim.provider_id,
                        owner=claim.owner,
                        attempt=claim.attempt,
                        lease_epoch=claim.lease_epoch,
                        fencing_token=claim.fencing_token,
                        proof_version=1,
                        proof_ref="proof/source-operation",
                    )
                    activated = store._activate_fence(
                        started,
                        proof,
                        now_ns=22,
                    )
                    if status == "CLAIMED":
                        pass
                    elif status == "EFFECT_PREPARED":
                        store._begin_effect(activated, now_ns=23)
                    elif status in {"UNKNOWN_EFFECT", "UNKNOWN"}:
                        effect = store._begin_effect(activated, now_ns=23)
                        store._mark_unknown_effect(effect, now_ns=24)
                    elif status in {"RECEIPTED", "COMPLETED"}:
                        receipt = store.execute_effect(
                            activated,
                            _RestoreAcceptanceProvider(),
                            now_ns=24,
                        )
                        if status == "COMPLETED":
                            store.complete(receipt, now_ns=25)
                    elif status == "CLEANED":
                        pass
                    else:
                        raise AssertionError(f"unsupported fixture status: {status}")

    if status in {"UNKNOWN", "RESTORE_INCOMPLETE", "CLEANED"}:
        _set_operation_status(root, status)
    artifact = SQLiteBackup(root, busy_timeout_ms=100).create("snapshot")
    source_snapshot = _read_only_sqlite_snapshot(root / artifact.database_basename)
    return root, artifact, source_snapshot


def _two_committed_restore_generations(
    temporary: str,
) -> tuple[Path, BackupArtifact, BackupArtifact]:
    root, first_artifact = _artifact_and_newer_destination(temporary)
    BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
        first_artifact,
        actor="operator",
        audit_ref="audit/generation-one",
    )
    second_artifact = SQLiteBackup(root, busy_timeout_ms=100).create("second")
    with CoordinationStore(root, busy_timeout_ms=100, clock=FakeClock(300)) as store:
        store.create_intent(
            "current-only",
            effect_key="effect/current-only",
            provider_id="provider/test",
            actor="main",
            clock_ns=300,
        )
    BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(400)).restore(
        second_artifact,
        actor="operator",
        audit_ref="audit/generation-two",
    )
    return root, first_artifact, second_artifact


def _kill_restore_worker(
    state_root: str,
    artifact: BackupArtifact,
    point: str,
) -> None:
    def kill_at_fault(current: str) -> None:
        if current == point:
            os.kill(os.getpid(), signal.SIGKILL)

    BackupRestore(
        Path(state_root),
        busy_timeout_ms=100,
        clock=FakeClock(200),
        fault=kill_at_fault,
    ).restore(
        artifact,
        actor="operator",
        audit_ref=f"audit/sigkill/{point}",
    )
    os._exit(3)


class RestoreContractTest(unittest.TestCase):
    def test_cleanup_owner_chain_attempts_all_members_and_is_idempotent(self) -> None:
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

        error = RestoreFilesystemError("cleanup chain")
        store_module._attach_cleanup_capability(
            error,
            store_module._CleanupCapability(previous),
        )
        restore_module._with_cleanup_owner(error, current)

        with self.assertRaises(OSError):
            error.retry_cleanup()
        self.assertEqual(2, len(calls))
        self.assertEqual(1, calls.count("previous"))
        self.assertEqual(1, calls.count("current"))
        error.retry_cleanup()
        self.assertEqual(3, len(calls))
        self.assertEqual(2, calls.count("previous"))
        self.assertEqual(1, calls.count("current"))

    def test_candidate_name_and_evidence_ref_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root = _make_root(temporary)
            artifact = SQLiteBackup(root, busy_timeout_ms=100).create("snapshot")
            self.assertEqual(
                ".coordination.sqlite3.restore-"
                + artifact.manifest.database_digest.removeprefix("sha256:"),
                _candidate_basename(artifact),
            )
            self.assertEqual(
                _evidence_ref("audit/restore"),
                "sha256:" + hashlib.sha256(b"audit/restore").hexdigest(),
            )

    def test_restore_result_is_immutable_and_has_no_resource_fields(self) -> None:
        result = RestoreResult(
            phase="RESTORE_COMMITTED",
            restore_generation=1,
            backup_digest="sha256:" + "a" * 64,
            candidate_digest="sha256:" + "b" * 64,
            floor=RecoveryFloor(1, 2),
            identities=(),
            active_tombstones=(),
        )
        with self.assertRaises(AttributeError):
            result.phase = "RESTORE_ABORTED"  # type: ignore[misc]
        self.assertNotIn("path", result.__dataclass_fields__)
        self.assertNotIn("fd", result.__dataclass_fields__)
        self.assertNotIn("token", result.__dataclass_fields__)


class RestoreIntegrationTest(unittest.TestCase):
    def test_new_restore_passes_generation_and_audit_evidence_ref(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            original_apply = RestoreStoreAuthority.apply_candidate
            calls: list[dict[str, Any]] = []
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))

            def apply_candidate(
                authority: RestoreStoreAuthority,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                calls.append(dict(kwargs))
                return original_apply(authority, *args, **kwargs)

            with mock.patch.object(
                RestoreStoreAuthority,
                "apply_candidate",
                new=apply_candidate,
            ):
                result = restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/generation",
                )
                second = restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/generation-next",
                )

            self.assertEqual("RESTORE_COMMITTED", result.phase)
            self.assertEqual(1, calls[0]["restore_generation"])
            self.assertEqual(
                _evidence_ref("audit/generation"),
                calls[0]["evidence_ref"],
            )
            self.assertEqual("RESTORE_COMMITTED", second.phase)
            self.assertEqual(2, calls[1]["restore_generation"])
            self.assertEqual(
                _evidence_ref("audit/generation-next"),
                calls[1]["evidence_ref"],
            )

    def test_new_restore_passes_previous_active_tombstones_and_returns_union(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            original_apply = RestoreStoreAuthority.apply_candidate
            calls: list[dict[str, Any]] = []

            def apply_candidate(
                authority: RestoreStoreAuthority,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                calls.append(dict(kwargs))
                return original_apply(authority, *args, **kwargs)

            with mock.patch.object(
                RestoreStoreAuthority,
                "apply_candidate",
                new=apply_candidate,
            ):
                result = BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/active-union",
                )

            self.assertEqual((), calls[0]["previous_active_tombstones"])
            self.assertEqual(
                (("destination-only", "effect/destination-only"),),
                tuple(
                    (identity.operation_id, identity.effect_key)
                    for identity in result.active_tombstones
                ),
            )

    def test_empty_next_batch_keeps_the_committed_active_union(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, first_artifact = _artifact_and_newer_destination(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            first = restore.restore(
                first_artifact,
                actor="operator",
                audit_ref="audit/cumulative-one",
            )
            second_artifact = SQLiteBackup(root, busy_timeout_ms=100).create("second")
            original_apply = RestoreStoreAuthority.apply_candidate
            calls: list[dict[str, Any]] = []

            def apply_candidate(
                authority: RestoreStoreAuthority,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                calls.append(dict(kwargs))
                return original_apply(authority, *args, **kwargs)

            with mock.patch.object(
                RestoreStoreAuthority,
                "apply_candidate",
                new=apply_candidate,
            ):
                second = restore.restore(
                    second_artifact,
                    actor="operator",
                    audit_ref="audit/cumulative-two",
                )

            self.assertEqual("RESTORE_COMMITTED", second.phase)
            self.assertEqual(2, second.restore_generation)
            self.assertEqual((), second.identities)
            self.assertEqual(
                first.active_tombstones, calls[0]["previous_active_tombstones"]
            )
            self.assertEqual(first.active_tombstones, second.active_tombstones)
            self.assertEqual(
                second.active_tombstones, calls[0]["previous_active_tombstones"]
            )

    def test_tampered_committed_union_blocks_older_backup_before_new_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            first = BackupRestore(
                root,
                busy_timeout_ms=100,
                clock=FakeClock(200),
            ).restore(
                artifact,
                actor="operator",
                audit_ref="audit/cumulative-tamper",
            )
            primary_before = (root / store_module.DATABASE_FILENAME).read_bytes()
            tombstone_path = root / recovery_module.RECOVERY_TOMBSTONES_BASENAME
            _rewrite_tombstone_identities(
                tombstone_path,
                (("forged-operation", "effect/forged"),),
            )
            tampered_tombstones = tombstone_path.read_bytes()
            candidate_path = root / _candidate_basename(artifact)
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("history tamper must block apply"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "cleanup",
                    side_effect=AssertionError("history tamper must block cleanup"),
                ),
                self.assertRaises(StoreIntegrityError),
            ):
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(300)).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/cumulative-tamper-next",
                )
            self.assertEqual(
                primary_before, (root / store_module.DATABASE_FILENAME).read_bytes()
            )
            self.assertEqual(tampered_tombstones, tombstone_path.read_bytes())
            self.assertFalse(candidate_path.exists())
            self.assertEqual(1, first.restore_generation)

    def test_prior_committed_union_survives_an_aborted_generation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, first_artifact = _artifact_and_newer_destination(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            first = restore.restore(
                first_artifact,
                actor="operator",
                audit_ref="audit/aborted-cumulative-one",
            )
            second_artifact = SQLiteBackup(root, busy_timeout_ms=100).create("second")
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(300),
                    fault=FaultAt("before_replace_call"),
                ).restore(
                    second_artifact,
                    actor="operator",
                    audit_ref="audit/aborted-cumulative-two",
                )
            controller = WalSidecarController(root, busy_timeout_ms=100)
            session = controller.hold_quiescence(
                allowed_root_names=(
                    second_artifact.database_basename,
                    second_artifact.manifest_basename,
                    _candidate_basename(second_artifact),
                )
            )
            try:
                owner = session.issue_owner()
                ledger = RestoreLedger(root, busy_timeout_ms=100)
                handle = ledger.read(owner)
                assert handle is not None
                ledger.mark_aborted(
                    handle,
                    RecoveryFloor(handle.recovery_epoch, handle.fencing_token_floor),
                    owner,
                )
            finally:
                session.close()
                controller.close()

            result = BackupRestore(
                root,
                busy_timeout_ms=100,
                clock=FakeClock(400),
            ).restore(
                first_artifact,
                actor="operator",
                audit_ref="audit/aborted-cumulative-three",
            )
            self.assertEqual("RESTORE_COMMITTED", result.phase)
            self.assertEqual(3, result.restore_generation)
            self.assertEqual(first.active_tombstones, result.active_tombstones)

    def test_history_binding_is_verified_before_candidate_apply(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            original_apply = RestoreStoreAuthority.apply_candidate
            original_verify = RestoreStoreAuthority.verify_history_binding
            events: list[str] = []

            def verify_history_binding(
                authority: RestoreStoreAuthority,
                primary_fd: int,
                state: object,
            ) -> None:
                events.append("history")
                original_verify(authority, primary_fd, state)

            def apply_candidate(
                authority: RestoreStoreAuthority,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                events.append("apply")
                return original_apply(authority, *args, **kwargs)

            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "verify_history_binding",
                    new=verify_history_binding,
                ),
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    new=apply_candidate,
                ),
            ):
                result = BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/history-order",
                )

            self.assertEqual("RESTORE_COMMITTED", result.phase)
            self.assertIn("history", events)
            self.assertIn("apply", events)
            self.assertLess(events.index("history"), events.index("apply"))

    def test_history_binding_failure_blocks_candidate_apply(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            binding_error = StoreIntegrityError("tampered restore history binding")
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "verify_history_binding",
                    side_effect=binding_error,
                ),
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("history failure must block apply"),
                ),
                self.assertRaises(StoreIntegrityError),
            ):
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/history-failure",
                )

    def test_nonresume_uses_pair_barrier_before_any_candidate_work(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))

            original_read = RestoreLedger.read
            original_read_for_resume = RestoreLedger.read_for_resume
            recovery_calls: list[str] = []

            def read(ledger: RestoreLedger, owner: object) -> Any:
                recovery_calls.append("read")
                return original_read(ledger, owner)

            def read_for_resume(ledger: RestoreLedger, owner: object) -> Any:
                recovery_calls.append("read_for_resume")
                return original_read_for_resume(ledger, owner)

            with (
                mock.patch.object(RestoreLedger, "read", new=read),
                mock.patch.object(
                    RestoreLedger,
                    "read_for_resume",
                    new=read_for_resume,
                ),
            ):
                result = restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/nonresume-barrier",
                )
            self.assertEqual("RESTORE_COMMITTED", result.phase)
            self.assertEqual("read_for_resume", recovery_calls[0])

    def test_nonresume_terminal_state_uses_pair_barrier(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            first = restore.restore(
                artifact,
                actor="operator",
                audit_ref="audit/nonresume-terminal",
            )
            self.assertEqual("RESTORE_COMMITTED", first.phase)
            original_read_for_resume = RestoreLedger.read_for_resume
            original_read = RestoreLedger.read
            recovery_calls: list[str] = []
            barrier_calls = 0

            def read(ledger: RestoreLedger, owner: object) -> Any:
                recovery_calls.append("read")
                return original_read(ledger, owner)

            def read_for_resume(
                ledger: RestoreLedger,
                owner: object,
            ) -> object:
                nonlocal barrier_calls
                barrier_calls += 1
                recovery_calls.append("read_for_resume")
                return original_read_for_resume(ledger, owner)

            with (
                mock.patch.object(RestoreLedger, "read", new=read),
                mock.patch.object(
                    RestoreLedger,
                    "read_for_resume",
                    new=read_for_resume,
                ),
            ):
                second = restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/nonresume-terminal",
                )
            self.assertEqual("RESTORE_COMMITTED", second.phase)
            self.assertGreaterEqual(barrier_calls, 1)
            self.assertEqual("read_for_resume", recovery_calls[0])

    def test_zero_event_source_with_tombstones_fails_before_candidate_apply(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root = _make_root(temporary)
            artifact = SQLiteBackup(root, busy_timeout_ms=100).create("snapshot")
            with CoordinationStore(
                root,
                busy_timeout_ms=100,
                clock=FakeClock(100),
            ) as store:
                store.create_intent(
                    "destination-only",
                    effect_key="effect/destination-only",
                    provider_id="provider/test",
                    actor="main",
                    clock_ns=100,
                )
            primary_before = (root / store_module.DATABASE_FILENAME).read_bytes()
            with self.assertRaises(StoreIntegrityError):
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/zero-event",
                )
            self.assertEqual(
                primary_before,
                (root / store_module.DATABASE_FILENAME).read_bytes(),
            )
            self.assertFalse((root / recovery_module.RECOVERY_LEDGER_BASENAME).exists())
            self.assertFalse(
                (root / recovery_module.RECOVERY_TOMBSTONES_BASENAME).exists()
            )

    def test_nonresume_barrier_failure_blocks_candidate_and_primary_work(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            barrier_error = recovery_module.RecoveryDurabilityError(
                "restore pair durability is unknown"
            )
            with (
                mock.patch.object(
                    RestoreLedger,
                    "read_for_resume",
                    side_effect=barrier_error,
                ),
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("barrier failure must block apply"),
                ),
                self.assertRaises(recovery_module.RecoveryDurabilityError),
            ):
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/barrier-failure",
                )
            self.assertFalse((root / recovery_module.RECOVERY_LEDGER_BASENAME).exists())
            self.assertFalse(
                (root / recovery_module.RECOVERY_TOMBSTONES_BASENAME).exists()
            )

    def test_retained_resource_registries_are_bounded_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-registry-"
        ) as temporary:
            root, _ = _artifact_and_newer_destination(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))

            session = cast(QuiescenceSession, object())
            restore._retain_session(session)
            restore._retain_session(session)
            self.assertEqual(1, len(restore._orphan_sessions))
            restore._orphan_sessions.clear()
            for _ in range(restore_module._MAX_ORPHAN_SESSIONS):
                restore._retain_session(cast(QuiescenceSession, object()))
            session_owner = mock.Mock()
            with self.assertRaises(RestoreFilesystemError) as raised:
                restore._retain_session(cast(QuiescenceSession, session_owner))
            raised.exception.retry_cleanup()
            session_owner.close.assert_called_once_with()
            restore._orphan_sessions.clear()

            controller = cast(WalSidecarController, object())
            restore._retain_controller(controller)
            restore._retain_controller(controller)
            self.assertEqual(1, len(restore._orphan_controllers))
            restore._orphan_controllers.clear()
            for _ in range(restore_module._MAX_ORPHAN_CONTROLLERS):
                restore._retain_controller(cast(WalSidecarController, object()))
            controller_owner = mock.Mock()
            with self.assertRaises(RestoreFilesystemError) as raised:
                restore._retain_controller(cast(WalSidecarController, controller_owner))
            raised.exception.retry_cleanup()
            controller_owner.close.assert_called_once_with()
            restore._orphan_controllers.clear()

            fds: list[int] = []
            try:
                for _ in range(restore_module._MAX_ORPHAN_FDS):
                    fd = os.open(root / "coordination.sqlite3", os.O_RDONLY)
                    fds.append(fd)
                    restore_module._remember_orphan_fd(
                        restore._orphan_fds,
                        fd,
                        None,
                        "restore registry",
                    )
                extra_fd = os.open(root / "coordination.sqlite3", os.O_RDONLY)
                try:
                    with self.assertRaises(RestoreFilesystemError) as raised:
                        restore_module._remember_orphan_fd(
                            restore._orphan_fds,
                            extra_fd,
                            None,
                            "restore registry",
                            cleanup_callback=lambda: os.close(extra_fd),
                        )
                    self.assertEqual(
                        restore_module._MAX_ORPHAN_FDS, len(restore._orphan_fds)
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
                restore._orphan_fds.clear()

    def test_owned_fd_preserves_body_error_and_attaches_retry_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-owned-fd-"
        ) as temporary:
            root = _make_root(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            entry = root / "restore-owned"
            entry.write_bytes(b"owned")
            entry.chmod(0o600)
            root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            body_error = RestoreError("injected owned-fd body failure")

            with (
                mock.patch.object(
                    restore_module,
                    "_close_temporary_fd",
                    side_effect=StoreUnavailableError(
                        "injected owned-fd close failure"
                    ),
                ),
                self.assertRaisesRegex(
                    RestoreError,
                    "injected owned-fd body failure",
                ) as raised,
            ):
                try:
                    with restore_module._owned_fd(
                        root_fd,
                        "restore-owned",
                        writable=False,
                        orphan_registry=restore._orphan_fds,
                        cleanup_callback=restore.close,
                    ):
                        raise body_error
                finally:
                    os.close(root_fd)

            self.assertIs(body_error, raised.exception)
            self.assertEqual(1, len(restore._orphan_fds))
            raised.exception.retry_cleanup()
            self.assertEqual([], restore._orphan_fds)

    def test_attrless_restore_body_gets_typed_cleanup_wrapper(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-owned-fd-"
        ) as temporary:
            root = _make_root(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            entry = root / "restore-owned"
            entry.write_bytes(b"owned")
            entry.chmod(0o600)
            root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            body_error = AttrlessBody("injected attrless restore body failure")
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
                    restore_module,
                    "_close_temporary_fd",
                    side_effect=StoreUnavailableError(
                        "injected attrless restore cleanup failure"
                    ),
                ),
                self.assertRaises(RestoreFilesystemError) as raised,
            ):
                try:
                    with restore_module._owned_fd(
                        root_fd,
                        "restore-owned",
                        writable=False,
                        orphan_registry=restore._orphan_fds,
                        cleanup_callback=restore.close,
                    ):
                        raise body_error
                finally:
                    os.close(root_fd)

            self.assertIs(body_error, raised.exception.__cause__)
            raised.exception.retry_cleanup()

    def test_attrless_post_open_fstat_retains_known_identity_and_fd(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-owned-fd-"
        ) as temporary:
            root = _make_root(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            entry = root / "restore-owned"
            entry.write_bytes(b"owned")
            entry.chmod(0o600)
            known_metadata = entry.stat()
            known_identity = (known_metadata.st_dev, known_metadata.st_ino)
            root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            original_open = os.open
            original_fstat = os.fstat
            original_close = os.close
            original_attach = store_module._attach_cleanup_capability
            target_fd: int | None = None
            body_error = AttrlessBody("post-open status unavailable")

            def open_file(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
                nonlocal target_fd
                fd = original_open(path, flags, *args, **kwargs)
                if path == "restore-owned":
                    target_fd = fd
                return fd

            def fstat(fd: int) -> os.stat_result:
                if target_fd is not None and fd == target_fd:
                    raise body_error
                return original_fstat(fd)

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
                    mock.patch.object(
                        restore_module,
                        "_close_temporary_fd",
                        side_effect=StoreUnavailableError(
                            "persistent restore close failure"
                        ),
                    ),
                    mock.patch.object(
                        store_module,
                        "_attach_cleanup_capability",
                        side_effect=attach,
                    ),
                    self.assertRaises(RestoreFilesystemError) as raised,
                    restore_module._owned_fd(
                        root_fd,
                        "restore-owned",
                        writable=False,
                        expected_identity=known_identity,
                        orphan_registry=restore._orphan_fds,
                        cleanup_callback=restore.close,
                    ),
                ):
                    pass
            finally:
                original_close(root_fd)

            self.assertIs(body_error, raised.exception.__cause__)
            self.assertEqual(1, len(restore._orphan_fds))
            assert target_fd is not None
            self.assertEqual(
                (target_fd, known_identity),
                restore._orphan_fds[0][:2],
            )
            restore.close()
            self.assertEqual([], restore._orphan_fds)
            with self.assertRaises(OSError):
                os.fstat(target_fd)

    def test_handoff_unknown_status_retains_identity_or_blocks_reused_fd(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-handoff-fd-"
        ) as temporary:
            root = _make_root(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            original_fstat = os.fstat
            known_fd = os.open(root / "coordination.sqlite3", os.O_RDONLY)
            known_identity = (
                original_fstat(known_fd).st_dev,
                original_fstat(known_fd).st_ino,
            )

            def fail_unknown(fd: int) -> os.stat_result:
                if fd == known_fd:
                    raise OSError("handoff status is unavailable")
                return original_fstat(fd)

            try:
                with mock.patch.object(os, "fstat", side_effect=fail_unknown):
                    restore_module._remember_orphan_fd(
                        restore._orphan_fds,
                        known_fd,
                        known_identity,
                        "restore handoff",
                    )
                self.assertEqual((known_fd, known_identity), restore._orphan_fds[0][:2])
                restore.close()
                self.assertEqual([], restore._orphan_fds)
            finally:
                try:
                    os.close(known_fd)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        raise

            unknown_fd = os.open(root / "coordination.sqlite3", os.O_RDONLY)

            def fail_identity(fd: int) -> os.stat_result:
                if fd == unknown_fd:
                    raise OSError("handoff identity is unavailable")
                return original_fstat(fd)

            try:
                with mock.patch.object(os, "fstat", side_effect=fail_identity):
                    restore_module._remember_orphan_fd(
                        restore._orphan_fds,
                        unknown_fd,
                        None,
                        "restore unresolved handoff",
                    )
                os.close(unknown_fd)
                replacement_path = root / "replacement"
                replacement_path.write_bytes(b"replacement")
                filler_fds: list[int] = []
                replacement_fd: int | None = None
                while replacement_fd is None:
                    opened_fd = os.open(replacement_path, os.O_RDONLY)
                    if opened_fd == unknown_fd:
                        replacement_fd = opened_fd
                    else:
                        filler_fds.append(opened_fd)
                try:
                    with self.assertRaises(RestoreFilesystemError):
                        restore.close()
                    self.assertEqual(1, len(restore._orphan_fds))
                    assert replacement_fd is not None
                    os.fstat(replacement_fd)
                finally:
                    if replacement_fd is not None:
                        try:
                            os.close(replacement_fd)
                        except OSError as error:
                            if error.errno != errno.EBADF:
                                raise
                    for filler_fd in filler_fds:
                        os.close(filler_fd)
            finally:
                try:
                    os.close(unknown_fd)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        raise

    def test_owned_fd_adopts_lower_cleanup_capability_into_restore_error(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-owned-fd-"
        ) as temporary:
            root = _make_root(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            entry = root / "restore-owned"
            entry.write_bytes(b"owned")
            entry.chmod(0o600)
            root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            cleanup_calls: list[str] = []
            lower_error = StoreUnavailableError("lower close uncertainty")
            store_module._attach_cleanup_capability(
                lower_error,
                store_module._CleanupCapability(
                    lambda: cleanup_calls.append("lower"),
                ),
            )

            try:
                with (
                    mock.patch.object(
                        restore_module,
                        "_close_temporary_fd",
                        side_effect=lower_error,
                    ),
                    self.assertRaises(RestoreFilesystemError) as raised,
                    restore_module._owned_fd(
                        root_fd,
                        "restore-owned",
                        writable=False,
                        orphan_registry=restore._orphan_fds,
                        cleanup_callback=restore.close,
                    ),
                ):
                    pass
            finally:
                os.close(root_fd)

            self.assertIs(lower_error, raised.exception.__cause__)
            self.assertEqual(1, len(restore._orphan_fds))
            raised.exception.retry_cleanup()
            self.assertEqual(["lower"], cleanup_calls)
            self.assertEqual([], restore._orphan_fds)

    def test_attrless_unknown_fd_identity_never_closes_a_reused_fd(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-handoff-fd-"
        ) as temporary:
            root = _make_root(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            original_close = os.close
            original_fstat = os.fstat
            unknown_fd = os.open(root / "coordination.sqlite3", os.O_RDONLY)
            status_error = AttrlessBody("handoff status is unavailable")

            def fail_identity(fd: int) -> os.stat_result:
                if fd == unknown_fd:
                    raise status_error
                return original_fstat(fd)

            try:
                with mock.patch.object(os, "fstat", side_effect=fail_identity):
                    restore_module._remember_orphan_fd(
                        restore._orphan_fds,
                        unknown_fd,
                        None,
                        "restore unresolved handoff",
                    )
                self.assertEqual((unknown_fd, None), restore._orphan_fds[0][:2])
                original_close(unknown_fd)
                replacement_path = root / "replacement"
                replacement_path.write_bytes(b"replacement")
                filler_fds: list[int] = []
                replacement_fd: int | None = None
                while replacement_fd is None:
                    opened_fd = os.open(replacement_path, os.O_RDONLY)
                    if opened_fd == unknown_fd:
                        replacement_fd = opened_fd
                    else:
                        filler_fds.append(opened_fd)
                try:
                    with self.assertRaises(RestoreFilesystemError):
                        restore.close()
                    assert replacement_fd is not None
                    os.fstat(replacement_fd)
                finally:
                    if replacement_fd is not None:
                        original_close(replacement_fd)
                    for filler_fd in filler_fds:
                        original_close(filler_fd)
                    restore._orphan_fds.clear()
            finally:
                try:
                    original_close(unknown_fd)
                except OSError as close_error:
                    if close_error.errno != errno.EBADF:
                        raise

    def test_known_fd_reuse_is_retained_without_closing_the_foreign_fd(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-reused-fd-"
        ) as temporary:
            root = _make_root(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            original_fstat = os.fstat
            original_close = os.close
            owned_fd = os.open(root / "coordination.sqlite3", os.O_RDONLY)
            metadata = original_fstat(owned_fd)
            expected_identity = (metadata.st_dev, metadata.st_ino)
            restore._orphan_fds.append((owned_fd, expected_identity, "known fd"))
            original_close(owned_fd)

            replacement_path = root / "foreign"
            replacement_path.write_bytes(b"foreign")
            replacement_path.chmod(0o600)
            filler_fds: list[int] = []
            replacement_fd: int | None = None
            while replacement_fd is None:
                opened_fd = os.open(replacement_path, os.O_RDONLY)
                if opened_fd == owned_fd:
                    replacement_fd = opened_fd
                else:
                    filler_fds.append(opened_fd)
            try:
                with self.assertRaises(RestoreFilesystemError):
                    restore.close()
                self.assertEqual(
                    (owned_fd, expected_identity),
                    restore._orphan_fds[0][:2],
                )
                original_fstat(replacement_fd)
            finally:
                original_close(replacement_fd)
                for filler_fd in filler_fds:
                    original_close(filler_fd)
                restore._orphan_fds.clear()

    def test_controller_overflow_preserves_acquisition_and_current_owner(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-controller-overflow-"
        ) as temporary:
            root = _make_root(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            restore._orphan_controllers.extend(
                cast(WalSidecarController, mock.Mock())
                for _ in range(restore_module._MAX_ORPHAN_CONTROLLERS)
            )
            controller = mock.Mock()
            acquisition_error = RestoreError("controller acquisition")
            controller.hold_quiescence.side_effect = acquisition_error
            controller.close.side_effect = OSError("controller close")

            with self.assertRaises(RestoreError) as raised:
                restore._hold_quiescence(
                    cast(WalSidecarController, controller),
                    allowed_root_names=(),
                )
            self.assertIs(acquisition_error, raised.exception)
            self.assertEqual(
                restore_module._MAX_ORPHAN_CONTROLLERS,
                len(restore._orphan_controllers),
            )

            controller.close.side_effect = None
            raised.exception.retry_cleanup()
            self.assertEqual([], restore._orphan_controllers)

    def test_session_overflow_preserves_cleanup_error_and_current_owner(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-session-overflow-"
        ) as temporary:
            root = _make_root(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            restore._orphan_sessions.extend(
                cast(QuiescenceSession, mock.Mock())
                for _ in range(restore_module._MAX_ORPHAN_SESSIONS)
            )
            session = mock.Mock()
            cleanup_error = OSError("session close")
            session.close.side_effect = cleanup_error

            with (
                self.assertRaises(OSError) as raised,
                restore._session_lifecycle(cast(QuiescenceSession, session)),
            ):
                pass
            self.assertIs(cleanup_error, raised.exception)
            self.assertEqual(
                restore_module._MAX_ORPHAN_SESSIONS,
                len(restore._orphan_sessions),
            )

            session.close.side_effect = None
            cast(Any, raised.exception).retry_cleanup()
            self.assertEqual([], restore._orphan_sessions)

    def test_fd_overflow_runs_current_and_registry_owners_independently(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-fd-overflow-"
        ) as temporary:
            root = _make_root(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            entry = root / "restore-current"
            entry.write_bytes(b"current")
            entry.chmod(0o600)
            root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            retained_fds: list[int] = []
            database_metadata = (root / "coordination.sqlite3").stat()
            database_identity = (database_metadata.st_dev, database_metadata.st_ino)
            for _ in range(restore_module._MAX_ORPHAN_FDS):
                fd = os.open(root / "coordination.sqlite3", os.O_RDONLY)
                retained_fds.append(fd)
                restore._orphan_fds.append((fd, database_identity, "retained"))

            close_attempts: list[int] = []

            def fail_close(
                fd: int,
                expected_identity: tuple[int, int] | None,
                label: str,
            ) -> None:
                del expected_identity, label
                close_attempts.append(fd)
                raise StoreUnavailableError("persistent restore fd close failure")

            def open_and_close_current() -> None:
                with restore_module._owned_fd(
                    root_fd,
                    "restore-current",
                    writable=False,
                    orphan_registry=restore._orphan_fds,
                    cleanup_callback=restore.close,
                ):
                    pass

            patcher = mock.patch.object(
                restore_module,
                "_close_temporary_fd",
                side_effect=fail_close,
            )
            try:
                patcher.start()
                with self.assertRaises(RestoreFilesystemError) as raised:
                    open_and_close_current()
                with self.assertRaises(RestoreFilesystemError):
                    raised.exception.retry_cleanup()
                self.assertGreaterEqual(
                    len(close_attempts), restore_module._MAX_ORPHAN_FDS + 2
                )
            finally:
                patcher.stop()
            try:
                raised.exception.retry_cleanup()
                self.assertEqual([], restore._orphan_fds)
            finally:
                os.close(root_fd)
                for fd in retained_fds:
                    try:
                        os.close(fd)
                    except OSError as close_error:
                        if close_error.errno != errno.EBADF:
                            raise

    def test_failed_quiescence_acquisition_retains_controller_for_next_operation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-controller-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            restore = MarkerLockFaultRestore(root)
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
                self.assertRaises(RestoreError) as raised,
            ):
                restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/controller-acquisition",
                )

            self.assertEqual(1, len(restore._orphan_controllers))
            retained = restore._orphan_controllers[0]
            raised.exception.retry_cleanup()
            self.assertEqual([], restore._orphan_controllers)
            result = restore.restore(
                artifact,
                actor="operator",
                audit_ref="audit/controller-acquisition",
            )
            self.assertEqual("RESTORE_COMMITTED", result.phase)
            self.assertEqual([], restore._orphan_controllers)
            self.assertEqual([], retained._pending_fds)

    def test_preflight_backup_helper_is_retained_and_retried(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-backup-helper-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            helper = restore._backup_helper
            original_close_temporary_fd = doctor_module._close_temporary_fd

            def fail_database_close(
                fd: int,
                expected_identity: tuple[int, int] | None,
                label: str,
            ) -> None:
                if label == "backup database":
                    raise doctor_module.StateFilesystemError(
                        "persistent preflight database close failure"
                    )
                original_close_temporary_fd(fd, expected_identity, label)

            with (
                mock.patch.object(
                    doctor_module,
                    "_close_temporary_fd",
                    side_effect=fail_database_close,
                ),
                self.assertRaises(RestoreFilesystemError) as raised,
            ):
                restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/preflight-helper",
                )

            self.assertIs(helper, restore._backup_helper)
            self.assertEqual(1, len(helper._orphan_fds))
            with (
                mock.patch.object(
                    doctor_module,
                    "_close_temporary_fd",
                    side_effect=fail_database_close,
                ),
                self.assertRaises(RestoreFilesystemError),
            ):
                restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/preflight-helper",
                )
            self.assertEqual(1, len(helper._orphan_fds))

            raised.exception.retry_cleanup()
            self.assertEqual([], helper._orphan_fds)
            result = restore.restore(
                artifact,
                actor="operator",
                audit_ref="audit/preflight-helper",
            )
            self.assertEqual("RESTORE_COMMITTED", result.phase)
            self.assertEqual([], helper._orphan_fds)

    def test_candidate_first_restore_cleans_once_and_calls_no_external_actions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            clock = FakeClock(200)
            fault = FaultAt("never")
            restore = BackupRestore(root, busy_timeout_ms=100, clock=clock, fault=fault)
            blocked = mock.patch.object(
                CoordinationStore,
                "__init__",
                side_effect=AssertionError("normal CoordinationStore open"),
            )
            forbidden_copy = mock.patch.object(
                QuiescenceSession,
                "copy_database_to",
                side_effect=AssertionError("restore must not copy primary"),
            )
            with blocked, forbidden_copy:
                result = restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/restore",
                )
            self.assertIsInstance(result, RestoreResult)
            self.assertEqual("RESTORE_COMMITTED", result.phase)
            self.assertEqual(1, result.restore_generation)
            self.assertIn(
                ("destination-only", "effect/destination-only"),
                {(item.operation_id, item.effect_key) for item in result.identities},
            )
            self.assertFalse((root / _candidate_basename(artifact)).exists())
            self.assertEqual(
                "sha256:" + hashlib.sha256(b"audit/restore").hexdigest(),
                _evidence_ref("audit/restore"),
            )

    def test_restore_passes_destination_high_water_to_ledger_prepare(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with CoordinationStore(
                root, busy_timeout_ms=100, clock=FakeClock(200)
            ) as store:
                store._advance_floor(store._reserve_floor(), now_ns=200)
            result = BackupRestore(
                root,
                busy_timeout_ms=100,
                clock=FakeClock(300),
            ).restore(
                artifact,
                actor="operator",
                audit_ref="audit/high-water",
            )
            self.assertEqual("RESTORE_COMMITTED", result.phase)

    def test_restore_closes_only_its_borrowed_image_descriptors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with mock.patch(
                "agent_team.restore._close_temporary_fd",
                wraps=wal_module._close_temporary_fd,
            ) as close_fd:
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/fd-ownership",
                )
            self.assertGreaterEqual(close_fd.call_count, 5)

    def test_close_uncertainty_does_not_return_success_or_prepare_ledger(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with (
                mock.patch(
                    "agent_team.restore._close_temporary_fd",
                    side_effect=StoreUnavailableError("injected close uncertainty"),
                ),
                self.assertRaises(StoreUnavailableError),
            ):
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/close-uncertainty",
                )
            self.assertFalse((root / "recovery.ledger").exists())

    def test_orphan_candidate_is_never_overwritten_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            candidate_path = root / _candidate_basename(artifact)
            fault = FaultAt("after_store_apply_call")
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=fault,
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/orphan",
                )
            before = (candidate_path.read_bytes(), candidate_path.stat().st_ino)
            with self.assertRaises(RestoreFilesystemError):
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(300)).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/orphan",
                )
            self.assertEqual(
                before, (candidate_path.read_bytes(), candidate_path.stat().st_ino)
            )

    def test_artifact_pair_is_revalidated_before_replace_and_phase_append(self) -> None:
        cases = (
            ("before_replace_call", "source", "RESTORE_PREPARED"),
            ("before_mark_replaced_call", "manifest", "RESTORE_PREPARED"),
            ("before_mark_committed_call", "source", "RESTORE_REPLACED"),
        )
        for point, entry, expected_phase in cases:
            with (
                self.subTest(point=point, entry=entry),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-restore-artifact-swap-"
                ) as temporary,
            ):
                root, artifact = _artifact_and_newer_destination(temporary)

                def swap_after_fault(
                    current: str,
                    *,
                    fault_point: str = point,
                    artifact_root: Path = root,
                    current_artifact: BackupArtifact = artifact,
                    current_entry: str = entry,
                ) -> None:
                    if current == fault_point:
                        _swap_with_byte_identical_inode(
                            artifact_root
                            / (
                                current_artifact.database_basename
                                if current_entry == "source"
                                else current_artifact.manifest_basename
                            )
                        )

                with self.assertRaises(RestoreReviewRequiredError):
                    BackupRestore(
                        root,
                        busy_timeout_ms=100,
                        clock=FakeClock(200),
                        fault=swap_after_fault,
                    ).restore(
                        artifact,
                        actor="operator",
                        audit_ref=f"audit/artifact-swap/{point}",
                    )
                self.assertEqual(expected_phase, _latest_ledger_phase(root))

    def test_byte_identical_source_or_manifest_swap_after_final_fault_is_caught(
        self,
    ) -> None:
        for entry in ("source", "manifest"):
            with (
                self.subTest(entry=entry),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-restore-final-swap-"
                ) as temporary,
            ):
                root, artifact = _artifact_and_newer_destination(temporary)

                def swap_before_result(
                    current: str,
                    *,
                    artifact_root: Path = root,
                    current_artifact: BackupArtifact = artifact,
                    current_entry: str = entry,
                ) -> None:
                    if current == "before_result_call":
                        _swap_with_byte_identical_inode(
                            artifact_root
                            / (
                                current_artifact.database_basename
                                if current_entry == "source"
                                else current_artifact.manifest_basename
                            )
                        )

                with self.assertRaises(RestoreReviewRequiredError):
                    BackupRestore(
                        root,
                        busy_timeout_ms=100,
                        clock=FakeClock(200),
                        fault=swap_before_result,
                    ).restore(
                        artifact,
                        actor="operator",
                        audit_ref=f"audit/final-swap/{entry}",
                    )

    def test_fifo_existing_open_never_blocks_owned_fd(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-fifo-"
        ) as temporary:
            root = _make_root(temporary)
            os.mkfifo(root / "restore-fifo", 0o600)
            process = multiprocessing.Process(
                target=_open_fifo_with_owned_fd,
                args=(str(root),),
            )
            process.start()
            process.join(timeout=2.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)
                self.fail("owned FIFO open blocked despite bounded restore open")
            self.assertEqual(0, process.exitcode)

    def test_persistent_orphan_fd_close_is_retained_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-orphan-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            with (
                mock.patch(
                    "agent_team.restore._close_temporary_fd",
                    side_effect=StoreUnavailableError("persistent close failure"),
                ),
                self.assertRaises(StoreUnavailableError),
            ):
                restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/orphan-fd",
                )
            self.assertGreaterEqual(len(restore._orphan_fds), 1)
            restore.close()
            self.assertEqual([], restore._orphan_fds)

    def test_fault_after_quiescence_call_releases_session(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-quiescence-fault-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("after_quiescence_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/quiescence-fault",
                )

            result = BackupRestore(
                root,
                busy_timeout_ms=100,
                clock=FakeClock(300),
            ).restore(
                artifact,
                actor="operator",
                audit_ref="audit/quiescence-fault-retry",
            )
            self.assertEqual("RESTORE_COMMITTED", result.phase)

    def test_quiescence_close_failure_is_retained_and_retried_before_next_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-session-close-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            original_close = QuiescenceSession.close
            closed_sessions: list[QuiescenceSession] = []
            failed = False

            def fail_once(session: QuiescenceSession) -> None:
                nonlocal failed
                closed_sessions.append(session)
                if not failed:
                    failed = True
                    raise OSError("injected restore session close failure")
                original_close(session)

            with (
                mock.patch.object(QuiescenceSession, "close", new=fail_once),
                self.assertRaises(OSError),
            ):
                restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/session-close",
                )

            self.assertEqual(1, len(restore._orphan_sessions))
            retained = restore._orphan_sessions[0]
            result = restore.resume(
                artifact,
                actor="operator",
                audit_ref="audit/session-close",
            )
            self.assertEqual("RESTORE_COMMITTED", result.phase)
            self.assertEqual([], restore._orphan_sessions)
            self.assertIs(retained, closed_sessions[0])

    def test_persistent_quiescence_close_failure_is_bounded_and_explicitly_retryable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-session-close-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            close_error = StoreUnavailableError(
                "persistent restore session close failure"
            )

            with (
                mock.patch.object(
                    QuiescenceSession,
                    "close",
                    side_effect=close_error,
                ),
                self.assertRaises(StoreUnavailableError) as raised,
            ):
                restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/session-persistent",
                )
            self.assertEqual(1, len(restore._orphan_sessions))

            with (
                mock.patch.object(
                    QuiescenceSession,
                    "close",
                    side_effect=close_error,
                ),
                self.assertRaises(StoreUnavailableError),
            ):
                restore.resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/session-persistent",
                )
            self.assertEqual(1, len(restore._orphan_sessions))

            raised.exception.retry_cleanup()
            self.assertEqual([], restore._orphan_sessions)

    def test_quiescence_close_failure_does_not_replace_restore_body_error(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-session-body-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            restore = BackupRestore(
                root,
                busy_timeout_ms=100,
                clock=FakeClock(200),
                fault=FaultAt("after_quiescence_call"),
            )
            close_error = OSError("injected restore cleanup failure")

            with (
                mock.patch.object(
                    QuiescenceSession,
                    "close",
                    side_effect=close_error,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "injected restore fault: after_quiescence_call",
                ),
            ):
                restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/session-body",
                )

            self.assertEqual(1, len(restore._orphan_sessions))
            restore.close()

    def test_quiescence_unlock_failure_is_retained_until_next_operation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-session-unlock-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            restore = BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200))
            original_hold = WalSidecarController.hold_quiescence
            held_sessions: list[QuiescenceSession] = []

            def capture_hold(
                controller: WalSidecarController,
                *,
                allowed_root_names: tuple[str, ...] = (),
            ) -> QuiescenceSession:
                session = original_hold(
                    controller,
                    allowed_root_names=allowed_root_names,
                )
                held_sessions.append(session)
                return session

            original_flock = fcntl.flock
            failed = False

            def fail_unlock(fd: int, operation: int) -> None:
                nonlocal failed
                session = held_sessions[0] if held_sessions else None
                session_fds = (
                    ()
                    if session is None
                    else (
                        session._resources.marker_fd,
                        session._resources.gate_fd,
                    )
                )
                if operation == fcntl.LOCK_UN and fd in session_fds and not failed:
                    failed = True
                    raise OSError("injected restore unlock failure")
                original_flock(fd, operation)

            with (
                mock.patch.object(
                    WalSidecarController,
                    "hold_quiescence",
                    new=capture_hold,
                ),
                mock.patch.object(fcntl, "flock", side_effect=fail_unlock),
                self.assertRaises(RestoreFilesystemError),
            ):
                restore.restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/session-unlock",
                )
            self.assertEqual(1, len(restore._orphan_sessions))

            result = restore.resume(
                artifact,
                actor="operator",
                audit_ref="audit/session-unlock",
            )
            self.assertEqual("RESTORE_COMMITTED", result.phase)
            self.assertEqual([], restore._orphan_sessions)

    def test_before_result_mutation_is_rechecked_before_return(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-result-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            candidate_path = root / _candidate_basename(artifact)

            def mutate_before_result(point: str) -> None:
                if point == "before_result_call":
                    candidate_path.write_bytes(b"foreign")
                    candidate_path.chmod(0o600)

            with self.assertRaises(RestoreReviewRequiredError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=mutate_before_result,
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/result-window",
                )
            self.assertTrue(candidate_path.exists())

    def test_old_backup_identity_or_effect_collision_is_rejected_before_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root = _make_root(temporary)
            with CoordinationStore(
                root,
                busy_timeout_ms=100,
                clock=FakeClock(50),
            ) as store:
                store.create_intent(
                    "base-operation",
                    effect_key="effect/base-operation",
                    provider_id="provider/test",
                    actor="main",
                    clock_ns=50,
                )
            empty_artifact = SQLiteBackup(root, busy_timeout_ms=100).create("empty")
            old_image_path = Path(os.path.realpath(temporary)) / "old-image"
            with CoordinationStore(
                root, busy_timeout_ms=100, clock=FakeClock(100)
            ) as store:
                store.create_intent(
                    "tomb-op",
                    effect_key="effect/tomb",
                    provider_id="provider/test",
                    actor="main",
                    clock_ns=100,
                )
                connection = store._connection
                assert connection is not None
                old_image_path.write_bytes(connection.serialize())
            old_image_path.chmod(0o600)
            old_fd = os.open(old_image_path, os.O_RDONLY)
            try:
                old_observation = RestoreStoreAuthority().inspect_image(old_fd)
            finally:
                os.close(old_fd)

            BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
                empty_artifact,
                actor="operator",
                audit_ref="audit/tombstone-first",
            )
            (root / empty_artifact.database_basename).unlink()
            (root / empty_artifact.manifest_basename).unlink()

            old_database = root / "old"
            old_manifest = root / "old.manifest"
            old_database.write_bytes(old_image_path.read_bytes())
            old_database.chmod(0o600)
            old_manifest_value = BackupManifest(
                version=1,
                database_basename="old",
                store_schema=3,
                event_schema_version=2,
                sqlite_user_version=3,
                integrity_check="ok",
                database_size=old_observation.size,
                database_digest=old_observation.digest,
                captured_recovery_epoch=old_observation.floor.recovery_epoch,
                captured_fencing_token_floor=old_observation.floor.fencing_token_floor,
            )
            old_manifest.write_bytes(_encode_manifest(old_manifest_value))
            old_manifest.chmod(0o600)
            old_artifact = BackupArtifact(
                database_basename="old",
                manifest_basename="old.manifest",
                manifest=old_manifest_value,
                database_identity=(
                    old_database.stat().st_dev,
                    old_database.stat().st_ino,
                ),
                manifest_identity=(
                    old_manifest.stat().st_dev,
                    old_manifest.stat().st_ino,
                ),
                workflow_row_counts=old_observation.workflow_row_counts,
            )
            primary_before = (root / "coordination.sqlite3").read_bytes()
            candidate_path = root / _candidate_basename(old_artifact)
            with self.assertRaises(RestoreReviewRequiredError):
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(300)).restore(
                    old_artifact,
                    actor="operator",
                    audit_ref="audit/tombstone-resurrection",
                )
            self.assertFalse(candidate_path.exists())
            self.assertEqual(
                primary_before, (root / "coordination.sqlite3").read_bytes()
            )
            with CoordinationStore(
                root, busy_timeout_ms=100, clock=FakeClock(400)
            ) as store:
                self.assertIsNone(store.operation("tomb-op"))
                with self.assertRaises(LeaseConflictError):
                    store.claim(
                        "tomb-op",
                        owner="owner",
                        provider_id="provider/test",
                        lease_ttl_ns=100,
                        now_ns=400,
                    )

    def test_initial_restore_requires_cleaned_cleanup_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root = _make_root(temporary)
            artifact = SQLiteBackup(root, busy_timeout_ms=100).create("snapshot")
            cleanup = SidecarCleanupResult(
                outcome="BLOCKED",
                request=CheckpointRequest("TRUNCATE"),
                checkpoint=None,
                removed=(),
                reason="READER_ACTIVE",
            )
            with (
                mock.patch.object(QuiescenceSession, "cleanup", return_value=cleanup),
                self.assertRaises(RestoreError),
            ):
                BackupRestore(root, busy_timeout_ms=100).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/cleanup",
                )

    def test_restore_rejects_pending_without_implicit_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            fault = FaultAt("before_replace_call")
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=fault,
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/pending",
                )
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("pending restore must not apply"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("pending restore must not replace"),
                ),
                self.assertRaises(RestorePendingError),
            ):
                BackupRestore(root, busy_timeout_ms=100).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/pending",
                )

    def test_resume_prepared_old_primary_candidate_present_verifies_then_replaces(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("before_replace_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/resume-old",
                )
            with mock.patch.object(
                RestoreStoreAuthority,
                "apply_candidate",
                side_effect=AssertionError("resume must not reapply"),
            ):
                result = BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(300),
                ).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/resume-old",
                )
            self.assertEqual("RESTORE_COMMITTED", result.phase)

    def test_replace_rejects_same_inode_primary_mutation_after_fault_barrier(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-primary-mutation-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            candidate_path = root / _candidate_basename(artifact)
            ledger_path = root / recovery_module.RECOVERY_LEDGER_BASENAME
            tombstone_path = root / recovery_module.RECOVERY_TOMBSTONES_BASENAME
            captured: dict[str, bytes] = {}
            primary_path = root / "coordination.sqlite3"
            primary_before = primary_path.read_bytes()
            replacement_bytes = (root / artifact.database_basename).read_bytes()
            self.assertEqual(len(primary_before), len(replacement_bytes))

            def mutate_primary(point: str) -> None:
                if point != "before_replace_call":
                    return
                captured["candidate"] = candidate_path.read_bytes()
                captured["ledger"] = ledger_path.read_bytes()
                captured["tombstone"] = tombstone_path.read_bytes()
                fd = os.open(primary_path, os.O_RDWR)
                try:
                    os.pwrite(fd, replacement_bytes, 0)
                    os.fsync(fd)
                finally:
                    os.close(fd)

            with (
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("mutated primary must not replace"),
                ),
                self.assertRaises(RestoreReviewRequiredError),
            ):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=mutate_primary,
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/primary-mutation",
                )
            self.assertNotEqual(primary_before, primary_path.read_bytes())
            self.assertEqual(replacement_bytes, primary_path.read_bytes())
            self.assertEqual(captured["candidate"], candidate_path.read_bytes())
            self.assertEqual(captured["ledger"], ledger_path.read_bytes())
            self.assertEqual(captured["tombstone"], tombstone_path.read_bytes())

    def test_prepared_resume_rejects_previous_destination_hwm_tampering(
        self,
    ) -> None:
        for field in (
            "previous_recovery_epoch",
            "previous_fencing_token_hwm",
            "previous_last_clock_ns",
        ):
            for direction in ("high", "low"):
                with (
                    self.subTest(field=field, direction=direction),
                    tempfile.TemporaryDirectory(
                        prefix="agent-team-restore-previous-hwm-"
                    ) as temporary,
                ):
                    root, artifact = _artifact_and_newer_destination(temporary)
                    with CoordinationStore(
                        root, busy_timeout_ms=100, clock=FakeClock(200)
                    ) as store:
                        store._advance_floor(store._reserve_floor(), now_ns=200)
                    with self.assertRaises(RuntimeError):
                        BackupRestore(
                            root,
                            busy_timeout_ms=100,
                            clock=FakeClock(300),
                            fault=FaultAt("before_replace_call"),
                        ).restore(
                            artifact,
                            actor="operator",
                            audit_ref="audit/previous-hwm-test",
                        )

                    primary_path = root / "coordination.sqlite3"
                    candidate_path = root / _candidate_basename(artifact)
                    ledger_path = root / recovery_module.RECOVERY_LEDGER_BASENAME
                    tombstone_path = root / recovery_module.RECOVERY_TOMBSTONES_BASENAME
                    primary_before = primary_path.read_bytes()
                    candidate_before = candidate_path.read_bytes()
                    ledger_before = ledger_path.read_bytes()
                    tombstone_item = json.loads(
                        tombstone_path.read_text(encoding="utf-8").splitlines()[-1]
                    )
                    original = tombstone_item[field]
                    tampered = original + 1000 if direction == "high" else original - 1
                    self.assertGreaterEqual(tampered, 0)
                    _rewrite_tombstone_field(tombstone_path, field, tampered)
                    tombstone_after_tamper = tombstone_path.read_bytes()

                    with (
                        mock.patch.object(
                            RestoreStoreAuthority,
                            "apply_candidate",
                            side_effect=AssertionError("resume must not reapply"),
                        ),
                        mock.patch.object(
                            QuiescenceSession,
                            "replace_database",
                            side_effect=AssertionError("tampered HWM must not replace"),
                        ),
                        self.assertRaises(
                            RestoreReviewRequiredError
                            if direction == "low" or field == "previous_last_clock_ns"
                            else recovery_module.RecoveryLedgerError
                        ),
                    ):
                        BackupRestore(root, busy_timeout_ms=100).resume(
                            artifact,
                            actor="operator",
                            audit_ref="audit/previous-hwm-test",
                        )
                    self.assertEqual(primary_before, primary_path.read_bytes())
                    self.assertEqual(candidate_before, candidate_path.read_bytes())
                    self.assertEqual(ledger_before, ledger_path.read_bytes())
                    self.assertEqual(
                        tombstone_after_tamper,
                        tombstone_path.read_bytes(),
                    )

    def test_initial_tombstone_first_rejects_hwm_tampering_before_ledger_completion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-initial-tombstone-hwm-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with CoordinationStore(
                root, busy_timeout_ms=100, clock=FakeClock(200)
            ) as store:
                store._advance_floor(store._reserve_floor(), now_ns=200)
            original_append = recovery_module.RecoveryLedgerWriter._append_owned_at_root

            def fail_initial_ledger(
                writer: recovery_module.RecoveryLedgerWriter,
                root_fd: int,
                record: recovery_module.RecoveryLedgerRecord,
                *,
                allow_create: bool,
            ) -> recovery_module.RecoveryLedgerRecord:
                if (
                    record.restore_generation == 1
                    and record.phase == "RESTORE_PREPARED"
                ):
                    raise recovery_module.RecoveryLedgerError(
                        "simulated initial ledger response loss"
                    )
                return original_append(
                    writer,
                    root_fd,
                    record,
                    allow_create=allow_create,
                )

            with (
                mock.patch.object(
                    recovery_module.RecoveryLedgerWriter,
                    "_append_owned_at_root",
                    new=fail_initial_ledger,
                ),
                self.assertRaises(recovery_module.RecoveryLedgerError),
            ):
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(300)).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/initial-tombstone-hwm",
                )

            primary_path = root / "coordination.sqlite3"
            candidate_path = root / _candidate_basename(artifact)
            ledger_path = root / recovery_module.RECOVERY_LEDGER_BASENAME
            tombstone_path = root / recovery_module.RECOVERY_TOMBSTONES_BASENAME
            self.assertFalse(ledger_path.exists())
            primary_before = primary_path.read_bytes()
            candidate_before = candidate_path.read_bytes()
            _rewrite_tombstone_field(
                tombstone_path,
                "previous_last_clock_ns",
                1000,
            )
            tombstone_before_resume = tombstone_path.read_bytes()
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("orphan resume must not reapply"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("tampered orphan must not replace"),
                ),
                mock.patch.object(
                    recovery_module.RecoveryLedgerWriter,
                    "_append_owned_at_root",
                    side_effect=AssertionError(
                        "tampered orphan must not complete ledger"
                    ),
                ),
                self.assertRaises(RestoreReviewRequiredError),
            ):
                BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/initial-tombstone-hwm",
                )
            self.assertFalse(ledger_path.exists())
            self.assertEqual(primary_before, primary_path.read_bytes())
            self.assertEqual(candidate_before, candidate_path.read_bytes())
            self.assertEqual(tombstone_before_resume, tombstone_path.read_bytes())

    def test_next_tombstone_first_rejects_hwm_tampering_before_ledger_completion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-next-tombstone-hwm-"
        ) as temporary:
            root, first_artifact = _artifact_and_newer_destination(temporary)
            BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(300)).restore(
                first_artifact,
                actor="operator",
                audit_ref="audit/next-tombstone-hwm/one",
            )
            second_artifact = SQLiteBackup(root, busy_timeout_ms=100).create("second")
            original_append = recovery_module.RecoveryLedgerWriter._append_owned_at_root

            def fail_second_ledger(
                writer: recovery_module.RecoveryLedgerWriter,
                root_fd: int,
                record: recovery_module.RecoveryLedgerRecord,
                *,
                allow_create: bool,
            ) -> recovery_module.RecoveryLedgerRecord:
                if (
                    record.restore_generation == 2
                    and record.phase == "RESTORE_PREPARED"
                ):
                    raise recovery_module.RecoveryLedgerError(
                        "simulated next-generation ledger response loss"
                    )
                return original_append(
                    writer,
                    root_fd,
                    record,
                    allow_create=allow_create,
                )

            with (
                mock.patch.object(
                    recovery_module.RecoveryLedgerWriter,
                    "_append_owned_at_root",
                    new=fail_second_ledger,
                ),
                self.assertRaises(recovery_module.RecoveryLedgerError),
            ):
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(400)).restore(
                    second_artifact,
                    actor="operator",
                    audit_ref="audit/next-tombstone-hwm/two",
                )

            primary_path = root / "coordination.sqlite3"
            candidate_path = root / _candidate_basename(second_artifact)
            ledger_path = root / recovery_module.RECOVERY_LEDGER_BASENAME
            tombstone_path = root / recovery_module.RECOVERY_TOMBSTONES_BASENAME
            primary_before = primary_path.read_bytes()
            candidate_before = candidate_path.read_bytes()
            ledger_before = ledger_path.read_bytes()
            _rewrite_tombstone_field(
                tombstone_path,
                "previous_last_clock_ns",
                0,
            )
            tombstone_before_resume = tombstone_path.read_bytes()
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("orphan resume must not reapply"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("tampered orphan must not replace"),
                ),
                mock.patch.object(
                    recovery_module.RecoveryLedgerWriter,
                    "_append_owned_at_root",
                    side_effect=AssertionError(
                        "tampered orphan must not complete ledger"
                    ),
                ),
                self.assertRaises(RestoreReviewRequiredError),
            ):
                BackupRestore(root, busy_timeout_ms=100).resume(
                    second_artifact,
                    actor="operator",
                    audit_ref="audit/next-tombstone-hwm/two",
                )
            self.assertEqual(ledger_before, ledger_path.read_bytes())
            self.assertEqual(primary_before, primary_path.read_bytes())
            self.assertEqual(candidate_before, candidate_path.read_bytes())
            self.assertEqual(tombstone_before_resume, tombstone_path.read_bytes())

    def test_resume_prepared_aborted_tombstone_never_replaces(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-aborted-response-loss-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("before_replace_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/aborted-response-loss",
                )

            candidate_path = root / _candidate_basename(artifact)
            controller = WalSidecarController(root, busy_timeout_ms=100)
            session = controller.hold_quiescence(
                allowed_root_names=(
                    artifact.database_basename,
                    artifact.manifest_basename,
                    candidate_path.name,
                )
            )
            try:
                owner = session.issue_owner()
                ledger = RestoreLedger(root, busy_timeout_ms=100)
                handle = ledger.read(owner)
                assert handle is not None
                original_append = (
                    recovery_module.RecoveryLedgerWriter._append_owned_at_root
                )

                def fail_ledger_abort(
                    writer: recovery_module.RecoveryLedgerWriter,
                    root_fd: int,
                    record: recovery_module.RecoveryLedgerRecord,
                    *,
                    allow_create: bool,
                ) -> recovery_module.RecoveryLedgerRecord:
                    if record.phase == "RESTORE_ABORTED":
                        raise recovery_module.RecoveryLedgerError(
                            "simulated ledger abort response loss"
                        )
                    return original_append(
                        writer,
                        root_fd,
                        record,
                        allow_create=allow_create,
                    )

                with (
                    mock.patch.object(
                        recovery_module.RecoveryLedgerWriter,
                        "_append_owned_at_root",
                        new=fail_ledger_abort,
                    ),
                    self.assertRaises(recovery_module.RecoveryLedgerError),
                ):
                    ledger.mark_aborted(
                        handle,
                        floor=RecoveryFloor(
                            handle.recovery_epoch,
                            handle.fencing_token_floor,
                        ),
                        owner=owner,
                    )
            finally:
                session.close()

            with mock.patch.object(
                QuiescenceSession,
                "replace_database",
                side_effect=AssertionError(
                    "aborted response-loss resume must not replace"
                ),
            ):
                result = BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/aborted-response-loss",
                )
            self.assertEqual("RESTORE_ABORTED", result.phase)
            self.assertTrue(candidate_path.exists())

    def test_aborted_response_loss_checks_old_primary_before_ledger_append(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-aborted-primary-mismatch-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("before_replace_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/aborted-primary-mismatch",
                )

            candidate_path = root / _candidate_basename(artifact)
            controller = WalSidecarController(root, busy_timeout_ms=100)
            session = controller.hold_quiescence(
                allowed_root_names=(
                    artifact.database_basename,
                    artifact.manifest_basename,
                    candidate_path.name,
                )
            )
            try:
                owner = session.issue_owner()
                ledger = RestoreLedger(root, busy_timeout_ms=100)
                handle = ledger.read(owner)
                assert handle is not None
                original_append = (
                    recovery_module.RecoveryLedgerWriter._append_owned_at_root
                )

                def fail_ledger_abort(
                    writer: recovery_module.RecoveryLedgerWriter,
                    root_fd: int,
                    record: recovery_module.RecoveryLedgerRecord,
                    *,
                    allow_create: bool,
                ) -> recovery_module.RecoveryLedgerRecord:
                    if record.phase == "RESTORE_ABORTED":
                        raise recovery_module.RecoveryLedgerError(
                            "simulated ledger abort response loss"
                        )
                    return original_append(
                        writer,
                        root_fd,
                        record,
                        allow_create=allow_create,
                    )

                with (
                    mock.patch.object(
                        recovery_module.RecoveryLedgerWriter,
                        "_append_owned_at_root",
                        new=fail_ledger_abort,
                    ),
                    self.assertRaises(recovery_module.RecoveryLedgerError),
                ):
                    ledger.mark_aborted(
                        handle,
                        floor=RecoveryFloor(
                            handle.recovery_epoch,
                            handle.fencing_token_floor,
                        ),
                        owner=owner,
                    )
            finally:
                session.close()

            ledger_path = root / recovery_module.RECOVERY_LEDGER_BASENAME
            ledger_before = ledger_path.read_bytes()
            primary_path = root / "coordination.sqlite3"
            primary_path.write_bytes(candidate_path.read_bytes())
            primary_path.chmod(0o600)
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("aborted mismatch must not apply"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("aborted mismatch must not replace"),
                ),
                self.assertRaises(RestoreReviewRequiredError),
            ):
                BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/aborted-primary-mismatch",
                )
            self.assertEqual(ledger_before, ledger_path.read_bytes())
            self.assertTrue(candidate_path.exists())

    def test_resume_initial_tombstone_first_orphan_completes_ledger(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-initial-tombstone-orphan-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            original_append = recovery_module.RecoveryLedgerWriter._append_owned_at_root

            def fail_initial_ledger(
                writer: recovery_module.RecoveryLedgerWriter,
                root_fd: int,
                record: recovery_module.RecoveryLedgerRecord,
                *,
                allow_create: bool,
            ) -> recovery_module.RecoveryLedgerRecord:
                if (
                    record.restore_generation == 1
                    and record.phase == "RESTORE_PREPARED"
                ):
                    raise recovery_module.RecoveryLedgerError(
                        "simulated initial ledger response loss"
                    )
                return original_append(
                    writer,
                    root_fd,
                    record,
                    allow_create=allow_create,
                )

            with (
                mock.patch.object(
                    recovery_module.RecoveryLedgerWriter,
                    "_append_owned_at_root",
                    new=fail_initial_ledger,
                ),
                self.assertRaises(recovery_module.RecoveryLedgerError),
            ):
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/initial-tombstone-orphan",
                )
            self.assertFalse((root / "recovery.ledger").exists())
            self.assertTrue((root / "recovery.tombstones").exists())

            with mock.patch.object(
                RestoreStoreAuthority,
                "apply_candidate",
                side_effect=AssertionError("orphan resume must not reapply"),
            ):
                result = BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/initial-tombstone-orphan",
                )
            self.assertEqual("RESTORE_COMMITTED", result.phase)

    def test_resume_next_generation_tombstone_first_orphan_completes_ledger(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-next-tombstone-orphan-"
        ) as temporary:
            root, first_artifact = _artifact_and_newer_destination(temporary)
            BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
                first_artifact,
                actor="operator",
                audit_ref="audit/next-tombstone-first/one",
            )
            second_artifact = SQLiteBackup(root, busy_timeout_ms=100).create("second")
            original_append = recovery_module.RecoveryLedgerWriter._append_owned_at_root

            def fail_second_ledger(
                writer: recovery_module.RecoveryLedgerWriter,
                root_fd: int,
                record: recovery_module.RecoveryLedgerRecord,
                *,
                allow_create: bool,
            ) -> recovery_module.RecoveryLedgerRecord:
                if (
                    record.restore_generation == 2
                    and record.phase == "RESTORE_PREPARED"
                ):
                    raise recovery_module.RecoveryLedgerError(
                        "simulated next-generation ledger response loss"
                    )
                return original_append(
                    writer,
                    root_fd,
                    record,
                    allow_create=allow_create,
                )

            with (
                mock.patch.object(
                    recovery_module.RecoveryLedgerWriter,
                    "_append_owned_at_root",
                    new=fail_second_ledger,
                ),
                self.assertRaises(recovery_module.RecoveryLedgerError),
            ):
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(300)).restore(
                    second_artifact,
                    actor="operator",
                    audit_ref="audit/next-tombstone-first/two",
                )
            self.assertEqual("RESTORE_COMMITTED", _latest_ledger_phase(root))

            with mock.patch.object(
                RestoreStoreAuthority,
                "apply_candidate",
                side_effect=AssertionError("orphan resume must not reapply"),
            ):
                result = BackupRestore(root, busy_timeout_ms=100).resume(
                    second_artifact,
                    actor="operator",
                    audit_ref="audit/next-tombstone-first/two",
                )
            self.assertEqual("RESTORE_COMMITTED", result.phase)

    def test_resume_prepared_missing_candidate_stops_without_reapply(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("before_replace_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/missing-candidate",
                )
            candidate_path = root / _candidate_basename(artifact)
            candidate_path.unlink()
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("missing candidate must not reapply"),
                ),
                self.assertRaises(RestoreReviewRequiredError),
            ):
                BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/missing-candidate",
                )
            self.assertEqual(
                artifact.manifest.database_digest,
                SQLiteBackup(root, busy_timeout_ms=100)
                .inspect("snapshot")
                .manifest.database_digest,
            )

    def test_resume_prepared_new_primary_candidate_missing_stops_for_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("after_replace_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/resume-new",
                )
            self.assertFalse((root / _candidate_basename(artifact)).exists())
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("resume must not reapply"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("resume must not replace new primary"),
                ),
                self.assertRaises(RestoreReviewRequiredError),
            ):
                BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/resume-new",
                )
            self.assertFalse((root / _candidate_basename(artifact)).exists())

    def test_replaced_resume_commits_only_and_committed_resume_is_noop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("after_mark_replaced_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/replaced",
                )
            restore = BackupRestore(root, busy_timeout_ms=100)
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("replaced resume must not apply"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("replaced resume must not replace"),
                ),
            ):
                result = restore.resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/replaced",
                )
            self.assertEqual("RESTORE_COMMITTED", result.phase)
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("committed resume must be a no-op"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("committed resume must not replace"),
                ),
            ):
                self.assertEqual(
                    result,
                    restore.resume(
                        artifact,
                        actor="operator",
                        audit_ref="audit/replaced",
                    ),
                )

    def test_aborted_restore_is_terminal_and_keeps_candidate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("before_replace_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/aborted",
                )
            candidate_path = root / _candidate_basename(artifact)
            controller = WalSidecarController(root, busy_timeout_ms=100)
            session = controller.hold_quiescence(
                allowed_root_names=(
                    artifact.database_basename,
                    artifact.manifest_basename,
                    candidate_path.name,
                )
            )
            try:
                owner = session.issue_owner()
                ledger = RestoreLedger(root, busy_timeout_ms=100)
                handle = ledger.read(owner)
                assert handle is not None
                aborted = ledger.mark_aborted(
                    handle,
                    RecoveryFloor(handle.recovery_epoch, handle.fencing_token_floor),
                    owner,
                )
                self.assertEqual("RESTORE_ABORTED", aborted.phase)
            finally:
                session.close()
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("aborted resume must not apply"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("aborted resume must not replace"),
                ),
            ):
                result = BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/aborted",
                )
            self.assertEqual("RESTORE_ABORTED", result.phase)
            self.assertTrue(candidate_path.exists())

    def test_sigkill_after_replace_leaves_pending_state_for_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)

            def kill_after_replace(point: str) -> None:
                if point == "after_replace_call":
                    os.kill(os.getpid(), signal.SIGKILL)

            child = os.fork()
            if child == 0:
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=kill_after_replace,
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/sigkill",
                )
                os._exit(3)
            _, wait_status = os.waitpid(child, 0)
            self.assertEqual(-signal.SIGKILL, os.waitstatus_to_exitcode(wait_status))
            self.assertFalse((root / _candidate_basename(artifact)).exists())
            with self.assertRaises(RestoreReviewRequiredError):
                BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/sigkill",
                )

    def test_wal_directory_fsync_exception_never_promotes_prepared_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            original_fault = WalSidecarController._fault

            def fail_before_directory_fsync(
                controller: WalSidecarController,
                point: str,
            ) -> None:
                if point == "before_directory_fsync":
                    raise RuntimeError("injected directory fsync uncertainty")
                original_fault(controller, point)

            with (
                mock.patch.object(
                    WalSidecarController,
                    "_fault",
                    fail_before_directory_fsync,
                ),
                self.assertRaises(RuntimeError),
            ):
                BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/wal-fsync-exception",
                )
            self.assertFalse((root / _candidate_basename(artifact)).exists())
            with self.assertRaises(RestoreReviewRequiredError):
                BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/wal-fsync-exception",
                )

    def test_wal_directory_fsync_sigkill_never_promotes_prepared_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            original_fault = WalSidecarController._fault

            def kill_before_directory_fsync(
                controller: WalSidecarController,
                point: str,
            ) -> None:
                if point == "before_directory_fsync":
                    os.kill(os.getpid(), signal.SIGKILL)
                original_fault(controller, point)

            child = os.fork()
            if child == 0:
                with mock.patch.object(
                    WalSidecarController,
                    "_fault",
                    kill_before_directory_fsync,
                ):
                    BackupRestore(
                        root, busy_timeout_ms=100, clock=FakeClock(200)
                    ).restore(
                        artifact,
                        actor="operator",
                        audit_ref="audit/wal-fsync-sigkill",
                    )
                os._exit(3)
            _, wait_status = os.waitpid(child, 0)
            self.assertEqual(-signal.SIGKILL, os.waitstatus_to_exitcode(wait_status))
            self.assertFalse((root / _candidate_basename(artifact)).exists())
            with self.assertRaises(RestoreReviewRequiredError):
                BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/wal-fsync-sigkill",
                )

    def test_initial_final_readback_rejects_post_commit_manifest_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)

            def mutate_manifest(point: str) -> None:
                if point == "after_mark_committed_call":
                    (root / artifact.manifest_basename).unlink()

            with self.assertRaises(BackupIncompleteError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=mutate_manifest,
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/final-manifest",
                )

    def test_initial_final_readback_rejects_post_commit_primary_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)

            def mutate_primary(point: str) -> None:
                if point == "after_mark_committed_call":
                    fd = os.open(root / "coordination.sqlite3", os.O_RDWR)
                    try:
                        os.pwrite(fd, b"x", 0)
                        os.fsync(fd)
                    finally:
                        os.close(fd)

            with self.assertRaises(StoreIntegrityError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=mutate_primary,
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/final-primary",
                )

    def test_terminal_resume_final_readback_rejects_post_commit_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("after_mark_replaced_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/final-resume",
                )

            def mutate_manifest(point: str) -> None:
                if point == "after_mark_committed_call":
                    (root / artifact.manifest_basename).unlink()

            with self.assertRaises(BackupIncompleteError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    fault=mutate_manifest,
                ).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/final-resume",
                )

    def test_prepared_new_primary_with_candidate_present_is_mixed_review_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("after_replace_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/mixed-state",
                )
            candidate_path = root / _candidate_basename(artifact)
            candidate_path.write_bytes((root / "coordination.sqlite3").read_bytes())
            candidate_path.chmod(0o600)
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("mixed state must not reapply"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("mixed state must not replace"),
                ),
                self.assertRaises(RestoreReviewRequiredError),
            ):
                BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/mixed-state",
                )
            self.assertTrue(candidate_path.exists())

    def test_prepared_primary_missing_is_rejected_before_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("before_replace_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/missing-primary",
                )
            (root / "coordination.sqlite3").unlink()
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("missing primary must not reapply"),
                ),
                self.assertRaises(WalSidecarUnsafeError),
            ):
                BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/missing-primary",
                )

    def test_prepared_candidate_mismatch_is_read_only_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("before_replace_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/mismatched-candidate",
                )
            candidate_path = root / _candidate_basename(artifact)
            with candidate_path.open("r+b") as candidate:
                candidate.seek(0)
                candidate.write(b"x")
                candidate.flush()
                os.fsync(candidate.fileno())
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("mismatched candidate must not reapply"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("mismatched candidate must not replace"),
                ),
                self.assertRaises(StoreIntegrityError),
            ):
                BackupRestore(root, busy_timeout_ms=100).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/mismatched-candidate",
                )

    def test_orchestration_fault_matrix_is_fail_closed_and_non_reapplying(self) -> None:
        fault_points = (
            "before_source_open_call",
            "after_source_open_call",
            "before_destination_open_call",
            "after_destination_open_call",
            "before_candidate_create_call",
            "after_candidate_create_call",
            "before_store_observation_call",
            "after_store_observation_call",
            "before_floor_reservation_call",
            "after_floor_reservation_call",
            "before_store_apply_call",
            "after_store_apply_call",
            "before_candidate_verify_call",
            "after_candidate_verify_call",
            "before_ledger_prepare_call",
            "after_ledger_prepare_call",
            "before_replace_call",
            "after_replace_call",
            "before_mark_replaced_call",
            "after_mark_replaced_call",
            "before_mark_committed_call",
            "after_mark_committed_call",
            "before_final_artifact_inspect_call",
            "before_final_primary_verify_call",
            "before_result_call",
        )
        for point in fault_points:
            with (
                self.subTest(point=point),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-restore-fault-"
                ) as temporary,
            ):
                root, artifact = _artifact_and_newer_destination(temporary)
                with self.assertRaises(RuntimeError):
                    BackupRestore(
                        root,
                        busy_timeout_ms=100,
                        clock=FakeClock(200),
                        fault=FaultAt(point),
                    ).restore(
                        artifact,
                        actor="operator",
                        audit_ref=f"audit/fault/{point}",
                    )

    def test_restore_never_enters_provider_or_recovery_coordinator(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-restore-") as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            with (
                mock.patch.object(
                    lease_module,
                    "require_provider_capabilities",
                    side_effect=AssertionError("provider path entered"),
                ),
                mock.patch.object(
                    recovery_module,
                    "RecoveryCoordinator",
                    side_effect=AssertionError("recovery coordinator entered"),
                ),
            ):
                result = BackupRestore(
                    root, busy_timeout_ms=100, clock=FakeClock(200)
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/no-provider",
                )
            self.assertEqual("RESTORE_COMMITTED", result.phase)


class RestoreReleaseAcceptanceTest(unittest.TestCase):
    def test_restore_status_matrix_preserves_projection_and_rebinds_receipt(
        self,
    ) -> None:
        statuses = (
            "INTENT",
            "FENCE_PENDING",
            "FENCE_RESERVATION_STARTED",
            "CLAIMED",
            "EFFECT_PREPARED",
            "UNKNOWN_EFFECT",
            "UNKNOWN",
            "RECEIPTED",
            "COMPLETED",
            "CLEANED",
        )
        for status in statuses:
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-restore-status-matrix-"
                ) as temporary,
            ):
                root, artifact, source = _status_source_artifact(temporary, status)
                with (
                    mock.patch.object(
                        lease_module,
                        "require_provider_capabilities",
                        side_effect=AssertionError("restore entered provider path"),
                    ),
                    mock.patch.object(
                        recovery_module,
                        "RecoveryCoordinator",
                        side_effect=AssertionError(
                            "restore entered recovery coordinator"
                        ),
                    ),
                ):
                    result = BackupRestore(
                        root,
                        busy_timeout_ms=100,
                        clock=FakeClock(200),
                    ).restore(
                        artifact,
                        actor="operator",
                        audit_ref=f"audit/status/{status.lower()}",
                    )

                after = _read_only_sqlite_snapshot(root / "coordination.sqlite3")
                self.assertEqual("RESTORE_COMMITTED", result.phase)
                self.assertEqual(source["user_version"], after["user_version"])
                source_meta = dict(source["store_meta"])
                after_meta = dict(after["store_meta"])
                self.assertEqual(set(source_meta), set(after_meta))
                self.assertEqual(
                    source_meta["store_schema"], after_meta["store_schema"]
                )
                self.assertEqual(1, after_meta["recovery_epoch"])
                self.assertEqual(
                    source_meta["fencing_token_floor"]
                    + 1
                    + (1 if status == "RECEIPTED" else 0),
                    after_meta["fencing_token_floor"],
                )
                self.assertEqual(200, after_meta["last_clock_ns"])

                source_operation = source["operations"][0]
                restored_operation = after["operations"][0]
                self.assertEqual(source_operation[:5], restored_operation[:5])
                self.assertEqual(source_operation[6], restored_operation[6])
                self.assertEqual(status, restored_operation[2])

                if status == "CLEANED":
                    allowed_meta_changes = {
                        "recovery_epoch",
                        "fencing_token_floor",
                        "last_clock_ns",
                    }
                    changed_meta = {
                        key
                        for key in source_meta
                        if source_meta[key] != after_meta[key]
                    }
                    self.assertTrue(changed_meta <= allowed_meta_changes)
                    self.assertEqual(
                        source["file_metadata"][:3], after["file_metadata"][:3]
                    )
                    self.assertEqual(100, len(source["raw_header"]))
                    self.assertEqual(100, len(after["raw_header"]))
                    self.assertEqual(source["operations"], after["operations"])
                    self.assertEqual(
                        source["operation_attempts"],
                        after["operation_attempts"],
                    )
                    self.assertEqual(
                        source["effect_receipts"],
                        after["effect_receipts"],
                    )
                    self.assertEqual(
                        source["transition_events"],
                        after["transition_events"],
                    )
                    continue

                self.assertEqual(1, restored_operation[5])
                self.assertEqual(200, restored_operation[7])
                source_attempts = source["operation_attempts"]
                restored_attempts = after["operation_attempts"]
                if status == "RECEIPTED":
                    self.assertEqual(
                        source_attempts[0],
                        restored_attempts[0],
                    )
                    self.assertEqual(
                        source_attempts[1][:4],
                        restored_attempts[1][:4],
                    )
                    self.assertEqual(1, restored_attempts[1][4])
                    self.assertEqual(source_attempts[1][5] + 2, restored_attempts[1][5])
                    self.assertEqual(
                        source_attempts[1][6:],
                        restored_attempts[1][6:],
                    )
                    source_receipt = source["effect_receipts"][0]
                    restored_receipt = after["effect_receipts"][0]
                    self.assertEqual(source_receipt[:7], restored_receipt[:7])
                    self.assertEqual(source_receipt[7] + 2, restored_receipt[7])
                    self.assertEqual(1, restored_receipt[8])
                    self.assertEqual(source_receipt[9:], restored_receipt[9:])
                else:
                    self.assertEqual(source_attempts, restored_attempts)
                    self.assertEqual(
                        source["effect_receipts"], after["effect_receipts"]
                    )

                source_events = source["transition_events"]
                restored_events = after["transition_events"]
                self.assertEqual(
                    source_events,
                    restored_events[: len(source_events)],
                )
                self.assertEqual(len(source_events) + 1, len(restored_events))
                restore_event = restored_events[-1]
                self.assertEqual(
                    (
                        len(source_events) + 1,
                        2,
                        "source-operation",
                        restored_operation[4],
                        status,
                        status,
                        "restore",
                        "operator",
                        200,
                        "restore",
                        mock.ANY,
                    ),
                    restore_event,
                )
                self.assertRegex(restore_event[10], r"^sha256:[0-9a-f]{64}$")

    def test_restore_incomplete_source_is_rejected_without_primary_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-incomplete-"
        ) as temporary:
            root, artifact, source_snapshot = _status_source_artifact(
                temporary,
                "RESTORE_INCOMPLETE",
            )
            primary_path = root / "coordination.sqlite3"
            primary_bytes_before = primary_path.read_bytes()
            primary_snapshot_before = _read_only_sqlite_snapshot(primary_path)
            source_path = root / artifact.database_basename
            source_bytes_before = source_path.read_bytes()
            candidate_path = root / _candidate_basename(artifact)
            self.assertFalse(candidate_path.exists())
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("incomplete source reached apply"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "cleanup",
                    side_effect=AssertionError("incomplete source reached cleanup"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("incomplete source reached replace"),
                ),
                self.assertRaises(StoreIntegrityError),
            ):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/restore-incomplete",
                )
            self.assertFalse(candidate_path.exists())
            self.assertEqual(primary_bytes_before, primary_path.read_bytes())
            self.assertEqual(
                primary_snapshot_before,
                _read_only_sqlite_snapshot(primary_path),
            )
            self.assertEqual(source_bytes_before, source_path.read_bytes())
            self.assertEqual(source_snapshot, _read_only_sqlite_snapshot(source_path))
            self.assertFalse((root / recovery_module.RECOVERY_LEDGER_BASENAME).exists())
            self.assertFalse(
                (root / recovery_module.RECOVERY_TOMBSTONES_BASENAME).exists()
            )

    def test_committed_destination_tombstones_reject_both_identity_collisions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-collision-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            result = BackupRestore(
                root,
                busy_timeout_ms=100,
                clock=FakeClock(200),
            ).restore(
                artifact,
                actor="operator",
                audit_ref="audit/tombstone-collision",
            )
            self.assertEqual(
                (("destination-only", "effect/destination-only"),),
                tuple(
                    (identity.operation_id, identity.effect_key)
                    for identity in result.active_tombstones
                ),
            )
            with CoordinationStore(
                root, busy_timeout_ms=100, clock=FakeClock(300)
            ) as store:
                with self.assertRaises(DuplicateOperationError):
                    store.create_intent(
                        "destination-only",
                        effect_key="effect/new",
                        provider_id="provider/test",
                        actor="main",
                        clock_ns=300,
                    )
                with self.assertRaises(DuplicateOperationError):
                    store.create_intent(
                        "new-operation",
                        effect_key="effect/destination-only",
                        provider_id="provider/test",
                        actor="main",
                        clock_ns=301,
                    )

    def test_committed_tombstone_current_and_past_identity_tamper_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-tombstone-binding-"
        ) as temporary:
            root, artifact = _artifact_and_newer_destination(temporary)
            initial = BackupRestore(
                root,
                busy_timeout_ms=100,
                clock=FakeClock(200),
            ).restore(
                artifact,
                actor="operator",
                audit_ref="audit/tombstone-binding",
            )
            self.assertEqual(
                (("destination-only", "effect/destination-only"),),
                tuple(
                    (identity.operation_id, identity.effect_key)
                    for identity in initial.active_tombstones
                ),
            )
            primary_before = (root / "coordination.sqlite3").read_bytes()
            tombstone_path = root / recovery_module.RECOVERY_TOMBSTONES_BASENAME
            _rewrite_tombstone_identities(
                tombstone_path,
                (("forged-operation", "effect/forged"),),
            )
            tampered_tombstones = tombstone_path.read_bytes()
            with self.assertRaises((StoreIntegrityError, RestoreReviewRequiredError)):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(300),
                ).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/tombstone-binding",
                )
            self.assertEqual(
                primary_before, (root / "coordination.sqlite3").read_bytes()
            )
            self.assertEqual(tampered_tombstones, tombstone_path.read_bytes())
            try:
                with (
                    CoordinationStore(
                        root,
                        busy_timeout_ms=100,
                        clock=FakeClock(400),
                    ) as store,
                    self.assertRaises(DuplicateOperationError),
                ):
                    store.create_intent(
                        "destination-only",
                        effect_key="effect/reused",
                        provider_id="provider/test",
                        actor="main",
                        clock_ns=400,
                    )
            except StoreIntegrityError:
                # Rejecting the tampered committed history before normal open
                # is an equally fail-closed outcome.
                pass

    def test_two_committed_generations_reject_current_and_past_tamper_before_apply(
        self,
    ) -> None:
        for tampered_generation in (1, 2):
            with (
                self.subTest(tampered_generation=tampered_generation),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-restore-generation-binding-"
                ) as temporary,
            ):
                root, first_artifact, second_artifact = (
                    _two_committed_restore_generations(temporary)
                )
                candidate_path = root / _candidate_basename(second_artifact)
                primary_path = root / store_module.DATABASE_FILENAME
                ledger_path = root / recovery_module.RECOVERY_LEDGER_BASENAME
                tombstone_path = root / recovery_module.RECOVERY_TOMBSTONES_BASENAME
                primary_before = primary_path.read_bytes()
                ledger_before = ledger_path.read_bytes()
                _rewrite_tombstone_identities(
                    tombstone_path,
                    (("forged-generation", "effect/forged-generation"),),
                    generation=tampered_generation,
                )
                tombstone_after_tamper = tombstone_path.read_bytes()
                calls = {"apply": 0, "replace": 0}

                def apply_candidate(
                    authority: RestoreStoreAuthority,
                    *args: Any,
                    calls_ref: dict[str, int] = calls,
                    **kwargs: Any,
                ) -> Any:
                    calls_ref["apply"] += 1
                    del authority, args, kwargs
                    raise AssertionError("generation tamper reached apply")

                def replace_database(
                    session: QuiescenceSession,
                    candidate: Any,
                    calls_ref: dict[str, int] = calls,
                ) -> Any:
                    calls_ref["replace"] += 1
                    del session, candidate
                    raise AssertionError("generation tamper reached replace")

                with (
                    mock.patch.object(
                        RestoreStoreAuthority,
                        "apply_candidate",
                        new=apply_candidate,
                    ),
                    mock.patch.object(
                        QuiescenceSession,
                        "replace_database",
                        new=replace_database,
                    ),
                ):
                    with self.assertRaises((StoreIntegrityError, RestoreError)):
                        BackupRestore(
                            root,
                            busy_timeout_ms=100,
                            clock=FakeClock(500),
                        ).restore(
                            second_artifact,
                            actor="operator",
                            audit_ref="audit/generation-two-new",
                        )
                    with self.assertRaises((StoreIntegrityError, RestoreError)):
                        BackupRestore(
                            root,
                            busy_timeout_ms=100,
                            clock=FakeClock(500),
                        ).resume(
                            second_artifact,
                            actor="operator",
                            audit_ref="audit/generation-two",
                        )
                    with self.assertRaises((StoreIntegrityError, RestoreError)):
                        BackupRestore(
                            root,
                            busy_timeout_ms=100,
                            clock=FakeClock(500),
                        ).restore(
                            first_artifact,
                            actor="operator",
                            audit_ref="audit/older-backup",
                        )

                self.assertEqual(0, calls["apply"])
                self.assertEqual(0, calls["replace"])
                self.assertFalse(candidate_path.exists())
                self.assertEqual(primary_before, primary_path.read_bytes())
                self.assertEqual(ledger_before, ledger_path.read_bytes())
                self.assertEqual(tombstone_after_tamper, tombstone_path.read_bytes())
                surviving_identity = (
                    ("current-only", "effect/reused")
                    if tampered_generation == 1
                    else ("destination-only", "effect/reused")
                )
                try:
                    with (
                        CoordinationStore(
                            root,
                            busy_timeout_ms=100,
                            clock=FakeClock(600),
                        ) as store,
                        self.assertRaises(DuplicateOperationError),
                    ):
                        store.create_intent(
                            surviving_identity[0],
                            effect_key=surviving_identity[1],
                            provider_id="provider/test",
                            actor="main",
                            clock_ns=600,
                        )
                except StoreIntegrityError:
                    pass
                self.assertEqual(primary_before, primary_path.read_bytes())
                self.assertEqual(ledger_before, ledger_path.read_bytes())
                self.assertEqual(tombstone_after_tamper, tombstone_path.read_bytes())

    def test_unissued_receipted_token_and_candidate_digest_fail_closed_on_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-receipt-binding-"
        ) as temporary:
            root, artifact, _ = _status_source_artifact(temporary, "RECEIPTED")
            with CoordinationStore(
                root, busy_timeout_ms=100, clock=FakeClock(100)
            ) as store:
                store.create_intent(
                    "destination-only",
                    effect_key="effect/destination-only",
                    provider_id="provider/test",
                    actor="main",
                    clock_ns=100,
                )
                store.claim(
                    "destination-only",
                    owner="owner/destination-only",
                    provider_id="provider/test",
                    lease_ttl_ns=100,
                    now_ns=101,
                )
            with self.assertRaises(RuntimeError):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                    fault=FaultAt("before_replace_call"),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/receipt-binding",
                )
            candidate_path = root / _candidate_basename(artifact)
            ledger_path = root / recovery_module.RECOVERY_LEDGER_BASENAME
            tombstone_path = root / recovery_module.RECOVERY_TOMBSTONES_BASENAME
            primary_path = root / "coordination.sqlite3"
            primary_before = primary_path.read_bytes()
            ledger_before = ledger_path.read_bytes()

            connection = sqlite3.connect(str(candidate_path))
            try:
                connection.execute(
                    "UPDATE operation_attempts SET fencing_token = 3 "
                    "WHERE operation_id = ? AND attempt = 1",
                    ("source-operation",),
                )
                connection.execute(
                    "UPDATE effect_receipts SET fencing_token = 3 "
                    "WHERE operation_id = ? AND attempt = 1",
                    ("source-operation",),
                )
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
            candidate_digest = (
                "sha256:" + hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            )
            lines = []
            for line in tombstone_path.read_bytes().splitlines():
                item = json.loads(line)
                item["candidate_digest"] = candidate_digest
                lines.append(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                )
            tombstone_path.write_bytes(b"".join(lines))
            tampered_candidate = candidate_path.read_bytes()
            tampered_tombstone = tombstone_path.read_bytes()
            with (
                mock.patch.object(
                    RestoreStoreAuthority,
                    "apply_candidate",
                    side_effect=AssertionError("receipt resume must not reapply"),
                ),
                mock.patch.object(
                    QuiescenceSession,
                    "replace_database",
                    side_effect=AssertionError("receipt token tamper must not replace"),
                ),
                self.assertRaises(StoreIntegrityError),
            ):
                BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(300),
                ).resume(
                    artifact,
                    actor="operator",
                    audit_ref="audit/receipt-binding",
                )
            self.assertEqual(primary_before, primary_path.read_bytes())
            self.assertEqual(ledger_before, ledger_path.read_bytes())
            self.assertEqual(tampered_candidate, candidate_path.read_bytes())
            self.assertEqual(tampered_tombstone, tombstone_path.read_bytes())

    def test_restore_rejects_old_claim_and_receipt_authority_after_rebase(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-old-authority-"
        ) as temporary:
            root, artifact, _ = _status_source_artifact(temporary, "CLAIMED")
            with CoordinationStore(root, busy_timeout_ms=100) as store:
                old_claim = store._rehydrate_claim("source-operation")
                self.assertIsNotNone(old_claim)
            assert old_claim is not None
            BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
                artifact,
                actor="operator",
                audit_ref="audit/old-claim",
            )
            provider = _RestoreAcceptanceProvider()
            with CoordinationStore(root, busy_timeout_ms=100) as store:
                with self.assertRaises(LeaseConflictError):
                    store.heartbeat(old_claim, lease_ttl_ns=100, now_ns=300)
                with self.assertRaises(LeaseConflictError):
                    store._begin_effect(old_claim, now_ns=301)
                with self.assertRaises(LeaseConflictError):
                    store.reserve_fence(old_claim, provider)
                with self.assertRaises(LeaseConflictError):
                    store.execute_effect(old_claim, provider, now_ns=302)
                with self.assertRaises(LeaseConflictError):
                    store.reclaim(
                        old_claim,
                        owner="replacement-owner",
                        provider_id=old_claim.provider_id,
                        effect_key=old_claim.effect_key,
                        lease_ttl_ns=100,
                        now_ns=400,
                    )
            self.assertEqual(0, provider.reserve_calls)
            self.assertEqual(0, provider.execute_calls)
            self.assertEqual(0, provider.status_calls)

        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-old-receipt-"
        ) as temporary:
            root, artifact, _ = _status_source_artifact(temporary, "RECEIPTED")
            with CoordinationStore(root, busy_timeout_ms=100) as store:
                old_receipt = store._rehydrate_receipt("source-operation")
            self.assertIsNotNone(old_receipt)
            assert old_receipt is not None
            provider = _RestoreAcceptanceProvider()
            BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
                artifact,
                actor="operator",
                audit_ref="audit/old-receipt",
            )
            with (
                CoordinationStore(root, busy_timeout_ms=100) as store,
                self.assertRaises(LeaseConflictError),
            ):
                store.complete(old_receipt, now_ns=300)
            with (
                CoordinationStore(root, busy_timeout_ms=100) as store,
                self.assertRaises(recovery_module.RecoveryConflictError),
            ):
                recovery_module.RecoveryCoordinator(store).rebind_receipt(
                    "source-operation",
                    receipt=old_receipt,
                    actor="operator",
                    now_ns=301,
                )
            self.assertEqual(0, provider.reserve_calls)
            self.assertEqual(0, provider.execute_calls)
            self.assertEqual(0, provider.status_calls)

        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-old-effect-"
        ) as temporary:
            root, artifact, _ = _status_source_artifact(temporary, "UNKNOWN_EFFECT")
            with (
                CoordinationStore(root, busy_timeout_ms=100) as store,
                store._recovery_transaction() as transaction,
            ):
                old_effect = transaction.recovery_effect("source-operation")
            self.assertIsNotNone(old_effect)
            assert old_effect is not None
            self.assertEqual("source-operation", old_effect.operation_id)
            BackupRestore(root, busy_timeout_ms=100, clock=FakeClock(200)).restore(
                artifact,
                actor="operator",
                audit_ref="audit/old-effect",
            )
            provider = _RestoreAcceptanceProvider()
            with (
                CoordinationStore(root, busy_timeout_ms=100) as store,
                self.assertRaises(recovery_module.RecoveryRequiredError),
            ):
                recovery_module.RecoveryCoordinator(store).resolve_unknown(
                    "source-operation",
                    provider=provider,
                    actor="operator",
                    now_ns=300,
                )
            self.assertEqual(0, provider.reserve_calls)
            self.assertEqual(0, provider.execute_calls)
            self.assertEqual(0, provider.status_calls)

    def test_orchestration_fault_matrix_classifies_state_and_resume_outcome(
        self,
    ) -> None:
        fault_points = (
            "before_source_open_call",
            "after_source_open_call",
            "before_destination_open_call",
            "after_destination_open_call",
            "before_candidate_create_call",
            "after_candidate_create_call",
            "before_store_observation_call",
            "after_store_observation_call",
            "before_floor_reservation_call",
            "after_floor_reservation_call",
            "before_store_apply_call",
            "after_store_apply_call",
            "before_candidate_verify_call",
            "after_candidate_verify_call",
            "before_ledger_prepare_call",
            "after_ledger_prepare_call",
            "before_replace_call",
            "after_replace_call",
            "before_mark_replaced_call",
            "after_mark_replaced_call",
            "before_mark_committed_call",
            "after_mark_committed_call",
            "before_final_artifact_inspect_call",
            "before_final_primary_verify_call",
            "before_result_call",
        )
        candidate_only_points = frozenset(
            {
                "after_store_apply_call",
                "before_candidate_verify_call",
                "after_candidate_verify_call",
                "before_ledger_prepare_call",
            }
        )
        absent = {
            "phase_pair": None,
            "candidate": "absent",
            "primary_changed": False,
            "primary_new": False,
            "initial_apply": 0,
            "initial_replace": 0,
            "resume": "reject",
            "resume_apply": 0,
            "resume_replace": 0,
            "resume_pair_changed": False,
        }
        expected: dict[str, dict[str, Any]] = {
            point: dict(absent) for point in fault_points
        }
        for point in (
            "after_candidate_create_call",
            "before_floor_reservation_call",
            "after_floor_reservation_call",
            "before_store_apply_call",
        ):
            expected[point]["candidate"] = "empty"
        for point in (
            "after_store_apply_call",
            "before_candidate_verify_call",
            "after_candidate_verify_call",
            "before_ledger_prepare_call",
        ):
            expected[point]["candidate"] = "nonempty"
            expected[point]["initial_apply"] = 1
        for point in fault_points[2:]:
            expected[point]["primary_changed"] = True
        for point in (
            "after_ledger_prepare_call",
            "before_replace_call",
        ):
            expected[point].update(
                {
                    "phase_pair": ("RESTORE_PREPARED", "PREPARED"),
                    "candidate": "nonempty",
                    "initial_apply": 1,
                    "resume": "commit",
                    "resume_replace": 1,
                    "resume_pair_changed": True,
                }
            )
        expected["after_replace_call"].update(
            {
                "phase_pair": ("RESTORE_PREPARED", "PREPARED"),
                "candidate": "absent",
                "primary_changed": True,
                "primary_new": True,
                "initial_apply": 1,
                "initial_replace": 1,
                "resume": "review",
            }
        )
        expected["before_mark_replaced_call"].update(
            {
                "phase_pair": ("RESTORE_PREPARED", "PREPARED"),
                "candidate": "absent",
                "primary_changed": True,
                "primary_new": True,
                "initial_apply": 1,
                "initial_replace": 1,
                "resume": "review",
            }
        )
        for point in ("after_mark_replaced_call", "before_mark_committed_call"):
            expected[point].update(
                {
                    "phase_pair": ("RESTORE_REPLACED", "PREPARED"),
                    "candidate": "absent",
                    "primary_changed": True,
                    "primary_new": True,
                    "initial_apply": 1,
                    "initial_replace": 1,
                    "resume": "commit",
                    "resume_pair_changed": True,
                }
            )
        for point in (
            "after_mark_committed_call",
            "before_final_artifact_inspect_call",
            "before_final_primary_verify_call",
            "before_result_call",
        ):
            expected[point].update(
                {
                    "phase_pair": ("RESTORE_COMMITTED", "COMMITTED"),
                    "candidate": "absent",
                    "primary_changed": True,
                    "primary_new": True,
                    "initial_apply": 1,
                    "initial_replace": 1,
                    "resume": "no_op",
                }
            )

        for point in fault_points:
            with (
                self.subTest(point=point),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-restore-fault-acceptance-"
                ) as temporary,
            ):
                root, artifact = _artifact_and_newer_destination(temporary)
                candidate_path = root / _candidate_basename(artifact)
                ledger_path = root / recovery_module.RECOVERY_LEDGER_BASENAME
                tombstone_path = root / recovery_module.RECOVERY_TOMBSTONES_BASENAME
                primary_path = root / "coordination.sqlite3"
                primary_before_snapshot = (
                    _read_only_sqlite_snapshot(primary_path)
                    if point in candidate_only_points
                    else None
                )
                source_observation = (
                    _read_store_observation(root / artifact.database_basename)
                    if point in candidate_only_points
                    else None
                )
                before = {
                    "primary": primary_path.read_bytes(),
                    "candidate": (
                        None
                        if not candidate_path.exists()
                        else candidate_path.read_bytes()
                    ),
                    "ledger": (
                        None if not ledger_path.exists() else ledger_path.read_bytes()
                    ),
                    "tombstone": (
                        None
                        if not tombstone_path.exists()
                        else tombstone_path.read_bytes()
                    ),
                }
                calls = {"apply": 0, "replace": 0}
                applied_results: list[RestoreApplyResult] = []
                original_apply = RestoreStoreAuthority.apply_candidate
                original_replace = QuiescenceSession.replace_database

                def apply_candidate(
                    authority: RestoreStoreAuthority,
                    *args: Any,
                    calls_ref: dict[str, int] = calls,
                    results_ref: list[RestoreApplyResult] = applied_results,
                    apply_impl: Any = original_apply,
                    **kwargs: Any,
                ) -> Any:
                    calls_ref["apply"] += 1
                    result = apply_impl(authority, *args, **kwargs)
                    results_ref.append(result)
                    return result

                def replace_database(
                    session: QuiescenceSession,
                    candidate: Any,
                    *,
                    calls_ref: dict[str, int] = calls,
                    replace_impl: Any = original_replace,
                ) -> Any:
                    calls_ref["replace"] += 1
                    return replace_impl(session, candidate)

                with (
                    mock.patch.object(
                        RestoreStoreAuthority,
                        "apply_candidate",
                        new=apply_candidate,
                    ),
                    mock.patch.object(
                        QuiescenceSession,
                        "replace_database",
                        new=replace_database,
                    ),
                    mock.patch.object(
                        lease_module,
                        "require_provider_capabilities",
                        side_effect=AssertionError(
                            "fault matrix entered provider path"
                        ),
                    ),
                    mock.patch.object(
                        recovery_module,
                        "RecoveryCoordinator",
                        side_effect=AssertionError(
                            "fault matrix entered recovery coordinator"
                        ),
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    BackupRestore(
                        root,
                        busy_timeout_ms=100,
                        clock=FakeClock(200),
                        fault=FaultAt(point),
                    ).restore(
                        artifact,
                        actor="operator",
                        audit_ref=f"audit/fault-matrix/{point}",
                    )

                actual_pair: tuple[str, str] | None = None
                if ledger_path.exists() or tombstone_path.exists():
                    self.assertTrue(ledger_path.exists())
                    self.assertTrue(tombstone_path.exists())
                    ledger_lines = ledger_path.read_bytes().splitlines()
                    tombstone_lines = tombstone_path.read_bytes().splitlines()
                    self.assertTrue(ledger_lines)
                    self.assertTrue(tombstone_lines)
                    for line in (*ledger_lines, *tombstone_lines):
                        self.assertEqual(
                            line,
                            json.dumps(
                                json.loads(line),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8"),
                        )
                    actual_pair = (
                        str(json.loads(ledger_lines[-1])["phase"]),
                        str(json.loads(tombstone_lines[-1])["phase"]),
                    )
                self.assertEqual(expected[point]["phase_pair"], actual_pair)
                candidate_bytes = (
                    None if not candidate_path.exists() else candidate_path.read_bytes()
                )
                candidate_state = (
                    "absent"
                    if candidate_bytes is None
                    else "empty"
                    if candidate_bytes == b""
                    else "nonempty"
                )
                self.assertEqual(expected[point]["candidate"], candidate_state)
                self.assertEqual(
                    expected[point]["primary_changed"],
                    primary_path.read_bytes() != before["primary"],
                )
                self.assertEqual(expected[point]["initial_apply"], calls["apply"])
                self.assertEqual(expected[point]["initial_replace"], calls["replace"])
                if expected[point]["phase_pair"] is not None:
                    phase_pair = cast(tuple[str, str], expected[point]["phase_pair"])
                    _assert_pending_restore_evidence(
                        self,
                        root=root,
                        artifact=artifact,
                        phase_pair=phase_pair,
                        actor="operator",
                        audit_ref=f"audit/fault-matrix/{point}",
                        candidate_present=expected[point]["candidate"] == "nonempty",
                        primary_is_new=bool(expected[point]["primary_new"]),
                    )
                if point in candidate_only_points:
                    self.assertEqual(1, len(applied_results))
                    assert primary_before_snapshot is not None
                    assert source_observation is not None
                    destination_observation = _read_store_observation(primary_path)
                    _assert_captured_candidate_result(
                        self,
                        root=root,
                        artifact=artifact,
                        captured=applied_results[0],
                        actor="operator",
                        audit_ref=f"audit/fault-matrix/{point}",
                        source_observation=source_observation,
                        destination_observation=destination_observation,
                        primary_before_snapshot=primary_before_snapshot,
                    )
                    self.assertFalse(ledger_path.exists())
                    self.assertFalse(tombstone_path.exists())
                durable_before_resume = {
                    "primary": primary_path.read_bytes(),
                    "candidate": candidate_bytes,
                    "ledger": (
                        None if not ledger_path.exists() else ledger_path.read_bytes()
                    ),
                    "tombstone": (
                        None
                        if not tombstone_path.exists()
                        else tombstone_path.read_bytes()
                    ),
                }

                resume_error: BaseException | None = None
                resume_result: RestoreResult | None = None
                try:
                    with (
                        mock.patch.object(
                            RestoreStoreAuthority,
                            "apply_candidate",
                            new=apply_candidate,
                        ),
                        mock.patch.object(
                            QuiescenceSession,
                            "replace_database",
                            new=replace_database,
                        ),
                        mock.patch.object(
                            lease_module,
                            "require_provider_capabilities",
                            side_effect=AssertionError(
                                "fault matrix resume entered provider path"
                            ),
                        ),
                        mock.patch.object(
                            recovery_module,
                            "RecoveryCoordinator",
                            side_effect=AssertionError(
                                "fault matrix resume entered recovery coordinator"
                            ),
                        ),
                    ):
                        resume_result = BackupRestore(
                            root,
                            busy_timeout_ms=100,
                            clock=FakeClock(300),
                        ).resume(
                            artifact,
                            actor="operator",
                            audit_ref=f"audit/fault-matrix/{point}",
                        )
                except RestoreError as error:
                    resume_error = error
                actual_resume = (
                    "review"
                    if isinstance(resume_error, RestoreReviewRequiredError)
                    else "reject"
                    if resume_error is not None
                    else "no_op"
                    if actual_pair == ("RESTORE_COMMITTED", "COMMITTED")
                    else "commit"
                )
                self.assertEqual(expected[point]["resume"], actual_resume)
                if expected[point]["resume"] == "review":
                    self.assertIsInstance(resume_error, RestoreReviewRequiredError)
                elif expected[point]["resume"] == "reject":
                    self.assertIsInstance(resume_error, RestoreError)
                else:
                    self.assertIsNotNone(resume_result)
                    assert resume_result is not None
                    self.assertEqual("RESTORE_COMMITTED", resume_result.phase)
                    _assert_committed_restore_evidence(
                        self,
                        root=root,
                        artifact=artifact,
                        result=resume_result,
                        actor="operator",
                        audit_ref=f"audit/fault-matrix/{point}",
                        expected_restore_events=1,
                    )
                self.assertEqual(
                    expected[point]["resume_apply"],
                    calls["apply"] - int(expected[point]["initial_apply"]),
                )
                self.assertEqual(
                    expected[point]["resume_replace"],
                    calls["replace"] - int(expected[point]["initial_replace"]),
                )
                after_resume = {
                    "primary": primary_path.read_bytes(),
                    "candidate": (
                        None
                        if not candidate_path.exists()
                        else candidate_path.read_bytes()
                    ),
                    "ledger": (
                        None if not ledger_path.exists() else ledger_path.read_bytes()
                    ),
                    "tombstone": (
                        None
                        if not tombstone_path.exists()
                        else tombstone_path.read_bytes()
                    ),
                }
                if expected[point]["resume"] in {"reject", "review", "no_op"}:
                    self.assertEqual(durable_before_resume, after_resume)
                else:
                    self.assertIsNone(after_resume["candidate"])
                    self.assertNotEqual(
                        durable_before_resume["ledger"], after_resume["ledger"]
                    )
                    self.assertNotEqual(
                        durable_before_resume["tombstone"],
                        after_resume["tombstone"],
                    )
                self.assertEqual(
                    expected[point]["resume_pair_changed"],
                    (
                        durable_before_resume["ledger"],
                        durable_before_resume["tombstone"],
                    )
                    != (
                        after_resume["ledger"],
                        after_resume["tombstone"],
                    ),
                )

    def test_sigkill_representative_restore_points_resume_from_exact_evidence(
        self,
    ) -> None:
        cases = {
            "after_ledger_prepare_call": {
                "phase_pair": ("RESTORE_PREPARED", "PREPARED"),
                "candidate": "nonempty",
                "primary_new": False,
                "resume": "commit",
                "resume_replace": 1,
                "pair_changed": True,
            },
            "before_replace_call": {
                "phase_pair": ("RESTORE_PREPARED", "PREPARED"),
                "candidate": "nonempty",
                "primary_new": False,
                "resume": "commit",
                "resume_replace": 1,
                "pair_changed": True,
            },
            "after_mark_replaced_call": {
                "phase_pair": ("RESTORE_REPLACED", "PREPARED"),
                "candidate": "absent",
                "primary_new": True,
                "resume": "commit",
                "resume_replace": 0,
                "pair_changed": True,
            },
            "after_mark_committed_call": {
                "phase_pair": ("RESTORE_COMMITTED", "COMMITTED"),
                "candidate": "absent",
                "primary_new": True,
                "resume": "no_op",
                "resume_replace": 0,
                "pair_changed": False,
            },
            "before_result_call": {
                "phase_pair": ("RESTORE_COMMITTED", "COMMITTED"),
                "candidate": "absent",
                "primary_new": True,
                "resume": "no_op",
                "resume_replace": 0,
                "pair_changed": False,
            },
        }
        context = multiprocessing.get_context("spawn")
        for point, expectation in cases.items():
            with (
                self.subTest(point=point),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-restore-sigkill-acceptance-"
                ) as temporary,
            ):
                root, artifact = _artifact_and_newer_destination(temporary)
                candidate_path = root / _candidate_basename(artifact)
                ledger_path = root / recovery_module.RECOVERY_LEDGER_BASENAME
                tombstone_path = root / recovery_module.RECOVERY_TOMBSTONES_BASENAME
                primary_path = root / "coordination.sqlite3"
                primary_before = primary_path.read_bytes()
                process = context.Process(
                    target=_kill_restore_worker,
                    args=(str(root), artifact, point),
                )
                process.start()
                process.join(timeout=30)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5)
                    self.fail(f"SIGKILL worker did not exit at {point}")
                self.assertEqual(-signal.SIGKILL, process.exitcode, point)
                process.close()

                self.assertEqual(
                    expectation["phase_pair"],
                    (
                        str(
                            json.loads(ledger_path.read_bytes().splitlines()[-1])[
                                "phase"
                            ]
                        ),
                        str(
                            json.loads(tombstone_path.read_bytes().splitlines()[-1])[
                                "phase"
                            ]
                        ),
                    ),
                )
                candidate_bytes = (
                    None if not candidate_path.exists() else candidate_path.read_bytes()
                )
                candidate_state = (
                    "absent"
                    if candidate_bytes is None
                    else "empty"
                    if candidate_bytes == b""
                    else "nonempty"
                )
                self.assertEqual(expectation["candidate"], candidate_state)
                self.assertNotEqual(primary_before, primary_path.read_bytes())
                _assert_pending_restore_evidence(
                    self,
                    root=root,
                    artifact=artifact,
                    phase_pair=cast(tuple[str, str], expectation["phase_pair"]),
                    actor="operator",
                    audit_ref=f"audit/sigkill/{point}",
                    candidate_present=expectation["candidate"] == "nonempty",
                    primary_is_new=bool(expectation["primary_new"]),
                )

                durable_before_resume = {
                    "primary": primary_path.read_bytes(),
                    "candidate": candidate_bytes,
                    "ledger": ledger_path.read_bytes(),
                    "tombstone": tombstone_path.read_bytes(),
                }
                replace_calls = 0
                original_replace = QuiescenceSession.replace_database

                def replace_database(
                    session: QuiescenceSession,
                    candidate: Any,
                    *,
                    replace_impl: Any = original_replace,
                ) -> Any:
                    nonlocal replace_calls
                    replace_calls += 1
                    return replace_impl(session, candidate)

                with (
                    mock.patch.object(
                        RestoreStoreAuthority,
                        "apply_candidate",
                        side_effect=AssertionError("SIGKILL resume must not reapply"),
                    ),
                    mock.patch.object(
                        QuiescenceSession,
                        "replace_database",
                        new=replace_database,
                    ),
                    mock.patch.object(
                        lease_module,
                        "require_provider_capabilities",
                        side_effect=AssertionError(
                            "SIGKILL resume entered provider path"
                        ),
                    ),
                    mock.patch.object(
                        recovery_module,
                        "RecoveryCoordinator",
                        side_effect=AssertionError(
                            "SIGKILL resume entered recovery coordinator"
                        ),
                    ),
                ):
                    resumed = BackupRestore(
                        root,
                        busy_timeout_ms=100,
                        clock=FakeClock(300),
                    ).resume(
                        artifact,
                        actor="operator",
                        audit_ref=f"audit/sigkill/{point}",
                    )
                self.assertEqual("RESTORE_COMMITTED", resumed.phase)
                _assert_committed_restore_evidence(
                    self,
                    root=root,
                    artifact=artifact,
                    result=resumed,
                    actor="operator",
                    audit_ref=f"audit/sigkill/{point}",
                    expected_restore_events=1,
                )
                self.assertEqual(expectation["resume_replace"], replace_calls)
                after_resume = {
                    "primary": primary_path.read_bytes(),
                    "candidate": (
                        None
                        if not candidate_path.exists()
                        else candidate_path.read_bytes()
                    ),
                    "ledger": ledger_path.read_bytes(),
                    "tombstone": tombstone_path.read_bytes(),
                }
                if expectation["resume"] == "no_op":
                    self.assertEqual(durable_before_resume, after_resume)
                else:
                    self.assertIsNone(after_resume["candidate"])
                    self.assertNotEqual(
                        durable_before_resume["ledger"],
                        after_resume["ledger"],
                    )
                    self.assertNotEqual(
                        durable_before_resume["tombstone"],
                        after_resume["tombstone"],
                    )
                self.assertEqual(
                    expectation["pair_changed"],
                    (
                        durable_before_resume["ledger"],
                        durable_before_resume["tombstone"],
                    )
                    != (
                        after_resume["ledger"],
                        after_resume["tombstone"],
                    ),
                )

    def test_intent_restore_preserves_projection_and_advances_only_floor(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-acceptance-"
        ) as temporary:
            root, artifact, source = _status_source_artifact(temporary, "INTENT")
            before_primary = _read_only_sqlite_snapshot(root / "coordination.sqlite3")
            with (
                mock.patch.object(
                    lease_module,
                    "require_provider_capabilities",
                    side_effect=AssertionError("restore entered provider path"),
                ),
                mock.patch.object(
                    recovery_module,
                    "RecoveryCoordinator",
                    side_effect=AssertionError("restore entered recovery coordinator"),
                ),
            ):
                result = BackupRestore(
                    root,
                    busy_timeout_ms=100,
                    clock=FakeClock(200),
                ).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/acceptance/intent",
                )

            after_primary = _read_only_sqlite_snapshot(root / "coordination.sqlite3")
            self.assertEqual("RESTORE_COMMITTED", result.phase)
            self.assertEqual(source["user_version"], after_primary["user_version"])
            source_operation = source["operations"][0]
            restored_operation = after_primary["operations"][0]
            self.assertEqual(source_operation[:5], restored_operation[:5])
            self.assertEqual(source_operation[6], restored_operation[6])
            self.assertEqual(1, restored_operation[5])
            self.assertEqual(200, restored_operation[7])
            self.assertEqual(
                source["operation_attempts"],
                after_primary["operation_attempts"],
            )
            self.assertEqual(
                source["effect_receipts"], after_primary["effect_receipts"]
            )
            self.assertEqual(
                tuple(source["transition_events"]),
                tuple(after_primary["transition_events"])[
                    : len(source["transition_events"])
                ],
            )
            self.assertEqual(
                (
                    len(source["transition_events"]) + 1,
                    2,
                    "source-operation",
                    0,
                    "INTENT",
                    "INTENT",
                    "restore",
                    "operator",
                    200,
                    "restore",
                    mock.ANY,
                ),
                tuple(after_primary["transition_events"])[-1],
            )
            self.assertRegex(
                tuple(after_primary["transition_events"])[-1][10],
                r"^sha256:[0-9a-f]{64}$",
            )
            self.assertNotEqual(
                before_primary["operations"], after_primary["operations"]
            )


if __name__ == "__main__":
    unittest.main()
